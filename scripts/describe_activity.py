"""
describe_activity.py -- for each developer prompt, state the project it belongs
to and what was being done. Deterministic; no LLM, no API key.

  project : inferred per session from repo names + product hosts in the session's
            surviving text (one session ~= one project), via project_attribution.
  task    : Claude Code's own conversation title where present (a free Haiku
            summary), else the prompt's first line.

Reuses analyze.py's loader + pairing and score_model_fit's title harvester so the
"real prompt" and "task" definitions stay single-sourced.

Usage:  python scripts/describe_activity.py
Output: data/processed/activity.json  + a console table.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass

from analyze import PROCESSED, RAW, build_pairs, load_records
from project_attribution import project_by_session, session_of_prompt
from score_model_fit import conversation_title_by_prompt


@dataclass(frozen=True)
class Activity:
    email: str
    session_id: str
    project: str
    project_evidence: str   # top markers behind the label, for auditability
    prompt_id: str
    task: str
    prompt: str


def describe(records: list[dict]) -> list[Activity]:
    pairs = build_pairs(records)
    titles = conversation_title_by_prompt(records)
    projects = project_by_session(records)
    sessions = session_of_prompt(records)

    activities: list[Activity] = []
    for pair in pairs:
        sid = sessions.get(pair["prompt_id"], "")
        label, evidence = projects.get(sid, ("unknown", ""))
        task = titles.get(pair["prompt_id"]) or pair["prompt"].strip().splitlines()[0][:80]
        activities.append(Activity(
            email=pair["email"], session_id=sid, project=label, project_evidence=evidence,
            prompt_id=pair["prompt_id"], task=task, prompt=pair["prompt"].strip()[:120],
        ))
    return activities


def main() -> None:
    records = load_records(RAW)
    activities = describe(records)

    PROCESSED.mkdir(parents=True, exist_ok=True)
    out = PROCESSED / "activity.json"
    out.write_text(json.dumps([asdict(a) for a in activities], indent=2, ensure_ascii=False),
                   encoding="utf-8")

    print(f"-- activity by prompt ({len(activities)}) --")
    for a in activities:
        print(f"  [{a.email:<22}] {a.project:<22} | {a.task}")
    print("\n-- prompts per project --")
    by_project: dict[str, int] = defaultdict(int)
    for a in activities:
        by_project[a.project] += 1
    for project, n in sorted(by_project.items(), key=lambda kv: -kv[1]):
        print(f"  {project:<24} {n}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
