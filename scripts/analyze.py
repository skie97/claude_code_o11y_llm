#!/usr/bin/env python3
"""
analyze.py -- exploratory analysis of the downloaded Claude Code log dump.

Reads the NDJSON produced by download_loki_data.sh, prints a human-readable
report, and writes two machine-readable artifacts:

  data/processed/prompt_response_pairs.json  -- candidate input for the scorer
  data/processed/summary.json                -- the report as data

The goal is to establish, from real data, how the eventual LLM-as-Judge scorer
should query and pair events -- the handover brief's assumptions about field
names and event types are wrong, so we go by what's actually here.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "data" / "raw" / "claude_code_logs.ndjson"
PROCESSED = REPO / "data" / "processed"

# Events whose `body` field carries the full request/response JSON.
REQUEST_BODY = "api_request_body"
RESPONSE_BODY = "api_response_body"
METRICS_EVENT = "api_request"        # tokens / cost / duration, no body
PROMPT_EVENT = "user_prompt"
REDACTION = "<REDACTED>"


def load_records(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def ns_to_iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


def is_real_prompt(record: dict) -> bool:
    """A developer's actual instruction -- not a slash command like /exit."""
    if record.get("command_name"):
        return False
    text = (record.get("prompt") or "").strip()
    return bool(text) and not text.startswith("/")


def assistant_text(body_json: str) -> str:
    """Concatenate assistant `text` blocks from a response body, dropping
    redacted thinking and tool_use noise."""
    try:
        content = json.loads(body_json).get("content", [])
    except (json.JSONDecodeError, AttributeError):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p and REDACTION not in p).strip()


def to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def inventory(records: list[dict]) -> dict:
    timestamps = [to_int(r.get("ts")) for r in records if r.get("ts")]
    return {
        "total_entries": len(records),
        "event_name_counts": dict(Counter(r.get("event_name", "<none>") for r in records).most_common()),
        "per_developer_counts": dict(Counter(r.get("user_email", "<none>") for r in records).most_common()),
        "distinct_sessions": len({r.get("session_id") for r in records}),
        "distinct_prompt_ids": len({r.get("prompt_id") for r in records if r.get("prompt_id")}),
        "time_span_utc": {
            "start": ns_to_iso(min(timestamps)),
            "end": ns_to_iso(max(timestamps)),
        } if timestamps else None,
    }


def fan_out(records: list[dict]) -> dict:
    """How many request/response events share each prompt_id -- decides whether
    the scoring unit is the user prompt or the individual API call."""
    by_prompt: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        pid = r.get("prompt_id")
        if pid:
            by_prompt[pid][r.get("event_name")] += 1

    responses_per_prompt = Counter(c[RESPONSE_BODY] for c in by_prompt.values())
    return {
        "prompt_ids_with_user_prompt": sum(1 for c in by_prompt.values() if c[PROMPT_EVENT]),
        "prompt_ids_with_response_body": sum(1 for c in by_prompt.values() if c[RESPONSE_BODY]),
        "responses_per_prompt_id_histogram": dict(sorted(responses_per_prompt.items())),
        "max_responses_for_one_prompt": max((c[RESPONSE_BODY] for c in by_prompt.values()), default=0),
    }


def build_pairs(records: list[dict]) -> list[dict]:
    """Pair each real user prompt with the final assistant response for its
    prompt_id, plus the model used. This is the candidate scorer input."""
    responses: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    models: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        pid = r.get("prompt_id")
        if not pid:
            continue
        if r.get("event_name") == RESPONSE_BODY:
            responses[pid].append((to_int(r.get("ts")), r.get("body", ""), r.get("model", "")))
        if r.get("event_name") in (METRICS_EVENT, REQUEST_BODY, RESPONSE_BODY) and r.get("model"):
            models[pid][r["model"]] += 1

    pairs = []
    for r in records:
        if r.get("event_name") != PROMPT_EVENT or not is_real_prompt(r):
            continue
        pid = r["prompt_id"]
        turn_responses = sorted(responses.get(pid, []))
        if not turn_responses:
            continue
        final = assistant_text(turn_responses[-1][1])
        if not final:
            continue
        model = models[pid].most_common(1)[0][0] if models[pid] else turn_responses[-1][2]
        pairs.append({
            "prompt_id": pid,
            "email": r.get("user_email"),
            "timestamp_utc": ns_to_iso(to_int(r.get("ts"))),
            "model": model,
            "prompt": r.get("prompt", ""),
            "response": final,
            "response_event_count": len(turn_responses),
        })
    return pairs


