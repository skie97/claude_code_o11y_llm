"""
fetch_loki_http.py -- pull Claude Code logs straight from Loki over HTTP, for the
scorer running INSIDE the observability compose network.

The SSH path (download_loki_data.sh) exists because Loki has no host port and must
be reached from the box via the container bridge IP. But the scorer container is
already on that network, so it skips SSH entirely and talks to the compose service
DNS `loki:3100` directly. This module is the thin I/O edge: it injects a urllib
transport into the shared, tested core in loki_chunker.py (histogram probe +
adaptive halving) -- no fetch algorithm is duplicated here.

It writes the same NDJSON shape download_loki_data.sh does (one entry per line:
all structured metadata + `ts` + `line`), so analyze.load_records consumes either
source unchanged.

Second job: scored_prompt_ids() reads the claude-code-scores stream back so the
scorer can skip prompts it has already scored -- the dedup source of truth, no side
state file. (See score_model_fit.py --skip-scored.)

Usage:
  python scripts/fetch_loki_http.py                 # last 30d -> data/raw/claude_code_logs.ndjson
  python scripts/fetch_loki_http.py --days 7
  python scripts/fetch_loki_http.py --scored-ids    # print already-scored prompt_ids (debug)
Env:
  LOKI_QUERY_URL (falls back to LOKI_PUSH_URL, then http://loki:3100)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from analyze import RAW
from loki_chunker import (
    CLAUDE_CODE_SELECTOR,
    DEFAULT_LIMIT,
    HOUR_NS,
    NS,
    SCORES_SELECTOR,
    QueryTooLarge,
    dedup_key,
    fetch_window,
    histogram_hours,
    total_count,
)
from push_scores_to_loki import prompt_ids_from_score_lines

REQ_TIMEOUT = 25          # seconds; a slow response means "too big, subdivide"
DEFAULT_DAYS = 30


def loki_root() -> str:
    """API root for Loki, from the same env the push side uses (one host)."""
    base = (os.environ.get("LOKI_QUERY_URL")
            or os.environ.get("LOKI_PUSH_URL")
            or "http://loki:3100")
    return base.rstrip("/") + "/loki/api/v1"


def _get(root: str, path: str, params: dict, *, timeout: int) -> dict:
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{root}{path}?{qs}", timeout=timeout) as resp:
        return json.load(resp)


def make_query_range(root: str, selector: str, *, timeout: int):
    """Build the transport fetch_window injects. ONLY the two signals that genuinely
    mean "this window is too big to return" become QueryTooLarge (-> subdivide):
    a read timeout, or a 5xx. A connection refusal / DNS failure means Loki is down,
    NOT that the window is oversize -- that propagates as a hard error so the caller
    aborts cleanly instead of thrashing every sub-window down to the 1s floor."""
    def query_range(lo: int, hi: int, limit: int) -> list:
        params = {"query": selector, "start": str(lo), "end": str(hi),
                  "limit": str(limit), "direction": "forward"}
        try:
            data = _get(root, "/query_range", params, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:                       # server couldn't assemble it
                raise QueryTooLarge(f"HTTP {exc.code}") from exc
            raise                                     # 4xx is our bug -- surface it
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise QueryTooLarge("read timed out") from exc
            raise                                     # connection refused / DNS = down
        except (socket.timeout, TimeoutError) as exc:
            raise QueryTooLarge("read timed out") from exc
        return [(int(ts), series["stream"], line)
                for series in data["data"]["result"]
                for ts, line in series["values"]]
    return query_range


def populated_hours(root: str, lo: int, hi: int, *, timeout: int) -> list[int]:
    data = _get(root, "/query_range", {
        "query": f"sum(count_over_time({CLAUDE_CODE_SELECTOR}[1h]))",
        "start": str(lo), "end": str(hi), "step": "3600",
    }, timeout=timeout)
    return histogram_hours(data["data"]["result"])


def _log_skip(lo: int, hi: int) -> None:
    sys.stderr.write(f"[fetch] skip oversize window @ {lo} (+{hi - lo}ns)\n")


def download(out_path: Path, lo: int, hi: int, *, root: str | None = None,
             timeout: int = REQ_TIMEOUT) -> int:
    """Fetch [lo, hi) to NDJSON at out_path (same shape as the SSH download).
    Returns the number of entries written."""
    root = root or loki_root()
    query_range = make_query_range(root, CLAUDE_CODE_SELECTOR, timeout=timeout)
    hours = populated_hours(root, lo, hi, timeout=timeout)
    sys.stderr.write(f"[fetch] {root}: {len(hours)} populated hour(s) in window\n")

    seen: set[tuple] = set()
    total = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for hour in hours:
            for ts, meta, line in fetch_window(
                hour, hour + HOUR_NS, query_range, limit=DEFAULT_LIMIT,
                floor_ns=NS, on_skip=_log_skip,
            ):
                key = dedup_key(meta, ts)
                if key in seen:
                    continue
                seen.add(key)
                record = dict(meta)
                record["ts"] = str(ts)
                record["line"] = line
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                total += 1
    sys.stderr.write(f"[fetch] wrote {total} entries to {out_path}\n")
    return total


def scored_prompt_ids(lo: int, hi: int, *, root: str | None = None,
                      timeout: int = REQ_TIMEOUT) -> set[str]:
    """Already-scored prompt_ids in [lo, hi), read from the claude-code-scores
    stream -- the dedup source of truth. Reuses the same chunker (the scores rows
    are tiny and single-label, so only the limit-page subdivision can ever fire)."""
    root = root or loki_root()
    query_range = make_query_range(root, SCORES_SELECTOR, timeout=timeout)
    lines = [line for _, _, line in fetch_window(lo, hi, query_range,
                                                 limit=DEFAULT_LIMIT, floor_ns=NS)]
    return prompt_ids_from_score_lines(lines)


def probe(root: str | None = None, *, timeout: int = REQ_TIMEOUT, days: int = 1) -> bool:
    """Read-only preflight: is Loki reachable at this root, and does the query path
    actually work? Hits /ready, then runs the same histogram count query the fetch
    uses, and reports how many claude-code entries are visible. Returns True iff both
    succeed -- the caller (CLI / run_scorer.sh) turns False into a non-zero exit so a
    misconfigured LOKI_QUERY_URL aborts loudly instead of failing mid-job."""
    root = root or loki_root()
    base = root.rsplit("/loki/api/v1", 1)[0]
    try:
        with urllib.request.urlopen(f"{base}/ready", timeout=timeout) as resp:
            ready = resp.read().decode().strip()
    except (urllib.error.URLError, OSError) as exc:
        sys.stderr.write(f"[probe] FAIL: Loki not reachable at {base} ({exc})\n")
        return False

    end_ns = time.time_ns()
    start_ns = end_ns - days * 24 * HOUR_NS
    try:
        data = _get(root, "/query_range", {
            "query": f"sum(count_over_time({CLAUDE_CODE_SELECTOR}[1h]))",
            "start": str(start_ns), "end": str(end_ns), "step": "3600",
        }, timeout=timeout)
    except (urllib.error.URLError, OSError) as exc:
        sys.stderr.write(f"[probe] FAIL: query path broken at {root} ({exc})\n")
        return False

    n = total_count(data["data"]["result"])
    sys.stderr.write(
        f"[probe] OK: {base} ready='{ready}', "
        f"{n} claude-code entries in last {days}d\n")
    return True


def _default_window(days: int) -> tuple[int, int]:
    end_ns = time.time_ns()
    return end_ns - days * 24 * HOUR_NS, end_ns


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(RAW), help="output NDJSON path")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="window size in days back from now (default 30)")
    parser.add_argument("--start-ns", type=int, help="explicit window start (ns)")
    parser.add_argument("--end-ns", type=int, help="explicit window end (ns)")
    parser.add_argument("--scored-ids", action="store_true",
                        help="print already-scored prompt_ids for the window and exit")
    parser.add_argument("--probe", action="store_true",
                        help="read-only connectivity preflight (GET /ready + one count "
                             "query); exits non-zero if Loki is unreachable")
    args = parser.parse_args()

    if args.probe:
        raise SystemExit(0 if probe(days=args.days) else 1)

    lo, hi = _default_window(args.days)
    if args.start_ns is not None:
        lo = args.start_ns
    if args.end_ns is not None:
        hi = args.end_ns

    if args.scored_ids:
        ids = scored_prompt_ids(lo, hi)
        print("\n".join(sorted(ids)))
        sys.stderr.write(f"[fetch] {len(ids)} already-scored prompt_ids in window\n")
        return

    download(Path(args.out), lo, hi)


if __name__ == "__main__":
    main()
