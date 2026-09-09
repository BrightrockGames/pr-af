"""The CI runner must refuse to dispatch a review with no LLM key.

``docker-compose.yml`` forwards ``OPENROUTER_API_KEY=${OPENROUTER_API_KEY}``
with no default, so a missing or misnamed CI secret arrives in the container
*set but empty* rather than absent — and nothing downstream objected. The node
built an AI client with an empty key, opencode resolved its ``{env:...}``
apiKey to nothing, and the run died at the first LLM call: several minutes
later, on the far side of a large repo clone, reported as a provider auth error
that reads like a wrong key rather than a missing one.

Checking on the host is what makes this cheap — compose interpolates from the
same environment, so no container has to be running yet.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ci_runner", Path(__file__).resolve().parents[1] / "scripts" / "ci_runner.py"
)
assert _SPEC and _SPEC.loader
ci_runner = importlib.util.module_from_spec(_SPEC)
sys.modules["ci_runner"] = ci_runner
_SPEC.loader.exec_module(ci_runner)


@pytest.mark.parametrize(
    "value",
    ["", " ", "\n", "\t\r\n   "],
    ids=["empty", "space", "newline", "mixed-whitespace"],
)
def test_blank_key_exits_nonzero(
    value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", value)

    with pytest.raises(SystemExit) as excinfo:
        ci_runner.require_llm_credential()

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "OPENROUTER_API_KEY" in out
    # The operator needs to know it was present-but-blank, not simply absent:
    # those have different causes (bad value vs. wrong secret name).
    assert "whitespace" in out


def test_unset_key_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        ci_runner.require_llm_credential()

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "is not set" in out
    # Distinguished from the blank case, which reports a whitespace length.
    assert "whitespace" not in out


def test_message_says_how_to_fix_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CI failure has no operator at a prompt, so the output must self-explain."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(SystemExit):
        ci_runner.require_llm_credential()

    out = capsys.readouterr().out
    assert "secrets.OPENROUTER_API_KEY" in out
    assert "clone" in out


def test_a_real_key_passes_and_is_never_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-realkey")

    ci_runner.require_llm_credential()  # must not raise

    assert "sk-or-v1-realkey" not in capsys.readouterr().out


def test_a_padded_key_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace around a real key is the node's job to trim, not a failure.

    config.credential strips it at every seam, so the preflight must not
    reject a key that will in fact authenticate.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "  sk-or-v1-realkey\n")
    ci_runner.require_llm_credential()  # must not raise


def test_main_refuses_before_dispatching(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard must run before any request is built, not merely exist.

    Any outbound call is made to blow up loudly, so a passing test here means
    main() really did stop at the credential check.
    """
    monkeypatch.setenv("PR_URL", "https://github.com/o/r/pull/1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("dispatched a review despite a missing LLM key")

    monkeypatch.setattr(ci_runner.urllib.request, "urlopen", _explode)

    with pytest.raises(SystemExit) as excinfo:
        ci_runner.main([])

    assert excinfo.value.code == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().out
