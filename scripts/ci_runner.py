#!/usr/bin/env python3
"""
CI Runner for PR-AF
Fires an async execution to the AgentField Control Plane and polls until completion.
Ensures GitHub Actions runners stay alive while the multi-agent DAG executes.

Deliberately stdlib-only: a CI job can run this straight from a checkout
without installing the package. That is why the review-cap table below is a
copy of ``pr_af.app.REVIEW_LIMIT_ENV_SPEC`` rather than an import —
``tests/test_review_limits.py`` asserts the two never drift.

Usage:
    PR_URL=https://github.com/owner/repo/pull/123 python3 scripts/ci_runner.py
    python3 scripts/ci_runner.py --verbose      # or PR_AF_CI_VERBOSE=1
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CP_URL = os.environ.get("AGENTFIELD_SERVER", "http://localhost:8080")
NODE_ID = os.environ.get("NODE_ID", "pr-af")

# Canonical per-execution record: carries status, result, error and
# error_details. This is the endpoint the AgentField SDK itself polls
# (async_execution_manager._execution_status_url), and the one that actually
# has an "error" field — the /api/ui/v1/.../details UI endpoint this script
# used to read does not reliably carry one, which is why every failure printed
# "Error details: None" while the real message sat in the event log.
STATUS_PATH = "/api/v1/executions/{exec_id}"
# Supplementary sources, tried only when the canonical record has no message.
# Different control-plane versions expose different subsets; each is optional.
FALLBACK_PATHS = (
    "/api/ui/v1/executions/{exec_id}/details",
    "/api/v1/executions/{exec_id}/events",
    "/api/v1/executions/{exec_id}/logs",
)
# Server-sent execution events, used by --verbose to tail progress. Global
# stream; each payload carries its own execution_id.
EVENT_STREAM_PATH = "/api/ui/v1/executions/events"
# urllib applies its `timeout` to socket READS as well as the connect, so a
# short value kills the event stream during any quiet stretch — a review spends
# minutes at a time silent (cloning a large repo, one long harness completion),
# which is exactly when progress output matters most. Generous enough to survive
# that, short enough to still notice a genuinely dead connection and reconnect.
EVENT_STREAM_READ_TIMEOUT_SECONDS = 600
EVENT_STREAM_RECONNECT_MAX_BACKOFF = 30

POLL_INTERVAL_SECONDS = 30
VERBOSE_POLL_INTERVAL_SECONDS = 10

TERMINAL_OK = ("succeeded", "success", "completed")
TERMINAL_BAD = ("failed", "error", "cancelled", "canceled", "timeout")

# (env var, review() input field, minimum accepted value). Mirrors
# pr_af.app.REVIEW_LIMIT_ENV_SPEC so a deployment's caps apply identically
# whether a review is triggered by webhook or by this script.
REVIEW_LIMIT_ENV_SPEC = (
    ("PR_AF_MAX_CONCURRENT_AGENTS", "max_concurrent_agents", 1),
    ("PR_AF_MAX_CONCURRENT_REVIEWERS", "max_concurrent_reviewers", 1),
    ("PR_AF_MAX_REVIEW_DEPTH", "max_review_depth", 0),
    ("PR_AF_MAX_COVERAGE_ITERATIONS", "max_coverage_iterations", 1),
)


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _headers(extra=None):
    """Request headers, including the CP API key when one is configured."""
    headers = dict(extra or {})
    # Trimmed: a secret stored with a trailing newline yields a header the
    # control plane cannot match (see config.credential).
    api_key = os.environ.get("AGENTFIELD_API_KEY", "").strip()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _get_json(url, timeout=30):
    """GET and parse JSON. Returns None on any transport/parse failure."""
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def resolve_review_limits(environ=None):
    """Per-deployment review caps from the environment, as review() inputs.

    Same table and same validation as the webhook path, so the caps documented
    in .env.example apply to a CI-triggered review too. Previously these fields
    were left null here, and a host tuned to survive `PR_AF_MAX_REVIEW_DEPTH=0`
    would silently get the full-depth default from CI.
    """
    environ = os.environ if environ is None else environ
    limits = {}
    for env_name, input_key, minimum in REVIEW_LIMIT_ENV_SPEC:
        raw = environ.get(env_name)
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            value = minimum - 1
        if value < minimum:
            print(
                f"[CI] Ignoring invalid {env_name}={raw!r} (must be an integer >= {minimum})"
            )
            continue
        limits[input_key] = value
    return limits


def extract_error(payload, _depth=0):
    """First non-empty error message anywhere in an execution payload.

    The message can sit at several depths depending on the endpoint and the
    control-plane version: a top-level "error" on the canonical record, an
    "error"/"message" on a `reasoner.failed` event's attributes, or a nested
    detail object. Rather than guessing one shape, walk the structure and take
    the first plausible message — the concrete case this was written for is
    `{"error": "git clone failed: ..."}` inside the event log.
    """
    if _depth > 8:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        # Prefer explicit error carriers at this level, in order of specificity.
        for key in ("error", "error_message", "error_details", "failure_reason"):
            if key in payload:
                found = extract_error(payload[key], _depth + 1)
                if found:
                    return found
        # A `*.failed` event carries its reason under attributes/detail/message.
        for key in ("attributes", "detail", "details", "message"):
            if key in payload:
                found = extract_error(payload[key], _depth + 1)
                if found:
                    return found
        for key in ("events", "logs", "steps", "result", "data", "items"):
            if key in payload:
                found = extract_error(payload[key], _depth + 1)
                if found:
                    return found
        return ""
    if isinstance(payload, list):
        # Last first: the failure is normally the final event in a log.
        for item in reversed(payload):
            found = extract_error(item, _depth + 1)
            if found:
                return found
    return ""


def terminal_error(exec_id, status_payload):
    """The real failure reason for a terminal execution, or a clear fallback.

    Reads the canonical status record first, then the supplementary endpoints,
    so a control plane that only records the message on the event log still
    produces something actionable in the CI log.
    """
    message = extract_error(status_payload)
    if message:
        return message
    for path in FALLBACK_PATHS:
        payload = _get_json(CP_URL + path.format(exec_id=exec_id))
        if payload is None:
            continue
        message = extract_error(payload)
        if message:
            return message
    return (
        "no error message recorded by the control plane; run "
        "`docker compose logs pr-af` for the node-side traceback"
    )


def _event_label(payload):
    """One-line description of an execution event, or None to skip it."""
    kind = payload.get("type") or payload.get("event") or payload.get("name") or ""
    status = payload.get("status") or ""
    parts = [str(p) for p in (kind, status) if p]
    detail = (
        payload.get("reasoner")
        or payload.get("phase")
        or payload.get("node_id")
        or payload.get("message")
        or ""
    )
    if detail:
        parts.append(str(detail))
    label = " ".join(parts).strip()
    if not label:
        return None
    error = extract_error(payload)
    if error and error not in label:
        label = f"{label} — {error}"
    return label[:500]


def stream_events(exec_id, stop_event, start_time):
    """Tail the CP's execution event stream, printing this execution's events.

    Reconnects for as long as the review runs. A stream that has connected once
    but then drops (idle read timeout, a proxy closing an idle connection, the
    control plane restarting) must not silently end the only progress output for
    the rest of a 35-50 minute review — which is what happened before, because a
    30s socket timeout also applied to reads and the stream is idle for minutes
    at a time.

    Best-effort: if the FIRST connect fails the endpoint is presumed absent, so
    this prints one notice and gives up, leaving the polling loop as the progress
    signal. Runs on a daemon thread so it can never hold up exit.
    """
    url = CP_URL + EVENT_STREAM_PATH
    connected_once = False
    announced_reconnect = False
    backoff = 2

    while not stop_event.is_set():
        req = urllib.request.Request(
            url, headers=_headers({"Accept": "text/event-stream"})
        )
        try:
            response = urllib.request.urlopen(
                req, timeout=EVENT_STREAM_READ_TIMEOUT_SECONDS
            )
        except (urllib.error.URLError, OSError) as exc:
            if not connected_once:
                print(
                    f"[CI] Event stream unavailable ({url}: {exc}); progress will "
                    "come from polling only"
                )
                return
            if stop_event.wait(backoff):
                return
            backoff = min(backoff * 2, EVENT_STREAM_RECONNECT_MAX_BACKOFF)
            continue

        if not connected_once:
            print(f"[CI] Tailing execution events from {url}")
            connected_once = True
        backoff = 2

        try:
            _consume_event_stream(response, exec_id, stop_event, start_time)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if stop_event.is_set():
                return
            if not announced_reconnect:
                # Say it once: a review can drop and reconnect many times, and a
                # line per reconnect would bury the events this exists to show.
                print(f"[CI] Event stream dropped ({exc}); reconnecting as needed")
                announced_reconnect = True
        if stop_event.wait(1):
            return


def _consume_event_stream(response, exec_id, stop_event, start_time):
    """Print this execution's events from one SSE response until it ends.

    SSE is line-oriented: `data:` lines accumulate until a blank line ends the
    event. Iterating the response yields lines as they arrive, so this needs no
    buffering of its own.
    """
    data_lines = []
    with response:
        for raw in response:
            if stop_event.is_set():
                return
            line = raw.decode("utf-8", errors="ignore").rstrip("\r\n")
            if line.startswith("data:"):
                if len(data_lines) < 64:  # bound a pathological event
                    data_lines.append(line[5:].lstrip())
                continue
            if line:
                continue  # id:/event:/retry:/comment — not needed here
            payload, data_lines = _sse_payload(data_lines), []
            if payload is None:
                continue
            if (payload.get("execution_id") or payload.get("executionId")) != exec_id:
                continue
            label = _event_label(payload)
            if label:
                elapsed = (time.time() - start_time) / 60
                print(f"[{elapsed:.1f}m] {label}", flush=True)


def _sse_payload(data_lines):
    """Decode one SSE event's accumulated `data:` lines, or None if unusable."""
    if not data_lines:
        return None
    try:
        payload = json.loads("\n".join(data_lines).strip())
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Fire a PR-AF review at the AgentField control plane and "
        "poll until it finishes."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=_env_flag("PR_AF_CI_VERBOSE"),
        help="Tail the execution's structured events as they arrive and poll "
        "more often, instead of only printing a coarse status line every "
        "30s. Also enabled by PR_AF_CI_VERBOSE=1.",
    )
    parser.add_argument(
        "--depth",
        default=os.environ.get("PR_AF_CI_DEPTH", "standard"),
        help="Review depth to request (default: standard).",
    )
    return parser.parse_args(argv)


