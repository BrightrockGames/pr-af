"""Long git operations must not run on the event loop thread.

``_resolve_repo`` shells out to git, and on a large monorepo the clone +
checkout takes minutes. Called inline from ``async def review()`` it blocked
the loop for that whole window, starving the SDK's heartbeat task — a real run
against a Unity monorepo produced, partway into a 4.5-minute clone::

    Agent pr-af marked inactive after 4 consecutive failures
    ...
    Agent pr-af recovered to active

The control plane can drop a node it believes is dead, and raising
``PR_AF_GIT_TIMEOUT_SECONDS`` only widens the window, so the blocking calls now
run in a worker thread.

Each test asserts the blocking work happens off the **main** thread, which is
the thread the event loop runs on. That is deliberately a statement about where
the work runs rather than about `asyncio.to_thread` specifically — any
offloading satisfies it — and it fails if the call is moved back inline.
"""

from __future__ import annotations

import threading

import pytest

from pr_af import app as app_module
from pr_af.orchestrator import ReviewOrchestrator
from pr_af.schemas.input import ReviewInput


class _Boom(Exception):
    """Ends the pipeline right after the call under test."""


async def test_review_resolves_the_repo_off_the_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review() must not run the clone/checkout on the event loop thread.

    Drives the real review() far enough to reach repo resolution; the
    orchestrator is stubbed to fail immediately afterwards, so this needs no
    control plane, harness, or LLM.
    """
    seen: dict[str, threading.Thread | None] = {"thread": None}

    def recording_resolve(repo_path: str | None, pr_url: str | None) -> str:
        seen["thread"] = threading.current_thread()
        return "/workspaces/widgets-pr1"

    class _StubOrchestrator:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def run(self) -> object:
            raise _Boom("stop here — the resolve already happened")

    monkeypatch.setattr(app_module, "_resolve_repo", recording_resolve)
    monkeypatch.setattr(app_module, "ReviewOrchestrator", _StubOrchestrator)

    with pytest.raises(Exception) as excinfo:
        await app_module.review(pr_url="https://github.com/acme/widgets/pull/1")
    # The stub's failure is what surfaces, proving we got past repo resolution
    # rather than failing inside it.
    assert "stop here" in str(excinfo.value)

    assert seen["thread"] is not None, "_resolve_repo was never called"
    assert seen["thread"] is not threading.main_thread(), (
        "_resolve_repo ran on the event loop thread — a multi-minute clone will "
        "starve the node's heartbeat and the control plane will mark it inactive"
    )


async def test_repo_diff_runs_off_the_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repo_path `git diff` is bounded by the same timeout, so same rule."""
    seen: dict[str, threading.Thread | None] = {"thread": None}

    def recording_diff(
        self: ReviewOrchestrator,
        repo_path: str,
        base_ref: str | None,
        head_ref: str | None,
    ) -> str:
        seen["thread"] = threading.current_thread()
        raise _Boom("stop here — the diff already happened")

    monkeypatch.setattr(ReviewOrchestrator, "_compute_repo_diff", recording_diff)

    orchestrator = ReviewOrchestrator(
        app=None,
        input=ReviewInput(repo_path="/workspaces/widgets", depth="quick"),
    )
    with pytest.raises(_Boom):
        await orchestrator._run_intake()

    assert seen["thread"] is not None, "_compute_repo_diff was never called"
    assert seen["thread"] is not threading.main_thread(), (
        "_compute_repo_diff ran on the event loop thread; `git diff` over a "
        "large tree can block for as long as PR_AF_GIT_TIMEOUT_SECONDS"
    )
