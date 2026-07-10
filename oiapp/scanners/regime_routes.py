# oiapp/scanners/regime_routes.py
from flask import Blueprint, jsonify, request
from .regime_scanner import run_regime_scan, get_latest_scan
import math

regime_bp = Blueprint("regime_bp", __name__, url_prefix="/regime")

def _json_safe(value):
    """Return a JSON-safe copy with NaN/Infinity converted to None.

    Pandas/numpy calculations can produce float('nan'), and Flask can serialize
    those as bare NaN tokens. Browsers reject that as invalid JSON, so every
    Regime API response is cleaned before jsonify().
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    try:
        # numpy/pandas scalars generally support .item().
        if hasattr(value, 'item') and not isinstance(value, (str, bytes, bytearray)):
            return _json_safe(value.item())
    except Exception:
        pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _get_wl_syms(wl_id):
    if not wl_id: return None
    try:
        import sqlite3
        from pathlib import Path
        db = str(Path(__file__).resolve().parents[2] / "options_data.db")
        con = sqlite3.connect(db)
        rows = con.execute("SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",(int(wl_id),)).fetchall()
        con.close()
        return [r[0] for r in rows] if rows else None
    except: return None

@regime_bp.route("/scan", methods=["POST","GET"])
def trigger_scan():
    try:
        wl_id  = request.args.get("watchlist_id", None, type=int) or (request.get_json(silent=True) or {}).get("watchlist_id")
        syms   = _get_wl_syms(wl_id)
        result = run_regime_scan(symbols=syms)
        return jsonify(_json_safe(result))
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:400]}), 500

@regime_bp.route("/results")
def get_results():
    date = request.args.get("date")
    regime_filter = request.args.get("regime","").strip()
    bias_filter   = request.args.get("bias","").strip()
    sector_filter = request.args.get("sector","").strip()
    try:
        data = get_latest_scan(date)
        results = data["results"]
        if regime_filter and regime_filter != "ALL":
            results = [r for r in results if r.get("regime","") == regime_filter]
        if bias_filter and bias_filter != "ALL":
            results = [r for r in results if r.get("bias","") == bias_filter]
        if sector_filter:
            try:
                from ..services.sector_service import get_symbol_sector
                results = [r for r in results if get_symbol_sector(r["symbol"]) == sector_filter]
            except: pass
        data["results"] = results
        data["count"]   = len(results)
        return jsonify(_json_safe(data))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@regime_bp.route("/symbol/<symbol>")
def symbol_regime(symbol):
    from .regime_scanner import _compute_regime_ta
    result = _compute_regime_ta(symbol.upper())
    return jsonify(_json_safe(result or {"error": "No data"}))
