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
from datetime import date
import sqlite3

from flask import Blueprint, jsonify, request, current_app
from ..config import DB_PATH

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


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _option_mid(option):
    bid, ask, last, price = (_number(option.get(k)) for k in ("bid", "ask", "last", "price"))
    if bid is not None and ask is not None and bid >= 0 and ask >= bid:
        return round((bid + ask) / 2.0, 2)
    return next((round(v, 2) for v in (last, price, bid, ask) if v is not None and v >= 0), None)


def _dte(expiration):
    try:
        return (date.fromisoformat(str(expiration)[:10]) - date.today()).days
    except (TypeError, ValueError):
        return None


def _nearest(options, target):
    return min(options, key=lambda x: abs(_number(x["strike"]) - target)) if options else None


def _mw_trade_idea(row):
    """Create a saved-chain, defined-risk vertical only when usable.

    Technical M/W matches are never filtered out by this enrichment.
    """
    metrics = row.get("metrics") or {}
    price = _number(metrics.get("Close")) or _number(row.get("price"))
    bullish = row.get("pattern") == "W Bottom"
    side = "put" if bullish else "call"
    wall_key, level_key = ("Put Wall", "Major Support") if bullish else ("Call Wall", "Major Resistance")
    wall, level = _number(metrics.get(wall_key)), _number(metrics.get(level_key))
    if price is None:
        return {"kind": "signal", "label": "Signal only", "flags": ["No valid close price"], "comment": "Technical pattern detected; price context is unavailable."}

    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            stamp = con.execute("SELECT MAX(fetch_ts) FROM options WHERE symbol = ?", (row["symbol"],)).fetchone()[0]
            if not stamp:
                return {"kind": "signal", "label": "Signal only", "flags": ["No saved option chain"], "comment": "Technical pattern detected; collect an option chain to build a trade idea."}
            raw = con.execute("SELECT expiration,type,strike,price,oi,bid,ask,last,iv,delta,gamma FROM options WHERE symbol=? AND fetch_ts=? AND oi>0", (row["symbol"], stamp)).fetchall()
        finally:
            con.close()
    except Exception:
        return {"kind": "signal", "label": "Signal only", "flags": ["Option chain unavailable"], "comment": "Technical pattern detected; saved-chain lookup failed safely."}

    chain = [dict(x) for x in raw if _dte(x["expiration"]) is not None and 14 <= _dte(x["expiration"]) <= 45]
    chain = [x for x in chain if str(x.get("type") or "").lower().startswith(side[0]) and _option_mid(x) is not None]
    if not chain:
        return {"kind": "signal", "label": "Signal only", "flags": ["No liquid 14–45 DTE " + side + "s"], "comment": "Technical pattern detected; no eligible saved options."}

    expiries = sorted({x["expiration"] for x in chain}, key=lambda x: abs((_dte(x) or 999) - 30))
    for expiry in expiries:
        contracts = sorted([x for x in chain if x["expiration"] == expiry], key=lambda x: _number(x["strike"]) or 0)
        shorts = []
        for x in contracts:
            strike, delta = _number(x["strike"]), abs(_number(x.get("delta")) or 0)
            otm = strike < price if bullish else strike > price
            if otm and (not delta or 0.10 <= delta <= 0.42):
                shorts.append(x)
        if not shorts:
            continue
        anchor = next((v for v in (wall, level) if v is not None), price)
        target = min(anchor, price * .985) if bullish else max(anchor, price * 1.015)
        short = _nearest(shorts, target)
        short_strike = _number(short["strike"])
        longs = [x for x in contracts if (_number(x["strike"]) < short_strike if bullish else _number(x["strike"]) > short_strike)]
        if not longs:
            continue
        long = _nearest(longs, short_strike - max(price * .025, 1.0) if bullish else short_strike + max(price * .025, 1.0))
        long_strike = _number(long["strike"])
        width = abs(short_strike - long_strike)
        credit = round((_option_mid(short) or 0) - (_option_mid(long) or 0), 2)
        if width <= 0 or credit <= 0 or credit >= width:
            continue
        max_loss, rr = round(width - credit, 2), round(credit / (width - credit), 2)
        pop = round((1 - min(abs(_number(short.get("delta")) or .5), .95)) * 100, 1)
        structural_values = [v for v in (price, wall, level) if v is not None]
        beyond_structure = short_strike <= min(structural_values) if bullish else short_strike >= max(structural_values)
        flags = []
        if not beyond_structure: flags.append("Short strike is not beyond nearest wall/structure")
        if rr < .60: flags.append("Risk/reward below 0.60")
        if abs(_number(short.get("delta")) or 0) > .42: flags.append("High short-leg delta")
        return {
            "kind": "vertical", "label": "Candidate" if rr >= .60 and beyond_structure else "Watchlist candidate",
            "strategy": "Put credit vertical" if bullish else "Call credit vertical", "expiry": expiry, "dte": _dte(expiry),
            "legs": f"Sell {short_strike:g} / Buy {long_strike:g} {side}", "credit": credit, "max_profit": credit,
            "max_loss": max_loss, "breakeven": round(short_strike - credit if bullish else short_strike + credit, 2),
            "rr": rr, "pop_proxy": pop, "iv": _number(short.get("iv")), "delta": _number(short.get("delta")),
            "flags": flags, "comment": "Newest saved chain only; confirm fills and liquidity before entry."
        }
    return {"kind": "signal", "label": "Signal only", "flags": ["No defined-risk saved-chain vertical"], "comment": "Technical pattern detected; no usable vertical met the stored-chain checks."}


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
