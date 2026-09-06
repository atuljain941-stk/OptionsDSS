# oiapp Architecture & Performance Audit

Written after the 10-hour scan hang. That bug is the clearest possible evidence for
everything below: a single unbounded `wait()` in one function, 8,000 lines away from
the nearest yfinance call, took down the whole app for a working day. That's not a
one-off mistake — it's what happens when there's no layer whose *job* is to make that
category of bug impossible.

## The numbers

| Metric | Count |
|---|---|
| Total application code | ~83,000 lines |
| Files that open their own SQLite connection independently | 34 |
| Separate `ensure_table()` / schema-creation functions | 19 |
| Direct `yfinance.Ticker(...).history()` call sites | 136, across 28 files |
| Independent `ThreadPoolExecutor(...)` instantiations | 44 |
| Files with a bare `except:` (swallows *everything*, including the signal that would tell you a thread is stuck) | 15 |
| Blueprints (feature modules) registered at startup | 41 |

None of these numbers are "wrong" in isolation. A 40-file app with one shared service
per concern would have these same features implemented in maybe 5-8 places. This one
has them in 20-40. That ratio *is* the architecture problem.

## Root cause: this is a script collection, not a service-oriented app

The app grew as ~40 independent feature modules (`oiapp/scanners/*.py`,
`oiapp/services/*.py`), each written to be self-sufficient rather than to depend on a
shared layer. That's a reasonable way to prototype fast. It stops being reasonable
once the same handful of concerns (get a DB connection, fetch a price, run something
in the background, don't block forever) need to behave *consistently* everywhere,
because "consistently" isn't something you can achieve by writing the same logic 30
times with 30 chances to get a detail slightly wrong.

Concretely, here's what "no service layer" has actually cost, in bugs we've already
hit this week:

1. **A production database got overwritten** by a zip extraction, because the DB path
   was computed independently in 60 places instead of read from one config.
2. **Tastytrade credentials appeared missing** in a way that took real investigation,
   because credential storage isn't consolidated with the rest of app settings in an
   obvious place.
3. **The same "delisted symbol costs 20 seconds" bug** had to be found and fixed *four
   separate times*, in four different files, because there's no single function that
   "the app" calls to get a price — there are dozens of copies of roughly that logic.
4. **A scan hung for 10 hours** because the one function that fans out work across
   threads had no deadline, and nothing in the surrounding architecture made "every
   concurrent operation has a deadline" a rule instead of a per-instance decision.

Each of these got fixed. None of them are the last one of their kind, because the
condition that produced them — many independent implementations of the same concern —
is still true everywhere I haven't personally touched.

## What's already been consolidated (this week)

Worth stating plainly, since it's real progress and the template for the rest:

- **DB path**: `oiapp/config.py` is now the single source of truth (was 60 independent
  computations).
- **Background scheduling**: `unified_scheduler.py` + `scheduled_jobs.py` replaced ~12
  independent always-on threads and a hardcoded duplicate 7AM scheduler.
- **yfinance hardening**: `yfinance_hardening.py` patches the two methods that
  everything funnels through (crumb retry, global throttle) — one fix, every caller
  benefits, zero call sites touched.
- **Negative-cache for bad symbols**: `market.py`'s `is_recently_failed` /
  `mark_fetch_failed`, now shared by 6 of the 136 call sites.
- **Scan deadline**: `SCAN_DEADLINE_SECONDS` in scanner_builder.py — the fix for
  today's hang.

Each of these is proof of the pattern that needs to repeat: find the one place
downstream everything actually funnels through, fix it there, and *then* migrate
callers opportunistically rather than promising to touch all 28 files at once.

## The four services this app actually needs

### 1. Market Data Service (highest priority — this is what keeps hurting you)

**Today**: 136 call sites, each deciding independently whether to cache, whether to
retry, whether to time out, whether to remember a failure. `market.py` has
accidentally become 80% of this already — it just isn't mandatory.

**Target**: one module (`market_data_service.py`, or promote `market.py` to this role
formally) that is the *only* thing in the codebase allowed to `import yfinance`. Every
other file calls it. It owns:
- Positive caching (exists)
- Negative caching for known-bad symbols (exists, needs to be mandatory not optional)
- A hard per-call timeout (yfinance's own 30s default is fine, but every caller needs
  to actually hit this path instead of raw `yf.Ticker()`)
- One retry/backoff policy (exists via `yfinance_hardening.py`)
- Consistent logging so a failure shows up in one recognizable format instead of 28
  different `except: pass` styles

**Migration path**: the 136 sites split roughly into "just needs a spot price"
(candidates for `get_spot`/`get_spot_snapshot` directly) and "needs full OHLCV for a
specific reason" (backfill, options chains — these need a *shared* fetch-with-cache
helper that returns a DataFrame, which doesn't fully exist yet and is the real
remaining work). Building that second helper and migrating the ~15 highest-traffic
files (scanners that run on every scan, not the long tail of one-off analysis
scripts) is a realistic multi-day project, not a rewrite.

