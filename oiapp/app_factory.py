import os
from datetime import datetime
from flask import Flask, request, render_template, send_from_directory, jsonify

ROOT_DIR      = os.path.dirname(os.path.dirname(__file__))
TEMPLATES_DIR = os.path.join(ROOT_DIR, "templates")
STATIC_DIR    = os.path.join(ROOT_DIR, "oiapp", "static")


def _run_all_tasks(fast=False):
    """Run all scheduler tasks. fast=True skips regime scan (just futures+sectors)."""
    import datetime as _dt3, concurrent.futures as _cf3
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
        def _fetch_one(_sym):
            return _sym, fetch_futures_oi_three_layer(_sym, use_cme_fallback=True, include_cot=True)
        with _cf3.ThreadPoolExecutor(max_workers=6) as ex:
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

    app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)

    # ── Core blueprints (always required) ─────────────────────────────────
    from .api.routes import api_bp
    from .scanners.routes import scanner_bp, sqlviewer_bp
    from .scanners.routes_strategy import strategy_bp
    from .journal.journal_routes import journal_bp, start_trade_alert_watcher, start_health_alert_watcher
    from .scanners.scanner_dashboard import scanner_dashboard_bp

    app.register_blueprint(api_bp)
    app.register_blueprint(scanner_bp)
    app.register_blueprint(strategy_bp)
    app.register_blueprint(journal_bp)
    app.register_blueprint(scanner_dashboard_bp)
    started = start_trade_alert_watcher(interval_seconds=60)
    print(f"[app] Trade alert watcher {'started' if started else 'already running'}")
    try:
        started_health = start_health_alert_watcher(interval_seconds=300)
        print(f"[app] Health alert watcher {'started' if started_health else 'already running'}")
    except Exception as e:
        print(f"[app] WARNING: health alert watcher not started — {e}")
    app.register_blueprint(sqlviewer_bp)

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
            print(f"[app] Alert rule watcher {'started' if started2 else 'already running'}")
        except Exception as e:
            print(f"[app] WARNING: alert rule watcher not started — {e}")
    except Exception as e:
        print(f"[app] WARNING: watchlist_manager not loaded — {e}")

    #try:
    #    from .scanners.scanner_builder import start_price_backfill_watcher
        # started3 = start_price_backfill_watcher(interval_seconds=120, batch_size=5)
        #print(f"[app] Scanner price backfill watcher {'started' if started3 else 'already running'}")
    #except Exception as e:
    #    print(f"[app] WARNING: price backfill watcher not started — {e}")

    #try:
    #    from .scanners.scanner_builder import start_intraday_backfill_watcher
    #    started3b = start_intraday_backfill_watcher(interval_seconds=150, batch_size=3)
    #    print(f"[app] Scanner intraday backfill watcher {'started' if started3b else 'already running'}")
    #except Exception as e:
    #    print(f"[app] WARNING: intraday backfill watcher not started — {e}")

    #try:
    #    from .scanners.scanner_builder import start_daily_price_refresh_watcher
    #    started3c = start_daily_price_refresh_watcher(interval_seconds=300, batch_size=25)
    #    print(f"[app] Scanner daily price refresh watcher {'started' if started3c else 'already running'}")
    #except Exception as e:
    #    print(f"[app] WARNING: daily price refresh watcher not started — {e}")

    #try:
    #    from .scanners.scanner_builder import start_intraday_price_refresh_watcher
    #    started3d = start_intraday_price_refresh_watcher(interval_seconds=420, batch_size=15)
    #    print(f"[app] Scanner intraday price refresh watcher {'started' if started3d else 'already running'}")
    #except Exception as e:
    #    print(f"[app] WARNING: intraday price refresh watcher not started — {e}")

    #try:
    #    from .services.technical_snapshot import technical_snapshot_bp, start_technical_snapshot_watcher
    #    app.register_blueprint(technical_snapshot_bp)
    #    started4 = start_technical_snapshot_watcher(interval_seconds=180, batch_symbols=10)
    #    print(f"[app] Technical snapshot cache watcher {'started' if started4 else 'already running'}")
    #except Exception as e:
    #    print(f"[app] WARNING: technical snapshot cache not started — {e}")

    try:
        from .scanners.institutional_scanner import inst_bp
        app.register_blueprint(inst_bp)
    except Exception as e:
        print(f"[app] WARN: institutional_scanner failed: {e}")
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

    # Start Telegram alert watcher for watchlist price crossings.
    try:
        from .services.telegram_alerts import start_telegram_alert_watcher, telegram_bp
        app.register_blueprint(telegram_bp)
        started = start_telegram_alert_watcher(interval_seconds=60)
        print(f"[app] Telegram alert watcher {'started' if started else 'already running'}")
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
        from .scanners.uae_trade_scanner import uae_trade_bp
        app.register_blueprint(uae_trade_bp)
        print("[app] UAE Guide Trade Scanner registered at /uae-trade-scanner")
    except Exception as e:
        print(f"[app] WARNING: uae_trade_scanner not loaded — {e}")

    try:
        from .ai.ai_hub import ai_hub_bp
        app.register_blueprint(ai_hub_bp)
        print("[app] AI Hub registered at /ai-hub")
    except Exception as e:
        print(f"[app] WARNING: ai_hub not loaded — {e}")

    try:
        from .scanners.agentic_ai_scanner import agentic_ai_bp, start_agentic_ai_scanner, _settings as _agentic_settings
        app.register_blueprint(agentic_ai_bp)
        _a_settings = _agentic_settings()
        if _a_settings.get("enabled", True):
            start_agentic_ai_scanner(interval_seconds=_a_settings.get("interval_seconds", 3600))
            print("[app] Agentic AI Scanner registered at /agentic-ai-scanner; watcher auto-started (enabled in settings)")
        else:
            print("[app] Agentic AI Scanner registered at /agentic-ai-scanner; watcher NOT started (disabled in settings)")
    except Exception as e:
        print(f"[app] WARNING: agentic_ai_scanner not loaded — {e}")
    try:
        from .services.scheduled_jobs import start_daily_jobs
        started_daily = start_daily_jobs(app)
        print(f"[app] Daily watchlist/GEX scheduler {'started' if started_daily else 'already running'}")
    except Exception as e:
        print(f"[app] WARNING: daily jobs scheduler not started — {e}")

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
        from .scanners.realtime_dashboard import realtime_bp, init_realtime
        app.register_blueprint(realtime_bp)
        # Symbols to keep streaming live on app start — extend with whatever
        # you actively watch (uses your 104-ticker watchlist / futures roots).
        init_realtime(default_symbols=["SPY", "/MGC", "/GC"])
        print("[app] Realtime tastytrade dashboard registered at /realtime/<symbol>")
    except Exception as e:
        print(f"[app] WARNING: realtime_dashboard (tastytrade) not loaded — {e}")

    # ── Routes ─────────────────────────────────────────────────────────────
    # ── 7AM auto-scheduler for regime scan ──────────────────────────────
    import threading, datetime as _dt, time as _time
    def _daily_regime_runner():
        import datetime as _dt2
        while True:
            now = _dt.datetime.now()
            # Next 7:00 AM
            target = now.replace(hour=7, minute=0, second=0, microsecond=0)
            if now >= target:
                target += _dt.timedelta(days=1)
            sleep_secs = (target - now).total_seconds()
            _time.sleep(sleep_secs)
            _run_all_tasks()
            try:
                from .scanners.regime_scanner import run_regime_scan
                run_regime_scan()
                print(f"✅ 7AM regime scan completed: {_dt.datetime.now()}")
            except Exception as e:
                print(f"[7AM scan] {e}")
            # Fetch futures OI via Schwab (authenticated)
            try:
                from .services.futures_oi_schwab import fetch_futures_oi_three_layer, SCHWAB_ROOTS
                for _fsym in SCHWAB_ROOTS.keys():
                    _r = fetch_futures_oi_three_layer(_fsym, use_cme_fallback=True, include_cot=True)
                    if not _r.get("ok"):
                        print(f"[7AM futures] {_fsym}: {_r.get('error','unknown error')}")
                print(f"✅ 7AM futures OI three-layer fetched: {_dt.datetime.now()}")
            except Exception as e3:
                print(f"[7AM futures Schwab] {e3}")
            # Fetch CFTC COT data on Tuesdays (CFTC releases weekly on Tuesday afternoon)
            try:
                if _dt2.datetime.now().weekday() == 1:   # 1 = Tuesday
                    from .services.cftc_cot import fetch_cot_data as _fetch_cot
                    _cr = _fetch_cot(years=2, force=False)
                    print(f"✅ 7AM CFTC COT fetched: {_cr.get('message','')}")
            except Exception as _e_cot:
                print(f"[7AM COT] {_e_cot}")
            # Update sector data weekly
            try:
                if _dt2.datetime.now().weekday() == 0:  # Monday
                    from .services.sector_service import refresh_all_sectors
                    refresh_all_sectors()
                print(f"✅ 7AM futures OI fetched: {_dt.datetime.now()}")
            except Exception as e2:
                print(f"[7AM futures] {e2}")

    _thread = threading.Thread(target=_daily_regime_runner, daemon=True)
    _thread.start()


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
