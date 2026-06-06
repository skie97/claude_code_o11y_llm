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

# 3. apply: builds the scorer image, recreates grafana with the scoring dashboard
#    (brief grafana blip), runs the scorer once (restart:"no" job), leaves the rest.
docker compose up -d --build

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
docker compose run --rm -e SCORER_ARGS=--dry-run scorer

# real judge (needs ANTHROPIC_API_KEY in $STACK/.env):
docker compose run --rm scorer
```

`docker compose up -d` also fires the scorer once, since it's part of the merged
set (it's a `restart:"no"` job: runs to completion, exits). The job preflights Loki
connectivity (`--probe`) and aborts cleanly if `loki:3100` is unreachable. Re-runs
are idempotent (`--skip-scored` reads the scores stream and skips prompts already
there), so you can also schedule `docker compose run --rm scorer` with host cron as
often as you like.

## Day-2 ops (up / down from the stack dir)

Once `COMPOSE_FILE` is set, the stack folder behaves normally — just **never**
`down -v`, which would also drop `grafana_data` + `caddy_data` (TLS certs):

```bash
docker compose up -d        # stack + dashboard + one scorer run
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
| `SKIP_SCORED` | `1` | `0` = re-score everything in the window |

## Verify on first deploy

- Grafana's base volumes (`grafana_data`, base provisioning) survive the override
  merge — **confirmed** via `docker compose config`: Compose *concatenates*
  `grafana.volumes`, so the 2 base mounts + our 2 scoring mounts all render.
- Container egress to `api.anthropic.com:443` (host-level already returns 401).
- A first `--dry-run` job end-to-end (fetch → stub score → push), then check the
  `claude-code-scores` stream in Grafana before switching the judge on.
