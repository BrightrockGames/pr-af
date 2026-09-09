"""The harness CLI must be invoked with an OpenRouter-qualified model.

The SDK hands ``HarnessConfig.model`` to ``opencode run`` as ``-m`` verbatim
(``resolve_model_and_variant`` only strips a ``#variant`` suffix), and opencode
resolves ``-m`` as ``"<provider>/<key>"`` against the providers its config
declares. ``docker-entrypoint.sh`` declares exactly one — ``openrouter`` — as
that is the only credential the image holds.

So the repo default ``deepseek/deepseek-v4-flash-0731`` named the *undeclared*
provider ``deepseek``. opencode exited 1 in about two seconds having written no
output file, and the SDK surfaced only::

    Schema retry 1 provider error: Process exited with code 1 and produced no output.

An earlier fix normalised the generated config file's ``model`` field, which
opencode ignores whenever ``-m`` is present — so the value it actually uses
stayed wrong and the symptom did not change. These tests therefore assert the
string handed to the SDK, which is what becomes ``-m``.
"""

from __future__ import annotations

import pytest

from pr_af.config import (
    OPENROUTER_ROUTING_PREFIX,
    AIIntegrationConfig,
    openrouter_harness_model,
)

BARE = "deepseek/deepseek-v4-flash-0731"
QUALIFIED = OPENROUTER_ROUTING_PREFIX + BARE


@pytest.mark.parametrize("provider", ["opencode", "aforge"])
def test_bare_slug_is_qualified(provider: str) -> None:
    """The bug: a bare slug names a provider opencode has never heard of."""
    assert openrouter_harness_model(BARE, provider) == QUALIFIED


@pytest.mark.parametrize("provider", ["opencode", "aforge"])
def test_already_qualified_is_left_alone(provider: str) -> None:
    assert openrouter_harness_model(QUALIFIED, provider) == QUALIFIED


@pytest.mark.parametrize("provider", ["OpenCode", "AFORGE", " opencode "])
def test_provider_match_is_case_and_space_insensitive(provider: str) -> None:
    """PR_AF_PROVIDER is operator input; casing must not silently skip the fix."""
    assert openrouter_harness_model(BARE, provider) == QUALIFIED


@pytest.mark.parametrize("provider", ["codex", "gemini", "grok", "claude"])
def test_other_providers_are_untouched(provider: str) -> None:
    """Prefixing a non-OpenRouter harness would break a working setup.

    Only opencode (whose config declares openrouter) and aforge (whose provider
    strips the prefix) may be normalised.
    """
    assert openrouter_harness_model("gpt-5", provider) == "gpt-5"
    assert openrouter_harness_model(BARE, provider) == BARE


def test_no_double_prefix() -> None:
    once = openrouter_harness_model(BARE, "opencode")
    assert openrouter_harness_model(once, "opencode") == once
    assert once.count(OPENROUTER_ROUTING_PREFIX) == 1


@pytest.mark.parametrize("model", ["", "   "])
def test_empty_model_does_not_become_a_bare_prefix(model: str) -> None:
    """An unset model must not turn into the meaningless "openrouter/"."""
    assert openrouter_harness_model(model, "opencode") == ""


def test_whitespace_is_trimmed() -> None:
    assert openrouter_harness_model(f"  {BARE}  ", "opencode") == QUALIFIED


def test_the_app_hands_the_sdk_a_qualified_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the wiring, not just the helper.

    A correct helper that app.py does not call would leave production broken
    in exactly the way it already was, so assert the configured value.
    """
    from pr_af import app as app_module

    configured = app_module.app.harness_config.model
    provider = app_module.app.harness_config.provider

    if provider in ("opencode", "aforge"):
        assert configured.startswith(OPENROUTER_ROUTING_PREFIX), (
            f"harness model is {configured!r}; opencode would read the leading "
            f"segment as a provider name its config never declares"
        )
    # Whatever the provider, the model must never carry the prefix twice.
    assert configured.count(OPENROUTER_ROUTING_PREFIX) <= 1


def test_default_config_produces_a_resolvable_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repo default, through the real config object, must come out qualified."""
    monkeypatch.setenv("PR_AF_MODEL", BARE)
    monkeypatch.setenv("PR_AF_PROVIDER", "opencode")
    monkeypatch.delenv("PR_AF_AI_MODEL", raising=False)

    cfg = AIIntegrationConfig.from_env()

    assert openrouter_harness_model(cfg.harness_model, cfg.provider) == QUALIFIED


def test_the_generated_config_and_the_flag_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """-m and the config file's model must name the same thing.

    They are derived independently — the entrypoint normalises the file, the
    node normalises the flag — so a divergence would be invisible until
    opencode resolved one of them against the wrong provider.
    """
    monkeypatch.setenv("PR_AF_MODEL", BARE)
    monkeypatch.setenv("PR_AF_PROVIDER", "opencode")
    cfg = AIIntegrationConfig.from_env()

    flag_value = openrouter_harness_model(cfg.harness_model, cfg.provider)

    # The entrypoint's normalisation, restated: prefix unless already present.
    entrypoint_value = (
        BARE if BARE.startswith(OPENROUTER_ROUTING_PREFIX)
        else OPENROUTER_ROUTING_PREFIX + BARE
    )
    assert flag_value == entrypoint_value
    # And the provider key the config file declares is the flag minus the prefix.
    assert flag_value.removeprefix(OPENROUTER_ROUTING_PREFIX) == BARE
