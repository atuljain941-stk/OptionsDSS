from flask import Blueprint, jsonify, request
from ..services.market import (
    sanitize_symbol,
    get_spot, get_expirations, get_history,
    get_live_strikes_and_volume,
    get_oi_map_fromDB, fetch_store_for,
    select_strikes_around_atm,
)
from ..db import (
    get_symbols, save_symbols, delete_symbol,
    get_expirations_for_symbol,get_oi_fromdb,get_expirationOI_date,get_two_latest_dates
)
from ..services.aggregate import get_aggregate_strike, get_pcr_snapshot
from ..services.scheduler import get_scheduler_status, trigger_fetch_all, stop_scheduler
from ..services.sqlview import get_table_view

api_bp = Blueprint("api", __name__, url_prefix="/api")

# In-memory status for manual Futures OI fetches.
# This prevents the UI from mistaking old rows for the result of the current fetch.
import threading as _futures_threading
import time as _futures_time
_FUTURES_FETCH_LOCK = _futures_threading.Lock()
_FUTURES_FETCH_JOBS = {}


def _futures_job_update(job_id, **updates):
    if not job_id:
        return
    with _FUTURES_FETCH_LOCK:
        job = _FUTURES_FETCH_JOBS.setdefault(job_id, {"job_id": job_id})
        job.update(updates)
        # Keep only recent jobs so the dict never grows indefinitely.
        if len(_FUTURES_FETCH_JOBS) > 20:
            oldest = sorted(_FUTURES_FETCH_JOBS.items(), key=lambda kv: kv[1].get("started_ts", 0))[:-20]
            for old_id, _old in oldest:
                _FUTURES_FETCH_JOBS.pop(old_id, None)


def _futures_job_get(job_id):
    if not job_id:
        return None
    with _FUTURES_FETCH_LOCK:
        job = _FUTURES_FETCH_JOBS.get(job_id)
        return dict(job) if job else None


def _futures_active_contracts(symbol=None):
    """Return currently relevant Schwab contracts for status checks."""
    try:
        from ..services.futures_oi_schwab import SCHWAB_ROOTS, _get_quarterly_contracts
        if symbol:
            syms = [symbol.upper()]
        else:
            syms = list(SCHWAB_ROOTS.keys())
        active = set()
        for s in syms:
            root = SCHWAB_ROOTS.get(s.upper())
            if not root:
                continue
            for c in _get_quarterly_contracts(root, 3):
                active.add(c.get("symbol"))
        return {c for c in active if c}
    except Exception:
        return set()



def _wl_key(base: str, watchlist_id=None) -> str:
    return f"{base}_{int(watchlist_id)}" if watchlist_id not in (None, "", 0) else base


def _get_watchlist_symbols(watchlist_id):
    if not watchlist_id:
        return None
    try:
        from ..db import _connect
        con = _connect()
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (int(watchlist_id),)
        ).fetchall()
        con.close()
        return [r[0] for r in rows] if rows else []
    except Exception:
        return None
@api_bp.route("/spot")
def api_spot():
    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    return jsonify({"symbol": t, "spot": get_spot(t)})

@api_bp.route("/expirations")
def api_expirations():
    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    return jsonify({"symbol": t, "expirations": get_expirations(t)})

@api_bp.route("/history")
def api_history():
    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    period = request.args.get("period", "90d")
    return jsonify({"symbol": t, "prices": get_history(t, period)})

@api_bp.route("/options")
def api_options():
    import math as _m
    def _i(v):
        try:
            f=float(v or 0)
            return 0 if (_m.isnan(f) or _m.isinf(f)) else int(f)
        except: return 0

    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    exp = request.args.get("expiration")
    if not exp:
        return jsonify({"error": "missing expiration"}), 400
    per_side = _i(request.args.get("per_side", 12))

    spot = get_spot(t)

    # Get OI from DB — auto-fetch from yfinance if missing
    oi_map, oi_day = get_oi_map_fromDB(t, exp)
    rows = get_oi_fromdb(t, exp)
    if not oi_map:
        try:
            fetch_store_for(t, expirations=[exp], per_side=per_side)
            oi_map, oi_day = get_oi_map_fromDB(t, exp)
            rows = get_oi_fromdb(t, exp)
        except Exception as e:
            print("[options] auto-fetch error:", e)

    # Build price map from latest DB snapshot (if present)
    price_map = {}
    for r in rows or []:
        try:
            price_map[(r.get("type"), float(r.get("strike")))] = r.get("price")
        except Exception:
            pass

    # STRIKE LIST: use DB strikes as primary (ensures OI data exists for every bar)
    # Live yfinance used only for volume overlay — never for the strike list itself
    db_strikes = sorted({s for (_typ, s) in oi_map.keys()}) if oi_map else []

    # Volume from yfinance (best-effort, separate from strike list)
    vol_map = {}
    if db_strikes:
        try:
            _, vol_map = get_live_strikes_and_volume(t, exp)
        except: pass

    if not db_strikes:
        return jsonify({"symbol": t, "expiration": exp, "spot": spot,
                        "calls": [], "puts": [], "oi_snapshot_day": None,
                        "message": "No OI data in DB — run Scheduler to fetch"})

    # Select per_side strikes on each side of spot from DB strikes only
    if spot:
        picked = select_strikes_around_atm(db_strikes, spot, per_side)
    else:
        picked = db_strikes[:per_side*2]

    calls, puts = [], []
    for s in picked:
        s_f = float(s)
        calls.append({"strike": s_f,
                       "price":  price_map.get(("call", s_f)),
                       "oi":     _i(oi_map.get(("call", s_f), 0)),
                       "volume": _i(vol_map.get(("call", s_f), 0))})
        puts.append({"strike":  s_f,
                      "price":  price_map.get(("put", s_f)),
                      "oi":     _i(oi_map.get(("put", s_f), 0)),
                      "volume": _i(vol_map.get(("put", s_f), 0))})

    return jsonify({"symbol": t, "expiration": exp, "spot": spot,
                    "oi_snapshot_day": oi_day, "calls": calls, "puts": puts})


@api_bp.route("/oi_change")
def api_oi_change():
    t = (request.args.get("symbol") or "SPY").upper().strip()
    exp = request.args.get("expiration")
    if not exp:
        return jsonify({"error": "missing expiration"}), 400

    def _typ(v):
        x = str(v or "").lower().strip()
        if x.startswith("c"):
            return "call"
        if x.startswith("p"):
            return "put"
        return x

    def _strike(v):
        try:
            return round(float(v), 8)
        except Exception:
            return str(v or "").strip()

    def _num(v, default=0):
        try:
            if v is None:
                return default
            return int(float(str(v).replace(",", "").strip() or 0))
        except Exception:
            return default

    def _rows_for(dt):
        return get_expirationOI_date(t, exp, dt) if dt else []

    def _dict(rows):
        out = {}
        for r in rows or []:
            key = (_typ(r.get("type")), _strike(r.get("strike")))
            if key[0] in ("call", "put"):
                out[key] = r
        return out

    def _calc(latest_rows, prev_rows):
        latest_dict = _dict(latest_rows)
        prev_dict = _dict(prev_rows)
        keys = sorted(set(latest_dict) | set(prev_dict), key=lambda k: (k[0], float(k[1]) if isinstance(k[1], (int, float)) else 0))
        changes = []
        for key in keys:
            typ, strike = key
            if typ not in ("call", "put"):
                continue
            new_row = latest_dict.get(key) or {}
            old_row = prev_dict.get(key) or {}
            new_oi = _num(new_row.get("oi"))
            new_vol = _num(new_row.get("volume"))
            old_oi = _num(old_row.get("oi"))
            old_vol = _num(old_row.get("volume"))
            oi_change = new_oi - old_oi
            vol_change = new_vol - old_vol
            change_pct = round(oi_change / old_oi * 100, 2) if old_oi else None
            changes.append({
                "type": typ,
                "strike": float(strike) if isinstance(strike, (int, float)) else strike,
                "oi_change": oi_change,
                "oi_change_pct": change_pct,
                "vol_change": vol_change,
                "latest_oi": new_oi,
                "prev_oi": old_oi,
                "is_new_strike": key not in prev_dict,
                "is_removed_strike": key not in latest_dict,
            })
        return changes

    try:
        from ..db import _connect
        con = _connect()
        try:
            date_rows = con.execute(
                """SELECT DISTINCT date FROM options
                   WHERE UPPER(symbol)=? AND expiration=?
                   ORDER BY date DESC LIMIT 12""",
                (t, exp)
            ).fetchall()
            dates = [r[0] for r in date_rows if r and r[0]]
        finally:
            con.close()

        if not dates:
            return jsonify({
                "symbol": t,
                "expiration": exp,
                "error": "No OI snapshots found for this symbol/expiration",
                "call_change": [],
                "put_change": [],
                "change": [],
            })

        latest_date = dates[0]
        latest_rows = _rows_for(latest_date)
        prev_date = dates[1] if len(dates) > 1 else None
        changes = _calc(latest_rows, _rows_for(prev_date)) if prev_date else _calc(latest_rows, [])

        # Some data sources can produce an identical next-day snapshot or a same
        # stale snapshot after a refetch.  If the immediate comparison is all
        # zero, search a few older snapshots and compare against the most recent
        # date that actually differs.  This restores the dashboard ΔOI chart
        # without fabricating data; the response tells the UI which comparison
        # date was used.
        comparison_note = ""
        skipped_identical_dates = []
        original_prev_date = prev_date
        if prev_date and not any(c.get("oi_change") for c in changes):
            skipped_identical_dates.append(prev_date)
            for older in dates[2:]:
                candidate = _calc(latest_rows, _rows_for(older))
                if any(c.get("oi_change") for c in candidate):
                    prev_date = older
                    changes = candidate
                    comparison_note = (
                        f"Skipped unchanged snapshot(s) {', '.join(skipped_identical_dates)}; "
                        f"showing latest non-zero OI comparison."
                    )
                    break
                skipped_identical_dates.append(older)

        call_change = [c for c in changes if c["type"] == "call"]
        put_change = [c for c in changes if c["type"] == "put"]
        call_change.sort(key=lambda x: abs(x["oi_change"]), reverse=True)
        put_change.sort(key=lambda x: abs(x["oi_change"]), reverse=True)
        return jsonify({
            "symbol": t,
            "expiration": exp,
            "latest_date": latest_date,
            "prev_date": prev_date,
            "has_prior": bool(prev_date),
            "comparison_note": comparison_note,
            "skipped_identical_dates": skipped_identical_dates,
            "immediate_prev_date": original_prev_date,
            "comparison_label": (f"Latest {latest_date} vs {prev_date}" if prev_date else f"Latest {latest_date}"),
            "call_change": call_change,
            "put_change": put_change,
            "change": changes,
        })
    except Exception as e:
        return jsonify({
            "symbol": t,
            "expiration": exp,
            "error": str(e),
            "call_change": [],
            "put_change": [],
            "change": [],
        })

@api_bp.route("/aggregate_strike")
def api_aggregate_strike():
    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    from_exp = request.args.get("from_expiration")
    count = int(request.args.get("count", 3))
    per_side = int(request.args.get("strikes", 10))
    return jsonify(get_aggregate_strike(t, from_exp, count, per_side))

@api_bp.route("/pcr_snapshot")
def api_pcr_snapshot():
    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    return jsonify({"symbol": t, "pcr_data": get_pcr_snapshot(t)})

@api_bp.route("/symbols", methods=["GET", "POST", "DELETE"])
def api_symbols():
    if request.method == "POST":
        syms = [sanitize_symbol(s) for s in (request.json or {}).get("symbols", []) if s]
        save_symbols(syms)
        return jsonify({"saved": syms})
    if request.method == "DELETE":
        sym = sanitize_symbol((request.json or {}).get("symbol", ""))
        if sym:
            delete_symbol(sym)
        return jsonify({"deleted": sym})
    return jsonify({"symbols": get_symbols()})

