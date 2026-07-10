"""
institutional_scanner.py — Institutional Accumulation Breakout Scanner

Identifies stocks where smart money has been quietly accumulating (tight base,
rising OI) then made a decisive move with strong volume — the classic 
institutional footprint. Configurable thresholds.
"""
import sqlite3, json
from pathlib import Path
from datetime import datetime, date, timedelta
from flask import Blueprint, jsonify, request

inst_bp = Blueprint("inst_bp", __name__, url_prefix="/scanner/institutional")
DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")

# ── Default scoring parameters ─────────────────────────────────────────────
DEFAULTS = {
    "min_score":          3.0,   # minimum composite score to show result
    "ema200_pct":         3.0,   # max % below 200 EMA (hard filter)
    "base_days":          30,    # base detection window (days)
    "base_tight_max":     30.0,  # max base range % to count as tight base
    "min_price_5d":       1.5,   # min 5D price change %
    "min_vol_surge":      1.2,   # min volume surge (5D avg / 20D avg)
    "rsi_min":            40.0,  # min RSI
    "rsi_max":            80.0,  # max RSI (avoid overbought)
    "min_price":          3.0,   # min stock price (filter penny stocks)
    "require_above_base": False, # if True, price must exceed the 30D base high
}

def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

def _get_symbols(watchlist_id=None):
    con = _conn()
    if watchlist_id:
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (watchlist_id,)
        ).fetchall()
        syms = [r[0] for r in rows]
    else:
        syms = [r[0] for r in con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()]
    con.close()
    return syms or []

def _get_oi_data(symbol, days=40):
    con = _conn()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = con.execute("""
        SELECT date,
               SUM(CASE WHEN type='call' THEN oi ELSE 0 END) call_oi,
               SUM(CASE WHEN type='put'  THEN oi ELSE 0 END) put_oi
        FROM options WHERE symbol=? AND date>=? AND expiration>=date
        GROUP BY date ORDER BY date
    """, (symbol, cutoff)).fetchall()
    con.close()
    return [dict(r) for r in rows]

def _ema(series, n):
    return series.ewm(span=n, adjust=False).mean()