def require_llm_credential():
    """Fail before dispatching when the LLM key is missing or blank.

    docker-compose passes OPENROUTER_API_KEY through with no default, so a
    missing or misnamed CI secret reaches the container as set-but-empty
    rather than absent. Nothing downstream validates it: the node builds an
    AI client with an empty key and opencode resolves its {env:...} apiKey to
    nothing, so both LLM seams fail at call time — several minutes later, on
    the far side of a large repo clone, as a provider auth error that reads
    like a bad key rather than a missing one.

    Checked on the host because that is what compose interpolates from, so
    this needs no running container and costs nothing. Only the length is
    ever reported; the value is never printed.
    """
    raw = os.environ.get("OPENROUTER_API_KEY")
    if raw is None:
        print("Error: OPENROUTER_API_KEY is not set.")
    elif not raw.strip():
        print(
            "Error: OPENROUTER_API_KEY is set but {} character(s) of "
            "whitespace.".format(len(raw))
        )
    else:
        return
    print("  The review needs an LLM key. In GitHub Actions, confirm the workflow maps it:")
    print("    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}")
    print(
        "  and that a secret of that exact name exists on this repository "
        "(or its org)."
    )
    print(
        "  Failing now rather than after the repo clone, which is where an "
        "empty key would otherwise surface as a provider 401."
    )
    sys.exit(1)


