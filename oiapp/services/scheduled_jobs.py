
"""Daily scheduled jobs for watchlist fetches and GEX snapshots."""
from __future__ import annotations

import datetime as _dt
import threading
import time
from typing import Callable, Dict, Iterable, Optional, Sequence, Tuple

_started = False
_lock = threading.Lock()
_watchlist_sched_started = False
_watchlist_sched_lock = threading.Lock()

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


def _run_morning_data_pipeline(app) -> None:
    """The ONE 7:30 AM job that replaces everything that used to run
    continuously in the background all day (price backfill/refresh
    watchers, intraday backfill/refresh watchers, technical snapshot
    watcher) PLUS a separate, hardcoded, undocumented duplicate 7:00 AM
    thread that used to live in app_factory.py (which redundantly ran
    the regime scan and futures OI fetch a SECOND time on top of what
    this same pipeline already does -- removed entirely, not folded in
    twice).

    Runs every step in explicit sequence, not in parallel, exactly as
    requested: each step only makes sense once the previous one has
    supplied fresh data (technical indicators need current price data;
    regime detection needs current price data). A failure in one step is
    caught and logged so it doesn't block the rest of the sequence.

    Historical backfill (initial multi-year daily history, initial ~2yr
    intraday history) is intentionally NOT part of this pipeline -- that
    stays a one-time, manually-triggered action (Scheduler Hub's "Run
    Now" on scanner_price_backfill / scanner_intraday_backfill) for
    newly-added symbols, exactly as requested. Everything below is
    incremental: gap-aware price refresh (fetches only what's missing
    since each symbol's last cached bar) and same-day technical
    indicator computation (5 rows per symbol per day: 1d, 1w, 1h, 2h,
    4h), not a re-fetch or re-compute of history that's already there.
    """
    import datetime as _dt2

    with app.app_context():
        # Step 1: Futures OI (three-layer: Schwab -> CME fallback -> CFTC
        # COT overlay), all contracts in parallel via the shared task pool.
        try:
            from .futures_oi_schwab import fetch_futures_oi_three_layer, SCHWAB_ROOTS
            from .task_executor import get_background_executor
            def _fetch_one(_sym):
                return _sym, fetch_futures_oi_three_layer(_sym, use_cme_fallback=True, include_cot=True)
            results = list(get_background_executor().map(_fetch_one, list(SCHWAB_ROOTS.keys())))
            stored = sum(int((_r.get('stored', 0) or 0)) or sum(1 for c in (_r.get('contracts') or []) if c.get('stored'))
                         for _sym, _r in results)
            print(f"[morning_pipeline] 1/6 futures OI: {stored} rows stored")
        except Exception as e:
            print(f"[morning_pipeline] 1/6 futures OI FAILED: {e}")

        # Step 2: Watchlist price/volume/options-OI sweep (per-watchlist
        # config decides which watchlists also fetch options OI).
        try:
            from .scheduler import run_all_watchlists_once
            wl_result = run_all_watchlists_once(source='morning-pipeline')
            print(f"[morning_pipeline] 2/6 watchlist sweep: {wl_result.get('count')} watchlist(s)")
        except Exception as e:
            print(f"[morning_pipeline] 2/6 watchlist sweep FAILED: {e}")

        # Step 3: Scanner Builder's own daily price cache -- gap-aware,
        # ALL already-backfilled symbols in one pass (not a rotating
        # batch -- there's no need to spread this across the day when it
        # only runs once).
        try:
            from ..scanners.scanner_builder import run_daily_price_refresh, get_all_cached_daily_symbols
            all_daily_symbols = get_all_cached_daily_symbols()
            daily_result = run_daily_price_refresh(max_symbols=max(1, len(all_daily_symbols)))
            print(f"[morning_pipeline] 3/6 daily price refresh: {daily_result}")
        except Exception as e:
            print(f"[morning_pipeline] 3/6 daily price refresh FAILED: {e}")

        # Step 4: Same idea for the intraday (1h base) cache.
        try:
            from ..scanners.scanner_builder import run_intraday_price_refresh, get_all_cached_intraday_symbols
            all_intraday_symbols = get_all_cached_intraday_symbols()
            intraday_result = run_intraday_price_refresh(max_symbols=max(1, len(all_intraday_symbols)))
            print(f"[morning_pipeline] 4/6 intraday price refresh: {intraday_result}")
        except Exception as e:
            print(f"[morning_pipeline] 4/6 intraday price refresh FAILED: {e}")

        # Step 5: Technical indicators for TODAY only, across all 5
        # timeframes (daily, weekly, 1h, 2h, 4h -- 5 rows per symbol per
        # day, exactly as requested). Cheap: reads the price data steps
        # 3-4 just refreshed (already in memory/cache), doesn't refetch
        # anything.
        try:
            from ..services.technical_snapshot import run_technical_snapshot_batch
            from ..scanners.scanner_builder import get_all_cached_daily_symbols
            symbols_for_ta = get_all_cached_daily_symbols()
            ta_result = run_technical_snapshot_batch(symbols_for_ta, timeframes=["1d", "1w", "1h", "2h", "4h"])
            print(f"[morning_pipeline] 5/6 technical indicators: {ta_result}")
        except Exception as e:
            print(f"[morning_pipeline] 5/6 technical indicators FAILED: {e}")

        # Step 6: Regime scan -- needs the fresh price data from steps 3-4.
        try:
            from ..scanners.regime_scanner import run_regime_scan
            run_regime_scan(max_workers=20)
            print(f"[morning_pipeline] 6/6 regime scan done")
        except Exception as e:
            print(f"[morning_pipeline] 6/6 regime scan FAILED: {e}")

        # Step 7 (V104): 0-10 DTE positional OI trend (SPY/QQQ/SPX/IWM).
        # Deliberately runs here, after options-chain/OI data for the day
        # has already settled via steps 1-2, and not any earlier -- real
        # OI is only final once a day, so computing this before the rest
        # of the pipeline has run would risk reading yesterday's chain.
        # Also computes an initial intraday-buildup snapshot for the day
        # so the Intraday page has something to show before its own
        # 20-min interval job (Scheduler Hub) ticks for the first time.
        try:
            from .dte_pages import run_full_refresh
            dte_result = run_full_refresh()
            print(f"[morning_pipeline] 7/7 DTE positional trend: {dte_result}")
        except Exception as e:
            print(f"[morning_pipeline] 7/7 DTE positional trend FAILED: {e}")

        # Weekly/day-specific extras, preserved from the old duplicate
        # 7AM thread (removed from app_factory.py -- this pipeline is now
        # the only place these run, instead of twice).
        try:
            if _dt2.datetime.now().weekday() == 1:  # Tuesday -- CFTC releases weekly
                from ..services.cftc_cot import fetch_cot_data
                cot_result = fetch_cot_data(years=2, force=False)
                print(f"[morning_pipeline] CFTC COT (Tuesday): {cot_result.get('message', '')}")
        except Exception as e:
            print(f"[morning_pipeline] CFTC COT FAILED: {e}")
        try:
            if _dt2.datetime.now().weekday() == 0:  # Monday
                from ..services.sector_service import refresh_all_sectors
                refresh_all_sectors()
                print(f"[morning_pipeline] sector refresh (Monday): done")
        except Exception as e:
            print(f"[morning_pipeline] sector refresh FAILED: {e}")

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

