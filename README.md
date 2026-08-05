# Alpaca → Google Sheets Screener

A small scheduled stock screener designed for a GitHub repository deployed as a Railway cron service.

It:

- retrieves every **active, tradable U.S. equity** from Alpaca;
- downloads adjusted daily bars and current stock snapshots;
- calculates trend, liquidity, volatility, and 52-week metrics;
- writes a clean, filterable table to Google Sheets;
- records each execution in a `Run_Log` tab;
- runs every 30 minutes during a broad weekday UTC window, checks Alpaca's market clock, and exits immediately when the U.S. equity market is closed;
- exits after each completed refresh so Railway cron can invoke it again later.

## Output columns

### Required fields

| Column | Definition |
|---|---|
| `price` | Latest Alpaca trade price; falls back to the latest completed close. |
| `sma200` | Simple average of the last 200 adjusted daily closes. |
| `volume` | Volume on the latest available daily bar. |
| `avg_volume_20d` | Average volume over 20 daily bars. |
| `float_shares` | Optional value joined from the Google Sheets `Fundamentals` tab. |
| `shares_outstanding` | Optional value joined from the `Fundamentals` tab. |
| `close` | Latest adjusted daily close. |
| `sma50` | Simple average of the last 50 adjusted daily closes. |
| `pos_52w` | Price position between the 252-session low and high, from 0 to 100. |
| `dollar_vol_m` | Average 20-day close × volume, expressed in USD millions. |

### Additional useful fields

The output also includes 20-day SMA, previous close, daily percentage change, live price versus close, 50-day average volume, relative volume, current-day dollar volume, distance from SMA50/SMA200, 52-week high/low, ATR14, ATR percentage, RSI14, annualized 20-day volatility, float turnover, margin/short/borrow/fractional flags, options availability, overnight-trading availability, and a data-quality status.

## Important limitation: float and shares outstanding

Alpaca's asset and market-data endpoints do not provide share float or shares outstanding. The output columns are still present, but they remain blank unless populated in the optional `Fundamentals` input tab.

The service automatically creates that tab with these headers:

```text
symbol | float_shares | shares_outstanding
```

This keeps the first version limited to Alpaca and Google Sheets. A dedicated fundamentals provider can be added later without changing the output schema.

## Google setup

1. Create a Google Cloud project.
2. Enable the Google Sheets API.
3. Create a service account and JSON key.
4. Create the destination spreadsheet.
5. Share the spreadsheet with the service account's `client_email` as an editor.
6. Copy the spreadsheet ID from its URL.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

For a small test, add:

```dotenv
SYMBOLS=AAPL,MSFT,NVDA
DRY_RUN=true
```

Then run:

```bash
python -m app.main
```

A dry run writes `output/screener.csv` instead of touching Google Sheets.

## Deploy to GitHub and Railway

1. Create an empty GitHub repository.
2. Commit and push this project.
3. In Railway, create a project and choose **Deploy from GitHub repo**.
4. Add the variables from `.env.example` in the Railway service's Variables tab.
5. Railway reads `railway.json`, builds the Dockerfile, and runs `python -m app.main`.
6. The included cron is `*/30 13-21 * * 1-5`. Railway evaluates cron in UTC, so this broad window covers regular U.S. market hours in both standard time and daylight-saving time. Before doing any expensive work, the app calls Alpaca's market clock and exits unless `is_open` is true. This also handles weekends, exchange holidays, and early-close sessions.

The default is a refresh every 30 minutes. Railway skips a new cron invocation if the previous run is still active, so shorten the interval only after confirming the full-universe job completes comfortably. Set `FORCE_RUN=true` for a one-off manual execution outside market hours, then set it back to `false`.

The service uses `restartPolicyType: NEVER` because a Railway cron process must finish and exit. Pushes to the connected GitHub branch trigger new Railway deployments.

## Alpaca feed

`ALPACA_DATA_FEED=iex` is the default and works for common free-plan setups. Set it to `sip` only when the Alpaca subscription/key has SIP access.

## Scale and runtime

The complete Alpaca equity universe requires many paginated bar requests. `SYMBOL_BATCH_SIZE=150` is conservative and can be tuned. The code retries HTTP 429 and transient server errors automatically.

Google Sheets comfortably holds the final screener because it writes one row per asset, not the underlying historical bars.

## Tests

```bash
pytest -q
```
