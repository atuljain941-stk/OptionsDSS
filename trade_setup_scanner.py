"""
trade_setup_scanner.py -- "Setup Context" Scanner (v1)

Answers the question: "This ticker looks interesting right now -- but
WHAT DID IT DO to get here, and is that a high-probability setup or a
trap?" This is the composite layer on top of Candle Context: instead of
scoring one candle, it scores the SEQUENCE that produced the current
price -- was there real consolidation before the move, is price extended
into a parabolic RSIDiff90 reading, does the higher-timeframe regime
agree, is a TTM squeeze coiled or just released, was the pullback shallow
and orderly, how important is the level that was broken.

DESIGN, matching candle_context_scanner.py's rule exactly:
  - This scanner has NO hard filters beyond "enough price history exists".
    Every dimension below only adds/withholds points and confluence --
    never excludes a symbol. min_score/min_confluence are separate,
    user-controlled DISPLAY filters applied last.
  - Every dimension reuses the existing, already-tested scanner_builder
    primitives via _sb_parse_query/_sb_eval against a lightweight
    price_cache-only ctx (same pattern as candle_context_scanner.py),
    with two exceptions that are computed directly in Python because the
    underlying primitives don't yet expose the needed detail:
      1. Squeeze duration -- SqueezeOn() itself is scanned across a range
         of `shift` values here to detect how many bars ago the squeeze
         state flipped (fired into compression, or released out of it).
         SqueezeOn's own implementation is NOT touched.
      2. Resistance/Support touch count and level age -- ResistanceStrength()'s
         own docstring notes period/bars/tolerance are currently unused by
         the implementation, so rather than patch that primitive blind,
         touch count/age are computed here directly from the raw
         Resistance()/Support() level plus the raw price series.
"""
import sqlite3
import json
import math as _math
from datetime import datetime
from flask import Blueprint, jsonify, request, render_template

trade_setup_bp = Blueprint("trade_setup_bp", __name__, url_prefix="/scanner/trade-setup")
from ..config import DB_PATH as _OIAPP_DB_PATH
DB_PATH = _OIAPP_DB_PATH

try:
    from .scanner_builder import _parse_query as _sb_parse_query, _eval as _sb_eval, _sector_etf_for_symbol
except Exception:
    _sb_parse_query = None
    _sb_eval = None
    _sector_etf_for_symbol = None

# Reuse the already-tested history loader / weekly resampler / current-candle
# detector instead of duplicating them -- same price_cache source and same
# "strong candle" definition candle_context_scanner.py already uses.
from .candle_context_scanner import _get_price_history, _resample_to_weekly, _safe, _detect_current_candle

# Which dimensions run on a given scan. Turn any of these off to skip that
# dimension's evaluation entirely (faster scans, or if a dimension doesn't
# apply to your style e.g. sector_rs for futures/indices).
DEFAULT_DIMENSIONS = {
    "breakout":             True,
    "candle_strength":      True,
    "pre_move_compression": True,
    "extension":            True,
    "regime":               True,
    "squeeze":              True,
    "pullback":             True,
    "level_importance":     True,
    "volume":               True,
    "relative_strength":    True,
    "sector_rs":            True,
}

# Per-dimension timeframe. Default is daily for everything except regime
# (which always checks daily+weekly together for alignment, not independently
# configurable). This is what lets you do e.g. "check consolidation on the
# WEEKLY chart but confirm the breakout candle itself on DAILY":
#   params={"timeframes": {"compression_tf": "1w", "breakout_tf": "1d"}}
# Only "1d" and "1w" are currently supported (both are pre-built into ctx).
DEFAULT_TIMEFRAMES = {
    "breakout_tf":           "1d",
    "candle_tf":              "1d",
    "compression_tf":        "1d",   # "1d" = pre-move compression via swing-age shift; "1w" = current weekly coil (shift=0)
    "extension_tf":          "1d",
    "squeeze_tf":             "1d",
    "pullback_tf":            "1d",
    "level_tf":               "1d",
    "volume_tf":              "1d",
    "relative_strength_tf":   "1d",
    "sector_rs_tf":           "1d",
}

