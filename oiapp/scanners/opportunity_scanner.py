# oiapp/scanners/opportunity_scanner.py  v5
"""
Opportunity Scanner — on-demand 15–45 DTE defined-risk credit scan.

It uses only the newest valid saved option-chain snapshot, cached earnings,
and saved daily price history. A result is emitted only when earnings,
liquidity, real bid/ask prices, implied volatility, expected-move room,
and defined-risk reward/risk gates all pass.

Two regimes:
  TRENDING      — directional bull-put or bear-call credit spread
  MEAN_REVERSION — range-only iron condor or confirmed reversal vertical
"""
import sqlite3, math, time, concurrent.futures
from datetime import date, datetime

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

_ta_cache    = {}
_chain_cache = {}
_earn_cache  = {}   # non-blocking, populated lazily
_TA_TTL      = 900
_CHAIN_TTL   = 300
_EARN_TTL    = 7200  # 2 hrs — earnings dates don't change often

def _ts(): return time.time()
def _cached(c, k, ttl, fn):
    e = c.get(k)
    if e and _ts() < e[1]: return e[0]
    v = fn(); c[k] = (v, _ts() + ttl); return v


# ── DB helpers ────────────────────────────────────────────────────────────
def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

def _today(): return date.today().strftime("%Y-%m-%d")

def _all_symbols():
    try:
        con = _conn()
        rows = con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()
        con.close()
        return [r["symbol"] for r in rows]
    except: return []

def _safe(v, dec=2):
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, dec)
    except: return None


# ── Earnings — lazy, non-blocking, cached ─────────────────────────────────
def _get_earn_days(symbol):
    """Read the cached earnings calendar only. Unknown earnings is unsafe for premium selling."""
    try:
        con = _conn()
        try:
            row = con.execute(
                "SELECT next_earn_date FROM earnings_calendar WHERE symbol=?",
                (symbol.upper().strip(),),
            ).fetchone()
        finally:
            con.close()
        if not row or not row["next_earn_date"]:
            return None, None
        earn_date = datetime.strptime(str(row["next_earn_date"])[:10], "%Y-%m-%d").date()
        return max(0, (earn_date - date.today()).days), earn_date.isoformat()
    except Exception:
        return None, None


# ── Technical Analysis# ── Technical Analysis (ADX + all indicators) ─────────────────────────────
def _compute_ta(symbol):
    cached = _ta_cache.get(symbol)
    if cached and _ts() < cached[1]: return cached[0]
    try:
        con = _conn()
        try:
            rows = con.execute(
                """SELECT date, open, high, low, close, volume
                   FROM price_cache
                   WHERE symbol=? AND close IS NOT NULL AND close>0
                   ORDER BY date DESC LIMIT 120""",
                (symbol.upper().strip(),),
            ).fetchall()
        finally:
            con.close()
        if len(rows) < 90:
            _ta_cache[symbol] = (None, _ts() + _TA_TTL); return None
        rows = list(reversed(rows))
        C=[float(row["close"]) for row in rows]
        H=[float(row["high"] or row["close"]) for row in rows]
        L=[float(row["low"] or row["close"]) for row in rows]
        V=[float(row["volume"] or 0) for row in rows]
        n=len(C)-1

        def ema(a,p):
            k=2/(p+1); o=list(a)
            for i in range(1,len(o)): o[i]=a[i]*k+o[i-1]*(1-k)
            return o

        def rsi_fn(c,p=14):
            o=[50.0]*len(c)
            if len(c)<p+1: return o
            g=l=0.0
            for i in range(1,p+1):
                d=c[i]-c[i-1]
                if d>0: g+=d
                else: l-=d
            ag,al=g/p,l/p
            o[p]=100 if al==0 else 100-100/(1+ag/al)
            for i in range(p+1,len(c)):
                d=c[i]-c[i-1]
                ag=(ag*(p-1)+max(d,0))/p; al=(al*(p-1)+max(-d,0))/p
                o[i]=100 if al==0 else 100-100/(1+ag/al)
            return o

        r14=rsi_fn(C,14); e90=ema(r14,90); e20=ema(C,20); e50=ema(C,50)
        e12=ema(C,12); e26=ema(C,26)
        ml=[a-b for a,b in zip(e12,e26)]; ms=ema(ml,9); mh=[a-b for a,b in zip(ml,ms)]

        tr=[H[i]-L[i] if i==0 else max(H[i]-L[i],abs(H[i]-C[i-1]),abs(L[i]-C[i-1]))
            for i in range(len(C))]
        atr=ema(tr,14)[n]

        sl=C[n-19:n+1]; mn=sum(sl)/20; sd=math.sqrt(sum((x-mn)**2 for x in sl)/20)
        bbu=mn+2*sd; bbl=mn-2*sd
        bbp=((C[n]-bbl)/(bbu-bbl)*100) if bbu!=bbl else 50

        # ADX-14 (correct Wilder implementation)
        adx=20; pdi_v=25; ndi_v=25
        try:
            p=14
            tr2=tr[1:]  # TR without first element (no prev close for i=0)
            pdm=[max(H[i]-H[i-1],0) if(H[i]-H[i-1])>(L[i-1]-L[i]) else 0
                 for i in range(1,len(C))]
            ndm=[max(L[i-1]-L[i],0) if(L[i-1]-L[i])>(H[i]-H[i-1]) else 0
                 for i in range(1,len(C))]
            # Wilder smoothing: SUM init for TR/DM
            def wilder_s(a,per):
                o=[0.0]*len(a)
                if len(a)<per: return o
                o[per-1]=sum(a[:per])
                for i in range(per,len(a)): o[i]=o[i-1]-o[i-1]/per+a[i]
                return o
            spd=wilder_s(pdm,p); snd=wilder_s(ndm,p); str2=wilder_s(tr2,p)
            pdi_arr=[100*s/t if t else 0 for s,t in zip(spd,str2)]
            ndi_arr=[100*s/t if t else 0 for s,t in zip(snd,str2)]
            # DX only valid from p-1 onward
            dx=[100*abs(a-b)/(a+b) if(a+b) else 0
                for a,b in zip(pdi_arr[p-1:],ndi_arr[p-1:])]
            # ADX: Wilder smooth of DX with AVERAGE init
            if len(dx)>=p:
                adx_vals=[0.0]*len(dx)
                adx_vals[p-1]=sum(dx[:p])/p  # AVERAGE (not sum) for ADX
                for i in range(p,len(dx)):
                    adx_vals[i]=adx_vals[i-1]-adx_vals[i-1]/p+dx[i]/p
                adx=min(100.0,max(0.0,round(adx_vals[-1],1)))
            pdi_v=round(pdi_arr[-1],1); ndi_v=round(ndi_arr[-1],1)
            # PDI/NDI 7-day trend: are they converging?
            if len(pdi_arr)>=8:
                pdi_7ago=pdi_arr[-8]; ndi_7ago=ndi_arr[-8]
                pdi_delta=round(pdi_arr[-1]-pdi_7ago,1)  # negative = PDI falling
                ndi_delta=round(ndi_arr[-1]-ndi_7ago,1)  # positive = NDI rising
            else:
                pdi_delta=0; ndi_delta=0
        except: pass

        # IV rank proxy
        # Same NaN-guard fix as routes_strategy.py's _compute_ta -- C[i-1]>0
        # alone doesn't catch a NaN Close at C[i] itself, which produces
        # math.log(NaN) and crashes round() below with "cannot convert
        # float NaN to integer" for symbols with a NaN bar in the window.
        r30=[math.log(C[i]/C[i-1]) for i in range(n-29,n+1) if C[i-1]>0 and math.isfinite(C[i]) and C[i]>0]
        r90=[math.log(C[i]/C[i-1]) for i in range(max(1,n-89),n+1) if C[i-1]>0 and math.isfinite(C[i]) and C[i]>0]
        rv30=math.sqrt(sum(x**2 for x in r30)/len(r30)*252)*100 if r30 else 20
        rv90=math.sqrt(sum(x**2 for x in r90)/len(r90)*252)*100 if r90 else rv30
        _ivr_raw = (rv30/rv90)*50 if rv90 else None
        ivr = min(100,max(0,round(_ivr_raw))) if _ivr_raw is not None and math.isfinite(_ivr_raw) else 40

        s20=(e20[n]-e20[max(0,n-5)])/5; s50=(e50[n]-e50[max(0,n-10)])/10
        if   e20[n]>e50[n] and s20>0 and s50>0:   trend="UPTREND"
        elif e20[n]<e50[n] and s20<0 and s50<0:   trend="DOWNTREND"
        elif abs(s20)<0.05*C[n]/100:               trend="SIDEWAYS"
        elif e20[n]>e50[n]:                        trend="MILD UP"
        else:                                       trend="MILD DOWN"

        diff=r14[n]-e90[n]
        if   diff>=20:  mom="OVERBOUGHT"
        elif diff<=-20: mom="OVERSOLD"
        elif diff>=10:  mom="ELEVATED"
        elif diff<=-10: mom="DEPRESSED"
        else:           mom="NEUTRAL"

        macd_sig=("BULLISH" if mh[n]>0 and mh[n]>mh[n-1] else
                  "BEARISH" if mh[n]<0 and mh[n]<mh[n-1] else "NEUTRAL")

        va5 =sum(V[max(0,n-4):n+1])/min(5,n+1)
        va20=sum(V[max(0,n-19):n+1])/min(20,n+1)

        result={
            "price":round(C[n],2), "atr":round(atr,2),
            "rsi":round(r14[n],1), "ema90_rsi":round(e90[n],1),
            "rsi_diff":round(diff,1), "momentum":mom,
            "macd":macd_sig, "macd_hist":round(mh[n],4),
            "trend":trend, "ema20":round(e20[n],2), "ema50":round(e50[n],2),
            "bb_pct":round(bbp,1), "bb_upper":round(bbu,2), "bb_lower":round(bbl,2),
            "adx":round(adx,1), "pdi":round(pdi_v,1), "ndi":round(ndi_v,1),
            "iv_rank":ivr, "iv_est":round(rv30,1),
            "vol_surge":round(va5/va20,2) if va20 else 1.0,
        }
        _ta_cache[symbol]=(result, _ts()+_TA_TTL); return result
    except Exception as e:
        print(f"[ta] {symbol}: {e}")
        _ta_cache[symbol]=(None, _ts()+_TA_TTL); return None


