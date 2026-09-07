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
    api_key = os.environ.get("AGENTFIELD_API_KEY", "")
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

    Best-effort: a control plane without the stream (or with it disabled) makes
    this print one notice and stop, leaving the polling loop as the only
    progress signal. Runs on a daemon thread so it can never hold up exit.
    """
    url = CP_URL + EVENT_STREAM_PATH
    req = urllib.request.Request(
        url, headers=_headers({"Accept": "text/event-stream"})
    )
    try:
        response = urllib.request.urlopen(req, timeout=30)
    except (urllib.error.URLError, OSError) as exc:
        print(
            f"[CI] Event stream unavailable ({url}: {exc}); progress will come from "
            "polling only"
        )
        return

    print(f"[CI] Tailing execution events from {url}")
    # SSE is line-oriented: `data:` lines accumulate until a blank line ends the
    # event. Iterating the response yields lines as they arrive, so this needs
    # no buffering of its own.
    data_lines = []
    try:
        with response:
            for raw in response:
                if stop_event.is_set():
                    break
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
                if (
                    payload.get("execution_id") or payload.get("executionId")
                ) != exec_id:
                    continue
                label = _event_label(payload)
                if label:
                    elapsed = (time.time() - start_time) / 60
                    print(f"[{elapsed:.1f}m] {label}", flush=True)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if not stop_event.is_set():
            print(f"[CI] Event stream ended: {exc}")


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


def main(argv=None):
    args = parse_args(argv)

    pr_url = os.environ.get("PR_URL")
    if not pr_url:
        print("Error: PR_URL environment variable is required.")
        sys.exit(1)

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
