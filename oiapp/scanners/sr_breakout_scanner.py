# oiapp/scanners/sr_breakout_scanner.py
"""
Support/Resistance Breakout Scanner
Based on: LonesomeTheBlue TradingView "Support Resistance Channels" indicator
+ momentum filter: |%change| > EMA(Abs(%change), 60) * 2

Logic:
  1. Compute pivot highs/lows (period=10 by default)
  2. Build S/R channels: group pivots within ChannelWidth% of each other
  3. Score channels by: pivot count + touches within lookback
  4. Momentum breach filter: daily or weekly close breaks a channel
     AND abs(pct_change) > EMA(abs(pct_change), 60) * 2
     AND change > 0 for resistance break, < 0 for support break
  5. Daily AND weekly timeframes computed separately, both returned
"""
import math
from datetime import date, datetime
from flask import Blueprint, jsonify, request

from .scoring_service import attach_scanner_scores

sr_bp = Blueprint("sr_bp", __name__, url_prefix="/scanner/sr")

def _get_wl_symbols(wl_id):
    """Return symbol list from watchlist, or None to use default symbols table."""
    if not wl_id:
        return None
    try:
        import sqlite3 as _sq
        from pathlib import Path as _P
        from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
        _db = _OIAPP_DB_PATH
        _c  = _sq.connect(_db)
        rows = _c.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (int(wl_id),)
        ).fetchall()
        _c.close()
        return [r[0] for r in rows] if rows else None
    except:
        return None



# ── Core SR math ───────────────────────────────────────────────────────────
def _compute_sr(closes, highs, lows, prd=10, channel_w_pct=5, loopback=290, max_sr=6):
    """
    Port of the TradingView Support Resistance Channels logic to Python.
    Returns list of (hi, lo, strength) sorted by strength desc.
    """
    n = len(closes)
    if n < prd*3: return []

    # Pivot highs/lows
    def is_pivot_high(i):
        if i < prd or i+prd >= n: return False
        return all(highs[i] >= highs[i-j] for j in range(1,prd+1)) and \
               all(highs[i] >= highs[i+j] for j in range(1,prd+1))
    def is_pivot_low(i):
        if i < prd or i+prd >= n: return False
        return all(lows[i] <= lows[i-j] for j in range(1,prd+1)) and \
               all(lows[i] <= lows[i+j] for j in range(1,prd+1))

    # Collect pivots within loopback
    pivot_vals = []
    start_idx = max(prd, n - loopback - prd)
    for i in range(start_idx, n-prd):
        if is_pivot_high(i): pivot_vals.append(highs[i])
        elif is_pivot_low(i): pivot_vals.append(lows[i])
    if not pivot_vals: return []

    # Channel width = ChannelW% of 300-bar range
    recent = closes[max(0,n-300):]
    cwidth = (max(recent)-min(recent)) * channel_w_pct / 100

    def get_sr_channel(pivot):
        lo = hi = pivot; count = 0
        for pv in pivot_vals:
            wdth = (hi-pv) if pv<=hi else (pv-lo)
            if wdth <= cwidth:
                if pv <= hi: lo = min(lo,pv)
                else:        hi = max(hi,pv)
                count += 20
        return hi, lo, count

    # Add candle-touch bonus (like the TV script)
    channels = []
    used = set()
    for i, pivot in enumerate(pivot_vals):
        if i in used: continue
        hi, lo, strength = get_sr_channel(pivot)
        # Count bar touches in loopback
        touches = sum(1 for j in range(max(0,n-loopback),n)
                     if (highs[j]<=hi and highs[j]>=lo) or (lows[j]<=hi and lows[j]>=lo))
        strength += touches
        # Mark all pivots inside this channel as used
        for k, pv in enumerate(pivot_vals):
            if lo <= pv <= hi: used.add(k)
        channels.append((hi, lo, strength, pivot))

    # Sort by strength desc, deduplicate, take top max_sr
    channels.sort(key=lambda x:-x[2])
    final = []
    for ch in channels:
        # No overlap with already selected channels
        overlap = any(abs(ch[0]-f[0])<cwidth*0.5 or abs(ch[1]-f[1])<cwidth*0.5
                      for f in final)
        if not overlap:
            final.append(ch)
        if len(final) >= max_sr: break
    return [(hi, lo, strength) for hi, lo, strength, _ in final]


def _momentum_filter(closes, period=60, multiplier=2.0):
    """
    Compute EMA(Abs(daily %change), period).
    Returns: (ema_values, threshold_values) aligned with closes.
    Breach: abs(pct_change[i]) > threshold[i]*multiplier
    """
    if len(closes) < period+2: return [], []
    pct = [0.0] + [abs((closes[i]-closes[i-1])/closes[i-1]*100)
                   for i in range(1,len(closes))]
    k = 2/(period+1)
    ema = list(pct)
    for i in range(1,len(ema)): ema[i] = pct[i]*k + ema[i-1]*(1-k)
    return pct, ema


