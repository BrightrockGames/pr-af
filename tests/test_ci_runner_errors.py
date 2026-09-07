"""``ci_runner.py`` surfaces the real failure reason.

The script printed ``Error details: None`` on every failure. Two causes:

* it polled ``/api/ui/v1/executions/<id>/details``, a UI endpoint that does not
  reliably carry an ``error`` field. The canonical record the AgentField SDK
  itself polls — ``/api/v1/executions/<id>`` — carries ``error`` /
  ``error_details``, so that is what the script now reads.
* it read only a *top-level* ``error``. The message frequently sits deeper: on a
  ``reasoner.failed`` event's attributes inside the execution's event log, which
  is where ``{"error": "git clone failed: ..."}`` was found by hand.

Every failure previously required dumping full container logs to find out what
happened; these tests pin the extraction.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_ci_runner() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "ci_runner.py"
    spec = importlib.util.spec_from_file_location("pr_af_ci_runner_errors", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pr_af_ci_runner_errors"] = module
    spec.loader.exec_module(module)
    return module


ci_runner = _load_ci_runner()
GIT_CLONE_ERROR = (
    "git clone failed: remote: Invalid username or token. Password "
    "authentication is not supported for Git operations."
)


def test_polls_the_canonical_status_endpoint() -> None:
    """The UI /details endpoint is what produced 'Error details: None'."""
    assert ci_runner.STATUS_PATH == "/api/v1/executions/{exec_id}"


def test_top_level_error() -> None:
    assert (
        ci_runner.extract_error({"status": "failed", "error": GIT_CLONE_ERROR})
        == GIT_CLONE_ERROR
    )


def test_error_details_field() -> None:
    assert (
        ci_runner.extract_error({"status": "failed", "error_details": GIT_CLONE_ERROR})
        == GIT_CLONE_ERROR
    )


def test_error_nested_in_a_detail_object() -> None:
    """The node raises HTTPException(detail={"error": ...})."""
    payload = {"status": "failed", "error": {"detail": {"error": GIT_CLONE_ERROR}}}
    assert ci_runner.extract_error(payload) == GIT_CLONE_ERROR


def test_error_on_a_reasoner_failed_event() -> None:
    """The real-world shape: the message lives on the event log, not the status."""
    payload = {
        "status": "failed",
        "events": [
            {"type": "reasoner.started", "attributes": {"reasoner": "review"}},
            {
                "type": "reasoner.failed",
                "attributes": {"reasoner": "review", "error": GIT_CLONE_ERROR},
            },
        ],
    }
    assert ci_runner.extract_error(payload) == GIT_CLONE_ERROR


def test_last_failure_in_a_log_wins() -> None:
    """A log with several errors should report the one that ended the run."""
    payload = {
        "logs": [
            {"error": "transient fetch warning"},
            {"error": GIT_CLONE_ERROR},
        ]
    }
    assert ci_runner.extract_error(payload) == GIT_CLONE_ERROR


def test_no_error_anywhere_is_empty() -> None:
    assert ci_runner.extract_error({"status": "failed"}) == ""
    assert ci_runner.extract_error({"status": "failed", "error": None}) == ""
    assert ci_runner.extract_error({"status": "failed", "error": ""}) == ""


def test_cycle_safe_depth_bound() -> None:
    """Deeply nested payloads must terminate rather than recurse forever."""
    payload: dict[str, object] = {"error": "deep"}
    for _ in range(50):
        payload = {"error": payload}
    assert ci_runner.extract_error(payload) == ""


def test_fallback_message_is_actionable(monkeypatch) -> None:
    """With no message anywhere, say what to do — not 'None'."""
    monkeypatch.setattr(ci_runner, "_get_json", lambda *a, **k: None)
    message = ci_runner.terminal_error("exec_1", {"status": "failed"})
    assert "docker compose logs" in message
    assert message != "None"


def test_falls_back_to_supplementary_endpoints(monkeypatch) -> None:
    """A CP that records the reason only on its event log must still report it."""
    calls: list[str] = []

    def fake_get_json(url: str, timeout: int = 30) -> object:
        calls.append(url)
        if url.endswith("/events"):
            return {"events": [{"type": "reasoner.failed", "error": GIT_CLONE_ERROR}]}
        return None

    monkeypatch.setattr(ci_runner, "_get_json", fake_get_json)
    assert ci_runner.terminal_error("exec_1", {"status": "failed"}) == GIT_CLONE_ERROR
    assert any(url.endswith("/events") for url in calls)
