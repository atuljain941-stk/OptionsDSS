"""
mtf_scanner.py -- Multi-Timeframe Alignment Scanner.

Same architecture as pattern_scanner.py: no new detection logic, just
composes existing Scanner Builder primitives (RegSlopeDeg, ChochBullish/
Bearish, BosBullish/Bearish, SqueezeOn, VolumeDryup, ATRCompression) at
different timeframes, run via the existing /scanner-builder/api/run
engine through an internal self-call.

Three scenarios, matched to a real top-down discretionary workflow:

1. CONFIRMED MISALIGNMENT ("counter-trend" setup): HTF is trending one
   way while the LTF has sustained an opposite regression slope for a
   configurable number of LTF bars. A one-bar character change is not
   enough to be called a mature counter-trend.

2. CONFIRMED ALIGNMENT ("continuation" setup): HTF trend + LTF sustained
   regression slope in the SAME direction for the configured maturity
   window. This avoids treating one impulsive LTF bar as continuation.

3. LTF BASE relevant on MTF ("could break and change the trend"): LTF
   showing a genuine volatility squeeze with volume drying up, AND the
   timeframe one step up ALSO showing some compression -- confirming
   this is a real structural base, not just LTF noise that would mean
   nothing on a higher timeframe.

HTF trend direction uses RegSlopeDeg(close, bars, tf) sign against a
configurable threshold -- simple and consistent, reusing the same
regression-slope primitive already built for the volume-vs-price trend
discussion, rather than a separate trend classifier.
"""
from flask import Blueprint, jsonify, request, current_app

mtf_scanner_bp = Blueprint("mtf_scanner_bp", __name__, url_prefix="/mtf-scanner")

TIMEFRAME_OPTIONS = ["1m", "1w", "1d", "4h", "2h", "1h"]  # monthly first (highest), down to hourly

# One step up the hierarchy -- used by Scenario 3 to find "the timeframe
# one level above the LTF" for the MTF-relevance check.
_TF_STEP_UP = {"1h": "4h", "2h": "1d", "4h": "1d", "1d": "1w", "1w": "1m", "1m": "1m"}

SCENARIOS = {
    "misalign_rejection": {
        "label": "HTF/LTF Misalignment -- Confirmed Counter-Trend + OI Unwind",
        "requires": ["htf", "ltf"],
    },
    "align_continuation": {
        "label": "LTF Aligned with HTF -- Confirmed Continuation",
        "requires": ["htf", "ltf"],
    },
    "ltf_base_mtf_relevant": {
        "label": "LTF Base Building, Confirmed on MTF (potential trend change)",
        "requires": ["ltf"],  # MTF is derived automatically (one step above LTF)
    },
}


@mtf_scanner_bp.route("/options")
def options_route():
    return jsonify({"timeframes": TIMEFRAME_OPTIONS,
                     "defaults": {"maturity_bars": 3, "require_oi_unwind": True, "min_oi_unwind_pct": 3.0},
                     "scenarios": [{"key": k, **v} for k, v in SCENARIOS.items()]})


