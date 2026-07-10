"""
scheduler.py — Fetch OI and/or price data for a chosen watchlist.
- If watchlist has fetch_options_oi=1  → fetch Options OI data
- If fetch_options_oi=0                 → fetch price/volume only
Logs each run with watchlist name and counts.
"""
import threading, datetime, time
from ..db import get_symbols, log_scheduler_run
from .market import get_expirations, fetch_store_for
from ..scanners.earnings_calendar import refresh_calendar
from .fundamentals import refresh_betas

_status = {
    "running": False, "last_run": None, "last_msg": "",
    "watchlist_id": None, "watchlist_name": None,
    "fetched": 0, "total": 0, "fetch_oi": True,
}


def _get_watchlist_symbols(watchlist_id=None):
    """
    Returns (symbols, watchlist_name, fetch_oi) for the given watchlist.
    If watchlist_id is None: uses the DEFAULT watchlist (symbols table is already synced to it).
    All scanners read from the `symbols` table which is kept in sync with the default watchlist.
    """
    try:
        from ..scanners.watchlist_manager import _conn, _ensure_tables
        _ensure_tables()
        con = _conn()
        if watchlist_id is None:
            # Use default watchlist
            row = con.execute("SELECT id, name, fetch_options_oi FROM watchlists WHERE is_default=1 LIMIT 1").fetchone()
            if not row:
                row = con.execute("SELECT id, name, fetch_options_oi FROM watchlists ORDER BY id LIMIT 1").fetchone()
        else:
            row = con.execute("SELECT id, name, fetch_options_oi FROM watchlists WHERE id=?", (watchlist_id,)).fetchone()
        if not row:
            con.close()
            return get_symbols(), "Default", True
        wl_id_use, wl_name, fetch_oi = row[0], row[1], bool(row[2])
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (wl_id_use,)
        ).fetchall()
        con.close()
        return [r[0] for r in rows], wl_name, fetch_oi
    except Exception as e:
        print(f"[scheduler] watchlist load error: {e}")
        return get_symbols(), "Default (symbols table)", True


