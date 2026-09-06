"""
smart_money_distribution_scanner.py — Institutional Distribution Breakdown Scanner

Mirror-image companion to institutional_scanner.py (accumulation/breakout side).
Identifies stocks where smart money appears to be quietly distributing --
falling up/down volume ratio, clustering distribution days, churn/stall days
near highs, RS-line rolling over vs. benchmark while price is still elevated
-- the classic pre-breakdown footprint. Configurable thresholds, same shape
of response as institutional_scanner.py so existing UI/consumers can reuse
patterns.

Deliberately kept as a separate module/blueprint rather than editing
institutional_scanner.py's core scoring, so nothing about the existing
accumulation scan's behavior changes.
"""
import sqlite3, json
from datetime import datetime, date, timedelta
from flask import Blueprint, jsonify, request

dist_bp = Blueprint("dist_bp", __name__, url_prefix="/scanner/distribution")
from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

# ── Default scoring parameters ─────────────────────────────────────────────
DEFAULTS = {
    "min_score":           3.0,   # minimum composite score to show result
    "udvr_lookback":       20,    # up/down volume ratio lookback window
    "udvr_threshold":      0.77,  # below this = distribution-heavy volume
    "dist_lookback":       25,    # distribution-day count window
    "min_dist_days":       4,     # min distribution days in window to flag
    "dist_pct_threshold":  -1.5,  # min % decline to count as a distribution day
    "dist_vol_multiplier": 1.5,   # volume vs 50D avg to count as distribution/churn day
    "stall_lookback":      10,    # churn/stall day window
    "min_churn_days":      2,     # min churn days to count as confirming signal
    "pct_near_high_max":   5.0,   # max % below 20D high to count as "still elevated"
    "min_price":           3.0,   # min stock price (filter penny stocks)
    "rs_symbol":           "SPY", # benchmark for RS-line rollover check
}


def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c


def _get_symbols(watchlist_id=None):
    con = _conn()
    if watchlist_id:
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (watchlist_id,)
        ).fetchall()
        syms = [r[0] for r in rows]
    else:
        syms = [r[0] for r in con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()]
    con.close()
    return syms or []


def _safe(v):
    """Replace NaN/Inf with None for JSON safety."""
    import math
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _get_price_history(symbol, min_days=400):
    """Reads OHLCV directly from price_cache instead of live-fetching from
    yfinance. Same rationale/behavior as institutional_scanner.py's copy
    of this function -- price_cache already holds 3+ years of daily
    history, so there's no reason to re-fetch live on every scan."""
    con = _conn()
    try:
        rows = con.execute(
            """SELECT date, open, high, low, close, volume
               FROM price_cache WHERE symbol=? ORDER BY date DESC LIMIT ?""",
            (symbol.upper().strip(), min_days),
        ).fetchall()
    finally:
        con.close()
    if not rows or len(rows) < 60:
        return None
    rows = list(reversed(rows))
    import numpy as np
    return {
        "close": np.array([r["close"] for r in rows], dtype=float),
        "high": np.array([r["high"] for r in rows], dtype=float),
        "low": np.array([r["low"] for r in rows], dtype=float),
        "volume": np.array([r["volume"] for r in rows], dtype=float),
        "date": [r["date"] for r in rows],
    }


_bench_cache = {}


def _get_bench_closes(rs_symbol, period="1y"):
    """Cached per-process for the duration of one scan batch -- avoids
    re-querying the benchmark once per symbol. Reads from price_cache
    (same rationale as _get_price_history above) rather than yfinance."""
    key = (rs_symbol, period)
    if key in _bench_cache:
        return _bench_cache[key]
    hist = _get_price_history(rs_symbol, min_days=400)
    closes = hist["close"] if hist is not None else None
    _bench_cache[key] = closes
    return closes


