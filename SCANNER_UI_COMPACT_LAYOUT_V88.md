# Scanner Builder / Dashboard UI compact layout v88

## Scanner Builder

- Query text area is reduced for normal 1-2 line scanner expressions.
- Result-column controls are compacted into a collapsible Result columns panel.
- Adding a primitive column immediately realigns the result table and marks values stale until refreshed.
- Result-table headers now include an inline delete button for non-locked columns.
- Column chips remain available for drag-reorder and template saving.
- Result tables remain sortable by clicking any header.

## Scanner Dashboard

- Normal dashboard layout now uses CSS grid instead of flex, so tiles wrap to the next row and cannot overlap.
- Edit-layout mode now clamps tile movement to the visible dashboard width.
- Saved or dragged tiles that overlap or extend outside the dashboard are automatically packed into non-overlapping rows.
- Added quick layout buttons:
  - Auto pack
  - 2x2
  - 3x3
- Preset layouts turn on edit mode, resize tiles to fit the visible width, and place tiles in a clean grid.

## Packaging

The release zip excludes database, SQLite, cache, and bytecode files.
