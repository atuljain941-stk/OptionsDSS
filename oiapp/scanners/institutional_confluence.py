"""Watchlist-driven institutional scanner — Phase 1.

The scanner is intentionally a pipeline, not a monolithic score.  Every
stage can be off, score-only, or a gate.  Later phases can append their own
stage dictionaries without changing the API or the results grid.
"""
from __future__ import annotations

import concurrent.futures
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, jsonify, render_template, request

from ..config import DB_PATH
from ..services.technical_snapshot import get_technical_snapshot

institutional_confluence_bp = Blueprint(
    "institutional_confluence", __name__, url_prefix="/institutional-confluence"
)

TIMEFRAMES = ("1w", "1d", "4h", "1h")
STAGE_MODES = {"off", "score", "filter"}


@dataclass(frozen=True)
class TimeframeRule:
    timeframe: str
    weight: float = 1.0


@dataclass(frozen=True)
class ConfluenceConfig:
    direction: str = "both"  # bull, bear, or both
    min_confluences: int = 3
    min_confluence_score: float = 3.0
    rules: Tuple[TimeframeRule, ...] = tuple(TimeframeRule(tf) for tf in TIMEFRAMES)


@dataclass
class ConfluenceResult:
    symbol: str
    timestamp: str
    direction: str
    confluences: int
    confluence_score: float
    timeframe_signals: Dict[str, Dict[str, Any]]
    is_confluence: bool
    confidence: float
    reasoning: str
    entry_price_zone: Tuple[Optional[float], Optional[float]]
    unavailable_timeframes: List[str]


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _watchlist_symbols(watchlist_id: int) -> List[str]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (watchlist_id,),
        ).fetchall()
        return [str(row["symbol"]).upper() for row in rows if row["symbol"]]
    finally:
        con.close()


