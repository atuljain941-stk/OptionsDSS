"""
weekly_analysis.py  —  Comprehensive Weekly Market Analysis

Combines 5 data streams into a single weekly directional assessment:
  1. COT Positioning   — CFTC weekly large spec net vs 52W range
  2. Futures OI Flow   — Schwab daily OI + price trend confirmation
  3. Options Sentiment — PCR trend, OI walls, call/put skew from our DB
  4. Volatility        — VIX level/trend, IV rank, term structure, HV vs IV
  5. Options Skew      — Put/call IV spread (fear premium measure)

Output: per-symbol weekly bias score + trade framework for the week.
"""
import sqlite3, json, datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _TE
from flask import Blueprint, jsonify, request

try:
    from ..scanners.scanner_builder import _watchlist_symbols as _sb_watchlist_symbols, _watchlists as _sb_watchlists
except Exception:
    _sb_watchlist_symbols = None
    _sb_watchlists = None

wa_bp   = Blueprint("wa_bp", __name__, url_prefix="/weekly_analysis")
DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")

SYMBOLS = ["SPY", "QQQ", "IWM"]   # default symbols for weekly analysis


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def _json_safe(obj):
    try:
        import math as _math
        import numpy as _np
    except Exception:
        _np = None
        _math = math
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return [_json_safe(v) for v in obj]
    if obj is None:
        return None
    if isinstance(obj, (int, str, bool)):
        return obj
    try:
        val = float(obj)
        return val if _math.isfinite(val) else None
    except Exception:
        return obj


def _watchlist_name(watchlist_id):
    try:
        if _sb_watchlists is not None:
            rows = _sb_watchlists() or []
            for w in rows:
                try:
                    if int(w.get('id')) == int(watchlist_id):
                        return w.get('name') or f'Watchlist {watchlist_id}'
                except Exception:
                    pass
    except Exception:
        pass
    return 'Default watchlist' if watchlist_id is None else f'Watchlist {watchlist_id}'


def _spot_price(sym: str):
    try:
        import yfinance as yf
        hist = yf.Ticker(sym).history(period='1d')
        if hist is not None and len(hist) > 0 and 'Close' in hist.columns:
            val = float(hist['Close'].iloc[-1])
            return round(val, 2) if val == val else None
    except Exception:
        pass
    return None


def _price_trend(sym: str) -> dict:
    """Short-term price action score for 1-6 week planning."""
    try:
        import yfinance as yf

        hist = yf.Ticker(sym).history(period='6mo')
        if hist is None or len(hist) < 30 or 'Close' not in hist.columns:
            return {'found': False, 'score': 5, 'bias': 'Neutral', 'detail': 'Not enough price history'}

        closes = hist['Close'].astype(float).dropna()
        if len(closes) < 30:
            return {'found': False, 'score': 5, 'bias': 'Neutral', 'detail': 'Not enough price history'}

        ema20 = closes.ewm(span=20, adjust=False).mean()
        ema50 = closes.ewm(span=50, adjust=False).mean()
        last = float(closes.iloc[-1])
        e20 = float(ema20.iloc[-1])
        e50 = float(ema50.iloc[-1])
        ret5 = round((last / float(closes.iloc[-6]) - 1) * 100.0, 2) if len(closes) >= 6 else 0.0
        ret20 = round((last / float(closes.iloc[-21]) - 1) * 100.0, 2) if len(closes) >= 21 else 0.0

        score = 5
        bias = 'Neutral'
        detail = f'Price {last:.2f} · EMA20 {e20:.2f} · EMA50 {e50:.2f} · 5d {ret5:+.2f}%'

        if last > e20 > e50 and ret5 > 0 and ret20 > 0:
            score = 9; bias = 'Bullish'
            detail += ' · Trend intact'
        elif last > e20 and e20 > e50:
            score = 8; bias = 'Bullish'
        elif last < e20 < e50 and ret5 < 0 and ret20 < 0:
            score = 1; bias = 'Bearish'
            detail += ' · Trend weak'
        elif last < e20 and e20 < e50:
            score = 2; bias = 'Bearish'
        elif abs(ret5) < 1.5:
            score = 5; bias = 'Neutral'
        elif ret5 > 2:
            score = 7; bias = 'Bullish'
        elif ret5 < -2:
            score = 3; bias = 'Bearish'

        return {
            'found': True,
            'symbol': sym,
            'score': score,
            'bias': bias,
            'last': round(last, 2),
            'ema20': round(e20, 2),
            'ema50': round(e50, 2),
            'ret5': ret5,
            'ret20': ret20,
            'detail': detail,
        }
    except Exception as e:
        return {'found': False, 'score': 5, 'bias': 'Neutral', 'error': str(e)[:80], 'detail': 'Price trend unavailable'}


