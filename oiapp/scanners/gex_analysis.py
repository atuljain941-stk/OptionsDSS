"""Interactive gamma-exposure analysis from saved and live option-chain inputs."""
from collections import defaultdict
from datetime import date
import math
import sqlite3

from flask import Blueprint, jsonify, render_template, request

from ..config import DB_PATH
from ._spot_cache import _fetch as fetch_live_spot
from .spy_strategies import _bs_gamma

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


def _previous_stamp(symbol, stamp):
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        row = con.execute(
            "SELECT MAX(fetch_ts) FROM options WHERE symbol=? AND fetch_ts < ?",
            (symbol.upper(), stamp),
        ).fetchone()
    return row[0] if row else None


def _spot_from_rows(rows):
    spots = [_num(row["underlying"]) for row in rows if _num(row["underlying"]) and _num(row["underlying"]) > 0]
    return spots[-1] if spots else None


def _stored_spot(symbol):
    candidates = (
        "SELECT close FROM intraday_price_cache WHERE symbol=? AND close>0 ORDER BY ts DESC LIMIT 1",
        "SELECT close FROM intraday_2m_price_cache WHERE symbol=? AND close>0 ORDER BY ts_et DESC LIMIT 1",
        "SELECT close FROM price_cache WHERE symbol=? AND close>0 ORDER BY date DESC LIMIT 1",
    )
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        for sql in candidates:
            try:
                row = con.execute(sql, (symbol,)).fetchone()
                value = _num(row[0]) if row else None
                if value and value > 0:
                    return value
            except sqlite3.OperationalError:
                continue
    return None


def _dte(expiration):
    try:
        return (date.fromisoformat(str(expiration)[:10]) - date.today()).days
    except (TypeError, ValueError):
        return None


def _saved_rows(symbol, stamp, expiration):
    sql = """SELECT expiration,type,strike,oi,volume,gamma,iv,underlying
             FROM options WHERE symbol=? AND fetch_ts=? AND oi>0"""
    args = [symbol, stamp]
    if expiration != "all":
        sql += " AND expiration=?"
        args.append(expiration)
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(sql, args).fetchall()]


def _previous_oi(symbol, previous_stamp, expiration):
    if not previous_stamp:
        return {}
    sql = "SELECT type,strike,SUM(oi) AS oi FROM options WHERE symbol=? AND fetch_ts=?"
    args = [symbol, previous_stamp]
    if expiration != "all":
        sql += " AND expiration=?"
        args.append(expiration)
    sql += " GROUP BY type,strike"
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        rows = con.execute(sql, args).fetchall()
    return {
        (str(kind or "").lower()[:1], _num(strike)): int(_num(oi) or 0)
        for kind, strike, oi in rows
        if _num(strike) is not None
    }


def _live_option_fields(symbol, expiration):
    """Best-effort live IV/volume. OI remains from the saved snapshot."""
    if expiration == "all":
        return {}, "Choose one expiration for intraday live option inputs."
    try:
        import yfinance as yf
        chain = yf.Ticker(symbol).option_chain(str(expiration)[:10])
        fields = {}
        for kind, frame in (("call", chain.calls), ("put", chain.puts)):
            for _, row in frame.iterrows():
                strike = _num(row.get("strike"))
                if strike is None:
                    continue
                fields[(kind[:1], strike)] = {
                    "iv": _num(row.get("impliedVolatility")),
                    "volume": _num(row.get("volume")),
                }
        return fields, None if fields else "Live chain returned no usable strike rows."
    except Exception as exc:
        return {}, "Live option chain unavailable: " + str(exc)[:120]


@gex_analysis_bp.route("/")
def page():
    return render_template("gex_analysis.html")


@gex_analysis_bp.route("/api/symbols")
def symbols_api():
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM options WHERE symbol IS NOT NULL AND trim(symbol) != '' ORDER BY symbol"
        ).fetchall()
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
    return jsonify({
        "symbol": symbol,
        "fetch_ts": stamp,
        "expirations": [{"value": row[0], "dte": _dte(row[0])} for row in rows],
    })


