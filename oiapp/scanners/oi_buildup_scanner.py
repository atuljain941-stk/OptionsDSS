"""
Reusable seller-flow scanner primitives.

The dashboard and AI Hub both call this module.  The scanner no longer treats
aggregate open-interest up/down as the whole signal.  It combines seller-side
PCR/OI flow with max-pain shift, skew/risk-reversal shift where available, price
confirmation, strategy timeframe guidance and cached earnings guardrails.

Historical data rules:
* Max pain can be computed from historical strike-level OI snapshots alone.
* True skew needs historical IV, or enough historical option price + spot data to
  back-solve IV.  If that is unavailable, the scanner uses an explicitly labelled
  OI-skew proxy and reduces confidence.
* No live fetch is performed here; all values come from local SQLite tables.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")

FUTURES_ROOTS = {"SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "XLE", "XLF", "XLK"}
NON_EQUITY = {
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO",
    "XLE", "XLF", "XLK", "XLV", "XLI", "XLP", "XLU", "XLY", "XLC", "XLRE", "XLB",
    "SMH", "SOXX", "GDX", "GDXJ", "TLT", "IEF", "SHY", "HYG", "LQD", "JNK", "BND",
    "AGG", "GLD", "SLV", "IAU", "USO", "IBIT", "FBTC", "GBTC", "EEM", "EFA", "VWO",
    "VXX", "ARKK", "KRE", "KBE", "XBI", "IBB", "RSP", "JEPI", "JEPQ", "XYLD", "QYLD",
}

# Rows are newest-first tuples: (date, call_oi, put_oi, call_vol, put_vol).
OiRow = Tuple[str, float, float, float, float]


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return con


def _safe_float(v: Any, default: Optional[float] = 0.0, ndigits: Optional[int] = None) -> Optional[float]:
    try:
        if v is None:
            return default
        f = float(v)
        if not math.isfinite(f):
            return default
        return round(f, ndigits) if ndigits is not None else f
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(float(v))
    except Exception:
        return default


def _pct_change(cur: Any, base: Any, ndigits: int = 2) -> Optional[float]:
    c = _safe_float(cur, None)
    b = _safe_float(base, None)
    if c is None or b is None:
        return None
    if abs(b) < 1e-9:
        if abs(c) < 1e-9:
            return 0.0
        return round(100.0 if c > 0 else -100.0, ndigits)
    return round((c - b) / abs(b) * 100.0, ndigits)


def _fmt_pct(v: Any, nd: int = 2) -> str:
    x = _safe_float(v, None)
    return "n/a" if x is None else f"{x:+.{nd}f}%"


def _fmt_num(v: Any, nd: int = 2) -> str:
    x = _safe_float(v, None)
    return "n/a" if x is None else f"{x:.{nd}f}"


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
        row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return bool(row)
    except Exception:
        return False


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]).lower() for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _ensure_options_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, expiration TEXT, type TEXT,
            strike REAL, price REAL, oi INTEGER, volume INTEGER, date TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_options_symbol_exp_date ON options(symbol, expiration, date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_options_exp ON options(expiration)")
    cols = _table_columns(con, "options")
    for col, sql in {
        "bid": "ALTER TABLE options ADD COLUMN bid REAL",
        "ask": "ALTER TABLE options ADD COLUMN ask REAL",
        "last": "ALTER TABLE options ADD COLUMN last REAL",
        "iv": "ALTER TABLE options ADD COLUMN iv REAL",
        "underlying": "ALTER TABLE options ADD COLUMN underlying REAL",
        "fetch_ts": "ALTER TABLE options ADD COLUMN fetch_ts TEXT",
    }.items():
        if col not in cols:
            try:
                con.execute(sql)
            except Exception:
                pass
    con.commit()


def _watchlist_symbols(con: sqlite3.Connection, watchlist_id: Optional[Any]) -> Optional[List[str]]:
    if watchlist_id in (None, "", 0, "0"):
        return None
    try:
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (int(watchlist_id),),
        ).fetchall()
    except Exception:
        return []
    return sorted({str(r["symbol"] or "").strip().upper() for r in rows if str(r["symbol"] or "").strip()})


def _cache_key(base: str, watchlist_id: Optional[Any] = None) -> str:
    if watchlist_id in (None, "", 0, "0"):
        return base
    return f"{base}_{int(watchlist_id)}"


# ---------------------------------------------------------------------------
# Window math
# ---------------------------------------------------------------------------

def _window_oi_stats(rows: Sequence[OiRow], days: int) -> Dict[str, Any]:
    """Return call/put/total OI and PCR changes for a lookback window."""
    latest = rows[0] if rows else None
    if not latest:
        return {
            "base": None, "call_oi_pct": 0.0, "put_oi_pct": 0.0, "oi_pct": 0.0,
            "vol_pct": 0.0, "pcr_base": 0.0, "pcr_now": 0.0, "pcr_chg": 0.0,
            "pcr_chg_pct": 0.0, "side_edge": 0.0,
        }
    idx = min(max(1, int(days)), len(rows) - 1) if len(rows) > 1 else 0
    base = rows[idx]

    call_now = _safe_float(latest[1]) or 0.0
    put_now = _safe_float(latest[2]) or 0.0
    total_now = call_now + put_now
    vol_now = (_safe_float(latest[3]) or 0.0) + (_safe_float(latest[4]) or 0.0)

    call_base = _safe_float(base[1]) or 0.0
    put_base = _safe_float(base[2]) or 0.0
    total_base = call_base + put_base
    vol_base = (_safe_float(base[3]) or 0.0) + (_safe_float(base[4]) or 0.0)

    pcr_now = round(put_now / max(1.0, call_now), 4)
    pcr_base = round(put_base / max(1.0, call_base), 4)
    pcr_chg = round(pcr_now - pcr_base, 4)
    pcr_chg_pct = _pct_change(pcr_now, pcr_base) or 0.0
    call_pct = _pct_change(call_now, call_base) or 0.0
    put_pct = _pct_change(put_now, put_base) or 0.0
    total_pct = _pct_change(total_now, total_base) or 0.0
    vol_pct = _pct_change(vol_now, vol_base) or 0.0

    return {
        "base": base,
        "call_oi_now": call_now, "put_oi_now": put_now, "total_oi_now": total_now,
        "call_oi_base": call_base, "put_oi_base": put_base, "total_oi_base": total_base,
        "call_oi_pct": call_pct, "put_oi_pct": put_pct, "oi_pct": total_pct,
        "vol_pct": vol_pct, "pcr_base": pcr_base, "pcr_now": pcr_now,
        "pcr_chg": pcr_chg, "pcr_chg_pct": pcr_chg_pct,
        "side_edge": round(put_pct - call_pct, 2),
    }


def _price_delta(points: Sequence[Tuple[str, float]], days: int) -> float:
    """Points are oldest-first tuples: (date, price_proxy)."""
    if not points:
        return 0.0
    idx = max(0, len(points) - 1 - max(1, int(days)))
    cur = _safe_float(points[-1][1], 0.0) or 0.0
    base = _safe_float(points[idx][1], 0.0) or 0.0
    return round((cur - base) / max(0.01, abs(base)) * 100.0, 2) if base else 0.0


# ---------------------------------------------------------------------------
# Max pain and skew analytics
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot: float, strike: float, years: float, vol: float, is_call: bool, rate: float = 0.04) -> Optional[float]:
    try:
        if spot <= 0 or strike <= 0 or years <= 0 or vol <= 0:
            return None
        sig_sqrt = vol * math.sqrt(years)
        d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / sig_sqrt
        d2 = d1 - sig_sqrt
        disc = math.exp(-rate * years)
        if is_call:
            return spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
        return strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    except Exception:
        return None


def _implied_vol(price: Any, spot: Any, strike: Any, snap_date: str, expiration: str, opt_type: str) -> Optional[float]:
    px = _safe_float(price, None)
    s = _safe_float(spot, None)
    k = _safe_float(strike, None)
    if px is None or s is None or k is None or px <= 0 or s <= 0 or k <= 0:
        return None
    try:
        days = max(1, (date.fromisoformat(str(expiration)[:10]) - date.fromisoformat(str(snap_date)[:10])).days)
    except Exception:
        days = 7
    years = max(days / 365.0, 1.0 / 365.0)
    is_call = str(opt_type or "").lower().startswith("c")
    intrinsic = max(0.0, s - k) if is_call else max(0.0, k - s)
    if px < intrinsic * 0.98:
        return None
    lo, hi = 0.01, 5.0
    best = None
    for _ in range(50):
        mid = (lo + hi) / 2.0
        val = _bs_price(s, k, years, mid, is_call)
        if val is None:
            return None
        best = mid
        if val > px:
            hi = mid
        else:
            lo = mid
    return best if best and math.isfinite(best) else None


def _row_get(r: Any, key: str, default: Any = None) -> Any:
    try:
        return r[key]
    except Exception:
        try:
            return r.get(key, default)
        except Exception:
            return default


def _compute_max_pain(rows: Sequence[Any]) -> Optional[float]:
    """Compute max pain from strike-level call/put OI for one snapshot."""
    by_strike: Dict[float, Dict[str, float]] = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    for r in rows or []:
        k = _safe_float(_row_get(r, "strike"), None)
        if k is None:
            continue
        typ = "call" if str(_row_get(r, "type") or "").lower().startswith("c") else "put"
        by_strike[k][typ] += _safe_float(_row_get(r, "oi"), 0.0) or 0.0
    strikes = sorted(by_strike.keys())
    if not strikes:
        return None
    best_k = None
    best_pain = None
    for s in strikes:
        pain = 0.0
        for k, vals in by_strike.items():
            pain += vals["call"] * max(0.0, s - k)
            pain += vals["put"] * max(0.0, k - s)
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best_k = s
    return round(float(best_k), 2) if best_k is not None else None


def _chain_step(strikes: Sequence[float]) -> float:
    vals = sorted({float(x) for x in strikes if x is not None})
    diffs = [round(vals[i + 1] - vals[i], 4) for i in range(len(vals) - 1) if vals[i + 1] > vals[i]]
    if not diffs:
        return 1.0
    diffs.sort()
    return float(diffs[len(diffs) // 2]) or 1.0


def _choose_seller_walls(rows: Sequence[Any], spot: Optional[float]) -> Dict[str, Any]:
    """Pick nearby high-OI put support and call resistance strikes from one chain snapshot.

    The scanner is seller-flow oriented, so these are not trade recommendations by
    themselves.  They provide readable strike anchors for the UI: short strike near
    the highest-quality support/resistance wall, long strike one chain step farther
    away where available.
    """
    out: Dict[str, Any] = {
        "put_wall_strike": None, "put_wall_oi": None, "put_long_strike": None,
        "call_wall_strike": None, "call_wall_oi": None, "call_long_strike": None,
        "strike_step": None,
    }
    strikes = sorted({_safe_float(_row_get(r, "strike"), None) for r in rows or [] if _safe_float(_row_get(r, "strike"), None) is not None})
    if not strikes:
        return out
    step = _chain_step(strikes)
    out["strike_step"] = step
    sp = _safe_float(spot, None)
    if sp is None or sp <= 0:
        sp = strikes[len(strikes) // 2]

    def _wall(opt_type: str, side: str) -> Optional[Any]:
        pool = []
        for r in rows or []:
            typ = "call" if str(_row_get(r, "type") or "").lower().startswith("c") else "put"
            if typ != opt_type:
                continue
            k = _safe_float(_row_get(r, "strike"), None)
            oi = _safe_float(_row_get(r, "oi"), 0.0) or 0.0
            if k is None or oi <= 0:
                continue
            if side == "put" and k > sp:
                continue
            if side == "call" and k < sp:
                continue
            dist_pct = abs(k - sp) / max(0.01, sp) * 100.0
            # Keep the anchor actionable and nearby.  If the chain is sparse this
            # still allows farther strikes, but strongly penalizes distance.
            score = oi / (1.0 + dist_pct * 0.35)
            pool.append((score, oi, -dist_pct, k, r))
        if not pool:
            for r in rows or []:
                typ = "call" if str(_row_get(r, "type") or "").lower().startswith("c") else "put"
                if typ != opt_type:
                    continue
                k = _safe_float(_row_get(r, "strike"), None)
                oi = _safe_float(_row_get(r, "oi"), 0.0) or 0.0
                if k is not None and oi > 0:
                    dist_pct = abs(k - sp) / max(0.01, sp) * 100.0
                    score = oi / (1.0 + dist_pct * 0.5)
                    pool.append((score, oi, -dist_pct, k, r))
        if not pool:
            return None
        # Keep displayed support/resistance actionable.  Massive deep OI walls are
        # useful context, but they should not become the card's primary support or
        # suggested short strike when there are near-price walls available.
        near = [x for x in pool if abs(float(x[3]) - sp) / max(0.01, sp) * 100.0 <= 10.0]
        use_pool = near if near else pool
        return max(use_pool, key=lambda x: (x[0], x[1]))

    put = _wall("put", "put")
    call = _wall("call", "call")
    if put:
        k = float(put[3])
        out["put_wall_strike"] = round(k, 2)
        out["put_wall_oi"] = int(put[1])
        lowers = [x for x in strikes if x < k]
        out["put_long_strike"] = round(lowers[-1], 2) if lowers else round(max(0.01, k - step), 2)
    if call:
        k = float(call[3])
        out["call_wall_strike"] = round(k, 2)
        out["call_wall_oi"] = int(call[1])
        uppers = [x for x in strikes if x > k]
        out["call_long_strike"] = round(uppers[0], 2) if uppers else round(k + step, 2)
    return out


def _infer_spot(rows: Sequence[Any], sym: str, snap_date: str, price_cache: Dict[Tuple[str, str], float]) -> Optional[float]:
    """Infer the underlying spot from real price sources only.

    Previous builds fell back to an OI-weighted strike proxy.  That made SPY show
    values like 560 when the actual underlying was near 747.  OI-weighted strike is
    useful as a wall/magnet context but must not be displayed as the underlying
    price or used to classify nearby walls.
    """
    sym = str(sym or "").upper()
    # Prefer actual bar/quote cache.  Exact snapshot-date close first, then latest
    # cached close for current-display/actionable wall selection.
    pc = price_cache.get((sym, str(snap_date or "")[:10]))
    pcv = _safe_float(pc, None)
    if pcv is not None and pcv > 0:
        return round(float(pcv), 4)
    latest = _latest_close_from_cache(price_cache, sym)
    if latest is not None and latest > 0:
        return round(float(latest), 4)

    # Use an explicit underlying column only when no price_cache is available.
    # Reject clearly impossible values versus the listed strike range.
    strikes = sorted([_safe_float(_row_get(r, "strike"), None) for r in rows if _safe_float(_row_get(r, "strike"), None) is not None])
    vals = [_safe_float(_row_get(r, "underlying"), None) for r in rows]
    vals = [v for v in vals if v and v > 0]
    if vals:
        u = float(sorted(vals)[len(vals) // 2])
        if strikes:
            lo, hi = min(strikes), max(strikes)
            # Underlying should usually sit reasonably near the visible chain.
            if lo * 0.55 <= u <= hi * 1.45:
                return round(u, 4)
        else:
            return round(u, 4)

    # No real price source available.  Return None so the UI shows n/a instead of
    # an invented OI-weighted strike masquerading as price.
    return None



def _normalise_iv(raw: Any) -> Optional[float]:
    iv = _safe_float(raw, None)
    if iv is None or iv <= 0:
        return None
    # yfinance stores IV as a decimal; some feeds store percentages.
    if iv > 3.0:
        iv = iv / 100.0
    return iv if 0.005 <= iv <= 5.0 else None


def _compute_skew_snapshot(rows: Sequence[Any], sym: str, snap_date: str, price_cache: Dict[Tuple[str, str], float]) -> Dict[str, Any]:
    rows = list(rows or [])
    if not rows:
        return {}
    exp = str(_row_get(rows[0], "expiration") or "")[:10]
    spot = _infer_spot(rows, sym, snap_date, price_cache)
    max_pain = _compute_max_pain(rows)
    calls = [r for r in rows if str(_row_get(r, "type") or "").lower().startswith("c")]
    puts = [r for r in rows if str(_row_get(r, "type") or "").lower().startswith("p")]
    call_oi = sum(_safe_float(_row_get(r, "oi"), 0.0) or 0.0 for r in calls)
    put_oi = sum(_safe_float(_row_get(r, "oi"), 0.0) or 0.0 for r in puts)
    total_oi = call_oi + put_oi
    walls = _choose_seller_walls(rows, spot)

    iv_points: List[Dict[str, Any]] = []
    source_priority = ""
    if spot and exp:
        for r in rows:
            k = _safe_float(_row_get(r, "strike"), None)
            if k is None:
                continue
            typ = "call" if str(_row_get(r, "type") or "").lower().startswith("c") else "put"
            iv = _normalise_iv(_row_get(r, "iv"))
            source = "iv" if iv is not None else ""
            if iv is None:
                iv = _implied_vol(_row_get(r, "price"), spot, k, snap_date, exp, typ)
                source = "price_iv" if iv is not None else ""
            if iv is not None:
                iv_points.append({"type": typ, "strike": k, "iv": iv})
                if source == "iv":
                    source_priority = "iv"
                elif source == "price_iv" and not source_priority:
                    source_priority = "price_iv"

    put_skew = call_skew = risk_reversal = None
    current_iv = None
    skew_source = source_priority or ""
    if spot and len(iv_points) >= 1:
        atm_any = min(iv_points, key=lambda x: abs(float(x["strike"]) - float(spot)))
        current_iv = round(float(atm_any.get("iv") or 0.0) * 100.0, 2) if atm_any.get("iv") else None
    if spot and len(iv_points) >= 3:
        atm = min(iv_points, key=lambda x: abs(float(x["strike"]) - float(spot)))
        put_pool = [x for x in iv_points if x["type"] == "put" and x["strike"] <= spot] or [x for x in iv_points if x["type"] == "put"]
        call_pool = [x for x in iv_points if x["type"] == "call" and x["strike"] >= spot] or [x for x in iv_points if x["type"] == "call"]
        if atm and put_pool and call_pool:
            # 5% OTM approximates a stable skew node better than far OTM noise in retail feeds.
            put = min(put_pool, key=lambda x: abs(float(x["strike"]) - float(spot) * 0.95))
            call = min(call_pool, key=lambda x: abs(float(x["strike"]) - float(spot) * 1.05))
            put_skew = round((float(put["iv"]) - float(atm["iv"])) * 100.0, 2)
            call_skew = round((float(call["iv"]) - float(atm["iv"])) * 100.0, 2)
            risk_reversal = round((float(call["iv"]) - float(put["iv"])) * 100.0, 2)
    if risk_reversal is None:
        # Explicitly labelled proxy: this is NOT volatility skew.  It measures
        # whether OTM OI is concentrated on the put/support side or call/resistance side.
        if spot:
            put_otm = sum(_safe_float(_row_get(r, "oi"), 0.0) or 0.0 for r in puts if (_safe_float(_row_get(r, "strike"), 0.0) or 0.0) < spot)
            call_otm = sum(_safe_float(_row_get(r, "oi"), 0.0) or 0.0 for r in calls if (_safe_float(_row_get(r, "strike"), 0.0) or 0.0) > spot)
        else:
            put_otm = put_oi
            call_otm = call_oi
        otm_total = put_otm + call_otm
        if otm_total > 0:
            risk_reversal = round((call_otm - put_otm) / otm_total * 100.0, 2)
            put_skew = round(put_otm / otm_total * 100.0 - 50.0, 2)
            call_skew = round(call_otm / otm_total * 100.0 - 50.0, 2)
            skew_source = "oi_proxy"

    return {
        "date": snap_date,
        "expiration": exp,
        "spot": spot,
        "max_pain": max_pain,
        "call_oi": int(call_oi),
        "put_oi": int(put_oi),
        "total_oi": int(total_oi),
        "pcr": round(put_oi / max(1.0, call_oi), 3),
        "put_skew": put_skew,
        "call_skew": call_skew,
        "risk_reversal": risk_reversal,
        "current_iv": current_iv,
        "iv_point_count": len(iv_points),
        "skew_source": skew_source or "unavailable",
        "skew_is_proxy": 1 if skew_source == "oi_proxy" else 0,
        **walls,
    }


def _market_data_db_path() -> Optional[Path]:
    """Return optional long-history OHLCV DB used by Scanner Builder/backtests."""
    raw = os.environ.get("MARKET_DATA_DB") or os.environ.get("BACKTEST_DB_PATH")
    path = Path(raw) if raw else Path(__file__).resolve().parents[2] / "data" / "market_data.db"
    try:
        return path if path.exists() else None
    except Exception:
        return None


def _load_market_bars_rows(symbols: Sequence[str], cutoff: str) -> List[Dict[str, Any]]:
    """Load historical OHLCV from data/market_data.db.market_bars when available.

    Seller Flow used to read only options_data.price_cache.  Many installs keep
    the richer daily history in data/market_data.db, so UAE/price-action/IV-rank
    showed unavailable even though Scanner Builder had enough bars.  This mirrors
    Scanner Builder's local-history source without making live provider calls.
    """
    syms = sorted({str(s or "").strip().upper() for s in symbols if str(s or "").strip()})
    dbp = _market_data_db_path()
    if not syms or dbp is None:
        return []
    ph = ",".join(["?"] * len(syms))
    try:
        c = sqlite3.connect(str(dbp), timeout=10)
        c.row_factory = sqlite3.Row
        try:
            if not _table_exists(c, "market_bars"):
                return []
            rows = c.execute(
                f"""
                SELECT symbol, substr(bar_time,1,10) AS date, open, high, low, close, volume
                FROM market_bars
                WHERE symbol IN ({ph})
                  AND interval IN ('1d','D','daily')
                  AND substr(bar_time,1,10) >= ?
                  AND close IS NOT NULL AND close>0
                ORDER BY symbol, substr(bar_time,1,10) ASC
                """,
                syms + [cutoff],
            ).fetchall()
        finally:
            c.close()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        sym = str(r["symbol"] or "").upper()
        dt = str(r["date"] or "")[:10]
        close = _safe_float(r["close"], None)
        if not sym or not dt or close is None or close <= 0:
            continue
        op = _safe_float(r["open"], close) or close
        hi = _safe_float(r["high"], max(op, close)) or max(op, close)
        lo = _safe_float(r["low"], min(op, close)) or min(op, close)
        out.append({
            "symbol": sym, "date": dt,
            "open": float(op), "high": float(max(hi, op, close)),
            "low": float(min(lo, op, close)), "close": float(close),
            "volume": _safe_float(r["volume"], 0.0) or 0.0,
            "source": "market_bars",
        })
    return out


def _load_option_underlying_bars(con: sqlite3.Connection, symbols: Sequence[str], cutoff: str) -> List[Dict[str, Any]]:
    """Build a light daily close series from stored option-chain underlying values.

    Many installations have rich strike-level OI snapshots but no separate OHLCV
    history in ``price_cache`` or ``data/market_data.db``.  The options table often
    still stores the underlying price used at fetch time.  That is enough to avoid
    blank Price/UAE/IVR context and to compute close-based indicators.  High/low are
    set from the available underlying samples on that date; if only one sample is
    present, open/high/low/close are the same, so ADX/BB-KC style fields are marked
    as fallback context rather than live chart-grade OHLC.
    """
    syms = sorted({str(s or "").strip().upper() for s in symbols if str(s or "").strip()})
    if not syms or not _table_exists(con, "options"):
        return []
    cols = _table_columns(con, "options")
    if "underlying" not in cols:
        return []
    ph = ",".join(["?"] * len(syms))
    try:
        rows = con.execute(
            f"""
            SELECT symbol, substr(date,1,10) AS date,
                   MIN(NULLIF(underlying,0)) AS lo,
                   MAX(NULLIF(underlying,0)) AS hi,
                   AVG(NULLIF(underlying,0)) AS close,
                   COUNT(NULLIF(underlying,0)) AS samples
            FROM options
            WHERE symbol IN ({ph})
              AND substr(date,1,10) >= ?
              AND underlying IS NOT NULL AND underlying>0
            GROUP BY symbol, substr(date,1,10)
            ORDER BY symbol, substr(date,1,10)
            """,
            list(syms) + [cutoff],
        ).fetchall()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        sym = str(r["symbol"] or "").upper()
        dt = str(r["date"] or "")[:10]
        close = _safe_float(r["close"], None)
        if not sym or not dt or close is None or close <= 0:
            continue
        lo = _safe_float(r["lo"], close) or close
        hi = _safe_float(r["hi"], close) or close
        out.append({
            "symbol": sym,
            "date": dt,
            "open": float(close),
            "high": float(max(hi, close)),
            "low": float(min(lo, close)),
            "close": float(close),
            "volume": 0.0,
            "source": "options_underlying",
            "samples": _safe_int(r["samples"], 0),
        })
    return out


def _compute_iv_rank_from_series(values: Sequence[float], source: str = "option_iv_history") -> Dict[str, Any]:
    """Return a stable 0-100 IV-rank style read from stored IV/HV observations.

    A true IV Rank needs a history of IV observations.  Older builds returned
    ``None`` when history was short, or extreme 0/100 readings from only a couple
    of snapshots.  For the Seller Flow card that was not useful.  This version is
    quality-aware:
      * >=10 observations: normal IV rank.
      * 2-9 observations: rank is blended toward 50 so a tiny sample does not
        falsely show 0/100.
      * 1 observation: display a neutral 50 "current IV only" context, clearly
        labelled as an estimate.
    """
    xs = [_safe_float(v, None) for v in values]
    xs = [float(x) for x in xs if x is not None and x > 0]
    if not xs:
        return {
            "iv_rank": None, "iv_percentile": None, "iv_regime": "IV n/a",
            "should_buy_sell": "WAIT", "premium_preference": "WAIT",
            "iv_rank_source": source, "iv_note": "No usable IV/HV observations were available.",
            "iv_history_points": 0, "iv_rank_estimated": 0,
        }
    cur = xs[-1]
    if len(xs) == 1:
        rank = 50.0
        pct = 50.0
        regime = "Current IV only"
        pref = "MIXED"
        note = (
            f"Only one IV observation is stored ({cur:.1f}%). Showing neutral IVR 50 as a rough placeholder; "
            "refresh more option snapshots before relying on IV rank."
        )
        return {
            "iv_rank": rank, "iv_percentile": pct, "current_iv": round(cur, 2),
            "iv_regime": regime, "iv_trend": "n/a", "premium_preference": pref,
            "should_buy_sell": pref, "iv_rank_source": source + "_current_only",
            "iv_note": note, "iv_history_points": 1, "iv_rank_estimated": 1,
        }

    lo, hi = min(xs), max(xs)
    raw_rank = 50.0 if hi <= lo + 1e-9 else (cur - lo) / (hi - lo) * 100.0
    raw_pct = sum(1 for x in xs if x < cur) / len(xs) * 100.0
    # Short histories should not generate overconfident 0/100 rank readings.
    if len(xs) < 10:
        weight = max(0.2, len(xs) / 10.0)
        rank = 50.0 * (1.0 - weight) + raw_rank * weight
        pct = 50.0 * (1.0 - weight) + raw_pct * weight
        estimated = 1
    else:
        rank = raw_rank
        pct = raw_pct
        estimated = 0
    rank = round(max(0.0, min(100.0, rank)), 1)
    pct = round(max(0.0, min(100.0, pct)), 1)
    if rank >= 70:
        regime, pref = "High IV", "SELL PREMIUM"
        note = "Option IV rank is elevated; premium-selling structures are preferred when price/sector confirm."
    elif rank >= 40:
        regime, pref = "Moderate IV", "MIXED"
        note = "Option IV rank is moderate; choose credit/debit based on seller-flow and price confirmation."
    else:
        regime, pref = "Low IV", "BUY OPTIONS"
        note = "Option IV rank is compressed; debit structures or waiting for better credit are preferred."
    if len(xs) < 10:
        note += f" Short IV history ({len(xs)} snapshots); rank is blended toward 50 and should be treated as an estimate."
    trend = "Stable"
    if len(xs) >= 10:
        recent = sum(xs[-5:]) / 5.0
        prior = sum(xs[-10:-5]) / 5.0
        trend = "Rising" if recent > prior * 1.05 else "Falling" if recent < prior * 0.95 else "Stable"
    return {
        "iv_rank": rank,
        "iv_percentile": pct,
        "current_iv": round(cur, 2),
        "iv_regime": regime,
        "iv_trend": trend,
        "premium_preference": pref,
        "should_buy_sell": pref,
        "iv_rank_source": source,
        "iv_note": note,
        "iv_history_points": len(xs),
        "iv_rank_estimated": estimated,
    }

def _load_option_iv_rank_contexts(con: sqlite3.Connection, symbols: Sequence[str], cutoff: str) -> Dict[str, Dict[str, Any]]:
    """Compute IV rank from historical option IV snapshots when available.

    This is better than the historical-volatility proxy for the Seller Flow card.
    It uses the IV closest to spot for each snapshot when the underlying value is
    stored; otherwise it uses an OI-weighted median-ish IV across the chain.
    """
    syms = sorted({str(s or "").strip().upper() for s in symbols if str(s or "").strip()})
    if not syms or not _table_exists(con, "options"):
        return {}
    cols = _table_columns(con, "options")
    if "iv" not in cols:
        return {}
    has_underlying = "underlying" in cols
    ph = ",".join(["?"] * len(syms))
    try:
        rows = con.execute(
            f"""
            SELECT symbol, substr(date,1,10) AS date, LOWER(type) AS type, strike,
                   iv, oi, {('underlying' if has_underlying else 'NULL AS underlying')}
            FROM options
            WHERE symbol IN ({ph})
              AND substr(date,1,10) >= ?
              AND iv IS NOT NULL AND iv>0
            ORDER BY symbol, substr(date,1,10), strike
            """,
            list(syms) + [cutoff],
        ).fetchall()
    except Exception:
        return {}
    grouped: Dict[Tuple[str, str], List[Any]] = defaultdict(list)
    for r in rows:
        sym = str(r["symbol"] or "").upper()
        dt = str(r["date"] or "")[:10]
        if sym and dt:
            grouped[(sym, dt)].append(r)
    series_by_sym: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for (sym, dt), rs in grouped.items():
        pts: List[Tuple[float, float, float]] = []  # distance, iv_pct, weight
        spots = [_safe_float(_row_get(r, "underlying"), None) for r in rs]
        spots = [x for x in spots if x is not None and x > 0]
        spot = sorted(spots)[len(spots)//2] if spots else None
        for r in rs:
            iv = _normalise_iv(_row_get(r, "iv"))
            if iv is None:
                continue
            iv_pct = float(iv) * 100.0
            k = _safe_float(_row_get(r, "strike"), None)
            oi = max(1.0, _safe_float(_row_get(r, "oi"), 0.0) or 0.0)
            dist = abs((k or 0.0) - spot) / max(0.01, spot) if spot and k else 0.0
            pts.append((dist, iv_pct, oi))
        if not pts:
            continue
        if spot:
            near = sorted(pts, key=lambda x: x[0])[:8]
        else:
            near = pts
        wsum = sum(x[2] for x in near) or float(len(near))
        iv_val = sum(x[1] * x[2] for x in near) / wsum
        series_by_sym[sym].append((dt, iv_val))
    out: Dict[str, Dict[str, Any]] = {}
    for sym, pairs in series_by_sym.items():
        pairs.sort(key=lambda x: x[0])
        ctx = _compute_iv_rank_from_series([x[1] for x in pairs], "option_iv_history")
        ctx["iv_history_points"] = len(pairs)
        ctx["iv_asof"] = pairs[-1][0] if pairs else None
        out[sym] = ctx
    return out


def _load_price_cache(con: sqlite3.Connection, symbols: Sequence[str], cutoff: str) -> Dict[Tuple[str, str], float]:
    """Load actual close/spot values from price_cache.

    Keys:
      (SYMBOL, date)       exact cached close for a bar date
      (SYMBOL, "__latest__") latest cached close, used for display/current context

    The Seller Flow screen must never use OI-weighted strike as spot.  OI-weighted
    strike is a wall/magnet proxy, not the underlying price.
    """
    if not symbols:
        return {}
    ph = ",".join(["?"] * len(symbols))
    out: Dict[Tuple[str, str], float] = {}
    try:
        rows = []
        has_price_cache = _table_exists(con, "price_cache")
        if has_price_cache:
            rows = con.execute(
                f"SELECT symbol, date, close FROM price_cache WHERE date>=? AND symbol IN ({ph}) AND close IS NOT NULL AND close>0 ORDER BY symbol, date",
                [cutoff] + list(symbols),
            ).fetchall()
        latest_by_sym: Dict[str, Tuple[str, float]] = {}
        for r in rows:
            sym = str(r["symbol"] or "").upper()
            dt = str(r["date"] or "")[:10]
            close = _safe_float(r["close"], None)
            if not sym or not dt or close is None or close <= 0:
                continue
            out[(sym, dt)] = float(close)
            latest_by_sym[sym] = (dt, float(close))
        # If cutoff omitted a weekend/current cached quote issue, still grab latest rows.
        missing = [s for s in symbols if (str(s).upper(), "__latest__") not in out and str(s).upper() not in latest_by_sym]
        if missing and has_price_cache:
            ph2 = ",".join(["?"] * len(missing))
            more = con.execute(
                f"""
                SELECT p.symbol, p.date, p.close
                FROM price_cache p
                JOIN (SELECT symbol, MAX(date) AS max_date FROM price_cache WHERE symbol IN ({ph2}) AND close IS NOT NULL AND close>0 GROUP BY symbol) m
                  ON m.symbol=p.symbol AND m.max_date=p.date
                """,
                list(missing),
            ).fetchall()
            for r in more:
                sym = str(r["symbol"] or "").upper()
                dt = str(r["date"] or "")[:10]
                close = _safe_float(r["close"], None)
                if sym and close is not None and close > 0:
                    latest_by_sym[sym] = (dt, float(close))
                    out[(sym, dt)] = float(close)
        # Option snapshots often contain the underlying price even when price_cache
        # is missing.  Use that as a real spot fallback, before giving up.
        try:
            for ob in _load_option_underlying_bars(con, symbols, cutoff):
                sym = str(ob.get("symbol") or "").upper()
                dt = str(ob.get("date") or "")[:10]
                close = _safe_float(ob.get("close"), None)
                if not sym or not dt or close is None or close <= 0:
                    continue
                out.setdefault((sym, dt), float(close))
                cur = latest_by_sym.get(sym)
                if cur is None or str(dt) >= str(cur[0]):
                    latest_by_sym[sym] = (dt, float(close))
        except Exception:
            pass
        # Also consult Scanner Builder/backtest market_bars DB.  That store often has
        # the freshest/longer OHLCV history and keeps Seller Flow aligned with Scanner Builder.
        try:
            for mb in _load_market_bars_rows(symbols, cutoff):
                sym = str(mb.get("symbol") or "").upper()
                dt = str(mb.get("date") or "")[:10]
                close = _safe_float(mb.get("close"), None)
                if not sym or not dt or close is None or close <= 0:
                    continue
                out[(sym, dt)] = float(close)
                cur = latest_by_sym.get(sym)
                if cur is None or str(dt) >= str(cur[0]):
                    latest_by_sym[sym] = (dt, float(close))
        except Exception:
            pass
        for sym, (dt, close) in latest_by_sym.items():
            out[(sym, "__latest__")] = close
            out[(sym, "__latest_date__")] = dt  # type: ignore[assignment]
    except Exception:
        return out
    return out


def _load_price_points(con: sqlite3.Connection, symbols: Sequence[str], cutoff: str) -> Dict[str, List[Tuple[str, float]]]:
    """Return actual close history for price-context deltas, oldest-first."""
    pc = _load_price_cache(con, symbols, cutoff)
    pts: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for (sym, dt), val in pc.items():
        if str(dt).startswith("__"):
            continue
        close = _safe_float(val, None)
        if close is not None and close > 0:
            pts[str(sym).upper()].append((str(dt), float(close)))
    for sym in list(pts.keys()):
        pts[sym].sort(key=lambda x: x[0])
    return pts


# ---------------------------------------------------------------------------
# Price action, IV-rank and sector enrichment
# ---------------------------------------------------------------------------

SECTOR_ETF_BY_NAME = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Health Care": "XLV",
    "Financials": "XLF",
    "Financial Services": "XLF",
    "Energy": "XLE",
    "Consumer Discretionary": "XLY",
    "Consumer Cyclical": "XLY",
    "Consumer Staples": "XLP",
    "Consumer Defensive": "XLP",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Basic Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}
SECTOR_NAME_BY_ETF = {v: k for k, v in SECTOR_ETF_BY_NAME.items() if v.startswith("XL")}
SECTOR_OVERRIDES = {
    # Small fallback list for common non-US/ADR/commodity names that yfinance sector
    # cache may miss.  The cache still wins when present.
    "SU": "Energy", "BP": "Energy", "SHEL": "Energy", "RIO": "Materials", "TECK": "Materials",
    "BABA": "Consumer Cyclical", "BIDU": "Communication Services", "ASML": "Technology", "TSM": "Technology",
    "TEVA": "Healthcare", "GSK": "Healthcare", "DB": "Financials", "HSBC": "Financials",
}


def _load_sector_map(con: sqlite3.Connection, symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    syms = sorted({str(s or "").strip().upper() for s in symbols if str(s or "").strip()})
    if not syms:
        return out
    cached: Dict[str, str] = {}
    if _table_exists(con, "sector_cache"):
        try:
            ph = ",".join(["?"] * len(syms))
            rows = con.execute(f"SELECT symbol, sector FROM sector_cache WHERE symbol IN ({ph})", syms).fetchall()
            cached = {str(r["symbol"] or "").upper(): str(r["sector"] or "").strip() for r in rows if str(r["symbol"] or "").strip()}
        except Exception:
            cached = {}
    for sym in syms:
        sector = cached.get(sym) or SECTOR_NAME_BY_ETF.get(sym) or SECTOR_OVERRIDES.get(sym) or ""
        etf = SECTOR_ETF_BY_NAME.get(sector or "")
        if sym in SECTOR_NAME_BY_ETF:
            etf = sym
            sector = SECTOR_NAME_BY_ETF.get(sym) or sector
        out[sym] = {
            "sector": sector or "",
            "sector_etf": etf or "",
            "sector_source": "sector_cache" if cached.get(sym) else ("symbol_etf" if sym in SECTOR_NAME_BY_ETF else ("fallback_map" if SECTOR_OVERRIDES.get(sym) else "unavailable")),
        }
    return out


def _load_price_bars(con: sqlite3.Connection, symbols: Sequence[str], cutoff: str) -> Dict[str, List[Dict[str, Any]]]:
    syms = sorted({str(s or "").strip().upper() for s in symbols if str(s or "").strip()})
    if not syms:
        return {}
    row_map: Dict[Tuple[str, str], Dict[str, Any]] = {}

    if _table_exists(con, "price_cache"):
        ph = ",".join(["?"] * len(syms))
        try:
            rows = con.execute(
                f"""
                SELECT symbol, date, open, high, low, close, volume
                FROM price_cache
                WHERE date>=? AND symbol IN ({ph}) AND close IS NOT NULL AND close>0
                ORDER BY symbol, date ASC
                """,
                [cutoff] + syms,
            ).fetchall()
        except Exception:
            rows = []
        for r in rows:
            sym = str(r["symbol"] or "").upper()
            dt = str(r["date"] or "")[:10]
            close = _safe_float(r["close"], None)
            if not sym or not dt or close is None or close <= 0:
                continue
            op = _safe_float(r["open"], close) or close
            hi = _safe_float(r["high"], max(op, close)) or max(op, close)
            lo = _safe_float(r["low"], min(op, close)) or min(op, close)
            row_map[(sym, dt)] = {
                "date": dt,
                "open": float(op), "high": float(max(hi, op, close)), "low": float(min(lo, op, close)),
                "close": float(close), "volume": _safe_float(r["volume"], 0.0) or 0.0,
                "source": "price_cache",
            }

    # Fall back to the underlying price stored with option-chain snapshots.
    # Do not override a real price_cache/market_bars OHLC row; use it only to
    # prevent Price/UAE/IVR context from being blank for option-only symbols.
    for ob in _load_option_underlying_bars(con, syms, cutoff):
        sym = str(ob.get("symbol") or "").upper()
        dt = str(ob.get("date") or "")[:10]
        if sym and dt and (sym, dt) not in row_map:
            row_map[(sym, dt)] = {k: v for k, v in ob.items() if k != "symbol"}

    # Merge richer long-history market_bars rows.  They intentionally override
    # price_cache/underlying fallback for the same symbol/date when available
    # because market_bars is the Scanner Builder/backtest source used for UAE validation.
    for mb in _load_market_bars_rows(syms, cutoff):
        sym = str(mb.get("symbol") or "").upper()
        dt = str(mb.get("date") or "")[:10]
        if sym and dt:
            row_map[(sym, dt)] = {k: v for k, v in mb.items() if k != "symbol"}

    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for (sym, _dt), row in row_map.items():
        out[sym].append(row)
    for sym in list(out.keys()):
        out[sym].sort(key=lambda x: str(x.get("date") or ""))
    return dict(out)


def _resample_weekly_bars(bars: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for b in bars or []:
        try:
            d = date.fromisoformat(str(b.get("date") or "")[:10])
            key = f"{d.isocalendar().year}-{d.isocalendar().week:02d}"
            groups[key].append(b)
        except Exception:
            continue
    out: List[Dict[str, Any]] = []
    for key in sorted(groups.keys()):
        g = groups[key]
        if not g:
            continue
        out.append({
            "date": str(g[-1].get("date") or ""),
            "open": _safe_float(g[0].get("open"), g[0].get("close")) or 0.0,
            "high": max(_safe_float(x.get("high"), x.get("close")) or 0.0 for x in g),
            "low": min(_safe_float(x.get("low"), x.get("close")) or 0.0 for x in g),
            "close": _safe_float(g[-1].get("close"), 0.0) or 0.0,
            "volume": sum(_safe_float(x.get("volume"), 0.0) or 0.0 for x in g),
        })
    return [x for x in out if x.get("close")]


def _ema_values(values: Sequence[float], period: int) -> List[Optional[float]]:
    vals = [_safe_float(v, None) for v in values]
    out: List[Optional[float]] = []
    alpha = 2.0 / (max(1, int(period)) + 1.0)
    prev: Optional[float] = None
    for v in vals:
        if v is None:
            out.append(prev)
            continue
        prev = float(v) if prev is None else prev + alpha * (float(v) - prev)
        out.append(prev)
    return out


def _rma_values(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    prev: Optional[float] = None
    p = max(1, int(period))
    for v in values:
        x = _safe_float(v, None)
        if x is None:
            out.append(prev)
            continue
        prev = float(x) if prev is None else (prev * (p - 1) + float(x)) / p
        out.append(prev)
    return out


def _rsi_values(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    if not closes:
        return []
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(closes)):
        ch = float(closes[i]) - float(closes[i - 1])
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = _rma_values(gains, period)
    avg_l = _rma_values(losses, period)
    out: List[Optional[float]] = []
    for g, l in zip(avg_g, avg_l):
        if g is None or l is None:
            out.append(None)
        elif l <= 1e-12:
            out.append(100.0)
        else:
            rs = g / l
            out.append(100.0 - 100.0 / (1.0 + rs))
    return out


def _stddev(vals: Sequence[float]) -> Optional[float]:
    xs = [_safe_float(v, None) for v in vals]
    xs = [float(x) for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _percentile_value(vals: Sequence[float], pct: float) -> Optional[float]:
    xs = sorted(float(x) for x in vals if _safe_float(x, None) is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * float(pct) / 100.0
    lo = int(math.floor(k)); hi = int(math.ceil(k))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _compute_iv_rank_from_bars(bars: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute HV-rank proxy from local price/underlying history.

    This is a fallback when real option-IV history is unavailable.  It now works
    with shorter local histories too.  The output is explicitly labelled as an
    HV proxy so it is not confused with true option IV Rank.
    """
    closes = [float(b.get("close") or 0.0) for b in (bars or []) if _safe_float(b.get("close"), None)]
    if len(closes) < 25:
        return {
            "iv_rank": None, "iv_percentile": None, "iv_regime": "IV n/a",
            "should_buy_sell": "WAIT", "premium_preference": "WAIT", "iv_rank_source": "hv_proxy",
            "iv_note": "Insufficient price history for HV/IV-rank proxy.",
            "iv_history_points": len(closes), "iv_rank_estimated": 0,
        }
    returns: List[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            returns.append(math.log(closes[i] / closes[i - 1]))
    if len(returns) < 20:
        return {
            "iv_rank": None, "iv_percentile": None, "iv_regime": "IV n/a",
            "should_buy_sell": "WAIT", "premium_preference": "WAIT", "iv_rank_source": "hv_proxy",
            "iv_note": "Not enough returns for HV/IV-rank proxy.",
            "iv_history_points": len(closes), "iv_rank_estimated": 0,
        }
    window = 20 if len(returns) >= 45 else max(8, min(15, len(returns) // 3))
    hvs: List[float] = []
    for i in range(window, len(returns) + 1):
        sd = _stddev(returns[i - window:i])
        if sd is not None:
            hvs.append(sd * math.sqrt(252) * 100.0)
    if len(hvs) < 5:
        return {
            "iv_rank": None, "iv_percentile": None, "iv_regime": "IV n/a",
            "should_buy_sell": "WAIT", "premium_preference": "WAIT", "iv_rank_source": "hv_proxy",
            "iv_note": "Not enough volatility observations for HV/IV-rank proxy.",
            "iv_history_points": len(closes), "iv_rank_estimated": 0,
        }
    return _compute_iv_rank_from_series(hvs, "hv_proxy" if len(hvs) >= 10 else "short_hv_proxy") | {
        "current_hv": round(hvs[-1], 2),
        "iv_window": window,
    }


def _compute_sr_distance_context(bars: Sequence[Dict[str, Any]], prefix: str = "", period: int = 20) -> Dict[str, Any]:
    """Distance from rolling support/resistance, using local cached OHLC.

    Distances are signed percentages of current close:
      * distance_from_support_20_pct: + means price is above support; 0 means at support.
      * distance_from_resistance_20_pct: + means resistance is above price; 0 means at resistance.
    """
    clean: List[Dict[str, Any]] = []
    for b in bars or []:
        c = _safe_float(b.get("close"), None)
        if c is None or c <= 0:
            continue
        clean.append(b)
    if len(clean) < 3:
        return {}
    c = _safe_float(clean[-1].get("close"), None)
    if c is None or c <= 0:
        return {}
    n = max(3, min(int(period or 20), len(clean)))
    win = clean[-n:]
    lows = [_safe_float(x.get("low"), x.get("close")) for x in win]
    highs = [_safe_float(x.get("high"), x.get("close")) for x in win]
    lows = [float(x) for x in lows if x is not None and x > 0]
    highs = [float(x) for x in highs if x is not None and x > 0]
    if not lows or not highs:
        return {}
    support = min(lows)
    resistance = max(highs)
    dist_sup = round((float(c) - support) / max(0.01, float(c)) * 100.0, 2)
    dist_res = round((resistance - float(c)) / max(0.01, float(c)) * 100.0, 2)
    flags: List[str] = []
    if dist_sup <= 1.0:
        flags.append("at/near support")
    elif dist_sup <= 3.0:
        flags.append("near support")
    if dist_res <= 1.0:
        flags.append("at/near resistance")
    elif dist_res <= 3.0:
        flags.append("near resistance")
    location = "; ".join(flags) if flags else "mid-range"
    return {
        prefix + "support_20": round(support, 2),
        prefix + "resistance_20": round(resistance, 2),
        prefix + "distance_from_support_20_pct": dist_sup,
        prefix + "distance_from_resistance_20_pct": dist_res,
        prefix + "near_support_20": bool(dist_sup <= 3.0),
        prefix + "near_resistance_20": bool(dist_res <= 3.0),
        prefix + "price_location": location,
    }

def _compute_price_action_context(bars: Sequence[Dict[str, Any]], prefix: str = "") -> Dict[str, Any]:
    closes = [float(b.get("close") or 0.0) for b in (bars or []) if _safe_float(b.get("close"), None)]
    highs = [float(b.get("high") or b.get("close") or 0.0) for b in (bars or []) if _safe_float(b.get("close"), None)]
    lows = [float(b.get("low") or b.get("close") or 0.0) for b in (bars or []) if _safe_float(b.get("close"), None)]
    if len(closes) < 25:
        sr_short = _compute_sr_distance_context(bars, prefix, 20)
        return {**sr_short, prefix + "price_action_read": "Price n/a", prefix + "price_action_bias": "Neutral", prefix + "price_action_score": 0, prefix + "price_action_note": "Insufficient price history."}
    ema20 = _ema_values(closes, 20)[-1]
    ema50 = _ema_values(closes, 50)[-1] if len(closes) >= 50 else _ema_values(closes, 20)[-1]
    rsi = _rsi_values(closes, 14)
    rsi14 = rsi[-1] if rsi else None
    rsi_ema90 = _ema_values([x if x is not None else 50.0 for x in rsi], 90)[-1] if rsi else None
    rsidiff90 = (rsi14 - rsi_ema90) if rsi14 is not None and rsi_ema90 is not None else None
    c = closes[-1]
    chg5 = _pct_change(c, closes[-6] if len(closes) >= 6 else closes[0])
    chg20 = _pct_change(c, closes[-21] if len(closes) >= 21 else closes[0])
    win20 = closes[-20:]
    mid = sum(win20) / len(win20)
    sd = _stddev(win20) or 0.0
    bb_upper = mid + 2.0 * sd
    bb_lower = mid - 2.0 * sd
    bb_pos = None if bb_upper <= bb_lower else round((c - bb_lower) / (bb_upper - bb_lower) * 100.0, 1)
    bb_width_pct = round((bb_upper - bb_lower) / max(0.01, mid) * 100.0, 2)
    tr_vals = []
    for i in range(len(closes)):
        prev = closes[i - 1] if i else closes[i]
        tr_vals.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    atr20 = _ema_values(tr_vals, 20)[-1]
    kc_mid = ema20 or mid
    kc_upper = kc_mid + 1.5 * (atr20 or 0.0)
    kc_lower = kc_mid - 1.5 * (atr20 or 0.0)
    kc_inside_bb = bool(kc_upper < bb_upper and kc_lower > bb_lower) if sd and atr20 else False
    bb_inside_kc = bool(bb_upper < kc_upper and bb_lower > kc_lower) if sd and atr20 else False
    score = 0
    notes: List[str] = []
    if ema20 and c > ema20:
        score += 20; notes.append("above EMA20")
    elif ema20:
        score -= 20; notes.append("below EMA20")
    if ema20 and ema50 and ema20 > ema50:
        score += 20; notes.append("EMA20>EMA50")
    elif ema20 and ema50:
        score -= 20; notes.append("EMA20<EMA50")
    if rsi14 is not None:
        if rsi14 >= 60:
            score += 15; notes.append(f"RSI {rsi14:.1f} strong")
        elif rsi14 <= 40:
            score -= 15; notes.append(f"RSI {rsi14:.1f} weak")
        else:
            notes.append(f"RSI {rsi14:.1f} neutral")
    if rsidiff90 is not None:
        if rsidiff90 > 5:
            score += 10; notes.append(f"RSIDiff90 {rsidiff90:.1f} supportive")
        elif rsidiff90 < -5:
            score -= 10; notes.append(f"RSIDiff90 {rsidiff90:.1f} bearish")
    if bb_pos is not None:
        if bb_pos >= 92:
            notes.append("near/above upper BB; upside chase risk")
        elif bb_pos <= 8:
            notes.append("near/lower BB; downside exhaustion risk")
    if kc_inside_bb:
        notes.append("KC inside BB = expanded/range-premium context")
    elif bb_inside_kc:
        notes.append("BB inside KC = squeeze/compression")
    bias = "Bullish" if score >= 25 else "Bearish" if score <= -25 else "Neutral"
    read = f"{bias} price action"
    sr_ctx = _compute_sr_distance_context(bars, prefix, 20)
    return {
        **sr_ctx,
        prefix + "price_action_read": read,
        prefix + "price_action_bias": bias,
        prefix + "price_action_score": int(max(-100, min(100, score))),
        prefix + "price_action_note": "; ".join(notes[:6]),
        prefix + "price_change_5d_pct": chg5,
        prefix + "price_change_20d_pct": chg20,
        prefix + "rsi14": None if rsi14 is None else round(rsi14, 2),
        prefix + "rsidiff90": None if rsidiff90 is None else round(rsidiff90, 2),
        prefix + "ema20": None if ema20 is None else round(ema20, 2),
        prefix + "ema50": None if ema50 is None else round(ema50, 2),
        prefix + "bb_position": bb_pos,
        prefix + "bb_width_pct": bb_width_pct,
        prefix + "bb_kc_state": "KC inside BB / expanded" if kc_inside_bb else "BB inside KC / squeeze" if bb_inside_kc else "normal",
    }


def _compute_uae_lite_context(bars: Sequence[Dict[str, Any]], timeframe: str = "1d") -> Dict[str, Any]:
    # Lightweight local fallback using the same Pine v5 definitions at a high level.
    # Used only when Scanner Builder UAE context cannot be loaded.
    if timeframe == "1w":
        bars = _resample_weekly_bars(bars)
    closes = [float(b.get("close") or 0.0) for b in (bars or []) if _safe_float(b.get("close"), None)]
    highs = [float(b.get("high") or b.get("close") or 0.0) for b in (bars or []) if _safe_float(b.get("close"), None)]
    lows = [float(b.get("low") or b.get("close") or 0.0) for b in (bars or []) if _safe_float(b.get("close"), None)]
    min_bars = 24 if timeframe == "1d" else 6
    if len(closes) < min_bars:
        return {}
    is_weekly = timeframe == "1w"
    fast_len, slow_len, signal_len, roc_len, slope_len, adx_thr = (8, 21, 3, 7, 12, 20.0) if is_weekly else (8, 21, 3, 6, 10, 20.0)
    atr_vals = []
    for i in range(len(closes)):
        prev = closes[i-1] if i else closes[i]
        atr_vals.append(max(highs[i]-lows[i], abs(highs[i]-prev), abs(lows[i]-prev)))
    atrv = _rma_values(atr_vals, 14)
    atr_base = _ema_values([x or 0.0 for x in atrv], slow_len)
    slow_ema = _ema_values(closes, slow_len)
    trend_pos = []
    for c, se, ab in zip(closes, slow_ema, atr_base):
        safe = max(float(ab or 0.0), 1e-6)
        trend_pos.append((c - float(se or c)) / safe)
    trend_mom = []
    for i, tp in enumerate(trend_pos):
        prev = trend_pos[i - roc_len] if i >= roc_len else 0.0
        trend_mom.append(tp - prev)
    raw_sig = []
    for i, tm in enumerate(trend_mom):
        safe = max(float(atr_base[i] or 0.0), 1e-6)
        vol_amp = max(0.1, float(atrv[i] or 0.0) / safe)
        raw_sig.append(tm * math.pow(vol_amp, 1.5))
    macd_line = _ema_values(raw_sig, fast_len)
    signal_line = _ema_values([x or 0.0 for x in macd_line], signal_len)
    hist = [(m or 0.0) - (sig or 0.0) for m, sig in zip(macd_line, signal_line)]
    rsi = _rsi_values(closes, 14)
    rsi_ema = _ema_values([x if x is not None else 50.0 for x in rsi], 90)
    rsidiff = [(a - b) if a is not None and b is not None else None for a, b in zip(rsi, rsi_ema)]
    # Simplified ADX for trend gate.
    trs = atr_vals
    plus_dm = [0.0]
    minus_dm = [0.0]
    for i in range(1, len(closes)):
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        plus_dm.append(up if up > dn and up > 0 else 0.0)
        minus_dm.append(dn if dn > up and dn > 0 else 0.0)
    sm_tr = _rma_values(trs, 14)
    sm_p = _rma_values(plus_dm, 14)
    sm_m = _rma_values(minus_dm, 14)
    dx = []
    for tr, p, m in zip(sm_tr, sm_p, sm_m):
        pdi = 100.0 * (p or 0.0) / max(tr or 0.0, 1e-6)
        mdi = 100.0 * (m or 0.0) / max(tr or 0.0, 1e-6)
        dx.append(100.0 * abs(pdi - mdi) / max(pdi + mdi, 1e-6))
    adx = _ema_values([x or 0.0 for x in _rma_values(dx, 14)], 3)
    idx = len(closes) - 1
    slope_len = min(slope_len, idx)
    safe_atr = max(float(atr_base[idx] or 0.0), 1e-6)
    ema_slope = ((slow_ema[idx] or closes[idx]) - (slow_ema[idx - slope_len] or closes[idx - slope_len])) / max(1, slope_len) / safe_atr
    min_slope = 0.002 if is_weekly else 0.001
    is_trending = ((rsidiff[idx] is not None and (rsidiff[idx] > 12 or rsidiff[idx] < -12)) or (adx[idx] or 0.0) > adx_thr)
    if is_trending and ema_slope > min_slope and hist[idx] > 0:
        regime = "BULL"
    elif is_trending and ema_slope > min_slope:
        regime = "WEAK_BULL"
    elif is_trending and ema_slope < -min_slope and hist[idx] < 0:
        regime = "BEAR"
    elif is_trending and ema_slope < -min_slope:
        regime = "WEAK_BEAR"
    else:
        regime = "SIDEWAYS"
    return {
        "regime": regime,
        "score": 80 if regime in {"BULL", "BEAR"} else 60 if regime in {"WEAK_BULL", "WEAK_BEAR"} else 45,
        "rsidiff": None if rsidiff[idx] is None else round(float(rsidiff[idx]), 2),
        "hist": round(float(hist[idx]), 4),
        "adx": None if adx[idx] is None else round(float(adx[idx]), 2),
        "marker": "",
        "marker_age": None,
        "marker_label": "No recent marker",
    }


def _load_context_enrichments(con: sqlite3.Connection, symbols: Sequence[str], sector_map: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    syms = sorted({str(s or "").strip().upper() for s in symbols if str(s or "").strip()})
    etfs = sorted({str((sector_map.get(s) or {}).get("sector_etf") or "").upper() for s in syms if str((sector_map.get(s) or {}).get("sector_etf") or "").strip()})
    all_price_syms = sorted(set(syms + etfs))
    hist_cutoff = (date.today() - timedelta(days=420)).isoformat()
    bars_by_sym = _load_price_bars(con, all_price_syms, hist_cutoff)
    option_iv_by_sym = _load_option_iv_rank_contexts(con, syms, hist_cutoff)
    stock_ctx: Dict[str, Dict[str, Any]] = {}
    sector_ctx: Dict[str, Dict[str, Any]] = {}
    uae_fallback: Dict[str, Dict[str, Any]] = {}
    for sym in syms:
        bars = bars_by_sym.get(sym, [])
        iv = _compute_iv_rank_from_bars(bars)
        opt_iv = option_iv_by_sym.get(sym) or {}
        # Prefer real option-IV rank over the historical-volatility proxy.  If no
        # option IV history exists, keep the HV proxy from price bars.
        if opt_iv and opt_iv.get("iv_rank") is not None:
            iv.update(opt_iv)
        elif opt_iv and iv.get("iv_rank") is None:
            iv.update(opt_iv)
        px = _compute_price_action_context(bars, "")
        weekly_bars = _resample_weekly_bars(bars)
        weekly_px = _compute_price_action_context(weekly_bars, "weekly_") if weekly_bars else {}
        weekly_sr = _compute_sr_distance_context(weekly_bars, "weekly_", 20) if weekly_bars else {}
        stock_ctx[sym] = {**iv, **px, **weekly_px, **weekly_sr}
        d_uae = _compute_uae_lite_context(bars, "1d")
        w_uae = _compute_uae_lite_context(bars, "1w")
        if d_uae or w_uae:
            uae_fallback[sym] = {
                "uae_context_available": bool(d_uae or w_uae),
                "uae_source": "price_cache_fallback",
                "uae_daily_regime": d_uae.get("regime"),
                "uae_weekly_regime": w_uae.get("regime"),
                "uae_daily_score": d_uae.get("score"),
                "uae_weekly_score": w_uae.get("score"),
                "uae_daily_rsidiff": d_uae.get("rsidiff"),
                "uae_weekly_rsidiff": w_uae.get("rsidiff"),
                "uae_daily_marker": d_uae.get("marker"),
                "uae_weekly_marker": w_uae.get("marker"),
                "uae_daily_marker_label": d_uae.get("marker_label"),
                "uae_weekly_marker_label": w_uae.get("marker_label"),
                "uae_daily_marker_age": d_uae.get("marker_age"),
                "uae_weekly_marker_age": w_uae.get("marker_age"),
            }
    for etf in etfs:
        px = _compute_price_action_context(bars_by_sym.get(etf, []), "sector_")
        # Rename for human display.
        sector_ctx[etf] = {
            "sector_regime": px.get("sector_price_action_bias") or "Neutral",
            "sector_trend": px.get("sector_price_action_read") or "Sector price n/a",
            "sector_score": px.get("sector_price_action_score"),
            "sector_note": px.get("sector_price_action_note"),
            "sector_change_5d_pct": px.get("sector_price_change_5d_pct"),
            "sector_change_20d_pct": px.get("sector_price_change_20d_pct"),
            "sector_rsi14": px.get("sector_rsi14"),
            "sector_rsidiff90": px.get("sector_rsidiff90"),
            "sector_bb_kc_state": px.get("sector_bb_kc_state"),
        }
    return stock_ctx, sector_ctx, uae_fallback


def _apply_enrichment_context(row: Dict[str, Any], stock_ctx: Optional[Dict[str, Any]], sector_map: Optional[Dict[str, Any]], sector_ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if stock_ctx:
        row.update(stock_ctx)
    if sector_map:
        # Backend sector should win over frontend async cache so saved/cached rows are complete.
        if sector_map.get("sector"):
            row["sector"] = sector_map.get("sector")
        row["sector_etf"] = sector_map.get("sector_etf") or row.get("sector_etf")
        row["sector_source"] = sector_map.get("sector_source") or row.get("sector_source")
    if sector_ctx:
        row.update(sector_ctx)
    if not row.get("sector_regime"):
        row["sector_regime"] = "Sector n/a"
        row["sector_trend"] = "Sector trend unavailable"
        row["sector_note"] = row.get("sector_note") or "No cached sector ETF price history was available."

    # Directional sector check used by the UI, AI Hub, and confidence score.
    direction = _direction_from_windows(row)
    sector_bias = str(row.get("sector_regime") or "").title()
    sector_etf = str(row.get("sector_etf") or "sector ETF")
    align = "Sector trend unavailable"
    score = 0
    cls = "neutral"
    if direction == "Bullish":
        if sector_bias == "Bullish":
            align, score, cls = f"{sector_etf} confirms bullish flow", 8, "bull"
        elif sector_bias == "Bearish":
            align, score, cls = f"{sector_etf} conflicts with bullish flow", -10, "bear"
        elif sector_bias in {"Neutral", "Sector N/A"}:
            align = f"{sector_etf} neutral/unavailable versus bullish flow"
    elif direction == "Bearish":
        if sector_bias == "Bearish":
            align, score, cls = f"{sector_etf} confirms bearish flow", 8, "bear"
        elif sector_bias == "Bullish":
            align, score, cls = f"{sector_etf} conflicts with bearish flow", -10, "bull"
        elif sector_bias in {"Neutral", "Sector N/A"}:
            align = f"{sector_etf} neutral/unavailable versus bearish flow"
    else:
        align = f"{sector_etf} trend is context only until Seller Flow direction aligns"
    row["sector_alignment"] = align
    row["sector_alignment_score"] = score
    row["sector_alignment_class"] = cls
    _apply_price_location_guard(row)
    return row


def _buy_sell_recommendation(row: Dict[str, Any]) -> Dict[str, Any]:
    if row.get("earnings_conflict"):
        return {"should_buy_sell": "WAIT", "entry_action": "AVOID", "buy_sell_note": "Earnings conflict blocks the suggested timeframe."}
    direction = _direction_from_windows(row)
    iv_rank = _safe_float(row.get("iv_rank"), None)
    price_bias = str(row.get("price_action_bias") or "Neutral")
    sector_bias = str(row.get("sector_regime") or "Neutral")
    uae_conf = str(row.get("uae_confirmation") or "")
    conflicts = []
    if direction == "Bullish" and price_bias == "Bearish":
        conflicts.append("price action bearish")
    if direction == "Bearish" and price_bias == "Bullish":
        conflicts.append("price action bullish")
    if direction == "Bullish" and sector_bias == "Bearish":
        conflicts.append(f"sector {row.get('sector_etf') or ''} bearish")
    if direction == "Bearish" and sector_bias == "Bullish":
        conflicts.append(f"sector {row.get('sector_etf') or ''} bullish")
    if "conflict" in uae_conf.lower():
        conflicts.append("UAE conflicts")
    if row.get("price_location_guard"):
        conflicts.append(str(row.get("price_location_guard")))
    if direction not in {"Bullish", "Bearish"}:
        return {"should_buy_sell": "WAIT", "entry_action": "WATCH", "buy_sell_note": "Seller-flow direction is mixed; wait for directional alignment."}
    if conflicts:
        return {"should_buy_sell": "WAIT", "entry_action": "WATCH", "buy_sell_note": "Do not enter yet: " + "; ".join(conflicts) + "."}
    if iv_rank is None:
        pref = "SELL PREMIUM" if direction in {"Bullish", "Bearish"} else "WAIT"
        note = "IV rank unavailable; use defined-risk structures only and verify bid/ask."
    elif iv_rank >= 60:
        pref = "SELL PREMIUM"
        note = "IV rank supports premium selling; credit spreads fit if liquidity and wall placement are acceptable."
    elif iv_rank <= 30:
        pref = "BUY OPTIONS"
        note = "IV rank is low; prefer debit spreads or wait for better credit instead of selling cheap premium."
    else:
        pref = "MIXED"
        note = "IV rank is moderate; use the seller-flow direction but be selective on credit/debit pricing."
    action = "SELL" if pref == "SELL PREMIUM" else "BUY" if pref == "BUY OPTIONS" else "WATCH"
    return {"should_buy_sell": pref, "entry_action": action, "buy_sell_note": note}





def _apply_buy_sell_action_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize IV/buy-sell guidance keys for UI, AI Hub and history rows."""
    if not isinstance(row, dict):
        return row
    pref = row.get("should_buy_sell") or row.get("premium_preference") or "WAIT"
    entry = row.get("entry_action") or ("SELL" if str(pref).upper().startswith("SELL") else "BUY" if str(pref).upper().startswith("BUY") else "WATCH")
    note = row.get("buy_sell_note") or row.get("iv_note") or "Verify price, IV, sector trend and liquidity before entry."
    row["should_buy_sell"] = pref
    row["trade_action"] = entry
    row["implementation_bias"] = pref
    row["trade_action_reason"] = note
    row["buy_sell_guidance"] = f"{pref}: {note}"
    return row

def _latest_close_from_cache(price_cache: Dict[Tuple[str, str], float], sym: str) -> Optional[float]:
    val = price_cache.get((str(sym).upper(), "__latest__"))
    return _safe_float(val, None)


def _latest_close_date_from_cache(price_cache: Dict[Tuple[str, str], float], sym: str) -> Optional[str]:
    val = price_cache.get((str(sym).upper(), "__latest_date__"))
    return str(val) if val not in (None, "") else None



def _load_flow_snapshots(con: sqlite3.Connection, symbols: Sequence[str], cutoff: str) -> Dict[str, List[Dict[str, Any]]]:
    if not symbols:
        return {}
    ph = ",".join(["?"] * len(symbols))
    cols = _table_columns(con, "options")
    iv_expr = "AVG(NULLIF(o.iv,0)) AS iv" if "iv" in cols else "NULL AS iv"
    underlying_expr = "MAX(NULLIF(o.underlying,0)) AS underlying" if "underlying" in cols else "NULL AS underlying"
    price_cache = _load_price_cache(con, symbols, cutoff)
    try:
        rows = con.execute(
            f"""
            SELECT o.symbol, o.date, MIN(o.expiration) AS expiration, LOWER(o.type) AS type, o.strike,
                   SUM(COALESCE(o.oi,0)) AS oi,
                   SUM(COALESCE(o.volume,0)) AS volume,
                   CASE WHEN SUM(COALESCE(o.oi,0))>0 THEN SUM(COALESCE(o.price,0)*COALESCE(o.oi,0))/SUM(COALESCE(o.oi,0)) ELSE AVG(o.price) END AS price,
                   {iv_expr},
                   {underlying_expr}
            FROM options o
            WHERE o.date >= ?
              AND o.symbol IN ({ph})
              AND o.expiration >= o.date
              AND o.expiration <= date(o.date, '+45 days')
            GROUP BY o.symbol, o.date, LOWER(o.type), o.strike
            ORDER BY o.symbol, o.date DESC, o.strike
            """,
            [cutoff] + list(symbols),
        ).fetchall()
    except Exception:
        return {}

    grouped: Dict[Tuple[str, str], List[Any]] = defaultdict(list)
    for r in rows:
        sym = str(r["symbol"] or "").upper()
        dt = str(r["date"] or "")
        if sym and dt:
            grouped[(sym, dt)].append(r)

    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for (sym, dt), rs in grouped.items():
        snap = _compute_skew_snapshot(rs, sym, dt, price_cache)
        if snap:
            out[sym].append(snap)
    for sym in list(out.keys()):
        out[sym].sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    return out



def _load_latest_wall_contexts(con: sqlite3.Connection, symbols: Sequence[str], cutoff: str, price_cache: Optional[Dict[Tuple[str, str], float]] = None) -> Dict[str, Dict[str, Any]]:
    """Latest aggregate strike-level context for display/actionable walls.

    The Seller Flow card snapshot uses aggregate OI by strike on the latest option
    snapshot date, across expiries up to roughly 45 days.  This keeps support,
    resistance and max-pain aligned with the OI wall/aggregate screens and avoids
    blank n/a values when the nearest-expiry skew snapshot is unavailable.
    """
    if not symbols:
        return {}
    price_cache = price_cache or _load_price_cache(con, symbols, cutoff)
    ph = ",".join(["?"] * len(symbols))
    cols = _table_columns(con, "options")
    iv_expr = "AVG(NULLIF(o.iv,0)) AS iv" if "iv" in cols else "NULL AS iv"
    underlying_expr = "MAX(NULLIF(o.underlying,0)) AS underlying" if "underlying" in cols else "NULL AS underlying"
    try:
        rows = con.execute(
            f"""
            WITH latest AS (
                SELECT symbol, MAX(date) AS date
                FROM options
                WHERE date >= ?
                  AND symbol IN ({ph})
                GROUP BY symbol
            )
            SELECT o.symbol, l.date AS date, MIN(o.expiration) AS expiration, LOWER(o.type) AS type, o.strike,
                   SUM(COALESCE(o.oi,0)) AS oi,
                   SUM(COALESCE(o.volume,0)) AS volume,
                   CASE WHEN SUM(COALESCE(o.oi,0))>0 THEN SUM(COALESCE(o.price,0)*COALESCE(o.oi,0))/SUM(COALESCE(o.oi,0)) ELSE AVG(o.price) END AS price,
                   {iv_expr},
                   {underlying_expr}
            FROM options o
            JOIN latest l ON l.symbol=o.symbol AND l.date=o.date
            WHERE o.expiration >= o.date
              AND o.expiration <= date(o.date, '+45 days')
              AND o.symbol IN ({ph})
            GROUP BY o.symbol, l.date, LOWER(o.type), o.strike
            ORDER BY o.symbol, o.strike
            """,
            [cutoff] + list(symbols) + list(symbols),
        ).fetchall()
    except Exception:
        return {}

    grouped: Dict[str, List[Any]] = defaultdict(list)
    latest_date: Dict[str, str] = {}
    for r in rows:
        sym = str(r["symbol"] or "").upper()
        if not sym:
            continue
        grouped[sym].append(r)
        latest_date[sym] = str(r["date"] or "")[:10]

    out: Dict[str, Dict[str, Any]] = {}
    for sym, rs in grouped.items():
        snap = _compute_skew_snapshot(rs, sym, latest_date.get(sym, ""), price_cache)
        if snap:
            latest_px = _latest_close_from_cache(price_cache, sym)
            if latest_px is not None:
                snap["spot"] = round(float(latest_px), 4)
            snap["context_source"] = "latest_aggregate_45d"
            snap["price_source"] = "price_cache" if latest_px else "underlying_or_unavailable"
            snap["price_date"] = _latest_close_date_from_cache(price_cache, sym)
            out[sym] = snap
    return out


def _apply_latest_context(row: Dict[str, Any], ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Fill display/actionable context fields from latest aggregate OI context."""
    if not ctx:
        return row
    # Always prefer actual cached current spot for the top-right price display.
    if ctx.get("spot"):
        row["spot"] = _safe_float(ctx.get("spot"), row.get("spot"), 2)
        row["spot_proxy"] = row["spot"]
        row["spot_source"] = ctx.get("price_source") or row.get("spot_source") or "price_cache"
        row["spot_price_date"] = ctx.get("price_date") or row.get("spot_price_date")
    # Aggregate latest walls/pain are better display/action anchors than blanks or
    # a stale nearest-expiry snapshot.
    for k in (
        "max_pain", "put_wall_strike", "put_wall_oi", "put_long_strike",
        "call_wall_strike", "call_wall_oi", "call_long_strike", "strike_step",
        "risk_reversal", "put_skew", "call_skew", "current_iv", "iv_point_count", "skew_source", "skew_is_proxy",
    ):
        if row.get(k) in (None, "", 0, "n/a", "unavailable") and ctx.get(k) not in (None, "", "unavailable"):
            row[k] = ctx.get(k)
    # For support/resistance anchors, prefer aggregate latest walls even when the
    # nearest-expiry snapshot produced a far/non-actionable value.
    for k in ("put_wall_strike", "put_wall_oi", "put_long_strike", "call_wall_strike", "call_wall_oi", "call_long_strike", "strike_step"):
        if ctx.get(k) not in (None, ""):
            row[k] = ctx.get(k)
    if ctx.get("max_pain") not in (None, ""):
        row["max_pain"] = ctx.get("max_pain")
        row["max_pain_context_source"] = ctx.get("context_source")
    # If the exact-expiry/current row could not produce a numeric RR, use the
    # aggregate OI-skew proxy so the card does not show a misleading blank.
    if row.get("risk_reversal") in (None, "", "n/a") and ctx.get("risk_reversal") not in (None, "", "n/a"):
        row["risk_reversal"] = ctx.get("risk_reversal")
        row["put_skew"] = ctx.get("put_skew")
        row["call_skew"] = ctx.get("call_skew")
        row["skew_source"] = ctx.get("skew_source") or row.get("skew_source")
        row["skew_is_proxy"] = ctx.get("skew_is_proxy")
    if ctx.get("skew_source") and row.get("skew_source") in (None, "", "unavailable"):
        row["skew_source"] = ctx.get("skew_source")
        row["skew_is_proxy"] = ctx.get("skew_is_proxy")
    if row.get("skew_source") == "oi_proxy":
        row["data_quality_note"] = "Skew shown as OI-skew proxy from aggregate strike-level OI because true historical IV/spot IV skew was not available."
    elif row.get("risk_reversal") in (None, "", "n/a"):
        row["data_quality_note"] = (row.get("data_quality_note") or "Skew source is known but no usable current risk-reversal/proxy value was available from the stored chain.")
    return row


def _shift_label(v: Any, threshold: float = 0.25, up: str = "Up", down: str = "Down") -> str:
    x = _safe_float(v, None)
    if x is None:
        return "n/a"
    if x > threshold:
        return up
    if x < -threshold:
        return down
    return "Flat"


def _window_flow_stats(snaps: Sequence[Dict[str, Any]], days: int) -> Dict[str, Any]:
    latest = snaps[0] if snaps else None
    if not latest:
        return {
            "base": None, "max_pain": None, "max_pain_base": None, "max_pain_chg": None,
            "max_pain_chg_pct": None, "max_pain_shift": "n/a", "risk_reversal": None,
            "risk_reversal_base": None, "risk_reversal_chg": None, "put_skew": None,
            "put_skew_chg": None, "call_skew": None, "call_skew_chg": None,
            "skew_source": "unavailable", "skew_is_proxy": 0,
        }
    idx = min(max(1, int(days)), len(snaps) - 1) if len(snaps) > 1 else 0
    base = snaps[idx]
    mp_now = _safe_float(latest.get("max_pain"), None)
    mp_base = _safe_float(base.get("max_pain"), None)
    rr_now = _safe_float(latest.get("risk_reversal"), None)
    rr_base = _safe_float(base.get("risk_reversal"), None)
    put_now = _safe_float(latest.get("put_skew"), None)
    put_base = _safe_float(base.get("put_skew"), None)
    call_now = _safe_float(latest.get("call_skew"), None)
    call_base = _safe_float(base.get("call_skew"), None)
    mp_chg = round(mp_now - mp_base, 2) if mp_now is not None and mp_base is not None else None
    mp_pct = _pct_change(mp_now, mp_base) if mp_now is not None and mp_base is not None else None
    rr_chg = round(rr_now - rr_base, 2) if rr_now is not None and rr_base is not None else None
    put_chg = round(put_now - put_base, 2) if put_now is not None and put_base is not None else None
    call_chg = round(call_now - call_base, 2) if call_now is not None and call_base is not None else None
    return {
        "base": base,
        "latest": latest,
        "max_pain": mp_now,
        "max_pain_base": mp_base,
        "max_pain_chg": mp_chg,
        "max_pain_chg_pct": mp_pct,
        "max_pain_shift": _shift_label(mp_pct, 0.20, "Up", "Down"),
        "risk_reversal": rr_now,
        "risk_reversal_base": rr_base,
        "risk_reversal_chg": rr_chg,
        "risk_reversal_shift": _shift_label(rr_chg, 0.75, "Call skew up", "Put skew up"),
        "put_skew": put_now,
        "put_skew_base": put_base,
        "put_skew_chg": put_chg,
        "call_skew": call_now,
        "call_skew_base": call_base,
        "call_skew_chg": call_chg,
        "skew_source": latest.get("skew_source") or "unavailable",
        "skew_is_proxy": int(latest.get("skew_is_proxy") or 0),
    }


# ---------------------------------------------------------------------------
# Seller-side signal model
# ---------------------------------------------------------------------------

def _sign(v: Any, threshold: float = 0.25) -> int:
    x = _safe_float(v, 0.0) or 0.0
    th = abs(float(threshold or 0.0))
    if x > th:
        return 1
    if x < -th:
        return -1
    return 0


def oi_trend(oi_pct: Any, neutral_threshold: float = 0.25) -> str:
    v = _safe_float(oi_pct, 0.0) or 0.0
    th = abs(float(neutral_threshold or 0.0))
    if v > th:
        return "Rising OI"
    if v < -th:
        return "Falling OI"
    return "Flat OI"


def trend_sign(oi_pct: Any, neutral_threshold: float = 0.25) -> int:
    return _sign(oi_pct, neutral_threshold)


def price_oi_signal(oi_pct: Any, price_pct: Any, neutral_threshold: float = 0.25) -> str:
    """Legacy price/OI interpretation retained as a separate compatibility field."""
    oi = trend_sign(oi_pct, neutral_threshold)
    pr = trend_sign(price_pct, neutral_threshold)
    if oi > 0 and pr > 0:
        return "Long Buildup"
    if oi > 0 and pr < 0:
        return "Short Buildup"
    if oi < 0 and pr < 0:
        return "Long Unwinding"
    if oi < 0 and pr > 0:
        return "Short Covering"
    return "Neutral"


def _pcr_side(stats: Dict[str, Any]) -> int:
    """+1 = put side gains share; -1 = call side gains share."""
    pcr_pct = _safe_float(stats.get("pcr_chg_pct"), 0.0) or 0.0
    pcr_abs = _safe_float(stats.get("pcr_chg"), 0.0) or 0.0
    side_edge = _safe_float(stats.get("side_edge"), 0.0) or 0.0
    put_votes = 0
    call_votes = 0
    if pcr_pct >= 3.0:
        put_votes += 1
    elif pcr_pct <= -3.0:
        call_votes += 1
    if pcr_abs >= 0.03:
        put_votes += 1
    elif pcr_abs <= -0.03:
        call_votes += 1
    if side_edge >= 3.0:
        put_votes += 1
    elif side_edge <= -3.0:
        call_votes += 1
    if put_votes > call_votes:
        return 1
    if call_votes > put_votes:
        return -1
    return 0


def seller_oi_signal(
    stats: Dict[str, Any],
    price_pct: Any = 0.0,
    risk_reversal_chg: Optional[float] = None,
    max_pain_chg_pct: Optional[float] = None,
    skew_source: Optional[str] = None,
) -> Tuple[str, str, int, str]:
    """Classify one ST/MT/LT window from the standpoint of option sellers.

    Returns: (signal label, direction, numeric score, confirmation context).
    The skew input is risk reversal change (call IV/proxy minus put IV/proxy).
    Positive RR change means call skew/share gained versus put skew/share.
    """
    oi_pct = _safe_float(stats.get("oi_pct"), 0.0) or 0.0
    pcr_side = _pcr_side(stats)
    price = _safe_float(price_pct, 0.0) or 0.0
    oi_s = _sign(oi_pct, 1.0)
    price_s = _sign(price, 0.35)

    if oi_s > 0:
        if pcr_side > 0:
            label, direction, score = "Bullish Put Selling", "Bullish", 2
        elif pcr_side < 0:
            label, direction, score = "Bearish Call Selling", "Bearish", -2
        else:
            label, direction, score = "Mixed OI Buildup", "Neutral", 0
    elif oi_s < 0:
        if pcr_side > 0:
            label, direction, score = "Bullish Call Unwind", "Bullish", 1
        elif pcr_side < 0:
            label, direction, score = "Bearish Put Unwind", "Bearish", -1
        else:
            label, direction, score = "Neutral OI Unwind", "Neutral", 0
    else:
        if pcr_side > 0:
            label, direction, score = "Bullish PCR Shift", "Bullish", 1
        elif pcr_side < 0:
            label, direction, score = "Bearish PCR Shift", "Bearish", -1
        else:
            label, direction, score = "Neutral", "Neutral", 0

    notes: List[str] = []
    if direction == "Bullish":
        if price_s > 0:
            score += 1
            notes.append("price confirming")
        elif price_s < 0:
            notes.append("support test")
        else:
            notes.append("price unconfirmed")
        rr = _safe_float(risk_reversal_chg, None)
        if rr is not None:
            if rr >= -1.0:
                score += 1
                if label == "Bullish Put Selling":
                    label = "Bullish Put Selling Confirmed"
                notes.append("skew not bidding puts")
            elif rr <= -3.0:
                score -= 1
                if label == "Bullish Put Selling":
                    label = "Bullish Put Selling But Skew Risk"
                notes.append("put skew risk")
        mp = _safe_float(max_pain_chg_pct, None)
        if mp is not None:
            if mp >= 0.20:
                score += 1
                notes.append("max pain rising")
            elif mp <= -0.75:
                score -= 1
                notes.append("max pain falling")
    elif direction == "Bearish":
        if price_s < 0:
            score -= 1
            notes.append("price confirming")
        elif price_s > 0:
            notes.append("resistance test")
        else:
            notes.append("price unconfirmed")
        rr = _safe_float(risk_reversal_chg, None)
        if rr is not None:
            if rr <= 1.0:
                score -= 1
                if label == "Bearish Call Selling":
                    label = "Bearish Call Selling Confirmed"
                notes.append("skew not chasing calls")
            elif rr >= 3.0:
                score += 1
                if label == "Bearish Call Selling":
                    label = "Bearish Call Selling But Upside Skew Risk"
                notes.append("call skew risk")
        mp = _safe_float(max_pain_chg_pct, None)
        if mp is not None:
            if mp <= -0.20:
                score -= 1
                notes.append("max pain falling")
            elif mp >= 0.75:
                score += 1
                notes.append("max pain rising")
    else:
        notes.append("price neutral")

    if skew_source == "oi_proxy":
        notes.append("OI-skew proxy")
    elif skew_source in {"iv", "price_iv"}:
        notes.append("IV skew")

    return label, direction, int(score), "; ".join(notes)


def _bias_from_seller_windows(
    st: Tuple[str, str, int, str],
    mt: Tuple[str, str, int, str],
    lt: Tuple[str, str, int, str],
    pcr: float,
    st_stats: Dict[str, Any],
    mt_stats: Dict[str, Any],
    lt_stats: Dict[str, Any],
) -> Tuple[str, int]:
    score = st[2] + int(round(mt[2] * 1.25)) + int(round(lt[2] * 1.5))
    if pcr >= 1.20:
        score += 1
    elif pcr <= 0.80:
        score -= 1
    pcr_mt = _safe_float(mt_stats.get("pcr_chg_pct"), 0.0) or 0.0
    pcr_lt = _safe_float(lt_stats.get("pcr_chg_pct"), 0.0) or 0.0
    if pcr_mt > 8 and pcr_lt > 5:
        score += 1
    elif pcr_mt < -8 and pcr_lt < -5:
        score -= 1
    if score >= 8:
        return "Strongly Bullish", score
    if score >= 4:
        return "Bullish", score
    if score >= 1:
        return "Mildly Bullish", score
    if score <= -8:
        return "Strongly Bearish", score
    if score <= -4:
        return "Bearish", score
    if score <= -1:
        return "Mildly Bearish", score
    return "Sideways", score


def _alignment(row: Dict[str, Any]) -> Dict[str, Any]:
    st_s = trend_sign(row.get("oi_st_pct"))
    mt_s = trend_sign(row.get("oi_mt_pct"))
    lt_s = trend_sign(row.get("oi_lt_pct"))
    mismatches: List[str] = []
    if lt_s != st_s:
        mismatches.append("ST")
    if lt_s != mt_s:
        mismatches.append("MT")
    row["st_oi_trend"] = oi_trend(row.get("oi_st_pct"))
    row["mt_oi_trend"] = oi_trend(row.get("oi_mt_pct"))
    row["lt_oi_trend"] = oi_trend(row.get("oi_lt_pct"))
    row["lt_st_aligned"] = lt_s == st_s
    row["lt_mt_aligned"] = lt_s == mt_s
    row["oi_trend_aligned"] = not mismatches
    row["misaligned_with"] = mismatches
    row["alignment_label"] = "Aligned" if not mismatches else "LT vs " + "/".join(mismatches) + " mismatch"
    row["divergence_score"] = round(
        max(
            abs((_safe_float(row.get("oi_lt_pct"), 0.0) or 0.0) - (_safe_float(row.get("oi_st_pct"), 0.0) or 0.0)),
            abs((_safe_float(row.get("oi_lt_pct"), 0.0) or 0.0) - (_safe_float(row.get("oi_mt_pct"), 0.0) or 0.0)),
        ),
        2,
    )
    return row


def _load_earnings_map(con: sqlite3.Connection, symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols or not _table_exists(con, "earnings_calendar"):
        return {}
    ph = ",".join(["?"] * len(symbols))
    try:
        rows = con.execute(
            f"""
            SELECT symbol, next_earn_date, next_earn_confirmed, last_earn_date,
                   last_surprise_pct, earn_reaction_pct, surprise_streak, earn_score
            FROM earnings_calendar
            WHERE symbol IN ({ph})
            """,
            list(symbols),
        ).fetchall()
        return {str(r["symbol"] or "").upper(): dict(r) for r in rows}
    except Exception:
        return {}


def _earn_context(symbol: str, rec: Optional[Dict[str, Any]], timeframe_hi: int) -> Dict[str, Any]:
    sym = (symbol or "").upper()
    if sym in NON_EQUITY:
        return {"earnings_date": None, "earnings_days": None, "earnings_conflict": False, "earnings_note": "ETF/index proxy: no company earnings conflict."}
    ed = str((rec or {}).get("next_earn_date") or "")[:10] or None
    days = None
    if ed:
        try:
            days = (date.fromisoformat(ed) - date.today()).days
        except Exception:
            days = None
    conflict = bool(days is not None and 0 <= days <= max(1, int(timeframe_hi or 0)) + 2)
    if not ed:
        note = "No cached upcoming earnings date. Refresh the earnings calendar before relying on the timeframe."
    elif conflict:
        note = f"Earnings {ed} is inside the suggested timeframe; avoid holding new option premium through the event."
    elif days is not None and days >= 0:
        note = f"Next earnings {ed} is {days} calendar days away; no conflict with the suggested timeframe."
    else:
        note = f"Cached earnings date {ed} is not upcoming."
    return {
        "earnings_date": ed,
        "earnings_days": days,
        "earnings_confirmed": int((rec or {}).get("next_earn_confirmed") or 0),
        "earnings_score": (rec or {}).get("earn_score"),
        "earnings_conflict": conflict,
        "earnings_note": note,
    }





# ---------------------------------------------------------------------------
# UAE Trend/Vol Analyzer context for Seller Flow rows
# ---------------------------------------------------------------------------

def _round_opt(v: Any, nd: int = 2) -> Optional[float]:
    x = _safe_float(v, None)
    return None if x is None else round(float(x), nd)


def _uae_marker_label(v: Any) -> str:
    raw = str(v or "").strip().upper()
    labels = {
        "TREND_BULL": "Trend Bull triangle",
        "TREND_BEAR": "Trend Bear triangle",
        "MRT_BUY": "MRT/Fade Long arrow",
        "MRT_SELL": "MRT/Fade Short arrow",
        "FADE_BUY": "MRT/Fade Long arrow",
        "FADE_SELL": "MRT/Fade Short arrow",
    }
    return labels.get(raw, raw.replace("_", " ").title() if raw else "No recent marker")


def _load_uae_contexts(symbols: Sequence[str], benchmark: str = "SPY") -> Dict[str, Dict[str, Any]]:
    """Return UAE v5 daily/weekly context for symbols using local cached bars only.

    This keeps the OI Buildup/Seller Flow response aligned with the Scanner
    Builder UAE primitives without triggering yfinance calls while scanning.
    The implementation reuses the Scanner Builder evaluator so the formulas stay
    single-maintenance with the Pine-aligned UAE primitives.
    """
    syms = [str(s or "").strip().upper() for s in symbols if str(s or "").strip()]
    if not syms:
        return {}
    try:
        from .scanner_builder import _symbol_ctx, _parse_query, _eval
    except Exception:
        return {}

    exprs = {
        "uae_daily_regime": 'UAERegime("1d")',
        "uae_weekly_regime": 'UAERegime("1w")',
        "uae_daily_score": 'UAERegimeScore("1d")',
        "uae_weekly_score": 'UAERegimeScore("1w")',
        "uae_daily_rsidiff": 'UAERSIDiff("1d")',
        "uae_weekly_rsidiff": 'UAERSIDiff("1w")',
        "uae_daily_hist": 'UAEHist("1d")',
        "uae_weekly_hist": 'UAEHist("1w")',
        "uae_daily_strong_hist": 'UAEStrongHist("1d")',
        "uae_weekly_strong_hist": 'UAEStrongHist("1w")',
        "uae_daily_marker": 'UAELastMarker("1d", 10)',
        "uae_daily_marker_age": 'UAELastMarkerAge("1d", 10)',
        "uae_weekly_marker": 'UAELastMarker("1w", 10)',
        "uae_weekly_marker_age": 'UAELastMarkerAge("1w", 10)',
        "uae_daily_bull_triangle_age": 'UAETrendTriangleAge("bull", "1d", 20)',
        "uae_daily_bear_triangle_age": 'UAETrendTriangleAge("bear", "1d", 20)',
        "uae_weekly_bull_triangle_age": 'UAETrendTriangleAge("bull", "1w", 20)',
        "uae_weekly_bear_triangle_age": 'UAETrendTriangleAge("bear", "1w", 20)',
        "uae_daily_mrt_buy_age": 'UAEFadeArrowAge("bull", "1d", 20)',
        "uae_daily_mrt_sell_age": 'UAEFadeArrowAge("bear", "1d", 20)',
    }
    try:
        nodes = {k: _parse_query(v) for k, v in exprs.items()}
    except Exception:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    old_mode = os.environ.get("SCANNER_HISTORY_SOURCE")
    os.environ["SCANNER_HISTORY_SOURCE"] = "db"
    try:
        for sym in syms:
            try:
                ctx = _symbol_ctx(sym, benchmark, ["1d", "1w"])
                vals: Dict[str, Any] = {}
                for key, node in nodes.items():
                    try:
                        vals[key] = _eval(node, ctx)
                    except Exception:
                        vals[key] = None
                for key in ("uae_daily_score", "uae_weekly_score", "uae_daily_rsidiff", "uae_weekly_rsidiff", "uae_daily_hist", "uae_weekly_hist"):
                    vals[key] = _round_opt(vals.get(key), 2)
                for key in ("uae_daily_marker_age", "uae_weekly_marker_age", "uae_daily_bull_triangle_age", "uae_daily_bear_triangle_age", "uae_weekly_bull_triangle_age", "uae_weekly_bear_triangle_age", "uae_daily_mrt_buy_age", "uae_daily_mrt_sell_age"):
                    v = vals.get(key)
                    vals[key] = None if v in (None, "") else _safe_int(v, -1)
                vals["uae_daily_marker_label"] = _uae_marker_label(vals.get("uae_daily_marker"))
                vals["uae_weekly_marker_label"] = _uae_marker_label(vals.get("uae_weekly_marker"))
                vals["uae_context_available"] = True
                out[sym] = vals
            except Exception as exc:
                out[sym] = {"uae_context_available": False, "uae_error": str(exc)[:160]}
    finally:
        if old_mode is None:
            os.environ.pop("SCANNER_HISTORY_SOURCE", None)
        else:
            os.environ["SCANNER_HISTORY_SOURCE"] = old_mode
    return out


def _uae_alignment_for_row(row: Dict[str, Any]) -> Dict[str, Any]:
    direction = _direction_from_windows(row)
    if not row.get("uae_context_available"):
        return {
            "uae_confirmation": "UAE unavailable",
            "uae_alignment_score": 0,
            "uae_action_note": "UAE Trend/Vol context was unavailable from local cached bars; do not use OI/PCR alone for entry timing.",
        }
    d_reg = str(row.get("uae_daily_regime") or "").upper()
    w_reg = str(row.get("uae_weekly_regime") or "").upper()
    d_marker = str(row.get("uae_daily_marker") or "").upper()
    w_marker = str(row.get("uae_weekly_marker") or "").upper()
    d_age = row.get("uae_daily_marker_age")
    w_age = row.get("uae_weekly_marker_age")
    bull_ctx = sum(1 for v in (d_reg, w_reg, d_marker, w_marker) if "BULL" in v or v == "MRT_BUY")
    bear_ctx = sum(1 for v in (d_reg, w_reg, d_marker, w_marker) if "BEAR" in v or v == "MRT_SELL")
    score = 0
    if direction == "Bullish":
        score = (bull_ctx - bear_ctx) * 20
    elif direction == "Bearish":
        score = (bear_ctx - bull_ctx) * 20
    else:
        score = 0
    # Recent same-side marker gets a small timing bonus; opposite marker is a warning.
    if direction == "Bullish":
        if d_marker in {"TREND_BULL", "MRT_BUY"} and d_age is not None and int(d_age) <= 5:
            score += 15
        if d_marker in {"TREND_BEAR", "MRT_SELL"} and d_age is not None and int(d_age) <= 5:
            score -= 15
    elif direction == "Bearish":
        if d_marker in {"TREND_BEAR", "MRT_SELL"} and d_age is not None and int(d_age) <= 5:
            score += 15
        if d_marker in {"TREND_BULL", "MRT_BUY"} and d_age is not None and int(d_age) <= 5:
            score -= 15
    score = int(max(-50, min(50, score)))

    if direction not in {"Bullish", "Bearish"}:
        conf = "UAE neutral"
        note = "Seller-flow direction is mixed; UAE is timing context only. Wait for OI windows and UAE direction to align."
    elif score >= 35:
        conf = "UAE confirms"
        note = f"UAE {d_reg or 'n/a'} daily / {w_reg or 'n/a'} weekly supports the {direction.lower()} seller-flow read; use the most recent UAE marker as entry timing confirmation."
    elif score <= -20:
        conf = "UAE conflicts"
        note = f"UAE {d_reg or 'n/a'} daily / {w_reg or 'n/a'} weekly conflicts with the {direction.lower()} OI read. Treat as watchlist only until the UAE marker/regime turns."
    else:
        conf = "UAE mixed"
        note = f"UAE is not strongly aligned: daily {d_reg or 'n/a'} marker {_uae_marker_label(d_marker)}; weekly {w_reg or 'n/a'} marker {_uae_marker_label(w_marker)}. Require price confirmation."
    return {
        "uae_confirmation": conf,
        "uae_alignment_score": score,
        "uae_action_note": note,
    }


def _apply_uae_to_seller_row(row: Dict[str, Any], uae: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return row
    if uae:
        row.update(uae)
    else:
        row.update({"uae_context_available": False})
    row.update(_uae_alignment_for_row(row))
    return row

def _direction_from_windows(row: Dict[str, Any]) -> str:
    """Infer the tradable seller-flow direction from ST/MT/LT windows.

    Do not rely only on the aggregate bias label.  If all three windows say
    Bearish Call Selling, the recommendation should be a bear-call framework
    even if the broader bias text is missing or stale in a cached row.
    """
    vals = [str(row.get(k) or "") for k in ("st_seller_bias", "mt_seller_bias", "lt_seller_bias")]
    labels = [str(row.get(k) or "") for k in ("st_outlook", "mt_outlook", "lt_outlook", "st_seller_signal", "mt_seller_signal", "lt_seller_signal")]
    bull = sum(1 for v in vals if "Bull" in v) + sum(1 for v in labels if "Bullish" in v or "Put Selling" in v or "Call Unwind" in v)
    bear = sum(1 for v in vals if "Bear" in v) + sum(1 for v in labels if "Bearish" in v or "Call Selling" in v or "Put Unwind" in v)
    if bear >= 2 and bear > bull:
        return "Bearish"
    if bull >= 2 and bull > bear:
        return "Bullish"
    bias = str(row.get("bias") or row.get("final_seller_read") or "")
    if "Bear" in bias:
        return "Bearish"
    if "Bull" in bias:
        return "Bullish"
    return "Neutral"



def _apply_price_location_guard(row: Dict[str, Any]) -> Dict[str, Any]:
    """Flag when seller-flow direction is too close to major price-action extremes.

    This prevents the card from showing a confident bearish strategy when price is
    already stretched down into support, or a confident bullish strategy when price
    is stretched up into resistance.  It uses daily and weekly distances plus RSI /
    RSIDiff90 exhaustion checks.
    """
    direction = _direction_from_windows(row)
    d_sup = _safe_float(row.get("distance_from_support_20_pct"), None)
    d_res = _safe_float(row.get("distance_from_resistance_20_pct"), None)
    w_sup = _safe_float(row.get("weekly_distance_from_support_20_pct"), None)
    w_res = _safe_float(row.get("weekly_distance_from_resistance_20_pct"), None)
    rsi = _safe_float(row.get("rsi14"), None)
    wrsi = _safe_float(row.get("weekly_rsi14"), None)
    rsid = _safe_float(row.get("rsidiff90"), None)
    wrsid = _safe_float(row.get("weekly_rsidiff90"), None)
    bb = _safe_float(row.get("bb_position"), None)
    wbb = _safe_float(row.get("weekly_bb_position"), None)

    notes: List[str] = []
    guard = ""
    score = 0
    near_support = any(x is not None and x <= lim for x, lim in ((d_sup, 2.0), (w_sup, 3.0)))
    near_resistance = any(x is not None and x <= lim for x, lim in ((d_res, 2.0), (w_res, 3.0)))
    oversold = any(x is not None and x <= lim for x, lim in ((rsi, 35.0), (wrsi, 38.0), (rsid, -18.0), (wrsid, -18.0), (bb, 8.0), (wbb, 10.0)))
    overbought = any(x is not None and x >= lim for x, lim in ((rsi, 70.0), (wrsi, 68.0), (rsid, 18.0), (wrsid, 18.0), (bb, 92.0), (wbb, 90.0)))

    if d_sup is not None:
        notes.append(f"D support {d_sup:.1f}%")
    if d_res is not None:
        notes.append(f"D resistance {d_res:.1f}%")
    if w_sup is not None:
        notes.append(f"W support {w_sup:.1f}%")
    if w_res is not None:
        notes.append(f"W resistance {w_res:.1f}%")
    if rsi is not None:
        notes.append(f"RSI {rsi:.1f}")
    if rsid is not None:
        notes.append(f"RSIDiff90 {rsid:.1f}")

    if direction == "Bearish" and (near_support or oversold):
        guard = "Bearish flow is near support / downside exhaustion"
        score = -16
    elif direction == "Bullish" and (near_resistance or overbought):
        guard = "Bullish flow is near resistance / upside exhaustion"
        score = -16
    elif direction == "Bearish" and near_resistance:
        score = 6
        notes.append("bearish idea has nearby resistance confirmation")
    elif direction == "Bullish" and near_support:
        score = 6
        notes.append("bullish idea has nearby support confirmation")

    row["price_location_note"] = "; ".join(notes) if notes else "No support/resistance distance context available."
    row["price_location_guard"] = guard
    row["price_location_score"] = score
    if guard:
        row["price_location_action_note"] = guard + "; wait for bounce/rejection or a clean break/hold before taking a new directional premium trade."
    else:
        row["price_location_action_note"] = row["price_location_note"]
    return row


def _strategy_from_seller_flow(row: Dict[str, Any], earn_rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    st_b = str(row.get("st_seller_bias") or "")
    mt_b = str(row.get("mt_seller_bias") or "")
    lt_b = str(row.get("lt_seller_bias") or "")
    direction = _direction_from_windows(row)
    if direction in {"Bullish", "Bearish"} and mt_b == direction and lt_b == direction:
        timeframe = "20-45D swing"
        expiry_window = "20-45 DTE"
        lo, hi = 20, 45
    elif direction in {"Bullish", "Bearish"} and (mt_b == direction or st_b == direction):
        timeframe = "7-21D tactical"
        expiry_window = "7-21 DTE"
        lo, hi = 7, 21
    elif direction in {"Bullish", "Bearish"}:
        timeframe = "3-10D watch-only"
        expiry_window = "3-10 DTE only after price confirmation"
        lo, hi = 3, 10
    else:
        timeframe = "No directional timeframe"
        expiry_window = "No trade / wait for alignment"
        lo, hi = 0, 0

    labels = " | ".join(str(row.get(k) or "") for k in ("st_outlook", "mt_outlook", "lt_outlook"))
    if direction == "Bullish":
        if "Skew Risk" in labels:
            strategy = "Wait or use small defined-risk bullish debit only after price confirms"
            code = "WAIT/CALL_DEBIT"
        else:
            strategy = "Bull put credit spread below put-support / max-pain support"
            code = "PS"
    elif direction == "Bearish":
        if "Upside Skew Risk" in labels:
            strategy = "Wait or use small defined-risk bearish debit only after rejection confirms"
            code = "WAIT/PUT_DEBIT"
        else:
            strategy = "Bear call credit spread above call-resistance / max-pain resistance"
            code = "CS"
    elif abs(_safe_float(row.get("max_pain_lt_chg_pct"), 0.0) or 0.0) <= 0.5 and str(row.get("lt_outlook") or "").startswith("Neutral"):
        strategy = "Iron condor only if price remains pinned and skew is stable"
        code = "IC"
    else:
        strategy = "No trade; seller flow is mixed"
        code = "NO_TRADE"

    # Price-location guardrail: OI flow can be directionally bearish/bullish, but
    # selling fresh premium into weekly support/resistance exhaustion is a low-quality
    # entry.  Keep the flow label, but downgrade the strategy to a conditional watch.
    guard = str(row.get("price_location_guard") or "")
    if guard and direction == "Bearish" and code == "CS":
        strategy = "WAIT: bearish seller flow is near support/exhaustion; consider CS only after a bounce/rejection or clean support break"
        code = "WAIT_CS"
        timeframe = "watch/retest only"
        expiry_window = "3-10D only after price confirmation"
        lo, hi = 3, 10
    elif guard and direction == "Bullish" and code == "PS":
        strategy = "WAIT: bullish seller flow is near resistance/exhaustion; consider PS only after pullback/support confirmation"
        code = "WAIT_PS"
        timeframe = "watch/retest only"
        expiry_window = "3-10D only after price confirmation"
        lo, hi = 3, 10

    earn = _earn_context(str(row.get("symbol") or ""), earn_rec, hi)
    avoid_timeframe = ""
    if earn.get("earnings_conflict"):
        if earn.get("earnings_days") is not None and int(earn.get("earnings_days") or 0) > 4:
            safe_hi = max(1, int(earn.get("earnings_days") or 0) - 2)
            avoid_timeframe = f"Avoid expiries after {earn.get('earnings_date')}; only consider <= {safe_hi}D trades that expire before earnings, otherwise wait until after the report."
        else:
            avoid_timeframe = f"Avoid new option trades until after earnings {earn.get('earnings_date')} clears and IV crush/price reaction is known."
        strategy = "NO NEW TRADE THROUGH EARNINGS; reassess after event"
        code = "NO_TRADE_EARNINGS"
    elif earn.get("earnings_date") and direction in {"Bullish", "Bearish"}:
        avoid_timeframe = f"Do not choose an expiry that crosses earnings {earn.get('earnings_date')}."

    put_short = _safe_float(row.get("put_wall_strike"), None)
    put_long = _safe_float(row.get("put_long_strike"), None)
    call_short = _safe_float(row.get("call_wall_strike"), None)
    call_long = _safe_float(row.get("call_long_strike"), None)
    suggested_strikes = ""
    if code == "PS":
        if put_short is not None:
            suggested_strikes = f"Sell {put_short:g}P" + (f" / Buy {put_long:g}P" if put_long is not None else "")
        else:
            suggested_strikes = "below put-support after support strike is confirmed"
    elif code == "CS":
        if call_short is not None:
            suggested_strikes = f"Sell {call_short:g}C" + (f" / Buy {call_long:g}C" if call_long is not None else "")
        else:
            suggested_strikes = "above call-resistance after resistance strike is confirmed"
    elif code == "IC":
        if put_short is not None and call_short is not None:
            suggested_strikes = f"Short put near {put_short:g}P / short call near {call_short:g}C"
        else:
            suggested_strikes = "between confirmed put-support and call-resistance"

    if suggested_strikes and not code.startswith("NO_TRADE"):
        suggested_trade_label = f"{code} {suggested_strikes}"
    else:
        suggested_trade_label = strategy

    return {
        "suggested_strategy": strategy,
        "suggested_strategy_code": code,
        "suggested_strikes": suggested_strikes,
        "suggested_trade_label": suggested_trade_label,
        "suggested_timeframe": timeframe,
        "suggested_expiry_window": expiry_window,
        "timeframe_min_days": lo,
        "timeframe_max_days": hi,
        "avoid_timeframe": avoid_timeframe,
        **earn,
    }


def ensure_seller_flow_strategy(row: Dict[str, Any]) -> Dict[str, Any]:
    """Backfill strategy fields for cached/saved Seller Flow rows.

    Older cache entries can have ST/MT/LT seller signals but no strategy fields.
    This keeps Load Saved visually useful without forcing a fresh scan.
    """
    if not isinstance(row, dict):
        return row
    code = str(row.get("suggested_strategy_code") or "")
    strat = str(row.get("suggested_strategy") or "")
    weak = (not strat) or ("No strategy" in strat) or (code in {"", "NO_TRADE"} and _direction_from_windows(row) in {"Bullish", "Bearish"})
    if weak:
        existing_earn = {k: row.get(k) for k in ("earnings_date", "earnings_days", "earnings_conflict", "earnings_note", "avoid_timeframe") if row.get(k) not in (None, "")}
        repl = _strategy_from_seller_flow(row, None)
        row.update(repl)
        # Preserve cached earnings context if it was already present.
        row.update(existing_earn)
        if row.get("earnings_conflict") and not str(row.get("suggested_strategy_code") or "").startswith("NO_TRADE"):
            row["suggested_strategy"] = "NO NEW TRADE THROUGH EARNINGS; reassess after event"
            row["suggested_strategy_code"] = "NO_TRADE_EARNINGS"
            row["suggested_trade_label"] = row["suggested_strategy"]
    if not row.get("suggested_trade_label"):
        code = str(row.get("suggested_strategy_code") or "")
        strikes = str(row.get("suggested_strikes") or "")
        row["suggested_trade_label"] = (f"{code} {strikes}".strip() if strikes and not code.startswith("NO_TRADE") else row.get("suggested_strategy") or "No strategy suggestion")
    return row


def repair_seller_flow_display_fields(row: Dict[str, Any], con: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Repair cached Seller Flow rows after display-context upgrades.

    Load Saved can return rows produced by an older scanner version.  This refreshes
    the fields that are most visible on the card: real spot, support/resistance
    walls, max pain and skew source.  It does not rerun the whole scanner.
    """
    if not isinstance(row, dict):
        return row
    own_con = con is None
    if own_con:
        con = _connect()
    try:
        sym = str(row.get("symbol") or "").strip().upper()
        if sym:
            cutoff = (date.today() - timedelta(days=45)).isoformat()
            pc = _load_price_cache(con, [sym], cutoff)
            latest_px = _latest_close_from_cache(pc, sym)
            if latest_px is not None and latest_px > 0:
                row["spot"] = round(float(latest_px), 2)
                row["spot_proxy"] = row["spot"]
                row["spot_source"] = "price_cache"
                row["spot_price_date"] = _latest_close_date_from_cache(pc, sym)
            ctx = _load_latest_wall_contexts(con, [sym], cutoff, pc).get(sym)
            if ctx:
                _apply_latest_context(row, ctx)
            sec_map = _load_sector_map(con, [sym]).get(sym, {})
            stock_ctx, sector_ctx_map, uae_fb = _load_context_enrichments(con, [sym], {sym: sec_map})
            _apply_enrichment_context(row, stock_ctx.get(sym), sec_map, sector_ctx_map.get(str(sec_map.get("sector_etf") or "").upper()))
            if not row.get("uae_context_available"):
                _apply_uae_to_seller_row(row, uae_fb.get(sym))
            row.update(_buy_sell_recommendation(row))
            _apply_buy_sell_action_fields(row)
            row["flow_confidence"] = _flow_confidence(row)
            row["confidence"] = row["flow_confidence"]
            row["suggested_action"] = _action_from_seller_flow(row)
            row["reason"] = _seller_reason(row)
            row["seller_thesis"] = row["reason"]
        return ensure_seller_flow_strategy(row)
    except Exception:
        return ensure_seller_flow_strategy(row)
    finally:
        if own_con and con is not None:
            try:
                con.close()
            except Exception:
                pass


def repair_seller_flow_display_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(rows, list):
        return list(rows or [])
    con = _connect()
    try:
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(repair_seller_flow_display_fields(dict(r), con) if isinstance(r, dict) else r)
        return out
    finally:
        try:
            con.close()
        except Exception:
            pass


def _final_seller_read(row: Dict[str, Any]) -> str:
    if row.get("earnings_conflict"):
        return "Earnings Conflict - No New Trade"
    labels = " | ".join(str(row.get(k) or "") for k in ("st_outlook", "mt_outlook", "lt_outlook"))
    bias = str(row.get("bias") or "")
    if "Skew Risk" in labels:
        return "Bullish Put Selling But Skew Risk" if "Bull" in bias else "Bearish Call Selling But Upside Skew Risk"
    if "Confirmed" in labels:
        if "Bull" in bias:
            return "Bullish Put Selling Confirmed"
        if "Bear" in bias:
            return "Bearish Call Selling Confirmed"
    if "Bull" in bias:
        return "Bullish Seller Flow"
    if "Bear" in bias:
        return "Bearish Seller Flow"
    if row.get("misaligned_with"):
        return "Mixed Seller Flow / Window Divergence"
    return "No Clear Seller Edge"


def _flow_confidence(row: Dict[str, Any]) -> int:
    score = 50 + min(35, abs(_safe_float(row.get("bias_score"), 0.0) or 0.0) * 5)
    src = str(row.get("skew_source") or "")
    if src in {"iv", "price_iv"}:
        score += 6
    elif src == "oi_proxy":
        score -= 3
    if row.get("oi_trend_aligned"):
        score += 4
    else:
        score -= 6
    mp_lt = _safe_float(row.get("max_pain_lt_chg_pct"), None)
    if mp_lt is not None:
        if "Bull" in str(row.get("bias")) and mp_lt > 0:
            score += 4
        elif "Bear" in str(row.get("bias")) and mp_lt < 0:
            score += 4
        elif abs(mp_lt) > 1.0:
            score -= 3

    # Sector / price-action / IV context now participates in confidence rather
    # than being display-only. This prevents OI-only rows from looking stronger
    # than the actual confirmation stack.
    score += _safe_float(row.get("sector_alignment_score"), 0.0) or 0.0
    pa_score = _safe_float(row.get("price_action_score"), None)
    score += _safe_float(row.get("price_location_score"), 0.0) or 0.0
    direction = _direction_from_windows(row)
    if pa_score is not None:
        if direction == "Bullish":
            score += max(-6, min(6, (pa_score - 50.0) / 5.0))
        elif direction == "Bearish":
            score += max(-6, min(6, (50.0 - pa_score) / 5.0))

    ivr = _safe_float(row.get("iv_rank"), None)
    code = str(row.get("suggested_strategy_code") or "")
    if ivr is not None:
        if code in {"PS", "CS", "IC"}:
            if ivr >= 55:
                score += 4
            elif ivr < 25:
                score -= 8
        elif "DEBIT" in code or "CALL" in code or "PUT" in code:
            if ivr <= 35:
                score += 4
            elif ivr > 70:
                score -= 5

    if row.get("earnings_conflict"):
        score -= 20
    if "Skew Risk" in str(row.get("final_seller_read") or ""):
        score -= 8
    uae_score = _safe_float(row.get("uae_alignment_score"), 0.0) or 0.0
    if uae_score >= 35:
        score += 5
    elif uae_score <= -20:
        score -= 8
    return int(max(0, min(100, round(score))))


def _action_from_seller_flow(row: Dict[str, Any]) -> str:
    if row.get("earnings_conflict"):
        return f"Avoid this timeframe because of earnings. {row.get('avoid_timeframe') or row.get('earnings_note')}"
    read = str(row.get("final_seller_read") or "")
    strat = row.get("suggested_strategy") or ""
    tf = row.get("suggested_timeframe") or ""
    trade_action = row.get("trade_action") or "WATCH"
    impl = row.get("implementation_bias") or row.get("buy_sell_guidance") or row.get("iv_action") or "IV rank unavailable"
    sector_txt = row.get("sector_alignment") or "Sector trend unavailable"
    sector_etf = row.get("sector_etf") or "sector ETF"
    price_txt = row.get("price_action_note") or "Price-action context unavailable"
    uae_note = str(row.get("uae_action_note") or "")
    ctx = (
        f" Action: {trade_action}. IV/implementation: {impl}. "
        f"Sector: {sector_etf} {row.get('sector_trend') or 'n/a'} - {sector_txt}. "
        f"Price action: {price_txt}."
    )
    if uae_note:
        ctx += f" UAE: {uae_note}"
    if "Confirmed" in read:
        return f"Validated seller-flow candidate. Suggested: {strat}; timeframe {tf}.{ctx} Require acceptable bid/ask and entry trigger before opening."
    if "Skew Risk" in read:
        return f"Do not treat OI/PCR alone as bullish/bearish. Skew conflicts with the seller read; wait for price confirmation or use smaller defined-risk debit structures only.{ctx}"
    if "Bullish" in read or "Bearish" in read:
        return f"Seller flow leans {read}. Suggested: {strat}; timeframe {tf}.{ctx}"
    if row.get("misaligned_with"):
        return f"Seller flow is mixed across windows. Use this as an alert, not an entry.{ctx}"
    return f"Neutral seller flow. Do not force a trade from OI alone.{ctx}"


def _seller_reason(row: Dict[str, Any]) -> str:
    skew_name = "IV skew" if row.get("skew_source") in {"iv", "price_iv"} else "OI-skew proxy" if row.get("skew_source") == "oi_proxy" else "skew unavailable"
    uae_bits = (
        f"UAE {row.get('uae_confirmation') or 'n/a'}; daily {row.get('uae_daily_regime') or 'n/a'} "
        f"marker {row.get('uae_daily_marker_label') or 'n/a'} age {row.get('uae_daily_marker_age') if row.get('uae_daily_marker_age') is not None else 'n/a'}; "
        f"weekly {row.get('uae_weekly_regime') or 'n/a'} marker {row.get('uae_weekly_marker_label') or 'n/a'} age {row.get('uae_weekly_marker_age') if row.get('uae_weekly_marker_age') is not None else 'n/a'}."
    )
    return (
        f"Seller-flow view: ST {row.get('st_days')}d {row.get('st_outlook')} "
        f"(OI {_fmt_pct(row.get('oi_st_pct'))}, PCR chg {_fmt_pct(row.get('pcr_st_chg_pct'))}, "
        f"RR/skew chg {_fmt_num(row.get('skew_st_chg'))}, max-pain chg {_fmt_pct(row.get('max_pain_st_chg_pct'))}), "
        f"MT {row.get('mt_days')}d {row.get('mt_outlook')} "
        f"(OI {_fmt_pct(row.get('oi_mt_pct'))}, PCR chg {_fmt_pct(row.get('pcr_mt_chg_pct'))}, "
        f"RR/skew chg {_fmt_num(row.get('skew_mt_chg'))}, max-pain chg {_fmt_pct(row.get('max_pain_mt_chg_pct'))}), "
        f"LT {row.get('lt_days')}d {row.get('lt_outlook')} "
        f"(OI {_fmt_pct(row.get('oi_lt_pct'))}, PCR chg {_fmt_pct(row.get('pcr_lt_chg_pct'))}, "
        f"RR/skew chg {_fmt_num(row.get('skew_lt_chg'))}, max-pain chg {_fmt_pct(row.get('max_pain_lt_chg_pct'))}). "
        f"Current PCR {_fmt_num(row.get('pcr'))}; max pain {_fmt_num(row.get('max_pain'))}; {skew_name}; {row.get('alignment_label')}. "
        f"IV rank {_fmt_num(row.get('iv_rank'),1)} ({row.get('iv_regime') or 'n/a'}; {row.get('iv_rank_source') or 'HV proxy'}): {row.get('buy_sell_guidance') or row.get('should_buy_sell') or 'n/a'}. "
        f"Sector check {row.get('sector_etf') or 'n/a'} trend {row.get('sector_trend') or 'n/a'} - {row.get('sector_alignment') or 'n/a'}. "
        f"Price action: {row.get('price_action_note') or 'n/a'}. "
        f"Support/resistance distance: {row.get('price_location_note') or 'n/a'}. "
        f"Decision/action: {row.get('trade_action') or 'n/a'} - {row.get('trade_action_reason') or 'n/a'}. "
        f"Strategy/timeframe: {row.get('suggested_strategy')} over {row.get('suggested_timeframe')}. {uae_bits} {row.get('earnings_note') or ''}"
    )


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def run_oi_buildup_screener(
    st_days: int = 3,
    mt_days: int = 10,
    lt_days: int = 30,
    watchlist_id: Optional[Any] = None,
    max_symbols: Optional[int] = None,
    save_cache: bool = True,
    warm_watchlist: bool = True,
) -> Dict[str, Any]:
    """Run the seller-flow scanner over the local options table."""
    st_days = max(1, min(30, _safe_int(st_days, 3)))
    mt_days = max(2, min(90, _safe_int(mt_days, 10)))
    lt_days = max(5, min(180, _safe_int(lt_days, 30)))
    if mt_days < st_days:
        mt_days = st_days
    if lt_days < mt_days:
        lt_days = mt_days

    max_rows = max(lt_days + 2, 32)
    today = date.today().isoformat()
    completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    errors: List[Any] = []

    con = _connect()
    try:
        _ensure_options_table(con)
        watchlist_syms = _watchlist_symbols(con, watchlist_id)
        sym_exp_rows = con.execute(
            "SELECT symbol, MIN(expiration) AS expiry FROM options WHERE expiration>=? GROUP BY symbol",
            (today,),
        ).fetchall()
        sym_exp = {
            str(r["symbol"] or "").strip().upper(): str(r["expiry"] or "")
            for r in sym_exp_rows
            if str(r["symbol"] or "").strip() and str(r["expiry"] or "").strip()
        }
        if watchlist_syms is not None:
            wl = set(watchlist_syms)
            sym_exp = {s: e for s, e in sym_exp.items() if s in wl}
            if not sym_exp and wl and warm_watchlist:
                try:
                    from ..services.market import fetch_store_for
                    with ThreadPoolExecutor(max_workers=6) as ex:
                        list(ex.map(lambda s: fetch_store_for(s), list(sorted(wl))[:40]))
                    sym_exp_rows = con.execute(
                        "SELECT symbol, MIN(expiration) AS expiry FROM options WHERE expiration>=? GROUP BY symbol",
                        (today,),
                    ).fetchall()
                    sym_exp = {
                        str(r["symbol"] or "").strip().upper(): str(r["expiry"] or "")
                        for r in sym_exp_rows
                        if str(r["symbol"] or "").strip() and str(r["expiry"] or "").strip()
                    }
                    sym_exp = {s: e for s, e in sym_exp.items() if s in wl}
                except Exception as exc:
                    errors.append({"watchlist_warmup": str(exc)[:220]})

        all_syms = sorted(sym_exp.keys())
        total_universe = len(all_syms)
        if max_symbols is not None:
            all_syms = all_syms[: max(1, min(1000, _safe_int(max_symbols, 250)))]
        if not all_syms:
            return {
                "ok": True, "model": "seller_flow_pcr_oi_skew_maxpain", "results": [], "count": 0,
                "date": today, "completed_at": completed_at, "st_days": st_days, "mt_days": mt_days,
                "lt_days": lt_days, "watchlist_id": watchlist_id, "total_universe": total_universe,
                "errors": [], "error_count": 0,
            }

        cutoff = (date.today() - timedelta(days=max_rows + 4)).isoformat()
        placeholders = ",".join(["?"] * len(all_syms))
        oi_rows = con.execute(
            f"""
            SELECT symbol, date,
                   SUM(CASE WHEN LOWER(type) LIKE 'c%' THEN COALESCE(oi,0) ELSE 0 END) AS call_oi,
                   SUM(CASE WHEN LOWER(type) LIKE 'p%' THEN COALESCE(oi,0) ELSE 0 END) AS put_oi,
                   SUM(CASE WHEN LOWER(type) LIKE 'c%' THEN COALESCE(volume,0) ELSE 0 END) AS call_vol,
                   SUM(CASE WHEN LOWER(type) LIKE 'p%' THEN COALESCE(volume,0) ELSE 0 END) AS put_vol
            FROM options
            WHERE date >= ?
              AND symbol IN ({placeholders})
              AND expiration >= date
            GROUP BY symbol, date
            ORDER BY symbol, date DESC
            """,
            [cutoff] + all_syms,
        ).fetchall()

        oi_by_sym: Dict[str, List[OiRow]] = defaultdict(list)
        for r in oi_rows:
            sym = str(r["symbol"] or "").upper()
            oi_by_sym[sym].append(
                (
                    str(r["date"] or ""),
                    _safe_float(r["call_oi"], 0.0) or 0.0,
                    _safe_float(r["put_oi"], 0.0) or 0.0,
                    _safe_float(r["call_vol"], 0.0) or 0.0,
                    _safe_float(r["put_vol"], 0.0) or 0.0,
                )
            )

        # Actual underlying price history.  Do not use OI-weighted strike as a
        # price proxy; that caused SPY to display wall/magnet levels as spot.
        sector_map_by_sym = _load_sector_map(con, all_syms)
        sector_etfs = sorted({str(v.get("sector_etf") or "").upper() for v in sector_map_by_sym.values() if str(v.get("sector_etf") or "").strip()})
        price_symbols = sorted(set(all_syms + sector_etfs))
        price_cache = _load_price_cache(con, price_symbols, cutoff)
        spot_by_sym = _load_price_points(con, price_symbols, cutoff)

        stock_enrich_by_sym, sector_ctx_by_etf, uae_fallback_by_sym = _load_context_enrichments(con, all_syms, sector_map_by_sym)

        flow_by_sym = _load_flow_snapshots(con, all_syms, cutoff)
        latest_context_by_sym = _load_latest_wall_contexts(con, all_syms, cutoff, price_cache)
        earnings_map = _load_earnings_map(con, all_syms)
        # UAE v5 context is display/action guidance only.  It reuses Scanner Builder
        # primitives against local cached bars when available, then falls back to a
        # local price-cache UAE-lite calculation so rows do not show blank context.
        uae_context_by_sym = _load_uae_contexts(all_syms)

        rows_out: List[Dict[str, Any]] = []
        for sym in all_syms:
            try:
                rows = oi_by_sym.get(sym, [])
                if len(rows) < 2:
                    continue
                latest = rows[0]
                call_oi_now = _safe_float(latest[1], 0.0) or 0.0
                put_oi_now = _safe_float(latest[2], 0.0) or 0.0
                total_oi = call_oi_now + put_oi_now
                if total_oi <= 0:
                    continue
                pcr = round(put_oi_now / max(1.0, call_oi_now), 3)
                pts = spot_by_sym.get(sym, [])
                spot_proxy = pts[-1][1] if pts else None

                st_stats = _window_oi_stats(rows, st_days)
                mt_stats = _window_oi_stats(rows, mt_days)
                lt_stats = _window_oi_stats(rows, lt_days)

                snaps = flow_by_sym.get(sym, [])
                latest_flow = snaps[0] if snaps else {}
                st_flow = _window_flow_stats(snaps, st_days)
                mt_flow = _window_flow_stats(snaps, mt_days)
                lt_flow = _window_flow_stats(snaps, lt_days)
                # Latest flow spot is now actual price_cache/underlying only; prefer
                # the latest cached market close when available.
                latest_px = _latest_close_from_cache(price_cache, sym)
                if latest_px is not None:
                    spot_proxy = round(float(latest_px), 2)
                elif latest_flow.get("spot"):
                    spot_proxy = _safe_float(latest_flow.get("spot"), spot_proxy, 2) or spot_proxy

                pr_st = _price_delta(pts, st_days)
                pr_mt = _price_delta(pts, mt_days)
                pr_lt = _price_delta(pts, lt_days)

                st_seller = seller_oi_signal(st_stats, pr_st, st_flow.get("risk_reversal_chg"), st_flow.get("max_pain_chg_pct"), st_flow.get("skew_source"))
                mt_seller = seller_oi_signal(mt_stats, pr_mt, mt_flow.get("risk_reversal_chg"), mt_flow.get("max_pain_chg_pct"), mt_flow.get("skew_source"))
                lt_seller = seller_oi_signal(lt_stats, pr_lt, lt_flow.get("risk_reversal_chg"), lt_flow.get("max_pain_chg_pct"), lt_flow.get("skew_source"))
                st_legacy = price_oi_signal(st_stats["oi_pct"], pr_st)
                mt_legacy = price_oi_signal(mt_stats["oi_pct"], pr_mt)
                lt_legacy = price_oi_signal(lt_stats["oi_pct"], pr_lt)
                bias, bias_score = _bias_from_seller_windows(st_seller, mt_seller, lt_seller, pcr, st_stats, mt_stats, lt_stats)

                row = {
                    "symbol": sym,
                    "spot": spot_proxy,
                    "spot_proxy": spot_proxy,
                    "spot_source": "price_cache" if spot_proxy is not None else ("option_underlying" if latest_flow.get("spot") else "unavailable"),
                    "spot_price_date": _latest_close_date_from_cache(price_cache, sym),
                    "expiry": sym_exp.get(sym),
                    "analytics_expiry": latest_flow.get("expiration"),
                    "has_futures": sym.upper() in FUTURES_ROOTS,
                    "call_oi": int(call_oi_now),
                    "put_oi": int(put_oi_now),
                    "total_oi": int(total_oi),
                    "pcr": pcr,
                    "pc_ratio": pcr,
                    "bias": bias,
                    "bias_score": bias_score,
                    "latest_date": latest[0],
                    "st_days": st_days,
                    "mt_days": mt_days,
                    "lt_days": lt_days,
                    "st_base_date": st_stats["base"][0] if st_stats.get("base") else None,
                    "mt_base_date": mt_stats["base"][0] if mt_stats.get("base") else None,
                    "lt_base_date": lt_stats["base"][0] if lt_stats.get("base") else None,
                    "skew_source": latest_flow.get("skew_source") or st_flow.get("skew_source") or "unavailable",
                    "skew_is_proxy": int(latest_flow.get("skew_is_proxy") or 0),
                    "put_skew": latest_flow.get("put_skew"),
                    "call_skew": latest_flow.get("call_skew"),
                    "risk_reversal": latest_flow.get("risk_reversal"),
                    "max_pain": latest_flow.get("max_pain"),
                    "max_pain_date": latest_flow.get("date"),
                    "put_wall_strike": latest_flow.get("put_wall_strike"),
                    "put_wall_oi": latest_flow.get("put_wall_oi"),
                    "put_long_strike": latest_flow.get("put_long_strike"),
                    "call_wall_strike": latest_flow.get("call_wall_strike"),
                    "call_wall_oi": latest_flow.get("call_wall_oi"),
                    "call_long_strike": latest_flow.get("call_long_strike"),
                    "strike_step": latest_flow.get("strike_step"),
                    "data_quality_note": "True IV/price-based skew" if latest_flow.get("skew_source") in {"iv", "price_iv"} else "Skew shown as OI-skew proxy because historical IV/spot price was not available.",
                    # Semantic ST fields
                    "price_st_pct": pr_st,
                    "oi_st_pct": st_stats["oi_pct"],
                    "call_oi_st_pct": st_stats["call_oi_pct"],
                    "put_oi_st_pct": st_stats["put_oi_pct"],
                    "pcr_st_base": st_stats["pcr_base"],
                    "pcr_st_now": st_stats["pcr_now"],
                    "pcr_st_chg": st_stats["pcr_chg"],
                    "pcr_st_chg_pct": st_stats["pcr_chg_pct"],
                    "side_edge_st": st_stats["side_edge"],
                    "vol_st_pct": st_stats["vol_pct"],
                    "st_outlook": st_seller[0],
                    "st_seller_signal": st_seller[0],
                    "st_seller_bias": st_seller[1],
                    "st_seller_score": st_seller[2],
                    "st_price_context": st_seller[3],
                    "st_price_oi_signal": st_legacy,
                    "skew_st_chg": st_flow.get("risk_reversal_chg"),
                    "put_skew_st_chg": st_flow.get("put_skew_chg"),
                    "call_skew_st_chg": st_flow.get("call_skew_chg"),
                    "max_pain_st_base": st_flow.get("max_pain_base"),
                    "max_pain_st_chg": st_flow.get("max_pain_chg"),
                    "max_pain_st_chg_pct": st_flow.get("max_pain_chg_pct"),
                    "max_pain_st_shift": st_flow.get("max_pain_shift"),
                    # Semantic MT fields
                    "price_mt_pct": pr_mt,
                    "oi_mt_pct": mt_stats["oi_pct"],
                    "call_oi_mt_pct": mt_stats["call_oi_pct"],
                    "put_oi_mt_pct": mt_stats["put_oi_pct"],
                    "pcr_mt_base": mt_stats["pcr_base"],
                    "pcr_mt_now": mt_stats["pcr_now"],
                    "pcr_mt_chg": mt_stats["pcr_chg"],
                    "pcr_mt_chg_pct": mt_stats["pcr_chg_pct"],
                    "side_edge_mt": mt_stats["side_edge"],
                    "vol_mt_pct": mt_stats["vol_pct"],
                    "mt_outlook": mt_seller[0],
                    "mt_seller_signal": mt_seller[0],
                    "mt_seller_bias": mt_seller[1],
                    "mt_seller_score": mt_seller[2],
                    "mt_price_context": mt_seller[3],
                    "mt_price_oi_signal": mt_legacy,
                    "skew_mt_chg": mt_flow.get("risk_reversal_chg"),
                    "put_skew_mt_chg": mt_flow.get("put_skew_chg"),
                    "call_skew_mt_chg": mt_flow.get("call_skew_chg"),
                    "max_pain_mt_base": mt_flow.get("max_pain_base"),
                    "max_pain_mt_chg": mt_flow.get("max_pain_chg"),
                    "max_pain_mt_chg_pct": mt_flow.get("max_pain_chg_pct"),
                    "max_pain_mt_shift": mt_flow.get("max_pain_shift"),
                    # Semantic LT fields
                    "price_lt_pct": pr_lt,
                    "oi_lt_pct": lt_stats["oi_pct"],
                    "call_oi_lt_pct": lt_stats["call_oi_pct"],
                    "put_oi_lt_pct": lt_stats["put_oi_pct"],
                    "pcr_lt_base": lt_stats["pcr_base"],
                    "pcr_lt_now": lt_stats["pcr_now"],
                    "pcr_lt_chg": lt_stats["pcr_chg"],
                    "pcr_lt_chg_pct": lt_stats["pcr_chg_pct"],
                    "side_edge_lt": lt_stats["side_edge"],
                    "vol_lt_pct": lt_stats["vol_pct"],
                    "lt_outlook": lt_seller[0],
                    "lt_seller_signal": lt_seller[0],
                    "lt_seller_bias": lt_seller[1],
                    "lt_seller_score": lt_seller[2],
                    "lt_price_context": lt_seller[3],
                    "lt_price_oi_signal": lt_legacy,
                    "skew_lt_chg": lt_flow.get("risk_reversal_chg"),
                    "put_skew_lt_chg": lt_flow.get("put_skew_chg"),
                    "call_skew_lt_chg": lt_flow.get("call_skew_chg"),
                    "max_pain_lt_base": lt_flow.get("max_pain_base"),
                    "max_pain_lt_chg": lt_flow.get("max_pain_chg"),
                    "max_pain_lt_chg_pct": lt_flow.get("max_pain_chg_pct"),
                    "max_pain_lt_shift": lt_flow.get("max_pain_shift"),
                    # Legacy UI aliases; names are historical labels but day counts are exposed in st/mt/lt days.
                    "price_1d_pct": pr_st,
                    "oi_1d_chg": 0,
                    "oi_1d_pct": st_stats["oi_pct"],
                    "call_oi_1d_pct": st_stats["call_oi_pct"],
                    "put_oi_1d_pct": st_stats["put_oi_pct"],
                    "pcr_1d_base": st_stats["pcr_base"],
                    "pcr_1d_now": st_stats["pcr_now"],
                    "pcr_1d_chg": st_stats["pcr_chg"],
                    "pcr_1d_chg_pct": st_stats["pcr_chg_pct"],
                    "vol_1d_pct": st_stats["vol_pct"],
                    "skew_1d_chg": st_flow.get("risk_reversal_chg"),
                    "max_pain_1d_chg_pct": st_flow.get("max_pain_chg_pct"),
                    "price_5d_pct": pr_mt,
                    "oi_5d_chg": 0,
                    "oi_5d_pct": mt_stats["oi_pct"],
                    "call_oi_5d_pct": mt_stats["call_oi_pct"],
                    "put_oi_5d_pct": mt_stats["put_oi_pct"],
                    "pcr_5d_base": mt_stats["pcr_base"],
                    "pcr_5d_now": mt_stats["pcr_now"],
                    "pcr_5d_chg": mt_stats["pcr_chg"],
                    "pcr_5d_chg_pct": mt_stats["pcr_chg_pct"],
                    "vol_5d_pct": mt_stats["vol_pct"],
                    "skew_5d_chg": mt_flow.get("risk_reversal_chg"),
                    "max_pain_5d_chg_pct": mt_flow.get("max_pain_chg_pct"),
                    "price_15d_pct": pr_lt,
                    "oi_15d_chg": 0,
                    "oi_15d_pct": lt_stats["oi_pct"],
                    "call_oi_15d_pct": lt_stats["call_oi_pct"],
                    "put_oi_15d_pct": lt_stats["put_oi_pct"],
                    "pcr_15d_base": lt_stats["pcr_base"],
                    "pcr_15d_now": lt_stats["pcr_now"],
                    "pcr_15d_chg": lt_stats["pcr_chg"],
                    "pcr_15d_chg_pct": lt_stats["pcr_chg_pct"],
                    "vol_15d_pct": lt_stats["vol_pct"],
                    "skew_15d_chg": lt_flow.get("risk_reversal_chg"),
                    "max_pain_15d_chg_pct": lt_flow.get("max_pain_chg_pct"),
                }
                _alignment(row)
                _apply_latest_context(row, latest_context_by_sym.get(sym))
                sec_meta = sector_map_by_sym.get(sym, {})
                sec_ctx = sector_ctx_by_etf.get(str(sec_meta.get("sector_etf") or "").upper())
                _apply_enrichment_context(row, stock_enrich_by_sym.get(sym), sec_meta, sec_ctx)
                row.update(_strategy_from_seller_flow(row, earnings_map.get(sym)))
                # Prefer exact Scanner Builder UAE, but use the local fallback if the
                # shared primitive engine has no cached bars for this symbol.
                uae_ctx = uae_context_by_sym.get(sym)
                if not (uae_ctx and uae_ctx.get("uae_context_available")):
                    uae_ctx = uae_fallback_by_sym.get(sym) or uae_ctx
                _apply_uae_to_seller_row(row, uae_ctx)
                row.update(_buy_sell_recommendation(row))
                _apply_buy_sell_action_fields(row)
                row["final_seller_read"] = _final_seller_read(row)
                row["flow_confidence"] = _flow_confidence(row)
                row["confidence"] = row["flow_confidence"]
                row["suggested_action"] = _action_from_seller_flow(row)
                row["reason"] = _seller_reason(row)
                row["seller_thesis"] = row["reason"]
                rows_out.append(row)
            except Exception as exc:
                errors.append({"symbol": sym, "error": str(exc)[:220]})

        rows_out.sort(
            key=lambda r: (
                _safe_float(r.get("flow_confidence"), 0.0) or 0.0,
                abs(_safe_float(r.get("bias_score"), 0.0) or 0.0),
                r.get("divergence_score") or 0,
                abs(_safe_float(r.get("pcr_lt_chg_pct"), 0.0) or 0.0),
                r.get("total_oi") or 0,
            ),
            reverse=True,
        )

        if save_cache:
            try:
                con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
                con.execute(
                    "INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                    (_cache_key("oi_buildup_scan", watchlist_id), json.dumps(rows_out[:1000], default=str), completed_at),
                )
                con.execute(
                    "INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                    (_cache_key("oib_completed_at", watchlist_id), "oib_ts", completed_at),
                )
                con.commit()
            except Exception as exc:
                errors.append({"cache": str(exc)[:220]})

        return {
            "ok": True,
            "model": "seller_flow_pcr_oi_skew_maxpain",
            "results": rows_out,
            "count": len(rows_out),
            "date": today,
            "completed_at": completed_at,
            "st_days": st_days,
            "mt_days": mt_days,
            "lt_days": lt_days,
            "watchlist_id": watchlist_id,
            "symbols_scanned": len(all_syms),
            "total_universe": total_universe,
            "errors": errors[:20],
            "error_count": len(errors),
            "notes": [
                "Max pain is calculated from historical strike-level OI snapshots.",
                "Skew uses historical IV when available, price-implied IV when possible, otherwise an explicitly labelled OI-skew proxy.",
                "Strategy/timeframe suggestions are candidates only and are blocked when cached earnings conflict with the holding window.",
                "UAE context reuses Scanner Builder's Pine v5-aligned primitives when available, then falls back to local price/option-underlying UAE-lite context.",
                "IV rank uses stored option-IV history when available, otherwise a historical-volatility proxy from local price/underlying history; short histories are labelled as estimates instead of showing misleading n/a or extreme 0/100 ranks.",
                "Sector regime uses the mapped sector ETF (for example Consumer Staples -> XLP) from local price_cache trend context.",
            ],
        }
    except Exception as exc:
        return {
            "ok": False,
            "model": "seller_flow_pcr_oi_skew_maxpain",
            "results": [],
            "count": 0,
            "date": today,
            "completed_at": completed_at,
            "st_days": st_days,
            "mt_days": mt_days,
            "lt_days": lt_days,
            "watchlist_id": watchlist_id,
            "errors": [str(exc)[:500]],
            "error_count": 1,
        }
    finally:
        con.close()



def refresh_seller_flow_display_context(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Repair cached Seller Flow rows with current display price and latest walls.

    Cached scans created by older builds may contain an OI-weighted strike in the
    spot field and blank support/resistance/max-pain values.  This function is used
    by the cached API to make Load Saved safe without requiring a fresh scan.
    """
    data = [dict(r) for r in (rows or []) if isinstance(r, dict)]
    syms = sorted({str(r.get("symbol") or "").upper() for r in data if str(r.get("symbol") or "").strip()})
    if not syms:
        return data
    con = _connect()
    try:
        _ensure_options_table(con)
        cutoff = (date.today() - timedelta(days=45)).isoformat()
        pc = _load_price_cache(con, syms, cutoff)
        ctx = _load_latest_wall_contexts(con, syms, cutoff, pc)
        sector_map = _load_sector_map(con, syms)
        stock_ctx, sector_ctx_map, uae_fb = _load_context_enrichments(con, syms, sector_map)
        uae_ctx = _load_uae_contexts(syms)
        for r in data:
            sym = str(r.get("symbol") or "").upper()
            latest_px = _latest_close_from_cache(pc, sym)
            if latest_px is not None:
                r["spot"] = round(float(latest_px), 2)
                r["spot_proxy"] = r["spot"]
                r["spot_source"] = "price_cache"
                r["spot_price_date"] = _latest_close_date_from_cache(pc, sym)
            _apply_latest_context(r, ctx.get(sym))
            sec_meta = sector_map.get(sym, {})
            _apply_enrichment_context(r, stock_ctx.get(sym), sec_meta, sector_ctx_map.get(str(sec_meta.get("sector_etf") or "").upper()))
            ensure_seller_flow_strategy(r)
            exact = uae_ctx.get(sym)
            if not (exact and exact.get("uae_context_available")):
                exact = uae_fb.get(sym) or exact
            _apply_uae_to_seller_row(r, exact)
            r.update(_buy_sell_recommendation(r))
            _apply_buy_sell_action_fields(r)
            r["flow_confidence"] = _flow_confidence(r)
            r["confidence"] = r["flow_confidence"]
            r["suggested_action"] = _action_from_seller_flow(r)
            r["reason"] = _seller_reason(r)
            r["seller_thesis"] = r["reason"]
        return data
    except Exception:
        return data
    finally:
        con.close()


def filter_oi_trend_misalignment(rows: Iterable[Dict[str, Any]], mode: str = "lt_vs_st_or_mt") -> List[Dict[str, Any]]:
    """Return stocks where LT aggregate OI trend is not aligned to ST and/or MT."""
    out: List[Dict[str, Any]] = []
    mode = (mode or "lt_vs_st_or_mt").lower()
    for row in rows or []:
        _alignment(row)
        st_bad = not bool(row.get("lt_st_aligned"))
        mt_bad = not bool(row.get("lt_mt_aligned"))
        if mode in {"lt_vs_st_and_mt", "both"}:
            keep = st_bad and mt_bad
        elif mode in {"lt_vs_st", "st"}:
            keep = st_bad
        elif mode in {"lt_vs_mt", "mt"}:
            keep = mt_bad
        else:
            keep = st_bad or mt_bad
        if keep:
            row["suggested_action"] = row.get("suggested_action") or _action_from_seller_flow(row)
            row["reason"] = row.get("reason") or _seller_reason(row)
            out.append(row)
    out.sort(
        key=lambda r: (r.get("divergence_score") or 0, abs(_safe_float(r.get("pcr_lt_chg_pct"), 0.0) or 0.0), r.get("total_oi") or 0),
        reverse=True,
    )
    return out
