# v80 Journal/Alerts Live Price + Entry Amount Formatting

## Journal and alert prices
The v79 hardening made option pricing DB-first by default to stop yfinance NaN failures. For journals and alerts, live prices are needed, so the journal paths now explicitly request live pricing:

```python
fetch_chain(symbol, expiry, prefer_live=True, db_first=False)
```

Behavior:

1. Try live yfinance bid/ask/last first.
2. Sanitize every numeric field before converting OI/volume/price.
3. If yfinance fails or returns no usable chain, fall back to the local `options` DB snapshot.
4. Live-chain cache uses a shorter 60-second TTL for journal/alert marks.

This applies to:

- `/journal/trade/<id>/live`
- `/journal/trade-alerts`
- journal PNR/health alert watcher paths
- journal scheduler current-OI refresh

## Entry amount display
Journal trade rows now display entry price/amount with 2 decimals:

```text
$1.23
```

instead of long raw float/text values.

The `/journal/trades` API also rounds common money fields to 2 decimals before returning JSON for UI display.
