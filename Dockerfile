from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.indicators import build_row


def make_bars(count: int = 260) -> list[dict]:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = []
    for index in range(count):
        close = 100.0 + index
        bars.append(
            {
                "t": (start + timedelta(days=index)).isoformat(),
                "o": close - 1,
                "h": close + 2,
                "l": close - 2,
                "c": close,
                "v": 1_000_000 + index * 1000,
            }
        )
    return bars


def test_build_row_contains_required_metrics() -> None:
    asset = {
        "symbol": "TEST",
        "name": "Test Corp",
        "exchange": "NASDAQ",
        "class": "us_equity",
        "marginable": True,
        "shortable": True,
        "easy_to_borrow": True,
        "fractionable": True,
        "attributes": ["has_options", "overnight_tradable"],
    }
    snapshot = {"latestTrade": {"p": 361.0, "t": "2026-01-02T20:00:00Z"}}
    fundamentals = {"float_shares": 10_000_000, "shares_outstanding": 12_000_000}

    row = build_row(asset, make_bars(), snapshot, fundamentals)

    assert row["symbol"] == "TEST"
    assert row["price"] == 361.0
    assert row["close"] == 359.0
    assert row["sma50"] == pytest.approx(sum(range(310, 360)) / 50)
    assert row["sma200"] == pytest.approx(sum(range(160, 360)) / 200)
    assert row["dollar_vol_m"] > 0
    assert row["pos_52w"] is not None
    assert row["float_turnover_pct"] > 0
    assert row["options_enabled"] is True
    assert row["data_status"] == "ok"


def test_no_bars_is_reported() -> None:
    asset = {"symbol": "NONE", "class": "us_equity", "attributes": []}
    row = build_row(asset, [], None, {})
    assert row["symbol"] == "NONE"
    assert row["data_status"] == "no_daily_bars"


def test_intraday_daily_bar_is_excluded() -> None:
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)  # 11:00 ET
    bars = make_bars(205)
    bars[-1]["t"] = "2026-08-05T04:00:00Z"
    bars[-1]["c"] = 999.0
    asset = {"symbol": "LIVE", "class": "us_equity", "attributes": []}

    row = build_row(asset, bars, None, {}, now=now)

    assert row["close"] != 999.0
    assert row["data_status"] == "ok"
