"""Coverage has to depend on what we ask the feed for, not on how often the
process happens to start.

One get_trades(limit=100) call spans whatever the last hundred trades span --
about two minutes. That was fine while the poller ran every 20 seconds on a
laptop. Under GitHub's scheduler, runs fell from ~92/day to ~6/day, so the
fleet looked at roughly 12 minutes out of every 1,440 and new bets fell from
21/day to 0-3/day. Nothing had failed; the bot had stopped looking. These
tests pin the paging that closes that gap, and the caps that stop it becoming
an unbounded pull.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src import experiment
from src.polymarket_client import PolymarketClient
from src.scorecard import TraderScorecard

# Anchored to real time on purpose: get_trades_since bounds its own lookback
# against time.time(), so a fixed epoch would put the entire synthetic feed
# behind the floor and every walk would stop after one page.
NOW = time.time()


def _feed(count, *, step=1.0, start=NOW):
    """Newest-first, one trade every `step` seconds going backwards."""
    return [{"transactionHash": f"0x{i:06x}", "timestamp": start - i * step,
             "price": "0.5", "size": "100"} for i in range(count)]


class FakeClient(PolymarketClient):
    """Serves a fixed feed through v2's cursor contract, counting requests.

    v2 has no offset: a cursor is opaque and the only way back. The fake
    encodes the read position in it, which is what the real one does too.
    """

    def __init__(self, feed, *, server_max_limit=None, stuck_cursor=False):
        self.feed = feed
        self.server_max_limit = server_max_limit
        self.stuck_cursor = stuck_cursor
        self.calls = []

    def _trades_page(self, *, limit=100, cursor=None, **kwargs):
        if self.server_max_limit is not None:
            limit = min(limit, self.server_max_limit)
        start = int(cursor) if cursor else 0
        self.calls.append((limit, start))
        page = self.feed[start:start + limit]
        nxt = start + len(page)
        has_more = nxt < len(self.feed)
        return page, ("0" if self.stuck_cursor else str(nxt)) if has_more else None, has_more


# -- the gap itself ----------------------------------------------------------
def test_a_short_gap_costs_a_single_request():
    """The continuous path must not regress into 40 calls every 20 seconds."""
    c = FakeClient(_feed(5000))
    trades, reached = c.get_trades_since(NOW - 30, page_size=500)
    assert reached
    assert len(c.calls) == 1


def test_a_long_gap_is_paged_until_it_is_covered():
    # Three hours of feed at one trade/second, asked for in 500s.
    c = FakeClient(_feed(12_000))
    trades, reached = c.get_trades_since(NOW - 3 * 3600, page_size=500, max_pages=40)
    assert reached
    stamps = [t["timestamp"] for t in trades]
    assert min(stamps) <= NOW - 3 * 3600, "did not reach back past the floor"
    # The whole window is present, not just the newest page.
    assert len(trades) > 10_000


def test_the_window_that_used_to_be_invisible_is_now_returned():
    """The concrete regression: a whale 40 minutes into a 3-hour gap."""
    feed = _feed(12_000)
    whale = feed[2400]                      # 40 minutes back
    _, reached = FakeClient(feed).get_trades_since(NOW - 60, page_size=500)
    assert reached
    old, _ = FakeClient(feed).get_trades_since(NOW - 3 * 3600, page_size=500)
    assert whale in old
    # ...and a single fixed page of 100 would still have missed it.
    assert whale not in feed[:100]


# -- the caps ----------------------------------------------------------------
def test_hitting_the_page_cap_reports_an_uncovered_gap():
    """Silence here would read as a quiet market rather than a blind spot."""
    c = FakeClient(_feed(50_000))
    trades, reached = c.get_trades_since(NOW - 10 * 3600, page_size=100, max_pages=3)
    assert reached is False
    assert len(c.calls) == 3


def test_a_cold_start_is_bounded_by_the_lookback_window():
    """since_ts=0 must not try to pull the entire history of the feed."""
    c = FakeClient(_feed(100_000))
    trades, reached = c.get_trades_since(0.0, page_size=500, max_pages=200,
                                         max_lookback_seconds=3600)
    assert reached
    # One trade per second, so an hour is ~3,600 of them, not 100,000.
    assert len(trades) < 5000


def test_an_exhausted_feed_counts_as_covered():
    c = FakeClient(_feed(120))
    trades, reached = c.get_trades_since(NOW - 10 * 3600, page_size=100)
    assert reached is True, "ran out of feed, not out of patience"
    assert len(trades) == 120


# -- paging mechanics --------------------------------------------------------
def test_the_walk_follows_the_cursor_and_skips_nothing():
    """A server-side limit lower than we asked for must not open a hole: the
    cursor, not our page_size, decides where the next page starts."""
    c = FakeClient(_feed(3000), server_max_limit=100)
    trades, reached = c.get_trades_since(NOW - 900, page_size=500, max_pages=40)
    assert reached
    assert [start for _, start in c.calls] == list(range(0, len(c.calls) * 100, 100))
    hashes = [t["transactionHash"] for t in trades]
    assert len(hashes) == len(set(hashes))
    assert hashes == [t["transactionHash"] for t in c.feed[:len(hashes)]], "a page was skipped"


def test_has_more_false_ends_the_walk_as_covered():
    """v2 states when the feed is exhausted, so there is nothing left to miss
    even if the floor was never reached."""
    c = FakeClient(_feed(250))
    trades, reached = c.get_trades_since(NOW - 10 * 3600, page_size=100, max_pages=40)
    assert reached is True
    assert len(trades) == 250
    assert len(c.calls) == 3


def test_a_trade_served_on_two_pages_is_returned_once():
    """Cursors are meant to be stable, but a feed that re-serves rows across
    page boundaries must not produce duplicate bets."""
    feed = _feed(1000)
    c = FakeClient(feed)
    def overlapping(*, limit=100, cursor=None, **k):
        start = int(cursor) if cursor else 0
        page = feed[max(0, start - 20):start + limit]
        nxt = start + limit
        return page, (str(nxt) if nxt < len(feed) else None), nxt < len(feed)
    c._trades_page = overlapping
    trades, _ = c.get_trades_since(NOW - 600, page_size=100, max_pages=20)
    hashes = [t["transactionHash"] for t in trades]
    assert len(hashes) == len(set(hashes))


def test_undated_trades_do_not_cause_an_endless_walk():
    c = FakeClient([{"transactionHash": f"0x{i}", "timestamp": None} for i in range(500)])
    trades, reached = c.get_trades_since(NOW - 3600, page_size=100, max_pages=40)
    assert reached
    assert len(c.calls) == 1


# -- the dedup window that makes a bigger pull safe --------------------------
def test_the_dedup_window_outlasts_one_cycles_pull():
    """If a hash can be evicted while the feed still lists the trade, the
    fleet bets the same whale twice."""
    worst_case = 500 * 40      # trades_page_size x trades_max_pages
    assert experiment.SEEN_HASHES_MAX > worst_case


def test_trimming_keeps_the_newest_hashes_not_an_arbitrary_set(tmp_path, monkeypatch):
    """seen_hashes was a set, and list(a_set)[-N:] keeps N arbitrary entries --
    so the trim could drop the most recent trades and re-process them."""
    monkeypatch.setattr(experiment, "SEEN_HASHES_MAX", 10)
    path = tmp_path / "state.json"
    seen = dict.fromkeys(f"0x{i:04x}" for i in range(100))
    experiment.save_state(str(path), [], TraderScorecard(), NOW, seen)

    _, restored, _ = experiment.load_state(str(path), [], TraderScorecard())
    assert len(restored) == 10
    assert list(restored) == [f"0x{i:04x}" for i in range(90, 100)]


def test_a_cursor_that_does_not_advance_stops_the_walk():
    """A cursor that keeps pointing at page one must cost two requests and an
    honest reached=False, not the whole page budget."""
    c = FakeClient(_feed(5000), stuck_cursor=True)
    trades, reached = c.get_trades_since(NOW - 3 * 3600, page_size=100, max_pages=40)
    assert reached is False, "silently claimed to have covered the gap"
    assert len(c.calls) == 2
    hashes = [t["transactionHash"] for t in trades]
    assert len(hashes) == len(set(hashes)) == 100