# ────────────────────────────────────────────────────────────────────────────
# 1. COT Positioning
# ────────────────────────────────────────────────────────────────────────────
def _get_cot(contract: str) -> dict:
    try:
        from .cftc_cot import get_cot_summary
        s = get_cot_summary(contract, weeks=52)
        if not s.get("found"):
            return {"found": False}
        idx   = s.get("cot_index", 50) or 50
        chg   = s.get("lspec_chg", 0) or 0
        trend = s.get("trend_dir", "Flat")
        net   = s.get("lspec_net", 0) or 0
        extreme = s.get("extreme", "")
        # Signal: contrarian at extremes, trend-follow in middle
        if idx >= 90:    signal, score = "⚠ Crowded Long",        4
        elif idx >= 70:  signal, score = "✅ Institutionally Long", 8
        elif idx >= 55:  signal, score = "↗ Mild Long Bias",       6
        elif idx >= 45:  signal, score = "⚖ Neutral",              5
        elif idx >= 30:  signal, score = "↘ Mild Short Bias",      4
        elif idx >= 10:  signal, score = "🔴 Institutionally Short",2
        else:            signal, score = "🔥 Extreme Short (Squeeze Risk)", 7
        return {
            "found": True, "contract": contract,
            "cot_index": round(idx, 1), "score": score, "signal": signal,
            "net": net, "wow_change": chg, "trend_dir": trend,
            "extreme": extreme, "report_date": s.get("report_date"),
            "detail": f"COT {idx:.0f}/100 · {signal} · Net {net:+,.0f} · WoW {chg:+,.0f}",
        }
    except Exception as e:
        return {"found": False, "error": str(e)[:60]}


# ────────────────────────────────────────────────────────────────────────────
# 2. Futures OI Flow
# ────────────────────────────────────────────────────────────────────────────
def _get_futures_oi(contract_root: str) -> dict:
    try:
        con = _conn()
        rows = con.execute(
            "SELECT trade_date, oi, settle FROM futures_oi_daily "
            "WHERE contract LIKE ? ORDER BY trade_date DESC LIMIT 20",
            (f"%{contract_root}%",)
        ).fetchall()
        con.close()
        if len(rows) < 5:
            return {"found": False, "detail": "Not enough Schwab data"}
        oi_5d  = [r["oi"]     for r in rows[:5]  if r["oi"]]
        oi_pr  = [r["oi"]     for r in rows[5:10] if r["oi"]]
        px_5d  = [r["settle"] for r in rows[:5]  if r["settle"]]
        px_pr  = [r["settle"] for r in rows[5:10] if r["settle"]]
        oi_now  = sum(oi_5d)  / len(oi_5d)  if oi_5d  else 0
        oi_then = sum(oi_pr)  / len(oi_pr)  if oi_pr  else oi_now
        px_now  = sum(px_5d)  / len(px_5d)  if px_5d  else 0
        px_then = sum(px_pr)  / len(px_pr)  if px_pr  else px_now
        oi_chg  = round((oi_now - oi_then) / max(1, oi_then) * 100, 1)
        px_chg  = round((px_now - px_then) / max(0.01, px_then) * 100, 2) if px_then else 0
        if oi_chg > 3  and px_chg > 0:  signal, score = "New longs entering ↑",    8
        elif oi_chg > 3  and px_chg < 0: signal, score = "New shorts entering ↓",   2
        elif oi_chg < -3 and px_chg > 0: signal, score = "Short covering rally ↗",  6
        elif oi_chg < -3 and px_chg < 0: signal, score = "Longs exiting ↘",         3
        else:                             signal, score = "OI stable — range bound",  5
        return {
            "found": True, "oi_chg_pct": oi_chg, "price_chg_pct": px_chg,
            "score": score, "signal": signal,
            "latest_oi": int(oi_now), "latest_price": round(px_now, 2),
            "days_data": len(rows),
            "detail": f"OI {oi_chg:+.1f}% · Price {px_chg:+.2f}% · {signal}",
        }
    except Exception as e:
        return {"found": False, "error": str(e)[:60]}


