"""
watchlist_manager.py — Multi-watchlist management with options OI fetch flag.

Tables:
  watchlists        — id, name, description, fetch_options_oi, color, created_at
  watchlist_symbols — watchlist_id, symbol, added_at
  
The legacy `symbols` table is kept for backwards compatibility but the scheduler
now reads from watchlists where fetch_options_oi=1.
"""
import sqlite3
from pathlib import Path
from datetime import datetime
import re
from flask import Blueprint, jsonify, request

wl_bp = Blueprint("wl_bp", __name__, url_prefix="/watchlists")
DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")

# ── DB helpers ─────────────────────────────────────────────────────────────
def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

def _ensure_tables():
    con = _conn()
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
            last_fetch_count INTEGER DEFAULT 0
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
    # Migrate: if "Options Watchlist" doesn't exist yet, seed it from symbols table
    existing = con.execute("SELECT id FROM watchlists WHERE name='Options Watchlist'").fetchone()
    # Add alert columns for existing DBs
    for _ddl in (
        "ALTER TABLE watchlist_symbols ADD COLUMN alert_price REAL",
        "ALTER TABLE watchlist_symbols ADD COLUMN alert_direction TEXT DEFAULT 'both'",
        "ALTER TABLE watchlist_symbols ADD COLUMN alert_enabled INTEGER DEFAULT 0",
        "ALTER TABLE watchlist_symbols ADD COLUMN alert_last_price REAL",
        "ALTER TABLE watchlist_symbols ADD COLUMN alert_last_side TEXT",
        "ALTER TABLE watchlist_symbols ADD COLUMN alert_last_sent_at TEXT",
    ):
        try: con.execute(_ddl)
        except: pass
    # Add is_default column if it doesn't exist yet (migration for existing DBs)
    try: con.execute("ALTER TABLE watchlists ADD COLUMN is_default INTEGER DEFAULT 0")
    except: pass
    try: con.execute("ALTER TABLE watchlists ADD COLUMN last_fetch_at TEXT")
    except: pass
    try: con.execute("ALTER TABLE watchlists ADD COLUMN last_fetch_mode TEXT")
    except: pass
    try: con.execute("ALTER TABLE watchlists ADD COLUMN last_fetch_count INTEGER DEFAULT 0")
    except: pass
    if not existing:
        con.execute("""
            INSERT OR IGNORE INTO watchlists (name, description, fetch_options_oi, is_default, color)
            VALUES ('Options Watchlist', 'Symbols tracked for Options OI data', 1, 1, '#22c55e')
        """)
        wl_id = con.execute("SELECT id FROM watchlists WHERE name='Options Watchlist'").fetchone()[0]
        # Copy existing symbols table into Options Watchlist
        syms = con.execute("SELECT symbol FROM symbols WHERE symbol IS NOT NULL").fetchall()
        for row in syms:
            con.execute("INSERT OR IGNORE INTO watchlist_symbols (watchlist_id, symbol) VALUES (?,?)",
                        (wl_id, row[0]))
        con.commit()
    con.commit(); con.close()


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


def _fetch_live_prices(symbols):
    """Return dict symbol->spot using threads to keep the panel responsive."""
    symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not symbols:
        return {}
    prices = {}
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ..services.market import get_spot
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as ex:
            futs = {ex.submit(get_spot, sym): sym for sym in symbols}
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    val = fut.result()
                except Exception:
                    val = None
                prices[sym] = val
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


# ── REST API ───────────────────────────────────────────────────────────────
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


@wl_bp.route("/<int:wl_id>", methods=["PUT"])
def update_watchlist(wl_id):
    """Update watchlist metadata."""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    con = _conn()
    fields = []
    vals   = []
    for col in ["name","description","fetch_options_oi","color"]:
        if col in d:
            fields.append(f"{col}=?")
            vals.append(1 if (col=="fetch_options_oi" and d[col]) else d[col])
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
    """Return symbols in a watchlist."""
    _ensure_tables()
    con = _conn()
    rows = con.execute(
        "SELECT symbol, added_at, alert_price, alert_direction, alert_enabled, alert_last_price, alert_last_side, alert_last_sent_at FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
        (wl_id,)
    ).fetchall()
    items = [dict(r) for r in rows]
    con.close()
    live = _fetch_live_prices([r['symbol'] for r in items]) if items else {}
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


