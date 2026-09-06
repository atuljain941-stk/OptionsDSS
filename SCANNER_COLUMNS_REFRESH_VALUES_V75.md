# Scanner Columns Refresh Values v75

This patch addresses Scanner Builder result-table alignment after changing primitive result columns.

## Scanner Builder

- Added **Realign table**.
  - Re-renders the existing scan rows with the current column order/template.
  - Use this after dragging columns around if the visual table needs to be re-synced.
  - It does not rerun the scanner.

- Added **Refresh values**.
  - Reruns the current Scanner Builder query using the current primitive-column template/order.
  - Use this after adding a new primitive column or applying a different template so `_result_columns` are recomputed by the backend.

- Added **Auto-refresh values**.
  - Optional checkbox.
  - When enabled, adding/applying/resetting column templates automatically reruns the current scan to recompute values.
  - The setting is saved in browser local storage.

- Column changes now immediately re-render the body rows from the last scan so headers and cells stay aligned.

## Validation

- Scanner Builder inline JavaScript syntax check passed.
- Scanner Dashboard JavaScript syntax check passed.
- Python compile validation passed for `oiapp`.
