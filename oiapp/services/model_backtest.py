"""Configurable Black-Scholes strategy backtester.

This engine intentionally models option entry prices from historical spot and
IV. It is not a historical-fill simulator unless the caller supplies actual
historical bid/ask data; every result carries its pricing source.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

from ..config import DB_PATH
from ..scanners.sqlite_store import get_market_data_store

model_backtest_bp = Blueprint("model_backtest", __name__, url_prefix="/model-backtest")
_jobs: Dict[str, Dict[str, Any]] = {}
_lock = threading.RLock()


@model_backtest_bp.route("/")
def page():
    return render_template("model_backtest.html")


def _f(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _d(v: Any, default: Optional[date] = None) -> date:
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date() if v else (default or date.today())


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs(spot: float, strike: float, years: float, iv: float, call: bool, rate: float, dividend: float) -> float:
    if spot <= 0 or strike <= 0 or years <= 0 or iv <= 0:
        return max(spot - strike, 0.0) if call else max(strike - spot, 0.0)
    srt = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * iv * iv) * years) / srt
    d2 = d1 - srt
    if call:
        return spot * math.exp(-dividend * years) * _cdf(d1) - strike * math.exp(-rate * years) * _cdf(d2)
    return strike * math.exp(-rate * years) * _cdf(-d2) - spot * math.exp(-dividend * years) * _cdf(-d1)


def _delta(spot: float, strike: float, years: float, iv: float, call: bool, rate: float, dividend: float) -> float:
    if spot <= 0 or strike <= 0 or years <= 0 or iv <= 0:
        return 0.5 if call else -0.5
    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * iv * iv) * years) / (iv * math.sqrt(years))
    return math.exp(-dividend * years) * (_cdf(d1) if call else _cdf(d1) - 1.0)


def _expiry_for(entry: date, rule: Dict[str, Any]) -> date:
    kind = str(rule.get("type") or "next_friday").lower()
    if kind == "fixed_dte":
        return entry + timedelta(days=int(rule.get("days") or 7))
    if kind == "calendar_date":
        return _d(rule.get("date"), entry + timedelta(days=7))
    if kind == "days_forward":
        return entry + timedelta(days=int(rule.get("days") or 5))
    # Strictly following Friday; if entry itself is Friday, use the next one.
    days = (4 - entry.weekday()) % 7 or 7
    return entry + timedelta(days=days)


def _entries(days: List[date], rule: Dict[str, Any]) -> List[date]:
    kind = str(rule.get("type") or "every_friday").lower()
    if kind in {"every_day", "daily"}:
        return days
    weekday = int(rule.get("weekday", 4))
    return [d for d in days if d.weekday() == weekday]


def _normal_iv(value: Any) -> Optional[float]:
    x = _f(value)
    if x is None or x <= 0:
        return None
    return x / 100.0 if x > 3.0 else x


def _realized_vol(closes: List[float], window: int = 20) -> Optional[float]:
    if len(closes) < window + 1:
        return None
    vals = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - window, len(closes)) if closes[i] > 0 and closes[i - 1] > 0]
    if len(vals) < 2:
        return None
    avg = sum(vals) / len(vals)
    return math.sqrt(sum((x - avg) ** 2 for x in vals) / (len(vals) - 1)) * math.sqrt(252.0)


def _historical_iv(symbol: str, entry: date, expiry: date) -> Optional[float]:
    try:
        with sqlite3.connect(DB_PATH) as con:
            snap = con.execute("""SELECT MAX(date) FROM options
                WHERE symbol=? AND date<=? AND expiration=?""",
                (symbol, entry.isoformat(), expiry.isoformat())).fetchone()
            if not snap or not snap[0]:
                return None
            row = con.execute("""SELECT AVG(iv) FROM options
                WHERE symbol=? AND date=? AND expiration=? AND iv IS NOT NULL AND iv>0""",
                (symbol, snap[0], expiry.isoformat())).fetchone()
            return _normal_iv(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def _load_iv_history(symbol: str, start: date, end: date) -> Dict[str, List[Any]]:
    """Load all usable IV snapshots once, then resolve them in memory."""
    history: Dict[str, List[Any]] = {}
    try:
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute("""SELECT date, expiration, AVG(iv) FROM options
                WHERE symbol=? AND date>=? AND date<=? AND iv IS NOT NULL AND iv>0
                GROUP BY date, expiration ORDER BY expiration, date""",
                (symbol, start.isoformat(), end.isoformat())).fetchall()
        for day, expiry, value in rows:
            iv = _normal_iv(value)
            if iv is not None:
                history.setdefault(str(expiry)[:10], []).append((str(day)[:10], iv))
        return history
    except Exception:
        return {}


def _iv_from_history(history: Dict[str, List[Any]], entry: date, expiry: date) -> Optional[float]:
    rows = history.get(expiry.isoformat()) or []
    days = [x[0] for x in rows]
    i = bisect_right(days, entry.isoformat()) - 1
    return rows[i][1] if i >= 0 else None


def _strike(spot: float, years: float, iv: float, side: str, rule: Dict[str, Any], rate: float, dividend: float) -> float:
    kind = str(rule.get("type") or "atm").lower()
    if kind == "absolute":
        return float(rule["strike"])
    if kind in {"dollar", "distance"}:
        distance = abs(float(rule.get("value") or rule.get("distance") or 0))
        return spot + distance if side == "call" else spot - distance
    if kind in {"pct_otm", "percent_otm"}:
        pct = abs(float(rule.get("value") or rule.get("pct") or 0)) / 100.0
        return spot * (1.0 + pct) if side == "call" else spot * (1.0 - pct)
    if kind == "expected_move":
        mult = float(rule.get("multiplier") or 1.0)
        move = spot * iv * math.sqrt(years) * mult
        return spot + move if side == "call" else spot - move
    if kind == "delta":
        target = abs(float(rule.get("value") or rule.get("delta") or 0.16))
        candidates = [spot * (1 + x / 1000.0) for x in range(1, 801)] if side == "call" else [spot * (1 - x / 1000.0) for x in range(1, 801)]
        return min(candidates, key=lambda k: abs(abs(_delta(spot, k, years, iv, side == "call", rate, dividend)) - target))
    return spot


def _leg(action: str, option: str, strike: float, premium: float) -> Dict[str, Any]:
    return {"side": action, "option_type": option, "strike": round(strike, 4), "entry_price": round(premium, 4)}


def _build_legs(strategy: Dict[str, Any], spot: float, years: float, iv: float, rate: float, dividend: float) -> List[Dict[str, Any]]:
    name = str(strategy.get("type") or "iron_fly").lower()
    rate, dividend = float(rate), float(dividend)
    legs: List[Dict[str, Any]] = []
    def px(option: str, strike: float) -> float:
        return _bs(spot, strike, years, iv, option == "call", rate, dividend)
    if name == "custom":
        for spec in strategy.get("legs") or []:
            option = str(spec.get("option") or spec.get("option_type") or "call").lower()
            strike = _strike(spot, years, iv, option, spec.get("strike_rule") or spec, rate, dividend)
            legs.append(_leg(str(spec.get("side") or "sell").lower(), option, strike, px(option, strike)))
        return legs
    if name in {"iron_fly", "ironfly"}:
        short = _strike(spot, years, iv, "call", strategy.get("short_strike") or {"type": "atm"}, rate, dividend)
        width = abs(float(strategy.get("wing_width") or 15.0))
        legs = [_leg("sell", "put", short, px("put", short)), _leg("sell", "call", short, px("call", short)),
                _leg("buy", "put", short - width, px("put", short - width)), _leg("buy", "call", short + width, px("call", short + width))]
    elif name in {"iron_condor", "condor"}:
        put_rule = strategy.get("short_put") or {"type": "delta", "value": 0.16}
        call_rule = strategy.get("short_call") or {"type": "delta", "value": 0.16}
        sp = _strike(spot, years, iv, "put", put_rule, rate, dividend)
        sc = _strike(spot, years, iv, "call", call_rule, rate, dividend)
        pw = abs(float(strategy.get("put_wing_width") or strategy.get("wing_width") or 15.0))
        cw = abs(float(strategy.get("call_wing_width") or strategy.get("wing_width") or 15.0))
        legs = [_leg("sell", "put", sp, px("put", sp)), _leg("buy", "put", sp - pw, px("put", sp - pw)),
                _leg("sell", "call", sc, px("call", sc)), _leg("buy", "call", sc + cw, px("call", sc + cw))]
    elif name in {"strangle", "short_strangle"}:
        pr = strategy.get("short_put") or {"type": "delta", "value": 0.16}
        cr = strategy.get("short_call") or {"type": "delta", "value": 0.16}
        sp, sc = _strike(spot, years, iv, "put", pr, rate, dividend), _strike(spot, years, iv, "call", cr, rate, dividend)
        legs = [_leg("sell", "put", sp, px("put", sp)), _leg("sell", "call", sc, px("call", sc))]
    elif name in {"straddle", "short_straddle"}:
        k = _strike(spot, years, iv, "call", strategy.get("short_strike") or {"type": "atm"}, rate, dividend)
        legs = [_leg("sell", "put", k, px("put", k)), _leg("sell", "call", k, px("call", k))]
    elif name in {"bull_put", "bull_put_spread"}:
        sp = _strike(spot, years, iv, "put", strategy.get("short_put") or {"type": "delta", "value": 0.20}, rate, dividend)
        width = abs(float(strategy.get("wing_width") or 15.0))
        legs = [_leg("sell", "put", sp, px("put", sp)), _leg("buy", "put", sp - width, px("put", sp - width))]
    elif name in {"bear_call", "bear_call_spread"}:
        sc = _strike(spot, years, iv, "call", strategy.get("short_call") or {"type": "delta", "value": 0.20}, rate, dividend)
        width = abs(float(strategy.get("wing_width") or 15.0))
        legs = [_leg("sell", "call", sc, px("call", sc)), _leg("buy", "call", sc + width, px("call", sc + width))]
    else:
        raise ValueError(f"Unsupported strategy type: {name}")
    return legs


def _payoff(legs: List[Dict[str, Any]], spot: float) -> float:
    value = 0.0
    for leg in legs:
        k = leg["strike"]
        intrinsic = max(spot - k, 0) if leg["option_type"] == "call" else max(k - spot, 0)
        value += (intrinsic - leg["entry_price"]) * (1 if leg["side"] == "buy" else -1)
    return value


def _stats(legs: List[Dict[str, Any]], spot: float) -> Dict[str, Any]:
    grid = [max(0.01, spot * 0.5) + spot * i / 200 for i in range(201)]
    vals = [_payoff(legs, x) for x in grid]
    credit = sum(x["entry_price"] * (1 if x["side"] == "sell" else -1) for x in legs)
    lo, hi = [], []
    for a, b, x, y in zip(vals, vals[1:], grid, grid[1:]):
        if a <= 0 < b or a >= 0 > b:
            frac = abs(a) / (abs(a) + abs(b)) if abs(a) + abs(b) else 0
            cross = x + frac * (y - x)
            (lo if a <= 0 < b else hi).append(cross)
    maximum, minimum = max(vals), min(vals)
    return {"credit": round(credit, 4), "max_profit": round(maximum, 4), "max_loss": round(minimum, 4),
            "rr": round(maximum / abs(minimum), 4) if minimum < 0 and maximum > 0 else None,
            "breakeven_lower": round(min(lo), 4) if lo else None, "breakeven_upper": round(max(hi), 4) if hi else None}


def _ensure_tables() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS model_backtest_runs (
          run_id TEXT PRIMARY KEY, run_name TEXT, status TEXT, created_at TEXT,
          config_json TEXT, summary_json TEXT, progress REAL DEFAULT 0, error_text TEXT
        );
        CREATE TABLE IF NOT EXISTS model_backtest_trades (
          run_id TEXT NOT NULL, trade_no INTEGER NOT NULL, trade_json TEXT NOT NULL,
          PRIMARY KEY(run_id, trade_no), FOREIGN KEY(run_id) REFERENCES model_backtest_runs(run_id) ON DELETE CASCADE
        );
        """)


