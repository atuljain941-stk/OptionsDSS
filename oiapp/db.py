import os
import re
import sqlite3
from pathlib import Path
from datetime import datetime

from .config import DB_PATH as _OIAPP_DB_PATH

DB_PATH = Path(_OIAPP_DB_PATH)

# Optional debugging mode: block INSERT/UPDATE/DELETE/REPLACE statements while
# still allowing SELECT/PRAGMA/DDL so the app can run read-only.
# Default is OFF because saved runs, watchlists, and scanner settings must persist.
READ_ONLY_MODE = os.getenv("OIAPP_READ_ONLY_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
_WRITE_SQL_PREFIXES = ("insert", "update", "delete", "replace", "merge", "upsert")


def _strip_sql(sql: str) -> str:
    s = str(sql or "")
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"--.*?(?:\n|$)", " ", s)
    return s.strip()


def _is_write_sql(sql: str) -> bool:
    s = _strip_sql(sql).lower()
    return any(s.startswith(prefix) for prefix in _WRITE_SQL_PREFIXES)


def _split_script(script: str):
    chunk = []
    in_single = False
    in_double = False
    escape = False
    for ch in str(script or ""):
        if escape:
            chunk.append(ch)
            escape = False
            continue
        if ch == "\\":
            chunk.append(ch)
            escape = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if ch == ";" and not in_single and not in_double:
            stmt = "".join(chunk).strip()
            if stmt:
                yield stmt
            chunk = []
        else:
            chunk.append(ch)
    stmt = "".join(chunk).strip()
    if stmt:
        yield stmt


class _ReadOnlyCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        if READ_ONLY_MODE and _is_write_sql(sql):
            return super().execute("SELECT 1 WHERE 0")
        return super().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        if READ_ONLY_MODE and _is_write_sql(sql):
            return super().execute("SELECT 1 WHERE 0")
        return super().executemany(sql, seq_of_parameters)

    def executescript(self, script):
        if not READ_ONLY_MODE:
            return super().executescript(script)
        last = self
        for stmt in _split_script(script):
            if _is_write_sql(stmt):
                continue
            last = super().execute(stmt)
        return last


class _ReadOnlyConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(factory or _ReadOnlyCursor)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, script):
        return self.cursor().executescript(script)


_ORIG_SQLITE_CONNECT = sqlite3.connect


def _connect(*args, **kwargs):
    kwargs.setdefault("timeout", 60)
    if READ_ONLY_MODE and kwargs.get("factory") is None:
        kwargs["factory"] = _ReadOnlyConnection
    con = _ORIG_SQLITE_CONNECT(*(args or (DB_PATH,)), **kwargs)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=60000")
    except Exception:
        pass
    return con


if READ_ONLY_MODE:
    def _patched_connect(*args, **kwargs):
        kwargs.setdefault("timeout", 60)
        if kwargs.get("factory") is None:
            kwargs["factory"] = _ReadOnlyConnection
        con = _ORIG_SQLITE_CONNECT(*args, **kwargs)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=60000")
        except Exception:
            pass
        return con

    sqlite3.connect = _patched_connect


def _ensure_options_enrichment_columns(con):
    """Add non-destructive option-chain columns used by skew/max-pain analytics.

    Existing user databases created by older builds only have price/OI/volume.
    These ALTERs preserve all history and allow future fetches to store bid/ask,
    last, implied volatility and underlying spot.  Historical rows that lack the
    new fields remain usable for max-pain and OI-skew proxy calculations.
    """
    cols = set()
    try:
        cols = {str(r[1]).lower() for r in con.execute("PRAGMA table_info(options)").fetchall()}
    except Exception:
        cols = set()
    migrations = {
        "bid": "ALTER TABLE options ADD COLUMN bid REAL",
        "ask": "ALTER TABLE options ADD COLUMN ask REAL",
        "last": "ALTER TABLE options ADD COLUMN last REAL",
        "iv": "ALTER TABLE options ADD COLUMN iv REAL",
        "underlying": "ALTER TABLE options ADD COLUMN underlying REAL",
        "fetch_ts": "ALTER TABLE options ADD COLUMN fetch_ts TEXT",
        # Real broker Greeks, tastytrade-sourced (see
        # tastytrade_options_backfill.py) -- yfinance, this table's
        # original source, never provided these at all, only IV. NULL on
        # any row not yet covered by the tastytrade backfill (which is a
        # slow, throttled background process, not instant for the whole
        # watchlist) -- callers should treat NULL here as "not yet
        # fetched from tastytrade," not "zero."
        "delta": "ALTER TABLE options ADD COLUMN delta REAL",
        "gamma": "ALTER TABLE options ADD COLUMN gamma REAL",
        "theta": "ALTER TABLE options ADD COLUMN theta REAL",
        "vega": "ALTER TABLE options ADD COLUMN vega REAL",
        "data_source": "ALTER TABLE options ADD COLUMN data_source TEXT",
    }
    for col, sql in migrations.items():
        if col not in cols:
            try:
                con.execute(sql)
            except Exception:
                pass