@api_bp.route("/fetch_status")
def api_fetch_status():
    import sqlite3
    from pathlib import Path as _P
    base = get_scheduler_status()
    # Enrich with last scan timestamps from app_config
    try:
        _db = str(_P(__file__).resolve().parents[2] / "options_data.db")
        _c = sqlite3.connect(_db)
        _c.execute("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)")
        for _k in ["regime_scan_completed_at", "oib_completed_at",
                    "post_earnings_completed_at", "pre_earnings_completed_at"]:
            _r = _c.execute("SELECT value FROM app_config WHERE key=?", (_k,)).fetchone()
            if _r: base[_k] = _r[0]
        _c.close()
    except: pass
    return jsonify(base)

@api_bp.route("/fetch_now", methods=["POST"])
def api_fetch_now():
    wl_id = request.args.get("watchlist_id", None, type=int)
    return jsonify(trigger_fetch_all(watchlist_id=wl_id))

@api_bp.route("/fetch_stop", methods=["POST"])
def api_fetch_stop():
    return jsonify(stop_scheduler())

@api_bp.route("/sql_view")
def api_sql_view():
    table = request.args.get("table", "options")
    limit = int(request.args.get("limit", 100))
    symbol = request.args.get("symbol")
    exp = request.args.get("expiration")
    return jsonify(get_table_view(table, limit, symbol, exp))

@api_bp.route("/db_expirations")
def api_db_expirations():
    from datetime import date as _date
    sym = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    future_only = request.args.get("future", "false").lower() == "true"
    exps = get_expirations_for_symbol(sym)
    if future_only:
        today = _date.today().strftime("%Y-%m-%d")
        exps = [e for e in exps if e >= today]
    return jsonify({"symbol": sym, "expirations": exps})

@api_bp.route("/scanner")
def api_scanner():
    limit = int(request.args.get("limit", 10))
    results = [{"symbol": s, "delta_oi": 0, "delta_vol": 0, "signal": "Neutral"} for s in get_symbols()][:limit]
    return jsonify({"results": results})


# ── Bootstrap endpoint — returns everything the UI needs on startup in ONE call ──
@api_bp.route("/bootstrap")
def api_bootstrap():
    """
    Returns symbols list + DB expirations for the default symbol (SPY)
    in a single fast DB-only call. No yfinance network request.
    Called once on page load so all tabs populate instantly.
    """
    sym = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    symbols    = get_symbols()                   # pure DB read
    from datetime import date as _d
    today = _d.today().strftime("%Y-%m-%d")
    db_exps = [e for e in get_expirations_for_symbol(sym) if e >= today]
    return jsonify({
        "symbols":     symbols,
        "expirations": db_exps,
        "symbol":      sym,
        "today":       today,
    })


