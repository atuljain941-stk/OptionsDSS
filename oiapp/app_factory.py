import os
from datetime import datetime
from flask import Flask, request, render_template, send_from_directory, jsonify

ROOT_DIR      = os.path.dirname(os.path.dirname(__file__))
TEMPLATES_DIR = os.path.join(ROOT_DIR, "templates")
STATIC_DIR    = os.path.join(ROOT_DIR, "oiapp", "static")


def _run_all_tasks(fast=False):
    """Run all scheduler tasks. fast=True skips regime scan (just futures+sectors)."""
    import datetime as _dt3
    try:
        from .scanners.regime_scanner import run_regime_scan
        # Increase parallelism for speed
        run_regime_scan(max_workers=20)
        print(f"✅ Regime scan done: {_dt3.datetime.now()}")
    except Exception as e:
        print(f"[scan] {e}")
    try:
        # Three-layer futures OI: Schwab first, CME fallback when mapped, CFTC COT overlay.
        # Do not store yfinance volume proxy as OI.
        from .services.futures_oi_schwab import fetch_futures_oi_three_layer, SCHWAB_ROOTS
        from .services.task_executor import get_background_executor
        def _fetch_one(_sym):
            return _sym, fetch_futures_oi_three_layer(_sym, use_cme_fallback=True, include_cot=True)
        ex = get_background_executor()
        results = list(ex.map(_fetch_one, list(SCHWAB_ROOTS.keys())))
        stored = 0
        for _sym, _r in results:
            _stored = int(_r.get('stored', 0) or 0) or sum(1 for c in (_r.get('contracts') or []) if c.get('stored'))
            stored += _stored
            print(f"  Futures {_sym}: stored={_stored} source={_r.get('source','?')}")
        print(f"✅ Futures OI three-layer update: {stored} rows, {_dt3.datetime.now()}")
    except Exception as e:
        print(f"[futures] {e}")
    try:
        if _dt3.datetime.now().weekday() == 0:
            from .services.sector_service import refresh_all_sectors
            refresh_all_sectors()
            print(f"✅ Sectors updated")
    except Exception as e:
        print(f"[sectors] {e}")