def init_db():
    con = _connect(); cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS options (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol TEXT, expiration TEXT, type TEXT,
          strike REAL, price REAL, oi INTEGER, volume INTEGER, date TEXT,
          bid REAL, ask REAL, last REAL, iv REAL, underlying REAL, fetch_ts TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_options_symbol_exp_date ON options(symbol, expiration, date);
        CREATE INDEX IF NOT EXISTS idx_options_symbol_date ON options(symbol, date);
        CREATE TABLE IF NOT EXISTS symbols (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS scheduler_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT,
            symbols TEXT,
            status TEXT,
            message TEXT
        )  ;
        CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        expiry TEXT NOT NULL,
        trade_type TEXT NOT NULL,
        trade_subtype TEXT DEFAULT 'vertical',
        long_strike REAL NOT NULL,
        short_strike REAL,
        entry_price REAL NOT NULL,
        quantity INTEGER NOT NULL,
        entry_reason TEXT,
        entry_oi REAL,
        current_oi REAL,
        risk_amt REAL,
        reward_amt REAL,
        status TEXT DEFAULT 'OPEN',
        exit_price REAL,
        exit_date TEXT,
        pnl REAL,
        outlook TEXT,
        suggested_action TEXT,
        exit_reason TEXT,
        close_reason TEXT,
        sector TEXT,
        roll_from_id INTEGER,
        put_sell REAL,
        put_buy REAL,
        call_sell REAL,
        call_buy REAL,
        put_credit REAL,
        call_credit REAL,
        pnr_alert_enabled INTEGER DEFAULT 1,
        pnr_alert_last_breached INTEGER DEFAULT 0,
        pnr_alert_last_sent_at TEXT
        );
        CREATE TABLE IF NOT EXISTS trade_health_alert_state (
            trade_id INTEGER PRIMARY KEY,
            symbol TEXT,
            trade_type TEXT,
            severity TEXT,
            action TEXT,
            score INTEGER,
            pnr_breached INTEGER DEFAULT 0,
            signature TEXT,
            last_reason TEXT,
            last_sent_at TEXT,
            last_observed_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_trade_health_alert_state_symbol
            ON trade_health_alert_state(symbol);
        CREATE INDEX IF NOT EXISTS idx_trade_health_alert_state_updated
            ON trade_health_alert_state(updated_at);
        CREATE TABLE IF NOT EXISTS trade_position_alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            severity TEXT DEFAULT 'warning',
            benchmark TEXT DEFAULT 'SPY',
            bull_condition TEXT DEFAULT '',
            bear_condition TEXT DEFAULT '',
            neutral_condition TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS trade_position_alert_state (
            trade_id INTEGER NOT NULL,
            rule_id INTEGER NOT NULL,
            symbol TEXT,
            trade_type TEXT,
            side TEXT,
            last_match INTEGER DEFAULT 0,
            last_trigger_date TEXT,
            last_signature TEXT,
            last_reason TEXT,
            last_sent_at TEXT,
            last_observed_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (trade_id, rule_id)
        );
        CREATE INDEX IF NOT EXISTS idx_trade_position_alert_rules_enabled
            ON trade_position_alert_rules(enabled);
        CREATE INDEX IF NOT EXISTS idx_trade_position_alert_state_symbol
            ON trade_position_alert_state(symbol);
        CREATE INDEX IF NOT EXISTS idx_trade_position_alert_state_sent
            ON trade_position_alert_state(last_sent_at);
        CREATE TABLE IF NOT EXISTS market_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL,
            source TEXT,
            headline TEXT NOT NULL,
            summary TEXT,
            url TEXT,
            symbol TEXT,
            sentiment TEXT,
            category TEXT
        );
        CREATE TABLE IF NOT EXISTS morning_digest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_date TEXT NOT NULL UNIQUE,
            oi_changes TEXT,
            market_summary TEXT,
            top_signals TEXT,
            generated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sector_cache (
            symbol TEXT PRIMARY KEY,
            sector TEXT,
            industry TEXT,
            updated TEXT
        );
        CREATE TABLE IF NOT EXISTS symbol_fundamentals (
            symbol TEXT PRIMARY KEY,
            beta REAL,
            source TEXT,
            updated TEXT
        );
        CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
            symbol TEXT PRIMARY KEY,
            pe_fwd REAL,
            pe_trailing REAL,
            profit_margin REAL,
            revenue_growth REAL,
            earnings_growth REAL,
            rec_mean REAL,
            analyst_target REAL,
            analyst_upside_pct REAL,
            eps_revision_trend TEXT,
            eps_revision_chg_pct REAL,
            eps_revision_net_30d INTEGER,
            beat_rate_pct REAL,
            avg_eps_surprise_pct REAL,
            last_post_earnings_move_pct REAL,
            last_pull_date TEXT,
            last_known_next_earn_date TEXT,
            fetch_ts TEXT,
            fetch_error TEXT
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS scanner_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            query_text TEXT NOT NULL,
            builder_json TEXT DEFAULT '[]',
            watchlist_id INTEGER,
            benchmark TEXT DEFAULT 'SPY',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            last_run_at TEXT,
            last_run_count INTEGER DEFAULT 0,
            last_results_json TEXT,
            last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS scanner_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            definition_id INTEGER,
            run_at TEXT DEFAULT (datetime('now')),
            watchlist_id INTEGER,
            benchmark TEXT DEFAULT 'SPY',
            query_text TEXT,
            result_count INTEGER DEFAULT 0,
            results_json TEXT,
            error_text TEXT
        );
        CREATE TABLE IF NOT EXISTS saved_scanner_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanner_key TEXT NOT NULL,
            run_name TEXT NOT NULL,
            watchlist_id INTEGER,
            symbol TEXT,
            summary_json TEXT,
            payload_json TEXT NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(scanner_key, run_name)
        );
    """)
    con.commit()

    # ── Safe migrations for existing DBs (ALTER TABLE is idempotent via try/except) ──
    migrations = [
        "ALTER TABLE trades ADD COLUMN trade_subtype TEXT DEFAULT 'vertical'",
        "ALTER TABLE trades ADD COLUMN close_reason TEXT",
        "ALTER TABLE trades ADD COLUMN sector TEXT",
        "ALTER TABLE trades ADD COLUMN roll_from_id INTEGER",
        # IC-specific strike columns
        "ALTER TABLE trades ADD COLUMN put_sell REAL",
        "ALTER TABLE trades ADD COLUMN put_buy REAL",
        "ALTER TABLE trades ADD COLUMN call_sell REAL",
        "ALTER TABLE trades ADD COLUMN call_buy REAL",
        "ALTER TABLE trades ADD COLUMN put_credit REAL",
        "ALTER TABLE trades ADD COLUMN call_credit REAL",
        "ALTER TABLE trades ADD COLUMN current_pnl REAL",
        "ALTER TABLE trades ADD COLUMN outlook TEXT",
        "ALTER TABLE trades ADD COLUMN spot_price REAL",
        "ALTER TABLE trades ADD COLUMN prob_score INTEGER",
        "ALTER TABLE trades ADD COLUMN analytics_rec TEXT",
        "ALTER TABLE trades ADD COLUMN analytics_json TEXT",
        "ALTER TABLE trades ADD COLUMN legs_json TEXT",
        "ALTER TABLE trades ADD COLUMN num_legs INTEGER DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN net_premium REAL",
        "ALTER TABLE trades ADD COLUMN pnr_alert_enabled INTEGER DEFAULT 1",
        "ALTER TABLE trades ADD COLUMN pnr_alert_last_breached INTEGER DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN pnr_alert_last_sent_at TEXT",
        "ALTER TABLE trades ADD COLUMN deep_loss_alert_last_breached INTEGER DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN deep_loss_alert_last_sent_at TEXT",
        "ALTER TABLE trades ADD COLUMN stop_loss_price REAL",
        "ALTER TABLE trades ADD COLUMN target_price REAL",
        "ALTER TABLE trades ADD COLUMN sl_tp_alert_last_state TEXT",  # NULL/'none' | 'sl_hit' | 'tp_hit' -- prevents re-firing every scan
        "ALTER TABLE trades ADD COLUMN sl_tp_alert_last_sent_at TEXT",
        # Trade snapshot / entry score columns
        "ALTER TABLE trades ADD COLUMN entry_score INTEGER",
        "ALTER TABLE trades ADD COLUMN entry_grade TEXT",
        "ALTER TABLE trades ADD COLUMN entry_recommendation TEXT",
        "ALTER TABLE options ADD COLUMN price REAL",
        "ALTER TABLE app_settings ADD COLUMN value TEXT",
        "ALTER TABLE scanner_definitions ADD COLUMN builder_json TEXT DEFAULT '[]'",
        "ALTER TABLE scanner_definitions ADD COLUMN watchlist_id INTEGER",
        "ALTER TABLE scanner_definitions ADD COLUMN benchmark TEXT DEFAULT 'SPY'",
        "ALTER TABLE scanner_definitions ADD COLUMN last_run_at TEXT",
        "ALTER TABLE scanner_definitions ADD COLUMN last_run_count INTEGER DEFAULT 0",
        "ALTER TABLE scanner_definitions ADD COLUMN last_results_json TEXT",
        "ALTER TABLE scanner_definitions ADD COLUMN last_error TEXT",
        "ALTER TABLE symbol_fundamentals ADD COLUMN beta REAL",
        "ALTER TABLE symbol_fundamentals ADD COLUMN source TEXT",
        "ALTER TABLE symbol_fundamentals ADD COLUMN updated TEXT",
        "ALTER TABLE fundamentals_snapshot ADD COLUMN beat_rate_pct REAL",
        "ALTER TABLE fundamentals_snapshot ADD COLUMN avg_eps_surprise_pct REAL",
        "ALTER TABLE fundamentals_snapshot ADD COLUMN last_post_earnings_move_pct REAL",
    ]
    # Regime scan column migrations (safe — ignored if already exist)
    for _sql in [
        "ALTER TABLE regime_scan ADD COLUMN earn_date TEXT",
        "ALTER TABLE regime_scan ADD COLUMN earn_score INTEGER DEFAULT 0",
        "ALTER TABLE regime_scan ADD COLUMN beta REAL",
    ]:
        try: cur.execute(_sql)
        except: pass

    # Regime scan table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS regime_scan (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_date    TEXT NOT NULL,
            symbol       TEXT NOT NULL,
            regime       TEXT,
            confidence   INTEGER,
            bias         TEXT,
            rsi          REAL,
            rsi_diff     REAL,
            macd         TEXT,
            adx          REAL,
            ema_trend    TEXT,
            momentum_move TEXT,
            post_earnings INTEGER DEFAULT 0,
            earn_days    INTEGER,
            earn_date    TEXT,
            earn_score   INTEGER DEFAULT 0,
            beta         REAL,
            signals_json TEXT,
            updated      TEXT,
            UNIQUE(scan_date, symbol)
        )""")
    for m in migrations:
        try: cur.execute(m)
        except: pass  # column already exists — safe to ignore
    con.commit()

    # Smart money accumulation/distribution scan history (persists results
    # from institutional_scanner.py and smart_money_distribution_scanner.py
    # over time, for hit-rate backtesting -- additive only, never touches
    # existing tables/columns).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS smart_money_scan_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_date    TEXT NOT NULL,
            symbol       TEXT NOT NULL,
            mode         TEXT NOT NULL,   -- 'accumulation' or 'distribution'
            score        REAL,
            signal       TEXT,
            udvr         REAL,
            dist_day_count INTEGER,
            extra_json   TEXT,
            created_at   TEXT DEFAULT (datetime('now')),
            UNIQUE(scan_date, symbol, mode)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_smart_money_symbol_mode ON smart_money_scan_history(symbol, mode)")
    con.commit()

    # Refresh query planner statistics for options immediately, so the new
    # indexes above actually get used right away rather than waiting on
    # SQLite's own internal heuristics on a table that may already be large.
    try:
        cur.execute("ANALYZE options")
        con.commit()
    except Exception:
        pass
    con.close()