def _scan_symbol(sym, use_oi=True, p=None):
    if p is None: p = DEFAULTS
    try:
        import yfinance as yf
        import pandas as pd, numpy as np

        tk   = yf.Ticker(sym)
        hist = tk.history(period="1y", interval="1d")
        if hist is None or len(hist) < 60:
            return None

        closes = hist["Close"].values.astype(float)
        highs  = hist["High"].values.astype(float)
        lows   = hist["Low"].values.astype(float)
        vols   = hist["Volume"].values.astype(float)
        n = len(closes)
        price = float(closes[-1])

        # Penny filter
        if price < float(p.get("min_price", 3)):
            return None

        # EMAs
        s = pd.Series(closes)
        ema20  = float(_ema(s, 20).iloc[-1])
        ema50  = float(_ema(s, 50).iloc[-1])
        ema200 = float(_ema(s, 200).iloc[-1])

        # Hard filter: must not be too far below 200 EMA
        ema200_pct = float(p.get("ema200_pct", 3.0))
        if price < ema200 * (1 - ema200_pct/100):
            return None

        # RSI
        delta = s.diff(); gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
        avg_g = gain.ewm(span=14, adjust=False).mean()
        avg_l = loss.ewm(span=14, adjust=False).mean()
        rsi = float((100 - 100 / (1 + avg_g / avg_l.replace(0, 1e-10))).iloc[-1])

        # RSI range filter
        rsi_min = float(p.get("rsi_min", 40))
        rsi_max = float(p.get("rsi_max", 80))
        if rsi < rsi_min or rsi > rsi_max:
            return None

        # Volume metrics
        vol_20d_avg = float(np.mean(vols[-25:-5]))
        vol_5d_avg  = float(np.mean(vols[-5:]))
        vol_surge   = round(vol_5d_avg / max(1, vol_20d_avg), 2)
        vol_peak    = float(max(vols[-5:]))
        vol_peak_pct= round(vol_peak / max(1, vol_20d_avg) * 100, 0)

        # Price changes
        price_5d_chg  = round((price - closes[-6]) / closes[-6] * 100, 2) if n >= 6 else 0
        price_20d_chg = round((price - closes[-21]) / closes[-21] * 100, 2) if n >= 21 else 0

        # Volume filter
        min_vol = float(p.get("min_vol_surge", 1.2))
        # Don't hard-filter, just score lower

        # Price move filter
        min_5d = float(p.get("min_price_5d", 1.5))

        # Base detection
        base_days = int(p.get("base_days", 30))
        if n < base_days + 10:
            return None
        base_highs = highs[-(base_days+5):-5]
        base_lows  = lows[-(base_days+5):-5]
        base_high  = float(max(base_highs))
        base_low   = float(min(base_lows))
        base_mid   = (base_high + base_low) / 2
        base_tight = round((base_high - base_low) / max(0.01, base_mid) * 100, 2)
        is_above_base = price > base_high
        pct_into_base = round((price - base_low) / max(0.01, base_high - base_low) * 100, 1)

        # Require above base?
        if p.get("require_above_base", False) and not is_above_base:
            return None

        # 52W metrics
        hi_52w = float(max(highs[-252:]))
        pct_from_52wh = round((price - hi_52w) / hi_52w * 100, 1)
        new_52w_high = price >= hi_52w * 0.98

        # Breakout type (informational, not a hard filter)
        if new_52w_high and is_above_base:
            breakout_type = "52W Breakout"
        elif is_above_base and price_5d_chg >= 3:
            breakout_type = "Base Breakout"
        elif is_above_base:
            breakout_type = "Resistance Break"
        elif price > ema50 and price_5d_chg >= 1.5:
            breakout_type = "EMA50 Reclaim"
        elif price > ema20 and price_5d_chg >= 2:
            breakout_type = "EMA20 Reclaim"
        elif abs(price - ema200) / ema200 < 0.04:
            breakout_type = "200 EMA Hold"
        elif pct_into_base >= 80 and base_tight < float(p.get("base_tight_max", 30)):
            breakout_type = "Pre-Breakout"
        elif price_5d_chg >= 3 and vol_surge >= 1.5:
            breakout_type = "Vol Momentum"
        elif price_5d_chg >= min_5d or vol_surge >= min_vol:
            breakout_type = "Setup"
        else:
            breakout_type = "Watching"

        # Skip very weak setups with no move at all
        if price_5d_chg < min_5d and vol_surge < min_vol and not is_above_base and pct_into_base < 70:
            return None

        # Liquidity sweep
        prior_support = float(min(lows[-30:-10])) if n >= 30 else base_low
        recent_lows   = lows[-10:]
        swept = float(min(recent_lows)) < prior_support * 0.995
        if swept:
            post = closes[-10:][int(np.argmin(recent_lows)):]
            liquidity_sweep = len(post) > 0 and float(post[-1]) > prior_support
            sweep_depth = round(abs(float(min(recent_lows)) - prior_support) / prior_support * 100, 2)
        else:
            liquidity_sweep = False; sweep_depth = 0.0

        # OI accumulation
        oi_buildup_pct = None; call_put_trend = 0.0; total_oi = 0; has_oi_data = False
        if use_oi:
            oi_rows = _get_oi_data(sym, days=int(base_days)+5)
            has_oi_data = len(oi_rows) >= 5
            if has_oi_data:
                oi_first = oi_rows[0]["call_oi"] + oi_rows[0]["put_oi"]
                oi_last  = oi_rows[-1]["call_oi"] + oi_rows[-1]["put_oi"]
                oi_buildup_pct = round((oi_last - oi_first) / max(1, oi_first) * 100, 1)
                early_cpr = oi_rows[0]["call_oi"] / max(1, oi_rows[0]["put_oi"])
                late_cpr  = oi_rows[-1]["call_oi"] / max(1, oi_rows[-1]["put_oi"])
                call_put_trend = round(late_cpr - early_cpr, 3)
                total_oi = oi_rows[-1]["call_oi"] + oi_rows[-1]["put_oi"]

        # ── Scoring ──────────────────────────────────────────────────────────
        score = 0.0

        # 1. Base quality (0-2)
        base_tight_max = float(p.get("base_tight_max", 30))
        if base_tight < 8:          score += 2.0
        elif base_tight < 15:       score += 1.5
        elif base_tight < base_tight_max: score += 1.0
        else:                       score += 0.3  # still score something

        # 2. OI (0-2) — only if watchlist has OI data
        if has_oi_data and oi_buildup_pct is not None:
            if   oi_buildup_pct > 50:  score += 2.0
            elif oi_buildup_pct > 20:  score += 1.5
            elif oi_buildup_pct > 5:   score += 1.0
            elif oi_buildup_pct >= 0:  score += 0.5
            if call_put_trend > 0.2:   score += 0.3

        # 3. Volume surge (0-2)
        if   vol_surge > 3.0: score += 2.0
        elif vol_surge > 2.0: score += 1.5
        elif vol_surge > 1.5: score += 1.0
        elif vol_surge > 1.2: score += 0.7
        elif vol_surge > 1.0: score += 0.3

        # 4. Breakout level (0-2)
        btypes = {
            "52W Breakout":    2.0, "Base Breakout":   1.5,
            "Resistance Break":1.2, "EMA50 Reclaim":   1.0,
            "EMA20 Reclaim":   0.8, "200 EMA Hold":    0.8,
            "Pre-Breakout":    0.7, "Vol Momentum":    0.6,
            "Setup":           0.4, "Watching":        0.2,
        }
        score += btypes.get(breakout_type, 0)

        # 5. Momentum (0-2)
        if rsi >= 55 and rsi <= 75:         score += 0.8
        elif rsi >= 45:                     score += 0.5
        else:                               score += 0.2
        if price > ema200:                  score += 0.4
        if price > ema50:                   score += 0.3
        if price > ema20:                   score += 0.2
        if ema20 > ema50 > ema200:          score += 0.3
        if price_5d_chg >= 5:               score += 0.3
        elif price_5d_chg >= 3:             score += 0.2
        elif price_5d_chg >= 1.5:           score += 0.1

        # Bonuses
        if liquidity_sweep:                 score += 0.5
        if vol_peak_pct > 300:             score += 0.3
        if pct_into_base >= 90:            score += 0.2

        score = round(min(10, score), 1)

        if score < float(p.get("min_score", 3.0)):
            return None

        signal = "🔥 Strong" if score >= 7 else "✅ Moderate" if score >= 5 else "👀 Watch"

        def _safe(v):
            """Replace NaN/Inf with None for JSON safety."""
            import math
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            return v

        return {
            "symbol": sym, "price": _safe(round(price, 2)), "score": score, "signal": signal,
            "breakout_type": breakout_type, "price_5d_chg": _safe(price_5d_chg),
            "price_20d_chg": _safe(price_20d_chg), "vol_surge": _safe(vol_surge),
            "vol_peak_pct": _safe(vol_peak_pct), "rsi": _safe(round(rsi, 1)),
            "ema20": _safe(round(ema20, 2)), "ema50": _safe(round(ema50, 2)), "ema200": _safe(round(ema200, 2)),
            "above_ema20": price > ema20, "above_ema50": price > ema50, "above_ema200": price > ema200,
            "base_tight_pct": _safe(base_tight), "base_days": base_days,
            "base_high": _safe(round(base_high, 2)),
            "pct_into_base": _safe(pct_into_base), "is_above_base": is_above_base,
            "liquidity_sweep": liquidity_sweep, "sweep_depth": _safe(sweep_depth),
            "oi_buildup_pct": oi_buildup_pct, "oi_available": has_oi_data,
            "call_put_trend": _safe(call_put_trend), "total_oi": total_oi,
            "hi_52w": _safe(round(hi_52w, 2)), "pct_from_52wh": _safe(pct_from_52wh),
            "new_52w_high": new_52w_high,
        }
    except Exception as e:
        return {"symbol": sym, "error": str(e)[:80], "score": -1}


