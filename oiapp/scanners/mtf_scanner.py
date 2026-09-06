"""
mtf_scanner.py -- Multi-Timeframe Alignment Scanner.

Same architecture as pattern_scanner.py: no new detection logic, just
composes existing Scanner Builder primitives (RegSlopeDeg, ChochBullish/
Bearish, BosBullish/Bearish, SqueezeOn, VolumeDryup, ATRCompression) at
different timeframes, run via the existing /scanner-builder/api/run
engine through an internal self-call.

Three scenarios, matched to a real top-down discretionary workflow:

1. MISALIGNMENT + first counter-move ("rejection" setup): HTF is
   trending one way, LTF just showed the FIRST character-change
   AGAINST it (ChochBullish/Bearish on the LTF) -- a counter-move with
   a real chance of failing back toward the HTF direction, not yet a
   genuine reversal.

2. ALIGNMENT ("continuation" setup): HTF trend + LTF confirming Break
   of Structure in the SAME direction -- the LTF has caught up and
   joined the HTF trend, a higher-conviction continuation entry than
   either timeframe alone.

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
        "label": "HTF/LTF Misalignment -- First Counter-Move (rejection setup)",
        "requires": ["htf", "ltf"],
    },
    "align_continuation": {
        "label": "LTF Aligning with HTF (continuation setup)",
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
                     "scenarios": [{"key": k, **v} for k, v in SCENARIOS.items()]})


def _build_query(scenario_key: str, htf: str, ltf: str, trend_bars: int, trend_threshold_deg: float) -> str:
    if scenario_key == "misalign_rejection":
        return (
            f'(RegSlopeDeg(close, {trend_bars}, "{htf}") > {trend_threshold_deg} and ChochBearish(40, 2, 2, "{ltf}")) '
            f'or (RegSlopeDeg(close, {trend_bars}, "{htf}") < -{trend_threshold_deg} and ChochBullish(40, 2, 2, "{ltf}"))'
        )
    if scenario_key == "align_continuation":
        return (
            f'(RegSlopeDeg(close, {trend_bars}, "{htf}") > {trend_threshold_deg} and BosBullish(40, 2, 2, "{ltf}")) '
            f'or (RegSlopeDeg(close, {trend_bars}, "{htf}") < -{trend_threshold_deg} and BosBearish(40, 2, 2, "{ltf}"))'
        )
    if scenario_key == "ltf_base_mtf_relevant":
        mtf = _TF_STEP_UP.get(ltf, ltf)
        return (
            f'SqueezeOn(20, 2.0, 20, 10, 1.5, "{ltf}") and VolumeDryup(20, "{ltf}") < 70 '
            f'and ATRCompression(14, "{mtf}") < 80'
        )
    raise ValueError(f"Unknown scenario: {scenario_key}")


def run_mtf_scan(watchlist_id, scenario_keys, htf, ltf, trend_bars=10, trend_threshold_deg=3.0):
    """For each selected scenario, builds its query (parameterized by
    the chosen HTF/LTF pair) and runs it via the existing scan engine,
    merging matches by symbol -- same merge-by-symbol pattern as
    pattern_scanner.py, so a stock matching multiple scenarios shows
    all of them together."""
    by_symbol = {}
    errors = []

    for key in scenario_keys:
        scenario = SCENARIOS.get(key)
        if not scenario:
            continue
        try:
            query_text = _build_query(key, htf, ltf, trend_bars, trend_threshold_deg)
        except Exception as e:
            errors.append({"scenario": key, "error": str(e)})
            continue
        try:
            with current_app.test_client() as client:
                resp = client.post("/scanner-builder/api/run", json={
                    "query_text": query_text,
                    "watchlist_id": watchlist_id,
                })
                data = resp.get_json() or {}
            if data.get("error"):
                errors.append({"scenario": scenario["label"], "error": data["error"]})
                continue
            for row in (data.get("results") or []):
                sym = row.get("symbol")
                if not sym:
                    continue
                entry = by_symbol.setdefault(sym, {"symbol": sym, "price": row.get("price") or row.get("close"), "scenarios": []})
                entry["scenarios"].append({"key": key, "label": scenario["label"]})
        except Exception as e:
            errors.append({"scenario": scenario["label"], "error": str(e)})

    results = sorted(by_symbol.values(), key=lambda r: -len(r["scenarios"]))
    return {"results": results, "htf": htf, "ltf": ltf, "scenarios_run": len(scenario_keys), "errors": errors}


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
        result = run_mtf_scan(watchlist_id, scenario_keys, htf, ltf, trend_bars, trend_threshold_deg)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
