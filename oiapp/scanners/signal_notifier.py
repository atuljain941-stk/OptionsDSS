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

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH
MAX_WORKERS = 6

_WATCHER_STARTED = False
_WATCHER_LOCK = threading.Lock()
_WATCHER_TICK_SEC = 30  # how often the background loop wakes up to check sources

SOURCE_KINDS = ("trade_scanner", "dashboard_tile", "scanner_query", "swing_positioning")


# ── DB helpers ──────────────────────────────────────────────────────────

def _conn():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
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
                schedule_kind TEXT NOT NULL DEFAULT 'interval',
                schedule_times TEXT DEFAULT '[]',
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
        if "combine_alerts" not in src_cols:
            con.execute("ALTER TABLE signal_notifier_sources ADD COLUMN combine_alerts INTEGER DEFAULT 0")
        if "score_expr" not in src_cols:
            con.execute("ALTER TABLE signal_notifier_sources ADD COLUMN score_expr TEXT DEFAULT 'Score()'")
        if "schedule_kind" not in src_cols:
            con.execute("ALTER TABLE signal_notifier_sources ADD COLUMN schedule_kind TEXT NOT NULL DEFAULT 'interval'")
        if "schedule_times" not in src_cols:
            con.execute("ALTER TABLE signal_notifier_sources ADD COLUMN schedule_times TEXT DEFAULT '[]'")
        if "min_confidence" not in src_cols:
            con.execute("ALTER TABLE signal_notifier_sources ADD COLUMN min_confidence INTEGER DEFAULT 40")
        if "min_total_oi" not in src_cols:
            con.execute("ALTER TABLE signal_notifier_sources ADD COLUMN min_total_oi INTEGER DEFAULT 50000")
        if "min_avg_daily_volume" not in src_cols:
            con.execute("ALTER TABLE signal_notifier_sources ADD COLUMN min_avg_daily_volume REAL DEFAULT 1000000")
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


def _validate_schedule_times(v: Any) -> List[str]:
    """Shared HH:MM validation/normalization for schedule_times, used by
    both the global config (set_config) and individual sources
    (create_source/update_source) so the two can't silently drift into
    accepting different formats."""
    times = [str(t).strip() for t in (v or []) if str(t).strip()]
    for t in times:
        parts = t.split(":")
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            raise ValueError(f"Invalid time format: {t} (use HH:MM)")
    return times


def get_config() -> Dict[str, Any]:
    import json as _json
    try:
        schedule_times = _json.loads(_get_setting("signal_notifier_schedule_times", "[]") or "[]")
        if not isinstance(schedule_times, list):
            schedule_times = []
    except Exception:
        schedule_times = []
    return {
        "enabled": _get_setting("signal_notifier_enabled", "0") == "1",
        "interval_sec": int(_get_setting("signal_notifier_interval_sec", "900") or 900),
        # schedule_kind: "interval" (run every interval_sec, the original/
        # default behavior) or "time" (run at each HH:MM in schedule_times,
        # once per slot per day) -- lets the default trade-scanner pass run
        # either "every X minutes" or "at specific times of day", your choice.
        "schedule_kind": _get_setting("signal_notifier_schedule_kind", "interval") or "interval",
        "schedule_times": [t for t in schedule_times if isinstance(t, str) and t.strip()],
        "min_score": int(_get_setting("signal_notifier_min_score", "70") or 70),
        "watchlist_id": _get_setting("signal_notifier_watchlist_id", "") or None,
        "min_regap_pts": int(_get_setting("signal_notifier_min_regap_pts", "8") or 8),
        "ai_gate_enabled": _get_setting("signal_notifier_ai_gate_enabled", "0") == "1",
        # Journal (trade P&L/risk + position health) and Telegram
        # watchlist-price alert checks now run from THIS background loop
        # instead of their own separate always-on watchers -- configured
        # here, each with its own independent enable + interval (these
        # are typically time-sensitive and want to be checked much more
        # often than the main opportunity-scanner pass above, so they're
        # NOT tied to schedule_kind/schedule_times).
        "journal_pnl_alerts_enabled": _get_setting("signal_notifier_journal_pnl_enabled", "1") == "1",
        # Default 4h (14400s), not 60s: the underlying inputs simply don't
        # change minute-to-minute. OI only refreshes once per fetch cycle,
        # and the P&L/DTE/PNR rules these alerts fire on move on the scale
        # of hours, not seconds -- a 60s cadence spent most of its runs
        # recomputing identical state and re-checking dedup guards. Still
        # fully configurable from the Signal Notifier page (this is only
        # the default for a setting that already existed), and the 30s
        # floor in the loop below is unchanged for anyone who wants it
        # tighter.
        "journal_pnl_alerts_interval_sec": int(_get_setting("signal_notifier_journal_pnl_interval_sec", "14400") or 14400),
        # Deep-loss alert: fires once when an open position's unrealised
        # loss first crosses this % of its max loss -- a 50%-of-max-loss
        # position is common and often recoverable; this default (75%) is
        # set for the much rarer, much harder to recover from case. Global,
        # not per-trade -- one threshold for every open position. Rides
        # along with the P&L check above (same interval), since it needs
        # the same live P&L computation anyway.
        "journal_deep_loss_alerts_enabled": _get_setting("signal_notifier_journal_deep_loss_enabled", "1") == "1",
        "journal_deep_loss_threshold_pct": float(_get_setting("signal_notifier_journal_deep_loss_threshold_pct", "75") or 75),
        "journal_health_alerts_enabled": _get_setting("signal_notifier_journal_health_enabled", "1") == "1",
        "journal_health_alerts_interval_sec": int(_get_setting("signal_notifier_journal_health_interval_sec", "300") or 300),
        "telegram_price_alerts_enabled": _get_setting("signal_notifier_telegram_price_enabled", "1") == "1",
        "telegram_price_alerts_interval_sec": int(_get_setting("signal_notifier_telegram_price_interval_sec", "60") or 60),
    }


