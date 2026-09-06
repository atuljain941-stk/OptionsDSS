# v104 - Futures OI status badge fix + 0-10 DTE Intraday/Positional pages

## 1. Futures OI "always stale" badge fix

**Root cause found in `oiapp/api/routes.py` (`futures_fetch_status`) and
`futures_oi_schwab.py` (`_get_quarterly_contracts`).**

`_futures_active_contracts()` tracks up to 6 forward quarterly contracts
per root -- for ES that's up to ~1.5 years out. Far-dated, thin
contracts routinely get `openInterest=0` back from Schwab's quotes
endpoint and (per the existing v18 fix) are never overwritten with a
zero. Once a far contract has any stored row from earlier, it stays in
the active set and can go stale indefinitely without that ever being a
real problem -- but the status badge was `any_stale = any(...)` across
**all** active contracts, so one permanently-stale far month dragged
the whole badge down even when the front-month contract you actually
trade (SPX/ES for 0-10 DTE) was fetching cleanly every morning. This is
why it looked "always stale" regardless of what time you fetched.

**Fix:** added `_futures_near_term_contracts(symbol, near_count=2)`,
which reuses the existing nearest-first ordering from
`_get_quarterly_contracts` to isolate the front 1-2 contracts per root.
`futures_fetch_status` now computes the pass/fail badge (`any_stale`,
the summary `message`) from near-term contracts only. Far/thin
contracts are still returned in `rows` (each with `near_term: false`
and its own real `stale_display` flag) so nothing is hidden -- they
just can't fail the top-line status anymore. Response now also
includes `far_stale_count` and `near_term_contracts` for diagnostics.

No schema change, no DB migration needed.

## 2. New pages: 0-10 DTE Intraday Buildup + Positional Trend

New service: `oiapp/services/dte_pages.py`. Two new tables
(auto-created, no manual migration): `oi_intraday_dte_cache`,
`oi_positional_dte_cache`. Both pages are pre-built/cache-backed --
they read from these tables, they don't fire a live scan on page load.

Scope: SPY, QQQ, SPX, IWM. Expiries anchored by expiry date (not a
fixed DTE bucket) and filtered to whatever is currently inside the
0-10 DTE window, so the window rolls automatically as each day passes
rather than needing manual re-selection.

### Intraday Buildup (`/dte/intraday`)
Real OI doesn't move intraday -- it settles once overnight. This page
tracks live volume at a strike vs *yesterday's* closing OI as a
same-day proxy for tomorrow's real OI move. `vol_oi_ratio` near 1.0
(highlighted) means today's volume roughly matches the OI move it
would take to open that many new contracts -- more likely real
positioning. A much higher ratio means mostly same-day churn/closing --
noisier as a signal. Refreshed automatically every 20 minutes during
market hours (registered with `unified_scheduler` + `job_registry`, so
it also shows up in Scheduler Hub with a manual "Run Now"). A
"Refresh Now" button on the page itself calls the same code path
synchronously.

### Positional / Weekly Trend (`/dte/positional`)
Real OI, but with a rolling 5-day slope per strike instead of a single
day-over-day diff -- this is what separates sustained accumulation from
a one-day spike. Each strike gets a `trend_label`:
`sustained_buildup` / `sustained_unwind` (slope has a consistent sign
across the window) / `one_day_spike` (today moved but the window isn't
consistent) / `flat` / `low_oi` (under 500 contracts, not worth
reading trend into). Runs as step 7 of the 7:30 AM morning pipeline
(`scheduled_jobs.py`), after options-chain/OI data has already settled
via the earlier steps -- not any earlier, since real OI is only final
once a day. Manual "Refresh Now" available on the page (re-running
mid-day mostly re-confirms the same day's numbers, since real OI won't
have changed, but useful after a feed hiccup or an event like FOMC/CPI).

Also on this page:
- **VIX / VIX9D + SPX near-dated skew** -- via the existing
  `yf_session.safe_history` helper (same yfinance session already used
  elsewhere in the app). Skew uses a ~5%-OTM strike proxy for 25-delta
  (no full Black-Scholes delta computed here) -- good enough for a
  regime read, not a replacement for an exact desk-grade 25-delta skew.
- **ES futures OI (current + next 3 expiries)** -- reuses
  `futures_oi_schwab.get_latest_oi`, same data source as the Dashboard
  Futures OI widget. Cross-check only; scoped to SPX/ES since QQQ/IWM
  don't have a comparably liquid futures analog.

### Wiring
- `oiapp/api/routes.py`: `/dte/intraday`, `/dte/positional` (pages),
  `/api/dte/intraday_data`, `/api/dte/positional_data` (JSON reads from
  cache), `/api/dte/refresh` (manual trigger, both pages).
- `oiapp/services/scheduled_jobs.py`: step 7 added to the 7:30 AM
  morning pipeline.
- `oiapp/app_factory.py`: registers the 20-min intraday interval job
  and the two Scheduler Hub entries at startup.
- `templates/dte_intraday.html`, `templates/dte_positional.html`: new,
  self-contained (reuse `/static/style.css` + `/static/theme_uplift.css`
  for palette consistency with the rest of the app).

No database file included in this package, per standing instruction.
