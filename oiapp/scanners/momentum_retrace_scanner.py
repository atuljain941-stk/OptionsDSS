"""
Momentum Retracement Scanner — "First Pullback After Sharp Move"
================================================================
Logic:
  1. Sharp move: stock moved ≥ threshold % in 10-20 days (up=bull, down=bear)
  2. First retracement: peak/trough happened 3-8 bars ago (not a multi-week base)
  3. Retracing cleanly: 3-6 bars of lower highs (bull) or higher lows (bear)
  4. Trend still intact: price above EMA20/50, MACD positive, RSI-EMA diff ok
  5. Bounce setup: RSI cooling off but not broken (45-65 bull / 35-55 bear)
  6. No volume climax on pullback (volume declining = healthy)

RSI thresholds:
  - Bull: RSI was overbought (≥70) OR sharp ΔRSI (≥20) during the move
  - Bear: RSI was oversold (≤30) OR sharp ΔRSI (≤-20) during the move
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .scoring_service import attach_scanner_scores

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")


# ── TA helpers ───────────────────────────────────────────────────────────────

def _ema(series, period):
    k = 2 / (period + 1)
    out = list(series)
    for i in range(1, len(out)):
        out[i] = series[i] * k + out[i-1] * (1 - k)
    return out

def _rsi(closes, period=14):
    out = [50.0] * len(closes)
    if len(closes) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        if i > period:
            ag = (ag * (period-1) + gains[i-1]) / period
            al = (al * (period-1) + losses[i-1]) / period
        rs = ag / al if al > 0 else 100
        out[i] = 100 - 100 / (1 + rs)
    return out

def _macd(closes, fast=12, slow=26, signal=9):
    ef = _ema(closes, fast); es = _ema(closes, slow)
    ml = [f - s for f, s in zip(ef, es)]
    sl = _ema(ml, signal)
    hist = [m - s for m, s in zip(ml, sl)]
    return ml, sl, hist

def _atr(H, L, C, period=14):
    trs = [H[0] - L[0]]
    for i in range(1, len(C)):
        trs.append(max(H[i]-L[i], abs(H[i]-C[i-1]), abs(L[i]-C[i-1])))
    atr = [sum(trs[:period]) / period]
    for i in range(period, len(trs)):
        atr.append((atr[-1]*(period-1) + trs[i]) / period)
    return atr

def _highest(series, n):
    """Highest value in last n bars (including current)."""
    return max(series[-n:]) if len(series) >= n else max(series)

def _lowest(series, n):
    return min(series[-n:]) if len(series) >= n else min(series)

def _bars_since_high(series, window=20):
    """How many bars ago was the highest close in last `window` bars."""
    sub = series[-window:]
    peak_idx = sub.index(max(sub))
    return len(sub) - 1 - peak_idx

def _bars_since_low(series, window=20):
    sub = series[-window:]
    trough_idx = sub.index(min(sub))
    return len(sub) - 1 - trough_idx

def _recent_recovery_or_sideways(closes, highs, lows, direction="bull", recent_days=5, atr=None):
    """True when the latest bars resume trend direction or are compressed sideways."""
    try:
        recent_days = max(2, int(recent_days or 5))
    except Exception:
        recent_days = 5
    n = len(closes) - 1
    start = max(0, n - recent_days + 1)
    seg = closes[start:n + 1]
    if len(seg) < 2:
        return True

    first = float(seg[0])
    last = float(seg[-1])
    hi = float(max(seg))
    lo = float(min(seg))
    rng = hi - lo

    atr_val = None
    if atr:
        try:
            atr_val = float(atr[-1])
        except Exception:
            atr_val = None

    if direction == "bull":
        trend_resume = last >= first and seg[-1] >= seg[-2]
    else:
        trend_resume = last <= first and seg[-1] <= seg[-2]

    price_ref = max(abs(last), 1e-9)
    sideways_pct = (rng / price_ref) * 100
    sideways_atr = atr_val is not None and rng <= max(1.15 * atr_val, price_ref * 0.02)
    sideways = sideways_atr or sideways_pct <= 2.5

    return bool(trend_resume or sideways)

def _count_retrace_bars(closes, direction="bull", from_bar=None):
    """
    Count consecutive lower-closes (bull) or higher-closes (bear)
    starting from `from_bar` bars ago.
    """
    n = len(closes)
    if from_bar is None or from_bar <= 0:
        return 0
    count = 0
    start = max(0, n - from_bar)
    for i in range(start, n):
        if direction == "bull":
            if i > start and closes[i] < closes[i-1]:
                count += 1
            elif i > start and closes[i] > closes[i-1]:
                break   # bounce started
        else:
            if i > start and closes[i] > closes[i-1]:
                count += 1
            elif i > start and closes[i] < closes[i-1]:
                break
    return count


# ── Per-symbol analysis ─────────────────────────────────────────────────────

def _analyze(sym, move_window=15, move_pct=6.0, retrace_min=3, retrace_max=8,
             rsi_ob=68, rsi_os=32, delta_rsi_thr=18, recovery_days=5):
    try:
        import yfinance as yf
        df = yf.Ticker(sym).history(period="3mo", interval="1d")
        if df is None or len(df) < 50:
            return None

        C = df["Close"].tolist()
        H = df["High"].tolist()
        L = df["Low"].tolist()
        V = df["Volume"].tolist()
        n = len(C) - 1

        rsi14     = _rsi(C, 14)
        ema20     = _ema(C, 20)
        ema50     = _ema(C, 50)
        ema200    = _ema(C, 200)
        ema90_rsi = _ema(rsi14, 90)
        macd_l, macd_s, macd_h = _macd(C)
        atr14     = _atr(H, L, C, 14)

        cur        = C[n]
        cur_rsi    = rsi14[n]
        cur_ema20  = ema20[n]
        cur_ema50  = ema50[n]
        cur_ema200 = ema200[n]
        cur_macd   = macd_l[n]; cur_msig = macd_s[n]; cur_mhist = macd_h[n]
        cur_atr    = atr14[-1] if atr14 else 0
        rsi_ema_d  = rsi14[n] - ema90_rsi[n]

        # ── How many bars since the most recent peak / trough (in move_window) ──
        bars_since_peak   = _bars_since_high(C, move_window)
        bars_since_trough = _bars_since_low(C, move_window)

        # ── The move itself: how much did price run before the peak/trough ──────
        # Bull: price change from trough-before-peak to peak
        peak_idx   = max(range(max(0, n-move_window), n+1), key=lambda i: C[i])
        trough_before_peak = min(C[max(0, peak_idx-move_window):peak_idx+1]) if peak_idx > 0 else C[0]
        bull_move_pct = (C[peak_idx] - trough_before_peak) / trough_before_peak * 100 if trough_before_peak else 0

        trough_idx = min(range(max(0, n-move_window), n+1), key=lambda i: C[i])
        peak_before_trough = max(C[max(0, trough_idx-move_window):trough_idx+1]) if trough_idx > 0 else C[0]
        bear_move_pct = (peak_before_trough - C[trough_idx]) / peak_before_trough * 100 if peak_before_trough else 0

        # ── RSI at peak / trough ─────────────────────────────────────────────
        rsi_at_peak   = rsi14[peak_idx]   if peak_idx < len(rsi14) else 50
        rsi_at_trough = rsi14[trough_idx] if trough_idx < len(rsi14) else 50

        # Delta RSI: RSI at peak vs RSI 10 bars before peak
        rsi_before_peak   = rsi14[max(0, peak_idx-10)]
        rsi_before_trough = rsi14[max(0, trough_idx-10)]
        delta_rsi_bull = rsi_at_peak   - rsi_before_peak
        delta_rsi_bear = rsi_before_trough - rsi_at_trough   # positive when drop is big

        # ── Volume on retrace vs move ────────────────────────────────────────
        vol_avg_move   = sum(V[max(0,peak_idx-move_window):peak_idx+1]) / max(1, move_window)
        vol_avg_retrace= sum(V[peak_idx:n+1]) / max(1, bars_since_peak + 1) if bars_since_peak > 0 else V[n]
        vol_declining  = vol_avg_retrace < vol_avg_move * 0.85

        vol_avg_move_b  = sum(V[max(0,trough_idx-move_window):trough_idx+1]) / max(1, move_window)
        vol_avg_ret_b   = sum(V[trough_idx:n+1]) / max(1, bars_since_trough + 1) if bars_since_trough > 0 else V[n]
        vol_declining_b = vol_avg_ret_b < vol_avg_move_b * 0.85

        recent_bull_ok = _recent_recovery_or_sideways(C, H, L, "bull", recovery_days, atr14)
        recent_bear_ok = _recent_recovery_or_sideways(C, H, L, "bear", recovery_days, atr14)

        # ── Pullback depth (Fib) ─────────────────────────────────────────────
        if C[peak_idx] > trough_before_peak:
            fib_pct_bull = (C[peak_idx] - cur) / (C[peak_idx] - trough_before_peak) * 100
        else:
            fib_pct_bull = 0

        if peak_before_trough > C[trough_idx]:
            fib_pct_bear = (cur - C[trough_idx]) / (peak_before_trough - C[trough_idx]) * 100
        else:
            fib_pct_bear = 0

        # ── BULLISH SETUP ─────────────────────────────────────────────────────
        # Condition 1: Sharp up-move
        b1_sharp     = bull_move_pct >= move_pct
        # Condition 2: RSI was overbought OR big RSI jump during move
        b2_rsi_ob    = rsi_at_peak >= rsi_ob or delta_rsi_bull >= delta_rsi_thr
        # Condition 3: Peak was 3-8 bars ago (first retracement)
        b3_first     = retrace_min <= bars_since_peak <= retrace_max
        # Condition 4: Currently retracing (below peak)
        b4_retrace   = cur < C[peak_idx]
        # Condition 5: Fibonacci depth 20-55% (not too deep, not too shallow)
        b5_fib       = 18 <= fib_pct_bull <= 55
        # Condition 6: Trend still up
        b6_trend     = cur > cur_ema50 or cur > cur_ema20
        # Condition 7: MACD still bullish or just turned
        b7_macd      = cur_macd > cur_msig or cur_mhist > macd_h[n-1]
        # Condition 8: RSI cooling but not broken (not back to oversold)
        b8_rsi_cool  = 40 <= cur_rsi <= 65
        # Condition 9: RSI-EMA diff still positive or near zero
        b9_rsi_ema   = rsi_ema_d > -8

        bull_score = 0; bull_sigs = []
        if b1_sharp:   bull_score += 20; bull_sigs.append(f"↑{bull_move_pct:.0f}% in {move_window}d")
        if b2_rsi_ob:
            bull_score += 18
            if rsi_at_peak >= rsi_ob:   bull_sigs.append(f"RSI peak {rsi_at_peak:.0f} (OB)")
            else:                        bull_sigs.append(f"ΔRSI +{delta_rsi_bull:.0f}")
        if b3_first:   bull_score += 20; bull_sigs.append(f"First retrace ({bars_since_peak}d)")
        if b5_fib:     bull_score += 12; bull_sigs.append(f"Fib {fib_pct_bull:.0f}%")
        if b6_trend:   bull_score += 10; bull_sigs.append("Above EMA" + ("20" if cur > cur_ema20 else "50"))
        if b7_macd:    bull_score += 10; bull_sigs.append("MACD ↑" if cur_mhist > macd_h[n-1] else "MACD+")
        if b8_rsi_cool:bull_score += 8;  bull_sigs.append(f"RSI {cur_rsi:.0f}")
        if b9_rsi_ema: bull_score += 6;  bull_sigs.append(f"RSI-EMA {rsi_ema_d:+.0f}")
        if vol_declining: bull_score += 6; bull_sigs.append("Vol ↓ on retrace")
        if recent_bull_ok: bull_score += 7; bull_sigs.append(f"Recent {recovery_days}d trend/sideways OK")
        if cur > cur_ema200: bull_score += 5; bull_sigs.append("Above EMA200")
        # Penalty for too deep
        if fib_pct_bull > 55: bull_score -= 10

        bull_qualifies = (b1_sharp and b2_rsi_ob and b3_first and b4_retrace
                         and b6_trend and recent_bull_ok and bull_score >= 55)

        # ── BEARISH SETUP ─────────────────────────────────────────────────────
        # Condition 1: Sharp down-move
        r1_sharp     = bear_move_pct >= move_pct
        # Condition 2: RSI was oversold OR big RSI drop during move
        r2_rsi_os    = rsi_at_trough <= rsi_os or delta_rsi_bear >= delta_rsi_thr
        # Condition 3: Trough was 3-8 bars ago (first bounce)
        r3_first     = retrace_min <= bars_since_trough <= retrace_max
        # Condition 4: Currently bouncing (above trough)
        r4_bounce    = cur > C[trough_idx]
        # Condition 5: Fibonacci bounce 18-55%
        r5_fib       = 18 <= fib_pct_bear <= 55
        # Condition 6: Trend still down
        r6_trend     = cur < cur_ema50 or cur < cur_ema20
        # Condition 7: MACD still bearish or just turned down
        r7_macd      = cur_macd < cur_msig or cur_mhist < macd_h[n-1]
        # Condition 8: RSI bounced but not overbought
        r8_rsi_cool  = 35 <= cur_rsi <= 60
        # Condition 9: RSI-EMA diff still negative
        r9_rsi_ema   = rsi_ema_d < 8

        bear_score = 0; bear_sigs = []
        if r1_sharp:   bear_score += 20; bear_sigs.append(f"↓{bear_move_pct:.0f}% in {move_window}d")
        if r2_rsi_os:
            bear_score += 18
            if rsi_at_trough <= rsi_os: bear_sigs.append(f"RSI trough {rsi_at_trough:.0f} (OS)")
            else:                        bear_sigs.append(f"ΔRSI -{delta_rsi_bear:.0f}")
        if r3_first:   bear_score += 20; bear_sigs.append(f"First bounce ({bars_since_trough}d)")
        if r5_fib:     bear_score += 12; bear_sigs.append(f"Fib {fib_pct_bear:.0f}%")
        if r6_trend:   bear_score += 10; bear_sigs.append("Below EMA" + ("20" if cur < cur_ema20 else "50"))
        if r7_macd:    bear_score += 10; bear_sigs.append("MACD ↓" if cur_mhist < macd_h[n-1] else "MACD-")
        if r8_rsi_cool:bear_score += 8;  bear_sigs.append(f"RSI {cur_rsi:.0f}")
        if r9_rsi_ema: bear_score += 6;  bear_sigs.append(f"RSI-EMA {rsi_ema_d:+.0f}")
        if vol_declining_b: bear_score += 6; bear_sigs.append("Vol ↓ on bounce")
        if recent_bear_ok: bear_score += 7; bear_sigs.append(f"Recent {recovery_days}d trend/sideways OK")
        if cur < cur_ema200: bear_score += 5; bear_sigs.append("Below EMA200")
        if fib_pct_bear > 55: bear_score -= 10

        bear_qualifies = (r1_sharp and r2_rsi_os and r3_first and r4_bounce
                         and r6_trend and recent_bear_ok and bear_score >= 55)

        if not bull_qualifies and not bear_qualifies:
            return None

        # Pick dominant direction
        if bull_qualifies and bear_qualifies:
            direction = "BULL" if bull_score >= bear_score else "BEAR"
        else:
            direction = "BULL" if bull_qualifies else "BEAR"

        score  = bull_score  if direction == "BULL" else bear_score
        signals= bull_sigs   if direction == "BULL" else bear_sigs
        bars_retrace = bars_since_peak if direction == "BULL" else bars_since_trough

        result = {
            "symbol":        sym,
            "direction":     direction,
            "setup_type":    "Momentum Retrace",
            "score":         min(score, 100),
            "native_score":  min(score, 100),
            "price":         round(cur, 2),
            "rsi":           round(cur_rsi, 1),
            "rsi_at_extreme":round(rsi_at_peak if direction=="BULL" else rsi_at_trough, 1),
            "delta_rsi":     round(delta_rsi_bull if direction=="BULL" else -delta_rsi_bear, 1),
            "rsi_ema_diff":  round(rsi_ema_d, 1),
            "move_pct":      round(bull_move_pct if direction=="BULL" else bear_move_pct, 1),
            "fib_pct":       round(fib_pct_bull  if direction=="BULL" else fib_pct_bear,  1),
            "bars_retrace":  bars_retrace,
            "macd":          round(cur_macd, 3),
            "macd_sig":      round(cur_msig, 3),
            "macd_hist":     round(cur_mhist, 3),
            "macd_turning":  cur_mhist > macd_h[n-1] if direction=="BULL" else cur_mhist < macd_h[n-1],
            "ema20":         round(cur_ema20, 2),
            "ema50":         round(cur_ema50, 2),
            "ema200":        round(cur_ema200, 2),
            "atr":           round(cur_atr, 2),
            "vol_declining": vol_declining if direction=="BULL" else vol_declining_b,
            "signals":       signals,
            "bull_score":    bull_score,
            "bear_score":    bear_score,
            "trend_age":     bars_retrace,
        }
        return attach_scanner_scores(result, sym, frame=df, setup_type="Momentum Retrace", direction=direction, native_score=result["native_score"], trend_age=bars_retrace)

    except Exception:
        return None


# ── Main ─────────────────────────────────────────────────────────────────────

def run_momentum_retrace_scan(symbols=None, lookback=10, workers=25,
                               rsi_peak_thr=68, rsi_trough_thr=32,
                               delta_rsi_thr=18, move_pct=6.0,
                               retrace_min=3, retrace_max=8,
                               recovery_days=5):
    if not symbols:
        try:
            con = sqlite3.connect(DB_PATH)
            rows = con.execute("SELECT symbol FROM symbols").fetchall()
            con.close()
            symbols = [r[0] for r in rows] if rows else []
        except:
            symbols = []
    if not symbols:
        symbols = [
            "AAPL","AMZN","MSFT","NVDA","META","GOOGL","TSLA","AMD","AVGO","NFLX",
            "COIN","PLTR","MSTR","CVNA","HOOD","DASH","RBLX","SHOP","NOW","CRM",
            "JPM","BAC","V","PYPL","ADBE","ORCL","QCOM","MU","LRCX","MRVL",
            "XOM","CVX","OXY","HAL","FSLR","CCJ","NEM","FCX","VST","NEE",
            "TSLA","GM","UAL","DAL","RCL","WYNN","LVS","CMG","SBUX","NKE",
            "PANW","DDOG","OKTA","UBER","ABNB","SNOW","ARM","SMCI"
        ]

    bulls = []; bears = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                _analyze, sym,
                lookback,       # move_window
                move_pct,
                retrace_min, retrace_max,
                rsi_peak_thr, rsi_trough_thr, delta_rsi_thr, recovery_days
            ): sym
            for sym in symbols
        }
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                (bulls if r["direction"]=="BULL" else bears).append(r)

    bulls.sort(key=lambda x: -x["score"])
    bears.sort(key=lambda x: -x["score"])
    return {
        "bulls": bulls[:30], "bears": bears[:30],
        "total_scanned": len(symbols),
        "bull_count": len(bulls), "bear_count": len(bears)
    }
