# oiapp/services/alert_outbox.py
"""
Async publisher/subscriber pattern for alert delivery.

PRODUCER: any code that wants to send an alert calls queue_alert(...).
This writes one row to alert_outbox with status='pending' and returns
immediately -- it does NOT send anything, does NOT touch the network,
and can never block whatever is producing the alert (a price check, a
health-score check, a P&L check) on a slow Telegram API call.

SUBSCRIBER/CONSUMER: process_due_alerts() picks up every pending row
whose due_at has passed, sends it via the right channel, and marks it
sent (with sent_at) or failed (with the error) so it is NEVER resent --
exactly the "pick up what's due, send it, mark it sent" flow requested.
Call this on a schedule; it's wired into Signal Notifier's loop
alongside the other alert checks, on its own short interval, since
delivery should be prompt even though production and delivery are
decoupled.

Why this matters over the old pattern (call send_telegram_message()
inline wherever an alert condition is found): every producer used to
need its own retry/error handling, a slow/failed Telegram call blocked
whatever was checking for the alert condition in the first place, and
there was no single place to see "here's every alert this app has ever
tried to send and whether it went through." Now there's one.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..config import DB_PATH as _OIAPP_DB_PATH
DB_PATH = _OIAPP_DB_PATH

_table_ready = False
_table_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def _ensure_table():
    global _table_ready
    if _table_ready:
        return
    with _table_lock:
        if _table_ready:
            return
        con = _conn()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS alert_outbox (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at   TEXT NOT NULL,
                    due_at       TEXT NOT NULL,
                    channel      TEXT NOT NULL DEFAULT 'telegram',
                    source       TEXT,
                    message      TEXT NOT NULL,
                    payload_json TEXT,
                    status       TEXT NOT NULL DEFAULT 'pending',
                    attempts     INTEGER NOT NULL DEFAULT 0,
                    sent_at      TEXT,
                    error        TEXT
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_alert_outbox_status_due ON alert_outbox(status, due_at)")
            con.commit()
            _table_ready = True
        finally:
            con.close()


def queue_alert(
    message: str,
    channel: str = "telegram",
    source: Optional[str] = None,
    due_at: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> int:
    """PRODUCER API. Writes the alert to the outbox and returns its row id
    immediately -- never blocks on network I/O. `source` should identify
    what produced it (e.g. 'telegram_price_alert', 'journal_health',
    'journal_pnl') so get_recent()/the UI can show where each alert came
    from. `due_at` defaults to now (send ASAP); set it in the future to
    schedule delivery for later.
    """
    _ensure_table()
    con = _conn()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        cur = con.execute(
            "INSERT INTO alert_outbox (created_at, due_at, channel, source, message, payload_json) "
            "VALUES (?,?,?,?,?,?)",
            (now, due_at or now, channel, source, message, json.dumps(payload) if payload else None),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _dispatch(row: sqlite3.Row) -> Tuple[bool, Optional[str]]:
    channel = row["channel"]
    try:
        if channel == "telegram":
            from .telegram_alerts import send_telegram_message
            result = send_telegram_message(row["message"])
            if result.get("ok"):
                return True, None
            return False, result.get("description") or result.get("error") or str(result)
        return False, f"Unknown channel: {channel}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def process_due_alerts(limit: int = 50) -> Dict[str, int]:
    """CONSUMER/SUBSCRIBER API. Sends every pending, due alert and marks
    each sent or failed so it is never resent. Call this on a schedule --
    Signal Notifier's loop calls it every tick alongside the other alert
    checks."""
    _ensure_table()
    con = _conn()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        rows = con.execute(
            "SELECT * FROM alert_outbox WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT ?",
            (now, limit),
        ).fetchall()
    finally:
        con.close()

    sent = 0
    failed = 0
    for row in rows:
        ok, err = _dispatch(row)
        con = _conn()
        try:
            if ok:
                con.execute(
                    "UPDATE alert_outbox SET status='sent', sent_at=?, attempts=attempts+1 WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), row["id"]),
                )
                sent += 1
            else:
                con.execute(
                    "UPDATE alert_outbox SET status='failed', error=?, attempts=attempts+1 WHERE id=?",
                    (str(err)[:500], row["id"]),
                )
                failed += 1
            con.commit()
        finally:
            con.close()
    return {"checked": len(rows), "sent": sent, "failed": failed}


def get_recent(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """For a UI: recent alerts, optionally filtered by status
    ('pending'/'sent'/'failed')."""
    _ensure_table()
    con = _conn()
    try:
        if status:
            rows = con.execute(
                "SELECT * FROM alert_outbox WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM alert_outbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()
