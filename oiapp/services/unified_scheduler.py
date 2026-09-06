"""unified_scheduler.py — single background dispatcher thread for all
interval-based watcher jobs (price backfill, technical snapshot cache,
telegram/trade/health alerts, signal notifier, agentic AI scanner, etc.)

BEFORE this module existed, every one of those jobs ran its own
`while True: ...; time.sleep(N)` thread. Each thread woke up on its own
schedule, opened its own SQLite connection(s), and went back to sleep --
independently of every other thread. With ~10 of these running
concurrently, the process carries 10 idle-but-alive OS threads and the
GIL + SQLite file handle churn is uncoordinated (e.g. two watchers can
both decide to open a connection in the same 50ms window for no reason
other than bad luck in their sleep timers).

This module replaces that with ONE dispatcher thread that ticks every 2
seconds, checks which registered jobs are due (respecting job_registry's
existing enabled/disabled + schedule state, so the Scheduler Hub UI page
keeps working exactly as before), and hands due work off to a small
shared, bounded ThreadPoolExecutor. A job that's still running when its
next tick comes due is simply skipped that tick (never double-queued).

Each watcher module keeps its existing `start_xxx_watcher()` public
function name/signature/return-value contract (True if newly started,
False if a watcher with that key is already registered) so nothing
calling into these modules -- app_factory.py in particular -- needs to
change.

Low-priority jobs (backfill/refresh -- the ones that do bulk historical
I/O rather than time-sensitive alerting) can be registered with
`low_priority=True`. Those are skipped entirely while a scan is actively
running (see `scan_started()` / `scan_finished()`), so a backfill sweep
never competes with an interactive Scanner Builder query for SQLite I/O
or worker threads.
"""
from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

_MAX_WORKERS = 4
_TICK_SECONDS = 2

_lock = threading.RLock()
_jobs: Dict[str, Dict[str, Any]] = {}
_executor: Optional[ThreadPoolExecutor] = None
_dispatcher_thread: Optional[threading.Thread] = None
_dispatcher_started = False
_scan_active_count = 0
_registration_count = 0

# How many seconds apart consecutive newly-registered jobs are allowed to
# become due for the first time after a fresh process start. Without
# this, every job's in-memory "last ran" clock resets to zero on every
# restart, so the very first dispatcher tick (2s after boot) sees ALL of
# them as overdue at once and fires the whole set simultaneously --
# exactly the moment someone who just restarted the app is most likely
# to be actively clicking around in it. Spreading first-runs out trades
# a few seconds of staggered startup for not starving interactive
# requests of worker threads and SQLite access right after boot.
_STARTUP_STAGGER_SECONDS = 4
# Uncapped, this grows every time a new scheduled job gets added to the
# app -- a job registered late in app_factory.py's startup sequence
# (this app now has several dozen registrations happening before some
# of the newer ones) could end up waiting minutes for its very first
# tick, looking exactly like "never run" from Scheduler Hub even though
# nothing is actually broken -- confirmed directly: a job's "Run now"
# button worked immediately (proving the function itself is fine) while
# its automatic schedule showed no last_run_at at all, minutes after
# registration. Capping the total stagger keeps startup spread-out
# without letting it become an ever-growing cold-start delay as more
# jobs accumulate over the app's history.
_MAX_STARTUP_STAGGER_SECONDS = 60


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="oiapp-sched-hub")
    return _executor


def register(
    key: str,
    fn: Callable[[], Any],
    interval_seconds: int,
    *,
    low_priority: bool = False,
    min_interval_seconds: int = 15,
) -> bool:
    """Register a job to be run on a schedule by the shared dispatcher
    thread. Returns True if this call registered the job, False if a job
    with this key was already registered (mirrors the old
    start_xxx_watcher() "already running" semantics).

    `fn` should be the same zero-arg "do one unit of work" callable each
    module already passes as job_registry's `run_now_fn` -- job_registry
    itself continues to own enabled/disabled state and schedule overrides
    (interval_min) so the existing Scheduler Hub UI page is unaffected.
    """
    with _lock:
        if key in _jobs:
            return False
        interval = max(min_interval_seconds, int(interval_seconds or min_interval_seconds))

        global _registration_count
        stagger_offset = min(_MAX_STARTUP_STAGGER_SECONDS, _registration_count * _STARTUP_STAGGER_SECONDS)
        _registration_count += 1

        # Default: due only after `stagger_offset` seconds from now, not
        # immediately -- see _STARTUP_STAGGER_SECONDS above.
        initial_last_run = time.time() - interval + stagger_offset
        # But if this job genuinely ran recently before the process
        # restarted (persisted in job_registry, unlike this in-memory
        # value), honor that instead of artificially delaying it further.
        try:
            from .job_registry import get_last_run_at
            persisted = get_last_run_at(key)
            if persisted is not None:
                initial_last_run = max(initial_last_run, persisted)
        except Exception:
            pass

        _jobs[key] = {
            "fn": fn,
            "interval_seconds": interval,
            "low_priority": low_priority,
            "last_run": initial_last_run,
            "running": False,
        }
    _ensure_dispatcher_started()
    return True


