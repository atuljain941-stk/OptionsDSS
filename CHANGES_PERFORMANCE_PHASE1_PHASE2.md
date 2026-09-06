# Performance Review — Findings vs. Reality, and Phase 1/2 Changes

I read through the actual `oiapp/` source (not just the pasted review) before
changing anything. A few of the review's claims turned out to already be
solved in this build — worth knowing so we don't duplicate work later.

## Already solved (verified in source, no action taken)

- **"Scanner recomputes primitives every scan"** — Not true anymore.
  `scanner_snapshot_cache` (SQLite, full computed series, date-keyed,
  async write-behind) is already wired into the query context builder
  (`_prepare_snapshot_cached`, called from `_symbol_ctx`). A second scan
  the same day was already a cache hit, not a recompute.
- **"technical_snapshot isn't leveraged by the scanner"** — True, but by
  design: `technical_snapshot` stores only *today's scalar* per
  indicator; Scanner Builder needs the *full historical series* for
  lookback/cross/slope logic. That's exactly why `scanner_snapshot_cache`
  exists as a separate, purpose-built cache. Merging them isn't a fix.
- **"Missing history cache / per-symbol reads"** — `_local_daily_history_cached`
  and `_local_intraday_history_cached` are already `@lru_cache`'d, with
  `.cache_clear()` correctly called after backfill writes.
- **"Missing SQLite indexes"** — `price_cache` and `intraday_price_cache`
  already use composite primary keys `(symbol, date)` / `(symbol, ts)`,
  which SQLite auto-indexes; `technical_snapshot` has both a PK and an
  explicit `(symbol, timeframe)` index. Nothing to add.
- **"CFTC watcher" as a background thread** — it isn't one; it's a
  paginated fetch inside a function triggered once a week from the 7AM
  job. No independent thread exists.

## Real issues — fixed in this pass

### Phase 1

1. **9 independent watcher threads → 1 dispatcher thread.**
   New `oiapp/services/unified_scheduler.py`. Consolidated:
   `telegram_price_alerts`, `technical_snapshot_cache`,
   `scanner_price_backfill`, `scanner_intraday_backfill`,
   `scanner_daily_price_refresh`, `scanner_intraday_price_refresh`,
   `trade_pnr_alerts`, `trade_health_alerts`, `alert_rules_scan`.
   Each `start_xxx_watcher()` keeps its exact original name/signature/
   return value — `app_factory.py` did not need to change. Verified live:
   process thread count dropped from ~16 to 7 on boot.
   - **Left alone on purpose:** `signal_notifier` (internally schedules
     several sub-sources per tick, needs Flask app context) and
     `agentic_ai_scanner` (hour-scale interval already, has its own
     stop-event used elsewhere). Low frequency, higher migration risk,
     not worth it in this pass.
   - **Left alone on purpose:** `scheduled_jobs.py`'s time-of-day jobs
     (7:30/8:45 style) and the tastytrade websocket loop — different
     scheduling model (specific clock times / persistent socket), not
     comparable to the interval watchers.
   - Backfill/refresh jobs are registered `low_priority=True`: the
     dispatcher skips them entirely while a scan is active
     (`unified_scheduler.scan_started()/scan_finished()`, wired into the
     scan endpoint) — this is the "pause refresh during active scanning"
     item from the review, now real.

2. **Shared, bounded task executor.** New `oiapp/services/task_executor.py`.
   Wired into the two highest-traffic call sites: Scanner Builder's
   per-symbol scan and the daily futures-OI fetch in
   `app_factory._run_all_tasks`. There are ~40 other ad-hoc
   `ThreadPoolExecutor(...)` call sites across the codebase (scanners,
   signal_notifier, regime/sector sweeps, etc.) — migrating all of them
   in one pass was more risk than this session should take on; the
   pattern is documented in `task_executor.py`'s docstring as a 2-line
   change per site for a follow-up pass.

### Phase 2

3. **In-memory tier in front of `scanner_snapshot_cache`.** Repeat
   symbol/timeframe/date lookups within the same process now skip the
   SQLite round-trip entirely (bounded `OrderedDict`, ~2000 entries).

4. **Bulk price-history preload.** Before a scan's per-symbol worker
   pool starts, `_bulk_preload_daily_history()` loads the whole
   watchlist's `price_cache` rows with one `WHERE symbol IN (...)` query
   instead of each worker opening its own connection on a cache miss.

## What I did NOT touch

- Business logic, indicator math, UAE framework, journal/backtest/AI
  modules — none of that changed.
- The other ~40 ThreadPoolExecutor sites (flagged above, Phase 3/4 work).
- `scheduled_jobs.py`, `recommendation_scheduler.py`, `signal_notifier.py`,
  `agentic_ai_scanner.py` internals — left as separate threads, by choice
  (see above).

## Verified before delivery

- All modified/new files byte-compile cleanly.
- `create_app()` boots successfully end-to-end (40 blueprints registered,
  no new errors) with the real dependency set installed.
- Confirmed live: all 9 target jobs register with the single hub thread;
  total process thread count dropped from ~16 to 7.
- Wrote and ran isolated unit tests against `unified_scheduler.register()`
  confirming due-job dispatch and that `job_registry` schedule overrides
  (the Scheduler Hub UI page) still take precedence correctly.

## One behavior change worth knowing about

Telegram/trade-alert watchers previously re-read a *dynamic* alert
interval (`get_global_alert_interval_seconds()`) on every single loop
iteration. They now use that value as their scheduling default at
registration time, with `job_registry`'s schedule (editable from the
Scheduler Hub page) as the live override — same place you'd change it,
slightly different mechanism underneath.
