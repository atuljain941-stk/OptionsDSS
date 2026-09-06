"""schwab_strategy_engine.py -- V134 addition. Mirrors
icici_strategy_engine.py's structure and safety properties, generalized
for legs that are either "stock" (symbol/side/qty only, no strike
resolution needed) or "option" (ITM/ATM/OTM + offset, resolved fresh
against live spot + chain each time, same as ICICI).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..db import DB_PATH
from . import schwab_trading as trading
from . import schwab_positions as positions

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_tables() -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS schwab_strategies (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                name                  TEXT NOT NULL,
                stock_code            TEXT NOT NULL,
                legs_json             TEXT NOT NULL,
                open_condition_type   TEXT NOT NULL,
                open_condition_expr   TEXT NOT NULL,
                eval_interval_minutes INTEGER NOT NULL DEFAULT 5,
                open_time_start       TEXT,
                open_time_end         TEXT,
                target_pnl            REAL NOT NULL,
                stop_loss_pnl         REAL NOT NULL,
                close_condition_type  TEXT NOT NULL DEFAULT 'none',
                close_condition_expr  TEXT,
                close_time            TEXT,
                max_reentries         INTEGER NOT NULL DEFAULT 0,
                reentry_count         INTEGER NOT NULL DEFAULT 0,
                execution_mode        TEXT NOT NULL DEFAULT 'safe_sequential',
                enabled               INTEGER NOT NULL DEFAULT 0,
                active_position_id    INTEGER,
                last_evaluated_at     TEXT,
                last_fired_at         TEXT,
                last_eval_note        TEXT,
                created_at            TEXT NOT NULL,
                updated_at            TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_schwab_strat_enabled ON schwab_strategies(enabled)")
        con.commit()
    finally:
        con.close()


def create_strategy(config: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_tables()
    legs = config.get("legs") or []
    if not legs:
        return {"ok": False, "error": "at least one leg is required"}
    for leg in legs:
        if leg.get("side") not in ("LONG", "SHORT"):
            return {"ok": False, "error": "leg side must be LONG or SHORT"}
        if leg.get("instrument_type") not in ("stock", "option"):
            return {"ok": False, "error": "leg instrument_type must be stock or option"}
        if leg["instrument_type"] == "option" and leg.get("strike_mode") not in ("ITM", "ATM", "OTM"):
            return {"ok": False, "error": "option leg strike_mode must be ITM/ATM/OTM"}
        if leg["instrument_type"] == "option" and leg.get("right") not in ("call", "put"):
            return {"ok": False, "error": "option leg right must be call or put"}

    open_type = config.get("open_condition_type")
    if open_type not in ("price", "scanner"):
        return {"ok": False, "error": "open_condition_type must be 'price' or 'scanner'"}
    if open_type == "price" and not _parse_price_condition(config.get("open_condition_expr", "")):
        return {"ok": False, "error": "open_condition_expr for a price condition must look like '> 450' or '<= 440'"}
    close_type = config.get("close_condition_type") or "none"
    if close_type not in ("none", "price", "scanner"):
        return {"ok": False, "error": "close_condition_type must be 'none', 'price' or 'scanner'"}

    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("""
            INSERT INTO schwab_strategies
                (name, stock_code, legs_json, open_condition_type, open_condition_expr, eval_interval_minutes,
                 open_time_start, open_time_end, target_pnl, stop_loss_pnl,
                 close_condition_type, close_condition_expr, close_time,
                 max_reentries, reentry_count, execution_mode, enabled, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            str(config.get("name") or "Unnamed strategy"), str(config.get("stock_code") or "").upper(),
            json.dumps(legs), open_type, str(config.get("open_condition_expr") or ""),
            int(config.get("eval_interval_minutes") or 5),
            str(config.get("open_time_start") or "") or None, str(config.get("open_time_end") or "") or None,
            abs(float(config.get("target_pnl") or 0)), abs(float(config.get("stop_loss_pnl") or 0)),
            close_type, str(config.get("close_condition_expr") or "") or None, str(config.get("close_time") or "") or None,
            int(config.get("max_reentries") or 0), 0,
            str(config.get("execution_mode") or "safe_sequential"), int(bool(config.get("enabled"))),
            _now(), _now(),
        ))
        strategy_id = cur.lastrowid
        con.commit()
    finally:
        con.close()
    return {"ok": True, "strategy_id": strategy_id}


