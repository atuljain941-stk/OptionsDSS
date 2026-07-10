# oiapp/ai/copilot.py
"""
AI Copilot — three LLM-powered agents layered on top of your existing,
deterministic data (journal, scanners, Signal Notifier history). The LLM
never sees or invents raw prices/OI numbers on its own; it only reasons
over structured summaries that we compute ourselves from SQL.

    1. Trade Journal Post-Mortem  — mines your closed trades + Signal
       Notifier alert history for what's actually working, and proposes
       concrete threshold/autotune changes.
    2. Pre-Trade Risk Checkpoint  — checks a candidate trade against your
       open-position concentration, regime alignment, earnings/macro
       conflicts, and a daily/weekly loss circuit breaker, then asks the
       LLM for an APPROVE / CAUTION / REJECT verdict with reasoning.
    3. Daily Briefing             — synthesizes regime + open positions +
       last 24h of Signal Notifier alerts + macro calendar into one
       prioritized "what needs attention today" memo.

All three log every run to their own history table so the record itself
becomes training/reference data for future post-mortems.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

from .llm_client import call_llm, call_llm_json, llm_configured, get_llm_settings, set_llm_settings

ai_copilot_bp = Blueprint("ai_copilot", __name__, url_prefix="/ai-copilot")

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")


# ── DB helpers ────────────────────────────────────────────────────────────

def _conn():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _ensure_tables():
    con = _conn()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ai_postmortem_reports (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL,
                period_days     INTEGER NOT NULL,
                trades_analyzed INTEGER NOT NULL,
                alerts_analyzed INTEGER NOT NULL,
                stats_json      TEXT,
                report_text     TEXT,
                model           TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS ai_risk_checks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                trade_type      TEXT,
                direction       TEXT,
                sector          TEXT,
                risk_amt        REAL,
                verdict         TEXT,
                reasoning       TEXT,
                risk_flags_json TEXT,
                sizing_note     TEXT,
                context_json    TEXT,
                model           TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS ai_daily_briefings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL,
                briefing_text   TEXT,
                context_json    TEXT,
                sent_telegram   INTEGER DEFAULT 0,
                model           TEXT
            )
        """)
        con.commit()
    finally:
        con.close()


def _get_setting(key: str, default: str = "") -> str:
    from ..scanners.watchlist_manager import _get_setting as _gs
    return _gs(key, default)


def _set_setting(key: str, value: str) -> None:
    from ..scanners.watchlist_manager import _set_setting as _ss
    _ss(key, value)


# ── circuit breaker (daily / weekly loss limit) ───────────────────────────
# This was a known gap — noted in the Signal Notifier integration doc as
# "the one piece that doesn't exist anywhere in the codebase yet."

def get_circuit_breaker_config() -> Dict[str, Any]:
    return {
        "max_daily_loss": float(_get_setting("risk_max_daily_loss", "0") or 0),
        "max_weekly_loss": float(_get_setting("risk_max_weekly_loss", "0") or 0),
    }


def set_circuit_breaker_config(max_daily_loss: Optional[float] = None, max_weekly_loss: Optional[float] = None) -> Dict[str, Any]:
    if max_daily_loss is not None:
        _set_setting("risk_max_daily_loss", str(float(max_daily_loss)))
    if max_weekly_loss is not None:
        _set_setting("risk_max_weekly_loss", str(float(max_weekly_loss)))
    return get_circuit_breaker_config()


def _realized_pnl_since(since_date: date) -> float:
    from ..db import _connect
    con = _connect()
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE status='CLOSED' AND exit_date >= ?",
            (since_date.isoformat(),),
        ).fetchone()
        return float(row["total"] or 0)
    finally:
        con.close()


def get_circuit_breaker_status() -> Dict[str, Any]:
    cfg = get_circuit_breaker_config()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    daily_pnl = _realized_pnl_since(today)
    weekly_pnl = _realized_pnl_since(week_start)
    daily_breached = cfg["max_daily_loss"] > 0 and daily_pnl <= -abs(cfg["max_daily_loss"])
    weekly_breached = cfg["max_weekly_loss"] > 0 and weekly_pnl <= -abs(cfg["max_weekly_loss"])
    return {
        "daily_pnl": daily_pnl,
        "weekly_pnl": weekly_pnl,
        "max_daily_loss": cfg["max_daily_loss"],
        "max_weekly_loss": cfg["max_weekly_loss"],
        "daily_breached": daily_breached,
        "weekly_breached": weekly_breached,
        "halt_new_trades": daily_breached or weekly_breached,
    }


