"""
pattern_scanner.py -- Price Action Patterns page backend.

Deliberately does NOT reimplement any pattern detection or context-
building logic. Every pattern below is a Scanner Builder primitive
that already exists (candlestick patterns, chart patterns, market-
structure patterns) -- this module is purely a registry + aggregator
that runs each selected pattern's query expression against a watchlist
via the SAME /scanner-builder/api/run engine everything else in the
app already uses (through an internal self-call, same technique the
Journal-sync code uses elsewhere), then merges results by symbol so a
stock matching multiple patterns shows all of them together with the
actual pattern name(s) attached.

Trade-off worth knowing: running N patterns means N separate calls
into the scan engine (one per pattern), not one shared pass -- simpler
and safer than hand-rolling a parallel evaluation loop, at the cost of
some redundant (but cheap, mostly cache-backed) work if you select
many patterns at once against a large watchlist.
"""
from flask import Blueprint, jsonify, request, current_app

pattern_scanner_bp = Blueprint("pattern_scanner_bp", __name__, url_prefix="/pattern-scanner")

TIMEFRAME_OPTIONS = ["1d", "1w", "4h", "2h", "1h"]

# key -> {label, category, direction, expr} -- expr is a query expression
# template with {tf} substituted for the chosen timeframe. Every expr
# here is just a call to an existing Scanner Builder primitive with
# its own defaults spelled out explicitly (not relying on positional-
# default drift if those functions' signatures ever change order).
PATTERN_REGISTRY = {
    # -- Candlestick (1-3 bar) --
    "bullish_engulfing":   {"label": "Bullish Engulfing",       "category": "Candlestick", "direction": "bullish", "expr": 'IsBullishEngulfing("{tf}")'},
    "bearish_engulfing":   {"label": "Bearish Engulfing",       "category": "Candlestick", "direction": "bearish", "expr": 'IsBearishEngulfing("{tf}")'},
    "morning_star":        {"label": "Morning Star",            "category": "Candlestick", "direction": "bullish", "expr": 'IsMorningStar("{tf}")'},
    "evening_star":        {"label": "Evening Star",            "category": "Candlestick", "direction": "bearish", "expr": 'IsEveningStar("{tf}")'},
    "three_white_soldiers":{"label": "Three White Soldiers",    "category": "Candlestick", "direction": "bullish", "expr": 'IsThreeWhiteSoldiers("{tf}")'},
    "three_black_crows":   {"label": "Three Black Crows",       "category": "Candlestick", "direction": "bearish", "expr": 'IsThreeBlackCrows("{tf}")'},
    "bullish_harami":      {"label": "Bullish Harami",          "category": "Candlestick", "direction": "bullish", "expr": 'IsBullishHarami("{tf}")'},
    "bearish_harami":      {"label": "Bearish Harami",          "category": "Candlestick", "direction": "bearish", "expr": 'IsBearishHarami("{tf}")'},
    "piercing_line":       {"label": "Piercing Line",           "category": "Candlestick", "direction": "bullish", "expr": 'IsPiercingLine("{tf}")'},
    "dark_cloud_cover":    {"label": "Dark Cloud Cover",        "category": "Candlestick", "direction": "bearish", "expr": 'IsDarkCloudCover("{tf}")'},

    # -- Price action / key-level reactions --
    "bounce_off_swing_high": {"label": "Bounce Off Swing High (rejection)", "category": "Price Action", "direction": "bearish", "expr": 'BounceOffSwingHigh(0.5, 60, 2, 2, "{tf}")'},
    "bounce_off_swing_low":  {"label": "Bounce Off Swing Low (rejection)",  "category": "Price Action", "direction": "bullish", "expr": 'BounceOffSwingLow(0.5, 60, 2, 2, "{tf}")'},
    "pin_bar_at_support":    {"label": "Pin Bar at Support",               "category": "Price Action", "direction": "bullish", "expr": 'PinBarAtSupport(0.5, 60, 2, 2, "{tf}")'},
    "pin_bar_at_resistance": {"label": "Pin Bar at Resistance",            "category": "Price Action", "direction": "bearish", "expr": 'PinBarAtResistance(0.5, 60, 2, 2, "{tf}")'},
    "failed_breakout":       {"label": "Failed Breakout (trap)",           "category": "Price Action", "direction": "bearish", "expr": 'FailedBreakoutStrength("{tf}") > 8'},
    "failed_breakdown":      {"label": "Failed Breakdown (reclaim)",       "category": "Price Action", "direction": "bullish", "expr": 'FailedBreakdownStrength("{tf}") > 8'},

    # -- Chart / structure patterns --
    "double_top":          {"label": "Double Top (M)",         "category": "Chart Pattern", "direction": "bearish", "expr": 'IsDoubleTop(40, 2, "{tf}")'},
    "double_bottom":       {"label": "Double Bottom (W)",       "category": "Chart Pattern", "direction": "bullish", "expr": 'IsDoubleBottom(40, 2, "{tf}")'},
    "head_and_shoulders":  {"label": "Head and Shoulders",     "category": "Chart Pattern", "direction": "bearish", "expr": 'IsHeadAndShoulders(8, 30, 2, 2, "{tf}")'},
    "inverse_head_and_shoulders": {"label": "Inverse Head and Shoulders", "category": "Chart Pattern", "direction": "bullish", "expr": 'IsInverseHeadAndShoulders(8, 30, 2, 2, "{tf}")'},
    "bull_flag":           {"label": "Bull Flag",              "category": "Chart Pattern", "direction": "bullish", "expr": 'IsBullFlag(15, 10, 8, 50, "{tf}")'},
    "bear_flag":           {"label": "Bear Flag",              "category": "Chart Pattern", "direction": "bearish", "expr": 'IsBearFlag(15, 10, 8, 50, "{tf}")'},
    "cup_and_handle":      {"label": "Cup and Handle",         "category": "Chart Pattern", "direction": "bullish", "expr": 'IsCupAndHandle(60, 10, 35, 3, "{tf}")'},

    # -- Market structure (CHoCH/BOS) --
    "choch_bullish":       {"label": "Change of Character (bullish)", "category": "Market Structure", "direction": "bullish", "expr": 'ChochBullish(60, 2, 2, "{tf}")'},
    "choch_bearish":       {"label": "Change of Character (bearish)", "category": "Market Structure", "direction": "bearish", "expr": 'ChochBearish(60, 2, 2, "{tf}")'},
    "bos_bullish":         {"label": "Break of Structure (bullish)",  "category": "Market Structure", "direction": "bullish", "expr": 'BosBullish(60, 2, 2, "{tf}")'},
    "bos_bearish":         {"label": "Break of Structure (bearish)",  "category": "Market Structure", "direction": "bearish", "expr": 'BosBearish(60, 2, 2, "{tf}")'},

    # -- Volatility squeeze (precursor, not directional) --
    "squeeze_on":          {"label": "Volatility Squeeze (TTM)", "category": "Volatility", "direction": "neutral", "expr": 'SqueezeOn(20, 2.0, 20, 10, 1.5, "{tf}")'},
}


