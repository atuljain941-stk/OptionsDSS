# Weekly Plan OI Alignment + Price Action Fix V70

## Problem fixed

The Weekly Plan and AI Hub could disagree with the OI / Aggregate screens because they used different OI sources and different wall-selection rules. In the SPY 2026-06-26 case, the context panel and weekly scorer could be pulled toward deep all-expiry walls such as 505/555/565, while the visible single-expiry and active-week aggregate OI screens showed the actionable weekly walls around the current price.

That produced weak strategy recommendations because the model was not consistently using:

- the selected weekly expiry,
- the cumulative listed expiries through the target Friday,
- actionable OI wall clusters near spot,
- daily + weekly Bollinger/Keltner stretch/range context,
- and the same OI data shown in the UI.

## Shared weekly OI source of truth

Added `oiapp/services/weekly_oi.py`.

This module centralizes:

- future expiration discovery,
- target-expiry latest option rows,
- active-week aggregation across listed expiries through target expiry,
- strike-level max pain,
- actionable wall selection around spot,
- raw/deep wall retention for diagnostics,
- option mid-price lookup for target-expiry ATM straddle.

The Aggregate screen, Weekly Plan, AI Hub fallback, and futures/options context now share the same weekly OI primitives.

## Actionable walls versus raw walls

The model now separates:

- **raw walls**: largest OI across the selected expiries, retained for context only;
- **actionable walls**: strikes near current spot, aligned with the Aggregate screen, used for weekly strategy scoring.

Deep walls are not deleted, but they can no longer become the short strike or primary support/resistance for a 5-DTE weekly plan.

For near-weekly expiries, actionable wall selection now applies a DTE-aware band around spot. This prevents high raw OI at very distant strikes, such as 800C or 565P when SPY is trading around 747, from driving weekly strategy selection.

## Aggregate screen alignment

Updated `oiapp/services/aggregate.py` so `/api/aggregate_strike` uses the shared weekly OI source first. This keeps the Aggregate chart and Weekly Plan from showing different OI values or walls for the same symbol/expiry/count.

## Weekly Plan context panel fix

Updated `oiapp/services/cftc_cot.py` and `oiapp/static/app.js` so the Weekly Plan context panel passes the selected target expiry. It now reports:

- real cached spot instead of OI-weighted strike estimates,
- selected expiries,
- actionable call/put walls,
- raw walls separately,
- pcr_actionable and pcr_all,
- source = `weekly_oi_context/actionable weekly aggregate`.

## BB/Keltner and price-action scoring

Updated `oiapp/scanners/spy_strategies.py` so Weekly Plan scoring uses:

- weekly, daily, 4H, and 2H trend profiles,
- Bollinger position and stretch state,
- Keltner channel state,
- BB-inside-KC squeeze,
- KC-inside-BB expansion/range-premium context,
- wall/candle alignment,
- expected move and actionable weekly wall clusters.

When price is stretched into upper-band/call-wall pressure, the scorer penalizes bullish put-spread chasing and boosts bear-call/iron-condor alternatives when the OI map and expected move support them.

## OI walls are pressure zones

The weekly strategy engine now treats put/call walls as pressure zones, not exact short strikes. For example, if active-week OI shows put walls around 740/745 and call walls around 750/755, the engine can select structures outside those clusters such as:

- iron condor outside the two-sided wall zone,
- bear call spread above the call-wall cluster,
- bull put spread below the put-wall cluster.

## AI Hub fallback alignment

Updated AI Hub’s fast weekly fallback so it uses an actionable near-spot wall band and keeps raw/deep walls as diagnostics only. This prevents stale deep OI from overriding visible weekly OI structure.

## Validation

- Python compile validation passed.
- Frontend JavaScript syntax validation passed.
- Synthetic weekly OI validation confirmed that a deep raw put wall remained visible as raw context while actionable weekly walls near spot were selected for strategy scoring.

## Files changed

- `oiapp/services/weekly_oi.py`
- `oiapp/services/aggregate.py`
- `oiapp/services/cftc_cot.py`
- `oiapp/scanners/spy_strategies.py`
- `oiapp/ai/ai_hub.py`
- `oiapp/static/app.js`
