# v34 Scanner alert de-duplication

This update adds a per-symbol alert state table for custom watchlist alert rules.

## Why

Event-style scanner expressions such as:

```text
CrossOver(ema20, ema50, "1d") OR CrossBelow(ema20, ema50, "1d")
```

can remain true for the currently forming daily bar across several scheduler runs. If the alert rule uses `Every time met`, the same symbol can be sent repeatedly during the same session.

## New trigger mode

Use:

```text
On change / re-arm
```

for scanner/primitive/combo alerts.

Behavior:

- Sends when a symbol changes from not matching to matching.
- Does not resend while the same symbol remains matched.
- Re-arms when a later scan observes that the symbol no longer matches.
- Then sends again on a future new match.

## New table

```sql
alert_rule_symbol_state(rule_id, symbol, last_match_state, last_triggered_at, last_seen_at, updated_at)
```

This table lives in the user's existing SQLite app database. It is created automatically and is not included in source ZIPs.

## Cross aliases

The Scanner Builder now accepts both names:

```text
CrossAbove(...)
CrossOver(...)
```

and both names:

```text
CrossBelow(...)
CrossUnder(...)
```
