# V94 - Watchlist yfinance futures symbol support

## Problem
Commodity futures symbols pasted into Watchlists, such as `GC=F`, `CL=F`, and `ZC=F`, were not saved from the UI. The frontend symbol parser only allowed letters, numbers, dot, dollar, and dash, so yfinance futures/FX suffixes containing `=` were silently dropped.

## Fix
- Watchlist UI parser now accepts yfinance-style symbols containing `=` and `^`.
- Server-side watchlist routes now use the same symbol normalization so direct API calls also work.
- Watchlist add/replace/import/bulk-delete endpoints now return rejected symbols instead of silently dropping everything.
- Watchlist form placeholder now includes examples such as `GC=F`, `SI=F`, and `^VIX`.
- Price-only watchlist fetch now handles missing/NaN volume values safely, which is common for some futures symbols.

## Accepted examples
- Stocks/ETFs: `AAPL`, `SPY`, `BRK-B`, `BRK.B`
- Crypto/FX style: `BTC-USD`, `EURUSD=X`
- Indices: `^VIX`, `^GSPC`
- Futures: `GC=F`, `SI=F`, `CL=F`, `NG=F`, `ZC=F`

## Note
Commodity futures do not generally have the same equity-options OI workflow in this app. Use a price/volume watchlist for these symbols unless you specifically want to test options fetching behavior.
