# Scanner Builder UAE primitives

This package adds UAE Trend/Vol Analyzer primitives to the existing Scanner Builder so the UAE scanner can be built from regular scanner expressions instead of a separate hard-coded page.

## Regime primitives

```text
UAERegime([timeframe])
IsUAERegime(regime[, timeframe])
UAEBull([timeframe])
UAEWeakBull([timeframe])
UAEBear([timeframe])
UAEWeakBear([timeframe])
UAESideways([timeframe])
UAERegimeScore([timeframe])
```

Regime labels returned by `UAERegime()` are:

```text
BULL
WEAK_BULL
BEAR
WEAK_BEAR
SIDEWAYS
```

Examples:

```text
UAEWeakBull("1d") AND UAERegimeScore("1d") >= 55
```

```text
(UAEBear("1h") OR UAEWeakBear("1h")) AND UAERegimeScore("1h") >= 60
```

```text
IsUAERegime("SIDEWAYS", "4h")
```

## Indicator/quality primitives

```text
UAEADX([timeframe])
UAEADXRising([timeframe])
UAETrending([timeframe])
UAEHist([timeframe])
UAEHistGrowing([timeframe])
```

Examples:

```text
UAEWeakBull("1d") AND UAEADXRising("1d") AND UAEHist("1d") < 0
```

```text
UAEBull("4h") AND UAEHistGrowing("4h") AND UAERegimeScore("4h") >= 65
```

## Signal primitives

```text
UAESolidTriangle(side[, timeframe])
UAEWeakTriangle(side[, timeframe])
UAECircle(side[, timeframe])
UAEDiamond(side[, timeframe])
UAEConfluence(side[, timeframe[, bars]])
```

`side` accepts `bull`, `bear`, `long`, `short`, `call`, or `put`.

Examples:

```text
UAEConfluence("bull", "1h", 3) >= 2 AND UAEHigherTFAligned("bull", "1h")
```

```text
lookback(UAESolidTriangle("bear", "4h"), 3) AND UAEBear("1d")
```

## Multi-timeframe primitives

```text
UAEHigherTFAligned(side, entryTimeframe)
UAEMultiTFAligned(side, entryTimeframe)
```

`UAEHigherTFAligned("bull", "1h")` checks the 4H regime. `UAEHigherTFAligned("bull", "15m")` checks the 1H regime. `UAEMultiTFAligned()` uses the stricter stack; for 5m it requires both 15m and 1H to align.

Examples:

```text
UAEHigherTFAligned("bull", "15m") AND UAEConfluence("bull", "15m", 3) >= 2
```

```text
UAEMultiTFAligned("bear", "5m") AND UAEConfluence("bear", "5m", 2) >= 2
```

## Simple scanner-builder recipes

Daily weak bull pullback candidates:

```text
UAEWeakBull("1d") AND UAERegimeScore("1d") >= 55
```

Daily bear or weak bear candidates:

```text
(UAEBear("1d") OR UAEWeakBear("1d")) AND UAERegimeScore("1d") >= 55
```

Avoid weekly overbought weak bull names:

```text
UAEWeakBull("1d") AND RSIDiff90("1w") <= 20
```

1H bull entry watchlist with 4H bias:

```text
UAEHigherTFAligned("bull", "1h") AND UAEConfluence("bull", "1h", 3) >= 2 AND NOT UAESideways("1h")
```

1H bear entry watchlist with 4H bias:

```text
UAEHigherTFAligned("bear", "1h") AND UAEConfluence("bear", "1h", 3) >= 2 AND NOT UAESideways("1h")
```

Regime checkbox equivalent for Daily weak bull + bear:

```text
UAEWeakBull("1d") OR UAEBear("1d")
```

## Notes

The primitives use the uploaded UAE Trend/Vol formula, not the normal MACD already present in Scanner Builder. Timeframe parameters are auto-selected from the Pine script: 5m, 15m, 1H, 4H, Daily, and Weekly each use their own fast/slow/signal/ROC/slope/ADX settings.

No option strikes are selected by these primitives. They are intended for symbol screening and overall regime scoring.
