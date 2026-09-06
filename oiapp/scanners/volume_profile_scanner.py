"""
volume_profile_scanner.py -- "Volume Profile Balance/Imbalance" Scanner (v1)

Answers, across a watchlist: for each symbol, is price currently inside
yesterday's value area (balanced -> mean-reversion territory) or has it
broken out and held (imbalanced -> trend territory), is that move backed by
real volume, and where is the point of control (POC) migrating -- then
classifies each symbol into Trend-Long / Trend-Short / Reversion-Long /
Reversion-Short / No-Edge with a target (nearest prior POC) and an
invalidation level (the value-area boundary that was broken).

This is a direct Python port of the same order-flow thesis (Fabio
Valentini's balance/imbalance model) already implemented as a TradingView
Pine indicator for the person's live gold-futures trading -- ported here so
it can run across the whole 104-symbol watchlist as a daily scan instead of
one chart at a time.

DESIGN, following trend_divergence_scanner.py's established pattern for
this codebase:
  - Reuses _history() (the real cached/backfill-aware loader) and
    _watchlists()/_get_symbols() from trade_setup_scanner.py -- no
    reimplementation of data loading.
  - Volume-at-price binning is NOT a scanner_builder.py primitive (confirmed
    via direct search -- none exists), so it's implemented directly here as
    plain numpy, same algorithm as the Pine indicator's f_profile(): bin by
    each hourly bar's (high+low)/2 midpoint weighted by that bar's volume,
    POC = the bin with the most volume, value area = expand outward from
    POC until 70% of volume is covered.
  - Runs on 1h bars (CACHED_TIMEFRAMES in this codebase -- fast, deep local
    history) rather than 1m: this is a daily/swing scanner across 104
    symbols, not a scalping tool, so hourly granularity is the right
    tradeoff between profile fidelity and scan speed. (The Pine indicator
    itself uses 1-30min sub-bars for live chart display; this scanner's
    job is different -- surface daily candidates, not paint a chart.)
  - Day/week bucketing uses plain calendar date / ISO week -- NOT the
    asset-aware Friday-5pm-anchored week logic built into the Pine script.
    That precision matters for gold/crypto scalping; it doesn't matter for
    a mostly-equity 104-symbol watchlist scan (no weekend equity trading to
    misalign), so this intentionally stays simpler here.
  - Results are cached per (symbol, date) in vp_scan_results so a scheduled
    precompute (the new "Volume Profile" watchlist schedule step) can run
    once each morning and the live page just reads the cache -- this is the
    actual mechanism behind "faster scanner execution" requested alongside
    this scanner.
  - Intraday confirmation is a SEPARATE, on-demand check (not precomputed)
    that pulls live DXLink candles via the existing shared `feed` singleton
    (services.tastytrade_feed.feed) and computes the same relative-volume +
    close-location-value delta proxy the Pine indicator's CVD filter uses,
    to confirm the daily thesis still holds right now before flagging a
    candidate as "confirmed" vs. merely "on watch."

KNOWN GAPS / NOT YET VERIFIED END-TO-END:
  - This sandbox has no network access and no copy of the real price_cache
    DB, so this has NOT been run against real market data. Verified via
    py_compile and a synthetic OHLCV smoke test (synthetic_selftest() at
    the bottom of this file). Recommend running one real scan on a small
    watchlist before trusting results broadly.
  - Intraday confirmation requires tastytrade_configured() to be true and a
    working DXLink session -- if unavailable, /api/confirm returns
    {"available": False, "error": ...} rather than failing the whole page.
"""
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, request, render_template

vp_bp = Blueprint("vp_bp", __name__, url_prefix="/scanner/volume-profile")

from ..config import DB_PATH as _OIAPP_DB_PATH
DB_PATH = _OIAPP_DB_PATH

try:
    from .scanner_builder import _history as _sb_history
except Exception:
    _sb_history = None

# Reuse the one watchlist source the rest of the app already agrees on.
try:
    from .trade_setup_scanner import _watchlists as _ts_watchlists, _get_symbols as _ts_get_symbols
except Exception:
    _ts_watchlists = None
    _ts_get_symbols = None