def set_config(**kwargs) -> Dict[str, Any]:
    import json as _json
    mapping = {
        "enabled": "signal_notifier_enabled",
        "interval_sec": "signal_notifier_interval_sec",
        "schedule_kind": "signal_notifier_schedule_kind",
        "min_score": "signal_notifier_min_score",
        "watchlist_id": "signal_notifier_watchlist_id",
        "min_regap_pts": "signal_notifier_min_regap_pts",
        "ai_gate_enabled": "signal_notifier_ai_gate_enabled",
        "journal_pnl_alerts_enabled": "signal_notifier_journal_pnl_enabled",
        "journal_pnl_alerts_interval_sec": "signal_notifier_journal_pnl_interval_sec",
        "journal_deep_loss_alerts_enabled": "signal_notifier_journal_deep_loss_enabled",
        "journal_deep_loss_threshold_pct": "signal_notifier_journal_deep_loss_threshold_pct",
        "journal_health_alerts_enabled": "signal_notifier_journal_health_enabled",
        "journal_health_alerts_interval_sec": "signal_notifier_journal_health_interval_sec",
        "telegram_price_alerts_enabled": "signal_notifier_telegram_price_enabled",
        "telegram_price_alerts_interval_sec": "signal_notifier_telegram_price_interval_sec",
    }
    _bool_settings = ("enabled", "ai_gate_enabled", "journal_pnl_alerts_enabled", "journal_deep_loss_alerts_enabled",
                       "journal_health_alerts_enabled", "telegram_price_alerts_enabled")
    for k, v in kwargs.items():
        if k == "schedule_times":
            if v is not None:
                times = _validate_schedule_times(v)
                _set_setting("signal_notifier_schedule_times", _json.dumps(times))
            continue
        if k == "schedule_kind":
            if v not in (None, "interval", "time"):
                raise ValueError("schedule_kind must be 'interval' or 'time'")
        if k in mapping and v is not None:
            is_bool_setting = k in _bool_settings
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
        spot = opp.get("spot")
        earn_date = opp.get("earn_date")
        earn_days = opp.get("earn_days")
        spot_earn_bits = []
        if spot is not None:
            spot_earn_bits.append(f"Spot: ${spot}")
        if earn_date:
            spot_earn_bits.append(f"Earnings: {earn_date}" + (f" ({earn_days}d)" if earn_days is not None else ""))
        if spot_earn_bits:
            lines.append(" | ".join(spot_earn_bits))
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
    risk_factors = opp.get("risk_factors") or []
    if risk_factors:
        # Same story as cons above: _scan_one already computes these
        # (earnings falling before expiry, a short strike sitting right on
        # a wall, spot already close to the point-of-no-return) -- they
        # were just never read here, so a real, already-identified risk
        # like "this spread expires the day AFTER earnings" could be
        # sitting in the data behind the alert without ever reaching the
        # message someone actually reads.
        lines.append("⚠ Risk: " + "; ".join(risk_factors))
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
    from ..services.task_executor import get_background_executor
    ex = get_background_executor()
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


