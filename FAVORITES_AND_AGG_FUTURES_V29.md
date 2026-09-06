# v29 - Aggregate Futures OI + Favorite Reorder

- Aggregate Futures OI now uses the same `/api/futures/chart_data` Schwab-only data source as the Dashboard.
- `/api/futures/all_contracts` is kept as a compatibility wrapper around the unified Dashboard futures-OI path.
- Aggregate symbol handling accepts ETF symbols and futures-root labels such as `SPY /ES` or `/ES`.
- Favorites in the smart nav can be reordered by dragging a favorite tab left or right.
- Favorite order is saved in browser localStorage.
- Source-only package: no SQLite, market-data, or backtest DB files included.