# ── data assembly (deterministic — no LLM involved) ───────────────────────

def _gather_closed_trades(days: int = 90) -> List[Dict[str, Any]]:
    from ..db import _connect
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    con = _connect()
    try:
        rows = con.execute(
            """SELECT symbol, trade_type, trade_subtype, entry_date, exit_date, pnl,
                      risk_amt, reward_amt, sector, entry_reason, exit_reason, close_reason,
                      quantity, entry_price, exit_price
               FROM trades
               WHERE status='CLOSED' AND exit_date >= ?
               ORDER BY exit_date DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _gather_open_positions() -> List[Dict[str, Any]]:
    from ..db import _connect
    con = _connect()
    try:
        rows = con.execute(
            """SELECT symbol, trade_type, trade_subtype, entry_date, risk_amt, reward_amt,
                      sector, quantity, entry_price
               FROM trades WHERE status='OPEN' ORDER BY entry_date DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _aggregate_trade_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    def bucket_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(rows)
        if n == 0:
            return {"count": 0}
        wins = [r for r in rows if (r.get("pnl") or 0) > 0]
        total_pnl = sum((r.get("pnl") or 0) for r in rows)
        avg_pnl = total_pnl / n
        avg_risk = sum((r.get("risk_amt") or 0) for r in rows) / n
        return {
            "count": n,
            "win_rate_pct": round(100 * len(wins) / n, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
            "avg_risk_amt": round(avg_risk, 2),
            "expectancy_per_trade": round(avg_pnl, 2),
        }

    by_type: Dict[str, List[Dict]] = {}
    by_sector: Dict[str, List[Dict]] = {}
    by_close_reason: Dict[str, List[Dict]] = {}
    for t in trades:
        by_type.setdefault(t.get("trade_type") or "?", []).append(t)
        by_sector.setdefault(t.get("sector") or "Unknown", []).append(t)
        by_close_reason.setdefault(t.get("close_reason") or t.get("exit_reason") or "Unspecified", []).append(t)

    return {
        "overall": bucket_stats(trades),
        "by_trade_type": {k: bucket_stats(v) for k, v in by_type.items()},
        "by_sector": {k: bucket_stats(v) for k, v in by_sector.items()},
        "by_close_reason": {k: bucket_stats(v) for k, v in by_close_reason.items()},
    }


def _gather_recent_alerts(limit: int = 300, since_days: int = 90) -> List[Dict[str, Any]]:
    con = _conn()
    try:
        cutoff = (date.today() - timedelta(days=since_days)).isoformat()
        rows = con.execute(
            """SELECT symbol, bucket, trade_type, grade, score, source_label, source_kind,
                      telegram_ok, sent_at, alert_date
               FROM signal_notifier_alerts
               WHERE alert_date >= ?
               ORDER BY id DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _aggregate_alert_stats(alerts: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_source: Dict[str, Dict[str, Any]] = {}
    for a in alerts:
        key = a.get("source_label") or a.get("source_kind") or "Trade Opportunity Scanner (default)"
        b = by_source.setdefault(key, {"count": 0, "delivered": 0, "symbols": set()})
        b["count"] += 1
        if a.get("telegram_ok"):
            b["delivered"] += 1
        b["symbols"].add(a.get("symbol"))
    return {
        k: {"count": v["count"], "delivered": v["delivered"], "unique_symbols": len(v["symbols"])}
        for k, v in by_source.items()
    }


def _match_alerts_to_trade_outcomes(alerts: List[Dict[str, Any]], trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Best-effort join: did an alerted symbol later show up as a trade, and
    how did that trade do? This is the concrete feedback loop connecting
    Signal Notifier output to real P&L, which is what makes autotune
    recommendations evidence-based rather than a guess."""
    trades_by_symbol: Dict[str, List[Dict]] = {}
    for t in trades:
        trades_by_symbol.setdefault(t.get("symbol"), []).append(t)

    matched, unmatched = 0, 0
    matched_pnl = 0.0
    for a in alerts:
        sym = a.get("symbol")
        cand = trades_by_symbol.get(sym) or []
        # crude date-window match: any trade entered within 5 days of the alert
        hit = None
        for t in cand:
            try:
                a_date = datetime.fromisoformat(a.get("alert_date"))
                e_date = datetime.fromisoformat(t.get("entry_date")[:10])
                if abs((e_date - a_date).days) <= 5:
                    hit = t
                    break
            except Exception:
                continue
        if hit:
            matched += 1
            matched_pnl += float(hit.get("pnl") or 0)
        else:
            unmatched += 1
    return {
        "alerts_that_led_to_a_trade": matched,
        "alerts_with_no_trade_taken": unmatched,
        "pnl_from_alerted_trades": round(matched_pnl, 2),
    }


# ── Agent 1: Trade Journal Post-Mortem ────────────────────────────────────

POSTMORTEM_SYSTEM = """You are a disciplined trading performance analyst embedded in a retail \
options/futures trading tool. You will be given structured statistics (already computed — \
never invent numbers not present in the data) covering: closed trades grouped by setup type, \
sector, and close reason; and Signal Notifier alert-source statistics, including how many \
alerts actually led to a trade.

Your job:
1. Identify which setups/sources are genuinely working (by win rate AND expectancy, not just \
   win rate alone — a high win rate with poor R:R can still be a loser).
2. Call out specific underperforming patterns worth cutting or changing.
3. Give 3-6 concrete, specific recommendations, each tied to a specific number from the data \
   (e.g. "the IC setup has 40% win rate and -$120 average P&L over 14 trades — consider raising \
   the min_score filter or dropping this setup"). Avoid generic advice.
4. If a Signal Notifier source is producing many alerts but few resulting trades or poor \
   resulting P&L, say so explicitly and suggest a specific threshold change (higher min_score, \
   longer interval, or disabling it).
5. Close with a short "if I only changed three things" priority list.

Be direct and specific. If the sample size for a bucket is small (under ~8 trades), say so and \
caveat the confidence of that specific finding — don't present a small sample as a strong signal."""


def run_postmortem(days: int = 90) -> Dict[str, Any]:
    _ensure_tables()
    trades = _gather_closed_trades(days)
    alerts = _gather_recent_alerts(since_days=days)
    stats = _aggregate_trade_stats(trades)
    alert_stats = _aggregate_alert_stats(alerts)
    feedback_loop = _match_alerts_to_trade_outcomes(alerts, trades)

    if not trades:
        return {"ok": False, "error": f"No closed trades in the last {days} days to analyze yet."}

    payload = {
        "period_days": days,
        "trade_stats": stats,
        "signal_notifier_source_stats": alert_stats,
        "alert_to_trade_feedback_loop": feedback_loop,
    }
    user_msg = "Here is the trading performance data to analyze:\n\n" + json.dumps(payload, indent=2, default=str)

    if not llm_configured():
        return {"ok": False, "error": "No LLM API key configured yet. Add one under AI Copilot → Settings.",
                "stats": payload}

    result = call_llm(POSTMORTEM_SYSTEM, user_msg, max_tokens=2200)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "stats": payload}

    con = _conn()
    try:
        con.execute(
            """INSERT INTO ai_postmortem_reports
               (created_at, period_days, trades_analyzed, alerts_analyzed, stats_json, report_text, model)
               VALUES (?,?,?,?,?,?,?)""",
            (datetime.now().isoformat(), days, len(trades), len(alerts),
             json.dumps(payload, default=str), result["text"], result.get("model", "")),
        )
        con.commit()
    finally:
        con.close()

    return {"ok": True, "report_text": result["text"], "stats": payload,
            "trades_analyzed": len(trades), "alerts_analyzed": len(alerts)}


def list_postmortem_reports(limit: int = 20) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT * FROM ai_postmortem_reports ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ── Agent 2: Pre-Trade Risk Checkpoint ────────────────────────────────────

RISK_CHECK_SYSTEM = """You are a risk manager reviewing ONE candidate trade before entry, for a \
retail options/futures trader. You are given: the candidate trade, a snapshot of the trader's \
current open positions (for concentration/correlation), a market regime snapshot, an earnings- \
date conflict check, upcoming macro events, and the trader's daily/weekly realized-loss circuit \
breaker status. All figures are already computed — do not invent or estimate any number that \
is not given to you.

Respond with ONLY a JSON object of this exact shape:
{
  "verdict": "APPROVE" | "CAUTION" | "REJECT",
  "reasoning": "2-4 sentences, specific to the data given",
  "risk_flags": ["short phrase", "short phrase", ...],
  "sizing_note": "one sentence on position sizing given current concentration/circuit-breaker status"
}

Guidance for the verdict:
- REJECT if the circuit breaker is already breached (halt_new_trades true), or the candidate \
  directly conflicts with an imminent earnings date with no earnings guard, or concentration in \
  the same symbol/sector is already very high.
- CAUTION if regime is misaligned with the trade direction, or a macro event lands inside the \
  likely holding period, or concentration is moderate.
- APPROVE only if none of the above apply."""


def _sector_concentration(open_positions: List[Dict[str, Any]], sector: Optional[str], symbol: str) -> Dict[str, Any]:
    same_symbol = [p for p in open_positions if (p.get("symbol") or "").upper() == symbol.upper()]
    same_sector = [p for p in open_positions if sector and (p.get("sector") or "").lower() == sector.lower()]
    total_open_risk = sum((p.get("risk_amt") or 0) for p in open_positions)
    return {
        "open_positions_total": len(open_positions),
        "open_positions_same_symbol": len(same_symbol),
        "open_positions_same_sector": len(same_sector),
        "total_open_risk_amt": round(total_open_risk, 2),
    }


def run_pretrade_check(symbol: str, trade_type: str = "", direction: str = "",
                        sector: Optional[str] = None, risk_amt: Optional[float] = None,
                        notes: str = "") -> Dict[str, Any]:
    _ensure_tables()
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"ok": False, "error": "symbol is required"}

    open_positions = _gather_open_positions()
    concentration = _sector_concentration(open_positions, sector, symbol)
    breaker = get_circuit_breaker_status()

    regime = None
    try:
        from ..scanners.regime_scanner import _compute_regime_ta
        regime = _compute_regime_ta(symbol)
    except Exception as e:
        regime = {"error": str(e)}

    earnings = None
    try:
        from ..scanners.earnings_calendar import get_earnings_info
        earnings = get_earnings_info(symbol)
    except Exception as e:
        earnings = {"error": str(e)}

    macro = None
    try:
        from ..services.macro_events import get_macro_message_board
        macro = get_macro_message_board(days_back=0, days_ahead=5)
    except Exception as e:
        macro = {"error": str(e)}

    context = {
        "candidate_trade": {
            "symbol": symbol, "trade_type": trade_type, "direction": direction,
            "sector": sector, "risk_amt": risk_amt, "notes": notes,
        },
        "concentration": concentration,
        "circuit_breaker": breaker,
        "regime_snapshot": regime,
        "earnings_check": earnings,
        "macro_events_next_5_days": macro,
    }

    if not llm_configured():
        return {"ok": False, "error": "No LLM API key configured yet. Add one under AI Copilot → Settings.",
                "context": context}

    user_msg = "Evaluate this candidate trade:\n\n" + json.dumps(context, indent=2, default=str)
    result = call_llm_json(RISK_CHECK_SYSTEM, user_msg, max_tokens=900)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "context": context}

    verdict_data = result.get("data") or {}
    verdict = verdict_data.get("verdict", "CAUTION")
    if breaker.get("halt_new_trades"):
        verdict = "REJECT"
        verdict_data["verdict"] = "REJECT"
        verdict_data.setdefault("risk_flags", []).append("Circuit breaker already breached — new trades halted")

    con = _conn()
    try:
        con.execute(
            """INSERT INTO ai_risk_checks
               (created_at, symbol, trade_type, direction, sector, risk_amt, verdict,
                reasoning, risk_flags_json, sizing_note, context_json, model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now().isoformat(), symbol, trade_type, direction, sector, risk_amt,
             verdict, verdict_data.get("reasoning", ""), json.dumps(verdict_data.get("risk_flags", [])),
             verdict_data.get("sizing_note", ""), json.dumps(context, default=str), result.get("model", "")),
        )
        con.commit()
    finally:
        con.close()

    return {"ok": True, "verdict": verdict, "reasoning": verdict_data.get("reasoning", ""),
            "risk_flags": verdict_data.get("risk_flags", []), "sizing_note": verdict_data.get("sizing_note", ""),
            "context": context}


def list_risk_checks(limit: int = 30) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM ai_risk_checks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ── Agent 3: Daily Briefing ────────────────────────────────────────────────

BRIEFING_SYSTEM = """You write a short, prioritized morning briefing for an active options/futures \
trader, using only the structured data you're given (regime snapshot for market proxies, the \
trader's open positions, the last 24h of Signal Notifier alerts, upcoming macro events, and \
circuit-breaker status). Do not invent any figures not present in the data.

