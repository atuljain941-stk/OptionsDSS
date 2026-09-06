# Position Custom Alerts v38

Adds optional global custom alert rules for open position trades. These rules run in addition to the existing Trade Health and PNR alerts.

## Position-sensitive rule mapping

Each rule has three Scanner Builder expressions:

- Bullish positions: PS and CB
- Bearish positions: CS and PB
- Neutral positions: IC

For example, a MACD adverse-cross rule can use:

- Bullish positions: `CrossBelow(MACDLine, MACDSignal, "1d")`
- Bearish positions: `CrossAbove(MACDLine, MACDSignal, "1d")`
- Neutral positions: `CrossAbove(MACDLine, MACDSignal, "1d") OR CrossBelow(MACDLine, MACDSignal, "1d")`

## De-duplication

Custom position alerts are capped to one Telegram per trade + rule + day. The alert refresh watcher can run every few minutes without repeating the same alert all day.

## Storage

Rules are stored in the existing SQLite app database in:

- `trade_position_alert_rules`
- `trade_position_alert_state`

No database files are packaged with this update.