def _run_weekly_plan_grading(app) -> None:
    from .weekly_plan_backtest import grade_pending_live_snapshots
    with app.app_context():
        result = grade_pending_live_snapshots(symbol="SPY")
        print(f"[scheduled_jobs] weekly_plan_grading: {result}")


def _scheduler_loop(app) -> None:
    from .job_registry import register_job, is_enabled, get_schedule, mark_run, mark_start, mark_finished, log_run_start, log_run_finish

    job_defs: Tuple[Tuple[str, str, str, Callable[[], None], Dict, Optional[Sequence[int]]], ...] = (
        ('morning_data_pipeline', 'Morning data pipeline',
         'Runs in order: futures OI -> watchlist price/volume/options OI -> '
         'scanner daily price refresh -> scanner intraday price refresh -> '
         'technical indicators (1d/1w/1h/2h/4h) for today -> regime scan. '
         'The ONE place all of this runs -- replaces the old continuous '
         'all-day background watchers for price/indicator refresh.',
         lambda: _run_morning_data_pipeline(app), {"times": ["07:30"], "weekdays": None}, None),
        ('oib_snapshot', 'OI Buildup snapshot', 'Runs the OI buildup screener and saves a snapshot.',
         lambda: _run_oib_snapshot(app), {"times": ["08:00"], "weekdays": None}, None),
        ('gex_snapshot_pre', 'GEX plan (premarket)', 'Saves an SPY daily GEX/plan snapshot before the open.',
         lambda: _run_gex_snapshot(app, 'SPY', '08:45 premarket'), {"times": ["08:45"], "weekdays": None}, None),
        ('weekly_plan_snapshot', 'Weekly plan snapshot', 'Saves the SPY weekly options plan (Mondays only).',
         lambda: _run_weekly_plan_snapshot(app), {"times": ["10:00"], "weekdays": [0]}, (0,)),
        ('weekly_plan_grading', 'Weekly plan grading (backtest)',
         'Grades prior Monday weekly-plan snapshots whose Friday expiry has passed against the realized close -- '
         'builds the real (non-approximated) track record. Fridays after close.',
         lambda: _run_weekly_plan_grading(app), {"times": ["16:15"], "weekdays": [4]}, (4,)),
        ('gex_snapshot_open', 'GEX plan (after open)', 'Saves an SPY daily GEX/plan snapshot shortly after the open.',
         lambda: _run_gex_snapshot(app, 'SPY', '10:15 after-open'), {"times": ["10:15"], "weekdays": None}, None),
    )
    for key, label, desc, fn, default_sched, _wd in job_defs:
        register_job(key, label, desc, kind="time", default_schedule=default_sched,
                     group="Daily Snapshots (Watchlist/GEX/OI)", run_now_fn=fn)

    while True:
        try:
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
            run_id = None
            try:
                mark_start(next_key)
                run_id = log_run_start(next_key)
                next_item()
                print(f"[scheduled_jobs] ran {next_key} at {_dt.datetime.now():%Y-%m-%d %H:%M:%S}")
                mark_run(next_key, True, f"Ran on schedule at {_dt.datetime.now():%Y-%m-%d %H:%M}")
                if run_id:
                    log_run_finish(run_id, True, f"Ran on schedule at {_dt.datetime.now():%Y-%m-%d %H:%M}")
            except Exception as e:
                print(f"[scheduled_jobs] {next_key} error: {e}")
                mark_run(next_key, False, str(e))
                if run_id:
                    log_run_finish(run_id, False, str(e))
            finally:
                try:
                    mark_finished(next_key)
                except Exception as e:
                    print(f"[scheduled_jobs] mark_finished error for {next_key}: {e}")
            time.sleep(1)
        except Exception as e:
            # This is the actual fix: previously, ANY unexpected exception
            # in this loop's own bookkeeping (computing what's next,
            # is_enabled()/get_schedule() hitting a transient DB issue,
            # _sleep_until, even a second exception inside the finally
            # block above) was completely uncaught here -- it would kill
            # this daemon thread outright, silently, with nothing printed
            # and nothing to restart it. Every scheduled job (including
            # the 7:30 AM morning pipeline) would then simply never fire
            # again for the rest of the process's life, with zero
            # indication anything was wrong until someone noticed data
            # wasn't refreshing. This outer guard means the absolute
            # worst case is now "logs an error and retries in 30s"
            # instead of "silently dead until the next restart."
            import traceback
            print(f"[scheduled_jobs] UNEXPECTED scheduler loop error (thread would have died here before this fix): {e}")
            traceback.print_exc()
            time.sleep(30)

