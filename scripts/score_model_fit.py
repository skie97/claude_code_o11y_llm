"""
score_model_fit.py -- per-prompt task extraction + model-appropriateness scoring.

For each real developer prompt (reusing analyze.py's pairing and noise filter):
  1. gather the telemetry for its prompt_id (API fan-out, tool calls, tokens, cost),
  2. ask a judge to read the prompt + final response + telemetry and return what
     the task was, why, its complexity (1-5), and the model tier it warranted,
  3. score the model actually used against that recommendation (0..1, asymmetric)
     using the pure core in model_scoring.py.

Two judges are available:
  * the real Claude judge (default) -- needs `pip install anthropic` + ANTHROPIC_API_KEY,
  * a deterministic telemetry-based stub (`--dry-run`) -- no SDK, no key, runs offline.

Output: data/processed/model_scores.json + a console table. The score is the
candidate for a Grafana gauge claude_code_model_appropriateness{email,task_type,
recommended_model,actual_model} pushed alongside the handover's other metrics.

Usage:
  python scripts/score_model_fit.py --dry-run     # offline, deterministic
  python scripts/score_model_fit.py               # real Claude judge
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

# Reuse the loader, noise filter, response extraction, and pairing rules so the
# definition of "a real scorable prompt" lives in exactly one place.
from analyze import (
    METRICS_EVENT,
    PROCESSED,
    RAW,
    RESPONSE_BODY,
    build_pairs,
    load_records,
    to_float,
    to_int,
)
from model_scoring import (
    FIT_OVER,
    FIT_RIGHT_SIZED,
    FIT_UNDER,
    JudgeVerdict,
    UnknownModelError,
    assess_from_verdict,
)
from project_attribution import project_by_session, session_of_prompt

TOOL_DECISION = "tool_decision"
RATE_LIMIT_SECONDS = 0.5  # handover: don't hammer the API between judge calls
JUDGE_MODEL = "claude-opus-4-8"
HOUR_PAD_NS = 3600 * 1_000_000_000  # pad the --skip-scored query window an hour each side

SYSTEM_PROMPT = """\
You are evaluating how an AI coding assistant handled one developer prompt, to \
judge whether the model used was the right size for the task.

You are given the developer's prompt, the assistant's final response, and \
telemetry about how much work the turn took (API round-trips, tool calls, \
tokens, cost). Use the telemetry as evidence of complexity, not just the text.

Available models, cheapest to most capable:
- haiku  (claude-haiku-4-5,  $1/$5 per 1M)  -- simple, well-scoped, single-step work
- sonnet (claude-sonnet-4-6, $3/$15 per 1M) -- routine multi-step work
- opus   (claude-opus-4-8,   $5/$25 per 1M) -- hard, open-ended, high-stakes, or deep-reasoning work

