# Scanner Builder All-Zero Fix v86

## Issue
After the UAE weekly lookback/data-alignment patch, Scanner Builder could return zero results almost immediately for every query. The failure was not the scanner logic itself; the new history-source helpers used `os.environ` but the `os` module import was missing from `oiapp/scanners/scanner_builder.py`.

Because each symbol load is isolated inside the scanner worker, the exception was captured per symbol and the UI showed an empty result set instead of a visible Python traceback.

## Fixes
- Restored `import os` in `scanner_builder.py`.
- Added `error_count` and `loaded_count` to `/scanner-builder/api/run` responses.
- Updated Scanner Builder result rendering so if all symbols fail to load, the UI shows a data-load error summary and first symbol error instead of a misleading plain `No matches` result.
- Preserved the UAE v5 Pine-aligned marker logic and exact marker-age semantics.

## Validation
- Python compile validation passed.
- Scanner Builder inline JavaScript syntax validation passed.
- Static Scanner Builder JavaScript syntax validation passed.
- The mixed UAE query parses with required timeframes `1d` and `1w`:

```text
lookback(UAETrendTriangle("bull","1w"),2) or lookback(UAETrendTriangle("bear","1w"),10)
```

- Synthetic marker-age validation confirmed:
  - a bull triangle 3 weekly bars ago does **not** satisfy `Lookback(..., 2)`.
  - a bear triangle 6 weekly bars ago **does** satisfy `Lookback(..., 10)`.
