"""
weekly_plan_backtest.py -- backtests the Monday-10am SPY weekly plan
(oiapp/scanners/spy_strategies.py::api_weekly, scheduled Mondays via
'weekly_plan_snapshot' in scheduled_jobs.py).

WHY TWO TIERS, NOT ONE:
The live weekly plan's composite score leans on live OI walls and current
IV -- neither is stored historically in this DB (only current/recent
snapshots exist). That means "what would the FULL plan have said on any
past Monday" cannot be honestly reconstructed -- faking it with today's
OI/IV applied to old price data would silently launder present-day
information into a supposedly historical backtest. Rather than do that:

  TIER 1 -- technical_bias_backtest(): fully historical, zero lookahead.
  Reconstructs just the TECHNICAL/PRICE half of the plan (regime, RSIDiff90,
  slope, ADX) using only price_cache data available AS OF each past Monday,
  and grades it against the realized Monday->Friday move. Expected-move is
  approximated from realized volatility (ATR-based), NOT the real IV-based
  number the live job uses -- labeled as such in every output.

  TIER 2 -- grade_pending_live_snapshots(): grades the REAL saved weekly
  plan snapshots your scheduled job already produces every Monday (full
  composite score, real OI walls, real IV) against what actually happened
  by that week's Friday close, once Friday has passed. This is a forward-
  accumulating, zero-approximation track record -- it just takes real
  weeks to build up, since true historical OI/IV can't be reconstructed.
"""
import sqlite3
import json
import math as _math
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request

weekly_backtest_bp = Blueprint("weekly_backtest_bp", __name__, url_prefix="/scanner/weekly-plan-backtest")
from ..config import DB_PATH as _OIAPP_DB_PATH
DB_PATH = _OIAPP_DB_PATH

try:
    from .scanner_builder import _parse_query as _sb_parse_query, _eval as _sb_eval
except Exception:
    _sb_parse_query = None
    _sb_eval = None

from .candle_context_scanner import _get_price_history, _safe


def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c


def _ensure_tables():
    con = _conn()
    con.execute("""
        CREATE TABLE IF NOT EXISTS weekly_plan_backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_run_name TEXT UNIQUE,
            symbol TEXT,
            monday_date TEXT,
            expiry_date TEXT,
            spot_monday REAL,
            close_expiry REAL,
            realized_pct REAL,
            bias TEXT,
            confidence REAL,
            plan_score REAL,
            expected_move_value REAL,
            direction_correct INTEGER,
            within_expected_move INTEGER,
            graded_at TEXT
        )
    """)
    con.commit()
    con.close()


def _ev(expr, ctx, shift=0, tf="1d"):
    if _sb_parse_query is None or _sb_eval is None:
        return None
    try:
        return _sb_eval(_sb_parse_query(expr), ctx, shift=shift, tf_default=tf)
    except Exception:
        return None


# ── TIER 1: fully historical technical-bias backtest ───────────────────────

def _build_ctx_through(full_hist, end_idx):
    """ctx containing ONLY bars up to and including end_idx -- this is what
    makes it zero-lookahead: nothing after the simulated Monday is visible
    to the evaluator."""
    return {
        "timeframes": {"1d": {"series": {
            "open": full_hist["open"].iloc[:end_idx + 1],
            "high": full_hist["high"].iloc[:end_idx + 1],
            "low": full_hist["low"].iloc[:end_idx + 1],
            "close": full_hist["close"].iloc[:end_idx + 1],
            "volume": full_hist["volume"].iloc[:end_idx + 1],
        }}}
    }


def _atr14(full_hist, end_idx, period=14):
    """Standard ATR, computed directly from OHLC -- no DSL primitive named
    plain ATR() exists in scanner_builder.py (only ATRCompression, a score,
    not the raw value), so this is computed here rather than guessing at
    a primitive name that doesn't exist."""
    start = max(1, end_idx - period * 3)
    highs = full_hist["high"].iloc[start:end_idx + 1]
    lows = full_hist["low"].iloc[start:end_idx + 1]
    closes = full_hist["close"].iloc[start:end_idx + 1]
    prev_close = closes.shift(1)
    tr = (highs - lows).combine((highs - prev_close).abs(), max).combine((lows - prev_close).abs(), max)
    atr = tr.rolling(window=period, min_periods=period).mean()
    val = atr.iloc[-1]
    return float(val) if val == val else None  # NaN check