DEFAULTS = {
    "min_score":            0.0,   # display filter only, applied last
    "min_confluence":       0,     # display filter only, applied last
    "sr_period":            20,    # Resistance/Support rolling window
    "touch_tolerance_pct":  1.0,   # % band around the level counted as a "touch"
    "touch_lookback":       90,    # bars searched for touches / level age
    "compression_period":   14,    # ATRCompression period
    "range_period":         20,    # RangeCompression / VolumeDryup period
    "pre_move_lookback":    60,    # swing-high lookback used to find "before the move"
    "squeeze_max_lookback": 30,    # bars scanned backward for squeeze state flip
    "min_price":            3.0,   # basic penny-stock guard, same as candle_context_scanner
    "candle_move_mult":     1.5,   # StrongCandle-equivalent thresholds, see _detect_current_candle
    "candle_vol_mult":      1.5,
    "candle_avg_bars":      20,
    "relative_strength_benchmark": "SPY",
    "relative_strength_period":    90,   # matches RelativeStrength()'s own default
    "sector_rs_period":            20,   # matches SectorRS()'s own default
    "dimensions":  dict(DEFAULT_DIMENSIONS),
    "timeframes":  dict(DEFAULT_TIMEFRAMES),
}


def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c


def _ensure_tables():
    con = _conn()
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_setup_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            params_json TEXT NOT NULL,
            updated_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_setup_saved_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            watchlist_id INTEGER,
            params_json TEXT NOT NULL,
            results_json TEXT NOT NULL,
            result_count INTEGER
        )
    """)
    con.commit()
    con.close()


def _watchlists():
    """Reuses the exact same watchlist source trade_opportunity_scanner.py
    already queries -- one list of watchlists across the whole app, not a
    second competing definition."""
    try:
        from .trade_opportunity_scanner import _watchlists as _tos_watchlists
        return _tos_watchlists()
    except Exception:
        return []


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


def _build_ctx(symbol, full_hist_daily):
    """Same lightweight ctx shape candle_context_scanner.py uses, plus a
    weekly series alongside it -- _node_series requires ctx["timeframes"][tf]
    to already be populated (no on-demand resampling inside the evaluator),
    so HTF regime checks and any "*_tf": "1w" dimension override need the
    weekly series present up front. "symbol"/"sector_etf" are set because
    SectorRS() reads ctx["sector_etf"] directly rather than resolving it
    itself (unlike Sector()/SectorStrength(), which fall back to a lookup)."""
    weekly = _resample_to_weekly(full_hist_daily)
    ctx = {
        "symbol": symbol,
        "sector_etf": _sector_etf_for_symbol(symbol) if _sector_etf_for_symbol else None,
        "timeframes": {
            "1d": {"series": {
                "open": full_hist_daily["open"], "high": full_hist_daily["high"],
                "low": full_hist_daily["low"], "close": full_hist_daily["close"],
                "volume": full_hist_daily["volume"],
            }},
        }
    }
    if weekly is not None and len(weekly["close"]) >= 10:
        ctx["timeframes"]["1w"] = {"series": {
            "open": weekly["open"], "high": weekly["high"],
            "low": weekly["low"], "close": weekly["close"],
            "volume": weekly["volume"],
        }}
    return ctx


def _ev(expr, ctx, shift=0, tf="1d"):
    """Evaluate a scanner_builder expression string against ctx at a given
    shift. Returns None on any failure -- every caller here treats None as
    'no signal on this dimension', never a reason to exclude the symbol."""
    if _sb_parse_query is None or _sb_eval is None:
        return None
    try:
        return _sb_eval(_sb_parse_query(expr), ctx, shift=shift, tf_default=tf)
    except Exception:
        return None


def _squeeze_state(ctx, p, tf="1d"):
    """Scans SqueezeOn() backward across shifts to find how many bars ago
    the squeeze state last flipped -- 'currently coiled for N bars' or
    'released N bars ago and expanding'. Does not touch SqueezeOn itself."""
    expr = "SqueezeOn(20, 2.0, 20, 10, 1.5)"
    max_lb = int(p.get("squeeze_max_lookback", 30))
    hist = [_ev(expr, ctx, shift=s, tf=tf) for s in range(max_lb)]
    if not hist or hist[0] is None:
        return {"currently_on": None, "bars_since_change": None}
    currently_on = bool(hist[0])
    bars_since_change = None
    for i in range(1, len(hist)):
        if hist[i] is None:
            break
        if bool(hist[i]) != currently_on:
            bars_since_change = i
            break
    return {"currently_on": currently_on, "bars_since_change": bars_since_change}


def _level_touch_context(series_high, series_low, level, tolerance_pct, lookback):
    """How many separate bars in the lookback window traded within
    tolerance_pct of `level`, plus how many bars ago the level was first
    approached -- a cheap proxy for level 'importance' and age that doesn't
    depend on ResistanceStrength's currently-unused period/tolerance args."""
    if level is None or level <= 0:
        return {"touch_count": None, "level_age_bars": None}
    n = min(len(series_high), len(series_low))
    if n == 0:
        return {"touch_count": None, "level_age_bars": None}
    lb = min(lookback, n)
    band = level * (tolerance_pct / 100.0)
    touches = 0
    first_touch_bars_ago = None
    for i in range(lb):
        idx = n - 1 - i
        hi, lo = float(series_high.iloc[idx]), float(series_low.iloc[idx])
        if (lo - band) <= level <= (hi + band):
            touches += 1
            first_touch_bars_ago = i
    return {"touch_count": touches, "level_age_bars": first_touch_bars_ago}


