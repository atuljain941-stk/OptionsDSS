# Scanner Primitive Columns UI Fix v72

This patch fixes the Scanner Builder primitive-column workflow and restores Scanner Dashboard tile rendering.

## Scanner Builder fixes

- Manual primitive/expression entry now works from the Result columns panel.
- The `+ Add typed / selected column` button first uses the typed `Primitive / expression` value, then falls back to the selected primitive helper.
- The expression input now has primitive suggestions through a datalist.
- Added a visible `Template name to save` field so templates can be named before saving.
- `Reset default columns` now resets to the built-in primitive default columns and refreshes the result header immediately.
- Adding/removing columns updates the visible result table headers immediately.
- Duplicate column expressions/labels are blocked.

## Scanner Dashboard fixes

- Restored tile rendering by adding a safe price resolver used by primitive column rendering.
- Wrapped individual tile table rendering in a guard so one bad result row cannot blank the whole dashboard.
- Added fallback starter dashboard behavior if saved-dashboard APIs are temporarily unavailable.
- Dashboard tiles continue to use the same Scanner Builder primitive-column templates.

## Packaging

The release ZIP excludes runtime databases and caches: `.db`, `.sqlite`, `.sqlite3`, `.pyc`, `.pyo`, and `__pycache__`.
