# Scanner Builder UAE v5 Alignment (v83)

This build aligns Scanner Builder UAE primitives with the uploaded AJ - Trend/Vol Analyzer v5 Pine script.

## Pine-aligned calculation changes

Scanner Builder and the UAE trade scanner now use the v5 Pine logic for:

- Custom UAE MACD/histogram calculation from normalized trend momentum.
- RSIdiff = RSI(14) - EMA(RSI(14), 90).
- `isTrending = RSIdiff > +12 OR RSIdiff < -12 OR ADX > timeframe threshold`.
- Slope confirmation uses a minimum normalized EMA slope threshold instead of simple slope > 0.
- Strong histogram uses the 60th-percentile threshold over the 100-bar histogram lookback.
- Fade arrows have priority over trend triangles.
- Trend triangles require MACD zero-line cross + trending + strong histogram + no same-bar fade arrow.

## New / clarified primitives

### Trend triangle

```text
UAETrendTriangle("bull", "1d")
UAETrendTriangle("bear", "1d")
```

Aliases:

```text
UAESolidTriangle("bull", "1d")
UAESolidTriangle("bear", "1d")
```

### MRT / fade arrow

```text
UAEFadeArrow("bull", "1d")   # teal up arrow / fade long
UAEFadeArrow("bear", "1d")   # vermillion down arrow / fade short
```

Aliases:

```text
UAEMRTArrow("bull", "1d")
UAEMRTArrow("bear", "1d")
```

### Optional strong-hist diamond

```text
UAEDiamond("bull", "1d")
UAEDiamond("bear", "1d")
```

### Numeric diagnostics

```text
UAERSIDiff("1d")
UAEMACD("1d")
UAESignal("1d")
UAEHist("1d")
UAEHistThreshold("1d")
UAEStrongHist("1d")
UAETrending("1d")
UAEADX("1d")
```

## Example scans

Bull trend triangle in the last 5 daily bars:

```text
Lookback(UAETrendTriangle("bull", "1d"), 5)
```

Bear trend triangle in the last 5 daily bars:

```text
Lookback(UAETrendTriangle("bear", "1d"), 5)
```

MRT/fade long arrow on daily:

```text
UAEFadeArrow("bull", "1d")
```

MRT/fade short arrow on daily:

```text
UAEFadeArrow("bear", "1d")
```

Trend continuation / strong histogram diamond:

```text
UAEDiamond("bull", "1d")
```

