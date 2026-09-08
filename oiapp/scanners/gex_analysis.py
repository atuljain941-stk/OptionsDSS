"""Interactive, read-only gamma-exposure analysis from saved option chains."""
from collections import defaultdict
from datetime import date
import math
import sqlite3

from flask import Blueprint, jsonify, render_template, request

from ..config import DB_PATH

gex_analysis_bp = Blueprint("gex_analysis", __name__, url_prefix="/gex-analysis")


def _num(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _latest_stamp(symbol):
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        row = con.execute("SELECT MAX(fetch_ts) FROM options WHERE symbol=?", (symbol.upper(),)).fetchone()
    return row[0] if row else None


def _spot_from_rows(rows):
    spots = [_num(row["underlying"]) for row in rows if _num(row["underlying"]) and _num(row["underlying"]) > 0]
    return spots[-1] if spots else None


def _dte(expiration):
    try:
        return (date.fromisoformat(str(expiration)[:10]) - date.today()).days
    except (TypeError, ValueError):
        return None


@gex_analysis_bp.route("/")
def page():
    return render_template("gex_analysis.html")


@gex_analysis_bp.route("/api/symbols")
def symbols_api():
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        rows = con.execute("SELECT DISTINCT symbol FROM options WHERE symbol IS NOT NULL AND trim(symbol) != '' ORDER BY symbol").fetchall()
    return jsonify({"symbols": [row[0] for row in rows]})


@gex_analysis_bp.route("/api/expirations")
def expirations_api():
    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    stamp = _latest_stamp(symbol)
    if not stamp:
        return jsonify({"symbol": symbol, "fetch_ts": None, "expirations": []})
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        rows = con.execute(
            "SELECT DISTINCT expiration FROM options WHERE symbol=? AND fetch_ts=? AND expiration IS NOT NULL ORDER BY expiration",
            (symbol, stamp),
        ).fetchall()
    expirations = [{"value": row[0], "dte": _dte(row[0])} for row in rows]
    return jsonify({"symbol": symbol, "fetch_ts": stamp, "expirations": expirations})


@gex_analysis_bp.route("/api/data")
def data_api():
    symbol = (request.args.get("symbol") or "").upper().strip()
    expiration = (request.args.get("expiration") or "all").strip()
    strike_count = max(10, min(120, int(request.args.get("strikes") or 40)))
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    stamp = _latest_stamp(symbol)
    if not stamp:
        return jsonify({"error": f"No saved option chain for {symbol}"}), 404

    sql = "SELECT expiration,type,strike,oi,gamma,underlying FROM options WHERE symbol=? AND fetch_ts=? AND oi>0 AND gamma IS NOT NULL"
    args = [symbol, stamp]
    if expiration != "all":
        sql += " AND expiration=?"
        args.append(expiration)
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, args).fetchall()
    spot = _spot_from_rows(rows)
    if not rows or spot is None:
        return jsonify({"error": "Saved chain has no usable underlying price and gamma rows"}), 422

    by_strike = defaultdict(lambda: {"call_gex": 0.0, "put_gex": 0.0, "call_oi": 0, "put_oi": 0})
    for row in rows:
        strike, gamma, oi = _num(row["strike"]), _num(row["gamma"]), _num(row["oi"])
        kind = str(row["type"] or "").lower()
        if strike is None or gamma is None or oi is None:
            continue
        exposure = abs(gamma) * oi * 100 * spot * spot * .01
        bucket = by_strike[strike]
        if kind.startswith("c"):
            bucket["call_gex"] += exposure
            bucket["call_oi"] += int(oi)
        elif kind.startswith("p"):
            bucket["put_gex"] += exposure
            bucket["put_oi"] += int(oi)

    ordered = sorted(by_strike)
    if not ordered:
        return jsonify({"error": "No call/put gamma rows available"}), 422
    # Show a useful centered window while retaining the selected expiration's calculations.
    nearest = min(range(len(ordered)), key=lambda i: abs(ordered[i] - spot))
    half = strike_count // 2
    lo, hi = max(0, nearest - half), min(len(ordered), nearest + half)
    selected = ordered[lo:hi]
    series = []
    for strike in selected:
        value = by_strike[strike]
        call_gex, put_gex = value["call_gex"], value["put_gex"]
        series.append({
            "strike": strike, "net_gamma": call_gex - put_gex, "abs_gamma": call_gex + put_gex,
            "call_gamma": call_gex, "put_gamma": put_gex, "put_gamma_signed": -put_gex,
            "call_oi": value["call_oi"], "put_oi": value["put_oi"],
        })

    total_call = sum(v["call_gex"] for v in by_strike.values())
    total_put = sum(v["put_gex"] for v in by_strike.values())
    gross, net = total_call + total_put, total_call - total_put
    pin = max(by_strike, key=lambda k: by_strike[k]["call_gex"] + by_strike[k]["put_gex"])
    call_wall = max(by_strike, key=lambda k: by_strike[k]["call_gex"])
    put_wall = max(by_strike, key=lambda k: by_strike[k]["put_gex"])
    return jsonify({
        "symbol": symbol, "expiration": expiration, "fetch_ts": stamp, "spot": spot,
        "dte": _dte(expiration) if expiration != "all" else None, "series": series,
        "summary": {
            "net_gex": net, "abs_gex": gross, "call_gex": total_call, "put_gex": total_put,
            "gex_ratio": (total_call / total_put) if total_put else None,
            "sentiment": (total_call / gross * 100) if gross else 50,
            "regime": "Positive gamma / mean-reversion tendency" if net >= 0 else "Negative gamma / trend-amplification tendency",
            "max_gamma_strike": pin, "call_wall": call_wall, "put_wall": put_wall,
        },
    })
