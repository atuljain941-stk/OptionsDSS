# Agentic AI Scanner V69 - Price Action + Aggregate OI Guardrails

This patch tightens Agentic AI trade alerts so a strong market/sector/RS score cannot by itself create an `OPEN` alert when the selected option structure is not supported by the actual option map.

## Why

A reported ARM alert selected a bullish put spread while:

- daily price action was strong, but weekly price action was stretched;
- cumulative option OI through the selected expiry was call-side heavy;
- the selected short put had very low OI;
- the short put was not cleanly below the lower edge of the put-support cluster.

Under the user's trading rules, that should not be a normal `OPEN` alert. It should be skipped or treated as watch-only until price pulls back into support or another structure is justified.

## Added guardrails

The Agentic scanner now adds a second-stage validation after UAE/exact-chain selection:

1. **Daily/weekly BB-Keltner extension context**
   - Flags bullish entries near weekly/daily upper-band extension.
   - Flags bearish entries near weekly/daily lower-band extension.
   - Identifies when BB/Keltner context favors range premium over trend chasing.

2. **Cumulative option OI through selected expiry**
   - Aggregates strike-level OI from today through the chosen expiry.
   - Calculates total call OI, put OI, PCR, top call walls, and top put walls.
   - Detects call-side overhead and put-side support conflicts.

3. **Selected short-strike quality**
   - For PS: verifies the short put is supported by meaningful aggregate put OI and is outside/below the put-support cluster.
   - For CS: verifies the short call is supported by meaningful aggregate call OI and is outside/above the call-resistance cluster.
   - For IC: checks both sides.

4. **Alert blocking**
   - A bullish PS is blocked when weekly price is stretched, call OI overhead is heavy, and the selected short put has weak support.
   - A bearish CS is blocked when downside price is stretched, put-side support is heavy, and the selected short call is weak.
   - Exact short-leg OI of 1 or lower combined with weak aggregate support blocks the alert.

## Stored evidence

New finding metrics include:

- `price_action_quality`
- `option_oi_quality`
- `option_setup_guardrail`
- updated `quality_gate`

Rationale text now includes price-action and setup-guardrail notes so the history explains why a candidate passed or failed.

## Packaging note

No database files are included in the ZIP package.