@api_bp.route("/oi_intelligence")
def api_oi_intelligence():
    """
    OI Direction Intelligence — one directional verdict per symbol+expiry.
    Applies 5 disambiguation signals to ALL significant OI changes, synthesises:
      BULLISH / MILDLY BULLISH / SIDEWAYS / MILDLY BEARISH / BEARISH
    Also returns top OI walls (support/resistance) closest to spot.
    """
    import math as _m
    from datetime import date as _d, datetime as _dt

    def _sf(v, dec=2):
        try:
            f=float(v); return None if(_m.isnan(f) or _m.isinf(f)) else round(f,dec)
        except: return None

    symbol = (request.args.get("symbol") or "SPY").upper()
    expiry = (request.args.get("expiration") or "").strip()
    if not expiry:
        return jsonify({"error":"expiration required"}),400

    latest_date, prev_date = get_two_latest_dates(symbol, expiry)
    if not latest_date or not prev_date:
        return jsonify({"symbol":symbol,"expiry":expiry,
            "error":"Need ≥2 days of OI data. Run Scheduler to populate.",
            "direction":"UNKNOWN","walls":[]})

    latest_rows = get_expirationOI_date(symbol, expiry, latest_date)
    prev_rows   = get_expirationOI_date(symbol, expiry, prev_date)
    latest_dict = {(r["type"],float(r["strike"])):r for r in latest_rows}
    prev_dict   = {(r["type"],float(r["strike"])):r for r in prev_rows}
    spot = get_spot(symbol) or 0

    # ── Price change on snapshot date ─────────────────────────────────────────
    price_chg_pct = vol_ratio = None
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="30d",interval="1d")
        if not df.empty:
            df.index = df.index.strftime("%Y-%m-%d")
            dates = list(df.index)
            if latest_date in dates:
                idx = dates.index(latest_date)
                if idx>0:
                    c1=float(df.iloc[idx]["Close"]); c0=float(df.iloc[idx-1]["Close"])
                    price_chg_pct = round((c1-c0)/c0*100,2) if c0 else None
                avg_vol=float(df["Volume"].mean()); day_vol=float(df.loc[latest_date]["Volume"])
                vol_ratio=round(day_vol/avg_vol,2) if avg_vol else None
    except Exception as e:
        print(f"[oi_intel] {symbol}: {e}")

    price_up   = price_chg_pct is not None and price_chg_pct >  0.3
    price_down = price_chg_pct is not None and price_chg_pct < -0.3

    # ── OI walls ──────────────────────────────────────────────────────────────
    all_oi = {}
    for (t,s),row in latest_dict.items():
        prev = prev_dict.get((t,s))
        oi_now  = int(row.get("oi") or 0)
        oi_prev = int(prev.get("oi") or 0) if prev else oi_now
        if oi_now < 500: continue
        all_oi[(t,s)] = {"oi":oi_now,"delta":oi_now-oi_prev}

    def wall_score(t,s,d):
        dist = abs(s-spot)/spot*100 if spot else 0
        return d["oi"] * max(0,1-dist/15) * (1.2 if d["delta"]>500 else 0.8 if d["delta"]<-500 else 1.0)

    top_puts  = sorted([(k,v) for k,v in all_oi.items() if k[0]=="put"  and k[1]<spot],
                       key=lambda x:-wall_score(*x[0],x[1]))[:4]
    top_calls = sorted([(k,v) for k,v in all_oi.items() if k[0]=="call" and k[1]>spot],
                       key=lambda x:-wall_score(*x[0],x[1]))[:4]

    walls=[]
    for (t,s),d in top_puts:
        dist=round((s-spot)/spot*100,1) if spot else 0
        walls.append({"strike":s,"type":"put","side":"support","oi":d["oi"],"delta":d["delta"],
            "pct_from_spot":dist,"strength":"STRONG" if d["oi"]>3000 else "MODERATE" if d["oi"]>1500 else "MILD",
            "growing":d["delta"]>200})
    for (t,s),d in top_calls:
        dist=round((s-spot)/spot*100,1) if spot else 0
        walls.append({"strike":s,"type":"call","side":"resistance","oi":d["oi"],"delta":d["delta"],
            "pct_from_spot":dist,"strength":"STRONG" if d["oi"]>3000 else "MODERATE" if d["oi"]>1500 else "MILD",
            "growing":d["delta"]>200})
    walls.sort(key=lambda x:abs(x["pct_from_spot"]))

    # ── 5-signal directional scoring ─────────────────────────────────────────
    bull_pts=0; bear_pts=0; evidence=[]
    MIN_DELTA=300

    changes=[]
    for (t,s),row in latest_dict.items():
        prev=prev_dict.get((t,s))
        if not prev: continue
        delta=int(row.get("oi") or 0)-int(prev.get("oi") or 0)
        if abs(delta)<MIN_DELTA: continue
        changes.append({"type":t,"strike":s,"delta":delta,
            "oi_new":int(row.get("oi") or 0),"vol_new":int(row.get("volume") or 0),
            "itm":(t=="put" and s>spot) or (t=="call" and s<spot),
            "otm":(t=="put" and s<spot) or (t=="call" and s>spot),
            "closing":delta<0})

    for c in changes:
        t,s,delta=c["type"],c["strike"],c["delta"]
        closing=c["closing"]; itm=c["itm"]; otm=c["otm"]
        w=min(3,max(1,abs(delta)//500))

        # Signal 1: Price action
        if price_chg_pct is not None:
            if   t=="put"  and closing and price_up:   bull_pts+=w*2; evidence.append(f"${s}P closed −{abs(delta):,} · price +{price_chg_pct}% → put buyer exiting (bullish)")
            elif t=="put"  and closing and price_down: bear_pts+=w*2; evidence.append(f"${s}P closed −{abs(delta):,} · price {price_chg_pct}% → put seller nervous (bearish)")
            elif t=="put"  and not closing and price_up:  bull_pts+=w; evidence.append(f"${s}P added +{delta:,} · price up → put selling for income (bullish)")
            elif t=="put"  and not closing and price_down: bear_pts+=w*2; evidence.append(f"${s}P added +{delta:,} · price down → fresh put hedging (bearish)")
            elif t=="call" and closing and price_down: bear_pts+=w*2; evidence.append(f"${s}C closed −{abs(delta):,} · price {price_chg_pct}% → call buyer exiting (bearish)")
            elif t=="call" and not closing and price_up:  bull_pts+=w*2; evidence.append(f"${s}C added +{delta:,} · price up → call buying (bullish)")
            elif t=="call" and not closing and price_down: bear_pts+=w; evidence.append(f"${s}C added +{delta:,} · price down → defensive call (mixed)")

        # Signal 3: Moneyness (Signal 2 vol just adjusts weight, already in w)
        if   closing and t=="put" and itm:  bear_pts+=w; evidence.append(f"${s}P ITM closed → put seller cutting loss (bearish)")
        elif closing and t=="put" and otm:  bull_pts+=w; evidence.append(f"${s}P OTM closed → put buyer exiting bearish bet")
        elif not closing and t=="put" and itm: bear_pts+=w*2; evidence.append(f"${s}P ITM added → aggressive downside hedge (bearish)")
        elif not closing and t=="call" and otm: bull_pts+=w; evidence.append(f"${s}C OTM added → bullish call positioning")

        # Signal 4: IV proxy (high vol/OI = event risk, note only)
        vol_oi=round(c["vol_new"]/max(abs(delta),1),1)
        if c["vol_new"]>abs(delta)*3:
            evidence.append(f"${s}{t[0].upper()} vol/ΔOI={vol_oi}× — IV expanding, uncertainty elevated")

        # Signal 5: Roll detection (cancel and re-score)
        for c2 in changes:
            if c2["type"]!=t or c2["strike"]==s: continue
            if abs(c2["strike"]-s)>max(5,abs(s)*0.03): continue
            if (c2["delta"]>0)==(delta>0): continue
            ratio=min(abs(delta),abs(c2["delta"]))/max(abs(delta),abs(c2["delta"]))
            if ratio>0.4:
                bull_pts=max(0,bull_pts-w); bear_pts=max(0,bear_pts-w)
                if t=="put":
                    if c2["strike"]<s: bull_pts+=1; evidence.append(f"${s}P → ${c2['strike']}P roll DOWN — reducing hedge (mildly bullish)")
                    else: bear_pts+=1; evidence.append(f"${s}P → ${c2['strike']}P roll UP — increasing hedge (mildly bearish)")
                break

    # PCR tie-breaker
    total_put_oi  = sum(d["oi"] for (t,s),d in all_oi.items() if t=="put")
    total_call_oi = sum(d["oi"] for (t,s),d in all_oi.items() if t=="call")
    pcr = round(total_put_oi/total_call_oi,3) if total_call_oi else None
    if pcr:
        if   pcr>1.5: bear_pts+=1; evidence.append(f"PCR={pcr} — heavy put loading (bearish lean)")
        elif pcr>1.1: bear_pts+=1
        elif pcr<0.6: bull_pts+=1; evidence.append(f"PCR={pcr} — low hedging, complacent (bullish lean)")
        elif pcr<0.8: bull_pts+=1

    # ── Verdict ───────────────────────────────────────────────────────────────
    net=bull_pts-bear_pts; total_ev=bull_pts+bear_pts
    if total_ev==0:
        direction="SIDEWAYS"; confidence="LOW"
    elif net>=6:   direction="BULLISH";        confidence="HIGH"
    elif net>=3:   direction="MILDLY BULLISH"; confidence="MODERATE"
    elif net>=-2:  direction="SIDEWAYS";       confidence="MODERATE" if total_ev>3 else "LOW"
    elif net>=-5:  direction="MILDLY BEARISH"; confidence="MODERATE"
    else:          direction="BEARISH";        confidence="HIGH"

    dir_color={"BULLISH":"#22c55e","MILDLY BULLISH":"#4ade80",
               "SIDEWAYS":"#f59e0b","MILDLY BEARISH":"#f87171","BEARISH":"#ef4444"}.get(direction,"#64748b")

    supports=[w for w in walls if w["side"]=="support"]
    resistances=[w for w in walls if w["side"]=="resistance"]
    nearest_sup=supports[0]["strike"] if supports else None
    nearest_res=resistances[0]["strike"] if resistances else None

    try: days=(_dt.strptime(expiry,"%Y-%m-%d").date()-_d.today()).days
    except: days=0

    summary_parts=[
        f"OI flow into {expiry} ({days}d) leans {'bullish' if 'BULL' in direction else 'bearish' if 'BEAR' in direction else 'neutral'} "
        f"({confidence.lower()} confidence · bull:{bull_pts} bear:{bear_pts})."]
    if nearest_sup and nearest_res:
        summary_parts.append(f"Range: ${nearest_sup} support → ${nearest_res} resistance.")
    if price_chg_pct is not None:
        summary_parts.append(f"Stock moved {'+' if price_chg_pct>=0 else ''}{price_chg_pct}% on OI snapshot date.")
    if pcr: summary_parts.append(f"PCR: {pcr}.")

    return jsonify({
        "symbol":symbol,"expiry":expiry,"spot":spot,
        "latest_date":latest_date,"prev_date":prev_date,
        "price_chg_pct":price_chg_pct,"vol_ratio":vol_ratio,"pcr":pcr,
        "bull_pts":bull_pts,"bear_pts":bear_pts,"net_score":net,
        "direction":direction,"confidence":confidence,"direction_color":dir_color,
        "summary":" ".join(summary_parts),
        "evidence":evidence[:8],
        "walls":walls,
        "days_to_expiry":days,
        "total_put_oi":total_put_oi,"total_call_oi":total_call_oi,
    })


# ═══════════════════════════════════════════════════════════════════
# FUTURES OI (Schwab real daily OI only)
# ═══════════════════════════════════════════════════════════════════
@api_bp.route("/futures/fetch")
def fetch_futures():
    """Legacy GET endpoint kept for older UI calls.

    IMPORTANT: this no longer calls the yfinance/volume-proxy futures fetcher.
    Futures OI in the dashboard/aggregate tab should come from Schwab real OI
    snapshots only. Use /api/futures/fetch_now for the background job UI.
    """
    sym = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    try:
        from ..services.futures_oi_schwab import fetch_futures_oi_schwab
        return jsonify(fetch_futures_oi_schwab(sym))
    except Exception as e:
        return jsonify({"ok": False, "symbol": sym, "source": "schwab", "error": str(e)})

@api_bp.route("/futures/contracts")
def futures_contracts():
    """Get active quarterly Schwab futures contracts for a dashboard symbol."""
    sym = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    try:
        from ..services.futures_oi_schwab import SCHWAB_ROOTS, _get_quarterly_contracts
        root = SCHWAB_ROOTS.get(sym.upper(), "/ES")
        contracts = _get_quarterly_contracts(root, count=3)
        return jsonify({"symbol": sym, "contracts": contracts, "source": "schwab"})
    except Exception as e:
        return jsonify({"symbol": sym, "contracts": [], "source": "schwab", "error": str(e)})

@api_bp.route("/futures/data")
def get_futures_data_route():
    """Get stored Schwab real OI series for one futures contract."""
    sym      = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    contract = (request.args.get("contract", "") or "").strip().upper()
    days     = int(request.args.get("days", "30") or 30)
    try:
        from ..services.futures_oi_schwab import get_latest_oi
        d = get_latest_oi(sym, days=days)
        contracts = d.get("contracts") or []
        if not contract and contracts:
            contract = contracts[0]
        if contract and not contract.startswith("/"):
            contract = "/" + contract
        rows = (d.get("oi_series") or {}).get(contract, []) if contract else []
        data = [{
            "date": r.get("date"),
            "open": r.get("close", 0),
            "high": r.get("close", 0),
            "low": r.get("close", 0),
            "close": r.get("close", 0),
            "volume": r.get("volume", 0),
            "oi": r.get("oi", 0),
            "oi_change": r.get("oi_change", 0),
        } for r in rows]
        return jsonify({"contract": contract, "data": data, "source": "schwab"})
    except Exception as e:
        return jsonify({"contract": contract or None, "data": [], "source": "schwab", "error": str(e)})

@api_bp.route("/futures/all_contracts")
def futures_all_contracts():
    """Return the exact same Schwab futures OI data used by the Dashboard.

    The Aggregate tab used to call this endpoint while the Dashboard used
    /futures/chart_data. If one path normalized symbols or filtered contracts
    differently, the Dashboard could show Schwab OI while Aggregate said no
    data. Keep this endpoint as a compatibility wrapper around the unified
    Schwab-only chart builder so both pages render from one source of truth.
    """
    raw = request.args.get("symbol", "SPY") or "SPY"
    sym = sanitize_symbol(raw) or "SPY"
    # Be forgiving if a UI control ever passes labels such as "SPY /ES" or
    # just a futures root such as "/ES". The storage key is the ETF proxy.
    try:
        from ..services.futures_oi_schwab import SCHWAB_ROOTS
        root_to_proxy = {v.replace("/", "").upper(): k for k, v in SCHWAB_ROOTS.items()}
        tokens = [t.strip().upper() for t in str(raw).replace("/", " /").replace("-", " ").split() if t.strip()]
        for tok in tokens:
            clean = tok.replace("/", "")
            if clean in root_to_proxy:
                sym = root_to_proxy[clean]
                break
            if clean in SCHWAB_ROOTS:
                sym = clean
                break
    except Exception:
        pass

    try:
        d = _unified_futures_chart(sym)
        series = d.get("oi_series") or {}
        contracts = []
        for ct in d.get("contracts") or []:
            rows = series.get(ct, []) or []
            contracts.append({
                "contract": ct,
                "source": "schwab",
                "data": [{
                    "date": r.get("date"),
                    "close": r.get("close", 0),
                    "volume": r.get("volume", 0),
                    "oi": r.get("oi", 0),
                    "oi_change": r.get("oi_change", 0),
                } for r in rows],
            })
        has_oi = any(any((row.get("oi") or 0) > 0 for row in c["data"]) for c in contracts)
        return jsonify({
            "symbol": sym,
            "source": d.get("source", "schwab"),
            "contracts": contracts,
            "has_oi": has_oi,
            "signal": d.get("signal"),
            "interpretation": d.get("interpretation"),
            "message": d.get("interpretation") if not has_oi else "",
        })
    except Exception as e:
        return jsonify({"symbol": sym, "contracts": [], "source": "schwab", "has_oi": False, "error": str(e)})

@api_bp.route("/futures/analysis")
def futures_analysis():
    """Futures OI snapshot for dashboard widgets.

    Keep this endpoint on the same unified reader as /futures/chart_data so the
    dashboard cannot show futures OI in one section while another section says
    no data.
    """
    raw = request.args.get("symbol", "SPY") or "SPY"
    try:
        d = _unified_futures_chart(raw)
        contracts = []
        total_oi = 0
        for r in d.get("contract_table") or []:
            oi = int(r.get("oi") or 0)
            total_oi += oi
            contracts.append({
                "contract": r.get("contract"),
                "expiry": r.get("expiry"),
                "oi": oi,
                "volume": r.get("volume", 0),
                "price": r.get("close", 0),
                "oi_change": r.get("oi_change", 0),
                "date": r.get("date", ""),
                "source": r.get("source", "schwab"),
            })
        front = next((r for r in contracts if r.get("contract") == d.get("front")), contracts[0] if contracts else {})
        return jsonify({
            "symbol": d.get("symbol", raw),
            "root": d.get("root", ""),
            "name": d.get("label") or d.get("display") or raw,
            "asset_class": d.get("asset_class", ""),
            "front": front.get("contract", d.get("front", "")),
            "active": d.get("active_contract", ""),
            "signal": d.get("signal", "NO_DATA"),
            "contracts": contracts,
            "total_oi": total_oi or d.get("total_oi", 0),
            "has_real_oi": (total_oi or d.get("total_oi", 0)) > 0,
            "is_volume_proxy": False,
            "price": {"today": front.get("price", 0), "change": d.get("price_chg", 0)},
            "oi": front.get("oi", 0),
            "oi_change": front.get("oi_change", d.get("front_oi_chg", 0)),
            "description": d.get("interpretation") or "No Schwab real futures OI rows available.",
            "source": d.get("source", "schwab"),
        })
    except Exception as e:
        return jsonify({
            "symbol": raw,
            "front": "",
            "signal": "NO_DATA",
            "contracts": [],
            "total_oi": 0,
            "description": "No Schwab real futures OI rows available. Use Scheduler -> Fetch OI Now after Schwab is connected.",
            "source": "none",
            "is_volume_proxy": False,
            "error": str(e),
        })


@api_bp.route("/futures/symbol_map")
def futures_symbol_map():
    from ..services.futures_oi import CME_PRODUCTS, get_quarterly_contracts
    sym = request.args.get("symbol","")
    if sym:
        contracts = get_quarterly_contracts(sym, 3)
        return jsonify({"symbol": sym.upper(), "contracts": contracts})
    return jsonify({k: v.get("root","") for k, v in CME_PRODUCTS.items()})
@api_bp.route("/futures_oi_multi")
def futures_oi_multi():
    """OI time series for multiple expiries (upcoming + next N)."""
    symbol = sanitize_symbol(request.args.get("symbol","SPY")) or "SPY"
    count = int(request.args.get("count","3"))
    from ..db import _connect
    con = _connect()
    
    # Get upcoming expiries
    import datetime as _dt
    today = _dt.date.today().isoformat()
    exps = [r[0] for r in con.execute(
        "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration LIMIT ?",
        (symbol, today, count)).fetchall()]
    
    result = []
    for exp in exps:
        rows = con.execute("""
            SELECT date, type, SUM(oi) as total_oi
            FROM options WHERE symbol=? AND expiration=?
            GROUP BY date, type ORDER BY date""",
            (symbol, exp)).fetchall()
        by_date = {}
        for r in rows:
            d = r[0]
            if d not in by_date:
                by_date[d] = {"date": d, "call_oi": 0, "put_oi": 0}
            if r[1] == "call": by_date[d]["call_oi"] = r[2]
            else: by_date[d]["put_oi"] = r[2]
        
        dates = sorted(by_date.keys())
        series = []
        prev_total = 0
        for d in dates:
            rec = by_date[d]
            total = rec["call_oi"] + rec["put_oi"]
            oi_chg = total - prev_total if prev_total > 0 else 0
            rec["total_oi"] = total
            rec["oi_change"] = oi_chg
            rec["pc_ratio"] = round(rec["put_oi"] / max(1, rec["call_oi"]), 3)
            series.append(rec)
            prev_total = total
        
        result.append({"expiry": exp, "series": series,
                        "latest_total": series[-1]["total_oi"] if series else 0,
                        "latest_pc": series[-1]["pc_ratio"] if series else 0})
    
    con.close()
    return jsonify({"symbol": symbol, "expiries": result})

@api_bp.route("/oi_heatmap_scanner")
def oi_heatmap_scanner():
    """OI buildup/covering signals across multiple symbols for heatmap."""
    from ..db import _connect
    import datetime as _dt
    con = _connect()
    today = _dt.date.today().isoformat()
    
    # Get all symbols with recent data
    symbols = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM options WHERE date >= date('now','-3 days')").fetchall()]
    
    results = []
    for sym in symbols:
        # Get the nearest future expiry
        exp_row = con.execute(
            "SELECT MIN(expiration) FROM options WHERE symbol=? AND expiration>=?",
            (sym, today)).fetchone()
        if not exp_row or not exp_row[0]: continue
        exp = exp_row[0]
        
        # Get last 2 dates of aggregate OI
        date_rows = con.execute(
            "SELECT DISTINCT date FROM options WHERE symbol=? AND expiration=? ORDER BY date DESC LIMIT 2",
            (sym, exp)).fetchall()
        if len(date_rows) < 2: continue
        
        d_today, d_prev = date_rows[0][0], date_rows[1][0]
        
        def get_agg(dt):
            rows = con.execute(
                "SELECT type, SUM(oi) FROM options WHERE symbol=? AND expiration=? AND date=? GROUP BY type",
                (sym, exp, dt)).fetchall()
            return {r[0]: r[1] or 0 for r in rows}
        
        today_oi = get_agg(d_today)
        prev_oi = get_agg(d_prev)
        
        t_call = today_oi.get("call", 0); t_put = today_oi.get("put", 0)
        p_call = prev_oi.get("call", 0); p_put = prev_oi.get("put", 0)
        total_today = t_call + t_put; total_prev = p_call + p_put
        oi_chg = total_today - total_prev
        oi_chg_pct = round(oi_chg / max(1, total_prev) * 100, 2)
        
        # Get price change
        try:
            import yfinance as yf
            hist = yf.Ticker(sym).history(period="5d")
            if len(hist) >= 2:
                price_chg = float(hist["Close"].iloc[-1]) - float(hist["Close"].iloc[-2])
                spot = float(hist["Close"].iloc[-1])
            else: price_chg = 0; spot = 0
        except: price_chg = 0; spot = 0
        
        # Signal
        if oi_chg > 0 and price_chg > 0: signal = "LONG_BUILDUP"
        elif oi_chg > 0 and price_chg < 0: signal = "SHORT_BUILDUP"
        elif oi_chg < 0 and price_chg < 0: signal = "LONG_UNWINDING"
        elif oi_chg < 0 and price_chg > 0: signal = "SHORT_COVERING"
        else: signal = "NEUTRAL"
        
        results.append({
            "symbol": sym, "expiry": exp, "spot": round(spot, 2),
            "call_oi": t_call, "put_oi": t_put, "total_oi": total_today,
            "oi_change": oi_chg, "oi_change_pct": oi_chg_pct,
            "pc_ratio": round(t_put / max(1, t_call), 3),
            "price_change": round(price_chg, 2),
            "signal": signal,
        })
    
    con.close()
    results.sort(key=lambda x: abs(x["oi_change"]), reverse=True)
    return jsonify({"results": results, "count": len(results)})


@api_bp.route("/oi_buildup_screener")
def oi_buildup_screener():
    """OI buildup screener — ST/MT/LT OI/vol changes, bias. No yfinance; all from DB."""
    import traceback as _tb, datetime as _dt, json as _json
    try:
        from ..db import _connect
        st_days = max(1,  min(10, request.args.get("st", 1,  type=int)))
        mt_days = max(2,  min(60, request.args.get("mt", 5,  type=int)))
        lt_days = max(5,  min(90, request.args.get("lt", 30, type=int)))
        watchlist_id = request.args.get("watchlist_id", None, type=int)
        watchlist_syms = _get_watchlist_symbols(watchlist_id)
        max_rows = max(lt_days + 2, 32)
        today    = _dt.date.today().isoformat()
        _errors  = []

        con = _connect()

        # ── Query 1: nearest expiry per symbol ──────────────────────────────
        sym_exp = dict(con.execute(
            "SELECT symbol, MIN(expiration) FROM options WHERE expiration>=? GROUP BY symbol",
            (today,)
        ).fetchall())
        if watchlist_syms is not None:
            wl_set = set(watchlist_syms)
            sym_exp = {s:e for s,e in sym_exp.items() if s in wl_set}
            # If the selected watchlist has symbols but no option rows yet, warm the DB
            # so the screener behaves like the other watchlist-driven tabs.
            if not sym_exp and wl_set:
                try:
                    from ..services.market import fetch_store_for
                    from concurrent.futures import ThreadPoolExecutor
                    with ThreadPoolExecutor(max_workers=6) as ex:
                        list(ex.map(lambda s: fetch_store_for(s), list(wl_set)[:40]))
                    sym_exp = dict(con.execute(
                        "SELECT symbol, MIN(expiration) FROM options WHERE expiration>=? GROUP BY symbol",
                        (today,)
                    ).fetchall())
                    sym_exp = {s:e for s,e in sym_exp.items() if s in wl_set}
                except Exception as _warm_e:
                    _errors.append(f"watchlist warmup: {_warm_e}")

        # ── Query 2: bulk OI history — ALL non-expired options per symbol ──
        # Using all expirations (not just nearest) gives full historical depth
        all_syms_list = list(sym_exp.keys())
        if not all_syms_list:
            return jsonify({"results":[], "count":0, "date":today,
                            "completed_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "st_days":st_days,"mt_days":mt_days,"lt_days":lt_days,
                            "errors":[],"error_count":0})

        cutoff = _dt.date.fromordinal(
            _dt.date.today().toordinal() - max_rows - 2
        ).isoformat()
        sym_ph = ",".join(["?"] * len(all_syms_list))
        oi_rows = con.execute(f"""
            SELECT symbol, date,
                   SUM(CASE WHEN type='call' THEN oi     ELSE 0 END) call_oi,
                   SUM(CASE WHEN type='put'  THEN oi     ELSE 0 END) put_oi,
                   SUM(CASE WHEN type='call' THEN volume ELSE 0 END) call_vol,
                   SUM(CASE WHEN type='put'  THEN volume ELSE 0 END) put_vol
            FROM options
            WHERE date >= ?
              AND symbol IN ({sym_ph})
              AND expiration >= date
            GROUP BY symbol, date
            ORDER BY symbol, date DESC
        """, [cutoff] + all_syms_list).fetchall()

        # Build {sym: [(date, c_oi, p_oi, c_vol, p_vol), ...] newest-first}
        from collections import defaultdict
        oi_by_sym = defaultdict(list)
        for sym, dt, c_oi, p_oi, c_vol, p_vol in oi_rows:
            oi_by_sym[sym].append((dt, c_oi or 0, p_oi or 0, c_vol or 0, p_vol or 0))

        # ── Query 3: OI-weighted spot per symbol per date ───────────────────
        spot_rows = con.execute("""
            SELECT symbol, date, SUM(strike*oi)/NULLIF(SUM(oi),0) spot
            FROM (
                SELECT symbol, date, strike, SUM(oi) oi,
                       ROW_NUMBER() OVER (PARTITION BY symbol,date ORDER BY SUM(oi) DESC) rn
                FROM options
                WHERE date >= date('now','-35 days')
                  AND expiration BETWEEN date('now') AND date('now','+45 days')
                GROUP BY symbol, date, strike
            )
            WHERE rn <= 5
            GROUP BY symbol, date ORDER BY symbol, date
        """).fetchall()
        spot_by_sym = defaultdict(list)
        for sym, dt, sp in spot_rows:
            if sp: spot_by_sym[sym].append((dt, round(sp, 2)))

        con.close()

        FUTURES_ROOTS = {"SPY","QQQ","IWM","DIA","GLD","SLV","TLT","XLE","XLF","XLK"}

        def _oi_delta(rows, n):
            """rows is newest-first list of (date,c_oi,p_oi,c_vol,p_vol)"""
            if len(rows) < 2: return 0, 0, rows[0] if rows else None
            d0  = rows[0]
            dn  = rows[min(n, len(rows)-1)]
            oi0 = d0[1]+d0[2]; oin = dn[1]+dn[2]
            vol0= d0[3]+d0[4]; voln= dn[3]+dn[4]
            return round((oi0-oin)/max(1,oin)*100,2), round((vol0-voln)/max(1,voln)*100,2), dn

        def _price_delta(pts, n):
            """pts is oldest-first list of (date, price)"""
            if not pts: return 0
            cur  = pts[-1][1]
            base = pts[max(0, len(pts)-1-n)][1]
            return round((cur-base)/max(0.01,base)*100, 2)

        def _signal(oi_pct, price_pct):
            if oi_pct > 0 and price_pct > 0: return "Long Buildup"
            if oi_pct > 0 and price_pct < 0: return "Short Buildup"
            if oi_pct < 0 and price_pct < 0: return "Long Unwinding"
            if oi_pct < 0 and price_pct > 0: return "Short Covering"
            return "Neutral"

        results = []
        for sym, exp in sym_exp.items():
            try:
                rows = oi_by_sym.get(sym, [])
                if len(rows) < 2: continue

                d0 = rows[0]
                call_oi_now = d0[1]; put_oi_now = d0[2]
                total_oi    = call_oi_now + put_oi_now
                if total_oi == 0: continue

                pcr = round(put_oi_now / max(1, call_oi_now), 3)

                pts = spot_by_sym.get(sym, [])
                spot = pts[-1][1] if pts else 0.0

                oi_st_pct, vol_st_pct, _ = _oi_delta(rows, st_days)
                oi_mt_pct, vol_mt_pct, _ = _oi_delta(rows, mt_days)
                oi_lt_pct, vol_lt_pct, _ = _oi_delta(rows, lt_days)

                pr_st = _price_delta(pts, st_days)
                pr_mt = _price_delta(pts, mt_days)
                pr_lt = _price_delta(pts, lt_days)

                st_out = _signal(oi_st_pct, pr_st)
                mt_out = _signal(oi_mt_pct, pr_mt)
                lt_out = _signal(oi_lt_pct, pr_lt)

                # Bias score
                bs = 0
                if   pr_mt >  2.0: bs += 2
                elif pr_mt >  0.5: bs += 1
                elif pr_mt < -2.0: bs -= 2
                elif pr_mt < -0.5: bs -= 1
                if   pr_lt >  4.0: bs += 2
                elif pr_lt >  1.5: bs += 1
                elif pr_lt < -4.0: bs -= 2
                elif pr_lt < -1.5: bs -= 1
                OI_SC = {"Long Buildup":2,"Short Covering":1,"Short Buildup":-2,"Long Unwinding":-1,"Neutral":0}
                bs += OI_SC.get(st_out,0)+OI_SC.get(mt_out,0)+OI_SC.get(lt_out,0)
                if pcr>1.5: bs-=1
                elif pcr<0.7: bs+=1

                if   bs>=4:  bias="Strongly Bullish"
                elif bs>=2:  bias="Bullish"
                elif bs>=1:  bias="Mildly Bullish"
                elif bs<=-4: bias="Strongly Bearish"
                elif bs<=-2: bias="Bearish"
                elif bs<=-1: bias="Mildly Bearish"
                else:        bias="Sideways"

                results.append({
                    "symbol":sym, "spot":spot, "expiry":exp,
                    "has_futures": sym.upper() in FUTURES_ROOTS,
                    "call_oi":call_oi_now,"put_oi":put_oi_now,
                    "total_oi":total_oi,"pcr":pcr,"pc_ratio":pcr,
                    "bias":bias,"bias_score":bs,
                    "price_1d_pct":pr_st,"oi_1d_chg":0,"oi_1d_pct":oi_st_pct,"vol_1d_pct":vol_st_pct,"st_outlook":st_out,
                    "price_5d_pct":pr_mt,"oi_5d_chg":0,"oi_5d_pct":oi_mt_pct,"vol_5d_pct":vol_mt_pct,"mt_outlook":mt_out,
                    "price_15d_pct":pr_lt,"oi_15d_chg":0,"oi_15d_pct":oi_lt_pct,"vol_15d_pct":vol_lt_pct,"lt_outlook":lt_out,
                    "st_days":st_days,"mt_days":mt_days,"lt_days":lt_days,
                })
            except Exception as _se:
                _errors.append({"sym":sym,"error":str(_se)})

        results.sort(key=lambda x: abs(x.get("oi_1d_pct",0)), reverse=True)

        # Save to cache
        completed_at = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            import sqlite3 as _sq
            from pathlib import Path as _P
            _db = str(_P(__file__).resolve().parents[2] / "options_data.db")
            _c  = _sq.connect(_db)
            _c.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
            cache_key = _wl_key('oi_buildup_scan', watchlist_id)
            ts_key = _wl_key('oib_completed_at', watchlist_id)
            _c.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                       (cache_key, _json.dumps(results), completed_at))
            _c.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                       (ts_key, 'oib_ts', completed_at))
            _c.commit(); _c.close()
        except: pass

        return jsonify({"results":results,"count":len(results),"date":today,
                        "completed_at":completed_at,
                        "st_days":st_days,"mt_days":mt_days,"lt_days":lt_days,
                        "errors":_errors[:10],"error_count":len(_errors)})
    except Exception as _e:
        return jsonify({"error":str(_e),"trace":_tb.format_exc()[-800:],"results":[],"count":0}), 500

