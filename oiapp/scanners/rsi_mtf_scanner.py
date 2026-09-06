"""
RSI-EMA(90) Multi-Timeframe Scanner
======================================
Signal: diff = rsi14 - ema(rsi14, 90)

BULL setup:
  Daily:    Highest(diff, 10) >= +20      ← was overbought in last 10 daily bars
  1H / 2H:  ema(rsi14,90) - rsi14 >= 20  ← intraday now pulling below EMA = -diff >= 20

BEAR setup:
  Daily:    Highest(-diff, 10) >= +20     ← was oversold in last 10 daily bars
            i.e. min(diff, 10) <= -20
  1H / 2H:  rsi14 - ema(rsi14,90) >= 20  ← intraday now bouncing above EMA = diff >= 20

Important: EMA(rsi,90) needs ~200+ bars to converge.
  Daily → fetch 2y data
  1H    → fetch 60d data (~300 bars)
  2H    → fetch 60d data (~150 bars)
"""

import sqlite3, json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .scoring_service import attach_scanner_scores

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH
PARAMS_KEY = "rsi_mtf_params"


# ── Timeframe config ────────────────────────────────────────────────
TF_MAP = {
    # label → (interval, period, resample_from)
    "monthly": ("1mo",  "10y", None),
    "weekly":  ("1wk",  "5y",  None),
    "daily":   ("1d",   "2y",  None),
    "4h":      ("1h",   "60d", "4h"),   # resample 1H→4H
    "2h":      ("1h",   "60d", "2h"),   # resample 1H→2H
    "1h":      ("1h",   "60d", None),
}

def _fetch_tf(ticker, label):
    """Fetch OHLCV for given timeframe label. Returns DataFrame or None."""
    import pandas as pd
    cfg = TF_MAP.get(label.lower())
    if cfg is None: return None
    interval, period, resample = cfg
    try:
        df = ticker.history(period=period, interval=interval)
        if df is None or df.empty: return None
        if resample:
            df = df.resample(resample).agg({
                "Open": "first", "High": "max",
                "Low": "min",   "Close": "last", "Volume": "sum"
            }).dropna()
        return df if len(df) > 10 else None
    except: return None

DEFAULTS = {
    "daily_lookback":   10,   # Highest(..., N) on daily
    "daily_threshold":  20,   # >= this
    "intra_threshold":  20,   # intraday |diff| >= this
    "htf":             "daily",   # higher TF: "daily","weekly","monthly"
    "ltf":             "1h",      # lower TF: "1h","2h","4h","daily"
    "intra_tf":        "1h",  # kept for backward compat
    "rsi_period":       14,
    "ema_period":       90,
    "macd_filter":     True,
    "ema50_filter":    True,
    "symbols":         "",
}

def _ensure_config():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)")
    con.commit(); con.close()

def save_params(p):
    _ensure_config()
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR REPLACE INTO app_config VALUES (?,?)", (PARAMS_KEY, json.dumps(p)))
    con.commit(); con.close()

def load_params():
    _ensure_config()
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute("SELECT value FROM app_config WHERE key=?", (PARAMS_KEY,)).fetchone()
        con.close()
        if row:
            m = dict(DEFAULTS); m.update(json.loads(row[0])); return m
    except: pass
    return dict(DEFAULTS)


# ── TA ────────────────────────────────────────────────────────────────────────

def _ema(series, period):
    k = 2/(period+1); out = list(series)
    for i in range(1, len(out)):
        out[i] = series[i]*k + out[i-1]*(1-k)
    return out

def _rsi(closes, period=14):
    out = [50.0]*len(closes)
    if len(closes) < period+1: return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag = sum(gains[:period])/period; al = sum(losses[:period])/period
    for i in range(period, len(closes)):
        if i > period:
            ag = (ag*(period-1)+gains[i-1])/period
            al = (al*(period-1)+losses[i-1])/period
        out[i] = 100 - 100/(1+(ag/al if al>0 else 100))
    return out

def _diff_series(closes, rsi_p, ema_p):
    """rsi_p-period RSI minus ema_p-period EMA of that RSI."""
    rsi_vals  = _rsi(closes, rsi_p)
    ema_vals  = _ema(rsi_vals, ema_p)
    diff      = [r - e for r, e in zip(rsi_vals, ema_vals)]
    return diff, rsi_vals, ema_vals

