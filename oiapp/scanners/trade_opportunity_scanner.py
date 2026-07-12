# oiapp/scanners/trade_opportunity_scanner.py
"""
Trade Opportunity Scanner  (v5.2)
──────────────────────────────────
Scans every symbol in a watchlist and surfaces ranked trade opportunities
using the same multi-factor Entry Quality Score used by the journal.

For each symbol it:
  1. Checks earnings guard   (skip if < min_earn_days, default 14)
  2. Selects best expiry     (7–30 DTE from live yfinance chain)
  3. Computes Entry Score    (Regime + RS + IV + PCR + Walls + Gamma Flip)
  4. Builds exact trade legs (PS / CS / IC / PB / CB with strikes + premium est)
  5. Returns full rationale  (why, entry, exit, PNR, risk, manage)

Sorted by Entry Score descending.  Grade A ≥80, B ≥65, C ≥50, D ≥35, F <35.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, render_template, request

trade_opp_bp = Blueprint("trade_opp", __name__, url_prefix="/trade-scanner")

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")
MIN_EARN_DAYS = 14   # hard floor — never scan inside earnings window
DTE_MIN = 7
DTE_MAX = 30
MAX_WORKERS = 6

# yfinance calls have no built-in timeout, and Yahoo occasionally stalls or
# rate-limits without erroring — without a hard ceiling here, a handful of
# stuck symbols can block the whole thread pool indefinitely (this is what
# was causing multi-minute/never-finishing scans). This wraps any blocking
# call with a wall-clock deadline; if it's exceeded we give up and treat the
# symbol like any other fetch failure (skip it) rather than hang forever.
# Uses a thread (not signal.alarm) so it works on Windows too.
_NETWORK_CALL_TIMEOUT_SEC = 12
_network_timeout_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS * 2, thread_name_prefix="tos-net")


def _with_timeout(fn, *args, timeout: float = _NETWORK_CALL_TIMEOUT_SEC, default=None, **kwargs):
    fut = _network_timeout_pool.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=timeout)
    except Exception:
        # Either it timed out, or the call itself raised — either way this
        # symbol just doesn't get data this round. The orphaned call (if it
        # was a real timeout, not an error) keeps running in the background
        # thread pool and is discarded when it eventually finishes.
        return default


# SPY's own history was previously being re-fetched from Yahoo separately
# for EVERY symbol scanned (100 symbols = 100 redundant identical SPY
# requests) — wasteful, and very likely to be exactly what triggers Yahoo's
# rate-limiting mid-scan. Cache it briefly instead.
_spy_history_cache: Dict[str, Any] = {"df": None, "fetched_at": 0.0}
_spy_history_lock = threading.Lock()
_SPY_CACHE_TTL_SEC = 600  # 10 minutes — plenty fresh for daily-bar RS calc


def _get_spy_history_cached():
    import time as _time
    now = _time.time()
    with _spy_history_lock:
        if _spy_history_cache["df"] is not None and (now - _spy_history_cache["fetched_at"]) < _SPY_CACHE_TTL_SEC:
            return _spy_history_cache["df"]
    import yfinance as yf
    df = _with_timeout(lambda: yf.Ticker("SPY").history(period="3mo"), default=None)
    with _spy_history_lock:
        if df is not None and not df.empty:
            _spy_history_cache["df"] = df
            _spy_history_cache["fetched_at"] = now
        return _spy_history_cache["df"]


# ── DB / helpers ───────────────────────────────────────────────────────────

def _conn():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c



# ── Scan cache (persist last scan per watchlist) ───────────────────────────

def _ensure_scan_cache_table():
    """Create trade_scan_cache table if it doesn't exist."""
    try:
        con = _conn()
        con.execute("""
            CREATE TABLE IF NOT EXISTS trade_scan_cache (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id    TEXT NOT NULL,
                scanned_at      TEXT NOT NULL,
                params_json     TEXT,
                results_json    TEXT,
                summary_json    TEXT,
                filtered_json   TEXT,
                symbol_count    INTEGER DEFAULT 0,
                result_count    INTEGER DEFAULT 0,
                UNIQUE(watchlist_id)
            )
        """)
        con.commit()
        con.close()
    except Exception as e:
        print(f"[scan_cache] ensure table: {e}")