@inst_bp.route("/scan", methods=["GET","POST"])
def institutional_scan():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    wl_id = request.args.get("watchlist_id", None, type=int)

    # Merge parameters from query string / JSON body
    params = dict(DEFAULTS)
    body = request.get_json(silent=True) or {}
    for k in DEFAULTS:
        if k in body:
            params[k] = type(DEFAULTS[k])(body[k])
        elif request.args.get(k) is not None:
            try: params[k] = type(DEFAULTS[k])(request.args.get(k))
            except: pass

    symbols = _get_symbols(wl_id)
    if not symbols:
        return jsonify({"results": [], "count": 0, "error": "No symbols in watchlist"})

    wl_name = "All Symbols"; use_oi = True
    if wl_id:
        try:
            con = _conn()
            row = con.execute("SELECT name, fetch_options_oi FROM watchlists WHERE id=?", (wl_id,)).fetchone()
            con.close()
            if row: wl_name, use_oi = row[0], bool(row[1])
        except: pass

    results = []; errors = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_scan_symbol, sym, use_oi, params): sym for sym in symbols}
        for fut in as_completed(futures):
            try:
                r = fut.result()
                if r:
                    if r.get("score", 0) < 0:
                        errors.append(r.get("symbol","?") + ": " + r.get("error","err"))
                    else:
                        r["watchlist"] = wl_name
                        results.append(r)
            except Exception as e:
                errors.append(str(e)[:40])

    results.sort(key=lambda x: x["score"], reverse=True)
    completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    import math as _math
    def _clean(v):
        """Replace NaN/Inf with None so json.dumps produces valid JSON."""
        if isinstance(v, float) and (_math.isnan(v) or _math.isinf(v)):
            return None
        return v
    def _clean_row(row):
        return {k: _clean(v) for k, v in row.items()}
    results = [_clean_row(r) for r in results]

    try:
        con = _conn()
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES ('institutional_scan',?,?)",
                    (json.dumps(results), completed_at))
        con.commit(); con.close()
    except: pass

    return jsonify({"results": results, "count": len(results),
                    "total_scanned": len(symbols), "completed_at": completed_at,
                    "watchlist": wl_name, "use_oi": use_oi,
                    "params_used": params, "errors": len(errors), "error_sample": errors[:3]})


@inst_bp.route("/scan_cached")
def institutional_scan_cached():
    try:
        con = _conn()
        row = con.execute("SELECT value, updated FROM app_cache WHERE key='institutional_scan'").fetchone()
        con.close()
        if row:
            data = json.loads(row[0])
            return jsonify({"results": data, "count": len(data), "completed_at": row[1], "from_cache": True})
        return jsonify({"results": [], "count": 0, "from_cache": True})
    except:
        return jsonify({"results": [], "count": 0})


@inst_bp.route("/defaults")
def get_defaults():
    return jsonify(DEFAULTS)