def _config(raw: Dict[str, Any]) -> Dict[str, Any]:
    end = _d(raw.get("end_date"), date.today()); start = _d(raw.get("start_date"), end - timedelta(days=365))
    if start >= end:
        raise ValueError("start_date must be before end_date")
    symbols = raw.get("symbols") or raw.get("symbol") or []
    if isinstance(symbols, str): symbols = [x.strip().upper() for x in symbols.split(",") if x.strip()]
    if not symbols: raise ValueError("symbol or symbols is required")
    entry_rule = raw.get("entry_rule") or {"type": "every_friday"}
    expiry_rule = raw.get("expiry_rule") or {"type": "next_friday"}
    strategy = raw.get("strategy") or {"type": "iron_fly", "wing_width": 15}
    if isinstance(entry_rule, str): entry_rule = {"type": entry_rule}
    if isinstance(expiry_rule, str): expiry_rule = {"type": expiry_rule}
    if isinstance(strategy, str): strategy = {"type": strategy}
    return {**raw, "start_date": start.isoformat(), "end_date": end.isoformat(), "symbols": symbols,
            "entry_rule": entry_rule, "expiry_rule": expiry_rule, "strategy": strategy,
            "rate": float(raw.get("rate", 0.04)), "dividend": float(raw.get("dividend", 0.0)),
            "iv_source": raw.get("iv_source", "historical_iv"), "rv_window": int(raw.get("rv_window", 20)),
            "slippage_pct": float(raw.get("slippage_pct", 0.0)), "contract_multiplier": int(raw.get("contract_multiplier", 100))}


