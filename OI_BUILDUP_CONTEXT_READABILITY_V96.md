# OI Buildup / Seller Flow Context Readability v96

This build enhances the Seller Flow scanner response/card with the missing decision context:

- IV rank / volatility proxy and whether the setup favors selling premium, buying options, or waiting.
- Sector ETF trend check and sector-vs-flow alignment.
- Price-action context from cached bars: EMA, RSI, RSIDiff90, Bollinger/Keltner state, and short-term/20-day change context.
- UAE Trend/Vol context uses the shared UAE primitive engine first, then a local cached-bar fallback when possible.
- Window cards show current max pain / current RR or OI-skew proxy when historical shift cannot be calculated, instead of leaving the user with unexplained n/a fields.
- Cached Load Saved rows are repaired with the latest spot, walls, IV/sector/price/UAE context before display.
- Seller Flow font sizing was increased for better readability.

No live data calls are made by the Seller Flow scan; all values come from local SQLite caches/options snapshots.
