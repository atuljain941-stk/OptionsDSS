"""dte_pages.py -- V104 addition.

Pre-built, cache-backed data layer for two new pages scoped to Atul's
0-10 DTE trading universe (SPY, QQQ, SPX, IWM):

  1. Intraday Buildup page
     OI does not move intraday -- it settles once overnight and is
     published before/around session open. What CAN move intraday is
     volume at a strike relative to *yesterday's* closing OI. That
     ratio is a same-day proxy/leading indicator for how much real OI
     is likely to move at that strike when tomorrow's snapshot lands.
     This module computes that ratio from the existing `options` table
     (latest_option_rows() already gives us today's row + prior-day OI)
     and writes it to oi_intraday_dte_cache so the page reads a
     pre-built table instead of firing queries live.

  2. Positional / Weekly DTE Trend page
     Real OI trend, but anchored by expiry date (not by a DTE bucket,
     since DTE shifts every day for the same expiry) and scoped to
     whatever expiries are *currently* inside the 0-10 DTE window.
     Computes a rolling N-day OI slope per strike (reusing the same
     simple linear-slope approach as the price-side SlopeDeg primitive)
     so a one-day spike can be told apart from sustained accumulation.
     Also folds in: VIX / VIX9D + SPX near-dated skew (regime context),
     and ES futures OI (current + next 3 expiries) as a cross-check
     against SPX options positioning.

Both computations are cheap (a handful of symbols x a handful of
expiries) and are meant to be triggered by:
  - the 7:30 AM morning pipeline (scheduled_jobs.py) for the positional
    page, since real OI is only final once-a-day and shouldn't be
    computed before Schwab/CME/options-chain data has settled, and
  - a periodic (default 20 min) interval job during market hours for
    the intraday page, registered with job_registry/Scheduler Hub so
    it also gets a manual "Run Now" button, and
  - a direct manual refresh call from either page for an on-demand
    recompute outside those schedules.

No new external data dependency for OI/volume (reuses the existing
`options` table via weekly_oi.py). VIX/VIX9D reuse the existing
yfinance session helper (yf_session.safe_history). ES futures reuse
futures_oi_schwab.get_latest_oi -- already built for the Dashboard.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from ..db import DB_PATH
from . import weekly_oi as _woi

# Symbols in scope for both pages. SPX is options-chain-only (no
# underlying shares); futures cross-check is scoped to the SPX/ES pair
# only, per the earlier discussion -- QQQ/IWM don't have a comparably
# liquid futures analog worth the added complexity.
TRACKED_SYMBOLS = ["SPY", "QQQ", "SPX", "IWM"]
FUTURES_CROSS_CHECK_SYMBOL = "SPX"  # maps to /ES via SCHWAB_ROOTS
MAX_DTE = 10
TREND_WINDOW_DAYS = 5

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _ensure_tables() -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS oi_intraday_dte_cache (
                symbol       TEXT NOT NULL,
                expiration   TEXT NOT NULL,
                dte          INTEGER,
                type         TEXT NOT NULL,
                strike       REAL NOT NULL,
                prior_oi     INTEGER,
                live_volume  INTEGER,
                vol_oi_ratio REAL,
                significant  INTEGER,
                snapshot_date TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                PRIMARY KEY (symbol, expiration, type, strike)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS oi_positional_dte_cache (
                symbol        TEXT NOT NULL,
                expiration    TEXT NOT NULL,
                dte           INTEGER,
                type          TEXT NOT NULL,
                strike        REAL NOT NULL,
                oi_today      INTEGER,
                oi_change_1d  INTEGER,
                slope_per_day REAL,
                window_days   INTEGER,
                consistent    INTEGER,
                trend_label   TEXT,
                oi_history    TEXT,
                updated_at    TEXT NOT NULL,
                PRIMARY KEY (symbol, expiration, type, strike)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_oidte_intra_sym ON oi_intraday_dte_cache(symbol)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_oidte_pos_sym   ON oi_positional_dte_cache(symbol)")
        # Migration: existing deployments created this table before
        # oi_history existed (CREATE TABLE IF NOT EXISTS is a no-op on
        # an already-existing table, so it wouldn't pick up the new
        # column on its own).
        cols = {r[1] for r in con.execute("PRAGMA table_info(oi_positional_dte_cache)").fetchall()}
        if "oi_history" not in cols:
            con.execute("ALTER TABLE oi_positional_dte_cache ADD COLUMN oi_history TEXT")
        con.commit()
    finally:
        con.close()


def _dte(expiration: str, today: Optional[date] = None) -> int:
    today = today or date.today()
    try:
        ed = date.fromisoformat(str(expiration)[:10])
    except Exception:
        return 9999
    return (ed - today).days


def get_dte_expirations(symbol: str, max_dte: int = MAX_DTE, min_count: int = 10) -> List[str]:
    """Expiries currently inside the 0-N DTE window, anchored by expiry
    date -- this list naturally rolls as today advances, rather than
    being pinned to a fixed set of expiry labels.

    Guarantees at least `min_count` expiries even if the strict
    calendar-day window comes up short -- e.g. SPY only lists M/W/F
    expiries (not a true daily cycle the way SPX does), so a strict
    "0-10 calendar days" cutoff can yield only 4-5 results even though
    there are plenty of further-out expiries available. Extends beyond
    max_dte, in date order, until min_count is reached (or the
    available data runs out) so the PCR strip and expiry dropdown
    always have a full row to show.
    """
    today = date.today()
    exps = _woi.future_expirations(symbol)
    windowed = [e for e in exps if 0 <= _dte(e, today) <= max_dte]
    if len(windowed) >= min_count:
        return windowed
    extended = [e for e in exps if _dte(e, today) >= 0]
    extended.sort(key=lambda e: _dte(e, today))
    return extended[:max(min_count, len(windowed))]


# ---------------------------------------------------------------------
# 1) Intraday buildup: volume-vs-yesterday's-OI proxy
# ---------------------------------------------------------------------

def compute_intraday_buildup(symbols: Optional[Sequence[str]] = None, max_dte: int = MAX_DTE) -> Dict[str, Any]:
    _ensure_tables()
    symbols = list(symbols or TRACKED_SYMBOLS)
    now = datetime.now().isoformat(timespec="seconds")
    today = date.today()
    written = 0
    errors = []

    con = sqlite3.connect(DB_PATH)
    try:
        for sym in symbols:
            try:
                exps = get_dte_expirations(sym, max_dte)
                for exp in exps:
                    rows, snap_date = _woi.latest_option_rows(sym, exp)
                    if not rows:
                        continue
                    dte = _dte(exp, today)
                    for r in rows:
                        prior_oi = int(r.get("prev_oi") or 0)
                        vol = int(r.get("volume") or 0)
                        # Ratio of today's live volume to the OI move it
                        # would take to fully explain that volume as new
                        # opening interest. Near 1.0 = mostly opening
                        # positions; much greater than 1.0 = mostly same-day
                        # churn/closing, i.e. noisier as a positioning signal.
                        oi_change_abs = abs(int(r.get("oi_change") or 0))
                        ratio = (vol / oi_change_abs) if oi_change_abs > 0 else (float("inf") if vol > 0 else 0.0)
                        significant = 1 if (oi_change_abs > 0 and 0.7 <= ratio <= 1.4 and vol >= 250) else 0
                        con.execute("""
                            INSERT OR REPLACE INTO oi_intraday_dte_cache
                                (symbol, expiration, dte, type, strike, prior_oi, live_volume,
                                 vol_oi_ratio, significant, snapshot_date, updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        """, (
                            sym, exp, dte, r.get("type"), float(r.get("strike") or 0),
                            prior_oi, vol,
                            None if math.isinf(ratio) else round(ratio, 3),
                            significant, snap_date or today.isoformat(), now,
                        ))
                        written += 1
            except Exception as e:
                errors.append(f"{sym}: {e}")
        con.commit()
    finally:
        con.close()
    return {"ok": not errors, "rows_written": written, "errors": errors, "updated_at": now}


def read_intraday_cache(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        if symbol:
            rows = con.execute(
                "SELECT * FROM oi_intraday_dte_cache WHERE symbol=? "
                "ORDER BY significant DESC, vol_oi_ratio IS NULL, live_volume DESC",
                (symbol.upper(),)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM oi_intraday_dte_cache "
                "ORDER BY significant DESC, vol_oi_ratio IS NULL, live_volume DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ---------------------------------------------------------------------
# 2) Positional / weekly trend: rolling N-day OI slope per strike
# ---------------------------------------------------------------------

def _linreg_slope(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return (num / den) if den else 0.0


def compute_positional_trend(
    symbols: Optional[Sequence[str]] = None,
    max_dte: int = MAX_DTE,
    window_days: int = TREND_WINDOW_DAYS,
) -> Dict[str, Any]:
    _ensure_tables()
    symbols = list(symbols or TRACKED_SYMBOLS)
    now = datetime.now().isoformat(timespec="seconds")
    today = date.today()
    written = 0
    errors = []

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        for sym in symbols:
            try:
                exps = get_dte_expirations(sym, max_dte)
                for exp in exps:
                    dte = _dte(exp, today)
                    dates = [r["date"] for r in con.execute(
                        "SELECT DISTINCT date FROM options WHERE symbol=? AND expiration=? "
                        "ORDER BY date DESC LIMIT ?",
                        (sym, exp, window_days)
                    ).fetchall()]
                    if len(dates) < 2:
                        continue
                    dates = list(reversed(dates))  # oldest -> newest
                    x_index = {d: i for i, d in enumerate(dates)}

                    rows = con.execute(f"""
                        SELECT date, type, strike, SUM(oi) AS oi
                        FROM options
                        WHERE symbol=? AND expiration=? AND date IN ({",".join("?" for _ in dates)})
                        GROUP BY date, type, strike
                    """, (sym, exp, *dates)).fetchall()

                    series: Dict[tuple, Dict[str, int]] = {}
                    for r in rows:
                        typ = _woi.norm_option_type(r["type"])
                        if typ not in ("call", "put"):
                            continue
                        k = (typ, round(float(r["strike"]), 4))
                        series.setdefault(k, {})[r["date"]] = int(r["oi"] or 0)

                    for (typ, strike), by_date in series.items():
                        pts = [(x_index[d], by_date[d]) for d in dates if d in by_date]
                        if len(pts) < 2:
                            continue
                        xs = [p[0] for p in pts]
                        ys = [float(p[1]) for p in pts]
                        slope = _linreg_slope(xs, ys)
                        oi_today = int(ys[-1])
                        oi_change_1d = int(ys[-1] - ys[-2]) if len(ys) >= 2 else 0
                        diffs = [ys[i] - ys[i - 1] for i in range(1, len(ys))]
                        consistent = 1 if diffs and (all(d >= 0 for d in diffs) or all(d <= 0 for d in diffs)) else 0
                        if oi_today < 500:
                            label = "low_oi"
                        elif consistent and slope > 0:
                            label = "sustained_buildup"
                        elif consistent and slope < 0:
                            label = "sustained_unwind"
                        elif oi_change_1d != 0 and not consistent:
                            label = "one_day_spike"
                        else:
                            label = "flat"
                        # The raw day-by-day values that produced slope/label
                        # above -- stored so the page can show what actually
                        # happened each day, not just the derived numbers.
                        history_json = json.dumps([{"date": dates[int(x)], "oi": int(y)} for x, y in pts])
                        con.execute("""
                            INSERT OR REPLACE INTO oi_positional_dte_cache
                                (symbol, expiration, dte, type, strike, oi_today, oi_change_1d,
                                 slope_per_day, window_days, consistent, trend_label, oi_history, updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (
                            sym, exp, dte, typ, strike, oi_today, oi_change_1d,
                            round(slope, 2), len(pts), consistent, label, history_json, now,
                        ))
                        written += 1
            except Exception as e:
                errors.append(f"{sym}: {e}")
        con.commit()
    finally:
        con.close()
    return {"ok": not errors, "rows_written": written, "errors": errors, "updated_at": now}


def read_positional_cache(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        if symbol:
            rows = con.execute(
                "SELECT * FROM oi_positional_dte_cache WHERE symbol=? "
                "ORDER BY (trend_label='sustained_buildup') DESC, ABS(slope_per_day) DESC",
                (symbol.upper(),)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM oi_positional_dte_cache "
                "ORDER BY (trend_label='sustained_buildup') DESC, ABS(slope_per_day) DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ---------------------------------------------------------------------
# 3) VIX / VIX9D + near-dated SPX skew (regime context panel)
# ---------------------------------------------------------------------

def get_pcr_strip(symbol: str, max_dte: int = MAX_DTE) -> List[Dict[str, Any]]:
    """Volume-based PCR (put volume / call volume) for every expiry
    currently inside the 0-N DTE window, one row per expiry -- feeds
    the PCR box strip at the top of the merged DTE page. Reads from
    the already-computed intraday cache (no live query) so this is
    cheap and always in sync with what the page's volume view shows.
    """
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute("""
            SELECT expiration, dte, type, SUM(live_volume) AS vol
            FROM oi_intraday_dte_cache
            WHERE symbol = ?
            GROUP BY expiration, type
        """, (symbol.upper(),)).fetchall()
    finally:
        con.close()

    by_exp: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        exp = r["expiration"]
        d = by_exp.setdefault(exp, {"expiration": exp, "dte": r["dte"], "call_volume": 0, "put_volume": 0})
        if r["type"] == "call":
            d["call_volume"] = int(r["vol"] or 0)
        elif r["type"] == "put":
            d["put_volume"] = int(r["vol"] or 0)

    out = []
    for d in by_exp.values():
        cv, pv = d["call_volume"], d["put_volume"]
        pcr = round(pv / cv, 3) if cv > 0 else None
        d["pcr"] = pcr
        out.append(d)
    out.sort(key=lambda d: d["dte"])
    return out[:10]


def get_expiry_detail(symbol: str, expiration: str) -> Dict[str, Any]:
    """Everything needed to render one expiry's detail view (both the
    volume chart and the OI-trend chart, so the frontend can switch
    between them without a second round-trip): per-strike live volume
    + prior OI (from the intraday cache) merged with per-strike OI
    slope/trend label (from the positional cache), joined on strike+type.
    """
    _ensure_tables()
    con = _conn()
    try:
        vol_rows = con.execute("""
            SELECT type, strike, prior_oi, live_volume, vol_oi_ratio, significant
            FROM oi_intraday_dte_cache WHERE symbol=? AND expiration=?
        """, (symbol.upper(), expiration)).fetchall()
        trend_rows = con.execute("""
            SELECT type, strike, oi_today, oi_change_1d, slope_per_day, trend_label, oi_history
            FROM oi_positional_dte_cache WHERE symbol=? AND expiration=?
        """, (symbol.upper(), expiration)).fetchall()
    finally:
        con.close()

    trend_by_key = {(r["type"], round(float(r["strike"]), 4)): dict(r) for r in trend_rows}
    merged = []
    for r in vol_rows:
        key = (r["type"], round(float(r["strike"]), 4))
        row = dict(r)
        t = trend_by_key.pop(key, None)
        if t:
            row.update({k: v for k, v in t.items() if k not in ("type", "strike")})
        merged.append(row)
    # Any strikes present only in the trend cache (e.g. no live volume yet today)
    for t in trend_by_key.values():
        merged.append(dict(t))

    merged.sort(key=lambda r: (r.get("type", ""), float(r.get("strike", 0))))
    return {"symbol": symbol.upper(), "expiration": expiration, "rows": merged}


def get_vix_skew_panel() -> Dict[str, Any]:
    out: Dict[str, Any] = {"vix": None, "vix9d": None, "term_structure": None, "spx_skew": None, "error": None}
    try:
        from .yf_session import safe_history
        vix_hist = safe_history("^VIX", period="5d", interval="1d")
        vix9d_hist = safe_history("^VIX9D", period="5d", interval="1d")
        if vix_hist is not None and len(vix_hist):
            out["vix"] = round(float(vix_hist["Close"].iloc[-1]), 2)
        if vix9d_hist is not None and len(vix9d_hist):
            out["vix9d"] = round(float(vix9d_hist["Close"].iloc[-1]), 2)
        if out["vix"] and out["vix9d"]:
            # >1.0 = front-dated fear exceeds 30d (backwardation, near-term
            # stress); <1.0 = normal contango / calmer near-term regime.
            out["term_structure"] = round(out["vix9d"] / out["vix"], 3)
    except Exception as e:
        out["error"] = str(e)

    try:
        exps = get_dte_expirations("SPX", MAX_DTE)
        if exps:
            rows, _ = _woi.latest_option_rows("SPX", exps[0])
            spot = None
            for r in rows:
                if r.get("underlying"):
                    spot = float(r["underlying"])
                    break
            if rows and spot:
                calls = [r for r in rows if r["type"] == "call" and r.get("iv")]
                puts = [r for r in rows if r["type"] == "put" and r.get("iv")]
                # ~5% OTM proxy for 25-delta since full BS delta isn't
                # computed here; good enough for a regime read, not meant
                # to replace the exact 25-delta skew a vol desk would use.
                otm_call = min(calls, key=lambda r: abs(r["strike"] - spot * 1.05), default=None) if calls else None
                otm_put = min(puts, key=lambda r: abs(r["strike"] - spot * 0.95), default=None) if puts else None
                if otm_call and otm_put:
                    out["spx_skew"] = {
                        "expiration": exps[0],
                        "call_strike": otm_call["strike"], "call_iv": round(otm_call["iv"], 4),
                        "put_strike": otm_put["strike"], "put_iv": round(otm_put["iv"], 4),
                        "skew_pts": round((otm_put["iv"] - otm_call["iv"]) * 100, 2),
                    }
    except Exception as e:
        if not out["error"]:
            out["error"] = str(e)
    return out


# ---------------------------------------------------------------------
# 4) ES futures cross-check (current + next 3 expiries)
# ---------------------------------------------------------------------

def get_futures_context(symbol: str = FUTURES_CROSS_CHECK_SYMBOL) -> Dict[str, Any]:
    try:
        from .futures_oi_schwab import get_latest_oi
        data = get_latest_oi(symbol)
        contracts = (data or {}).get("contracts") or []
        contracts_sorted = sorted(contracts, key=lambda c: c.get("expiry") or "9999")[:4]
        return {"symbol": symbol, "contracts": contracts_sorted, "error": (data or {}).get("error")}
    except Exception as e:
        return {"symbol": symbol, "contracts": [], "error": str(e)}


# ---------------------------------------------------------------------
# Combined refresh (used by morning pipeline, interval job, and manual
# "Refresh Now" button on either page -- one code path for all three).
# ---------------------------------------------------------------------

def run_full_refresh(symbols: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    with _lock:
        t0 = time.time()
        intraday = compute_intraday_buildup(symbols)
        positional = compute_positional_trend(symbols)
        return {
            "ok": intraday["ok"] and positional["ok"],
            "intraday": intraday,
            "positional": positional,
            "elapsed_sec": round(time.time() - t0, 2),
        }


def run_intraday_refresh_only(symbols: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    with _lock:
        return compute_intraday_buildup(symbols)


# ---------------------------------------------------------------------
# Scheduler Hub registration -- gives both a manual "Run Now" button and
# (for the intraday job) a configurable interval, without a new UI.
# ---------------------------------------------------------------------

_registered = False
_reg_lock = threading.Lock()


def register_dte_jobs() -> bool:
    global _registered
    with _reg_lock:
        if _registered:
            return False
        _registered = True
    from .job_registry import register_job

    register_job(
        "dte_positional_trend", "0-10 DTE Positional OI Trend (SPY/QQQ/SPX/IWM)",
        "Computes rolling 5-day OI slope per strike for expiries currently inside the "
        "0-10 DTE window, plus VIX/VIX9D + SPX near-dated skew and ES futures OI "
        "cross-check. Runs automatically as part of the 7:30 AM morning pipeline; "
        "Run Now here triggers an ad-hoc recompute (real OI itself won't have changed "
        "intraday, so re-running mid-day mainly re-confirms the same day's trend).",
        kind="interval", default_schedule={"interval_min": 24 * 60},
        group="Manual / One-Time",
        run_now_fn=lambda: compute_positional_trend(),
    )
    register_job(
        "dte_intraday_buildup", "0-10 DTE Intraday Volume-vs-OI Buildup (SPY/QQQ/SPX/IWM)",
        "Computes live volume vs yesterday's closing OI per strike for expiries inside "
        "the 0-10 DTE window -- a same-day proxy for tomorrow's real OI move. Real OI "
        "does not update intraday, so this is a leading indicator, not a live OI feed. "
        "Runs every 20 minutes during market hours by default; Run Now triggers an "
        "immediate recompute.",
        kind="interval", default_schedule={"interval_min": 20},
        group="Manual / One-Time",
        run_now_fn=lambda: compute_intraday_buildup(),
    )
    return True
