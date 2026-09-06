"""icici_positions.py -- V113 addition.

Position tracking + live P&L monitoring for ICICI auto-trading.
Target/stop-loss basis: combined P&L in rupees across the whole
position (per explicit choice), checked on every monitor tick against
`target_pnl_rupees` (positive number, auto-close when P&L >= this) and
`stop_loss_pnl_rupees` (positive number representing max acceptable
loss, auto-close when P&L <= -this).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..db import DB_PATH
from . import vertical_executor as executor
from . import icici_breeze as breeze

MONITOR_INTERVAL_SEC = 30
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _ensure_tables() -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS icici_auto_positions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name       TEXT,
                stock_code          TEXT NOT NULL,
                expiry_date         TEXT NOT NULL,
                lot_size            INTEGER NOT NULL DEFAULT 1,
                legs_json           TEXT NOT NULL,
                entry_net_price     REAL,
                target_pnl_rupees   REAL NOT NULL,
                stop_loss_pnl_rupees REAL NOT NULL,
                current_pnl_rupees  REAL,
                status              TEXT NOT NULL DEFAULT 'OPENING',
                execution_mode      TEXT NOT NULL DEFAULT 'safe_sequential',
                dry_run             INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                closed_at           TEXT,
                close_reason        TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS icici_order_log (
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
        con.execute("CREATE INDEX IF NOT EXISTS idx_icici_pos_status ON icici_auto_positions(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_icici_log_pos ON icici_order_log(position_id)")
        cols = {r[1] for r in con.execute("PRAGMA table_info(icici_auto_positions)").fetchall()}
        if "execution_mode" not in cols:
            con.execute("ALTER TABLE icici_auto_positions ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'safe_sequential'")
        con.commit()
    finally:
        con.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log_leg_results(position_id: int, phase: str, leg_results: List[Dict[str, Any]]) -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        for r in leg_results:
            con.execute("""
                INSERT INTO icici_order_log (position_id, phase, leg_index, side, action, order_id, ok, error, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (position_id, phase, r.get("leg_index"), r.get("side"), r.get("action"),
                  r.get("order_id"), int(bool(r.get("ok"))), r.get("error"), _now()))
        con.commit()
    finally:
        con.close()


