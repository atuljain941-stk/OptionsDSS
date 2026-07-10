# Agentic AI Price/OI Guardrails V69

This update adds a final trade-quality gate to the Agentic AI background scanner before a finding can be persisted and alerted.

## Why

A candidate can score highly from market regime, sector regime, relative strength, and UAE trend alignment while still being a poor option recommendation.  The example that motivated this patch was a bullish ARM bull-put spread where:

- daily price action was strong,
- weekly price action was stretched near the upper band,
- cumulative option OI through the target expiry was call-heavy,
- the selected short put had almost no OI,
- the recommendation still went out as `OPEN`.

That should not happen.  The scanner must confirm that the chosen option structure is supported by strike-level OI and does not conflict with stretched price action.

## New guardrails

The Agentic scanner now checks:

1. **Daily/weekly Bollinger/Keltner context**
   - upper/lower band extension,
   - BB/KC range-premium context,
   - prior-high/prior-low proximity,
   - extension risk for bullish or bearish entries.

2. **Cumulative option OI through target expiry**
   - aggregate call OI and put OI from all expiries through the selected expiry,
   - top call walls above spot,
   - top put walls below spot,
   - PCR and call/put pressure ratios,
   - target-expiry and aggregate max-pain where available.

3. **Short-strike OI quality**
   - selected short strikes must have meaningful target/aggregate OI,
   - short strikes that are far from the active side wall are capped or blocked,
   - very thin short-leg OI can suppress the alert.

4. **Price-action/OI conflict rules**
   - bullish PS is downgraded or blocked when weekly price is stretched and call OI dominates overhead,
   - bearish CS is downgraded or blocked when downside price is stretched and put OI dominates support,
   - IC requires a range-premium context and two-sided walls.

5. **Earnings-window conflict**
   - single-name swing credit trades are blocked when earnings fall inside or very near the holding window.

## Alert behavior

If the final guardrail caps confidence below the configured alert threshold, or caps the recommendation to `WATCH`, the candidate is not sent as a new Agentic AI trade alert.

The alert rationale now includes:

- cumulative OI summary,
- price-action summary,
- quality-gate notes,
- setup guardrail notes,
- guardrail-agent summary when a finding survives with reduced confidence.

## Files changed

- `oiapp/scanners/agentic_ai_scanner.py`
- `oiapp/scanners/agentic_guardrails.py`

## Configuration

Optional environment variable:

```bash
AGENTIC_AI_MIN_SHORT_STRIKE_OI=25
```

This controls the absolute minimum short-strike OI floor used by the Agentic quality gate.  Dynamic relative OI checks are also applied against the active side wall.
