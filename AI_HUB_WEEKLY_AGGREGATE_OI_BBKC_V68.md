# AI Hub Weekly Aggregate OI + BB/Keltner Strategy Fix V68

This build updates AI Hub weekly strategy selection for SPY/QQQ/IWM-style liquid weeklies.

## Why
The prior fast weekly path could still lean on a single-expiry OI wall and miss the range-premium setup visible from the dashboard aggregate view. For SPY daily expiries, the best weekly context often comes from cumulative OI across all expiries in the week, not only the requested Friday expiry.

## New weekly strategy path
For near-weekly liquid symbols, AI Hub now uses a DB-first weekly strategy engine before trying the slower dashboard Weekly Plan route.

The engine uses:

- Requested-expiry option chain for pricing.
- Cumulative weekly strike OI from all local expiries through the requested expiry for wall detection.
- Call and put OI wall clusters instead of a single raw wall.
- Daily and weekly Bollinger Band / Keltner Channel context from `price_cache`.
- Prior-high / upper-band context for call-credit risk.
- Local futures OI context when ES/NQ/RTY/YM proxy data exists.
- Max pain from target-expiry and aggregate weekly OI.

## Strategy selection changes
AI Hub now ranks:

- Bull Put Spread
- Bear Call Spread
- Iron Condor

For range-style SPY weeks, IC can now win when:

- cumulative put and call walls bracket spot,
- price action is not in a fresh breakout state,
- BB/Keltner context supports range premium,
- futures OI is neutral or not strongly conflicting,
- expected move is contained between the selected short strikes.

Bear call spreads are also scored as alternatives when call-wall clusters and prior-high / upper-band context support an upside cap.

## Strike behavior
The model no longer sells directly at close OI walls. It treats wall clusters as pressure zones and selects short strikes outside the cluster:

- Put credit side: short put below the put-support cluster.
- Call credit side: short call above the call-resistance cluster / prior-high zone.
- Iron condor: combines both sides when range context is valid.

For example, a SPY map with cumulative call walls around 750/755 and put walls around 740/745 can now surface an IC or a 760/765 call spread candidate instead of a far, low-credit put spread.

## AI Hub answer changes
The answer now includes:

- aggregate OI wall summary,
- price-action BB/Keltner summary,
- futures OI note,
- max pain context,
- alternate weekly candidates.

The AI Hub result table now renders `all_candidates` for best-strategy and weekly-plan answers, so alternate IC/CS/PS candidates are visible instead of only the selected row.

## Files changed

- `oiapp/ai/ai_hub.py`
- `templates/ai_hub.html`

## Data policy
No `.db`, `.sqlite`, `.sqlite3`, `.pyc`, `.pyo`, or `__pycache__` files are included in the ZIP.
