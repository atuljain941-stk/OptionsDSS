# Quick Trade Modal v88

Adds a global top-bar `➕ Trade` button next to the existing quick-alert button.

## Behavior

- Opens a modal from any dashboard tab without switching screens.
- Prefills the current visible symbol when available.
- Supports stock trades and common option structures:
  - PS, CS, IC, PB, CB, CC, CP, Straddle, Strangle, Calendar, Butterfly, Custom.
- Saves directly to the existing Journal endpoint:
  - `POST /journal/trade/add`
- Uses the same Journal payload shape as the full Add Trade form:
  - symbol
  - trade_type
  - entry_date
  - entry_price / net premium
  - quantity
  - entry_reason
  - legs
- Refreshes open-trade and portfolio header panels after save when those functions are loaded.
- Includes an `Open full Journal form` action to transfer the modal draft into the normal Journal Add Trade page for advanced scoring/review.

## Files changed

- `templates/index.html`
  - Adds top-bar `➕ Trade` button.
  - Adds Quick Trade modal markup.
- `oiapp/static/style.css`
  - Adds Quick Trade modal styling.
- `oiapp/static/app.js`
  - Adds modal open/close, leg templates, leg editing, payload building, save, and transfer-to-Journal helpers.

## Packaging

The distribution ZIP excludes DB/cache/bytecode files.
