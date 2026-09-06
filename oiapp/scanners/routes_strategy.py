# oiapp/scanners/routes_strategy.py  v2
"""
Strategy engine v2:
- Symbol dropdown from DB symbols
- Full technical analysis: RSI-14, EMA-90(RSI), MACD, BB, ATR, Volume, IV rank
- OI history from local DB: OI change, put/call wall strength, PCR trend
- Auto-selects best strategy (optional manual override)
- Returns PoP, R:R, rationale, grade per strategy
"""
from flask import Blueprint, jsonify, request
import sqlite3, math
from pathlib import Path
from datetime import date, datetime

from ._spot_cache import get_spot
try:
    from ..services.option_prices import enrich_strategies as _enrich
except Exception as _ep:
    print(f"[strategy] option_prices not loaded: {_ep}")
    def _enrich(sym, exp, strats): return strats

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH
strategy_bp = Blueprint("strategy_bp", __name__, url_prefix="/strategy")


# ── DB helpers ────────────────────────────────────────────────────────────────
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def _today(): return date.today().strftime("%Y-%m-%d")


def _future_expirations(symbol):
    con = _conn()
    rows = con.execute(
        "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration",
        (symbol, _today())
    ).fetchall()
    con.close()
    return [r["expiration"] for r in rows]


def _all_symbols():
    con = _conn()
    rows = con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()
    con.close()
    return [r["symbol"] for r in rows] or ["SPY"]


def _oi_latest(symbol, expiry, con):
    d = con.execute(
        "SELECT MAX(date) AS d FROM options WHERE symbol=? AND expiration=?",
        (symbol, expiry)
    ).fetchone()["d"]
    if not d: return []
    rows = con.execute("""
        SELECT type, strike, SUM(oi) AS oi, SUM(volume) AS vol
        FROM options WHERE symbol=? AND expiration=? AND date=?
        GROUP BY type, strike HAVING SUM(oi)>0 ORDER BY strike
    """, (symbol, expiry, d)).fetchall()
    return [dict(r) for r in rows]


def _oi_history(symbol, expiry, con, days=5):
    """OI per (type, strike) for last N snapshot dates."""
    dates = [r[0] for r in con.execute("""
        SELECT DISTINCT date FROM options WHERE symbol=? AND expiration=?
        ORDER BY date DESC LIMIT ?
    """, (symbol, expiry, days)).fetchall()]
    if not dates: return {}
    hist = {}  # (type,strike) → [(date, oi)]
    for d in reversed(dates):
        rows = con.execute("""
            SELECT type, strike, SUM(oi) AS oi FROM options
            WHERE symbol=? AND expiration=? AND date=?
            GROUP BY type, strike
        """, (symbol, expiry, d)).fetchall()
        for r in rows:
            k = (r["type"], float(r["strike"]))
            hist.setdefault(k, []).append((d, int(r["oi"])))
    return hist


def _pcr_history(symbol, con, days=5):
    """PCR trend across all future expiries for last N dates."""
    dates = [r[0] for r in con.execute("""
        SELECT DISTINCT date FROM options WHERE symbol=? AND date>=?
        ORDER BY date DESC LIMIT ?
    """, (symbol, _today()[:8]+"01", days)).fetchall()]  # crude; enough for trend
    dates = [r[0] for r in con.execute("""
        SELECT DISTINCT date FROM options WHERE symbol=?
        ORDER BY date DESC LIMIT ?
    """, (symbol, days)).fetchall()]
    result = []
    for d in reversed(dates):
        row = con.execute("""
            SELECT SUM(CASE WHEN type='put' THEN oi ELSE 0 END) put_oi,
                   SUM(CASE WHEN type='call' THEN oi ELSE 0 END) call_oi
            FROM options WHERE symbol=? AND date=? AND expiration>=?
        """, (symbol, d, _today())).fetchone()
        p, c = (row["put_oi"] or 0), (row["call_oi"] or 0)
        result.append({"date": d, "pcr": round(p/c, 3) if c else None,
                        "put_oi": p, "call_oi": c})
    return result


def _pcr_by_expiry(symbol, con):
    rows = con.execute("""
        WITH ld AS (SELECT expiration,MAX(date) AS snap FROM options WHERE symbol=? AND expiration>=? GROUP BY expiration)
        SELECT o.expiration,
               SUM(CASE WHEN o.type='put' THEN o.oi ELSE 0 END) put_oi,
               SUM(CASE WHEN o.type='call' THEN o.oi ELSE 0 END) call_oi,
               ld.snap
        FROM options o JOIN ld ON o.expiration=ld.expiration AND o.date=ld.snap
        WHERE o.symbol=? GROUP BY o.expiration ORDER BY o.expiration
    """, (symbol, _today(), symbol)).fetchall()
    out = []
    for r in rows:
        p, c = (r["put_oi"] or 0), (r["call_oi"] or 0)
        out.append({"expiration": r["expiration"], "put_oi": p, "call_oi": c,
                    "pcr": round(p/c, 3) if c else None, "snapshot": r["snap"]})
    return out