def _due_interval_seconds(key: str, job: Dict[str, Any]) -> int:
    """Prefer the live schedule from job_registry (interval_min, editable
    from the Scheduler Hub UI) over the interval captured at registration
    time, so changing a job's schedule in the UI takes effect without a
    restart -- same behavior the old per-job loops had."""
    try:
        from .job_registry import get_schedule
        sched = get_schedule(key)
        interval_min = sched.get("interval_min")
        if interval_min:
            return max(15, int(float(interval_min) * 60))
    except Exception:
        pass
    return job["interval_seconds"]


def _run_job(key: str, job: Dict[str, Any]) -> None:
    from .job_registry import mark_start, mark_finished, mark_run, log_run_start, log_run_finish
    mark_start(key)
    run_id = log_run_start(key)
    try:
        result = job["fn"]()
        mark_run(key, True, f"{result}" if result is not None else "ok")
        log_run_finish(run_id, True, f"{result}" if result is not None else "ok")
        _log_job_notification(key, True, result)
    except Exception as e:  # noqa: BLE001
        mark_run(key, False, str(e))
        log_run_finish(run_id, False, str(e))
        _log_job_notification(key, False, str(e))
        print(f"[unified_scheduler] job '{key}' failed: {e}")
        traceback.print_exc()
    finally:
        mark_finished(key)
        with _lock:
            job["running"] = False
            job["last_run"] = time.time()


def _log_job_notification(key: str, success: bool, result) -> None:
    """Persists every scheduled job's outcome to the same alert_notifications
    table the bell/notifications panel already reads (watchlist_manager.py's
    log_alert_notification) -- not the separate job_registry run-history
    used by Scheduler Hub, which is a different, less-visible page. This is
    the one place EVERY unified_scheduler-registered job actually executes,
    so hooking in here covers all of them (GEX Trend Tracker, Live Chain
    Tracker, the tastytrade backfill, etc.) without touching each job
    function individually.

    Failures always get logged -- that's the whole point. Successes are
    filtered to skip "ran, found nothing to do" ticks (e.g. an empty
    backfill queue) so a job ticking every 90s doesn't flood the panel with
    repetitive empty-result entries forever; anything with real numbers in
    it (a dict with a nonzero value, a non-empty list, or any other
    non-trivial result) still gets logged.
    """
    try:
        from ..scanners.watchlist_manager import log_alert_notification
    except Exception:
        return  # logging is enrichment, never allowed to break the job it's describing

    if success:
        is_boring = (
            result is None or result == "" or result == "ok" or
            (isinstance(result, dict) and not any(
                (isinstance(v, (int, float)) and v) or (isinstance(v, (list, dict)) and len(v))
                for v in result.values()
            )) or
            (isinstance(result, (list, dict)) and len(result) == 0)
        )
        if is_boring:
            return

    try:
        from .job_registry import _registry
        label = _registry.get(key, {}).get("label", key)
    except Exception:
        label = key
    detail = str(result) if result is not None else ""
    try:
        log_alert_notification(
            "SCHEDULER_RUN", f"{label}: {'completed' if success else 'failed'}",
            detail, source="scheduler", severity="info" if success else "error",
            scanner_name=key,
        )
    except Exception as e:
        print(f"[unified_scheduler] failed to log notification for '{key}': {e}")


def _dispatcher_loop() -> None:
    from .job_registry import is_enabled
    while True:
        try:
            now = time.time()
            with _lock:
                due_keys = []
                for key, job in _jobs.items():
                    if job["running"]:
                        continue
                    if job["low_priority"] and _scan_active_count > 0:
                        continue  # yield the whole tick to the active scan
                    interval = _due_interval_seconds(key, job)
                    if now - job["last_run"] >= interval:
                        due_keys.append(key)
                for key in due_keys:
                    try:
                        if not is_enabled(key):
                            _jobs[key]["last_run"] = now  # don't hot-loop re-checking a disabled job
                            continue
                    except Exception:
                        pass
                    _jobs[key]["running"] = True
                    _get_executor().submit(_run_job, key, _jobs[key])
        except Exception as e:  # noqa: BLE001
            print(f"[unified_scheduler] dispatcher tick error: {e}")
        time.sleep(_TICK_SECONDS)


def _ensure_dispatcher_started() -> None:
    global _dispatcher_started, _dispatcher_thread
    with _lock:
        if _dispatcher_started:
            return
        _dispatcher_started = True
        _dispatcher_thread = threading.Thread(
            target=_dispatcher_loop, name="oiapp-scheduler-hub", daemon=True
        )
        _dispatcher_thread.start()
        print("[unified_scheduler] dispatcher thread started (replaces per-job watcher threads)")


def scan_started() -> None:
    """Call when an interactive scan begins so low_priority jobs (backfill
    / refresh sweeps) yield SQLite + worker-thread capacity to it."""
    global _scan_active_count
    with _lock:
        _scan_active_count += 1


def scan_finished() -> None:
    global _scan_active_count
    with _lock:
        _scan_active_count = max(0, _scan_active_count - 1)


def status() -> Dict[str, Any]:
    with _lock:
        return {
            "dispatcher_started": _dispatcher_started,
            "scan_active": _scan_active_count > 0,
            "jobs": {
                k: {
                    "interval_seconds": v["interval_seconds"],
                    "low_priority": v["low_priority"],
                    "running": v["running"],
                    "last_run": v["last_run"],
                }
                for k, v in _jobs.items()
            },
        }
