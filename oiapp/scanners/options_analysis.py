# oiapp/scanners/options_analysis.py
"""
Options Trade Analysis Backend
- Black-Scholes pricing + Greeks
- P&L payoff diagrams (at expiry + at N DTE)
- Multi-leg strategy simulation
- Backtesting engine using historical price data
"""
import math, json
import sqlite3
from pathlib import Path
from flask import Blueprint, jsonify, request
from datetime import date, datetime, timedelta
analysis_bp = Blueprint("analysis_bp", __name__, url_prefix="/analysis")

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")


def _load_watchlist_symbols_for_momentum(watchlist_id):
    """Return symbols for a selected watchlist or None if unavailable."""
    if not watchlist_id:
        return None
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (int(watchlist_id),),
        ).fetchall()
        con.close()
        syms = [r[0] for r in rows if r and r[0]]
        return syms or None
    except Exception:
        return None
# ── Black-Scholes Core ────────────────────────────────────────────────────
def _norm_cdf(x):
    """Abramowitz & Stegun approximation."""
    if x < 0: return 1 - _norm_cdf(-x)
    t = 1/(1+0.2316419*x)
    poly = t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))))
    return 1 - (1/math.sqrt(2*math.pi))*math.exp(-0.5*x*x)*poly
def _norm_pdf(x):
    return (1/math.sqrt(2*math.pi))*math.exp(-0.5*x*x)
def bs_price(S, K, T, iv, r=0.05, is_call=True):
    """Black-Scholes price. T in years."""
    if T <= 0:
        if is_call: return max(0, S-K)
        return max(0, K-S)
    if iv <= 0 or S <= 0 or K <= 0: return 0
    try:
        d1 = (math.log(S/K) + (r + 0.5*iv*iv)*T) / (iv*math.sqrt(T))
        d2 = d1 - iv*math.sqrt(T)
        if is_call: return max(0, S*_norm_cdf(d1) - K*math.exp(-r*T)*_norm_cdf(d2))
        return max(0, K*math.exp(-r*T)*_norm_cdf(-d2) - S*_norm_cdf(-d1))
    except: return 0
def bs_greeks(S, K, T, iv, r=0.05, is_call=True):
    """All Greeks. Returns dict."""
    if T <= 1/365:
        intrinsic = max(0, S-K) if is_call else max(0, K-S)
        delta = 1.0 if (is_call and S>K) else (-1.0 if (not is_call and S<K) else 0.0)
        return {"price":round(intrinsic,4),"delta":delta,"gamma":0,"theta":0,"vega":0,"rho":0}
    try:
        d1 = (math.log(S/K) + (r + 0.5*iv*iv)*T) / (iv*math.sqrt(T))
        d2 = d1 - iv*math.sqrt(T)
        pdf_d1 = _norm_pdf(d1)
        price  = bs_price(S, K, T, iv, r, is_call)
        delta  = _norm_cdf(d1) if is_call else (_norm_cdf(d1)-1)
        gamma  = pdf_d1 / (S*iv*math.sqrt(T))
        theta  = (-(S*pdf_d1*iv)/(2*math.sqrt(T)) - r*K*math.exp(-r*T)*(_norm_cdf(d2) if is_call else _norm_cdf(-d2))) / 365
        vega   = S*pdf_d1*math.sqrt(T)/100   # per 1% IV change
        rho    = K*T*math.exp(-r*T)*(_norm_cdf(d2) if is_call else _norm_cdf(-d2))*(1 if is_call else -1)/100
        return {
            "price": round(price,4), "delta": round(delta,4),
            "gamma": round(gamma,6), "theta": round(theta,4),
            "vega":  round(vega,4),  "rho":   round(rho,4),
        }
    except: return {"price":0,"delta":0,"gamma":0,"theta":0,"vega":0,"rho":0}
def strategy_pnl(legs, spot_range, dte, iv, entry_prices=None):
    """
    Compute P&L of a multi-leg strategy across spot range at given DTE.
    legs: [{side, option_type, strike, qty, entry_price}]
    Returns: list of {spot, pnl, delta, gamma, theta, vega}
    """
    T = max(0, dte/365)
    results = []
    for S in spot_range:
        total_pnl = 0; total_delta = 0; total_gamma = 0; total_theta = 0; total_vega = 0
        for i, leg in enumerate(legs):
            K       = float(leg["strike"])
            is_call = leg["option_type"].lower() == "call"
            side    = leg["side"].lower()   # "buy" or "sell"
            qty     = int(leg.get("qty",1))
            ep      = float(leg.get("entry_price", leg.get("price",0)))
            if leg["option_type"].lower() == "stock":
                # Stock leg: P&L = (current - entry) * qty
                pnl = (S - ep) * qty * (1 if side=="buy" else -1)
                total_pnl += pnl
                total_delta += qty * (1 if side=="buy" else -1)
                continue
            g = bs_greeks(S, K, T, iv, is_call=is_call)
            curr_price = g["price"]
            sign = 1 if side=="buy" else -1
            # P&L = (current - entry) * qty * 100 * sign
            total_pnl   += sign * (curr_price - ep) * qty * 100
            total_delta += sign * g["delta"]  * qty * 100
            total_gamma += sign * g["gamma"]  * qty * 100
            total_theta += sign * g["theta"]  * qty * 100
            total_vega  += sign * g["vega"]   * qty * 100
        results.append({
            "spot":   round(S,2),
            "pnl":    round(total_pnl,2),
            "delta":  round(total_delta,4),
            "gamma":  round(total_gamma,6),
            "theta":  round(total_theta,4),
            "vega":   round(total_vega,4),
        })
    return results
def expiry_pnl(legs, spot_range):
    """P&L at expiry (T=0) - pure intrinsic value."""
    results = []
    for S in spot_range:
        total = 0
        for leg in legs:
            if leg["option_type"].lower() == "stock":
                ep = float(leg.get("entry_price", leg.get("price",0)))
                sign = 1 if leg["side"]=="buy" else -1
                total += sign * (S - ep) * int(leg.get("qty",1))
                continue
            K       = float(leg["strike"])
            is_call = leg["option_type"].lower() == "call"
            side    = leg["side"].lower()
            qty     = int(leg.get("qty",1))
            ep      = float(leg.get("entry_price", leg.get("price",0)))
            intrinsic = max(0, S-K) if is_call else max(0, K-S)
            sign = 1 if side=="buy" else -1
            total += sign * (intrinsic - ep) * qty * 100
        results.append({"spot":round(S,2),"pnl":round(total,2)})
    return results
