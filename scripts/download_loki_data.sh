#!/usr/bin/env bash
#
# download_loki_data.sh — pull the full Claude Code log dump out of Loki.
#
# Loki on the remote host publishes no host port; it is only reachable from
# that host via the container bridge IP. So we run the fetch ON the host (over
# SSH) and stream the resulting NDJSON back down stdout into a local file.
#
# Each output line is one Loki entry: all of its structured metadata
# (event_name, user_email, prompt_id, model, body, ...) plus `ts` (nanoseconds)
# and `line` (the raw log line).
#
# Usage:
#   bash scripts/download_loki_data.sh [output_file]
# Env overrides:
#   LOKI_SSH_KEY, LOKI_SSH_HOST, START_NS, END_NS (default window: last 30 days)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Optional machine-local config (gitignored): real SSH host/key for this box.
# Sourced after REPO_DIR is set, so it may reference $REPO_DIR.
# shellcheck source=/dev/null
[ -f "$SCRIPT_DIR/loki.env.local" ] && source "$SCRIPT_DIR/loki.env.local"

KEY="${LOKI_SSH_KEY:-$REPO_DIR/loki-ssh-key.pem}"
HOST="${LOKI_SSH_HOST:?set LOKI_SSH_HOST to the Loki SSH target, e.g. ubuntu@your-host (or create scripts/loki.env.local)}"
OUT="${1:-$REPO_DIR/data/raw/claude_code_logs.ndjson}"

END_NS="${END_NS:-$(date +%s)000000000}"
START_NS="${START_NS:-$(date -d '30 days ago' +%s)000000000}"

mkdir -p "$(dirname "$OUT")"

echo "[download] window ${START_NS} -> ${END_NS} ns, host ${HOST}" >&2

# The remote program reads its own program text from stdin (`python3 -`) and
# takes the time window as argv. It resolves Loki's container IP at runtime and
# writes NDJSON to stdout; progress/summary go to stderr so the local stdout
# redirect stays pure NDJSON.
#
# NOTE: the histogram-probe + adaptive-halving algorithm below is the canonical
# copy's twin. The tested, single source of that logic lives in scripts/loki_chunker.py
# and is reused by scripts/fetch_loki_http.py (the in-cluster HTTP fetch). This
# heredoc is the one irreducible duplicate: it is program text piped to a remote
# `python3 -` over SSH, so it cannot import the module. Keep the two in sync. // DEBT
#
# Why this isn't a simple paged loop: every distinct `body` (a full Claude Code
# conversation, often tens of thousands of tokens) is part of the Loki stream
# identity. A wide query_range therefore tries to ship many megabytes of bodies
# back through the querier->frontend gRPC channel and blows past its message-size
# limit ("error notifying frontend about finished query"), so the client just
# times out. We can't retune Loki (don't-modify-the-stack constraint), so we keep
# every response small from the client side:
#   1. A cheap histogram probe -- sum(count_over_time(...)) collapses the giant
#      labels -- finds which hours actually hold data (retention is mostly empty).
#   2. Each populated hour is fetched with adaptive halving: any window that times
#      out or hits the entry limit is split in two and retried, so a burst of
#      huge bodies can never overflow a single response.
ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST" \
    "python3 - ${START_NS} ${END_NS}" > "$OUT" <<'REMOTE'
import json, subprocess, sys, urllib.error, urllib.parse, urllib.request

start_ns, end_ns = int(sys.argv[1]), int(sys.argv[2])
NS = 1_000_000_000
LIMIT = 5000          # Loki's max entries per response
REQ_TIMEOUT = 25      # seconds; a slow response means "too big, subdivide"

ip = subprocess.check_output(
    ["docker", "inspect", "loki",
     "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"]
).decode().strip()
root = f"http://{ip}:3100/loki/api/v1"
SELECTOR = '{service_name="claude-code"}'
sys.stderr.write(f"[remote] loki at {ip}:3100\n")


def get(path, params):
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{root}{path}?{qs}", timeout=REQ_TIMEOUT) as r:
        return json.load(r)


def populated_hours(lo, hi):
    """Return [hour_start_ns] that contain >=1 entry, via a tiny summed histogram."""
    data = get("/query_range", {
        "query": f"sum(count_over_time({SELECTOR}[1h]))",
        "start": str(lo), "end": str(hi), "step": "3600",
    })
    hours = []
    for series in data["data"]["result"]:
        for ts_s, val in series["values"]:
            if float(val) > 0:
                hours.append(int(float(ts_s)) * NS)
    return sorted(set(hours))


seen = set()          # (session_id, event_sequence, ts) -> guards against overlap
total = 0
counts: dict[str, int] = {}


def emit(stream_meta, ts, line):
    global total
    key = (stream_meta.get("session_id"), stream_meta.get("event_sequence"), ts)
    if key in seen:
        return
    seen.add(key)
    record = dict(stream_meta)
    record["ts"] = str(ts)
    record["line"] = line
    sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
    total += 1
    name = stream_meta.get("event_name", "<none>")
    counts[name] = counts.get(name, 0) + 1


def fetch(lo, hi):
    """Fetch [lo, hi) entries, halving on timeout/oversize/limit-hit."""
    try:
        data = get("/query_range", {
            "query": SELECTOR, "start": str(lo), "end": str(hi),
            "limit": str(LIMIT), "direction": "forward",
        })
    except (urllib.error.URLError, OSError):
        if hi - lo <= NS:                       # 1s floor: give up subdividing
            sys.stderr.write(f"[remote] skip oversize 1s window @ {lo}\n")
            return
        mid = lo + (hi - lo) // 2
        fetch(lo, mid); fetch(mid, hi)
        return

    entries = [(int(ts), s["stream"], line)
               for s in data["data"]["result"] for ts, line in s["values"]]
    if len(entries) >= LIMIT and hi - lo > NS:  # capped: window too coarse
        mid = lo + (hi - lo) // 2
        fetch(lo, mid); fetch(mid, hi)
        return
    for ts, meta, line in sorted(entries, key=lambda e: e[0]):
        emit(meta, ts, line)


for hour in populated_hours(start_ns, end_ns):
    fetch(hour, hour + 3600 * NS)

sys.stderr.write(f"[remote] {total} entries; event_names={counts}\n")
REMOTE

echo "[download] wrote $(wc -l < "$OUT") lines to ${OUT}" >&2
