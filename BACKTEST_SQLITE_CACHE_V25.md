# Backtest SQLite Market Data Cache

This build uses a local SQLite database for backtest market data instead of MongoDB.

## Why SQLite

For the current 30-DTE backtest workflow, five years of daily OHLCV for about 500 symbols is small enough for SQLite when the table is indexed by symbol, interval, and date. SQLite avoids Windows service installation, admin rights, open ports, and MongoDB security restrictions.

## Storage split

- `options_data.db` remains the existing application/config database.
- `data/market_data.db` stores historical OHLCV and saved backtest runs.

The market-data DB is separate on purpose so a source-code package cannot overwrite your app data.

## Configuration

Default path:

```text
data/market_data.db
```

Optional override:

```powershell
$env:MARKET_DATA_DB="T:\\ajain33\\Learn\\Python\\spy_oi_app_v5\\data\\market_data.db"
python app.py
```

Data-provider choices on the Backtest page:

```text
Auto - SQLite then yfinance fill
SQLite cache only
yfinance only
```

## Data sync

Use the Backtest page button:

```text
Sync 5y daily data
```

The app downloads daily OHLCV from yfinance and upserts it into SQLite using:

```text
PRIMARY KEY(symbol, interval, bar_time)
```

Repeated syncs update existing rows instead of duplicating them.

## Backtest behavior

The chronological backtest reads daily bars from SQLite first. If data is missing and fill is enabled, it fetches from yfinance, caches the data, and continues.

The simulation still runs one trading day at a time and evaluates scanner conditions only with data available through that simulated day.

## Tables

```text
market_bars
market_data_sync
backtest_runs
backtest_trades
```

## Packaging rule

Do not include database files in source ZIPs:

```text
*.db
*.sqlite
*.sqlite3
*.db-wal
*.db-shm
*.db-journal
```
