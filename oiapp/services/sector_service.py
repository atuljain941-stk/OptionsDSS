# oiapp/services/sector_service.py
"""
Sector classification and 4-quadrant momentum analysis.
Uses yfinance info for sector data, cached in SQLite.
"""
import sqlite3, time, math
from pathlib import Path
from datetime import date

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH
_sector_mem = {}  # in-memory cache

# ETF proxies for sector performance
SECTOR_ETFS = {
    "Technology":           "XLK",
    "Healthcare":           "XLV",
    "Financials":           "XLF",
    "Energy":               "XLE",
    "Consumer Discretionary":"XLY",
    "Consumer Staples":     "XLP",
    "Industrials":          "XLI",
    "Materials":            "XLB",
    "Utilities":            "XLU",
    "Real Estate":          "XLRE",
    "Communication Services":"XLC",
}


def _is_finite(value):
    try:
        return math.isfinite(float(value))
    except Exception:
        return False

def _safe_num(value, digits=None, default=None):
    try:
        v = float(value)
        if not math.isfinite(v):
            return default
        return round(v, digits) if digits is not None else v
    except Exception:
        return default

def _json_safe(obj):
    """Recursively replace NaN/inf with None so sector API returns valid JSON."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj

def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

def get_symbol_sector(symbol):
    """Get sector for a symbol — from cache, then DB, then yfinance."""
    if symbol in _sector_mem: return _sector_mem[symbol]
    con = _conn()
    row = con.execute("SELECT sector FROM sector_cache WHERE symbol=?", (symbol,)).fetchone()
    con.close()
    if row and row["sector"]: 
        _sector_mem[symbol] = row["sector"]
        return row["sector"]
    # Fetch from yfinance
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        sector = info.get("sector") or info.get("sectorDisp") or "Other"
        industry = info.get("industry","")
        con = _conn()
        con.execute("""
            INSERT OR REPLACE INTO sector_cache (symbol, sector, industry, updated)
            VALUES (?,?,?,?)
        """, (symbol, sector, industry, date.today().isoformat()))
        con.commit(); con.close()
        _sector_mem[symbol] = sector
        return sector
    except: return "Other"

def get_all_sectors_with_symbols():
    """Return dict of sector → [symbols] for watchlist."""
    con = _conn()
    symbols = [r["symbol"] for r in con.execute("SELECT symbol FROM symbols").fetchall()]
    con.close()
    # Get cached sectors
    sector_map = {}
    for sym in symbols:
        s = get_symbol_sector(sym)
        if s not in sector_map: sector_map[s] = []
        sector_map[s].append(sym)
    return sector_map

def get_sector_advance_decline(ma_period: int = 20):
    """Advance/decline ratio and % above N-day MA, computed PER SECTOR
    from the app's own symbol universe and price_cache -- same
    methodology as the WatchlistBreadth/WatchlistAdvanceDecline scanner
    primitives, but grouped by sector instead of by watchlist. This is
    a genuinely different signal from the ETF-based quadrant analysis
    above: the ETF tells you how the sector's benchmark moved: this
    tells you whether that move was broad-based across the sector's
    actual constituent stocks or carried by just a few names.
    """
    sector_map = get_all_sectors_with_symbols()
    con = _conn()
    try:
        out = {}
        for sector, symbols in sector_map.items():
            if not symbols or sector in (None, "", "Other"):
                continue
            advancing, declining, above, checked = 0, 0, 0, 0
            for sym in symbols[:300]:
                rows = con.execute(
                    "SELECT close FROM price_cache WHERE symbol=? ORDER BY date DESC LIMIT ?",
                    (sym, max(ma_period, 2)),
                ).fetchall()
                closes = [r[0] for r in rows if r[0] is not None]
                if len(closes) >= 2:
                    if closes[0] > closes[1]:
                        advancing += 1
                    elif closes[0] < closes[1]:
                        declining += 1
                if len(closes) >= ma_period:
                    ma = sum(closes[:ma_period]) / ma_period
                    checked += 1
                    if closes[0] >= ma:
                        above += 1
            ad_ratio = round(advancing / declining, 2) if declining > 0 else (float(advancing) if advancing else None)
            pct_above = round(above / checked * 100, 1) if checked > 0 else None
            out[sector] = {
                "advancing": advancing, "declining": declining, "ad_ratio": ad_ratio,
                "pct_above_ma": pct_above, "symbols_checked": checked, "total_symbols": len(symbols),
            }
        return out
    finally:
        con.close()


_sector_perf_cache = {}  # per-timeframe: {"daily": {"ts": ..., "data": [...]}, "weekly": {...}, "monthly": {...}}
_SECTOR_PERF_TTL = 300.0  # 5 minutes. Sector rotation doesn't meaningfully
# change minute-to-minute, and every previous version of this function did
# 11 sequential, uncached yfinance calls on every single tab load -- this
# was the actual, measured cause of the "used to take 10s, now takes over
# a minute" regression (nothing to do with the realtime dashboard changes;
# this function was never touched before and has always had this pattern,
# it's just that yfinance latency getting worse over time made it much
# more painful).


def get_sector_performance(timeframe: str = "daily"):
    """
    Compute 4-quadrant sector analysis using ETF proxies.
    Returns each sector with: price_chg_1d, price_chg_5d, rsi14, momentum_state, quadrant.
    Quadrant:
      1 (top-right) = BULLISH: positive 5d + positive RSI momentum (improving + strong)
      2 (top-left)  = RECOVERING: negative 5d but RSI improving (weakening but turning)
      3 (bottom-right)= OVERBOUGHT/EXTENDED: strong 5d but RSI extreme (strong but stretched)
      4 (bottom-left) = BEARISH: negative 5d + weak RSI (deteriorating)

    timeframe: "daily" (default), "weekly", or "monthly" -- resamples the
    SAME fetched history to weekly/monthly bars before every downstream
    calculation (RSI, MACD, ADX, quadrant) runs. Nothing about those
    calculations changes; they just operate on differently-aggregated
    bars, same as switching timeframe on a chart. Weekly/monthly need
    real history to have enough resampled bars for a valid RSI-14/MACD,
    so those two fetch a longer period than the daily view does.

    Cached for _SECTOR_PERF_TTL seconds per timeframe, and the 11 per-ETF yfinance calls
    run in parallel (ThreadPoolExecutor) instead of sequentially -- these
    are pure network I/O waits, not CPU work, so parallelizing them cuts
    wall-clock time by roughly the degree of parallelism achieved rather
    than summing all 11 calls' latency one after another.
    """
    tf = str(timeframe or "daily").lower().strip()
    if tf not in ("daily", "weekly", "monthly"):
        tf = "daily"

    now = time.time()
    cache_entry = _sector_perf_cache.setdefault(tf, {"data": None, "ts": 0})
    if cache_entry["data"] is not None and (now - cache_entry["ts"]) < _SECTOR_PERF_TTL:
        return cache_entry["data"]

    try:
        import yfinance as yf
    except Exception:
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed

    fetch_period = "3mo" if tf == "daily" else ("2y" if tf == "weekly" else "5y")
    resample_rule = {"daily": None, "weekly": "W-FRI", "monthly": "ME"}[tf]

    def _compute_one_sector(sector, etf):
        try:
            df = yf.Ticker(etf).history(period=fetch_period)
            if df is None or df.empty:
                return None
            try:
                df = df.replace([float("inf"), float("-inf")], None).dropna(subset=["Close"])
                if "High" in df.columns and "Low" in df.columns:
                    df = df.dropna(subset=["High", "Low"])
            except Exception:
                pass
            if resample_rule:
                df = df.resample(resample_rule).agg({
                    "Open": "first", "High": "max", "Low": "min", "Close": "last",
                    **({"Volume": "sum"} if "Volume" in df.columns else {}),
                }).dropna(subset=["Close"])
            closes = [_safe_num(x, None, None) for x in df["Close"].tolist()]
            closes = [x for x in closes if x is not None]
            if len(closes) < 20:
                return None
            n = len(closes) - 1

            # 1-day and 5-day change -- on weekly/monthly bars these are
            # correspondingly "1 week ago"/"5 weeks ago" etc., same
            # relative meaning as the daily view's "1 day"/"5 days".
            chg_1d = round((closes[n]-closes[n-1])/closes[n-1]*100, 2) if n>=1 else 0
            chg_5d = round((closes[n]-closes[n-5])/closes[n-5]*100, 2) if n>=5 else 0
            chg_20d= round((closes[n]-closes[n-20])/closes[n-20]*100, 2) if n>=20 else 0

            # RSI-14
            def rsi_fn(c, p=14):
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

            def ema_fn(a,p):
                k=2/(p+1); o=list(a)
                for i in range(1,len(o)): o[i]=a[i]*k+o[i-1]*(1-k)
                return o

            rsi_vals = rsi_fn(closes)
            rsi = _safe_num(rsi_vals[-1], 1, None)

            # EMA90 of RSI — same signal used in scanner
            ema90_rsi = ema_fn(rsi_vals, 90)
            rsi_diff  = _safe_num((rsi if rsi is not None else 50.0) - ema90_rsi[-1], 1, None)  # your proprietary diff signal
            rsi_for_logic = rsi if rsi is not None else 50.0
            rsi_diff_for_logic = rsi_diff if rsi_diff is not None else 0.0

            # EMA20 vs EMA50
            e20 = ema_fn(closes,20); e50 = ema_fn(closes,50)
            ema_trend = "above" if e20[-1] > e50[-1] else "below"

            # EMA20 slope (5-bar)
            e20_slope = (e20[-1] - e20[max(0,n-5)]) / 5

            # MACD (12/26/9)
            e12 = ema_fn(closes,12); e26 = ema_fn(closes,26)
            ml  = [a-b for a,b in zip(e12,e26)]
            ms  = ema_fn(ml, 9)
            mh  = [a-b for a,b in zip(ml,ms)]
            macd_hist    = mh[-1]
            macd_hist_1d = mh[-2] if len(mh)>1 else 0
            macd_sig = ("BULLISH" if macd_hist>0 and macd_hist>macd_hist_1d else
                        "BEARISH" if macd_hist<0 and macd_hist<macd_hist_1d else "NEUTRAL")

            # ADX-14 (simplified)
            adx_val = 20  # default
            try:
                H = df["High"].tolist(); L = df["Low"].tolist()
                tr2=[max(H[i]-L[i],abs(H[i]-closes[i-1]),abs(L[i]-closes[i-1]))
                     for i in range(1,len(closes))]
                pdm=[max(H[i]-H[i-1],0) if(H[i]-H[i-1])>(L[i-1]-L[i]) else 0
                     for i in range(1,len(closes))]
                ndm=[max(L[i-1]-L[i],0) if(L[i-1]-L[i])>(H[i]-H[i-1]) else 0
                     for i in range(1,len(closes))]
                def wild(a,p):
                    o=[0.0]*len(a)
                    if len(a)<p: return o
                    o[p-1]=sum(a[:p])
                    for i in range(p,len(a)): o[i]=o[i-1]-o[i-1]/p+a[i]
                    return o
                p=14
                spd=wild(pdm,p);snd=wild(ndm,p);str2=wild(tr2,p)
                pdi_a=[100*s/t if t else 0 for s,t in zip(spd,str2)]
                ndi_a=[100*s/t if t else 0 for s,t in zip(snd,str2)]
                dx=[100*abs(a-b)/(a+b) if(a+b) else 0 for a,b in zip(pdi_a[p-1:],ndi_a[p-1:])]
                if len(dx)>=p:
                    av=[0.0]*len(dx); av[p-1]=sum(dx[:p])/p
                    for i in range(p,len(dx)): av[i]=av[i-1]-av[i-1]/p+dx[i]/p
                    adx_val = min(100, max(0, round(av[-1],1)))
            except: pass

            # 4-quadrant using 5-day price momentum and RSIDiff90.
            # RSIDiff90 = RSI14 - EMA90(RSI14). This is not the raw RSI value.
            # Positive/negative 5-day return chooses the price-momentum side.
            # The EXTENDED bucket must be reserved for RSIDiff90 >= +20 only;
            # older builds incorrectly used it as a catch-all for any 5-day-up sector
            # whose RSIDiff90 was below +10.
            x_pos = chg_5d >= 0
            is_overbought = rsi_diff_for_logic >= 20
            is_oversold = rsi_diff_for_logic <= -20

            # Momentum label using the same thresholds exposed in the UI.
            if   rsi_diff_for_logic >= 20: mom = "OVERBOUGHT"
            elif rsi_diff_for_logic <= -20: mom = "OVERSOLD"
            elif rsi_diff_for_logic >= 10: mom = "ELEVATED"
            elif rsi_diff_for_logic <= -10: mom = "DEPRESSED"
            else: mom = "NEUTRAL"

            if x_pos:
                if is_overbought:
                    quadrant = 3
                    quad_label = "EXTENDED/TOPPING" if macd_sig == "BEARISH" else "EXTENDED"
                    quad_color = "#f97316" if macd_sig == "BEARISH" else "#f59e0b"
                else:
                    quadrant = 1
                    if rsi_diff_for_logic >= 10:
                        quad_label = "BULLISH/ELEVATED"
                    elif rsi_diff_for_logic >= 0:
                        quad_label = "BULLISH"
                    else:
                        quad_label = "ADVANCING/WEAK RSI"
                    quad_color = "#22c55e"
            else:
                if rsi_diff_for_logic >= 0:
                    quadrant = 2
                    quad_label = "RECOVERING"
                    quad_color = "#4ade80"
                else:
                    quadrant = 4
                    quad_label = "BEARISH" if (is_oversold or macd_sig == "BEARISH") else "BEARISH/WEAK"
                    quad_color = "#ef4444" if (is_oversold or macd_sig == "BEARISH") else "#f87171"

            return _json_safe({
                "sector": sector, "etf": etf,
                "price": _safe_num(closes[-1], 2, None),
                "chg_1d": _safe_num(chg_1d, 2, 0.0), "chg_5d": _safe_num(chg_5d, 2, 0.0), "chg_20d": _safe_num(chg_20d, 2, 0.0),
                "rsi": rsi, "rsi_diff": rsi_diff,
                "ema_trend": ema_trend, "macd": macd_sig, "adx": _safe_num(adx_val, 1, None),
                "momentum": mom, "quadrant": quadrant,
                "quad_label": quad_label, "quad_color": quad_color,
            })
        except Exception:
            return None

    results = []
    from .task_executor import get_background_executor
    ex = get_background_executor()
    futs = {ex.submit(_compute_one_sector, sector, etf): sector for sector, etf in SECTOR_ETFS.items()}
    for fut in as_completed(futs):
        try:
            r = fut.result()
        except Exception:
            r = None
        if r is not None:
            results.append(r)

    results.sort(key=lambda x: (-x["quadrant"]==1, x["chg_5d"]), reverse=False)
    results.sort(key=lambda x: x["quadrant"])
    cache_entry["ts"] = now
    cache_entry["data"] = results
    return results


def refresh_all_sectors(symbols=None):
    """Fetch and cache sector for a list of symbols (or full watchlist)."""
    import yfinance as yf, time, sqlite3
    from pathlib import Path
    from datetime import date

    if not symbols:
        WATCHLIST = ["AA","AAPL","ADBE","AMD","AMZN","APA","AR","AVGO","AXP","BA","BABA",
            "BAC","BMY","BP","BSX","BX","C","CCJ","CMG","COF","COIN","CRM","CSCO","CSX",
            "CTRA","CVNA","CVS","CVX","DAL","DASH","DDOG","DIS","DOW","DVN","EPD","EQT",
            "FCX","FSLR","GE","GILD","GM","GOOG","GOOGL","GSK","HAL","HOOD","IBM","INTC",
            "JPM","KMI","KO","LRCX","LUV","LVS","MDLZ","META","MMM","MO","MRK","MRNA",
            "MRVL","MS","MSFT","MSTR","MU","NEE","NEM","NFLX","NKE","NOW","NVDA","OKTA",
            "ORCL","OXY","PANW","PDD","PEP","PG","PLTR","PYPL","RBLX","RCL","RTX","SBUX",
            "SCHW","SHOP","SLB","SPY","QQQ","IWM","TEVA","TGT","TSLA","TSM","UAL","UBER",
            "UNH","UPS","V","VST","VZ","WDC","WFC","WMT","WYNN","XOM","Z"]
        symbols = WATCHLIST

    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS sector_cache (
        symbol TEXT PRIMARY KEY, sector TEXT, industry TEXT, updated TEXT)""")
    today = date.today().isoformat()
    updated = 0
    for sym in symbols:
        try:
            tk = yf.Ticker(sym)
            sector   = tk.fast_info.sector   if hasattr(tk.fast_info,'sector')   else ''
            industry = tk.fast_info.industry if hasattr(tk.fast_info,'industry') else ''
            if not sector:
                info = tk.info or {}
                sector   = info.get('sector','')
                industry = info.get('industry','')
            if sector:
                con.execute("INSERT OR REPLACE INTO sector_cache VALUES (?,?,?,?)",
                            (sym.upper(), sector, industry, today))
                updated += 1
            # Note: do NOT add to symbols table - that's managed by the user
            time.sleep(0.1)
        except: pass
    con.commit(); con.close()
    print(f"[sector] Refreshed {updated}/{len(symbols)} symbols")
    return updated
