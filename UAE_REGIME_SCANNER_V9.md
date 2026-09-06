# UAE Regime Scanner v9

This version simplifies the UAE Guide Scanner into a fast regime-only stock scanner.

## What changed

- Added a timeframe dropdown: 5m, 15m, 1h, 4h, Daily, Weekly.
- Added five regime checkboxes: Bull, Weak Bull, Bear, Weak Bear, Sideways.
- The scanner returns only symbols that match the selected timeframe + selected regimes.
- Each matching symbol still gets an overall 0-100 score and grade.
- Option strikes, RR, option-chain lookup, OI/GEX detail loading, and trade suggestions are removed from this UAE scanner view.

## Default behavior

- Default timeframe: Daily.
- Default regimes checked: Weak Bull and Bear.
- Leave Symbol blank to scan the selected watchlist or all symbols.
- Enter a Symbol to scan one ticker.

## Important scoring note

Weak Bull is treated as a bullish-trend pullback/watchlist state, not as a bearish trade signal.
Weak Bear is treated as a bearish-trend bounce/watchlist state, not as a bullish trade signal.