def analyze_sr_breakouts(symbol, timeframe="1d"):
    """
    Full SR breakout analysis for a symbol on daily or weekly timeframe.
    Returns dict with SR levels, recent breaches, momentum status.
    """
    try:
        from ..services.market import get_history_cached
        period = "2y" if timeframe in ("1wk","1w","weekly") else "1y"
        interval = "1wk" if timeframe in ("1wk","1w","weekly") else "1d"
        df = get_history_cached(symbol, period=period, interval=interval)
        if df is None or df.empty or len(df) < 60: return {"error":"insufficient data"}

        closes = df["Close"].tolist()
        highs  = df["High"].tolist()
        lows   = df["Low"].tolist()
        n = len(closes)

        # Compute SR channels
        channels = _compute_sr(closes, highs, lows)
        if not channels: return {"symbol":symbol,"channels":[],"breakouts":[]}

        # Momentum filter
        pct_abs, ema_abs = _momentum_filter(closes)

        # Current price
        spot = closes[-1]
        spot_prev = closes[-2] if n>=2 else spot

        # Classify each channel vs current price
        sr_levels = []
        for hi, lo, strength in channels:
            mid = (hi+lo)/2
            if hi < spot:   lvl_type = "support"
            elif lo > spot: lvl_type = "resistance"
            else:           lvl_type = "in_channel"
            sr_levels.append({
                "hi": round(hi,2), "lo": round(lo,2),
                "mid": round(mid,2),
                "strength": strength,
                "type": lvl_type,
                "pct_from_spot": round((mid-spot)/spot*100, 2)
            })

        # Check recent breakouts (last 5 bars)
        breakouts = []
        threshold = ema_abs[-1] * 2 if ema_abs else 0

        for lookback in range(1, 6):
            idx = n - lookback
            if idx < 1: break
            c_now  = closes[idx]
            c_prev = closes[idx-1]
            pct_chg = (c_now-c_prev)/c_prev*100 if c_prev else 0
            abs_chg = abs(pct_chg)
            mom_ema = ema_abs[idx] if idx < len(ema_abs) else 0
            mom_thr = mom_ema * 2
            is_momentum = abs_chg > mom_thr and mom_thr > 0

            for hi, lo, strength in channels:
                mid = (hi+lo)/2
                # Resistance breach: prev close below/in channel, now close above
                if c_prev <= hi and c_now > hi and pct_chg > 0:
                    breakouts.append({
                        "bar_offset": lookback,
                        "type": "resistance_break",
                        "level": round(hi,2),
                        "channel_lo": round(lo,2),
                        "pct_change": round(pct_chg,2),
                        "momentum_threshold": round(mom_thr,3),
                        "is_momentum_confirmed": is_momentum,
                        "strength": strength,
                        "close": round(c_now,2),
                    })
                # Support breach: prev close above/in channel, now close below
                elif c_prev >= lo and c_now < lo and pct_chg < 0:
                    breakouts.append({
                        "bar_offset": lookback,
                        "type": "support_break",
                        "level": round(lo,2),
                        "channel_hi": round(hi,2),
                        "pct_change": round(pct_chg,2),
                        "momentum_threshold": round(mom_thr,3),
                        "is_momentum_confirmed": is_momentum,
                        "strength": strength,
                        "close": round(c_now,2),
                    })

        # Current momentum state
        curr_mom = abs((closes[-1]-closes[-2])/closes[-2]*100) if n>=2 else 0
        curr_thr = ema_abs[-1]*2 if ema_abs else 0

        result = {
            "symbol": symbol,
            "timeframe": timeframe,
            "spot": round(spot,2),
            "channels": sr_levels,
            "breakouts": breakouts,
            "current_momentum_pct": round(curr_mom,3),
            "momentum_threshold": round(curr_thr,3),
            "momentum_active": curr_mom > curr_thr,
        }
        return attach_scanner_scores(result, symbol, frame=df, setup_type="S/R Breakout", direction=result.get("direction", "NEUTRAL"), native_score=result.get("score"))
    except Exception as e:
        return {"symbol":symbol,"error":str(e),"channels":[],"breakouts":[]}


