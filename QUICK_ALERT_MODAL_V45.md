# v45 Quick Alert Modal

Adds a global Quick Alert action so price/primitive/combo alerts can be created without switching to the Alert Hub tab.

## Entry points

- Topbar: `⚡ Alert`
- Tools menu: `⚡ Quick Alert`

## Supported fields

The modal uses the same alert-rule API as Alert Hub and supports:

- watchlist target
- optional symbol target
- price / primitive / combo alert type
- trigger mode: daily, on-change with daily cap, one-time
- timeframe
- enabled toggle
- price operator and threshold
- scanner-builder expression text
- primitive/scanner autocomplete and parameter help
- notes

On save, the modal closes and the active page remains unchanged. If Alert Hub is already loaded, its active alert table refreshes in the background.

## Packaging

This package is source-only. Do not include local SQLite DB files such as `options_data.db` or `data/market_data.db`.