def _score_symbol(sym, p):
    dims = {**DEFAULT_DIMENSIONS, **(p.get("dimensions") or {})}
    tfs = {**DEFAULT_TIMEFRAMES, **(p.get("timeframes") or {})}

    full_hist = _get_price_history(sym, min_days=280)
    if full_hist is None:
        return {"matched": False, "symbol": sym, "reason": "no cached price history"}

    price = float(full_hist["close"].iloc[-1])
    if price < float(p.get("min_price", 3.0)):
        return {"matched": False, "symbol": sym, "reason": "below min_price"}

    ctx = _build_ctx(sym, full_hist)
    points, reasons = {}, []
    dim_results = {}  # for compute_confluence -- True/False/None per dimension

    # ── 1. Breakout quality vs failed-breakout trap risk ──────────────────
    points["breakout"] = 0.0
    if dims.get("breakout"):
        tf = tfs.get("breakout_tf", "1d")
        breakout_strength = _ev(f'BreakoutStrength("{tf}")', ctx)
        failed_strength = _ev(f'FailedBreakoutStrength("{tf}")', ctx)
        trap_risk = None
        if breakout_strength is not None:
            bs = float(breakout_strength)
            if bs >= 0.75:
                points["breakout"] = 1.4
                reasons.append(f"Strong breakout candle quality ({bs:.2f}, {tf})")
            elif bs >= 0.5:
                points["breakout"] = 0.8
                reasons.append(f"Moderate breakout candle quality ({bs:.2f}, {tf})")
        if failed_strength is not None:
            fs = float(failed_strength)
            trap_risk = fs >= 40
            if trap_risk:
                points["breakout"] = max(0.0, points["breakout"] - 0.8)
                reasons.append(f"Elevated failed-breakout/trap risk ({fs:.0f}, {tf})")
        dim_results["breakout"] = points["breakout"] >= 1.0
        dim_results["not_trap"] = None if trap_risk is None else (not trap_risk)

    # ── 2. Candle strength: how strong is the actual candle right now ─────
    points["candle_strength"] = 0.0
    if dims.get("candle_strength"):
        tf = tfs.get("candle_tf", "1d")
        candle_p = {"move_mult": p.get("candle_move_mult", 1.5), "vol_mult": p.get("candle_vol_mult", 1.5),
                    "avg_change_bars": p.get("candle_avg_bars", 20), "side_mode": "both"}
        candle = _detect_current_candle(ctx, candle_p) if tf == "1d" else None  # _detect_current_candle is daily-series-only today
        if candle:
            points["candle_strength"] = min(1.2, 0.5 * candle["move_multiple"])
            reasons.append(f"{candle['side'].title()} candle {candle['move_multiple']:.1f}x normal move, "
                            f"{candle['vol_multiple']:.1f}x volume, {candle['body_pct']:.0f}% body")
            dim_results["strong_candle"] = True
        else:
            dim_results["strong_candle"] = False if tf == "1d" else None

    # ── 3. Pre-move / structural compression: coiled base or vertical move ─
    points["pre_move_compression"] = 0.0
    was_coiled = None
    if dims.get("pre_move_compression"):
        tf = tfs.get("compression_tf", "1d")
        if tf == "1d":
            swing_age = _ev(f'DaysSinceSwingHigh({p.get("pre_move_lookback",60)}, 2, 2, "1d")', ctx)
            shift = int(swing_age) if swing_age is not None and swing_age > 0 else 10
            note = f"~{shift} bars before the move"
        else:
            shift = 0  # non-daily: read CURRENT structural compression on that timeframe, not "before a daily move"
            note = f"current {tf} structure"
        atrc = _ev(f'ATRCompression({p.get("compression_period",14)}, "{tf}")', ctx, shift=shift, tf=tf)
        rangec = _ev(f'RangeCompression({p.get("range_period",20)}, "{tf}")', ctx, shift=shift, tf=tf)
        volc = _ev(f'VolumeDryup({p.get("range_period",20)}, "{tf}")', ctx, shift=shift, tf=tf)
        comp_vals = [v for v in (atrc, rangec, volc) if v is not None]
        if comp_vals:
            avg_comp = sum(comp_vals) / len(comp_vals)
            was_coiled = avg_comp < 70
            if was_coiled:
                points["pre_move_compression"] = 1.3
                reasons.append(f"Real consolidation ({note}, avg compression {avg_comp:.0f})")
            else:
                reasons.append(f"Little consolidation ({note}) -- vertical/parabolic risk (avg compression {avg_comp:.0f})")
        dim_results["was_coiled"] = was_coiled

    # ── 4. Extension risk: is price already stretched (RSIDiff90 + slope) ──
    points["extension"] = 0.0
    overextended = None
    if dims.get("extension"):
        tf = tfs.get("extension_tf", "1d")
        rsidiff = _ev(f'RSIDiff90("{tf}")', ctx)
        slope = _ev(f'SlopeDegPerBar(close, 5, "{tf}")', ctx)
        if rsidiff is not None and slope is not None:
            overextended = abs(rsidiff) >= 25 and abs(slope) >= 45
            if not overextended:
                points["extension"] = 1.0
                reasons.append(f"Not overextended (RSIDiff90 {rsidiff:.1f}, slope {slope:.0f} deg, {tf})")
            else:
                reasons.append(f"Extended/parabolic risk -- RSIDiff90 {rsidiff:.1f}, slope {slope:.0f} deg ({tf})")
        dim_results["not_overextended"] = None if overextended is None else (not overextended)

    # ── 5. HTF regime alignment (daily + weekly agreeing) ──────────────────
    points["regime"] = 0.0
    if dims.get("regime"):
        d_bull = _ev('UAEBull("1d")', ctx)
        d_bear = _ev('UAEBear("1d")', ctx)
        w_bull = _ev('UAEBull("1w")', ctx) if "1w" in ctx["timeframes"] else None
        w_bear = _ev('UAEBear("1w")', ctx) if "1w" in ctx["timeframes"] else None
        adx_rising = _ev('UAEADXRising("1d")', ctx)
        hist_growing = _ev('UAEHistGrowing("1d")', ctx)
        regime_aligned = None
        if d_bull is not None or d_bear is not None:
            daily_side = "bull" if d_bull else ("bear" if d_bear else None)
            weekly_side = "bull" if w_bull else ("bear" if w_bear else None)
            regime_aligned = daily_side is not None and daily_side == weekly_side
            if regime_aligned:
                points["regime"] = 1.2
                reasons.append(f"Daily and weekly regime both {daily_side}")
            elif daily_side and weekly_side and daily_side != weekly_side:
                reasons.append(f"Daily regime ({daily_side}) disagrees with weekly ({weekly_side})")
            if adx_rising:
                points["regime"] += 0.4
                reasons.append("Trend strength (ADX) rising")
            if hist_growing:
                points["regime"] += 0.4
                reasons.append("Momentum histogram growing")
        dim_results["regime_aligned"] = regime_aligned

    # ── 6. Squeeze state: coiled, or just released and expanding ──────────
    points["squeeze"] = 0.0
    if dims.get("squeeze"):
        tf = tfs.get("squeeze_tf", "1d")
        sq = _squeeze_state(ctx, p, tf=tf)
        if sq["currently_on"] is True:
            points["squeeze"] = 0.9
            age = sq["bars_since_change"]
            reasons.append(f"Currently in TTM squeeze{f' ({age} bars)' if age else ''} -- coiled ({tf})")
        elif sq["currently_on"] is False and sq["bars_since_change"] is not None and sq["bars_since_change"] <= 5:
            points["squeeze"] = 1.3
            reasons.append(f"Squeeze released {sq['bars_since_change']} bars ago -- early expansion window ({tf})")
        dim_results["squeeze_favorable"] = None if sq["currently_on"] is None else (points["squeeze"] > 0)
    else:
        sq = {"currently_on": None, "bars_since_change": None}

    # ── 7. Pullback quality: shallow pullback + retest of broken level ────
    points["pullback"] = 0.0
    pullback_atr = None
    resistance_level = None
    if dims.get("pullback") or dims.get("level_importance"):
        tf = tfs.get("pullback_tf", "1d")
        pullback_atr = _ev(f'PullbackFromSwingHighATR({p.get("pre_move_lookback",60)}, 2, 2, "{tf}")', ctx)
        resistance_level = _ev(f'Resistance({p.get("sr_period",20)}, "{tf}")', ctx)
    shallow_pullback = None
    if dims.get("pullback"):
        tf = tfs.get("pullback_tf", "1d")
        retested = _ev(f'Retest(Resistance({p.get("sr_period",20)}, "{tf}"), {p.get("touch_tolerance_pct",1.0)}, 10, "{tf}")', ctx)
        if pullback_atr is not None:
            shallow_pullback = abs(pullback_atr) <= 1.5
            if shallow_pullback:
                points["pullback"] = 1.0
                reasons.append(f"Shallow pullback ({pullback_atr:.2f} ATR, {tf}) -- trend intact")
        if retested:
            points["pullback"] += 0.6
            reasons.append("Retesting the broken level rather than running away from it")
        dim_results["shallow_pullback"] = shallow_pullback

    # ── 8. Level importance: touch count + age on the resistance broken ───
    points["level_importance"] = 0.0
    touch_ctx = {"touch_count": None, "level_age_bars": None}
    important_level = None
    if dims.get("level_importance"):
        touch_ctx = _level_touch_context(
            full_hist["high"], full_hist["low"], resistance_level,
            float(p.get("touch_tolerance_pct", 1.0)), int(p.get("touch_lookback", 90)),
        )
        if touch_ctx["touch_count"] is not None:
            important_level = touch_ctx["touch_count"] >= 3
            if important_level:
                points["level_importance"] = 1.0
                age_txt = f", first tested {touch_ctx['level_age_bars']} bars ago" if touch_ctx["level_age_bars"] else ""
                reasons.append(f"Broken level tested {touch_ctx['touch_count']}x{age_txt} -- meaningful level")
            elif touch_ctx["touch_count"] == 1:
                reasons.append("Broken level only tested once -- low significance")
        dim_results["important_level"] = important_level

    # ── 9. Volume confirmation on the move ─────────────────────────────────
    points["volume"] = 0.0
    if dims.get("volume"):
        tf = tfs.get("volume_tf", "1d")
        vol_ratio = _ev(f'volume[{tf}] / ema(volume, 20)', ctx, tf=tf)
        vol_confirmed = None
        if vol_ratio is not None:
            vol_confirmed = vol_ratio >= 1.3
            if vol_confirmed:
                points["volume"] = 0.9
                reasons.append(f"Volume confirms the move ({vol_ratio:.2f}x avg, {tf})")
            else:
                reasons.append(f"Volume does not confirm the move ({vol_ratio:.2f}x avg, {tf})")
        dim_results["volume_confirmed"] = vol_confirmed

    # ── 10. Relative strength vs benchmark (e.g. SPY) ──────────────────────
    points["relative_strength"] = 0.0
    if dims.get("relative_strength"):
        tf = tfs.get("relative_strength_tf", "1d")
        bench = p.get("relative_strength_benchmark", "SPY")
        period = int(p.get("relative_strength_period", 90))
        rs = _ev(f'RelativeStrength("{bench}", {period}, "{tf}")', ctx)
        leading_market = None
        if rs is not None:
            leading_market = rs > 0
            if leading_market:
                points["relative_strength"] = min(1.0, 0.5 + abs(rs) / 40)
                reasons.append(f"Outperforming {bench} by {rs:.1f}pts over {period} bars ({tf})")
            else:
                reasons.append(f"Lagging {bench} by {abs(rs):.1f}pts over {period} bars ({tf})")
        dim_results["leading_market"] = leading_market

    # ── 11. Sector relative strength ───────────────────────────────────────
    points["sector_rs"] = 0.0
    if dims.get("sector_rs"):
        tf = tfs.get("sector_rs_tf", "1d")
        period = int(p.get("sector_rs_period", 20))
        srs = _ev(f'SectorRS({period}, "{tf}")', ctx)
        leading_sector = None
        if srs is not None:
            leading_sector = srs > 0
            if leading_sector:
                points["sector_rs"] = min(1.0, 0.5 + abs(srs) / 20)
                reasons.append(f"Outperforming its sector ETF by {srs:.1f}pts over {period} bars ({tf})")
            else:
                reasons.append(f"Lagging its sector ETF by {abs(srs):.1f}pts over {period} bars ({tf})")
        elif ctx.get("sector_etf") is None:
            reasons.append("No sector ETF mapping found for this symbol -- sector_rs skipped")
        dim_results["leading_sector"] = leading_sector

    score = round(min(10, sum(points.values())), 1)
    if score < float(p.get("min_score", 0.0)):
        return {"matched": False, "symbol": sym, "reason": "below min_score", "score": score}

    from .confluence import compute_confluence
    confluence = compute_confluence(dim_results)
    min_confluence = int(p.get("min_confluence", 0) or 0)
    if min_confluence > 0 and confluence["agreeing"] < min_confluence:
        return {"matched": False, "symbol": sym, "reason": "below min_confluence", "score": score}

    signal = "🔥 High probability" if score >= 7.5 else "✅ Favorable" if score >= 5.5 else "👀 Watch" if score >= 3 else "· Weak"

    return {
        "matched": True, "symbol": sym, "price": _safe(round(price, 2)),
        "score": score, "signal": signal, "confluence": confluence,
        "points": {k: round(v, 2) for k, v in points.items()},
        "reasons": reasons,
        "resistance_level": _safe(round(float(resistance_level), 2)) if resistance_level else None,
        "touch_count": touch_ctx["touch_count"], "level_age_bars": touch_ctx["level_age_bars"],
        "squeeze": sq,
        "pullback_atr": _safe(round(float(pullback_atr), 2)) if pullback_atr is not None else None,
    }