@wl_bp.route("/<int:wl_id>/symbols/<sym>/alert", methods=["POST"])
def set_symbol_alert(wl_id, sym):
    """Set alert price/direction for one symbol in a watchlist."""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    try:
        alert_price = float(d.get("alert_price")) if d.get("alert_price") not in (None, "") else None
    except Exception:
        return jsonify({"error": "alert_price must be numeric"}), 400
    direction = (d.get("alert_direction") or "both").strip().lower()
    if direction not in {"above", "below", "both"}:
        return jsonify({"error": "alert_direction must be above, below, or both"}), 400
    enabled = 1 if d.get("alert_enabled", True) else 0
    con = _conn()
    con.execute("""
        UPDATE watchlist_symbols
        SET alert_price=?, alert_direction=?, alert_enabled=?
        WHERE watchlist_id=? AND UPPER(symbol)=UPPER(?)
    """, (alert_price, direction, enabled, wl_id, sym))
    con.commit(); con.close()
    return jsonify({"ok": True, "watchlist_id": wl_id, "symbol": sym.upper(),
                    "alert_price": alert_price, "alert_direction": direction, "alert_enabled": enabled})


@wl_bp.route("/import_scan_results", methods=["POST"])
def import_scan_results():
    """Create a new watchlist or append to an existing one from scanner results."""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    symbols = []
    if d.get("symbols"):
        symbols = [str(s).strip().upper() for s in d.get("symbols") if str(s).strip()]
    elif d.get("results"):
        for row in d.get("results") or []:
            sym = str((row or {}).get("symbol", "")).strip().upper()
            if sym:
                symbols.append(sym)
    symbols = sorted(set(symbols))
    if not symbols:
        return jsonify({"error": "No symbols to import"}), 400

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
        return jsonify({"ok": True, "watchlist_id": wl_id, "watchlist_name": wl_name, "added": added, "total": len(symbols)})
    finally:
        con.close()


@wl_bp.route("/<int:wl_id>/symbols", methods=["POST"])
def add_symbols(wl_id):
    """Add symbols to a watchlist. Body: {symbols: ['AAPL','MSFT',...]}"""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    symbols = [s.strip().upper() for s in (d.get("symbols") or []) if s.strip()]
    if not symbols:
        return jsonify({"error": "no symbols provided"}), 400
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
    return jsonify({"ok": True, "added": added, "total": len(symbols)})


@wl_bp.route("/<int:wl_id>/symbols/<sym>", methods=["DELETE"])
def remove_symbol(wl_id, sym):
    """Remove a symbol from a watchlist."""
    _ensure_tables()
    con = _conn()
    con.execute("DELETE FROM watchlist_symbols WHERE watchlist_id=? AND symbol=?",
                (wl_id, sym.upper()))
    _sync_symbols_table(con)
    con.commit(); con.close()
    return jsonify({"ok": True})


@wl_bp.route("/<int:wl_id>/symbols/bulk_delete", methods=["POST"])
def bulk_remove_symbols(wl_id):
    """Remove multiple symbols. Body: {symbols: ['AAPL',...]}"""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    symbols = [s.strip().upper() for s in (d.get("symbols") or []) if s.strip()]
    con = _conn()
    for sym in symbols:
        con.execute("DELETE FROM watchlist_symbols WHERE watchlist_id=? AND symbol=?",
                    (wl_id, sym))
    _sync_symbols_table(con)
    con.commit(); con.close()
    return jsonify({"ok": True, "removed": len(symbols)})


