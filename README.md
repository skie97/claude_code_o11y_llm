# Claude Code o11y — LLM-as-Judge scoring

Scoring logic that sits on top of a Claude Code observability stack (Caddy +
OpenTelemetry + Prometheus + Loki + Grafana). It reads the telemetry Claude Code
emits about real developer sessions and answers two questions per prompt:

1. **Model appropriateness** — was the model right-sized for the task? A `0.0–1.0`
   score, with under-provisioning penalised harder than over-provisioning.
2. **Project / task attribution** — which product/project the prompt belongs to,
   and what was being done — derived deterministically, with no LLM call.

The companion stack (the Docker/observability deployment) lives in a separate
repo; this repo is the **analysis and scoring brain**, built and tested in
isolation so the rules are a single, verifiable source of truth.

> The full data-schema findings — what the Loki events actually look like and the
> query gotchas that shape every script here — are in [`ANALYSIS.md`](./ANALYSIS.md).

---

## Model-appropriateness scoring

Models sit on a capability ladder, cheapest/weakest first:

| Tier | Family | Suited to |
|---|---|---|
| 0 | `haiku` | simple, well-scoped, single-step work |
| 1 | `sonnet` | routine multi-step work |
| 2 | `opus` | hard, open-ended, high-stakes, or deep-reasoning work |

A judge reads the prompt, the assistant's final response, and the turn's
telemetry (API round-trips, tool calls, tokens, cost) and returns the **cheapest
tier that would do the job well**. The score compares that recommendation against
the model actually used:

```
gap = actual_tier − recommended_tier
gap == 0           → 1.0                      (right-sized)
gap  > 0 (stronger) → 1 − 0.25 × gap          (over-provisioned — cost waste)
gap  < 0 (weaker)   → 1 − 0.50 × |gap|        (under-provisioned — quality risk)
```

The penalty is **asymmetric on purpose**: shipping too weak a model risks task
failure, which is worse than the cost waste of shipping too strong a one. So the
same one-tier mismatch scores `0.75` when over-provisioned but `0.50` when
under-provisioned. This is the interpretation behind the three fit classes:

| Score | Fit |
|---|---|
| `1.00` | right-sized |
| `< 1.00`, model stronger than needed | over-provisioned |
| `< 1.00`, model weaker than needed | under-provisioned |

The formula is a pure function in [`scripts/model_scoring.py`](./scripts/model_scoring.py)
— no I/O, no SDK — so it is the single unit-tested source of truth. The LLM
judgement and all I/O live in the adapter, [`scripts/score_model_fit.py`](./scripts/score_model_fit.py).

Two judges are available:

- **Claude judge** (default) — uses the Anthropic SDK with structured output.
  Needs `pip install anthropic` and `ANTHROPIC_API_KEY`.
- **Telemetry stub** (`--dry-run`) — derives complexity from observed work alone.
  No SDK, no key, fully offline; a deterministic baseline for validating the
  pipeline end-to-end.

### Score row fields

Each scored prompt produces one row (`ModelScore` in
[`scripts/score_model_fit.py`](./scripts/score_model_fit.py)), written to
`data/processed/model_scores.json` and pushed to Loki as a single JSON log line.
Only `service_name` is a Loki stream label; every field below rides in the line,
so Grafana parses with `| json` and the stream stays single-identity.

| Field | Type | Meaning |
|---|---|---|
| `prompt_id` | string | The `user_prompt` id — the unit of scoring, and the dedup/join key |
| `email` | string | Developer who issued the prompt |
| `project` | string | Attributed project (e.g. `ExampleApp`), or `unknown` |
| `actual_model` | string | Model actually used (e.g. `claude-opus-4-8`) |
| `recommended_model` | string | Cheapest tier that would do the job well — `haiku` \| `sonnet` \| `opus` |
| `complexity` | int | Judge's complexity rating, 1 (trivial) – 5 (very complex) |
| `model_appropriateness` | float | The 0–1 score (1.0 = right-sized; asymmetric penalty) |
| `fit` | string | `right_sized` \| `over_provisioned` \| `under_provisioned` |
| `tier_gap` | int | Signed capability gap (actual − recommended; + over, − under) |
| `output_tokens` | int | Work-weight — tokens the model generated for this prompt |
| `cost_usd` | float | Work-weight — USD cost of the turn (headline weight for rates) |
| `work_units` | int | Work-weight — API round-trips + tool calls |
| `weighted_by_cost` | float | `model_appropriateness × cost_usd` — pre-multiplied so a cost-weighted mean is one division in LogQL |
| `task_type` | string | `bugfix` \| `refactor` \| `feature` \| `explanation` \| `analysis` \| `planning` \| `trivial_edit` \| `other` |
| `task` | string | One line on what was done (Claude Code's own title if present, else the prompt's first line) |
| `purpose` | string | One line on the developer's underlying intent |
| `reasoning` | string | One line justifying the recommended tier |

