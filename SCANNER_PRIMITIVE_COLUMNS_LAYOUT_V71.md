# Scanner primitive columns and dashboard layout v71

This build adds primitive-based result column templates to Scanner Builder and shares the same templates with Scanner Dashboard tiles.

## Scanner Builder

- Result columns are evaluated as Scanner Builder primitives/expressions for each matched symbol.
- Column templates are stored in the runtime SQLite app database table `scanner_column_templates`.
- Built-in templates include Core Momentum, UAE Multi-Timeframe, and Options Flow.
- Saved scanner definitions can keep their selected result template or custom column JSON.
- Run responses include `result_columns` and each row includes `_result_columns` so the UI renders exactly the selected primitive columns.

## Scanner Dashboard

- Each tile has a Result Column Template selector.
- The selector uses the same templates managed from Scanner Builder.
- Tiles pass selected primitive columns into `/scanner-builder/api/run` and display `_result_columns` from the scanner response.
- Edit Layout mode allows tiles to be resized and moved with the drag handle. Width, height, x, y, selected template, query, limit, watchlist, and prior-day settings are saved in dashboard layout JSON.

## Packaging note

The distributable ZIP intentionally excludes `.db`, `.sqlite`, `.sqlite3`, `.pyc`, `.pyo`, and `__pycache__` files.
