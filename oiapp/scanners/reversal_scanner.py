"""Systematic capitulation / reversal scanner built from measurable rules."""
from __future__ import annotations

import concurrent.futures
import math
import sqlite3
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

from ..config import DB_PATH
from ..services.option_volatility import option_iv_context

systematic_reversal_bp = Blueprint("systematic_reversal", __name__, url_prefix="/systematic-reversal")
MODES = {"off", "score", "filter"}


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _num(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _mode(payload: Dict[str, Any], name: str) -> str:
    value = str(payload.get(name, "score")).lower()
    if name == "earnings_risk":
        return value if value in {"off", "filter"} else "filter"
    return value if value in MODES else "score"


def _watchlists() -> List[Dict[str, Any]]:
    try:
        from .watchlist_manager import _ensure_tables
        _ensure_tables()
    except Exception:
        pass
    con = _conn()
    try:
        rows = con.execute(
            "SELECT w.id, w.name, COALESCE(w.is_default, 0) AS is_default, "
            "COUNT(ws.symbol) AS symbol_count FROM watchlists w "
            "LEFT JOIN watchlist_symbols ws ON ws.watchlist_id=w.id "
            "GROUP BY w.id, w.name, w.is_default "
            "ORDER BY COALESCE(w.is_default, 0) DESC, w.name"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _symbols(watchlist_id: int) -> List[str]:
    con = _conn()
    try:
        return [
            str(row["symbol"]).upper()
            for row in con.execute(
                "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                (watchlist_id,),
            ).fetchall()
            if row["symbol"]
        ]
    finally:
        con.close()


def _bars(symbol: str, as_of: Optional[date] = None, limit: int = 260) -> List[Dict[str, Any]]:
    con = _conn()
    try:
        query = "SELECT date,high,low,close,volume FROM price_cache WHERE symbol=?"
        values: List[Any] = [symbol]
        if as_of:
            query += " AND date<=?"
            values.append(as_of.isoformat())
        query += " ORDER BY date DESC LIMIT ?"
        values.append(limit)
        rows = con.execute(query, values).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    output = []
    for row in reversed(rows):
        values = {key: _num(row[key]) for key in ("high", "low", "close", "volume")}
        if all(values[key] is not None for key in ("high", "low", "close")):
            output.append({"date": str(row["date"])[:10], **values})
    return output


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _std(values):
    average = _mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / len(values)) if len(values) > 1 else 0.0


def _streak(closes):
    if len(closes) < 2 or closes[-1] == closes[-2]:
        return "neutral", 0
    side = "bull" if closes[-1] > closes[-2] else "bear"
    count = 0
    for index in range(len(closes) - 1, 0, -1):
        if (side == "bull" and closes[index] > closes[index - 1]) or (side == "bear" and closes[index] < closes[index - 1]):
            count += 1
        else:
            break
    return side, count


def _levels(bars: List[Dict[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    lookback = bars[-21:-1] if len(bars) > 21 else bars[:-1]
    if not lookback:
        return None, None
    return min(row["low"] for row in lookback), max(row["high"] for row in lookback)


def _walls(symbol: str, as_of: Optional[date] = None) -> Dict[str, Optional[float]]:
    """Largest OI put/call strikes known at the evaluation date."""
    con = _conn()
    try:
        query = "SELECT type, strike, oi FROM options WHERE symbol=? AND type IN ('call','put')"
        values: List[Any] = [symbol]
        if as_of:
            query += " AND date<=?"
            values.append(as_of.isoformat())
        query += " ORDER BY date DESC, oi DESC LIMIT 400"
        rows = con.execute(query, values).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    walls = {"put_wall": None, "call_wall": None}
    for option_type, key in (("put", "put_wall"), ("call", "call_wall")):
        candidates = [row for row in rows if str(row["type"]).lower() == option_type and _num(row["strike"]) is not None]
        if candidates:
            walls[key] = _num(max(candidates, key=lambda row: _num(row["oi"]) or 0)["strike"])
    return walls


def _earnings_risk(symbol: str, minimum_days: int, as_of: Optional[date] = None) -> Dict[str, Any]:
    con = _conn()
    try:
        query = "SELECT next_earn_date, next_earn_confirmed, fetch_date FROM earnings_calendar WHERE symbol=?"
        values: List[Any] = [symbol]
        if as_of:
            query += " AND fetch_date<=?"
            values.append(as_of.isoformat())
        row = con.execute(query + " ORDER BY fetch_date DESC LIMIT 1", values).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    if not row or not row["next_earn_date"]:
        return {"status": "unavailable", "pass": True, "score": 0, "reason": "No cached upcoming earnings date", "details": {"earnings_days": None}}
    try:
        earnings_date = date.fromisoformat(str(row["next_earn_date"])[:10])
    except ValueError:
        return {"status": "unavailable", "pass": True, "score": 0, "reason": "Unparseable earnings date", "details": {"earnings_days": None}}
    reference = as_of or date.today()
    days = (earnings_date - reference).days
    safe = days > minimum_days
    confirmed = "confirmed" if row["next_earn_confirmed"] else "estimated"
    return {
        "status": "ok",
        "pass": safe,
        "score": 0,
        "reason": f"Earnings {earnings_date.isoformat()} · {days} calendar days away · {confirmed}",
        "details": {"earnings_date": earnings_date.isoformat(), "earnings_days": days, "confirmed": bool(row["next_earn_confirmed"])},
    }


def _evaluate(symbol: str, payload: Dict[str, Any], as_of: Optional[date] = None) -> Dict[str, Any]:
    bars = _bars(symbol, as_of)
    if len(bars) < 25:
        return {"symbol": symbol, "included": False, "score": 0, "max_score": 0, "error": "Insufficient daily history"}

    closes = [row["close"] for row in bars]
    price = closes[-1]
    move_direction, streak = _streak(closes)
    requested = str(payload.get("direction", "both")).lower()
    target = requested if requested in {"bull", "bear"} else ("bear" if move_direction == "bull" else "bull" if move_direction == "bear" else "neutral")
    move_direction = "bear" if target == "bull" else "bull" if target == "bear" else move_direction

    returns = [abs(closes[index] / closes[index - 1] - 1) for index in range(1, len(closes))]
    rate = returns[-1] / max(_mean(returns[-21:-1]), 0.0001)
    acceleration = {"status": "ok", "pass": rate >= 2, "score": 2 if rate >= 3 else 1 if rate >= 2 else 0, "reason": f"Last move {returns[-1] * 100:.2f}% · {rate:.1f}× 20-day average"}
    streak_stage = {"status": "ok", "pass": streak >= 3, "score": 2 if streak >= 5 else 1 if streak >= 3 else 0, "reason": f"{streak} consecutive {move_direction} closes"}

    basis = _mean(closes[-20:])
    band = 2 * _std(closes[-20:])
    upper, lower = basis + band, basis - band
    extension = (price - upper) / max(band, 0.0001) if move_direction == "bull" else (lower - price) / max(band, 0.0001)
    bollinger = {"status": "ok", "pass": extension >= 0, "score": 2 if extension >= 0.5 else 1 if extension >= 0 else 0, "reason": f"Price {price:.2f}; band {(upper if move_direction == 'bull' else lower):.2f}"}

    volumes = [row["volume"] for row in bars if row["volume"] is not None]
    average_volume = _mean(volumes[-21:-1]) if len(volumes) >= 21 else 0
    volume_ratio = (bars[-1]["volume"] or 0) / average_volume if average_volume else None
    volume = {"status": "ok" if volume_ratio is not None else "unavailable", "pass": volume_ratio is not None and volume_ratio >= 1.5, "score": 2 if volume_ratio is not None and volume_ratio >= 3 else 1 if volume_ratio is not None and volume_ratio >= 1.5 else 0, "reason": f"Volume {volume_ratio:.1f}× average" if volume_ratio is not None else "Volume unavailable"}

    directions = [1 if closes[index] > closes[index - 1] else -1 if closes[index] < closes[index - 1] else 0 for index in range(1, len(closes))][-12:]
    sign = 1 if move_direction == "bull" else -1
    legs = sum(1 for index in range(1, len(directions)) if directions[index] == sign and directions[index - 1] != sign)
    flat_fraction = sum(value == 0 for value in directions) / max(len(directions), 1)
    structure = {"status": "ok", "pass": legs >= 2 and flat_fraction <= .25, "score": 2 if legs >= 3 and flat_fraction <= .17 else 1 if legs >= 2 and flat_fraction <= .25 else 0, "reason": f"{legs} directional legs; {flat_fraction * 100:.0f}% flat bars"}

    change_90 = (price / closes[-min(91, len(closes))] - 1) * 100 if len(closes) >= 2 else None
    if change_90 is None:
        rsi = {"status": "unavailable", "pass": True, "score": 0, "reason": "RSIDiff90 unavailable"}
    else:
        good = change_90 <= -20 if target == "bull" else change_90 >= 20
        rsi = {"status": "ok", "pass": good, "score": 2 if good and abs(change_90) >= 30 else 1 if good else 0, "reason": f"RSIDiff90 proxy {change_90:+.1f}; threshold {'≤ -20' if target == 'bull' else '≥ +20'}"}

    iv_dte = max(1, min(365, int(payload.get("volatility_dte", 30) or 30)))
    iv_context = option_iv_context(symbol, price, dte=iv_dte, as_of=as_of)
    iv_change = _num(iv_context.get("iv_change_5obs"))
    if iv_context.get("status") != "ok" or iv_change is None:
        iv_fade = {"status": "unavailable", "pass": True, "score": 0, "reason": iv_context.get("reason", "IV trend unavailable"), "details": iv_context}
    else:
        fading = iv_change <= -2.0
        iv_fade = {"status": "ok", "pass": fading, "score": 2 if iv_change <= -5.0 else 1 if fading else 0, "reason": f"IV change {iv_change:+.1f} points across five stored observations", "details": iv_context}

    support, resistance = _levels(bars)
    level = support if target == "bull" else resistance
    label = "support" if target == "bull" else "resistance"
    near_level = level is not None and (price <= level * 1.02 if target == "bull" else price >= level * .98)
    sr = {"status": "ok" if level is not None else "unavailable", "pass": near_level if level is not None else True, "score": 1 if near_level else 0, "reason": f"Price {price:.2f}; {label} {level:.2f}" if level is not None else f"{label.title()} unavailable"}

    walls = _walls(symbol, as_of)
    earnings = _earnings_risk(symbol, max(0, int(payload.get("min_earnings_days", 0) or 0)), as_of)
    stages = {"acceleration": acceleration, "streak": streak_stage, "bollinger": bollinger, "volume": volume, "structure": structure, "rsi_extreme": rsi, "iv_fade": iv_fade, "sr_location": sr, "earnings_risk": earnings}

    included = target != "neutral"
    for name, stage in stages.items():
        if _mode(payload, name) == "filter" and stage["status"] != "unavailable" and not stage["pass"]:
            included = False
    enabled = [name for name in stages if _mode(payload, name) != "off" and name != "earnings_risk"]
    total = sum(stages[name]["score"] for name in enabled)
    maximum = sum(1 if name == "sr_location" else 2 for name in enabled)
    return {"symbol": symbol, "direction": target, "move_direction": move_direction, "price": price, "score": total, "max_score": maximum, "included": included, "stages": stages, "rsidiff90": change_90, "support": support, "resistance": resistance, "put_wall": walls["put_wall"], "call_wall": walls["call_wall"], "iv_context": iv_context, "earnings": earnings.get("details", {})}


def _backtest_symbol(symbol: str, start: date, days: int, dte: int, payload: Dict[str, Any], minimum: float) -> List[Dict[str, Any]]:
    all_bars = _bars(symbol, limit=520)
    by_date = {row["date"]: row for row in all_bars}
    rows: List[Dict[str, Any]] = []
    for offset in range(days):
        requested_date = start + timedelta(days=offset)
        entry = next((row for row in all_bars if row["date"] >= requested_date.isoformat()), None)
        if not entry or entry["date"] > (start + timedelta(days=days)).isoformat():
            continue
        entry_date = date.fromisoformat(entry["date"])
        evaluation = _evaluate(symbol, payload, entry_date)
        if not evaluation.get("included") or evaluation.get("score", 0) < minimum:
            continue
        exit_target = entry_date + timedelta(days=dte)
        exit_row = next((row for row in all_bars if row["date"] >= exit_target.isoformat()), None)
        exit_price = exit_row["close"] if exit_row else None
        direction = evaluation["direction"]
        outcome = "pending" if exit_price is None else ("winner" if (direction == "bull" and exit_price > entry["close"]) or (direction == "bear" and exit_price < entry["close"]) else "loser")
        move = None if exit_price is None else round((exit_price / entry["close"] - 1) * 100, 2)
        rows.append({"symbol": symbol, "trade_date": entry["date"], "direction": direction, "entry_close": entry["close"], "dte": dte, "exit_date": exit_row["date"] if exit_row else None, "exit_close": exit_price, "move_pct": move, "outcome": outcome, "score": evaluation["score"], "max_score": evaluation["max_score"], "detail": f"{direction.title()} reversal signal at entry close; evaluated against the first close on/after {exit_target.isoformat()}."})
    return rows


@systematic_reversal_bp.route("/")
def page():
    return render_template("systematic_reversal.html", watchlists=_watchlists())


@systematic_reversal_bp.route("/api/watchlists")
def watchlists():
    return jsonify({"watchlists": _watchlists()})


@systematic_reversal_bp.route("/api/run", methods=["POST"])
def run():
    payload = request.get_json(silent=True) or {}
    try:
        symbols = _symbols(int(payload.get("watchlist_id")))
    except (TypeError, ValueError):
        return jsonify({"error": "Choose a watchlist."}), 400
    if not symbols:
        return jsonify({"error": "Selected watchlist has no symbols."}), 400
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        rows = list(pool.map(lambda symbol: _evaluate(symbol, payload), symbols))
    minimum = max(0, float(payload.get("min_score", 0) or 0))
    results = [row for row in rows if row.get("included") and row.get("score", 0) >= minimum]
    results.sort(key=lambda row: row["score"], reverse=True)
    return jsonify({"results": results, "scanned": len(symbols), "min_score": minimum})


@systematic_reversal_bp.route("/api/backtest", methods=["POST"])
def backtest():
    payload = request.get_json(silent=True) or {}
    try:
        symbols = _symbols(int(payload.get("watchlist_id")))
        start = date.fromisoformat(str(payload.get("backtest_from"))[:10])
        days = max(1, min(90, int(payload.get("backtest_days", 5))))
        dte = max(1, min(365, int(payload.get("backtest_dte", 30))))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid watchlist, From date, run length, and DTE."}), 400
    if not symbols:
        return jsonify({"error": "The selected watchlist has no symbols."}), 400
    minimum = max(0, float(payload.get("min_score", 0) or 0))
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        grouped = list(pool.map(lambda symbol: _backtest_symbol(symbol, start, days, dte, payload, minimum), symbols))
    results = [row for group in grouped for row in group]
    results.sort(key=lambda row: (row["trade_date"], row["score"], row["symbol"]), reverse=True)
    winners = sum(row["outcome"] == "winner" for row in results)
    losers = sum(row["outcome"] == "loser" for row in results)
    pending = sum(row["outcome"] == "pending" for row in results)
    return jsonify({"results": results, "watchlist_symbols": len(symbols), "eligible_after_filters": len({row["symbol"] for row in results}), "summary": {"total_trades": len(results), "winners": winners, "losers": losers, "pending": pending, "win_rate": round(winners / (winners + losers) * 100, 1) if winners + losers else None}, "note": "Historical outcome uses daily closes and the first cached close on/after entry date plus calendar DTE. It is directional analysis, not option-contract P/L."})