@api_bp.route("/pcr_timeseries")
def pcr_timeseries():
    """PCR change over time for a specific expiry — time series."""
    sym = sanitize_symbol(request.args.get("symbol","SPY")) or "SPY"
    expiry = request.args.get("expiry","")
    
    from ..db import _connect
    import datetime as _dt
    
    con = _connect()
    
    # If no expiry specified, use nearest future expiry
    if not expiry:
        today = _dt.date.today().isoformat()
        row = con.execute(
            "SELECT MIN(expiration) FROM options WHERE symbol=? AND expiration>=?",
            (sym, today)).fetchone()
        expiry = row[0] if row and row[0] else ""
    
    if not expiry:
        con.close()
        return jsonify({"error": "No expiry found", "series": [], "expiry": ""})
    
    # Get PCR for each date we have data for this expiry
    rows = con.execute("""
        SELECT date,
               SUM(CASE WHEN type='call' THEN oi ELSE 0 END) as call_oi,
               SUM(CASE WHEN type='put' THEN oi ELSE 0 END) as put_oi,
               SUM(CASE WHEN type='call' THEN volume ELSE 0 END) as call_vol,
               SUM(CASE WHEN type='put' THEN volume ELSE 0 END) as put_vol
        FROM options WHERE symbol=? AND expiration=?
        GROUP BY date ORDER BY date
    """, (sym, expiry)).fetchall()
    con.close()
    
    series = []
    prev_pcr = None
    for r in rows:
        calls = r[1] or 0; puts = r[2] or 0
        total = calls + puts
        pcr = round(puts / max(1, calls), 3)
        pcr_chg = round(pcr - prev_pcr, 3) if prev_pcr is not None else 0
        pcr_chg_pct = round((pcr - prev_pcr) / max(0.001, prev_pcr) * 100, 2) if prev_pcr else 0
        
        # Sentiment from PCR
        if pcr > 1.5: sentiment = "EXTREME_FEAR"
        elif pcr > 1.1: sentiment = "CAUTIOUS"
        elif pcr < 0.6: sentiment = "COMPLACENT"
        elif pcr < 0.8: sentiment = "BULLISH"
        else: sentiment = "NEUTRAL"
        
        series.append({
            "date": r[0], "call_oi": calls, "put_oi": puts,
            "total_oi": total, "pcr": pcr,
            "pcr_change": pcr_chg, "pcr_change_pct": pcr_chg_pct,
            "sentiment": sentiment,
            "call_vol": r[3] or 0, "put_vol": r[4] or 0,
        })
        prev_pcr = pcr
    
    # Summary stats
    if series:
        pcrs = [s["pcr"] for s in series]
        current = series[-1]["pcr"]
        high = max(pcrs); low = min(pcrs)
        pcr_rank = round((current - low) / (high - low) * 100, 1) if high != low else 50
    else:
        current = high = low = pcr_rank = 0
    
    return jsonify({
        "symbol": sym, "expiry": expiry,
        "series": series,
        "summary": {
            "current": current, "high": high, "low": low,
            "pcr_rank": pcr_rank,
            "trend": "RISING" if len(series)>=2 and series[-1]["pcr"] > series[-2]["pcr"] else "FALLING" if len(series)>=2 else "N/A",
        }
    })

