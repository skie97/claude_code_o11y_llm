#!/usr/bin/env bash
#
# One-command scorer bootstrap. Bakes in every manual setup step the README
# describes, then schedules the job via host cron — so a fresh box goes from
# `git clone` to a running, self-refreshing scoring pipeline with one command.
#
# Intended to run ON THE LINUX HOST (the observability box), from anywhere:
#
#     bash claude_code_o11y_llm/deploy/setup.sh            # hourly cron, real judge
#     bash claude_code_o11y_llm/deploy/setup.sh --dry-run  # keyless offline stub
#     bash claude_code_o11y_llm/deploy/setup.sh --interval '*/30 * * * *'
#     bash claude_code_o11y_llm/deploy/setup.sh --no-cron  # deploy only, no schedule
#
# IDEMPOTENT: safe to re-run. It refreshes (never duplicates) the cron entry,
# only scaffolds $STACK/.env if absent, and `docker compose up -d --build`
# converges rather than re-creates. Re-run it to change the interval.
#
# What it CANNOT invent (it scaffolds + stops with a clear instruction instead):
#   - ANTHROPIC_API_KEY (a secret)         -> prompts on a TTY, else stops.
#   - the Loki datasource UID for alerting -> --with-alerting scaffolds the files
#                                             and prints the 2 remaining manual steps.
# It NEVER touches the public stack's compose/Caddyfile, the admin password, or
# domains — only our private overlay, $STACK/.env, and your crontab.
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults / flags
# ---------------------------------------------------------------------------
CRON_SCHEDULE="0 * * * *"     # hourly, on the hour
INSTALL_CRON=1
FETCH_DAYS_VAL=1              # rolling fetch window; small so a scheduled run finishes
                              # well under the interval (widen for a first backfill)
DRY_RUN=0                      # keyless: SCORER_ARGS=--dry-run, key not required
WITH_ALERTING=0
SMOKE=0                        # run one --dry-run scorer pass after `up` to prove the pipe
STACK_DIR="${STACK_DIR:-}"    # overridable via env or --stack-dir
NO_PROMPT=0                   # never prompt; stop instead (for unattended runs)

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; s/^#$//' | sed '$d'
  cat <<'EOF'

Flags:
  --stack-dir DIR     The stack's compose dir (holds the base docker-compose.yml).
                      Default: $STACK_DIR env, else a sibling dir named `observability`.
  --interval 'EXPR'   Cron schedule (5-field). Default: '0 * * * *' (hourly).
  --fetch-days N      Rolling fetch window written to $STACK/.env. Default: 1 (good for
                      a frequent cron). Bump (e.g. 30) for a first backfill / after an outage.
  --no-cron           Deploy only; do not install/refresh the cron entry.
  --dry-run           Configure the scorer keyless (SCORER_ARGS=--dry-run); no API key needed.
  --smoke             After `up`, run one offline (--dry-run) scorer pass to prove fetch->score->push.
  --with-alerting     Scaffold the Grafana alerting files from their .example templates.
  --no-prompt         Never prompt interactively; stop with instructions if a value is missing.
  -h, --help          Show this help.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --stack-dir)    STACK_DIR="$2"; shift 2 ;;
    --interval)     CRON_SCHEDULE="$2"; shift 2 ;;
    --fetch-days)   FETCH_DAYS_VAL="$2"; shift 2 ;;
    --no-cron)      INSTALL_CRON=0; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --smoke)        SMOKE=1; shift ;;
    --with-alerting) WITH_ALERTING=1; shift ;;
    --no-prompt)    NO_PROMPT=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "Unknown flag: $1" >&2; usage >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Locate this repo (script lives in deploy/) and the stack dir
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_NAME="$(basename "$REPO_ROOT")"

# The override's host paths are hard-coded ../claude_code_o11y_llm/... so the repo
# dir name is load-bearing — fail loud rather than let compose silently miss mounts.
[ "$REPO_NAME" = "claude_code_o11y_llm" ] || \
  die "This repo must be named 'claude_code_o11y_llm' (found '$REPO_NAME') so the override's relative paths resolve. Rename/clone accordingly."

# Default the stack dir to a sibling called `observability` (the box's layout).
if [ -z "$STACK_DIR" ]; then
  STACK_DIR="$(cd "$REPO_ROOT/.." && pwd)/observability"
