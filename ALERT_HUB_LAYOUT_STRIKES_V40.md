# Alert Hub layout, strike display, and safe JSON fix v40

This update focuses on the Alert Hub UI and custom position alert data.

## Changes

- Alert Hub uses the full available browser width.
- New Alert/Edit Alert panel is collapsible.
- Active Alerts section is manually collapsible.
- Trade Health Alerts section is manually collapsible.
- Custom Position Alert panel remains collapsible.
- Open positions covered by custom rules is manually collapsible.
- Saved custom position alert rules is manually collapsible.
- Open position health table is manually collapsible.
- Position alert strike display now uses the same strike resolver as the custom position-alert engine.
- Strike display reads legacy strike columns, IC columns, and legs_json.
- Journal health alert JSON responses are sanitized with `_jsonify_safe()` so NaN / Infinity values such as `pnr: NaN` cannot break browser JSON parsing.

## No database files

This is a source-only package. It intentionally excludes app DBs, SQLite cache DBs, WAL/SHM/journal files, and bytecode caches.
