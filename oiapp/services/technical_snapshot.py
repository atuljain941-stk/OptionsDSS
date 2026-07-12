"""
oiapp/services/technical_snapshot.py
-------------------------------------
Precomputes the common technical primitives (RSI14, RSI3, EMA(RSI14,13),
EMA(RSI14,90)/rsidiff90, EMA9/20/50/60/200, MACD, ADX/DI+/DI-, bar strength
vs EMA60, support/resistance) for every watchlist symbol, daily AND
weekly, and stores them in a dedicated table.

Why this exists: scanner_builder.py, conviction_scorer.py, regime_scanner.py,
and trade_opportunity_scanner.py each independently recompute overlapping
technical indicators from raw price history on every single query/scan --
the same RSI14 for the same symbol on the same day gets computed from
scratch potentially dozens of times across different features in a single
day. Precomputing once and reading from a table is what this module builds;
wiring each of those consumers to read from it instead of recomputing is a
deliberately separate, incremental follow-up (see the module docstring
note at the bottom) rather than a single risky sweep touching every
consumer at once.

Reuses the ALREADY-VALIDATED _rsi/_ema/_macd from scanner_builder.py
(same formulas already cross-checked against Pine Script's documented RMA
convention and a textbook Wilder's RSI reference) rather than a third
independent implementation.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from ..scanners.scanner_builder import _rsi as get_rsi, _ema as get_ema, _macd as get_macd

DB_PATH = Path(__file__).resolve().parents[2] / "options_data.db"


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    return con


def _ensure_table():
    con = _conn()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS technical_snapshot (
                symbol          TEXT NOT NULL,
                timeframe       TEXT NOT NULL,
                date            TEXT NOT NULL,
                close           REAL,
                rsi3            REAL,
                rsi14           REAL,
                ema_rsi14_13    REAL,
                ema_rsi14_90    REAL,
                rsidiff90       REAL,
                rsidiff90_trusted INTEGER,
                ema9            REAL,
                ema20           REAL,
                ema50           REAL,
                ema60           REAL,
                ema200          REAL,
                bar_strength_vs_ema60 REAL,
                macd            REAL,
                macd_signal     REAL,
                macd_hist       REAL,
                adx             REAL,
                di_plus         REAL,
                di_minus        REAL,
                sr_support      REAL,
                sr_resistance   REAL,
                computed_at     TEXT NOT NULL,
                PRIMARY KEY (symbol, timeframe, date)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_techsnap_symbol_tf ON technical_snapshot(symbol, timeframe)")
        con.commit()
    finally:
        con.close()


def _dmi_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    """Wilder's DMI/ADX -- same RMA smoothing convention (alpha=1/period,
    matching Pine's ta.rma()) already validated elsewhere in this project
    for RSI. Returns (adx, di_plus, di_minus) as pandas Series."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move.clip(lower=0)
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move.clip(lower=0)

    tr1 = (high - low)
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, 1e-9)
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, 1e-9)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx, plus_di, minus_di


