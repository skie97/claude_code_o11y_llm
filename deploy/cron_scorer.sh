#!/usr/bin/env bash
#
# Cron wrapper: run the scorer AT MOST ONCE AT A TIME.
#
# Why this exists: a scorer run fetches giant response bodies + judges every new
# prompt, so a run can take longer than the cron interval. Two overlapping runs
# both read the scores stream BEFORE either has pushed (--skip-scored), so both see
# the same prompts as unscored and both judge + push them -> the same prompt is
# scored 2-3x (wasted judge $ + double-counted dashboards).
#
# `flock -n` makes a cron tick that fires while the previous run is still going
# SKIP rather than queue/overlap. A skip is healthy back-pressure, so it exits 0;
# only a genuine scorer failure propagates its non-zero code (so cron/monitoring
# still surface real failures, e.g. a fatal JUDGE_MODEL).
#
# Usage (installed by deploy/setup.sh):  bash cron_scorer.sh STACK_DIR
set -uo pipefail

STACK_DIR="${1:?usage: cron_scorer.sh STACK_DIR}"
LOCK="${SCORER_LOCK:-/tmp/claude-code-scorer.lock}"
FLOCK_BIN="${FLOCK_BIN:-flock}"                                  # testability seam
DOCKER_BIN="${DOCKER_BIN:-$(command -v docker || echo /usr/bin/docker)}"
CONFLICT_RC=99                                                   # flock -E: "lock held" code

# --profile scorer: the scorer service is profiled so `up -d` does NOT start it;
# `run` re-enables the profile for this one-off. -T: no TTY (cron has none).
"$FLOCK_BIN" -n -E "$CONFLICT_RC" "$LOCK" \
  -c "cd '$STACK_DIR' && '$DOCKER_BIN' compose --profile scorer run --rm -T scorer"
rc=$?

if [ "$rc" -eq "$CONFLICT_RC" ]; then
  echo "[cron] skipped: a previous scorer run is still in progress"
  exit 0
fi
exit "$rc"
