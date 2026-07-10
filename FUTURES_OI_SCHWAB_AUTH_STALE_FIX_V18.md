# Futures OI Schwab Auth / Stale Data Fix v18

This update fixes the manual Futures OI fetch flow so stale cached rows are not mistaken for newly fetched data.

## Main changes

- Futures OI manual fetch now treats Schwab as the real source of truth.
- yfinance volume proxy fallback is disabled by default because it is not real exchange open interest.
- The UI now shows a clear failed-fetch state if Schwab auth/token refresh fails.
- Existing cached/stale rows are left in the database for reference, but the UI labels them as stale if the current fetch did not update them.
- Schwab refresh-token failures now return useful error details instead of only `Failed: 400`.
- The Schwab service now tries to refresh an expired access token automatically before fetching quotes.
- Schwab quote responses that contain no usable `openInterest` no longer overwrite older good OI rows with zero values.

## Optional diagnostic fallback

The old yfinance volume proxy can still be enabled only for manual diagnostics by calling:

```text
/api/futures/fetch_now?proxy_fallback=1
```

Do not use that for real OI decisions. yfinance volume is not futures open interest.

## If refresh token fails with HTTP 400

Re-authorize Schwab from the Settings/Scheduler tab, then run Fetch OI Now again.