def scan_sr_breakouts(symbols=None, min_strength=40, require_momentum=True):
    """
    Run SR breakout scan across all watchlist symbols.
    Returns symbols with recent confirmed breakouts on daily or weekly.
    """
    import concurrent.futures
    if not symbols:
        try:
            import sqlite3
            from pathlib import Path
            from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
            DB_PATH = _OIAPP_DB_PATH
            con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
            symbols = [r["symbol"] for r in con.execute("SELECT symbol FROM symbols").fetchall()]
            con.close()
        except: symbols = ["SPY","QQQ","AAPL","MSFT","NVDA"]

    results = []

    def _one(sym):
        # Analyze both timeframes
        daily  = analyze_sr_breakouts(sym, "1d")
        weekly = analyze_sr_breakouts(sym, "1wk")
        all_breakouts = []
        for tf, res in [("1d", daily), ("1wk", weekly)]:
            for bo in res.get("breakouts", []):
                if require_momentum and not bo.get("is_momentum_confirmed"): continue
                if bo.get("strength",0) < min_strength: continue
                if bo.get("bar_offset",99) > 3: continue  # only last 3 bars
                all_breakouts.append({**bo, "timeframe":tf,
                    "spot": res.get("spot",0),
                    "channels": res.get("channels",[]),
                })
        if not all_breakouts: return None
        # Score: momentum confirmed + weekly >> daily + strength + recency
        best = sorted(all_breakouts, key=lambda x: (
            x.get("is_momentum_confirmed",0)*3,
            (x.get("timeframe")=="1wk")*2,
            x.get("strength",0)/100,
            -x.get("bar_offset",5)
        ), reverse=True)[0]
        return {
            "symbol": sym,
            "spot": best["spot"],
            "breakout_type": best["type"],
            "timeframe": best["timeframe"],
            "level": best["level"],
            "pct_change": best["pct_change"],
            "momentum_threshold": best["momentum_threshold"],
            "momentum_confirmed": best["is_momentum_confirmed"],
            "strength": best["strength"],
            "bar_offset": best["bar_offset"],
            "all_breakouts": all_breakouts,
            "channels": best["channels"][:4],
        }
        return attach_scanner_scores(result, sym, frame=hist, setup_type="S/R Breakout", direction=result.get("direction", "NEUTRAL"), native_score=result.get("score"), trend_age=result.get("days_since"))

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=12)
    try:
        from ..services.bounded_wait import bounded_as_completed
        futs = {ex.submit(_one, sym): sym for sym in symbols[:60]}
        for fut, sym in bounded_as_completed(futs, timeout=60,
                on_timeout=lambda ks: print(f"[sr_breakout_scanner] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                continue
            r = fut.result()
            if r: results.append(r)
    finally:
        ex.shutdown(wait=False)

    # Sort: resistance breaks first (bullish) then support breaks (bearish)
    results.sort(key=lambda x: (
        x.get("momentum_confirmed",0)*10,
        abs(x.get("pct_change",0)),
        x.get("strength",0)
    ), reverse=True)
    return results



# ── S/R Proximity Scanner ────────────────────────────────────────────────────

def scan_sr_proximity(symbols, proximity_pct=3.0, rsi_filter="all", sector_filter="", **kwargs):
    """
    Find symbols where spot price is within proximity_pct% of a S/R channel.
    Uses _compute_sr which returns (hi, lo, strength) tuples.
    rsi_filter: 'all' | 'overbought' (>=65) | 'oversold' (<=35) | 'neutral'
    """
    import math, concurrent.futures
    import yfinance as yf

    if sector_filter:
        try:
            from ..services.sector_service import get_symbol_sector
            symbols = [s for s in symbols if get_symbol_sector(s) == sector_filter]
        except: pass

    def _rsi14(closes):
        if len(closes) < 15: return 50.0
        deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d,0) for d in deltas]
        losses = [abs(min(d,0)) for d in deltas]
        ag = sum(gains[:14])/14; al = sum(losses[:14])/14
        for i in range(14, len(deltas)):
            ag = (ag*13 + gains[i])  / 14
            al = (al*13 + losses[i]) / 14
        rs = ag/al if al > 0 else 100
        return round(100 - 100/(1+rs), 1)

    def _ema_series(vals, period):
        if not vals: return []
        k = 2/(period+1); e = vals[0]; out = [e]
        for v in vals[1:]: e = v*k + e*(1-k); out.append(e)
        return out

    # ── TF fetch helper ──────────────────────────────────────────────────
    _TF_CFG = {
        "daily":   ("1d",  "6mo",  None),
        "weekly":  ("1wk", "5y",   None),
        "monthly": ("1mo", "10y",  None),
        "4h":      ("1h",  "60d",  "4h"),  # resample 1H→4H
    }

    def _fetch_sr_tf(ticker, tf_label):
        cfg = _TF_CFG.get(tf_label.lower(), _TF_CFG["daily"])
        interval, period, resample = cfg
        try:
            from ..services.market import get_history_cached
            df = get_history_cached(getattr(ticker, "ticker", None) or str(ticker), period=period, interval=interval)
            if df is None or df.empty: return None
            if resample:
                df = df.resample(resample).agg({
                    "Open":"first","High":"max","Low":"min",
                    "Close":"last","Volume":"sum"
                }).dropna()
            return df if len(df) > 30 else None
        except: return None

    def _one(sym, tf_label="daily"):
        try:
            tk   = yf.Ticker(sym)
            hist = _fetch_sr_tf(tk, tf_label)
            if hist is None or hist.empty or len(hist) < 30: return None

            closes = hist["Close"].tolist()
            highs  = hist["High"].tolist()
            lows   = hist["Low"].tolist()
            vols   = hist["Volume"].tolist()
            spot   = round(closes[-1], 2)

            # ── S/R channels via _compute_sr → list of (hi, lo, strength) ──
            channels = _compute_sr(closes, highs, lows)
            if not channels: return None

            # ── Find channels near current price ──────────────────────────
            nearby = []
            for hi, lo, strength in channels:
                mid = (hi + lo) / 2
                # Distance from spot to nearest channel boundary
                if spot < lo:               # price below channel → resistance above
                    dist_pct = round((lo - spot) / spot * 100, 2)
                    sr_type  = "resistance"
                    level    = round(lo, 2)
                elif spot > hi:             # price above channel → support below
                    dist_pct = round((spot - hi) / spot * 100, 2)
                    sr_type  = "support"
                    level    = round(hi, 2)
                else:                       # price inside channel
                    dist_pct = 0.0
                    sr_type  = "in_channel"
                    level    = round(mid, 2)

                if dist_pct <= proximity_pct:
                    nearby.append({
                        "price":    level,
                        "hi":       round(hi, 2),
                        "lo":       round(lo, 2),
                        "mid":      round(mid, 2),
                        "sr_type":  sr_type,
                        "dist_pct": dist_pct,
                        "strength": round(strength, 1),
                    })

            if not nearby: return None

            # Pick the closest level
            best = min(nearby, key=lambda x: x["dist_pct"])

            # ── RSI-14 ────────────────────────────────────────────────────
            rsi = _rsi14(closes)

            # ── Delta RSI = RSI - EMA90(RSI) ─────────────────────────────
            rsi_series = [_rsi14(closes[max(0,i-30):i+1]) for i in range(29, len(closes))]
            ema90      = _ema_series(rsi_series, 90)
            delta_rsi  = round(rsi_series[-1] - ema90[-1], 1) if ema90 else 0.0

            # ── RSI filter ────────────────────────────────────────────────
            if rsi_filter == "overbought" and rsi  < 65: return None
            if rsi_filter == "oversold"   and rsi  > 35: return None
            if rsi_filter == "neutral"    and (rsi >= 65 or rsi <= 35): return None

            # ── Volume ────────────────────────────────────────────────────
            avg_vol   = sum(vols[-20:])/max(1, len(vols[-20:]))
            vol_ratio = round(vols[-1] / max(1, avg_vol) * 100, 0)

            # ── Signal & scoring ─────────────────────────────────────────
            sr_type  = best["sr_type"]
            strength = best["strength"]
            dist     = best["dist_pct"]

            # Directional signal
            if sr_type == "support":
                signal = "BULLISH" if rsi <= 70 else "NEUTRAL"
            elif sr_type == "resistance":
                signal = "BEARISH" if rsi >= 30 else "NEUTRAL"
            else:
                signal = "NEUTRAL"

            # RSI alignment bonus
            rsi_aligned = (signal=="BULLISH" and rsi <= 40) or (signal=="BEARISH" and rsi >= 60)

            score = 40
            if dist < 1.0:        score += 15
            elif dist < 2.0:      score += 10
            if strength >= 70:    score += 15
            elif strength >= 50:  score += 8
            if rsi_aligned:       score += 12
            if vol_ratio > 120:   score +=  5
            if delta_rsi > 20 and signal == "BULLISH": score += 5
            if delta_rsi < -20 and signal == "BEARISH": score += 5
            score = min(100, score)

            pop = round(min(80, 40 + (strength - 40)*0.4 + (10 if rsi_aligned else 0) + (5 if dist < 1.5 else 0)), 0)

            # ── Sector ───────────────────────────────────────────────────
            sector = ""
            try:
                from ..services.sector_service import get_symbol_sector
                sector = get_symbol_sector(sym) or ""
            except: pass

            result = {
                "symbol":    sym,
                "spot":      spot,
                "sr_level":  best["price"],
                "sr_type":   best["sr_type"],
                "dist_pct":  best["dist_pct"],
                "strength":  strength,
                "rsi":       rsi,
                "delta_rsi": delta_rsi,
                "vol_ratio": int(vol_ratio),
                "signal":    signal,
                "score":     score,
                "pop":       int(pop),
                "sector":    sector,
                "all_nearby": nearby[:3],
                "channels":   nearby,
            }
            return attach_scanner_scores(result, sym, frame=hist, setup_type="S/R Breakout", direction=result.get("direction", "NEUTRAL"), native_score=result.get("score"), trend_age=result.get("days_since"))
        except: return None

    timeframes = kwargs.get("timeframes", ["daily"])
    if not timeframes: timeframes = ["daily"]

    seen = {}  # sym → merged result
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=20)
    try:
        futs = {ex.submit(_one, sym, tf): (sym, tf)
                for sym in symbols[:80]
                for tf in timeframes}
        from ..services.bounded_wait import bounded_as_completed
        for fut, key in bounded_as_completed(futs, timeout=60,
                on_timeout=lambda ks: print(f"[sr_breakout_scanner] {len(ks)} symbol/timeframe pair(s) timed out: {ks[:20]}")):
            if fut is None:
                continue
            sym, tf = key
            r = fut.result()
            if not r: continue
            # Tag channels with their timeframe
            for ch in r.get("channels", []):
                ch["tf"] = tf
            if sym in seen:
                # Merge: extend channels, track all TFs seen
                seen[sym]["channels"].extend(r.get("channels", []))
                tfs = seen[sym].get("timeframes", [])
                if tf not in tfs: tfs.append(tf)
                seen[sym]["timeframes"] = tfs
                # Keep highest score
                if r["score"] > seen[sym]["score"]:
                    seen[sym].update({k: r[k] for k in
                        ["score","signal","rsi","delta_rsi","dist_pct","sr_level","sr_type","strength"]})
            else:
                r["timeframes"] = [tf]
                seen[sym] = r
    finally:
        ex.shutdown(wait=False)

    results = sorted(seen.values(), key=lambda x: (-x["score"], x["dist_pct"]))
    return results


