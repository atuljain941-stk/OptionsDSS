# Seller Flow Context Fix v98

This build fixes the Seller Flow / OI Buildup cards that could show `IVR n/a`, `UAE unavailable`, and unclear skew labels even when the app had usable underlying data elsewhere.

## Changes

- Seller Flow now loads the same `data/market_data.db.market_bars` daily history source used by Scanner Builder before falling back to `price_cache` and `options.underlying`.
- IV rank now uses option IV history from the local `options.iv` column when available. Historical-volatility proxy is only a fallback.
- Added a visible-card `Refresh context` action. It fetches a small batch of daily yfinance history for the cards currently on screen, stores it in `price_cache`, and recomputes IVR, UAE, price action, sector trend, walls, max pain and skew labels for those rows only.
- Skew display now shows the current risk-reversal/proxy value in the card header, for example `IV skew RR +91.5` or `OI proxy RR -8.2`, instead of only showing a source label.
- Added `/api/oi_buildup_context_refresh` for lightweight row repair without rerunning the entire Seller Flow scan.

## Notes

True IV skew still requires option IV by strike. If the chain has no IV, the scanner labels the value as an OI-skew proxy. UAE requires daily/weekly price history; if not present locally, use `Refresh context` for visible cards.
