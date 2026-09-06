# Seller Flow Scanner v65 - max pain, skew, earnings, responsive UI

This build upgrades the OI Buildup screen into a seller-side Seller Flow Scanner.

## Signal model

The scanner no longer treats rising/falling aggregate OI as sufficient.  For each ST / MT / LT window it now combines:

- total OI change
- call OI change
- put OI change
- PCR point and percent change
- price context
- max-pain shift computed from historical strike-level OI snapshots
- skew / risk-reversal shift when IV or enough option pricing data exists
- explicitly labelled OI-skew proxy when true historical IV skew is not available
- earnings-calendar guardrails

## Historical max pain

Max pain is computed per symbol, snapshot date and nearest active expiry from strike-level call/put OI:

```text
pain(settlement) = sum(call_oi * max(0, settlement - strike))
                 + sum(put_oi  * max(0, strike - settlement))
```

The max-pain strike is the settlement strike with the lowest total payout.  ST / MT / LT max-pain change is the current max-pain strike compared with the historical snapshot for that window.

## Historical skew

True historical skew requires historical IV, or option price plus spot/expiry/strike/type so IV can be estimated.  When neither is available, the scanner uses an OI-skew proxy and labels the source as `oi_proxy`.  The UI shows the skew source for every row.

## Seller interpretation

- Put OI up + PCR up is treated as bullish only when price and skew do not suggest defensive put buying.
- Call OI up + PCR down is treated as bearish only when price and skew do not suggest upside call chasing.
- Max-pain shift is a secondary confirmation, not a primary signal.
- Earnings conflicts block suggested timeframes for single-name equities.

## Strategy and timeframe output

The scanner suggests candidate strategy/timeframe only after seller-flow direction is known:

- Bullish aligned flow: bull put credit spread below put support / max-pain support.
- Bearish aligned flow: bear call credit spread above call resistance / max-pain resistance.
- Stable/pinned neutral flow: iron condor only if price remains pinned and skew is stable.
- Mixed flow or earnings conflict: no new trade / wait.

Timeframes are mapped from window alignment:

- MT + LT aligned: 20-45D swing candidate.
- ST/MT tactical alignment: 7-21D candidate.
- Weak directional read: 3-10D watch-only.

The earnings guardrail avoids expiries that cross the cached earnings date.

## UI redesign

The OI Buildup page now renders as responsive cards instead of a wide table.  It removes horizontal scrolling and groups each symbol into:

- snapshot metrics
- ST / MT / LT flow cards
- final seller read
- strategy/timeframe
- earnings status
- expandable rationale/action details

## Changed files

- `oiapp/scanners/oi_buildup_scanner.py`
- `oiapp/api/routes.py`
- `oiapp/ai/ai_hub.py`
- `templates/index.html`
- `templates/index_mine.html`
- `oiapp/static/app.js`
- `oiapp/static/style.css`
