# v12 Scanner Builder Fixes

This source-only package adds/restores `Between(value, low, high)` and `NotBetween(value, low, high)` to Scanner Builder.

Example:

```text
Between(SlopeDeg(close, 10, "1d"), 0, 45)
```

It also keeps the slope normalization fixes:

- `SlopeDeg()` now uses the raw endpoint formula `degrees(atan((current - value[bars]) / bars))`. Use `SlopeDegPerBar()` / `RegSlopeDeg()` for normalized variants.
- `SlopePct()` shows the underlying percent-per-bar value.
- `SlopeDegRaw()` / `SlopeRawDeg()` are available for the old raw-unit behavior.
- `SlopeATR()` / `SlopeATRDeg()` are available when you want a volatility-adjusted slope filter.

No database files are included.
