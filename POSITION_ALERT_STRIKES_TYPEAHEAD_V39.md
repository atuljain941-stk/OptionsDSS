# v39 - Position custom alert strikes and type-ahead

This source-only update extends the custom position-alert rule editor in Alert Hub.

Changes:

- Shows an open-position table inside the custom position alert panel with symbol, strategy, side, strikes, expiry, DTE, spot, PNR, and health score.
- Makes the custom position-alert rules table use the full available width.
- Adds Scanner Builder-style autocomplete/type-ahead to the bullish, bearish, and neutral/IC condition boxes.
- Adds parameter help that follows the cursor and advances as commas are typed inside a primitive.
- Adds position variable suggestions such as `short_strike`, `long_strike`, `put_sell`, `call_sell`, `breakeven`, `dte`, `pnl`, and distance variables.
- Custom position alert Telegram messages include strike details and key strike variables.
- Custom position alert scanner expressions can compare market primitives to trade-specific variables, for example:
  - `close[1d] < short_strike`
  - `distance_pct_to_short <= 1`
  - `close[1d] > call_sell OR close[1d] < put_sell`

No database/cache files are included in the ZIP.
