"""
Pure project-attribution core.

Claude Code's logs carry no clean project/cwd field, and the request-body
`<env>` block (working directory + git repo) is truncated away. But the
surviving text -- prompts, tool inputs, file contents, responses -- is full of
the repo name (from GitHub file paths) and the product's deployment host. Those
identify the project deterministically, with no LLM call.

This module is the regex extraction + labelling only. Grouping text per session
and the I/O live in the adapter (describe_activity.py).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

# Fields whose text can carry a project signal (repo paths, hosts, file contents).
_TEXT_FIELDS = ("prompt", "body", "line")

# Repo name = the path segment right after a GitHub directory, on either slash
# style. Require >=3 chars to skip stray fragments like "GitHub/ab".
_REPO = re.compile(r"GitHub[\\/]+([A-Za-z0-9][A-Za-z0-9._-]{2,})")

# A product host like myapp.vercel.app or grafana.example.dev. The full
# subdomain is kept because it is what identifies the product.
_HOST = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:dev|io|app|net|com))\b", re.IGNORECASE)

# Hosts that appear in everyone's logs and identify no product -- infra, the API,
# package registries, schema namespaces. The match is by registrable suffix so
# api.github.com and raw.githubusercontent-style hosts are covered.
_NOISE_SUFFIXES = (
    "github.com", "anthropic.com", "example.com", "schema.org", "w3.org",
    "googleapis.com", "npmjs.com", "python.org", "vercel.com", "githubusercontent.com",
)


def extract_markers(text: str) -> tuple[Counter, Counter]:
    """Return (repo-name counts, product-host counts) found in text."""
    repos: Counter = Counter(_REPO.findall(text or ""))
    hosts: Counter = Counter(
        host.lower()
        for host in _HOST.findall(text or "")
        if not host.lower().endswith(_NOISE_SUFFIXES)
    )
    return repos, hosts


def project_label(repos: Counter, hosts: Counter) -> str:
    """The best single project identifier: repo name, else product host, else unknown."""
    if repos:
        return repos.most_common(1)[0][0]
    if hosts:
        return hosts.most_common(1)[0][0]
    return "unknown"


def project_by_session(records: list[dict]) -> dict[str, tuple[str, str]]:
    """Map session_id -> (project_label, evidence). One session ~= one project,
    so a label inferred from the whole session's text covers its terse prompts."""
    text: dict[str, list[str]] = defaultdict(list)
    for r in records:
        sid = r.get("session_id")
        if not sid:
            continue
        text[sid].extend(r[f] for f in _TEXT_FIELDS if r.get(f))

    result: dict[str, tuple[str, str]] = {}
    for sid, chunks in text.items():
        repos, hosts = extract_markers(" ".join(chunks))
        evidence = ", ".join(f"{name}:{n}" for name, n in (repos + hosts).most_common(3))
        result[sid] = (project_label(repos, hosts), evidence)
    return result


def session_of_prompt(records: list[dict]) -> dict[str, str]:
    """prompt_id -> session_id (recovered from records; build_pairs drops it)."""
    return {r["prompt_id"]: r.get("session_id", "")
            for r in records if r.get("prompt_id") and r.get("session_id")}