# ── Breakout Age Scanner ─────────────────────────────────────────────────────
def _fetch_sr_history(symbol, timeframe="1d"):
    """Lightweight history fetch for the breakout-age scanner."""
    import yfinance as yf
    if timeframe in ("1wk", "1w", "weekly"):
        period, interval = "2y", "1wk"
    else:
        period, interval = "1y", "1d"
    try:
        from ..services.market import get_history_cached
        df = get_history_cached(symbol, period=period, interval=interval)
        if df is None or df.empty or len(df) < 60:
            return None
        return df
    except Exception:
        return None


def _classify_breakout_age(sym, df, days_back=10, min_strength=40, require_momentum=True, timeframe="1d"):
    """Return the most recent breakout within days_back calendar days."""
    closes = df["Close"].tolist()
    highs  = df["High"].tolist()
    lows   = df["Low"].tolist()
    n = len(closes)
    if n < 60:
        return None

    channels = _compute_sr(closes, highs, lows)
    if not channels:
        return None

    pct_abs, ema_abs = _momentum_filter(closes)
    if not ema_abs:
        return None

    latest_dt = df.index[-1].date()
    candidates = []

    for idx in range(1, n):
        try:
            bar_dt = df.index[idx].date()
        except Exception:
            continue
        days_since = (latest_dt - bar_dt).days
        if days_since < 0 or days_since > days_back:
            continue

        c_now  = closes[idx]
        c_prev = closes[idx - 1]
        pct_chg = ((c_now - c_prev) / c_prev * 100) if c_prev else 0
        mom_ema = ema_abs[idx] if idx < len(ema_abs) else 0
        mom_thr = mom_ema * 2
        is_momentum = abs(pct_chg) > mom_thr and mom_thr > 0

        if require_momentum and not is_momentum:
            continue

        for hi, lo, strength in channels:
            strength = float(strength)
            if strength < min_strength:
                continue

            # Resistance break: close moves above the channel high
            if c_prev <= hi and c_now > hi and pct_chg > 0:
                breakout_close = c_now
                current_close = closes[-1]
                hi_since = max(highs[idx:])
                lo_since = min(lows[idx:])
                favorable = (current_close - breakout_close) / breakout_close * 100
                max_fav = (hi_since - breakout_close) / breakout_close * 100
                max_adv = (lo_since - breakout_close) / breakout_close * 100
                if current_close < hi * 0.995:
                    status = "Failed"
                elif abs(current_close - hi) / hi <= 0.01:
                    status = "Retesting"
                elif days_since <= 2:
                    status = "Fresh"
                else:
                    status = "Running"
                candidates.append({
                    "symbol": sym,
                    "timeframe": timeframe,
                    "breakout_date": bar_dt.isoformat(),
                    "days_since": days_since,
                    "breakout_type": "resistance_break",
                    "direction": "BULLISH",
                    "level": round(hi, 2),
                    "channel_hi": round(hi, 2),
                    "channel_lo": round(lo, 2),
                    "breakout_close": round(breakout_close, 2),
                    "current_close": round(current_close, 2),
                    "directional_return_pct": round(favorable, 2),
                    "max_favorable_pct": round(max_fav, 2),
                    "max_adverse_pct": round(max_adv, 2),
                    "status": status,
                    "strength": round(strength, 1),
                    "momentum_confirmed": bool(is_momentum),
                    "momentum_pct": round(abs(pct_chg), 2),
                    "momentum_threshold": round(mom_thr, 2),
                    "price_relative_to_level_pct": round((current_close - hi) / hi * 100, 2),
                    "notes": [
                        f"Broke above ${hi:.2f} on {bar_dt.isoformat()}",
                        f"{days_since} day(s) since breakout",
                        f"Current move {favorable:+.2f}%",
                    ],
                })

            # Support break: close moves below the channel low
            elif c_prev >= lo and c_now < lo and pct_chg < 0:
                breakout_close = c_now
                current_close = closes[-1]
                hi_since = max(highs[idx:])
                lo_since = min(lows[idx:])
                favorable = (breakout_close - current_close) / breakout_close * 100
                max_fav = (breakout_close - lo_since) / breakout_close * 100
                max_adv = (breakout_close - hi_since) / breakout_close * 100
                if current_close > lo * 1.005:
                    status = "Failed"
                elif abs(current_close - lo) / lo <= 0.01:
                    status = "Retesting"
                elif days_since <= 2:
                    status = "Fresh"
                else:
                    status = "Running"
                candidates.append({
                    "symbol": sym,
                    "timeframe": timeframe,
                    "breakout_date": bar_dt.isoformat(),
                    "days_since": days_since,
                    "breakout_type": "support_break",
                    "direction": "BEARISH",
                    "level": round(lo, 2),
                    "channel_hi": round(hi, 2),
                    "channel_lo": round(lo, 2),
                    "breakout_close": round(breakout_close, 2),
                    "current_close": round(current_close, 2),
                    "directional_return_pct": round(favorable, 2),
                    "max_favorable_pct": round(max_fav, 2),
                    "max_adverse_pct": round(max_adv, 2),
                    "status": status,
                    "strength": round(strength, 1),
                    "momentum_confirmed": bool(is_momentum),
                    "momentum_pct": round(abs(pct_chg), 2),
                    "momentum_threshold": round(mom_thr, 2),
                    "price_relative_to_level_pct": round((current_close - lo) / lo * 100, 2),
                    "notes": [
                        f"Broke below ${lo:.2f} on {bar_dt.isoformat()}",
                        f"{days_since} day(s) since breakout",
                        f"Current move {favorable:+.2f}%",
                    ],
                })

    if not candidates:
        return None

    # Prefer most recent breakout, then strongest channel, then best current move.
    best = sorted(candidates, key=lambda x: (x["days_since"], -x["strength"], -x["directional_return_pct"]))[0]
    if best["breakout_type"] == "resistance_break":
        score = 55 + min(20, best["strength"] * 0.15) + max(0, best["directional_return_pct"]) * 2
        if best["status"] == "Running": score += 6
        if best["status"] == "Retesting": score += 3
        if best["status"] == "Failed": score -= 12
    else:
        score = 55 + min(20, best["strength"] * 0.15) + max(0, best["directional_return_pct"]) * 2
        if best["status"] == "Running": score += 6
        if best["status"] == "Retesting": score += 3
        if best["status"] == "Failed": score -= 12
    best["score"] = int(max(0, min(100, round(score))))
    best["notes_text"] = " · ".join(best.get("notes") or [])
    return best