def _save_scan_cache(watchlist_id, scanned_at, params, opportunities, summary, filtered):
    """Persist scan results to DB (upsert by watchlist_id)."""
    try:
        _ensure_scan_cache_table()
        con = _conn()
        con.execute("""
            INSERT OR REPLACE INTO trade_scan_cache
            (watchlist_id, scanned_at, params_json, results_json, summary_json, filtered_json,
             symbol_count, result_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(watchlist_id or "all"),
            scanned_at,
            json.dumps(params),
            json.dumps(opportunities[:100]),
            json.dumps(summary),
            json.dumps(filtered[:30]),
            summary.get("scanned_count", len(opportunities)),
            len(opportunities),
        ))
        con.commit()
        con.close()
    except Exception as e:
        print(f"[scan_cache] save: {e}")


def _load_scan_cache(watchlist_id) -> Optional[Dict]:
    """Load last cached scan for watchlist. Returns None if not found."""
    try:
        _ensure_scan_cache_table()
        con = _conn()
        row = con.execute(
            "SELECT * FROM trade_scan_cache WHERE watchlist_id=?",
            (str(watchlist_id or "all"),)
        ).fetchone()
        con.close()
        if not row:
            return None
        return {
            "watchlist_id":  row["watchlist_id"],
            "scanned_at":    row["scanned_at"],
            "result_count":  row["result_count"],
            "symbol_count":  row["symbol_count"],
            "params":        json.loads(row["params_json"] or "{}"),
            "opportunities": json.loads(row["results_json"] or "[]"),
            "summary":       json.loads(row["summary_json"] or "{}"),
            "filtered":      json.loads(row["filtered_json"] or "[]"),
        }
    except Exception as e:
        print(f"[scan_cache] load: {e}")
        return None


def _safe(v, dec=2):
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, dec)
    except Exception:
        return None


def _watchlist_symbols(watchlist_id=None) -> List[str]:
    try:
        con = _conn()
        if watchlist_id:
            rows = con.execute(
                "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                (int(watchlist_id),),
            ).fetchall()
        else:
            rows = con.execute("SELECT DISTINCT symbol FROM symbols ORDER BY symbol").fetchall()
        con.close()
        return [r[0].upper() for r in rows if r[0]]
    except Exception:
        return []


def _watchlists() -> List[Dict]:
    try:
        con = _conn()
        rows = con.execute(
            "SELECT w.id, w.name, COUNT(ws.id) AS cnt "
            "FROM watchlists w LEFT JOIN watchlist_symbols ws ON ws.watchlist_id=w.id "
            "GROUP BY w.id ORDER BY w.name"
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── Data collectors (lightweight — reuse journal_snapshot logic) ───────────

def _batch_fetch_histories(symbols: List[str], period: str = "1y") -> Dict[str, Any]:
    """Fetch daily history for ALL given symbols in one (or a few chunked)
    yfinance calls instead of one HTTP request per symbol. This is the
    single biggest lever on total scan time — N individual requests each
    pay their own connection/auth overhead, and firing that many small
    requests in a burst is also a plausible trigger for Yahoo's rate
    limiting in the first place. Best-effort: any symbol that isn't in the
    result (chunk failed, timed out, or wasn't returned) just falls back
    to its own individual fetch inside _get_ta/_get_rs_vs_spy, so this can
    only speed things up, never break a scan that would've worked before.
    """
    out: Dict[str, Any] = {}
    symbols = [s for s in dict.fromkeys(s.upper() for s in symbols if s)]
    if not symbols:
        return out
    try:
        import yfinance as yf
    except Exception:
        return out

    CHUNK = 50  # keep each batched request a reasonable size
    chunks = [symbols[i:i + CHUNK] for i in range(0, len(symbols), CHUNK)]
    for chunk in chunks:
        def _dl(syms=chunk):
            return yf.download(
                tickers=" ".join(syms), period=period, group_by="ticker",
                threads=True, progress=False, auto_adjust=False,
            )
        data = _with_timeout(_dl, timeout=60, default=None)
        if data is None or getattr(data, "empty", True):
            continue
        for sym in chunk:
            try:
                if len(chunk) == 1 or sym not in getattr(data.columns, "levels", [[]])[0]:
                    # yf.download with a single ticker (or a chunk that
                    # collapsed to one column set) doesn't build a
                    # MultiIndex — treat the whole frame as that symbol's.
                    df = data if len(chunk) == 1 else None
                else:
                    df = data[sym]
                if df is not None and not df.empty:
                    cleaned = df.dropna(how="all")
                    if not cleaned.empty:
                        out[sym] = cleaned
            except Exception:
                continue
    return out


def _get_ta(symbol: str, prefetched_df=None):
    """
    Enhanced TA fetcher — extends _compute_ta with:
      - RSI14 (explicit, not just the diff)
      - MACD histogram + signal line cross
      - EMA gap %  (ema20 - ema50) / spot × 100
      - Market state: REVERSAL_SETUP | TRENDING | RANGE

    prefetched_df: if the caller already batch-fetched this symbol's daily
    history (see _batch_fetch_histories), reuse it instead of making our own
    network call — this is what turns "one HTTP request per symbol" into
    "one request for the whole watchlist."
    """
    try:
        import math as _m

        if prefetched_df is not None and not prefetched_df.empty:
            df = prefetched_df
        else:
            import yfinance as yf
            df = _with_timeout(lambda: yf.Ticker(symbol).history(period="1y"), default=None)
        if df is None or df.empty or len(df) < 35:
            return None
        C = [float(c) for c in df["Close"].tolist() if c and _m.isfinite(c)]
        H = [float(h) for h in df["High"].tolist() if h and _m.isfinite(h)]
        L = [float(l) for l in df["Low"].tolist() if l and _m.isfinite(l)]
        if len(C) < 35:
            return None
        n = len(C) - 1

        def _ema_s(series, p):
            k = 2 / (p + 1)
            out = list(series)
            for i in range(1, len(out)):
                out[i] = series[i] * k + out[i - 1] * (1 - k)
            return out

        def _rsi_s(closes, p=14):
            out = [50.0] * len(closes)
            if len(closes) < p + 1:
                return out
            gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
            ag = sum(gains[:p]) / p
            al = sum(losses[:p]) / p
            for i in range(p, len(closes)):
                if i > p:
                    ag = (ag * (p - 1) + gains[i-1]) / p
                    al = (al * (p - 1) + losses[i-1]) / p
                out[i] = (100 - 100 / (1 + ag / al)) if al > 0 else 100
            return out

        # Core indicators
        rsi14_s  = _rsi_s(C, 14)
        ema90_rsi= _ema_s(rsi14_s, 90)
        ema20_s  = _ema_s(C, 20)
        ema50_s  = _ema_s(C, 50)
        ema200_s = _ema_s(C, 200)

        rn       = rsi14_s[n]
        en       = ema90_rsi[n]
        rsi_diff = rn - en

        # Prefer the cache for these three specific scalars when available
        # and trusted: this function's own local RSI/EMA computation above
        # uses only a 1-year window (~250 bars) with no convergence check
        # at all -- directly measured elsewhere this session that
        # EMA(RSI,90) needs ~540 bars before it's not still distorted by
        # under-convergence. The cache (backed by a 3-year window, same
        # threshold already validated) is strictly more accurate when
        # present, and reading it also skips redundant recomputation this
        # symbol may have already had done today by regime_scanner or the
        # technical_snapshot watcher. Everything else in this function
        # (EMA20/50 arrays, MACD, ATR, IVR, trend/state classification)
        # stays locally computed -- those are either fast-converging
        # enough not to need this, or entangled with array-based slope
        # calculations that a single scalar override can't safely replace.
        try:
            from ..services.technical_snapshot import get_or_compute_technical_snapshot
            cached = get_or_compute_technical_snapshot(symbol, "1d")
            if cached and cached.get("rsidiff90_trusted") and cached.get("rsi14") is not None:
                rn = cached["rsi14"]
                en = cached.get("ema_rsi14_90", en)
                rsi_diff = cached.get("rsidiff90", rsi_diff)
        except Exception:
            pass  # fall through to the locally-computed values above

        # MACD (12/26/9)
        ema12 = _ema_s(C, 12)
        ema26 = _ema_s(C, 26)
        macd_line = [ema12[i] - ema26[i] for i in range(len(C))]
        signal_line = _ema_s(macd_line, 9)
        macd_hist_v = macd_line[n] - signal_line[n]
        macd_hist_prev = macd_line[max(0,n-1)] - signal_line[max(0,n-1)]
        macd_cross_bull = macd_hist_prev < 0 <= macd_hist_v  # histogram just turned positive
        macd_cross_bear = macd_hist_prev > 0 >= macd_hist_v  # histogram just turned negative
        macd_above_zero = macd_line[n] > 0
        macd_hist_rising = macd_hist_v > macd_hist_prev

        # EMA gap (as % of price)
        ema_gap_pct = round((ema20_s[n] - ema50_s[n]) / max(C[n], 1) * 100, 2)

        # Trend label
        sl20 = (ema20_s[n] - ema20_s[max(0, n-5)]) / 5
        sl50 = (ema50_s[n] - ema50_s[max(0, n-10)]) / 10
        if ema20_s[n] > ema50_s[n] and sl20 > 0 and sl50 > 0:    trend = "UPTREND"
        elif ema20_s[n] < ema50_s[n] and sl20 < 0 and sl50 < 0:  trend = "DOWNTREND"
        elif abs(sl20) < 0.05 * C[n] / 100:                       trend = "SIDEWAYS"
        elif ema20_s[n] > ema50_s[n]:                             trend = "MILD UP"
        else:                                                       trend = "MILD DOWN"

        mom = ("OVERBOUGHT" if rsi_diff >= 20 else
               "OVERSOLD"   if rsi_diff <= -20 else
               "ELEVATED"   if rsi_diff >= 10 else
               "DEPRESSED"  if rsi_diff <= -10 else "NEUTRAL")

        # ── Market state: REVERSAL_SETUP | TRENDING | RANGE ─────────────
        # Reversal setup requires:  exhaustion signal + nascent turn
        reversal_bull = (
            rn < 35 and                      # oversold
            rsi_diff < -5 and               # below 90-EMA (depressed)
            (macd_cross_bull or macd_hist_rising) and  # MACD turning up
            C[n] > ema20_s[n] * 0.97        # price not in freefall
        )
        reversal_bear = (
            rn > 65 and                      # overbought
            rsi_diff > 5 and                # above 90-EMA (elevated)
            (macd_cross_bear or not macd_hist_rising) and
            C[n] < ema20_s[n] * 1.03
        )
        trending = (
            abs(rsi_diff) > 8 and
            abs(ema_gap_pct) > 0.5 and
            abs(macd_hist_v) > 0
        )

        if reversal_bull:   market_state = "REVERSAL_BULL"
        elif reversal_bear: market_state = "REVERSAL_BEAR"
        elif trending:      market_state = "TRENDING"
        else:               market_state = "RANGE"

        # ATR
        tr = [max(H[i]-L[i], abs(H[i]-C[i-1]), abs(L[i]-C[i-1])) for i in range(1, len(C))]
        atr = _ema_s(tr, 14)[-1]

        # IV rank proxy (30d vs 90d HV)
        rets30 = [_m.log(C[i]/C[i-1]) for i in range(n-29, n+1) if C[i-1] > 0]
        rets90 = [_m.log(C[i]/C[i-1]) for i in range(n-89, n+1) if C[i-1] > 0] if n >= 90 else rets30
        rv30   = _m.sqrt(sum(x**2 for x in rets30)/len(rets30)*252)*100 if rets30 else 20
        rv90   = _m.sqrt(sum(x**2 for x in rets90)/len(rets90)*252)*100 if rets90 else rv30
        ivr    = min(100, max(0, round((rv30/rv90) * 50))) if rv90 else 40

        return {
            "price":      round(C[n], 2),
            "prev_close": round(C[n-1], 2),
            "chg_pct":    round((C[n]-C[n-1])/C[n-1]*100, 2) if C[n-1] else 0,
            "rsi":        round(rn, 1),
            "rsi14":      round(rn, 1),
            "ema90_rsi":  round(en, 1),
            "rsi_ema_diff": round(rsi_diff, 1),
            "ema20":      round(ema20_s[n], 2),
            "ema50":      round(ema50_s[n], 2),
            "ema_gap_pct": ema_gap_pct,         # (ema20-ema50)/price ×100
            "macd_hist":  round(macd_hist_v, 4),
            "macd_line":  round(macd_line[n], 4),
            "macd_signal":round(signal_line[n], 4),
            "macd_cross_bull": macd_cross_bull,
            "macd_cross_bear": macd_cross_bear,
            "macd_above_zero": macd_above_zero,
            "macd_hist_rising": macd_hist_rising,
            "atr":        round(atr, 2),
            "iv_est":     round(rv30, 1),
            "iv_rank":    ivr,
            "trend":      trend,
            "momentum":   mom,
            "market_state": market_state,
            "reversal_bull": reversal_bull,
            "reversal_bear": reversal_bear,
            "high52":     round(max(H), 2),
            "low52":      round(min(L), 2),
        }
    except Exception as e:
        print(f"[get_ta] {symbol}: {e}")
        return None


def _get_earn_days(symbol: str) -> int:
    try:
        from .earnings_calendar import get_earnings_info
        info = get_earnings_info(symbol) or {}
        days = info.get("earn_days")
        return int(days) if days is not None else 999
    except Exception:
        return 999


def _get_rs_vs_spy(symbol: str, prefetched_df=None) -> Optional[float]:
    try:
        import yfinance as yf
        if prefetched_df is not None and not prefetched_df.empty:
            sym_df = prefetched_df.tail(63)  # ~3 trading months out of the already-fetched 1y history
        else:
            sym_df = _with_timeout(lambda: yf.Ticker(symbol).history(period="3mo"), default=None)
        spy_df = _get_spy_history_cached()
        if sym_df is None or spy_df is None or sym_df.empty or spy_df.empty or len(sym_df) < 20:
            return None
        sr = float(sym_df["Close"].iloc[-1]) / float(sym_df["Close"].iloc[-20]) - 1
        br = float(spy_df["Close"].iloc[-1]) / float(spy_df["Close"].iloc[-20]) - 1
        return round((sr - br) * 100, 2)
    except Exception:
        return None


def _get_iv_rank(symbol: str, ta=None) -> Optional[float]:
    if ta:
        return _safe(ta.get("iv_rank"))
    try:
        from .spy_strategies import _compute_ta
        t = _compute_ta(symbol)
        return _safe(t.get("iv_rank")) if t else None
    except Exception:
        return None


def _get_regime(symbol: str) -> Dict:
    try:
        con = _conn()
        row = con.execute(
            "SELECT bias, confidence, regime, rsi_trend, weekly_trend, confluence FROM regime_scan "
            "WHERE symbol=? ORDER BY scan_date DESC LIMIT 1",
            (symbol.upper(),),
        ).fetchone()
        con.close()
        if row:
            return {
                "bias": (row[0] or "").lower(), "confidence": float(row[1] or 50), "regime": row[2] or "",
                "rsi_trend": row[3] or "", "weekly_trend": row[4] or "", "confluence": row[5] or "UNKNOWN",
            }
    except Exception:
        pass
    return {}


def _get_market_regime() -> str:
    """SPY regime label for header display."""
    r = _get_regime("SPY")
    return r.get("regime") or r.get("bias") or "Unknown"


def _get_oi_chart_data(symbol: str, expiry: str, spot: Optional[float], max_strikes: int = 14) -> Dict:
    """Per-strike call/put OI for ONE specific expiry, for the small OI
    chart on each scanner tile. Reads the same cached local options
    snapshot table oi_wall_map.py already uses for the put/call walls —
    no live network fetch, so this doesn't add scan latency, only a
    couple of cheap indexed SQLite reads per already-filtered result."""
    if not symbol or not expiry:
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT MAX(date) FROM options WHERE symbol=? AND expiration=?", (symbol, expiry))
        row = c.fetchone()
        last_date = row[0] if row else None
        if not last_date:
            conn.close()
            return {}
        c.execute(
            """SELECT type, strike, SUM(oi) FROM options
               WHERE symbol=? AND expiration=? AND date=?
               GROUP BY type, strike ORDER BY strike""",
            (symbol, expiry, last_date),
        )
        rows = c.fetchall()
        conn.close()
        if not rows:
            return {}
        by_strike: Dict[float, Dict[str, Any]] = {}
        for typ, strike, oi in rows:
            sf = float(strike)
            entry = by_strike.setdefault(sf, {"strike": sf, "call_oi": 0, "put_oi": 0})
            if str(typ).upper().startswith("P"):
                entry["put_oi"] = int(oi or 0)
            else:
                entry["call_oi"] = int(oi or 0)
        nearest = sorted(by_strike.values(), key=lambda x: abs(x["strike"] - (spot or 0)))[:max_strikes]
        nearest.sort(key=lambda x: x["strike"])
        return {
            "asof": last_date,
            "strikes": [n["strike"] for n in nearest],
            "call_oi": [n["call_oi"] for n in nearest],
            "put_oi": [n["put_oi"] for n in nearest],
        }
    except Exception:
        return {}


def _get_pcr_and_walls(symbol: str, spot: float) -> Dict:
    result = {"pcr": None, "call_wall": None, "put_wall": None, "gamma_flip": None,
              "max_pain": None, "top_call_walls": [], "top_put_walls": []}
    try:
        from ..services.aggregate import get_pcr_snapshot
        pcr_snaps = get_pcr_snapshot(symbol)
        if pcr_snaps:
            tc = sum(s.get("calls", 0) for s in pcr_snaps)
            tp = sum(s.get("puts", 0) for s in pcr_snaps)
            result["pcr"] = round(tp / tc, 3) if tc else None

        from .oi_wall_map import get_oi_wall_map
        walls = get_oi_wall_map(symbol, top_n=5, max_dte=60)
        if "error" not in walls:
            pw = walls.get("put_walls", [])
            cw = walls.get("call_walls", [])
            if pw:
                result["put_wall"] = pw[-1]["strike"]
                result["top_put_walls"] = [w["strike"] for w in pw[:3]]
            if cw:
                result["call_wall"] = cw[0]["strike"]
                result["top_call_walls"] = [w["strike"] for w in cw[:3]]
    except Exception:
        pass

    # GEX / gamma flip fallback
    try:
        from .spy_strategies import _oi_rows, _compute_gex
        from ..services.aggregate import _future_exps
        exps = _future_exps(symbol)
        if exps:
            exp = exps[0]
            dte = max(1, (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days)
            rows = _oi_rows(symbol, exp)
            if rows:
                ta_tmp = _get_ta(symbol) or {}
                iv = float(ta_tmp.get("iv_est") or 20.0)
                gex = _compute_gex(rows, spot, dte, iv)
                result["gamma_flip"] = gex.get("gamma_flip")
                result["max_pain"] = gex.get("max_pain")
                if not result["put_wall"]:
                    result["put_wall"] = gex.get("support")
                    result["call_wall"] = gex.get("resistance")
                    result["top_put_walls"] = [w[0] for w in gex.get("top_put_walls", [])[:3]]
                    result["top_call_walls"] = [w[0] for w in gex.get("top_call_walls", [])[:3]]
    except Exception:
        pass

    return result


# ── Expiry selection ───────────────────────────────────────────────────────

def _pick_best_expiry(symbol: str, dte_min: int = DTE_MIN, dte_max: int = DTE_MAX) -> Tuple[Optional[str], int]:
    """
    Find the best expiry in [dte_min, dte_max] window:
      - Prefer Friday (standard weekly expiry) for theta decay rhythm
      - Prefer mid-range DTE (14-21) for optimal theta/gamma balance
      - Fall back to nearest available if none in window
    """
    try:
        import yfinance as yf
        opts = _with_timeout(lambda: yf.Ticker(symbol).options, default=None)
        if not opts:
            return None, 0
        today = date.today()
        candidates = []
        for exp_str in opts:
            try:
                exp_d = datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = (exp_d - today).days
                if dte_min <= dte <= dte_max:
                    # Prefer Fridays (weekday 4) and mid-range DTE
                    is_friday = exp_d.weekday() == 4
                    score = (10 if is_friday else 0) + (10 if 14 <= dte <= 21 else 5 if 10 <= dte <= 25 else 0)
                    candidates.append((exp_str, dte, score))
            except Exception:
                continue

        if not candidates:
            # Widen window: nearest expiry with dte > dte_min
            for exp_str in opts:
                try:
                    exp_d = datetime.strptime(exp_str, "%Y-%m-%d").date()
                    dte = (exp_d - today).days
                    if dte > dte_min:
                        return exp_str, dte
                except Exception:
                    continue
            return None, 0

        # Sort by score desc, then by distance from 17 DTE (sweet spot)
        candidates.sort(key=lambda x: (-x[2], abs(x[1] - 17)))
        return candidates[0][0], candidates[0][1]
    except Exception:
        return None, 0


# ── Option price estimator ─────────────────────────────────────────────────

def _bs_price(S: float, K: float, T_days: int, iv_pct: float, is_call: bool) -> float:
    """Black-Scholes approximate price."""
    try:
        T = max(T_days, 1) / 252.0
        sig = max(iv_pct / 100.0, 0.05)
        sqT = math.sqrt(T)
        d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * sqT)
        d2 = d1 - sig * sqT

        def cdf(x):
            t = 1 / (1 + 0.2316419 * abs(x))
            poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
            p = 1 - 0.3989422803 * math.exp(-0.5 * x * x) * poly
            return p if x >= 0 else 1 - p

        if is_call:
            return max(0.01, round(S * cdf(d1) - K * cdf(d2), 2))
        else:
            return max(0.01, round(K * cdf(-d2) - S * cdf(-d1), 2))
    except Exception:
        intrinsic = max(0.0, S - K if is_call else K - S)
        return max(0.05, round(intrinsic + 0.5, 2))


def _spread_credit(S, sell_K, buy_K, dte, iv, is_call) -> Tuple[float, float]:
    """Returns (credit, max_loss) for a vertical spread."""
    sell_px = _bs_price(S, sell_K, dte, iv, is_call)
    buy_px = _bs_price(S, buy_K, dte, iv, is_call)
    credit = round(sell_px - buy_px, 2)
    width = abs(buy_K - sell_K)
    ml = round(width - credit, 2)
    return credit, ml


def _round_strike(price: float, interval: float = 1.0) -> float:
    if interval <= 0:
        interval = 1.0
    return round(round(price / interval) * interval, 2)


def _strike_interval(spot: float) -> float:
    """Infer standard strike interval from spot price."""
    if spot < 20:    return 0.5
    if spot < 50:    return 1.0
    if spot < 100:   return 2.5
    if spot < 200:   return 5.0
    if spot < 500:   return 5.0
    return 10.0


# ── Entry Quality Score ────────────────────────────────────────────────────

def _grade_for_score(score: float) -> tuple:
    """Matches _entry_score's own grade thresholds exactly -- used to keep
    grade consistent with score after post-hoc adjustments (width cap,
    POP/RR balance) that happen after _entry_score already returned."""
    if score >= 80:   return "A", "OPEN"
    elif score >= 65: return "B", "OPEN"
    elif score >= 50: return "C", "OPEN_SMALL"
    elif score >= 35: return "D", "OPEN_SMALL"
    else:             return "F", "AVOID"


def _entry_score(
    symbol: str, trade_type: str, spot: float,
    regime_bias: str, rs: Optional[float], iv_rank: Optional[float],
    pcr: Optional[float], put_wall: Optional[float], call_wall: Optional[float],
    gamma_flip: Optional[float],
    confluence: str = "UNKNOWN", rsi_trend: str = "",
) -> Dict:
    """
    Compact entry quality scorer.
    Returns dict with score (0-100), grade, recommendation, pros, cons.

    confluence/rsi_trend (new): daily regime alone can rate a directional
    trade highly even when the weekly timeframe doesn't support it, or
    when RSI is actively moving the opposite direction of the proposed
    bias -- both real, checkable signals that were previously invisible to
    this scorer. See regime_scanner.py's _compute_weekly_regime /
    rsi_trend computation for where these come from.
    """
    score = 50
    pros: List[str] = []
    cons: List[str] = []
    is_bull = trade_type in ("PS", "PB", "CB")
    is_bear = trade_type in ("CS",)
    is_ic = trade_type == "IC"
    is_credit = trade_type in ("PS", "CS", "IC")

    # Regime (25 pts)
    b = regime_bias.lower()
    if is_bull:
        if "bull" in b:  score += 15; pros.append(f"Regime bullish ✓")
        elif "bear" in b: score -= 12; cons.append(f"Regime bearish — headwind")
        if rsi_trend == "FALLING":
            score -= 8; cons.append("RSI trending down — momentum fading against this bullish call")
    elif is_bear:
        if "bear" in b:  score += 15; pros.append(f"Regime bearish ✓")
        elif "bull" in b: score -= 12; cons.append(f"Regime bullish — headwind")
        if rsi_trend == "RISING":
            score -= 8; cons.append("RSI trending up — momentum improving against this bearish call")

    # Multi-timeframe confluence (new): a directional call built on daily
    # structure alone is weaker when the weekly timeframe disagrees or is
    # genuinely range-bound -- this is what actually determines whether a
    # directional spread or an Iron Condor fits better, not daily regime
    # in isolation.
    if not is_ic:
        if confluence == "AGREE":
            score += 6; pros.append("Daily + weekly trend agree")
        elif confluence == "WEEKLY_SHOCK_AGAINST":
            score -= 20; cons.append("Recent weekly candle moved sharply against this trade's direction — possible reversal in progress")
        elif confluence == "DISAGREE":
            score -= 15; cons.append("Daily and weekly trend disagree — this call is fighting the higher timeframe")
        elif confluence == "WEEKLY_SIDEWAYS":
            score -= 10; cons.append("Weekly is sideways — consider an Iron Condor instead of a directional call")
    elif is_ic and confluence == "WEEKLY_SIDEWAYS":
        score += 8; pros.append("Weekly genuinely range-bound ✓ — fits an Iron Condor well")
    elif is_ic:
        if "bull" in b or "bear" in b:
            score += 5; pros.append(f"Directional regime — IC acceptable")

    # RS vs SPY (20 pts)
    if rs is not None:
        if is_bull:
            if rs > 5:    score += 15; pros.append(f"RS +{rs:.1f}% vs SPY")
            elif rs > 2:  score += 8;  pros.append(f"RS +{rs:.1f}% vs SPY (mild)")
            elif rs < -5: score -= 12; cons.append(f"RS {rs:.1f}% vs SPY — underperforming")
            elif rs < -2: score -= 6;  cons.append(f"RS {rs:.1f}% vs SPY (mild weakness)")
        elif is_bear:
            if rs < -5:   score += 15; pros.append(f"RS {rs:.1f}% vs SPY — confirmed weakness")
            elif rs < -2: score += 8;  pros.append(f"RS {rs:.1f}% vs SPY (mild weakness)")
            elif rs > 5:  score -= 12; cons.append(f"RS +{rs:.1f}% — stock outperforming")
            elif rs > 2:  score -= 6;  cons.append(f"RS +{rs:.1f}% vs SPY")
        elif is_ic:
            if abs(rs) <= 3: score += 8;  pros.append(f"RS {rs:+.1f}% neutral — ideal for IC")
            elif abs(rs) > 8: score -= 5; cons.append(f"RS {rs:+.1f}% — trending hard, IC risky")

    # IV Rank (15 pts)
    if iv_rank is not None:
        if is_credit:
            if iv_rank > 65:   score += 12; pros.append(f"IVR {iv_rank:.0f}% — premium rich")
            elif iv_rank > 45: score += 7;  pros.append(f"IVR {iv_rank:.0f}% — good credit")
            elif iv_rank < 20: score -= 10; cons.append(f"IVR {iv_rank:.0f}% — thin premium")
            elif iv_rank < 30: score -= 5;  cons.append(f"IVR {iv_rank:.0f}% — low IV")
        else:
            if iv_rank < 25:   score += 12; pros.append(f"IVR {iv_rank:.0f}% — options cheap")
            elif iv_rank < 35: score += 7;  pros.append(f"IVR {iv_rank:.0f}% — reasonable cost")
            elif iv_rank > 65: score -= 10; cons.append(f"IVR {iv_rank:.0f}% — options expensive")

    # PCR (15 pts)
    if pcr is not None:
        if is_bull:
            if pcr < 0.7:   score += 12; pros.append(f"PCR {pcr:.2f} — bullish flow")
            elif pcr < 0.9: score += 6;  pros.append(f"PCR {pcr:.2f} — balanced/bullish")
            elif pcr > 1.3: score -= 12; cons.append(f"PCR {pcr:.2f} — heavy puts/bearish")
            elif pcr > 1.1: score -= 6;  cons.append(f"PCR {pcr:.2f} — mildly bearish")
        elif is_bear:
            if pcr > 1.3:   score += 12; pros.append(f"PCR {pcr:.2f} — bearish flow")
            elif pcr > 1.1: score += 6;  pros.append(f"PCR {pcr:.2f} — mildly bearish")
            elif pcr < 0.7: score -= 12; cons.append(f"PCR {pcr:.2f} — call-heavy")
        elif is_ic:
            if 0.8 < pcr < 1.2: score += 8; pros.append(f"PCR {pcr:.2f} — balanced (IC ideal)")

    # Wall support/resistance (15 pts)
    if spot and put_wall and call_wall:
        pd = (spot - put_wall) / spot * 100
        cd = (call_wall - spot) / spot * 100
        if is_bull:
            if pd < 2:    score += 10; pros.append(f"Put wall ${put_wall:.0f} — strong support nearby")
            elif pd < 5:  score += 6;  pros.append(f"Put wall ${put_wall:.0f} ({pd:.1f}% away)")
            else:         score -= 3;  cons.append(f"Put wall ${put_wall:.0f} far ({pd:.1f}%)")
        elif is_bear:
            if cd < 2:    score += 10; pros.append(f"Call wall ${call_wall:.0f} — strong resistance nearby")
            elif cd < 5:  score += 6;  pros.append(f"Call wall ${call_wall:.0f} ({cd:.1f}% away)")
            else:         score -= 3;  cons.append(f"Call wall ${call_wall:.0f} far ({cd:.1f}%)")
        elif is_ic:
            if pd < 5 and cd < 5:
                score += 10; pros.append(f"Pinched walls ${put_wall:.0f}–${call_wall:.0f}")
            elif cd < 3 or pd < 3:
                score -= 5;  cons.append("Spot too close to one wall — IC risk")

    # Gamma flip (10 pts)
    if spot and gamma_flip:
        if is_bull and spot > gamma_flip:
            score += 8; pros.append(f"Above gamma flip ${gamma_flip:.0f} — dealers stabilising")
        elif is_bull and abs(gamma_flip - spot) / spot < 0.01:
            score -= 5; cons.append(f"Near gamma flip ${gamma_flip:.0f} — vol risk")
        elif is_bear and spot < gamma_flip:
            score += 8; pros.append(f"Below gamma flip ${gamma_flip:.0f} — trend amplification")

    score = max(5, min(97, round(score)))

    if score >= 80:   grade, rec = "A", "OPEN"
    elif score >= 65: grade, rec = "B", "OPEN"
    elif score >= 50: grade, rec = "C", "OPEN_SMALL"
    elif score >= 35: grade, rec = "D", "OPEN_SMALL"
    else:             grade, rec = "F", "AVOID"

    return {"score": score, "grade": grade, "recommendation": rec,
            "pros": pros, "cons": cons}


# ── Trade builder ──────────────────────────────────────────────────────────

def _build_trade(
    symbol: str, expiry: str, dte: int, spot: float, iv_pct: float,
    trend: str, rsi_diff: float, ta: Dict,
    walls: Dict, rs: Optional[float], iv_rank: Optional[float], pcr: Optional[float],
    earn_days: int,
) -> List[Dict]:
    """
    Build exact trade legs — 1 to 5 strikes wide (risk-controlled).
    Uses RSI, RSIDiff90, MACD, and EMA gap to distinguish:
      - REVERSAL_BULL/BEAR: oversold/overbought with MACD turning → debit or tight credit
      - TRENDING: momentum confirmed → standard credit spread
      - RANGE: both sides quiet → IC
    Returns list of opportunity dicts, each fully self-contained.
    """
    interval = _strike_interval(spot)
    put_wall = walls.get("put_wall") or round(spot * 0.97, 2)
    call_wall = walls.get("call_wall") or round(spot * 1.03, 2)
    gamma_flip = walls.get("gamma_flip")
    max_pain = walls.get("max_pain")
    atr = _safe(ta.get("atr")) or spot * 0.01

    trend_u = (trend or "").upper()
    is_uptrend = "UP" in trend_u
    is_downtrend = "DOWN" in trend_u
    is_sideways = "SIDE" in trend_u or "MILD" in trend_u or (not is_uptrend and not is_downtrend)
    iv_rank_n = iv_rank or 50

    # ── Enhanced technical signals ─────────────────────────────────────────
    rsi14       = _safe(ta.get("rsi14")) or _safe(ta.get("rsi")) or 50.0
    macd_hist   = _safe(ta.get("macd_hist")) or 0.0
    macd_rising = bool(ta.get("macd_hist_rising", False))
    macd_cross_bull = bool(ta.get("macd_cross_bull", False))
    macd_cross_bear = bool(ta.get("macd_cross_bear", False))
    macd_above_zero = bool(ta.get("macd_above_zero", False))
    ema_gap_pct = _safe(ta.get("ema_gap_pct")) or 0.0  # (ema20-ema50)/spot ×100
    market_state= ta.get("market_state", "RANGE")
    reversal_bull = bool(ta.get("reversal_bull", False))
    reversal_bear = bool(ta.get("reversal_bear", False))
    momentum    = ta.get("momentum", "NEUTRAL")

    # ── Strike width: HARD CAP 1–5 strikes ────────────────────────────────
    # Width choices by market state and conviction:
    #   Reversal / early move  → narrow (1-2 strikes): cheaper, faster to profit
    #   Confirmed trend        → standard (3 strikes): balance credit vs risk
    #   Strong trend + high IV → wider (4-5 strikes): more credit headroom
    # "N strikes" = N × interval dollars wide
    if reversal_bull or reversal_bear:
        spread_width_strikes = 2   # early/uncertain — stay narrow
    elif abs(rsi_diff) > 20 and abs(ema_gap_pct) > 1.5:
        spread_width_strikes = 4   # strong confirmed trend
    elif abs(rsi_diff) > 10 and iv_rank_n > 55:
        spread_width_strikes = 3
    else:
        spread_width_strikes = 2

    # Absolute dollar cap
    spread_width = round(spread_width_strikes * interval, 2)
    # IC legs are always 1-2 strikes wide each side for tight risk
    ic_leg_width = round(min(2, spread_width_strikes) * interval, 2)

    # ── HARD ABSOLUTE DOLLAR CAP: 10 points wide, regardless of strike
    # interval or "N strikes" framing above. The "N strikes" system alone
    # doesn't control for how expensive the underlying is -- confirmed
    # directly: WDC at ~$582 gets interval=$10 (see _strike_interval), so
    # even a "moderate" 3-strike spread becomes $30 wide, requiring up to
    # $3000/contract of capital at risk on a single credit spread. Clamp
    # to the largest valid multiple of `interval` that doesn't exceed the
    # cap, and flag it as a real compromise (not silent) when the width
    # actually had to be narrowed from what the trend/IV signals called for
    # -- a forcibly-narrowed spread can have meaningfully worse credit/POP
    # characteristics than the "ideal" width would have.
    MAX_SPREAD_WIDTH_DOLLARS = 10.0
    width_was_capped = False
    if spread_width > MAX_SPREAD_WIDTH_DOLLARS:
        capped_strikes = max(1, int(MAX_SPREAD_WIDTH_DOLLARS // interval))
        capped_width = round(capped_strikes * interval, 2)
        if capped_width > MAX_SPREAD_WIDTH_DOLLARS:
            # Even a single strike interval exceeds the cap (only possible
            # for very high strike intervals) -- no valid spread fits, skip
            # directional credit/debit spread construction below entirely.
            return []
        width_was_capped = spread_width - capped_width
        spread_width = capped_width
        spread_width_strikes = capped_strikes
    ic_leg_width = min(ic_leg_width, MAX_SPREAD_WIDTH_DOLLARS)

    trades = []

    def _pnr(long_k):
        """PNR = long_strike − (long_strike × dte × atr) / 2000"""
        if not (long_k and dte and atr):
            return None
        return round(long_k - (long_k * dte * atr) / 2000, 2)

    def _pnr_call(short_k):
        """Upper PNR for call spreads."""
        if not (short_k and dte and atr):
            return None
        return round(short_k + (short_k * dte * atr) / 2000, 2)

    def _otm_pct(k, is_call_side):
        if is_call_side:
            return round((k - spot) / spot * 100, 1) if k > spot else 0
        return round((spot - k) / spot * 100, 1) if k < spot else 0

    def _pop_credit(strike, is_put_side, dte_horizon=None):
        """Probability the underlying is on the OTM (winning) side of
        `strike` at a given time horizon -- using Black-Scholes N(d2),
        the standard risk-neutral probability of expiring ITM/OTM, with
        real IV and time-to-expiry as actual inputs. The previous version
        of this function was `65 + otm_pct*3.4`, clamped to [50,90] --
        completely blind to both volatility and time, meaning every POP
        shown up to this point used the same crude estimate regardless of
        whether IV was 15 or 80, or DTE was 10 or 45.

        dte_horizon defaults to max(1, dte-7): the probability of still
        being OTM one week before expiry, not at expiry itself. This
        matters because the exit plan text below already says "close at
        <7 DTE" -- late-cycle gamma risk means the position is meant to
        be closed before full expiry regardless, so the probability that
        actually reflects real risk exposure is "OTM through the planned
        exit," which is a higher (easier to clear) bar than "OTM at
        expiry" since there's less time for the underlying to move
        against the position. Pass dte_horizon=dte explicitly for the
        standard "POP at expiry" number instead.
        """
        try:
            S = max(float(spot), 0.01)
            K = max(float(strike), 0.01)
            horizon = dte_horizon if dte_horizon is not None else max(1, dte - 7)
            T = max(float(horizon), 0.5) / 365.0
            sigma = max(0.05, min(float(iv_pct or 20) / 100.0, 2.50))
            r = 0.04
            d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
            d2 = d1 - sigma * math.sqrt(T)
            norm_cdf = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
            if is_put_side:
                # Short put: loses (ITM) if S ends up below K -> P(ITM)=N(-d2)
                # Wins (OTM, = POP) = 1 - N(-d2) = N(d2)
                p = norm_cdf(d2)
            else:
                # Short call: loses (ITM) if S ends up above K -> P(ITM)=N(d2)
                # Wins (OTM, = POP) = 1 - N(d2) = N(-d2)
                p = norm_cdf(-d2)
            return round(min(97, max(50, p * 100)))
        except Exception:
            # Fall back to the old heuristic only if something's genuinely
            # wrong with the inputs (e.g. non-finite values) -- never let
            # a POP calculation crash the whole trade build.
            otm_p = _otm_pct(strike, not is_put_side)
            return min(90, max(50, round(65 + otm_p * 3.4)))

    def _manage_text(tt, credit, width, expiry_str):
        half = round(credit * 0.5, 2)
        return (f"Take profit at 50% credit (${half}/contract). "
                f"Stop at 50% max loss. Close at <7 DTE if not profitable.")

    def _market_state_label():
        if reversal_bull:
            return f"Reversal setup (RSI {rsi14:.0f} oversold, MACD{'▲ cross' if macd_cross_bull else ' rising'})"
        if reversal_bear:
            return f"Reversal setup (RSI {rsi14:.0f} overbought, MACD{'▼ cross' if macd_cross_bear else ' falling'})"
        if is_uptrend:
            return f"Uptrend (EMA gap {ema_gap_pct:+.1f}%, MACD {'▲' if macd_above_zero else '~'})"
        if is_downtrend:
            return f"Downtrend (EMA gap {ema_gap_pct:+.1f}%, MACD {'▼' if not macd_above_zero else '~'})"
        return f"Range/Sideways (RSI {rsi14:.0f}, diff {rsi_diff:+.1f})"

    state_label = _market_state_label()
    MIN_RR = 0.35   # slightly looser for narrow spreads

    # ── Bull Put Spread ──────────────────────────────────────────────────────
    # Open when: uptrend OR reversal_bull OR oversold bounce setup
    # Width: spread_width (1-5 strikes wide, hard cap)
    bull_ok = (
        is_uptrend or
        reversal_bull or
        (not is_downtrend and rsi_diff > -8) or
        (rsi14 < 38 and macd_rising)  # oversold + MACD turning = bull setup
    )
    if bull_ok and iv_rank_n >= 20:
        otm_pcts = [0.005, 0.01, 0.015, 0.02, 0.025, 0.03] if not reversal_bull else [0.005, 0.01, 0.015]
        for pct in otm_pcts:
            sell_s = _round_strike(spot * (1 - pct), interval)
            buy_s  = _round_strike(sell_s - spread_width, interval)
            if sell_s >= spot or buy_s >= sell_s:
                continue
            actual_width = round(sell_s - buy_s, 2)
            # enforce hard cap: max 5 strikes
            if actual_width > 5 * interval:
                buy_s = _round_strike(sell_s - 5 * interval, interval)
                actual_width = round(sell_s - buy_s, 2)
            cr, ml = _spread_credit(spot, sell_s, buy_s, dte, iv_pct or 20, False)
            if ml <= 0 or cr / (cr + ml) < MIN_RR:
                continue
            otm = _otm_pct(sell_s, False)
            pop = _pop_credit(sell_s, True)
            pnr_val = _pnr(buy_s)
            # Enhanced rationale with all four signals
            tech_detail = (
                f"RSI {rsi14:.0f} {'(oversold)' if rsi14 < 38 else ''}. "
                f"RSIDiff90 {rsi_diff:+.1f} ({'depressed → reversal candidate' if rsi_diff < -10 else 'acceptable'}). "
                f"MACD hist {macd_hist:+.4f} ({'▲ cross — momentum turning bull' if macd_cross_bull else '▲ rising' if macd_rising else 'flat'}). "
                f"EMA gap {ema_gap_pct:+.1f}% ({'bull stack' if ema_gap_pct > 0 else 'bear stack — caution'})."
            )
            rationale = (
                f"Bull Put Spread on {symbol}: Sell ${sell_s}P / Buy ${buy_s}P — {actual_width:.0f}pt wide ({spread_width_strikes} strikes). "
                f"Expiry {expiry} ({dte} DTE). Sell is {otm:.1f}% OTM. "
                f"Market state: {state_label}. {tech_detail} "
                f"{'Put wall at $'+str(int(put_wall))+' supports spread. ' if put_wall and abs(put_wall - sell_s) < spot * 0.04 else ''}"
                f"{'RS +'+str(rs)+'% vs SPY — outperforming. ' if rs and rs > 0 else ''}"
                f"IV Rank {iv_rank_n:.0f}% — credit: ${cr}/contract. Max loss ${ml:.2f}/contract. POP ~{pop}%."
            )
            trades.append({
                "trade_type": "PS", "bias": "Bullish", "symbol": symbol,
                "expiry": expiry, "dte": dte,
                "sell_strike": sell_s, "buy_strike": buy_s,
                "legs": f"Sell {sell_s}P / Buy {buy_s}P",
                "width": actual_width, "width_strikes": spread_width_strikes,
                "est_credit": cr, "max_loss": ml,
                "max_gain_per_contract": round(cr * 100, 0),
                "max_loss_per_contract": round(ml * 100, 0),
                "rr": round(cr / ml, 2) if ml > 0 else 0,
                "pop": pop, "otm_pct": otm,
                "pnr": pnr_val, "iv_used": iv_pct,
                "market_state": market_state,
                "rsi14": rsi14, "rsi_diff": rsi_diff,
                "macd_hist": macd_hist, "macd_cross_bull": macd_cross_bull,
                "ema_gap_pct": ema_gap_pct,
                "manage": _manage_text("PS", cr, actual_width, expiry),
                "rationale": rationale.strip(),
            })
            break

    # ── Bear Call Spread ─────────────────────────────────────────────────────
    # Open when: downtrend OR reversal_bear OR overbought fade setup
    bear_ok = (
        is_downtrend or
        reversal_bear or
        (not is_uptrend and rsi_diff < 8) or
        (rsi14 > 62 and not macd_rising)  # overbought + MACD rolling over
    )
    if bear_ok and iv_rank_n >= 20:
        otm_pcts = [0.005, 0.01, 0.015, 0.02, 0.025, 0.03] if not reversal_bear else [0.005, 0.01, 0.015]
        for pct in otm_pcts:
            sell_s = _round_strike(spot * (1 + pct), interval)
            buy_s  = _round_strike(sell_s + spread_width, interval)
            if sell_s <= spot or buy_s <= sell_s:
                continue
            actual_width = round(buy_s - sell_s, 2)
            if actual_width > 5 * interval:
                buy_s = _round_strike(sell_s + 5 * interval, interval)
                actual_width = round(buy_s - sell_s, 2)
            cr, ml = _spread_credit(spot, sell_s, buy_s, dte, iv_pct or 20, True)
            if ml <= 0 or cr / (cr + ml) < MIN_RR:
                continue
            otm = _otm_pct(sell_s, True)
            pop = _pop_credit(sell_s, False)
            pnr_val = _pnr_call(sell_s)
            tech_detail = (
                f"RSI {rsi14:.0f} {'(overbought)' if rsi14 > 62 else ''}. "
                f"RSIDiff90 {rsi_diff:+.1f} ({'elevated → reversal candidate' if rsi_diff > 10 else 'acceptable'}). "
                f"MACD hist {macd_hist:+.4f} ({'▼ cross — momentum turning bear' if macd_cross_bear else '▼ falling' if not macd_rising else 'flat'}). "
                f"EMA gap {ema_gap_pct:+.1f}% ({'bear stack' if ema_gap_pct < 0 else 'bull stack — caution'})."
            )
            rationale = (
                f"Bear Call Spread on {symbol}: Sell ${sell_s}C / Buy ${buy_s}C — {actual_width:.0f}pt wide ({spread_width_strikes} strikes). "
                f"Expiry {expiry} ({dte} DTE). Sell is {otm:.1f}% OTM. "
                f"Market state: {state_label}. {tech_detail} "
                f"{'Call wall at $'+str(int(call_wall))+' acts as resistance. ' if call_wall and abs(call_wall - sell_s) < spot * 0.04 else ''}"
                f"{'RS '+str(rs)+'% vs SPY — underperforming. ' if rs and rs < 0 else ''}"
                f"IV Rank {iv_rank_n:.0f}% — credit: ${cr}/contract. Max loss ${ml:.2f}/contract. POP ~{pop}%."
            )
            trades.append({
                "trade_type": "CS", "bias": "Bearish", "symbol": symbol,
                "expiry": expiry, "dte": dte,
                "sell_strike": sell_s, "buy_strike": buy_s,
                "legs": f"Sell {sell_s}C / Buy {buy_s}C",
                "width": actual_width, "width_strikes": spread_width_strikes,
                "est_credit": cr, "max_loss": ml,
                "max_gain_per_contract": round(cr * 100, 0),
                "max_loss_per_contract": round(ml * 100, 0),
                "rr": round(cr / ml, 2) if ml > 0 else 0,
                "pop": pop, "otm_pct": otm,
                "pnr": pnr_val, "iv_used": iv_pct,
                "market_state": market_state,
                "rsi14": rsi14, "rsi_diff": rsi_diff,
                "macd_hist": macd_hist, "macd_cross_bear": macd_cross_bear,
                "ema_gap_pct": ema_gap_pct,
                "manage": _manage_text("CS", cr, actual_width, expiry),
                "rationale": rationale.strip(),
            })
            break

    # ── Iron Condor ──────────────────────────────────────────────────────────
    # Open when: sideways + abs(rsi_diff) < 12 + MACD near zero
    ic_ok = (
        is_sideways and
        abs(rsi_diff) < 15 and
        iv_rank_n >= 35 and
        not reversal_bull and not reversal_bear
    )
    if ic_ok:
        best_put = best_call = None
        for pct in [0.01, 0.015, 0.02, 0.025, 0.03]:
            sp2 = _round_strike(spot * (1 - pct), interval)
            bp2 = _round_strike(sp2 - ic_leg_width, interval)
            if sp2 >= spot or bp2 >= sp2:
                continue
            if (sp2 - bp2) > 5 * interval:
                bp2 = _round_strike(sp2 - 5 * interval, interval)
            cr_p, ml_p = _spread_credit(spot, sp2, bp2, dte, iv_pct or 20, False)
            if ml_p > 0 and cr_p / (cr_p + ml_p) >= MIN_RR:
                best_put = (sp2, bp2, cr_p, ml_p)
                break
        for pct in [0.01, 0.015, 0.02, 0.025, 0.03]:
            sc2 = _round_strike(spot * (1 + pct), interval)
            bc2 = _round_strike(sc2 + ic_leg_width, interval)
            if sc2 <= spot or bc2 <= sc2:
                continue
            if (bc2 - sc2) > 5 * interval:
                bc2 = _round_strike(sc2 + 5 * interval, interval)
            cr_c, ml_c = _spread_credit(spot, sc2, bc2, dte, iv_pct or 20, True)
            if ml_c > 0 and cr_c / (cr_c + ml_c) >= MIN_RR:
                best_call = (sc2, bc2, cr_c, ml_c)
                break
        if best_put and best_call:
            sp2, bp2, cr_p, ml_p = best_put
            sc2, bc2, cr_c, ml_c = best_call
            total_cr = round(cr_p + cr_c, 2)
            ml_ic = max(ml_p, ml_c)
            rr_ic = round(total_cr / ml_ic, 2) if ml_ic > 0 else 0
            if rr_ic >= MIN_RR:
                pop_ic = round((_pop_credit(sp2, True) + _pop_credit(sc2, False)) / 2)
                range_w = round(sc2 - sp2, 2)
                tech_detail = (
                    f"RSI {rsi14:.0f} (neutral). "
                    f"RSIDiff90 {rsi_diff:+.1f} (within range). "
                    f"MACD hist {macd_hist:+.4f} ({'flat/neutral' if abs(macd_hist) < 0.1 else 'mild bias'}). "
                    f"EMA gap {ema_gap_pct:+.1f}% (coiling)."
                )
                rationale = (
                    f"Iron Condor on {symbol}: expiry {expiry} ({dte} DTE). "
                    f"Put spread: Sell ${sp2}P/Buy ${bp2}P ({round(sp2-bp2,0):.0f}pt). "
                    f"Call spread: Sell ${sc2}C/Buy ${bc2}C ({round(bc2-sc2,0):.0f}pt). "
                    f"Profit zone ${sp2}–${sc2} ({range_w:.0f}pt = {round(range_w/spot*100,1)}% of spot). "
                    f"Market state: {state_label}. {tech_detail} "
                    f"IV Rank {iv_rank_n:.0f}% — total credit ${total_cr}/contract. Max loss ${ml_ic:.2f}/contract. POP ~{pop_ic}%."
                )
                trades.append({
                    "trade_type": "IC", "bias": "Neutral", "symbol": symbol,
                    "expiry": expiry, "dte": dte,
                    "put_sell": sp2, "put_buy": bp2, "call_sell": sc2, "call_buy": bc2,
                    "legs": f"Sell {sp2}P/Buy {bp2}P · Sell {sc2}C/Buy {bc2}C",
                    "range_low": sp2, "range_high": sc2,
                    "width": round((sp2-bp2 + bc2-sc2)/2, 2),
                    "est_credit": total_cr, "max_loss": ml_ic,
                    "max_gain_per_contract": round(total_cr * 100, 0),
                    "max_loss_per_contract": round(ml_ic * 100, 0),
                    "rr": rr_ic, "pop": pop_ic, "iv_used": iv_pct,
                    "market_state": market_state,
                    "rsi14": rsi14, "rsi_diff": rsi_diff,
                    "macd_hist": macd_hist, "ema_gap_pct": ema_gap_pct,
                    "manage": (f"Take profit at 50% credit (${round(total_cr*0.5,2)}). "
                               f"If either short strike breached, close that side immediately. "
                               f"Close full position at <7 DTE."),
                    "rationale": rationale.strip(),
                })

    if width_was_capped:
        for t in trades:
            t["width_capped_note"] = (
                f"Spread width capped to ${spread_width:.0f} wide (10-point max) -- "
                f"trend/IV signals called for ${spread_width + width_was_capped:.0f}, narrowed to control capital at risk"
            )

    return trades


# ── Main scan function for one symbol ─────────────────────────────────────

def _scan_one(symbol: str, dte_min: int, dte_max: int, min_earn_days: int, min_score: int,
               allowed_types: Optional[List[str]] = None, prefetched_df=None) -> Optional[Dict]:
    """
    Run the full scan pipeline for a single symbol.
    Returns opportunity dict or None if filtered/errored.

    allowed_types: optionally restrict which trade types (PS=bullish credit,
    CS=bearish credit, IC=sideways/neutral) are even considered — used by
    Signal Notifier so a scanner query tagged e.g. "Bearish" only gets
    scored/suggested against bearish setups, instead of being scored as if
    any direction was acceptable and then penalized for not being bullish.

    prefetched_df: this symbol's daily history if the caller already
    batch-fetched the whole watchlist (see _batch_fetch_histories) — reused
    for both the TA computation and the RS-vs-SPY calc so this symbol needs
    zero individual price-history network calls of its own.
    """
    try:
        # 1. Earnings guard
        earn_days = _get_earn_days(symbol)
        if earn_days < min_earn_days:
            return {"symbol": symbol, "filtered": True, "filter_reason": f"Earnings in {earn_days} days"}

        # 2. TA
        ta = _get_ta(symbol, prefetched_df=prefetched_df)
        if not ta:
            return None
        spot = _safe(ta.get("price"))
        if not spot or spot <= 0:
            return None

        trend      = ta.get("trend", "SIDEWAYS")
        rsi_diff   = _safe(ta.get("rsi_ema_diff")) or 0.0
        iv_pct     = _safe(ta.get("iv_est")) or 20.0
        iv_rank    = _safe(ta.get("iv_rank"))
        atr        = _safe(ta.get("atr")) or spot * 0.01
        market_state = ta.get("market_state", "RANGE")
        rsi14      = _safe(ta.get("rsi14") or ta.get("rsi")) or 50.0
        macd_hist  = _safe(ta.get("macd_hist")) or 0.0
        ema_gap_pct= _safe(ta.get("ema_gap_pct")) or 0.0

        # 3. Expiry
        expiry, dte = _pick_best_expiry(symbol, dte_min, dte_max)
        if not expiry or dte < dte_min:
            return {"symbol": symbol, "filtered": True, "filter_reason": "No suitable expiry found"}

        # 4. Market data
        rs = _get_rs_vs_spy(symbol, prefetched_df=prefetched_df)
        regime = _get_regime(symbol)
        walls = _get_pcr_and_walls(symbol, spot)
        pcr = walls.get("pcr")
        put_wall = walls.get("put_wall")
        call_wall = walls.get("call_wall")
        gamma_flip = walls.get("gamma_flip")
        max_pain = walls.get("max_pain")

        regime_bias = regime.get("bias", "")
        confluence = regime.get("confluence", "UNKNOWN")
        rsi_trend_val = regime.get("rsi_trend", "")

        # 5. Determine best trade type from conditions
        trend_u = (trend or "").upper()
        is_up = "UP" in trend_u
        is_down = "DOWN" in trend_u
        is_side = not is_up and not is_down

        # Score all three types and pick the best
        candidate_types = []
        if is_up or (not is_down and rsi_diff > -5):
            candidate_types.append("PS")
        if is_down or (not is_up and rsi_diff < 5):
            candidate_types.append("CS")
        if is_side or (not is_up and not is_down):
            candidate_types.append("IC")
        if not candidate_types:
            candidate_types = ["PS", "CS", "IC"]

        if allowed_types:
            allowed_set = {t.upper() for t in allowed_types}
            constrained = [t for t in candidate_types if t in allowed_set]
            if not constrained:
                dir_label = "/".join(sorted(allowed_set))
                return {"symbol": symbol, "filtered": True,
                        "filter_reason": f"No {dir_label} setup under current conditions (trend={trend})"}
            candidate_types = constrained

        best_type = candidate_types[0]
        best_eq = None
        for tt in candidate_types:
            eq = _entry_score(symbol, tt, spot, regime_bias, rs, iv_rank, pcr, put_wall, call_wall, gamma_flip,
                               confluence=confluence, rsi_trend=rsi_trend_val)
            if best_eq is None or eq["score"] > best_eq["score"]:
                best_eq = eq
                best_type = tt

        # 6. Skip if below threshold
        if best_eq["score"] < min_score:
            return {"symbol": symbol, "filtered": True,
                    "filter_reason": f"Entry score {best_eq['score']} below threshold {min_score}"}

        # 7. Build concrete trade legs
        trades = _build_trade(
            symbol, expiry, dte, spot, iv_pct, trend, rsi_diff, ta,
            walls, rs, iv_rank, pcr, earn_days,
        )
        # Filter to types this caller actually allows before falling back to "any"
        if allowed_types:
            allowed_set = {t.upper() for t in allowed_types}
            trades = [t for t in trades if t.get("trade_type") in allowed_set]
            if not trades:
                dir_label = "/".join(sorted(allowed_set))
                return {"symbol": symbol, "filtered": True,
                        "filter_reason": f"Conditions favor {best_type} but couldn't build viable {dir_label} legs"}
        # Filter to best_type first, fallback to any
        typed = [t for t in trades if t.get("trade_type") == best_type]
        if typed:
            trade = typed[0]
        elif trades:
            trade = trades[0]
        else:
            return {"symbol": symbol, "filtered": True, "filter_reason": "Could not build viable trade legs"}

        # 8. Compute final entry score for this trade type
        final_eq = _entry_score(
            symbol, trade["trade_type"], spot,
            regime_bias, rs, iv_rank, pcr, put_wall, call_wall, gamma_flip,
            confluence=confluence, rsi_trend=rsi_trend_val,
        )
        trade.update(final_eq)
        if trade.get("width_capped_note"):
            trade.setdefault("cons", []).append(trade["width_capped_note"])
            trade["score"] = max(10, trade.get("score", 50) - 8)

        # ── POP/RR balance ────────────────────────────────────────────
        # Grounded in the actual math rather than arbitrary "high POP low
        # RR is bad" thresholds: for a credit-spread-style payout, the
        # breakeven POP given a reward:risk ratio is 100/(1+RR) -- e.g. RR
        # 1.08 needs ~48% POP to break even. Comparing the trade's ACTUAL
        # POP against this breakeven is what "balance" should mean here --
        # a trade can have a "good" POP number in isolation and still be
        # a poor bet if RR doesn't support it, or vice versa.
        pop_val = trade.get("pop")
        rr_val = trade.get("rr")
        if pop_val is not None and rr_val and rr_val > 0:
            breakeven_pop = 100.0 / (1 + rr_val)
            edge = pop_val - breakeven_pop
            if edge > 15:
                trade["score"] = min(100, trade.get("score", 50) + 8)
                trade.setdefault("pros", []).append(
                    f"POP {pop_val}% comfortably clears the {breakeven_pop:.0f}% breakeven for RR {rr_val} — real edge")
            elif edge > 5:
                trade["score"] = min(100, trade.get("score", 50) + 4)
                trade.setdefault("pros", []).append(
                    f"POP {pop_val}% above the {breakeven_pop:.0f}% breakeven for RR {rr_val}")
            elif edge < -5:
                trade["score"] = max(10, trade.get("score", 50) - 12)
                trade.setdefault("cons", []).append(
                    f"POP {pop_val}% is BELOW the {breakeven_pop:.0f}% breakeven for RR {rr_val} — unfavorable math before fees/slippage")
            elif edge < 0:
                trade["score"] = max(10, trade.get("score", 50) - 6)
                trade.setdefault("cons", []).append(
                    f"POP {pop_val}% barely clears the {breakeven_pop:.0f}% breakeven for RR {rr_val} — thin margin")

        # ── High-POP credit trades don't need momentum ─────────────────
        # A well-OTM credit trade with a genuinely high POP wins by the
        # underlying NOT moving much, not by moving in its favor --
        # fundamentally different from a trade relying on an actual
        # directional move. Regime/momentum agreement is still a real
        # bonus (it can mean an earlier profit-take via the 50% rule),
        # but its absence shouldn't be penalized as heavily for a trade
        # that's already statistically safe by a wide margin on its own.
        # Scales from 0 at POP=75% up to a max +10 around POP=92%+.
        if pop_val is not None and trade.get("trade_type") in ("PS", "CS", "IC") and pop_val >= 75:
            momentum_offset = min(10, round((pop_val - 75) * 0.6))
            if momentum_offset > 0:
                trade["score"] = min(100, trade.get("score", 50) + momentum_offset)
                trade.setdefault("pros", []).append(
                    f"POP {pop_val}% is high enough that this trade doesn't need momentum or direction to "
                    f"work — just needs the underlying to stay roughly where it is"
                )

        # Keep grade consistent with score after the post-hoc adjustments
        # above -- final_eq's grade was assigned before these ran.
        trade["grade"], trade["recommendation"] = _grade_for_score(trade.get("score", 50))

        # ── Risk factors: what could actually derail this trade ────────
        # Distinct from "cons" above (which explains why the SCORE is what
        # it is) -- this is specifically "what would have to happen for
        # this trade to lose," using data already computed elsewhere in
        # this function rather than restating the score's own reasoning.
        risk_factors: List[str] = []
        short_strike = trade.get("sell_strike")
        trade_dte = dte

        # 1. Earnings/event risk -- a gap can blow through both the
        # technical setup and the delta-implied probability at once,
        # which nothing else in this scorer accounts for.
        if earn_days is not None and 0 <= earn_days <= trade_dte:
            risk_factors.append(
                f"Earnings in {earn_days}d, before this trade's {trade_dte} DTE expiry — "
                f"a gap could invalidate both the technical setup and the probability estimate at once"
            )

        # 2. Distance from the short strike to the nearest known S/R wall
        # -- a strike sitting close to a real level is a different risk
        # profile than the same delta with no nearby structure.
        put_wall = walls.get("put_wall") if isinstance(walls, dict) else None
        call_wall = walls.get("call_wall") if isinstance(walls, dict) else None
        if short_strike and trade.get("trade_type") in ("PS", "IC") and put_wall:
            dist_pct = abs(short_strike - put_wall) / spot * 100
            if dist_pct < 2.0:
                risk_factors.append(
                    f"Short strike is only {dist_pct:.1f}% from the put wall (${put_wall}) — "
                    f"a break of that level removes a key layer of support"
                )
        if short_strike and trade.get("trade_type") in ("CS", "IC") and call_wall:
            dist_pct = abs(call_wall - short_strike) / spot * 100
            if dist_pct < 2.0:
                risk_factors.append(
                    f"Short strike is only {dist_pct:.1f}% from the call wall (${call_wall}) — "
                    f"a break of that level removes a key layer of resistance"
                )

        # 3. PNR (point of no return) proximity -- how far spot actually
        # is from the level where this trade is effectively unrecoverable.
        pnr = trade.get("pnr")
        if pnr:
            pnr_dist_pct = abs(spot - pnr) / spot * 100
            if pnr_dist_pct < 5.0:
                risk_factors.append(
                    f"Spot is only {pnr_dist_pct:.1f}% from this trade's PNR (${pnr}) — "
                    f"limited room before the position is effectively unrecoverable"
                )

        # 4. The gamma-risk window itself -- explicit, not just implied by
        # the exit-plan text, since POP is now computed assuming you exit
        # by DTE-7 (see _pop_credit) -- staying past that materially
        # changes the actual risk this trade carries.
        if trade_dte and trade_dte > 7:
            risk_factors.append(
                f"POP above assumes closing by ~{max(1, trade_dte-7)} DTE (1 week before expiry) — "
                f"staying open into the final week adds real gamma risk this number doesn't cover"
            )

        # 5. Low IV rank -- less compensation for the risk being taken,
        # independent of how good the strike selection itself looks.
        if iv_rank is not None and iv_rank < 30:
            risk_factors.append(
                f"IV Rank {iv_rank:.0f} is low — less premium compensation for the risk vs. a higher-IV entry"
            )

        if risk_factors:
            trade["risk_factors"] = risk_factors

        # IV Rank advisory: this engine only builds premium-selling (credit)
        # structures. When IV Rank is low, selling premium is poor
        # risk/reward — flag it explicitly rather than silently suggesting
        # a credit spread as if IV were irrelevant.
        iv_rank_n = iv_rank if iv_rank is not None else 50
        if iv_rank_n < 30:
            trade["rationale"] = (
                (trade.get("rationale") or "").strip()
                + f" ⚠ IV Rank is low ({iv_rank_n:.0f}%) — credit spread premium here is thin; "
                  f"a debit vertical or waiting for elevated IV may be a better risk/reward."
            )
            trade["iv_rank_advisory"] = "low_iv_favors_debit"

        # 9. Build full opportunity record
        return {
            **trade,
            "symbol": symbol,
            "spot": spot,
            "trend": trend,
            "rsi_diff": rsi_diff,
            "iv_pct": iv_pct,
            "iv_rank": iv_rank,
            "atr": atr,
            "rs_vs_spy": rs,
            "regime": regime.get("regime", ""),
            "regime_bias": regime_bias,
            "regime_confidence": regime.get("confidence", 50),
            "pcr": pcr,
            "put_wall": put_wall,
            "call_wall": call_wall,
            "gamma_flip": gamma_flip,
            "max_pain": max_pain,
            "top_put_walls": walls.get("top_put_walls", []),
            "top_call_walls": walls.get("top_call_walls", []),
            "earn_days": earn_days,
            "market_state": market_state,
            "rsi14": rsi14,
            "macd_hist": macd_hist,
            "ema_gap_pct": ema_gap_pct,
            "filtered": False,
        }

    except Exception as e:
        return {"symbol": symbol, "filtered": True, "filter_reason": f"Error: {str(e)[:80]}"}


# ── Flask routes ───────────────────────────────────────────────────────────

@trade_opp_bp.route("/")
def page():
    return render_template("trade_opportunity_scanner.html")


@trade_opp_bp.route("/api/watchlists")
def api_watchlists():
    return jsonify({"watchlists": _watchlists()})


@trade_opp_bp.route("/api/market_regime")
def api_market_regime():
    return jsonify({
        "spy_regime": _get_market_regime(),
        "spy_rs": _get_rs_vs_spy("SPY"),
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })


@trade_opp_bp.route("/api/scan", methods=["POST"])
def api_scan():
    payload = request.get_json(force=True) or {}
    watchlist_id = payload.get("watchlist_id")
    dte_min      = int(payload.get("dte_min", DTE_MIN))
    dte_max      = int(payload.get("dte_max", DTE_MAX))
    min_score    = int(payload.get("min_score", 50))
    min_earn_days= int(payload.get("min_earn_days", MIN_EARN_DAYS))
    limit        = int(payload.get("limit", 40))
    trade_filter = (payload.get("trade_type") or "ALL").upper()

    symbols = _watchlist_symbols(watchlist_id)
    if not symbols:
        return jsonify({"error": "No symbols in watchlist"}), 400

    opportunities = []
    filtered_out = []
    errors = []

    histories = _batch_fetch_histories(symbols, period="1y")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {
            ex.submit(_scan_one, sym, dte_min, dte_max, min_earn_days, min_score,
                      prefetched_df=histories.get(sym.upper())): sym
            for sym in symbols
        }
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                result = fut.result()
                if result is None:
                    continue
                if result.get("filtered"):
                    filtered_out.append(result)
                else:
                    if trade_filter != "ALL" and result.get("trade_type") != trade_filter:
                        continue
                    opportunities.append(result)
            except Exception as e:
                errors.append({"symbol": sym, "error": str(e)[:80]})

    # Sort by entry score descending
    opportunities.sort(key=lambda x: x.get("score", 0), reverse=True)
    opportunities = opportunities[:limit]

    # OI-by-strike chart for the suggested expiry — only computed for the
    # final, already-limited set of tiles that will actually be shown, and
    # reads local cached data only (no live fetch), so this doesn't add
    # meaningful scan latency.
    for o in opportunities:
        try:
            o["oi_chart"] = _get_oi_chart_data(o.get("symbol"), o.get("expiry"), o.get("spot"))
        except Exception:
            o["oi_chart"] = {}

    # Market summary
    total_bull = sum(1 for o in opportunities if o.get("bias") == "Bullish")
    total_bear = sum(1 for o in opportunities if o.get("bias") == "Bearish")
    total_neutral = sum(1 for o in opportunities if o.get("bias") == "Neutral")
    avg_score = round(sum(o.get("score", 0) for o in opportunities) / max(len(opportunities), 1), 1)
    grade_dist = {}
    for o in opportunities:
        g = o.get("grade", "?")
        grade_dist[g] = grade_dist.get(g, 0) + 1

    scanned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    params = {"dte_min": dte_min, "dte_max": dte_max, "min_score": min_score,
              "min_earn_days": min_earn_days, "trade_type": trade_filter}
    summary = {
        "total": len(opportunities),
        "bull": total_bull, "bear": total_bear, "neutral": total_neutral,
        "avg_score": avg_score, "grade_distribution": grade_dist,
        "market_regime": _get_market_regime(),
        "scanned_count": len(symbols) if "symbols" in dir() else 0,
    }

    # Persist to cache
    _save_scan_cache(watchlist_id, scanned_at, params, opportunities,
                     summary, filtered_out[:30])

    return jsonify({
        "ok": True,
        "count": len(opportunities),
        "opportunities": opportunities,
        "filtered_count": len(filtered_out),
        "filtered": filtered_out[:20],
        "errors": errors[:10],
        "scanned_at": scanned_at,
        "params": params,
        "summary": summary,
        "cached": True,
    })


@trade_opp_bp.route("/api/scan/cached", methods=["GET"])
def api_scan_cached():
    """Return the last cached scan results for a watchlist (no re-fetch)."""
    watchlist_id = request.args.get("watchlist_id") or None
    cached = _load_scan_cache(watchlist_id)
    if not cached:
        return jsonify({"ok": False, "error": "No cached results found for this watchlist. Run a scan first."}), 404
    return jsonify({
        "ok": True,
        "from_cache": True,
        "scanned_at": cached["scanned_at"],
        "count": cached["result_count"],
        "opportunities": cached["opportunities"],
        "filtered_count": len(cached["filtered"]),
        "filtered": cached["filtered"],
        "params": cached["params"],
        "summary": cached["summary"],
        "errors": [],
    })


@trade_opp_bp.route("/api/cache/status", methods=["GET"])
def api_cache_status():
    """Return cache metadata for all watchlists (for UI display)."""
    try:
        _ensure_scan_cache_table()
        con = _conn()
        rows = con.execute(
            "SELECT watchlist_id, scanned_at, result_count, symbol_count FROM trade_scan_cache ORDER BY scanned_at DESC"
        ).fetchall()
        con.close()
        return jsonify({"caches": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"caches": [], "error": str(e)})


@trade_opp_bp.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    """Clear cached scan for a specific watchlist."""
    watchlist_id = (request.json or {}).get("watchlist_id") or None
    try:
        _ensure_scan_cache_table()
        con = _conn()
        if watchlist_id:
            con.execute("DELETE FROM trade_scan_cache WHERE watchlist_id=?", (str(watchlist_id),))
        else:
            con.execute("DELETE FROM trade_scan_cache")
        con.commit(); con.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@trade_opp_bp.route("/api/symbol_detail")
def api_symbol_detail():
    """Full detail for a single symbol — all three strategy types + full rationale."""
    symbol = (request.args.get("symbol") or "").upper().strip()
    dte_min = int(request.args.get("dte_min", DTE_MIN))
    dte_max = int(request.args.get("dte_max", DTE_MAX))
    if not symbol:
        return jsonify({"error": "symbol required"}), 400

    earn_days = _get_earn_days(symbol)
    ta = _get_ta(symbol)
    if not ta:
        return jsonify({"error": f"Could not fetch TA for {symbol}"}), 400

    spot = _safe(ta.get("price"))
    expiry, dte = _pick_best_expiry(symbol, dte_min, dte_max)
    iv_pct = _safe(ta.get("iv_est")) or 20.0
    iv_rank = _safe(ta.get("iv_rank"))
    trend = ta.get("trend", "SIDEWAYS")
    rsi_diff = _safe(ta.get("rsi_ema_diff")) or 0.0

    rs = _get_rs_vs_spy(symbol)
    regime = _get_regime(symbol)
    walls = _get_pcr_and_walls(symbol, spot)
    pcr = walls.get("pcr")

    all_trades = _build_trade(
        symbol, expiry, dte, spot, iv_pct, trend, rsi_diff, ta,
        walls, rs, iv_rank, pcr, earn_days,
    ) if expiry else []

    scored = []
    for t in all_trades:
        eq = _entry_score(symbol, t["trade_type"], spot,
                          regime.get("bias",""), rs, iv_rank,
                          pcr, walls.get("put_wall"), walls.get("call_wall"),
                          walls.get("gamma_flip"),
                          confluence=regime.get("confluence", "UNKNOWN"),
                          rsi_trend=regime.get("rsi_trend", ""))
        scored.append({**t, **eq})

    scored.sort(key=lambda x: x.get("score", 0), reverse=True)

    return jsonify({
        "symbol": symbol, "spot": spot, "expiry": expiry, "dte": dte,
        "trend": trend, "rsi_diff": rsi_diff, "iv_pct": iv_pct, "iv_rank": iv_rank,
        "earn_days": earn_days, "rs_vs_spy": rs,
        "regime": regime,
        "walls": walls,
        "pcr": pcr,
        "trades": scored,
    })
