"""
trend_divergence_scanner.py -- "Trend + Divergence" Scanner (v1)

New page answering: across a watchlist and a chosen timeframe, which
symbols just printed a TB (bull trend flip), TS (bear trend flip), or one
of the four divergence types (classic bull/bear, hidden bull/bear) within
the last N days -- and for each hit, what would entry/stop/target have
been, and did it hit target, hit stop, or is it still open.

DESIGN, following trade_setup_scanner.py / candle_context_scanner.py's
established pattern for this codebase:
  - Reuses the already-tested internals directly instead of re-deriving
    them: _history() (the real cached/backfill-aware history loader,
    NOT the raw yfinance-only _fetch_history_cached), _last_swing_pivot(),
    _detect_bos(), _ema()/_rsi()/_macd() from scanner_builder.py, and
    _watchlists()/_get_symbols() from trade_setup_scanner.py.
  - TB/TS structure gate reuses _detect_bos() exactly as the DSL's
    BosBullish/BosBearish primitives do internally -- not a separate
    reimplementation that could drift from what those primitives mean
    elsewhere in the app.
  - Divergence reuses _last_swing_pivot() called TWICE per bar (current
    pivot, then the prior one via shift + age + 1) -- this is the EXACT
    technique _detect_choch()/_detect_bos() already use internally to
    find the pivot-before-the-last-pivot. Because this runs as real
    Python against the full series (not the query-string DSL, which
    only exposes the single most recent pivot), this gets the prior
    pivot's actual index -- an exact comparison, not the Shift(expr,
    age+3) approximation used in the scanner-query version of these
    conditions from the prior conversation.
  - The tight single-candle stop (nearest prior candle with a higher
    high / lower low than the signal candle) has no scanner_builder
    primitive equivalent (flagged as a known gap when the query-string
    version of this was built) -- implemented directly here as a plain
    backward scan over the DataFrame, since this module has raw array
    access and doesn't need to go through the DSL at all.

KNOWN GAPS / NOT YET VERIFIED END-TO-END:
  - This sandbox has no network access to Yahoo Finance and no copy of
    your real price_cache DB, so this has NOT been run against real
    market data. It has been verified via: py_compile, and a synthetic
    OHLCV smoke test (synthetic_selftest() at the bottom of this file)
    confirming TB/TS/divergence detection and stop/target/outcome logic
    fire correctly on constructed data with a known trend+pullback
    shape. Recommend running one real scan through the UI on a small
    watchlist before trusting results broadly -- same caution your own
    SCANNER_LET_BINDINGS_V1.md flagged for the `let` feature.
  - 5m/15m timeframes have no persistent local cache in this codebase
    (only 1h/2h/4h/1d/1w/1m do, per _history()'s routing) -- selecting
    5m/15m here means a live yfinance call per symbol per scan, capped
    at ~59 days of history by Yahoo's own limits. Fine for a quick scan,
    slow for a large watchlist, and marginal for RSIDiff90's ~540-bar
    trust threshold. 30m is not a supported timeframe at all (not in
    scanner_builder.TIMEFRAMES) -- would need adding to the resample
    pipeline first, a separate task.
"""
import math
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta

import pandas as pd
from flask import Blueprint, jsonify, request, render_template

trend_div_bp = Blueprint("trend_div_bp", __name__, url_prefix="/scanner/trend-divergence")

from ..config import DB_PATH as _OIAPP_DB_PATH
DB_PATH = _OIAPP_DB_PATH

try:
    from .scanner_builder import (
        _history as _sb_history,
        _last_swing_pivot as _sb_last_swing_pivot,
        _detect_bos as _sb_detect_bos,
        _ema as _sb_ema,
        _rsi as _sb_rsi,
        _macd as _sb_macd,
        _atr_series as _sb_atr_series,
        _normalize_tf as _sb_normalize_tf,
        TIMEFRAMES as _SB_TIMEFRAMES,
    )
except Exception:
    _sb_history = None
    _sb_last_swing_pivot = None
    _sb_detect_bos = None
    _sb_ema = None
    _sb_rsi = None
    _sb_macd = None
    _sb_atr_series = None
    _sb_normalize_tf = None
    _SB_TIMEFRAMES = ["5m", "15m", "1h", "2h", "4h", "1d", "1w", "1m"]

# Reuse the one watchlist source the rest of the app already agrees on.
try:
    from .trade_setup_scanner import _watchlists as _ts_watchlists, _get_symbols as _ts_get_symbols
except Exception:
    _ts_watchlists = None
    _ts_get_symbols = None

# Timeframes that have a persistent local DB cache (fast, deep history).
# 5m/15m fall back to a live yfinance call each time (see module docstring).
CACHED_TIMEFRAMES = {"1h", "2h", "4h", "1d", "1w", "1m"}

DEFAULT_PARAMS = {
    "timeframe": "1h",
    "lookback_days": 30,
    "rr_multiple": 3.0,
    "atr_stop_mult": 1.5,          # fallback stop distance if no tight-candle stop found
    "tight_stop_max_lookback": 50, # bars to search backward for the tight single-candle stop
    "struct_lookback": 60,
    "struct_left": 3,              # matches the Pine strategy's structurePivotLeft default
    "struct_right": 2,             # matches structurePivotRight default
    "div_lookback": 60,
    "div_left": 2,
    "div_right": 2,
    "max_shifts_per_symbol": 300,  # hard cap on how far back the backward-shift scan walks
    "max_outcome_walk_bars": 2000, # hard cap on the forward walk that looks for stop/target hit
    # "Was there a real prior move, not just chop" gate on divergence
    # matches only -- see _prior_momentum_extreme(). Off by default for
    # TB/TS (not applicable there; TB/TS already requires structure
    # confirmation via BOS).
    "require_prior_momentum_extreme": True,
    "momentum_extreme_threshold": 20,
    "momentum_extreme_lookback": 90,
    "momentum_extreme_direction_matched": True,
    "conditions": {
        "tb": True,
        "ts": True,
        "classic_bull_div": True,
        "classic_bear_div": True,
        "hidden_bull_div": True,
        "hidden_bear_div": True,
    },
}


def _watchlists():
    if _ts_watchlists is None:
        return []
    try:
        return _ts_watchlists()
    except Exception:
        return []


def _get_symbols(watchlist_id=None):
    if _ts_get_symbols is None:
        return []
    try:
        return _ts_get_symbols(watchlist_id)
    except Exception:
        return []