def compute_technical_snapshot(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Pure computation: given an OHLCV DataFrame (Open/High/Low/Close or
    open/high/low/close, newest last), returns the full primitive set for
    the LATEST bar. Returns None if there isn't enough data to compute
    anything meaningful."""
    if df is None or df.empty or len(df) < 25:
        return None

    cols = {c.lower(): c for c in df.columns}
    close = df[cols.get("close", "Close")].astype(float)
    high = df[cols.get("high", "High")].astype(float) if "high" in cols or "High" in df.columns else close
    low = df[cols.get("low", "Low")].astype(float) if "low" in cols or "Low" in df.columns else close

    rsi3 = get_rsi(close, 3)
    rsi14 = get_rsi(close, 14)
    ema_rsi13 = get_ema(rsi14, 13)

    valid_rsi_bars = int(rsi14.notna().sum())
    rsidiff90_trusted = valid_rsi_bars >= 540  # same threshold measured/validated in scanner_builder.py
    if rsidiff90_trusted:
        ema_rsi90 = get_ema(rsi14, 90)
        rsidiff90 = float((rsi14 - ema_rsi90).iloc[-1])
        ema_rsi90_val = float(ema_rsi90.iloc[-1])
    else:
        rsidiff90 = None
        ema_rsi90_val = None

    ema9 = get_ema(close, 9)
    ema20 = get_ema(close, 20)
    ema50 = get_ema(close, 50)
    ema60 = get_ema(close, 60)
    ema200 = get_ema(close, 200) if len(close) >= 200 else None

    bar_strength_vs_ema60 = None
    if ema60 is not None and not ema60.empty and ema60.iloc[-1]:
        bar_strength_vs_ema60 = round(float((close.iloc[-1] - ema60.iloc[-1]) / ema60.iloc[-1] * 100), 4)

    macd_line, macd_signal, macd_hist = get_macd(close)

    adx_val = di_p = di_m = None
    if len(df) >= 20:
        try:
            adx_s, dip_s, dim_s = _dmi_adx(high, low, close, 14)
            adx_val = round(float(adx_s.iloc[-1]), 2)
            di_p = round(float(dip_s.iloc[-1]), 2)
            di_m = round(float(dim_s.iloc[-1]), 2)
        except Exception:
            pass

    sr_support = sr_resistance = None
    try:
        from ..charts.chart_primitives import tv_sr_channels
        sr_df = df.rename(columns={cols.get("open","Open"): "Open", cols.get("high","High"): "High",
                                    cols.get("low","Low"): "Low", cols.get("close","Close"): "Close"})
        channels = tv_sr_channels(sr_df)
        spot = float(close.iloc[-1])
        below = [c for c in channels if c.get("low", 0) <= spot]
        above = [c for c in channels if c.get("high", 0) >= spot]
        if below:
            sr_support = round(max(c["high"] for c in below), 4)
        if above:
            sr_resistance = round(min(c["low"] for c in above), 4)
    except Exception:
        pass

    return {
        "close": round(float(close.iloc[-1]), 4),
        "rsi3": round(float(rsi3.iloc[-1]), 4) if pd.notna(rsi3.iloc[-1]) else None,
        "rsi14": round(float(rsi14.iloc[-1]), 4) if pd.notna(rsi14.iloc[-1]) else None,
        "ema_rsi14_13": round(float(ema_rsi13.iloc[-1]), 4) if pd.notna(ema_rsi13.iloc[-1]) else None,
        "ema_rsi14_90": round(ema_rsi90_val, 4) if ema_rsi90_val is not None else None,
        "rsidiff90": round(rsidiff90, 4) if rsidiff90 is not None else None,
        "rsidiff90_trusted": rsidiff90_trusted,
        "ema9": round(float(ema9.iloc[-1]), 4),
        "ema20": round(float(ema20.iloc[-1]), 4),
        "ema50": round(float(ema50.iloc[-1]), 4),
        "ema60": round(float(ema60.iloc[-1]), 4),
        "ema200": round(float(ema200.iloc[-1]), 4) if ema200 is not None else None,
        "bar_strength_vs_ema60": bar_strength_vs_ema60,
        "macd": round(float(macd_line.iloc[-1]), 4),
        "macd_signal": round(float(macd_signal.iloc[-1]), 4),
        "macd_hist": round(float(macd_hist.iloc[-1]), 4),
        "adx": adx_val, "di_plus": di_p, "di_minus": di_m,
        "sr_support": sr_support, "sr_resistance": sr_resistance,
    }


import threading as _threading_mod
import queue as _queue_mod

_write_queue: "_queue_mod.Queue" = _queue_mod.Queue()
_write_worker_started = False
_write_worker_lock = _threading_mod.Lock()


def _ensure_write_worker():
    """Starts the single background thread that actually performs
    technical_snapshot writes, draining _write_queue sequentially. Because
    SQLite only allows one writer at a time (even in WAL mode), having
    write-through call sites (regime_scanner.py, scanner_builder.py) write
    directly from inside a ThreadPoolExecutor(max_workers=8) scan loop
    means up to 8 threads can contend for that single write lock
    simultaneously -- with a 5s busy_timeout per attempt, this can add up
    to real, multi-minute delays across a large watchlist scan. Routing
    all writes through one queue + one writer thread eliminates that
    contention entirely: scanning threads enqueue (a fast, in-memory,
    non-blocking operation) and move on immediately."""
    global _write_worker_started
    with _write_worker_lock:
        if _write_worker_started:
            return
        _write_worker_started = True

    def _worker():
        while True:
            item = _write_queue.get()
            try:
                symbol, timeframe, date_str, snap = item
                store_technical_snapshot(symbol, timeframe, date_str, snap)
            except Exception as e:  # noqa: BLE001
                print(f"[technical_snapshot] background write failed: {type(e).__name__}: {e}")
            finally:
                _write_queue.task_done()

    t = _threading_mod.Thread(target=_worker, name="technical-snapshot-writer", daemon=True)
    t.start()


def queue_technical_snapshot_write(symbol: str, timeframe: str, date_str: str, snap: Dict[str, Any]) -> None:
    """What write-through call sites (regime_scanner.py, scanner_builder.py)
    should use instead of calling store_technical_snapshot() directly --
    enqueues the write and returns immediately without touching the
    database on the calling (scanning) thread at all. See
    _ensure_write_worker for why this matters under concurrent scans."""
    _ensure_write_worker()
    _write_queue.put((symbol, timeframe, date_str, snap))


def store_technical_snapshot(symbol: str, timeframe: str, date_str: str, snap: Dict[str, Any]) -> None:
    """Merges with any existing row for this (symbol, timeframe, date) key
    rather than blindly overwriting -- different callers populate
    different subsets of fields (regime_scanner.py computes ADX/DI+/DI-
    but not RSI3/S-R; this module's own compute_technical_snapshot()
    computes RSI3/S-R but not ADX/DI). A blind INSERT OR REPLACE would
    make whichever call happened most recently silently wipe out fields
    the other had already filled in. Merge rule: a new non-None value
    overwrites (fresher data wins for anything both sides compute); a new
    None value does NOT clobber an existing non-None value."""
    _ensure_table()
    con = _conn()
    con.row_factory = sqlite3.Row
    try:
        existing = con.execute(
            "SELECT * FROM technical_snapshot WHERE symbol=? AND timeframe=? AND date=?",
            (symbol, timeframe, date_str),
        ).fetchone()
        merged = dict(snap)
        if existing:
            existing_d = dict(existing)
            for key, new_val in snap.items():
                if new_val is None and existing_d.get(key) is not None:
                    merged[key] = existing_d[key]

        cols = ["symbol", "timeframe", "date"] + list(merged.keys()) + ["computed_at"]
        placeholders = ",".join("?" * len(cols))
        values = [symbol, timeframe, date_str] + list(merged.values()) + [datetime.now().isoformat(timespec="seconds")]
        con.execute(f"INSERT OR REPLACE INTO technical_snapshot ({','.join(cols)}) VALUES ({placeholders})", values)
        con.commit()
    finally:
        con.close()


def get_technical_snapshot(symbol: str, timeframe: str = "1d") -> Optional[Dict[str, Any]]:
    """Read the most recent stored snapshot for a symbol+timeframe --
    what consumers (scanners, scoring, regime) would read from instead of
    recomputing live, once wired up (see module docstring)."""
    _ensure_table()
    con = _conn()
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM technical_snapshot WHERE symbol=? AND timeframe=? ORDER BY date DESC LIMIT 1",
            (symbol.upper(), timeframe),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def is_snapshot_complete_today(symbol: str, timeframe: str = "1d") -> bool:
    """Cheap check (one indexed SELECT, no merge logic) for whether a
    write-through can be skipped entirely -- write-through callers
    (regime_scanner.py, scanner_builder.py's query engine) should call
    this FIRST and skip their own store_technical_snapshot() call if it
    returns True, rather than unconditionally doing a SELECT+INSERT merge
    on every single symbol on every single scan. This is what actually
    keeps repeated scanner passes over the same watchlist cheap -- most
    calls after the first pass of the day should hit this fast path and
    do zero extra database writes."""
    symbol = symbol.upper().strip()
    today = datetime.now().strftime("%Y-%m-%d")
    _ensure_table()
    con = _conn()
    try:
        row = con.execute(
            "SELECT rsi3, computed_at FROM technical_snapshot WHERE symbol=? AND timeframe=? ORDER BY date DESC LIMIT 1",
            (symbol, timeframe),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return False
    rsi3, computed_at = row
    return rsi3 is not None and computed_at is not None and str(computed_at)[:10] == today


def get_or_compute_technical_snapshot(symbol: str, timeframe: str = "1d") -> Optional[Dict[str, Any]]:
    """The actual read-through cache consumers should call: if today's
    record already exists for this symbol+timeframe, return it as-is --
    do NOT recompute (this is the whole point: once built for the day,
    every subsequent scan/score/query for that symbol+timeframe reuses
    it rather than redoing the same work). If no record exists for today,
    compute it now (synchronously -- this is a local computation over
    already-cached price data, not a network fetch, so blocking briefly
    here is fine and different from the price-backfill blocking issue
    fixed earlier), store it, and return the freshly-built result.

    Symbol is normalized/upper-cased consistently with the rest of this
    module. Returns None only if computation was attempted and genuinely
    failed (e.g. no price history available at all for this symbol)."""
    symbol = symbol.upper().strip()
    today = datetime.now().strftime("%Y-%m-%d")

    _ensure_table()
    con = _conn()
    try:
        row = con.execute(
            "SELECT date, rsi3, computed_at FROM technical_snapshot WHERE symbol=? AND timeframe=? ORDER BY date DESC LIMIT 1",
            (symbol, timeframe),
        ).fetchone()
    finally:
        con.close()

    # Use computed_at's date (when this record was last built/refreshed),
    # not the `date` column (which trading day the data represents) --
    # market data legitimately has no bar dated "today" on weekends or
    # holidays, so comparing against calendar-today on the `date` column
    # would cause needless recomputation attempts every single check over
    # a weekend even though nothing has changed since Friday's close.
    computed_today = row is not None and row[2] is not None and str(row[2])[:10] == today

    # rsi3 is only ever populated by this module's own full computation
    # (compute_technical_snapshot) -- regime_scanner.py's write-through
    # deliberately leaves it None, since it doesn't compute RSI3 itself.
    # Using it here as the "is this a complete record" signal: a record
    # from regime_scanner's write-through alone (rsi14/macd/adx/emas
    # present, but rsi3/sr/ema_rsi14_13 still None) should NOT permanently
    # block the fuller computation from running later the same day.
    is_complete = row is not None and row[1] is not None

    if computed_today and is_complete:
        # Already fully built for today -- skip recomputation, just return it.
        return get_technical_snapshot(symbol, timeframe)

    # No record for today (either never computed, or stale from a prior
    # day) -- build it now and store it before returning.
    try:
        ok = compute_and_store_for_symbol(symbol, timeframe)
    except Exception as e:  # noqa: BLE001
        print(f"[technical_snapshot] get_or_compute failed for {symbol} ({timeframe}): {type(e).__name__}: {e}")
        ok = False

    if not ok:
        # Computation failed (e.g. no price history yet) -- fall back to
        # whatever's on record, even if stale, rather than nothing at all.
        return get_technical_snapshot(symbol, timeframe)

    return get_technical_snapshot(symbol, timeframe)


def compute_and_store_for_symbol(symbol: str, timeframe: str = "1d") -> bool:
    """Computes and persists the full primitive set for one symbol+timeframe,
    using the SAME history source (_history()) that scanner_builder.py's
    live computations already use -- including its backfill-queueing
    behavior for thin-history symbols, so this benefits from and stays
    consistent with the price backfill pipeline already built."""
    from ..scanners.scanner_builder import _history
    df = _history(symbol, timeframe)
    if df is None or df.empty:
        return False
    snap = compute_technical_snapshot(df)
    if snap is None:
        return False
    date_str = pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")
    store_technical_snapshot(symbol, timeframe, date_str, snap)
    return True


def run_technical_snapshot_batch(symbols: List[str], timeframes: Optional[List[str]] = None,
                                  delay_seconds: float = 0.3) -> Dict[str, int]:
    """Computes and stores snapshots for every symbol x timeframe given.
    Meant to be called from either the background watcher (small batches,
    rate-limited) or the manual bulk-trigger endpoint (a full watchlist at
    once) -- same underlying function, different callers/batch sizes,
    matching the price-backfill pattern already established."""
    timeframes = timeframes or ["1d", "1w"]
    result = {"processed": 0, "succeeded": 0, "failed": 0}
    for symbol in symbols:
        for tf in timeframes:
            result["processed"] += 1
            try:
                ok = compute_and_store_for_symbol(symbol, tf)
            except Exception as e:  # noqa: BLE001
                print(f"[technical_snapshot] {symbol} ({tf}) failed: {type(e).__name__}: {e}")
                ok = False
            result["succeeded" if ok else "failed"] += 1
            time.sleep(delay_seconds)
    return result


# ---------------------------------------------------------------------
# Automatic background watcher -- small batches, continuous, matches the
# price-backfill watcher pattern exactly.
# ---------------------------------------------------------------------
_watcher_started = False
_watcher_lock = None


def start_technical_snapshot_watcher(interval_seconds: int = 180, batch_symbols: int = 10):
    global _watcher_started, _watcher_lock
    if _watcher_lock is None:
        _watcher_lock = threading.Lock()
    with _watcher_lock:
        if _watcher_started:
            return False
        _watcher_started = True

    def _loop():
        from ..services.job_registry import register_job, is_enabled, mark_run
        register_job(
            "technical_snapshot_cache", "Technical indicator precompute cache",
            "Precomputes RSI14/RSI3/EMA-RSI-90/rsidiff90/EMA9-20-50-60-200/MACD/ADX/DI+-/S-R "
            "for watchlist symbols, daily + weekly, so scanners and scoring can read a cached "
            "value instead of recomputing from raw price history every time.",
            kind="interval", default_schedule={"interval_min": max(1, int(interval_seconds / 60))},
            group="Alert Watchers",
            run_now_fn=lambda: run_technical_snapshot_batch(_next_batch_symbols(batch_symbols)),
        )
        while True:
            if is_enabled("technical_snapshot_cache"):
                try:
                    symbols = _next_batch_symbols(batch_symbols)
                    result = run_technical_snapshot_batch(symbols) if symbols else {"processed": 0}
                    mark_run("technical_snapshot_cache", True, f"{result}")
                except Exception as e:  # noqa: BLE001
                    mark_run("technical_snapshot_cache", False, str(e))
            time.sleep(max(30, interval_seconds))

    t = threading.Thread(target=_loop, name="technical-snapshot-watcher", daemon=True)
    t.start()
    return True


def _next_batch_symbols(n: int) -> List[str]:
    """Cycles through the union of all watchlist symbols, oldest-computed
    first, so every symbol eventually gets refreshed rather than the
    watcher only ever touching the first N alphabetically."""
    try:
        from ..scanners.trade_opportunity_scanner import _watchlist_symbols as _tos_symbols
        all_symbols = _tos_symbols(None)
    except Exception:
        all_symbols = []
    if not all_symbols:
        return []
    _ensure_table()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT symbol, MAX(computed_at) as last FROM technical_snapshot "
            "WHERE symbol IN ({}) GROUP BY symbol".format(",".join("?" * len(all_symbols))),
            all_symbols,
        ).fetchall()
        last_computed = {r[0]: r[1] for r in rows}
    finally:
        con.close()
    # Symbols never computed sort first (None sorts before any timestamp string)
    ranked = sorted(all_symbols, key=lambda s: last_computed.get(s) or "")
    return ranked[:n]


# ---------------------------------------------------------------------
# Manual bulk trigger -- full watchlist at once, on demand.
# ---------------------------------------------------------------------
from flask import Blueprint, jsonify, request  # noqa: E402

technical_snapshot_bp = Blueprint("technical_snapshot", __name__, url_prefix="/technical-snapshot")

_bulk_status: Dict[str, Any] = {"running": False, "processed": 0, "total": 0, "succeeded": 0, "failed": 0}
_bulk_lock = None


@technical_snapshot_bp.route("/api/bulk-compute", methods=["POST"])
def api_bulk_compute():
    global _bulk_lock
    if _bulk_lock is None:
        _bulk_lock = threading.Lock()
    payload = request.get_json(force=True) or {}
    timeframes = payload.get("timeframes") or ["1d", "1w"]
    symbols = payload.get("symbols")
    if not symbols:
        try:
            from ..scanners.trade_opportunity_scanner import _watchlist_symbols as _tos_symbols
            symbols = _tos_symbols(payload.get("watchlist_id"))
        except Exception:
            symbols = []
    symbols = list(dict.fromkeys(str(s).upper().strip() for s in (symbols or []) if s))
    if not symbols:
        return jsonify({"error": "No symbols found -- pass watchlist_id or symbols"}), 400
    if _bulk_status["running"]:
        return jsonify({"error": "A bulk compute is already running", "status": _bulk_status}), 409

    def _run():
        global _bulk_status
        with _bulk_lock:
            _bulk_status = {"running": True, "processed": 0, "total": len(symbols) * len(timeframes),
                             "succeeded": 0, "failed": 0}
        for symbol in symbols:
            for tf in timeframes:
                try:
                    ok = compute_and_store_for_symbol(symbol, tf)
                except Exception:
                    ok = False
                with _bulk_lock:
                    _bulk_status["processed"] += 1
                    _bulk_status["succeeded" if ok else "failed"] += 1
                time.sleep(0.3)
        with _bulk_lock:
            _bulk_status["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "started": True, "symbol_count": len(symbols), "timeframes": timeframes})


@technical_snapshot_bp.route("/api/bulk-compute/status")
def api_bulk_compute_status():
    return jsonify(_bulk_status)


@technical_snapshot_bp.route("/api/snapshot/<symbol>")
def api_get_snapshot(symbol):
    tf = request.args.get("timeframe", "1d")
    snap = get_technical_snapshot(symbol, tf)
    if snap is None:
        return jsonify({"symbol": symbol, "timeframe": tf, "error": "no snapshot computed yet"})
    return jsonify(snap)