def list_strategies() -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM schwab_strategies ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["legs"] = json.loads(d.pop("legs_json") or "[]")
            out.append(d)
        return out
    finally:
        con.close()


def set_enabled(strategy_id: int, enabled: bool) -> Dict[str, Any]:
    _ensure_tables()
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("UPDATE schwab_strategies SET enabled=?, updated_at=? WHERE id=?", (int(enabled), _now(), strategy_id))
        con.commit()
        ok = cur.rowcount > 0
    finally:
        con.close()
    return {"ok": ok, "error": None if ok else "strategy not found"}


def delete_strategy(strategy_id: int) -> Dict[str, Any]:
    _ensure_tables()
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("DELETE FROM schwab_strategies WHERE id=?", (strategy_id,))
        con.commit()
        ok = cur.rowcount > 0
    finally:
        con.close()
    return {"ok": ok, "error": None if ok else "strategy not found"}


def reset_reentries(strategy_id: int) -> Dict[str, Any]:
    _ensure_tables()
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("UPDATE schwab_strategies SET reentry_count=0, updated_at=? WHERE id=?", (_now(), strategy_id))
        con.commit()
        ok = cur.rowcount > 0
    finally:
        con.close()
    return {"ok": ok, "error": None if ok else "strategy not found"}


def _resolve_strike(spot: float, right: str, mode: str, offset: float, available: List[float]) -> Optional[float]:
    if not available:
        return None
    if mode == "ATM":
        target = spot
    elif right == "call":
        target = spot + offset if mode == "OTM" else spot - offset
    else:
        target = spot - offset if mode == "OTM" else spot + offset
    return min(available, key=lambda s: abs(s - target))


def resolve_strategy_legs(strategy: Dict[str, Any]) -> Dict[str, Any]:
    """Resolves each leg for right now: stock legs pass through
    unchanged (no strike concept); option legs get ITM/ATM/OTM resolved
    against live spot + the actual chain, same as ICICI."""
    needs_options = any(leg["instrument_type"] == "option" for leg in strategy["legs"])
    spot = None
    strikes_by_right: Dict[str, List[float]] = {"call": [], "put": []}
    expiry_date = None

    if needs_options:
        spot_result = trading.get_spot_price(strategy["stock_code"])
        if not spot_result.get("ok"):
            return {"ok": False, "error": f"could not fetch spot price: {spot_result.get('error')}"}
        spot = spot_result["spot"]

        expiries_result = trading.get_available_expiries(strategy["stock_code"])
        if not expiries_result.get("ok") or not expiries_result.get("expiries"):
            return {"ok": False, "error": f"could not fetch expiries: {expiries_result.get('error')}"}
        expiry_date = expiries_result["expiries"][0]  # nearest -- no per-strategy expiry pinning needed here since Schwab can discover it live each time

        chain_result = trading.get_option_chain(strategy["stock_code"], expiry_date)
        if not chain_result.get("ok") or not chain_result.get("rows"):
            return {"ok": False, "error": f"could not load option chain: {chain_result.get('error')}"}
        for row in chain_result["rows"]:
            strikes_by_right.setdefault(row["right"], []).append(row["strike_price"])

    resolved_legs = []
    for i, leg in enumerate(strategy["legs"]):
        if leg["instrument_type"] == "stock":
            resolved_legs.append({"leg_index": i, "instrument_type": "stock", "side": leg["side"], "quantity": int(leg.get("quantity") or 1)})
            continue
        available = strikes_by_right.get(leg["right"], [])
        strike = _resolve_strike(spot, leg["right"], leg["strike_mode"], float(leg.get("strike_offset") or 0), available)
        if strike is None:
            return {"ok": False, "error": f"leg {i}: no {leg['right']} strikes available to resolve {leg['strike_mode']}"}
        resolved_legs.append({
            "leg_index": i, "instrument_type": "option", "right": leg["right"], "strike_price": strike,
            "expiry_date": expiry_date, "side": leg["side"], "quantity": int(leg.get("quantity") or 1),
        })
    return {"ok": True, "legs": resolved_legs, "spot": spot}