def scan_watchlist(watchlist_id=None, params=None):
    """Entry point for the Scheduler Hub and the API route below. Returns
    every matched symbol sorted by score, highest first."""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    symbols = _get_symbols(watchlist_id)
    results = []
    for sym in symbols:
        try:
            r = _score_symbol(sym, p)
        except Exception as e:
            r = {"matched": False, "symbol": sym, "reason": f"error: {e}"}
        if r.get("matched"):
            results.append(r)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


@trade_setup_bp.route("/scan", methods=["POST"])
def api_scan():
    _ensure_tables()
    body = request.get_json(silent=True) or {}
    watchlist_id = body.get("watchlist_id")
    params = body.get("params") or {}
    results = scan_watchlist(watchlist_id=watchlist_id, params=params)
    top_n = int(params.get("top_n", 20))
    return jsonify({"count": len(results), "results": results[:top_n]})


# ── Page ─────────────────────────────────────────────────────────────────

@trade_setup_bp.route("/")
def page():
    return render_template("trade_setup_scanner.html")


@trade_setup_bp.route("/api/watchlists")
def api_watchlists():
    return jsonify({"watchlists": _watchlists()})


# ── Settings persistence (params for next run) ─────────────────────────────

@trade_setup_bp.route("/api/settings", methods=["GET"])
def api_get_settings():
    _ensure_tables()
    con = _conn()
    row = con.execute("SELECT params_json, updated_at FROM trade_setup_settings WHERE id=1").fetchone()
    con.close()
    if row:
        try:
            return jsonify({"params": json.loads(row["params_json"]), "updated_at": row["updated_at"], "saved": True})
        except Exception:
            pass
    # No saved settings yet -- return the built-in defaults so the page has
    # something sensible to render on first visit.
    return jsonify({"params": {"dimensions": DEFAULT_DIMENSIONS, "timeframes": DEFAULT_TIMEFRAMES,
                                **{k: v for k, v in DEFAULTS.items() if k not in ("dimensions", "timeframes")}},
                     "updated_at": None, "saved": False})


