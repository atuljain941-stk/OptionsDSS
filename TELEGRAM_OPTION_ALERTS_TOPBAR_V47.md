# V47 - Telegram option alert context + topbar message-board controls

## Telegram trade-health alerts

Trade-health Telegram messages now include option context only when the trade is an option trade.

Added to option-trade alerts:

- Strikes resolved from legacy strike columns and `legs_json`
- Expiry / expiries
- DTE days

Stock/share trades do not display option strike or expiry lines.

## Topbar message board

The market message board now uses a larger 14px font and supports a scroll-speed selector.

Speed options:

- Fast
- Normal
- Slow
- Slower

The selected speed is stored in browser `localStorage`.

News items in the topbar are clickable. If a headline has a URL, it opens the news detail in a new browser tab. If no URL is available, the app opens the Market News tab and searches for the clicked headline.

## Packaging

This package is source-only. Do not include SQLite, MongoDB, cache, WAL, journal, or backtest database files.