def _build_ctx(symbol, df, tf):
    """Same lightweight ctx shape scanner_builder.py's own evaluator and
    _last_swing_pivot()/_detect_bos() expect: ctx["timeframes"][tf]["series"][name].
    """
    return {
        "symbol": symbol,
        "timeframes": {
            tf: {
                "series": {
                    "open": df["open"],
                    "high": df["high"],
                    "low": df["low"],
                    "close": df["close"],
                    "volume": df.get("volume", pd.Series(0, index=df.index)),
                }
            }
        },
    }


def _load_history(symbol, tf):
    """Real cached/backfill-aware loader -- NOT the raw live-only fetcher.
    Returns a DataFrame with lowercase open/high/low/close/volume columns
    and a datetime index, or None."""
    if _sb_history is None:
        return None
    df = _sb_history(symbol, tf)
    if df is None or df.empty:
        return None
    cols = {c: c.lower() for c in df.columns}
    df = df.rename(columns=cols)
    needed = {"open", "high", "low", "close"}
    if not needed.issubset(set(df.columns)):
        return None
    return df


def _cutoff_shift(df, lookback_days, max_shifts):
    """How many bars back to scan (as a max `shift` value) to cover
    lookback_days of calendar time, regardless of the instrument's
    session structure (24h futures/crypto vs. equity market hours) --
    filters by actual date, not an assumed bars-per-day constant."""
    n = len(df)
    if n == 0:
        return 0
    try:
        cutoff_date = df.index[-1] - timedelta(days=int(lookback_days))
    except Exception:
        return min(max_shifts, n - 1)
    try:
        idx_arr = df.index
        cutoff_pos = idx_arr.searchsorted(cutoff_date)
        bars_available = max(0, (n - 1) - int(cutoff_pos))
    except Exception:
        bars_available = n - 1
    return max(0, min(int(max_shifts), bars_available))


def _tight_stop(df, idx, side, max_lookback):
    """Nearest prior candle (walking backward from idx, capped at
    max_lookback bars) whose high exceeds this candle's high (side=='short')
    or whose low is below this candle's low (side=='long'). Returns None if
    nothing qualifies within the window -- caller falls back to ATR."""
    lo_bound = max(0, idx - max_lookback)
    highs = df["high"].values
    lows = df["low"].values
    if side == "long":
        target_low = lows[idx]
        for j in range(idx - 1, lo_bound - 1, -1):
            if lows[j] < target_low:
                return float(lows[j])
        return None
    else:
        target_high = highs[idx]
        for j in range(idx - 1, lo_bound - 1, -1):
            if highs[j] > target_high:
                return float(highs[j])
        return None


def _walk_outcome(df, idx, side, stop, target, max_bars):
    """Walk forward from idx+1 looking for the first bar that touches
    stop or target. Returns (outcome, exit_price, exit_date, bars_held)."""
    n = len(df)
    end = min(n, idx + 1 + max_bars)
    highs = df["high"].values
    lows = df["low"].values
    for k in range(idx + 1, end):
        if side == "long":
            if lows[k] <= stop:
                return "STOP", stop, _idx_date(df, k), k - idx
            if highs[k] >= target:
                return "TARGET", target, _idx_date(df, k), k - idx
        else:
            if highs[k] >= stop:
                return "STOP", stop, _idx_date(df, k), k - idx
            if lows[k] <= target:
                return "TARGET", target, _idx_date(df, k), k - idx
    return "OPEN", float(df["close"].iloc[-1]), _idx_date(df, n - 1), (n - 1) - idx


def _idx_date(df, idx):
    try:
        label = df.index[idx]
        if hasattr(label, "isoformat"):
            return label.isoformat()
        return str(label)
    except Exception:
        return None


def _entry_stop_target(df, idx, side, p):
    """side: 'long' or 'short'. Returns dict with entry/stop/target or
    None if there isn't enough data to compute a stop."""
    entry = float(df["close"].iloc[idx])
    stop = _tight_stop(df, idx, side, int(p["tight_stop_max_lookback"]))
    if stop is None:
        try:
            atr = _sb_atr_series(df["high"].iloc[: idx + 1], df["low"].iloc[: idx + 1],
                                  df["close"].iloc[: idx + 1], 14)
            atr_val = float(atr.iloc[-1])
        except Exception:
            atr_val = None
        if not atr_val or atr_val <= 0 or math.isnan(atr_val):
            return None
        stop = entry - atr_val * p["atr_stop_mult"] if side == "long" else entry + atr_val * p["atr_stop_mult"]
    risk = (entry - stop) if side == "long" else (stop - entry)
    if risk is None or risk <= 0:
        return None
    target = entry + risk * p["rr_multiple"] if side == "long" else entry - risk * p["rr_multiple"]
    return {"entry": entry, "stop": stop, "target": target, "risk": risk}


def _macd_arrays(close):
    macd_line, signal, hist = _sb_macd(close)
    return macd_line, signal, hist


def _compute_mfi(high, low, close, volume, period=14):
    """Money Flow Index -- standard formula, computed locally same as
    _compute_adx below it (no MFI primitive exists anywhere in
    scanner_builder.py, confirmed by direct search before writing this).
    typical_price = (H+L+C)/3; raw money flow = typical_price * volume;
    a bar's flow is "positive" if typical_price rose vs the prior bar,
    "negative" if it fell (flat bars contribute to neither running sum).
    MFI = 100 - 100/(1 + positive_flow_sum/negative_flow_sum) over the
    period, same shape as RSI but volume-weighted instead of price-only
    -- this is what makes it a genuinely independent confirming/diverging
    signal alongside RSI and MACD, not just another view of the same input.
    """
    typical = (high + low + close) / 3.0
    raw_flow = typical * volume
    tp_diff = typical.diff()
    pos_flow = raw_flow.where(tp_diff > 0, 0.0)
    neg_flow = raw_flow.where(tp_diff < 0, 0.0)
    pos_sum = pos_flow.rolling(window=period, min_periods=period).sum()
    neg_sum = neg_flow.rolling(window=period, min_periods=period).sum()
    neg_sum_safe = neg_sum.replace(0, 1e-9)
    money_ratio = pos_sum / neg_sum_safe
    mfi = 100 - (100 / (1 + money_ratio))
    return mfi


