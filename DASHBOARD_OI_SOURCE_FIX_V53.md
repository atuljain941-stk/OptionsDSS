# v53 Dashboard OI source fix

This update fixes two Dashboard OI display issues:

1. Futures OI now reads the current daily history table only (`futures_oi_daily`) for the Dashboard/Aggregate Schwab OI widgets.  The legacy `futures_oi` table is no longer mixed into the Dashboard chart data, which prevents older CME/proxy rows from appearing as if they came from the current Schwab OI table.
2. Options delta-OI chart rendering now clears the loading placeholder before Plotly draws the chart and uses clearer comparison text when a stored snapshot is unchanged.

CME fallback rows are mirrored into `futures_oi_daily` when the three-layer fetch uses CME, so the UI still has one source-of-truth table for futures OI history.

CFTC COT remains a weekly macro positioning overlay.  It is used for selected-future sentiment only; it does not replace daily Schwab OI or drive the OI charts.  The combined sentiment uses daily Schwab cumulative OI/price trend plus weekly COT large-spec positioning when COT data is available.
