# oiapp/services/job_registry.py
"""
Central registry for background scheduled jobs.

Individual scheduler modules (scheduled_jobs.py, signal_notifier.py, etc.)
call register_job() once at startup to describe what they run and how, and
call is_enabled()/get_schedule() from inside their loop each cycle so that
changes made on the Scheduler page take effect without restarting the app.
Modules that call mark_run() after each execution get "last run" visibility
on the page; ones that don't can still be listed (schedule + enable/disable
+ manual run), just without a last-run timestamp.

Everything here is intentionally dependency-free (stdlib + sqlite3) so it
can be imported from any scanner/service module without circular imports.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

_registry: Dict[str, Dict[str, Any]] = {}
_registry_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    # Was 5000 (5s) -- raised to match the more generous 30s convention
    # already used elsewhere in this app (e.g. tastytrade_options_backfill.py's
    # _conn()). The production log shows this DB under real, sustained
    # concurrent load (a separate volume_profile_scanner bug -- since
    # fixed -- was hammering it with rapid, failing retries across most
    # of a watchlist, plus signal_notifier's own watcher loop hitting
    # "database is locked" repeatedly in the same window). A short
    # busy_timeout under that load fails fast when waiting longer would
    # often just succeed.
    c.execute("PRAGMA busy_timeout=30000")
    return c


_table_ready = False


def _ensure_table():
    global _table_ready
    if _table_ready:
        return
    con = _conn()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_jobs_state (
                job_key       TEXT PRIMARY KEY,
                enabled       INTEGER NOT NULL DEFAULT 1,
                schedule_json TEXT,
                last_run_at   TEXT,
                last_status   TEXT,
                last_note     TEXT
            )
        """)
        # Persistent history of every run (not just "last run") -- start
        # time, end time, duration, status. This is what actually answers
        # "did the 7:30 pipeline run, is it done, how long did it take" --
        # scheduled_jobs_state above only ever remembers the most recent
        # run, so there's no way to see e.g. "yesterday's run took 4x as
        # long as usual" or confirm a run actually started at the time it
        # was supposed to.
        con.execute("""
            CREATE TABLE IF NOT EXISTS job_run_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                job_key       TEXT NOT NULL,
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                duration_sec  REAL,
                status        TEXT NOT NULL DEFAULT 'running',
                note          TEXT
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_job_run_log_key_started ON job_run_log(job_key, started_at DESC)")
        con.commit()
        _table_ready = True
    finally:
        con.close()


def log_run_start(job_key: str) -> int:
    """Records that a run just started; returns the log row id to pass to
    log_run_finish() when it completes. Call this at the same point
    mark_start() is called (they serve different purposes: mark_start's
    in-memory _running dict is for "is this job running right now" checks
    within this process; this is the persistent, queryable history)."""
    _ensure_table()
    con = _conn()
    try:
        cur = con.execute(
            "INSERT INTO job_run_log (job_key, started_at, status) VALUES (?, ?, 'running')",
            (job_key, datetime.now().isoformat(timespec="seconds")),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def log_run_finish(run_id: int, ok: bool, note: str = "") -> None:
    """Completes the run record started by log_run_start()."""
    if not run_id:
        return
    _ensure_table()
    con = _conn()
    try:
        row = con.execute("SELECT started_at FROM job_run_log WHERE id=?", (run_id,)).fetchone()
        duration = None
        if row and row["started_at"]:
            try:
                started = datetime.fromisoformat(row["started_at"])
                duration = round((datetime.now() - started).total_seconds(), 1)
            except Exception:
                pass
        con.execute(
            "UPDATE job_run_log SET finished_at=?, duration_sec=?, status=?, note=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), duration,
             "ok" if ok else "error", (note or "")[:500], run_id),
        )
        con.commit()
    finally:
        con.close()


def get_run_history(job_key: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Recent run history, newest first. Pass job_key to filter to one
    job, or None for a combined feed across every job (what a
    notifications view wants)."""
    _ensure_table()
    con = _conn()
    try:
        if job_key:
            rows = con.execute(
                "SELECT * FROM job_run_log WHERE job_key=? ORDER BY started_at DESC LIMIT ?",
                (job_key, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM job_run_log ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        now = datetime.now()
        out = []
        for r in rows:
            d = dict(r)
            if d["status"] == "running" and d["started_at"]:
                # still running (or was, when the process handling it last
                # checked in) -- show live elapsed time instead of nothing
                try:
                    d["elapsed_sec"] = round((now - datetime.fromisoformat(d["started_at"])).total_seconds(), 1)
                except Exception:
                    d["elapsed_sec"] = None
            out.append(d)
        return out
    finally:
        con.close()


def _get_state_row(job_key: str) -> Optional[sqlite3.Row]:
    _ensure_table()
    con = _conn()
    try:
        return con.execute("SELECT * FROM scheduled_jobs_state WHERE job_key=?", (job_key,)).fetchone()
    finally:
        con.close()


def get_last_run_at(job_key: str) -> Optional[float]:
    """Persisted last-run time as a unix timestamp, or None if this job
    has never run (or never called mark_run()). Used by
    unified_scheduler.register() to seed its in-memory due-time on
    startup -- without this, every job's in-memory clock resets to "never
    run" on every process restart, making all of them look simultaneously
    overdue and dispatch in the same first tick (a startup thundering
    herd -- background jobs and interactive requests competing for
    SQLite + worker threads right when someone is most likely to be
    actively using the app right after starting it)."""
    row = _get_state_row(job_key)
    if not row or not row["last_run_at"]:
        return None
    try:
        return datetime.fromisoformat(row["last_run_at"]).timestamp()
    except Exception:
        return None


def _upsert_state(job_key: str, **fields) -> None:
    _ensure_table()
    con = _conn()
    try:
        existing = con.execute("SELECT job_key FROM scheduled_jobs_state WHERE job_key=?", (job_key,)).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE scheduled_jobs_state SET {sets} WHERE job_key=?", (*fields.values(), job_key))
        else:
            cols = ["job_key"] + list(fields.keys())
            placeholders = ", ".join("?" for _ in cols)
            con.execute(f"INSERT INTO scheduled_jobs_state ({', '.join(cols)}) VALUES ({placeholders})",
                        (job_key, *fields.values()))
        con.commit()
    finally:
        con.close()


def register_job(
    key: str,
    label: str,
    description: str,
    kind: str,
    default_schedule: Dict[str, Any],
    group: str = "General",
    run_now_fn: Optional[Callable[[], Any]] = None,
    editable: bool = True,
) -> None:
    """Register a job's static metadata.

    kind: 'time' (runs at specific HH:MM times, optionally certain
          weekdays) or 'interval' (runs every N minutes).
    default_schedule:
        time:     {"times": ["07:30", "08:45"], "weekdays": [0,1,2,3,4] or None}
        interval: {"interval_min": 15}
    run_now_fn: zero-arg callable that runs the job immediately (used by the
        "Run now" button). May be None if a job can't safely be triggered
        on demand (still listed, just without that button).
    editable: whether the Scheduler page lets the user change the schedule
        for this job (some jobs may only support enable/disable + manual run).
    """
    with _registry_lock:
        _registry[key] = {
            "key": key,
            "label": label,
            "description": description,
            "kind": kind,
            "default_schedule": default_schedule,
            "group": group,
            "run_now_fn": run_now_fn,
            "editable": editable,
        }


_GLOBAL_PAUSE_KEY = "__global_pause__"


def is_globally_paused() -> bool:
    """When true, is_enabled() returns False for every job regardless of
    each job's own individual enabled/disabled state -- a single master
    switch for 'pause all background processing' (e.g. during active
    trading hours when you want the app maximally responsive), without
    overwriting anyone's per-job preferences. Turning this back off
    restores every job to whatever its own individual setting already was.

    Defaults to PAUSED (True) when no explicit preference has been saved
    yet -- background processes do not run out of the box; you opt in by
    unchecking 'Pause background processes' on the Realtime Dashboard."""
    row = _get_state_row(_GLOBAL_PAUSE_KEY)
    if row is None:
        return True
    return bool(row["enabled"])


def set_global_pause(paused: bool) -> None:
    _upsert_state(_GLOBAL_PAUSE_KEY, enabled=1 if paused else 0)


def _signal_notifier_enabled_display() -> bool:
    """Just this job's own toggle state, for list_jobs() display -- deliberately
    NOT folded with is_globally_paused() here, same as every other job's
    displayed 'enabled' field (which reflects its own per-job toggle only,
    not the separate global-pause switch)."""
    try:
        from ..scanners.signal_notifier import get_config
        return bool(get_config().get("enabled"))
    except Exception:
        return True


def is_enabled(job_key: str) -> bool:
    if is_globally_paused():
        return False
    if job_key == "signal_notifier":
        # Signal Notifier runs its own dedicated thread (not dispatched
        # through unified_scheduler like other jobs), and checks its OWN
        # config's "enabled" flag -- not this generic per-job column. Without
        # this special case, the Scheduler Hub toggle for it was cosmetic:
        # flipping it here had zero effect on whether Signal Notifier (or
        # anything folded into its loop -- journal P&L/health alerts,
        # telegram price alerts) actually ran. Scheduler Hub is meant to be
        # the one place that governs everything, including this, so read
        # the real source of truth here instead of a disconnected duplicate.
        return _signal_notifier_enabled_display()
    row = _get_state_row(job_key)
    if row is None:
        return True
    return bool(row["enabled"])


def set_enabled(job_key: str, enabled: bool) -> None:
    if job_key == "signal_notifier":
        try:
            from ..scanners.signal_notifier import set_config
            set_config(enabled=bool(enabled))
        except Exception:
            pass
    _upsert_state(job_key, enabled=1 if enabled else 0)


def get_schedule(job_key: str) -> Dict[str, Any]:
    row = _get_state_row(job_key)
    meta = _registry.get(job_key)
    default = meta["default_schedule"] if meta else {}
    if row is None or not row["schedule_json"]:
        return dict(default)
    try:
        return json.loads(row["schedule_json"])
    except Exception:
        return dict(default)


def set_schedule(job_key: str, schedule: Dict[str, Any]) -> None:
    _upsert_state(job_key, schedule_json=json.dumps(schedule))


_running: Dict[str, float] = {}
_running_lock = threading.Lock()

# ---------------------------------------------------------------------
# Cooperative stop requests -- Python can't forcibly kill a running
# thread safely, so this is a flag a long-running job checks at its own
# natural checkpoints (between sources, between symbols, between steps)
# and aborts early when set. It stops the CURRENT run, not the job's
# future scheduling -- the watcher/dispatcher itself keeps running and
# will pick the job up again on its next due time, same as if it had
# finished normally. In-memory only (not persisted): a stop request that
# outlives the process it was meant for doesn't mean anything anyway.
# ---------------------------------------------------------------------
_stop_requested: Dict[str, bool] = {}
_stop_lock = threading.Lock()


def request_stop(job_key: str) -> None:
    with _stop_lock:
        _stop_requested[job_key] = True


def is_stop_requested(job_key: str) -> bool:
    with _stop_lock:
        return bool(_stop_requested.get(job_key))


def clear_stop(job_key: str) -> None:
    with _stop_lock:
        _stop_requested.pop(job_key, None)


def mark_start(job_key: str) -> None:
    with _running_lock:
        _running[job_key] = _time.time()


def mark_finished(job_key: str) -> None:
    with _running_lock:
        _running.pop(job_key, None)
    clear_stop(job_key)


def mark_run(job_key: str, ok: bool, note: str = "") -> None:
    _upsert_state(job_key, last_run_at=datetime.now().isoformat(), last_status="ok" if ok else "error", last_note=note[:500])


def run_now(job_key: str) -> Dict[str, Any]:
    meta = _registry.get(job_key)
    if not meta:
        return {"ok": False, "error": "Unknown job"}
    fn = meta.get("run_now_fn")
    if not fn:
        return {"ok": False, "error": "This job does not support manual triggering."}
    with _running_lock:
        if job_key in _running:
            return {"ok": False, "error": "Already running — wait for it to finish before running it again."}

    def _bg():
        mark_start(job_key)
        run_id = log_run_start(job_key)
        try:
            fn()
            mark_run(job_key, True, "Manually triggered")
            log_run_finish(run_id, True, "Manually triggered")
        except Exception as e:
            mark_run(job_key, False, f"Manual run failed: {e}")
            log_run_finish(run_id, False, f"Manual run failed: {e}")
        finally:
            mark_finished(job_key)

    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return {"ok": True, "message": f"{meta['label']} started in the background."}


def list_jobs() -> List[Dict[str, Any]]:
    _ensure_table()
    out = []
    with _registry_lock:
        items = list(_registry.values())
    now = _time.time()
    for meta in items:
        row = _get_state_row(meta["key"])
        with _running_lock:
            started_at = _running.get(meta["key"])
        out.append({
            "key": meta["key"],
            "label": meta["label"],
            "description": meta["description"],
            "kind": meta["kind"],
            "group": meta["group"],
            "editable": meta["editable"],
            "can_run_now": meta.get("run_now_fn") is not None,
            "enabled": (
                _signal_notifier_enabled_display() if meta["key"] == "signal_notifier"
                else (bool(row["enabled"]) if row else True)
            ),
            "schedule": get_schedule(meta["key"]),
            "default_schedule": meta["default_schedule"],
            "last_run_at": row["last_run_at"] if row else None,
            "last_status": row["last_status"] if row else None,
            "last_note": row["last_note"] if row else None,
            "is_running": started_at is not None,
            "running_seconds": round(now - started_at, 1) if started_at is not None else None,
            "stop_requested": is_stop_requested(meta["key"]),
        })
    out.sort(key=lambda j: (j["group"], j["label"]))
    return out