@api_bp.route("/iv_rank/<symbol>")
def api_iv_rank(symbol):
    """
    IV Rank, IV Percentile, current HV, and credit/debit recommendation.
    Uses 252 days of price history.
    """
    import math, numpy as np
    sym = sanitize_symbol(symbol) or "SPY"
    
    try:
        import yfinance as yf
        hist = yf.Ticker(sym).history(period="1y")
        closes = list(hist["Close"])
    except:
        return jsonify({"error": "Cannot fetch price history", "symbol": sym}), 500
    
    if len(closes) < 60:
        return jsonify({"error": "Insufficient price data", "symbol": sym}), 400
    
    # Compute daily log returns
    log_ret = np.log(np.array(closes[1:]) / np.array(closes[:-1]))
    
    # Rolling 20-day HV (annualized %)
    window = 20
    hvs = []
    for i in range(window, len(log_ret)):
        hv = np.std(log_ret[i-window:i]) * math.sqrt(252) * 100
        hvs.append(round(hv, 2))
    
    if len(hvs) < 30:
        return jsonify({"error": "Not enough HV data", "symbol": sym}), 400
    
    current_hv = hvs[-1]
    hv_30 = round(np.mean(hvs[-30:]), 2)   # 30-day avg
    hv_high = max(hvs)
    hv_low  = min(hvs)
    
    # IV Rank: where current HV sits in the 1-year range
    iv_rank = round((current_hv - hv_low) / max(0.01, hv_high - hv_low) * 100, 1)
    
    # IV Percentile: % of days where HV was BELOW current
    iv_pct = round(sum(1 for h in hvs if h < current_hv) / len(hvs) * 100, 1)
    
    # HV term structure
    hv_5  = round(np.std(log_ret[-5:])  * math.sqrt(252) * 100, 2) if len(log_ret) >= 5  else current_hv
    hv_10 = round(np.std(log_ret[-10:]) * math.sqrt(252) * 100, 2) if len(log_ret) >= 10 else current_hv
    hv_21 = round(np.std(log_ret[-21:]) * math.sqrt(252) * 100, 2) if len(log_ret) >= 21 else current_hv
    hv_63 = round(np.std(log_ret[-63:]) * math.sqrt(252) * 100, 2) if len(log_ret) >= 63 else current_hv
    
    # Regime classification
    if iv_rank >= 70:
        regime = "HIGH"
        recommendation = "SELL PREMIUM"
        advice = "IV is elevated — options are expensive. Favor credit strategies: Iron Condors, Vertical Spreads, Cash-Secured Puts."
        strategies = ["Iron Condor", "Bull Put Spread", "Bear Call Spread", "Short Strangle", "Cash-Secured Put"]
    elif iv_rank >= 40:
        regime = "MODERATE"
        recommendation = "MIXED"
        advice = "IV is moderate. Credit spreads still viable. Consider defined-risk structures over naked premium."
        strategies = ["Bull Put Spread", "Bear Call Spread", "Iron Condor", "Calendar Spread"]
    else:
        regime = "LOW"
        recommendation = "BUY OPTIONS"
        advice = "IV is compressed — options are cheap. Favor debit strategies or structures that benefit from IV expansion."
        strategies = ["Call Debit Spread", "Put Debit Spread", "Long Straddle", "Backspread", "LEAP Calls/Puts"]
    
    # IV trend (rising or falling)
    if len(hvs) >= 5:
        recent_avg = np.mean(hvs[-5:])
        prior_avg  = np.mean(hvs[-10:-5]) if len(hvs) >= 10 else recent_avg
        iv_trend = "RISING" if recent_avg > prior_avg * 1.05 else "FALLING" if recent_avg < prior_avg * 0.95 else "STABLE"
    else:
        iv_trend = "STABLE"
    
    return jsonify({
        "symbol": sym,
        "current_hv": current_hv,
        "hv_5": hv_5, "hv_10": hv_10, "hv_21": hv_21, "hv_63": hv_63,
        "hv_high": hv_high, "hv_low": hv_low, "hv_30d_avg": hv_30,
        "iv_rank": iv_rank,
        "iv_percentile": iv_pct,
        "regime": regime,
        "recommendation": recommendation,
        "advice": advice,
        "preferred_strategies": strategies,
        "iv_trend": iv_trend,
        "data_days": len(hvs),
    })

@api_bp.route("/oi_buildup_cached")
def oi_buildup_cached():
    """Return last OI buildup scan from DB cache."""
    import json as _json
    from ..db import _connect
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key=?", (_wl_key('oi_buildup_scan', watchlist_id),)).fetchone()
        if row:
            return jsonify({"results": _json.loads(row[0]), "date": row[1], "from_cache": True, "watchlist_id": watchlist_id})
        return jsonify({"results": [], "date": None, "from_cache": True, "watchlist_id": watchlist_id})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})
    finally:
        con.close()

@api_bp.route("/update_sectors", methods=["POST"])
def update_sectors():
    """
    Fetch and store sector for watchlist symbols using yfinance.
    Runs in a background thread — returns immediately.
    Results committed every 10 symbols so progress is never lost.
    """
    import yfinance as yf, threading, datetime, time
    from ..db import _connect

    force  = request.args.get("force","false").lower() == "true"
    wl_id  = request.args.get("watchlist_id", None, type=int)

    # Gather symbols
    con = _connect()
    con.execute("""CREATE TABLE IF NOT EXISTS sector_cache (
        symbol TEXT PRIMARY KEY, sector TEXT, industry TEXT, updated TEXT)""")
    con.commit()

    if wl_id:
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (wl_id,)
        ).fetchall()
        all_syms = [r[0] for r in rows] if rows else []
    if not wl_id or not all_syms:
        all_syms = [r[0] for r in con.execute(
            "SELECT symbol FROM symbols WHERE symbol IS NOT NULL ORDER BY symbol"
        ).fetchall()]
    con.close()

    if not all_syms:
        return jsonify({"ok": False, "error": "No symbols found", "total": 0})

    today  = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()

    # Store run state in app_cache for status polling
    import json, sqlite3 as _sq
    from pathlib import Path as _P
    _db = str(_P(__file__).resolve().parents[2] / "options_data.db")
    def _save_status(updated, skipped, failed, done=False):
        try:
            c = _sq.connect(_db)
            c.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
            c.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                      ("sector_update_status", json.dumps({
                          "updated": updated, "skipped": skipped, "failed": failed,
                          "total": len(all_syms), "done": done, "ts": today
                      }), today))
            c.commit(); c.close()
        except: pass

    def _run():
        updated = skipped = failed = 0
        batch_con = _sq.connect(_db)
        try:
            for i, sym in enumerate(all_syms):
                # Skip if fresh (unless force)
                if not force:
                    row = batch_con.execute(
                        "SELECT updated FROM sector_cache WHERE symbol=?", (sym,)
                    ).fetchone()
                    if row and row[0] and row[0] >= cutoff:
                        skipped += 1
                        continue
                try:
                    info     = yf.Ticker(sym).info or {}
                    sector   = info.get("sector","")   or info.get("sectorDisp","")
                    industry = info.get("industry","") or info.get("industryDisp","")
                    if sector:
                        batch_con.execute(
                            "INSERT OR REPLACE INTO sector_cache VALUES (?,?,?,?)",
                            (sym.upper(), sector, industry, today)
                        )
                        updated += 1
                    else:
                        failed += 1
                    time.sleep(0.15)
                except:
                    failed += 1
                # Commit every 10 symbols so progress is never lost
                if (i + 1) % 10 == 0:
                    batch_con.commit()
                    _save_status(updated, skipped, failed)
            batch_con.commit()
        finally:
            batch_con.close()
        _save_status(updated, skipped, failed, done=True)

    t = threading.Thread(target=_run, daemon=True); t.start()
    return jsonify({"ok": True, "started": True, "total": len(all_syms),
                    "mode": "background", "updated": 0, "skipped": 0, "failed": 0})


