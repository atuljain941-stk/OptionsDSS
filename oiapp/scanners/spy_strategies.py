# oiapp/scanners/spy_strategies.py
"""
SPY Daily & Weekly Strategy Engine
- Pulls live OI from DB, computes technicals via yfinance
- RSI-14, EMA-90(RSI), ATR, BB%B, OI walls
- Generates 0-2 DTE daily + 5-10 DTE weekly strategies with PoP
"""
import sqlite3, math, json, os
from pathlib import Path
from datetime import date, timedelta, datetime
from flask import Blueprint, jsonify, request, current_app
import yfinance as yf
try:
    from scipy.stats import norm as _norm
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH
try:
    from ..services.weekly_oi import (
        build_weekly_oi_context as _shared_weekly_oi_context,
        latest_option_rows as _shared_latest_option_rows,
        option_mid as _shared_option_mid,
        strike_interval_from_rows as _shared_strike_interval,
    )
except Exception:  # pragma: no cover - package import fallback
    _shared_weekly_oi_context = None
    _shared_latest_option_rows = None
    _shared_option_mid = None
    _shared_strike_interval = None
try:
    from ..services.oi_significance import threshold_from_args as _oi_sig_threshold_from_args
except Exception:  # pragma: no cover
    def _oi_sig_threshold_from_args(args, default=30.0):
        try:
            for key in ("min_change_pct", "oi_sig_pct", "oi_change_pct_threshold", "threshold_pct"):
                if args.get(key) not in (None, ""):
                    return max(0.0, float(args.get(key)))
        except Exception:
            pass
        return float(default)
spy_bp = Blueprint("spy_bp", __name__, url_prefix="/spy")

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def _today():
    return date.today().strftime("%Y-%m-%d")


def _finite_number(value, default=None):
    try:
        f = float(value)
    except Exception:
        return default
    return f if math.isfinite(f) else default


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    try:
        if hasattr(value, 'item') and not isinstance(value, (str, bytes, bytearray)):
            return _json_safe(value.item())
    except Exception:
        pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except Exception:
        return int(default)


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def _rows_total_oi(rows):
    total = 0
    for r in rows or []:
        try:
            total += int(float((r or {}).get("oi") or 0))
        except Exception:
            pass
    return int(total)


def _oi_change_filter_context(symbol, rows, expiry=None, source="", total_oi=None, min_change_pct=None, min_expiry_total_oi=None, min_strike_oi=None, min_abs_change=None):
    """Liquidity gate and thresholds for strike-level OI-change significance.

    Used only by GEX Plan and Weekly Plan.  The normal OI wall level can still
    be selected from current OI, but daily/weekly OI *change* only contributes
    when the selected expiry/aggregate is liquid enough and the strike-level
    change is large both in percentage and absolute contracts.
    """
    sym = str(symbol or "").upper().strip()
    expiry_total = int(total_oi if total_oi is not None else _rows_total_oi(rows))
    min_expiry_total = int(min_expiry_total_oi if min_expiry_total_oi is not None else _env_int("OI_SIGNIFICANCE_MIN_EXPIRY_TOTAL_OI", 50000))
    min_strike_oi = int(min_strike_oi if min_strike_oi is not None else _env_int("OI_SIGNIFICANCE_MIN_STRIKE_OI", 1000))
    min_abs_change = int(min_abs_change if min_abs_change is not None else _env_int("OI_SIGNIFICANCE_MIN_ABS_CHANGE", 500))
    min_change_pct = float(min_change_pct if min_change_pct is not None else _env_float("OI_SIGNIFICANCE_MIN_CHANGE_PCT", 30.0))
    qualified = bool(expiry_total >= min_expiry_total)
    reason = (
        f"qualified: total expiry OI {expiry_total:,} >= {min_expiry_total:,}"
        if qualified else
        f"not qualified: total expiry OI {expiry_total:,} < {min_expiry_total:,}; OI change ignored"
    )
    return {
        "enabled": True,
        "qualified": qualified,
        "symbol": sym,
        "expiry": str(expiry or "")[:10] if expiry else None,
        "source": str(source or ""),
        "expiry_total_oi": expiry_total,
        "min_expiry_total_oi": min_expiry_total,
        "min_strike_oi": min_strike_oi,
        "min_abs_change": min_abs_change,
        "min_change_pct": min_change_pct,
        "reason": reason,
        "method": (
            f"Strike-level OI change counts only when expiry total OI >= {min_expiry_total:,}, "
            f"abs(ΔOI) >= {min_abs_change:,}, abs(ΔOI%) >= {min_change_pct:g}%, "
            f"and max(prev OI,current OI) >= {min_strike_oi:,}."
        ),
    }


def _oi_change_sig_flags(oi, prev_oi, oi_change, ctx=None):
    """Return strike-level OI-change significance flags for a wall row."""
    oi = int(_finite_number(oi, 0) or 0)
    prev_oi = int(_finite_number(prev_oi, 0) or 0)
    oi_change = int(_finite_number(oi_change, 0) or 0)
    base_oi = max(oi, prev_oi)
    pct = (oi_change / max(1, prev_oi) * 100.0) if prev_oi else None
    if not ctx:
        # Backward-compatible behavior for callers that have not opted into the
        # liquidity/significance gate.
        sig_build = oi_change > 0
        sig_remove = oi_change < -max(500, oi * 0.10)
        reason = "legacy wall scoring"
    else:
        qualified = bool(ctx.get("qualified"))
        min_strike_oi = int(ctx.get("min_strike_oi") or 0)
        min_abs = int(ctx.get("min_abs_change") or 0)
        min_pct = float(ctx.get("min_change_pct") or 0.0)
        pct_abs_ok = pct is not None and abs(pct) >= min_pct
        abs_ok = abs(oi_change) >= min_abs
        size_ok = base_oi >= min_strike_oi
        sig_build = bool(qualified and oi_change > 0 and pct_abs_ok and abs_ok and size_ok)
        sig_remove = bool(qualified and oi_change < 0 and pct_abs_ok and abs_ok and size_ok)
        if not qualified:
            reason = ctx.get("reason") or "expiry total OI below threshold"
        elif not size_ok:
            reason = f"strike OI {base_oi:,} below {min_strike_oi:,}"
        elif not abs_ok:
            reason = f"abs ΔOI {abs(oi_change):,} below {min_abs:,}"
        elif not pct_abs_ok:
            reason = f"abs ΔOI% {abs(pct or 0):.1f}% below {min_pct:g}%"
        else:
            reason = "significant OI build" if sig_build else "significant OI removal" if sig_remove else "OI change not directional"
    return {
        "pct": round(pct, 2) if pct is not None else None,
        "base_oi": base_oi,
        "significant_build": sig_build,
        "significant_removal": sig_remove,
        "significant": bool(sig_build or sig_remove),
        "reason": reason,
    }


def _last_finite(values, default=None):
    try:
        seq = list(values)
    except Exception:
        return default
    for v in reversed(seq):
        f = _finite_number(v)
        if f is not None:
            return f
    return default

def _future_exps(symbol="SPY"):
    con = _conn()
    rows = con.execute(
        "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration",
        (symbol, _today())
    ).fetchall()
    con.close()
    return [r["expiration"] for r in rows]


def _expiry_dte(expiry, today=None):
    """Calendar DTE for an expiry string; returns 0 for past/invalid dates.

    GEX/Daily Plan routes may be called with a user-selected expiry.  Older
    builds referenced a helper named _pick_exp here, but that helper was missing
    in some packaged versions, causing /spy/daily_plan to 500.  Keep the date
    math local and dependency-free so selected-expiry GEX plans always work.
    """
    try:
        d = date.fromisoformat(str(expiry)[:10])
        base = today or date.today()
        return max(0, (d - base).days)
    except Exception:
        return 0


def _pick_exp(expirations, min_dte=0, max_dte=999, fallback_index=0):
    """Pick the nearest expiry inside a DTE window, with safe fallback.

    Returns (expiry, dte).  This is used by the legacy daily/full GEX plan
    endpoints.  It intentionally uses calendar DTE to match the rest of this
    module and avoid relying on yfinance/business-day availability.
    """
    today = date.today()
    candidates = []
    for e in expirations or []:
        try:
            d = date.fromisoformat(str(e)[:10])
        except Exception:
            continue
        raw_dte = (d - today).days
        if raw_dte < 0:
            continue
        dte = raw_dte
        candidates.append((str(e)[:10], d, dte))
    if not candidates:
        return None, 0
    in_window = [c for c in candidates if int(min_dte) <= c[2] <= int(max_dte)]
    if in_window:
        e, _d, dte = sorted(in_window, key=lambda x: (x[2], x[1]))[0]
        return e, dte
    try:
        idx = int(fallback_index or 0)
    except Exception:
        idx = 0
    ordered = sorted(candidates, key=lambda x: x[1])
    idx = max(0, min(idx, len(ordered) - 1))
    e, _d, dte = ordered[idx]
    return e, dte

def _oi_rows(symbol, expiry):
    """
    Latest OI snapshot for one expiration, enriched with day-over-day OI change.

    The GEX plan needs both total/current OI and fresh OI build.  The options
    table stores snapshots by date, so we compare the latest snapshot with the
    prior available snapshot for the same symbol/expiry/strike/type.  If the
    prior snapshot is missing, oi_change is 0 rather than raising.

    Walks back past any prior date whose full per-strike OI snapshot is
    IDENTICAL to the latest one (not just adjacent-by-date), instead of
    blindly diffing against whatever the second-most-recent date happens
    to be. This ports the exact fix /api/oi_change (OI Viewer) already
    had for the same underlying problem -- when a day's fetch doesn't
    actually pull fresh OI (a quote-only/failed fetch that re-stores
    yesterday's numbers under today's date), blindly comparing the two
    most recent dates produces an all-zero diff for every strike even
    though real data exists further back. Wall Term Structure was
    hitting exactly this: OI Viewer's own duplicate-skip logic already
    handled it there, this function just never had the same fix.
    """
    if not expiry:
        return []
    con = _conn()
    dates = con.execute(
        "SELECT DISTINCT date FROM options WHERE symbol=? AND expiration=? ORDER BY date DESC LIMIT 15",
        (symbol, expiry),
    ).fetchall()
    if not dates:
        con.close(); return []
    date_list = [r["date"] for r in dates]
    latest_date = date_list[0]

    def _fetch(dt):
        return con.execute("""
            SELECT type, strike, SUM(oi) AS oi, SUM(volume) AS vol
            FROM options WHERE symbol=? AND expiration=? AND date=?
            GROUP BY type, strike
        """, (symbol, expiry, dt)).fetchall()

    def _signature(rows):
        pairs = []
        for r in rows:
            try:
                pairs.append((str(r["type"]).lower(), round(float(r["strike"]), 4), int(r["oi"] or 0)))
            except Exception:
                continue
        return tuple(sorted(pairs))

    latest_rows_raw = _fetch(latest_date)
    latest_sig = _signature(latest_rows_raw)

    prev_date = None
    prev_map = {}
    for candidate_date in date_list[1:]:
        candidate_rows = _fetch(candidate_date)
        if _signature(candidate_rows) == latest_sig:
            continue  # identical snapshot -- not a real second data point, keep walking back
        prev_date = candidate_date
        for r in candidate_rows:
            try:
                prev_map[(str(r["type"]).lower(), float(r["strike"]))] = int(r["oi"] or 0)
            except Exception:
                pass
        break

    con.close()

    latest_rows = [r for r in latest_rows_raw if (r["oi"] or 0) > 0]
    out = []
    for r in latest_rows:
        d = dict(r)
        try:
            typ = str(d.get("type") or "").lower()
            strike = float(d.get("strike") or 0)
            oi_now = int(d.get("oi") or 0)
            oi_prev = int(prev_map.get((typ, strike), 0)) if prev_map else 0
            oi_chg = oi_now - oi_prev if prev_map else 0
            d["type"] = typ
            d["strike"] = strike
            d["oi"] = oi_now
            d["vol"] = int(d.get("vol") or 0)
            d["oi_change"] = oi_chg
            d["oi_change_pct"] = round(oi_chg / max(1, oi_prev) * 100, 2) if oi_prev else None
            d["date"] = latest_date
            d["prev_date"] = prev_date
            d["prev_oi"] = oi_prev
        except Exception:
            d.setdefault("oi_change", 0)
            d.setdefault("oi_change_pct", None)
        out.append(d)
    out.sort(key=lambda d: (d.get("strike") or 0, d.get("type") or ""))
    return out

def _ema(arr, n):
    k = 2/(n+1); out = list(arr)
    for i in range(1, len(out)):
        out[i] = arr[i]*k + out[i-1]*(1-k)
    return out

def _rsi(C, p=14):
    if len(C) < p+1: return [50.0]*len(C)
    out = [50.0]*len(C); g=l=0.0
    for i in range(1, p+1):
        d = C[i]-C[i-1]
        if d>0: g+=d
        else: l-=d
    ag,al = g/p, l/p
    out[p] = 100 if al==0 else 100-100/(1+ag/al)
    for i in range(p+1, len(C)):
        d = C[i]-C[i-1]
        ag = (ag*(p-1)+max(d,0))/p
        al = (al*(p-1)+max(-d,0))/p
        out[i] = 100 if al==0 else 100-100/(1+ag/al)
    return out

def _compute_ta(symbol):
    try:
        from ..services.market import get_history_cached
        df = get_history_cached(symbol, period="1y", interval="1d")
        if df is None or df.empty or len(df)<30: return None
        rows = [
            (float(c), float(h), float(l))
            for c, h, l in zip(df["Close"].tolist(), df["High"].tolist(), df["Low"].tolist())
            if _finite_number(c) is not None and _finite_number(h) is not None and _finite_number(l) is not None
        ]
        if len(rows) < 30:
            return None
        C = [r[0] for r in rows]; H = [r[1] for r in rows]; L = [r[2] for r in rows]
        n=len(C)-1
        rsi14=_rsi(C,14); ema90rsi=_ema(rsi14,90); ema20=_ema(C,20); ema50=_ema(C,50)
        rn=rsi14[n]; en=ema90rsi[n]; diff=rn-en
        tr=[H[i]-L[i] if i==0 else max(H[i]-L[i],abs(H[i]-C[i-1]),abs(L[i]-C[i-1])) for i in range(len(C))]
        atr=_ema(tr,14)[n]
        sl=C[n-19:n+1]; mean=sum(sl)/20; std=math.sqrt(sum((x-mean)**2 for x in sl)/20)
        bbu=mean+2*std; bbl=mean-2*std
        bbp=((C[n]-bbl)/(bbu-bbl)*100) if bbu!=bbl else 50
        sl20=(ema20[n]-ema20[max(0,n-5)])/5; sl50=(ema50[n]-ema50[max(0,n-10)])/10
        if ema20[n]>ema50[n] and sl20>0 and sl50>0: trend="UPTREND"
        elif ema20[n]<ema50[n] and sl20<0 and sl50<0: trend="DOWNTREND"
        elif abs(sl20)<0.05*C[n]/100: trend="SIDEWAYS"
        elif ema20[n]>ema50[n]: trend="MILD UP"
        else: trend="MILD DOWN"
        if diff>=20: mom="OVERBOUGHT"
        elif diff<=-20: mom="OVERSOLD"
        elif diff>=10: mom="ELEVATED"
        elif diff<=-10: mom="DEPRESSED"
        else: mom="NEUTRAL"
        # Same NaN-guard fix as the duplicated copies of this calc elsewhere
        # (routes_strategy.py/opportunity_scanner.py/trade_opportunity_scanner.py/maya_pages.py).
        rets30=[math.log(C[i]/C[i-1]) for i in range(n-29,n+1) if C[i-1]>0 and math.isfinite(C[i]) and C[i]>0]
        rets90=[math.log(C[i]/C[i-1]) for i in range(n-89,n+1) if C[i-1]>0 and math.isfinite(C[i]) and C[i]>0] if n>=90 else rets30
        rv30=math.sqrt(sum(x**2 for x in rets30)/len(rets30)*252)*100 if rets30 else 20
        rv90=math.sqrt(sum(x**2 for x in rets90)/len(rets90)*252)*100 if rets90 else rv30
        _ivr_raw = (rv30/rv90)*50 if rv90 else None
        ivr=min(100,max(0,round(_ivr_raw))) if _ivr_raw is not None and math.isfinite(_ivr_raw) else 40
        price = _last_finite(C, None)
        prev = _last_finite(C[:-1], price) if len(C) > 1 else price
        if price is None or prev is None:
            return None
        return {
            "price":round(price,2),"prev_close":round(prev,2),
            "chg_pct":round((C[n]-prev)/prev*100,2) if prev else 0,
            "rsi":round(rn,1),"ema90_rsi":round(en,1),"rsi_ema_diff":round(diff,1),
            "ema20":round(ema20[n],2),"ema50":round(ema50[n],2),
            "atr":round(atr,2),"bb_pct":round(bbp,1),"bb_upper":round(bbu,2),"bb_lower":round(bbl,2),
            "trend":trend,"momentum":mom,"iv_rank":ivr,"iv_est":round(rv30,1),
            "high52":round(max(H),2) if H else None,"low52":round(min(L),2) if L else None,
        }
    except Exception as e:
        print("[compute_ta]", e); return None

def _wall_label(score, oi_change, oi, doi_rank=0.0, sig=None):
    """Plain-English label for OI wall significance."""
    try:
        score = float(score or 0)
        oi_change = int(oi_change or 0)
        oi = int(oi or 0)
        doi_rank = float(doi_rank or 0)
    except Exception:
        score, oi_change, oi, doi_rank = 0.0, 0, 0, 0.0
    sig = sig or {}
    if sig.get("significant_removal"):
        return "Significant Wall Removal"
    if sig.get("significant_build") and score >= 78:
        return "Major Fresh Wall"
    if score >= 68:
        return "Major Existing Wall"
    if sig.get("significant_build") and score >= 55:
        return "Fresh Developing Wall"
    if score >= 45:
        return "Relevant Wall"
    if sig.get("significant_build") or (doi_rank >= 0.75 and oi_change > 0 and not sig):
        return "Fresh Build"
    return "Weak/Stale Wall"


