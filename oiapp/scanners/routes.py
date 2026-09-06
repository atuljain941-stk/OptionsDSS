from flask import Blueprint, jsonify, request, render_template
from .large_oi_change import run_large_oi_scanner
from .pcr_change import run_pcr_scanner
from .weekly_pin import run_weekly_pin_scanner
from .oi_wall_map import get_oi_wall_map
from .symbol_screener import run_screener
from .sr_breakout_scanner import scan_sr_breakouts, scan_sr_breakout_age, _get_wl_symbols
import sqlite3
from ..db import run_select_query

def _apply_sector_filter(symbols, sector):
    """Filter symbol list by sector if specified."""
    if not sector: return symbols
    try:
        from ..services.sector_service import get_symbol_sector
        return [s for s in symbols if get_symbol_sector(s) == sector]
    except: return symbols



scanner_bp   = Blueprint("scanner",      __name__, url_prefix="/api/scanner")
sqlviewer_bp = Blueprint("sqlviewer_bp", __name__, url_prefix="/sqlviewer")


@scanner_bp.route("/")
def home():
    return render_template("index.html")


@scanner_bp.route("/large_oi", methods=["GET"])
def api_large_oi():
    min_abs = request.args.get("min_abs",  1000, type=int)
    min_pct = request.args.get("min_pct",  15,   type=int)
    max_dte = request.args.get("max_dte",  60,   type=int)
    data = run_large_oi_scanner(min_abs=min_abs, min_pct=min_pct, max_dte=max_dte)
    return jsonify(data)


@scanner_bp.route("/pcr_change", methods=["GET"])
def api_pcr_change():
    threshold  = request.args.get("threshold",  10,   type=int)
    max_dte    = request.args.get("max_dte",     60,   type=int)
    per_expiry = request.args.get("per_expiry", "true").lower() == "true"
    data = run_pcr_scanner(threshold=threshold, max_dte=max_dte, per_expiry=per_expiry)
    return jsonify(data)


@scanner_bp.route("/weekly_pin", methods=["GET"])
def api_weekly_pin():
    include_monthly = request.args.get("monthly", "true").lower() == "true"
    data = run_weekly_pin_scanner(include_monthly=include_monthly)
    return jsonify(data)


@scanner_bp.route("/oi_wall_map", methods=["GET"])
def api_oi_wall_map():
    symbol  = (request.args.get("symbol") or "SPY").upper()
    top_n   = request.args.get("top_n",   10,  type=int)
    max_dte = request.args.get("max_dte", 60,  type=int)
    data = get_oi_wall_map(symbol, top_n=top_n, max_dte=max_dte)
    return jsonify(data)


@sqlviewer_bp.route("/sqlviewer")
def sqlviewer_page():
    return render_template("sqlviewer.html")


@sqlviewer_bp.route("/run_sql", methods=["POST"])
def run_sql():
    query = request.json.get("query", "").strip()
    if not query:
        return jsonify({"error": "No SQL query provided."}), 400
    try:
        result = run_select_query(query)
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@scanner_bp.route("/symbol_screener", methods=["GET"])
def api_symbol_screener():
    from .symbol_screener import run_screener, run_price_screener

    mode      = request.args.get("mode", "pcr_change")
    threshold = request.args.get("threshold", 10.0, type=float)
    vol_ratio = request.args.get("vol_ratio", 1.2, type=float)
    mom_x     = request.args.get("mom_x", 2.0, type=float)
    wl_id     = request.args.get("watchlist_id", None, type=int)
    sector    = (request.args.get("sector") or "").strip()

    # Resolve watchlist symbols
    wl_syms = None
    if wl_id:
        try:
            import sqlite3 as _sq2
            from pathlib import Path as _P2
            from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
            _db2 = _OIAPP_DB_PATH
            _c2  = _sq2.connect(_db2)
            _rows = _c2.execute(
                "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                (wl_id,)
            ).fetchall()
            _c2.close()
            wl_syms = [r[0] for r in _rows] if _rows else None
        except: pass

    # Price-based modes use yfinance directly — not limited to options DB symbols
    if mode in ("high_volume", "large_momentum"):
        data = run_price_screener(mode=mode, vol_ratio=vol_ratio, mom_x=mom_x,
                                   symbols=wl_syms)  # wl_syms=None → uses symbols table
    else:
        # OI-based modes (pcr_change, oi_change)
        data = run_screener(mode=mode, threshold=threshold, vol_ratio=vol_ratio, mom_x=mom_x)
        if wl_syms:
            wl_set = set(wl_syms)
            data = [r for r in data if r.get("symbol") in wl_set]

    # Sector filter (for OI-based modes)
    if sector and mode not in ("high_volume", "large_momentum"):
        try:
            from ..services.sector_service import get_symbol_sector
            data = [r for r in data if get_symbol_sector(r.get("symbol","")) == sector]
        except: pass

    return jsonify(data)


