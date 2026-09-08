"""``ci_runner.py --verbose`` tails the execution's structured events.

The polling loop only ever printed ``[X.Ym] Status: running`` every 30s, so a
GitHub Actions log showed a silent multi-minute gap through a 35-50 minute
review. Verbose mode subscribes to the control plane's server-sent execution
event stream (the same endpoint the AgentField SDK's optional event stream uses)
and prints each event for this execution as it arrives.

Two behaviours are pinned here beyond the parsing:

* it must **reconnect**. A real run showed ``Event stream ended: timed out``
  after 3.3 minutes and then printed no events at all for the rest of the
  review: urllib applies its ``timeout`` to socket reads too, and the stream is
  idle for minutes at a time (a large clone, one long harness completion).
* it must degrade quietly when the endpoint is absent, rather than failing the
  run.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
import time
import urllib.request
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
    requests_served = 0

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).requests_served += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(SSE_BODY.encode())
        self.wfile.flush()

    def log_message(self, *args: object) -> None:
        pass  # keep pytest output clean


def _serve() -> tuple[http.server.HTTPServer, str]:
    _SSEHandler.requests_served = 0
    server = http.server.HTTPServer(("127.0.0.1", 0), _SSEHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_streams_only_this_executions_events(capsys) -> None:
    """One response's worth of events: parsed, filtered, and labelled."""
    server, base = _serve()
    try:
        response = urllib.request.urlopen(base + ci_runner.EVENT_STREAM_PATH, timeout=10)
        ci_runner._consume_event_stream(
            response, EXEC_ID, threading.Event(), time.time()
        )
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "meta_semantic" in out
    # A multi-line data payload must still be decoded, error included.
    assert "git clone failed: boom" in out
    # Another execution's events must not pollute this run's log.
    assert "noise" not in out
    assert "exec_other" not in out


def test_consume_stops_on_stop_event(capsys) -> None:
    """A set stop event must end consumption without printing."""
    server, base = _serve()
    stop = threading.Event()
    stop.set()
    try:
        response = urllib.request.urlopen(base + ci_runner.EVENT_STREAM_PATH, timeout=10)
        ci_runner._consume_event_stream(response, EXEC_ID, stop, time.time())
    finally:
        server.shutdown()
    assert "meta_semantic" not in capsys.readouterr().out


def test_missing_stream_degrades_to_a_notice(capsys, monkeypatch) -> None:
    """No event stream must not fail the run — polling still reports status."""
    # Port 1 on loopback refuses connections.
    monkeypatch.setattr(ci_runner, "CP_URL", "http://127.0.0.1:1")
    ci_runner.stream_events(EXEC_ID, threading.Event(), time.time())
    out = capsys.readouterr().out
    assert "Event stream unavailable" in out
    assert "polling only" in out


def test_reconnects_after_the_stream_drops(capsys, monkeypatch) -> None:
    """Regression: one dropped stream must not end progress output for the run.

    The handler closes the body after each event batch, so a run that only
    connected once would serve exactly one request and go silent.
    """
    server, base = _serve()
    monkeypatch.setattr(ci_runner, "CP_URL", base)
    # No reconnect delay — the point here is that it reconnects at all.
    monkeypatch.setattr(ci_runner, "EVENT_STREAM_RECONNECT_MAX_BACKOFF", 0)
    stop = threading.Event()
    worker = threading.Thread(
        target=ci_runner.stream_events,
        args=(EXEC_ID, stop, time.time()),
        daemon=True,
    )
    worker.start()
    try:
        deadline = time.time() + 20
        while _SSEHandler.requests_served < 2 and time.time() < deadline:
            time.sleep(0.05)
        served = _SSEHandler.requests_served
    finally:
        stop.set()
        worker.join(timeout=10)
        server.shutdown()

    assert served >= 2, f"stream did not reconnect (requests served: {served})"
    assert not worker.is_alive(), "stop_event did not end the stream thread"
    # The reconnect notice appears at most once, however many drops occur.
    assert capsys.readouterr().out.count("Event stream dropped") <= 1


def test_read_timeout_is_long_enough_for_a_quiet_review() -> None:
    """A review is idle for minutes at a time; 30s killed the stream."""
    assert ci_runner.EVENT_STREAM_READ_TIMEOUT_SECONDS >= 300


def test_sse_payload_decoding() -> None:
    assert ci_runner._sse_payload(['{"a": 1}']) == {"a": 1}
    assert ci_runner._sse_payload(['{"a":', "1}"]) == {"a": 1}
    assert ci_runner._sse_payload([]) is None
    assert ci_runner._sse_payload(["not json"]) is None
    # A bare JSON array is not an event object.
    assert ci_runner._sse_payload(["[1, 2]"]) is None


def test_verbose_polls_more_often_than_the_default() -> None:
    """A silent gap is the complaint; verbose mode must shorten it."""
    assert ci_runner.VERBOSE_POLL_INTERVAL_SECONDS < ci_runner.POLL_INTERVAL_SECONDS


def test_json_payload_shape_is_what_the_stream_sends() -> None:
    """Guards the fixture itself: each event body must be valid SSE JSON."""
    events = [block for block in SSE_BODY.split("\n\n") if block.strip()]
    decoded = 0
    for block in events:
        data = [
            line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
        ]
        if not data:
            continue
        try:
            json.loads("\n".join(data))
        except ValueError:
            continue
        decoded += 1
    assert decoded == 3  # two ours + one other execution