def _build_query(scenario_key: str, htf: str, ltf: str, trend_bars: int, trend_threshold_deg: float, maturity_bars: int, require_oi_unwind: bool = True, min_oi_unwind_pct: float = 3.0) -> str:
    # A regression window is used as the maturity gate: it evaluates the
    # complete recent LTF window instead of a single current-bar CHoCH/BOS.
    # 0.25° is intentionally modest; the direction must persist, but we do
    # not require a large momentum move merely to call it established.
    ltf_confirm_deg = 0.25
    if scenario_key == "misalign_rejection":
        # Options OI is stored as daily end-of-day history rather than
        # intraday/weekly candles. A meaningful drop in aggregate OI is the
        # available confirmation that the prevailing position is unwinding;
        # without it, a counter-trend price drift is treated as noise.
        oi_gate = f' and oi_change_pct <= -{min_oi_unwind_pct}' if require_oi_unwind else ''
        return (
            f'(RegSlopeDeg(close, {trend_bars}, "{htf}") > {trend_threshold_deg} and '
            f'RegSlopeDeg(close, {maturity_bars}, "{ltf}") < -{ltf_confirm_deg}{oi_gate}) '
            f'or (RegSlopeDeg(close, {trend_bars}, "{htf}") < -{trend_threshold_deg} and '
            f'RegSlopeDeg(close, {maturity_bars}, "{ltf}") > {ltf_confirm_deg}{oi_gate})'
        )
    if scenario_key == "align_continuation":
        return (
            f'(RegSlopeDeg(close, {trend_bars}, "{htf}") > {trend_threshold_deg} and '
            f'RegSlopeDeg(close, {maturity_bars}, "{ltf}") > {ltf_confirm_deg}) '
            f'or (RegSlopeDeg(close, {trend_bars}, "{htf}") < -{trend_threshold_deg} and '
            f'RegSlopeDeg(close, {maturity_bars}, "{ltf}") < -{ltf_confirm_deg})'
        )
    if scenario_key == "ltf_base_mtf_relevant":
        mtf = _TF_STEP_UP.get(ltf, ltf)
        return (
            f'SqueezeOn(20, 2.0, 20, 10, 1.5, "{ltf}") and VolumeDryup(20, "{ltf}") < 70 '
            f'and ATRCompression(14, "{mtf}") < 80'
        )
    raise ValueError(f"Unknown scenario: {scenario_key}")


def _mtf_result_columns(ltf: str):
    """Context columns shown with every MTF match.

    These intentionally do not filter the scan.  They make the directional
    setup tradeable by showing extension, nearby price structure, and current
    aggregate option positioning in the same result row.
    """
    return [
        {"expr": "close", "label": "Close"},
        {"expr": f"close[{ltf}] / ema13[{ltf}]", "label": "Close / EMA13"},
        {"expr": f"ema13[{ltf}] / ema50[{ltf}]", "label": "EMA13 / EMA50"},
        {"expr": f"rsidiff90(90, \"{ltf}\")", "label": "RSI Diff 90"},
        {"expr": f"Support(60, \"{ltf}\")", "label": "Major Support"},
        {"expr": f"Resistance(60, \"{ltf}\")", "label": "Major Resistance"},
        {"expr": "PutWallStrike()", "label": "Put Wall"},
        {"expr": "CallWallStrike()", "label": "Call Wall"},
    ]


def run_mtf_scan(watchlist_id, scenario_keys, htf, ltf, trend_bars=10, trend_threshold_deg=3.0, maturity_bars=3, require_oi_unwind=True, min_oi_unwind_pct=3.0):
    """For each selected scenario, builds its query (parameterized by
    the chosen HTF/LTF pair) and runs it via the existing scan engine,
    merging matches by symbol -- same merge-by-symbol pattern as
    pattern_scanner.py, so a stock matching multiple scenarios shows
    all of them together."""
    by_symbol = {}
    errors = []
    result_columns = _mtf_result_columns(ltf)

    for key in scenario_keys:
        scenario = SCENARIOS.get(key)
        if not scenario:
            continue
        try:
            query_text = _build_query(key, htf, ltf, trend_bars, trend_threshold_deg, maturity_bars, require_oi_unwind, min_oi_unwind_pct)
        except Exception as e:
            errors.append({"scenario": key, "error": str(e)})
            continue
        try:
            with current_app.test_client() as client:
                resp = client.post("/scanner-builder/api/run", json={
                    "query_text": query_text,
                    "watchlist_id": watchlist_id,
                    "result_columns": result_columns,
                })
                data = resp.get_json() or {}
            if data.get("error"):
                errors.append({"scenario": scenario["label"], "error": data["error"]})
                continue
            for row in (data.get("results") or []):
                sym = row.get("symbol")
                if not sym:
                    continue
                entry = by_symbol.setdefault(sym, {"symbol": sym, "price": row.get("price") or row.get("close"), "metrics": row.get("_result_columns") or {}, "scenarios": []})
                if not entry.get("metrics") and row.get("_result_columns"):
                    entry["metrics"] = row.get("_result_columns")
                entry["scenarios"].append({"key": key, "label": scenario["label"]})
        except Exception as e:
            errors.append({"scenario": scenario["label"], "error": str(e)})

    results = sorted(by_symbol.values(), key=lambda r: -len(r["scenarios"]))
    return {"results": results, "htf": htf, "ltf": ltf, "maturity_bars": maturity_bars,
            "require_oi_unwind": require_oi_unwind, "min_oi_unwind_pct": min_oi_unwind_pct,
            "result_columns": result_columns, "scenarios_run": len(scenario_keys), "errors": errors}


