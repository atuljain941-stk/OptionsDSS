# v44 Topbar live news refresh

Adds latest market headlines to the topbar message board and lets the user control the news refresh cadence.

## Behavior

- Macro reminders still appear first in the scrolling message board.
- Latest market headlines are appended after macro reminders.
- Headlines are fetched from yfinance ticker news plus no-key RSS market feeds.
- The refresh cadence is selectable from the topbar: 5m, 15m, 30m, 1h, 2h, 4h.
- The selected cadence is stored in browser localStorage.
- The refresh button forces an immediate news refresh.
- Spot, VIX, and macro reminders continue refreshing every five minutes; news calls honor the selected server-side cache interval.

## Optional configuration

Override RSS feeds with a comma-separated environment variable:

```bash
OIAPP_NEWS_RSS_FEEDS="https://example.com/rss,https://example2.com/rss"
```

No database files are required or included.
