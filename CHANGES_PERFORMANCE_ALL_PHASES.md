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

---

# Phase 3, 4, 5 — Batching, Duplicate-Load Elimination, Instrumentation + Scanner Builder Response Time

## Phase 3 — in-memory snapshot cache (mostly already delivered in Phase 2)

The Phase 2 in-memory tier in front of `scanner_snapshot_cache` already
covers what Phase 3 originally asked for ("in-memory cache so most
queries skip SQLite"). Verified end-to-end with a synthetic dataset:
first lookup for a symbol computes fresh (`snapshot_computed`), the
second identical lookup is a pure in-memory hit
(`snapshot_memory_hits`) — no SQLite round-trip. Nothing new needed here
beyond what Phase 2 already shipped.

## Phase 4 — batch DB access, eliminate duplicate loads

1. **Bulk intraday preload**, mirroring the daily one from Phase 2:
   `_bulk_preload_intraday_history()` loads the whole watchlist's
   `intraday_price_cache` rows in one `WHERE symbol IN (...)` query.
   Gated so it only runs when a scan's required timeframes actually
   include 1h/2h/4h — no cost added to pure-daily scans.
2. **Eliminated a real duplicate-I/O source**: `_market_bars_daily_history()`
   was re-checking whether `data/market_data.db` exists (a filesystem
   `stat()`) on every cache-miss call, once per unique symbol per
   process. That existence check is now resolved and cached once per
   process (`_market_data_db_path()`).
3. **More ThreadPoolExecutor sites migrated** to the shared pool from
   Phase 1's `task_executor.py`: `regime_scanner.py` (daily 7AM job +
   manual trigger), `sector_service.py`, and both fan-out blocks in
   `signal_notifier.py` (runs regularly via its watcher). That's 6 of
   the ~40 sites now on the shared pool total (2 from Phase 1 + 4 here).
   I deliberately did **not** attempt to mechanically migrate the
   remaining ~34 in this pass — each one has slightly different
   context (some are persistent single-worker executors used for
   timeout-wrapping or cross-request job tracking, not fan-out pools,
   and blindly rewriting `with ThreadPoolExecutor(...) as ex:` blocks
   across files I can't live-test against real market data is exactly
   the kind of risk not worth taking silently). Remaining candidates,
   for a future pass: `ai_hub.py` (2 fan-out sites; its 2 single-worker
   executors should stay separate), `weekly_analysis.py` (3),
   `oi_buildup_scanner.py` / `oi_buildup_core.py`, `sr_breakout_scanner.py`
   (5), `opportunity_scanner.py`, `trend_exhaustion_scanner.py`,
   `momentum_retrace_scanner.py`, `market_structure.py`,
   `intraday_routes.py` (2), `options_analysis.py` (2), `backtest.py`,
   `routes_strategy.py`, `institutional_scanner.py`, `symbol_screener.py`,
   `trade_planner.py`, `rsi_mtf_scanner.py`, `trend_second_pullback_scanner.py`,
   `api/routes.py`, `maya_pages.py`. Explicitly left alone:
   `autotrading/schwab_eod.py` (order execution — real-money risk, not
   something to touch in a mechanical batch) and `realtime_dashboard.py`
   (live websocket streaming, different concurrency model).

## Phase 5 — instrumentation + Scanner Builder response time

New `oiapp/services/profiling.py`:
- `Timer` — a context manager for wall-clock elapsed ms.
- Per-endpoint rolling request-timing history (last 200 calls): count,
  avg, min, max, p50, p95.
- Request-scoped counters (cache memory/sqlite/computed hits, DB
  connections opened) that any module can increment from any thread
  during a request and read back at the end.
- Cumulative, process-lifetime totals of the same counters, for the
  diagnostics page.
- `system_snapshot()` — live thread count/names, Scheduler Hub job
  states, shared task pool size.

Wired in:
- `scanner_builder.py`'s `_conn()` now counts every SQLite connection it
  opens.
- `_prepare_snapshot_cached()` now tags every lookup as a memory hit,
  SQLite hit, or fresh compute.
- Scanner Builder's `/api/run` endpoint now times the whole request and
  returns a `timing` block in its JSON response:
  ```json
  "timing": {
    "elapsed_ms": 340.2,
    "symbols_requested": 104,
    "symbols_scanned": 104,
    "cache": {"memory_hits": 61, "sqlite_hits": 12, "computed": 31},
    "db_connections_opened": 9
  }
  ```
  Every run is also recorded into the rolling history for the
  diagnostics page.

**New `/diagnostics` page** (linked from the app, self-refreshing every
5s): system stats (thread count, shared pool size, cache hit rate, DB
connections opened), a dedicated Scanner Builder response-time panel
(last/avg/p95, symbols/matches, cache breakdown for the most recent
scan), a table of every endpoint with timing recorded, the live thread
list, and the Scheduler Hub job table (interval, low-priority flag,
running state, last run) — all in one place.

**Scanner Builder page itself** — this is the part you actually asked
for: next to the status line, there's now a response-time badge that
shows up after every scan or "Validate":
```
⏱ 340ms server · 410ms total · cache 61 mem / 12 db / 31 new
```
- "server" = time spent inside the Flask endpoint (the number in the
  `timing` block above).
- "total" = full round trip measured in the browser
  (`performance.now()`), so the gap between the two numbers is your
  network/proxy overhead, not app logic.
- The badge is color-coded: green under 1s, amber 1–3s, red above 3s —
  glanceable without reading the numbers.
- The cache breakdown tells you *why* a scan was fast or slow: high
  "new" count means most symbols were computed fresh (first scan of the
  day, or after a data refresh); high "mem"/"db" means it was mostly
  cache hits.

## Verified before delivery

- All modified/new files byte-compile and AST-parse cleanly.
- `create_app()` boots end-to-end again after all Phase 3/4/5 changes —
  41 blueprints now (was 40), no new errors.
- `/diagnostics/api/snapshot` returns 200 with the expected shape via
  Flask's test client.
- Wrote a synthetic-data functional test (temp SQLite DB, no network)
  exercising the full path: bulk daily preload → lru-cached loader →
  `_prepare_snapshot_cached` — confirmed a first call registers as
  `snapshot_computed` and an immediately repeated call for the same
  symbol/timeframe/date registers as `snapshot_memory_hits`, with zero
  extra SQLite reads on the hit.

## One behavior change worth knowing about (from Phase 1/2, still applies)

Telegram/trade-alert watchers previously re-read a *dynamic* alert
interval (`get_global_alert_interval_seconds()`) on every single loop
iteration. They now use that value as their scheduling default at
registration time, with `job_registry`'s schedule (editable from the
Scheduler Hub page) as the live override — same place you'd change it,
slightly different mechanism underneath.

