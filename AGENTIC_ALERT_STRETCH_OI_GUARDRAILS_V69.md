# Agentic Alert Stretch/OI Guardrails V69

This build tightens Agentic AI trade alerts so high market/sector alignment and strong RS do not automatically become an `OPEN` alert when the selected options structure is weak.

## Why this was added

Example issue: ARM produced a bullish `PS 415/410` alert even though:

- Daily looked strong, but weekly was stretched.
- The selected short put had only ~1 OI.
- Cumulative OI into the target expiry showed heavy call-side pressure.
- The selected PS was not clearly anchored by a meaningful put wall.

That should be treated as `WATCH` or skipped, not a normal `OPEN` alert.

## New checks

Agentic AI now runs a final quality gate before persisting or alerting a finding:

1. **Daily/weekly BB/Keltner extension**
   - Detects upper-band / lower-band extension.
   - Bullish PS alerts are penalized when price is stretched near/above upper bands.
   - Bearish CS alerts are penalized when price is stretched near/below lower bands.

2. **Cumulative OI through selected expiry**
   - Sums strike-level OI from current date through selected expiry.
   - Finds top cumulative call/put walls.
   - Measures call-overhang and put-support ratios.

3. **Short-strike OI anchor validation**
   - Checks whether the selected short strike has meaningful target/cumulative OI.
   - A short strike with extremely low OI is blocked from becoming an `OPEN` finding.
   - The UAE exact-chain score now caps credit-spread scores when the short strike has very low OI.

4. **Price/OI conflict blocking**
   - Bullish PS is blocked when all of these align:
     - price is stretched upward,
     - call-side OI overhang is heavy,
     - short put is weakly anchored.
   - Bearish CS is blocked symmetrically when downside price stretch, put support, and weak call anchor conflict.

## Alert behavior

- Blocked setups are not persisted as new Agentic findings and do not send Telegram/local alerts.
- Penalized setups can still appear only if final confidence remains above the alert threshold.
- `OPEN` now requires stronger quality-gate confirmation; marginal but valid setups become `OPEN_SMALL`.

## Files changed

- `oiapp/scanners/agentic_ai_scanner.py`
- `oiapp/scanners/uae_trade_scanner.py`

## Validation

Synthetic ARM-style validation confirmed that a bullish PS with:

- stretched bullish price action,
- heavy call-side cumulative OI,
- and a 1-OI short put

is blocked before alerting.
