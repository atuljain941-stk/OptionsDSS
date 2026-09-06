"""Schwab EOD broker-resident auto-trading workflow.

This module avoids realtime quote processing.  It scans completed daily candles,
creates a next-session short entry stop/stop-limit order, and attaches broker-
resident OCO exits so Schwab monitors the trigger and closing orders.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, jsonify, render_template, request

from ..db import _connect
from .ema5_strategy import build_eod_short_plan, evaluate_latest_signal, simple_backtest_short_stop_entry

schwab_eod_bp = Blueprint("schwab_eod_bp", __name__, url_prefix="/auto-trading")

_DB_LOCK = threading.RLock()
_INIT_DONE = False
_LIVE_ENV = "OIAPP_AUTOTRADE_LIVE_ORDERS"


# ------------------------------- DB helpers -------------------------------

def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


def _ensure_tables() -> None:
    global _INIT_DONE
    if _INIT_DONE:
        return
    with _DB_LOCK:
        if _INIT_DONE:
            return
        con = _connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS auto_trading_eod_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    mode TEXT DEFAULT 'SCHWAB_EOD_STOP_OCO',
                    watchlist_id INTEGER,
                    watchlist_name TEXT,
                    symbols_json TEXT,
                    params_json TEXT,
                    status TEXT DEFAULT 'CREATED',
                    candidate_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    submitted_count INTEGER DEFAULT 0,
                    notes TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_auto_eod_runs_created ON auto_trading_eod_runs(created_at DESC);
                CREATE TABLE IF NOT EXISTS auto_trading_eod_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    signal_time TEXT,
                    valid INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'CANDIDATE',
                    qty INTEGER DEFAULT 0,
                    entry_stop REAL,
                    entry_limit REAL,
                    stop_loss REAL,
                    target REAL,
                    risk_per_share REAL,
                    reward_per_share REAL,
                    risk_dollars REAL,
                    rr REAL,
                    body_ratio REAL,
                    ema5 REAL,
                    signal_json TEXT,
                    plan_json TEXT,
                    order_json TEXT,
                    schwab_order_id TEXT,
                    schwab_status TEXT,
                    error TEXT,
                    reason TEXT,
                    source TEXT,
                    bars INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(run_id, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_auto_eod_candidates_run ON auto_trading_eod_candidates(run_id, status);
                CREATE TABLE IF NOT EXISTS auto_trading_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    symbol TEXT,
                    message TEXT DEFAULT '',
                    metadata TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_auto_trading_events_created ON auto_trading_events(created_at DESC);
                """
            )
            cols = {str(r[1]).lower() for r in con.execute("PRAGMA table_info(auto_trading_eod_candidates)").fetchall()}
            migrations = {
                "reason": "ALTER TABLE auto_trading_eod_candidates ADD COLUMN reason TEXT",
                "source": "ALTER TABLE auto_trading_eod_candidates ADD COLUMN source TEXT",
                "bars": "ALTER TABLE auto_trading_eod_candidates ADD COLUMN bars INTEGER DEFAULT 0",
            }
            for col, sql in migrations.items():
                if col not in cols:
                    try:
                        con.execute(sql)
                    except Exception:
                        pass
            con.commit()
            _INIT_DONE = True
        finally:
            con.close()


def _write_with_retry(fn, retries: int = 8, base_sleep: float = 0.15):
    _ensure_tables()
    last_exc = None
    for attempt in range(max(1, int(retries))):
        try:
            with _DB_LOCK:
                con = _connect()
                try:
                    con.execute("BEGIN IMMEDIATE")
                    result = fn(con)
                    con.commit()
                    return result
                except Exception:
                    try:
                        con.rollback()
                    except Exception:
                        pass
                    raise
                finally:
                    con.close()
        except Exception as exc:
            last_exc = exc
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            time.sleep(base_sleep * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _log_event(event_type: str, symbol: str = "", message: str = "", metadata: Any = None) -> None:
    try:
        def _op(con):
            con.execute(
                "INSERT INTO auto_trading_events(created_at,event_type,symbol,message,metadata) VALUES (?,?,?,?,?)",
                (_now(), event_type, str(symbol or "").upper(), str(message or ""), _json_dumps(metadata or {})),
            )
        _write_with_retry(_op, retries=3)
    except Exception:
        pass


# ----------------------------- Schwab helpers ------------------------------

def _live_enabled() -> bool:
    return os.getenv(_LIVE_ENV, "0").strip().lower() in {"1", "true", "yes", "on"}


def _schwab_cfg_headers() -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]], Optional[str]]:
    try:
        from ..schwab.schwab_routes import _get_config, _headers
        cfg = _get_config() or {}
        headers = _headers()
        acct = (cfg or {}).get("account_hash") or ""
        return cfg, headers, acct
    except Exception:
        return None, None, None