# ── Regime classification ─────────────────────────────────────────────────
def _classify(ta):
    """
    Classify into TRENDING, MEAN_REVERSION, or MEAN_REVERSION with exhaustion flag.

    MEAN_REVERSION covers both:
      - Classic: price extended (RSI-EMA diff ±15, BB extreme, ADX<30)
      - ADX Exhaustion: strong trend + overbought/oversold + PDI/NDI converging
        This is still mean reversion — just triggered by trend exhaustion signals

    sub_type returned tells scanner which spread to build:
      None        → single spread (bull put or bear call)
      "condor"    → Iron Condor (ADX very high + PDI>>NDI, range expected)
      "bear_call" → Bear Call (PDI falling + MACD rolling over)
      "bull_put"  → Bull Put (NDI falling + MACD turning up)
    """
    adx=ta["adx"]; diff=ta["rsi_diff"]; bb=ta["bb_pct"]
    trend=ta["trend"]; macd=ta["macd"]; mom=ta["momentum"]
    vol=ta["vol_surge"]; pdi=ta["pdi"]; ndi=ta["ndi"]; rsi=ta["rsi"]
    pdi_delta=ta.get("pdi_delta",0)   # PDI change over last 7 days
    ndi_delta=ta.get("ndi_delta",0)   # NDI change over last 7 days

    # ── PDI/NDI convergence signal ────────────────────────────────────────
    # Converging = dominant DI falling + opposing DI rising
    pdi_falling = pdi_delta < -3   # PDI lost >3 pts in 7 days
    ndi_rising  = ndi_delta >  3   # NDI gained >3 pts in 7 days
    ndi_falling = ndi_delta < -3
    pdi_rising  = pdi_delta >  3
    converging_bearish = pdi_falling and ndi_rising   # uptrend losing steam
    converging_bullish = ndi_falling and pdi_rising   # downtrend losing steam

    # ── ADX exhaustion check ──────────────────────────────────────────────
    # High ADX (>40) + overbought/oversold + PDI/NDI converging = exhaustion
    adx_peak = adx >= 40
    adx_extreme = adx >= 48   # very rare, highest exhaustion probability

    # ── Score MEAN REVERSION (includes exhaustion) ────────────────────────
    mr_score=0; mr_r=[]; mr_dir=None; sub_type=None

    # Classic MR signals
    if   diff<=-20 and bb<15:  mr_score+=6; mr_dir="oversold";   mr_r+=[f"RSI-EMA {diff:+.0f} OVERSOLD",f"BB%B {bb:.0f}%"]
    elif diff<=-15 and bb<25:  mr_score+=4; mr_dir="oversold";   mr_r+=[f"RSI-EMA {diff:+.0f} depressed",f"BB%B {bb:.0f}%"]
    elif diff<=-12 and bb<30:  mr_score+=2; mr_dir="oversold";   mr_r+=[f"RSI-EMA {diff:+.0f}",f"BB%B {bb:.0f}%"]
    elif diff>= 20 and bb>85:  mr_score+=6; mr_dir="overbought"; mr_r+=[f"RSI-EMA {diff:+.0f} OVERBOUGHT",f"BB%B {bb:.0f}%"]
    elif diff>= 15 and bb>75:  mr_score+=4; mr_dir="overbought"; mr_r+=[f"RSI-EMA {diff:+.0f} elevated",f"BB%B {bb:.0f}%"]
    elif diff>= 12 and bb>70:  mr_score+=2; mr_dir="overbought"; mr_r+=[f"RSI-EMA {diff:+.0f}",f"BB%B {bb:.0f}%"]

    # ADX exhaustion adds to MR score (overrides the ADX penalty below)
    if mr_dir and adx_peak:
        if adx_extreme:
            mr_score+=4; mr_r.append(f"ADX {adx:.0f} EXTREME — exhaustion zone")
        else:
            mr_score+=2; mr_r.append(f"ADX {adx:.0f} peak — trend fading")

        # PDI/NDI convergence is the strongest exhaustion confirmation
        if mr_dir=="overbought" and converging_bearish:
            mr_score+=3; sub_type="bear_call"
            mr_r.append(f"PDI falling {pdi_delta:+.0f}pts, NDI rising {ndi_delta:+.0f}pts → exhaustion confirmed")
        elif mr_dir=="oversold" and converging_bullish:
            mr_score+=3; sub_type="bull_put"
            mr_r.append(f"NDI falling {ndi_delta:+.0f}pts, PDI rising {pdi_delta:+.0f}pts → exhaustion confirmed")
        elif mr_dir=="overbought" and pdi > ndi * 2.5:
            # PDI heavily dominant — range likely after peak
            mr_score+=2; sub_type="condor"
            mr_r.append(f"PDI({pdi:.0f})>>NDI({ndi:.0f}) — bull dominance → IC range play")
        elif mr_dir=="oversold" and ndi > pdi * 2.5:
            mr_score+=2; sub_type="condor"
            mr_r.append(f"NDI({ndi:.0f})>>PDI({pdi:.0f}) — bear dominance → IC range play")
        # MACD divergence (price going one way, MACD other)
        if mr_dir=="overbought" and macd=="BEARISH":
            mr_score+=2; sub_type=sub_type or "bear_call"
            mr_r.append("MACD bearish divergence — momentum rolling over")
        elif mr_dir=="oversold" and macd=="BULLISH":
            mr_score+=2; sub_type=sub_type or "bull_put"
            mr_r.append("MACD bullish divergence — momentum turning up")
    elif mr_dir:
        # Normal MR (no ADX exhaustion)
        if adx<20:   mr_score+=3; mr_r.append(f"ADX {adx:.0f} (weak trend → reversion likely)")
        elif adx<30: mr_score+=1; mr_r.append(f"ADX {adx:.0f}")
        else:        mr_score-=2  # strong trend without exhaustion works against MR

        if mr_dir=="oversold":
            if rsi<25:          mr_score+=2; mr_r.append(f"RSI {rsi:.0f} extreme")
            elif rsi<30:        mr_score+=1; mr_r.append(f"RSI {rsi:.0f}")
            if macd=="BULLISH": mr_score+=2; mr_r.append("MACD turning↑")
            if converging_bullish: mr_score+=2; mr_r.append(f"PDI/NDI converging bullish")
            if vol>=1.5:        mr_score+=1; mr_r.append(f"Vol {vol:.1f}×")
        else:
            if rsi>75:          mr_score+=2; mr_r.append(f"RSI {rsi:.0f} extreme")
            elif rsi>70:        mr_score+=1; mr_r.append(f"RSI {rsi:.0f}")
            if macd=="BEARISH": mr_score+=2; mr_r.append("MACD turning↓")
            if converging_bearish: mr_score+=2; mr_r.append(f"PDI/NDI converging bearish")
            if vol>=1.5:        mr_score+=1; mr_r.append(f"Vol {vol:.1f}×")

    # ── Score TRENDING ────────────────────────────────────────────────────
    tr_score=0; tr_r=[]
    if adx>=20 and trend in ("UPTREND","DOWNTREND","MILD UP","MILD DOWN"):
        if adx>=30:   tr_score+=3; tr_r.append(f"ADX {adx:.0f} strong")
        elif adx>=20: tr_score+=2; tr_r.append(f"ADX {adx:.0f} moderate")
        if trend=="UPTREND":
            tr_score+=3; tr_r.append("EMA20>50 uptrend")
            if macd=="BULLISH": tr_score+=2; tr_r.append("MACD↑")
            if pdi>ndi:         tr_score+=1; tr_r.append(f"+DI{pdi:.0f}>-DI{ndi:.0f}")
        elif trend=="DOWNTREND":
            tr_score+=3; tr_r.append("EMA20<50 downtrend")
            if macd=="BEARISH": tr_score+=2; tr_r.append("MACD↓")
            if ndi>pdi:         tr_score+=1; tr_r.append(f"-DI{ndi:.0f}>+DI{pdi:.0f}")
        elif trend in ("MILD UP","MILD DOWN"):
            tr_score+=1
            if macd in ("BULLISH","BEARISH"): tr_score+=1
        if vol>=1.5: tr_score+=1; tr_r.append(f"Vol {vol:.1f}×")

    # ── Decision ──────────────────────────────────────────────────────────
    macd_reversing = (mr_dir=="oversold" and macd=="BULLISH") or                      (mr_dir=="overbought" and macd=="BEARISH")

    if mr_score > 0 and mr_dir:
        # ADX exhaustion: MR wins even in strong trend if score is high enough
        if adx_peak and mr_score >= 6:
            q = "A" if mr_score>=10 else "B" if mr_score>=7 else "C"
            return "MEAN_REVERSION", q, mr_r, sub_type
        # Normal MR logic
        if adx >= 30 and not macd_reversing and not converging_bearish and not converging_bullish:
            pass  # strong trend, no reversal signal → trending wins
        elif mr_score >= tr_score or adx < 20:
            q = "A" if mr_score>=8 else "B" if mr_score>=5 else "C"
            return "MEAN_REVERSION", q, mr_r, sub_type
        elif mr_score >= 4:
            q = "B" if mr_score>=6 else "C"
            return "MEAN_REVERSION", q, mr_r, sub_type

    if tr_score >= 3:
        q = "A" if tr_score>=7 else "B" if tr_score>=5 else "C"
        return "TRENDING", q, tr_r, None

    if mr_score >= 3 and mr_dir:
        return "MEAN_REVERSION", "C", mr_r, sub_type

    return None, None, [], None


