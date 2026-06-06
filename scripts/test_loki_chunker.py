"""
test_loki_chunker.py -- unit tests for the shared Loki fetch core.

The chunker is the single source of the histogram-probe + adaptive-halving
algorithm that both the SSH download (download_loki_data.sh) and the in-cluster
HTTP fetch (fetch_loki_http.py) rely on. Only its decision logic lives here; the
HTTP transport is injected, so every test runs offline with a fake query_range.
"""

from __future__ import annotations

import unittest

from loki_chunker import (
    NS,
    QueryTooLarge,
    dedup_key,
    fetch_window,
    histogram_hours,
    total_count,
)


class HistogramHoursTests(unittest.TestCase):
    def test_should_return_sorted_unique_hour_starts_in_ns_for_nonzero_buckets(self):
        # Loki sum(count_over_time(...)) result: one series, [ts_seconds, count].
        result = [{"values": [["7200", "3"], ["3600", "1"], ["10800", "2"]]}]
        self.assertEqual(
            histogram_hours(result),
            [3600 * NS, 7200 * NS, 10800 * NS],
        )

    def test_should_ignore_zero_count_buckets(self):
        result = [{"values": [["3600", "0"], ["7200", "5"]]}]
        self.assertEqual(histogram_hours(result), [7200 * NS])

    def test_should_dedupe_hours_across_series(self):
        result = [
            {"values": [["3600", "1"]]},
            {"values": [["3600", "2"], ["7200", "1"]]},
        ]
        self.assertEqual(histogram_hours(result), [3600 * NS, 7200 * NS])

    def test_should_return_empty_for_no_results(self):
        self.assertEqual(histogram_hours([]), [])


class TotalCountTests(unittest.TestCase):
    def test_should_sum_all_bucket_values_in_a_series(self):
        result = [{"values": [["3600", "3"], ["7200", "1"]]}]
        self.assertEqual(total_count(result), 4)

    def test_should_sum_across_multiple_series(self):
        result = [
            {"values": [["3600", "2"]]},
            {"values": [["3600", "5"], ["7200", "1"]]},
        ]
        self.assertEqual(total_count(result), 8)

    def test_should_return_zero_for_no_results(self):
        self.assertEqual(total_count([]), 0)


class DedupKeyTests(unittest.TestCase):
    def test_should_key_on_session_sequence_and_ts(self):
        meta = {"session_id": "s1", "event_sequence": "42", "model": "opus"}
        self.assertEqual(dedup_key(meta, 1234), ("s1", "42", 1234))

    def test_should_tolerate_missing_fields(self):
        self.assertEqual(dedup_key({}, 9), (None, None, 9))


def _entry(ts, seq):
    """A fetchable entry: (ts, stream_meta, raw_line)."""
    return (ts, {"session_id": "s", "event_sequence": str(seq)}, f"line{seq}")


class FetchWindowTests(unittest.TestCase):
    def test_should_yield_all_entries_when_window_fits(self):
        universe = [_entry(5, 0), _entry(15, 1), _entry(25, 2)]

        def transport(lo, hi, limit):
            return [e for e in universe if lo <= e[0] < hi][:limit]

        out = list(fetch_window(0, 64, transport, limit=10, floor_ns=1))
        self.assertEqual([ts for ts, _, _ in out], [5, 15, 25])

    def test_should_subdivide_when_limit_hit_and_collect_all(self):
        # limit=2 forces subdivision until each sub-window holds < limit entries.
        universe = [_entry(t, i) for i, t in enumerate([5, 15, 25, 35, 55])]

        def transport(lo, hi, limit):
            hits = sorted(e for e in universe if lo <= e[0] < hi)
            return hits[:limit]  # Loki caps the response at `limit`

        out = list(fetch_window(0, 64, transport, limit=2, floor_ns=1))
        self.assertEqual(sorted(ts for ts, _, _ in out), [5, 15, 25, 35, 55])

    def test_should_subdivide_on_query_too_large(self):
        universe = [_entry(t, i) for i, t in enumerate([1, 2, 5, 6])]

        def transport(lo, hi, limit):
            if hi - lo > 2:  # transport refuses anything wider than 2ns
                raise QueryTooLarge("too big")
            return [e for e in universe if lo <= e[0] < hi][:limit]

        out = list(fetch_window(0, 8, transport, limit=10, floor_ns=1))
        self.assertEqual(sorted(ts for ts, _, _ in out), [1, 2, 5, 6])

    def test_should_skip_window_at_floor_that_still_fails(self):
        skips = []

        def transport(lo, hi, limit):
            raise QueryTooLarge("always")

        out = list(
            fetch_window(0, 4, transport, limit=10, floor_ns=1,
                         on_skip=lambda lo, hi: skips.append((lo, hi)))
        )
        self.assertEqual(out, [])
        self.assertTrue(skips)                       # gave up rather than recurse forever
        self.assertTrue(all(hi - lo <= 1 for lo, hi in skips))

    def test_should_truncate_rather_than_recurse_below_floor_on_limit_hit(self):
        # A floor-width window that still hits the entry limit must yield what it
        # got (truncated), never recurse forever.
        entries = [_entry(0, 0), _entry(0, 1)]

        def transport(lo, hi, limit):
            return entries[:limit]

        out = list(fetch_window(0, 1, transport, limit=2, floor_ns=1))
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