def economics(records: list[dict]) -> dict:
    rollup: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for r in records:
        if r.get("event_name") != METRICS_EVENT:
            continue
        dev = rollup[r.get("user_email", "<none>")]
        dev["api_requests"] += 1
        dev["input_tokens"] += to_int(r.get("input_tokens"))
        dev["output_tokens"] += to_int(r.get("output_tokens"))
        dev["cache_read_tokens"] += to_int(r.get("cache_read_tokens"))
        dev["cache_creation_tokens"] += to_int(r.get("cache_creation_tokens"))
        dev["cost_usd"] += to_float(r.get("cost_usd"))
    return {dev: {k: round(v, 4) for k, v in vals.items()} for dev, vals in rollup.items()}


def data_quality(records: list[dict], pairs: list[dict]) -> dict:
    body_events = [r for r in records if r.get("event_name") in (REQUEST_BODY, RESPONSE_BODY)]
    redacted = sum(1 for r in body_events if REDACTION in r.get("body", ""))
    real_prompts = sum(1 for r in records if r.get("event_name") == PROMPT_EVENT and is_real_prompt(r))
    slash_prompts = sum(1 for r in records if r.get("event_name") == PROMPT_EVENT and not is_real_prompt(r))
    return {
        "real_prompts": real_prompts,
        "slash_command_prompts_excluded": slash_prompts,
        "pairs_built": len(pairs),
        "body_events": len(body_events),
        "body_events_with_redacted_thinking": redacted,
        "empty_bodies": sum(1 for r in body_events if not r.get("body")),
    }


def print_report(report: dict, pairs: list[dict]) -> None:
    inv = report["inventory"]
    print("=" * 70)
    print("CLAUDE CODE LOG ANALYSIS")
    print("=" * 70)
    print(f"\nTotal entries:      {inv['total_entries']}")
    print(f"Distinct sessions:  {inv['distinct_sessions']}")
    print(f"Distinct prompt_ids:{inv['distinct_prompt_ids']}")
    if inv["time_span_utc"]:
        print(f"Time span (UTC):    {inv['time_span_utc']['start']}  ->  {inv['time_span_utc']['end']}")

    print("\n-- event_name distribution --")
    for name, n in inv["event_name_counts"].items():
        print(f"   {name:<24} {n}")

    print("\n-- per developer --")
    for dev, n in inv["per_developer_counts"].items():
        print(f"   {dev:<28} {n} entries")

    print("\n-- prompt_id fan-out (responses per prompt) --")
    fo = report["fan_out"]
    print(f"   prompt_ids with a user_prompt:   {fo['prompt_ids_with_user_prompt']}")
    print(f"   prompt_ids with a response body: {fo['prompt_ids_with_response_body']}")
    print(f"   responses-per-prompt histogram:  {fo['responses_per_prompt_id_histogram']}")
    print(f"   max responses for one prompt:    {fo['max_responses_for_one_prompt']}")

    print("\n-- economics (from api_request metrics) --")
    for dev, vals in report["economics"].items():
        print(f"   {dev}")
        print(f"      requests={int(vals['api_requests'])} in={int(vals['input_tokens'])} "
              f"out={int(vals['output_tokens'])} cache_read={int(vals['cache_read_tokens'])} "
              f"cost_usd=${vals['cost_usd']:.4f}")

    print("\n-- data quality --")
    for k, v in report["data_quality"].items():
        print(f"   {k:<34} {v}")

    print(f"\n-- candidate scoring pairs ({len(pairs)}) --")
    for p in pairs:
        prompt_preview = p["prompt"].replace("\n", " ")[:70]
        print(f"   [{p['email']:<22}] {p['model']:<26} {prompt_preview!r}")


def main() -> None:
    records = load_records(RAW)
    pairs = build_pairs(records)
    report = {
        "inventory": inventory(records),
        "fan_out": fan_out(records),
        "economics": economics(records),
        "data_quality": data_quality(records, pairs),
    }

    PROCESSED.mkdir(parents=True, exist_ok=True)
    (PROCESSED / "prompt_response_pairs.json").write_text(
        json.dumps(pairs, indent=2, ensure_ascii=False), encoding="utf-8")
    (PROCESSED / "summary.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print_report(report, pairs)
    print(f"\nWrote {PROCESSED / 'prompt_response_pairs.json'}")
    print(f"Wrote {PROCESSED / 'summary.json'}")


if __name__ == "__main__":
    main()
