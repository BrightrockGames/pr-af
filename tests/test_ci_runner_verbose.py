"""``ci_runner.py --verbose`` tails the execution's structured events.

The polling loop only ever printed ``[X.Ym] Status: running`` every 30s, so a
GitHub Actions log showed a silent multi-minute gap through a 35-50 minute
review. Verbose mode subscribes to the control plane's server-sent execution
event stream (the same endpoint the AgentField SDK's optional event stream uses)
and prints each event for this execution as it arrives.

It must degrade quietly: a control plane without that stream leaves polling as
the only progress signal, rather than failing the run.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType


def _load_ci_runner() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "ci_runner.py"
    spec = importlib.util.spec_from_file_location("pr_af_ci_runner_verbose", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pr_af_ci_runner_verbose"] = module
    spec.loader.exec_module(module)
    return module


ci_runner = _load_ci_runner()

EXEC_ID = "exec_abc123"
SSE_BODY = (
    # Ours, plain progress.
    f'data: {{"execution_id": "{EXEC_ID}", "type": "reasoner.started", '
    '"reasoner": "meta_semantic"}\n\n'
    # A different execution — must not be printed.
    'data: {"execution_id": "exec_other", "type": "reasoner.started", '
    '"reasoner": "noise"}\n\n'
    # Multi-line data payload, which SSE allows.
    f'data: {{"execution_id": "{EXEC_ID}",\ndata:  "type": "reasoner.failed",\n'
    'data:  "error": "git clone failed: boom"}\n\n'
    # Unparsable, and an event with no data at all — both skipped.
    "data: not json\n\n"
    ": keep-alive comment\n\n"
)


class _SSEHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(SSE_BODY.encode())
        self.wfile.flush()

    def log_message(self, *args: object) -> None:
        pass  # keep pytest output clean


def _serve() -> tuple[http.server.HTTPServer, str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _SSEHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_streams_only_this_executions_events(capsys, monkeypatch) -> None:
    server, base = _serve()
    monkeypatch.setattr(ci_runner, "CP_URL", base)
    try:
        ci_runner.stream_events(EXEC_ID, threading.Event(), time.time())
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "meta_semantic" in out
    # A multi-line data payload must still be decoded, error included.
    assert "git clone failed: boom" in out
    # Another execution's events must not pollute this run's log.
    assert "noise" not in out
    assert "exec_other" not in out


def test_missing_stream_degrades_to_a_notice(capsys, monkeypatch) -> None:
    """No event stream must not fail the run — polling still reports status."""
    # Port 1 on loopback refuses connections.
    monkeypatch.setattr(ci_runner, "CP_URL", "http://127.0.0.1:1")
    ci_runner.stream_events(EXEC_ID, threading.Event(), time.time())
    out = capsys.readouterr().out
    assert "Event stream unavailable" in out
    assert "polling only" in out


def test_sse_payload_decoding() -> None:
    assert ci_runner._sse_payload(['{"a": 1}']) == {"a": 1}
    assert ci_runner._sse_payload(['{"a":', '1}']) == {"a": 1}
    assert ci_runner._sse_payload([]) is None
    assert ci_runner._sse_payload(["not json"]) is None
    # A bare JSON array is not an event object.
    assert ci_runner._sse_payload(["[1, 2]"]) is None


def test_verbose_polls_more_often_than_the_default() -> None:
    """A silent gap is the complaint; verbose mode must shorten it."""
    assert (
        ci_runner.VERBOSE_POLL_INTERVAL_SECONDS < ci_runner.POLL_INTERVAL_SECONDS
    )


def test_stop_event_ends_the_stream(monkeypatch) -> None:
    """The daemon thread must not keep reading after the run is done."""
    server, base = _serve()
    monkeypatch.setattr(ci_runner, "CP_URL", base)
    stop = threading.Event()
    stop.set()
    try:
        ci_runner.stream_events(EXEC_ID, stop, time.time())
    finally:
        server.shutdown()


def test_json_payload_shape_is_what_the_stream_sends() -> None:
    """Guards the fixture itself: each event body must be valid SSE JSON."""
    events = [block for block in SSE_BODY.split("\n\n") if block.strip()]
    decoded = 0
    for block in events:
        data = [
            line[5:].lstrip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if not data:
            continue
        try:
            json.loads("\n".join(data))
        except ValueError:
            continue
        decoded += 1
    assert decoded == 3  # two ours + one other execution