def get_symbols():
    con = _connect()
    rows = [r["symbol"] for r in con.execute("SELECT symbol FROM symbols ORDER BY symbol")]
    con.close(); return rows

def save_symbols(symbols):
    if READ_ONLY_MODE:
        return
    con = _connect(); cur = con.cursor()
    cur.execute("DELETE FROM symbols")
    for s in symbols:
        if s:
            cur.execute("INSERT OR IGNORE INTO symbols(symbol) VALUES (?)", (s,))
    con.commit(); con.close()

def delete_symbol(symbol: str):
    if READ_ONLY_MODE:
        return
    con = _connect(); cur = con.cursor()
    cur.execute("DELETE FROM symbols WHERE symbol=?", (symbol,))
    cur.execute("DELETE FROM options WHERE symbol=?", (symbol,))
    con.commit(); con.close()

# Global write lock — prevents concurrent SQLite write conflicts
import threading as _threading
import time as _time
_db_write_lock = _threading.RLock()


def _is_sqlite_locked(exc):
    return isinstance(exc, sqlite3.OperationalError) and any(tok in str(exc).lower() for tok in ('locked', 'busy'))


def _retry_locked_write(fn, attempts=20, base_delay=0.20):
    last = None
    for i in range(max(1, int(attempts or 1))):
        try:
            with _db_write_lock:
                return fn()
        except Exception as exc:
            if not _is_sqlite_locked(exc):
                raise
            last = exc
            _time.sleep(base_delay * (i + 1))
    if last:
        raise last



