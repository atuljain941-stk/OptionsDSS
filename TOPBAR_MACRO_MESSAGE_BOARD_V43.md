# v43 - Topbar VIX + Macro Message Board

Changes:

- Moved the notification bell into the right-side market status group.
- Added a VIX badge next to Spot.
- Added a center topbar message board that scrolls major market events right-to-left.
- Added `/api/topbar_context` for Spot, VIX, and macro-event messages.
- Added `oiapp/services/macro_events.py`.

The message board uses a built-in high-impact U.S. macro calendar as a fallback and can also merge custom events from:

```text
data/macro_events.json
```

Optional custom event format:

```json
[
  {
    "date": "2026-06-17",
    "time": "14:00",
    "title": "FOMC Rate Decision",
    "impact": "high",
    "source": "manual",
    "detail": "Press conference 14:30 ET"
  }
]
```

No database files are included in the package.
