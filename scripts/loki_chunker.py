"""
loki_chunker.py -- the shared, transport-agnostic core for fetching Claude Code
logs out of Loki without overflowing it.

Why this module exists: every distinct `body` (a full conversation) is part of the
Loki stream identity, so a wide query_range tries to ship many megabytes back
through the querier->frontend gRPC channel and blows its message-size limit -- the
client just times out. We can't retune the stack (don't-modify constraint), so we
keep every response small from the client side with two moves:

  1. populated_hours -- a cheap summed histogram (sum(count_over_time(...)) collapses
     the giant labels) tells us which hours actually hold data; retention is mostly
     empty, so this skips almost everything.
  2. fetch_window -- adaptive halving: any window that times out OR returns a full
     `limit` page is split in two and retried, so a burst of huge bodies can never
     overflow a single response.

The *decision logic* (when to subdivide, the 1s floor, the dedup key, the histogram
parse) lives here and nothing else does -- the SSH download (download_loki_data.sh,
a remote python heredoc that can't import this) is the one irreducible copy. The
in-cluster HTTP fetch (fetch_loki_http.py) injects a urllib transport and reuses
this verbatim. Pure: no I/O, fully unit-tested with a fake query_range.
"""

from __future__ import annotations

from typing import Callable, Iterator, Optional

NS = 1_000_000_000
HOUR_NS = 3600 * NS
DEFAULT_LIMIT = 5000          # Loki's max entries per query_range response

# Stream selectors. Both streams carry only `service_name` as a label.
CLAUDE_CODE_SELECTOR = '{service_name="claude-code"}'
SCORES_SELECTOR = '{service_name="claude-code-scores"}'

# A fetched entry: (timestamp_ns, stream_metadata, raw_log_line).
Entry = tuple
# query_range(lo_ns, hi_ns, limit) -> list[Entry]; raises QueryTooLarge to subdivide.
QueryRange = Callable[[int, int, int], list]


class QueryTooLarge(Exception):
    """A transport raises this when a window cannot be returned (timeout / gRPC
    overflow). It is the signal -- not an error -- that tells fetch_window to halve
    the window and retry. Transports translate their own timeouts into this."""


def histogram_hours(result: list[dict]) -> list[int]:
    """Parse the `data.result` of a sum(count_over_time(...)) query into the sorted,
    unique hour-start timestamps (ns) that hold at least one entry."""
    hours: set[int] = set()
    for series in result:
        for ts_seconds, count in series.get("values", []):
            if float(count) > 0:
                hours.add(int(float(ts_seconds)) * NS)
    return sorted(hours)


def total_count(result: list[dict]) -> int:
    """Sum every bucket of a sum(count_over_time(...)) matrix -- the total entries
    in the queried window. Used by the connectivity probe to report 'N entries
    visible' as proof the query path works, not just that Loki is up."""
    return sum(
        int(float(count))
        for series in result
        for _ts, count in series.get("values", [])
    )


def dedup_key(meta: dict, ts: int) -> tuple:
    """Identity of an entry, used to guard against the same row arriving from two
    overlapping windows. Windows are half-open so overlap shouldn't happen, but a
    cheap belt-and-suspenders guard is worth keeping."""
    return (meta.get("session_id"), meta.get("event_sequence"), ts)


def _subdivide(lo: int, hi: int) -> tuple[tuple[int, int], tuple[int, int]]:
    mid = lo + (hi - lo) // 2
    return (lo, mid), (mid, hi)


def fetch_window(
    lo: int,
    hi: int,
    query_range: QueryRange,
    *,
    limit: int = DEFAULT_LIMIT,
    floor_ns: int = NS,
    on_skip: Optional[Callable[[int, int], None]] = None,
) -> Iterator[tuple]:
    """Yield every entry in the half-open window [lo, hi), in ascending ts.

    Subdivides on either failure mode:
      * the transport raises QueryTooLarge (window too big to return), or
      * the response is a full `limit` page (so entries were almost certainly
        dropped -- the window is too coarse).
    The 1s floor (floor_ns) stops the recursion: a window at or below the floor
    that still fails is reported via on_skip and dropped; one that merely hits the
    limit is yielded truncated (matching the SSH downloader -- a sub-second burst
    of >limit entries is not worth splitting further)."""
    try:
        entries = query_range(lo, hi, limit)
    except QueryTooLarge:
        if hi - lo <= floor_ns:
            if on_skip is not None:
                on_skip(lo, hi)
            return
        (a, b), (c, d) = _subdivide(lo, hi)
        yield from fetch_window(a, b, query_range, limit=limit, floor_ns=floor_ns, on_skip=on_skip)
        yield from fetch_window(c, d, query_range, limit=limit, floor_ns=floor_ns, on_skip=on_skip)
        return

    if len(entries) >= limit and hi - lo > floor_ns:
        (a, b), (c, d) = _subdivide(lo, hi)
        yield from fetch_window(a, b, query_range, limit=limit, floor_ns=floor_ns, on_skip=on_skip)
        yield from fetch_window(c, d, query_range, limit=limit, floor_ns=floor_ns, on_skip=on_skip)
        return

    yield from sorted(entries, key=lambda e: e[0])