def technical_bias_backtest(symbol="SPY", weeks=104, min_bars=120):
    """Walks back through `weeks` historical Mondays. For each, computes a
    technical bias (regime + RSIDiff90 + slope) using ONLY data through
    that Monday's close, then grades it against the realized move to that
    week's Friday (or last trading day before the next Monday, for holiday
    weeks). Returns per-week rows plus aggregate accuracy stats."""
    full_hist = _get_price_history(symbol, min_days=weeks * 7 + 400)
    if full_hist is None:
        return {"error": f"no cached price history for {symbol}"}

    import pandas as pd
    dates = pd.to_datetime(full_hist["date"])
    n = len(dates)
    rows = []

    for i in range(min_bars, n):
        if dates.iloc[i].weekday() != 0:  # only simulate Mondays
            continue
        # Find that week's Friday (or last trading day before next Monday)
        friday_idx = None
        for j in range(i + 1, min(i + 6, n)):
            if dates.iloc[j].weekday() == 4 or (j + 1 < n and dates.iloc[j + 1].weekday() == 0):
                friday_idx = j
                if dates.iloc[j].weekday() == 4:
                    break
        if friday_idx is None or friday_idx <= i:
            continue  # incomplete week (e.g. most recent Monday, no Friday yet)

        ctx = _build_ctx_through(full_hist, i)
        rsidiff = _ev('RSIDiff90("1d")', ctx)
        slope = _ev('SlopeDegPerBar(close, 5, "1d")', ctx)
        uae_bull = _ev('UAEBull("1d")', ctx)
        uae_bear = _ev('UAEBear("1d")', ctx)
        atr = _atr14(full_hist, i)  # historical-vol proxy for expected move

        if uae_bull:
            bias = "bull"
        elif uae_bear:
            bias = "bear"
        elif rsidiff is not None and slope is not None and rsidiff > 5 and slope > 0:
            bias = "bull"
        elif rsidiff is not None and slope is not None and rsidiff < -5 and slope < 0:
            bias = "bear"
        else:
            bias = "neutral"

        spot_monday = float(full_hist["close"].iloc[i])
        close_friday = float(full_hist["close"].iloc[friday_idx])
        realized = close_friday - spot_monday
        realized_pct = round((realized / spot_monday) * 100, 2) if spot_monday else None

        expected_move_proxy = float(atr) * 1.8 if atr is not None else None  # rough weekly scaling of daily ATR

        direction_correct = None
        if bias in ("bull", "bear"):
            direction_correct = (bias == "bull" and realized > 0) or (bias == "bear" and realized < 0)

        within_move = None
        if expected_move_proxy is not None:
            within_move = abs(realized) <= expected_move_proxy

        rows.append({
            "monday_date": str(dates.iloc[i].date()),
            "friday_date": str(dates.iloc[friday_idx].date()),
            "bias": bias,
            "rsidiff90": _safe(round(float(rsidiff), 1)) if rsidiff is not None else None,
            "slope_deg": _safe(round(float(slope), 1)) if slope is not None else None,
            "spot_monday": round(spot_monday, 2),
            "close_friday": round(close_friday, 2),
            "realized_pct": realized_pct,
            "expected_move_proxy": round(expected_move_proxy, 2) if expected_move_proxy is not None else None,
            "direction_correct": direction_correct,
            "within_expected_move": within_move,
        })
        if len(rows) >= weeks:
            break

    directional_rows = [r for r in rows if r["direction_correct"] is not None]
    move_rows = [r for r in rows if r["within_expected_move"] is not None]
    stats = {
        "weeks_tested": len(rows),
        "directional_calls_made": len(directional_rows),
        "direction_accuracy_pct": round(100 * sum(1 for r in directional_rows if r["direction_correct"]) / len(directional_rows), 1) if directional_rows else None,
        "within_expected_move_pct": round(100 * sum(1 for r in move_rows if r["within_expected_move"]) / len(move_rows), 1) if move_rows else None,
        "avg_realized_move_pct": round(sum(abs(r["realized_pct"]) for r in rows if r["realized_pct"] is not None) / len(rows), 2) if rows else None,
        "note": "TIER 1: technical/price bias only, reconstructed with zero lookahead. "
                "expected_move_proxy is an ATR-based stand-in, NOT the real IV-based "
                "expected move the live Monday job computes -- historical IV isn't stored.",
    }
    return {"symbol": symbol, "stats": stats, "weeks": rows}


# ── TIER 2: grade the REAL live snapshots once their Friday has passed ─────

