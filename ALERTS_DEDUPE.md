# Trade Health Alert De-duplication

This version persists the last trade-health alert state in SQLite so alerts do not repeat after restarting the app.

## Behavior

For each open trade, the app stores the latest alert baseline in `trade_health_alert_state`.

Automatic Telegram health alerts are sent only when one of these changes from the saved state:

- health status/severity, such as `warning` to `critical`
- recommended action, such as `HOLD` to `EXIT`
- PNR breached/safe state
- health score by at least the configured score delta

On the first observation of an already-open position, the app records a baseline and does not send a Telegram. This prevents old active positions from re-alerting immediately after an app restart.

## Optional environment variables

```powershell
# Default 1. Increase to 5 or 10 if score changes are too chatty.
$env:HEALTH_ALERT_SCORE_DELTA_POINTS="1"

# Default 0. Leave disabled to avoid startup alert floods.
# Set to 1 only if you want the very first observation of an existing bad trade to send immediately.
$env:HEALTH_ALERT_SEND_ON_FIRST_SEEN="0"
```

## Debug endpoint

```text
GET /journal/health_alert/state
```

This shows the persisted alert state, watcher status, and active configuration.
