# Backtest Replay Lab v101

Adds a Replay Lab to the Backtest tab for the three primary decision engines:

1. Seller Flow / OI Buildup
2. Weekly Plan
3. GEX Plan

## What it does

Replay Lab replays each trading day in the selected date range and stores the trade ideas it would have found on that day. Each selected candidate is then closed with a simple outcome rule and marked WIN or LOSS.

Saved runs are stored in the existing local SQLite backtest store so they can be re-opened later.

## As-of-date behavior

Replay Lab uses only historical data available through the simulated date:

- daily OHLCV from the local market-data cache/yfinance fill,
- historical option-chain OI snapshots with `date <= replay_date`,
- OI walls and max-pain reconstructed from strike-level OI snapshots,
- weekly plans targeting the next/current Friday from the replay date,
- GEX plans using same-day or near-term expiry snapshots as of the replay date.

## User controls

### Common

- run name
- start/end date
- watchlist or symbol override
- max symbols
- max trades per day
- modules to include
- save run on/off

### Seller Flow

- quality level: 1 All, 2 Watch+, 3 Setup+, 4 Tradable+, 5 Best only
- ST/MT/LT windows
- DTE
- OTM percent
- spread width

### Weekly Plan

- symbols, for example SPY,QQQ,IWM
- run weekday, default Monday
- minimum score

### GEX Plan

- symbols, default SPY
- run time label
- mode: auto/range/breakout/breakdown
- minimum GEX strength percentage

## Outcome model

This version uses daily OHLC/close proxy rules:

- PS wins if exit close is above the short put.
- CS wins if exit close is below the short call.
- IC wins if exit close stays between the short put and short call.
- GEX range fade wins if the daily close finishes inside the planned range.
- GEX breakout/breakdown wins if the daily close confirms beyond the trigger.

This is intentionally a first replay layer. It validates signal quality and directional/range logic before adding intraday execution, option mark-to-market, profit targets, stops, or roll simulation.

## Files changed

- `oiapp/scanners/replay_lab.py`
- `oiapp/app_factory.py`
- `oiapp/static/app.js`
- `BACKTEST_REPLAY_LAB_V101.md`
