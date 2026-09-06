# Scanner Builder ChangePct

`ChangePct(expr[, bars][, timeframe])` returns percentage change over the selected number of bars.

Default behavior remains one bar:

```text
ChangePct(close)
ChangePct(close, "1d")
```

New multi-bar behavior:

```text
ChangePct(close, 10, "1d")
```

This computes:

```text
((current_close - close_10_bars_ago) / abs(close_10_bars_ago)) * 100
```

Examples:

```text
ChangePct(close, 10, "1d") > 10
```

Finds stocks up more than 10% over the last 10 daily bars.

```text
ChangePct(close, 10, "1d") < -10
```

Finds stocks down more than 10% over the last 10 daily bars.

The output is in percentage points. A move from 100 to 110 returns `10`, not `0.10`.