def _compute_adx(high, low, close, period=14):
    """Wilder's ADX -- same manual DI/DX/RMA construction the original Pine
    strategy used (not borrowed from scanner_builder.py, which has no
    plain/non-UAE-branded ADX primitive to reuse)."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    atr_safe = atr.replace(0, 1e-9)
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_safe)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_safe)
    di_sum = (plus_di + minus_di).replace(0, 1e-9)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def _trend_score(direction, idx, close, ema_fast, ema_slow, adx, macd_line, macd_signal, rsidiff90, atr, bos_confirmed):
    """0-100 composite trend-strength score, same spirit as the original
    Pine strategy's bullProb/bearProb (structure/ADX/EMA-separation/slope/
    MACD/RSI, weighted) -- NOT a byte-identical port of its exact tuned
    constants (structureBreakBufferAtr, trendSlopeMinAtr, etc. were tuned
    for that specific indicator/timeframe and I can't verify an exact
    match without side-by-side testing against the live Pine script).
    Treat this as "how strong does this composite read right now",
    comparable across bars/symbols for ranking, not as a certified
    match to the Pine version's number."""
    w_struct, w_adx, w_ema, w_slope, w_macd, w_rsi = 20.0, 12.0, 18.0, 18.0, 16.0, 16.0
    total_w = w_struct + w_adx + w_ema + w_slope + w_macd + w_rsi

    struct_pts = 100.0 if bos_confirmed else 0.0

    adx_val = adx.iloc[idx]
    adx_pts = 0.0 if adx_val <= 10 else 100.0 if adx_val >= 25 else ((adx_val - 10.0) / 15.0) * 100.0

    ema_sep_pct = abs(ema_fast.iloc[idx] - ema_slow.iloc[idx]) / close.iloc[idx] * 100.0 if close.iloc[idx] else 0.0
    ema_aligned = (ema_fast.iloc[idx] > ema_slow.iloc[idx]) if direction == "bullish" else (ema_fast.iloc[idx] < ema_slow.iloc[idx])
    ema_pts = min(100.0, ema_sep_pct * 120.0) if ema_aligned else 0.0

    lookback_bars = 5
    if idx >= lookback_bars and atr.iloc[idx] and atr.iloc[idx] > 0:
        price_slope_atr = (close.iloc[idx] - close.iloc[idx - lookback_bars]) / atr.iloc[idx] / lookback_bars
    else:
        price_slope_atr = 0.0
    slope_signed = price_slope_atr if direction == "bullish" else -price_slope_atr
    slope_pts = min(100.0, max(0.0, slope_signed) / 0.12 * 100.0)

    macd_aligned = (macd_line.iloc[idx] > macd_signal.iloc[idx]) if direction == "bullish" else (macd_line.iloc[idx] < macd_signal.iloc[idx])
    macd_pts = 100.0 if macd_aligned else 0.0

    rsi_val = rsidiff90.iloc[idx]
    rsi_aligned = rsi_val > 0 if direction == "bullish" else rsi_val < 0
    rsi_pts = 100.0 if rsi_aligned and not pd.isna(rsi_val) else 0.0

    score = (struct_pts * w_struct + adx_pts * w_adx + ema_pts * w_ema
             + slope_pts * w_slope + macd_pts * w_macd + rsi_pts * w_rsi) / total_w
    return round(score, 1)


def _scan_tb_ts(df, ctx, tf, p, want_tb, want_ts):
    """Backward-shift scan for TB (bull structure+trend+momentum flip)
    and TS (bear mirror). Structure gate reuses _detect_bos() exactly --
    same function the DSL's BosBullish/BosBearish primitives call."""
    out = []
    if not want_tb and not want_ts:
        return out
    close = df["close"]
    ema_fast = _sb_ema(close, 8)
    ema_slow = _sb_ema(close, 21)
    rsi14 = _sb_rsi(close, 14)
    rsidiff90 = rsi14 - _sb_ema(rsi14, 90)
    macd_line, macd_signal, hist = _macd_arrays(close)
    atr = _sb_atr_series(df["high"], df["low"], df["close"], 14)
    adx = _compute_adx(df["high"], df["low"], df["close"], 14)

    n = len(df)
    max_shift = _cutoff_shift(df, p["lookback_days"], p["max_shifts_per_symbol"])
    for shift in range(0, max_shift + 1):
        idx = n - 1 - shift
        if idx < 1:
            continue
        if want_tb:
            bos = _sb_detect_bos(ctx, "bullish", p["struct_lookback"], p["struct_left"], p["struct_right"], shift, tf)
            bos_confirmed = bool(bos and bos.get("bos"))
            if bos_confirmed:
                if (ema_fast.iloc[idx] > ema_slow.iloc[idx]
                        and close.iloc[idx] > ema_fast.iloc[idx]
                        and macd_line.iloc[idx] > macd_signal.iloc[idx]
                        and hist.iloc[idx] > hist.iloc[idx - 1]
                        and rsidiff90.iloc[idx] > 0):
                    est = _entry_stop_target(df, idx, "long", p)
                    if est:
                        score = _trend_score("bullish", idx, close, ema_fast, ema_slow, adx,
                                              macd_line, macd_signal, rsidiff90, atr, bos_confirmed)
                        out.append({"condition": "TB", "side": "long", "idx": idx, "trend_score": score, **est})
        if want_ts:
            bos = _sb_detect_bos(ctx, "bearish", p["struct_lookback"], p["struct_left"], p["struct_right"], shift, tf)
            bos_confirmed = bool(bos and bos.get("bos"))
            if bos_confirmed:
                if (ema_fast.iloc[idx] < ema_slow.iloc[idx]
                        and close.iloc[idx] < ema_fast.iloc[idx]
                        and macd_line.iloc[idx] < macd_signal.iloc[idx]
                        and hist.iloc[idx] < hist.iloc[idx - 1]
                        and rsidiff90.iloc[idx] < 0):
                    est = _entry_stop_target(df, idx, "short", p)
                    if est:
                        score = _trend_score("bearish", idx, close, ema_fast, ema_slow, adx,
                                              macd_line, macd_signal, rsidiff90, atr, bos_confirmed)
                        out.append({"condition": "TS", "side": "short", "idx": idx, "trend_score": score, **est})
    return out


