#!/usr/bin/env bash
#
# Unit tests for cron_scorer.sh — the cron wrapper's exit-code CONTRACT:
#
#   * lock held by a still-running scorer  -> wrapper exits 0  (a skip is healthy
#     back-pressure, not a failure; cron/monitoring must NOT alarm on it)
#   * scorer succeeds                      -> exit 0 (pass-through)
#   * scorer fails (e.g. fatal JUDGE_MODEL)-> that non-zero code PROPAGATES
#   * no STACK_DIR argument                -> usage error (non-zero)
#
# flock is stubbed via the FLOCK_BIN seam so the test needs no real lock or docker
# and is deterministic. Run:  bash deploy/test_cron_scorer.sh
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAP="$DIR/cron_scorer.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Stub flock(1): ignore every argument, exit with $STUB_RC. Simulating "lock held"
# means STUB_RC=99 (the wrapper asks real flock for -E 99 on conflict).
stub="$tmp/flock"
cat > "$stub" <<'EOF'
#!/usr/bin/env bash
exit "${STUB_RC:-0}"
EOF
chmod +x "$stub"

fail=0
check() { # actual expected label
  if [ "$1" = "$2" ]; then echo "ok: $3"; else echo "FAIL: $3 (want $2, got $1)"; fail=1; fi
}

# 1. overlap (lock held) -> exit 0, and a skip line is printed
out="$(STUB_RC=99 FLOCK_BIN="$stub" bash "$WRAP" /tmp 2>&1)"; rc=$?
check "$rc" 0 "overlap skip maps to exit 0"
case "$out" in
  *skipped*) echo "ok: skip message printed" ;;
  *) echo "FAIL: skip message missing (got: $out)"; fail=1 ;;
esac

# 2. scorer success -> 0 passes through
STUB_RC=0 FLOCK_BIN="$stub" bash "$WRAP" /tmp >/dev/null 2>&1
check "$?" 0 "success passes through as 0"

# 3. real scorer failure -> the command's code propagates (not masked as a skip)
STUB_RC=7 FLOCK_BIN="$stub" bash "$WRAP" /tmp >/dev/null 2>&1
check "$?" 7 "real failure propagates"

# 4. missing STACK_DIR -> non-zero usage error
STUB_RC=0 FLOCK_BIN="$stub" bash "$WRAP" >/dev/null 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then echo "ok: missing STACK_DIR errors"; else echo "FAIL: missing arg should error"; fail=1; fi

echo "---"
[ "$fail" = 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit "$fail"
