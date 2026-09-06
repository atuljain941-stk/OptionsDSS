"""
watchlist_manager.py — Multi-watchlist management with options OI fetch flag.

Tables:
  watchlists        — id, name, description, fetch_options_oi, color, created_at
  watchlist_symbols — watchlist_id, symbol, added_at
  
The legacy `symbols` table is kept for backwards compatibility but the scheduler
now reads from watchlists where fetch_options_oi=1.
"""
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

wl_bp = Blueprint("wl_bp", __name__, url_prefix="/watchlists")

# Tracks which watchlist IDs currently have a background price/OI fetch
# running, so clicking "Fetch Price"/"Fetch OI" again while one is still
# in progress returns a clear "already running" instead of silently
# spawning a second, redundant, fully-concurrent sweep over the same few
# hundred symbols (which was happening before -- a real contributor to
# repeated slowness, since impatient re-clicking is a natural reaction
# to a request that LOOKS stuck).
_fetch_in_progress = set()
_fetch_in_progress_lock = threading.Lock()
# The one-minute extended-hours fetch is independent from daily/OI fetches,
# but a duplicate would create redundant DXLink subscriptions, so guard it too.
_intraday_fetch_in_progress = set()
_intraday_fetch_in_progress_lock = threading.Lock()
from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

_WL_INIT_LOCK = threading.RLock()
_WL_INITIALIZED = False

# ── DB helpers ─────────────────────────────────────────────────────────────
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    return c



_YF_SYMBOL_RE = re.compile(r"^[A-Z0-9.$=^_-]{1,40}$")

def _normalize_watchlist_symbol(value):
    """Normalize a watchlist symbol while preserving yfinance futures suffixes.

    Examples accepted: AAPL, BRK-B, BRK.B, BTC-USD, GC=F, EURUSD=X, ^VIX.
    Returns '' for clearly unsafe/invalid values.
    """
    sym = str(value or "").strip().upper()
    if not sym:
        return ""
    # Users often paste semicolon-delimited text; separators are handled upstream,
    # but trim common trailing punctuation defensively.
    sym = sym.strip().strip(",;")
    if not sym or not _YF_SYMBOL_RE.match(sym):
        return ""
    return sym

def _parse_watchlist_symbols(value):
    """Return (symbols, rejected) from an array or pasted comma/newline text.

    The previous frontend regex rejected yfinance futures such as GC=F.
    Keep parsing server-side too so API calls and future UI changes behave the same.
    """
    if value is None:
        return [], []
    if isinstance(value, str):
        raw_items = re.split(r"[,\n\r\t ]+", value)
    else:
        raw_items = list(value or [])
    out, bad = [], []
    seen = set()
    for item in raw_items:
        raw = str(item or "").strip()
        if not raw:
            continue
        sym = _normalize_watchlist_symbol(raw)
        if not sym:
            bad.append(raw)
            continue
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return sorted(out), bad

