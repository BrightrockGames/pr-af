"""The preflight must verify the key works, not merely that one was set.

A presence check cannot tell a live key from a revoked one, so a dead
credential still cost a full repo clone before surfacing as a provider 401.
This probes OpenRouter directly with the same auth the review will use.

Two details are pinned deliberately, because both produced wrong conclusions
during the investigation this replaces:

* **The Bearer prefix.** Omit it and OpenRouter answers 401 for *any* key,
  which is indistinguishable from a rejected credential. A diagnostic that
  dropped it sent us chasing a key that was in fact valid.
* **Only 401/403 are conclusive.** A timeout or a 500 says nothing about the
  key. The probe exists to catch a misconfiguration and must never become a
  new way for CI to fail.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ci_runner", Path(__file__).resolve().parents[1] / "scripts" / "ci_runner.py"
)
assert _SPEC and _SPEC.loader
ci_runner = importlib.util.module_from_spec(_SPEC)
sys.modules["ci_runner"] = ci_runner
_SPEC.loader.exec_module(ci_runner)

KEY = "sk-or-v1-" + "a" * 64


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _capture_request(monkeypatch: pytest.MonkeyPatch, status: int = 200) -> list:
    """Record the request the probe builds, and answer it with `status`."""
    seen: list = []

    def _urlopen(request, timeout=None):  # noqa: ANN001
        seen.append(request)
        return _Response(status)

    monkeypatch.setattr(ci_runner.urllib.request, "urlopen", _urlopen)
    return seen


def test_probe_sends_a_bearer_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without "Bearer ", OpenRouter 401s any key — the trap we fell into."""
    seen = _capture_request(monkeypatch)

    assert ci_runner.probe_llm_credential(KEY) is None

    header = seen[0].get_header("Authorization")
    assert header == "Bearer " + KEY
    assert header.startswith("Bearer ")


def test_probe_uses_the_documented_key_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture_request(monkeypatch)
    ci_runner.probe_llm_credential(KEY)
    assert seen[0].full_url == "https://openrouter.ai/api/v1/key"
    # A GET: introspection only, no model and no token spend.
    assert seen[0].get_method() == "GET"


def test_a_good_key_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_request(monkeypatch, status=200)
    assert ci_runner.probe_llm_credential(KEY) is None


@pytest.mark.parametrize("code", [401, 403])
def test_rejection_is_reported(code: int, monkeypatch: pytest.MonkeyPatch) -> None:
    def _urlopen(request, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            ci_runner.OPENROUTER_KEY_URL, code, "denied", {}, None
        )

    monkeypatch.setattr(ci_runner.urllib.request, "urlopen", _urlopen)

    failure = ci_runner.probe_llm_credential(KEY)
    assert failure is not None
    assert str(code) in failure


@pytest.mark.parametrize("code", [500, 502, 429])
def test_server_side_trouble_is_not_a_bad_key(
    code: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A provider outage must not fail the build with a credential error."""

    def _urlopen(request, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            ci_runner.OPENROUTER_KEY_URL, code, "oops", {}, None
        )

    monkeypatch.setattr(ci_runner.urllib.request, "urlopen", _urlopen)

    assert ci_runner.probe_llm_credential(KEY) is None
    assert "could not verify" in capsys.readouterr().out


def test_network_failure_is_not_a_bad_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _urlopen(request, timeout=None):  # noqa: ANN001
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(ci_runner.urllib.request, "urlopen", _urlopen)

    assert ci_runner.probe_llm_credential(KEY) is None
    assert "could not reach OpenRouter" in capsys.readouterr().out


def test_probe_is_bounded_by_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbounded probe would hang the job instead of shortening it."""
    seen: list = []

    def _urlopen(request, timeout=None):  # noqa: ANN001
        seen.append(timeout)
        return _Response(200)

    monkeypatch.setattr(ci_runner.urllib.request, "urlopen", _urlopen)
    ci_runner.probe_llm_credential(KEY)

    assert seen[0] == ci_runner.CREDENTIAL_PROBE_TIMEOUT_SECONDS
    assert 0 < seen[0] <= 60


def test_guard_rejects_a_dead_key_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: a 401 key must stop main() before any review is dispatched."""
    monkeypatch.setenv("PR_URL", "https://github.com/o/r/pull/1")
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.delenv("PR_AF_SKIP_CREDENTIAL_PROBE", raising=False)

    def _urlopen(request, timeout=None):  # noqa: ANN001
        if "openrouter.ai" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)
        raise AssertionError("dispatched a review despite a rejected key")

    monkeypatch.setattr(ci_runner.urllib.request, "urlopen", _urlopen)

    with pytest.raises(SystemExit) as excinfo:
        ci_runner.main([])

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "401" in out
    # The key itself must never be echoed into CI logs.
    assert KEY not in out


def test_probe_can_be_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An escape hatch, so the probe cannot become an unavoidable blocker."""
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("PR_AF_SKIP_CREDENTIAL_PROBE", "1")

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("probed despite PR_AF_SKIP_CREDENTIAL_PROBE=1")

    monkeypatch.setattr(ci_runner.urllib.request, "urlopen", _explode)

    ci_runner.require_llm_credential()  # must not raise
