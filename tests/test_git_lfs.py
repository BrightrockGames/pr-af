"""``PR_AF_SKIP_GIT_LFS`` and its effect on the git subprocess environment.

Skipping Git-LFS content used to be an *implicit* consequence of git-lfs not
being installed in the runtime image: nothing in the code asked for it, so the
behaviour would have flipped silently the moment git-lfs appeared on PATH (the
Docker images now install it, precisely so the opt-in below works). These tests
pin the intended default — pointer stubs, not gigabytes of binary assets — and
the escape hatch.
"""

from __future__ import annotations

import pytest

from pr_af.config import git_env, skip_git_lfs


def test_default_skips_lfs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PR_AF_SKIP_GIT_LFS", raising=False)
    monkeypatch.delenv("GIT_LFS_SKIP_SMUDGE", raising=False)
    assert skip_git_lfs() is True
    assert git_env()["GIT_LFS_SKIP_SMUDGE"] == "1"


@pytest.mark.parametrize("raw", ["1", "true", "yes", "TRUE", " True "])
def test_truthy_values_skip_lfs(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("PR_AF_SKIP_GIT_LFS", raw)
    assert skip_git_lfs() is True
    assert git_env()["GIT_LFS_SKIP_SMUDGE"] == "1"


@pytest.mark.parametrize("raw", ["0", "false", "no", "FALSE", " no "])
def test_falsy_values_allow_lfs(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("PR_AF_SKIP_GIT_LFS", raw)
    assert skip_git_lfs() is False
    assert "GIT_LFS_SKIP_SMUDGE" not in git_env()


def test_optin_overrides_inherited_smudge_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inherited GIT_LFS_SKIP_SMUDGE must not defeat an explicit opt-in."""
    monkeypatch.setenv("PR_AF_SKIP_GIT_LFS", "0")
    monkeypatch.setenv("GIT_LFS_SKIP_SMUDGE", "1")
    assert "GIT_LFS_SKIP_SMUDGE" not in git_env()