DEFAULT_PARAMS = {
    "profile_rows": 24,
    "value_area_pct": 0.70,
    "lookback_days": 10,          # how many completed DAYS to keep for POC-migration + target search
    "week_lookback": 12,          # how many completed WEEKS to keep for target search (catches multi-week bases)
    "month_lookback": 12,         # how many completed MONTHS to keep for target search (catches multi-month bases)
    "rel_vol_lookback": 20,       # bars for the relative-volume average
    "rel_vol_mult": 1.30,
    "body_frac_min": 0.50,        # aggression proxy: min body/range fraction
    "min_score": 0.0,
    "check_oi_walls": False,      # optional: reinforces target/invalidation with options OI walls
    "check_uae_framework": False,  # optional: your UAE Framework's RSIdiff90/MACD exhaustion check
    "check_period_consolidation": False,  # optional: was this a REAL multi-period base, not just yesterday's range
    "consolidation_period_unit": "1w",    # '1d' | '1w' | '1m' -- the granularity to check flatness at
    "consolidation_lookback_n": 4,        # how many completed periods must all be flat
    "consolidation_max_change_pct": 0.05, # 0.05 = 5% max period-over-period move to count as "flat"
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


def _load_history(symbol, tf="1h"):
    if _sb_history is None:
        return None
    df = _sb_history(symbol, tf)
    if df is None or df.empty:
        return None
    df = df.rename(columns={c: c.lower() for c in df.columns})
    needed = {"open", "high", "low", "close"}
    if not needed.issubset(set(df.columns)):
        return None
    if "volume" not in df.columns:
        df["volume"] = 0.0
    return df


# ── Volume-at-price (same algorithm as the Pine indicator's f_profile) ──

def _volume_profile(df, rows=24, va_pct=0.70):
    """Bins by (high+low)/2 midpoint, weighted by volume. POC = the bin
    with the most volume. Value area = expand outward from POC until
    va_pct of total volume is covered. Returns None if there isn't enough
    range/volume to compute anything meaningful."""
    if df is None or df.empty:
        return None
    lo = float(df["low"].min())
    hi = float(df["high"].max())
    if not (hi > lo):
        return None
    step = (hi - lo) / rows
    mid = ((df["high"] + df["low"]) / 2.0).to_numpy()
    vol = df["volume"].fillna(0).to_numpy(dtype=float)
    idx = np.clip(((mid - lo) / step).astype(int), 0, rows - 1)
    bins = np.zeros(rows, dtype=float)
    np.add.at(bins, idx, vol)
    total = bins.sum()
    if total <= 0:
        return None
    poc_idx = int(np.argmax(bins))
    poc = lo + (poc_idx + 0.5) * step
    target_vol = total * va_pct
    cum = bins[poc_idx]
    left = right = poc_idx
    while cum < target_vol and (left > 0 or right < rows - 1):
        next_left = bins[left - 1] if left > 0 else -1.0
        next_right = bins[right + 1] if right < rows - 1 else -1.0
        if next_right >= next_left and right < rows - 1:
            right += 1
            cum += next_right
        elif left > 0:
            left -= 1
            cum += next_left
        else:
            break
    vah = lo + (right + 1) * step
    val = lo + left * step
    return {"poc": round(float(poc), 4), "vah": round(float(vah), 4), "val": round(float(val), 4)}


def _daily_buckets(df):
    """{date: sub_df}, ascending by date."""
    out = {}
    for day, g in df.groupby(df.index.date):
        out[day] = g
    return out


def _weekly_buckets(df):
    """{(iso_year, iso_week): sub_df}, ascending."""
    iso = df.index.isocalendar()
    key = pd.Series(list(zip(iso["year"], iso["week"])), index=df.index)
    out = {}
    for wk, g in df.groupby(key):
        out[wk] = g
    return out


def _monthly_buckets(df):
    """{(year, month): sub_df}, ascending."""
    key = pd.Series(list(zip(df.index.year, df.index.month)), index=df.index)
    out = {}
    for mo, g in df.groupby(key):
        out[mo] = g
    return out


def _nearest_poc_target_tiered(price, direction, day_pool, week_pool, month_pool):
    """Searches Day + Week + Month completed-POC pools TOGETHER for the
    nearest one beyond `price` in `direction` ('above' or 'below'). This is
    what lets the scanner find a target even when the closest real prior
    balance area is a multi-week or multi-month base, not just something
    from the last ~10 trading days -- a stock breaking out of a long
    consolidation has its most meaningful prior POC sitting in the Week or
    Month pool, not the Day one. Returns (value, source_tier) so the caller
    can show which tier actually produced the target; (None, None) if
    nothing qualifies in any tier."""
    candidates = []
    for v in day_pool:
        if v is not None:
            candidates.append((v, "day"))
    for v in week_pool:
        if v is not None:
            candidates.append((v, "week"))
    for v in month_pool:
        if v is not None:
            candidates.append((v, "month"))
    if direction == "above":
        pool = [(v, s) for v, s in candidates if v > price]
        if not pool:
            return None, None
        return min(pool, key=lambda x: x[0])
    pool = [(v, s) for v, s in candidates if v < price]
    if not pool:
        return None, None
    return max(pool, key=lambda x: x[0])


# ── Per-symbol classification (dual Trend / Mean-Reversion model) ──

def _classify_retest(df, level, direction, lookback_bars=20):
    """Fabio's rule, codified: 'never take the first drive' -- the first
    touch of a level is the one most likely to be a fakeout; a level that
    gets retested and holds (or that price has extended away from
    WITHOUT ever coming back) is a meaningfully different, more
    trustworthy situation. Walks back from the current bar to find where
    price first crossed `level` in `direction` ('above' or 'below'), then
    checks whether price has come back to touch/cross it again since.
    Returns one of:
      'not_broken'         -- current bar hasn't broken the level at all
      'first_touch'        -- broke it on THIS bar, no history yet -- the
                               exact situation Fabio says to wait out
      'retested'           -- broke it earlier, pulled back to touch it
                               again, and is still on the break side now --
                               the confirmed, higher-probability case
      'extended_no_retest' -- broke it earlier and never came back --
                               tradeable but lower-confidence than a real
                               retest, no pullback ever confirmed it
    """
    close = df["close"]
    n = len(df)
    broken = (close > level) if direction == "above" else (close < level)
    if not bool(broken.iloc[-1]):
        return "not_broken"

    first_break_idx = None
    window_start = max(0, n - 1 - lookback_bars)
    for i in range(n - 1, window_start - 1, -1):
        if not bool(broken.iloc[i]):
            first_break_idx = i + 1
            break
    if first_break_idx is None:
        first_break_idx = window_start  # broke before our lookback window -- treat as extended

    if first_break_idx >= n - 1:
        return "first_touch"

    segment = df.iloc[first_break_idx:n - 1]  # bars strictly between the break and now
    if direction == "above":
        retested = bool((segment["low"] <= level).any())
    else:
        retested = bool((segment["high"] >= level).any())
    return "retested" if retested else "extended_no_retest"


def _bar_aggression(df, rel_vol_lookback=20, rel_vol_mult=1.30, body_frac_min=0.50, bar=None):
    """Aggression proxy shared by every scan mode: relative volume +
    candle body dominance, in place of literal order-size data (see
    module docstring). `bar` defaults to the last row of `df`; pass a
    specific row when checking a bar other than the most recent one.
    Returns (bull_aggression, bear_aggression, last_vol, avg_vol)."""
    if bar is None:
        bar = df.iloc[-1]
    recent = df.tail(int(rel_vol_lookback))
    avg_vol = float(recent["volume"].mean()) if len(recent) else 0.0
    last_vol = float(bar["volume"])
    rel_vol_ok = avg_vol > 0 and last_vol >= avg_vol * float(rel_vol_mult)
    body = float(bar["close"] - bar["open"])
    bar_range = max(float(bar["high"] - bar["low"]), 1e-9)
    body_share = abs(body) / bar_range
    bull = body > 0 and body_share >= float(body_frac_min) and rel_vol_ok
    bear = body < 0 and body_share >= float(body_frac_min) and rel_vol_ok
    return bull, bear, last_vol, avg_vol


def scan_symbol(symbol, params=None):
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    df = _load_history(symbol, "1h")
    if df is None or len(df) < 30:
        return None

    days = _daily_buckets(df)
    sorted_days = sorted(days.keys())
    today = datetime.now().date()

    if sorted_days and sorted_days[-1] == today:
        developing_day = sorted_days[-1]
        completed_days = sorted_days[:-1]
    else:
        developing_day = None
        completed_days = sorted_days

    if not completed_days:
        return None  # not enough history for a prior-day profile yet

    lookback = int(p["lookback_days"])
    recent_completed = completed_days[-lookback:]
    d1_day = recent_completed[-1]
    d1_df = days[d1_day]
    d1_profile = _volume_profile(d1_df, int(p["profile_rows"]), float(p["value_area_pct"]))
    if not d1_profile:
        return None

    dev_df = days.get(developing_day) if developing_day else None
    last_bar = df.iloc[-1]
    last_close = float(last_bar["close"])

    poc, vah, val = d1_profile["poc"], d1_profile["vah"], d1_profile["val"]

    # Market state relative to yesterday's (last completed day's) value area.
    balanced = val <= last_close <= vah
    imbalanced_long = last_close > vah
    imbalanced_short = last_close < val

    bull_aggression, bear_aggression, last_vol, avg_vol = _bar_aggression(
        df, p["rel_vol_lookback"], p["rel_vol_mult"], p["body_frac_min"], bar=last_bar)

    # POC migration over the last few completed days -- is value itself
    # accepting higher or lower prices, not just where price is right now.
    poc_series = []
    for day in recent_completed:
        prof = _volume_profile(days[day], int(p["profile_rows"]), float(p["value_area_pct"]))
        if prof:
            poc_series.append(prof["poc"])
    poc_rising = len(poc_series) >= 2 and poc_series[-1] > poc_series[0]
    poc_falling = len(poc_series) >= 2 and poc_series[-1] < poc_series[0]

    # Week and Month POC pools -- this is what lets the scanner find a
    # target when a stock just broke out of a multi-week or multi-month
    # consolidation: the most meaningful prior balance area in that case
    # sits in one of these pools, not in the last ~10 trading days.
    #
    # Built from DAILY bars, not the hourly `df` used for the day-level
    # profile above. This matters: _load_history("1h") depth depends on
    # how much intraday history happens to be cached for this specific
    # symbol, which can be quite shallow for anything not recently
    # backfilled -- and week/month bucketing built on a shallow hourly
    # window silently produces values from whatever partial data exists
    # rather than the ACTUAL last completed week/month, with no error to
    # signal it (confirmed against a real symbol where the computed
    # week/month POC didn't correspond to any visible volume cluster on
    # the real weekly chart). Daily bars are the one data source this
    # entire app already relies on for deep history (same cache backing
    # _history("1w")/_history("1m")'s own resampling), so rebuilding the
    # week/month profile from daily OHLCV instead removes this failure
    # mode structurally rather than patching around one symbol's cache
    # state. Hourly stays the source for D0/D1 -- day-level profiles
    # genuinely need intrabar granularity that daily bars can't provide.
    daily_df = _load_history(symbol, "1d")
    week_poc_series = []
    month_poc_series = []
    w1_poc = None
    m1_poc = None
    if daily_df is not None and len(daily_df) >= 10:
        weeks = _weekly_buckets(daily_df)
        sorted_weeks = sorted(weeks.keys())
        this_week_key = (int(daily_df.index[-1].isocalendar().year), int(daily_df.index[-1].isocalendar().week))
        completed_weeks = [w for w in sorted_weeks if w != this_week_key][-int(p["week_lookback"]):]
        for wk in completed_weeks:
            prof = _volume_profile(weeks[wk], int(p["profile_rows"]), float(p["value_area_pct"]))
            if prof:
                week_poc_series.append(prof["poc"])
        w1_poc = week_poc_series[-1] if week_poc_series else None

        months = _monthly_buckets(daily_df)
        sorted_months = sorted(months.keys())
        this_month_key = (int(daily_df.index[-1].year), int(daily_df.index[-1].month))
        completed_months = [m for m in sorted_months if m != this_month_key][-int(p["month_lookback"]):]
        for mo in completed_months:
            prof = _volume_profile(months[mo], int(p["profile_rows"]), float(p["value_area_pct"]))
            if prof:
                month_poc_series.append(prof["poc"])
        m1_poc = month_poc_series[-1] if month_poc_series else None

    thesis = "No-Edge"
    side = None
    target = None
    target_source = None
    invalidation = None
    retest_state = None
    reasons = []

    tech_ctx = {"score_bonus": 0.0}
    oi_ctx = {"score_bonus": 0.0}

    if imbalanced_long and bull_aggression:
        retest_state = _classify_retest(df, vah, "above")
        thesis, side = "Trend-Long", "long"
        invalidation = vah
        target, target_source = _nearest_poc_target_tiered(last_close, "above", poc_series, week_poc_series, month_poc_series)
        reasons.append(f"Closed above yesterday's VAH ({vah:.2f}) with an aggressive buy candle "
                        f"({last_vol/avg_vol:.1f}x avg volume)" if avg_vol else "Closed above yesterday's VAH")
        if retest_state == "first_touch":
            thesis = "Trend-Long-Watch"
            reasons.append("First touch of VAH -- Fabio's rule: don't take the first drive. "
                            "Waiting for a retest that holds, or continued acceptance without snapping back.")
        elif retest_state == "retested":
            reasons.append("Retested VAH and held -- confirmed breakout, not a first-touch entry")
        elif retest_state == "extended_no_retest":
            reasons.append("Extended above VAH without ever pulling back to retest it -- tradeable, "
                            "but lower confidence than a confirmed retest")
        if poc_rising:
            reasons.append("POC has been rising over the last few sessions -- value accepting higher")
        if target_source in ("week", "month"):
            reasons.append(f"Nearest prior balance area is a {target_source}ly POC, not a recent daily one -- "
                            f"likely breaking out of a longer consolidation")
    elif imbalanced_short and bear_aggression:
        retest_state = _classify_retest(df, val, "below")
        thesis, side = "Trend-Short", "short"
        invalidation = val
        target, target_source = _nearest_poc_target_tiered(last_close, "below", poc_series, week_poc_series, month_poc_series)
        reasons.append(f"Closed below yesterday's VAL ({val:.2f}) with an aggressive sell candle "
                        f"({last_vol/avg_vol:.1f}x avg volume)" if avg_vol else "Closed below yesterday's VAL")
        if retest_state == "first_touch":
            thesis = "Trend-Short-Watch"
            reasons.append("First touch of VAL -- Fabio's rule: don't take the first drive. "
                            "Waiting for a retest that holds, or continued acceptance without snapping back.")
        elif retest_state == "retested":
            reasons.append("Retested VAL and held -- confirmed breakdown, not a first-touch entry")
        elif retest_state == "extended_no_retest":
            reasons.append("Extended below VAL without ever pulling back to retest it -- tradeable, "
                            "but lower confidence than a confirmed retest")
        if poc_falling:
            reasons.append("POC has been falling over the last few sessions -- value accepting lower")
        if target_source in ("week", "month"):
            reasons.append(f"Nearest prior balance area is a {target_source}ly POC, not a recent daily one -- "
                            f"likely breaking down out of a longer consolidation")
    elif balanced and dev_df is not None:
        day_low = float(dev_df["low"].min())
        day_high = float(dev_df["high"].max())
        if day_low < val and last_close > val and bull_aggression:
            thesis, side = "Reversion-Long", "long"
            target = poc
            target_source = "day"
            invalidation = day_low
            retest_state = "retested"  # the poke-then-reclaim IS the second drive Fabio waits for
            reasons.append(f"Poked below yesterday's VAL ({val:.2f}) then reclaimed it -- failed breakdown, "
                            f"targeting POC ({poc:.2f})")
        elif day_high > vah and last_close < vah and bear_aggression:
            thesis, side = "Reversion-Short", "short"
            target = poc
            target_source = "day"
            invalidation = day_high
            retest_state = "retested"
            reasons.append(f"Poked above yesterday's VAH ({vah:.2f}) then rejected -- failed breakout, "
                            f"targeting POC ({poc:.2f})")

    if thesis == "No-Edge":
        reasons.append("Inside balance with no confirmed break/reject and volume -- no edge right now")

    # 0-10 composite score: state clarity + volume confirmation + R:R,
    # then discounted for retest quality -- a first-touch "Watch" should
    # never score as high as a confirmed retest, even if everything else
    # about it looks identical, per Fabio's "never take the first drive" rule.
    score = 0.0
    if thesis != "No-Edge":
        score += 4.0  # a real classification at all
        if (side == "long" and bull_aggression) or (side == "short" and bear_aggression):
            score += 2.0
        if (side == "long" and poc_rising) or (side == "short" and poc_falling):
            score += 1.5
        if target is not None and invalidation is not None:
            risk = abs(last_close - invalidation)
            reward = abs(target - last_close)
            if risk > 0:
                rr = reward / risk
                score += min(2.5, rr / 3.0 * 2.5)

        oi_wall_info = None
        if p.get("check_oi_walls") and side is not None:
            oi_wall_info = _check_oi_walls(symbol, side, last_close, target, invalidation)
            score += oi_wall_info["score_delta"]
            reasons.extend(oi_wall_info["reasons"])

        uae_info = None
        if p.get("check_uae_framework") and side is not None:
            uae_info = _check_uae_framework(df, side)
            score += uae_info["score_delta"]
            reasons.extend(uae_info["reasons"])

        period_consol_info = None
        if p.get("check_period_consolidation") and side is not None:
            unit = p.get("consolidation_period_unit", "1w")
            n = int(p.get("consolidation_lookback_n", 4))
            thresh = float(p.get("consolidation_max_change_pct", 0.05))
            period_consol_info = _check_period_consolidation(symbol, unit, n, thresh)
            if period_consol_info["passed"]:
                score += 1.5
                reasons.append(
                    f"Price moved only {period_consol_info['total_move_pct']*100:.1f}% total over the last "
                    f"{period_consol_info['periods_checked']} completed {unit} periods (under your {thresh*100:.0f}% "
                    f"threshold) -- a real base, not just a recent poke"
                )
            else:
                score -= 1.0
                detail = f"moved {period_consol_info['total_move_pct']*100:.1f}% total" if period_consol_info["total_move_pct"] is not None else "not enough history"
                reasons.append(
                    f"Doesn't meet your consolidation requirement ({detail}, threshold {thresh*100:.0f}% over "
                    f"{n} {unit} periods) -- may just be a recent, less-tested move"
                )

        if retest_state == "first_touch":
            score *= 0.4
        elif retest_state == "extended_no_retest":
            score *= 0.85
    else:
        oi_wall_info = None
        uae_info = None
        period_consol_info = None  # was missing here -- every symbol without a clear
                                    # directional side (the else branch) hit an
                                    # UnboundLocalError at the return statement below,
                                    # which unconditionally references this variable
    score = round(max(0.0, min(10.0, score)), 1)

    return {
        "symbol": symbol,
        "date": str(sorted_days[-1]),
        "price": round(last_close, 4),
        "thesis": thesis,
        "side": side,
        "score": score,
        "retest_state": retest_state,
        "poc": poc, "vah": vah, "val": val,
        "week_poc": w1_poc,
        "month_poc": m1_poc,
        "target": round(target, 4) if target is not None else None,
        "target_source": target_source,
        "invalidation": round(invalidation, 4) if invalidation is not None else None,
        "poc_trend": "rising" if poc_rising else ("falling" if poc_falling else "flat"),
        "volume_confirmed": bool(bull_aggression or bear_aggression),
        "oi_wall": oi_wall_info,
        "uae_framework": uae_info,
        "period_consolidation": period_consol_info,
        "reasons": reasons,
    }


def get_chart_levels(symbol, params=None):
    """Lightweight companion to scan_symbol() for chart overlay use --
    returns just the reference levels (no thesis/classification/scoring)
    that the realtime dashboard draws as horizontal price lines: today's
    developing POC/VAH/VAL, D1 (yesterday, completed), W1 (last completed
    week), M1 (last completed month). Reuses the exact same bucketing and
    _volume_profile() binning as scan_symbol() -- one implementation of
    "what is the POC for this period," not a second copy that could drift.
    Kept deliberately minimal (4 levels, not the full day/week/month pools)
    after the lesson learned building the Pine indicator: more reference
    lines than that just clutters the chart without adding real signal."""
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    df = _load_history(symbol, "1h")
    if df is None or len(df) < 10:
        return None

    days = _daily_buckets(df)
    sorted_days = sorted(days.keys())
    today = datetime.now().date()
    if sorted_days and sorted_days[-1] == today:
        developing_day = sorted_days[-1]
        completed_days = sorted_days[:-1]
    else:
        developing_day = None
        completed_days = sorted_days

    out = {"developing": None, "d1": None, "w1": None, "m1": None}

    if developing_day is not None:
        out["developing"] = _volume_profile(days[developing_day], int(p["profile_rows"]), float(p["value_area_pct"]))

    if completed_days:
        out["d1"] = _volume_profile(days[completed_days[-1]], int(p["profile_rows"]), float(p["value_area_pct"]))

    # W1/M1 built from daily bars, same reasoning as scan_symbol() -- see
    # the comment there. Hourly `df` stays the source for developing/D1
    # only, where intrabar granularity actually matters.
    daily_df = _load_history(symbol, "1d")
    if daily_df is not None and len(daily_df) >= 10:
        weeks = _weekly_buckets(daily_df)
        sorted_weeks = sorted(weeks.keys())
        this_week_key = (int(daily_df.index[-1].isocalendar().year), int(daily_df.index[-1].isocalendar().week))
        completed_weeks = [w for w in sorted_weeks if w != this_week_key]
        if completed_weeks:
            out["w1"] = _volume_profile(weeks[completed_weeks[-1]], int(p["profile_rows"]), float(p["value_area_pct"]))

        months = _monthly_buckets(daily_df)
        sorted_months = sorted(months.keys())
        this_month_key = (int(daily_df.index[-1].year), int(daily_df.index[-1].month))
        completed_months = [m for m in sorted_months if m != this_month_key]
        if completed_months:
            out["m1"] = _volume_profile(months[completed_months[-1]], int(p["profile_rows"]), float(p["value_area_pct"]))

    return out


def scan_watchlist(watchlist_id=None, params=None):
    symbols = _get_symbols(watchlist_id)
    results = []
    errors = {}
    for sym in symbols:
        try:
            r = scan_symbol(sym, params)
            if r:
                results.append(r)
        except Exception as e:
            errors[sym] = str(e)
    min_score = float((params or {}).get("min_score", 0) or 0)
    results = [r for r in results if r["score"] >= min_score]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results, errors


# ── S/R breakout scan mode (any timeframe) ──
# A second trigger philosophy sharing all the same machinery above: instead
# of triggering off yesterday's VAH/VAL, this triggers off a classic
# support/resistance level (same tv_sr_channels pivot-clustering already
# used for the realtime chart's S/R overlay) that price just broke out of
# AFTER genuinely consolidating near it -- not just any touch of an old
# level from weeks back. Once triggered, it profiles the CONSOLIDATION
# ITSELF (the exact "range volume profile" idea from the chart tooling)
# for its POC, checks a proper CVD trend (not just the single-bar
# aggression proxy) over the consolidation, and applies the same
# _classify_retest() discipline as the VAH/VAL scanner. Timeframe is a
# parameter, not hardcoded -- "1h" default, but any _history()-supported
# timeframe works (5m/15m/1h/4h/1d/1w).

SR_DEFAULT_PARAMS = {
    "sr_prd": 10,             # tv_sr_channels pivot lookback
    "sr_channel_w_pct": 5.0,  # tv_sr_channels channel width
    "sr_loopback": 200,       # tv_sr_channels how far back to look for pivots
    "consolidation_bars": 15,  # how many bars right before the breakout must have hugged the level
    "consolidation_min_frac": 0.60,  # fraction of those bars that must be "near" the level
    "profile_rows": 24,
    "value_area_pct": 0.70,
    "rel_vol_lookback": 20,
    "rel_vol_mult": 1.30,
    "body_frac_min": 0.50,
    "cvd_lookback": 20,
    "retest_lookback_bars": 20,
    "min_score": 0.0,
    "check_oi_walls": False,
    "check_uae_framework": False,
    "check_period_consolidation": False,
    "consolidation_period_unit": "1w",
    "consolidation_lookback_n": 4,
    "consolidation_max_change_pct": 0.05,
}


def _is_consolidating_near(df, level_lo, level_hi, lookback_bars=15, min_frac=0.60):
    """True if, over the `lookback_bars` immediately BEFORE the current
    (possible breakout) bar, price spent most of its time near
    [level_lo, level_hi] -- i.e. this was a real base the level was
    built from, not a level touched once weeks ago that price is only
    now approaching again."""
    if len(df) < lookback_bars + 2:
        return False
    recent = df.iloc[-(lookback_bars + 1):-1]  # excludes the current/breakout bar itself
    if recent.empty:
        return False
    pad = max((level_hi - level_lo) * 0.5, 1e-9)
    inside = ((recent["close"] >= level_lo - pad) & (recent["close"] <= level_hi + pad)).mean()
    return bool(inside >= min_frac)


def _cvd_trend(df, lookback_bars=20):
    """Aggregate CVD proxy over a window (not just one bar's aggression):
    same close-location-value formula used elsewhere in this codebase,
    cumulative-summed across the window. Returns ('rising'|'falling'|'flat',
    net_delta) -- whether order flow over this window net favored buyers
    or sellers, matching the Pine indicator's CVD confluence check."""
    seg = df.tail(int(lookback_bars))
    if len(seg) < 2:
        return "flat", 0.0
    rng = (seg["high"] - seg["low"]).clip(lower=1e-9)
    clv = (2 * (seg["close"] - seg["low"]) / rng) - 1.0
    delta = (clv * seg["volume"]).sum()
    direction = "rising" if delta > 0 else ("falling" if delta < 0 else "flat")
    return direction, float(delta)


# ── Optional confluence layers -- OFF by default, added onto any scan
# mode's result rather than gating it. Both fail SILENTLY (return "not
# available", never raise) if the underlying data doesn't exist for a
# symbol (e.g. OI walls need options chain data; UAE framework needs
# enough bar history for RSIdiff90's warm-up) -- an optional layer that
# can crash the whole scan for symbols missing that data would be worse
# than just not having it. ──

def _check_oi_walls(symbol, side, price, target, invalidation):
    """Reuses the SAME get_oi_walls() service already used by
    scanner_builder.py and the realtime dashboard -- not a re-derivation.
    Checks two things: is there a wall reinforcing the invalidation side
    (dealer/large-OI support under a long, resistance over a short --
    makes the stop level more meaningful), and is there a wall sitting
    BETWEEN price and target that could cap the move before it gets there
    (pin risk)."""
    try:
        from ..services.oi_wall_service import get_oi_walls
        walls = get_oi_walls(symbol)
    except Exception as e:
        return {"available": False, "error": str(e), "reasons": [], "score_delta": 0.0}
    if not walls:
        return {"available": False, "error": "No options OI data for this symbol", "reasons": [], "score_delta": 0.0}

    reasons = []
    score_delta = 0.0
    put_walls = walls.get("put_walls") or []
    call_walls = walls.get("call_walls") or []

    def _nearest(walls_list, ref_price):
        if not walls_list:
            return None
        return min(walls_list, key=lambda w: abs(w["strike"] - ref_price))

    if side == "long":
        support_wall = _nearest(put_walls, invalidation)
        if support_wall and abs(support_wall["strike"] - invalidation) / max(invalidation, 1e-9) < 0.02:
            reasons.append(f"Put OI wall at {support_wall['strike']} sits right at the invalidation level -- "
                            f"dealer positioning reinforces that floor")
            score_delta += 0.5
        capping_wall = _nearest(call_walls, target) if target else None
        if capping_wall and price < capping_wall["strike"] < (target or float("inf")):
            reasons.append(f"Call OI wall at {capping_wall['strike']} sits between price and target -- "
                            f"real pin/resistance risk before the move gets there")
            score_delta -= 0.5
    else:
        resistance_wall = _nearest(call_walls, invalidation)
        if resistance_wall and abs(resistance_wall["strike"] - invalidation) / max(invalidation, 1e-9) < 0.02:
            reasons.append(f"Call OI wall at {resistance_wall['strike']} sits right at the invalidation level -- "
                            f"dealer positioning reinforces that ceiling")
            score_delta += 0.5
        capping_wall = _nearest(put_walls, target) if target else None
        if capping_wall and (target or 0) < capping_wall["strike"] < price:
            reasons.append(f"Put OI wall at {capping_wall['strike']} sits between price and target -- "
                            f"real pin/support risk before the move gets there")
            score_delta -= 0.5

    return {
        "available": True,
        "gamma_wall": walls.get("gamma_wall"),
        "pcr": walls.get("pcr"),
        "reasons": reasons,
        "score_delta": score_delta,
    }


def _check_period_consolidation(symbol, period_unit="1w", lookback_n=4, max_change_pct=0.05):
    """'Has price moved less than X% over the last N periods' -- measures
    TOTAL displacement across the whole lookback window (highest high to
    lowest low over those N completed periods, as a % of the window's
    starting close), NOT each individual period's own move. Deliberately
    range-based rather than pure start-vs-end net change: net change can
    be fooled by a round trip (up 8% then down 8% nets to ~0% while
    genuinely NOT consolidating) -- range correctly still flags that as
    not consolidated. Distinct from RangeCompression (today's bar vs. a
    moving average of recent range) and from scan_symbol_sr's
    _is_consolidating_near (bar-count + price-band proximity) -- this is
    a direct 'how far did price actually travel over this whole stretch'
    test, closer to how a genuine multi-week/month base actually looks
    (e.g. OUST's multi-month consolidation before its breakout week). The
    current/still-forming period is always excluded (same conservative
    convention as every other completed-vs-developing check in this
    module) -- only fully closed periods count toward 'has consolidated.'
    Returns {"passed": bool, "total_move_pct": float|None, "periods_checked": int}."""
    df = _load_history(symbol, period_unit)
    needed = lookback_n + 1  # +1 to exclude the still-forming period
    if df is None or len(df) < needed:
        return {"passed": False, "total_move_pct": None, "periods_checked": 0}
    window = df.iloc[-(lookback_n + 1):-1]  # last lookback_n COMPLETED periods
    if len(window) < lookback_n:
        return {"passed": False, "total_move_pct": None, "periods_checked": len(window)}
    hi = float(window["high"].max())
    lo = float(window["low"].min())
    ref = float(window["close"].iloc[0])  # window's starting close, as the reference base
    if ref <= 0:
        return {"passed": False, "total_move_pct": None, "periods_checked": len(window)}
    total_move_pct = (hi - lo) / ref
    passed = total_move_pct <= max_change_pct
    return {"passed": passed, "total_move_pct": round(total_move_pct, 4), "periods_checked": len(window)}


def _check_uae_framework(df, side, rsidiff_period=90, hist_shrink_bars=5):
    """Your UAE Unified Framework's actual MRT exhaustion conditions,
    reusing the SAME formulas as scanner_builder.py's RSIdiff90 primitive
    and the standard 12/26/9 MACD -- not a re-derivation. MRT LONG wants
    RSIdiff90 having been oversold and now turning up, with the MACD
    histogram shrinking (bearish momentum fading); MRT SHORT is the
    mirror. This does NOT replace scanner_builder.py's own RSIdiff90 --
    it's a lightweight local copy so this module doesn't need to import
    the full DSL engine for two numbers. Returns "not available" (never
    raises) if there isn't enough bar history for RSIdiff90's warm-up
    (~6x period, i.e. ~540 bars at the default period=90 on daily bars --
    the exact requirement documented on the DSL primitive itself)."""
    from .scanner_builder import _rsi as _sb_rsi, _macd as _sb_macd

    min_bars = int(rsidiff_period) * 6
    if len(df) < min_bars:
        return {"available": False, "error": f"Needs ~{min_bars} bars for RSIdiff90 warm-up, have {len(df)}",
                "reasons": [], "score_delta": 0.0}

    rsi14 = _sb_rsi(df["close"], 14)
    rsidiff = rsi14 - rsi14.ewm(span=int(rsidiff_period), adjust=False).mean()
    _, _, hist = _sb_macd(df["close"])

    last_rsidiff = float(rsidiff.iloc[-1])
    hist_recent = hist.tail(int(hist_shrink_bars) + 1)
    hist_shrinking_bull = bool((hist_recent.diff().dropna() < 0).all()) if hist_recent.iloc[0] > 0 else False
    hist_shrinking_bear = bool((hist_recent.diff().dropna() > 0).all()) if hist_recent.iloc[0] < 0 else False

    reasons = []
    score_delta = 0.0
    if side == "long":
        oversold_turning = last_rsidiff < -8 and float(rsidiff.iloc[-1]) > float(rsidiff.iloc[-2])
        if oversold_turning:
            reasons.append(f"RSIdiff90 at {last_rsidiff:.1f} -- oversold and turning up, matches your MRT LONG trigger")
            score_delta += 1.0
        elif last_rsidiff > 8:
            reasons.append(f"RSIdiff90 at {last_rsidiff:.1f} -- actually OVERBOUGHT, not oversold. "
                            f"Doesn't match a long MRT reversal -- worth double-checking before entering")
            score_delta -= 1.0
        if hist_shrinking_bear:
            reasons.append("MACD histogram shrinking on the bear side -- downside momentum fading")
            score_delta += 0.5
    else:
        overbought_turning = last_rsidiff > 8 and float(rsidiff.iloc[-1]) < float(rsidiff.iloc[-2])
        if overbought_turning:
            reasons.append(f"RSIdiff90 at {last_rsidiff:.1f} -- overbought and turning down, matches your MRT SHORT trigger")
            score_delta += 1.0
        elif last_rsidiff < -8:
            reasons.append(f"RSIdiff90 at {last_rsidiff:.1f} -- actually OVERSOLD, not overbought. "
                            f"Doesn't match a short MRT reversal -- worth double-checking before entering")
            score_delta -= 1.0
        if hist_shrinking_bull:
            reasons.append("MACD histogram shrinking on the bull side -- upside momentum fading")
            score_delta += 0.5

    return {
        "available": True,
        "rsidiff90": round(last_rsidiff, 2),
        "reasons": reasons,
        "score_delta": score_delta,
    }


def scan_symbol_sr(symbol, timeframe="1h", params=None):
    p = dict(SR_DEFAULT_PARAMS)
    if params:
        p.update(params)

    df = _load_history(symbol, timeframe)
    if df is None or len(df) < max(60, int(p["consolidation_bars"]) + 30):
        return None

    from ..charts.chart_primitives import tv_sr_channels
    sr_df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    channels = tv_sr_channels(
        sr_df, prd=int(p["sr_prd"]), channel_w_pct=float(p["sr_channel_w_pct"]),
        loopback=int(p["sr_loopback"]), max_sr=10,
    )
    if not channels:
        return None

    last_bar = df.iloc[-1]
    last_close = float(last_bar["close"])

    # Find every channel price has genuinely broken (any retest_state other
    # than 'not_broken'), using the SAME walk-back logic _classify_retest
    # already uses elsewhere -- far more robust against ordinary
    # consolidation noise than a fixed "N bars back" snapshot check, which
    # can misfire if price randomly poked to the far side of the channel
    # at some point during the consolidation itself.
    candidates = []  # (channel, direction, side, retest_state, level_price)
    for c in channels:
        if last_close > c["hi"]:
            rst = _classify_retest(df, c["hi"], "above", int(p["retest_lookback_bars"]))
            if rst != "not_broken":
                candidates.append((c, "above", "long", rst, c["hi"]))
        elif last_close < c["lo"]:
            rst = _classify_retest(df, c["lo"], "below", int(p["retest_lookback_bars"]))
            if rst != "not_broken":
                candidates.append((c, "below", "short", rst, c["lo"]))

    if not candidates:
        return None  # nothing genuinely broken right now

    level_channel, direction, side, retest_state, level_price = min(
        candidates, key=lambda t: abs(last_close - t[4]))

    consolidating = _is_consolidating_near(
        df, level_channel["lo"], level_channel["hi"],
        int(p["consolidation_bars"]), float(p["consolidation_min_frac"]),
    )
    if not consolidating:
        return None  # broke a level, but it wasn't a real base -- not what this scan mode is for

    bull_aggression, bear_aggression, last_vol, avg_vol = _bar_aggression(
        df, p["rel_vol_lookback"], p["rel_vol_mult"], p["body_frac_min"], bar=last_bar)
    aggression_ok = bull_aggression if side == "long" else bear_aggression
    if not aggression_ok:
        return None

    # Profile the CONSOLIDATION ITSELF -- the exact "range volume profile"
    # analysis from the chart tooling, done programmatically: what's the
    # POC of the base this breakout came from?
    consolidation_window = df.iloc[-(int(p["consolidation_bars"]) + 1):-1]
    consolidation_profile = _volume_profile(consolidation_window, int(p["profile_rows"]), float(p["value_area_pct"]))

    cvd_direction, cvd_delta = _cvd_trend(df, int(p["cvd_lookback"]))
    cvd_supports = (cvd_direction == "rising") if side == "long" else (cvd_direction == "falling")

    # Where does POC sit WITHIN the S/R channel's own range (NOT the
    # consolidation profile's own value area -- that's centered on its own
    # POC by construction and would always read ~0.5 regardless of where
    # in the channel it actually sits, which was a real bug caught by a
    # direct top-heavy-vs-bottom-heavy synthetic test before this shipped).
    # poc_position: 0.0 = POC at the bottom of the channel, 1.0 = at the
    # top. For a LONG breakout, POC near the TOP means most of the base's
    # volume already traded close to the breakout price -- less
    # "unfinished business" left behind, lower odds of a full round-trip
    # back through the range before continuing. POC near the BOTTOM means
    # most trading happened far below the breakout -- more of the base is
    # still unresolved, so a deeper pullback before continuation is more
    # likely. Exactly mirrored for a SHORT breakdown (favor POC near the
    # bottom of the channel).
    poc_position = None
    poc_alignment = 0.0
    if consolidation_profile:
        chan_span = max(level_channel["hi"] - level_channel["lo"], 1e-9)
        poc_position = (consolidation_profile["poc"] - level_channel["lo"]) / chan_span
        poc_position = max(0.0, min(1.0, poc_position))
        poc_alignment = poc_position if side == "long" else (1.0 - poc_position)

    thesis = f"SR-Breakout-{'Long' if side == 'long' else 'Short'}"
    reasons = [
        f"Broke {'above resistance' if side == 'long' else 'below support'} at "
        f"{level_price:.2f} (channel strength {level_channel.get('strength', '?')}) "
        f"after consolidating there for ~{p['consolidation_bars']} bars on {timeframe}",
    ]
    if not aggression_ok:
        reasons.append("Break lacked volume/body confirmation")
    if consolidation_profile:
        reasons.append(f"Consolidation POC at {consolidation_profile['poc']:.2f} -- likely magnet on any pullback")
        if poc_alignment >= 0.65:
            where = "near the top" if side == "long" else "near the bottom"
            reasons.append(f"POC sits {where} of the base, right by the breakout side -- most of the range's "
                            f"volume already traded near current levels, less unfinished business left behind")
        elif poc_alignment <= 0.35:
            where = "low in the base, well below the breakout" if side == "long" else "high in the base, well above the breakdown"
            reasons.append(f"POC sits {where} -- a large share of the base's volume never traded near the "
                            f"breakout price, real chance of a deeper pullback to fill that in first")
    if cvd_supports:
        reasons.append(f"CVD {cvd_direction} over the last {p['cvd_lookback']} bars -- order flow supports the move")
    else:
        reasons.append(f"CVD {cvd_direction} over the last {p['cvd_lookback']} bars -- NOT confirming the move, be cautious")

    if retest_state == "first_touch":
        thesis += "-Watch"
        reasons.append("First touch of the level -- don't take the first drive, wait for a retest or continued hold")
    elif retest_state == "retested":
        reasons.append("Retested the level and held -- confirmed, not a first-touch entry")
    elif retest_state == "extended_no_retest":
        reasons.append("Extended away without a retest -- tradeable, lower confidence than a confirmed retest")

    invalidation = level_channel["lo"] if side == "long" else level_channel["hi"]
    target = consolidation_profile["poc"] if consolidation_profile else None
    # If price already has more room than just the base's own POC (a
    # measured-move-style extension: base height projected from the
    # breakout point), offer that as a secondary reference too.
    base_height = level_channel["hi"] - level_channel["lo"]
    measured_move = last_close + base_height if side == "long" else last_close - base_height

    # Rebalanced so the four independent factors -- classification itself,
    # aggression, CVD, base structure (POC position), and R:R -- sum to a
    # sensible 10 before the retest-quality discount is applied.
    score = 0.0
    score += 3.5
    if aggression_ok:
        score += 1.5
    if cvd_supports:
        score += 1.5
    score += poc_alignment * 1.5  # continuous 0..1.5, not a binary bonus
    if target is not None:
        risk = abs(last_close - invalidation)
        reward = abs(target - last_close)
        if risk > 0:
            score += min(2.0, (reward / risk) / 3.0 * 2.0)

    oi_wall_info = None
    if p.get("check_oi_walls"):
        oi_wall_info = _check_oi_walls(symbol, side, last_close, target, invalidation)
        score += oi_wall_info["score_delta"]
        reasons.extend(oi_wall_info["reasons"])

    uae_info = None
    if p.get("check_uae_framework"):
        uae_info = _check_uae_framework(df, side)
        score += uae_info["score_delta"]
        reasons.extend(uae_info["reasons"])

    # Complements (doesn't replace) the bar-count + price-band
    # consolidation check already REQUIRED to reach this point (see
    # _is_consolidating_near above) -- that one asks "did price stay near
    # this level," this one asks "was period-over-period movement
    # actually small the whole time." A base can satisfy one without the
    # other (e.g. a wide, choppy range that still stays "near" a level
    # wouldn't pass this stricter flatness test), so running both when
    # enabled is a genuinely stronger, not redundant, confirmation.
    period_consol_info = None
    if p.get("check_period_consolidation"):
        unit = p.get("consolidation_period_unit", "1w")
        n = int(p.get("consolidation_lookback_n", 4))
        thresh = float(p.get("consolidation_max_change_pct", 0.05))
        period_consol_info = _check_period_consolidation(symbol, unit, n, thresh)
        if period_consol_info["passed"]:
            score += 1.5
            reasons.append(
                f"Price moved only {period_consol_info['total_move_pct']*100:.1f}% total over the last "
                f"{period_consol_info['periods_checked']} completed {unit} periods (under your {thresh*100:.0f}% "
                f"threshold) -- a real base, not just a recent poke"
            )
        else:
            score -= 1.0
            detail = f"moved {period_consol_info['total_move_pct']*100:.1f}% total" if period_consol_info["total_move_pct"] is not None else "not enough history"
            reasons.append(
                f"Doesn't meet your consolidation requirement ({detail}, threshold {thresh*100:.0f}% over "
                f"{n} {unit} periods) -- may just be a recent, less-tested move"
            )

    if retest_state == "first_touch":
        score *= 0.4
    elif retest_state == "extended_no_retest":
        score *= 0.85
    score = round(max(0.0, min(10.0, score)), 1)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "date": str(df.index[-1].date()),
        "price": round(last_close, 4),
        "thesis": thesis,
        "side": side,
        "score": score,
        "retest_state": retest_state,
        "oi_wall": oi_wall_info,
        "uae_framework": uae_info,
        "period_consolidation": period_consol_info,
        "level": round(level_price, 4),
        "level_strength": level_channel.get("strength"),
        "consolidation_poc": round(consolidation_profile["poc"], 4) if consolidation_profile else None,
        "consolidation_vah": round(consolidation_profile["vah"], 4) if consolidation_profile else None,
        "consolidation_val": round(consolidation_profile["val"], 4) if consolidation_profile else None,
        "poc_position": round(poc_position, 3) if poc_position is not None else None,
        "cvd_direction": cvd_direction,
        "cvd_supports": bool(cvd_supports),
        "target": round(target, 4) if target is not None else None,
        "measured_move": round(measured_move, 4),
        "invalidation": round(invalidation, 4),
        "volume_confirmed": bool(aggression_ok),
        "reasons": reasons,
    }