def _set_watchlist_schedule_date(wl_id: int, column: str, date_str: str) -> None:
    """Marks a watchlist's schedule step as having run today, regardless
    of whether it succeeded or failed -- a failure still counts as
    "attempted today" so a broken fetch doesn't retry every single
    minute for the rest of the day; it gets one attempt per day, same
    as every other scheduled job in this file."""
    try:
        import sqlite3
        from ..config import DB_PATH
        con = sqlite3.connect(DB_PATH)
        con.execute(f"UPDATE watchlists SET {column}=? WHERE id=?", (date_str, wl_id))
        con.commit()
        con.close()
    except Exception as e:
        print(f"[watchlist_schedule] failed to record {column} for watchlist {wl_id}: {e}")


def _notify_job(alert_type: str, title: str, detail: str, severity: str = "ok") -> None:
    """Shared wrapper around log_alert_notification() (the same
    backend-persisted alert store the bell/notification panel already
    polls -- confirmed by reading its real implementation and the
    frontend's _loadAlertHistory() before reusing it, not a new
    notification system built from scratch) so each of the 4 scheduled
    job types below doesn't repeat the same import-and-try/except
    boilerplate. Never lets a notification-write failure affect the
    job itself -- this is purely informational plumbing.
    """
    try:
        from ..scanners.watchlist_manager import log_alert_notification
        log_alert_notification(alert_type, title, detail, severity=severity, source="Watchlist Schedule")
    except Exception as e:
        print(f"[watchlist_schedule] notification write failed: {e}")


