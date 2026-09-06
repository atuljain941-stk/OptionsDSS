# Scanner Builder / Dashboard UI cleanup v87

## Scanner Builder

- Reduced the query editor height so two-line scanner logic uses less vertical space.
- Made the column-template area compact.
- Added primitive columns directly into the visible result table.
- Result table headers now support:
  - click to sort,
  - drag to reorder columns,
  - `×` delete button on non-locked columns.
- Symbol and Reason remain locked columns.
- Column chips are still available under a small expandable section for advanced review.
- Refresh Values continues to rerun the current scanner and recompute primitive columns after adding/changing columns.

## Scanner Dashboard

- Dashboard tiles now use a responsive CSS grid so tiles do not overlap or render outside the viewport.
- Edit layout mode no longer uses absolute-positioned tiles.
- Tiles can be drag/dropped to reorder in edit mode.
- Tile resizing is vertical only so width cannot force overlap or off-screen placement.
- Added quick layout buttons:
  - Auto pack
  - 2x2
  - 3x3
- Saved dashboards persist the chosen quick layout through the existing dashboard settings/layout payload.
