# Option Price NaN Handling Fix V78

## Issue
`oiapp/services/option_prices.py` converted yfinance `openInterest` and `volume` values directly with `int(...)`.
Some yfinance chains return `NaN` for those fields, causing:

```text
[option_prices] PLTR/2026-07-10: cannot convert float NaN to integer
```

The exception caused the whole option-chain fetch for that symbol/expiry to return `{}`, which could degrade strategy pricing and weekly-plan enrichment.

## Fix
Added finite-safe numeric helpers:

- `_safe_int(value, default=0)`
- `_safe_float(value, default=None, ndigits=None)`

`fetch_chain()` now stores missing `openInterest` and `volume` as `0` instead of raising.

Also adjusted spread net-credit calculation so valid zero-priced legs are handled by checking `is not None` rather than truthiness.

## Files changed

- `oiapp/services/option_prices.py`

## Validation

- Direct synthetic yfinance-chain test with `NaN` OI/volume passed.
- `python -m compileall -q oiapp` passed.