def _schwab_request(method: str, path: str, *, params: Dict[str, Any] = None, payload: Any = None, timeout: int = 20):
    import requests
    try:
        from ..schwab.schwab_routes import BASE
    except Exception:
        BASE = "https://api.schwabapi.com"
    cfg, headers, _acct = _schwab_cfg_headers()
    if not headers:
        return {"error": "Schwab is not authenticated. Connect or refresh Schwab first."}, 401
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")
    if method.upper() == "GET":
        # Schwab's market-data GET endpoints do not need Content-Type.  Some
        # gateways are picky about request headers, so keep GETs lean.
        headers.pop("Content-Type", None)
    url = path if str(path).startswith("http") else f"{BASE}{path}"
    r = requests.request(method.upper(), url, headers=headers, params=params or None, json=payload, timeout=timeout)
    if r.status_code == 401:
        # Try the app's existing refresh helper once; it already knows how the
        # local token is stored.
        try:
            from ..services.futures_oi_schwab import refresh_schwab_access_token
            rr = refresh_schwab_access_token()
            if rr and rr.get("ok"):
                _cfg2, headers2, _acct2 = _schwab_cfg_headers()
                if headers2:
                    headers2 = dict(headers2 or {})
                    headers2.setdefault("Accept", "application/json")
                    if method.upper() == "GET":
                        headers2.pop("Content-Type", None)
                    r = requests.request(method.upper(), url, headers=headers2, params=params or None, json=payload, timeout=timeout)
        except Exception:
            pass
    if r.status_code in (200, 201, 202, 204):
        if r.text:
            try:
                data = r.json()
            except Exception:
                data = {"text": r.text[:1000]}
        else:
            data = {"ok": True}
        return {"ok": True, "data": data, "headers": dict(r.headers), "status_code": r.status_code}, r.status_code
    try:
        detail = r.json()
    except Exception:
        detail = (r.text or "")[:2000]
    return {"error": f"Schwab API error {r.status_code}", "detail": detail, "status_code": r.status_code}, r.status_code


def _compact_error_detail(detail: Any, limit: int = 600) -> str:
    """Return a compact user-facing API/data error string."""
    try:
        if isinstance(detail, dict):
            # Schwab errors often include message/error/description fields.
            for key in ("message", "error_description", "description", "error"):
                val = detail.get(key)
                if val:
                    return str(val)[:limit]
            return json.dumps(detail, default=str, separators=(",", ":"))[:limit]
        if isinstance(detail, list):
            return json.dumps(detail, default=str, separators=(",", ":"))[:limit]
        return str(detail or "")[:limit]
    except Exception:
        return str(detail or "")[:limit]


def _schwab_error_string(data: Dict[str, Any], code: int, attempt: str = "") -> str:
    base = str(data.get("error") or f"Schwab API error {code}")
    detail = _compact_error_detail(data.get("detail"), 700)
    prefix = f"{attempt}: " if attempt else ""
    if detail and detail not in base:
        return f"{prefix}{base} - {detail}"
    return f"{prefix}{base}"


