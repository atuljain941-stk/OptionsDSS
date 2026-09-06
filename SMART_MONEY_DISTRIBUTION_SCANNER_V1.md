# Smart Money Distribution Scanner — V1

Adds the mirror-image companion to `institutional_scanner.py` (which already
covered the accumulation/breakout side): a distribution/breakdown scanner
that flags names where volume asymmetry, distribution-day clustering, churn
days, and RS-line rollover suggest institutional selling while price is
still elevated.

## Files added
- `oiapp/scanners/smart_money_distribution_scanner.py` — new Flask blueprint
  `dist_bp` at `/scanner/distribution`, routes `/scan`, `/scan_cached`,
  `/defaults`. Structured identically to `institutional_scanner.py`
  (same `_conn()`/`_get_symbols()`/ThreadPoolExecutor/`app_cache` pattern)
  so it's a drop-in sibling, not a new paradigm.
- `scripts/smart_money_backtest.py` — pandas port of the scan logic for
  offline backtesting against `price_cache` history (not live yfinance).
- `scripts/smart_money_watchlist.py` — the 104-symbol watchlist as a plain
  Python list, importable by the offline scanner.
- `scripts/smart_money_offline_scan.py` — CLI: reads `price_cache` directly
  via `oiapp.config.DB_PATH`, runs either scanner, and supports
  `--backtest` mode to compute historical hit rate per symbol before you
  trust the live thresholds.

## Files changed (additive only — see verification below)
- `oiapp/app_factory.py` — registers `dist_bp` immediately after `inst_bp`,
  using the exact same try/except pattern as every other blueprint
  registration in the file. No existing registration lines touched.
- `oiapp/db.py` — two additive changes only:
  1. New table `smart_money_scan_history` (CREATE TABLE IF NOT EXISTS,
     inserted right before `init_db()`'s existing `con.close()`) — persists
     scan hits from both scanners over time for later backtesting.
  2. Two new functions appended at the end of the file:
     `save_smart_money_scan_result()` and `get_smart_money_scan_history()`.
  No existing table, column, or function was modified or removed.
- `oiapp/scanners/institutional_scanner.py` — one additive block: after
  the existing `app_cache` write, results are also (best-effort,
  try/except-wrapped) persisted into `smart_money_scan_history`. The
  existing scoring, filters, response shape, and `app_cache` write are
  byte-for-byte unchanged.

## Verification performed before packaging
- All new/modified `.py` files pass `py_compile`.
- `init_db()` was run twice in a row against a fresh synthetic DB
  (idempotency check) — no errors, new table present with expected columns.
- `init_db()` was run against a simulated pre-existing DB seeded with real
  rows in `options` and `trades` — confirmed after migration that **all
  original rows in both tables were preserved exactly**, and no existing
  columns were dropped.
- `run_watchlist_scan` / offline scanner smoke-tested end-to-end against a
  synthetic SQLite DB matching the confirmed real `price_cache` schema
  (`symbol, date, open, high, low, close, volume`, `PRIMARY KEY(symbol,date)`).

## Known gaps / next steps
- The two Flask-blueprint scanners fetch live from yfinance per scan
  (matching `institutional_scanner.py`'s existing pattern) rather than
  reading `price_cache` — same architectural note already flagged for
  `chart_service.py`. The offline `scripts/smart_money_offline_scan.py`
  path is the one that reads `price_cache` directly.
- Distribution scanner thresholds (`udvr_threshold`, `min_dist_days`, etc.)
  are IBD-style starting points, not tuned to your specific watchlist —
  use `--backtest` mode in the offline script to validate before trusting
  live results.
- GEX/gamma-flip overlay on top of scanner hits (discussed earlier) is not
  yet implemented — natural next layer once this is validated.

---

# V2 — price_cache instead of live yfinance, plus UI tabs

## What changed
1. **Both scanners now read from `price_cache` instead of live-fetching
   yfinance on every scan.** Since `price_cache` already holds 3+ years
   of daily history, live-fetching was pure waste — added latency, and
   yfinance rate-limit exposure, for data already sitting locally.
   - `institutional_scanner.py`: new `_get_price_history(symbol,
     min_days=400)` replaces the `yf.Ticker(...).history(...)` call.
     OI data (`_get_oi_data`) was already local (`options` table) and is
     unchanged.
   - `smart_money_distribution_scanner.py`: same `_get_price_history()`
     added, plus `_get_bench_closes()` (SPY) switched from yfinance to
     `price_cache` as well.
   - Net effect: a full-watchlist scan should now run in low single-digit
     seconds instead of 30-90s, with no network dependency.

2. **UI tabs added.** Two things were discovered missing from this export:
   - `templates/index.html` already had a nav button
     (`data-tab="inst-scan"`, labeled "Inst. Breakout") and `app.js`
     already had the full `_inst*` JS handlers wired to it — but the
     actual `<div id="tab-inst-scan">` section markup wasn't present
     anywhere in the included templates. Added it, with field IDs
     matching exactly what `_instGetParams()`/`_instRenderTable()`
     already expected (`inst-watchlist`, `btn-inst-run`, `inst-status`,
     `inst-results`, `inst-p-*` criteria inputs, `inst-criteria-panel`).
   - The distribution scanner had no UI at all. Added a new nav button
     (`data-tab="dist-scan"`, "Inst. Breakdown"), a matching section, and
     a full parallel set of JS handlers (`_distLoadWatchlists`,
     `_distRenderTable`, `_distGetParams`, `_distToggleCriteria`,
     `_distResetCriteria`, `_distRunScan`, `_distLoadCache`) mirroring the
     institutional ones exactly, hitting `/scanner/distribution/*`.

## Verification performed before packaging
- `py_compile` on both modified scanner files.
- `node --check` on `app.js`.
- HTML `<div>`/`</div>` tag-balance check on the newly inserted section
  (22 open / 22 close — confirmed balanced).
- Built a synthetic `price_cache` (AAPL + SPY, 400 trading days each,
  matching the real schema) and:
  - Called `_get_price_history()` and `_get_bench_closes()` directly —
    confirmed correct row counts and ascending date order.
  - Registered both blueprints against a **real Flask app + test client**
    (not just direct function calls) and hit `/scanner/institutional/scan`
    and `/scanner/distribution/scan` end-to-end — both returned HTTP 200
    with well-formed JSON, zero errors, and no network calls.

## Known gaps
- I don't have your real `options_data.db`, so this was verified against
  synthetic data matching the confirmed schema, not your actual price
  history — the synthetic run returning 0 hits is expected (random-walk
  data rarely satisfies the base/RSI/EMA filters), not a sign of a bug.
- The new UI tabs weren't visually verified in a browser (no way to run
  the actual page here) — the HTML/JS was checked for syntax and ID
  consistency with the existing JS handlers, but give it a real click
  through once deployed.
- The distribution tab's criteria panel exposes a subset of
  `DEFAULTS` (`min_score`, `udvr_lookback`, `udvr_threshold`,
  `dist_lookback`, `min_dist_days`, `stall_lookback`, `min_churn_days`,
  `pct_near_high_max`) — `dist_pct_threshold`, `dist_vol_multiplier`,
  `min_price`, and `rs_symbol` are left at their code defaults and not
  yet exposed as UI fields, matching how the institutional tab similarly
  doesn't expose every single `DEFAULTS` key either.
