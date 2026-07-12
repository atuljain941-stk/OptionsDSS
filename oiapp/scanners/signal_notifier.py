# oiapp/scanners/signal_notifier.py
"""
Signal Notifier — bridges scanners to Telegram push alerts.
──────────────────────────────────────────────────────────────────
scanner runs on a schedule  ->  filters for high-probability setups
  -> dedupes vs already-alerted-today  ->  pushes a Telegram card
  -> logs to signal_notifier_alerts

This module does NOT place any trades. It only watches and notifies.

Sources you can configure (each with its own enable/disable + frequency):
    trade_scanner   -> oiapp/scanners/trade_opportunity_scanner.py
                       (options PS/CS/IC setups, graded A-F)
    dashboard_tile  -> a tile on the Scanner Dashboard (any saved scanner
                       query + watchlist combination already built there)
    scanner_query   -> a saved Scanner Builder definition, OR a raw
                       ad-hoc query string + watchlist typed directly here

Two switches control whether anything fires:
    Global switch   -> signal_notifier_enabled setting (kills everything)
    Per-source switch -> signal_notifier_sources.enabled (kills just that one)

Dedupe rule (per your request): a symbol is only ever alerted ONCE per
calendar day, full stop — regardless of which source raised it or how many
times its score changes intraday. Once logged today, it's skipped.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, render_template, request

signal_notifier_bp = Blueprint("signal_notifier", __name__, url_prefix="/signal-notifier")

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")
MAX_WORKERS = 6

_WATCHER_STARTED = False
_WATCHER_LOCK = threading.Lock()
_WATCHER_TICK_SEC = 30  # how often the background loop wakes up to check sources

SOURCE_KINDS = ("trade_scanner", "dashboard_tile", "scanner_query")


# ── DB helpers ──────────────────────────────────────────────────────────

def _conn():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def _ensure_table():
    con = _conn()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS signal_notifier_alerts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_date    TEXT NOT NULL,
                symbol        TEXT NOT NULL,
                bucket        TEXT NOT NULL,   -- TRENDING / MEAN_REVERSION / SCANNER
                trade_type    TEXT NOT NULL,   -- PS / CS / IC / SCAN
                grade         TEXT NOT NULL,
                score         INTEGER NOT NULL,
                pop           INTEGER,
                legs          TEXT,
                rationale     TEXT,
                manage        TEXT,
                sent_at       TEXT NOT NULL,
                telegram_ok   INTEGER DEFAULT 0
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_signal_alerts_lookup
            ON signal_notifier_alerts (alert_date, symbol, trade_type)
        """)
        # migrate: add source columns if this is an older DB
        cols = _table_columns(con, "signal_notifier_alerts")
        if "source_id" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN source_id INTEGER")
        if "source_kind" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN source_kind TEXT")
        if "source_label" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN source_label TEXT")
        if "message" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN message TEXT")
        if "symbol_price" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN symbol_price REAL")
        if "expiry" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN expiry TEXT")
        if "dte" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN dte INTEGER")
        if "ai_verdict" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN ai_verdict TEXT")
        if "ai_reasoning" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN ai_reasoning TEXT")
        if "ai_risk_flags" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN ai_risk_flags TEXT")
        if "ai_sizing_note" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN ai_sizing_note TEXT")
        # Structured strikes/credit — the scanner already computes these as
        # real numbers (sell_strike/buy_strike for PS/CS, put_sell/put_buy/
        # call_sell/call_buy for IC); capturing them directly means
        # backtesting the outcome later doesn't have to parse the "legs"
        # display text back apart.
        if "short_put_strike" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN short_put_strike REAL")
        if "long_put_strike" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN long_put_strike REAL")
        if "short_call_strike" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN short_call_strike REAL")
        if "long_call_strike" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN long_call_strike REAL")
        if "est_credit" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN est_credit REAL")
        if "max_loss_amt" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN max_loss_amt REAL")
        # Backtest outcome — computed once expiry has passed, cached here so
        # it isn't recomputed on every page load.
        if "bt_outcome" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN bt_outcome TEXT")
        if "bt_pnl" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN bt_pnl REAL")
        if "bt_pnl_pct" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN bt_pnl_pct REAL")
        if "bt_expiry_price" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN bt_expiry_price REAL")
        if "bt_computed_at" not in cols:
            con.execute("ALTER TABLE signal_notifier_alerts ADD COLUMN bt_computed_at TEXT")

        # This index needs expiry/legs, which only exist after the migration
        # above runs (they aren't in the original base CREATE TABLE), so it
        # has to be created here, not alongside the other index above.
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_signal_alerts_setup_lookup
            ON signal_notifier_alerts (symbol, expiry, legs)
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS signal_notifier_sources (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                kind          TEXT NOT NULL,           -- trade_scanner / dashboard_tile / scanner_query
                label         TEXT NOT NULL,
                enabled       INTEGER NOT NULL DEFAULT 1,
                interval_sec  INTEGER NOT NULL DEFAULT 900,
                watchlist_id  TEXT DEFAULT '',
                min_score     INTEGER DEFAULT 70,
                min_conviction_score INTEGER DEFAULT 0,
                direction_tags TEXT DEFAULT '[]',
                benchmark     TEXT DEFAULT 'SPY',
                dashboard_id  INTEGER,
                tile_id       TEXT,
                definition_id INTEGER,
                query_text    TEXT,
                last_run_at   TEXT,
                last_run_count INTEGER DEFAULT 0,
                last_error    TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        src_cols = _table_columns(con, "signal_notifier_sources")
        if "min_conviction_score" not in src_cols:
            con.execute("ALTER TABLE signal_notifier_sources ADD COLUMN min_conviction_score INTEGER DEFAULT 0")
        if "direction_tags" not in src_cols:
            con.execute("ALTER TABLE signal_notifier_sources ADD COLUMN direction_tags TEXT DEFAULT '[]'")
        con.commit()
    finally:
        con.close()


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


# ── global settings (reuse the same settings table other features use) ──

def _get_setting(key: str, default: str = "") -> str:
    from .watchlist_manager import _get_setting as _gs
    return _gs(key, default)


def _set_setting(key: str, value: str) -> None:
    from .watchlist_manager import _set_setting as _ss
    _ss(key, value)


def get_config() -> Dict[str, Any]:
    return {
        "enabled": _get_setting("signal_notifier_enabled", "1") == "1",
        "interval_sec": int(_get_setting("signal_notifier_interval_sec", "900") or 900),
        "min_score": int(_get_setting("signal_notifier_min_score", "70") or 70),
        "watchlist_id": _get_setting("signal_notifier_watchlist_id", "") or None,
        "min_regap_pts": int(_get_setting("signal_notifier_min_regap_pts", "8") or 8),
        "ai_gate_enabled": _get_setting("signal_notifier_ai_gate_enabled", "0") == "1",
    }


def set_config(**kwargs) -> Dict[str, Any]:
    mapping = {
        "enabled": "signal_notifier_enabled",
        "interval_sec": "signal_notifier_interval_sec",
        "min_score": "signal_notifier_min_score",
        "watchlist_id": "signal_notifier_watchlist_id",
        "min_regap_pts": "signal_notifier_min_regap_pts",
        "ai_gate_enabled": "signal_notifier_ai_gate_enabled",
    }
    for k, v in kwargs.items():
        if k in mapping and v is not None:
            is_bool_setting = k in ("enabled", "ai_gate_enabled")
            _set_setting(mapping[k], "1" if (is_bool_setting and v in (True, "1", 1)) else ("0" if is_bool_setting else str(v)))
    return get_config()


# ── dedupe: one alert per symbol per calendar day, full stop ────────────

def _grade_for(score: int) -> str:
    if score >= 80: return "A"
    if score >= 65: return "B"
    if score >= 50: return "C"
    if score >= 35: return "D"
    return "F"


def _already_alerted_today(symbol: str) -> bool:
    con = _conn()
    try:
        row = con.execute(
            "SELECT 1 FROM signal_notifier_alerts WHERE alert_date=? AND symbol=? LIMIT 1",
            (_today(), symbol),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def _same_setup_already_sent(symbol: str, expiry: str, legs: str) -> bool:
    """True if this EXACT expiry+strikes combo was already alerted for this
    symbol on ANY previous day — not just today. If tomorrow's scan turns
    up the identical recommendation, that isn't new information, so it
    shouldn't fire a second alert just because a day has passed."""
    if not expiry or not legs:
        return False
    con = _conn()
    try:
        row = con.execute(
            "SELECT 1 FROM signal_notifier_alerts WHERE symbol=? AND expiry=? AND legs=? LIMIT 1",
            (symbol, expiry, legs),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def _is_duplicate_recommendation(symbol: str, expiry: str = "", legs: str = "") -> bool:
    """Combines both dedupe rules: one alert per symbol per day (regardless
    of setup), PLUS never repeat the identical expiry+strikes combo again
    on a later day either."""
    if _already_alerted_today(symbol):
        return True
    return _same_setup_already_sent(symbol, expiry, legs)


def _should_alert(symbol: str, trade_type: str = "", score: int = 0, min_regap_pts: int = 8) -> bool:
    """Kept for backward compatibility with the Phase-1 integration; now just
    enforces the one-alert-per-symbol-per-day rule regardless of trade_type
    or score movement."""
    return not _already_alerted_today(symbol)


def _log_alert(symbol: str, bucket: str, opp: Dict, telegram_ok: bool,
                source_id: Optional[int] = None, source_kind: str = "", source_label: str = "",
                message: str = "", ai_gate: Optional[Dict[str, Any]] = None) -> None:
    ai_gate = ai_gate or {}
    tt = opp.get("trade_type", "")
    if tt == "PS":
        short_put, long_put = opp.get("sell_strike"), opp.get("buy_strike")
        short_call, long_call = None, None
    elif tt == "CS":
        short_put, long_put = None, None
        short_call, long_call = opp.get("sell_strike"), opp.get("buy_strike")
    elif tt == "IC":
        short_put, long_put = opp.get("put_sell"), opp.get("put_buy")
        short_call, long_call = opp.get("call_sell"), opp.get("call_buy")
    else:
        short_put = long_put = short_call = long_call = None

    con = _conn()
    try:
        con.execute(
            """INSERT INTO signal_notifier_alerts
               (alert_date, symbol, bucket, trade_type, grade, score, pop,
                legs, rationale, manage, sent_at, telegram_ok,
                source_id, source_kind, source_label, message, symbol_price,
                expiry, dte, ai_verdict, ai_reasoning, ai_risk_flags, ai_sizing_note,
                short_put_strike, long_put_strike, short_call_strike, long_call_strike,
                est_credit, max_loss_amt)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _today(), symbol, bucket, opp.get("trade_type", "") or "SCAN",
                opp.get("grade", "") or "", int(opp.get("score", 0) or 0),
                opp.get("pop"), opp.get("legs", "") or "",
                (opp.get("rationale") or "")[:500],
                opp.get("manage", "") or "", datetime.now().isoformat(),
                1 if telegram_ok else 0,
                source_id, source_kind, source_label, message or "",
                opp.get("price") or opp.get("close") or opp.get("spot"),
                opp.get("expiry") or "",
                opp.get("dte") if opp.get("dte") is not None else None,
                ai_gate.get("verdict"), ai_gate.get("reasoning"),
                json.dumps(ai_gate.get("risk_flags") or []), ai_gate.get("sizing_note"),
                short_put, long_put, short_call, long_call,
                opp.get("est_credit"), opp.get("max_loss"),
            ),
        )
        con.commit()
    finally:
        con.close()


def _apply_ai_gate(symbol: str, trade_type: str, bucket: str, sector: Optional[str] = None,
                    risk_amt: Optional[float] = None) -> Dict[str, Any]:
    """Optional gate: if enabled in settings and an LLM key is configured, run
    the candidate through the AI Copilot's pre-trade risk check before it's
    sent to Telegram. Returns {"allow": bool, "note": str|None, "verdict":
    str|None, "reasoning": str|None, "risk_flags": list, "sizing_note":
    str|None} — the verdict/reasoning/etc are stored on the alert itself
    (not just folded into the message text) so they show up as their own
    fields in history/export and can be reviewed later. Fails open
    (allow=True) on any error so a misconfigured/unavailable LLM never blocks
    the underlying deterministic scanner from working."""
    empty = {"allow": True, "note": None, "verdict": None, "reasoning": None, "risk_flags": [], "sizing_note": None}
    if not get_config().get("ai_gate_enabled"):
        return dict(empty)
    try:
        from ..ai.llm_client import llm_configured
        if not llm_configured():
            return dict(empty)
        from ..ai.copilot import run_pretrade_check
        direction = {
            "TRENDING": "bullish", "BULLISH": "bullish",
            "BEARISH": "bearish",
            "MEAN_REVERSION": "neutral", "SIDEWAYS": "neutral",
        }.get(bucket, "")
        result = run_pretrade_check(symbol=symbol, trade_type=trade_type, direction=direction,
                                     sector=sector, risk_amt=risk_amt)
        if not result.get("ok"):
            return dict(empty)
        verdict = result.get("verdict")
        base = {
            "verdict": verdict,
            "reasoning": result.get("reasoning", ""),
            "risk_flags": result.get("risk_flags", []),
            "sizing_note": result.get("sizing_note", ""),
        }
        if verdict == "REJECT":
            return {"allow": False, "note": f"AI risk check REJECTED this alert: {result.get('reasoning', '')}", **base}
        if verdict == "CAUTION":
            return {"allow": True, "note": f"⚠️ AI caution: {result.get('reasoning', '')}", **base}
        return {"allow": True, "note": None, **base}
    except Exception:
        return dict(empty)


# ── conviction scoring for non-options alert sources ──────────────────────
# scanner_query / dashboard_tile matches are plain symbol hits (no options
# chain, no PS/CS/IC legs), so they never got a grade/score/POP the way the
# Trade Opportunity Scanner alerts do. conviction_scorer.py already builds a
# 0-12 composite score from cached scanner outputs (OI buildup, regime, S/R,
# institutional, RSI MTF) for any symbol, with no options data required, so
# we reuse it here rather than inventing a second scoring system.

def _score_via_conviction(symbol: str) -> Dict[str, Any]:
    try:
        from .conviction_scorer import score_symbol
        result = score_symbol(symbol)
        total = float(result.get("total_score") or 0)
        max_score = float(result.get("max_score") or 12) or 12
        score_100 = int(round(min(100, max(0, total / max_score * 100))))
        return {
            "score": score_100,
            "grade": _grade_for(score_100),
            "signals": result.get("signals") or [],
            "label": result.get("label") or "",
        }
    except Exception:
        return {"score": 0, "grade": "", "signals": [], "label": ""}


# ── message formatting ───────────────────────────────────────────────────

def _format_message(symbol: str, bucket: str, opp: Dict, source_label: str = "") -> str:
    emoji = {"TRENDING": "📈", "MEAN_REVERSION": "🔁"}.get(bucket, "🔎")
    bias = opp.get("bias", "")
    pop = opp.get("pop")
    pop_txt = f"{pop}%" if pop is not None else "n/a"
    credit = opp.get("est_credit")
    max_loss = opp.get("max_loss")
    rr = opp.get("rr")
    lines = [f"{emoji} {bucket.replace('_', ' ').title()} setup: {symbol}"]
    if opp.get("trade_type") and opp.get("trade_type") != "SCAN":
        lines.append(f"Grade {opp.get('grade', '?')} ({opp.get('score', '?')}/100) — {bias}")
        lines.append(f"Type: {opp.get('trade_type', '?')} | Expiry: {opp.get('expiry', '?')} ({opp.get('dte', '?')} DTE)")
        lines.append(f"Legs: {opp.get('legs', 'n/a')}")
        lines.append(f"POP: {pop_txt} | Credit: ${credit} | Max loss: ${max_loss} | RR: {rr}")
    else:
        # scanner-query / dashboard-tile style alert (no options legs). No
        # options chain here, so no POP/credit/RR — but the symbol still gets
        # scored via conviction_scorer (same 0-100/grade scale as the options
        # scanner) so these alerts aren't left without any score at all.
        if opp.get("grade"):
            lines.append(f"Conviction grade {opp.get('grade')} ({opp.get('score', 0)}/100)")
        price = opp.get("price") or opp.get("close") or opp.get("spot")
        if price is not None:
            lines.append(f"Price: {price}")
    pros = opp.get("pros") or []
    cons = opp.get("cons") or []
    if pros:
        lines.append("Why: " + "; ".join(pros))
    if cons:
        # Cons were computed by _entry_score all along but never shown in
        # the message before -- this is exactly what lets a reviewer see
        # WHY a trade only got the grade it got, not just the grade
        # itself. A B-grade trade with 4 pros and 2 real cons tells a very
        # different story than a B-grade trade nobody can see the caution
        # flags on.
        lines.append("Caution: " + "; ".join(cons))
    manage = opp.get("manage")
    if manage:
        lines.append(f"Exit plan: {manage}")
    if source_label:
        lines.append(f"Source: {source_label}")
    lines.append(f"⏱ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


# ── source 1: Trade Opportunity Scanner (options PS/CS/IC) ──────────────

def _run_trade_scanner(*, watchlist_id: Optional[str], min_score: int, dry_run: bool,
                        source_id: Optional[int] = None, source_label: str = "Trade Opportunity Scanner") -> Dict[str, Any]:
    from .trade_opportunity_scanner import _watchlist_symbols, _scan_one, _batch_fetch_histories, DTE_MIN, DTE_MAX, MIN_EARN_DAYS
    from ..services.telegram_alerts import send_telegram_message, telegram_configured

    symbols = _watchlist_symbols(watchlist_id or None)
    if not symbols:
        return {"ok": False, "error": "No symbols found for watchlist", "watchlist_id": watchlist_id}

    histories = _batch_fetch_histories(symbols, period="1y")

    results: List[Dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {
            ex.submit(_scan_one, sym, DTE_MIN, DTE_MAX, MIN_EARN_DAYS, min_score,
                      prefetched_df=histories.get(sym.upper())): sym
            for sym in symbols
        }
        for fut in as_completed(futs):
            try:
                r = fut.result()
            except Exception as e:
                r = {"symbol": futs[fut], "filtered": True, "filter_reason": f"Error: {e}"}
            if r and not r.get("filtered"):
                results.append(r)

    sent, skipped = [], []
    can_send = telegram_configured() and not dry_run

    for opp in results:
        symbol = opp["symbol"]
        trade_type = opp.get("trade_type", "")
        trend = (opp.get("trend") or "").upper()
        bucket = "MEAN_REVERSION" if (trade_type == "IC" or "SIDEWAYS" in trend) else "TRENDING"

        if _is_duplicate_recommendation(symbol, opp.get("expiry", ""), opp.get("legs", "")):
            skipped.append(symbol)
            continue

        msg = _format_message(symbol, bucket, opp, source_label=source_label)
        gate = _apply_ai_gate(symbol, trade_type, bucket, risk_amt=opp.get("max_loss"))
        ok = False
        if not dry_run and not gate["allow"]:
            _log_alert(symbol, bucket, opp, False, source_id=source_id, source_kind="trade_scanner",
                        source_label=source_label, message=f"[BLOCKED BY AI GATE] {gate['note']}\n\n{msg}", ai_gate=gate)
            sent.append({"symbol": symbol, "bucket": bucket, "grade": opp.get("grade"),
                         "score": opp.get("score"), "telegram_ok": False, "message": msg, "ai_blocked": True})
            continue
        if gate["note"]:
            msg = f"{gate['note']}\n\n{msg}"
        if can_send:
            resp = send_telegram_message(msg)
            ok = bool(resp.get("ok"))
        if not dry_run:
            _log_alert(symbol, bucket, opp, ok, source_id=source_id, source_kind="trade_scanner", source_label=source_label, message=msg, ai_gate=gate)
        sent.append({"symbol": symbol, "bucket": bucket, "grade": opp.get("grade"),
                     "score": opp.get("score"), "telegram_ok": ok, "message": msg})

    return {
        "ok": True,
        "scanned": len(symbols),
        "candidates": len(results),
        "sent": len(sent),
        "skipped_duplicate": len(skipped),
        "telegram_configured": telegram_configured(),
        "alerts": sent,
    }


# kept for backward compatibility (Phase-1 integration doc / existing API callers)
def run_signal_scan(watchlist_id: Optional[int] = None,
                     min_score: Optional[int] = None,
                     min_regap_pts: Optional[int] = None,
                     dry_run: bool = False) -> Dict[str, Any]:
    _ensure_table()
    cfg = get_config()
    min_score = min_score if min_score is not None else cfg["min_score"]
    wl_id = watchlist_id if watchlist_id is not None else cfg["watchlist_id"]
    return _run_trade_scanner(watchlist_id=wl_id, min_score=min_score, dry_run=dry_run,
                               source_id=None, source_label="Trade Opportunity Scanner (default)")


# ── source 2: Scanner Dashboard tile ─────────────────────────────────────

def _resolve_dashboard_tile(dashboard_id: int, tile_id: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (query_text, watchlist_id, title) pulled fresh from the saved
    dashboard, so edits to the tile are picked up automatically."""
    from .scanner_dashboard import _get_dashboard
    dash = _get_dashboard(dashboard_id)
    if not dash:
        return None, None, None
    for t in dash.get("tiles") or []:
        if str(t.get("id")) == str(tile_id):
            return t.get("query_text") or "", str(t.get("watchlist_id") or ""), t.get("title") or ""
    return None, None, None


def _run_scanner_query(*, query_text: str, watchlist_id: Optional[str], benchmark: str,
                        dry_run: bool, source_id: Optional[int], source_kind: str, source_label: str,
                        min_conviction_score: int = 0, direction_tags: Optional[List[str]] = None) -> Dict[str, Any]:
    from .scanner_builder import (
        _parse_query, _expand_scan_nodes, _watchlist_symbols as _sb_watchlist_symbols,
        _scan_symbol, _eval, _explain, _required_timeframes,
    )
    from ..services.telegram_alerts import send_telegram_message, telegram_configured

    query_text = (query_text or "").strip()
    if not query_text:
        return {"ok": False, "error": "query_text is required"}

    try:
        raw_root = _parse_query(query_text)
        root = _expand_scan_nodes(raw_root, ())
    except Exception as e:
        return {"ok": False, "error": f"Query parse error: {e}"}

    wl_id_int = None
    if watchlist_id not in (None, ""):
        try:
            wl_id_int = int(watchlist_id)
        except Exception:
            wl_id_int = None
    symbols = _sb_watchlist_symbols(wl_id_int)
    if not symbols:
        return {"ok": False, "error": "No symbols found for the selected watchlist"}

    req_tfs = _required_timeframes(root)
    passed: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_scan_symbol, sym, root, benchmark, req_tfs): sym for sym in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                ctx, err = fut.result()
            except Exception as e:
                ctx, err = None, str(e)
            if not ctx:
                continue
            try:
                ok = bool(_eval(root, ctx, shift=0, tf_default="1d"))
            except Exception:
                ok = False
            if ok:
                try:
                    reasons = _explain(root, ctx, shift=0, tf_default="1d")
                except Exception:
                    reasons = []
                passed.append({
                    "symbol": sym,
                    "price": ctx.get("close") or ctx.get("spot") or ctx.get("price"),
                    "pros": reasons,
                })

    sent, skipped = [], []
    can_send = telegram_configured() and not dry_run
    allowed_types = _allowed_trade_types_for_tags(direction_tags or [])
    for row in passed:
        symbol = row["symbol"]
        if _already_alerted_today(symbol):
            skipped.append(symbol)
            continue

        bucket = "SCANNER"
        opp: Dict[str, Any]

        if allowed_types:
            # Direction-aware path: the query matched, but the alert only
            # goes out if a REAL, direction-aligned options setup exists
            # right now — otherwise this would just be conviction-scoring
            # a symbol against a bias it was never trying to have.
            try:
                from .trade_opportunity_scanner import _scan_one as _tos_scan_one, DTE_MIN, DTE_MAX, MIN_EARN_DAYS
                trade = _tos_scan_one(symbol, DTE_MIN, DTE_MAX, MIN_EARN_DAYS, min_score=0, allowed_types=allowed_types)
            except Exception as e:
                trade = {"symbol": symbol, "filtered": True, "filter_reason": str(e)}

            if not trade or trade.get("filtered"):
                skipped.append(symbol)
                continue
            if min_conviction_score and int(trade.get("score", 0) or 0) < min_conviction_score:
                skipped.append(symbol)
                continue

            tt = trade.get("trade_type", "")
            bucket = {"PS": "BULLISH", "CS": "BEARISH", "IC": "SIDEWAYS"}.get(tt, "SCANNER")
            scan_reasons = row.get("pros") or []
            opp = dict(trade)
            opp["pros"] = scan_reasons
            # Keep the scanner's own match reasons visible alongside the
            # trade engine's own rationale (already in opp["rationale"]).
            if scan_reasons:
                opp["rationale"] = (opp.get("rationale") or "") + " Scanner match: " + "; ".join(scan_reasons[:3])
        else:
            # No direction preference set on this source — original
            # generic-conviction behavior, unchanged for existing sources.
            conviction = _score_via_conviction(symbol)
            if min_conviction_score and conviction["score"] < min_conviction_score:
                skipped.append(symbol)
                continue
            combined_pros = list(row.get("pros") or []) + list(conviction.get("signals") or [])
            opp = {"trade_type": "SCAN", "grade": conviction["grade"], "score": conviction["score"],
                   "pros": combined_pros, "price": row.get("price")}

        msg = _format_message(symbol, bucket, opp, source_label=source_label)
        # Full dedup now that expiry/legs (if any) are known: same-day check
        # already happened above; this additionally blocks the identical
        # expiry+strikes combo from firing again on a LATER day too.
        if _same_setup_already_sent(symbol, opp.get("expiry", ""), opp.get("legs", "")):
            skipped.append(symbol)
            continue
        gate = _apply_ai_gate(symbol, opp.get("trade_type", "SCAN"), bucket)
        ok = False
        if not dry_run and not gate["allow"]:
            _log_alert(symbol, bucket, opp, False, source_id=source_id, source_kind=source_kind,
                        source_label=source_label, message=f"[BLOCKED BY AI GATE] {gate['note']}\n\n{msg}", ai_gate=gate)
            sent.append({"symbol": symbol, "bucket": bucket, "telegram_ok": False, "message": msg, "ai_blocked": True})
            continue
        if gate["note"]:
            msg = f"{gate['note']}\n\n{msg}"
        if can_send:
            resp = send_telegram_message(msg)
            ok = bool(resp.get("ok"))
        if not dry_run:
            _log_alert(symbol, bucket, opp, ok, source_id=source_id, source_kind=source_kind, source_label=source_label, message=msg, ai_gate=gate)
        sent.append({"symbol": symbol, "bucket": bucket, "telegram_ok": ok, "message": msg})

    return {
        "ok": True,
        "scanned": len(symbols),
        "candidates": len(passed),
        "sent": len(sent),
        "skipped_duplicate": len(skipped),
        "telegram_configured": telegram_configured(),
        "alerts": sent,
    }


# ── source registry (CRUD) ───────────────────────────────────────────────

def _source_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["direction_tags"] = json.loads(d.get("direction_tags") or "[]")
    except Exception:
        d["direction_tags"] = []
    return d


def list_sources() -> List[Dict[str, Any]]:
    _ensure_table()
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM signal_notifier_sources ORDER BY id DESC").fetchall()
        return [_source_row_to_dict(r) for r in rows]
    finally:
        con.close()


def get_source(source_id: int) -> Optional[Dict[str, Any]]:
    _ensure_table()
    con = _conn()
    try:
        row = con.execute("SELECT * FROM signal_notifier_sources WHERE id=?", (source_id,)).fetchone()
        return _source_row_to_dict(row) if row else None
    finally:
        con.close()


# Direction tags a scanner_query/dashboard_tile source can be marked with,
# so alerts get scored/suggested against the RIGHT bias instead of being
# scored generically and penalized for not being bullish. MRT = mean-
# reversion trade (maps to a neutral/Iron-Condor-style setup, same as
# Sideways) — kept as its own label since that's the term already used
# elsewhere in the app for this setup style.
VALID_DIRECTION_TAGS = ("MRT", "BULLISH", "BEARISH", "SIDEWAYS")
_DIRECTION_TAG_TO_TRADE_TYPE = {"BULLISH": "PS", "BEARISH": "CS", "SIDEWAYS": "IC", "MRT": "IC"}


def _normalize_direction_tags(value: Any) -> List[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = [value] if value else []
    if not isinstance(value, list):
        value = []
    return sorted({str(t).upper() for t in value if str(t).upper() in VALID_DIRECTION_TAGS})


def _allowed_trade_types_for_tags(tags: List[str]) -> Optional[List[str]]:
    """None means 'no preference — any direction is fine' (backward
    compatible with sources created before this feature existed)."""
    if not tags:
        return None
    types = sorted({_DIRECTION_TAG_TO_TRADE_TYPE[t] for t in tags if t in _DIRECTION_TAG_TO_TRADE_TYPE})
    return types or None


def create_source(payload: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_table()
    kind = str(payload.get("kind") or "").strip()
    if kind not in SOURCE_KINDS:
        raise ValueError(f"kind must be one of {SOURCE_KINDS}")
    label = str(payload.get("label") or "").strip() or kind.replace("_", " ").title()
    enabled = 1 if payload.get("enabled", True) in (True, 1, "1") else 0
    interval_sec = max(60, int(payload.get("interval_sec") or 900))
    watchlist_id = str(payload.get("watchlist_id") or "")
    min_score = int(payload.get("min_score") or 70)
    min_conviction_score = max(0, int(payload.get("min_conviction_score") or 0))
    direction_tags = json.dumps(_normalize_direction_tags(payload.get("direction_tags")))
    benchmark = str(payload.get("benchmark") or "SPY").upper()
    dashboard_id = payload.get("dashboard_id")
    tile_id = payload.get("tile_id")
    definition_id = payload.get("definition_id")
    query_text = payload.get("query_text")

    con = _conn()
    try:
        cur = con.execute(
            """INSERT INTO signal_notifier_sources
               (kind, label, enabled, interval_sec, watchlist_id, min_score, min_conviction_score,
                direction_tags, benchmark, dashboard_id, tile_id, definition_id, query_text, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (kind, label, enabled, interval_sec, watchlist_id, min_score, min_conviction_score,
             direction_tags, benchmark, dashboard_id, tile_id, definition_id, query_text),
        )
        con.commit()
        sid = cur.lastrowid
        row = con.execute("SELECT * FROM signal_notifier_sources WHERE id=?", (sid,)).fetchone()
        return _source_row_to_dict(row)
    finally:
        con.close()


def update_source(source_id: int, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    _ensure_table()
    existing = get_source(source_id)
    if not existing:
        return None
    fields = {
        "label": str,
        "enabled": lambda v: 1 if v in (True, 1, "1") else 0,
        "interval_sec": lambda v: max(60, int(v)),
        "watchlist_id": str,
        "min_score": int,
        "min_conviction_score": lambda v: max(0, int(v)),
        "direction_tags": lambda v: json.dumps(_normalize_direction_tags(v)),
        "benchmark": lambda v: str(v).upper(),
        "dashboard_id": lambda v: v,
        "tile_id": lambda v: v,
        "definition_id": lambda v: v,
        "query_text": lambda v: v,
    }
    sets, vals = [], []
    for key, caster in fields.items():
        if key in payload and payload[key] is not None:
            try:
                sets.append(f"{key}=?")
                vals.append(caster(payload[key]))
            except Exception:
                pass
    if not sets:
        return existing
    sets.append("updated_at=datetime('now')")
    vals.append(source_id)
    con = _conn()
    try:
        con.execute(f"UPDATE signal_notifier_sources SET {', '.join(sets)} WHERE id=?", vals)
        con.commit()
        row = con.execute("SELECT * FROM signal_notifier_sources WHERE id=?", (source_id,)).fetchone()
        return _source_row_to_dict(row)
    finally:
        con.close()


def delete_source(source_id: int) -> bool:
    _ensure_table()
    con = _conn()
    try:
        con.execute("DELETE FROM signal_notifier_sources WHERE id=?", (source_id,))
        con.commit()
        return True
    finally:
        con.close()


def _mark_source_run(source_id: int, count: int, error: Optional[str]) -> None:
    con = _conn()
    try:
        con.execute(
            "UPDATE signal_notifier_sources SET last_run_at=datetime('now'), last_run_count=?, last_error=? WHERE id=?",
            (count, error, source_id),
        )
        con.commit()
    finally:
        con.close()


def run_source(source: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    kind = source.get("kind")
    label = source.get("label") or kind
    sid = source.get("id")
    try:
        if kind == "trade_scanner":
            result = _run_trade_scanner(
                watchlist_id=source.get("watchlist_id") or None,
                min_score=int(source.get("min_score") or 70),
                dry_run=dry_run,
                source_id=sid,
                source_label=label,
            )
        elif kind == "dashboard_tile":
            query_text, watchlist_id, title = _resolve_dashboard_tile(
                int(source.get("dashboard_id")), str(source.get("tile_id"))
            )
            if query_text is None:
                result = {"ok": False, "error": "Dashboard tile not found (it may have been deleted or renamed)"}
            else:
                result = _run_scanner_query(
                    query_text=query_text, watchlist_id=watchlist_id, benchmark=source.get("benchmark") or "SPY",
                    dry_run=dry_run, source_id=sid, source_kind=kind,
                    source_label=f"{label} ({title})" if title else label,
                    min_conviction_score=int(source.get("min_conviction_score") or 0),
                    direction_tags=_normalize_direction_tags(source.get("direction_tags")),
                )
        elif kind == "scanner_query":
            query_text = source.get("query_text") or ""
            watchlist_id = source.get("watchlist_id") or ""
            benchmark = source.get("benchmark") or "SPY"
            def_id = source.get("definition_id")
            if def_id:
                from .scanner_builder import _conn as _sb_conn
                sc = _sb_conn()
                try:
                    row = sc.execute(
                        "SELECT query_text, watchlist_id, benchmark FROM scanner_definitions WHERE id=?",
                        (int(def_id),),
                    ).fetchone()
                    if row:
                        query_text = row["query_text"] or query_text
                        watchlist_id = str(row["watchlist_id"] or watchlist_id or "")
                        benchmark = row["benchmark"] or benchmark
                finally:
                    sc.close()
            result = _run_scanner_query(
                query_text=query_text, watchlist_id=watchlist_id, benchmark=benchmark,
                dry_run=dry_run, source_id=sid, source_kind=kind, source_label=label,
                min_conviction_score=int(source.get("min_conviction_score") or 0),
                direction_tags=_normalize_direction_tags(source.get("direction_tags")),
            )
        else:
            result = {"ok": False, "error": f"Unknown source kind: {kind}"}
    except Exception as e:
        result = {"ok": False, "error": str(e)}

    if sid is not None and not dry_run:
        _mark_source_run(sid, int(result.get("sent") or 0), None if result.get("ok") else result.get("error"))
    return result


# ── background scheduler thread ──────────────────────────────────────────

def _due(last_run_at: Optional[str], interval_sec: int) -> bool:
    if not last_run_at:
        return True
    try:
        last = datetime.fromisoformat(last_run_at)
    except Exception:
        return True
    return (datetime.now() - last).total_seconds() >= interval_sec


def _watcher_loop(app):
    from ..services.job_registry import register_job
    register_job(
        "signal_notifier", "Signal Notifier", "Runs the Trade Opportunity Scanner sweep plus any configured alert sources.",
        kind="interval", default_schedule={"interval_min": max(1, int(get_config()["interval_sec"] / 60))},
        group="Alert Watchers", run_now_fn=lambda: run_signal_scan(dry_run=False), editable=False,
    )
    # tracks last-run for the legacy default trade-scanner pass separately
    legacy_last_run: Optional[datetime] = None
    while True:
        try:
            cfg = get_config()
            if cfg["enabled"]:
                now = datetime.now()
                # legacy/default trade-scanner sweep (backward compatible)
                interval = max(60, cfg["interval_sec"])
                if legacy_last_run is None or (now - legacy_last_run).total_seconds() >= interval:
                    with app.app_context():
                        try:
                            result = run_signal_scan()
                            print(f"[signal_notifier] default trade-scanner pass: "
                                  f"{result.get('sent', 0)} sent / {result.get('candidates', 0)} candidates")
                            from ..services.job_registry import mark_run as _mark_run
                            _mark_run("signal_notifier", True, f"sent={result.get('sent', 0)} candidates={result.get('candidates', 0)}")
                        except Exception as e:
                            print(f"[signal_notifier] default pass error: {e}")
                            from ..services.job_registry import mark_run as _mark_run
                            _mark_run("signal_notifier", False, str(e))
                    legacy_last_run = now

                # per-source sweep
                with app.app_context():
                    try:
                        for source in list_sources():
                            if not source.get("enabled"):
                                continue
                            if not _due(source.get("last_run_at"), int(source.get("interval_sec") or 900)):
                                continue
                            try:
                                result = run_source(source, dry_run=False)
                                print(f"[signal_notifier] source #{source['id']} ({source['label']}): "
                                      f"{result.get('sent', 0)} sent / {result.get('candidates', 0)} candidates"
                                      + ("" if result.get("ok") else f" — error: {result.get('error')}"))
                            except Exception as e:
                                print(f"[signal_notifier] source #{source.get('id')} error: {e}")
                    except Exception as e:
                        print(f"[signal_notifier] source sweep error: {e}")
        except Exception as e:
            print(f"[signal_notifier] watcher loop error: {e}")
        time.sleep(_WATCHER_TICK_SEC)


def start_signal_notifier_watcher(app) -> bool:
    global _WATCHER_STARTED
    with _WATCHER_LOCK:
        if _WATCHER_STARTED:
            return False
        _ensure_table()
        t = threading.Thread(target=_watcher_loop, args=(app,), daemon=True)
        t.start()
        _WATCHER_STARTED = True
        return True


# ── picker helpers for the front-end UI ──────────────────────────────────

def _picker_watchlists() -> List[Dict[str, Any]]:
    try:
        from .watchlist_manager import _ensure_tables as _ensure_wl
        _ensure_wl()
    except Exception:
        pass
    con = _conn()
    try:
        rows = con.execute(
            """SELECT w.id, w.name, COALESCE(w.is_default,0) AS is_default, COUNT(ws.id) AS symbol_count
               FROM watchlists w LEFT JOIN watchlist_symbols ws ON ws.watchlist_id = w.id
               GROUP BY w.id ORDER BY w.is_default DESC, LOWER(w.name)"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _picker_dashboards() -> List[Dict[str, Any]]:
    from .scanner_dashboard import _list_dashboards
    dashboards = _list_dashboards()
    out = []
    for d in dashboards:
        out.append({
            "id": d.get("id"),
            "name": d.get("name"),
            "tiles": [{"id": t.get("id"), "title": t.get("title"), "query_text": t.get("query_text")}
                      for t in (d.get("tiles") or [])],
        })
    return out


def _picker_scanner_definitions() -> List[Dict[str, Any]]:
    from .scanner_builder import _conn as _sb_conn
    con = _sb_conn()
    try:
        rows = con.execute(
            "SELECT id, name, query_text, watchlist_id, benchmark FROM scanner_definitions ORDER BY LOWER(name)"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ── Flask routes ─────────────────────────────────────────────────────────

@signal_notifier_bp.route("", strict_slashes=False)
def page():
    _ensure_table()
    return render_template("signal_notifier.html")


@signal_notifier_bp.route("/config", methods=["GET"])
def api_get_config():
    return jsonify(get_config())


@signal_notifier_bp.route("/config", methods=["POST"])
def api_set_config():
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(set_config(**body))


@signal_notifier_bp.route("/run", methods=["POST"])
def api_run_now():
    body = request.get_json(force=True, silent=True) or {}
    result = run_signal_scan(
        watchlist_id=body.get("watchlist_id"),
        min_score=body.get("min_score"),
        dry_run=bool(body.get("dry_run", False)),
    )
    return jsonify(result)


def _query_alerts(date_from: str = "", date_to: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    _ensure_table()
    con = _conn()
    try:
        clauses = []
        params: list = []
        if date_from:
            clauses.append("alert_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("alert_date <= ?")
            params.append(date_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = con.execute(
            f"""SELECT * FROM signal_notifier_alerts
               {where}
               ORDER BY id DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


@signal_notifier_bp.route("/history", methods=["GET"])
def api_history():
    limit = int(request.args.get("limit", 200))
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    return jsonify({"ok": True, "alerts": _query_alerts(date_from, date_to, limit)})


@signal_notifier_bp.route("/history/export.csv", methods=["GET"])
def api_history_export_csv():
    """CSV export of alert history — symbol, source, grade/score, suggested
    expiry/strikes, and spot price at alert time, so this can be pasted
    into a spreadsheet to backtest recommendations against what actually
    happened by expiry."""
    import csv
    import io
    from flask import Response

    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    limit = int(request.args.get("limit", 5000))
    rows = _query_alerts(date_from, date_to, limit)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Time", "Alert Date", "Symbol", "Source", "Source Kind", "Bucket",
        "Trade Type", "Grade", "Score", "POP %", "Spot Price", "Expiry", "DTE",
        "Strikes / Legs", "Rationale", "Manage Plan", "Telegram Delivered", "Message",
        "AI Verdict", "AI Reasoning", "AI Risk Flags", "AI Sizing Note",
    ])
    for r in rows:
        try:
            risk_flags = ", ".join(json.loads(r.get("ai_risk_flags") or "[]"))
        except Exception:
            risk_flags = ""
        writer.writerow([
            r.get("sent_at", ""), r.get("alert_date", ""), r.get("symbol", ""),
            r.get("source_label", ""), r.get("source_kind", ""), r.get("bucket", ""),
            r.get("trade_type", ""), r.get("grade", ""), r.get("score", ""),
            r.get("pop", ""), r.get("symbol_price", ""), r.get("expiry", ""), r.get("dte", ""),
            r.get("legs", ""), r.get("rationale", ""), r.get("manage", ""),
            "yes" if r.get("telegram_ok") else "no", r.get("message", ""),
            r.get("ai_verdict", "") or "", r.get("ai_reasoning", "") or "",
            risk_flags, r.get("ai_sizing_note", "") or "",
        ])

    fname = f"signal_notifier_alerts_{date_from or 'all'}_{date_to or 'all'}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )



@signal_notifier_bp.route("/sources", methods=["GET"])
def api_list_sources():
    try:
        return jsonify({"ok": True, "sources": list_sources()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@signal_notifier_bp.route("/sources", methods=["POST"])
def api_create_source():
    body = request.get_json(force=True, silent=True) or {}
    try:
        src = create_source(body)
        return jsonify({"ok": True, "source": src})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@signal_notifier_bp.route("/sources/<int:source_id>", methods=["GET"])
def api_get_source(source_id: int):
    src = get_source(source_id)
    if not src:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "source": src})


@signal_notifier_bp.route("/sources/<int:source_id>", methods=["PUT"])
def api_update_source(source_id: int):
    body = request.get_json(force=True, silent=True) or {}
    try:
        src = update_source(source_id, body)
        if not src:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "source": src})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@signal_notifier_bp.route("/sources/<int:source_id>", methods=["DELETE"])
def api_delete_source(source_id: int):
    try:
        delete_source(source_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@signal_notifier_bp.route("/sources/<int:source_id>/run", methods=["POST"])
def api_run_source(source_id: int):
    body = request.get_json(force=True, silent=True) or {}
    src = get_source(source_id)
    if not src:
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        result = run_source(src, dry_run=bool(body.get("dry_run", True)))
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@signal_notifier_bp.route("/telegram-status", methods=["GET"])
def api_telegram_status():
    from ..services.telegram_alerts import telegram_configured
    return jsonify({"ok": True, "telegram_configured": telegram_configured()})


@signal_notifier_bp.route("/picker/watchlists", methods=["GET"])
def api_picker_watchlists():
    try:
        return jsonify({"ok": True, "watchlists": _picker_watchlists()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "watchlists": []}), 500


@signal_notifier_bp.route("/picker/dashboards", methods=["GET"])
def api_picker_dashboards():
    try:
        return jsonify({"ok": True, "dashboards": _picker_dashboards()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "dashboards": []}), 500


@signal_notifier_bp.route("/picker/scanners", methods=["GET"])
def api_picker_scanners():
    try:
        return jsonify({"ok": True, "scanners": _picker_scanner_definitions()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "scanners": []}), 500


@signal_notifier_bp.route("/backtest/run", methods=["POST"])
def api_backtest_run():
    from .backtest_engine import run_backtest
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = run_backtest(
            date_from=(body.get("from") or "").strip(),
            date_to=(body.get("to") or "").strip(),
            force_recompute=bool(body.get("force_recompute", False)),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@signal_notifier_bp.route("/backtest/export.csv", methods=["GET"])
def api_backtest_export_csv():
    from .backtest_engine import run_backtest
    import csv
    import io
    from flask import Response

    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    result = run_backtest(date_from=date_from, date_to=date_to, force_recompute=False)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Alert Date", "Symbol", "Trade Type", "Grade", "Score", "Predicted POP %",
        "Expiry", "Strikes / Legs", "Est Credit", "Max Loss", "Expiry Price",
        "Outcome", "Realized PnL", "PnL % of Max Loss", "Source",
    ])
    for r in result.get("results", []):
        writer.writerow([
            r.get("alert_date", ""), r.get("symbol", ""), r.get("trade_type", ""),
            r.get("grade", ""), r.get("score", ""), r.get("pop", ""),
            r.get("expiry", ""), r.get("legs", ""), r.get("est_credit", ""), r.get("max_loss_amt", ""),
            r.get("bt_expiry_price", ""), r.get("bt_outcome", ""), r.get("bt_pnl", ""), r.get("bt_pnl_pct", ""),
            r.get("source_label", ""),
        ])
    fname = f"signal_notifier_backtest_{date_from or 'all'}_{date_to or 'all'}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": f'attachment; filename="{fname}"'})
