# UAE Guide Trade Scanner

This build adds a separate scanner at `/uae-trade-scanner` and a new navigation item named **UAE Guide Scanner**.

It does not replace the existing `/trade-scanner` logic.

## What it uses

The scanner implements the uploaded UAE Trend/Vol Analyzer v4 logic in Python:

- MACD-like trend/vol line from ATR-normalized trend momentum
- smoothed ADX regime filter
- strong-histogram percentile filter
- BULL, WEAK_BULL, SIDEWAYS, WEAK_BEAR, BEAR regimes
- triangle/circle/diamond signal confluence

## DTE-aware timeframe selection

- 7 DTE: 15m entry, 1H filter, 4H context
- 14/21 DTE: 1H entry, 4H bias, Daily context
- 28/35 DTE: 4H entry, Daily bias, Weekly context
- 45 DTE: Daily entry, Weekly macro bias

## Pre-trade checklist scoring

Each opportunity receives an 8-part checklist:

1. Higher-timeframe regime alignment
2. ADX above threshold and rising
3. Signal confluence count: triangle, circle, diamond
4. Support/resistance room
5. Earnings and macro event risk
6. Histogram bar direction
7. Reward/risk ratio, default target 1:1 with a configurable minimum RR and a default 0.45 short-delta selection
8. SPY/index GEX or strike OI/OI-change proxy

## Option and OI enrichment

For the suggested DTE/expiry and strikes, the scanner displays:

- selected expiry and actual DTE
- suggested legs
- short strike delta estimate
- credit/debit estimate
- max loss estimate
- estimated RR
- short strike open interest
- short strike OI change from the local SQLite option snapshots when at least two snapshots exist
- SPY/index GEX context when local OI rows are available

If OI change is blank, run the existing option data Scheduler on at least two different days so the SQLite `options` table has prior and latest snapshots.

## v8 fast scan / lazy detail changes

The UAE Guide Scanner now uses a two-step design for speed:

1. **Fast first pass** downloads OHLCV in batches by timeframe and scores symbols using the guide rules. It does not request yfinance option chains for every watchlist symbol.
2. **Lazy exact detail** runs only when a result tile is expanded. That detail request fetches the exact option chain, computes the exact spread/strike/OI values, and renders the call/put OI graph using the same call/put OI concept as the main Dashboard.

The page defaults to `SPY`. To scan a watchlist, clear the Symbol field and select a watchlist. `Max Symbols` limits large watchlists so scans do not hang on hundreds of yfinance requests.