def _parse_price_condition(expr: str) -> Optional[Dict[str, Any]]:
    m = re.match(r"^\s*(>=|<=|>|<|==)\s*(-?\d+(\.\d+)?)\s*$", expr or "")
    if not m:
        return None
    return {"op": m.group(1), "value": float(m.group(2))}


def evaluate_price_condition(stock_code: str, expr: str) -> Dict[str, Any]:
    parsed = _parse_price_condition(expr)
    if not parsed:
        return {"ok": False, "met": False, "error": f"unparseable price condition: {expr!r}"}
    spot_result = trading.get_spot_price(stock_code)
    if not spot_result.get("ok"):
        return {"ok": False, "met": False, "error": spot_result.get("error")}
    spot = spot_result["spot"]
    op, value = parsed["op"], parsed["value"]
    met = (spot > value if op == ">" else spot >= value if op == ">=" else
           spot < value if op == "<" else spot <= value if op == "<=" else spot == value)
    return {"ok": True, "met": met, "spot": spot, "error": None}


def evaluate_scanner_condition(stock_code: str, query_text: str) -> Dict[str, Any]:
    """Reuses the same Scanner Builder live-query engine + symbol
    override built for the ICICI system -- for US equities (which
    Schwab trades), the underlying price_cache is far more likely to
    actually be populated than it was for NIFTY, so this should be
    more reliable here than it was on the ICICI side."""
    try:
        from flask import current_app
        with current_app.test_client() as client:
            resp = client.post("/scanner-builder/api/run", json={"query_text": query_text, "symbol": stock_code})
            data = resp.get_json() or {}
        if resp.status_code != 200:
            return {"ok": False, "met": False, "error": data.get("error") or f"scanner run failed ({resp.status_code})"}
        matches = data.get("results") or data.get("matches") or []
        matched_symbols = {(m.get("symbol") or m).upper() if isinstance(m, dict) else str(m).upper() for m in matches}
        return {"ok": True, "met": stock_code.upper() in matched_symbols, "error": None}
    except Exception as e:
        return {"ok": False, "met": False, "error": str(e)}


def evaluate_condition(stock_code: str, condition_type: str, expr: str) -> Dict[str, Any]:
    if condition_type == "price":
        return evaluate_price_condition(stock_code, expr)
    if condition_type == "scanner":
        return evaluate_scanner_condition(stock_code, expr)
    return {"ok": False, "met": False, "error": f"unknown condition_type {condition_type!r}"}


def _within_time_window(start: Optional[str], end: Optional[str]) -> bool:
    if not start and not end:
        return True
    now_t = datetime.now().strftime("%H:%M")
    if start and now_t < start:
        return False
    if end and now_t > end:
        return False
    return True


def _past_close_time(close_time: Optional[str]) -> bool:
    if not close_time:
        return False
    return datetime.now().strftime("%H:%M") >= close_time


def _due_for_eval(strategy: Dict[str, Any]) -> bool:
    last = strategy.get("last_evaluated_at")
    if not last:
        return True
    try:
        elapsed_min = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60.0
    except Exception:
        return True
    return elapsed_min >= max(1, int(strategy.get("eval_interval_minutes") or 5))


