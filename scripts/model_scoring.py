"""
Pure model-appropriateness scoring core.

Given a judge's verdict (the recommended model tier for a task) and the model
that was actually used, produce a 0..1 score for how right-sized the choice was.
The score is asymmetric: under-provisioning (too weak a model -- a quality risk)
is penalised harder than over-provisioning (too strong -- only a cost waste).

This module is deliberately free of any Claude/SDK or I/O dependency so the
scoring rule is the single, unit-testable source of truth. The LLM judge and
file handling live in the adapter (score_model_fit.py).
"""

from __future__ import annotations

from dataclasses import dataclass

# Capability ladder, cheapest/weakest first. Matched by family substring so that
# version suffixes (claude-haiku-4-5-20251001) and future point releases map
# without edits. Order matters only for readability; matching is by membership.
_TIER_BY_FAMILY: tuple[tuple[str, int], ...] = (
    ("haiku", 0),
    ("sonnet", 1),
    ("opus", 2),
)
_NAME_TO_TIER = {name: rank for name, rank in _TIER_BY_FAMILY}

# Asymmetric weights (per tier of mismatch), confirmed with the user:
# under-provisioning risks task failure; over-provisioning only wastes money.
UNDER_PENALTY = 0.5
OVER_PENALTY = 0.25

# Fit classes -- the human-readable verdict behind the score. These are emitted
# as a low-cardinality label on every score row, so downstream (Grafana) can
# count and weight by category. Magic strings live here once.
FIT_RIGHT_SIZED = "right_sized"
FIT_OVER = "over_provisioned"
FIT_UNDER = "under_provisioned"


class UnknownModelError(ValueError):
    """Raised when a model id cannot be mapped to a capability tier."""


@dataclass(frozen=True)
class JudgeVerdict:
    """The LLM judge's reading of one prompt/response turn.

    recommended_model is constrained to a tier name (haiku|sonnet|opus) at the
    adapter boundary via structured-output enum, so the scorer can trust it.
    """

    task: str
    purpose: str
    task_type: str
    complexity: int            # 1..5 (clamped at the boundary)
    recommended_model: str     # "haiku" | "sonnet" | "opus"
    reasoning: str


@dataclass(frozen=True)
class FitAssessment:
    """The three facts that always travel together for one prompt: the 0..1
    score, its categorical fit, and the signed capability gap behind both."""

    score: float
    fit: str          # FIT_RIGHT_SIZED | FIT_OVER | FIT_UNDER
    tier_gap: int


def tier_gap(recommended_tier: int, actual_tier: int) -> int:
    """Signed capability gap. Positive = a stronger model than needed
    (over-provisioned); negative = a weaker model than needed (under-provisioned).
    This is the one place the sign convention is defined; everything else derives
    over/under from it, so the direction can never drift between callers."""
    return actual_tier - recommended_tier


def fit_class(recommended_tier: int, actual_tier: int) -> str:
    """Categorise the choice as right-sized, over-, or under-provisioned."""
    gap = tier_gap(recommended_tier, actual_tier)
    if gap == 0:
        return FIT_RIGHT_SIZED
    return FIT_OVER if gap > 0 else FIT_UNDER


def model_tier(model_id: str) -> int | None:
    """Capability rank of a Claude model by family, or None if unrecognised."""
    name = (model_id or "").lower()
    for family, rank in _TIER_BY_FAMILY:
        if family in name:
            return rank
    return None


def appropriateness_score(
    recommended_tier: int,
    actual_tier: int,
    *,
    under_penalty: float = UNDER_PENALTY,
    over_penalty: float = OVER_PENALTY,
) -> float:
    """0..1 score for using actual_tier when recommended_tier was right-sized."""
    gap = actual_tier - recommended_tier
    if gap == 0:
        return 1.0
    penalty = (over_penalty if gap > 0 else under_penalty) * abs(gap)
    return max(0.0, min(1.0, 1.0 - penalty))


def assess_from_verdict(verdict: JudgeVerdict, actual_model: str) -> FitAssessment:
    """Resolve both tiers once and derive the score, fit, and gap together.

    Single point of tier resolution and the only place that raises for an
    unrecognised model, so score and fit can never disagree about the tiers."""
    actual_tier = model_tier(actual_model)
    if actual_tier is None:
        raise UnknownModelError(f"unrecognised model id: {actual_model!r}")
    recommended_tier = _NAME_TO_TIER[verdict.recommended_model]
    return FitAssessment(
        score=appropriateness_score(recommended_tier, actual_tier),
        fit=fit_class(recommended_tier, actual_tier),
        tier_gap=tier_gap(recommended_tier, actual_tier),
    )


def score_from_verdict(verdict: JudgeVerdict, actual_model: str) -> float:
    """Combine the judge's recommended tier with the model actually used."""
    return assess_from_verdict(verdict, actual_model).score