@pattern_scanner_bp.route("/patterns")
def list_patterns():
    """The pattern picker's data source -- label/category/direction per
    pattern key, for building the one/multiple/all selector."""
    return jsonify({
        "patterns": [{"key": k, **{kk: vv for kk, vv in v.items() if kk != "expr"}} for k, v in PATTERN_REGISTRY.items()],
        "timeframes": TIMEFRAME_OPTIONS,
    })


def _compute_trade_levels(watchlist_id, symbols, timeframe):
    """For every matched symbol, pulls current price + nearest
    Resistance(20)/Support(20) in ONE pass (via the columns feature on
    the existing run engine, not a separate call per symbol), then
    derives a target/stop/RR/POP per symbol based on the majority
    direction of ITS OWN matched patterns.

    Important honesty note, not a hidden simplification: POP here is
    NOT an options-priced probability (that needs an actual option's
    delta/IV, which doesn't exist for a bare stock-level pattern like
    most of these). It's a statistical estimate derived from where
    price currently sits within its own Resistance/Support range --
    the closer to the stop side of that range, the lower the estimated
    POP, and vice versa. Treat it as a rough, volatility-shaped
    sanity check, not a precise probability."""
    if not symbols:
        return {}
    try:
        with current_app.test_client() as client:
            resp = client.post("/scanner-builder/api/run", json={
                "query_text": "1 > 0",
                "watchlist_id": watchlist_id,
                "symbol": ",".join(symbols),
                "columns": [
                    {"label": "close", "expr": f'close[{timeframe}]'},
                    {"label": "resistance", "expr": f'Resistance(20, "{timeframe}")'},
                    {"label": "support", "expr": f'Support(20, "{timeframe}")'},
                ],
            })
            data = resp.get_json() or {}
        if data.get("error") or not data.get("results"):
            print(f"[pattern_scanner] trade-levels lookup returned no usable data: {data.get('error') or 'empty results'}")
            return {}
    except Exception as e:
        print(f"[pattern_scanner] trade-levels lookup failed: {type(e).__name__}: {e}")
        return {}

    levels = {}
    for row in (data.get("results") or []):
        sym = row.get("symbol")
        close, res, sup = row.get("close"), row.get("resistance"), row.get("support")
        if sym is None or close is None or res is None or sup is None or res <= sup:
            continue
        levels[sym] = {"close": close, "resistance": res, "support": sup}
    return levels