def evaluate_strategies_tick() -> Dict[str, Any]:
    _ensure_tables()
    if not trading.is_market_hours():
        return {"ok": True, "checked": 0, "results": [], "checked_at": _now(), "note": "outside US market hours -- skipped"}
    with _lock:
        strategies = [s for s in list_strategies() if s["enabled"]]
        results = []
        for strat in strategies:
            note = None
            if strat.get("active_position_id"):
                pos_rows = positions.list_positions()
                pos = next((p for p in pos_rows if p["id"] == strat["active_position_id"]), None)
                if pos is None:
                    note = "active_position_id pointed at a deleted position -- clearing"
                    _update_strategy(strat["id"], active_position_id=None)
                elif pos["status"] == "OPEN":
                    should_close = False
                    close_reason = None
                    if _past_close_time(strat.get("close_time")):
                        should_close, close_reason = True, "STRATEGY_CLOSE_TIME"
                    if not should_close and strat["close_condition_type"] != "none" and strat.get("close_condition_expr"):
                        cond = evaluate_condition(strat["stock_code"], strat["close_condition_type"], strat["close_condition_expr"])
                        if cond.get("met"):
                            should_close, close_reason = True, "STRATEGY_CLOSE_CONDITION"
                        elif not cond.get("ok"):
                            note = f"close condition check failed: {cond.get('error')}"
                    if should_close:
                        close_result = positions.close_position(pos["id"], reason=close_reason)
                        note = f"{close_reason.lower()} -> close_position: ok={close_result.get('ok')}"
                    elif note is None:
                        note = "position open, no close trigger yet"
                else:
                    if strat["reentry_count"] < strat["max_reentries"]:
                        _update_strategy(strat["id"], active_position_id=None, reentry_count=strat["reentry_count"] + 1)
                        note = f"position ended ({pos['status']}) -- re-entry {strat['reentry_count'] + 1}/{strat['max_reentries']} available"
                    else:
                        _update_strategy(strat["id"], active_position_id=None)
                        note = f"position ended ({pos['status']}) -- re-entry cap reached"
            else:
                if _due_for_eval(strat):
                    if not _within_time_window(strat.get("open_time_start"), strat.get("open_time_end")):
                        note = "outside configured opening time window"
                        _update_strategy(strat["id"], last_evaluated_at=_now())
                    else:
                        cond = evaluate_condition(strat["stock_code"], strat["open_condition_type"], strat["open_condition_expr"])
                        _update_strategy(strat["id"], last_evaluated_at=_now())
                        if cond.get("met"):
                            fire_result = _fire_strategy(strat)
                            note = f"open condition met -> fire: {fire_result.get('note')}"
                        elif not cond.get("ok"):
                            note = f"open condition check failed: {cond.get('error')}"
                        else:
                            note = "open condition not met"
                else:
                    note = "not due for evaluation yet"
            if note:
                _update_strategy(strat["id"], last_eval_note=note)
            results.append({"strategy_id": strat["id"], "name": strat["name"], "note": note})
        return {"ok": True, "checked": len(strategies), "results": results, "checked_at": _now()}


def _fire_strategy(strat: Dict[str, Any]) -> Dict[str, Any]:
    resolved = resolve_strategy_legs(strat)
    if not resolved.get("ok"):
        return {"ok": False, "note": f"strike resolution failed: {resolved.get('error')}"}
    live = trading.is_live_trading_enabled()
    result = positions.open_position(
        strategy_name=strat["name"], stock_code=strat["stock_code"], legs=resolved["legs"],
        target_pnl=strat["target_pnl"], stop_loss_pnl=strat["stop_loss_pnl"],
        execution_mode=strat.get("execution_mode") or "safe_sequential", dry_run=not live,
    )
    if result.get("ok"):
        _update_strategy(strat["id"], active_position_id=result["position_id"], last_fired_at=_now())
        return {"ok": True, "note": f"opened {'LIVE' if live else 'PAPER'} position {result['position_id']}"}
    return {"ok": False, "note": f"open_position failed: {result.get('error')}"}


def _update_strategy(strategy_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    con = sqlite3.connect(DB_PATH)
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE schwab_strategies SET {sets} WHERE id=?", (*fields.values(), strategy_id))
        con.commit()
    finally:
        con.close()


_registered = False
_reg_lock = threading.Lock()


def register_strategy_evaluator_job() -> bool:
    global _registered
    with _reg_lock:
        if _registered:
            return False
        _registered = True
    from .job_registry import register_job
    register_job(
        "schwab_strategy_evaluator", "Schwab Strategy Evaluator",
        "Checks every ENABLED Schwab strategy's opening/closing conditions and fires "
        "tracked positions automatically -- same logic as the ICICI strategy evaluator.",
        kind="interval", default_schedule={"interval_min": 1},
        group="Manual / One-Time", run_now_fn=evaluate_strategies_tick,
    )
    return True
