# Scanner Builder UAE Lookback/Data Alignment v85

## Issue
A query such as:

```text
Lookback(UAETrendTriangle("bull", "1w"), 2)
OR Lookback(UAETrendTriangle("bear", "1w"), 2)
```

could return a symbol even though the visible TradingView weekly triangle was much older than 2 bars.

## Cause
The generic Lookback logic was already bounded to the requested number of bars for UAE marker primitives, but Scanner Builder history could still come from a separate yfinance feed. If that feed was stale or missing recent weekly bars, a 6-7 week old triangle in the chart could be near the tail of the scanner's internal weekly series and therefore look recent to the scanner.

## Fixes

1. Scanner Builder now uses local app data first for 1D/1W/1M scanner history:
   - `data/market_data.db.market_bars` when present
   - `options_data.db.price_cache` when present
   - yfinance only as fallback

2. Weekly/monthly scanner history is resampled from local daily OHLCV so UAE weekly lookbacks line up with the same dashboard/chart cache.

3. Stale provider fallback is rejected by default for 1D/1W/1M when local data is unavailable. Set this only if needed:

```bash
SCANNER_ALLOW_STALE_YF_HISTORY=1
```

4. Added diagnostic primitive columns:

```text
UAETrendTriangleAge("bull", "1w", 10)
UAETrendTriangleAge("bear", "1w", 10)
UAETrendTriangleDate("bull", "1w", 10)
UAETrendTriangleDate("bear", "1w", 10)
UAELastMarker("1w")
UAELastMarkerAge("1w", 10)
```

5. The Scanner Builder match reason now includes the UAE marker age/date when a Lookback() call matches a UAE visible marker.

## Recommended validation query

```text
Lookback(UAETrendTriangle("bull", "1w"), 2)
OR Lookback(UAETrendTriangle("bear", "1w"), 2)
```

Add these result columns:

```text
UAETrendTriangleAge("bull", "1w", 10)
UAETrendTriangleDate("bull", "1w", 10)
UAETrendTriangleAge("bear", "1w", 10)
UAETrendTriangleDate("bear", "1w", 10)
UAELastMarker("1w")
UAELastMarkerAge("1w", 10)
```

AMD should not pass this query if the latest visible weekly triangle is 6-7 bars old.
