# oiapp/scanners/gex_pine_export.py
"""GEX Pine Export -- auto-feeds TradingView indicator."""

from flask import Blueprint, jsonify, request, Response, render_template_string
import math, os, sqlite3
from datetime import date, datetime

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


def _market_overview_row(symbol):
    """Merge saved GEX/OI with request-time spot and put/call volume."""
    from .spy_strategies import _compute_ta, _compute_gex, _score_gex_walls, _five_factor_score, _bs_gamma
    from .gex_analysis import _latest_stamp, _saved_rows, _oi_by_stamp, _stamp_on_or_before, _prior_business_day
    ta = _compute_ta(symbol) or {}
    try:
        from ..services.market import get_spot_snapshot
        spot_snapshot = get_spot_snapshot(symbol) or {}
    except Exception:
        spot_snapshot = {}
    spot = _number(spot_snapshot.get("price"), _number(ta.get("price")))
    # Match Saved GEX Analysis: use its latest exact chain snapshot and its
    # prior-business-day OI comparison, rather than an independently chosen
    # cached expiry/row set.
    stamp = _latest_stamp(symbol)
    all_rows = _saved_rows(symbol, stamp, "all") if stamp else []
    expiries = sorted({str(row.get("expiration") or "")[:10] for row in all_rows if str(row.get("expiration") or "")[:10] >= date.today().isoformat()})
    expiry = expiries[0] if expiries else None
    if not expiry or not spot:
        raise ValueError("Saved option-chain data or live spot is unavailable")
    dte = max(0, (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days)
    previous_stamp = _stamp_on_or_before(symbol, _prior_business_day())
    previous_oi = _oi_by_stamp(symbol, previous_stamp, expiry)
    rows = []
    for raw in _saved_rows(symbol, stamp, expiry):
        kind = str(raw.get("type") or "").lower()
        side = "call" if kind.startswith("c") else "put" if kind.startswith("p") else kind
        strike = _number(raw.get("strike"))
        current_oi = int(_number(raw.get("oi"), 0) or 0)
        prior_oi = int(previous_oi.get((side[:1], strike), 0) or 0) if strike is not None else 0
        item = dict(raw)
        item["type"] = side
        item["oi_change"] = current_oi - prior_oi
        item["prev_oi"] = prior_oi
        rows.append(item)
    iv_atm = _number(ta.get("iv_est"), 20.0)
    gex = _compute_gex(rows, spot, max(1, dte or 1), iv_atm) if rows else {}
    wall_strength = _score_gex_walls(rows, spot, gex, side=5) if rows else {}
    calls_oi = sum(int(row.get("oi") or 0) for row in rows if row.get("type") == "call")
    puts_oi = sum(int(row.get("oi") or 0) for row in rows if row.get("type") == "put")
    oi_pcr = round(puts_oi / max(calls_oi, 1), 3)
    score, confidence, regime, _ = _five_factor_score(gex.get("total_gex", 0), oi_pcr, 0, spot, gex.get("pin_strike", spot), gex.get("gamma_flip", spot), rows, gex_ratio=gex.get("gex_ratio"), max_pain=gex.get("max_pain"))
    top_puts = wall_strength.get("top_put_walls") or []
    top_calls = wall_strength.get("top_call_walls") or []
    put_wall = _number(top_puts[0].get("strike")) if top_puts else None
    call_wall = _number(top_calls[0].get("strike")) if top_calls else None
    gamma_by_strike = {}
    # Gamma exposure per 1% underlying move.  Calls are plotted positive and
    # puts negative to make the same call-vs-put relationship visible as the
    # existing GEX chart.
    for option in rows:
        strike = _number(option.get("strike"))
        gamma = _number(option.get("gamma"), 0.0)
        oi = _number(option.get("oi"), 0.0)
        if strike is None or not oi:
            continue
        # Some saved broker/yfinance snapshots have IV and OI but no gamma.
        # Derive Black-Scholes gamma from that same saved IV so the overview
        # still renders the identical per-strike exposure chart.
        if not gamma:
            strike_iv = _number(option.get("iv"), iv_atm) or iv_atm
            if strike_iv <= 3:
                strike_iv *= 100.0
            gamma = _bs_gamma(spot, strike, max(1, dte), strike_iv)
        if not gamma:
            continue
        exposure = abs(gamma * oi * 100 * spot * spot * 0.01)
        item = gamma_by_strike.setdefault(strike, {"strike": strike, "call": 0.0, "put": 0.0})
        if str(option.get("type", "")).lower() == "call":
            item["call"] += exposure
        elif str(option.get("type", "")).lower() == "put":
            item["put"] -= exposure
    # Keep the rendered chart readable if the selected expiry has many strikes.
    gamma_by_strike = sorted(gamma_by_strike.values(), key=lambda item: abs(item["strike"] - spot))[:36]
    gamma_by_strike.sort(key=lambda item: item["strike"])

    live = _live_volume_snapshot(symbol, expiry)
    saved = _saved_volume_snapshot(symbol, expiry)
    live_pcv = round(live["put_volume"] / max(live["call_volume"], 1), 3) if live.get("available") else None
    return {
        "symbol": symbol, "expiry": expiry, "dte": dte, "spot": spot,
        "spot_source": spot_snapshot.get("source") or "technical fallback",
        "regime": regime, "score": score, "confidence": confidence,
        "net_gex": _number(gex.get("total_gex"), 0), "gamma_flip": _number(gex.get("gamma_flip")),
        "pin": _number(gex.get("pin_strike")), "max_pain": _number(gex.get("max_pain")), "put_wall": put_wall, "call_wall": call_wall,
        "location": _overview_location(spot, _number(gex.get("gamma_flip")), put_wall, call_wall),
        "gamma_by_strike": gamma_by_strike,
        "live_volume": live, "saved_volume": saved, "live_pcv": live_pcv,
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


_MARKET_OVERVIEW_TEMPLATE = """<!doctype html>
<title>GEX Market Overview</title>
<style>
body{background:#0b1120;color:#e5e7eb;font:14px system-ui;margin:24px}.top{display:flex;gap:16px;align-items:center;flex-wrap:wrap}.controls{display:flex;gap:6px}.controls button{background:#1f2937}.controls button.active{background:#2563eb}.grid{display:grid;grid-template-columns:repeat(3,minmax(320px,1fr));gap:16px;margin-top:18px}.card{background:#111827;border:1px solid #263349;border-radius:10px;padding:16px}.good{color:#60a5fa}.bad{color:#f87171}.muted{color:#9ca3af}.metric{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-bottom:1px solid #1f2937}button{background:#2563eb;color:white;border:0;border-radius:6px;padding:9px 14px;cursor:pointer}.gamma{width:100%;height:260px;margin-top:14px;background:#0b1018;border-radius:7px}.axis{stroke:#334155;stroke-width:1}.spot{stroke:#60a5fa;stroke-width:2;stroke-dasharray:4 3}.label{fill:#94a3b8;font-size:10px}.chart-title{fill:#e5e7eb;font-size:12px;font-weight:600}
</style>
<div class=top><h2>GEX Market Overview</h2><button id=refresh>Refresh live view</button><label class=muted>Auto refresh <select id=interval><option value=0>Off</option><option value=60>1 minute</option><option value=300>5 minutes</option><option value=900>15 minutes</option></select></label><div class=controls><button data-mode=net class=active>Net gamma</button><button data-mode=absolute>Absolute gamma</button><button data-mode=split>Put / call gamma</button></div><span class=muted id=status>Saved GEX/OI + live spot and option volume</span></div><div id=grid class=grid></div>
<script>
var overviewRows=[], mode='net';
var n=function(v){return v==null?'—':typeof v==='number'?v.toLocaleString(undefined,{maximumFractionDigits:2}):v};
function row(k,v,c){return '<div class="metric '+(c||'')+'"><span>'+k+'</span><b>'+v+'</b></div>'}
function gammaChart(x){
  var data=x.gamma_by_strike||[]; if(!data.length)return '<p class=muted>Saved Greeks are unavailable for the gamma chart.</p>';
  var w=520,h=260,left=42,right=12,top=28,bottom=32,iw=w-left-right,ih=h-top-bottom;
  var values=[]; data.forEach(function(d){if(mode==='net')values.push(d.call+d.put);else if(mode==='absolute')values.push(Math.abs(d.call)+Math.abs(d.put));else{values.push(d.call);values.push(d.put)}});
  var max=Math.max.apply(null,values.map(Math.abs))||1, min=mode==='absolute'?0:-max, maxY=mode==='absolute'?max:max;
  var y=function(v){return top+(maxY-v)/(maxY-min)*ih}, zero=y(0), step=iw/data.length, bars='';
  data.forEach(function(d,i){var cx=left+step*i+step/2, bw=Math.max(2,step*.66);function bar(v,fill,offset){var yy=y(v),base=mode==='absolute'?zero:zero,ht=Math.abs(base-yy);return '<rect x="'+(cx-bw/2+(offset||0))+'" y="'+Math.min(base,yy)+'" width="'+(mode==='split'?bw/2-1:bw)+'" height="'+ht+'" fill="'+fill+'"><title>'+x.symbol+' '+d.strike+' gamma: '+n(v)+'</title></rect>'}if(mode==='net'){var net=d.call+d.put;bars+=bar(net,net>=0?'#5790e8':'#f0646b',0)}else if(mode==='absolute'){bars+=bar(Math.abs(d.call)+Math.abs(d.put),'#5790e8',0)}else{bars+=bar(d.call,'#5790e8',-bw/4);bars+=bar(d.put,'#f0646b',bw/4)}}); 
  var first=data[0].strike,last=data[data.length-1].strike,spotX=left+(x.spot-first)/(last-first||1)*iw; spotX=Math.max(left,Math.min(left+iw,spotX));
  var labels='<text x="'+left+'" y="14" class="chart-title">'+(mode==='net'?'Net gamma exposure':mode==='absolute'?'Absolute gamma exposure':'Call vs put gamma exposure')+'</text><text x="'+left+'" y="'+(h-8)+'" class="label">'+n(first)+'</text><text x="'+(left+iw-28)+'" y="'+(h-8)+'" class="label">'+n(last)+'</text><text x="'+(spotX+3)+'" y="'+(top+10)+'" class="label">Spot '+n(x.spot)+'</text>';
  return '<svg class=gamma viewBox="0 0 '+w+' '+h+'" role="img" aria-label="'+x.symbol+' gamma by strike"><line x1="'+left+'" y1="'+zero+'" x2="'+(left+iw)+'" y2="'+zero+'" class="axis" /><line x1="'+spotX+'" y1="'+top+'" x2="'+spotX+'" y2="'+(top+ih)+'" class="spot" />'+bars+labels+'</svg>';
}
function card(x){var l=x.live_volume||{},s=x.saved_volume||{},dc=(l.call_volume||0)-(s.call_volume||0),dp=(l.put_volume||0)-(s.put_volume||0),klass=(x.regime||'').toLowerCase().includes('positive')?'good':'bad';return '<section class=card><h2>'+x.symbol+' <small class=muted>'+x.expiry+' • '+x.dte+' DTE</small></h2>'+row('Regime',x.regime,klass)+row('Live spot',n(x.spot))+row('Price location',x.location)+row('Net GEX',n(x.net_gex))+row('Gamma flip',n(x.gamma_flip))+row('Balance pin',n(x.pin))+row('Max pain',n(x.max_pain))+row('Put / call wall',n(x.put_wall)+' / '+n(x.call_wall))+row('Live call / put volume',n(l.call_volume)+' / '+n(l.put_volume))+row('Live put/call volume',n(x.live_pcv))+row('Volume vs saved','C '+(dc>=0?'+':'')+n(dc)+' • P '+(dp>=0?'+':'')+n(dp))+'<p class=muted>Saved volume: '+(s.date||'unavailable')+'</p><p><b>Read:</b> '+x.trade_read+'</p>'+gammaChart(x)+'</section>'}
function draw(){grid.innerHTML=overviewRows.map(card).join('')||'<p>No saved GEX data is available.</p>'}
document.querySelectorAll('[data-mode]').forEach(function(button){button.onclick=function(){mode=button.dataset.mode;document.querySelectorAll('[data-mode]').forEach(function(b){b.classList.toggle('active',b===button)});draw()}});
async function load(){status.textContent='Loading saved GEX/OI and live volume…';try{var r=await fetch('/gex/market-overview?format=json',{cache:'no-store'}),d=await r.json();overviewRows=d.results||[];draw();status.textContent='Updated '+d.updated+(d.errors&&d.errors.length?' • '+d.errors.map(function(e){return e.symbol}).join(', ')+' unavailable':'')}catch(e){status.textContent='Could not load overview: '+e.message}}
var refreshTimer=null;
function setRefreshInterval(){if(refreshTimer){clearInterval(refreshTimer);refreshTimer=null}var seconds=Number(interval.value||0);if(seconds){refreshTimer=setInterval(load,seconds*1000);status.textContent='Auto refresh every '+seconds/60+' minute'+(seconds===60?'':'s')}}
interval.onchange=setRefreshInterval;
window.addEventListener('pagehide',function(){if(refreshTimer)clearInterval(refreshTimer)});
refresh.onclick=load;load();
</script>""";