# ── Live chain ────────────────────────────────────────────────────────────
def _fetch_chain(symbol, spot, max_dte=45, min_dte=15):
    """Use the newest saved option snapshot only; never synthesize a chain or quote."""
    try:
        con = _conn()
        try:
            rows = con.execute(
                """SELECT expiration, MAX(date) AS snapshot_date
                   FROM options
                   WHERE symbol=? AND expiration>=?
                   GROUP BY expiration""",
                (symbol.upper().strip(), date.today().isoformat()),
            ).fetchall()
            candidates = []
            for row in rows:
                try:
                    expiry = str(row["expiration"])
                    dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days
                except Exception:
                    continue
                if min_dte <= dte <= max_dte and row["snapshot_date"]:
                    try:
                        age_days = (date.today() - datetime.strptime(str(row["snapshot_date"])[:10], "%Y-%m-%d").date()).days
                    except Exception:
                        age_days = 99
                    if age_days <= 4:
                        candidates.append((abs(dte - 35), dte, expiry, str(row["snapshot_date"])))
            if not candidates:
                return None, None, {}
            _, dte, expiry, snapshot_date = min(candidates)
            chain_rows = con.execute(
                """SELECT type, strike, price, oi, volume, bid, ask, last, iv, underlying
                   FROM options
                   WHERE symbol=? AND expiration=? AND date=?""",
                (symbol.upper().strip(), expiry, snapshot_date),
            ).fetchall()
        finally:
            con.close()
        result = {}
        for row in chain_rows:
            side = "call" if str(row["type"]).upper().startswith("C") else "put"
            strike = _safe(row["strike"])
            if strike is None or abs(strike - spot) / max(spot, 0.01) > 0.20:
                continue
            bid, ask = _safe(row["bid"]), _safe(row["ask"])
            mid = round((bid + ask) / 2.0, 2) if bid and ask and bid > 0 and ask > 0 else None
            entry = {
                "bid": bid, "ask": ask, "mid": mid, "last": _safe(row["last"]),
                "price": _safe(row["price"]), "iv": _safe(row["iv"], 6),
                "oi": int(row["oi"] or 0), "volume": int(row["volume"] or 0),
                "strike": strike, "snapshot_date": snapshot_date,
                "underlying": _safe(row["underlying"]),
            }
            result[(side, int(strike))] = entry
            result[(side, float(strike))] = entry
        return expiry, dte, result
    except Exception as exc:
        print(f"[saved chain] {symbol}: {exc}")
        return None, None, {}


def _chain_leg(chain, side, strike):
    for key in ((side, int(strike)), (side, float(strike))):
        row = chain.get(key)
        if row:
            return row
    return None