def _parse_schwab_candles(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in raw or []:
        try:
            ts = c.get("datetime")
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(float(ts) / 1000.0).date().isoformat()
            else:
                dt = str(ts or "")
            out.append({
                "datetime": dt,
                "open": float(c.get("open")),
                "high": float(c.get("high")),
                "low": float(c.get("low")),
                "close": float(c.get("close")),
                "volume": c.get("volume"),
            })
        except Exception:
            continue
    return out


def _fetch_schwab_daily_history(symbol: str, period_years: int = 1) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Fetch daily bars from Schwab with multiple compatible parameter shapes.

    The uploaded run showed every symbol failing before signal evaluation with
    generic HTTP 400 rows.  This function now tries a small set of Schwab-legal
    request shapes and returns the real response detail instead of collapsing it
    to just "Schwab API error 400".
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return [], "blank symbol"
    years = max(1, min(10, int(period_years or 1)))
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=years * 370)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    attempts: List[Tuple[str, Dict[str, Any]]] = [
        ("period", {
            "symbol": sym,
            "periodType": "year",
            "period": years,
            "frequencyType": "daily",
            "frequency": 1,
            "needExtendedHoursData": "false",
        }),
        ("date_range", {
            "symbol": sym,
            "frequencyType": "daily",
            "frequency": 1,
            "startDate": start_ms,
            "endDate": end_ms,
            "needExtendedHoursData": "false",
        }),
        ("minimal_period", {
            "symbol": sym,
            "periodType": "year",
            "period": years,
            "frequencyType": "daily",
            "frequency": 1,
        }),
    ]
    errors: List[str] = []
    for label, params in attempts:
        data, code = _schwab_request("GET", "/marketdata/v1/pricehistory", params=params, timeout=20)
        if code == 200 and data.get("ok"):
            raw = (data.get("data") or {}).get("candles") or []
            out = _parse_schwab_candles(raw)
            if len(out) >= 5:
                return out, None
            errors.append(f"{label}: not enough price history from Schwab ({len(out)} bars)")
        else:
            errors.append(_schwab_error_string(data if isinstance(data, dict) else {"error": str(data)}, code, label))
            # Auth failures will not be fixed by other query shapes.
            if int(code or 0) == 401:
                break
    return [], "; ".join(errors[-3:]) or "Schwab price history unavailable"


def _yfinance_period_for_years(period_years: int) -> str:
    years = max(1, min(10, int(period_years or 1)))
    if years <= 1:
        return "1y"
    if years <= 2:
        return "2y"
    if years <= 5:
        return "5y"
    return "10y"


def _fetch_yfinance_daily_history(symbol: str, period_years: int = 1) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return [], "blank symbol"
    try:
        from ..services.yf_session import safe_history
        df = safe_history(sym, period=_yfinance_period_for_years(period_years), interval="1d", retries=2)
    except Exception as exc:
        return [], f"yfinance unavailable: {exc}"
    if df is None or getattr(df, "empty", True):
        return [], "no yfinance daily history"
    out: List[Dict[str, Any]] = []
    try:
        for idx, row in df.iterrows():
            try:
                dt = idx.date().isoformat() if hasattr(idx, "date") else str(idx)
                out.append({
                    "datetime": dt,
                    "open": float(row.get("Open")),
                    "high": float(row.get("High")),
                    "low": float(row.get("Low")),
                    "close": float(row.get("Close")),
                    "volume": row.get("Volume"),
                })
            except Exception:
                continue
    except Exception as exc:
        return [], f"could not parse yfinance history: {exc}"
    if len(out) < 5:
        return out, f"not enough yfinance history ({len(out)} bars)"
    return out, None


def _fetch_daily_history(symbol: str, params: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str], Dict[str, Any]]:
    source = str(params.get("data_source_effective") or params.get("data_source") or "auto").strip().lower()
    years = int(params.get("period_years") or 1)
    meta: Dict[str, Any] = {"requested_source": source, "source": source}
    if source not in {"auto", "schwab", "yfinance"}:
        source = "auto"
        meta["requested_source"] = "auto"
    if source in {"auto", "schwab"}:
        rows, err = _fetch_schwab_daily_history(symbol, years)
        if not err:
            meta.update({"source": "schwab", "bars": len(rows)})
            return rows, None, meta
        meta["schwab_error"] = err
        if source == "schwab":
            meta.update({"source": "schwab", "bars": len(rows)})
            return rows, err, meta
    rows, err = _fetch_yfinance_daily_history(symbol, years)
    if not err:
        meta.update({"source": "yfinance", "bars": len(rows), "fallback_from": meta.get("schwab_error")})
        return rows, None, meta
    if meta.get("schwab_error"):
        err = f"Schwab failed: {meta['schwab_error']}; yfinance fallback failed: {err}"
    meta.update({"source": "yfinance", "bars": len(rows), "error": err})
    return rows, err, meta

def _format_price(x: Any) -> str:
    try:
        v = float(x)
    except Exception:
        v = 0.0
    if abs(v) >= 1:
        return f"{v:.2f}"
    return f"{v:.4f}"


