"""Data API v1 was retired on 2 Nov 2026 and the fleet sat dead for two days.

The outage itself was unavoidable -- a vendor withdrew an endpoint. What made
it cost two days is that every layer absorbed it quietly: the cycle exited
non-zero, the workflow's `|| echo continuing` swallowed that, the four spot
experiments kept committing state on schedule, and from the outside a dead
Polymarket arm looks exactly like a quiet market.

v2 changed four things at once -- snake_case field names, a {data, pagination}
envelope, cursors instead of offset, and limit capped at 1000. Downstream code
speaks the v1 vocabulary, so the whole rename lives in one adapter. These pin
that adapter, because a silently-missing field reads as "no whales today".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src import polymarket_client as pc
from src.polymarket_client import PolymarketClient, _normalize_trade

# One real v2 row, trimmed of the fields nothing reads.
V2_ROW = {
    "proxy_wallet": "0x460a20d648c4601abfcd0c71ec37eb6dfd26d738",
    "side": "BUY",
    "token_id": "79741527247999470263157029650535081071426552161496336841006781664825222515188",
    "condition_id": "0x99dcf0abf02cdbd62e6a17a95c68f7864be5f5779d25d44cf7ea134f5d7979a9",
    "size": 29.0,
    "price": 0.12,
    "timestamp": 1793820666,
    "title": "Will Chuck Schumer be the next Senate Majority Leader?",
    "slug": "will-chuck-schumer-be-the-next-senate-majority-leader",
    "outcome": "No",
    "outcome_index": 1,
    "icon": "https://example.invalid/a.jpg",
    "bio": "",
    "profile_image": "",
    "pseudonym": "Dual-Cephalopod",
    "transaction_hash": "0x225cad2f198f66586854918bdefa7c803b370fd806eb7855583de2862316e7d3",
}

# Exactly what experiment.py, live.py and backtest.py read off a trade.
CONSUMED_FIELDS = ("transactionHash", "proxyWallet", "conditionId", "asset",
                   "timestamp", "price", "size")


def test_every_field_the_fleet_consumes_survives_the_rename():
    """The regression that would read as 'no whales today': a field that is
    silently None makes the trade fail validation and get skipped."""
    t = _normalize_trade(V2_ROW)
    for field in CONSUMED_FIELDS:
        assert t.get(field) is not None, f"{field} did not survive normalization"


def test_the_rename_maps_to_the_right_values():
    t = _normalize_trade(V2_ROW)
    assert t["proxyWallet"] == V2_ROW["proxy_wallet"]
    assert t["conditionId"] == V2_ROW["condition_id"]
    assert t["asset"] == V2_ROW["token_id"]          # the one that is not a pure rename
    assert t["transactionHash"] == V2_ROW["transaction_hash"]
    assert t["price"] == 0.12 and t["size"] == 29.0
    assert t["outcome"] == "No"


def test_the_usd_size_the_whale_threshold_uses_is_computable():
    t = _normalize_trade(V2_ROW)
    assert float(t["size"]) * float(t["price"]) == pytest.approx(3.48)


def test_title_now_arrives_with_the_trade():
    """v1 needed a separate Gamma lookup for this."""
    assert _normalize_trade(V2_ROW)["title"] == V2_ROW["title"]


def test_the_noisy_fields_are_dropped():
    """A cycle holds tens of thousands of rows; profile blurbs are not worth
    carrying through any of it."""
    t = _normalize_trade(V2_ROW)
    for junk in ("icon", "bio", "profile_image", "pseudonym"):
        assert junk not in t


def test_a_missing_field_becomes_none_rather_than_raising():
    """A KeyError mid-cycle would lose the whole batch; None lets the trade
    fail validation on its own and the rest proceed."""
    t = _normalize_trade({"price": 0.5})
    assert t["proxyWallet"] is None and t["transactionHash"] is None


# -- the envelope ------------------------------------------------------------
class _Client(PolymarketClient):
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def _fake_get(self, url, params=None):
        self.urls.append((url, params or {}))
        return self.payload


def _client(payload, monkeypatch):
    c = _Client(payload)
    monkeypatch.setattr(pc, "_get", c._fake_get)
    return c


def test_the_envelope_is_unwrapped_and_the_cursor_returned(monkeypatch):
    c = _client({"data": [V2_ROW], "pagination": {"next_cursor": "abc", "has_more": True}}, monkeypatch)
    rows, cursor, has_more = c._trades_page(limit=100)
    assert len(rows) == 1 and rows[0]["proxyWallet"] == V2_ROW["proxy_wallet"]
    assert cursor == "abc" and has_more is True


def test_requests_go_to_v2_not_the_retired_path(monkeypatch):
    c = _client({"data": [], "pagination": {}}, monkeypatch)
    c._trades_page(limit=100)
    url = c.urls[0][0]
    assert "/v2/trades" in url, f"still calling the retired endpoint: {url}"


def test_the_limit_is_clamped_to_what_v2_accepts(monkeypatch):
    """Asking for more than 1000 is a 400, which would kill the whole cycle."""
    c = _client({"data": [], "pagination": {}}, monkeypatch)
    c._trades_page(limit=5000)
    assert c.urls[0][1]["limit"] == PolymarketClient.MAX_TRADE_LIMIT == 1000


def test_offset_is_never_sent(monkeypatch):
    """v2 rejects it outright rather than ignoring it."""
    c = _client({"data": [], "pagination": {}}, monkeypatch)
    c._trades_page(limit=100, cursor="xyz")
    params = c.urls[0][1]
    assert "offset" not in params
    assert params["cursor"] == "xyz"


def test_a_bare_list_is_tolerated(monkeypatch):
    """If the envelope is ever dropped again, losing the cursor should cost
    coverage, not the cycle."""
    c = _client([V2_ROW], monkeypatch)
    rows, cursor, has_more = c._trades_page(limit=100)
    assert len(rows) == 1 and cursor is None and has_more is False


def test_an_unexpected_shape_yields_no_trades_rather_than_raising(monkeypatch):
    c = _client({"error": "Data API v1 has been retired"}, monkeypatch)
    rows, cursor, has_more = c._trades_page(limit=100)
    assert rows == [] and cursor is None and has_more is False


def test_get_trades_still_returns_a_plain_list(monkeypatch):
    """live.py and backtest.py call it and index straight into the result."""
    c = _client({"data": [V2_ROW, V2_ROW], "pagination": {"has_more": False}}, monkeypatch)
    out = c.get_trades(limit=2)
    assert isinstance(out, list) and len(out) == 2
