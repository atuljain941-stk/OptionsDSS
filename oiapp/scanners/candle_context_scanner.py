"""
candle_context_scanner.py — Strong Candle Context Scanner (v2)

Answers the question: "I see a strong candle with a big volume bar --
is it actually meaningful, and what's the setup?"

v2 rewrite: switched from the heavy _symbol_ctx (which fetches live
options history, earnings info, sector data, and a flow snapshot -- none
of which this scanner's scoring actually uses) to a lightweight
price_cache-only context, matching institutional_scanner.py's pattern.
The heavy version was causing widespread timeouts on real watchlist
scans (60s timeout, 6 workers, ~198 symbols each needing several live
fetches -- most symbols simply couldn't finish in time). RSI is now
computed directly here (standard Wilder's smoothing) instead of reading
it off the heavy ctx.

FILTERING vs SCORING -- this is the core design rule of this scanner:
  - The ONLY hard filter is the base candle+volume check
    (_detect_current_candle). If a bar qualifies as a strong candle with
    real volume, the symbol IS returned, always, regardless of anything
    else. `min_price` is the only other hard filter (a basic penny-stock
    guard) -- separate from the candle logic entirely.
  - RSI zone, S/R proximity/strength, TouchCount freshness, prior
    consolidation, and prior trend NEVER cause a symbol to be excluded.
    They only add (or fail to add) points to the score. A symbol with a
    qualifying candle but no nearby S/R zone still comes back -- just
    with fewer points on that one dimension.
  - `min_score` is a separate, final, user-controlled display filter
    (default 0 -- show everything that has a qualifying candle, sorted
    by score, and let the person decide what's worth their attention).

Everything after the base candle filter (context/scoring) still reuses
the existing, already-tested scanner_builder primitives via
_parse_query/_eval against a minimal ctx -- DistanceFromResistance/
DistanceFromSupport, ResistanceStrength/SupportStrength, Resistance/
Support (raw level, fed into TouchCount), ATRCompression/
RangeCompression/VolumeDryup, and RegSlopePct all run through the real
evaluator, which only needs ctx["timeframes"]["1d"]["series"] -- no
options/earnings/sector data required for any of them (confirmed by
direct testing).
"""
import sqlite3
import json
import math as _math
from datetime import datetime
from flask import Blueprint, jsonify, request

candle_ctx_bp = Blueprint("candle_ctx_bp", __name__, url_prefix="/scanner/candle-context")
from ..config import DB_PATH as _OIAPP_DB_PATH
DB_PATH = _OIAPP_DB_PATH

try:
    from .scanner_builder import _parse_query as _sb_parse_query, _eval as _sb_eval
except Exception:
    _sb_parse_query = None
    _sb_eval = None

