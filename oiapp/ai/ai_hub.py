# oiapp/ai/ai_hub.py
"""
Conversational AI Hub
---------------------
A deterministic, data-grounded natural-language router for the trading app.
It does not call an external LLM and does not invent market data.  Each answer
is produced by existing scanners, journal health rules, option-chain helpers,
agentic market/sector context, and unified scoring/OI weighting already in the
application.
"""
from __future__ import annotations

import calendar
import difflib
import json
import math
import os
import re
import sqlite3
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from flask import Blueprint, current_app, jsonify, render_template, request

ai_hub_bp = Blueprint("ai_hub", __name__, url_prefix="/ai-hub")

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")

COMMON_NON_SYMBOLS = {
    "A", "ACTION", "ACTIONS", "AI", "ALL", "ANALYZE", "AND", "OR", "WHERE", "QUERY", "API", "ATM", "ATR", "BEST",
    "CALL", "CPI", "CS", "DTE", "ETF", "EXPIRING", "EXPIRY", "FOR", "GEX", "HEALTH",
    "HUB", "I", "IC", "IDEA", "IDEAS", "IV", "MACD", "ME", "MY", "NEW", "OI", "OKAY", "OPEN",
    "ST", "MT", "LT", "DAY", "DAYS", "SHORT", "MEDIUM", "LONG", "TERM", "TERMS",
    "BUILD", "BUILDUP", "BUILDS", "BUILT", "TREND", "TRENDS", "ALIGNED", "ALIGN",
    "ALIGNMENT", "MISALIGNED", "DIVERGENCE", "DIVERGENT", "NOT",
    "PB", "PCR", "PNR", "PS", "PUT", "QQQ", "ROLL", "RUN", "RS", "RSI", "SHOULD", "SHOW", "SCAN",
    "SPY", "SUGGEST", "THE", "TODAY", "TOP", "TRADE", "TRADES", "UAE", "UNDER", "VIX",
    "WHICH", "WITH", "XLK", "XLF", "XLE", "XLY", "XLV",
}

TRADE_TYPE_LABELS = {
    "PS": "Bull put credit spread",
    "CS": "Bear call credit spread",
    "IC": "Iron condor",
    "CALL": "Long call / call debit",
    "PUT": "Long put / put debit",
}

# AI Hub uses the dedicated Weekly Plan for core ETF / near-weekly expiry
# strategy requests.  This avoids the slow generic all-strategy exact-chain loop
# and aligns SPY/QQQ/IWM-style planning with the dashboard Weekly Plan.
CORE_WEEKLY_PLAN_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA", "SPX", "XSP"}
LIQUID_WEEKLY_PLAN_SYMBOLS = {
    "AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOGL", "GOOG",
    "TSLA", "NFLX", "AVGO", "COST", "ADBE", "CRM", "NOW", "SMH",
    "XLK", "XLF", "XLE", "XLY", "XLV", "GLD", "TLT", "HYG", "LQD",
}
WEEKLY_PLAN_MAX_DTE = int(os.environ.get("AI_HUB_WEEKLY_PLAN_MAX_DTE", "10") or 10)
WEEKLY_PLAN_TIMEOUT_SEC = float(os.environ.get("AI_HUB_WEEKLY_PLAN_TIMEOUT_SEC", "18") or 18)

MONTH_LOOKUP = {}
for i in range(1, 13):
    MONTH_LOOKUP[calendar.month_name[i].lower()] = i
    MONTH_LOOKUP[calendar.month_abbr[i].lower()] = i


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return con


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_float(v: Any, default: Optional[float] = None, ndigits: Optional[int] = None) -> Optional[float]:
    try:
        f = float(v)
        if not math.isfinite(f):
            return default
        return round(f, ndigits) if ndigits is not None else f
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, float) and not math.isfinite(v):
            return default
        return int(float(v))
    except Exception:
        return default




def _dte_bucket(dte: Optional[int]) -> str:
    try:
        d = int(dte or 0)
    except Exception:
        d = 0
    if d <= 10:
        return "weekly / 0-10 DTE"
    if d <= 21:
        return "tactical / 7-21 DTE"
    if d <= 60:
        return "swing / 30-60 DTE"
    return "longer-dated / 60+ DTE"


def _cached_earnings_context(symbol: str, expiry: Optional[str] = None, dte: Optional[int] = None) -> Dict[str, Any]:
    """Return cached earnings guardrail for strategy answers without live fetch.

    If an option trade would hold through an upcoming earnings date, AI Hub must
    say so and avoid that timeframe instead of treating the setup as clean.
    ETFs/index proxies are marked as no company earnings conflict.
    """
    sym = str(symbol or "").upper().strip()
    try:
        from ..scanners.oi_buildup_scanner import NON_EQUITY
    except Exception:
        NON_EQUITY = {"SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLU", "XLI", "XLC", "XLRE", "XLB", "SMH", "TLT", "GLD", "SLV"}
    if not sym:
        return {"earnings_conflict": False, "earnings_note": "No symbol supplied."}
    if sym in NON_EQUITY:
        return {"symbol": sym, "earnings_date": None, "earnings_days": None, "earnings_conflict": False, "earnings_note": "ETF/index proxy: no company earnings conflict."}

    row = None
    con = _conn()
    try:
        try:
            r = con.execute(
                "SELECT * FROM earnings_calendar WHERE symbol=? LIMIT 1",
                (sym,),
            ).fetchone()
            row = dict(r) if r else None
        except Exception:
            row = None
    finally:
        con.close()

    ed = str((row or {}).get("next_earn_date") or "")[:10] or None
    days = None
    if ed:
        try:
            days = (date.fromisoformat(ed) - date.today()).days
        except Exception:
            days = None
    exp_dt = None
    if expiry:
        try:
            exp_dt = date.fromisoformat(str(expiry)[:10])
        except Exception:
            exp_dt = None
    if exp_dt is None and dte is not None:
        try:
            exp_dt = date.today() + timedelta(days=int(dte))
        except Exception:
            exp_dt = None

    conflict = False
    if ed and days is not None and days >= 0:
        try:
            earn_dt = date.fromisoformat(ed)
            if exp_dt is not None:
                # Holding an option through earnings is a conflict unless the
                # expiry is clearly before the report.
                conflict = date.today() <= earn_dt <= exp_dt + timedelta(days=1)
            elif dte is not None:
                conflict = days <= int(dte) + 1
        except Exception:
            conflict = False

    if not ed:
        note = "No cached upcoming earnings date. Refresh the earnings calendar before relying on this timeframe."
    elif conflict:
        note = f"Earnings {ed} falls inside this holding window; avoid opening/holding this option strategy through the report."
    elif days is not None and days >= 0:
        note = f"Next earnings {ed} is {days} calendar days away and does not conflict with this expiry."
    else:
        note = f"Cached earnings date {ed} is not upcoming."
    return {
        "symbol": sym,
        "earnings_date": ed,
        "earnings_days": days,
        "earnings_confirmed": int((row or {}).get("next_earn_confirmed") or 0),
        "earnings_score": (row or {}).get("earn_score"),
        "earnings_conflict": bool(conflict),
        "earnings_note": note,
    }

def _sanitize(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize(v) for v in obj]
    try:
        return _sanitize(obj.item())
    except Exception:
        return str(obj)


def _json_dumps(obj: Any) -> str:
    try:
        return json.dumps(_sanitize(obj), ensure_ascii=False, allow_nan=False, default=str)
    except Exception:
        return json.dumps({"serialization_error": True}, allow_nan=False)


def _json_loads(txt: Any, default: Any = None) -> Any:
    if txt in (None, ""):
        return default
    try:
        return json.loads(txt)
    except Exception:
        return default