def _leg_quality(chain, side, strike, min_oi=50, min_volume=5, max_spread_pct=15.0):
    row = _chain_leg(chain, side, strike)
    if not row:
        return False, "missing saved leg"
    bid, ask = row.get("bid"), row.get("ask")
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return False, "missing executable bid/ask"
    mid = (bid + ask) / 2.0
    spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 else 999.0
    if spread_pct > max_spread_pct:
        return False, "wide bid/ask"
    if int(row.get("oi") or 0) < min_oi:
        return False, "low OI"
    if int(row.get("volume") or 0) < min_volume:
        return False, "low option volume"
    if not row.get("iv") or row["iv"] <= 0:
        return False, "missing implied volatility"
    return True, ""


def _norm_cdf(value):
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _stored_delta(spot, strike, dte, iv, is_call):
    iv = float(iv or 0)
    if iv > 3:
        iv /= 100.0
    if spot <= 0 or strike <= 0 or iv <= 0 or dte <= 0:
        return None
    t = dte / 365.0
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    delta = _norm_cdf(d1)
    return delta if is_call else delta - 1.0


def _chain_expected_move(chain, spot, dte):
    entries = []
    seen = set()
    for (side, strike), row in chain.items():
        key = (side, float(strike))
        if key in seen:
            continue
        seen.add(key)
        iv = row.get("iv")
        if iv and iv > 0:
            entries.append((abs(float(strike) - spot), float(iv)))
    if not entries:
        return None
    entries.sort(key=lambda item: item[0])
    iv = sum(value for _, value in entries[:4]) / min(4, len(entries))
    if iv > 3:
        iv /= 100.0
    return round(spot * iv * math.sqrt(max(dte, 1) / 365.0), 2)


def _mid(chain,side,strike):
    for k in [(side,int(strike)),(side,float(strike)),(side,int(strike)+1),(side,int(strike)-1)]:
        d=chain.get(k)
        if d and d.get("mid"): return d["mid"]
    return None

def _snap(s,interval): return round(round(s/interval)*interval,2)
def _pop(pct): return min(90,max(50,round(60+pct*2.8)))


# ── Build spread ──────────────────────────────────────────────────────────

def _win_probability(ta, regime, quality, rr_val, pop, dte, pct_otm, earn_days):
    """
    0-100 win probability using: RSI-EMA diff, MACD, ADX, PDI/NDI,
    EMA9/20/50 alignment, BB%B, Volume, IVR, R:R, PoP, DTE, earnings.
    """
    adx   = ta.get("adx",20);     diff  = ta.get("rsi_diff",0)
    bb    = ta.get("bb_pct",50);  macd  = ta.get("macd","NEUTRAL")
    trend = ta.get("trend","SIDEWAYS"); vol = ta.get("vol_surge",1.0)
    pdi   = ta.get("pdi",25);     ndi   = ta.get("ndi",25)
    pd_d  = ta.get("pdi_delta",0); nd_d = ta.get("ndi_delta",0)
    rsi   = ta.get("rsi",50);     ivr   = ta.get("iv_rank",50)
    e9_a20= ta.get("e9_above_e20",False)
    e9_ap = ta.get("e9_above_price",False)
    e9_sl = ta.get("ema9_slope",0)

    score = 50; reasons = []
    is_mr = "MEAN" in regime.upper()
    is_tr = "TREND" in regime.upper()

    # RSI-EMA diff (primary signal)
    abs_diff = abs(diff)
    if is_mr:
        if   abs_diff >= 20: score += 15; reasons.append(f"RSI-EMA {diff:+.0f} extreme ✓")
        elif abs_diff >= 15: score += 10; reasons.append(f"RSI-EMA {diff:+.0f} elevated ✓")
        elif abs_diff >= 10: score += 5;  reasons.append(f"RSI-EMA {diff:+.0f}")
        else:                score -= 5;  reasons.append(f"RSI-EMA {diff:+.0f} weak signal")
    else:
        if   abs_diff >= 20: score -= 8;  reasons.append(f"RSI-EMA {diff:+.0f} overbought — reversal risk")
        elif abs_diff <= 5:  score += 8;  reasons.append(f"RSI-EMA {diff:+.0f} neutral — trend intact")
        else:                score += 3

    # MACD
    if macd == "BULLISH" and (not is_mr or diff <= -10):
        score += 10; reasons.append("MACD↑ bullish")
    elif macd == "BEARISH" and (not is_mr or diff >= 10):
        score += 10; reasons.append("MACD↓ bearish")
    elif macd == "NEUTRAL":
        score -= 3

    # ADX
    if is_tr:
        if   adx >= 35: score += 12; reasons.append(f"ADX {adx:.0f} strong trend ✓")
        elif adx >= 25: score += 7;  reasons.append(f"ADX {adx:.0f} trending")
        elif adx >= 18: score += 2
        else:           score -= 8;  reasons.append(f"ADX {adx:.0f} weak — no trend")
    else:
        if   adx >= 40: score += 10; reasons.append(f"ADX {adx:.0f} exhaustion zone ✓")
        elif adx < 20:  score += 5;  reasons.append(f"ADX {adx:.0f} weak → reversion likely")
        elif 20 <= adx < 30: score += 2
        else:           score -= 3;  reasons.append(f"ADX {adx:.0f} — trend may continue")

    # PDI / NDI
    pdi_falling = pd_d < -3; ndi_rising  = nd_d >  3
    ndi_falling = nd_d < -3; pdi_rising  = pd_d >  3
    if is_mr:
        if pdi_falling and ndi_rising:
            score += 10; reasons.append(f"PDI↓{pd_d:.0f}/NDI↑{nd_d:.0f} converging bearish ✓")
        elif ndi_falling and pdi_rising:
            score += 10; reasons.append(f"NDI↓{nd_d:.0f}/PDI↑{pd_d:.0f} converging bullish ✓")
        elif abs(pd_d) < 2 and abs(nd_d) < 2:
            score += 5; reasons.append("DI stable — range environment")
    else:
        if pdi > ndi and not pdi_falling:
            score += 8; reasons.append(f"PDI({pdi:.0f})>NDI({ndi:.0f}) bull dominance")
        elif ndi > pdi and not ndi_falling:
            score += 8; reasons.append(f"NDI({ndi:.0f})>PDI({pdi:.0f}) bear dominance")
        if pdi_falling or ndi_rising:
            score -= 5; reasons.append("DI converging — trend weakening")

    # EMA9 / 20 / 50 alignment
    trend_up   = trend in ("UPTREND","MILD UP")
    trend_down = trend in ("DOWNTREND","MILD DOWN")
    if is_tr and trend_up:
        if e9_a20 and e9_ap:   score += 8; reasons.append("EMA9>EMA20 + price>EMA9 aligned bull ✓")
        elif e9_a20:            score += 4; reasons.append("EMA9>EMA20")
        if e9_sl > 0:           score += 3; reasons.append("EMA9 slope↑")
    elif is_tr and trend_down:
        if not e9_a20 and not e9_ap: score += 8; reasons.append("EMA9<EMA20 + price<EMA9 aligned bear ✓")
        elif not e9_a20:             score += 4; reasons.append("EMA9<EMA20")
        if e9_sl < 0:                score += 3; reasons.append("EMA9 slope↓")
    elif is_mr:
        if e9_sl > 0 and diff <= -10:  score += 5; reasons.append("EMA9 turning up from oversold ✓")
        elif e9_sl < 0 and diff >= 10: score += 5; reasons.append("EMA9 turning down from overbought ✓")

    # BB%B
    if is_mr:
        if   bb < 10 or bb > 90: score += 8;  reasons.append(f"BB%B {bb:.0f}% extreme ✓")
        elif bb < 20 or bb > 80: score += 5;  reasons.append(f"BB%B {bb:.0f}% extended")
        elif bb < 30 or bb > 70: score += 2
        else:                    score -= 5;  reasons.append(f"BB%B {bb:.0f}% mid-range — poor MR entry")
    else:
        if 30 < bb < 70: score += 3; reasons.append(f"BB%B {bb:.0f}% — trend space remaining")

    # Volume
    if   vol >= 2.0: score += 8; reasons.append(f"Vol {vol:.1f}× — strong conviction")
    elif vol >= 1.5: score += 5; reasons.append(f"Vol {vol:.1f}× — above avg")
    elif vol >= 1.2: score += 2
    elif vol < 0.7:  score -= 5; reasons.append(f"Vol {vol:.1f}× — thin volume ⚠")

    # IV Rank
    if   ivr >= 70: score += 8; reasons.append(f"IVR {ivr}/100 — premium rich ✓")
    elif ivr >= 50: score += 5; reasons.append(f"IVR {ivr}/100 — above avg")
    elif ivr >= 30: score += 1
    else:           score -= 5; reasons.append(f"IVR {ivr}/100 — thin premium ⚠")

    # R:R and PoP
    if   rr_val >= 1.5: score += 8; reasons.append(f"R:R {rr_val:.2f}:1 excellent")
    elif rr_val >= 1.0: score += 5; reasons.append(f"R:R {rr_val:.2f}:1 solid")
    elif rr_val >= 0.7: score += 2
    else:               score -= 5; reasons.append(f"R:R {rr_val:.2f}:1 marginal")

    if   pop >= 75: score += 5; reasons.append(f"PoP {pop}% high probability")
    elif pop >= 65: score += 3
    elif pop < 55:  score -= 3

    # DTE
    if   21 <= dte <= 45: score += 5; reasons.append(f"DTE {dte} sweet spot ✓")
    elif 14 <= dte <= 60: score += 2
    elif dte < 7:          score -= 10; reasons.append(f"DTE {dte} — gamma risk ⚠")

    # Earnings
    if earn_days < dte:
        score -= 20; reasons.append(f"⚠ Earnings in {earn_days}d (before expiry)")
    elif earn_days < 14:
        score -= 8; reasons.append(f"Earnings in {earn_days}d — IV spike risk")

    # Quality modifier
    if quality == 'A':   score += 5
    elif quality == 'C': score -= 5

    score = max(5, min(96, round(score)))

    if   score >= 75: wp_grade = "A"
    elif score >= 58: wp_grade = "B"
    elif score >= 42: wp_grade = "C"
    else:             wp_grade = "D"

    return score, wp_grade, reasons[:6]