The three **work-weight** fields exist so the aggregator can weight the
right-sized / over- / under-provisioned rates by spend (`cost_usd`) or volume
(`output_tokens` / `work_units`) rather than by raw prompt count.

## Project / task attribution

Claude Code's logs carry no clean project or working-directory field (the
request-body `<env>` block is truncated away). But the surviving text — prompts,
tool inputs, file contents, responses — is full of the **repo name** (from GitHub
file paths) and the **product's deployment host**. Those identify the project
deterministically.

Attribution is inferred **per session** (one session ≈ one project) and is pure
regex extraction + labelling in
[`scripts/project_attribution.py`](./scripts/project_attribution.py): repo name
wins, else product host, else `unknown`. Infra/registry hosts (github.com,
the API, package registries) are filtered as noise.

---

## Repository layout

```
scripts/
  download_loki_data.sh        host-side fetch over SSH → data/raw/*.ndjson
  analyze.py                   inventory + prompt/response pairing → data/processed/{pairs,summary}.json
  model_scoring.py             PURE core: capability tiers + asymmetric 0..1 score
  test_model_scoring.py        unit tests for the scoring core
  project_attribution.py       PURE-ish: marker extraction + per-session project label
  test_project_attribution.py  unit tests for attribution
  score_model_fit.py           adapter: telemetry + Claude judge (or --dry-run stub) → model_scores.json
  describe_activity.py         adapter: per-prompt {project, task} (deterministic) → activity.json
ANALYSIS.md                    data-schema findings + scorer design rationale
data/                          gitignored — raw dumps contain real prompts (sensitive)
```

## Requirements

- **Python 3.12+** (developed on 3.14). The analysis and the tests use the
  **standard library only** (`unittest`).
- **For the real Claude judge only:** `pip install anthropic` and an
  `ANTHROPIC_API_KEY` in the environment.

## Running it

```bash
# 1. Fetch the log dump from Loki (see "Fetching data" below for config)
bash scripts/download_loki_data.sh

# 2. Inventory + build cleaned prompt/response pairs
python scripts/analyze.py

# 3. Per-prompt project + task (deterministic, no API key)
python scripts/describe_activity.py

# 4. Model-appropriateness — offline stub, no key required
python scripts/score_model_fit.py --dry-run

# 4b. Model-appropriateness — real Claude judge (needs anthropic + key)
python scripts/score_model_fit.py

# Tests
cd scripts && python -m unittest test_model_scoring test_project_attribution
```

### Fetching data

`download_loki_data.sh` runs the query **on the remote host over SSH** — Loki
publishes no host port and is only reachable from the box via its container
bridge IP. Configure the SSH target with environment variables:

| Variable | Meaning | Default |
|---|---|---|
| `LOKI_SSH_HOST` | `user@host` of the box running Loki | **required** |
| `LOKI_SSH_KEY` | path to the SSH private key | `./loki-ssh-key.pem` |
| `START_NS` / `END_NS` | query window in nanoseconds | last 30 days |

```bash
LOKI_SSH_HOST=ubuntu@your-host LOKI_SSH_KEY=~/keys/loki.pem bash scripts/download_loki_data.sh
```

For convenience on a development machine you can drop these into a **gitignored**
`scripts/loki.env.local` (it is auto-sourced if present):

```bash
export LOKI_SSH_HOST="ubuntu@your-host"
export LOKI_SSH_KEY="$REPO_DIR/your-key.pem"
```

> **Why the fetch is not a simple paged loop:** every distinct `body` (a full
> Claude Code conversation) is part of the Loki stream identity, so a wide query
> overflows the querier→frontend gRPC message-size limit and times out. The
> script works around this client-side — a summed-histogram probe to find
> populated hours, then adaptive time-window halving on timeout. See
> [`ANALYSIS.md`](./ANALYSIS.md) for the full reasoning.

---

## Roadmap

The scoring core and the offline pipeline run today. Still to come:

- Run the **real Claude judge** at scale and compare its semantic complexity
  reading against the telemetry stub.
- Emit **one score row per prompt back into Loki** and aggregate in **Grafana**
  (counts and *work-weighted* right-sized / over-provisioned / under-provisioned
  rates per developer and per project) — including a weekly leaderboard.
- Package the scorer as a scheduled service alongside the observability stack.

## Privacy

The downloaded dump contains **real prompts and responses** and is treated as
sensitive: `data/` and `*.pem` are gitignored and must never be committed.