def _ensure_tables() -> None:
    con = _conn()
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_hub_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                question TEXT NOT NULL,
                intent TEXT,
                params_json TEXT,
                answer_text TEXT,
                response_json TEXT,
                ok INTEGER DEFAULT 1,
                error_text TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ai_hub_queries_created
                ON ai_hub_queries(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_hub_queries_intent
                ON ai_hub_queries(intent);
            """
        )
        con.commit()
    finally:
        con.close()


def _save_query(question: str, result: Dict[str, Any]) -> int:
    _ensure_tables()
    con = _conn()
    try:
        cur = con.execute(
            """
            INSERT INTO ai_hub_queries
                (created_at, question, intent, params_json, answer_text, response_json, ok, error_text)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                _now(),
                question or "",
                result.get("intent") or "unknown",
                _json_dumps(result.get("params") or {}),
                result.get("answer") or "",
                _json_dumps(result),
                1 if result.get("ok", True) else 0,
                result.get("error") or "",
            ),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _history(limit: int = 30) -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT id, created_at, question, intent, answer_text, ok, error_text FROM ai_hub_queries ORDER BY created_at DESC LIMIT ?",
            (max(1, min(200, int(limit or 30))),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _known_symbols() -> List[str]:
    con = _conn()
    try:
        vals = set()
        for table, col in (("symbols", "symbol"), ("watchlist_symbols", "symbol"), ("trades", "symbol"), ("options", "symbol")):
            try:
                for r in con.execute(f"SELECT DISTINCT {col} AS symbol FROM {table} WHERE {col} IS NOT NULL").fetchall():
                    s = str(r["symbol"] or "").strip().upper()
                    if s:
                        vals.add(s)
            except Exception:
                pass
        vals.update(["SPY", "QQQ", "IWM", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NOW"])
        return sorted(vals)
    finally:
        con.close()


def _watchlists() -> List[Dict[str, Any]]:
    con = _conn()
    try:
        rows = con.execute(
            """
            SELECT w.id, w.name, COALESCE(w.is_default,0) AS is_default, COUNT(ws.id) AS symbol_count
            FROM watchlists w LEFT JOIN watchlist_symbols ws ON ws.watchlist_id=w.id
            GROUP BY w.id
            ORDER BY COALESCE(w.is_default,0) DESC, w.name
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def _resolve_symbols(watchlist_id: Optional[Any] = None, max_symbols: int = 120) -> Tuple[List[str], Dict[str, Any]]:
    con = _conn()
    meta = {"watchlist_id": watchlist_id or "", "watchlist_name": "All symbols"}
    try:
        rows = []
        if watchlist_id not in (None, "", 0, "0"):
            try:
                row = con.execute("SELECT name FROM watchlists WHERE id=?", (int(watchlist_id),)).fetchone()
                if row:
                    meta["watchlist_name"] = row["name"]
                rows = con.execute(
                    "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                    (int(watchlist_id),),
                ).fetchall()
            except Exception:
                rows = []
        if not rows:
            try:
                default = con.execute("SELECT id,name FROM watchlists WHERE COALESCE(is_default,0)=1 ORDER BY id LIMIT 1").fetchone()
                if default:
                    meta["watchlist_id"] = default["id"]
                    meta["watchlist_name"] = default["name"]
                    rows = con.execute(
                        "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                        (int(default["id"]),),
                    ).fetchall()
            except Exception:
                rows = []
        if not rows:
            try:
                rows = con.execute("SELECT DISTINCT symbol FROM symbols ORDER BY symbol").fetchall()
            except Exception:
                rows = []
        syms = sorted({str(r["symbol"] or "").strip().upper() for r in rows if str(r["symbol"] or "").strip()})
        meta["total_universe"] = len(syms)
        return syms[: max(1, min(500, int(max_symbols or 120)))], meta
    finally:
        con.close()


def _parse_symbol(text: str) -> Optional[str]:
    raw = text or ""
    known = set(_known_symbols())

    # Prefer explicit uppercase tickers from the user's original text.  This keeps
    # symbol NOW distinct from the ordinary lowercase word "now".
    tokens = re.findall(r"\b[A-Z][A-Z0-9.]{0,5}\b", raw)
    for tok in tokens:
        t = tok.upper().replace(".", "-")
        if t in known:
            return t
    for tok in tokens:
        t = tok.upper().replace(".", "-")
        if t not in COMMON_NON_SYMBOLS and 1 <= len(t) <= 6:
            return t

    # Secondary pattern: "for nvda", "trade nvda", "analyze nvda".
    m = re.search(r"\b(?:for|trade|symbol|ticker|on|open)\s+([a-z]{1,6})\b", raw, re.I)
    if m:
        cand = m.group(1).upper()
        if cand not in COMMON_NON_SYMBOLS:
            return cand
    return None


def _parse_trade_type(text: str) -> Optional[str]:
    s = (text or "").lower()
    if re.search(r"\bbull\s+put\b|\bput\s+credit\b|\bcredit\s+put\b|\bshort\s+put\s+spread\b", s):
        return "PS"
    if re.search(r"\bbear\s+call\b|\bcall\s+credit\b|\bcredit\s+call\b|\bshort\s+call\s+spread\b", s):
        return "CS"
    if re.search(r"\biron\s+condor\b|\bic\b", s):
        return "IC"
    if re.search(r"\blong\s+call\b|\bbuy\s+call\b|\bcall\s+debit\b", s):
        return "CALL"
    if re.search(r"\blong\s+put\b|\bbuy\s+put\b|\bput\s+debit\b", s):
        return "PUT"
    m = re.search(r"\b(PS|CS|IC|CALL|PUT)\b", text or "")
    if m:
        return m.group(1).upper()
    return None


BEST_STRATEGY_TYPES = ["PS", "CS", "IC", "CALL", "PUT"]

# Liquid underlyings where a near-term expiry should use the app's Weekly Plan
# first instead of running the heavier five-structure, full-chain search.
# The set is intentionally broad enough for the common weekly options universe,
# while still bounded so a random illiquid symbol does not take the fast path.
FAST_WEEKLY_PLAN_SYMBOLS = {
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "XLY", "XLI", "XLC", "XLU", "XLP", "XLRE",
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "AMD", "AVGO", "NFLX", "SMCI",
    "COST", "CRM", "NOW", "ADBE", "ORCL", "INTC", "MU", "BA", "JPM", "BAC", "GS", "V", "MA",
    "UNH", "LLY", "MSTR", "COIN", "PLTR", "UBER", "SHOP", "SNOW", "PANW", "CRWD",
}
AI_HUB_WEEKLY_PLAN_TIMEOUT_SECONDS = float(os.environ.get("AI_HUB_WEEKLY_PLAN_TIMEOUT_SECONDS", "12"))
AI_HUB_STRATEGY_TIMEOUT_SECONDS = float(os.environ.get("AI_HUB_STRATEGY_TIMEOUT_SECONDS", "25"))
_AGENTIC_CONTEXT_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _is_best_strategy_request(text: str) -> bool:
    """True when the user asks the hub to choose the strategy, not evaluate one fixed setup."""
    lq = (text or "").lower()
    has_best_word = any(x in lq for x in [
        "best", "optimal", "preferred", "recommend", "suggest", "which strategy", "what strategy",
        "which trade", "what trade", "trade idea", "trade setup", "setup for", "open a new trade",
        "okay to open", "ok to open", "should i open",
    ])
    has_strategy_word = any(x in lq for x in [
        "strategy", "trade", "setup", "spread", "position", "entry", "open",
    ])
    return bool(has_best_word and has_strategy_word)


def _is_manual_trade_review_request(text: str, trade_type: Optional[str], strikes: Sequence[float]) -> bool:
    """True only when the question identifies a concrete structure or strikes."""
    lq = (text or "").lower()
    if trade_type:
        return True
    if strikes:
        return True
    # Avoid treating a plain ticker+expiry question as a PS by default.  These
    # should route to best_strategy if the user asks for a recommendation, or to
    # help if the question is incomplete.
    return False


def _parse_price_cap(text: str) -> Optional[float]:
    patterns = [
        r"\bunder\s*\$?\s*(\d+(?:\.\d+)?)\b",
        r"\bbelow\s*\$?\s*(\d+(?:\.\d+)?)\b",
        r"\bprice\s*(?:<|<=|under|below)\s*\$?\s*(\d+(?:\.\d+)?)\b",
        r"\$\s*(\d+(?:\.\d+)?)\s*(?:or\s+less|max|maximum)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text or "", re.I)
        if m:
            return _safe_float(m.group(1), None)
    return None


def _parse_top_n(text: str, default: int = 10) -> int:
    m = re.search(r"\btop\s+(\d{1,3})\b", text or "", re.I)
    if m:
        return max(1, min(50, _safe_int(m.group(1), default)))
    return default


def _parse_oi_horizons(text: str) -> Dict[str, int]:
    """Parse ST / MT / LT day horizons from natural language.

    Supports examples like:
    - ST as 3 days MT as 10 days LT as 30 days
    - short term 3, medium term 10, long term 30
    - 3/10/30 day OI buildup
    """
    raw = text or ""
    lq = raw.lower()
    defaults = {"st_days": 3, "mt_days": 10, "lt_days": 30}

    combo = re.search(
        r"\b(\d{1,3})\s*(?:/|-|,)\s*(\d{1,3})\s*(?:/|-|,)\s*(\d{1,3})\s*(?:d|day|days)?\b",
        lq,
    )
    if combo and ("oi" in lq or "open interest" in lq or "st" in lq or "short" in lq):
        defaults["st_days"] = _safe_int(combo.group(1), defaults["st_days"])
        defaults["mt_days"] = _safe_int(combo.group(2), defaults["mt_days"])
        defaults["lt_days"] = _safe_int(combo.group(3), defaults["lt_days"])

    labels = {
        "st_days": r"(?:st|short\s*term|short-term|short|near\s*term|near-term)",
        "mt_days": r"(?:mt|medium\s*term|medium-term|medium|mid\s*term|mid-term|intermediate)",
        "lt_days": r"(?:lt|long\s*term|long-term|long)",
    }
    for key, label in labels.items():
        patterns = [
            rf"\b{label}\b\s*(?:as|=|:|is|to|for|of|at)?\s*(\d{{1,3}})\s*(?:d|day|days)?\b",
            rf"\b(\d{{1,3}})\s*(?:d|day|days)\s*(?:{label})\b",
        ]
        for pat in patterns:
            m = re.search(pat, lq, re.I)
            if m:
                defaults[key] = _safe_int(m.group(1), defaults[key])
                break

    st = max(1, min(10, defaults["st_days"]))
    mt = max(2, min(60, defaults["mt_days"]))
    lt = max(5, min(90, defaults["lt_days"]))
    if mt < st:
        mt = st
    if lt < mt:
        lt = mt
    return {"st_days": st, "mt_days": mt, "lt_days": lt}


def _is_oi_buildup_request(text: str) -> bool:
    lq = (text or "").lower()
    has_oi = "oi" in lq or "open interest" in lq
    has_build = any(x in lq for x in ["buildup", "build up", "build-up", "oi trend", "trend"])
    has_scan = any(x in lq for x in ["run", "scan", "scanner", "show", "find", "suggest", "which", "list"])
    has_horizon = any(x in lq for x in ["st", "mt", "lt", "short term", "medium term", "long term", "3/", "/10", "/30"])
    return bool(has_oi and has_build and (has_scan or has_horizon))


def _is_oi_divergence_request(text: str) -> bool:
    lq = (text or "").lower()
    return any(
        x in lq
        for x in [
            "not aligned", "not align", "misaligned", "divergence", "divergent",
            "does not align", "isn't aligned", "not matching", "not matched", "different from",
            "lt vs", "30 days oi trend is not", "30 day oi trend is not",
        ]
    )


def _scanner_catalog() -> List[Dict[str, Any]]:
    """Saved and built-in Scanner Builder definitions available to AI Hub."""
    items: List[Dict[str, Any]] = []
    try:
        from ..scanners.scanner_builder import BUILTIN_SCANNERS
        for item in BUILTIN_SCANNERS:
            q = (item.get("query_text") or "").strip()
            name = (item.get("name") or "").strip()
            if name and q:
                items.append({
                    "name": name,
                    "description": item.get("description") or "",
                    "category": item.get("category") or "Built-in",
                    "query_text": q,
                    "source": "builtin",
                })
    except Exception:
        pass

    con = _conn()
    try:
        rows = con.execute(
            "SELECT name, description, query_text, COALESCE(benchmark,'SPY') AS benchmark FROM scanner_definitions ORDER BY name"
        ).fetchall()
        for r in rows:
            name = (r["name"] or "").strip()
            q = (r["query_text"] or "").strip()
            if name and q:
                items.append({
                    "name": name,
                    "description": r["description"] or "",
                    "category": "Saved",
                    "query_text": q,
                    "benchmark": r["benchmark"] or "SPY",
                    "source": "saved",
                })
    except Exception:
        pass
    finally:
        con.close()

    seen = set()
    out = []
    for item in items:
        key = item["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _scanner_match_score(question: str, name: str) -> float:
    q = (question or "").lower()
    n = (name or "").lower()
    if not n:
        return 0.0
    if n in q:
        return 1.0
    stop = {
        "run", "scan", "scanner", "show", "find", "top", "stocks", "stock", "symbols", "symbol",
        "me", "the", "a", "an", "for", "with", "under", "over", "above", "below", "please",
    }
    q_tokens = {t for t in re.findall(r"[a-z0-9]+", q) if t not in stop and len(t) > 1}
    n_tokens = {t for t in re.findall(r"[a-z0-9]+", n) if t not in stop and len(t) > 1}
    if not n_tokens:
        return 0.0
    overlap = len(q_tokens & n_tokens) / max(1, len(n_tokens))
    ratio = difflib.SequenceMatcher(None, n, q).ratio()
    return max(overlap, ratio)


def _match_saved_scanner(text: str) -> Optional[Dict[str, Any]]:
    lq = (text or "").lower()
    if not any(w in lq for w in ["run", "scan", "scanner", "show", "find", "which", "list", "top", "query", "where"]):
        return None
    best: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for item in _scanner_catalog():
        score = _scanner_match_score(lq, item.get("name") or "")
        if score > best_score:
            best = item
            best_score = score
    if best and (best_score >= 0.74 or (best.get("name") or "").lower() in lq):
        return {**best, "match_score": round(best_score, 3)}
    return None


def _looks_like_builder_expression(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    has_operator = bool(re.search(r"(>=|<=|>|<|=|\band\b|\bor\b|\bnot\b|crosses\s+above|crosses\s+below)", low))
    has_builder_token = bool(re.search(
        r"\b(close|open|high|low|volume|rsi14|rsidiff90|macd|ema\d+|uae|changePct|oichange|oichangepct|pcrchange|pcrchangepct|pcr|slope|support|resistance|scan\s*\(|lookback\s*\(|between\s*\()\b",
        raw,
        re.I,
    )) or "[" in raw or "]" in raw
    return bool(has_operator and has_builder_token)


def _extract_builder_expression(text: str) -> Optional[str]:
    raw = (text or "").strip()
    markers = [
        "scanner query:", "query:", "where ", "that match ", "matching ", "with condition ", "with conditions ",
    ]
    low = raw.lower()
    for marker in markers:
        idx = low.find(marker)
        if idx >= 0:
            expr = raw[idx + len(marker):].strip()
            expr = re.sub(r"^(stocks|symbols|that|are)\s+", "", expr, flags=re.I).strip()
            if _looks_like_builder_expression(expr):
                return expr
    if _looks_like_builder_expression(raw):
        return raw
    return None


def _nl_to_scanner_builder_query(text: str) -> Tuple[Optional[str], List[str]]:
    """Small deterministic translator from common phrases into Scanner Builder clauses."""
    raw = text or ""
    lq = raw.lower()
    clauses: List[str] = []

    explicit = _extract_builder_expression(raw)
    if explicit:
        return explicit, ["Used explicit Scanner Builder expression from the question."]

    cap = _parse_price_cap(raw)
    if cap is not None:
        clauses.append(f"close[1d] <= {cap:g}")

    m = re.search(r"\b(?:over|above|greater\s+than|price\s*>|price\s+above)\s*\$?\s*(\d+(?:\.\d+)?)\b", raw, re.I)
    if m and "ema" not in lq:
        clauses.append(f"close[1d] >= {(_safe_float(m.group(1), 0.0) or 0.0):g}")

    for ema in [5, 9, 20, 50, 200]:
        if re.search(rf"\b(?:above|over|reclaim(?:ing|ed)?|holding)\s+ema\s*{ema}\b|\bclose\s+above\s+ema\s*{ema}\b", lq):
            clauses.append(f"close[1d] > ema{ema}[1d]")
        if re.search(rf"\b(?:below|under|lost|losing)\s+ema\s*{ema}\b|\bclose\s+below\s+ema\s*{ema}\b", lq):
            clauses.append(f"close[1d] < ema{ema}[1d]")

    m = re.search(r"\brsi(?:14)?\s*(?:is\s*)?(?:under|below|<|<=)\s*(\d{1,3})\b", lq)
    if m:
        clauses.append(f"rsi14[1d] < {_safe_int(m.group(1), 50)}")
    m = re.search(r"\brsi(?:14)?\s*(?:is\s*)?(?:over|above|>|>=)\s*(\d{1,3})\b", lq)
    if m:
        clauses.append(f"rsi14[1d] > {_safe_int(m.group(1), 50)}")

    m = re.search(r"\brsi\s*diff\s*90\s*(?:is\s*)?(?:over|above|>|>=)\s*(-?\d{1,3})\b", lq)
    if not m:
        m = re.search(r"\brsidiff90\s*(?:is\s*)?(?:over|above|>|>=)\s*(-?\d{1,3})\b", lq)
    if m:
        clauses.append(f"RSIDiff90(\"1d\") > {_safe_int(m.group(1), 0)}")
    m = re.search(r"\brsi\s*diff\s*90\s*(?:is\s*)?(?:under|below|<|<=)\s*(-?\d{1,3})\b", lq)
    if not m:
        m = re.search(r"\brsidiff90\s*(?:is\s*)?(?:under|below|<|<=)\s*(-?\d{1,3})\b", lq)
    if m:
        clauses.append(f"RSIDiff90(\"1d\") < {_safe_int(m.group(1), 0)}")

    if "macd" in lq and ("hist" in lq or "histogram" in lq):
        if any(x in lq for x in ["positive", "above zero", "> 0", "bullish"]):
            clauses.append("macd_hist[1d] > 0")
        elif any(x in lq for x in ["negative", "below zero", "< 0", "bearish"]):
            clauses.append("macd_hist[1d] < 0")

    if any(x in lq for x in ["volume above average", "high volume", "volume expansion", "volume spike"]):
        clauses.append("volume[1d] > ema(volume,20)")
    if any(x in lq for x in ["volume dryup", "low volume", "volume contraction"]):
        clauses.append("volume[1d] < ema(volume,20)")

    if "uae" in lq or "regime" in lq:
        if "weak bull" in lq:
            clauses.append('UAEWeakBull("1d")')
        elif "bull" in lq or "bullish" in lq:
            clauses.append('(UAEBull("1d") OR UAEWeakBull("1d"))')
        if "weak bear" in lq:
            clauses.append('UAEWeakBear("1d")')
        elif "bear" in lq or "bearish" in lq:
            clauses.append('(UAEBear("1d") OR UAEWeakBear("1d"))')
        if "sideways" in lq or "chop" in lq or "range" in lq:
            clauses.append('UAESideways("1d")')

    if "momentum" in lq and not any("RSIDiff90" in c for c in clauses):
        if any(x in lq for x in ["bear", "bearish", "downside", "short"]):
            clauses.extend(['RSIDiff90("1d") <= -10', 'close[1d] < ema20[1d]'])
        else:
            clauses.extend(['RSIDiff90("1d") >= 10', 'close[1d] > ema20[1d]'])

    # Dedupe while preserving order.
    clauses = list(dict.fromkeys([c for c in clauses if c]))
    if not clauses:
        return None, []
    return " AND ".join(clauses), clauses


def _scanner_builder_request_params(text: str) -> Dict[str, Any]:
    lq = (text or "").lower()
    query_text, clauses = _nl_to_scanner_builder_query(text)
    if query_text and _looks_like_builder_expression(query_text):
        return {
            "scanner_name": "AI Hub generated scanner query",
            "scanner_source": "generated",
            "query_text": query_text,
            "benchmark": "SPY",
            "generated_clauses": clauses,
        }
    if not any(w in lq for w in ["run", "scan", "scanner", "show", "find", "which", "list", "top", "query", "where"]):
        return {}
    saved = _match_saved_scanner(text)
    if saved:
        return {
            "scanner_name": saved.get("name"),
            "scanner_source": saved.get("source"),
            "scanner_match_score": saved.get("match_score"),
            "query_text": saved.get("query_text"),
            "benchmark": saved.get("benchmark") or "SPY",
            "generated_clauses": [],
        }
    return {}


def _year_from_text(y: Optional[str]) -> int:
    today = date.today()
    if not y:
        return today.year
    yy = int(y)
    if yy < 100:
        return 2000 + yy if yy < 70 else 1900 + yy
    return yy


def _parse_expiry_date(text: str) -> Optional[str]:
    raw = text or ""
    today = date.today()

    # ISO: 2026-06-26
    m = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", raw)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except Exception:
            return None

    # US slash: 6/26/26 or 6/26/2026 or 6/26.
    # Iterate through every match so spread notation such as PS 95/90 does not
    # prevent a later real expiry like 6/26/26 from being detected.
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", raw):
        try:
            mo = int(m.group(1)); day = int(m.group(2)); year = _year_from_text(m.group(3))
            if not (1 <= mo <= 12 and 1 <= day <= 31):
                continue
            dt = date(year, mo, day)
            if not m.group(3) and dt < today:
                dt = date(year + 1, mo, day)
            return dt.isoformat()
        except Exception:
            continue

    # Month name: July 19, Jul 19 2026, expiring July 19.
    month_names = "|".join(sorted((re.escape(k) for k in MONTH_LOOKUP.keys()), key=len, reverse=True))
    m = re.search(rf"\b({month_names})\s+(\d{{1,2}})(?:,?\s+(20\d{{2}}|\d{{2}}))?\b", raw, re.I)
    if m:
        try:
            mo = MONTH_LOOKUP[m.group(1).lower()]
            day = int(m.group(2))
            year = _year_from_text(m.group(3))
            dt = date(year, mo, day)
            if not m.group(3) and dt < today:
                dt = date(year + 1, mo, day)
            return dt.isoformat()
        except Exception:
            return None
    return None


def _parse_dte(text: str, default: Optional[int] = None) -> Optional[int]:
    m = re.search(r"\b(\d{1,3})\s*(?:dte|days?\s+to\s+expir(?:y|ation)|days?)\b", text or "", re.I)
    if m:
        return max(1, min(365, _safe_int(m.group(1), default or 45)))
    return default


def _parse_strikes(text: str, trade_type: Optional[str] = None) -> List[float]:
    raw = text or ""
    # For explicit spread notation near trade type: PS 95/90, CS 100/105.
    if trade_type:
        m = re.search(rf"\b{re.escape(trade_type)}\b\s*(\d+(?:\.\d+)?)(?:\s*/\s*|\s*-\s*)(\d+(?:\.\d+)?)", raw, re.I)
        if m:
            return [_safe_float(m.group(1), 0.0) or 0.0, _safe_float(m.group(2), 0.0) or 0.0]
    m = re.search(r"\b(\d+(?:\.\d+)?)(?:\s*/\s*|\s*-\s*)(\d+(?:\.\d+)?)(?:\s*/\s*|\s*-\s*)?(\d+(?:\.\d+)?)?(?:\s*/\s*|\s*-\s*)?(\d+(?:\.\d+)?)?\b", raw)
    if m:
        vals = [_safe_float(g, None) for g in m.groups() if g]
        vals = [v for v in vals if v is not None]
        # Avoid mistaking date fragments such as 6/26/2026 or 6/26 for strikes
        # when no strategy type is present.  Manual strike review should be
        # explicit (PS/CS/IC/CALL/PUT or clear strike wording).
        if not trade_type and len(vals) >= 2:
            a, b = vals[0], vals[1]
            looks_like_date = 1 <= a <= 12 and 1 <= b <= 31
            if looks_like_date:
                return []
            has_strike_word = re.search(r"\b(strike|strikes|spread|leg|legs)\b", raw, re.I)
            if not has_strike_word:
                return []
        if trade_type or len(vals) >= 2:
            return vals
    return []


def _classify_question(text: str) -> Dict[str, Any]:
    q = (text or "").strip()
    lq = q.lower()
    symbol = _parse_symbol(q)
    trade_type = _parse_trade_type(q)
    expiry = _parse_expiry_date(q)
    dte = _parse_dte(q)
    strikes = _parse_strikes(q, trade_type)
    price_cap = _parse_price_cap(q)
    top_n = _parse_top_n(q)
    oi_horizons = _parse_oi_horizons(q)
    scanner_req = _scanner_builder_request_params(q)

    if "open trade" in lq or "open trades" in lq or "portfolio" in lq:
        if "roll" in lq:
            intent = "roll_review"
        elif any(w in lq for w in ["health", "analy", "review", "suggest", "action"]):
            intent = "open_trades_health"
        elif "which" in lq and "today" in lq:
            intent = "open_trades_health"
        else:
            intent = "open_trades_health"
    elif _is_oi_buildup_request(q):
        intent = "oi_buildup_divergence" if _is_oi_divergence_request(q) else "oi_buildup_scan"
    elif scanner_req.get("query_text") and scanner_req.get("scanner_source") in {"saved", "builtin"}:
        intent = "scanner_builder_query"
    elif any(w in lq for w in ["momentum", "retest", "retrace", "pullback"]):
        intent = "momentum_retests"
    elif symbol and not trade_type and _weekly_plan_requested(q):
        intent = "weekly_plan_strategy"
    elif symbol and not trade_type and _is_best_strategy_request(q):
        # Example: "What is the best strategy for NOW expiring July 19?"
        # This must run an all-strategy search, not reuse/default to PS.
        intent = "best_strategy"
    elif symbol and _is_manual_trade_review_request(q, trade_type, strikes):
        intent = "specific_trade"
    elif any(w in lq for w in ["new finds", "findings", "scanner history", "latest finds", "stored finds"]):
        intent = "agentic_findings"
    elif ("agentic" in lq or "ai scanner" in lq or "trade idea" in lq or "trade ideas" in lq) and any(w in lq for w in ["run", "scan", "find", "show", "top"]):
        intent = "agentic_scan"
    elif scanner_req.get("query_text"):
        intent = "scanner_builder_query"
    else:
        intent = "help"

    return {
        "intent": intent,
        "symbol": symbol,
        "trade_type": trade_type,
        "expiry": expiry,
        "dte": dte,
        "strikes": strikes,
        "price_cap": price_cap,
        "top_n": top_n,
        "question": q,
        **oi_horizons,
        **scanner_req,
    }


def _available_expiries(symbol: str, live_fallback: bool = True) -> List[str]:
    """Return known future expiries, preferring local snapshots over live calls.

    The prior AI Hub implementation asked yfinance for the option expiry list
    before checking SQLite.  For SPY/QQQ/liquid weekly queries this can hang or
    throttle even though the dashboard already has the chain locally.  This
    function is DB-first and only falls back to yfinance when local data is
    missing.
    """
    sym = (symbol or "").upper().strip()
    today = date.today().isoformat()
    exps: List[str] = []
    con = _conn()
    try:
        rows = con.execute(
            "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration",
            (sym, today),
        ).fetchall()
        exps.extend(str(r["expiration"]) for r in rows if r["expiration"])
    except Exception:
        pass
    finally:
        con.close()

    if exps or not live_fallback:
        return sorted(set(exps))

    try:
        import yfinance as yf
        vals = list(getattr(yf.Ticker(sym), "options", []) or [])
        exps.extend(str(x) for x in vals if x)
    except Exception:
        pass
    return sorted(set(exps))


def _resolve_expiry(symbol: str, requested_expiry: Optional[str], target_dte: Optional[int] = None) -> Dict[str, Any]:
    today = date.today()
    if requested_expiry:
        try:
            req = datetime.strptime(requested_expiry, "%Y-%m-%d").date()
        except Exception:
            return {"expiry": None, "dte": None, "requested_expiry": requested_expiry, "exact": False, "note": "Could not parse requested expiry."}
        if req < today:
            return {"expiry": None, "dte": (req - today).days, "requested_expiry": requested_expiry, "exact": False, "note": "Requested expiry is in the past."}
        exps = _available_expiries(symbol, live_fallback=False)
        if not exps:
            return {"expiry": requested_expiry, "dte": (req - today).days, "requested_expiry": requested_expiry, "exact": True, "note": "No option expiry list available; exact chain will decide availability.", "available_expiries": []}
        if requested_expiry in exps:
            return {"expiry": requested_expiry, "dte": (req - today).days, "requested_expiry": requested_expiry, "exact": True, "note": "Exact listed expiry found.", "available_expiries": exps[:12]}
        # If the requested date is a weekend/holiday, use the nearest listed expiry but mark it clearly.
        future = []
        for x in exps:
            try:
                xd = datetime.strptime(x, "%Y-%m-%d").date()
                if xd >= today:
                    future.append((abs((xd - req).days), xd, x))
            except Exception:
                pass
        if future:
            future.sort(key=lambda r: (r[0], r[1]))
            chosen = future[0][2]
            cd = future[0][1]
            return {
                "expiry": chosen,
                "dte": (cd - today).days,
                "requested_expiry": requested_expiry,
                "exact": False,
                "note": f"Requested expiry {requested_expiry} is not listed; using nearest listed expiry {chosen}.",
                "available_expiries": exps[:12],
            }
        return {"expiry": None, "dte": None, "requested_expiry": requested_expiry, "exact": False, "note": "No future option expiries available.", "available_expiries": exps[:12]}

    target = int(target_dte or 45)
    try:
        from ..scanners.uae_trade_scanner import _pick_expiry
        exp, dte, exps = _pick_expiry(symbol, target)
        return {"expiry": exp, "dte": dte, "requested_dte": target, "exact": True, "note": f"Selected listed expiry nearest {target} DTE.", "available_expiries": exps[:12]}
    except Exception as exc:
        return {"expiry": None, "dte": None, "requested_dte": target, "exact": False, "note": str(exc)[:180]}


def _find_exact_row(df: Any, strike: float) -> Optional[Any]:
    try:
        if df is None or getattr(df, "empty", True):
            return None
        for _, row in df.iterrows():
            k = _safe_float(row.get("strike"), None)
            if k is not None and abs(k - float(strike)) <= 0.01:
                return row
    except Exception:
        pass
    return None


def _trade_direction(trade_type: str) -> str:
    tt = (trade_type or "").upper()
    if tt in {"PS", "CALL"}:
        return "bull"
    if tt in {"CS", "PUT"}:
        return "bear"
    if tt == "IC":
        return "neutral"
    return "neutral"


def _build_trade_from_exact_strikes(
    symbol: str,
    trade_type: str,
    expiry: str,
    dte: int,
    spot: float,
    strikes: Sequence[float],
    width: float,
    short_delta: float,
    target_rr: float,
    min_rr: float,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    from ..scanners.uae_trade_scanner import (
        _get_chain, _hist_iv_proxy, _choose_vertical, _choose_ic, _choose_debit, _leg_from_row,
    )

    tt = (trade_type or "AUTO").upper()
    meta: Dict[str, Any] = {"expiry": expiry, "dte": dte, "requested_strikes": list(strikes or [])}
    calls, puts, chain_source = _get_chain(symbol, expiry)
    meta["chain_source"] = chain_source
    if calls is None or puts is None:
        meta["error"] = f"Option chain unavailable for {symbol} {expiry}: {chain_source}"
        return None, meta

    fallback_iv = _hist_iv_proxy(symbol)
    meta["iv_proxy"] = fallback_iv

    # No explicit strikes: ask the existing exact-chain selector for the best candidate.
    if not strikes:
        if tt == "PS":
            trade = _choose_vertical(symbol, expiry, dte, spot, "bull", width, short_delta, target_rr, min_rr, fallback_iv, calls, puts)
        elif tt == "CS":
            trade = _choose_vertical(symbol, expiry, dte, spot, "bear", width, short_delta, target_rr, min_rr, fallback_iv, calls, puts)
        elif tt == "IC":
            trade = _choose_ic(symbol, expiry, dte, spot, width, short_delta, target_rr, min_rr, fallback_iv, calls, puts)
        elif tt == "CALL":
            trade = _choose_debit(symbol, expiry, dte, spot, "bull", fallback_iv, calls, puts)
        elif tt == "PUT":
            trade = _choose_debit(symbol, expiry, dte, spot, "bear", fallback_iv, calls, puts)
        else:
            trade = _choose_vertical(symbol, expiry, dte, spot, "bull", width, short_delta, target_rr, min_rr, fallback_iv, calls, puts)
        if not trade:
            meta["error"] = "No candidate passed the exact-chain selector for the requested structure."
            return None, meta
        trade["iv_proxy"] = fallback_iv
        trade["actual_dte"] = dte
        return trade, meta

    if tt in {"PS", "CS"}:
        if len(strikes) < 2:
            meta["error"] = "Two strikes are required for a vertical spread."
            return None, meta
        is_bull = tt == "PS"
        opt_type = "put" if is_bull else "call"
        df = puts if is_bull else calls
        short_k = float(strikes[0])
        long_k = float(strikes[1])
        short_row = _find_exact_row(df, short_k)
        long_row = _find_exact_row(df, long_k)
        if short_row is None or long_row is None:
            meta["error"] = f"Requested strikes {short_k:g}/{long_k:g} are not both listed in the {expiry} {opt_type} chain."
            return None, meta
        short_leg = _leg_from_row(symbol, expiry, short_row, opt_type, spot, dte, fallback_iv)
        long_leg = _leg_from_row(symbol, expiry, long_row, opt_type, spot, dte, fallback_iv)
        credit = round(max(0.0, (short_leg.get("mid") or 0.0) - (long_leg.get("mid") or 0.0)), 2)
        actual_width = round(abs(short_leg["strike"] - long_leg["strike"]), 2)
        max_loss = round(max(0.01, actual_width - credit), 2)
        rr = round(credit / max_loss, 2) if max_loss > 0 else 0.0
        trade = {
            "trade_type": tt,
            "bias": "Bullish" if is_bull else "Bearish",
            "direction": "bull" if is_bull else "bear",
            "expiry": expiry,
            "dte": dte,
            "sell_strike": short_leg["strike"],
            "buy_strike": long_leg["strike"],
            "legs": f"Sell {short_leg['strike']}{'P' if is_bull else 'C'} / Buy {long_leg['strike']}{'P' if is_bull else 'C'}",
            "short_leg": short_leg,
            "long_leg": long_leg,
            "width": actual_width,
            "credit": credit,
            "max_loss": max_loss,
            "rr": rr,
            "target_rr": target_rr,
            "min_rr": min_rr,
            "otm_pct": round(abs(short_leg["strike"] - spot) / max(spot, 0.01) * 100.0, 2),
            "short_delta": round(short_leg.get("delta") or 0.0, 3),
            "short_oi": short_leg.get("open_interest"),
            "short_oi_change": short_leg.get("oi_change"),
            "short_oi_change_pct": short_leg.get("oi_change_pct"),
            "iv_proxy": fallback_iv,
            "actual_dte": dte,
            "requested_exact_strikes": True,
        }
        if is_bull and short_leg["strike"] >= spot:
            trade.setdefault("warnings", []).append("Short put is not below spot; this is not an OTM bull put spread.")
        if (not is_bull) and short_leg["strike"] <= spot:
            trade.setdefault("warnings", []).append("Short call is not above spot; this is not an OTM bear call spread.")
        return trade, meta

    if tt == "IC":
        if len(strikes) >= 4:
            # Reuse selector for now when explicit IC strikes are not implemented leg-by-leg.
            trade = _choose_ic(symbol, expiry, dte, spot, width, short_delta, target_rr, min_rr, fallback_iv, calls, puts)
            if trade:
                trade["warnings"] = ["Explicit IC four-strike validation is not yet used; selected closest rule-based IC from the listed chain."]
                return trade, meta
        meta["error"] = "Iron condor requires either no strikes for best-chain selection or four strikes for manual review."
        return None, meta

    # Single-leg debit: if user supplies a strike, validate it exactly.
    if tt in {"CALL", "PUT"}:
        opt_type = "call" if tt == "CALL" else "put"
        df = calls if tt == "CALL" else puts
        row = _find_exact_row(df, float(strikes[0])) if strikes else None
        if row is None:
            if strikes:
                meta["error"] = f"Requested {tt} strike {strikes[0]:g} is not listed for {expiry}."
                return None, meta
            direction = "bull" if tt == "CALL" else "bear"
            trade = _choose_debit(symbol, expiry, dte, spot, direction, fallback_iv, calls, puts)
            return trade, meta
        leg = _leg_from_row(symbol, expiry, row, opt_type, spot, dte, fallback_iv)
        premium = round(max(0.01, leg.get("mid") or 0.01), 2)
        return {
            "trade_type": tt,
            "bias": "Bullish" if tt == "CALL" else "Bearish",
            "direction": "bull" if tt == "CALL" else "bear",
            "expiry": expiry,
            "dte": dte,
            "buy_strike": leg["strike"],
            "legs": f"Buy {leg['strike']}{'C' if tt == 'CALL' else 'P'}",
            "long_leg": leg,
            "debit": premium,
            "max_loss": premium,
            "rr": 1.0,
            "target_rr": target_rr,
            "min_rr": 0.0,
            "iv_proxy": fallback_iv,
            "actual_dte": dte,
        }, meta

    meta["error"] = f"Unsupported trade type: {trade_type}"
    return None, meta


def _agentic_context_for_symbol(symbol: str, dte: int) -> Dict[str, Any]:
    from ..scanners.agentic_ai_scanner import (
        MARKET_PROXIES, _scan_plan, _sector_for_symbol, _market_context, _sector_contexts,
    )
    from ..scanners.uae_trade_scanner import _build_indicator_cache

    sym = (symbol or "").upper().strip()
    plan = _scan_plan(dte)
    sector, sector_etf = _sector_for_symbol(sym)
    cache_key = "|".join([sym, sector_etf, ",".join(plan.get("required_tfs") or []), str(max(1, int(dte or 1)))])
    try:
        ts, cached = _AGENTIC_CONTEXT_CACHE.get(cache_key, (0, None))
        if cached is not None and time.time() - ts <= 300:
            return cached
    except Exception:
        pass

    fetch_symbols = sorted(set([sym, sector_etf] + list(MARKET_PROXIES)))
    indicators_by_symbol, errors_by_symbol, frames_by_symbol = _build_indicator_cache(fetch_symbols, plan)
    market_ctx = _market_context(indicators_by_symbol, frames_by_symbol, dte)
    sector_ctx_map = _sector_contexts([sector_etf], indicators_by_symbol, frames_by_symbol)
    ctx = {
        "sector": sector,
        "sector_etf": sector_etf,
        "plan": plan,
        "indicators_by_symbol": indicators_by_symbol,
        "errors_by_symbol": errors_by_symbol,
        "frames_by_symbol": frames_by_symbol,
        "market_context": market_ctx,
        "sector_context": sector_ctx_map.get(sector_etf) or {"bias": "neutral", "score": 0},
    }
    try:
        _AGENTIC_CONTEXT_CACHE[cache_key] = (time.time(), ctx)
        if len(_AGENTIC_CONTEXT_CACHE) > 24:
            for k, _ in sorted(_AGENTIC_CONTEXT_CACHE.items(), key=lambda kv: kv[1][0])[:8]:
                _AGENTIC_CONTEXT_CACHE.pop(k, None)
    except Exception:
        pass
    return ctx


def _score_specific_trade(symbol: str, trade_type: str, expiry: Optional[str], dte: Optional[int], strikes: Sequence[float]) -> Dict[str, Any]:
    from ..scanners.uae_trade_scanner import _wall_proxy_from_chain, _spy_gex_context, _checklist_score, _get_earn_days
    from ..scanners.agentic_ai_scanner import (
        _alignment_score, _rs_score, _price_volume_score, _options_score, _futures_alignment_score,
        _relative_strength, _daily_metrics, _options_oi_buildup, _pressure_context, _bias_label,
    )

    sym = (symbol or "").upper().strip()
    tt = (trade_type or "").upper().strip()
    if not tt:
        return {"ok": False, "error": "No trade type supplied; use best-strategy search or specify PS/CS/IC/CALL/PUT.", "symbol": sym}
    expiry_info = _resolve_expiry(sym, expiry, dte or 45)
    if not expiry_info.get("expiry"):
        return {
            "ok": False,
            "error": expiry_info.get("note") or "Could not resolve a valid expiry.",
            "symbol": sym,
            "expiry_info": expiry_info,
        }
    exp = expiry_info["expiry"]
    actual_dte = _safe_int(expiry_info.get("dte"), dte or 45)
    if actual_dte < 0:
        return {"ok": False, "error": "Expiry is in the past.", "symbol": sym, "expiry_info": expiry_info}

    ctx = _agentic_context_for_symbol(sym, max(1, actual_dte))
    frames = ctx["frames_by_symbol"].get(sym) or {}
    indicators = ctx["indicators_by_symbol"].get(sym) or {}
    daily = _daily_metrics(frames.get("1d"))
    spot = _safe_float(daily.get("spot"), None)
    if spot is None:
        # Fallback to indicator close.
        for tf in ("1d", "1h", "1wk"):
            spot = _safe_float((indicators.get(tf) or {}).get("close"), None)
            if spot:
                break
    if not spot:
        return {"ok": False, "error": f"No price data available for {sym}; cannot score trade.", "symbol": sym, "expiry_info": expiry_info}

    width = abs(float(strikes[0]) - float(strikes[1])) if len(strikes) >= 2 else 5.0
    if width <= 0:
        width = 5.0
    trade, meta = _build_trade_from_exact_strikes(
        sym, tt, exp, actual_dte, float(spot), strikes,
        width=width,
        short_delta=0.45,
        target_rr=1.00,
        min_rr=0.70,
    )
    if not trade:
        return {
            "ok": False,
            "error": meta.get("error") or "Could not build the requested trade from listed chain data.",
            "symbol": sym,
            "trade_type": tt,
            "expiry_info": expiry_info,
            "meta": meta,
        }

    direction = trade.get("direction") or _trade_direction(tt)
    plan = ctx["plan"]
    walls = _wall_proxy_from_chain(sym, exp)
    gex = _spy_gex_context(sym, exp, max(1, actual_dte), float(spot), trade.get("iv_proxy") or 25.0)
    earn_days = _get_earn_days(sym)
    earnings_ctx = _cached_earnings_context(sym, exp, actual_dte)
    if earnings_ctx.get("earnings_days") is not None:
        earn_days = _safe_int(earnings_ctx.get("earnings_days"), earn_days)
    checks, uae_score, grade = _checklist_score(sym, direction, trade, indicators, plan, earn_days, 14, walls, gex)

    spy_df = (ctx["frames_by_symbol"].get("SPY") or {}).get("1d")
    sector_df = (ctx["frames_by_symbol"].get(ctx["sector_etf"]) or {}).get("1d")
    rs = _relative_strength(frames.get("1d"), spy_df, sector_df)
    oi = _options_oi_buildup(sym, max(1, actual_dte))
    pressure = _pressure_context(sym, _safe_float(spot, None), max(1, actual_dte))
    market_ctx = ctx["market_context"]
    sector_ctx = ctx["sector_context"]

    m_align = _alignment_score(direction, market_ctx.get("bias"))
    s_align = _alignment_score(direction, sector_ctx.get("bias"))
    rs_sc = _rs_score(direction, rs)
    pv_sc = _price_volume_score(direction, daily)
    opt_sc = _options_score(direction, oi, pressure)
    fut_sc = _futures_alignment_score(direction, market_ctx)
    confidence = round(
        uae_score * 0.30 +
        m_align * 0.15 +
        s_align * 0.15 +
        rs_sc * 0.15 +
        pv_sc * 0.10 +
        opt_sc * 0.10 +
        fut_sc * 0.05
    )
    confidence = int(max(0, min(100, confidence)))

    recommendation = "OPEN" if confidence >= 80 and uae_score >= 75 else "OPEN_SMALL" if confidence >= 65 and uae_score >= 60 else "AVOID"
    if trade.get("rr") is not None and (trade.get("trade_type") in {"PS", "CS", "IC"}) and _safe_float(trade.get("rr"), 0.0) < 0.50:
        recommendation = "AVOID"
    if earn_days < 14:
        recommendation = "AVOID" if confidence < 85 else "OPEN_SMALL"
    if earnings_ctx.get("earnings_conflict"):
        recommendation = "AVOID"

    score_detail = {
        "uae_score": uae_score,
        "market_alignment_score": m_align,
        "sector_alignment_score": s_align,
        "relative_strength_score": rs_sc,
        "price_volume_score": pv_sc,
        "options_pressure_score": opt_sc,
        "futures_alignment_score": fut_sc,
        "weights": {"uae": 0.30, "market": 0.15, "sector": 0.15, "rs": 0.15, "price_volume": 0.10, "options": 0.10, "futures": 0.05},
    }
    rationale = [
        f"{sym} {TRADE_TYPE_LABELS.get(tt, tt)} scored {confidence}/100; UAE checklist {uae_score}/100 ({grade}).",
        f"Market regime is {_bias_label(market_ctx.get('bias'))}; sector {ctx['sector']} via {ctx['sector_etf']} is {_bias_label(sector_ctx.get('bias'))}.",
        f"RS vs market 20d {rs.get('rs_vs_market_20d')}; RS vs sector 20d {rs.get('rs_vs_sector_20d')}; RSI {daily.get('rsi14')}; RSIDiff90 {daily.get('rsi_diff90')}; MACD hist {daily.get('macd_hist')}.",
        f"Option OI: {oi.get('note') if isinstance(oi, dict) else 'n/a'} Strike/OI: short OI {trade.get('short_oi')}; OI change {trade.get('short_oi_change')}.",
    ]
    if expiry_info.get("note"):
        rationale.append(expiry_info["note"])
    timeframe = _dte_bucket(actual_dte)
    rationale.append(f"Timeframe: {timeframe} based on {actual_dte} DTE.")
    if earnings_ctx.get("earnings_note"):
        rationale.append("Earnings guardrail: " + str(earnings_ctx.get("earnings_note")))
    if trade.get("warnings"):
        rationale.append("Warnings: " + " ".join(str(x) for x in trade.get("warnings") or []))

    action = ""
    if recommendation == "OPEN":
        action = "Eligible to open at planned size only if current bid/ask fill remains near the scored credit/debit and the entry timeframe still confirms."
    elif recommendation == "OPEN_SMALL":
        action = "Only open small or wait for one more confirming close; some score components are not ideal."
    else:
        action = "Skip this setup under the current rules unless the weak components improve."
    if earnings_ctx.get("earnings_conflict"):
        action = "Avoid this setup/timeframe because it would hold through earnings; wait until after the report or choose an expiry that ends before earnings."

    return {
        "ok": True,
        "symbol": sym,
        "trade_type": tt,
        "trade_type_label": TRADE_TYPE_LABELS.get(tt, tt),
        "direction": direction,
        "spot": _safe_float(spot, None, 2),
        "expiry": exp,
        "dte": actual_dte,
        "expiry_info": expiry_info,
        "trade": trade,
        "strategy": trade,
        "confidence": confidence,
        "score": confidence,
        "uae_score": uae_score,
        "grade": grade,
        "recommendation": recommendation,
        "action": action,
        "timeframe": timeframe,
        "suggested_timeframe": timeframe,
        "earnings": earnings_ctx,
        "earnings_conflict": bool(earnings_ctx.get("earnings_conflict")),
        "rationale": " ".join(rationale),
        "checks": checks,
        "metrics": {
            "daily": daily,
            "relative_strength": rs,
            "option_oi_buildup": oi,
            "pressure": pressure,
            "score_detail": score_detail,
            "walls": walls,
            "gex": gex,
            "market_context": market_ctx,
            "sector_context": sector_ctx,
            "option_meta": meta,
        },
        "evidence": [
            {"label": "Market regime", "value": market_ctx.get("bias_label") or market_ctx.get("bias"), "source": "Agentic market context"},
            {"label": "Sector regime", "value": f"{ctx['sector']} / {sector_ctx.get('bias_label') or sector_ctx.get('bias')}", "source": "Agentic sector context"},
            {"label": "UAE checklist", "value": f"{uae_score}/100 {grade}", "source": "UAE guide scanner"},
            {"label": "Option OI", "value": oi.get("note") if isinstance(oi, dict) else "n/a", "source": "Local option OI snapshots"},
            {"label": "RR", "value": trade.get("rr"), "source": "Listed option chain mid-price estimate"},
            {"label": "Earnings", "value": earnings_ctx.get("earnings_note"), "source": "earnings_calendar cache"},
        ],
    }


def _answer_specific_trade(params: Dict[str, Any]) -> Dict[str, Any]:
    symbol = params.get("symbol")
    trade_type = params.get("trade_type")
    if not symbol:
        return _help_result("I need a ticker symbol to score a specific trade.")
    if not trade_type:
        return _help_result("I need a strategy type for a specific trade review. Ask for the best strategy, or specify PS/CS/IC/CALL/PUT with strikes if needed.")
    res = _score_specific_trade(symbol, trade_type, params.get("expiry"), params.get("dte"), params.get("strikes") or [])
    if not res.get("ok"):
        answer = f"I could not score that trade from the underlying data. Reason: {res.get('error')}."
        return {"ok": False, "intent": "specific_trade", "params": params, "answer": answer, "items": [], "evidence": res.get("evidence") or [], "raw": res}

    trade = res.get("trade") or {}
    exp_note = (res.get("expiry_info") or {}).get("note") or ""
    rr = trade.get("rr")
    credit = trade.get("credit") if trade.get("credit") is not None else trade.get("debit")
    best_fixed_request = "best" in (params.get("question") or "").lower() and not (params.get("strikes") or [])
    if best_fixed_request and res.get("recommendation") == "AVOID":
        lead = (
            f"NO ELIGIBLE {res['trade_type']} SETUP: the best chain-selected {TRADE_TYPE_LABELS.get(res['trade_type'], res['trade_type'])} "
            f"candidate was {trade.get('legs') or ''} expiring {res['expiry']}, but it only scored {res['confidence']}/100."
        )
    else:
        lead = f"{res['recommendation']}: {res['symbol']} {res['trade_type']} {trade.get('legs') or ''} expiring {res['expiry']} scored {res['confidence']}/100."
    lines = [
        lead,
        f"Action: {res['action']}",
        f"Pricing/risk: spot {res.get('spot')}, credit/debit {credit}, RR {rr}, max loss {trade.get('max_loss')}.",
        f"Rationale: {res['rationale']}",
    ]
    if exp_note:
        lines.append(f"Expiry note: {exp_note}")
    return {
        "ok": True,
        "intent": "specific_trade",
        "params": params,
        "answer": "\n".join(lines),
        "items": [res],
        "evidence": res.get("evidence") or [],
        "rules_used": ["UAE checklist", "Agentic market/sector regime", "RS vs market/sector", "Price/volume", "Option OI/GEX weighting", "Futures OI alignment"],
    }



def _expiry_dte_value(expiry: Optional[str], dte: Optional[int] = None) -> Optional[int]:
    if dte is not None:
        try:
            return int(dte)
        except Exception:
            pass
    if not expiry:
        return None
    try:
        return (date.fromisoformat(str(expiry)) - date.today()).days
    except Exception:
        return None


def _weekly_plan_requested(question: str) -> bool:
    lq = (question or "").lower()
    return any(x in lq for x in [
        "weekly plan", "weekly strategy", "this week", "friday expiry", "friday plan",
        "0dte", "1dte", "2dte", "3dte", "4dte", "5dte", "6dte", "7dte",
        "weekly expiry", "weekly options",
    ])


def _should_use_weekly_plan(params: Dict[str, Any]) -> Tuple[bool, str]:
    """Choose the fast Weekly Plan path for near-expiry strategy selection.

    The generic best-strategy search scores five structures independently.  For
    SPY/QQQ/IWM-style weekly trades this is slower and less aligned with the
    user's workflow than the dedicated Weekly Plan, which evaluates the full
    plan once and returns candidate structures from the same plan.
    """
    symbol = str(params.get("symbol") or "").upper().strip()
    question = params.get("question") or ""
    dte = _expiry_dte_value(params.get("expiry"), params.get("dte"))
    if dte is not None and dte < 0:
        return False, "requested expiry is in the past"
    if _weekly_plan_requested(question):
        return True, "question explicitly asked for weekly-plan style analysis"
    if dte is not None and 0 <= dte <= WEEKLY_PLAN_MAX_DTE:
        return True, f"requested expiry is {dte} DTE, inside the weekly-plan window"
    if symbol in CORE_WEEKLY_PLAN_SYMBOLS and not params.get("expiry") and params.get("dte") is None:
        return True, f"{symbol} is a core weekly-plan symbol and no longer-dated expiry was requested"
    if symbol in CORE_WEEKLY_PLAN_SYMBOLS and dte is not None and dte <= 14:
        return True, f"{symbol} is a core weekly-plan symbol and expiry is near-term"
    if symbol in LIQUID_WEEKLY_PLAN_SYMBOLS and dte is not None and 0 <= dte <= WEEKLY_PLAN_MAX_DTE:
        return True, f"{symbol} is a liquid weekly symbol and expiry is near-term"
    return False, "generic all-strategy search is appropriate"


def _weekly_strategy_code(name: str) -> str:
    n = (name or "").lower()
    if "bull put" in n:
        return "PS"
    if "bear call" in n:
        return "CS"
    if "iron condor" in n:
        return "IC"
    if "call debit" in n or "bull call" in n or "call spread" in n:
        return "CALL"
    if "put debit" in n or "bear put" in n or "put spread" in n:
        return "PUT"
    if "calendar" in n:
        return "CAL"
    return "WEEKLY"


def _weekly_legs_text(strategy: Dict[str, Any]) -> str:
    legs = strategy.get("legs") or []
    if isinstance(legs, str):
        return legs
    out = []
    for leg in legs:
        try:
            side = str(leg.get("side") or "").strip().capitalize()
            opt = str(leg.get("type") or "").strip().lower()
            strike = leg.get("strike")
            suffix = "C" if opt.startswith("call") else "P" if opt.startswith("put") else ""
            exp = leg.get("expiry")
            label = f"{side} {strike}{suffix}" if strike is not None else side
            if exp:
                label += f" {exp}"
            out.append(label)
        except Exception:
            pass
    return " / ".join([x for x in out if x])


def _weekly_money_from_entry(entry: str) -> Tuple[Optional[str], Optional[float]]:
    txt = entry or ""
    m = re.search(r"\b(Credit|Debit)\s*~?\$\s*(\d+(?:\.\d+)?)", txt, re.I)
    if not m:
        return None, None
    return m.group(1).lower(), _safe_float(m.group(2), None, 2)


def _weekly_approach_scores(plan: Dict[str, Any]) -> Dict[str, float]:
    score_map: Dict[str, float] = {}
    plan_score = plan.get("weekly_plan_score") or {}
    for item in plan_score.get("approaches") or []:
        code = str(item.get("name") or "").upper()
        label = str(item.get("label") or "")
        score = _safe_float(item.get("score"), None)
        if score is None:
            continue
        if code:
            score_map[code] = score
        if label:
            score_map[_weekly_strategy_code(label)] = max(score, score_map.get(_weekly_strategy_code(label), 0.0))
    return score_map


def _weekly_preferred_code(plan: Dict[str, Any]) -> Optional[str]:
    pref = ((plan.get("weekly_plan_score") or {}).get("preferred") or {})
    code = str(pref.get("name") or "").upper().strip()
    if code:
        return code
    label = str(pref.get("label") or "")
    return _weekly_strategy_code(label) if label else None


def _normalise_weekly_strategy(strategy: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    sym = str(plan.get("symbol") or "").upper()
    name = str(strategy.get("type") or strategy.get("strategy") or "Weekly strategy")
    code = _weekly_strategy_code(name)
    score_map = _weekly_approach_scores(plan)
    score = _safe_float(strategy.get("confidence"), None)
    if score is None:
        score = score_map.get(code)
    if score is None:
        score = _safe_float((plan.get("weekly_plan_score") or {}).get("composite_score"), None)
    if score is None:
        score = _safe_float(plan.get("confidence"), 50.0)
    score = round(float(score or 0), 1)
    pref_code = _weekly_preferred_code(plan)
    if pref_code and code == pref_code:
        score = min(100.0, round(score + 4.0, 1))

    entry_kind, entry_amt = _weekly_money_from_entry(str(strategy.get("entry") or ""))
    max_profit = _safe_float(strategy.get("max_profit"), None)
    max_loss = _safe_float(strategy.get("max_loss"), None)
    rr = None
    if max_profit is not None and max_loss and max_loss > 0:
        rr = round(max_profit / max_loss, 2)
    elif strategy.get("rr") is not None:
        rr = _safe_float(strategy.get("rr"), None, 2)

    name_l = name.lower()
    txt = (str(strategy.get("iv_context") or "") + " " + str(strategy.get("edge") or "") + " " + str(strategy.get("rationale") or "")).lower()
    diagnostic = "not recommended" in name_l or "too thin" in txt or "not recommended" in txt
    if diagnostic:
        recommendation = "AVOID"
    elif score >= 72:
        recommendation = "OPEN"
    elif score >= 58:
        recommendation = "OPEN_SMALL"
    else:
        recommendation = "WATCH"

    if recommendation == "OPEN":
        action = "Eligible under the Weekly Plan if the current bid/ask fill is close to the planned entry and the intraday trigger still agrees."
    elif recommendation == "OPEN_SMALL":
        action = "Use reduced size or wait for the next intraday confirmation; the Weekly Plan edge is present but not strong enough for full size."
    elif recommendation == "WATCH":
        action = "Do not force entry yet; keep this as the best watch candidate until score/levels improve."
    else:
        action = "Skip this structure; the Weekly Plan marked it as diagnostic or not recommended."

    trade = {
        "trade_type": code,
        "strategy_name": name,
        "legs": _weekly_legs_text(strategy),
        "entry": strategy.get("entry"),
        "credit": entry_amt if entry_kind == "credit" else None,
        "debit": entry_amt if entry_kind == "debit" else None,
        "rr": rr,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "pop": strategy.get("pop"),
        "target": strategy.get("target"),
        "edge": strategy.get("edge"),
        "anchor": strategy.get("anchor"),
        "raw": strategy,
    }
    return {
        "ok": True,
        "symbol": sym,
        "trade_type": code,
        "trade_type_label": TRADE_TYPE_LABELS.get(code, name),
        "strategy_name": name,
        "direction": str(strategy.get("bias") or strategy.get("direction") or plan.get("bias") or "").lower(),
        "spot": _safe_float(plan.get("spot"), None, 2),
        "expiry": plan.get("expiry"),
        "dte": plan.get("dte"),
        "confidence": score,
        "score": score,
        "uae_score": _safe_float(((plan.get("weekly_plan_score") or {}).get("component_scores") or {}).get("technical_trend"), None, 1),
        "grade": "Weekly Plan",
        "recommendation": recommendation,
        "diagnostic_only": diagnostic or recommendation == "AVOID",
        "action": action,
        "rationale": str(strategy.get("rationale") or strategy.get("edge") or ""),
        "trade": trade,
        "strategy": trade,
        "metrics": {
            "weekly_bias": plan.get("bias"),
            "weekly_score": plan.get("score"),
            "weekly_confidence": plan.get("confidence"),
            "weekly_plan_score": plan.get("weekly_plan_score"),
            "iv_rank": plan.get("iv_rank"),
            "pcr": plan.get("pcr"),
            "expected_move": plan.get("expected_move"),
            "walls": plan.get("walls"),
            "futures_bias": plan.get("futures_bias"),
            "futures_note": plan.get("futures_note"),
            "timeframes": plan.get("timeframes"),
        },
    }


def _weekly_strategy_rank(row: Dict[str, Any]) -> Tuple[int, float, float, float, str]:
    rec_rank = {"OPEN": 3, "OPEN_SMALL": 2, "WATCH": 1, "AVOID": 0}
    trade = row.get("trade") or {}
    pop = _safe_float(trade.get("pop"), 0.0) or 0.0
    rr = _safe_float(trade.get("rr"), 0.0) or 0.0
    return (
        rec_rank.get(str(row.get("recommendation") or "").upper(), 0),
        _safe_float(row.get("confidence"), 0.0) or 0.0,
        pop,
        rr,
        str(row.get("strategy_name") or ""),
    )


def _format_weekly_candidate(row: Dict[str, Any]) -> str:
    t = row.get("trade") or {}
    cd = t.get("credit") if t.get("credit") is not None else t.get("debit")
    return (
        f"{row.get('strategy_name')} ({row.get('trade_type')}) score {row.get('confidence')}/100, "
        f"{row.get('recommendation')}, entry {cd}, POP {t.get('pop')}, RR {t.get('rr')}"
    )


def _run_weekly_plan_direct(symbol: str, expiry: Optional[str], app: Any) -> Dict[str, Any]:
    if app is None:
        return {"ok": False, "error": "No Flask app context is available for the Weekly Plan runner."}
    from ..scanners.spy_strategies import api_weekly

    qs = urlencode({"symbol": str(symbol or "").upper(), "expiry": expiry or ""})
    path = f"/spy/weekly?{qs}"
    with app.app_context():
        with app.test_request_context(path):
            rv = api_weekly()
            status = 200
            if isinstance(rv, tuple):
                if len(rv) > 1 and isinstance(rv[1], int):
                    status = rv[1]
                rv = rv[0]
            data = rv.get_json(silent=True) if hasattr(rv, "get_json") else rv
            if not isinstance(data, dict):
                return {"ok": False, "status": status, "error": "Weekly Plan did not return JSON data."}
            if status >= 400 or data.get("error"):
                return {"ok": False, "status": status, "error": data.get("error") or f"Weekly Plan returned HTTP {status}", "raw": data}
            data["ok"] = True
            data["source_path"] = path
            return data


def _weekly_plan_payload_with_timeout(symbol: str, expiry: Optional[str], timeout_sec: float = WEEKLY_PLAN_TIMEOUT_SEC) -> Dict[str, Any]:
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except Exception as exc:
        return {"ok": False, "error": f"Weekly Plan is only available inside the Flask app context: {exc}"}

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aihub_weekly_plan")
    future = executor.submit(_run_weekly_plan_direct, symbol, expiry, app)
    try:
        return future.result(timeout=max(3.0, float(timeout_sec or WEEKLY_PLAN_TIMEOUT_SEC)))
    except FutureTimeoutError:
        return {
            "ok": False,
            "timeout": True,
            "error": f"Weekly Plan exceeded the {timeout_sec:g}s AI Hub time budget. Returned no-trade instead of leaving the chat request hanging.",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)


def _answer_weekly_best_strategy(params: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(params.get("symbol") or "").upper().strip()
    if not symbol:
        return {"ok": False, "intent": "weekly_plan_strategy", "params": params, "answer": "I need a ticker symbol to run the Weekly Plan.", "items": []}

    should, reason = _should_use_weekly_plan(params)
    if not should and not _weekly_plan_requested(params.get("question") or ""):
        reason = reason or "generic strategy search is appropriate"
    plan = _weekly_plan_payload_with_timeout(symbol, params.get("expiry"), WEEKLY_PLAN_TIMEOUT_SEC)
    if not plan.get("ok"):
        msg = plan.get("error") or "Weekly Plan data was unavailable."
        answer = (
            f"NO TRADE: I routed {symbol} to the Weekly Plan path ({reason}), but the plan did not complete from the underlying data. "
            f"Reason: {msg} Use the Weekly Plan tab or refresh live market/option data, then ask again."
        )
        return {
            "ok": True,
            "intent": "weekly_plan_strategy",
            "params": params,
            "answer": answer,
            "items": [],
            "all_candidates": [],
            "errors": [plan],
            "evidence": [
                {"label": "Router", "value": reason, "source": "AI Hub weekly-plan router"},
                {"label": "Completion guard", "value": msg, "source": "AI Hub timeout/no-guess rule"},
            ],
            "rules_used": ["Weekly Plan router", "No-guess timeout guard"],
        }

    candidates = [_normalise_weekly_strategy(s, plan) for s in (plan.get("strategies") or [])]
    candidates.sort(key=_weekly_strategy_rank, reverse=True)
    viable = [c for c in candidates if not c.get("diagnostic_only") and c.get("recommendation") in {"OPEN", "OPEN_SMALL", "WATCH"}]
    shown = viable[:5] if viable else candidates[:5]

    exp = plan.get("expiry") or params.get("expiry")
    dte = plan.get("dte")
    expected = plan.get("expected_move") or {}
    walls = plan.get("walls") or {}
    plan_score = plan.get("weekly_plan_score") or {}
    pref = plan_score.get("preferred") or {}
    route_line = f"Used Weekly Plan because {reason}; this avoids the slower five-structure exact-chain loop for weekly SPY/liquid-symbol planning."

    if viable:
        best = viable[0]
        t = best.get("trade") or {}
        cd = t.get("credit") if t.get("credit") is not None else t.get("debit")
        lines = [
            f"BEST WEEKLY PLAN STRATEGY: {symbol} {best.get('strategy_name')} expiring {exp} scored {best.get('confidence')}/100 with recommendation {best.get('recommendation')}.",
            route_line,
            f"Action: {best.get('action')}",
            f"Structure: {t.get('legs') or 'see Weekly Plan legs'}. Entry: {t.get('entry') or cd}; POP {t.get('pop')}; RR {t.get('rr')}; max profit {t.get('max_profit')}; max loss {t.get('max_loss')}.",
            f"Regime: Weekly bias {plan.get('bias')} with score {plan.get('score')} and confidence {plan.get('confidence')}/100. Preferred approach from plan: {pref.get('label') or pref.get('name') or 'n/a'}.",
            f"Levels/context: spot {plan.get('spot')}, expected move {expected.get('display') or expected.get('move')}, support {walls.get('support')}, resistance {walls.get('resistance')}, gamma wall {walls.get('gamma_wall')}, max pain {plan.get('max_pain')}, PCR {plan.get('pcr')}, IV rank {plan.get('iv_rank')}, futures {plan.get('futures_bias')}.",
            f"Rationale: {best.get('rationale') or t.get('edge') or 'Weekly Plan selected this as the highest-ranked viable structure.'}",
        ]
        if len(viable) > 1:
            lines.append("Other viable weekly candidates: " + "; ".join(_format_weekly_candidate(x) for x in viable[1:4]))
    else:
        lines = [
            f"NO TRADE: The Weekly Plan ran for {symbol} expiring {exp}, but no viable OPEN/OPEN_SMALL/WATCH strategy was produced from the plan candidates.",
            route_line,
            "Action: Do not force a new weekly trade until the Weekly Plan produces a viable structure or the market/option context improves.",
        ]
        if candidates:
            lines.append("Top diagnostic candidate: " + _format_weekly_candidate(candidates[0]))

    evidence = [
        {"label": "Router", "value": reason, "source": "AI Hub weekly-plan router"},
        {"label": "Weekly Plan", "value": f"{symbol} {exp} DTE {dte}", "source": "Weekly Plan engine"},
        {"label": "Expected move", "value": expected.get("display") or expected.get("move"), "source": expected.get("source") or "Weekly Plan"},
        {"label": "Walls", "value": f"support {walls.get('support')} / resistance {walls.get('resistance')} / gamma {walls.get('gamma_wall')}", "source": "OI/GEX wall engine"},
        {"label": "IV/PCR", "value": f"IV rank {plan.get('iv_rank')} / PCR {plan.get('pcr')}", "source": "Weekly Plan options context"},
    ]
    return {
        "ok": True,
        "intent": "weekly_plan_strategy",
        "params": params,
        "answer": "\n".join(lines),
        "items": shown,
        "all_candidates": candidates,
        "weekly_plan": plan,
        "evidence": evidence,
        "rules_used": ["Weekly Plan", "UAE-style timeframe alignment", "OI/GEX walls", "PCR and IV context", "Futures OI context", "No-guess timeout guard"],
    }


def _strategy_sort_key(row: Dict[str, Any]) -> Tuple[int, int, int, float, float, str]:
    rec_rank = {"OPEN": 2, "OPEN_SMALL": 1, "AVOID": 0}
    trade = row.get("trade") or {}
    rr = _safe_float(trade.get("rr"), 0.0) or 0.0
    short_oi = _safe_float(trade.get("short_oi"), 0.0) or 0.0
    return (
        rec_rank.get(str(row.get("recommendation") or "").upper(), 0),
        _safe_int(row.get("confidence"), 0),
        _safe_int(row.get("uae_score"), 0),
        rr,
        short_oi,
        str(row.get("trade_type") or ""),
    )


def _format_strategy_row(row: Dict[str, Any]) -> str:
    trade = row.get("trade") or {}
    credit = trade.get("credit") if trade.get("credit") is not None else trade.get("debit")
    return (
        f"{row.get('trade_type')} {trade.get('legs') or ''} "
        f"score {row.get('confidence')}/100, recommendation {row.get('recommendation')}, "
        f"credit/debit {credit}, RR {trade.get('rr')}, max loss {trade.get('max_loss')}"
    )




def _run_with_timeout(label: str, timeout_seconds: float, func):
    """Run a callable with a hard response-time budget for AI Hub requests."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aihub-fast")
    future = executor.submit(func)
    try:
        return future.result(timeout=max(1.0, float(timeout_seconds or 1.0))), None
    except FutureTimeoutError:
        try:
            future.cancel()
        except Exception:
            pass
        return None, f"{label} timed out after {timeout_seconds:g}s"
    except Exception as exc:
        return None, f"{label} failed: {str(exc)[:220]}"
    finally:
        # Do not block the request waiting on a slow yfinance/network thread.
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def _db_latest_close(symbol: str) -> Optional[float]:
    sym = (symbol or "").upper().strip()
    if not sym:
        return None
    con = _conn()
    try:
        row = con.execute(
            "SELECT close FROM price_cache WHERE symbol=? AND close>0 ORDER BY date DESC LIMIT 1",
            (sym,),
        ).fetchone()
        if row:
            return _safe_float(row["close"], None, 2)
    except Exception:
        pass
    finally:
        con.close()
    return None


def _spot_fast(symbol: str, timeout_seconds: float = 3.0) -> Tuple[Optional[float], str]:
    spot = _db_latest_close(symbol)
    if spot:
        return spot, "price_cache"

    def _fetch():
        from ..services.market import get_spot_snapshot
        snap = get_spot_snapshot(symbol)
        if isinstance(snap, dict):
            return _safe_float(snap.get("price"), None, 2), snap.get("source") or "spot_snapshot"
        return None, "spot_snapshot"

    res, err = _run_with_timeout("spot lookup", timeout_seconds, _fetch)
    if isinstance(res, tuple) and res[0]:
        return res[0], res[1]
    return None, err or "no cached spot"


def _weekly_plan_preferred(params: Dict[str, Any], expiry_info: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict[str, Any], str]:
    sym = str(params.get("symbol") or "").upper().strip()
    if not sym:
        return False, {}, "no symbol"
    # Manual structures still go through exact trade review.  The fast path is only
    # for unconstrained "best strategy" questions.
    if params.get("trade_type") or params.get("strikes"):
        return False, {}, "manual trade request"
    info = expiry_info or _resolve_expiry(sym, params.get("expiry"), params.get("dte") or 7)
    dte = _safe_int(info.get("dte"), 999)
    weekly_symbols = set(FAST_WEEKLY_PLAN_SYMBOLS) | set(CORE_WEEKLY_PLAN_SYMBOLS) | set(LIQUID_WEEKLY_PLAN_SYMBOLS)
    max_dte = max(_safe_int(WEEKLY_PLAN_MAX_DTE, 10), 10)
    if dte < 0 or dte > max_dte:
        return False, info, f"DTE {dte} is outside weekly-plan window"
    if sym not in weekly_symbols:
        return False, info, "symbol is not in liquid weekly-plan universe"
    return True, info, f"{sym} {dte} DTE uses Weekly Plan"


def _invoke_weekly_plan(symbol: str, expiry: Optional[str], timeout_seconds: float) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    sym = (symbol or "").upper().strip()
    exp = expiry or ""
    try:
        app = current_app._get_current_object()
    except Exception as exc:
        return None, f"No Flask app context for Weekly Plan: {exc}"

    def _call_weekly():
        from ..scanners.spy_strategies import api_weekly
        qs = urlencode({"symbol": sym, "expiry": exp})
        with app.test_request_context("/spy/weekly?" + qs):
            rv = api_weekly()
            status = 200
            resp = rv
            if isinstance(rv, tuple):
                resp = rv[0]
                try:
                    status = int(rv[1])
                except Exception:
                    status = 200
            data = None
            if hasattr(resp, "get_json"):
                data = resp.get_json(silent=True)
            if data is None and hasattr(resp, "get_data"):
                data = json.loads(resp.get_data(as_text=True) or "{}")
            if status >= 400 or not isinstance(data, dict) or data.get("error"):
                raise RuntimeError((data or {}).get("error") or f"Weekly Plan HTTP {status}")
            return data

    data, err = _run_with_timeout("Weekly Plan", timeout_seconds, _call_weekly)
    if isinstance(data, dict):
        return data, None
    return None, err


def _to_float_from_money(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return _safe_float(value, default)
    txt = str(value)
    m = re.search(r"-?\d+(?:\.\d+)?", txt.replace(",", ""))
    return _safe_float(m.group(0), default) if m else default


def _weekly_strategy_code(label: str) -> str:
    l = (label or "").lower()
    if "bull put" in l:
        return "PS"
    if "bear call" in l:
        return "CS"
    if "iron condor" in l:
        return "IC"
    if "bull call" in l or "call debit" in l or "call spread" in l:
        return "CALL"
    if "bear put" in l or "put debit" in l or "put spread" in l:
        return "PUT"
    if "calendar" in l:
        return "CAL"
    return "WEEKLY"



def _rank_weekly_strategy(plan: Dict[str, Any], strat: Dict[str, Any]) -> Tuple[int, float, float, float]:
    typ = str(strat.get("type") or strat.get("name") or "")
    edge_txt = str(strat.get("edge") or "") + " " + str(strat.get("rationale") or "")
    if "not recommended" in typ.lower() or "❌" in edge_txt or strat.get("pricing_missing"):
        eligible = 0
    else:
        eligible = 1
    pref = ((plan.get("weekly_plan_score") or {}).get("preferred") or {})
    pref_label = str(pref.get("label") or "").lower()
    pref_name = str(pref.get("name") or "").upper()
    code = _weekly_strategy_code(typ)
    pref_bonus = 20 if ((pref_label and pref_label in typ.lower()) or (pref_name and pref_name == code)) else 0
    score_map = _weekly_approach_scores(plan)
    score = _safe_float(strat.get("confidence"), None)
    if score is None:
        score = _safe_float(strat.get("score"), None)
    if score is None:
        score = score_map.get(code)
    if score is None:
        score = _safe_float(pref.get("score"), None)
    if score is None:
        score = _safe_float((plan.get("weekly_plan_score") or {}).get("composite_score"), 50.0)
    pop = _safe_float(strat.get("pop"), 0.0) or 0.0
    max_profit = _to_float_from_money(strat.get("max_profit"), 0.0) or 0.0
    max_loss = _to_float_from_money(strat.get("max_loss"), 0.0) or 0.0
    rr = _safe_float(strat.get("rr"), None)
    if rr is None:
        rr = max_profit / max(max_loss, 1.0)
    return eligible, float(score or 0) + pref_bonus, float(rr or 0), pop


def _select_weekly_strategy(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    strategies = [s for s in (plan.get("strategies") or []) if isinstance(s, dict)]
    if not strategies:
        return None
    # Prefer explicitly scored local/weekly candidates over a preferred label that
    # may point to a weak diagnostic structure.
    return sorted(strategies, key=lambda s: _rank_weekly_strategy(plan, s), reverse=True)[0]


def _weekly_recommendation(plan: Dict[str, Any], strat: Optional[Dict[str, Any]]) -> Tuple[str, str, int]:
    if not strat:
        return "NO_TRADE", "No strategy candidates were produced by the Weekly Plan.", 0
    typ = str(strat.get("type") or strat.get("name") or "")
    edge_txt = str(strat.get("edge") or "") + " " + str(strat.get("rationale") or "")
    pref = ((plan.get("weekly_plan_score") or {}).get("preferred") or {})
    code = _weekly_strategy_code(typ)
    score_map = _weekly_approach_scores(plan)
    score = _safe_float(strat.get("confidence"), None)
    if score is None:
        score = _safe_float(strat.get("score"), None)
    if score is None:
        score = score_map.get(code)
    if score is None:
        score = _safe_float(pref.get("score"), None)
    if score is None:
        score = _safe_float((plan.get("weekly_plan_score") or {}).get("composite_score"), None)
    if score is None:
        score = _safe_float(plan.get("confidence"), 50.0)
    conf = int(max(0, min(100, round(float(score or 0)))))
    if "not recommended" in typ.lower() or "❌" in edge_txt:
        return "NO_TRADE", "Weekly Plan marks the top structure as not recommended.", conf
    if strat.get("pricing_missing"):
        return "WATCH", "Best structure identified, but local DB pricing is incomplete; do not enter until live bid/ask confirms credit and spread width.", min(conf, 57)
    rr = _safe_float(strat.get("rr"), None)
    if rr is None:
        max_profit = _to_float_from_money(strat.get("max_profit"), 0.0) or 0.0
        max_loss = _to_float_from_money(strat.get("max_loss"), 0.0) or 0.0
        rr = max_profit / max(max_loss, 1.0)
    # A weekly credit spread/IC with almost no premium should not be called OPEN,
    # but it can remain the best watch candidate rather than hiding the structure.
    if rr is not None and rr < 0.04:
        return "WATCH", "Best structure is identified, but the current credit/R:R is too thin; wait for better premium or closer trigger.", min(conf, 57)
    if conf >= 70:
        return "OPEN", "Eligible only if live bid/ask fill remains close to the plan and entry timeframe confirms.", conf
    if conf >= 58:
        return "OPEN_SMALL", "Use small size or wait for one more confirming 2H/4H close because the weekly score is not strong enough for full size.", conf
    return "NO_TRADE", "Do not open a new weekly trade now; the weekly plan score is below the entry threshold.", conf


def _format_weekly_strategy_result(params: Dict[str, Any], plan: Dict[str, Any], source_note: str, fallback_note: Optional[str] = None) -> Dict[str, Any]:
    sym = str(plan.get("symbol") or params.get("symbol") or "").upper()
    strat = _select_weekly_strategy(plan)
    rec, action, conf = _weekly_recommendation(plan, strat)
    exp = plan.get("expiry") or (params.get("expiry") or "")
    dte = _safe_int(plan.get("dte"), _safe_int((params or {}).get("dte"), 0))
    spot = _safe_float(plan.get("spot"), None, 2)
    weekly_score = plan.get("weekly_plan_score") or {}
    preferred = weekly_score.get("preferred") or {}
    outlook = plan.get("week_outlook") or {}
    expected = plan.get("expected_move") or {}
    walls = plan.get("walls") or {}
    typ = str((strat or {}).get("type") or "No qualified setup")
    code = _weekly_strategy_code(typ)
    entry = (strat or {}).get("entry")
    legs = (strat or {}).get("strikes") or _weekly_legs_text(strat or {}) or ""
    rationale = (strat or {}).get("rationale") or ""
    edge = (strat or {}).get("edge") or ""
    earnings_ctx = _cached_earnings_context(sym, exp, dte)
    if earnings_ctx.get("earnings_conflict"):
        rec = "NO_TRADE"
        action = "Avoid this weekly/timeframe because it would hold through earnings; wait until after the report or select an expiry before earnings."
        conf = min(conf, 45)

    if rec == "NO_TRADE":
        lead = f"NO TRADE: Weekly Plan evaluated {sym} for {exp} ({dte} DTE) and did not find an eligible setup."
    elif rec == "WATCH":
        lead = f"BEST WEEKLY CANDIDATE: {sym} {typ} {legs} expiring {exp} scored {conf}/100, but recommendation is WATCH until pricing/trigger improves."
    else:
        lead = f"BEST WEEKLY STRATEGY: {sym} {typ} {legs} expiring {exp} scored {conf}/100 with recommendation {rec}."

    lines = [
        lead,
        f"Why weekly plan: requested expiry is {dte} DTE for a liquid weekly underlying, so AI Hub used {source_note} instead of the slower generic all-strategy loop.",
        f"Action: {action}",
        f"Entry/risk: {entry or 'n/a'}; max profit {(strat or {}).get('max_profit')}; max loss {(strat or {}).get('max_loss')}; PoP {(strat or {}).get('pop')}.",
        f"Regime/score: bias {plan.get('bias')}; confidence {plan.get('confidence')}; preferred approach {preferred.get('label') or typ} score {preferred.get('score', conf)}; composite {weekly_score.get('composite_score')}.",
        f"Levels: spot {spot}; expected move {expected.get('display') or expected.get('move')}; suggested support {outlook.get('support') or walls.get('support')}; suggested resistance {outlook.get('resistance') or walls.get('resistance')}; max pain {plan.get('max_pain')}; PCR {plan.get('pcr')}.",
        f"Earnings: {earnings_ctx.get('earnings_note')}",
    ]
    if walls.get("aggregate_summary"):
        lines.append(f"Aggregate OI walls: {walls.get('aggregate_summary')}.")
    price_action = plan.get("price_action") or {}
    if price_action.get("summary"):
        lines.append(f"Price action: {price_action.get('summary')}")
    if plan.get("futures_note"):
        lines.append(f"Futures/OI context: {str(plan.get('futures_note')).rstrip('.')}.")
    if edge or rationale:
        lines.append(f"Rationale: {edge}. {rationale}".strip())
    if fallback_note:
        lines.append(f"Data note: {fallback_note}")

    item = {
        "ok": rec != "NO_TRADE",
        "symbol": sym,
        "trade_type": code,
        "trade_type_label": typ,
        "direction": "bull" if code in {"PS", "CALL"} else "bear" if code in {"CS", "PUT"} else "neutral",
        "expiry": exp,
        "dte": dte,
        "spot": spot,
        "confidence": conf,
        "score": conf,
        "recommendation": rec,
        "action": action,
        "rationale": " ".join([str(edge or ""), str(rationale or "")]).strip(),
        "trade": {
            "trade_type": code,
            "label": typ,
            "legs": legs,
            "entry": entry,
            "max_profit": (strat or {}).get("max_profit"),
            "max_loss": (strat or {}).get("max_loss"),
            "pop": (strat or {}).get("pop"),
            "target": (strat or {}).get("target"),
            "raw": strat or {},
        },
        "timeframe": _dte_bucket(dte),
        "suggested_timeframe": _dte_bucket(dte),
        "earnings": earnings_ctx,
        "earnings_conflict": bool(earnings_ctx.get("earnings_conflict")),
        "metrics": {"weekly_plan": plan, "source": source_note, "earnings": earnings_ctx},
        "evidence": [
            {"label": "Weekly Plan", "value": f"{sym} {exp} {dte} DTE", "source": source_note},
            {"label": "Preferred approach", "value": preferred.get("label") or typ, "source": "Weekly Plan composite scoring"},
            {"label": "OI walls", "value": f"support {outlook.get('support') or walls.get('support')} / resistance {outlook.get('resistance') or walls.get('resistance')}", "source": walls.get("source") or "Local option OI snapshots"},
            {"label": "Aggregate walls", "value": walls.get("aggregate_summary"), "source": "Cumulative weekly option OI"},
            {"label": "Price action", "value": (plan.get("price_action") or {}).get("summary"), "source": "price_cache BB/Keltner"},
            {"label": "Earnings", "value": earnings_ctx.get("earnings_note"), "source": "earnings_calendar cache"},
        ],
    }
    all_items = [item]
    try:
        for cand in sorted([x for x in (plan.get("strategies") or []) if isinstance(x, dict)], key=lambda x: _rank_weekly_strategy(plan, x), reverse=True):
            if cand is strat:
                continue
            ctyp = str(cand.get("type") or cand.get("name") or "Weekly candidate")
            ccode = _weekly_strategy_code(ctyp)
            cscore = _safe_float(cand.get("confidence"), _safe_float(cand.get("score"), conf))
            all_items.append({
                "ok": False,
                "symbol": sym,
                "trade_type": ccode,
                "trade_type_label": ctyp,
                "direction": "bull" if ccode in {"PS", "CALL"} else "bear" if ccode in {"CS", "PUT"} else "neutral",
                "expiry": exp,
                "dte": dte,
                "spot": spot,
                "confidence": cscore,
                "score": cscore,
                "recommendation": "ALTERNATE",
                "action": "Alternate weekly candidate; compare only if the preferred candidate does not fit your trigger.",
                "rationale": " ".join([str(cand.get("edge") or ""), str(cand.get("rationale") or "")]).strip(),
                "trade": {
                    "trade_type": ccode,
                    "label": ctyp,
                    "legs": cand.get("strikes") or _weekly_legs_text(cand),
                    "entry": cand.get("entry"),
                    "max_profit": cand.get("max_profit"),
                    "max_loss": cand.get("max_loss"),
                    "pop": cand.get("pop"),
                    "target": cand.get("target"),
                    "raw": cand,
                },
                "timeframe": _dte_bucket(dte),
                "suggested_timeframe": _dte_bucket(dte),
                "earnings": earnings_ctx,
                "earnings_conflict": bool(earnings_ctx.get("earnings_conflict")),
                "metrics": {"weekly_plan": plan, "source": source_note, "earnings": earnings_ctx},
            })
    except Exception:
        all_items = [item]

    if len(all_items) > 1:
        alt_bits = []
        for alt in all_items[1:4]:
            tr = alt.get("trade") or {}
            alt_bits.append(f"{alt.get('trade_type_label')} {tr.get('legs')} score {alt.get('confidence')}")
        if alt_bits:
            lines.append("Other weekly candidates: " + "; ".join(alt_bits))

    return {
        "ok": True,
        "intent": "best_strategy",
        "params": params,
        "answer": "\n".join(lines),
        "items": [item],
        "all_candidates": all_items,
        "weekly_plan": plan,
        "evidence": item["evidence"],
        "rules_used": ["Weekly Plan", "Cumulative weekly OI walls", "PCR", "expected move", "BB/Keltner price action", "futures OI", "multi-timeframe confirmation"],
    }



def _latest_option_rows(symbol: str, expiry: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Return the latest local option-chain snapshot for one symbol/expiry.

    The query is deliberately DB-first and tolerant of older schemas.  When bid,
    ask, last, IV or underlying columns exist they are included; otherwise those
    fields are returned as None so downstream logic can still use OI/max-pain.
    """
    sym = (symbol or "").upper().strip()
    con = _conn()
    try:
        drow = con.execute(
            "SELECT MAX(date) AS d FROM options WHERE symbol=? AND expiration=?",
            (sym, expiry),
        ).fetchone()
        latest = drow["d"] if drow else None
        if not latest:
            return [], None
        try:
            cols = {str(r[1]).lower() for r in con.execute("PRAGMA table_info(options)").fetchall()}
        except Exception:
            cols = set()
        def col_expr(name: str, expr: str) -> str:
            return expr if name.lower() in cols else f"NULL AS {name}"
        selects = [
            "type",
            "strike",
            "SUM(oi) AS oi",
            "SUM(COALESCE(volume,0)) AS volume",
            "CASE WHEN SUM(oi)>0 THEN SUM(COALESCE(price,0)*oi)/SUM(oi) ELSE AVG(price) END AS price",
            col_expr("bid", "AVG(NULLIF(bid,0)) AS bid"),
            col_expr("ask", "AVG(NULLIF(ask,0)) AS ask"),
            col_expr("last", "AVG(NULLIF(last,0)) AS last"),
            col_expr("iv", "AVG(NULLIF(iv,0)) AS iv"),
            col_expr("underlying", "AVG(NULLIF(underlying,0)) AS underlying"),
        ]
        rows = con.execute(
            f"""
            SELECT {', '.join(selects)}
            FROM options
            WHERE symbol=? AND expiration=? AND date=?
            GROUP BY type, strike
            HAVING SUM(oi)>0
            ORDER BY strike
            """,
            (sym, expiry, latest),
        ).fetchall()
        return [dict(r) for r in rows], str(latest)
    except Exception:
        return [], None
    finally:
        con.close()


def _option_mid_from_row(row: Dict[str, Any]) -> Optional[float]:
    bid = _safe_float((row or {}).get("bid"), None)
    ask = _safe_float((row or {}).get("ask"), None)
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        return round((bid + ask) / 2.0, 2)
    for key in ("price", "last"):
        px = _safe_float((row or {}).get(key), None)
        if px is not None and px > 0:
            return round(px, 2)
    return None


def _nearest_option_price(rows: Sequence[Dict[str, Any]], opt_type: str, strike: float) -> Tuple[Optional[float], Optional[float], int]:
    best = None
    best_dist = 10 ** 9
    for r in rows:
        if _norm_option_type(r.get("type")) != opt_type.lower():
            continue
        k = _safe_float(r.get("strike"), None)
        if k is None:
            continue
        dist = abs(k - float(strike))
        if dist < best_dist:
            best = r
            best_dist = dist
    if not best:
        return None, None, 0
    return _safe_float(best.get("strike"), None), _option_mid_from_row(best), _safe_int(best.get("oi"), 0)


def _local_max_pain_from_rows(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
    by_strike: Dict[float, Dict[str, float]] = {}
    for r in rows or []:
        k = _safe_float(r.get("strike"), None)
        if k is None:
            continue
        typ = "call" if str(r.get("type") or "").lower().startswith("c") else "put"
        by_strike.setdefault(k, {"call": 0.0, "put": 0.0})[typ] += _safe_float(r.get("oi"), 0.0) or 0.0
    strikes = sorted(by_strike)
    if not strikes:
        return None
    best_k, best_pain = None, None
    for settle in strikes:
        pain = 0.0
        for k, vals in by_strike.items():
            pain += vals.get("call", 0.0) * max(0.0, settle - k)
            pain += vals.get("put", 0.0) * max(0.0, k - settle)
        if best_pain is None or pain < best_pain:
            best_k, best_pain = settle, pain
    return round(float(best_k), 2) if best_k is not None else None


def _strike_interval(strikes: Sequence[float], spot: Optional[float] = None) -> float:
    vals = sorted({float(x) for x in strikes if x is not None})
    diffs = [round(vals[i + 1] - vals[i], 4) for i in range(len(vals) - 1) if vals[i + 1] > vals[i]]
    if not diffs:
        return 5.0 if (spot or 0) >= 100 else 1.0
    diffs.sort()
    # Use the lower quartile so SPY-style 5-wide structures do not become 1-wide
    # simply because weekly chains also list every dollar.
    return max(0.01, float(diffs[max(0, len(diffs) // 4)]))


def _standard_weekly_width(strikes: Sequence[float], spot: float) -> float:
    itv = _strike_interval(strikes, spot)
    # For liquid index/ETF chains, the standard weekly vertical width is usually
    # 5 points even when strikes are listed every 1 or 5 points.  Do not multiply
    # the interval by five; that made SPY fallback ICs absurdly wide.
    if spot >= 500:
        return 5.0 if itv <= 5.0 else round(max(5.0, min(itv, 25.0)), 2)
    if spot >= 100:
        return 5.0 if itv <= 5.0 else round(max(5.0, min(itv, 10.0)), 2)
    if spot >= 40:
        return 2.5 if itv <= 2.5 else round(max(2.5, min(itv, 5.0)), 2)
    return max(itv, round(spot * 0.05, 2))


def _listed_below(strikes: Sequence[float], x: float, fallback_step: float) -> float:
    vals = [k for k in sorted(strikes) if k < x]
    return round(vals[-1], 2) if vals else round(max(0.01, x - fallback_step), 2)


def _listed_above(strikes: Sequence[float], x: float, fallback_step: float) -> float:
    vals = [k for k in sorted(strikes) if k > x]
    return round(vals[0], 2) if vals else round(x + fallback_step, 2)


def _local_straddle_expected_move(rows: Sequence[Dict[str, Any]], spot: float) -> Tuple[Optional[float], str]:
    strikes = sorted({_safe_float(r.get("strike"), None) for r in rows or [] if _safe_float(r.get("strike"), None) is not None})
    if not strikes or not spot:
        return None, "no strikes"
    atm = min(strikes, key=lambda k: abs(k - spot))
    _, cmid, _ = _nearest_option_price(rows, "call", atm)
    _, pmid, _ = _nearest_option_price(rows, "put", atm)
    if cmid is not None and pmid is not None and cmid > 0 and pmid > 0:
        return round(cmid + pmid, 2), f"ATM straddle at {atm:g} from local option prices"
    return None, "ATM local option prices unavailable"


def _price_cache_move_estimate(symbol: str, spot: float, dte: int) -> Tuple[Optional[float], str]:
    sym = (symbol or "").upper().strip()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT close FROM price_cache WHERE symbol=? AND close>0 ORDER BY date DESC LIMIT 35",
            (sym,),
        ).fetchall()
        closes = [float(r["close"]) for r in rows][::-1]
    except Exception:
        closes = []
    finally:
        con.close()
    if len(closes) >= 12:
        rets = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                rets.append(math.log(closes[i] / closes[i - 1]))
        if len(rets) >= 8:
            mean = sum(rets) / len(rets)
            var = sum((x - mean) ** 2 for x in rets) / max(1, len(rets) - 1)
            daily_sigma = math.sqrt(max(0.0, var))
            move = spot * daily_sigma * math.sqrt(max(1, dte))
            if move > 0:
                return round(move, 2), "price_cache realized-vol estimate"
    # Conservative DB-only default for weekly index/liquid names.  This is not a
    # quote; it prevents deep stale OI walls from dominating when price history is
    # unavailable.
    return round(max(spot * 0.018, 1.0), 2), "fallback weekly range estimate"


def _actionable_weekly_wall(
    rows: Sequence[Dict[str, Any]],
    opt_type: str,
    spot: float,
    target: float,
    min_dist: float,
    max_dist: float,
    width: float,
) -> Optional[Dict[str, Any]]:
    side = "put" if opt_type == "put" else "call"
    pool: List[Tuple[float, float, float, Dict[str, Any]]] = []
    for r in rows or []:
        typ = _norm_option_type(r.get("type"))
        if typ != opt_type:
            continue
        k = _safe_float(r.get("strike"), None)
        oi = _safe_float(r.get("oi"), 0.0) or 0.0
        if k is None or oi <= 0:
            continue
        if side == "put" and k >= spot:
            continue
        if side == "call" and k <= spot:
            continue
        dist = abs(k - spot)
        if dist < max(width * 0.75, min_dist * 0.55):
            continue
        if dist > max_dist:
            continue
        target_quality = 1.0 / (1.0 + abs(k - target) / max(width, max_dist * 0.25, 1.0))
        oi_score = math.log1p(oi)
        mid = _option_mid_from_row(r) or 0.0
        price_score = 1.0 + min(0.35, mid / max(width, 1.0))
        score = oi_score * target_quality * price_score
        pool.append((score, oi, -abs(k - target), r))
    if not pool:
        # Last resort: closest listed OI strike to the desired weekly boundary.
        for r in rows or []:
            typ = _norm_option_type(r.get("type"))
            if typ != opt_type:
                continue
            k = _safe_float(r.get("strike"), None)
            oi = _safe_float(r.get("oi"), 0.0) or 0.0
            if k is None or oi <= 0:
                continue
            if side == "put" and k >= spot:
                continue
            if side == "call" and k <= spot:
                continue
            dist_to_target = abs(k - target)
            if dist_to_target <= max(max_dist * 0.75, width * 4):
                score = math.log1p(oi) / (1.0 + dist_to_target / max(width, 1.0))
                pool.append((score, oi, -dist_to_target, r))
    if not pool:
        return None
    best = max(pool, key=lambda x: (x[0], x[1], x[2]))[3]
    out = dict(best)
    out["mid"] = _option_mid_from_row(best)
    out["actionable_score"] = round(max(pool, key=lambda x: (x[0], x[1], x[2]))[0], 3)
    return out


def _credit_metrics(short_px: Optional[float], long_px: Optional[float], width: float) -> Tuple[float, int, int, float, bool]:
    missing = short_px is None or long_px is None
    credit = round(max(0.0, (short_px or 0.0) - (long_px or 0.0)), 2)
    max_profit = int(round(credit * 100))
    max_loss = int(round(max(0.01, width - credit) * 100))
    rr = round(credit / max(0.01, width - credit), 2) if width else 0.0
    return credit, max_profit, max_loss, rr, missing




def _norm_option_type(value: Any) -> str:
    txt = str(value or "").strip().lower()
    if txt.startswith("c"):
        return "call"
    if txt.startswith("p"):
        return "put"
    return txt


def _table_columns(con: sqlite3.Connection, table: str) -> set:
    try:
        return {str(r[1]).lower() for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _aggregate_weekly_option_rows(symbol: str, target_expiry: str, max_expiries: int = 7) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Aggregate strike-level OI from the nearest expiries through the target expiry.

    SPY/QQQ/IWM often have daily expiries.  A Friday weekly strategy should not
    look only at Friday OI; the actionable weekly walls are frequently clearer
    when Monday-through-Friday expiries are summed, matching the dashboard's
    Aggregate view.  Pricing still comes from the requested expiry; this helper
    is only for wall/PCR/max-pain context.
    """
    sym = (symbol or "").upper().strip()
    if not sym or not target_expiry:
        return [], {"source": "aggregate_weekly", "expirations": [], "latest_dates": {}}
    try:
        target_dt = date.fromisoformat(str(target_expiry)[:10])
    except Exception:
        target_dt = None
    today_s = date.today().isoformat()
    exps = []
    con = _conn()
    try:
        erows = con.execute(
            "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration",
            (sym, today_s),
        ).fetchall()
        for r in erows:
            e = str(r["expiration"])
            try:
                ed = date.fromisoformat(e[:10])
            except Exception:
                continue
            if target_dt and ed <= target_dt:
                exps.append(e)
        if not exps and target_expiry:
            exps = [target_expiry]
        exps = exps[:max(1, int(max_expiries or 7))]
        cols = _table_columns(con, "options")
        has_volume = "volume" in cols
        has_price = "price" in cols
        has_bid = "bid" in cols
        has_ask = "ask" in cols
        has_last = "last" in cols
        has_iv = "iv" in cols
        agg: Dict[Tuple[str, float], Dict[str, Any]] = {}
        latest_dates: Dict[str, str] = {}
        for exp in exps:
            drow = con.execute(
                "SELECT MAX(date) AS d FROM options WHERE symbol=? AND expiration=?",
                (sym, exp),
            ).fetchone()
            latest = drow["d"] if drow else None
            if not latest:
                continue
            latest_dates[exp] = str(latest)
            select_cols = ["type", "strike", "SUM(oi) AS oi"]
            select_cols.append("SUM(COALESCE(volume,0)) AS volume" if has_volume else "0 AS volume")
            select_cols.append("CASE WHEN SUM(oi)>0 THEN SUM(COALESCE(price,0)*oi)/SUM(oi) ELSE AVG(price) END AS price" if has_price else "NULL AS price")
            select_cols.append("AVG(NULLIF(bid,0)) AS bid" if has_bid else "NULL AS bid")
            select_cols.append("AVG(NULLIF(ask,0)) AS ask" if has_ask else "NULL AS ask")
            select_cols.append("AVG(NULLIF(last,0)) AS last" if has_last else "NULL AS last")
            select_cols.append("AVG(NULLIF(iv,0)) AS iv" if has_iv else "NULL AS iv")
            q = f"""
                SELECT {', '.join(select_cols)}
                FROM options
                WHERE symbol=? AND expiration=? AND date=?
                GROUP BY type, strike
                HAVING SUM(oi)>0
            """
            for rr in con.execute(q, (sym, exp, latest)).fetchall():
                typ = _norm_option_type(rr["type"])
                k = _safe_float(rr["strike"], None)
                if typ not in {"call", "put"} or k is None:
                    continue
                key = (typ, float(k))
                item = agg.setdefault(key, {
                    "type": typ,
                    "strike": float(k),
                    "oi": 0,
                    "volume": 0,
                    "price_num": 0.0,
                    "price_den": 0.0,
                    "bid_vals": [],
                    "ask_vals": [],
                    "last_vals": [],
                    "iv_vals": [],
                    "expirations": [],
                })
                oi = _safe_int(rr["oi"], 0)
                vol = _safe_int(rr["volume"], 0)
                item["oi"] += oi
                item["volume"] += vol
                px = _safe_float(rr["price"], None)
                if px is not None and px > 0 and oi > 0:
                    item["price_num"] += px * oi
                    item["price_den"] += oi
                for name, vals_key in [("bid", "bid_vals"), ("ask", "ask_vals"), ("last", "last_vals"), ("iv", "iv_vals")]:
                    val = _safe_float(rr[name], None) if name in rr.keys() else None
                    if val is not None and val > 0:
                        item[vals_key].append(val)
                item["expirations"].append(exp)
        out = []
        for item in agg.values():
            den = item.pop("price_den", 0.0)
            num = item.pop("price_num", 0.0)
            item["price"] = round(num / den, 4) if den > 0 else None
            for vals_key, out_key in [("bid_vals", "bid"), ("ask_vals", "ask"), ("last_vals", "last"), ("iv_vals", "iv")]:
                vals = item.pop(vals_key, [])
                item[out_key] = round(sum(vals) / len(vals), 4) if vals else None
            out.append(item)
        out.sort(key=lambda r: (_norm_option_type(r.get("type")), _safe_float(r.get("strike"), 0) or 0))
        return out, {
            "source": "aggregate_weekly",
            "expirations": exps,
            "latest_dates": latest_dates,
            "used_expiries": len(latest_dates),
            "target_expiry": target_expiry,
        }
    except Exception as exc:
        return [], {"source": "aggregate_weekly", "expirations": [], "latest_dates": {}, "error": str(exc)[:180]}
    finally:
        try:
            con.close()
        except Exception:
            pass


def _top_weekly_walls(rows: Sequence[Dict[str, Any]], opt_type: str, spot: float, limit: int = 6, max_distance: Optional[float] = None) -> List[Dict[str, Any]]:
    side = "put" if opt_type == "put" else "call"
    out = []
    for r in rows or []:
        if _norm_option_type(r.get("type")) != side:
            continue
        k = _safe_float(r.get("strike"), None)
        oi = _safe_int(r.get("oi"), 0)
        if k is None or oi <= 0:
            continue
        if side == "put" and k > spot:
            continue
        if side == "call" and k < spot:
            continue
        dist = abs(k - spot)
        if max_distance is not None and dist > max_distance:
            continue
        out.append({
            "type": side,
            "strike": round(k, 2),
            "oi": oi,
            "distance_pct": round(dist / max(spot, 0.01) * 100.0, 2),
            "volume": _safe_int(r.get("volume"), 0),
        })
    out.sort(key=lambda x: (-x["oi"], x["distance_pct"]))
    return out[:limit]


def _ema_last(values: Sequence[float], period: int) -> Optional[float]:
    vals = [float(x) for x in values if x is not None]
    if not vals:
        return None
    alpha = 2.0 / (period + 1.0)
    ema = vals[0]
    for v in vals[1:]:
        ema = alpha * v + (1.0 - alpha) * ema
    return ema


def _price_cache_rows(symbol: str, limit: int = 260) -> List[Dict[str, Any]]:
    sym = (symbol or "").upper().strip()
    con = _conn()
    try:
        cols = _table_columns(con, "price_cache")
        if not cols or "close" not in cols:
            return []
        sel = ["date"]
        for c in ["open", "high", "low", "close", "volume"]:
            if c in cols:
                sel.append(c)
            elif c in {"open", "high", "low"}:
                sel.append(f"close AS {c}")
            else:
                sel.append("0 AS volume")
        rows = con.execute(
            f"SELECT {', '.join(sel)} FROM price_cache WHERE symbol=? AND close>0 ORDER BY date DESC LIMIT ?",
            (sym, max(30, int(limit or 260))),
        ).fetchall()
        out = [dict(r) for r in rows][::-1]
        for r in out:
            for k in ["open", "high", "low", "close", "volume"]:
                r[k] = _safe_float(r.get(k), 0.0) or 0.0
        return out
    except Exception:
        return []
    finally:
        con.close()


def _weekly_bars_from_daily(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[Tuple[int, int], Dict[str, Any]] = {}
    order: List[Tuple[int, int]] = []
    for r in rows or []:
        try:
            dt = date.fromisoformat(str(r.get("date"))[:10])
        except Exception:
            continue
        key = (dt.isocalendar().year, dt.isocalendar().week)
        if key not in buckets:
            order.append(key)
            buckets[key] = {"date": dt.isoformat(), "open": r.get("open"), "high": r.get("high"), "low": r.get("low"), "close": r.get("close"), "volume": r.get("volume") or 0}
        else:
            b = buckets[key]
            b["date"] = dt.isoformat()
            b["high"] = max(_safe_float(b.get("high"), 0) or 0, _safe_float(r.get("high"), 0) or 0)
            lo = _safe_float(r.get("low"), None)
            if lo is not None and lo > 0:
                b["low"] = min(_safe_float(b.get("low"), lo) or lo, lo)
            b["close"] = r.get("close")
            b["volume"] = (_safe_float(b.get("volume"), 0) or 0) + (_safe_float(r.get("volume"), 0) or 0)
    return [buckets[k] for k in order]


def _bb_kc_profile(rows: Sequence[Dict[str, Any]], label: str) -> Dict[str, Any]:
    rows = list(rows or [])
    if len(rows) < 22:
        return {"timeframe": label, "available": False, "note": "not enough price_cache bars"}
    closes = [_safe_float(r.get("close"), 0.0) or 0.0 for r in rows]
    highs = [_safe_float(r.get("high"), closes[i]) or closes[i] for i, r in enumerate(rows)]
    lows = [_safe_float(r.get("low"), closes[i]) or closes[i] for i, r in enumerate(rows)]
    if not closes or closes[-1] <= 0:
        return {"timeframe": label, "available": False, "note": "no valid close"}
    bb_widths = []
    last = closes[-1]
    bb_mid = bb_upper = bb_lower = None
    for i in range(19, len(closes)):
        sl = closes[i-19:i+1]
        m = sum(sl) / 20.0
        sd = math.sqrt(sum((x - m) ** 2 for x in sl) / 20.0)
        up = m + 2.0 * sd
        lo = m - 2.0 * sd
        if m > 0:
            bb_widths.append((up - lo) / m * 100.0)
        if i == len(closes) - 1:
            bb_mid, bb_upper, bb_lower = m, up, lo
    trs = []
    for i in range(len(closes)):
        prev = closes[i-1] if i > 0 else closes[i]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    atr20 = sum(trs[-20:]) / 20.0 if len(trs) >= 20 else sum(trs) / max(1, len(trs))
    ema20 = _ema_last(closes, 20) or bb_mid or last
    kc_upper = ema20 + 1.5 * atr20
    kc_lower = ema20 - 1.5 * atr20
    bb_width_pct = bb_widths[-1] if bb_widths else 0.0
    bb_width_rank = round(sum(1 for w in bb_widths if w <= bb_width_pct) / max(1, len(bb_widths)) * 100.0, 1) if bb_widths else None
    bb_inside_kc = bool(bb_upper is not None and bb_lower is not None and bb_upper < kc_upper and bb_lower > kc_lower)
    kc_inside_bb = bool(bb_upper is not None and bb_lower is not None and kc_upper < bb_upper and kc_lower > bb_lower)
    bb_pct = (last - bb_lower) / max(1e-9, bb_upper - bb_lower) * 100.0 if bb_upper and bb_lower and bb_upper != bb_lower else 50.0
    prior_window = rows[-21:-1] if len(rows) >= 21 else rows[:-1]
    prior_high = max((_safe_float(r.get("high"), 0.0) or 0.0) for r in prior_window) if prior_window else None
    prior_low = min((_safe_float(r.get("low"), 0.0) or 0.0) for r in prior_window if (_safe_float(r.get("low"), 0.0) or 0.0) > 0) if prior_window else None
    squeeze_state = "squeeze_on" if bb_inside_kc else "bb_expanded_kc_inside" if kc_inside_bb else "neutral_volatility"
    near_upper = bool(bb_pct >= 70 or (prior_high and abs(last - prior_high) / max(last, 1.0) <= 0.015))
    near_lower = bool(bb_pct <= 30 or (prior_low and abs(last - prior_low) / max(last, 1.0) <= 0.015))
    return {
        "timeframe": label,
        "available": True,
        "close": round(last, 2),
        "bb_upper": round(bb_upper, 2) if bb_upper is not None else None,
        "bb_lower": round(bb_lower, 2) if bb_lower is not None else None,
        "bb_mid": round(bb_mid, 2) if bb_mid is not None else None,
        "bb_pct": round(bb_pct, 1),
        "bb_width_pct": round(bb_width_pct, 2),
        "bb_width_rank": bb_width_rank,
        "kc_upper": round(kc_upper, 2),
        "kc_lower": round(kc_lower, 2),
        "kc_inside_bb": kc_inside_bb,
        "bb_inside_kc": bb_inside_kc,
        "squeeze_state": squeeze_state,
        "prior_high": round(prior_high, 2) if prior_high else None,
        "prior_low": round(prior_low, 2) if prior_low else None,
        "near_upper": near_upper,
        "near_lower": near_lower,
        "note": f"{label}: {squeeze_state}, BB% {bb_pct:.0f}, width-rank {bb_width_rank if bb_width_rank is not None else 'n/a'}",
    }


def _weekly_price_action_context(symbol: str, spot: float) -> Dict[str, Any]:
    daily_rows = _price_cache_rows(symbol, 320)
    weekly_rows = _weekly_bars_from_daily(daily_rows)
    daily = _bb_kc_profile(daily_rows, "1D")
    weekly = _bb_kc_profile(weekly_rows, "1W")
    notes = []
    for prof in [daily, weekly]:
        if prof.get("available"):
            notes.append(prof.get("note"))
    range_score = 0.0
    call_ceiling_score = 0.0
    breakout_risk = False
    if daily.get("available"):
        if not daily.get("bb_inside_kc"):
            range_score += 5
        if daily.get("kc_inside_bb") or (daily.get("bb_width_rank") or 0) >= 45:
            range_score += 4
        if daily.get("near_upper"):
            call_ceiling_score += 5
        ph = _safe_float(daily.get("prior_high"), None)
        if ph and spot > ph * 1.003:
            breakout_risk = True
        elif ph and spot <= ph * 1.003:
            call_ceiling_score += 4
            notes.append(f"1D prior high near {ph:g}; upside call sales need strikes above that zone.")
    if weekly.get("available"):
        if weekly.get("kc_inside_bb"):
            range_score += 8
            notes.append("1W Bollinger band is expanded with Keltner inside; no active squeeze expansion signal.")
        elif not weekly.get("bb_inside_kc"):
            range_score += 4
        if weekly.get("near_upper"):
            call_ceiling_score += 5
        ph = _safe_float(weekly.get("prior_high"), None)
        if ph and spot <= ph * 1.005:
            call_ceiling_score += 3
    if breakout_risk:
        range_score -= 8
        call_ceiling_score -= 6
        notes.append("Price is breaking above the recent high; do not over-prefer call credit/IC without confirmation.")
    range_premium_ok = bool(range_score >= 8 and not breakout_risk)
    return {
        "available": bool(daily.get("available") or weekly.get("available")),
        "daily": daily,
        "weekly": weekly,
        "range_score": round(max(0.0, min(20.0, range_score)), 1),
        "call_ceiling_score": round(max(0.0, min(15.0, call_ceiling_score)), 1),
        "range_premium_ok": range_premium_ok,
        "breakout_risk": breakout_risk,
        "notes": notes[:6],
        "summary": "; ".join(notes[:3]) if notes else "Price-cache BB/KC context unavailable.",
    }


def _fast_futures_oi_context(symbol: str) -> Dict[str, Any]:
    sym = (symbol or "").upper().strip()
    roots = {"SPY": "ES", "QQQ": "NQ", "IWM": "RTY", "DIA": "YM", "SPX": "ES", "XSP": "ES"}
    root = roots.get(sym)
    if not root:
        return {"available": False, "bias": "NEUTRAL", "note": "No futures proxy mapped for this symbol."}
    con = _conn()
    try:
        rows = []
        cols = _table_columns(con, "futures_oi")
        if {"oi"}.issubset(cols) and ("contract" in cols or "symbol" in cols):
            date_col = "date" if "date" in cols else "trade_date" if "trade_date" in cols else None
            sym_col = "contract" if "contract" in cols else "symbol"
            if date_col:
                rows = con.execute(
                    f"SELECT {date_col} AS d, oi FROM futures_oi WHERE {sym_col} LIKE ? AND oi>0 ORDER BY {date_col} DESC LIMIT 6",
                    (f"{root}%",),
                ).fetchall()
        if not rows:
            cols = _table_columns(con, "futures_oi_daily")
            if {"symbol", "oi"}.issubset(cols):
                date_col = "trade_date" if "trade_date" in cols else "date" if "date" in cols else None
                if date_col:
                    rows = con.execute(
                        f"SELECT {date_col} AS d, oi FROM futures_oi_daily WHERE symbol IN (?,?) AND oi>0 ORDER BY {date_col} DESC LIMIT 6",
                        (sym, root),
                    ).fetchall()
        if len(rows) >= 2:
            latest = _safe_float(rows[0]["oi"], 0.0) or 0.0
            old = _safe_float(rows[-1]["oi"], latest) or latest
            chg = latest - old
            pct = chg / max(1.0, old) * 100.0
            if pct >= 1.0:
                bias = "BULLISH"
                note = f"{root} futures OI +{pct:.1f}% over latest {len(rows)} reads; supports put-credit side but can fight call spreads."
            elif pct <= -1.0:
                bias = "BEARISH"
                note = f"{root} futures OI {pct:.1f}% over latest {len(rows)} reads; supports call-credit side."
            else:
                bias = "NEUTRAL"
                note = f"{root} futures OI roughly flat ({pct:+.1f}%); range trades are acceptable if walls/price action agree."
            return {"available": True, "root": root, "bias": bias, "pct_change": round(pct, 2), "oi_change": round(chg, 0), "note": note}
        return {"available": False, "root": root, "bias": "NEUTRAL", "note": f"No recent {root} futures OI rows in local DB."}
    except Exception as exc:
        return {"available": False, "root": root, "bias": "NEUTRAL", "note": f"Futures OI unavailable: {str(exc)[:120]}"}
    finally:
        con.close()



def _build_local_weekly_plan_fallback(symbol: str, expiry_info: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fast DB-first weekly strategy plan using aggregate weekly OI + BB/KC context.

    For SPY/QQQ/IWM and other daily-expiry liquid names, a Friday strategy should
    use the whole weekly option map, not only the selected Friday chain.  This
    fallback therefore prices with the requested expiry but finds support and
    resistance from cumulative OI across all expiries from today through the
    requested expiry.  It also adds cached daily/weekly BB-Keltner context and
    futures OI context before ranking PS, CS and IC.
    """
    sym = (symbol or "").upper().strip()
    exp = expiry_info.get("expiry")
    dte = _safe_int(expiry_info.get("dte"), 0)
    if not exp:
        return None, "No expiry resolved for local weekly plan."

    rows, latest_date = _latest_option_rows(sym, exp)
    if not rows:
        return None, f"No local option OI snapshot found for {sym} {exp}."

    spot, spot_src = _spot_fast(sym, timeout_seconds=3.0)
    if not spot:
        under = [_safe_float(r.get("underlying"), None) for r in rows]
        under = [x for x in under if x and x > 0]
        if under:
            spot = round(sorted(under)[len(under) // 2], 2)
            spot_src = "options.underlying"
    if not spot:
        return None, f"No cached/live spot available for {sym}; {spot_src}."

    # Pricing uses the requested expiry.  Wall detection uses aggregate weekly OI.
    agg_rows, agg_meta = _aggregate_weekly_option_rows(sym, exp, max_expiries=7)
    wall_rows = agg_rows or rows
    wall_source = "aggregate weekly OI" if agg_rows else "target-expiry OI only"
    used_exps = agg_meta.get("expirations") or [exp]

    calls = [r for r in wall_rows if _norm_option_type(r.get("type")) == "call"]
    puts = [r for r in wall_rows if _norm_option_type(r.get("type")) == "put"]
    call_oi = sum(_safe_int(r.get("oi"), 0) for r in calls)
    put_oi = sum(_safe_int(r.get("oi"), 0) for r in puts)
    pcr = round(put_oi / max(1, call_oi), 2)

    strikes = sorted({_safe_float(r.get("strike"), None) for r in rows if _safe_float(r.get("strike"), None) is not None})
    if not strikes:
        return None, f"No strike-level rows found for {sym} {exp}."
    interval = _strike_interval(strikes, spot)
    width = _standard_weekly_width(strikes, spot)

    straddle_move, straddle_src = _local_straddle_expected_move(rows, spot)
    if straddle_move is None or straddle_move <= 0:
        straddle_move, straddle_src = _price_cache_move_estimate(sym, spot, dte)
    expected_move = max(float(straddle_move or 0.0), width * 1.4, spot * 0.012)

    target_max_pain = _local_max_pain_from_rows(rows)
    aggregate_max_pain = _local_max_pain_from_rows(wall_rows)
    max_pain = aggregate_max_pain or target_max_pain

    near_band = max(expected_move * 2.2, spot * 0.040, width * 6.0)
    raw_top_put_walls = _top_weekly_walls(wall_rows, "put", spot, limit=8)
    raw_top_call_walls = _top_weekly_walls(wall_rows, "call", spot, limit=8)
    top_put_walls = _top_weekly_walls(wall_rows, "put", spot, limit=8, max_distance=near_band) or raw_top_put_walls[:3]
    top_call_walls = _top_weekly_walls(wall_rows, "call", spot, limit=8, max_distance=near_band) or raw_top_call_walls[:3]
    # Recompute PCR on actionable weekly strikes when available.  This prevents
    # deep stale OI from flipping a 5-DTE SPY plan.
    action_rows = []
    for _r in wall_rows:
        _k = _safe_float(_r.get("strike"), None)
        if _k is not None and abs(_k - spot) <= near_band:
            action_rows.append(_r)
    if action_rows:
        _ac = sum(_safe_int(r.get("oi"), 0) for r in action_rows if _norm_option_type(r.get("type")) == "call")
        _ap = sum(_safe_int(r.get("oi"), 0) for r in action_rows if _norm_option_type(r.get("type")) == "put")
        if _ac + _ap > 0:
            pcr = round(_ap / max(1, _ac), 2)
    put_wall_threshold = (top_put_walls[0]["oi"] * 0.35) if top_put_walls else 0
    call_wall_threshold = (top_call_walls[0]["oi"] * 0.35) if top_call_walls else 0
    near_put_cluster = [w for w in top_put_walls if (spot - w["strike"]) <= near_band and w["oi"] >= put_wall_threshold]
    near_call_cluster = [w for w in top_call_walls if (w["strike"] - spot) <= near_band and w["oi"] >= call_wall_threshold]
    put_cluster_low = min((w["strike"] for w in near_put_cluster[:4]), default=(top_put_walls[0]["strike"] if top_put_walls else None))
    put_cluster_high = max((w["strike"] for w in near_put_cluster[:4]), default=(top_put_walls[0]["strike"] if top_put_walls else None))
    call_cluster_low = min((w["strike"] for w in near_call_cluster[:4]), default=(top_call_walls[0]["strike"] if top_call_walls else None))
    call_cluster_high = max((w["strike"] for w in near_call_cluster[:4]), default=(top_call_walls[0]["strike"] if top_call_walls else None))

    price_ctx = _weekly_price_action_context(sym, spot)
    futures_ctx = _fast_futures_oi_context(sym)
    range_ok = bool(price_ctx.get("range_premium_ok"))

    # Safe short-strike targets.  Use the OI wall cluster as a pressure zone,
    # then sell outside the zone instead of directly at a close wall.
    put_dist = max(expected_move * (1.35 if pcr >= 1.2 else 1.15), spot * 0.025, width * 3.0)
    call_dist = max(expected_move * (0.90 if range_ok else 1.10), spot * 0.015, width * 2.0)
    if pcr >= 1.7:
        put_dist = max(put_dist, expected_move * 1.55)
    if pcr <= 0.7:
        call_dist = max(call_dist, expected_move * 1.45)
    put_target = spot - put_dist
    call_target = spot + call_dist
    if put_cluster_low is not None:
        put_target = min(put_target, put_cluster_low - width * (4.0 if range_ok else 3.0))
    if call_cluster_high is not None:
        call_target = max(call_target, call_cluster_high + width)

    # Respect visible prior-high / upper-band zones from price action.  A CS just
    # above the prior high is often better than selling directly into the wall.
    for prof_key in ("daily", "weekly"):
        prof = price_ctx.get(prof_key) or {}
        ph = _safe_float(prof.get("prior_high"), None)
        if ph and ph > spot and (ph - spot) <= max(expected_move * 1.5, spot * 0.035):
            call_target = max(call_target, ph)

    max_dist = max(expected_move * 3.6, spot * 0.080, width * 8.0)
    min_dist = max(width * 0.9, spot * 0.008)
    put_wall = _actionable_weekly_wall(wall_rows, "put", spot, put_target, min_dist, max_dist, width)
    call_wall = _actionable_weekly_wall(wall_rows, "call", spot, call_target, min_dist, max_dist, width)

    def _agg_oi_at(opt_type: str, strike: Optional[float]) -> int:
        if strike is None:
            return 0
        best_oi = 0
        best_dist = 10 ** 9
        for r in wall_rows:
            if _norm_option_type(r.get("type")) != opt_type:
                continue
            k = _safe_float(r.get("strike"), None)
            if k is None:
                continue
            d = abs(k - float(strike))
            if d < best_dist:
                best_dist = d
                best_oi = _safe_int(r.get("oi"), 0)
        return best_oi

    def _short_long_from_wall(wall: Optional[Dict[str, Any]], side: str) -> Tuple[Optional[float], Optional[float], int, Optional[float]]:
        if not wall:
            return None, None, 0, None
        short = _safe_float(wall.get("strike"), None)
        if short is None:
            return None, None, 0, None
        if side == "put":
            # If the selected wall is still inside/at the visible put cluster,
            # move the short strike below the cluster for a real credit-spread edge.
            if put_cluster_low is not None and short >= put_cluster_low - width * 0.25:
                desired = min(put_target, put_cluster_low - width * 2.0)
                listed = _listed_below(strikes, desired + 0.01, width)
                if listed < short:
                    short = listed
            elif range_ok and put_cluster_low is not None and short > put_cluster_low - width * 3.5:
                # For range/premium setups, keep the put short clearly below the
                # visible support cluster rather than just one or two strikes under it.
                listed = _listed_below(strikes, put_cluster_low - width * 4.0 + 0.01, width)
                if listed < short:
                    short = listed
            long = _listed_below(strikes, short - max(interval * 0.25, 0.01), width)
            if short - long < width * 0.8:
                long = _listed_below(strikes, short - width + 0.01, width)
            oi = _agg_oi_at("put", short)
        else:
            # Sell above the visible call-wall cluster, not inside it.
            if call_cluster_high is not None and short <= call_cluster_high + width * 0.25:
                desired = max(call_target, call_cluster_high + width)
                listed = _listed_above(strikes, desired - 0.01, width)
                if listed > short:
                    short = listed
            long = _listed_above(strikes, short + max(interval * 0.25, 0.01), width)
            if long - short < width * 0.8:
                long = _listed_above(strikes, short + width - 0.01, width)
            oi = _agg_oi_at("call", short)
        return round(short, 2), round(long, 2), oi, _safe_float(wall.get("mid"), None)

    ps_short, ps_long, ps_oi, _ = _short_long_from_wall(put_wall, "put")
    cs_short, cs_long, cs_oi, _ = _short_long_from_wall(call_wall, "call")

    aggregate_summary = (
        f"Weekly cumulative walls: puts " + ", ".join(f"{w['strike']:g}P({w['oi']:,})" for w in top_put_walls[:3]) +
        "; calls " + ", ".join(f"{w['strike']:g}C({w['oi']:,})" for w in top_call_walls[:3])
    )

    futures_bias = str(futures_ctx.get("bias") or "NEUTRAL").upper()
    price_range_bonus = _safe_float(price_ctx.get("range_score"), 0.0) or 0.0
    call_ceiling_bonus = _safe_float(price_ctx.get("call_ceiling_score"), 0.0) or 0.0

    def _make_ps() -> Optional[Dict[str, Any]]:
        if ps_short is None or ps_long is None or ps_short <= ps_long:
            return None
        _, spx, soi = _nearest_option_price(rows, "put", ps_short)
        _, lpx, _ = _nearest_option_price(rows, "put", ps_long)
        w = round(abs(ps_short - ps_long), 2)
        credit, max_profit, max_loss, rr, missing = _credit_metrics(spx, lpx, w)
        dist_pct = round((spot - ps_short) / max(spot, 0.01) * 100.0, 2)
        score = 47 + min(15, math.log1p(max(ps_oi, 0))) + (7 if pcr >= 1.0 else -3) + min(13, dist_pct * 2.0)
        if futures_bias == "BULLISH":
            score += 4
        elif futures_bias == "BEARISH":
            score -= 4
        if range_ok:
            score += 2
        if rr >= 0.12:
            score += 6
        elif rr >= 0.07:
            score += 2
        elif rr <= 0.03:
            score -= 8
        if missing:
            score -= 6
        score = round(max(0, min(100, score)), 1)
        return {
            "type": "Bull Put Spread", "bias": "BULL", "code": "PS", "confidence": score,
            "strikes": f"${ps_long:g}/{ps_short:g}",
            "entry": f"Credit ${credit} (local DB)" if not missing else "Credit n/a (local DB price missing)",
            "max_profit": max_profit if not missing else None,
            "max_loss": max_loss if not missing else None,
            "pop": 68 if dist_pct >= 2.5 else 62,
            "target": f"Hold above ${ps_short:g}; put-wall cluster {put_cluster_low:g}-{put_cluster_high:g}" if put_cluster_low and put_cluster_high else f"Hold above ${ps_short:g}",
            "edge": f"Put-credit candidate outside weekly put-wall cluster; short ${ps_short:g}P, aggregate OI {ps_oi:,}, PCR {pcr}, {dist_pct}% below spot",
            "rationale": f"{aggregate_summary}. Uses {wall_source} through {exp}; pricing from {exp} snapshot {latest_date}. {price_ctx.get('summary')} {futures_ctx.get('note')}",
            "rr": rr, "pricing_missing": missing,
            "wall_source": wall_source,
        }

    def _make_cs() -> Optional[Dict[str, Any]]:
        if cs_short is None or cs_long is None or cs_long <= cs_short:
            return None
        _, spx, soi = _nearest_option_price(rows, "call", cs_short)
        _, lpx, _ = _nearest_option_price(rows, "call", cs_long)
        w = round(abs(cs_long - cs_short), 2)
        credit, max_profit, max_loss, rr, missing = _credit_metrics(spx, lpx, w)
        dist_pct = round((cs_short - spot) / max(spot, 0.01) * 100.0, 2)
        score = 48 + min(15, math.log1p(max(cs_oi, 0))) + min(12, dist_pct * 2.0) + min(12, call_ceiling_bonus)
        if pcr <= 1.1:
            score += 4
        elif pcr >= 1.7:
            score -= 2
        if futures_bias == "BEARISH":
            score += 4
        elif futures_bias == "BULLISH":
            score -= 3
        if price_ctx.get("breakout_risk"):
            score -= 10
        if rr >= 0.12:
            score += 6
        elif rr >= 0.07:
            score += 2
        elif rr <= 0.03:
            score -= 8
        if missing:
            score -= 6
        score = round(max(0, min(100, score)), 1)
        return {
            "type": "Bear Call Spread", "bias": "BEAR", "code": "CS", "confidence": score,
            "strikes": f"${cs_short:g}/{cs_long:g}",
            "entry": f"Credit ${credit} (local DB)" if not missing else "Credit n/a (local DB price missing)",
            "max_profit": max_profit if not missing else None,
            "max_loss": max_loss if not missing else None,
            "pop": 68 if dist_pct >= 1.5 else 61,
            "target": f"Stay below ${cs_short:g}; call-wall cluster {call_cluster_low:g}-{call_cluster_high:g}" if call_cluster_low and call_cluster_high else f"Stay below ${cs_short:g}",
            "edge": f"Call-credit candidate above weekly call-wall cluster; short ${cs_short:g}C, aggregate OI {cs_oi:,}, {dist_pct}% above spot",
            "rationale": f"{aggregate_summary}. BB/KC and prior-high context give call-ceiling score {call_ceiling_bonus:g}. {price_ctx.get('summary')} {futures_ctx.get('note')}",
            "rr": rr, "pricing_missing": missing,
            "wall_source": wall_source,
        }

    def _make_ic(ps: Optional[Dict[str, Any]], cs: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not ps or not cs or ps_short is None or ps_long is None or cs_short is None or cs_long is None:
            return None
        _, pshort_px, _ = _nearest_option_price(rows, "put", ps_short)
        _, plong_px, _ = _nearest_option_price(rows, "put", ps_long)
        _, cshort_px, _ = _nearest_option_price(rows, "call", cs_short)
        _, clong_px, _ = _nearest_option_price(rows, "call", cs_long)
        pw = round(abs(ps_short - ps_long), 2)
        cw = round(abs(cs_long - cs_short), 2)
        pc, _, _, _, pmiss = _credit_metrics(pshort_px, plong_px, pw)
        cc, _, _, _, cmiss = _credit_metrics(cshort_px, clong_px, cw)
        credit = round(pc + cc, 2)
        max_width = max(pw, cw)
        max_profit = int(round(credit * 100))
        max_loss = int(round(max(0.01, max_width - credit) * 100))
        rr = round(credit / max(0.01, max_width - credit), 2) if max_width else 0.0
        put_dist = (spot - ps_short) / max(spot, 0.01) * 100.0
        call_dist = (cs_short - spot) / max(spot, 0.01) * 100.0
        balanced = min(put_dist, call_dist) >= 0.8 and ps_short < spot < cs_short
        expected_inside = bool(ps_short <= spot - expected_move * 0.55 and cs_short >= spot + expected_move * 0.35)
        score = 52 + min(10, (put_dist + call_dist) * 1.25) + min(9, math.log1p(max(ps_oi, 0)) / 2.0) + min(9, math.log1p(max(cs_oi, 0)) / 2.0)
        score += min(14, price_range_bonus)
        if top_put_walls and top_call_walls:
            score += 7
        if range_ok:
            score += 5
        if expected_inside:
            score += 5
        if futures_bias == "NEUTRAL":
            score += 3
        elif futures_bias in {"BULLISH", "BEARISH"}:
            score -= 2
        if max_pain is not None and ps_short <= max_pain <= cs_short:
            score += 5
        if rr >= 0.12:
            score += 7
        elif rr >= 0.07:
            score += 3
        elif rr <= 0.03:
            score -= 8
        if not balanced:
            score -= 9
        if price_ctx.get("breakout_risk"):
            score -= 10
        if pmiss or cmiss:
            score -= 7
        score = round(max(0, min(100, score)), 1)
        return {
            "type": "Iron Condor", "bias": "NEUTRAL", "code": "IC", "confidence": score,
            "strikes": f"${ps_long:g}/${ps_short:g} | ${cs_short:g}/${cs_long:g}",
            "entry": f"Credit ${credit} (local DB)" if not (pmiss or cmiss) else "Credit n/a (local DB price missing)",
            "max_profit": max_profit if not (pmiss or cmiss) else None,
            "max_loss": max_loss if not (pmiss or cmiss) else None,
            "pop": 72 if range_ok and balanced else 65,
            "target": f"Stay between ${ps_short:g} and ${cs_short:g}",
            "edge": f"Two-sided weekly range: sell below put cluster ({put_cluster_low:g}-{put_cluster_high:g}) and above call cluster ({call_cluster_low:g}-{call_cluster_high:g}); PCR {pcr}" if put_cluster_low and call_cluster_high else f"Two-sided weekly range; PCR {pcr}",
            "rationale": f"{aggregate_summary}. Price-action overlay: {price_ctx.get('summary')} Max pain {max_pain}; target-exp max pain {target_max_pain}; aggregate max pain {aggregate_max_pain}. {futures_ctx.get('note')}",
            "rr": rr, "pricing_missing": bool(pmiss or cmiss),
            "wall_source": wall_source,
        }

    ps = _make_ps()
    cs = _make_cs()
    ic = _make_ic(ps, cs)
    strategies = [x for x in [ic, cs, ps] if x]
    if not strategies:
        return None, f"Could not build actionable local weekly candidates for {sym} {exp}."

    strategies.sort(key=lambda x: (_safe_float(x.get("confidence"), 0.0) or 0.0, _safe_float(x.get("rr"), 0.0) or 0.0), reverse=True)
    if ic and ps and cs and range_ok and not ic.get("pricing_missing"):
        best_score = _safe_float(strategies[0].get("confidence"), 0.0) or 0.0
        ic_score = _safe_float(ic.get("confidence"), 0.0) or 0.0
        if ic_score + 8 >= best_score:
            strategies = [ic] + [x for x in strategies if x is not ic]
    best = strategies[0]
    bias = "NEUTRAL" if best.get("code") == "IC" else "BULLISH" if best.get("code") == "PS" else "BEARISH"
    confidence = int(round(_safe_float(best.get("confidence"), 55.0) or 55.0))

    approaches = [{"name": s.get("code"), "label": s.get("type"), "score": s.get("confidence"), "why": s.get("edge")} for s in strategies]
    notes = [
        f"Target expiry snapshot {latest_date}; spot source {spot_src}.",
        f"Wall source: {wall_source} over {', '.join(used_exps[:7])}.",
        f"Expected move source: {straddle_src}; move ±${expected_move:.2f}.",
        aggregate_summary,
    ]
    notes.extend(price_ctx.get("notes") or [])
    notes.append(futures_ctx.get("note"))

    plan = {
        "symbol": sym,
        "expiry": exp,
        "dte": dte,
        "spot": spot,
        "date": date.today().isoformat(),
        "plan_name": "Fast Local Weekly Aggregate OI + BB/KC Plan",
        "bias": bias,
        "score": confidence,
        "confidence": confidence,
        "pcr": pcr,
        "max_pain": max_pain,
        "target_expiry_max_pain": target_max_pain,
        "aggregate_max_pain": aggregate_max_pain,
        "futures_note": futures_ctx.get("note"),
        "futures_context": futures_ctx,
        "price_action": price_ctx,
        "aggregate_oi": agg_meta,
        "expected_move": {"display": f"±${expected_move:.2f}", "move": round(expected_move, 2), "source": straddle_src},
        "walls": {
            "support": ps_short,
            "resistance": cs_short,
            "raw_put_wall": top_put_walls[0]["strike"] if top_put_walls else None,
            "raw_call_wall": top_call_walls[0]["strike"] if top_call_walls else None,
            "put_cluster_low": put_cluster_low,
            "put_cluster_high": put_cluster_high,
            "call_cluster_low": call_cluster_low,
            "call_cluster_high": call_cluster_high,
            "top_put_walls": top_put_walls,
            "top_call_walls": top_call_walls,
            "raw_top_put_walls": raw_top_put_walls,
            "raw_top_call_walls": raw_top_call_walls,
            "aggregate_summary": aggregate_summary,
            "source": wall_source,
        },
        "week_outlook": {"support": ps_short, "resistance": cs_short, "max_pain": max_pain, "range_low": ps_short, "range_high": cs_short},
        "weekly_plan_score": {
            "target_expiry": exp,
            "target_dte": dte,
            "directional_bias": bias,
            "composite_score": confidence,
            "preferred": {"name": best.get("code"), "label": best.get("type"), "score": best.get("confidence"), "why": best.get("edge")},
            "approaches": approaches,
            "component_scores": {
                "aggregate_oi_walls": 80 if top_put_walls and top_call_walls else 55,
                "price_action_bb_kc": price_ctx.get("range_score"),
                "call_ceiling": price_ctx.get("call_ceiling_score"),
                "futures_oi": 60 if futures_ctx.get("available") else 45,
            },
            "setup_notes": notes,
        },
        "strategies": strategies,
    }
    note = f"Used fast local weekly aggregate OI + BB/KC plan; wall expiries {', '.join(used_exps[:7])}; target option snapshot {latest_date}; spot source {spot_src}; expected move source {straddle_src}."
    return plan, note

def _answer_weekly_plan_strategy(params: Dict[str, Any], expiry_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    sym = str(params.get("symbol") or "").upper().strip()
    info = expiry_info or _resolve_expiry(sym, params.get("expiry"), params.get("dte") or 7)
    if not info.get("expiry"):
        return {
            "ok": False,
            "intent": "best_strategy",
            "params": params,
            "answer": f"I could not resolve a listed weekly expiry for {sym}. {info.get('note') or ''}",
            "items": [],
            "evidence": [],
        }

    # DB/local first for near-weekly liquid names.  This is faster and uses the
    # exact aggregate weekly OI view the user relies on for SPY/QQQ planning.
    fallback_plan, fallback_note = _build_local_weekly_plan_fallback(sym, info)
    if isinstance(fallback_plan, dict):
        return _format_weekly_strategy_result(params, fallback_plan, "Fast local weekly aggregate OI + BB/KC plan", fallback_note)

    plan, err = _invoke_weekly_plan(sym, info.get("expiry"), AI_HUB_WEEKLY_PLAN_TIMEOUT_SECONDS)
    if isinstance(plan, dict):
        return _format_weekly_strategy_result(params, plan, "Dashboard Weekly Plan engine", fallback_note)

    return {
        "ok": False,
        "intent": "best_strategy",
        "params": params,
        "answer": (
            f"I could not produce a weekly strategy for {sym} {info.get('expiry')} quickly from the available data. "
            f"Local aggregate plan issue: {fallback_note or 'unknown'}; Weekly Plan issue: {err or 'unknown'}. "
            "No trade suggestion was generated because AI Hub does not guess without data."
        ),
        "items": [],
        "errors": [{"local_aggregate_plan": fallback_note}, {"weekly_plan": err}],
        "evidence": [],
    }

def _answer_best_strategy(params: Dict[str, Any]) -> Dict[str, Any]:
    symbol = params.get("symbol")
    if not symbol:
        return _help_result("I need a ticker symbol to search for the best strategy.")

    # Fast path: SPY/QQQ/IWM and other liquid weekly underlyings should use the
    # dedicated Weekly Plan for near-term expiries.  This keeps AI Hub aligned
    # with the dashboard workflow and avoids the slow five-structure chain loop.
    expiry_info = _resolve_expiry(str(symbol).upper(), params.get("expiry"), params.get("dte") or 7)
    use_weekly, weekly_info, _weekly_reason = _weekly_plan_preferred(params, expiry_info)
    if use_weekly:
        return _answer_weekly_plan_strategy(params, weekly_info)

    candidates: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    started = time.time()
    per_structure_timeout = max(4.0, min(10.0, AI_HUB_STRATEGY_TIMEOUT_SECONDS / max(1, len(BEST_STRATEGY_TYPES))))
    for tt in BEST_STRATEGY_TYPES:
        remaining = AI_HUB_STRATEGY_TIMEOUT_SECONDS - (time.time() - started)
        if remaining <= 1.0:
            errors.append({"trade_type": tt, "error": "AI Hub generic strategy search time budget exhausted before this structure."})
            break
        res, timeout_err = _run_with_timeout(
            f"{str(symbol).upper()} {tt} scoring",
            min(per_structure_timeout, remaining),
            lambda tt=tt: _score_specific_trade(symbol, tt, params.get("expiry"), params.get("dte"), []),
        )
        if timeout_err:
            errors.append({"trade_type": tt, "error": timeout_err})
            continue
        res = res or {}
        if res.get("ok"):
            res["strategy_search"] = True
            res["diagnostic_only"] = res.get("recommendation") == "AVOID"
            candidates.append(res)
        else:
            errors.append({"trade_type": tt, "error": res.get("error") or "unavailable", "raw": res})

    candidates.sort(key=_strategy_sort_key, reverse=True)
    eligible = [c for c in candidates if c.get("recommendation") in {"OPEN", "OPEN_SMALL"}]
    shown = eligible if eligible else candidates[: min(5, len(candidates))]

    if not candidates:
        err = "; ".join(f"{e['trade_type']}: {e['error']}" for e in errors[:5]) or "No strategy candidates could be built."
        return {
            "ok": False,
            "intent": "best_strategy",
            "params": params,
            "answer": f"I could not build a strategy candidate for {str(symbol).upper()} from the available data. {err}",
            "items": [],
            "errors": errors,
            "evidence": [],
        }

    exp_notes = []
    for c in candidates:
        note = (c.get("expiry_info") or {}).get("note")
        if note and note not in exp_notes:
            exp_notes.append(note)

    if eligible:
        best = eligible[0]
        trade = best.get("trade") or {}
        credit = trade.get("credit") if trade.get("credit") is not None else trade.get("debit")
        lines = [
            f"BEST STRATEGY: {best.get('symbol')} {best.get('trade_type')} {trade.get('legs') or ''} expiring {best.get('expiry')} scored {best.get('confidence')}/100 with recommendation {best.get('recommendation')}.",
            f"Why this won: I evaluated {', '.join(BEST_STRATEGY_TYPES)} using the same UAE, market/sector regime, RS, price/volume, OI/GEX, and futures-alignment scoring; this was the highest eligible candidate.",
            f"Action: {best.get('action')}",
            f"Pricing/risk: spot {best.get('spot')}, credit/debit {credit}, RR {trade.get('rr')}, max loss {trade.get('max_loss')}.",
            f"Timeframe/earnings: {best.get('suggested_timeframe') or best.get('timeframe') or _dte_bucket(best.get('dte'))}; {(best.get('earnings') or {}).get('earnings_note','')}",
            f"Rationale: {best.get('rationale')}",
        ]
        if len(eligible) > 1:
            lines.append("Other eligible candidates: " + "; ".join(_format_strategy_row(x) for x in eligible[1:4]))
    else:
        best = candidates[0]
        lines = [
            f"NO TRADE: I evaluated {', '.join(BEST_STRATEGY_TYPES)} for {str(symbol).upper()} and none qualified as an OPEN or OPEN_SMALL setup under the current rules.",
            "Action: Do not open a new strategy now. Wait for the weak components to improve, or ask for a narrower manual review if you intentionally want to override the model.",
            f"Top near-miss for diagnostics only: {_format_strategy_row(best)}.",
            f"Timeframe/earnings: {best.get('suggested_timeframe') or best.get('timeframe') or _dte_bucket(best.get('dte'))}; {(best.get('earnings') or {}).get('earnings_note','')}",
            f"Why it still failed: {best.get('rationale')}",
        ]

    if exp_notes:
        lines.append("Expiry note: " + " ".join(exp_notes))
    if errors:
        lines.append("Unavailable structures: " + "; ".join(f"{e['trade_type']}: {e['error']}" for e in errors[:3]))

    evidence = []
    if candidates:
        evidence = candidates[0].get("evidence") or []
        evidence.append({"label": "Strategy search", "value": f"Evaluated {len(candidates)} structures", "source": "AI Hub best-strategy router"})

    return {
        "ok": True,
        "intent": "best_strategy",
        "params": params,
        "answer": "\n".join(lines),
        "items": shown,
        "all_candidates": candidates,
        "errors": errors,
        "evidence": evidence,
        "rules_used": ["All-strategy search", "UAE checklist", "Agentic market/sector regime", "RS vs market/sector", "Price/volume", "Option OI/GEX weighting", "Futures OI alignment"],
    }



def _answer_oi_buildup_scan(params: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run the reusable OI buildup scanner from an AI Hub natural-language request."""
    payload = payload or {}
    from ..scanners.oi_buildup_scanner import run_oi_buildup_screener, filter_oi_trend_misalignment

    st_days = _safe_int(params.get("st_days"), 3)
    mt_days = _safe_int(params.get("mt_days"), 10)
    lt_days = _safe_int(params.get("lt_days"), 30)
    watchlist_id = payload.get("watchlist_id") or params.get("watchlist_id")
    max_symbols = max(5, min(1000, _safe_int(payload.get("max_symbols") or params.get("max_symbols"), 250)))
    scan = run_oi_buildup_screener(
        st_days=st_days,
        mt_days=mt_days,
        lt_days=lt_days,
        watchlist_id=watchlist_id,
        max_symbols=max_symbols,
        save_cache=True,
    )
    intent = params.get("intent") or "oi_buildup_scan"
    if not scan.get("ok"):
        err = "; ".join(str(x) for x in (scan.get("errors") or [])) or scan.get("error") or "unknown error"
        return {
            "ok": False,
            "intent": intent,
            "params": {**params, "watchlist_id": watchlist_id, "max_symbols": max_symbols},
            "answer": f"I tried to run the OI buildup scanner, but the required local option-history data was not available: {err}",
            "items": [],
            "raw": scan,
            "evidence": [{"label": "Missing data", "value": err, "source": "options table / OI buildup scanner"}],
        }

    all_rows = list(scan.get("results") or [])
    rows = all_rows
    divergence_mode = "all"
    if intent == "oi_buildup_divergence" or _is_oi_divergence_request(str(params.get("question") or "")):
        # The user's wording "not aligned to 3 or 10" means LT can disagree
        # with either ST or MT.  If they explicitly say both, require both.
        qtxt = str(params.get("question") or "").lower()
        divergence_mode = "lt_vs_st_and_mt" if ("both" in qtxt or "st and mt" in qtxt) else "lt_vs_st_or_mt"
        rows = filter_oi_trend_misalignment(rows, mode=divergence_mode)

    requested_top = re.search(r"\btop\s+(\d{1,3})\b", str(params.get("question") or ""), re.I)
    top_n_default = 25 if not requested_top else _safe_int(requested_top.group(1), 10)
    top_n = max(1, min(100, _safe_int(params.get("top_n"), top_n_default)))
    if not requested_top:
        top_n = top_n_default
    items = rows[:top_n]
    for row in items:
        row.setdefault("score", row.get("divergence_score") if divergence_mode != "all" else abs(_safe_float(row.get("bias_score"), 0.0)) * 10)
        row.setdefault("action", row.get("suggested_action"))
        row.setdefault("recommendation", row.get("bias") or row.get("alignment_label"))
        row.setdefault("rationale", row.get("reason") or row.get("seller_thesis"))

    if divergence_mode == "lt_vs_st_and_mt":
        filter_text = f"{lt_days}d LT OI trend disagrees with both {st_days}d ST and {mt_days}d MT"
    elif divergence_mode != "all":
        filter_text = f"{lt_days}d LT OI trend disagrees with either {st_days}d ST or {mt_days}d MT"
    else:
        filter_text = "all OI buildup rows"

    if not items:
        answer = (
            f"I ran the seller-side OI buildup scanner with ST={st_days}d, MT={mt_days}d and LT={lt_days}d across "
            f"{scan.get('symbols_scanned', 0)} symbol(s). No stocks matched the requested condition: {filter_text}."
        )
    else:
        best = items[0]
        answer = (
            f"I ran the seller-side OI buildup scanner with ST={st_days}d, MT={mt_days}d and LT={lt_days}d across "
            f"{scan.get('symbols_scanned', 0)} symbol(s). Found {len(rows)} stock(s) matching: {filter_text}. Showing {len(items)}.\n"
            f"Top match: {best.get('symbol')} - {best.get('alignment_label')} | divergence score {best.get('divergence_score')}. "
            f"Seller signal: ST {best.get('st_outlook')} (OI {_safe_float(best.get('oi_st_pct'), 0.0):+.2f}%, PCRΔ {_safe_float(best.get('pcr_st_chg_pct'), 0.0):+.2f}%), "
            f"MT {best.get('mt_outlook')} (OI {_safe_float(best.get('oi_mt_pct'), 0.0):+.2f}%, PCRΔ {_safe_float(best.get('pcr_mt_chg_pct'), 0.0):+.2f}%), "
            f"LT {best.get('lt_outlook')} (OI {_safe_float(best.get('oi_lt_pct'), 0.0):+.2f}%, PCRΔ {_safe_float(best.get('pcr_lt_chg_pct'), 0.0):+.2f}%).\n"
            f"UAE timing: {best.get('uae_confirmation') or 'n/a'}; daily {best.get('uae_daily_regime') or 'n/a'} "
            f"marker {best.get('uae_daily_marker_label') or 'n/a'} age {best.get('uae_daily_marker_age') if best.get('uae_daily_marker_age') is not None else 'n/a'}; "
            f"weekly {best.get('uae_weekly_regime') or 'n/a'} marker {best.get('uae_weekly_marker_label') or 'n/a'} age {best.get('uae_weekly_marker_age') if best.get('uae_weekly_marker_age') is not None else 'n/a'}.\n"
            "Seller lens: put OI buildup with rising PCR is bullish support; call OI buildup with falling PCR is bearish resistance. UAE is used as timing confirmation using the Pine v5 priority model: MRT/fade arrow first, then trend triangle when MACD zero-cross + trending + strong histogram align. Use these as candidates for follow-up validation: price action, sector RS, UAE trend, GEX/OI walls, expiry/strike OI, and liquidity before opening a trade."
        )

    return {
        "ok": True,
        "intent": intent,
        "params": {**params, "watchlist_id": watchlist_id, "max_symbols": max_symbols, "divergence_mode": divergence_mode},
        "answer": answer,
        "items": items,
        "raw": {**scan, "results": rows[:200], "filtered_count": len(rows), "divergence_mode": divergence_mode},
        "evidence": [
            {"label": "Scanner", "value": "Seller-flow OI/PCR + skew/max-pain + UAE timing", "source": "oiapp.scanners.oi_buildup_scanner"},
            {"label": "Windows", "value": f"ST {st_days}d / MT {mt_days}d / LT {lt_days}d", "source": "Natural-language parser"},
            {"label": "Universe", "value": f"{scan.get('symbols_scanned', 0)} scanned / {scan.get('count', 0)} with OI history", "source": "options table"},
            {"label": "Filter", "value": filter_text, "source": "LT-vs-ST/MT OI alignment rule"},
            {"label": "Completed", "value": scan.get("completed_at"), "source": "Local scanner run"},
        ],
        "rules_used": [
            "Existing OI buildup scanner data path",
            "OIChangePct plus PCRChangePct over ST/MT/LT windows",
            "Historical max-pain shift from strike-level OI snapshots",
            "Skew/risk-reversal shift from historical IV or price-implied IV, falling back to labelled OI-skew proxy",
            "Seller-side PCR/OI classification and LT OI trend vs ST/MT alignment filter",
            "Strategy/timeframe suggestion with cached earnings conflict avoidance",
            "UAE Trend/Vol Analyzer context: daily/weekly regime, most recent MRT/triangle marker, and OI-vs-UAE confirmation/conflict",
        ],
    }


def _run_scanner_builder_query(query_text: str, watchlist_id: Optional[Any], benchmark: str = "SPY", limit: int = 50, max_symbols: int = 250) -> Dict[str, Any]:
    """Run a Scanner Builder DSL query directly from AI Hub."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..scanners.scanner_builder import (
        _parse_query, _expand_scan_nodes, _required_timeframes, _scan_symbol, _eval, _explain,
        _flatten_atoms, _build_scan_summary, _json_safe, _preferred_watchlist_id, _watchlist_symbols,
    )

    raw_root = _parse_query(query_text)
    root = _expand_scan_nodes(raw_root, ())
    if not watchlist_id:
        watchlist_id = _preferred_watchlist_id()
    symbols = _watchlist_symbols(watchlist_id)
    symbols = list(dict.fromkeys([str(s).upper().strip() for s in symbols if str(s).strip()]))[:max(1, min(1000, int(max_symbols or 250)))]
    if not symbols:
        return {"ok": False, "error": "No symbols found for the selected/default watchlist", "results": [], "count": 0}

    req_tfs = _required_timeframes(root)
    snapshots: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_scan_symbol, sym, root, benchmark, req_tfs): sym for sym in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                res, err = fut.result()
            except Exception as exc:
                res, err = None, str(exc)
            if res:
                snapshots.append(res)
            elif err:
                errors.append({"symbol": sym, "error": str(err)[:220]})

    passed: List[Dict[str, Any]] = []
    for row in snapshots:
        try:
            ok = bool(_eval(root, row, shift=0, tf_default="1d"))
        except Exception as exc:
            ok = False
            row.setdefault("scan_error", str(exc)[:220])
        if ok:
            row["reason"] = _explain(root, row, shift=0, tf_default="1d")
            passed.append(row)
    passed.sort(key=lambda x: (x.get("leadership") or 0, x.get("relative_strength") or 0), reverse=True)
    for row in passed:
        row.pop("timeframes", None)
        row.pop("options_history", None)
        row.pop("_uae_cache", None)
        row.setdefault("score", row.get("leadership") or row.get("relative_strength") or 0)
        row.setdefault("recommendation", "MATCH")
        row.setdefault("action", "Review scanner match; validate option-chain setup separately.")
    if limit > 0:
        passed = passed[:limit]
    return _json_safe({
        "ok": True,
        "query_text": query_text,
        "benchmark": benchmark,
        "watchlist_id": watchlist_id,
        "symbols_scanned": len(symbols),
        "count": len(passed),
        "results": passed,
        "summary": _build_scan_summary(passed),
        "errors": errors[:50],
        "clauses": _flatten_atoms(root),
        "scanned_at": _now(),
    })


def _answer_scanner_builder_query(params: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    query_text = (params.get("query_text") or "").strip()
    if not query_text:
        return _help_result("I can run Scanner Builder primitives when the question contains a generated or explicit query, for example: query: OIChangePct(30) > 5 AND RSIDiff90() > 10")
    watchlist_id = payload.get("watchlist_id") or params.get("watchlist_id")
    max_symbols = max(5, min(1000, _safe_int(payload.get("max_symbols") or params.get("max_symbols"), 250)))
    limit = max(1, min(100, _safe_int(params.get("top_n"), 25)))
    benchmark = (params.get("benchmark") or "SPY").upper()
    try:
        raw = _run_scanner_builder_query(query_text, watchlist_id=watchlist_id, benchmark=benchmark, limit=limit, max_symbols=max_symbols)
    except Exception as exc:
        return {
            "ok": False,
            "intent": "scanner_builder_query",
            "params": {**params, "watchlist_id": watchlist_id, "max_symbols": max_symbols},
            "answer": f"The Scanner Builder primitive query could not run: {exc}",
            "items": [],
            "error": str(exc),
        }
    if not raw.get("ok"):
        return {
            "ok": False,
            "intent": "scanner_builder_query",
            "params": {**params, "watchlist_id": watchlist_id, "max_symbols": max_symbols},
            "answer": f"The Scanner Builder primitive query could not run: {raw.get('error') or 'unknown error'}",
            "items": [],
            "raw": raw,
        }
    items = list(raw.get("results") or [])
    source = params.get("scanner_name") or "Scanner Builder query"
    if not items:
        answer = f"{source} ran over {raw.get('symbols_scanned', 0)} symbol(s), but no rows matched: {query_text}"
    else:
        answer = f"{source} matched {raw.get('count', 0)} row(s) from {raw.get('symbols_scanned', 0)} scanned symbol(s). Top result: {items[0].get('symbol')}. Query: {query_text}"
    return {
        "ok": True,
        "intent": "scanner_builder_query",
        "params": {**params, "watchlist_id": watchlist_id, "max_symbols": max_symbols, "query_text": query_text},
        "answer": answer,
        "items": items,
        "raw": raw,
        "evidence": [
            {"label": "Scanner", "value": source, "source": params.get("scanner_source") or "AI Hub generated/saved scanner"},
            {"label": "Query", "value": query_text, "source": "Scanner Builder DSL"},
            {"label": "Scanned", "value": raw.get("symbols_scanned", 0), "source": "watchlist symbols"},
            {"label": "Clauses", "value": "; ".join(raw.get("clauses") or [])[:240], "source": "scanner_builder parser"},
        ],
        "rules_used": ["Scanner Builder primitive parser", "Saved watchlist universe", "Indicator/OI primitives", "Boolean query evaluator"],
    }


def _answer_momentum_retests(params: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    max_symbols = max(5, min(300, _safe_int(payload.get("max_symbols") or params.get("max_symbols"), 120)))
    watchlist_id = payload.get("watchlist_id") or params.get("watchlist_id")
    symbols, universe_meta = _resolve_symbols(watchlist_id, max_symbols)
    if not symbols:
        return {"ok": False, "intent": "momentum_retests", "params": params, "answer": "No symbols were available to scan. Add symbols/watchlists or run the scheduler first.", "items": []}

    from ..scanners.momentum_retrace_scanner import run_momentum_retrace_scan
    from ..ai.journal_ai import rank_scanner_results

    raw = run_momentum_retrace_scan(symbols=symbols, workers=min(25, max(4, len(symbols) // 4 or 4)))
    rows = list(raw.get("bulls") or []) + list(raw.get("bears") or [])
    price_cap = _safe_float(params.get("price_cap"), None)
    if price_cap is not None:
        rows = [r for r in rows if _safe_float(r.get("price"), 10**9) is not None and _safe_float(r.get("price"), 10**9) <= price_cap]
    ranked = rank_scanner_results(rows)
    top_n = max(1, min(50, _safe_int(params.get("top_n"), 10)))
    items = list(ranked.get("top") or [])[:top_n]
    cap_text = f" under ${price_cap:g}" if price_cap is not None else ""
    if not items:
        answer = f"No momentum retest candidates{cap_text} passed the scanner in {universe_meta.get('watchlist_name')} ({len(symbols)} scanned)."
    else:
        top = items[0]
        answer = (
            f"Found {len(items)} top momentum retest candidate(s){cap_text} from {universe_meta.get('watchlist_name')} "
            f"({len(symbols)} scanned). Best ranked: {top.get('symbol')} {top.get('direction')} score {top.get('ai_score', top.get('score'))}. "
            "Ranking uses the scanner's native momentum score blended with the app's RS/sector/institutional/expected-move/OI edge profile."
        )
    return {
        "ok": True,
        "intent": "momentum_retests",
        "params": {**params, "watchlist_id": watchlist_id, "max_symbols": max_symbols},
        "answer": answer,
        "items": items,
        "raw": {"scan": raw, "ranked_summary": ranked.get("summary"), "universe": universe_meta},
        "evidence": [
            {"label": "Scanner", "value": "Momentum Retracement Scanner", "source": "oiapp.scanners.momentum_retrace_scanner"},
            {"label": "Universe", "value": f"{universe_meta.get('watchlist_name')} / {len(symbols)} symbols", "source": "watchlists/symbols"},
            {"label": "Ranking", "value": "Native momentum + unified edge scoring", "source": "scoring_service + journal_ai.rank_scanner_results"},
        ],
        "rules_used": ["Momentum retrace logic", "Unified scanner scoring", "RS/sector/institutional/expected-move/OI edge weighting"],
    }



def _oi_divergence_score(row: Dict[str, Any]) -> int:
    lt = str(row.get("lt_outlook") or "Neutral")
    total = 0.0
    for prefix in ["st", "mt"]:
        sig = str(row.get(f"{prefix}_outlook") or "Neutral")
        exact_mismatch = sig != lt
        bias_mismatch = row.get(f"{prefix}_bias") != row.get("lt_bias")
        if exact_mismatch:
            total += 18
        if bias_mismatch:
            total += 36
        total += min(24, abs(_safe_float(row.get("lt_oi_pct"), 0.0) - _safe_float(row.get(f"{prefix}_oi_pct"), 0.0)) * 1.2)
        total += min(12, abs(_safe_float(row.get("lt_price_pct"), 0.0) - _safe_float(row.get(f"{prefix}_price_pct"), 0.0)) * 1.1)
    # Larger current OI makes the signal more relevant, but cap the boost.
    total += min(10, math.log10(max(10, _safe_float(row.get("total_oi"), 0.0))) * 2)
    return int(max(0, min(100, round(total))))


def _annotate_oi_alignment(row: Dict[str, Any]) -> Dict[str, Any]:
    r = dict(row or {})
    lt = str(r.get("lt_outlook") or "Neutral")
    st = str(r.get("st_outlook") or "Neutral")
    mt = str(r.get("mt_outlook") or "Neutral")
    lt_bias = str(r.get("lt_bias") or "neutral")
    st_bias = str(r.get("st_bias") or "neutral")
    mt_bias = str(r.get("mt_bias") or "neutral")

    mismatches = []
    bias_mismatches = []
    if st != lt:
        mismatches.append("ST")
    if mt != lt:
        mismatches.append("MT")
    if st_bias != lt_bias:
        bias_mismatches.append("ST")
    if mt_bias != lt_bias:
        bias_mismatches.append("MT")

    score = _oi_divergence_score(r)
    if lt_bias == "bullish" and (st_bias == "bearish" or mt_bias == "bearish"):
        action = "Long-term OI is bullish but shorter OI is bearish. Wait for ST/MT realignment before opening bullish spreads, or size smaller with defined risk."
        recommendation = "WAIT_FOR_BULL_REALIGNMENT"
    elif lt_bias == "bearish" and (st_bias == "bullish" or mt_bias == "bullish"):
        action = "Long-term OI is bearish but shorter OI is bullish. Treat near-term strength as a bounce/covering risk; wait for failure before bearish spreads."
        recommendation = "WAIT_FOR_BEAR_REALIGNMENT"
    elif lt_bias == "neutral" and (st_bias != "neutral" or mt_bias != "neutral"):
        action = "Shorter horizons are moving but LT is neutral. Treat as tactical only; do not assume a durable 30-day regime."
        recommendation = "TACTICAL_ONLY"
    elif mismatches:
        action = "Exact OI/price pattern differs across horizons. Require price confirmation and avoid forcing a trade until the shorter and longer signals converge."
        recommendation = "WATCH_DIVERGENCE"
    else:
        action = "OI patterns are aligned across the requested horizons."
        recommendation = "ALIGNED"

    r.update({
        "score": score,
        "ai_score": score,
        "recommendation": recommendation,
        "action": action,
        "misaligned_to": mismatches,
        "bias_misaligned_to": bias_mismatches,
        "lt_vs_st": "bias_mismatch" if "ST" in bias_mismatches else "pattern_mismatch" if "ST" in mismatches else "aligned",
        "lt_vs_mt": "bias_mismatch" if "MT" in bias_mismatches else "pattern_mismatch" if "MT" in mismatches else "aligned",
        "rationale": (
            f"LT {r.get('lt_days')}d={lt} ({lt_bias}, OI {r.get('lt_oi_pct')}%, price {r.get('lt_price_pct')}%) vs "
            f"ST {r.get('st_days')}d={st} ({st_bias}, OI {r.get('st_oi_pct')}%, price {r.get('st_price_pct')}%) and "
            f"MT {r.get('mt_days')}d={mt} ({mt_bias}, OI {r.get('mt_oi_pct')}%, price {r.get('mt_price_pct')}%). "
            f"PCR {r.get('pcr')}, total OI {r.get('total_oi')}."
        ),
        "reason": (
            f"30d/LT signal is not aligned to {', '.join(mismatches) if mismatches else 'none'}; "
            f"bias mismatch: {', '.join(bias_mismatches) if bias_mismatches else 'none'}."
        ),
    })
    return r


def _answer_oi_buildup(params: Dict[str, Any], payload: Optional[Dict[str, Any]] = None, *, divergence_only: bool = False) -> Dict[str, Any]:
    payload = payload or {}
    from ..scanners.oi_buildup_core import run_oi_buildup_screener

    st_days = _safe_int(params.get("st_days"), 3)
    mt_days = _safe_int(params.get("mt_days"), 10)
    lt_days = _safe_int(params.get("lt_days"), 30)
    watchlist_id = payload.get("watchlist_id") or params.get("watchlist_id")
    max_symbols = max(5, min(500, _safe_int(payload.get("max_symbols") or params.get("max_symbols"), 120)))
    top_n = max(1, min(50, _safe_int(params.get("top_n"), 20)))

    scan = run_oi_buildup_screener(
        st_days=st_days,
        mt_days=mt_days,
        lt_days=lt_days,
        watchlist_id=watchlist_id,
        max_symbols=max_symbols,
        store_cache=True,
    )
    intent = "oi_buildup_divergence" if divergence_only else "oi_buildup_scan"
    if not scan.get("ok", True):
        return {
            "ok": False,
            "intent": intent,
            "params": {**params, "watchlist_id": watchlist_id, "max_symbols": max_symbols},
            "answer": f"The OI Buildup scanner could not run from the local options table: {scan.get('error') or 'unknown error'}",
            "items": [],
            "raw": scan,
        }

    rows = [_annotate_oi_alignment(r) for r in list(scan.get("results") or [])]
    if divergence_only:
        rows = [r for r in rows if r.get("misaligned_to")]
        rows.sort(key=lambda r: (r.get("score") or 0, abs(_safe_float(r.get("lt_oi_pct"), 0.0))), reverse=True)
    else:
        rows.sort(key=lambda r: abs(_safe_float(r.get("oi_1d_pct"), 0.0)), reverse=True)
    items = rows[:top_n]

    if divergence_only:
        if not items:
            answer = (
                f"Ran OI Buildup with ST={scan.get('st_days')}d, MT={scan.get('mt_days')}d, LT={scan.get('lt_days')}d. "
                f"No symbols had a 30-day/LT OI trend that differed from the ST or MT trend across {scan.get('count', 0)} scanned symbol(s)."
            )
        else:
            best = items[0]
            answer = (
                f"Ran OI Buildup with ST={scan.get('st_days')}d, MT={scan.get('mt_days')}d, LT={scan.get('lt_days')}d. "
                f"Found {len(rows)} symbol(s) where the LT/30-day OI trend is not aligned with ST or MT. "
                f"Top divergence: {best.get('symbol')} score {best.get('score')}/100; {best.get('rationale')}. "
                f"Suggested setup: {best.get('suggested_trade_label') or best.get('suggested_strategy') or 'n/a'}; "
                f"timeframe {best.get('suggested_timeframe') or 'n/a'}; earnings {best.get('earnings_date') or 'n/a'} "
                f"({best.get('earnings_days') if best.get('earnings_days') is not None else '?'}d)."
            )
    else:
        if not items:
            answer = (
                f"Ran OI Buildup with ST={scan.get('st_days')}d, MT={scan.get('mt_days')}d, LT={scan.get('lt_days')}d, "
                "but no symbols had enough local option OI history. Run/fetch options data first or choose a populated watchlist."
            )
        else:
            best = items[0]
            answer = (
                f"Ran OI Buildup with ST={scan.get('st_days')}d, MT={scan.get('mt_days')}d, LT={scan.get('lt_days')}d. "
                f"Showing {len(items)} row(s) from {scan.get('count', 0)} scanned symbol(s). "
                f"Top seller-flow row: {best.get('symbol')} {best.get('final_seller_read') or best.get('st_outlook')} "
                f"with suggested setup {best.get('suggested_trade_label') or best.get('suggested_strategy') or 'n/a'}; "
                f"timeframe {best.get('suggested_timeframe') or 'n/a'}; earnings {best.get('earnings_date') or 'n/a'} "
                f"({best.get('earnings_days') if best.get('earnings_days') is not None else '?'}d)."
            )

    return {
        "ok": True,
        "intent": intent,
        "params": {**params, "watchlist_id": watchlist_id, "max_symbols": max_symbols, "top_n": top_n},
        "answer": answer,
        "items": items,
        "raw": {**scan, "results": (scan.get("results") or [])[:200]},
        "evidence": [
            {"label": "Scanner", "value": "OI Buildup ST/MT/LT", "source": "oiapp.scanners.oi_buildup_core.run_oi_buildup_screener"},
            {"label": "Horizons", "value": f"ST {scan.get('st_days')}d / MT {scan.get('mt_days')}d / LT {scan.get('lt_days')}d", "source": "Question parser"},
            {"label": "Scanned", "value": scan.get("count", 0), "source": "Local options table"},
            {"label": "Filter", "value": "LT exact signal differs from ST or MT" if divergence_only else "No divergence filter", "source": "AI Hub alignment rule"},
        ],
        "rules_used": [
            "OI Buildup scanner",
            "ST/MT/LT horizon parser",
            "Seller-flow PCR/OI classification",
            "Max-pain shift from strike-level historical OI",
            "Skew or OI-skew proxy shift",
            "Strategy/timeframe and earnings-conflict guardrails",
            "LT-vs-ST/MT exact-pattern and bullish/bearish-bias alignment",
        ],
    }


def _builder_json_safe(sb: Any, value: Any) -> Any:
    try:
        return sb._json_safe(value)
    except Exception:
        return _sanitize(value)


def _run_scanner_builder_query(
    query_text: str,
    *,
    watchlist_id: Optional[Any] = None,
    benchmark: str = "SPY",
    max_symbols: int = 120,
    limit: int = 50,
) -> Dict[str, Any]:
    """Programmatic Scanner Builder runner for AI Hub.

    Mirrors /scanner-builder/api/run but is callable from the conversational
    router, allowing AI Hub to execute saved scanner definitions or generated
    primitive expressions without making an HTTP request.
    """
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ..scanners import scanner_builder as sb

        sb._ensure_tables()
        raw_root = sb._parse_query(query_text)
        root = sb._expand_scan_nodes(raw_root, ())
        if not watchlist_id:
            watchlist_id = sb._preferred_watchlist_id()
        symbols = list(dict.fromkeys(sb._watchlist_symbols(watchlist_id)))
        if not symbols:
            return {"ok": False, "error": "No symbols found for the selected/default watchlist.", "results": [], "count": 0}
        symbols = symbols[: max(1, min(500, int(max_symbols or 120)))]
        req_tfs = sb._required_timeframes(root)

        snapshots: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        workers = min(8, max(1, len(symbols)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(sb._scan_symbol, sym, root, (benchmark or "SPY").upper(), req_tfs): sym for sym in symbols}
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    res, err = fut.result()
                except Exception as exc:
                    res, err = None, str(exc)
                if res:
                    snapshots.append(res)
                elif err:
                    errors.append({"symbol": sym, "error": err})

        rs_vals = [r.get("relative_strength") for r in snapshots if r.get("relative_strength") is not None]
        rs_sorted = sorted(rs_vals)
        n = len(rs_sorted)
        if n:
            for r in snapshots:
                rs = r.get("relative_strength")
                if rs is None:
                    continue
                pct = sum(1 for v in rs_sorted if v <= rs) / n
                r["leadership"] = int(round(pct * 100))

        try:
            rsrank_periods = sorted(sb._collect_function_periods(root, {"rsrank", "rs_rank"}))
        except Exception:
            rsrank_periods = []
        for period in rsrank_periods:
            base_vals: List[float] = []
            for r in snapshots:
                try:
                    v = sb._relative_strength_value(r, benchmark, period, tf="1d", shift=0)
                except Exception:
                    v = None
                r[f"_rsrank_source_{period}"] = v
                if v is not None:
                    base_vals.append(v)
            base_vals_sorted = sorted(base_vals)
            if not base_vals_sorted:
                continue
            total = len(base_vals_sorted)
            for r in snapshots:
                v = r.get(f"_rsrank_source_{period}")
                if v is None:
                    continue
                pct = sum(1 for x in base_vals_sorted if x <= v) / total
                r[f"rs_rank_{period}"] = int(round(pct * 100))

        passed: List[Dict[str, Any]] = []
        for r in snapshots:
            try:
                ok = bool(sb._eval(root, r, shift=0, tf_default="1d"))
            except Exception as exc:
                ok = False
                r.setdefault("scan_error", str(exc))
            if ok:
                try:
                    r["reason"] = sb._explain(root, r, shift=0, tf_default="1d")
                except Exception:
                    r["reason"] = "Passed Scanner Builder expression."
                if _safe_float(r.get("score"), None) is None:
                    leadership = _safe_float(r.get("leadership"), 50.0) or 50.0
                    rs = _safe_float(r.get("relative_strength"), 0.0) or 0.0
                    r["score"] = round(max(0.0, min(100.0, 50.0 + (leadership - 50.0) * 0.45 + max(min(rs, 20.0), -20.0) * 0.8)), 1)
                r["recommendation"] = "REVIEW_SETUP"
                r["action"] = "Review candidate against market/sector regime, option OI walls, liquidity, earnings, and planned strategy before entry."
                passed.append(r)

        passed.sort(key=lambda x: (x.get("score") or 0, x.get("leadership") or 0, x.get("relative_strength") or 0), reverse=True)
        if limit > 0:
            passed = passed[:limit]
        try:
            summary = sb._build_scan_summary(passed)
        except Exception:
            summary = {"count": len(passed)}
        try:
            clauses = sb._flatten_atoms(root)
        except Exception:
            clauses = []

        for r in passed:
            r.pop("timeframes", None)
            r.pop("options_history", None)
            r.pop("_uae_cache", None)

        return {
            "ok": True,
            "query_text": query_text,
            "benchmark": benchmark,
            "watchlist_id": watchlist_id,
            "clauses": clauses,
            "count": len(passed),
            "results": _builder_json_safe(sb, passed),
            "summary": _builder_json_safe(sb, summary),
            "errors": errors[:50],
            "symbols": symbols,
            "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "logic": {"timeframes": req_tfs},
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "results": [], "count": 0, "trace": traceback.format_exc()[-1200:]}


def _answer_scanner_builder_query(params: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    query_text = (params.get("query_text") or "").strip()
    if not query_text:
        return _help_result("I could not translate that into a Scanner Builder query. Try naming an existing scanner or using a condition such as: scan where rsi14[1d] < 35 AND close[1d] > ema20[1d].")

    max_symbols = max(5, min(500, _safe_int(payload.get("max_symbols") or params.get("max_symbols"), 120)))
    watchlist_id = payload.get("watchlist_id") or params.get("watchlist_id")
    top_n = max(1, min(50, _safe_int(params.get("top_n"), 20)))
    benchmark = (params.get("benchmark") or "SPY").upper()
    scan = _run_scanner_builder_query(
        query_text,
        watchlist_id=watchlist_id,
        benchmark=benchmark,
        max_symbols=max_symbols,
        limit=max(top_n, 50),
    )
    if not scan.get("ok"):
        return {
            "ok": False,
            "intent": "scanner_builder_query",
            "params": {**params, "watchlist_id": watchlist_id, "max_symbols": max_symbols},
            "answer": f"I understood this as a Scanner Builder request, but the underlying scanner could not run: {scan.get('error')}",
            "items": [],
            "raw": scan,
            "evidence": [{"label": "Query", "value": query_text, "source": "AI Hub scanner planner"}],
        }

    rows = list(scan.get("results") or [])
    try:
        from ..ai.journal_ai import rank_scanner_results
        ranked = rank_scanner_results(rows)
        items = list(ranked.get("top") or rows)[:top_n]
        rank_note = ranked.get("summary")
    except Exception:
        items = rows[:top_n]
        rank_note = "Sorted by Scanner Builder score/leadership."

    scanner_name = params.get("scanner_name") or "AI Hub generated scanner query"
    if not items:
        answer = (
            f"Ran {scanner_name} over {len(scan.get('symbols') or [])} symbol(s), but no rows passed. "
            f"Query used: {query_text}"
        )
    else:
        best = items[0]
        answer = (
            f"Ran {scanner_name} over {len(scan.get('symbols') or [])} symbol(s). "
            f"{len(rows)} row(s) passed; showing top {len(items)}. Best: {best.get('symbol')} score {best.get('ai_score', best.get('score'))}. "
            f"Query used: {query_text}"
        )

    return {
        "ok": True,
        "intent": "scanner_builder_query",
        "params": {**params, "watchlist_id": watchlist_id, "max_symbols": max_symbols, "top_n": top_n},
        "answer": answer,
        "items": items,
        "raw": scan,
        "evidence": [
            {"label": "Scanner", "value": scanner_name, "source": params.get("scanner_source") or "Scanner Builder"},
            {"label": "Query", "value": query_text, "source": "Scanner Builder primitives"},
            {"label": "Scanned", "value": len(scan.get("symbols") or []), "source": "watchlist/symbol universe"},
            {"label": "Ranking", "value": rank_note, "source": "journal_ai.rank_scanner_results + Scanner Builder score"},
        ],
        "rules_used": [
            "Scanner Builder parser/evaluator",
            "Saved scanner definitions and built-in scanner catalog",
            "AI Hub phrase-to-primitive query planner",
            "Unified scanner result ranking",
        ],
    }

def _load_open_trades() -> List[Dict[str, Any]]:
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY expiry, symbol, id").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _review_open_trades(roll_only: bool = False) -> Dict[str, Any]:
    from ..journal.journal_routes import _compute_live_pnl, _build_ai_alert_analysis, _needs_ai_roll_review, _roll_candidates_for_trade
    from ..ai.journal_ai import summarize_portfolio_review

    trades = _load_open_trades()
    items: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for t in trades:
        try:
            live = _compute_live_pnl(t)
            ai = _build_ai_alert_analysis(t, live)
            score = _safe_float(live.get("trade_health_score") or live.get("probability_score") or ai.get("score"), 0.0) or 0.0
            action = str(ai.get("recommendation") or live.get("action") or live.get("trade_action") or "HOLD").upper()
            should_roll = bool(_needs_ai_roll_review(t, live)) or "ROLL" in action or "ROLL" in str(live.get("action") or "").upper()
            roll_candidates = _roll_candidates_for_trade(t, live, limit=3) if should_roll else []
            severity = "critical" if score < 35 or live.get("pnr_breached") else "high" if score < 50 or should_roll else "warning" if score < 65 else "watch"
            item = {
                "id": t.get("id"),
                "symbol": (t.get("symbol") or "").upper(),
                "trade_type": t.get("trade_type"),
                "expiry": t.get("expiry"),
                "dte": live.get("dte"),
                "spot": live.get("spot"),
                "score": round(score, 1),
                "severity": severity,
                "action": action,
                "journal_action": live.get("action"),
                "reason": ai.get("explicit_action") or ai.get("summary") or live.get("action_reason") or live.get("rec_reason") or "",
                "outlook": live.get("outlook"),
                "pnr_status": live.get("pnr_status"),
                "pnr_breached": live.get("pnr_breached"),
                "unrealised_pnl": live.get("unrealised_pnl"),
                "pct_of_max_profit": live.get("pct_of_max_profit"),
                "max_profit": live.get("max_profit"),
                "max_loss": live.get("max_loss"),
                "thesis": ai.get("thesis") or [],
                "risks": ai.get("risks") or [],
                "action_steps": ai.get("action_steps") or ai.get("next_actions") or [],
                "critical_price_alerts": ai.get("critical_price_alerts") or [],
                "should_roll": should_roll,
                "roll_candidates": roll_candidates,
                "live": live,
                "ai": ai,
            }
            if not roll_only or should_roll:
                items.append(item)
        except Exception as exc:
            errors.append({"id": str(t.get("id")), "symbol": str(t.get("symbol") or ""), "error": str(exc)[:180]})
    if roll_only:
        items.sort(key=lambda x: (0 if x.get("pnr_breached") else 1, x.get("score") or 999, x.get("dte") or 999))
    else:
        items.sort(key=lambda x: (x.get("score") or 999, 0 if x.get("pnr_breached") else 1, x.get("dte") or 999))
    portfolio = summarize_portfolio_review(items)
    return {"items": items, "errors": errors, "portfolio": portfolio, "open_count": len(trades)}


def _answer_open_trades(params: Dict[str, Any], roll_only: bool = False) -> Dict[str, Any]:
    review = _review_open_trades(roll_only=roll_only)
    items = review.get("items") or []
    if roll_only:
        if not items:
            answer = f"No open trades currently triggered the roll-review rules. I checked {review.get('open_count', 0)} open trade(s)."
        else:
            answer = f"{len(items)} open trade(s) need roll review today. Highest priority: {items[0].get('symbol')} #{items[0].get('id')} score {items[0].get('score')} with action {items[0].get('action')}."
        intent = "roll_review"
    else:
        port = review.get("portfolio") or {}
        answer = port.get("summary") or f"Reviewed {review.get('open_count', 0)} open trade(s)."
        if items:
            weak = [x for x in items if (x.get("score") or 100) < 50 or x.get("pnr_breached")]
            if weak:
                answer += f" {len(weak)} trade(s) need immediate attention; weakest is {weak[0].get('symbol')} #{weak[0].get('id')} score {weak[0].get('score')}."
        intent = "open_trades_health"
    return {
        "ok": True,
        "intent": intent,
        "params": params,
        "answer": answer,
        "items": items[:50],
        "portfolio": review.get("portfolio"),
        "errors": review.get("errors"),
        "evidence": [
            {"label": "Open trades", "value": review.get("open_count", 0), "source": "trades table"},
            {"label": "Health engine", "value": "Journal live P&L, PNR, probability score, AI alert analysis", "source": "journal_routes"},
            {"label": "Roll candidates", "value": "Generated only for trades that trigger roll review", "source": "journal roll rules"},
        ],
        "rules_used": ["Journal health score", "PNR method", "MRT profit/loss rules", "AI trade alert analysis", "Roll candidate generator"],
    }



def _answer_agentic_scan(params: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    from ..scanners.agentic_ai_scanner import run_agentic_scan

    max_symbols = max(5, min(300, _safe_int(payload.get("max_symbols") or params.get("max_symbols"), 80)))
    overrides = {
        "target_dte": _safe_int(params.get("dte"), 45),
        "max_symbols": max_symbols,
        "watchlist_id": payload.get("watchlist_id") or params.get("watchlist_id"),
        "trade_type": params.get("trade_type") or "AUTO",
    }
    # Let the underlying scanner enforce min confidence, UAE score, RR, regime alignment,
    # dedupe, persistence and alert throttling from its saved settings.
    result = run_agentic_scan(source="ai_hub", overrides=overrides)
    if not result.get("ok"):
        return {
            "ok": False,
            "intent": "agentic_scan",
            "params": {**params, **overrides},
            "answer": f"The Agentic AI scanner could not run: {result.get('error') or 'unknown error'}",
            "items": [],
            "raw": result,
        }
    findings = list(result.get("findings") or [])
    top_n = max(1, min(50, _safe_int(params.get("top_n"), 10)))
    items = findings[:top_n]
    summary = result.get("summary") or {}
    if not items:
        answer = (
            f"Agentic scan completed with no trade ideas passing the saved rules. "
            f"Scanned {summary.get('scanned', 0)} symbols; candidates {summary.get('candidates', 0)}."
        )
    else:
        best = items[0]
        answer = (
            f"Agentic scan found {len(items)} ranked trade idea(s) shown from {summary.get('scanned', 0)} scanned symbols. "
            f"Best: {best.get('symbol')} {best.get('strategy_type') or best.get('trade_type')} confidence "
            f"{best.get('confidence') or best.get('score')} with recommendation {best.get('recommendation')}. "
            f"New persisted findings this run: {summary.get('new_findings', 0)}; repeated findings: {summary.get('repeated_findings', 0)}."
        )
    return {
        "ok": True,
        "intent": "agentic_scan",
        "params": {**params, **overrides},
        "answer": answer,
        "items": items,
        "raw": result,
        "evidence": [
            {"label": "Scanner", "value": "Agentic AI scanner", "source": "oiapp.scanners.agentic_ai_scanner.run_agentic_scan"},
            {"label": "Scanned", "value": summary.get("scanned", 0), "source": "watchlist/symbol universe"},
            {"label": "New findings", "value": summary.get("new_findings", 0), "source": "agentic dedupe/persistence"},
            {"label": "Market regime", "value": (summary.get("market_context") or {}).get("bias_label") or (summary.get("market_context") or {}).get("bias"), "source": "Agentic market context"},
        ],
        "rules_used": ["Agentic confidence score", "UAE checklist", "market/sector regime", "RS vs market/sector", "price/volume", "options OI/GEX", "futures OI", "new-finding dedupe"],
    }

def _answer_agentic_findings(params: Dict[str, Any]) -> Dict[str, Any]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT * FROM agentic_ai_findings ORDER BY found_at DESC, confidence DESC LIMIT 20"
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            for key in ["strategy_json", "metrics_json", "market_context_json", "sector_context_json", "checklist_json"]:
                d[key.replace("_json", "")] = _json_loads(d.get(key), {} if key != "checklist_json" else [])
            items.append(d)
    except Exception as exc:
        return {"ok": False, "intent": "agentic_findings", "params": params, "answer": f"Agentic findings table is not available yet: {exc}", "items": []}
    finally:
        con.close()
    if not items:
        answer = "No Agentic AI scanner findings are stored yet. Run the Agentic AI scanner first."
    else:
        answer = f"Latest Agentic AI scanner findings: {len(items)} shown. Newest: {items[0].get('symbol')} {items[0].get('strategy_type')} confidence {items[0].get('confidence')}."
    return {"ok": True, "intent": "agentic_findings", "params": params, "answer": answer, "items": items, "evidence": [{"label": "Source", "value": "agentic_ai_findings", "source": "SQLite"}]}


def _help_result(prefix: str = "") -> Dict[str, Any]:
    examples = [
        "Show me top momentum retests under $50",
        "What is the best strategy for SPY for 6/26/2026?",
        "What is the best strategy for NOW expiring July 19?",
        "What is the best bull put spread for NVDA expiring July 19?",
        "Which open trades should I roll today?",
        "Go analyze all my open trades and suggest their health and appropriate actions",
        "Is it okay to open a new trade NOW PS 95/90 for 6/26/26 expiry?",
        "Run the Agentic scanner for 45 DTE and show top trade ideas",
        "Run OI buildup with ST 3 days, MT 10 days, LT 30 days and show stocks where LT OI trend is not aligned to ST or MT",
        "query: OIChangePct(30) > 5 AND RSIDiff90() > 10",
    ]
    answer = (prefix + "\n" if prefix else "") + "Ask about scanner candidates, Weekly Plan strategy selection, OI buildup scans, Scanner Builder primitive queries, exact option trades, open-trade health, roll candidates, or stored Agentic AI findings. Example: " + examples[0]
    return {"ok": True, "intent": "help", "params": {}, "answer": answer, "items": [], "examples": examples}


def answer_question(question: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    params = _classify_question(question)
    # Allow UI override fields while keeping parsed values as defaults.
    for key in ["watchlist_id", "max_symbols"]:
        if payload.get(key) not in (None, ""):
            params[key] = payload.get(key)
    intent = params.get("intent")
    params["question"] = question
    if intent in {"oi_buildup_scan", "oi_buildup_divergence"}:
        return _answer_oi_buildup_scan(params, payload)
    if intent == "scanner_builder_query":
        return _answer_scanner_builder_query(params, payload)
    if intent == "momentum_retests":
        return _answer_momentum_retests(params, payload)
    if intent == "weekly_plan_strategy":
        return _answer_weekly_plan_strategy(params)
    if intent == "best_strategy":
        return _answer_best_strategy(params)
    if intent == "specific_trade":
        return _answer_specific_trade(params)
    if intent == "roll_review":
        return _answer_open_trades(params, roll_only=True)
    if intent == "open_trades_health":
        return _answer_open_trades(params, roll_only=False)
    if intent == "agentic_scan":
        return _answer_agentic_scan(params, payload)
    if intent == "agentic_findings":
        return _answer_agentic_findings(params)
    return _help_result()


@ai_hub_bp.route("/")
def page():
    return render_template("ai_hub.html")


@ai_hub_bp.route("/api/status")
def api_status():
    return jsonify(_sanitize({
        "ok": True,
        "watchlists": _watchlists(),
        "history": _history(12),
        "examples": _help_result().get("examples"),
    }))


@ai_hub_bp.route("/api/history")
def api_history():
    limit = max(1, min(200, _safe_int(request.args.get("limit"), 50)))
    return jsonify(_sanitize({"ok": True, "items": _history(limit)}))


@ai_hub_bp.route("/api/ask", methods=["POST"])
def api_ask():
    data = request.get_json(silent=True) or {}
    question = str(data.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "Question is required."}), 400
    try:
        result = answer_question(question, data)
        result["question"] = question
        result["created_at"] = _now()
        qid = _save_query(question, result)
        result["history_id"] = qid
        return jsonify(_sanitize(result)), 200 if result.get("ok", True) else 422
    except Exception as exc:
        tb = traceback.format_exc()
        result = {
            "ok": False,
            "intent": "error",
            "question": question,
            "answer": f"The AI Hub could not complete this request from the available data/rules: {exc}",
            "error": str(exc),
            "trace": tb[-2000:],
            "items": [],
            "created_at": _now(),
        }
        try:
            result["history_id"] = _save_query(question, result)
        except Exception:
            pass
        return jsonify(_sanitize(result)), 500
