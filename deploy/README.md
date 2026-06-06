# Scorer deploy (private compose override)

Layers the LLM-as-Judge scoring stage onto the **public** observability stack
(`../claude_code_o11y`) without editing it. Everything here stays in this repo.

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

This repo must be cloned as a **sibling of the stack repo, named exactly
`claude_code_o11y_llm`** — the override's `../claude_code_o11y_llm/...` paths
resolve against the stack's compose dir, so only *our* dir name matters (the stack
dir can be named anything, e.g. `observability`). `$STACK` below is that stack dir.

```bash
# 1. config the scorer
cd .../claude_code_o11y_llm/deploy && cp .env.example .env   # set ANTHROPIC_API_KEY

# 2. from the stack dir, FIRST render the merged config (read-only) and eyeball that
#    grafana keeps its base volumes (grafana_data + provisioning) PLUS our 2 mounts:
cd "$STACK"
docker compose -f docker-compose.yml \
  -f ../claude_code_o11y_llm/deploy/docker-compose.scorer.yml config | less

# 3. apply: builds the scorer image, recreates grafana with the scoring dashboard
#    (brief grafana blip), leaves the other services untouched.
docker compose -f docker-compose.yml \
  -f ../claude_code_o11y_llm/deploy/docker-compose.scorer.yml up -d --build

# 4. confirm the dashboard provider landed inside the nested provisioning mount:
docker exec grafana ls /etc/grafana/provisioning/dashboards/
```

## Run the job

```bash
# from the stack dir ($STACK), same -f pair. First time, prove connectivity offline:
docker compose -f docker-compose.yml \
  -f ../claude_code_o11y_llm/deploy/docker-compose.scorer.yml \
  run --rm -e SCORER_ARGS=--dry-run scorer        # fetch -> stub score -> push, no API key

# then the real judge:
docker compose -f docker-compose.yml \
  -f ../claude_code_o11y_llm/deploy/docker-compose.scorer.yml \
  run --rm scorer
```

The job preflights Loki connectivity (`--probe`) and aborts cleanly if
`loki:3100` is unreachable, before doing any work.

Schedule it with host cron (the container is a job, not a daemon). Re-runs are
safe: `--skip-scored` reads the scores stream and only scores prompts not already
there, so cron can fire as often as you like.

## Knobs (`.env`)

| var | default | meaning |
|-----|---------|---------|
| `ANTHROPIC_API_KEY` | — | judge key; omit only with `SCORER_ARGS=--dry-run` |
| `LOKI_PUSH_URL` / `LOKI_QUERY_URL` | `http://loki:3100` | one host; query falls back to push |
| `SCORER_ARGS` | — | `--dry-run` uses the offline stub (no key) |
| `FETCH` | `1` | `0` = skip HTTP fetch, score a pre-mounted `data/raw` |
| `SKIP_SCORED` | `1` | `0` = re-score everything in the window |

## Verify on first deploy

- Grafana's base volumes (`grafana_data`, base provisioning) survive the override
  merge — the override only *appends* the scoring dashboard mounts.
- Container egress to `api.anthropic.com:443` (host-level already returns 401).
- A first `--dry-run` job end-to-end (fetch → stub score → push), then check the
  `claude-code-scores` stream in Grafana before switching the judge on.
