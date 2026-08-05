"""Alpaca -> Google Sheets market screener.

Railway environment variables required:
    ALPACA_API_KEY
    ALPACA_SECRET_KEY
    GOOGLE_SPREADSHEET_ID
    GOOGLE_SERVICE_ACCOUNT_JSON

Useful optional variables:
    ALPACA_DATA_FEED=iex
    FMP_API_KEY=...              # enables automatic float/share data
    FMP_REFRESH_HOURS=24
    FMP_PAGE_SIZE=1000
    FMP_MAX_PAGES=200
    FMP_INDIVIDUAL_THRESHOLD=25
    FMP_MIN_MATCHES=100
    FMP_REQUIRED=false
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

The Fundamentals worksheet is an automatic cache and supports manual overrides:
    symbol | float_shares | shares_outstanding | free_float_pct | source | updated_utc

Rows whose source is "manual" override FMP values. If no FMP_API_KEY is supplied,
the screener continues to use any values manually entered on that worksheet.
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
FMP_BASE_URL = "https://financialmodelingprep.com/stable"
EASTERN = ZoneInfo("America/New_York")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

FUNDAMENTALS_COLUMNS = [
    "symbol",
    "float_shares",
    "shares_outstanding",
    "free_float_pct",
    "source",
    "updated_utc",
]

OUTPUT_COLUMNS = [
    "updated_utc",
    "symbol",
    "name",
    "exchange",
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
    "float_shares",
    "shares_outstanding",
    "free_float_pct",
    "fundamentals_source",
    "fundamentals_updated_utc",
    "float_turnover_pct",
    "marginable",
    "shortable",
    "easy_to_borrow",
    "fractionable",
    "data_status",
]


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
    """Return conservative symbol aliases used only for provider matching."""
    symbol = symbol.strip().upper()
    aliases = [symbol]
    if "." in symbol:
        aliases.append(symbol.replace(".", "-"))
    if "-" in symbol:
        aliases.append(symbol.replace("-", "."))
    return list(dict.fromkeys(aliases))


def minimum_fmp_matches(target_count: int) -> int:
    configured = max(1, env_int("FMP_MIN_MATCHES", 100))
    proportional = max(1, math.ceil(target_count * 0.25))
    return min(configured, proportional)


class FMP:
    """Financial Modeling Prep client for float and shares outstanding."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.base_url = os.getenv("FMP_BASE_URL", FMP_BASE_URL).rstrip("/")
        self.timeout = env_int("REQUEST_TIMEOUT_SECONDS", 45)
        self.pause = env_float("FMP_REQUEST_PAUSE_SECONDS", 0.20)
        self.page_size = env_int("FMP_PAGE_SIZE", 1000)
        self.max_pages = env_int("FMP_MAX_PAGES", 200)
        if self.page_size < 1:
            raise ValueError("FMP_PAGE_SIZE must be at least 1")
        if self.max_pages < 1:
            raise ValueError("FMP_MAX_PAGES must be at least 1")

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
                "User-Agent": "simple-alpaca-sheets-screener/1.2",
            }
        )

    def get(self, path: str, params: dict[str, Any]) -> Any:
        params = dict(params)
        params["apikey"] = self.api_key
        response = self.session.get(
            f"{self.base_url}/{path.lstrip('/')}",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        if self.pause > 0:
            time.sleep(self.pause)
        payload = response.json()
        if isinstance(payload, dict):
            message = payload.get("Error Message") or payload.get("error") or payload.get("message")
            if message:
                raise RuntimeError(f"FMP API error: {message}")
        return payload

    def _record(self, item: dict[str, Any], fetched_utc: str) -> dict[str, Any] | None:
        float_shares = safe_float(item.get("floatShares", item.get("float_shares")))
        shares_outstanding = safe_float(
            item.get("outstandingShares", item.get("shares_outstanding"))
        )
        free_float_pct = safe_float(item.get("freeFloat", item.get("free_float")))
        if float_shares is None and shares_outstanding is None:
            return None
        return {
            "float_shares": float_shares,
            "shares_outstanding": shares_outstanding,
            "free_float_pct": free_float_pct,
            "source": "FMP",
            "updated_utc": fetched_utc,
        }

    def individual_shares_float(
        self, target_symbols: list[str]
    ) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        fetched_utc = datetime.now(timezone.utc).isoformat()
        for index, target in enumerate(target_symbols, start=1):
            logging.info(
                "Fetching FMP float data for %s (%s/%s)",
                target,
                index,
                len(target_symbols),
            )
            for provider_symbol in symbol_aliases(target):
                payload = self.get("shares-float", {"symbol": provider_symbol})
                items = payload if isinstance(payload, list) else [payload]
                record = None
                for item in items:
                    if isinstance(item, dict):
                        record = self._record(item, fetched_utc)
                        if record:
                            break
                if record:
                    found[target] = record
                    break
        return found

    def all_shares_float(
        self, target_symbols: list[str]
    ) -> dict[str, dict[str, Any]]:
        target_symbols = [symbol.strip().upper() for symbol in target_symbols if symbol]
        threshold = env_int("FMP_INDIVIDUAL_THRESHOLD", 25)
        if len(target_symbols) <= threshold:
            found = self.individual_shares_float(target_symbols)
            minimum_matches = minimum_fmp_matches(len(target_symbols))
            if len(found) < minimum_matches:
                raise RuntimeError(
                    f"FMP matched only {len(found)} of {len(target_symbols)} requested symbols"
                )
            logging.info(
                "Matched FMP float/share data for %s of %s Alpaca symbols",
                len(found),
                len(target_symbols),
            )
            return found
        alias_to_target: dict[str, str | None] = {}
        for target in target_symbols:
            for alias in symbol_aliases(target):
                existing = alias_to_target.get(alias)
                if existing is None and alias in alias_to_target:
                    continue
                if existing is not None and existing != target:
                    alias_to_target[alias] = None
                else:
                    alias_to_target[alias] = target

        found: dict[str, dict[str, Any]] = {}
        fetched_utc = datetime.now(timezone.utc).isoformat()
        seen_page_signatures: set[tuple[Any, ...]] = set()

        logging.info(
            "FMP bulk pagination configured with requested limit=%s and max_pages=%s",
            self.page_size,
            self.max_pages,
        )

        for page in range(self.max_pages):
            logging.info("Fetching FMP float data page %s", page + 1)
            payload = self.get(
                "shares-float-all",
                {"page": page, "limit": self.page_size},
            )
            if not isinstance(payload, list):
                raise RuntimeError("Unexpected FMP shares-float-all response")
            if not payload:
                logging.info("FMP page %s was empty; pagination complete", page + 1)
                break

            page_symbols = tuple(
                str(item.get("symbol", "")).strip().upper()
                for item in payload
                if isinstance(item, dict) and item.get("symbol")
            )
            signature = (
                len(payload),
                page_symbols[:10],
                page_symbols[-10:],
            )
            if signature in seen_page_signatures:
                logging.warning(
                    "FMP page %s repeated a previous page; stopping pagination to avoid a loop",
                    page + 1,
                )
                break
            seen_page_signatures.add(signature)

            matches_before = len(found)
            for item in payload:
                if not isinstance(item, dict):
                    continue
                provider_symbol = str(item.get("symbol", "")).strip().upper()
                if not provider_symbol:
                    continue
                target = alias_to_target.get(provider_symbol)
                if not target:
                    continue

                candidate = self._record(item, fetched_utc)
                if not candidate:
                    continue
                existing = found.get(target)
                existing_score = sum(
                    existing.get(key) is not None
                    for key in ("float_shares", "shares_outstanding", "free_float_pct")
                ) if existing else -1
                candidate_score = sum(
                    candidate.get(key) is not None
                    for key in ("float_shares", "shares_outstanding", "free_float_pct")
                )
                if candidate_score >= existing_score:
                    found[target] = candidate

            logging.info(
                "FMP page %s returned %s rows; added %s matches (%s total)",
                page + 1,
                len(payload),
                len(found) - matches_before,
                len(found),
            )

            if len(found) >= len(target_symbols):
                logging.info("All requested symbols have FMP data; pagination complete")
                break

            # Do not stop merely because FMP returned fewer rows than the requested
            # limit. Some account tiers cap the actual response below `limit` while
            # still exposing additional pages. Continue until an empty or repeated
            # page is received.
        else:
            logging.warning(
                "Reached FMP_MAX_PAGES=%s before receiving an empty page",
                self.max_pages,
            )

        minimum_matches = minimum_fmp_matches(len(target_symbols))
        if len(found) < minimum_matches:
            raise RuntimeError(
                f"FMP matched only {len(found)} of {len(target_symbols)} symbols; "
                "check the API key, plan access, or FMP_MIN_MATCHES"
            )
        logging.info(
            "Matched FMP float/share data for %s of %s Alpaca symbols",
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
                range=f"'{self.fundamentals_title}'!A:F",
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
        required = ["symbol", "float_shares", "shares_outstanding"]
        if not all(header in headers for header in required):
            logging.warning(
                "The Fundamentals tab must start with: symbol, float_shares, shares_outstanding"
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
            float_shares = safe_float(cell(row, "float_shares"))
            shares_outstanding = safe_float(cell(row, "shares_outstanding"))
            free_float_pct = safe_float(cell(row, "free_float_pct"))
            source = str(cell(row, "source") or "").strip()
            if not source and (float_shares is not None or shares_outstanding is not None):
                source = "manual"
            result[symbol] = {
                "float_shares": float_shares,
                "shares_outstanding": shares_outstanding,
                "free_float_pct": free_float_pct,
                "source": source,
                "updated_utc": str(cell(row, "updated_utc") or "").strip(),
            }
        return result

    def write_fundamentals(self, fundamentals: dict[str, dict[str, Any]]) -> None:
        sheet_id = self.ensure_sheet(
            self.fundamentals_title,
            max(len(fundamentals) + 100, 1000),
            len(FUNDAMENTALS_COLUMNS),
        )
        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{self.fundamentals_title}'!A:F",
            body={},
        ).execute()

        values: list[list[Any]] = [FUNDAMENTALS_COLUMNS]
        for symbol in sorted(fundamentals):
            record = fundamentals[symbol]
            values.append(
                [
                    symbol,
                    "" if record.get("float_shares") is None else record.get("float_shares"),
                    "" if record.get("shares_outstanding") is None else record.get("shares_outstanding"),
                    "" if record.get("free_float_pct") is None else record.get("free_float_pct"),
                    record.get("source", ""),
                    record.get("updated_utc", ""),
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
        column_count = len(OUTPUT_COLUMNS) + 2
        sheet_id = self.ensure_sheet(
            self.output_title, max(row_count, 1000), max(column_count, 30)
        )

        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{self.output_title}'!A:AZ",
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

        last_column = column_letter(len(OUTPUT_COLUMNS))
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
                            "setBasicFilter": {
                                "filter": {
                                    "range": {
                                        "sheetId": sheet_id,
                                        "startRowIndex": 0,
                                        "endRowIndex": end_row,
                                        "startColumnIndex": 0,
                                        "endColumnIndex": len(OUTPUT_COLUMNS),
                                    }
                                }
                            }
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


def build_row(
    asset: dict[str, Any],
    bars: list[dict[str, Any]],
    snapshot: dict[str, Any] | None,
    fundamentals: dict[str, Any] | None,
    updated_utc: str,
) -> dict[str, Any]:
    symbol = str(asset.get("symbol", ""))
    historical = completed_bars(bars)
    snapshot = snapshot or {}
    fundamentals = fundamentals or {}

    latest_trade = snapshot.get("latestTrade") or {}
    daily_bar = snapshot.get("dailyBar") or {}
    previous_daily_bar = snapshot.get("prevDailyBar") or {}

    historical_closes = [float(bar["c"]) for bar in historical]
    historical_volumes = [float(bar["v"]) for bar in historical]
    historical_dollar_volumes = [
        float(bar["c"]) * float(bar["v"]) for bar in historical
    ]

    close = historical_closes[-1] if historical_closes else safe_float(previous_daily_bar.get("c"))
    prev_close = historical_closes[-2] if len(historical_closes) >= 2 else None
    price = safe_float(latest_trade.get("p"))
    if price is None:
        price = safe_float(daily_bar.get("c"))
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

    float_shares = safe_float(fundamentals.get("float_shares"))
    shares_outstanding = safe_float(fundamentals.get("shares_outstanding"))
    free_float_pct = safe_float(fundamentals.get("free_float_pct"))
    fundamentals_source = str(fundamentals.get("source", ""))
    fundamentals_updated_utc = str(fundamentals.get("updated_utc", ""))

    if not historical:
        data_status = "no_daily_bars"
    elif len(historical) < 200:
        data_status = f"insufficient_history_{len(historical)}d"
    elif price is None:
        data_status = "no_current_price"
    else:
        data_status = "ok"

    return {
        "updated_utc": updated_utc,
        "symbol": symbol,
        "name": asset.get("name", ""),
        "exchange": asset.get("exchange", ""),
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
        "float_shares": float_shares,
        "shares_outstanding": shares_outstanding,
        "free_float_pct": free_float_pct,
        "fundamentals_source": fundamentals_source,
        "fundamentals_updated_utc": fundamentals_updated_utc,
        "float_turnover_pct": (volume / float_shares * 100.0) if volume is not None and float_shares else None,
        "marginable": bool(asset.get("marginable", False)),
        "shortable": bool(asset.get("shortable", False)),
        "easy_to_borrow": bool(asset.get("easy_to_borrow", False)),
        "fractionable": bool(asset.get("fractionable", False)),
        "data_status": data_status,
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


def fmp_cache_is_fresh(
    fundamentals: dict[str, dict[str, Any]],
    max_age_hours: float,
    target_count: int,
) -> bool:
    if max_age_hours <= 0:
        return False
    fmp_records = [
        record
        for record in fundamentals.values()
        if str(record.get("source", "")).strip().upper() == "FMP"
    ]
    if len(fmp_records) < minimum_fmp_matches(target_count):
        return False
    timestamps = [
        parse_datetime(str(record.get("updated_utc", "")))
        for record in fmp_records
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
        if str(record.get("source", "")).strip().lower() != "fmp"
        and (
            safe_float(record.get("float_shares")) is not None
            or safe_float(record.get("shares_outstanding")) is not None
        )
    }


def refresh_fundamentals(
    sheets: Sheets, symbols: list[str]
) -> dict[str, dict[str, Any]]:
    cached = sheets.fundamentals()
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        logging.info(
            "FMP_API_KEY is not set; using %s cached/manual fundamentals rows",
            len(cached),
        )
        return cached

    refresh_hours = env_float("FMP_REFRESH_HOURS", 24.0)
    if fmp_cache_is_fresh(cached, refresh_hours, len(symbols)):
        logging.info("Using cached FMP fundamentals (refresh interval %.1f hours)", refresh_hours)
        return cached

    try:
        fetched = FMP(api_key).all_shares_float(symbols)
        merged = dict(fetched)
        overrides = manual_fundamentals(cached)
        merged.update(overrides)
        sheets.write_fundamentals(merged)
        logging.info(
            "Updated Fundamentals tab with %s FMP rows and %s manual overrides",
            len(fetched),
            len(overrides),
        )
        return merged
    except Exception as exc:
        if env_bool("FMP_REQUIRED", False):
            raise
        logging.warning(
            "Could not refresh FMP float/share data; continuing with cached values: %s",
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
    symbols = [str(asset["symbol"]) for asset in assets]
    logging.info("Processing %s active tradable U.S. equities", len(symbols))

    fundamentals = refresh_fundamentals(sheets, symbols)
    lookback_days = env_int("LOOKBACK_CALENDAR_DAYS", 420)
    batch_size = env_int("SYMBOL_BATCH_SIZE", 150)
    if lookback_days < 300:
        raise ValueError("LOOKBACK_CALENDAR_DAYS must be at least 300 for SMA200")
    if batch_size < 1:
        raise ValueError("SYMBOL_BATCH_SIZE must be at least 1")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)
    updated_utc = now.isoformat()
    all_rows: list[dict[str, Any]] = []
    asset_batches = list(chunks(assets, batch_size))

    for batch_number, asset_batch in enumerate(asset_batches, start=1):
        batch_symbols = [str(asset["symbol"]) for asset in asset_batch]
        logging.info(
            "Fetching batch %s/%s (%s symbols)",
            batch_number,
            len(asset_batches),
            len(batch_symbols),
        )
        bars = alpaca.daily_bars(batch_symbols, start, now)
        snapshots = alpaca.snapshots(batch_symbols)

        for asset in asset_batch:
            symbol = str(asset["symbol"])
            all_rows.append(
                build_row(
                    asset,
                    bars.get(symbol, []),
                    snapshots.get(symbol),
                    fundamentals.get(symbol),
                    updated_utc,
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
