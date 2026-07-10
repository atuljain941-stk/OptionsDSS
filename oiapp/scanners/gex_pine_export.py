# oiapp/scanners/gex_pine_export.py
"""GEX Pine Export -- auto-feeds TradingView indicator."""

from flask import Blueprint, jsonify, request, Response
import math, os
from datetime import date, datetime

gex_pine_bp = Blueprint("gex_pine", __name__, url_prefix="/gex")

_HERE = os.path.dirname(os.path.abspath(__file__))


@gex_pine_bp.route("/pine")
def gex_pine_export():
    """
    Returns 20 comma-separated floats for Pine Script.
    CSV order:
      0=spot 1=gamma_flip 2=pin 3=max_pain
      4=sigma_high 5=sigma_low
      6-8=call_walls(1-3) 9-11=put_walls(1-3)
      12=total_gex 13=gross_gex 14=gex_ratio
      15=pcr 16=iv_atm 17=dte 18=score 19=confidence
    Price levels default to -1 if unavailable.
    GEX values default to 0.
    """
    from .spy_strategies import (
        _compute_ta, _future_exps, _oi_rows, _compute_gex,
        _walls, _score_gex_walls, _five_factor_score, _pick_exp, _finite_number,
        compute_gex_reliability,
    )
    import yfinance as yf

    sym = (request.args.get("symbol") or "SPY").upper()
    sel_expiry = request.args.get("expiry", "")
    want_json = request.args.get("fmt", "") == "json"

    def _s(v, default=-1):
        try:
            f = float(v)
            return round(f, 4) if (f is not None and math.isfinite(f)) else default
        except Exception:
            return default

    try:
        ta = _compute_ta(sym) or {}
        spot_snapshot = None
        try:
            from ..services.market import get_spot_snapshot
            spot_snapshot = get_spot_snapshot(sym)
        except Exception:
            spot_snapshot = None

        spot = _s((spot_snapshot or {}).get("price"), -1)
        if spot <= 0:
            spot = _s(ta.get("price"), -1)
        if spot <= 0:
            err = {"error": "Could not fetch spot price for " + sym}
            return (jsonify(err), 500) if want_json else Response("-1," * 19 + "-1", mimetype="text/plain")

        # Keep all GEX/expected-move calculations anchored to the freshest available
        # market price.  Before the open this will usually be the latest premarket
        # yfinance bar; during RTH it is the latest regular intraday bar; after the
        # close it can be the latest after-hours bar.
        ta["price"] = spot

        iv_atm = _s(ta.get("iv_est", 20.0), 20.0)
        exps = _future_exps(sym) or []

        if sel_expiry and sel_expiry in exps:
            exp = sel_expiry
            dte = max(1, (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days)
        else:
            exp, dte = _pick_exp(exps, 0, 5, 0) if exps else (None, 0)

        if not exp:
            try:
                exps2 = list(yf.Ticker(sym).options[:4])
                exp = exps2[0] if exps2 else None
                if exp:
                    dte = max(1, (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days)
            except Exception:
                pass

        rows = _oi_rows(sym, exp) if exp else []
        gex = _compute_gex(rows, spot, max(1, dte or 1), iv_atm) if rows else {}
        sigma_1d = round(spot * (iv_atm / 100) / math.sqrt(252), 2) if spot > 0 else 0
        sigma_h  = round(spot + sigma_1d, 2)
        sigma_l  = round(spot - sigma_1d, 2)
        walls_data = _walls(rows, spot) if rows else {}
        wall_strength = _score_gex_walls(rows, spot, gex, side=5) if rows else {
            "top_put_walls": [], "top_call_walls": [], "score": 0, "summary": "No OI rows"
        }
        walls_data = dict(walls_data or {})
        if wall_strength.get("top_put_walls"):
            walls_data["top_put_walls"] = [(w["strike"], w["oi"]) for w in wall_strength["top_put_walls"]]
        if wall_strength.get("top_call_walls"):
            walls_data["top_call_walls"] = [(w["strike"], w["oi"]) for w in wall_strength["top_call_walls"]]

        gamma_flip = _s(gex.get("gamma_flip"), -1)
        pin_strike = _s(gex.get("pin_strike"), -1)
        max_pain   = _s(gex.get("max_pain"),   -1)
        total_gex  = _s(gex.get("total_gex"),   0)
        gross_gex  = _s(gex.get("gross_gex"),   0)
        gex_ratio  = _s(gex.get("gex_ratio"),   0)

        def _wall_strikes(key, n=3):
            items = walls_data.get(key) or []
            out = []
            for item in items[:n]:
                if isinstance(item, (list, tuple)) and len(item) >= 1:
                    out.append(_s(item[0], -1))
                elif isinstance(item, dict):
                    out.append(_s(item.get("strike", -1), -1))
                else:
                    out.append(_s(item, -1))
            while len(out) < n:
                out.append(-1)
            return out

        cw = _wall_strikes("top_call_walls", 3)
        pw = _wall_strikes("top_put_walls",  3)

        all_rows = []
        for e in (exps or [])[:5]:
            all_rows.extend(_oi_rows(sym, e))
        total_calls = sum(int(r.get("oi") or 0) for r in all_rows if r.get("type") == "call")
        total_puts  = sum(int(r.get("oi") or 0) for r in all_rows if r.get("type") == "put")
        pcr = round(total_puts / max(1, total_calls), 3)

        score, confidence, regime, _ = _five_factor_score(
            gex.get("total_gex", 0), pcr, 0, spot,
            gex.get("pin_strike", spot), gex.get("gamma_flip", spot), rows,
            gex_ratio=gex.get("gex_ratio"), max_pain=gex.get("max_pain"),
        )
        reliability = compute_gex_reliability(sym, spot, gex.get("total_gex", 0), iv_atm, dte or 1)
        if (wall_strength or {}).get("score", 0) >= 75:
            confidence = min(95, confidence + 5)

        values = [
            spot, gamma_flip, pin_strike, max_pain,
            sigma_h, sigma_l,
            cw[0], cw[1], cw[2],
            pw[0], pw[1], pw[2],
            total_gex, gross_gex, gex_ratio,
            pcr, iv_atm, dte or 0, score, confidence,
        ]
        csv_str = ",".join(str(v) for v in values)

        if want_json:
            return jsonify({
                "symbol": sym, "expiry": exp, "dte": dte,
                "updated": datetime.now().strftime("%H:%M:%S"),
                "spot": spot,
                "spot_source": (spot_snapshot or {}).get("source") or "daily_close",
                "spot_time": (spot_snapshot or {}).get("timestamp"),
                "spot_prev_close": (spot_snapshot or {}).get("prev_close"),
                "spot_change_pct": (spot_snapshot or {}).get("change_pct"),
                "gamma_flip": gamma_flip, "pin_strike": pin_strike,
                "max_pain": max_pain, "sigma_high": sigma_h, "sigma_low": sigma_l,
                "call_walls": cw, "put_walls": pw,
                "call_wall_details": (wall_strength or {}).get("top_call_walls", [])[:3],
                "put_wall_details": (wall_strength or {}).get("top_put_walls", [])[:3],
                "wall_method": (wall_strength or {}).get("method"),
                "total_gex": total_gex, "gross_gex": gross_gex, "gex_ratio": gex_ratio,
                "pcr": pcr, "iv_atm": iv_atm, "score": score, "confidence": confidence,
                "regime": regime,
                "wall_strength": wall_strength,
                "significant_walls": wall_strength,
                "vix_overlay": {
                    "vix": (reliability or {}).get("vix"),
                    "note": "VIX is a risk/reliability overlay; wall ranking uses OI, OI change, proximity/Z-score, and GEX.",
                    "reliability_score": (reliability or {}).get("score"),
                    "trade_ok": (reliability or {}).get("trade_ok"),
                },
                "csv": csv_str,
            })

        return Response(
            csv_str, mimetype="text/plain",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
        )

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        if want_json:
            return jsonify({"error": str(e), "trace": tb[:600]}), 500
        return Response("# error: " + str(e), mimetype="text/plain", status=500)


@gex_pine_bp.route("/live")
def gex_live_dashboard():
    """Auto-refreshing dashboard with Copy Pine String button."""
    sym = (request.args.get("symbol") or "SPY").upper()
    req_path = request.path   # /gex/live or /spy/gex/live
    api_base = req_path[:-5]  # strip /live

    html_path = os.path.join(_HERE, "gex_live.html")
    with open(html_path) as f:
        tpl = f.read()

    sym_links = " ".join(
        '<a href="' + req_path + '?symbol=' + s + '" style="color:#6366f1">' + s + '</a>'
        for s in ["SPY", "QQQ", "NVDA", "AAPL", "TSLA", "AMZN", "META"]
    )

    html = (tpl
        .replace("{{SYM}}", sym)
        .replace("{{API_BASE}}", api_base)
        .replace("{{REQ_PATH}}", req_path)
        .replace("{{SYM_LINKS}}", sym_links)
    )
    return Response(html, mimetype="text/html")
