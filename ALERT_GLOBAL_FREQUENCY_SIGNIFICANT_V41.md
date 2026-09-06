# v41 - Telegram alert significance and global cadence

## What changed

- Trade Health Telegram alerts now fire only for significant state transitions by default:
  - PNR becomes breached
  - health tier worsens into Warning / Exit Candidate / Critical
  - action/recommendation changes into a management action such as Exit, Close, Stop, Roll, Adjust, Reduce, Hedge, Cut, or Defend
- Minor score-only moves such as 70 -> 68 or 63 -> 62 are persisted as observed state but do not send Telegram.
- Score-only alerts can still be enabled later by setting `telegram_alert_score_delta_points` in `app_settings` or `HEALTH_ALERT_SCORE_DELTA_POINTS`, but the default is disabled.
- Alert Hub now has a global Telegram alert frequency selector:
  - 5m
  - 15m
  - 1h
  - 2h
  - 4h
  - 1d
- The global cadence applies to:
  - watchlist price alerts
  - saved scanner/watchlist alerts
  - PNR trade alerts
  - Trade Health alerts
  - custom position alerts

## Notes

- Manual alert buttons still run immediately.
- Existing daily de-duplication and custom position-alert de-duplication remain in place.
- Watcher loops read the setting on each cycle, so changing the cadence does not require restarting the app. The new interval applies after the current sleep cycle finishes.