def scan_watchlist_sr(watchlist_id=None, timeframe="1h", params=None):
    symbols = _get_symbols(watchlist_id)
    results = []
    errors = {}
    for sym in symbols:
        try:
            r = scan_symbol_sr(sym, timeframe, params)
            if r:
                results.append(r)
        except Exception as e:
            errors[sym] = str(e)
    min_score = float((params or {}).get("min_score", 0) or 0)
    results = [r for r in results if r["score"] >= min_score]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results, errors


# ── Custom-query-driven scan mode ──
# A third, genuinely different entry point from scan_symbol/scan_symbol_sr:
# those two have a FIXED trigger philosophy baked in (VAH/VAL break, or S/R
# break). This one lets the QUERY ITSELF define the setup -- any
# scanner_builder.py DSL expression, e.g. a consolidation check like
# `(High[1w] - High[1w][4]) / High[1w] < 0.1` -- and infers the lookback
# window to profile directly from the query's own [N] bar-shift syntax
# (IndexNode), rather than requiring a separate duration parameter. "Fetch
# the query, then apply oldest to most recent" -- exactly the window the
# query itself was checking gets profiled, no re-specification needed.

def _extract_query_duration(query_text):
    """Parses a scanner_builder.py DSL query for [N] bar-shift index
    expressions (IndexNode -- the `expr[N]` syntax) to find the widest
    implied lookback window and its timeframe. Also recognizes the
    Shift(expr, N)/priorDay(expr, N) function-call form as an
    alternative way of expressing the same thing. Returns
    (timeframe, periods) or (None, None) if no such reference is found
    (caller should fall back to an explicit duration parameter in that
    case, same pattern as the rest of this module's optional checks)."""
    try:
        from .scanner_builder import (
            _parse_query, _expand_scan_nodes, IdentifierNode, FuncCallNode,
            UnaryNode, BinaryNode, CompareNode, IndexNode, NumberNode,
        )
        root = _expand_scan_nodes(_parse_query(query_text), ())
    except Exception:
        return None, None

    best = {"tf": None, "n": 0}

    def find_tf(node):
        if isinstance(node, IdentifierNode):
            return node.tf
        if isinstance(node, IndexNode):
            return find_tf(node.expr)
        if isinstance(node, FuncCallNode):
            for a in node.args:
                tf = find_tf(a)
                if tf:
                    return tf
            return None
        if isinstance(node, (BinaryNode, CompareNode)):
            return find_tf(node.left) or find_tf(node.right)
        if isinstance(node, UnaryNode):
            return find_tf(node.expr)
        return None

    def walk(node):
        if isinstance(node, IndexNode):
            n_val = int(node.bars)
            if n_val > best["n"]:
                best["n"] = n_val
                best["tf"] = find_tf(node.expr) or "1d"
            walk(node.expr)
        elif isinstance(node, FuncCallNode):
            fname = node.name.lower().strip()
            if fname in ("shift", "priorday") and len(node.args) >= 2 and isinstance(node.args[1], NumberNode):
                n_val = int(node.args[1].value)
                if n_val > best["n"]:
                    best["n"] = n_val
                    best["tf"] = find_tf(node.args[0]) or "1d"
            for a in node.args:
                walk(a)
        elif isinstance(node, (BinaryNode, CompareNode)):
            walk(node.left)
            walk(node.right)
        elif isinstance(node, UnaryNode):
            walk(node.expr)

    walk(root)
    return (best["tf"], best["n"]) if best["n"] else (None, None)


