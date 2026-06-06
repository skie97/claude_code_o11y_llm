"""
Tests for the pure model-appropriateness scoring core.

These cover the deterministic logic only -- model-tier mapping and the
gap-based 0..1 score. The LLM judge and NDJSON I/O are adapters and are not
exercised here. Run with: python -m unittest scripts.test_model_scoring
(or: cd scripts && python -m unittest test_model_scoring)
"""

import unittest

from model_scoring import (
    FIT_OVER,
    FIT_RIGHT_SIZED,
    FIT_UNDER,
    JudgeVerdict,
    UnknownModelError,
    appropriateness_score,
    assess_from_verdict,
    fit_class,
    model_tier,
    score_from_verdict,
    tier_gap,
)


class ModelTierTests(unittest.TestCase):
    def test_maps_each_family_to_its_capability_rank(self):
        self.assertEqual(model_tier("claude-haiku-4-5"), 0)
        self.assertEqual(model_tier("claude-sonnet-4-6"), 1)
        self.assertEqual(model_tier("claude-opus-4-8"), 2)

    def test_ignores_version_suffix_and_case(self):
        self.assertEqual(model_tier("claude-haiku-4-5-20251001"), 0)
        self.assertEqual(model_tier("CLAUDE-OPUS-4-8"), 2)

    def test_returns_none_for_unknown_model(self):
        self.assertIsNone(model_tier("gpt-4o"))
        self.assertIsNone(model_tier(""))


class AppropriatenessScoreTests(unittest.TestCase):
    def test_exact_match_scores_full(self):
        self.assertEqual(appropriateness_score(recommended_tier=2, actual_tier=2), 1.0)

    def test_over_provisioning_is_a_mild_penalty(self):
        self.assertEqual(appropriateness_score(recommended_tier=1, actual_tier=2), 0.75)
        self.assertEqual(appropriateness_score(recommended_tier=0, actual_tier=2), 0.50)

    def test_under_provisioning_is_penalised_harder(self):
        self.assertEqual(appropriateness_score(recommended_tier=1, actual_tier=0), 0.50)
        self.assertEqual(appropriateness_score(recommended_tier=2, actual_tier=0), 0.0)

    def test_score_never_goes_below_zero(self):
        # Hypothetical 3-tier under-shoot with a harsher weight still clamps at 0.
        self.assertEqual(
            appropriateness_score(recommended_tier=2, actual_tier=0, under_penalty=0.6),
            0.0,
        )


class TierGapTests(unittest.TestCase):
    def test_positive_when_actual_stronger_than_recommended(self):
        self.assertEqual(tier_gap(recommended_tier=1, actual_tier=2), 1)

    def test_negative_when_actual_weaker_than_recommended(self):
        self.assertEqual(tier_gap(recommended_tier=2, actual_tier=0), -2)

    def test_zero_when_matched(self):
        self.assertEqual(tier_gap(recommended_tier=1, actual_tier=1), 0)


class FitClassTests(unittest.TestCase):
    def test_right_sized_when_tiers_match(self):
        self.assertEqual(fit_class(recommended_tier=2, actual_tier=2), FIT_RIGHT_SIZED)

    def test_over_provisioned_when_actual_stronger(self):
        self.assertEqual(fit_class(recommended_tier=0, actual_tier=2), FIT_OVER)

    def test_under_provisioned_when_actual_weaker(self):
        self.assertEqual(fit_class(recommended_tier=2, actual_tier=1), FIT_UNDER)


class AssessFromVerdictTests(unittest.TestCase):
    def _verdict(self, recommended_model: str) -> JudgeVerdict:
        return JudgeVerdict(
            task="t", purpose="p", task_type="bugfix",
            complexity=3, recommended_model=recommended_model, reasoning="r",
        )

    def test_bundles_score_fit_and_gap_for_over_provisioning(self):
        assessment = assess_from_verdict(self._verdict("sonnet"), "claude-opus-4-8")
        self.assertEqual(assessment.score, 0.75)
        self.assertEqual(assessment.fit, FIT_OVER)
        self.assertEqual(assessment.tier_gap, 1)

    def test_bundles_score_fit_and_gap_for_under_provisioning(self):
        assessment = assess_from_verdict(self._verdict("opus"), "claude-haiku-4-5")
        self.assertEqual(assessment.score, 0.0)
        self.assertEqual(assessment.fit, FIT_UNDER)
        self.assertEqual(assessment.tier_gap, -2)

    def test_right_sized_assessment(self):
        assessment = assess_from_verdict(self._verdict("opus"), "claude-opus-4-8")
        self.assertEqual(assessment.score, 1.0)
        self.assertEqual(assessment.fit, FIT_RIGHT_SIZED)
        self.assertEqual(assessment.tier_gap, 0)

    def test_unknown_actual_model_raises(self):
        with self.assertRaises(UnknownModelError):
            assess_from_verdict(self._verdict("opus"), "mistral-large")


class ScoreFromVerdictTests(unittest.TestCase):
    def _verdict(self, recommended_model: str) -> JudgeVerdict:
        return JudgeVerdict(
            task="t", purpose="p", task_type="bugfix",
            complexity=3, recommended_model=recommended_model, reasoning="r",
        )

    def test_combines_recommended_name_with_actual_model_id(self):
        self.assertEqual(score_from_verdict(self._verdict("sonnet"), "claude-opus-4-8"), 0.75)
        self.assertEqual(score_from_verdict(self._verdict("opus"), "claude-opus-4-8"), 1.0)
        self.assertEqual(score_from_verdict(self._verdict("opus"), "claude-haiku-4-5"), 0.0)

    def test_unknown_actual_model_raises(self):
        with self.assertRaises(UnknownModelError):
            score_from_verdict(self._verdict("opus"), "mistral-large")


if __name__ == "__main__":
    unittest.main()
