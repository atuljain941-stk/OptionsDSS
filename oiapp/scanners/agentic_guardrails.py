# oiapp/scanners/agentic_guardrails.py
"""
Agentic trade-quality guardrails.

These checks run after UAE/chain scoring and before a background Agentic AI
finding is allowed to alert.  They prevent high RS/trend scores from producing
an OPEN alert when the selected short strike is thin, cumulative OI conflicts
with the proposed trade, price is stretched, or earnings fall inside the holding
window.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

ETF_INDEX_PROXIES = {
    "SPY", "SPX", "XSP", "QQQ", "IWM", "DIA", "RSP",
    "XLK", "XLF", "XLE", "XLY", "XLV", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC",
    "SMH", "SOXX", "IBB", "XBI", "GLD", "SLV", "TLT", "HYG", "LQD", "EEM", "EFA",
}


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return con


def _safe_float(v: Any, default: Optional[float] = None, ndigits: Optional[int] = None) -> Optional[float]:
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


def _norm_type(value: Any) -> str:
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


def _latest_option_rows(symbol: str, expiry: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    sym = (symbol or "").upper().strip()
    if not sym or not expiry:
        return [], None
    con = _conn()
    try:
        cols = _table_columns(con, "options")
        if not cols:
            return [], None
        drow = con.execute("SELECT MAX(date) AS d FROM options WHERE symbol=? AND expiration=?", (sym, expiry)).fetchone()
        latest = drow["d"] if drow else None
        if not latest:
            return [], None
        type_col = "type" if "type" in cols else "option_type" if "option_type" in cols else "type"
        strike_col = "strike"
        oi_expr = "SUM(COALESCE(oi,0)) AS oi" if "oi" in cols else "SUM(COALESCE(openInterest,0)) AS oi" if "openinterest" in cols else "0 AS oi"
        vol_expr = "SUM(COALESCE(volume,0)) AS volume" if "volume" in cols else "0 AS volume"
        price_expr = "CASE WHEN SUM(COALESCE(oi,0))>0 THEN SUM(COALESCE(price,0)*COALESCE(oi,0))/SUM(COALESCE(oi,0)) ELSE AVG(price) END AS price" if "price" in cols and "oi" in cols else "NULL AS price"
        rows = con.execute(
            f"""
            SELECT {type_col} AS type, {strike_col} AS strike, {oi_expr}, {vol_expr}, {price_expr}
            FROM options
            WHERE symbol=? AND expiration=? AND date=?
            GROUP BY {type_col}, {strike_col}
            HAVING oi>0
            ORDER BY {strike_col}
            """,
            (sym, expiry, latest),
        ).fetchall()
        return [dict(r) for r in rows], str(latest)
    except Exception:
        return [], None
    finally:
        con.close()


def _aggregate_rows_through_expiry(symbol: str, target_expiry: str, max_expiries: int = 24) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    sym = (symbol or "").upper().strip()
    if not sym or not target_expiry:
        return [], {"source": "aggregate_through_expiry", "expirations": [], "latest_dates": {}}
    try:
        target_dt = date.fromisoformat(str(target_expiry)[:10])
    except Exception:
        target_dt = None
    today_s = date.today().isoformat()
    con = _conn()
    try:
        cols = _table_columns(con, "options")
        if not cols:
            return [], {"source": "aggregate_through_expiry", "expirations": [], "latest_dates": {}}
        type_col = "type" if "type" in cols else "option_type" if "option_type" in cols else "type"
        erows = con.execute(
            "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration",
            (sym, today_s),
        ).fetchall()
        exps: List[str] = []
        for r in erows:
            exp = str(r["expiration"] or "")
            try:
                ed = date.fromisoformat(exp[:10])
            except Exception:
                continue
            if target_dt is None or ed <= target_dt:
                exps.append(exp)
        if not exps and target_expiry:
            exps = [target_expiry]
        exps = exps[: max(1, int(max_expiries or 24))]
        agg: Dict[Tuple[str, float], Dict[str, Any]] = {}
        latest_dates: Dict[str, str] = {}
        for exp in exps:
            drow = con.execute("SELECT MAX(date) AS d FROM options WHERE symbol=? AND expiration=?", (sym, exp)).fetchone()
            latest = drow["d"] if drow else None
            if not latest:
                continue
            latest_dates[exp] = str(latest)
            oi_expr = "SUM(COALESCE(oi,0)) AS oi" if "oi" in cols else "SUM(COALESCE(openInterest,0)) AS oi" if "openinterest" in cols else "0 AS oi"
            vol_expr = "SUM(COALESCE(volume,0)) AS volume" if "volume" in cols else "0 AS volume"
            price_expr = "CASE WHEN SUM(COALESCE(oi,0))>0 THEN SUM(COALESCE(price,0)*COALESCE(oi,0))/SUM(COALESCE(oi,0)) ELSE AVG(price) END AS price" if "price" in cols and "oi" in cols else "NULL AS price"
            rows = con.execute(
                f"""
                SELECT {type_col} AS type, strike, {oi_expr}, {vol_expr}, {price_expr}
                FROM options
                WHERE symbol=? AND expiration=? AND date=?
                GROUP BY {type_col}, strike
                HAVING oi>0
                """,
                (sym, exp, latest),
            ).fetchall()
            for rr in rows:
                typ = _norm_type(rr["type"])
                k = _safe_float(rr["strike"], None)
                if typ not in {"call", "put"} or k is None:
                    continue
                key = (typ, float(k))
                item = agg.setdefault(key, {"type": typ, "strike": float(k), "oi": 0, "volume": 0, "expirations": []})
                item["oi"] += _safe_int(rr["oi"], 0)
                item["volume"] += _safe_int(rr["volume"], 0)
                item["expirations"].append(exp)
        out = list(agg.values())
        out.sort(key=lambda r: (_norm_type(r.get("type")), _safe_float(r.get("strike"), 0) or 0))
        return out, {"source": "aggregate_through_expiry", "expirations": exps, "latest_dates": latest_dates, "used_expiries": len(latest_dates), "target_expiry": target_expiry}
    except Exception as exc:
        return [], {"source": "aggregate_through_expiry", "expirations": [], "latest_dates": {}, "error": str(exc)[:180]}
    finally:
        con.close()


def _max_pain(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
    by_strike: Dict[float, Dict[str, float]] = {}
    for r in rows or []:
        k = _safe_float(r.get("strike"), None)
        typ = _norm_type(r.get("type"))
        if k is None or typ not in {"call", "put"}:
            continue
        by_strike.setdefault(k, {"call": 0.0, "put": 0.0})[typ] += _safe_float(r.get("oi"), 0.0) or 0.0
    strikes = sorted(by_strike)
    best_k, best_pain = None, None
    for settle in strikes:
        pain = 0.0
        for k, vals in by_strike.items():
            pain += vals.get("call", 0.0) * max(0.0, settle - k)
            pain += vals.get("put", 0.0) * max(0.0, k - settle)
        if best_pain is None or pain < best_pain:
            best_k, best_pain = settle, pain
    return round(float(best_k), 2) if best_k is not None else None


def _top_walls(rows: Sequence[Dict[str, Any]], side: str, spot: Optional[float], limit: int = 6) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        if _norm_type(r.get("type")) != side:
            continue
        k = _safe_float(r.get("strike"), None)
        oi = _safe_int(r.get("oi"), 0)
        if k is None or oi <= 0:
            continue
        if spot and side == "put" and k > spot:
            continue
        if spot and side == "call" and k < spot:
            continue
        out.append({"type": side, "strike": round(k, 2), "oi": oi, "distance_pct": round(abs(k - spot) / max(spot, 0.01) * 100.0, 2) if spot else None})
    out.sort(key=lambda x: (-x["oi"], x.get("distance_pct") if x.get("distance_pct") is not None else 999.0))
    return out[:limit]


def _oi_at(rows: Sequence[Dict[str, Any]], side: str, strike: Optional[float]) -> int:
    if strike is None:
        return 0
    best_oi = 0
    best_dist = 10 ** 9
    for r in rows or []:
        if _norm_type(r.get("type")) != side:
            continue
        k = _safe_float(r.get("strike"), None)
        if k is None:
            continue
        d = abs(k - float(strike))
        if d < best_dist:
            best_dist = d
            best_oi = _safe_int(r.get("oi"), 0)
    return best_oi


def _side_stats(rows: Sequence[Dict[str, Any]], side: str, spot: Optional[float]) -> Dict[str, Any]:
    total = max_oi = directional = 0
    for r in rows or []:
        if _norm_type(r.get("type")) != side:
            continue
        k = _safe_float(r.get("strike"), None)
        oi = _safe_int(r.get("oi"), 0)
        total += oi
        max_oi = max(max_oi, oi)
        if spot and k is not None:
            if side == "put" and k <= spot:
                directional += oi
            elif side == "call" and k >= spot:
                directional += oi
    return {"total_oi": total, "max_oi": max_oi, "directional_oi": directional}


def _ema_last(values: Sequence[float], period: int) -> Optional[float]:
    vals = [float(x) for x in values if x is not None]
    if not vals:
        return None
    alpha = 2.0 / (period + 1.0)
    ema = vals[0]
    for v in vals[1:]:
        ema = alpha * v + (1.0 - alpha) * ema
    return ema


def _frame_to_records(df: Any) -> List[Dict[str, float]]:
    if df is None or getattr(df, "empty", True):
        return []
    out: List[Dict[str, float]] = []
    try:
        for _, r in df.iterrows():
            close = _safe_float(r.get("Close"), None)
            if close is None or close <= 0:
                continue
            out.append({
                "open": _safe_float(r.get("Open"), close) or close,
                "high": _safe_float(r.get("High"), close) or close,
                "low": _safe_float(r.get("Low"), close) or close,
                "close": close,
                "volume": _safe_float(r.get("Volume"), 0.0) or 0.0,
            })
    except Exception:
        return []
    return out


def _bb_kc_profile(rows: Sequence[Dict[str, float]], label: str) -> Dict[str, Any]:
    rows = list(rows or [])
    if len(rows) < 22:
        return {"timeframe": label, "available": False, "note": "not enough bars"}
    closes = [_safe_float(r.get("close"), 0.0) or 0.0 for r in rows]
    highs = [_safe_float(r.get("high"), closes[i]) or closes[i] for i, r in enumerate(rows)]
    lows = [_safe_float(r.get("low"), closes[i]) or closes[i] for i, r in enumerate(rows)]
    last = closes[-1]
    bb_widths: List[float] = []
    bb_mid = bb_upper = bb_lower = None
    for i in range(19, len(closes)):
        sl = closes[i - 19:i + 1]
        m = sum(sl) / 20.0
        sd = math.sqrt(sum((x - m) ** 2 for x in sl) / 20.0)
        up, lo = m + 2.0 * sd, m - 2.0 * sd
        if m > 0:
            bb_widths.append((up - lo) / m * 100.0)
        if i == len(closes) - 1:
            bb_mid, bb_upper, bb_lower = m, up, lo
    trs = []
    for i in range(len(closes)):
        prev = closes[i - 1] if i > 0 else closes[i]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    atr20 = sum(trs[-20:]) / 20.0 if len(trs) >= 20 else sum(trs) / max(1, len(trs))
    ema20 = _ema_last(closes, 20) or bb_mid or last
    kc_upper = ema20 + 1.5 * atr20
    kc_lower = ema20 - 1.5 * atr20
    bb_pct = (last - bb_lower) / max(1e-9, bb_upper - bb_lower) * 100.0 if bb_upper and bb_lower and bb_upper != bb_lower else 50.0
    bbw = bb_widths[-1] if bb_widths else 0.0
    bbw_rank = round(sum(1 for w in bb_widths if w <= bbw) / max(1, len(bb_widths)) * 100.0, 1) if bb_widths else None
    prior = rows[-21:-1]
    prior_high = max((_safe_float(r.get("high"), 0.0) or 0.0) for r in prior) if prior else None
    prior_lows = [_safe_float(r.get("low"), None) for r in prior]
    prior_lows = [x for x in prior_lows if x is not None and x > 0]
    prior_low = min(prior_lows) if prior_lows else None
    bb_inside_kc = bool(bb_upper is not None and bb_lower is not None and bb_upper < kc_upper and bb_lower > kc_lower)
    kc_inside_bb = bool(bb_upper is not None and bb_lower is not None and kc_upper < bb_upper and kc_lower > bb_lower)
    close_above_upper = bool(bb_upper is not None and last >= bb_upper * 0.995)
    close_below_lower = bool(bb_lower is not None and last <= bb_lower * 1.005)
    near_upper = bool(bb_pct >= 80 or close_above_upper or (prior_high and last >= prior_high * 0.985))
    near_lower = bool(bb_pct <= 20 or close_below_lower or (prior_low and last <= prior_low * 1.015))
    extended_up = bool(bb_pct >= 92 or close_above_upper)
    extended_down = bool(bb_pct <= 8 or close_below_lower)
    state = "bb_expanded_kc_inside" if kc_inside_bb else "squeeze_on" if bb_inside_kc else "neutral_volatility"
    return {
        "timeframe": label, "available": True, "close": round(last, 2),
        "bb_upper": round(bb_upper, 2) if bb_upper is not None else None,
        "bb_lower": round(bb_lower, 2) if bb_lower is not None else None,
        "bb_mid": round(bb_mid, 2) if bb_mid is not None else None,
        "bb_pct": round(bb_pct, 1), "bb_width_rank": bbw_rank,
        "kc_upper": round(kc_upper, 2), "kc_lower": round(kc_lower, 2),
        "bb_inside_kc": bb_inside_kc, "kc_inside_bb": kc_inside_bb,
        "squeeze_state": state, "prior_high": round(prior_high, 2) if prior_high else None,
        "prior_low": round(prior_low, 2) if prior_low else None,
        "near_upper": near_upper, "near_lower": near_lower,
        "extended_up": extended_up, "extended_down": extended_down,
        "note": f"{label}: {state}, BB% {bb_pct:.0f}, width-rank {bbw_rank if bbw_rank is not None else 'n/a'}",
    }


def price_action_context(frames: Dict[str, Any]) -> Dict[str, Any]:
    daily = _bb_kc_profile(_frame_to_records((frames or {}).get("1d")), "1D")
    weekly = _bb_kc_profile(_frame_to_records((frames or {}).get("1wk")), "1W")
    notes = [x.get("note") for x in (daily, weekly) if x.get("available") and x.get("note")]
    upper_extension_score = lower_extension_score = range_score = 0
    for prof, wt in [(daily, 1), (weekly, 2)]:
        if not prof.get("available"):
            continue
        if prof.get("extended_up"):
            upper_extension_score += 2 * wt
        elif prof.get("near_upper"):
            upper_extension_score += 1 * wt
        if prof.get("extended_down"):
            lower_extension_score += 2 * wt
        elif prof.get("near_lower"):
            lower_extension_score += 1 * wt
        if prof.get("kc_inside_bb"):
            range_score += 2 * wt
        elif not prof.get("bb_inside_kc"):
            range_score += 1 * wt
    return {
        "available": bool(daily.get("available") or weekly.get("available")),
        "daily": daily,
        "weekly": weekly,
        "upper_extension_score": upper_extension_score,
        "lower_extension_score": lower_extension_score,
        "range_score": range_score,
        "range_premium_ok": bool(range_score >= 4 and upper_extension_score <= 4 and lower_extension_score <= 4),
        "summary": "; ".join(notes[:4]) if notes else "Price-action context unavailable.",
        "notes": notes[:6],
    }


def option_context(symbol: str, expiry: str, spot: Optional[float], trade: Dict[str, Any]) -> Dict[str, Any]:
    rows, latest_date = _latest_option_rows(symbol, expiry)
    agg_rows, agg_meta = _aggregate_rows_through_expiry(symbol, expiry)
    wall_rows = agg_rows or rows
    call_stats = _side_stats(wall_rows, "call", spot)
    put_stats = _side_stats(wall_rows, "put", spot)
    call_oi = call_stats["total_oi"]
    put_oi = put_stats["total_oi"]
    pcr = round(put_oi / max(1, call_oi), 2)
    top_calls = _top_walls(wall_rows, "call", spot, limit=6)
    top_puts = _top_walls(wall_rows, "put", spot, limit=6)
    target_top_calls = _top_walls(rows, "call", spot, limit=5)
    target_top_puts = _top_walls(rows, "put", spot, limit=5)
    ttype = str((trade or {}).get("trade_type") or "").upper()
    short_legs: List[Dict[str, Any]] = []
    if ttype == "PS":
        short_legs.append({"side": "put", "strike": _safe_float(trade.get("sell_strike"), None)})
    elif ttype == "CS":
        short_legs.append({"side": "call", "strike": _safe_float(trade.get("sell_strike"), None)})
    elif ttype == "IC":
        short_legs.append({"side": "put", "strike": _safe_float(trade.get("put_sell"), None)})
        short_legs.append({"side": "call", "strike": _safe_float(trade.get("call_sell"), None)})
    for leg in short_legs:
        side = leg.get("side")
        strike = _safe_float(leg.get("strike"), None)
        leg["target_oi"] = _oi_at(rows, side, strike)
        leg["aggregate_oi"] = _oi_at(wall_rows, side, strike)
        stats = put_stats if side == "put" else call_stats
        leg["side_max_oi"] = stats.get("max_oi")
        leg["side_total_oi"] = stats.get("total_oi")
        leg["oi_share_of_max"] = round((leg["aggregate_oi"] or 0) / max(1, leg["side_max_oi"] or 0), 3)
    directional_call_oi = call_stats.get("directional_oi") or 0
    directional_put_oi = put_stats.get("directional_oi") or 0
    call_to_put_pressure = round(directional_call_oi / max(1, directional_put_oi), 2)
    put_to_call_pressure = round(directional_put_oi / max(1, directional_call_oi), 2)
    return {
        "available": bool(rows or wall_rows),
        "latest_date": latest_date,
        "aggregate_meta": agg_meta,
        "target_rows": len(rows),
        "aggregate_rows": len(wall_rows),
        "call_oi": call_oi,
        "put_oi": put_oi,
        "total_oi": call_oi + put_oi,
        "pcr": pcr,
        "call_max_oi": call_stats.get("max_oi"),
        "put_max_oi": put_stats.get("max_oi"),
        "call_oi_above_spot": directional_call_oi,
        "put_oi_below_spot": directional_put_oi,
        "call_to_put_pressure": call_to_put_pressure,
        "put_to_call_pressure": put_to_call_pressure,
        "top_call_walls": top_calls,
        "top_put_walls": top_puts,
        "target_top_call_walls": target_top_calls,
        "target_top_put_walls": target_top_puts,
        "target_max_pain": _max_pain(rows),
        "aggregate_max_pain": _max_pain(wall_rows),
        "short_legs": short_legs,
        "summary": (
            f"Aggregate-to-expiry OI: calls {call_oi:,}, puts {put_oi:,}, PCR {pcr}; "
            f"top calls {', '.join(str(x['strike']) for x in top_calls[:3]) or 'n/a'}; "
            f"top puts {', '.join(str(x['strike']) for x in top_puts[:3]) or 'n/a'}."
        ),
    }


def _cap_recommendation(current: str, confidence: int, cap: str) -> str:
    order = {"AVOID": 0, "WATCH": 1, "OPEN_SMALL": 2, "OPEN": 3}
    cur = str(current or "WATCH").upper()
    capv = str(cap or "OPEN").upper()
    capped = cur if order.get(cur, 1) <= order.get(capv, 3) else capv
    if confidence < 72:
        capped = "WATCH"
    elif confidence < 80 and capped == "OPEN":
        capped = "OPEN_SMALL"
    return capped


def evaluate_agentic_guardrails(symbol: str, direction: str, trade: Dict[str, Any], frames: Dict[str, Any], spot: Optional[float], earn_days: Optional[int] = None) -> Dict[str, Any]:
    sym = (symbol or "").upper().strip()
    ttype = str((trade or {}).get("trade_type") or "").upper()
    expiry = str((trade or {}).get("expiry") or "")[:10]
    dte = _safe_int((trade or {}).get("dte") or (trade or {}).get("actual_dte"), 0)
    px = _safe_float(spot or (trade or {}).get("spot"), None)
    price_ctx = price_action_context(frames or {})
    opt_ctx = option_context(sym, expiry, px, trade or {}) if expiry else {"available": False, "summary": "No expiry for option context."}

    blockers: List[str] = []
    warnings: List[str] = []
    positives: List[str] = []
    confidence_cap = 100
    score_adjust = 0
    recommendation_cap = "OPEN"

    credit_trade = ttype in {"PS", "CS", "IC"}
    if credit_trade:
        for leg in opt_ctx.get("short_legs") or []:
            side = leg.get("side")
            strike = leg.get("strike")
            effective_oi = max(_safe_int(leg.get("aggregate_oi"), 0), _safe_int(leg.get("target_oi"), 0))
            side_max = _safe_int(leg.get("side_max_oi"), 0)
            share = _safe_float(leg.get("oi_share_of_max"), 0.0) or 0.0
            label = f"{strike:g}{'P' if side == 'put' else 'C'}" if strike is not None else str(side)
            dynamic_floor = max(10, int(side_max * 0.10)) if side_max > 0 else 25
            if effective_oi < 25:
                blockers.append(f"Short strike {label} has only {effective_oi:,} OI; too thin for an Agentic alert.")
                confidence_cap = min(confidence_cap, 68)
                score_adjust -= 18
                recommendation_cap = "WATCH"
            elif effective_oi < 75:
                warnings.append(f"Short strike {label} OI is light ({effective_oi:,}); use only small/watch sizing unless OI builds.")
                confidence_cap = min(confidence_cap, 76)
                score_adjust -= 7
                recommendation_cap = "OPEN_SMALL"
            if side_max > 0 and effective_oi < dynamic_floor:
                warnings.append(f"Short strike {label} is not near the active {side} wall ({effective_oi:,} OI vs side max {side_max:,}).")
                confidence_cap = min(confidence_cap, 72)
                score_adjust -= 8
                if recommendation_cap == "OPEN":
                    recommendation_cap = "OPEN_SMALL"
            elif share >= 0.35:
                positives.append(f"Short strike {label} is near an active {side} wall ({share:.0%} of side max OI).")

    upper_ext = _safe_int(price_ctx.get("upper_extension_score"), 0)
    lower_ext = _safe_int(price_ctx.get("lower_extension_score"), 0)
    call_pressure = _safe_float(opt_ctx.get("call_to_put_pressure"), 1.0) or 1.0
    put_pressure = _safe_float(opt_ctx.get("put_to_call_pressure"), 1.0) or 1.0
    pcr = _safe_float(opt_ctx.get("pcr"), None)
    top_call = (opt_ctx.get("top_call_walls") or [{}])[0] if opt_ctx.get("top_call_walls") else {}
    top_put = (opt_ctx.get("top_put_walls") or [{}])[0] if opt_ctx.get("top_put_walls") else {}
    top_call_dist = _safe_float(top_call.get("distance_pct"), None)
    top_put_dist = _safe_float(top_put.get("distance_pct"), None)
    dirn = str(direction or (trade or {}).get("direction") or "").lower()

    if dirn == "bull" or ttype in {"PS", "CALL"}:
        if upper_ext >= 4:
            warnings.append("Daily/weekly price is extended near/above upper Bollinger area; bullish entries need a pullback or very strong put support.")
            confidence_cap = min(confidence_cap, 78)
            score_adjust -= 6
            if recommendation_cap == "OPEN":
                recommendation_cap = "OPEN_SMALL"
        elif upper_ext >= 2:
            warnings.append("Price is near the upper daily/weekly band; avoid chasing bullish premium unless support/OI is clear.")
            confidence_cap = min(confidence_cap, 82)
            score_adjust -= 3
        if call_pressure >= 1.35 and (pcr is None or pcr <= 0.95):
            warnings.append(f"Aggregate option map is call-heavy into expiry (call/put pressure {call_pressure:.2f}, PCR {pcr}); this conflicts with a new bullish PS.")
            confidence_cap = min(confidence_cap, 72)
            score_adjust -= 10
            if recommendation_cap == "OPEN":
                recommendation_cap = "OPEN_SMALL"
        if top_call_dist is not None and top_call_dist <= 8.0 and upper_ext >= 2:
            warnings.append(f"Nearest active call wall is only {top_call_dist:.1f}% above spot while price is extended; upside may be capped before bullish edge improves.")
            confidence_cap = min(confidence_cap, 74)
            score_adjust -= 5
        if put_pressure >= 1.25 and top_put_dist is not None and top_put_dist <= 12.0:
            positives.append(f"Put OI support exists below spot (put/call support ratio {put_pressure:.2f}; top put wall {top_put.get('strike')}).")
    elif dirn == "bear" or ttype in {"CS", "PUT"}:
        if lower_ext >= 4:
            warnings.append("Daily/weekly price is extended near/below lower Bollinger area; bearish entries need a bounce or very strong call resistance.")
            confidence_cap = min(confidence_cap, 78)
            score_adjust -= 6
            if recommendation_cap == "OPEN":
                recommendation_cap = "OPEN_SMALL"
        if put_pressure >= 1.35 and (pcr is None or pcr >= 1.20):
            warnings.append(f"Aggregate option map is put-heavy into expiry (put/call pressure {put_pressure:.2f}, PCR {pcr}); this conflicts with a new bearish CS/PUT.")
            confidence_cap = min(confidence_cap, 72)
            score_adjust -= 10
            if recommendation_cap == "OPEN":
                recommendation_cap = "OPEN_SMALL"
    elif ttype == "IC" or dirn == "neutral":
        if price_ctx.get("range_premium_ok"):
            positives.append("BB/Keltner context supports range-premium logic.")
        else:
            warnings.append("Price-action range context is not strong enough for a high-conviction IC.")
            confidence_cap = min(confidence_cap, 78)
            score_adjust -= 4

    edays = _safe_int(earn_days, 999)
    if sym not in ETF_INDEX_PROXIES and dte > 0 and 0 < edays <= dte + 3:
        blockers.append(f"Earnings in {edays} days falls inside/near the {dte} DTE holding window; avoid opening a new swing credit trade through earnings.")
        confidence_cap = min(confidence_cap, 68)
        score_adjust -= 16
        recommendation_cap = "WATCH"
    elif sym not in ETF_INDEX_PROXIES and edays < 999:
        positives.append(f"Next earnings is {edays} calendar days away; no direct earnings conflict for {dte} DTE." if dte else f"Next earnings is {edays} calendar days away.")

    if not opt_ctx.get("available"):
        warnings.append("Local strike-level OI context was unavailable; do not send full-size Agentic alerts without option confirmation.")
        confidence_cap = min(confidence_cap, 76)
        score_adjust -= 6
        if recommendation_cap == "OPEN":
            recommendation_cap = "OPEN_SMALL"

    grade = "BLOCK" if blockers else "WARN" if warnings else "PASS"
    alt = None
    if upper_ext >= 2 and call_pressure >= 1.15 and top_call.get("strike"):
        alt = f"If price rejects the stretched upper-band area, evaluate a CS above the active call wall near {top_call.get('strike'):g}C; otherwise wait."
    elif lower_ext >= 2 and put_pressure >= 1.15 and top_put.get("strike"):
        alt = f"If price rejects lower-band weakness, evaluate a PS below the active put wall near {top_put.get('strike'):g}P; otherwise wait."

    summary_bits: List[str] = []
    if blockers:
        summary_bits.append("Blockers: " + " ".join(blockers[:2]))
    if warnings:
        summary_bits.append("Warnings: " + " ".join(warnings[:3]))
    if positives:
        summary_bits.append("Confirmations: " + " ".join(positives[:2]))
    summary_bits.append(opt_ctx.get("summary") or "")
    summary_bits.append(price_ctx.get("summary") or "")
    if alt:
        summary_bits.append("Alternate: " + alt)

    return {
        "grade": grade,
        "confidence_cap": int(max(0, min(100, confidence_cap))),
        "score_adjust": int(score_adjust),
        "recommendation_cap": recommendation_cap,
        "blockers": blockers,
        "warnings": warnings,
        "confirmations": positives,
        "alternate_action": alt,
        "price_action": price_ctx,
        "option_context": opt_ctx,
        "summary": " ".join(x for x in summary_bits if x),
    }


def apply_agentic_guardrails(finding: Dict[str, Any], guard: Dict[str, Any], min_confidence: int = 72) -> Dict[str, Any]:
    current_conf = _safe_int(finding.get("confidence"), 0)
    adjusted = current_conf + _safe_int(guard.get("score_adjust"), 0)
    cap = _safe_int(guard.get("confidence_cap"), 100)
    new_conf = int(max(0, min(cap, adjusted)))
    finding["confidence"] = new_conf
    finding["score"] = new_conf
    finding["recommendation"] = _cap_recommendation(finding.get("recommendation"), new_conf, guard.get("recommendation_cap") or "OPEN")
    metrics = finding.setdefault("metrics", {})
    metrics["agentic_guardrails"] = guard
    if guard.get("summary"):
        finding["rationale"] = (finding.get("rationale") or "") + " Guardrail agent: " + str(guard.get("summary"))
    if new_conf < int(min_confidence or 0):
        finding["filtered_by_guardrails"] = True
        finding["filter_reason"] = f"Agentic guardrails capped confidence at {new_conf}; below alert threshold {min_confidence}."
    return finding