def scan_sr_breakout_age(symbols=None, days_back=10, min_strength=40, require_momentum=True, sector_filter=""):
    """Scan for breakouts that happened within days_back calendar days."""
    import concurrent.futures
    if not symbols:
        try:
            import sqlite3
            from pathlib import Path
            from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
            DB_PATH = _OIAPP_DB_PATH
            con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
            symbols = [r[0] for r in con.execute("SELECT symbol FROM symbols WHERE symbol IS NOT NULL").fetchall()]
            con.close()
        except Exception:
            symbols = ["SPY","QQQ","AAPL","MSFT","NVDA"]

    if sector_filter:
        try:
            from ..services.sector_service import get_symbol_sector
            symbols = [s for s in symbols if get_symbol_sector(s) == sector_filter]
        except Exception:
            pass

    results = []

    def _one(sym):
        try:
            df = _fetch_sr_history(sym, "1d")
            if df is None or df.empty or len(df) < 60:
                return None
            return _classify_breakout_age(sym, df, days_back=days_back, min_strength=min_strength, require_momentum=require_momentum, timeframe="1d")
        except Exception:
            return None

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=20)
    try:
        futs = {ex.submit(_one, sym): sym for sym in symbols[:120]}
        from ..services.bounded_wait import bounded_as_completed
        for fut, sym in bounded_as_completed(futs, timeout=60,
                on_timeout=lambda ks: print(f"[sr_breakout_scanner] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                continue
            r = fut.result()
            if r:
                results.append(r)
    finally:
        ex.shutdown(wait=False)

    results = sorted(results, key=lambda x: (-x.get("score",0), x.get("days_since", 999), -x.get("strength",0)))
    return results



@sr_bp.route("/proximity")
def sr_proximity():
    """S/R proximity scanner — symbols near support/resistance."""
    from ..db import _connect as _dc
    import sqlite3

    prox_pct   = request.args.get("proximity", 3.0, type=float)
    rsi_filter = request.args.get("rsi_filter", "all")
    sector     = (request.args.get("sector") or "").strip()
    min_earn_days = request.args.get("min_earn_days", None)
    if min_earn_days in (None, "", "None", "null"):
        min_earn_days = None
    else:
        try:
            min_earn_days = int(min_earn_days)
        except Exception:
            min_earn_days = None
    # Accept comma-separated timeframes e.g. "daily,weekly,4h"
    tfs_raw    = request.args.get("timeframes", "daily")
    timeframes = [t.strip().lower() for t in tfs_raw.split(",") if t.strip()]
    if not timeframes: timeframes = ["daily"]

    # Full watchlist — prefer symbols table, fallback to options, then hardcoded
    WATCHLIST = ["AA","AAPL","ADBE","AMD","AMZN","APA","AR","AVGO","AXP","BA","BABA",
        "BAC","BMY","BP","BSX","BX","C","CCJ","CMG","COF","COIN","CRM","CSCO","CSX",
        "CTRA","CVNA","CVS","CVX","DAL","DASH","DDOG","DIS","DOW","DVN","EPD","EQT",
        "FCX","FSLR","GE","GILD","GM","GOOG","GOOGL","GSK","HAL","HOOD","IBM","INTC",
        "JPM","KMI","KO","LRCX","LUV","LVS","MDLZ","META","MMM","MO","MRK","MRNA",
        "MRVL","MS","MSFT","MSTR","MU","NEE","NEM","NFLX","NKE","NOW","NVDA","OKTA",
        "ORCL","OXY","PANW","PDD","PEP","PG","PLTR","PYPL","RBLX","RCL","RTX","SBUX",
        "SCHW","SHOP","SLB","TEVA","TGT","TSLA","TSM","UAL","UBER","UNH","UPS","V",
        "VST","VZ","WDC","WFC","WMT","WYNN","XOM","Z"]
    try:
        wl_id_p  = request.args.get("watchlist_id", None, type=int)
        wl_syms_p = _get_wl_symbols(wl_id_p)
        con = _dc()
        if wl_syms_p:
            syms = wl_syms_p
            con.close()
        else:
            syms = [r[0] for r in con.execute("SELECT symbol FROM symbols WHERE symbol IS NOT NULL").fetchall()]
            if not syms:
                syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM options").fetchall()]
            con.close()
        if not syms: syms = WATCHLIST
    except:
        syms = WATCHLIST

    results = scan_sr_proximity(syms, proximity_pct=prox_pct, rsi_filter=rsi_filter, sector_filter=sector, timeframes=timeframes)
    if min_earn_days is not None:
        from .scoring_service import filter_by_min_earnings
        results = filter_by_min_earnings(results, min_earn_days)
    return jsonify({"results": results, "count": len(results),
                    "params": {"proximity": prox_pct, "rsi_filter": rsi_filter, "min_earn_days": min_earn_days}})

# ── Routes ─────────────────────────────────────────────────────────────────
@sr_bp.route("/scan")
def sr_scan():
    """Scan all symbols for S/R breakouts with momentum confirmation."""
    min_strength = request.args.get("min_strength", 40, type=int)
    require_mom  = request.args.get("require_momentum","true").lower() != "false"
    sector       = request.args.get("sector","").strip()
    wl_id        = request.args.get("watchlist_id", None, type=int)
    min_earn_days = request.args.get("min_earn_days", None)
    if min_earn_days in (None, "", "None", "null"):
        min_earn_days = None
    else:
        try:
            min_earn_days = int(min_earn_days)
        except Exception:
            min_earn_days = None
    try:
        # Priority: watchlist > sector > default symbols table
        syms = _get_wl_symbols(wl_id)  # None if no watchlist selected
        if syms is None and sector:
            from ..services.sector_service import get_symbol_sector
            import sqlite3
            from pathlib import Path
            from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
            DB_PATH = _OIAPP_DB_PATH
            con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
            all_syms = [r["symbol"] for r in con.execute("SELECT symbol FROM symbols").fetchall()]
            con.close()
            syms = [s for s in all_syms if get_symbol_sector(s)==sector]
        results = scan_sr_breakouts(syms, min_strength, require_mom)
        if min_earn_days is not None:
            from .scoring_service import filter_by_min_earnings
            results = filter_by_min_earnings(results, min_earn_days)
        # Save to cache
        import json as _j2, datetime as _d2, sqlite3 as _s2
        from pathlib import Path as _P2
        _ts2 = _d2.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
            _db2 = _OIAPP_DB_PATH
            _c2 = _s2.connect(_db2)
            _c2.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
            _c2.execute("INSERT OR REPLACE INTO app_cache VALUES ('sr_breakout_scan',?,?)", (_j2.dumps(results), _ts2))
            _c2.commit(); _c2.close()
        except: pass
        return jsonify({"results": results, "count": len(results), "completed_at": _ts2})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:400]}), 500