def build_schwab_short_stop_oco_order(plan: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Return Schwab TRIGGER parent + OCO children payload for a short equity trade."""
    symbol = str(plan.get("symbol") or "").upper()
    qty = int(plan.get("qty") or 0)
    if not symbol or qty < 1:
        raise ValueError("symbol and positive qty are required")
    entry_type = str(params.get("entry_order_type") or "STOP_LIMIT").upper()
    stop_type = str(params.get("exit_stop_order_type") or "STOP").upper()
    parent_duration = str(params.get("parent_duration") or "DAY").upper()
    child_duration = str(params.get("child_duration") or "GOOD_TILL_CANCEL").upper()
    session = str(params.get("session") or "NORMAL").upper()
    instrument = {"symbol": symbol, "assetType": "EQUITY"}
    parent: Dict[str, Any] = {
        "session": session,
        "duration": parent_duration,
        "orderType": "STOP_LIMIT" if entry_type == "STOP_LIMIT" else "STOP",
        "stopPrice": _format_price(plan.get("entry_stop")),
        "orderStrategyType": "TRIGGER",
        "orderLegCollection": [{
            "instruction": "SELL_SHORT",
            "quantity": qty,
            "instrument": instrument,
        }],
        "childOrderStrategies": [{
            "orderStrategyType": "OCO",
            "childOrderStrategies": [
                {
                    "session": session,
                    "duration": child_duration,
                    "orderType": "LIMIT",
                    "price": _format_price(plan.get("target")),
                    "orderStrategyType": "SINGLE",
                    "orderLegCollection": [{
                        "instruction": "BUY_TO_COVER",
                        "quantity": qty,
                        "instrument": instrument,
                    }],
                },
                {
                    "session": session,
                    "duration": child_duration,
                    "orderType": "STOP_LIMIT" if stop_type == "STOP_LIMIT" else "STOP",
                    "stopPrice": _format_price(plan.get("stop_loss")),
                    "orderStrategyType": "SINGLE",
                    "orderLegCollection": [{
                        "instruction": "BUY_TO_COVER",
                        "quantity": qty,
                        "instrument": instrument,
                    }],
                },
            ],
        }],
    }
    if parent["orderType"] == "STOP_LIMIT":
        parent["price"] = _format_price(plan.get("entry_limit") or plan.get("entry_stop"))
    stop_child = parent["childOrderStrategies"][0]["childOrderStrategies"][1]
    if stop_child["orderType"] == "STOP_LIMIT":
        exit_buffer = max(0.0, float(params.get("exit_stop_limit_buffer_pct") or 0.15)) / 100.0
        stop_limit = float(plan.get("stop_loss") or 0.0) * (1.0 + exit_buffer)
        stop_child["price"] = _format_price(stop_limit)
    return parent


def _place_schwab_order(order: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    cfg, headers, acct = _schwab_cfg_headers()
    if not headers:
        return {"error": "Schwab is not authenticated"}, 401
    if not acct:
        return {"error": "No Schwab account_hash configured"}, 400
    data, code = _schwab_request("POST", f"/trader/v1/accounts/{acct}/orders", payload=order, timeout=30)
    if code in (200, 201, 202) and data.get("ok"):
        loc = (data.get("headers") or {}).get("Location") or (data.get("headers") or {}).get("location") or ""
        oid = str(loc).rstrip("/").split("/")[-1] if loc else ""
        return {"ok": True, "order_id": oid, "location": loc, "status_code": code}, code
    return data, code


# ------------------------------ scan helpers -------------------------------

def _norm_symbols(value: Any) -> List[str]:
    import re
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\n\r\t ]+", value)
    else:
        parts = list(value or [])
    out: List[str] = []
    seen = set()
    for p in parts:
        sym = str(p or "").strip().upper().strip(",;")
        if not sym:
            continue
        if sym not in seen:
            seen.add(sym); out.append(sym)
    return out


def _watchlists() -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _connect()
    try:
        rows = con.execute(
            """
            SELECT w.id, w.name, COALESCE(w.description,'') description,
                   COALESCE(w.is_default,0) is_default,
                   COUNT(ws.id) symbol_count
            FROM watchlists w
            LEFT JOIN watchlist_symbols ws ON ws.watchlist_id=w.id
            GROUP BY w.id
            ORDER BY COALESCE(w.is_default,0) DESC, w.name
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def _symbols_for_watchlist(watchlist_id: Any) -> Tuple[List[str], str]:
    try:
        wl_id = int(watchlist_id or 0)
    except Exception:
        wl_id = 0
    if not wl_id:
        return [], ""
    con = _connect()
    try:
        row = con.execute("SELECT name FROM watchlists WHERE id=?", (wl_id,)).fetchone()
        name = str(row[0]) if row else ""
        rows = con.execute("SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol", (wl_id,)).fetchall()
        return [str(r[0]).upper() for r in rows], name
    finally:
        con.close()


def _risk_budget(params: Dict[str, Any]) -> float:
    mode = str(params.get("risk_mode") or "fixed").lower()
    if mode == "percent":
        account_value = max(0.0, float(params.get("account_value") or 0.0))
        risk_pct = max(0.0, float(params.get("risk_pct") or 0.0))
        return account_value * risk_pct / 100.0
    return max(0.0, float(params.get("risk_dollars") or 100.0))


def _candidate_for_symbol(symbol: str, params: Dict[str, Any]) -> Dict[str, Any]:
    rows, err, meta = _fetch_daily_history(symbol, params)
    source = str(meta.get("source") or "")
    bars = int(meta.get("bars") or len(rows) or 0)
    if err:
        return {
            "symbol": symbol,
            "ok": False,
            "valid": False,
            "status": "DATA_ERROR",
            "error": err,
            "reason": err,
            "source": source,
            "rows": bars,
            "meta": meta,
        }
    try:
        threshold = float(params.get("body_ratio_threshold") or 0.5)
        rr = float(params.get("rr") or 3.0)
        signal = evaluate_latest_signal(symbol, rows, threshold)
        sig_d = signal.to_dict()
        if not signal.valid:
            return {
                "symbol": symbol,
                "ok": True,
                "valid": False,
                "status": "NO_SIGNAL",
                "signal": sig_d,
                "reason": signal.reason,
                "source": source,
                "rows": bars,
                "meta": meta,
            }
        plan = build_eod_short_plan(
            signal,
            risk_budget=_risk_budget(params),
            rr=rr,
            entry_order_type=str(params.get("entry_order_type") or "STOP_LIMIT"),
            stop_limit_buffer_pct=float(params.get("entry_stop_limit_buffer_pct") or 0.10),
            max_qty=int(params.get("max_qty") or 1000),
        )
        if not plan:
            return {
                "symbol": symbol,
                "ok": True,
                "valid": False,
                "status": "PLAN_REJECTED",
                "signal": sig_d,
                "reason": "Could not build order plan",
                "source": source,
                "rows": bars,
                "meta": meta,
            }
        plan_d = plan.to_dict()
        if int(plan_d.get("qty") or 0) < 1:
            reason = plan_d.get("notes") or "Qty is 0"
            return {
                "symbol": symbol,
                "ok": True,
                "valid": False,
                "status": "RISK_REJECTED",
                "signal": sig_d,
                "plan": plan_d,
                "reason": reason,
                "source": source,
                "rows": bars,
                "meta": meta,
            }
        order = build_schwab_short_stop_oco_order(plan_d, params)
        bt = simple_backtest_short_stop_entry(rows, body_ratio_threshold=threshold, rr=rr)
        return {
            "symbol": symbol,
            "ok": True,
            "valid": True,
            "status": "CANDIDATE",
            "signal": sig_d,
            "plan": plan_d,
            "order": order,
            "backtest": bt,
            "reason": "Valid candidate",
            "source": source,
            "rows": bars,
            "meta": meta,
        }
    except Exception as exc:
        return {
            "symbol": symbol,
            "ok": False,
            "valid": False,
            "status": "ERROR",
            "error": str(exc),
            "reason": str(exc),
            "source": source,
            "rows": bars,
            "meta": meta,
        }

def _save_scan_run(name: str, watchlist_id: Optional[int], watchlist_name: str, symbols: List[str], params: Dict[str, Any], results: List[Dict[str, Any]]) -> int:
    valid = [r for r in results if r.get("valid")]
    errors = [r for r in results if r.get("status") in {"DATA_ERROR", "ERROR"} or r.get("error")]
    def _op(con):
        cur = con.execute(
            """
            INSERT INTO auto_trading_eod_runs
                (name, created_at, watchlist_id, watchlist_name, symbols_json, params_json, status, candidate_count, error_count, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (name, _now(), watchlist_id, watchlist_name, _json_dumps(symbols), _json_dumps(params), "SCANNED", len(valid), len(errors), "EOD short stop-entry scan"),
        )
        run_id = int(cur.lastrowid)
        for r in results:
            sig = r.get("signal") or {}
            plan = r.get("plan") or {}
            con.execute(
                """
                INSERT OR REPLACE INTO auto_trading_eod_candidates
                    (run_id, symbol, signal_time, valid, status, qty, entry_stop, entry_limit, stop_loss, target,
                     risk_per_share, reward_per_share, risk_dollars, rr, body_ratio, ema5,
                     signal_json, plan_json, order_json, error, reason, source, bars, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    str(r.get("symbol") or "").upper(),
                    str(sig.get("candle_time") or plan.get("signal_time") or ""),
                    1 if r.get("valid") else 0,
                    str(r.get("status") or ("CANDIDATE" if r.get("valid") else "NO_SIGNAL")),
                    int(plan.get("qty") or 0),
                    plan.get("entry_stop"), plan.get("entry_limit"), plan.get("stop_loss"), plan.get("target"),
                    plan.get("risk_per_share"), plan.get("reward_per_share"), plan.get("risk_dollars"), plan.get("rr"),
                    sig.get("body_ratio") or plan.get("body_ratio"), sig.get("ema5") or plan.get("ema5"),
                    _json_dumps(sig), _json_dumps(plan), _json_dumps(r.get("order") or {}),
                    str(r.get("error") or ""), str(r.get("reason") or r.get("error") or ""), str(r.get("source") or ""), int(r.get("rows") or r.get("bars") or 0), _now(),
                ),
            )
        return run_id
    run_id = int(_write_with_retry(_op))
    _log_event("EOD_SCAN", "", f"Created run {name} with {len(valid)} candidates", {"run_id": run_id, "symbols": len(symbols), "errors": len(errors)})
    return run_id


def _load_run(run_id: int) -> Dict[str, Any]:
    _ensure_tables()
    con = _connect()
    try:
        run = con.execute("SELECT * FROM auto_trading_eod_runs WHERE id=?", (int(run_id),)).fetchone()
        if not run:
            return {}
        candidates = con.execute("SELECT * FROM auto_trading_eod_candidates WHERE run_id=? ORDER BY valid DESC, symbol", (int(run_id),)).fetchall()
        d = dict(run)
        d["params"] = json.loads(d.get("params_json") or "{}")
        d["symbols"] = json.loads(d.get("symbols_json") or "[]")
        d["candidates"] = []
        for row in candidates:
            c = dict(row)
            for key in ("signal_json", "plan_json", "order_json"):
                try:
                    c[key.replace("_json", "")] = json.loads(c.get(key) or "{}")
                except Exception:
                    c[key.replace("_json", "")] = {}
            d["candidates"].append(c)
        return d
    finally:
        con.close()


# --------------------------------- routes ----------------------------------

@schwab_eod_bp.route("", methods=["GET"])
@schwab_eod_bp.route("/", methods=["GET"])
def page():
    return render_template("auto_trading_schwab.html")


@schwab_eod_bp.route("/api/status", methods=["GET"])
def api_status():
    _ensure_tables()
    cfg, headers, acct = _schwab_cfg_headers()
    configured = bool(headers and acct)
    con = _connect()
    try:
        runs = con.execute(
            "SELECT id,name,created_at,candidate_count,error_count,submitted_count,status FROM auto_trading_eod_runs ORDER BY id DESC LIMIT 20"
        ).fetchall()
        events = con.execute(
            "SELECT * FROM auto_trading_events ORDER BY id DESC LIMIT 20"
        ).fetchall()
    finally:
        con.close()
    return jsonify({
        "ok": True,
        "schwab_configured": configured,
        "account_hash_masked": (acct[:6] + "..." if acct else ""),
        "live_enabled": _live_enabled(),
        "live_env": _LIVE_ENV,
        "runs": [dict(r) for r in runs],
        "events": [dict(e) for e in events],
    })


@schwab_eod_bp.route("/api/watchlists", methods=["GET"])
def api_watchlists():
    return jsonify({"watchlists": _watchlists()})


@schwab_eod_bp.route("/api/runs", methods=["GET"])
def api_runs():
    _ensure_tables()
    name = str(request.args.get("name") or "").strip()
    date_from = str(request.args.get("from") or "").strip()
    limit = min(200, max(1, int(request.args.get("limit") or 50)))
    sql = "SELECT id,name,created_at,candidate_count,error_count,submitted_count,status FROM auto_trading_eod_runs WHERE 1=1"
    params: List[Any] = []
    if name:
        sql += " AND lower(name) LIKE ?"; params.append(f"%{name.lower()}%")
    if date_from:
        sql += " AND date(created_at) >= date(?)"; params.append(date_from)
    sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
    con = _connect()
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return jsonify({"runs": [dict(r) for r in rows]})


@schwab_eod_bp.route("/api/runs/<int:run_id>", methods=["GET"])
def api_run_detail(run_id: int):
    d = _load_run(run_id)
    if not d:
        return jsonify({"error": "run not found"}), 404
    return jsonify(d)


@schwab_eod_bp.route("/api/eod-scan", methods=["POST"])
def api_eod_scan():
    _ensure_tables()
    d = request.get_json(force=True) or {}
    params = {
        "body_ratio_threshold": float(d.get("body_ratio_threshold") or 0.5),
        "rr": float(d.get("rr") or 3.0),
        "risk_mode": str(d.get("risk_mode") or "fixed"),
        "risk_dollars": float(d.get("risk_dollars") or 100.0),
        "risk_pct": float(d.get("risk_pct") or 0.25),
        "account_value": float(d.get("account_value") or 100000.0),
        "entry_order_type": str(d.get("entry_order_type") or "STOP_LIMIT").upper(),
        "entry_stop_limit_buffer_pct": float(d.get("entry_stop_limit_buffer_pct") or 0.10),
        "exit_stop_order_type": str(d.get("exit_stop_order_type") or "STOP").upper(),
        "exit_stop_limit_buffer_pct": float(d.get("exit_stop_limit_buffer_pct") or 0.15),
        "max_qty": int(d.get("max_qty") or 1000),
        "max_symbols": int(d.get("max_symbols") or 250),
        "period_years": int(d.get("period_years") or 1),
        "data_source": str(d.get("data_source") or "auto").strip().lower(),
        "session": "NORMAL",
        "parent_duration": str(d.get("parent_duration") or "DAY").upper(),
        "child_duration": str(d.get("child_duration") or "GOOD_TILL_CANCEL").upper(),
    }
    watchlist_id = d.get("watchlist_id")
    wl_symbols, wl_name = _symbols_for_watchlist(watchlist_id)
    manual = _norm_symbols(d.get("symbols") or "")
    symbols = manual or wl_symbols
    if not symbols:
        return jsonify({"error": "No symbols selected. Choose a watchlist or enter symbols."}), 400
    symbols = symbols[: max(1, int(params["max_symbols"]))]
    # In AUTO mode, probe Schwab once.  If market-data pricehistory is rejected
    # globally (the uploaded run showed 198 identical HTTP 400 rows), skip
    # repeated Schwab calls and use the local/yfinance daily-history fallback for
    # the whole run.  Order submission still uses Schwab.
    if str(params.get("data_source") or "auto").lower() == "auto":
        _probe_rows, _probe_err = _fetch_schwab_daily_history("SPY", int(params.get("period_years") or 1))
        if _probe_err:
            params["data_source_effective"] = "yfinance"
            params["schwab_history_preflight_error"] = _probe_err
        else:
            params["data_source_effective"] = "schwab"
    run_name = str(d.get("name") or "").strip() or f"EODShort_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    max_workers = min(8, max(1, int(d.get("max_workers") or 5)), len(symbols))
    started = time.time()
    results: List[Dict[str, Any]] = []
    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futs = {ex.submit(_candidate_for_symbol, sym, params): sym for sym in symbols}
        from ..services.bounded_wait import bounded_as_completed
        for fut, sym in bounded_as_completed(futs, timeout=90,
                on_timeout=lambda ks: print(f"[schwab_eod] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                results.append({"symbol": sym, "ok": False, "valid": False, "error": "timed out"})
                continue
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append({"symbol": sym, "ok": False, "valid": False, "error": str(exc)})
    finally:
        ex.shutdown(wait=False)
    results.sort(key=lambda r: (not bool(r.get("valid")), str(r.get("symbol") or "")))
    run_id = _save_scan_run(run_name, int(watchlist_id or 0) or None, wl_name, symbols, params, results)
    detail = _load_run(run_id)
    detail["elapsed_sec"] = round(time.time() - started, 2)
    return jsonify(detail)


@schwab_eod_bp.route("/api/submit", methods=["POST"])
def api_submit():
    _ensure_tables()
    d = request.get_json(force=True) or {}
    run_id = int(d.get("run_id") or 0)
    ids = [int(x) for x in (d.get("candidate_ids") or []) if str(x).strip().isdigit()]
    dry_run = bool(d.get("dry_run", True))
    confirm_live = bool(d.get("confirm_live", False))
    if not run_id:
        return jsonify({"error": "run_id is required"}), 400
    run = _load_run(run_id)
    if not run:
        return jsonify({"error": "run not found"}), 404
    params = run.get("params") or {}
    candidates = [c for c in run.get("candidates", []) if int(c.get("valid") or 0) == 1]
    if ids:
        idset = set(ids)
        candidates = [c for c in candidates if int(c.get("id") or 0) in idset]
    if not candidates:
        return jsonify({"error": "No valid candidates selected"}), 400
    live_ok = _live_enabled() and confirm_live and not dry_run
    results: List[Dict[str, Any]] = []
    submitted = 0
    for c in candidates:
        cid = int(c.get("id"))
        order = c.get("order") or {}
        if not order:
            order = build_schwab_short_stop_oco_order(c.get("plan") or {}, params)
        if not live_ok:
            res = {"ok": True, "dry_run": True, "candidate_id": cid, "symbol": c.get("symbol"), "order": order, "message": "Dry-run only; order not sent to Schwab"}
            status = "DRY_RUN"
            oid = ""
            err = ""
        else:
            res, code = _place_schwab_order(order)
            status = "SUBMITTED" if res.get("ok") else "ERROR"
            oid = str(res.get("order_id") or "")
            err = "" if res.get("ok") else str(res.get("error") or res.get("detail") or f"HTTP {code}")[:1000]
            if res.get("ok"):
                submitted += 1
        def _op(con, cid=cid, status=status, oid=oid, err=err, order=order):
            con.execute(
                """
                UPDATE auto_trading_eod_candidates
                   SET status=?, schwab_order_id=?, schwab_status=?, error=?, order_json=?, updated_at=?
                 WHERE id=?
                """,
                (status, oid, status, err, _json_dumps(order), _now(), cid),
            )
            if status == "SUBMITTED":
                con.execute("UPDATE auto_trading_eod_runs SET submitted_count=COALESCE(submitted_count,0)+1 WHERE id=?", (run_id,))
        _write_with_retry(_op)
        _log_event("ORDER_SUBMIT" if live_ok else "ORDER_DRY_RUN", str(c.get("symbol") or ""), f"{status} {c.get('symbol')}", {"run_id": run_id, "candidate_id": cid, "order_id": oid, "error": err})
        results.append(res)
    return jsonify({"ok": True, "live_sent": live_ok, "submitted": submitted, "results": results, "run": _load_run(run_id)})


@schwab_eod_bp.route("/api/orders", methods=["GET"])
def api_orders():
    days = max(1, min(30, int(request.args.get("days") or 7)))
    start = (date.today() - timedelta(days=days)).isoformat()
    cfg, headers, acct = _schwab_cfg_headers()
    if not acct:
        return jsonify({"error": "No Schwab account_hash configured"}), 400
    data, code = _schwab_request("GET", f"/trader/v1/accounts/{acct}/orders", params={"fromEnteredTime": start})
    return jsonify(data), code


@schwab_eod_bp.route("/api/history-test", methods=["GET"])
def api_history_test():
    sym = str(request.args.get("symbol") or "SPY").strip().upper() or "SPY"
    years = max(1, min(10, int(request.args.get("period_years") or 1)))
    source = str(request.args.get("source") or "auto").strip().lower()
    params = {"period_years": years, "data_source": source}
    rows, err, meta = _fetch_daily_history(sym, params)
    return jsonify({
        "ok": not bool(err),
        "symbol": sym,
        "rows": len(rows),
        "source": meta.get("source"),
        "requested_source": meta.get("requested_source"),
        "error": err,
        "meta": meta,
        "sample": rows[-3:],
    }), 200 if not err else 400


@schwab_eod_bp.route("/api/candidate/<int:candidate_id>/payload", methods=["GET"])
def api_candidate_payload(candidate_id: int):
    _ensure_tables()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM auto_trading_eod_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            return jsonify({"error": "candidate not found"}), 404
        d = dict(row)
        for key in ("signal_json", "plan_json", "order_json"):
            try:
                d[key.replace("_json", "")] = json.loads(d.get(key) or "{}")
            except Exception:
                d[key.replace("_json", "")] = {}
        return jsonify(d)
    finally:
        con.close()
