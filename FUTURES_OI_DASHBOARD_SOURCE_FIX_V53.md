# v53 - Dashboard Futures OI source and Options Delta OI cleanup

## Dashboard Futures OI

The Dashboard Futures OI widget now reads visible history only from the current daily futures OI store:

- `futures_oi_daily`
- source values: `schwab` or `cme`

It no longer displays old rows whose source is a legacy/proxy source. This prevents the Dashboard from showing historical points that did not come from the current Schwab/CME daily OI pipeline.

The widget also shows a small history note with the table name, row count, and date range so the user can tell exactly why older daily points are appearing.

## Options Delta OI

The Dashboard Options Delta OI chart now compares the latest option OI snapshot against the previous distinct OI snapshot. If the immediately prior stored date has the exact same OI state, it is skipped silently and the chart title shows the actual comparison dates.

The old yellow warning about identical prior snapshots was removed because it was confusing. The API still returns skipped dates for diagnostics.

## CFTC COT

CFTC COT remains a weekly positioning overlay. It is not daily OI. The selected-future sentiment panel combines Schwab/CME daily OI trend with the latest COT index/net positioning when COT rows are available.