def _prior_momentum_extreme(rsidiff90, idx, lookback, threshold, direction):
    """Did RSIDiff90 breach +-threshold at any point in the `lookback`
    bars strictly BEFORE `idx` (the recent pivot)? This is the "was
    there a genuine prior move, not just chop" confirmation -- a
    reversal off a low is more probable when a real prior selloff drove
    RSIDiff90 to a negative extreme first (direction="down"), and
    mirrored for reversals off a high (direction="up"). Returns
    (passes: bool, extreme_value: float|None) -- extreme_value is the
    most extreme RSIDiff90 print found in the window, for display/
    transparency even when the gate is off.
    """
    start = max(0, idx - int(lookback))
    end = idx  # exclusive of idx itself -- "before" the pivot, not at it
    if end <= start:
        return False, None
    window = rsidiff90.iloc[start:end]
    if window.empty or window.isna().all():
        return False, None
    if direction == "down":
        extreme = float(window.min())
        return extreme <= -abs(threshold), extreme
    if direction == "up":
        extreme = float(window.max())
        return extreme >= abs(threshold), extreme
    # either direction -- report whichever side is more extreme
    lo, hi = float(window.min()), float(window.max())
    extreme = lo if abs(lo) >= abs(hi) else hi
    return abs(extreme) >= abs(threshold), extreme


def _scan_divergence(df, ctx, tf, p, flags):
    """Classic/hidden bull/bear divergence. Calls _last_swing_pivot()
    twice per bar (current + prior, via shift + age + 1 -- the same
    technique _detect_choch/_detect_bos use internally) to get an EXACT
    prior-pivot comparison, not the Shift(expr, age+N) approximation
    the query-string version of this had to use.

    Each match also gets a divergence_score (0-4): how many of RSI,
    MACD line, MACD histogram, and MFI INDEPENDENTLY agree with the
    divergence direction at that pivot pair. RSI/MACD-line/MACD-hist are
    an exact port of the original Pine strategy's bullScore/bearScore/
    hiddenBullScore/hiddenBearScore (3 independent yes/no checks against
    the same price condition, summed); MFI is added as a 4th, computed
    the same way (no MFI primitive existed anywhere in this codebase
    before this addition -- see _compute_mfi above).

    Optional gate (p["require_prior_momentum_extreme"], default True):
    only keep a match if RSIDiff90 hit +-p["momentum_extreme_threshold"]
    (default 20) at some point in the p["momentum_extreme_lookback"]
    bars before the pivot -- i.e. this was a real prior move that
    exhausted, not noise. When p["momentum_extreme_direction_matched"]
    is True (default), the extreme must be on the side that matches the
    reversal (negative extreme before a low-side reversal, positive
    before a high-side reversal); when False, an extreme in EITHER
    direction satisfies the gate.
    """
    out = []
    any_wanted = any(flags.values())
    if not any_wanted:
        return out
    close = df["close"]
    rsi14 = _sb_rsi(close, 14)
    macd_line, macd_signal, hist = _macd_arrays(close)
    mfi = _compute_mfi(df["high"], df["low"], df["close"], df["volume"])
    rsidiff90 = rsi14 - _sb_ema(rsi14, 90)
    n = len(df)
    max_shift = _cutoff_shift(df, p["lookback_days"], p["max_shifts_per_symbol"])

    require_extreme = bool(p.get("require_prior_momentum_extreme", True))
    extreme_threshold = float(p.get("momentum_extreme_threshold", 20) or 20)
    extreme_lookback = int(p.get("momentum_extreme_lookback", 90) or 90)
    direction_matched = bool(p.get("momentum_extreme_direction_matched", True))

    for shift in range(0, max_shift + 1):
        for side in ("low", "high"):
            p_recent = _sb_last_swing_pivot(ctx, side=side, lookback=p["div_lookback"],
                                             left=p["div_left"], right=p["div_right"],
                                             shift=shift, tf_default=tf)
            if not p_recent:
                continue
            p_prior = _sb_last_swing_pivot(ctx, side=side, lookback=p["div_lookback"],
                                            left=p["div_left"], right=p["div_right"],
                                            shift=shift + int(p_recent["age"]) + 1, tf_default=tf)
            if not p_prior:
                continue
            r_idx, p_idx = p_recent["idx"], p_prior["idx"]
            if r_idx >= n or p_idx >= n or r_idx < 0 or p_idx < 0:
                continue
            rsi_recent = float(rsi14.iloc[r_idx])
            rsi_prior = float(rsi14.iloc[p_idx])
            macd_recent = float(macd_line.iloc[r_idx])
            macd_prior = float(macd_line.iloc[p_idx])
            hist_recent = float(hist.iloc[r_idx])
            hist_prior = float(hist.iloc[p_idx])
            mfi_recent = float(mfi.iloc[r_idx]) if pd.notna(mfi.iloc[r_idx]) else None
            mfi_prior = float(mfi.iloc[p_idx]) if pd.notna(mfi.iloc[p_idx]) else None
            price_recent = p_recent["price"]
            price_prior = p_prior["price"]

            def _gate(extreme_dir):
                if not require_extreme:
                    return True, None
                dirn = extreme_dir if direction_matched else "either"
                ok, val = _prior_momentum_extreme(rsidiff90, r_idx, extreme_lookback, extreme_threshold, dirn)
                return ok, val

            if side == "low":
                price_lower = price_recent < price_prior
                price_higher = price_recent > price_prior
                if flags.get("classic_bull_div") and price_lower:
                    score = (int(rsi_recent > rsi_prior) + int(macd_recent > macd_prior) + int(hist_recent > hist_prior))
                    if mfi_recent is not None and mfi_prior is not None:
                        score += int(mfi_recent > mfi_prior)
                    gate_ok, extreme_val = _gate("down")
                    if score > 0 and gate_ok:
                        est = _entry_stop_target(df, r_idx, "long", p)
                        if est:
                            out.append({"condition": "Classic Bull Div", "side": "long", "idx": r_idx,
                                        "divergence_score": score, "prior_momentum_extreme": extreme_val, **est})
                if flags.get("hidden_bull_div") and price_higher:
                    score = (int(rsi_recent < rsi_prior) + int(macd_recent < macd_prior) + int(hist_recent < hist_prior))
                    if mfi_recent is not None and mfi_prior is not None:
                        score += int(mfi_recent < mfi_prior)
                    gate_ok, extreme_val = _gate("down")
                    if score > 0 and gate_ok:
                        est = _entry_stop_target(df, r_idx, "long", p)
                        if est:
                            out.append({"condition": "Hidden Bull Div", "side": "long", "idx": r_idx,
                                        "divergence_score": score, "prior_momentum_extreme": extreme_val, **est})
            else:
                price_higher = price_recent > price_prior
                price_lower = price_recent < price_prior
                if flags.get("classic_bear_div") and price_higher:
                    score = (int(rsi_recent < rsi_prior) + int(macd_recent < macd_prior) + int(hist_recent < hist_prior))
                    if mfi_recent is not None and mfi_prior is not None:
                        score += int(mfi_recent < mfi_prior)
                    gate_ok, extreme_val = _gate("up")
                    if score > 0 and gate_ok:
                        est = _entry_stop_target(df, r_idx, "short", p)
                        if est:
                            out.append({"condition": "Classic Bear Div", "side": "short", "idx": r_idx,
                                        "divergence_score": score, "prior_momentum_extreme": extreme_val, **est})
                if flags.get("hidden_bear_div") and price_lower:
                    score = (int(rsi_recent > rsi_prior) + int(macd_recent > macd_prior) + int(hist_recent > hist_prior))
                    if mfi_recent is not None and mfi_prior is not None:
                        score += int(mfi_recent > mfi_prior)
                    gate_ok, extreme_val = _gate("up")
                    if score > 0 and gate_ok:
                        est = _entry_stop_target(df, r_idx, "short", p)
                        if est:
                            out.append({"condition": "Hidden Bear Div", "side": "short", "idx": r_idx,
                                        "divergence_score": score, "prior_momentum_extreme": extreme_val, **est})
    return out