def _direction_for_matches(patterns):
    bulls = sum(1 for p in patterns if p.get("direction") == "bullish")
    bears = sum(1 for p in patterns if p.get("direction") == "bearish")
    if bulls > bears:
        return "bullish"
    if bears > bulls:
        return "bearish"
    return None  # tied or all-neutral (e.g. squeeze-only) -- no defensible directional level call


def _trade_plan_for(levels, direction):
    """target/stop/RR/POP for one symbol given its Resistance/Support
    range and the dominant direction of its matched patterns."""
    close, res, sup = levels["close"], levels["resistance"], levels["support"]
    rng = res - sup
    if rng <= 0:
        return None
    if direction == "bearish":
        target, stop = sup, res
        reward, risk = close - target, stop - close
    else:
        target, stop = res, sup
        reward, risk = target - close, close - stop
    if risk <= 0 or reward <= 0:
        return None
    rr = round(reward / risk, 2)
    # Statistical POP proxy (see _compute_trade_levels docstring): where
    # close sits within the R/S range, oriented so "closer to target,
    # further from stop" reads as higher POP. Clamped to a 20-85% band
    # -- deliberately never claims near-certainty or near-impossibility,
    # since this is a rough positional heuristic, not a priced probability.
    frac = (close - sup) / rng
    pop_raw = (1 - frac) if direction == "bearish" else frac
    pop = round(max(0.20, min(0.85, pop_raw)) * 100, 0)
    return {"target": round(target, 2), "stop": round(stop, 2), "rr": rr, "pop_pct": pop}


def run_pattern_scan(watchlist_id, pattern_keys, timeframe):
    """For each selected pattern, runs its query expression against the
    watchlist via the existing /scanner-builder/api/run engine (an
    internal self-call, not a new evaluation path), then merges
    results by symbol so a stock matching multiple patterns shows all
    of them together."""
    timeframe = timeframe if timeframe in TIMEFRAME_OPTIONS else "1d"
    by_symbol = {}
    errors = []

    for key in pattern_keys:
        pat = PATTERN_REGISTRY.get(key)
        if not pat:
            continue
        query_text = pat["expr"].format(tf=timeframe)
        try:
            with current_app.test_client() as client:
                resp = client.post("/scanner-builder/api/run", json={
                    "query_text": query_text,
                    "watchlist_id": watchlist_id,
                })
                data = resp.get_json() or {}
            if data.get("error"):
                errors.append({"pattern": pat["label"], "error": data["error"]})
                continue
            for row in (data.get("results") or []):
                sym = row.get("symbol")
                if not sym:
                    continue
                entry = by_symbol.setdefault(sym, {"symbol": sym, "patterns": [], "price": row.get("price") or row.get("close")})
                entry["patterns"].append({"key": key, "label": pat["label"], "category": pat["category"], "direction": pat["direction"]})
        except Exception as e:
            errors.append({"pattern": pat["label"], "error": str(e)})

    results = sorted(by_symbol.values(), key=lambda r: -len(r["patterns"]))

    matched_symbols = [r["symbol"] for r in results]
    levels_by_symbol = _compute_trade_levels(watchlist_id, matched_symbols, timeframe)
    for r in results:
        direction = _direction_for_matches(r["patterns"])
        levels = levels_by_symbol.get(r["symbol"])
        r["trade_plan"] = _trade_plan_for(levels, direction) if (levels and direction) else None

    return {"results": results, "timeframe": timeframe, "patterns_run": len(pattern_keys), "errors": errors}


@pattern_scanner_bp.route("/run", methods=["POST"])
def run_route():
    payload = request.get_json(force=True) or {}
    watchlist_id = payload.get("watchlist_id")
    timeframe = str(payload.get("timeframe") or "1d")
    pattern_keys = payload.get("patterns") or []
    if pattern_keys in (["all"], "all"):
        pattern_keys = list(PATTERN_REGISTRY.keys())
    if not pattern_keys:
        return jsonify({"error": "No patterns selected"}), 400
    if not watchlist_id:
        return jsonify({"error": "watchlist_id is required"}), 400
    try:
        result = run_pattern_scan(watchlist_id, pattern_keys, timeframe)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