# ────────────────────────────────────────────────────────────────────────────
# 3. Options Sentiment (from our DB)
# ────────────────────────────────────────────────────────────────────────────
def _get_options_sentiment(sym: str) -> dict:
    try:
        con = _conn()
        today = datetime.date.today().isoformat()
        cutoff = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()

        # PCR over last 10 days (trend)
        pcr_rows = con.execute("""
            SELECT date,
                   SUM(CASE WHEN type='call' THEN oi ELSE 0 END) call_oi,
                   SUM(CASE WHEN type='put'  THEN oi ELSE 0 END) put_oi,
                   SUM(CASE WHEN type='call' THEN volume ELSE 0 END) call_vol,
                   SUM(CASE WHEN type='put'  THEN volume ELSE 0 END) put_vol
            FROM options
            WHERE symbol=? AND date>=? AND expiration>=?
            GROUP BY date ORDER BY date DESC LIMIT 10
        """, (sym, cutoff, today)).fetchall()

        # OI walls — top strikes by total OI
        wall_rows = con.execute("""
            SELECT strike, type, SUM(oi) total_oi
            FROM options
            WHERE symbol=? AND date=(SELECT MAX(date) FROM options WHERE symbol=?)
              AND expiration>=?
            GROUP BY strike, type
            HAVING total_oi > 0
            ORDER BY total_oi DESC LIMIT 30
        """, (sym, sym, today)).fetchall()
        con.close()

        if not pcr_rows:
            return {"found": False, "detail": "No options data in DB"}

        latest = pcr_rows[0]
        call_oi = latest["call_oi"] or 0
        put_oi  = latest["put_oi"]  or 0
        pcr     = round(put_oi / max(1, call_oi), 3)
        call_pct= round(call_oi / max(1, call_oi + put_oi) * 100, 1)

        # PCR trend over 5 days
        pcrs = []
        for r in pcr_rows[:5]:
            c, p = r["call_oi"] or 0, r["put_oi"] or 0
            if c > 0: pcrs.append(round(p / c, 3))
        pcr_trend = "Rising" if len(pcrs) >= 2 and pcrs[0] > pcrs[-1] else \
                    "Falling" if len(pcrs) >= 2 and pcrs[0] < pcrs[-1] else "Flat"

        # OI walls
        call_walls = sorted([r for r in wall_rows if r["type"]=="call"], key=lambda x: -x["total_oi"])[:4]
        put_walls  = sorted([r for r in wall_rows if r["type"]=="put"],  key=lambda x: -x["total_oi"])[:4]

        # Score: PCR < 0.7 bullish, > 1.2 bearish
        if pcr < 0.6:   score = 8
        elif pcr < 0.8: score = 7
        elif pcr < 1.0: score = 6
        elif pcr < 1.2: score = 4
        else:           score = 2
        if pcr_trend == "Falling": score = min(10, score + 1)  # falling PCR = bullish
        if pcr_trend == "Rising":  score = max(0, score - 1)

        # Volume-based signal today
        call_vol = latest["call_vol"] or 0
        put_vol  = latest["put_vol"] or 0
        vol_pcr  = round(put_vol / max(1, call_vol), 3) if call_vol else None

        return {
            "found": True, "symbol": sym,
            "pcr": pcr, "call_pct": call_pct,
            "pcr_trend": pcr_trend, "score": score,
            "pcr_5d": pcrs,
            "vol_pcr": vol_pcr,
            "call_walls": [{"strike": r["strike"], "oi": r["total_oi"]} for r in call_walls],
            "put_walls":  [{"strike": r["strike"], "oi": r["total_oi"]} for r in put_walls],
            "call_oi": call_oi, "put_oi": put_oi,
            "detail": f"PCR {pcr} ({pcr_trend}) · Calls {call_pct}% · Vol PCR {vol_pcr or '—'}",
        }
    except Exception as e:
        return {"found": False, "error": str(e)[:60]}