def _run(run_id: str, cfg: Dict[str, Any]) -> None:
    try:
        all_trades: List[Dict[str, Any]] = []
        store = get_market_data_store()
        for symbol in cfg["symbols"]:
            bars = store.get_bars(symbol, _d(cfg["start_date"]) - timedelta(days=cfg["rv_window"] + 5), _d(cfg["end_date"]) + timedelta(days=15), "1d")
            if bars is None or bars.empty: continue
            closes = [float(x) for x in bars["Close"].tolist()]
            dates = [x.date() for x in bars.index]
            date_to_idx = {day: idx for idx, day in enumerate(dates)}
            iv_history = _load_iv_history(symbol, _d(cfg["start_date"]) - timedelta(days=cfg["rv_window"] + 5), _d(cfg["end_date"]))
            entry_days = _entries([x for x in dates if _d(cfg["start_date"]) <= x <= _d(cfg["end_date"])], cfg["entry_rule"])
            for i, entry in enumerate(entry_days):
                try:
                    entry_idx = date_to_idx[entry]; expiry = _expiry_for(entry, cfg["expiry_rule"])
                    expiry_idx = bisect_left(dates, expiry)
                    if expiry_idx >= len(dates): continue
                    expiry_date = dates[expiry_idx]
                    spot = closes[entry_idx]; years = max((expiry_date - entry).days, 1) / 365.0
                    iv = _iv_from_history(iv_history, entry, expiry_date)
                    pricing_source = "HISTORICAL_IV_BS"
                    if iv is None or cfg["iv_source"] in {"realized_volatility", "rv"}:
                        iv = _realized_vol(closes[:entry_idx + 1], cfg["rv_window"])
                        pricing_source = "REALIZED_VOLATILITY_BS"
                    if iv is None or iv <= 0: continue
                    legs = _build_legs(cfg["strategy"], spot, years, iv, cfg["rate"], cfg["dividend"])
                    slip = max(0.0, cfg["slippage_pct"]) / 100.0
                    for leg in legs: leg["entry_price"] = round(leg["entry_price"] * (1 + slip if leg["side"] == "buy" else 1 - slip), 4)
                    stats = _stats(legs, spot); expiry_spot = closes[expiry_idx]; pnl = _payoff(legs, expiry_spot)
                    risk = abs(stats["max_loss"]); pop = None
                    if stats["breakeven_lower"] and stats["breakeven_upper"]:
                        st = iv * math.sqrt(years)
                        pop = _cdf((math.log(stats["breakeven_upper"] / spot) + 0.5 * iv * iv * years) / st) - _cdf((math.log(stats["breakeven_lower"] / spot) + 0.5 * iv * iv * years) / st)
                    all_trades.append({"symbol": symbol, "entry_date": entry.isoformat(), "expiry_date": expiry_date.isoformat(), "entry_spot": round(spot, 4), "expiry_spot": round(expiry_spot, 4), "iv": round(iv, 6), "pricing_source": pricing_source, "legs": legs, **stats, "model_pop": round(pop, 4) if pop is not None else None, "pnl_per_share": round(pnl, 4), "pnl_dollars": round(pnl * cfg["contract_multiplier"], 2), "realized_r": round(pnl / risk, 4) if risk else None, "outcome": "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"})
                except Exception:
                    continue
                with _lock: _jobs[run_id]["progress"] = round((i + 1) / max(1, len(entry_days)) * 100, 2)
        wins = [x for x in all_trades if x["outcome"] == "WIN"]; pnls = [x["pnl_dollars"] for x in all_trades]
        rs = [x["realized_r"] for x in all_trades if x.get("realized_r") is not None]
        summary = {"trades": len(all_trades), "wins": len(wins), "losses": sum(x["outcome"] == "LOSS" for x in all_trades), "win_rate": len(wins) / len(all_trades) if all_trades else None, "total_pnl_dollars": sum(pnls), "average_pnl_dollars": sum(pnls) / len(pnls) if pnls else None, "average_winner_dollars": sum(x["pnl_dollars"] for x in wins) / len(wins) if wins else None, "average_loser_dollars": sum(x["pnl_dollars"] for x in all_trades if x["outcome"] == "LOSS") / max(1, sum(x["outcome"] == "LOSS" for x in all_trades)), "expectancy_r": sum(rs) / len(rs) if rs else None, "pricing_warning": "Premiums are Black-Scholes theoretical prices, not historical fills."}
        _ensure_tables()
        with sqlite3.connect(DB_PATH) as con:
            con.executemany("INSERT INTO model_backtest_trades(run_id,trade_no,trade_json) VALUES(?,?,?)", [(run_id, i, json.dumps(t, allow_nan=False)) for i, t in enumerate(all_trades)])
            con.execute("UPDATE model_backtest_runs SET status='completed',progress=100,summary_json=? WHERE run_id=?", (json.dumps(summary, allow_nan=False), run_id))
        with _lock: _jobs[run_id].update({"status": "completed", "summary": summary})
    except Exception as exc:
        _ensure_tables()
        with sqlite3.connect(DB_PATH) as con: con.execute("UPDATE model_backtest_runs SET status='failed',error_text=? WHERE run_id=?", (str(exc), run_id))
        with _lock: _jobs[run_id].update({"status": "failed", "error": str(exc)})