@sr_bp.route("/analyze/<symbol>")
def sr_analyze(symbol):
    """Detailed SR analysis for a single symbol on both timeframes."""
    daily  = analyze_sr_breakouts(symbol.upper(), "1d")
    weekly = analyze_sr_breakouts(symbol.upper(), "1wk")
    return jsonify({"daily": daily, "weekly": weekly})

# ══════════════════════════════════════════════════════════════════════════════
# FAILED BREAKOUT SCANNER
# ══════════════════════════════════════════════════════════════════════════════

def scan_failed_breakouts(symbols=None, lookback_days=10, min_strength=30):
    """
    Scan for two setups:
    - FAILED BREAKOUT (bearish): price broke ABOVE resistance within `lookback_days`
      but has since fallen BACK BELOW that resistance level.
    - FAILED BREAKDOWN (bullish): price broke BELOW support within `lookback_days`
      but has since recovered ABOVE that support level.

    Returns list of dicts with setup details.
    """
    import yfinance as yf
    import sqlite3
    from pathlib import Path
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if symbols is None:
        from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
        db = _OIAPP_DB_PATH
        con = sqlite3.connect(db)
        symbols = [r[0] for r in con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()]
        con.close()

    def _check(sym):
        try:
            from ..services.market import get_history_cached
            hist = get_history_cached(sym, period="6mo", interval="1d")
            if hist is None or len(hist) < 60:
                return None
            closes = hist["Close"].values.astype(float)
            highs  = hist["High"].values.astype(float)
            lows   = hist["Low"].values.astype(float)

            sr_levels = _compute_sr(closes, highs, lows,
                                    prd=10, channel_w_pct=4, loopback=180, max_sr=8)
            if not sr_levels:
                return None

            spot        = closes[-1]
            recent_hi   = max(highs[-lookback_days:])
            recent_lo   = min(lows[-lookback_days:])
            prev_close  = closes[-lookback_days-1] if len(closes) > lookback_days+1 else closes[0]

            results = []
            for (res_hi, res_lo, strength) in sr_levels:
                if strength < min_strength:
                    continue
                res_mid = (res_hi + res_lo) / 2

                # FAILED BREAKOUT (bearish):
                # Price reached ABOVE resistance during the lookback window
                # AND now trading BELOW resistance mid
                if recent_hi > res_hi and spot < res_mid:
                    pullback_pct = round((recent_hi - spot) / recent_hi * 100, 2)
                    above_pct    = round((recent_hi - res_mid) / res_mid * 100, 2)
                    if pullback_pct >= 0.5:   # at least 0.5% reversal
                        native = min(100, int(40 + strength * 0.5 + max(0, pullback_pct * 5)))
                        results.append(attach_scanner_scores({
                        "symbol":    sym,
                        "setup":     "Failed Breakout",
                        "setup_type": "Failed Breakout",
                        "direction": "BEARISH",
                        "icon":      "�",
                        "res_level": round(res_mid, 2),
                        "res_hi":    round(res_hi, 2),
                        "res_lo":    round(res_lo, 2),
                        "spot":      round(spot, 2),
                        "high_reached": round(recent_hi, 2),
                        "above_pct":  above_pct,
                        "pullback_pct": pullback_pct,
                        "strength":  strength,
                        "native_score": native,
                        "detail":    f"Broke above ${res_mid:.2f} resistance (+{above_pct:.1f}%) "
                                     f"but pulled back {pullback_pct:.1f}% — now below resistance",
                    }, sym, frame=hist, setup_type="Failed Breakout", direction="BEARISH", native_score=native, trend_age=lookback_days))

                # FAILED BREAKDOWN (bullish):
                # Price reached BELOW support during the lookback window
                # AND now trading ABOVE support mid
                if recent_lo < res_lo and spot > res_mid:
                    recovery_pct = round((spot - recent_lo) / recent_lo * 100, 2)
                    below_pct    = round((res_mid - recent_lo) / res_mid * 100, 2)
                    if recovery_pct >= 0.5:
                        native = min(100, int(40 + strength * 0.5 + max(0, recovery_pct * 5)))
                        results.append(attach_scanner_scores({
                        "symbol":    sym,
                        "setup":     "Failed Breakdown",
                        "setup_type": "Failed Breakdown",
                        "direction": "BULLISH",
                        "icon":      "�",
                        "res_level": round(res_mid, 2),
                        "res_hi":    round(res_hi, 2),
                        "res_lo":    round(res_lo, 2),
                        "spot":      round(spot, 2),
                        "low_reached": round(recent_lo, 2),
                        "below_pct":  below_pct,
                        "recovery_pct": recovery_pct,
                        "strength":  strength,
                        "native_score": native,
                        "detail":    f"Broke below ${res_mid:.2f} support (−{below_pct:.1f}%) "
                                     f"but recovered {recovery_pct:.1f}% — now above support",
                    }, sym, frame=hist, setup_type="Failed Breakdown", direction="BULLISH", native_score=native, trend_age=lookback_days))

            # Return the highest-strength result per symbol (avoid duplicate same symbol)
            if results:
                return max(results, key=lambda x: x["strength"])
            return None
        except Exception:
            return None

    out = []
    ex = ThreadPoolExecutor(max_workers=8)
    try:
        futs = {ex.submit(_check, s): s for s in symbols}
        from ..services.bounded_wait import bounded_as_completed
        for fut, s in bounded_as_completed(futs, timeout=60,
                on_timeout=lambda ks: print(f"[sr_breakout_scanner] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                continue
            r = fut.result()
            if r:
                out.append(r)
    finally:
        ex.shutdown(wait=False)

    out.sort(key=lambda x: (-x["strength"], x["symbol"]))
    return out


@sr_bp.route("/failed_breakout")
def api_failed_breakout():
    lookback = request.args.get("lookback_days", 10, type=int)
    min_str  = request.args.get("min_strength", 30, type=int)
    wl_id    = request.args.get("watchlist_id", None, type=int)
    syms     = _get_wl_symbols(wl_id)
    try:
        results = scan_failed_breakouts(syms, lookback_days=lookback, min_strength=min_str)
        return jsonify({"results": results, "count": len(results),
                        "lookback_days": lookback, "min_strength": min_str})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:400]}), 500


