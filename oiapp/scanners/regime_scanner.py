# oiapp/scanners/regime_scanner.py
"""
Watchlist Regime Scanner
Runs daily at 7AM. Computes for every symbol in DB:
  - Regime: TRENDING_UP / TRENDING_DOWN / MEAN_REVERSION_BULL / MEAN_REVERSION_BEAR / SIDEWAYS
  - Confidence score 0-100
  - Key signals: RSI14, RSI-EMA90 diff, EMA20/50, MACD, ADX, momentum move, post-earnings flag
  - Stores results in regime_scan table
"""
import sqlite3, math, json, time
from ..services.fundamentals import get_beta
from pathlib import Path
from datetime import date, datetime
from typing import Optional, Dict

def _get_wl_symbols(wl_id):
    """Return list of symbols from watchlist, or None to use default symbols table."""
    if not wl_id:
        return None
    try:
        import sqlite3 as _sq
        from pathlib import Path as _P
        db = str(_P(__file__).resolve().parents[2] / "options_data.db")
        con = _sq.connect(db)
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (int(wl_id),)
        ).fetchall()
        con.close()
        return [r[0] for r in rows] if rows else None
    except:
        return None


DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")

def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    try:
        if hasattr(value, 'item') and not isinstance(value, (str, bytes, bytearray)):
            return _json_safe(value.item())
    except Exception:
        pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value

def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

# ── Ensure table exists ────────────────────────────────────────────────────
def _ensure_table():
    con = _conn()
    con.execute("""
        CREATE TABLE IF NOT EXISTS regime_scan (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_date    TEXT NOT NULL,
            symbol       TEXT NOT NULL,
            regime       TEXT,
            confidence   INTEGER,
            bias         TEXT,
            rsi          REAL,
            rsi_diff     REAL,
            macd         TEXT,
            adx          REAL,
            ema_trend    TEXT,
            momentum_move TEXT,
            post_earnings INTEGER DEFAULT 0,
            earn_days    INTEGER,
            beta         REAL,
            earn_date    TEXT,
            earn_score   INTEGER DEFAULT 0,
            signals_json TEXT,
            updated      TEXT,
            UNIQUE(scan_date, symbol)
        )""")
    # Non-destructive migrations for older DBs
    for sql in (
        "ALTER TABLE regime_scan ADD COLUMN earn_date TEXT",
        "ALTER TABLE regime_scan ADD COLUMN earn_score INTEGER DEFAULT 0",
        "ALTER TABLE regime_scan ADD COLUMN rsi_trend TEXT",
        "ALTER TABLE regime_scan ADD COLUMN weekly_trend TEXT",
        "ALTER TABLE regime_scan ADD COLUMN confluence TEXT",
    ):
        try:
            con.execute(sql)
        except Exception:
            pass
    con.commit(); con.close()

# ── Technical computation ──────────────────────────────────────────────────

def _get_futures_signal(symbol):
    """Get futures OI buildup signal for regime scoring."""
    try:
        from ..services.futures_oi import analyze_oi_buildup
        result = analyze_oi_buildup(symbol)
        return {
            "signal": result.get("signal","NO_DATA"),
            "bias": result.get("bias","Unknown"),
            "score": result.get("score", 0),
            "futures_symbol": result.get("futures_symbol"),
            "description": result.get("description",""),
        }
    except:
        return {"signal":"NO_DATA","bias":"Unknown","score":0,"futures_symbol":None,"description":""}


def _compute_iv_rank(symbol, closes):
    """
    Compute IV Rank proxy using historical volatility.
    IVR = (current_30d_HV - 1y_low_HV) / (1y_high_HV - 1y_low_HV) × 100
    Returns 0-100 (high = elevated volatility = good for selling premium).
    """
    import numpy as np
    if len(closes) < 60:
        return None
    
    # Compute rolling 20-day HV (annualized)
    log_returns = np.log(np.array(closes[1:]) / np.array(closes[:-1]))
    window = 20
    hvs = []
    for i in range(window, len(log_returns)):
        hv = np.std(log_returns[i-window:i]) * (252**0.5) * 100
        hvs.append(hv)
    
    if len(hvs) < 30:
        return None
    
    current_hv = hvs[-1]
    hv_high = max(hvs)
    hv_low = min(hvs)
    
    if hv_high == hv_low:
        return 50
    
    iv_rank = (current_hv - hv_low) / (hv_high - hv_low) * 100
    return round(max(0, min(100, iv_rank)), 1)

def _compute_weekly_regime(symbol: str) -> Optional[Dict]:
    """Lightweight weekly-timeframe trend classification -- deliberately
    NOT a full duplicate of _compute_regime_ta's 300+ line daily analysis;
    just enough EMA-trend structure to answer the one question that
    matters for cross-timeframe confluence: is the weekly picture
    trending, or genuinely range-bound? That's what should decide between
    a directional credit spread and an Iron Condor, not daily structure
    alone (see _suggest_strategies)."""
    try:
        import yfinance as yf
        h = yf.Ticker(symbol).history(period="5y", interval="1wk")
        if h is None or h.empty or len(h) < 52:
            return None
        C = h["Close"].tolist()
        n = len(C) - 1

        def ema(a, p):
            k = 2 / (p + 1)
            o = list(a)
            for i in range(1, len(o)):
                o[i] = a[i] * k + o[i - 1] * (1 - k)
            return o

        e20 = ema(C, 20)
        e50 = ema(C, 50)
        e20_slope = (e20[n] - e20[max(0, n - 4)]) / 4
        e50_slope = (e50[n] - e50[max(0, n - 8)]) / 8
        spread_pct = abs(e20[n] - e50[n]) / max(1e-9, e50[n])

        # ── Recent weekly shock (new): a single dramatic weekly candle --
        # e.g. a sharp selloff after a long uptrend -- won't move a
        # 20/50-week EMA structure enough to flip the trend classification
        # above for a while, by design (EMAs lag). But that single candle
        # can be exactly the kind of exhaustion/reversal signal a faster,
        # candle-reactive indicator (like a custom UAE-style script) picks
        # up immediately. Surfacing it explicitly here means a still-
        # "UPTREND"-classified symbol with a violent recent down week
        # shows up as a real caution flag, not silently invisible just
        # because the slower EMA structure hasn't caught up yet.
        pct_changes = [(C[i] - C[i-1]) / C[i-1] * 100 for i in range(1, len(C))]
        last_week_chg = pct_changes[-1] if pct_changes else 0.0
        recent_vol = (sum(abs(x) for x in pct_changes[-20:]) / min(20, len(pct_changes))) if pct_changes else 1.0
        shock_threshold = max(3.0, recent_vol * 2.2)
        weekly_shock = None
        if abs(last_week_chg) > shock_threshold:
            weekly_shock = {
                "direction": "DOWN" if last_week_chg < 0 else "UP",
                "chg_pct": round(last_week_chg, 2),
                "threshold_pct": round(shock_threshold, 2),
            }

        if e20[n] > e50[n] and e20_slope > 0 and e50_slope > 0:
            trend = "UPTREND"
        elif e20[n] < e50[n] and e20_slope < 0 and e50_slope < 0:
            trend = "DOWNTREND"
        elif spread_pct < 0.03 and abs(e50_slope) < e50[n] * 0.002:
            # EMAs close together AND the STRUCTURAL (slower) EMA50 is
            # roughly flat -- genuinely range-bound. Deliberately checking
            # e50's slope here, not e20's: EMA20 is short enough to
            # legitimately oscillate up and down even within a genuinely
            # sideways range (confirmed directly: a synthetic pure
            # oscillation with zero net drift was misclassified as
            # "MILD_DOWN" when this checked e20's slope instead, simply
            # because the most recent few weeks happened to be on a local
            # downswing within the range).
            trend = "SIDEWAYS"
        elif e20[n] > e50[n]:
            trend = "MILD_UP"
        elif e20[n] < e50[n]:
            trend = "MILD_DOWN"
        else:
            trend = "SIDEWAYS"

        return {
            "trend": trend,
            "weekly_shock": weekly_shock,
            "close": round(C[n], 2),
            "ema20": round(e20[n], 2),
            "ema50": round(e50[n], 2),
            "bars": len(C),
        }
    except Exception:
        return None


