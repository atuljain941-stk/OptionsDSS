# AI Hub Scanner Router Upgrade

This build expands the Conversational AI Hub from a small set of hardcoded intents into a scanner-aware router.

## What changed

### OI Buildup natural-language routing

AI Hub now recognizes requests such as:

```text
can you run OI buildup for ST as 3 days mt as 10 days and lt as 30 days and suggest stocks for which 30 days OI trend is not aligned to 3 or 10 days OI trend
```

It parses the requested horizons:

- ST = 3 days
- MT = 10 days
- LT = 30 days

Then it runs the reusable OI buildup scanner and filters for symbols where the LT OI trend does not align with ST and/or MT.

### Shared OI buildup primitive

Added:

- `oiapp/scanners/oi_buildup_scanner.py`

This module is framework-free and can be called by:

- AI Hub
- Dashboard API
- future scheduled jobs
- future agentic scanner-query planner

The dashboard endpoint `/api/oi_buildup_screener` now calls this shared primitive first, while retaining the older implementation as fallback.

### Scanner Builder query planner

AI Hub can now route additional scanner-style questions to Scanner Builder primitives:

```text
Run First Pullback After Breakout
Run scanner where rsi14[1d] < 35 AND close[1d] > ema20[1d]
query: OIChangePct(30) > 5 AND RSIDiff90() > 10
```

It can match saved/built-in scanner names or run explicit Scanner Builder DSL queries directly against the selected watchlist.

### New AI Hub intents

- `oi_buildup_scan`
- `oi_buildup_divergence`
- `scanner_builder_query`

### Output details

OI divergence rows include:

- symbol
- ST/MT/LT day windows
- ST/MT/LT OI percent changes
- ST/MT/LT OI trend labels
- ST/MT/LT price/OI outlooks
- LT-vs-ST and LT-vs-MT alignment flags
- divergence score
- suggested action
- bias score, PCR, total OI, expiry, and data dates

## No-guess behavior

The AI Hub still does not invent results. If the local `options` table has no OI history for the selected universe, it returns a data-missing response and identifies the missing data path.