@api_bp.route("/update_sectors_status")
def update_sectors_status():
    """Poll the background sector update progress."""
    from ..db import _connect
    import json
    con = _connect()
    try:
        row = con.execute("SELECT value FROM app_cache WHERE key='sector_update_status'").fetchone()
        con.close()
        if row:
            return jsonify(json.loads(row[0]))
        return jsonify({"done": False, "updated": 0, "skipped": 0, "failed": 0, "total": 0})
    except:
        con.close()
        return jsonify({"done": False, "updated": 0, "total": 0})


@api_bp.route("/pre_earnings_cache", methods=["GET"])
def get_pre_earnings_cache():
    """Load last pre-earnings scan from DB."""
    import json as _j
    from ..db import _connect
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key=?", (_wl_key('pre_earnings_scan', watchlist_id),)).fetchone()
        if row:
            return jsonify({"results": _j.loads(row[0]), "date": row[1], "from_cache": True, "watchlist_id": watchlist_id})
        return jsonify({"results": [], "date": None, "from_cache": True, "watchlist_id": watchlist_id})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})
    finally:
        con.close()

@api_bp.route("/pre_earnings_cache", methods=["POST"])
def save_pre_earnings_cache():
    """Save pre-earnings scan results to DB."""
    import json as _j
    from ..db import _connect
    d = request.get_json()
    results = d.get("results", [])
    date_str = d.get("date", __import__('datetime').date.today().isoformat())
    watchlist_id = d.get("watchlist_id")
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                    (_wl_key('pre_earnings_scan', watchlist_id), _j.dumps(results), date_str))
        con.commit()
        return jsonify({"ok": True, "saved": len(results), "watchlist_id": watchlist_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        con.close()

@api_bp.route("/post_earnings_cache", methods=["GET"])
def get_post_earnings_cache():
    import json as _j; from ..db import _connect
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key=?", (_wl_key('post_earnings_scan', watchlist_id),)).fetchone()
        if row: return jsonify({"results": _j.loads(row[0]), "date": row[1], "from_cache": True, "watchlist_id": watchlist_id})
        return jsonify({"results": [], "date": None, "from_cache": True, "watchlist_id": watchlist_id})
    except Exception as e: return jsonify({"results": [], "error": str(e)})
    finally: con.close()

@api_bp.route("/post_earnings_cache", methods=["POST"])
def save_post_earnings_cache():
    import json as _j; from ..db import _connect
    d = request.get_json()
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                    (_wl_key('post_earnings_scan', d.get('watchlist_id')), _j.dumps(d.get("results",[])), d.get("date",__import__('datetime').date.today().isoformat())))
        con.commit()
        return jsonify({"ok": True, "saved": len(d.get("results",[])), "watchlist_id": d.get('watchlist_id')})
    except Exception as e: return jsonify({"ok": False, "error": str(e)})
    finally: con.close()

@api_bp.route("/sr_proximity_cache", methods=["GET"])
def get_sr_proximity_cache():
    import json as _j; from ..db import _connect
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key='sr_proximity_scan'").fetchone()
        if row: return jsonify({"results": _j.loads(row[0]), "date": row[1], "from_cache": True})
        return jsonify({"results": [], "date": None})
    except Exception as e: return jsonify({"results": [], "error": str(e)})
    finally: con.close()

@api_bp.route("/sr_proximity_cache", methods=["POST"])
def save_sr_proximity_cache():
    import json as _j; from ..db import _connect
    d = request.get_json(); con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES ('sr_proximity_scan',?,?)",
                    (_j.dumps(d.get("results",[])), d.get("date", __import__('datetime').date.today().isoformat())))
        con.commit()
        return jsonify({"ok": True, "saved": len(d.get("results",[]))})
    except Exception as e: return jsonify({"ok": False, "error": str(e)})
    finally: con.close()

@api_bp.route("/run_scheduler", methods=["POST"])
def run_scheduler_now():
    """Manually trigger scheduler tasks in background thread."""
    import threading
    def _bg():
        try:
            from ..app_factory import _run_all_tasks
            _run_all_tasks()
        except Exception as e:
            print(f"[manual scheduler] {e}")
    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Scheduler started in background"})

# ── Futures OI (Real Data) ────────────────────────────────────────────────────
@api_bp.route("/futures/config", methods=["GET"])
def get_futures_config():
    """Get current futures data source config."""
    from ..services.futures_oi_real import _get_api_key, FUTURES_MAP
    key = _get_api_key()
    return jsonify({
        "barchart_key_set": bool(key),
        "barchart_key_hint": key[:6]+"…" if key else "",
        "supported_symbols": list(FUTURES_MAP.keys()),
        "instructions": "Get free key at https://www.barchart.com/ondemand/free-api-key"
    })

@api_bp.route("/futures/config", methods=["POST"])
def save_futures_config():
    """Save Barchart API key."""
    from ..services.futures_oi_real import _save_api_key
    d = request.get_json() or {}
    key = d.get("barchart_api_key", "").strip()
    if not key: return jsonify({"ok": False, "error": "key required"}), 400
    _save_api_key(key)
    return jsonify({"ok": True, "message": "Barchart API key saved"})

@api_bp.route("/futures/roll_adjusted")
def futures_roll_adjusted_real():
    """Roll-adjusted signal — reads from best available source in DB."""
    sym = sanitize_symbol(request.args.get("symbol","SPY")) or "SPY"
    return jsonify(_unified_futures_signal(sym))

@api_bp.route("/futures/chart_data")
def futures_chart_data():
    """OHLCV + OI/volume series for all 3 contracts — for dashboard charts."""
    sym = sanitize_symbol(request.args.get("symbol","SPY")) or "SPY"
    return jsonify(_unified_futures_chart(sym))

def _unified_futures_chart(sym):
    """Build dashboard futures signal from Schwab real OI snapshots only.

    Older builds mixed Schwab OI with yfinance futures price/volume rows. That
    made the dashboard look populated even when no real exchange OI had been
    refreshed. This function intentionally ignores yfinance/volume-proxy rows.
    """
    try:
        from ..services.futures_oi_schwab import get_latest_oi
        d = get_latest_oi(sym, days=30)
    except Exception as e:
        return {
            "symbol": sym,
            "signal": "NO_DATA",
            "interpretation": f"Unable to read Schwab futures OI: {e}",
            "contracts": [],
            "oi_series": {},
            "source": "schwab",
            "is_volume_proxy": False,
            "score": 0,
        }

    series = d.get("oi_series") or {}
    contracts = [ct for ct in (d.get("contracts") or []) if (series.get(ct) or [])]
    # Keep contracts with real OI rows only.
    contracts = [ct for ct in contracts if any((r.get("oi") or 0) > 0 for r in (series.get(ct) or []))]
    oi_series = {ct: (series.get(ct) or []) for ct in contracts}

    if not contracts:
        return {
            "symbol": sym,
            "signal": "NO_DATA",
            "interpretation": "No Schwab real futures OI rows are stored yet. Connect Schwab, then run Scheduler → Fetch OI Now.",
            "contracts": d.get("contracts") or [],
            "oi_series": {},
            "source": "schwab",
            "is_volume_proxy": False,
            "score": 0,
        }

    contracts_sorted = sorted(contracts)
    front = contracts_sorted[0] if contracts_sorted else None
    back  = contracts_sorted[1] if len(contracts_sorted) > 1 else None

    signal = "NO_DATA"
    interp = "Run Scheduler → Fetch OI Now"
    front_chg = back_chg = net = 0

    if front and len(oi_series.get(front, [])) >= 1:
        f_data = oi_series.get(front, [])
        f_now  = f_data[-1].get("oi", 0) if f_data else 0
        # Prefer stored Schwab oi_change because it compares against the prior stored trading date.
        f_stored_chg = f_data[-1].get("oi_change") if f_data else None
        f_prev = f_data[-2].get("oi", f_now) if len(f_data) > 1 else f_now
        front_chg = int(f_stored_chg if f_stored_chg not in (None, "") else (f_now - f_prev))

        b_data = oi_series.get(back, []) if back else []
        b_now  = b_data[-1].get("oi", 0) if b_data else 0
        b_stored_chg = b_data[-1].get("oi_change") if b_data else None
        b_prev = b_data[-2].get("oi", b_now) if len(b_data) > 1 else b_now
        back_chg = int(b_stored_chg if b_stored_chg not in (None, "") else (b_now - b_prev))
        net = front_chg + back_chg

        is_roll = front_chg < 0 and back_chg > 0
        if len(f_data) < 2 and front_chg == 0 and back_chg == 0:
            signal = "FIRST_FETCH"
            interp = f"First Schwab OI snapshot — {front}: {f_now:,}"
            if b_now:
                interp += f" | {back}: {b_now:,}. Check again tomorrow for OI change direction."
        elif is_roll:
            signal = "ROLL"
            interp = f"Roll: {front} {front_chg:+,} → {back} {back_chg:+,}. Neutral."
        elif net > 0:
            signal = "NET_LONG_BUILDUP"
            interp = f"Combined futures OI +{net:,}. Institutional accumulation."
        elif net < 0:
            signal = "NET_LONG_UNWINDING"
            interp = f"Combined futures OI {net:,}. Position reduction."
        else:
            signal = "NEUTRAL"
            interp = "No net futures OI change."

    return {
        "symbol": sym,
        "signal": signal,
        "interpretation": interp,
        "is_roll": signal == "ROLL",
        "front": front,
        "back": back,
        "front_oi_chg": front_chg,
        "back_oi_chg": back_chg,
        "net_oi_chg": net,
        "contracts": contracts,
        "oi_series": oi_series,
        "source": "schwab",
        "is_volume_proxy": False,
        "roll_note": f"Roll: {front}→{back}" if signal == "ROLL" else None,
        "score": 3 if signal == "NET_LONG_BUILDUP" else -3 if signal == "NET_LONG_UNWINDING" else 0,
    }

def _unified_futures_signal(sym):
    return _unified_futures_chart(sym)

