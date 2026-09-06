# Auto Trading - Schwab EOD Stop/OCO

This package adds a separate Auto Trading section at `/auto-trading`.

The workflow is broker-resident and does not require the app to stream prices for many symbols during the day:

1. Run an end-of-day scan over a selected watchlist or manual symbols.
2. For each symbol, compute EMA5 on daily closes.
3. A valid signal candle must have low > EMA5 and body ratio above the configured threshold.
4. Build a next-session short entry stop or stop-limit order at the signal candle body low.
5. Attach an OCO child group: buy-to-cover target limit and buy-to-cover stop loss.
6. Parent order defaults to DAY; OCO child orders default to GOOD_TILL_CANCEL.

Actual Schwab submission is guarded by `OIAPP_AUTOTRADE_LIVE_ORDERS=1`. Without that flag, the Submit button can dry-run/preview payloads but will not send orders.

New tables:

- `auto_trading_eod_runs`
- `auto_trading_eod_candidates`
- `auto_trading_events`

New files:

- `oiapp/autotrading/ema5_strategy.py`
- `oiapp/autotrading/schwab_eod.py`
- `templates/auto_trading_schwab.html`
