"""
push_scores_to_loki.py -- emit per-prompt model-fit scores back into Loki as a
new log stream, one JSON line per prompt, for Grafana to aggregate.

Design (see README "Roadmap"): the scorer stays a thin per-prompt emitter. All
counting and work-weighting -- right-sized / over- / under-provisioned rates per
developer and project, the weekly leaderboard -- is done at query time in Grafana
(LogQL), not pre-aggregated here. So this adapter just shapes the rows from
model_scores.json into a Loki push and sends it.

Cardinality: ONLY `service_name` is a stream label. prompt_id, email, fit, and
the weights ride inside the JSON line (parsed downstream with `| json`), so the
stream stays single-identity -- the same lesson the source data taught us.

The pure payload builder is unit-tested; the HTTP POST is the I/O edge.

Usage:
  python scripts/push_scores_to_loki.py --dry-run   # print payload, send nothing
  python scripts/push_scores_to_loki.py             # POST to $LOKI_PUSH_URL (default loki:3100)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

from analyze import PROCESSED

# Only label on the scores stream; everything else is in the JSON line.
SCORES_STREAM = {"service_name": "claude-code-scores"}
# Compose service DNS -- reachable from inside the stack network, which is the
# whole point of running the scorer as a container behind Loki's (port-less) wall.
DEFAULT_LOKI_URL = "http://loki:3100"
PUSH_TIMEOUT = 15


def build_push_payload(rows: list[dict], ts_ns: int) -> dict:
    """Loki push-API body for the score rows: one JSON log line per row.

    Timestamps increment by 1ns per row so they are strictly ascending and never
    collide within the stream, however fast the batch is assembled. An empty
    input yields no stream (Loki rejects a stream with no values)."""
    if not rows:
        return {"streams": []}
    values = [
        [str(ts_ns + i), json.dumps(row, ensure_ascii=False, sort_keys=True)]
        for i, row in enumerate(rows)
    ]
    return {"streams": [{"stream": SCORES_STREAM, "values": values}]}


def prompt_ids_from_score_lines(lines: Iterable[str]) -> set[str]:
    """Extract the set of already-scored prompt_ids from raw score log lines.

    The scores stream is the single source of truth for "what's already scored"
    (no side state file) -- so re-runs read this back and skip those prompts before
    paying the judge. Each line is a JSON row build_push_payload wrote, carrying a
    prompt_id. Malformed lines and rows without a prompt_id are skipped, not fatal."""
    ids: set[str] = set()
    for line in lines:
        try:
            pid = json.loads(line).get("prompt_id")
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue
        if pid:
            ids.add(pid)
    return ids


def push(payload: dict, loki_url: str, *, timeout: int = PUSH_TIMEOUT) -> None:
    """POST a push payload to Loki. Raises urllib.error.HTTPError on rejection
    (e.g. 400 for a malformed entry); the caller decides how to surface it."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{loki_url}/loki/api/v1/push", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()  # drain; Loki returns 204 No Content on success


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the push payload instead of sending it")
    parser.add_argument("--scores", default=str(PROCESSED / "model_scores.json"),
                        help="path to model_scores.json (output of score_model_fit.py)")
    args = parser.parse_args()

    rows = json.loads(Path(args.scores).read_text(encoding="utf-8"))
    payload = build_push_payload(rows, ts_ns=time.time_ns())

    if args.dry_run or not rows:
        print(json.dumps(payload, indent=2)[:2000])
        print(f"\n[push] dry-run: {len(rows)} rows (not sent)")
        return

    loki_url = os.environ.get("LOKI_PUSH_URL", DEFAULT_LOKI_URL)
    try:
        push(payload, loki_url)
    except (urllib.error.URLError, OSError) as exc:
        print(f"[push] failed to POST to {loki_url}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[push] pushed {len(rows)} score rows to {loki_url}")


if __name__ == "__main__":
    main()