def scan_symbol(symbol, params):
    p = dict(DEFAULT_PARAMS)
    p.update({k: v for k, v in (params or {}).items() if k != "conditions"})
    conditions = dict(DEFAULT_PARAMS["conditions"])
    conditions.update((params or {}).get("conditions") or {})

    tf = _sb_normalize_tf(p["timeframe"]) if _sb_normalize_tf else p["timeframe"]
    df = _load_history(symbol, tf)
    if df is None or len(df) < (p["struct_left"] + p["struct_right"] + 5):
        return []

    ctx = _build_ctx(symbol, df, tf)
    matches = []
    matches += _scan_tb_ts(df, ctx, tf, p, conditions.get("tb"), conditions.get("ts"))
    matches += _scan_divergence(df, ctx, tf, p, conditions)

    results = []
    # Dedup: same (condition, idx) shouldn't repeat across overlapping
    # shift ranges scanned from different starting points.
    seen = set()
    for m in matches:
        key = (m["condition"], m["idx"])
        if key in seen:
            continue
        seen.add(key)
        outcome, exit_price, exit_date, bars_held = _walk_outcome(
            df, m["idx"], m["side"], m["stop"], m["target"], int(p["max_outcome_walk_bars"])
        )
        results.append({
            "symbol": symbol,
            "condition": m["condition"],
            "side": m["side"],
            "date": _idx_date(df, m["idx"]),
            "entry": round(m["entry"], 4),
            "stop": round(m["stop"], 4),
            "target": round(m["target"], 4),
            "risk": round(m["risk"], 4),
            "rr_multiple": p["rr_multiple"],
            "trend_score": m.get("trend_score"),          # 0-100, TB/TS only
            "divergence_score": m.get("divergence_score"), # 0-4, divergence conditions only
            "prior_momentum_extreme": m.get("prior_momentum_extreme"), # RSIDiff90 extreme found before the pivot, divergence conditions only
            "outcome": outcome,
            "exit_price": round(exit_price, 4) if exit_price is not None else None,
            "exit_date": exit_date,
            "bars_held": bars_held,
        })
    results.sort(key=lambda r: r["date"] or "", reverse=True)
    return results


def scan_watchlist(watchlist_id=None, params=None):
    symbols = _get_symbols(watchlist_id)
    all_results = []
    errors = {}
    for sym in symbols:
        try:
            r = scan_symbol(sym, params)
            all_results.extend(r)
        except Exception as e:
            errors[sym] = str(e)
    all_results.sort(key=lambda r: r["date"] or "", reverse=True)
    return all_results, errors


# ── Backtest engine (multi-symbol, multi-timeframe, persisted, with a
#    feedback loop: POP per symbol/timeframe/condition + rule-based
#    strategy-adjustment recommendations) ──────────────────────────────
#
# Reuses scan_symbol() completely unchanged -- a backtest here is just
# scan_symbol() run with a much longer lookback (from start_date instead
# of a short recent window) and its results persisted instead of only
# returned to the caller. This guarantees the backtest and the "live"
# scan can never silently disagree about what counts as a signal or how
# entry/stop/target/outcome are computed -- there is exactly one
# implementation of that logic in this file.
#
# Runs in a background thread (same pattern as scanner_builder.py's own
# bulk-backfill watchers) because a full multi-symbol x multi-timeframe
# backtest from an old start_date can take a while -- the API returns a
# run_id immediately and the UI polls /api/backtest/status/<run_id>.

def _bt_conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _ensure_backtest_tables():
    con = _bt_conn()
    con.execute("""
        CREATE TABLE IF NOT EXISTS trend_div_backtest_runs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            start_date TEXT NOT NULL,
            watchlist_id INTEGER,
            condition_mode TEXT NOT NULL,
            timeframes_json TEXT NOT NULL,
            params_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            symbols_total INTEGER DEFAULT 0,
            symbols_done INTEGER DEFAULT 0,
            trade_count INTEGER DEFAULT 0,
            error TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trend_div_backtest_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            condition TEXT NOT NULL,
            side TEXT NOT NULL,
            signal_date TEXT,
            entry REAL, stop REAL, target REAL, risk REAL,
            outcome TEXT, exit_price REAL, exit_date TEXT, bars_held INTEGER,
            r_multiple REAL,
            trend_score REAL,
            divergence_score INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trend_div_pop_summary (
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            condition TEXT NOT NULL,
            trade_count INTEGER,
            target_count INTEGER,
            stop_count INTEGER,
            open_count INTEGER,
            pop_pct REAL,
            avg_r REAL,
            expectancy_r REAL,
            run_id TEXT,
            updated_at TEXT,
            PRIMARY KEY (symbol, timeframe, condition)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trend_div_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_id TEXT,
            generated_at TEXT,
            recommendation TEXT NOT NULL,
            severity TEXT,
            basis_json TEXT
        )
    """)
    con.commit()
    con.close()