def _build_signal_detail(ta, regime, wp_reasons):
    """Build structured signal breakdown for the UI rationale panel."""
    adx  = ta.get("adx",20); diff = ta.get("rsi_diff",0)
    rsi  = ta.get("rsi",50); bb   = ta.get("bb_pct",50)
    macd = ta.get("macd","NEUTRAL"); trend = ta.get("trend","SIDEWAYS")
    pdi  = ta.get("pdi",25); ndi  = ta.get("ndi",25)
    pd_d = ta.get("pdi_delta",0); nd_d = ta.get("ndi_delta",0)
    vol  = ta.get("vol_surge",1.0); ivr = ta.get("iv_rank",50)
    e9v  = ta.get("ema9",0); e20v = ta.get("ema20",0); e50v = ta.get("ema50",0)
    e9sl = ta.get("ema9_slope",0)
    spot = ta.get("price",0)
    
    def sig(label, value, color, note=""):
        return {"label":label,"value":value,"color":color,"note":note}
    
    signals = [
        sig("EMA9",   f"${e9v:.2f}  slope{'↑' if e9sl>0 else '↓'}", 
            "#22c55e" if (e9sl>0 and diff<0) or (e9sl<0 and diff>0) else "#64748b",
            f"{'Above' if spot>e9v else 'Below'} EMA9"),
        sig("EMA20",  f"${e20v:.2f}" if e20v else "—",
            "#3b82f6", f"Price {'above' if spot>e20v else 'below'} EMA20"),
        sig("EMA50",  f"${e50v:.2f}" if e50v else "—",
            "#8b5cf6", f"Trend: {trend}"),
        sig("RSI-14", f"{rsi:.0f}",
            "#ef4444" if rsi>70 else "#22c55e" if rsi<30 else "#64748b",
            "Overbought" if rsi>70 else "Oversold" if rsi<30 else "Neutral"),
        sig("RSI-EMA Δ", f"{diff:+.1f}",
            "#ef4444" if diff>=20 else "#22c55e" if diff<=-20 else "#f59e0b" if abs(diff)>=12 else "#64748b",
            "OVERBOUGHT" if diff>=20 else "OVERSOLD" if diff<=-20 else "Elevated" if abs(diff)>=12 else "Neutral"),
        sig("MACD",   macd,
            "#22c55e" if macd=="BULLISH" else "#ef4444" if macd=="BEARISH" else "#64748b",
            "Hist rising" if macd=="BULLISH" else "Hist falling" if macd=="BEARISH" else "Flat"),
        sig("ADX",    f"{adx:.0f}",
            "#22c55e" if adx>=30 else "#f59e0b" if adx>=20 else "#64748b",
            "Strong" if adx>=35 else "Trending" if adx>=25 else "Weak" if adx<20 else "Moderate"),
        sig("PDI→",   f"{pdi:.0f} ({pd_d:+.0f})",
            "#22c55e" if pd_d>3 else "#ef4444" if pd_d<-3 else "#64748b",
            "Rising" if pd_d>3 else "Falling" if pd_d<-3 else "Stable"),
        sig("NDI→",   f"{ndi:.0f} ({nd_d:+.0f})",
            "#ef4444" if nd_d>3 else "#22c55e" if nd_d<-3 else "#64748b",
            "Rising" if nd_d>3 else "Falling" if nd_d<-3 else "Stable"),
        sig("BB%B",   f"{bb:.0f}%",
            "#ef4444" if bb>80 else "#22c55e" if bb<20 else "#64748b",
            "Upper band" if bb>75 else "Lower band" if bb<25 else "Mid-range"),
        sig("Volume", f"{vol:.1f}×",
            "#22c55e" if vol>=1.5 else "#f59e0b" if vol>=1.2 else "#64748b",
            "Above avg" if vol>=1.5 else "Normal"),
        sig("IVR",    f"{ivr}/100",
            "#ef4444" if ivr>=70 else "#f59e0b" if ivr>=50 else "#3b82f6",
            "Rich premium" if ivr>=70 else "Elevated" if ivr>=50 else "Thin"),
    ]
    return {"signals": signals, "top_reasons": wp_reasons}


def _continuation_pct(ta, regime):
    """
    Probability the CURRENT move CONTINUES (not reverses) as a %.
    For TRENDING setups: high = trend likely to persist (good for trend trades)
    For MEAN_REVERSION: low = current extreme likely to reverse (good for fade trades)
    Based on: ADX strength, MACD momentum, trend alignment, RSI extremity.
    """
    adx   = ta["adx"]
    diff  = ta["rsi_diff"]
    macd  = ta["macd"]
    trend = ta["trend"]
    bb    = ta["bb_pct"]
    vol   = ta["vol_surge"]
    score = 50  # neutral baseline

    # ADX: higher ADX = trend more likely to continue
    if adx >= 35:   score += 20
    elif adx >= 25: score += 12
    elif adx >= 18: score += 5
    elif adx < 15:  score -= 10  # weak ADX = trend fragile

    # MACD: aligned with trend = continuation likely
    if trend in ("UPTREND","MILD UP") and macd == "BULLISH":   score += 10
    if trend in ("DOWNTREND","MILD DOWN") and macd == "BEARISH": score += 10
    if trend in ("UPTREND","MILD UP") and macd == "BEARISH":   score -= 15  # divergence
    if trend in ("DOWNTREND","MILD DOWN") and macd == "BULLISH": score -= 15

    # RSI extremes reduce continuation (mean reversion pressure)
    abs_diff = abs(diff)
    if abs_diff >= 25:   score -= 20  # deeply extreme = reversion more likely
    elif abs_diff >= 18: score -= 12
    elif abs_diff >= 12: score -= 6
    elif abs_diff < 5:   score += 5   # neutral momentum = trend steady

    # BB: near upper/lower band = continuation harder
    if bb > 90 or bb < 10:   score -= 15
    elif bb > 80 or bb < 20: score -= 8
    elif 40 < bb < 60:       score += 5  # mid-band = trend stable

    # Volume: surge on breakout = continuation more likely
    if vol >= 2.0: score += 8
    elif vol >= 1.5: score += 4
    elif vol < 0.7:  score -= 5  # low volume = weak move

    return min(95, max(10, score))


