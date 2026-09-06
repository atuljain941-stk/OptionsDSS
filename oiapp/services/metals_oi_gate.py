# oiapp/services/metals_oi_gate.py
"""
Metals OI/Price Gate
─────────────────────
Additional confirmation gate for gold (GC) and silver (SI) sitting in
front of the UAE oversold/overbought fade signal — same concept discussed
for the standalone tracker, but built directly on top of infrastructure
that already exists in this app rather than a new data pipeline:

  - Futures OI + settle price: reuses `futures_oi_schwab.get_latest_oi()`,
    the same three-layer (Schwab -> CME fallback -> CFTC COT) fetch and
    front-month roll-splicing already powering the Futures OI Dashboard.
    No new fetch, no new scheduler job — GLD/SLV are already in
    SCHWAB_ROOTS and already refreshed by the existing per-watchlist
    schedule.
  - UAE regime/fade signal: reuses `_compute_uae()` and `_yf_history()`
    from uae_trade_scanner.py directly — same RSIdiff/regime logic
    already trusted for the equity watchlist, run here against GC=F/SI=F
    continuous futures price history instead of a stock ticker.

THE FRAMEWORK (unchanged from the standalone design):
    Price Up   + OI Up   -> new longs entering    -> trend confirmed (bullish)
    Price Up   + OI Down -> shorts covering        -> fragile rally
    Price Down + OI Up   -> new shorts entering    -> trend confirmed (bearish)
    Price Down + OI Down -> longs bailing          -> fragile decline (liquidation)

UAE oversold  (fade_buy)  + weak_decline_long_liquidation -> confirms
UAE oversold  (fade_buy)  + trend_confirmation_bearish     -> warns
UAE overbought (fade_sell) + weak_rally_short_covering     -> confirms
UAE overbought (fade_sell) + trend_confirmation_bullish    -> warns

This is a gate, not a standalone signal — it tells you whether futures
positioning supports or undercuts what UAE is already saying, once a day
per symbol (OI is settlement-based, same constraint as everywhere else).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, date as dtdate
from typing import Any, Dict, Literal, Optional

from flask import Blueprint, jsonify, render_template, request

from ..config import DB_PATH

metals_oi_gate_bp = Blueprint("metals_oi_gate", __name__, url_prefix="/metals-oi-gate")

# root_symbol (as used by futures_oi_schwab.SCHWAB_ROOTS) -> Yahoo ticker for UAE price history
# UAE price history uses the ETF itself (GLD/SLV), NOT raw continuous
# futures tickers (GC=F/SI=F). Raw =F tickers from yfinance are an
# unadjusted front-month splice -- every contract roll (~monthly)
# creates an artificial price jump unrelated to real price action, and
# that jump corrupts RSI14/EMA90 computed across it, which directly
# feeds RSIdiff90 -- this was producing an inaccurate reading right
# around roll dates. GLD/SLV are continuously-traded single securities
# with no such gap, and this app already treats them as the canonical
# gold/silver proxy everywhere else (see futures_oi_schwab.SCHWAB_ROOTS,
# which maps GLD->/GC and SLV->/SI for the OI side of this same page).
ROOT_TO_YF = {"GLD": "GLD", "SLV": "SLV"}
ROOT_LABEL = {"GLD": "Gold / GC", "SLV": "Silver / SI"}
TRACKED_ROOTS = ["GLD", "SLV"]

Quadrant = Literal[
    "trend_confirmation_bullish",
    "trend_confirmation_bearish",
    "weak_rally_short_covering",
    "weak_decline_long_liquidation",
    "flat",
    "insufficient_data",
]


# ── Schema ────────────────────────────────────────────────────────────────

def _ensure_table():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS metals_oi_gate_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        root_symbol TEXT NOT NULL,
        session_date TEXT NOT NULL,
        close REAL, prev_close REAL,
        oi INTEGER, prev_oi INTEGER,
        price_change_pct REAL, oi_change_pct REAL,
        quadrant TEXT,
        uae_tf TEXT, uae_regime TEXT, uae_rsidiff REAL,
        uae_fade_buy INTEGER, uae_fade_sell INTEGER,
        verdict TEXT, confidence_adjustment REAL, note TEXT,
        logged_at TEXT,
        UNIQUE(root_symbol, session_date, uae_tf)
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_metals_gate_root_date ON metals_oi_gate_log(root_symbol, session_date)")
    con.commit()
    con.close()


def _log(reading: Dict[str, Any], uae: Dict[str, Any], verdict: Dict[str, Any]):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT OR REPLACE INTO metals_oi_gate_log
           (root_symbol, session_date, close, prev_close, oi, prev_oi,
            price_change_pct, oi_change_pct, quadrant,
            uae_tf, uae_regime, uae_rsidiff, uae_fade_buy, uae_fade_sell,
            verdict, confidence_adjustment, note, logged_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            reading["root_symbol"], reading["session_date"], reading["close"], reading["prev_close"],
            reading["oi"], reading["prev_oi"], reading["price_change_pct"], reading["oi_change_pct"],
            reading["quadrant"], uae.get("tf"), uae.get("regime"), uae.get("rsidiff"),
            int(bool(uae.get("fade_buy"))), int(bool(uae.get("fade_sell"))),
            verdict["verdict"], verdict["confidence_adjustment"], verdict["note"],
            datetime.now().isoformat(),
        ),
    )
    con.commit()
    con.close()


def get_recent(root_symbol: str, limit: int = 30) -> list:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM metals_oi_gate_log WHERE root_symbol=? ORDER BY session_date DESC LIMIT ?",
        (root_symbol.upper(), limit),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ── OI/price quadrant, off existing futures_oi_schwab data ────────────────

def classify_quadrant(price_change_pct: float, oi_change_pct: float,
                       flat_threshold_pct: float = 0.05) -> Quadrant:
    if abs(price_change_pct) <= flat_threshold_pct:
        return "flat"
    price_up = price_change_pct > 0
    oi_up = oi_change_pct > 0
    if price_up and oi_up:
        return "trend_confirmation_bullish"
    if price_up and not oi_up:
        return "weak_rally_short_covering"
    if not price_up and oi_up:
        return "trend_confirmation_bearish"
    return "weak_decline_long_liquidation"


def get_oi_reading(root_symbol: str) -> Dict[str, Any]:
    """
    Reads the last two sessions off the SAME roll-spliced front-month
    series the Futures OI Dashboard already computes and displays --
    front_continuous_series from get_latest_oi(). No new fetch.
    """
    from .futures_oi_schwab import get_latest_oi

    # 45-day lookback (not the original 15) specifically so a stale fetch
    # gap still returns the most recent available rows to compare and
    # flag as stale, rather than falling into "insufficient_data" purely
    # because the gap is wider than a short window would tolerate.
    data = get_latest_oi(root_symbol, days=45)
    series = data.get("front_continuous_series") or []
    if len(series) < 2:
        return {
            "root_symbol": root_symbol, "session_date": dtdate.today().isoformat(),
            "close": None, "prev_close": None, "oi": None, "prev_oi": None,
            "price_change_pct": 0.0, "oi_change_pct": 0.0,
            "quadrant": "insufficient_data",
            "display": ROOT_LABEL.get(root_symbol, root_symbol),
            "note": f"Fewer than 2 sessions of stored OI history for {root_symbol} yet "
                    f"-- check /scheduler-hub that the futures OI job has run, or wait "
                    f"for tomorrow's fetch.",
        }

    today, prior = series[-1], series[-2]
    close, prev_close = float(today.get("close") or 0), float(prior.get("close") or 0)
    oi, prev_oi = int(today.get("oi") or 0), int(prior.get("oi") or 0)
    price_change_pct = ((close - prev_close) / prev_close * 100) if prev_close else 0.0
    oi_change_pct = ((oi - prev_oi) / prev_oi * 100) if prev_oi else 0.0

    session_date = today.get("date", dtdate.today().isoformat())
    days_stale = None
    try:
        days_stale = (dtdate.today() - dtdate.fromisoformat(str(session_date)[:10])).days
    except Exception:
        pass
    # 4 calendar days covers a normal weekend gap (Fri close -> Mon fetch)
    # without flagging every Monday morning as "stale" -- anything beyond
    # that means the daily futures OI fetch (Schwab -> CME fallback, see
    # fetch_futures_oi_three_layer) has actually stopped producing new
    # rows for this root, not just a weekend gap.
    is_stale = days_stale is not None and days_stale > 4

    return {
        "root_symbol": root_symbol,
        "session_date": session_date,
        "close": close, "prev_close": prev_close,
        "oi": oi, "prev_oi": prev_oi,
        "price_change_pct": round(price_change_pct, 3),
        "oi_change_pct": round(oi_change_pct, 3),
        "quadrant": classify_quadrant(price_change_pct, oi_change_pct),
        "display": ROOT_LABEL.get(root_symbol, root_symbol),
        "contract": today.get("contract"),
        "days_stale": days_stale,
        "is_stale": is_stale,
        "stale_note": (
            f"This reading is {days_stale} days old, not today's price -- the daily "
            f"futures OI fetch (Schwab, falling back to CME) doesn't appear to have "
            f"stored a new row for {root_symbol} recently. Check /scheduler-hub for "
            f"the 'morning_data_pipeline' job's last run/error, and /diagnostics for "
            f"whether Schwab auth is still valid -- that's the most common cause of "
            f"this going silently stale, since a failed fetch only prints to the "
            f"server log rather than surfacing as an error here."
        ) if is_stale else "",
    }


# ── UAE regime/fade, off existing uae_trade_scanner logic ─────────────────

def get_uae_reading(root_symbol: str, tf: str = "1d") -> Dict[str, Any]:
    """
    Reuses _compute_uae + _yf_history from uae_trade_scanner.py directly
    -- same RSIdiff/regime/fade logic already trusted for the equity
    watchlist, run here against continuous gold/silver futures price
    history (GC=F / SI=F) instead of a stock ticker.
    """
    from ..scanners.uae_trade_scanner import _yf_history, _compute_uae

    yf_symbol = ROOT_TO_YF.get(root_symbol)
    if not yf_symbol:
        return {"error": f"No UAE price mapping for {root_symbol}"}

    df = _yf_history(yf_symbol, tf)
    result = _compute_uae(df, tf)
    if result is None:
        return {"error": f"Not enough {yf_symbol} history at tf={tf} to compute UAE"}
    return result


# ── Gate verdict ────────────────────────────────────────────────────────

def combine(oi_reading: Dict[str, Any], uae: Dict[str, Any]) -> Dict[str, Any]:
    if uae.get("error") or oi_reading.get("quadrant") == "insufficient_data":
        return {"verdict": "neutral", "confidence_adjustment": 0.0,
                "note": uae.get("error") or oi_reading.get("note") or "Insufficient data."}

    q = oi_reading["quadrant"]
    fade_buy = bool(uae.get("fade_buy"))    # UAE oversold-fade (potential long)
    fade_sell = bool(uae.get("fade_sell"))  # UAE overbought-fade (potential short)

    if q == "flat" or (not fade_buy and not fade_sell):
        return {"verdict": "neutral", "confidence_adjustment": 0.0,
                "note": "No UAE fade signal active, or no meaningful price/OI move today "
                        "-- nothing for the gate to confirm or warn against."}

    if fade_buy:
        if q == "weak_decline_long_liquidation":
            return {"verdict": "confirms", "confidence_adjustment": 0.5,
                    "note": "UAE oversold fade + decline looks liquidation-driven, not "
                            "fresh conviction selling -- bounce is more credible."}
        if q == "trend_confirmation_bearish":
            return {"verdict": "warns", "confidence_adjustment": -0.5,
                    "note": "UAE oversold fade, but new shorts are still entering even as "
                            "price falls -- real conviction selling. Classic 'oversold gets "
                            "more oversold' setup, lower confidence in a bounce here."}
        return {"verdict": "neutral", "confidence_adjustment": 0.0,
                "note": "UAE oversold fade active, but OI action doesn't clearly support "
                        "or undercut it."}

    if fade_sell:
        if q == "weak_rally_short_covering":
            return {"verdict": "confirms", "confidence_adjustment": 0.5,
                    "note": "UAE overbought fade + rally looks like short-covering, not "
                            "fresh buying -- pullback is more credible."}
        if q == "trend_confirmation_bullish":
            return {"verdict": "warns", "confidence_adjustment": -0.5,
                    "note": "UAE overbought fade, but new longs are still entering even as "
                            "price extends -- real conviction buying. Classic 'overbought "
                            "gets more overbought' setup, lower confidence in a pullback here."}
        return {"verdict": "neutral", "confidence_adjustment": 0.0,
                "note": "UAE overbought fade active, but OI action doesn't clearly support "
                        "or undercut it."}

    return {"verdict": "neutral", "confidence_adjustment": 0.0, "note": "Unhandled case."}


def read_gate(root_symbol: str, tf: str = "1d", log: bool = True) -> Dict[str, Any]:
    oi_reading = get_oi_reading(root_symbol)
    uae = get_uae_reading(root_symbol, tf)
    verdict = combine(oi_reading, uae)
    verdict["uae_tf"] = tf
    if log and oi_reading.get("quadrant") != "insufficient_data" and not uae.get("error"):
        try:
            _log(oi_reading, uae, verdict)
        except Exception:
            pass
    return {"oi": oi_reading, "uae": uae, "verdict": verdict}


# ── Routes ──────────────────────────────────────────────────────────────

@metals_oi_gate_bp.route("/")
def page():
    return render_template("metals_oi_gate.html")


@metals_oi_gate_bp.route("/api/read")
def api_read():
    from ..scanners.spy_strategies import _json_safe
    try:
        root = (request.args.get("root") or "GLD").upper()
        tf = request.args.get("tf") or "1d"
        if root not in TRACKED_ROOTS:
            return jsonify({"error": f"root must be one of {TRACKED_ROOTS}"}), 400
        return jsonify(_json_safe(read_gate(root, tf)))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@metals_oi_gate_bp.route("/api/read_all")
def api_read_all():
    from ..scanners.spy_strategies import _json_safe
    tf = request.args.get("tf") or "1d"
    out = {}
    for root in TRACKED_ROOTS:
        try:
            out[root] = read_gate(root, tf)
        except Exception as e:
            out[root] = {"error": str(e)}
    return jsonify(_json_safe(out))


@metals_oi_gate_bp.route("/api/history")
def api_history():
    from ..scanners.spy_strategies import _json_safe
    try:
        root = (request.args.get("root") or "GLD").upper()
        limit = int(request.args.get("limit", 30))
        return jsonify(_json_safe({"root": root, "rows": get_recent(root, limit)}))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
