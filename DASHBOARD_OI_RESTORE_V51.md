# v51 Dashboard OI restore

Fixes Dashboard visibility for:

- Options delta OI chart when the immediate prior snapshot is identical/stale.
- Futures OI panel when rows exist in the legacy futures_oi table or when the ladder has current rows but the richer daily-series reader cannot build a series.

The options delta OI API now compares the latest snapshot to the most recent prior snapshot that actually differs, while clearly returning the comparison date and note.

The futures reader now includes legacy CME/fallback rows from futures_oi and the dashboard synthesizes a one-point series from contract-table rows as a compatibility fallback. This keeps Dashboard and Aggregate Futures OI aligned.
