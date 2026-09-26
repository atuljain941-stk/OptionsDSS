"""Near-price, strike-level OI buildup scanner.

Read-only on-demand page: finds positive call/put OI buildup near saved spot.
It intentionally does not use Scanner Builder's multi-timeframe price logic.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from flask import Blueprint, jsonify, render_template_string, request

from ..config import DB_PATH

near_price_oi_bp = Blueprint("near_price_oi", __name__, url_prefix="/near-price-oi")


def _connect():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    return con


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _watchlists():
    with _connect() as con:
        try:
            rows = con.execute(
                """SELECT w.id, w.name, COUNT(ws.id) AS symbols, COALESCE(w.is_default,0) AS is_default
                   FROM watchlists w LEFT JOIN watchlist_symbols ws ON ws.watchlist_id=w.id
                   GROUP BY w.id, w.name, w.is_default
                   ORDER BY is_default DESC, LOWER(w.name)"""
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [{"id": row["id"], "name": row["name"], "symbols": row["symbols"],
             "is_default": bool(row["is_default"])} for row in rows]


def _symbols(con, watchlist_id):
    if watchlist_id:
        rows = con.execute(
            "SELECT DISTINCT UPPER(symbol) AS symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (watchlist_id,),
        ).fetchall()
    else:
        rows = con.execute("SELECT DISTINCT UPPER(symbol) AS symbol FROM watchlist_symbols ORDER BY symbol").fetchall()
    return [str(row["symbol"]) for row in rows if row["symbol"]]


def _near_buildup_for_symbol(con, symbol, min_pct, max_distance_pct, lookback_days):
    latest = con.execute(
        """SELECT date, MAX(fetch_ts) AS stamp
           FROM options WHERE UPPER(symbol)=? GROUP BY date ORDER BY date DESC LIMIT 1""",
        (symbol,),
    ).fetchone()
    if not latest or not latest["date"] or not latest["stamp"]:
        return []
    latest_date, latest_stamp = str(latest["date"])[:10], latest["stamp"]
    try:
        target = (date.fromisoformat(latest_date) - timedelta(days=lookback_days)).isoformat()
    except ValueError:
        return []
    prior = con.execute(
        """SELECT date, MAX(fetch_ts) AS stamp
           FROM options WHERE UPPER(symbol)=? AND date<=?
           GROUP BY date ORDER BY date DESC LIMIT 1""",
        (symbol, target),
    ).fetchone()
    if not prior or not prior["stamp"] or str(prior["date"])[:10] == latest_date:
        return []

    expiries = con.execute(
        """SELECT DISTINCT expiration FROM options
           WHERE UPPER(symbol)=? AND fetch_ts=? AND expiration>=?
           ORDER BY expiration""",
        (symbol, latest_stamp, latest_date),
    ).fetchall()
    if not expiries:
        return []
    expiry = str(expiries[0]["expiration"])[:10]
    spot_row = con.execute(
        """SELECT AVG(COALESCE(underlying,0)) AS spot FROM options
           WHERE UPPER(symbol)=? AND expiration=? AND fetch_ts=?""",
        (symbol, expiry, latest_stamp),
    ).fetchone()
    spot = _number(spot_row["spot"] if spot_row else 0)
    if spot <= 0:
        return []

    current = con.execute(
        """SELECT LOWER(type) AS side, strike, SUM(COALESCE(oi,0)) AS oi
           FROM options WHERE UPPER(symbol)=? AND expiration=? AND fetch_ts=?
           GROUP BY LOWER(type), strike""",
        (symbol, expiry, latest_stamp),
    ).fetchall()
    previous = {
        (str(row["side"]), _number(row["strike"])): _number(row["oi"])
        for row in con.execute(
            """SELECT LOWER(type) AS side, strike, SUM(COALESCE(oi,0)) AS oi
               FROM options WHERE UPPER(symbol)=? AND expiration=? AND fetch_ts=?
               GROUP BY LOWER(type), strike""",
            (symbol, expiry, prior["stamp"]),
        ).fetchall()
    }
    results = []
    for row in current:
        side = str(row["side"] or "").lower()
        if side not in ("call", "put"):
            continue
        strike, current_oi = _number(row["strike"]), _number(row["oi"])
        before = previous.get((side, strike), 0.0)
        if strike <= 0 or current_oi <= before:
            continue
        distance_pct = abs(strike - spot) / spot * 100.0
        if distance_pct > max_distance_pct:
            continue
        change = current_oi - before
        # A newly listed strike with zero prior OI is a valid buildup; report
        # it as 100% so it can pass a percentage-based scanner threshold.
        change_pct = 100.0 if before <= 0 else change / before * 100.0
        if change_pct < min_pct:
            continue
        results.append({
            "symbol": symbol, "side": side, "expiry": expiry, "spot": round(spot, 2),
            "strike": strike, "distance_pct": round(distance_pct, 2),
            "oi": int(current_oi), "prior_oi": int(before), "change": int(change),
            "change_pct": round(change_pct, 2), "latest_date": latest_date,
            "prior_date": str(prior["date"])[:10],
        })
    return results


@near_price_oi_bp.route("/")
def page():
    return render_template_string(PAGE)


@near_price_oi_bp.route("/api/watchlists")
def api_watchlists():
    return jsonify({"watchlists": _watchlists()})


@near_price_oi_bp.route("/api/run")
def api_run():
    try:
        watchlist_id = int(request.args.get("watchlist_id") or 0) or None
        min_pct = max(0.0, min(10000.0, float(request.args.get("min_pct") or 20)))
        distance_pct = max(0.05, min(25.0, float(request.args.get("distance_pct") or 2)))
        lookback_days = max(1, min(30, int(request.args.get("lookback_days") or 5)))
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid scanner controls."}), 400
    with _connect() as con:
        symbols = _symbols(con, watchlist_id)
        rows = []
        for symbol in symbols:
            try:
                rows.extend(_near_buildup_for_symbol(con, symbol, min_pct, distance_pct, lookback_days))
            except Exception:
                continue
    rows.sort(key=lambda row: (row["distance_pct"], -row["change_pct"], -row["change"]))
    return jsonify({"ok": True, "count": len(rows), "symbols_scanned": len(symbols),
                    "results": rows, "settings": {"min_pct": min_pct,
                    "distance_pct": distance_pct, "lookback_days": lookback_days}})


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Near-Price OI Buildup</title>
<link rel="stylesheet" href="/static/style.css">
<style>
body{max-width:1500px;margin:0 auto;padding:18px}.toolbar{display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin:14px 0}.toolbar label{display:grid;gap:5px;font-size:12px}.toolbar input,.toolbar select{min-width:105px}.muted{color:var(--muted)}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:8px;border-bottom:1px solid var(--border);text-align:right}th{text-align:right;color:var(--muted)}th:first-child,td:first-child{text-align:left}.call{color:#60a5fa;font-weight:700}.put{color:#fb7185;font-weight:700}.positive{color:#86efac}
</style></head><body>
<div style="display:flex;justify-content:space-between;align-items:center;gap:12px"><div><h1 style="margin:0">Near-Price OI Buildup</h1><p class="muted">Saved strike-level OI only — no multi-timeframe price logic. Finds fresh positive call or put buildup close to current saved spot.</p></div><a class="btn btn-ghost" href="/">← OI Viewer</a></div>
<div class="card" style="padding:14px"><div class="toolbar">
<label>Watchlist<select id="watchlist"></select></label>
<label>Minimum OI buildup %<input id="minPct" type="number" value="20" min="0" step="5"></label>
<label>Within spot %<input id="distancePct" type="number" value="2" min="0.05" step="0.25"></label>
<label>Lookback days<input id="lookbackDays" type="number" value="5" min="1" max="30"></label>
<button class="btn btn-primary" id="run">Run scanner</button></div>
<div id="status" class="muted">Load a watchlist and run the scanner.</div></div>
<div class="card" style="padding:14px;margin-top:14px;overflow:auto"><table><thead><tr><th>Symbol</th><th>Side</th><th>Expiry</th><th>Spot</th><th>Strike</th><th>Distance</th><th>OI Δ</th><th>OI Δ%</th><th>Prior OI</th><th>Current OI</th><th>Window</th></tr></thead><tbody id="rows"><tr><td colspan="11" class="muted">No scan run yet.</td></tr></tbody></table></div>
<script>
const $=id=>document.getElementById(id), n=v=>Number(v||0).toLocaleString();
async function loadWatchlists(){const d=await fetch('/near-price-oi/api/watchlists').then(r=>r.json());$('watchlist').innerHTML=(d.watchlists||[]).map(x=>'<option value="'+x.id+'" '+(x.is_default?'selected':'')+'>'+x.name+' ('+x.symbols+')</option>').join('')||'<option value="">All available symbols</option>'}
$('run').onclick=async()=>{const q=new URLSearchParams({watchlist_id:$('watchlist').value,min_pct:$('minPct').value,distance_pct:$('distancePct').value,lookback_days:$('lookbackDays').value});$('status').textContent='Scanning saved OI…';const d=await fetch('/near-price-oi/api/run?'+q).then(r=>r.json());if(!d.ok){$('status').textContent=d.error||'Scanner failed.';return}$('status').textContent=d.count+' candidates from '+d.symbols_scanned+' symbols. Minimum +'+d.settings.min_pct+'% OI build within '+d.settings.distance_pct+'% of saved spot.';$('rows').innerHTML=d.results.length?d.results.map(r=>'<tr><td>'+r.symbol+'</td><td class="'+r.side+'">'+r.side.toUpperCase()+'</td><td>'+r.expiry+'</td><td>'+r.spot.toFixed(2)+'</td><td>'+r.strike.toFixed(2)+'</td><td>'+r.distance_pct.toFixed(2)+'%</td><td class="positive">+'+n(r.change)+'</td><td class="positive">+'+r.change_pct.toFixed(1)+'%</td><td>'+n(r.prior_oi)+'</td><td>'+n(r.oi)+'</td><td>'+r.prior_date+' → '+r.latest_date+'</td></tr>').join(''):'<tr><td colspan="11" class="muted">No call or put buildup met these proximity and percentage thresholds.</td></tr>'};loadWatchlists();
</script></body></html>"""