Write in plain prose, organized as:
1. One-line market regime summary (SPY/QQQ/IWM).
2. What needs attention today (open positions near risk, earnings/macro conflicts, circuit \
   breaker status) — most important first.
3. Notable new Signal Notifier alerts worth a look, if any.
4. Anything to explicitly NOT worry about today (to cut noise), if relevant.

Keep it under 250 words. No headers/markdown — just a short, direct memo, like a sharp analyst \
texting you before market open."""


def run_daily_briefing(send_telegram: bool = False) -> Dict[str, Any]:
    _ensure_tables()
    open_positions = _gather_open_positions()
    breaker = get_circuit_breaker_status()
    recent_alerts = _gather_recent_alerts(limit=50, since_days=1)

    regimes = {}
    try:
        from ..scanners.regime_scanner import _compute_regime_ta
        for sym in ("SPY", "QQQ", "IWM"):
            try:
                regimes[sym] = _compute_regime_ta(sym)
            except Exception as e:
                regimes[sym] = {"error": str(e)}
    except Exception as e:
        regimes = {"error": str(e)}

    macro = None
    try:
        from ..services.macro_events import get_macro_message_board
        macro = get_macro_message_board(days_back=0, days_ahead=3)
    except Exception as e:
        macro = {"error": str(e)}

    context = {
        "regime_snapshot": regimes,
        "open_positions": open_positions,
        "circuit_breaker": breaker,
        "recent_signal_notifier_alerts_24h": recent_alerts,
        "macro_events_next_3_days": macro,
    }

    if not llm_configured():
        return {"ok": False, "error": "No LLM API key configured yet. Add one under AI Copilot → Settings.",
                "context": context}

    user_msg = "Write today's briefing from this data:\n\n" + json.dumps(context, indent=2, default=str)
    result = call_llm(BRIEFING_SYSTEM, user_msg, max_tokens=700)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "context": context}

    sent_ok = False
    if send_telegram:
        try:
            from ..services.telegram_alerts import send_telegram_message, telegram_configured
            if telegram_configured():
                resp = send_telegram_message(f"☀️ Daily Briefing\n\n{result['text']}")
                sent_ok = bool(resp.get("ok"))
        except Exception:
            sent_ok = False

    con = _conn()
    try:
        con.execute(
            """INSERT INTO ai_daily_briefings (created_at, briefing_text, context_json, sent_telegram, model)
               VALUES (?,?,?,?,?)""",
            (datetime.now().isoformat(), result["text"], json.dumps(context, default=str),
             1 if sent_ok else 0, result.get("model", "")),
        )
        con.commit()
    finally:
        con.close()

    return {"ok": True, "briefing_text": result["text"], "sent_telegram": sent_ok, "context": context}


def list_briefings(limit: int = 20) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM ai_daily_briefings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ── Flask routes ─────────────────────────────────────────────────────────

@ai_copilot_bp.route("", strict_slashes=False)
def page():
    _ensure_tables()
    return render_template("ai_copilot.html")


@ai_copilot_bp.route("/settings", methods=["GET"])
def api_get_settings():
    return jsonify(get_llm_settings())


@ai_copilot_bp.route("/settings", methods=["POST"])
def api_set_settings():
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(set_llm_settings(api_key=body.get("api_key"), model=body.get("model")))


@ai_copilot_bp.route("/circuit-breaker", methods=["GET"])
def api_get_circuit_breaker():
    return jsonify(get_circuit_breaker_status())


@ai_copilot_bp.route("/circuit-breaker", methods=["POST"])
def api_set_circuit_breaker():
    body = request.get_json(force=True, silent=True) or {}
    cfg = set_circuit_breaker_config(max_daily_loss=body.get("max_daily_loss"), max_weekly_loss=body.get("max_weekly_loss"))
    return jsonify({"ok": True, **cfg})


@ai_copilot_bp.route("/postmortem/run", methods=["POST"])
def api_run_postmortem():
    body = request.get_json(force=True, silent=True) or {}
    days = int(body.get("days") or 90)
    try:
        return jsonify(run_postmortem(days=days))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@ai_copilot_bp.route("/postmortem/history", methods=["GET"])
def api_postmortem_history():
    return jsonify({"ok": True, "reports": list_postmortem_reports()})


@ai_copilot_bp.route("/risk-check/run", methods=["POST"])
def api_run_risk_check():
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = run_pretrade_check(
            symbol=body.get("symbol", ""),
            trade_type=body.get("trade_type", ""),
            direction=body.get("direction", ""),
            sector=body.get("sector"),
            risk_amt=body.get("risk_amt"),
            notes=body.get("notes", ""),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@ai_copilot_bp.route("/risk-check/history", methods=["GET"])
def api_risk_check_history():
    return jsonify({"ok": True, "checks": list_risk_checks()})


@ai_copilot_bp.route("/briefing/run", methods=["POST"])
def api_run_briefing():
    body = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(run_daily_briefing(send_telegram=bool(body.get("send_telegram", False))))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@ai_copilot_bp.route("/briefing/history", methods=["GET"])
def api_briefing_history():
    return jsonify({"ok": True, "briefings": list_briefings()})
