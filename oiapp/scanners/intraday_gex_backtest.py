"""Two-minute intraday GEX-regime strategy backtest.

This module deliberately uses saved underlying bars plus saved GEX/IV snapshots.
Historical option bid/ask ticks are not available locally, so option P&L is a
transparent Black-Scholes mid-price model; every result is marked modelled.
"""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from ..config import DB_PATH

intraday_backtest_bp = Blueprint("intraday_backtest_bp", __name__, url_prefix="/intraday-backtest")

_ET_OPEN = time(9, 30)
_ET_CLOSE = time(15, 58)


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _ensure_tables():
    # Create the three upstream caches on demand too, so opening this page
    # before any manual fetch gives a clear "missing data" result rather than
    # a SQLite "no such table" error.
    from ..services.intraday_price_cache import ensure_tables as _ensure_intraday_cache
    from .watchlist_manager import _ensure_tables as _ensure_watchlists
    from .spy_strategies import _ensure_gex_snapshot_table
    _ensure_intraday_cache()
    _ensure_watchlists()
    _ensure_gex_snapshot_table()
    con = _conn()
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS intraday_strategy_backtest_runs (
            run_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            config_json TEXT NOT NULL,
            summary_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS intraday_strategy_backtest_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            strategy TEXT NOT NULL,
            direction TEXT,
            entry_at TEXT NOT NULL,
            exit_at TEXT NOT NULL,
            entry_underlying REAL,
            exit_underlying REAL,
            strikes_json TEXT NOT NULL,
            entry_value REAL,
            exit_value REAL,
            pnl REAL,
            exit_reason TEXT,
            reentry_number INTEGER NOT NULL DEFAULT 0,
            assumptions TEXT DEFAULT 'modelled_black_scholes',
            FOREIGN KEY(run_id) REFERENCES intraday_strategy_backtest_runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_intraday_bt_trades_run
            ON intraday_strategy_backtest_trades(run_id);
        """)
        con.commit()
    finally:
        con.close()


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _option_value(spot, strike, years_left, iv, option_type):
    """European Black-Scholes model value; modelled, not a historical quote."""
    spot, strike = float(spot), float(strike)
    years_left = max(float(years_left), 1.0 / (365.0 * 24.0 * 60.0))
    iv = max(float(iv), 0.01)
    if spot <= 0 or strike <= 0:
        return 0.0
    root = iv * math.sqrt(years_left)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years_left) / root
    d2 = d1 - root
    if str(option_type).lower() == "call":
        return max(0.0, spot * _normal_cdf(d1) - strike * _normal_cdf(d2))
    return max(0.0, strike * _normal_cdf(-d2) - spot * _normal_cdf(-d1))


def _option_delta(spot, strike, years_left, iv, option_type):
    years_left = max(float(years_left), 1.0 / (365.0 * 24.0 * 60.0))
    root = max(float(iv), 0.01) * math.sqrt(years_left)
    d1 = (math.log(float(spot) / float(strike)) + 0.5 * iv * iv * years_left) / root
    call_delta = _normal_cdf(d1)
    return call_delta if str(option_type).lower() == "call" else call_delta - 1.0


def _years_left(ts: datetime) -> float:
    # Expiry-session option value decays to the 4 PM ET close.
    end = ts.replace(hour=16, minute=0, second=0, microsecond=0)
    return max((end - ts).total_seconds(), 60.0) / (365.0 * 24.0 * 3600.0)


def _bars(symbol: str, trade_date: str):
    con = _conn()
    try:
        rows = con.execute("""
            SELECT ts_et, open, high, low, close, volume
            FROM intraday_2m_price_cache
            WHERE symbol=? AND timeframe='2m' AND substr(ts_et,1,10)=?
              AND session='regular'
            ORDER BY ts_et
        """, (symbol, trade_date)).fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        try:
            ts = datetime.fromisoformat(row["ts_et"])
            out.append({
                "ts": ts, "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row["volume"] or 0), "ema13": None,
            })
        except (TypeError, ValueError):
            continue
    previous = None
    multiplier = 2.0 / 14.0
    for bar in out:
        previous = bar["close"] if previous is None else bar["close"] * multiplier + previous * (1.0 - multiplier)
        bar["ema13"] = previous
    return out


def _premarket(symbol: str, trade_date: str):
    con = _conn()
    try:
        row = con.execute("""
            SELECT premarket_high, premarket_low
            FROM premarket_levels WHERE symbol=? AND trade_date=?
        """, (symbol, trade_date)).fetchone()
        return (float(row[0]), float(row[1])) if row else None
    finally:
        con.close()


def _gex_context(symbol: str, trade_date: str, fallback_symbol=None):
    """Latest saved plan for the date; falls back to SPY only when requested."""
    symbols = [symbol]
    if fallback_symbol and fallback_symbol.upper() not in symbols:
        symbols.append(fallback_symbol.upper())
    con = _conn()
    try:
        for candidate in symbols:
            row = con.execute("""
                SELECT regime, payload_json, captured_at
                FROM gex_plan_snapshots
                WHERE symbol=? AND substr(COALESCE(captured_at,''),1,10)=?
                ORDER BY datetime(captured_at) DESC, id DESC LIMIT 1
            """, (candidate, trade_date)).fetchone()
            if not row:
                continue
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            regime = str(row["regime"] or
                         (payload.get("reliability") or {}).get("gex_regime") or "").upper()
            if not regime:
                total = ((payload.get("gex_info") or {}).get("total_gex"))
                regime = "POSITIVE" if total is not None and float(total) >= 0 else "NEGATIVE"
            iv = payload.get("iv_atm") or (payload.get("gex_info") or {}).get("iv_atm") or 0.20
            try:
                iv = float(iv)
                iv = iv / 100.0 if iv > 1.0 else iv
            except (TypeError, ValueError):
                iv = 0.20
            return {"regime": "POSITIVE" if "POS" in regime else "NEGATIVE",
                    "iv": min(max(iv, 0.05), 2.0), "source_symbol": candidate,
                    "captured_at": row["captured_at"]}
    finally:
        con.close()
    return None


def _inside_condor(bar, put_short, call_short):
    return bar["close"] > put_short and bar["close"] < call_short


def _make_trade(symbol, trade_date, strategy, direction, entry, exit_bar, strikes,
                entry_value, exit_value, exit_reason, reentry_number, contracts):
    multiplier = max(1, int(contracts)) * 100
    pnl = (exit_value - entry_value) * multiplier if strategy == "LONG_OPTION" else (entry_value - exit_value) * multiplier
    return {
        "symbol": symbol, "trade_date": trade_date, "strategy": strategy,
        "direction": direction, "entry_at": entry["ts"].isoformat(),
        "exit_at": exit_bar["ts"].isoformat(), "entry_underlying": round(entry["close"], 4),
        "exit_underlying": round(exit_bar["close"], 4), "strikes": strikes,
        "entry_value": round(entry_value, 4), "exit_value": round(exit_value, 4),
        "pnl": round(pnl, 2), "exit_reason": exit_reason,
        "reentry_number": reentry_number, "assumptions": "modelled_black_scholes",
    }


def _run_iron_condor(symbol, trade_date, bars, pm_high, pm_low, iv, cfg):
    """Positive-GEX: sell a 2-point-wide IC, one point outside premarket levels."""
    wing = float(cfg["wing_width"])
    offset = float(cfg["ic_offset"])
    contracts = cfg["contracts"]
    max_reentries = cfg["max_reentries"]
    put_short, call_short = pm_low - offset, pm_high + offset
    put_long, call_long = put_short - wing, call_short + wing
    trades, live, reentries = [], None, 0

    for bar in bars:
        clock = bar["ts"].time()
        if clock < _ET_OPEN or clock > _ET_CLOSE:
            continue
        if live is None:
            if reentries > max_reentries or not _inside_condor(bar, put_short, call_short):
                continue
            yrs = _years_left(bar["ts"])
            credit = (_option_value(bar["close"], put_short, yrs, iv, "put") +
                      _option_value(bar["close"], call_short, yrs, iv, "call") -
                      _option_value(bar["close"], put_long, yrs, iv, "put") -
                      _option_value(bar["close"], call_long, yrs, iv, "call"))
            if credit > 0.01:
                live = {"entry": bar, "credit": credit}
            continue

        yrs = _years_left(bar["ts"])
        close_cost = (_option_value(bar["close"], put_short, yrs, iv, "put") +
                      _option_value(bar["close"], call_short, yrs, iv, "call") -
                      _option_value(bar["close"], put_long, yrs, iv, "put") -
                      _option_value(bar["close"], call_long, yrs, iv, "call"))
        reason = None
        # 50% of credit retained is the target. Stops use the requested
        # one-point breach beyond either short strike.
        if close_cost <= live["credit"] * 0.50:
            reason = "50pct_profit_target"
        elif bar["high"] >= call_short + 1.0 or bar["low"] <= put_short - 1.0:
            reason = "short_strike_stop"
        elif clock >= _ET_CLOSE:
            reason = "time_exit"
        if reason:
            trades.append(_make_trade(
                symbol, trade_date, "IRON_CONDOR", "neutral", live["entry"], bar,
                {"put_long": put_long, "put_short": put_short,
                 "call_short": call_short, "call_long": call_long},
                live["credit"], close_cost, reason, reentries, contracts))
            if reason == "short_strike_stop":
                reentries += 1
            else:
                break
            live = None
    if live is not None:
        bar = bars[-1]
        yrs = _years_left(bar["ts"])
        close_cost = (_option_value(bar["close"], put_short, yrs, iv, "put") +
                      _option_value(bar["close"], call_short, yrs, iv, "call") -
                      _option_value(bar["close"], put_long, yrs, iv, "put") -
                      _option_value(bar["close"], call_long, yrs, iv, "call"))
        trades.append(_make_trade(symbol, trade_date, "IRON_CONDOR", "neutral", live["entry"], bar,
            {"put_long": put_long, "put_short": put_short, "call_short": call_short, "call_long": call_long},
            live["credit"], close_cost, "end_of_data", reentries, contracts))
    return trades


def _closest_delta_strike(spot, ts, iv, option_type, target_delta, step):
    lo, hi = max(step, spot * 0.80), spot * 1.20
    start, end = math.floor(lo / step) * step, math.ceil(hi / step) * step
    best = None
    strike = start
    while strike <= end + 1e-9:
        delta = _option_delta(spot, strike, _years_left(ts), iv, option_type)
        candidate = (abs(abs(delta) - target_delta), strike)
        if best is None or candidate < best:
            best = candidate
        strike += step
    return best[1] if best else round(spot / step) * step


def _run_long_options(symbol, trade_date, bars, pm_high, pm_low, iv, cfg):
    """Negative-GEX: post-break premarket retest/EMA13 continuation entries."""
    contracts, reentries = cfg["contracts"], 0
    max_reentries, target_delta, step = cfg["max_reentries"], cfg["target_delta"], cfg["strike_step"]
    trades, live = [], None
    broke_high, broke_low = False, False
    high_break_index, low_break_index = -1, -1

    for index, bar in enumerate(bars):
        clock = bar["ts"].time()
        if clock < _ET_OPEN or clock > _ET_CLOSE:
            continue
        previous = bars[index - 1] if index else None
        if not broke_high and bar["high"] >= pm_high:
            broke_high, high_break_index = True, index
        if not broke_low and bar["low"] <= pm_low:
            broke_low, low_break_index = True, index

        if live is None:
            if reentries > max_reentries:
                break
            bullish_retest = broke_high and index > high_break_index and bar["low"] <= pm_high and bar["close"] >= pm_high
            bearish_retest = broke_low and index > low_break_index and bar["high"] >= pm_low and bar["close"] <= pm_low
            bullish_ema = broke_high and previous and previous["close"] <= previous["ema13"] and bar["close"] > bar["ema13"]
            bearish_ema = broke_low and previous and previous["close"] >= previous["ema13"] and bar["close"] < bar["ema13"]
            direction = "call" if bullish_retest or bullish_ema else "put" if bearish_retest or bearish_ema else None
            if direction:
                strike = _closest_delta_strike(bar["close"], bar["ts"], iv, direction, target_delta, step)
                debit = _option_value(bar["close"], strike, _years_left(bar["ts"]), iv, direction)
                if debit > 0.01:
                    live = {"entry": bar, "strike": strike, "direction": direction, "debit": debit}
            continue

        exit_reason = None
        # User's requested exits: bullish position exits once candle high crosses
        # below EMA13; bearish position exits once candle low crosses above EMA13.
        if live["direction"] == "call" and previous and previous["high"] >= previous["ema13"] and bar["high"] < bar["ema13"]:
            exit_reason = "ema13_high_cross_below"
        elif live["direction"] == "put" and previous and previous["low"] <= previous["ema13"] and bar["low"] > bar["ema13"]:
            exit_reason = "ema13_low_cross_above"
        elif clock >= _ET_CLOSE:
            exit_reason = "time_exit"
        if exit_reason:
            value = _option_value(bar["close"], live["strike"], _years_left(bar["ts"]), iv, live["direction"])
            trades.append(_make_trade(symbol, trade_date, "LONG_OPTION", live["direction"], live["entry"], bar,
                {"strike": live["strike"], "target_abs_delta": target_delta},
                live["debit"], value, exit_reason, reentries, contracts))
            if exit_reason != "time_exit":
                reentries += 1
                # Rearm only after another premarket-level break, avoiding
                # immediate churn on the same failed breakout.
                broke_high, broke_low = False, False
            live = None
    if live is not None:
        bar = bars[-1]
        value = _option_value(bar["close"], live["strike"], _years_left(bar["ts"]), iv, live["direction"])
        trades.append(_make_trade(symbol, trade_date, "LONG_OPTION", live["direction"], live["entry"], bar,
            {"strike": live["strike"], "target_abs_delta": target_delta},
            live["debit"], value, "end_of_data", reentries, contracts))
    return trades


def _symbols(payload):
    supplied = [s.strip().upper() for s in str(payload.get("symbols") or "").replace("\n", ",").split(",") if s.strip()]
    if supplied:
        return list(dict.fromkeys(supplied))
    watchlist_id = payload.get("watchlist_id")
    if not watchlist_id:
        return []
    con = _conn()
    try:
        rows = con.execute("SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                           (int(watchlist_id),)).fetchall()
        return [str(row[0]).upper() for row in rows]
    finally:
        con.close()


def _dates(start, end):
    cursor, finish = date.fromisoformat(start), date.fromisoformat(end)
    if finish < cursor:
        raise ValueError("date_to must be on or after date_from")
    if (finish - cursor).days > 1095:
        raise ValueError("date range is limited to 1,096 calendar days")
    output = []
    while cursor <= finish:
        if cursor.weekday() < 5:
            output.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return output


def _save(run_id, config, summary, trades):
    _ensure_tables()
    con = _conn()
    try:
        con.execute("INSERT INTO intraday_strategy_backtest_runs(run_id,created_at,config_json,summary_json) VALUES (?,?,?,?)",
                    (run_id, datetime.now().isoformat(timespec="seconds"), json.dumps(config), json.dumps(summary)))
        con.executemany("""
            INSERT INTO intraday_strategy_backtest_trades
            (run_id,symbol,trade_date,strategy,direction,entry_at,exit_at,entry_underlying,exit_underlying,
             strikes_json,entry_value,exit_value,pnl,exit_reason,reentry_number,assumptions)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [(run_id, t["symbol"], t["trade_date"], t["strategy"], t["direction"], t["entry_at"], t["exit_at"],
               t["entry_underlying"], t["exit_underlying"], json.dumps(t["strikes"]), t["entry_value"], t["exit_value"],
               t["pnl"], t["exit_reason"], t["reentry_number"], t["assumptions"]) for t in trades])
        con.commit()
    finally:
        con.close()