Return:
- task: one sentence on what was actually done
- purpose: one sentence on the developer's underlying intent
- task_type: one of bugfix | refactor | feature | explanation | analysis | planning | trivial_edit | other
- complexity: integer 1 (trivial) to 5 (very complex), grounded in the telemetry
- recommended_model: haiku | sonnet | opus -- the cheapest tier that would do the job well
- reasoning: one sentence justifying the recommendation\
"""


@dataclass(frozen=True)
class ModelScore:
    prompt_id: str
    email: str
    project: str
    actual_model: str
    recommended_model: str
    complexity: int
    model_appropriateness: float
    fit: str                  # right_sized | over_provisioned | under_provisioned
    tier_gap: int             # signed: + over-provisioned, - under-provisioned
    # Work-weights: how much the model actually did for this prompt. Emitted raw
    # so the aggregator (Grafana) can choose which to weight rates by; cost_usd is
    # the headline weight (over-provisioning's harm is dollars). weighted_by_cost
    # is score*cost so a cost-weighted mean appropriateness is one division downstream.
    output_tokens: int
    cost_usd: float
    work_units: int           # api round-trips + tool calls
    weighted_by_cost: float
    task_type: str
    task: str
    purpose: str
    reasoning: str


def conversation_title_by_prompt(records: list[dict]) -> dict[str, str]:
    """Claude Code auto-generates a conversation title (a Haiku call) whose
    response body is `{"title": "..."}`. That is a free, already-paid-for task
    summary -- harvest it, keyed by prompt_id. Only topic-opening prompts get one."""
    titles: dict[str, str] = {}
    for r in records:
        if r.get("event_name") != RESPONSE_BODY or r.get("prompt_id") in titles:
            continue
        try:
            content = json.loads(r.get("body", "")).get("content", [])
        except (json.JSONDecodeError, AttributeError):
            continue
        for block in content:
            text = block.get("text", "").strip() if isinstance(block, dict) else ""
            if text.startswith("{") and '"title"' in text:
                try:
                    title = json.loads(text).get("title")
                except json.JSONDecodeError:
                    continue
                if title:
                    titles[r["prompt_id"]] = title
    return titles


def telemetry_by_prompt(records: list[dict]) -> dict[str, dict]:
    """Aggregate the per-prompt_id work signals the judge uses as complexity evidence."""
    tel: dict[str, dict] = defaultdict(
        lambda: {"api_calls": 0, "tool_calls": 0, "tools": set(),
                 "output_tokens": 0, "cost_usd": 0.0, "duration_ms": 0}
    )
    for r in records:
        pid = r.get("prompt_id")
        if not pid:
            continue
        bucket = tel[pid]
        event = r.get("event_name")
        if event == RESPONSE_BODY:
            bucket["api_calls"] += 1
        elif event == TOOL_DECISION:
            bucket["tool_calls"] += 1
            if r.get("tool_name"):
                bucket["tools"].add(r["tool_name"])
        elif event == METRICS_EVENT:
            bucket["output_tokens"] += to_int(r.get("output_tokens"))
            bucket["cost_usd"] += to_float(r.get("cost_usd"))
            bucket["duration_ms"] += to_int(r.get("duration_ms"))
    return tel


def _render_user_message(pair: dict, tel: dict) -> str:
    title_hint = f"Claude Code's own title for this turn: {pair['title']}\n\n" if pair.get("title") else ""
    return (
        f"{title_hint}"
        f"Prompt:\n{pair['prompt']}\n\n"
        f"Assistant final response:\n{pair['response'][:4000]}\n\n"
        f"Model actually used: {pair['model']}\n"
        f"Telemetry: api_round_trips={tel['api_calls']}, tool_calls={tel['tool_calls']}, "
        f"distinct_tools={sorted(tel['tools'])}, output_tokens={tel['output_tokens']}, "
        f"cost_usd={tel['cost_usd']:.4f}, duration_ms={tel['duration_ms']}"
    )


def make_claude_judge(thinking: bool = True):
    """Real judge: official Anthropic SDK with structured output. Imported lazily
    so the offline dry-run path needs neither the SDK nor an API key."""
    import anthropic
    from pydantic import BaseModel
    from typing import Literal

    class Verdict(BaseModel):
        task: str
        purpose: str
        task_type: str
        complexity: int  # clamped to 1..5 below; no schema range (structured outputs strip it)
        recommended_model: Literal["haiku", "sonnet", "opus"]
        reasoning: str

    client = anthropic.Anthropic()

    def judge(pair: dict, tel: dict) -> JudgeVerdict:
        kwargs = dict(
            model=JUDGE_MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _render_user_message(pair, tel)}],
            output_format=Verdict,
        )
        if thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        verdict = client.messages.parse(**kwargs).parsed_output
        return JudgeVerdict(
            task=verdict.task,
            purpose=verdict.purpose,
            task_type=verdict.task_type,
            complexity=max(1, min(5, verdict.complexity)),
            recommended_model=verdict.recommended_model,
            reasoning=verdict.reasoning,
        )

    return judge


def stub_judge(pair: dict, tel: dict) -> JudgeVerdict:
    """Deterministic offline judge: derives complexity from observed work alone.
    Useful as a no-API baseline and to validate the pipeline end-to-end."""
    api_calls, tool_calls = tel["api_calls"], tel["tool_calls"]
    if api_calls <= 1 and tool_calls == 0:
        complexity = 1
    elif api_calls <= 2 and tool_calls <= 2:
        complexity = 2
    elif api_calls <= 4:
        complexity = 3
    elif api_calls <= 8:
        complexity = 4
    else:
        complexity = 5
    recommended = {1: "haiku", 2: "sonnet", 3: "sonnet", 4: "opus", 5: "opus"}[complexity]
    # task: prefer Claude Code's own conversation title (a free, already-paid-for
    # Haiku summary); otherwise the prompt's first line. purpose: the developer's
    # own words -- the best intent proxy available without an LLM judge.
    task = pair.get("title") or pair["prompt"].strip().splitlines()[0][:80]
    return JudgeVerdict(
        task=task,
        purpose=pair["prompt"].strip()[:200],
        task_type="other",
        complexity=complexity,
        recommended_model=recommended,
        reasoning=f"telemetry only: {api_calls} api round-trips, {tool_calls} tool calls",
    )


def score_all(pairs: list[dict], telemetry: dict[str, dict], judge, *, rate_limit: float) -> list[ModelScore]:
    scores: list[ModelScore] = []
    for pair in pairs:
        tel = telemetry[pair["prompt_id"]]  # defaultdict: missing prompt_id yields an empty bucket
        try:
            verdict = judge(pair, tel)
            assessment = assess_from_verdict(verdict, pair["model"])
        except UnknownModelError as exc:
            print(f"  skip {pair['prompt_id']}: {exc}", file=sys.stderr)
            continue
        except Exception as exc:  # one bad prompt must never crash the loop (handover)
            print(f"  skip {pair['prompt_id']}: judge failed: {exc}", file=sys.stderr)
            continue
        cost_usd = round(tel["cost_usd"], 6)
        scores.append(ModelScore(
            prompt_id=pair["prompt_id"], email=pair["email"],
            project=pair.get("project", "unknown"), actual_model=pair["model"],
            recommended_model=verdict.recommended_model, complexity=verdict.complexity,
            model_appropriateness=assessment.score, fit=assessment.fit, tier_gap=assessment.tier_gap,
            output_tokens=tel["output_tokens"], cost_usd=cost_usd,
            work_units=tel["api_calls"] + tel["tool_calls"],
            weighted_by_cost=round(assessment.score * cost_usd, 6),
            task_type=verdict.task_type,
            task=verdict.task, purpose=verdict.purpose, reasoning=verdict.reasoning,
        ))
        if rate_limit:
            time.sleep(rate_limit)
    return scores


def print_report(scores: list[ModelScore]) -> None:
    print(f"\n-- model-appropriateness scores ({len(scores)}) --")
    for s in scores:
        print(f"  {s.model_appropriateness:.2f}  {s.fit:<18} [{s.email:<22}] {s.project:<22} "
              f"used={s.actual_model:<22} rec={s.recommended_model:<7} "
              f"cx={s.complexity}  {s.task[:44]!r}")
    by_dev = defaultdict(list)
    for s in scores:
        by_dev[s.email].append(s.model_appropriateness)
    print("\n-- mean appropriateness per developer --")
    for dev, vals in sorted(by_dev.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        print(f"  {dev:<24} {sum(vals) / len(vals):.3f}  (n={len(vals)})")
    print("\n-- fit breakdown per developer (counts; weighted rates are computed in Grafana) --")
    fit_by_dev: dict[str, Counter] = defaultdict(Counter)
    for s in scores:
        fit_by_dev[s.email][s.fit] += 1
    for dev, fits in sorted(fit_by_dev.items()):
        print(f"  {dev:<24} right={fits[FIT_RIGHT_SIZED]} "
              f"over={fits[FIT_OVER]} under={fits[FIT_UNDER]}")
    print("\n-- recommended vs actual tier --")
    for combo, n in Counter((s.recommended_model, s.actual_model) for s in scores).most_common():
        print(f"  rec={combo[0]:<7} actual={combo[1]:<22} {n}")


def drop_already_scored(pairs: list[dict], records: list[dict]) -> list[dict]:
    """Filter out pairs whose prompt_id is already in the claude-code-scores stream,
    so a re-run is idempotent and never re-pays the judge. The scores stream is the
    single source of truth (no side state file). Queried over the span of the loaded
    records, padded an hour each side. A failed query must not block the job: warn
    and score everything rather than crash."""
    import fetch_loki_http  # lazy: only the --skip-scored path needs urllib/Loki

    ts_values = [to_int(r.get("ts")) for r in records if r.get("ts")]
    if not pairs or not ts_values:
        return pairs
    lo, hi = min(ts_values) - HOUR_PAD_NS, max(ts_values) + HOUR_PAD_NS
    try:
        already = fetch_loki_http.scored_prompt_ids(lo, hi)
    except Exception as exc:  # Loki unreachable / query failed -> don't block the job
        print(f"[score] --skip-scored: could not read scores stream ({exc}); "
              f"scoring all {len(pairs)} prompts", file=sys.stderr)
        return pairs
    kept = [p for p in pairs if p["prompt_id"] not in already]
    print(f"[score] --skip-scored: {len(pairs) - len(kept)} already scored, "
          f"{len(kept)} to score")
    return kept


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="use the offline telemetry stub judge (no SDK / API key)")
    parser.add_argument("--no-thinking", action="store_true",
                        help="disable adaptive thinking on the Claude judge")
    parser.add_argument("--skip-scored", action="store_true",
                        help="skip prompt_ids already in the claude-code-scores stream "
                             "(idempotent re-runs; queries LOKI_QUERY_URL)")
    args = parser.parse_args()

    records = load_records(RAW)
    pairs = build_pairs(records)
    if args.skip_scored:
        pairs = drop_already_scored(pairs, records)
    telemetry = telemetry_by_prompt(records)
    titles = conversation_title_by_prompt(records)
    projects = project_by_session(records)
    sessions = session_of_prompt(records)
    for pair in pairs:
        pair["title"] = titles.get(pair["prompt_id"])
        pair["project"] = projects.get(sessions.get(pair["prompt_id"], ""), ("unknown", ""))[0]

    if args.dry_run:
        judge, rate_limit = stub_judge, 0.0
        print(f"[score] dry-run stub judge over {len(pairs)} prompts")
    else:
        judge, rate_limit = make_claude_judge(thinking=not args.no_thinking), RATE_LIMIT_SECONDS
        print(f"[score] Claude judge ({JUDGE_MODEL}) over {len(pairs)} prompts")

    scores = score_all(pairs, telemetry, judge, rate_limit=rate_limit)

    PROCESSED.mkdir(parents=True, exist_ok=True)
    out = PROCESSED / "model_scores.json"
    out.write_text(json.dumps([asdict(s) for s in scores], indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print_report(scores)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
