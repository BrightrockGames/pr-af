#!/usr/bin/env python3
"""
CI Runner for PR-AF
Fires an async execution to the AgentField Control Plane and polls until completion.
Ensures GitHub Actions runners stay alive while the multi-agent DAG executes.

Deliberately stdlib-only: a CI job can run this straight from a checkout
without installing the package.

Usage:
    PR_URL=https://github.com/owner/repo/pull/123 python3 scripts/ci_runner.py
"""

import json
import os
import sys
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

POLL_INTERVAL_SECONDS = 30

TERMINAL_OK = ("succeeded", "success", "completed")
TERMINAL_BAD = ("failed", "error", "cancelled", "canceled", "timeout")


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


def main():
    pr_url = os.environ.get("PR_URL")
    if not pr_url:
        print("Error: PR_URL environment variable is required.")
        sys.exit(1)

    print(f"[CI] Initiating PR-AF Review for: {pr_url}")

    # 1. Fire the execution
    payload = json.dumps(
        {"input": {"pr_url": pr_url, "depth": "standard", "dry_run": False}}
    ).encode("utf-8")

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
    status_url = CP_URL + STATUS_PATH.format(
        exec_id=urllib.parse.quote(str(exec_id), safe="")
    )

    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed_min = (time.time() - start_time) / 60

        status_data = _get_json(status_url)
        if status_data is None:
            print(
                f"[{elapsed_min:.1f}m] Warning: Could not reach Control Plane API "
                f"({status_url})"
            )
            continue

        status = str(status_data.get("status") or "").lower()
        print(f"[{elapsed_min:.1f}m] Status: {status or 'unknown'}")

        if status in TERMINAL_OK:
            print("\n[CI] Review completed successfully!")
            return 0
        if status in TERMINAL_BAD:
            print(f"\n[CI] Review ended with status: {status}")
            print(f"Error details: {terminal_error(exec_id, status_data)}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
