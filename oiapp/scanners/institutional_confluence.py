"""Watchlist-driven institutional scanner — Phase 1.

The scanner is intentionally a pipeline, not a monolithic score.  Every
stage can be off, score-only, or a gate.  Later phases can append their own
stage dictionaries without changing the API or the results grid.
"""
from __future__ import annotations

import concurrent.futures
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
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


def _iv_rank(symbol: str) -> Dict[str, Any]:
    """IV Rank from daily stored chain snapshots.

    IV Rank is (current IV - lowest observed IV) / observed range.  We use
    the average IV across each daily stored chain because the database does
    not retain one canonical ATM contract for every historical snapshot.
    """
    con = _conn()
    try:
        rows = con.execute(
            "SELECT date, AVG(iv) AS iv FROM options "
            "WHERE symbol=? AND iv IS NOT NULL AND iv>0 GROUP BY date ORDER BY date",
            (symbol,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    values = [(str(row["date"])[:10], _safe_num(row["iv"])) for row in rows]
    values = [(when, iv) for when, iv in values if iv is not None]
    if not values:
        return {"status": "unavailable", "rank": None, "current_iv": None,
                "observations": 0, "reason": "No historical option IV snapshots"}
    _, current = values[-1]
    history = [iv for _, iv in values]
    low, high = min(history), max(history)
    rank = 50.0 if high == low else round((current - low) / (high - low) * 100.0, 1)
    context = "high-volatility context" if rank >= 67 else (
        "low-volatility context" if rank <= 33 else "mid-range volatility"
    )
    return {"status": "ok", "rank": rank, "current_iv": round(current * 100, 1),
            "low_iv": round(low * 100, 1), "high_iv": round(high * 100, 1),
            "observations": len(history), "as_of": values[-1][0], "reason": context}


def _earnings_risk(symbol: str, min_days: int) -> Dict[str, Any]:
    """Phase 4 catalyst guard using the cached earnings calendar.

    An unknown earnings date is reported as unavailable rather than treated
    as safe.  Calendar days are used deliberately and labelled in the UI;
    this avoids claiming holiday-aware trading-day precision from a cached
    company-calendar record.
    """
    con = _conn()
    try:
        row = con.execute(
            "SELECT next_earn_date, next_earn_confirmed, fetch_date FROM earnings_calendar "
            "WHERE symbol=? ORDER BY fetch_date DESC LIMIT 1", (symbol,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    if not row or not row["next_earn_date"]:
        return {"status": "unavailable", "pass": True, "score": 0,
                "reason": "No cached upcoming earnings date", "details": {"earnings_days": None}}
    raw = str(row["next_earn_date"])[:10]
    try:
        earn_date = date.fromisoformat(raw)
    except ValueError:
        return {"status": "unavailable", "pass": True, "score": 0,
                "reason": f"Unparseable earnings date: {raw}", "details": {"earnings_days": None}}
    days = (earn_date - date.today()).days
    if days < 0:
        return {"status": "unavailable", "pass": True, "score": 0,
                "reason": "Cached earnings date is no longer upcoming", "details": {"earnings_days": None}}
    passed = days >= min_days
    score = 1 if passed else 0
    confirmation = "confirmed" if row["next_earn_confirmed"] else "unconfirmed"
    return {
        "status": "ok", "pass": passed, "score": score,
        "reason": f"Earnings {raw} · {days} calendar day(s) away · {confirmation}",
        "details": {"earnings_date": raw, "earnings_days": days,
                    "confirmed": bool(row["next_earn_confirmed"]), "min_days": min_days},
    }


def _historical_prices(symbol: str) -> List[Tuple[date, float]]:
    """Return clean daily closes for a deterministic price-outcome backtest."""
    con = _conn()
    try:
        rows = con.execute(
            "SELECT date, close FROM price_cache WHERE symbol=? ORDER BY date", (symbol,)
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    prices: List[Tuple[date, float]] = []
    for row in rows:
        try:
            when = date.fromisoformat(str(row["date"])[:10])
        except ValueError:
            continue
        close = _safe_num(row["close"])
        if close is not None and close > 0:
            prices.append((when, close))
    return prices


def _ema(values: List[float], length: int) -> Optional[float]:
    if len(values) < length:
        return None
    value = sum(values[:length]) / length
    alpha = 2.0 / (length + 1.0)
    for close in values[length:]:
        value = alpha * close + (1.0 - alpha) * value
    return value


def _historical_direction(closes: List[float]) -> Optional[str]:
    """Daily historical signal used for the Phase 5 outcome study.

    It deliberately uses only data available at the entry close: EMA13/EMA50
    trend placement plus a 10-session momentum check.  We do not pretend that
    today's cached option chain or sector scan was known on a past date.
    """
    ema13, ema50 = _ema(closes, 13), _ema(closes, 50)
    if ema13 is None or ema50 is None or len(closes) < 11:
        return None
    close = closes[-1]
    momentum = close - closes[-11]
    if close >= ema13 >= ema50 and momentum >= 0:
        return "bull"
    if close <= ema13 <= ema50 and momentum <= 0:
        return "bear"
    return None


def _backtest_symbol(symbol: str, start: date, days: int, dte: int, direction_mode: str) -> List[Dict[str, Any]]:
    prices = _historical_prices(symbol)
    if len(prices) < 50:
        return []
    rows: List[Dict[str, Any]] = []
    for offset in range(days):
        requested = start + timedelta(days=offset)
        # A requested non-trading day does not open a duplicate next-session
        # trade. It is simply skipped, as no closing signal existed that day.
        entry_idx = next((idx for idx, (when, _) in enumerate(prices) if when == requested), None)
        if entry_idx is None or entry_idx < 49:
            continue
        entry_date, entry_price = prices[entry_idx]
        side = _historical_direction([close for _, close in prices[:entry_idx + 1]])
        if side is None or (direction_mode in ("bull", "bear") and side != direction_mode):
            continue
        target = entry_date + timedelta(days=dte)
        exit_idx = next((idx for idx, (when, _) in enumerate(prices) if idx > entry_idx and when >= target), None)
        if exit_idx is None:
            rows.append({
                "symbol": symbol, "trade_date": entry_date.isoformat(), "direction": side,
                "entry_price": entry_price, "dte": dte, "outcome": "pending",
                "reason": "No cached close yet at the selected DTE", "exit_date": None,
                "exit_price": None, "return_pct": None,
            })
            continue
        exit_date, exit_price = prices[exit_idx]
        raw_return = (exit_price / entry_price - 1.0) * 100.0
        won = exit_price > entry_price if side == "bull" else exit_price < entry_price
        rows.append({
            "symbol": symbol, "trade_date": entry_date.isoformat(), "direction": side,
            "entry_price": round(entry_price, 2), "dte": dte,
            "exit_date": exit_date.isoformat(), "exit_price": round(exit_price, 2),
            "return_pct": round(raw_return, 2), "outcome": "winner" if won else "loser",
            "reason": (
                f"{side.title()} signal at entry close; {exit_date.isoformat()} close "
                f"${exit_price:.2f} is {'above' if exit_price > entry_price else 'below' if exit_price < entry_price else 'equal to'} entry ${entry_price:.2f}"
            ),
        })
    return rows


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
    regime_label = str(regime.get("regime") or "").lower()
    regime_text = " ".join((bias, trend, regime_label))
    # Determine one explicit regime direction.  A counter-directional regime
    # is a conflict, not a pass merely because the numerical sector fields
    # are unavailable.
    regime_direction = "bull" if any(word in regime_text for word in ("bull", "uptrend", " up")) else (
        "bear" if any(word in regime_text for word in ("bear", "downtrend", " down")) else "neutral"
    )
    regime_pass = regime_direction == direction if direction in ("bull", "bear") else regime_direction != "neutral"
    sector_pass = (sector_strength is None or sector_strength >= 0) and (sector_rs is None or sector_rs >= 0)
    if direction == "bear":
        sector_pass = (sector_strength is None or sector_strength <= 0) and (sector_rs is None or sector_rs <= 0)
    passed = regime_pass and sector_pass
    conflict = direction in ("bull", "bear") and regime_direction in ("bull", "bear") and regime_direction != direction
    weak_alignment = passed and any(word in regime_text for word in ("mild", "weak"))
    score = 0.5 if weak_alignment else 1 if passed else 0
    reason = (f"CONFLICT: candidate {direction} vs regime {regime_direction}" if conflict else
              (f"Weak {regime_direction} alignment" if weak_alignment else
               f"{regime.get('regime') or 'Regime'} · {regime.get('bias') or 'neutral bias'} · sector RS {sector_rs if sector_rs is not None else '—'}"))
    return {
        "status": "ok", "pass": passed, "score": score,
        "reason": reason,
        "details": {**regime, "sector_strength": sector_strength, "sector_rs": sector_rs,
                    "regime_direction": regime_direction, "conflict": conflict,
                    "weak_alignment": weak_alignment},
    }


def _mode(payload: Dict[str, Any], name: str) -> str:
    value = str(payload.get(name, "off")).lower()
    if name == "earnings_risk":
        # Earnings is a risk gate, not a directional score contributor.
        return value if value in {"off", "filter"} else "filter"
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
    iv_rank = _iv_rank(symbol)
    earnings = _earnings_risk(symbol, max(0, int(payload.get("min_earnings_days", 0))))
    stages = {
        "sector_regime": sector,
        "mtf_confluence": {
            "status": "ok" if not confluence.unavailable_timeframes else "partial",
            "pass": confluence.is_confluence, "score": confluence.confluence_score,
            "reason": confluence.reasoning, "details": asdict(confluence),
        },
        "price_structure": structure,
        "options_positioning": positioning,
        "earnings_risk": earnings,
    }
    included = True
    for name in ("sector_regime", "mtf_confluence", "price_structure", "options_positioning", "earnings_risk"):
        if _mode(payload, name) == "filter" and stages[name]["status"] != "unavailable" and not stages[name]["pass"]:
            included = False
    daily = (confluence.timeframe_signals.get("1d") or {}).get("values") or {}
    stage_max = {"sector_regime": 1, "mtf_confluence": 4, "price_structure": 3,
                 "options_positioning": 2}
    # Earnings is deliberately excluded: it is Off or Filter only.
    enabled = [name for name in stage_max if _mode(payload, name) != "off"]
    total_score = round(sum(stages[name]["score"] for name in enabled), 2)
    max_score = sum(stage_max[name] for name in enabled)
    score_breakdown = [
        {"stage": name, "score": stages[name]["score"], "max": stage_max[name],
         "pass": stages[name]["pass"], "status": stages[name]["status"]}
        for name in stage_max if _mode(payload, name) == "score"
    ]
    return {
        "symbol": symbol, "included": included, "direction": confluence.direction,
        "score": total_score, "max_score": max_score,
        "score_breakdown": score_breakdown,
        "price": daily.get("close") or structure.get("details", {}).get("price"),
        "entry_zone": [
            confluence.entry_price_zone[0] or structure.get("details", {}).get("support"),
            confluence.entry_price_zone[1] or structure.get("details", {}).get("resistance"),
        ],
        "stages": stages,
        "iv_rank": iv_rank,
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
    def survives(row: Dict[str, Any], names: Iterable[str]) -> bool:
        return all(
            _mode(payload, name) != "filter"
            or row["stages"][name]["status"] == "unavailable"
            or row["stages"][name]["pass"]
            for name in names
        )
    stage_order = ("sector_regime", "mtf_confluence", "price_structure", "options_positioning", "earnings_risk")
    after_sector = sum(survives(row, stage_order[:1]) for row in rows)
    after_mtf = sum(survives(row, stage_order[:2]) for row in rows)
    after_structure = sum(survives(row, stage_order[:3]) for row in rows)
    after_options = sum(survives(row, stage_order[:4]) for row in rows)
    after_earnings = sum(survives(row, stage_order) for row in rows)
    min_total_score = max(0, float(payload.get("min_total_score", 0) or 0))
    included = [row for row in rows if row.get("included") and row.get("score", 0) >= min_total_score]
    return jsonify({
        "results": included, "excluded": [row for row in rows if not row.get("included")],
        "funnel": [
            {"stage": "Watchlist", "count": len(symbols)},
            {"stage": "Sector / regime", "count": after_sector, "mode": _mode(payload, "sector_regime")},
            {"stage": "MTF confluence", "count": after_mtf, "mode": _mode(payload, "mtf_confluence")},
            {"stage": "Price / structure", "count": after_structure, "mode": _mode(payload, "price_structure")},
            {"stage": "Options positioning", "count": after_options, "mode": _mode(payload, "options_positioning")},
            {"stage": "Earnings / catalyst", "count": after_earnings, "mode": _mode(payload, "earnings_risk")},
            {"stage": "Overall score", "count": len(included), "mode": f">= {min_total_score:g}"},
        ],
        "completed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    })


@institutional_confluence_bp.route("/api/backtest", methods=["POST"])
def backtest():
    """Run the daily historical-price outcome study for a selected watchlist.

    This does not reconstruct unavailable historical option, sector, or
    earnings snapshots.  It uses the entry-date daily close, EMA13/EMA50 and
    momentum available on that date, then evaluates the close at DTE.  The
    response makes this limitation explicit so the results are not presented
    as an options P/L simulation.
    """
    payload = request.get_json(silent=True) or {}
    try:
        watchlist_id = int(payload.get("watchlist_id"))
        start = date.fromisoformat(str(payload.get("backtest_from"))[:10])
        days = max(1, min(90, int(payload.get("backtest_days", 5))))
        dte = max(1, min(365, int(payload.get("backtest_dte", 30))))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid From date, run length, and DTE."}), 400
    symbols = _watchlist_symbols(watchlist_id)
    if not symbols:
        return jsonify({"error": "The selected watchlist has no symbols."}), 400
    direction = str(payload.get("direction", "both")).lower()
    if direction not in {"bull", "bear", "both"}:
        direction = "both"
    rows: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        futures = [pool.submit(_backtest_symbol, symbol, start, days, dte, direction) for symbol in symbols]
        for future in concurrent.futures.as_completed(futures):
            try:
                rows.extend(future.result())
            except Exception:
                continue
    rows.sort(key=lambda row: (row["trade_date"], row["symbol"]), reverse=True)
    winners = sum(row["outcome"] == "winner" for row in rows)
    losers = sum(row["outcome"] == "loser" for row in rows)
    pending = sum(row["outcome"] == "pending" for row in rows)
    completed = winners + losers
    return jsonify({
        "results": rows,
        "summary": {
            "total_trades": len(rows), "winners": winners, "losers": losers,
            "pending": pending,
            "win_rate": round(winners / completed * 100, 1) if completed else None,
            "from_date": start.isoformat(), "days": days, "dte": dte,
        },
        "methodology": "Historical daily price signal only: entry-close EMA13/EMA50 plus 10-session momentum; exit is the first cached market close on or after entry date + calendar DTE. This is direction outcome analysis, not option-contract P/L.",
        "completed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    })
