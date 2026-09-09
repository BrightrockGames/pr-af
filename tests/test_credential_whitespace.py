"""Credentials must reach providers without surrounding whitespace.

A production run failed repeatedly with OpenRouter answering

    401 {"error": {"message": "User not found.", "code": 401}}

for a key the operator had validated by hand. A secret stored with a trailing
newline — the ordinary result of pasting into a GitHub Actions secret field —
is forwarded byte for byte, and the provider sees an Authorization header it
cannot match. That 401 is identical to the one an unknown key produces, so the
symptom points at the key's validity rather than at its transport.

These tests pin the trim at every seam a credential crosses, because fixing
only one of them (the ``.ai()`` client) would leave the other (the harness
subprocess environment) broken, and the run would look unchanged.
"""

from __future__ import annotations

import inspect

import pytest

from pr_af.config import AIIntegrationConfig, credential

DIRTY = "sk-or-v1-abc123\n"
CLEAN = "sk-or-v1-abc123"


@pytest.mark.parametrize(
    "raw",
    [
        "sk-or-v1-abc123\n",
        "sk-or-v1-abc123\r\n",
        " sk-or-v1-abc123 ",
        "\tsk-or-v1-abc123\t",
        "\nsk-or-v1-abc123",
    ],
    ids=["newline", "crlf", "spaces", "tabs", "leading-newline"],
)
def test_surrounding_whitespace_is_stripped(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", raw)
    assert credential("OPENROUTER_API_KEY") == CLEAN


def test_interior_characters_are_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the edges are trimmed; the key's own bytes are never rewritten."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "  sk-or-v1-a b c  ")
    assert credential("OPENROUTER_API_KEY") == "sk-or-v1-a b c"


def test_unset_returns_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert credential("OPENROUTER_API_KEY") == ""
    assert credential("OPENROUTER_API_KEY", "fallback") == "fallback"


def test_whitespace_only_is_absent_not_a_blank_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key of only whitespace must read as missing, not as a key of spaces.

    provider_env() drops falsy values, so this is what keeps a whitespace-only
    secret from being forwarded as a present-but-unusable credential.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "   \n")
    assert credential("OPENROUTER_API_KEY") == ""


def test_harness_subprocess_env_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The opencode/aforge subprocess env — the seam that fed the harness 401."""
    monkeypatch.setenv("OPENROUTER_API_KEY", DIRTY)
    monkeypatch.setenv("GH_TOKEN", "ghs_token\n")

    env = AIIntegrationConfig.from_env().provider_env()

    assert env["OPENROUTER_API_KEY"] == CLEAN
    assert env["GH_TOKEN"] == "ghs_token"


def test_whitespace_only_key_is_not_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "  \n  ")
    assert "OPENROUTER_API_KEY" not in AIIntegrationConfig.from_env().provider_env()


def test_ai_seam_reads_the_key_through_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The .ai() client — the seam that produced the OpenrouterException 401.

    app.py builds the Agent at module scope, so the value cannot be re-read
    here. Assert instead that the module takes the trimming path at all: a
    passing trim test over a helper the module does not call would prove
    nothing about production.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", DIRTY)

    from pr_af import app as app_module

    source = inspect.getsource(app_module)
    assert 'api_key=credential("OPENROUTER_API_KEY")' in source
    assert 'os.getenv("OPENROUTER_API_KEY"' not in source
    assert app_module.credential("OPENROUTER_API_KEY") == CLEAN