def _run_custom_query(query_text, watchlist_id=None, benchmark="SPY", limit=250):
    """Runs an arbitrary scanner_builder.py DSL query against a watchlist
    by calling the EXISTING /scanner-builder/api/run endpoint internally
    (Flask's test client), rather than reimplementing its concurrent
    execution, snapshot-free prefiltering, and per-scan timeout handling
    here -- that machinery is substantial and already tuned; duplicating
    it would be a real correctness/maintenance risk for no benefit.
    Returns (matching_symbols: List[str], error: str|None)."""
    try:
        from flask import current_app
        client = current_app.test_client()
        resp = client.post("/scanner-builder/api/run", json={
            "query_text": query_text, "watchlist_id": watchlist_id,
            "benchmark": benchmark, "limit": limit,
        })
        data = resp.get_json() or {}
        if resp.status_code != 200:
            return [], data.get("error", f"Scanner query failed (HTTP {resp.status_code})")
        symbols = [r.get("symbol") for r in (data.get("results") or []) if r.get("symbol")]
        return symbols, None
    except Exception as e:  # noqa: BLE001
        return [], str(e)


def _profile_query_window(symbol, period_unit, lookback_n, params):
    """Profiles the last `lookback_n` COMPLETED periods at `period_unit`
    granularity (oldest to most recent, excluding the still-forming
    current period) for POC/VAH/VAL, then classifies the CURRENT price
    (including the still-forming period, since that's genuinely where
    price is right now) against that profile."""
    df = _load_history(symbol, period_unit)
    if df is None or len(df) < lookback_n + 1:
        return None
    window = df.iloc[-(lookback_n + 1):-1]
    if len(window) < lookback_n:
        return None

    # Profile from FINER underlying (daily) data covering the SAME
    # calendar range, not from the query's own period_unit bars
    # directly. With only `lookback_n` bars (e.g. 4 weekly candles),
    # _volume_profile()'s binning assigns each bar's ENTIRE volume to a
    # single price bin (its own high-low midpoint) -- a POC/VAH/VAL built
    # from just 4 data points, not a real day-to-day distribution. Same
    # fix already applied to scan_symbol's week/month POC pooling
    # elsewhere in this module: daily bars are the one data source
    # reliably deep enough to cover any window, so rebuild the profile
    # from those whenever the query's own granularity is coarser than
    # daily. Padded by one extra period on the start side since a
    # resampled weekly/monthly bar's index label can represent either
    # end of its span depending on the resampling convention -- safer to
    # include a few extra days than silently clip the true start of the
    # window and miss real volume that belongs in this profile.
    profile_df = window
    if period_unit != "1d":
        daily_df = _load_history(symbol, "1d")
        if daily_df is not None and len(daily_df) > 0:
            pad = {"1w": pd.Timedelta(weeks=1), "1m": pd.Timedelta(days=31)}.get(period_unit, pd.Timedelta(weeks=1))
            range_start = window.index[0] - pad
            range_end = window.index[-1]
            fine_window = daily_df[(daily_df.index >= range_start) & (daily_df.index <= range_end)]
            if len(fine_window) >= lookback_n:  # only switch if this genuinely adds granularity
                profile_df = fine_window

    profile = _volume_profile(profile_df, int(params.get("profile_rows", 24)), float(params.get("value_area_pct", 0.70)))
    if not profile:
        return None
    poc, vah, val = profile["poc"], profile["vah"], profile["val"]
    last_close = float(df["close"].iloc[-1])
    window_lo = float(profile_df["low"].min())
    window_hi = float(profile_df["high"].max())

    retest_lookback = max(int(params.get("retest_lookback_bars", 20)), lookback_n * 3)
    reasons = [
        f"Profiled the last {lookback_n} completed {period_unit} periods (your query's own implied window) -- "
        f"POC {poc:.2f}, VAH {vah:.2f}, VAL {val:.2f}"
    ]
    state, side, retest_state, invalidation, target = "Inside-Range", None, None, None, None
    score = 0.0

    if last_close > vah:
        state, side = "Above-VAH-Long", "long"
        retest_state = _classify_retest(df, vah, "above", retest_lookback)
        invalidation, target = vah, poc
        reasons.append(f"Price ({last_close:.2f}) is above the window's VAH -- broke out of the range this query found")
        score += 4.0
        if retest_state == "first_touch":
            state += "-Watch"
            score *= 0.4
            reasons.append("First touch of VAH -- don't take the first drive, wait for a retest or hold")
        elif retest_state == "retested":
            score += 2.0
            reasons.append("Retested VAH and held -- confirmed, not a first-touch entry")
        elif retest_state == "extended_no_retest":
            score *= 0.85
            reasons.append("Extended above VAH without a retest -- tradeable, lower confidence")
    elif last_close < val:
        state, side = "Below-VAL-Short", "short"
        retest_state = _classify_retest(df, val, "below", retest_lookback)
        invalidation, target = val, poc
        reasons.append(f"Price ({last_close:.2f}) is below the window's VAL -- broke down out of the range this query found")
        score += 4.0
        if retest_state == "first_touch":
            state += "-Watch"
            score *= 0.4
            reasons.append("First touch of VAL -- don't take the first drive, wait for a retest or hold")
        elif retest_state == "retested":
            score += 2.0
            reasons.append("Retested VAL and held -- confirmed, not a first-touch entry")
        elif retest_state == "extended_no_retest":
            score *= 0.85
            reasons.append("Extended below VAL without a retest -- tradeable, lower confidence")
    else:
        reasons.append(f"Price ({last_close:.2f}) is still inside the range -- no breakout yet, POC {poc:.2f} is the fair-value reference")

    # Explicit, structured trade guidance -- distinct from `reasons`
    # (prose, for context) and `thesis` (a label, not an instruction).
    # This exists specifically so the UI can render one unambiguous line
    # ("ENTER LONG NOW" vs "WAIT for retest of $X, then enter") instead
    # of making the person infer next steps from a badge + a bullet list,
    # and so a "wait for retest" state carries exactly the (symbol,
    # price, operator) a one-click price alert needs -- no separate
    # judgment call required to translate "wait for retest" into an
    # actual alert configuration.
    if side is None:
        action = {
            "type": "no_edge",
            "message": f"No edge yet -- price is inside the range. A break above VAH {vah:.2f} or below VAL {val:.2f} is what to watch for.",
            "alert_price": None, "alert_operator": None,
        }
    elif retest_state == "first_touch":
        level = vah if side == "long" else val
        op = "<=" if side == "long" else ">="
        action = {
            "type": "wait_retest",
            "message": f"WAIT -- first touch only, not confirmed. Retest of {level:.2f} that holds is the entry signal for {side.upper()}.",
            "alert_price": round(level, 4), "alert_operator": op,
        }
    else:
        action = {
            "type": "enter",
            "message": f"{'Confirmed retest' if retest_state == 'retested' else 'Extended move, no retest yet'} -- {side.upper()} is tradeable now. Invalidation {invalidation:.2f}, target {target:.2f}.",
            "alert_price": round(invalidation, 4), "alert_operator": (">=" if side == "short" else "<="),
        }

    return {
        "symbol": symbol,
        "period_unit": period_unit,
        "lookback_n": lookback_n,
        "price": round(last_close, 4),
        "thesis": state,
        "side": side,
        "score": round(min(10.0, score), 1),
        "retest_state": retest_state,
        "poc": round(poc, 4), "vah": round(vah, 4), "val": round(val, 4),
        "window_low": round(window_lo, 4), "window_high": round(window_hi, 4),
        "target": round(target, 4) if target is not None else None,
        "invalidation": round(invalidation, 4) if invalidation is not None else None,
        "action": action,
        "reasons": reasons,
    }


