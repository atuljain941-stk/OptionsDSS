# v47 - Option alert context and topbar ticker controls

## Telegram alert changes

Trade Health and Custom Position Telegram alerts now include option-only context:

- strike summary
- expiry date(s)
- DTE per expiry

Stock/share trades do not display option strike or expiry metadata.

## Topbar message board changes

- Message-board font increased to 14px.
- Added ticker speed selector: fast, normal, slow, slower.
- Speed is saved in browser localStorage.
- Latest-news items in the ticker are clickable.
- Clicking a news item opens the source URL when available; if no URL is available, it switches to Market News and searches for the headline.