fi
[ -d "$STACK_DIR" ] || die "Stack dir not found: $STACK_DIR  (pass --stack-dir DIR)"
[ -f "$STACK_DIR/docker-compose.yml" ] || \
  die "No docker-compose.yml in $STACK_DIR — is that the stack's compose dir? (pass --stack-dir DIR)"

# ---------------------------------------------------------------------------
# Preconditions: docker + compose plugin
# ---------------------------------------------------------------------------
DOCKER_BIN="$(command -v docker || true)"
[ -n "$DOCKER_BIN" ] || die "docker not found on PATH."
"$DOCKER_BIN" compose version >/dev/null 2>&1 || \
  die "The 'docker compose' plugin is not available. Install Docker Compose v2."

say "Repo:  $REPO_ROOT"
say "Stack: $STACK_DIR"

# ---------------------------------------------------------------------------
# 1. Config: ensure $STACK/.env exists, COMPOSE_FILE is set, key is present
# ---------------------------------------------------------------------------
ENV_FILE="$STACK_DIR/.env"
ENV_TEMPLATE="$SCRIPT_DIR/.env.example"

if [ ! -f "$ENV_FILE" ]; then
  say "Creating $ENV_FILE from template."
  cp "$ENV_TEMPLATE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
else
  say "$ENV_FILE already exists — leaving it in place."
fi

# Replace an existing `KEY=...` line in $ENV_FILE (or append if absent). Keeps the
# file as the single source of truth; never duplicates a key.
set_env_var() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  cat "$tmp" > "$ENV_FILE"   # preserve perms/inode rather than mv
  rm -f "$tmp"
}

# COMPOSE_FILE must point at base + our override so plain `docker compose` merges both.
if ! grep -q "^COMPOSE_FILE=" "$ENV_FILE"; then
  warn "COMPOSE_FILE missing from $ENV_FILE — adding the standard merge."
  set_env_var "COMPOSE_FILE" "docker-compose.yml:../claude_code_o11y_llm/deploy/docker-compose.scorer.yml"
fi

# Bound each run's fetch to a small rolling window so a scheduled run finishes well
# under the cron interval (the overlap that double-judges prompts comes from runs
# outlasting the interval). Single-sourced in $STACK/.env so manual + cron runs agree.
say "Setting FETCH_DAYS=$FETCH_DAYS_VAL in $ENV_FILE (rolling fetch window)."
set_env_var "FETCH_DAYS" "$FETCH_DAYS_VAL"

current_key="$(sed -n 's/^ANTHROPIC_API_KEY=//p' "$ENV_FILE" | head -n1)"
if [ "$DRY_RUN" = "1" ]; then
  say "Keyless mode (--dry-run): setting SCORER_ARGS=--dry-run; API key not required."
  set_env_var "SCORER_ARGS" "--dry-run"
elif [ -z "$current_key" ]; then
  if [ "$NO_PROMPT" = "1" ] || [ ! -t 0 ]; then
    die "ANTHROPIC_API_KEY is empty in $ENV_FILE.
       Set it (e.g.  ANTHROPIC_API_KEY=sk-ant-...) and re-run, or use --dry-run for the keyless offline stub."
  fi
  printf 'Enter ANTHROPIC_API_KEY (input hidden, or press Enter to use --dry-run keyless mode): '
  read -rs typed_key; echo
  if [ -z "$typed_key" ]; then
    say "No key entered — falling back to keyless --dry-run mode."
    DRY_RUN=1
    set_env_var "SCORER_ARGS" "--dry-run"
  else
    set_env_var "ANTHROPIC_API_KEY" "$typed_key"
    say "API key written to $ENV_FILE (chmod 600)."
  fi
else
  say "ANTHROPIC_API_KEY already set in $ENV_FILE."
fi

