# Scanner Builder Between Primitive

`Between(value, low, high)` returns `true` when the value is inside the inclusive range.

```text
Between(SlopeDeg(close, 10, "1d"), 0, 45)
```

Equivalent long form:

```text
SlopeDeg(close, 10, "1d") >= 0
AND SlopeDeg(close, 10, "1d") <= 45
```

`NotBetween(value, low, high)` returns `true` when the value is outside the inclusive range.

```text
NotBetween(rsi14[1d], 40, 70)
```

The bounds are order-safe, so these two are the same:

```text
Between(SlopeDeg(close, 10, "1d"), 0, 45)
Between(SlopeDeg(close, 10, "1d"), 45, 0)
```
