# Loki Data Analysis — Findings for the Scoring Pipeline

This is the factual basis for building the LLM-as-Judge scorer described in
`HANDOVER-scoring-pipeline.md`. The handover's assumptions about the Loki schema
are **materially wrong**; the scorer must be built against what's actually in the
data, documented below.

## How to reproduce

```bash
bash scripts/download_loki_data.sh    # host-side fetch over SSH -> data/raw/claude_code_logs.ndjson
python scripts/analyze.py             # report + data/processed/{prompt_response_pairs,summary}.json
```

Dataset analyzed: 483 entries, 8 sessions, 16 prompt_ids, 2 developers, spanning
~2h on 2026-06-05 (the stack is new; this is essentially all the data there is).

## Handover assumptions vs. reality

| Handover said | Reality in Loki |
|---|---|
| stream selector `{job="claudecode"}` | only label is `{service_name="claude-code"}`; **no** `job` label |
| `user.email`, `prompt.id` (dotted) | `user_email`, `prompt_id` (**underscores**) |
| `event_name` is a label | `event_name` is **structured metadata**, not a stream label (so `/labels` won't list it) |
| events: `user_prompt` / `api_request` / `api_response` | `user_prompt`, `api_request` (metrics only), `api_request_body` (full request), `api_response_body` (full response) + 7 others |
| pair prompt+response, prompt text via pairing | prompt text is delivered directly in `user_prompt.prompt` |
| `input_tokens`/`output_tokens` on the response | live on the `api_request` **metrics** event, not the body events |

### Correct LogQL (verified against live Loki)

Structured metadata is filtered with the `|` label-filter syntax — **no `| json`**:

```logql
{service_name="claude-code"} | event_name="user_prompt"
{service_name="claude-code"} | prompt_id="<uuid>"
```

## Event types (this dataset)

| event_name | count | has `body` | key fields |
|---|--:|:--:|---|
| `api_request_body` | 90 | ✅ | full request JSON (messages, system) |
| `api_request` | 89 | — | `input_tokens`, `output_tokens`, `cache_read_tokens`, `cost_usd`, `model`, `duration_ms` |
| `api_response_body` | 88 | ✅ | full assistant response JSON |
| `tool_decision` | 81 | — | `tool_name`, `decision` |
| `tool_result` | 78 | — | `tool_name`, `success`, sizes |
| `user_prompt` | 15 | — | **`prompt`** (raw text), `command_name`, `prompt_length` |
| `mcp_server_connection` | 15 | — | server/transport |
| `internal_error` | 13 | — | `error_name` |
| `plugin_loaded` | 7 | — | plugin metadata |
| `permission_mode_changed` | 6 | — | `from_mode`/`to_mode` |
| `auth` | 1 | — | `auth_method` |

Common to (almost) every event: `user_email`, `session_id`, `prompt_id`,
`event_sequence`, `event_timestamp`, `terminal_type`, `os_type`, scope/version.

## Key findings that shape the scorer

1. **Score the `user_prompt`, not API calls.** One `prompt_id` fans out to many
   API events — responses-per-prompt histogram: `{0:6, 2:1, 3:2, 4:1, 5:2, 6:1,
   7:1, 22:1, 31:1}` (max **31** `api_response_body` for a single prompt, the
   agent tool loop). The unit of developer intent is the `user_prompt`; the
   matching response for quality judgement is the **last** `api_response_body`
   for that `prompt_id`.

2. **Filter slash-command noise.** 5 of 15 `user_prompt` events are `/exit`,
   `/login`, `/doctor` etc. (`command_name` set, or text starts with `/`). They
   never hit the API (0 responses) and must be excluded from scoring.
   `is_real_prompt()` in `scripts/analyze.py` is the reusable rule.

3. **Thinking is redacted, text is intact.** 141 of 178 body events contain
   `<REDACTED>` thinking blocks; assistant `text` blocks are present. Extract
   `content[].type == "text"` and drop redacted/thinking — see `assistant_text()`.

4. **Token/cost come from `api_request`.** Per-developer rollup (this dataset):
   `dev-a@example.com` 83 reqs / $5.20 / 3.9M cache-read tokens;
   `dev-b@example.com` 6 reqs / $0.91. Cache-read dominates input — prompt
   caching is heavily in play.

5. **Two developers present** — enough to demonstrate the ranking dashboard.

## Loki query gotcha (critical for the scorer)

`body` (a full conversation, often tens of thousands of tokens) is part of the
**stream identity**. A wide `query_range` therefore ships many megabytes of
bodies through the querier→frontend gRPC channel and exceeds its message-size
limit — Loki logs `error notifying frontend about finished query` and the client
just times out. We can't retune Loki (don't-modify-the-stack constraint).

**The scorer must keep every response small client-side.** Two levers, both
proven in `scripts/download_loki_data.sh`:
- A summed histogram probe (`sum(count_over_time({service_name="claude-code"}[1h]))`)
  to locate populated windows cheaply (the `sum` collapses the giant labels).
- Adaptive time-window **halving** on timeout / limit-hit so a burst of huge
  bodies can't overflow one response.

For the scorer specifically, prefer fetching the small events:
`user_prompt` (prompt text, no body) and `api_request` (tokens/cost, no body),
and pull `api_response_body` **per `prompt_id` in a narrow time window** only for
the prompts being scored — never bulk-scan bodies.

## Sensitivity

The dump contains **real prompts and responses**, including an SSH command with a
host IP. Treat `data/` as sensitive: it is git-ignored alongside `*.pem`. Do not
commit or forward it.

## Outputs

- `data/raw/claude_code_logs.ndjson` — one flattened entry per line.
- `data/processed/prompt_response_pairs.json` — 8 cleaned prompt↔response pairs;
  the candidate scorer input shape (`prompt_id`, `email`, `model`, `prompt`,
  `response`, `timestamp_utc`).
- `data/processed/summary.json` — the report as data.
