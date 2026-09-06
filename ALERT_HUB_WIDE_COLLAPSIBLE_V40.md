# Alert Hub wide/collapsible + JSON safety v40

Changes:

- Alert Hub container now uses full available screen width.
- New Alert / Edit Alert panel is collapsible.
- Active alerts panel is collapsible.
- Trade Health Alerts panel is collapsible.
- Open position health table is manually collapsible.
- Custom position-alert open-position and saved-rules sections are collapsible.
- Position-alert and trade-health tables are widened to use available screen real estate.
- `/journal/health_alerts_all` now sanitizes NaN/Infinity values before JSON output to prevent browser parse errors such as `Unexpected token 'N' ... "pnr": NaN`.
- Alert Hub strike display now uses the same strike resolver used by journal/position-alert logic, covering legacy strike columns, IC columns, and `legs_json` rows.

The package is source-only and must not include SQLite/Mongo/cache files.