### 2. Data Access Layer

**Today**: 34 files each write `sqlite3.connect(DB_PATH); c.row_factory =
sqlite3.Row; ...`. 19 separate `ensure_table()` functions, each deciding independently
whether to use `CREATE TABLE IF NOT EXISTS` alone (silently wrong on schema changes —
this already caused the `trade_snapshot.created_at` bug) or a proper migration.

**Target**: a `db.py` (partially exists) that owns:
- The one connection factory (WAL mode, busy_timeout, row_factory — set once)
- A registry of every table's schema + migration function, run once at startup, not
  scattered across 19 functions that each may or may not get called
- A documented pattern for adding a column later (`ALTER TABLE ADD COLUMN` with a
  default, guarded) so the next schema change doesn't repeat the `created_at` bug

### 3. Background Job Service

**Today**: mostly fixed. `unified_scheduler.py` (interval jobs) + `scheduled_jobs.py`
(time-of-day jobs) cover the cases we've touched. Two things still missing:
- A **hard per-job execution deadline**, the same way the scan endpoint now has one.
  Right now a stuck job can occupy a shared worker forever the same way the scan
  could — the fix pattern from today needs to become the default, not a one-off.
- **Job isolation**: all interval jobs currently share one 4-worker pool
  (`unified_scheduler.py`) and all interactive scans share a 12-worker pool
  (`task_executor.py`). A genuinely stuck job in either pool reduces everyone else's
  capacity. Worth considering per-job-class pools once you have real usage data on
  which jobs actually compete for time.

### 4. Concurrency & Timeout Policy (the newest lesson)

**Today**: 44 independent `ThreadPoolExecutor` instances, most using unbounded
`as_completed()` or `.result()` with no timeout — the exact shape of bug that caused
today's hang. I only found and fixed the one in the interactive scan path because you
hit it. The other 43 are unaudited.

**Target**: a documented rule (and ideally a lint check, or at minimum a `grep` you
run before merging) — *no wait on concurrent work without a timeout, ever* — plus a
small shared helper (`oiapp/services/bounded_wait.py` or similar) that wraps
`as_completed`/`wait` with a sane default deadline and consistent partial-result
handling, so every future case of "fan out across threads" gets the deadline for free
rather than needing someone to remember it.

## Prioritized roadmap

**Phase 1 (this session, done)**: scan deadline fix, thread-dump diagnostic tool.

**Phase 2 (next, high value / low risk)**: audit the other 43 `ThreadPoolExecutor`
sites specifically for unbounded waits (not full migration to a shared pool — just
"does this have a timeout"). This is a mechanical, verifiable check, and it's the
category of bug most likely to cause another multi-hour hang.