def _compute_regime_ta(symbol):
    """Full TA computation returning regime, confidence, signals."""
    try:
        import yfinance as yf, time as _time

        def _yf_history(sym, period="6mo", interval="1d"):
            """Fetch with one auto-retry on 401/session errors."""
            for attempt in range(2):
                try:
                    h = yf.Ticker(sym).history(period=period, interval=interval)
                    if h is not None and not h.empty: return h
                except Exception as _e:
                    if attempt == 0 and ("401" in str(_e) or "crumb" in str(_e).lower()):
                        try: yf.download("SPY", period="1d", progress=False)
                        except: pass
                        _time.sleep(0.3); continue
            return None

        def _yf_calendar(sym):
            try: return yf.Ticker(sym).calendar
            except: return None

        def _yf_earn_dates(sym):
            try:
                ed = yf.Ticker(sym).earnings_dates
                return ed if ed is not None and not ed.empty else None
            except: return None

        # 6mo (~125 bars) was nowhere near enough for EMA(RSI,90) to
        # converge -- directly measured elsewhere (scanner_builder.py) that
        # this needs ~500-540 bars before the value stops being distorted
        # by under-convergence. 3y comfortably clears that.
        _raw = _yf_history(symbol, period="3y", interval="1d")
        df = _raw
        if df is None or df.empty or len(df) < 60:
            return None
        C = df["Close"].tolist()
        H = df["High"].tolist()
        L = df["Low"].tolist()
        V = df["Volume"].tolist()
        n = len(C) - 1

        # ── EMA helper ──────────────────────────────────────────────────
        def ema(a, p):
            k = 2/(p+1); o = list(a)
            for i in range(1, len(o)): o[i] = a[i]*k + o[i-1]*(1-k)
            return o

        # ── RSI-14 ──────────────────────────────────────────────────────
        def rsi_series(c, p=14):
            out = [50.0]*len(c)
            if len(c) < p+1: return out
            g = l = 0.0
            for i in range(1, p+1):
                d = c[i]-c[i-1]
                if d > 0: g += d
                else: l -= d
            ag, al = g/p, l/p
            out[p] = 100 if al == 0 else 100-100/(1+ag/al)
            for i in range(p+1, len(c)):
                d = c[i]-c[i-1]
                ag = (ag*(p-1)+max(d,0))/p
                al = (al*(p-1)+max(-d,0))/p
                out[i] = 100 if al == 0 else 100-100/(1+ag/al)
            return out

        rsi_vals  = rsi_series(C)
        ema90_rsi = ema(rsi_vals, 90)
        rsi       = round(rsi_vals[n], 1)
        rsi_diff  = round(rsi_vals[n] - ema90_rsi[n], 1)

        # ── EMAs ────────────────────────────────────────────────────────
        e9  = ema(C, 9);  e20 = ema(C, 20); e50 = ema(C, 50)
        e9v = round(e9[n],2); e20v = round(e20[n],2); e50v = round(e50[n],2)

        # EMA slopes (5-bar)
        e20_slope = (e20[n] - e20[max(0,n-5)]) / 5
        e50_slope = (e50[n] - e50[max(0,n-10)]) / 10
        e9_slope  = (e9[n]  - e9[max(0,n-3)])   / 3

        if e20[n] > e50[n] and e20_slope > 0 and e50_slope > 0:
            ema_trend = "UPTREND"
        elif e20[n] < e50[n] and e20_slope < 0 and e50_slope < 0:
            ema_trend = "DOWNTREND"
        elif e20[n] > e50[n]:
            ema_trend = "MILD_UP"
        elif e20[n] < e50[n]:
            ema_trend = "MILD_DOWN"
        else:
            ema_trend = "SIDEWAYS"

        # ── MACD (12/26/9) ──────────────────────────────────────────────
        e12 = ema(C,12); e26 = ema(C,26)
        ml  = [a-b for a,b in zip(e12,e26)]
        ms  = ema(ml, 9)
        mh  = [a-b for a,b in zip(ml,ms)]
        macd_hist     = mh[n]
        macd_hist_1d  = mh[n-1] if n > 0 else 0
        if   macd_hist > 0 and macd_hist > macd_hist_1d: macd_sig = "BULLISH"
        elif macd_hist < 0 and macd_hist < macd_hist_1d: macd_sig = "BEARISH"
        elif macd_hist > 0:                               macd_sig = "BULL_FADE"
        elif macd_hist < 0:                               macd_sig = "BEAR_FADE"
        else:                                             macd_sig = "NEUTRAL"

        # ── ADX (14) ────────────────────────────────────────────────────
        adx_val = 20; pdi = 25; ndi = 25; pdi_d = 0; ndi_d = 0
        try:
            tr  = [max(H[i]-L[i],abs(H[i]-C[i-1]),abs(L[i]-C[i-1])) for i in range(1,len(C))]
            pdm = [max(H[i]-H[i-1],0) if(H[i]-H[i-1])>(L[i-1]-L[i]) else 0 for i in range(1,len(C))]
            ndm = [max(L[i-1]-L[i],0) if(L[i-1]-L[i])>(H[i]-H[i-1]) else 0 for i in range(1,len(C))]
            def wild(a, p=14):
                o = [0.0]*len(a)
                if len(a)<p: return o
                o[p-1] = sum(a[:p])
                for i in range(p,len(a)): o[i] = o[i-1]-o[i-1]/p+a[i]
                return o
            sp=wild(pdm); sn=wild(ndm); st=wild(tr)
            pdi_a=[100*s/t if t else 0 for s,t in zip(sp,st)]
            ndi_a=[100*s/t if t else 0 for s,t in zip(sn,st)]
            dx   =[100*abs(a-b)/(a+b) if(a+b) else 0 for a,b in zip(pdi_a,ndi_a)]
            if len(dx)>=14:
                av=[0.0]*len(dx); av[13]=sum(dx[:14])/14
                for i in range(14,len(dx)): av[i]=av[i-1]-av[i-1]/14+dx[i]/14
                adx_val = min(100,max(0,round(av[n-1],1)))
                pdi     = round(pdi_a[n-1],1)
                ndi     = round(ndi_a[n-1],1)
                pdi_d   = round(pdi_a[n-1]-pdi_a[max(0,n-8)],1)
                ndi_d   = round(ndi_a[n-1]-ndi_a[max(0,n-8)],1)
        except: pass

        # Write-through to the technical_snapshot cache: this function runs
        # once daily (7AM) with the most complete lookback (3y) of any of
        # the independent RSI/EMA/MACD/ADX implementations in this
        # codebase. Populating the cache here (with a genuinely complete
        # record, not a partial one -- a partial write would otherwise
        # incorrectly satisfy get_or_compute_technical_snapshot's "already
        # computed today" check and permanently block the fuller
        # computation for the rest of the day) means other consumers
        # running LATER the same day (trade_opportunity_scanner's _get_ta,
        # scanner queries) can read an already-accurate value instead of
        # redoing the same work with their own, often shorter/less-
        # converged lookback windows. Best-effort: never let a cache write
        # failure interrupt the regime scan itself.
        try:
            from ..services.technical_snapshot import queue_technical_snapshot_write, is_snapshot_complete_today
            if not is_snapshot_complete_today(symbol, "1d"):
                valid_rsi_bars_cache = int(sum(1 for v in rsi_vals if v is not None))
                rsidiff90_ok = valid_rsi_bars_cache >= 540
                queue_technical_snapshot_write(symbol, "1d", df.index[-1].strftime("%Y-%m-%d"), {
                    "close": round(C[n], 4),
                    "rsi3": None,  # not computed here -- technical_snapshot's own run fills this if it runs later
                    "rsi14": rsi,
                    "ema_rsi14_13": None,
                    "ema_rsi14_90": round(ema90_rsi[n], 4) if rsidiff90_ok else None,
                    "rsidiff90": rsi_diff if rsidiff90_ok else None,
                    "rsidiff90_trusted": rsidiff90_ok,
                    "ema9": round(e9[n], 4), "ema20": round(e20[n], 4), "ema50": round(e50[n], 4),
                    "ema60": None, "ema200": None,
                    "bar_strength_vs_ema60": None,
                    "macd": round(ml[n], 4), "macd_signal": round(ms[n], 4), "macd_hist": round(macd_hist, 4),
                    "adx": adx_val, "di_plus": pdi, "di_minus": ndi,
                    "sr_support": None, "sr_resistance": None,
                })
        except Exception:
            pass

        # ── BB%B ────────────────────────────────────────────────────────
        sma20  = [sum(C[max(0,i-19):i+1])/min(20,i+1) for i in range(len(C))]
        std20  = [math.sqrt(sum((C[max(0,i-19+k)]-sma20[i])**2 for k in range(min(20,i+1)))/min(20,i+1)) for i in range(len(C))]
        bb_pct = max(0,min(100, (C[n]-sma20[n]+2*std20[n])/(4*std20[n])*100 if std20[n]>0 else 50))

        # ── Volume averages — compute FIRST (used in move + spike detection) ─
        avg_vol_20 = sum(V[max(0,n-20):n]) / min(20, n) if n > 0 else 1
        vol_ratio  = round(V[n] / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0

        # ── Momentum move signal: |%change| > EMA(Abs(%change), 60) * 2 ─
        dates_idx = df.index  # DatetimeIndex
        pct_abs   = [abs((C[i]-C[i-1])/C[i-1]*100) if i>0 else 0 for i in range(len(C))]
        ema60_abs = ema(pct_abs, 60)
        today_chg = (C[n]-C[n-1])/C[n-1]*100 if n>0 else 0
        mom_thr   = ema60_abs[n]*2
        is_mom_move  = abs(today_chg) > mom_thr
        mom_move_str = f"{today_chg:+.2f}% (thr:{mom_thr:.2f}%)" if is_mom_move else None

        # ── All significant momentum moves in last 10 trading days ───────
        recent_mom     = None
        large_moves_10d = []
        for lookback in range(1, 11):
            idx = n - lookback
            if idx < 1: break
            chg = (C[idx]-C[idx-1])/C[idx-1]*100
            thr = ema60_abs[idx]*2
            if abs(chg) > thr:
                # Get the actual date for this bar
                try:
                    bar_date = dates_idx[idx].strftime("%Y-%m-%d")
                except:
                    bar_date = None
                vol_r = round(V[idx] / avg_vol_20, 1) if avg_vol_20 > 0 else 1.0
                move = {
                    "bars_ago":    lookback,
                    "date":        bar_date,
                    "chg_pct":     round(chg, 2),
                    "direction":   "UP" if chg > 0 else "DOWN",
                    "threshold":   round(thr, 2),
                    "close":       round(C[idx], 2),
                    "volume_ratio":vol_r,
                }
                large_moves_10d.append(move)
                if recent_mom is None:
                    recent_mom = move

        # Largest move (by abs %) in the 10d window
        largest_move_10d = max(large_moves_10d, key=lambda x: abs(x["chg_pct"])) if large_moves_10d else None

        # ── Volume spike: any bar in last 10d where vol > avg_vol_20 * 1.2 ─
        # Matching ThinkScript: Highest((vol > Average(vol,20)*1.2), 10) == 1
        vol_spike_10d  = False
        vol_spike_info = None
        for lookback in range(1, 11):
            idx = n - lookback
            if idx < 0: break
            bar_vol    = V[idx]
            vol_thresh = avg_vol_20 * 1.2
            if bar_vol > vol_thresh:
                vol_spike_10d = True
                try:
                    spike_date = dates_idx[idx].strftime("%Y-%m-%d")
                except:
                    spike_date = None
                vol_spike_info = {
                    "bars_ago":    lookback,
                    "date":        spike_date,
                    "volume":      int(bar_vol),
                    "avg_vol_20":  int(avg_vol_20),
                    "vol_ratio":   round(bar_vol / avg_vol_20, 2),
                    "close":       round(C[idx], 2),
                }
                break  # most recent spike only

        # ── Earnings check — multiple fallback methods ──────────────────
        earn_days = 999; post_earnings = False; earn_date_str = None
        tk2 = yf.Ticker(symbol)
        try:
            cal = _yf_calendar(symbol)
            nd = None
            if isinstance(cal, dict):
                nd = cal.get("Earnings Date") or cal.get("earningsDate")
                if nd and hasattr(nd, "__iter__") and not isinstance(nd, str):
                    nd = list(nd)[0]
            elif cal is not None and hasattr(cal, "empty") and not cal.empty:
                for col in ["Earnings Date","earningsDate","earnings_date"]:
                    if col in cal.columns:
                        nd = cal[col].iloc[0]; break
            if nd is not None:
                # Handle list of dates — pick first future date
                if hasattr(nd, "__iter__") and not isinstance(nd, str):
                    nd_list = sorted([x for x in nd], key=str)
                    nd = None
                    today_dt2 = date.today()
                    for nd_cand in nd_list:
                        try:
                            nd_d = nd_cand.date() if hasattr(nd_cand,"date") else datetime.strptime(str(nd_cand)[:10],"%Y-%m-%d").date()
                            if (nd_d - today_dt2).days >= 1:
                                nd = nd_d; break
                        except: continue
                if nd is not None:
                    if hasattr(nd, "date"): nd = nd.date()
                    elif isinstance(nd, str): nd = datetime.strptime(nd[:10], "%Y-%m-%d").date()
                    d_from_today = (nd - date.today()).days
                    if d_from_today >= 1:  # only store if future
                        earn_days = d_from_today
                        earn_date_str = str(nd)
                        post_earnings = False
        except: pass

        if earn_days == 999:
            try:
                ed_df = _yf_earn_dates(symbol)
                if ed_df is not None and not ed_df.empty:
                    today_dt = date.today()
                    # Collect ALL dates, find earliest FUTURE date (d_diff >= 1)
                    future_dates = []
                    for idx_val in ed_df.index:
                        try:
                            ed_val = idx_val.date() if hasattr(idx_val,"date") else idx_val
                            d_diff = (ed_val - today_dt).days
                            if d_diff >= 1:  # strictly future only
                                future_dates.append((d_diff, str(ed_val)))
                        except: continue
                    if future_dates:
                        future_dates.sort(key=lambda x: x[0])  # nearest first
                        earn_days, earn_date_str = future_dates[0]
                        post_earnings = False
            except: pass

        # Post-earnings: check if most recent actual is within last 5 days
        if earn_days == 999:
            try:
                ed_df2 = _yf_earn_dates(symbol)
                if ed_df2 is not None and not ed_df2.empty:
                    today_dt = date.today()
                    for idx_val in ed_df2.index:
                        try:
                            ed_val = idx_val.date() if hasattr(idx_val,"date") else idx_val
                            d_diff = (ed_val - today_dt).days
                            if -5 <= d_diff <= 0:
                                earn_days = d_diff
                                earn_date_str = str(ed_val)
                                post_earnings = True
                                break
                        except: continue
            except: pass

        spot = round(C[n], 2)
        beta = get_beta(symbol)

        # ── Earnings score (-10 to +10) ────────────────────────────────────
        earn_score = 0; earn_score_notes = []
        try:
            try:
                info2_full = tk2.info or {}
            except: info2_full = {}
            def _gf2(k, default=None):
                # Try camelCase, lowercase, and fast_info attribute
                for key in [k, k[0].lower()+k[1:], k.lower()]:
                    v = info2_full.get(key)
                    if v is not None: return v
                # Also try fast_info attribute (object)
                try:
                    fi = tk2.fast_info
                    if hasattr(fi, k): return getattr(fi, k)
                except: pass
                return default
            try: price_52h2 = float(_gf2("fiftyTwoWeekHigh") or spot)
            except: price_52h2 = spot
            try: price_52l2 = float(_gf2("fiftyTwoWeekLow") or spot)
            except: price_52l2 = spot
            try: rec_mean2  = float(_gf2("recommendationMean") or 3.0)
            except: rec_mean2 = 3.0
            try: target2    = float(_gf2("targetMeanPrice") or spot)
            except: target2 = spot
            # Analyst consensus
            if   rec_mean2 <= 1.5: earn_score += 3; earn_score_notes.append("Strong Buy consensus")
            elif rec_mean2 <= 2.2: earn_score += 2; earn_score_notes.append("Buy consensus")
            elif rec_mean2 <= 2.8: earn_score += 1
            elif rec_mean2 <= 3.5: earn_score -= 1
            else:                  earn_score -= 2; earn_score_notes.append("Sell consensus")
            # Price target upside
            if spot > 0:
                upside2 = (target2 - spot) / spot * 100
                if   upside2 > 20:  earn_score += 2; earn_score_notes.append(f"Target +{upside2:.0f}%")
                elif upside2 > 10:  earn_score += 1
                elif upside2 < -5:  earn_score -= 2; earn_score_notes.append(f"Target {upside2:.0f}%")
            # EPS beat streak
            try:
                ed_df2 = _yf_earn_dates(symbol)
                if ed_df2 is not None and not ed_df2.empty:
                    actuals2 = ed_df2[ed_df2["Reported EPS"].notna()].head(6)
                    surps2 = []
                    for _, row2 in actuals2.iterrows():
                        sp2 = row2.get("Surprise(%)")
                        if sp2 is not None:
                            try:
                                fv = float(sp2)
                                if not math.isnan(fv): surps2.append(fv)
                            except: pass
                        elif row2.get("EPS Estimate") and row2.get("Reported EPS"):
                            try:
                                est2 = float(row2["EPS Estimate"]); act2 = float(row2["Reported EPS"])
                                if est2 != 0: surps2.append((act2-est2)/abs(est2)*100)
                            except: pass
                    if surps2:
                        streak2 = sum(1 for s in surps2 if s > 0)
                        if   streak2 >= 4: earn_score += 2; earn_score_notes.append(f"{streak2}Q beat streak")
                        elif streak2 >= 2: earn_score += 1
                        avg_surp2 = sum(surps2)/len(surps2)
                        if avg_surp2 > 10: earn_score += 1; earn_score_notes.append(f"Avg EPS surprise +{avg_surp2:.0f}%")
                        elif avg_surp2 < -5: earn_score -= 1
            except: pass
            # 52w range position
            if price_52h2 > price_52l2 and spot > 0:
                pct_rng2 = (spot - price_52l2) / (price_52h2 - price_52l2) * 100
                if   pct_rng2 > 90: earn_score -= 1; earn_score_notes.append("Near 52w high")
                elif pct_rng2 > 65: earn_score += 1
                elif pct_rng2 < 15: earn_score += 1; earn_score_notes.append("Near 52w low")
            # Revenue growth
            rev_g2 = info2_full.get("revenueGrowth")
            if rev_g2 is not None:
                try:
                    rgp = float(rev_g2)*100
                    if   rgp > 15: earn_score += 1; earn_score_notes.append(f"Rev +{rgp:.0f}%")
                    elif rgp < -5: earn_score -= 1
                except: pass
            earn_score = max(-10, min(10, earn_score))
        except: pass

        # ══════════════════════════════════════════════════════════════════
        # REGIME CLASSIFICATION
        # ══════════════════════════════════════════════════════════════════
        signals = []
        # Compute IV Rank
        iv_rank = _compute_iv_rank(symbol, list(C))
        
        score   = 0  # -100 to +100, positive = bullish trend, negative = bearish trend
        abs_diff = abs(rsi_diff)

        # ── RSI-EMA diff (primary signal) ────────────────────────────────
        if rsi_diff >= 20:
            signals.append(f"RSI-EMA +{rsi_diff:.0f} OVERBOUGHT")
            score -= 20  # overbought = bearish lean for MR
        elif rsi_diff >= 10:
            signals.append(f"RSI-EMA +{rsi_diff:.0f} elevated")
            score -= 8
        elif rsi_diff <= -20:
            signals.append(f"RSI-EMA {rsi_diff:.0f} OVERSOLD")
            score += 20  # oversold = bullish lean for MR
        elif rsi_diff <= -10:
            signals.append(f"RSI-EMA {rsi_diff:.0f} depressed")
            score += 8
        else:
            signals.append(f"RSI-EMA {rsi_diff:.0f} neutral")

        # ── EMA trend (directional bias) ────────────────────────────────
        if ema_trend == "UPTREND":
            signals.append(f"EMA20({e20v:.0f})>EMA50({e50v:.0f}) UPTREND")
            score += 25
        elif ema_trend == "DOWNTREND":
            signals.append(f"EMA20({e20v:.0f})<EMA50({e50v:.0f}) DOWNTREND")
            score -= 25
        elif ema_trend == "MILD_UP":
            signals.append(f"EMA20>EMA50 mild up")
            score += 12
        elif ema_trend == "MILD_DOWN":
            signals.append(f"EMA20<EMA50 mild down")
            score -= 12

        # EMA9 momentum
        if e9[n] > e20[n] and e9_slope > 0:
            signals.append(f"EMA9({e9v:.0f}) above EMA20 rising")
            score += 8
        elif e9[n] < e20[n] and e9_slope < 0:
            signals.append(f"EMA9({e9v:.0f}) below EMA20 falling")
            score -= 8

        # ── MACD ────────────────────────────────────────────────────────
        if macd_sig == "BULLISH":
            signals.append("MACD↑ bullish histogram growing")
            score += 12
        elif macd_sig == "BEARISH":
            signals.append("MACD↓ bearish histogram growing")
            score -= 12
        elif macd_sig == "BULL_FADE":
            signals.append("MACD hist positive but fading")
            score += 4
        elif macd_sig == "BEAR_FADE":
            signals.append("MACD hist negative but fading")
            score -= 4

        # ── ADX ─────────────────────────────────────────────────────────
        if adx_val >= 35:
            signals.append(f"ADX {adx_val:.0f} STRONG trend")
            score += 12 if score > 0 else -12  # amplifies direction
        elif adx_val >= 25:
            signals.append(f"ADX {adx_val:.0f} trending")
            score += 6 if score > 0 else -6
        elif adx_val >= 40:
            signals.append(f"ADX {adx_val:.0f} EXHAUSTION zone")
            # ADX exhaustion = trend ending, flip toward MR
            score = score * 0.6  # dampen directional score
        elif adx_val < 18:
            signals.append(f"ADX {adx_val:.0f} weak — no trend")

        # PDI / NDI convergence
        if pdi_d < -3 and ndi_d > 3:
            signals.append(f"PDI↓{pdi_d:.0f} NDI↑{ndi_d:.0f} bearish convergence")
            score -= 10
        elif ndi_d < -3 and pdi_d > 3:
            signals.append(f"NDI↓{ndi_d:.0f} PDI↑{pdi_d:.0f} bullish convergence")
            score += 10

        # ── BB%B extreme ─────────────────────────────────────────────────
        if bb_pct > 80:
            signals.append(f"BB%B {bb_pct:.0f}% near upper band")
            score -= 6
        elif bb_pct < 20:
            signals.append(f"BB%B {bb_pct:.0f}% near lower band")
            score += 6

        # ── Momentum move ────────────────────────────────────────────────
        if is_mom_move:
            if today_chg > 0:
                signals.append(f"🔥 Momentum UP {today_chg:+.2f}% (2× EMA threshold)")
                score += 15
            else:
                signals.append(f"🔥 Momentum DOWN {today_chg:.2f}% (2× EMA threshold)")
                score -= 15
        elif recent_mom:
            ba = recent_mom['bars_ago']
            direction_emoji = "↑" if recent_mom['direction']=='UP' else "↓"
            signals.append(f"Recent momentum {direction_emoji} {recent_mom['chg_pct']:+.2f}% ({ba}d ago)")
            score_adj = 8 if recent_mom['direction']=='UP' else -8
            score_adj *= max(0.3, 1 - ba*0.15)  # decay by recency
            score += score_adj

        # ── Post-earnings ────────────────────────────────────────────────
        if post_earnings:
            signals.append(f"📊 Post-earnings (reported {abs(earn_days)}d ago)")
            # Post-earnings amplifies the current direction
            score *= 1.2

        # ── Volume ──────────────────────────────────────────────────────
        if vol_ratio >= 2.0:
            signals.append(f"Vol {vol_ratio:.1f}× — high conviction move")
            score += 5 if score > 0 else -5
        elif vol_ratio >= 1.5:
            signals.append(f"Vol {vol_ratio:.1f}× above avg")

        # ── Final regime assignment ───────────────────────────────────────
        # Add earn_score and futures OI score to total
        score += earn_score
        try:
            _fut_sig = _get_futures_signal(symbol)
            score += _fut_sig.get("score", 0)
        except: pass
        score = max(-100, min(100, round(score)))

        adx_is_trending  = adx_val >= 22
        adx_is_strong    = adx_val >= 30
        adx_exhaustion   = adx_val >= 40
        abs_diff_extreme = abs_diff >= 15   # ±15 = significant overbought/oversold
        abs_diff_major   = abs_diff >= 20   # ±20 = your primary threshold

        # ── PRIORITY 1: RSI-EMA extremes override trend signals ──────────
        # If diff >= +20 (OVERBOUGHT), this IS a mean reversion bear candidate
        # regardless of what EMA trend and MACD say.
        # Sub-classify: ADX high + trending = "overbought IN trend" vs pure MR
        if rsi_diff >= 20:
            if adx_exhaustion:
                # ADX >= 40 with overbought = exhaustion — strongest MR signal
                regime = "MEAN_REV_BEAR"
                bias   = "Bearish"
                signals.insert(0, f"ADX {adx_val:.0f} EXHAUSTION + RSI-EMA +{rsi_diff:.0f} → strong fade setup")
            elif adx_is_trending and ema_trend in ("UPTREND","MILD_UP"):
                # Trending but overbought: classify as TRENDING_UP_OB
                # Still a mean reversion fade opportunity, just with trend wind against it
                regime = "TRENDING_UP_OB"
                bias   = "Bearish"   # fade the overbought condition
                signals.insert(0, f"RSI-EMA +{rsi_diff:.0f} OVERBOUGHT in uptrend — fade candidate")
            else:
                regime = "MEAN_REV_BEAR"
                bias   = "Bearish"
                signals.insert(0, f"RSI-EMA +{rsi_diff:.0f} OVERBOUGHT — mean reversion bear")

        elif rsi_diff <= -20:
            if adx_exhaustion:
                regime = "MEAN_REV_BULL"
                bias   = "Bullish"
                signals.insert(0, f"ADX {adx_val:.0f} EXHAUSTION + RSI-EMA {rsi_diff:.0f} → strong bounce setup")
            elif adx_is_trending and ema_trend in ("DOWNTREND","MILD_DOWN"):
                regime = "TRENDING_DOWN_OS"
                bias   = "Bullish"   # fade the oversold condition
                signals.insert(0, f"RSI-EMA {rsi_diff:.0f} OVERSOLD in downtrend — bounce candidate")
            else:
                regime = "MEAN_REV_BULL"
                bias   = "Bullish"
                signals.insert(0, f"RSI-EMA {rsi_diff:.0f} OVERSOLD — mean reversion bull")

        # ── PRIORITY 2: Elevated (±12-19) — lean MR but not extreme ─────
        elif rsi_diff >= 12:
            if adx_is_strong and ema_trend == "UPTREND" and macd_sig == "BULLISH":
                regime = "TRENDING_UP"
                bias   = "Bullish"
            else:
                regime = "MILD_BEARISH"   # elevated RSI, watch for fade
                bias   = "Bearish"
                signals.insert(0, f"RSI-EMA +{rsi_diff:.0f} elevated — watching for fade")

        elif rsi_diff <= -12:
            if adx_is_strong and ema_trend == "DOWNTREND" and macd_sig == "BEARISH":
                regime = "TRENDING_DOWN"
                bias   = "Bearish"
            else:
                regime = "MILD_BULLISH"
                bias   = "Bullish"
                signals.insert(0, f"RSI-EMA {rsi_diff:.0f} depressed — watching for bounce")

        # ── PRIORITY 3: Neutral RSI-EMA — use trend signals ─────────────
        elif score >= 45 and adx_is_trending:
            regime = "TRENDING_UP"
            bias   = "Bullish"
        elif score <= -45 and adx_is_trending:
            regime = "TRENDING_DOWN"
            bias   = "Bearish"
        elif score >= 25:
            regime = "MILD_BULLISH"
            bias   = "Bullish"
        elif score <= -25:
            regime = "MILD_BEARISH"
            bias   = "Bearish"
        else:
            regime = "SIDEWAYS"
            bias   = "Neutral"

        # ── Confidence score ─────────────────────────────────────────────
        # Base: how extreme are the signals?
        conf_base = 50

        # RSI-EMA extremity is the strongest confidence driver
        if abs_diff_major:    conf_base += 20  # ≥±20: very confident
        elif abs_diff_extreme: conf_base += 12  # ±15-19: fairly confident
        elif abs_diff >= 8:    conf_base += 5

        # ADX confirms trend strength
        if adx_exhaustion:      conf_base += 12   # peak ADX = high confidence of reversal
        elif adx_is_strong:     conf_base += 8    # strong trend
        elif adx_is_trending:   conf_base += 4

        # Momentum move today = high conviction
        if is_mom_move:         conf_base += 10

        # MACD alignment with regime
        macd_aligned = (
            (bias == "Bullish" and "BULL" in macd_sig) or
            (bias == "Bearish" and "BEAR" in macd_sig)
        )
        if macd_aligned:        conf_base += 6
        else:                   conf_base -= 4   # MACD contradicts regime = less confident

        # Post-earnings = higher conviction on current price action
        if post_earnings:       conf_base += 5

        # Large recent move = more conviction
        if largest_move_10d:    conf_base += 5
        if vol_spike_10d:       conf_base += 3

        # PDI/NDI convergence confirms
        if (pdi_d < -3 and ndi_d > 3 and bias=="Bearish") or            (ndi_d < -3 and pdi_d > 3 and bias=="Bullish"):
            conf_base += 8

        confidence = min(95, max(15, round(conf_base)))

        # ── RSI momentum DIRECTION (rising/falling), not just level ──────
        # A "bearish regime" reading that's really just a lagging label on
        # a symbol whose RSI has been climbing for several bars is a much
        # weaker case for a fresh bearish trade than one where RSI is
        # actively falling too -- level alone doesn't capture this.
        rsi_lookback = min(5, n)
        rsi_now = rsi_vals[n]
        rsi_then = rsi_vals[max(0, n - rsi_lookback)]
        rsi_delta = round(rsi_now - rsi_then, 2)
        if rsi_delta > 2:
            rsi_trend = "RISING"
        elif rsi_delta < -2:
            rsi_trend = "FALLING"
        else:
            rsi_trend = "FLAT"

        # ── Weekly regime cross-check ──────────────────────────────────
        # The daily computation above, however thorough, is still only
        # ONE timeframe. A "Grade A bearish" call built entirely on daily
        # structure while the weekly trend is actually sideways or
        # improving is a materially weaker setup than one where both
        # timeframes agree -- this is what actually determines whether a
        # directional credit spread or a range-bound iron condor is the
        # better-fitting structure, not daily regime alone.
        weekly = _compute_weekly_regime(symbol)
        confluence = "UNKNOWN"
        if weekly:
            wk_trend = weekly.get("trend", "")
            daily_dir = "up" if "UP" in ema_trend or "BULL" in bias.upper() else \
                        "down" if "DOWN" in ema_trend or "BEAR" in bias.upper() else "flat"
            wk_dir = "up" if "UP" in wk_trend else "down" if "DOWN" in wk_trend else "flat"
            if wk_dir == "flat":
                confluence = "WEEKLY_SIDEWAYS"
            elif daily_dir == wk_dir:
                confluence = "AGREE"
            elif daily_dir == "flat":
                confluence = "DAILY_FLAT"
            else:
                confluence = "DISAGREE"

            # A recent weekly shock opposing the daily bias overrides
            # whatever the slower EMA-trend structure still says -- e.g. a
            # symbol still EMA-classified UPTREND (EMAs haven't caught up
            # yet) that just had a violent down week is a real warning the
            # daily-only bullish case should reflect, not silently miss.
            shock = weekly.get("weekly_shock")
            if shock and shock["direction"].lower() != daily_dir and daily_dir != "flat":
                confluence = "WEEKLY_SHOCK_AGAINST"

        return {
            "symbol":      symbol,
            "spot":        spot,
            "regime":      regime,
            "bias":        bias,
            "confidence":  confidence,
            "score":       score,
            "rsi":         rsi,
            "rsi_diff":    rsi_diff,
            "rsi_trend":   rsi_trend,
            "weekly_regime": weekly,
            "confluence":  confluence,
            "ema_trend":   ema_trend,
            "ema9":        e9v, "ema20": e20v, "ema50": e50v,
            "macd":        macd_sig,
            "adx":         adx_val,
            "pdi":         pdi, "ndi": ndi,
            "pdi_delta":   pdi_d, "ndi_delta": ndi_d,
            "bb_pct":      round(bb_pct,1),
            "vol_ratio":   vol_ratio,
            "is_mom_move": is_mom_move,
            "mom_move":    mom_move_str,
            "recent_mom":  recent_mom,
            "post_earnings":int(post_earnings),
            "earn_days":   earn_days if earn_days < 999 else None,
            "beta":        beta,
            "signals":          signals[:8],
            "large_moves_10d":  large_moves_10d,
            "largest_move_10d": largest_move_10d,
            "vol_spike_10d":    vol_spike_10d,
            "vol_spike_info":   vol_spike_info,
            "earn_score":       earn_score,
            "futures_oi_signal": _get_futures_signal(symbol),
            "earn_score_notes": earn_score_notes,
            "earn_date_str":    earn_date_str,
            "strategies":       _suggest_strategies(
                                    regime, bias, rsi_diff, adx_val,
                                    ema_trend, macd_sig,
                                    earn_days if earn_days < 999 else 999,
                                    iv_rank=iv_rank,
                                    confluence=confluence, rsi_trend=rsi_trend,
                                ),
            "iv_rank":          iv_rank if iv_rank is not None else 50,
        }
    except Exception as e:
        print(f"[regime_scan] {symbol}: {e}")
        return None


def _suggest_strategies(regime, bias, rsi_diff, adx, ema_trend, macd, earn_days, iv_rank=50,
                         confluence="UNKNOWN", rsi_trend="FLAT"):
    """
    Return top 3 strategy suggestions with probability score (0-100).
    Based on regime, RSI-EMA diff, ADX, EMA trend, MACD, earnings proximity.

    confluence/rsi_trend (new): when the weekly timeframe is genuinely
    sideways (confluence == "WEEKLY_SIDEWAYS") regardless of what the daily
    regime says, a directional credit spread is betting on a move the
    higher timeframe doesn't support -- an Iron Condor fits a genuinely
    range-bound underlying better, and can often collect comparable or
    better premium with similar POP, since it's not fighting the weekly
    structure. Also: when daily and weekly actively DISAGREE (confluence
    == "DISAGREE"), or RSI is trending opposite the proposed bias, that's
    real information the probability score should reflect, not just
    single-timeframe regime/ADX/MACD as before.
    """
    suggestions = []
    earn_risk = earn_days is not None and earn_days < 21

    def _prob(base, *modifiers):
        p = base
        for m in modifiers: p += m
        return max(10, min(95, round(p)))

    # Multi-timeframe confluence bonus/penalty -- applied to directional
    # (credit/debit) suggestions below, not to the IC suggestion (an IC
    # doesn't need directional agreement, that's the whole point of it).
    confluence_bonus = 10 if confluence == "AGREE" else \
                        -15 if confluence == "DISAGREE" else \
                        -8 if confluence == "WEEKLY_SIDEWAYS" else 0

    # IVR adjustments: high IVR boosts credit strategies, low IVR boosts debit
    ivr_credit_bonus = 8 if iv_rank and iv_rank > 60 else (4 if iv_rank and iv_rank > 40 else 0)
    ivr_debit_bonus = 8 if iv_rank and iv_rank < 30 else (4 if iv_rank and iv_rank < 45 else 0)
    ivr_credit_penalty = -6 if iv_rank and iv_rank < 25 else 0  # don't sell in low IV
    ivr_debit_penalty = -6 if iv_rank and iv_rank > 70 else 0   # don't buy in high IV

    # ── Weekly is genuinely sideways: lead with Iron Condor regardless of
    # what the daily-only regime below would otherwise suggest. This is
    # the direct fix for "Grade A bearish CS on a weekly-sideways name" --
    # the IC gets offered FIRST, ahead of (not instead of) the directional
    # ideas, so it's what a reviewer sees as the lead suggestion.
    if confluence == "WEEKLY_SIDEWAYS":
        ic_bonus = 10 if adx < 20 else 4 if adx < 28 else 0  # low ADX = genuinely range-bound, not just short-term calm
        suggestions.append({
            "strategy":    "Iron Condor",
            "type":        "credit",
            "bias":        "Neutral",
            "note":        "Weekly structure is sideways -- range-bound premium collection fits "
                            "better here than betting a direction the higher timeframe doesn't support.",
            "probability": _prob(60, ic_bonus, ivr_credit_bonus, ivr_credit_penalty),
        })

    # ── TRENDING_UP / MILD_BULLISH ─────────────────────────────────────
    if regime in ("TRENDING_UP","MILD_BULLISH","TRENDING_DOWN_OS"):
        adx_bonus  = 10 if adx>=30 else 5 if adx>=22 else 0
        macd_bonus = 8  if macd=="BULLISH" else 0
        earn_pen   = -12 if earn_risk else 0
        rsi_pen    = -8 if rsi_trend == "FALLING" else 0  # bullish call, but RSI actively fading
        suggestions.append({
            "strategy":    "Bull Put Spread",
            "type":        "credit",
            "bias":        "Bullish",
            "note":        f"Sell OTM put below EMA20. ADX {adx:.0f} confirms trend.",
            "probability": _prob(62, adx_bonus, macd_bonus, earn_pen, ivr_credit_bonus, ivr_credit_penalty, confluence_bonus, rsi_pen),
        })
        suggestions.append({
            "strategy":    "Bull Call Debit",
            "type":        "debit",
            "bias":        "Bullish",
            "note":        "Buy call on pullback to EMA9/EMA20. Ride the trend.",
            "probability": _prob(55, adx_bonus, macd_bonus, earn_pen, ivr_debit_bonus, ivr_debit_penalty, confluence_bonus, rsi_pen),
        })
        if adx >= 30 and not earn_risk:
            suggestions.append({
                "strategy":    "Covered Call / CC Spread",
                "type":        "income",
                "bias":        "Neutral-Bull",
                "note":        "Strong trend — sell OTM call for premium while holding.",
                "probability": _prob(60, adx_bonus),
            })
        else:
            suggestions.append({
                "strategy":    "Bear Put Hedge",
                "type":        "hedge",
                "bias":        "Hedge",
                "note":        "Add small bear put spread to hedge overbought risk.",
                "probability": _prob(45, earn_pen),
            })

    # ── TRENDING_DOWN / MILD_BEARISH ──────────────────────────────────
    elif regime in ("TRENDING_DOWN","MILD_BEARISH","TRENDING_UP_OB"):
        adx_bonus  = 10 if adx>=30 else 5 if adx>=22 else 0
        macd_bonus = 8  if macd=="BEARISH" else 0
        earn_pen   = -12 if earn_risk else 0
        rsi_pen    = -8 if rsi_trend == "RISING" else 0  # bearish call, but RSI actively improving -- exactly WYNN's case
        suggestions.append({
            "strategy":    "Bear Call Spread",
            "type":        "credit",
            "bias":        "Bearish",
            "note":        f"Sell OTM call above EMA20. ADX {adx:.0f} confirms trend.",
            "probability": _prob(62, adx_bonus, macd_bonus, earn_pen, ivr_credit_bonus, ivr_credit_penalty, confluence_bonus, rsi_pen),
        })
        suggestions.append({
            "strategy":    "Bear Put Debit",
            "type":        "debit",
            "bias":        "Bearish",
            "note":        "Buy put on bounce to EMA9/EMA20. Ride the downtrend.",
            "probability": _prob(55, adx_bonus, macd_bonus, earn_pen, confluence_bonus, rsi_pen),
        })
        suggestions.append({
            "strategy":    "Put Backspread",
            "type":        "debit",
            "bias":        "Bearish",
            "note":        "Buy 2 OTM puts, sell 1 ATM put. Profits on big move.",
            "probability": _prob(45, adx_bonus),
        })

    # ── MEAN_REV_BEAR (overbought) ────────────────────────────────────
    elif regime in ("MEAN_REV_BEAR",):
        diff_bonus = min(15, max(0, abs(rsi_diff)-15))  # extra bonus above 15
        adx_bonus  = 10 if adx>=35 else 5 if adx>=25 else 0
        earn_pen   = -15 if earn_risk else 0
        suggestions.append({
            "strategy":    "Bear Call Spread",
            "type":        "credit",
            "bias":        "Bearish",
            "note":        f"RSI-EMA +{rsi_diff:.0f} OVERBOUGHT. Sell call spread above resistance. High conviction fade.",
            "probability": _prob(68, diff_bonus, adx_bonus, earn_pen),
        })
        suggestions.append({
            "strategy":    "OTM Put Debit",
            "type":        "debit",
            "bias":        "Bearish",
            "note":        f"Buy put 3-5% OTM for overbought reversal. Enter on first red candle.",
            "probability": _prob(58, diff_bonus, earn_pen),
        })
        suggestions.append({
            "strategy":    "Iron Condor",
            "type":        "credit",
            "bias":        "Neutral",
            "note":        "If ADX exhaustion: sell both sides. Stock likely to range after peak.",
            "probability": _prob(55 if adx>=35 else 40, adx_bonus, earn_pen, ivr_credit_bonus),
        })

    # ── MEAN_REV_BULL (oversold) ──────────────────────────────────────
    elif regime in ("MEAN_REV_BULL",):
        diff_bonus = min(15, max(0, abs(rsi_diff)-15))
        adx_bonus  = 10 if adx>=35 else 5 if adx>=25 else 0
        earn_pen   = -15 if earn_risk else 0
        suggestions.append({
            "strategy":    "Bull Put Spread",
            "type":        "credit",
            "bias":        "Bullish",
            "note":        f"RSI-EMA {rsi_diff:.0f} OVERSOLD. Sell put spread below support. High conviction bounce.",
            "probability": _prob(68, diff_bonus, adx_bonus, earn_pen),
        })
        suggestions.append({
            "strategy":    "ATM Call Debit",
            "type":        "debit",
            "bias":        "Bullish",
            "note":        f"Buy ATM call for oversold bounce. Enter on first green candle / MACD cross.",
            "probability": _prob(58, diff_bonus, earn_pen),
        })
        suggestions.append({
            "strategy":    "Iron Condor",
            "type":        "credit",
            "bias":        "Neutral",
            "note":        "If ADX exhaustion: IC while stock consolidates after bounce.",
            "probability": _prob(50 if adx>=35 else 38, adx_bonus, earn_pen, ivr_credit_bonus),
        })

    # ── SIDEWAYS ─────────────────────────────────────────────────────
    else:
        earn_pen = -10 if earn_risk else 0
        ic_prob = 60 if adx < 20 else 50
        suggestions.append({
            "strategy":    "Iron Condor",
            "type":        "credit",
            "bias":        "Neutral",
            "note":        f"ADX {adx:.0f} weak. Range-bound — sell OTM call + OTM put.",
            "probability": _prob(ic_prob, earn_pen, ivr_credit_bonus),
        })
        suggestions.append({
            "strategy":    "Short Strangle",
            "type":        "credit",
            "bias":        "Neutral",
            "note":        "Wider risk/reward. Sell 5-8% OTM call + put.",
            "probability": _prob(50, earn_pen),
        })
        suggestions.append({
            "strategy":    "Cash-Secured Put",
            "type":        "credit",
            "bias":        "Neutral-Bull",
            "note":        "Sell ATM or slightly OTM put for premium in sideways market.",
            "probability": _prob(55, earn_pen),
        })

    # Add IVR context to notes
    for s in suggestions:
        if iv_rank is not None:
            if s['type'] == 'credit' and iv_rank > 55:
                s['note'] += f' IVR {iv_rank:.0f}% — premium rich.'
            elif s['type'] == 'credit' and iv_rank < 25:
                s['note'] += f' ⚠ IVR {iv_rank:.0f}% LOW — thin premium.'
            elif s['type'] == 'debit' and iv_rank < 30:
                s['note'] += f' IVR {iv_rank:.0f}% — options cheap.'
            elif s['type'] == 'debit' and iv_rank > 65:
                s['note'] += f' ⚠ IVR {iv_rank:.0f}% HIGH — expensive.'

    # Sort by probability desc, take top 3
    suggestions.sort(key=lambda x: -x["probability"])
    return suggestions[:3]



def run_regime_scan(symbols=None, max_workers=12):
    """Run full regime scan for all symbols. Called by scheduler at 7AM."""
    import concurrent.futures
    _ensure_table()

    if not symbols:
        con = _conn()
        symbols = [r["symbol"] for r in con.execute("SELECT symbol FROM symbols").fetchall()]
        con.close()

    # Use the full default watchlist if DB is empty
    if not symbols:
        symbols = [
            "AA","AAPL","ADBE","AMD","AMZN","APA","AR","AVGO","AXP","BA","BABA","BAC",
            "BMY","BP","BSX","BX","C","CCJ","CMG","COF","COIN","CRM","CSCO","CSX","CTRA",
            "CVNA","CVS","CVX","DAL","DASH","DDOG","DIS","DOW","DVN","EPD","EQT","FCX",
            "FSLR","GE","GILD","GM","GOOG","GOOGL","GSK","HAL","HOOD","IBM","INTC","JPM",
            "KMI","KO","LRCX","LUV","LVS","MDLZ","META","MMM","MO","MRK","MRNA","MRVL",
            "MS","MSFT","MSTR","MU","NEE","NEM","NFLX","NKE","NOW","NVDA","OKTA","ORCL",
            "OXY","PANW","PDD","PEP","PG","PLTR","PYPL","RBLX","RCL","RTX","SBUX","SCHW",
            "SHOP","SLB","TEVA","TGT","TSLA","TSM","UAL","UBER","UNH","UPS","V","VST",
            "VZ","WDC","WFC","WMT","WYNN","XOM","Z"
        ]

    today = date.today().isoformat()
    results = []
    errors  = []

    def _one(sym):
        try:
            return _compute_regime_ta(sym)
        except Exception as e:
            errors.append(f"{sym}: {e}")
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for res in ex.map(_one, symbols):
            if res:
                results.append(res)

    # Save to DB
    if results:
        con = _conn()
        for r in results:
            con.execute("""
                INSERT OR REPLACE INTO regime_scan
                (scan_date, symbol, regime, confidence, bias, rsi, rsi_diff,
                 macd, adx, ema_trend, momentum_move, post_earnings, earn_days, beta,
                 earn_date, earn_score, rsi_trend, weekly_trend, confluence, signals_json, updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                today, r["symbol"], r["regime"], r["confidence"], r["bias"],
                r["rsi"], r["rsi_diff"], r["macd"], r["adx"], r["ema_trend"],
                r.get("mom_move"), r["post_earnings"], r.get("earn_days"), r.get("beta"),
                r.get("earn_date_str"), r.get("earn_score", 0),
                r.get("rsi_trend"), (r.get("weekly_regime") or {}).get("trend"), r.get("confluence"),
                json.dumps(_json_safe({
                    "signals":         r["signals"],
                    "score":           r["score"],
                    "futures_oi":      r.get("futures_oi_signal",{}),
                    "ema9":            r["ema9"], "ema20": r["ema20"], "ema50": r["ema50"],
                    "pdi":             r["pdi"], "ndi": r["ndi"],
                    "pdi_delta":       r["pdi_delta"], "ndi_delta": r["ndi_delta"],
                    "bb_pct":          r["bb_pct"],
                    "vol_ratio":       r["vol_ratio"],
                    "is_mom_move":     r["is_mom_move"],
                    "recent_mom":      r.get("recent_mom"),
                    "large_moves_10d":  r.get("large_moves_10d",[]),
                    "largest_move_10d": r.get("largest_move_10d"),
                    "vol_spike_10d":    r.get("vol_spike_10d", False),
                    "vol_spike_info":   r.get("vol_spike_info"),
                    "spot":             r["spot"],
                    "earn_score":       r.get("earn_score", 0),
                    "iv_rank":          r.get("iv_rank", 50),
                    "earn_score_notes": r.get("earn_score_notes",[]),
                    "earn_date_str":    r.get("earn_date_str"),
                    "strategies":       r.get("strategies",[]),
                    "earn_days":        r.get("earn_days"),
                    "beta":             r.get("beta"),
                    "rsi_trend":        r.get("rsi_trend"),
                    "weekly_regime":    r.get("weekly_regime"),
                    "confluence":       r.get("confluence"),
                }), allow_nan=False),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))
        con.commit(); con.close()

    completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Store completion time in app_config for UI to read
    try:
        import sqlite3
        _c = sqlite3.connect(DB_PATH)
        _c.execute("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)")
        _c.execute("INSERT OR REPLACE INTO app_config VALUES (?,?)",
                   ("regime_scan_completed_at", completed_at))
        _c.commit(); _c.close()
    except: pass
    return {
        "scanned":      len(results),
        "errors":       len(errors),
        "date":         today,
        "completed_at": completed_at,
        "symbols":      [r["symbol"] for r in results],
    }


def get_latest_scan(scan_date=None):
    """Return latest scan results from DB."""
    con = _conn()
    d = scan_date or date.today().isoformat()
    # If today has no data, get most recent available date
    count = con.execute("SELECT COUNT(*) FROM regime_scan WHERE scan_date=?", (d,)).fetchone()[0]
    if count == 0:
        row = con.execute("SELECT MAX(scan_date) FROM regime_scan").fetchone()
        if row and row[0]: d = row[0]

    rows = con.execute(
        "SELECT * FROM regime_scan WHERE scan_date=? ORDER BY confidence DESC",
        (d,)
    ).fetchall()
    con.close()
    results = []
    for r in rows:
        rec = dict(r)
        try: rec["signals_data"] = json.loads(r["signals_json"] or "{}")
        except: rec["signals_data"] = {}
        results.append(rec)
    # Get last completed timestamp from app_config
    last_at = ""
    try:
        _c2 = _conn()
        _r = _c2.execute("SELECT value FROM app_config WHERE key='regime_scan_completed_at'").fetchone()
        if _r: last_at = _r[0]
        _c2.close()
    except: pass
    return {"date": d, "results": results, "count": len(results), "completed_at": last_at}
