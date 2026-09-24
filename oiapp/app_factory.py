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

    # Stabilization baseline: interval/timed Scheduler Hub work is paused
    # across restarts. Per-watchlist yfinance refreshes and Telegram price
    # alerts use their own lean loops below; every other job remains visible
    # in Scheduler Hub and requires an explicit enable/unpause action.
    try:
        from .services.job_registry import set_global_pause
        set_global_pause(True)
        print("[app] Lean mode: nonessential Scheduler Hub jobs paused")
    except Exception as e:
        print(f"[app] WARNING: could not apply lean scheduler pause — {e}")

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
        from .journal.journal_routes import journal_bp
        app.register_blueprint(journal_bp)
        print("[app] Journal routes registered (journal background alerts disabled by lean mode)")
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
        from .scanners.gex_analysis import gex_analysis_bp
        app.register_blueprint(gex_analysis_bp)
        print("[app] Saved GEX Analysis registered at /gex-analysis")
    except Exception as e:
        print(f"[app] WARNING: gex_analysis not loaded — {e}")

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
        from .scanners.watchlist_manager import wl_bp
        app.register_blueprint(wl_bp)
        print("[app] Watchlist routes registered (alert-rule watcher disabled by lean mode)")
    except Exception as e:
        print(f"[app] WARNING: watchlist_manager not loaded — {e}")

    # Scanner cache/backfill loops are deliberately not started in lean mode.
    # They create persistent watcher threads and bulk SQLite/network work.
    print("[app] Lean mode: Scanner cache/backfill loops are on-demand only")

    try:
        from .services.technical_snapshot import technical_snapshot_bp
        app.register_blueprint(technical_snapshot_bp)
        print("[app] Technical snapshot routes registered (on-demand only)")
    except Exception as e:
        print(f"[app] WARNING: technical snapshot cache not started — {e}")

    # DTE, ICICI, and Schwab auto-trading checks must not exist as startup
    # jobs.  Their 30–60 second ticks were still consuming workers in a
    # supposedly lean process.  The corresponding pages remain on-demand.
    print("[app] Lean mode: DTE and broker auto-trading monitors are on-demand only")

    # Candle Context is intentionally on-demand in lean mode.  Registering
    # its scheduled scan on startup allowed a saved Scheduler Hub setting to
    # scan an entire watchlist even when nobody had opened that page.
    print("[app] Lean mode: Candle Context scan is on-demand only")

    print("[app] Lean mode: Schwab pending-order sync is on-demand only")

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
        from .scanners.signal_notifier import set_config as _set_signal_notifier_config
        app.register_blueprint(signal_notifier_bp)
        # Preserve Telegram price alerts while preventing every scanner,
        # journal-risk, and health sweep from starting at boot. Those can be
        # re-enabled intentionally from Signal Notifier/Scheduler Hub.
        _set_signal_notifier_config(
            enabled=False,
            journal_pnl_alerts_enabled=False,
            journal_deep_loss_alerts_enabled=False,
            journal_health_alerts_enabled=False,
            telegram_price_alerts_enabled=True,
        )
        start_signal_notifier_watcher(app)
        print("[app] Lean alert loop started (Telegram price alerts only)")
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
        print(f"[app] Scheduler Hub dispatcher {'started' if started_daily else 'already running'} (nonessential jobs paused by lean mode)")
    except Exception as e:
        print(f"[app] WARNING: daily jobs scheduler not started — {e}")

    try:
        from .services.scheduled_jobs import start_watchlist_schedule
        started_wl_sched = start_watchlist_schedule(app)
        print(f"[app] Per-watchlist yfinance refresh schedule {'started' if started_wl_sched else 'already running'}")
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
        from .services.gex_trend_tracker import gex_trend_bp, _ensure_table as _gex_trend_ensure
        app.register_blueprint(gex_trend_bp)
        _gex_trend_ensure()
        print("[app] GEX Trend Tracker registered at /gex-trend (on-demand only)")
    except Exception as e:
        print(f"[app] WARNING: gex_trend_tracker not loaded — {e}")

    try:
        from .services.live_chain_tracker import live_chain_bp, _ensure_table as _live_chain_ensure
        app.register_blueprint(live_chain_bp)
        _live_chain_ensure()
        print("[app] Live Chain Tracker registered at /live-chain (on-demand only)")
    except Exception as e:
        print(f"[app] WARNING: live_chain_tracker not loaded — {e}")

    try:
        from .services.tastytrade_options_backfill import tastytrade_backfill_bp
        app.register_blueprint(tastytrade_backfill_bp)
        print("[app] Tastytrade Options Backfill registered (manual/on-demand only)")
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

    print("[app] Lean mode: futures OI scheduler is on-demand only")

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
        from .scanners.realtime_dashboard import realtime_bp
        app.register_blueprint(realtime_bp)
        print("[app] Realtime dashboard registered at /realtime/<symbol> (no feed starts until opened)")
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
