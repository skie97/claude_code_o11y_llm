"""
Tests for the pure Loki push-payload builder.

The HTTP POST itself is the I/O edge and is not exercised here; only the
deterministic payload shaping is. Run: cd scripts && python -m unittest test_push_scores_to_loki
"""

import json
import unittest

from push_scores_to_loki import (
    HEALTH_STREAM,
    SCORES_STREAM,
    build_push_payload,
    prompt_ids_from_score_lines,
)


def _row(prompt_id: str = "p1", **overrides) -> dict:
    row = {
        "prompt_id": prompt_id, "email": "dev@example.com",
        "fit": "over_provisioned", "cost_usd": 0.20, "model_appropriateness": 0.75,
    }
    row.update(overrides)
    return row


class BuildPushPayloadTests(unittest.TestCase):
    def test_single_stream_labelled_for_scores(self):
        payload = build_push_payload([_row()], ts_ns=1000)
        self.assertEqual(len(payload["streams"]), 1)
        self.assertEqual(payload["streams"][0]["stream"], SCORES_STREAM)

    def test_one_json_value_per_row_preserving_fields(self):
        payload = build_push_payload([_row("a"), _row("b", fit="right_sized")], ts_ns=1000)
        values = payload["streams"][0]["values"]
        self.assertEqual(len(values), 2)
        self.assertEqual(json.loads(values[0][1])["prompt_id"], "a")
        self.assertEqual(json.loads(values[1][1])["fit"], "right_sized")

    def test_timestamps_are_strictly_ascending_to_avoid_collisions(self):
        payload = build_push_payload([_row("a"), _row("b"), _row("c")], ts_ns=1000)
        timestamps = [int(v[0]) for v in payload["streams"][0]["values"]]
        self.assertEqual(timestamps, [1000, 1001, 1002])

    def test_empty_rows_yield_no_streams(self):
        self.assertEqual(build_push_payload([], ts_ns=1000), {"streams": []})

    def test_stream_label_is_overridable_for_the_health_stream(self):
        payload = build_push_payload([_row()], ts_ns=1000, stream=HEALTH_STREAM)
        self.assertEqual(payload["streams"][0]["stream"], HEALTH_STREAM)
        self.assertEqual(HEALTH_STREAM, {"service_name": "claude-code-scorer-health"})


class PromptIdsFromScoreLinesTests(unittest.TestCase):
    """Dedup reader: the scores stream is the single source of 'already scored'.
    Round-trips the very lines build_push_payload would have written."""

    def test_extracts_prompt_ids_from_score_json_lines(self):
        payload = build_push_payload([_row("a"), _row("b")], ts_ns=1000)
        lines = [v[1] for v in payload["streams"][0]["values"]]
        self.assertEqual(prompt_ids_from_score_lines(lines), {"a", "b"})

    def test_ignores_lines_without_a_prompt_id(self):
        lines = [json.dumps({"email": "x@example.com"}), json.dumps({"prompt_id": "keep"})]
        self.assertEqual(prompt_ids_from_score_lines(lines), {"keep"})

    def test_ignores_malformed_json_lines(self):
        lines = ["not json at all", json.dumps({"prompt_id": "keep"}), ""]
        self.assertEqual(prompt_ids_from_score_lines(lines), {"keep"})

    def test_empty_input_yields_empty_set(self):
        self.assertEqual(prompt_ids_from_score_lines([]), set())


if __name__ == "__main__":
    unittest.main()
