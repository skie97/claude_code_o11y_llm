# Scorer deploy (private compose override)

Layers the LLM-as-Judge scoring stage onto the **public** observability stack
(`../claude_code_o11y`) without editing it. The override + scoring code stay in this
repo; the only file that lives outside it is the deployer's `.env` (secrets +
`COMPOSE_FILE`), which Compose requires in the stack dir — see Deploy below.

## What gets added

- **`scorer`** — a run-to-completion container (`restart: "no"`) on the stack
  network. Because it sits *inside* the network it reaches `loki:3100` directly
  (Loki has no host port) and egresses to `api.anthropic.com` for the judge.
- **scoring dashboard** — mounted into the already-running Grafana via provisioning.

## One run, three stages (`run_scorer.sh`)

```
fetch_loki_http.py    pull raw claude-code logs from loki:3100 over HTTP -> data/raw/*.ndjson
        |             (histogram-probe populated hours, then adaptive-halving per hour)
        v
score_model_fit.py    judge + score each NEW prompt; --skip-scored drops prompt_ids
        |             already in the claude-code-scores stream (idempotent re-runs)
        v
push_scores_to_loki.py  POST one JSON row per prompt -> stream service_name="claude-code-scores"
```

Grafana then aggregates the `claude-code-scores` stream at query time (LogQL `| json`).

## Quick start (one command)

`deploy/setup.sh` bakes in every step below — it scaffolds `$STACK/.env`, builds,
brings the stack + scorer + dashboard up, verifies the Grafana mount, and installs
an **idempotent host cron** so the job keeps running. Run it on the box:

```bash
# real Sonnet judge, hourly cron (prompts for ANTHROPIC_API_KEY if unset):
bash claude_code_o11y_llm/deploy/setup.sh

# prove it first, keyless + offline (no API key, runs the --dry-run stub):
bash claude_code_o11y_llm/deploy/setup.sh --dry-run --smoke

# pick a schedule / skip cron / point at a non-default stack dir:
bash claude_code_o11y_llm/deploy/setup.sh --interval '*/30 * * * *'
bash claude_code_o11y_llm/deploy/setup.sh --no-cron
bash claude_code_o11y_llm/deploy/setup.sh --stack-dir /home/ubuntu/observability
```

It is **safe to re-run** (refreshes the cron line instead of duplicating it; only
creates `$STACK/.env` if absent) — re-run it to change the interval. It never edits
the public stack, the admin password, or domains. Two things it can only *scaffold*
because it can't invent them: your `ANTHROPIC_API_KEY` (it prompts, or stops with the
exact line to edit under `--no-prompt`/non-TTY) and the alerting Loki UID
(`--with-alerting` copies the templates and prints the 2 remaining manual steps).

The manual walkthrough below is the same steps, by hand, if you'd rather not use the script.

## Deploy

This repo must be cloned as a **sibling of the stack dir, named exactly
`claude_code_o11y_llm`** — the override's `../claude_code_o11y_llm/...` paths
resolve against the stack's compose dir, so only *our* dir name matters (the stack
dir can be named anything, e.g. `observability`). `$STACK` below is that stack dir.

> **Where `.env` goes:** Compose reads `.env` from the directory you run it in
> (`$STACK`), **not** from this repo's `deploy/` folder. So the scorer's config
> lives in **`$STACK/.env`** — use this repo's `deploy/.env.example` as the template.
> Setting `COMPOSE_FILE` there also makes plain `docker compose` from `$STACK`
> auto-merge both files, so you never have to type `-f` again (for up, down,
> restart, logs, config, run — everything).

```bash
# 1. config: copy the template to $STACK/.env and set the key. The template already
#    carries COMPOSE_FILE (so plain `docker compose` from $STACK merges the stack +
#    the scorer override automatically) — just confirm its relative path fits your layout.
cp .../claude_code_o11y_llm/deploy/.env.example "$STACK/.env"
$EDITOR "$STACK/.env"     # set ANTHROPIC_API_KEY (or SCORER_ARGS=--dry-run to start keyless)

# 2. from the stack dir, render the merged config (read-only) and eyeball that grafana
#    keeps its base volumes (grafana_data + provisioning) PLUS our 2 scoring mounts:
cd "$STACK"
docker compose config | less     # COMPOSE_FILE makes -f unnecessary

# 3. apply. The scorer is a profiled, run-on-demand job, so `up` neither starts NOR
#    builds it — build it explicitly first, then bring up the long-running services
#    (recreates grafana with the scoring dashboard; brief grafana blip).
docker compose --profile scorer build
docker compose up -d

# 4. confirm the dashboard provider landed inside the nested provisioning mount:
docker exec grafana ls /etc/grafana/provisioning/dashboards/
```

