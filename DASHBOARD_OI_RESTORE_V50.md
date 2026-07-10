# v50 Dashboard OI restore

Fixes a regression where the Dashboard Options ΔOI chart and Futures OI panel could show no data after the expanded futures changes.

## Changes

- Options ΔOI endpoint now normalizes option type (`call`/`put` vs `C`/`P`) and strike keys before calculating day-over-day OI change.
- Options ΔOI chart now uses Plotly like the main OI chart, avoiding silent blank canvases when Chart.js fails or a canvas is hidden/resized.
- Options ΔOI no-data states now show an explicit message instead of an empty panel.
- Futures OI reader now discovers already-stored contracts in the existing SQLite DB even if older rows were stored with or without a leading slash in the contract symbol.
- Futures OI reader now falls back to stored contracts if the generated active-contract list changes after a code update.
- `/api/futures/analysis` now uses the same unified reader as `/api/futures/chart_data`, preventing one dashboard section from showing data while another says no data.
- Dashboard futures chart now falls back to the first contract with stored rows if the generated front contract has no visible history.

## Packaging

Source-only package. Database/cache files are excluded.