def main(argv=None):
    args = parse_args(argv)

    pr_url = os.environ.get("PR_URL")
    if not pr_url:
        print("Error: PR_URL environment variable is required.")
        sys.exit(1)

    require_llm_credential()

    print(f"[CI] Initiating PR-AF Review for: {pr_url}")

    # 1. Fire the execution
    review_input = {
        "pr_url": pr_url,
        "depth": args.depth,
        "dry_run": False,
    }
    limits = resolve_review_limits()
    if limits:
        review_input.update(limits)
        print(
            "[CI] Applying deployment review caps: {}".format(
                ", ".join(f"{k}={v}" for k, v in sorted(limits.items()))
            )
        )
    payload = json.dumps({"input": review_input}).encode("utf-8")

    req = urllib.request.Request(
        f"{CP_URL}/api/v1/execute/async/{NODE_ID}.review",
        data=payload,
        headers=_headers({"Content-Type": "application/json"}),
    )

    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            exec_id = res_data.get("execution_id")
            if not exec_id:
                print("Error: Failed to get execution_id")
                sys.exit(1)
            print(f"[CI] Review dispatched. Execution ID: {exec_id}")
    except urllib.error.URLError as e:
        print(f"Error triggering review: {e}")
        sys.exit(1)

    # 2. Poll for completion
    print("[CI] Polling for completion (this may take 30-60 minutes)...")
    start_time = time.time()
    interval = POLL_INTERVAL_SECONDS
    stop_event = threading.Event()

    if args.verbose:
        interval = VERBOSE_POLL_INTERVAL_SECONDS
        threading.Thread(
            target=stream_events,
            args=(exec_id, stop_event, start_time),
            daemon=True,
        ).start()

    status_url = CP_URL + STATUS_PATH.format(
        exec_id=urllib.parse.quote(str(exec_id), safe="")
    )
    try:
        while True:
            time.sleep(interval)
            elapsed_min = (time.time() - start_time) / 60

            status_data = _get_json(status_url)
            if status_data is None:
                print(
                    f"[{elapsed_min:.1f}m] Warning: Could not reach Control Plane API "
                    f"({status_url})"
                )
                continue

            status = str(status_data.get("status") or "").lower()
            print("[{:.1f}m] Status: {}".format(elapsed_min, status or "unknown"))

            if status in TERMINAL_OK:
                print("\n[CI] Review completed successfully!")
                return 0
            if status in TERMINAL_BAD:
                print(f"\n[CI] Review ended with status: {status}")
                print(f"Error details: {terminal_error(exec_id, status_data)}")
                return 1
    finally:
        stop_event.set()


if __name__ == "__main__":
    sys.exit(main())