@api_bp.route("/futures/fetch_now", methods=["POST"])
def futures_fetch_now():
    """Manually trigger Futures OI fetch and expose job progress to the UI.

    The old implementation started a background thread, then the browser polled
    /fetch_status. If stale rows already existed, the browser stopped polling
    before the new Schwab request finished. This job_id lets the frontend wait
    for the current fetch rather than treating old data as success.
    """
    import threading
    import datetime as _dt

    sym = sanitize_symbol(request.args.get("symbol", "")) or None
    job_id = f"futoi-{int(_futures_time.time() * 1000)}"

    try:
        from ..services.futures_oi_schwab import SCHWAB_ROOTS
        syms = [sym.upper()] if sym else list(SCHWAB_ROOTS.keys())
    except Exception:
        syms = [sym.upper()] if sym else ["SPY", "QQQ", "IWM", "GLD", "TLT"]

    allow_proxy_fallback = str(request.args.get("proxy_fallback", request.args.get("fallback", "0"))).lower() in ("1", "true", "yes", "y")

    _futures_job_update(
        job_id,
        running=True,
        done=False,
        ok=False,
        started_at=_dt.datetime.now().isoformat(timespec="seconds"),
        started_ts=_futures_time.time(),
        symbols=syms,
        results=[],
        errors=[],
        stored_total=0,
        oi_total=0,
        used_proxy=False,
        reauthorize_required=False,
        message="Fetching real futures OI from Schwab...",
    )

    def _bg():
        results = []
        errors = []
        stored_total = 0
        oi_total_all = 0
        used_proxy = False
        reauthorize_required = False
        try:
            from ..services.futures_oi_schwab import fetch_futures_oi_schwab, _schwab_headers
            headers = _schwab_headers(auto_refresh=True)
            if not headers:
                err = getattr(_schwab_headers, "last_error", "Schwab is not connected/authenticated")
                detail = getattr(_schwab_headers, "last_detail", "")
                reauthorize_required = bool(getattr(_schwab_headers, "reauthorize_required", True))
                if detail:
                    err = f"{err} Detail: {detail}"
                errors.append(err)
                print(f"  Schwab auth unavailable: {err}")
            else:
                for s in syms:
                    r = fetch_futures_oi_schwab(s)
                    contracts = r.get("contracts", []) or []
                    oi_total = sum(c.get("oi", 0) or 0 for c in contracts)
                    stored = sum(1 for c in contracts if c.get("stored"))
                    row = {
                        "symbol": s,
                        "ok": bool(r.get("ok")),
                        "contracts": len(contracts),
                        "stored": stored,
                        "oi_total": oi_total,
                        "source": r.get("source", "schwab"),
                        "date": r.get("date", ""),
                        "error": r.get("error", ""),
                        "reauthorize_required": bool(r.get("reauthorize_required", False)),
                    }
                    results.append(row)
                    stored_total += stored
                    oi_total_all += oi_total
                    reauthorize_required = reauthorize_required or bool(row.get("reauthorize_required"))
                    if row.get("error"):
                        errors.append(f"{s}: {row['error']}")
                    elif row["ok"] and stored == 0:
                        errors.append(f"{s}: Schwab returned no usable OI rows for active contracts")
                    print(f"  Schwab {s}: {len(contracts)} contracts OI={oi_total:,} stored={stored}")
                    _futures_job_update(
                        job_id,
                        results=list(results),
                        errors=list(errors),
                        stored_total=stored_total,
                        oi_total=oi_total_all,
                        reauthorize_required=reauthorize_required,
                        message=f"Fetched {len(results)}/{len(syms)} symbols from Schwab...",
                    )
        except Exception as e:
            errors.append(f"Schwab fetch failed: {e}")
            print(f"[schwab fetch] {e}")

        # Do not silently store yfinance volume proxy rows as Futures OI. It is not
        # exchange open interest and can make stale data look refreshed. Keep this
        # opt-in for emergency diagnostics only.
        if stored_total <= 0 and allow_proxy_fallback:
            used_proxy = True
            try:
                from ..services.futures_oi_real import fetch_real_futures_oi
                fallback_results = []
                for s in syms:
                    r = fetch_real_futures_oi(s)
                    fallback_results.append({
                        "symbol": s,
                        "ok": bool(r.get("rows_saved", 0)),
                        "rows_saved": r.get("rows_saved", 0),
                        "source": r.get("source", "fallback"),
                        "error": r.get("error", ""),
                    })
                    print(f"  {s}: {r.get('rows_saved',0)} rows [{r.get('source','?')}] proxy/fallback")
                results.extend(fallback_results)
                errors.append("Proxy fallback was used. These rows may not be real exchange OI.")
            except Exception as e:
                errors.append(f"fallback fetch failed: {e}")
                print(f"[futures fallback] {e}")
        elif stored_total <= 0:
            errors.append("No new Schwab OI rows were stored. Existing cached rows, if any, were left unchanged.")

        ok = stored_total > 0
        if ok:
            msg = f"Schwab futures OI updated: {stored_total} contract rows stored."
        elif reauthorize_required:
            msg = "Schwab auth failed. Re-authorize Schwab in Settings, then Fetch OI Now again."
        else:
            msg = "Schwab futures OI fetch finished but did not store new rows."
        _futures_job_update(
            job_id,
            running=False,
            done=True,
            ok=ok,
            finished_at=_dt.datetime.now().isoformat(timespec="seconds"),
            finished_ts=_futures_time.time(),
            results=results,
            errors=errors,
            stored_total=stored_total,
            oi_total=oi_total_all,
            used_proxy=used_proxy,
            reauthorize_required=reauthorize_required,
            message=msg,
        )

    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id, "symbols": syms,
                    "message": "Fetching futures OI in background..."})

@api_bp.route("/futures/oi_data")
def futures_oi_data():
    """Get latest Schwab real OI series for dashboard charts.

    Do not fall back to yfinance/volume-proxy rows here. If Schwab is not
    connected or no rows are stored, return an explicit no-data payload.
    """
    sym = sanitize_symbol(request.args.get("symbol","SPY")) or "SPY"
    try:
        from ..services.futures_oi_schwab import get_latest_oi as _gl
        d = _gl(sym)
        d["source"] = "schwab"
        d["is_volume_proxy"] = False
        return jsonify(d)
    except Exception as e:
        return jsonify({"symbol": sym, "contracts": [], "oi_series": {}, "source": "none", "is_volume_proxy": False, "error": str(e)})

@api_bp.route("/futures/fetch_status")
def futures_fetch_status():
    """Check latest active Futures OI rows, with staleness and optional job status."""
    import sqlite3, datetime as _dt2
    from pathlib import Path

    symbol = sanitize_symbol(request.args.get("symbol", "")) or None
    job_id = request.args.get("job_id", "")
    job = _futures_job_get(job_id)

    db = str(Path(__file__).resolve().parents[2] / "options_data.db")
    try:
        # Make sure the fetched_at column exists for upgraded user DBs.
        try:
            from ..services.futures_oi_schwab import _ensure_table
            _ensure_table()
        except Exception:
            pass

        active = _futures_active_contracts(symbol)
        con = sqlite3.connect(db)

        # Only report currently relevant contracts. Old expired contracts should
        # not keep the status badge stale forever.
        if active:
            ph = ",".join(["?"] * len(active))
            rows = con.execute(f"""
                SELECT f.symbol, f.contract, f.trade_date, f.oi, f.volume, f.source, f.settle,
                       COALESCE(f.fetched_at, '')
                FROM futures_oi_daily f
                INNER JOIN (
                    SELECT contract, MAX(trade_date) as max_date
                    FROM futures_oi_daily
                    WHERE contract IN ({ph})
                    GROUP BY contract
                ) m ON f.contract = m.contract AND f.trade_date = m.max_date
                ORDER BY f.symbol, f.contract
            """, tuple(active)).fetchall()
        else:
            rows = con.execute("""
                SELECT f.symbol, f.contract, f.trade_date, f.oi, f.volume, f.source, f.settle,
                       COALESCE(f.fetched_at, '')
                FROM futures_oi_daily f
                INNER JOIN (
                    SELECT contract, MAX(trade_date) as max_date
                    FROM futures_oi_daily GROUP BY contract
                ) m ON f.contract = m.contract AND f.trade_date = m.max_date
                ORDER BY f.symbol, f.contract
            """).fetchall()
        con.close()

        today = _dt2.date.today().isoformat()
        if not rows:
            job_errors = list((job or {}).get("errors") or [])
            job_done = bool(job and job.get("done"))
            job_failed = bool(job_done and not (job or {}).get("ok") and job_errors)
            return jsonify({
                "has_data": False,
                "rows": [],
                "job": job,
                "job_failed": job_failed,
                "job_errors": job_errors,
                "active_contracts": sorted(active),
                "message": "Current fetch failed - no active futures OI rows were stored" if job_failed else "No active futures OI data in DB yet",
            })

        result = []
        for r in rows:
            last_date = r[2]
            try:
                ld = _dt2.date.fromisoformat(last_date)
                td = _dt2.date.today()
                days_old = max(0, (td - ld).days)
                # Business days elapsed after the stored date. Friday -> Monday is 1.
                biz_days_old = sum(1 for i in range(days_old)
                                   if (ld + _dt2.timedelta(days=i+1)).weekday() < 5)
                stale = biz_days_old > 1
                stale_msg = f" ⚠ {biz_days_old}d old" if stale else " ✅ current"
            except Exception:
                stale = False
                stale_msg = ""
                biz_days_old = 0
            result.append({
                "symbol":    r[0],
                "contract":  r[1],
                "last_date": last_date,
                "oi":        r[3],
                "volume":    r[4],
                "source":    r[5],
                "settle":    r[6],
                "fetched_at": r[7] or "",
                "is_today":  last_date == today,
                "stale":     stale,
                "stale_msg": stale_msg,
                "biz_days_old": biz_days_old,
            })

        any_stale = any(r["stale"] for r in result)
        running = bool(job and job.get("running"))
        job_done = bool(job and job.get("done"))
        job_errors = list((job or {}).get("errors") or [])
        job_failed = bool(job_done and not (job or {}).get("ok") and job_errors)
        msg = f"{len(result)} active contracts · "
        if running:
            msg += "fetch running"
        elif job_failed:
            msg += "current fetch failed; showing existing DB rows"
        else:
            msg += "data stale" if any_stale else "up to date"
        return jsonify({
            "has_data": True,
            "rows": result,
            "count": len(result),
            "any_stale": any_stale,
            "today": today,
            "job": job,
            "job_failed": job_failed,
            "job_errors": job_errors,
            "active_contracts": sorted(active),
            "message": msg,
        })
    except Exception as e:
        return jsonify({"has_data": False, "job": job, "error": str(e)})

@api_bp.route("/futures/clear", methods=["POST"])
def futures_clear():
    """Wipe both futures tables so we can test clean Schwab fetch."""
    import sqlite3
    from pathlib import Path
    DB = str(Path(__file__).resolve().parents[2] / "options_data.db")
    tables_cleared = []
    try:
        con = sqlite3.connect(DB)
        for tbl in ("futures_oi_daily", "futures_oi"):
            try:
                before = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                con.execute(f"DELETE FROM {tbl}")
                con.commit()
                tables_cleared.append({"table": tbl, "rows_deleted": before})
            except Exception as e:
                tables_cleared.append({"table": tbl, "error": str(e)})
        con.close()
        return jsonify({"ok": True, "cleared": tables_cleared})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── Momentum Retracement Scanner ─────────────────────────────────────────────