Project name defaults to the stack dir's basename (so volumes are `<stackdir>_loki_data`
etc.) — run compose from `$STACK` so it stays stable.

## Run the job

With `COMPOSE_FILE` in `$STACK/.env`, every command from `$STACK` already merges
both files — no `-f` needed.

```bash
# first time, prove the pipeline offline (no API key): fetch -> stub score -> push
docker compose --profile scorer run --rm -e SCORER_ARGS=--dry-run scorer

# real judge (needs ANTHROPIC_API_KEY in $STACK/.env):
docker compose --profile scorer run --rm scorer
```

The scorer is **profiled** (`profiles: ["scorer"]`), so `docker compose up -d` does
**not** start it — `up` would otherwise fire one unscheduled run per deploy, which then
overlaps the cron. `run` re-enables the profile for a one-off, so the commands above (and
the cron) still work. The job preflights Loki connectivity (`--probe`) and aborts cleanly
if `loki:3100` is unreachable. Re-runs are idempotent (`--skip-scored` reads the scores
stream and skips prompts already there).

**Schedule via `deploy/cron_scorer.sh`, not a bare `run`.** A run can outlast the cron
interval (it fetches giant bodies + judges every new prompt); two overlapping runs both
read the scores stream before either pushes, so both judge + push the same prompts —
double cost, double-counted dashboards. The wrapper `flock -n`-guards the run so an
overlapping tick is **skipped** (exit 0; a real failure still propagates). **`deploy/setup.sh`
installs that flock-guarded cron for you** (default hourly, `--interval` to change;
`--fetch-days` to size the window) — re-running it refreshes the line rather than
duplicating it. Keep the window small (`FETCH_DAYS=1`, the setup default) so a run
finishes well under the interval.

## Failure handling & alerting

A bad `JUDGE_MODEL` (or key) is a **config** error — it fails identically for every
prompt. The scorer treats it as fatal: it preflights the model (`models.retrieve`,
no inference) before scoring, aborts **non-zero** with a clear message, and never
writes an empty `model_scores.json` and exits 0. The failure surfaces on **two
independent channels** (deliberately — the error must not travel through whatever is
down):

1. **Exit code (dependency-free, primary).** The job exits non-zero → visible in
   `docker logs scorer`, to Docker, and to cron. This is the channel that still works
   when Loki itself is down. Wire a cron notification on it:
   ```cron
   0 * * * *  DOCKER_BIN=/usr/bin/docker bash ~/claude_code_o11y_llm/deploy/cron_scorer.sh ~/observability >> ~/scorer-cron.log 2>&1 || \
              echo "scorer FAILED $(date)" | mail -s "Claude scorer failed" you@example.com
   ```
   (the wrapper exits 0 on a skipped overlap, so `||` only fires on a real failure).