def open_position(
    strategy_name: str,
    stock_code: str,
    expiry_date: str,
    legs: List[Dict[str, Any]],
    target_pnl_rupees: float,
    stop_loss_pnl_rupees: float,
    lot_size: int = 1,
    order_type: str = "market",
    execution_mode: str = "safe_sequential",
    price_by_leg: Optional[Dict[int, float]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Validates legs, opens via the safe sequencer, and only persists
    the position row if execution succeeded -- a failed/aborted open
    is reported back to the caller with the partial leg_results so
    they know exactly what state the account is in, but is NOT
    silently recorded as a tracked position (that would make an
    incomplete position invisible to monitoring)."""
    _ensure_tables()
    # SAFETY: same fix as the Schwab side -- Live Trading OFF must
    # block every path into a real order, not just the strategy
    # engine's automatic firing. Previously a manual Open Position
    # call here used only its own per-request dry_run flag.
    if not dry_run and not breeze.is_live_trading_enabled():
        dry_run = True

    if not legs:
        return {"ok": False, "error": "at least one leg is required"}
    for leg in legs:
        if leg.get("side") not in ("LONG", "SHORT"):
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: side must be LONG or SHORT"}
        if leg.get("right") not in ("call", "put"):
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: right must be call or put"}

    result = executor.execute_open(
        stock_code=stock_code, expiry_date=expiry_date, legs=legs,
        order_type=order_type, execution_mode=execution_mode,
        price_by_leg=price_by_leg, dry_run=dry_run,
    )

    if not result["ok"]:
        return {
            "ok": False,
            "error": f"open sequence aborted after leg {result['aborted_after_leg']} -- see leg_results for exact account state",
            "leg_results": result["leg_results"],
        }

    # V126 fix: entry_price was never actually being set here for a
    # freshly-opened position (only adopt_broker_positions() set it,
    # from the broker's own average_price on an ALREADY-held leg) --
    # meaning P&L for anything opened through this function was
    # computing against a None entry_price the whole time. Fixed: use
    # the confirmed fill's average_price when safe_sequential mode
    # captured one; otherwise (simultaneous_market, which doesn't wait
    # for fill confirmation, or a dry-run/paper position where there's
    # no real fill at all) fetch a live quote right now as the entry
    # reference -- always live-sourced, never a stored/stale number,
    # per the "all rule computation on live ICICI prices" requirement.
    for leg, leg_result in zip(executor._sequence_for_open(legs), result["leg_results"]):
        leg["order_id"] = leg_result.get("order_id")
        entry_price = leg_result.get("fill_average_price")
        if entry_price is None:
            q = breeze.get_quote(stock_code, expiry_date, leg["right"], leg["strike_price"])
            entry_price = q.get("ltp") if q.get("ok") else None
        leg["entry_price"] = entry_price

    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("""
            INSERT INTO icici_auto_positions
                (strategy_name, stock_code, expiry_date, lot_size, legs_json, entry_net_price,
                 target_pnl_rupees, stop_loss_pnl_rupees, current_pnl_rupees, status, execution_mode, dry_run,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            strategy_name, stock_code, expiry_date, lot_size, json.dumps(legs), None,
            abs(target_pnl_rupees), abs(stop_loss_pnl_rupees), 0.0,
            "OPEN", execution_mode, int(dry_run), _now(), _now(),
        ))
        position_id = cur.lastrowid
        con.commit()
    finally:
        con.close()

    _log_leg_results(position_id, "OPEN", result["leg_results"])
    return {"ok": True, "position_id": position_id, "leg_results": result["leg_results"]}


def close_position(position_id: int, reason: str = "MANUAL", order_type: Optional[str] = None,
                    execution_mode_override: Optional[str] = None) -> Dict[str, Any]:
    _ensure_tables()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM icici_auto_positions WHERE id=?", (position_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return {"ok": False, "error": "position not found"}
    if row["status"] not in ("OPEN",):
        return {"ok": False, "error": f"position is not OPEN (status={row['status']})"}

    legs = json.loads(row["legs_json"])
    dry_run = bool(row["dry_run"])
    # Uses the SAME execution mode chosen at open time by default --
    # important because auto-close (target/stop-loss hit) fires from
    # the background monitor, not from the same request as opening, so
    # there's no "current" mode to fall back to other than what was
    # stored. A manual close can still override it explicitly.
    execution_mode = execution_mode_override or row["execution_mode"] or "safe_sequential"

    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE icici_auto_positions SET status='CLOSING', updated_at=? WHERE id=?", (_now(), position_id))
        con.commit()
    finally:
        con.close()

    result = executor.execute_close(
        stock_code=row["stock_code"], expiry_date=row["expiry_date"], legs=legs,
        order_type=order_type or "market", execution_mode=execution_mode, dry_run=dry_run,
    )
    _log_leg_results(position_id, "CLOSE", result["leg_results"])

    con = sqlite3.connect(DB_PATH)
    try:
        if result["ok"]:
            con.execute("""
                UPDATE icici_auto_positions
                SET status='CLOSED', closed_at=?, close_reason=?, updated_at=?
                WHERE id=?
            """, (_now(), reason, _now(), position_id))
        else:
            con.execute("""
                UPDATE icici_auto_positions
                SET status='ERROR', close_reason=?, updated_at=?
                WHERE id=?
            """, (f"close aborted after leg {result['aborted_after_leg']}", _now(), position_id))
        con.commit()
    finally:
        con.close()

    return {"ok": result["ok"], "leg_results": result["leg_results"]}


def delete_position(position_id: int) -> Dict[str, Any]:
    """Remove a tracked row entirely. Does NOT place any orders --
    purely a record cleanup for bad test rows / duplicate adopts /
    anything left stuck (e.g. an ERROR row from a close attempt that
    ran with no active broker session). If real legs are still open at
    the broker, deleting the tracked row does not close them -- this
    only stops OUR monitoring of it."""
    _ensure_tables()
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("DELETE FROM icici_auto_positions WHERE id=?", (position_id,))
        con.commit()
        deleted = cur.rowcount > 0
    finally:
        con.close()
    if not deleted:
        return {"ok": False, "error": "position not found"}
    return {"ok": True}


def mark_closed_manual(position_id: int, note: str = "") -> Dict[str, Any]:
    """For when the legs were actually closed manually at the broker
    (or some other way outside this app) and you just need the tracked
    row reconciled -- no orders placed, just updates status/closed_at
    so it stops showing as OPEN/ERROR and stops being polled."""
    _ensure_tables()
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("""
            UPDATE icici_auto_positions
            SET status='CLOSED', closed_at=?, close_reason=?, updated_at=?
            WHERE id=? AND status IN ('OPEN','CLOSING','ERROR')
        """, (_now(), f"MANUAL_RECONCILE{(': ' + note) if note else ''}", _now(), position_id))
        con.commit()
        updated = cur.rowcount > 0
    finally:
        con.close()
    if not updated:
        return {"ok": False, "error": "position not found or not in a closeable state"}
    return {"ok": True}


def retry_close(position_id: int, execution_mode_override: Optional[str] = None) -> Dict[str, Any]:
    """For an ERROR-status position (a close attempt that aborted
    partway, e.g. because there was no active Breeze session at the
    time) -- resets status back to OPEN and re-runs the normal close
    sequence. Uses whatever legs are still recorded on the position;
    if some legs were actually closed by the earlier failed attempt
    before it aborted, this will try to close ALL recorded legs again,
    which could double-close an already-closed leg -- check the order
    log for that position before retrying if you're not sure how far
    the earlier attempt got."""
    _ensure_tables()
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("UPDATE icici_auto_positions SET status='OPEN', updated_at=? WHERE id=? AND status='ERROR'",
                           (_now(), position_id))
        con.commit()
        reset = cur.rowcount > 0
    finally:
        con.close()
    if not reset:
        return {"ok": False, "error": "position not found or not in ERROR state"}
    return close_position(position_id, reason="RETRY", execution_mode_override=execution_mode_override)


def list_positions(status: Optional[str] = None) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        if status:
            rows = con.execute("SELECT * FROM icici_auto_positions WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM icici_auto_positions ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["legs"] = json.loads(d.pop("legs_json") or "[]")
            out.append(d)
        return out
    finally:
        con.close()


def get_order_log(position_id: int) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM icici_order_log WHERE position_id=? ORDER BY id ASC", (position_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def adopt_broker_positions(
    strategy_name: str,
    stock_code: str,
    expiry_date: str,
    legs: List[Dict[str, Any]],
    target_pnl_rupees: float,
    stop_loss_pnl_rupees: float,
    lot_size: int = 1,
    execution_mode: str = "safe_sequential",
) -> Dict[str, Any]:
    """Bundle one or more ALREADY-HELD broker positions (from
    get_portfolio_positions) into a new tracked row for monitoring --
    no orders are placed, since these legs already exist in the
    account. Each leg needs entry_price set from the position's real
    average_price (the caller passes this through from the broker
    positions list) so P&L math is accurate from adoption onward."""
    _ensure_tables()
    if not legs:
        return {"ok": False, "error": "at least one leg is required"}
    for leg in legs:
        if leg.get("side") not in ("LONG", "SHORT"):
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: side must be LONG or SHORT"}
        if leg.get("right") not in ("call", "put"):
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: right must be call or put"}
        if leg.get("entry_price") is None:
            return {"ok": False, "error": f"leg {leg.get('leg_index')}: entry_price is required when adopting an existing broker position"}

    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("""
            INSERT INTO icici_auto_positions
                (strategy_name, stock_code, expiry_date, lot_size, legs_json, entry_net_price,
                 target_pnl_rupees, stop_loss_pnl_rupees, current_pnl_rupees, status, execution_mode, dry_run,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            strategy_name, stock_code, expiry_date, lot_size, json.dumps(legs), None,
            abs(target_pnl_rupees), abs(stop_loss_pnl_rupees), 0.0,
            "OPEN", execution_mode, 0, _now(), _now(),
        ))
        position_id = cur.lastrowid
        con.commit()
    finally:
        con.close()

    _log_leg_results(position_id, "ADOPTED", [
        {"leg_index": leg["leg_index"], "side": leg["side"], "action": "adopted_existing", "ok": True, "order_id": None}
        for leg in legs
    ])
    return {"ok": True, "position_id": position_id}


def monitor_tick() -> Dict[str, Any]:
    """One pass over every OPEN position: refresh combined P&L, and
    auto-close if target or stop-loss has been hit. Registered as a
    periodic job -- also callable directly for a manual 'check now'
    from the UI."""
    _ensure_tables()
    if not breeze.is_nse_market_hours():
        return {"ok": True, "checked": 0, "results": [], "checked_at": _now(), "note": "outside NSE market hours -- skipped"}
    with _lock:
        positions = list_positions(status="OPEN")
        results = []
        for pos in positions:
            # V126: dry_run/paper positions now DO get live P&L and
            # target/stop-loss auto-close -- their entry_price is a
            # real live-sourced number (see open_position's V126 fix),
            # and execute_close() already simulates the safe sequencing
            # without placing real orders when dry_run is set. Only
            # actual order placement is skipped for these, never the
            # rule computation itself.
            pnl_result = executor.compute_combined_pnl_rupees(
                pos["stock_code"], pos["expiry_date"], pos["legs"], pos.get("lot_size") or 1
            )
            if not pnl_result["ok"] and pnl_result["partial"]:
                results.append({"position_id": pos["id"], "status": "quote_error", "detail": pnl_result})
                continue

            pnl = pnl_result["combined_pnl_rupees"]
            con = sqlite3.connect(DB_PATH)
            try:
                con.execute("UPDATE icici_auto_positions SET current_pnl_rupees=?, updated_at=? WHERE id=?",
                            (pnl, _now(), pos["id"]))
                con.commit()
            finally:
                con.close()

            reason = None
            # Same fix as the Schwab side: a target/stop of 0 means
            # "not set," not a literal Rs.0 threshold -- otherwise
            # `pnl >= 0` fires on almost any position almost immediately.
            if pos["target_pnl_rupees"] and pos["target_pnl_rupees"] > 0 and pnl >= pos["target_pnl_rupees"]:
                reason = "TARGET_HIT"
            elif pos["stop_loss_pnl_rupees"] and pos["stop_loss_pnl_rupees"] > 0 and pnl <= -pos["stop_loss_pnl_rupees"]:
                reason = "STOP_HIT"

            if reason:
                close_result = close_position(pos["id"], reason=reason)
                results.append({"position_id": pos["id"], "status": reason, "pnl": pnl, "close_result": close_result})
            else:
                results.append({"position_id": pos["id"], "status": "monitoring", "pnl": pnl})

        return {"ok": True, "checked": len(positions), "results": results, "checked_at": _now()}


_registered = False
_reg_lock = threading.Lock()


def register_icici_monitor_job() -> bool:
    global _registered
    with _reg_lock:
        if _registered:
            return False
        _registered = True
    from .job_registry import register_job
    register_job(
        "icici_pnl_monitor", "ICICI Auto-Trading P&L Monitor",
        "Checks every OPEN ICICI auto-trading position's combined rupee P&L and "
        "auto-closes (with the same safe short-first leg sequencing used to open) "
        "if the target or stop-loss threshold is hit. Runs every 30 seconds during "
        "market hours by default; Run Now triggers an immediate check.",
        kind="interval", default_schedule={"interval_min": 0.5},
        group="Manual / One-Time",
        run_now_fn=monitor_tick,
    )
    return True
