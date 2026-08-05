from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_trading_base_url: str
    alpaca_data_base_url: str
    alpaca_data_feed: str

    google_spreadsheet_id: str
    google_service_account_json: str
    google_output_sheet: str
    google_fundamentals_sheet: str
    google_run_log_sheet: str

    lookback_calendar_days: int
    symbol_batch_size: int
    request_timeout_seconds: int
    request_pause_seconds: float

    symbols_override: tuple[str, ...]
    max_symbols: int | None
    dry_run: bool
    output_csv: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        symbols = tuple(
            item.strip().upper()
            for item in os.getenv("SYMBOLS", "").split(",")
            if item.strip()
        )
        max_symbols_raw = os.getenv("MAX_SYMBOLS", "").strip()

        settings = cls(
            alpaca_api_key=os.getenv("ALPACA_API_KEY", "").strip(),
            alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", "").strip(),
            alpaca_trading_base_url=os.getenv(
                "ALPACA_TRADING_BASE_URL", "https://paper-api.alpaca.markets"
            ).rstrip("/"),
            alpaca_data_base_url=os.getenv(
                "ALPACA_DATA_BASE_URL", "https://data.alpaca.markets"
            ).rstrip("/"),
            alpaca_data_feed=os.getenv("ALPACA_DATA_FEED", "iex").strip().lower(),
            google_spreadsheet_id=os.getenv("GOOGLE_SPREADSHEET_ID", "").strip(),
            google_service_account_json=os.getenv(
                "GOOGLE_SERVICE_ACCOUNT_JSON", ""
            ).strip(),
            google_output_sheet=os.getenv("GOOGLE_OUTPUT_SHEET", "Screener").strip(),
            google_fundamentals_sheet=os.getenv(
                "GOOGLE_FUNDAMENTALS_SHEET", "Fundamentals"
            ).strip(),
            google_run_log_sheet=os.getenv("GOOGLE_RUN_LOG_SHEET", "Run_Log").strip(),
            lookback_calendar_days=_int("LOOKBACK_CALENDAR_DAYS", 420),
            symbol_batch_size=_int("SYMBOL_BATCH_SIZE", 150),
            request_timeout_seconds=_int("REQUEST_TIMEOUT_SECONDS", 45),
            request_pause_seconds=_float("REQUEST_PAUSE_SECONDS", 0.15),
            symbols_override=symbols,
            max_symbols=int(max_symbols_raw) if max_symbols_raw else None,
            dry_run=_bool("DRY_RUN", False),
            output_csv=os.getenv("OUTPUT_CSV", "output/screener.csv").strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        missing = []
        if not self.alpaca_api_key:
            missing.append("ALPACA_API_KEY")
        if not self.alpaca_secret_key:
            missing.append("ALPACA_SECRET_KEY")
        if not self.dry_run:
            if not self.google_spreadsheet_id:
                missing.append("GOOGLE_SPREADSHEET_ID")
            if not self.google_service_account_json:
                missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        if self.symbol_batch_size < 1:
            raise ValueError("SYMBOL_BATCH_SIZE must be at least 1")
        if self.lookback_calendar_days < 300:
            raise ValueError("LOOKBACK_CALENDAR_DAYS should be at least 300 for SMA200")
