"""``PR_AF_GIT_TIMEOUT_SECONDS`` resolution.

The git subprocess timeouts used to be per-call literals, and ``checkout``'s
was 30s — short enough that a large monorepo died with
``git checkout ... timed out after 30 seconds`` while git was still writing the
working tree. One env var now covers clone/fetch/checkout/diff in both nodes.

Bad values must degrade to the default rather than to "no timeout": an
unbounded git call hangs the whole review with no diagnostic.
"""

from __future__ import annotations

import pytest

from pr_af.config import DEFAULT_GIT_TIMEOUT_SECONDS, git_env, git_timeout_seconds


def test_default_is_the_largest_previous_literal() -> None:
    """No git call may get a shorter budget than the pre-env-var code did."""
    assert DEFAULT_GIT_TIMEOUT_SECONDS == 600


def test_unset_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PR_AF_GIT_TIMEOUT_SECONDS", raising=False)
    assert git_timeout_seconds() == float(DEFAULT_GIT_TIMEOUT_SECONDS)


def test_empty_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PR_AF_GIT_TIMEOUT_SECONDS", "")
    assert git_timeout_seconds() == float(DEFAULT_GIT_TIMEOUT_SECONDS)


@pytest.mark.parametrize(("raw", "expected"), [("1800", 1800.0), ("45.5", 45.5)])
def test_explicit_value_wins(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    monkeypatch.setenv("PR_AF_GIT_TIMEOUT_SECONDS", raw)
    assert git_timeout_seconds() == expected


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "5m"])
def test_invalid_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Never disable the timeout — a hung git call is worse than a timeout."""
    monkeypatch.setenv("PR_AF_GIT_TIMEOUT_SECONDS", raw)
    assert git_timeout_seconds() == float(DEFAULT_GIT_TIMEOUT_SECONDS)


def test_git_env_disables_interactive_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing credential must fail fast, not block on a terminal prompt."""
    monkeypatch.setenv("PR_AF_SOME_PASSTHROUGH", "kept")
    env = git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "echo"
    assert env["PR_AF_SOME_PASSTHROUGH"] == "kept"
