# oiapp/scanners/oi_buildup_core.py
"""Reusable OI buildup scanner primitives.

The dashboard API and the AI Hub both need the same ST / MT / LT open-interest
buildup logic.  Keep the data/rules here so conversational requests can run the
real scanner without having to go through a Flask request object.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..db import _connect


def _wl_key(base: str, watchlist_id: Optional[Any] = None) -> str:
    return f"{base}_{int(watchlist_id)}" if watchlist_id not in (None, "", 0, "0") else base


def _get_watchlist_symbols(watchlist_id: Optional[Any]) -> Optional[List[str]]:
    if watchlist_id in (None, "", 0, "0"):
        return None
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                (int(watchlist_id),),
            ).fetchall()
            return [str(r[0]).upper().strip() for r in rows if r and str(r[0]).strip()]
        finally:
            con.close()
    except Exception:
        return None


def _safe_float(value: Any, default: float = 0.0, ndigits: Optional[int] = None) -> float:
    try:
        f = float(value)
        if not math.isfinite(f):
            return default
        return round(f, ndigits) if ndigits is not None else f
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _clamp_days(value: Any, default: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, _safe_int(value, default)))


def _oi_delta(rows: Sequence[Tuple[str, int, int, int, int]], n: int) -> Tuple[float, float, Optional[Tuple[str, int, int, int, int]]]:
    """rows is newest-first list of (date, call_oi, put_oi, call_vol, put_vol)."""
    if len(rows) < 2:
        return 0.0, 0.0, rows[0] if rows else None
    d0 = rows[0]
    dn = rows[min(max(1, int(n or 1)), len(rows) - 1)]
    oi0 = _safe_int(d0[1]) + _safe_int(d0[2])
    oin = _safe_int(dn[1]) + _safe_int(dn[2])
    vol0 = _safe_int(d0[3]) + _safe_int(d0[4])
    voln = _safe_int(dn[3]) + _safe_int(dn[4])
    return round((oi0 - oin) / max(1, oin) * 100, 2), round((vol0 - voln) / max(1, voln) * 100, 2), dn


def _price_delta(points: Sequence[Tuple[str, float]], n: int) -> float:
    """points is oldest-first list of (date, price)."""
    if not points:
        return 0.0
    cur = _safe_float(points[-1][1], 0.0)
    base = _safe_float(points[max(0, len(points) - 1 - int(n or 1))][1], cur)
    return round((cur - base) / max(0.01, base) * 100, 2)


def oi_buildup_signal(oi_pct: float, price_pct: float) -> str:
    """Classic OI/price interpretation used by the OI Buildup dashboard."""
    if oi_pct > 0 and price_pct > 0:
        return "Long Buildup"
    if oi_pct > 0 and price_pct < 0:
        return "Short Buildup"
    if oi_pct < 0 and price_pct < 0:
        return "Long Unwinding"
    if oi_pct < 0 and price_pct > 0:
        return "Short Covering"
    return "Neutral"


def oi_signal_bias(signal: str) -> str:
    s = (signal or "").strip().lower()
    if s in {"long buildup", "short covering"}:
        return "bullish"
    if s in {"short buildup", "long unwinding"}:
        return "bearish"
    return "neutral"


def _bias_score(pr_mt: float, pr_lt: float, st_out: str, mt_out: str, lt_out: str, pcr: float) -> Tuple[int, str]:
    score = 0
    if pr_mt > 2.0:
        score += 2
    elif pr_mt > 0.5:
        score += 1
    elif pr_mt < -2.0:
        score -= 2
    elif pr_mt < -0.5:
        score -= 1

    if pr_lt > 4.0:
        score += 2
    elif pr_lt > 1.5:
        score += 1
    elif pr_lt < -4.0:
        score -= 2
    elif pr_lt < -1.5:
        score -= 1

    oi_score = {"Long Buildup": 2, "Short Covering": 1, "Short Buildup": -2, "Long Unwinding": -1, "Neutral": 0}
    score += oi_score.get(st_out, 0) + oi_score.get(mt_out, 0) + oi_score.get(lt_out, 0)
    if pcr > 1.5:
        score -= 1
    elif pcr < 0.7:
        score += 1

    if score >= 4:
        bias = "Strongly Bullish"
    elif score >= 2:
        bias = "Bullish"
    elif score >= 1:
        bias = "Mildly Bullish"
    elif score <= -4:
        bias = "Strongly Bearish"
    elif score <= -2:
        bias = "Bearish"
    elif score <= -1:
        bias = "Mildly Bearish"
    else:
        bias = "Sideways"
    return score, bias


def run_oi_buildup_screener(
    *,
    st_days: int = 1,
    mt_days: int = 5,
    lt_days: int = 30,
    watchlist_id: Optional[Any] = None,
    max_symbols: Optional[int] = None,
    store_cache: bool = True,
    warm_watchlist: bool = True,
) -> Dict[str, Any]:
    """Run the DB-backed OI buildup screener.

    Returns the same row fields the dashboard expects plus metadata.  It does not
    use yfinance; all OI, volume and OI-weighted spot proxies come from the local
    ``options`` table.
    """
    st_days = _clamp_days(st_days, 1, 1, 10)
    mt_days = _clamp_days(mt_days, 5, 2, 60)
    lt_days = _clamp_days(lt_days, 30, 5, 90)
    if mt_days < st_days:
        mt_days = st_days
    if lt_days < mt_days:
        lt_days = mt_days

    today = _dt.date.today().isoformat()
    completed_at = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    max_rows = max(lt_days + 2, 32)
    errors: List[Any] = []
    watchlist_syms = _get_watchlist_symbols(watchlist_id)

    con = _connect()
    try:
        # Ensure tables exist even when called before full app init finished.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS app_cache (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated TEXT
            )
            """
        )
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
        con.commit()

        sym_exp = dict(
            con.execute(
                "SELECT symbol, MIN(expiration) FROM options WHERE expiration>=? GROUP BY symbol",
                (today,),
            ).fetchall()
        )
        if watchlist_syms is not None:
            wl_set = {s.upper() for s in watchlist_syms if s}
            sym_exp = {s: e for s, e in sym_exp.items() if str(s).upper() in wl_set}
            if not sym_exp and wl_set and warm_watchlist:
                try:
                    from ..services.market import fetch_store_for
                    from concurrent.futures import ThreadPoolExecutor

                    with ThreadPoolExecutor(max_workers=6) as ex:
                        list(ex.map(lambda s: fetch_store_for(s), list(wl_set)[:40]))
                    sym_exp = dict(
                        con.execute(
                            "SELECT symbol, MIN(expiration) FROM options WHERE expiration>=? GROUP BY symbol",
                            (today,),
                        ).fetchall()
                    )
                    sym_exp = {s: e for s, e in sym_exp.items() if str(s).upper() in wl_set}
                except Exception as warm_exc:
                    errors.append(f"watchlist warmup: {warm_exc}")

        all_syms = list(dict.fromkeys([str(s).upper() for s in sym_exp.keys() if s]))
        if max_symbols:
            all_syms = all_syms[: max(1, min(1000, int(max_symbols)))]
            sym_exp = {s: sym_exp.get(s) for s in all_syms if s in sym_exp}
        if not all_syms:
            return {
                "ok": True,
                "results": [],
                "count": 0,
                "date": today,
                "completed_at": completed_at,
                "st_days": st_days,
                "mt_days": mt_days,
                "lt_days": lt_days,
                "watchlist_id": watchlist_id,
                "errors": [],
                "error_count": 0,
            }

        cutoff = (_dt.date.today() - _dt.timedelta(days=max_rows + 2)).isoformat()
        sym_ph = ",".join(["?"] * len(all_syms))
        oi_rows = con.execute(
            f"""
            SELECT symbol, date,
                   SUM(CASE WHEN type='call' THEN oi     ELSE 0 END) call_oi,
                   SUM(CASE WHEN type='put'  THEN oi     ELSE 0 END) put_oi,
                   SUM(CASE WHEN type='call' THEN volume ELSE 0 END) call_vol,
                   SUM(CASE WHEN type='put'  THEN volume ELSE 0 END) put_vol
            FROM options
            WHERE date >= ?
              AND symbol IN ({sym_ph})
              AND expiration >= date
            GROUP BY symbol, date
            ORDER BY symbol, date DESC
            """,
            [cutoff] + all_syms,
        ).fetchall()

        oi_by_sym: Dict[str, List[Tuple[str, int, int, int, int]]] = defaultdict(list)
        for sym, dt, c_oi, p_oi, c_vol, p_vol in oi_rows:
            oi_by_sym[str(sym).upper()].append((str(dt), _safe_int(c_oi), _safe_int(p_oi), _safe_int(c_vol), _safe_int(p_vol)))

        spot_rows = con.execute(
            """
            SELECT symbol, date, SUM(strike*oi)/NULLIF(SUM(oi),0) spot
            FROM (
                SELECT symbol, date, strike, SUM(oi) oi,
                       ROW_NUMBER() OVER (PARTITION BY symbol,date ORDER BY SUM(oi) DESC) rn
                FROM options
                WHERE date >= ?
                  AND expiration BETWEEN date('now') AND date('now','+45 days')
                GROUP BY symbol, date, strike
            )
            WHERE rn <= 5
            GROUP BY symbol, date ORDER BY symbol, date
            """,
            (cutoff,),
        ).fetchall()
        spot_by_sym: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
        allowed = set(all_syms)
        for sym, dt, sp in spot_rows:
            usym = str(sym).upper()
            if usym in allowed and sp:
                spot_by_sym[usym].append((str(dt), round(_safe_float(sp, 0.0), 2)))

        futures_roots = {"SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "XLE", "XLF", "XLK"}
        results: List[Dict[str, Any]] = []
        for sym, exp in sym_exp.items():
            usym = str(sym).upper()
            try:
                rows = oi_by_sym.get(usym, [])
                if len(rows) < 2:
                    continue
                d0 = rows[0]
                call_oi_now = _safe_int(d0[1])
                put_oi_now = _safe_int(d0[2])
                total_oi = call_oi_now + put_oi_now
                if total_oi == 0:
                    continue

                pcr = round(put_oi_now / max(1, call_oi_now), 3)
                pts = spot_by_sym.get(usym, [])
                spot = pts[-1][1] if pts else 0.0

                oi_st_pct, vol_st_pct, _ = _oi_delta(rows, st_days)
                oi_mt_pct, vol_mt_pct, _ = _oi_delta(rows, mt_days)
                oi_lt_pct, vol_lt_pct, _ = _oi_delta(rows, lt_days)

                pr_st = _price_delta(pts, st_days)
                pr_mt = _price_delta(pts, mt_days)
                pr_lt = _price_delta(pts, lt_days)

                st_out = oi_buildup_signal(oi_st_pct, pr_st)
                mt_out = oi_buildup_signal(oi_mt_pct, pr_mt)
                lt_out = oi_buildup_signal(oi_lt_pct, pr_lt)
                bs, bias = _bias_score(pr_mt, pr_lt, st_out, mt_out, lt_out, pcr)

                results.append(
                    {
                        "symbol": usym,
                        "spot": spot,
                        "expiry": exp,
                        "has_futures": usym in futures_roots,
                        "call_oi": call_oi_now,
                        "put_oi": put_oi_now,
                        "total_oi": total_oi,
                        "pcr": pcr,
                        "pc_ratio": pcr,
                        "bias": bias,
                        "bias_score": bs,
                        # Legacy dashboard field names retained for compatibility.
                        "price_1d_pct": pr_st,
                        "oi_1d_chg": 0,
                        "oi_1d_pct": oi_st_pct,
                        "vol_1d_pct": vol_st_pct,
                        "st_outlook": st_out,
                        "price_5d_pct": pr_mt,
                        "oi_5d_chg": 0,
                        "oi_5d_pct": oi_mt_pct,
                        "vol_5d_pct": vol_mt_pct,
                        "mt_outlook": mt_out,
                        "price_15d_pct": pr_lt,
                        "oi_15d_chg": 0,
                        "oi_15d_pct": oi_lt_pct,
                        "vol_15d_pct": vol_lt_pct,
                        "lt_outlook": lt_out,
                        # Explicit horizon aliases for AI/rule explanations.
                        "st_days": st_days,
                        "mt_days": mt_days,
                        "lt_days": lt_days,
                        "st_oi_pct": oi_st_pct,
                        "mt_oi_pct": oi_mt_pct,
                        "lt_oi_pct": oi_lt_pct,
                        "st_price_pct": pr_st,
                        "mt_price_pct": pr_mt,
                        "lt_price_pct": pr_lt,
                        "st_vol_pct": vol_st_pct,
                        "mt_vol_pct": vol_mt_pct,
                        "lt_vol_pct": vol_lt_pct,
                        "st_bias": oi_signal_bias(st_out),
                        "mt_bias": oi_signal_bias(mt_out),
                        "lt_bias": oi_signal_bias(lt_out),
                        "latest_oi_date": d0[0],
                        "history_points": len(rows),
                    }
                )
            except Exception as sym_exc:
                errors.append({"sym": usym, "error": str(sym_exc)})

        results.sort(key=lambda x: abs(_safe_float(x.get("oi_1d_pct"), 0.0)), reverse=True)

        if store_cache:
            try:
                cache_key = _wl_key("oi_buildup_scan", watchlist_id)
                ts_key = _wl_key("oib_completed_at", watchlist_id)
                con.execute(
                    "INSERT OR REPLACE INTO app_cache(key,value,updated) VALUES (?,?,?)",
                    (cache_key, _json.dumps(results), completed_at),
                )
                con.execute(
                    "INSERT OR REPLACE INTO app_cache(key,value,updated) VALUES (?,?,?)",
                    (ts_key, "oib_ts", completed_at),
                )
                con.commit()
            except Exception as cache_exc:
                errors.append(f"cache save: {cache_exc}")

        return {
            "ok": True,
            "results": results,
            "count": len(results),
            "date": today,
            "completed_at": completed_at,
            "st_days": st_days,
            "mt_days": mt_days,
            "lt_days": lt_days,
            "watchlist_id": watchlist_id,
            "errors": errors[:10],
            "error_count": len(errors),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "results": [],
            "count": 0,
            "date": today,
            "completed_at": completed_at,
            "st_days": st_days,
            "mt_days": mt_days,
            "lt_days": lt_days,
            "watchlist_id": watchlist_id,
            "errors": errors[:10],
            "error_count": len(errors),
        }
    finally:
        con.close()

# V61 compatibility shim: keep older imports of oi_buildup_core aligned with
# the seller-side PCR/OI scanner used by the dashboard and AI Hub.
def run_oi_buildup_screener(  # type: ignore[no-redef]
    *,
    st_days: int = 3,
    mt_days: int = 10,
    lt_days: int = 30,
    watchlist_id: Optional[Any] = None,
    max_symbols: Optional[int] = None,
    store_cache: bool = True,
    warm_watchlist: bool = True,
) -> Dict[str, Any]:
    from .oi_buildup_scanner import run_oi_buildup_screener as _seller_side_scanner

    return _seller_side_scanner(
        st_days=st_days,
        mt_days=mt_days,
        lt_days=lt_days,
        watchlist_id=watchlist_id,
        max_symbols=max_symbols,
        save_cache=store_cache,
        warm_watchlist=warm_watchlist,
    )