def _format_swing_message(symbol: str, r: Dict[str, Any], source_label: str = "") -> str:
    """Mirrors _format_message's shape (headline / Why: / risk note) for a
    Swing Positioning result -- deliberately NOT reusing _format_message
    itself, since that function is built around an options-trade opp
    dict (legs, credit, POP/RR) and a swing-positioning result has no
    trade structure at all, just a directional read. Keeping "Why: " as
    the pros-style line for consistency with how the rest of this app's
    alert text reads, even though there's no equivalent "Caution: " line
    here (no POP/RR math to warn about for a bias read, not a trade)."""
    walls = r.get("walls") or {}
    bias_word = "BULLISH" if r["bias"] == "bullish" else "BEARISH"
    lines = [
        f"📐 Swing positioning: {symbol}",
        f"{bias_word} — {r['confidence']}% confidence | Spot ${r['spot']:.2f}",
    ]
    if walls.get("call_wall") or walls.get("put_wall"):
        cw, pw = walls.get("call_wall"), walls.get("put_wall")
        wall_bits = []
        if cw: wall_bits.append(f"Call wall ${cw['strike']:.2f}")
        if pw: wall_bits.append(f"Put wall ${pw['strike']:.2f}")
        lines.append(f"Swing walls ({walls.get('expiry', '')}, {walls.get('dte', '')}d): {' · '.join(wall_bits)}")
    lines.append(f"Why: {r.get('narrative', '')}")
    lines.append("⚠ Risk: this is a directional/positioning read, not a specific trade -- "
                  "no defined entry, stop, or POP/RR math the way an options-trade alert has. "
                  "Treat as a starting point for your own trade structuring, not a ready signal.")
    if source_label:
        lines.append(f"— {source_label}")
    return "\n".join(lines)


def _run_swing_positioning_scanner(*, watchlist_id: Optional[str], min_confidence: int,
                                    min_total_oi: int = None, min_avg_daily_volume: float = None,
                                    dry_run: bool, source_id: Optional[int] = None,
                                    source_label: str = "Swing Positioning Scanner") -> Dict[str, Any]:
    from .swing_positioning import scan_watchlist, DEFAULT_MIN_TOTAL_OI, DEFAULT_MIN_AVG_DAILY_VOLUME
    from .trade_opportunity_scanner import _watchlist_symbols
    from ..services.telegram_alerts import send_telegram_message, telegram_configured

    symbols = _watchlist_symbols(watchlist_id) if watchlist_id else None
    if watchlist_id and not symbols:
        return {"ok": False, "error": "No symbols found for watchlist", "watchlist_id": watchlist_id}

    scan = scan_watchlist(
        symbols=symbols, min_confidence=min_confidence,
        min_total_oi=min_total_oi if min_total_oi is not None else DEFAULT_MIN_TOTAL_OI,
        min_avg_daily_volume=min_avg_daily_volume if min_avg_daily_volume is not None else DEFAULT_MIN_AVG_DAILY_VOLUME,
    )
    if not scan.get("ok"):
        return {"ok": False, "error": scan.get("error", "Swing positioning scan failed")}

    sent, skipped = [], []
    can_send = telegram_configured() and not dry_run

    for r in scan.get("results", []):
        symbol = r["symbol"]
        # One alert per symbol per day, same rule trade_scanner alerts use --
        # a swing positioning read doesn't have "legs" to dedupe a second way
        # against (it's not a specific trade structure), so this one check
        # is the whole dedupe rule here, not a partial version of the
        # trade_scanner one.
        if _is_duplicate_recommendation(symbol):
            skipped.append(symbol)
            continue

        msg = _format_swing_message(symbol, r, source_label=source_label)
        ok = False
        if can_send:
            resp = send_telegram_message(msg)
            ok = bool(resp.get("ok"))

        # opp-shaped dict for _log_alert -- that function is generic/
        # defensive (opp.get(...) with fallbacks everywhere), so a swing
        # positioning result maps onto it without needing to touch
        # _log_alert itself: trade_type="SCAN" (falls back correctly,
        # matches its own "SCAN" default when trade_type is absent),
        # score=confidence, rationale=narrative, price=spot.
        opp = {
            "trade_type": "SWING", "grade": r["bias"].upper()[:1], "score": r["confidence"],
            "rationale": r.get("narrative", ""), "spot": r["spot"],
            "expiry": (r.get("walls") or {}).get("expiry", ""),
        }
        bucket = "SWING_BULLISH" if r["bias"] == "bullish" else "SWING_BEARISH"
        if not dry_run:
            _log_alert(symbol, bucket, opp, ok, source_id=source_id, source_kind="swing_positioning",
                        source_label=source_label, message=msg)
        sent.append({"symbol": symbol, "bias": r["bias"], "confidence": r["confidence"],
                      "telegram_ok": ok, "message": msg})

    return {
        "ok": True, "scanned": scan.get("scanned", 0), "candidates": len(scan.get("results", [])),
        "sent": len(sent), "skipped_duplicate": len(skipped),
        "telegram_configured": telegram_configured(), "alerts": sent,
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


def _send_combined_alert(passed: List[Dict[str, Any]], *, source_id: Optional[int], source_kind: str,
                          source_label: str, score_expr: str, symbols_scanned: int, dry_run: bool) -> Dict[str, Any]:
    """The actual thing this was built for: instead of one Telegram
    push per matching symbol (unusable at 200+ stocks -- that's the
    whole problem this exists to solve), evaluate score_expr for every
    match, sort by strength, and send ONE combined digest message with
    every symbol and its score. Dedup is per SOURCE per DAY here, not
    per symbol -- a daily digest is supposed to re-list whatever
    matches today, so per-symbol "already alerted" dedup would be the
    wrong rule for this mode specifically."""
    from .scanner_builder import _eval as _sb_eval, _parse_query as _sb_parse_query
    from ..services.telegram_alerts import send_telegram_message, telegram_configured

    if not passed:
        return {"ok": True, "scanned": symbols_scanned, "candidates": 0, "sent": 0, "skipped_duplicate": 0,
                "telegram_configured": telegram_configured(), "alerts": [], "note": "No matches today -- nothing sent."}

    if source_id is not None and _combined_alert_already_sent_today(source_id):
        return {"ok": True, "scanned": symbols_scanned, "candidates": len(passed), "sent": 0,
                "skipped_duplicate": len(passed), "telegram_configured": telegram_configured(), "alerts": [],
                "note": "Combined digest already sent today for this source."}

    try:
        score_node = _sb_parse_query(score_expr) if score_expr else None
    except Exception:
        score_node = None

    scored = []
    for row in passed:
        score_val = None
        if score_node is not None:
            try:
                score_val = _sb_eval(score_node, row["_ctx"], shift=0, tf_default="1d")
            except Exception:
                score_val = None
        scored.append({"symbol": row["symbol"], "price": row.get("price"), "score": score_val, "pros": row.get("pros") or []})
    scored.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))

    # "Comma-separated list with strength and score" -- literally that,
    # one line per symbol so it's still scannable at a glance even with
    # 30-40 matches, not one giant unreadable comma-blob.
    lines = [f"{r['symbol']}: {round(r['score'], 1) if r['score'] is not None else '—'}" for r in scored]
    header = f"📋 {source_label} -- {len(scored)} match(es) out of {symbols_scanned} scanned ({_today()})"
    body = ", ".join(lines)
    message = f"{header}\n\n{body}"

    can_send = telegram_configured() and not dry_run
    ok = False
    if can_send:
        resp = send_telegram_message(message)
        ok = bool(resp.get("ok"))

    if not dry_run:
        for r in scored:
            opp = {"trade_type": "SCAN", "score": r["score"], "pros": r["pros"]}
            _log_alert(r["symbol"], "COMBINED", opp, ok, source_id=source_id, source_kind=source_kind,
                       source_label=source_label, message=message)
        if source_id is not None:
            _mark_combined_alert_sent(source_id)

    return {"ok": True, "scanned": symbols_scanned, "candidates": len(scored), "sent": len(scored) if ok else 0,
            "skipped_duplicate": 0, "telegram_configured": telegram_configured(),
            "alerts": [{"symbol": r["symbol"], "score": r["score"], "telegram_ok": ok} for r in scored],
            "combined_message": message}