# ────────────────────────────────────────────────────────────────────────────
# 4 + 5. Volatility + Skew (live yfinance)
# ────────────────────────────────────────────────────────────────────────────
def _get_vol_and_skew(sym: str) -> dict:
    """Compute IV rank, term structure, skew, HV vs IV from yfinance."""
    try:
        import yfinance as yf, pandas as pd, numpy as np

        tk = yf.Ticker(sym)

        # ── Price history for HV ──────────────────────────────────────────
        hist = tk.history(period="1y")
        if hist is None or len(hist) < 30:
            return {"found": False, "detail": "Not enough price history"}
        closes = hist["Close"].values.astype(float)

        # HV-20 and HV-5
        def hv(n):
            if len(closes) < n + 1: return None
            rets = np.log(closes[-n:] / closes[-n-1:-1])
            return round(float(np.std(rets) * np.sqrt(252) * 100), 1)
        hv20 = hv(20); hv5 = hv(5)

        # ── VIX ──────────────────────────────────────────────────────────
        vix_data = None
        if sym in ("SPY", "QQQ", "IWM", "DIA"):
            try:
                vix = yf.Ticker("^VIX").history(period="60d")
                if vix is not None and len(vix) > 5:
                    vix_now  = round(float(vix["Close"].iloc[-1]), 2)
                    vix_5d   = round(float(vix["Close"].iloc[-6]), 2) if len(vix) >= 6 else vix_now
                    vix_20d  = round(float(vix["Close"].iloc[-21]), 2) if len(vix) >= 21 else vix_now
                    vix_52wh = round(float(vix["Close"].max()), 2)
                    vix_52wl = round(float(vix["Close"].min()), 2)
                    vix_rank = round((vix_now - vix_52wl) / max(0.01, vix_52wh - vix_52wl) * 100, 1)
                    vix_data = {
                        "current": vix_now, "5d_ago": vix_5d, "20d_ago": vix_20d,
                        "chg_5d":  round(vix_now - vix_5d, 2),
                        "chg_20d": round(vix_now - vix_20d, 2),
                        "52w_high": vix_52wh, "52w_low": vix_52wl,
                        "rank_52w": vix_rank,
                        "regime": ("Fear" if vix_now >= 25 else
                                   "Elevated" if vix_now >= 18 else
                                   "Normal" if vix_now >= 13 else "Complacent"),
                    }
            except Exception:
                pass

        # ── IV Rank + Term Structure + Skew ──────────────────────────────
        iv_rank = None; term_structure = None; skew = None
        atm_iv  = None; front_iv = None; back_iv = None

        try:
            exps = list(tk.options or [])[:4]   # front 4 expirations
        except Exception:
            exps = []

        if len(exps) >= 2:
            spot = float(closes[-1])
            exp_ivs = {}  # expiry -> ATM IV

            for exp in exps[:3]:
                try:
                    oc = tk.option_chain(exp)
                    calls = oc.calls; puts = oc.puts
                    if calls is None or len(calls) == 0: continue
                    # ATM = strike closest to spot
                    calls["dist"] = abs(calls["strike"] - spot)
                    puts["dist"]  = abs(puts["strike"]  - spot)
                    atm_call = calls.nsmallest(1, "dist")
                    atm_put  = puts.nsmallest(1, "dist")
                    atm_call_iv = float(atm_call["impliedVolatility"].iloc[0]) * 100 if len(atm_call) else None
                    atm_put_iv  = float(atm_put["impliedVolatility"].iloc[0])  * 100 if len(atm_put)  else None
                    if atm_call_iv and atm_put_iv:
                        exp_ivs[exp] = round((atm_call_iv + atm_put_iv) / 2, 1)
                except Exception:
                    continue

            exp_list = sorted(exp_ivs.keys())
            if exp_list:
                front_iv = exp_ivs.get(exp_list[0])
                back_iv  = exp_ivs.get(exp_list[-1]) if len(exp_list) >= 2 else None
                atm_iv   = front_iv

                if front_iv and back_iv:
                    term_structure = ("Contango" if back_iv > front_iv else "Backwardation")
                    ts_spread = round(back_iv - front_iv, 1)
                else:
                    ts_spread = None; term_structure = None

                # IV Rank from 52W proxy (using HV as proxy since we don't have IV history)
                # Real IV rank needs IV history — use current vs HV20 as a proxy
                if atm_iv and hv20:
                    hv_ratio = round(atm_iv / hv20, 2)
                    iv_rank  = min(100, round(hv_ratio * 50, 1))  # >1 means IV > HV (elevated)

            # ── Skew (5% OTM put IV vs 5% OTM call IV) ──────────────────
            if len(exps) >= 1:
                try:
                    oc = tk.option_chain(exps[0])
                    calls = oc.calls; puts = oc.puts
                    if calls is not None and puts is not None and len(calls) > 5:
                        calls["dist"] = abs(calls["strike"] - spot)
                        puts["dist"]  = abs(puts["strike"]  - spot)
                        # OTM: calls above spot, puts below spot
                        otm_calls = calls[calls["strike"] > spot * 1.03].nsmallest(3, "dist")
                        otm_puts  = puts[puts["strike"]   < spot * 0.97].nsmallest(3, "dist")
                        if len(otm_calls) > 0 and len(otm_puts) > 0:
                            c_iv = float(otm_calls["impliedVolatility"].mean()) * 100
                            p_iv = float(otm_puts["impliedVolatility"].mean())  * 100
                            skew = {
                                "put_iv":    round(p_iv, 1),
                                "call_iv":   round(c_iv, 1),
                                "spread":    round(p_iv - c_iv, 1),
                                "ratio":     round(p_iv / max(0.01, c_iv), 2),
                                "sentiment": ("High Fear" if p_iv - c_iv > 6
                                              else "Elevated Fear" if p_iv - c_iv > 3
                                              else "Neutral" if p_iv - c_iv > -1
                                              else "Bullish Skew"),
                            }
                except Exception:
                    pass

        # Volatility score
        vol_score = 5  # neutral default
        if vix_data:
            v = vix_data["current"]
            vr = vix_data.get("rank_52w", 50)
            if v <= 14:   vol_score = 8   # low vol = complacent = risk-on
            elif v <= 18: vol_score = 7
            elif v <= 22: vol_score = 5
            elif v <= 28: vol_score = 3   # elevated = risk-off
            else:         vol_score = 2   # fear = extreme bearish
            if vix_data["chg_5d"] < -2:  vol_score = min(10, vol_score + 1)  # VIX falling = bullish
            if vix_data["chg_5d"] > 2:   vol_score = max(0,  vol_score - 1)

        return {
            "found":          True,
            "symbol":         sym,
            "score":          vol_score,
            "hv20":           hv20,
            "hv5":            hv5,
            "atm_iv":         atm_iv,
            "front_iv":       front_iv,
            "back_iv":        back_iv,
            "iv_vs_hv":       round(atm_iv / hv20, 2) if atm_iv and hv20 else None,
            "term_structure": term_structure,
            "ts_spread":      ts_spread if 'ts_spread' in dir() else None,
            "iv_rank":        iv_rank,
            "vix":            vix_data,
            "skew":           skew,
            "detail": (f"ATM IV {atm_iv:.1f}% | HV20 {hv20:.1f}% | "
                      f"VIX {vix_data['current'] if vix_data else '—'} | "
                      f"Skew {skew['spread']:+.1f}%" if skew and vix_data and hv20 and atm_iv
                      else f"HV20 {hv20}% | VIX {vix_data['current'] if vix_data else '—'}"),
        }
    except Exception as e:
        return {"found": False, "error": str(e)[:100]}


