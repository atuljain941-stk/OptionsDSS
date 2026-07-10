# Backtest Replay Lab v102 - path-risk and trade ledger fixes

## Why this build exists

v101 produced suspicious 100% Seller Flow win rates because Seller Flow candidates did not have a real target expiry. The outcome engine fell back to the entry date, so OTM credit spreads were effectively tested on the same day's close. That made many trades look like instant winners and showed 0.00% average move.

## Fixes

- Seller Flow now assigns a real target expiry/DTE.
- The replay engine loads future price bars beyond the signal date range for outcome scoring only.
- Signal generation still uses only data available as of each replay date.
- Added path-risk outcome mode:
  - conservative: short-strike touch during the hold counts as a loss
  - balanced: expiry result with path-breach warning
  - expiry-only: old close-at-expiry style
- Added overlapping-position controls:
  - block same symbol
  - block same symbol + module
  - block same symbol + strategy
  - allow overlaps
- Added maximum open position control.
- Added full candidate signal log.
- Added detailed trade ledger fields:
  - actual trade description
  - target expiry
  - strikes/legs
  - entry/exit underlying
  - days held
  - max/min underlying during hold
  - path breach
  - MAE/MFE
  - exit check

## Saved runs

Saved Replay Lab runs now include both executed trades and the candidate signal log through a new `backtest_signals` table in the local market-data SQLite store.

## Notes

GEX replay still uses a daily OHLC proxy. Precise 10:00 / 1H candle confirmation requires intraday bars and remains a future precision layer.
