# oiapp/scanners/swing_positioning.py
"""
Swing Positioning Scanner
────────────────────────────
The multi-day equivalent of the 0DTE GEX/Live Chain work -- instead of
intraday gamma/vega, this reads STOCK volume vs price over the last
couple weeks (classic Wyckoff effort-vs-result: heavy volume with little
price movement is absorption/distribution, a leading indicator, not
noise) and crosses it against options positioning at a SWING DTE window
(20-45 days), reusing this app's existing wall-scoring engine.

Core idea, stated plainly: price is the RESULT, volume is the EFFORT.
When effort and result agree (big volume, big move), that's a confirmed
move. When effort is high but result is small, someone big is
absorbing the other side without letting price move yet -- accumulation
if it happens after a decline, distribution if after an advance. That's
the "high probability" setup this scanner looks for: stock-level
absorption/distribution that ALSO lines up with where fresh options OI
is building in the same direction, at a horizon this app hasn't
scanned for before (this app's 0DTE/GEX side never looks past ~45 DTE
for this kind of thing; swing needs the longer horizon).

Reuses, doesn't reinvent:
  - _price_cache_daily_history() (scanner_builder.py) for daily OHLCV+volume
  - _future_exps/_expiry_dte/_oi_rows/_walls (spy_strategies.py) for the
    same wall-scoring engine Wall Term Structure and GEX Plan already use
  - get_symbols_for_options_oi() (watchlist_manager.py) for which symbols
    actually have options OI tracked, so the scanner only runs where it
    can produce a real answer
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

swing_positioning_bp = Blueprint("swing_positioning", __name__, url_prefix="/swing-positioning")

SWING_DTE_MIN = 20
SWING_DTE_MAX = 45
VOLUME_LOOKBACK_DAYS = 20   # rolling average volume window
EFFORT_LOOKBACK_DAYS = 10   # how many recent days to classify and weigh
HIGH_VOL_RATIO = 1.5        # day volume >= this x the 20d average = "high effort"
LOW_VOL_RATIO = 0.7         # day volume <= this x the 20d average = "low effort"
BIG_MOVE_RATIO = 1.2        # day's |% move| >= this x its own 20d avg |% move| = "big result"
SMALL_MOVE_RATIO = 0.5      # day's |% move| <= this x its own 20d avg |% move| = "small result"

# Liquidity gates -- reusing this app's own existing house standard for
# min_expiry_total_oi (50,000, see oi_significance.py's default) rather
# than inventing a fresh number, since this app already relies on that
# threshold everywhere else to decide whether an options chain is
# trustworthy. min_avg_daily_volume has no existing precedent in this
# codebase to reuse -- 1,000,000 shares/day is a commonly-cited practical
# floor below which options markets on a name tend to get genuinely thin
# (wide spreads, sparse strikes, low OI even where OI exists) regardless
# of what the wall analysis itself says; both are exposed as parameters
# so they're adjustable per how strict a filter is actually wanted.
DEFAULT_MIN_TOTAL_OI = 50_000
DEFAULT_MIN_AVG_DAILY_VOLUME = 1_000_000


# ── Stock-level effort vs result ───────────────────────────────────────────

def _normalize_history_schema(df):
    """_price_cache_daily_history() checks a shared module-level cache
    before running its own query -- that cache can get populated by a
    DIFFERENT caller (Scanner Builder's bulk-fetch path) via
    _normalise_ohlcv_frame(), which uses Title Case columns ("Close", not
    "close") with date as the DataFrame's index rather than a column.
    _price_cache_daily_history()'s own direct-query path uses lowercase
    columns with date as a regular column. Whichever caller populates the
    cache first silently determines which schema every other caller
    sees -- this was the actual cause of a 100% failure rate across every
    symbol (KeyError: 'close') once the cache got populated by the
    Title-Case path. Normalizes to one consistent shape here rather than
    touching the shared cache/function itself, so this fix can't affect
    any other caller of that shared infrastructure.
    """
    if df is None:
        return None
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    if "date" not in out.columns:
        # Title-Case path leaves date as the index, not a column.
        out = out.reset_index()
        out.columns = [str(c).strip().lower() for c in out.columns]
        if "index" in out.columns and "date" not in out.columns:
            out = out.rename(columns={"index": "date"})
    return out


def _classify_effort_vs_result(symbol: str) -> Dict[str, Any]:
    from .scanner_builder import _price_cache_daily_history

    df = _price_cache_daily_history(symbol)
    if df is None or len(df) < VOLUME_LOOKBACK_DAYS + EFFORT_LOOKBACK_DAYS:
        return {"ok": False, "error": f"Not enough stored daily history for {symbol} "
                                       f"(need {VOLUME_LOOKBACK_DAYS + EFFORT_LOOKBACK_DAYS}+ days)."}

    df = _normalize_history_schema(df)
    if df is None or "close" not in df.columns or "date" not in df.columns:
        return {"ok": False, "error": f"Stored history for {symbol} is missing expected OHLCV columns."}

    df = df.copy()
    df["pct_change"] = df["close"].pct_change() * 100
    df["avg_volume_20d"] = df["volume"].rolling(VOLUME_LOOKBACK_DAYS).mean()
    df["avg_abs_move_20d"] = df["pct_change"].abs().rolling(VOLUME_LOOKBACK_DAYS).mean()
    df = df.dropna(subset=["avg_volume_20d", "avg_abs_move_20d"])
    if df.empty:
        return {"ok": False, "error": f"Not enough history after computing rolling averages for {symbol}."}

    recent = df.tail(EFFORT_LOOKBACK_DAYS)
    days = []
    accumulation_score = 0.0  # positive = net bullish absorption signal, negative = net bearish
    n = len(recent)
    for i, (_, row) in enumerate(recent.iterrows()):
        vol_ratio = row["volume"] / row["avg_volume_20d"] if row["avg_volume_20d"] else 0
        move_ratio = abs(row["pct_change"]) / row["avg_abs_move_20d"] if row["avg_abs_move_20d"] else 0
        # Recency weight: most recent day counts full, oldest in the
        # window counts least -- a 9-day-old absorption signal matters
        # less than yesterday's.
        recency_weight = (i + 1) / n

        classification = "neutral"
        direction = None
        if vol_ratio >= HIGH_VOL_RATIO and move_ratio <= SMALL_MOVE_RATIO:
            # High effort, small result -- absorption or distribution.
            # Direction depends on what price was doing INTO this day,
            # not the day itself (that's the whole point -- price barely
            # moved on this day).
            prior_idx = df.index.get_loc(row.name)
            prior_window = df.iloc[max(0, prior_idx - 5):prior_idx]
            prior_trend_pct = ((prior_window["close"].iloc[-1] - prior_window["close"].iloc[0])
                                / prior_window["close"].iloc[0] * 100) if len(prior_window) >= 2 else 0
            if prior_trend_pct < -1.0:
                classification, direction = "absorption", "bullish"
                accumulation_score += recency_weight
            elif prior_trend_pct > 1.0:
                classification, direction = "distribution", "bearish"
                accumulation_score -= recency_weight
            else:
                classification = "high_effort_flat_trend"  # ambiguous -- no clear prior direction to read against
        elif vol_ratio >= HIGH_VOL_RATIO and move_ratio >= BIG_MOVE_RATIO:
            classification = "confirmed_move"
            direction = "bullish" if row["pct_change"] > 0 else "bearish"
            accumulation_score += recency_weight * (0.6 if direction == "bullish" else -0.6)
        elif vol_ratio <= LOW_VOL_RATIO and move_ratio >= BIG_MOVE_RATIO:
            classification = "weak_move"  # thin volume behind the move -- prone to reverse, not weighted into the score

        days.append({
            "date": str(row["date"])[:10], "pct_change": round(row["pct_change"], 2),
            "volume_ratio": round(vol_ratio, 2), "move_ratio": round(move_ratio, 2),
            "classification": classification, "direction": direction,
        })

    bias = "bullish" if accumulation_score > 0.5 else "bearish" if accumulation_score < -0.5 else "neutral"
    return {
        "ok": True, "symbol": symbol, "days": days, "accumulation_score": round(accumulation_score, 2),
        "bias": bias, "last_close": round(float(df["close"].iloc[-1]), 2),
        "avg_daily_volume": round(float(df["avg_volume_20d"].iloc[-1]), 0),
    }


# ── Options-level swing walls ──────────────────────────────────────────────

def _swing_wall_levels(symbol: str, spot: float) -> Dict[str, Any]:
    from .spy_strategies import _future_exps, _expiry_dte, _oi_rows, _walls
    from ..services.oi_significance import build_oi_change_filter_context

    all_exps = _future_exps(symbol)
    swing_exps = [e for e in all_exps if SWING_DTE_MIN <= _expiry_dte(e) <= SWING_DTE_MAX]
    if not swing_exps:
        return {"ok": False, "error": f"No stored expiries for {symbol} in the {SWING_DTE_MIN}-{SWING_DTE_MAX} DTE window."}

    # Nearest swing expiry -- close enough to trade, far enough to hold
    expiry = min(swing_exps, key=_expiry_dte)
    rows = _oi_rows(symbol, expiry)
    if not rows:
        return {"ok": False, "error": f"No stored OI rows for {symbol} {expiry}."}

    oi_ctx = build_oi_change_filter_context(symbol, rows, expiry=expiry, source="swing_positioning")
    wall_info = _walls(rows, spot, side=3, oi_change_filter=oi_ctx)
    sig_calls = wall_info.get("significant_call_walls") or []
    sig_puts = wall_info.get("significant_put_walls") or []
    top_call = max(sig_calls, key=lambda w: w.get("score", 0)) if sig_calls else None
    top_put = max(sig_puts, key=lambda w: w.get("score", 0)) if sig_puts else None
    total_oi = sum(int(r.get("oi") or 0) for r in rows)

    return {
        "ok": True, "expiry": expiry, "dte": _expiry_dte(expiry),
        "call_wall": top_call, "put_wall": top_put, "total_oi": total_oi,
    }


# ── Combined read ───────────────────────────────────────────────────────────

def analyze_swing_positioning(symbol: str, min_total_oi: int = DEFAULT_MIN_TOTAL_OI,
                               min_avg_daily_volume: float = DEFAULT_MIN_AVG_DAILY_VOLUME) -> Dict[str, Any]:
    from ..services.market import get_spot

    symbol = symbol.upper().strip()
    spot = get_spot(symbol)
    if not spot:
        return {"ok": False, "symbol": symbol, "error": f"No spot price available for {symbol}."}

    effort = _classify_effort_vs_result(symbol)
    if not effort.get("ok"):
        return {"ok": False, "symbol": symbol, "error": effort.get("error")}

    # Liquidity gate -- checked here, before spending effort on the
    # cross-reference/narrative, since an illiquid name's wall reads
    # aren't trustworthy regardless of how clean the stock-side signal
    # looks. Stock volume is already computed above (avg_daily_volume);
    # options OI needs the wall fetch, so check that gate right after.
    if effort["avg_daily_volume"] < min_avg_daily_volume:
        return {"ok": False, "symbol": symbol, "excluded_illiquid": True,
                "error": f"{symbol} average daily volume ({effort['avg_daily_volume']:,.0f}) is below the "
                         f"{min_avg_daily_volume:,.0f} minimum -- excluded as not liquid enough for a "
                         f"trustworthy options read at this horizon."}

    walls = _swing_wall_levels(symbol, spot)
    if walls.get("ok") and walls.get("total_oi", 0) < min_total_oi:
        return {"ok": False, "symbol": symbol, "excluded_illiquid": True,
                "error": f"{symbol} total OI at the {walls.get('expiry')} expiry ({walls.get('total_oi'):,}) is "
                         f"below the {min_total_oi:,} minimum -- excluded as not liquid enough for a "
                         f"trustworthy wall read."}

    # Cross-reference: does fresh options OI building line up with the
    # stock-level absorption/distribution direction? Agreement between an
    # independent stock-side signal and an independent options-side signal
    # is the actual "high probability" case -- either one alone is just a
    # hypothesis.
    confirmations = []
    confidence = min(60, abs(effort["accumulation_score"]) * 40)  # base confidence from the stock signal alone
    if walls.get("ok"):
        put_w, call_w = walls.get("put_wall"), walls.get("call_wall")
        if effort["bias"] == "bullish" and put_w and put_w.get("fresh"):
            confirmations.append(f"put wall at ${put_w['strike']:.2f} is fresh-building — options positioning agrees")
            confidence += 25
        elif effort["bias"] == "bearish" and call_w and call_w.get("fresh"):
            confirmations.append(f"call wall at ${call_w['strike']:.2f} is fresh-building — options positioning agrees")
            confidence += 25
        elif effort["bias"] == "bullish" and call_w and call_w.get("fresh") and not (put_w and put_w.get("fresh")):
            confirmations.append(f"call wall at ${call_w['strike']:.2f} fresh-building — this actually disagrees with the stock-side bullish read, worth a second look")
            confidence -= 15
        elif effort["bias"] == "bearish" and put_w and put_w.get("fresh") and not (call_w and call_w.get("fresh")):
            confirmations.append(f"put wall at ${put_w['strike']:.2f} fresh-building — this actually disagrees with the stock-side bearish read, worth a second look")
            confidence -= 15

    confidence = max(0, min(100, round(confidence)))

    narrative_parts = []
    if effort["bias"] != "neutral":
        recent_signal = next((d for d in reversed(effort["days"]) if d["classification"] in ("absorption", "distribution")), None)
        if recent_signal:
            narrative_parts.append(
                f"{symbol}: {recent_signal['classification']} on {recent_signal['date']} "
                f"({recent_signal['volume_ratio']}x average volume, only {recent_signal['pct_change']:+.1f}% move) "
                f"— {'someone absorbed selling without letting price fall further' if recent_signal['classification']=='absorption' else 'someone distributed into strength without letting price rise further'}, "
                f"a {effort['bias']} lean.")
    else:
        narrative_parts.append(f"{symbol}: no clear absorption/distribution signal in the last {EFFORT_LOOKBACK_DAYS} days — effort and result have mostly agreed, or there's been too little volume to read.")
    if confirmations:
        narrative_parts.extend(confirmations)

    return {
        "ok": True, "symbol": symbol, "spot": spot, "bias": effort["bias"],
        "confidence": confidence, "accumulation_score": effort["accumulation_score"],
        "days": effort["days"], "walls": walls if walls.get("ok") else None,
        "avg_daily_volume": effort["avg_daily_volume"],
        "total_oi": walls.get("total_oi") if walls.get("ok") else None,
        "narrative": " ".join(narrative_parts),
    }


def scan_watchlist(symbols: Optional[List[str]] = None, min_confidence: int = 40, limit: int = 200,
                    min_total_oi: int = DEFAULT_MIN_TOTAL_OI,
                    min_avg_daily_volume: float = DEFAULT_MIN_AVG_DAILY_VOLUME) -> Dict[str, Any]:
    """Runs analyze_swing_positioning across a symbol list (default: every
    symbol with options OI actually being tracked, via
    get_symbols_for_options_oi() -- no point scanning a symbol this can't
    produce a real wall read for), returns only non-neutral, confirmed
    results above min_confidence, ranked.

    Every symbol that DOESN'T make the results list gets bucketed into
    exactly why not -- illiquid, neutral (no absorption/distribution
    signal found), below the confidence threshold, or a real error (not
    enough stored history, no swing-DTE expiry, etc). A 0-result scan
    with no visibility into which of these actually happened just looks
    like "the filters are too strict" whether or not that's true --
    surfacing the real breakdown turns a guess into something checkable.
    """
    if symbols is None:
        from .watchlist_manager import get_symbols_for_options_oi
        symbols = get_symbols_for_options_oi()
    symbols = symbols[:limit]

    results = []
    errors = []
    excluded_illiquid = 0
    neutral_bias = []       # symbols with a real read, but no absorption/distribution signal
    below_confidence = []   # symbols with a non-neutral bias that just didn't clear min_confidence
    for sym in symbols:
        try:
            r = analyze_swing_positioning(sym, min_total_oi=min_total_oi, min_avg_daily_volume=min_avg_daily_volume)
            if r.get("ok") and r.get("bias") != "neutral" and r.get("confidence", 0) >= min_confidence:
                results.append(r)
            elif r.get("excluded_illiquid"):
                excluded_illiquid += 1
            elif r.get("ok") and r.get("bias") == "neutral":
                neutral_bias.append(sym)
            elif r.get("ok") and r.get("bias") != "neutral":
                below_confidence.append({"symbol": sym, "bias": r.get("bias"), "confidence": r.get("confidence")})
            elif not r.get("ok"):
                errors.append({"symbol": sym, "error": r.get("error")})
        except Exception as e:
            errors.append({"symbol": sym, "error": str(e)})

    results.sort(key=lambda r: r["confidence"], reverse=True)
    below_confidence.sort(key=lambda r: r["confidence"], reverse=True)
    return {"ok": True, "scanned": len(symbols), "results": results, "errors_count": len(errors),
            "excluded_illiquid_count": excluded_illiquid, "errors_sample": errors[:8],
            "neutral_count": len(neutral_bias), "neutral_sample": neutral_bias[:15],
            "below_confidence_count": len(below_confidence), "below_confidence_sample": below_confidence[:10],
            "min_total_oi": min_total_oi, "min_avg_daily_volume": min_avg_daily_volume}


# ── Routes ──────────────────────────────────────────────────────────────

@swing_positioning_bp.route("/")
def page():
    return render_template("swing_positioning.html")


@swing_positioning_bp.route("/api/analyze")
def api_analyze():
    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"ok": False, "error": "symbol required"}), 400
    min_total_oi = int(request.args.get("min_total_oi", DEFAULT_MIN_TOTAL_OI))
    min_avg_daily_volume = float(request.args.get("min_avg_daily_volume", DEFAULT_MIN_AVG_DAILY_VOLUME))
    try:
        return jsonify(analyze_swing_positioning(symbol, min_total_oi=min_total_oi,
                                                   min_avg_daily_volume=min_avg_daily_volume))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@swing_positioning_bp.route("/api/scan")
def api_scan():
    min_confidence = int(request.args.get("min_confidence", 40))
    min_total_oi = int(request.args.get("min_total_oi", DEFAULT_MIN_TOTAL_OI))
    min_avg_daily_volume = float(request.args.get("min_avg_daily_volume", DEFAULT_MIN_AVG_DAILY_VOLUME))
    symbols_param = (request.args.get("symbols") or "").strip()
    symbols = [s.strip().upper() for s in symbols_param.split(",") if s.strip()] if symbols_param else None
    try:
        return jsonify(scan_watchlist(symbols=symbols, min_confidence=min_confidence, min_total_oi=min_total_oi,
                                       min_avg_daily_volume=min_avg_daily_volume))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
