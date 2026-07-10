# Journal / Alert Live Pricing + Entry Formatting v80

## Changes

- Journal and alert position valuation now uses live-first option marks.
- Live yfinance bid/ask mid prices are attempted first for `/journal/trade/<id>/live`, PNR alerts, health alerts, AI alerts, and custom position-alert checks.
- If the live provider chain is unavailable or hits a NaN parser issue, the app falls back to the latest local `options` table snapshot.
- Repeated provider parser errors no longer show the raw `cannot convert float NaN to integer` text repeatedly.
- Live option-chain cache uses `OPTION_PRICES_LIVE_TTL_SECONDS` with a default of 60 seconds.
- Journal open/closed grids display entry and exit price fields rounded to 2 decimals.
- New and edited journal trades store `entry_price` rounded to 2 decimals for cleaner display.

## Config

```bash
OPTION_PRICES_LIVE_TTL_SECONDS=60
```

Increase this if yfinance rate limits; decrease it if you want more frequent refreshes.
