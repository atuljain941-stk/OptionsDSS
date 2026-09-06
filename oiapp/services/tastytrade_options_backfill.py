# oiapp/services/tastytrade_options_backfill.py
"""
Throttled, queue-based backfill of real tastytrade Greeks/OI into the
`options` table, for the full watchlist across expiries up to ~2 months
out -- not a hard replacement of the existing yfinance fetch, added
alongside it.

TIMING, REVISED: the first version of this file queued one item per
(symbol, expiry) pair and called TastytradeFeed.get_live_chain_snapshot()
once per pair -- each call paying its own ~15s DXLink collection window
separately. For ~104 symbols x ~8 expiries within 2 months, that's
roughly 3.5 hours run sequentially, which is a fair thing to push back
on (yfinance, by comparison, does the whole watchlist in a few minutes
via simple REST calls -- fundamentally faster because it's request/
response, not a streaming collection window).

That multiplication wasn't actually necessary. DXLink already batches
many strikes into ONE subscription burst for a single expiry (that's
how one expiry's ~50-100 strikes all arrive together in the existing
15s window). There's no architectural reason the same one-session,
one-wait approach can't span every expiry for a symbol at once --
TastytradeFeed.get_multi_expiry_chain_snapshot() does exactly that,
subscribing to all strikes across all expiries within months_ahead in
ONE DXLink session, waiting ONCE (20s) for everything to arrive. This
file now queues and processes at SYMBOL granularity, not (symbol,
expiry) pairs -- one call covers a symbol's entire 2-month chain.

Revised estimate: ~104 symbols x ~20s each = ~35 minutes for a full
watchlist backfill, run sequentially through the throttled queue. Still
slower than yfinance's simple REST calls (expected, given the streaming-
collection-window architecture is fundamentally different), but no
longer the multi-hour gap the per-expiry-loop design produced.

WHY ADDITIVE, NOT A REPLACEMENT (unchanged from the original reasoning):
many other pages already depend on the existing yfinance-fed options
table working. Tastytrade rows write into the SAME options table,
tagged data_source='tastytrade', with real delta/gamma/theta/vega
alongside the OI/price/bid/ask/iv columns yfinance already populated.
yfinance's own fetch path (services/market.py) is untouched by this file.

OI=0 GATE:
Rows with zero open interest are dropped before the INSERT, not written
and filtered later. Genuinely illiquid strikes with real (if tiny) OI
still get stored; strikes with confirmed zero OI don't.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import DB_PATH as _OIAPP_DB_PATH

DEFAULT_MAX_ITEMS_PER_TICK = 3  # ~3 symbols x ~20s each = ~60s per tick, matching the 90s scheduler interval below with room to spare
DEFAULT_MAX_DTE = 50
DEFAULT_WEEKLY_EXPIRY_LIMIT = 8
# Number of strike prices on each side of spot, per selected expiry. Each
# selected strike includes its call and put, so 20 each side means up to
# roughly 80 option contracts per expiry.
DEFAULT_STRIKES_EACH_SIDE = max(1, int(os.environ.get("OIAPP_TASTYTRADE_STRIKES_EACH_SIDE", "20")))


def _conn():
    con = sqlite3.connect(_OIAPP_DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _ensure_queue_table(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS tastytrade_options_backfill_queue (
        symbol TEXT PRIMARY KEY,
        queued_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        last_error TEXT,
        max_dte INTEGER NOT NULL DEFAULT 50,
        expiries_covered INTEGER,
        rows_written INTEGER
    )""")
    # Existing installations created the queue before max_dte existed.
    columns = {row[1] for row in con.execute("PRAGMA table_info(tastytrade_options_backfill_queue)")}
    if "max_dte" not in columns:
        con.execute("ALTER TABLE tastytrade_options_backfill_queue ADD COLUMN max_dte INTEGER NOT NULL DEFAULT 50")
    con.execute("UPDATE tastytrade_options_backfill_queue SET max_dte=? WHERE max_dte IS NULL OR max_dte < 1", (DEFAULT_MAX_DTE,))
    con.execute("CREATE INDEX IF NOT EXISTS idx_tt_backfill_status ON tastytrade_options_backfill_queue(status, queued_at)")