# ────────────────────────────────────────────────────────────────────────────
# Master weekly analysis aggregator
# ────────────────────────────────────────────────────────────────────────────
EQUITY_TO_FUTURES = {
    "SPY": "/ES", "QQQ": "/NQ", "IWM": "/RTY",
    "DIA": "/ES", "GLD": "/GC", "USO": "/CL",
}


def run_weekly_analysis(symbol: str) -> dict:
    sym      = symbol.upper()
    contract = EQUITY_TO_FUTURES.get(sym, "/ES")
    started  = datetime.datetime.now()
    spot     = _spot_price(sym)

    # Run all sections in parallel
    tasks = {
        "cot":      lambda: _get_cot(contract),
        "futures":  lambda: _get_futures_oi(contract[1:]),  # strip /
        "options":  lambda: _get_options_sentiment(sym),
        "vol_skew": lambda: _get_vol_and_skew(sym),
        "price":    lambda: _price_trend(sym),
    }
    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fn): k for k, fn in tasks.items()}
        for fut in as_completed(futs):
            k = futs[fut]
            try:    results[k] = fut.result()
            except: results[k] = {"found": False, "error": "task failed"}

    # ── Composite weekly score (0-10) ────────────────────────────────────
    # COT + Futures are shared market context, so keep them in the summary header.
    # The row score is driven by symbol-specific factors: price action, options flow, and volatility.
    weights  = {"options": 0.45, "vol_skew": 0.35, "price": 0.20}
    total_w  = 0.0; total_s = 0.0
    for key, w in weights.items():
        r = results.get(key, {})
        if r.get("found") or r.get("score") is not None:
            s = r.get("score", 5)
            total_s += s * w
            total_w += w

    weekly_score = round(total_s / total_w, 1) if total_w > 0 else 5.0
    bias = ("🔥 Strong Bull"  if weekly_score >= 7.5
            else "✅ Bullish"  if weekly_score >= 6.0
            else "⚖ Neutral"  if weekly_score >= 4.5
            else "🔴 Bearish"  if weekly_score >= 3.0
            else "💀 Strong Bear")

    # ── Strategy recommendations ─────────────────────────────────────────
    opt   = results.get("options", {})
    vs    = results.get("vol_skew", {})
    vix   = (vs.get("vix") or {}).get("current", 18)
    skew  = vs.get("skew") or {}
    pcr   = opt.get("pcr", 1.0) or 1.0
    atm_iv= vs.get("atm_iv")
    hv20  = vs.get("hv20")
    iv_hv = vs.get("iv_vs_hv", 1.0) or 1.0

    strategies = []
    call_walls = opt.get("call_walls", [])
    put_walls  = opt.get("put_walls",  [])

    if weekly_score >= 6.5:
        strategies.append("✅ Bull call spread or short put spread")
        if vix >= 18: strategies.append("✅ Sell OTM puts (elevated IV = good premium)")
        if call_walls: strategies.append(f"📐 Target near call wall ${call_walls[0]['strike']:.0f}")
    elif weekly_score <= 3.5:
        strategies.append("🔴 Bear put spread or short call spread")
        if vix >= 18: strategies.append("✅ Sell OTM calls (elevated IV = good premium)")
        if put_walls: strategies.append(f"📐 Target near put wall ${put_walls[0]['strike']:.0f}")
    else:
        strategies.append("⚖ Iron condor or short strangle (neutral bias)")
        if iv_hv > 1.3: strategies.append("✅ Elevated IV — favour selling premium")
        if iv_hv < 0.8: strategies.append("⚠ Low IV — buying options is cheap, consider spreads")

    if vs.get("term_structure") == "Backwardation":
        strategies.append("⚡ Vol backwardation — near-term fear elevated, calendar spreads")
    if skew.get("spread", 0) > 6:
        strategies.append("🛡 High put skew — market paying up for downside protection")

    elapsed = round((datetime.datetime.now() - started).total_seconds(), 1)
    return {
        "symbol":       sym,
        "spot":         spot,
        "contract":     contract,
        "weekly_score": weekly_score,
        "bias":         bias,
        "sections":     results,
        "strategies":   strategies,
        "expiry_profile": _get_expiry_flow_profile(sym, max_expiries=4),
        "elapsed_s":    elapsed,
        "generated_at": started.strftime("%Y-%m-%d %H:%M"),
    }