def _continuation_note(ta, regime):
    """Human-readable explanation of continuation probability."""
    pct  = _continuation_pct(ta, regime)
    adx  = ta["adx"]
    diff = ta["rsi_diff"]
    if regime == "MEAN_REVERSION":
        if pct < 35:   return f"Low continuation ({pct}%) — reversal likely. ADX {adx:.0f}, RSI-EMA {diff:+.0f}"
        elif pct < 50: return f"Moderate continuation ({pct}%) — reversal possible but trend still present"
        else:          return f"High continuation ({pct}%) — trend may override reversion. Caution."
    else:
        if pct >= 65:  return f"Strong continuation ({pct}%) — trend likely to persist. ADX {adx:.0f}"
        elif pct >= 50:return f"Moderate continuation ({pct}%) — trend intact but watch for reversal"
        else:          return f"Weak continuation ({pct}%) — trend losing momentum"


def _make_spread(symbol, expiry, dte, side, sell_s, buy_s, chain, spot, ta, regime,
                 quality, reasons, earn_days, earn_date, expected_move=None):
    """Return an executable, defined-risk saved-chain credit spread or None."""
    width = abs(sell_s - buy_s)
    if width < 2.0:
        return None
    sell_ok, _ = _leg_quality(chain, side, sell_s)
    buy_ok, _ = _leg_quality(chain, side, buy_s)
    if not sell_ok or not buy_ok:
        return None
    se = _chain_leg(chain, side, sell_s) or {}
    be = _chain_leg(chain, side, buy_s) or {}
    sm, bm = _mid(chain, side, sell_s), _mid(chain, side, buy_s)
    if sm is None or bm is None:
        return None
    net = round(sm - bm, 2)
    if net < 0.15 or net >= width:
        return None
    rr = round(net / (width - net), 2)
    if rr < 0.60:  # user-approved hard floor; 1.0 remains the preferred target.
        return None

    is_call = side == "call"
    sell_delta = _stored_delta(spot, sell_s, dte, se.get("iv"), is_call)
    if sell_delta is None or not (0.10 <= abs(sell_delta) <= 0.30):
        return None
    if expected_move is None or abs(spot - sell_s) < 0.75 * expected_move:
        return None

    pct = round(abs(spot - sell_s) / spot * 100, 1)
    # This is a model estimate from delta; it is not a historical win probability.
    pop_model = round(min(90, max(50, (1.0 - abs(sell_delta)) * 100)))
    ev_model = round(net * (pop_model / 100.0) - (width - net) * (1.0 - pop_model / 100.0), 3)
    bias = "Bullish" if side == "put" else "Bearish"
    name = "Bull Put Spread" if side == "put" else "Bear Call Spread"
    legs = (f"Sell ${sell_s}P / Buy ${buy_s}P" if side == "put"
            else f"Sell ${sell_s}C / Buy ${buy_s}C")
    icon = {"TRENDING": "📈", "MEAN_REVERSION": "🔄"}.get(regime, "•")
    score, grade, score_reasons = _win_probability(
        ta, regime, quality, rr, pop_model, dte, pct, earn_days
    )
    snapshot = se.get("snapshot_date") or be.get("snapshot_date")
    return {
        "symbol": symbol, "expiry": expiry, "dte": dte,
        "strategy": name, "bias": bias, "regime": regime,
        "regime_label": regime.replace("_", " "), "quality": grade,
        "legs": legs, "sell_strike": sell_s, "buy_strike": buy_s, "spread_width": width,
        "sell_bid": se.get("bid"), "sell_ask": se.get("ask"), "sell_mid": sm,
        "buy_bid": be.get("bid"), "buy_ask": be.get("ask"), "buy_mid": bm,
        "net_credit": net, "max_gain_dol": round(net * 100, 2),
        "max_loss_dol": round((width - net) * 100, 2),
        "rr": f"{rr:.2f}:1", "rr_val": rr, "pop": pop_model,
        "pop_label": "Delta model, unvalidated", "pct_otm": pct, "spot": round(spot, 2),
        "short_delta": round(sell_delta, 3), "expected_move": expected_move,
        "snapshot_date": snapshot, "option_data_source": "saved chain",
        "adx": ta["adx"], "iv_rank": ta["iv_rank"], "rsi": ta["rsi"],
        "rsi_diff": ta["rsi_diff"], "bb_pct": ta["bb_pct"], "macd": ta["macd"],
        "trend": ta["trend"], "momentum": ta["momentum"], "vol_surge": ta["vol_surge"],
        "pdi": ta["pdi"], "ndi": ta["ndi"], "pdi_delta": ta.get("pdi_delta", 0),
        "ndi_delta": ta.get("ndi_delta", 0), "earn_days": earn_days,
        "earn_date": earn_date or "—", "earn_warn": "",
        "continuation_pct": _continuation_pct(ta, regime),
        "continuation_note": _continuation_note(ta, regime),
        "price_source": "saved data", "ev": ev_model, "reasons": reasons,
        "win_prob": score, "win_grade": grade, "win_reasons": score_reasons[:6],
        "signal_detail": _build_signal_detail(ta, regime, score_reasons),
        "rationale": (
            f"${icon} ${regime.replace('_', ' ')} [${grade}] ${bias} | "
            f"${'; '.join(reasons[:2])} | $${sell_s} ${pct}% OTM | "
            f"Δ ${sell_delta:.2f} | EM $${expected_move:.2f} | saved ${snapshot}"
        ),
        "manage": (
            f"Close at 50% credit (~$${round(net * 0.5, 2)}). "
            f"Exit if price closes through $${sell_s}; do not hold through earnings."
        ),
    }