@wl_bp.route("/<int:wl_id>/symbols/replace", methods=["POST"])
def replace_symbols(wl_id):
    """Replace all symbols in a watchlist. Body: {symbols: ['AAPL','MSFT',...]}"""
    _ensure_tables()
    d = request.get_json(force=True) or {}
    symbols = [s.strip().upper() for s in (d.get("symbols") or []) if s.strip()]
    con = _conn()
    con.execute("DELETE FROM watchlist_symbols WHERE watchlist_id=?", (wl_id,))
    for sym in symbols:
        con.execute("INSERT OR IGNORE INTO watchlist_symbols (watchlist_id, symbol) VALUES (?,?)",
                    (wl_id, sym))
    _sync_symbols_table(con)
    con.commit(); con.close()
    return jsonify({"ok": True, "count": len(symbols)})


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

@wl_bp.route("/<int:wl_id>/fetch_data", methods=["POST"])
def fetch_data_for_watchlist(wl_id):
    """
    Trigger Options OI fetch OR price-only fetch for a specific watchlist.
    Options OI → stored in `options` table.
    Price-only  → stored in `price_cache` table.
    Runs in background thread; returns immediately.
    """
    import threading, datetime as _dt
    _ensure_tables()
    con = _conn()
    wl = con.execute(
        "SELECT name, fetch_options_oi FROM watchlists WHERE id=?", (wl_id,)
    ).fetchone()
    if not wl:
        con.close()
        return jsonify({"error": "Watchlist not found"}), 404
    wl_name, fetch_oi = wl[0], bool(wl[1])
    syms = [r[0] for r in con.execute(
        "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol", (wl_id,)
    ).fetchall()]
    con.close()

    if not syms:
        return jsonify({"error": "No symbols in this watchlist"}), 400

    import time as _time
    def _run():
        if fetch_oi:
            from ..services.market import get_expirations, fetch_store_for
            import time
            for sym in syms:
                try:
                    exps = get_expirations(sym)[:10]
                    if exps: fetch_store_for(sym, exps)
                    time.sleep(0.3)
                except: pass
        else:
            import yfinance as yf, sqlite3 as sq
            from pathlib import Path as P
            db = str(P(__file__).resolve().parents[2] / "options_data.db")
            today = _dt.date.today().isoformat()
            for sym in syms:
                try:
                    hist = yf.Ticker(sym).history(period="2d")
                    if hist is None or hist.empty: continue
                    r = hist.iloc[-1]
                    c = sq.connect(db)
                    c.execute("""CREATE TABLE IF NOT EXISTS price_cache (
                        symbol TEXT NOT NULL, date TEXT NOT NULL,
                        open REAL, high REAL, low REAL, close REAL, volume INTEGER,
                        PRIMARY KEY (symbol, date))""")
                    c.execute("INSERT OR REPLACE INTO price_cache VALUES (?,?,?,?,?,?,?)",
                              (sym, today, round(float(r["Open"]),4), round(float(r["High"]),4),
                               round(float(r["Low"]),4), round(float(r["Close"]),4), int(r["Volume"])))
                    c.commit(); c.close()
                except: pass

    def _run_and_log():
        _run()
        # Update last_fetch info
        try:
            import sqlite3 as _sq, datetime as _dtt
            _db = str(__import__('pathlib').Path(__file__).resolve().parents[2] / "options_data.db")
            _c  = _sq.connect(_db)
            _c.execute("""UPDATE watchlists SET last_fetch_at=?, last_fetch_mode=?, last_fetch_count=?
                          WHERE id=?""",
                       (_dtt.datetime.now().strftime("%Y-%m-%d %H:%M"), mode, len(syms), wl_id))
            _c.commit(); _c.close()
        except: pass

    t = threading.Thread(target=_run_and_log, daemon=True); t.start()
    table = "options" if fetch_oi else "price_cache"
    mode  = "Options OI" if fetch_oi else "Price/Volume"
    return jsonify({"ok": True, "watchlist": wl_name, "symbols": len(syms),
                    "mode": mode, "table": table, "status": "started"})


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
    return jsonify({"watchlist": wl_name, "symbols_total": len(syms),
                    "fetched_today": count, "table": table})


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
            CREATE INDEX IF NOT EXISTS idx_alert_rules_enabled ON alert_rules(enabled);
            CREATE INDEX IF NOT EXISTS idx_alert_rules_watchlist ON alert_rules(watchlist_id);
            CREATE INDEX IF NOT EXISTS idx_alert_rules_symbol ON alert_rules(symbol);
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
            trigger_mode = (rule.get('trigger_mode') or 'once').strip().lower()
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

            if kind == 'price':
                op = rule.get('price_operator') or '>='
                threshold = rule.get('price_value')
                if threshold is None:
                    continue
                for sym in rule_symbols:
                    px = _spot(sym)
                    if px is None:
                        continue
                    if _compare_price(px, op, threshold):
                        detail = f"{sym} spot {px:.2f} {op} {float(threshold):g}"
                        log_alert_notification('PRICE', name, detail, symbol=sym, severity='info', source=source, metadata=_json.dumps({
                            'rule_id': rule_id, 'kind': kind, 'watchlist_id': rule.get('watchlist_id'), 'symbol': rule.get('symbol'), 'trigger_mode': trigger_mode,
                        }))
                        con2 = _conn()
                        try:
                            con2.execute(
                                "UPDATE alert_rules SET last_triggered_at=datetime('now'), last_trigger_state=1, last_match_symbol=?, updated_at=datetime('now') WHERE id=?",
                                (sym, rule_id),
                            )
                            con2.commit()
                        finally:
                            con2.close()
                        triggered.append({'id': rule_id, 'symbol': sym, 'title': name, 'detail': detail})
                        if trigger_mode == 'once':
                            break
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
                    continue
                detail = f"{sym} matched {kind} alert: {name}"
                try:
                    log_alert_notification(kind.upper(), name, detail, symbol=sym, severity='info', source=source, metadata=_json.dumps({
                        'rule_id': rule_id, 'kind': kind, 'watchlist_id': rule.get('watchlist_id'), 'symbol': rule.get('symbol'), 'trigger_mode': trigger_mode,
                    }), scanner_name=name)
                except Exception:
                    pass
                con2 = _conn()
                try:
                    con2.execute(
                        "UPDATE alert_rules SET last_triggered_at=datetime('now'), last_trigger_state=1, last_match_symbol=?, updated_at=datetime('now') WHERE id=?",
                        (sym, rule_id),
                    )
                    con2.commit()
                finally:
                    con2.close()
                triggered.append({'id': rule_id, 'symbol': sym, 'title': name, 'detail': detail})
                if trigger_mode == 'once':
                    break
        except Exception:
            continue

    return {'ok': True, 'triggered': len(triggered), 'alerts': triggered}