# ── Technical Analysis (from yfinance, with caching) ─────────────────────────
def _compute_ta(symbol):
    try:
        import yfinance as yf, time
        # reuse market.py cache if available
        try:
            from ..services.market import _get, _set
            key = f"ta_full:{symbol}"
            cached = _get(key)
            if cached: return cached
        except: _get = _set = None

        df = yf.Ticker(symbol).history(period="1y")
        if df.empty or len(df) < 30: return None
        C = df["Close"].tolist()
        H = df["High"].tolist()
        L = df["Low"].tolist()
        V = df["Volume"].tolist()
        n = len(C) - 1

        def ema(arr, p):
            k = 2/(p+1); out = list(arr)
            for i in range(1, len(out)): out[i] = arr[i]*k + out[i-1]*(1-k)
            return out

        def rsi_calc(closes, p=14):
            if len(closes) < p+1: return [50.0]*len(closes)
            out = [50.0]*len(closes); g=l=0.0
            for i in range(1,p+1):
                d=closes[i]-closes[i-1]
                if d>0: g+=d
                else: l-=d
            ag,al=g/p,l/p
            out[p]=100 if al==0 else 100-100/(1+ag/al)
            for i in range(p+1,len(closes)):
                d=closes[i]-closes[i-1]
                ag=(ag*(p-1)+max(d,0))/p; al=(al*(p-1)+max(-d,0))/p
                out[i]=100 if al==0 else 100-100/(1+ag/al)
            return out

        rsi14   = rsi_calc(C, 14)
        ema90rsi = ema(rsi14, 90)
        ema12   = ema(C, 12)
        ema26   = ema(C, 26)
        macd_line = [a-b for a,b in zip(ema12, ema26)]
        signal    = ema(macd_line, 9)
        histogram = [a-b for a,b in zip(macd_line, signal)]
        ema20   = ema(C, 20)
        ema50   = ema(C, 50)
        ema200  = ema(C, 200) if len(C)>=200 else [None]*len(C)

        # BB-20
        bb_pct = bb_upper = bb_lower = None
        if n >= 19:
            sl = C[n-19:n+1]; mean=sum(sl)/20
            std = math.sqrt(sum((x-mean)**2 for x in sl)/20)
            bb_upper = mean+2*std; bb_lower = mean-2*std
            bb_pct = ((C[n]-bb_lower)/(bb_upper-bb_lower)*100) if bb_upper!=bb_lower else 50

        # ATR
        tr = [H[i]-L[i] if i==0 else max(H[i]-L[i],abs(H[i]-C[i-1]),abs(L[i]-C[i-1]))
              for i in range(len(C))]
        atr = ema(tr, 14)[n]

        # Volume trend (20-day avg vs 5-day avg)
        vol_avg20 = sum(V[max(0,n-19):n+1])/min(20,n+1)
        vol_avg5  = sum(V[max(0,n-4):n+1])/min(5,n+1)
        vol_ratio = round(vol_avg5/vol_avg20, 2) if vol_avg20 else 1.0

        # IV rank proxy (30d vs 90d realized vol)
        def rv(rets): return math.sqrt(sum(x**2 for x in rets)/len(rets)*252)*100 if rets else 20
        # Guarding C[i-1]>0 alone isn't enough -- a NaN Close at C[i] itself
        # (yfinance data gap) still passes that check and produces
        # math.log(NaN) = NaN, which then propagates through rv30/rv90 and
        # crashed round() below with "cannot convert float NaN to integer"
        # for symbols with any NaN bar in the lookback window (EBAY, FCX,
        # ABT, AVGO confirmed affected). Require C[i] itself finite too.
        rets30 = [math.log(C[i]/C[i-1]) for i in range(n-29,n+1) if C[i-1]>0 and math.isfinite(C[i]) and C[i]>0]
        rets90 = [math.log(C[i]/C[i-1]) for i in range(max(1,n-89),n+1) if C[i-1]>0 and math.isfinite(C[i]) and C[i]>0]
        rv30 = rv(rets30); rv90 = rv(rets90)
        _iv_rank_raw = (rv30/rv90)*50 if rv90 else None
        iv_rank = min(100, max(0, round(_iv_rank_raw))) if _iv_rank_raw is not None and math.isfinite(_iv_rank_raw) else 40

        # Trend
        s20 = (ema20[n]-ema20[max(0,n-5)])/5
        s50 = (ema50[n]-ema50[max(0,n-10)])/10
        if ema20[n]>ema50[n] and s20>0 and s50>0:    trend="UPTREND"
        elif ema20[n]<ema50[n] and s20<0 and s50<0:  trend="DOWNTREND"
        elif abs(s20)<0.05*C[n]/100:                 trend="SIDEWAYS"
        elif ema20[n]>ema50[n]:                      trend="MILD_UP"
        else:                                         trend="MILD_DOWN"

        # Momentum
        diff = rsi14[n] - ema90rsi[n]
        if diff>=20:   mom="OVERBOUGHT"
        elif diff<=-20: mom="OVERSOLD"
        elif diff>=10: mom="ELEVATED"
        elif diff<=-10: mom="DEPRESSED"
        else:          mom="NEUTRAL"

        # MACD signal
        macd_sig = "BULLISH" if histogram[n]>0 and histogram[n]>histogram[n-1] else \
                   "BEARISH" if histogram[n]<0 and histogram[n]<histogram[n-1] else "NEUTRAL"

        # Price action signals
        pa_signals = []
        if C[n] > ema20[n] > ema50[n]: pa_signals.append("Price above EMA20/50 (bullish structure)")
        if C[n] < ema20[n] < ema50[n]: pa_signals.append("Price below EMA20/50 (bearish structure)")
        if bb_pct and bb_pct > 85:     pa_signals.append(f"BB%B={bb_pct:.0f}% (near upper band, overbought)")
        if bb_pct and bb_pct < 15:     pa_signals.append(f"BB%B={bb_pct:.0f}% (near lower band, oversold)")
        if vol_ratio > 1.5:            pa_signals.append(f"Volume surge {vol_ratio:.1f}x avg (strong conviction)")
        if vol_ratio < 0.5:            pa_signals.append(f"Low volume {vol_ratio:.1f}x avg (weak move)")
        prev5_chg = (C[n]-C[max(0,n-5)])/C[max(0,n-5)]*100 if n>=5 else 0
        if abs(prev5_chg)>5:           pa_signals.append(f"Strong 5-day move {prev5_chg:+.1f}%")

        result = {
            "price":      round(C[n],2),
            "prev_close": round(C[n-1],2) if n>0 else None,
            "chg_pct":    round((C[n]-C[n-1])/C[n-1]*100,2) if n>0 else 0,
            "rsi14":      round(rsi14[n],1),
            "ema90_rsi":  round(ema90rsi[n],1),
            "rsi_ema_diff": round(diff,1),
            "momentum":   mom,
            "macd_hist":  round(histogram[n],4),
            "macd_signal": macd_sig,
            "ema20":      round(ema20[n],2),
            "ema50":      round(ema50[n],2),
            "ema200":     round(ema200[n],2) if ema200[n] else None,
            "atr":        round(atr,2),
            "bb_pct":     round(bb_pct,1) if bb_pct else None,
            "bb_upper":   round(bb_upper,2) if bb_upper else None,
            "bb_lower":   round(bb_lower,2) if bb_lower else None,
            "iv_rank":    iv_rank,
            "iv_est_pct": round(rv30,1),
            "trend":      trend,
            "vol_ratio":  vol_ratio,
            "pa_signals": pa_signals,
            "high52":     round(max(H),2),
            "low52":      round(min(L),2),
            "atr_pct":    round(atr/C[n]*100,2),
        }
        if _set: _set(f"ta_full:{symbol}", result, 900)  # 15 min cache
        return result
    except Exception as e:
        print(f"[ta] {symbol}: {e}")
        return None


