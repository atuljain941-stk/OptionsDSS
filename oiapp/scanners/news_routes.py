# oiapp/scanners/news_routes.py
from flask import Blueprint, jsonify, request
import math
from ..services.news_service import fetch_market_news, save_news_to_db, get_saved_news, generate_morning_digest, get_saved_digest
from ..services.sector_service import get_sector_performance, get_all_sectors_with_symbols, get_symbol_sector

news_bp  = Blueprint("news_bp",   __name__, url_prefix="/news")
sector_bp= Blueprint("sector_bp", __name__, url_prefix="/sectors")


def _json_safe(obj):
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj

# ── News routes ────────────────────────────────────────────────────────────
@news_bp.route("/latest")
def latest_news():
    """Return today's saved news. If none, fetch live."""
    try:
        saved = get_saved_news()
        if saved:
            return jsonify(saved)
        from ..db import get_symbols
        syms = get_symbols()[:20]
        news = fetch_market_news(syms)
        if news:
            save_news_to_db(news)
        return jsonify(news)
    except Exception as e:
        print(f"[news/latest] {e}")
        return jsonify([])

@news_bp.route("/refresh", methods=["POST"])
def refresh_news():
    try:
        from ..db import get_symbols
        syms = get_symbols()[:20]
        news = fetch_market_news(syms)
        if news:
            save_news_to_db(news)
        return jsonify({"ok": True, "count": len(news)})
    except Exception as e:
        print(f"[news/refresh] {e}")
        return jsonify({"ok": False, "error": str(e), "count": 0})

@news_bp.route("/digest")
def get_digest():
    d = request.args.get("date")
    saved = get_saved_digest(d)
    if saved: return jsonify(saved)
    digest = generate_morning_digest()
    return jsonify(digest)

@news_bp.route("/digest/generate", methods=["POST"])
def generate_digest():
    try:
        digest = generate_morning_digest()
        return jsonify(digest)
    except Exception as e:
        print(f"[digest/generate] {e}")
        return jsonify({"error": str(e), "date": "", "oi_signals": [],
                        "market_summary": "Digest generation failed", "top_signals": []})

# ── Sector routes ──────────────────────────────────────────────────────────
@sector_bp.route("/performance")
def sector_performance():
    data = get_sector_performance()
    return jsonify(_json_safe(data))

@sector_bp.route("/map")
def sector_map():
    """Return sector → [symbols] mapping for heatmap filter."""
    return jsonify(get_all_sectors_with_symbols())

@sector_bp.route("/symbol/<symbol>")
def symbol_sector(symbol):
    return jsonify({"symbol": symbol, "sector": get_symbol_sector(symbol.upper())})
