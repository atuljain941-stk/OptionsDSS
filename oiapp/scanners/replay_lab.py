from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from flask import Blueprint, jsonify, request

from .sqlite_store import get_market_data_store
from .scanner_builder import _ensure_tables as _scanner_ensure_tables, _preferred_watchlist_id, _watchlist_symbols, _watchlists

replay_lab_bp = Blueprint("replay_lab_bp", __name__, url_prefix="/replay-lab")
from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
OPTIONS_DB_PATH = _OIAPP_DB_PATH

QUALITY_LABELS = {
    1: "All",
    2: "Watch+",
    3: "Setup+",
    4: "Tradable+",
    5: "Best only",
}

# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def _today() -> date:
    return date.today()


def _parse_date(value: Any, default: Optional[date] = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    if not s:
        if default is not None:
            return default
        raise ValueError("date is required")
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _clean_symbol(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "")


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        v = float(value)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _pct(cur: Any, base: Any, ndigits: int = 2) -> float:
    c = _safe_float(cur, 0.0) or 0.0
    b = _safe_float(base, 0.0) or 0.0
    if abs(b) < 1e-9:
        if abs(c) < 1e-9:
            return 0.0
        return round(100.0 if c > 0 else -100.0, ndigits)
    return round((c - b) / abs(b) * 100.0, ndigits)


def _clamp(value: Any, lo: float, hi: float) -> float:
    v = _safe_float(value, lo) or lo
    return max(lo, min(hi, v))


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _connect_options() -> sqlite3.Connection:
    con = sqlite3.connect(OPTIONS_DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return con


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
    except Exception:
        return False


def _parse_symbols_csv(raw: Any) -> List[str]:
    parts = []
    for chunk in str(raw or "").replace("\n", ",").split(","):
        s = _clean_symbol(chunk)
        if s:
            parts.append(s)
    return list(dict.fromkeys(parts))


def _symbols_from_payload(payload: Dict[str, Any]) -> Tuple[Optional[Any], List[str]]:
    symbol = _clean_symbol(payload.get("symbol"))
    if symbol:
        return None, [symbol]
    syms = _parse_symbols_csv(payload.get("symbols"))
    if syms:
        return None, syms
    watchlist_id = payload.get("watchlist_id") or _preferred_watchlist_id()
    syms = [_clean_symbol(s) for s in _watchlist_symbols(watchlist_id) if _clean_symbol(s)]
    return watchlist_id, list(dict.fromkeys(syms))


def _first_bar_on_or_after(df: Optional[pd.DataFrame], d: date) -> Optional[int]:
    if df is None or df.empty:
        return None
    for i, idx in enumerate(df.index):
        try:
            if idx.date() >= d:
                return i
        except Exception:
            pass
    return None


def _bar_idx_on_or_before(df: Optional[pd.DataFrame], d: date) -> Optional[int]:
    if df is None or df.empty:
        return None
    out = None
    for i, idx in enumerate(df.index):
        try:
            if idx.date() <= d:
                out = i
            else:
                break
        except Exception:
            pass
    return out


def _trading_days_from_histories(histories: Dict[str, pd.DataFrame], start: date, end: date) -> List[date]:
    days = set()
    for df in histories.values():
        if df is None or df.empty:
            continue
        for idx in df.index:
            try:
                d = idx.date()
                if start <= d <= end:
                    days.add(d)
            except Exception:
                pass
    return sorted(days)


def _load_histories(symbols: Sequence[str], start: date, end: date, provider: str = "auto", auto_fetch: bool = True) -> Tuple[Dict[str, pd.DataFrame], List[Dict[str, Any]]]:
    store = get_market_data_store()
    warmup = start - timedelta(days=420)
    histories: Dict[str, pd.DataFrame] = {}
    errors: List[Dict[str, Any]] = []
    for sym in list(dict.fromkeys([_clean_symbol(s) for s in symbols if _clean_symbol(s)])):
        try:
            df, meta = store.ensure_daily_history(sym, warmup, end, provider=provider, auto_fetch=auto_fetch)
            if df is None or df.empty:
                # For replay work, a partial local cache is still useful.  The
                # generic store requires full warmup coverage, but a user may
                # only have the last month/quarter synced.  Use whatever cached
                # bars are available and let module-level warmup checks decide.
                try:
                    df = store.get_bars(sym, warmup, end, "1d")
                    if df is None or df.empty:
                        df = store.get_bars(sym, start, end, "1d")
                except Exception:
                    df = None
            if df is None or df.empty:
                errors.append({"symbol": sym, "error": (meta or {}).get("error") or "no daily bars"})
                continue
            histories[sym] = df.sort_index()
        except Exception as exc:
            errors.append({"symbol": sym, "error": str(exc)[:240]})
    return histories, errors


def _close_at(df: pd.DataFrame, idx: int) -> Optional[float]:
    try:
        return float(df["Close"].iloc[idx])
    except Exception:
        return None


def _high_at(df: pd.DataFrame, idx: int) -> Optional[float]:
    try:
        return float(df["High"].iloc[idx])
    except Exception:
        return None


def _low_at(df: pd.DataFrame, idx: int) -> Optional[float]:
    try:
        return float(df["Low"].iloc[idx])
    except Exception:
        return None


def _open_at(df: pd.DataFrame, idx: int) -> Optional[float]:
    try:
        return float(df["Open"].iloc[idx])
    except Exception:
        return None


def _date_at(df: pd.DataFrame, idx: int) -> Optional[date]:
    try:
        return df.index[idx].date()
    except Exception:
        return None


def _hist_vol(closes: Sequence[float], lookback: int = 20) -> float:
    vals = [float(x) for x in closes if _safe_float(x, None) is not None and float(x) > 0]
    if len(vals) < 12:
        return 0.25
    rets = []
    for i in range(1, len(vals)):
        if vals[i - 1] > 0 and vals[i] > 0:
            rets.append(math.log(vals[i] / vals[i - 1]))
    if len(rets) < 8:
        return 0.25
    tail = rets[-max(8, min(lookback, len(rets))):]
    if len(tail) < 2:
        return 0.25
    mean = sum(tail) / len(tail)
    var = sum((x - mean) ** 2 for x in tail) / len(tail)
    return max(0.05, min(1.5, math.sqrt(var) * math.sqrt(252)))


def _rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    vals = [float(x) for x in closes if _safe_float(x, None) is not None]
    if len(vals) < period + 2:
        return None
    gains = []
    losses = []
    for i in range(1, len(vals)):
        ch = vals[i] - vals[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    k = 2.0 / (max(1, period) + 1.0)
    prev: Optional[float] = None
    for raw in values:
        v = _safe_float(raw, None)
        if v is None:
            out.append(prev)
            continue
        prev = v if prev is None else prev + k * (v - prev)
        out.append(prev)
    return out


def _rsidiff90(closes: Sequence[float]) -> Optional[float]:
    vals = [float(x) for x in closes if _safe_float(x, None) is not None]
    if len(vals) < 105:
        return None
    # Approximate with rolling RSI series and EMA90, same conceptual field as UAE.
    rsi_series: List[float] = []
    for i in range(len(vals)):
        r = _rsi(vals[: i + 1], 14)
        rsi_series.append(50.0 if r is None else float(r))
    e = _ema(rsi_series, 90)
    if not e or e[-1] is None:
        return None
    return rsi_series[-1] - float(e[-1])


def _strike_interval(price: float) -> float:
    if price < 25:
        return 0.5
    if price < 100:
        return 1.0
    if price < 250:
        return 2.5
    if price < 500:
        return 5.0
    return 10.0


def _round_strike(value: float, interval: float) -> float:
    return round(round(float(value) / interval) * interval, 2)


def _next_friday(d: date) -> date:
    out = d
    while out.weekday() != 4:
        out += timedelta(days=1)
    return out


def _calendar_target_date(entry: date, dte: int) -> date:
    return entry + timedelta(days=max(0, int(dte or 0)))


def _nearest_trading_date(df: Optional[pd.DataFrame], target: date) -> Optional[date]:
    idx = _first_bar_on_or_after(df, target)
    if idx is None:
        return None
    return _date_at(df, idx)


def _weekday_index(raw: Any, default: int = 0) -> int:
    s = str(raw or "").strip().lower()
    names = {"mon": 0, "monday": 0, "tue": 1, "tuesday": 1, "wed": 2, "wednesday": 2, "thu": 3, "thursday": 3, "fri": 4, "friday": 4}
    if s in names:
        return names[s]
    try:
        return max(0, min(4, int(float(s))))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Option OI snapshots as-of-date
# ---------------------------------------------------------------------------

def _latest_option_date(con: sqlite3.Connection, symbol: str, as_of: date) -> Optional[str]:
    try:
        row = con.execute(
            "SELECT MAX(substr(date,1,10)) AS d FROM options WHERE symbol=? AND substr(date,1,10)<=?",
            (_clean_symbol(symbol), as_of.isoformat()),
        ).fetchone()
        return str(row["d"] or "")[:10] if row and row["d"] else None
    except Exception:
        return None


def _nearest_option_expiry(con: sqlite3.Connection, symbol: str, as_of: date, target: date, max_extra_days: int = 45) -> date:
    """Pick an actual listed expiration known as-of the snapshot, closest to target.

    This avoids the v101 Seller Flow mistake where no target expiry was set and
    trades were effectively closed the same day.
    """
    sym = _clean_symbol(symbol)
    snap = _latest_option_date(con, sym, as_of)
    if not snap:
        return target
    try:
        rows = con.execute(
            """
            SELECT DISTINCT expiration
            FROM options
            WHERE symbol=? AND substr(date,1,10)=?
              AND expiration>=? AND expiration<=?
            ORDER BY expiration
            """,
            (sym, snap, as_of.isoformat(), (target + timedelta(days=max_extra_days)).isoformat()),
        ).fetchall()
    except Exception:
        rows = []
    exps: List[date] = []
    for r in rows:
        try:
            exps.append(_parse_date(r["expiration"]))
        except Exception:
            pass
    if not exps:
        return target
    # Prefer expiries on/after target, else closest available.
    after = [e for e in exps if e >= target]
    pool = after or exps
    return min(pool, key=lambda e: abs((e - target).days))


def _aggregate_oi_by_date(con: sqlite3.Connection, symbol: str, as_of: date, max_days: int = 60) -> List[Dict[str, Any]]:
    sym = _clean_symbol(symbol)
    cutoff = (as_of - timedelta(days=max_days + 15)).isoformat()
    try:
        rows = con.execute(
            """
            SELECT substr(date,1,10) AS d,
                   SUM(CASE WHEN lower(type) LIKE 'c%' THEN COALESCE(oi,0) ELSE 0 END) AS call_oi,
                   SUM(CASE WHEN lower(type) LIKE 'p%' THEN COALESCE(oi,0) ELSE 0 END) AS put_oi
            FROM options
            WHERE symbol=? AND substr(date,1,10)<=? AND substr(date,1,10)>=? AND expiration>=substr(date,1,10)
            GROUP BY substr(date,1,10)
            ORDER BY d DESC
            LIMIT ?
            """,
            (sym, as_of.isoformat(), cutoff, max_days + 8),
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        call_oi = _safe_float(r["call_oi"], 0.0) or 0.0
        put_oi = _safe_float(r["put_oi"], 0.0) or 0.0
        out.append({"date": str(r["d"]), "call_oi": call_oi, "put_oi": put_oi, "total_oi": call_oi + put_oi, "pcr": put_oi / max(1.0, call_oi)})
    return out


def _oi_window_stats(rows: Sequence[Dict[str, Any]], bars: int) -> Dict[str, Any]:
    if not rows:
        return {"oi_pct": 0.0, "call_pct": 0.0, "put_pct": 0.0, "pcr_chg_pct": 0.0, "base_date": None}
    latest = rows[0]
    idx = min(max(1, int(bars)), len(rows) - 1) if len(rows) > 1 else 0
    base = rows[idx]
    return {
        "oi_pct": _pct(latest.get("total_oi"), base.get("total_oi")),
        "call_pct": _pct(latest.get("call_oi"), base.get("call_oi")),
        "put_pct": _pct(latest.get("put_oi"), base.get("put_oi")),
        "pcr_chg_pct": _pct(latest.get("pcr"), base.get("pcr")),
        "base_date": base.get("date"),
    }


def _strike_rows_for_snapshot(con: sqlite3.Connection, symbol: str, as_of: date, max_expiry: Optional[date] = None, expiry: Optional[date] = None) -> List[Dict[str, Any]]:
    sym = _clean_symbol(symbol)
    snap = _latest_option_date(con, sym, as_of)
    if not snap:
        return []
    params: List[Any] = [sym, snap]
    cond = "symbol=? AND substr(date,1,10)=?"
    if expiry is not None:
        cond += " AND expiration=?"
        params.append(expiry.isoformat())
    else:
        cond += " AND expiration>=?"
        params.append(as_of.isoformat())
        if max_expiry is not None:
            cond += " AND expiration<=?"
            params.append(max_expiry.isoformat())
    try:
        rows = con.execute(
            f"""
            SELECT strike, lower(type) AS type, SUM(COALESCE(oi,0)) AS oi,
                   AVG(CASE WHEN underlying IS NOT NULL AND underlying>0 THEN underlying ELSE NULL END) AS underlying
            FROM options
            WHERE {cond}
            GROUP BY strike, lower(type)
            ORDER BY strike
            """,
            params,
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        out.append({"strike": _safe_float(r["strike"], None), "type": str(r["type"] or ""), "oi": _safe_float(r["oi"], 0.0) or 0.0, "underlying": _safe_float(r["underlying"], None), "snapshot_date": snap})
    return [x for x in out if x["strike"] is not None]


def _oi_walls(rows: Sequence[Dict[str, Any]], spot: float, band_pct: float = 0.12) -> Dict[str, Any]:
    puts = [r for r in rows if str(r.get("type") or "").startswith("p") and float(r.get("strike") or 0) <= spot]
    calls = [r for r in rows if str(r.get("type") or "").startswith("c") and float(r.get("strike") or 0) >= spot]
    near_lo = spot * (1 - band_pct)
    near_hi = spot * (1 + band_pct)
    puts_near = [r for r in puts if float(r.get("strike") or 0) >= near_lo]
    calls_near = [r for r in calls if float(r.get("strike") or 0) <= near_hi]
    put_wall = max(puts_near or puts, key=lambda r: (float(r.get("oi") or 0), float(r.get("strike") or 0)), default=None)
    call_wall = max(calls_near or calls, key=lambda r: (float(r.get("oi") or 0), -float(r.get("strike") or 0)), default=None)
    call_oi = sum(float(r.get("oi") or 0) for r in rows if str(r.get("type") or "").startswith("c"))
    put_oi = sum(float(r.get("oi") or 0) for r in rows if str(r.get("type") or "").startswith("p"))
    total = call_oi + put_oi
    return {
        "put_wall": round(float(put_wall.get("strike")), 2) if put_wall else None,
        "put_wall_oi": int(float(put_wall.get("oi") or 0)) if put_wall else 0,
        "call_wall": round(float(call_wall.get("strike")), 2) if call_wall else None,
        "call_wall_oi": int(float(call_wall.get("oi") or 0)) if call_wall else 0,
        "call_oi": int(call_oi),
        "put_oi": int(put_oi),
        "total_oi": int(total),
        "pcr": round(put_oi / max(1.0, call_oi), 3),
        "top_put_walls": sorted([{"strike": round(float(r.get("strike") or 0), 2), "oi": int(float(r.get("oi") or 0))} for r in puts_near], key=lambda x: x["oi"], reverse=True)[:5],
        "top_call_walls": sorted([{"strike": round(float(r.get("strike") or 0), 2), "oi": int(float(r.get("oi") or 0))} for r in calls_near], key=lambda x: x["oi"], reverse=True)[:5],
    }


def _max_pain(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
    strikes = sorted({float(r.get("strike")) for r in rows if _safe_float(r.get("strike"), None) is not None})
    if not strikes:
        return None
    best_k = None
    best_pain = None
    for settle in strikes:
        pain = 0.0
        for r in rows:
            k = float(r.get("strike") or 0.0)
            oi = float(r.get("oi") or 0.0)
            typ = str(r.get("type") or "")
            if typ.startswith("c"):
                pain += max(0.0, settle - k) * oi
            elif typ.startswith("p"):
                pain += max(0.0, k - settle) * oi
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best_k = settle
    return round(float(best_k), 2) if best_k is not None else None


# ---------------------------------------------------------------------------
# Trade construction/outcome
# ---------------------------------------------------------------------------

def _build_spread(symbol: str, strategy: str, spot: float, dte: int, entry_date: date, width: float, otm_pct: float) -> Dict[str, Any]:
    interval = _strike_interval(spot)
    width = max(interval, float(width or interval))
    otm = max(0.0, float(otm_pct or 0.0) / 100.0)
    strategy = str(strategy or "PS").upper()
    short_put = short_call = long_put = long_call = None
    if strategy == "PS":
        short_put = _round_strike(spot * (1 - otm), interval)
        long_put = max(interval, _round_strike(short_put - width, interval))
    elif strategy == "CS":
        short_call = _round_strike(spot * (1 + otm), interval)
        long_call = _round_strike(short_call + width, interval)
    elif strategy == "IC":
        short_put = _round_strike(spot * (1 - max(otm, 0.01)), interval)
        long_put = max(interval, _round_strike(short_put - width, interval))
        short_call = _round_strike(spot * (1 + max(otm, 0.01)), interval)
        long_call = _round_strike(short_call + width, interval)
    elif strategy in {"CB", "CALL"}:
        strategy = "CB"
        long_call = _round_strike(spot, interval)
        short_call = _round_strike(long_call + width, interval)
    elif strategy in {"PB", "PUT"}:
        strategy = "PB"
        long_put = _round_strike(spot, interval)
        short_put = max(interval, _round_strike(long_put - width, interval))
    target = _calendar_target_date(entry_date, int(dte))
    trade = {
        "symbol": symbol,
        "trade_type": strategy,
        "strategy": strategy,
        "entry_date": entry_date.isoformat(),
        "target_expiry_date": target.isoformat(),
        "expiry_date": target.isoformat(),
        "dte": int(dte),
        "entry_price": round(float(spot), 2),
        "short_put": short_put,
        "long_put": long_put,
        "short_call": short_call,
        "long_call": long_call,
        "strike_width": round(width, 2),
        "otm_pct": round(otm * 100.0, 2),
    }
    trade["strikes"] = _format_strikes(trade)
    trade["legs"] = _trade_legs(trade)
    trade["trade_description"] = _trade_description(trade)
    return trade


def _format_strikes(trade: Dict[str, Any]) -> str:
    t = str(trade.get("trade_type") or "")
    if t == "PS":
        return f"S{trade.get('short_put')}P/B{trade.get('long_put')}P"
    if t == "CS":
        return f"S{trade.get('short_call')}C/B{trade.get('long_call')}C"
    if t == "IC":
        return f"B{trade.get('long_put')}P/S{trade.get('short_put')}P | S{trade.get('short_call')}C/B{trade.get('long_call')}C"
    if t == "CB":
        return f"B{trade.get('long_call')}C/S{trade.get('short_call')}C"
    if t == "PB":
        return f"B{trade.get('long_put')}P/S{trade.get('short_put')}P"
    return ""


def _trade_legs(trade: Dict[str, Any]) -> List[Dict[str, Any]]:
    t = str(trade.get("trade_type") or "").upper()
    exp = trade.get("target_expiry_date") or trade.get("expiry_date")
    legs: List[Dict[str, Any]] = []
    def add(side: str, opt_type: str, strike: Any):
        if _safe_float(strike, None) is None:
            return
        legs.append({"side": side, "type": opt_type, "strike": _safe_float(strike, None), "expiry": exp, "qty": 1})
    if t == "PS":
        add("SELL", "PUT", trade.get("short_put")); add("BUY", "PUT", trade.get("long_put"))
    elif t == "CS":
        add("SELL", "CALL", trade.get("short_call")); add("BUY", "CALL", trade.get("long_call"))
    elif t == "IC":
        add("BUY", "PUT", trade.get("long_put")); add("SELL", "PUT", trade.get("short_put")); add("SELL", "CALL", trade.get("short_call")); add("BUY", "CALL", trade.get("long_call"))
    elif t == "CB":
        add("BUY", "CALL", trade.get("long_call")); add("SELL", "CALL", trade.get("short_call"))
    elif t == "PB":
        add("BUY", "PUT", trade.get("long_put")); add("SELL", "PUT", trade.get("short_put"))
    return legs


def _trade_description(trade: Dict[str, Any]) -> str:
    sym = str(trade.get("symbol") or "")
    typ = str(trade.get("trade_type") or trade.get("strategy") or "")
    exp = str(trade.get("target_expiry_date") or trade.get("expiry_date") or "")
    strikes = trade.get("strikes") or _format_strikes(trade)
    return f"{sym} {typ} {strikes} exp {exp}".strip()


def _refresh_trade_labels(trade: Dict[str, Any]) -> Dict[str, Any]:
    trade["strikes"] = trade.get("strikes") or _format_strikes(trade)
    trade["legs"] = _trade_legs(trade)
    trade["trade_description"] = _trade_description(trade)
    return trade


def _close_trade_with_daily_bars(trade: Dict[str, Any], daily: pd.DataFrame, exit_target: date, outcome_mode: str = "touch_stop", stop_buffer_pct: float = 0.0) -> Optional[Dict[str, Any]]:
    entry_idx = _first_bar_on_or_after(daily, _parse_date(trade.get("entry_date")))
    exit_idx = _first_bar_on_or_after(daily, exit_target)
    if entry_idx is None or exit_idx is None:
        return None
    if exit_idx < entry_idx:
        exit_idx = entry_idx
    exit_day = _date_at(daily, exit_idx)
    exit_close = _close_at(daily, exit_idx)
    if exit_day is None or exit_close is None:
        return None
    entry = _safe_float(trade.get("entry_price"), None)
    if entry is None or entry <= 0:
        return None
    typ = str(trade.get("trade_type") or "").upper()
    lows = []
    highs = []
    closes = []
    for i in range(entry_idx, exit_idx + 1):
        lo = _low_at(daily, i); hi = _high_at(daily, i); cl = _close_at(daily, i)
        if lo is not None: lows.append(float(lo))
        if hi is not None: highs.append(float(hi))
        if cl is not None: closes.append(float(cl))
    min_price = min(lows) if lows else exit_close
    max_price = max(highs) if highs else exit_close
    stop_buffer = max(0.0, float(stop_buffer_pct or 0.0)) / 100.0
    expiry_win = False
    path_breach = False
    breach_note = ""
    check = ""
    if typ == "PS":
        sp = float(trade.get("short_put") or entry)
        expiry_win = exit_close >= sp
        path_breach = min_price <= sp * (1.0 - stop_buffer)
        check = f"PS expiry check: exit close >= short put {sp}; exit {exit_close:.2f}."
        breach_note = f"Min during hold {min_price:.2f}; short put breach={'YES' if path_breach else 'NO'}."
    elif typ == "CS":
        sc = float(trade.get("short_call") or entry)
        expiry_win = exit_close <= sc
        path_breach = max_price >= sc * (1.0 + stop_buffer)
        check = f"CS expiry check: exit close <= short call {sc}; exit {exit_close:.2f}."
        breach_note = f"Max during hold {max_price:.2f}; short call breach={'YES' if path_breach else 'NO'}."
    elif typ == "IC":
        sp = float(trade.get("short_put") or entry)
        sc = float(trade.get("short_call") or entry)
        expiry_win = sp <= exit_close <= sc
        put_breach = min_price <= sp * (1.0 - stop_buffer)
        call_breach = max_price >= sc * (1.0 + stop_buffer)
        path_breach = put_breach or call_breach
        check = f"IC expiry check: exit close between short strikes {sp}-{sc}; exit {exit_close:.2f}."
        breach_note = f"Path H/L {max_price:.2f}/{min_price:.2f}; put breach={'YES' if put_breach else 'NO'}, call breach={'YES' if call_breach else 'NO'}."
    elif typ == "CB":
        sc = float(trade.get("short_call") or entry)
        expiry_win = exit_close >= sc
        path_breach = False
        check = f"CB target check: exit close >= short call/target {sc}; exit {exit_close:.2f}."
        breach_note = f"Max during hold {max_price:.2f}."
    elif typ == "PB":
        sp = float(trade.get("short_put") or entry)
        expiry_win = exit_close <= sp
        path_breach = False
        check = f"PB target check: exit close <= short put/target {sp}; exit {exit_close:.2f}."
        breach_note = f"Min during hold {min_price:.2f}."
    else:
        expiry_win = exit_close >= entry
        path_breach = False
        check = f"Directional check: exit close >= entry; exit {exit_close:.2f}."
        breach_note = f"Path H/L {max_price:.2f}/{min_price:.2f}."
    mode = str(outcome_mode or "touch_stop").strip().lower()
    if mode in {"expiry", "expiry_only", "close_only"}:
        winner = expiry_win
        outcome_model_note = "Expiry-only model: ignores intraperiod short-strike touches."
    elif mode in {"touch", "touch_stop", "conservative"}:
        winner = bool(expiry_win and not path_breach)
        outcome_model_note = "Conservative path-risk model: credit spread loses if a short strike is touched during the hold."
    else:
        # Balanced: still counts expiry win, but marks breach as path risk.
        winner = expiry_win
        outcome_model_note = "Balanced model: expiry result is primary; path breach is reported as risk."
    change = exit_close - entry
    pct = change / entry * 100.0
    mae_down = (min_price - entry) / entry * 100.0
    mfe_up = (max_price - entry) / entry * 100.0
    out = dict(trade)
    out.update({
        "expiry_date": exit_day.isoformat(),
        "exit_date": exit_day.isoformat(),
        "exit_price": round(exit_close, 2),
        "price_change": round(change, 2),
        "price_change_pct": round(pct, 2),
        "winner": bool(winner),
        "outcome": "WIN" if winner else "LOSS",
        "expiry_winner": bool(expiry_win),
        "path_breach": bool(path_breach),
        "outcome_model": mode,
        "outcome_model_note": outcome_model_note,
        "exit_check": check + " " + breach_note,
        "days_held": (_parse_date(exit_day) - _parse_date(trade.get("entry_date"))).days,
        "strikes": trade.get("strikes") or _format_strikes(trade),
        "min_underlying_during_hold": round(min_price, 2),
        "max_underlying_during_hold": round(max_price, 2),
        "mae_down_pct": round(mae_down, 2),
        "mfe_up_pct": round(mfe_up, 2),
    })
    return _refresh_trade_labels(out)


def _close_gex_trade(trade: Dict[str, Any], daily: pd.DataFrame, entry_day: date) -> Optional[Dict[str, Any]]:
    idx = _first_bar_on_or_after(daily, entry_day)
    if idx is None:
        return None
    day = _date_at(daily, idx)
    op = _open_at(daily, idx)
    hi = _high_at(daily, idx)
    lo = _low_at(daily, idx)
    close = _close_at(daily, idx)
    if day is None or op is None or hi is None or lo is None or close is None:
        return None
    lower = _safe_float(trade.get("range_low"), None)
    upper = _safe_float(trade.get("range_high"), None)
    breakout = _safe_float(trade.get("breakout_level"), upper)
    breakdown = _safe_float(trade.get("breakdown_level"), lower)
    mode = str(trade.get("trade_type") or "GEX_RANGE").upper()
    winner = False
    check = ""
    if mode == "GEX_BREAKOUT_LONG":
        winner = close > (breakout or upper or op)
        check = f"Breakout long winner if daily close holds above {breakout}; close {close:.2f}."
    elif mode == "GEX_BREAKDOWN_SHORT":
        winner = close < (breakdown or lower or op)
        check = f"Breakdown short winner if daily close holds below {breakdown}; close {close:.2f}."
    else:
        if lower is not None and upper is not None:
            # Daily OHLC proxy for an intraday fade: pass if price closes inside the plan range and does not close outside the danger levels.
            winner = lower <= close <= upper
            check = f"Range-fade proxy winner if close stays inside {lower}-{upper}; close {close:.2f}, H/L {hi:.2f}/{lo:.2f}."
        else:
            winner = abs(close - op) / max(0.01, op) < 0.006
            check = f"Range proxy winner if daily move is muted; open/close {op:.2f}/{close:.2f}."
    entry = _safe_float(trade.get("entry_price"), op) or op
    out = dict(trade)
    out.update({
        "expiry_date": day.isoformat(),
        "exit_date": day.isoformat(),
        "exit_price": round(close, 2),
        "daily_high": round(hi, 2),
        "daily_low": round(lo, 2),
        "price_change": round(close - entry, 2),
        "price_change_pct": round((close - entry) / max(0.01, entry) * 100.0, 2),
        "winner": bool(winner),
        "outcome": "WIN" if winner else "LOSS",
        "exit_check": check,
        "days_held": 0,
    })
    return out


# ---------------------------------------------------------------------------
# Module engines
# ---------------------------------------------------------------------------

def _seller_flow_label(st: Dict[str, Any], mt: Dict[str, Any], lt: Dict[str, Any]) -> Tuple[str, str, int, str]:
    def one(w: Dict[str, Any]) -> Tuple[str, int]:
        call_pct = float(w.get("call_pct") or 0.0)
        put_pct = float(w.get("put_pct") or 0.0)
        pcr_pct = float(w.get("pcr_chg_pct") or 0.0)
        oi_pct = float(w.get("oi_pct") or 0.0)
        if call_pct < -5 and pcr_pct > 3:
            return "Bullish Call Unwind", 1
        if put_pct < -5 and pcr_pct < -3:
            return "Bearish Put Unwind", -1
        if put_pct > call_pct + 5 and pcr_pct > 3 and oi_pct > -10:
            return "Bullish Seller Flow", 1
        if call_pct > put_pct + 5 and pcr_pct < -3 and oi_pct > -10:
            return "Bearish Seller Flow", -1
        return "Mixed / Neutral", 0
    labels = [one(st), one(mt), one(lt)]
    score = sum(x[1] for x in labels)
    if score >= 2:
        return "Bullish Seller Flow", "BULL", score, ", ".join(x[0] for x in labels)
    if score <= -2:
        return "Bearish Seller Flow", "BEAR", score, ", ".join(x[0] for x in labels)
    if score > 0:
        return "Bullish / Mixed", "BULL", score, ", ".join(x[0] for x in labels)
    if score < 0:
        return "Bearish / Mixed", "BEAR", score, ", ".join(x[0] for x in labels)
    return "Mixed / Neutral", "NEUTRAL", score, ", ".join(x[0] for x in labels)


def _quality_from_score(score: float) -> Tuple[int, str]:
    if score >= 85:
        return 5, "Best"
    if score >= 70:
        return 4, "Tradable"
    if score >= 55:
        return 3, "Setup"
    if score >= 40:
        return 2, "Watch"
    return 1, "Weak"


def _seller_flow_candidates_for_day(con: sqlite3.Connection, day: date, symbols: Sequence[str], histories: Dict[str, pd.DataFrame], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    st_days = _safe_int(config.get("oi_st_days"), 5)
    mt_days = _safe_int(config.get("oi_mt_days"), 15)
    lt_days = _safe_int(config.get("oi_lt_days"), 30)
    dte = _safe_int(config.get("oi_dte"), 30)
    otm_pct = _safe_float(config.get("otm_pct"), 2.0) or 2.0
    width = _safe_float(config.get("strike_width"), 5.0) or 5.0
    min_quality = _safe_int(config.get("oi_quality_min"), 4)
    out: List[Dict[str, Any]] = []
    for sym in symbols:
        df = histories.get(sym)
        idx = _bar_idx_on_or_before(df, day)
        if df is None or idx is None or idx < max(35, lt_days + 2):
            continue
        spot = _close_at(df, idx)
        if spot is None or spot <= 0:
            continue
        rows = _aggregate_oi_by_date(con, sym, day, max_days=max(lt_days + 10, 45))
        if len(rows) < 2:
            continue
        st = _oi_window_stats(rows, st_days)
        mt = _oi_window_stats(rows, mt_days)
        lt = _oi_window_stats(rows, lt_days)
        label, direction, align, detail = _seller_flow_label(st, mt, lt)
        if direction == "NEUTRAL":
            continue
        closes = [float(x) for x in df["Close"].iloc[: idx + 1].tolist()]
        rsi14 = _rsi(closes, 14)
        rsidiff = _rsidiff90(closes)
        price_5 = _pct(closes[-1], closes[-6]) if len(closes) >= 6 else 0.0
        support = min(closes[-20:]) if len(closes) >= 20 else min(closes)
        resistance = max(closes[-20:]) if len(closes) >= 20 else max(closes)
        d_sup = round((spot - support) / max(0.01, spot) * 100.0, 2)
        d_res = round((resistance - spot) / max(0.01, spot) * 100.0, 2)
        # Guardrail: do not chase bearish at support or bullish at resistance.
        guard = ""
        guard_penalty = 0.0
        if direction == "BEAR" and (d_sup < 1.0 or (rsi14 is not None and rsi14 < 35) or (rsidiff is not None and rsidiff < -18)):
            guard = "bearish flow near support/exhaustion"
            guard_penalty = 18.0
        if direction == "BULL" and (d_res < 1.0 or (rsi14 is not None and rsi14 > 70) or (rsidiff is not None and rsidiff > 18)):
            guard = "bullish flow near resistance/exhaustion"
            guard_penalty = 18.0
        base = 45 + abs(align) * 11 + min(18, abs(float(lt.get("oi_pct") or 0)) * 0.35) + min(14, abs(float(lt.get("pcr_chg_pct") or 0)) * 0.18)
        if direction == "BULL" and price_5 >= -2:
            base += 6
        if direction == "BEAR" and price_5 <= 2:
            base += 6
        q_score = round(_clamp(base - guard_penalty, 1, 99), 1)
        q_level, q_label = _quality_from_score(q_score)
        if q_level < min_quality:
            continue
        strategy = "PS" if direction == "BULL" else "CS"
        if guard:
            # Keep it as a signal but mark it conditional if user allows Setup+.
            strategy = "WAIT"
        target_calendar = _calendar_target_date(day, dte)
        target_expiry = _nearest_option_expiry(con, sym, day, target_calendar)
        actual_dte = max(1, (target_expiry - day).days)
        trade = _build_spread(sym, "PS" if direction == "BULL" else "CS", spot, actual_dte, day, width, otm_pct)
        trade["target_expiry_date"] = target_expiry.isoformat()
        trade["expiry_date"] = target_expiry.isoformat()
        _refresh_trade_labels(trade)
        trade.update({
            "module": "Seller Flow",
            "strategy": strategy,
            "trade_type": trade.get("trade_type") if strategy != "WAIT" else ("PS" if direction == "BULL" else "CS"),
            "direction": direction,
            "seller_flow_label": label,
            "quality_score": q_score,
            "quality_level": q_level,
            "quality_label": q_label,
            "entry_rule": f"Seller Flow {QUALITY_LABELS.get(min_quality)} threshold; ST/MT/LT {st_days}/{mt_days}/{lt_days}; {detail}",
            "reason": [
                f"{label}; ST OI {st['oi_pct']}%, PCR {st['pcr_chg_pct']}%; MT OI {mt['oi_pct']}%, PCR {mt['pcr_chg_pct']}%; LT OI {lt['oi_pct']}%, PCR {lt['pcr_chg_pct']}%.",
                f"Price {spot:.2f}; RSI {(round(rsi14, 1) if rsi14 is not None else 'n/a')}; support distance {d_sup}%, resistance distance {d_res}%.",
                f"Guardrail: {guard or 'none'}."
            ],
            "guardrail": guard,
            "rsi14": round(rsi14, 2) if rsi14 is not None else None,
            "rsidiff90": round(rsidiff, 2) if rsidiff is not None else None,
            "distance_from_support_pct": d_sup,
            "distance_from_resistance_pct": d_res,
            "st_oi_pct": st.get("oi_pct"),
            "mt_oi_pct": mt.get("oi_pct"),
            "lt_oi_pct": lt.get("oi_pct"),
            "st_pcr_chg_pct": st.get("pcr_chg_pct"),
            "mt_pcr_chg_pct": mt.get("pcr_chg_pct"),
            "lt_pcr_chg_pct": lt.get("pcr_chg_pct"),
        })
        _refresh_trade_labels(trade)
        out.append(trade)
    out.sort(key=lambda x: float(x.get("quality_score") or 0), reverse=True)
    return out


def _weekly_plan_candidates_for_day(con: sqlite3.Connection, day: date, symbols: Sequence[str], histories: Dict[str, pd.DataFrame], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    run_weekday = _weekday_index(config.get("weekly_run_weekday"), 0)
    if day.weekday() != run_weekday:
        return []
    max_symbols = max(1, _safe_int(config.get("weekly_max_symbols"), 10))
    target = _next_friday(day)
    min_score = _safe_float(config.get("weekly_min_score"), 55.0) or 55.0
    width_default = _safe_float(config.get("strike_width"), 5.0) or 5.0
    out: List[Dict[str, Any]] = []
    for sym in symbols[:max_symbols]:
        df = histories.get(sym)
        idx = _bar_idx_on_or_before(df, day)
        if df is None or idx is None or idx < 60:
            continue
        spot = _close_at(df, idx)
        if spot is None or spot <= 0:
            continue
        dte = max(1, (target - day).days)
        rows = _strike_rows_for_snapshot(con, sym, day, max_expiry=target)
        walls = _oi_walls(rows, spot, band_pct=0.08) if rows else {}
        put_wall = walls.get("put_wall") or _round_strike(spot * 0.98, _strike_interval(spot))
        call_wall = walls.get("call_wall") or _round_strike(spot * 1.02, _strike_interval(spot))
        max_pain = _max_pain(rows) if rows else None
        closes = [float(x) for x in df["Close"].iloc[: idx + 1].tolist()]
        hv = _hist_vol(closes[-70:], 20)
        em = round(spot * hv * math.sqrt(max(1, dte) / 252.0), 2)
        range_low = round(spot - em, 2)
        range_high = round(spot + em, 2)
        rsi14 = _rsi(closes, 14)
        ma20 = statistics.mean(closes[-20:]) if len(closes) >= 20 else spot
        sd20 = statistics.pstdev(closes[-20:]) if len(closes) >= 20 else spot * 0.02
        bb_upper = ma20 + 2 * sd20
        bb_lower = ma20 - 2 * sd20
        near_upper = spot >= bb_upper * 0.995
        near_lower = spot <= bb_lower * 1.005
        two_sided = put_wall < spot < call_wall
        em_inside = range_low >= put_wall and range_high <= call_wall if put_wall and call_wall else False
        call_heavy = (walls.get("call_oi") or 0) > (walls.get("put_oi") or 0) * 1.15
        put_heavy = (walls.get("put_oi") or 0) > (walls.get("call_oi") or 0) * 1.15
        strategy = "IC"
        score = 55.0
        why = []
        if two_sided:
            score += 12
            why.append("two-sided OI walls bracket spot")
        if em_inside:
            score += 12
            why.append("expected move contained inside weekly walls")
        if near_upper or call_heavy:
            strategy = "CS"
            score += 8
            why.append("near upper band/call-heavy weekly OI")
        if near_lower or put_heavy:
            strategy = "PS"
            score += 8
            why.append("near lower band/put-heavy weekly OI")
        if two_sided and not near_upper and not near_lower and not call_heavy and not put_heavy:
            strategy = "IC"
        score = round(_clamp(score, 1, 99), 1)
        if score < min_score:
            continue
        interval = _strike_interval(spot)
        width = max(interval, width_default)
        if strategy == "IC":
            trade = _build_spread(sym, "IC", spot, dte, day, width, max(0.5, em / max(0.01, spot) * 100.0))
            if put_wall and call_wall:
                trade["short_put"] = _round_strike(min(float(put_wall), spot - interval), interval)
                trade["long_put"] = max(interval, _round_strike(trade["short_put"] - width, interval))
                trade["short_call"] = _round_strike(max(float(call_wall), spot + interval), interval)
                trade["long_call"] = _round_strike(trade["short_call"] + width, interval)
        elif strategy == "CS":
            trade = _build_spread(sym, "CS", spot, dte, day, width, max(0.5, (call_wall - spot) / max(0.01, spot) * 100.0 if call_wall else 2.0))
            if call_wall:
                trade["short_call"] = _round_strike(max(float(call_wall), spot + interval), interval)
                trade["long_call"] = _round_strike(trade["short_call"] + width, interval)
        else:
            trade = _build_spread(sym, "PS", spot, dte, day, width, max(0.5, (spot - put_wall) / max(0.01, spot) * 100.0 if put_wall else 2.0))
            if put_wall:
                trade["short_put"] = _round_strike(min(float(put_wall), spot - interval), interval)
                trade["long_put"] = max(interval, _round_strike(trade["short_put"] - width, interval))
        trade.update({
            "module": "Weekly Plan",
            "strategy": strategy,
            "trade_type": strategy,
            "entry_date": day.isoformat(),
            "target_expiry_date": target.isoformat(),
            "dte": dte,
            "quality_score": score,
            "quality_level": _quality_from_score(score)[0],
            "quality_label": _quality_from_score(score)[1],
            "direction": "NEUTRAL" if strategy == "IC" else ("BEAR" if strategy == "CS" else "BULL"),
            "entry_rule": f"Weekly Plan run weekday {run_weekday}; target Friday {target.isoformat()}.",
            "expected_move": em,
            "expected_range_low": range_low,
            "expected_range_high": range_high,
            "put_wall": put_wall,
            "call_wall": call_wall,
            "max_pain": max_pain,
            "pcr": walls.get("pcr"),
            "rsi14": round(rsi14, 2) if rsi14 is not None else None,
            "reason": [
                f"Weekly strategy {strategy}; score {score}. {'; '.join(why) or 'basic weekly setup' }.",
                f"Spot {spot:.2f}; put wall {put_wall}; call wall {call_wall}; max pain {max_pain}; expected move ±{em}.",
                f"Daily BB context: upper {bb_upper:.2f}, lower {bb_lower:.2f}, near_upper={near_upper}, near_lower={near_lower}."
            ],
        })
        _refresh_trade_labels(trade)
        out.append(trade)
    out.sort(key=lambda x: float(x.get("quality_score") or 0), reverse=True)
    return out


def _gex_plan_candidates_for_day(con: sqlite3.Connection, day: date, symbols: Sequence[str], histories: Dict[str, pd.DataFrame], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    min_strength = _safe_float(config.get("gex_min_strength_pct"), 8.0) or 8.0
    mode = str(config.get("gex_trade_mode") or "auto").strip().lower()
    run_time = str(config.get("gex_run_time") or "10:00").strip()
    out: List[Dict[str, Any]] = []
    for sym in symbols:
        df = histories.get(sym)
        idx = _bar_idx_on_or_before(df, day)
        if df is None or idx is None or idx < 25:
            continue
        spot = _open_at(df, idx) or _close_at(df, idx)
        if spot is None or spot <= 0:
            continue
        rows = _strike_rows_for_snapshot(con, sym, day, expiry=day)
        if not rows:
            rows = _strike_rows_for_snapshot(con, sym, day, max_expiry=day + timedelta(days=2))
        if not rows:
            continue
        walls = _oi_walls(rows, spot, band_pct=0.05)
        total_oi = float(walls.get("total_oi") or 0.0)
        if total_oi <= 0:
            continue
        top_near = float(max(walls.get("put_wall_oi") or 0, walls.get("call_wall_oi") or 0))
        strength = round(top_near / max(1.0, total_oi) * 100.0, 2)
        if strength < min_strength:
            continue
        put_wall = walls.get("put_wall")
        call_wall = walls.get("call_wall")
        max_pain = _max_pain(rows)
        lower = put_wall or max_pain or round(spot * 0.995, 2)
        upper = call_wall or max_pain or round(spot * 1.005, 2)
        if lower and upper and lower > upper:
            lower, upper = upper, lower
        pcr = float(walls.get("pcr") or 0.0)
        selected = "GEX_RANGE_FADE"
        if mode == "breakout":
            selected = "GEX_BREAKOUT_LONG"
        elif mode == "breakdown":
            selected = "GEX_BREAKDOWN_SHORT"
        elif mode == "auto":
            if pcr > 1.8 and max_pain and spot < max_pain:
                selected = "GEX_BREAKDOWN_SHORT"
            elif pcr < 0.65 and max_pain and spot > max_pain:
                selected = "GEX_BREAKOUT_LONG"
        score = 50 + min(30, strength * 1.6)
        if selected == "GEX_RANGE_FADE" and 0.7 <= pcr <= 1.8:
            score += 8
        score = round(_clamp(score, 1, 99), 1)
        trade = {
            "module": "GEX Plan",
            "symbol": sym,
            "trade_type": selected,
            "strategy": selected.replace("GEX_", ""),
            "entry_date": day.isoformat(),
            "target_expiry_date": day.isoformat(),
            "expiry_date": day.isoformat(),
            "dte": 0,
            "entry_price": round(float(spot), 2),
            "range_low": round(float(lower), 2) if lower else None,
            "range_high": round(float(upper), 2) if upper else None,
            "breakout_level": round(float(upper), 2) if upper else None,
            "breakdown_level": round(float(lower), 2) if lower else None,
            "gamma_flip": max_pain,
            "max_pain": max_pain,
            "pcr": round(pcr, 3),
            "gex_strength_pct": strength,
            "gex_run_time": run_time,
            "quality_score": score,
            "quality_level": _quality_from_score(score)[0],
            "quality_label": _quality_from_score(score)[1],
            "direction": "NEUTRAL" if selected == "GEX_RANGE_FADE" else ("BULL" if selected == "GEX_BREAKOUT_LONG" else "BEAR"),
            "entry_rule": f"GEX daily plan at {run_time}; daily OHLC proxy; min strength {min_strength}%.",
            "reason": [
                f"GEX plan {selected}; strength {strength}% from top near wall / total OI.",
                f"Range {lower}-{upper}; max pain/gamma flip proxy {max_pain}; PCR {pcr}.",
                "Daily-bar backtest approximates the intraday GEX plan; add intraday bars for precise 1H confirmation tests."
            ],
            "strikes": f"Range {lower}-{upper}",
        }
        out.append(trade)
    out.sort(key=lambda x: float(x.get("quality_score") or 0), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Runner, summaries, routes
# ---------------------------------------------------------------------------

def _summaries(trades: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_strategy: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_module: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_symbol[str(t.get("symbol") or "?")].append(t)
        by_strategy[str(t.get("strategy") or t.get("trade_type") or "?")].append(t)
        by_module[str(t.get("module") or "?")].append(t)

    def one(rows: Sequence[Dict[str, Any]], extra: Dict[str, Any]) -> Dict[str, Any]:
        total = len(rows)
        wins = sum(1 for r in rows if r.get("winner"))
        losses = total - wins
        q = [float(r.get("quality_score") or 0) for r in rows]
        moves = [float(r.get("price_change_pct") or 0) for r in rows if _safe_float(r.get("price_change_pct"), None) is not None]
        return {
            **extra,
            "trades": total,
            "winners": wins,
            "losers": losses,
            "win_rate": round(wins / total * 100.0, 2) if total else 0.0,
            "avg_price_change_pct": round(statistics.mean(moves), 2) if moves else 0.0,
            "best_price_change_pct": round(max(moves), 2) if moves else 0.0,
            "worst_price_change_pct": round(min(moves), 2) if moves else 0.0,
            "avg_quality_score": round(statistics.mean(q), 1) if q else 0.0,
        }
    symbol_perf = sorted([one(v, {"symbol": k}) for k, v in by_symbol.items()], key=lambda r: (r["win_rate"], r["trades"]), reverse=True)
    strat_perf = sorted([one(v, {"strategy": k}) for k, v in by_strategy.items()], key=lambda r: (r["win_rate"], r["trades"]), reverse=True)
    module_perf = sorted([one(v, {"strategy": k}) for k, v in by_module.items()], key=lambda r: (r["win_rate"], r["trades"]), reverse=True)
    return symbol_perf, strat_perf, module_perf


def _active_overlap(candidate: Dict[str, Any], existing: Sequence[Dict[str, Any]], day: date, mode: str = "symbol") -> Optional[Dict[str, Any]]:
    scope = str(mode or "symbol").strip().lower()
    if scope in {"allow", "none", "off"}:
        return None
    sym = str(candidate.get("symbol") or "")
    mod = str(candidate.get("module") or "")
    strat = str(candidate.get("strategy") or candidate.get("trade_type") or "")
    for t in existing:
        try:
            ed = _parse_date(t.get("entry_date"))
            xd = _parse_date(t.get("exit_date") or t.get("expiry_date") or t.get("target_expiry_date"))
        except Exception:
            continue
        if not (ed <= day < xd):
            continue
        if str(t.get("symbol") or "") != sym:
            continue
        if scope in {"symbol", "same_symbol"}:
            return t
        if scope in {"symbol_module", "same_symbol_module"} and str(t.get("module") or "") == mod:
            return t
        if scope in {"symbol_strategy", "same_symbol_strategy"} and str(t.get("strategy") or t.get("trade_type") or "") == strat:
            return t
    return None


def _active_count(existing: Sequence[Dict[str, Any]], day: date) -> int:
    n = 0
    for t in existing:
        try:
            if _parse_date(t.get("entry_date")) <= day < _parse_date(t.get("exit_date") or t.get("expiry_date") or t.get("target_expiry_date")):
                n += 1
        except Exception:
            pass
    return n


def _signal_record(candidate: Dict[str, Any], day: date, status: str, reason: str = "") -> Dict[str, Any]:
    return {
        "date": day.isoformat(),
        "module": candidate.get("module"),
        "symbol": candidate.get("symbol"),
        "strategy": candidate.get("strategy") or candidate.get("trade_type"),
        "trade_type": candidate.get("trade_type"),
        "trade_description": candidate.get("trade_description") or _trade_description(candidate),
        "strikes": candidate.get("strikes") or _format_strikes(candidate),
        "entry_price": candidate.get("entry_price"),
        "target_expiry_date": candidate.get("target_expiry_date") or candidate.get("expiry_date"),
        "quality_score": candidate.get("quality_score"),
        "quality_level": candidate.get("quality_level"),
        "quality_label": candidate.get("quality_label"),
        "seller_flow_label": candidate.get("seller_flow_label"),
        "direction": candidate.get("direction"),
        "status": status,
        "status_reason": reason,
        "reason": candidate.get("reason"),
        "guardrail": candidate.get("guardrail"),
    }


def _stats(trades: Sequence[Dict[str, Any]], daily_log: Sequence[Dict[str, Any]], skipped: Sequence[Dict[str, Any]], signal_log: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    total = len(trades)
    wins = sum(1 for t in trades if t.get("winner"))
    losses = total - wins
    moves = [float(t.get("price_change_pct") or 0) for t in trades if _safe_float(t.get("price_change_pct"), None) is not None]
    breaches = sum(1 for t in trades if t.get("path_breach"))
    sigs = list(signal_log or [])
    return {
        "total_trades": total,
        "closed_trades": total,
        "winners": wins,
        "losers": losses,
        "win_rate": round(wins / total * 100.0, 2) if total else 0.0,
        "avg_price_change_pct": round(statistics.mean(moves), 2) if moves else 0.0,
        "best_price_change_pct": round(max(moves), 2) if moves else 0.0,
        "worst_price_change_pct": round(min(moves), 2) if moves else 0.0,
        "days_processed": len(daily_log),
        "signals_found": len(sigs) if sigs else sum(int(x.get("signals") or 0) for x in daily_log),
        "signals_entered": sum(1 for x in sigs if x.get("status") == "ENTERED") if sigs else total,
        "signals_not_entered": sum(1 for x in sigs if x.get("status") != "ENTERED") if sigs else len(skipped),
        "path_breaches": breaches,
        "skipped_signals": len(skipped),
        "open_trades": 0,
    }


def run_replay_lab(config: Dict[str, Any]) -> Dict[str, Any]:
    end = _parse_date(config.get("end_date"), _today())
    start = _parse_date(config.get("start_date"), end - timedelta(days=31))
    if start >= end:
        raise ValueError("Start date must be before end date")
    modules = [str(x).strip().lower() for x in (config.get("modules") or []) if str(x).strip()]
    if not modules:
        modules = ["seller_flow", "weekly_plan", "gex_plan"]
    watchlist_id, symbols = _symbols_from_payload(config)
    if not symbols:
        raise ValueError("No symbols found for selected watchlist or symbol list")
    max_symbols = _safe_int(config.get("max_symbols"), 0)
    if max_symbols > 0:
        symbols = symbols[:max_symbols]
    weekly_symbols = _parse_symbols_csv(config.get("weekly_symbols")) or [s for s in symbols if s in {"SPY", "QQQ", "IWM", "DIA"}] or symbols[:5]
    gex_symbols = _parse_symbols_csv(config.get("gex_symbols")) or ["SPY"]
    all_price_symbols = list(dict.fromkeys(symbols + weekly_symbols + gex_symbols + ["SPY", "^VIX"]))
    # Load future bars beyond the replay decision window for outcome scoring only.
    # Signal generation still slices each history as-of the replay day.
    max_horizon_days = max(7, _safe_int(config.get("oi_dte"), 30) + 14)
    price_end = end + timedelta(days=max_horizon_days)
    provider = str(config.get("data_provider") or "auto").strip().lower()
    if provider not in {"auto", "sqlite", "yfinance"}:
        provider = "auto"
    auto_fetch = _as_bool(config.get("auto_fetch"), True)
    histories, errors = _load_histories(all_price_symbols, start, price_end, provider, auto_fetch)
    if not histories:
        raise ValueError("No usable historical price bars found")
    days = _trading_days_from_histories({k: v for k, v in histories.items() if k in set(symbols + weekly_symbols + gex_symbols)}, start, end)
    if not days:
        raise ValueError("No trading days found in selected range")
    max_trades_per_day = max(1, _safe_int(config.get("max_trades_per_day"), 8))
    allow_same_symbol_same_day = _as_bool(config.get("allow_same_symbol_same_day"), False)
    outcome_model = str(config.get("outcome_model") or "touch_stop").strip().lower()
    stop_buffer_pct = _safe_float(config.get("stop_buffer_pct"), 0.0) or 0.0
    overlap_mode = str(config.get("overlap_mode") or "symbol").strip().lower()
    max_open_positions = max(1, _safe_int(config.get("max_open_positions"), 12))
    save_all_signals = _as_bool(config.get("save_all_signals"), True)
    signal_cap = max(1000, _safe_int(config.get("signal_log_cap"), 25000))
    trades: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    signal_log: List[Dict[str, Any]] = []
    daily_log: List[Dict[str, Any]] = []

    con = _connect_options()
    try:
        if not _table_exists(con, "options"):
            raise ValueError("options table not found; replay modules need historical options/OI snapshots")
        for day in days:
            day_candidates: List[Dict[str, Any]] = []
            if "seller_flow" in modules or "oi" in modules or "oi_buildup" in modules:
                day_candidates.extend(_seller_flow_candidates_for_day(con, day, symbols, histories, config))
            if "weekly_plan" in modules or "weekly" in modules:
                day_candidates.extend(_weekly_plan_candidates_for_day(con, day, weekly_symbols, histories, config))
            if "gex_plan" in modules or "gex" in modules:
                day_candidates.extend(_gex_plan_candidates_for_day(con, day, gex_symbols, histories, config))
            day_candidates.sort(key=lambda x: (float(x.get("quality_score") or 0), str(x.get("module") or "")), reverse=True)
            if save_all_signals and len(signal_log) < signal_cap:
                for c in day_candidates[: max(0, signal_cap - len(signal_log))]:
                    signal_log.append(_signal_record(c, day, "FOUND", "candidate generated by module"))
            if not allow_same_symbol_same_day:
                seen = set()
                filtered = []
                for c in day_candidates:
                    key = (c.get("symbol"), c.get("module"))
                    if key in seen:
                        if save_all_signals and len(signal_log) < signal_cap:
                            signal_log.append(_signal_record(c, day, "SKIPPED", "duplicate symbol/module on same replay day"))
                        continue
                    seen.add(key)
                    filtered.append(c)
                day_candidates = filtered
            selected: List[Dict[str, Any]] = []
            for c in day_candidates:
                if len(selected) >= max_trades_per_day:
                    if save_all_signals and len(signal_log) < signal_cap:
                        signal_log.append(_signal_record(c, day, "NOT_SELECTED", "daily max trade limit reached"))
                    continue
                if _active_count(trades, day) + len(selected) >= max_open_positions:
                    if save_all_signals and len(signal_log) < signal_cap:
                        signal_log.append(_signal_record(c, day, "SKIPPED", "max open positions limit reached"))
                    skipped.append({"date": day.isoformat(), "symbol": c.get("symbol"), "module": c.get("module"), "reason": "max open positions limit reached", "signal": c})
                    continue
                overlap = _active_overlap(c, trades, day, overlap_mode)
                if overlap is not None:
                    msg = f"overlap blocked by active {overlap.get('trade_description') or overlap.get('symbol')} until {overlap.get('exit_date') or overlap.get('expiry_date')}"
                    if save_all_signals and len(signal_log) < signal_cap:
                        signal_log.append(_signal_record(c, day, "SKIPPED", msg))
                    skipped.append({"date": day.isoformat(), "symbol": c.get("symbol"), "module": c.get("module"), "reason": msg, "signal": c})
                    continue
                selected.append(c)
            closed = 0
            for cand in selected:
                sym = str(cand.get("symbol") or "")
                df = histories.get(sym)
                if df is None or df.empty:
                    skipped.append({"date": day.isoformat(), "symbol": sym, "module": cand.get("module"), "reason": "missing price history for outcome"})
                    continue
                if str(cand.get("strategy") or "").upper() == "WAIT":
                    skipped.append({"date": day.isoformat(), "symbol": sym, "module": cand.get("module"), "reason": "WAIT/guardrail candidate not entered", "signal": cand})
                    if save_all_signals and len(signal_log) < signal_cap:
                        signal_log.append(_signal_record(cand, day, "SKIPPED", "WAIT/guardrail candidate not entered"))
                    continue
                if cand.get("module") == "GEX Plan":
                    result = _close_gex_trade(cand, df, day)
                else:
                    target = _parse_date(cand.get("target_expiry_date") or cand.get("expiry_date") or cand.get("entry_date"))
                    result = _close_trade_with_daily_bars(cand, df, target, outcome_mode=outcome_model, stop_buffer_pct=stop_buffer_pct)
                if result is None:
                    skipped.append({"date": day.isoformat(), "symbol": sym, "module": cand.get("module"), "reason": "not enough future bars to close trade", "signal": cand})
                    if save_all_signals and len(signal_log) < signal_cap:
                        signal_log.append(_signal_record(cand, day, "SKIPPED", "not enough future bars to close trade"))
                    continue
                result["trade_no"] = len(trades) + 1
                _refresh_trade_labels(result)
                trades.append(result)
                if save_all_signals and len(signal_log) < signal_cap:
                    sr = _signal_record(result, day, "ENTERED", result.get("outcome") or "outcome recorded")
                    sr["outcome"] = result.get("outcome")
                    sr["exit_date"] = result.get("exit_date")
                    signal_log.append(sr)
                closed += 1
            daily_log.append({
                "date": day.isoformat(),
                "signals": len(day_candidates),
                "entries": len(selected),
                "closed_outcomes": closed,
                "active_after_day": _active_count(trades, day),
                "modules": modules,
            })
    finally:
        con.close()

    symbol_perf, strat_perf, module_perf = _summaries(trades)
    run_id = str(uuid.uuid4())
    run_name = str(config.get("run_name") or f"Replay Lab {start.isoformat()} to {end.isoformat()}").strip()[:160]
    result = {
        "ok": True,
        "run_id": run_id,
        "run_name": run_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "engine": "replay_lab_modules_v102_path_risk_overlap",
        "config": {
            "modules": modules,
            "watchlist_id": watchlist_id,
            "symbol": _clean_symbol(config.get("symbol")) or None,
            "symbols": symbols,
            "weekly_symbols": weekly_symbols,
            "gex_symbols": gex_symbols,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "oi_quality_min": _safe_int(config.get("oi_quality_min"), 4),
            "oi_st_days": _safe_int(config.get("oi_st_days"), 5),
            "oi_mt_days": _safe_int(config.get("oi_mt_days"), 15),
            "oi_lt_days": _safe_int(config.get("oi_lt_days"), 30),
            "oi_dte": _safe_int(config.get("oi_dte"), 30),
            "weekly_run_weekday": _weekday_index(config.get("weekly_run_weekday"), 0),
            "weekly_min_score": _safe_float(config.get("weekly_min_score"), 55.0),
            "gex_run_time": str(config.get("gex_run_time") or "10:00"),
            "gex_trade_mode": str(config.get("gex_trade_mode") or "auto"),
            "gex_min_strength_pct": _safe_float(config.get("gex_min_strength_pct"), 8.0),
            "max_trades_per_day": max_trades_per_day,
            "max_symbols": max_symbols,
            "data_provider": provider,
            "auto_fetch": auto_fetch,
            "universe_count": len(symbols),
            "price_history_loaded_through": price_end.isoformat(),
            "outcome_model": outcome_model,
            "stop_buffer_pct": stop_buffer_pct,
            "overlap_mode": overlap_mode,
            "max_open_positions": max_open_positions,
            "save_all_signals": save_all_signals,
        },
        "stats": _stats(trades, daily_log, skipped, signal_log),
        "trades": trades,
        "signals": signal_log[:signal_cap],
        "open_trades": [],
        "symbol_performance": symbol_perf,
        "strategy_summary": strat_perf,
        "strategy_symbol_summary": module_perf,
        "module_summary": module_perf,
        "daily_log": daily_log[-1000:],
        "skipped": skipped[:1000],
        "errors": errors[:200],
        "notes": [
            "Replay Lab runs selected modules day-by-day and records every selected trade candidate with a winner/loser outcome.",
            "Seller Flow uses historical options OI/PCR snapshots as-of each date and applies the selected 1-5 quality threshold.",
            "Weekly Plan uses a daily-bar proxy for Monday-to-Friday style plans, cumulative OI walls through the target Friday, expected move, and configurable expiry/path-risk rules.",
            "GEX Plan uses a daily OHLC proxy. It records the requested run time but exact 1H confirmation requires intraday bars in a future phase.",
            "v102 fixes Seller Flow same-day close bias by assigning a real target expiry/DTE and can block overlapping positions so one symbol is not re-entered every day while a prior trade is still open.",
            "Saved replay runs are stored in the same local SQLite backtest store as the generic backtest page, with full trade JSON and rationale.",
        ],
    }
    result = _json_safe(result)
    if _as_bool(config.get("save_run"), True):
        save = get_market_data_store().save_backtest_result(result, run_name)
        result["saved"] = bool(save.get("ok"))
        result["save_result"] = save
    return result


@replay_lab_bp.route("/api/watchlists", methods=["GET"])
def api_replay_watchlists():
    _scanner_ensure_tables()
    return jsonify({"watchlists": _watchlists(), "preferred_watchlist_id": _preferred_watchlist_id()})


@replay_lab_bp.route("/api/run", methods=["POST"])
def api_replay_run():
    payload = request.get_json(force=True) or {}
    try:
        return jsonify(run_replay_lab(payload))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Replay Lab failed: {exc}"}), 500


@replay_lab_bp.route("/api/runs", methods=["GET"])
def api_replay_runs():
    try:
        limit = int(request.args.get("limit") or 50)
    except Exception:
        limit = 50
    result = get_market_data_store().list_backtest_runs(limit=limit)
    if result.get("ok"):
        runs = result.get("runs") or []
        # Put Replay Lab runs first and keep newest saved runs at the top.
        replay = [r for r in runs if str(r.get("engine") or "").startswith("replay_lab")]
        other = [r for r in runs if not str(r.get("engine") or "").startswith("replay_lab")]
        replay.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        other.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        result["runs"] = replay + other
    return jsonify(result)


@replay_lab_bp.route("/api/runs/<run_id>", methods=["GET"])
def api_replay_run_detail(run_id: str):
    result = get_market_data_store().get_backtest_run(run_id)
    code = 200 if result.get("ok") else 404 if result.get("error") == "Run not found" else 503
    return jsonify(result), code
