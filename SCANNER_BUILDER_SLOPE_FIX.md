# Scanner Builder SlopeDeg Fix

`SlopeDeg(expr, bars[, timeframe])` now uses the explicit raw endpoint formula requested for price-action slope checks:

```python
raw_slope = (current_value - value_bars_ago) / float(bars)
SlopeDeg = math.degrees(math.atan(raw_slope))
```

For example:

```text
SlopeDeg(close, 10, "1d")
```

uses:

```text
degrees(atan((current close - close[10]) / 10))
```

This is not a regression and not percent-normalized. It is the straight-line angle from the value N bars ago to the current value, in raw units per bar.

Related helpers:

```text
Slope(expr, bars[, timeframe])         -> raw units per bar
SlopeDeg(expr, bars[, timeframe])      -> degrees(atan(raw units per bar))
SlopeDegRaw(expr, bars[, timeframe])   -> alias for SlopeDeg
SlopePct(expr, bars[, timeframe])      -> total endpoint percent move
SlopePctPerBar(expr, bars[, timeframe])-> total percent move / bars
SlopeDegPerBar(expr, bars[, timeframe])-> degrees(atan(percent move per bar))
RegSlopeDeg(expr, bars[, timeframe])   -> regression-based percent-per-bar angle
```

Example consolidation filter:

```text
Between(SlopeDeg(close, 10, "1d"), -30, 30)
```

Debug result columns:

```text
Round(SlopeDeg(close, 10, "1d"), 2)
Round(Slope(close, 10, "1d"), 2)
Round(SlopePct(close, 10, "1d"), 2)
```
