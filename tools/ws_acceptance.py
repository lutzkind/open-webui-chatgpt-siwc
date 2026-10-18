#!/usr/bin/env python3
"""Bounded live acceptance for the SIWC adapter transport.

Drives the adapter's public HTTP interface (the same interface Open WebUI
uses) through the three progressive acceptance phases:

A. one trivial response,
B. same-connection continuation with ``previous_response_id`` and only new
   input items,
C. a tool call whose result is returned incrementally.

The tool never prints prompt text, tool results or credentials; it reports
event types, opaque IDs, byte counts and provider usage counters only.

Usage:
    python tools/ws_acceptance.py --base-url https://siwc.example.com/v1 \
        --api-key "$ADAPTER_API_KEY" [--model gpt-6.1-sol] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

TOOLS = [
    {
        "type": "function",
        "name": "get_test_results",
        "description": "Return a local demo test result. Always call this tool.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    }
]


def post_stream(base_url: str, api_key: str, body: dict, timeout: float = 300.0) -> dict:
    """Send one Responses request and summarize the SSE stream."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/responses",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = time.monotonic()
    summary: dict = {"events": [], "function_calls": [], "usage": None, "response_id": None, "status": None}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    continue
                event = json.loads(payload)
                event_type = event.get("type")
                if event_type not in summary["events"]:
                    summary["events"].append(event_type)
                if event_type == "response.output_item.done":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        summary["function_calls"].append(
                            {"name": item.get("name"), "call_id": item.get("call_id")}
                        )
                if event_type in {"response.completed", "response.failed", "response.incomplete"}:
                    response_object = event.get("response") or {}
                    summary["response_id"] = response_object.get("id")
                    summary["status"] = response_object.get("status")
                    summary["usage"] = response_object.get("usage")
                    error = response_object.get("error")
                    if isinstance(error, dict):
                        summary["error"] = {"type": error.get("type"), "code": error.get("code")}
                if event_type == "error":
                    detail = event.get("error") or {}
                    summary["error"] = {"type": detail.get("type"), "code": detail.get("code")}
    except urllib.error.HTTPError as exc:
        summary["http_status"] = exc.code
        try:
            body = json.loads(exc.read())
            summary["error"] = body.get("error") or {"detail": "rejected"}
        except Exception:  # noqa: BLE001 - diagnostic only
            summary["error"] = {"detail": "rejected"}
    summary["rpc_ms"] = round((time.monotonic() - started) * 1000, 1)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="adapter base URL ending in /v1")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--json", default=None, help="optional path for the JSON report")
    args = parser.parse_args()

    report: dict = {"model": args.model, "phases": {}}
    conversation = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Reply with the single word: ok"}]}]

    print("A. trivial response ...", flush=True)
    phase_a = post_stream(
        args.base_url,
        args.api_key,
        {"model": args.model, "store": False, "stream": True, "input": conversation,
         "reasoning": {"effort": "low"}},
    )
    report["phases"]["A"] = phase_a
    print("   ->", json.dumps(phase_a, separators=(",", ":")), flush=True)
    if phase_a.get("status") != "completed":
        report["verdict"] = "A_FAILED"
        return _finish(args, report, exit_code=1)

    print("B. same-conversation continuation (previous_response_id + new item only) ...", flush=True)
    continuation = [*conversation, {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Reply with the single word: two"}]}]
    phase_b = post_stream(
        args.base_url,
        args.api_key,
        {"model": args.model, "store": False, "stream": True, "input": continuation,
         "reasoning": {"effort": "low"}},
    )
    report["phases"]["B"] = phase_b
    print("   ->", json.dumps(phase_b, separators=(",", ":")), flush=True)
    if phase_b.get("status") != "completed":
        report["verdict"] = "B_FAILED"
        return _finish(args, report, exit_code=1)

    print("C. tool call + incremental tool result ...", flush=True)
    call_phase = post_stream(
        args.base_url,
        args.api_key,
        {
            "model": args.model,
            "store": False,
            "stream": True,
            "tools": TOOLS,
            "tool_choice": {"type": "function", "name": "get_test_results"},
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Call get_test_results now."}]}],
            "reasoning": {"effort": "low"},
        },
    )
    report["phases"]["C1"] = call_phase
    print("   ->", json.dumps(call_phase, separators=(",", ":")), flush=True)
    calls = call_phase.get("function_calls") or []
    if not calls:
        report["verdict"] = "C1_FAILED"
        return _finish(args, report, exit_code=1)

    tool_result = {
        "type": "function_call_output",
        "call_id": calls[0]["call_id"],
        "output": json.dumps({"test": "test_fizz_buzz", "failure": "Expected FizzBuzz, got Fizz."}),
    }
    phase_c = post_stream(
        args.base_url,
        args.api_key,
        {
            "model": args.model,
            "store": False,
            "stream": True,
            "tools": TOOLS,
            "tool_choice": "none",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Call get_test_results now."}]},
                {"type": "function_call", "call_id": calls[0]["call_id"], "name": calls[0]["name"], "arguments": "{}"},
                tool_result,
            ],
            "reasoning": {"effort": "low"},
        },
    )
    report["phases"]["C2"] = phase_c
    print("   ->", json.dumps(phase_c, separators=(",", ":")), flush=True)
    report["verdict"] = "ACCEPTED" if phase_c.get("status") == "completed" else "C2_FAILED"
    return _finish(args, report, exit_code=0 if report["verdict"] == "ACCEPTED" else 1)


def _finish(args, report: dict, exit_code: int) -> int:
    transport = report.get("transport")
    if transport is None:
        report["transport"] = "unknown (inspect adapter telemetry for the transport record)"
    print(json.dumps({"verdict": report["verdict"]}, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
