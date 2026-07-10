# OI Buildup / Seller Flow Context Fix v98

## Problem fixed
Seller Flow cards could still show `IVR n/a`, `Price n/a`, and `UAE unavailable` even when the app had usable option-chain snapshots. The main reason was that the context enrichment path only trusted `price_cache` or `data/market_data.db` OHLCV rows. Many deployments have rich strike-level OI plus `options.underlying` and `options.iv`, but no separate daily OHLCV cache for every options watchlist symbol.

## Changes

1. **Option-underlying fallback for price context**
   - Builds a daily close series from `options.underlying` by symbol/date.
   - Used only when richer `price_cache` / `market_bars` history is missing.
   - Prevents Seller Flow cards from showing `Price n/a` when option snapshots contain the underlying price.

2. **Option-IV rank fallback**
   - Computes IV rank from historical `options.iv` snapshots when available.
   - Uses ATM/near-spot IV when `underlying` is stored; otherwise falls back to OI-weighted chain IV.
   - Historical-volatility proxy remains the fallback when option IV history is unavailable.

3. **UAE-lite fallback can use option-underlying history**
   - If Scanner Builder UAE primitives cannot load cached bars, Seller Flow now uses the option-underlying daily series as a local fallback.
   - Daily context requires fewer bars than the full Scanner Builder history path and is labelled internally as fallback context.
   - This reduces unnecessary `UAE unavailable` cards.

4. **Latest aggregate skew context improved**
   - Latest aggregate wall context now carries aggregated IV and underlying fields instead of forcing `NULL`.
   - This allows top-level card skew/risk-reversal to be computed from the same latest aggregate OI context.

5. **Skew display clarified**
   - The card now shows a numeric RR/current value when available.
   - If only window shift exists, the card shows a delta instead of just `IV skew` with no value.

6. **Saved cache repair**
   - Load Saved now refreshes IVR, price action, UAE fallback, skew, max pain, and support/resistance from the improved context path.

## Data priority

For price/UAE context:
1. `data/market_data.db.market_bars`
2. `price_cache`
3. `options.underlying` fallback

For IV rank:
1. `options.iv` history
2. historical-volatility proxy from price/underlying bars

For skew:
1. true `options.iv` skew
2. price-implied IV skew when possible
3. explicitly labelled OI-skew proxy