def _ensure_tables():
    global _WL_INITIALIZED
    if _WL_INITIALIZED:
        return

    with _WL_INIT_LOCK:
        if _WL_INITIALIZED:
            return
        con = _conn()
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS price_cache (
                    symbol   TEXT NOT NULL,
                    date     TEXT NOT NULL,
                    open     REAL, high  REAL, low REAL, close REAL, volume INTEGER,
                    PRIMARY KEY (symbol, date)
                );
                CREATE TABLE IF NOT EXISTS watchlists (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    name            TEXT NOT NULL UNIQUE,
                    description     TEXT DEFAULT '',
                    fetch_options_oi INTEGER DEFAULT 0,
                    is_default      INTEGER DEFAULT 0,
                    color           TEXT DEFAULT '#818cf8',
                    created_at      TEXT DEFAULT (datetime('now')),
                    last_fetch_at   TEXT,
                    last_fetch_mode TEXT,
                    last_fetch_count INTEGER DEFAULT 0,
                    schedule_price_oi_time  TEXT,
                    schedule_earnings_time  TEXT,
                    schedule_indicators_time TEXT,
                    schedule_corporate_events_time TEXT,
                    schedule_price_oi_last_date  TEXT,
                    schedule_earnings_last_date  TEXT,
                    schedule_indicators_last_date TEXT,
                    schedule_corporate_events_last_date TEXT,
                    schedule_volume_profile_time TEXT,
                    schedule_volume_profile_last_date TEXT,
                    schedule_intraday_price_time TEXT,
                    schedule_intraday_price_last_date TEXT
                );
                CREATE TABLE IF NOT EXISTS corporate_events_snapshot (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol          TEXT NOT NULL,
                    as_of_date      TEXT NOT NULL,
                    -- insider activity (SEC Form 4, real $ and shares)
                    insider_dollars_bought  REAL,
                    insider_dollars_sold    REAL,
                    insider_net_dollars     REAL,
                    insider_shares_bought   REAL,
                    insider_shares_sold     REAL,
                    insider_transaction_count INTEGER,
                    insider_lookback_days  INTEGER,
                    -- debt (SEC XBRL, real $ figures)
                    debt_latest_value      REAL,
                    debt_latest_period_end TEXT,
                    debt_prior_value       REAL,
                    debt_dollar_change     REAL,
                    debt_pct_change        REAL,
                    debt_tag_used          TEXT,
                    -- volume (oiapp's own cached price data)
                    volume_latest           INTEGER,
                    volume_avg              REAL,
                    volume_pct_of_average   REAL,
                    -- material events (SEC 8-K item codes, no text)
                    material_events_json    TEXT DEFAULT '[]',
                    fetched_at              TEXT DEFAULT (datetime('now')),
                    UNIQUE(symbol, as_of_date)
                );
                CREATE TABLE IF NOT EXISTS watchlist_symbols (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id    INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
                    symbol          TEXT NOT NULL,
                    added_at        TEXT DEFAULT (datetime('now')),
                    alert_price     REAL,
                    alert_direction TEXT DEFAULT 'both',
                    alert_enabled   INTEGER DEFAULT 0,
                    alert_last_price REAL,
                    alert_last_side TEXT,
                    alert_last_sent_at TEXT,
                    UNIQUE(watchlist_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS app_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_wl_sym_wlid ON watchlist_symbols(watchlist_id);
                CREATE INDEX IF NOT EXISTS idx_wl_sym_sym  ON watchlist_symbols(symbol);
                CREATE TABLE IF NOT EXISTS alert_notifications (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_type   TEXT NOT NULL,
                    title        TEXT NOT NULL,
                    detail       TEXT DEFAULT '',
                    source       TEXT DEFAULT 'system',
                    severity     TEXT DEFAULT 'info',
                    symbol       TEXT,
                    trade_id     INTEGER,
                    scanner_name TEXT,
                    metadata     TEXT,
                    created_at   TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_alert_notifications_created_at ON alert_notifications(created_at DESC);
            """)
            for _ddl in (
                "ALTER TABLE watchlist_symbols ADD COLUMN alert_price REAL",
                "ALTER TABLE watchlist_symbols ADD COLUMN alert_direction TEXT DEFAULT 'both'",
                "ALTER TABLE watchlist_symbols ADD COLUMN alert_enabled INTEGER DEFAULT 0",
                "ALTER TABLE watchlist_symbols ADD COLUMN alert_last_price REAL",
                "ALTER TABLE watchlist_symbols ADD COLUMN alert_last_side TEXT",
                "ALTER TABLE watchlist_symbols ADD COLUMN alert_last_sent_at TEXT",
            ):
                try:
                    con.execute(_ddl)
                except Exception:
                    pass
            for _ddl in (
                "ALTER TABLE watchlists ADD COLUMN is_default INTEGER DEFAULT 0",
                "ALTER TABLE watchlists ADD COLUMN last_fetch_at TEXT",
                "ALTER TABLE watchlists ADD COLUMN last_fetch_mode TEXT",
                "ALTER TABLE watchlists ADD COLUMN last_fetch_count INTEGER DEFAULT 0",
                "ALTER TABLE watchlists ADD COLUMN schedule_price_oi_time TEXT",
                "ALTER TABLE watchlists ADD COLUMN schedule_earnings_time TEXT",
                "ALTER TABLE watchlists ADD COLUMN schedule_indicators_time TEXT",
                "ALTER TABLE watchlists ADD COLUMN schedule_price_oi_last_date TEXT",
                "ALTER TABLE watchlists ADD COLUMN schedule_earnings_last_date TEXT",
                "ALTER TABLE watchlists ADD COLUMN schedule_indicators_last_date TEXT",
                "ALTER TABLE watchlists ADD COLUMN schedule_corporate_events_time TEXT",
                "ALTER TABLE watchlists ADD COLUMN schedule_corporate_events_last_date TEXT",
                "ALTER TABLE watchlists ADD COLUMN schedule_volume_profile_time TEXT",
                "ALTER TABLE watchlists ADD COLUMN schedule_volume_profile_last_date TEXT",
                "ALTER TABLE watchlists ADD COLUMN schedule_intraday_price_time TEXT",
                "ALTER TABLE watchlists ADD COLUMN schedule_intraday_price_last_date TEXT",
            ):
                try:
                    con.execute(_ddl)
                except Exception:
                    pass

            existing = con.execute("SELECT id FROM watchlists WHERE name='Options Watchlist'").fetchone()
            if not existing:
                con.execute("""
                    INSERT OR IGNORE INTO watchlists (name, description, fetch_options_oi, is_default, color)
                    VALUES ('Options Watchlist', 'Symbols tracked for Options OI data', 1, 1, '#22c55e')
                """)
                wl_id = con.execute("SELECT id FROM watchlists WHERE name='Options Watchlist'").fetchone()[0]
                syms = con.execute("SELECT symbol FROM symbols WHERE symbol IS NOT NULL").fetchall()
                for row in syms:
                    con.execute(
                        "INSERT OR IGNORE INTO watchlist_symbols (watchlist_id, symbol) VALUES (?,?)",
                        (wl_id, row[0]),
                    )
            con.commit()
            _WL_INITIALIZED = True
        finally:
            con.close()


def get_symbols_for_options_oi():
    """Return sorted list of symbols from watchlists with fetch_options_oi=1."""
    _ensure_tables()
    con = _conn()
    rows = con.execute("""
        SELECT DISTINCT ws.symbol
        FROM watchlist_symbols ws
        JOIN watchlists w ON w.id = ws.watchlist_id
        WHERE w.fetch_options_oi = 1
        ORDER BY ws.symbol
    """).fetchall()
    con.close()
    return [r[0] for r in rows]


def get_all_watchlist_symbols():
    """Return all symbols across all watchlists (union)."""
    _ensure_tables()
    con = _conn()
    rows = con.execute(
        "SELECT DISTINCT symbol FROM watchlist_symbols ORDER BY symbol"
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def _get_setting(key: str, default=None):
    _ensure_tables()
    con = _conn()
    try:
        row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row and row[0] is not None else default
    finally:
        con.close()


def _set_setting(key: str, value):
    _ensure_tables()
    con = _conn()
    try:
        con.execute("INSERT INTO app_settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, '' if value is None else str(value)))
        con.commit()
    finally:
        con.close()


_ALERT_FREQUENCY_OPTIONS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
}


def _normalise_alert_frequency(value=None, default: str = "15m") -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "5": "5m", "5min": "5m", "5 mins": "5m", "5 minutes": "5m",
        "15": "15m", "15min": "15m", "15 mins": "15m", "15 minutes": "15m",
        "60": "1h", "60m": "1h", "1hr": "1h", "1 hour": "1h",
        "120": "2h", "120m": "2h", "2hr": "2h", "2 hours": "2h",
        "240": "4h", "240m": "4h", "4hr": "4h", "4 hours": "4h",
        "daily": "1d", "day": "1d", "1day": "1d", "24h": "1d",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in _ALERT_FREQUENCY_OPTIONS else default


def get_global_alert_frequency(default: str = "15m") -> str:
    """Return the global Telegram alert cadence stored in app_settings.

    This setting is shared by price alerts, scanner/watchlist alerts, trade
    health/PNR alerts, and custom position alerts.  Watchers read it on every
    sleep cycle so changing it in the UI does not require an app restart.
    """
    try:
        raw = _get_setting("telegram_alert_frequency", None)
    except Exception:
        raw = None
    if not raw:
        try:
            import os as _os
            raw = _os.getenv("TELEGRAM_ALERT_FREQUENCY") or _os.getenv("ALERT_FREQUENCY")
        except Exception:
            raw = None
    return _normalise_alert_frequency(raw, default)


def set_global_alert_frequency(value: str) -> str:
    freq = _normalise_alert_frequency(value, "15m")
    _set_setting("telegram_alert_frequency", freq)
    return freq


def get_global_alert_interval_seconds(default_seconds: int = 900) -> int:
    # Global means one cadence for all Telegram alert loops.  If the user has
    # not saved a setting yet, use 15m rather than each watcher preserving its
    # old hard-coded interval.
    return int(_ALERT_FREQUENCY_OPTIONS.get(get_global_alert_frequency("15m"), 900))


def _fetch_live_prices(symbols, max_seconds: float = 8.0):
    """Return dict symbol->spot using threads to keep the panel responsive.

    Hard time budget: this runs SYNCHRONOUSLY inside an HTTP request (it's
    used by the "view watchlist with prices" table, which needs prices in
    the response, unlike every other watchlist symbol-fetch path which
    backgrounds itself). Without a bound, a watchlist with even a few
    never-before-seen bad symbols can tie up a waitress worker thread for
    as long as those fetches take -- with enough of them (or enough
    concurrent page loads hitting this), that's exactly how all 8 worker
    threads end up stuck at once. Whatever hasn't resolved by max_seconds
    comes back as None instead of blocking further; the negative-cache in
    market.py means a symbol that times out here won't cost this again on
    the next page load.
    """
    symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not symbols:
        return {}
    prices = {sym: None for sym in symbols}
    try:
        from concurrent.futures import ThreadPoolExecutor, wait as _wait
        from ..services.market import get_spot
        ex = ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols))))
        try:
            futs = {ex.submit(get_spot, sym): sym for sym in symbols}
            done, not_done = _wait(futs.keys(), timeout=max_seconds)
            for fut in done:
                sym = futs[fut]
                try:
                    prices[sym] = fut.result()
                except Exception:
                    prices[sym] = None
            if not_done:
                print(f"[watchlist_manager] _fetch_live_prices: {len(not_done)}/{len(symbols)} "
                      f"symbol(s) didn't resolve within {max_seconds}s -- returning partial results "
                      f"rather than blocking further (still-running fetches finish in the background "
                      f"and populate the cache for next time).")
        finally:
            # wait=False is the entire point: don't let this function's
            # return be blocked by symbols that are still fetching -- they
            # keep running on this executor's own threads (not the calling
            # waitress worker) and their results land in market.py's cache
            # for whoever asks next, they just won't be in THIS response.
            ex.shutdown(wait=False)
    except Exception:
        try:
            from ..services.market import get_spot
            for sym in symbols:
                try:
                    prices[sym] = get_spot(sym)
                except Exception:
                    prices[sym] = None
        except Exception:
            pass
    return prices


def log_alert_notification(alert_type, title, detail='', *, symbol=None, trade_id=None, severity='info', source='system', metadata=None, scanner_name=None):
    """Persist a notification in alert history for the bell and alerts hub."""
    _ensure_tables()
    con = _conn()
    try:
        con.execute(
            """
            INSERT INTO alert_notifications
                (alert_type, title, detail, source, severity, symbol, trade_id, scanner_name, metadata)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                str(alert_type or 'INFO').strip(),
                str(title or 'Alert').strip(),
                '' if detail is None else str(detail),
                str(source or 'system').strip(),
                str(severity or 'info').strip(),
                None if symbol is None else str(symbol).strip().upper(),
                trade_id,
                None if scanner_name is None else str(scanner_name).strip(),
                None if metadata is None else __import__('json').dumps(metadata, default=str),
            ),
        )
        con.commit()
        return True
    finally:
        con.close()


def store_corporate_events_snapshot(symbol, insider=None, debt=None, volume=None, events=None):
    """Persists one day's corporate-events snapshot for a symbol -- real
    $ and share figures from oiapp.services.sec_edgar, not text/news.
    UNIQUE(symbol, as_of_date) means a same-day re-fetch replaces rather
    than duplicates. Each of insider/debt/volume/events is optional
    (pass whatever you actually fetched) so a partial failure upstream
    (e.g. debt data unavailable for this company) doesn't block storing
    what DID succeed.
    """
    _ensure_tables()
    con = _conn()
    try:
        today = __import__('datetime').date.today().isoformat()
        insider = insider or {}
        debt = debt or {}
        volume = volume or {}
        events = events or {}
        con.execute(
            """
            INSERT INTO corporate_events_snapshot
                (symbol, as_of_date,
                 insider_dollars_bought, insider_dollars_sold, insider_net_dollars,
                 insider_shares_bought, insider_shares_sold, insider_transaction_count, insider_lookback_days,
                 debt_latest_value, debt_latest_period_end, debt_prior_value, debt_dollar_change, debt_pct_change, debt_tag_used,
                 volume_latest, volume_avg, volume_pct_of_average,
                 material_events_json, fetched_at)
            VALUES (?,?, ?,?,?,?,?,?,?, ?,?,?,?,?,?, ?,?,?, ?, datetime('now'))
            ON CONFLICT(symbol, as_of_date) DO UPDATE SET
                insider_dollars_bought=excluded.insider_dollars_bought,
                insider_dollars_sold=excluded.insider_dollars_sold,
                insider_net_dollars=excluded.insider_net_dollars,
                insider_shares_bought=excluded.insider_shares_bought,
                insider_shares_sold=excluded.insider_shares_sold,
                insider_transaction_count=excluded.insider_transaction_count,
                insider_lookback_days=excluded.insider_lookback_days,
                debt_latest_value=excluded.debt_latest_value,
                debt_latest_period_end=excluded.debt_latest_period_end,
                debt_prior_value=excluded.debt_prior_value,
                debt_dollar_change=excluded.debt_dollar_change,
                debt_pct_change=excluded.debt_pct_change,
                debt_tag_used=excluded.debt_tag_used,
                volume_latest=excluded.volume_latest,
                volume_avg=excluded.volume_avg,
                volume_pct_of_average=excluded.volume_pct_of_average,
                material_events_json=excluded.material_events_json,
                fetched_at=datetime('now')
            """,
            (
                symbol.upper(), today,
                insider.get("dollars_bought"), insider.get("dollars_sold"), insider.get("net_dollars"),
                insider.get("shares_bought"), insider.get("shares_sold"),
                insider.get("transaction_count"), insider.get("lookback_days"),
                debt.get("latest_value"), debt.get("latest_period_end"), debt.get("prior_value"),
                debt.get("dollar_change"), debt.get("pct_change"), debt.get("tag_used"),
                volume.get("latest_volume"), volume.get("avg_volume"), volume.get("pct_of_average"),
                __import__('json').dumps(events.get("events", []), default=str),
            ),
        )
        con.commit()
        return True
    finally:
        con.close()


def get_corporate_events_snapshot(symbol, limit=30):
    """Recent corporate-events snapshots for a symbol, most recent first
    -- backs the watchlist page's detail view."""
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT * FROM corporate_events_snapshot WHERE symbol=? ORDER BY as_of_date DESC LIMIT ?",
            (symbol.upper(), limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ── Corporate events fetch: ONE implementation shared by the scheduled
#    "Corp Events" step (scheduled_jobs.py) and the manual button below --
#    exactly one place this logic lives, so the two paths can never
#    silently disagree about what "fetch corporate events" means. ──

_corp_events_status = {"running": False, "processed": 0, "total": 0, "fetched": 0, "skipped_no_cik": 0, "errors": 0}
_corp_events_lock = threading.Lock()


def run_corporate_events_for_symbols(symbols, progress=True):
    """Fetches insider activity, debt snapshot, volume signal, and material
    events for each symbol via SEC EDGAR, storing each as a
    corporate_events_snapshot row. Returns (fetched, skipped_no_cik, errors,
    material_insider_events) -- the last being [(symbol, net_dollars), ...]
    for large ($1M+) net insider moves, same threshold the scheduled step
    uses to fire its own notification."""
    from . import sec_edgar
    if progress:
        with _corp_events_lock:
            _corp_events_status.update({"running": True, "processed": 0, "total": len(symbols),
                                         "fetched": 0, "skipped_no_cik": 0, "errors": 0})
    fetched, skipped_no_cik, errors = 0, 0, 0
    material_insider_events = []
    for sym in symbols:
        cik = sec_edgar.get_cik(sym)
        if not cik:
            skipped_no_cik += 1  # futures/options/crypto/non-US-equity symbols correctly have no SEC filings
        else:
            try:
                insider = sec_edgar.fetch_insider_activity(sym, lookback_days=90)
                debt = sec_edgar.fetch_debt_snapshot(sym)
                volume = sec_edgar.fetch_volume_signal(sym, avg_days=20)
                events = sec_edgar.fetch_material_events(sym, lookback_days=30)
                store_corporate_events_snapshot(sym, insider=insider, debt=debt, volume=volume, events=events)
                fetched += 1
                net = insider.get("net_dollars") or 0
                if abs(net) >= 1_000_000:
                    material_insider_events.append((sym, net))
            except Exception as e:
                errors += 1
                print(f"[corporate_events] {sym} FAILED: {e}")
        if progress:
            with _corp_events_lock:
                _corp_events_status["processed"] += 1
                _corp_events_status["fetched"] = fetched
                _corp_events_status["skipped_no_cik"] = skipped_no_cik
                _corp_events_status["errors"] = errors
    if progress:
        with _corp_events_lock:
            _corp_events_status["running"] = False
    return fetched, skipped_no_cik, errors, material_insider_events


@wl_bp.route("/<int:wl_id>/run-corporate-events", methods=["POST"])
def run_corporate_events_manual(wl_id):
    """Manual trigger for the same corporate-events fetch the scheduled
    step runs -- lets you warm/refresh it on demand without waiting for
    the schedule, exactly like Earnings/Compute Indicators already work."""
    if _corp_events_status["running"]:
        return jsonify({"error": "A corporate events fetch is already running", "status": _corp_events_status}), 409
    try:
        from .earnings import _get_watchlist_symbols
        symbols = _get_watchlist_symbols(wl_id) or []
    except Exception as e:
        return jsonify({"error": f"Could not load symbols: {e}"}), 400
    if not symbols:
        return jsonify({"error": "No symbols in this watchlist"}), 400

    def _run():
        try:
            fetched, skipped, errors, material = run_corporate_events_for_symbols(symbols, progress=True)
            for sym, net in material:
                direction = "buying" if net > 0 else "selling"
                try:
                    log_alert_notification(
                        "INSIDER_ACTIVITY", f"{sym}: large net insider {direction}",
                        f"Net ${abs(net):,.0f} over the last 90 days",
                        symbol=sym, severity="ok" if net > 0 else "warn", source="Corporate Events",
                    )
                except Exception:
                    pass
        except Exception as e:
            with _corp_events_lock:
                _corp_events_status["running"] = False
            print(f"[corporate_events] manual run FAILED: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"started": True, "symbol_count": len(symbols)})


@wl_bp.route("/api/corporate-events-status")
def corporate_events_status():
    return jsonify(_corp_events_status)



@wl_bp.route("/", methods=["GET"])
def list_watchlists():
    """Return all watchlists with symbol counts and sector coverage."""
    _ensure_tables()
    con = _conn()
    rows = con.execute("""
        SELECT w.id, w.name, w.description, w.fetch_options_oi,
               COALESCE(w.is_default, 0) as is_default, w.color, w.created_at,
               COALESCE(w.last_fetch_at, '') as last_fetch_at,
               COALESCE(w.last_fetch_mode, '') as last_fetch_mode,
               COALESCE(w.last_fetch_count, 0) as last_fetch_count,
               COALESCE(w.schedule_price_oi_time, '') as schedule_price_oi_time,
               COALESCE(w.schedule_earnings_time, '') as schedule_earnings_time,
               COALESCE(w.schedule_indicators_time, '') as schedule_indicators_time,
               COALESCE(w.schedule_corporate_events_time, '') as schedule_corporate_events_time,
               COALESCE(w.schedule_price_oi_last_date, '') as schedule_price_oi_last_date,
               COALESCE(w.schedule_earnings_last_date, '') as schedule_earnings_last_date,
               COALESCE(w.schedule_indicators_last_date, '') as schedule_indicators_last_date,
               COALESCE(w.schedule_corporate_events_last_date, '') as schedule_corporate_events_last_date,
               COALESCE(w.schedule_volume_profile_time, '') as schedule_volume_profile_time,
               COALESCE(w.schedule_volume_profile_last_date, '') as schedule_volume_profile_last_date,
               COALESCE(w.schedule_intraday_price_time, '') as schedule_intraday_price_time,
               COALESCE(w.schedule_intraday_price_last_date, '') as schedule_intraday_price_last_date,
               COUNT(ws.id) as symbol_count
        FROM watchlists w
        LEFT JOIN watchlist_symbols ws ON ws.watchlist_id = w.id
        GROUP BY w.id ORDER BY w.name
    """).fetchall()
    result = [dict(r) for r in rows]
    # Add sector coverage per watchlist
    try:
        for wl in result:
            sec_rows = con.execute("""
                SELECT sc.sector, COUNT(*) cnt
                FROM watchlist_symbols ws
                JOIN sector_cache sc ON sc.symbol = ws.symbol
                WHERE ws.watchlist_id = ? AND sc.sector != ''
                GROUP BY sc.sector ORDER BY cnt DESC LIMIT 5
            """, (wl['id'],)).fetchall()
            wl['sectors'] = [{"sector": r[0], "count": r[1]} for r in sec_rows]
            # Count how many symbols have sector data
            covered = con.execute("""
                SELECT COUNT(DISTINCT ws.symbol) FROM watchlist_symbols ws
                JOIN sector_cache sc ON sc.symbol=ws.symbol
                WHERE ws.watchlist_id=? AND sc.sector!=''
            """, (wl['id'],)).fetchone()[0]
            wl['sector_coverage'] = covered
    except: pass
    con.close()
    return jsonify({"watchlists": result})


@wl_bp.route("/", methods=["POST"])
def create_watchlist():
    """Create a new watchlist. Body: {name, description, fetch_options_oi, color}"""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    name = (d.get("name","") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    desc    = d.get("description","")
    fetch   = 1 if d.get("fetch_options_oi") else 0
    color   = d.get("color","#818cf8")
    con = _conn()
    try:
        con.execute("INSERT INTO watchlists (name, description, fetch_options_oi, color) VALUES (?,?,?,?)",
                    (name, desc, fetch, color))
        wl_id = con.execute("SELECT id FROM watchlists WHERE name=?", (name,)).fetchone()[0]
        con.commit(); con.close()
        return jsonify({"ok": True, "id": wl_id, "name": name})
    except sqlite3.IntegrityError:
        con.close()
        return jsonify({"error": f"Watchlist '{name}' already exists"}), 409


@wl_bp.route("/future-oi-schedule", methods=["GET"])
def get_future_oi_schedule():
    """Global (not per-watchlist) time-of-day for the futures OI fetch,
    read by _run_future_oi_if_due() in scheduled_jobs.py. Blank/unset
    means it never runs on a schedule -- same "no time = no execution"
    rule as the per-watchlist steps."""
    return jsonify({"time": _get_setting("future_oi_schedule_time", "") or ""})


@wl_bp.route("/future-oi-schedule", methods=["PUT"])
def set_future_oi_schedule():
    d = request.get_json(force=True) or {}
    _set_setting("future_oi_schedule_time", (d.get("time") or "").strip())
    return jsonify({"ok": True, "time": (d.get("time") or "").strip()})


@wl_bp.route("/<int:wl_id>", methods=["PUT"])
def update_watchlist(wl_id):
    """Update watchlist metadata."""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    con = _conn()
    fields = []
    vals   = []
    for col in ["name","description","fetch_options_oi","color",
                "schedule_price_oi_time","schedule_earnings_time","schedule_indicators_time",
                "schedule_corporate_events_time","schedule_volume_profile_time",
                "schedule_intraday_price_time"]:
        if col in d:
            fields.append(f"{col}=?")
            vals.append(1 if (col=="fetch_options_oi" and d[col]) else (d[col] or None))
    if not fields:
        return jsonify({"error": "nothing to update"}), 400
    con.execute(f"UPDATE watchlists SET {','.join(fields)} WHERE id=?", vals + [wl_id])
    # Sync symbols table if Options Watchlist OI flag changed
    if "fetch_options_oi" in d:
        _sync_symbols_table(con)
    con.commit(); con.close()
    return jsonify({"ok": True})


@wl_bp.route("/<int:wl_id>", methods=["DELETE"])
def delete_watchlist(wl_id):
    """Delete a watchlist and all its symbols."""
    _ensure_tables()
    con = _conn()
    # Prevent deleting the last watchlist
    cnt = con.execute("SELECT COUNT(*) FROM watchlists").fetchone()[0]
    if cnt <= 1:
        con.close()
        return jsonify({"error": "Cannot delete the only watchlist"}), 400
    con.execute("DELETE FROM watchlist_symbols WHERE watchlist_id=?", (wl_id,))
    con.execute("DELETE FROM watchlists WHERE id=?", (wl_id,))
    con.commit(); con.close()
    return jsonify({"ok": True})


@wl_bp.route("/<int:wl_id>/symbols", methods=["GET"])
def get_symbols(wl_id):
    """Return symbols in a watchlist. Live prices are fetched by default
    (used by the watchlist price table), but that's a synchronous,
    per-symbol network fetch -- for a few hundred symbols, even a couple
    of delisted/invalid tickers among them (each paying the full cost of
    get_spot_snapshot()'s fallback chain) can turn this into a multi-
    minute hang. Callers that only need the symbol list itself (e.g. the
    edit-watchlist form, which just populates a textarea with names) pass
    ?prices=0 to skip this entirely.
    """
    _ensure_tables()
    include_prices = (request.args.get("prices", "1") or "1").strip() not in ("0", "false", "no")
    con = _conn()
    rows = con.execute(
        "SELECT symbol, added_at, alert_price, alert_direction, alert_enabled, alert_last_price, alert_last_side, alert_last_sent_at FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
        (wl_id,)
    ).fetchall()
    items = [dict(r) for r in rows]
    con.close()
    live = _fetch_live_prices([r['symbol'] for r in items]) if (items and include_prices) else {}
    for r in items:
        r['current_price'] = live.get(r['symbol'])
    return jsonify({"symbols": items, "count": len(items)})


def get_alert_watchlist_rows():
    """Return enabled alert rows for Telegram watcher."""
    _ensure_tables()
    con = _conn()
    rows = con.execute("""
        SELECT ws.watchlist_id, w.name AS watchlist_name, ws.symbol,
               ws.alert_price, ws.alert_direction, ws.alert_enabled,
               ws.alert_last_price, ws.alert_last_side, ws.alert_last_sent_at,
               ws.added_at
        FROM watchlist_symbols ws
        JOIN watchlists w ON w.id = ws.watchlist_id
        WHERE COALESCE(ws.alert_enabled, 0) = 1
          AND ws.alert_price IS NOT NULL
        ORDER BY w.name, ws.symbol
    """).fetchall()
    con.close()
    return [dict(r) for r in rows]


@wl_bp.route("/alerts/open", methods=["GET"])
def alerts_open():
    """Return all enabled symbol alerts across watchlists for the alerts hub."""
    rows = get_alert_watchlist_rows()
    return jsonify({"alerts": rows, "count": len(rows)})


@wl_bp.route("/alerts/history", methods=["GET"])
def alerts_history():
    """Return recent alert notifications for the bell and alerts hub."""
    _ensure_tables()
    try:
        limit = int(request.args.get('limit', 100))
    except Exception:
        limit = 100
    limit = max(1, min(limit, 500))
    con = _conn()
    try:
        rows = con.execute(
            """
            SELECT id, alert_type, title, detail, source, severity, symbol, trade_id, scanner_name, metadata, created_at
            FROM alert_notifications
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return jsonify({"alerts": [dict(r) for r in rows], "count": len(rows)})


@wl_bp.route("/<int:wl_id>/symbols/<sym>/alert", methods=["POST", "DELETE"])
def set_symbol_alert(wl_id, sym):
    """Set or clear alert price/direction for one symbol in a watchlist."""
    _ensure_tables()
    if request.method == "DELETE":
        con = _conn()
        try:
            cur = con.execute("""
                UPDATE watchlist_symbols
                SET alert_price=NULL, alert_enabled=0, alert_last_price=NULL, alert_last_side=NULL, alert_last_sent_at=NULL
                WHERE watchlist_id=? AND UPPER(symbol)=UPPER(?)
            """, (wl_id, sym))
            con.commit()
            return jsonify({"ok": True, "deleted": cur.rowcount, "watchlist_id": wl_id, "symbol": sym.upper(),
                            "alert_price": None, "alert_direction": "both", "alert_enabled": 0})
        finally:
            con.close()

    d = request.get_json(force=True) or {}
    try:
        alert_price = float(d.get("alert_price")) if d.get("alert_price") not in (None, "") else None
    except Exception:
        return jsonify({"error": "alert_price must be numeric"}), 400
    direction = (d.get("alert_direction") or "both").strip().lower()
    if direction not in {"above", "below", "both"}:
        return jsonify({"error": "alert_direction must be above, below, or both"}), 400
    enabled = 1 if d.get("alert_enabled", True) and alert_price is not None else 0
    con = _conn()
    try:
        cur = con.execute("""
            UPDATE watchlist_symbols
            SET alert_price=?, alert_direction=?, alert_enabled=?
            WHERE watchlist_id=? AND UPPER(symbol)=UPPER(?)
        """, (alert_price, direction, enabled, wl_id, sym))
        con.commit()
        return jsonify({"ok": True, "updated": cur.rowcount, "watchlist_id": wl_id, "symbol": sym.upper(),
                        "alert_price": alert_price, "alert_direction": direction, "alert_enabled": enabled})
    finally:
        con.close()


@wl_bp.route('/alerts/rules/<int:rule_id>/test', methods=['POST', 'GET'])
def alerts_rules_test(rule_id):
    """Dry-run a rule and send any matching rows to Telegram."""
    _ensure_alert_rules_table()
    con = _conn()
    try:
        row = con.execute("SELECT * FROM alert_rules WHERE id=?", (rule_id,)).fetchone()
        if not row:
            return jsonify({'error': 'rule not found'}), 404
        rule = dict(row)
    finally:
        con.close()

    try:
        from .scanner_builder import _parse_query, _expand_scan_nodes, _required_timeframes, _scan_symbol, _eval
    except Exception as e:
        return jsonify({'error': f'alert evaluator unavailable: {e}'}), 500

    kind = (rule.get('alert_kind') or 'price').strip().lower()
    benchmark = (rule.get('benchmark') or 'SPY').strip().upper() or 'SPY'
    rule_symbols = _alert_rule_symbols(rule)
    results = []

    if kind == 'price':
        try:
            from ..services.market import get_spot as _get_spot
        except Exception:
            _get_spot = None
        op = rule.get('price_operator') or '>='
        threshold = rule.get('price_value')
        if threshold is None:
            return jsonify({'error': 'price_value is missing'}), 400
        for sym in rule_symbols[:200]:
            try:
                px = _get_spot(sym) if _get_spot is not None else None
            except Exception:
                px = None
            if px is None:
                continue
            results.append({'symbol': sym, 'spot': px, 'matched': bool(_compare_price(px, op, threshold))})
        matched = [r['symbol'] for r in results if r['matched']]
        telegram = _send_alert_test_telegram(rule, kind, matched, results, rule_id=rule_id)
        status = 'ok' if matched else 'no_match'
        message = f"{len(matched)} match(es)" if matched else 'No matches'
        if telegram.get('error'):
            message = f"{message} · Telegram: {telegram.get('error')}"
        return jsonify({'ok': True, 'status': status, 'message': message, 'rule_id': rule_id, 'kind': kind, 'results': results, 'matched': matched, 'telegram': telegram})

    expr = (rule.get('condition_text') or '').strip()
    if not expr:
        return jsonify({'error': 'condition_text is missing'}), 400
    try:
        raw = _parse_query(expr)
        root = _expand_scan_nodes(raw, ())
        rule_tf = (rule.get('timeframe') or '1d').strip() or '1d'
        req_tfs = list(dict.fromkeys((_required_timeframes(root) or []) + [rule_tf]))
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    for sym in rule_symbols[:200]:
        try:
            ctx, err = _scan_symbol(sym, root, benchmark, req_tfs)
            if not ctx:
                results.append({'symbol': sym, 'matched': False, 'error': err or 'no context'})
                continue
            ok = bool(_eval(root, ctx, shift=0, tf_default=rule_tf))
            results.append({'symbol': sym, 'matched': ok, 'error': None})
        except Exception as e:
            results.append({'symbol': sym, 'matched': False, 'error': str(e)})
    matched = [r['symbol'] for r in results if r.get('matched')]
    telegram = _send_alert_test_telegram(rule, kind, matched, results, rule_id=rule_id)
    status = 'ok' if matched else 'no_match'
    message = f"{len(matched)} match(es)" if matched else 'No matches'
    if telegram.get('error'):
        message = f"{message} · Telegram: {telegram.get('error')}"
    return jsonify({'ok': True, 'status': status, 'message': message, 'rule_id': rule_id, 'kind': kind, 'results': results, 'matched': matched, 'telegram': telegram})


@wl_bp.route('/alerts/status', methods=['GET'])
def alerts_status():
    """Return watcher status and the last scheduler run summary."""
    return jsonify({
        'watcher_running': bool(_alert_rule_watcher_started),
        'last_run_at': _alert_rule_last_run_at,
        'last_result': _alert_rule_last_result,
    })



@wl_bp.route("/import_scan_results", methods=["POST"])
def import_scan_results():
    """Create a new watchlist or append to an existing one from scanner results."""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    rejected = []
    if d.get("symbols"):
        symbols, rejected = _parse_watchlist_symbols(d.get("symbols"))
    else:
        symbols = []
        for row in d.get("results") or []:
            sym = _normalize_watchlist_symbol((row or {}).get("symbol", ""))
            if sym:
                symbols.append(sym)
        symbols = sorted(set(symbols))
    if not symbols:
        msg = "No symbols to import"
        if rejected:
            msg += f"; rejected: {', '.join(rejected[:10])}"
        return jsonify({"error": msg, "rejected": rejected}), 400

    append_wl_id = d.get("watchlist_id") or d.get("append_watchlist_id")
    source = (d.get("source") or "scan").strip()
    description = (d.get("description") or f"Imported from {source}").strip()
    fetch_oi = 1 if d.get("fetch_options_oi") else 0
    color = d.get("color") or "#818cf8"

    con = _conn()
    try:
        if append_wl_id:
            wl_id = int(append_wl_id)
            wl = con.execute("SELECT id, name FROM watchlists WHERE id=?", (wl_id,)).fetchone()
            if not wl:
                return jsonify({"error": "Watchlist not found"}), 404
        else:
            name = (d.get("name") or f"{source.title()} Watchlist").strip()
            base_name = name
            i = 2
            while con.execute("SELECT 1 FROM watchlists WHERE name=?", (name,)).fetchone():
                name = f"{base_name} ({i})"
                i += 1
            con.execute("INSERT INTO watchlists (name, description, fetch_options_oi, color) VALUES (?,?,?,?)",
                        (name, description, fetch_oi, color))
            wl_id = con.execute("SELECT id FROM watchlists WHERE name=?", (name,)).fetchone()[0]

        added = 0
        for sym in symbols:
            before = con.total_changes
            con.execute("INSERT OR IGNORE INTO watchlist_symbols (watchlist_id, symbol) VALUES (?,?)", (wl_id, sym))
            if con.total_changes > before:
                added += 1
        _sync_symbols_table(con)
        con.commit()
        wl_name = con.execute("SELECT name FROM watchlists WHERE id=?", (wl_id,)).fetchone()[0]
        return jsonify({"ok": True, "watchlist_id": wl_id, "watchlist_name": wl_name, "added": added, "total": len(symbols), "rejected": rejected})
    finally:
        con.close()


@wl_bp.route("/<int:wl_id>/symbols", methods=["POST"])
def add_symbols(wl_id):
    """Add symbols to a watchlist. Body: {symbols: ['AAPL','MSFT',...]}"""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    symbols, rejected = _parse_watchlist_symbols(d.get("symbols"))
    if not symbols:
        return jsonify({"error": "no symbols provided", "rejected": rejected}), 400
    con = _conn()
    added = 0
    for sym in symbols:
        try:
            before = con.total_changes
            con.execute("INSERT OR IGNORE INTO watchlist_symbols (watchlist_id, symbol) VALUES (?,?)",
                        (wl_id, sym))
            if con.total_changes > before:
                added += 1
        except: pass
    _sync_symbols_table(con)
    con.commit(); con.close()
    return jsonify({"ok": True, "added": added, "total": len(symbols), "rejected": rejected})


@wl_bp.route("/<int:wl_id>/symbols/<sym>", methods=["DELETE"])
def remove_symbol(wl_id, sym):
    """Remove a symbol from a watchlist."""
    _ensure_tables()
    con = _conn()
    con.execute("DELETE FROM watchlist_symbols WHERE watchlist_id=? AND symbol=?",
                (wl_id, _normalize_watchlist_symbol(sym)))
    _sync_symbols_table(con)
    con.commit(); con.close()
    return jsonify({"ok": True})


@wl_bp.route("/<int:wl_id>/symbols/bulk_delete", methods=["POST"])
def bulk_remove_symbols(wl_id):
    """Remove multiple symbols. Body: {symbols: ['AAPL',...]}"""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    symbols, rejected = _parse_watchlist_symbols(d.get("symbols"))
    con = _conn()
    for sym in symbols:
        con.execute("DELETE FROM watchlist_symbols WHERE watchlist_id=? AND symbol=?",
                    (wl_id, sym))
    _sync_symbols_table(con)
    con.commit(); con.close()
    return jsonify({"ok": True, "removed": len(symbols), "rejected": rejected})


@wl_bp.route("/<int:wl_id>/symbols/replace", methods=["POST"])
def replace_symbols(wl_id):
    """Replace all symbols in a watchlist. Body: {symbols: ['AAPL','MSFT',...]}"""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    symbols, rejected = _parse_watchlist_symbols(d.get("symbols"))
    con = _conn()
    con.execute("DELETE FROM watchlist_symbols WHERE watchlist_id=?", (wl_id,))
    for sym in symbols:
        con.execute("INSERT OR IGNORE INTO watchlist_symbols (watchlist_id, symbol) VALUES (?,?)",
                    (wl_id, sym))
    _sync_symbols_table(con)
    con.commit(); con.close()
    return jsonify({"ok": True, "count": len(symbols), "rejected": rejected})


def _sync_symbols_table(con):
    """
    Keep the `symbols` table in sync with the DEFAULT watchlist.
    All existing scanners (regime, earnings, OI buildup, RSI MTF, etc.) read from
    `symbols`, so setting a watchlist as default automatically changes what they scan.
    """
    try:
        # Find the default watchlist
        row = con.execute("SELECT id FROM watchlists WHERE is_default=1 LIMIT 1").fetchone()
        if not row:
            row = con.execute("SELECT id FROM watchlists ORDER BY id LIMIT 1").fetchone()
        if not row:
            return
        default_wl_id = row[0]
        con.execute("DELETE FROM symbols")
        con.execute("""
            INSERT OR IGNORE INTO symbols (symbol)
            SELECT symbol FROM watchlist_symbols
            WHERE watchlist_id = ?
            ORDER BY symbol
        """, (default_wl_id,))
        print(f"[wl] symbols synced from watchlist #{default_wl_id}")
    except Exception as e:
        print(f"[wl] symbols sync error: {e}")


@wl_bp.route("/<int:wl_id>/set_default", methods=["POST"])
def set_default(wl_id):
    """Set this watchlist as the default for scanners."""
    _ensure_tables()
    con = _conn()
    try: con.execute("ALTER TABLE watchlists ADD COLUMN is_default INTEGER DEFAULT 0")
    except: pass
    try: con.execute("ALTER TABLE watchlists ADD COLUMN last_fetch_at TEXT")
    except: pass
    try: con.execute("ALTER TABLE watchlists ADD COLUMN last_fetch_mode TEXT")
    except: pass
    try: con.execute("ALTER TABLE watchlists ADD COLUMN last_fetch_count INTEGER DEFAULT 0")
    except: pass
    con.execute("UPDATE watchlists SET is_default=0")
    con.execute("UPDATE watchlists SET is_default=1 WHERE id=?", (wl_id,))
    _sync_symbols_table(con)   # update symbols table so all scanners use new default
    con.commit(); con.close()
    return jsonify({"ok": True})


def get_default_watchlist_id():
    """Return the id of the default watchlist, or None."""
    try:
        _ensure_tables()
        con = _conn()
        row = con.execute("SELECT id FROM watchlists WHERE is_default=1 LIMIT 1").fetchone()
        if not row:
            row = con.execute("SELECT id FROM watchlists ORDER BY id LIMIT 1").fetchone()
        con.close()
        return row[0] if row else None
    except:
        return None


# ── Per-watchlist action routes ─────────────────────────────────────────────

@wl_bp.route("/<int:wl_id>/fetch_history", methods=["GET"])
def watchlist_fetch_history(wl_id):
    """When each action type (Fetch Price, Fetch OI, Backfill History,
    Backfill Intraday) last ran for this watchlist -- these used to
    share one 'Last Run' column that whichever action ran most recently
    overwrote, so running Fetch OI would hide when Fetch Price last
    completed, and vice versa. Each action has its own tracked history
    now (same job-log system used everywhere else in the app)."""
    from ..services.job_registry import get_run_history
    actions = {
        "fetch_price": f"wl_fetch_price_{wl_id}",
        "fetch_oi": f"wl_fetch_oi_{wl_id}",
        "backfill_history": f"wl_backfill_history_{wl_id}",
        "backfill_intraday": f"wl_backfill_intraday_{wl_id}",
        "fetch_intraday_price": f"wl_fetch_intraday_price_{wl_id}",
    }
    out = {}
    for label, key in actions.items():
        hist = get_run_history(key, limit=1)
        out[label] = hist[0] if hist else None
    return jsonify({"ok": True, "watchlist_id": wl_id, "actions": out})


# Last OI-fetch result per watchlist, keyed by wl_id -- previously the
# per-symbol error count (_oi_stats below) was local to one function
# call and discarded the second it returned, with the actual exception
# message never captured anywhere at all (just a silent counter
# increment). watchlist_fetch_status() only ever reported successfully-
# fetched row counts, never error counts -- so if every symbol in a
# fetch was failing (rate limit, yfinance API change, network issue),
# the frontend's polling loop just saw "0 fetched" indefinitely with
# zero indication why, until it gave up after 5 minutes. That's the
# exact "silently failed, no status shown" symptom this fixes.
_last_oi_fetch_result: dict = {}


def _fetch_data_for_watchlist_core(wl_id, source="manual", remote_addr="?", user_agent="?", referer="?", blocking=False, oi_source="yfinance"):
    """The actual fetch logic, extracted from the fetch_data_for_watchlist
    Flask route below so it can ALSO be called from the new per-watchlist
    scheduler (_watchlist_schedule_loop) without needing a Flask request
    context -- flask.request access outside of an active request raises
    RuntimeError, which is exactly why this couldn't just be called
    directly from a background scheduler thread before this refactor.
    Returns (status_code, payload_dict) instead of a Flask response, so
    the route below can just `return jsonify(payload), status` and the
    scheduler can just inspect the dict directly.
    """
    import threading, datetime as _dt
    # This exact line is the answer to "why did this fire, I didn't click
    # it" -- every previous investigation of that question required
    # reasoning backward from code paths with no way to actually confirm
    # which one fired. This makes it provable from the log going forward:
    # real user clicks show a browser user-agent and a referring page;
    # anything else (a stray retried request, a script, a different
    # trigger entirely) will look visibly different here.
    print(f"[watchlist_manager] fetch_data triggered for watchlist_id={wl_id} source={source} "
          f"from {remote_addr} | UA: {user_agent} | Referer: {referer}")
    with _fetch_in_progress_lock:
        if wl_id in _fetch_in_progress:
            return 409, {
                "ok": False,
                "error": "A fetch is already running for this watchlist -- wait for it to finish "
                         "(check the 'Last Run' column) instead of starting another one.",
            }
        _fetch_in_progress.add(wl_id)

    _ensure_tables()
    con = _conn()
    wl = con.execute(
        "SELECT name, fetch_options_oi FROM watchlists WHERE id=?", (wl_id,)
    ).fetchone()
    if not wl:
        con.close()
        with _fetch_in_progress_lock:
            _fetch_in_progress.discard(wl_id)
        return 404, {"error": "Watchlist not found"}
    wl_name, fetch_oi = wl[0], bool(wl[1])
    syms = [r[0] for r in con.execute(
        "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol", (wl_id,)
    ).fetchall()]
    con.close()

    if not syms:
        with _fetch_in_progress_lock:
            _fetch_in_progress.discard(wl_id)
        return 400, {"error": "No symbols in this watchlist"}

    import time as _time
    def _run():
        if fetch_oi:
            _oi_stats = {"skipped_cache": 0, "no_options": 0, "fetched": 0, "errored": 0, "sample_errors": [], "timed_out": 0}
            if oi_source == "tastytrade":
                # Off-hours-only in practice -- each symbol's full 2-month
                # chain takes ~20s via DXLink (see tastytrade_options_backfill.py's
                # own docstring for why), and TastytradeFeed uses ONE shared,
                # persistent event loop for every call (confirmed directly --
                # _run_coro submits onto a single background thread's loop via
                # run_coroutine_threadsafe, not genuinely independent per-thread
                # work), so concurrency here is intentionally conservative -- a
                # handful at once, not the shared 8-worker pool's full blast,
                # to avoid overloading that one shared session/loop. Timeout is
                # deliberately generous (not the yfinance path's 20-minute cap)
                # since this is explicitly meant to run long, unattended,
                # off-hours -- the goal here is "don't time out", not "finish fast".
                from ..services.tastytrade_options_backfill import fetch_and_store_symbol
                from concurrent.futures import ThreadPoolExecutor
                from ..services.bounded_wait import bounded_as_completed

                # DXLink Summary snapshots are connection-sensitive. Parallel
                # stream sessions on the same authenticated Tastytrade session
                # caused complete per-symbol OI drops (0/N summaries received).
                # Keep this serial; one symbol still batches all its expiries.
                TASTYTRADE_CONCURRENCY = 1

                def _fetch_oi_one_tastytrade(sym):
                    try:
                        r = fetch_and_store_symbol(sym)
                        if r.get("ok"):
                            _oi_stats["fetched"] += 1
                        else:
                            _oi_stats["errored"] += 1
                            if len(_oi_stats["sample_errors"]) < 5:
                                _oi_stats["sample_errors"].append(f"{sym}: {r.get('error')}")
                    except Exception as e:
                        _oi_stats["errored"] += 1
                        if len(_oi_stats["sample_errors"]) < 5:
                            _oi_stats["sample_errors"].append(f"{sym}: {type(e).__name__}: {e}")

                tt_ex = ThreadPoolExecutor(max_workers=TASTYTRADE_CONCURRENCY, thread_name_prefix="oiapp-tt-oi-fetch")
                try:
                    tt_futs = {tt_ex.submit(_fetch_oi_one_tastytrade, sym): sym for sym in syms}
                    # ~20s/symbol at TASTYTRADE_CONCURRENCY-way parallelism,
                    # plus generous margin -- meant to comfortably exceed what
                    # a full watchlist actually needs under normal conditions.
                    tt_timeout = max(1800, int(len(syms) / TASTYTRADE_CONCURRENCY * 30))
                    def _on_tt_timeout(ks):
                        _oi_stats["timed_out"] = len(ks)
                        print(f"[watchlist_manager] tastytrade OI fetch: {len(ks)} symbol(s) didn't finish in time: {ks[:20]}")
                    for fut, sym in bounded_as_completed(tt_futs, timeout=tt_timeout, on_timeout=_on_tt_timeout):
                        pass
                finally:
                    tt_ex.shutdown(wait=False)

                print(f"[watchlist_manager] tastytrade OI fetch for '{wl_name}' done: "
                      f"{_oi_stats['fetched']} fetched, {_oi_stats['errored']} errored (out of {len(syms)} symbols)")
                _last_oi_fetch_result[wl_id] = dict(_oi_stats, total=len(syms))
                log_alert_notification(
                    "WATCHLIST_FETCH", f"'{wl_name}': tastytrade OI fetch completed",
                    f"{_oi_stats['fetched']}/{len(syms)} fetched, {_oi_stats['errored']} errored, "
                    f"{_oi_stats.get('timed_out', 0)} still finishing in background",
                    source="scheduler" if source == "scheduler" else "manual",
                    severity="error" if _oi_stats["errored"] > 0 else "info",
                )
            else:
                from ..services.market import get_expirations, fetch_store_for, is_recently_failed, mark_fetch_failed
                from ..services.task_executor import get_background_executor

                def _fetch_oi_one(sym):
                    # This is a MANUAL, explicit button click -- the user is
                    # asking for a fresh attempt right now, so it deliberately
                    # does NOT respect the negative cache the way automatic/
                    # scheduled fetches do. The cache is an in-memory dict
                    # with zero connection to the database: if an earlier run
                    # hit a burst of failures (e.g. a rate-limit event from
                    # too many concurrent requests) and cached a batch of
                    # symbols as "recently failed", deleting DB rows does
                    # nothing to clear that -- a manual rerun would otherwise
                    # silently skip every one of them and write nothing,
                    # which looks exactly like "stuck" from the outside.
                    try:
                        exps = get_expirations(sym)[:10]
                        if exps:
                            fetch_store_for(sym, exps)
                            _oi_stats["fetched"] += 1
                        else:
                            mark_fetch_failed(sym)
                            _oi_stats["no_options"] += 1
                    except Exception as e:
                        mark_fetch_failed(sym)
                        _oi_stats["errored"] += 1
                        # Keep a small sample, not every message -- enough to
                        # show WHY things are failing (rate limit vs network
                        # vs something else) without spamming if hundreds of
                        # symbols fail the same way.
                        if len(_oi_stats["sample_errors"]) < 5:
                            _oi_stats["sample_errors"].append(f"{sym}: {type(e).__name__}: {e}")

                # Parallelized through the shared pool instead of a sequential
                # for-loop with a 0.3s sleep between every symbol (198 symbols
                # = ~60s of sleeping alone, before any actual network time).
                # Uses submit() + bounded_as_completed (not .map()) because
                # this is the app-wide SHARED executor -- never call
                # shutdown() on it.
                ex = get_background_executor()
                futs = {ex.submit(_fetch_oi_one, sym): sym for sym in syms}
                from ..services.bounded_wait import bounded_as_completed
                # Was a flat 180s regardless of watchlist size -- with only
                # 8 concurrent workers on the shared background pool (see
                # get_background_executor()'s own _BG_MAX_WORKERS) and each
                # symbol fetching up to 10 expirations (get_expirations(sym)
                # [:10]), each a separate yfinance call, 180s was nowhere
                # close to enough for a ~200-symbol watchlist -- confirmed
                # directly from a real run's logs: 128/199 finished, 71 still
                # mid-flight when the wait gave up (not failed -- the
                # underlying fetches keep running regardless, this timeout
                # only controls how long THIS function waits before moving
                # on, so the previous fixed value was making completed work
                # look like a stall for no real reason). 3s/symbol as a
                # simple, size-scaling floor, generous rather than tight,
                # capped at 20 minutes so a truly pathological case still
                # can't hang this request forever.
                fetch_timeout = min(1200, max(180, len(syms) * 3))
                def _on_timeout(ks):
                    # Distinct from "errored" -- these symbols' fetches are
                    # still running on the shared pool, not failed, just not
                    # waited-for any further by this request. Tracked
                    # separately so the status endpoint can say "still in
                    # progress in the background" rather than lumping them in
                    # with genuine failures or silently dropping the count.
                    _oi_stats["timed_out"] = len(ks)
                    print(f"[watchlist_manager] OI fetch: {len(ks)} symbol(s) didn't finish in time: {ks[:20]}")
                for fut, sym in bounded_as_completed(futs, timeout=fetch_timeout, on_timeout=_on_timeout):
                    pass  # _fetch_oi_one writes its own results; nothing to collect here
                print(f"[watchlist_manager] OI fetch for '{wl_name}' done: "
                      f"{_oi_stats['fetched']} fetched, {_oi_stats['no_options']} had no options chain, "
                      f"{_oi_stats['errored']} errored (out of {len(syms)} symbols)")
                _last_oi_fetch_result[wl_id] = dict(_oi_stats, total=len(syms))
                log_alert_notification(
                    "WATCHLIST_FETCH", f"'{wl_name}': yfinance OI fetch completed",
                    f"{_oi_stats['fetched']}/{len(syms)} fetched, {_oi_stats['no_options']} had no options chain, "
                    f"{_oi_stats['errored']} errored, {_oi_stats.get('timed_out', 0)} still finishing in background",
                    source="scheduler" if source == "scheduler" else "manual",
                    severity="error" if _oi_stats["errored"] > 0 else "info",
                )
                # Tied to this same watchlist OI fetch, not a separate manual
                # step -- whenever this watchlist's yfinance-based OI fetch
                # runs (manually or via the scheduled per-watchlist loop),
                # the same symbol list gets seeded into the tastytrade
                # backfill queue too, so real Greeks/OI stay in sync with
                # whatever this app already considers "this watchlist's
                # symbols" rather than needing a separate manual trigger.
                # enqueue_watchlist() is a fast, local DB-insert-only call
                # (INSERT OR IGNORE against an existing queue row) -- no
                # tastytrade network call happens here, the actual fetch
                # stays on the throttled background job's own schedule.
                try:
                    from ..services.tastytrade_options_backfill import enqueue_watchlist as _tt_enqueue
                    _tt_result = _tt_enqueue(syms)
                    print(f"[watchlist_manager] tastytrade backfill queue: "
                          f"{_tt_result.get('newly_queued', 0)} newly queued (of {len(syms)} symbols in this watchlist)")
                except Exception as e:
                    print(f"[watchlist_manager] tastytrade backfill enqueue skipped: {e}")
        else:
            import sqlite3 as sq
            from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
            from ..services.market import mark_fetch_failed
            from ..services.task_executor import get_background_executor
            db = _OIAPP_DB_PATH
            today = _dt.date.today().isoformat()
            _price_stats = {"fetched": 0, "empty": 0, "errored": 0}

            def _fetch_one(sym):
                # Same reasoning as _fetch_oi_one above: a manual button
                # click always attempts every symbol, regardless of
                # whether it's cached as recently-failed from an earlier
                # run.
                import yfinance as yf
                try:
                    hist = yf.Ticker(sym).history(period="2d")
                except Exception:
                    mark_fetch_failed(sym)
                    _price_stats["errored"] += 1
                    return
                if hist is None or hist.empty:
                    mark_fetch_failed(sym)
                    _price_stats["empty"] += 1
                    return
                r = hist.iloc[-1]
                def _f(v, default=None):
                    try:
                        x = float(v)
                        return x if x == x and abs(x) != float('inf') else default
                    except Exception:
                        return default
                def _i(v, default=0):
                    x = _f(v, None)
                    return int(x) if x is not None else default
                o = _f(r.get("Open")); h = _f(r.get("High")); lo = _f(r.get("Low")); cl = _f(r.get("Close"))
                if cl is None:
                    return
                try:
                    c = sq.connect(db)
                    c.execute("""CREATE TABLE IF NOT EXISTS price_cache (
                        symbol TEXT NOT NULL, date TEXT NOT NULL,
                        open REAL, high REAL, low REAL, close REAL, volume INTEGER,
                        PRIMARY KEY (symbol, date))""")
                    c.execute("INSERT OR REPLACE INTO price_cache VALUES (?,?,?,?,?,?,?)",
                              (sym, __import__("pandas").Timestamp(r.name).strftime("%Y-%m-%d"),
                               round(o if o is not None else cl, 4), round(h if h is not None else cl, 4),
                               round(lo if lo is not None else cl, 4), round(cl, 4), _i(r.get("Volume"))))
                    c.commit(); c.close()
                    _price_stats["fetched"] += 1
                except Exception:
                    pass

            # Bounded concurrency via the shared pool instead of one
            # symbol at a time -- for a few hundred symbols, sequential
            # fetching means the slowest handful of symbols (delisted
            # tickers hitting network timeouts) each add their full delay
            # to the total, one after another. Uses submit() +
            # bounded_as_completed (not .map()) because this is the
            # app-wide SHARED executor -- never call shutdown() on it,
            # and .map() with no timeout is exactly the unbounded-wait
            # pattern fixed everywhere else in this app.
            ex = get_background_executor()
            futs = {ex.submit(_fetch_one, sym): sym for sym in syms}
            from ..services.bounded_wait import bounded_as_completed
            for fut, sym in bounded_as_completed(futs, timeout=120,
                    on_timeout=lambda ks: print(f"[watchlist_manager] price fetch: {len(ks)} symbol(s) "
                                                 f"didn't finish in time: {ks[:20]}")):
                pass
            print(f"[watchlist_manager] price fetch for '{wl_name}' done: "
                  f"{_price_stats['fetched']} fetched, {_price_stats['empty']} empty, "
                  f"{_price_stats['errored']} errored (out of {len(syms)} symbols)")
            log_alert_notification(
                "WATCHLIST_FETCH", f"'{wl_name}': price fetch completed",
                f"{_price_stats['fetched']}/{len(syms)} fetched, {_price_stats['empty']} empty, "
                f"{_price_stats['errored']} errored",
                source="scheduler" if source == "scheduler" else "manual",
                severity="error" if _price_stats["errored"] > 0 else "info",
            )

    table = "options" if fetch_oi else "price_cache"
    mode  = "Options OI" if fetch_oi else "Price/Volume"
    # Per-action-type history: "Fetch Price" and "Fetch OI" on the same
    # watchlist used to share one "Last Run" column that whichever ran
    # most recently overwrote -- so if you ran Fetch OI, you'd lose any
    # visibility into when Fetch Price last completed, and vice versa.
    # This gives each its own trackable run history, same job-log system
    # used everywhere else in the app.
    action_key = f"wl_fetch_{'oi' if fetch_oi else 'price'}_{wl_id}"

    def _run_and_log():
        from ..services.job_registry import log_run_start, log_run_finish
        # log_run_start() previously ran BEFORE this try block -- if it
        # failed (confirmed happening in production: sqlite3.OperationalError
        # "database is locked", likely from concurrent load elsewhere in
        # the app), the whole thread crashed before reaching the finally
        # below, which is the ONLY thing that removes wl_id from
        # _fetch_in_progress. That set is a hard gate (line ~1193: "if
        # wl_id in _fetch_in_progress: reject this request") -- so a
        # single transient lock error permanently blocked every future
        # fetch attempt for that watchlist until the app was restarted.
        # Moved inside try/finally and made non-fatal: a logging failure
        # should never be able to block the actual fetch it's trying to
        # log, let alone block every future one too.
        run_id = None
        try:
            try:
                run_id = log_run_start(action_key)
            except Exception as e:
                print(f"[watchlist_manager] log_run_start failed (non-fatal, fetch proceeds anyway): {e}")
            _run()
            if run_id is not None:
                try:
                    log_run_finish(run_id, True, f"{mode}: {len(syms)} symbols")
                except Exception as e:
                    print(f"[watchlist_manager] log_run_finish failed (non-fatal): {e}")
            # Update last_fetch info
            try:
                import sqlite3 as _sq, datetime as _dtt
                from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
                _db = _OIAPP_DB_PATH
                _c  = _sq.connect(_db)
                _c.execute("""UPDATE watchlists SET last_fetch_at=?, last_fetch_mode=?, last_fetch_count=?
                              WHERE id=?""",
                           (_dtt.datetime.now().strftime("%Y-%m-%d %H:%M"), mode, len(syms), wl_id))
                _c.commit(); _c.close()
            except: pass
        except Exception as e:
            if run_id is not None:
                try:
                    log_run_finish(run_id, False, str(e))
                except Exception as e2:
                    print(f"[watchlist_manager] log_run_finish failed (non-fatal): {e2}")
            else:
                print(f"[watchlist_manager] fetch for wl_id={wl_id} failed: {e}")
        finally:
            with _fetch_in_progress_lock:
                _fetch_in_progress.discard(wl_id)

    t = threading.Thread(target=_run_and_log, daemon=True)
    if blocking:
        # The scheduler's use case: it needs to know price/OI has
        # ACTUALLY finished (not just "started") before it's safe to run
        # the earnings step, which depends on fresh price data existing.
        # Runs _run_and_log() synchronously in the caller's own thread
        # instead -- fine here since the scheduler loop is already on its
        # own dedicated background thread, not the main app/request
        # thread, so blocking it doesn't block anything else.
        _run_and_log()
        return 200, {"ok": True, "watchlist": wl_name, "symbols": len(syms), "mode": mode, "table": table, "status": "completed"}
    t.start()
    return 200, {"ok": True, "watchlist": wl_name, "symbols": len(syms),
                 "mode": mode, "table": table, "status": "started"}


@wl_bp.route("/<int:wl_id>/fetch_data", methods=["POST"])
def fetch_data_for_watchlist(wl_id):
    """
    Trigger Options OI fetch OR price-only fetch for a specific watchlist.
    Options OI → stored in `options` table.
    Price-only  → stored in `price_cache` table.
    Runs in background thread; returns immediately.

    Thin wrapper around _fetch_data_for_watchlist_core (see its
    docstring above) -- this is the only place flask.request gets
    touched, so the core logic stays callable from the scheduler too.

    oi_source: "yfinance" (default, fast, no real Greeks) or
    "tastytrade" (slow -- ~20s/symbol, real broker Greeks, intended for
    off-hours unattended runs where long-running is explicitly fine).
    Read from JSON body or query string, defaults to yfinance so every
    existing caller (including the scheduled per-watchlist loop) keeps
    its current behavior unless this is explicitly requested.
    """
    body = request.get_json(silent=True) or {}
    oi_source = (body.get("oi_source") or request.args.get("oi_source") or "yfinance").strip().lower()
    if oi_source not in ("yfinance", "tastytrade"):
        oi_source = "yfinance"
    status, payload = _fetch_data_for_watchlist_core(
        wl_id, source="manual-button",
        remote_addr=request.remote_addr,
        user_agent=request.headers.get("User-Agent", "?"),
        referer=request.headers.get("Referer", "?"),
        blocking=False,
        oi_source=oi_source,
    )
    return jsonify(payload), status


def _fetch_intraday_price_for_watchlist_core(wl_id: int) -> dict:
    """Fetch one completed 04:00--16:00 ET session for every symbol.

    One-minute source bars are aggregated before storage in
    intraday_2m_price_cache, matching the strategy's two-minute chart and
    avoiding unnecessary SQLite rows.  premarket_levels stores the derived 04:00--09:29
    ET high/low for each symbol and date.
    """
    _ensure_tables()
    from ..services.intraday_price_cache import fetch_watchlist_intraday
    return fetch_watchlist_intraday(wl_id)


@wl_bp.route("/<int:wl_id>/fetch_intraday_price", methods=["POST"])
def fetch_intraday_price_for_watchlist(wl_id):
    """Start an end-of-day extended-hours price fetch without blocking HTTP."""
    _ensure_tables()
    with _intraday_fetch_in_progress_lock:
        if wl_id in _intraday_fetch_in_progress:
            return jsonify({"ok": False, "error": "An intraday price fetch is already running for this watchlist"}), 409
        _intraday_fetch_in_progress.add(wl_id)

    def _run():
        run_id = None
        try:
            from ..services.job_registry import log_run_start, log_run_finish
            run_id = log_run_start(f"wl_fetch_intraday_price_{wl_id}")
            result = _fetch_intraday_price_for_watchlist_core(wl_id)
            ok = bool(result.get("ok"))
            log_run_finish(
                run_id, ok,
                f"{result.get('symbols', 0)} symbol(s); "
                f"{sum(r.get('bars', 0) for r in result.get('results', []))} two-minute bars",
            )
            print(f"[watchlist_manager] intraday price fetch for watchlist_id={wl_id}: {result}")
        except Exception as exc:
            print(f"[watchlist_manager] intraday price fetch FAILED for watchlist_id={wl_id}: {exc}")
            if run_id is not None:
                try:
                    from ..services.job_registry import log_run_finish
                    log_run_finish(run_id, False, str(exc))
                except Exception:
                    pass
        finally:
            with _intraday_fetch_in_progress_lock:
                _intraday_fetch_in_progress.discard(wl_id)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "started": True, "watchlist_id": wl_id,
                    "message": "Fetching extended-hours data and storing two-minute bars for backtests."})


@wl_bp.route("/<int:wl_id>/fetch_status")
def watchlist_fetch_status(wl_id):
    """Check how many rows exist in the relevant table for this watchlist."""
    _ensure_tables()
    con = _conn()
    wl = con.execute(
        "SELECT name, fetch_options_oi FROM watchlists WHERE id=?", (wl_id,)
    ).fetchone()
    if not wl:
        con.close()
        return jsonify({"error": "Not found"}), 404
    wl_name, fetch_oi = wl[0], bool(wl[1])
    syms = [r[0] for r in con.execute(
        "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=?", (wl_id,)
    ).fetchall()]
    today = __import__("datetime").date.today().isoformat()
    if fetch_oi:
        count = con.execute(
            f"SELECT COUNT(DISTINCT symbol) FROM options WHERE date=? AND symbol IN ({','.join(['?']*len(syms))})",
            [today]+syms
        ).fetchone()[0] if syms else 0
        table = "options"
    else:
        count = con.execute(
            f"SELECT COUNT(*) FROM price_cache WHERE date=? AND symbol IN ({','.join(['?']*len(syms))})",
            [today]+syms
        ).fetchone()[0] if syms else 0
        table = "price_cache"
    con.close()
    result = {"watchlist": wl_name, "symbols_total": len(syms),
              "fetched_today": count, "table": table}
    # Real error visibility -- previously this endpoint only ever
    # reported successfully-fetched row counts, so a fetch where every
    # symbol failed looked identical to one still in progress from the
    # frontend's perspective (both show "0 fetched" and keep polling).
    last = _last_oi_fetch_result.get(wl_id)
    if last:
        result["last_run"] = {
            "fetched": last.get("fetched", 0), "errored": last.get("errored", 0),
            "no_options": last.get("no_options", 0), "total": last.get("total", 0),
            "sample_errors": last.get("sample_errors", []), "timed_out": last.get("timed_out", 0),
        }
    return jsonify(result)


# ── Custom alert rules (price / primitive / combo) ─────────────────────────
def _ensure_alert_rules_table():
    """Create the custom alert rules table without touching existing data."""
    con = _conn()
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                alert_kind TEXT NOT NULL DEFAULT 'price',
                watchlist_id INTEGER,
                symbol TEXT,
                benchmark TEXT DEFAULT 'SPY',
                trigger_mode TEXT DEFAULT 'once',
                enabled INTEGER DEFAULT 1,
                price_operator TEXT DEFAULT '>=',
                price_value REAL,
                condition_text TEXT DEFAULT '',
                timeframe TEXT DEFAULT '1d',
                notes TEXT DEFAULT '',
                last_triggered_at TEXT,
                last_trigger_state INTEGER DEFAULT 0,
                last_match_symbol TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS alert_rule_symbol_state (
                rule_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                last_match_state INTEGER DEFAULT 0,
                last_triggered_at TEXT,
                last_seen_at TEXT,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (rule_id, symbol)
            );
            CREATE INDEX IF NOT EXISTS idx_alert_rules_enabled ON alert_rules(enabled);
            CREATE INDEX IF NOT EXISTS idx_alert_rules_watchlist ON alert_rules(watchlist_id);
            CREATE INDEX IF NOT EXISTS idx_alert_rules_symbol ON alert_rules(symbol);
            CREATE INDEX IF NOT EXISTS idx_alert_rule_symbol_state_rule ON alert_rule_symbol_state(rule_id);
            CREATE INDEX IF NOT EXISTS idx_alert_rule_symbol_state_updated ON alert_rule_symbol_state(updated_at);
            """
        )
        for ddl in (
            "ALTER TABLE alert_rules ADD COLUMN alert_kind TEXT NOT NULL DEFAULT 'price'",
            "ALTER TABLE alert_rules ADD COLUMN watchlist_id INTEGER",
            "ALTER TABLE alert_rules ADD COLUMN symbol TEXT",
            "ALTER TABLE alert_rules ADD COLUMN benchmark TEXT DEFAULT 'SPY'",
            "ALTER TABLE alert_rules ADD COLUMN trigger_mode TEXT DEFAULT 'once'",
            "ALTER TABLE alert_rules ADD COLUMN enabled INTEGER DEFAULT 1",
            "ALTER TABLE alert_rules ADD COLUMN price_operator TEXT DEFAULT '>='",
            "ALTER TABLE alert_rules ADD COLUMN price_value REAL",
            "ALTER TABLE alert_rules ADD COLUMN condition_text TEXT DEFAULT ''",
            "ALTER TABLE alert_rules ADD COLUMN timeframe TEXT DEFAULT '1d'",
            "ALTER TABLE alert_rules ADD COLUMN notes TEXT DEFAULT ''",
            "ALTER TABLE alert_rules ADD COLUMN last_triggered_at TEXT",
            "ALTER TABLE alert_rules ADD COLUMN last_trigger_state INTEGER DEFAULT 0",
            "ALTER TABLE alert_rules ADD COLUMN last_match_symbol TEXT",
            "ALTER TABLE alert_rules ADD COLUMN created_at TEXT DEFAULT (datetime('now'))",
            "ALTER TABLE alert_rules ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))",
            "ALTER TABLE alert_rule_symbol_state ADD COLUMN last_match_state INTEGER DEFAULT 0",
            "ALTER TABLE alert_rule_symbol_state ADD COLUMN last_triggered_at TEXT",
            "ALTER TABLE alert_rule_symbol_state ADD COLUMN last_seen_at TEXT",
            "ALTER TABLE alert_rule_symbol_state ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))",
        ):
            try:
                con.execute(ddl)
            except Exception:
                pass
        con.commit()
    finally:
        con.close()


def _alert_rule_scope(rule: dict) -> str:
    wl = (rule.get('watchlist_name') or '').strip()
    sym = (rule.get('symbol') or '').strip().upper()
    if wl and sym:
        return f"{wl} · {sym}"
    if wl:
        return wl
    if sym:
        return sym
    return 'All symbols'


def _alert_rule_auto_name(scope: str, kind: str, timeframe: str, condition: str, symbol: str = '') -> str:
    """Build a stable internal name for alert_rules without exposing it in the UI."""
    scope = (scope or 'All symbols').strip()
    kind = (kind or 'price').strip().lower()
    timeframe = (timeframe or '1d').strip()
    condition = re.sub(r'\s+', ' ', (condition or '').strip())[:42]
    symbol = (symbol or '').strip().upper()
    stamp = datetime.now().strftime('%Y%m%d%H%M%S')
    parts = [scope]
    if symbol and symbol not in scope.upper():
        parts.append(symbol)
    parts.extend([kind, timeframe])
    if condition:
        parts.append(condition)
    parts.append(stamp)
    return ' · '.join(parts)[:220]


def _alert_rule_symbols(rule: dict):
    """Resolve the symbols to evaluate for a rule."""
    con = _conn()
    try:
        watchlist_id = rule.get('watchlist_id')
        symbol = (rule.get('symbol') or '').strip().upper()
        symbols = []
        if symbol:
            if watchlist_id:
                row = con.execute(
                    "SELECT 1 FROM watchlist_symbols WHERE watchlist_id=? AND UPPER(symbol)=UPPER(?)",
                    (watchlist_id, symbol),
                ).fetchone()
                if row:
                    symbols = [symbol]
                else:
                    symbols = [symbol]  # free-form symbol still allowed
            else:
                symbols = [symbol]
        elif watchlist_id:
            rows = con.execute(
                "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                (watchlist_id,),
            ).fetchall()
            symbols = [str(r['symbol'] or '').upper() for r in rows if str(r['symbol'] or '').strip()]
        else:
            rows = con.execute(
                "SELECT DISTINCT UPPER(symbol) AS symbol FROM watchlist_symbols WHERE COALESCE(symbol,'') <> '' ORDER BY 1"
            ).fetchall()
            symbols = [str(r['symbol'] or '').upper() for r in rows if str(r['symbol'] or '').strip()]
        return list(dict.fromkeys([s for s in symbols if s]))
    finally:
        con.close()


def _alert_rule_rows():
    _ensure_alert_rules_table()
    con = _conn()
    try:
        rows = con.execute(
            """
            SELECT ar.*, w.name AS watchlist_name
            FROM alert_rules ar
            LEFT JOIN watchlists w ON w.id = ar.watchlist_id
            ORDER BY ar.updated_at DESC, ar.created_at DESC, ar.id DESC
            """
        ).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            item['scope'] = _alert_rule_scope(item)
            items.append(item)
        return items
    finally:
        con.close()


_alert_rule_last_run_at = None
_alert_rule_last_result = {'ok': False, 'triggered': 0, 'alerts': []}


def _send_alert_telegram(rule, kind, matched, results, *, rule_id=None, prefix='📣 Alert'):
    """Send a Telegram message for alert matches."""
    try:
        from ..services.telegram_alerts import telegram_configured, send_telegram_message
    except Exception as e:
        return {'configured': False, 'sent': 0, 'error': f'telegram service unavailable: {e}'}

    if not telegram_configured():
        return {'configured': False, 'sent': 0, 'error': 'Telegram credentials not configured'}

    rule_name = str(rule.get('name') or f"Alert #{rule_id or rule.get('id') or ''}").strip() or 'Alert'
    watchlist_name = str(rule.get('watchlist_name') or 'Watchlist').strip() or 'Watchlist'
    rule_kind = str(kind or 'price').strip().lower()
    matched_syms = [str(s).strip().upper() for s in (matched or []) if str(s).strip()]
    if not matched_syms:
        return {'configured': True, 'sent': 0, 'error': 'no matched symbols'}

    sent = 0
    errors = []
    result_map = {str(r.get('symbol')).strip().upper(): r for r in (results or []) if r.get('symbol')}

    for sym in matched_syms[:5]:
        row = result_map.get(sym, {})
        parts = [
            prefix,
            f'Rule: {rule_name}',
            f'Watchlist: {watchlist_name}',
            f'Symbol: {sym}',
        ]
        if rule_kind == 'price':
            spot = row.get('spot')
            try:
                if spot is not None:
                    parts.append(f'Spot: {float(spot):.2f}')
            except Exception:
                parts.append(f'Spot: {spot}')
            op = rule.get('price_operator') or '>='
            threshold = rule.get('price_value')
            if threshold is not None:
                try:
                    parts.append(f'Condition: spot {op} {float(threshold):g}')
                except Exception:
                    parts.append(f'Condition: spot {op} {threshold}')
        else:
            cond = (rule.get('condition_text') or '').strip()
            if cond:
                parts.append(f'Condition: {cond}')
            parts.append('Matched: yes')

        try:
            result = send_telegram_message('\n'.join(parts))
        except Exception as e:
            result = {'ok': False, 'error': str(e)}
        if result.get('ok'):
            sent += 1
        else:
            errors.append(result.get('error') or result.get('description') or 'Telegram send failed')

    payload = {'configured': True, 'sent': sent}
    if errors and sent == 0:
        payload['error'] = '; '.join(errors[:3])
    elif errors:
        payload['error'] = '; '.join(errors[:3])
    return payload


def _send_alert_test_telegram(rule, kind, matched, results, *, rule_id=None):
    """Send a Telegram message for alert test matches."""
    return _send_alert_telegram(rule, kind, matched, results, rule_id=rule_id, prefix='📣 Alert test')


def _compare_price(val, op, threshold):
    try:
        v = float(val)
        t = float(threshold)
    except Exception:
        return False
    op = (op or '>=').strip().lower()
    if op in {'>', 'gt'}:
        return v > t
    if op in {'>=', 'gte', 'ge'}:
        return v >= t
    if op in {'<', 'lt'}:
        return v < t
    if op in {'<=', 'lte', 'le'}:
        return v <= t
    if op in {'=', '==', 'eq'}:
        return abs(v - t) < 1e-9
    if op in {'cross_above', 'cross above'}:
        return v >= t
    if op in {'cross_below', 'cross below'}:
        return v <= t
    return False


def _normalize_alert_trigger_mode(mode) -> str:
    """Normalize alert delivery mode.

    Historic rows used `every` for repeating alerts. For scanner/watchlist
    alerts that refresh every few minutes, repeating every run is too noisy,
    so `every` is now treated as `daily`: at most one notification per
    rule/symbol/calendar day while the condition remains true.

    `on_change` still re-arms after the condition becomes false, but it also
    has the same once-per-day guard to avoid current-bar flicker duplicates.
    `every_run` is kept only as an explicit internal escape hatch.
    """
    m = str(mode or 'daily').strip().lower().replace('-', '_').replace(' ', '_')
    if m in {'on_change', 'change', 'changes', 'transition', 'enter_exit', 'rearm', 're_arm'}:
        return 'on_change'
    if m in {'every_run', 'everyrun', 'always_run', 'debug_every_run'}:
        return 'every_run'
    if m in {'daily', 'once_per_day', 'once_daily', 'per_day', 'every_day', 'every', 'always', 'repeat', 'recurring'}:
        return 'daily'
    if m in {'once', 'one_time', 'one_time_only', 'first'}:
        return 'once'
    return 'daily'


def _alert_date_key(value=None) -> str:
    """Return YYYY-MM-DD for an alert timestamp/date-like value."""
    if value:
        txt = str(value).strip()
        if len(txt) >= 10:
            return txt[:10]
    try:
        return datetime.now().date().isoformat()
    except Exception:
        return str(__import__('datetime').date.today())


def _alert_triggered_today(state: dict) -> bool:
    ts = state.get('last_triggered_at') if state else None
    if not ts:
        return False
    return _alert_date_key(ts) == _alert_date_key()


def _alert_symbol_state(rule_id, symbol) -> dict:
    """Return persisted match state for one alert rule/symbol pair."""
    _ensure_alert_rules_table()
    sym = str(symbol or '').strip().upper()
    if not rule_id or not sym:
        return {'last_match_state': 0}
    con = _conn()
    try:
        row = con.execute(
            "SELECT * FROM alert_rule_symbol_state WHERE rule_id=? AND UPPER(symbol)=UPPER(?)",
            (int(rule_id), sym),
        ).fetchone()
        return dict(row) if row else {'rule_id': int(rule_id), 'symbol': sym, 'last_match_state': 0}
    finally:
        con.close()


def _save_alert_symbol_state(rule_id, symbol, matched: bool, *, triggered: bool = False):
    """Persist per-symbol alert state so recurring scans do not resend unchanged matches."""
    _ensure_alert_rules_table()
    sym = str(symbol or '').strip().upper()
    if not rule_id or not sym:
        return
    con = _conn()
    try:
        con.execute(
            """
            INSERT INTO alert_rule_symbol_state
                (rule_id, symbol, last_match_state, last_triggered_at, last_seen_at, updated_at)
            VALUES (?, ?, ?, CASE WHEN ? THEN datetime('now') ELSE NULL END, datetime('now'), datetime('now'))
            ON CONFLICT(rule_id, symbol) DO UPDATE SET
                last_match_state=excluded.last_match_state,
                last_triggered_at=CASE
                    WHEN ? THEN datetime('now')
                    ELSE alert_rule_symbol_state.last_triggered_at
                END,
                last_seen_at=datetime('now'),
                updated_at=datetime('now')
            """,
            (int(rule_id), sym, 1 if matched else 0, 1 if triggered else 0, 1 if triggered else 0),
        )
        con.commit()
    finally:
        con.close()


def _alert_should_notify_for_symbol(rule, symbol, matched: bool) -> bool:
    """
    Decide whether a rule should notify for this symbol.

    Delivery modes:
      once       -> existing one-time behavior, controlled by alert_rules.last_triggered_at.
      daily      -> notify at most once per rule/symbol/calendar day while matched.
      on_change  -> notify only when this symbol moves from not-matched to matched,
                    also capped at once per day to avoid intraday current-bar flicker.
      every_run  -> internal escape hatch; not exposed in the UI.

    This prevents duplicate Telegram messages when the alert scheduler refreshes
    every few minutes and the same symbol still matches the same daily signal.
    """
    mode = _normalize_alert_trigger_mode(rule.get('trigger_mode'))
    if not matched:
        # Persist false state so on_change can re-arm on a later real transition.
        if mode in {'on_change', 'daily'}:
            _save_alert_symbol_state(rule.get('id'), symbol, False, triggered=False)
        return False

    if mode == 'every_run':
        return True
    if mode == 'once':
        return True

    state = _alert_symbol_state(rule.get('id'), symbol)
    if _alert_triggered_today(state):
        return False

    if mode == 'on_change':
        return int(state.get('last_match_state') or 0) != 1

    # daily / default: condition can remain true all day, but notify only once.
    return True


def _get_watchlist_symbols_by_id(watchlist_id):
    con = _conn()
    try:
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (watchlist_id,),
        ).fetchall()
        return [str(r['symbol'] or '').upper() for r in rows if str(r['symbol'] or '').strip()]
    finally:
        con.close()


def run_alert_rules_once(symbols=None, watchlist_id=None, source='scheduler'):
    """Evaluate active alert rules and log notifications when conditions are met."""
    _ensure_alert_rules_table()
    try:
        import json as _json
        from .scanner_builder import _parse_query, _expand_scan_nodes, _required_timeframes, _scan_symbol, _eval
    except Exception as e:
        return {'ok': False, 'error': f'alert evaluator unavailable: {e}', 'triggered': 0}

    con = _conn()
    try:
        rules = con.execute(
            """
            SELECT ar.*, w.name AS watchlist_name
            FROM alert_rules ar
            LEFT JOIN watchlists w ON w.id = ar.watchlist_id
            WHERE COALESCE(ar.enabled,1)=1
            ORDER BY ar.updated_at DESC, ar.created_at DESC, ar.id DESC
            """
        ).fetchall()
        rules = [dict(r) for r in rules]
    finally:
        con.close()

    triggered = []
    scope_symbols = None
    if symbols:
        scope_symbols = list(dict.fromkeys([str(s).strip().upper() for s in symbols if str(s).strip()]))
    elif watchlist_id:
        scope_symbols = _get_watchlist_symbols_by_id(watchlist_id)

    live_price_cache = {}
    try:
        from ..services.market import get_spot as _get_spot
    except Exception:
        _get_spot = None

    def _spot(sym):
        sym = str(sym or '').strip().upper()
        if not sym:
            return None
        if sym not in live_price_cache:
            val = None
            try:
                if _get_spot is not None:
                    val = _get_spot(sym)
            except Exception:
                val = None
            live_price_cache[sym] = val
        return live_price_cache.get(sym)

    for rule in rules:
        try:
            rule_id = rule['id']
            name = rule.get('name') or f'Alert #{rule_id}'
            trigger_mode = _normalize_alert_trigger_mode(rule.get('trigger_mode'))
            last_triggered_at = rule.get('last_triggered_at')
            if trigger_mode == 'once' and last_triggered_at:
                continue

            kind = (rule.get('alert_kind') or 'price').strip().lower()
            rule_symbols = _alert_rule_symbols(rule)
            if scope_symbols is not None:
                if rule.get('symbol'):
                    rule_symbols = [s for s in rule_symbols if s in scope_symbols] or rule_symbols
                elif rule.get('watchlist_id') and watchlist_id and int(rule.get('watchlist_id') or 0) != int(watchlist_id or 0):
                    continue
                elif rule.get('watchlist_id') is None and watchlist_id is not None:
                    # broad/global rules are okay; keep them if we have current scope symbols
                    rule_symbols = [s for s in (scope_symbols or rule_symbols) if s]
            if not rule_symbols:
                continue

            # Accumulates every symbol that matches THIS rule during this
            # scan pass, so we log one summary notification per rule
            # ("3 symbols matched: TTWO, MRNA, XYZ") instead of one
            # notification per symbol -- per-symbol side effects (Telegram
            # send, DB state update, last_match_symbol) are unaffected,
            # only the notification-bell entry is aggregated.
            _rule_matched_syms = []

            if kind == 'price':
                op = rule.get('price_operator') or '>='
                threshold = rule.get('price_value')
                if threshold is None:
                    continue
                for sym in rule_symbols:
                    px = _spot(sym)
                    if px is None:
                        continue
                    matched = _compare_price(px, op, threshold)
                    if not matched:
                        _alert_should_notify_for_symbol(rule, sym, False)
                        continue
                    if not _alert_should_notify_for_symbol(rule, sym, True):
                        continue
                    detail = f"{sym} spot {px:.2f} {op} {float(threshold):g}"
                    _rule_matched_syms.append(sym)
                    con2 = _conn()
                    try:
                        con2.execute(
                            "UPDATE alert_rules SET last_triggered_at=datetime('now'), last_trigger_state=1, last_match_symbol=?, updated_at=datetime('now') WHERE id=?",
                            (sym, rule_id),
                        )
                        con2.commit()
                    finally:
                        con2.close()
                    _save_alert_symbol_state(rule_id, sym, True, triggered=True)
                    telegram = _send_alert_telegram(rule, kind, [sym], [{'symbol': sym, 'spot': px, 'matched': True}], rule_id=rule_id, prefix='📣 Alert')
                    if telegram.get('error'):
                        detail = f"{detail} · Telegram: {telegram.get('error')}"
                    triggered.append({'id': rule_id, 'symbol': sym, 'title': name, 'detail': detail, 'telegram': telegram})
                    if trigger_mode == 'once':
                        break
                if _rule_matched_syms:
                    _n = len(_rule_matched_syms)
                    _summary = f"{_n} symbol{'s' if _n != 1 else ''} matched: {', '.join(_rule_matched_syms[:15])}" + (f" (+{_n-15} more)" if _n > 15 else "")
                    try:
                        log_alert_notification('PRICE', name, _summary, symbol=(_rule_matched_syms[0] if _n == 1 else None),
                                                severity='info', source=source, metadata=_json.dumps({
                            'rule_id': rule_id, 'kind': kind, 'watchlist_id': rule.get('watchlist_id'),
                            'symbols': _rule_matched_syms, 'trigger_mode': trigger_mode,
                        }))
                    except Exception:
                        pass
                continue

            expr = (rule.get('condition_text') or '').strip()
            if not expr:
                continue
            try:
                raw = _parse_query(expr)
                root = _expand_scan_nodes(raw, ())
                rule_tf = (rule.get('timeframe') or '1d').strip() or '1d'
                req_tfs = list(dict.fromkeys((_required_timeframes(root) or []) + [rule_tf]))
            except Exception as e:
                continue

            for sym in rule_symbols:
                ctx, err = _scan_symbol(sym, root, (rule.get('benchmark') or 'SPY').strip().upper() or 'SPY', req_tfs)
                if not ctx:
                    continue
                try:
                    ok = bool(_eval(root, ctx, shift=0, tf_default=rule_tf))
                except Exception:
                    ok = False
                if not ok:
                    _alert_should_notify_for_symbol(rule, sym, False)
                    continue
                if not _alert_should_notify_for_symbol(rule, sym, True):
                    continue
                detail = f"{sym} matched {kind} alert: {name}"
                _rule_matched_syms.append(sym)
                con2 = _conn()
                try:
                    con2.execute(
                        "UPDATE alert_rules SET last_triggered_at=datetime('now'), last_trigger_state=1, last_match_symbol=?, updated_at=datetime('now') WHERE id=?",
                        (sym, rule_id),
                    )
                    con2.commit()
                finally:
                    con2.close()
                _save_alert_symbol_state(rule_id, sym, True, triggered=True)
                telegram = _send_alert_telegram(rule, kind, [sym], [{'symbol': sym, 'matched': True}], rule_id=rule_id, prefix='📣 Alert')
                if telegram.get('error'):
                    detail = f"{detail} · Telegram: {telegram.get('error')}"
                triggered.append({'id': rule_id, 'symbol': sym, 'title': name, 'detail': detail, 'telegram': telegram})
                if trigger_mode == 'once':
                    break
            if _rule_matched_syms:
                _n = len(_rule_matched_syms)
                _summary = f"{_n} symbol{'s' if _n != 1 else ''} matched: {', '.join(_rule_matched_syms[:15])}" + (f" (+{_n-15} more)" if _n > 15 else "")
                try:
                    log_alert_notification(kind.upper(), name, _summary, symbol=(_rule_matched_syms[0] if _n == 1 else None),
                                            severity='info', source=source, metadata=_json.dumps({
                        'rule_id': rule_id, 'kind': kind, 'watchlist_id': rule.get('watchlist_id'),
                        'symbols': _rule_matched_syms, 'trigger_mode': trigger_mode,
                    }), scanner_name=name)
                except Exception:
                    pass
        except Exception:
            continue

    global _alert_rule_last_run_at, _alert_rule_last_result
    _alert_rule_last_run_at = datetime.now().isoformat(timespec='seconds')
    _alert_rule_last_result = {'ok': True, 'triggered': len(triggered), 'alerts': triggered, 'source': source}
    return {'ok': True, 'triggered': len(triggered), 'alerts': triggered}


_alert_rule_watcher_started = False
_alert_rule_watcher_lock = __import__('threading').Lock()


def start_alert_rule_watcher(interval_seconds=900):
    """Background loop that evaluates custom scanner/watchlist alerts.

    The cadence is global across all Telegram alerts and can be changed from
    Alert Hub without restarting the app.
    """
    global _alert_rule_watcher_started
    with _alert_rule_watcher_lock:
        if _alert_rule_watcher_started:
            return False
        _alert_rule_watcher_started = True

    from ..services.job_registry import register_job
    from ..services import unified_scheduler
    try:
        default_interval = get_global_alert_interval_seconds(interval_seconds or 900)
    except Exception:
        default_interval = int(interval_seconds or 900)
    register_job(
        "alert_rules_scan", "Scanner/watchlist alert rules", "Evaluates custom scanner/watchlist alert rules.",
        kind="interval", default_schedule={"interval_min": max(1, int((default_interval or 900) / 60))},
        group="Alert Watchers", run_now_fn=lambda: run_alert_rules_once(source='watcher'),
    )
    unified_scheduler.register("alert_rules_scan", lambda: run_alert_rules_once(source='watcher'), default_interval)
    return True




@wl_bp.route('/alerts/settings', methods=['GET'])
def alerts_settings_get():
    """Return global alert scheduler settings."""
    _ensure_tables()
    freq = _normalise_alert_frequency()
    return jsonify({
        "ok": True,
        "frequency": freq,
        "seconds": _alert_frequency_seconds(freq),
        "options": _alert_frequency_options_payload(),
        "note": "Applies to price alerts, scanner/watchlist alerts, PNR checks, trade-health checks, and custom position alerts. Manual tests run immediately.",
    })


@wl_bp.route('/alerts/settings', methods=['POST'])
def alerts_settings_set():
    """Persist the global alert scheduler frequency."""
    _ensure_tables()
    d = request.get_json(silent=True) or {}
    freq = _normalise_alert_frequency(d.get('frequency') or d.get('global_alert_frequency'))
    _set_setting('global_alert_frequency', freq)
    return jsonify({
        "ok": True,
        "frequency": freq,
        "seconds": _alert_frequency_seconds(freq),
        "options": _alert_frequency_options_payload(),
    })

@wl_bp.route('/alerts/picklists', methods=['GET'])
def alerts_picklists():
    """Return watchlists and alert symbol picklists for the alert hub."""
    _ensure_tables()
    watchlist_id = request.args.get('watchlist_id')
    try:
        watchlist_id = int(watchlist_id) if watchlist_id not in (None, '', 'all') else None
    except Exception:
        watchlist_id = None
    con = _conn()
    try:
        wls = [dict(r) for r in con.execute('SELECT id, name, is_default, fetch_options_oi FROM watchlists ORDER BY is_default DESC, lower(name)').fetchall()]
        if watchlist_id:
            syms = [r['symbol'] for r in con.execute("SELECT DISTINCT UPPER(symbol) AS symbol FROM watchlist_symbols WHERE watchlist_id=? AND COALESCE(symbol,'') <> '' ORDER BY 1", (watchlist_id,)).fetchall()]
        else:
            syms = [r['symbol'] for r in con.execute("SELECT DISTINCT UPPER(symbol) AS symbol FROM watchlist_symbols WHERE COALESCE(symbol,'') <> '' ORDER BY 1").fetchall()]
    except Exception:
        wls, syms = [], []
    finally:
        con.close()
    return jsonify({'watchlists': wls, 'symbols': syms})


@wl_bp.route('/alerts/rules', methods=['GET', 'POST'])
def alerts_rules_collection():
    _ensure_alert_rules_table()
    if request.method == 'GET':
        try:
            rows = _alert_rule_rows()
            return jsonify({'rules': rows, 'count': len(rows)})
        except Exception as e:
            return jsonify({'rules': [], 'count': 0, 'error': str(e)})

    d = request.get_json(force=True) or {}
    name = (d.get('name') or '').strip()
    kind = (d.get('alert_kind') or 'price').strip().lower()
    trigger_mode = _normalize_alert_trigger_mode(d.get('trigger_mode') or 'daily')
    if trigger_mode not in {'once', 'daily', 'on_change', 'every_run'}:
        return jsonify({'error': 'trigger_mode must be once, daily, or on_change'}), 400
    watchlist_id = d.get('watchlist_id')
    symbol = (d.get('symbol') or '').strip().upper() or None
    benchmark = (d.get('benchmark') or 'SPY').strip().upper() or 'SPY'
    timeframe = (d.get('timeframe') or '1d').strip() or '1d'
    notes = (d.get('notes') or '').strip()
    enabled = 1 if d.get('enabled', True) else 0
    if watchlist_id in ('', None):
        watchlist_id = None
    else:
        try:
            watchlist_id = int(watchlist_id)
        except Exception:
            return jsonify({'error': 'watchlist_id must be numeric'}), 400
    watchlist_name = ''
    if watchlist_id:
        con_name = _conn()
        try:
            row = con_name.execute('SELECT name FROM watchlists WHERE id=?', (watchlist_id,)).fetchone()
            watchlist_name = row[0] if row else ''
        except Exception:
            watchlist_name = ''
        finally:
            con_name.close()
    if not watchlist_id and not symbol:
        return jsonify({'error': 'select a watchlist or enter a symbol'}), 400
    if not name:
        scope = _alert_rule_scope({'watchlist_name': watchlist_name or '', 'symbol': symbol or ''})
        name = _alert_rule_auto_name(scope, kind, timeframe, d.get('condition_text') or '', symbol or '')
    price_operator = (d.get('price_operator') or '>=').strip()
    price_value = d.get('price_value')
    if kind == 'price':
        if price_value in ('', None):
            return jsonify({'error': 'price_value is required for price alerts'}), 400
        try:
            price_value = float(price_value)
        except Exception:
            return jsonify({'error': 'price_value must be numeric'}), 400
    else:
        price_value = None
    condition_text = (d.get('condition_text') or '').strip()
    if kind != 'price' and not condition_text:
        return jsonify({'error': 'condition_text is required for primitive/combo alerts'}), 400

    con = _conn()
    try:
        con.execute(
            """
            INSERT INTO alert_rules
              (name, alert_kind, watchlist_id, symbol, benchmark, trigger_mode, enabled, price_operator, price_value, condition_text, timeframe, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (name, kind, watchlist_id, symbol, benchmark, trigger_mode, enabled, price_operator, price_value, condition_text, timeframe, notes),
        )
        con.commit()
        row = con.execute(
            """
            SELECT ar.*, w.name AS watchlist_name
            FROM alert_rules ar LEFT JOIN watchlists w ON w.id = ar.watchlist_id
            WHERE ar.name=?
            """,
            (name,),
        ).fetchone()
        item = dict(row) if row else {}
        item['scope'] = _alert_rule_scope(item)
        return jsonify({'ok': True, 'rule': item})
    except sqlite3.IntegrityError:
        return jsonify({'error': f"alert rule '{name}' already exists"}), 409
    finally:
        con.close()


@wl_bp.route('/alerts/rules/<int:rule_id>/delete', methods=['POST'])
def alerts_rules_delete_post(rule_id: int):
    """POST fallback for deleting alert rules.

    Some browser/proxy setups handle DELETE inconsistently.  The UI first tries
    DELETE and falls back to this endpoint so confirmation actually removes the
    rule in those environments too.
    """
    return _delete_alert_rule(rule_id)


def _delete_alert_rule(rule_id: int):
    _ensure_alert_rules_table()
    con = _conn()
    try:
        con.execute('DELETE FROM alert_rule_symbol_state WHERE rule_id=?', (rule_id,))
        cur = con.execute('DELETE FROM alert_rules WHERE id=?', (rule_id,))
        con.commit()
        return jsonify({'ok': True, 'deleted': cur.rowcount, 'id': rule_id})
    finally:
        con.close()


@wl_bp.route('/alerts/rules/<int:rule_id>', methods=['PUT', 'DELETE'])
def alerts_rules_item(rule_id: int):
    _ensure_alert_rules_table()
    if request.method == 'DELETE':
        return _delete_alert_rule(rule_id)

    d = request.get_json(force=True) or {}
    fields = []
    vals = []
    for key in ('name', 'alert_kind', 'watchlist_id', 'symbol', 'benchmark', 'trigger_mode', 'enabled', 'price_operator', 'price_value', 'condition_text', 'timeframe', 'notes'):
        if key not in d:
            continue
        v = d[key]
        if key == 'symbol' and v:
            v = str(v).strip().upper()
        if key == 'watchlist_id' and v in ('', None):
            v = None
        if key == 'enabled':
            v = 1 if bool(v) else 0
        if key == 'trigger_mode' and v:
            v = _normalize_alert_trigger_mode(v)
        if key == 'alert_kind' and v:
            v = str(v).strip().lower()
        if key == 'benchmark' and v:
            v = str(v).strip().upper()
        if key == 'timeframe' and v:
            v = str(v).strip()
        if key == 'price_value' and v in ('', None):
            v = None
        fields.append(f'{key}=?')
        vals.append(v)
    if not fields:
        return jsonify({'error': 'no fields to update'}), 400
    vals.append(rule_id)
    con = _conn()
    try:
        con.execute(f"UPDATE alert_rules SET {', '.join(fields)}, updated_at=datetime('now') WHERE id=?", vals)
        # Rule changes should re-arm per-symbol daily/on-change state.
        con.execute('DELETE FROM alert_rule_symbol_state WHERE rule_id=?', (rule_id,))
        con.commit()
        row = con.execute(
            """
            SELECT ar.*, w.name AS watchlist_name
            FROM alert_rules ar LEFT JOIN watchlists w ON w.id = ar.watchlist_id
            WHERE ar.id=?
            """,
            (rule_id,),
        ).fetchone()
        item = dict(row) if row else {}
        item['scope'] = _alert_rule_scope(item)
        return jsonify({'ok': True, 'rule': item})
    finally:
        con.close()


@wl_bp.route('/alerts/rules/<int:rule_id>/toggle', methods=['POST'])
def alerts_rules_toggle(rule_id: int):
    _ensure_alert_rules_table()
    d = request.get_json(force=True) or {}
    enabled = 1 if d.get('enabled', True) else 0
    con = _conn()
    try:
        con.execute('UPDATE alert_rules SET enabled=?, updated_at=datetime(\'now\') WHERE id=?', (enabled, rule_id))
        con.commit()
        return jsonify({'ok': True, 'id': rule_id, 'enabled': enabled})
    finally:
        con.close()
