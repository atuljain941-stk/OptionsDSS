"""
institutional_scanner.py — Institutional Accumulation Breakout Scanner

Identifies stocks where smart money has been quietly accumulating (tight base,
rising OI) then made a decisive move with strong volume — the classic 
institutional footprint. Configurable thresholds.
"""
import sqlite3, json, re
from pathlib import Path
from datetime import datetime, date, timedelta
from flask import Blueprint, jsonify, request

inst_bp = Blueprint("inst_bp", __name__, url_prefix="/scanner/institutional")
from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

# ── Default scoring parameters ─────────────────────────────────────────────
DEFAULTS = {
    "min_score":          3.0,   # minimum composite score to show result
    "ema200_pct":         3.0,   # max % below 200 EMA (hard filter)
    "base_days":          30,    # FINAL/tightest base window (days) -- the last leg into the breakout
    "lookback_days":      65,    # CONTEXT window (days) -- ~13 weeks, the standard VCP formation
                                  # period (Minervini/O'Neil methodology). Drives VCP contraction
                                  # detection, lookback-relative volume, and lookback-relative price
                                  # position. base_days should be <= lookback_days: base_days is the
                                  # final, tightest leg; lookback_days is the whole structure it sits in.
    "base_tight_max":     30.0,  # max base range % to count as tight base
    "min_price_5d":       1.5,   # min 5D price change %
    "min_vol_surge":      1.2,   # min volume surge (5D avg / 20D avg)
    "rsi_min":            40.0,  # min RSI
    "rsi_max":            80.0,  # max RSI (avoid overbought)
    "min_price":          3.0,   # min stock price (filter penny stocks)
    "require_above_base": False, # if True, price must exceed the 30D base high
    "udvr_lookback":      20,    # up/down volume ratio window -- SAME formula as smart_money_distribution_scanner.py's
                                  # distribution detector, used here for the opposite read: high UDVR = accumulation
                                  # (big up-day volume, quiet down-days), not distribution.
    "rs_symbol":          "SPY", # benchmark for relative-strength leadership check
    "require_fundamentals": True, # pulls EPS/revenue growth via the same cached fundamentals fetch Scanner Builder's
                                  # EarningsGrowthPct()/RevenueGrowthPct() use -- one extra data call per symbol
                                  # (cached), set False to skip it and speed up large scans.
    "min_earnings_growth": 25.0, # YoY EPS growth % floor for the fundamentals score bonus (not a hard filter)
    "min_revenue_growth": 15.0,  # YoY revenue growth % floor for the fundamentals score bonus (not a hard filter)
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

def _get_oi_data(symbol, days=40, min_days_to_expiry=15):
    """min_days_to_expiry excludes contracts close to expiring from the OI
    sum. Without this, total OI naturally craters right after every
    monthly expiration (front-month contracts roll off, OI resets near
    zero, then rebuilds through the next cycle) -- that's a calendar
    artifact, not institutional distribution, but the raw first-vs-last
    OI comparison can't tell the difference. Excluding near-expiry
    contracts keeps the OI series measuring genuine position-building
    instead of getting swamped by the expiration cliff."""
    con = _conn()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = con.execute("""
        SELECT date,
               SUM(CASE WHEN type='call' THEN oi ELSE 0 END) call_oi,
               SUM(CASE WHEN type='put'  THEN oi ELSE 0 END) put_oi
        FROM options
        WHERE symbol=? AND date>=? AND expiration>=date
          AND date(expiration) >= date(date, '+' || ? || ' days')
        GROUP BY date ORDER BY date
    """, (symbol, cutoff, int(min_days_to_expiry))).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _get_price_history(symbol, min_days=400):
    """Reads OHLCV directly from price_cache instead of live-fetching from
    yfinance on every scan. price_cache already holds 3+ years of daily
    history (backfilled via scanner_builder.py's
    _backfill_price_history_to_cache), so there's no reason to re-fetch
    live and eat yfinance latency/rate limits on every single scan.

    min_days=400 comfortably covers the 252-bar 52-week-high lookback
    used below with room to spare. Returns None if fewer than 60 rows are
    cached, matching the previous `len(hist) < 60` guard so callers can
    bail out identically to before.
    """
    con = _conn()
    try:
        rows = con.execute(
            """SELECT date, open, high, low, close, volume
               FROM price_cache WHERE symbol=? ORDER BY date DESC LIMIT ?""",
            (symbol.upper().strip(), min_days),
        ).fetchall()
    finally:
        con.close()
    if not rows or len(rows) < 60:
        return None
    rows = list(reversed(rows))  # ascending date order, oldest first
    import numpy as np
    return {
        "close": np.array([r["close"] for r in rows], dtype=float),
        "high": np.array([r["high"] for r in rows], dtype=float),
        "low": np.array([r["low"] for r in rows], dtype=float),
        "volume": np.array([r["volume"] for r in rows], dtype=float),
        "date": [r["date"] for r in rows],
    }

def _ema(series, n):
    return series.ewm(span=n, adjust=False).mean()


def _detect_vcp(highs, lows, lookback_days, min_contractions=2, tightening_tolerance=1.1, min_swing_pct=3.0):
    """Real VCP (Volatility Contraction Pattern) detection, not just a
    single base's range tightness. Uses a standard zigzag pivot algorithm
    (a swing high/low is confirmed once price reverses by min_swing_pct%
    from the extreme) to find the actual sequence of local peak-to-trough
    legs, then checks whether those pullback percentages are genuinely
    CONTRACTING over time -- the classic Minervini/O'Neil signature: e.g.
    -25% -> -15% -> -8%, each leg shallower than the last.

    (v1 of this function used a single running "all-time high in window"
    peak, which collapses a real multi-leg VCP into one blob whenever a
    later local high doesn't exceed the very first peak -- fixed here.)

    Returns a dict with `detected` (bool), `num_contractions`, the last few
    `contraction_pcts` (most recent last), and a `tightening_ratio` (last
    contraction / first contraction -- smaller means tighter VCP).
    """
    m = min(len(highs), len(lows), lookback_days)
    if m < 15:
        return {"detected": False, "num_contractions": 0, "contraction_pcts": [], "tightening_ratio": None}

    h = highs[-m:]
    l = lows[-m:]

    # ── ZigZag pivot detection ──────────────────────────────────────────
    pivots = []  # (index, price, 'H' or 'L')
    trend = None
    extreme_idx = 0
    extreme_price = float(h[0])

    for i in range(1, m):
        if trend is None:
            if float(h[i]) >= extreme_price * (1 + min_swing_pct / 100):
                trend = 'up'
                extreme_price = float(h[i]); extreme_idx = i
            elif float(l[i]) <= extreme_price * (1 - min_swing_pct / 100):
                trend = 'down'
                extreme_price = float(l[i]); extreme_idx = i
            continue
        if trend == 'up':
            if float(h[i]) > extreme_price:
                extreme_price = float(h[i]); extreme_idx = i
            elif float(l[i]) <= extreme_price * (1 - min_swing_pct / 100):
                pivots.append((extreme_idx, extreme_price, 'H'))
                trend = 'down'
                extreme_price = float(l[i]); extreme_idx = i
        else:  # trend == 'down'
            if float(l[i]) < extreme_price:
                extreme_price = float(l[i]); extreme_idx = i
            elif float(h[i]) >= extreme_price * (1 + min_swing_pct / 100):
                pivots.append((extreme_idx, extreme_price, 'L'))
                trend = 'up'
                extreme_price = float(h[i]); extreme_idx = i

    # Close out the final in-progress swing too -- typically the "final
    # base" the stock is sitting in right now.
    if trend == 'up':
        pivots.append((extreme_idx, extreme_price, 'H'))
    elif trend == 'down':
        pivots.append((extreme_idx, extreme_price, 'L'))

    # Walk consecutive High->Low pivot pairs as pullback legs, in
    # chronological order.
    contractions = []
    for i in range(len(pivots) - 1):
        _, p1, t1 = pivots[i]
        _, p2, t2 = pivots[i + 1]
        if t1 == 'H' and t2 == 'L' and p1 > 0:
            pct = (p1 - p2) / p1 * 100
            if pct > 0.5:
                contractions.append(pct)

    num = len(contractions)
    detected = False
    tightening_ratio = None
    if num >= min_contractions and contractions[0] > 0:
        # Broadly contracting: each leg roughly <= the one before it, with
        # a little tolerance for real-world noise (real VCPs are never
        # perfectly monotonic). Allow at most one violation in the sequence.
        violations = sum(
            1 for i in range(1, num)
            if contractions[i] > contractions[i - 1] * tightening_tolerance
        )
        detected = violations <= 1 and contractions[-1] < contractions[0] * 0.7
        tightening_ratio = round(contractions[-1] / contractions[0], 3)

    return {
        "detected": detected,
        "num_contractions": num,
        "contraction_pcts": [round(c, 1) for c in contractions[-5:]],
        "tightening_ratio": tightening_ratio,
    }

def _scan_symbol(sym, use_oi=True, p=None):
    if p is None: p = DEFAULTS
    try:
        import pandas as pd, numpy as np

        hist = _get_price_history(sym, min_days=400)
        if hist is None:
            return None

        closes = hist["close"]
        highs  = hist["high"]
        lows   = hist["low"]
        vols   = hist["volume"]
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

        # Base detection (final/tightest leg)
        base_days = int(p.get("base_days", 30))
        lookback_days = int(p.get("lookback_days", 65))
        if n < max(base_days, lookback_days) + 10:
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

        # ── Lookback context window (broader structure the base sits in) ──
        # Where does current price sit relative to the whole lookback range
        # (not just the final base), and how does current volume compare to
        # the lookback-period baseline (a longer, more stable reference than
        # the 20D window used for vol_surge above)?
        lookback_high = float(max(highs[-lookback_days:]))
        lookback_low  = float(min(lows[-lookback_days:]))
        pct_in_lookback_range = round((price - lookback_low) / max(0.01, lookback_high - lookback_low) * 100, 1)
        vol_lookback_avg = float(np.mean(vols[-lookback_days:-5])) if lookback_days > 10 else vol_20d_avg
        vol_vs_lookback = round(vol_5d_avg / max(1, vol_lookback_avg), 2)

        # ── VCP (Volatility Contraction Pattern) detection over the lookback
        # window -- this is the piece a single base's range tightness alone
        # can't express: a genuine sequence of progressively shallower
        # pullbacks, not just "the last 30 days were tight".
        vcp = _detect_vcp(highs, lows, lookback_days)

        # 52W metrics
        hi_52w = float(max(highs[-252:]))
        pct_from_52wh = round((price - hi_52w) / hi_52w * 100, 1)
        new_52w_high = price >= hi_52w * 0.98

        # ── Up/Down Volume Ratio -- same formula as smart_money_distribution
        # _scanner.py, used for the opposite read here: HIGH udvr means big
        # up-day volume with quiet down-days (accumulation), matching
        # "repeated big up-days on 1.5-2x volume, down-days should be quiet".
        udvr_lb = int(p.get("udvr_lookback", 20))
        day_chg = pd.Series(closes).diff()
        up_vol = pd.Series(vols).where(day_chg > 0, 0.0)
        down_vol = pd.Series(vols).where(day_chg < 0, 0.0)
        sum_up = float(up_vol.tail(udvr_lb).sum())
        sum_down = float(down_vol.tail(udvr_lb).sum())
        udvr = round(sum_up / (sum_down + 1), 2)

        # ── Relative Strength leadership -- RS line (stock/benchmark) sitting
        # at or near ITS OWN recent high while price is also near its own
        # high confirms genuine leadership, not just "the whole market ran".
        # Same rs_line = stock/benchmark formula as the distribution scanner's
        # RS-rollover check, but reading for strength instead of weakness.
        rs_symbol = str(p.get("rs_symbol", "SPY"))
        rs_leadership = False
        rs_pct_vs_bench = None
        if rs_symbol and rs_symbol.upper() != sym.upper():
            try:
                from .smart_money_distribution_scanner import _get_bench_closes
                bench_closes = _get_bench_closes(rs_symbol)
                if bench_closes is not None and len(bench_closes) >= n:
                    bench_s = pd.Series(bench_closes[-n:])
                    rs_line = s / bench_s.replace(0, np.nan)
                    rs_high20 = rs_line.rolling(20).max()
                    price_high20 = s.rolling(20).max()
                    rs_leadership = bool(
                        pd.notna(rs_line.iloc[-1]) and pd.notna(rs_high20.iloc[-1])
                        and (rs_line.iloc[-1] >= rs_high20.iloc[-1] * 0.98)
                        and (price >= price_high20.iloc[-1] * 0.95)
                    )
                    if n >= 21 and pd.notna(rs_line.iloc[-21]) and rs_line.iloc[-21]:
                        rs_pct_vs_bench = round((rs_line.iloc[-1] / rs_line.iloc[-21] - 1) * 100, 2)
            except Exception:
                pass

        # ── Fundamentals: EPS/revenue growth, the "strong fundamentals
        # support strong technicals" leg of the checklist. Reuses the exact
        # same cached fetch Scanner Builder's EarningsGrowthPct()/
        # RevenueGrowthPct() primitives use, so this can never report a
        # different number than those do for the same symbol -- one extra
        # data call per symbol (cached), skippable via require_fundamentals.
        earnings_growth = None
        revenue_growth = None
        if p.get("require_fundamentals", True):
            try:
                from .scanner_builder import _get_fundamentals_cached
                fd = _get_fundamentals_cached(sym) or {}
                eg = fd.get("earnings_growth")
                rg = fd.get("revenue_growth")
                earnings_growth = round(float(eg), 2) if eg is not None else None
                revenue_growth = round(float(rg), 2) if rg is not None else None
            except Exception:
                pass

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
            oi_rows = _get_oi_data(sym, days=int(lookback_days))
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
        if vcp["detected"]:
            # Base bonus for a genuine VCP, plus extra for more contractions
            # (up to 4, per Minervini's typical range) and a tighter final
            # leg relative to the first (lower tightening_ratio = better).
            vcp_bonus = 0.8 + 0.2 * min(vcp["num_contractions"] - 2, 2)
            if vcp["tightening_ratio"] is not None and vcp["tightening_ratio"] < 0.4:
                vcp_bonus += 0.3
            score += vcp_bonus

        # ── Up/Down Volume Ratio: rewards accumulation-style volume
        # (big up-day volume, quiet down-days), not distribution.
        if   udvr >= 2.0:  score += 1.0
        elif udvr >= 1.5:  score += 0.7
        elif udvr >= 1.2:  score += 0.4
        elif udvr < 0.8:   score -= 0.5  # down-day volume actually dominating -- a real negative signal, not just "no bonus"

        # ── Relative strength leadership vs benchmark
        if rs_leadership:
            score += 0.8

        # ── Fundamentals: EPS/revenue growth support for the technical setup
        min_eg = float(p.get("min_earnings_growth", 25.0))
        min_rg = float(p.get("min_revenue_growth", 15.0))
        if earnings_growth is not None and earnings_growth >= min_eg:
            score += 0.6
        if revenue_growth is not None and revenue_growth >= min_rg:
            score += 0.4

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
            "lookback_days": lookback_days,
            "lookback_high": _safe(round(lookback_high, 2)), "lookback_low": _safe(round(lookback_low, 2)),
            "pct_in_lookback_range": _safe(pct_in_lookback_range),
            "vol_vs_lookback": _safe(vol_vs_lookback),
            "vcp_detected": vcp["detected"], "vcp_num_contractions": vcp["num_contractions"],
            "vcp_contraction_pcts": vcp["contraction_pcts"],
            "vcp_tightening_ratio": _safe(vcp["tightening_ratio"]) if vcp["tightening_ratio"] is not None else None,
            "udvr": _safe(udvr),
            "rs_leadership": rs_leadership, "rs_pct_vs_bench_20d": _safe(rs_pct_vs_bench),
            "earnings_growth_pct": _safe(earnings_growth), "revenue_growth_pct": _safe(revenue_growth),
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
    ex = ThreadPoolExecutor(max_workers=6)
    try:
        from ..services.bounded_wait import bounded_as_completed
        futures = {ex.submit(_scan_symbol, sym, use_oi, params): sym for sym in symbols}
        for fut, sym in bounded_as_completed(futures, timeout=60,
                on_timeout=lambda ks: print(f"[institutional_scanner] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                errors.append(f"{sym}: timed out")
                continue
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
    finally:
        ex.shutdown(wait=False)

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

    # Optional: persist hits into smart_money_scan_history for later
    # hit-rate/backtesting. Best-effort -- never breaks the scan response.
    try:
        from .. import db as _oiapp_db
        for r in results:
            _oiapp_db.save_smart_money_scan_result(
                symbol=r.get("symbol"), mode="accumulation", score=r.get("score"),
                signal=r.get("signal"), extra=r,
            )
    except Exception as _persist_exc:
        print(f"[institutional_scanner] history persist skipped: {_persist_exc}")

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


def _oib_row_direction(row: dict) -> str:
    """Exact port of the removed client-side _oibRowDirection() -- counts
    bullish/bearish keyword hits across several outlook/bias fields to
    infer an overall directional read for the row."""
    vals = " ".join(str(row.get(k) or "") for k in (
        "final_seller_read", "bias", "st_outlook", "mt_outlook", "lt_outlook",
        "st_seller_bias", "mt_seller_bias", "lt_seller_bias",
    ))
    bull = len(re.findall(r"bullish|put selling|call unwind", vals, re.IGNORECASE))
    bear = len(re.findall(r"bearish|call selling|put unwind", vals, re.IGNORECASE))
    if bear > bull:
        return "bearish"
    if bull > bear:
        return "bullish"
    return "neutral"


def _oib_compute_quality(row: dict) -> dict:
    """Exact port of the removed client-side _oibComputeQuality() --
    moved server-side so the specific point weights (earnings conflict
    -35, price guard -22, UAE confirms +8, etc.) and the bucket/hard-block
    thresholds aren't readable via browser dev tools. Every weight and
    threshold below is identical to the JS version it replaced.
    """
    if not isinstance(row, dict):
        return {"score": 0, "bucket": 1, "label": "Weak", "reasons": ["missing row"]}

    def n(v, d=None):
        try:
            x = float(v)
            return x if x == x else d  # NaN check
        except (TypeError, ValueError):
            return d

    score = n(row.get("flow_confidence"), n(row.get("confidence"), 50)) or 50
    reasons = []

    direction = _oib_row_direction(row)
    action = str(row.get("should_buy_sell") or row.get("trade_action") or row.get("entry_action") or "").upper()
    uae = str(row.get("uae_confirmation") or "").lower()
    sector_align = str(row.get("sector_alignment") or "").lower()
    sector_regime = str(row.get("sector_regime") or "").lower()
    price_bias = str(row.get("price_action_bias") or "").lower()
    guard = str(row.get("price_location_guard") or "")
    ivr = n(row.get("iv_rank"), None)
    strat = str(row.get("suggested_trade_label") or row.get("suggested_strategy") or "").lower()
    st = str(row.get("st_seller_bias") or row.get("st_outlook") or "")
    mt = str(row.get("mt_seller_bias") or row.get("mt_outlook") or "")
    lt = str(row.get("lt_seller_bias") or row.get("lt_outlook") or "")

    if row.get("earnings_conflict"):
        score -= 35; reasons.append("earnings conflict")
    if guard:
        score -= 22; reasons.append("price-location guard")
    if re.search(r"WAIT|AVOID|WATCH", action) or re.match(r"^wait", strat, re.IGNORECASE):
        score -= 18; reasons.append("wait/watch action")
    if re.search(r"SELL PREMIUM|BUY OPTIONS|SELL|BUY", action) and not re.search(r"WAIT|AVOID|WATCH", action):
        score += 8; reasons.append("actionable implementation")
    if strat and not re.match(r"^wait|no strategy|n/a", strat):
        score += 5; reasons.append("strategy present")

    if "confirms" in uae:
        score += 8; reasons.append("UAE confirms")
    elif "conflict" in uae:
        score -= 12; reasons.append("UAE conflicts")
    elif "unavailable" in uae:
        score -= 4; reasons.append("UAE unavailable")

    if "confirms" in sector_align:
        score += 6; reasons.append("sector confirms")
    elif "conflict" in sector_align:
        score -= 10; reasons.append("sector conflicts")
    elif "n/a" in sector_regime or "unavailable" in sector_regime:
        score -= 3; reasons.append("sector unavailable")

    if direction == "bullish" and "bull" in price_bias:
        score += 6; reasons.append("price confirms")
    elif direction == "bearish" and "bear" in price_bias:
        score += 6; reasons.append("price confirms")
    elif (direction == "bullish" and "bear" in price_bias) or (direction == "bearish" and "bull" in price_bias):
        score -= 12; reasons.append("price conflicts")

    if ivr is None:
        score -= 4; reasons.append("IVR unavailable")
    elif (re.search(r"SELL PREMIUM", action) or re.search(r"PS|CS|IC", str(row.get("suggested_strategy_code") or ""))) and ivr >= 50:
        score += 5; reasons.append("IV supports premium sale")
    elif re.search(r"BUY OPTIONS", action) and ivr <= 35:
        score += 5; reasons.append("low IV supports debit")

    dir_word = "Bullish" if direction == "bullish" else "Bearish" if direction == "bearish" else ""
    if dir_word and dir_word in st and dir_word in mt and dir_word in lt:
        score += 8; reasons.append("ST/MT/LT aligned")
    elif dir_word and (dir_word in mt or dir_word in lt):
        score += 3; reasons.append("partial timeframe alignment")

    score = max(0, min(100, round(score)))
    bucket = 5 if score >= 85 else 4 if score >= 70 else 3 if score >= 55 else 2 if score >= 40 else 1

    # Hard blocks cannot be shown as tradable/best. They can still be
    # visible at lower slider levels.
    if (row.get("earnings_conflict") or row.get("price_location_guard")
            or re.search(r"WAIT|AVOID", str(row.get("should_buy_sell") or row.get("trade_action") or row.get("entry_action") or "").upper())
            or re.match(r"^wait", str(row.get("suggested_trade_label") or ""), re.IGNORECASE)):
        bucket = min(bucket, 3)

    label = {5: "Best", 4: "Tradable", 3: "Setup", 2: "Watch"}.get(bucket, "Weak")
    return {"score": score, "bucket": bucket, "label": label, "reasons": reasons}


@inst_bp.route("/batch_quality", methods=["POST"])
def batch_quality():
    """Takes a list of OI Buildup scanner rows and returns a matching
    list of {score, bucket, label, reasons} -- batched (one request for
    the whole table) rather than per-row, since the frontend scores an
    entire scan result set at once for filtering/sorting, not one row
    in isolation.
    """
    d = request.get_json(force=True) or {}
    rows = d.get("rows") or []
    if not isinstance(rows, list):
        return jsonify({"error": "rows must be a list"}), 400
    results = [_oib_compute_quality(r if isinstance(r, dict) else {}) for r in rows]
    return jsonify({"results": results})