def _process_watchlist_schedule_row(row: tuple, today: str, hh_mm: str) -> None:
    """One watchlist's worth of the sequence: price/OI -> earnings ->
    indicators -> corporate events -> volume profile. Each step only
    executes if it HAS a configured time (blank/None means "don't run this
    step" -- opt-in per watchlist, per step) AND that time has passed today
    AND it hasn't already run today. All steps after price/OI additionally
    require price/OI to have ALREADY completed today -- a real dependency
    gate, not just "set the clock times in the right order" advice: if
    price/OI's time is misconfigured to be LATER than a later step's, that
    step doesn't error out or skip permanently for the day -- it simply
    keeps re-checking every loop tick (its own last-run date is
    deliberately left unset on a dependency-skip) until price/OI actually
    completes, then cascades immediately within that same tick.
    """
    (wl_id, wl_name, t_price, t_earn, t_ind, t_events, t_vp,
     d_price, d_earn, d_ind, d_events, d_vp) = row

    price_ran_today = (d_price == today)

    if t_price and not price_ran_today and hh_mm >= t_price:
        try:
            from ..scanners.watchlist_manager import _fetch_data_for_watchlist_core
            status, payload = _fetch_data_for_watchlist_core(wl_id, source="scheduler", blocking=True)
            print(f"[watchlist_schedule] price/OI for '{wl_name}': {payload}")
            _notify_job("WATCHLIST_PRICE_OI", f"Price/OI fetch finished: \"{wl_name}\"",
                        f"{payload.get('symbols', '?')} symbol(s), mode={payload.get('mode', '?')}",
                        severity="ok" if payload.get("ok") else "warn")
        except Exception as e:
            print(f"[watchlist_schedule] price/OI for '{wl_name}' FAILED: {e}")
            _notify_job("WATCHLIST_PRICE_OI", f"Price/OI fetch FAILED: \"{wl_name}\"", str(e), severity="error")
        _set_watchlist_schedule_date(wl_id, "schedule_price_oi_last_date", today)
        price_ran_today = True  # ran (success or failure) -- don't retry again today, and
                                 # unblocks earnings/indicators immediately within this same tick

    if t_earn and d_earn != today and hh_mm >= t_earn:
        if not price_ran_today:
            print(f"[watchlist_schedule] earnings for '{wl_name}': waiting on price/OI to complete first")
        else:
            try:
                # Matches the manual "📅 Earnings" button's full behavior
                # (checked its actual route before assuming) -- that
                # button calls BOTH refresh_calendar() (next-earnings-date
                # data) AND bulk_fetch_fundamentals() (PE/growth/margin/
                # analyst consensus), not fundamentals alone. Both are
                # plain synchronous functions (confirmed via their real
                # signatures), so both are called directly and awaited
                # here rather than going through the button's own
                # async-with-polling wrapper, which exists only to give a
                # web request a fast response -- irrelevant for a
                # scheduler that's already background and can just wait.
                from ..scanners.earnings import bulk_fetch_fundamentals, _get_watchlist_symbols
                from ..scanners.earnings_calendar import refresh_calendar
                syms = _get_watchlist_symbols(wl_id) or []
                if syms:
                    cal_updated, cal_skipped, cal_failed = refresh_calendar(syms, force=False)
                    fund_result = bulk_fetch_fundamentals(syms, force=False)
                    print(f"[watchlist_schedule] earnings for '{wl_name}': "
                          f"calendar updated={cal_updated} skipped={cal_skipped} failed={cal_failed}, "
                          f"fundamentals={fund_result}")
                    _notify_job("WATCHLIST_EARNINGS", f"Earnings fetch finished: \"{wl_name}\"",
                                f"Calendar: {cal_updated} updated / {cal_skipped} skipped / {cal_failed} failed. "
                                f"Fundamentals: {fund_result}",
                                severity="ok" if cal_failed == 0 else "warn")
                else:
                    print(f"[watchlist_schedule] earnings for '{wl_name}': no symbols, skipped")
            except Exception as e:
                print(f"[watchlist_schedule] earnings for '{wl_name}' FAILED: {e}")
                _notify_job("WATCHLIST_EARNINGS", f"Earnings fetch FAILED: \"{wl_name}\"", str(e), severity="error")
            _set_watchlist_schedule_date(wl_id, "schedule_earnings_last_date", today)

    if t_ind and d_ind != today and hh_mm >= t_ind:
        if not price_ran_today:
            print(f"[watchlist_schedule] indicators for '{wl_name}': waiting on price/OI to complete first")
        else:
            try:
                from .technical_snapshot import run_technical_snapshot_batch
                from ..scanners.earnings import _get_watchlist_symbols
                syms = _get_watchlist_symbols(wl_id) or []
                if syms:
                    result = run_technical_snapshot_batch(syms, timeframes=["1d", "1w", "1h", "2h", "4h"])
                    print(f"[watchlist_schedule] indicators for '{wl_name}': {result}")
                    _notify_job("WATCHLIST_INDICATORS", f"Indicators computed: \"{wl_name}\"",
                                f"{len(syms)} symbol(s), {result}", severity="ok")
                else:
                    print(f"[watchlist_schedule] indicators for '{wl_name}': no symbols, skipped")
            except Exception as e:
                print(f"[watchlist_schedule] indicators for '{wl_name}' FAILED: {e}")
                _notify_job("WATCHLIST_INDICATORS", f"Indicators computation FAILED: \"{wl_name}\"", str(e), severity="error")
            _set_watchlist_schedule_date(wl_id, "schedule_indicators_last_date", today)

    if t_events and d_events != today and hh_mm >= t_events:
        if not price_ran_today:
            print(f"[watchlist_schedule] corporate events for '{wl_name}': waiting on price/OI to complete first")
        else:
            try:
                # Shared with the manual "Corp Events" button (see
                # run_corporate_events_for_symbols in watchlist_manager.py)
                # -- exactly one implementation of this fetch, so the
                # scheduled and manual paths can never silently disagree.
                from ..scanners.watchlist_manager import run_corporate_events_for_symbols, log_alert_notification
                from ..scanners.earnings import _get_watchlist_symbols
                syms = _get_watchlist_symbols(wl_id) or []
                if syms:
                    fetched, skipped_no_cik, errors, material_insider_events = run_corporate_events_for_symbols(syms, progress=False)
                    print(f"[watchlist_schedule] corporate events for '{wl_name}': "
                          f"{fetched} fetched, {skipped_no_cik} skipped (no CIK), {errors} errors")
                    _notify_job("WATCHLIST_CORPORATE_EVENTS", f"Corporate events fetch finished: \"{wl_name}\"",
                                f"{fetched} symbol(s) updated, {skipped_no_cik} skipped (no SEC filer match), {errors} error(s)",
                                severity="ok" if errors == 0 else "warn")
                    for sym, net in material_insider_events:
                        direction = "buying" if net > 0 else "selling"
                        try:
                            log_alert_notification(
                                "INSIDER_ACTIVITY", f"{sym}: large net insider {direction}",
                                f"Net ${abs(net):,.0f} over the last 90 days",
                                symbol=sym, severity="ok" if net > 0 else "warn", source="Corporate Events",
                            )
                        except Exception:
                            pass
                else:
                    print(f"[watchlist_schedule] corporate events for '{wl_name}': no symbols, skipped")
            except Exception as e:
                print(f"[watchlist_schedule] corporate events for '{wl_name}' FAILED: {e}")
                _notify_job("WATCHLIST_CORPORATE_EVENTS", f"Corporate events fetch FAILED: \"{wl_name}\"", str(e), severity="error")
            _set_watchlist_schedule_date(wl_id, "schedule_corporate_events_last_date", today)

    if t_vp and d_vp != today and hh_mm >= t_vp:
        if not price_ran_today:
            print(f"[watchlist_schedule] volume profile for '{wl_name}': waiting on price/OI to complete first")
        else:
            try:
                # Precomputes and caches today's balance/imbalance scan
                # (POC/VAH/VAL, Trend/Reversion classification, score) per
                # symbol -- this is the actual mechanism behind "faster
                # scanner execution": the live Volume Profile Scanner page
                # reads this cache instead of recomputing on every visit.
                from ..scanners.volume_profile_scanner import precompute_watchlist
                from ..scanners.earnings import _get_watchlist_symbols
                syms = _get_watchlist_symbols(wl_id) or []
                if syms:
                    result = precompute_watchlist(syms, progress=False)
                    print(f"[watchlist_schedule] volume profile for '{wl_name}': {result}")
                    _notify_job("WATCHLIST_VOLUME_PROFILE", f"Volume profile scan finished: \"{wl_name}\"",
                                f"{result.get('computed', 0)} symbol(s) computed, "
                                f"{result.get('errors', 0)} error(s), of {result.get('total', len(syms))} total",
                                severity="ok" if result.get("errors", 0) == 0 else "warn")
                else:
                    print(f"[watchlist_schedule] volume profile for '{wl_name}': no symbols, skipped")
            except Exception as e:
                print(f"[watchlist_schedule] volume profile for '{wl_name}' FAILED: {e}")
                _notify_job("WATCHLIST_VOLUME_PROFILE", f"Volume profile scan FAILED: \"{wl_name}\"", str(e), severity="error")
            _set_watchlist_schedule_date(wl_id, "schedule_volume_profile_last_date", today)


