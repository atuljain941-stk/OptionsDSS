# Scanner Columns Sortable/Drag UX v74

This patch improves Scanner Builder and Scanner Dashboard column/result usability.

## Scanner Builder

- Removed the left/right `<` and `>` reorder buttons from result-column chips.
- Column chips now use a drag grip and can be reordered by drag-and-drop.
- Increased font sizes across the Builder screen, column panel, inputs, buttons, chips, help text, and result table.
- Result tables are now sortable by clicking any column header.
- Sorting handles numeric, price, percent, and text columns. Empty values sort last.
- The active sort column shows an up/down arrow.

## Scanner Dashboard

- Dashboard tile result tables are now sortable by clicking any column header.
- Tile sort state is stored in each tile layout, so saving the dashboard preserves the selected sort column/direction.
- Dashboard fonts and table readability were increased.

## Files changed

- `templates/scanner_builder.html`
- `templates/scanner_dashboard.html`
- `oiapp/static/scanner_dashboard.js`
- `builder_inline.js` regenerated from the Builder inline script for syntax validation.
