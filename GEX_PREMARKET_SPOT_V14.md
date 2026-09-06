# GEX pre-market spot update

This build updates the GEX live dashboard and GEX plan price source logic so the spot price uses the freshest available yfinance quote:

1. latest pre-market bar before 9:30 ET,
2. latest regular-hours intraday bar during the session,
3. latest after-hours bar after 16:00 ET,
4. `fast_info` quote,
5. previous daily close as the final fallback.

The GEX Pine CSV keeps the same 20-field format; field 0 is now populated with the freshest spot price. The JSON response adds metadata:

- `spot_source`
- `spot_time`
- `spot_prev_close`
- `spot_change_pct`

The live dashboard displays the price source next to the update timestamp and in a small card.
