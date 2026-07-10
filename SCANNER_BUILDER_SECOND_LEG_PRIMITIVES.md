# Scanner Builder Second-Leg / Loose M-W Pattern Primitives

This update adds scanner-builder primitives for loose second-leg reversal and mean-reversion patterns.

The goal is not to require a perfect textbook `M` or `W`. The second high can be a little higher or lower than the first high, and the second low can be a little higher or lower than the first low. The tolerance is configurable.

## Concept

- `SecondLegUp` = H1 -> pullback/valley -> H2 retest near H1. This is a loose M-top / potential bearish reversal or mean-reversion setup.
- `SecondLegDown` = L1 -> bounce/peak -> L2 retest near L1. This is a loose W-bottom / potential bullish reversal or mean-reversion setup.

The implementation uses only historical bars available through the current bar. It does not look into the future.

## New primitives

```text
SecondLegUp(lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
SecondLegDown(lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
LooseMPattern(lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
LooseWPattern(lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
DoubleTop(lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
DoubleBottom(lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
SecondLegScore(side, lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
SecondLegLevel(side, which, lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
SecondLegFirst(side, lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
SecondLegSecond(side, lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
SecondLegNeckline(side, lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
SecondLegMatchPct(side, lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
SecondLegSwingPct(side, lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
SecondLegAge(side, lookback[, timeframe[, tolerancePct[, minSwingPct[, recentBars]]]])
```

## Arguments

```text
lookback      Bars to search for the first leg.
timeframe     5m, 15m, 1h, 4h, 1d, 1w, etc.
tolerancePct  How close the second leg must be to the first leg. Default 5%.
minSwingPct   Required pullback/bounce between the two legs. Default 3%.
recentBars    How many recent bars can contain the second touch. Default 3.
```

## Side values for generic functions

For M-style second leg up:

```text
"up", "m", "top", "bear", "short"
```

For W-style second leg down:

```text
"down", "w", "bottom", "bull", "long"
```

## Level names

`SecondLegLevel()` accepts:

```text
"first"     first high/low
"second"    second-leg high/low
"neckline"  M valley or W peak
"midpoint"  midpoint between first swing and neckline
"target"    simple measured-move target
```

## Example scans

Potential loose M-top / second leg up zone:

```text
SecondLegUp(45, "1d", 5, 3, 3)
AND SecondLegScore("up", 45, "1d") >= 60
```

Potential loose W-bottom / second leg down zone:

```text
SecondLegDown(45, "1d", 5, 3, 3)
AND SecondLegScore("down", 45, "1d") >= 60
```

Bearish mean-reversion candidate:

```text
SecondLegUp(45, "1d", 5, 3, 3)
AND SecondLegScore("bear", 45, "1d") >= 65
AND RSIDiff90("1d") > 8
AND NOT UAEHistGrowing("1d")
```

Bullish mean-reversion candidate:

```text
SecondLegDown(45, "1d", 5, 3, 3)
AND SecondLegScore("bull", 45, "1d") >= 65
AND RSIDiff90("1d") < -8
AND NOT UAEHistGrowing("1d")
```

Confirmed M breakdown:

```text
SecondLegUp(60, "1d", 6, 3, 5)
AND close[1d] < SecondLegNeckline("up", 60, "1d", 6, 3, 5)
```

Confirmed W breakout:

```text
SecondLegDown(60, "1d", 6, 3, 5)
AND close[1d] > SecondLegNeckline("down", 60, "1d", 6, 3, 5)
```

Tight double-top retest:

```text
DoubleTop(60, "1d", 2, 4, 2)
AND SecondLegMatchPct("up", 60, "1d", 2, 4, 2) <= 2
```

Loose double-bottom retest:

```text
DoubleBottom(60, "1d", 6, 3, 5)
AND SecondLegSwingPct("down", 60, "1d", 6, 3, 5) >= 5
```

## Notes

Existing `MPattern()` and `WPattern()` primitives remain available as older/stricter pattern confirmation primitives. Use `SecondLegUp`, `SecondLegDown`, `LooseMPattern`, or `LooseWPattern` for the new loose second-leg logic.
