"""
test_scorer_status.py -- unit tests for scorer run-health classification + the
run-status row.

These are the pure decisions behind the scorer's failure trigger: a bad JUDGE_MODEL
(or key) is a CONFIG error that fails identically for every prompt and must abort
the run, whereas a rate-limit or one malformed prompt is transient and skippable.
Conflating the two is exactly the silent-failure bug this guards against.
"""

from __future__ import annotations

import unittest

from scorer_status import (
    STATUS_FAILED,
    STATUS_OK,
    build_run_status,
    is_fatal_judge_error,
)


# Stand-ins for anthropic's exception types so the pure classifier stays import-free.
class _ApiErr(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class NotFoundError(Exception):
    pass


class AuthenticationError(Exception):
    pass


class IsFatalJudgeErrorTests(unittest.TestCase):
    def test_404_401_403_are_fatal_config_errors(self):
        for code in (401, 403, 404):
            self.assertTrue(is_fatal_judge_error(_ApiErr(code)), code)

    def test_429_and_5xx_are_transient_not_fatal(self):
        for code in (429, 500, 503, 529):
            self.assertFalse(is_fatal_judge_error(_ApiErr(code)), code)

    def test_fatal_by_class_name_when_no_status_code(self):
        self.assertTrue(is_fatal_judge_error(NotFoundError("model x")))
        self.assertTrue(is_fatal_judge_error(AuthenticationError("bad key")))

    def test_generic_exceptions_are_not_fatal(self):
        self.assertFalse(is_fatal_judge_error(ValueError("oops")))
        self.assertFalse(is_fatal_judge_error(Exception("???")))


class BuildRunStatusTests(unittest.TestCase):
    def test_ok_row_computes_total_and_omits_reason(self):
        row = build_run_status(status=STATUS_OK, judge_model="claude-sonnet-4-6",
                               scored=7, skipped=1, dry_run=False)
        self.assertEqual(row["kind"], "run_status")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["judge_model"], "claude-sonnet-4-6")
        self.assertEqual((row["scored"], row["skipped"], row["total"]), (7, 1, 8))
        self.assertFalse(row["dry_run"])
        self.assertNotIn("reason", row)

    def test_failed_row_includes_reason(self):
        row = build_run_status(status=STATUS_FAILED, judge_model="claude-bogus-9-9",
                               scored=0, skipped=41, dry_run=False,
                               reason="judge model not found")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["total"], 41)
        self.assertEqual(row["reason"], "judge model not found")

    def test_dry_run_flag_is_carried(self):
        row = build_run_status(status=STATUS_OK, judge_model="stub",
                               scored=3, skipped=0, dry_run=True)
        self.assertTrue(row["dry_run"])


if __name__ == "__main__":
    unittest.main()