2. **Loki health event → Grafana alert (dashboard/notify).** Every run pushes one
   `run_status` row (`--emit-status`) to a separate stream `claude-code-scorer-health`
   (`status` ok/failed, `judge_model`, `scored`, `skipped`, `reason`). The
   **Scorer Health** dashboard (auto-provisioned) visualizes it; the alert rules in
   `grafana-scoring/alerting/scorer-alerts.yaml` fire on `status="failed"` (last hour)
   or zero runs in 6h (stale cron).

   To enable the alert (opt-in — a provisioned rule with a bad datasource UID is
   rejected, so it isn't mounted by default). Both filled files are **gitignored** —
   they carry your deployment's UID / destinations, so they stay box-local:
   1. `cp scorer-alerts.yaml.example scorer-alerts.yaml` and replace
      `REPLACE_WITH_LOKI_DATASOURCE_UID` with your Loki UID (Grafana → Connections →
      Loki → UID, or `GET /api/datasources`). Grafana loads only `*.yaml`, so the
      `.example` template is ignored.
   2. `cp contactpoints.yaml.example contactpoints.yaml` and set a real destination
      (safe — adding a contact point touches nothing else). **Use Slack/webhook, not
      email:** plain Grafana has no SMTP, so an `email` contact point silently fails
      (`SMTP not configured`) and the firing alert errors on every eval. The template
      defaults to a Slack incoming webhook — paste your `hooks.slack.com/...` URL.
   3. Uncomment the `alerting` mount in `docker-compose.scorer.yml`, then reload Grafana
      so it provisions the rules + contact point: `docker compose up -d` (recreates
      grafana) or `docker compose restart grafana`. Finally, in Grafana add a **nested**
      notification route matching `component = scorer` → `scorer-oncall`
      (Alerting → Notification policies → New nested policy). Without the route the
      rules still fire and show on the dashboard, but notifications fall through to the
      root default policy (the `grafana-default-email` no-SMTP dead end). **Don't
      provision a root policy** — it would hijack routing for every other alert on this
      shared Grafana, which is why this one step is UI-only by design.

   If provisioned-rule schema drift bites your Grafana version, just recreate the two
   rules in the UI from the LogQL in that file — the queries are the load-bearing part.

   **Verify it's actually delivering** (not just firing): on the box,
   `docker logs grafana 2>&1 | grep -iE 'ngalert|alertmanager' | tail` — a working
   route shows `Sending alerts to local notifier` followed by a successful notify,
   *not* `SMTP not configured`. The `scorer-stale` rule is a good live test: it fires
   whenever there's been no run in 6h and self-resolves on the next successful run.

## Day-2 ops (up / down from the stack dir)

Once `COMPOSE_FILE` is set, the stack folder behaves normally — just **never**
`down -v`, which would also drop `grafana_data` + `caddy_data` (TLS certs):

```bash
docker compose up -d        # stack + dashboard (scorer is profiled — runs on demand only)
docker compose down         # safe: removes containers/network, KEEPS all named volumes
```

To wipe data for a test cycle, target the two volumes explicitly (keeps Grafana +
Caddy intact):

```bash
docker compose down
docker volume rm <stackdir>_loki_data <stackdir>_prometheus_data
docker compose up -d
```

## Knobs (`$STACK/.env`)

Lives in the **stack dir**, not this repo's `deploy/`. Template: `deploy/.env.example`.

| var | default | meaning |
|-----|---------|---------|
| `COMPOSE_FILE` | — | set to `docker-compose.yml:../claude_code_o11y_llm/deploy/docker-compose.scorer.yml` so plain `docker compose` auto-merges both |
| `ANTHROPIC_API_KEY` | — | judge key; omit only with `SCORER_ARGS=--dry-run` |
| `JUDGE_MODEL` | `claude-sonnet-4-6` | judge model (pinned — upgrade deliberately + re-validate, don't float to "latest") |
| `LOKI_PUSH_URL` / `LOKI_QUERY_URL` | `http://loki:3100` | one host; query falls back to push |
| `SCORER_ARGS` | — | `--dry-run` uses the offline stub (no key) |
| `PROBE` | `1` | `0` = skip the Loki connectivity preflight |
| `FETCH` | `1` | `0` = skip HTTP fetch, score a pre-mounted `data/raw` |
| `FETCH_DAYS` | `1` | rolling fetch window (days). Bounds each run so it finishes under the cron interval; `setup.sh` writes `1` (override with `--fetch-days`). Widen (e.g. `30`) for a first backfill or after a gap |
| `SKIP_SCORED` | `1` | `0` = re-score everything in the window |

## Verify on first deploy

- Grafana's base volumes (`grafana_data`, base provisioning) survive the override
  merge — **confirmed** via `docker compose config`: Compose *concatenates*
  `grafana.volumes`, so the 2 base mounts + our 2 scoring mounts all render.
- Container egress to `api.anthropic.com:443` (host-level already returns 401).
- A first `--dry-run` job end-to-end (fetch → stub score → push), then check the
  `claude-code-scores` stream in Grafana before switching the judge on.
