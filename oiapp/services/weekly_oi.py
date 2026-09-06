"""Shared weekly option-OI utilities.

The Aggregate chart, Weekly Plan, AI Hub, and market-context panel should all
read option walls from the same strike-level snapshots.  This module keeps the
rules in one place:

* target-expiry pricing can use the requested expiry;
* weekly wall context aggregates the expiries in the active week;
* actionable walls are selected from strikes around spot, not deep stale walls;
* max pain is computed from strike-level OI only when the rows are available.
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..db import DB_PATH
from .oi_significance import build_oi_change_filter_context, oi_change_sig_flags


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def norm_option_type(value: Any) -> str:
    txt = str(value or "").strip().lower()
    if txt.startswith("c"):
        return "call"
    if txt.startswith("p"):
        return "put"
    return txt


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]).lower() for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def future_expirations(symbol: str, today: Optional[str] = None) -> List[str]:
    sym = (symbol or "").upper().strip()
    if not sym:
        return []
    today = today or date.today().isoformat()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration",
            (sym, today),
        ).fetchall()
        return [str(r["expiration"]) for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def weekly_expirations(
    symbol: str,
    target_expiry: Optional[str] = None,
    from_expiration: Optional[str] = None,
    count: Optional[int] = None,
    max_expiries: int = 7,
) -> List[str]:
    """Return the expiries that define the weekly aggregate window.

    This mirrors the Aggregate screen: start from the selected/first future
    expiration and take N expiries.  If a target Friday expiry is supplied, use
    every listed future expiry through that target, capped by ``max_expiries``.
    """
    exps = future_expirations(symbol)
    if not exps:
        return []
    start = 0
    if from_expiration and from_expiration in exps:
        start = exps.index(from_expiration)
    exps = exps[start:]

    target_dt = None
    if target_expiry:
        try:
            target_dt = date.fromisoformat(str(target_expiry)[:10])
        except Exception:
            target_dt = None
    if target_dt is not None:
        through: List[str] = []
        for e in exps:
            try:
                ed = date.fromisoformat(str(e)[:10])
            except Exception:
                continue
            if ed <= target_dt:
                through.append(e)
        if through:
            exps = through
        elif target_expiry in exps:
            exps = [target_expiry]
    elif count:
        exps = exps[: max(1, int(count))]

    cap = max(1, int(count or max_expiries or 7))
    if target_dt is not None:
        cap = max(cap, min(max_expiries, len(exps)))
    return exps[: min(len(exps), max(1, int(max_expiries or cap)), cap)]


def latest_option_rows(symbol: str, expiration: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Latest strike-level option rows for one expiration, with prior-day ΔOI."""
    sym = (symbol or "").upper().strip()
    exp = str(expiration or "")[:10]
    if not sym or not exp:
        return [], None
    con = _conn()
    try:
        cols = _table_columns(con, "options")
        date_rows = con.execute(
            "SELECT DISTINCT date FROM options WHERE symbol=? AND expiration=? ORDER BY date DESC LIMIT 2",
            (sym, exp),
        ).fetchall()
        if not date_rows:
            return [], None
        latest = str(date_rows[0]["date"])
        prev = str(date_rows[1]["date"]) if len(date_rows) > 1 else None

        def select_expr(name: str, expr: str, fallback: str = "NULL") -> str:
            return expr if name.lower() in cols else f"{fallback} AS {name}"

        select_cols = [
            "type",
            "strike",
            "SUM(oi) AS oi",
            select_expr("volume", "SUM(COALESCE(volume,0)) AS volume", "0"),
            select_expr("price", "CASE WHEN SUM(oi)>0 THEN SUM(COALESCE(price,0)*oi)/SUM(oi) ELSE AVG(price) END AS price"),
            select_expr("bid", "AVG(NULLIF(bid,0)) AS bid"),
            select_expr("ask", "AVG(NULLIF(ask,0)) AS ask"),
            select_expr("last", "AVG(NULLIF(last,0)) AS last"),
            select_expr("iv", "AVG(NULLIF(iv,0)) AS iv"),
            select_expr("underlying", "AVG(NULLIF(underlying,0)) AS underlying"),
        ]
        latest_rows = con.execute(
            f"""
            SELECT {', '.join(select_cols)}
            FROM options
            WHERE symbol=? AND expiration=? AND date=?
            GROUP BY type, strike
            HAVING SUM(oi)>0
            ORDER BY strike
            """,
            (sym, exp, latest),
        ).fetchall()

        prev_map: Dict[Tuple[str, float], int] = {}
        if prev:
            prev_rows = con.execute(
                """
                SELECT type, strike, SUM(oi) AS oi
                FROM options
                WHERE symbol=? AND expiration=? AND date=?
                GROUP BY type, strike
                """,
                (sym, exp, prev),
            ).fetchall()
            for r in prev_rows:
                typ = norm_option_type(r["type"])
                k = _safe_float(r["strike"], None)
                if typ in {"call", "put"} and k is not None:
                    prev_map[(typ, float(k))] = _safe_int(r["oi"], 0)

        out: List[Dict[str, Any]] = []
        for r in latest_rows:
            d = dict(r)
            typ = norm_option_type(d.get("type"))
            k = _safe_float(d.get("strike"), None)
            if typ not in {"call", "put"} or k is None:
                continue
            oi_now = _safe_int(d.get("oi"), 0)
            oi_prev = prev_map.get((typ, float(k)), 0) if prev_map else 0
            item = {
                "symbol": sym,
                "expiration": exp,
                "date": latest,
                "prev_date": prev,
                "type": typ,
                "strike": float(k),
                "oi": oi_now,
                "volume": _safe_int(d.get("volume"), 0),
                "vol": _safe_int(d.get("volume"), 0),
                "price": _safe_float(d.get("price"), None),
                "bid": _safe_float(d.get("bid"), None),
                "ask": _safe_float(d.get("ask"), None),
                "last": _safe_float(d.get("last"), None),
                "iv": _safe_float(d.get("iv"), None),
                "underlying": _safe_float(d.get("underlying"), None),
                "oi_change": oi_now - oi_prev if prev_map else 0,
                "prev_oi": oi_prev,
                "oi_change_pct": round((oi_now - oi_prev) / max(1, oi_prev) * 100.0, 2) if oi_prev else None,
            }
            out.append(item)
        return out, latest
    except Exception:
        return [], None
    finally:
        con.close()


