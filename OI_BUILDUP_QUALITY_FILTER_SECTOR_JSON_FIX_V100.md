# v100 - Seller Flow quality filter and Sector JSON fix

## Seller Flow / OI Buildup

Added a 5-level Trade Quality slider that filters only the visible cards. The full scan result set remains in memory/cache, so moving the slider back to level 1 restores all rows.

Levels:

1. All - show everything.
2. Watch+ - excludes very weak/noisy rows.
3. Setup+ - shows rows with usable setup quality, including conditional WAIT rows.
4. Tradable+ - shows actionable rows with enough confirmation and without hard guardrails.
5. Best only - highest quality rows where confidence, action, sector/UAE/price context, IVR and timeframe alignment are strongest.

Quality score uses:

- flow confidence
- earnings conflict
- WAIT/AVOID/WATCH action state
- suggested strategy availability
- UAE confirmation/conflict
- sector confirmation/conflict
- price action confirmation/conflict
- IV rank support for debit/credit approach
- ST/MT/LT seller-flow alignment
- support/resistance price-location guardrails

Hard blocked rows, such as earnings conflicts or price-location guardrails, are capped below Tradable/Best so they do not appear when the slider is set to 4 or 5.

## Sector page

Fixed invalid JSON caused by `NaN` / `Infinity` values in sector RSI and RSIDiff fields. Sector API responses are recursively sanitized so non-finite numeric values become `null`, which browsers can parse safely.

