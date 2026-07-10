# Scanner Builder MACD Spread Primitives

This update adds primitives for the distance between MACD and its signal line:

```text
MACDSpread(timeframe) = MACDDiff(timeframe) = macd - macd_signal
```

That value is the normal MACD histogram.  Positive means MACD is above signal.  Negative means MACD is below signal.

## New primitives

```text
MACDDiff([timeframe])
MACDSpread([timeframe])
MACDGap([timeframe])
MACDDiffAbs([timeframe])
MACDDiffPct([timeframe])
MACDSpreadRatio([lookback][, timeframe])
MACDDiffShrinkPct([bars][, timeframe])
MACDSpreadStable([side,][maxShrinkPct[, bars]][, timeframe])
MACDFarFromSignal([side,] minPct[, timeframe])
```

## Practical meanings

### MACDSpread / MACDDiff

```text
MACDSpread("1w") > 0
```

Weekly MACD is above weekly signal.

```text
MACDSpread("1w") < 0
```

Weekly MACD is below weekly signal.

### MACDSpreadRatio

```text
MACDSpreadRatio(20, "1w") >= 1.0
```

The current absolute weekly MACD spread is at least as wide as its average absolute spread over the last 20 weekly bars.

### MACDDiffShrinkPct

```text
MACDDiffShrinkPct(1, "1w") <= 25
```

The weekly MACD histogram/spread has not shrunk more than 25% versus the prior weekly bar.

Positive values mean the histogram got smaller.  Negative values mean it expanded.

### MACDSpreadStable

```text
MACDSpreadStable("bull", 25, 1, "1w")
```

Bullish weekly MACD spread is still positive and has not shrunk more than 25% versus one weekly bar ago.

This also works without side:

```text
MACDSpreadStable(25, 1, "1w")
```

### MACDFarFromSignal

```text
MACDFarFromSignal("bull", 0.25, "1w")
```

Weekly MACD is above signal and the spread is at least 0.25% of weekly close.

## Swing pullback scanner

Weekly was extended recently, weekly MACD remains intact, but daily price dipped:

```text
Lookback(RSIDiff90("1w") >= 20, 8)
AND MACDSpread("1w") > 0
AND MACDSpreadRatio(20, "1w") >= 1.0
AND MACDSpreadStable("bull", 25, 1, "1w")
AND Between(ChangePct(close, 5, "1d"), -18, -4)
AND close[1d] > ema50[1d]
```

Daily dip into EMA20 area:

```text
Lookback(RSIDiff90("1w") >= 20, 8)
AND MACDSpread("1w") > 0
AND MACDSpreadStable("bull", 25, 1, "1w")
AND Between(ChangePct(close, 5, "1d"), -18, -4)
AND low[1d] <= ema20[1d] * 1.03
AND close[1d] > ema50[1d]
```

Bearish inverse:

```text
Lookback(RSIDiff90("1w") <= -20, 8)
AND MACDSpread("1w") < 0
AND MACDSpreadRatio(20, "1w") >= 1.0
AND MACDSpreadStable("bear", 25, 1, "1w")
AND Between(ChangePct(close, 5, "1d"), 4, 18)
AND close[1d] < ema50[1d]
```