def _wall_rows(rows, spot, gex_info=None, sigma_1d=None, oi_change_filter=None):
    """Return per-strike wall objects scored by current OI + significant OI change + distance + GEX."""
    spot = _finite_number(spot)
    if spot is None or spot <= 0:
        return [], 1.0
    sigma_1d = _finite_number(sigma_1d, None)
    gex_info = gex_info or {}
    gex_map = gex_info.get("gex_per_strike") or gex_info.get("gex_by_strike") or {}

    by_key = {}
    strikes = []
    for r in rows or []:
        try:
            typ = str(r.get("type") or "").lower()
            if typ not in ("put", "call"):
                continue
            strike = float(r.get("strike") or 0)
            oi = int(r.get("oi") or 0)
            if strike <= 0 or oi <= 0:
                continue
            key = (typ, strike)
            cur = by_key.setdefault(key, {"type": typ, "strike": strike, "oi": 0, "vol": 0, "oi_change": 0, "prev_oi": 0})
            cur["oi"] += oi
            cur["vol"] += int(r.get("vol") or r.get("volume") or 0)
            cur["oi_change"] += int(r.get("oi_change") or 0)
            cur["prev_oi"] += int(r.get("prev_oi") or max(0, oi - int(r.get("oi_change") or 0)))
            strikes.append(strike)
        except Exception:
            continue

    if len(set(strikes)) >= 2:
        ss = sorted(set(strikes))
        gaps = [ss[i+1] - ss[i] for i in range(len(ss)-1) if ss[i+1] > ss[i]]
        interval = sorted(gaps)[len(gaps)//2] if gaps else 1.0
    else:
        interval = 1.0

    vals = list(by_key.values())
    if not vals:
        return [], round(interval, 2)

    for v in vals:
        oi = int(v.get("oi") or 0)
        oi_change = int(v.get("oi_change") or 0)
        prev_oi = int(v.get("prev_oi") or max(0, oi - oi_change))
        v["_oi_sig"] = _oi_change_sig_flags(oi, prev_oi, oi_change, oi_change_filter)

    max_oi = max([v["oi"] for v in vals] or [1])
    max_pos_chg = max([max(0, v.get("oi_change", 0)) if (v.get("_oi_sig") or {}).get("significant_build") else 0 for v in vals] or [0])
    max_abs_gex = 0.0
    for v in vals:
        gx = _finite_number(gex_map.get(str(v["strike"])) if isinstance(gex_map, dict) else None, 0.0) or 0.0
        max_abs_gex = max(max_abs_gex, abs(gx))

    # A wall within roughly 2 daily sigmas gets more weight.  If IV/sigma is unavailable,
    # fall back to 2% of spot, which works acceptably for index/ETF walls.
    prox_span = max(float(sigma_1d or 0) * 2.0, spot * 0.02, float(interval or 1.0) * 2.0)
    out = []
    for v in vals:
        strike = float(v["strike"])
        oi = int(v["oi"] or 0)
        oi_change = int(v.get("oi_change") or 0)
        prev_oi = int(v.get("prev_oi") or max(0, oi - oi_change))
        sig = v.get("_oi_sig") or _oi_change_sig_flags(oi, prev_oi, oi_change, oi_change_filter)
        oi_rank = oi / max(1, max_oi)
        doi_rank = (max(0, oi_change) / max(1, max_pos_chg)) if max_pos_chg and sig.get("significant_build") else 0.0
        distance = abs(strike - spot)
        prox_rank = max(0.0, 1.0 - distance / max(0.01, prox_span))
        gex_val = _finite_number(gex_map.get(str(strike)) if isinstance(gex_map, dict) else None, 0.0) or 0.0
        gex_rank = abs(gex_val) / max(1.0, max_abs_gex) if max_abs_gex else 0.0
        # Current OI is always a wall base.  OI change contributes only when it
        # passes the liquidity, percent-change, absolute-change and strike-OI gates.
        score = 40.0 * oi_rank + 30.0 * doi_rank + 20.0 * prox_rank + 10.0 * gex_rank
        if sig.get("significant_removal"):
            score *= 0.72
        z = (strike - spot) / sigma_1d if sigma_1d and sigma_1d > 0 else None
        pct_from_spot = (strike - spot) / spot * 100.0 if spot else None
        oi_change_pct = sig.get("pct")
        item = {
            "type": v["type"],
            "side": "support" if v["type"] == "put" else "resistance",
            "strike": round(strike, 2),
            "oi": oi,
            "oi_change": oi_change,
            "oi_change_pct": oi_change_pct,
            "prev_oi": prev_oi,
            "vol": int(v.get("vol") or 0),
            "score": round(score, 1),
            "oi_rank": round(oi_rank, 3),
            "oi_change_rank": round(doi_rank, 3),
            "proximity_rank": round(prox_rank, 3),
            "gex_rank": round(gex_rank, 3),
            "gex": round(gex_val, 2),
            "zscore": round(z, 2) if z is not None else None,
            "pct_from_spot": round(pct_from_spot, 2) if pct_from_spot is not None else None,
            "fresh": bool(sig.get("significant_build")),
            "unwinding": bool(sig.get("significant_removal")),
            "oi_change_significant": bool(sig.get("significant")),
            "oi_change_filter_reason": sig.get("reason"),
        }
        item["label"] = _wall_label(score, oi_change, oi, doi_rank, sig=sig)
        item["note"] = (
            f"{item['label']}: OI {oi:,}, OI chg {oi_change:+,}" +
            (f" ({oi_change_pct:+.1f}%)" if oi_change_pct is not None else "") +
            f", {abs(item['pct_from_spot'] or 0):.2f}% from spot" +
            (f", z {item['zscore']:+.2f}" if item.get("zscore") is not None else "") +
            ("" if item.get("oi_change_significant") else f"; ΔOI ignored: {sig.get('reason')}")
        )
        out.append(item)
    return out, round(interval, 2)

def _walls(rows, spot, side=5, gex_info=None, sigma_1d=None, oi_change_filter=None):
    spot = _finite_number(spot)
    if spot is None or spot <= 0:
        return {"support": None, "resistance": None, "gamma_wall": None, "interval": 1.0, "top_put_walls": [], "top_call_walls": [], "significant_put_walls": [], "significant_call_walls": []}

    wall_items, interval = _wall_rows(rows, spot, gex_info=gex_info, sigma_1d=sigma_1d, oi_change_filter=oi_change_filter)
    if not wall_items:
        return {"support": round(spot*0.98,2), "resistance": round(spot*1.02,2), "gamma_wall": round(spot,2), "interval": 1.0, "top_put_walls": [], "top_call_walls": [], "significant_put_walls": [], "significant_call_walls": []}

    puts = [w for w in wall_items if w["type"] == "put"]
    calls = [w for w in wall_items if w["type"] == "call"]
    pb = [w for w in puts if w["strike"] <= spot]
    ca = [w for w in calls if w["strike"] >= spot]

    # Most significant tradable support/resistance: current OI + fresh change + proximity + GEX.
    #
    # _wall_rows()'s score is ADDITIVE (40*oi_rank + 30*doi_rank + 20*prox_rank
    # + 10*gex_rank), which means a strike far outside the proximity window
    # (prox_rank clamped to exactly 0) can still WIN purely on having the
    # single largest raw OI anywhere in the chain -- 40 points from OI alone
    # beats a nearby wall with more balanced but smaller numbers. That's
    # precisely how a stale, oversized, deep-OTM hedge position (e.g. a put
    # strike 30% below spot on a 0DTE chain, where real OI shouldn't
    # meaningfully exist that far out) can get picked as "the" wall over
    # multiple genuinely relevant strikes sitting right near spot.
    #
    # Fix: prefer strikes actually within the proximity window when picking
    # THE single support/resistance level; only fall back to something
    # farther out if nothing at all exists nearby (keeps the function from
    # ever returning None just because the near list happens to be empty).
    # significant_put_walls/significant_call_walls below are untouched --
    # those broader top-N lists (used elsewhere for bubble charts etc.)
    # still show the raw ranking, proximity-capped or not, so nothing far
    # from spot silently disappears from the app -- it just stops being
    # allowed to claim the single "the wall" line in Key Levels.
    pb_near = [w for w in pb if (w.get("proximity_rank") or 0) > 0]
    ca_near = [w for w in ca if (w.get("proximity_rank") or 0) > 0]
    support_item = (max(pb_near, key=lambda w: w["score"]) if pb_near else
                     (max(pb, key=lambda w: w["score"]) if pb else
                      (max(puts, key=lambda w: w["score"]) if puts else None)))
    resist_item = (max(ca_near, key=lambda w: w["score"]) if ca_near else
                    (max(ca, key=lambda w: w["score"]) if ca else
                     (max(calls, key=lambda w: w["score"]) if calls else None)))

    all_near = [w for w in wall_items if abs(w["strike"] - spot) / max(spot, 1) < 0.05]
    gwall_item = max(all_near or wall_items, key=lambda w: abs(w.get("gex") or 0) if w.get("gex") else w["oi"])

    sig_puts = sorted(pb or puts, key=lambda w: (-w["score"], abs(w["strike"] - spot)))[:side]
    sig_calls = sorted(ca or calls, key=lambda w: (-w["score"], abs(w["strike"] - spot)))[:side]

    # Backward-compatible list format; extra tuple values are ignored by older renderers.
    top_put_tuples = [(w["strike"], w["oi"], w["score"], w["oi_change"], w["label"]) for w in sig_puts]
    top_call_tuples = [(w["strike"], w["oi"], w["score"], w["oi_change"], w["label"]) for w in sig_calls]

    return {
        "support": round(support_item["strike"], 2) if support_item else round(spot*0.98,2),
        "resistance": round(resist_item["strike"], 2) if resist_item else round(spot*1.02,2),
        "gamma_wall": round(gwall_item["strike"], 2) if gwall_item else round(spot,2),
        "interval": round(interval, 2),
        "top_put_walls": top_put_tuples,
        "top_call_walls": top_call_tuples,
        "significant_put_walls": sig_puts,
        "significant_call_walls": sig_calls,
        "all_wall_scores": sorted(wall_items, key=lambda w: (-w["score"], abs(w["strike"]-spot)))[:24],
        "support_detail": support_item,
        "resistance_detail": resist_item,
        "method": (
            "40% current OI + 30% significant positive OI change + 20% proximity/Z + 10% GEX contribution. "
            + ((oi_change_filter or {}).get("method") or "")
        ).strip(),
        "oi_change_filter": oi_change_filter or {},
    }



def _clip(v, lo=0.0, hi=100.0):
    try:
        f = float(v)
    except Exception:
        return lo
    if not math.isfinite(f):
        return lo
    return max(lo, min(hi, f))


def _zscore_value(value, sample):
    vals = [_finite_number(x) for x in (sample or [])]
    vals = [x for x in vals if x is not None]
    v = _finite_number(value)
    if v is None or len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / len(vals)
    sd = math.sqrt(var)
    if sd <= 1e-9:
        return 0.0
    return round((v - mean) / sd, 2)


def _score_gex_walls(rows, spot, gex_info=None, side=5):
    """
    Score option walls using the blend discussed for GEX Plan:
      40% current OI rank + 30% positive OI change rank +
      20% proximity to spot + 10% GEX contribution.

    Z-scores are included for explanation, while VIX is intentionally left as a
    risk/reliability overlay instead of a wall selector.  The strongest walls are
    fresh, large, near spot, and have meaningful gamma contribution.
    """
    spot = _finite_number(spot)
    if spot is None or spot <= 0:
        return {"top_put_walls": [], "top_call_walls": [], "summary": "Spot unavailable", "score": 0}

    rows = [dict(r) for r in (rows or [])]
    gex_map = {}
    try:
        gex_map = {float(k): abs(float(v)) for k, v in (gex_info or {}).get("gex_per_strike", {}).items()}
    except Exception:
        gex_map = {}
    max_gex = max(gex_map.values()) if gex_map else 0.0

    def _side_rows(opt_type):
        out = []
        for r in rows:
            if str(r.get("type") or "").lower() != opt_type:
                continue
            strike = _finite_number(r.get("strike"))
            oi = int(_finite_number(r.get("oi"), 0) or 0)
            if strike is None or oi <= 0:
                continue
            # Put support below/at spot, call resistance above/at spot.
            if opt_type == "put" and strike > spot:
                continue
            if opt_type == "call" and strike < spot:
                continue
            out.append((strike, oi, int(_finite_number(r.get("oi_change"), 0) or 0), r))
        return out

    def _score_side(opt_type):
        data = _side_rows(opt_type)
        if not data:
            return []
        oi_vals = [x[1] for x in data]
        pos_delta_vals = [max(0, x[2]) for x in data]
        max_oi = max(oi_vals) if oi_vals else 1
        max_pos_delta = max(pos_delta_vals) if pos_delta_vals else 0
        out = []
        for strike, oi, delta, raw in data:
            oi_norm = _clip((oi / max(1, max_oi)) * 100.0)
            delta_norm = _clip((max(0, delta) / max(1, max_pos_delta)) * 100.0) if max_pos_delta > 0 else 0.0
            dist_pct = abs(strike - spot) / spot * 100.0
            # For near-expiry GEX walls, 0-3% from spot is most actionable.
            prox_norm = _clip(100.0 - (dist_pct / 3.0 * 100.0))
            gex_norm = _clip((gex_map.get(float(strike), 0.0) / max_gex) * 100.0) if max_gex > 0 else 0.0
            unwind_penalty = 18.0 if delta < 0 and abs(delta) >= max(100, oi * 0.08) else 0.0
            score = _clip(0.40 * oi_norm + 0.30 * delta_norm + 0.20 * prox_norm + 0.10 * gex_norm - unwind_penalty)
            z_oi = _zscore_value(oi, oi_vals)
            z_delta = _zscore_value(max(0, delta), pos_delta_vals) if max_pos_delta > 0 else 0.0
            wall_role = "Support" if opt_type == "put" else "Resistance"
            if delta < 0 and abs(delta) >= max(100, oi * 0.08):
                label = f"Unwinding {wall_role}"
                tier = "unwinding"
            elif score >= 75 and delta > 0:
                label = f"Major Fresh {wall_role}"
                tier = "major_fresh"
            elif score >= 65:
                label = f"Major Existing {wall_role}"
                tier = "major_existing"
            elif delta > 0 and z_delta >= 1.0:
                label = f"Fresh Developing {wall_role}"
                tier = "fresh_developing"
            elif score >= 45:
                label = f"Moderate {wall_role}"
                tier = "moderate"
            else:
                label = f"Weak/Stale {wall_role}"
                tier = "weak_stale"
            out.append({
                "type": opt_type,
                "role": wall_role.lower(),
                "strike": round(strike, 2),
                "oi": oi,
                "oi_change": delta,
                "oi_change_pct": raw.get("oi_change_pct"),
                "prev_oi": raw.get("prev_oi"),
                "snapshot_date": raw.get("date") or raw.get("snapshot_date"),
                "prev_date": raw.get("prev_date"),
                "distance_pct": round(dist_pct, 2),
                "gex_abs": round(gex_map.get(float(strike), 0.0), 2),
                "score": round(score, 1),
                "z_oi": z_oi,
                "z_delta": z_delta,
                "tier": tier,
                "label": label,
                "components": {
                    "current_oi": round(oi_norm, 1),
                    "fresh_oi_change": round(delta_norm, 1),
                    "proximity": round(prox_norm, 1),
                    "gex": round(gex_norm, 1),
                    "unwind_penalty": round(unwind_penalty, 1),
                },
                "reason": (
                    f"OI z {z_oi:+.1f}, delta z {z_delta:+.1f}, "
                    f"{dist_pct:.1f}% from spot; {'fresh build' if delta > 0 else 'flat/unwinding'}"
                ),
            })
        # Same additive-score flaw the GEX Plan wall selector had (fixed
        # in _walls()): oi_norm alone (40% weight) can push a strike with
        # ZERO proximity contribution (prox_norm clamps to 0 past 3% from
        # spot, same cutoff this function already uses internally) above
        # every genuinely nearby wall, purely on having an oversized raw
        # OI count -- exactly how a stale, far-off position can end up
        # labeled "Major Existing Support" at the top of this list while
        # every wall actually near spot sits below it. Two-tier sort using
        # the SAME 3%-from-spot cutoff the score already computes:
        # everything within it ranks by score first, same as before;
        # nothing beyond it can outrank a nearby wall regardless of size,
        # but it's not dropped -- it just falls to the back of the list
        # rather than claiming the top spot.
        out.sort(key=lambda x: (x["distance_pct"] > 3.0, -x["score"], x["distance_pct"]))
        return out[:side]

    put_walls = _score_side("put")
    call_walls = _score_side("call")
    top_scores = [w.get("score", 0) for w in (put_walls[:1] + call_walls[:1])]
    wall_score = round(sum(top_scores) / len(top_scores), 1) if top_scores else 0
    if put_walls and call_walls:
        summary = f"Put {put_walls[0]['strike']} {put_walls[0]['label']} · Call {call_walls[0]['strike']} {call_walls[0]['label']}"
    elif put_walls:
        summary = f"Put {put_walls[0]['strike']} {put_walls[0]['label']}"
    elif call_walls:
        summary = f"Call {call_walls[0]['strike']} {call_walls[0]['label']}"
    else:
        summary = "No actionable walls"
    return {
        "top_put_walls": put_walls,
        "top_call_walls": call_walls,
        "score": wall_score,
        "summary": summary,
        "method": "40% current OI + 30% positive OI change + 20% proximity + 10% GEX; VIX is a reliability overlay",
    }

def _pop_credit(dist_pct): return min(90,max(50,round(65+dist_pct*3.4)))
def _pop_debit(width,debit): return round(max(40,min(80,(width-debit)/width*100))) if width>0 else 50

def _round_to_interval(price, interval):
    """Round price to nearest strike interval."""
    if interval <= 0: interval = 1
    return round(round(price / interval) * interval, 2)

def _build_strats(sym, expiry, ta, walls, dte_label):
    """
    Build strategies using ATR-based strikes close to spot.
    Use OI walls only as reference, not as strikes.
    Enforce min 0.7:1 R:R — collect at least 41% of width as credit.
    Credit approximated from IV and DTE (Black-Scholes proxy).
    """
    import math
    spot=ta["price"]; mom=ta["momentum"]; trend=ta["trend"]
    ivr=ta["iv_rank"]; diff=ta["rsi_ema_diff"]; atr=ta["atr"]
    interval=walls["interval"] if walls["interval"] > 0 else 1
    oi_support=walls["support"]; oi_resist=walls["resistance"]
    iv_est = max(0.15, (ta.get('iv_est') or ta.get('iv_rank', 25) or 25) / 100)

    # DTE from label
    try:
        from datetime import date, datetime
        dte = (datetime.strptime(expiry,"%Y-%m-%d").date() - date.today()).days
    except: dte = 14

    def option_price_approx(S, K, T_days, iv, is_call=True):
        """
        Option price estimate using Black-Scholes.
        Fallback uses a correct lognormal approximation (not the broken distance-based one).
        """
        T = max(T_days, 1) / 365
        sqT = math.sqrt(T)

        def _norm_cdf(x):
            """Abramowitz & Stegun approximation, accurate to 7.5e-8."""
            if x < 0: return 1 - _norm_cdf(-x)
            p = 0.2316419; b1,b2,b3,b4,b5 = 0.319381530,-0.356563782,1.781477937,-1.821255978,1.330274429
            t = 1/(1+p*x)
            poly = t*(b1+t*(b2+t*(b3+t*(b4+t*b5))))
            return 1 - (1/math.sqrt(2*math.pi))*math.exp(-0.5*x*x)*poly

        if _HAS_SCIPY:
            _cdf = _norm.cdf
        else:
            _cdf = _norm_cdf

        try:
            if iv <= 0 or S <= 0 or K <= 0: raise ValueError
            d1 = (math.log(S/K) + 0.5*iv*iv*T) / (iv*sqT)
            d2 = d1 - iv*sqT
            if is_call: return max(0, round(S*_cdf(d1) - K*_cdf(d2), 2))
            else:       return max(0, round(K*_cdf(-d2) - S*_cdf(-d1), 2))
        except:
            # Last resort: intrinsic + time value
            intrinsic = max(0, S-K if is_call else K-S)
            tv = iv * sqT * S * 0.3984  # ≈ vega at ATM
            moneyness = abs(S-K)/(S*iv*sqT) if S*iv*sqT > 0 else 0
            otm_factor = math.exp(-0.5*moneyness*moneyness)
            return round(max(0.01, intrinsic + tv*otm_factor), 2)

    def credit_for_spread(sell_s, buy_s, side):
        """Estimate net credit for a vertical spread."""
        is_call = (side == "call")
        sell_px = option_price_approx(spot, sell_s, dte, iv_est, is_call)
        buy_px  = option_price_approx(spot, buy_s, dte, iv_est, is_call)
        return round(sell_px - buy_px, 2)

    # Width: ATR-based, prefer $5 for SPY-type liquid names, $10 for high-IV stocks
    # SPY interval=1, so interval*5=5 (standard $5 wide), high-price stocks use wider
    raw_width = max(atr * 0.6, interval * 5)  # at least 5 intervals wide
    width = _round_to_interval(raw_width, interval)
    # Clamp: min 5 intervals, max 10 intervals (no $1-$2 wide on SPY)
    width = max(interval*5, min(interval*10, width))

    MIN_RR = 0.5  # min 0.5:1 — collect 33% of width (realistic for liquid ETFs/stocks)

    strats = []

    # ── Bull Put Spread ─────────────────────────────────────────────────────
    if mom != "OVERBOUGHT" and trend != "DOWNTREND":
        # Sell put 1-3% below spot, near OI support if available
        for pct_otm in [0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]:
            raw_sell = spot * (1 - pct_otm)
            sp = _round_to_interval(raw_sell, interval)
            bp = round(sp - width, 2)
            if sp >= spot: continue
            cr = credit_for_spread(sp, bp, "put")
            ml = round(width - cr, 2)
            if ml <= 0: continue
            rr = round(cr / ml, 2)
            if rr < MIN_RR: continue
            dp = round(abs(spot-sp)/spot*100, 1)
            cr_d = round(cr*100, 0); ml_d = round(ml*100, 0)
            strats.append({
                "name":"Bull Put Spread","type":"credit","bias":"Bullish",
                "legs":f"Sell {sp}P / Buy {bp}P","expiry":expiry,"dte_label":dte_label,
                "est_credit":f"~${cr}","max_gain":f"~${cr_d}/contract","max_loss":f"~${ml_d}/contract",
                "rr":f"{rr:.2f}:1","pop":_pop_credit(dp),
                "rationale":f"Sell ${sp}P ({dp}% OTM). OI support at {oi_support}. Trend: {trend}. Mom: {mom} (RSI-EMA diff {diff}). IV rank {ivr}/100.",
                "manage":f"Close at 50% credit (~${round(cr*0.5,2)}). Stop if spot closes below ${bp}.",
            }); break

    # ── Bear Call Spread ─────────────────────────────────────────────────────
    if mom != "OVERSOLD" and trend != "UPTREND":
        for pct_otm in [0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]:
            raw_sell = spot * (1 + pct_otm)
            sc = _round_to_interval(raw_sell, interval)
            bc = round(sc + width, 2)
            if sc <= spot: continue
            cr = credit_for_spread(sc, bc, "call")
            ml = round(width - cr, 2)
            if ml <= 0: continue
            rr = round(cr / ml, 2)
            if rr < MIN_RR: continue
            dc = round(abs(sc-spot)/spot*100, 1)
            cr_d = round(cr*100, 0); ml_d = round(ml*100, 0)
            strats.append({
                "name":"Bear Call Spread","type":"credit","bias":"Bearish",
                "legs":f"Sell {sc}C / Buy {bc}C","expiry":expiry,"dte_label":dte_label,
                "est_credit":f"~${cr}","max_gain":f"~${cr_d}/contract","max_loss":f"~${ml_d}/contract",
                "rr":f"{rr:.2f}:1","pop":_pop_credit(dc),
                "rationale":f"Sell ${sc}C ({dc}% OTM). OI resistance at {oi_resist}. Trend: {trend}. Mom: {mom} (RSI-EMA diff {diff}). IV rank {ivr}/100.",
                "manage":f"Close at 50% credit (~${round(cr*0.5,2)}). Stop if spot closes above ${bc}.",
            }); break

    # ── Iron Condor ──────────────────────────────────────────────────────────
    if mom in ("NEUTRAL","ELEVATED","DEPRESSED") or ivr >= 50:
        # Build best put + call legs independently
        best_put = best_call = None
        for pct in [0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]:
            sp2 = _round_to_interval(spot*(1-pct), interval)
            bp2 = round(sp2-width, 2)
            if sp2 >= spot: continue
            cr_p = credit_for_spread(sp2, bp2, "put")
            ml_p = width - cr_p
            if ml_p > 0 and cr_p/ml_p >= MIN_RR:
                best_put = (sp2, bp2, cr_p); break
        for pct in [0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]:
            sc2 = _round_to_interval(spot*(1+pct), interval)
            bc2 = round(sc2+width, 2)
            if sc2 <= spot: continue
            cr_c = credit_for_spread(sc2, bc2, "call")
            ml_c = width - cr_c
            if ml_c > 0 and cr_c/ml_c >= MIN_RR:
                best_call = (sc2, bc2, cr_c); break
        if best_put and best_call:
            sp2,bp2,cr_p = best_put; sc2,bc2,cr_c = best_call
            total_cr = round(cr_p+cr_c, 2); ml_ic = round(width-total_cr, 2)
            rr_ic = round(total_cr/ml_ic, 2) if ml_ic>0 else 0
            if rr_ic >= MIN_RR:
                dp2=round(abs(spot-sp2)/spot*100,1); dc2=round(abs(sc2-spot)/spot*100,1)
                pop_ic=round((_pop_credit(dp2)+_pop_credit(dc2))/2)
                strats.append({
                    "name":"Iron Condor","type":"credit","bias":"Neutral",
                    "legs":f"Sell {sp2}P/Buy {bp2}P  ·  Sell {sc2}C/Buy {bc2}C",
                    "put_sell":sp2,"put_buy":bp2,"call_sell":sc2,"call_buy":bc2,
                    "put_credit":cr_p,"call_credit":cr_c,
                    "expiry":expiry,"dte_label":dte_label,
                    "est_credit":f"~${total_cr}","max_gain":f"~${round(total_cr*100,0)}/contract",
                    "max_loss":f"~${round(ml_ic*100,0)}/contract",
                    "rr":f"{rr_ic:.2f}:1","pop":pop_ic,
                    "rationale":f"OI walls {oi_support}/{oi_resist}. IC: ${sp2}P/${sc2}C. Mom: {mom}, IV rank: {ivr}/100. RSI-EMA diff: {diff}.",
                    "manage":f"Close at 50% max profit (~${round(total_cr*0.5,2)}). Adjust if either short strike breached.",
                })

    return strats


def _weekly_liquidity_profile(sym, ta):
    """Return a compact liquidity profile for weekly planning."""
    ta = ta or {}
    price = float(ta.get("price") or 0.0)
    atr = float(ta.get("atr") or ta.get("sigma_wk") or 0.0)
    volume = float(ta.get("volume") or 0.0)
    avg_dollar_volume = float(ta.get("avg_dollar_volume") or 0.0)

    if avg_dollar_volume <= 0 and price > 0 and volume > 0:
        avg_dollar_volume = price * volume

    score = 0.0
    if avg_dollar_volume >= 1_000_000_000:
        score = 95.0
    elif avg_dollar_volume >= 500_000_000:
        score = 85.0
    elif avg_dollar_volume >= 200_000_000:
        score = 75.0
    elif avg_dollar_volume >= 100_000_000:
        score = 65.0
    elif avg_dollar_volume >= 50_000_000:
        score = 55.0
    else:
        score = 40.0

    spread_proxy = None
    if price > 0 and atr > 0:
        spread_proxy = round(min(100.0, max(0.5, (atr / price) * 100.0)), 2)
    grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"
    return {
        "symbol": sym,
        "avg_dollar_volume": round(avg_dollar_volume, 2),
        "liquidity_score": round(score, 1),
        "grade": grade,
        "spread_proxy_pct": spread_proxy,
        "is_core_etf": sym in {"SPY", "QQQ", "IWM"},
    }


def _weekly_relative_strength_profile(sym, benchmark="SPY", lookback=126):
    """Return a simple benchmark-relative weekly strength profile."""
    try:
        from ..services.market import get_history_cached
        s = get_history_cached(sym, period="1y", interval="1d")
        b = get_history_cached(benchmark, period="1y", interval="1d")
        if s is None or b is None or s.empty or b.empty:
            return {"symbol": sym, "benchmark": benchmark, "rs_score": 50.0, "trend": "UNKNOWN"}
        s_close = s["Close"].dropna()
        b_close = b["Close"].dropna()
        n = min(len(s_close), len(b_close), max(lookback, 20))
        if n < 20:
            return {"symbol": sym, "benchmark": benchmark, "rs_score": 50.0, "trend": "UNKNOWN"}
        s_ret = (s_close.iloc[-1] / s_close.iloc[-n] - 1.0) * 100.0
        b_ret = (b_close.iloc[-1] / b_close.iloc[-n] - 1.0) * 100.0
        diff = s_ret - b_ret
        score = max(0.0, min(100.0, 50.0 + diff * 2.0))
        trend = "STRONG" if diff >= 5 else "MODERATE" if diff >= 0 else "WEAK"
        return {
            "symbol": sym,
            "benchmark": benchmark,
            "lookback": n,
            "symbol_return_pct": round(float(s_ret), 2),
            "benchmark_return_pct": round(float(b_ret), 2),
            "relative_strength_diff_pct": round(float(diff), 2),
            "rs_score": round(score, 1),
            "trend": trend,
        }
    except Exception:
        return {"symbol": sym, "benchmark": benchmark, "rs_score": 50.0, "trend": "UNKNOWN"}


def _weekly_earnings_profile(sym):
    """Return earnings proximity / risk for weekly planning."""
    try:
        info = get_earnings_info(sym) or {}
        days = info.get("earn_days")
        score = info.get("earn_score")
        risk = "LOW"
        if days is None:
            risk = "UNKNOWN"
        else:
            try:
                d = int(days)
                if d <= 7:
                    risk = "HIGH"
                elif d <= 14:
                    risk = "MEDIUM"
            except Exception:
                risk = "UNKNOWN"
        return {
            "symbol": sym,
            "earn_days": days,
            "earn_score": score,
            "risk": risk,
        }
    except Exception:
        return {"symbol": sym, "earn_days": None, "earn_score": None, "risk": "UNKNOWN"}


def _series_ema(values, n):
    vals = [_finite_number(v, 0.0) or 0.0 for v in (values or [])]
    if not vals:
        return []
    n = max(1, int(n or 1))
    k = 2.0 / (n + 1.0)
    out = [float(vals[0])]
    for v in vals[1:]:
        out.append(float(v) * k + out[-1] * (1.0 - k))
    return out


def _safe_last(seq, default=None):
    try:
        vals = list(seq)
    except Exception:
        return default
    for v in reversed(vals):
        f = _finite_number(v)
        if f is not None:
            return f
    return default


def _macd_series(closes):
    closes = [_finite_number(x, 0.0) or 0.0 for x in (closes or [])]
    if not closes:
        return [], [], []
    ema12 = _series_ema(closes, 12)
    ema26 = _series_ema(closes, 26)
    macd = [a - b for a, b in zip(ema12, ema26)]
    signal = _series_ema(macd, 9)
    hist = [a - b for a, b in zip(macd, signal)]
    return macd, signal, hist


def _adx_last(highs, lows, closes, period=14):
    try:
        H = [float(x) for x in highs]
        L = [float(x) for x in lows]
        C = [float(x) for x in closes]
        if len(C) < period + 3:
            return 0.0, False
        tr = []
        pdm = []
        ndm = []
        for i in range(1, len(C)):
            up = H[i] - H[i-1]
            dn = L[i-1] - L[i]
            pdm.append(up if up > dn and up > 0 else 0.0)
            ndm.append(dn if dn > up and dn > 0 else 0.0)
            tr.append(max(H[i] - L[i], abs(H[i] - C[i-1]), abs(L[i] - C[i-1])))
        atr = _series_ema(tr, period)
        ep = _series_ema(pdm, period)
        en = _series_ema(ndm, period)
        dx = []
        for a, p, n in zip(atr, ep, en):
            if a <= 0:
                dx.append(0.0); continue
            dip = 100.0 * p / a
            din = 100.0 * n / a
            dx.append(100.0 * abs(dip - din) / max(1e-9, dip + din))
        adx = _series_ema(dx, period)
        if not adx:
            return 0.0, False
        return round(adx[-1], 1), bool(len(adx) > 2 and adx[-1] > adx[-2])
    except Exception:
        return 0.0, False


def _resample_history(df, rule):
    try:
        if df is None or df.empty:
            return df
        d = df.copy()
        # yfinance can return capitalized OHLCV columns; preserve only needed ones.
        agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        out = d.resample(rule).agg(agg).dropna(subset=["Close"])
        return out
    except Exception:
        return df


def _fetch_tf_history(symbol, timeframe="1d"):
    """Fetch OHLCV for a weekly-plan timeframe. 2h/4h are built from 1h bars."""
    sym = (symbol or "SPY").upper()
    tf = str(timeframe or "1d").lower()
    from ..services.market import get_history_cached
    try:
        if tf in ("2h", "4h"):
            df = get_history_cached(sym, period="90d", interval="1h")
            return _resample_history(df, tf)
        if tf == "1h":
            return get_history_cached(sym, period="60d", interval="1h")
        if tf in ("1w", "weekly"):
            return get_history_cached(sym, period="2y", interval="1wk")
        return get_history_cached(sym, period="1y", interval="1d")
    except Exception:
        return None


def _tf_profile(symbol, timeframe="1d", change_mult=2.0, vol_mult=1.2):
    """Indicator block for Weekly Plan: BB stretch/squeeze, RSI, MACD, EMAs, ADX, strong candles."""
    df = _fetch_tf_history(symbol, timeframe)
    if df is None or getattr(df, "empty", True) or len(df) < 35:
        return {"timeframe": timeframe, "error": "not enough data", "bias_score": 0, "score": 50}
    try:
        O = [float(x) for x in df["Open"].tolist()]
        H = [float(x) for x in df["High"].tolist()]
        L = [float(x) for x in df["Low"].tolist()]
        C = [float(x) for x in df["Close"].tolist()]
        V = [float(x or 0) for x in df["Volume"].tolist()]
        idx = list(df.index)
        n = len(C) - 1
        ema20s = _series_ema(C, 20); ema50s = _series_ema(C, 50); ema200s = _series_ema(C, min(200, max(20, len(C)//2)))
        rsis = _rsi(C, 14); rsi_ema = _series_ema(rsis, min(90, max(20, len(rsis)//2)))
        macd, sig, hist = _macd_series(C)
        adx, adx_rising = _adx_last(H, L, C, 14)
        chg = [0.0] + [((C[i] - C[i-1]) / C[i-1] * 100.0 if C[i-1] else 0.0) for i in range(1, len(C))]
        abs_chg_ema = _series_ema([abs(x) for x in chg], 60)
        vol_ema20 = _series_ema(V, 20)
        # Bollinger metrics.
        bb_pct = 50.0; bb_width_pct = 0.0; bb_width_rank = None; bb_upper = bb_lower = None
        if len(C) >= 20:
            widths = []
            for i in range(19, len(C)):
                sl = C[i-19:i+1]
                m = sum(sl) / 20.0
                sd = math.sqrt(sum((x - m) ** 2 for x in sl) / 20.0)
                up = m + 2.0 * sd; lo = m - 2.0 * sd
                w = (up - lo) / max(0.01, m) * 100.0
                widths.append(w)
            sl = C[-20:]
            m = sum(sl) / 20.0
            sd = math.sqrt(sum((x - m) ** 2 for x in sl) / 20.0)
            bb_upper = m + 2.0 * sd; bb_lower = m - 2.0 * sd
            bb_width_pct = (bb_upper - bb_lower) / max(0.01, m) * 100.0
            bb_pct = (C[-1] - bb_lower) / max(1e-9, bb_upper - bb_lower) * 100.0 if bb_upper != bb_lower else 50.0
            if widths:
                bb_width_rank = round(sum(1 for w in widths if w <= bb_width_pct) / len(widths) * 100.0, 1)
        # Keltner channel: EMA20 +/- 1.5 * ATR20.
        kc_mid = kc_upper = kc_lower = atr20 = None
        bb_inside_kc = kc_inside_bb = False
        if len(C) >= 20:
            tr = []
            for i in range(len(C)):
                if i == 0:
                    tr.append(max(0.0, H[i] - L[i]))
                else:
                    tr.append(max(H[i] - L[i], abs(H[i] - C[i-1]), abs(L[i] - C[i-1])))
            atrs = _series_ema(tr, 20)
            atr20 = atrs[-1] if atrs else None
            kc_mid = ema20s[-1] if ema20s else None
            if atr20 is not None and kc_mid is not None:
                kc_upper = kc_mid + 1.5 * atr20
                kc_lower = kc_mid - 1.5 * atr20
                if bb_upper is not None and bb_lower is not None:
                    bb_inside_kc = bool(bb_upper <= kc_upper and bb_lower >= kc_lower)
                    kc_inside_bb = bool(kc_upper <= bb_upper and kc_lower >= bb_lower)

        # Recent strong candle using user definition: one-bar % move vs EMA(abs(ChangePct),60), volume vs EMA(volume,20).
        strong = None
        lookback = min(12, len(C)-1)
        for j in range(n, max(0, n - lookback), -1):
            rng = max(0.01, H[j] - L[j])
            body_pct = abs(C[j] - O[j]) / rng * 100.0
            base = abs_chg_ema[j] if j < len(abs_chg_ema) and abs_chg_ema[j] else 0.0
            vm = V[j] / max(1.0, vol_ema20[j] if j < len(vol_ema20) else 1.0)
            if base <= 0:
                continue
            is_bull = C[j] > O[j] and chg[j] >= base * change_mult and body_pct >= 50 and C[j] >= L[j] + rng * 0.60 and vm >= vol_mult
            is_bear = C[j] < O[j] and chg[j] <= -base * change_mult and body_pct >= 50 and C[j] <= H[j] - rng * 0.60 and vm >= vol_mult
            if is_bull or is_bear:
                strong = {
                    "side": "bull" if is_bull else "bear",
                    "bars_ago": n - j,
                    "date": str(idx[j])[:19],
                    "open": round(O[j], 2), "high": round(H[j], 2), "low": round(L[j], 2), "close": round(C[j], 2),
                    "change_pct": round(chg[j], 2), "baseline_pct": round(base, 2),
                    "change_mult": round(abs(chg[j]) / max(0.01, base), 2),
                    "volume_mult": round(vm, 2),
                    "mid": round((H[j] + L[j]) / 2.0, 2),
                }
                break
        e20 = ema20s[-1]; e50 = ema50s[-1]; e200 = ema200s[-1]
        rsi = rsis[-1]; rd = rsi - rsi_ema[-1]
        mh = hist[-1] if hist else 0.0
        mh_prev = hist[-2] if len(hist) > 1 else mh
        slope20 = e20 - (ema20s[-4] if len(ema20s) >= 4 else e20)
        bull_bits = 0; bear_bits = 0
        if C[-1] > e20: bull_bits += 1
        else: bear_bits += 1
        if e20 > e50: bull_bits += 1
        else: bear_bits += 1
        if slope20 > 0: bull_bits += 1
        else: bear_bits += 1
        if mh > 0: bull_bits += 1
        else: bear_bits += 1
        if rd > 5: bull_bits += 1
        elif rd < -5: bear_bits += 1
        if adx >= 20 and adx_rising:
            if bull_bits > bear_bits: bull_bits += 1
            elif bear_bits > bull_bits: bear_bits += 1
        bias_score = round((bull_bits - bear_bits) / 6.0 * 100.0, 1)
        if adx < 16 and abs(bias_score) < 55:
            regime = "SIDEWAYS"
        elif bias_score >= 55:
            regime = "BULL"
        elif bias_score >= 20:
            regime = "WEAK_BULL"
        elif bias_score <= -55:
            regime = "BEAR"
        elif bias_score <= -20:
            regime = "WEAK_BEAR"
        else:
            regime = "SIDEWAYS"
        stretch = "neutral"
        if bb_pct >= 90 or rsi >= 70 or rd >= 20:
            stretch = "stretched_up"
        elif bb_pct <= 10 or rsi <= 30 or rd <= -20:
            stretch = "stretched_down"
        squeeze = bool(bb_width_rank is not None and bb_width_rank <= 20)
        return {
            "timeframe": timeframe,
            "bars": len(C),
            "last_time": str(idx[-1])[:19] if idx else None,
            "close": round(C[-1], 2),
            "change_pct": round(chg[-1], 2),
            "ema20": round(e20, 2), "ema50": round(e50, 2), "ema200": round(e200, 2),
            "rsi": round(rsi, 1), "rsi_diff90": round(rd, 1),
            "macd": round(macd[-1] if macd else 0.0, 4),
            "macd_signal": round(sig[-1] if sig else 0.0, 4),
            "macd_hist": round(mh, 4),
            "macd_hist_change": round(mh - mh_prev, 4),
            "hist_growing": bool(abs(mh) > abs(mh_prev)),
            "adx": adx, "adx_rising": adx_rising,
            "bb_pct": round(bb_pct, 1), "bb_width_pct": round(bb_width_pct, 2), "bb_width_rank": bb_width_rank,
            "bb_upper": round(bb_upper, 2) if bb_upper else None, "bb_lower": round(bb_lower, 2) if bb_lower else None,
            "kc_upper": round(kc_upper, 2) if kc_upper else None, "kc_lower": round(kc_lower, 2) if kc_lower else None,
            "kc_mid": round(kc_mid, 2) if kc_mid else None, "atr20": round(atr20, 2) if atr20 else None,
            "bb_inside_kc": bb_inside_kc, "kc_inside_bb": kc_inside_bb,
            "squeeze": squeeze or bb_inside_kc, "stretch": stretch,
            "volume": int(V[-1] or 0), "volume_mult": round(V[-1] / max(1.0, vol_ema20[-1] if vol_ema20 else 1.0), 2),
            "strong_candle": strong,
            "regime": regime, "bias_score": bias_score, "score": round(50 + bias_score / 2.0, 1),
        }
    except Exception as e:
        return {"timeframe": timeframe, "error": str(e), "bias_score": 0, "score": 50}


def _friday_expiry(exps, selected=None):
    today = date.today()
    if selected:
        try:
            d = date.fromisoformat(selected)
            return selected, max(0, (d - today).days)
        except Exception:
            pass
    # Prefer this week's Friday.  On Monday at 10am this is the intended weekly plan expiry.
    fri = today + timedelta((4 - today.weekday()) % 7)
    candidates = []
    for e in exps or []:
        try:
            d = date.fromisoformat(e)
        except Exception:
            continue
        candidates.append((e, d, max(0, (d - today).days)))
    for e, d, dte in candidates:
        if d == fri:
            return e, dte
    # fallback: nearest Friday within 0-7 DTE, then nearest expiry.
    fridays = [(e, d, dte) for e, d, dte in candidates if d.weekday() == 4 and 0 <= dte <= 7]
    if fridays:
        e, d, dte = sorted(fridays, key=lambda x: x[2])[0]
        return e, dte
    if candidates:
        e, d, dte = sorted(candidates, key=lambda x: abs(x[2] - 4))[0]
        return e, dte
    return None, 0


def _option_chain_weekly_context(sym, expiry, spot, fallback_iv=16.0):
    """ATM IV and straddle expected move for the target Friday expiry.

    Prefer the local option snapshot first so Weekly Plan and AI Hub use the
    same DB OI/pricing view.  yfinance remains a fallback only when the DB lacks
    target-expiry prices.
    """
    out = {"source": "fallback", "atm_strike": None, "atm_iv": fallback_iv, "straddle_move": None, "straddle_move_pct": None, "call_mid": None, "put_mid": None}
    if not expiry or not spot:
        return out
    if _shared_latest_option_rows is not None:
        try:
            rows, latest = _shared_latest_option_rows(sym, expiry)
            strikes = sorted({float(r.get("strike")) for r in rows if r.get("strike") is not None})
            if rows and strikes:
                atm = min(strikes, key=lambda k: abs(k - spot))
                def _near(opt_type):
                    best = None; best_dist = 10**9
                    for rr in rows:
                        typ = str(rr.get("type") or "").lower()
                        if typ != opt_type:
                            continue
                        k = _finite_number(rr.get("strike"), None)
                        if k is None:
                            continue
                        dist = abs(k - atm)
                        if dist < best_dist:
                            best = rr; best_dist = dist
                    if not best:
                        return None, None, 0
                    mid = _shared_option_mid(best) if _shared_option_mid is not None else None
                    iv = _finite_number(best.get("iv"), None)
                    if iv is not None and 0 < iv < 5:
                        iv *= 100.0
                    return mid, iv, int(best.get("oi") or 0)
                cm, civ, coi = _near("call")
                pm, piv, poi = _near("put")
                if (cm is not None and cm > 0) or (pm is not None and pm > 0):
                    ivs = [x for x in (civ, piv) if x is not None and x > 0]
                    move = (cm or 0.0) + (pm or 0.0)
                    out.update({
                        "source": f"local_db:{latest}",
                        "atm_strike": round(atm, 2),
                        "atm_iv": round(sum(ivs) / len(ivs), 2) if ivs else fallback_iv,
                        "call_iv": round(civ, 2) if civ else None,
                        "put_iv": round(piv, 2) if piv else None,
                        "call_mid": round(cm, 2) if cm is not None else None,
                        "put_mid": round(pm, 2) if pm is not None else None,
                        "straddle_move": round(move, 2) if move > 0 else None,
                        "straddle_move_pct": round(move / spot * 100.0, 2) if move > 0 and spot else None,
                        "atm_call_oi": coi,
                        "atm_put_oi": poi,
                    })
                    return out
        except Exception:
            pass
    try:
        ch = yf.Ticker(sym).option_chain(expiry)
        calls = ch.calls
        puts = ch.puts
        if calls is None or puts is None or calls.empty or puts.empty:
            return out
        strikes = sorted(set([float(x) for x in calls["strike"].tolist()] + [float(x) for x in puts["strike"].tolist()]))
        if not strikes:
            return out
        atm = min(strikes, key=lambda k: abs(k - spot))
        def _row_mid(df, strike):
            r = df.iloc[(df["strike"] - strike).abs().argsort()[:1]]
            if r.empty:
                return None, None, None
            rr = r.iloc[0]
            bid = _finite_number(rr.get("bid"), 0.0) or 0.0
            ask = _finite_number(rr.get("ask"), 0.0) or 0.0
            last = _finite_number(rr.get("lastPrice"), 0.0) or 0.0
            iv = _finite_number(rr.get("impliedVolatility"), None)
            mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
            return mid, (iv * 100.0 if iv is not None and iv < 5 else iv), int(_finite_number(rr.get("openInterest"), 0) or 0)
        cm, civ, coi = _row_mid(calls, atm)
        pm, piv, poi = _row_mid(puts, atm)
        ivs = [x for x in [civ, piv] if x is not None and x > 0]
        move = (cm or 0.0) + (pm or 0.0) if cm is not None or pm is not None else None
        out.update({
            "source": "option_chain",
            "atm_strike": round(atm, 2),
            "atm_iv": round(sum(ivs) / len(ivs), 2) if ivs else fallback_iv,
            "call_iv": round(civ, 2) if civ else None,
            "put_iv": round(piv, 2) if piv else None,
            "call_mid": round(cm, 2) if cm is not None else None,
            "put_mid": round(pm, 2) if pm is not None else None,
            "straddle_move": round(move, 2) if move is not None else None,
            "straddle_move_pct": round(move / spot * 100.0, 2) if move is not None and spot else None,
            "atm_call_oi": coi,
            "atm_put_oi": poi,
        })
    except Exception:
        pass
    return out


def _vix_context():
    try:
        from ..services.market import get_history_cached
        h = get_history_cached("^VIX", period="10d", interval="1d")
        if h is None or h.empty:
            return {"available": False}
        closes = [float(x) for x in h["Close"].dropna().tolist()]
        if not closes:
            return {"available": False}
        last = closes[-1]
        prev = closes[-2] if len(closes) > 1 else last
        chg = last - prev
        if last >= 25:
            regime = "HIGH_VOL"
            note = "High VIX: walls and ICs are less reliable; expect larger Friday range."
        elif last >= 18:
            regime = "ELEVATED"
            note = "Elevated VIX: use wider strikes and respect expected move."
        else:
            regime = "CALM"
            note = "Calm VIX: OI walls/pin behavior is more reliable if GEX is positive."
        return {"available": True, "level": round(last, 2), "change": round(chg, 2), "regime": regime, "note": note}
    except Exception:
        return {"available": False}


def _align_walls_with_strong_candles(walls, profiles, spot):
    out = []
    if not walls or not profiles or not spot:
        return {"score": 0, "notes": []}
    put = walls.get("support_detail") or {}
    call = walls.get("resistance_detail") or {}
    for tf in ["4h", "2h", "1d"]:
        p = profiles.get(tf) or {}
        sc = p.get("strong_candle") or {}
        if not sc:
            continue
        levels = [sc.get("low"), sc.get("mid"), sc.get("high")]
        if sc.get("side") == "bull" and put.get("strike"):
            if any(abs(float(put["strike"]) - float(x)) / max(1.0, spot) <= 0.012 for x in levels if x):
                out.append(f"{tf} strong bull candle with {sc.get('volume_mult')}x volume aligns with put/support wall ${put.get('strike')}")
        if sc.get("side") == "bear" and call.get("strike"):
            if any(abs(float(call["strike"]) - float(x)) / max(1.0, spot) <= 0.012 for x in levels if x):
                out.append(f"{tf} strong bear candle with {sc.get('volume_mult')}x volume aligns with call/resistance wall ${call.get('strike')}")
    return {"score": min(100, len(out) * 35), "notes": out}


def _weekly_plan_composite(sym, spot, expiry, dte, profiles, walls, iv_rank, expected_move, pcr, vix=None, bias_override=None):
    d = profiles.get("1d") or {}
    w = profiles.get("1w") or {}
    h4 = profiles.get("4h") or {}
    h2 = profiles.get("2h") or {}

    directional = round(
        (w.get("bias_score", 0) * 0.25) +
        (d.get("bias_score", 0) * 0.35) +
        (h4.get("bias_score", 0) * 0.25) +
        (h2.get("bias_score", 0) * 0.15), 1
    )
    # User-provided view, when given, REPLACES the auto-computed technical
    # directional score outright -- not blended with it. Blending a stated
    # view with the technical read would produce a number that matches
    # neither: not a clean expression of "I think this is bullish" (which
    # is what was actually asked for), and not the technical model's own
    # independent read either. Every downstream strategy score (ps/cs/ic/
    # cb/pb below) already keys off `directional`, so overriding it here
    # is the single point of leverage that correctly reshapes the whole
    # plan around the stated view.
    if bias_override is not None:
        directional = {"bullish": 65.0, "bearish": -65.0, "sideways": 0.0}.get(str(bias_override).lower(), directional)
    put_wall = walls.get("support_detail") or {}
    call_wall = walls.get("resistance_detail") or {}
    put_score = float(put_wall.get("score") or 0)
    call_score = float(call_wall.get("score") or 0)
    both_walls = min(put_score, call_score) if put_score and call_score else 0
    stretch_d = d.get("stretch") or "neutral"
    stretch_w = w.get("stretch") or "neutral"
    stretched_up = stretch_d == "stretched_up" or stretch_w == "stretched_up" or float(d.get("bb_pct") or 50) >= 82 or float(w.get("bb_pct") or 50) >= 82
    stretched_down = stretch_d == "stretched_down" or stretch_w == "stretched_down" or float(d.get("bb_pct") or 50) <= 18 or float(w.get("bb_pct") or 50) <= 18
    squeeze = bool(d.get("squeeze") or h4.get("squeeze") or w.get("squeeze"))
    kc_inside_bb = bool(d.get("kc_inside_bb") or w.get("kc_inside_bb"))
    bb_inside_kc = bool(d.get("bb_inside_kc") or w.get("bb_inside_kc"))
    range_premium_ok = bool(kc_inside_bb and both_walls >= 35)
    align = _align_walls_with_strong_candles(walls, profiles, spot)

    # Component scores shown to the user.
    tech_score = round(50 + directional / 2.0, 1)
    wall_score = round((put_score + call_score) / 2.0 if put_score and call_score else max(put_score, call_score), 1)
    intraday_score = round((h4.get("score", 50) * 0.60) + (h2.get("score", 50) * 0.40), 1)
    iv_score = 75 if 35 <= iv_rank <= 65 else 70 if iv_rank > 65 else 55 if iv_rank >= 20 else 45
    if (vix or {}).get("regime") == "HIGH_VOL":
        iv_score = max(35, iv_score - 12)
    squeeze_score = 82 if range_premium_ok else 78 if squeeze else 66 if kc_inside_bb else 50
    if bb_inside_kc:
        squeeze_score = max(squeeze_score, 74)

    # Approach scores.  Seller-side weekly planning should not chase a bullish PS
    # when price is stretched into call-wall/upper-band pressure.  In that case
    # CS/IC should rise because call selling or range premium is the cleaner use
    # of the weekly OI map.
    ps = 45 + directional * 0.18 + put_score * 0.24 + (10 if iv_rank >= 35 else -4) + align.get("score", 0) * 0.06
    cs = 45 - directional * 0.18 + call_score * 0.24 + (10 if iv_rank >= 35 else -4) + align.get("score", 0) * 0.06
    ic = 40 + both_walls * 0.30 + (14 if abs(directional) <= 35 else -4) + (10 if iv_rank >= 35 else -6)
    cb = 42 + directional * 0.30 + (14 if iv_rank <= 35 else -4) + (8 if h4.get("bias_score", 0) > 20 and h2.get("bias_score", 0) > 0 else 0)
    pb = 42 - directional * 0.30 + (14 if iv_rank <= 35 else -4) + (8 if h4.get("bias_score", 0) < -20 and h2.get("bias_score", 0) < 0 else 0)

    if range_premium_ok:
        ic += 14
    if squeeze:
        ic += 6
    if stretched_up:
        ps -= 10
        cs += 10
        ic += 6
        cb -= 8
    if stretched_down:
        cs -= 10
        ps += 8
        ic += 4
        pb -= 8
    if call_score >= put_score + 8:
        cs += 5
        ps -= 4
    if put_score >= call_score + 8:
        ps += 4
    if pcr is not None:
        try:
            p = float(pcr)
            if p >= 1.4:
                ps += 4
                ic += 3
            elif p <= 0.75:
                cs += 4
                ic += 2
        except Exception:
            pass

    approaches = [
        {"name": "PS", "label": "Bull Put Spread", "score": round(_clip(ps), 1), "why": "Bull/neutral bias + actionable put-wall support + IV premium."},
        {"name": "CS", "label": "Bear Call Spread", "score": round(_clip(cs), 1), "why": "Call-wall resistance, stretched upper-band risk, or bearish/neutral bias."},
        {"name": "IC", "label": "Iron Condor", "score": round(_clip(ic), 1), "why": "Two-sided weekly walls plus BB/Keltner range-premium conditions."},
        {"name": "CB", "label": "Call Debit/Buy", "score": round(_clip(cb), 1), "why": "Low IV bullish directional continuation."},
        {"name": "PB", "label": "Put Debit/Buy", "score": round(_clip(pb), 1), "why": "Low IV bearish directional continuation."},
    ]
    approaches.sort(key=lambda x: x["score"], reverse=True)
    top = approaches[0]
    composite = round(0.27 * tech_score + 0.27 * wall_score + 0.18 * intraday_score + 0.13 * iv_score + 0.15 * squeeze_score, 1)
    bias = "BULLISH" if directional >= 25 else "BEARISH" if directional <= -25 else "NEUTRAL"
    pa_note = (
        f"Daily BB% {d.get('bb_pct', '—')}, Weekly BB% {w.get('bb_pct', '—')}; "
        f"KC-inside-BB={'yes' if kc_inside_bb else 'no'}, BB-inside-KC={'yes' if bb_inside_kc else 'no'}; "
        f"stretch={'up' if stretched_up else 'down' if stretched_down else 'neutral'}."
    )
    return {
        "run_use": "Monday 10:00 ET plan for the coming Friday expiry",
        "target_expiry": expiry,
        "target_dte": dte,
        "directional_bias": bias,
        "directional_score": directional,
        "composite_score": composite,
        "preferred": top,
        "approaches": approaches,
        "component_scores": {
            "technical_trend": tech_score,
            "oi_walls": wall_score,
            "two_four_hour_confirmation": intraday_score,
            "iv_expected_move": iv_score,
            "squeeze_stretch": squeeze_score,
            "wall_candle_alignment": align.get("score", 0),
            "price_action_bb_kc": squeeze_score,
        },
        "setup_notes": [
            f"Daily stretch state: {stretch_d}; Weekly stretch state: {stretch_w}.",
            pa_note,
            f"Expected Friday move: {expected_move.get('display') if expected_move else '—'}.",
            f"PCR {pcr}; IV rank {iv_rank}.",
        ] + (align.get("notes") or []),
        "range_premium_ok": range_premium_ok,
        "stretched_up": stretched_up,
        "stretched_down": stretched_down,
    }


@spy_bp.route("/weekly")
def api_weekly():
    """Weekly Trading Plan — 5-10 DTE options for SPY, QQQ, IWM etc."""
    import math, sqlite3, traceback
    from datetime import date as dt_date, timedelta as dt_td
    from pathlib import Path

    sym        = (request.args.get("symbol") or "SPY").upper()
    sel_expiry = request.args.get("expiry", "")
    oi_sig_pct = _oi_sig_threshold_from_args(request.args, 30.0)
    bias_override = (request.args.get("bias_override") or "").strip().lower() or None
    if bias_override not in (None, "bullish", "bearish", "sideways"):
        bias_override = None
    from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
    DB = _OIAPP_DB_PATH

    # ── helpers ───────────────────────────────────────────────────────────
    def ema_s(ser, p):
        k = 2/(p+1); out = list(ser)
        for i in range(1, len(out)): out[i] = ser[i]*k + out[i-1]*(1-k)
        return out

    def rsi_s(closes, p=14):
        out = [50.0]*len(closes)
        if len(closes) < p+1: return out
        gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
        ag = sum(gains[:p])/p; al = sum(losses[:p])/p
        for i in range(p, len(closes)):
            if i > p:
                ag = (ag*(p-1)+gains[i-1])/p
                al = (al*(p-1)+losses[i-1])/p
            out[i] = 100 - 100/(1+ag/al) if al > 0 else 100
        return out

    def macd_hist(closes):
        if len(closes) < 35: return 0.0
        ef = ema_s(closes, 12); es = ema_s(closes, 26)
        ml = [f-s for f,s in zip(ef,es)]
        return round(ml[-1] - ema_s(ml, 9)[-1], 4)

    def norm_cdf(x):
        if x < 0: return 1 - norm_cdf(-x)
        t = 1/(1+0.2316419*x)
        p = t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))))
        return 1 - 0.3989422803*math.exp(-0.5*x*x)*p

    def bs_pop(K, is_put, T, sig, S):
        if T<=0 or sig<=0 or S<=0 or K<=0: return 68.0
        d2 = (math.log(S/K)+(0.05-0.5*sig*sig)*T)/(sig*math.sqrt(T))
        pop = norm_cdf(d2) if is_put else norm_cdf(-d2)
        return round(min(92, max(45, pop*100)))

    def rs(x, itv): return round(round(x/itv)*itv, 2)

    # ── 1. Price + TA ─────────────────────────────────────────────────────
    spot = ema20 = ema50 = ema200 = 0.0
    rsi14_val = 50.0; rsi_ema_diff = 0.0; macd_h = 0.0
    iv_atm = 16.0; iv_rank = 50
    C = []

    for _p in ["1y","6mo","3mo"]:
        try:
            from ..services.market import get_history_cached
            h = get_history_cached(sym, period=_p, interval="1d")
            if h is not None and not h.empty and len(h) >= 20:
                C = h["Close"].tolist(); break
        except: pass

    if C:
        n      = len(C)-1
        spot   = round(C[n], 2)
        ema20  = round(ema_s(C, 20)[n], 2)
        ema50  = round(ema_s(C, min(50, n))[n], 2)
        ema200 = round(ema_s(C, min(200, n))[n], 2)
        rs14   = rsi_s(C)
        ea90   = ema_s(rs14, min(90, n))
        rsi14_val    = round(rs14[n], 1)
        rsi_ema_diff = round(rs14[n] - ea90[n], 1)
        macd_h       = macd_hist(C)
        if n >= 20:
            rets   = [math.log(C[i]/C[i-1]) for i in range(max(1,n-29), n+1) if C[i-1]>0]
            iv_atm = round(math.sqrt(252)*(sum(r*r for r in rets)/len(rets))**0.5*100, 1) if rets else 16.0

    # `spot == 0` doesn't catch NaN (NaN == 0 is False in Python), so a NaN
    # last-close from yfinance was silently passing both fallback checks
    # below and crashing later downstream (e.g. round(spot) on NaN raises
    # ValueError, not caught until the walls-fallback branch far below).
    # math.isfinite(spot) explicitly rejects NaN/inf, closing that gap.
    if not math.isfinite(spot) or spot == 0:
        spot = 0.0
        try:
            fi = yf.Ticker(sym).fast_info
            spot = round(float(getattr(fi,"last_price",0) or 0), 2)
            ema20 = ema50 = ema200 = spot
        except: pass

    if not math.isfinite(spot) or spot == 0:
        return jsonify({"error": f"Cannot fetch price for {sym}. Check internet connection."}), 500

    # Multi-timeframe profiles for the Monday-to-Friday Weekly Plan.
    # Daily = structure, 4H/2H = swing timing and strong candle/volume confirmation.
    profiles = {
        "1w": _tf_profile(sym, "1w"),
        "1d": _tf_profile(sym, "1d"),
        "4h": _tf_profile(sym, "4h"),
        "2h": _tf_profile(sym, "2h"),
    }

    # IV rank
    try:
        if len(C) >= 60:
            def _rv(cl, w=30):
                if len(cl)<w+1: return iv_atm
                r=[math.log(cl[i]/cl[i-1]) for i in range(len(cl)-w,len(cl)) if cl[i-1]>0]
                return round(math.sqrt(252)*(sum(x*x for x in r)/len(r))**0.5*100,1) if r else iv_atm
            rv_cur  = _rv(C)
            rv_hist = [_rv(C[:i]) for i in range(60, len(C)+1)]
            if rv_hist:
                iv_rank = round(sum(1 for v in rv_hist if v<=rv_cur)/len(rv_hist)*100)
    except: pass

    dprof = profiles.get("1d") or {}
    ta = {
        "symbol": sym,
        "price": spot,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi14": rsi14_val,
        "rsi_ema_diff": rsi_ema_diff,
        "macd_hist": macd_h,
        "iv_atm": iv_atm,
        "iv_rank": iv_rank,
        "atr": max(0.01, abs(spot) * 0.01),
        "volume": dprof.get("volume", 0.0),
        "avg_dollar_volume": (spot * float(dprof.get("volume") or 0.0)) if spot else 0.0,
    }

    # ── 2. Expiry ─────────────────────────────────────────────────────────
    exps = []
    try: exps = _future_exps(sym)
    except: pass
    if not exps:
        try: exps = list(yf.Ticker(sym).options[:8])
        except: pass
    if not exps:
        d = dt_date.today()
        while d.weekday() != 4: d += dt_td(1)
        exps = [(d+dt_td(7*i)).isoformat() for i in range(4)]

    today = dt_date.today()
    exp, dte = _friday_expiry(exps, sel_expiry if sel_expiry else None)
    if not exp:
        d = today
        while d.weekday() != 4: d += dt_td(1)
        exp = d.isoformat(); dte = max(1,(d-today).days)

    # ATM option IV + straddle expected move for the selected Friday expiry.
    chain_ctx = _option_chain_weekly_context(sym, exp, spot, iv_atm)
    if chain_ctx.get("atm_iv"):
        iv_atm = round(float(chain_ctx.get("atm_iv") or iv_atm), 2)
        ta["iv_atm"] = iv_atm

    # ── 3. OI walls + max pain ────────────────────────────────────────────
    oi_rows = []
    try: oi_rows = _oi_rows(sym, exp)
    except: pass
    if not oi_rows:
        try:
            from ..services.market import fetch_store_for
            fetch_store_for(sym, expirations=[exp]); oi_rows = _oi_rows(sym, exp)
        except: pass

    # Walls. Include per-strike GEX so wall significance can blend OI + OI change + proximity + GEX.
    sigma_1d_for_walls = round(spot * (iv_atm/100.0) * math.sqrt(1/252.0), 2) if spot and iv_atm else None
    weekly_oi_change_filter = _oi_change_filter_context(sym, oi_rows, expiry=exp, source="weekly_plan_target_expiry", min_change_pct=oi_sig_pct)
    gex_info = _compute_gex(oi_rows, spot, max(1, dte), iv_atm) if oi_rows else {}
    if oi_rows and spot > 0:
        walls_raw = _walls(oi_rows, spot, gex_info=gex_info, sigma_1d=sigma_1d_for_walls, oi_change_filter=weekly_oi_change_filter)
    else:
        walls_raw = {"support":round(spot*0.97,2),"resistance":round(spot*1.03,2),
                     "gamma_wall":round(spot),"interval":1.0,
                     "top_put_walls":[],"top_call_walls":[],
                     "significant_put_walls":[],"significant_call_walls":[],
                     "oi_change_filter": weekly_oi_change_filter}

    raw_sup = walls_raw.get("support",   round(spot*0.97,2))
    raw_res = walls_raw.get("resistance",round(spot*1.03,2))
    near_sup = raw_sup if raw_sup >= spot*0.88 else round(spot*0.97,2)
    near_res = raw_res if raw_res <= spot*1.12 else round(spot*1.03,2)
    walls = dict(walls_raw)
    walls["support"] = near_sup; walls["resistance"] = near_res
    walls["raw_support"] = raw_sup; walls["raw_resistance"] = raw_res
    for k,v in [("gamma_wall",round(spot)),("interval",1.0),
                ("top_put_walls",[]),("top_call_walls",[])]:
        walls.setdefault(k, v)

    # Max pain
    max_pain = spot
    if oi_rows:
        try:
            strikes = sorted(set(float(r["strike"]) for r in oi_rows))
            best = float("inf")
            for K in strikes:
                pain = sum(
                    max(0, K-float(r["strike"]))*int(r["oi"] or 0) if r["type"]=="call"
                    else max(0, float(r["strike"])-K)*int(r["oi"] or 0)
                    for r in oi_rows)
                if pain < best: best=pain; max_pain=K
        except: pass

    # PCR
    all_rows = []
    try:
        for e_ in (exps[:4] if exps else [exp]):
            all_rows.extend(_oi_rows(sym, e_) or [])
    except: pass
    tc = sum(int(r["oi"] or 0) for r in all_rows if r.get("type")=="call")
    tp = sum(int(r["oi"] or 0) for r in all_rows if r.get("type")=="put")
    pcr = round(tp/max(1,tc), 3)

    # ── 4. Weekly expected move ───────────────────────────────────────────
    T_wk     = max(dte,1)/252.0
    sigma_wk = round(spot*(iv_atm/100)*math.sqrt(T_wk), 2)
    straddle_move = chain_ctx.get("straddle_move")
    friday_move = round(float(straddle_move), 2) if straddle_move else sigma_wk
    upper_1  = round(spot+friday_move,2);   lower_1 = round(spot-friday_move,2)
    upper_2  = round(spot+2*friday_move,2); lower_2 = round(spot-2*friday_move,2)
    expected_move = {
        "source": "ATM straddle" if straddle_move else "IV sigma",
        "move": friday_move,
        "move_pct": round(friday_move / max(0.01, spot) * 100.0, 2),
        "range_low": lower_1,
        "range_high": upper_1,
        "range_low_2sd": lower_2,
        "range_high_2sd": upper_2,
        "display": f"±${friday_move} ({round(friday_move / max(0.01, spot) * 100.0, 2)}%) to ${lower_1}–${upper_1}",
    }

    # Shared weekly OI context.  This is the important alignment fix: Weekly
    # Plan now uses the same active-week aggregate and strike band as the
    # Aggregate screen instead of letting deep all-expiry OI dominate.
    weekly_oi_context = {}
    if _shared_weekly_oi_context is not None:
        try:
            weekly_oi_context = _shared_weekly_oi_context(sym, exp, spot=spot, count=5, per_side=12, max_expiries=7)
            target_rows = weekly_oi_context.get("target_rows") or []
            action_rows = weekly_oi_context.get("actionable_rows") or []
            if target_rows:
                oi_rows = target_rows
            _filter_rows = target_rows or weekly_oi_context.get("aggregate_rows") or action_rows
            _filter_total = _rows_total_oi(target_rows) if target_rows else ((weekly_oi_context.get("totals_all") or {}).get("total_oi") or _rows_total_oi(_filter_rows))
            weekly_oi_change_filter = _oi_change_filter_context(
                sym, _filter_rows, expiry=exp, source="weekly_plan_target_expiry" if target_rows else "weekly_plan_aggregate", total_oi=_filter_total, min_change_pct=oi_sig_pct
            )
            weekly_oi_context["oi_change_filter"] = weekly_oi_change_filter
            if action_rows:
                # Use actionable cumulative rows for support/resistance walls.
                walls_raw = _walls(action_rows, spot, gex_info={}, sigma_1d=sigma_1d_for_walls, oi_change_filter=weekly_oi_change_filter)
                walls_raw["source"] = "weekly_oi_context/actionable aggregate"
                walls_raw["aggregate_expirations"] = weekly_oi_context.get("expirations") or []
                walls_raw["selected_strikes"] = weekly_oi_context.get("selected_strikes") or []
                walls_raw["raw_put_walls"] = weekly_oi_context.get("raw_put_walls") or []
                walls_raw["raw_call_walls"] = weekly_oi_context.get("raw_call_walls") or []
                raw_sup = walls_raw.get("support", round(spot*0.97,2))
                raw_res = walls_raw.get("resistance", round(spot*1.03,2))
                # Actionable walls are already band-limited; do not replace them
                # with synthetic 3% levels unless missing.
                near_sup = raw_sup if raw_sup else round(spot*0.97,2)
                near_res = raw_res if raw_res else round(spot*1.03,2)
                walls = dict(walls_raw)
                walls["support"] = near_sup; walls["resistance"] = near_res
                walls["raw_support"] = (weekly_oi_context.get("raw_put_walls") or [{}])[0].get("strike", raw_sup) if weekly_oi_context.get("raw_put_walls") else raw_sup
                walls["raw_resistance"] = (weekly_oi_context.get("raw_call_walls") or [{}])[0].get("strike", raw_res) if weekly_oi_context.get("raw_call_walls") else raw_res
                for k,v in [("gamma_wall",round(spot)),("interval",_shared_strike_interval(action_rows, spot) if _shared_strike_interval else walls.get("interval",1.0)),
                            ("top_put_walls",[]),("top_call_walls",[])]:
                    walls.setdefault(k, v)
            if weekly_oi_context.get("pcr_actionable") is not None:
                pcr = round(float(weekly_oi_context.get("pcr_actionable")), 3)
            elif weekly_oi_context.get("pcr_all") is not None:
                pcr = round(float(weekly_oi_context.get("pcr_all")), 3)
            mp = weekly_oi_context.get("actionable_max_pain") or weekly_oi_context.get("target_max_pain") or weekly_oi_context.get("aggregate_max_pain")
            if mp:
                max_pain = float(mp)
        except Exception as _woi_exc:
            weekly_oi_context = {"error": str(_woi_exc)}

    # ── 5. Futures OI ────────────────────────────────────────────────────
    futures_bias="NEUTRAL"; futures_note="No futures data"; futures_oi_chg=0
    ROOTS={"SPY":"ES","QQQ":"NQ","IWM":"RTY","DIA":"YM"}
    root=ROOTS.get(sym,"ES")
    try:
        con = sqlite3.connect(DB)
        rf  = con.execute(
            "SELECT trade_date,oi FROM futures_oi WHERE contract LIKE ? AND oi>0 ORDER BY date DESC LIMIT 6",
            (f"{root}%",)).fetchall()
        if not rf:
            rf = con.execute(
                "SELECT trade_date,oi FROM futures_oi_daily WHERE symbol=? AND oi>0 ORDER BY trade_date DESC LIMIT 6",
                (sym,)).fetchall()
        con.close()
        if len(rf)>=2:
            chg = rf[0][1]-rf[-1][1]; futures_oi_chg=chg
            pct = chg/max(1,rf[0][1])*100
            if pct>1:   futures_bias="BULLISH"; futures_note=f"{root} OI +{chg:,} — accumulating"
            elif pct<-1:futures_bias="BEARISH"; futures_note=f"{root} OI {chg:,} — reducing"
            else:        futures_note=f"{root} OI flat"
    except: pass

    # ── 6. Bias score ────────────────────────────────────────────────────
    score=0; factors=[]
    def fac(name, pts, bull):
        factors.append({"name":name,"value":f"+{pts}" if isinstance(pts,int) and pts>0 else str(pts),"bull":bull})

    e50p  = (spot-ema50) /ema50 *100 if ema50  else 0
    e200p = (spot-ema200)/ema200*100 if ema200 else 0
    if   e50p > 1:   score+=2; fac(f"Above EMA50 (+{e50p:.1f}%)",   +2, True)
    elif e50p < -1:  score-=2; fac(f"Below EMA50 ({e50p:.1f}%)",    -2, False)
    else:                       fac(f"Near EMA50 ({e50p:.1f}%)",      0, None)
    if ema200>0 and ema200!=spot:
        if   e200p > 1:  score+=2; fac(f"Above EMA200 (+{e200p:.1f}%)", +2, True)
        elif e200p < -1: score-=2; fac(f"Below EMA200 ({e200p:.1f}%)",  -2, False)
        else:                       fac(f"Near EMA200 ({e200p:.1f}%)",    0, None)
    if   rsi_ema_diff>=15:  score+=2; fac(f"RSI-EMA {rsi_ema_diff:+.1f} (strong)",+2,True)
    elif rsi_ema_diff>=8:   score+=1; fac(f"RSI-EMA {rsi_ema_diff:+.1f}",          +1,True)
    elif rsi_ema_diff<=-15: score-=2; fac(f"RSI-EMA {rsi_ema_diff:+.1f} (strong)",-2,False)
    elif rsi_ema_diff<=-8:  score-=1; fac(f"RSI-EMA {rsi_ema_diff:+.1f}",          -1,False)
    else:                               fac(f"RSI-EMA {rsi_ema_diff:+.1f} (neutral)", 0,None)
    thr = abs(spot)*0.0001
    if   macd_h>thr:   score+=1; fac(f"MACD hist {macd_h:+.3f}",  +1,True)
    elif macd_h<-thr:  score-=1; fac(f"MACD hist {macd_h:+.3f}",  -1,False)
    else:                          fac(f"MACD hist ~0 (neutral)",    0,None)
    if   pcr>1.2:  score+=1; fac(f"PCR {pcr} (put-heavy)",   +1,True)
    elif pcr<0.8:  score-=1; fac(f"PCR {pcr} (call-heavy)",  -1,False)
    else:                     fac(f"PCR {pcr} (balanced)",     0,None)
    if   futures_bias=="BULLISH": score+=2; fac("Futures OI building",   +2,True)
    elif futures_bias=="BEARISH": score-=2; fac("Futures OI unwinding",  -2,False)
    else:                                    fac("Futures OI flat",        0,None)
    mp_pct=(max_pain-spot)/spot*100 if spot else 0
    if   mp_pct>0.5:  score+=1; fac(f"Max pain ${max_pain:.0f} above (+{mp_pct:.1f}%)", +1,True)
    elif mp_pct<-0.5: score-=1; fac(f"Max pain ${max_pain:.0f} below ({mp_pct:.1f}%)",  -1,False)
    else:                         fac(f"Max pain ${max_pain:.0f} (near spot)",             0,None)

    bias = "BULLISH" if score>=3 else "BEARISH" if score<=-3 else "NEUTRAL"
    confidence = max(5, min(95, round(abs(score)/9*100)))

    # ── 7. Option chain for real prices ──────────────────────────────────
    puts_df = calls_df = None
    if exp:
        try:
            ch = yf.Ticker(sym).option_chain(exp)
            puts_df  = ch.puts  if ch.puts  is not None and len(ch.puts)  > 0 else None
            calls_df = ch.calls if ch.calls is not None and len(ch.calls) > 0 else None
        except: pass
    price_src = "live" if puts_df is not None else "est"

    def mid(df, K):
        if df is None or df.empty: return 0.0
        row = df[abs(df["strike"]-K)<0.01]
        if row.empty: return 0.0
        b=float(row["bid"].iloc[0] or 0); a=float(row["ask"].iloc[0] or 0)
        if b<=0 and a<=0: return float(row["lastPrice"].iloc[0] or 0)
        if b<=0: return round(a*0.8, 2)
        return round((b+a)/2, 2)

    def spread_cr(sk, lk, is_put):
        df = puts_df if is_put else calls_df
        sh = mid(df, sk); lg = mid(df, lk)
        if sh > 0: return max(0.05, round(sh-lg, 2))
        # BS fallback
        T=max(dte,1)/252.0; sig=iv_atm/100.0
        if is_put: sig *= skew_f
        def _p(K):
            m=abs(math.log(spot/K))/(sig*math.sqrt(T)) if spot>0 and K>0 and T>0 and sig>0 else 99
            return round(max(0.05, 0.4*spot*sig*math.sqrt(T)*math.exp(-0.5*m*m)),2)
        return max(0.05, round(_p(sk)-_p(lk), 2))

    # Weekly robustness overlays for core ETFs and liquid stocks
    liquidity = _weekly_liquidity_profile(sym, ta)
    rs_profile = _weekly_relative_strength_profile(sym, "SPY")
    earn_profile = _weekly_earnings_profile(sym)
    vix_ctx = _vix_context()
    plan_score = _weekly_plan_composite(sym, spot, exp, dte, profiles, walls, iv_rank, expected_move, pcr, vix=vix_ctx, bias_override=bias_override)

    weekly_overlays = {
        "liquidity": liquidity,
        "relative_strength": rs_profile,
        "earnings": earn_profile,
        "setup_type": (
            "CORE ETF" if sym in {"SPY", "QQQ", "IWM"} else
            "TREND" if score >= 2 else
            "MEAN REVERSION" if score <= -2 else
            "NEUTRAL"
        ),
        "weekly_bias": bias,
        "weekly_confidence": confidence,
    }

    # ── 8. Strategy params ────────────────────────────────────────────────
    support    = walls["support"];    resistance = walls["resistance"]
    raw_sup    = walls["raw_support"]; raw_res   = walls["raw_resistance"]
    gw         = walls.get("gamma_wall", round(spot))
    itv        = max(1.0, walls.get("interval",1.0) or 1.0)
    skew_f     = 1.0+min(0.35, max(0.1,(pcr-0.8)*0.2))

    # ~25-delta OTM using BS approximation
    T_s    = max(dte,1)/252.0; sig_s = iv_atm/100.0
    otm    = max(0.015, min(0.07, sig_s*math.sqrt(T_s)*0.674))
    ps_tgt = rs(spot*(1-otm), itv)    # put short target
    cs_tgt = rs(spot*(1+otm), itv)    # call short target
    wing_w = max(5*itv, round(sigma_wk*0.40/itv)*itv)

    # Treat OI walls as pressure zones, not sell-at strikes.  Sell below the
    # put-wall cluster and above the call-wall cluster, while respecting the
    # 1σ Friday expected move.  This is what keeps SPY 740/745P and 750/755C
    # walls from becoming short strikes inside the pin zone.
    _put_vals = [float(w.get("strike")) for w in (walls.get("significant_put_walls") or [])[:4] if w.get("strike") is not None]
    _call_vals = [float(w.get("strike")) for w in (walls.get("significant_call_walls") or [])[:4] if w.get("strike") is not None]
    put_cluster_low = min(_put_vals) if _put_vals else near_sup
    put_cluster_high = max(_put_vals) if _put_vals else near_sup
    call_cluster_low = min(_call_vals) if _call_vals else near_res
    call_cluster_high = max(_call_vals) if _call_vals else near_res
    if put_cluster_low:
        ps_tgt = rs(min(ps_tgt, put_cluster_low - wing_w, spot - friday_move * 1.15), itv)
    if call_cluster_high:
        cs_tgt = rs(max(cs_tgt, call_cluster_high + wing_w, spot + friday_move * 1.00), itv)

    # ── IV regime classification ──────────────────────────────────────────
    # iv_rank > 50  → sell premium (credit spreads give good reward/risk)
    # iv_rank 30-50 → borderline; put skew helps bull put, show both
    # iv_rank < 30  → buy premium (debit spreads; not enough credit to collect)
    # iv_rank < 20  → buy premium + calendar (IV likely to expand)
    CREDIT_THRESHOLD = 50    # above this: credit is clearly preferred
    DEBIT_THRESHOLD  = 30    # below this: debit is clearly preferred
    # Bull put spreads get a skew bonus (put skew inflates put premiums)
    BULL_PUT_BONUS   = 8     # bull put viable down to iv_rank 42 due to skew

    def iv_label():
        if iv_rank >= CREDIT_THRESHOLD: return f"IV rank {iv_rank} — HIGH, sell premium ✅"
        if iv_rank >= DEBIT_THRESHOLD:  return f"IV rank {iv_rank} — MODERATE, both viable"
        if iv_rank >= 20:               return f"IV rank {iv_rank} — LOW, buy premium ✅"
        return f"IV rank {iv_rank} — VERY LOW, buy premium / calendar ✅"

    def iv_warning(is_credit):
        if is_credit and iv_rank < DEBIT_THRESHOLD:
            return f"⚠ IV rank {iv_rank} is LOW — credit is thin. Debit spread preferred."
        if is_credit and iv_rank < CREDIT_THRESHOLD:
            return f"ℹ IV rank {iv_rank} is moderate — credit viable but not ideal."
        if not is_credit and iv_rank >= CREDIT_THRESHOLD:
            return f"ℹ IV rank {iv_rank} is HIGH — selling premium would give better R:R."
        return ""

    strats = []

    if score >= 2:       # BULLISH
        # ── Credit: Bull Put Spread (viable when IV rank ≥ 42 due to put skew)
        if iv_rank >= DEBIT_THRESHOLD + BULL_PUT_BONUS:
            sk=ps_tgt; lk=rs(sk-wing_w,itv)
            cr=spread_cr(sk,lk,True)
            warn=iv_warning(True)
            strats.append({"type":"Bull Put Spread","bias":"BULL",
                "iv_context":iv_label(),
                "legs":[{"side":"sell","type":"put","strike":sk,"expiry":exp},
                        {"side":"buy","type":"put","strike":lk,"expiry":exp}],
                "strikes":f"${lk}/{sk} ({wing_w:.0f}-wide)",
                "entry":f"Credit ${cr} ({price_src}) · put skew ×{skew_f:.2f}",
                "max_profit":round(cr*100),"max_loss":round(max(0.01,wing_w-cr)*100),
                "pop":bs_pop(sk,True,T_s,sig_s,spot),
                "target":f"${max_pain:.0f} max pain",
                "edge":f"OI support ${near_sup:.0f} · put skew ×{skew_f:.2f} · IV rank {iv_rank}",
                "rationale":(warn+(" " if warn else "")+
                    f"Short ${sk:.0f}P ({otm*100:.1f}% OTM, ~25Δ). "
                    f"Put skew boosts credit even in moderate IV. "
                    f"OI support ${near_sup:.0f} (deep wall ${raw_sup:.0f}).")})

        # ── Debit: Bull Call Spread (always viable but best when IV rank < 50)
        if iv_rank < CREDIT_THRESHOLD or iv_rank < DEBIT_THRESHOLD + BULL_PUT_BONUS:
            lk=cs_tgt; sk=rs(near_res,itv)
            if sk<=lk: sk=rs(lk+wing_w,itv)
            db=spread_cr(lk,sk,False)
            warn=iv_warning(False)
            strats.append({"type":"Bull Call Spread","bias":"BULL",
                "iv_context":iv_label(),
                "legs":[{"side":"buy","type":"call","strike":lk,"expiry":exp},
                        {"side":"sell","type":"call","strike":sk,"expiry":exp}],
                "strikes":f"${lk}/{sk} ({round(sk-lk,0):.0f}-wide)",
                "entry":f"Debit ${db} ({price_src})",
                "max_profit":round(max(0.01,sk-lk-db)*100),"max_loss":round(db*100),
                "pop":bs_pop(lk,False,T_s,sig_s,spot),
                "target":f"${near_res:.0f} call wall",
                "edge":f"Low IV → cheaper debit · bullish momentum · call wall ${near_res:.0f}",
                "rationale":(warn+(" " if warn else "")+
                    f"Buy {lk:.0f}C / sell {sk:.0f}C. "
                    f"Low IV = cheap premium to buy. "
                    f"Target ${near_res:.0f} resistance. Max risk = debit paid.")})

    _pref_code = (plan_score.get("preferred") or {}).get("name")
    _stretched_up = bool(plan_score.get("stretched_up"))
    if score <= -2 or _pref_code in {"CS", "IC"} or _stretched_up:      # BEARISH / call-ceiling / IC alternate
        # ── Credit: Bear Call Spread (viable when IV rank ≥ 50, calls cheaper than puts)
        if iv_rank >= CREDIT_THRESHOLD:
            sk=cs_tgt; lk=rs(sk+wing_w,itv)
            cr=spread_cr(sk,lk,False)
            warn=iv_warning(True)
            strats.append({"type":"Bear Call Spread","bias":"BEAR",
                "iv_context":iv_label(),
                "legs":[{"side":"sell","type":"call","strike":sk,"expiry":exp},
                        {"side":"buy","type":"call","strike":lk,"expiry":exp}],
                "strikes":f"${sk}/{lk} ({wing_w:.0f}-wide)",
                "entry":f"Credit ${cr} ({price_src})",
                "max_profit":round(cr*100),"max_loss":round(max(0.01,wing_w-cr)*100),
                "pop":bs_pop(sk,False,T_s,sig_s,spot),
                "target":f"${max_pain:.0f} max pain",
                "edge":f"OI resistance ${near_res:.0f} · IV rank {iv_rank}",
                "rationale":(warn+(" " if warn else "")+
                    f"Short ${sk:.0f}C ({otm*100:.1f}% OTM, ~25Δ). "
                    f"High IV = good call premium. OI resistance ${near_res:.0f} (deep ${raw_res:.0f}).")})

        # ── Debit: Bear Put Spread (best when IV rank < 50, skew makes puts expensive)
        if iv_rank < CREDIT_THRESHOLD:
            lk=ps_tgt; sk=rs(near_sup,itv)
            if sk>=lk: sk=rs(lk-wing_w,itv)
            db=spread_cr(lk,sk,True)
            warn=iv_warning(False)
            strats.append({"type":"Bear Put Spread","bias":"BEAR",
                "iv_context":iv_label(),
                "legs":[{"side":"buy","type":"put","strike":lk,"expiry":exp},
                        {"side":"sell","type":"put","strike":sk,"expiry":exp}],
                "strikes":f"${sk}/{lk} ({round(lk-sk,0):.0f}-wide)",
                "entry":f"Debit ${db} ({price_src}) · skew ×{skew_f:.2f}",
                "max_profit":round(max(0.01,lk-sk-db)*100),"max_loss":round(db*100),
                "pop":bs_pop(lk,True,T_s,sig_s,spot),
                "target":f"${near_sup:.0f} support",
                "edge":f"Bearish momentum · put skew ×{skew_f:.2f} · support ${near_sup:.0f}",
                "rationale":(warn+(" " if warn else "")+
                    f"Buy {lk:.0f}P / sell {sk:.0f}P. "
                    f"Put skew means put debit spreads have good R:R even at moderate IV. "
                    f"Target ${near_sup:.0f} support.")})

    # ── Iron Condor (only when IV rank ≥ 40, otherwise premium too thin) ──
    cs=cs_tgt; cl=rs(cs+wing_w,itv); ps=ps_tgt; pl=rs(ps-wing_w,itv)
    cr_c=spread_cr(cs,cl,False); cr_p=spread_cr(ps,pl,True)
    cr_ic=round(cr_c+cr_p,2)
    if iv_rank >= 40:
        strats.append({"type":"Iron Condor","bias":"NEUTRAL",
            "iv_context":f"IV rank {iv_rank} — {'ideal ✅' if iv_rank>=50 else 'viable (prefer 50+)'}",
            "legs":[{"side":"sell","type":"call","strike":cs,"expiry":exp},
                    {"side":"buy","type":"call","strike":cl,"expiry":exp},
                    {"side":"sell","type":"put","strike":ps,"expiry":exp},
                    {"side":"buy","type":"put","strike":pl,"expiry":exp}],
            "strikes":f"${pl}/{ps} | ${cs}/{cl} ({wing_w:.0f}-wide)",
            "entry":f"Credit ${cr_ic} ({price_src}) · put ${cr_p} + call ${cr_c} · skew ×{skew_f:.2f}",
            "max_profit":round(cr_ic*100),
            "max_loss":round(max((wing_w-cr_p),(wing_w-cr_c))*100),
            "pop":70,"target":f"${max_pain:.0f} max pain · stay ${ps:.0f}–${cs:.0f}",
            "edge":f"Outside actionable weekly OI clusters ${put_cluster_low:.0f}–${put_cluster_high:.0f}P and ${call_cluster_low:.0f}–${call_cluster_high:.0f}C · 1σ=±${friday_move:.2f}",
            "rationale":f"High IV/range premium → good premium on both sides. Short {ps:.0f}P/{cs:.0f}C. "
                f"Profit zone ${ps:.0f}–${cs:.0f} brackets 1σ range ${lower_1}–${upper_1}. "
                f"Actionable OI clusters: puts ${put_cluster_low:.0f}–${put_cluster_high:.0f}, calls ${call_cluster_low:.0f}–${call_cluster_high:.0f}."})
    else:
        # Low IV → IC not recommended, suggest debit butterfly instead
        mid_k = rs(spot, itv)
        strats.append({"type":"⚠ Iron Condor — NOT recommended","bias":"NEUTRAL",
            "iv_context":f"IV rank {iv_rank} — TOO LOW for IC. Use debit spread or calendar.",
            "legs":[{"side":"sell","type":"call","strike":cs,"expiry":exp},
                    {"side":"buy","type":"call","strike":cl,"expiry":exp},
                    {"side":"sell","type":"put","strike":ps,"expiry":exp},
                    {"side":"buy","type":"put","strike":pl,"expiry":exp}],
            "strikes":f"${pl}/{ps} | ${cs}/{cl}",
            "entry":f"Credit only ${cr_ic} ({price_src}) — too thin",
            "max_profit":round(cr_ic*100),
            "max_loss":round(max((wing_w-cr_p),(wing_w-cr_c))*100),
            "pop":70,"target":"N/A",
            "edge":"❌ Not recommended — premium too thin to justify the risk",
            "rationale":f"IV rank {iv_rank} is too low for Iron Condors. "
                f"With only ${cr_ic} credit on ${wing_w:.0f}-wide wings, R:R is unfavorable. "
                f"Consider: Bull/Bear debit spread (if directional) or ATM Calendar (if neutral)."})

    # ── Calendar (best when IV rank < 25) ────────────────────────────────
    if iv_rank < 30 and len(exps) > 2:
        cal_k=rs(gw,itv); back=exps[min(len(exps)-1,2)]
        strats.append({"type":"ATM Calendar","bias":"NEUTRAL",
            "iv_context":f"IV rank {iv_rank} — LOW IV, buy vega ✅",
            "legs":[{"side":"sell","type":"call","strike":cal_k,"expiry":exp},
                    {"side":"buy","type":"call","strike":cal_k,"expiry":back}],
            "strikes":f"${cal_k} | sell {exp} / buy {back}",
            "entry":f"Debit ~${round(sigma_wk*0.06,2)} (est)",
            "max_profit":None,"max_loss":round(sigma_wk*0.06*100),"pop":58,
            "target":f"Pin at γ-wall ${gw:.0f}",
            "edge":f"IV rank {iv_rank} → IV likely to expand · buy cheap vega now",
            "rationale":f"Low IV environment: sell front-week, buy back-week at γ-wall ${gw:.0f}. "
                f"Profits from time decay differential AND potential IV expansion. "
                f"Best risk-adjusted trade when IV rank < 30."})

    # Display candidates in the same order as the scored approach list, not in
    # construction order.
    try:
        _score_map = {a.get("name"): float(a.get("score") or 0) for a in (plan_score.get("approaches") or [])}
        def _code_for_strategy(st):
            t = str(st.get("type") or "").lower()
            if "iron condor" in t: return "IC"
            if "bear call" in t: return "CS"
            if "bull put" in t: return "PS"
            if "bull call" in t: return "CB"
            if "bear put" in t: return "PB"
            return ""
        for _st in strats:
            _st["approach_score"] = round(_score_map.get(_code_for_strategy(_st), 0), 1)
        strats.sort(key=lambda st: (_score_map.get(_code_for_strategy(st), 0), float(st.get("pop") or 0)), reverse=True)
    except Exception:
        pass

    payload = {
        "symbol":sym,"expiry":exp,"dte":dte,"expiry_list":exps[:8],
        "spot":round(spot,2),"date":dt_date.today().isoformat(),
        "plan_name":"Weekly Plan",
        "plan_use":"Run Monday around 10:00 ET to plan Friday SPY/QQQ structures",
        "bias":bias,"score":score,"confidence":confidence,"factors":factors,
        "pcr":pcr,"max_pain":round(max_pain,2),"walls":walls,
        "gex":gex_info,
        "futures_bias":futures_bias,"futures_note":futures_note,"futures_oi_chg":futures_oi_chg,
        "iv_atm":round(iv_atm,2),"iv_rank":iv_rank,"option_chain":chain_ctx,"vix":vix_ctx,
        "rsi":round(rsi14_val,1),"rsi_ema_diff":round(rsi_ema_diff,1),
        "macd_hist":round(macd_h,4),"adx":(profiles.get("1d") or {}).get("adx",20),
        "ema20":round(ema20,2),"ema50":round(ema50,2),"ema200":round(ema200,2),
        "skew_factor":round(skew_f,2),"otm_pct":round(otm*100,1),
        "expected_move":expected_move,
        "timeframes":profiles,
        "weekly_plan_score":plan_score,
        "weekly_oi_context": weekly_oi_context,
        "oi_change_filter": weekly_oi_change_filter,
        "week_outlook":{
            "range_low_1sd":lower_1,"range_high_1sd":upper_1,
            "range_low_2sd":lower_2,"range_high_2sd":upper_2,
            "sigma_week":friday_move,"iv_sigma_week":sigma_wk,"max_pain":round(max_pain,2),
            "gamma_wall":gw,"support":round(near_sup,2),"resistance":round(near_res,2),
            "put_walls":walls.get("top_put_walls",[])[:3],
            "call_walls":walls.get("top_call_walls",[])[:3],
            "significant_put_walls":walls.get("significant_put_walls",[])[:5],
            "significant_call_walls":walls.get("significant_call_walls",[])[:5],
        },
        "weekly_overlays": weekly_overlays,
        "strategies":strats,
    }
    return jsonify(_json_safe(payload))


def api_full():
    sym=(request.args.get("symbol") or "SPY").upper()
    ta=_compute_ta(sym)
    if not ta: return jsonify({"error":"TA failed"}),500
    exps=_future_exps(sym)
    if not exps:
        try: exps=list(yf.Ticker(sym).options[:7])
        except: exps=[]
    out={"symbol":sym,"ta":ta,"daily":None,"weekly":None}
    for label,(mn,mx,fi) in [("daily",(0,3,0)),("weekly",(5,12,1))]:
        exp,dte=_pick_exp(exps,mn,mx,fi)
        if not exp: continue
        rows=_oi_rows(sym,exp)
        spot=ta["price"]
        w=_walls(rows,spot) if rows else {"support":round(spot*0.99,2),"resistance":round(spot*1.01,2),"gamma_wall":round(spot,0),"interval":ta["atr"]*0.5,"top_put_walls":[],"top_call_walls":[]}
        out[label]={"expiry":exp,"dte":dte,"walls":w,"strategies":_build_strats(sym,exp,ta,w,f"{label} ({dte}d)")}
    return jsonify(out)


# ═══════════════════════════════════════════════════════════════════════════
# GEX-BASED DAILY TRADING PLAN  (TradeHive-style)
# ═══════════════════════════════════════════════════════════════════════════

import math as _math

def _bs_gamma(S, K, T_days, iv, r=0.0):
    """Black-Scholes gamma at a given strike."""
    if T_days <= 0 or iv <= 0 or S <= 0 or K <= 0:
        return 0.0
    T = max(T_days / 262.0, 1/262.0)  # Subhadip Method: T = biz_days/262, r=0
    v = iv / 100.0
    d1 = (_math.log(S / K) + 0.5 * v * v * T) / (v * _math.sqrt(T))
    phi_d1 = _math.exp(-0.5 * d1 * d1) / _math.sqrt(2 * _math.pi)
    return phi_d1 / (S * v * _math.sqrt(T))


def _gex_at_price(rows, price, T_days, iv_atm):
    """Signed net dealer gamma exposure at a hypothetical underlying price."""
    total = 0.0
    for r in rows:
        K = float(r["strike"])
        dist_pct = (K - price) / price * 100 if price else 0.0
        if r["type"] == "put" and K < price:
            strike_iv = iv_atm * (1 + abs(dist_pct) * 0.025)
        elif r["type"] == "call" and K > price:
            strike_iv = iv_atm * (1 - abs(dist_pct) * 0.005)
        else:
            strike_iv = iv_atm * (1 + abs(dist_pct) * 0.01)
        gamma = _bs_gamma(price, K, T_days, strike_iv)
        oi = int(r["oi"] or 0)
        gex = gamma * oi * 100 * price * price * 0.01
        total += gex if r["type"] == "call" else -gex
    return total


def _compute_gex(rows, spot, T_days, iv_atm):
    spot = _finite_number(spot)
    iv_atm = _finite_number(iv_atm, 15.0) or 15.0
    T_days = max(1, int(_finite_number(T_days, 1) or 1))
    if spot is None or spot <= 0:
        return {"gex_by_strike": {}, "gross_gex": 0.0, "total_gex": 0.0, "gex_ratio": 0.0, "gamma_flip": None, "pin_strike": None, "max_pain": None, "support": None, "resistance": None, "interval": 1.0, "top_put_walls": [], "top_call_walls": []}
    """
    Compute per-strike GEX and aggregate.
    GEX = gamma × OI × 100 × spot² × 0.01  (in $ per 1% move)
    Dealers: short calls (+GEX), long puts (-GEX)
    Positive aggregate GEX = mean reversion; Negative = trend amplification.
    """
    gex_by_strike = {}
    gross_gex = 0.0

    for r in rows:
        K = float(r["strike"])
        # Use ATM IV as proxy (in a real system you'd use per-strike IV from the chain)
        # Adjust slightly: OTM puts have higher IV (skew), OTM calls lower
        dist_pct = (K - spot) / spot * 100 if spot else 0.0
        if r["type"] == "put" and K < spot:
            strike_iv = iv_atm * (1 + abs(dist_pct) * 0.025)
        elif r["type"] == "call" and K > spot:
            strike_iv = iv_atm * (1 - abs(dist_pct) * 0.005)
        else:
            strike_iv = iv_atm * (1 + abs(dist_pct) * 0.01)

        gamma = _bs_gamma(spot, K, T_days, strike_iv)
        oi = int(r["oi"] or 0)
        gex = gamma * oi * 100 * spot * spot * 0.01  # $ per 1% move

        if K not in gex_by_strike:
            gex_by_strike[K] = 0.0

        signed_gex = gex if r["type"] == "call" else -gex
        gex_by_strike[K] += signed_gex
        gross_gex += abs(signed_gex)

    total_gex = sum(gex_by_strike.values())
    gex_ratio = (total_gex / gross_gex) if gross_gex else 0.0

    # Gamma flip: underlying price where total signed GEX changes sign.
    # We solve this on a price grid and linearly interpolate the first zero crossing.
    flip_price = None
    grid = [spot * (0.90 + i * 0.005) for i in range(41)]  # 90% to 110% spot in 0.5% steps
    prev_p = None
    prev_g = None
    for p in grid:
        g = _gex_at_price(rows, p, T_days, iv_atm)
        if prev_g is not None and ((prev_g <= 0 <= g) or (prev_g >= 0 >= g)):
            if g == prev_g:
                flip_price = round(p, 2)
            else:
                frac = abs(prev_g) / (abs(prev_g) + abs(g))
                flip_price = round(prev_p + (p - prev_p) * frac, 2)
            break
        prev_p, prev_g = p, g

    # Pin strike: highest absolute GEX (where market makers have most exposure)
    pin_strike = max(gex_by_strike, key=lambda k: abs(gex_by_strike[k])) if gex_by_strike else round(spot)

    # Max pain (expiration strike minimizing aggregate payout)
    strikes = sorted(gex_by_strike.keys())
    max_pain = None
    if strikes:
        payout_by_strike = {}
        call_rows = [r for r in rows if r["type"] == "call"]
        put_rows = [r for r in rows if r["type"] == "put"]
        for settle in strikes:
            payout = 0.0
            for r in call_rows:
                k = float(r["strike"])
                payout += int(r["oi"] or 0) * max(0.0, settle - k)
            for r in put_rows:
                k = float(r["strike"])
                payout += int(r["oi"] or 0) * max(0.0, k - settle)
            payout_by_strike[settle] = payout
        max_pain = min(payout_by_strike, key=lambda k: payout_by_strike[k])

    return {
        "total_gex": round(total_gex, 2),
        "gross_gex": round(gross_gex, 2),
        "gex_ratio": round(gex_ratio, 4),
        "gex_per_strike": {str(k): round(v, 2) for k, v in sorted(gex_by_strike.items())},
        "gamma_flip": flip_price,
        "pin_strike": pin_strike,
        "max_pain": round(max_pain, 2) if max_pain is not None else None,
        "regime": "NEGATIVE_GAMMA" if total_gex < 0 else "POSITIVE_GAMMA",
        "regime_strength": round(abs(gex_ratio) * 100, 1),
    }


def _compute_skew_rr(rows, spot, T_days, iv_atm):
    spot = _finite_number(spot)
    iv_atm = _finite_number(iv_atm, 15.0) or 15.0
    T_days = max(1, int(_finite_number(T_days, 1) or 1))
    if spot is None or spot <= 0:
        return 0.0
    """
    Compute 25-delta Risk Reversal (put skew - call skew).
    Approximate 25D strikes from BS delta.
    Negative RR = put bias (bearish institutional hedging).
    """
    if T_days <= 0: return 0.0
    T = max(T_days / 252.0, 1/252.0)
    iv = iv_atm / 100.0
    # 25-delta strikes: K = S × exp(±N^-1(0.25) × iv × sqrt(T) - 0.5 × iv² × T)
    d25 = 0.6745  # N^-1(0.75) ≈ 0.6745 → 25D put at -0.6745
    K_put_25  = spot * _math.exp(-d25 * iv * _math.sqrt(T) - 0.5 * iv * iv * T)
    K_call_25 = spot * _math.exp( d25 * iv * _math.sqrt(T) - 0.5 * iv * iv * T)
    
    # Find OI-weighted IV near those strikes
    def oi_near(target, side, rows):
        best_dist = 999; total_oi = 0
        for r in rows:
            if r["type"] != side: continue
            dist = abs(float(r["strike"]) - target)
            if dist < best_dist:
                best_dist = dist
                total_oi = int(r["oi"] or 0)
        return total_oi
    
    put_oi_25  = oi_near(K_put_25, "put", rows)
    call_oi_25 = oi_near(K_call_25, "call", rows)
    total = put_oi_25 + call_oi_25
    
    # Skew implied from OI imbalance at 25D strikes
    if total > 0:
        put_frac = put_oi_25 / total
        skew_rr = round((put_frac - 0.5) * -8.0, 2)  # scale to ~±4%
    else:
        skew_rr = 0.0
    return skew_rr


def _five_factor_score(gex_total, pcr, skew_rr, spot, pin_strike, gamma_flip, rows, gex_ratio=None, max_pain=None, dte=None, vix_level=None, gex_strength_pct=None):
    spot = _finite_number(spot)
    pin_strike = _finite_number(pin_strike)
    gamma_flip = _finite_number(gamma_flip)
    max_pain = _finite_number(max_pain)
    vix_level = _finite_number(vix_level)
    gex_strength_pct = _finite_number(gex_strength_pct)
    try:
        dte = int(dte) if dte is not None else None
    except Exception:
        dte = None
    """
    Composite GEX score.  The original grid used 5 core factors; VIX regime and
    DTE/expiry effect are now promoted to scored factors because they change how
    sticky walls should be intraday.  Each factor remains in the -2..+2 range.
    Returns: score, confidence %, per-factor breakdown, regime label.
    """
    factors = []
    score = 0

    # 1. Skew RR 25D
    if skew_rr < -2.0:   f1, s1 = "Bearish",   -2
    elif skew_rr < -0.5: f1, s1 = "Mild Bearish", -1
    elif skew_rr > 2.0:  f1, s1 = "Bullish",    +2
    elif skew_rr > 0.5:  f1, s1 = "Mild Bullish", +1
    else:                f1, s1 = "Neutral",      0
    factors.append({"name":"Skew RR 25D", "value":f"{skew_rr}%", "signal":f1, "score":s1,
                    "note":"Reverse smirk. Put bias dominant." if s1 < 0 else "Call skew elevated." if s1 > 0 else "Balanced wings."})
    score += s1

    # 2. PCR
    if pcr > 1.5:   f2, s2 = "Bearish",     -2
    elif pcr > 1.1: f2, s2 = "Mild Bearish", -1
    elif pcr < 0.6: f2, s2 = "Bullish",      +2
    elif pcr < 0.85: f2, s2 = "Mild Bullish", +1
    else:            f2, s2 = "Neutral",       0
    factors.append({"name":"Put-Call Ratio", "value":str(round(pcr,2)), "signal":f2, "score":s2,
                    "note":"Put heavy. Cautious sentiment." if s2 < 0 else "Call heavy. Complacent." if s2 > 0 else "Balanced PCR."})
    score += s2

    # 3. GEX Regime / Strength %.
    # >20% of gross GEX = very strong pin/fade day; 10-20% = moderate;
    # <10% = weak pin, breakouts become more valid.  Sign still matters:
    # positive GEX cushions/ranges, negative GEX amplifies/trends.
    regime_metric = gex_ratio if gex_ratio is not None else (gex_total / max(1.0, abs(gex_total)))
    regime_metric = _finite_number(regime_metric, 0.0) or 0.0
    strength = abs(regime_metric) * 100.0
    if gex_strength_pct is not None:
        strength = abs(gex_strength_pct)
    if strength >= 20:
        f3 = "Very Strong Pos-G Pin" if regime_metric >= 0 else "Very Strong Neg-G Trend"
        s3 = +2 if regime_metric >= 0 else -2
        strength_note = "Very strong pin: fade-first unless price breaks and holds outside the wall."
    elif strength >= 10:
        f3 = "Moderate Pos-G Pin" if regime_metric >= 0 else "Moderate Neg-G Trend"
        s3 = +1 if regime_metric >= 0 else -1
        strength_note = "Moderate pin: current reading; fades valid, but require tight risk."
    else:
        f3, s3 = "Weak GEX", 0
        strength_note = "Weak pin: breakouts are valid and wall fades need confirmation."
    if s3 > 0:
        regime_note = "Dealers cushion moves and support range-fade behavior while levels hold."
    elif s3 < 0:
        regime_note = "Dealers amplify moves; confirmed breaks can accelerate."
    else:
        regime_note = "Dealer cushion is not strong enough to trust blindly."
    factors.append({"name":"GEX Regime", "value":f"{strength:.1f}%", "signal":f3, "score":s3,
                    "note":f"Net GEX ${gex_total:,.0f}; {regime_metric:+.2f} of gross. {regime_note} {strength_note}"})
    score += s3

    # 4. Gamma Flip / Spot distance
    if gamma_flip:
        dist_pct = round(abs(spot - gamma_flip) / spot * 100, 2) if spot else 0.0
        if spot < gamma_flip:
            if dist_pct > 1.0: f4, s4 = "Below Flip", -2
            else:              f4, s4 = "Near Flip",  -1
            note = f"Spot {dist_pct}% below gamma flip ${gamma_flip}. Negative gamma can intensify downside pressure."
        elif spot > gamma_flip:
            if dist_pct > 1.0: f4, s4 = "Above Flip", +2
            else:              f4, s4 = "Near Flip",  +1
            note = f"Spot {dist_pct}% above gamma flip ${gamma_flip}. Dealer flows are more supportive."
        else:
            f4, s4 = "At Flip", 0
            note = "Spot is at gamma flip. Transition zone."
        value = f"${gamma_flip}" + (f" ({dist_pct}% away)" if dist_pct else "")
    else:
        f4, s4 = "No Flip", 0
        note = "No stable flip found in the scan range."
        value = f"${pin_strike}"
    factors.append({"name":"Gamma Flip", "value":value, "signal":f4, "score":s4, "note":note})
    score += s4

    # 5. Wing Premium / max pain context (put/call OTM OI balance)
    if rows:
        otm_calls = sum(int(r["oi"] or 0) for r in rows if r["type"]=="call" and float(r["strike"]) > spot*1.02)
        otm_puts  = sum(int(r["oi"] or 0) for r in rows if r["type"]=="put"  and float(r["strike"]) < spot*0.98)
        total_wing = otm_calls + otm_puts
        if total_wing > 0:
            put_wing_frac = otm_puts / total_wing
            if put_wing_frac > 0.65:   f5, s5 = "Put Heavy",   -1
            elif put_wing_frac < 0.35: f5, s5 = "Call Heavy",  +1
            else:                      f5, s5 = "Symmetric",    0
            wing_pct = round(abs(put_wing_frac - 0.5) * 200, 1)
        else:
            f5, s5, wing_pct = "Symmetric", 0, 0.0
        factors.append({"name":"Wing Premium", "value":f"{wing_pct}%",
                        "signal":f5, "score":s5,
                        "note":f"{'OTM put demand elevated.' if s5<0 else 'OTM call demand elevated.' if s5>0 else 'Balanced wing demand.'}"})
    else:
        s5 = 0; factors.append({"name":"Wing Premium","value":"N/A","signal":"Neutral","score":0,"note":"No wing data."})
    score += s5

    # 6. VIX regime (formalized instead of footer-only)
    if vix_level is None:
        f6, s6, vix_val = "Unavailable", 0, "N/A"
        vix_note = "VIX unavailable; not scored."
    else:
        vix_val = f"{vix_level:.1f}"
        if vix_level < 15:
            f6, s6 = "Calm", +1
            vix_note = "VIX <15: walls/pin behavior is more reliable."
        elif vix_level <= 20:
            f6, s6 = "Normal / Cautious", 0
            vix_note = "VIX 15-20: elevated enough for chop, not panic; size normally but respect whipsaw."
        elif vix_level <= 30:
            f6, s6 = "Elevated", -1
            vix_note = "VIX >20: gamma levels reprice faster; use wider stops and smaller size."
        else:
            f6, s6 = "Panic", -2
            vix_note = "VIX >30: dealer walls are less reliable; avoid blind fades."
    factors.append({"name":"VIX Regime", "value":vix_val, "signal":f6, "score":s6, "note":vix_note})
    score += s6

    # 7. DTE / expiry effect
    gex_positive = (gex_ratio is not None and gex_ratio >= 0) or (gex_ratio is None and gex_total >= 0)
    strength = gex_strength_pct if gex_strength_pct is not None else abs(gex_ratio or 0) * 100.0
    if dte is None:
        f7, s7, dte_val = "Unavailable", 0, "N/A"
        dte_note = "DTE unavailable; not scored."
    elif dte <= 0:
        dte_val = "0DTE"
        if gex_positive and strength >= 10:
            f7, s7 = "Stronger Pin", +2
            dte_note = "Expiry today: positive/moderate GEX makes walls stickier, but whipsaw risk is high."
        elif gex_positive:
            f7, s7 = "Pin With Weak GEX", +1
            dte_note = "Expiry today: pin can work, but GEX strength is weak; do not fade without confirmation."
        else:
            f7, s7 = "Breakout Risk", -2
            dte_note = "Expiry today with negative GEX: breaks can accelerate fast."
    elif dte <= 2:
        dte_val = f"{dte}DTE"
        f7, s7 = "Less Sticky", -1
        dte_note = "1-2DTE: walls are less sticky than 0DTE; confirmed breakouts are more valid."
    elif dte <= 5:
        dte_val = f"{dte}DTE"
        f7, s7 = "Weekly Structure", +1
        dte_note = "3-5DTE: enough weekly OI density for range/wing planning."
    else:
        dte_val = f"{dte}DTE"
        f7, s7 = "Longer DTE", 0
        dte_note = "Longer DTE: use GEX as context, not as an intraday pin."
    factors.append({"name":"DTE Effect", "value":dte_val, "signal":f7, "score":s7, "note":dte_note})
    score += s7

    # Regime label
    if score <= -5: regime = "TREND / SELL-DOWN REGIME"
    elif score <= -2: regime = "BEARISH REGIME"
    elif score >= 5: regime = "RANGE / PIN-UP REGIME"
    elif score >= 2: regime = "BULLISH REGIME"
    else: regime = "NEUTRAL REGIME"

    confidence = min(95, max(35, 46 + abs(score) * 5.5))
    return score, round(confidence), regime, factors


def _build_trade_plan(spot, gex_info, walls, ta, score, sigma_1d):
    spot = _finite_number(spot)
    sigma_1d = _finite_number(sigma_1d, 0.0) or 0.0
    gex_info = dict(gex_info or {})
    walls = dict(walls or {})
    if spot is None or spot <= 0:
        return {"primary": {}, "counter": {}, "note": "Unable to build plan because spot is unavailable."}
    """
    Build primary (bearish/bullish) and counter-trend scenarios.
    Mirrors TradeHive structure.
    """
    gamma_flip = _finite_number(gex_info.get("gamma_flip"))
    if gamma_flip is None:
        gamma_flip = round(spot * 1.005, 2)
    pin        = _finite_number(gex_info.get("pin_strike")) or round(spot, 2)
    max_pain   = _finite_number(gex_info.get("max_pain")) or pin
    put_wall   = _finite_number(walls.get("support")) or round(spot * 0.985, 2)
    call_wall  = _finite_number(walls.get("resistance")) or round(spot * 1.015, 2)
    breakdown  = round(spot - sigma_1d * 0.5, 2)
    breakout   = round(spot + sigma_1d * 0.4, 2)
    balance    = round(pin, 2)  # GEX pin = balance/magnet

    if score < 0:  # Bearish primary
        primary = {
            "direction": "BEARISH",
            "trigger": f"Spot opens at or fails to reclaim ${balance} within first 30 min. Negative GEX amplifies selling.",
            "entry_zone": f"${balance} – ${round(balance + sigma_1d*0.3, 2)}",
            "entry_low": balance, "entry_high": round(balance + sigma_1d*0.3, 2),
            "stop": gamma_flip,
            "stop_note": f"${gamma_flip} Gamma Flip — regime flips bullish above",
            "t1": breakdown, "t1_note": f"${breakdown} Breakdown — partial exit 50%",
            "t2": put_wall,  "t2_note": f"${put_wall} Put Wall — support magnet, exit 40%",
            "t3": round(put_wall - sigma_1d * 0.5, 2), "t3_note": "Extended target if Put Wall breaks",
            "stop_dist": round(gamma_flip - balance, 2),
            "reward_t2": round(balance - put_wall, 2),
        }
        primary["rr"] = round(primary["reward_t2"] / max(0.01, primary["stop_dist"]), 1) if primary["stop_dist"] > 0 else 0
        counter = {
            "direction": "BULLISH",
            "trigger": f"Spot must CLOSE above ${breakout} Breakout AND hold above ${gamma_flip} Gamma Flip.",
            "entry": breakout, "entry_note": f"${breakout} reclaim + hold on volume",
            "stop": breakdown, "stop_note": f"${breakdown} Breakdown — structural failure",
            "t1": gamma_flip, "t1_note": f"${gamma_flip}–${round(gamma_flip+sigma_1d*0.3, 2)} Gamma Flip zone",
            "t2": call_wall, "t2_note": f"${call_wall} Call Wall — full exit",
        }
    else:  # Bullish primary
        primary = {
            "direction": "BULLISH",
            "trigger": f"Spot holds above ${balance} Balance. Positive GEX provides mean-reversion cushion.",
            "entry_zone": f"${round(balance - sigma_1d*0.3, 2)} – ${balance}",
            "entry_low": round(balance - sigma_1d*0.3, 2), "entry_high": balance,
            "stop": breakdown,
            "stop_note": f"${breakdown} Breakdown — below mean reversion zone",
            "t1": breakout, "t1_note": f"${breakout} Breakout — partial exit 50%",
            "t2": call_wall, "t2_note": f"${call_wall} Call Wall — resistance magnet, exit 40%",
            "t3": round(call_wall + sigma_1d * 0.5, 2), "t3_note": "Extended if Call Wall breaks",
            "stop_dist": round(balance - breakdown, 2),
            "reward_t2": round(call_wall - balance, 2),
        }
        primary["rr"] = round(primary["reward_t2"] / max(0.01, primary["stop_dist"]), 1) if primary["stop_dist"] > 0 else 0
        counter = {
            "direction": "BEARISH",
            "trigger": f"Spot fails to hold ${balance} Balance and closes below ${breakdown}.",
            "entry": breakdown, "entry_note": f"${breakdown} break + confirm on volume",
            "stop": gamma_flip, "stop_note": f"${gamma_flip} Gamma Flip — above = abort",
            "t1": put_wall, "t1_note": f"${put_wall} Put Wall — target",
            "t2": round(put_wall - sigma_1d*0.5, 2), "t2_note": "Extended if Put Wall breaks",
        }

    sig = walls.get("significance") or {}
    # top_put_walls/top_call_walls here are significant_put_walls/
    # significant_call_walls -- the RAW score-sorted lists, deliberately
    # left untouched by the proximity fix in _walls() so other consumers
    # (bubble charts etc.) still see every wall regardless of distance.
    # But put_wall/call_wall above now come from the PROXIMITY-AWARE
    # selection, so [0] here can be a completely different, far-off
    # strike (e.g. an oversized stale position 30% from spot) whose
    # label/score/OI-change text would then get attached to a price it
    # doesn't describe. Find the entry that actually matches the
    # selected strike; fall back to [0] only if nothing matches (e.g.
    # put_wall came from the spot*0.985 fallback, not a real wall).
    def _match_wall_sig(entries, target_price):
        if not entries:
            return {}
        if target_price is not None:
            for w in entries:
                if abs((w.get("strike") or 0) - target_price) < 0.01:
                    return w
        return entries[0]

    top_put_sig = _match_wall_sig(sig.get("top_put_walls"), put_wall)
    top_call_sig = _match_wall_sig(sig.get("top_call_walls"), call_wall)
    call_note = top_call_sig.get("label") or "Max Call OI · resistance"
    put_note = top_put_sig.get("label") or "Max Put OI · support"
    if top_call_sig.get("score") is not None:
        call_note += f" · score {top_call_sig.get('score')} · OI Δ {top_call_sig.get('oi_change', 0):+,}"
    if top_put_sig.get("score") is not None:
        put_note += f" · score {top_put_sig.get('score')} · OI Δ {top_put_sig.get('oi_change', 0):+,}"

    levels = [
        {"label": "CALL WALL",    "price": call_wall, "note": call_note},
        {"label": "GAMMA FLIP",   "price": gamma_flip,"note": "Dealer regime flip line"},
        {"label": "BREAKOUT",     "price": breakout,  "note": "Resistance zone"},
        {"label": "BALANCE / PIN","price": balance,   "note": "GEX magnet / pin"},
        {"label": "MAX PAIN",     "price": round(max_pain,2) if max_pain else balance, "note": "Lowest expiry payout"},
        {"label": "SPOT",         "price": spot,      "note": "Current"},
        {"label": "BREAKDOWN",    "price": breakdown, "note": "Key support"},
        {"label": "PUT WALL",     "price": put_wall,  "note": put_note},
    ]
    levels.sort(key=lambda x: -x["price"])

    return {
        "primary": primary,
        "counter": counter,
        "levels": levels,
        "key": {
            "gamma_flip": gamma_flip, "pin": pin, "max_pain": round(max_pain, 2) if max_pain else None, "put_wall": put_wall,
            "call_wall": call_wall, "breakdown": breakdown, "breakout": breakout,
            "balance": balance,
        }
    }



def _fmt_money(value):
    value = _finite_number(value)
    if value is None:
        return "n/a"
    return f"${value:.2f}"


def _build_gex_action_guide(symbol, spot, expiry, dte, gex_info, walls, pcr, score, confidence, sigma_1d, vix_ctx=None, factors=None):
    """Convert GEX/PCR/VIX/DTE levels into concrete intraday actions.

    This is intentionally separate from option trade suggestions.  It answers:
    where do I fade, where do I stop fading and trade a break, and what conflict
    should I watch today?
    """
    spot = _finite_number(spot)
    if spot is None or spot <= 0:
        return {"available": False, "summary": "Spot unavailable; cannot build GEX action guide."}
    gex_info = dict(gex_info or {})
    walls = dict(walls or {})
    vix_ctx = dict(vix_ctx or {})
    sigma_1d = _finite_number(sigma_1d, 0.0) or 0.0
    gex_total = _finite_number(gex_info.get("total_gex"), 0.0) or 0.0
    gex_ratio = _finite_number(gex_info.get("gex_ratio"), 0.0) or 0.0
    gex_strength = _finite_number(gex_info.get("regime_strength"), abs(gex_ratio) * 100.0) or 0.0
    gamma_flip = _finite_number(gex_info.get("gamma_flip"))
    pin = _finite_number(gex_info.get("pin_strike")) or spot
    max_pain = _finite_number(gex_info.get("max_pain"))
    put_wall = _finite_number(walls.get("support"))
    call_wall = _finite_number(walls.get("resistance"))
    if put_wall is None or put_wall >= spot:
        put_wall = gamma_flip if gamma_flip and gamma_flip < spot else round(spot - max(sigma_1d, spot * 0.004), 2)
    if call_wall is None or call_wall <= spot:
        call_wall = round(spot + max(sigma_1d, spot * 0.004), 2)

    # Use gamma flip as the lower range anchor when it sits between put wall and spot;
    # otherwise use the actionable put wall.  This matches the way 0DTE SPY plans
    # often trade the flip as the first support/break line.
    lower = put_wall
    if gamma_flip and put_wall <= gamma_flip <= spot:
        lower = gamma_flip
    upper = call_wall
    if upper <= lower:
        upper = round(max(spot, lower) + max(sigma_1d, spot * 0.004), 2)
    mid = round((lower + upper) / 2.0, 2)
    target_up = round(max(pin, mid, spot), 2)
    target_down = round(min(pin, mid, spot), 2)
    stop_pad = max(sigma_1d * 0.20, spot * 0.0015, 0.25)
    breakout_level = round(upper + max(sigma_1d * 0.25, spot * 0.002, 0.5), 2)
    breakdown_t1 = round(lower - max(sigma_1d * 0.35, spot * 0.002, 0.5), 2)
    breakdown_t2 = round(lower - max(sigma_1d * 0.75, spot * 0.004, 1.0), 2)

    positive_gex = gex_total >= 0
    strong_pin = positive_gex and gex_strength >= 20
    moderate_pin = positive_gex and 10 <= gex_strength < 20
    weak_pin = gex_strength < 10
    expiry_today = dte is not None and int(dte) <= 0
    near_expiry = dte is not None and int(dte) <= 2

    if strong_pin:
        pin_label = "Very strong positive-GEX pin"
        pin_note = "Fade-first unless price breaks and holds outside the range."
    elif moderate_pin:
        pin_label = "Moderate positive-GEX pin"
        pin_note = "Range fades are valid, but wait for reaction at the wall."
    elif positive_gex:
        pin_label = "Weak positive-GEX pin"
        pin_note = "Walls can help, but breakouts are valid with confirmation."
    else:
        pin_label = "Negative-GEX trend risk"
        pin_note = "Dealer hedging can amplify breaks; avoid blind fades."

    # PCR interpretation from a tactical GEX standpoint.
    if pcr is None:
        pcr_label = "PCR unavailable"
        pcr_bias = "unknown"
    elif pcr >= 1.5:
        pcr_label = f"PCR {pcr:.2f} is bearish / put-heavy"
        pcr_bias = "bearish"
    elif pcr >= 1.1:
        pcr_label = f"PCR {pcr:.2f} is mildly bearish"
        pcr_bias = "mild_bearish"
    elif pcr <= 0.60:
        pcr_label = f"PCR {pcr:.2f} is bullish / call-heavy"
        pcr_bias = "bullish"
    elif pcr <= 0.85:
        pcr_label = f"PCR {pcr:.2f} is mildly bullish"
        pcr_bias = "mild_bullish"
    else:
        pcr_label = f"PCR {pcr:.2f} is balanced"
        pcr_bias = "neutral"

    if positive_gex and pcr_bias in ("bearish", "mild_bearish"):
        tension = {
            "title": "Positive GEX vs bearish PCR",
            "gex_says": "Dealers should cushion moves and favor a range/pin day.",
            "pcr_says": "Put-heavy positioning says downside hedges are active.",
            "resolution": f"As long as spot stays above {_fmt_money(lower)}, GEX gets priority. If {_fmt_money(lower)} breaks and holds, PCR was warning correctly and downside can speed up.",
        }
    elif (not positive_gex) and pcr_bias in ("bearish", "mild_bearish"):
        tension = {
            "title": "Negative GEX + bearish PCR confluence",
            "gex_says": "Dealer hedging can amplify downside once support breaks.",
            "pcr_says": "Put-heavy positioning supports downside risk.",
            "resolution": f"Do not buy the dip below {_fmt_money(lower)} without reclaim. Breakdown setups get priority.",
        }
    elif positive_gex:
        tension = {
            "title": "Positive GEX range structure",
            "gex_says": "Dealers should cushion moves back toward the pin/range.",
            "pcr_says": pcr_label,
            "resolution": f"Range trade remains favored while spot holds between {_fmt_money(lower)} and {_fmt_money(upper)}.",
        }
    else:
        tension = {
            "title": "Negative GEX breakout structure",
            "gex_says": "Breaks can accelerate instead of mean-reverting.",
            "pcr_says": pcr_label,
            "resolution": f"Wait for a confirmed break/hold outside {_fmt_money(lower)}–{_fmt_money(upper)} before committing size.",
        }

    vix_level = _finite_number(vix_ctx.get("level") or vix_ctx.get("vix"))
    if vix_level is None:
        vix_note = "VIX unavailable; keep size conservative and rely on confirmation."
    elif vix_level < 15:
        vix_note = f"VIX {vix_level:.1f}: calm. Pin/range behavior should be more reliable if GEX is positive."
    elif vix_level <= 20:
        vix_note = f"VIX {vix_level:.1f}: elevated but not panic. Expect chop; range fades are favored over blind directional chase when GEX is positive."
    elif vix_level <= 30:
        vix_note = f"VIX {vix_level:.1f}: elevated. Use wider stops, smaller size, and require candle confirmation for fades."
    else:
        vix_note = f"VIX {vix_level:.1f}: panic/high-vol regime. Avoid blind wall fades; favor confirmed breaks or skip."

    if expiry_today and positive_gex:
        day_note = "0DTE expiry today: walls can be sticky, but whipsaw risk around the gamma flip is high."
    elif expiry_today:
        day_note = "0DTE expiry today with negative GEX: breaks can be fast; trade smaller and respect stops."
    elif near_expiry:
        day_note = f"{dte}DTE: walls are less sticky than expiry day; confirmed breakouts deserve more respect."
    else:
        day_note = f"{dte}DTE: use this as structure, not a pure 0DTE pin map."

    if positive_gex and gex_strength >= 10:
        primary = f"Fade {_fmt_money(upper)} resistance and buy {_fmt_money(lower)} support/gamma-flip reactions." 
        risk = f"Break and hold below {_fmt_money(lower)} changes regime; get defensive or short." 
        upside = f"Upside breakout is valid only on a 1H close above {_fmt_money(upper)} with volume; target {_fmt_money(breakout_level)} first." 
    elif positive_gex:
        primary = f"Use {_fmt_money(lower)}–{_fmt_money(upper)} as a reference range, but require a reaction candle before fading." 
        risk = f"Weak GEX means breaks through {_fmt_money(lower)} or {_fmt_money(upper)} are tradable." 
        upside = f"Above {_fmt_money(upper)} with volume, trade toward {_fmt_money(breakout_level)}." 
    else:
        primary = f"Do not assume a pin. Trade confirmed breaks outside {_fmt_money(lower)}–{_fmt_money(upper)}." 
        risk = f"Below {_fmt_money(lower)}, negative gamma can amplify downside toward {_fmt_money(breakdown_t1)} then {_fmt_money(breakdown_t2)}." 
        upside = f"Above {_fmt_money(upper)}, upside can squeeze toward {_fmt_money(breakout_level)}." 

    size_note = "Keep size small" if expiry_today or (vix_level is not None and vix_level > 20) else "Normal-small size"
    if weak_pin:
        size_note += "; pin strength is weak"
    if gamma_flip and abs(spot - gamma_flip) / max(1.0, spot) <= 0.004:
        size_note += "; spot is near gamma flip, so whipsaw risk is elevated"

    return {
        "available": True,
        "symbol": symbol,
        "expiry": expiry,
        "dte": dte,
        "title": "How To Trade This Today",
        "range": {"low": round(lower, 2), "high": round(upper, 2), "label": f"{_fmt_money(lower)} → {_fmt_money(upper)}"},
        "context": f"{pin_label}. Price is trading around {_fmt_money(spot)} with key range {_fmt_money(lower)}–{_fmt_money(upper)}. {pin_note}",
        "setups": [
            {
                "name": "Setup A — Range Fade",
                "probability": "Highest probability" if positive_gex and gex_strength >= 10 else "Requires confirmation",
                "rules": [
                    f"IF price rallies to {_fmt_money(upper)} call-wall/resistance: fade short, target {_fmt_money(target_down)}–{_fmt_money(pin)}; stop above {_fmt_money(round(upper + stop_pad, 2))}.",
                    f"IF price drops to {_fmt_money(lower)} support/gamma-flip: fade long, target {_fmt_money(pin)}–{_fmt_money(upper)}; stop below {_fmt_money(round(lower - stop_pad, 2))}.",
                ],
            },
            {
                "name": "Setup B — Breakout Long",
                "probability": "Lower probability / higher reward" if positive_gex else "Valid with confirmation",
                "rules": [
                    f"IF price closes a 1H candle above {_fmt_money(upper)} with volume: long toward {_fmt_money(breakout_level)}.",
                    f"Stop back below {_fmt_money(upper)}; do not chase if the break fails back into the range.",
                ],
            },
            {
                "name": "Setup C — Breakdown Short",
                "probability": "Needs confirmation" if positive_gex else "Higher priority below support",
                "rules": [
                    f"IF price breaks and holds below {_fmt_money(lower)}: short toward {_fmt_money(breakdown_t1)} then {_fmt_money(breakdown_t2)}.",
                    f"Stop back above {_fmt_money(lower)}. Below the flip/support zone, dealer flows can move fast.",
                ],
            },
        ],
        "tension": tension,
        "vix_note": vix_note,
        "dte_note": day_note,
        "bottom_line": [primary, risk, upside, f"Position sizing: {size_note}."]
    }





def _build_gex_today_action_plan(spot, gex_info, walls, score, pcr, vix, dte, sigma_1d, wall_strength=None):
    """Convert the GEX grid into concrete intraday action rules."""
    spot = _finite_number(spot)
    if spot is None or spot <= 0:
        return {"available": False, "summary": "Spot unavailable; action plan cannot be built."}
    gex_info = dict(gex_info or {})
    walls = dict(walls or {})
    wall_strength = dict(wall_strength or {})
    sigma = _finite_number(sigma_1d, max(spot * 0.006, 0.5)) or max(spot * 0.006, 0.5)
    gamma_flip = _finite_number(gex_info.get("gamma_flip"))
    pin = _finite_number(gex_info.get("pin_strike"))
    max_pain = _finite_number(gex_info.get("max_pain"))
    put_wall = _finite_number(walls.get("support"))
    call_wall = _finite_number(walls.get("resistance"))
    total_gex = _finite_number(gex_info.get("total_gex"), 0.0) or 0.0
    gex_ratio = _finite_number(gex_info.get("gex_ratio"), 0.0) or 0.0
    gex_strength = abs(gex_ratio) * 100.0
    vix_val = _finite_number(vix)
    try:
        dte_i = int(dte) if dte is not None else None
    except Exception:
        dte_i = None

    below = []
    above = []
    for label, value in (("Gamma Flip", gamma_flip), ("Put Wall", put_wall), ("Pin", pin), ("Max Pain", max_pain)):
        if value is not None and value < spot:
            below.append((spot - value, label, round(value, 2)))
    for label, value in (("Call Wall", call_wall), ("Gamma Flip", gamma_flip), ("Pin", pin), ("Max Pain", max_pain)):
        if value is not None and value > spot:
            above.append((value - spot, label, round(value, 2)))
    low_label, high_label = "Support", "Resistance"
    if below:
        _, low_label, range_low = min(below, key=lambda x: x[0])
    else:
        range_low = round((put_wall if put_wall is not None else spot - sigma), 2)
    if above:
        _, high_label, range_high = min(above, key=lambda x: x[0])
    else:
        range_high = round((call_wall if call_wall is not None else spot + sigma), 2)
    if range_low >= range_high:
        range_low = round(min(put_wall or spot - sigma, gamma_flip or spot - sigma, spot - sigma), 2)
        range_high = round(max(call_wall or spot + sigma, gamma_flip or spot + sigma, spot + sigma), 2)
        low_label = "Support"
        high_label = "Resistance"

    center = _finite_number(pin) or round((range_low + range_high) / 2.0, 2)
    upside_target = round(max(range_high + sigma * 0.35, range_high + max(0.25, sigma * 0.15)), 2)
    downside_target = round(min(range_low - sigma * 0.35, range_low - max(0.25, sigma * 0.15)), 2)
    upper_stop = round(range_high + max(0.25, sigma * 0.20), 2)
    lower_stop = round(range_low - max(0.25, sigma * 0.20), 2)

    pos_gex = total_gex >= 0
    pin_strong = pos_gex and (gex_strength >= 10.0)
    zero_dte = (dte_i is not None and dte_i <= 0)
    near_expiry = (dte_i is not None and dte_i <= 2)
    pcr_num = _finite_number(pcr, 1.0) or 1.0
    pcr_bearish = pcr_num >= 1.2
    pcr_bullish = pcr_num <= 0.8
    conflict = pos_gex and pcr_bearish

    if pin_strong and zero_dte:
        day_type = "Dealer-pinned 0DTE range day"
        primary = "Range Fade"
    elif pin_strong:
        day_type = "Positive-GEX range / fade-first day"
        primary = "Range Fade"
    elif total_gex < 0:
        day_type = "Negative-GEX breakout / trend-amplification day"
        primary = "Break and hold"
    else:
        day_type = "Mixed GEX day"
        primary = "Confirm before entry"

    if vix_val is None:
        vix_note = "VIX unavailable; keep size conservative."
    elif vix_val < 15:
        vix_note = f"VIX {vix_val:.1f}: calm; pin/fade levels are cleaner if GEX remains positive."
    elif vix_val <= 20:
        vix_note = f"VIX {vix_val:.1f}: elevated but not panic; expect chop and avoid oversized 0DTE trades."
    elif vix_val <= 30:
        vix_note = f"VIX {vix_val:.1f}: risk-off; require candle confirmation and reduce size."
    else:
        vix_note = f"VIX {vix_val:.1f}: panic regime; skip blind fades."

    if conflict:
        tension_title = "PCR hedging is fighting positive GEX."
        tension_resolution = (
            f"As long as spot holds above ${range_low:.2f} ({low_label}), GEX/range behavior has priority. "
            f"If ${range_low:.2f} breaks and holds, downside hedges/PCR take control and the move can speed up."
        )
    elif pos_gex:
        tension_title = "GEX and PCR are not in major conflict."
        tension_resolution = "Positive GEX favors two-sided fades while price remains inside the range. Breakouts need a candle close and volume."
    elif pcr_bearish:
        tension_title = "Negative GEX and bearish PCR align."
        tension_resolution = "Do not fade breakdowns. If the lower boundary breaks and holds, trend-follow short setups have priority."
    elif pcr_bullish:
        tension_title = "Negative GEX but PCR is not bearish."
        tension_resolution = "Breaks can still move fast in negative gamma; require hold/reclaim confirmation before acting."
    else:
        tension_title = "Mixed PCR/GEX read."
        tension_resolution = "Use boundary levels as triggers; no blind trade in the middle of the range."

    setups = [
        {
            "name": "Setup A - Range Fade",
            "priority": "Highest probability" if pin_strong else "Conditional",
            "rules": [
                f"IF price rallies to ${range_high:.2f} ({high_label}), fade short toward ${center:.2f}-${range_low:.2f}; stop above ${upper_stop:.2f}.",
                f"IF price drops to ${range_low:.2f} ({low_label}), fade long toward ${center:.2f}-${range_high:.2f}; stop below ${lower_stop:.2f}.",
            ],
            "when": "Best when GEX is positive/moderate and price fails at the boundary.",
        },
        {
            "name": "Setup B - Breakout Long",
            "priority": "Lower probability / higher reward" if pin_strong else "Valid on confirmation",
            "rules": [
                f"IF a 1H candle closes above ${range_high:.2f} with volume, long toward ${upside_target:.2f}; stop back below ${range_high:.2f}.",
                "Avoid chasing a wick through the wall; require close + hold."
            ],
            "when": "Use when call wall/pin fails and dealers need to hedge upside."
        },
        {
            "name": "Setup C - Breakdown Short",
            "priority": "Needs confirmation" if pos_gex else "Primary risk setup",
            "rules": [
                f"IF price breaks and holds below ${range_low:.2f}, short toward ${downside_target:.2f} then ${round(downside_target - sigma * 0.35, 2):.2f}; stop back above ${range_low:.2f}.",
                "Below the flip/support line, negative-gamma behavior can amplify downside moves."
            ],
            "when": "Higher conviction when PCR is bearish or your Trend Analyzer flips bearish."
        },
    ]

    bottom = []
    if pin_strong:
        bottom.append(f"Primary: fade ${range_high:.2f} resistance and buy ${range_low:.2f} support/flip while both levels hold.")
    else:
        bottom.append(f"Primary: no blind fade; wait for a close/hold around ${range_low:.2f} or ${range_high:.2f}.")
    bottom.append(f"Risk event: break below ${range_low:.2f} = short bias can accelerate.")
    bottom.append(f"Upside breakout: valid only after strong close/hold through ${range_high:.2f} toward ${upside_target:.2f}.")
    if zero_dte:
        bottom.append("Sizing: small. 0DTE + near gamma boundary = whipsaw risk.")
    elif near_expiry:
        bottom.append("Sizing: reduced/moderate. 1-2DTE walls are less sticky than 0DTE.")
    elif vix_val is not None and vix_val > 20:
        bottom.append("Sizing: reduced due to elevated VIX.")
    else:
        bottom.append("Sizing: normal only after confirmation; avoid entries in the middle of the range.")

    return {
        "available": True,
        "day_type": day_type,
        "primary_trade": primary,
        "range": {"low": round(range_low, 2), "high": round(range_high, 2), "low_label": low_label, "high_label": high_label, "center": round(center, 2)},
        "setups": setups,
        "tension": {"title": tension_title, "resolution": tension_resolution, "pcr": round(pcr_num, 2), "gex_positive": pos_gex},
        "vix": {"value": vix_val, "note": vix_note},
        "bottom_line": bottom,
        "meta": {"gex_strength_pct": round(gex_strength, 1), "dte": dte_i, "zero_dte": zero_dte, "pin_strong": pin_strong, "wall_score": wall_strength.get("score")},
    }

def compute_gex_reliability(sym: str, spot: float, gex_total: float,
                             iv_atm: float, dte: int) -> dict:
    """
    Compute GEX structural reliability for next-day trading.
    How stable will today's GEX levels (flip, walls, pin) be tomorrow?

    Returns dict with:
      - score: 0-100 reliability %
      - label: plain-English summary
      - factors: list of adjustments with reasons
      - trade_tomorrow: True/False
      - caveat: key warning if any
    """
    import datetime as _dt
    import sqlite3 as _sq
    from pathlib import Path as _P

    today     = _dt.date.today()
    tomorrow  = today + _dt.timedelta(days=1)
    dow       = today.weekday()   # 0=Mon, 4=Fri
    dow_names = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    base = 75
    factors = []
    caveats = []

    # ── 1. Day of week ───────────────────────────────────────────────────
    if dow == 0:   # Monday — OI reset from Friday expiry
        adj = -15
        factors.append({"factor": "Day of Week", "adj": adj,
                        "detail": "Monday: OI rolled over the weekend, walls may have shifted",
                        "icon": "📅"})
    elif dow == 4:  # Friday — expiry day effect
        adj = -12 if dte <= 1 else -5
        factors.append({"factor": "Day of Week", "adj": adj,
                        "detail": "Friday: expiry day, gamma collapse makes levels hypersensitive",
                        "icon": "📅"})
    elif dow in (1, 2, 3):  # Tue-Thu — cleanest days
        adj = +5
        factors.append({"factor": "Day of Week", "adj": adj,
                        "detail": f"{dow_names[dow]}: best days for GEX reliability (Tue-Thu)",
                        "icon": "📅"})
    else:
        adj = 0
        factors.append({"factor": "Day of Week", "adj": adj, "detail": "Weekend", "icon": "📅"})
    base += adj

    # ── 2. VIX regime ────────────────────────────────────────────────────
    vix_val = None
    try:
        from ..services.market import get_history_cached
        _vx = get_history_cached("^VIX", period="5d", interval="1d")
        if _vx is not None and len(_vx) >= 2:
            vix_val      = round(float(_vx["Close"].iloc[-1]), 1)
            vix_prev     = round(float(_vx["Close"].iloc[-2]), 1)
            vix_chg      = round(vix_val - vix_prev, 1)
    except Exception:
        pass

    if vix_val is not None:
        if vix_val < 13:
            adj = +5; label = f"VIX {vix_val} — very low, complacent market"
        elif vix_val <= 18:
            adj = +5; label = f"VIX {vix_val} — normal range, OI structure stable"
        elif vix_val <= 22:
            adj = 0;  label = f"VIX {vix_val} — slightly elevated"
        elif vix_val <= 28:
            adj = -10; label = f"VIX {vix_val} — elevated, gamma profiles repricing"
        else:
            adj = -20; label = f"VIX {vix_val} — fear regime, GEX less predictive"
            caveats.append(f"VIX {vix_val} is in fear territory — negative gamma dominates")

        # VIX change spike
        if abs(vix_chg) > 3:
            spike_adj = -15
            factors.append({"factor": "VIX Spike", "adj": spike_adj,
                            "detail": f"VIX moved {vix_chg:+.1f} today — IV repriced across chain",
                            "icon": "⚡"})
            base += spike_adj
            caveats.append(f"VIX spiked {vix_chg:+.1f} — gamma profiles shifted significantly")

        factors.append({"factor": "VIX Level", "adj": adj, "detail": label, "icon": "🌡"})
        base += adj
    else:
        factors.append({"factor": "VIX Level", "adj": 0,
                        "detail": "VIX data unavailable", "icon": "🌡"})

    # ── 3. Gamma regime ──────────────────────────────────────────────────
    if gex_total > 0:
        adj = +5
        factors.append({"factor": "Gamma Regime", "adj": adj,
                        "detail": "Positive gamma: dealers pin price, levels are more stable",
                        "icon": "⚡"})
    else:
        adj = -5
        factors.append({"factor": "Gamma Regime", "adj": adj,
                        "detail": "Negative gamma: dealers amplify moves, levels less predictive",
                        "icon": "⚡"})
        caveats.append("Negative gamma regime — ranges will expand beyond GEX levels")
    base += adj

    # ── 4. DTE (near expiry = hypersensitive) ────────────────────────────
    if dte == 0:
        adj = -15
        factors.append({"factor": "DTE", "adj": adj,
                        "detail": "0-DTE today — gamma collapse, walls can break instantly",
                        "icon": "⏱"})
        caveats.append("0-DTE expiry: GEX levels extremely sensitive, use tight stops")
    elif dte == 1:
        adj = -8
        factors.append({"factor": "DTE", "adj": adj,
                        "detail": "1-DTE: elevated gamma sensitivity, pin risk high",
                        "icon": "⏱"})
    elif dte <= 5:
        adj = +3
        factors.append({"factor": "DTE", "adj": adj,
                        "detail": f"{dte}-DTE weekly: good gamma density, levels well-defined",
                        "icon": "⏱"})
    else:
        adj = 0
        factors.append({"factor": "DTE", "adj": adj,
                        "detail": f"{dte}-DTE: monthly options, adequate gamma structure",
                        "icon": "⏱"})
    base += adj

    # ── 5. IV stability (IV vs HV — elevated IV means gamma repricing risk) ──
    if iv_atm:
        if iv_atm > 30:
            adj = -10
            factors.append({"factor": "IV Level", "adj": adj,
                            "detail": f"ATM IV {iv_atm:.0f}% — very elevated, gamma profiles unstable",
                            "icon": "📊"})
            caveats.append(f"IV {iv_atm:.0f}% is very high — gamma recalculates rapidly")
        elif iv_atm > 22:
            adj = -5
            factors.append({"factor": "IV Level", "adj": adj,
                            "detail": f"ATM IV {iv_atm:.0f}% — elevated",
                            "icon": "📊"})
        elif iv_atm < 12:
            adj = +5
            factors.append({"factor": "IV Level", "adj": adj,
                            "detail": f"ATM IV {iv_atm:.0f}% — very low, extremely stable gamma",
                            "icon": "📊"})
        else:
            adj = +3
            factors.append({"factor": "IV Level", "adj": adj,
                            "detail": f"ATM IV {iv_atm:.0f}% — normal range",
                            "icon": "📊"})
        base += adj

    # ── 6. Upcoming earnings for this symbol ─────────────────────────────
    try:
        from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
        _db = _OIAPP_DB_PATH
        _con = _sq.connect(_db, timeout=5)
        earn_row = _con.execute(
            "SELECT next_earn_date FROM earnings_calendar WHERE symbol=?", (sym,)
        ).fetchone()
        _con.close()
        if earn_row and earn_row[0]:
            earn_date = _dt.date.fromisoformat(str(earn_row[0])[:10])
            days_to_earn = (earn_date - today).days
            if days_to_earn == 1:
                adj = -30
                factors.append({"factor": "Earnings", "adj": adj,
                                "detail": f"⚠ EARNINGS TOMORROW ({earn_date}) — do NOT trade GEX levels",
                                "icon": "📅"})
                caveats.append(f"EARNINGS TOMORROW ({earn_date}) — GEX levels invalid for event day")
                base += adj
            elif days_to_earn <= 3:
                adj = -15
                factors.append({"factor": "Earnings", "adj": adj,
                                "detail": f"Earnings in {days_to_earn}d ({earn_date}) — IV inflated, skew distorted",
                                "icon": "📅"})
                base += adj
            elif days_to_earn <= 7:
                adj = -5
                factors.append({"factor": "Earnings", "adj": adj,
                                "detail": f"Earnings in {days_to_earn}d — watch for IV creep",
                                "icon": "📅"})
                base += adj
    except Exception:
        pass

    # ── 7. Known high-impact macro dates (heuristic) ─────────────────────
    # FOMC: typically 8 times/year, roughly every 6-7 weeks
    # We check if tomorrow is a Wed/Thu in weeks 1/3/5/7 of a 2-month cycle
    # Simple proxy: warn on Tue/Wed if near end of month or mid-month
    month_day = tomorrow.day
    if tomorrow.weekday() in (1, 2, 3) and month_day in range(10, 16):
        adj = -5
        factors.append({"factor": "Macro Calendar", "adj": adj,
                        "detail": "Mid-month: possible CPI/FOMC week — check economic calendar",
                        "icon": "🏦"})
        base += adj

    # ── Final score ──────────────────────────────────────────────────────
    score = max(20, min(95, base))

    if score >= 80:
        label = "🟢 High Reliability"
        trade_ok = True
        summary  = f"Today's GEX levels have {score}% structural reliability for tomorrow. Clean setup — normal conditions, stable gamma environment."
    elif score >= 65:
        label = "🟡 Good Reliability"
        trade_ok = True
        summary  = f"Today's GEX levels have {score}% reliability for tomorrow. Good for directional trades — stay aware of noted caveats."
    elif score >= 50:
        label = "🟠 Moderate Reliability"
        trade_ok = True
        summary  = f"Today's GEX levels have {score}% reliability for tomorrow. Use as reference only — tighter stops, size down."
    elif score >= 35:
        label = "🔴 Low Reliability"
        trade_ok = False
        summary  = f"Today's GEX levels have only {score}% reliability for tomorrow. Elevated risk — consider skipping GEX-based trades."
    else:
        label = "⛔ Very Low Reliability"
        trade_ok = False
        summary  = f"GEX levels unreliable ({score}%) — high-impact event or extreme conditions."

    return {
        "score":        score,
        "label":        label,
        "trade_ok":     trade_ok,
        "summary":      summary,
        "factors":      factors,
        "caveats":      caveats,
        "vix":          vix_val,
        "gex_regime":   "POSITIVE" if gex_total >= 0 else "NEGATIVE",
        "computed_for": tomorrow.isoformat(),
    }



def _build_tomorrow_prep(sym: str, spot: float, gex_info: dict, walls: dict,
                          iv_atm: float, sigma_1d: float, reliability: dict,
                          score: int, pcr: float) -> dict:
    """
    Build a concrete 'Tomorrow's Trade Prep' from today's EOD GEX data.
    Returns specific setups with entry triggers, stops, targets.
    """
    import math as _m
    from datetime import date as _d, timedelta as _td

    tomorrow   = (_d.today() + _td(days=1)).isoformat()
    flip       = gex_info.get("gamma_flip") or round(spot)
    pin        = gex_info.get("pin_strike") or round(spot)
    total_gex  = gex_info.get("total_gex", 0)
    pos_gamma  = total_gex >= 0
    regime     = "POSITIVE" if pos_gamma else "NEGATIVE"

    call_walls = sorted(walls.get("top_call_walls", []) or [],
                        key=lambda x: x[1] if isinstance(x, (list,tuple)) else x.get("oi",0),
                        reverse=True)
    put_walls  = sorted(walls.get("top_put_walls", []) or [],
                        key=lambda x: x[1] if isinstance(x, (list,tuple)) else x.get("oi",0),
                        reverse=True)

    def _strike(w):
        if isinstance(w, dict): return w.get("strike", 0)
        if isinstance(w, (list,tuple)): return w[0]
        return float(w)

    top_call = _strike(call_walls[0]) if call_walls else round(spot * 1.015, 0)
    top_put  = _strike(put_walls[0])  if put_walls  else round(spot * 0.985, 0)

    # ── Range and scenario ───────────────────────────────────────────────
    daily_range_est = round(sigma_1d, 2)
    range_pct       = round(sigma_1d / spot * 100, 2)
    wall_range      = round(top_call - top_put, 2)
    midpoint        = round((top_call + top_put) / 2, 2)

    # ── Scenario matrix ──────────────────────────────────────────────────
    # Scenario A: price opens above flip → range day
    # Scenario B: price opens below flip → directional/volatile day
    # Scenario C: price opens at flip ± 0.25% → wait for confirm

    flip_buffer = round(flip * 0.0025, 2)  # 0.25% of flip

    scenarios = []

    if pos_gamma:
        # ── Positive gamma scenarios ──────────────────────────────────
        scenarios.append({
            "id":      "A",
            "trigger": f"Open ≥ ${flip + flip_buffer:.2f} (above flip)",
            "bias":    "Bullish range day",
            "rationale": f"Dealers long gamma above flip — they sell rips, buy dips. "
                         f"Price tends to oscillate between flip (${flip:.0f}) "
                         f"and call wall (${top_call:.0f}).",
            "trades": [
                {
                    "type":    "Bull Put Spread",
                    "strikes": f"Sell ${top_put:.0f}P / Buy ${top_put - 5:.0f}P",
                    "expiry":  "0-1 DTE (tomorrow)",
                    "entry":   f"Open within ${flip:.0f}–${round(flip+sigma_1d*0.3):.0f} range",
                    "max_profit": "Full credit at expiry",
                    "stop":    f"Close below ${top_put - 2:.0f}",
                    "target":  f"50% of credit by 2PM",
                    "rationale": f"Put wall at ${top_put:.0f} = strong OI support. "
                                 f"In positive gamma, dealers defend this level.",
                    "pop_est": 72,
                },
                {
                    "type":    "Iron Condor",
                    "strikes": f"Sell ${top_put:.0f}P–${top_call:.0f}C / "
                               f"Buy ${top_put-5:.0f}P–${top_call+5:.0f}C",
                    "expiry":  "2-3 DTE",
                    "entry":   f"Sell at open if price between ${top_put:.0f}–${top_call:.0f}",
                    "max_profit": "Full credit at expiry",
                    "stop":    f"Either short strike breached + $0.20",
                    "target":  f"50% of credit by end of day",
                    "rationale": f"OI walls define the range. Positive gamma pins price "
                                 f"between ${top_put:.0f}–${top_call:.0f} (${wall_range:.0f} range).",
                    "pop_est": 68,
                },
            ],
        })

        scenarios.append({
            "id":      "B",
            "trigger": f"Open ≤ ${flip - flip_buffer:.2f} (below flip)",
            "bias":    "Cautious / wait",
            "rationale": f"Below flip in positive gamma = dealers defend flip as resistance. "
                         f"Range shifts to ${top_put:.0f}–${flip:.0f}. "
                         f"Wait 30 min for direction before entering.",
            "trades": [
                {
                    "type":    "Bear Call Spread",
                    "strikes": f"Sell ${flip:.0f}C / Buy ${flip + 5:.0f}C",
                    "expiry":  "0-1 DTE",
                    "entry":   f"Sell if price bounces to ${flip:.0f} and fails",
                    "max_profit": "Full credit",
                    "stop":    f"Close above ${flip + 2:.0f}",
                    "target":  f"70% of credit by 1PM",
                    "rationale": f"Flip level ${flip:.0f} becomes resistance. "
                                 f"Positive gamma means dealers sell any bounce here.",
                    "pop_est": 65,
                },
            ],
        })

        scenarios.append({
            "id":      "C",
            "trigger": f"Open ${flip - flip_buffer:.2f}–${flip + flip_buffer:.2f} (at flip)",
            "bias":    "Wait 30 min",
            "rationale": f"Opening at the flip is the most uncertain setup. "
                         f"Wait for the first 30-min candle to confirm direction "
                         f"before entering any position.",
            "trades": [
                {
                    "type":    "Deferred entry",
                    "strikes": "Determine after 30-min candle",
                    "expiry":  "0-1 DTE",
                    "entry":   f"After 10:00 AM — use Scenario A if holds above "
                               f"${flip + flip_buffer:.2f}, Scenario B if breaks below",
                    "max_profit": "—",
                    "stop":    "—",
                    "target":  "—",
                    "rationale": "Patience at the flip pays off — forcing a trade "
                                 "here has the lowest win rate.",
                    "pop_est": None,
                },
            ],
        })
    else:
        # ── Negative gamma scenarios ──────────────────────────────────
        scenarios.append({
            "id":      "A",
            "trigger": f"Open ≥ ${flip + flip_buffer:.2f} (above flip)",
            "bias":    "Bullish trending day",
            "rationale": f"Negative gamma + above flip = dealers SHORT gamma, "
                         f"they BUY as price rises (amplifying the move). "
                         f"First target: ${top_call:.0f} call wall. "
                         f"Range could be {range_pct * 1.5:.1f}% or more.",
            "trades": [
                {
                    "type":    "Long Call Spread (buy vol)",
                    "strikes": f"Buy ${round(spot * 1.003):.0f}C / Sell ${round(spot * 1.018):.0f}C",
                    "expiry":  "0-1 DTE",
                    "entry":   f"Open or on pullback to ${flip:.0f}–${flip + sigma_1d*0.2:.0f}",
                    "max_profit": f"Full spread width if closes above ${round(spot*1.018):.0f}",
                    "stop":    f"Break back below ${flip:.0f}",
                    "target":  f"Call wall ${top_call:.0f} or 70% of spread value",
                    "rationale": "In negative gamma, buying directional spreads beats "
                                 "selling premium — dealers amplify the move in your direction.",
                    "pop_est": 58,
                },
            ],
        })

        scenarios.append({
            "id":      "B",
            "trigger": f"Open ≤ ${flip - flip_buffer:.2f} (below flip)",
            "bias":    "Bearish trending day",
            "rationale": f"Negative gamma + below flip = dealers SELL as price falls "
                         f"(amplifying decline). First target: ${top_put:.0f} put wall. "
                         f"Expect larger than normal daily range.",
            "trades": [
                {
                    "type":    "Long Put Spread (buy vol)",
                    "strikes": f"Buy ${round(spot * 0.997):.0f}P / Sell ${round(spot * 0.982):.0f}P",
                    "expiry":  "0-1 DTE",
                    "entry":   f"Open or on bounce to ${flip:.0f}–${flip - sigma_1d*0.2:.0f}",
                    "max_profit": f"Full spread width if closes below ${round(spot*0.982):.0f}",
                    "stop":    f"Break back above ${flip:.0f}",
                    "target":  f"Put wall ${top_put:.0f} or 70% of spread value",
                    "rationale": "In negative gamma, buying puts beats selling calls — "
                                 "dealers amplify the downside move.",
                    "pop_est": 58,
                },
            ],
        })

    # ── Level checklist for tomorrow morning ────────────────────────────
    checklist = [
        f"✅ Reliability score: {reliability.get('score',0)}% ({reliability.get('label','')})",
        f"📐 Gamma Flip: ${flip:.2f}  →  Bull/bear line for the day",
        f"🟢 Call Wall: ${top_call:.2f}  →  Upside resistance, sell above here",
        f"🔴 Put Wall:  ${top_put:.2f}  →  Downside support, buy below here",
        f"📌 Pin Strike: ${pin:.2f}  →  Gravity toward expiry",
        f"📏 1-sigma daily range: ±${daily_range_est:.2f} (±{range_pct:.2f}%)",
        f"{'⚡ Negative gamma — expect larger ranges, trend days' if not pos_gamma else '⚖ Positive gamma — expect range-bound / mean-reversion day'}",
    ]

    if reliability.get("caveats"):
        checklist += [f"⚠ {c}" for c in reliability["caveats"]]

    return {
        "symbol":       sym,
        "for_date":     tomorrow,
        "spot_now":     round(spot, 2),
        "flip":         flip,
        "pin":          pin,
        "top_call":     top_call,
        "top_put":      top_put,
        "wall_range":   wall_range,
        "midpoint":     midpoint,
        "sigma_1d":     daily_range_est,
        "range_pct":    range_pct,
        "regime":       regime,
        "scenarios":    scenarios,
        "checklist":    checklist,
        "reliability":  reliability,
    }



def build_3pm_trades(sym: str, spot: float, iv_atm: float,
                      gex_info: dict, walls: dict,
                      reliability: dict, score: int) -> dict:
    """
    Build 1-DTE credit spread trades for entry at 3PM today.
    Target: high theta, defined risk, gap-resistant strikes.
    Only generates trades when GEX reliability >= 65.
    """
    import math as _m
    from datetime import date as _d, timedelta as _td

    rel_score  = reliability.get("score", 0)
    trade_ok   = rel_score >= 65
    pos_gamma  = gex_info.get("total_gex", 0) >= 0
    flip       = gex_info.get("gamma_flip") or round(spot)
    pin        = gex_info.get("pin_strike") or round(spot)
    tomorrow   = (_d.today() + _td(days=1)).isoformat()

    def _s(w):
        if isinstance(w, dict): return float(w.get("strike", 0))
        if isinstance(w, (list,tuple)): return float(w[0])
        return float(w)

    cw = sorted(walls.get("top_call_walls",[]) or [], key=lambda x: _s(x) if _s(x)>spot else -1)
    pw = sorted(walls.get("top_put_walls", []) or [], key=lambda x: -(_s(x)) if _s(x)<spot else -1)

    top_call = _s(cw[0]) if cw else round(spot * 1.015 / 0.5) * 0.5
    top_put  = _s(pw[0]) if pw else round(spot * 0.985 / 0.5) * 0.5

    if not trade_ok:
        return {
            "ok": False,
            "reason": f"GEX reliability {rel_score}% is below 65% threshold — skip 1-DTE trades today",
            "reliability_score": rel_score,
            "min_required": 65,
        }

    if not pos_gamma:
        return {
            "ok": False,
            "reason": "Negative gamma regime — selling 1-DTE premium is high risk. "
                      "Dealers amplify moves; a gap can blow through spreads instantly.",
            "reliability_score": rel_score,
            "regime": "NEGATIVE_GAMMA",
        }

    # ── BS pricing (22.5h to expiry = 0.9375 days for 3PM entry) ────────
    T   = 0.9375 / 365.0   # 3PM entry, tomorrow expiry
    iv  = iv_atm / 100.0
    itv = walls.get("interval", 1.0) or 1.0
    width = max(itv * 2, 5.0)   # spread width ~ 2 strikes wide

    def norm_cdf(x):
        if x < 0: return 1 - norm_cdf(-x)
        t = 1/(1+0.2316419*x)
        p = t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))))
        return 1 - (1/_m.sqrt(2*_m.pi))*_m.exp(-0.5*x*x)*p

    def bs_put(K):
        if K <= 0 or iv <= 0: return 0.0
        d1 = (_m.log(spot/K) + 0.5*iv*iv*T) / (iv*_m.sqrt(T))
        d2 = d1 - iv*_m.sqrt(T)
        return max(0.01, K*norm_cdf(-d2) - spot*norm_cdf(-d1))

    def bs_call(K):
        if K <= 0 or iv <= 0: return 0.0
        d1 = (_m.log(spot/K) + 0.5*iv*iv*T) / (iv*_m.sqrt(T))
        d2 = d1 - iv*_m.sqrt(T)
        return max(0.01, spot*norm_cdf(d1) - K*norm_cdf(d2))

    def pop_below(K):   # P(S_T < K)
        d2 = (_m.log(spot/K) - 0.5*iv*iv*T) / (iv*_m.sqrt(T))
        return round(norm_cdf(d2) * 100, 1)

    def pop_above(K):   # P(S_T > K)
        return round(100 - pop_below(K), 1)

    def rr(credit, width):
        """Return risk/reward as string: e.g. 1:2.5"""
        risk = round(width - credit, 2)
        if risk <= 0: return "∞"
        return f"1:{round(risk/credit, 1)}"

    def gap_analysis(short_strike, opt_type, overnight_pcts=(-1.0, -0.5, 0.0, +0.5, +1.0)):
        """Show P&L for different overnight gap scenarios."""
        gaps = []
        for g in overnight_pcts:
            gap_spot = spot * (1 + g/100)
            if opt_type == "put":
                itm = gap_spot < short_strike
                otm_dist = round((gap_spot - short_strike) / spot * 100, 2)
            else:
                itm = gap_spot > short_strike
                otm_dist = round((short_strike - gap_spot) / spot * 100, 2)
            gaps.append({
                "gap_pct": g,
                "gap_price": round(gap_spot, 2),
                "in_the_money": itm,
                "dist_from_strike_pct": otm_dist,
                "status": "⚠ ITM at open" if itm else f"✅ OTM {otm_dist:+.1f}%",
            })
        return gaps

    trades = []

    # ── Trade 1: Bull Put Spread ─────────────────────────────────────────
    # Short strike: OI put wall (strong support). Long: 1 width below.
    # Optimal when: spot > flip (above gamma flip = bullish bias)
    bps_short = round(top_put / itv) * itv        # snap to nearest strike
    bps_long  = bps_short - width
    bps_credit = round(bs_put(bps_short) - bs_put(bps_long), 2)
    bps_max_loss = round(width - bps_credit, 2)
    bps_pop = pop_above(bps_short)   # need to stay above short put

    trades.append({
        "id":         "T1",
        "name":       "Bull Put Spread",
        "type":       "credit",
        "direction":  "Bullish / Neutral",
        "best_when":  f"Spot opens ≥ ${flip:.2f} (above flip) tomorrow",
        "legs": [
            {"action": "SELL", "option": f"${bps_short:.0f} Put", "expiry": tomorrow},
            {"action": "BUY",  "option": f"${bps_long:.0f} Put",  "expiry": tomorrow},
        ],
        "short_strike":  bps_short,
        "long_strike":   bps_long,
        "width":         width,
        "credit_est":    bps_credit,
        "max_profit":    bps_credit,
        "max_loss":      bps_max_loss,
        "rr":            rr(bps_credit, width),
        "pop":           bps_pop,
        "theta_edge":    "High — 22h theta decay from 3PM to tomorrow close",
        "stop":          f"Exit if spot trades below ${bps_short - 1:.0f} (long put activated)",
        "target":        f"Close at 50% profit OR hold to expiry if staying above ${bps_short:.0f}",
        "rationale":     (f"OI put wall at ${top_put:.0f} = largest put OI cluster = strong support. "
                          f"Positive gamma means dealers defend this level. "
                          f"Flip at ${flip:.0f} = your invalidation."),
        "gap_analysis":  gap_analysis(bps_short, "put"),
    })

    # ── Trade 2: Bear Call Spread ────────────────────────────────────────
    # Short: OI call wall (resistance). Long: 1 width above.
    bcs_short = round(top_call / itv) * itv
    bcs_long  = bcs_short + width
    bcs_credit = round(bs_call(bcs_short) - bs_call(bcs_long), 2)
    bcs_max_loss = round(width - bcs_credit, 2)
    bcs_pop = pop_below(bcs_short)   # need to stay below short call

    trades.append({
        "id":         "T2",
        "name":       "Bear Call Spread",
        "type":       "credit",
        "direction":  "Bearish / Neutral",
        "best_when":  f"Spot opens ≤ ${flip:.2f} (below flip) tomorrow",
        "legs": [
            {"action": "SELL", "option": f"${bcs_short:.0f} Call", "expiry": tomorrow},
            {"action": "BUY",  "option": f"${bcs_long:.0f} Call",  "expiry": tomorrow},
        ],
        "short_strike":  bcs_short,
        "long_strike":   bcs_long,
        "width":         width,
        "credit_est":    bcs_credit,
        "max_profit":    bcs_credit,
        "max_loss":      bcs_max_loss,
        "rr":            rr(bcs_credit, width),
        "pop":           bcs_pop,
        "theta_edge":    "High — 22h theta decay from 3PM to tomorrow close",
        "stop":          f"Exit if spot trades above ${bcs_short + 1:.0f}",
        "target":        f"50% profit OR hold to expiry if staying below ${bcs_short:.0f}",
        "rationale":     (f"OI call wall at ${top_call:.0f} = ceiling. "
                          f"Dealers hedge here — resistance is sticky. "
                          f"Positive gamma compresses upside."),
        "gap_analysis":  gap_analysis(bcs_short, "call"),
    })

    # ── Trade 3: Iron Condor ─────────────────────────────────────────────
    # Combine both — only when pin strike is near midpoint
    ic_credit    = round(bps_credit + bcs_credit, 2)
    ic_max_loss  = round(width - ic_credit, 2)   # assume one side max
    ic_pop       = round(bps_pop * bcs_pop / 100, 1)  # rough joint PoP

    trades.append({
        "id":         "T3",
        "name":       "Iron Condor",
        "type":       "credit",
        "direction":  "Neutral",
        "best_when":  f"Spot opens within ${top_put:.0f}–${top_call:.0f} (between walls)",
        "legs": [
            {"action": "SELL", "option": f"${bcs_short:.0f} Call", "expiry": tomorrow},
            {"action": "BUY",  "option": f"${bcs_long:.0f} Call",  "expiry": tomorrow},
            {"action": "SELL", "option": f"${bps_short:.0f} Put",  "expiry": tomorrow},
            {"action": "BUY",  "option": f"${bps_long:.0f} Put",   "expiry": tomorrow},
        ],
        "short_call": bcs_short, "long_call": bcs_long,
        "short_put":  bps_short, "long_put":  bps_long,
        "width":      width,
        "credit_est": ic_credit,
        "max_profit": ic_credit,
        "max_loss":   ic_max_loss,
        "rr":         rr(ic_credit, width),
        "pop":        ic_pop,
        "theta_edge": "Maximum — collecting theta on both wings simultaneously",
        "stop":       f"Exit if spot breaks above ${bcs_short:.0f} or below ${bps_short:.0f}",
        "target":     f"50% of total credit, or full if pinned near ${pin:.0f}",
        "rationale":  (f"OI walls define the expected range: ${top_put:.0f}–${top_call:.0f}. "
                       f"Pin strike at ${pin:.0f} is the gravity center. "
                       f"Positive gamma + 1-DTE = maximum pinning force."),
        "gap_analysis": None,  # both sides visible in T1+T2
    })

    # ── Gap risk summary ─────────────────────────────────────────────────
    sigma_overnight = round(iv_atm / 100 * spot * _m.sqrt(0.9375/365), 2)
    gap_1pct = round(spot * 0.01, 2)
    put_buffer = round(spot - top_put, 2)
    call_buffer = round(top_call - spot, 2)

    gap_context = {
        "overnight_1sigma":   sigma_overnight,
        "overnight_1sigma_pct": round(sigma_overnight/spot*100, 2),
        "put_wall_buffer":     put_buffer,
        "put_wall_buffer_pct": round(put_buffer/spot*100, 2),
        "call_wall_buffer":    call_buffer,
        "call_wall_buffer_pct": round(call_buffer/spot*100, 2),
        "gap_exceeds_put_wall":  f"Gap of >{round(put_buffer/spot*100,1)}% needed to breach put wall",
        "gap_exceeds_call_wall": f"Gap of >{round(call_buffer/spot*100,1)}% needed to breach call wall",
        "historical_note": ("SPY gaps >1% overnight on ~8% of sessions. "
                            "Positive gamma + reliability ≥65% reduces this further."),
    }

    return {
        "ok":            True,
        "symbol":        sym,
        "entry_time":    "3:00–3:30 PM today",
        "expiry":        tomorrow,
        "dte":           1,
        "spot":          round(spot, 2),
        "flip":          flip,
        "top_call":      top_call,
        "top_put":       top_put,
        "pin":           pin,
        "iv_atm":        round(iv_atm, 1),
        "regime":        "POSITIVE_GAMMA",
        "reliability_score": rel_score,
        "trades":        trades,
        "gap_context":   gap_context,
        "entry_rules":   [
            f"Enter between 3:00–3:30 PM — after most of today's move has played out",
            f"Check spot vs flip (${flip:.2f}): above = lean bull spread, below = lean bear, at flip = iron condor",
            f"Confirm VIX has NOT spiked >2pts in the last hour before entry",
            f"Do NOT enter if any news/events scheduled for tomorrow morning (check calendar)",
            f"Size: max 2-3% of account per spread given 1-DTE risk",
        ],
        "exit_rules":    [
            f"Target: 50% of credit collected (typical 1-DTE GEX mean-reversion target)",
            f"Stop:   Short strike breached + $0.15-0.20 cushion",
            f"Time stop: If not 50% by 1PM tomorrow, close at market to avoid pin risk",
            f"Never hold iron condor past 3PM expiry day — gamma explodes in final hour",
        ],
    }