def aggregate_option_rows(symbol: str, expirations: Sequence[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Sum strike-level rows across the supplied expirations."""
    sym = (symbol or "").upper().strip()
    agg: Dict[Tuple[str, float], Dict[str, Any]] = {}
    latest_dates: Dict[str, str] = {}
    used: List[str] = []
    for exp in expirations or []:
        rows, latest = latest_option_rows(sym, str(exp))
        if not rows:
            continue
        used.append(str(exp))
        if latest:
            latest_dates[str(exp)] = latest
        for r in rows:
            typ = norm_option_type(r.get("type"))
            k = _safe_float(r.get("strike"), None)
            if typ not in {"call", "put"} or k is None:
                continue
            key = (typ, float(k))
            cur = agg.setdefault(key, {
                "symbol": sym,
                "type": typ,
                "strike": float(k),
                "oi": 0,
                "volume": 0,
                "vol": 0,
                "oi_change": 0,
                "prev_oi": 0,
                "price_num": 0.0,
                "price_den": 0.0,
                "bid_vals": [],
                "ask_vals": [],
                "last_vals": [],
                "iv_vals": [],
                "underlying_vals": [],
                "expirations": [],
                "date": latest,
            })
            oi = _safe_int(r.get("oi"), 0)
            cur["oi"] += oi
            cur["volume"] += _safe_int(r.get("volume"), 0)
            cur["vol"] = cur["volume"]
            cur["oi_change"] += _safe_int(r.get("oi_change"), 0)
            cur["prev_oi"] += _safe_int(r.get("prev_oi"), 0)
            px = _safe_float(r.get("price"), None)
            if px is not None and px > 0 and oi > 0:
                cur["price_num"] += px * oi
                cur["price_den"] += oi
            for name, vals_key in (("bid", "bid_vals"), ("ask", "ask_vals"), ("last", "last_vals"), ("iv", "iv_vals"), ("underlying", "underlying_vals")):
                val = _safe_float(r.get(name), None)
                if val is not None and val > 0:
                    cur[vals_key].append(val)
            cur["expirations"].append(str(exp))
    out: List[Dict[str, Any]] = []
    for item in agg.values():
        den = item.pop("price_den", 0.0)
        num = item.pop("price_num", 0.0)
        item["price"] = round(num / den, 4) if den > 0 else None
        for vals_key, out_key in (("bid_vals", "bid"), ("ask_vals", "ask"), ("last_vals", "last"), ("iv_vals", "iv"), ("underlying_vals", "underlying")):
            vals = item.pop(vals_key, [])
            item[out_key] = round(sum(vals) / len(vals), 4) if vals else None
        item["oi_change_pct"] = round(item["oi_change"] / max(1, item["prev_oi"]) * 100.0, 2) if item.get("prev_oi") else None
        out.append(item)
    out.sort(key=lambda r: (norm_option_type(r.get("type")), _safe_float(r.get("strike"), 0.0) or 0.0))
    return out, {"expirations": used, "latest_dates": latest_dates, "used_expiries": len(used)}


def estimate_spot_from_db(symbol: str) -> Optional[float]:
    sym = (symbol or "").upper().strip()
    if not sym:
        return None
    con = _conn()
    try:
        # Prefer the app's cached spot/close if available.
        cols = _table_columns(con, "price_cache")
        if "close" in cols:
            row = con.execute(
                "SELECT close FROM price_cache WHERE symbol=? AND close>0 ORDER BY date DESC LIMIT 1",
                (sym,),
            ).fetchone()
            px = _safe_float(row["close"], None) if row else None
            if px and px > 0:
                return round(px, 4)
    except Exception:
        pass
    finally:
        con.close()
    # Network/live fallback is optional and may be cached elsewhere.
    try:
        from .market import get_spot
        px = _safe_float(get_spot(sym), None)
        if px and px > 0:
            return round(px, 4)
    except Exception:
        pass
    return None


def strike_interval_from_rows(rows: Sequence[Dict[str, Any]], spot: Optional[float] = None) -> float:
    vals = sorted({_safe_float(r.get("strike"), None) for r in rows or [] if _safe_float(r.get("strike"), None) is not None})
    if len(vals) < 2:
        return 5.0 if (spot or 0.0) >= 100 else 1.0
    diffs = [round(vals[i + 1] - vals[i], 4) for i in range(len(vals) - 1) if vals[i + 1] > vals[i]]
    if not diffs:
        return 5.0 if (spot or 0.0) >= 100 else 1.0
    diffs.sort()
    return max(0.01, float(diffs[max(0, len(diffs) // 4)]))


def select_strikes_around_spot(strikes: Sequence[float], spot: Optional[float], per_side: int = 12) -> List[float]:
    vals = sorted({float(x) for x in strikes if x is not None})
    if not vals:
        return []
    if not spot or spot <= 0:
        mid = len(vals) // 2
    else:
        mid = min(range(len(vals)), key=lambda i: abs(vals[i] - float(spot)))
    side = max(1, int(per_side or 12))
    lo = max(0, mid - side)
    hi = min(len(vals), mid + side + 1)
    return vals[lo:hi]


def filter_rows_to_strikes(rows: Sequence[Dict[str, Any]], strikes: Iterable[float]) -> List[Dict[str, Any]]:
    allowed = {round(float(x), 6) for x in strikes if x is not None}
    if not allowed:
        return list(rows or [])
    return [dict(r) for r in (rows or []) if round(_safe_float(r.get("strike"), -999999.0) or -999999.0, 6) in allowed]


def filter_rows_by_band(rows: Sequence[Dict[str, Any]], spot: Optional[float], band: Optional[float]) -> List[Dict[str, Any]]:
    if not spot or spot <= 0 or not band or band <= 0:
        return list(rows or [])
    lo, hi = float(spot) - float(band), float(spot) + float(band)
    return [dict(r) for r in (rows or []) if (lo <= (_safe_float(r.get("strike"), -1e9) or -1e9) <= hi)]


def totals_by_side(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    calls = sum(_safe_int(r.get("oi"), 0) for r in rows or [] if norm_option_type(r.get("type")) == "call")
    puts = sum(_safe_int(r.get("oi"), 0) for r in rows or [] if norm_option_type(r.get("type")) == "put")
    return {"call_oi": calls, "put_oi": puts, "total_oi": calls + puts, "pcr": round(puts / max(1, calls), 3)}


def max_pain_from_rows(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
    by_strike: Dict[float, Dict[str, float]] = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    for r in rows or []:
        typ = norm_option_type(r.get("type"))
        k = _safe_float(r.get("strike"), None)
        if typ not in {"call", "put"} or k is None:
            continue
        by_strike[float(k)][typ] += _safe_float(r.get("oi"), 0.0) or 0.0
    strikes = sorted(by_strike)
    if not strikes:
        return None
    best_k = None
    best_pain = None
    for settle in strikes:
        pain = 0.0
        for k, vals in by_strike.items():
            pain += vals.get("call", 0.0) * max(0.0, settle - k)
            pain += vals.get("put", 0.0) * max(0.0, k - settle)
        if best_pain is None or pain < best_pain:
            best_k, best_pain = settle, pain
    return round(float(best_k), 2) if best_k is not None else None


def option_mid(row: Dict[str, Any]) -> Optional[float]:
    bid = _safe_float((row or {}).get("bid"), None)
    ask = _safe_float((row or {}).get("ask"), None)
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        return round((bid + ask) / 2.0, 4)
    for key in ("price", "last"):
        px = _safe_float((row or {}).get(key), None)
        if px is not None and px > 0:
            return round(px, 4)
    return None


def top_walls(rows: Sequence[Dict[str, Any]], opt_type: str, spot: Optional[float], limit: int = 6) -> List[Dict[str, Any]]:
    side = "put" if norm_option_type(opt_type) == "put" else "call"
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        typ = norm_option_type(r.get("type"))
        if typ != side:
            continue
        k = _safe_float(r.get("strike"), None)
        oi = _safe_int(r.get("oi"), 0)
        if k is None or oi <= 0:
            continue
        if spot and spot > 0:
            if side == "put" and k > spot:
                continue
            if side == "call" and k < spot:
                continue
        out.append({
            "type": side,
            "strike": round(float(k), 2),
            "oi": oi,
            "oi_change": _safe_int(r.get("oi_change"), 0),
            "volume": _safe_int(r.get("volume", r.get("vol")), 0),
            "distance_pct": round(abs(float(k) - float(spot or k)) / max(0.01, float(spot or k)) * 100.0, 2) if spot else None,
            "mid": option_mid(r),
            "expirations": list(r.get("expirations") or ([r.get("expiration")] if r.get("expiration") else [])),
        })
    out.sort(key=lambda x: (-x["oi"], x.get("distance_pct") if x.get("distance_pct") is not None else 999.0))
    return out[: max(1, int(limit or 6))]


def aggregate_strike_payload(symbol: str, from_expiration: Optional[str], count: int, per_side: int, min_change_pct: Optional[float] = None) -> Dict[str, Any]:
    sym = (symbol or "").upper().strip()
    exps = weekly_expirations(sym, target_expiry=None, from_expiration=from_expiration, count=count, max_expiries=max(1, int(count or 1)))
    if not exps:
        return {"symbol": sym, "expirations": [], "strikes": [], "call_sum": [], "put_sum": [], "call_vol_sum": [], "put_vol_sum": [], "message": "No future expiry data — run Scheduler"}
    spot = estimate_spot_from_db(sym)
    rows, meta = aggregate_option_rows(sym, exps)
    strikes = select_strikes_around_spot([r["strike"] for r in rows], spot, per_side)
    selected = filter_rows_to_strikes(rows, strikes)
    call_oi = defaultdict(int); put_oi = defaultdict(int)
    call_vol = defaultdict(int); put_vol = defaultdict(int)
    call_chg = defaultdict(int); put_chg = defaultdict(int)
    call_prev = defaultdict(int); put_prev = defaultdict(int)
    call_sig = {}; put_sig = {}
    oi_sig_ctx = build_oi_change_filter_context(sym, rows, expiry=", ".join(exps), source="aggregate_screen", min_change_pct=min_change_pct)
    for r in selected:
        k = float(r["strike"])
        if norm_option_type(r.get("type")) == "call":
            call_oi[k] += _safe_int(r.get("oi"), 0)
            call_vol[k] += _safe_int(r.get("volume"), 0)
            call_chg[k] += _safe_int(r.get("oi_change"), 0)
            call_prev[k] += _safe_int(r.get("prev_oi"), 0)
        elif norm_option_type(r.get("type")) == "put":
            put_oi[k] += _safe_int(r.get("oi"), 0)
            put_vol[k] += _safe_int(r.get("volume"), 0)
            put_chg[k] += _safe_int(r.get("oi_change"), 0)
            put_prev[k] += _safe_int(r.get("prev_oi"), 0)
    for s in strikes:
        call_sig[s] = oi_change_sig_flags(call_oi[s], call_prev[s], call_chg[s], oi_sig_ctx)
        put_sig[s] = oi_change_sig_flags(put_oi[s], put_prev[s], put_chg[s], oi_sig_ctx)
    significant = []
    for s in strikes:
        if call_sig[s].get("significant"):
            significant.append({"type":"call", "strike":s, "oi":call_oi[s], "prev_oi":call_prev[s], "oi_change":call_chg[s], "oi_change_pct":call_sig[s].get("pct"), "reason":call_sig[s].get("reason"), "direction":call_sig[s].get("direction")})
        if put_sig[s].get("significant"):
            significant.append({"type":"put", "strike":s, "oi":put_oi[s], "prev_oi":put_prev[s], "oi_change":put_chg[s], "oi_change_pct":put_sig[s].get("pct"), "reason":put_sig[s].get("reason"), "direction":put_sig[s].get("direction")})
    significant.sort(key=lambda x: abs(_safe_int(x.get("oi_change"), 0)), reverse=True)
    return {
        "symbol": sym,
        "spot": spot,
        "expirations": exps,
        "latest_dates": meta.get("latest_dates", {}),
        "strikes": strikes,
        "call_sum": [call_oi[s] for s in strikes],
        "put_sum": [put_oi[s] for s in strikes],
        "call_vol_sum": [call_vol[s] for s in strikes],
        "put_vol_sum": [put_vol[s] for s in strikes],
        "call_change_sum": [call_chg[s] for s in strikes],
        "put_change_sum": [put_chg[s] for s in strikes],
        "call_change_pct": [call_sig[s].get("pct") for s in strikes],
        "put_change_pct": [put_sig[s].get("pct") for s in strikes],
        "call_oi_change_significant": [bool(call_sig[s].get("significant")) for s in strikes],
        "put_oi_change_significant": [bool(put_sig[s].get("significant")) for s in strikes],
        "significant_oi_changes": significant[:20],
        "oi_change_filter": oi_sig_ctx,
        "source": "weekly_oi.aggregate_strike_payload",
    }


def build_weekly_oi_context(
    symbol: str,
    target_expiry: Optional[str],
    spot: Optional[float] = None,
    from_expiration: Optional[str] = None,
    count: Optional[int] = None,
    per_side: int = 12,
    max_expiries: int = 7,
) -> Dict[str, Any]:
    """Return weekly aggregate OI context that matches the Aggregate screen."""
    sym = (symbol or "").upper().strip()
    spot = _safe_float(spot, None) or estimate_spot_from_db(sym)
    exps = weekly_expirations(sym, target_expiry=target_expiry, from_expiration=from_expiration, count=count, max_expiries=max_expiries)
    target_rows: List[Dict[str, Any]] = []
    target_date = None
    if target_expiry:
        target_rows, target_date = latest_option_rows(sym, str(target_expiry))
    agg_rows, meta = aggregate_option_rows(sym, exps)
    if not agg_rows and target_rows:
        agg_rows = list(target_rows)
        meta = {"expirations": [target_expiry], "latest_dates": {str(target_expiry): target_date}, "used_expiries": 1}
    all_strikes = sorted({_safe_float(r.get("strike"), None) for r in agg_rows if _safe_float(r.get("strike"), None) is not None})
    selected_strikes = select_strikes_around_spot(all_strikes, spot, per_side)
    actionable_rows = filter_rows_to_strikes(agg_rows, selected_strikes)
    target_actionable_rows = filter_rows_to_strikes(target_rows, selected_strikes)
    # Strike-count filtering matches the Aggregate screen.  Add a safety band so
    # sparse chains or very large deep OI cannot re-enter weekly strategy walls.
    if spot and spot > 0 and agg_rows:
        interval = strike_interval_from_rows(agg_rows, spot)
        # Weekly strategy walls must be actionable near current price.  The
        # Aggregate screen may display the selected strike window, but a far
        # strike with large raw OI (for example SPY 800C or 565P when spot is
        # ~747) should remain diagnostic only.  Use a DTE-aware percent band
        # plus strike-spacing band and take the tighter one.
        dte_hint = None
        try:
            if target_expiry:
                dte_hint = max(1, (date.fromisoformat(str(target_expiry)[:10]) - date.today()).days)
        except Exception:
            dte_hint = None
        pct_band = 0.040 if (dte_hint is not None and dte_hint <= 7) else 0.065 if (dte_hint is not None and dte_hint <= 21) else 0.090
        pct_abs_band = float(spot) * pct_band
        strike_abs_band = float(interval or 1.0) * max(8.0, float(per_side or 12) * 1.2)
        band = max(float(interval or 1.0) * 4.0, min(pct_abs_band, strike_abs_band))
        actionable_rows = filter_rows_by_band(actionable_rows, spot, band)
        target_actionable_rows = filter_rows_by_band(target_actionable_rows, spot, band)
        selected_strikes = [k for k in selected_strikes if abs(float(k) - float(spot)) <= band]
    totals_all = totals_by_side(agg_rows)
    totals_actionable = totals_by_side(actionable_rows)
    return {
        "symbol": sym,
        "target_expiry": target_expiry,
        "spot": spot,
        "expirations": meta.get("expirations") or exps,
        "latest_dates": meta.get("latest_dates") or {},
        "target_snapshot_date": target_date,
        "aggregate_rows": agg_rows,
        "target_rows": target_rows,
        "actionable_rows": actionable_rows,
        "target_actionable_rows": target_actionable_rows,
        "selected_strikes": selected_strikes,
        "totals_all": totals_all,
        "totals_actionable": totals_actionable,
        "pcr": totals_actionable.get("pcr") if totals_actionable.get("total_oi") else totals_all.get("pcr"),
        "pcr_all": totals_all.get("pcr"),
        "pcr_actionable": totals_actionable.get("pcr"),
        "call_walls": top_walls(actionable_rows, "call", spot, limit=8),
        "put_walls": top_walls(actionable_rows, "put", spot, limit=8),
        "raw_call_walls": top_walls(agg_rows, "call", spot, limit=8),
        "raw_put_walls": top_walls(agg_rows, "put", spot, limit=8),
        "target_max_pain": max_pain_from_rows(target_rows),
        "aggregate_max_pain": max_pain_from_rows(agg_rows),
        "actionable_max_pain": max_pain_from_rows(actionable_rows),
        "source": "weekly_oi_context",
        "method": "aggregate listed expiries through target; actionable walls use strikes around spot matching Aggregate screen",
    }
