# Note: SQLite is now the default backtest cache

This app build uses `data/market_data.db` as the default local SQLite market-data cache. MongoDB is no longer required for backtesting. The older MongoDB notes below are kept only for reference.

# MongoDB Backtest Storage

This version keeps the existing SQLite-backed app configuration in place and adds MongoDB for high-volume backtest data.

## What stays in SQLite

- Watchlists
- Saved scanner definitions
- Scanner-builder configuration
- Existing app settings and small metadata

## What is stored in MongoDB

- `market_bars`: daily OHLCV bars synced from yfinance
- `backtest_runs`: saved backtest summaries and configuration
- `backtest_trades`: saved trade logs for each run
- `market_data_sync`: sync status per symbol

## Configuration

Set these environment variables if needed:

```bash
MONGO_URI=mongodb://localhost:27017
MONGO_DB=oiapp
MARKET_DATA_PROVIDER=auto
```

`MARKET_DATA_PROVIDER` supports:

- `auto`: read MongoDB first, fill missing daily bars from yfinance, then cache them
- `mongo`: require MongoDB cached data
- `yfinance`: bypass MongoDB and fetch directly from yfinance

## Fixing "MongoDB unavailable · using yfinance fallback"

That message means the Flask app could not connect to MongoDB. Backtests can still run from yfinance, but caching, syncing five years of OHLCV, saving results, and viewing historical runs require MongoDB.

Common fixes:

1. Confirm `pymongo` is installed in the same Python environment used to run the app:

   ```bash
   pip install -r requirements.txt
   ```

2. Start MongoDB locally. The default app connection expects:

   ```text
   mongodb://localhost:27017
   ```

3. Confirm the app environment variables before starting Flask:

   Windows PowerShell:

   ```powershell
   $env:MONGO_URI="mongodb://localhost:27017"
   $env:MONGO_DB="oiapp"
   python app.py
   ```

   Windows cmd:

   ```bat
   set MONGO_URI=mongodb://localhost:27017
   set MONGO_DB=oiapp
   python app.py
   ```

4. Test connectivity from the same environment:

   ```bash
   python -c "from pymongo import MongoClient; c=MongoClient('mongodb://localhost:27017', serverSelectionTimeoutMS=2000); print(c.admin.command('ping'))"
   ```

5. Reload the Backtest page or click **Retry MongoDB status**.

If MongoDB is running on another host, set `MONGO_URI` to that connection string. If authentication is enabled, include credentials and the correct `authSource` in the URI.

## Backtest scoring

The generic Backtest page uses simple DTE scoring by default. It does not compute option dollars.

- `CALL`: winner if DTE close is above entry close
- `PUT`: winner if DTE close is below entry close
- `CS`: winner if DTE close is at or below the short call strike
- `PS`: winner if DTE close is at or above the short put strike
- `IC`: winner if DTE close is between the short put and short call strikes

## Strike selection

The Backtest page now supports two spread strike modes:

- **Target short delta**: choose a target short strike delta, such as `0.20`. The engine estimates the short strike with a Black-Scholes delta calculation using historical volatility known at the entry date. This is used only for strike selection; the backtest still scores winner/loss from the stock close at DTE.
- **Fixed OTM %**: choose the old percentage offset from entry close.

The **Strike width** field controls the distance between the short and long spread legs. For example, a CS with short call 105 and width 5 creates a long call near 110. An IC uses the same width on both sides.

Because yfinance does not provide reliable historical option chains for each past entry date, short-delta selection is an approximation based on historical underlying volatility rather than true historical option delta/IV.

## New API endpoints

```text
GET  /backtest/api/market-data/status
POST /backtest/api/market-data/sync
GET  /backtest/api/runs
GET  /backtest/api/runs/<run_id>
POST /backtest/api/save
```

The Backtest page has buttons to sync five years of daily bars, save a completed run, and reload historical runs.
