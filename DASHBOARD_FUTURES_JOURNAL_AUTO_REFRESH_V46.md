# v46 Dashboard Futures OI + Journal Auto Refresh

## Dashboard
- Replaced the separate Futures OI Signal and OI Buildup / Covering panels with one compact Futures OI panel.
- The compact panel shows two side-by-side charts:
  - Immediate/front expiring futures contract OI.
  - Cumulative OI across the active Schwab futures contracts.
- The summary still shows the roll/net OI read and Schwab real-OI status.
- The CFTC/Futures Sentiment panel remains separate and unchanged.

## Journal
- Added a Journal auto-refresh selector for live price/health preview refresh:
  - Off, 5m, 15m, 1h, 2h, 4h.
- Setting is saved in browser localStorage.
- Auto refresh only runs while the Journal Open Trades tab is active.
- Journal symbols are now links. Clicking a symbol opens the Dashboard for that symbol and preselects the trade expiry when available.

## Packaging
- Source-only package. No database/cache files are included.
