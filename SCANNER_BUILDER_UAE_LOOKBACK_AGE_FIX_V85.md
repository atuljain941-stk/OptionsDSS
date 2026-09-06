# Scanner Builder UAE lookback age fix v85

This patch fixes short-lookback UAE marker scans such as:

```text
Lookback(UAETrendTriangle("bull", "1w"), 2)
OR Lookback(UAETrendTriangle("bear", "1w"), 2)
```

## Why AMD could pass before

The prior patch focused on confirmed higher-timeframe bars, but the deeper issue was that the app could still treat a UAE marker primitive like a normal boolean expression inside generic `Lookback()`.

UAE markers are not ordinary continuous booleans. The Pine script has visible-marker semantics:

1. Fade/MRT arrow has priority.
2. Trend triangle can print only when no fade arrow prints on the same bar.
3. Only the visible marker for that bar should be considered.

Because the scanner did not force `Lookback(UAETrendTriangle(...), N)` to use the exact visible-marker age, a symbol could pass even when the latest chart-visible triangle was outside the requested N-bar window.

## Fixed behavior

`Lookback()` now detects UAE marker primitives and applies exact age semantics:

```text
Lookback(UAETrendTriangle("bull", "1w"), 2)
```

passes only if:

```text
UAETrendTriangleAge("bull", "1w", 2) is 0 or 1
```

A triangle 6 or 7 weekly bars ago will not pass a 2-bar lookback.

## Diagnostic result columns

Use these to validate Scanner Builder results against TradingView:

```text
UAETrendTriangleAge("bull", "1w", 12)
UAETrendTriangleAge("bear", "1w", 12)
UAELastMarker("1w", 12)
UAELastMarkerAge("1w", 12)
UAERSIDiff("1w")
UAEMACD("1w")
UAEHist("1w")
UAEHistThreshold("1w")
```

The scanner result reason now shows the exact UAE marker age when `Lookback()` is used with a UAE marker primitive.