**Phase 3**: build the shared OHLCV-fetch-with-cache helper and migrate the ~15
highest-traffic market-data call sites (the scanners that run on every scan: regime,
sector, signal notifier's sources, trade opportunity scanner). This is where the next
"CNTA took 20 seconds" bug is most likely to resurface if left alone.

**Phase 4**: consolidate the 19 `ensure_table` functions into one schema registry, so
the next `created_at`-style bug gets caught by a startup check instead of a runtime
failure months later.

**Phase 5** (lower urgency, real payoff): per-job-class worker pools for background
jobs once there's usage data showing which jobs actually contend for capacity.

## What I'm not going to do

Rewrite the app. 83,000 lines with a working trading operation behind it is not a
one-session or even one-week rewrite candidate, and "re-architect everything" as a
single project has a well-known failure mode: a long freeze on new work, a large
diff nobody can fully review, and new bugs introduced faster than old ones are fixed.
The plan above is the alternative — find the one choke point per concern, fix it
there once, migrate callers by traffic priority, and keep the app working and
deployable after every single step. That's exactly the pattern that already fixed the
DB path, the scheduler duplication, and today's scan hang.

## Update: Phases 2 and 4 completed in a follow-up pass

**Phase 2 — thread pool audit: complete.** All 41 real `ThreadPoolExecutor` sites in
the codebase now have a hard timeout, verified by re-running the audit script and
manually confirming every remaining flagged site was a false positive (a comment, or
a pattern the detection script's regex didn't recognize — e.g. `.map(..., timeout=60)`
across a multi-line call). Built `oiapp/services/bounded_wait.py` as the shared
pattern — including a caveat I caught myself mid-migration: `with ThreadPoolExecutor()
as ex:` still blocks on exit regardless of any timeout used inside the block, so every
site needed `ex.shutdown(wait=False)` instead of the context manager. Verified with a
functional test using genuinely-hung workers (`time.sleep(999)`) mixed with real ones,
confirming the pattern correctly returns completed results and reports timeouts
without hanging.

**Phase 4 — schema consolidation: done, via a safer path than originally proposed.**
Rather than hand-maintaining a parallel schema registry (risking the registry itself
drifting from the real `CREATE TABLE` statements — the same class of bug this was
meant to prevent), built `oiapp/services/schema_registry.py`, which calls all 19
`ensure_table()` functions once, deterministically, at startup. Verified: 47 tables
now exist immediately on a fresh boot, instead of depending on whichever feature
happens to be used first (previously, several tables like `app_cache`/`app_config`
only appeared after their specific button was clicked at least once).

**Not attempted in this pass**: Phase 3 (the shared OHLCV-fetch-with-cache helper and
migrating the ~15 highest-traffic market-data call sites) and Phase 5 (per-job-class
worker pools, already marked lower-priority pending real usage data). Phase 3 remains
the most valuable next step — it's where the next "CNTA took 20 seconds" bug is most
likely to resurface.

## Update: Phase 3 completed in a follow-up pass

Built `get_history_cached(symbol, period, interval)` in `market.py` — the shared
OHLCV helper the phase called for, backed by the same negative-cache as
`get_spot_snapshot()`, plus a positive cache (5 min TTL for intraday, 1 hour for
daily/weekly) so repeat requests for the same symbol/timeframe don't hit yfinance
again at all. Verified: a second identical call produces zero new yfinance calls.

Migrated the 4 highest-traffic files: `spy_strategies.py` (10 of 12 sites — 2 left
alone, they deliberately use `prepost=True` for pre/post-market data, which the
helper doesn't support and I didn't want to silently drop), `uae_trade_scanner.py`
(all 8), `earnings.py` (5 of 6 — the 6th is dead code, an unused lambda), and
`sr_breakout_scanner.py` (all 5). That's 28 of the ~95 remaining raw
`yf.Ticker().history()` call sites now cached, prioritized by call count per file
(the busiest scanners first).

**Deliberately not migrated**: `options_analysis.py`'s 5 sites, which all use
`start=`/`end=` date-range fetches (backtesting-style historical windows) rather than
`period=`/`interval=` — a different calling convention the helper doesn't support.
Extending the helper to cover date-range fetches safely was more scope than this pass
should take on; the caching value there is also lower since a backtest fetching one
specific historical window is less likely to repeat the same call within a cache TTL
window, unlike "give me the latest EMA for a scan" which repeats constantly.

Verified with an end-to-end functional test (not just compilation): migrated
functions in both `uae_trade_scanner.py` and `spy_strategies.py` correctly fetch on
first call and serve from cache on a second identical call, using mocked yfinance
data.

**Remaining for a future pass**: ~67 more raw call sites across ~30 files (the long
tail — one-off analysis scripts, lower-traffic scanners). The pattern and the helper
both now exist; it's pure migration work from here, prioritized the same way (call
count per file, then actual usage frequency).
