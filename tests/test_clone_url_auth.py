"""Clone-URL credential form for GitHub tokens.

Regression test for the failure seen with a GitHub Actions `secrets.GITHUB_TOKEN`
(a GitHub App *installation* token, ``ghs_…``)::

    remote: Invalid username or token. Password authentication is not
    supported for Git operations.
    fatal: Authentication failed for 'https://github.com/<org>/<repo>.git/'

Installation/App tokens are only accepted as
``https://x-access-token:<token>@github.com/…``. The bare-token userinfo form
this used to build works for classic PATs (``ghp_…``) but not for those, which
is why it went unnoticed. ``x-access-token`` covers both, so it is the only form
used — if a future change "simplifies" the username away, these fail.
"""

from __future__ import annotations

from pr_af.app import _tokenized_clone_url


def test_installation_token_uses_x_access_token_username() -> None:
    url = _tokenized_clone_url(
        "https://github.com/acme/widgets.git", "ghs_installationtoken"
    )
    assert url == (
        "https://x-access-token:ghs_installationtoken@github.com/acme/widgets.git"
    )


def test_classic_pat_uses_the_same_form() -> None:
    """One code path for both token kinds — no branch on token prefix."""
    url = _tokenized_clone_url("https://github.com/acme/widgets.git", "ghp_classicpat")
    assert url == "https://x-access-token:ghp_classicpat@github.com/acme/widgets.git"


def test_no_bare_token_userinfo() -> None:
    """The exact shape GitHub rejects for installation tokens must never appear."""
    url = _tokenized_clone_url("https://github.com/acme/widgets.git", "ghs_tok")
    assert "https://ghs_tok@github.com/" not in url
    assert url.startswith("https://x-access-token:")


def test_empty_token_leaves_url_untouched() -> None:
    plain = "https://github.com/acme/widgets.git"
    assert _tokenized_clone_url(plain, "") == plain


def test_non_github_and_non_https_urls_are_untouched() -> None:
    for url in (
        "https://gitlab.com/acme/widgets.git",
        "git@github.com:acme/widgets.git",
        "http://github.com/acme/widgets.git",
    ):
        assert _tokenized_clone_url(url, "ghs_tok") == url


def test_only_the_host_prefix_is_rewritten() -> None:
    """A repo path that happens to contain the prefix text is not double-rewritten."""
    url = _tokenized_clone_url(
        "https://github.com/acme/https://github.com/.git", "ghs_tok"
    )
    assert url.count("x-access-token") == 1
