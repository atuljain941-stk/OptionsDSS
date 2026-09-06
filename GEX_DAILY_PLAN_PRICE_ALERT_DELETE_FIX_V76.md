# GEX Daily Plan + Price Alert Delete Fix v76

## Fixes

### 1) GEX / Daily Plan 500 error
`/spy/daily_plan?symbol=SPY&expiry=YYYY-MM-DD` could raise:

```text
NameError: name '_pick_exp' is not defined
```

The daily plan route now includes local expiry helpers:

- `_expiry_dte(expiry)`
- `_pick_exp(expirations, min_dte, max_dte, fallback_index)`

Selected-expiry calls now compute DTE directly from the selected expiry date, so GEX Plan no longer depends on a missing helper.

### 2) Alert deletion reliability
Price/custom alert deletion was made more robust:

- `/watchlists/alerts/rules/<id>` still supports `DELETE`.
- Added POST fallback `/watchlists/alerts/rules/<id>/delete` for browser/proxy setups where DELETE is unreliable.
- Alert deletion now returns deleted row count.
- Frontend catches delete errors and shows a notification instead of silently failing.

### 3) Watchlist symbol price-alert delete
Watchlist symbol alerts now support direct clearing:

- `DELETE /watchlists/<watchlist_id>/symbols/<symbol>/alert`

The Alerts Hub and Watchlist symbol panel now include a Delete button for old-style symbol price alerts. This clears price, enabled state, and last-trigger state.

## Validation

- Python compileall passed.
- JavaScript syntax check passed.
- ZIP excludes `.db`, `.sqlite`, `.sqlite3`, `.pyc`, `.pyo`, and `__pycache__`.
