# Scanner Builder Column UX + Dashboard Tile Fix V72

## What changed

- Result-column entry now supports manual primitive/expression typing directly from the Result columns panel.
- Added a visible `Pick primitive` dropdown inside the Result columns panel.
- Added a datalist-backed primitive expression field for faster primitive entry.
- `Add picked/typed primitive` now falls back to the typed expression instead of requiring a dropdown selection.
- Added a `Save as` template-name input so result-column templates can be named before saving.
- `Reset default columns` now resets to the built-in default primitive columns and refreshes the result header.
- Dashboard tiles now recover from empty saved layouts by restoring starter tiles.
- Dashboard tile rendering now defines the missing `getPrice()` helper used by primitive column rendering.
- Dashboard init is more defensive: if the dashboard API is temporarily unavailable, a local starter dashboard is shown instead of a blank screen.

## Files changed

- `templates/scanner_builder.html`
- `oiapp/static/scanner_dashboard.js`
- `oiapp/scanners/scanner_dashboard.py`
- `oiapp/scanners/scanner_builder.py`

## Notes

Column definitions are still primitive/expression based and are shared by Scanner Builder and Scanner Dashboard tiles.
