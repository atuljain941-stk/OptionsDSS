"""schwab_positions.py -- V134 addition. Mirrors icici_positions.py's
structure and safety properties, generalized for the stock+option leg
model in schwab_vertical_executor.py.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..db import DB_PATH
from . import schwab_vertical_executor as executor
from . import schwab_trading as trading

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
            CREATE TABLE IF NOT EXISTS schwab_auto_positions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name       TEXT,
                stock_code          TEXT NOT NULL,
                legs_json           TEXT NOT NULL,
                target_pnl          REAL NOT NULL,
                stop_loss_pnl       REAL NOT NULL,
                current_pnl         REAL,
                status              TEXT NOT NULL DEFAULT 'OPENING',
                execution_mode      TEXT NOT NULL DEFAULT 'safe_sequential',
                dry_run             INTEGER NOT NULL DEFAULT 0,
                oco_order_id        TEXT,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                closed_at           TEXT,
                close_reason        TEXT
            )
        """)
        try:
            con.execute("ALTER TABLE schwab_auto_positions ADD COLUMN oco_order_id TEXT")
        except Exception:
            pass  # already exists on tables created before this addition
        try:
            con.execute("ALTER TABLE schwab_auto_positions ADD COLUMN pending_oco_target REAL")
            con.execute("ALTER TABLE schwab_auto_positions ADD COLUMN pending_oco_stop REAL")
        except Exception:
            pass  # already exists on tables created before this addition
        con.execute("""
            CREATE TABLE IF NOT EXISTS schwab_order_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id   INTEGER NOT NULL,
                phase         TEXT NOT NULL,
                leg_index     INTEGER,
                side          TEXT,
                action        TEXT,
                order_id      TEXT,
                ok            INTEGER,
                error         TEXT,
                created_at    TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_schwab_pos_status ON schwab_auto_positions(status)")
        con.commit()
    finally:
        con.close()


def _log_leg_results(position_id: int, phase: str, leg_results: List[Dict[str, Any]]) -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        for r in leg_results:
            con.execute("""
                INSERT INTO schwab_order_log (position_id, phase, leg_index, side, action, order_id, ok, error, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (position_id, phase, r.get("leg_index"), r.get("side"), r.get("action"),
                  r.get("order_id"), int(bool(r.get("ok"))), r.get("error"), _now()))
        con.commit()
    finally:
        con.close()


def open_position(strategy_name: str, stock_code: str, legs: List[Dict[str, Any]],
                   target_pnl: float, stop_loss_pnl: float, order_type: str = "MARKET",
                   execution_mode: str = "safe_sequential", price_by_leg: Optional[Dict[int, float]] = None,
                   dry_run: bool = False, combo_net_price: Optional[float] = None,
                   oco_target_price: Optional[float] = None, oco_stop_price: Optional[float] = None,
                   oco_trailing: bool = False, oco_trail_amount: Optional[float] = None, oco_trail_is_percent: bool = False,
                   duration: str = "DAY", cancel_time: Optional[str] = None) -> Dict[str, Any]:
    _ensure_tables()
    # SAFETY: the top-level "Live Trading" toggle used to only gate the
    # Strategy engine's automatic firing -- a manual "Open Position"
    # submission (or the "Group Selected" adopt flow calling this same
    # function) went out for real regardless of that toggle, using only
    # whatever this specific request's dry_run flag said (default: not
    # dry run). That's a real safety gap, confirmed by a real incident --
    # Live Trading OFF must mean nothing real can be submitted from
    # ANYWHERE, not just the strategy engine. Forcing it here, once,
    # covers every caller instead of trusting each call site to remember.
    if not dry_run and not trading.is_live_trading_enabled():
        dry_run = True
    if not legs:
        return {"ok": False, "error": "at least one leg is required"}
    for leg in legs:
        if leg.get("side") not in ("LONG", "SHORT"):
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: side must be LONG or SHORT"}
        if leg.get("instrument_type") not in ("stock", "option"):
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: instrument_type must be stock or option"}
        if leg["instrument_type"] == "option" and leg.get("right") not in ("call", "put"):
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: right must be call or put"}

    pending_order_id = None  # set below if the order is submitted but not yet filled (e.g. a GTC limit)

    if execution_mode == "native_combo":
        # Whole spread as ONE atomic order -- Schwab's own matching
        # engine fills every leg together or not at all, unlike the
        # sequential/simultaneous modes below (built for ICICI's
        # Breeze API, which has no combo-order support).
        actions = {leg["leg_index"]: executor._open_action(leg) for leg in legs}
        if dry_run:
            leg_results = [{"leg_index": leg["leg_index"], "side": leg["side"], "action": actions[leg["leg_index"]],
                             "dry_run": True, "ok": True, "order_id": None, "fill_confirmed": True} for leg in legs]
            combo_order_id = None
        else:
            combo_order_type = order_type.upper() if order_type.upper() in ("MARKET",) else ("NET_CREDIT" if (combo_net_price or 0) >= 0 else "NET_DEBIT")
            combo_result = trading.place_multileg_order(stock_code, legs, actions, order_type=combo_order_type, net_price=combo_net_price,
                                                          duration=duration, cancel_time=cancel_time)
            if not combo_result["ok"]:
                return {"ok": False, "error": f"combo order placement failed: {combo_result['error']}"}
            combo_order_id = combo_result["order_id"]
            fill = trading.poll_order_fill(combo_order_id, timeout_sec=8.0)
            if not fill.get("filled"):
                last_status = (fill.get("status") or "").strip().lower()
                if last_status in trading._REJECTED_STATUSES:
                    return {"ok": False, "error": f"combo order not filled: {fill.get('error')}",
                             "leg_results": [{"leg_index": leg["leg_index"], "order_id": combo_order_id, "ok": False} for leg in legs]}
                # Still working (queued/pending activation/GTC sitting
                # unfilled) -- not a failure, just not filled YET. Track
                # it as pending instead of throwing the order away.
                pending_order_id = combo_order_id
            leg_results = [{"leg_index": leg["leg_index"], "side": leg["side"], "action": actions[leg["leg_index"]],
                             "ok": True, "order_id": combo_order_id, "fill_confirmed": pending_order_id is None,
                             "fill_average_price": fill.get("average_price")} for leg in legs]
        for leg in legs:
            leg["order_id"] = combo_order_id
            if pending_order_id:
                leg["entry_price"] = None  # not known until it actually fills -- set by check_pending_entries()
                continue
            entry_price = None
            if leg["instrument_type"] == "stock":
                q = trading.get_quote(stock_code)
            else:
                occ = trading.to_occ_symbol(stock_code, leg["expiry_date"], leg["right"], leg["strike_price"])
                q = trading.get_quote(occ)
            entry_price = q.get("ltp") if q.get("ok") else None
            leg["entry_price"] = entry_price
        result = {"ok": True, "leg_results": leg_results}
    else:
        result = executor.execute_open(stock_code, legs, order_type=order_type, price_by_leg=price_by_leg,
                                        execution_mode=execution_mode, dry_run=dry_run)
        if not result["ok"]:
            return {"ok": False, "error": f"open sequence aborted after leg {result['aborted_after_leg']}",
                    "leg_results": result["leg_results"]}

        for leg, leg_result in zip(executor._sequence_for_open(legs), result["leg_results"]):
            leg["order_id"] = leg_result.get("order_id")
            entry_price = leg_result.get("fill_average_price")
            if entry_price is None:
                if leg["instrument_type"] == "stock":
                    q = trading.get_quote(stock_code)
                else:
                    occ = trading.to_occ_symbol(stock_code, leg["expiry_date"], leg["right"], leg["strike_price"])
                    q = trading.get_quote(occ)
                entry_price = q.get("ltp") if q.get("ok") else None
            leg["entry_price"] = entry_price

    con = sqlite3.connect(DB_PATH)
    try:
        position_status = "PENDING_ENTRY" if pending_order_id else "OPEN"
        cur = con.execute("""
            INSERT INTO schwab_auto_positions
                (strategy_name, stock_code, legs_json, target_pnl, stop_loss_pnl, current_pnl,
                 status, execution_mode, dry_run, pending_oco_target, pending_oco_stop, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (strategy_name, stock_code.upper(), json.dumps(legs), abs(target_pnl), abs(stop_loss_pnl), 0.0,
              position_status, execution_mode, int(dry_run),
              (oco_target_price if pending_order_id else None), (oco_stop_price if pending_order_id else None),
              _now(), _now()))
        position_id = cur.lastrowid
        con.commit()
    finally:
        con.close()

    _log_leg_results(position_id, "OPEN", result["leg_results"])

    oco_order_id = None
    oco_error = None
    if execution_mode == "native_combo" and not dry_run and not pending_order_id and oco_target_price is not None and oco_stop_price is not None:
        close_actions = {leg["leg_index"]: executor._close_action(leg) for leg in legs}
        oco_result = trading.place_oco_exit(stock_code, legs, close_actions, oco_target_price, oco_stop_price,
                                             trailing=oco_trailing, trail_amount=oco_trail_amount, trail_is_percent=oco_trail_is_percent)
        if oco_result["ok"]:
            oco_order_id = oco_result["order_id"]
            con = sqlite3.connect(DB_PATH)
            try:
                con.execute("UPDATE schwab_auto_positions SET oco_order_id=?, updated_at=? WHERE id=?", (oco_order_id, _now(), position_id))
                con.commit()
            finally:
                con.close()
        else:
            oco_error = oco_result["error"]

    return {"ok": True, "position_id": position_id, "leg_results": result["leg_results"],
            "oco_order_id": oco_order_id, "oco_error": oco_error}


def close_position(position_id: int, reason: str = "MANUAL", execution_mode_override: Optional[str] = None,
                    close_pct: float = 100.0) -> Dict[str, Any]:
    """close_pct: 1-100. Below 100, this closes only that fraction of
    each leg's quantity (rounded down, min 1 per leg) and leaves the
    position OPEN with the remaining quantity tracked -- e.g. close_pct=50
    on a 4-lot position closes 2 lots and leaves 2 open. A position
    with an outstanding OCO exit can't be partially closed through
    this path (the OCO covers the FULL quantity) -- cancel the OCO
    first if you need to scale out."""
    _ensure_tables()
    close_pct = max(1.0, min(100.0, close_pct))
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM schwab_auto_positions WHERE id=?", (position_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return {"ok": False, "error": "position not found"}
    if row["status"] != "OPEN":
        return {"ok": False, "error": f"position is not OPEN (status={row['status']})"}
    if close_pct < 100.0 and row["oco_order_id"]:
        return {"ok": False, "error": "position has an outstanding OCO exit covering the full quantity -- cancel it first to partially close"}

    legs = json.loads(row["legs_json"])
    dry_run = bool(row["dry_run"])
    execution_mode = execution_mode_override or row["execution_mode"] or "safe_sequential"

    partial = close_pct < 100.0
    close_legs = legs
    remaining_legs = None
    if partial:
        close_legs = []
        remaining_legs = []
        for leg in legs:
            total_qty = int(leg["quantity"])
            close_qty = max(1, min(total_qty - 1, round(total_qty * close_pct / 100.0))) if total_qty > 1 else total_qty
            if close_qty >= total_qty:
                close_qty = total_qty  # single-lot legs (or rounding to the full amount) just close entirely
            close_leg = dict(leg); close_leg["quantity"] = close_qty
            close_legs.append(close_leg)
            if close_qty < total_qty:
                remaining_leg = dict(leg); remaining_leg["quantity"] = total_qty - close_qty
                remaining_legs.append(remaining_leg)
        if not remaining_legs:
            partial = False  # every leg's close_qty rounded up to its full quantity -- this is really a full close

    if row["oco_order_id"] and reason != "OCO_FILLED":
        # Being closed through some OTHER path (manual, strategy
        # condition, etc.) while an OCO exit is still outstanding --
        # cancel it first so it can't fire later against a position
        # that's already gone.
        trading.cancel_order(row["oco_order_id"])

    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE schwab_auto_positions SET status='CLOSING', updated_at=? WHERE id=?", (_now(), position_id))
        con.commit()
    finally:
        con.close()

    result = executor.execute_close(row["stock_code"], close_legs, execution_mode=execution_mode, dry_run=dry_run)
    _log_leg_results(position_id, "CLOSE" if not partial else f"PARTIAL_CLOSE_{close_pct:.0f}PCT", result["leg_results"])

    con = sqlite3.connect(DB_PATH)
    try:
        if result["ok"] and partial:
            # Scaled out, not fully closed -- back to OPEN with the
            # reduced remaining quantity, not CLOSED.
            con.execute("UPDATE schwab_auto_positions SET status='OPEN', legs_json=?, updated_at=? WHERE id=?",
                        (json.dumps(remaining_legs), _now(), position_id))
        elif result["ok"]:
            con.execute("UPDATE schwab_auto_positions SET status='CLOSED', closed_at=?, close_reason=?, updated_at=? WHERE id=?",
                        (_now(), reason, _now(), position_id))
        else:
            con.execute("UPDATE schwab_auto_positions SET status='ERROR', close_reason=?, updated_at=? WHERE id=?",
                        (f"close aborted after leg {result['aborted_after_leg']}", _now(), position_id))
        con.commit()
    finally:
        con.close()
    return {"ok": result["ok"], "leg_results": result["leg_results"], "partial": partial}


def list_positions(status: Optional[str] = None) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        if status:
            rows = con.execute("SELECT * FROM schwab_auto_positions WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM schwab_auto_positions ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["legs"] = json.loads(d.pop("legs_json") or "[]")
            out.append(d)
        return out
    finally:
        con.close()


def adopt_broker_positions(strategy_name: str, stock_code: str, legs: List[Dict[str, Any]],
                            target_pnl: float, stop_loss_pnl: float,
                            execution_mode: str = "safe_sequential") -> Dict[str, Any]:
    """Bundle one or more ALREADY-HELD broker legs into a new tracked
    row -- no orders placed, since these positions already exist in
    the account. Mirrors icici_positions.adopt_broker_positions()
    exactly. Each leg needs entry_price already set from the broker
    position's real average_price (the caller passes this through
    from the broker_positions list), so P&L math is accurate from
    adoption onward, not from whatever the price happens to be right
    now."""
    _ensure_tables()
    if not legs:
        return {"ok": False, "error": "at least one leg is required"}
    for leg in legs:
        if leg.get("side") not in ("LONG", "SHORT"):
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: side must be LONG or SHORT"}
        if leg.get("instrument_type") not in ("stock", "option"):
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: instrument_type must be stock or option"}
        if leg["instrument_type"] == "option" and leg.get("right") not in ("call", "put"):
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: right must be call or put"}
        if leg.get("entry_price") is None:
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: entry_price is required when adopting an existing broker position"}

    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("""
            INSERT INTO schwab_auto_positions
                (strategy_name, stock_code, legs_json, target_pnl, stop_loss_pnl, current_pnl,
                 status, execution_mode, dry_run, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (strategy_name, stock_code.upper(), json.dumps(legs), abs(target_pnl), abs(stop_loss_pnl), 0.0,
              "OPEN", execution_mode, 0, _now(), _now()))
        position_id = cur.lastrowid
        con.commit()
    finally:
        con.close()
    return {"ok": True, "position_id": position_id}


def parse_occ_option_symbol(occ_symbol: str) -> Optional[Dict[str, Any]]:
    """Parses a Schwab/OCC-style option symbol (e.g. 'TGT   260807C00145000')
    back into {underlying, expiry_date, right, strike_price} -- the
    inverse of schwab_trading.to_occ_symbol(), needed to turn raw
    broker positions (which only give a flat symbol string) into
    groupable legs."""
    s = occ_symbol.strip()
    # Underlying is left-padded to 6 chars; strip trailing spaces to get it back.
    if len(s) < 15:
        return None
    underlying = s[:6].strip()
    rest = s[6:]
    try:
        yymmdd = rest[:6]
        cp = rest[6].upper()
        strike_raw = rest[7:15]
        yy, mm, dd = int(yymmdd[0:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
        year = 2000 + yy
        expiry_date = f"{year:04d}-{mm:02d}-{dd:02d}"
        strike = int(strike_raw) / 1000.0
        right = "call" if cp == "C" else "put"
        return {"underlying": underlying, "expiry_date": expiry_date, "right": right, "strike_price": strike}
    except (ValueError, IndexError):
        return None


def get_order_log(position_id: int) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM schwab_order_log WHERE position_id=? ORDER BY id ASC", (position_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_all_order_logs(since: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
    """Every logged order across every position, newest first, joined
    with the parent position's strategy name/symbol/dry_run flag so a
    real incident (like unexpected fills or closes) can be audited in
    one place instead of hunting position-by-position. `since` is a
    'YYYY-MM-DD' or full timestamp string -- only orders logged on/
    after that are returned."""
    _ensure_tables()
    con = _conn()
    try:
        query = """
            SELECT l.*, p.strategy_name, p.stock_code, p.dry_run, p.status as position_status
            FROM schwab_order_log l
            JOIN schwab_auto_positions p ON p.id = l.position_id
        """
        params: List[Any] = []
        if since:
            query += " WHERE l.created_at >= ?"
            params.append(since)
        query += " ORDER BY l.id DESC LIMIT ?"
        params.append(limit)
        rows = con.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def delete_position(position_id: int) -> Dict[str, Any]:
    _ensure_tables()
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("DELETE FROM schwab_auto_positions WHERE id=?", (position_id,))
        con.commit()
        deleted = cur.rowcount > 0
    finally:
        con.close()
    return {"ok": deleted, "error": None if deleted else "position not found"}


def mark_closed_manual(position_id: int, note: str = "") -> Dict[str, Any]:
    _ensure_tables()
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("""
            UPDATE schwab_auto_positions SET status='CLOSED', closed_at=?, close_reason=?, updated_at=?
            WHERE id=? AND status IN ('OPEN','CLOSING','ERROR')
        """, (_now(), f"MANUAL_RECONCILE{(': ' + note) if note else ''}", _now(), position_id))
        con.commit()
        updated = cur.rowcount > 0
    finally:
        con.close()
    return {"ok": updated, "error": None if updated else "position not found or not in a closeable state"}


def retry_close(position_id: int, execution_mode_override: Optional[str] = None) -> Dict[str, Any]:
    _ensure_tables()
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("UPDATE schwab_auto_positions SET status='OPEN', updated_at=? WHERE id=? AND status='ERROR'", (_now(), position_id))
        con.commit()
        reset = cur.rowcount > 0
    finally:
        con.close()
    if not reset:
        return {"ok": False, "error": "position not found or not in ERROR state"}
    return close_position(position_id, reason="RETRY", execution_mode_override=execution_mode_override)


def _create_journal_entry(pos: Dict[str, Any]) -> Optional[int]:
    """Fires a real trade into the Journal (the SAME /journal/trade/add
    endpoint the manual Add Trade page uses) once a position's entry
    is confirmed filled -- so a GTC order that sits for days and fills
    while nobody's watching still ends up logged, not silently missing
    from trade history."""
    try:
        from flask import current_app
        legs_payload = [{
            "side": "buy" if leg["side"] == "LONG" else "sell",
            "option_type": leg["instrument_type"] if leg["instrument_type"] == "stock" else leg["right"],
            "strike": leg.get("strike_price") or 0,
            "expiry": leg.get("expiry_date") or "",
            "qty": leg["quantity"],
            "price": leg.get("entry_price") or 0,
        } for leg in pos["legs"]]
        payload = {
            "symbol": pos["stock_code"],
            "trade_type": pos.get("strategy_name") or "CUSTOM",
            "entry_date": _now()[:10],
            "entry_reason": f"[SCHWAB_AUTO] Auto-synced from filled order -- position #{pos['id']}",
            "legs": legs_payload,
        }
        with current_app.test_client() as client:
            resp = client.post("/journal/trade/add", json=payload)
            data = resp.get_json() or {}
        return data.get("trade_id") or data.get("id")
    except Exception as e:
        print(f"[schwab_positions] journal sync failed for position {pos['id']}: {e}")
        return None


def check_pending_entries() -> Dict[str, Any]:
    """Runs alongside monitor_tick -- checks every PENDING_ENTRY
    position's stored order for a fill. On fill: sets real entry
    prices from the fill, flips status to OPEN, places any OCO exit
    that was requested at open time (deferred until now since it
    can't be placed against an unfilled position), and logs the trade
    to the Journal. On reject/cancel/expire: marks the position ERROR
    with the reason, no journal entry."""
    _ensure_tables()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM schwab_auto_positions WHERE status='PENDING_ENTRY'").fetchall()
    finally:
        con.close()

    results = []
    for row in rows:
        legs = json.loads(row["legs_json"])
        order_id = next((leg.get("order_id") for leg in legs if leg.get("order_id")), None)
        if not order_id:
            results.append({"position_id": row["id"], "status": "no_order_id_stored"})
            continue

        fill = trading.poll_order_fill(order_id, timeout_sec=1.0, poll_interval_sec=0.3)
        status_str = (fill.get("status") or "").strip().lower()

        if fill.get("filled"):
            avg_price = fill.get("average_price")
            for leg in legs:
                if leg.get("entry_price") is None:
                    if avg_price is not None:
                        leg["entry_price"] = avg_price  # best-effort -- combo fill gives one net price, not per-leg; refined below if a live quote is available
                    if leg["instrument_type"] == "stock":
                        q = trading.get_quote(row["stock_code"])
                    else:
                        occ = trading.to_occ_symbol(row["stock_code"], leg["expiry_date"], leg["right"], leg["strike_price"])
                        q = trading.get_quote(occ)
                    if q.get("ok") and q.get("ltp") is not None:
                        leg["entry_price"] = q["ltp"]

            con = sqlite3.connect(DB_PATH)
            try:
                con.execute("UPDATE schwab_auto_positions SET status='OPEN', legs_json=?, updated_at=? WHERE id=?",
                            (json.dumps(legs), _now(), row["id"]))
                con.commit()
            finally:
                con.close()

            pos = dict(row)
            pos["legs"] = legs

            oco_order_id = None
            if row["pending_oco_target"] is not None and row["pending_oco_stop"] is not None:
                close_actions = {leg["leg_index"]: executor._close_action(leg) for leg in legs}
                oco_result = trading.place_oco_exit(row["stock_code"], legs, close_actions, row["pending_oco_target"], row["pending_oco_stop"])
                if oco_result["ok"]:
                    oco_order_id = oco_result["order_id"]
                    con = sqlite3.connect(DB_PATH)
                    try:
                        con.execute("UPDATE schwab_auto_positions SET oco_order_id=?, updated_at=? WHERE id=?", (oco_order_id, _now(), row["id"]))
                        con.commit()
                    finally:
                        con.close()

            journal_id = _create_journal_entry(pos) if not row["dry_run"] else None
            results.append({"position_id": row["id"], "status": "FILLED", "oco_order_id": oco_order_id, "journal_id": journal_id})

        elif status_str in trading._REJECTED_STATUSES:
            con = sqlite3.connect(DB_PATH)
            try:
                con.execute("UPDATE schwab_auto_positions SET status='ERROR', close_reason=?, updated_at=? WHERE id=?",
                            (f"entry order {status_str}", _now(), row["id"]))
                con.commit()
            finally:
                con.close()
            results.append({"position_id": row["id"], "status": f"ENTRY_{status_str.upper()}"})
        else:
            results.append({"position_id": row["id"], "status": "still_pending", "broker_status": fill.get("status")})

    return {"ok": True, "checked": len(rows), "results": results, "checked_at": _now()}


_pending_job_registered = False


def register_pending_entries_job() -> bool:
    global _pending_job_registered
    if _pending_job_registered:
        return False
    _pending_job_registered = True
    from .job_registry import register_job
    register_job(
        "schwab_pending_entries", "Schwab Pending Order Fill Sync",
        "Checks every PENDING_ENTRY Schwab position (GTC or still-working orders) for a fill. "
        "On fill: syncs entry prices, places any deferred OCO exit, and logs the trade to the Journal.",
        kind="interval", default_schedule={"interval_min": 1},
        group="Manual / One-Time", run_now_fn=check_pending_entries,
    )
    return True


def monitor_tick() -> Dict[str, Any]:
    _ensure_tables()
    with _lock:
        positions = list_positions(status="OPEN")
        results = []
        for pos in positions:
            # If a native-combo OCO exit was placed at open, check
            # whether Schwab already filled it before this poller does
            # anything -- otherwise the poller could try to close a
            # position the OCO already closed (or vice versa: an OCO
            # that's still working while the poller ALSO fires would
            # double-close). Whichever side wins, the position is
            # marked closed and the other side is cleaned up.
            if pos.get("oco_order_id"):
                oco_status = trading.get_order_status(pos["oco_order_id"])
                status_str = (oco_status.get("status") or "").strip().lower()
                if oco_status.get("ok") and status_str in ("filled", "executed"):
                    con = sqlite3.connect(DB_PATH)
                    try:
                        con.execute("UPDATE schwab_auto_positions SET status='CLOSED', closed_at=?, close_reason='OCO_FILLED', updated_at=? WHERE id=?",
                                    (_now(), _now(), pos["id"]))
                        con.commit()
                    finally:
                        con.close()
                    results.append({"position_id": pos["id"], "status": "OCO_FILLED"})
                    continue

            pnl_result = executor.compute_combined_pnl(pos["stock_code"], pos["legs"])
            if not pnl_result["ok"] and pnl_result["partial"]:
                results.append({"position_id": pos["id"], "status": "quote_error", "detail": pnl_result})
                continue
            pnl = pnl_result["combined_pnl"]
            con = sqlite3.connect(DB_PATH)
            try:
                con.execute("UPDATE schwab_auto_positions SET current_pnl=?, updated_at=? WHERE id=?", (pnl, _now(), pos["id"]))
                con.commit()
            finally:
                con.close()

            reason = None
            # SAFETY FIX: target_pnl=0 / stop_loss_pnl=0 means "no
            # target/stop was set" (e.g. a position tracked for
            # monitoring only), NOT a literal $0 threshold. The old
            # code did `pnl >= target_pnl` unconditionally -- with
            # target_pnl=0 that's just `pnl >= 0`, true for almost any
            # position almost immediately, auto-closing it within one
            # poll cycle even though no target was ever actually
            # configured. Confirmed as the cause of a real incident:
            # positions closing without being asked to. Now a
            # threshold of 0 (or unset) disables that specific check
            # entirely instead of firing on it.
            if pos["target_pnl"] and pos["target_pnl"] > 0 and pnl >= pos["target_pnl"]:
                reason = "TARGET_HIT"
            elif pos["stop_loss_pnl"] and pos["stop_loss_pnl"] > 0 and pnl <= -pos["stop_loss_pnl"]:
                reason = "STOP_HIT"
            if reason:
                if pos.get("oco_order_id"):
                    cancel_result = trading.cancel_order(pos["oco_order_id"])
                    if not cancel_result.get("ok"):
                        results.append({"position_id": pos["id"], "status": "oco_cancel_failed", "detail": cancel_result})
                        continue  # don't fire a second close attempt while the OCO might still be live
                close_result = close_position(pos["id"], reason=reason)
                results.append({"position_id": pos["id"], "status": reason, "pnl": pnl, "close_result": close_result})
            else:
                results.append({"position_id": pos["id"], "status": "monitoring", "pnl": pnl})
        return {"ok": True, "checked": len(positions), "results": results, "checked_at": _now()}


_registered = False
_reg_lock = threading.Lock()


def register_monitor_job() -> bool:
    global _registered
    with _reg_lock:
        if _registered:
            return False
        _registered = True
    from .job_registry import register_job
    register_job(
        "schwab_pnl_monitor", "Schwab Auto-Trading P&L Monitor",
        "Checks every OPEN Schwab position's combined P&L every 30 seconds and "
        "auto-closes (safe short-first sequencing) on target/stop-loss hit.",
        kind="interval", default_schedule={"interval_min": 0.5},
        group="Manual / One-Time", run_now_fn=monitor_tick,
    )
    return True
