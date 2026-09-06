# v49 Futures OI Dashboard Simplification

Changes:

- Dashboard Futures OI now recomputes displayed OI change from stored OI history, so upgraded databases with stale/zero `oi_change` values still show day-over-day deltas.
- The large all-market Futures Sentiment/COT card was removed from the Dashboard.
- Selected-future sentiment is shown inside the compact Futures OI panel instead.
- The selected sentiment row includes Schwab OI trend, price trend, COT index, COT net, and COT delta when available.
- The Futures OI panel shows two charts side by side:
  - immediate/front expiring contract OI
  - cumulative curve OI across active contracts
- Latest front and cumulative OI subtitles include visible `Delta OI` values.

Notes:

- The full COT data still exists and can be fetched from Scheduler; it is no longer shown as a large grid on the Dashboard.
- No database migration is required. Existing history is reused.
