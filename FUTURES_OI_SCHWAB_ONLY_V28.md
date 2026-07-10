# Futures OI Schwab-only display fix (v28)

Dashboard and Aggregate futures OI now read Schwab real daily OI from `futures_oi_daily` only.

- Aggregate cumulative futures OI no longer triggers the legacy yfinance/volume-proxy fetch.
- `/api/futures/all_contracts` returns Schwab OI rows only.
- `/api/futures/chart_data` ignores yfinance/volume-proxy rows.
- Dashboard title now says Schwab Real OI.
- If Schwab OI is missing, the UI asks you to connect Schwab and run Scheduler -> Fetch OI Now.

No database files are included.