# ── Default scoring parameters ─────────────────────────────────────────────
DEFAULTS = {
    "min_score":          0.0,   # 0 = show EVERY symbol with a qualifying candle, sorted by score.
    "min_confluence":     0,     # 0 = no confluence filter. This is a SEPARATE, stricter screen than min_score -- e.g. min_confluence=4 requires at least 4 of the applicable dimensions to actually agree, regardless of total score.
                                  # Raise this only if you want to hide low-scoring matches -- it is
                                  # a display filter, not part of what counts as a "match".
    "side_mode":          "both",  # "bull", "bear", or "both"
    "move_mult":          1.5,   # candle's abs %-change must be >= this x its own EMA(avgBars) baseline
    "vol_mult":           1.5,   # candle's volume must be >= this x EMA(volume,20)
    "avg_change_bars":    20,    # averaging window for the move-multiple baseline
    "sr_period":          20,    # S/R zone lookback period (DistanceFromResistance/Support etc.)
    "touch_tolerance_pct":1.0,   # % band around the level for TouchCount
    "touch_bars":         60,    # lookback window for TouchCount
    "compression_period": 14,    # ATRCompression period
    "range_compression_period": 20,  # RangeCompression / VolumeDryup period
    "trend_bars":         20,    # prior-trend slope window, measured just before the candle
    "min_price":          3.0,   # the ONLY other hard filter besides the candle itself
    "lookback_days":      1,     # check the last N bars (most recent first); first match wins and its date is reported
    "timeframe":          "daily",  # "daily" or "weekly" -- weekly resamples history to Mon-Fri bars first; every scoring dimension runs unchanged on top of whichever bar size is selected
    "oi_min_wall_strength_pct": 15.0,  # % of that side's total OI concentrated at the wall strike, to "count"
    "oi_max_wall_distance_pct": 5.0,   # wall must be within this % of price to be relevant, not deep-OTM noise
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


def _safe(v):
    if isinstance(v, float) and (_math.isnan(v) or _math.isinf(v)):
        return None
    return v


def _get_price_history(symbol, min_days=250):
    """Reads OHLCV directly from price_cache -- same rationale as
    institutional_scanner.py: price_cache already holds years of daily
    history, no reason to live-fetch on every scan. This is the fix for
    the timeout problem: this is a single fast local SQLite query per
    symbol instead of several live network calls."""
    con = _conn()
    try:
        rows = con.execute(
            """SELECT date, open, high, low, close, volume
               FROM price_cache WHERE symbol=? ORDER BY date DESC LIMIT ?""",
            (symbol.upper().strip(), min_days),
        ).fetchall()
    finally:
        con.close()
    if not rows or len(rows) < 40:
        return None
    rows = list(reversed(rows))
    import pandas as pd
    return {
        "date": [r["date"] for r in rows],
        "open": pd.Series([r["open"] for r in rows], dtype=float),
        "high": pd.Series([r["high"] for r in rows], dtype=float),
        "low": pd.Series([r["low"] for r in rows], dtype=float),
        "close": pd.Series([r["close"] for r in rows], dtype=float),
        "volume": pd.Series([r["volume"] for r in rows], dtype=float),
    }


def _compute_rsi(closes, period=14):
    """Standard Wilder's-smoothed RSI, computed locally -- this scanner
    no longer depends on the heavy ctx builder for this."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def _ev(expr, ctx):
    """Evaluate a scanner_builder expression string against ctx. Returns
    None on any failure (missing data, no S/R zone found, etc.) rather
    than raising -- every caller here already treats None as "no signal
    on this dimension" (a SCORING gap, never a reason to exclude the
    symbol -- see module docstring)."""
    if _sb_parse_query is None or _sb_eval is None:
        return None
    try:
        return _sb_eval(_sb_parse_query(expr), ctx, shift=0, tf_default="1d")
    except Exception:
        return None


def _detect_current_candle(ctx, p):
    """The ONLY hard filter in this scanner. Returns None if the last bar
    doesn't qualify as a strong candle in either direction (or the
    requested side), else a dict with the details. See module docstring
    for why this can't reuse StrongBullCandle/StrongCandleAge as-is
    (those exclude the current bar by design, for retest scans)."""
    series = ctx.get("timeframes", {}).get("1d", {}).get("series", {})
    o, h, l, c, v = series.get("open"), series.get("high"), series.get("low"), series.get("close"), series.get("volume")
    if o is None or h is None or l is None or c is None or v is None:
        return None
    n = min(len(o), len(h), len(l), len(c), len(v))
    if n < max(int(p.get("avg_change_bars", 20)), 25) + 2:
        return None

    change_pct = c.pct_change() * 100
    avg_abs_change = change_pct.abs().ewm(span=int(p.get("avg_change_bars", 20)), adjust=False, min_periods=5).mean()
    vol_avg = v.ewm(span=20, adjust=False, min_periods=5).mean()

    last = n - 1
    cur_change = change_pct.iloc[last]
    cur_avg_abs = avg_abs_change.iloc[last - 1] if last >= 1 else None
    cur_vol = v.iloc[last]
    cur_vol_avg = vol_avg.iloc[last - 1] if last >= 1 else None
    rng = float(h.iloc[last]) - float(l.iloc[last])
    body = abs(float(c.iloc[last]) - float(o.iloc[last]))
    body_pct = (body / rng * 100.0) if rng > 0 else 0.0

    if any(x is None or (isinstance(x, float) and _math.isnan(x)) for x in [cur_change, cur_avg_abs, cur_vol_avg]):
        return None
    if cur_avg_abs <= 0 or cur_vol_avg <= 0:
        return None

    move_mult_actual = abs(cur_change) / cur_avg_abs
    vol_mult_actual = cur_vol / cur_vol_avg
    is_strong_move = move_mult_actual >= float(p.get("move_mult", 1.5))
    is_strong_vol = vol_mult_actual >= float(p.get("vol_mult", 1.5))

    # ── THE ONLY TWO GATE CONDITIONS ─────────────────────────────────────
    # Per explicit instruction: strong candle (move) + good volume are the
    # ONLY filter. body_pct is reported and scored (see _score_symbol) but
    # does NOT gate here -- a big move with a smaller body still comes
    # back, just scores lower on the "conviction" dimension.
    if not (is_strong_move and is_strong_vol):
        return None
    # ─────────────────────────────────────────────────────────────────

    side = "bull" if cur_change > 0 else "bear"
    side_mode = p.get("side_mode", "both")
    if side_mode != "both" and side_mode != side:
        return None

    return {
        "side": side,
        "change_pct": round(float(cur_change), 2),
        "move_multiple": round(float(move_mult_actual), 2),
        "vol_multiple": round(float(vol_mult_actual), 2),
        "body_pct": round(float(body_pct), 1),
    }


def _resample_to_weekly(full_hist):
    """Aggregates the daily dict-of-Series shape into weekly bars
    (Mon-Fri grouped, weekly date = the last trading day in each
    week), keeping the exact same dict shape (+ "date" list) so every
    downstream function -- candle detection, RSI, S/R, OI wall, all of
    it -- works completely unchanged, just operating on weekly bars
    instead of daily ones. This is what makes "Timeframe: Weekly"
    possible without touching the actual scoring logic at all.
    """
    import pandas as pd
    dates = pd.to_datetime(full_hist["date"])
    df = pd.DataFrame({
        "date": dates, "open": full_hist["open"].values, "high": full_hist["high"].values,
        "low": full_hist["low"].values, "close": full_hist["close"].values, "volume": full_hist["volume"].values,
    })
    df = df.set_index("date")
    weekly = df.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    return {
        "date": [d.strftime("%Y-%m-%d") for d in weekly.index],
        "open": weekly["open"].reset_index(drop=True),
        "high": weekly["high"].reset_index(drop=True),
        "low": weekly["low"].reset_index(drop=True),
        "close": weekly["close"].reset_index(drop=True),
        "volume": weekly["volume"].reset_index(drop=True),
    }


def _score_symbol(sym, p):
    """Wrapper: fetches full history once, then checks the last
    lookback_days bars (most recent first) for a qualifying candle --
    the first (most recent) day that matches wins, and its date is
    reported so results say WHEN the condition fired, not just that
    it fired at some point in the window. Always returns a dict (never
    bare None) so a 0-signal scan can still report WHY -- how many
    symbol-days had a candle pass the hard gate at all, and the best
    score seen even if it never cleared min_score, instead of leaving
    "0 signals" as an unexplained black box."""
    full_hist = _get_price_history(sym, min_days=250)
    if full_hist is None:
        return {"matched": False, "symbol": sym, "reason": "no cached price history", "candle_gate_passes": 0, "best_score_seen": None}
    if str(p.get("timeframe", "daily")).lower() == "weekly":
        full_hist = _resample_to_weekly(full_hist)
        if full_hist is None or len(full_hist["close"]) < 15:
            return {"matched": False, "symbol": sym, "reason": "not enough weekly bars after resampling", "candle_gate_passes": 0, "best_score_seen": None}
    lookback_days = max(1, int(p.get("lookback_days", 1)))
    n = len(full_hist["close"])  # _get_price_history returns a dict of equal-length Series (+ a plain "date" list) -- NOT a DataFrame, so len(full_hist) itself would wrongly give the key count (5), not the row count
    candle_gate_passes = 0
    best_score_seen = None
    days_checked = 0
    min_bars_needed = max(int(p.get("avg_change_bars", 20)), 25) + 2  # same floor _detect_current_candle itself requires -- was a flat 50 before, which only worked for daily bars and silently blocked every weekly-mode check (only ~50 weekly bars exist total from 250 days of history)
    for back in range(lookback_days):
        end_idx = n - 1 - back
        if end_idx < min_bars_needed:  # not enough history left to score reliably this far back
            break
        days_checked += 1
        hist_slice = {
            "date": full_hist["date"][:end_idx + 1],
            "open": full_hist["open"].iloc[:end_idx + 1],
            "high": full_hist["high"].iloc[:end_idx + 1],
            "low": full_hist["low"].iloc[:end_idx + 1],
            "close": full_hist["close"].iloc[:end_idx + 1],
            "volume": full_hist["volume"].iloc[:end_idx + 1],
        }
        result, gate_passed, raw_score = _score_symbol_asof(sym, p, hist_slice)
        if gate_passed:
            candle_gate_passes += 1
            if raw_score is not None and (best_score_seen is None or raw_score > best_score_seen):
                best_score_seen = raw_score
        if result is not None:
            matched_dt = hist_slice["date"][-1]
            result["matched_date"] = str(matched_dt)
            result["days_ago"] = back
            result["matched"] = True
            return result
    return {"matched": False, "symbol": sym, "days_checked": days_checked,
            "candle_gate_passes": candle_gate_passes, "best_score_seen": best_score_seen}


def _score_symbol_asof(sym, p, hist):
    """Same scoring logic as before, now operating on whatever history
    slice the caller passes in -- ENDS at the day being evaluated, so
    every dimension (RSI, S/R, OI wall, etc.) is computed as-of that
    day, not with future bars leaking in. Returns
    (result_or_None, candle_gate_passed_bool, raw_score_or_None) so
    the caller can distinguish "candle never qualified at all" from
    "candle qualified but score fell below min_score"."""

    price = float(hist["close"].iloc[-1])
    if price < float(p.get("min_price", 3.0)):
        return None, False, None

    ctx = {"timeframes": {"1d": {"series": hist}}, "price": price}

    # ── THE ONLY HARD FILTER ────────────────────────────────────────────
    candle = _detect_current_candle(ctx, p)
    if candle is None:
        return None, False, None
    side = candle["side"]
    # ─────────────────────────────────────────────────────────────────

    rsi_series = _compute_rsi(hist["close"])
    rsi = float(rsi_series.iloc[-1]) if not _math.isnan(rsi_series.iloc[-1]) else None

    sr_period = int(p.get("sr_period", 20))
    tol = float(p.get("touch_tolerance_pct", 1.0)) / 100.0
    touch_bars = int(p.get("touch_bars", 60))
    trend_bars = int(p.get("trend_bars", 20))
    comp_period = int(p.get("compression_period", 14))
    range_comp_period = int(p.get("range_compression_period", 20))

    if side == "bull":
        level = _ev(f'Resistance({sr_period},"1d")', ctx)
        distance = _ev(f'DistanceFromResistance({sr_period},"1d")', ctx)
        strength = _ev(f'ResistanceStrength({sr_period},"1d")', ctx)
    else:
        level = _ev(f'Support({sr_period},"1d")', ctx)
        distance = _ev(f'DistanceFromSupport({sr_period},"1d")', ctx)
        strength = _ev(f'SupportStrength({sr_period},"1d")', ctx)

    touch_count = _ev(f'TouchCount({level},{tol},{touch_bars},"1d")', ctx) if level is not None else None
    atr_compression = _ev(f'ATRCompression({comp_period},"1d")', ctx)
    range_compression = _ev(f'RangeCompression({range_comp_period},"1d")', ctx)
    volume_dryup = _ev(f'VolumeDryup({range_comp_period},"1d")', ctx)
    prior_slope = _ev(f'RegSlopePct(close,{trend_bars},"1d")', ctx)

    # ── SCORING (0-10) -- every point below is additive/informational
    # only; nothing here can cause the symbol to be excluded. ──────────
    points = {"candle": 0.0, "rsi": 0.0, "sr": 0.0, "touch": 0.0, "compression": 0.0, "trend": 0.0, "oi_wall": 0.0}
    reasons = []

    mm = candle["move_multiple"]
    if mm >= 3.0:
        points["candle"] = 1.5; reasons.append(f"Very large move ({mm}x normal)")
    elif mm >= 2.0:
        points["candle"] = 1.1; reasons.append(f"Large move ({mm}x normal)")
    else:
        points["candle"] = 0.6; reasons.append(f"Move {mm}x normal")
    bp = candle["body_pct"]
    if bp >= 70:
        points["candle"] += 0.5; reasons.append(f"Strong real body ({bp}% of range)")
    elif bp >= 50:
        points["candle"] += 0.3; reasons.append(f"Decent body ({bp}% of range)")
    else:
        reasons.append(f"Small body relative to range ({bp}%) -- more wick than conviction")

    if rsi is not None:
        if side == "bull":
            if rsi < 35:
                points["rsi"] = 2.0; reasons.append(f"Bull candle out of oversold (RSI {rsi:.0f})")
            elif rsi < 65:
                points["rsi"] = 1.2; reasons.append(f"RSI neutral ({rsi:.0f})")
            elif rsi < 75:
                points["rsi"] = 0.5; reasons.append(f"RSI already elevated ({rsi:.0f}) -- some chase risk")
            else:
                reasons.append(f"RSI overbought ({rsi:.0f}) -- high chase risk")
        else:
            if rsi > 65:
                points["rsi"] = 2.0; reasons.append(f"Bear candle out of overbought (RSI {rsi:.0f})")
            elif rsi > 35:
                points["rsi"] = 1.2; reasons.append(f"RSI neutral ({rsi:.0f})")
            elif rsi > 25:
                points["rsi"] = 0.5; reasons.append(f"RSI already depressed ({rsi:.0f}) -- some chase risk")
            else:
                reasons.append(f"RSI oversold ({rsi:.0f}) -- high chase risk")

    level_word = "resistance" if side == "bull" else "support"
    if distance is not None and strength is not None:
        if distance <= 2.0 and strength >= 50:
            points["sr"] = 2.5; reasons.append(f"Right at a strong {level_word} zone (strength {strength:.0f})")
        elif distance <= 2.0:
            points["sr"] = 1.5; reasons.append(f"Right at a {level_word} zone (moderate strength {strength:.0f})")
        elif distance <= 5.0:
            points["sr"] = 1.0; reasons.append(f"Near a {level_word} zone ({distance:.1f}% away)")
        else:
            reasons.append(f"No nearby {level_word} zone ({distance:.1f}% away)")
    elif distance is not None and distance <= 3.0:
        points["sr"] = 0.8; reasons.append(f"Near a {level_word} zone ({distance:.1f}% away)")
    else:
        reasons.append("No S/R zone detected")

    if touch_count is not None:
        tc = int(touch_count)
        if 2 <= tc <= 4:
            points["touch"] = 1.5; reasons.append(f"Level tested {tc}x -- validated, not exhausted")
        elif tc in (1, 5):
            points["touch"] = 0.8; reasons.append(f"Level tested {tc}x")
        elif tc == 0:
            reasons.append("Fresh/untested level")
        else:
            reasons.append(f"Level tested {tc}x -- heavily faded zone")

    comp_scores = [x for x in [atr_compression, range_compression, volume_dryup] if x is not None]
    avg_comp = (sum(comp_scores) / len(comp_scores)) if comp_scores else None
    if avg_comp is not None:
        if avg_comp <= 40:
            points["compression"] = 1.5; reasons.append("Broke out of a tight consolidation")
        elif avg_comp <= 65:
            points["compression"] = 0.8; reasons.append("Some prior consolidation")
        else:
            reasons.append("No meaningful consolidation beforehand")

    trend_word = None
    if prior_slope is not None:
        if abs(prior_slope) >= 1.0:
            points["trend"] = 0.5
            trend_word = "uptrend" if prior_slope > 0 else "downtrend"
            agrees = (trend_word == "uptrend" and side == "bull") or (trend_word == "downtrend" and side == "bear")
            reasons.append(f"Prior {trend_word} ({'continuation' if agrees else 'reversal'} setup)")
        else:
            trend_word = "sideways"
            reasons.append("Prior action was sideways/choppy")

    # ── OI WALLS (merged in) -- additive dimension like everything
    # else here: adds points/reasons when data exists and a wall
    # qualifies, contributes nothing and excludes nobody when it
    # doesn't (this table only covers symbols actively being
    # archived, a subset of any watchlist, so most scans will have
    # this be None for most symbols -- that's expected, not a bug).
    points["oi_wall"] = 0.0
    oi_wall = None
    try:
        from .oi_wall_backtest import get_oi_wall_snapshot
        as_of_str = str(hist["date"][-1])[:10]
        oi_wall = get_oi_wall_snapshot(
            sym, as_of_date=as_of_str,
            min_wall_strength_pct=float(p.get("oi_min_wall_strength_pct", 15.0)),
            max_wall_distance_pct=float(p.get("oi_max_wall_distance_pct", 5.0)),
        )
    except Exception:
        oi_wall = None

    if oi_wall:
        near_qualifying_wall = None
        near_dist = None
        if side == "bull" and oi_wall.get("call_wall_qualifies"):
            near_qualifying_wall, near_dist = "call", oi_wall.get("call_wall_distance_pct")
        elif side == "bear" and oi_wall.get("put_wall_qualifies"):
            near_qualifying_wall, near_dist = "put", oi_wall.get("put_wall_distance_pct")
        elif oi_wall.get("call_wall_qualifies"):
            near_qualifying_wall, near_dist = "call", oi_wall.get("call_wall_distance_pct")
        elif oi_wall.get("put_wall_qualifies"):
            near_qualifying_wall, near_dist = "put", oi_wall.get("put_wall_distance_pct")

        if near_qualifying_wall and near_dist is not None:
            if near_dist <= 1.5:
                points["oi_wall"] = 1.2
                reasons.append(f"Sitting right at a strong OI {near_qualifying_wall} wall ({near_dist:.1f}% away)")
            elif near_dist <= 3.0:
                points["oi_wall"] = 0.8
                reasons.append(f"Near a qualifying OI {near_qualifying_wall} wall ({near_dist:.1f}% away)")
            else:
                points["oi_wall"] = 0.4
                reasons.append(f"OI {near_qualifying_wall} wall in range but not close ({near_dist:.1f}% away)")
            if oi_wall.get("recommended_strategy"):
                reasons.append(f"OI wall structure suggests a {oi_wall['recommended_strategy']} setup ({oi_wall['dte_days']}d out)")
        else:
            reasons.append("Have OI history for this symbol, but no wall qualifies within strength/distance filters")

    score = round(min(10, sum(points.values())), 1)

    # min_score is a DISPLAY filter only, applied last, after every
    # scoring dimension has already been computed -- default 0 means
    # this line never actually excludes anything unless the user raises it.
    if score < float(p.get("min_score", 0.0)):
        return None, True, score

    from .confluence import compute_confluence
    confluence = compute_confluence({
        "candle": points["candle"] >= 1.1,
        "rsi": points["rsi"] >= 1.2,
        "sr": points["sr"] >= 1.0,
        "touch": points["touch"] > 0,
        "compression": points["compression"] > 0,
        "trend": points["trend"] > 0,
        "oi_wall": None if oi_wall is None else (points["oi_wall"] > 0),
    })
    min_confluence = int(p.get("min_confluence", 0) or 0)
    if min_confluence > 0 and confluence["agreeing"] < min_confluence:
        return None, True, score

    signal = "🔥 Strong" if score >= 7.5 else "✅ Moderate" if score >= 6 else "👀 Watch" if score >= 3 else "· Minimal"

    return {
        "symbol": sym, "price": _safe(round(price, 2)), "score": score, "signal": signal,
        "confluence": confluence,
        "side": side, "change_pct": candle["change_pct"], "move_multiple": candle["move_multiple"],
        "vol_multiple": candle["vol_multiple"], "body_pct": candle["body_pct"],
        "rsi": _safe(round(rsi, 1)) if rsi is not None else None,
        "sr_level": _safe(round(float(level), 2)) if level is not None else None,
        "sr_distance_pct": _safe(round(float(distance), 2)) if distance is not None else None,
        "sr_strength": _safe(round(float(strength), 1)) if strength is not None else None,
        "sr_touch_count": int(touch_count) if touch_count is not None else None,
        "compression_avg": _safe(round(avg_comp, 1)) if avg_comp is not None else None,
        "prior_trend": trend_word,
        "prior_slope_pct": _safe(round(float(prior_slope), 2)) if prior_slope is not None else None,
        "oi_call_wall_strike": oi_wall.get("call_wall_strike") if oi_wall else None,
        "oi_call_wall_strength_pct": oi_wall.get("call_wall_strength_pct") if oi_wall else None,
        "oi_call_wall_distance_pct": oi_wall.get("call_wall_distance_pct") if oi_wall else None,
        "oi_put_wall_strike": oi_wall.get("put_wall_strike") if oi_wall else None,
        "oi_put_wall_strength_pct": oi_wall.get("put_wall_strength_pct") if oi_wall else None,
        "oi_put_wall_distance_pct": oi_wall.get("put_wall_distance_pct") if oi_wall else None,
        "oi_recommended_strategy": oi_wall.get("recommended_strategy") if oi_wall else None,
        "oi_wall_dte_days": oi_wall.get("dte_days") if oi_wall else None,
        "points": {k: round(v, 1) for k, v in points.items()},  # per-dimension point contributions
        "reasons": reasons,
    }, True, score


def run_candle_context_scan(wl_id, params):
    """Core scan logic, no Flask dependency -- callable from the route
    AND from a scheduled job (see register_scheduled_scan_job below).
    Returns a plain dict; the route wraps it in jsonify()."""
    from concurrent.futures import ThreadPoolExecutor
    symbols = _get_symbols(wl_id)
    if not symbols:
        return {"results": [], "count": 0, "error": "No symbols in watchlist"}

    wl_name = "All Symbols"
    if wl_id:
        try:
            con = _conn()
            row = con.execute("SELECT name FROM watchlists WHERE id=?", (wl_id,)).fetchone()
            con.close()
            if row: wl_name = row[0]
        except Exception:
            pass

    results, errors = [], []
    diag_candle_gate_passes = 0
    diag_days_checked = 0
    diag_no_price_history = 0
    diag_best_score_seen = None
    diag_best_score_symbol = None
    # price_cache reads are fast local SQLite queries now (no more live
    # fetches), so a much smaller timeout is appropriate.
    ex = ThreadPoolExecutor(max_workers=12)
    try:
        futures = {ex.submit(_score_symbol, sym, params): sym for sym in symbols}
        try:
            from ..services.bounded_wait import bounded_as_completed
            iterator = bounded_as_completed(futures, timeout=25,
                    on_timeout=lambda ks: print(f"[candle_context_scanner] {len(ks)} symbol(s) timed out: {ks[:20]}"))
        except Exception:
            from concurrent.futures import as_completed
            iterator = ((f, futures[f]) for f in as_completed(futures, timeout=25))
        for fut, sym in iterator:
            if fut is None:
                errors.append(f"{sym}: timed out")
                continue
            try:
                r = fut.result()
                if r and r.get("matched"):
                    r["watchlist"] = wl_name
                    results.append(r)
                elif r:
                    diag_candle_gate_passes += r.get("candle_gate_passes") or 0
                    diag_days_checked += r.get("days_checked") or 0
                    if r.get("reason") == "no cached price history":
                        diag_no_price_history += 1
                    bss = r.get("best_score_seen")
                    if bss is not None and (diag_best_score_seen is None or bss > diag_best_score_seen):
                        diag_best_score_seen = bss
                        diag_best_score_symbol = sym
            except Exception as e:
                errors.append(f"{sym}: {str(e)[:60]}")
    finally:
        ex.shutdown(wait=False)

    results.sort(key=lambda x: x["score"], reverse=True)
    completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        con = _conn()
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES ('candle_context_scan',?,?)",
                    (json.dumps(results), completed_at))
        con.commit(); con.close()
    except Exception:
        pass

    try:
        from .. import db as _oiapp_db
        for r in results:
            _oiapp_db.save_smart_money_scan_result(
                symbol=r.get("symbol"), mode="candle_context", score=r.get("score"),
                signal=r.get("signal"), extra=r,
            )
    except Exception as _persist_exc:
        print(f"[candle_context_scanner] history persist skipped: {_persist_exc}")

    return {"results": results, "count": len(results),
            "total_scanned": len(symbols), "completed_at": completed_at,
            "watchlist": wl_name,
            "params_used": params, "errors": len(errors), "error_sample": errors[:5],
            "diagnostics": {
                "symbol_days_checked": diag_days_checked,
                "symbols_with_no_cached_price_history": diag_no_price_history,
                "candle_gate_passes": diag_candle_gate_passes,
                "best_score_seen": diag_best_score_seen,
                "best_score_symbol": diag_best_score_symbol,
                "min_score_threshold": params.get("min_score", 0.0),
            }}


def run_scheduled_candle_context_scan():
    """Zero-arg entrypoint for the Scheduler Hub 'Run now' button and
    the actual scheduled trigger -- uses saved DEFAULTS and the
    preferred/default watchlist (no per-run params UI; adjust DEFAULTS
    or run manually from the Candle Context tab for a one-off custom
    scan)."""
    try:
        con = _conn()
        row = con.execute("SELECT id FROM watchlists WHERE is_default=1 LIMIT 1").fetchone()
        con.close()
        wl_id = row[0] if row else None
    except Exception:
        wl_id = None
    result = run_candle_context_scan(wl_id, dict(DEFAULTS))
    return f"{result.get('count', 0)} signal(s) from {result.get('total_scanned', 0)} scanned"


_scheduled_job_registered = False


def register_scheduled_scan_job():
    global _scheduled_job_registered
    if _scheduled_job_registered:
        return False
    _scheduled_job_registered = True
    from ..services.job_registry import register_job
    register_job(
        "candle_context_scheduled_scan", "Candle Context Scheduled Scan",
        "Runs the Candle Context scanner (strong-candle + OI-wall scoring) on a schedule "
        "using the saved default criteria and default watchlist -- results are cached the "
        "same way a manual scan is (visible via 'cached' results, and persisted to scan history). "
        "Set to a single weekday in Scheduler Hub for a weekly cadence, or leave weekdays "
        "unset for daily.",
        kind="time", default_schedule={"times": ["08:00"], "weekdays": None},
        group="Scanners", run_now_fn=run_scheduled_candle_context_scan,
    )
    return True


@candle_ctx_bp.route("/scan", methods=["GET", "POST"])
def candle_context_scan():
    wl_id = request.args.get("watchlist_id", None, type=int)

    params = dict(DEFAULTS)
    body = request.get_json(silent=True) or {}
    for k in DEFAULTS:
        if k in body:
            params[k] = type(DEFAULTS[k])(body[k]) if not isinstance(DEFAULTS[k], str) else str(body[k])
        elif request.args.get(k) is not None:
            try:
                params[k] = type(DEFAULTS[k])(request.args.get(k)) if not isinstance(DEFAULTS[k], str) else request.args.get(k)
            except Exception:
                pass

    return jsonify(run_candle_context_scan(wl_id, params))

@candle_ctx_bp.route("/scan_cached")
def candle_context_scan_cached():
    try:
        con = _conn()
        row = con.execute("SELECT value, updated FROM app_cache WHERE key='candle_context_scan'").fetchone()
        con.close()
        if row:
            data = json.loads(row[0])
            return jsonify({"results": data, "count": len(data), "completed_at": row[1], "from_cache": True})
        return jsonify({"results": [], "count": 0, "from_cache": True})
    except Exception:
        return jsonify({"results": [], "count": 0})


@candle_ctx_bp.route("/defaults")
def get_defaults():
    return jsonify(DEFAULTS)