def _ensure_gex_snapshot_table():
    con = _conn()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS gex_plan_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                expiry TEXT,
                label TEXT DEFAULT 'snapshot',
                captured_at TEXT DEFAULT (datetime('now')),
                spot REAL,
                score REAL,
                confidence REAL,
                bias TEXT,
                regime TEXT,
                payload_json TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_gex_snapshot_sym_time ON gex_plan_snapshots(symbol, captured_at DESC)")
        con.commit()
    finally:
        con.close()


def _cleanup_gex_plan_snapshots(con=None, symbol=None, keep_limit=None):
    """Compatibility no-op.

    GEX plans are now retained as historical, date-filterable backtest inputs.
    Older versions deleted prior days and trimmed the current day as a UI cache;
    this function intentionally performs no deletion.
    """
    return None


def _save_daily_plan_snapshot(payload, label='snapshot'):
    if not payload:
        return False
    # Do not persist embedded historical snapshot lists inside each snapshot.
    payload_to_store = dict(payload)
    payload_to_store.pop('snapshots', None)
    payload_to_store.pop('snapshot_saved', None)
    payload_to_store.pop('snapshot_retention', None)
    _ensure_gex_snapshot_table()
    con = _conn()
    try:
        _cleanup_gex_plan_snapshots(con, symbol=str(payload.get('symbol') or 'SPY').upper())
        con.execute(
            """
            INSERT INTO gex_plan_snapshots(symbol, expiry, label, captured_at, spot, score, confidence, bias, regime, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(payload.get('symbol') or 'SPY').upper(),
                payload.get('expiry'),
                str(label or 'snapshot'),
                datetime.now().isoformat(timespec='seconds'),
                payload.get('spot'),
                payload.get('score'),
                payload.get('confidence'),
                payload.get('bias'),
                payload.get('regime'),
                json.dumps(_json_safe(payload_to_store), allow_nan=False),
            ),
        )
        # Snapshots are retained permanently for date-filtered review and backtesting.
        con.commit()
        return True
    finally:
        con.close()


def _load_daily_plan_snapshots(symbol='SPY', limit=4, snapshot_date=None):
    _ensure_gex_snapshot_table()
    con = _conn()
    try:
        # Default to today for the live UI, but permit any saved date for review.
        selected_date = str(snapshot_date or date.today().isoformat())[:10]
        rows = con.execute(
            """
            SELECT captured_at, label, spot, score, confidence, bias, regime, expiry, payload_json
            FROM gex_plan_snapshots
            WHERE symbol=?
              AND substr(COALESCE(captured_at,''),1,10)=?
            ORDER BY datetime(captured_at) DESC, id DESC
            LIMIT ?
            """,
            (str(symbol or 'SPY').upper(), selected_date, int(limit or 4)),
        ).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r['payload_json']) if r['payload_json'] else {}
            except Exception:
                payload = {}
            out.append({
                'captured_at': r['captured_at'],
                'label': r['label'],
                'spot': r['spot'],
                'score': r['score'],
                'confidence': r['confidence'],
                'bias': r['bias'],
                'regime': r['regime'],
                'expiry': r['expiry'],
                'gamma_flip': (payload.get('interpretation') or {}).get('flip_note'),
                'pin_note': (payload.get('interpretation') or {}).get('pin_note'),
                'max_pain_note': (payload.get('interpretation') or {}).get('max_pain_note'),
                'gex_strength': payload.get('gex_strength'),
                'payload': payload,
            })
        return out
    finally:
        con.close()


def _nearest_expiry_for_dte(symbol, target_date):
    """Closest available expiry to a specific target trading DATE,
    reusing _future_exps (the same expiry-listing query api_weekly's own
    expiry dropdown uses) rather than a separate lookup. Returns None if
    no expiries exist at all for this symbol.

    Takes a real date now, not a raw calendar-day offset from today --
    the caller is expected to have already resolved "DTE 0-5" into
    actual trading days via macro_regime.next_trading_days(), which
    skips weekends and US market holidays. Confirmed directly why this
    mattered: with the old raw-offset version, a request made on a
    Saturday with Monday being Labor Day had targets 0/1/2/3 all
    collapse onto the same Tuesday expiry, since days 0-2 corresponded
    to calendar dates (Sat/Sun/Mon-holiday) with no possible listed
    contract at all -- not a data gap, but the function never
    distinguished "no contract exists for this date" from "this isn't
    a trading date in the first place."
    """
    from datetime import date as _d
    exps = _future_exps(symbol)
    if not exps:
        return None
    best, best_diff = None, None
    for e in exps:
        try:
            actual_date = _d.fromisoformat(e)
        except Exception:
            continue
        diff = abs((actual_date - target_date).days)
        if best_diff is None or diff < best_diff:
            best, best_diff = {"expiry": e, "actual_dte": (actual_date - _d.today()).days}, diff
    return best


@spy_bp.route("/weekly_rolling")
def api_weekly_rolling():
    """0DTE or 0-5DTE rolling plan, strictly SPY/QQQ/IWM, with an optional
    user-supplied directional bias.

    Deliberately does NOT reimplement any of api_weekly's ~2400 lines of
    proven strike/wall/IV-rank/POP logic -- that function is called
    internally, once per (symbol, unique expiry) pair, via Flask's own
    test client. This is the standard, safe way to reuse an existing
    route's full logic from another route in the same app without
    touching its internals at all: zero risk of a transcription error in
    a function this large, and any future fix to api_weekly automatically
    applies here too, since there's only one copy of the actual logic.

    Per-symbol futures OI context (score -3 to +3, reusing
    analyze_roll_adjusted's own established SPY->ES/QQQ->NQ/IWM->RTY
    mapping) is fetched once per symbol, not once per day, since it isn't
    DTE-dependent -- it reflects current positioning, not a specific
    expiry.
    """
    mode = (request.args.get("mode") or "0-5dte").strip().lower()
    if mode not in ("0dte", "0-5dte"):
        mode = "0-5dte"
    bias_override = (request.args.get("bias") or "").strip().lower() or None
    if bias_override not in (None, "bullish", "bearish", "sideways"):
        bias_override = None
    # DTE 0-5 means the next N genuine TRADING days, not raw calendar
    # days -- weekends and market holidays skipped via
    # macro_regime.next_trading_days(), which is rule-based (computes
    # Good Friday, Labor Day, etc. per year) rather than a hardcoded,
    # staleness-prone date list. This directly fixes a confirmed issue:
    # a request made on a Saturday with the following Monday being Labor
    # Day previously collapsed targets 0-3 onto one expiry, since those
    # calendar dates couldn't possibly have a listed contract at all.
    from ..services.macro_regime import next_trading_days
    from datetime import date as _d
    n_days = 1 if mode == "0dte" else 6
    target_dates = next_trading_days(_d.today(), n_days)
    max_target_dte = (target_dates[-1] - _d.today()).days

    # Macro is computed ONCE, not per-symbol -- it's the same regardless
    # of which of the three ETFs is being planned. FOMC gate uses the
    # widest DTE actually being served in this call, since that's the
    # real window a short-DTE position is exposed for. See
    # macro_regime.py's own docstring for what's a real, verified gate
    # here (FOMC-in-window) versus what isn't (a market-implied hike/cut
    # probability, which this app can't honestly compute -- see that
    # docstring for exactly why).
    try:
        from ..services.macro_regime import get_macro_regime, fomc_in_window, vix_extremes
        macro = get_macro_regime()
        fomc = fomc_in_window(days_ahead=max_target_dte)
        vix = vix_extremes()
    except Exception as e:
        macro = {"equity_headwind_score": 0, "equity_notes": [f"macro unavailable: {e}"]}
        fomc = {"available": False, "in_window": False, "note": str(e)}
        vix = {"available": False, "note": str(e)}

    symbols = ["SPY", "QQQ", "IWM"]  # strictly these three, per spec -- not the general watchlist
    client = current_app.test_client()
    results = {}

    for sym in symbols:
        try:
            from ..services.futures_oi_real import analyze_roll_adjusted
            futures_oi = analyze_roll_adjusted(sym)
        except Exception as e:
            futures_oi = {"signal": "UNAVAILABLE", "error": str(e)}

        seen_expiries = {}  # expiry -> list of trading-day indices (0=next trading
                             # day, 1=the one after, etc) it serves, so a missing
                             # 0DTE contract that maps to the same Friday as later
                             # days doesn't get computed 6 times
        for i, target_date in enumerate(target_dates):
            match = _nearest_expiry_for_dte(sym, target_date)
            if not match:
                continue
            seen_expiries.setdefault(match["expiry"], []).append(i)

        days = []
        for expiry, dtes_served in seen_expiries.items():
            try:
                url = f"/spy/weekly?symbol={sym}&expiry={expiry}"
                if bias_override:
                    url += f"&bias_override={bias_override}"
                resp = client.get(url)
                plan = resp.get_json() if resp.status_code == 200 else {"error": f"HTTP {resp.status_code}"}
            except Exception as e:
                plan = {"error": str(e)}
            days.append({
                "target_dtes_served": sorted(dtes_served),
                "expiry": expiry,
                "plan": plan,
            })
        days.sort(key=lambda d: min(d["target_dtes_served"]))

        results[sym] = {"futures_oi": futures_oi, "days": days}

    return jsonify({
        "ok": True, "mode": mode, "bias_override": bias_override,
        "symbols": symbols, "results": results,
        "macro": macro, "fomc": fomc, "vix": vix,
    })


@spy_bp.route("/daily_plan")
def api_daily_plan():
    """GEX-based daily plan with expiry + trade suggestions with PoP scores."""
    import math
    sym  = (request.args.get("symbol") or "SPY").upper()
    sel_expiry = request.args.get("expiry","")   # user-selected expiry
    oi_sig_pct = _oi_sig_threshold_from_args(request.args, 30.0)

    ta = _compute_ta(sym)
    spot_snapshot = None
    if ta:
        try:
            from ..services.market import get_spot_snapshot as _live_spot_snapshot
            spot_snapshot = _live_spot_snapshot(sym)
            _live_num = _finite_number((spot_snapshot or {}).get("price"))
            if _live_num is not None:
                ta["price"] = round(_live_num, 2)
        except Exception:
            pass
    if not ta:
        try:
            df = yf.Ticker(sym).history(period="3mo", prepost=True)
            if df is None or df.empty:
                return jsonify({"error": f"TA failed for {sym}"}), 400
            closes = [v for v in (_finite_number(x) for x in df["Close"].tolist()) if v is not None]
            if not closes:
                return jsonify({"error": f"TA failed for {sym}"}), 400
            spot = closes[-1]
            prev = closes[-2] if len(closes) > 1 else spot
            highs = [v for v in (_finite_number(x) for x in df["High"].tolist()) if v is not None]
            lows = [v for v in (_finite_number(x) for x in df["Low"].tolist()) if v is not None]
            ta = {
                "price": round(spot, 2),
                "prev_close": round(prev, 2),
                "chg_pct": round((spot - prev) / prev * 100, 2) if prev else 0,
                "rsi": 50.0, "ema90_rsi": 50.0, "rsi_ema_diff": 0.0,
                "ema20": round(spot, 2), "ema50": round(spot, 2),
                "atr": round(max(spot * 0.02, 0.01), 2),
                "bb_pct": 50.0, "bb_upper": round(spot * 1.02, 2), "bb_lower": round(spot * 0.98, 2),
                "trend": "UNKNOWN", "momentum": "NEUTRAL",
                "iv_rank": 40, "iv_est": 20.0,
                "high52": round(max(highs), 2) if highs else None,
                "low52": round(min(lows), 2) if lows else None,
            }
        except Exception as e:
            return jsonify({"error": f"TA failed for {sym}: {e}"}), 400

    spot = _finite_number(ta.get("price"))
    if spot is None:
        try:
            df = yf.Ticker(sym).history(period="5d", prepost=True)
            if df is not None and not df.empty:
                closes = [ _finite_number(v) for v in df["Close"].tolist() ]
                closes = [v for v in closes if v is not None]
                if closes:
                    spot = closes[-1]
        except Exception:
            spot = None
    if spot is None:
        return jsonify({"error": f"Unable to determine live spot for {sym}"}), 400
    ta["price"] = round(spot, 2)
    iv_atm = _finite_number(ta.get("iv", 15.0), 15.0) or 15.0

    # Expiry list
    exps = _future_exps(sym)
    if not exps:
        try: exps = list(yf.Ticker(sym).options[:6])
        except: pass

    # Use selected expiry or auto-pick a near-term GEX expiry.
    if sel_expiry and sel_expiry in exps:
        exp, dte = sel_expiry, _expiry_dte(sel_expiry)
    elif sel_expiry:
        # If the caller selected an expiry not present in the local list, still
        # try the explicit date first; the downstream OI fetch may populate it.
        exp, dte = sel_expiry, _expiry_dte(sel_expiry)
    else:
        exp, dte = _pick_exp(exps, 0, 5, 0)
    if not exp: return jsonify({"error":"No expiry found"}), 404

    rows = _oi_rows(sym, exp)
    if not rows:
        try:
            from ..services.market import fetch_store_for
            fetch_store_for(sym, expirations=[exp])
            rows = _oi_rows(sym, exp)
        except: pass

    # GEX
    oi_change_filter = _oi_change_filter_context(sym, rows, expiry=exp, source="gex_plan", min_change_pct=oi_sig_pct)
    T_days = max(1, dte)
    gex_info = _compute_gex(rows, spot, T_days, iv_atm)
    gex_info = dict(gex_info or {})
    gex_info["gamma_flip"] = _finite_number(gex_info.get("gamma_flip")) or spot
    gex_info["pin_strike"] = _finite_number(gex_info.get("pin_strike")) or spot
    gex_info["max_pain"] = _finite_number(gex_info.get("max_pain")) or gex_info["pin_strike"]

    # PCR (multi-expiry)
    all_rows = []
    for e in exps[:5]: all_rows.extend(_oi_rows(sym, e))
    total_calls = sum(int(r["oi"] or 0) for r in all_rows if r["type"]=="call")
    total_puts  = sum(int(r["oi"] or 0) for r in all_rows if r["type"]=="put")
    pcr = round(total_puts / max(1, total_calls), 3)

    skew_rr  = _compute_skew_rr(rows, spot, T_days, iv_atm)
    sigma_1d = round(spot * (iv_atm/100) / _math.sqrt(252), 2) if spot and iv_atm else 0.0

    walls = _walls(rows, spot, gex_info=gex_info, sigma_1d=sigma_1d, oi_change_filter=oi_change_filter) if rows else {
        "support": round(spot*0.985,2), "resistance": round(spot*1.015,2),
        "gamma_wall": round(spot), "interval":1.0,
        "top_put_walls":[], "top_call_walls":[],
        "significant_put_walls": [], "significant_call_walls": [],
        "oi_change_filter": oi_change_filter}
    walls = dict(walls or {})
    walls["support"] = _finite_number(walls.get("support")) or round(spot*0.985,2)
    walls["resistance"] = _finite_number(walls.get("resistance")) or round(spot*1.015,2)
    walls["gamma_wall"] = _finite_number(walls.get("gamma_wall")) or round(spot, 2)

    top_sig = (walls.get("significant_put_walls") or [])[:1] + (walls.get("significant_call_walls") or [])[:1]
    wall_strength_score = round(sum(float(w.get("score") or 0) for w in top_sig) / max(1, len(top_sig)), 1) if top_sig else 0
    if walls.get("significant_put_walls") and walls.get("significant_call_walls"):
        wall_strength_summary = (
            f"Put {walls['significant_put_walls'][0]['strike']} {walls['significant_put_walls'][0]['label']} · "
            f"Call {walls['significant_call_walls'][0]['strike']} {walls['significant_call_walls'][0]['label']}"
        )
    elif walls.get("significant_put_walls"):
        wall_strength_summary = f"Put {walls['significant_put_walls'][0]['strike']} {walls['significant_put_walls'][0]['label']}"
    elif walls.get("significant_call_walls"):
        wall_strength_summary = f"Call {walls['significant_call_walls'][0]['strike']} {walls['significant_call_walls'][0]['label']}"
    else:
        wall_strength_summary = "No actionable walls"
    walls["significance"] = {
        "top_put_walls": walls.get("significant_put_walls", []),
        "top_call_walls": walls.get("significant_call_walls", []),
        "score": wall_strength_score,
        "summary": wall_strength_summary,
        "method": walls.get("method"),
        "oi_change_filter": walls.get("oi_change_filter") or oi_change_filter,
    }
    wall_strength = walls.get("significance") or {}

    # VIX is now part of the scored GEX grid, not just a footer overlay.
    vix_ctx = _vix_context()
    score, confidence, regime, factors = _five_factor_score(
        gex_info["total_gex"], pcr, skew_rr, spot,
        gex_info["pin_strike"], gex_info["gamma_flip"], rows,
        gex_ratio=gex_info.get("gex_ratio"), max_pain=gex_info.get("max_pain"),
        dte=dte, vix_level=(vix_ctx or {}).get("level"), gex_strength_pct=gex_info.get("regime_strength"))

    plan = _build_trade_plan(spot, gex_info, walls, ta, score, sigma_1d)

    # ── Trade suggestions with PoP ───────────────────────────────
    suggestions = _gex_trade_suggestions(sym, exp, dte, spot, iv_atm, score,
                                          gex_info, walls, sigma_1d, pcr, rows)

    bias_label = "BEARISH" if score<0 else "BULLISH" if score>0 else "NEUTRAL"
    sub_regime = ""
    if skew_rr<-1.0 and pcr>1.1: sub_regime = "Skew × PCR confluence"
    elif gex_info["total_gex"]<0 and abs(score)>=3: sub_regime = "Neg GEX + directional bias"
    elif gex_info.get("max_pain"):
        sub_regime = f"Max pain near ${gex_info['max_pain']:.0f}" if abs(gex_info['max_pain'] - spot) / spot < 0.01 else sub_regime

    reliability = compute_gex_reliability(sym, spot, gex_info["total_gex"], iv_atm, dte)
    if (vix_ctx or {}).get("level") is None and (reliability or {}).get("vix") is not None:
        vix_ctx = {"available": True, "level": (reliability or {}).get("vix"), "regime": "FROM_RELIABILITY"}

    wall_score = _finite_number((wall_strength or {}).get("score"), 0) or 0
    wall_signal = "High" if wall_score >= 75 else "Good" if wall_score >= 60 else "Mixed" if wall_score >= 40 else "Weak"
    if wall_score >= 75:
        confidence = min(95, confidence + 5)
    elif wall_score < 40:
        confidence = max(25, confidence - 5)
    # Wall Quality is displayed in the dedicated Significant Walls panel below;
    # it adjusts confidence but is not counted as part of the 5 core + VIX + DTE composite grid.

    action_guide = _build_gex_action_guide(
        sym, spot, exp, dte, gex_info, walls, pcr, score, confidence, sigma_1d,
        vix_ctx=vix_ctx, factors=factors,
    )

    payload = {
        "symbol": sym, "date": date.today().isoformat(),
        "spot": spot,
        "spot_source": (spot_snapshot or {}).get("source"),
        "spot_time": (spot_snapshot or {}).get("timestamp"),
        "spot_prev_close": (spot_snapshot or {}).get("prev_close"),
        "spot_change_pct": (spot_snapshot or {}).get("change_pct"),
        "expiry": exp, "dte": dte,
        "expiry_list": exps[:8],
        "iv_atm": round(iv_atm,2), "pcr": pcr,
        "skew_rr": skew_rr, "sigma_1d": sigma_1d,
        "gex_strength": gex_info.get("regime_strength"),
        "max_pain": gex_info.get("max_pain"),
        "oi_change_filter": oi_change_filter,
        "sigma_range": {"low": round(spot-sigma_1d,2), "high": round(spot+sigma_1d,2)},
        "gex": gex_info, "score": score, "confidence": confidence,
        "reliability": reliability,
        "trades_3pm": build_3pm_trades(sym, spot, iv_atm, gex_info, walls, reliability, score),
        "tomorrow_prep": _build_tomorrow_prep(sym, spot, gex_info, walls, iv_atm, sigma_1d, reliability, score, pcr),
        "bias": bias_label, "regime": regime, "sub_regime": sub_regime,
        "composite_grid": {"factor_count": len(factors), "label": f"{len(factors)}-factor GEX composite grid",
                           "factors": [f.get("name") for f in factors]},
        "interpretation": {
            "dealer_bias": "Negative gamma amplifies moves" if gex_info["total_gex"] < 0 else "Positive gamma cushions moves",
            "flip_note": f"Gamma flip near ${gex_info['gamma_flip']:.2f}" if gex_info.get("gamma_flip") else "No stable flip found",
            "pin_note": f"Pin/magnet near ${gex_info['pin_strike']:.2f}",
            "max_pain_note": f"Max pain near ${gex_info['max_pain']:.2f}" if gex_info.get("max_pain") else "Max pain unavailable",
            "wall_note": (wall_strength or {}).get("summary"),
            "vix_note": "VIX is used as a reliability/risk overlay, not as a wall selector.",
            "regime_strength": gex_info.get("regime_strength"),
        },
        "factors": factors, "plan": plan, "walls": walls,
        "wall_strength": wall_strength,
        "significant_walls": wall_strength,
        "vix_overlay": {
            "vix": (vix_ctx or {}).get("level") if (vix_ctx or {}).get("level") is not None else (reliability or {}).get("vix"),
            "regime": (vix_ctx or {}).get("regime"),
            "note": (vix_ctx or {}).get("note") or "VIX is used as a scored risk/reliability factor and position-sizing overlay.",
            "reliability_score": (reliability or {}).get("score"),
            "trade_ok": (reliability or {}).get("trade_ok"),
        },
        "action_guide": action_guide,
        "suggestions": suggestions,
        "ta": {k: ta[k] for k in ["rsi","macd_sig","adx","bb_pct","atr"] if k in ta},
        "history": [{"date":d,"close":c} for d,c in
                    zip(ta.get("history_dates",[])[-10:], ta.get("history_closes",[])[-10:])],
    }
    snapshot_saved = False
    if str(request.args.get("save_snapshot") or request.args.get("save") or "").lower() in ("1", "true", "yes", "y"):
        label = request.args.get("snapshot_label") or request.args.get("label") or "on demand"
        snapshot_saved = bool(_save_daily_plan_snapshot(payload, label=label))
    payload["snapshot_saved"] = snapshot_saved
    payload["snapshot_retention"] = {
        "policy": "retained_by_date",
        "selected_date": date.today().isoformat(),
    }
    payload["snapshots"] = _load_daily_plan_snapshots(sym, limit=12)
    return jsonify(_json_safe(payload))



@spy_bp.route("/daily_plan_snapshots")
def api_daily_plan_snapshots():
    sym = (request.args.get("symbol") or "SPY").upper()
    limit = request.args.get("limit", 4, type=int)
    selected_date = (request.args.get("date") or date.today().isoformat())[:10]
    try:
        date.fromisoformat(selected_date)
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    return jsonify(_json_safe({
        "symbol": sym,
        "date": selected_date,
        "retention": {"policy": "retained_by_date"},
        "snapshots": _load_daily_plan_snapshots(sym, limit=max(1, limit), snapshot_date=selected_date),
    }))

def _gex_trade_suggestions(sym, exp, dte, spot, iv_atm, score, gex_info, walls, sigma_1d, pcr, rows):
    """
    Build 3-5 concrete trade suggestions with strikes, PoP, R:R, confidence.
    Uses GEX levels as natural boundaries.
    """
    import math
    T   = max(dte, 1) / 365.0
    iv  = iv_atm / 100.0
    itv = walls.get("interval", 1.0)
    
    def norm_cdf(x):
        """Abramowitz & Stegun approximation."""
        if x < 0: return 1 - norm_cdf(-x)
        t = 1/(1+0.2316419*x)
        p = t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))))
        return 1 - (1/_math.sqrt(2*_math.pi))*_math.exp(-0.5*x*x)*p

    def pop_above(K):
        """P(S_T > K) under lognormal."""
        if K <= 0: return 0
        d2 = (_math.log(spot/K) - 0.5*iv*iv*T) / (iv*_math.sqrt(T))
        return norm_cdf(d2)

    def pop_below(K):
        return 1 - pop_above(K)

    def pop_between(K_lo, K_hi):
        return pop_above(K_lo) - pop_above(K_hi)

    def snap(k):
        """Snap to nearest tradeable strike."""
        return round(round(k / itv) * itv, 2)

    def bs_credit_approx(K_sell, K_buy, opt_type):
        """BS credit approximation for spread."""
        def bs_price(K, flag):
            d1 = (_math.log(spot/K) + 0.5*iv*iv*T) / (iv*_math.sqrt(T))
            d2 = d1 - iv*_math.sqrt(T)
            if flag == 'c':
                return max(0.01, spot*norm_cdf(d1) - K*norm_cdf(d2))
            else:
                return max(0.01, K*norm_cdf(-d2) - spot*norm_cdf(-d1))
        sell_px = bs_price(K_sell, 'p' if opt_type=='put' else 'c')
        buy_px  = bs_price(K_buy,  'p' if opt_type=='put' else 'c')
        return round(sell_px - buy_px, 2)

    gamma_flip = gex_info.get("gamma_flip") or snap(spot * 1.005)
    pin        = gex_info.get("pin_strike") or snap(spot)
    put_wall   = walls.get("support")    or snap(spot * 0.980)
    call_wall  = walls.get("resistance") or snap(spot * 1.020)
    
    # Use sigma-based wing sizing, anchored to GEX levels
    wing_w = max(itv*2, round(sigma_1d * 1.5 / itv) * itv)

    suggestions = []

    # ── 1. Bear Call Spread (if bearish or at/below gamma flip) ──
    if score <= 0:
        sell_k = snap(gamma_flip if gamma_flip > spot else spot + sigma_1d * 0.5)
        buy_k  = snap(sell_k + wing_w)
        credit = bs_credit_approx(sell_k, buy_k, 'call')
        width  = buy_k - sell_k
        if width > 0 and credit > 0:
            pop = round(pop_below(sell_k) * 100, 1)
            rr  = round(credit / max(0.01, width - credit), 2)
            conf = min(90, round(50 + abs(score)*6 + (5 if pcr>1.1 else 0)))
            suggestions.append({
                "strategy": "Bear Call Spread",
                "type": "credit",
                "direction": "bearish",
                "sell_strike": sell_k, "buy_strike": buy_k,
                "opt_type": "call",
                "expiry": exp, "dte": dte,
                "credit": round(credit, 2),
                "max_profit": round(credit * 100, 0),
                "max_loss": round((width - credit) * 100, 0),
                "breakeven": round(sell_k + credit, 2),
                "pop": pop, "rr": rr, "confidence": conf,
                "rationale": f"Sell ${sell_k}C at gamma flip ${gamma_flip}. Score {score:+d}, PCR {pcr}.",
                "anchor": f"Gamma Flip ${gamma_flip}"
            })

    # ── 2. Bull Put Spread (if bullish or above gamma flip) ──
    if score >= 0:
        sell_k = snap(gamma_flip if gamma_flip < spot else spot - sigma_1d * 0.5)
        buy_k  = snap(sell_k - wing_w)
        credit = bs_credit_approx(sell_k, buy_k, 'put')
        width  = sell_k - buy_k
        if width > 0 and credit > 0:
            pop = round(pop_above(sell_k) * 100, 1)
            rr  = round(credit / max(0.01, width - credit), 2)
            conf = min(90, round(50 + abs(score)*6 + (5 if pcr<0.9 else 0)))
            suggestions.append({
                "strategy": "Bull Put Spread",
                "type": "credit",
                "direction": "bullish",
                "sell_strike": sell_k, "buy_strike": buy_k,
                "opt_type": "put",
                "expiry": exp, "dte": dte,
                "credit": round(credit, 2),
                "max_profit": round(credit * 100, 0),
                "max_loss": round((width - credit) * 100, 0),
                "breakeven": round(sell_k - credit, 2),
                "pop": pop, "rr": rr, "confidence": conf,
                "rationale": f"Sell ${sell_k}P at gamma flip ${gamma_flip}. Score {score:+d}.",
                "anchor": f"Gamma Flip ${gamma_flip}"
            })

    # ── 3. Iron Condor (if neutral/low score, positive GEX) ──
    if abs(score) <= 2 or gex_info.get("total_gex", 0) > 0:
        c_sell = snap(min(call_wall, spot + sigma_1d * 0.8))
        c_buy  = snap(c_sell + wing_w)
        p_sell = snap(max(put_wall, spot - sigma_1d * 0.8))
        p_buy  = snap(p_sell - wing_w)
        c_cred = bs_credit_approx(c_sell, c_buy, 'call')
        p_cred = bs_credit_approx(p_sell, p_buy, 'put')
        total_cred = round(c_cred + p_cred, 2)
        width = min(c_buy - c_sell, p_sell - p_buy)
        if total_cred > 0 and width > 0:
            pop = round(pop_between(p_sell, c_sell) * 100, 1)
            rr  = round(total_cred / max(0.01, width - total_cred), 2)
            conf = min(88, round(48 + pop * 0.3 - abs(score) * 3))
            suggestions.append({
                "strategy": "Iron Condor",
                "type": "credit",
                "direction": "neutral",
                "call_sell": c_sell, "call_buy": c_buy,
                "put_sell": p_sell, "put_buy": p_buy,
                "expiry": exp, "dte": dte,
                "credit": total_cred,
                "max_profit": round(total_cred * 100, 0),
                "max_loss": round((width - total_cred) * 100, 0),
                "upper_be": round(c_sell + total_cred, 2),
                "lower_be": round(p_sell - total_cred, 2),
                "pop": pop, "rr": rr, "confidence": conf,
                "rationale": f"Condor between ${p_sell}P–${c_sell}C. Pin at ${pin}.",
                "anchor": f"Pin ${pin} / Walls ${put_wall}-${call_wall}"
            })

    # ── 4. Directional debit (if strong signal) ──
    if abs(score) >= 3:
        if score < 0:  # bearish — long put
            atm_k  = snap(spot)
            otm_k  = snap(spot - sigma_1d * 1.2)
            buy_px = bs_credit_approx(atm_k, otm_k, 'put')  # debit spread
            width  = atm_k - otm_k
            if buy_px > 0 and width > 0:
                pop = round(pop_below(atm_k - buy_px) * 100, 1)
                rr  = round((width - buy_px) / max(0.01, buy_px), 2)
                conf = min(82, round(45 + abs(score) * 5))
                suggestions.append({
                    "strategy": "Put Debit Spread",
                    "type": "debit",
                    "direction": "bearish",
                    "buy_strike": atm_k, "sell_strike": otm_k,
                    "opt_type": "put",
                    "expiry": exp, "dte": dte,
                    "debit": round(buy_px, 2),
                    "max_profit": round((width - buy_px) * 100, 0),
                    "max_loss": round(buy_px * 100, 0),
                    "breakeven": round(atm_k - buy_px, 2),
                    "pop": pop, "rr": rr, "confidence": conf,
                    "rationale": f"Bearish score {score}. Target ${otm_k} = put wall ${put_wall}.",
                    "anchor": f"Put Wall ${put_wall}"
                })
        else:  # bullish — call debit
            atm_k  = snap(spot)
            otm_k  = snap(spot + sigma_1d * 1.2)
            buy_px = bs_credit_approx(atm_k, otm_k, 'call')
            width  = otm_k - atm_k
            if buy_px > 0 and width > 0:
                pop = round(pop_above(atm_k + buy_px) * 100, 1)
                rr  = round((width - buy_px) / max(0.01, buy_px), 2)
                conf = min(82, round(45 + abs(score) * 5))
                suggestions.append({
                    "strategy": "Call Debit Spread",
                    "type": "debit",
                    "direction": "bullish",
                    "buy_strike": atm_k, "sell_strike": otm_k,
                    "opt_type": "call",
                    "expiry": exp, "dte": dte,
                    "debit": round(buy_px, 2),
                    "max_profit": round((width - buy_px) * 100, 0),
                    "max_loss": round(buy_px * 100, 0),
                    "breakeven": round(atm_k + buy_px, 2),
                    "pop": pop, "rr": rr, "confidence": conf,
                    "rationale": f"Bullish score {score}. Target ${otm_k} = call wall ${call_wall}.",
                    "anchor": f"Call Wall ${call_wall}"
                })

    # Sort: highest confidence first
    suggestions.sort(key=lambda x: -x.get("confidence", 0))
    return suggestions