@model_backtest_bp.route("/api/run", methods=["POST"])
def start():
    cfg = _config(request.get_json(force=True) or {})
    _ensure_tables(); run_id = uuid.uuid4().hex; now = datetime.utcnow().isoformat(timespec="seconds")
    name = cfg.get("run_name") or f"Model backtest {cfg['symbols']} {cfg['start_date']} to {cfg['end_date']}"
    with sqlite3.connect(DB_PATH) as con: con.execute("INSERT INTO model_backtest_runs(run_id,run_name,status,created_at,config_json) VALUES(?,?,?,?,?)", (run_id, name, "running", now, json.dumps(cfg, allow_nan=False)))
    with _lock: _jobs[run_id] = {"status": "running", "progress": 0}
    ThreadPoolExecutor(max_workers=1).submit(_run, run_id, cfg)
    return jsonify({"ok": True, "run_id": run_id, "status": "running"}), 202


@model_backtest_bp.route("/api/runs")
def runs():
    _ensure_tables()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row; rows = con.execute("SELECT run_id,run_name,status,created_at,progress,summary_json,error_text FROM model_backtest_runs ORDER BY created_at DESC LIMIT 25").fetchall()
    return jsonify([{**dict(r), "summary": json.loads(r["summary_json"] or "null")} for r in rows])


@model_backtest_bp.route("/api/runs/<run_id>")
def detail(run_id: str):
    _ensure_tables()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row; r = con.execute("SELECT * FROM model_backtest_runs WHERE run_id=?", (run_id,)).fetchone()
        if not r: return jsonify({"ok": False, "error": "run not found"}), 404
        trades = [json.loads(x[0]) for x in con.execute("SELECT trade_json FROM model_backtest_trades WHERE run_id=? ORDER BY trade_no", (run_id,)).fetchall()]
    out = dict(r); out["config"] = json.loads(out.pop("config_json") or "{}"); out["summary"] = json.loads(out.pop("summary_json") or "null"); out["trades"] = trades
    with _lock: out.update(_jobs.get(run_id, {}))
    return jsonify(out)