def _combined_alert_already_sent_today(source_id: int) -> bool:
    con = _conn()
    try:
        row = con.execute(
            "SELECT 1 FROM signal_notifier_alerts WHERE source_id=? AND bucket='COMBINED' AND alert_date=? LIMIT 1",
            (source_id, _today()),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def _mark_combined_alert_sent(source_id: int) -> None:
    pass  # the COMBINED-bucket row(s) _log_alert already writes ARE the record _combined_alert_already_sent_today checks -- nothing extra to persist


def _run_scanner_query(*, query_text: str, watchlist_id: Optional[str], benchmark: str,
                        dry_run: bool, source_id: Optional[int], source_kind: str, source_label: str,
                        min_conviction_score: int = 0, direction_tags: Optional[List[str]] = None,
                        combine_alerts: bool = False, score_expr: str = "Score()") -> Dict[str, Any]:
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
    from ..services.task_executor import get_background_executor
    ex = get_background_executor()
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
                    "_ctx": ctx,  # only used by the combine_alerts path below to evaluate score_expr without re-scanning; never serialized/logged
                })

    if combine_alerts:
        return _send_combined_alert(passed, source_id=source_id, source_kind=source_kind,
                                     source_label=source_label, score_expr=score_expr,
                                     symbols_scanned=len(symbols), dry_run=dry_run)

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
    try:
        d["schedule_times"] = json.loads(d.get("schedule_times") or "[]")
        if not isinstance(d["schedule_times"], list):
            d["schedule_times"] = []
    except Exception:
        d["schedule_times"] = []
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
    schedule_kind = str(payload.get("schedule_kind") or "interval")
    if schedule_kind not in ("interval", "time"):
        raise ValueError("schedule_kind must be 'interval' or 'time'")
    schedule_times = json.dumps(_validate_schedule_times(payload.get("schedule_times")))
    watchlist_id = str(payload.get("watchlist_id") or "")
    min_score = int(payload.get("min_score") or 70)
    min_conviction_score = max(0, int(payload.get("min_conviction_score") or 0))
    direction_tags = json.dumps(_normalize_direction_tags(payload.get("direction_tags")))
    benchmark = str(payload.get("benchmark") or "SPY").upper()
    dashboard_id = payload.get("dashboard_id")
    tile_id = payload.get("tile_id")
    definition_id = payload.get("definition_id")
    query_text = payload.get("query_text")
    min_confidence = max(0, min(100, int(payload.get("min_confidence") or 40)))
    min_total_oi = max(0, int(payload.get("min_total_oi") or 50000))
    min_avg_daily_volume = max(0.0, float(payload.get("min_avg_daily_volume") or 1000000))

    con = _conn()
    try:
        cur = con.execute(
            """INSERT INTO signal_notifier_sources
               (kind, label, enabled, interval_sec, schedule_kind, schedule_times,
                watchlist_id, min_score, min_conviction_score,
                direction_tags, benchmark, dashboard_id, tile_id, definition_id, query_text,
                combine_alerts, score_expr, min_confidence, min_total_oi, min_avg_daily_volume,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (kind, label, enabled, interval_sec, schedule_kind, schedule_times,
             watchlist_id, min_score, min_conviction_score,
             direction_tags, benchmark, dashboard_id, tile_id, definition_id, query_text,
             1 if payload.get("combine_alerts") in (True, 1, "1") else 0,
             str(payload.get("score_expr") or "Score()"),
             min_confidence, min_total_oi, min_avg_daily_volume),
        )
        con.commit()
        sid = cur.lastrowid
        row = con.execute("SELECT * FROM signal_notifier_sources WHERE id=?", (sid,)).fetchone()
        return _source_row_to_dict(row)
    finally:
        con.close()


def _validate_schedule_kind(v: Any) -> str:
    if v not in ("interval", "time"):
        raise ValueError("schedule_kind must be 'interval' or 'time'")
    return v


def update_source(source_id: int, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    _ensure_table()
    existing = get_source(source_id)
    if not existing:
        return None
    fields = {
        "label": str,
        "enabled": lambda v: 1 if v in (True, 1, "1") else 0,
        "interval_sec": lambda v: max(60, int(v)),
        "schedule_kind": _validate_schedule_kind,
        "schedule_times": lambda v: json.dumps(_validate_schedule_times(v)),
        "watchlist_id": str,
        "min_score": int,
        "min_conviction_score": lambda v: max(0, int(v)),
        "direction_tags": lambda v: json.dumps(_normalize_direction_tags(v)),
        "benchmark": lambda v: str(v).upper(),
        "dashboard_id": lambda v: v,
        "tile_id": lambda v: v,
        "definition_id": lambda v: v,
        "query_text": lambda v: v,
        "combine_alerts": lambda v: 1 if v in (True, 1, "1") else 0,
        "score_expr": lambda v: str(v),
        "min_confidence": lambda v: max(0, min(100, int(v))),
        "min_total_oi": lambda v: max(0, int(v)),
        "min_avg_daily_volume": lambda v: max(0.0, float(v)),
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
                    combine_alerts=bool(source.get("combine_alerts")),
                    score_expr=str(source.get("score_expr") or "Score()"),
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
                combine_alerts=bool(source.get("combine_alerts")),
                score_expr=str(source.get("score_expr") or "Score()"),
            )
        elif kind == "swing_positioning":
            result = _run_swing_positioning_scanner(
                watchlist_id=source.get("watchlist_id") or None,
                min_confidence=int(source.get("min_confidence") or 40),
                min_total_oi=int(source.get("min_total_oi") or 0) or None,
                min_avg_daily_volume=float(source.get("min_avg_daily_volume") or 0) or None,
                dry_run=dry_run, source_id=sid, source_label=label,
            )
        else:
            result = {"ok": False, "error": f"Unknown source kind: {kind}"}
    except Exception as e:
        result = {"ok": False, "error": str(e)}

    if sid is not None and not dry_run:
        _mark_source_run(sid, int(result.get("sent") or 0), None if result.get("ok") else result.get("error"))
    return result


# ── background scheduler thread ──────────────────────────────────────────

def _source_due(source: Dict[str, Any], now: datetime) -> bool:
    """Per-source counterpart to _default_pass_due() above -- same two
    modes (interval vs specific times-of-day), just reading from a
    source dict's schedule_kind/schedule_times/interval_sec instead of
    the global config dict. Extracted as its own function rather than
    inlined at the call site so create_source/update_source's validation
    and this due-check can't drift apart from what schedule_kind actually
    means.
    """
    last_run_at = source.get("last_run_at")
    try:
        legacy_last_run = datetime.fromisoformat(last_run_at) if last_run_at else None
    except Exception:
        legacy_last_run = None

    if source.get("schedule_kind") == "time":
        times = source.get("schedule_times") or []
        if not times:
            return False  # time mode selected but nothing configured yet -- never fire
        for t_str in times:
            try:
                hh, mm = [int(x) for x in str(t_str).split(":")[:2]]
            except Exception:
                continue
            scheduled = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now >= scheduled and (legacy_last_run is None or legacy_last_run < scheduled):
                return True
        return False
    interval = max(60, int(source.get("interval_sec") or 900))
    return legacy_last_run is None or (now - legacy_last_run).total_seconds() >= interval


def _due(last_run_at: Optional[str], interval_sec: int) -> bool:
    if not last_run_at:
        return True
    try:
        last = datetime.fromisoformat(last_run_at)
    except Exception:
        return True
    return (datetime.now() - last).total_seconds() >= interval_sec


def _default_pass_due(cfg: Dict[str, Any], legacy_last_run: Optional[datetime], now: datetime) -> bool:
    """Due-check for the default trade-scanner pass, honoring whichever
    schedule mode is configured:
      - schedule_kind='interval' (default, original behavior): due every
        interval_sec seconds since it last ran.
      - schedule_kind='time': due once per configured HH:MM slot per day
        -- fires the moment 'now' reaches/passes a scheduled time it
        hasn't already run for since that time began (checked every
        _WATCHER_TICK_SEC, so it fires within ~30s of the scheduled
        minute, not exactly on it -- same tolerance every other
        time-kind job in this app already has)."""
    if cfg.get("schedule_kind") == "time":
        times = cfg.get("schedule_times") or []
        if not times:
            return False  # time mode selected but nothing configured yet -- never fire
        for t_str in times:
            try:
                hh, mm = [int(x) for x in str(t_str).split(":")[:2]]
            except Exception:
                continue
            scheduled = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now >= scheduled and (legacy_last_run is None or legacy_last_run < scheduled):
                return True
        return False
    interval = max(60, cfg.get("interval_sec", 900))
    return legacy_last_run is None or (now - legacy_last_run).total_seconds() >= interval


def _watcher_loop(app):
    from ..services.job_registry import register_job
    _sn_cfg = get_config()
    if _sn_cfg.get("schedule_kind") == "time":
        _sn_desc = (
            f"Runs the Trade Opportunity Scanner sweep plus any configured alert sources, "
            f"at {', '.join(_sn_cfg.get('schedule_times') or ['(no times configured)'])} daily. "
            f"Configure schedule at /signal-notifier."
        )
    else:
        _sn_desc = (
            f"Runs the Trade Opportunity Scanner sweep plus any configured alert sources, "
            f"every {max(1, _sn_cfg['interval_sec'] // 60)} min. Configure schedule at /signal-notifier."
        )
    register_job(
        "signal_notifier", "Signal Notifier", _sn_desc,
        kind="interval", default_schedule={"interval_min": max(1, int(_sn_cfg["interval_sec"] / 60))},
        group="Alert Watchers", run_now_fn=lambda: run_signal_scan(dry_run=False), editable=False,
    )
    # tracks last-run for the legacy default trade-scanner pass separately
    legacy_last_run: Optional[datetime] = None
    # tracks last-run for the three checks folded in from their old
    # separate always-on watchers (journal P&L, journal health, telegram
    # price alerts) -- each keeps its own independent interval, checked
    # every tick via the plain _due() helper (interval-only -- these three
    # are deliberately NOT given the time-of-day option sources/the
    # default pass have, since they're meant to run frequently all day).
    journal_pnl_last_run: Optional[datetime] = None
    journal_deep_loss_last_run: Optional[datetime] = None
    journal_health_last_run: Optional[datetime] = None
    telegram_price_last_run: Optional[datetime] = None

    while True:
        try:
            cfg = get_config()
            if cfg["enabled"]:
                now = datetime.now()
                # legacy/default trade-scanner sweep (backward compatible),
                # now honoring schedule_kind ('interval' or 'time' -- see
                # _default_pass_due()) instead of always being interval-only.
                if _default_pass_due(cfg, legacy_last_run, now):
                    with app.app_context():
                        from ..services.job_registry import mark_run as _mark_run, log_run_start as _log_start, log_run_finish as _log_finish
                        _run_id = _log_start("signal_notifier")
                        try:
                            result = run_signal_scan()
                            print(f"[signal_notifier] default trade-scanner pass: "
                                  f"{result.get('sent', 0)} sent / {result.get('candidates', 0)} candidates")
                            _note = f"sent={result.get('sent', 0)} candidates={result.get('candidates', 0)}"
                            _mark_run("signal_notifier", True, _note)
                            _log_finish(_run_id, True, _note)
                        except Exception as e:
                            print(f"[signal_notifier] default pass error: {e}")
                            _mark_run("signal_notifier", False, str(e))
                            _log_finish(_run_id, False, str(e))
                    legacy_last_run = now

                # Journal: trade P&L / risk-level alerts (folded in from
                # the old separate trade_pnr_alerts watcher).
                if cfg.get("journal_pnl_alerts_enabled", True) and (
                    journal_pnl_last_run is None
                    or (now - journal_pnl_last_run).total_seconds() >= max(30, cfg.get("journal_pnl_alerts_interval_sec", 14400))
                ):
                    with app.app_context():
                        from ..services.job_registry import mark_run as _mark_run, log_run_start as _log_start, log_run_finish as _log_finish
                        _run_id = _log_start("trade_pnr_alerts")
                        try:
                            from ..journal.journal_routes import _scan_trade_pnr_alerts_once
                            _scan_trade_pnr_alerts_once()
                            _mark_run("trade_pnr_alerts", True, "checked via signal_notifier")
                            _log_finish(_run_id, True, "checked via signal_notifier")
                        except Exception as e:
                            print(f"[signal_notifier] journal P&L alert check error: {e}")
                            try:
                                _mark_run("trade_pnr_alerts", False, str(e))
                                _log_finish(_run_id, False, str(e))
                            except Exception:
                                pass
                    journal_pnl_last_run = now

                # Journal: deep-loss alert -- a 50%-of-max-loss position is
                # common and often still recoverable; crossing the
                # (configurable, default 75%) threshold is much rarer and,
                # per the reasoning behind this alert, close to a point of
                # no return in practice. Same check-once-per-interval shape
                # as the P&L/PNR check above, reusing the same
                # journal_pnl_alerts_interval_sec cadence (no separate
                # interval setting -- this rides along with the same P&L
                # pass since it needs the same live P&L computation anyway).
                if cfg.get("journal_deep_loss_alerts_enabled", True) and (
                    journal_deep_loss_last_run is None
                    or (now - journal_deep_loss_last_run).total_seconds() >= max(30, cfg.get("journal_pnl_alerts_interval_sec", 14400))
                ):
                    with app.app_context():
                        from ..services.job_registry import mark_run as _mark_run, log_run_start as _log_start, log_run_finish as _log_finish
                        _run_id = _log_start("trade_deep_loss_alerts")
                        try:
                            from ..journal.journal_routes import _scan_trade_deep_loss_alerts_once
                            _scan_trade_deep_loss_alerts_once()
                            _mark_run("trade_deep_loss_alerts", True, "checked via signal_notifier")
                            _log_finish(_run_id, True, "checked via signal_notifier")
                        except Exception as e:
                            print(f"[signal_notifier] deep-loss alert check error: {e}")
                            try:
                                _mark_run("trade_deep_loss_alerts", False, str(e))
                                _log_finish(_run_id, False, str(e))
                            except Exception:
                                pass
                    journal_deep_loss_last_run = now

                # Journal: position health score alerts (folded in from
                # the old separate trade_health_alerts watcher).
                if cfg.get("journal_health_alerts_enabled", True) and (
                    journal_health_last_run is None
                    or (now - journal_health_last_run).total_seconds() >= max(30, cfg.get("journal_health_alerts_interval_sec", 300))
                ):
                    with app.app_context():
                        from ..services.job_registry import mark_run as _mark_run, log_run_start as _log_start, log_run_finish as _log_finish
                        _run_id = _log_start("trade_health_alerts")
                        try:
                            from ..journal.journal_routes import _scan_health_alerts_and_notify
                            _scan_health_alerts_and_notify()
                            _mark_run("trade_health_alerts", True, "checked via signal_notifier")
                            _log_finish(_run_id, True, "checked via signal_notifier")
                        except Exception as e:
                            print(f"[signal_notifier] journal health alert check error: {e}")
                            try:
                                _mark_run("trade_health_alerts", False, str(e))
                                _log_finish(_run_id, False, str(e))
                            except Exception:
                                pass
                    journal_health_last_run = now

                # Telegram: watchlist price alerts (folded in from the
                # old separate telegram_price_alerts watcher).
                if cfg.get("telegram_price_alerts_enabled", True) and (
                    telegram_price_last_run is None
                    or (now - telegram_price_last_run).total_seconds() >= max(30, cfg.get("telegram_price_alerts_interval_sec", 60))
                ):
                    with app.app_context():
                        from ..services.job_registry import mark_run as _mark_run, log_run_start as _log_start, log_run_finish as _log_finish
                        _run_id = _log_start("telegram_price_alerts")
                        try:
                            from ..services.telegram_alerts import check_watchlist_price_alerts
                            result = check_watchlist_price_alerts()
                            _note = f"checked={result.get('checked')} sent={result.get('sent')}"
                            _mark_run("telegram_price_alerts", True, _note)
                            _log_finish(_run_id, True, _note)
                        except Exception as e:
                            print(f"[signal_notifier] telegram price alert check error: {e}")
                            try:
                                _mark_run("telegram_price_alerts", False, str(e))
                                _log_finish(_run_id, False, str(e))
                            except Exception:
                                pass
                    telegram_price_last_run = now

                # Alert outbox consumer -- delivers whatever's been queued
                # by queue_alert() (currently: telegram price alerts;
                # journal P&L/health alerts still send inline pending
                # their own migration -- see alert_outbox.py). Runs every
                # tick (not gated by an interval like the checks above)
                # since delivery should be prompt once something's queued.
                try:
                    from ..services.alert_outbox import process_due_alerts
                    outbox_result = process_due_alerts()
                    if outbox_result.get("checked"):
                        print(f"[signal_notifier] alert outbox: {outbox_result}")
                except Exception as e:
                    print(f"[signal_notifier] alert outbox consumer error: {e}")

                # per-source sweep -- this is the part most likely to run
                # long (N configured sources, each doing a full scan), so
                # it's what "running"/"Stop" on the Scheduler Hub page
                # actually reflects: marked running for the duration, and
                # checks for a stop request between each source so a
                # long sweep can be aborted without killing the watcher
                # thread itself (it just skips the rest of THIS sweep and
                # picks back up on the normal schedule next tick).
                with app.app_context():
                    from ..services.job_registry import mark_start as _mark_start, \
                        mark_finished as _mark_finished, is_stop_requested as _is_stop_requested, \
                        log_run_start as _log_start, log_run_finish as _log_finish
                    _mark_start("signal_notifier")
                    _run_id = _log_start("signal_notifier")
                    _sources_run = 0
                    try:
                        for source in list_sources():
                            if _is_stop_requested("signal_notifier"):
                                print("[signal_notifier] stop requested -- aborting rest of this source sweep")
                                break
                            if not source.get("enabled"):
                                continue
                            if not _source_due(source, datetime.now()):
                                continue
                            try:
                                result = run_source(source, dry_run=False)
                                _sources_run += 1
                                print(f"[signal_notifier] source #{source['id']} ({source['label']}): "
                                      f"{result.get('sent', 0)} sent / {result.get('candidates', 0)} candidates"
                                      + ("" if result.get("ok") else f" — error: {result.get('error')}"))
                                try:
                                    from .watchlist_manager import log_alert_notification
                                    ok = result.get("ok", True)
                                    log_alert_notification(
                                        "SIGNAL_SOURCE_RUN",
                                        f"Signal source \"{source['label']}\" finished",
                                        (f"{result.get('candidates', 0)} candidate(s) checked, "
                                         f"{result.get('sent', 0)} alert(s) sent") if ok else
                                        f"Failed: {result.get('error', 'unknown error')}",
                                        severity="ok" if ok else "warn",
                                        source="Signal Notifier",
                                    )
                                except Exception as notif_err:
                                    print(f"[signal_notifier] notification write failed: {notif_err}")
                            except Exception as e:
                                print(f"[signal_notifier] source #{source.get('id')} error: {e}")
                                try:
                                    from .watchlist_manager import log_alert_notification
                                    log_alert_notification(
                                        "SIGNAL_SOURCE_RUN",
                                        f"Signal source \"{source.get('label', source.get('id'))}\" failed",
                                        str(e), severity="error", source="Signal Notifier",
                                    )
                                except Exception:
                                    pass
                        _log_finish(_run_id, True, f"source sweep: {_sources_run} source(s) run")
                    except Exception as e:
                        print(f"[signal_notifier] source sweep error: {e}")
                        _log_finish(_run_id, False, str(e))
                    finally:
                        _mark_finished("signal_notifier")
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


def _query_alerts(date_from: str = "", date_to: str = "", limit: int = 200,
                   symbol: str = "", source_label: str = "") -> List[Dict[str, Any]]:
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
        if symbol:
            clauses.append("UPPER(symbol) = ?")
            params.append(symbol.upper().strip())
        if source_label:
            clauses.append("source_label = ?")
            params.append(source_label)
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
    symbol = (request.args.get("symbol") or "").strip()
    source_label = (request.args.get("source_label") or "").strip()
    return jsonify({"ok": True, "alerts": _query_alerts(date_from, date_to, limit, symbol, source_label)})


@signal_notifier_bp.route("/history/sources", methods=["GET"])
def api_history_sources():
    """Distinct source_label values actually present in the alert log --
    backs the Scanner filter dropdown on both the history table and the
    backtest panel, so the filter only ever offers choices that exist."""
    _ensure_table()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT DISTINCT source_label FROM signal_notifier_alerts "
            "WHERE source_label IS NOT NULL AND source_label != '' ORDER BY source_label"
        ).fetchall()
        return jsonify({"ok": True, "sources": [r[0] for r in rows]})
    finally:
        con.close()


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
    symbol = (request.args.get("symbol") or "").strip()
    source_label = (request.args.get("source_label") or "").strip()
    limit = int(request.args.get("limit", 5000))
    rows = _query_alerts(date_from, date_to, limit, symbol, source_label)

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
            symbol=(body.get("symbol") or "").strip(),
            source_label=(body.get("source_label") or "").strip(),
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
    symbol = (request.args.get("symbol") or "").strip()
    source_label = (request.args.get("source_label") or "").strip()
    result = run_backtest(date_from=date_from, date_to=date_to, force_recompute=False,
                           symbol=symbol, source_label=source_label)

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
