# OI Buildup / Seller Flow Context Fix v97

This build fixes the remaining context gaps on the Seller Flow / OI Buildup cards.

## Changes

1. **UAE no longer depends only on `price_cache`**
   - Seller Flow now reads the same optional long-history `data/market_data.db.market_bars` store used by Scanner Builder/backtests.
   - If `price_cache` has only recent rows, but `market_bars` has enough history, UAE daily/weekly regime, IV-rank proxy, and price-action context are computed from `market_bars`.

2. **Price-action / IV-rank context uses richer local history**
   - IV rank proxy, EMA/RSI/RSIDiff90, BB/Keltner state, and sector trend are now computed from the best local daily history available:
     - `data/market_data.db.market_bars`, when present;
     - `options_data.db.price_cache`, as fallback.

3. **Skew display fallback improved**
   - If true/current IV skew cannot produce a numeric current risk-reversal value, Seller Flow backfills the card from the latest aggregate OI-skew proxy.
   - The snapshot chip now shows values such as `IV skew +1.2 RR` or `OI-proxy -8.4 RR` instead of just a generic source label.

4. **Live spot option added**
   - Added a `Live spot` checkbox and `Price` button on the Seller Flow page.
   - It fetches latest spot only for visible cards via the existing `/api/spot` cached endpoint.
   - This does not rerun the scanner and avoids a heavy full-watchlist live fetch.

5. **`/api/spot` now returns metadata**
   - In addition to the legacy `spot` value, it also returns source, timestamp, previous close, and change percent.

## Notes

- Seller Flow still remains DB-first for OI/PCR/skew/max-pain calculations.
- Live spot is display-only; it does not rewrite historical OI calculations.
- If UAE still shows unavailable for a symbol, that means neither `market_bars` nor `price_cache` has enough historical OHLCV bars for that symbol.