def _scan_symbol(sym, p=None):
    if p is None: p = DEFAULTS
    try:
        import pandas as pd, numpy as np

        hist = _get_price_history(sym, min_days=400)
        if hist is None:
            return None

        closes = hist["close"]
        highs = hist["high"]
        lows = hist["low"]
        vols = hist["volume"]
        n = len(closes)
        price = float(closes[-1])

        if price < float(p.get("min_price", 3)):
            return None

        s = pd.Series(closes)
        v = pd.Series(vols)

        # ── Up/Down Volume Ratio ──────────────────────────────────────────
        udvr_lb = int(p.get("udvr_lookback", 20))
        up_day = s > s.shift(1)
        down_day = s < s.shift(1)
        up_vol = v.where(up_day, 0)
        down_vol = v.where(down_day, 0)
        sum_up = up_vol.rolling(udvr_lb).sum()
        sum_down = down_vol.rolling(udvr_lb).sum()
        udvr_series = sum_up / (sum_down + 1)
        udvr = float(udvr_series.iloc[-1])

        # ── Distribution day count ─────────────────────────────────────────
        dist_lb = int(p.get("dist_lookback", 25))
        pct_thresh = float(p.get("dist_pct_threshold", -1.5))
        vol_mult = float(p.get("dist_vol_multiplier", 1.5))
        pct_chg = s.pct_change() * 100
        avg_vol50 = v.rolling(50).mean()
        is_dist_day = (pct_chg <= pct_thresh) & (v > avg_vol50 * vol_mult)
        dist_day_count = int(is_dist_day.rolling(dist_lb).sum().iloc[-1])

        # ── Churn/stall day: wide range, weak close, heavy volume ──────────
        stall_lb = int(p.get("stall_lookback", 10))
        day_range = pd.Series(highs - lows)
        avg_range20 = day_range.rolling(20).mean()
        close_position = (s - pd.Series(lows)) / (day_range + 1e-4)
        is_churn_day = (
            (day_range > avg_range20 * 1.2)
            & (close_position < 0.35)
            & (v > avg_vol50 * vol_mult)
        )
        churn_count = int(is_churn_day.rolling(stall_lb).sum().iloc[-1])

        # ── RS line rollover vs benchmark (leading divergence) ──────────────
        rs_symbol = str(p.get("rs_symbol", "SPY"))
        rs_rolling_over = False
        rs_line_last = None
        if rs_symbol and rs_symbol.upper() != sym.upper():
            bench_closes = _get_bench_closes(rs_symbol)
            if bench_closes is not None and len(bench_closes) >= n:
                bench_s = pd.Series(bench_closes[-n:])
                rs_line = s / bench_s.replace(0, np.nan)
                rs_high20 = rs_line.rolling(20).max()
                price_high20 = s.rolling(20).max()
                rs_rolling_over = bool(
                    (rs_line.iloc[-1] < rs_high20.iloc[-1] * 0.97)
                    and (s.iloc[-1] >= price_high20.iloc[-1] * 0.97)
                )
                rs_line_last = float(rs_line.iloc[-1]) if pd.notna(rs_line.iloc[-1]) else None

        # ── Trend weakening ──────────────────────────────────────────────
        ma50 = s.rolling(50).mean()
        trend_weakening = bool(price < ma50.iloc[-1] or ma50.iloc[-1] < ma50.iloc[-6])

        # ── "Still elevated" check (price hasn't broken down yet -- this is
        # the pre-breakdown window, not confirmation after the fact) ───────
        high20 = float(np.max(highs[-20:]))
        pct_below_20d_high = round((high20 - price) / high20 * 100, 2)
        still_elevated = pct_below_20d_high <= float(p.get("pct_near_high_max", 5.0))

        udvr_threshold = float(p.get("udvr_threshold", 0.77))
        min_dist_days = int(p.get("min_dist_days", 4))
        min_churn_days = int(p.get("min_churn_days", 2))

        # Hard filter: need at least the volume-asymmetry signal present
        if udvr >= udvr_threshold and dist_day_count < min_dist_days:
            return None

        # ── Scoring (0-10, mirrors institutional_scanner.py's scale) ───────
        score = 0.0

        # 1. Volume asymmetry (0-2.5)
        if udvr < 0.5:            score += 2.5
        elif udvr < 0.65:         score += 2.0
        elif udvr < udvr_threshold: score += 1.3
        else:                     score += 0.3

        # 2. Distribution day density (0-2.5)
        if dist_day_count >= 6:   score += 2.5
        elif dist_day_count >= min_dist_days: score += 1.8
        elif dist_day_count >= 2: score += 0.8

        # 3. Churn/stall confirmation (0-2)
        if churn_count >= 3:      score += 2.0
        elif churn_count >= min_churn_days: score += 1.2
        elif churn_count >= 1:    score += 0.5

        # 4. RS-line leading divergence (0-2)
        if rs_rolling_over:       score += 2.0

        # 5. Still elevated bonus -- catches it BEFORE the breakdown, not after (0-1)
        if still_elevated:        score += 1.0

        # Trend weakening confirmation
        if trend_weakening:       score += 0.5

        score = round(min(10, score), 1)

        if score < float(p.get("min_score", 3.0)):
            return None

        signal = "🔻 Strong" if score >= 7 else "⚠️ Moderate" if score >= 5 else "👀 Watch"

        breakdown_type = (
            "Distribution Top" if dist_day_count >= 6 and still_elevated else
            "RS Divergence" if rs_rolling_over else
            "Churn/Stall" if churn_count >= min_churn_days else
            "Volume Weakening" if udvr < udvr_threshold else
            "Watching"
        )

        return {
            "symbol": sym, "price": _safe(round(price, 2)), "score": score, "signal": signal,
            "breakdown_type": breakdown_type,
            "udvr": _safe(round(udvr, 2)), "udvr_threshold": udvr_threshold,
            "dist_day_count": dist_day_count, "min_dist_days": min_dist_days,
            "churn_count": churn_count,
            "rs_rolling_over": rs_rolling_over, "rs_line": _safe(round(rs_line_last, 4)) if rs_line_last else None,
            "trend_weakening": trend_weakening,
            "pct_below_20d_high": _safe(pct_below_20d_high), "still_elevated": still_elevated,
        }
    except Exception as e:
        return {"symbol": sym, "error": str(e)[:80], "score": -1}


