from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.alpaca_client import AlpacaClient, chunks
from app.config import Settings
from app.google_sheets import GoogleSheetsClient
from app.indicators import OUTPUT_COLUMNS, build_row


LOGGER = logging.getLogger(__name__)


class Screener:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.alpaca = AlpacaClient(settings)
        self.sheets = None if settings.dry_run else GoogleSheetsClient(settings)

    def _select_assets(self) -> list[dict]:
        assets = self.alpaca.get_tradable_assets()
        if self.settings.symbols_override:
            wanted = set(self.settings.symbols_override)
            assets = [asset for asset in assets if asset.get("symbol") in wanted]
        if self.settings.max_symbols:
            assets = assets[: self.settings.max_symbols]
        return assets

    def run(self) -> pd.DataFrame:
        assets = self._select_assets()
        symbols = [asset["symbol"] for asset in assets]
        fundamentals = self.sheets.read_fundamentals() if self.sheets else {}

        now = datetime.now(timezone.utc)
        start = now - timedelta(days=self.settings.lookback_calendar_days)
        bars_by_symbol: dict[str, list[dict]] = {}
        snapshots: dict[str, dict] = {}

        batches = list(chunks(symbols, self.settings.symbol_batch_size))
        for batch_number, symbol_batch in enumerate(batches, start=1):
            LOGGER.info(
                "Fetching batch %s/%s (%s symbols)",
                batch_number,
                len(batches),
                len(symbol_batch),
            )
            bars_by_symbol.update(self.alpaca.get_daily_bars(symbol_batch, start, now))
            snapshots.update(self.alpaca.get_snapshots(symbol_batch))

        rows = [
            build_row(
                asset=asset,
                bars=bars_by_symbol.get(asset["symbol"], []),
                snapshot=snapshots.get(asset["symbol"]),
                fundamentals=fundamentals.get(asset["symbol"], {}),
            )
            for asset in assets
        ]
        frame = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
        if not frame.empty:
            frame = frame.sort_values(
                by=["dollar_vol_m", "symbol"],
                ascending=[False, True],
                na_position="last",
            ).reset_index(drop=True)
        return frame