@scanner_bp.route("/opportunities", methods=["GET"])
def api_opportunities():
    """
    Full watchlist scan — credit spreads with 1:1+ R:R, $5 wide, ≤30 DTE,
    no earnings within 30 days. Classifies as TRENDING/MEAN_REVERSION/SIDEWAYS.
    """
    from .opportunity_scanner import run_opportunity_scanner
    min_dte  = request.args.get("min_dte",  21,  type=int)
    max_dte  = request.args.get("max_dte",  60,  type=int)
    wing     = request.args.get("wing",     5.0, type=float)
    sector   = request.args.get("sector", "").strip()
    result   = run_opportunity_scanner(
        min_dte=min_dte,
        max_dte=max_dte,
        preferred_wing=wing,
        sector_filter=sector or None,
    )
    return jsonify(result)

@scanner_bp.route("/sr/age_scan", methods=["GET"])
def api_sr_breakout_age():
    """Breakout-age scanner: symbols with breakouts within the last x days."""
    min_strength   = request.args.get("min_strength", 40, type=int)
    require_mom    = request.args.get("require_momentum", "true").lower() != "false"
    days_back      = request.args.get("days_back", 10, type=int)
    sector         = (request.args.get("sector", "") or "").strip()
    wl_id          = request.args.get("watchlist_id", None, type=int)
    min_earn_days  = request.args.get("min_earn_days", None)
    if min_earn_days in (None, "", "None", "null"):
        min_earn_days = None
    else:
        try:
            min_earn_days = int(min_earn_days)
        except Exception:
            min_earn_days = None

    try:
        syms = _get_wl_symbols(wl_id) or []
        syms = _apply_sector_filter(syms, sector)
        if not syms:
            from ..config import DB_PATH as _OIAPP_DB_PATH
            con = sqlite3.connect(_OIAPP_DB_PATH)
            con.row_factory = sqlite3.Row
            syms = [r[0] for r in con.execute("SELECT symbol FROM symbols WHERE symbol IS NOT NULL").fetchall()]
            con.close()
            syms = _apply_sector_filter(syms, sector)
        if not syms:
            syms = None
        result = scan_sr_breakout_age(syms, days_back=days_back, min_strength=min_strength,
                                      require_momentum=require_mom, sector_filter=sector)
        if min_earn_days is not None:
            from .scoring_service import filter_by_min_earnings
            result = filter_by_min_earnings(result, min_earn_days)

        try:
            import json as _j, datetime as _d, sqlite3 as _s
            from pathlib import Path as _P
            ts = _d.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
            db = _OIAPP_DB_PATH
            con = _s.connect(db)
            con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
            con.execute("INSERT OR REPLACE INTO app_cache VALUES ('sr_breakout_age_scan', ?, ?)", (_j.dumps(result), ts))
            con.commit(); con.close()
        except Exception:
            pass

        return jsonify({
            "results": result,
            "count": len(result),
            "completed_at": __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "params": {"days_back": days_back, "min_strength": min_strength, "require_momentum": require_mom, "sector": sector, "min_earn_days": min_earn_days},
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:400]}), 500


@scanner_bp.route("/sectors")
def api_sectors():
    """Return {symbol: sector} map from sector_cache."""
    import sqlite3
    from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
    db = _OIAPP_DB_PATH
    try:
        con = sqlite3.connect(db)
        rows = con.execute("SELECT symbol, sector FROM sector_cache WHERE sector IS NOT NULL").fetchall()
        con.close()
        return __import__('flask').jsonify({r[0]: r[1] for r in rows})
    except: return __import__('flask').jsonify({})
