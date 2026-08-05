from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from app.config import Settings
from app.screener import Screener


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def main() -> int:
    started = time.monotonic()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)
    screener = Screener(settings)

    asset_count = 0
    row_count = 0
    try:
        if settings.run_only_when_market_open and not settings.force_run:
            clock = screener.alpaca.get_market_clock()
            if not clock.get("is_open", False):
                logger.info(
                    "Market is closed; skipping refresh. Next open: %s",
                    clock.get("next_open", "unknown"),
                )
                return 0
            logger.info(
                "Market is open; scheduled close: %s",
                clock.get("next_close", "unknown"),
            )
        elif settings.force_run:
            logger.warning("FORCE_RUN is enabled; bypassing the market-open check")

        frame = screener.run()
        asset_count = row_count = len(frame)

        if settings.dry_run:
            output_path = Path(settings.output_csv)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_csv(output_path, index=False)
            logger.info("Dry run complete: wrote %s rows to %s", len(frame), output_path)
        else:
            assert screener.sheets is not None
            screener.sheets.write_dataframe(frame)
            elapsed = time.monotonic() - started
            screener.sheets.append_run_log("success", asset_count, row_count, elapsed)
            logger.info("Wrote %s rows to Google Sheets", row_count)
        return 0
    except Exception as exc:
        elapsed = time.monotonic() - started
        logger.exception("Screener failed")
        if screener.sheets is not None:
            try:
                screener.sheets.append_run_log(
                    "failed", asset_count, row_count, elapsed, str(exc)
                )
            except Exception:
                logger.exception("Unable to write failure to Run_Log")
        return 1


if __name__ == "__main__":
    sys.exit(main())
