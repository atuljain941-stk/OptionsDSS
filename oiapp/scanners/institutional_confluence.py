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


def _price_structure(symbol: str, direction: str) -> Dict[str, Any]:
    """Phase 2 price/structure evidence from the same cached OHLCV data
    used by the rest of OIAPP.  The cache fallback makes the entry zone
    usable even when a precomputed snapshot has not yet written S/R."""
    con = _conn()
    try:
        rows = con.execute(
            "SELECT date, open, high, low, close FROM price_cache "
            "WHERE symbol=? ORDER BY date DESC LIMIT 60", (symbol,)
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    if len(rows) < 20:
        return {"status": "unavailable", "pass": True, "score": 0,
                "reason": "Insufficient cached daily bars for structure", "details": {}}
    rows = list(reversed(rows))
    closes = [_safe_num(row["close"]) for row in rows]
    highs = [_safe_num(row["high"]) for row in rows]
    lows = [_safe_num(row["low"]) for row in rows]
    if not all(value is not None for value in closes + highs + lows):
        return {"status": "unavailable", "pass": True, "score": 0,
                "reason": "Incomplete OHLCV data", "details": {}}
    price = closes[-1]
    support = round(min(lows[-20:]), 2)
    resistance = round(max(highs[-20:]), 2)
    prior_resistance = max(highs[-21:-1])
    prior_support = min(lows[-21:-1])
    breakout = price > prior_resistance
    breakdown = price < prior_support
    snapshot = get_technical_snapshot(symbol, "1d") or {}
    candle_score = _safe_num(snapshot.get("candle_ctx_score"))
    candle_conf = _safe_num(snapshot.get("candle_ctx_confluence"))
    if direction == "bull":
        passed = breakout or (price >= support and price > closes[-5])
        event = "breakout" if breakout else "holding above support" if passed else "no bullish structure confirmation"
    else:
        passed = breakdown or (price <= resistance and price < closes[-5])
        event = "breakdown" if breakdown else "holding below resistance" if passed else "no bearish structure confirmation"
    score = (2 if breakout or breakdown else 1 if passed else 0) + (1 if (candle_score or 0) >= 4 else 0)
    return {
        "status": "ok", "pass": passed, "score": score,
        "reason": f"{event}; support ${support:.2f} / resistance ${resistance:.2f}",
        "details": {"price": price, "support": support, "resistance": resistance,
                    "breakout": breakout, "breakdown": breakdown,
                    "candle_context_score": candle_score, "candle_context_confluence": candle_conf},
    }


def _options_positioning(symbol: str, direction: str, spot: Optional[float]) -> Dict[str, Any]:
    """Phase 3: use the newest available option-chain snapshot only.

    Calls contribute positive gamma (blue in the UI convention) and puts
    contribute negative gamma (red).  This is positioning context, not a
    prediction: walls are levels where hedging/positioning can matter.
    """
    con = _conn()
    try:
        row = con.execute("SELECT MAX(date) AS d FROM options WHERE symbol=?", (symbol,)).fetchone()
        as_of = row["d"] if row else None
        if not as_of:
            rows = []
        else:
            rows = con.execute(
                "SELECT type, strike, COALESCE(oi,0) AS oi, COALESCE(volume,0) AS volume, "
                "COALESCE(gamma,0) AS gamma, underlying FROM options "
                "WHERE symbol=? AND date=? AND expiration>=date(?)", (symbol, as_of, as_of)
            ).fetchall()
    except sqlite3.Error:
        rows, as_of = [], None
    finally:
        con.close()
    if not rows:
        return {"status": "unavailable", "pass": True, "score": 0,
                "reason": "No current option-chain snapshot", "details": {}}
    calls = [row for row in rows if str(row["type"]).lower() == "call"]
    puts = [row for row in rows if str(row["type"]).lower() == "put"]
    if not calls or not puts:
        return {"status": "partial", "pass": True, "score": 0,
                "reason": "Incomplete call/put option chain", "details": {"as_of": as_of}}
    call_wall = max(calls, key=lambda row: _safe_num(row["oi"]) or 0)
    put_wall = max(puts, key=lambda row: _safe_num(row["oi"]) or 0)
    call_oi = sum(_safe_num(row["oi"]) or 0 for row in calls)
    put_oi = sum(_safe_num(row["oi"]) or 0 for row in puts)
    call_vol = sum(_safe_num(row["volume"]) or 0 for row in calls)
    put_vol = sum(_safe_num(row["volume"]) or 0 for row in puts)
    inferred_spot = spot or _safe_num(calls[0]["underlying"])
    scale = 100 * (inferred_spot or 0) ** 2 * 0.01
    call_gex = sum((_safe_num(row["gamma"]) or 0) * (_safe_num(row["oi"]) or 0) * scale for row in calls)
    put_gex = -sum((_safe_num(row["gamma"]) or 0) * (_safe_num(row["oi"]) or 0) * scale for row in puts)
    net_gex = call_gex + put_gex
    call_strike, put_strike = _safe_num(call_wall["strike"]), _safe_num(put_wall["strike"])
    inside_walls = bool(inferred_spot and put_strike and call_strike and put_strike <= inferred_spot <= call_strike)
    gamma_agrees = (net_gex >= 0 if direction == "bull" else net_gex <= 0)
    passed = inside_walls and gamma_agrees
    score = int(inside_walls) + int(gamma_agrees)
    wall_text = f"put wall ${put_strike:.2f} · call wall ${call_strike:.2f}"
    return {
        "status": "ok", "pass": passed, "score": score,
        "reason": f"{wall_text}; net GEX ${net_gex/1_000_000:.1f}M",
        "details": {
            "as_of": as_of, "put_wall": put_strike, "put_wall_oi": _safe_num(put_wall["oi"]),
            "call_wall": call_strike, "call_wall_oi": _safe_num(call_wall["oi"]),
            "call_oi": call_oi, "put_oi": put_oi, "call_volume": call_vol, "put_volume": put_vol,
            "put_call_oi_ratio": round(put_oi / call_oi, 2) if call_oi else None,
            "put_call_volume_ratio": round(put_vol / call_vol, 2) if call_vol else None,
            "call_gex": round(call_gex, 2), "put_gex": round(put_gex, 2),
            "net_gex": round(net_gex, 2), "inside_walls": inside_walls,
        },
    }


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
    structure = _price_structure(symbol, confluence.direction)
    positioning = _options_positioning(symbol, confluence.direction, structure.get("details", {}).get("price"))
    stages = {
        "sector_regime": sector,
        "mtf_confluence": {
            "status": "ok" if not confluence.unavailable_timeframes else "partial",
            "pass": confluence.is_confluence, "score": confluence.confluence_score,
            "reason": confluence.reasoning, "details": asdict(confluence),
        },
        "price_structure": structure,
        "options_positioning": positioning,
    }
    included = True
    for name in ("sector_regime", "mtf_confluence", "price_structure", "options_positioning"):
        if _mode(payload, name) == "filter" and stages[name]["status"] != "unavailable" and not stages[name]["pass"]:
            included = False
    daily = (confluence.timeframe_signals.get("1d") or {}).get("values") or {}
    return {
        "symbol": symbol, "included": included, "direction": confluence.direction,
        "score": round(sum(stage["score"] for stage in stages.values()), 2),
        "price": daily.get("close") or structure.get("details", {}).get("price"),
        "entry_zone": [
            confluence.entry_price_zone[0] or structure.get("details", {}).get("support"),
            confluence.entry_price_zone[1] or structure.get("details", {}).get("resistance"),
        ],
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
    after_mtf = sum(1 for row in rows if _mode(payload, "mtf_confluence") != "filter" or row["stages"]["mtf_confluence"]["pass"] or row["stages"]["mtf_confluence"]["status"] == "unavailable")
    after_structure = sum(1 for row in rows if _mode(payload, "price_structure") != "filter" or row["stages"]["price_structure"]["pass"] or row["stages"]["price_structure"]["status"] == "unavailable")
    included = [row for row in rows if row.get("included")]
    return jsonify({
        "results": included, "excluded": [row for row in rows if not row.get("included")],
        "funnel": [
            {"stage": "Watchlist", "count": len(symbols)},
            {"stage": "Sector / regime", "count": after_sector, "mode": _mode(payload, "sector_regime")},
            {"stage": "MTF confluence", "count": after_mtf, "mode": _mode(payload, "mtf_confluence")},
            {"stage": "Price / structure", "count": after_structure, "mode": _mode(payload, "price_structure")},
            {"stage": "Options positioning", "count": len(included), "mode": _mode(payload, "options_positioning")},
        ],
        "completed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    })
