"""Alpaca -> Google Sheets market screener.

Railway environment variables required:
    ALPACA_API_KEY
    ALPACA_SECRET_KEY
    GOOGLE_SPREADSHEET_ID
    GOOGLE_SERVICE_ACCOUNT_JSON

To populate shares outstanding from free SEC EDGAR data, also set:
    SEC_USER_AGENT=Your Name or App your-email@example.com

Useful optional variables:
    ALPACA_DATA_FEED=iex
    SEC_REFRESH_HOURS=24
    SEC_FRAME_QUARTERS=16
    SEC_REQUEST_PAUSE_SECONDS=0.15
    SEC_MIN_MATCHES=100
    SEC_REQUIRED=false
    GOOGLE_OUTPUT_SHEET=Screener
    GOOGLE_FUNDAMENTALS_SHEET=Fundamentals
    RUN_ONLY_WHEN_MARKET_OPEN=true
    FORCE_RUN=false
    LOOKBACK_CALENDAR_DAYS=420
    SYMBOL_BATCH_SIZE=150
    REQUEST_TIMEOUT_SECONDS=45
    REQUEST_PAUSE_SECONDS=0.35
    SYMBOLS=AAPL,MSFT,NVDA       # optional test list
    MAX_SYMBOLS=100             # optional test limit
    LOG_LEVEL=INFO

The Fundamentals worksheet is an SEC cache and supports manual overrides:
    symbol | shares_outstanding | reported_date | source | updated_utc | cik | accession

Rows whose source is "manual" override SEC values. SEC data represents the latest
reported common shares outstanding, not tradeable float. Instruments such as ETFs,
warrants, units, rights, preferred shares, and some foreign issuers may remain blank.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from statistics import fmean
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


TRADING_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"
EASTERN = ZoneInfo("America/New_York")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

FUNDAMENTALS_COLUMNS = [
    "symbol",
    "shares_outstanding",
    "reported_date",
    "source",
    "updated_utc",
    "cik",
    "accession",
]

OUTPUT_COLUMNS = [
    "symbol",
    "price",
    "close",
    "prev_close",
    "change_from_close_pct",
    "volume",
    "avg_volume_20d",
    "avg_volume_50d",
    "relative_volume",
    "dollar_vol_m",
    "avg_dollar_vol_20d_m",
    "sma50",
    "sma200",
    "price_vs_sma50_pct",
    "price_vs_sma200_pct",
    "week52_low",
    "week52_high",
    "pos_52w",
    "shares_outstanding",
    "shares_turnover_pct",
]

# Keep AF and later reserved for user formulas. Clearing the former generated
# range removes stale headers/data from earlier versions without touching AF+.
OUTPUT_CLEAR_END_COLUMN = "AE"
OUTPUT_RESERVED_COLUMN_COUNT = 31


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing Railway variable: {name}")
    return value


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def mean_or_none(values: list[float], minimum: int) -> float | None:
    return fmean(values[-minimum:]) if len(values) >= minimum else None


def pct(value: float | None, reference: float | None) -> float | None:
    if value is None or reference in (None, 0):
        return None
    return (value / reference - 1.0) * 100.0


def chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def column_letter(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


class Alpaca:
    def __init__(self) -> None:
        self.api_key = required_env("ALPACA_API_KEY")
        self.secret_key = required_env("ALPACA_SECRET_KEY")
        self.feed = os.getenv("ALPACA_DATA_FEED", "iex").strip().lower()
        self.timeout = env_int("REQUEST_TIMEOUT_SECONDS", 45)
        self.pause = env_float("REQUEST_PAUSE_SECONDS", 0.35)

        self.session = requests.Session()
        retry = Retry(
            total=7,
            connect=4,
            read=4,
            status=7,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Accept": "application/json",
                "User-Agent": "simple-alpaca-sheets-screener/1.0",
            }
        )

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        if self.pause > 0:
            time.sleep(self.pause)
        return response.json()

    def market_clock(self) -> dict[str, Any]:
        payload = self.get(f"{TRADING_BASE_URL}/v2/clock")
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Alpaca market-clock response")
        return payload

    def tradable_assets(self) -> list[dict[str, Any]]:
        payload = self.get(
            f"{TRADING_BASE_URL}/v2/assets",
            params={"status": "active", "asset_class": "us_equity"},
        )
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected Alpaca assets response")

        assets = [
            asset
            for asset in payload
            if asset.get("tradable") is True
            and asset.get("status") == "active"
            and asset.get("class") == "us_equity"
        ]
        assets.sort(key=lambda item: str(item.get("symbol", "")))
        return assets

    def snapshots(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not symbols:
            return {}
        payload = self.get(
            f"{DATA_BASE_URL}/v2/stocks/snapshots",
            params={"symbols": ",".join(symbols), "feed": self.feed},
        )
        if isinstance(payload, dict) and isinstance(payload.get("snapshots"), dict):
            return payload["snapshots"]
        return payload if isinstance(payload, dict) else {}

    def daily_bars(
        self, symbols: list[str], start: datetime, end: datetime
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
        page_token: str | None = None

        while True:
            params: dict[str, Any] = {
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjustment": "all",
                "feed": self.feed,
                "limit": 10000,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token

            payload = self.get(f"{DATA_BASE_URL}/v2/stocks/bars", params=params)
            for symbol, bars in (payload.get("bars") or {}).items():
                result.setdefault(symbol, []).extend(bars)

            page_token = payload.get("next_page_token")
            if not page_token:
                return result



def symbol_aliases(symbol: str) -> list[str]:
    """Return conservative aliases for matching ticker formats."""
    symbol = symbol.strip().upper()
    aliases = [symbol]
    if "." in symbol:
        aliases.append(symbol.replace(".", "-"))
    if "-" in symbol:
        aliases.append(symbol.replace("-", "."))
    return list(dict.fromkeys(aliases))


def minimum_sec_matches(target_count: int) -> int:
    configured = max(1, env_int("SEC_MIN_MATCHES", 100))
    proportional = max(1, math.ceil(target_count * 0.10))
    return min(configured, proportional)


def recent_quarter_frames(count: int, now: datetime | None = None) -> list[str]:
    """Return SEC instantaneous calendar-quarter frame names, newest first."""
    if count < 1:
        raise ValueError("SEC_FRAME_QUARTERS must be at least 1")
    current = now or datetime.now(timezone.utc)
    year = current.year
    quarter = (current.month - 1) // 3 + 1
    frames: list[str] = []
    for _ in range(count):
        frames.append(f"CY{year}Q{quarter}I")
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    return frames


class SEC:
    """SEC EDGAR client for reported common shares outstanding."""

    TICKER_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
    TICKER_FALLBACK_URL = "https://www.sec.gov/files/company_tickers.json"
    FRAME_BASE_URL = "https://data.sec.gov/api/xbrl/frames"
    CONCEPTS = (
        ("dei", "EntityCommonStockSharesOutstanding", "SEC_DEI"),
        ("us-gaap", "CommonStockSharesOutstanding", "SEC_US_GAAP"),
    )

    def __init__(self, user_agent: str) -> None:
        if not user_agent.strip():
            raise ValueError(
                "SEC_USER_AGENT must identify your app/name and contact email"
            )
        self.timeout = env_int("REQUEST_TIMEOUT_SECONDS", 45)
        self.pause = max(0.11, env_float("SEC_REQUEST_PAUSE_SECONDS", 0.15))
        self.frame_quarters = env_int("SEC_FRAME_QUARTERS", 16)
        if self.frame_quarters < 1:
            raise ValueError("SEC_FRAME_QUARTERS must be at least 1")

        self.session = requests.Session()
        retry = Retry(
            total=7,
            connect=4,
            read=4,
            status=7,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": user_agent.strip(),
            }
        )

    def get_json(self, url: str) -> Any | None:
        response = self.session.get(url, timeout=self.timeout)
        if response.status_code == 404:
            if self.pause > 0:
                time.sleep(self.pause)
            return None
        response.raise_for_status()
        if self.pause > 0:
            time.sleep(self.pause)
        return response.json()

    def ticker_cik_map(self) -> dict[str, str]:
        """Return uppercase SEC ticker -> zero-padded CIK."""
        result: dict[str, str] = {}
        payload = self.get_json(self.TICKER_URL)
        if isinstance(payload, dict):
            fields = [str(field).strip().lower() for field in payload.get("fields", [])]
            for raw_row in payload.get("data", []):
                if not isinstance(raw_row, list):
                    continue
                row = dict(zip(fields, raw_row))
                ticker = str(row.get("ticker", "")).strip().upper()
                cik = str(row.get("cik", "")).strip()
                if ticker and cik.isdigit():
                    result[ticker] = cik.zfill(10)
        if result:
            return result

        payload = self.get_json(self.TICKER_FALLBACK_URL)
        if isinstance(payload, dict):
            for item in payload.values():
                if not isinstance(item, dict):
                    continue
                ticker = str(item.get("ticker", "")).strip().upper()
                cik = str(item.get("cik_str", "")).strip()
                if ticker and cik.isdigit():
                    result[ticker] = cik.zfill(10)
        if not result:
            raise RuntimeError("Unexpected SEC ticker/CIK mapping response")
        return result

    @staticmethod
    def _record_rank(record: dict[str, Any]) -> tuple[str, int, str]:
        source_priority = 1 if record.get("source") == "SEC_DEI" else 0
        return (
            str(record.get("reported_date", "")),
            source_priority,
            str(record.get("accession", "")),
        )

    def shares_by_cik(self) -> dict[str, dict[str, Any]]:
        """Fetch recent SEC frames and keep the latest usable value per CIK."""
        found: dict[str, dict[str, Any]] = {}
        fetched_utc = datetime.now(timezone.utc).isoformat()
        frames = recent_quarter_frames(self.frame_quarters)

        for taxonomy, tag, source in self.CONCEPTS:
            for frame in frames:
                logging.info("Fetching SEC shares frame %s/%s/%s", source, frame, tag)
                url = f"{self.FRAME_BASE_URL}/{taxonomy}/{tag}/shares/{frame}.json"
                payload = self.get_json(url)
                if payload is None:
                    logging.info("SEC frame %s/%s is not available", source, frame)
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise RuntimeError(f"Unexpected SEC frame response for {source}/{frame}")

                added = 0
                for item in payload["data"]:
                    if not isinstance(item, dict):
                        continue
                    cik_raw = str(item.get("cik", "")).strip()
                    shares = safe_float(item.get("val"))
                    reported_date = str(item.get("end", "")).strip()
                    if not cik_raw.isdigit() or shares is None or shares <= 0:
                        continue
                    # Guard against obvious XBRL scaling errors without rejecting
                    # legitimate large issuers.
                    if shares >= 100_000_000_000_000:
                        continue
                    record = {
                        "shares_outstanding": shares,
                        "reported_date": reported_date,
                        "source": source,
                        "updated_utc": fetched_utc,
                        "cik": cik_raw.zfill(10),
                        "accession": str(item.get("accn", "")).strip(),
                    }
                    cik = record["cik"]
                    existing = found.get(cik)
                    if existing is None or self._record_rank(record) > self._record_rank(existing):
                        found[cik] = record
                        added += 1
                logging.info(
                    "SEC frame %s/%s returned %s rows; accepted %s latest records",
                    source,
                    frame,
                    len(payload["data"]),
                    added,
                )
        return found

    def shares_outstanding(
        self, target_symbols: list[str]
    ) -> dict[str, dict[str, Any]]:
        ticker_map = self.ticker_cik_map()
        by_cik = self.shares_by_cik()
        found: dict[str, dict[str, Any]] = {}

        for target in target_symbols:
            target = target.strip().upper()
            cik = None
            for alias in symbol_aliases(target):
                cik = ticker_map.get(alias)
                if cik:
                    break
            if cik and cik in by_cik:
                found[target] = dict(by_cik[cik])

        logging.info(
            "Matched SEC shares-outstanding data for %s of %s Alpaca symbols",
            len(found),
            len(target_symbols),
        )
        if len(found) < minimum_sec_matches(len(target_symbols)):
            logging.warning(
                "SEC coverage is low (%s of %s); unsupported instruments will remain blank",
                len(found),
                len(target_symbols),
            )
        return found


class Sheets:
    def __init__(self) -> None:
        spreadsheet_id = required_env("GOOGLE_SPREADSHEET_ID")
        raw_credentials = required_env("GOOGLE_SERVICE_ACCOUNT_JSON")
        try:
            credentials_info = json.loads(raw_credentials)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_JSON must contain the complete service-account JSON object"
            ) from exc

        credentials = Credentials.from_service_account_info(
            credentials_info, scopes=SCOPES
        )
        self.service = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        self.spreadsheet_id = spreadsheet_id
        self.output_title = os.getenv("GOOGLE_OUTPUT_SHEET", "Screener").strip()
        self.fundamentals_title = os.getenv(
            "GOOGLE_FUNDAMENTALS_SHEET", "Fundamentals"
        ).strip()

    def sheet_map(self) -> dict[str, dict[str, Any]]:
        response = (
            self.service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties")
            .execute()
        )
        return {
            item["properties"]["title"]: item["properties"]
            for item in response.get("sheets", [])
        }

    def ensure_sheet(self, title: str, rows: int, columns: int) -> int:
        sheets = self.sheet_map()
        if title not in sheets:
            response = (
                self.service.spreadsheets()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={
                        "requests": [
                            {
                                "addSheet": {
                                    "properties": {
                                        "title": title,
                                        "gridProperties": {
                                            "rowCount": rows,
                                            "columnCount": columns,
                                        },
                                    }
                                }
                            }
                        ]
                    },
                )
                .execute()
            )
            return response["replies"][0]["addSheet"]["properties"]["sheetId"]

        properties = sheets[title]
        sheet_id = properties["sheetId"]
        grid = properties.get("gridProperties", {})
        target_rows = max(rows, int(grid.get("rowCount", 0)))
        target_columns = max(columns, int(grid.get("columnCount", 0)))
        if target_rows != grid.get("rowCount") or target_columns != grid.get(
            "columnCount"
        ):
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": sheet_id,
                                    "gridProperties": {
                                        "rowCount": target_rows,
                                        "columnCount": target_columns,
                                    },
                                },
                                "fields": "gridProperties(rowCount,columnCount)",
                            }
                        }
                    ]
                },
            ).execute()
        return sheet_id

    def fundamentals(self) -> dict[str, dict[str, Any]]:
        self.ensure_sheet(self.fundamentals_title, 1000, len(FUNDAMENTALS_COLUMNS))
        response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.fundamentals_title}'!A:Z",
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute()
        )
        values = response.get("values", [])
        if not values:
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.fundamentals_title}'!A1",
                valueInputOption="RAW",
                body={"values": [FUNDAMENTALS_COLUMNS]},
            ).execute()
            return {}

        headers = [str(value).strip().lower() for value in values[0]]
        if "symbol" not in headers or "shares_outstanding" not in headers:
            logging.warning(
                "The Fundamentals tab must contain symbol and shares_outstanding columns"
            )
            return {}

        indexes = {header: headers.index(header) for header in headers}

        def cell(row: list[Any], name: str) -> Any:
            index = indexes.get(name)
            return row[index] if index is not None and len(row) > index else None

        result: dict[str, dict[str, Any]] = {}
        for row in values[1:]:
            symbol = str(cell(row, "symbol") or "").strip().upper()
            if not symbol:
                continue
            shares_outstanding = safe_float(cell(row, "shares_outstanding"))
            source = str(cell(row, "source") or "").strip()
            if not source and shares_outstanding is not None:
                source = "manual"
            result[symbol] = {
                "shares_outstanding": shares_outstanding,
                "reported_date": str(cell(row, "reported_date") or "").strip(),
                "source": source,
                "updated_utc": str(cell(row, "updated_utc") or "").strip(),
                "cik": str(cell(row, "cik") or "").strip(),
                "accession": str(cell(row, "accession") or "").strip(),
            }
        return result

    def write_fundamentals(self, fundamentals: dict[str, dict[str, Any]]) -> None:
        sheet_id = self.ensure_sheet(
            self.fundamentals_title,
            max(len(fundamentals) + 100, 1000),
            len(FUNDAMENTALS_COLUMNS),
        )
        # Preserve any user-managed columns to the right of the generated
        # Fundamentals data.
        fundamentals_last_column = column_letter(len(FUNDAMENTALS_COLUMNS))
        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{self.fundamentals_title}'!A:{fundamentals_last_column}",
            body={},
        ).execute()

        values: list[list[Any]] = [FUNDAMENTALS_COLUMNS]
        for symbol in sorted(fundamentals):
            record = fundamentals[symbol]
            values.append(
                [
                    symbol,
                    "" if record.get("shares_outstanding") is None else record.get("shares_outstanding"),
                    record.get("reported_date", ""),
                    record.get("source", ""),
                    record.get("updated_utc", ""),
                    record.get("cik", ""),
                    record.get("accession", ""),
                ]
            )

        for start in range(0, len(values), 1000):
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.fundamentals_title}'!A{start + 1}",
                valueInputOption="RAW",
                body={"majorDimension": "ROWS", "values": values[start : start + 1000]},
            ).execute()

        try:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": sheet_id,
                                    "gridProperties": {"frozenRowCount": 1},
                                },
                                "fields": "gridProperties.frozenRowCount",
                            }
                        },
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 0,
                                    "endRowIndex": 1,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": len(FUNDAMENTALS_COLUMNS),
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "textFormat": {"bold": True},
                                        "wrapStrategy": "WRAP",
                                    }
                                },
                                "fields": "userEnteredFormat(textFormat,wrapStrategy)",
                            }
                        },
                        {
                            "autoResizeDimensions": {
                                "dimensions": {
                                    "sheetId": sheet_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": 0,
                                    "endIndex": len(FUNDAMENTALS_COLUMNS),
                                }
                            }
                        },
                    ]
                },
            ).execute()
        except HttpError as exc:
            logging.warning("Fundamentals were written but formatting failed: %s", exc)

    def write_rows(self, rows: list[dict[str, Any]]) -> None:
        row_count = len(rows) + 100
        column_count = max(len(OUTPUT_COLUMNS) + 2, OUTPUT_RESERVED_COLUMN_COUNT)
        sheet_id = self.ensure_sheet(
            self.output_title, max(row_count, 1000), column_count
        )

        # A:AE was used by prior generated versions, so clear that range to remove
        # obsolete columns. AF and later are reserved for user formulas.
        last_column = column_letter(len(OUTPUT_COLUMNS))
        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{self.output_title}'!A:{OUTPUT_CLEAR_END_COLUMN}",
            body={},
        ).execute()

        all_values = [OUTPUT_COLUMNS]
        for row in rows:
            all_values.append(
                ["" if row.get(column) is None else row.get(column) for column in OUTPUT_COLUMNS]
            )

        write_size = 1000
        for start in range(0, len(all_values), write_size):
            block = all_values[start : start + write_size]
            start_row = start + 1
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.output_title}'!A{start_row}",
                valueInputOption="RAW",
                body={"majorDimension": "ROWS", "values": block},
            ).execute()

        end_row = max(len(all_values), 2)
        try:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": sheet_id,
                                    "gridProperties": {"frozenRowCount": 1},
                                },
                                "fields": "gridProperties.frozenRowCount",
                            }
                        },
                        {
                            # Remove the header filter/dropdown controls if a prior
                            # version of the screener added them.
                            "clearBasicFilter": {"sheetId": sheet_id}
                        },
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 0,
                                    "endRowIndex": 1,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": len(OUTPUT_COLUMNS),
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "textFormat": {"bold": True},
                                        "wrapStrategy": "WRAP",
                                    }
                                },
                                "fields": "userEnteredFormat(textFormat,wrapStrategy)",
                            }
                        },
                        {
                            "autoResizeDimensions": {
                                "dimensions": {
                                    "sheetId": sheet_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": 0,
                                    "endIndex": len(OUTPUT_COLUMNS),
                                }
                            }
                        },
                    ]
                },
            ).execute()
        except HttpError as exc:
            logging.warning(
                "Rows were written, but sheet formatting failed for A1:%s%s: %s",
                last_column,
                end_row,
                exc,
            )


def completed_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove invalid rows and today's still-forming daily bar."""
    today_eastern = datetime.now(timezone.utc).astimezone(EASTERN).date()
    cleaned: list[dict[str, Any]] = []

    for bar in bars:
        timestamp = bar.get("t")
        close = safe_float(bar.get("c"))
        high = safe_float(bar.get("h"))
        low = safe_float(bar.get("l"))
        volume = safe_float(bar.get("v"))
        if not timestamp or None in (close, high, low, volume):
            continue
        try:
            bar_time = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if bar_time.astimezone(EASTERN).date() >= today_eastern:
            continue
        cleaned.append(
            {
                "t": timestamp,
                "c": close,
                "h": high,
                "l": low,
                "v": volume,
            }
        )

    cleaned.sort(key=lambda item: str(item["t"]))
    return cleaned