# ── Per-symbol# ── Per-symbol (one thread unit) ──────────────────────────────────────────
def _find_condor(put_strikes, call_strikes, wings, chain, spot,
                  symbol, expiry, dte, ta, regime, quality, reasons,
                  earn_days, earn_date, expected_move=None):
    """
    Build an Iron Condor from actual chain strikes.
    Sell OTM put + buy further OTM put (put spread)
    Sell OTM call + buy further OTM call (call spread)
    Requires both spreads to each pass the EV gate individually.
    Returns a combined IC dict or None.
    """
    best_put  = None
    best_call = None

    # Find best put spread (3-10% OTM)
    min_pct=0.03; max_pct=0.10
    for sell_s in put_strikes:
        pct = abs(sell_s-spot)/spot
        if pct < min_pct or pct > max_pct: continue
        sell_ok, _ = _leg_quality(chain, "put", sell_s)
        sell_leg = _chain_leg(chain, "put", sell_s) or {}
        sell_delta = _stored_delta(spot, sell_s, dte, sell_leg.get("iv"), False)
        if (not sell_ok or sell_delta is None or not (0.10 <= abs(sell_delta) <= 0.30)
                or expected_move is None or abs(spot - sell_s) < 0.75 * expected_move):
            continue
        sm = _mid(chain,"put",sell_s)
        if not sm or sm < 0.10: continue
        for wing in wings:
            if wing < 2.0: continue
            target_buy = sell_s - wing
            best_buy = None
            for s2 in put_strikes:
                if s2 >= sell_s: continue
                if abs(s2-target_buy) <= wing*0.6:
                    if best_buy is None or abs(s2-target_buy)<abs(best_buy-target_buy):
                        best_buy=s2
            if not best_buy: continue
            buy_ok, _ = _leg_quality(chain, "put", best_buy)
            bm = _mid(chain,"put",best_buy)
            if not buy_ok or not bm: continue
            width=abs(sell_s-best_buy)
            if width<2.0: continue
            net=round(sm-bm,2)
            if net<=0: continue
            rr=round(net/(width-net),2) if width>net else 0
            pop_dec=_pop(abs(spot-sell_s)/spot*100)/100
            min_rr=max(0.60, round((1-pop_dec)/pop_dec,2))
            if rr>=min_rr:
                best_put=(sell_s,best_buy,net,sm,bm,width)
                break
        if best_put: break

    # Find best call spread (3-10% OTM)
    for sell_s in call_strikes:
        pct = abs(sell_s-spot)/spot
        if pct < min_pct or pct > max_pct: continue
        sell_ok, _ = _leg_quality(chain, "call", sell_s)
        sell_leg = _chain_leg(chain, "call", sell_s) or {}
        sell_delta = _stored_delta(spot, sell_s, dte, sell_leg.get("iv"), True)
        if (not sell_ok or sell_delta is None or not (0.10 <= abs(sell_delta) <= 0.30)
                or expected_move is None or abs(spot - sell_s) < 0.75 * expected_move):
            continue
        sm = _mid(chain,"call",sell_s)
        if not sm or sm < 0.10: continue
        for wing in wings:
            if wing < 2.0: continue
            target_buy = sell_s + wing
            best_buy = None
            for s2 in call_strikes:
                if s2 <= sell_s: continue
                if abs(s2-target_buy) <= wing*0.6:
                    if best_buy is None or abs(s2-target_buy)<abs(best_buy-target_buy):
                        best_buy=s2
            if not best_buy: continue
            buy_ok, _ = _leg_quality(chain, "call", best_buy)
            bm = _mid(chain,"call",best_buy)
            if not buy_ok or not bm: continue
            width=abs(sell_s-best_buy)
            if width<2.0: continue
            net=round(sm-bm,2)
            if net<=0: continue
            rr=round(net/(width-net),2) if width>net else 0
            pop_dec=_pop(abs(spot-sell_s)/spot*100)/100
            min_rr=round((1-pop_dec)/pop_dec,2)
            if rr>=min_rr:
                best_call=(sell_s,best_buy,net,sm,bm,width)
                break
        if best_call: break

    if not best_put or not best_call: return None

    p_sell,p_buy,p_net,p_sm,p_bm,p_width = best_put
    c_sell,c_buy,c_net,c_sm,c_bm,c_width = best_call

    total_credit = round(p_net+c_net, 2)
    wing_used    = max(p_width, c_width)
    max_profit   = round(total_credit*100, 2)
    max_loss     = round((wing_used-total_credit/2)*100, 2)
    rr_val       = round(total_credit/2/(wing_used-total_credit/2), 2) if wing_used > total_credit/2 else 0
    put_pop      = _pop(abs(spot-p_sell)/spot*100)
    call_pop     = _pop(abs(spot-c_sell)/spot*100)
    avg_pop      = round((put_pop+call_pop)/2)

    adx=ta["adx"]; pdi=ta["pdi"]; ndi=ta["ndi"]; diff=ta["rsi_diff"]
    ic_reasons = reasons[:3] + [
        f"IC: Put wall ${p_sell} ({round(abs(spot-p_sell)/spot*100,1)}%OTM) "
        f"/ Call wall ${c_sell} (+{round(abs(spot-c_sell)/spot*100,1)}%OTM)",
        f"Range width: ${round(c_sell-p_sell,1)}"
    ]

    rationale = (
        f"🔥 ADX_EXHAUSTION [{quality}] — IRON CONDOR | "
        f"ADX {adx:.0f} peak zone · PDI({pdi:.0f})/NDI({ndi:.0f}) | "
        f"RSI-EMA {diff:+.0f} · Stock rangebound after trend exhaustion | "
        f"Sell ${p_sell}P−${c_sell}C, collect ${total_credit} total"
    )

    # IC P&L: max_loss = wing_used (one side) - total_credit collected
    # Because max loss is when ONE side blows through fully
    # e.g. $5 wide call spread blows up: lose $5×100 - total_credit×100
    ic_max_loss = round((wing_used - total_credit) * 100, 2)
    ic_rr = round(total_credit / (wing_used - total_credit), 2) if wing_used > total_credit else 0
    # EV: if one side hits, lose (wing-credit); both sides don't hit simultaneously
    ic_ev = round(total_credit*(avg_pop/100) - (wing_used-total_credit)*((100-avg_pop)/100), 2)

    return {
        "symbol":symbol,"expiry":expiry,"dte":dte,
        "strategy":"Iron Condor","bias":"Neutral","regime":regime,
        "regime_label":"MEAN REVERSION","quality":quality,
        "legs":f"Sell ${p_sell}P/Buy ${p_buy}P  ·  Sell ${c_sell}C/Buy ${c_buy}C",
        "sell_strike":f"{p_sell}P/{c_sell}C","buy_strike":f"{p_buy}P/{c_buy}C",
        "spread_width":wing_used,
        # IC: show put-side and call-side credits separately
        "sell_mid":p_net,    # put spread credit
        "buy_mid":c_net,     # call spread credit
        "sell_bid":p_sm,"sell_ask":None,
        "buy_bid":c_sm,"buy_ask":None,
        "p_sell":p_sell,"p_buy":p_buy,"p_credit":p_net,
        "c_sell":c_sell,"c_buy":c_buy,"c_credit":c_net,
        "net_credit":total_credit,
        "max_gain_dol":max_profit,
        "max_loss_dol":ic_max_loss,
        "rr":f"{ic_rr:.2f}:1","rr_val":ic_rr,
        "pop":avg_pop,"pct_otm":round((abs(spot-p_sell)+abs(spot-c_sell))/2/spot*100,1),
        "spot":round(spot,2),
        "adx":ta["adx"],"iv_rank":ta["iv_rank"],"rsi":ta["rsi"],
        "rsi_diff":ta["rsi_diff"],"bb_pct":ta["bb_pct"],"macd":ta["macd"],
        "trend":ta["trend"],"momentum":ta["momentum"],"vol_surge":ta["vol_surge"],
        "pdi":ta["pdi"],"ndi":ta["ndi"],"pdi_delta":ta.get("pdi_delta",0),"ndi_delta":ta.get("ndi_delta",0),
        "earn_days":earn_days,"earn_date":earn_date or "—","earn_warn":"",
        "price_source":"saved data","option_data_source":"saved chain","expected_move":expected_move,
        "pop_label":"OTM model, unvalidated","reasons":ic_reasons,
        "rationale":rationale,
        "manage":(
            f"Close at 50% max profit (~${round(max_profit*0.5,0)}). "
            f"Put spread: stop if spot < ${p_sell}. "
            f"Call spread: stop if spot > ${c_sell}. "
            f"Never let both sides threaten simultaneously."
        ),
        "continuation_pct":_continuation_pct(ta, regime),
        "continuation_note":_continuation_note(ta, regime),
        "ev":ic_ev, "win_prob": None, "win_grade": None, "win_reasons": [], "signal_detail": {},
    }


