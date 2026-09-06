"""
minervini_scanner.py -- Minervini SEPA / Trend Template Scanner (stocks).

Same architecture as mtf_scanner.py: no new detection logic, just composes
existing Scanner Builder primitives into a query string per request, run
via the existing /scanner-builder/api/run engine through an internal
self-call (current_app.test_client()). Nothing here talks to price data,
fundamentals fetches, or the DB directly -- that all stays inside
scanner_builder.py, which is already tested and cached.

Every primitive/argument order used below was verified directly against
scanner_builder.py before use (function name registry + evaluator
branches), not assumed from memory -- same discipline as the existing
"always inspect scanner_builder.py before drafting new primitives"
convention, to avoid an argument-shift bug like the StrongCandleLevel one.

One DSL fact worth flagging explicitly: this query language has NO "!="
or "<>" operator (confirmed against the tokenizer's _TOKEN_RE -- only
EQ "=" exists for equality). "Not falling" is therefore written as
NOT(EpsRevisionTrend() = "FALLING"), not EpsRevisionTrend() != "FALLING".

Four independently toggleable steps, each with its own adjustable
thresholds (this is the "options for each step" surface):

  1. Trend Template  -- Minervini's 8-point structural filter (MA stack,
     rising 200D MA, 52w range position, RS rank). Equity-specific --
     meaningless for futures/commodities, which this page is NOT for
     (use the existing VCP Compression / UAE scanners for MGC/GC).
  2. VCP / Volatility Contraction -- ATRCompression + RangeCompression +
     VolumeDryup, same primitives as the existing "VCP Compression"
     saved scanner in scanner_builder.py, but with independently
     adjustable thresholds here rather than that scanner's fixed ones.
  3. Financials (Fundamentals) -- EPS growth, revenue growth, EPS
     revision trend, optional max P/E. This is the "use financials"
     leg that has no analog for commodities but is real for stocks.
  4. Breakout Trigger -- resistance cross with volume expansion and a
     slope cap, mirroring the existing "Controlled Volume Breakout"
     saved scanner.

Steps are ANDed together in the order enabled. Disabling all four steps
is rejected (nothing to scan). Disabling everything except one step
lets this double as a plain Trend Template screener, a plain VCP
screener, a plain fundamentals screener, etc.
"""
from flask import Blueprint, jsonify, request, render_template, current_app

minervini_bp = Blueprint("minervini_bp", __name__, url_prefix="/minervini-scanner")

STEP_DEFAULTS = {
    "trend_template": {
        "enabled": True,
        "rs_rank_min": 70,          # RSRank(252) >= this
        "pct_above_low_min": 30,    # close >= 52w-low * (1 + this/100)
        "pct_below_high_max": 25,   # close >= 52w-high * (1 - this/100)
        "slope_lookback_bars": 21,  # 200D MA slope window; ~21 = 1 month of daily bars
    },
    "vcp": {
        "enabled": True,
        "atr_compression_max": 70,
        "range_compression_max": 70,
        "volume_dryup_max": 70,
    },
    "financials": {
        "enabled": True,
        "eps_growth_min": 20,       # EarningsGrowthPct() >= this
        "revenue_growth_min": 15,   # RevenueGrowthPct() >= this
        "require_eps_not_falling": True,   # NOT(EpsRevisionTrend() = "FALLING")
        "pe_max": None,             # optional; omitted if blank/None
    },
    "breakout": {
        "enabled": False,           # off by default: this is the execution
                                     # trigger, not the watchlist filter --
                                     # most users will want steps 1-3 as a
                                     # daily watchlist and only flip this on
                                     # when hunting for the actual entry day
        "resistance_bars": 20,
        "volume_expansion_mult": 1.5,
        "max_slope_deg_per_bar": 35,
    },
}

# Shown to the user as help text next to each control -- kept here (not
# hardcoded in the template) so the page and the API describe the same
# defaults if either one changes.
STEP_LABELS = {
    "trend_template": "1. Trend Template (structural)",
    "vcp": "2. VCP / Volatility Contraction (pattern)",
    "financials": "3. Financials (fundamentals)",
    "breakout": "4. Breakout Trigger (execution)",
}

RESULT_COLUMNS = [
    {"label": "Symbol", "expr": "symbol", "format": "text", "locked": True},
    {"label": "Price", "expr": "close[1d]", "format": "price"},
    {"label": "Sector", "expr": "Sector()", "format": "text"},
    {"label": "RS Rank", "expr": "RSRank(252)", "format": "number"},
    {"label": "EPS Growth %", "expr": "EarningsGrowthPct()", "format": "number"},
    {"label": "Rev Growth %", "expr": "RevenueGrowthPct()", "format": "number"},
    {"label": "PE (fwd)", "expr": "PE()", "format": "number"},
    {"label": "ATR Compression", "expr": "ATRCompression(14)", "format": "number"},
    {"label": "Volume Dryup", "expr": "VolumeDryup(20)", "format": "number"},
    {"label": "Earnings (days)", "expr": "EarningsDays()", "format": "integer"},
]