# ══════════════════════════════════════════════════════════════════════════════
# HIGHER LOW / LOWER HIGH SCANNER
# ══════════════════════════════════════════════════════════════════════════════

def _find_swing_pivots(highs, lows, period=5):
    """
    Find swing highs and swing lows within the data.
    Returns (swing_highs, swing_lows) as lists of (index, price).
    """
    n = len(highs)
    sw_hi, sw_lo = [], []
    for i in range(period, n - period):
        if all(highs[i] >= highs[i-j] for j in range(1, period+1)) and \
           all(highs[i] >= highs[i+j] for j in range(1, period+1)):
            sw_hi.append((i, highs[i]))
        if all(lows[i] <= lows[i-j] for j in range(1, period+1)) and \
           all(lows[i] <= lows[i+j] for j in range(1, period+1)):
            sw_lo.append((i, lows[i]))
    return sw_hi, sw_lo


def scan_hl_lh(symbols=None, x_days=5, swing_period=5):
    """
    Scan for:
    - HIGHER LOW (bullish): latest swing low > previous swing low, formed within last x_days
    - LOWER HIGH (bearish): latest swing high < previous swing high, formed within last x_days

    x_days: the swing low/high must have formed within the last x_days bars.
    """
    import yfinance as yf
    import sqlite3
    from pathlib import Path
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if symbols is None:
        from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
        db = _OIAPP_DB_PATH
        con = sqlite3.connect(db)
        symbols = [r[0] for r in con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()]
        con.close()

    def _check(sym):
        try:
            from ..services.market import get_history_cached
            hist = get_history_cached(sym, period="6mo", interval="1d")
            if hist is None or len(hist) < 40:
                return None
            closes = hist["Close"].values.astype(float)
            highs  = hist["High"].values.astype(float)
            lows   = hist["Low"].values.astype(float)

            n = len(closes)
            spot = closes[-1]

            sw_hi, sw_lo = _find_swing_pivots(highs, lows, period=swing_period)
            if len(sw_hi) < 2 and len(sw_lo) < 2:
                return None

            results = []

            # HIGHER LOW check
            if len(sw_lo) >= 2:
                last_lo_idx,  last_lo_px  = sw_lo[-1]
                prev_lo_idx,  prev_lo_px  = sw_lo[-2]
                days_ago = n - 1 - last_lo_idx
                if days_ago <= x_days and last_lo_px > prev_lo_px:
                    rise_pct = round((last_lo_px - prev_lo_px) / prev_lo_px * 100, 2)
                    native = min(100, int(45 + rise_pct * 4 + max(0, 12 - days_ago) * 2))
                    result = {
                        "symbol":      sym,
                        "setup":       "Higher Low",
                        "setup_type":  "Higher Low",
                        "direction":   "Bullish",
                        "icon":        "�",
                        "spot":        round(spot, 2),
                        "last_swing":  round(last_lo_px, 2),
                        "prev_swing":  round(prev_lo_px, 2),
                        "swing_rise":  rise_pct,
                        "days_ago":    days_ago,
                        "native_score": native,
                        "trend_age":    days_ago,
                        "detail":      f"Higher Low: ${last_lo_px:.2f} > ${prev_lo_px:.2f} "
                                       f"(+{rise_pct:.1f}%) formed {days_ago}d ago — bullish structure",
                    }
                    results.append(attach_scanner_scores(result, sym, frame=hist, setup_type="Higher Low", direction="BULLISH", native_score=native, trend_age=days_ago))

            # LOWER HIGH check
            if len(sw_hi) >= 2:
                last_hi_idx,  last_hi_px  = sw_hi[-1]
                prev_hi_idx,  prev_hi_px  = sw_hi[-2]
                days_ago = n - 1 - last_hi_idx
                if days_ago <= x_days and last_hi_px < prev_hi_px:
                    drop_pct = round((prev_hi_px - last_hi_px) / prev_hi_px * 100, 2)
                    native = min(100, int(45 + drop_pct * 4 + max(0, 12 - days_ago) * 2))
                    result = {
                        "symbol":      sym,
                        "setup":       "Lower High",
                        "setup_type":  "Lower High",
                        "direction":   "Bearish",
                        "icon":        "�",
                        "spot":        round(spot, 2),
                        "last_swing":  round(last_hi_px, 2),
                        "prev_swing":  round(prev_hi_px, 2),
                        "swing_drop":  drop_pct,
                        "days_ago":    days_ago,
                        "native_score": native,
                        "trend_age":    days_ago,
                        "detail":      f"Lower High: ${last_hi_px:.2f} < ${prev_hi_px:.2f} "
                                       f"(−{drop_pct:.1f}%) formed {days_ago}d ago — bearish structure",
                    }
                    results.append(attach_scanner_scores(result, sym, frame=hist, setup_type="Lower High", direction="Bearish", native_score=native, trend_age=days_ago))

            return results if results else None
        except Exception:
            return None

    out = []
    ex = ThreadPoolExecutor(max_workers=8)
    try:
        futs = {ex.submit(_check, s): s for s in symbols}
        from ..services.bounded_wait import bounded_as_completed
        for fut, s in bounded_as_completed(futs, timeout=60,
                on_timeout=lambda ks: print(f"[sr_breakout_scanner] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                continue
            r = fut.result()
            if r:
                out.extend(r)
    finally:
        ex.shutdown(wait=False)

    out.sort(key=lambda x: (x["direction"], x["days_ago"], x["symbol"]))
    return out


@sr_bp.route("/hl_lh")
def api_hl_lh():
    x_days    = request.args.get("x_days", 5, type=int)
    swing_prd = request.args.get("swing_period", 5, type=int)
    wl_id     = request.args.get("watchlist_id", None, type=int)
    syms      = _get_wl_symbols(wl_id)
    try:
        results = scan_hl_lh(syms, x_days=x_days, swing_period=swing_prd)
        return jsonify({"results": results, "count": len(results),
                        "x_days": x_days, "swing_period": swing_prd})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:400]}), 500