def _run_future_oi_if_due(today: str, hh_mm: str) -> None:
    """Global (not per-watchlist) futures OI schedule time, stored as a
    single app_setting rather than a watchlists column since futures
    aren't tied to any one watchlist. Same "blank = don't run, one
    attempt per day" rules as the per-watchlist steps above."""
    try:
        from ..scanners.watchlist_manager import _get_setting, _set_setting
        sched_time = (_get_setting("future_oi_schedule_time", "") or "").strip()
        last_date = _get_setting("future_oi_schedule_last_date", "")
        if not sched_time or last_date == today or hh_mm < sched_time:
            return
        from .futures_oi_schwab import fetch_futures_oi_three_layer, SCHWAB_ROOTS
        from .task_executor import get_background_executor

        def _fetch_one(_sym):
            return _sym, fetch_futures_oi_three_layer(_sym, use_cme_fallback=True, include_cot=True)
        results = list(get_background_executor().map(_fetch_one, list(SCHWAB_ROOTS.keys())))
        stored = sum(int((_r.get('stored', 0) or 0)) or sum(1 for c in (_r.get('contracts') or []) if c.get('stored'))
                     for _sym, _r in results)
        print(f"[watchlist_schedule] futures OI: {stored} rows stored")
        _notify_job("FUTURES_OI", "Futures OI fetch finished",
                    f"{stored} row(s) stored across {len(SCHWAB_ROOTS)} root(s)", severity="ok")
        _set_setting("future_oi_schedule_last_date", today)
    except Exception as e:
        print(f"[watchlist_schedule] futures OI FAILED: {e}")
        _notify_job("FUTURES_OI", "Futures OI fetch FAILED", str(e), severity="error")