def _scan_one(symbol, min_dte, max_dte, wings):
    """Strict, on-demand 15–45 DTE credit scan using saved market data only."""
    try:
        ta = _compute_ta(symbol)
        if not ta:
            return [], None
        spot = ta["price"]
        if not spot or spot <= 0:
            return [], None

        regime, quality, reasons, sub_type = _classify(ta)
        if not regime:
            return [], None

        expiry, dte, chain = _fetch_chain(symbol, spot, max_dte, min_dte)
        if not expiry or not chain:
            return [], None

        # Unknown or in-window earnings makes a premium-selling candidate ineligible.
        earn_days, earn_date = _get_earn_days(symbol)
        if earn_days is None or earn_days <= dte:
            return [], None

        expected_move = _chain_expected_move(chain, spot, dte)
        if not expected_move or expected_move <= 0:
            return [], None

        results=[]
        
        # Build spread candidates from ACTUAL chain strikes (not theoretical grid)
        # This ensures both legs have real prices
        put_strikes  = sorted({k[1] for k in chain if k[0]=="put"  and k[1]<spot}, reverse=True)
        call_strikes = sorted({k[1] for k in chain if k[0]=="call" and k[1]>spot})

        def _find_spread(side, strikes_otm, preferred_wings):
            """Build only liquid, saved-chain 10–30 delta defined-risk credit spreads."""
            is_call = side == "call"
            ordered = sorted(
                strikes_otm,
                key=lambda strike: abs(abs(_stored_delta(
                    spot, strike, dte, (_chain_leg(chain, side, strike) or {}).get("iv"), is_call
                ) or 99) - 0.20),
            )
            for sell_s in ordered:
                sell_leg = _chain_leg(chain, side, sell_s) or {}
                sell_delta = _stored_delta(spot, sell_s, dte, sell_leg.get("iv"), is_call)
                sell_ok, _ = _leg_quality(chain, side, sell_s)
                if not sell_ok or sell_delta is None or not (0.10 <= abs(sell_delta) <= 0.30):
                    continue
                if abs(spot - sell_s) < 0.75 * expected_move:
                    continue
                for wing in preferred_wings:
                    if wing < 2.0:
                        continue
                    target_buy = sell_s - wing if side == "put" else sell_s + wing
                    candidates = [
                        strike for strike in strikes_otm
                        if ((side == "put" and strike < sell_s) or (side == "call" and strike > sell_s))
                        and abs(strike - target_buy) <= wing * 0.6
                    ]
                    if not candidates:
                        continue
                    best_buy = min(candidates, key=lambda strike: abs(strike - target_buy))
                    buy_ok, _ = _leg_quality(chain, side, best_buy)
                    if not buy_ok or abs(sell_s - best_buy) < 2.0:
                        continue
                    spread = _make_spread(
                        symbol, expiry, dte, side, sell_s, best_buy, chain, spot, ta,
                        regime, quality, reasons, earn_days, earn_date, expected_move=expected_move,
                    )
                    if spread:
                        return spread
            return None


        want_bull = False; want_bear = False; want_condor = False

        if regime=="TRENDING":
            if "UP"   in ta["trend"]: want_bull=True
            if "DOWN" in ta["trend"]: want_bear=True

        elif regime=="MEAN_REVERSION":
            diff=ta["rsi_diff"]
            # sub_type set by _classify based on ADX exhaustion + PDI/NDI signals
            if sub_type=="condor":
                want_condor=True
            elif sub_type=="bear_call":
                want_bear=True
            elif sub_type=="bull_put":
                want_bull=True
            else:
                # Classic MR: direction from momentum
                if ta["momentum"] in ("OVERSOLD","DEPRESSED")  or diff<=-15: want_bull=True
                if ta["momentum"] in ("OVERBOUGHT","ELEVATED") or diff>= 15: want_bear=True

        if want_bull:
            sp = _find_spread("put",  put_strikes,  wings)
            if sp: results.append(sp)

        if want_bear:
            sp = _find_spread("call", call_strikes, wings)
            if sp: results.append(sp)

        if want_condor:
            ic = _find_condor(put_strikes, call_strikes, wings, chain, spot,
                               symbol, expiry, dte, ta, regime, quality, reasons,
                               earn_days, earn_date, expected_move=expected_move)
            if ic: results.append(ic)

        # Dedup
        seen={}
        for op in results:
            k=(op["symbol"],op["strategy"])
            if k not in seen or op["rr_val"]>seen[k]["rr_val"]: seen[k]=op
        return list(seen.values()),None
    except Exception as e:
        print(f"[scan_one] {symbol}: {e}")
        return [],None


# ── Main ──────────────────────────────────────────────────────────────────
def run_opportunity_scanner(min_dte=15, max_dte=45, preferred_wing=5.0, max_workers=15, sector_filter=None):
    symbols=_all_symbols()
    if sector_filter:
        try:
            from ..services.sector_service import get_symbol_sector
            symbols = [s for s in symbols if get_symbol_sector(s) == sector_filter]
        except: pass
    wings=[preferred_wing]+[w for w in [5.0,2.5,10.0] if w!=preferred_wing]  # min $2.5 — never $1 wide
    results=[]; errors=[]

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futs={ex.submit(_scan_one,sym,min_dte,max_dte,wings):sym for sym in symbols}
        from ..services.bounded_wait import bounded_as_completed
        for fut, sym in bounded_as_completed(futs, timeout=90,
                on_timeout=lambda ks: print(f"[opportunity_scanner] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                errors.append(f"{sym}:timeout")
                continue
            try:
                ops,_=fut.result(timeout=20)
                results.extend(ops)
            except concurrent.futures.TimeoutError:
                errors.append(f"{sym}:timeout")
            except Exception as e:
                errors.append(f"{sym}:{str(e)[:50]}")
    finally:
        ex.shutdown(wait=False)

    qo={"A":0,"B":1,"C":2}
    results.sort(key=lambda x:(qo.get(x["quality"],3),-x.get("rr_val",0)))

    rb={"TRENDING":      sum(1 for r in results if r["regime"]=="TRENDING"),
        "MEAN_REVERSION":sum(1 for r in results if r["regime"]=="MEAN_REVERSION")}
    det={
        "UPTREND_setups":    sum(1 for r in results if r["regime"]=="TRENDING"       and r["bias"]=="Bullish"),
        "DOWNTREND_setups":  sum(1 for r in results if r["regime"]=="TRENDING"       and r["bias"]=="Bearish"),
        "OVERSOLD_setups":   sum(1 for r in results if r["regime"]=="MEAN_REVERSION" and r["bias"]=="Bullish"),
        "OVERBOUGHT_setups": sum(1 for r in results if r["regime"]=="MEAN_REVERSION" and r["bias"]=="Bearish"),
        "CONDOR_setups":     sum(1 for r in results if r["strategy"]=="Iron Condor"),
        "EXHAUSTION_condors":sum(1 for r in results if r["strategy"]=="Iron Condor"),
        "EXHAUSTION_bear_calls":sum(1 for r in results if r["regime"]=="MEAN_REVERSION" and "Bear" in r.get("strategy","") and r.get("adx",0)>=40),
    }
    return {"opportunities":results,"summary":{
        "total_found":len(results),"symbols_scanned":len(symbols),
        "errors":errors[:10],"regime_breakdown":rb,"detail_breakdown":det}}