@trade_setup_bp.route("/api/settings", methods=["POST"])
def api_save_settings():
    _ensure_tables()
    body = request.get_json(silent=True) or {}
    params = body.get("params")
    if params is None:
        return jsonify({"error": "params required"}), 400
    con = _conn()
    con.execute(
        """INSERT INTO trade_setup_settings (id, params_json, updated_at) VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET params_json=excluded.params_json, updated_at=excluded.updated_at""",
        (json.dumps(params), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    con.commit()
    con.close()
    return jsonify({"ok": True})


# ── Named saved results (save a run, pull it back later) ───────────────────

@trade_setup_bp.route("/api/results/save", methods=["POST"])
def api_save_results():
    _ensure_tables()
    body = request.get_json(silent=True) or {}
    results = body.get("results")
    if results is None:
        return jsonify({"error": "results required"}), 400
    name = (body.get("name") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    watchlist_id = body.get("watchlist_id")
    params = body.get("params") or {}
    con = _conn()
    cur = con.execute(
        """INSERT INTO trade_setup_saved_scans (name, created_at, watchlist_id, params_json, results_json, result_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), watchlist_id,
         json.dumps(params), json.dumps(results), len(results)),
    )
    con.commit()
    saved_id = cur.lastrowid
    con.close()
    return jsonify({"ok": True, "id": saved_id, "name": name})


@trade_setup_bp.route("/api/results/list")
def api_list_results():
    _ensure_tables()
    con = _conn()
    rows = con.execute(
        "SELECT id, name, created_at, watchlist_id, result_count FROM trade_setup_saved_scans ORDER BY created_at DESC"
    ).fetchall()
    con.close()
    return jsonify({"saved_scans": [dict(r) for r in rows]})


@trade_setup_bp.route("/api/results/<int:scan_id>")
def api_get_results(scan_id):
    _ensure_tables()
    con = _conn()
    row = con.execute(
        "SELECT id, name, created_at, watchlist_id, params_json, results_json FROM trade_setup_saved_scans WHERE id=?",
        (scan_id,),
    ).fetchone()
    con.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": row["id"], "name": row["name"], "created_at": row["created_at"],
        "watchlist_id": row["watchlist_id"],
        "params": json.loads(row["params_json"]),
        "results": json.loads(row["results_json"]),
    })


@trade_setup_bp.route("/api/results/<int:scan_id>", methods=["DELETE"])
def api_delete_results(scan_id):
    _ensure_tables()
    con = _conn()
    con.execute("DELETE FROM trade_setup_saved_scans WHERE id=?", (scan_id,))
    con.commit()
    con.close()
    return jsonify({"ok": True})
