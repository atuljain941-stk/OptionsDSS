# v39 Position custom alerts: strikes, typeahead, wide table

Changes:

- Custom position alert expressions can now use position variables in addition to normal Scanner Builder primitives.
- Variables include `spot`, `short_strike`, `long_strike`, `sell_strike`, `buy_strike`, `put_sell`, `put_buy`, `call_sell`, `call_buy`, `breakeven`, `lower_breakeven`, `upper_breakeven`, `dte`, `pnl`, `pnl_pct`, `max_loss`, `max_profit`, `pnr`, `pnr_upper`, and distance helpers.
- Telegram custom position alerts now include the trade strikes so the alert is actionable.
- Alert Hub custom position text boxes now have the same autocomplete/parameter help style as normal alerts, plus variable typeahead.
- The custom position alert rules table uses the available width and keeps long conditions readable.

Example bullish-position warning for PS/CB:

```text
CrossBelow(MACDLine, MACDSignal, "1d") OR close[1d] < short_strike
```

Example bearish-position warning for CS/PB:

```text
CrossAbove(MACDLine, MACDSignal, "1d") OR close[1d] > short_strike
```

Example IC warning:

```text
close[1d] > call_sell OR close[1d] < put_sell
```

The rules remain in addition to Trade Health and PNR alerts, and are still daily de-duplicated per trade + rule.
