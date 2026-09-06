# Scanner Columns Sortable/Reorder UI v74

This build updates Scanner Builder and Scanner Dashboard column handling.

## Scanner Builder

- Removed the left/right arrow controls from result-column chips.
- Column chips can be reordered by drag-and-drop.
- Increased screen font sizes for better readability.
- Result tables are sortable by clicking any column header.
- Sorting uses numeric comparison when the primitive output is numeric and natural text comparison otherwise.
- Sort direction toggles ascending/descending on repeated clicks.

## Scanner Dashboard

- Tile result tables are sortable by clicking any primitive-column header.
- Tile sort state is retained in the tile object and saved with the dashboard layout when the dashboard is saved.
- Increased dashboard font sizes and table padding for readability.

## Files changed

- `templates/scanner_builder.html`
- `templates/scanner_dashboard.html`
- `oiapp/static/scanner_dashboard.js`

## Packaging

The ZIP excludes database files, SQLite files, Python bytecode, and cache folders.