def scan_custom_query(query_text, watchlist_id=None, benchmark="SPY", params=None):
    """Full pipeline: run `query_text` (any scanner_builder.py DSL
    expression) against `watchlist_id`, infer the lookback window from
    the query's own [N] bar-shift syntax, then for every matching symbol
    profile exactly that window and classify current price against its
    POC/VAH/VAL. If the query contains no [N] reference (so no window
    can be inferred), falls back to params.consolidation_period_unit /
    consolidation_lookback_n -- `duration_source` in each result says
    which happened, so it's never ambiguous which window was actually
    used."""
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    symbols, err = _run_custom_query(query_text, watchlist_id, benchmark)
    if err:
        return [], {"_query_error": err}

    tf, n = _extract_query_duration(query_text)
    duration_source = "query"
    if not tf or not n:
        tf = p.get("consolidation_period_unit", "1w")
        n = int(p.get("consolidation_lookback_n", 4))
        duration_source = "params"

    results = []
    errors = {}
    for sym in symbols:
        try:
            r = _profile_query_window(sym, tf, n, p)
            if r:
                r["duration_source"] = duration_source
                results.append(r)
        except Exception as e:  # noqa: BLE001
            errors[sym] = str(e)

    min_score = float(p.get("min_score", 0) or 0)
    results = [r for r in results if r["score"] >= min_score]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results, errors


