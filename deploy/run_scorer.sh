#!/usr/bin/env bash
#
# Scorer entrypoint: fetch the latest logs, score the new prompts, push one row
# per prompt to Loki. Run-to-completion job (compose restart: "no").
#
# Runs INSIDE the observability stack's compose network, so it reaches Loki at
# http://loki:3100 with no host port -- the whole point of running the scorer as a
# container behind Loki's port-less wall. Three stages:
#
#   1. fetch_loki_http.py   -- pull raw logs from loki:3100 over HTTP into
#                              data/raw/ (replaces the host-side SSH download; the
#                              container is already on the network). Set FETCH=0 to
#                              skip and use a pre-mounted data/raw instead.
#   2. score_model_fit.py   -- judge + score the prompts. --skip-scored (SKIP_SCORED=1,
#                              default) drops prompt_ids already in the scores stream,
#                              so re-runs are idempotent and never re-pay the judge.
#                              SCORER_ARGS appends flags, e.g. --dry-run for the
#                              offline stub (no API key).
#   3. push_scores_to_loki.py -- POST the new rows to the claude-code-scores stream.
set -euo pipefail
cd /app

# Preflight: every mode ends by pushing to Loki, so a misconfigured LOKI_QUERY_URL
# should abort here with a clear message rather than fail mid-job. set -e turns the
# probe's non-zero exit into an immediate stop. PROBE=0 skips it.
if [ "${PROBE:-1}" = "1" ]; then
  python scripts/fetch_loki_http.py --probe
fi

if [ "${FETCH:-1}" = "1" ]; then
  # Rolling window, not all-history: the scorer only scores NEW prompts (--skip-scored
  # skips everything already in the scores stream), so re-pulling a month of giant
  # response bodies every run is wasted work that grows unbounded over time. FETCH_DAYS
  # (default 3) keeps each run's fetch bounded and fast. Widen it (e.g. 30) for a first
  # backfill or after a long gap. // DEBT: a watermark-based incremental fetch (since
  # last run) would be even cheaper, but the rolling window is simple and bounded.
  python scripts/fetch_loki_http.py --days "${FETCH_DAYS:-3}"
fi

SKIP_ARG=""
[ "${SKIP_SCORED:-1}" = "1" ] && SKIP_ARG="--skip-scored"

# --emit-status: push a run_status row to the claude-code-scorer-health stream so
# the Grafana run-health alert can fire on a failed run (e.g. a bad JUDGE_MODEL).
# set -e means a fatal config error here (non-zero exit) aborts before the push below.
# Unquoted SCORER_ARGS on purpose: empty must expand to no argument; "--dry-run"
# must word-split into a flag.
python scripts/score_model_fit.py $SKIP_ARG --emit-status ${SCORER_ARGS:-}
python scripts/push_scores_to_loki.py