@dist_bp.route("/scan", methods=["GET", "POST"])
def distribution_scan():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    wl_id = request.args.get("watchlist_id", None, type=int)

    params = dict(DEFAULTS)
    body = request.get_json(silent=True) or {}
    for k in DEFAULTS:
        if k in body:
            params[k] = type(DEFAULTS[k])(body[k])
        elif request.args.get(k) is not None:
            try: params[k] = type(DEFAULTS[k])(request.args.get(k))
            except: pass

    symbols = _get_symbols(wl_id)
    if not symbols:
        return jsonify({"results": [], "count": 0, "error": "No symbols in watchlist"})

    wl_name = "All Symbols"
    if wl_id:
        try:
            con = _conn()
            row = con.execute("SELECT name FROM watchlists WHERE id=?", (wl_id,)).fetchone()
            con.close()
            if row: wl_name = row[0]
        except: pass

    # Warm the benchmark cache once up front so the thread pool below
    # doesn't fire off N redundant SPY fetches.
    _get_bench_closes(str(params.get("rs_symbol", "SPY")))

    results = []; errors = []
    ex = ThreadPoolExecutor(max_workers=6)
    try:
        from ..services.bounded_wait import bounded_as_completed
        futures = {ex.submit(_scan_symbol, sym, params): sym for sym in symbols}
        for fut, sym in bounded_as_completed(futures, timeout=60,
                on_timeout=lambda ks: print(f"[smart_money_distribution_scanner] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                errors.append(f"{sym}: timed out")
                continue
            try:
                r = fut.result()
                if r:
                    if r.get("score", 0) < 0:
                        errors.append(r.get("symbol", "?") + ": " + r.get("error", "err"))
                    else:
                        r["watchlist"] = wl_name
                        results.append(r)
            except Exception as e:
                errors.append(str(e)[:40])
    finally:
        ex.shutdown(wait=False)

    results.sort(key=lambda x: x["score"], reverse=True)
    completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    import math as _math
    def _clean(v):
        if isinstance(v, float) and (_math.isnan(v) or _math.isinf(v)):
            return None
        return v
    def _clean_row(row):
        return {k: _clean(v) for k, v in row.items()}
    results = [_clean_row(r) for r in results]

    try:
        con = _conn()
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES ('distribution_scan',?,?)",
                    (json.dumps(results), completed_at))
        con.commit(); con.close()
    except: pass

    # Optional: persist hits into smart_money_scan_history for later
    # hit-rate/backtesting. Best-effort -- never breaks the scan response.
    try:
        from .. import db as _oiapp_db
        for r in results:
            _oiapp_db.save_smart_money_scan_result(
                symbol=r.get("symbol"), mode="distribution", score=r.get("score"),
                signal=r.get("signal"), udvr=r.get("udvr"), dist_day_count=r.get("dist_day_count"),
                extra=r,
            )
    except Exception as _persist_exc:
        print(f"[smart_money_distribution_scanner] history persist skipped: {_persist_exc}")

    return jsonify({"results": results, "count": len(results),
                    "total_scanned": len(symbols), "completed_at": completed_at,
                    "watchlist": wl_name,
                    "params_used": params, "errors": len(errors), "error_sample": errors[:3]})


@dist_bp.route("/scan_cached")
def distribution_scan_cached():
    try:
        con = _conn()
        row = con.execute("SELECT value, updated FROM app_cache WHERE key='distribution_scan'").fetchone()
        con.close()
        if row:
            data = json.loads(row[0])
            return jsonify({"results": data, "count": len(data), "completed_at": row[1], "from_cache": True})
        return jsonify({"results": [], "count": 0, "from_cache": True})
    except:
        return jsonify({"results": [], "count": 0})


@dist_bp.route("/defaults")
def get_defaults():
    return jsonify(DEFAULTS)
