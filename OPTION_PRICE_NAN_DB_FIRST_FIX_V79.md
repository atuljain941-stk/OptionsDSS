# v79 Option Price NaN DB-first Fix

## Issue
The app could still print messages such as:

```text
[option_prices] MRVL/2026-08-21: cannot convert float NaN to integer
```

even after the first NaN patch. The remaining failure can happen before row-level
sanitization when yfinance internally encounters NaN option-chain values, or when
journal/alert refreshes repeatedly ask for current option values.

## Fix
`oiapp/services/option_prices.py` was hardened again:

- Local `options` DB snapshot is now used first by default.
- yfinance is used only if local data is missing or `OPTION_PRICES_PREFER_LIVE=1` is set.
- Every numeric field is sanitized before int/float conversion.
- yfinance failures are logged as `[option_prices][yfinance]` and throttled once per symbol/expiry/error.
- Spread pricing and strategy enrichment report whether prices came from local DB, yfinance, or partial estimates.

## Journal / alert refresh
Journal option-price and current-OI helpers now call the same hardened option-price service instead of calling yfinance directly.
This prevents health-alert / journal refreshes from repeatedly triggering yfinance NaN conversion errors.

## Related hardening
`store_option_chain()` and live volume helpers now sanitize pandas/numpy NaN values before converting OI or volume to integers.

## Optional settings
Default behavior is DB-first:

```bash
OPTION_PRICES_DB_FIRST=1
```

To force live yfinance pricing before DB fallback:

```bash
OPTION_PRICES_PREFER_LIVE=1
```

DB fallback still applies if yfinance fails.