def snapshot_current_price(snapshot: dict[str, Any] | None) -> float | None:
    """Return a usable current-session price from an Alpaca snapshot.

    Deliberately does not use prevDailyBar because that is a prior-session value,
    not evidence that the symbol currently has market data.
    """
    snapshot = snapshot or {}
    latest_trade = snapshot.get("latestTrade") or {}
    minute_bar = snapshot.get("minuteBar") or {}
    daily_bar = snapshot.get("dailyBar") or {}

    for value in (
        latest_trade.get("p"),
        minute_bar.get("c"),
        daily_bar.get("c"),
    ):
        price = safe_float(value)
        if price is not None and price > 0:
            return price
    return None


def build_row(
    asset: dict[str, Any],
    bars: list[dict[str, Any]],
    snapshot: dict[str, Any] | None,
    fundamentals: dict[str, Any] | None,
) -> dict[str, Any]:
    symbol = str(asset.get("symbol", ""))
    historical = completed_bars(bars)
    snapshot = snapshot or {}
    fundamentals = fundamentals or {}

    daily_bar = snapshot.get("dailyBar") or {}
    previous_daily_bar = snapshot.get("prevDailyBar") or {}

    historical_closes = [float(bar["c"]) for bar in historical]
    historical_volumes = [float(bar["v"]) for bar in historical]
    historical_dollar_volumes = [
        float(bar["c"]) * float(bar["v"]) for bar in historical
    ]

    close = historical_closes[-1] if historical_closes else safe_float(previous_daily_bar.get("c"))
    prev_close = historical_closes[-2] if len(historical_closes) >= 2 else None
    price = snapshot_current_price(snapshot)
    if price is None:
        price = close

    volume = safe_float(daily_bar.get("v"))
    if volume is None and historical_volumes:
        volume = historical_volumes[-1]

    avg_volume_20d = mean_or_none(historical_volumes, 20)
    avg_volume_50d = mean_or_none(historical_volumes, 50)
    sma50 = mean_or_none(historical_closes, 50)
    sma200 = mean_or_none(historical_closes, 200)
    avg_dollar_vol_20d = mean_or_none(historical_dollar_volumes, 20)

    week52 = historical[-252:]
    week52_low = min((float(bar["l"]) for bar in week52), default=None)
    week52_high = max((float(bar["h"]) for bar in week52), default=None)
    pos_52w = None
    if (
        price is not None
        and week52_low is not None
        and week52_high is not None
        and week52_high != week52_low
    ):
        pos_52w = max(
            0.0,
            min(100.0, (price - week52_low) / (week52_high - week52_low) * 100.0),
        )

    shares_outstanding = safe_float(fundamentals.get("shares_outstanding"))

    return {
        "symbol": symbol,
        "price": price,
        "close": close,
        "prev_close": prev_close,
        "change_from_close_pct": pct(price, close),
        "volume": volume,
        "avg_volume_20d": avg_volume_20d,
        "avg_volume_50d": avg_volume_50d,
        "relative_volume": (volume / avg_volume_20d) if volume is not None and avg_volume_20d else None,
        "dollar_vol_m": (price * volume / 1_000_000) if price is not None and volume is not None else None,
        "avg_dollar_vol_20d_m": (avg_dollar_vol_20d / 1_000_000) if avg_dollar_vol_20d is not None else None,
        "sma50": sma50,
        "sma200": sma200,
        "price_vs_sma50_pct": pct(price, sma50),
        "price_vs_sma200_pct": pct(price, sma200),
        "week52_low": week52_low,
        "week52_high": week52_high,
        "pos_52w": pos_52w,
        "shares_outstanding": shares_outstanding,
        "shares_turnover_pct": (volume / shares_outstanding * 100.0) if volume is not None and shares_outstanding else None,
    }


