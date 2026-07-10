# V77 - GEX Snapshot Retention and Price Alert Grid Refresh

## GEX Plan snapshot retention

The GEX Plan snapshot panel now treats saved GEX Plan runs as an intraday UI cache.
On every save/load it deletes stale rows from prior local trading days.

Default behavior:

- keep only the latest GEX Plan snapshot for today per symbol
- delete older days automatically
- insert new snapshots using local Python timestamp instead of SQLite UTC timestamp
- return retention metadata from `/spy/daily_plan` and `/spy/daily_plan_snapshots`

Optional override:

```bash
GEX_PLAN_TODAY_SNAPSHOT_LIMIT=3
```

Set the value above 1 if multiple intraday snapshots should be retained.

## Price alert delete refresh

The symbol price-alert delete flow now:

- clears the visible row immediately after a confirmed delete
- clears watchlist symbol alert controls immediately
- reloads the symbol alert hub with a cache-busting URL
- reloads the watchlist symbol panel without blocking the alert grid refresh
- reloads the alert-rule grid as a secondary safety refresh
- uses `fetch(..., {cache: 'no-store'})` for GET API calls by default

This fixes the issue where an alert was deleted in the database but remained visible in the grid until manual page refresh.
