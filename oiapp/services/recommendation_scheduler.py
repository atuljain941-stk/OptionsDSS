"""Background scheduler for saving AI recommendation history.

Runs the OI buildup scan daily at 08:00 and the Weekly Plan scan every Monday
at 10:00, then stores tradeable+ outcomes in recommendation_history so the AI
can fine-tune against past runs.
"""
from __future__ import annotations

import datetime as _dt
import threading
import time
from typing import Any, Dict, List, Sequence, Tuple

_started = False
_lock = threading.Lock()


def _next_daily(now: _dt.datetime, hour: int, minute: int) -> _dt.datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += _dt.timedelta(days=1)
    return target


def _next_weekly(now: _dt.datetime, weekday: int, hour: int, minute: int) -> _dt.datetime:
    # weekday: Monday=0 ... Sunday=6
    days_ahead = (weekday - now.weekday()) % 7
    target = now + _dt.timedelta(days=days_ahead)
    target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += _dt.timedelta(days=7)
    return target


def _sleep_until(target: _dt.datetime) -> None:
    delay = max(0.5, (target - _dt.datetime.now()).total_seconds())
    time.sleep(delay)


def _save_oib_history(app, scan: Dict[str, Any], source: str = "scheduled-0800") -> Dict[str, Any]:
    from .recommendation_history import save_run
    with app.app_context():
        rows = scan.get("results") or []
        label = f"OI Buildup {scan.get('date') or _dt.date.today().isoformat()}"
        summary = {
            "symbol": scan.get("symbol") or "",
            "count": scan.get("count") or len(rows),
            "completed_at": scan.get("completed_at") or scan.get("date") or "",
            "st_days": scan.get("st_days"),
            "mt_days": scan.get("mt_days"),
            "lt_days": scan.get("lt_days"),
            "source": source,
            "status": "saved",
        }
        return save_run(source=source, run_kind="oi_buildup", rows=rows, summary=summary, settings={
            "st_days": scan.get("st_days"),
            "mt_days": scan.get("mt_days"),
            "lt_days": scan.get("lt_days"),
        }, label=label, payload=scan)


def _run_oib_scan(app) -> Dict[str, Any]:
    from ..scanners.oi_buildup_scanner import run_oi_buildup_screener
    with app.app_context():
        scan = run_oi_buildup_screener(st_days=3, mt_days=10, lt_days=30, watchlist_id=None, max_symbols=None, save_cache=True, warm_watchlist=True)
        if scan.get("ok"):
            saved = _save_oib_history(app, scan, source="scheduled-0800")
            scan["history_saved"] = saved
        return scan


def _run_weekly_plan(app) -> Dict[str, Any]:
    from ..scanners.spy_strategies import api_weekly
    from .recommendation_history import save_run
    symbols = ["SPY", "QQQ", "IWM", "DIA"]
    out: List[Dict[str, Any]] = []
    saved_total = 0
    with app.app_context():
        for sym in symbols:
            try:
                with app.test_request_context(f"/spy/weekly?symbol={sym}"):
                    resp = api_weekly()
                    payload = resp.get_json() if hasattr(resp, "get_json") else None
                if not payload or payload.get("error"):
                    out.append({"symbol": sym, "ok": False, "error": (payload or {}).get("error") if payload else "unknown"})
                    continue
                strategies = payload.get("strategies") or []
                saved = save_run(
                    source="monday-1000",
                    run_kind="weekly_plan",
                    rows=strategies,
                    summary={
                        "symbol": sym,
                        "expiry": payload.get("expiry"),
                        "score": payload.get("score"),
                        "confidence": payload.get("confidence"),
                        "bias": payload.get("bias"),
                        "status": "saved",
                    },
                    settings={"symbol": sym},
                    label=f"Weekly Plan {sym} {payload.get('expiry') or ''}".strip(),
                    payload=payload,
                )
                payload["history_saved"] = saved
                saved_total += int(saved.get("saved_count") or 0)
                out.append({"symbol": sym, "ok": True, "saved": saved, "expiry": payload.get("expiry"), "score": payload.get("score")})
            except Exception as exc:
                out.append({"symbol": sym, "ok": False, "error": str(exc)[:220]})
    return {"ok": True, "runs": out, "saved_count": saved_total}


def _scheduler_loop(app) -> None:
    from .job_registry import register_job, is_enabled, get_schedule, mark_run

    job_defs: Sequence[Dict[str, Any]] = (
        {"key": "rec_oi_buildup_0800", "label": "Recommendation history: OI Buildup",
         "description": "Runs OI Buildup and saves outcomes to recommendation_history for AI fine-tuning.",
         "weekday": None, "hour": 8, "minute": 0, "func": lambda: _run_oib_scan(app)},
        {"key": "rec_weekly_plan_monday_1000", "label": "Recommendation history: Weekly Plan",
         "description": "Runs the Weekly Plan scan (Mondays) and saves outcomes to recommendation_history.",
         "weekday": 0, "hour": 10, "minute": 0, "func": lambda: _run_weekly_plan(app)},
    )
    for j in job_defs:
        register_job(
            j["key"], j["label"], j["description"], kind="time",
            default_schedule={"times": [f"{j['hour']:02d}:{j['minute']:02d}"],
                               "weekdays": [j["weekday"]] if j["weekday"] is not None else None},
            group="Recommendation History (AI fine-tune data)", run_now_fn=j["func"],
        )
        j.setdefault("note", "This overlaps in time with similarly-named jobs under 'Daily Snapshots' — "
                              "they save to different tables (recommendation_history vs saved_scanner_runs), "
                              "but if you don't use the AI recommendation-history feature, disabling this "
                              "group cuts background load without losing the daily GEX/OI snapshots.")

    while True:
        now = _dt.datetime.now()
        next_job = None
        next_time = None
        for job in job_defs:
            if not is_enabled(job["key"]):
                continue
            live = get_schedule(job["key"])
            times = live.get("times") or [f"{job['hour']:02d}:{job['minute']:02d}"]
            weekdays = live.get("weekdays", [job["weekday"]] if job["weekday"] is not None else None)
            for t_str in times:
                try:
                    hh, mm = [int(x) for x in str(t_str).split(":")[:2]]
                except Exception:
                    continue
                run_at = (_next_daily(now, hh, mm) if not weekdays else _next_weekly(now, int(weekdays[0]), hh, mm))
                if next_time is None or run_at < next_time:
                    next_time = run_at
                    next_job = job
        if not next_job or not next_time:
            time.sleep(60)
            continue
        _sleep_until(next_time)
        if not is_enabled(next_job["key"]):
            time.sleep(1)
            continue
        try:
            result = next_job["func"]()
            print(f"[recommendation_scheduler] ran {next_job['key']} at {_dt.datetime.now():%Y-%m-%d %H:%M:%S} -> {result.get('ok', True)}")
            mark_run(next_job["key"], True, f"saved_count={result.get('saved_count')}")
        except Exception as e:
            print(f"[recommendation_scheduler] {next_job['key']} error: {e}")
            mark_run(next_job["key"], False, str(e))
        time.sleep(1)


def start_recommendation_scheduler(app) -> bool:
    global _started
    with _lock:
        if _started:
            return False
        t = threading.Thread(target=_scheduler_loop, args=(app,), daemon=True, name="recommendation-scheduler")
        t.start()
        _started = True
        return True
