from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import Settings


LOGGER = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class GoogleSheetsClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        credentials_info = json.loads(settings.google_service_account_json)
        credentials = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
        self.service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self.spreadsheet_id = settings.google_spreadsheet_id

    def _sheet_map(self) -> dict[str, int]:
        spreadsheet = (
            self.service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties")
            .execute()
        )
        return {
            sheet["properties"]["title"]: sheet["properties"]["sheetId"]
            for sheet in spreadsheet.get("sheets", [])
        }

    def ensure_sheet(self, title: str, rows: int = 1000, columns: int = 40) -> int:
        sheet_map = self._sheet_map()
        if title in sheet_map:
            return sheet_map[title]
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

    def read_fundamentals(self) -> dict[str, dict[str, Any]]:
        title = self.settings.google_fundamentals_sheet
        self.ensure_sheet(title, rows=1000, columns=4)
        response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A:C",
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute()
        )
        values = response.get("values", [])
        if not values:
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A1:C2",
                valueInputOption="RAW",
                body={
                    "values": [
                        ["symbol", "float_shares", "shares_outstanding"],
                        ["AAPL", "", ""],
                    ]
                },
            ).execute()
            return {}

        headers = [str(value).strip().lower() for value in values[0]]
        required = {"symbol", "float_shares", "shares_outstanding"}
        if not required.issubset(headers):
            LOGGER.warning(
                "Fundamentals sheet must include headers: symbol, float_shares, shares_outstanding"
            )
            return {}

        index = {header: headers.index(header) for header in required}
        result: dict[str, dict[str, Any]] = {}
        for row in values[1:]:
            symbol = str(row[index["symbol"]]).strip().upper() if len(row) > index["symbol"] else ""
            if not symbol:
                continue
            result[symbol] = {
                "float_shares": row[index["float_shares"]] if len(row) > index["float_shares"] else None,
                "shares_outstanding": row[index["shares_outstanding"]]
                if len(row) > index["shares_outstanding"]
                else None,
            }
        return result

    @staticmethod
    def _clean_value(value: Any) -> Any:
        if pd.isna(value):
            return ""
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if hasattr(value, "item"):
            value = value.item()
        return value

    def write_dataframe(self, frame: pd.DataFrame) -> None:
        title = self.settings.google_output_sheet
        target_rows = max(len(frame) + 100, 1000)
        target_columns = max(len(frame.columns) + 5, 40)
        sheet_id = self.ensure_sheet(title, rows=target_rows, columns=target_columns)

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

        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A:AZ",
            body={},
        ).execute()

        values = [frame.columns.tolist()] + [
            [self._clean_value(value) for value in row]
            for row in frame.itertuples(index=False, name=None)
        ]
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A1",
            valueInputOption="RAW",
            body={"majorDimension": "ROWS", "values": values},
        ).execute()

        row_count = max(len(values), 2)
        column_count = len(frame.columns)
        requests = [
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
                            "endRowIndex": row_count,
                            "startColumnIndex": 0,
                            "endColumnIndex": column_count,
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
                        "endColumnIndex": column_count,
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
                        "endIndex": column_count,
                    }
                }
            },
        ]
        try:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": requests},
            ).execute()
        except HttpError as exc:
            LOGGER.warning("Values were written, but formatting failed: %s", exc)

    def append_run_log(
        self,
        status: str,
        asset_count: int,
        row_count: int,
        elapsed_seconds: float,
        message: str = "",
    ) -> None:
        title = self.settings.google_run_log_sheet
        self.ensure_sheet(title, rows=1000, columns=8)
        existing = (
            self.service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=f"'{title}'!A1:F2")
            .execute()
            .get("values", [])
        )
        if not existing:
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A1:F1",
                valueInputOption="RAW",
                body={
                    "values": [["run_utc", "status", "assets", "rows", "elapsed_seconds", "message"]]
                },
            ).execute()

        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A:F",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={
                "values": [
                    [
                        datetime.now(timezone.utc).isoformat(),
                        status,
                        asset_count,
                        row_count,
                        round(elapsed_seconds, 2),
                        message[:500],
                    ]
                ]
            },
        ).execute()