# ── Persistence: precomputed daily cache (the "faster scanner execution" piece) ──

def _conn():
    # Missing journal_mode=WAL and busy_timeout here (unlike every other
    # module's _conn() -- signal_notifier.py, job_registry.py,
    # futures_oi_schwab.py, ai_hub.py, copilot.py all already set both)
    # is the likely cause of "database is locked" errors interleaving
    # with this scanner's precompute runs in the logs -- WAL mode lets
    # readers/writers on other connections proceed concurrently instead
    # of blocking on this one's write transactions, and busy_timeout
    # gives a real retry window instead of failing immediately. Python's
    # own connect(timeout=30) sets a busy handler too, but without WAL
    # mode explicitly set, the database can still default to rollback-
    # journal semantics where a writer blocks everyone else outright.
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _ensure_tables():
    con = _conn()
    con.execute("""
        CREATE TABLE IF NOT EXISTS vp_scan_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            thesis TEXT, side TEXT, score REAL, retest_state TEXT,
            price REAL, poc REAL, vah REAL, val REAL, week_poc REAL, month_poc REAL,
            target REAL, target_source TEXT, invalidation REAL, poc_trend TEXT,
            volume_confirmed INTEGER,
            reasons_json TEXT,
            intraday_confirmed INTEGER,
            intraday_checked_at TEXT,
            intraday_detail_json TEXT,
            UNIQUE(symbol, as_of_date)
        )
    """)
    for _ddl in (
        "ALTER TABLE vp_scan_results ADD COLUMN month_poc REAL",
        "ALTER TABLE vp_scan_results ADD COLUMN target_source TEXT",
        "ALTER TABLE vp_scan_results ADD COLUMN retest_state TEXT",
    ):
        try:
            con.execute(_ddl)
        except Exception:
            pass  # already exists, from a DB created before this field was added
    con.commit()
    con.close()