def _watchlists_to_process():
    """Return all watchlists sorted so the default one runs first."""
    try:
        from ..scanners.watchlist_manager import _conn, _ensure_tables
        _ensure_tables()
        con = _conn()
        rows = con.execute(
            """
            SELECT id, name, COALESCE(fetch_options_oi,0) AS fetch_options_oi
            FROM watchlists
            ORDER BY COALESCE(is_default,0) DESC, lower(name), id
            """
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[scheduler] watchlist list error: {e}")
        return []


def _update_watchlist_fetch_meta(watchlist_id, mode, count):
    try:
        from ..scanners.watchlist_manager import _conn, _ensure_tables
        _ensure_tables()
        con = _conn()
        con.execute(
            """
            UPDATE watchlists
            SET last_fetch_at=datetime('now'), last_fetch_mode=?, last_fetch_count=?
            WHERE id=?
            """,
            (str(mode), int(count), int(watchlist_id)),
        )
        con.commit(); con.close()
    except Exception as e:
        print(f"[scheduler] watchlist meta update error: {e}")


def _fetch_watchlist_sync(watchlist_id=None):
    """Fetch one watchlist synchronously. Price/volume always; OI only when enabled."""
    syms, wl_name, fetch_oi = _get_watchlist_symbols(watchlist_id)
    if not syms:
        syms = ["SPY"]
    fetched = []
    mode = "Options OI + Price/Volume" if fetch_oi else "Price/Volume"
    for sym in syms:
        if _fetch_price_only(sym):
            if fetch_oi:
                try:
                    exps = get_expirations(sym)[:10]
                    if exps:
                        fetch_store_for(sym, exps)
                except Exception as e:
                    print(f"[scheduler] {sym} OI fetch error: {e}")
            fetched.append(sym)
        time.sleep(0.35 if fetch_oi else 0.08)
    _update_watchlist_fetch_meta(watchlist_id, mode, len(fetched))
    return {"watchlist_id": watchlist_id, "watchlist_name": wl_name, "fetch_oi": fetch_oi, "mode": mode, "fetched": len(fetched), "total": len(syms)}


def run_all_watchlists_once(source='scheduler'):
    """Sequentially process every watchlist one at a time."""
    results = []
    for wl in _watchlists_to_process():
        wl_id = wl.get('id')
        wl_name = wl.get('name') or f'Watchlist {wl_id}'
        fetch_oi = bool(wl.get('fetch_options_oi'))
        _status.update({
            'running': True,
            'watchlist_id': wl_id,
            'watchlist_name': wl_name,
            'fetch_oi': fetch_oi,
            'last_msg': f'Running {wl_name} ...',
        })
        try:
            res = _fetch_watchlist_sync(watchlist_id=wl_id)
            results.append(res)
            log_scheduler_run(res.get('fetched', 0), 'success', f"Watchlist: {wl_name} | Mode: {res.get('mode')} | Fetched: {res.get('fetched')}")
        except Exception as e:
            results.append({'watchlist_id': wl_id, 'watchlist_name': wl_name, 'error': str(e)})
            log_scheduler_run(wl_name, 'error', str(e))
    _status.update({
        'running': False,
        'last_run': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'last_msg': f"✅ Completed {len(results)} watchlists",
        'watchlist_id': None,
        'watchlist_name': None,
    })
    return {'ok': True, 'results': results, 'count': len(results), 'source': source}


def _fetch_price_only(sym):
    """Fetch OHLCV price data for a symbol without options OI."""
    try:
        import yfinance as yf
        import sqlite3
        from pathlib import Path
        ticker = yf.Ticker(sym)
        hist = ticker.history(period="2d")
        if hist is None or hist.empty:
            return False
        # Store in a price_cache table
        latest = hist.iloc[-1]
        db = str(Path(__file__).resolve().parents[2] / "options_data.db")
        con = sqlite3.connect(db)
        con.execute("""
            CREATE TABLE IF NOT EXISTS price_cache (
                symbol TEXT NOT NULL, date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume INTEGER,
                PRIMARY KEY (symbol, date)
            )
        """)
        con.execute("""
            INSERT OR REPLACE INTO price_cache VALUES (?,?,?,?,?,?,?)
        """, (sym, datetime.date.today().isoformat(),
              round(float(latest["Open"]),4), round(float(latest["High"]),4),
              round(float(latest["Low"]),4), round(float(latest["Close"]),4),
              int(latest["Volume"])))
        con.commit(); con.close()
        return True
    except:
        return False


def _worker(watchlist_id=None):
    syms, wl_name, fetch_oi = _get_watchlist_symbols(watchlist_id)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _status.update({
        "running": True, "last_run": now, "last_msg": f"Started · {wl_name}",
        "watchlist_id": watchlist_id, "watchlist_name": wl_name,
        "fetched": 0, "total": len(syms), "fetch_oi": fetch_oi,
    })
    try:
        fetched = []
        if not syms:
            syms = ["SPY"]
        mode = "Options OI" if fetch_oi else "Price/Volume only"
        for sym in syms:
            if fetch_oi:
                exps = get_expirations(sym)[:10]
                if exps:
                    fetch_store_for(sym, exps)
                    fetched.append(sym)
            else:
                if _fetch_price_only(sym):
                    fetched.append(sym)
            _status["fetched"] = len(fetched)
            _status["last_msg"] = f"{mode} · {len(fetched)}/{len(syms)} · {wl_name}"
            time.sleep(0.5 if fetch_oi else 0.1)

        try:
            refresh_calendar(syms, force=False)
        except Exception as _e:
            print(f"[scheduler] earnings refresh error: {_e}")
        try:
            refresh_betas(syms, refresh=False)
        except Exception as _e:
            print(f"[scheduler] beta refresh error: {_e}")

        try:
            from ..scanners.watchlist_manager import run_alert_rules_once
            run_alert_rules_once(symbols=syms, watchlist_id=watchlist_id, source='scheduler')
        except Exception as _e:
            print(f"[scheduler] alert rules error: {_e}")

        run_scheduled_job(fetched, wl_name, fetch_oi)
        _status["last_msg"] = f"✅ Done · {len(fetched)}/{len(syms)} · {wl_name} · {mode}"
    except Exception as e:
        _status["last_msg"] = f"❌ Error: {e}"
        log_scheduler_run(syms, "error", str(e))
    finally:
        _status["running"] = False


def get_scheduler_status():
    return dict(_status)


def trigger_fetch_all(watchlist_id=None):
    if _status["running"]:
        return {"running": True, "message": "Already running"}
    if watchlist_id is None:
        t = threading.Thread(target=run_all_watchlists_once, kwargs={"source": "manual"}, daemon=True)
    else:
        t = threading.Thread(target=_worker, kwargs={"watchlist_id": watchlist_id}, daemon=True)
    t.start()
    return {"started": True, "watchlist_id": watchlist_id}


def stop_scheduler():
    _status["running"] = False
    _status["last_msg"] = "Stopped manually"
    return {"stopped": True}


def run_scheduled_job(symbols, watchlist_name="Default", fetch_oi=True):
    """Log the scheduler run and run post-fetch tasks."""
    try:
        mode = "OI" if fetch_oi else "Price"
        print(f"✅ Scheduler [{watchlist_name}/{mode}] — {len(symbols)} symbols")
        # News digest (only for OI fetches)
        if fetch_oi:
            try:
                from .news_service import fetch_market_news, save_news_to_db, generate_morning_digest
                news = fetch_market_news(symbols[:20])
                save_news_to_db(news)
                generate_morning_digest(symbols)
            except Exception as ne:
                print(f"[digest] {ne}")
        log_scheduler_run(symbols, "success",
                          f"Watchlist: {watchlist_name} | Mode: {mode} | "
                          f"Fetched: {len(symbols)}")
    except Exception as e:
        log_scheduler_run(symbols, "error", str(e))
