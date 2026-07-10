# Backtest Simple v27

This version keeps the Backtest page intentionally minimal and uses the local SQLite market-data cache.

## Backtest inputs

- Watchlist
- Optional symbol override
- DTE, including 30 DTE
- Strategy: PS, CS, IC, CB, PB
- Start date
- Saved scanner or custom Scanner Builder query
- Save backtest toggle
- Allow overlapping trades toggle
- Earnings-days avoid window

## Outcome logic

The backtest uses simple DTE close-based scoring. It does not compute option P/L.

For directional spreads, the entry spot is used as the short/reference strike.

Example:

- Entry date: 2026-05-01
- Spot: 136
- Strategy: PS
- DTE: 30
- Winner if the DTE close is at or above 136

Rules:

- PS credit bullish: winner if DTE close is at/above short put/reference strike.
- CS credit bearish: winner if DTE close is at/below short call/reference strike.
- IC credit neutral: winner if DTE close remains between the automatically created short put/call range.
- CB debit bullish: winner if DTE close moves through the upper short call.
- PB debit bearish: winner if DTE close moves through the lower short put.

Between-the-wings outcomes are marked as losses for simple winner/loser statistics because no dollar model is being used yet.

## Data

Daily OHLCV is read from `data/market_data.db` when available. Missing data can be filled from yfinance. This ZIP does not include any database files.