def store_option_chain(symbol, expiration, opt_chain, underlying=None):
    import math
    today = datetime.now().strftime("%Y-%m-%d")
    fetch_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _finite_float(value, default=None):
        try:
            f = float(value)
            if math.isnan(f) or math.isinf(f):
                return default
            return f
        except Exception:
            return default

    def _finite_int(value, default=0):
        f = _finite_float(value, None)
        if f is None:
            return int(default)
        try:
            return int(f)
        except Exception:
            return int(default)

    underlying_val = _finite_float(underlying, None)

    def _iter_rows(df):
        if df is None:
            return []
        try:
            if getattr(df, "empty", False):
                return []
            if "strike" in getattr(df, "columns", []):
                df = df[df["strike"].notna()]
            try:
                df = df.where(df.notna(), None)
            except Exception:
                pass
            return list(df.iterrows())
        except Exception:
            return []

    def _build_rows(df, typ):
        rows = []
        for _, r in _iter_rows(df):
            strike = _finite_float(r.get("strike"), None)
            if strike is None or strike <= 0:
                continue
            oi = _finite_int(r.get("openInterest", 0), 0)
            vol = _finite_int(r.get("volume", 0), 0)
            if oi <= 0:
                continue
            bid = _finite_float(r.get("bid"), None)
            ask = _finite_float(r.get("ask"), None)
            last = _finite_float(r.get("lastPrice"), None)
            iv = _finite_float(r.get("impliedVolatility"), None)
            if iv is not None and iv <= 0:
                iv = None
            px = round((bid + ask) / 2, 2) if (bid is not None and ask is not None and bid > 0 and ask > 0) else (round(last, 2) if last is not None and last > 0 else None)
            rows.append((symbol, expiration, typ, float(strike), px, oi, vol, today, bid if bid and bid > 0 else None, ask if ask and ask > 0 else None, last if last and last > 0 else None, iv, underlying_val, fetch_ts))
        return rows

    call_rows = _build_rows(getattr(opt_chain, "calls", None), "call")
    put_rows  = _build_rows(getattr(opt_chain, "puts", None),  "put")
    all_rows  = call_rows + put_rows

    if READ_ONLY_MODE:
        return
    with _db_write_lock:   # serialize all DB writes — no more lock errors
        con = _connect()
        try:
            _ensure_options_enrichment_columns(con)
            con.execute("DELETE FROM options WHERE symbol=? AND expiration=? AND date=?",
                        (symbol, expiration, today))
            if all_rows:
                con.executemany(
                    "INSERT INTO options(symbol,expiration,type,strike,price,oi,volume,date,bid,ask,last,iv,underlying,fetch_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    all_rows
                )
            con.commit()
        finally:
            con.close()