# ---------------------------------------------------------------------------
# 2. (optional) Scaffold Grafana alerting files
# ---------------------------------------------------------------------------
if [ "$WITH_ALERTING" = "1" ]; then
  ALERT_DIR="$SCRIPT_DIR/grafana-scoring/alerting"
  for base in scorer-alerts contactpoints; do
    if [ -f "$ALERT_DIR/$base.yaml" ]; then
      say "Alerting: $base.yaml already present — leaving it."
    else
      cp "$ALERT_DIR/$base.yaml.example" "$ALERT_DIR/$base.yaml"
      say "Alerting: scaffolded $base.yaml from template."
    fi
  done
  warn "Alerting needs 2 manual steps the script cannot do for you:"
  warn "  (a) replace REPLACE_WITH_LOKI_DATASOURCE_UID in $ALERT_DIR/scorer-alerts.yaml"
  warn "      (Grafana -> Connections -> Loki -> UID, or GET /api/datasources),"
  warn "  (b) uncomment the 'alerting' mount in deploy/docker-compose.scorer.yml,"
  warn "      then re-run this script and add a nested route (component=scorer -> scorer-oncall) in the Grafana UI."
fi

# ---------------------------------------------------------------------------
# 3. Build the scorer image, then bring the long-running stack + dashboard up
# ---------------------------------------------------------------------------
# The scorer service is profiled (run-on-demand), so `up -d` neither starts NOR
# builds it. Build it explicitly first (--profile scorer) so cron's `run` finds an
# image; then `up -d` brings up the long-running services + the Grafana mounts.
say "Building images, incl. the run-on-demand scorer (docker compose --profile scorer build)…"
( cd "$STACK_DIR" && "$DOCKER_BIN" compose --profile scorer build )
say "Starting the stack (Grafana dashboard + Loki; the scorer runs on demand via cron)…"
( cd "$STACK_DIR" && "$DOCKER_BIN" compose up -d )

# ---------------------------------------------------------------------------
# 4. Verify the scoring dashboard mount landed inside Grafana
# ---------------------------------------------------------------------------
if "$DOCKER_BIN" exec grafana ls /etc/grafana/provisioning/dashboards/ 2>/dev/null | grep -q scoring-provider.yaml; then
  say "Verified: scoring dashboard provider mounted in Grafana."
else
  warn "Could not confirm the scoring-provider.yaml mount in Grafana — check 'docker exec grafana ls /etc/grafana/provisioning/dashboards/'."
fi

# ---------------------------------------------------------------------------
# 5. (optional) offline smoke run — prove fetch -> score -> push end to end
# ---------------------------------------------------------------------------
if [ "$SMOKE" = "1" ]; then
  say "Smoke run: one offline (--dry-run) scorer pass…"
  ( cd "$STACK_DIR" && "$DOCKER_BIN" compose --profile scorer run --rm -e SCORER_ARGS=--dry-run scorer )
fi

# ---------------------------------------------------------------------------
# 6. Install / refresh the cron entry (marker-based, idempotent)
# ---------------------------------------------------------------------------
if [ "$INSTALL_CRON" = "1" ]; then
  # The marker lets a re-run REPLACE its own line instead of appending a duplicate.
  MARKER="# claude-code-scorer (managed by deploy/setup.sh)"
  LOG_FILE="\$HOME/scorer-cron.log"
  # Go through cron_scorer.sh: it flock-guards the run so a tick firing while the
  # previous run is still going is SKIPPED, not overlapped (overlap double-judges
  # prompts). Pass DOCKER_BIN explicitly — cron's PATH is minimal. `bash <script>`
  # so no execute bit is required on the checkout.
  WRAPPER="$SCRIPT_DIR/cron_scorer.sh"
  CRON_CMD="DOCKER_BIN=$DOCKER_BIN bash $WRAPPER $STACK_DIR >> $LOG_FILE 2>&1"
  CRON_LINE="$CRON_SCHEDULE $CRON_CMD $MARKER"

  # Drop any prior managed line, keep everything else, append the fresh line.
  existing="$(crontab -l 2>/dev/null | grep -vF "$MARKER" || true)"
  {
    [ -n "$existing" ] && printf '%s\n' "$existing"
    printf '%s\n' "$CRON_LINE"
  } | crontab -

  say "Cron installed/refreshed:  $CRON_SCHEDULE  ->  cron_scorer.sh (flock-guarded run)"
  say "  (logs append to $LOG_FILE; remove with: crontab -l | grep -vF '$MARKER' | crontab -)"
else
  say "Skipping cron (--no-cron). The scorer ran once with 'up'; schedule it yourself or re-run without --no-cron."
fi

if [ "$INSTALL_CRON" = "1" ]; then
  say "Done. Scoring pipeline is deployed and scheduled."
else
  say "Done. Scoring pipeline is deployed (no cron)."
fi
