# v35 Scanner alert same-day de-duplication

This update prevents duplicate Telegram scanner/watchlist alerts when the alert scheduler runs every few minutes.

## Behavior

For custom alert rules, each rule + symbol can notify at most once per local calendar day while the condition remains true.

Examples:

- MRNA matches a daily support/resistance break at 5:08 PM -> Telegram is sent.
- Scheduler runs again at 5:09 PM and MRNA still matches -> suppressed.
- Scheduler runs later the same day and MRNA still matches -> suppressed.
- The next day, if MRNA still/newly matches -> eligible again.

## Trigger modes

- `daily` / `every` -> once per symbol per day while matched.
- `on_change` -> sends on false-to-true transition, still capped at once per symbol per day.
- `once` -> original one-time rule behavior.

The UI now labels the preferred mode as `Once per symbol per day`.

## Data

The alert state is stored in the user's existing SQLite app database table:

```sql
alert_rule_symbol_state
```

No database files are included in the source package.