def _watchlist_schedule_loop(app) -> None:
    """Separate from _scheduler_loop above (the fixed few named daily
    jobs) because this one is fundamentally per-watchlist and dynamic --
    the set of "jobs" here isn't a small static list registered once at
    startup, it's however many watchlists currently exist, each with its
    own 3 independently-configurable times. Ticks once a minute (fine
    granularity doesn't matter here the way it would for a sub-minute
    job -- these are daily, HH:MM-resolution schedules), and, like
    _scheduler_loop, has its own outer guard so an unexpected exception
    in the bookkeeping can't silently kill the whole thread for the rest
    of the process's life.
    """
    while True:
        try:
            with app.app_context():
                now = _dt.datetime.now()
                today = now.date().isoformat()
                hh_mm = now.strftime("%H:%M")

                _run_future_oi_if_due(today, hh_mm)

                import sqlite3
                from ..config import DB_PATH
                con = sqlite3.connect(DB_PATH)
                con.row_factory = None
                try:
                    rows = con.execute("""
                        SELECT id, name, schedule_price_oi_time, schedule_earnings_time, schedule_indicators_time,
                               schedule_corporate_events_time, schedule_volume_profile_time,
                               schedule_price_oi_last_date, schedule_earnings_last_date, schedule_indicators_last_date,
                               schedule_corporate_events_last_date, schedule_volume_profile_last_date
                        FROM watchlists
                    """).fetchall()
                finally:
                    con.close()

                for row in rows:
                    try:
                        _process_watchlist_schedule_row(row, today, hh_mm)
                    except Exception as e:
                        print(f"[watchlist_schedule] error processing watchlist row {row}: {e}")
        except Exception as e:
            import traceback
            print(f"[watchlist_schedule] UNEXPECTED loop error: {e}")
            traceback.print_exc()
        time.sleep(60)


def start_watchlist_schedule(app) -> bool:
    global _watchlist_sched_started
    with _watchlist_sched_lock:
        if _watchlist_sched_started:
            return False
        t = threading.Thread(target=_watchlist_schedule_loop, args=(app,), daemon=True)
        t.start()
        _watchlist_sched_started = True
        return True


def start_daily_jobs(app) -> bool:
    global _started
    with _lock:
        if _started:
            return False
        t = threading.Thread(target=_scheduler_loop, args=(app,), daemon=True)
        t.start()
        _started = True
        return True