def _r_multiple(side, entry, stop, exit_price):
    """Realized R for a closed trade: how many multiples of the original
    risk (entry-to-stop distance) the trade actually made or lost.
    TARGET outcomes will always equal ~+rr_multiple by construction
    (scan_symbol's outcome walk exits exactly at the target price) --
    this is what makes expectancy math below meaningful rather than
    circular: it's real regardless of outcome type, including OPEN
    (marked-to-market against the still-open position)."""
    risk = (entry - stop) if side == "long" else (stop - entry)
    if not risk or risk <= 0:
        return None
    pnl = (exit_price - entry) if side == "long" else (entry - exit_price)
    return round(pnl / risk, 3)


def _run_backtest_worker(run_id, start_date_str, watchlist_id, timeframes, condition_mode, base_params):
    con = _bt_conn()
    try:
        symbols = _get_symbols(watchlist_id)
        con.execute("UPDATE trend_div_backtest_runs SET symbols_total=? WHERE id=?",
                     (len(symbols) * max(1, len(timeframes)), run_id))
        con.commit()

        try:
            start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
            lookback_days = max(1, (datetime.now() - start_dt).days)
        except Exception:
            lookback_days = 365

        conditions = {
            "tb": condition_mode in ("TB", "BOTH"),
            "ts": condition_mode in ("TS", "BOTH"),
            # Backtest mode is trend-focused (TB/TS) per this request;
            # divergence conditions are left available on the live scan
            # tab but off here unless explicitly requested later.
            "classic_bull_div": False,
            "classic_bear_div": False,
            "hidden_bull_div": False,
            "hidden_bear_div": False,
        }

        done = 0
        total_trades = 0
        for sym in symbols:
            for tf in timeframes:
                try:
                    p = dict(base_params)
                    p["timeframe"] = tf
                    p["lookback_days"] = lookback_days
                    # Backtest wants full date-range coverage, not the
                    # short-window cap the live-scan tab defaults to.
                    p["max_shifts_per_symbol"] = max(base_params.get("max_shifts_per_symbol", 300), 5000)
                    p["conditions"] = conditions
                    trades = scan_symbol(sym, p)
                    rows = []
                    for t in trades:
                        r_mult = _r_multiple(t["side"], t["entry"], t["stop"], t["exit_price"]) if t["exit_price"] is not None else None
                        rows.append((
                            run_id, sym, tf, t["condition"], t["side"], t["date"],
                            t["entry"], t["stop"], t["target"], t["risk"],
                            t["outcome"], t["exit_price"], t["exit_date"], t["bars_held"], r_mult,
                            t.get("trend_score"), t.get("divergence_score")
                        ))
                    if rows:
                        con.executemany("""
                            INSERT INTO trend_div_backtest_trades
                            (run_id, symbol, timeframe, condition, side, signal_date,
                             entry, stop, target, risk, outcome, exit_price, exit_date, bars_held, r_multiple,
                             trend_score, divergence_score)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, rows)
                        total_trades += len(rows)
                except Exception as e:
                    con.execute("UPDATE trend_div_backtest_runs SET error=? WHERE id=?",
                                 (f"{sym}/{tf}: {e}", run_id))
                done += 1
                con.execute("UPDATE trend_div_backtest_runs SET symbols_done=?, trade_count=? WHERE id=?",
                             (done, total_trades, run_id))
                con.commit()

        _compute_pop_summary(con, run_id)
        _generate_recommendations(con, run_id, condition_mode)
        con.execute("UPDATE trend_div_backtest_runs SET status='completed' WHERE id=?", (run_id,))
        con.commit()
    except Exception as e:
        con.execute("UPDATE trend_div_backtest_runs SET status='error', error=? WHERE id=?", (str(e), run_id))
        con.commit()
    finally:
        con.close()


def _compute_pop_summary(con, run_id):
    """POP (probability of profit) per (symbol, timeframe, condition) =
    TARGET / (TARGET + STOP) among CLOSED trades from this run -- OPEN
    trades are excluded from the ratio (outcome not yet known) but still
    counted separately so a high proportion of still-open trades is
    visible rather than silently dropped. avg_r / expectancy_r are
    computed across all trades with a known exit (closed + OPEN, since
    OPEN carries a mark-to-market r_multiple), which is what actually
    determines whether the concept is profitable for that symbol/tf,
    not just how often it wins."""
    rows = con.execute(
        "SELECT symbol, timeframe, condition, outcome, r_multiple FROM trend_div_backtest_trades WHERE run_id=?",
        (run_id,)
    ).fetchall()
    groups = {}
    for r in rows:
        key = (r["symbol"], r["timeframe"], r["condition"])
        groups.setdefault(key, []).append(r)

    now = datetime.now().isoformat(timespec="seconds")
    for (symbol, tf, cond), trades in groups.items():
        target_n = sum(1 for t in trades if t["outcome"] == "TARGET")
        stop_n = sum(1 for t in trades if t["outcome"] == "STOP")
        open_n = sum(1 for t in trades if t["outcome"] == "OPEN")
        closed_n = target_n + stop_n
        pop_pct = round(100.0 * target_n / closed_n, 1) if closed_n > 0 else None
        r_vals = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
        avg_r = round(sum(r_vals) / len(r_vals), 3) if r_vals else None
        con.execute("""
            INSERT INTO trend_div_pop_summary
            (symbol, timeframe, condition, trade_count, target_count, stop_count, open_count,
             pop_pct, avg_r, expectancy_r, run_id, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol, timeframe, condition) DO UPDATE SET
                trade_count=excluded.trade_count, target_count=excluded.target_count,
                stop_count=excluded.stop_count, open_count=excluded.open_count,
                pop_pct=excluded.pop_pct, avg_r=excluded.avg_r, expectancy_r=excluded.expectancy_r,
                run_id=excluded.run_id, updated_at=excluded.updated_at
        """, (symbol, tf, cond, len(trades), target_n, stop_n, open_n,
              pop_pct, avg_r, avg_r, run_id, now))
    con.commit()


# Minimum closed-trade sample size before a recommendation is stated
# without a low-confidence qualifier -- an arbitrary but explicit
# threshold, not hidden inside the logic.
MIN_SAMPLE_FOR_CONFIDENT_REC = 15
MIN_SAMPLE_FOR_ANY_REC = 5


def _generate_recommendations(con, run_id, condition_mode):
    """Rule-based (not ML) recommendations, deliberately -- every rule
    here is inspectable and its basis_json shows exactly which numbers
    drove it, consistent with how the rest of this codebase avoids
    black-box scoring. Three rule types:
      1. Best timeframe for a symbol (highest POP among tf's with
         enough sample size).
      2. TB vs TS asymmetry for a symbol (BOTH mode only) -- flag if
         one side is meaningfully worse than the other.
      3. Expectancy sign vs the configured RR -- breakeven POP needed
         for a given RR is 1/(1+RR); if actual POP is below that with
         a real sample, the concept has negative expectancy for that
         symbol/timeframe as currently configured, which is actionable
         (lower RR, or exclude that symbol/timeframe/side)."""
    rows = con.execute(
        "SELECT symbol, timeframe, condition, trade_count, target_count, stop_count, pop_pct, avg_r "
        "FROM trend_div_pop_summary WHERE run_id=?", (run_id,)
    ).fetchall()
    by_symbol = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r)

    now = datetime.now().isoformat(timespec="seconds")
    p = dict(DEFAULT_PARAMS)
    rr = p["rr_multiple"]
    breakeven_pop = round(100.0 / (1.0 + rr), 1)

    for symbol, entries in by_symbol.items():
        closed_entries = [e for e in entries if (e["target_count"] + e["stop_count"]) >= MIN_SAMPLE_FOR_ANY_REC]
        if not closed_entries:
            continue

        # Rule 1: best timeframe
        best = max(closed_entries, key=lambda e: (e["pop_pct"] or -1))
        n_closed = best["target_count"] + best["stop_count"]
        confidence = "confident" if n_closed >= MIN_SAMPLE_FOR_CONFIDENT_REC else "low-confidence (small sample)"
        rec_text = (f"Best observed timeframe: {best['timeframe']} / {best['condition']} "
                    f"-- POP {best['pop_pct']}% over {n_closed} closed trades ({confidence}).")
        _insert_recommendation(con, symbol, run_id, rec_text, "info", {
            "rule": "best_timeframe", "timeframe": best["timeframe"], "condition": best["condition"],
            "pop_pct": best["pop_pct"], "closed_trades": n_closed
        })

        # Rule 2: TB vs TS asymmetry (only meaningful in BOTH mode)
        if condition_mode == "BOTH":
            tb_rows = [e for e in closed_entries if e["condition"] == "TB"]
            ts_rows = [e for e in closed_entries if e["condition"] == "TS"]
            if tb_rows and ts_rows:
                tb_pop = sum(e["pop_pct"] * (e["target_count"] + e["stop_count"]) for e in tb_rows) / sum(e["target_count"] + e["stop_count"] for e in tb_rows)
                ts_pop = sum(e["pop_pct"] * (e["target_count"] + e["stop_count"]) for e in ts_rows) / sum(e["target_count"] + e["stop_count"] for e in ts_rows)
                if abs(tb_pop - ts_pop) >= 15:
                    weaker_side = "TS" if ts_pop < tb_pop else "TB"
                    stronger_side = "TB" if weaker_side == "TS" else "TS"
                    rec_text = (f"{weaker_side} underperforms {stronger_side} by {abs(tb_pop-ts_pop):.1f} POP points "
                                f"on this symbol (TB={tb_pop:.1f}%, TS={ts_pop:.1f}%) -- consider trading {stronger_side}-only here.")
                    _insert_recommendation(con, symbol, run_id, rec_text, "suggestion", {
                        "rule": "side_asymmetry", "tb_pop": round(tb_pop, 1), "ts_pop": round(ts_pop, 1)
                    })

        # Rule 3: expectancy vs configured RR
        for e in closed_entries:
            n = e["target_count"] + e["stop_count"]
            if n < MIN_SAMPLE_FOR_ANY_REC:
                continue
            pop = e["pop_pct"] or 0.0
            if pop < breakeven_pop:
                confidence = "" if n >= MIN_SAMPLE_FOR_CONFIDENT_REC else " (small sample -- treat as a lead, not a conclusion)"
                rec_text = (f"{e['timeframe']} / {e['condition']}: POP {pop}% is BELOW the {breakeven_pop}% "
                             f"breakeven needed at {rr}:1 target risk -- negative expectancy as currently configured{confidence}. "
                             f"Consider lowering the target R-multiple for this symbol/timeframe, or excluding it.")
                _insert_recommendation(con, symbol, run_id, rec_text, "warning", {
                    "rule": "expectancy_below_breakeven", "timeframe": e["timeframe"], "condition": e["condition"],
                    "pop_pct": pop, "breakeven_pop_pct": breakeven_pop, "rr_multiple": rr, "closed_trades": n
                })
    con.commit()


def _insert_recommendation(con, symbol, run_id, text, severity, basis):
    con.execute("""
        INSERT INTO trend_div_recommendations (symbol, run_id, generated_at, recommendation, severity, basis_json)
        VALUES (?,?,?,?,?,?)
    """, (symbol, run_id, datetime.now().isoformat(timespec="seconds"), text, severity, json.dumps(basis)))


def start_backtest(start_date, watchlist_id, timeframes, condition_mode, params):
    _ensure_backtest_tables()
    run_id = uuid.uuid4().hex[:12]
    con = _bt_conn()
    con.execute("""
        INSERT INTO trend_div_backtest_runs
        (id, created_at, start_date, watchlist_id, condition_mode, timeframes_json, params_json, status)
        VALUES (?,?,?,?,?,?,?, 'running')
    """, (run_id, datetime.now().isoformat(timespec="seconds"), start_date, watchlist_id,
          condition_mode, json.dumps(timeframes), json.dumps(params)))
    con.commit()
    con.close()

    t = threading.Thread(
        target=_run_backtest_worker,
        args=(run_id, start_date, watchlist_id, timeframes, condition_mode, params),
        daemon=True,
    )
    t.start()
    return run_id




@trend_div_bp.route("/")
def page():
    return render_template("trend_divergence_scanner.html", timeframes=_SB_TIMEFRAMES,
                            cached_timeframes=sorted(CACHED_TIMEFRAMES))


@trend_div_bp.route("/api/watchlists")
def api_watchlists():
    return jsonify({"watchlists": _watchlists()})


@trend_div_bp.route("/api/scan", methods=["POST"])
def api_scan():
    body = request.get_json(silent=True) or {}
    watchlist_id = body.get("watchlist_id")
    params = body.get("params") or {}
    results, errors = scan_watchlist(watchlist_id=watchlist_id, params=params)
    return jsonify({"count": len(results), "results": results, "errors": errors})


# ── Backtest routes ────────────────────────────────────────────────────

@trend_div_bp.route("/api/backtest/start", methods=["POST"])
def api_backtest_start():
    body = request.get_json(silent=True) or {}
    start_date = body.get("start_date")
    if not start_date:
        return jsonify({"error": "start_date is required (YYYY-MM-DD)"}), 400
    watchlist_id = body.get("watchlist_id")
    timeframes = body.get("timeframes") or sorted(CACHED_TIMEFRAMES)
    condition_mode = (body.get("condition_mode") or "BOTH").upper()
    if condition_mode not in ("TB", "TS", "BOTH"):
        return jsonify({"error": "condition_mode must be TB, TS, or BOTH"}), 400
    params = body.get("params") or {}
    run_id = start_backtest(start_date, watchlist_id, timeframes, condition_mode, params)
    return jsonify({"run_id": run_id})


@trend_div_bp.route("/api/backtest/status/<run_id>")
def api_backtest_status(run_id):
    _ensure_backtest_tables()
    con = _bt_conn()
    row = con.execute("SELECT * FROM trend_div_backtest_runs WHERE id=?", (run_id,)).fetchone()
    con.close()
    if not row:
        return jsonify({"error": "run not found"}), 404
    return jsonify(dict(row))


@trend_div_bp.route("/api/backtest/results/<run_id>")
def api_backtest_results(run_id):
    _ensure_backtest_tables()
    con = _bt_conn()
    rows = con.execute(
        "SELECT * FROM trend_div_backtest_trades WHERE run_id=? ORDER BY signal_date DESC", (run_id,)
    ).fetchall()
    con.close()
    return jsonify({"count": len(rows), "trades": [dict(r) for r in rows]})


@trend_div_bp.route("/api/backtest/pop")
def api_backtest_pop():
    _ensure_backtest_tables()
    symbol = request.args.get("symbol")
    run_id = request.args.get("run_id")
    con = _bt_conn()
    q = "SELECT * FROM trend_div_pop_summary WHERE 1=1"
    args = []
    if symbol:
        q += " AND symbol=?"
        args.append(symbol.upper())
    if run_id:
        q += " AND run_id=?"
        args.append(run_id)
    q += " ORDER BY pop_pct DESC"
    rows = con.execute(q, args).fetchall()
    con.close()
    return jsonify({"count": len(rows), "pop": [dict(r) for r in rows]})


@trend_div_bp.route("/api/backtest/recommendations")
def api_backtest_recommendations():
    _ensure_backtest_tables()
    symbol = request.args.get("symbol")
    run_id = request.args.get("run_id")
    con = _bt_conn()
    q = "SELECT * FROM trend_div_recommendations WHERE 1=1"
    args = []
    if symbol:
        q += " AND symbol=?"
        args.append(symbol.upper())
    if run_id:
        q += " AND run_id=?"
        args.append(run_id)
    q += " ORDER BY generated_at DESC"
    rows = con.execute(q, args).fetchall()
    con.close()
    return jsonify({"count": len(rows), "recommendations": [dict(r) for r in rows]})


@trend_div_bp.route("/api/backtest/runs")
def api_backtest_runs():
    _ensure_backtest_tables()
    con = _bt_conn()
    rows = con.execute(
        "SELECT id, created_at, start_date, condition_mode, timeframes_json, status, symbols_total, symbols_done, trade_count "
        "FROM trend_div_backtest_runs ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    con.close()
    return jsonify({"count": len(rows), "runs": [dict(r) for r in rows]})




def synthetic_selftest():
    """Builds a synthetic OHLCV series with a known uptrend + pullback +
    breakout shape and confirms TB/TS/divergence detection and the
    stop/target/outcome math behave sanely. Not a substitute for testing
    against real data -- see module docstring's KNOWN GAPS section."""
    import numpy as np
    n = 300
    rng = pd.date_range("2024-01-01", periods=n, freq="h")
    price = [100.0]
    for i in range(1, n):
        if i < 100:
            drift = 0.15
        elif i < 140:
            drift = -0.08
        elif i < 260:
            drift = 0.20
        else:
            drift = -0.05
        noise = np.random.uniform(-0.3, 0.3)
        price.append(max(1.0, price[-1] + drift + noise))
    close = pd.Series(price, index=rng)
    high = close + pd.Series(np.random.uniform(0.05, 0.4, n), index=rng)
    low = close - pd.Series(np.random.uniform(0.05, 0.4, n), index=rng)
    openp = close.shift(1).fillna(close.iloc[0])
    vol = pd.Series(np.random.uniform(1000, 5000, n), index=rng)
    df = pd.DataFrame({"open": openp, "high": high, "low": low, "close": close, "volume": vol})

    ctx = _build_ctx("TEST", df, "1h")
    p = dict(DEFAULT_PARAMS)
    p["timeframe"] = "1h"
    p["lookback_days"] = 20
    conditions = dict(DEFAULT_PARAMS["conditions"])

    tb_ts = _scan_tb_ts(df, ctx, "1h", p, True, True)
    div = _scan_divergence(df, ctx, "1h", p, conditions)

    print(f"[selftest] TB/TS matches: {len(tb_ts)}")
    print(f"[selftest] Divergence matches: {len(div)}")
    for m in (tb_ts + div)[:5]:
        est = m
        print(f"  {est['condition']:18s} idx={est['idx']:4d} entry={est['entry']:.2f} "
              f"stop={est['stop']:.2f} target={est['target']:.2f} risk={est['risk']:.2f}")
        outcome, exit_price, exit_date, bars = _walk_outcome(df, est["idx"], est["side"], est["stop"], est["target"], 2000)
        print(f"    -> outcome={outcome} exit_price={exit_price} bars_held={bars}")

    assert len(tb_ts) > 0 or len(div) > 0, "Expected at least one signal on a trending synthetic series"
    print("[selftest] PASSED")


if __name__ == "__main__":
    synthetic_selftest()