@mtf_scanner_bp.route("/run", methods=["POST"])
def run_route():
    payload = request.get_json(force=True) or {}
    watchlist_id = payload.get("watchlist_id")
    htf = str(payload.get("htf") or "1m")
    ltf = str(payload.get("ltf") or "1d")
    scenario_keys = payload.get("scenarios") or []
    if scenario_keys in (["all"], "all"):
        scenario_keys = list(SCENARIOS.keys())
    if not scenario_keys:
        return jsonify({"error": "No scenarios selected"}), 400
    if not watchlist_id:
        return jsonify({"error": "watchlist_id is required"}), 400
    if htf not in TIMEFRAME_OPTIONS or ltf not in TIMEFRAME_OPTIONS:
        return jsonify({"error": "Invalid timeframe"}), 400
    try:
        trend_bars = int(payload.get("trend_bars") or 10)
        trend_threshold_deg = float(payload.get("trend_threshold_deg") or 3.0)
        maturity_bars = max(2, min(10, int(payload.get("maturity_bars") or 3)))
        require_oi_unwind = bool(payload.get("require_oi_unwind", True))
        min_oi_unwind_pct = max(0.1, min(50.0, float(payload.get("min_oi_unwind_pct") or 3.0)))
        result = run_mtf_scan(watchlist_id, scenario_keys, htf, ltf, trend_bars, trend_threshold_deg,
                              maturity_bars, require_oi_unwind, min_oi_unwind_pct)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@mtf_scanner_bp.route("/mw-run", methods=["POST"])
def mw_run_route():
    """Separate M/W scan; deliberately does not alter Alignment filtering."""
    p=request.get_json(force=True) or {}; watchlist_id=p.get("watchlist_id")
    if not watchlist_id:return jsonify({"error":"watchlist_id required"}),400
    timeframe=str(p.get("timeframe") or "1d"); pattern=str(p.get("pattern") or "both").lower()
    tolerance=max(.25,min(8,float(p.get("tolerance_pct") or 1.0))); lookback=max(20,min(120,int(p.get("lookback") or 60)))
    queries=[]
    if pattern in ("m","both"): queries.append(("M Top",f'TouchCount(Resistance({lookback},"{timeframe}"),{tolerance},{lookback},"{timeframe}") >= 2 and lookback(BounceOffSwingHigh({tolerance},{lookback},2,2,"{timeframe}"),3) and close < ema5'))
    if pattern in ("w","both"): queries.append(("W Bottom",f'TouchCount(Support({lookback},"{timeframe}"),{tolerance},{lookback},"{timeframe}") >= 2 and lookback(BounceOffSwingLow({tolerance},{lookback},2,2,"{timeframe}"),3) and close > ema5'))
    rows=[]
    for label,query in queries:
        with current_app.test_client() as c:data=(c.post("/scanner-builder/api/run",json={"query_text":query,"watchlist_id":watchlist_id,"result_columns":_mtf_result_columns(timeframe)}).get_json() or {})
        for row in data.get("results",[]): rows.append({"symbol":row.get("symbol"),"pattern":label,"price":row.get("price"),"metrics":row.get("_result_columns") or {}})
    return jsonify({"results":rows,"timeframe":timeframe})
