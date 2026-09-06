# Futures OI stale-fetch fix

This build fixes the Scheduler/Futures OI flow where the UI could show old Schwab futures OI rows after clicking **Fetch OI Now**.

## Root cause

The old flow started the Schwab fetch in a background thread and immediately polled `/api/futures/fetch_status`. If stale rows already existed in `futures_oi_daily`, the frontend treated those old rows as a successful fetch and stopped polling before the new Schwab request finished.

That is why clearing the table made the fetch appear to work: with no old rows, the frontend kept polling until new rows arrived.

## Fixes

- `/api/futures/fetch_now` now returns a `job_id`.
- `/api/futures/fetch_status?job_id=...` returns the current fetch-job state.
- The frontend now waits for the current job to finish instead of stopping when it sees any old rows.
- Fetch status now reports only currently active futures contracts, so expired/stale contracts do not keep the Scheduler badge stale forever.
- `futures_oi_daily` now has a safe optional `fetched_at` column for diagnostics.
- Day-over-day `oi_change` now compares against the previous stored trading date, not another snapshot from the same date.
- The 7 AM scheduled Schwab fetch now iterates the configured Schwab root map instead of using the invalid `CL` symbol directly.

No database file is included in this package. Existing user DBs are upgraded in place; no clearing is required.
