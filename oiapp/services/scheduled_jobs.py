
"""Daily scheduled jobs for watchlist fetches and GEX snapshots."""
from __future__ import annotations

import datetime as _dt
import threading
import time
from typing import Callable, Dict, Iterable, Optional, Sequence, Tuple

_started = False
_lock = threading.Lock()

def _next_run(now: _dt.datetime, hour: int, minute: int, weekdays: Optional[Sequence[int]] = None) -> _dt.datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    weekdays_set = set(int(d) for d in weekdays) if weekdays else None
    while True:
        if target > now and (weekdays_set is None or target.weekday() in weekdays_set):
            return target
        target += _dt.timedelta(days=1)

def _sleep_until(target: _dt.datetime) -> None:
    delay = max(0.5, (target - _dt.datetime.now()).total_seconds())
    time.sleep(delay)

def _run_watchlist_sweep(app) -> None:
    from .scheduler import run_all_watchlists_once
    with app.app_context():
        run_all_watchlists_once(source='auto-730am')

def _run_gex_snapshot(app, symbol: str, label: str) -> None:
    from ..scanners.spy_strategies import api_daily_plan, _save_daily_plan_snapshot
    with app.app_context():
        with app.test_request_context(f'/spy/daily_plan?symbol={symbol}'):
            resp = api_daily_plan()
            payload = resp.get_json() if hasattr(resp, 'get_json') else None
            if payload and not payload.get('error'):
                _save_daily_plan_snapshot(payload, label=label)


def _run_oib_snapshot(app) -> None:
    from ..scanners.oi_buildup_scanner import run_oi_buildup_screener
    from ..db import save_saved_scanner_run
    with app.app_context():
        payload = run_oi_buildup_screener(save_cache=True, warm_watchlist=False)
        if payload and payload.get('ok'):
            name = _dt.datetime.now().strftime('auto_oib_%Y%m%d_%H%M')
            save_saved_scanner_run('oi_buildup', name, payload, watchlist_id=payload.get('watchlist_id'), symbol=None, summary={
                'count': payload.get('count', 0),
                'date': payload.get('date'),
                'completed_at': payload.get('completed_at'),
                'st_days': payload.get('st_days'),
                'mt_days': payload.get('mt_days'),
                'lt_days': payload.get('lt_days'),
            }, note='scheduled 8am snapshot')


def _run_weekly_plan_snapshot(app) -> None:
    from ..scanners.spy_strategies import api_weekly
    from ..db import save_saved_scanner_run
    with app.app_context():
        with app.test_request_context('/spy/weekly?symbol=SPY'):
            resp = api_weekly()
            payload = resp.get_json() if hasattr(resp, 'get_json') else None
            if payload and not payload.get('error'):
                name = _dt.datetime.now().strftime('auto_weekly_%Y%m%d_%H%M')
                save_saved_scanner_run('weekly_plan', name, payload, watchlist_id=None, symbol=payload.get('symbol') or 'SPY', summary={
                    'symbol': payload.get('symbol'),
                    'expiry': payload.get('expiry'),
                    'dte': payload.get('dte'),
                    'spot': payload.get('spot'),
                    'bias': payload.get('bias'),
                    'confidence': payload.get('confidence'),
                    'plan_score': (payload.get('weekly_plan_score') or {}).get('composite_score') if isinstance(payload.get('weekly_plan_score'), dict) else None,
                }, note='scheduled monday snapshot')

def _scheduler_loop(app) -> None:
    from .job_registry import register_job, is_enabled, get_schedule, mark_run, mark_start, mark_finished

    job_defs: Tuple[Tuple[str, str, str, Callable[[], None], Dict, Optional[Sequence[int]]], ...] = (
        ('watchlist_sweep', 'Watchlist refresh', 'Refreshes all watchlists (price/volume data).',
         lambda: _run_watchlist_sweep(app), {"times": ["07:30"], "weekdays": None}, None),
        ('oib_snapshot', 'OI Buildup snapshot', 'Runs the OI buildup screener and saves a snapshot.',
         lambda: _run_oib_snapshot(app), {"times": ["08:00"], "weekdays": None}, None),
        ('gex_snapshot_pre', 'GEX plan (premarket)', 'Saves an SPY daily GEX/plan snapshot before the open.',
         lambda: _run_gex_snapshot(app, 'SPY', '08:45 premarket'), {"times": ["08:45"], "weekdays": None}, None),
        ('weekly_plan_snapshot', 'Weekly plan snapshot', 'Saves the SPY weekly options plan (Mondays only).',
         lambda: _run_weekly_plan_snapshot(app), {"times": ["10:00"], "weekdays": [0]}, (0,)),
        ('gex_snapshot_open', 'GEX plan (after open)', 'Saves an SPY daily GEX/plan snapshot shortly after the open.',
         lambda: _run_gex_snapshot(app, 'SPY', '10:15 after-open'), {"times": ["10:15"], "weekdays": None}, None),
    )
    for key, label, desc, fn, default_sched, _wd in job_defs:
        register_job(key, label, desc, kind="time", default_schedule=default_sched,
                     group="Daily Snapshots (Watchlist/GEX/OI)", run_now_fn=fn)

    while True:
        now = _dt.datetime.now()
        next_item = None
        next_time = None
        next_key = None
        for key, label, desc, fn, default_sched, _wd in job_defs:
            if not is_enabled(key):
                continue
            live_sched = get_schedule(key)
            times = live_sched.get("times") or default_sched.get("times") or []
            weekdays = live_sched.get("weekdays", default_sched.get("weekdays"))
            for t_str in times:
                try:
                    hh, mm = [int(x) for x in str(t_str).split(":")[:2]]
                except Exception:
                    continue
                run_at = _next_run(now, hh, mm, weekdays=weekdays)
                if next_time is None or run_at < next_time:
                    next_time = run_at
                    next_item = fn
                    next_key = key
        if next_time is None or next_item is None:
            time.sleep(60)
            continue
        _sleep_until(next_time)
        # Re-check enabled state right before running — it may have been
        # toggled off while we were sleeping until the scheduled time.
        if not is_enabled(next_key):
            time.sleep(1)
            continue
        try:
            mark_start(next_key)
            next_item()
            print(f"[scheduled_jobs] ran {next_key} at {_dt.datetime.now():%Y-%m-%d %H:%M:%S}")
            mark_run(next_key, True, f"Ran on schedule at {_dt.datetime.now():%Y-%m-%d %H:%M}")
        except Exception as e:
            print(f"[scheduled_jobs] {next_key} error: {e}")
            mark_run(next_key, False, str(e))
        finally:
            mark_finished(next_key)
        time.sleep(1)

def start_daily_jobs(app) -> bool:
    global _started
    with _lock:
        if _started:
            return False
        t = threading.Thread(target=_scheduler_loop, args=(app,), daemon=True)
        t.start()
        _started = True
        return True