def _macd_now(closes):
    if len(closes) < 35: return 0,0,0
    ef=_ema(closes,12); es=_ema(closes,26)
    ml=[f-s for f,s in zip(ef,es)]
    sl=_ema(ml,9)
    return ml[-1], sl[-1], ml[-1]-sl[-1]


# ── Per-symbol ────────────────────────────────────────────────────────────────

def _scan(sym, p):
    try:
        import yfinance as yf

        rsi_p   = int(p["rsi_period"])
        ema_p   = int(p["ema_period"])
        d_lb    = int(p["daily_lookback"])
        d_thr   = float(p["daily_threshold"])
        i_thr   = float(p["intra_threshold"])
        htf     = str(p.get("htf", "daily")).lower()
        ltf     = str(p.get("ltf", p.get("intra_tf", "1h"))).lower()
        use_macd= bool(p.get("macd_filter", True))
        use_ema = bool(p.get("ema50_filter", True))

        tk = yf.Ticker(sym)

        # ── Higher Timeframe (HTF) ────────────────────────────────────────
        htf_df = _fetch_tf(tk, htf)
        if htf_df is None or len(htf_df) < ema_p + d_lb + 5:
            return None
        DC = htf_df["Close"].tolist()
        n  = len(DC) - 1

        diff_d, rsi_d, ema_rsi_d = _diff_series(DC, rsi_p, ema_p)

        win_d      = diff_d[max(0, n - d_lb + 1) : n + 1]
        highest_d  = max(win_d)
        lowest_d   = min(win_d)
        diff_d_now = diff_d[n]

        cur_px = DC[n]
        d_macd, d_msig, d_mhist = _macd_now(DC)
        ema50  = _ema(DC, 50)[-1]
        ema200 = _ema(DC, 200)[-1]

        # ── Lower Timeframe (LTF) ─────────────────────────────────────────
        ltf_df = _fetch_tf(tk, ltf)
        if ltf_df is None or len(ltf_df) < ema_p + 5:
            return None
        IC = ltf_df["Close"].tolist()
        diff_i, rsi_i, ema_rsi_i = _diff_series(IC, rsi_p, ema_p)
        diff_i_now = diff_i[-1]
        i_macd, i_msig, _ = _macd_now(IC)
        itf = ltf  # for labels

        # ── Exact formulas ────────────────────────────────────────────────
        # BULL daily:   Highest(diff, d_lb) >= +d_thr
        bull_d = highest_d >= d_thr
        # BULL intra:   ema(rsi,90) - rsi >= i_thr  →  -diff_i_now >= i_thr
        bull_i = (-diff_i_now) >= i_thr

        # BEAR daily:   Highest(-diff, d_lb) >= +d_thr  →  lowest_d <= -d_thr
        bear_d = lowest_d <= -d_thr
        # BEAR intra:   rsi - ema(rsi,90) >= i_thr  →  diff_i_now >= i_thr
        bear_i = diff_i_now >= i_thr

        bull = bull_d and bull_i
        bear = bear_d and bear_i

        if not bull and not bear: return None

        # Optional extra filters
        if use_ema:
            if bull: bull = bull and cur_px > ema50
            if bear: bear = bear and cur_px < ema50
        if use_macd:
            if bull: bull = bull and (d_macd > d_msig or d_mhist > 0)
            if bear: bear = bear and (d_macd < d_msig or d_mhist < 0)

        if not bull and not bear: return None

        # ── Score ─────────────────────────────────────────────────────────
        def _score(is_bull):
            sc = 0; sigs = []
            pk = highest_d if is_bull else -lowest_d   # how far above threshold
            id_val = -diff_i_now if is_bull else diff_i_now  # intraday retrace depth

            sc += min(35, int(pk   / d_thr * 35)); sigs.append(
                f"Daily peak diff {highest_d:+.1f}" if is_bull
                else f"Daily low diff {lowest_d:+.1f}")
            sc += min(30, int(id_val / i_thr * 30)); sigs.append(
                f"{itf.upper()} diff {diff_i_now:+.1f} (retrace)" if is_bull
                else f"{itf.upper()} diff {diff_i_now:+.1f} (bounce)")

            if is_bull:
                if d_macd > d_msig or d_mhist > 0: sc+=12; sigs.append("MACD ▲")
                if cur_px > ema50:  sc+=10; sigs.append("Above EMA50")
                if cur_px > ema200: sc+=8;  sigs.append("Above EMA200")
                if i_macd < i_msig: sc+=5;  sigs.append(f"{itf.upper()} MACD- (retrace)")
            else:
                if d_macd < d_msig or d_mhist < 0: sc+=12; sigs.append("MACD ▼")
                if cur_px < ema50:  sc+=10; sigs.append("Below EMA50")
                if cur_px < ema200: sc+=8;  sigs.append("Below EMA200")
                if i_macd > i_msig: sc+=5;  sigs.append(f"{itf.upper()} MACD+ (bounce)")
            return min(sc, 100), sigs

        bs, bsig = _score(True)  if bull else (0, [])
        rs, rsig = _score(False) if bear else (0, [])

        direction = ("BULL" if bs >= rs else "BEAR") if (bull and bear) else ("BULL" if bull else "BEAR")

        result = {
            "symbol":      sym, "direction": direction,
            "setup_type":  "RSI MTF",
            "score":       bs if direction=="BULL" else rs,
            "native_score": bs if direction=="BULL" else rs,
            "price":       round(cur_px, 2),
            # Daily diff stats
            "highest_d":   round(highest_d, 1),
            "lowest_d":    round(lowest_d, 1),
            "diff_d_now":  round(diff_d_now, 1),
            "d_rsi":       round(rsi_d[n], 1),
            "d_ema_rsi":   round(ema_rsi_d[n], 1),
            # Intraday diff
            "diff_i_now":  round(diff_i_now, 1),
            "i_rsi":       round(rsi_i[-1], 1),
            "i_ema_rsi":   round(ema_rsi_i[-1], 1),
            "i_tf":        itf,
            # Indicators
            "d_macd": round(d_macd,3), "d_msig": round(d_msig,3), "d_mhist": round(d_mhist,3),
            "ema50":  round(ema50,2),  "ema200": round(ema200,2),
            "signals": bsig if direction=="BULL" else rsig,
            "bull_score": bs, "bear_score": rs,
            "trend_age": d_lb,
        }
        return attach_scanner_scores(result, sym, frame=htf_df, setup_type="RSI MTF", direction=direction, native_score=result["native_score"], trend_age=d_lb)
    except Exception:
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def run_rsi_mtf_scan(params=None, workers=25):
    if params is None: params = load_params()
    syms_raw = params.get("symbols","")
    symbols  = [s.strip().upper() for s in syms_raw.split(",") if s.strip()]
    if not symbols:
        try:
            con = sqlite3.connect(DB_PATH)
            rows = con.execute("SELECT symbol FROM symbols").fetchall()
            con.close()
            symbols = [r[0] for r in rows] if rows else []
        except: pass
    if not symbols:
        symbols = ["AAPL","AMZN","MSFT","NVDA","META","GOOGL","TSLA","AMD","AVGO","NFLX",
                   "COST","QCOM","MU","LRCX","MRVL","JPM","BAC","V","ADBE","ORCL",
                   "COIN","PLTR","MSTR","CVNA","HOOD","DASH","NOW","CRM","PANW","DDOG",
                   "XOM","CVX","OXY","FSLR","NEE","VST","NEM","FCX","GLD","SLB"]
    bulls, bears = [], []
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(_scan, sym, params): sym for sym in symbols}
        from ..services.bounded_wait import bounded_as_completed
        for fut, sym in bounded_as_completed(futs, timeout=60,
                on_timeout=lambda ks: print(f"[rsi_mtf_scanner] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                continue
            r = fut.result()
            if r: (bulls if r["direction"]=="BULL" else bears).append(r)
    finally:
        ex.shutdown(wait=False)
    bulls.sort(key=lambda x: -x["score"])
    bears.sort(key=lambda x: -x["score"])
    return {"bulls": bulls[:40], "bears": bears[:40],
            "total_scanned": len(symbols),
            "bull_count": len(bulls), "bear_count": len(bears),
            "params": params}
