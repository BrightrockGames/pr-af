"""The ``.ai()`` model must route to the endpoint its credentials belong to.

app.py hardwires the ``.ai()`` seam to OpenRouter — ``OPENROUTER_API_KEY`` plus
``api_base=https://openrouter.ai/api/v1`` — but LiteLLM picks the provider from
the model string's own prefix, and the provider's base URL wins over
``api_base``. With the repo's ``PR_AF_MODEL=deepseek/deepseek-v4-flash-0731``
that meant the OpenRouter key was posted to ``api.deepseek.com``, which answered

    401 {"message": "User not found.", "code": 401}

surfaced by LiteLLM as ``DeepseekException`` — so a real run looked like a
DeepSeek credential problem when nothing was meant to reach DeepSeek at all.

These tests assert against **LiteLLM's own resolver**, so they pin the actual
routing rather than a restatement of the normaliser. They are offline:
``get_llm_provider`` is pure string/registry work and makes no network call.

The Go node needs no equivalent — its SDK posts the model verbatim, so
``node.go``'s ``aiModelForAPI`` strips the prefix and either form works there.
"""

from __future__ import annotations

import pytest

from pr_af.config import OPENROUTER_ROUTING_PREFIX, openrouter_ai_model

litellm = pytest.importorskip(
    "litellm", reason="litellm ships with the agentfield SDK; skip if absent"
)

# The value the repo defaults to, and the one the failing production run used.
BARE_SLUG = "deepseek/deepseek-v4-flash-0731"


def _resolve(model: str) -> tuple[str, str]:
    """(provider, model actually sent upstream) per LiteLLM's own resolver."""
    sent, provider, _key, _api_base = litellm.get_llm_provider(model=model)
    return provider, sent


def test_bare_slug_would_route_to_the_wrong_provider() -> None:
    """Documents the bug: this is why the OpenRouter key hit api.deepseek.com."""
    provider, _sent = _resolve(BARE_SLUG)
    assert provider == "deepseek", (
        f"expected LiteLLM to read {BARE_SLUG!r} as provider 'deepseek' — if this "
        f"changed upstream, the normaliser's rationale needs revisiting"
    )


def test_normalised_model_routes_to_openrouter() -> None:
    """The fix: prefixed, LiteLLM routes to OpenRouter and sends the real slug."""
    provider, sent = _resolve(openrouter_ai_model(BARE_SLUG))
    assert provider == "openrouter"
    # LiteLLM consumes the routing prefix, leaving the OpenRouter slug intact.
    assert sent == BARE_SLUG


def test_already_prefixed_is_left_alone() -> None:
    prefixed = OPENROUTER_ROUTING_PREFIX + BARE_SLUG
    assert openrouter_ai_model(prefixed) == prefixed
    provider, sent = _resolve(prefixed)
    assert provider == "openrouter"
    assert sent == BARE_SLUG


def test_no_double_prefix() -> None:
    once = openrouter_ai_model(BARE_SLUG)
    assert openrouter_ai_model(once) == once
    assert once.count(OPENROUTER_ROUTING_PREFIX) == 1


@pytest.mark.parametrize("model", ["", "   "])
def test_empty_model_is_not_turned_into_a_bare_prefix(model: str) -> None:
    """An unset model must not become the meaningless "openrouter/"."""
    assert openrouter_ai_model(model) == ""


def test_whitespace_is_trimmed() -> None:
    assert openrouter_ai_model(f"  {BARE_SLUG}  ") == (
        OPENROUTER_ROUTING_PREFIX + BARE_SLUG
    )


def test_the_app_configures_the_normalised_model() -> None:
    """Guards the wiring, not just the helper.

    Importing app.py builds the Agent at module scope, so this asserts what the
    node actually hands the SDK.
    """
    from pr_af import app as app_module

    configured = app_module.app.ai_config.model
    assert configured.startswith(OPENROUTER_ROUTING_PREFIX), (
        f"the .ai() model is {configured!r}; without the routing prefix LiteLLM "
        f"sends OPENROUTER_API_KEY to whichever provider the slug names"
    )
    provider, _sent = _resolve(configured)
    assert provider == "openrouter"