def _latest_regime(symbol: str) -> Optional[Dict[str, Any]]:
    con = _conn()
    try:
        row = con.execute(
            "SELECT regime, confidence, bias, ema_trend, rsi_diff, adx, scan_date "
            "FROM regime_scan WHERE symbol=? ORDER BY scan_date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _safe_num(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _timeframe_signal(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a directional, explainable read from one cached snapshot.

    A direction requires both trend placement (close vs EMA13/EMA50) and
    momentum confirmation (RSIdiff90).  ADX is recorded as quality context,
    not used to erase an otherwise valid alignment.
    """
    if not snapshot:
        return {"status": "unavailable", "direction": "neutral", "aligned": False,
                "reason": "No cached snapshot"}
    close, ema13, ema50, diff, adx = (
        _safe_num(snapshot.get("close")), _safe_num(snapshot.get("ema13")),
        _safe_num(snapshot.get("ema50")), _safe_num(snapshot.get("rsidiff90")),
        _safe_num(snapshot.get("adx")),
    )
    needed = {"close": close, "ema13": ema13, "ema50": ema50, "rsidiff90": diff}
    missing = [key for key, value in needed.items() if value is None]
    if missing:
        return {"status": "unavailable", "direction": "neutral", "aligned": False,
                "reason": "Missing " + ", ".join(missing), "values": needed}
    bull = close >= ema13 >= ema50 and diff >= 0
    bear = close <= ema13 <= ema50 and diff <= 0
    direction = "bull" if bull else "bear" if bear else "neutral"
    quality = "trending" if adx is not None and adx >= 25 else "developing"
    return {
        "status": "ok", "direction": direction, "aligned": direction != "neutral",
        "quality": quality, "values": {**needed, "adx": adx},
        "reason": (
            f"{direction.title()} alignment: close/EMA13/EMA50 and RSIdiff90 {diff:+.1f}"
            if direction != "neutral" else f"Mixed trend / momentum (RSIdiff90 {diff:+.1f})"
        ),
    }


def MULTI_TIMEFRAME_CONFLUENCE(symbol: str, config: ConfluenceConfig) -> ConfluenceResult:
    signals = {tf: _timeframe_signal(get_technical_snapshot(symbol, tf)) for tf in TIMEFRAMES}
    available = {tf: s for tf, s in signals.items() if s["status"] == "ok"}
    candidates = ("bull", "bear") if config.direction == "both" else (config.direction,)
    direction = max(
        candidates,
        key=lambda side: sum(1 for signal in available.values() if signal["direction"] == side),
    )
    aligned = [tf for tf, signal in available.items() if signal["direction"] == direction]
    score = sum(rule.weight for rule in config.rules if rule.timeframe in aligned)
    daily = signals.get("1d", {}).get("values", {})
    support = _safe_num((get_technical_snapshot(symbol, "1d") or {}).get("sr_support"))
    resistance = _safe_num((get_technical_snapshot(symbol, "1d") or {}).get("sr_resistance"))
    passed = len(aligned) >= config.min_confluences and score >= config.min_confluence_score
    unavailable = [tf for tf, signal in signals.items() if signal["status"] != "ok"]
    reasoning = (
        f"{direction.title()} alignment on {len(aligned)}/{len(TIMEFRAMES)} timeframes: "
        + ", ".join(tf.upper() for tf in aligned)
    )
    if unavailable:
        reasoning += "; unavailable: " + ", ".join(tf.upper() for tf in unavailable)
    return ConfluenceResult(
        symbol=symbol, timestamp=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        direction=direction, confluences=len(aligned), confluence_score=round(score, 2),
        timeframe_signals=signals, is_confluence=passed,
        confidence=round(score / max(1.0, sum(rule.weight for rule in config.rules)), 2),
        reasoning=reasoning, entry_price_zone=(support, resistance),
        unavailable_timeframes=unavailable,
    )


def _sector_regime_stage(symbol: str, direction: str) -> Dict[str, Any]:
    regime = _latest_regime(symbol)
    snapshot = get_technical_snapshot(symbol, "1d") or {}
    sector_strength = _safe_num(snapshot.get("sector_strength"))
    sector_rs = _safe_num(snapshot.get("sector_rs"))
    if not regime and sector_strength is None and sector_rs is None:
        return {"status": "unavailable", "pass": True, "score": 0,
                "reason": "No cached regime or sector snapshot", "details": {}}
    if not regime:
        sector_ok = (sector_strength or 0) >= 0 and (sector_rs or 0) >= 0
        return {"status": "partial", "pass": sector_ok, "score": 1 if sector_ok else 0,
                "reason": f"Sector strength {sector_strength if sector_strength is not None else '—'} · RS {sector_rs if sector_rs is not None else '—'}",
                "details": {"sector_strength": sector_strength, "sector_rs": sector_rs}}
    bias = str(regime.get("bias") or "").lower()
    trend = str(regime.get("ema_trend") or "").lower()
    bullish = any(word in (bias + " " + trend) for word in ("bull", "up"))
    bearish = any(word in (bias + " " + trend) for word in ("bear", "down"))
    regime_pass = bullish if direction == "bull" else bearish if direction == "bear" else bullish or bearish
    sector_pass = (sector_strength is None or sector_strength >= 0) and (sector_rs is None or sector_rs >= 0)
    if direction == "bear":
        sector_pass = (sector_strength is None or sector_strength <= 0) and (sector_rs is None or sector_rs <= 0)
    passed = regime_pass and sector_pass
    return {
        "status": "ok", "pass": passed, "score": 1 if passed else 0,
        "reason": f"{regime.get('regime') or 'Regime'} · {regime.get('bias') or 'neutral bias'} · sector RS {sector_rs if sector_rs is not None else '—'}",
        "details": {**regime, "sector_strength": sector_strength, "sector_rs": sector_rs},
    }


def _mode(payload: Dict[str, Any], name: str) -> str:
    value = str(payload.get(name, "off")).lower()
    return value if value in STAGE_MODES else "off"


def _evaluate_symbol(symbol: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    config = ConfluenceConfig(
        direction=str(payload.get("direction", "both")).lower(),
        min_confluences=max(1, min(4, int(payload.get("min_confluences", 3)))),
        min_confluence_score=float(payload.get("min_score", 3)),
    )
    confluence = MULTI_TIMEFRAME_CONFLUENCE(symbol, config)
    sector = _sector_regime_stage(symbol, confluence.direction)
    stages = {
        "sector_regime": sector,
        "mtf_confluence": {
            "status": "ok" if not confluence.unavailable_timeframes else "partial",
            "pass": confluence.is_confluence, "score": confluence.confluence_score,
            "reason": confluence.reasoning, "details": asdict(confluence),
        },
    }
    included = True
    for name in ("sector_regime", "mtf_confluence"):
        if _mode(payload, name) == "filter" and stages[name]["status"] != "unavailable" and not stages[name]["pass"]:
            included = False
    daily = (confluence.timeframe_signals.get("1d") or {}).get("values") or {}
    return {
        "symbol": symbol, "included": included, "direction": confluence.direction,
        "score": round(sum(stage["score"] for stage in stages.values()), 2),
        "price": daily.get("close"), "entry_zone": list(confluence.entry_price_zone),
        "stages": stages,
    }


@institutional_confluence_bp.route("/")
def page():
    return render_template("institutional_confluence.html")


@institutional_confluence_bp.route("/api/run", methods=["POST"])
def run():
    payload = request.get_json(silent=True) or {}
    try:
        watchlist_id = int(payload.get("watchlist_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Choose a watchlist."}), 400
    symbols = _watchlist_symbols(watchlist_id)
    if not symbols:
        return jsonify({"error": "The selected watchlist has no symbols."}), 400
    rows: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        futures = [pool.submit(_evaluate_symbol, symbol, payload) for symbol in symbols]
        for future in concurrent.futures.as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as exc:
                rows.append({"symbol": "?", "included": False, "score": 0,
                             "error": f"{type(exc).__name__}: {exc}"})
    rows.sort(key=lambda row: (row.get("included", False), row.get("score", 0)), reverse=True)
    after_sector = sum(1 for row in rows if _mode(payload, "sector_regime") != "filter" or row["stages"]["sector_regime"]["pass"] or row["stages"]["sector_regime"]["status"] == "unavailable")
    included = [row for row in rows if row.get("included")]
    return jsonify({
        "results": included, "excluded": [row for row in rows if not row.get("included")],
        "funnel": [
            {"stage": "Watchlist", "count": len(symbols)},
            {"stage": "Sector / regime", "count": after_sector, "mode": _mode(payload, "sector_regime")},
            {"stage": "MTF confluence", "count": len(included), "mode": _mode(payload, "mtf_confluence")},
        ],
        "completed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    })