def enqueue_symbol(symbol: str, max_dte: int = DEFAULT_MAX_DTE) -> Dict[str, Any]:
    """Queues a symbol for backfill -- fast, no tastytrade call at all
    here (expiry discovery now happens inside the batched fetch itself,
    at processing time, not as a separate up-front step). Safe to call
    again for the same symbol -- INSERT OR IGNORE leaves an existing
    queue row (pending, done, or failed) alone."""
    con = _conn()
    try:
        _ensure_queue_table(con)
        now = datetime.now().isoformat(timespec="seconds")
        cur = con.execute(
            "INSERT OR IGNORE INTO tastytrade_options_backfill_queue (symbol, queued_at, status, max_dte) VALUES (?,?,'pending',?)",
            (symbol.upper(), now, max(1, int(max_dte))),
        )
        con.commit()
        return {"ok": True, "queued": cur.rowcount}
    finally:
        con.close()


def enqueue_watchlist(symbols: List[str], max_dte: int = DEFAULT_MAX_DTE) -> Dict[str, Any]:
    """Enqueues every symbol given. This itself is fast (just queue
    inserts, no tastytrade calls) -- the resulting queue takes roughly
    20s/symbol to actually drain via the throttled background job."""
    con = _conn()
    try:
        _ensure_queue_table(con)
        now = datetime.now().isoformat(timespec="seconds")
        queued = 0
        for sym in symbols:
            cur = con.execute(
                "INSERT OR IGNORE INTO tastytrade_options_backfill_queue (symbol, queued_at, status, max_dte) VALUES (?,?,'pending',?)",
                (sym.upper(), now, max(1, int(max_dte))),
            )
            queued += cur.rowcount
        con.commit()
        return {"ok": True, "symbols_given": len(symbols), "newly_queued": queued}
    finally:
        con.close()


