# Scanner Builder compact layout v26

Changes:

- Removed the row-based visual Builder panel from the Scanner Builder page.
- Made the query text area and Results section full-width.
- Changed Saved Scanners to a compact dropdown with Load, Run, Delete, Refresh, Alert, and Mode controls.
- Changed Scanner Catalog to a compact dropdown with Insert code and Insert scan() controls.
- Added a Primitive Helper dropdown.
- Added persistent inline primitive/function parameter help under the query box.
- Parameter help tracks the current argument as the cursor moves inside a function and advances when commas are entered.
- Collapsed the How to write rules panel by default.
- Caches the last successfully executed query in browser localStorage and restores it on the next page load.

Packaging policy:

- Source-only package.
- Do not include options_data.db.
- Do not include data/market_data.db or any backtest cache database.
- Do not include *.db, *.sqlite, *.sqlite3, WAL, SHM, journal, pycache, or pyc files.