def run_backtest(payload):
    _ensure_tables()
    mode = str(payload.get("mode") or "both").lower()
    if mode not in {"ic", "buying", "both"}:
        raise ValueError("mode must be IC, buying, or both")
    symbols = _symbols(payload)
    if not symbols:
        raise ValueError("Choose a watchlist or enter comma-separated symbols")
    cfg = {
        "mode": mode, "contracts": max(1, int(payload.get("contracts") or 1)),
        "max_reentries": max(0, int(payload.get("max_reentries") or 0)),
        "wing_width": max(0.5, float(payload.get("wing_width") or 2.0)),
        "ic_offset": max(0.0, float(payload.get("ic_offset") or 1.0)),
        "target_delta": min(0.49, max(0.05, float(payload.get("target_delta") or 0.30))),
        "strike_step": max(0.01, float(payload.get("strike_step") or 1.0)),
        "gex_symbol": str(payload.get("gex_symbol") or "").upper().strip(),
        "date_from": str(payload.get("date_from") or ""), "date_to": str(payload.get("date_to") or ""),
        "symbols": symbols,
    }
    if not cfg["date_from"] or not cfg["date_to"]:
        raise ValueError("date_from and date_to are required")
    skipped, trades = [], []
    for trade_date in _dates(cfg["date_from"], cfg["date_to"]):
        for symbol in symbols:
            bars, pm = _bars(symbol, trade_date), _premarket(symbol, trade_date)
            gex = _gex_context(symbol, trade_date, cfg["gex_symbol"] or None)
            if not bars:
                skipped.append({"symbol": symbol, "date": trade_date, "reason": "missing_regular_2m_bars"})
                continue
            if not pm:
                skipped.append({"symbol": symbol, "date": trade_date, "reason": "missing_premarket_levels"})
                continue
            if not gex:
                skipped.append({"symbol": symbol, "date": trade_date, "reason": "missing_saved_gex_plan"})
                continue
            if gex["regime"] == "POSITIVE" and mode in {"ic", "both"}:
                trades.extend(_run_iron_condor(symbol, trade_date, bars, pm[0], pm[1], gex["iv"], cfg))
            elif gex["regime"] == "NEGATIVE" and mode in {"buying", "both"}:
                trades.extend(_run_long_options(symbol, trade_date, bars, pm[0], pm[1], gex["iv"], cfg))
    total = round(sum(t["pnl"] for t in trades), 2)
    winners = [t for t in trades if t["pnl"] > 0]
    summary = {
        "run_id": uuid.uuid4().hex, "model": "Black-Scholes mid-price model using saved GEX-plan IV; not historical option bid/ask",
        "trades": len(trades), "winners": len(winners), "losers": len(trades) - len(winners),
        "win_rate_pct": round(100.0 * len(winners) / len(trades), 2) if trades else 0.0,
        "total_pnl": total, "average_pnl": round(total / len(trades), 2) if trades else 0.0,
        "skipped": skipped, "skipped_count": len(skipped),
    }
    _save(summary["run_id"], cfg, summary, trades)
    return {"ok": True, "config": cfg, "summary": summary, "trades": trades}