def _weekly_watchlist_analysis(watchlist_id=None, benchmark='SPY', limit=None):
    if _sb_watchlist_symbols is None:
        raise RuntimeError('watchlist helper unavailable')
    symbols = _sb_watchlist_symbols(watchlist_id) or []
    if limit:
        try:
            limit = int(limit)
            if limit > 0:
                symbols = symbols[:limit]
        except Exception:
            pass
    if not symbols:
        return {"error": "No symbols found for the selected watchlist", "watchlist_id": watchlist_id}

    started = datetime.datetime.now()
    rows = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as ex:
        futs = {ex.submit(run_weekly_analysis, s): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                row = fut.result()
            except Exception as e:
                row = {"symbol": sym, "error": str(e)}
            rows.append(row)

    def _bias_bucket(bias: str) -> str:
        b = (bias or '').lower()
        if 'bull' in b:
            return 'bull'
        if 'bear' in b:
            return 'bear'
        return 'neutral'

    rows.sort(key=lambda r: (r.get('weekly_score') or 0, r.get('generated_at') or ''), reverse=True)
    summary = {
        'symbol_count': len(rows),
        'bull_count': sum(1 for r in rows if _bias_bucket(r.get('bias')) == 'bull'),
        'bear_count': sum(1 for r in rows if _bias_bucket(r.get('bias')) == 'bear'),
        'neutral_count': sum(1 for r in rows if _bias_bucket(r.get('bias')) == 'neutral'),
        'avg_score': round(sum(float(r.get('weekly_score') or 0) for r in rows) / max(1, len(rows)), 1),
        'avg_pcr': None,
        'avg_iv_rank': None,
        'avg_iv_change_pct': None,
        'avg_iv_vs_hv': None,
        'avg_vix_chg_5d': None,
        'avg_price_score': None,
        'cot_score': None,
        'cot_signal': None,
        'cot_detail': None,
        'futures_score': None,
        'futures_signal': None,
        'futures_detail': None,
    }
    pcr_vals = []; ivr_vals = []; ivchg_vals = []; iv_vs_hv_vals = []; vixchg_vals = []; price_vals = []
    for r in rows:
        sec = r.get('sections') or {}
        opt = sec.get('options') or {}
        vs  = sec.get('vol_skew') or {}
        pcr = opt.get('pcr')
        ivr = vs.get('iv_rank')
        ivhv = vs.get('iv_vs_hv')
        atm = vs.get('atm_iv')
        hv  = vs.get('hv20')
        vix = (vs.get('vix') or {}).get('chg_5d')

        price = r.get('sections', {}).get('price') if isinstance(r.get('sections'), dict) else {}
        if isinstance(price, dict) and price.get('score') is not None:
            price_vals.append(float(price.get('score')))
        if pcr is not None:
            pcr_vals.append(float(pcr))
        if ivr is not None:
            ivr_vals.append(float(ivr))
        if ivhv is not None:
            iv_vs_hv_vals.append(float(ivhv))
        if atm is not None and hv not in (None, 0):
            try:
                ivchg_vals.append(round((float(atm) - float(hv)) / max(0.01, float(hv)) * 100.0, 1))
            except Exception:
                pass
        if vix is not None:
            vixchg_vals.append(float(vix))
    if pcr_vals: summary['avg_pcr'] = round(sum(pcr_vals)/len(pcr_vals), 2)
    if ivr_vals: summary['avg_iv_rank'] = round(sum(ivr_vals)/len(ivr_vals), 1)
    if ivchg_vals: summary['avg_iv_change_pct'] = round(sum(ivchg_vals)/len(ivchg_vals), 1)
    if iv_vs_hv_vals: summary['avg_iv_vs_hv'] = round(sum(iv_vs_hv_vals)/len(iv_vs_hv_vals), 2)
    if vixchg_vals: summary['avg_vix_chg_5d'] = round(sum(vixchg_vals)/len(vixchg_vals), 2)
    if price_vals: summary['avg_price_score'] = round(sum(price_vals)/len(price_vals), 1)

    # Shared market context (COT + Futures are market-wide; surface once in header)
    if rows:
        first_sec = rows[0].get('sections') or {}
        first_cot = first_sec.get('cot') or {}
        first_fut = first_sec.get('futures') or {}
        summary['cot_score'] = first_cot.get('score')
        summary['cot_signal'] = first_cot.get('signal')
        summary['cot_detail'] = first_cot.get('detail')
        summary['futures_score'] = first_fut.get('score')
        summary['futures_signal'] = first_fut.get('signal')
        summary['futures_detail'] = first_fut.get('detail')

    # normalize rows for UI
    ui_rows = []
    for r in rows:
        sec = r.get('sections') or {}
        opt = sec.get('options') or {}
        vs  = sec.get('vol_skew') or {}
        vix = (vs.get('vix') or {})
        ui_rows.append({
            'symbol': r.get('symbol'),
            'spot': r.get('spot'),
            'bias': r.get('bias'),
            'weekly_score': r.get('weekly_score'),
            'price_score': (sec.get('price') or {}).get('score'),
            'cot_score': (sec.get('cot') or {}).get('score'),
            'futures_score': (sec.get('futures') or {}).get('score'),
            'options_score': opt.get('score'),
            'vol_score': vs.get('score'),
            'pcr': opt.get('pcr'),
            'pcr_trend': opt.get('pcr_trend'),
            'iv_rank': vs.get('iv_rank'),
            'iv_vs_hv': vs.get('iv_vs_hv'),
            'iv_change_pct': (round((float(vs.get('atm_iv')) - float(vs.get('hv20'))) / max(0.01, float(vs.get('hv20'))) * 100.0, 1) if vs.get('atm_iv') is not None and vs.get('hv20') not in (None, 0) else None),
            'atm_iv': vs.get('atm_iv'),
            'hv20': vs.get('hv20'),
            'vix': vix.get('current'),
            'vix_chg_5d': vix.get('chg_5d'),
            'detail': r.get('strategies', []),
            'expiry_profile': r.get('expiry_profile', []),
            'generated_at': r.get('generated_at'),
            'elapsed_s': r.get('elapsed_s'),
            'error': r.get('error'),
        })

    return {
        'ok': True,
        'mode': 'watchlist',
        'watchlist_id': watchlist_id,
        'watchlist_name': _watchlist_name(watchlist_id),
        'benchmark': benchmark,
        'summary': summary,
        'rows': ui_rows,
        'symbols': symbols,
        'generated_at': started.strftime('%Y-%m-%d %H:%M'),
        'elapsed_s': round((datetime.datetime.now() - started).total_seconds(), 1),
    }

def _safe_num(v, default=None):
    try:
        import math as _math
        f = float(v)
        return f if _math.isfinite(f) else default
    except Exception:
        return default


def _get_expiry_flow_profile(sym: str, max_expiries: int = 4) -> list:
    """Return compact per-expiry OI / PCR / wall snapshots for the watchlist table."""
    try:
        con = _conn()
        today = datetime.date.today().isoformat()
        rows = con.execute(
            "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration ASC",
            (sym, today),
        ).fetchall()
        expiries = [r[0] if not isinstance(r, sqlite3.Row) else r["expiration"] for r in rows][:max_expiries]
        if not expiries:
            con.close()
            return []

        profile = []
        for exp in expiries:
            try:
                latest_date_row = con.execute(
                    "SELECT MAX(date) AS d FROM options WHERE symbol=? AND expiration=?",
                    (sym, exp),
                ).fetchone()
                latest_date = latest_date_row["d"] if latest_date_row and isinstance(latest_date_row, sqlite3.Row) else (latest_date_row[0] if latest_date_row else None)
                if not latest_date:
                    continue
                prev_date_row = con.execute(
                    "SELECT DISTINCT date FROM options WHERE symbol=? AND expiration=? AND date < ? ORDER BY date DESC LIMIT 1",
                    (sym, exp, latest_date),
                ).fetchone()
                prev_date = prev_date_row[0] if prev_date_row else None

                latest_rows = con.execute(
                    "SELECT strike, type, COALESCE(oi,0) oi, COALESCE(volume,0) volume FROM options WHERE symbol=? AND expiration=? AND date=?",
                    (sym, exp, latest_date),
                ).fetchall()
                prev_rows = []
                if prev_date:
                    prev_rows = con.execute(
                        "SELECT strike, type, COALESCE(oi,0) oi, COALESCE(volume,0) volume FROM options WHERE symbol=? AND expiration=? AND date=?",
                        (sym, exp, prev_date),
                    ).fetchall()

                call_rows = [r for r in latest_rows if str(r["type"]).lower() == "call"]
                put_rows  = [r for r in latest_rows if str(r["type"]).lower() == "put"]
                prev_call_rows = [r for r in prev_rows if str(r["type"]).lower() == "call"]
                prev_put_rows  = [r for r in prev_rows if str(r["type"]).lower() == "put"]

                call_oi = sum(float(r["oi"] or 0) for r in call_rows)
                put_oi  = sum(float(r["oi"] or 0) for r in put_rows)
                prev_call_oi = sum(float(r["oi"] or 0) for r in prev_call_rows)
                prev_put_oi  = sum(float(r["oi"] or 0) for r in prev_put_rows)
                total_oi = call_oi + put_oi
                prev_total_oi = prev_call_oi + prev_put_oi
                oi_change_pct = round((total_oi - prev_total_oi) / max(1.0, prev_total_oi) * 100.0, 1) if prev_total_oi > 0 else None
                pcr = round(put_oi / max(1.0, call_oi), 2) if call_oi > 0 else None

                call_wall = max(call_rows, key=lambda r: float(r["oi"] or 0), default=None)
                put_wall  = max(put_rows,  key=lambda r: float(r["oi"] or 0), default=None)
                call_wall_strike = _safe_num(call_wall["strike"] if call_wall else None)
                put_wall_strike  = _safe_num(put_wall["strike"] if put_wall else None)

                dte = None
                try:
                    dte = (datetime.date.fromisoformat(exp) - datetime.date.today()).days
                except Exception:
                    pass

                if (pcr is not None and pcr <= 0.85 and (oi_change_pct is None or oi_change_pct >= 0)):
                    bias = "Bullish"
                    trade_hint = "Bull call spread"
                elif (pcr is not None and pcr >= 1.15 and (oi_change_pct is None or oi_change_pct >= 0)):
                    bias = "Bearish"
                    trade_hint = "Bear put spread"
                elif oi_change_pct is not None and oi_change_pct < 0 and (pcr is None or pcr < 1.0):
                    bias = "Bullish"
                    trade_hint = "Short put / support hold"
                elif oi_change_pct is not None and oi_change_pct < 0 and (pcr is not None and pcr > 1.0):
                    bias = "Bearish"
                    trade_hint = "Short call / resistance hold"
                else:
                    bias = "Neutral"
                    trade_hint = "Iron condor / premium sale"

                profile.append({
                    "expiry": exp,
                    "dte": dte,
                    "bias": bias,
                    "pcr": pcr,
                    "oi_change_pct": oi_change_pct,
                    "call_oi": round(call_oi, 0),
                    "put_oi": round(put_oi, 0),
                    "call_wall": call_wall_strike,
                    "put_wall": put_wall_strike,
                    "trade_hint": trade_hint,
                    "call_wall_oi": _safe_num(call_wall["oi"] if call_wall else None),
                    "put_wall_oi": _safe_num(put_wall["oi"] if put_wall else None),
                })
            except Exception:
                continue
        con.close()
        return profile
    except Exception:
        return []


# ── Flask routes ─────────────────────────────────────────────────────────────
@wa_bp.route("/analyze/<symbol>")
def analyze(symbol):
    return jsonify(_json_safe(run_weekly_analysis(symbol.upper())))



@wa_bp.route("/watchlist")
def analyze_watchlist():
    watchlist_id = request.args.get("watchlist_id", type=int)
    benchmark = (request.args.get("benchmark") or "SPY").strip().upper() or "SPY"
    limit = request.args.get("limit", type=int)
    payload = _weekly_watchlist_analysis(watchlist_id=watchlist_id, benchmark=benchmark, limit=limit)
    return jsonify(_json_safe(payload))

@wa_bp.route("/multi")
def analyze_multi():
    """Analyze SPY, QQQ, IWM in parallel."""
    syms = request.args.get("symbols", "SPY,QQQ,IWM").upper().split(",")
    results = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(run_weekly_analysis, s): s for s in syms[:4]}
        for fut in as_completed(futs, timeout=120):
            s = futs[fut]
            try:    results[s] = fut.result()
            except: results[s] = {"error": "failed", "symbol": s}
    return jsonify(_json_safe({"results": results, "symbols": list(results.keys())}))
