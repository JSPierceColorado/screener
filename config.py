from __future__ import annotations

import math
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from typing import Any

import numpy as np
import pandas as pd


OUTPUT_COLUMNS = [
    "symbol",
    "name",
    "exchange",
    "asset_class",
    "price",
    "price_timestamp",
    "close",
    "close_date",
    "prev_close",
    "close_change_pct",
    "live_vs_close_pct",
    "volume",
    "avg_volume_20d",
    "avg_volume_50d",
    "relative_volume",
    "dollar_vol_m",
    "today_dollar_vol_m",
    "sma20",
    "sma50",
    "sma200",
    "price_vs_sma50_pct",
    "price_vs_sma200_pct",
    "week52_low",
    "week52_high",
    "pos_52w",
    "atr14",
    "atr_pct",
    "rsi14",
    "volatility_20d_pct",
    "shares_outstanding",
    "float_shares",
    "float_turnover_pct",
    "marginable",
    "shortable",
    "easy_to_borrow",
    "fractionable",
    "options_enabled",
    "overnight_tradable",
    "data_status",
]


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return (numerator / denominator - 1.0) * 100.0


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.iloc[-1]
    avg_gain = gain.iloc[-1]
    if pd.isna(avg_loss) or pd.isna(avg_gain):
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def _atr(frame: pd.DataFrame, period: int = 14) -> float | None:
    if len(frame) < period + 1:
        return None
    prev_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    return None if pd.isna(atr) else float(atr)


def bars_to_frame(bars: list[dict[str, Any]]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    frame = pd.DataFrame(bars).rename(
        columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"Bar response missing columns: {missing}")

    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=required).sort_values("timestamp")
    frame = frame.drop_duplicates(subset=["timestamp"], keep="last")
    return frame.reset_index(drop=True)


def build_row(
    asset: dict[str, Any],
    bars: list[dict[str, Any]],
    snapshot: dict[str, Any] | None,
    fundamentals: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    symbol = asset.get("symbol", "")
    fundamentals = fundamentals or {}
    frame = bars_to_frame(bars)
    now = now or datetime.now(timezone.utc)
    market_now = now.astimezone(ZoneInfo("America/New_York"))
    if not frame.empty:
        latest_market_date = frame["timestamp"].iloc[-1].tz_convert("America/New_York").date()
        # During a live session, Alpaca may include the still-forming daily bar.
        if latest_market_date == market_now.date() and market_now.time() < time(16, 15):
            frame = frame.iloc[:-1].reset_index(drop=True)

    attributes = set(asset.get("attributes") or [])
    base: dict[str, Any] = {
        "symbol": symbol,
        "name": asset.get("name", ""),
        "exchange": asset.get("exchange", ""),
        "asset_class": asset.get("class", ""),
        "marginable": bool(asset.get("marginable", False)),
        "shortable": bool(asset.get("shortable", False)),
        "easy_to_borrow": bool(asset.get("easy_to_borrow", False)),
        "fractionable": bool(asset.get("fractionable", False)),
        "options_enabled": "has_options" in attributes,
        "overnight_tradable": "overnight_tradable" in attributes,
        "shares_outstanding": _safe_number(fundamentals.get("shares_outstanding")),
        "float_shares": _safe_number(fundamentals.get("float_shares")),
    }

    latest_trade = (snapshot or {}).get("latestTrade") or {}
    price = _safe_number(latest_trade.get("p"))
    price_timestamp = latest_trade.get("t", "")

    if frame.empty:
        return {
            **{column: None for column in OUTPUT_COLUMNS},
            **base,
            "price": price,
            "price_timestamp": price_timestamp,
            "data_status": "no_daily_bars",
        }

    close = float(frame["close"].iloc[-1])
    prev_close = float(frame["close"].iloc[-2]) if len(frame) >= 2 else None
    price = price if price is not None else close
    close_date = frame["timestamp"].iloc[-1].date().isoformat()
    volume = float(frame["volume"].iloc[-1])

    close_series = frame["close"]
    vol_series = frame["volume"]
    dollar_volume = close_series * vol_series

    sma20 = float(close_series.tail(20).mean()) if len(frame) >= 20 else None
    sma50 = float(close_series.tail(50).mean()) if len(frame) >= 50 else None
    sma200 = float(close_series.tail(200).mean()) if len(frame) >= 200 else None
    avg_volume_20d = float(vol_series.tail(20).mean()) if len(frame) >= 20 else None
    avg_volume_50d = float(vol_series.tail(50).mean()) if len(frame) >= 50 else None
    avg_dollar_vol_m = float(dollar_volume.tail(20).mean() / 1_000_000) if len(frame) >= 20 else None
    today_dollar_vol_m = close * volume / 1_000_000

    window_52 = frame.tail(252)
    week52_low = float(window_52["low"].min()) if not window_52.empty else None
    week52_high = float(window_52["high"].max()) if not window_52.empty else None
    pos_52w = None
    if week52_low is not None and week52_high not in (None, week52_low):
        raw_position = (price - week52_low) / (week52_high - week52_low) * 100.0
        pos_52w = max(0.0, min(100.0, raw_position))

    atr14 = _atr(frame, 14)
    returns = np.log(close_series / close_series.shift(1)).dropna()
    volatility_20d_pct = None
    if len(returns) >= 20:
        volatility_20d_pct = float(returns.tail(20).std(ddof=1) * np.sqrt(252) * 100)

    float_shares = base["float_shares"]
    float_turnover_pct = None
    if float_shares not in (None, 0):
        float_turnover_pct = volume / float_shares * 100.0

    data_status = "ok" if len(frame) >= 200 else f"insufficient_history_{len(frame)}d"

    row = {
        **base,
        "price": price,
        "price_timestamp": price_timestamp,
        "close": close,
        "close_date": close_date,
        "prev_close": prev_close,
        "close_change_pct": _pct(close, prev_close),
        "live_vs_close_pct": _pct(price, close),
        "volume": volume,
        "avg_volume_20d": avg_volume_20d,
        "avg_volume_50d": avg_volume_50d,
        "relative_volume": volume / avg_volume_20d if avg_volume_20d else None,
        "dollar_vol_m": avg_dollar_vol_m,
        "today_dollar_vol_m": today_dollar_vol_m,
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "price_vs_sma50_pct": _pct(price, sma50),
        "price_vs_sma200_pct": _pct(price, sma200),
        "week52_low": week52_low,
        "week52_high": week52_high,
        "pos_52w": pos_52w,
        "atr14": atr14,
        "atr_pct": (atr14 / price * 100.0) if atr14 is not None and price else None,
        "rsi14": _rsi(close_series, 14),
        "volatility_20d_pct": volatility_20d_pct,
        "float_turnover_pct": float_turnover_pct,
        "data_status": data_status,
    }
    return {column: row.get(column) for column in OUTPUT_COLUMNS}