def _upsert_result(con, r):
    con.execute("""
        INSERT INTO vp_scan_results
            (symbol, as_of_date, computed_at, thesis, side, score, retest_state, price, poc, vah, val,
             week_poc, month_poc, target, target_source, invalidation, poc_trend, volume_confirmed, reasons_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol, as_of_date) DO UPDATE SET
            computed_at=excluded.computed_at, thesis=excluded.thesis, side=excluded.side,
            score=excluded.score, retest_state=excluded.retest_state,
            price=excluded.price, poc=excluded.poc, vah=excluded.vah,
            val=excluded.val, week_poc=excluded.week_poc, month_poc=excluded.month_poc,
            target=excluded.target, target_source=excluded.target_source,
            invalidation=excluded.invalidation, poc_trend=excluded.poc_trend,
            volume_confirmed=excluded.volume_confirmed, reasons_json=excluded.reasons_json
    """, (
        r["symbol"], r["date"], datetime.now().isoformat(timespec="seconds"),
        r["thesis"], r["side"], r["score"], r["retest_state"],
        r["price"], r["poc"], r["vah"], r["val"], r["week_poc"], r["month_poc"],
        r["target"], r["target_source"], r["invalidation"], r["poc_trend"], int(r["volume_confirmed"]),
        json.dumps(r["reasons"]),
    ))


_vp_bulk_status = {"running": False, "processed": 0, "total": 0, "computed": 0, "errors": 0}
_vp_bulk_lock = threading.Lock()


def precompute_watchlist(symbols, params=None, progress=True):
    """Called by both the scheduled 'Volume Profile' watchlist step and the
    manual precompute button. Computes and caches a result row per symbol
    for TODAY, so the live scanner page can load instantly from cache
    afterward instead of recomputing on every visit."""
    _ensure_tables()
    if progress:
        with _vp_bulk_lock:
            _vp_bulk_status.update({"running": True, "processed": 0, "total": len(symbols), "computed": 0, "errors": 0})
    con = _conn()
    computed, errors = 0, 0
    for sym in symbols:
        try:
            r = scan_symbol(sym, params)
            if r:
                _upsert_result(con, r)
                computed += 1
        except Exception as e:
            errors += 1
            print(f"[volume_profile_scanner] precompute failed for {sym}: {e}")
        if progress:
            with _vp_bulk_lock:
                _vp_bulk_status["processed"] += 1
                _vp_bulk_status["computed"] = computed
                _vp_bulk_status["errors"] = errors
    con.commit()
    con.close()
    if progress:
        with _vp_bulk_lock:
            _vp_bulk_status["running"] = False
    return {"computed": computed, "errors": errors, "total": len(symbols)}


def _cached_results(watchlist_id=None, min_score=0.0):
    _ensure_tables()
    symbols = set(_get_symbols(watchlist_id))
    today = datetime.now().date().isoformat()
    con = _conn()
    rows = con.execute(
        "SELECT * FROM vp_scan_results WHERE as_of_date=? ORDER BY score DESC", (today,)
    ).fetchall()
    con.close()
    out = []
    for row in rows:
        if symbols and row["symbol"] not in symbols:
            continue
        if row["score"] < min_score:
            continue
        d = dict(row)
        d["reasons"] = json.loads(d.pop("reasons_json") or "[]")
        d["volume_confirmed"] = bool(d["volume_confirmed"])
        d["date"] = d.pop("as_of_date")
        out.append(d)
    return out