def grade_pending_live_snapshots(symbol="SPY"):
    """Finds saved 'weekly_plan' runs (the real scheduled Monday snapshots,
    full composite score, real OI/IV) whose expiry has passed and hasn't
    been graded yet, looks up the actual close on expiry day, and stores
    the grade. This is the honest track record -- no approximation, just
    slower to accumulate since it only advances one real week at a time."""
    _ensure_tables()
    from ..db import list_saved_scanner_runs

    runs = list_saved_scanner_runs(scanner_key="weekly_plan", symbol=symbol, limit=200)
    if not runs:
        return {"graded": 0, "note": "no saved weekly_plan snapshots found yet"}

    con = _conn()
    already_graded = {r["source_run_name"] for r in con.execute(
        "SELECT source_run_name FROM weekly_plan_backtest_results"
    ).fetchall()}

    full_hist = _get_price_history(symbol, min_days=800)
    close_by_date = {}
    if full_hist is not None:
        close_by_date = dict(zip(full_hist["date"], full_hist["close"]))

    today = datetime.now().date()
    graded = 0
    for r in runs:
        run_name = r.get("run_name")
        if not run_name or run_name in already_graded:
            continue
        summary = r.get("summary") or {}
        expiry = summary.get("expiry")
        spot = summary.get("spot")
        bias = summary.get("bias")
        confidence = summary.get("confidence")
        plan_score = summary.get("plan_score")
        payload = r.get("payload") or {}
        expected_move = (payload.get("expected_move") or {}).get("value") if isinstance(payload.get("expected_move"), dict) else None

        if not expiry or spot is None:
            continue
        try:
            expiry_date = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if expiry_date >= today:
            continue  # this week hasn't finished yet -- don't grade early

        close_expiry = close_by_date.get(str(expiry_date))
        if close_expiry is None:
            continue  # market data for that day not cached (holiday, etc.) -- skip, don't guess

        realized = float(close_expiry) - float(spot)
        realized_pct = round((realized / float(spot)) * 100, 2) if spot else None
        direction_correct = None
        if bias in ("bull", "bear"):
            direction_correct = (bias == "bull" and realized > 0) or (bias == "bear" and realized < 0)
        within_move = None
        if expected_move is not None:
            within_move = abs(realized) <= float(expected_move)

        con.execute(
            """INSERT OR IGNORE INTO weekly_plan_backtest_results
               (source_run_name, symbol, monday_date, expiry_date, spot_monday, close_expiry,
                realized_pct, bias, confidence, plan_score, expected_move_value,
                direction_correct, within_expected_move, graded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_name, symbol, str(r.get("created_at"))[:10], str(expiry_date), float(spot), float(close_expiry),
             realized_pct, bias, confidence, plan_score, expected_move,
             int(direction_correct) if direction_correct is not None else None,
             int(within_move) if within_move is not None else None,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        graded += 1
    con.commit()
    con.close()
    return {"graded": graded}


def get_track_record(symbol="SPY", limit=100):
    _ensure_tables()
    con = _conn()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM weekly_plan_backtest_results WHERE symbol=? ORDER BY expiry_date DESC LIMIT ?",
        (symbol, limit),
    ).fetchall()]
    con.close()
    directional = [r for r in rows if r["direction_correct"] is not None]
    move = [r for r in rows if r["within_expected_move"] is not None]
    stats = {
        "weeks_graded": len(rows),
        "direction_accuracy_pct": round(100 * sum(r["direction_correct"] for r in directional) / len(directional), 1) if directional else None,
        "within_expected_move_pct": round(100 * sum(r["within_expected_move"] for r in move) / len(move), 1) if move else None,
    }
    return {"symbol": symbol, "stats": stats, "weeks": rows}


# ── Flask routes ────────────────────────────────────────────────────────────

@weekly_backtest_bp.route("/technical", methods=["GET"])
def api_technical_backtest():
    symbol = (request.args.get("symbol") or "SPY").upper()
    weeks = int(request.args.get("weeks", 104))
    return jsonify(technical_bias_backtest(symbol=symbol, weeks=weeks))


@weekly_backtest_bp.route("/grade-pending", methods=["POST"])
def api_grade_pending():
    symbol = (request.get_json(silent=True) or {}).get("symbol", "SPY")
    return jsonify(grade_pending_live_snapshots(symbol=symbol))


@weekly_backtest_bp.route("/track-record", methods=["GET"])
def api_track_record():
    symbol = (request.args.get("symbol") or "SPY").upper()
    return jsonify(get_track_record(symbol=symbol))
