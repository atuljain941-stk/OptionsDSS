# Scanner Builder UAE triangle lookback fix v84

This patch tightens the Scanner Builder UAE marker primitives to match the uploaded `AJ - Trend/Vol Analyzer v5` Pine behavior more closely.

## Fixed

- `UAETrendTriangle()` now uses a visible marker model:
  - Fade/MRT arrow has priority.
  - Trend triangle can fire only when no fade arrow fires on the same bar.
  - The scanner stores `marker_kind` internally as `MRT_BUY`, `MRT_SELL`, `TREND_BULL`, `TREND_BEAR`, or blank.
- Weekly/monthly UAE marker primitives default to confirmed bars, so a forming higher-timeframe bar does not create a provisional scanner match that is not visible/confirmed on the TradingView chart.
  - Use `"live"` as an optional mode argument to include the forming bar.
- UAE histogram threshold now waits for the full 100-bar percentile window, matching the Pine `histLookback = 100` behavior instead of using partial warm-up windows.
- UAE raw signal warm-up now mirrors Pine `nz(trendPos[rocLen])`.
- `lookback()` explanations now show which bar matched, for example `matched 1 bars ago`.

## New diagnostics

Use these as result columns when validating against TradingView:

```text
UAETrendTriangleAge("bull", "1w", 8)
UAETrendTriangleAge("bear", "1w", 8)
UAEFadeArrowAge("bull", "1w", 8)
UAEFadeArrowAge("bear", "1w", 8)
UAELastMarker("1w", 8)
UAELastMarkerAge("1w", 8)
UAERSIDiff("1w")
UAEMACD("1w")
UAEHist("1w")
UAEHistThreshold("1w")
UAEStrongHist("1w")
```

## Query examples

Confirmed weekly bars, default:

```text
lookback(UAETrendTriangle("bull", "1w"), 2)
OR lookback(UAETrendTriangle("bear", "1w"), 2)
```

Include the forming weekly bar:

```text
lookback(UAETrendTriangle("bull", "1w", "live"), 2)
OR lookback(UAETrendTriangle("bear", "1w", "live"), 2)
```

Bull triangle only:

```text
lookback(UAETrendTriangle("bull", "1w"), 2)
```

Bear triangle only:

```text
lookback(UAETrendTriangle("bear", "1w"), 2)
```