@gex_analysis_bp.route("/api/data")
def data_api():
    symbol = (request.args.get("symbol") or "").upper().strip()
    expiration = (request.args.get("expiration") or "all").strip()
    mode = (request.args.get("mode") or "saved").lower()
    strike_count = max(10, min(120, int(request.args.get("strikes") or 40)))
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    if mode not in {"saved", "intraday"}:
        return jsonify({"error": "mode must be saved or intraday"}), 400

    stamp = _latest_stamp(symbol)
    if not stamp:
        return jsonify({"error": f"No saved option chain for {symbol}"}), 404
    rows = _saved_rows(symbol, stamp, expiration)
    spot = fetch_live_spot(symbol)
    spot_source = "live" if spot else None
    if spot is None:
        spot = _spot_from_rows(rows) or _stored_spot(symbol)
        spot_source = "saved fallback" if spot else None
    if not rows or spot is None:
        return jsonify({"error": "Live spot lookup failed and no stored price is available"}), 422

    # OI is a saved baseline for the GEX calculation. In intraday mode we do
    # not treat it as a live input or query a previous snapshot for an OI chart.
    previous_stamp = _previous_stamp(symbol, stamp) if mode == "saved" else None
    prior_oi = _previous_oi(symbol, previous_stamp, expiration) if previous_stamp else {}
    live_fields, live_note = ({}, None)
    market_source = "saved snapshot"
    if mode == "intraday":
        live_fields, live_note = _live_option_fields(symbol, expiration)
        market_source = "live IV and volume" if live_fields else "saved fallback"

    by_strike = defaultdict(lambda: {
        "call_gex": 0.0, "put_gex": 0.0,
        "call_oi": 0, "put_oi": 0,
        "call_oi_change": 0, "put_oi_change": 0,
        "call_volume": 0, "put_volume": 0,
    })
    for row in rows:
        strike, saved_gamma, oi = _num(row["strike"]), _num(row["gamma"]), _num(row["oi"])
        kind = str(row["type"] or "").lower()
        side = kind[:1]
        if strike is None or oi is None or side not in {"c", "p"}:
            continue

        live = live_fields.get((side, strike), {}) if mode == "intraday" else {}
        iv = _num(live.get("iv")) if live else _num(row["iv"])
        volume = _num(live.get("volume")) if live else _num(row["volume"])
        # Intraday recomputes gamma from the current spot and the live IV.
        gamma = None if mode == "intraday" else saved_gamma
        if gamma is None or gamma <= 0:
            iv_pct = (iv or 0.30) * 100.0 if (iv or 0.30) <= 1 else (iv or 30.0)
            gamma = _bs_gamma(spot, strike, max(1, _dte(row["expiration"]) or 1), iv_pct)
        if gamma is None or gamma <= 0:
            continue

        exposure = abs(gamma) * oi * 100 * spot * spot * 0.01
        bucket = by_strike[strike]
        prior = prior_oi.get((side, strike), 0)
        if side == "c":
            bucket["call_gex"] += exposure
            bucket["call_oi"] += int(oi)
            bucket["call_oi_change"] += int(oi) - prior
            bucket["call_volume"] += int(volume or 0)
        else:
            bucket["put_gex"] += exposure
            bucket["put_oi"] += int(oi)
            bucket["put_oi_change"] += int(oi) - prior
            bucket["put_volume"] += int(volume or 0)

    ordered = sorted(by_strike)
    if not ordered:
        return jsonify({"error": "No call/put gamma rows available"}), 422
    nearest = min(range(len(ordered)), key=lambda i: abs(ordered[i] - spot))
    half = strike_count // 2
    selected = ordered[max(0, nearest - half):min(len(ordered), nearest + half)]

    series = []
    for strike in selected:
        value = by_strike[strike]
        call_gex, put_gex = value["call_gex"], value["put_gex"]
        series.append({
            "strike": strike,
            "net_gamma": call_gex - put_gex,
            "abs_gamma": call_gex + put_gex,
            "call_gamma": call_gex,
            "put_gamma": put_gex,
            "put_gamma_signed": -put_gex,
            "call_oi": value["call_oi"],
            "put_oi": value["put_oi"],
            "call_oi_change": value["call_oi_change"],
            "put_oi_change": value["put_oi_change"],
            "call_volume": value["call_volume"],
            "put_volume": value["put_volume"],
        })

    total_call = sum(v["call_gex"] for v in by_strike.values())
    total_put = sum(v["put_gex"] for v in by_strike.values())
    gross, net = total_call + total_put, total_call - total_put
    pin = max(by_strike, key=lambda k: by_strike[k]["call_gex"] + by_strike[k]["put_gex"])
    call_wall = max(by_strike, key=lambda k: by_strike[k]["call_gex"])
    put_wall = max(by_strike, key=lambda k: by_strike[k]["put_gex"])
    return jsonify({
        "symbol": symbol,
        "expiration": expiration,
        "fetch_ts": stamp,
        "previous_fetch_ts": previous_stamp,
        "mode": mode,
        "market_source": market_source,
        "live_note": live_note,
        "spot": spot,
        "spot_source": spot_source,
        "dte": _dte(expiration) if expiration != "all" else None,
        "series": series,
        "summary": {
            "net_gex": net,
            "abs_gex": gross,
            "call_gex": total_call,
            "put_gex": total_put,
            "gex_ratio": (total_call / total_put) if total_put else None,
            "sentiment": (total_call / gross * 100) if gross else 50,
            "regime": "Positive gamma / mean-reversion tendency" if net >= 0 else "Negative gamma / trend-amplification tendency",
            "max_gamma_strike": pin,
            "call_wall": call_wall,
            "put_wall": put_wall,
        },
    })