def _write_chain_rows(symbol: str, expiry: str, rows: List[dict]) -> int:
    """Only positive-OI rows are written here; zero or missing OI is skipped.
    Same DELETE-then-INSERT pattern db.py's store_option_chain already uses
    for same-day re-fetches, so re-running a backfill for a symbol that
    already has tastytrade data today replaces it rather than
    duplicating rows. Called once per expiry found in a symbol's batched
    fetch result."""
    keep_rows = [r for r in rows if (r.get("oi") or 0) > 0]
    if not keep_rows:
        return 0
    today = datetime.now().strftime("%Y-%m-%d")
    fetch_ts = datetime.now().isoformat(timespec="seconds")
    con = _conn()
    try:
        from ..db import _ensure_options_enrichment_columns
        _ensure_options_enrichment_columns(con)
        con.execute(
            "DELETE FROM options WHERE symbol=? AND expiration=? AND date=? AND data_source='tastytrade'",
            (symbol, expiry, today),
        )
        con.executemany(
            """INSERT INTO options
               (symbol, expiration, type, strike, price, oi, volume, date,
                bid, ask, last, iv, underlying, fetch_ts,
                delta, gamma, theta, vega, data_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    symbol, expiry, r["type"], r["strike"],
                    r.get("last"), r.get("oi"), r.get("volume"), today,
                    None, None, r.get("last"), r.get("iv"), None, fetch_ts,
                    r.get("delta"), r.get("gamma"), r.get("theta"), r.get("vega"), "tastytrade",
                )
                for r in keep_rows
            ],
        )
        con.commit()
        return len(keep_rows)
    finally:
        con.close()


def fetch_and_store_symbol(symbol: str, max_dte: int = DEFAULT_MAX_DTE) -> Dict[str, Any]:
    """One batched call covering daily expiries through max_dte, or at
    most eight dates for a weekly-only chain, and only the configured
    number of strikes on each side of spot. See this module's docstring
    for why this replaced the original per-expiry-loop design.

    ok=True here means the DXLink session/subscription succeeded --
    it does NOT by itself mean any data was actually written. This
    distinction matters: a real production run reported "173/199
    fetched" via this function's ok flag while the database showed
    ZERO new rows, because every strike's open interest came back as
    0 (a separate bug in tastytrade_feed.py, since fixed with a
    Summary-event fallback and diagnostic logging -- see there for
    details). The caller MUST check rows_written, not just ok, to
    know whether this symbol actually produced anything.
    """
    from .tastytrade_feed import feed
    result = feed.get_multi_expiry_chain_snapshot(
        symbol,
        max_dte=max(1, int(max_dte)),
        weekly_expiry_limit=DEFAULT_WEEKLY_EXPIRY_LIMIT,
        strikes_each_side=DEFAULT_STRIKES_EACH_SIDE,
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "expiries_covered": 0, "rows_written": 0}
    by_expiry = result.get("by_expiry") or {}
    total_written = 0
    for expiry, rows in by_expiry.items():
        total_written += _write_chain_rows(symbol, expiry, rows)
    oi_sources = result.get("oi_sources_used") or {}
    resp = {"ok": True, "expiries_covered": len(by_expiry), "rows_written": total_written,
            "oi_sources_used": oi_sources}
    if total_written == 0 and by_expiry:
        # Connected fine, found strikes, but every single one had oi<=0
        # (or somehow otherwise failed the write gate) -- this is the
        # exact failure mode that silently reported as "success" before.
        # Flagging it explicitly rather than letting ok=True stand alone.
        total_strikes = sum(len(rows) for rows in by_expiry.values())
        resp["ok"] = False
        resp["error"] = (
            f"Connected and found {total_strikes} strikes across {len(by_expiry)} expiries, but wrote 0 rows -- "
            f"every strike had oi<=0. OI sources used: {oi_sources.get('summary',0)} from Summary event, "
            f"{oi_sources.get('static_attr',0)} from static instrument attribute, "
            f"{oi_sources.get('none',0)} had none. Check server logs for '[tastytrade_feed] DIAGNOSTIC' -- "
            f"it logs the real SDK object attributes for the first strike this happened on."
        )
    return resp


def run_pending_backfills(max_items: int = DEFAULT_MAX_ITEMS_PER_TICK) -> Dict[str, Any]:
    """Background job: processes a small batch of queued SYMBOLS per
    call (not symbol/expiry pairs -- see this module's docstring). Each
    item now costs one ~20s batched DXLink collection window covering
    that symbol's whole 2-month chain, not one window per expiry."""
    con = _conn()
    try:
        _ensure_queue_table(con)
        rows = con.execute(
            "SELECT symbol, max_dte FROM tastytrade_options_backfill_queue WHERE status='pending' ORDER BY queued_at ASC LIMIT ?",
            (max_items,),
        ).fetchall()
    finally:
        con.close()

    result = {"processed": 0, "succeeded": 0, "failed": 0, "rows_written": 0, "expiries_covered": 0}
    for row in rows:
        symbol, max_dte = row[0], row[1]
        result["processed"] += 1
        try:
            r = fetch_and_store_symbol(symbol, max_dte=max_dte)
            ok = bool(r.get("ok"))
            result["rows_written"] += r.get("rows_written", 0)
            result["expiries_covered"] += r.get("expiries_covered", 0)
            err = None if ok else r.get("error")
        except Exception as e:
            ok = False
            err = str(e)
            r = {"expiries_covered": 0, "rows_written": 0}
        con2 = _conn()
        try:
            _ensure_queue_table(con2)
            con2.execute(
                "UPDATE tastytrade_options_backfill_queue SET status=?, last_error=?, expiries_covered=?, rows_written=? WHERE symbol=?",
                ("done" if ok else "failed", err, r.get("expiries_covered", 0), r.get("rows_written", 0), symbol),
            )
            con2.commit()
        finally:
            con2.close()
        result["succeeded" if ok else "failed"] += 1
    return result


def queue_status() -> Dict[str, Any]:
    """Diagnostic: how much of the queue is left, plus a rough time-
    remaining estimate at ~20s/symbol -- so real progress can be
    checked directly instead of guessed at."""
    con = _conn()
    try:
        _ensure_queue_table(con)
        rows = con.execute(
            "SELECT status, COUNT(*) FROM tastytrade_options_backfill_queue GROUP BY status"
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        pending = counts.get("pending", 0)
        return {
            "ok": True, "counts": counts,
            # ~8s/symbol, not the old 20s: the collection window used to
            # always run to its full timeout because the Trade-event
            # collector had no exit condition (see tastytrade_feed.py's
            # get_multi_expiry_chain_snapshot). Now that it stops as soon
            # as Greeks are complete, a symbol takes roughly as long as
            # its data actually needs. Still an estimate, not a promise --
            # a symbol with many illiquid strikes that never push Greeks
            # will still ride the full timeout.
            "estimated_minutes_remaining": round(pending * 8 / 60, 1),
        }
    finally:
        con.close()


def register_scheduler_job(interval_seconds: int = 90):
    """Every 90s by default, processing DEFAULT_MAX_ITEMS_PER_TICK (3)
    symbols each tick -- roughly 3 symbols/90s => a full ~104-symbol
    watchlist drains in a little over an hour of wall-clock scheduler
    time (not 3.5 hours -- see this module's docstring for the revised
    per-symbol batching this estimate is based on). Registered with
    job_registry (visible in Scheduler Hub) and NOT low_priority: a
    background backfill can tolerate occasional delay from an active
    scan, but shouldn't be silently skipped entirely for however long
    that scan runs.
    """
    from . import unified_scheduler
    from .job_registry import register_job
    register_job(
        "tastytrade_options_backfill", "Tastytrade Options Backfill (Greeks + OI)",
        "Throttled full-watchlist backfill of real broker Greeks and OI across expiries up to ~2 months out",
        kind="interval", default_schedule={"interval_min": round(interval_seconds / 60, 2)},
        group="Live Capture", run_now_fn=run_pending_backfills, editable=True,
    )
    return unified_scheduler.register(
        "tastytrade_options_backfill", run_pending_backfills,
        interval_seconds=interval_seconds, low_priority=False,
    )


# ── Minimal routes: no dedicated page, just enough to trigger and check
# progress from a browser instead of requiring a Python console. ──────

from flask import Blueprint, jsonify, request

tastytrade_backfill_bp = Blueprint("tastytrade_options_backfill", __name__, url_prefix="/tastytrade-backfill")


@tastytrade_backfill_bp.route("/api/status")
def api_status():
    try:
        return jsonify(queue_status())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@tastytrade_backfill_bp.route("/api/enqueue_watchlist", methods=["POST"])
def api_enqueue_watchlist():
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols")
    max_dte = int(body.get("max_dte", DEFAULT_MAX_DTE))
    if not symbols:
        try:
            con = _conn()
            symbols = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM options").fetchall()]
            con.close()
        except Exception as e:
            return jsonify({"ok": False, "error": f"couldn't resolve default symbol list: {e}"}), 500
    try:
        return jsonify(enqueue_watchlist(symbols, max_dte=max_dte))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@tastytrade_backfill_bp.route("/api/enqueue_symbol", methods=["POST"])
def api_enqueue_symbol():
    body = request.get_json(silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"ok": False, "error": "symbol required"}), 400
    max_dte = int(body.get("max_dte", DEFAULT_MAX_DTE))
    try:
        return jsonify(enqueue_symbol(symbol, max_dte=max_dte))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