@intraday_backtest_bp.route("/")
def intraday_backtest_page():
    return render_template("intraday_backtest.html")


@intraday_backtest_bp.route("/api/watchlists")
def intraday_backtest_watchlists():
    con = _conn()
    try:
        rows = con.execute("""
            SELECT w.id,w.name,COUNT(ws.id) symbol_count
            FROM watchlists w LEFT JOIN watchlist_symbols ws ON ws.watchlist_id=w.id
            GROUP BY w.id ORDER BY w.name
        """).fetchall()
        return jsonify({"watchlists": [dict(row) for row in rows]})
    finally:
        con.close()


@intraday_backtest_bp.route("/api/run", methods=["POST"])
def intraday_backtest_run():
    try:
        return jsonify(run_backtest(request.get_json(force=True) or {}))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@intraday_backtest_bp.route("/api/runs/<run_id>")
def intraday_backtest_run_detail(run_id):
    con = _conn()
    try:
        run = con.execute("SELECT * FROM intraday_strategy_backtest_runs WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            return jsonify({"error": "Run not found"}), 404
        rows = con.execute("SELECT * FROM intraday_strategy_backtest_trades WHERE run_id=? ORDER BY trade_date,symbol,entry_at",
                           (run_id,)).fetchall()
        return jsonify({"run": dict(run), "trades": [dict(row) for row in rows]})
    finally:
        con.close()
