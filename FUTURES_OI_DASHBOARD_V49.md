# v49 Futures OI Dashboard Cleanup

## Fixes

- Recomputes daily OI change from stored OI history before displaying futures OI.
- Cumulative OI change is now calculated from total curve OI versus the prior stored date.
- Front-contract and cumulative charts display the latest delta in the subtitle and latest-bar label.
- Dashboard no longer shows the large all-futures sentiment grid by default.
- Selected futures sentiment is now shown as a compact row inside the Futures OI panel.

## Display

The Futures OI section now shows:

1. selected root sentiment score / COT overlay when available
2. immediate/front contract OI chart
3. cumulative OI across contracts chart
4. compact contract ladder

The hidden all-symbol COT renderer remains in the code for compatibility and scheduler refreshes, but it no longer takes dashboard real estate.