def select_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requested = {
        symbol.strip().upper()
        for symbol in os.getenv("SYMBOLS", "").split(",")
        if symbol.strip()
    }
    if requested:
        assets = [asset for asset in assets if asset.get("symbol") in requested]

    max_symbols = env_int("MAX_SYMBOLS", 0)
    if max_symbols > 0:
        assets = assets[:max_symbols]
    return assets



def parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sec_cache_is_fresh(
    fundamentals: dict[str, dict[str, Any]],
    max_age_hours: float,
    target_count: int,
) -> bool:
    if max_age_hours <= 0:
        return False
    sec_records = [
        record
        for record in fundamentals.values()
        if str(record.get("source", "")).strip().upper().startswith("SEC_")
    ]
    if len(sec_records) < minimum_sec_matches(target_count):
        return False
    timestamps = [
        parse_datetime(str(record.get("updated_utc", "")))
        for record in sec_records
    ]
    timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    if not timestamps:
        return False
    return max(timestamps) >= datetime.now(timezone.utc) - timedelta(hours=max_age_hours)


def manual_fundamentals(
    fundamentals: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    return {
        symbol: record
        for symbol, record in fundamentals.items()
        if not str(record.get("source", "")).strip().upper().startswith("SEC_")
        and safe_float(record.get("shares_outstanding")) is not None
    }


def refresh_fundamentals(
    sheets: Sheets, symbols: list[str]
) -> dict[str, dict[str, Any]]:
    cached = sheets.fundamentals()
    user_agent = os.getenv("SEC_USER_AGENT", "").strip()
    if not user_agent:
        logging.warning(
            "SEC_USER_AGENT is not set; using %s cached/manual shares rows",
            len(cached),
        )
        return cached

    refresh_hours = env_float("SEC_REFRESH_HOURS", 24.0)
    if sec_cache_is_fresh(cached, refresh_hours, len(symbols)):
        logging.info("Using cached SEC shares data (refresh interval %.1f hours)", refresh_hours)
        return cached

    try:
        fetched = SEC(user_agent).shares_outstanding(symbols)
        merged = dict(fetched)
        overrides = manual_fundamentals(cached)
        merged.update(overrides)
        sheets.write_fundamentals(merged)
        logging.info(
            "Updated Fundamentals tab with %s SEC rows and %s manual overrides",
            len(fetched),
            len(overrides),
        )
        return merged
    except Exception as exc:
        if env_bool("SEC_REQUIRED", False):
            raise
        logging.warning(
            "Could not refresh SEC shares data; continuing with cached values: %s",
            exc,
        )
        return cached


def run() -> None:
    alpaca = Alpaca()
    sheets = Sheets()

    run_only_open = env_bool("RUN_ONLY_WHEN_MARKET_OPEN", True)
    force_run = env_bool("FORCE_RUN", False)
    if run_only_open and not force_run:
        clock = alpaca.market_clock()
        if not clock.get("is_open", False):
            logging.info(
                "Market is closed. Nothing to do. Next open: %s",
                clock.get("next_open", "unknown"),
            )
            return
        logging.info("Market is open. Next close: %s", clock.get("next_close"))
    elif force_run:
        logging.warning("FORCE_RUN=true; bypassing market-open check")

    assets = select_assets(alpaca.tradable_assets())
    candidate_count = len(assets)
    logging.info(
        "Checking current prices for %s active tradable U.S. equities",
        candidate_count,
    )

    lookback_days = env_int("LOOKBACK_CALENDAR_DAYS", 420)
    batch_size = env_int("SYMBOL_BATCH_SIZE", 150)
    if lookback_days < 300:
        raise ValueError("LOOKBACK_CALENDAR_DAYS must be at least 300 for SMA200")
    if batch_size < 1:
        raise ValueError("SYMBOL_BATCH_SIZE must be at least 1")

    # Fetch snapshots first. Symbols without a usable current-session price are
    # excluded before SEC matching and before the much heavier historical-bars
    # requests. The snapshots are retained and reused when building the rows.
    snapshots_by_symbol: dict[str, dict[str, Any]] = {}
    eligible_assets: list[dict[str, Any]] = []
    candidate_batches = list(chunks(assets, batch_size))
    for batch_number, asset_batch in enumerate(candidate_batches, start=1):
        batch_symbols = [str(asset["symbol"]) for asset in asset_batch]
        logging.info(
            "Checking price batch %s/%s (%s symbols)",
            batch_number,
            len(candidate_batches),
            len(batch_symbols),
        )
        batch_snapshots = alpaca.snapshots(batch_symbols)
        for asset in asset_batch:
            symbol = str(asset["symbol"])
            snapshot = batch_snapshots.get(symbol)
            if snapshot_current_price(snapshot) is None:
                continue
            eligible_assets.append(asset)
            snapshots_by_symbol[symbol] = snapshot or {}

    excluded_count = candidate_count - len(eligible_assets)
    logging.info(
        "Current-price check kept %s symbols and excluded %s with no usable price",
        len(eligible_assets),
        excluded_count,
    )
    if not eligible_assets:
        logging.warning("No symbols returned a usable current price; writing an empty screener")
        sheets.write_rows([])
        return

    symbols = [str(asset["symbol"]) for asset in eligible_assets]
    fundamentals = refresh_fundamentals(sheets, symbols)

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)
    all_rows: list[dict[str, Any]] = []
    asset_batches = list(chunks(eligible_assets, batch_size))

    for batch_number, asset_batch in enumerate(asset_batches, start=1):
        batch_symbols = [str(asset["symbol"]) for asset in asset_batch]
        logging.info(
            "Fetching historical batch %s/%s (%s symbols)",
            batch_number,
            len(asset_batches),
            len(batch_symbols),
        )
        bars = alpaca.daily_bars(batch_symbols, start, now)

        for asset in asset_batch:
            symbol = str(asset["symbol"])
            all_rows.append(
                build_row(
                    asset,
                    bars.get(symbol, []),
                    snapshots_by_symbol.get(symbol),
                    fundamentals.get(symbol),
                )
            )

    all_rows.sort(
        key=lambda row: (
            -(safe_float(row.get("dollar_vol_m")) or -1.0),
            str(row.get("symbol", "")),
        )
    )
    sheets.write_rows(all_rows)
    logging.info("Finished. Wrote %s rows to Google Sheets.", len(all_rows))


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s - %(message)s",
    )
    started = time.monotonic()
    try:
        run()
        logging.info("Total runtime: %.1f seconds", time.monotonic() - started)
        return 0
    except Exception:
        logging.exception("Screener failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