@api_bp.route("/scanner/momentum_retrace", methods=["GET","POST"])
def momentum_retrace_scan():
    """First pullback after sharp momentum move — bull and bear."""
    d             = request.get_json(silent=True) or {}
    def _p(k, default): return request.args.get(k, d.get(k, default))
    move_window   = int(_p("lookback",     15))
    move_pct      = float(_p("move_pct",   6.0))
    rsi_ob        = int(_p("rsi_peak",     68))
    rsi_os        = int(_p("rsi_trough",   32))
    delta_rsi_thr = int(_p("delta_rsi",    18))
    retrace_min   = int(_p("retrace_min",  3))
    retrace_max   = int(_p("retrace_max",  8))
    recovery_days = int(_p("recovery_days", 5))
    min_earn_days = _p("min_earn_days", None)
    if min_earn_days in ("", "None", "null"):
        min_earn_days = None
    else:
        try:
            min_earn_days = int(min_earn_days)
        except Exception:
            min_earn_days = None
    symbols_raw   = _p("symbols", "")
    watchlist_id  = _p("watchlist_id", None)
    symbols       = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()] or None
    if not symbols and watchlist_id:
        try:
            from ..db import _connect as _mr_con
            _c = _mr_con()
            rows = _c.execute("SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol", (int(watchlist_id),)).fetchall()
            _c.close()
            symbols = [r[0] for r in rows] if rows else None
        except Exception:
            pass
    workers       = int(_p("workers", 25))
    try:
        from ..scanners.momentum_retrace_scanner import run_momentum_retrace_scan
        result = run_momentum_retrace_scan(
            symbols=symbols, lookback=move_window, workers=workers,
            rsi_peak_thr=rsi_ob, rsi_trough_thr=rsi_os,
            delta_rsi_thr=delta_rsi_thr, move_pct=move_pct,
            retrace_min=retrace_min, retrace_max=retrace_max,
            recovery_days=recovery_days
        )
        if min_earn_days is not None:
            from ..scanners.scoring_service import filter_by_min_earnings
            result["bulls"] = filter_by_min_earnings(result.get("bulls", []), min_earn_days)
            result["bears"] = filter_by_min_earnings(result.get("bears", []), min_earn_days)
            result["bull_count"] = len(result["bulls"])
            result["bear_count"] = len(result["bears"])
            result["count"] = result["bull_count"] + result["bear_count"]
        if watchlist_id:
            result.setdefault("params", {})["watchlist_id"] = int(watchlist_id)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500



# ── Trend Exhaustion Second Pullback Scanner ─────────────────────────────────
@api_bp.route("/scanner/momentum_retrace_exhaustion", methods=["GET", "POST"])
def momentum_retrace_exhaustion_scan():
    """Find stocks that had an exhaustion signal in the last X days and are now on the second pullback."""
    d = request.get_json(silent=True) or {}
    def _p(k, default):
        return request.args.get(k, d.get(k, default))

    watchlist_id = _p("watchlist_id", None)
    symbols_raw = _p("symbols", "")
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()] or None
    if not symbols and watchlist_id:
        try:
            from ..db import _connect as _mr_con
            _c = _mr_con()
            rows = _c.execute("SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol", (int(watchlist_id),)).fetchall()
            _c.close()
            symbols = [r[0] for r in rows] if rows else None
        except Exception:
            pass

    lookback_days = int(_p("lookback_days", 20))
    rsi_hi = float(_p("rsi_hi", 68))
    rsi_lo = float(_p("rsi_lo", 32))
    diff_thr = float(_p("diff_thr", 20))
    min_bounce_pct = float(_p("min_bounce_pct", 4))
    min_second_bars = int(_p("min_second_bars", 1))
    min_earn_days = _p("min_earn_days", None)
    if min_earn_days in ("", "None", "null"):
        min_earn_days = None
    else:
        try:
            min_earn_days = int(min_earn_days)
        except Exception:
            min_earn_days = None
    exhaust_tf = str(_p("exhaust_tf", "1d")).strip() or "1d"
    pullback_tf = str(_p("pullback_tf", "1h")).strip() or "1h"
    workers = int(_p("workers", 18))

    try:
        from ..scanners.trend_second_pullback_scanner import run_trend_second_pullback_scan
        result = run_trend_second_pullback_scan(
            symbols=symbols,
            watchlist_id=int(watchlist_id) if watchlist_id else None,
            workers=workers,
            lookback_days=lookback_days,
            rsi_hi=rsi_hi,
            rsi_lo=rsi_lo,
            diff_thr=diff_thr,
            min_bounce_pct=min_bounce_pct,
            min_second_bars=min_second_bars,
            exhaust_tf=exhaust_tf,
            pullback_tf=pullback_tf,
        )
        if min_earn_days is not None:
            from ..scanners.scoring_service import filter_by_min_earnings
            result["bulls"] = filter_by_min_earnings(result.get("bulls", []), min_earn_days)
            result["bears"] = filter_by_min_earnings(result.get("bears", []), min_earn_days)
            result["bull_count"] = len(result["bulls"])
            result["bear_count"] = len(result["bears"])
            result["count"] = result["bull_count"] + result["bear_count"]
        if watchlist_id:
            result.setdefault("params", {})["watchlist_id"] = int(watchlist_id)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500

# ── RSI Multi-Timeframe Scanner ───────────────────────────────────────────────
@api_bp.route("/scanner/rsi_mtf", methods=["GET","POST"])
def rsi_mtf_scan():
    """Daily RSI delta diverging from intraday RSI delta — bull/bear retrace scanner."""
    try:
        from ..scanners.rsi_mtf_scanner import run_rsi_mtf_scan, load_params
        # Allow param override from request
        saved = load_params()
        req   = request.get_json(silent=True) or {}
        for k in saved:
            if k in req: saved[k] = req[k]
        # Also check query args
        for k in ["daily_delta_bull","daily_delta_bear","intra_delta_bull","intra_delta_bear",
                  "daily_lookback","intra_lookback","intra_tf","rsi_period","min_price","symbols"]:
            if request.args.get(k) is not None:
                v = request.args.get(k)
                try: v = float(v) if "." in v else int(v)
                except: pass
                saved[k] = v
        for kb in ["macd_filter","ema_filter"]:
            if request.args.get(kb) is not None:
                saved[kb] = request.args.get(kb).lower() in ("1","true","yes")
        # Support watchlist_id — override symbols from watchlist
        min_earn_days = request.args.get("min_earn_days", None)
        if min_earn_days in (None, "", "None", "null"):
            min_earn_days = req.get("min_earn_days")
        if min_earn_days in (None, "", "None", "null"):
            min_earn_days = None
        else:
            try:
                min_earn_days = int(min_earn_days)
            except Exception:
                min_earn_days = None
        wl_id = request.args.get("watchlist_id", None, type=int) or req.get("watchlist_id")
        if wl_id:
            try:
                from ..db import _connect as _rwlcon
                _wlc = _rwlcon()
                wl_rows = _wlc.execute(
                    "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                    (int(wl_id),)
                ).fetchall()
                _wlc.close()
                if wl_rows:
                    saved["symbols"] = ",".join(r[0] for r in wl_rows)
            except: pass
        result = run_rsi_mtf_scan(params=saved, workers=25)
        if min_earn_days is not None:
            from ..scanners.scoring_service import filter_by_min_earnings
            result["bulls"] = filter_by_min_earnings(result.get("bulls", []), min_earn_days)
            result["bears"] = filter_by_min_earnings(result.get("bears", []), min_earn_days)
            result["bull_count"] = len(result["bulls"])
            result["bear_count"] = len(result["bears"])
            result["total_scanned"] = result.get("total_scanned", 0)
        # Save to cache
        import json as _rj, datetime as _rd
        from ..db import _connect as _rc
        _ts_rsi = _rd.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            _cc = _rc()
            _cc.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
            _cc.execute("INSERT OR REPLACE INTO app_cache VALUES ('rsi_mtf_scan',?,?)",
                        (_rj.dumps(result), _ts_rsi))
            _cc.commit(); _cc.close()
        except: pass
        result["completed_at"] = _ts_rsi
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500

@api_bp.route("/scanner/rsi_mtf/params", methods=["GET"])
def rsi_mtf_get_params():
    from ..scanners.rsi_mtf_scanner import load_params
    return jsonify(load_params())

@api_bp.route("/scanner/rsi_mtf/params", methods=["POST"])
def rsi_mtf_save_params():
    from ..scanners.rsi_mtf_scanner import save_params, load_params
    d = request.get_json(force=True) or {}
    current = load_params()
    current.update(d)
    save_params(current)
    return jsonify({"ok": True, "params": current})

@api_bp.route("/rsi_mtf_cache", methods=["GET"])
def get_rsi_mtf_cache():
    import json as _json
    from ..db import _connect
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key='rsi_mtf_scan'").fetchone()
        if row:
            d = _json.loads(row[0])
            return jsonify({"bulls": d.get("bulls",[]), "bears": d.get("bears",[]),
                            "bull_count": d.get("bull_count",0), "bear_count": d.get("bear_count",0),
                            "total_scanned": d.get("total_scanned",0),
                            "params": d.get("params",{}), "date": row[1], "from_cache": True})
        return jsonify({"bulls": [], "bears": [], "date": None, "from_cache": True})
    except Exception as e:
        return jsonify({"bulls": [], "bears": [], "error": str(e)})
    finally: con.close()

@api_bp.route("/rsi_mtf_cache", methods=["POST"])
def save_rsi_mtf_cache():
    import json as _json
    from ..db import _connect
    import datetime as _dt
    d = request.get_json(force=True) or {}
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES ('rsi_mtf_scan',?,?)",
                    (_json.dumps(d), _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally: con.close()

@api_bp.route("/sr_breakout_cache", methods=["GET"])
def get_sr_breakout_cache():
    import json as _json
    from ..db import _connect
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key='sr_breakout_scan'").fetchone()
        if row:
            return jsonify({"results": _json.loads(row[0]), "date": row[1], "from_cache": True})
        return jsonify({"results": [], "date": None, "from_cache": True})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})
    finally: con.close()

@api_bp.route("/sr_breakout_cache", methods=["POST"])
def save_sr_breakout_cache():
    import json as _json
    from ..db import _connect
    import datetime as _dt
    d = request.get_json(force=True) or {}
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES ('sr_breakout_scan',?,?)",
                    (_json.dumps(d), _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally: con.close()


# ── Trend Exhaustion Scanner ───────────────────────────────────────────────
@api_bp.route("/scanner/trend_exhaustion", methods=["GET"])
def trend_exhaustion_scan():
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    min_earn_days = request.args.get("min_earn_days", None)
    if min_earn_days in (None, "", "None", "null"):
        min_earn_days = None
    else:
        try:
            min_earn_days = int(min_earn_days)
        except Exception:
            min_earn_days = None
    symbols_raw = request.args.get("symbols", "")
    sector = (request.args.get("sector") or "").strip()
    workers = request.args.get("workers", 18, type=int)
    min_score = request.args.get("min_score", 50, type=int)
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()] or None
    try:
        from ..scanners.trend_exhaustion_scanner import run_trend_exhaustion_scan
        result = run_trend_exhaustion_scan(symbols=symbols, watchlist_id=watchlist_id, workers=workers)
        rows = result.get("results", [])
        if sector:
            try:
                from ..services.sector_service import get_symbol_sector
                rows = [r for r in rows if get_symbol_sector(r.get("symbol", "")) == sector]
            except Exception:
                pass
        rows = [r for r in rows if (r.get("score") or 0) >= min_score]
        if min_earn_days is not None:
            from ..scanners.scoring_service import filter_by_min_earnings
            rows = filter_by_min_earnings(rows, min_earn_days)
        result["results"] = rows
        result["count"] = len(rows)
        import json as _j, datetime as _d
        from ..db import _connect as _c
        try:
            con = _c()
            con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
            con.execute("INSERT OR REPLACE INTO app_cache VALUES ('trend_exhaustion_scan', ?, ?)", (_j.dumps(rows), _d.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            con.commit(); con.close()
        except Exception:
            pass
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500