def create_app():
    from .db import init_db
    init_db()

    try:
        from .services.schema_registry import ensure_all_schemas
        ensure_all_schemas()
    except Exception as e:
        print(f"[app] WARNING: schema registry pass failed — {e}")

    try:
        from .services.yfinance_hardening import install as _install_yf_hardening
        _install_yf_hardening()
    except Exception as e:
        print(f"[app] WARNING: yfinance hardening not installed — {e}")

    app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)

    # ── Global NaN-safe JSON provider ───────────────────────────────────
    # Root-cause fix for a recurring class of bug: any route that returns
    # jsonify(some_dict_containing_a_bare_NaN_float) produces a response
    # with a literal `NaN` token in it -- Python's json module allows this
    # (allow_nan=True by default) but it is NOT valid JSON per spec, so
    # the browser's fetch().json() rejects it with "Unexpected token 'N'
    # ... is not valid JSON". This has shown up independently on multiple
    # unrelated pages (Trade Opportunity Scanner, S/R Breakout Scanner,
    # Analyze Symbol/earnings, the new Wall Term Structure page) --
    # different routes, same underlying gap: yfinance/computed floats
    # going straight into jsonify() with no NaN/Infinity guard. Rather
    # than patching each route individually (and inevitably missing some
    # that haven't been hit yet), this recursively sanitizes NaN/Infinity
    # to null for every jsonify() call app-wide, using the same
    # _json_safe() helper already used for this purpose elsewhere in the
    # codebase. This does NOT fix routes that crash outright trying to
    # int()/round() a NaN before it even reaches jsonify (e.g. the
    # /spy/weekly and [ta] cases) -- those need their own guard at the
    # point of conversion, see spy_strategies.py and technical_snapshot.py
    # fixes for that separate failure mode.
    try:
        from flask.json.provider import DefaultJSONProvider
        from .scanners.spy_strategies import _json_safe

        class NaNSafeJSONProvider(DefaultJSONProvider):
            def dumps(self, obj, **kwargs):
                return super().dumps(_json_safe(obj), **kwargs)

        app.json = NaNSafeJSONProvider(app)
        print("[app] NaN-safe JSON provider installed (fixes 'Unexpected token N ... not valid JSON' app-wide)")
    except Exception as e:
        print(f"[app] WARNING: NaN-safe JSON provider not installed — {e}")

    # ── Blueprints -- each independently isolated so one failing to
    # import doesn't take down the others, same pattern used for every
    # blueprint below. These 6 were previously imported/registered
    # WITHOUT this isolation ("core blueprints, always required") -- but
    # there's no good reason a transient issue in, say, the Strategy
    # routes should also prevent totally unrelated features (the
    # realtime dashboard, watchlist manager, etc.) from starting at all.
    # The loud printed WARNING already gives the same "notice something's
    # wrong" signal a hard startup failure would, without the full
    # blast radius.
    try:
        from .api.routes import api_bp, dte_pages_bp
        app.register_blueprint(api_bp)
        app.register_blueprint(dte_pages_bp)
        print("[app] Core API routes registered")
    except Exception as e:
        print(f"[app] WARNING: core api.routes not loaded — {e}")

    try:
        from .scanners.routes import scanner_bp, sqlviewer_bp
        app.register_blueprint(scanner_bp)
        app.register_blueprint(sqlviewer_bp)
        print("[app] Scanner routes registered")
    except Exception as e:
        print(f"[app] WARNING: scanners.routes not loaded — {e}")

    try:
        from .scanners.routes_strategy import strategy_bp
        app.register_blueprint(strategy_bp)
        print("[app] Strategy routes registered")
    except Exception as e:
        print(f"[app] WARNING: routes_strategy not loaded — {e}")

    try:
        from .journal.journal_routes import journal_bp, start_trade_alert_watcher, start_health_alert_watcher
        app.register_blueprint(journal_bp)
        # 4h to match signal_notifier's own journal_pnl_alerts_interval_sec
        # default -- this value only feeds Scheduler Hub's displayed
        # schedule (the actual cadence is driven by Signal Notifier's
        # loop, which reads the configurable setting), so leaving it at
        # 60s here would have shown a schedule that doesn't match what
        # actually runs.
        started = start_trade_alert_watcher(interval_seconds=14400)
        print(f"[app] Trade P&L alerts {'registered' if started else 'already registered'} (runs via Signal Notifier, not a separate watcher -- configure at /signal-notifier)")
        try:
            started_health = start_health_alert_watcher(interval_seconds=300)
            print(f"[app] Trade health alerts {'registered' if started_health else 'already registered'} (runs via Signal Notifier, not a separate watcher -- configure at /signal-notifier)")
        except Exception as e:
            print(f"[app] WARNING: health alert watcher not started — {e}")
    except Exception as e:
        print(f"[app] WARNING: journal_routes not loaded — {e}")

    try:
        from .scanners.scanner_dashboard import scanner_dashboard_bp
        app.register_blueprint(scanner_dashboard_bp)
        print("[app] Scanner dashboard registered")
    except Exception as e:
        print(f"[app] WARNING: scanner_dashboard not loaded — {e}")

    # ── Optional blueprints — isolated so one failure doesn't kill the app ─
    try:
        from .scanners.spy_strategies import spy_bp
        app.register_blueprint(spy_bp)
        try:
            from .scanners.gex_pine_export import gex_pine_bp
            app.register_blueprint(gex_pine_bp)
            print("[app] GEX Pine routes registered at /gex/pine and /gex/live")
        except Exception as _gpe:
            print(f"[app] WARNING: gex_pine_bp not loaded — {_gpe}")
    except Exception as e:
        print(f"[app] WARNING: spy_strategies not loaded — {e}")

    try:
        from .scanners.earnings import earnings_bp
        app.register_blueprint(earnings_bp)
    except Exception as e:
        print(f"[app] WARNING: earnings not loaded — {e}")

    try:
        from .scanners.earnings_calendar import earn_cal_bp
        app.register_blueprint(earn_cal_bp)
    except Exception as e:
        print(f"[app] WARNING: earnings_calendar not loaded — {e}")

    try:
        from .scanners.watchlist_manager import wl_bp, start_alert_rule_watcher
        app.register_blueprint(wl_bp)
        try:
            started2 = start_alert_rule_watcher(interval_seconds=900)
            print(f"[app] Alert rule watcher {'started' if started2 else 'already running'} (this one DOES run continuously in the background -- the only one that still does; see /scheduler-hub)")
        except Exception as e:
            print(f"[app] WARNING: alert rule watcher not started — {e}")
    except Exception as e:
        print(f"[app] WARNING: watchlist_manager not loaded — {e}")

    try:
        from .scanners.scanner_builder import start_price_backfill_watcher
        started3 = start_price_backfill_watcher(interval_seconds=120, batch_size=5)
        print(f"[app] Scanner price backfill {'registered' if started3 else 'already registered'} (manual only -- Run Now on /scheduler-hub, not automatic)")
    except Exception as e:
        print(f"[app] WARNING: price backfill watcher not started — {e}")

    try:
        from .scanners.scanner_builder import start_intraday_backfill_watcher
        started3b = start_intraday_backfill_watcher(interval_seconds=150, batch_size=3)
        print(f"[app] Scanner intraday backfill {'registered' if started3b else 'already registered'} (manual only -- Run Now on /scheduler-hub, not automatic)")
    except Exception as e:
        print(f"[app] WARNING: intraday backfill watcher not started — {e}")

    try:
        from .scanners.scanner_builder import start_daily_price_refresh_watcher
        started3c = start_daily_price_refresh_watcher(interval_seconds=300, batch_size=25)
        print(f"[app] Scanner daily price refresh {'registered' if started3c else 'already registered'} (runs at 7:30 AM via the morning data pipeline, not a separate watcher)")
    except Exception as e:
        print(f"[app] WARNING: daily price refresh watcher not started — {e}")

    try:
        from .scanners.scanner_builder import start_intraday_price_refresh_watcher
        started3d = start_intraday_price_refresh_watcher(interval_seconds=420, batch_size=15)
        print(f"[app] Scanner intraday price refresh {'registered' if started3d else 'already registered'} (runs at 7:30 AM via the morning data pipeline, not a separate watcher)")
    except Exception as e:
        print(f"[app] WARNING: intraday price refresh watcher not started — {e}")

    try:
        from .services.technical_snapshot import technical_snapshot_bp, start_technical_snapshot_watcher
        app.register_blueprint(technical_snapshot_bp)
        started4 = start_technical_snapshot_watcher(interval_seconds=180, batch_symbols=10)
        print(f"[app] Technical snapshot cache {'registered' if started4 else 'already registered'} (runs at 7:30 AM via the morning data pipeline, not a separate watcher)")
    except Exception as e:
        print(f"[app] WARNING: technical snapshot cache not started — {e}")

    try:
        # V104: 0-10 DTE Intraday Buildup + Positional Trend pages
        # (SPY/QQQ/SPX/IWM). Positional trend runs as step 7 of the
        # 7:30 AM morning pipeline; intraday buildup gets its own
        # 20-min interval job here (registered for Scheduler Hub
        # visibility + manual "Run Now", same pattern as the other
        # watchers on this page).
        from .services.dte_pages import register_dte_jobs
        from .services import unified_scheduler as _sched, dte_pages as _dte
        register_dte_jobs()
        _sched.register("dte_intraday_buildup", lambda: _dte.compute_intraday_buildup(),
                         interval_seconds=20 * 60, low_priority=True)
        print("[app] DTE pages (Intraday Buildup + Positional Trend) registered")
    except Exception as e:
        print(f"[app] WARNING: DTE pages not started — {e}")

    try:
        # V113: ICICI Direct auto-trading P&L monitor -- checks every
        # OPEN position's combined rupee P&L every 30s and auto-closes
        # (safe short-first sequencing) on target/stop-loss hit.
        from .services.icici_positions import register_icici_monitor_job
        from .services import unified_scheduler as _sched2, icici_positions as _icici
        register_icici_monitor_job()
        _sched2.register("icici_pnl_monitor", lambda: _icici.monitor_tick(),
                          interval_seconds=30, low_priority=False)
        print("[app] ICICI auto-trading P&L monitor registered")
    except Exception as e:
        print(f"[app] WARNING: ICICI auto-trading monitor not started — {e}")

    try:
        # V125: ICICI strategy evaluator -- checks every ENABLED
        # strategy's opening/closing conditions (price or reused
        # Scanner Builder live query) once a minute, resolving
        # ITM/ATM/OTM legs against live spot and firing tracked
        # positions automatically.
        from .services.icici_strategy_engine import register_strategy_evaluator_job
        from .services import unified_scheduler as _sched3, icici_strategy_engine as _strat
        register_strategy_evaluator_job()
        _sched3.register("icici_strategy_evaluator", lambda: _strat.evaluate_strategies_tick(),
                          interval_seconds=60, low_priority=False)
        print("[app] ICICI strategy evaluator registered")
    except Exception as e:
        print(f"[app] WARNING: ICICI strategy evaluator not started — {e}")

    try:
        # V134: Schwab auto-trading P&L monitor + strategy evaluator --
        # same architecture as the ICICI jobs, built on Schwab's
        # existing OAuth infrastructure.
        from .services.schwab_positions import register_monitor_job as _register_schwab_monitor
        from .services.schwab_strategy_engine import register_strategy_evaluator_job as _register_schwab_strategy
        from .services import unified_scheduler as _sched4, schwab_positions as _schwab_pos, schwab_strategy_engine as _schwab_strat
        _register_schwab_monitor()
        _register_schwab_strategy()
        _sched4.register("schwab_pnl_monitor", lambda: _schwab_pos.monitor_tick(), interval_seconds=30, low_priority=False)
        _sched4.register("schwab_strategy_evaluator", lambda: _schwab_strat.evaluate_strategies_tick(), interval_seconds=60, low_priority=False)
        print("[app] Schwab auto-trading monitor + strategy evaluator registered")
    except Exception as e:
        print(f"[app] WARNING: Schwab auto-trading jobs not started — {e}")

    try:
        # V145: Candle Context scheduled scan -- reuses the existing
        # Scheduler Hub time-based schedule UI (daily by default, or
        # weekly by restricting to one weekday) instead of building a
        # separate scheduling control.
        from .scanners.candle_context_scanner import register_scheduled_scan_job
        register_scheduled_scan_job()
        print("[app] Candle Context scheduled scan registered")
    except Exception as e:
        print(f"[app] WARNING: Candle Context scheduled scan not started — {e}")

    try:
        from .services.schwab_positions import register_pending_entries_job
        from .services import unified_scheduler as _sched5, schwab_positions as _schwab_pos2
        register_pending_entries_job()
        _sched5.register("schwab_pending_entries", lambda: _schwab_pos2.check_pending_entries(), interval_seconds=60, low_priority=False)
        print("[app] Schwab pending-order fill sync registered")
    except Exception as e:
        print(f"[app] WARNING: Schwab pending-order fill sync not started — {e}")

    try:
        from .scanners.institutional_scanner import inst_bp
        app.register_blueprint(inst_bp)
    except Exception as e:
        print(f"[app] WARN: institutional_scanner failed: {e}")
    try:
        from .scanners.smart_money_distribution_scanner import dist_bp
        app.register_blueprint(dist_bp)
    except Exception as e:
        print(f"[app] WARN: smart_money_distribution_scanner failed: {e}")
    try:
        from .scanners.candle_context_scanner import candle_ctx_bp
        app.register_blueprint(candle_ctx_bp)
    except Exception as e:
        print(f"[app] WARN: candle_context_scanner failed: {e}")
    try:
        from .scanners.trade_setup_scanner import trade_setup_bp
        app.register_blueprint(trade_setup_bp)
    except Exception as e:
        print(f"[app] WARN: trade_setup_scanner failed: {e}")
    try:
        from .scanners.trend_divergence_scanner import trend_div_bp
        app.register_blueprint(trend_div_bp)
    except Exception as e:
        print(f"[app] WARN: trend_divergence_scanner failed: {e}")
    try:
        from .scanners.volume_profile_scanner import vp_bp
        app.register_blueprint(vp_bp)
    except Exception as e:
        print(f"[app] WARN: volume_profile_scanner failed: {e}")
    try:
        from .scanners.weekly_plan_backtest import weekly_backtest_bp
        app.register_blueprint(weekly_backtest_bp)
    except Exception as e:
        print(f"[app] WARN: weekly_plan_backtest failed: {e}")
    try:
        from .scanners.conviction_scorer import conv_bp
        app.register_blueprint(conv_bp)
    except Exception as e:
        print(f"[app] WARN: conviction_scorer failed: {e}")
    try:
        from .journal.outcome_tracker import outcome_bp, _migrate
        app.register_blueprint(outcome_bp)
        _migrate()
    except Exception as e:
        print(f"[app] WARN: outcome_tracker failed: {e}")
    try:
        from .scanners.trade_planner import planner_bp
        app.register_blueprint(planner_bp)
    except Exception as e:
        print(f"[app] WARN: trade_planner failed: {e}")
    try:
        from .maya_pages import maya_bp
        app.register_blueprint(maya_bp)
    except Exception as e:
        print(f"[app] WARN: maya_pages failed: {e}")
    try:
        from .scanners.scanner_builder import scanner_builder_bp, _ensure_tables as _scanner_builder_ensure_tables
        app.register_blueprint(scanner_builder_bp)
        try:
            _scanner_builder_ensure_tables()
        except Exception as _sb_init_exc:
            print(f"[app] WARN: scanner_builder init failed: {_sb_init_exc}")
    except Exception as e:
        print(f"[app] WARN: scanner_builder failed: {e}")
    try:
        from .scanners.institutional_confluence import institutional_confluence_bp
        app.register_blueprint(institutional_confluence_bp)
        print("[app] Institutional Confluence Scanner registered at /institutional-confluence")
    except Exception as e:
        print(f"[app] WARN: institutional_confluence failed: {e}")
    try:
        from .scanners.backtest import backtest_bp
        app.register_blueprint(backtest_bp)
        print("[app] Backtest API registered at /backtest")
    except Exception as e:
        print(f"[app] WARN: backtest failed: {e}")
    try:
        from .scanners.replay_lab import replay_lab_bp
        app.register_blueprint(replay_lab_bp)
        print("[app] Replay Lab API registered at /replay-lab")
    except Exception as e:
        print(f"[app] WARN: replay_lab failed: {e}")
    try:
        from .scanners.intraday_gex_backtest import intraday_backtest_bp
        app.register_blueprint(intraday_backtest_bp)
        print("[app] Intraday GEX backtest registered at /intraday-backtest")
    except Exception as e:
        print(f"[app] WARN: intraday_gex_backtest failed: {e}")
    try:
        from .services.cftc_cot import cot_bp, _ensure_table as _cot_ensure
        app.register_blueprint(cot_bp)
        _cot_ensure()
    except Exception as e:
        print(f"[app] WARN: cftc_cot failed: {e}")
    try:
        from .services.weekly_analysis import wa_bp
        app.register_blueprint(wa_bp)
    except Exception as e:
        print(f"[app] WARNING: institutional_scanner not loaded — {e}")

    # Telegram watchlist price-crossing alerts -- registered for Scheduler
    # Hub visibility/manual trigger; actual execution runs via Signal
    # Notifier's loop (configure at /signal-notifier), not this watcher.
    try:
        from .services.telegram_alerts import start_telegram_alert_watcher, telegram_bp
        app.register_blueprint(telegram_bp)
        started = start_telegram_alert_watcher(interval_seconds=60)
        print(f"[app] Telegram price alerts {'registered' if started else 'already registered'} (runs via Signal Notifier, not a separate watcher -- configure at /signal-notifier)")
    except Exception as e:
        print(f"[app] WARNING: telegram alert watcher not started — {e}")

    # ── New blueprints: news, sectors ─────────────────────────────────────
    try:
        from .scanners.news_routes import news_bp, sector_bp
        app.register_blueprint(news_bp)
        app.register_blueprint(sector_bp)
    except Exception as e:
        print(f"[app] WARNING: news/sector not loaded — {e}")

    try:
        from .scanners.pattern_scanner import pattern_scanner_bp
        app.register_blueprint(pattern_scanner_bp)
        print("[app] Price Action Patterns scanner registered")
    except Exception as e:
        print(f"[app] WARNING: pattern scanner not loaded — {e}")

    try:
        from .scanners.mtf_scanner import mtf_scanner_bp
        app.register_blueprint(mtf_scanner_bp)
        print("[app] MTF Alignment scanner registered")
    except Exception as e:
        print(f"[app] WARNING: mtf scanner not loaded — {e}")

    try:
        from .scanners.sr_breakout_scanner import sr_bp
        app.register_blueprint(sr_bp)
    except Exception as e:
        print(f"[app] WARNING: sr_bp not loaded — {e}")

    try:
        from .scanners.regime_routes import regime_bp
        app.register_blueprint(regime_bp)
    except Exception as e:
        print(f"[app] WARNING: regime_bp not loaded — {e}")

    try:
        from .scanners.intraday_routes import intraday_bp
        app.register_blueprint(intraday_bp)
    except Exception as e:
        print(f"[app] WARNING: intraday_bp not loaded — {e}")

    try:
        from .scanners.market_structure import ms_bp
        app.register_blueprint(ms_bp)
    except Exception as e:
        print(f"[app] WARNING: market_structure not loaded — {e}")

    try:
        from .scanners.options_analysis import analysis_bp
        app.register_blueprint(analysis_bp)
    except Exception as e:
        print(f"[app] WARNING: analysis_bp not loaded — {e}")

    try:
        from .scanners.trade_opportunity_scanner import trade_opp_bp
        app.register_blueprint(trade_opp_bp)
        print("[app] Trade Opportunity Scanner registered at /trade-scanner")
    except Exception as e:
        print(f"[app] WARNING: trade_opportunity_scanner not loaded — {e}")

    try:
        from .scanners.signal_notifier import signal_notifier_bp, start_signal_notifier_watcher
        app.register_blueprint(signal_notifier_bp)
        start_signal_notifier_watcher(app)
        print("[app] Signal notifier watcher started")
    except Exception as e:
        print(f"[app] WARNING: signal notifier not started — {e}")

    try:
        from .ai.copilot import ai_copilot_bp
        app.register_blueprint(ai_copilot_bp)
        print("[app] AI Copilot registered at /ai-copilot")
    except Exception as e:
        print(f"[app] WARNING: AI Copilot not loaded — {e}")

    try:
        from .services.scheduler_hub import scheduler_hub_bp
        app.register_blueprint(scheduler_hub_bp)
        print("[app] Scheduler Hub registered at /scheduler-hub")
    except Exception as e:
        print(f"[app] WARNING: Scheduler Hub not loaded — {e}")

    try:
        from .services.diagnostics_routes import diagnostics_bp
        app.register_blueprint(diagnostics_bp)
        print("[app] Diagnostics registered at /diagnostics")
    except Exception as e:
        print(f"[app] WARNING: Diagnostics not loaded — {e}")
    
    try:
        from .scanners.uae_trade_scanner import uae_trade_bp
        app.register_blueprint(uae_trade_bp)
        print("[app] UAE Guide Trade Scanner registered at /uae-trade-scanner")
    except Exception as e:
        print(f"[app] WARNING: uae_trade_scanner not loaded — {e}")

    try:
        from .scanners.minervini_scanner import minervini_bp
        app.register_blueprint(minervini_bp)
        print("[app] Minervini SEPA Scanner registered at /minervini-scanner")
    except Exception as e:
        print(f"[app] WARNING: minervini_scanner not loaded — {e}")

    try:
        from .scanners.corporate_events_page import corporate_events_bp
        app.register_blueprint(corporate_events_bp)
        print("[app] Corporate Events Query page registered at /corporate-events")
    except Exception as e:
        print(f"[app] WARNING: corporate_events_page not loaded — {e}")

    try:
        from .ai.ai_hub import ai_hub_bp
        app.register_blueprint(ai_hub_bp)
        print("[app] AI Hub registered at /ai-hub")
    except Exception as e:
        print(f"[app] WARNING: ai_hub not loaded — {e}")

    try:
        from .scanners.agentic_ai_scanner import agentic_ai_bp, start_agentic_ai_scanner, _settings as _agentic_settings
        app.register_blueprint(agentic_ai_bp)
        # NOT auto-started at boot -- per explicit design decision, the
        # only things that should run continuously in the background are
        # signal notifier, journal alert checks, and alert rules. This
        # scanner is available at /agentic-ai-scanner and can still be
        # triggered manually from there (or re-enable auto-start by
        # calling start_agentic_ai_scanner() again here if you want it
        # back as a background job).
        print("[app] Agentic AI Scanner registered at /agentic-ai-scanner (manual trigger only, not auto-started)")
    except Exception as e:
        print(f"[app] WARNING: agentic_ai_scanner not loaded — {e}")
    try:
        from .services.scheduled_jobs import start_daily_jobs
        started_daily = start_daily_jobs(app)
        print(f"[app] Daily watchlist/GEX scheduler {'started' if started_daily else 'already running'}")
    except Exception as e:
        print(f"[app] WARNING: daily jobs scheduler not started — {e}")

    try:
        from .services.scheduled_jobs import start_watchlist_schedule
        started_wl_sched = start_watchlist_schedule(app)
        print(f"[app] Per-watchlist schedule (price/OI -> earnings -> indicators -> corp events -> "
              f"volume profile, futures OI) "
              f"{'started' if started_wl_sched else 'already running'}")
    except Exception as e:
        print(f"[app] WARNING: per-watchlist schedule not started — {e}")

    try:
        from .schwab.schwab_routes import schwab_bp
        app.register_blueprint(schwab_bp)
    except Exception as e:
        print(f"[app] WARNING: schwab_bp not loaded — {e}")

    try:
        from .autotrading.schwab_eod import schwab_eod_bp
        app.register_blueprint(schwab_eod_bp)
        print("[app] Auto Trading Schwab EOD registered at /auto-trading")
    except Exception as e:
        print(f"[app] WARNING: auto_trading_schwab not loaded — {e}")

    try:
        from .services.connections_status import connections_bp
        app.register_blueprint(connections_bp)
        print("[app] Connections status page registered at /connections")
    except Exception as e:
        print(f"[app] WARNING: connections_status not loaded — {e}")

    try:
        from .scanners.scoring_params_routes import scoring_params_bp
        app.register_blueprint(scoring_params_bp)
        print("[app] Scoring Parameters registered at /scoring-params")
    except Exception as e:
        print(f"[app] WARNING: scoring_params_routes not loaded — {e}")

    try:
        from .scanners.wall_term_structure import wall_term_bp
        app.register_blueprint(wall_term_bp)
        print("[app] Wall Term Structure registered at /wall-term-structure")
    except Exception as e:
        print(f"[app] WARNING: wall_term_structure not loaded — {e}")

    try:
        from .services.metals_oi_gate import metals_oi_gate_bp, _ensure_table as _metals_gate_ensure
        app.register_blueprint(metals_oi_gate_bp)
        _metals_gate_ensure()
        print("[app] Metals OI Gate registered at /metals-oi-gate")
    except Exception as e:
        print(f"[app] WARNING: metals_oi_gate not loaded — {e}")

    try:
        from .services.gex_trend_tracker import gex_trend_bp, _ensure_table as _gex_trend_ensure, register_scheduler_job as _gex_trend_register
        app.register_blueprint(gex_trend_bp)
        _gex_trend_ensure()
        _gex_trend_started = _gex_trend_register(interval_seconds=5 * 60)
        print(f"[app] GEX Trend Tracker registered at /gex-trend (scheduler {'started' if _gex_trend_started else 'already running'})")
    except Exception as e:
        print(f"[app] WARNING: gex_trend_tracker not loaded — {e}")

    try:
        from .services.live_chain_tracker import live_chain_bp, _ensure_table as _live_chain_ensure, register_scheduler_job as _live_chain_register
        app.register_blueprint(live_chain_bp)
        _live_chain_ensure()
        _live_chain_started = _live_chain_register(interval_seconds=5 * 60)
        print(f"[app] Live Chain Tracker registered at /live-chain (scheduler {'started' if _live_chain_started else 'already running'})")
    except Exception as e:
        print(f"[app] WARNING: live_chain_tracker not loaded — {e}")

    try:
        # queue starts empty until /tastytrade-backfill/api/enqueue_watchlist
        # is POSTed once -- seeding the full watchlist is a deliberate
        # action, not something that should fire silently on every app restart.
        from .services.tastytrade_options_backfill import tastytrade_backfill_bp, register_scheduler_job as _tt_backfill_register
        app.register_blueprint(tastytrade_backfill_bp)
        _tt_backfill_started = _tt_backfill_register(interval_seconds=90)
        print(f"[app] Tastytrade Options Backfill registered at /tastytrade-backfill, scheduler {'started' if _tt_backfill_started else 'already running'} "
              f"(queue empty until /api/enqueue_watchlist is POSTed once)")
    except Exception as e:
        print(f"[app] WARNING: tastytrade_options_backfill not loaded — {e}")

    try:
        from .scanners.swing_positioning import swing_positioning_bp
        app.register_blueprint(swing_positioning_bp)
        print("[app] Swing Positioning Scanner registered at /swing-positioning")
    except Exception as e:
        print(f"[app] WARNING: swing_positioning not loaded — {e}")

    try:
        from .scanners.iron_condor_candidates import iron_condor_bp
        app.register_blueprint(iron_condor_bp)
        print("[app] Iron Condor / Vertical Scanner registered at /iron-condor-scanner")
    except Exception as e:
        print(f"[app] WARNING: iron_condor_candidates not loaded — {e}")

    try:
        from .scanners.gex_predictive_analysis import gex_predictive_bp
        app.register_blueprint(gex_predictive_bp)
        print("[app] GEX Predictive Analysis registered at /gex-predictive-analysis")
    except Exception as e:
        print(f"[app] WARNING: gex_predictive_analysis not loaded — {e}")

    # Keep this independent: a predictive-analysis dependency must not hide
    # the saved-chain GEX dashboard route.
    try:
        from .scanners.gex_analysis import gex_analysis_bp
        app.register_blueprint(gex_analysis_bp)
        print("[app] GEX Analysis registered at /gex-analysis")
    except Exception as e:
        print(f"[app] WARNING: gex_analysis not loaded — {e}")

    try:
        from .scanners.greeks_strategy_scanner import greeks_strategy_bp
        app.register_blueprint(greeks_strategy_bp)
        print("[app] Greeks Strategy Scanner registered at /greeks-strategy-scanner")
    except Exception as e:
        print(f"[app] WARNING: greeks_strategy_scanner not loaded — {e}")

    try:
        from .services.option_sale_framework import option_sale_framework_bp
        app.register_blueprint(option_sale_framework_bp)
        print("[app] Option Sale Framework registered at /option-sale-framework")
    except Exception as e:
        print(f"[app] WARNING: option_sale_framework not loaded — {e}")

    try:
        # No blueprint -- this module has no routes, just the scheduled
        # daily futures OI fetch that was confirmed missing entirely
        # (no register_scheduler_job existed anywhere in this module),
        # which is why api_weekly_rolling's futures OI context has been
        # showing NO_DATA(0) for SPY/QQQ/IWM.
        from .services.futures_oi_real import register_scheduler_job as _futures_oi_register
        _futures_oi_started = _futures_oi_register(interval_seconds=21600)
        print(f"[app] Futures OI daily fetch scheduler {'started' if _futures_oi_started else 'already running'} (SPY/QQQ/IWM every 6h)")
    except Exception as e:
        print(f"[app] WARNING: futures_oi_real scheduler not loaded — {e}")

    try:
        from .services.scanner_primitives_guide import scanner_primitives_guide_bp
        app.register_blueprint(scanner_primitives_guide_bp)
        print("[app] Scanner Primitives Guide registered at /scanner-primitives-guide")
    except Exception as e:
        print(f"[app] WARNING: scanner_primitives_guide not loaded — {e}")

    try:
        from .charts.chart_routes import charts_bp
        app.register_blueprint(charts_bp)
        print("[app] Charts workspace registered at /charts (this module existed but was never registered here -- that's the actual root cause of the 404 the Pattern Search chart hit)")
    except Exception as e:
        print(f"[app] WARNING: charts module not loaded — {e}")

    try:
        from .scanners.realtime_dashboard import realtime_bp, init_realtime
        app.register_blueprint(realtime_bp)
        # Symbols to keep streaming live on app start — extend with whatever
        # you actively watch (uses your 104-ticker watchlist / futures roots).
        init_realtime(default_symbols=["SPY", "/MGC", "/GC"])
        print("[app] Realtime tastytrade dashboard registered at /realtime/<symbol>")
    except Exception as e:
        print(f"[app] WARNING: realtime_dashboard (tastytrade) not loaded — {e}")

    # ── Routes ─────────────────────────────────────────────────────────────
    # NOTE: the old 7:00 AM background thread that used to live here
    # (_daily_regime_runner) has been removed. It was a hardcoded,
    # undocumented duplicate of what scheduled_jobs.py's
    # 'morning_data_pipeline' job (07:30) now does properly -- it was
    # running the regime scan TWICE and the futures OI fetch TWICE every
    # single morning (once via _run_all_tasks(), then again explicitly
    # right after), invisible to and unmanageable from the Scheduler Hub
    # page since it never went through job_registry. Everything it did
    # (regime scan, futures OI, CFTC COT on Tuesdays, sector refresh on
    # Mondays) now runs exactly once, in explicit order, as steps of
    # morning_data_pipeline -- see oiapp/services/scheduled_jobs.py.


    @app.route("/scanner/sr/age_scan", methods=["GET"])
    def legacy_sr_age_scan():
        # Backward-compatible alias for older UI code that calls /scanner/...
        from .scanners.routes import api_sr_breakout_age
        return api_sr_breakout_age()
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/healthz")
    def healthz():
        """Lightweight liveness/status endpoint for running this app as an
        unattended service — a monitoring script (or the Task Scheduler
        wrapper) can hit this instead of needing anyone to open the UI to
        confirm the app and its background jobs are actually alive."""
        import time as _time
        payload = {"ok": True, "status": "up", "time": datetime.now().isoformat()}
        try:
            from .services.job_registry import list_jobs
            jobs = list_jobs()
            payload["jobs_registered"] = len(jobs)
            payload["jobs_enabled"] = sum(1 for j in jobs if j.get("enabled"))
            stale_cutoff = _time.time() - 6 * 3600
            stale = []
            for j in jobs:
                if not j.get("enabled") or not j.get("last_run_at"):
                    continue
                try:
                    last = datetime.fromisoformat(j["last_run_at"]).timestamp()
                    if last < stale_cutoff:
                        stale.append(j["key"])
                except Exception:
                    pass
            payload["stale_jobs"] = stale
        except Exception as e:
            payload["jobs_error"] = str(e)
        return jsonify(payload)

    @app.route("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    @app.after_request
    def add_no_cache(resp):
        if request.path.endswith('.js') or request.path.endswith('.css'):
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp

    return app
