# Scanner Builder strong-candle EMA update v37

Strong-candle primitives now follow the ChangePct-based definition:

```text
Bull: ChangePct >= moveMult * EMA(abs(ChangePct), avgBars)
Bear: ChangePct <= -moveMult * EMA(abs(ChangePct), avgBars)
```

Volume confirmation uses EMA(volume,20):

```text
volume >= minVolMult * EMA(volume,20)
```

ATR is no longer used by these strong-candle primitives. The fourth parameter is kept in the same position for compatibility, but now represents `moveMult` instead of ATR multiple.

Default behavior:

```text
StrongBullCandle(20, "1d")
```

is equivalent to:

```text
StrongBullCandle(20, "1d", 50, 2, 1.1, 60)
```

Meaning:

```text
look back 20 daily bars
body >= 50% of candle range
bull candle close in upper part of range
one-bar ChangePct >= 2 * EMA(abs(ChangePct),60)
volume >= 1.1 * EMA(volume,20), when volume data exists
```