# ── Routes ──────────────────────────────────────────────────────────────
@analysis_bp.route("/payoff")
def payoff_diagram():
    """
    Compute P&L at expiry + multiple DTE slices for a multi-leg strategy.
    Query params: legs (JSON), iv, spot, dte_max
    """
    try:
        legs    = json.loads(request.args.get("legs","[]"))
        iv      = float(request.args.get("iv", 0.25))
        spot    = float(request.args.get("spot", 100))
        dte_max = int(request.args.get("dte_max", 30))
        if not legs: return jsonify({"error":"No legs provided"}), 400
        # Spot range: ±20% around spot, 120 points
        lo = spot * 0.80; hi = spot * 1.20
        spot_range = [round(lo + i*(hi-lo)/119, 2) for i in range(120)]
        # P&L at expiry
        at_expiry = expiry_pnl(legs, spot_range)
        # P&L at multiple DTE slices
        dte_slices = {}
        for dte in [dte_max, round(dte_max*0.75), round(dte_max*0.5), round(dte_max*0.25), 1]:
            if dte < 0: continue
            dte_slices[str(dte)] = strategy_pnl(legs, spot_range, dte, iv)
        # Greeks at current spot for each DTE
        greeks_summary = {}
        for dte in [dte_max, round(dte_max*0.75), round(dte_max*0.5), 1]:
            g = strategy_pnl(legs, [spot], dte, iv)
            if g: greeks_summary[str(dte)] = g[0]
        # Break-even points at expiry
        prev_pnl = at_expiry[0]["pnl"]
        breakevens = []
        for pt in at_expiry[1:]:
            if (prev_pnl < 0 and pt["pnl"] >= 0) or (prev_pnl >= 0 and pt["pnl"] < 0):
                breakevens.append(round((pt["spot"] + at_expiry[at_expiry.index(pt)-1]["spot"])/2, 2))
            prev_pnl = pt["pnl"]
        # Max profit / max loss at expiry
        pnls = [p["pnl"] for p in at_expiry]
        max_profit = max(pnls); max_loss = min(pnls)
        return jsonify({
            "spot_range":    spot_range,
            "at_expiry":     at_expiry,
            "dte_slices":    dte_slices,
            "greeks_at_spot":greeks_summary,
            "breakevens":    breakevens,
            "max_profit":    round(max_profit,2),
            "max_loss":      round(max_loss,2),
            "dte_max":       dte_max,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace":traceback.format_exc()[:400]}), 500
@analysis_bp.route("/greeks_surface")
def greeks_surface():
    """Compute Greeks across spot × DTE grid for 3D surface visualization."""
    try:
        legs  = json.loads(request.args.get("legs","[]"))
        iv    = float(request.args.get("iv", 0.25))
        spot  = float(request.args.get("spot",100))
        dte_max = int(request.args.get("dte_max", 30))
        metric  = request.args.get("metric","pnl")  # pnl, delta, theta, vega, gamma
        if not legs: return jsonify({"error":"No legs"}), 400
        lo = spot*0.85; hi = spot*1.15
        spots_grid = [round(lo+i*(hi-lo)/39,2) for i in range(40)]
        dtes_grid  = [max(1,round(dte_max*i/14)) for i in range(1,15)]
        surface = []
        for d in dtes_grid:
            row = strategy_pnl(legs, spots_grid, d, iv)
            surface.append({"dte":d,"values":[pt[metric] for pt in row]})
        return jsonify({"spots":spots_grid,"dte_axis":dtes_grid,"surface":surface,"metric":metric})
    except Exception as e:
        return jsonify({"error":str(e)}), 500
@analysis_bp.route("/backtest")
def backtest():
    """
    Backtest a strategy on historical price data.
    Uses historical OHLCV + IV estimate (HV20 as proxy) to simulate option prices.
    Entry rules: RSI-EMA diff thresholds, momentum signals.
    """
    try:
        symbol   = request.args.get("symbol","SPY").upper()
        strategy = request.args.get("strategy","PS")   # PS, CS, IC, PB, CB
        entry_signal = request.args.get("entry_signal","MEAN_REV_BEAR")
        rsi_ema_threshold = float(request.args.get("rsi_ema_threshold", 15))
        dte_target  = int(request.args.get("dte", 28))
        wing_pct    = float(request.args.get("wing_pct", 5.0))  # % OTM for short strike
        start_date  = request.args.get("start", "2023-01-01")
        end_date    = request.args.get("end",   date.today().isoformat())
        import yfinance as yf
        df = yf.Ticker(symbol).history(start=start_date, end=end_date, interval="1d")
        if df.empty or len(df) < 60:
            return jsonify({"error":"Insufficient historical data"}), 400
        closes = df["Close"].tolist()
        highs  = df["High"].tolist()
        lows   = df["Low"].tolist()
        vols   = df["Volume"].tolist()
        dates  = [str(d.date()) for d in df.index]
        n = len(closes)
        # ── Compute indicators ──────────────────────────────────────────
        def ema_fn(a, p):
            k=2/(p+1); o=list(a)
            for i in range(1,len(o)): o[i]=a[i]*k+o[i-1]*(1-k)
            return o
        def rsi_fn(c, p=14):
            out=[50.0]*len(c)
            if len(c)<p+1: return out
            g=l=0.0
            for i in range(1,p+1):
                d=c[i]-c[i-1]
                if d>0: g+=d
                else: l-=d
            ag,al=g/p,l/p
            out[p]=100 if al==0 else 100-100/(1+ag/al)
            for i in range(p+1,len(c)):
                d=c[i]-c[i-1]; ag=(ag*(p-1)+max(d,0))/p; al=(al*(p-1)+max(-d,0))/p
                out[i]=100 if al==0 else 100-100/(1+ag/al)
            return out
        # Historical Volatility (20-day) as IV proxy
        hv = [0.25]*n
        for i in range(20, n):
            rets = [math.log(closes[j]/closes[j-1]) for j in range(i-19,i+1)]
            hv[i] = min(2.0, max(0.05, math.sqrt(252) * math.sqrt(sum(r*r for r in rets)/20)))
        rsi14    = rsi_fn(closes)
        ema90rsi = ema_fn(rsi14, 90)
        rsi_diff = [round(rsi14[i]-ema90rsi[i],2) for i in range(n)]
        ema20    = ema_fn(closes, 20)
        ema50    = ema_fn(closes, 50)
        # ATR-14
        atr = [0.0]*n
        for i in range(1,n):
            atr[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        ema_atr = ema_fn(atr, 14)
        # ── Backtest loop ───────────────────────────────────────────────
        trades = []; in_trade = False; trade = {}
        for i in range(50, n):
            S = closes[i]; iv_est = hv[i]; diff = rsi_diff[i]
            if not in_trade:
                # Entry conditions
                signal = False
                if entry_signal == "MEAN_REV_BEAR" and diff >= rsi_ema_threshold:
                    signal = True  # sell call spread when overbought
                elif entry_signal == "MEAN_REV_BULL" and diff <= -rsi_ema_threshold:
                    signal = True  # sell put spread when oversold
                elif entry_signal == "TRENDING" and ema20[i] > ema50[i] and diff > -5:
                    signal = True
                if signal:
                    # Determine strikes based on strategy
                    T = dte_target/365
                    if strategy in ("CS","MEAN_REV_BEAR"):
                        short_k = round(S*(1+wing_pct/100), 1)
                        long_k  = round(short_k*(1+wing_pct/200), 1)
                        short_ep= bs_price(S, short_k, T, iv_est, is_call=True)
                        long_ep = bs_price(S, long_k,  T, iv_est, is_call=True)
                        net_cr  = round(short_ep - long_ep, 3)
                        if net_cr < 0.05: continue
                        trade = {
                            "entry_date": dates[i], "entry_idx": i,
                            "entry_spot": round(S,2),
                            "strategy": "Bear Call Spread",
                            "short_k":short_k,"long_k":long_k,
                            "net_credit":net_cr,"width":round(long_k-short_k,2),
                            "iv":round(iv_est,3),"rsi_diff":round(diff,1),
                            "expiry_idx": min(n-1, i+dte_target),
                        }
                        in_trade = True
                    elif strategy in ("PS","MEAN_REV_BULL"):
                        short_k = round(S*(1-wing_pct/100), 1)
                        long_k  = round(short_k*(1-wing_pct/200), 1)
                        short_ep= bs_price(S, short_k, T, iv_est, is_call=False)
                        long_ep = bs_price(S, long_k,  T, iv_est, is_call=False)
                        net_cr  = round(short_ep - long_ep, 3)
                        if net_cr < 0.05: continue
                        trade = {
                            "entry_date": dates[i], "entry_idx": i,
                            "entry_spot": round(S,2),
                            "strategy": "Bull Put Spread",
                            "short_k":short_k,"long_k":long_k,
                            "net_credit":net_cr,"width":round(short_k-long_k,2),
                            "iv":round(iv_est,3),"rsi_diff":round(diff,1),
                            "expiry_idx": min(n-1, i+dte_target),
                        }
                        in_trade = True
            else:
                # Exit conditions
                ei = trade["expiry_idx"]
                days_in = i - trade["entry_idx"]
                S_now = closes[i]
                T_rem = max(0, (ei-i)/365)
                iv_now = hv[i]
                # Current value of spread
                is_call_spread = "Call" in trade["strategy"]
                sk = trade["short_k"]; lk = trade["long_k"]
                if is_call_spread:
                    curr_val = bs_price(S_now,sk,T_rem,iv_now,is_call=True) - bs_price(S_now,lk,T_rem,iv_now,is_call=True)
                else:
                    curr_val = bs_price(S_now,sk,T_rem,iv_now,is_call=False) - bs_price(S_now,lk,T_rem,iv_now,is_call=False)
                pnl_pct = (trade["net_credit"]-curr_val)/trade["net_credit"] if trade["net_credit"] else 0
                # Exit rules: 50% profit, 200% loss stop, expiry
                exit_reason = None
                if i >= ei:
                    exit_reason = "Expiry"
                elif pnl_pct >= 0.50:
                    exit_reason = "50% Profit"
                elif pnl_pct <= -2.0:
                    exit_reason = "Stop Loss (2×)"
                if exit_reason:
                    max_gain = trade["net_credit"] * 100
                    max_loss = (trade["width"] - trade["net_credit"]) * 100
                    final_pnl = round((trade["net_credit"]-curr_val)*100, 2)
                    trade.update({
                        "exit_date":  dates[i],
                        "exit_spot":  round(S_now,2),
                        "exit_reason":exit_reason,
                        "final_pnl":  final_pnl,
                        "max_gain":   round(max_gain,2),
                        "max_loss":   round(max_loss,2),
                        "days_held":  days_in,
                        "winner":     final_pnl > 0,
                        "pnl_pct_of_max": round(pnl_pct*100,1),
                    })
                    trades.append(dict(trade))
                    in_trade = False; trade = {}
        # ── Stats ───────────────────────────────────────────────────────
        if not trades:
            return jsonify({"trades":[],"stats":{"total":0},"equity_curve":[]})
        winners  = [t for t in trades if t["winner"]]
        losers   = [t for t in trades if not t["winner"]]
        total_pnl= sum(t["final_pnl"] for t in trades)
        # Equity curve
        equity = 0; curve = []
        for t in trades:
            equity += t["final_pnl"]
            curve.append({"date":t["exit_date"],"equity":round(equity,2),"trade_pnl":t["final_pnl"]})
        # Max drawdown
        peak=0; max_dd=0; run=0
        for pt in curve:
            if pt["equity"]>peak: peak=pt["equity"]
            dd=peak-pt["equity"]; max_dd=max(max_dd,dd)
        stats = {
            "total":      len(trades),
            "winners":    len(winners),
            "losers":     len(losers),
            "win_rate":   round(len(winners)/len(trades)*100,1),
            "total_pnl":  round(total_pnl,2),
            "avg_win":    round(sum(t["final_pnl"] for t in winners)/len(winners),2) if winners else 0,
            "avg_loss":   round(sum(t["final_pnl"] for t in losers)/len(losers),2) if losers else 0,
            "max_dd":     round(max_dd,2),
            "avg_days":   round(sum(t["days_held"] for t in trades)/len(trades),1),
            "expectancy": round(total_pnl/len(trades),2),
            "symbol":     symbol,
            "strategy":   strategy,
            "start":      start_date,
            "end":        end_date,
        }
        return jsonify({"trades":trades,"stats":stats,"equity_curve":curve})
    except Exception as e:
        import traceback
        return jsonify({"error":str(e),"trace":traceback.format_exc()[:500]}), 500

# ── Backtest engine: deterministic historical runs + saved-run storage ─────
import sqlite3
from pathlib import Path
from .maya_composite_logic import composite_overlay


def _backtest_db_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "options_data.db")


def _ensure_backtest_tables(con=None):
    local = con is None
    if con is None:
        con = sqlite3.connect(_backtest_db_path(), timeout=30)
        con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanner TEXT,
                backtest_name TEXT,
                run_date TEXT,
                saved_at TEXT,
                params_json TEXT,
                logic_json TEXT,
                stats_json TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtest_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                symbol TEXT,
                signal_date TEXT,
                today_date TEXT,
                run_back_days INTEGER,
                direction TEXT,
                trade_type TEXT,
                winner INTEGER,
                pnl REAL,
                score INTEGER,
                red_signal INTEGER,
                width REAL,
                long_strike REAL,
                short_strike REAL,
                entry_vertical REAL,
                current_vertical REAL,
                row_json TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_scan_date ON backtest_runs(scanner, run_date)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_backtest_rows_run_id ON backtest_rows(run_id)")
        con.commit()
    finally:
        if local:
            con.close()


def _backtest_conn():
    con = sqlite3.connect(_backtest_db_path(), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    _ensure_backtest_tables(con)
    return con


def _last_completed_trading_day(ref: date | None = None) -> date:
    d = (ref or date.today()) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _safe_float(v, dec=4):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, dec)
    except Exception:
        return None


def _ema_series(values, period):
    if not values:
        return []
    out = [float(values[0])]
    k = 2 / (period + 1)
    for v in values[1:]:
        out.append(float(v) * k + out[-1] * (1 - k))
    return out


def _rsi_series(closes, period=14):
    out = [50.0] * len(closes)
    if len(closes) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = float(closes[i]) - float(closes[i - 1])
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        if i > period:
            ag = (ag * (period - 1) + gains[i - 1]) / period
            al = (al * (period - 1) + losses[i - 1]) / period
        rs = ag / al if al > 0 else 100.0
        out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def _macd_series(closes, fast=12, slow=26, signal=9):
    ef = _ema_series(closes, fast)
    es = _ema_series(closes, slow)
    macd = [a - b for a, b in zip(ef, es)]
    sig = _ema_series(macd, signal)
    hist = [a - b for a, b in zip(macd, sig)]
    return macd, sig, hist


def _adx_series(highs, lows, closes, period=14):
    n = len(closes)
    if n < period + 2:
        return [20.0] * n, [25.0] * n, [25.0] * n
    tr, pdm, ndm = [], [], []
    for i in range(1, n):
        up = float(highs[i]) - float(highs[i - 1])
        down = float(lows[i - 1]) - float(lows[i])
        pdm.append(up if up > down and up > 0 else 0.0)
        ndm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(
            float(highs[i]) - float(lows[i]),
            abs(float(highs[i]) - float(closes[i - 1])),
            abs(float(lows[i]) - float(closes[i - 1])),
        ))
    def wilder(vals):
        out = [0.0] * len(vals)
        if len(vals) < period:
            return out
        out[period - 1] = sum(vals[:period]) / period
        for i in range(period, len(vals)):
            out[i] = (out[i - 1] * (period - 1) + vals[i]) / period
        return out
    tr_s = wilder(tr)
    pdm_s = wilder(pdm)
    ndm_s = wilder(ndm)
    pdi = [25.0] * n
    ndi = [25.0] * n
    dx = [0.0] * len(tr)
    for i in range(len(tr)):
        if tr_s[i] == 0:
            continue
        p = 100.0 * (pdm_s[i] / tr_s[i])
        m = 100.0 * (ndm_s[i] / tr_s[i])
        pdi[i + 1] = p
        ndi[i + 1] = m
        dx[i] = 100.0 * abs(p - m) / (p + m) if (p + m) else 0.0
    adx = [20.0] * n
    if len(dx) >= period:
        adx[period] = sum(dx[:period]) / period
        for i in range(period + 1, len(dx)):
            adx[i + 1] = (adx[i] * (period - 1) + dx[i]) / period
    return adx, pdi, ndi


def _atr_series(highs, lows, closes, period=14):
    tr = [0.0]
    for i in range(1, len(closes)):
        tr.append(max(
            float(highs[i]) - float(lows[i]),
            abs(float(highs[i]) - float(closes[i - 1])),
            abs(float(lows[i]) - float(closes[i - 1])),
        ))
    if len(tr) < period:
        return [0.0] * len(tr)
    out = [0.0] * len(tr)
    out[period - 1] = sum(tr[:period]) / period
    for i in range(period, len(tr)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def _hv20_series(closes):
    import math as _math
    rets = [0.0]
    for i in range(1, len(closes)):
        prev = float(closes[i - 1]) or 1e-9
        rets.append((float(closes[i]) / prev) - 1.0)
    hv = [0.20] * len(closes)
    for i in range(20, len(closes)):
        window = rets[i - 19:i + 1]
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / len(window)
        hv[i] = _math.sqrt(var) * (_math.sqrt(252))
    return hv


def _snap_strike(strikes, target, direction="nearest"):
    if not strikes:
        return None
    strikes = sorted(float(s) for s in strikes)
    if direction == "down":
        vals = [s for s in strikes if s <= target]
        return max(vals) if vals else strikes[0]
    if direction == "up":
        vals = [s for s in strikes if s >= target]
        return min(vals) if vals else strikes[-1]
    return min(strikes, key=lambda s: abs(s - target))


def _save_backtest_run(scanner: str, backtest_name: str, run_date: str, params: dict, logic: dict, stats: dict, rows: list[dict]):
    con = _backtest_conn()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO backtest_runs (scanner, backtest_name, run_date, saved_at, params_json, logic_json, stats_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scanner,
                backtest_name,
                run_date,
                datetime.utcnow().isoformat(timespec='seconds'),
                json.dumps(params, default=str),
                json.dumps(logic, default=str),
                json.dumps(stats, default=str),
            ),
        )
        run_id = cur.lastrowid
        row_values = []
        for r in rows:
            row_values.append((
                run_id,
                r.get("symbol"),
                r.get("signal_date"),
                r.get("today_date"),
                int(r.get("run_back_days") or 0),
                r.get("direction"),
                r.get("trade_type"),
                1 if r.get("winner") is True else 0 if r.get("winner") is False else None,
                float(r.get("pnl") or 0),
                int(r.get("score") or 0),
                1 if r.get("red_signal") else 0,
                float(r.get("width") or 0),
                float(r.get("long_strike") or 0) if r.get("long_strike") is not None else None,
                float(r.get("short_strike") or 0) if r.get("short_strike") is not None else None,
                float(r.get("entry_vertical") or 0) if r.get("entry_vertical") is not None else None,
                float(r.get("current_vertical") or 0) if r.get("current_vertical") is not None else None,
                json.dumps(r, default=str),
            ))
        cur.executemany(
            """
            INSERT INTO backtest_rows
              (run_id, symbol, signal_date, today_date, run_back_days, direction, trade_type,
               winner, pnl, score, red_signal, width, long_strike, short_strike,
               entry_vertical, current_vertical, row_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row_values,
        )
        con.commit()
        return run_id
    finally:
        con.close()


def _load_saved_backtest_runs(scanner: str | None = None, run_date: str | None = None, backtest_name: str | None = None, limit: int = 30):
    con = _backtest_conn()
    try:
        q = ["SELECT * FROM backtest_runs WHERE 1=1"]
        params = []
        if scanner:
            q.append("AND scanner=?")
            params.append(scanner)
        if run_date:
            q.append("AND run_date=?")
            params.append(run_date)
        if backtest_name:
            q.append("AND backtest_name LIKE ?")
            params.append(f"%{backtest_name}%")
        q.append("ORDER BY id DESC LIMIT ?")
        params.append(int(limit))
        rows = con.execute(" ".join(q), params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _analyze_saved_backtests(scanner: str | None = None, run_date: str | None = None, backtest_name: str | None = None, limit: int = 25):
    runs = _load_saved_backtest_runs(scanner=scanner, run_date=run_date, backtest_name=backtest_name, limit=limit)
    con = _backtest_conn()
    try:
        details = []
        for r in runs:
            rid = int(r["id"])
            rows = con.execute("SELECT * FROM backtest_rows WHERE run_id=?", (rid,)).fetchall()
            row_dicts = [dict(x) for x in rows]
            signals = [x for x in row_dicts if int(x.get("red_signal") or 0) or x.get("direction")]
            winners = [x for x in signals if int(x.get("winner") or 0) == 1]
            losers = [x for x in signals if int(x.get("winner") or 0) == 0]
            avg_pnl = round(sum(float(x.get("pnl") or 0) for x in signals) / max(1, len(signals)), 2)
            details.append({
                "run": r,
                "signals": len(signals),
                "winners": len(winners),
                "losers": len(losers),
                "win_rate": round((len(winners) / max(1, len(signals))) * 100, 1),
                "avg_pnl": avg_pnl,
                "rows": row_dicts,
            })
        if not details:
            return {"runs": [], "summary": {}, "suggestions": ["No saved backtests matched the filters."]}
        total_signals = sum(d["signals"] for d in details)
        total_winners = sum(d["winners"] for d in details)
        total_losers = sum(d["losers"] for d in details)
        avg_win = round((total_winners / max(1, total_signals)) * 100, 1)
        # Heuristic suggestions
        suggestions = []
        if total_signals:
            win_rate = total_winners / total_signals * 100
            if win_rate < 50:
                suggestions.append("Win rate is below 50%; raise the minimum score or tighten the momentum filter.")
            else:
                suggestions.append("Win rate is healthy; consider testing a wider run span or slightly lower min score.")
        suggestions.append("Compare runs by scanner, run date, and width to see which combination produces the best avg P/L.")
        suggestions.append("If red-signal rows underperform, keep them flagged or exclude them in future runs.")
        return {
            "runs": details,
            "summary": {
                "matched_runs": len(details),
                "signals": total_signals,
                "winners": total_winners,
                "losers": total_losers,
                "win_rate": round((total_winners / max(1, total_signals)) * 100, 1),
                "avg_win_rate": avg_win,
            },
            "suggestions": suggestions,
        }
    finally:
        con.close()


def _simulate_vertical_exit(symbol, closes, highs, lows, vols, dates, sig_idx, scanner, direction, width_mode, dte_target, entry_long, entry_short, entry_iv, as_of_date):
    # Build deterministic signal-specific trade and walk forward day by day.
    trade_side = "CALL" if direction == "BULLISH" else "PUT"
    expiry_date = dates[min(len(dates) - 1, sig_idx + max(1, dte_target))]
    # Use the last available date <= as_of_date for exit horizon.
    end_idx = max(sig_idx, max(i for i, dt in enumerate(dates) if dt <= as_of_date))
    width = abs(float(entry_short) - float(entry_long))
    entry_T = max(1 / 365, (expiry_date - dates[sig_idx]).days / 365.0)
    if trade_side == "CALL":
        entry_val = max(0.01, round(bs_price(closes[sig_idx], entry_long, entry_T, entry_iv, is_call=True) - bs_price(closes[sig_idx], entry_short, entry_T, entry_iv, is_call=True), 2))
    else:
        entry_val = max(0.01, round(bs_price(closes[sig_idx], entry_short, entry_T, entry_iv, is_call=False) - bs_price(closes[sig_idx], entry_long, entry_T, entry_iv, is_call=False), 2))

    exit_idx = end_idx
    exit_reason = "Held to as-of date"
    exit_val = entry_val
    for j in range(sig_idx + 1, end_idx + 1):
        cur_date = dates[j]
        dte_now = max(0, (expiry_date - cur_date).days)
        T_now = max(1 / 365, dte_now / 365.0)
        iv_now = max(0.05, min(1.75, float(_hv20_series(closes[:j + 1])[-1] if j >= 20 else entry_iv)))
        if trade_side == "CALL":
            curr_long = bs_price(closes[j], entry_long, T_now, iv_now, is_call=True)
            curr_short = bs_price(closes[j], entry_short, T_now, iv_now, is_call=True)
            current_vertical = max(0.0, round(curr_long - curr_short, 2))
            ditm = closes[j] >= entry_short
            itm = closes[j] >= entry_long
            macro_flip = _macd_series(closes[:j + 1])[0][-1] < _macd_series(closes[:j + 1])[1][-1] and closes[j] < entry_long
            bullish_against = False
        else:
            curr_short = bs_price(closes[j], entry_short, T_now, iv_now, is_call=False)
            curr_long = bs_price(closes[j], entry_long, T_now, iv_now, is_call=False)
            current_vertical = max(0.0, round(curr_short - curr_long, 2))
            ditm = closes[j] <= entry_short
            itm = closes[j] <= entry_long
            macro_flip = _macd_series(closes[:j + 1])[0][-1] > _macd_series(closes[:j + 1])[1][-1] and closes[j] > entry_long
            bullish_against = True
        pnl_pct = ((current_vertical - entry_val) / entry_val) * 100 if entry_val else 0
        # Exit tree (debit spread): profit target, DITM/ITM near expiration, MACD reversal, PNR breach, 65% loss, <3 DTE partial close.
        atr_now = _atr_series(highs[:j + 1], lows[:j + 1], closes[:j + 1])[-1]
        pnr_now = _compute_pnr(float(entry_long), max(dte_now, 1), float(atr_now or 0))
        pnr_breach = (closes[j] < pnr_now) if (pnr_now is not None and trade_side == "CALL") else (closes[j] > pnr_now if pnr_now is not None else False)
        if current_vertical >= width * 0.94:
            exit_idx = j; exit_reason = "Take profit at 94% max gain"; exit_val = current_vertical; break
        if dte_now <= 7 and itm and ((cur_date.weekday() == 2 and cur_date <= expiry_date) or ditm):
            exit_idx = j; exit_reason = "Wednesday of expiration week / ITM exit"; exit_val = current_vertical; break
        if pnl_pct <= -65:
            exit_idx = j; exit_reason = "65% loss stop"; exit_val = current_vertical; break
        if macro_flip:
            exit_idx = j; exit_reason = "MACD trend flip / price lost long strike"; exit_val = current_vertical; break
        if dte_now < 15 and pnr_breach:
            exit_idx = j; exit_reason = "PNR breach under 15 DTE -> 50% loss"; exit_val = current_vertical; break
        if dte_now < 3:
            exit_idx = j; exit_reason = "<3 DTE partial loss close"; exit_val = current_vertical; break
        # If this is the last date, exit there.
        exit_idx = j; exit_reason = "Held to as-of date"; exit_val = current_vertical
    if exit_idx == end_idx and expiry_date <= as_of_date:
        # At expiry, settle intrinsic.
        S = closes[end_idx]
        if trade_side == "CALL":
            exit_val = max(0.0, min(width, S - entry_long) - max(0.0, S - entry_short))
        else:
            exit_val = max(0.0, min(width, entry_short - S) - max(0.0, entry_long - S))
    pnl = round((exit_val - entry_val) * 100, 2)
    return {
        "entry_vertical": round(entry_val, 2),
        "current_vertical": round(exit_val, 2),
        "pnl": pnl,
        "winner": exit_val > entry_val,
        "exit_date": dates[exit_idx].isoformat(),
        "exit_reason": exit_reason,
        "days_held": (dates[exit_idx] - dates[sig_idx]).days,
        "entry_price": float(closes[sig_idx]),
        "exit_price": float(closes[exit_idx]),
    }


@analysis_bp.route("/momentum_backtest")
def momentum_backtest():
    """
    Deterministic historical backtest for watchlist symbols.
    Supports multiple scanners via the scanner dropdown:
      - momentum: momentum trigger from abs(%change) + volume surge
      - maya: EMA 9/21/50 + MACD/ADX/DMI + RSI health / pullback setup

    The exit tree is evaluated day-by-day and recalculates PNR each day.
    """
    try:
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor, as_completed

        watchlist_id = request.args.get("watchlist_id", type=int)
        raw_symbols = request.args.get("symbols", "").strip()
        days_back = request.args.get("days_back", 30, type=int)
        run_span_days = max(1, request.args.get("run_span_days", 1, type=int))
        dte_target = request.args.get("dte", 30, type=int)
        width_mode = (request.args.get("width", "auto") or "auto").strip().lower()
        min_score = request.args.get("min_score", 0, type=int)
        min_earn_days = request.args.get("min_earn_days", None, type=int)
        scanner = (request.args.get("scanner", "momentum") or "momentum").strip().lower()
        maya_mode = (request.args.get("maya_mode", "core") or "core").strip().lower()
        backtest_name = (request.args.get("backtest_name", "") or "").strip()
        save_run = str(request.args.get("save_run", "0")).lower() not in {"0", "false", "no", "off"}
        run_date = (request.args.get("as_of") or "").strip()
        as_of_date = datetime.strptime(run_date, "%Y-%m-%d").date() if run_date else _last_completed_trading_day()
        maya_controls = {
            "trade_type": (request.args.get("trade_type") or "any").strip().lower(),
            "bias": (request.args.get("bias") or "any").strip().lower(),
            "min_rsi": request.args.get("min_rsi", type=float),
            "max_rsi": request.args.get("max_rsi", type=float),
            "avoid_earnings": str(request.args.get("avoid_earnings", "0")).lower() not in {"0", "false", "no", "off"},
            "strictness": request.args.get("strictness", 2),
            "use_rsi": str(request.args.get("use_rsi", "1")).lower() not in {"0", "false", "no", "off"},
            "use_dmi": str(request.args.get("use_dmi", "1")).lower() not in {"0", "false", "no", "off"},
            "use_ema": str(request.args.get("use_ema", "1")).lower() not in {"0", "false", "no", "off"},
            "use_macd": str(request.args.get("use_macd", "1")).lower() not in {"0", "false", "no", "off"},
            "use_squeeze": str(request.args.get("use_squeeze", "0")).lower() not in {"0", "false", "no", "off"},
            "width": width_mode,
        }

        if raw_symbols:
            symbols = [s.strip().upper() for s in raw_symbols.replace("\n", ",").split(",") if s.strip()]
        else:
            symbols = _load_watchlist_symbols_for_momentum(watchlist_id)
        if not symbols:
            symbols = ["SPY"]
        run_offsets = list(range(days_back, days_back + run_span_days))

        def _choose_width(price, atr_pct, sym):
            if width_mode in ("1", "1-wide", "1wide"):
                return 1
            if width_mode in ("5", "5-wide", "5wide"):
                return 5
            return 1 if (price < 100 or atr_pct <= 2.5 or sym in {"SPY", "QQQ", "IWM", "IVV", "DIA", "XLF", "XLK", "XLV", "XLE"}) else 5

        def _build_trade(sym, lookback_days):
            try:
                target_date = as_of_date - timedelta(days=int(lookback_days))
                hist_start = (target_date - timedelta(days=320)).isoformat()
                hist_end = (as_of_date + timedelta(days=1)).isoformat()
                df = yf.Ticker(sym).history(start=hist_start, end=hist_end, interval="1d", auto_adjust=False)
                if df is None or df.empty or len(df) < 95:
                    return {"symbol": sym, "lookback_days": lookback_days, "error": "insufficient historical data"}
                df = df.dropna(subset=["Close", "High", "Low", "Volume"]).copy()
                if df.empty:
                    return {"symbol": sym, "lookback_days": lookback_days, "error": "empty OHLCV data"}
                dates = [idx.date() if hasattr(idx, "date") else idx for idx in df.index]
                closes = [float(x) for x in df["Close"].tolist()]
                highs = [float(x) for x in df["High"].tolist()]
                lows = [float(x) for x in df["Low"].tolist()]
                vols = [float(x) for x in df["Volume"].tolist()]
                idx = None
                for i in range(len(dates) - 1, -1, -1):
                    if dates[i] <= target_date:
                        idx = i
                        break
                if idx is None or idx < 30:
                    return {"symbol": sym, "lookback_days": lookback_days, "error": "no trading day found for lookback"}
                if idx >= len(closes) - 1:
                    idx = len(closes) - 2

                abs_chg = [0.0]
                for i in range(1, len(closes)):
                    prev = closes[i - 1] or 1e-9
                    abs_chg.append(abs(((closes[i] - closes[i - 1]) / prev) * 100.0))
                abs_ema60 = _ema_series(abs_chg, 60)
                vol_avg20 = []
                for i in range(len(vols)):
                    start = max(0, i - 19)
                    window = vols[start:i + 1]
                    vol_avg20.append(sum(window) / len(window))
                rsi14 = _rsi_series(closes, 14)
                rsi90 = _ema_series(rsi14, 90)
                macd_l, macd_s, macd_h = _macd_series(closes)
                adx14, pdi14, ndi14 = _adx_series(highs, lows, closes, 14)
                hv20 = _hv20_series(closes)
                sig_date = dates[idx]
                cur_close = closes[idx]
                prev_close = closes[idx - 1]
                pct_change = ((cur_close - prev_close) / prev_close) * 100.0 if prev_close else 0.0
                abs_pct_change = abs(pct_change)
                abs_pct_ema60 = abs_ema60[idx] if idx < len(abs_ema60) else 0.0
                vol_now = vols[idx]
                vol_now_avg20 = vol_avg20[idx] if idx < len(vol_avg20) else 0.0
                momentum_hit = abs_pct_change >= (abs_pct_ema60 * 2.0) and vol_now > (vol_now_avg20 * 1.2)
                red_signal = False
                red_reasons = []
                rsi_now = float(rsi14[idx])
                rsi_diff = float(rsi_now - rsi90[idx])
                if rsi_now > 80:
                    red_signal = True; red_reasons.append(f"RSI {rsi_now:.1f} > 80")
                if rsi_now < 20:
                    red_signal = True; red_reasons.append(f"RSI {rsi_now:.1f} < 20")
                if rsi_diff > 20:
                    red_signal = True; red_reasons.append(f"RSI-EMA {rsi_diff:.1f} > 20")
                if rsi_diff < -20:
                    red_signal = True; red_reasons.append(f"RSI-EMA {rsi_diff:.1f} < -20")
                ema9_now = float(_ema_series(closes, 9)[idx])
                ema21_now = float(_ema_series(closes, 21)[idx])
                ema50_now = float(_ema_series(closes, 50)[idx])
                macd_now = float(macd_l[idx])
                macd_sig = float(macd_s[idx])
                macd_hist = float(macd_h[idx])
                adx_now = float(adx14[idx])
                pdi_now = float(pdi14[idx])
                ndi_now = float(ndi14[idx])
                atr_now = _atr_series(highs[:idx + 1], lows[:idx + 1], closes[:idx + 1])[-1]
                atr_pct = round((atr_now / cur_close) * 100, 2) if cur_close else 0
                iv_proxy = max(0.12, min(1.5, float(hv20[idx]) if idx < len(hv20) else 0.25))
                width = _choose_width(cur_close, atr_pct, sym)

                # Signal selection by scanner type
                direction = None
                trade_type = None
                score = 0
                reasons = []
                contrarian = False
                scanner_l = scanner.lower()

                if scanner_l == "maya":
                    from ..maya_pages import _classify_setup as _maya_classify_setup
                    cand = _maya_classify_setup(df.iloc[:idx + 1].copy(), sym, None, 0 if width_mode == "auto" else int(width_mode), maya_mode, maya_controls)
                    if not cand:
                        return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": 0, "direction": None, "error": "no maya signal"}
                    direction = cand.get("direction")
                    trade_type = cand.get("spread_kind") or ("Bull Call Spread" if cand.get("trade_side") == "CALL" and direction == "BULLISH" else "Bull Put Spread" if cand.get("trade_side") == "PUT" and direction == "BULLISH" else "Bear Put Spread" if cand.get("trade_side") == "PUT" and direction == "BEARISH" else "Bear Call Spread")
                    score = int(cand.get("score") or 0)
                    reasons = list(cand.get("notes") or [])[:8]
                    width = int(cand.get("width") or _choose_width(cur_close, atr_pct, sym))
                    long_strike = float(cand.get("long_strike") or 0)
                    short_strike = float(cand.get("short_strike") or 0)
                    if red_signal:
                        score -= 15
                    if maya_mode == "composite":
                        try:
                            overlay = composite_overlay(sym, df.iloc[:idx+1].copy(), idx, direction, cur_close)
                            score += int(overlay.get("score_delta") or 0)
                            reasons.extend([f"Composite: {x}" for x in overlay.get("notes", [])])
                            if overlay.get("contrarian"):
                                red_signal = True
                                red_reasons.extend(overlay.get("flags") or ["Composite contrarian"] )
                                contrarian = True
                            else:
                                contrarian = False
                        except Exception as _e:
                            reasons.append(f"Composite overlay error: {_e}")
                            contrarian = False
                    if score < min_score:
                        return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": int(score), "direction": direction, "error": "no maya signal"}

                elif scanner_l == "momentum":
                    # Legacy momentum rule: abs(%change) versus EMA(abs change) + volume surge
                    if momentum_hit:
                        direction = "BULLISH" if pct_change >= 0 else "BEARISH"
                        trade_type = "Bull Call Spread" if direction == "BULLISH" else "Bear Put Spread"
                        score = min(100, int(max(25, abs_pct_change * 2 + abs_pct_ema60 * 1.5 + (12 if vol_now > vol_now_avg20 * 1.5 else 0))))
                        reasons = [f"{abs_pct_change:.1f}% move", f"Vol {vol_now / max(vol_now_avg20,1):.1f}x", "Momentum surge"]
                    else:
                        return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": 0, "direction": None, "error": "no momentum signal"}

                elif scanner_l in {"rsi_mtf", "rsi-mtf"}:
                    # Daily proxy for RSI MTF: high positive diff -> bearish fade, low negative diff -> bullish bounce
                    bull_hit = rsi_diff <= -rsi_ema_threshold
                    bear_hit = rsi_diff >= rsi_ema_threshold
                    if not bull_hit and not bear_hit:
                        return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": 0, "direction": None, "error": "no rsi mtf signal"}
                    if bull_hit and bear_hit:
                        direction = "BULLISH" if abs(rsi_diff) >= abs(rsi_ema_threshold) else "BEARISH"
                    else:
                        direction = "BULLISH" if bull_hit else "BEARISH"
                    trade_type = "Bull Call Spread" if direction == "BULLISH" else "Bear Put Spread"
                    score = min(100, int(40 + min(35, abs(rsi_diff) * 1.2) + (10 if (direction == "BULLISH" and cur_close > ema50_now) or (direction == "BEARISH" and cur_close < ema50_now) else 0) + (10 if (direction == "BULLISH" and macd_hist > 0) or (direction == "BEARISH" and macd_hist < 0) else 0)))
                    reasons = [f"RSI-EMA diff {rsi_diff:+.1f}", f"RSI {rsi_now:.1f}"]

                elif scanner_l in {"trend_exhaustion", "exhaustion"}:
                    bull_hit = (rsi_now <= 32 and rsi_diff <= -abs(rsi_ema_threshold))
                    bear_hit = (rsi_now >= 68 and rsi_diff >= abs(rsi_ema_threshold))
                    if not bull_hit and not bear_hit:
                        return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": 0, "direction": None, "error": "no exhaustion signal"}
                    direction = "BULLISH" if bull_hit and not bear_hit else "BEARISH" if bear_hit and not bull_hit else ("BULLISH" if abs(rsi_diff) >= abs(rsi_ema_threshold) else "BEARISH")
                    trade_type = "Bull Call Spread" if direction == "BULLISH" else "Bear Put Spread"
                    score = min(100, int(50 + min(25, abs(rsi_diff) * 1.1) + (10 if (direction == "BULLISH" and cur_close < ema20_now) or (direction == "BEARISH" and cur_close > ema20_now) else 0) + (10 if (direction == "BULLISH" and macd_hist < 0) or (direction == "BEARISH" and macd_hist > 0) else 0)))
                    reasons = [f"RSI {rsi_now:.1f}", f"RSI-EMA diff {rsi_diff:+.1f}"]

                elif scanner_l in {"sr", "s_r", "breakout", "breakdown"}:
                    look = min(20, idx)
                    if look < 5:
                        return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": 0, "direction": None, "error": "insufficient sr lookback"}
                    prior_hi = max(highs[idx - look:idx])
                    prior_lo = min(lows[idx - look:idx])
                    vol_ratio = vol_now / max(vol_now_avg20, 1)
                    bull_hit = cur_close > prior_hi and vol_ratio >= 1.15
                    bear_hit = cur_close < prior_lo and vol_ratio >= 1.15
                    if not bull_hit and not bear_hit:
                        return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": 0, "direction": None, "error": "no sr breakout signal"}
                    direction = "BULLISH" if bull_hit and not bear_hit else "BEARISH" if bear_hit and not bull_hit else ("BULLISH" if cur_close >= prior_hi else "BEARISH")
                    trade_type = "Bull Call Spread" if direction == "BULLISH" else "Bear Put Spread"
                    score = min(100, int(45 + min(25, abs(pct_change) * 2) + min(20, vol_ratio * 8)))
                    reasons = [f"{('Break above' if direction == 'BULLISH' else 'Break below')} {prior_hi if direction == 'BULLISH' else prior_lo:.2f}", f"Vol {vol_ratio:.1f}x"]

                elif scanner_l in {"momentum_retrace", "mom_retrace", "retrace"}:
                    # Simplified first-pullback proxy for backtest
                    move_window = min(10, idx)
                    if move_window < 5:
                        return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": 0, "direction": None, "error": "insufficient momentum lookback"}
                    move_hi = max(closes[idx - move_window:idx + 1])
                    move_lo = min(closes[idx - move_window:idx + 1])
                    up_move = (move_hi - closes[idx - move_window]) / max(closes[idx - move_window], 1e-9) * 100
                    down_move = (closes[idx - move_window] - move_lo) / max(closes[idx - move_window], 1e-9) * 100
                    bull_hit = up_move >= 6 and cur_close > ema20_now and cur_close < move_hi and rsi_now <= 65
                    bear_hit = down_move >= 6 and cur_close < ema20_now and cur_close > move_lo and rsi_now >= 35
                    if not bull_hit and not bear_hit:
                        return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": 0, "direction": None, "error": "no retrace signal"}
                    direction = "BULLISH" if bull_hit and not bear_hit else "BEARISH" if bear_hit and not bull_hit else ("BULLISH" if up_move >= down_move else "BEARISH")
                    trade_type = "Bull Call Spread" if direction == "BULLISH" else "Bear Put Spread"
                    score = min(100, int(55 + min(20, max(up_move, down_move)) + (8 if (direction == "BULLISH" and rsi_now <= 65) or (direction == "BEARISH" and rsi_now >= 35) else 0)))
                    reasons = [f"Move {up_move:.1f}%" if direction == "BULLISH" else f"Move {down_move:.1f}%", f"RSI {rsi_now:.1f}"]

                elif scanner_l in {"second_pullback", "trend_second_pullback", "tep"}:
                    # Use the dedicated scanner and take the best recent result that matches this symbol.
                    try:
                        from .trend_second_pullback_scanner import _scan_one as _tep_scan_one
                        tep = _tep_scan_one(sym, lookback_days=max(lookback_days, 20), exhaust_tf="1d", pullback_tf="1h")
                        if not tep:
                            return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": 0, "direction": None, "error": "no second pullback signal"}
                        direction = "BULLISH" if str(tep.get("direction", "")).startswith("BULL") else "BEARISH"
                        trade_type = "Bull Call Spread" if direction == "BULLISH" else "Bear Put Spread"
                        score = int(tep.get("score") or 0)
                        reasons = list(tep.get("notes") or [])[:8]
                    except Exception as _e:
                        return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": 0, "direction": None, "error": f"second pullback scan failed: {_e}"}

                else:
                    return {"symbol": sym, "lookback_days": lookback_days, "signal": False, "score": 0, "direction": None, "error": f"unsupported scanner: {scanner}"}

                # Build strike pair (ATM/slightly ITM)
                if direction == "BULLISH":
                    long_target = round(cur_close / 5) * 5 if cur_close >= 100 else round(cur_close)
                    if long_target >= cur_close:
                        long_target -= 1 if cur_close < 100 else 5
                    long_strike = float(long_target)
                    short_strike = float(long_strike + width)
                else:
                    short_target = round(cur_close / 5) * 5 if cur_close >= 100 else round(cur_close)
                    if short_target <= cur_close:
                        short_target += 1 if cur_close < 100 else 5
                    short_strike = float(short_target)
                    long_strike = float(short_strike - width)

                # Determine trade leg pricing deterministically at signal date
                expiry_date = min(as_of_date, sig_date + timedelta(days=max(1, dte_target)))
                # Use the nearest available history row to the intended expiry date.
                exp_idx = idx
                while exp_idx < len(dates) - 1 and dates[exp_idx] < expiry_date:
                    exp_idx += 1
                T_entry = max(1 / 365, (expiry_date - sig_date).days / 365.0)
                if direction == "BULLISH":
                    entry_long_px = bs_price(cur_close, long_strike, T_entry, iv_proxy, is_call=True)
                    entry_short_px = bs_price(cur_close, short_strike, T_entry, iv_proxy, is_call=True)
                else:
                    entry_long_px = bs_price(cur_close, long_strike, T_entry, iv_proxy, is_call=False)
                    entry_short_px = bs_price(cur_close, short_strike, T_entry, iv_proxy, is_call=False)
                entry_vertical = max(0.01, round(entry_long_px - entry_short_px, 2)) if direction == "BULLISH" else max(0.01, round(entry_short_px - entry_long_px, 2))

                latest_idx = max(i for i, dt in enumerate(dates) if dt <= as_of_date)
                exit_info = _simulate_vertical_exit(sym, closes, highs, lows, vols, dates, idx, scanner, direction, width_mode, dte_target, long_strike, short_strike, iv_proxy, as_of_date)
                # include signal-level data in response
                return {
                    "symbol": sym,
                    "signal_date": sig_date.isoformat(),
                    "today_date": as_of_date.isoformat(),
                    "lookback_days": lookback_days,
                    "signal_price": round(cur_close, 2),
                    "today_price": round(closes[latest_idx], 2),
                    "pct_change": round(pct_change, 2),
                    "abs_pct_change": round(abs_pct_change, 2),
                    "abs_pct_ema60": round(abs_pct_ema60, 2),
                    "volume": int(vol_now),
                    "volume_avg20": round(vol_now_avg20, 0),
                    "momentum_hit": bool(momentum_hit),
                    "signal": True,
                    "scanner": scanner,
                    "direction": direction,
                    "trade_type": trade_type,
                    "red_signal": bool(red_signal),
                    "contrarian": bool(contrarian),
                    "red_reasons": red_reasons,
                    "rsi14": round(rsi_now, 1),
                    "rsi14_ema90": round(rsi90[idx], 1),
                    "rsi_diff": round(rsi_diff, 1),
                    "macd": round(macd_now, 4),
                    "macd_signal": round(macd_sig, 4),
                    "macd_hist": round(macd_hist, 4),
                    "adx": round(adx_now, 1),
                    "pdi": round(pdi_now, 1),
                    "ndi": round(ndi_now, 1),
                    "ema9": round(ema9_now, 2),
                    "ema21": round(ema21_now, 2),
                    "ema50": round(ema50_now, 2),
                    "width": float(width),
                    "expiry": expiry_date.isoformat(),
                    "long_strike": round(float(long_strike), 2),
                    "short_strike": round(float(short_strike), 2),
                    "entry_vertical": round(entry_vertical, 2),
                    "current_vertical": round(exit_info["current_vertical"], 2),
                    "pnl": exit_info["pnl"],
                    "pnl_pct": round(((exit_info["current_vertical"] - entry_vertical) / entry_vertical) * 100, 1) if entry_vertical else None,
                    "winner": exit_info["winner"],
                    "value_note": exit_info["exit_reason"],
                    "score": int(min(100, max(0, score))),
                    "reasons": reasons[:8],
                    "iv_proxy": round(iv_proxy, 3),
                    "status": "WINNER" if exit_info["winner"] else "LOSER",
                    "run_back_days": lookback_days,
                    "exit_date": exit_info["exit_date"],
                    "days_held": exit_info["days_held"],
                    "entry_price": round(float(cur_close), 2),
                    "exit_price": round(float(exit_info.get("exit_price", closes[latest_idx])), 2),
                    "directional_points": round((float(exit_info.get("exit_price", closes[latest_idx])) - float(cur_close)) * (1 if direction == "BULLISH" else -1), 2),
                }
            except Exception as e:
                return {"symbol": sym, "lookback_days": lookback_days, "error": str(e)}

        rows = []
        with ThreadPoolExecutor(max_workers=min(10, max(2, len(symbols)))) as ex:
            futs = [ex.submit(_build_trade, sym, lookback) for lookback in run_offsets for sym in symbols]
            for fut in as_completed(futs):
                rows.append(fut.result())

        total_processed = len(symbols) * len(run_offsets)
        rows_ok = [r for r in rows if not r.get("error") and (r.get("signal") or r.get("score", 0) >= min_score)]
        rows_ok.sort(key=lambda r: (
            r.get("run_back_days", 0),
            not r.get("momentum_hit", False),
            -(r.get("score", 0) or 0),
            -(abs(r.get("pct_change", 0) or 0)),
            r.get("symbol", "")
        ))
        signal_rows = [r for r in rows_ok if r.get("signal")]
        signal_winners = [r for r in signal_rows if r.get("winner") is True]
        signal_losers = [r for r in signal_rows if r.get("winner") is False]
        signal_open = [r for r in signal_rows if r.get("winner") is None]
        red_hits = len([r for r in signal_rows if r.get("red_signal")])
        dir_pts = [float(r.get("directional_points", 0) or 0) for r in signal_rows]
        hit_1pt = len([p for p in dir_pts if p >= 1.0])
        hit_5pt = len([p for p in dir_pts if p >= 5.0])
        avg_move = round(sum(abs(r.get("pct_change", 0) or 0) for r in signal_rows) / len(signal_rows), 2) if signal_rows else 0
        avg_pnl = round(sum(r.get("pnl", 0) or 0 for r in signal_rows if r.get("pnl") is not None) / max(1, len([r for r in signal_rows if r.get("pnl") is not None])), 2)
        stats = {
            "scanner": scanner,
            "symbols": len(symbols),
            "runs": len(run_offsets),
            "processed": total_processed,
            "evaluated": len(rows_ok),
            "qualified": len(rows_ok),
            "signals": len(signal_rows),
            "signal_winners": len(signal_winners),
            "signal_losers": len(signal_losers),
            "signal_open": len(signal_open),
            "win_rate": round((len(signal_winners) / max(1, len(signal_rows))) * 100, 1) if signal_rows else 0,
            "avg_move": avg_move,
            "avg_pnl": avg_pnl,
            "red_signals": red_hits,
            "hit_1pt": hit_1pt,
            "hit_5pt": hit_5pt,
            "lookback_days": days_back,
            "run_span_days": run_span_days,
            "dte": dte_target,
            "width": width_mode,
            "maya_mode": maya_mode,
            "backtest_name": backtest_name,
            "run_date": as_of_date.isoformat(),
        }

        run_id = None
        if save_run:
            run_id = _save_backtest_run(
                scanner=scanner,
                backtest_name=backtest_name,
                run_date=as_of_date.isoformat(),
                params={
                    "watchlist_id": watchlist_id,
                    "symbols": symbols,
                    "days_back": days_back,
                    "run_span_days": run_span_days,
                    "dte": dte_target,
                    "width": width_mode,
                    "min_score": min_score,
                    "scanner": scanner,
                    "maya_mode": maya_mode,
                    "trade_type": maya_controls.get("trade_type"),
                    "bias": maya_controls.get("bias"),
                    "min_rsi": maya_controls.get("min_rsi"),
                    "max_rsi": maya_controls.get("max_rsi"),
                    "avoid_earnings": maya_controls.get("avoid_earnings"),
                    "strictness": maya_controls.get("strictness"),
                    "use_rsi": maya_controls.get("use_rsi"),
                    "use_dmi": maya_controls.get("use_dmi"),
                    "use_ema": maya_controls.get("use_ema"),
                    "use_macd": maya_controls.get("use_macd"),
                    "use_squeeze": maya_controls.get("use_squeeze"),
                },
                logic={
                    "scanner": scanner,
                    "maya_mode": maya_mode,
                    "controls": maya_controls,
                    "momentum": "abs(%change) >= EMA(abs(%change),60) * 2 AND volume > average(volume,20) * 1.2",
                    "maya": "EMA 9/21/50 stack + MACD/ADX/DMI + RSI health/pullback",
                    "red_signal": "RSI14 > 80 OR RSI14 < 20 OR RSI14-EMA(RSI14,90) beyond ±20",
                    "exit_tree": "Daily PNR recalculation + profit target + Wednesday/ITM + 65% loss + MACD flip + <15 DTE PNR breach + <3 DTE partial close",
                },
                stats=stats,
                rows=rows_ok,
            )
            stats["run_id"] = run_id

        return jsonify({
            "method": {
                "scanner": scanner,
                "supported_scanners": ["momentum", "maya", "rsi_mtf", "sr", "trend_exhaustion", "momentum_retrace", "second_pullback"],
                "maya_mode": maya_mode,
                "momentum": "abs(%change) >= EMA(abs(%change),60) * 2 AND volume > average(volume,20) * 1.2",
                "maya": "EMA 9/21/50 stack + MACD/ADX/DMI + RSI health/pullback",
                "mode": maya_mode,
                "red_signal": "RSI14 > 80 OR RSI14 < 20 OR RSI14-EMA(RSI14,90) beyond ±20",
                "exit_tree": "Daily PNR recalculation + profit target + Wednesday/ITM + 65% loss + MACD flip + <15 DTE PNR breach + <3 DTE partial close",
                "pricing": "deterministic historical BS pricing; no live quote dependency in backtests",
                "controls": maya_controls,
            },
            "stats": stats,
            "rows": rows_ok,
            "signal_rows": signal_rows,
            "errors": [r for r in rows if r.get("error")],
            "watchlist": symbols,
            "scanner": scanner,
            "backtest_name": backtest_name,
            "run_id": run_id,
            "as_of": as_of_date.isoformat(),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:800]}), 500


@analysis_bp.route("/backtest_runs")
def backtest_runs():
    scanner = (request.args.get("scanner") or "").strip().lower() or None
    run_date = (request.args.get("run_date") or "").strip() or None
    backtest_name = (request.args.get("backtest_name") or "").strip() or None
    limit = request.args.get("limit", 30, type=int)
    return jsonify({"runs": _load_saved_backtest_runs(scanner, run_date, backtest_name, limit)})


@analysis_bp.route("/backtest_analyze")
def backtest_analyze():
    scanner = (request.args.get("scanner") or "").strip().lower() or None
    run_date = (request.args.get("run_date") or "").strip() or None
    backtest_name = (request.args.get("backtest_name") or "").strip() or None
    limit = request.args.get("limit", 25, type=int)
    return jsonify(_analyze_saved_backtests(scanner, run_date, backtest_name, limit))

@analysis_bp.route("/iv/<symbol>/<expiry>/<strike>/<opt_type>")
def get_iv(symbol, expiry, strike, opt_type):
    """Fetch implied volatility for a specific option from yfinance."""
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol.upper())
        chain = tk.option_chain(expiry)
        df = chain.calls if opt_type.lower()=='call' else chain.puts
        k = float(strike)
        # Find nearest strike
        row = df.iloc[(df['strike']-k).abs().argsort()[:1]]
        if row.empty: return jsonify({"iv": 0.25, "source": "fallback"})
        iv = float(row['impliedVolatility'].iloc[0])
        price = float(row['lastPrice'].iloc[0])
        return jsonify({"iv": round(iv,4), "price": round(price,4), "strike": float(row['strike'].iloc[0]), "source": "live"})
    except Exception as e:
        return jsonify({"iv": 0.25, "price": 0, "source": "fallback", "error": str(e)})
@analysis_bp.route("/spot/<symbol>")
def get_spot(symbol):
    """Fetch current spot price for a symbol."""
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol.upper())
        hist = tk.history(period="1d", interval="1m")
        if hist.empty:
            info = tk.info
            spot = info.get("regularMarketPrice") or info.get("currentPrice") or 0
        else:
            spot = float(hist["Close"].iloc[-1])
        return jsonify({"spot": round(spot, 2), "symbol": symbol.upper()})
    except Exception as e:
        return jsonify({"spot": 0, "error": str(e)})

# ════════════════════════════════════════════════════════════════════════════
# UAE TrendVol v3 Backtest
# Tests the Trend/Vol Analyzer v3 signal against options trades at
# multiple DTEs so you can find the optimal expiry for your signal.
# ════════════════════════════════════════════════════════════════════════════

def _uae_trendvol_signals(closes, highs, lows, fast=10, slow=20,
                           signal_len=5, roc_len=3, atr_len=14,
                           adx_len=14, slope_len=5, vol_mult=1.5):
    """
    Compute UAE TrendVol v3 signals on daily OHLCV data.
    Returns a list of dicts per bar with:
      macd, signal, hist, regime, regime_label, adx, ema_slope
    Regime: 1=BULL, 2=WEAK_BULL, 0=SIDEWAYS, -2=WEAK_BEAR, -1=BEAR
    """
    import math as _m
    n = len(closes)

    # ATR + baseline
    atr_raw = _atr_series(highs, lows, closes, atr_len)
    atr_base = _ema_series(atr_raw, slow)
    safe_atr = [max(b, 1e-6) for b in atr_base]

    # Slow EMA
    slow_ema = _ema_series(closes, slow)

    # Normalized trend position
    trend_pos = [(closes[i] - slow_ema[i]) / safe_atr[i] for i in range(n)]

    # Rate of change of trend position (speed)
    trend_mom = [0.0] * n
    for i in range(roc_len, n):
        trend_mom[i] = trend_pos[i] - trend_pos[i - roc_len]

    # Vol amplifier
    vol_amp = [max(0.1, atr_raw[i] / safe_atr[i]) for i in range(n)]

    # Composite: momentum scaled by vol
    raw_sig = [trend_mom[i] * (vol_amp[i] ** vol_mult) for i in range(n)]

    # Single smooth layer
    macd_line = _ema_series(raw_sig, fast)
    sig_line  = _ema_series(macd_line, signal_len)
    hist_line = [macd_line[i] - sig_line[i] for i in range(n)]

    # ADX
    adx_vals, pdi_vals, ndi_vals = _adx_series(highs, lows, closes, adx_len)

    # EMA slope (normalized)
    ema_slope = [0.0] * n
    for i in range(slope_len, n):
        ema_slope[i] = (slow_ema[i] - slow_ema[i - slope_len]) / slope_len / safe_atr[i]

    results = []
    for i in range(n):
        adx = adx_vals[i]
        slope = ema_slope[i]
        hist = hist_line[i]
        trending = adx > 20.0
        bull_slope = slope > 0
        bear_slope = slope < 0

        if trending and bull_slope and hist > 0:
            regime = 1
        elif trending and bull_slope and hist <= 0:
            regime = 2
        elif trending and bear_slope and hist < 0:
            regime = -1
        elif trending and bear_slope and hist >= 0:
            regime = -2
        else:
            regime = 0

        labels = {1: "BULL", 2: "WEAK_BULL", 0: "SIDEWAYS", -2: "WEAK_BEAR", -1: "BEAR"}
        results.append({
            "macd":          round(macd_line[i], 6),
            "signal":        round(sig_line[i],  6),
            "hist":          round(hist,          6),
            "adx":           round(adx,           2),
            "ema_slope":     round(slope,         6),
            "regime":        regime,
            "regime_label":  labels[regime],
            "trending":      trending,
            "trend_pos":     round(trend_pos[i],  4),
            "vol_amp":       round(vol_amp[i],    4),
        })
    return results


def _uae_dte_backtest(closes, highs, lows, dates, hv_series,
                       dte, strategy, signals, wing_pct=3.0,
                       profit_target=0.50, stop_loss_mult=2.0,
                       signal_filter="BULL_ENTRY"):
    """
    Run one pass of the backtest for a specific DTE.
    signal_filter options:
      BULL_ENTRY  - enter PS when regime turns BULL or crosses macd>0
      BEAR_ENTRY  - enter CS when regime turns BEAR or crosses macd<0
      BOTH        - enter PS on bull signal, CS on bear signal
    Returns list of trade dicts.
    """
    n = len(closes)
    trades = []
    in_trade = False
    trade = {}

    for i in range(50, n):
        sig = signals[i]
        prev_sig = signals[i - 1]
        S = closes[i]
        iv = max(0.05, min(1.8, hv_series[i]))

        if not in_trade:
            # Entry logic: regime change to BULL/BEAR or macd zero-cross while trending
            enter_bull = False
            enter_bear = False

            if signal_filter in ("BULL_ENTRY", "BOTH"):
                # Bull entry: macd crosses above 0 while trending bull slope
                macd_cross_bull = sig["macd"] > 0 and prev_sig["macd"] <= 0 and sig["trending"]
                regime_bull_start = sig["regime"] == 1 and prev_sig["regime"] != 1
                enter_bull = macd_cross_bull or regime_bull_start

            if signal_filter in ("BEAR_ENTRY", "BOTH"):
                # Bear entry: macd crosses below 0 while trending bear slope
                macd_cross_bear = sig["macd"] < 0 and prev_sig["macd"] >= 0 and sig["trending"]
                regime_bear_start = sig["regime"] == -1 and prev_sig["regime"] != -1
                enter_bear = macd_cross_bear or regime_bear_start

            if enter_bull and strategy in ("PS", "BOTH"):
                # Bull Put Spread: sell OTM put, buy lower put
                short_k = round(S * (1 - wing_pct / 100), 2)
                long_k  = round(short_k * (1 - wing_pct / 100), 2)
                T = dte / 365.0
                cr = bs_price(S, short_k, T, iv, is_call=False) -                      bs_price(S, long_k,  T, iv, is_call=False)
                cr = round(cr, 3)
                if cr >= 0.05:
                    expiry_idx = min(n - 1, i + dte)
                    trade = {
                        "entry_date":  str(dates[i]),
                        "entry_idx":   i,
                        "entry_spot":  round(S, 2),
                        "strategy":    "PS",
                        "short_k":     short_k,
                        "long_k":      long_k,
                        "net_credit":  cr,
                        "width":       round(short_k - long_k, 2),
                        "iv_entry":    round(iv, 3),
                        "expiry_idx":  expiry_idx,
                        "dte_target":  dte,
                        "regime_entry": sig["regime_label"],
                        "adx_entry":   sig["adx"],
                        "hist_entry":  sig["hist"],
                        "macd_entry":  sig["macd"],
                    }
                    in_trade = True

            elif enter_bear and strategy in ("CS", "BOTH"):
                # Bear Call Spread
                short_k = round(S * (1 + wing_pct / 100), 2)
                long_k  = round(short_k * (1 + wing_pct / 100), 2)
                T = dte / 365.0
                cr = bs_price(S, short_k, T, iv, is_call=True) -                      bs_price(S, long_k,  T, iv, is_call=True)
                cr = round(cr, 3)
                if cr >= 0.05:
                    expiry_idx = min(n - 1, i + dte)
                    trade = {
                        "entry_date":  str(dates[i]),
                        "entry_idx":   i,
                        "entry_spot":  round(S, 2),
                        "strategy":    "CS",
                        "short_k":     short_k,
                        "long_k":      long_k,
                        "net_credit":  cr,
                        "width":       round(long_k - short_k, 2),
                        "iv_entry":    round(iv, 3),
                        "expiry_idx":  expiry_idx,
                        "dte_target":  dte,
                        "regime_entry": sig["regime_label"],
                        "adx_entry":   sig["adx"],
                        "hist_entry":  sig["hist"],
                        "macd_entry":  sig["macd"],
                    }
                    in_trade = True

        else:
            # Exit logic
            ei = trade["expiry_idx"]
            days_in = i - trade["entry_idx"]
            T_rem = max(0.0001, (ei - i) / 365.0)
            iv_now = max(0.05, min(1.8, hv_series[i]))
            sk, lk = trade["short_k"], trade["long_k"]
            is_ps = trade["strategy"] == "PS"

            if is_ps:
                curr_val = bs_price(closes[i], sk, T_rem, iv_now, is_call=False) -                            bs_price(closes[i], lk, T_rem, iv_now, is_call=False)
            else:
                curr_val = bs_price(closes[i], sk, T_rem, iv_now, is_call=True) -                            bs_price(closes[i], lk, T_rem, iv_now, is_call=True)

            cr = trade["net_credit"]
            pnl_pct = (cr - curr_val) / cr if cr else 0

            # Also exit if regime flips against trade
            sig_now = signals[i]
            regime_exit = False
            if is_ps and sig_now["regime"] in (-1, -2):
                regime_exit = True
            elif not is_ps and sig_now["regime"] in (1, 2):
                regime_exit = True

            exit_reason = None
            if i >= ei:
                exit_reason = "Expiry"
            elif pnl_pct >= profit_target:
                exit_reason = f"{int(profit_target*100)}pct Profit"
            elif pnl_pct <= -stop_loss_mult:
                exit_reason = f"Stop ({int(stop_loss_mult*100)}pct Loss)"
            elif regime_exit:
                exit_reason = "Regime Flip Exit"

            if exit_reason:
                final_pnl = round((cr - curr_val) * 100, 2)
                trade.update({
                    "exit_date":      str(dates[i]),
                    "exit_idx":       i,
                    "exit_spot":      round(closes[i], 2),
                    "exit_reason":    exit_reason,
                    "final_pnl":      final_pnl,
                    "max_gain":       round(cr * 100, 2),
                    "max_loss":       round((trade["width"] - cr) * 100, 2),
                    "days_held":      days_in,
                    "winner":         final_pnl > 0,
                    "pnl_pct_of_max": round(pnl_pct * 100, 1),
                    "regime_exit":    sig_now["regime_label"],
                })
                trades.append(dict(trade))
                in_trade = False
                trade = {}

    return trades


def _dte_stats(trades, dte):
    """Compute summary stats for one DTE bucket."""
    if not trades:
        return {"dte": dte, "total": 0, "win_rate": 0, "total_pnl": 0,
                "expectancy": 0, "avg_days": 0, "max_dd": 0, "profit_factor": 0}
    winners = [t for t in trades if t["winner"]]
    losers  = [t for t in trades if not t["winner"]]
    total_pnl = sum(t["final_pnl"] for t in trades)
    gross_win = sum(t["final_pnl"] for t in winners) if winners else 0
    gross_loss = abs(sum(t["final_pnl"] for t in losers)) if losers else 1e-9
    # Max drawdown
    eq = 0; peak = 0; max_dd = 0
    for t in trades:
        eq += t["final_pnl"]
        if eq > peak: peak = eq
        dd = peak - eq
        if dd > max_dd: max_dd = dd
    return {
        "dte":            dte,
        "total":          len(trades),
        "winners":        len(winners),
        "losers":         len(losers),
        "win_rate":       round(len(winners) / len(trades) * 100, 1),
        "total_pnl":      round(total_pnl, 2),
        "avg_win":        round(gross_win / len(winners), 2) if winners else 0,
        "avg_loss":       round(-gross_loss / len(losers), 2) if losers else 0,
        "expectancy":     round(total_pnl / len(trades), 2),
        "avg_days":       round(sum(t["days_held"] for t in trades) / len(trades), 1),
        "max_dd":         round(max_dd, 2),
        "profit_factor":  round(gross_win / gross_loss, 2) if gross_loss else 0,
        "avg_adx_entry":  round(sum(t.get("adx_entry",20) for t in trades) / len(trades), 1),
        "pct_profit_exit":round(sum(1 for t in trades if "Profit" in t.get("exit_reason","")) / len(trades) * 100, 1),
        "pct_regime_exit":round(sum(1 for t in trades if "Regime" in t.get("exit_reason","")) / len(trades) * 100, 1),
    }


@analysis_bp.route("/uae_backtest")
def uae_backtest():
    """
    UAE TrendVol v3 backtest with DTE sweep.

    Parameters:
      symbol      - ticker (default SPY)
      strategy    - PS (bull put), CS (bear call), BOTH (default BOTH)
      start       - start date (default 2022-01-01)
      end         - end date (default today)
      wing_pct    - % OTM for short strike (default 3.0)
      dte_list    - comma-separated DTEs to test (default 7,14,21,28,45)
      profit_tgt  - profit target fraction (default 0.50)
      stop_mult   - stop loss as multiple of credit (default 2.0)
      fast        - fast EMA (default 10)
      slow        - slow EMA (default 20)
      signal_len  - signal smooth (default 5)
      roc_len     - ROC period (default 3)
      adx_thr     - ADX threshold (default 20)
    """
    try:
        import yfinance as yf

        symbol      = request.args.get("symbol", "SPY").upper()
        strategy    = request.args.get("strategy", "BOTH").upper()
        start       = request.args.get("start", "2022-01-01")
        end         = request.args.get("end", date.today().isoformat())
        wing_pct    = float(request.args.get("wing_pct", 3.0))
        profit_tgt  = float(request.args.get("profit_tgt", 0.50))
        stop_mult   = float(request.args.get("stop_mult", 2.0))
        fast        = int(request.args.get("fast", 10))
        slow_p      = int(request.args.get("slow", 20))
        signal_len  = int(request.args.get("signal_len", 5))
        roc_len     = int(request.args.get("roc_len", 3))
        adx_thr     = float(request.args.get("adx_thr", 20))
        vol_mult    = float(request.args.get("vol_mult", 1.5))

        raw_dtes = request.args.get("dte_list", "7,14,21,28,45")
        dte_list = [int(x.strip()) for x in raw_dtes.split(",") if x.strip().isdigit()]
        if not dte_list:
            dte_list = [7, 14, 21, 28, 45]

        signal_filter = "BOTH" if strategy == "BOTH" else                         "BULL_ENTRY" if strategy == "PS" else "BEAR_ENTRY"

        # Fetch data
        df = yf.Ticker(symbol).history(start=start, end=end, interval="1d", auto_adjust=False)
        if df is None or df.empty or len(df) < 80:
            return jsonify({"error": "Insufficient data for " + symbol}), 400

        df = df.dropna(subset=["Close", "High", "Low"])
        closes = [float(x) for x in df["Close"].tolist()]
        highs  = [float(x) for x in df["High"].tolist()]
        lows   = [float(x) for x in df["Low"].tolist()]
        dates  = [str(d.date()) if hasattr(d, "date") else str(d) for d in df.index]
        n = len(closes)

        # Compute HV20 for IV proxy
        hv_series = _hv20_series(closes)

        # Compute UAE TrendVol v3 signals once
        signals = _uae_trendvol_signals(
            closes, highs, lows,
            fast=fast, slow=slow_p, signal_len=signal_len,
            roc_len=roc_len, atr_len=14, adx_len=14,
            slope_len=5, vol_mult=vol_mult,
        )

        # Count signal distribution
        regime_counts = {}
        for s in signals[50:]:
            r = s["regime_label"]
            regime_counts[r] = regime_counts.get(r, 0) + 1
        total_bars = sum(regime_counts.values())
        regime_pct = {k: round(v / total_bars * 100, 1) for k, v in regime_counts.items()}

        # Count entry signals
        entry_signals = []
        for i in range(1, n):
            sig = signals[i]; prev = signals[i-1]
            macd_bull = sig["macd"] > 0 and prev["macd"] <= 0 and sig["trending"]
            macd_bear = sig["macd"] < 0 and prev["macd"] >= 0 and sig["trending"]
            reg_bull  = sig["regime"] == 1 and prev["regime"] != 1
            reg_bear  = sig["regime"] == -1 and prev["regime"] != -1
            if macd_bull or reg_bull or macd_bear or reg_bear:
                entry_signals.append({
                    "date": dates[i],
                    "type": "BULL" if (macd_bull or reg_bull) else "BEAR",
                    "trigger": "MACD_CROSS" if (macd_bull or macd_bear) else "REGIME_CHANGE",
                    "regime": sig["regime_label"],
                    "adx": sig["adx"],
                    "macd": sig["macd"],
                    "hist": sig["hist"],
                })

        # Run backtest for each DTE
        dte_results = {}
        all_trades_by_dte = {}

        for dte in dte_list:
            trades = _uae_dte_backtest(
                closes, highs, lows, dates, hv_series,
                dte=dte, strategy=strategy,
                signals=signals, wing_pct=wing_pct,
                profit_target=profit_tgt, stop_loss_mult=stop_mult,
                signal_filter=signal_filter,
            )
            dte_results[dte] = _dte_stats(trades, dte)
            all_trades_by_dte[dte] = trades

        # Find best DTE by expectancy, win rate, profit factor
        ranked = sorted(dte_results.values(),
                        key=lambda x: (x["expectancy"] * 0.5 + x.get("profit_factor", 0) * 10
                                       + x["win_rate"] * 0.5),
                        reverse=True)
        best_dte = ranked[0]["dte"] if ranked else dte_list[0]

        # Build recommendation
        best = dte_results.get(best_dte, {})
        recommendation = {
            "best_dte":      best_dte,
            "reason":        (
                f"DTE {best_dte} gives the best combination: "
                f"Win Rate {best.get('win_rate',0)}%, "
                f"Expectancy ${best.get('expectancy',0)}, "
                f"Profit Factor {best.get('profit_factor',0)}, "
                f"Avg {best.get('avg_days',0)} days held."
            ),
            "trade_style":   "Short-term scalp" if best_dte <= 14 else
                             "Weekly/Bi-weekly" if best_dte <= 21 else
                             "Monthly swing",
            "signal_count":  len(entry_signals),
            "regime_time_pct": regime_pct,
        }

        return jsonify({
            "ok":           True,
            "symbol":       symbol,
            "strategy":     strategy,
            "start":        start,
            "end":          end,
            "wing_pct":     wing_pct,
            "params": {
                "fast": fast, "slow": slow_p, "signal_len": signal_len,
                "roc_len": roc_len, "adx_thr": adx_thr, "vol_mult": vol_mult,
            },
            "dte_sweep":    dte_results,
            "dte_ranking":  [r["dte"] for r in ranked],
            "best_dte":     best_dte,
            "recommendation": recommendation,
            "entry_signals": entry_signals[-50:],
            "trades_by_dte": {str(k): v for k, v in all_trades_by_dte.items()},
            "total_bars":   n,
            "data_start":   dates[0] if dates else start,
            "data_end":     dates[-1] if dates else end,
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:800]}), 500


# ════════════════════════════════════════════════════════════════════════════
# UAE TrendVol v3 — Watchlist × Period sweep
# Runs the signal across every symbol in a watchlist for 3m, 6m, 12m periods
# ════════════════════════════════════════════════════════════════════════════

@analysis_bp.route("/uae_watchlist_backtest")
def uae_watchlist_backtest():
    """
    Run UAE TrendVol v3 signal against all symbols in a watchlist
    across multiple time periods (3m, 6m, 12m) and multiple DTEs.

    Parameters:
      watchlist_id  - watchlist to scan (required)
      symbols       - comma-separated override (optional)
      strategy      - PS, CS, BOTH (default BOTH)
      dte_list      - comma-separated DTEs (default 7,14,21,28)
      wing_pct      - % OTM short strike (default 3.0)
      profit_tgt    - profit target 0-1 (default 0.50)
      stop_mult     - stop loss multiplier (default 2.0)
      periods       - comma-separated months: 3,6,12 (default 3,6,12)
      max_symbols   - cap symbols for speed (default 20)
    """
    try:
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        from datetime import date as _date, timedelta as _td

        watchlist_id = request.args.get("watchlist_id", type=int)
        raw_syms     = request.args.get("symbols", "").strip()
        strategy     = request.args.get("strategy", "BOTH").upper()
        wing_pct     = float(request.args.get("wing_pct", 3.0))
        profit_tgt   = float(request.args.get("profit_tgt", 0.50))
        stop_mult    = float(request.args.get("stop_mult", 2.0))
        max_syms     = int(request.args.get("max_symbols", 20))
        fast_p       = int(request.args.get("fast", 10))
        slow_p       = int(request.args.get("slow", 20))
        signal_len   = int(request.args.get("signal_len", 5))
        roc_len      = int(request.args.get("roc_len", 3))
        vol_mult     = float(request.args.get("vol_mult", 1.5))

        raw_dte   = request.args.get("dte_list", "7,14,21,28")
        dte_list  = [int(x.strip()) for x in raw_dte.split(",") if x.strip().isdigit()]
        if not dte_list:
            dte_list = [7, 14, 21, 28]

        raw_periods = request.args.get("periods", "3,6,12")
        periods_mo  = [int(x.strip()) for x in raw_periods.split(",") if x.strip().isdigit()]
        if not periods_mo:
            periods_mo = [3, 6, 12]

        # Resolve symbols
        if raw_syms:
            symbols = [s.strip().upper() for s in raw_syms.replace("\n", ",").split(",") if s.strip()]
        else:
            symbols = _load_watchlist_symbols_for_momentum(watchlist_id) or []
        if not symbols:
            return jsonify({"error": "No symbols found. Provide watchlist_id or symbols parameter."}), 400
        symbols = symbols[:max_syms]

        # Get watchlist name
        wl_name = "Custom"
        if watchlist_id:
            try:
                con = sqlite3.connect(DB_PATH)
                row = con.execute("SELECT name FROM watchlists WHERE id=?", (watchlist_id,)).fetchone()
                con.close()
                if row:
                    wl_name = row[0]
            except Exception:
                pass

        today = _date.today()
        period_dates = {}
        for mo in periods_mo:
            days = mo * 30
            period_dates[mo] = (today - _td(days=days)).isoformat()

        signal_filter = "BOTH" if strategy == "BOTH" else                         "BULL_ENTRY" if strategy == "PS" else "BEAR_ENTRY"

        # ── Per-symbol backtest ──────────────────────────────────────────
        def _run_symbol(sym):
            try:
                # Fetch enough history for the longest period + warmup
                max_days = max(periods_mo) * 32 + 100
                fetch_start = (today - _td(days=max_days)).isoformat()
                df = yf.Ticker(sym).history(
                    start=fetch_start, end=today.isoformat(),
                    interval="1d", auto_adjust=False
                )
                if df is None or df.empty or len(df) < 80:
                    return {"symbol": sym, "error": "insufficient data"}
                df = df.dropna(subset=["Close", "High", "Low"])
                closes = [float(x) for x in df["Close"].tolist()]
                highs  = [float(x) for x in df["High"].tolist()]
                lows   = [float(x) for x in df["Low"].tolist()]
                dates_raw = df.index.tolist()
                dates  = [str(d.date()) if hasattr(d, "date") else str(d) for d in dates_raw]
                n = len(closes)

                hv_s = _hv20_series(closes)
                sigs = _uae_trendvol_signals(
                    closes, highs, lows,
                    fast=fast_p, slow=slow_p, signal_len=signal_len,
                    roc_len=roc_len, atr_len=14, adx_len=14,
                    slope_len=5, vol_mult=vol_mult,
                )

                result = {"symbol": sym, "periods": {}, "error": None}

                for mo in periods_mo:
                    period_start = period_dates[mo]
                    # Find the first index in our data >= period_start
                    start_idx = 0
                    for ii, d in enumerate(dates):
                        if d >= period_start:
                            start_idx = ii
                            break

                    if start_idx < 50:
                        start_idx = 50

                    # Slice data to this period
                    p_closes = closes[start_idx:]
                    p_highs  = highs[start_idx:]
                    p_lows   = lows[start_idx:]
                    p_dates  = dates[start_idx:]
                    p_hv     = hv_s[start_idx:]
                    p_sigs   = sigs[start_idx:]

                    # Need at least 20 bars
                    if len(p_closes) < 20:
                        result["periods"][mo] = {"error": "too few bars in period"}
                        continue

                    dte_stats_period = {}
                    for dte in dte_list:
                        trades = _uae_dte_backtest(
                            p_closes, p_highs, p_lows, p_dates, p_hv,
                            dte=dte, strategy=strategy, signals=p_sigs,
                            wing_pct=wing_pct, profit_target=profit_tgt,
                            stop_loss_mult=stop_mult, signal_filter=signal_filter,
                        )
                        dte_stats_period[dte] = _dte_stats(trades, dte)

                    # Best DTE for this symbol+period
                    best_dte_p = max(
                        dte_stats_period.keys(),
                        key=lambda x: (
                            dte_stats_period[x]["expectancy"] * 0.5
                            + dte_stats_period[x].get("profit_factor", 0) * 10
                            + dte_stats_period[x]["win_rate"] * 0.5
                        )
                    ) if dte_stats_period else dte_list[0]

                    result["periods"][mo] = {
                        "period_months":  mo,
                        "period_start":   period_start,
                        "bars":           len(p_closes),
                        "dte_stats":      dte_stats_period,
                        "best_dte":       best_dte_p,
                        "best_stats":     dte_stats_period.get(best_dte_p, {}),
                    }

                return result

            except Exception as e:
                return {"symbol": sym, "error": str(e)[:120]}

        # Run all symbols in parallel (capped workers to avoid rate limits)
        symbol_results = []
        workers = min(6, len(symbols))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_run_symbol, s): s for s in symbols}
            for fut in _as_completed(futs):
                symbol_results.append(fut.result())

        # ── Aggregate across symbols per period × DTE ────────────────────
        # Structure: agg[period_months][dte] = list of stat dicts
        agg = {mo: {dte: [] for dte in dte_list} for mo in periods_mo}

        for sr in symbol_results:
            if sr.get("error"):
                continue
            for mo in periods_mo:
                period_data = sr.get("periods", {}).get(mo, {})
                if period_data.get("error"):
                    continue
                for dte in dte_list:
                    stats = period_data.get("dte_stats", {}).get(dte)
                    if stats and stats.get("total", 0) > 0:
                        agg[mo][dte].append({**stats, "symbol": sr["symbol"]})

        # Aggregate summary per period × DTE
        def _agg_stats(stat_list, mo, dte):
            if not stat_list:
                return {"period_months": mo, "dte": dte, "symbols_with_trades": 0,
                        "total_trades": 0, "avg_win_rate": 0, "avg_expectancy": 0,
                        "avg_profit_factor": 0, "total_pnl": 0, "symbols": []}
            total_trades = sum(s["total"] for s in stat_list)
            total_winners = sum(s["winners"] for s in stat_list)
            total_pnl = sum(s["total_pnl"] for s in stat_list)
            avg_wr  = round(total_winners / total_trades * 100, 1) if total_trades else 0
            avg_exp = round(total_pnl / total_trades, 2) if total_trades else 0
            pf_vals = [s["profit_factor"] for s in stat_list if s.get("profit_factor")]
            avg_pf  = round(sum(pf_vals) / len(pf_vals), 2) if pf_vals else 0
            return {
                "period_months":      mo,
                "dte":                dte,
                "symbols_with_trades":len(stat_list),
                "total_trades":       total_trades,
                "avg_win_rate":       avg_wr,
                "avg_expectancy":     avg_exp,
                "avg_profit_factor":  avg_pf,
                "total_pnl":          round(total_pnl, 2),
                "best_symbol":        max(stat_list, key=lambda s: s["expectancy"])["symbol"],
                "worst_symbol":       min(stat_list, key=lambda s: s["expectancy"])["symbol"],
                "symbols":            [s["symbol"] for s in stat_list],
            }

        period_dte_summary = {}
        for mo in periods_mo:
            period_dte_summary[mo] = {}
            for dte in dte_list:
                period_dte_summary[mo][dte] = _agg_stats(agg[mo][dte], mo, dte)

        # Find best DTE per period (by avg win rate × profit factor)
        best_dte_per_period = {}
        for mo in periods_mo:
            ranked = sorted(
                dte_list,
                key=lambda d: (
                    period_dte_summary[mo][d]["avg_win_rate"] * 0.4
                    + period_dte_summary[mo][d]["avg_profit_factor"] * 20
                    + period_dte_summary[mo][d]["avg_expectancy"] * 0.2
                ),
                reverse=True,
            )
            best_dte_per_period[mo] = ranked[0] if ranked else dte_list[0]

        # Overall best DTE across all periods (majority vote weighted by period length)
        dte_votes = {dte: 0 for dte in dte_list}
        for mo in periods_mo:
            winner = best_dte_per_period[mo]
            dte_votes[winner] = dte_votes.get(winner, 0) + mo  # weight by months
        overall_best_dte = max(dte_votes, key=lambda d: dte_votes[d])

        # ── Human-readable recommendation ────────────────────────────────
        best_stats_3m  = period_dte_summary.get(3, {}).get(overall_best_dte, {})
        best_stats_12m = period_dte_summary.get(12, {}).get(overall_best_dte, {})

        def _period_label(mo):
            return f"{mo} Month{'s' if mo > 1 else ''}"

        recommendations = []
        for mo in sorted(periods_mo):
            bdte = best_dte_per_period[mo]
            bs   = period_dte_summary[mo][bdte]
            recommendations.append({
                "period":    _period_label(mo),
                "best_dte":  bdte,
                "win_rate":  bs["avg_win_rate"],
                "expectancy":bs["avg_expectancy"],
                "pf":        bs["avg_profit_factor"],
                "trades":    bs["total_trades"],
                "symbols":   bs["symbols_with_trades"],
                "summary":   (
                    f"{_period_label(mo)}: Best DTE is {bdte}d — "
                    f"Win Rate {bs['avg_win_rate']}% across "
                    f"{bs['symbols_with_trades']} symbols, "
                    f"Expectancy ${bs['avg_expectancy']}/trade, "
                    f"Profit Factor {bs['avg_profit_factor']}"
                ),
            })

        return jsonify({
            "ok":                  True,
            "watchlist_name":      wl_name,
            "watchlist_id":        watchlist_id,
            "symbols_scanned":     symbols,
            "symbols_with_results":len([s for s in symbol_results if not s.get("error")]),
            "symbols_errored":     [s["symbol"] for s in symbol_results if s.get("error")],
            "periods_months":      periods_mo,
            "dte_list":            dte_list,
            "strategy":            strategy,
            "wing_pct":            wing_pct,
            "overall_best_dte":    overall_best_dte,
            "best_dte_per_period": best_dte_per_period,
            "period_dte_summary":  {str(k): {str(d): v for d, v in vs.items()}
                                    for k, vs in period_dte_summary.items()},
            "symbol_results":      symbol_results,
            "recommendations":     recommendations,
            "params": {
                "fast": fast_p, "slow": slow_p, "signal_len": signal_len,
                "roc_len": roc_len, "vol_mult": vol_mult,
                "profit_tgt": profit_tgt, "stop_mult": stop_mult,
            },
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:1000]}), 500