_alert_rule_watcher_started = False
_alert_rule_watcher_lock = __import__('threading').Lock()


def start_alert_rule_watcher(interval_seconds=900):
    """Background loop that evaluates custom alerts on a fixed cadence."""
    global _alert_rule_watcher_started
    with _alert_rule_watcher_lock:
        if _alert_rule_watcher_started:
            return False
        _alert_rule_watcher_started = True

    def _loop():
        while True:
            try:
                run_alert_rules_once(source='watcher')
            except Exception:
                pass
            __import__('time').sleep(max(60, int(interval_seconds or 900)))

    t = __import__('threading').Thread(target=_loop, name='alert-rule-watcher', daemon=True)
    t.start()
    return True


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
    trigger_mode = (d.get('trigger_mode') or 'once').strip().lower()
    if trigger_mode not in {'once', 'every'}:
        return jsonify({'error': 'trigger_mode must be once or every'}), 400
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


@wl_bp.route('/alerts/rules/<int:rule_id>', methods=['PUT', 'DELETE'])
def alerts_rules_item(rule_id: int):
    _ensure_alert_rules_table()
    if request.method == 'DELETE':
        con = _conn()
        try:
            con.execute('DELETE FROM alert_rules WHERE id=?', (rule_id,))
            con.commit()
            return jsonify({'ok': True})
        finally:
            con.close()

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
            v = str(v).strip().lower()
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
