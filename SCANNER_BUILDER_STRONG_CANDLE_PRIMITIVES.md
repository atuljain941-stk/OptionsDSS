# Scanner Builder strong-candle retest primitives

These primitives are for the pattern: a strong impulse candle happened first, and the current bar is now retesting that candle's low, high, midpoint, or another percentage level.

The current bar is intentionally excluded from the anchor search. The anchor candle must be a prior candle; then the current candle is checked for the retest/touch.

## Updated strong-candle definition

A strong candle now uses the user's ChangePct-based definition instead of ATR:

```text
Bull strong candle:
ChangePct(close, 1, timeframe) >= EMA(abs(ChangePct(close, 1, timeframe)), avgBars) * moveMult

Bear strong candle:
ChangePct(close, 1, timeframe) <= -EMA(abs(ChangePct(close, 1, timeframe)), avgBars) * moveMult
```

The baseline is shifted by one bar, so the anchor candle is compared only against information that existed before that candle formed.

Volume also uses EMA, not SMA:

```text
volume >= EMA(volume, 20) * minVolMult
```

## Primitive list

```text
AvgAbsChangePct(bars[, timeframe])
EMAAbsChangePct(bars[, timeframe])

StrongBullCandle(lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])
StrongBearCandle(lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])

StrongCandleAge(side, lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])
StrongCandleHigh(side, lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])
StrongCandleLow(side, lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])
StrongCandleOpen(side, lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])
StrongCandleClose(side, lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])
StrongCandleChangePct(side, lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])
StrongCandleAvgAbsChangePct(side, lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])
StrongCandleMoveMultiple(side, lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])

StrongCandleLevel(side, level, lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])
TouchStrongCandleLevel(side, level, lookback[, tolerancePct][, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])
NearStrongCandleLevel(side, level, lookback[, tolerancePct][, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]])
```

## Parameter meaning

```text
lookback    = how many prior bars to search for an anchor candle
timeframe   = "1d", "4h", "1h", etc.
minBodyPct  = body as % of candle high-low range; default 50
moveMult    = required multiple of EMA(abs(ChangePct), avgBars); default 2
minVolMult  = required multiple of EMA(volume,20); default 1.1
avgBars     = EMA length for abs(ChangePct); default 60
```

`side` can be:

```text
"bull", "bullish", "long", "call"
"bear", "bearish", "short", "put"
```

`level` can be:

```text
"low"
"high"
"open"
"close"
"mid"
50
25
75
```

Numeric levels are percent of the candle range:

```text
0   = anchor candle low
50  = anchor candle midpoint
100 = anchor candle high
```

This is the same for bull and bear anchors.

## Default strong-candle definition

A prior bull candle qualifies when:

```text
close > open
body >= 50% of candle range
close is in the upper 40% of the range
ChangePct >= 2.0 * EMA(abs(ChangePct), 60)
volume >= 1.1 * EMA(volume, 20), when volume is available
```

A prior bear candle qualifies when:

```text
close < open
body >= 50% of candle range
close is in the lower 40% of the range
ChangePct <= -2.0 * EMA(abs(ChangePct), 60)
volume >= 1.1 * EMA(volume, 20), when volume is available
```

## Examples

Strong bull candle with 2x normal absolute change and 1.5x EMA volume:

```text
StrongBullCandle(30, "1d", 55, 2, 1.5, 60)
```

Strong bear candle with 2x normal absolute change and 1.5x EMA volume:

```text
StrongBearCandle(30, "1d", 55, 2, 1.5, 60)
```

Bull retest of the midpoint of that candle:

```text
StrongBullCandle(30, "1d", 55, 2, 1.5, 60)
AND TouchStrongCandleLevel("bull", 50, 30, 0.75, "1d", 55, 2, 1.5, 60)
```

Bull retest of the low of that candle:

```text
StrongBullCandle(30, "1d", 55, 2, 1.5, 60)
AND TouchStrongCandleLevel("bull", "low", 30, 0.75, "1d", 55, 2, 1.5, 60)
```

Bear retest of the midpoint of that candle:

```text
StrongBearCandle(30, "1d", 55, 2, 1.5, 60)
AND TouchStrongCandleLevel("bear", 50, 30, 0.75, "1d", 55, 2, 1.5, 60)
```

Bear retest of the high of that candle:

```text
StrongBearCandle(30, "1d", 55, 2, 1.5, 60)
AND TouchStrongCandleLevel("bear", "high", 30, 0.75, "1d", 55, 2, 1.5, 60)
```

Debug the matched anchor:

```text
StrongCandleAge("bull", 30, "1d", 55, 2, 1.5, 60)
StrongCandleChangePct("bull", 30, "1d", 55, 2, 1.5, 60)
StrongCandleAvgAbsChangePct("bull", 30, "1d", 55, 2, 1.5, 60)
StrongCandleMoveMultiple("bull", 30, "1d", 55, 2, 1.5, 60)
```