# ── Intraday confirmation (live, on-demand -- NOT precomputed) ──

def intraday_confirmation(symbol, side, params=None):
    """Pulls today's live intraday candles via the shared DXLink feed and
    checks whether current order flow still supports the daily thesis:
    relative volume + a close-location-value delta proxy (same combo the
    Pine indicator's CVD filter uses). Returns {"available": False, ...}
    rather than raising if DXLink isn't configured/reachable -- this must
    never break the page for people not using tastytrade."""
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    try:
        from ..services.tastytrade_feed import feed, tastytrade_configured
    except Exception as e:
        return {"available": False, "error": f"tastytrade_feed import failed: {e}"}
    if not tastytrade_configured():
        return {"available": False, "error": "Tastytrade not configured (see Realtime setup)"}

    try:
        start_time = datetime.now() - timedelta(hours=8)
        candles = feed.get_candles(symbol, "5m", start_time, extended_hours=True, overall_timeout=20.0)
    except Exception as e:
        return {"available": False, "error": f"DXLink candle fetch failed: {e}"}

    if not candles:
        return {"available": False, "error": "No intraday candles returned"}

    rows = []
    for c in candles:
        try:
            o, h, l, cl = float(c.open), float(c.high), float(c.low), float(c.close)
            v = float(c.volume) if c.volume is not None else 0.0
            rows.append((o, h, l, cl, v))
        except Exception:
            continue
    if len(rows) < 5:
        return {"available": False, "error": "Not enough intraday candles yet"}

    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    avg_vol = float(df["volume"].tail(int(p["rel_vol_lookback"])).mean()) or 0.0
    last = df.iloc[-1]
    rel_vol = (float(last["volume"]) / avg_vol) if avg_vol > 0 else None

    # Close-location-value delta proxy, same formula as the Pine CVD proxy.
    clv = (2 * (df["close"] - df["low"]) / (df["high"] - df["low"]).clip(lower=1e-9)) - 1.0
    delta = (clv * df["volume"]).sum()
    cvd_direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")

    supports = None
    if side == "long":
        supports = cvd_direction == "up" and (rel_vol is None or rel_vol >= 0.8)
    elif side == "short":
        supports = cvd_direction == "down" and (rel_vol is None or rel_vol >= 0.8)

    detail = {
        "rel_vol": round(rel_vol, 2) if rel_vol is not None else None,
        "cvd_direction": cvd_direction,
        "cumulative_delta": round(float(delta), 1),
        "bars_used": len(df),
    }

    try:
        _ensure_tables()
        con = _conn()
        today = datetime.now().date().isoformat()
        con.execute("""
            UPDATE vp_scan_results
            SET intraday_confirmed=?, intraday_checked_at=?, intraday_detail_json=?
            WHERE symbol=? AND as_of_date=?
        """, (int(bool(supports)), datetime.now().isoformat(timespec="seconds"), json.dumps(detail), symbol, today))
        con.commit()
        con.close()
    except Exception:
        pass

    return {"available": True, "supports_thesis": supports, "detail": detail}


# ── Routes ──

@vp_bp.route("/")
def page():
    return render_template("volume_profile_scanner.html")


@vp_bp.route("/api/watchlists")
def api_watchlists():
    return jsonify({"watchlists": _watchlists()})


@vp_bp.route("/api/scan", methods=["POST"])
def api_scan():
    body = request.get_json(silent=True) or {}
    watchlist_id = body.get("watchlist_id")
    params = body.get("params") or {}
    use_cache = body.get("use_cache", True)

    if use_cache:
        cached = _cached_results(watchlist_id, float(params.get("min_score", 0) or 0))
        if cached:
            return jsonify({"count": len(cached), "results": cached, "errors": {}, "source": "cache"})

    results, errors = scan_watchlist(watchlist_id=watchlist_id, params=params)
    # Opportunistically cache what we just computed live, so the NEXT load
    # (or the scheduled step later today) benefits too.
    try:
        _ensure_tables()
        con = _conn()
        for r in results:
            _upsert_result(con, r)
        con.commit()
        con.close()
    except Exception as e:
        print(f"[volume_profile_scanner] failed to cache live scan results: {e}")
    return jsonify({"count": len(results), "results": results, "errors": errors, "source": "live"})


# Short in-memory TTL cache for the S/R scan -- NOT the daily-precompute
# pattern above, since this is meant to be run live across whatever
# timeframe the person picks, and results on a 15m/1h chart go stale much
# faster than once-a-day. Just avoids recomputing the whole watchlist
# again if the same (watchlist, timeframe, params) combo is hit twice in
# quick succession (e.g. a page refresh).
_SR_CACHE = {}
_SR_CACHE_TTL = 90.0


@vp_bp.route("/api/scan-sr", methods=["POST"])
def api_scan_sr():
    body = request.get_json(silent=True) or {}
    watchlist_id = body.get("watchlist_id")
    timeframe = body.get("timeframe") or "1h"
    params = body.get("params") or {}
    use_cache = body.get("use_cache", True)

    cache_key = (watchlist_id, timeframe, tuple(sorted(params.items())))
    if use_cache:
        cached = _SR_CACHE.get(cache_key)
        if cached and (time.time() - cached[0]) < _SR_CACHE_TTL:
            results, errors = cached[1]
            return jsonify({"count": len(results), "results": results, "errors": errors, "source": "cache"})

    results, errors = scan_watchlist_sr(watchlist_id=watchlist_id, timeframe=timeframe, params=params)
    _SR_CACHE[cache_key] = (time.time(), (results, errors))
    return jsonify({"count": len(results), "results": results, "errors": errors, "source": "live"})


@vp_bp.route("/api/scan-query", methods=["POST"])
def api_scan_query():
    """Custom-query-driven scan: the query itself defines the setup (any
    scanner_builder.py DSL expression), and the lookback window to
    profile gets inferred from the query's own [N] bar-shift syntax --
    see scan_custom_query()'s docstring. No caching here (unlike the
    other two scan modes): a Scanner Builder query result is exactly as
    fresh as calling it directly, and caching an arbitrary user-typed
    query by its text would grow unboundedly for no real benefit given
    how ad-hoc this mode is meant to be used."""
    body = request.get_json(silent=True) or {}
    query_text = (body.get("query_text") or "").strip()
    if not query_text:
        return jsonify({"error": "query_text is required"}), 400
    watchlist_id = body.get("watchlist_id")
    benchmark = body.get("benchmark") or "SPY"
    params = body.get("params") or {}

    results, errors = scan_custom_query(query_text, watchlist_id=watchlist_id, benchmark=benchmark, params=params)
    if "_query_error" in errors:
        return jsonify({"error": errors["_query_error"]}), 400
    return jsonify({"count": len(results), "results": results, "errors": errors})


@vp_bp.route("/api/confirm/<symbol>", methods=["POST"])
def api_confirm(symbol):
    body = request.get_json(silent=True) or {}
    side = body.get("side")
    params = body.get("params") or {}
    result = intraday_confirmation(symbol.upper().strip(), side, params)
    return jsonify(result)


@vp_bp.route("/api/precompute", methods=["POST"])
def api_precompute():
    """Manual trigger for the same precompute the scheduled step runs --
    lets you warm the cache on demand without waiting for the schedule."""
    if _vp_bulk_status["running"]:
        return jsonify({"error": "A volume profile precompute is already running", "status": _vp_bulk_status}), 409
    body = request.get_json(silent=True) or {}
    watchlist_id = body.get("watchlist_id")
    params = body.get("params") or {}
    symbols = _get_symbols(watchlist_id)
    if not symbols:
        return jsonify({"error": "No symbols found for this watchlist"}), 400

    def _worker():
        precompute_watchlist(symbols, params, progress=True)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return jsonify({"started": True, "symbol_count": len(symbols)})


@vp_bp.route("/api/precompute/status")
def api_precompute_status():
    return jsonify(_vp_bulk_status)


def synthetic_selftest():
    """Synthetic OHLCV smoke test -- see module docstring's KNOWN GAPS."""
    n = 240  # 10 days of hourly bars
    rng = pd.date_range(end=datetime.now().replace(minute=0, second=0, microsecond=0), periods=n, freq="h")
    rs = np.random.RandomState(7)
    price = [100.0]
    for i in range(1, n):
        drift = 0.05 if i > n - 30 else 0.0  # trend up into the last ~day
        price.append(max(1.0, price[-1] + drift + rs.uniform(-0.3, 0.3)))
    close = pd.Series(price, index=rng)
    high = close + pd.Series(rs.uniform(0.05, 0.4, n), index=rng)
    low = close - pd.Series(rs.uniform(0.05, 0.4, n), index=rng)
    openp = close.shift(1).fillna(close.iloc[0])
    vol = pd.Series(rs.uniform(1000, 5000, n), index=rng)
    vol.iloc[-1] = vol.iloc[:-1].mean() * 2.0  # force an aggressive final bar
    df = pd.DataFrame({"open": openp, "high": high, "low": low, "close": close, "volume": vol})

    global _sb_history
    _orig = _sb_history
    _sb_history = lambda symbol, tf: df
    try:
        r = scan_symbol("TEST")
        print(f"[selftest] result: {r}")
        assert r is not None, "Expected a classification on a trending synthetic series"
        assert r["thesis"] in ("Trend-Long", "Trend-Short", "Reversion-Long", "Reversion-Short", "No-Edge")
        print("[selftest] PASSED")
    finally:
        _sb_history = _orig


if __name__ == "__main__":
    synthetic_selftest()
