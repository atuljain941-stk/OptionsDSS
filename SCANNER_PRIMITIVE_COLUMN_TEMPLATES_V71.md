# Scanner primitive column templates and dashboard layout v71

This build adds reusable result-column templates for Scanner Builder and Scanner Dashboard.

## Scanner Builder

- Result columns are now defined as scanner primitives or expressions.
- The selected columns are sent with `/scanner-builder/api/run` and evaluated for every matched symbol.
- Column templates can be saved, applied, and deleted from the Scanner Builder page.
- Saved scanners can store their own result-column set or selected template.

Examples of primitive columns:

```text
close[1d]
RelativeStrength(20, "1d")
RSIDiff90(90, "1d")
UAERegime("1d")
UAERegime("1w")
EarningsDays()
OIChangePct(5)
PCRShift(5)
FlowBias()
```

## Backend storage

New table:

```sql
scanner_column_templates
```

Added to `scanner_definitions`:

```sql
result_columns_json
result_template_id
```

## Shared APIs

```text
GET    /scanner-builder/api/column-templates
POST   /scanner-builder/api/column-templates
PUT    /scanner-builder/api/column-templates/<id>
DELETE /scanner-builder/api/column-templates/<id>
```

Aliases are also available under `/scanner-builder/api/result-column-templates`.

## Scanner Dashboard

- Dashboard tiles can use the same column templates.
- Each tile includes a Columns selector.
- Tile runs pass selected primitive columns to `/scanner-builder/api/run`, so the dashboard displays the same evaluated primitive fields as Scanner Builder.
- Layout edit mode now supports moving tiles with the drag handle and resizing tiles with the browser resize handle.
- Tile positions and sizes are saved in the dashboard layout JSON.

## Files changed

- `oiapp/scanners/scanner_builder.py`
- `templates/scanner_builder.html`
- `oiapp/scanners/scanner_dashboard.py`
- `templates/scanner_dashboard.html`
- `oiapp/static/scanner_dashboard.js`

## Packaging note

The ZIP must exclude database/cache files: `.db`, `.sqlite`, `.sqlite3`, `.pyc`, `.pyo`, and `__pycache__`.
