# Dashboard Futures OI Render Fix v52

Fixes a Dashboard-only rendering failure where Aggregate could show stored Schwab Futures OI but the Dashboard panel showed no futures data.

Root cause:
- The Dashboard futures ladder renderer used `esc(...)`, but `esc` was not globally defined in that JavaScript scope.
- That threw a client-side render exception after the API returned valid futures OI rows, causing the Dashboard panel to fall back to the generic no-data message.

Changes:
- Added a local/global `_htmlEsc(...)` helper for the Dashboard futures widget.
- Updated the futures contract ladder to use `_htmlEsc(...)`.
- Added a Dashboard fallback that adapts `/api/futures/all_contracts` data if `/api/futures/chart_data` returns no visible rows or errors.
- Added console logging and a clearer UI error if rendering fails again.

No database files are included in this source-only package.
