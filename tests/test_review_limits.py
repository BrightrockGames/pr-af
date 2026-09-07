"""Review caps apply on every invocation path, not just the webhook.

``.env.example`` documents ``PR_AF_MAX_CONCURRENT_REVIEWERS`` /
``PR_AF_MAX_REVIEW_DEPTH`` / ``PR_AF_MAX_COVERAGE_ITERATIONS`` as deployment
caps, but ``scripts/ci_runner.py`` used to send those fields as null — so a host
tuned to survive ``PR_AF_MAX_REVIEW_DEPTH=0`` silently got the full-depth
default whenever CI triggered the review.

``ci_runner.py`` is deliberately stdlib-only (a CI job runs it straight from a
checkout, without installing the package), so it carries a *copy* of the cap
table rather than importing it. The first test is the drift guard for that copy.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from pr_af.app import REVIEW_LIMIT_ENV_SPEC, _webhook_review_limits


def _load_ci_runner() -> ModuleType:
    """Import scripts/ci_runner.py by path — it is a script, not a package."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "ci_runner.py"
    spec = importlib.util.spec_from_file_location("pr_af_ci_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pr_af_ci_runner"] = module
    spec.loader.exec_module(module)
    return module


ci_runner = _load_ci_runner()


def test_cap_tables_do_not_drift() -> None:
    """The CI script's copy of the cap table must match the node's."""
    assert tuple(ci_runner.REVIEW_LIMIT_ENV_SPEC) == tuple(REVIEW_LIMIT_ENV_SPEC)


def test_documented_caps_are_all_covered() -> None:
    """Every cap .env.example advertises must be in the table."""
    covered = {env for env, _, _ in REVIEW_LIMIT_ENV_SPEC}
    assert {
        "PR_AF_MAX_CONCURRENT_REVIEWERS",
        "PR_AF_MAX_REVIEW_DEPTH",
        "PR_AF_MAX_COVERAGE_ITERATIONS",
    } <= covered


def test_unset_env_sends_no_caps() -> None:
    """Default behaviour is unchanged when nothing is configured."""
    assert ci_runner.resolve_review_limits({}) == {}


def test_caps_become_review_inputs() -> None:
    limits = ci_runner.resolve_review_limits(
        {
            "PR_AF_MAX_CONCURRENT_REVIEWERS": "1",
            "PR_AF_MAX_REVIEW_DEPTH": "0",
            "PR_AF_MAX_COVERAGE_ITERATIONS": "1",
        }
    )
    assert limits == {
        "max_concurrent_reviewers": 1,
        "max_review_depth": 0,
        "max_coverage_iterations": 1,
    }


@pytest.mark.parametrize(
    ("env", "value"),
    [
        ("PR_AF_MAX_CONCURRENT_REVIEWERS", "0"),
        ("PR_AF_MAX_REVIEW_DEPTH", "-1"),
        ("PR_AF_MAX_COVERAGE_ITERATIONS", "lots"),
    ],
)
def test_invalid_caps_are_dropped(env: str, value: str) -> None:
    """A bad value must be ignored, not passed through as a broken input."""
    assert ci_runner.resolve_review_limits({env: value}) == {}


def test_ci_and_webhook_agree_on_the_same_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both paths must derive identical review inputs from identical env."""
    env = {
        "PR_AF_MAX_CONCURRENT_REVIEWERS": "2",
        "PR_AF_MAX_REVIEW_DEPTH": "0",
        "PR_AF_MAX_COVERAGE_ITERATIONS": "3",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PR_AF_MAX_CONCURRENT_AGENTS", raising=False)
    assert _webhook_review_limits() == ci_runner.resolve_review_limits(env)