def get_two_latest_dates(symbol, expiration):
    """Return the two most recent dates for a symbol + expiration."""
    con = _connect()
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute("""
        SELECT DISTINCT date
        FROM options
        WHERE symbol = ? AND expiration = ?
        ORDER BY date DESC
        LIMIT 2
    """, (symbol, expiration)).fetchall()
    con.close()
    if len(rows) < 2:
        return None, None
    return rows[0]["date"], rows[1]["date"]

def get_expirationOI_date(symbol, expiration, dt):
    """Fetch all option OI data for a given symbol, expiration, and date."""
    con = _connect()
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute("""
        SELECT symbol, expiration, type, strike, price, oi, volume, date
        FROM options
        WHERE symbol = ? AND expiration = ? AND date = ?
    """, (symbol, expiration, dt)).fetchall()
    con.close()
    return [dict(r) for r in rows]

def get_oi_fromdb(symbol, expiration):
    con = _connect(); cur = con.cursor()
    row = cur.execute("SELECT MAX(date) AS d FROM options WHERE symbol=? AND expiration=?",
                      (symbol, expiration)).fetchone()
    if not row or not row["d"]:
        con.close(); return []
    d = row["d"]
    rows = cur.execute("""SELECT type, strike, price, oi, volume, date
                          FROM options WHERE symbol=? AND expiration=? AND date=?""",                       (symbol, expiration, d)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _ensure_fetch_attempts_table(con=None):
    """Tracks fetch attempts (including failures/timeouts), separate from
    the actual options data table. Without this, a symbol that times out
    once has no record of that fact -- has_option_chain_for_date() still
    correctly returns False for it (no data was ever stored), so it gets
    retried and re-times-out on every single app restart for the rest of
    the day, which is the actual cause of "very slow on restart" -- the
    same chronically-slow/failing symbols get re-attempted every time."""
    own = con is None
    c = con or _connect()
    try:
        c.execute("""
            CREATE TABLE IF NOT EXISTS fetch_attempts (
                symbol      TEXT NOT NULL,
                expiration  TEXT NOT NULL,
                date        TEXT NOT NULL,
                status      TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                PRIMARY KEY (symbol, expiration, date)
            )
        """)
        if own:
            c.commit()
    finally:
        if own:
            c.close()


def has_fetch_been_attempted_today(symbol, expiration, date_value=None):
    """True if we already tried (successfully or not) to fetch this
    symbol+expiration today -- used to skip re-attempting a fetch that
    already failed/timed out earlier today, rather than only skipping
    fetches that already *succeeded*."""
    if date_value is None:
        date_value = datetime.now().strftime("%Y-%m-%d")
    con = _connect()
    try:
        _ensure_fetch_attempts_table(con)
        row = con.execute(
            "SELECT status FROM fetch_attempts WHERE symbol=? AND expiration=? AND date=? LIMIT 1",
            (symbol, expiration, date_value),
        ).fetchone()
        return (row[0] if row else None)
    finally:
        con.close()


def record_fetch_attempt(symbol, expiration, status, date_value=None):
    """Records that a fetch was attempted today, and whether it succeeded,
    failed, or timed out. `status` should be one of 'success', 'timeout',
    'error'. Overwrites any earlier attempt the same day (e.g. if a manual
    re-run later succeeds after an earlier timeout)."""
    if date_value is None:
        date_value = datetime.now().strftime("%Y-%m-%d")
    con = _connect()
    try:
        _ensure_fetch_attempts_table(con)
        con.execute(
            """INSERT INTO fetch_attempts (symbol, expiration, date, status, attempted_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(symbol, expiration, date) DO UPDATE SET
                 status=excluded.status, attempted_at=excluded.attempted_at""",
            (symbol, expiration, date_value, status, datetime.now().isoformat()),
        )
        con.commit()
    finally:
        con.close()


def has_option_chain_for_date(symbol, expiration, date_value=None):
    """Return True when the database already has option rows for the requested date."""
    if date_value is None:
        date_value = datetime.now().strftime("%Y-%m-%d")
    con = _connect()
    try:
        row = con.execute(
            "SELECT 1 FROM options WHERE symbol=? AND expiration=? AND date=? LIMIT 1",
            (symbol, expiration, date_value),
        ).fetchone()
        return bool(row)
    finally:
        con.close()


def _ensure_saved_scanner_runs_table(con=None):
    if READ_ONLY_MODE:
        return
    own = con is None

    def _op():
        c = con or _connect()
        try:
            if own:
                c.execute("BEGIN IMMEDIATE")
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_scanner_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scanner_key TEXT NOT NULL,
                    run_name TEXT NOT NULL,
                    watchlist_id INTEGER,
                    symbol TEXT,
                    summary_json TEXT,
                    payload_json TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(scanner_key, run_name)
                )
                """
            )
            if own:
                c.commit()
        finally:
            if own:
                c.close()

    if own:
        return _retry_locked_write(_op)
    return _op()


def save_saved_scanner_run(scanner_key, run_name, payload, watchlist_id=None, symbol=None, summary=None, note=None):
    if READ_ONLY_MODE:
        return None
    import json as _json
    scanner_key = str(scanner_key or '').strip().lower()
    run_name = str(run_name or '').strip()
    if not scanner_key or not run_name:
        raise ValueError('scanner_key and run_name are required')

    def _op():
        con = _connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            _ensure_saved_scanner_runs_table(con)
            con.execute(
                """
                INSERT INTO saved_scanner_runs(scanner_key, run_name, watchlist_id, symbol, summary_json, payload_json, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(scanner_key, run_name) DO UPDATE SET
                    watchlist_id=excluded.watchlist_id,
                    symbol=excluded.symbol,
                    summary_json=excluded.summary_json,
                    payload_json=excluded.payload_json,
                    note=excluded.note,
                    updated_at=datetime('now')
                """,
                (
                    scanner_key,
                    run_name,
                    int(watchlist_id) if watchlist_id not in (None, '', 0) else None,
                    str(symbol).upper() if symbol else None,
                    _json.dumps(summary if summary is not None else {}, default=str),
                    _json.dumps(payload if payload is not None else {}, default=str),
                    note,
                ),
            )
            row = con.execute(
                "SELECT * FROM saved_scanner_runs WHERE scanner_key=? AND run_name=?",
                (scanner_key, run_name),
            ).fetchone()
            con.commit()
            return dict(row) if row else None
        finally:
            con.close()

    return _retry_locked_write(_op, attempts=20, base_delay=0.20)


def list_saved_scanner_runs(scanner_key=None, limit=50, run_name=None, date_from=None, date_to=None, watchlist_id=None, symbol=None):
    import json as _json
    con = _connect()
    try:
        clauses = []
        params = []
        if scanner_key:
            clauses.append('scanner_key=?')
            params.append(str(scanner_key).strip().lower())
        if run_name:
            clauses.append('run_name LIKE ?')
            params.append(f"%{str(run_name).strip()}%")
        if date_from:
            clauses.append("datetime(COALESCE(updated_at, created_at)) >= datetime(?)")
            params.append(str(date_from).strip())
        if date_to:
            clauses.append("datetime(COALESCE(updated_at, created_at)) <= datetime(?)")
            params.append(str(date_to).strip() + ' 23:59:59')
        if watchlist_id not in (None, '', 'all'):
            clauses.append('watchlist_id=?')
            params.append(int(watchlist_id))
        if symbol:
            clauses.append('UPPER(COALESCE(symbol, "")) LIKE ?')
            params.append(f"%{str(symbol).strip().upper()}%")
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = con.execute(
            f"SELECT * FROM saved_scanner_runs {where} ORDER BY datetime(updated_at) DESC, id DESC LIMIT ?",
            params + [max(1, min(200, int(limit or 50)))],
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d['summary'] = _json.loads(d.get('summary_json') or '{}')
            try:
                payload = _json.loads(d.get('payload_json') or '{}')
            except Exception:
                payload = {}
            d['payload'] = payload
            d['result_count'] = payload.get('count') or len(payload.get('results') or payload.get('items') or [])
            out.append(d)
        return out
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc).lower():
            return []
        raise
    finally:
        con.close()


def get_saved_scanner_run(run_id=None, scanner_key=None, run_name=None):
    import json as _json
    con = _connect()
    try:
        row = None
        if run_id is not None:
            row = con.execute('SELECT * FROM saved_scanner_runs WHERE id=?', (int(run_id),)).fetchone()
        elif scanner_key and run_name:
            row = con.execute('SELECT * FROM saved_scanner_runs WHERE scanner_key=? AND run_name=?', (str(scanner_key).strip().lower(), str(run_name).strip())).fetchone()
        if not row:
            return None
        d = dict(row)
        d['summary'] = _json.loads(d.get('summary_json') or '{}')
        try:
            d['payload'] = _json.loads(d.get('payload_json') or '{}')
        except Exception:
            d['payload'] = {}
        return d
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc).lower():
            return None
        raise
    finally:
        con.close()

def get_expirations_for_symbol(symbol):
    con = _connect(); cur = con.cursor()
    rows = cur.execute("""SELECT DISTINCT expiration FROM options
                          WHERE symbol=? ORDER BY expiration""", (symbol,)).fetchall()
    con.close()
    return [r["expiration"] for r in rows]


def run_select_query(query: str):
    """Execute any SQL — SELECT returns rows/cols, DML returns rowcount."""
    print("Inside run_select_query:", query[:60])
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(query)
        q = query.strip().upper()
        # For SELECT / PRAGMA — return columns + rows
        if cursor.description is not None:
            cols = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            conn.close()
            return {"columns": cols, "rows": rows}
        # For DELETE / INSERT / UPDATE / DROP / CREATE — commit and return affected rows
        conn.commit()
        rowcount = cursor.rowcount
        conn.close()
        verb = q.split()[0] if q else "SQL"
        return {
            "columns": ["result"],
            "rows":    [[f"{verb} OK — {rowcount} row(s) affected"]]
        }
    except Exception as e:
        return {"error": str(e)}
    
def log_scheduler_run(symbols, status="success", message=""):
    """Insert a log entry for each scheduler run."""
    if READ_ONLY_MODE:
        return
    try:
        # Normalize symbols input to string
        if isinstance(symbols, (list, tuple, set)):
            symbols_str = ",".join([str(s) for s in symbols])
        elif isinstance(symbols, str):
            symbols_str = symbols
        else:
            symbols_str = str(symbols or "")

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO scheduler_log (run_time, symbols, status, message)
            VALUES (?, ?, ?, ?)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            symbols_str,
            str(status),
            str(message)
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Scheduler Log Error] {e}")



def save_smart_money_scan_result(symbol, mode, score=None, signal=None, udvr=None,
                                  dist_day_count=None, extra=None, scan_date=None):
    """Persist one accumulation/distribution scan hit into
    smart_money_scan_history, so hit-rate/forward-return backtesting is
    possible later without re-running scans against historical dates.
    mode should be 'accumulation' or 'distribution'.
    """
    if READ_ONLY_MODE:
        return
    import json as _json
    scan_date = scan_date or datetime.now().strftime("%Y-%m-%d")
    con = _connect()
    try:
        con.execute(
            """
            INSERT INTO smart_money_scan_history
                (scan_date, symbol, mode, score, signal, udvr, dist_day_count, extra_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(scan_date, symbol, mode) DO UPDATE SET
                score=excluded.score,
                signal=excluded.signal,
                udvr=excluded.udvr,
                dist_day_count=excluded.dist_day_count,
                extra_json=excluded.extra_json,
                created_at=datetime('now')
            """,
            (
                scan_date, str(symbol).upper(), str(mode),
                float(score) if score is not None else None,
                signal,
                float(udvr) if udvr is not None else None,
                int(dist_day_count) if dist_day_count is not None else None,
                _json.dumps(extra if extra is not None else {}, default=str),
            ),
        )
        con.commit()
    finally:
        con.close()


def get_smart_money_scan_history(symbol=None, mode=None, date_from=None, date_to=None, limit=500):
    """Read back persisted scan hits for backtesting/analysis. Filters are
    all optional; omit everything to get the most recent rows across all
    symbols/modes."""
    con = _connect()
    try:
        clauses = []
        params = []
        if symbol:
            clauses.append("symbol=?")
            params.append(str(symbol).upper())
        if mode:
            clauses.append("mode=?")
            params.append(str(mode))
        if date_from:
            clauses.append("scan_date >= ?")
            params.append(str(date_from))
        if date_to:
            clauses.append("scan_date <= ?")
            params.append(str(date_to))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = con.execute(
            f"SELECT * FROM smart_money_scan_history {where} ORDER BY scan_date DESC, id DESC LIMIT ?",
            params + [max(1, min(5000, int(limit or 500)))],
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise
    finally:
        con.close()


# Safe migration note:
# Future schema changes should avoid DROP TABLE / destructive DELETEs.
# If a table restructure is required, copy data into a backup table first,
# verify it, then swap references only after the backup is confirmed.