def _num(d, key, default):
    v = d.get(key, default)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except Exception:
        return default


def _build_query(steps: dict) -> str:
    """steps: dict keyed by step name -> {enabled, ...params}. Merges
    each step's params over STEP_DEFAULTS[step] so a caller only needs to
    send the fields they want to override."""
    clauses = []

    tt = {**STEP_DEFAULTS["trend_template"], **(steps.get("trend_template") or {})}
    if tt.get("enabled"):
        rs_min = _num(tt, "rs_rank_min", 70)
        pct_low = _num(tt, "pct_above_low_min", 30)
        pct_high = _num(tt, "pct_below_high_max", 25)
        slope_bars = int(_num(tt, "slope_lookback_bars", 21) or 21)
        low_mult = round(1 + (pct_low or 0) / 100.0, 6)
        high_mult = round(1 - (pct_high or 0) / 100.0, 6)
        clauses.append(
            'close[1d] > sma(close, 150, "1d") AND close[1d] > sma(close, 200, "1d") '
            'AND sma(close, 150, "1d") > sma(close, 200, "1d") '
            f'AND SlopeDegPerBar(sma(close, 200, "1d"), {slope_bars}, "1d") > 0 '
            'AND sma(close, 50, "1d") > sma(close, 150, "1d") '
            'AND sma(close, 50, "1d") > sma(close, 200, "1d") '
            'AND close[1d] > sma(close, 50, "1d") '
            f'AND close[1d] >= Lowest(low, 252, "1d") * {low_mult} '
            f'AND close[1d] >= Highest(high, 252, "1d") * {high_mult} '
            f'AND RSRank(252) >= {rs_min}'
        )

    vcp = {**STEP_DEFAULTS["vcp"], **(steps.get("vcp") or {})}
    if vcp.get("enabled"):
        atr_max = _num(vcp, "atr_compression_max", 70)
        range_max = _num(vcp, "range_compression_max", 70)
        voldry_max = _num(vcp, "volume_dryup_max", 70)
        clauses.append(
            f'ATRCompression(14) < {atr_max} AND RangeCompression(20) < {range_max} '
            f'AND VolumeDryup(20) < {voldry_max}'
        )

    fin = {**STEP_DEFAULTS["financials"], **(steps.get("financials") or {})}
    if fin.get("enabled"):
        eps_min = _num(fin, "eps_growth_min", 20)
        rev_min = _num(fin, "revenue_growth_min", 15)
        fin_clauses = [f'EarningsGrowthPct() >= {eps_min}', f'RevenueGrowthPct() >= {rev_min}']
        if fin.get("require_eps_not_falling", True):
            fin_clauses.append('NOT(EpsRevisionTrend() = "FALLING")')
        pe_max = _num(fin, "pe_max", None)
        if pe_max is not None:
            fin_clauses.append(f'PE() <= {pe_max}')
        clauses.append(" AND ".join(fin_clauses))

    bo = {**STEP_DEFAULTS["breakout"], **(steps.get("breakout") or {})}
    if bo.get("enabled"):
        res_bars = int(_num(bo, "resistance_bars", 20) or 20)
        vol_mult = _num(bo, "volume_expansion_mult", 1.5)
        max_slope = _num(bo, "max_slope_deg_per_bar", 35)
        clauses.append(
            f'CrossAbove(close, ResistanceUpper({res_bars}, "1d"), "1d") '
            f'AND volume[1d] >= ema(volume, 20) * {vol_mult} '
            f'AND SlopeDegPerBar(close, 5, "1d") < {max_slope}'
        )

    if not clauses:
        raise ValueError("At least one step must be enabled")

    return " AND ".join(f"({c})" for c in clauses)


@minervini_bp.route("/")
def page():
    return render_template("minervini_scanner.html")


@minervini_bp.route("/api/options")
def api_options():
    return jsonify({
        "steps": STEP_DEFAULTS,
        "labels": STEP_LABELS,
    })


@minervini_bp.route("/api/run", methods=["POST"])
def api_run():
    payload = request.get_json(force=True) or {}
    watchlist_id = payload.get("watchlist_id")
    symbol = payload.get("symbol")  # optional CSV override, passed straight through
    steps = payload.get("steps") or {}
    limit = payload.get("limit") or 250

    try:
        query_text = _build_query(steps)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    run_payload = {
        "query_text": query_text,
        "watchlist_id": watchlist_id,
        "columns": RESULT_COLUMNS,
        "limit": limit,
    }
    if symbol:
        run_payload["symbol"] = symbol

    try:
        with current_app.test_client() as client:
            resp = client.post("/scanner-builder/api/run", json=run_payload)
            data = resp.get_json() or {}
    except Exception as e:
        return jsonify({"error": str(e), "query_text": query_text}), 500

    if data.get("error"):
        return jsonify({"error": data["error"], "query_text": query_text}), 400

    data["query_text"] = query_text  # echo back the constructed query for transparency
    return jsonify(data)
