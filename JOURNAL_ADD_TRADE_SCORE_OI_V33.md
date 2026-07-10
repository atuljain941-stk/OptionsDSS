# Journal Add Trade v33

Source-only update. No database files are included.

Changes:

- Add Trade score preview now uses the same journal health-score logic as Open Trades.
- Added `/journal/draft_health_score` to evaluate an unsaved draft trade without saving it.
- Leg rows remain the source of truth for strikes, quantity, expiry, and net premium.
- First-leg quantity now cascades to the other legs for common strategies to reduce repeated typing.
- First-leg expiry now cascades to other legs for common same-expiry strategies, but not Calendar or Custom trades, so calendars remain supported.
- Added an Add Trade OI + IV chart by symbol/expiry. The chart shows Call OI, Put OI, Call IV %, and Put IV %. It does not display volume.
- Expiry remains row-level for legs; the chart has its own expiry selector that defaults from the first option leg.
- Close-trade and alert paths are not changed except for preserving the previous v32 spread P&L fixes.