# ── OI analysis ───────────────────────────────────────────────────────────────
def _analyse_oi(rows, oi_hist, spot, atr):
    puts  = [(float(r["strike"]), int(r["oi"]), int(r.get("vol") or 0))
             for r in rows if r["type"]=="put"]
    calls = [(float(r["strike"]), int(r["oi"]), int(r.get("vol") or 0))
             for r in rows if r["type"]=="call"]
    puts.sort(key=lambda x: x[0]); calls.sort(key=lambda x: x[0])

    all_s = sorted({s for s,_,_ in puts+calls})
    interval = max(1.0, sorted([all_s[i+1]-all_s[i] for i in range(len(all_s)-1)]
                               )[len(all_s)//2]) if len(all_s)>1 else 1.0

    puts_below  = [(s,oi,v) for s,oi,v in puts  if s<=spot]
    calls_above = [(s,oi,v) for s,oi,v in calls if s>=spot]

    # Top OI walls
    top_put_walls  = sorted(puts_below,  key=lambda x:-x[1])[:5]
    top_call_walls = sorted(calls_above, key=lambda x:-x[1])[:5]

    support    = top_put_walls[0][0]  if top_put_walls  else round(spot*0.98,2)
    resistance = top_call_walls[0][0] if top_call_walls else round(spot*1.02,2)

    # OI change (from history)
    oi_changes = {}  # (type,strike) → delta from oldest to latest
    for (t,s), history in oi_hist.items():
        if len(history) >= 2:
            delta = history[-1][1] - history[0][1]
            oi_changes[(t,s)] = delta

    # Strikes with biggest OI buildup
    put_buildups  = sorted([(s,d) for (t,s),d in oi_changes.items() if t=="put"  and d>0], key=lambda x:-x[1])[:3]
    call_buildups = sorted([(s,d) for (t,s),d in oi_changes.items() if t=="call" and d>0], key=lambda x:-x[1])[:3]

    # Total OI
    total_put_oi  = sum(oi for _,oi,_ in puts)
    total_call_oi = sum(oi for _,oi,_ in calls)
    pcr = round(total_put_oi/total_call_oi,3) if total_call_oi else None

    # Gamma wall (highest combined OI near spot)
    near = [(s,oi) for s,oi,_ in puts+calls if abs(s-spot)/max(spot,1)<0.05]
    gamma_wall = max(near, key=lambda x:x[1])[0] if near else spot

    return {
        "interval": interval,
        "support": support,
        "resistance": resistance,
        "gamma_wall": round(gamma_wall,2),
        "top_put_walls":  [(s,oi) for s,oi,_ in top_put_walls],
        "top_call_walls": [(s,oi) for s,oi,_ in top_call_walls],
        "put_buildups":   put_buildups,
        "call_buildups":  call_buildups,
        "pcr": pcr,
        "total_put_oi":  total_put_oi,
        "total_call_oi": total_call_oi,
    }


# ── Strategy selection & scoring ──────────────────────────────────────────────
def _pop_credit(dist_pct):
    """PoP proxy for credit spread by % OTM of short strike."""
    return min(90, max(50, round(65 + dist_pct * 3.4)))

def _pop_debit(width, debit):
    return round(max(35, min(80, (width-debit)/width*100))) if width>0 else 50

def _dte(expiry):
    try: return (datetime.strptime(expiry,"%Y-%m-%d").date()-date.today()).days
    except: return 0


def _build_strategies(symbol, expiry, ta, oi, pcr_hist, requested_type=None):
    """
    Score and build strategies. requested_type=None means auto-select best.
    Returns list of strategy dicts with full rationale + PoP + R:R.
    """
    spot  = ta["price"]
    diff  = ta["rsi_ema_diff"]
    mom   = ta["momentum"]
    trend = ta["trend"]
    ivr   = ta["iv_rank"]
    macd  = ta["macd_signal"]
    atr   = ta["atr"]
    bbp   = ta["bb_pct"] or 50
    dte   = _dte(expiry)

    support    = oi["support"]
    resistance = oi["resistance"]
    interval   = oi["interval"]
    pcr        = oi["pcr"]
    gamma_wall = oi["gamma_wall"]
    put_build  = oi["put_buildups"]
    call_build = oi["call_buildups"]

    # Wing width = max(interval, 0.8 × ATR), snapped to interval
    raw_wing = max(atr*0.8, interval)
    wing = max(interval, round(raw_wing/interval)*interval) if interval>0 else max(atr,1)

    # PCR trend (rising = more puts = bearish hedging)
    pcr_rising = (len(pcr_hist)>=2 and pcr_hist[-1]["pcr"] and pcr_hist[-2]["pcr"]
                  and pcr_hist[-1]["pcr"] > pcr_hist[-2]["pcr"]) if pcr_hist else False

    strategies = []

    def _rationale_base():
        return (f"Trend:{trend} | Momentum:{mom} (RSI14-EMA90={diff:+.1f}) | "
                f"MACD:{macd} | IVR:{ivr}/100 | BB%B:{bbp:.0f}% | "
                f"PCR:{pcr:.3f}{'↑' if pcr_rising else '↓' if pcr_hist else ''} | DTE:{dte}")

    # ── 1. Bull Put Credit Spread ─────────────────────────────────────────
    bull_score = 0
    if mom not in ("OVERBOUGHT","ELEVATED"):  bull_score += 20
    if trend in ("UPTREND","MILD_UP"):        bull_score += 20
    if macd == "BULLISH":                     bull_score += 15
    if pcr and pcr > 1.0:                     bull_score += 10  # high PCR = oversold
    if bbp < 40:                              bull_score += 10  # below mid-band
    if diff <= -10:                           bull_score += 15  # oversold bias
    if put_build:                             bull_score += 10  # put OI building
    if 5 <= dte <= 21:                        bull_score += 10  # sweet DTE
    if ivr >= 40:                             bull_score += 10  # decent premium

    sp = support; bp = round(sp - wing, 2)
    dist_pct_bull = round(abs(spot-sp)/spot*100,2) if spot else 2
    cr_bull = round(wing*0.40, 2); ml_bull = round(wing-cr_bull, 2)
    rr_bull = round(cr_bull/ml_bull,2) if ml_bull>0 else 0
    pop_bull = _pop_credit(dist_pct_bull)

    strategies.append({
        "name": "Bull Put Spread", "type": "credit", "bias": "BULLISH",
        "legs": f"Sell ${sp}P  /  Buy ${bp}P",
        "expiry": expiry, "dte": dte,
        "est_credit": f"~${cr_bull}", "max_gain": f"~${cr_bull}/contract",
        "max_loss": f"~${ml_bull}/contract",
        "rr": f"{rr_bull}:1", "pop": pop_bull,
        "score": bull_score,
        "grade": "A" if bull_score>=65 else "B" if bull_score>=45 else "C",
        "rationale": (
            f"Put wall at ${sp} ({dist_pct_bull}% below spot) anchors support. "
            + (f"OI buildup at ${put_build[0][0]} (+{put_build[0][1]:,}). " if put_build else "")
            + _rationale_base()
        ),
        "manage": f"Close at 50% credit (~${round(cr_bull*0.5,2)}). Stop if spot closes below ${bp}.",
    })

    # ── 2. Bear Call Credit Spread ────────────────────────────────────────
    bear_score = 0
    if mom not in ("OVERSOLD","DEPRESSED"):   bear_score += 20
    if trend in ("DOWNTREND","MILD_DOWN"):    bear_score += 20
    if macd == "BEARISH":                     bear_score += 15
    if pcr and pcr < 0.8:                     bear_score += 10  # low PCR = complacent
    if bbp > 65:                              bear_score += 10  # above mid-band
    if diff >= 10:                            bear_score += 15  # overbought bias
    if call_build:                            bear_score += 10  # call OI building
    if 5 <= dte <= 21:                        bear_score += 10
    if ivr >= 40:                             bear_score += 10

    sc = resistance; bc = round(sc + wing, 2)
    dist_pct_bear = round(abs(sc-spot)/spot*100,2) if spot else 2
    cr_bear = round(wing*0.40, 2); ml_bear = round(wing-cr_bear, 2)
    rr_bear = round(cr_bear/ml_bear,2) if ml_bear>0 else 0
    pop_bear = _pop_credit(dist_pct_bear)

    strategies.append({
        "name": "Bear Call Spread", "type": "credit", "bias": "BEARISH",
        "legs": f"Sell ${sc}C  /  Buy ${bc}C",
        "expiry": expiry, "dte": dte,
        "est_credit": f"~${cr_bear}", "max_gain": f"~${cr_bear}/contract",
        "max_loss": f"~${ml_bear}/contract",
        "rr": f"{rr_bear}:1", "pop": pop_bear,
        "score": bear_score,
        "grade": "A" if bear_score>=65 else "B" if bear_score>=45 else "C",
        "rationale": (
            f"Call wall at ${sc} ({dist_pct_bear}% above spot) caps upside. "
            + (f"OI buildup at ${call_build[0][0]} (+{call_build[0][1]:,}). " if call_build else "")
            + _rationale_base()
        ),
        "manage": f"Close at 50% credit (~${round(cr_bear*0.5,2)}). Stop if spot closes above ${bc}.",
    })

    # ── 3. Iron Condor ────────────────────────────────────────────────────
    ic_score = 0
    if mom in ("NEUTRAL","ELEVATED","DEPRESSED"):  ic_score += 25
    if trend in ("SIDEWAYS","MILD_UP","MILD_DOWN"): ic_score += 20
    if ivr >= 50:                                  ic_score += 25
    if pcr and 0.8 <= pcr <= 1.3:                 ic_score += 15
    if 40 <= bbp <= 65:                            ic_score += 10
    if 7 <= dte <= 30:                             ic_score += 10
    if abs(diff) < 15:                             ic_score += 10

    cr_ic = round((cr_bull + cr_bear)*0.9, 2)  # slight discount for IC
    ml_ic = round(wing - cr_ic/2, 2)
    rr_ic = round(cr_ic/ml_ic,2) if ml_ic>0 else 0
    pop_ic = round((_pop_credit(dist_pct_bull)+_pop_credit(dist_pct_bear))/2)
    width_ic = round(resistance-support,2)

    strategies.append({
        "name": "Iron Condor", "type": "credit", "bias": "NEUTRAL",
        "legs": f"Sell ${sp}P/Buy ${bp}P  ·  Sell ${sc}C/Buy ${bc}C",
        "expiry": expiry, "dte": dte,
        "est_credit": f"~${cr_ic}", "max_gain": f"~${cr_ic}/contract",
        "max_loss": f"~${ml_ic}/contract",
        "rr": f"{rr_ic}:1", "pop": pop_ic,
        "score": ic_score,
        "grade": "A" if ic_score>=65 else "B" if ic_score>=45 else "C",
        "rationale": (
            f"OI walls bracket spot: support ${sp} / resistance ${sc} (width {width_ic} pts). "
            f"Gamma wall at ${gamma_wall}. " + _rationale_base()
        ),
        "manage": f"Close at 50% max profit. Roll/close if either short strike breached.",
    })

    # ── 4. Bull Call Debit Spread (low IV, strong uptrend) ────────────────
    bc_score = 0
    if trend == "UPTREND":                    bc_score += 30
    if macd == "BULLISH":                     bc_score += 20
    if mom in ("NEUTRAL","DEPRESSED"):        bc_score += 15
    if ivr <= 35:                             bc_score += 20  # low IV = buy premium
    if bbp < 30:                              bc_score += 10
    if diff <= 0:                             bc_score += 10
    if 10 <= dte <= 45:                       bc_score += 10

    atm_call = min(calls_above := [(s,oi) for s,oi,_ in
                   [(float(r["strike"]),int(r["oi"]),0)
                    for r in [] ] ], default=(resistance, 0))[0] \
               if False else round(spot, -int(math.log10(interval)) if interval>1 else 0)
    bc_buy = round(spot / interval) * interval  # nearest ATM strike
    bc_sell = round(bc_buy + wing, 2)
    debit_bc = round(wing*0.45, 2); mg_bc = round(wing-debit_bc, 2)
    rr_bc = round(mg_bc/debit_bc,2) if debit_bc>0 else 0
    pop_bc = _pop_debit(wing, debit_bc)

    strategies.append({
        "name": "Bull Call Spread", "type": "debit", "bias": "BULLISH",
        "legs": f"Buy ${bc_buy}C  /  Sell ${bc_sell}C",
        "expiry": expiry, "dte": dte,
        "est_credit": f"~${debit_bc} debit", "max_gain": f"~${mg_bc}/contract",
        "max_loss": f"~${debit_bc}/contract (full debit)",
        "rr": f"{rr_bc}:1", "pop": pop_bc,
        "score": bc_score,
        "grade": "A" if bc_score>=65 else "B" if bc_score>=45 else "C",
        "rationale": (
            f"Directional bull play — low IV ({ivr}/100) favors buying premium. "
            f"Target: ${bc_sell} by expiry. " + _rationale_base()
        ),
        "manage": f"Take 60% of max gain. Stop at 50% debit loss.",
    })

    # ── 5. Bear Put Debit Spread ──────────────────────────────────────────
    bp_score = 0
    if trend == "DOWNTREND":                  bp_score += 30
    if macd == "BEARISH":                     bp_score += 20
    if mom in ("NEUTRAL","ELEVATED"):         bp_score += 15
    if ivr <= 35:                             bp_score += 20
    if bbp > 70:                              bp_score += 10
    if diff >= 0:                             bp_score += 10
    if 10 <= dte <= 45:                       bp_score += 10

    bp_buy  = round(spot / interval) * interval
    bp_sell = round(bp_buy - wing, 2)
    debit_bp = round(wing*0.45,2); mg_bp = round(wing-debit_bp,2)
    rr_bp = round(mg_bp/debit_bp,2) if debit_bp>0 else 0
    pop_bp = _pop_debit(wing, debit_bp)

    strategies.append({
        "name": "Bear Put Spread", "type": "debit", "bias": "BEARISH",
        "legs": f"Buy ${bp_buy}P  /  Sell ${bp_sell}P",
        "expiry": expiry, "dte": dte,
        "est_credit": f"~${debit_bp} debit", "max_gain": f"~${mg_bp}/contract",
        "max_loss": f"~${debit_bp}/contract (full debit)",
        "rr": f"{rr_bp}:1", "pop": pop_bp,
        "score": bp_score,
        "grade": "A" if bp_score>=65 else "B" if bp_score>=45 else "C",
        "rationale": (
            f"Directional bear play — low IV ({ivr}/100) favors buying premium. "
            f"Target: ${bp_sell} by expiry. " + _rationale_base()
        ),
        "manage": f"Take 60% of max gain. Stop at 50% debit loss.",
    })

    # ── 6. Short Strangle (very high IV, neutral) ─────────────────────────
    if ivr >= 60:
        ss_score = 0
        if ivr >= 70:                         ss_score += 30
        if mom in ("NEUTRAL","ELEVATED","DEPRESSED"): ss_score += 20
        if trend in ("SIDEWAYS","MILD_UP","MILD_DOWN"): ss_score += 20
        if pcr and 0.7 <= pcr <= 1.4:        ss_score += 15
        if 7 <= dte <= 21:                   ss_score += 15

        ot_put  = round(support - interval, 2)
        ot_call = round(resistance + interval, 2)
        cr_ss = round(wing*0.55,2)
        dp    = round(abs(spot-ot_put)/spot*100,2)
        dc    = round(abs(ot_call-spot)/spot*100,2)
        pop_ss = round((_pop_credit(dp)+_pop_credit(dc))/2)

        strategies.append({
            "name": "Short Strangle", "type": "credit", "bias": "NEUTRAL",
            "legs": f"Sell ${ot_put}P  ·  Sell ${ot_call}C",
            "expiry": expiry, "dte": dte,
            "est_credit": f"~${cr_ss}", "max_gain": f"~${cr_ss}/contract",
            "max_loss": "Substantial — use stop at 2× credit",
            "rr": "High PoP / undefined risk", "pop": pop_ss,
            "score": ss_score,
            "grade": "A" if ss_score>=65 else "B" if ss_score>=45 else "C",
            "rationale": (
                f"IV Rank {ivr}/100 — sell elevated premium OTM both sides. "
                f"Puts at ${ot_put}, calls at ${ot_call}. " + _rationale_base()
            ),
            "manage": "Close at 50% credit. Stop at 2× credit received.",
        })

    # ── Sort and filter by requested type ────────────────────────────────
    strategies.sort(key=lambda x: -x["score"])

    type_map = {
        "bull_put":    lambda s: s["name"]=="Bull Put Spread",
        "bear_call":   lambda s: s["name"]=="Bear Call Spread",
        "condor":      lambda s: s["name"]=="Iron Condor",
        "bull_call":   lambda s: s["name"]=="Bull Call Spread",
        "bear_put":    lambda s: s["name"]=="Bear Put Spread",
        "strangle":    lambda s: s["name"]=="Short Strangle",
    }
    if requested_type and requested_type in type_map:
        strategies = [s for s in strategies if type_map[requested_type](s)]
    # Auto: return top 3 by score
    elif not requested_type:
        strategies = strategies[:3]

    return strategies


# ── Main analysis endpoint ────────────────────────────────────────────────────
def _full_analysis(symbol, expiry, requested_type=None, prefer_live_prices=False):
    con = _conn()
    rows    = _oi_latest(symbol, expiry, con)
    oi_hist = _oi_history(symbol, expiry, con, days=7)
    pcr_hist= _pcr_history(symbol, con, days=5)
    pcr_exp = _pcr_by_expiry(symbol, con)
    con.close()

    if not rows:
        # Try to synthesize walls from the hardened option-price service. It is
        # local-DB first and safely handles yfinance NaN values when it falls back.
        try:
            from ..services.option_prices import fetch_chain
            ch = fetch_chain(symbol, expiry)
            rows = []
            for (side, strike), data in (ch or {}).items():
                if side not in ("call", "put"):
                    continue
                try:
                    oi = int(float(data.get("oi") or 0))
                except Exception:
                    oi = 0
                try:
                    vol = int(float(data.get("volume") or 0))
                except Exception:
                    vol = 0
                if oi > 0:
                    rows.append({"type": side, "strike": float(strike), "oi": oi, "volume": vol})
        except Exception:
            pass

    spot = get_spot(symbol)
    # If still no OI, build synthetic walls from spot ± 3%
    if not rows and spot:
        rows = [
            {"type":"put", "strike":round(spot*0.97,2),"oi":1000,"volume":0},
            {"type":"call","strike":round(spot*1.03,2),"oi":1000,"volume":0},
        ]
    if not rows and not spot:
        return {"error": f"No data available for {symbol}. Check symbol and run scheduler."}
    if not spot:
        all_s = sorted({float(r["strike"]) for r in rows})
        spot  = all_s[len(all_s)//2] if all_s else 100

    ta  = _compute_ta(symbol)
    if not ta:
        # Minimal TA from spot alone
        ta = {
            "price":round(spot,2),"prev_close":None,"chg_pct":0,
            "rsi14":50,"ema90_rsi":50,"rsi_ema_diff":0,"momentum":"NEUTRAL",
            "macd_hist":0,"macd_signal":"NEUTRAL","ema20":round(spot,2),
            "ema50":round(spot,2),"ema200":None,"atr":round(spot*0.01,2),
            "bb_pct":50,"bb_upper":round(spot*1.02,2),"bb_lower":round(spot*0.98,2),
            "iv_rank":40,"iv_est_pct":25,"trend":"SIDEWAYS","vol_ratio":1.0,
            "pa_signals":["TA unavailable — yfinance blocked"],"high52":None,"low52":None,"atr_pct":1.0,
        }

    ta["price"] = round(spot, 2)  # always use live spot
    oi = _analyse_oi(rows, oi_hist, spot, ta["atr"])

    # OI change summary for display
    oi_change_summary = []
    for (t, s), hist in sorted(oi_hist.items(), key=lambda x: -(x[1][-1][1]-x[1][0][1]) if len(x[1])>=2 else 0)[:8]:
        if len(hist) >= 2:
            delta = hist[-1][1] - hist[0][1]
            if abs(delta) > 500:
                oi_change_summary.append({
                    "type": t, "strike": s,
                    "oi_latest": hist[-1][1], "oi_oldest": hist[0][1],
                    "delta": delta,
                    "dates": f"{hist[0][0]} → {hist[-1][0]}",
                })
    oi_change_summary.sort(key=lambda x: -abs(x["delta"]))

    strategies = _build_strategies(symbol, expiry, ta, oi, pcr_hist, requested_type)

    # Enrich with option bid/ask prices. Journal/alert callers pass
    # prefer_live_prices=True so position health uses live marks first, then DB fallback.
    try:
        try:
            strategies = _enrich(symbol, expiry, strategies, prefer_live=bool(prefer_live_prices))
        except TypeError:
            strategies = _enrich(symbol, expiry, strategies)
    except Exception as _e:
        print(f"[enrich] {_e}")
        for s in strategies:
            s.setdefault("price_source", "estimated")
            s.setdefault("real_prices", False)

    return {
        "symbol": symbol, "expiry": expiry, "spot": round(spot,2),
        "ta": ta,
        "oi_summary": {
            "support": oi["support"], "resistance": oi["resistance"],
            "gamma_wall": oi["gamma_wall"], "interval": oi["interval"],
            "pcr": oi["pcr"], "top_put_walls": oi["top_put_walls"],
            "top_call_walls": oi["top_call_walls"],
            "put_buildups": oi["put_buildups"], "call_buildups": oi["call_buildups"],
            "total_put_oi": oi["total_put_oi"], "total_call_oi": oi["total_call_oi"],
        },
        "oi_change_history": oi_change_summary,
        "pcr_history": pcr_hist,
        "pcr_by_expiry": pcr_exp,
        "strategies": strategies,
    }


# ── Legacy helpers (keep scanner tabs working) ────────────────────────────────
def _split_puts_calls(rows):
    puts  = sorted([(float(r["strike"]), int(r["oi"])) for r in rows if r["type"]=="put"],  key=lambda x:x[0])
    calls = sorted([(float(r["strike"]), int(r["oi"])) for r in rows if r["type"]=="call"], key=lambda x:x[0])
    return puts, calls

def _strike_interval(all_strikes):
    if len(all_strikes)<2: return 1.0
    gaps=[all_strikes[i+1]-all_strikes[i] for i in range(len(all_strikes)-1)]
    return round(sorted(gaps)[len(gaps)//2],2)

def _scan_condors_db(expiry):
    today=_today()
    if expiry<today: return []
    con=_conn()
    syms=[r[0] for r in con.execute("SELECT DISTINCT symbol FROM options WHERE expiration=? AND expiration>=?",(expiry,today)).fetchall()]
    dte=_dte(expiry); results=[]
    for sym in syms:
        rows=_oi_latest(sym,expiry,con)
        puts,calls=_split_puts_calls(rows)
        if not puts or not calls: continue
        spot=get_spot(sym)
        if spot is None:
            all_s=sorted({s for s,_ in puts+calls}); spot=all_s[len(all_s)//2]
        interval=_strike_interval(sorted({s for s,_ in puts+calls}))
        puts_below=[(s,oi) for s,oi in puts if s<=spot]; calls_above=[(s,oi) for s,oi in calls if s>=spot]
        if not puts_below or not calls_above: continue
        pw_s,pw_oi=max(puts_below,key=lambda x:x[1]); cw_s,cw_oi=max(calls_above,key=lambda x:x[1])
        w=3*interval
        np_oi=sum(oi for s,oi in puts if abs(s-spot)<=w); nc_oi=sum(oi for s,oi in calls if abs(s-spot)<=w)
        if not np_oi or not nc_oi: continue
        symmetry=round(min(np_oi,nc_oi)/max(np_oi,nc_oi),2)
        if symmetry<0.4: continue
        score=min(100,int(symmetry*40)+(20 if 7<=dte<=21 else 10)+min(20,int(pw_oi/500))+min(20,int(cw_oi/500)))
        results.append({"symbol":sym,"expiration":expiry,"dte":dte,"spot":round(spot,2),
            "put_wall_strike":pw_s,"put_wall_oi":pw_oi,"call_wall_strike":cw_s,"call_wall_oi":cw_oi,
            "range_width":round(cw_s-pw_s,2),"symmetry":symmetry,
            "grade":"A" if score>=75 else "B" if score>=50 else "C","signal_score":score,
            "suggested_spread":f"Sell {pw_s}P/Buy {round(pw_s-interval,1)}P  |  Sell {cw_s}C/Buy {round(cw_s+interval,1)}C"})
    con.close(); results.sort(key=lambda x:-x["signal_score"]); return results

def _scan_verticals_db(expiry,dist=2):
    today=_today()
    if expiry<today: return []
    con=_conn()
    syms=[r[0] for r in con.execute("SELECT DISTINCT symbol FROM options WHERE expiration=? AND expiration>=?",(expiry,today)).fetchall()]
    dte=_dte(expiry); results=[]
    for sym in syms:
        rows=_oi_latest(sym,expiry,con); puts,calls=_split_puts_calls(rows)
        if not puts or not calls: continue
        spot=get_spot(sym)
        if spot is None:
            all_s=sorted({s for s,_ in puts+calls}); spot=all_s[len(all_s)//2]
        interval=_strike_interval(sorted({s for s,_ in puts+calls}))
        total_put_oi=sum(oi for _,oi in puts); total_call_oi=sum(oi for _,oi in calls)
        pcr=round(total_put_oi/total_call_oi,3) if total_call_oi else None
        puts_below=[(s,oi) for s,oi in puts if s<=spot]
        if puts_below:
            pw_s,pw_oi=max(puts_below,key=lambda x:x[1])
            if abs(spot-pw_s)<=dist*interval and pw_oi>500:
                score=min(100,min(30,int(pw_oi/333))+(20 if 5<=dte<=21 else 10)+15)
                results.append({"strategy":"Bull Put Spread","symbol":sym,"expiration":expiry,"dte":dte,
                    "spot":round(spot,2),"sell_strike":pw_s,"buy_strike":round(pw_s-interval,2),
                    "wall_oi":pw_oi,"strike_dist_pct":round(abs(spot-pw_s)/spot*100,2) if spot else None,
                    "full_chain_pcr":pcr,"grade":"A" if score>=75 else "B" if score>=50 else "C",
                    "signal_score":score,"suggested_trade":f"Sell {pw_s}P / Buy {round(pw_s-interval,2)}P  exp {expiry}",
                    "bias":"Bullish – put wall support"})
        calls_above=[(s,oi) for s,oi in calls if s>=spot]
        if calls_above:
            cw_s,cw_oi=max(calls_above,key=lambda x:x[1])
            if abs(cw_s-spot)<=dist*interval and cw_oi>500:
                score=min(100,min(30,int(cw_oi/333))+(20 if 5<=dte<=21 else 10)+15)
                results.append({"strategy":"Bear Call Spread","symbol":sym,"expiration":expiry,"dte":dte,
                    "spot":round(spot,2),"sell_strike":cw_s,"buy_strike":round(cw_s+interval,2),
                    "wall_oi":cw_oi,"strike_dist_pct":round(abs(cw_s-spot)/spot*100,2) if spot else None,
                    "full_chain_pcr":pcr,"grade":"A" if score>=75 else "B" if score>=50 else "C",
                    "signal_score":score,"suggested_trade":f"Sell {cw_s}C / Buy {round(cw_s+interval,2)}C  exp {expiry}",
                    "bias":"Bearish – call wall resistance"})
    con.close(); results.sort(key=lambda x:-x["signal_score"]); return results


# ── Flask routes ──────────────────────────────────────────────────────────────
@strategy_bp.route("/symbols")
def strat_symbols():
    return jsonify({"symbols": _all_symbols()})

@strategy_bp.route("/future_expirations")
def future_expirations():
    sym = (request.args.get("symbol") or "SPY").upper()
    return jsonify({"symbol": sym, "expirations": _future_expirations(sym)})

@strategy_bp.route("/analyze")
def analyze_strategy():
    sym    = (request.args.get("symbol") or "").upper().strip()
    expiry = request.args.get("expiry","").strip()
    stype  = request.args.get("type","").strip() or None
    min_dte= request.args.get("min_dte",21,type=int)
    max_dte= request.args.get("max_dte",45,type=int)
    sector = request.args.get("sector","").strip()

    # If no symbol — run across all watchlist symbols and return list
    if not sym or sym == "ALL":
        from ..db import get_symbols as _get_syms
        import concurrent.futures
        syms = _get_syms()
        if sector:
            try:
                from ..services.sector_service import get_symbol_sector
                syms = [s for s in syms if get_symbol_sector(s) == sector]
            except: pass
        # For multi-symbol: find suitable expiry per symbol
        def _one(s):
            try:
                exp = _pick_expiry(s, min_dte, max_dte)
                if not exp: return None
                result = _full_analysis(s, exp, stype)
                result["symbol"] = s
                return result
            except: return None
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=10)
        try:
            results = list(ex.map(_one, syms[:30], timeout=60))
        except concurrent.futures.TimeoutError:
            results = []
            print("[routes_strategy] multi-symbol analysis timed out")
        finally:
            ex.shutdown(wait=False)
        results = [r for r in results if r and not r.get("error")]
        return jsonify({"multi": True, "results": results, "count": len(results)})

    # Single symbol — if no expiry pick best DTE window
    if not expiry:
        expiry = _pick_expiry(sym, min_dte, max_dte)
        if not expiry:
            return jsonify({"error": f"No expiry found in {min_dte}–{max_dte} DTE range for {sym}"}), 400
    if expiry < _today():
        return jsonify({"error": "Expiry is in the past"}), 400
    return jsonify(_full_analysis(sym, expiry, stype))


def _pick_expiry(symbol, min_dte=21, max_dte=45):
    """
    Pick best expiry in DTE range from DB then yfinance.
    Progressively widens the range if nothing found in exact window.
    """
    from datetime import date, datetime
    today = date.today()

    def _find_in_list(exps, lo, hi):
        candidates = []
        for exp in sorted(exps):
            try:
                d = (datetime.strptime(str(exp)[:10],"%Y-%m-%d").date()-today).days
                if lo <= d <= hi:
                    candidates.append((d, exp))
            except: pass
        # Return the expiry closest to the midpoint of the range
        if candidates:
            mid = (lo+hi)/2
            return min(candidates, key=lambda x: abs(x[0]-mid))[1]
        return None

    all_exps = []
    # Try DB first
    try:
        from ..db import get_expirations_for_symbol
        all_exps = get_expirations_for_symbol(symbol)
    except: pass

    # Try yfinance
    yf_exps = []
    try:
        import yfinance as yf
        yf_exps = list(yf.Ticker(symbol).options or [])
    except: pass

    combined = sorted(set(list(all_exps) + yf_exps))

    # Try progressively wider windows
    for lo, hi in [(min_dte, max_dte), (14, 60), (7, 90), (1, 180)]:
        exp = _find_in_list(combined, lo, hi)
        if exp: return exp

    # Last resort: use first future expiry from combined list
    if combined:
        future = [e for e in combined
                  if datetime.strptime(str(e)[:10], "%Y-%m-%d").date() >= today]
        if future: return future[0]

    # Absolute fallback: next monthly expiry Friday ~DTE midpoint from now
    from datetime import timedelta
    target = today + timedelta(days=max(min_dte, (min_dte+max_dte)//2))
    # Roll to nearest Friday
    while target.weekday() != 4:
        target += timedelta(days=1)
    return target.isoformat()

@strategy_bp.route("/scan_condors")
def scan_condors():
    expiry = request.args.get("expiry","")
    if not expiry: return jsonify({"error":"expiry required"}),400
    return jsonify(_scan_condors_db(expiry))

@strategy_bp.route("/scan_verticals")
def scan_verticals():
    expiry = request.args.get("expiry","")
    dist   = request.args.get("distance",2,type=int)
    if not expiry: return jsonify({"error":"expiry required"}),400
    return jsonify(_scan_verticals_db(expiry,dist))


# ─── Live prices endpoints ─────────────────────────────────────────────────────

@strategy_bp.route("/live_prices")
def live_prices():
    """
    Fetch real bid/ask/mid prices for an options chain from yfinance.
    Finds the nearest available yfinance expiry to the requested date.
    """
    symbol = (request.args.get("symbol") or "SPY").upper()
    expiry = request.args.get("expiry", "").strip()
    if not expiry:
        return jsonify({"error": "expiry required"}), 400

    import yfinance as yf, math
    from datetime import datetime

    def _f(v, dec=2):
        """Safe float conversion — returns None for NaN/inf/None."""
        try:
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else round(f, dec)
        except:
            return None

    def _i(v):
        """Safe int conversion — returns 0 for NaN/None."""
        try:
            f = float(v)
            return 0 if (math.isnan(f) or math.isinf(f)) else int(f)
        except:
            return 0

    try:
        tk        = yf.Ticker(symbol)
        available = list(tk.options or [])
        if not available:
            return jsonify({"error": f"No options expirations for {symbol}"}), 500

        # Snap to nearest available expiry
        req_dt  = datetime.strptime(expiry, "%Y-%m-%d")
        nearest = min(available,
                      key=lambda e: abs((datetime.strptime(e, "%Y-%m-%d") - req_dt).days))
        chain   = tk.option_chain(nearest)

        result = {}
        for side, df in [("call", chain.calls), ("put", chain.puts)]:
            for _, row in df.iterrows():
                strike = _f(row.get("strike"))
                if strike is None:
                    continue

                bid  = _f(row.get("bid"))
                ask  = _f(row.get("ask"))
                last = _f(row.get("lastPrice"))
                # Mid = (bid+ask)/2 when both valid, else last price
                if bid and ask and bid > 0 and ask > 0:
                    mid = round((bid + ask) / 2, 2)
                elif last and last > 0:
                    mid = last
                else:
                    mid = None

                data_val = {
                    "bid":    bid,
                    "ask":    ask,
                    "mid":    mid,
                    "iv":     _f(row.get("impliedVolatility")),
                    "oi":     _i(row.get("openInterest")),
                    "volume": _i(row.get("volume")),
                }

                # Store under both int key (JS uses) and float key
                strike_int = int(strike) if strike == int(strike) else strike
                result[f"{side}_{strike_int}"] = data_val   # "call_735"
                result[f"{side}_{strike}"]     = data_val   # "call_735.0"

        return jsonify({
            "symbol":           symbol,
            "requested_expiry": expiry,
            "actual_expiry":    nearest,
            "expiry":           nearest,
            "chain":            result,
            "count":            len(result) // 2,   # divide by 2 since each stored twice
            "source":           "yfinance bid/ask mid-price",
        })

    except Exception as e:
        import traceback
        print(f"[live_prices] {symbol}/{expiry}: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e), "symbol": symbol, "expiry": expiry}), 500


@strategy_bp.route("/test_chain")
def test_chain():
    """Quick connectivity test — returns first available expiry chain for symbol."""
    symbol = (request.args.get("symbol") or "SPY").upper()
    try:
        import yfinance as yf
        tk  = yf.Ticker(symbol)
        exp = (tk.options or [None])[0]
        if not exp:
            return jsonify({"error": "No expirations found"})
        chain = tk.option_chain(exp)
        row   = chain.calls.iloc[len(chain.calls)//2]
        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "expiry": exp,
            "sample_strike": float(row.get("strike", 0)),
            "sample_bid":    float(row.get("bid", 0)),
            "sample_ask":    float(row.get("ask", 0)),
            "sample_mid":    round((float(row.get("bid",0)) + float(row.get("ask",0)))/2, 2),
        })
    except Exception as e:
        return jsonify({"error": str(e), "status": "failed"})


def _compute_strategy_score(pop_pct, rr_ratio, theta, dte, is_credit, delta, vega):
    """The Trade Analyzer's 100-point weighted strategy score. Exact port
    of the client-side JS version that used to compute this in the
    browser (formerly inside _taShowStockInfo in app.js) -- moved
    server-side so the specific point weights and thresholds (the actual
    trading judgment calibration) aren't readable via browser dev tools.
    The underlying options math (Black-Scholes pricing, PoP, R:R, Greeks)
    stays client-side since that's public, standard finance math with no
    real secrecy value, and keeping it client-side is what lets the
    Trade Analyzer's sliders feel instant instead of round-tripping to
    the server on every drag -- only the SCORE (this function) moved,
    not the pricing engine that feeds it.

    Every weight/threshold below is identical to the removed JS version:
    PoP 30pts, R:R 20pts, theta 15pts, DTE sweet spot 10pts, credit/debit
    10pts, delta neutrality 10pts, vega 5pts = 100pts max.
    """
    score = 0
    notes = []

    # PoP (max 30 pts)
    if pop_pct >= 70:
        score += 30; notes.append(f"PoP {pop_pct:.0f}% excellent")
    elif pop_pct >= 55:
        score += 22; notes.append(f"PoP {pop_pct:.0f}% good")
    elif pop_pct >= 40:
        score += 14; notes.append(f"PoP {pop_pct:.0f}% moderate")
    else:
        score += 6; notes.append(f"PoP {pop_pct:.0f}% low")

    # R:R ratio (max 20 pts)
    if rr_ratio >= 2.0:
        score += 20; notes.append(f"R:R {rr_ratio:.1f}:1 excellent")
    elif rr_ratio >= 1.0:
        score += 14; notes.append(f"R:R {rr_ratio:.1f}:1 solid")
    elif rr_ratio >= 0.5:
        score += 8; notes.append(f"R:R {rr_ratio:.1f}:1 marginal")
    else:
        score += 3

    # Theta advantage (max 15 pts) -- positive theta = time working for you
    if theta > 0:
        score += 15; notes.append(f"Theta +{theta:.1f} (time decay earns)")
    elif theta > -5:
        score += 8
    else:
        score += 2; notes.append(f"Theta {theta:.1f} (time decay costs)")

    # DTE sweet spot (max 10 pts)
    if 21 <= dte <= 45:
        score += 10; notes.append(f"DTE {dte}d sweet spot")
    elif 14 <= dte <= 60:
        score += 6
    elif dte < 7:
        score += 2; notes.append(f"DTE {dte}d gamma risk")
    else:
        score += 4

    # Credit vs debit (max 10 pts)
    if is_credit:
        score += 10; notes.append("Credit trade \u2713")
    else:
        score += 4

    # Delta neutrality for neutral strategies (max 10 pts)
    abs_delta = abs(delta)
    if abs_delta < 5:
        score += 10; notes.append("Delta neutral \u2713")
    elif abs_delta < 15:
        score += 7
    elif abs_delta < 30:
        score += 4
    else:
        score += 1

    # Vega (max 5 pts) -- negative vega for credit spreads is good
    if is_credit and vega < 0:
        score += 5; notes.append("Short vega \u2713")
    elif not is_credit and vega > 0:
        score += 5
    else:
        score += 2

    score = min(100, max(5, round(score)))
    return score, notes


@strategy_bp.route("/strategy_score", methods=["POST"])
def strategy_score():
    """Takes the already-computed PoP/R:R/Greeks (the client already has
    these from its own Black-Scholes math, run client-side for instant
    slider feedback) and returns the weighted score + notes -- the part
    that's actually worth keeping off the wire as readable JS.
    """
    d = request.get_json(force=True) or {}
    try:
        pop_pct = float(d.get("pop_pct", 0))
        rr_ratio = float(d.get("rr_ratio", 0))
        theta = float(d.get("theta", 0))
        dte = int(float(d.get("dte", 0)))
        is_credit = bool(d.get("is_credit"))
        delta = float(d.get("delta", 0))
        vega = float(d.get("vega", 0))
    except (TypeError, ValueError) as e:
        return jsonify({"error": f"Invalid input: {e}"}), 400

    score, notes = _compute_strategy_score(pop_pct, rr_ratio, theta, dte, is_credit, delta, vega)
    return jsonify({"score": score, "score_notes": notes})

