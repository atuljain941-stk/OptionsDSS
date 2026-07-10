# OI Buildup / Seller Flow Context Fix v97

## Fixed

- Seller Flow cards no longer show `IV skew / n/a` when no usable skew value was computed. If true IV skew is missing but strike-level OI exists, the card uses an explicitly labelled OI-skew proxy.
- UAE context now has a stronger local fallback. If the Scanner Builder UAE engine cannot load local bars, Seller Flow computes daily/weekly UAE-lite context from cached price bars or option underlying snapshots.
- Price action, IV-rank proxy, and sector trend enrichment now tolerate older or partial `price_cache` schemas.
- Seller Flow can refresh live/cached spot prices for the first visible cards via `/api/spot_batch` without running a heavy watchlist-wide quote job.
- Seller Flow UI has a `Live spot` button in the toolbar. The page also refreshes visible spot values opportunistically after rendering.

## Data source rules

- IV skew requires stored option IV or enough option price/spot history to estimate IV.
- If true IV skew is unavailable, the scanner falls back to OI-skew proxy and labels it as such.
- UAE context first uses the shared Scanner Builder UAE primitive engine, then local cached OHLCV, then historical `options.underlying` snapshots.
- Live spot refresh is capped to visible cards so a 198-symbol Seller Flow scan does not become a slow live-quote job.
