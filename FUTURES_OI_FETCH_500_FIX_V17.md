# Futures OI Fetch 500 Fix - v17

Fixes a 500 error triggered by `/api/futures/fetch_now` after the v16 job tracking change.

## Root cause

The route called `_futures_job_update()` like this:

```python
_futures_job_update(
    job_id,
    job_id=job_id,
    running=True,
    ...
)
```

Because `job_id` is both the first positional argument and also a keyword argument, Python raises:

```text
TypeError: _futures_job_update() got multiple values for argument 'job_id'
```

That happened before the background fetch thread could start, so the browser saw `500 INTERNAL SERVER ERROR`.

## Fix

Removed the duplicate `job_id=job_id` keyword argument. The helper already stores the job id internally when it creates the job record.

Updated files:

- `oiapp/api/routes.py`
- `oiapp_api_routes.py`

## Validation

- Python compile check passed.
- JavaScript syntax checks passed.
- Static check confirms the duplicate `job_id` argument is gone.
