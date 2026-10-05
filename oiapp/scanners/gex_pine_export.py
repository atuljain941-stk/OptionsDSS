# oiapp/scanners/gex_pine_export.py
"""GEX Pine Export -- auto-feeds TradingView indicator."""

from flask import Blueprint, jsonify, request, Response, render_template_string
import math, os, sqlite3
from datetime import date, datetime, timedelta

gex_pine_bp = Blueprint("gex_pine", __name__, url_prefix="/gex")

_HERE = os.path.dirname(os.path.abspath(__file__))


@gex_pine_bp.route("/pine")
def gex_pine_export():
    """
    Returns 22 comma-separated floats for Pine Script.
    CSV order:
      0=spot 1=gamma_flip 2=pin 3=max_pain
      4=sigma_high 5=sigma_low
      6-8=call_walls(1-3) 9-11=put_walls(1-3)
      12=total_gex 13=gross_gex 14=gex_ratio
      15=pcr 16=iv_atm 17=dte 18=score 19=confidence
      20=breakout 21=breakdown
    Price levels default to -1 if unavailable.
    GEX values default to 0.

    breakout/breakdown (V105) use the same formula as the app's own
    Daily Plan / Key Levels panel (spy_strategies._build_trade_plan):
    breakout = spot + 0.4*sigma_1d, breakdown = spot - 0.5*sigma_1d.
    These were missing from this export entirely before V105, which is
    why the TradingView chart never had BREAKOUT/BREAKDOWN lines even
    though the in-app Key Levels panel always showed them.
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
            return (jsonify(err), 500) if want_json else Response("-1," * 21 + "-1", mimetype="text/plain")

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
        # V105: same formula as spy_strategies._build_trade_plan, so the
        # app's own Daily Plan panel and this Pine export always agree.
        breakout   = _s(round(spot + sigma_1d * 0.4, 2), -1) if spot > 0 else -1
        breakdown  = _s(round(spot - sigma_1d * 0.5, 2), -1) if spot > 0 else -1

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
            breakout, breakdown,
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
                "breakout": breakout, "breakdown": breakdown,
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


def _render_gex_live_dashboard(sym: str, req_path: str, api_base: str):
    """Render the live dashboard without starting a background worker."""

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


@gex_pine_bp.route("/live")
def gex_live_dashboard():
    """Live GEX dashboard, backed by a request-time calculation."""
    sym = (request.args.get("symbol") or "SPY").upper()
    return _render_gex_live_dashboard(sym, request.path, "/gex")



# ── GEX Market Overview (on-demand only) ─────────────────────────────────
# No scheduler, timer, worker, or write is associated with this page.

_MARKET_OVERVIEW_SYMBOLS = ("SPY", "QQQ", "IWM")


def _number(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _saved_volume_snapshot(symbol, expiry):
    """Last persisted call/put volume for the expiry; read-only."""
    try:
        from ..config import DB_PATH
        con = sqlite3.connect(DB_PATH)
        row = con.execute("SELECT MAX(date) FROM options WHERE symbol=? AND expiration=?", (symbol, expiry)).fetchone()
        saved_date = row[0] if row else None
        if not saved_date:
            con.close()
            return {"date": None, "call_volume": 0, "put_volume": 0}
        totals = con.execute(
            """SELECT SUM(CASE WHEN lower(type)='call' THEN COALESCE(volume, 0) ELSE 0 END),
                      SUM(CASE WHEN lower(type)='put' THEN COALESCE(volume, 0) ELSE 0 END)
                 FROM options WHERE symbol=? AND expiration=? AND date=?""",
            (symbol, expiry, saved_date),
        ).fetchone()
        con.close()
        return {"date": str(saved_date), "call_volume": int((totals or [0, 0])[0] or 0), "put_volume": int((totals or [0, 0])[1] or 0)}
    except Exception:
        return {"date": None, "call_volume": 0, "put_volume": 0}


def _live_volume_snapshot(symbol, expiry):
    """Current option volume for a single selected expiry; never persisted."""
    try:
        import yfinance as yf
        chain = yf.Ticker(symbol).option_chain(expiry)
        calls = int(chain.calls["volume"].fillna(0).sum()) if "volume" in chain.calls else 0
        puts = int(chain.puts["volume"].fillna(0).sum()) if "volume" in chain.puts else 0
        return {"available": True, "call_volume": calls, "put_volume": puts}
    except Exception as exc:
        return {"available": False, "call_volume": 0, "put_volume": 0, "error": str(exc)[:120]}


def _overview_location(spot, gamma_flip, put_wall, call_wall):
    if spot is None:
        return "Spot unavailable"
    if put_wall and call_wall and put_wall <= spot <= call_wall:
        return "Inside put/call-wall range"
    if call_wall and spot > call_wall:
        return "Above call wall"
    if put_wall and spot < put_wall:
        return "Below put wall"
    if gamma_flip:
        return "Above gamma flip" if spot >= gamma_flip else "Below gamma flip"
    return "Location unavailable"


def _overview_trade_read(regime, spot, put_wall, call_wall, live_pcv):
    """Conservative context only; no order is placed by this page."""
    regime_text = (regime or "").lower()
    positive = "positive" in regime_text or "long" in regime_text
    negative = "negative" in regime_text or "short" in regime_text
    inside_walls = bool(put_wall and call_wall and put_wall <= spot <= call_wall)
    if negative:
        return "Wait — negative gamma can expand moves; do not sell premium solely from this view."
    if positive and inside_walls and live_pcv is not None and 0.75 <= live_pcv <= 1.25:
        return "Range setup — consider a defined-risk iron condor only if price action confirms both walls."
    if positive and put_wall and spot and spot <= put_wall * 1.01 and (live_pcv or 0) < 1.0:
        return "Support test — consider a defined-risk bull put spread only after support holds."
    if positive and call_wall and spot and spot >= call_wall * 0.99 and (live_pcv or 0) > 1.0:
        return "Resistance test — consider a defined-risk bear call spread only after resistance holds."
    return "No clear premium-selling setup — wait for price, regime, and volume flow to align."


def _live_chain_rows(symbol, expiry):
    """Compatibility wrapper for the canonical GEX chain normalizer."""
    from .spy_strategies import _live_chain_rows as _shared_live_chain_rows
    return _shared_live_chain_rows(symbol, expiry)


def _live_expiries(symbol):
    try:
        import yfinance as yf
        return sorted(str(value)[:10] for value in (yf.Ticker(symbol).options or []) if str(value)[:10])
    except Exception:
        return []


def _first_expiry_on_or_after(expiries, target):
    target = target.isoformat()
    return next((expiry for expiry in sorted(set(expiries or [])) if expiry >= target), None)


def _volume_flow_summary(symbol, expiry, spot, now_et):
    rows, error = _live_chain_rows(symbol, expiry)
    if error:
        return {"available": False, "expiry": expiry, "error": error}
    by_strike = {}
    for row in rows:
        strike = row["strike"]
        bucket = by_strike.setdefault(strike, {"strike": strike, "call_volume": 0, "put_volume": 0, "call_oi": 0, "put_oi": 0})
        side = row["type"]
        bucket[side + "_volume"] += int(row.get("volume") or 0)
        bucket[side + "_oi"] += int(row.get("oi") or 0)
    nearby = [value for value in by_strike.values() if abs(value["strike"] - spot) / max(spot, 1) <= 0.04]
    nearby = nearby or list(by_strike.values())
    calls = sorted(nearby, key=lambda item: item["call_volume"], reverse=True)[:3]
    puts = sorted(nearby, key=lambda item: item["put_volume"], reverse=True)[:3]
    call_total = sum(item["call_volume"] for item in nearby)
    put_total = sum(item["put_volume"] for item in nearby)
    call_lead = calls[0] if calls else None
    put_lead = puts[0] if puts else None
    ready = (now_et.hour, now_et.minute) >= (14, 30)
    if not ready:
        commentary = "Available after 2:30 PM ET; it will use today’s volume in tomorrow-expiry contracts."
    elif not call_lead and not put_lead:
        commentary = "Tomorrow-expiry chain has no usable volume yet."
    else:
        call_text = ("calls at $" + format(call_lead["strike"], ".2f") + " (" + format(call_lead["call_volume"], ",") + ")") if call_lead else "no concentrated calls"
        put_text = ("puts at $" + format(put_lead["strike"], ".2f") + " (" + format(put_lead["put_volume"], ",") + ")") if put_lead else "no concentrated puts"
        if call_total > put_total * 1.35:
            stance = "Call-led flow; watch the leading call strike as tomorrow’s upside magnet/resistance."
        elif put_total > call_total * 1.35:
            stance = "Put-led flow; watch the leading put strike as tomorrow’s downside magnet/support."
        else:
            stance = "Two-sided flow; treat the leading put/call strikes as a possible range until price breaks one."
        commentary = "Today’s tomorrow-expiry flow: " + call_text + "; " + put_text + ". " + stance + " This is volume flow, not confirmed OI buildup."
    return {
        "available": True, "ready": ready, "expiry": expiry,
        "call_volume": call_total, "put_volume": put_total,
        "put_call_volume_ratio": round(put_total / max(call_total, 1), 3),
        "top_calls": calls, "top_puts": puts, "commentary": commentary,
    }


def _norm_cdf(value):
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _bs_option_value(spot, strike, years, iv_pct, side):
    """Simple European option estimate used only for the Theta Clock model."""
    if not spot or not strike or years <= 0 or iv_pct <= 0:
        return 0.0
    vol = iv_pct / 100.0
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * years) / max(vol * root_t, 1e-12)
    d2 = d1 - vol * root_t
    if side == "call":
        return max(0.0, spot * _norm_cdf(d1) - strike * _norm_cdf(d2))
    return max(0.0, strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1))


def _theta_clock(rows, spot, dte, now_et, net_gex, pin, put_wall, call_wall, live_pcv=None):
    """0DTE time-risk context. It models remaining time value; it is not a dealer inventory feed."""
    if dte != 0:
        return {"available": False, "note": "Theta Clock is shown only for the current 0DTE expiry."}

    session_open = 9 * 60 + 30
    session_close = 16 * 60
    clock_minutes = now_et.hour * 60 + now_et.minute
    remaining = max(0, min(390, session_close - max(session_open, clock_minutes)))
    elapsed = max(0, min(390, clock_minutes - session_open))
    if remaining <= 0:
        return {"available": False, "note": "Regular session is closed; no 0DTE time remains."}

    strikes = sorted({_number(row.get("strike")) for row in rows if _number(row.get("strike")) is not None})
    if not strikes:
        return {"available": False, "note": "No listed strikes are available for the Theta Clock."}
    atm = min(strikes, key=lambda strike: abs(strike - spot))
    atm_rows = [row for row in rows if _number(row.get("strike")) == atm and row.get("type") in {"call", "put"}]
    ivs = []
    for row in atm_rows:
        value = _number(row.get("iv"))
        if value and value > 0:
            ivs.append(value * 100.0 if value <= 3 else value)
    iv_pct = sum(ivs) / len(ivs) if ivs else 20.0
    years_left = remaining / (252.0 * 390.0)
    full_session_years = 1.0 / 252.0
    call_iv = next((_number(row.get("iv")) for row in atm_rows if row.get("type") == "call"), None)
    put_iv = next((_number(row.get("iv")) for row in atm_rows if row.get("type") == "put"), None)
    call_iv = (call_iv * 100.0 if call_iv and call_iv <= 3 else call_iv) or iv_pct
    put_iv = (put_iv * 100.0 if put_iv and put_iv <= 3 else put_iv) or iv_pct
    live_straddle_model = _bs_option_value(spot, atm, years_left, call_iv, "call") + _bs_option_value(spot, atm, years_left, put_iv, "put")
    open_straddle_model = _bs_option_value(spot, atm, full_session_years, call_iv, "call") + _bs_option_value(spot, atm, full_session_years, put_iv, "put")
    daily_em = spot * (iv_pct / 100.0) / math.sqrt(252.0)
    remaining_em = spot * (iv_pct / 100.0) * math.sqrt(years_left)
    positive_gamma = (net_gex or 0) > 0
    inside_walls = bool(put_wall and call_wall and put_wall <= spot <= call_wall)
    if positive_gamma and inside_walls:
        posture = "Range/pin tendency: long premium needs a confirmed break and acceptance beyond a wall; time decay is working against a stalled option buyer."
    elif not positive_gamma:
        posture = "Expansion risk: negative gamma can amplify a move; use the remaining expected move and wall break/hold as the directional risk map."
    else:
        posture = "Mixed structure: use wall acceptance and live volume before choosing long or short premium."
    def distance(level):
        if level is None:
            return None
        dollars = level - spot
        return {"dollars": round(dollars, 2), "remaining_em": round(dollars / max(remaining_em, 0.01), 2)}

    put_distance = distance(put_wall)
    call_distance = distance(call_wall)
    wall_proximity = min(
        abs(put_distance["remaining_em"]) if put_distance else float("inf"),
        abs(call_distance["remaining_em"]) if call_distance else float("inf"),
    )
    flow_note = "Put/call volume is balanced."
    if live_pcv is not None and live_pcv >= 1.25:
        flow_note = "Put volume is relatively heavy; confirm price before treating it as bearish."
    elif live_pcv is not None and live_pcv <= 0.80:
        flow_note = "Call volume is relatively heavy; confirm price before treating it as bullish."

    if remaining <= 45:
        trade_commentary = {
            "label": "Late-session risk control",
            "strategy": "Avoid a fresh long-premium entry unless price has already accepted beyond a wall. Reduce size and take profits faster.",
            "trigger": "A clean break, retest, and hold beyond the relevant wall with price still moving.",
            "invalidation": "Price returns inside the wall range or stalls near the pin.",
            "risk": "With only %d minutes left, time decay can overwhelm a correct but late directional read. %s" % (remaining, flow_note),
        }
    elif positive_gamma and inside_walls:
        near_wall = wall_proximity <= 0.20
        trade_commentary = {
            "label": "Conditional range / pin trade",
            "strategy": "If price remains between the walls, favor a defined-risk iron condor outside them; near a wall, wait for a rejection before a small range-fade.",
            "trigger": "Price holds inside %.2f–%.2f and rejects a wall; avoid entry on a clean acceptance through it." % (put_wall, call_wall),
            "invalidation": "A 5-minute close and retest that holds beyond the put or call wall.",
            "risk": "%s Remaining expected move is %.2f; do not sell premium into an expanding move." % (flow_note, remaining_em),
        }
        if near_wall:
            trade_commentary["strategy"] = "At the nearby wall, wait for a rejection before a small defined-risk range-fade; do not pre-empt a break."
    elif not positive_gamma and call_wall is not None and spot > call_wall:
        trade_commentary = {
            "label": "Conditional upside expansion",
            "strategy": "After a break, retest, and hold above the call wall, use a defined-risk call debit vertical around 0.30 delta; avoid naked short premium.",
            "trigger": "Acceptance above %.2f followed by a successful retest." % call_wall,
            "invalidation": "Price closes back below the call wall; use the EMA13 or the retest low as a management reference.",
            "risk": "%s Negative GEX is a volatility regime label, not proof of dealer positioning." % flow_note,
        }
    elif not positive_gamma and put_wall is not None and spot < put_wall:
        trade_commentary = {
            "label": "Conditional downside expansion",
            "strategy": "After a break, retest, and hold below the put wall, use a defined-risk put debit vertical around 0.30 delta; avoid naked short premium.",
            "trigger": "Acceptance below %.2f followed by a failed retest." % put_wall,
            "invalidation": "Price closes back above the put wall; use the EMA13 or the retest high as a management reference.",
            "risk": "%s Negative GEX is a volatility regime label, not proof of dealer positioning." % flow_note,
        }
    else:
        trade_commentary = {
            "label": "Wait for the wall break",
            "strategy": "No trade inside the structure. Use a defined-risk 0.30-delta debit vertical only after price accepts beyond a wall and retests it.",
            "trigger": "Break, retest, and hold beyond %.2f (downside) or %.2f (upside)." % (put_wall or 0, call_wall or 0),
            "invalidation": "The retest fails and price returns to the pin/range.",
            "risk": "%s Remaining expected move is %.2f; a wall touch alone is not a directional signal." % (flow_note, remaining_em),
        }
    return {
        "available": True, "minutes_remaining": remaining, "minutes_elapsed": elapsed,
        "atm_strike": atm, "atm_iv_pct": round(iv_pct, 2),
        "daily_expected_move": round(daily_em, 2), "remaining_expected_move": round(remaining_em, 2),
        "half_shelves": {"lower": round(spot - 0.5 * daily_em, 2), "upper": round(spot + 0.5 * daily_em, 2)},
        "model_atm_straddle": round(live_straddle_model, 2),
        "model_open_straddle": round(open_straddle_model, 2),
        "model_time_value_spent": round(max(0.0, open_straddle_model - live_straddle_model), 2),
        "pin_distance": distance(pin), "put_wall_distance": put_distance, "call_wall_distance": call_distance,
        "posture": posture, "trade_commentary": trade_commentary,
        "note": "ATM straddle and decay are Black-Scholes time-value estimates using current IV, not historical traded premiums or confirmed dealer hedges.",
    }


def _market_overview_row(symbol):
    """Saved GEX/OI plus request-time 0DTE chain and tomorrow-expiry volume."""
    from .spy_strategies import (
        _compute_ta, _compute_gex, _score_gex_walls, _five_factor_score,
        _bs_gamma, _compute_skew_rr, _vix_context, _canonical_gex_input,
        _future_exps,
    )
    from .gex_analysis import _latest_stamp, _saved_rows, _oi_by_stamp, _stamp_on_or_before, _prior_business_day
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.now()
    ta = _compute_ta(symbol) or {}
    try:
        from ..services.market import get_spot_snapshot
        spot_snapshot = get_spot_snapshot(symbol) or {}
    except Exception:
        spot_snapshot = {}
    spot = _number(spot_snapshot.get("price"), _number(ta.get("price")))
    if not spot:
        raise ValueError("Live spot is unavailable")

    stamp = _latest_stamp(symbol)
    saved_all = _saved_rows(symbol, stamp, "all") if stamp else []
    saved_expiries = sorted({str(row.get("expiration") or "")[:10] for row in saved_all if str(row.get("expiration") or "")[:10]})
    live_expiries = _live_expiries(symbol)
    today = now_et.date()
    # Use the same expiry universe and canonical 0DTE input as the Day Plan.
    available_expiries = sorted(set(live_expiries + _future_exps(symbol) + saved_expiries))
    expiry = _first_expiry_on_or_after(available_expiries, today)
    if not expiry:
        raise ValueError("No current or future option expiry is available")
    dte = max(0, (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days)
    canonical_input = _canonical_gex_input(symbol, expiry, spot, _number(ta.get("iv_est"), 20.0))
    spot = _number(canonical_input.get("spot"), spot)
    rows = canonical_input.get("rows") or []
    source = canonical_input.get("source") or "saved option snapshot"
    live_rows = rows if source == "live option chain" else []
    live_error = canonical_input.get("live_error")
    iv_atm = _number(canonical_input.get("iv_atm"), _number(ta.get("iv_est"), 20.0))
    if not rows:
        raise ValueError(live_error or "No option rows for selected expiry")

    # Normalize only; current GEX must not depend on a page-specific OI-change
    # enrichment.  OI-change remains a saved-snapshot diagnostic.
    normalized = []
    for raw in rows:
        side = str(raw.get("type") or "").lower()
        side = "call" if side.startswith("c") else "put" if side.startswith("p") else ""
        strike = _number(raw.get("strike"))
        if not side or strike is None:
            continue
        item = dict(raw)
        item["type"] = side
        normalized.append(item)
    rows = normalized
    gex = _compute_gex(rows, spot, max(1, dte), iv_atm) if rows else {}
    wall_strength = _score_gex_walls(rows, spot, gex, side=5) if rows else {}
    calls_oi = sum(int(row.get("oi") or 0) for row in rows if row["type"] == "call")
    puts_oi = sum(int(row.get("oi") or 0) for row in rows if row["type"] == "put")
    oi_pcr = round(puts_oi / max(calls_oi, 1), 3)
    skew_rr = _compute_skew_rr(rows, spot, max(1, dte), iv_atm)
    vix_ctx = _vix_context()
    score, confidence, regime, _ = _five_factor_score(
        gex.get("total_gex", 0), oi_pcr, skew_rr, spot,
        gex.get("pin_strike", spot), gex.get("gamma_flip", spot), rows,
        gex_ratio=gex.get("gex_ratio"), max_pain=gex.get("max_pain"),
        dte=dte, vix_level=(vix_ctx or {}).get("level"),
        gex_strength_pct=gex.get("net_gex_share_pct"),
    )
    top_puts, top_calls = wall_strength.get("top_put_walls") or [], wall_strength.get("top_call_walls") or []
    put_wall = _number(top_puts[0].get("strike")) if top_puts else None
    call_wall = _number(top_calls[0].get("strike")) if top_calls else None

    gamma_by_strike = {}
    for option in rows:
        strike, oi = _number(option.get("strike")), _number(option.get("oi"), 0)
        if strike is None or not oi:
            continue
        gamma = _number(option.get("gamma"))
        if gamma is None or gamma <= 0:
            strike_iv = _number(option.get("iv"), iv_atm) or iv_atm
            gamma = _bs_gamma(spot, strike, max(1, dte), strike_iv * 100 if strike_iv <= 3 else strike_iv)
        if gamma is None or gamma <= 0:
            continue
        exposure = abs(gamma * oi * 100 * spot * spot * 0.01)
        item = gamma_by_strike.setdefault(strike, {"strike": strike, "call": 0.0, "put": 0.0})
        # Preserve put magnitude as a positive number in data. The renderer
        # alone applies the negative sign, preventing accidental double signs.
        item[option["type"]] += exposure
    gamma_by_strike = sorted(gamma_by_strike.values(), key=lambda item: abs(item["strike"] - spot))[:36]
    gamma_by_strike.sort(key=lambda item: item["strike"])
    gamma_totals = {"call": round(sum(item["call"] for item in gamma_by_strike), 2), "put": round(sum(item["put"] for item in gamma_by_strike), 2)}

    live = {"available": bool(live_rows), "call_volume": sum(int(row.get("volume") or 0) for row in rows if row["type"] == "call"), "put_volume": sum(int(row.get("volume") or 0) for row in rows if row["type"] == "put")}
    saved = _saved_volume_snapshot(symbol, expiry)
    live_pcv = round(live["put_volume"] / max(live["call_volume"], 1), 3) if live["available"] else None
    tomorrow_expiry = _first_expiry_on_or_after(live_expiries, today + timedelta(days=1))
    tomorrow_flow = _volume_flow_summary(symbol, tomorrow_expiry, spot, now_et) if tomorrow_expiry else {"available": False, "error": "No tomorrow expiry is listed."}
    return {
        "symbol": symbol, "expiry": expiry, "dte": dte,
        "chain_source": source, "spot": spot,
        "chain_asof": canonical_input.get("fetched_at"),
        "gex_input": canonical_input.get("signature"),
        "gex_input_cache_age_seconds": canonical_input.get("cache_age_seconds"),
        "iv_atm": round(iv_atm, 2), "pcr": oi_pcr, "skew_rr": skew_rr,
        "spot_source": spot_snapshot.get("source") or "technical fallback",
        "regime": regime, "score": score, "confidence": confidence,
        "net_gex": _number(gex.get("total_gex"), 0),
        "gex_strength": _number(gex.get("regime_strength"), 0),
        "gex_strength_label": gex.get("regime_strength_label"),
        "net_gex_share_pct": _number(gex.get("net_gex_share_pct"), 0),
        "gamma_flip": _number(gex.get("gamma_flip")),
        "pin": _number(gex.get("pin_strike")), "max_pain": _number(gex.get("max_pain")), "put_wall": put_wall, "call_wall": call_wall,
        "location": _overview_location(spot, _number(gex.get("gamma_flip")), put_wall, call_wall),
        "gamma_by_strike": gamma_by_strike, "gamma_totals": gamma_totals,
        "live_volume": live, "saved_volume": saved, "live_pcv": live_pcv,
        "tomorrow_flow": tomorrow_flow,
        "theta_clock": _theta_clock(rows, spot, dte, now_et, _number(gex.get("total_gex"), 0), _number(gex.get("pin_strike")), put_wall, call_wall, live_pcv),
        "trade_read": _overview_trade_read(regime, spot, put_wall, call_wall, live_pcv),
    }


@gex_pine_bp.route("/market-overview")
@gex_pine_bp.route("/market-overview/")
def gex_market_overview():
    """In-app SPY/QQQ/IWM overview. All work is performed at request time."""
    results, errors = [], []
    for symbol in _MARKET_OVERVIEW_SYMBOLS:
        try:
            results.append(_market_overview_row(symbol))
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)[:180]})
    if request.args.get("format") == "json":
        return jsonify({"results": results, "errors": errors, "updated": datetime.now().isoformat(timespec="seconds")})
    return render_template_string(_MARKET_OVERVIEW_TEMPLATE)


@gex_pine_bp.route("/market-overview.js")
def gex_market_overview_script():
    """Same-origin renderer; avoids inline-script CSP restrictions."""
    return Response(
        _MARKET_OVERVIEW_SCRIPT,
        mimetype="application/javascript",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


_MARKET_OVERVIEW_SCRIPT = r"""
var overviewRows=[],mode='net',grid=document.getElementById('grid'),statusEl=document.getElementById('status'),diagnosticEl=document.getElementById('diagnostic'),refreshButton=document.getElementById('refresh'),intervalSelect=document.getElementById('interval');
function diagnostic(message){if(diagnosticEl)diagnosticEl.textContent='Diagnostics v20260930.4: '+message}
window.addEventListener('error',function(e){diagnostic('Browser error — '+(e.message||'unknown error'))});
window.addEventListener('unhandledrejection',function(e){diagnostic('Unhandled request error — '+String(e.reason||'unknown error'))});
diagnostic('browser script started; requesting API…');var n=function(v){return v==null?'—':typeof v==='number'?v.toLocaleString(undefined,{maximumFractionDigits:2}):v};function row(k,v,c){return '<div class="metric '+(c||'')+'"><span>'+k+'</span><b>'+v+'</b></div>'}
function gammaChart(x){var data=x.gamma_by_strike||[];if(!data.length)return '<p class=muted>No gamma data for this expiry.</p>';var firstStrike=data[0].strike,lastStrike=data[data.length-1].strike,spotPct=Math.max(2,Math.min(98,(x.spot-firstStrike)/(lastStrike-firstStrike||1)*100));var values=[];data.forEach(function(d){if(mode==='net')values.push(d.call-d.put);else if(mode==='absolute')values.push(d.call+d.put);else{values.push(d.call);values.push(-d.put)}});var scale=Math.max.apply(null,values.map(Math.abs))||1,split=mode!=='absolute',zero=split?50:92,html='<div class="gamma"><b class="gamma-title">'+({net:'Net gamma exposure',absolute:'Absolute gamma exposure',split:'Call vs put gamma exposure'}[mode])+'</b>';if(split)html+='<i class="gamma-zero" style="top:50%"></i>';data.forEach(function(d,i){var step=100/data.length,left=i*step+step*.14,width=Math.max(.55,step*(mode==='split'?.31:.68));function bar(value,color,shift){var height=Math.max(value?1:0,Math.abs(value)/scale*44),top=value>=0?zero-height:zero;return '<span class="gamma-bar" title="'+x.symbol+' $'+d.strike+' gamma: '+n(value)+'" style="left:'+(left+(shift||0))+'%;width:'+width+'%;top:'+top+'%;height:'+height+'%;background:'+color+'"></span>'}if(mode==='net'){var net=d.call-d.put;html+=bar(net,net>=0?'#5790e8':'#f0646b',0)}else if(mode==='absolute'){html+=bar(d.call+d.put,'#5790e8',0)}else{html+=bar(d.call,'#5790e8',0)+bar(-d.put,'#f0646b',width+step*.08)}});html+='<i class="gamma-spotline" style="left:'+spotPct+'%"></i><small class="gamma-label left">'+n(firstStrike)+'</small><small class="gamma-label right">'+n(lastStrike)+'</small><small class="gamma-label spot" style="left:'+spotPct+'%">Spot '+n(x.spot)+'</small></div>';return html}
function levels(items,side){return(items||[]).map(function(x){return '$'+n(x.strike)+' ('+n(x[side+'_volume'])+')'}).join(', ')||'—'}
function tomorrow(x){var f=x.tomorrow_flow||{};if(!f.available)return '<div class=flow><b>Tomorrow view</b><p class=muted>'+((f.error)||'Tomorrow expiry unavailable.')+'</p></div>';return '<div class=flow><b>Tomorrow view — '+f.expiry+' (today\'s volume)</b><p>'+f.commentary+'</p><p class=muted>Top calls: '+levels(f.top_calls,'call')+'<br>Top puts: '+levels(f.top_puts,'put')+'<br>Tomorrow-expiry P/C volume: '+n(f.put_call_volume_ratio)+'</p></div>'}
function thetaClock(x){var t=x.theta_clock||{};if(!t.available)return '<div class=clock><b>0DTE Theta Clock</b><p class=muted>'+((t.note)||'Unavailable.')+'</p></div>';function d(v){return v==null?'—':(v.dollars>=0?'+':'')+n(v.dollars)+' ('+n(v.remaining_em)+'× remaining EM)'}var c=t.trade_commentary||{};var commentary=c.label?'<div class=trade-commentary><b>Trade commentary — '+c.label+'</b><p><b>Strategy:</b> '+c.strategy+'</p><p><b>Trigger:</b> '+c.trigger+'</p><p><b>Invalidation:</b> '+c.invalidation+'</p><p class=muted>'+c.risk+'</p></div>':'';return '<div class=clock><b>0DTE Theta Clock — '+n(t.minutes_remaining)+' min remaining</b>'+row('ATM / ATM IV',n(t.atm_strike)+' / '+n(t.atm_iv_pct)+'%')+row('Daily / remaining EM',n(t.daily_expected_move)+' / '+n(t.remaining_expected_move))+row('Half-EM shelves',n(t.half_shelves.lower)+' / '+n(t.half_shelves.upper))+row('ATM straddle model now',n(t.model_atm_straddle))+row('Model time value spent',n(t.model_time_value_spent))+row('Pin distance',d(t.pin_distance))+row('Put / call wall distance',d(t.put_wall_distance)+' / '+d(t.call_wall_distance))+'<p><b>Posture:</b> '+t.posture+'</p>'+commentary+'<p class=muted>'+t.note+'</p></div>'}
function card(x){var l=x.live_volume||{},s=x.saved_volume||{},g=x.gamma_totals||{},dc=(l.call_volume||0)-(s.call_volume||0),dp=(l.put_volume||0)-(s.put_volume||0),klass=(x.regime||'').toLowerCase().includes('positive')?'good':'bad';return '<section class=card><h2>'+x.symbol+' <small class=muted>'+x.expiry+' • '+x.dte+' DTE</small></h2><p class=muted>Chain: '+x.chain_source+'</p>'+row('Regime',x.regime,klass)+row('Live spot',n(x.spot))+row('Price location',x.location)+row('Net GEX',n(x.net_gex))+row('Gamma flip',n(x.gamma_flip))+row('Balance pin',n(x.pin))+row('Max pain',n(x.max_pain))+row('Put / call wall',n(x.put_wall)+' / '+n(x.call_wall))+row('Live call / put volume',n(l.call_volume)+' / '+n(l.put_volume))+row('Live put/call volume',n(x.live_pcv))+row('Chart call / put gamma',n(g.call)+' / '+n(g.put))+row('Volume vs saved','C '+(dc>=0?'+':'')+n(dc)+' • P '+(dp>=0?'+':'')+n(dp))+'<p><b>Read:</b> '+x.trade_read+'</p>'+thetaClock(x)+tomorrow(x)+gammaChart(x)+'</section>'}
function draw(){grid.innerHTML=overviewRows.map(card).join('')||'<p>No GEX data is available.</p>'}document.querySelectorAll('[data-mode]').forEach(function(b){b.onclick=function(){mode=b.dataset.mode;document.querySelectorAll('[data-mode]').forEach(function(x){x.classList.toggle('active',x===b)});draw()}});async function load(){statusEl.textContent='Loading live 0DTE GEX and tomorrow-expiry volume…';diagnostic('requesting /gex/market-overview?format=json');try{var r=await fetch('/gex/market-overview?format=json',{cache:'no-store'}),raw=await r.text();if(!r.ok)throw new Error('HTTP '+r.status+': '+raw.slice(0,180));var d=JSON.parse(raw);overviewRows=d.results||[];draw();var apiErrors=(d.errors||[]).map(function(e){return e.symbol+': '+e.error}).join(' | ');statusEl.textContent='Updated '+d.updated+(apiErrors?' • '+apiErrors:'');diagnostic('API responded: '+overviewRows.length+' card(s)'+(apiErrors?'; errors: '+apiErrors:'; no API errors.'))}catch(e){statusEl.textContent='Could not load overview: '+e.message;diagnostic('API request failed — '+e.message)}}var refreshTimer=null;function setRefreshInterval(){if(refreshTimer){clearInterval(refreshTimer);refreshTimer=null}var seconds=Number(intervalSelect.value||0);if(seconds)refreshTimer=setInterval(load,seconds*1000)}intervalSelect.onchange=setRefreshInterval;window.addEventListener('pagehide',function(){if(refreshTimer)clearInterval(refreshTimer)});refreshButton.onclick=load;load();
"""


_MARKET_OVERVIEW_TEMPLATE = """<!doctype html>
<title>GEX Market Overview</title>
<style>
body{background:#0b1120;color:#e5e7eb;font:14px system-ui;margin:24px}.top{display:flex;gap:16px;align-items:center;flex-wrap:wrap}.controls{display:flex;gap:6px}.controls button{background:#1f2937}.controls button.active{background:#2563eb}.grid{display:grid;grid-template-columns:repeat(3,minmax(320px,1fr));gap:16px;margin-top:18px}.card{background:#111827;border:1px solid #263349;border-radius:10px;padding:16px}.good{color:#60a5fa}.bad{color:#f87171}.muted{color:#9ca3af}.metric{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-bottom:1px solid #1f2937}button{background:#2563eb;color:white;border:0;border-radius:6px;padding:9px 14px;cursor:pointer}.gamma{position:relative;width:100%;height:260px;margin-top:14px;background:#0b1018;border-radius:7px;overflow:hidden}.gamma-bar{position:absolute;min-height:1px;border-radius:2px 2px 0 0}.gamma-zero{position:absolute;left:4%;right:4%;height:1px;background:#334155}.gamma-spotline{position:absolute;top:24px;bottom:22px;width:2px;background:repeating-linear-gradient(to bottom,#60a5fa 0,#60a5fa 5px,transparent 5px,transparent 9px)}.gamma-title{position:absolute;top:7px;left:12px;font-size:12px}.gamma-label{position:absolute;bottom:7px;color:#94a3b8}.gamma-label.left{left:12px}.gamma-label.right{right:12px}.gamma-label.spot{top:28px;right:12px;bottom:auto;color:#60a5fa}.axis{stroke:#334155;stroke-width:1}.spot{stroke:#60a5fa;stroke-width:2;stroke-dasharray:4 3}.label{fill:#94a3b8;font-size:10px}.chart-title{fill:#e5e7eb;font-size:12px;font-weight:600}.clock{margin-top:12px;padding:10px;border-left:3px solid #60a5fa;background:#101a2b;border-radius:5px}.clock p{margin:6px 0}.trade-commentary{margin-top:10px;padding:9px;border-left:3px solid #fbbf24;background:#1f1a0c;border-radius:4px}.trade-commentary p{margin:5px 0}.flow{margin-top:12px;padding:10px;border-left:3px solid #a78bfa;background:#121827;border-radius:5px}.flow p{margin:6px 0}
</style>
<div class=top><h2>GEX Market Overview</h2><button id=refresh>Refresh live view</button><a id=apiDebug target=_blank href="/gex/market-overview?format=json" class=muted>Open API debug</a><label class=muted>Auto refresh <select id=interval><option value=0>Off</option><option value=60>1 minute</option><option value=300>5 minutes</option><option value=900>15 minutes</option></select></label><div class=controls><button data-mode=net class=active>Net gamma</button><button data-mode=absolute>Absolute gamma</button><button data-mode=split>Put / call gamma</button></div><span class=muted id=status>Live 0DTE GEX + tomorrow-expiry flow</span></div><p id=diagnostic class=muted>Diagnostics v20260930.4: page loaded; waiting for browser script.</p><div id=grid class=grid><p class=muted>Loading GEX cards…</p></div>
<script src="/gex/market-overview.js?v=20260930.5"></script>"""

