# Scanner Builder Breakout Recipes

These recipes use existing Scanner Builder primitives. They do not add new backend indicators.

## Controlled resistance breakout after consolidation

Use this when you want a breakout that came out of a coil rather than a vertical exhaustion move.

```text
scan(VCP Compression)
AND CrossAbove(close, Resistance(20, "1d"), "1d")
AND BreakoutStrength("1d") >= 0.75
AND volume[1d] >= ema(volume, 20) * 1.5
AND SlopePct(close, 10, "1d") < 1.4
AND SlopeATR(close, 10, "1d") < 0.35
AND UAEADXRising("1d")
```

Meaning:

- `scan(VCP Compression)` checks ATR/range compression and volume dry-up.
- `CrossAbove(close, Resistance(...))` confirms the resistance break.
- `BreakoutStrength()` checks the quality of the breakout candle.
- Volume must be at least 1.5x the 20-bar average.
- Percent slope and ATR-normalized slope are capped so the move is not too extended already.
- `UAEADXRising()` confirms trend strength is improving.

## Exhausted breakout / fallback risk

Use this as an avoid-list or caution scanner when price breaks resistance too vertically without participation.

```text
CrossAbove(close, Resistance(20, "1d"), "1d")
AND SlopePct(close, 10, "1d") > 1.4
AND SlopeATR(close, 10, "1d") > 0.35
AND volume[1d] < ema(volume, 20) * 1.2
AND (NOT UAEHistGrowing("1d") OR RSIDiff90("1w") > 20)
```

Meaning:

- Price did break resistance.
- The recent move is steep.
- Volume is not confirming the move.
- Momentum is no longer growing or weekly RS is already extended/overbought.

## Saved catalog examples

The v11 package adds these examples to the Scanner Builder catalog:

- `Controlled Volume Breakout`
- `Exhausted Breakout Risk`

## Slope note

`SlopeDeg(close, N, "1d")` now uses the raw endpoint angle: `degrees(atan((current close - close[N]) / N))`. For cross-symbol percent-normalized slope filters, use `SlopeDegPerBar()` or `RegSlopeDeg()` explicitly.

For high-volatility stocks, percent slope can still look visually aggressive even when the move is normal for that ticker. Use `SlopeATR(close, N, "1d")` or `SlopeATRDeg(close, N, "1d")` when you want a volatility-adjusted vertical/exhaustion test.
