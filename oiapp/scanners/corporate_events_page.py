"""
corporate_events_page.py -- Query page for the real quantitative
corporate-events data (SEC Form 4 insider $/shares, XBRL debt $/%,
volume vs average, material 8-K item codes) fetched by the watchlist
scheduler's Corporate Events step (oiapp/services/sec_edgar.py +
scheduled_jobs.py) and stored in corporate_events_snapshot.

Same architecture as minervini_scanner.py: no new query-execution logic
here -- builds a query string from the 10 new InsiderNetDollars()/
DebtChangePct()/VolumePctOfAvg()/etc primitives (registered in
scanner_builder.py, verified against a real DB there before this page
was written) and delegates execution to the existing
/scanner-builder/api/run engine via an internal self-call. This page is
purely: turn filter form values into a query string, show the results
in a table.

A symbol with no corporate-events snapshot yet (never reached by the
watchlist scheduler's Corporate Events step, or not a US SEC filer at
all -- futures, options, crypto) returns None from every primitive here
and is correctly excluded by any numeric filter, not an error.
"""
from flask import Blueprint, jsonify, request, render_template, current_app

corporate_events_bp = Blueprint("corporate_events_bp", __name__, url_prefix="/corporate-events")

RESULT_COLUMNS = [
    {"label": "Symbol", "expr": "symbol", "format": "text", "locked": True},
    {"label": "Price", "expr": "close[1d]", "format": "price"},
    {"label": "Insider Net $", "expr": "InsiderNetDollars()", "format": "price"},
    {"label": "Insider Bought $", "expr": "InsiderDollarsBought()", "format": "price"},
    {"label": "Insider Sold $", "expr": "InsiderDollarsSold()", "format": "price"},
    {"label": "Insider Txns", "expr": "InsiderTransactionCount()", "format": "integer"},
    {"label": "Debt Change %", "expr": "DebtChangePct()", "format": "number"},
    {"label": "Debt Change $", "expr": "DebtChangeDollars()", "format": "price"},
    {"label": "Debt (latest)", "expr": "DebtValue()", "format": "price"},
    {"label": "Volume % of Avg", "expr": "VolumePctOfAvg()", "format": "number"},
    {"label": "Material Events", "expr": "MaterialEventCount()", "format": "integer"},
]

# Shown next to each filter control -- kept here rather than hardcoded
# in the template so the page and the API stay in sync if either changes.
FILTER_LABELS = {
    "min_insider_net": "Min insider net $ (bought - sold, 90d)",
    "max_insider_net": "Max insider net $ (bought - sold, 90d)",
    "min_debt_change_pct": "Min debt change % (period over period)",
    "max_debt_change_pct": "Max debt change % (period over period)",
    "min_volume_pct": "Min volume, % of 20-day average",
    "require_material_event": "Require a material 8-K event (last 30d)",
}


def _num(d, key, default):
    v = d.get(key, default)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except Exception:
        return default


def _build_query(filters: dict) -> str:
    """Every clause is independently optional -- a caller sets only the
    filters they care about, unlike minervini's fixed 4-step structure.
    At least one filter is still required (an unfiltered "show
    everything with corporate events data" query is intentionally NOT
    supported here -- ambiguous what "everything" should mean when most
    symbols in a typical watchlist won't have been through the
    Corporate Events scheduler step at all yet).
    """
    clauses = []

    min_net = _num(filters, "min_insider_net", None)
    if min_net is not None:
        clauses.append(f"InsiderNetDollars() >= {min_net}")
    max_net = _num(filters, "max_insider_net", None)
    if max_net is not None:
        clauses.append(f"InsiderNetDollars() <= {max_net}")

    min_debt_pct = _num(filters, "min_debt_change_pct", None)
    if min_debt_pct is not None:
        clauses.append(f"DebtChangePct() >= {min_debt_pct}")
    max_debt_pct = _num(filters, "max_debt_change_pct", None)
    if max_debt_pct is not None:
        clauses.append(f"DebtChangePct() <= {max_debt_pct}")

    min_vol_pct = _num(filters, "min_volume_pct", None)
    if min_vol_pct is not None:
        clauses.append(f"VolumePctOfAvg() >= {min_vol_pct}")

    if filters.get("require_material_event"):
        # Bare boolean condition, no comparison operator -- same usage
        # pattern already established for other boolean-returning
        # primitives elsewhere in this DSL (e.g. IsATH(...) used bare in
        # an AND clause), confirmed against scanner_builder.py before
        # relying on it here.
        clauses.append("HasMaterialEvent()")

    if not clauses:
        raise ValueError("Set at least one filter -- there's no meaningful 'show everything' default here, "
                          "since most symbols won't have corporate-events data until the watchlist "
                          "scheduler's Corporate Events step has run for them.")

    return " AND ".join(f"({c})" for c in clauses)


@corporate_events_bp.route("/")
def page():
    return render_template("corporate_events.html")


@corporate_events_bp.route("/api/options")
def api_options():
    return jsonify({"labels": FILTER_LABELS, "columns": RESULT_COLUMNS})


@corporate_events_bp.route("/api/run", methods=["POST"])
def api_run():
    payload = request.get_json(force=True) or {}
    watchlist_id = payload.get("watchlist_id")
    symbol = payload.get("symbol")  # optional CSV override, passed straight through
    filters = payload.get("filters") or {}
    limit = payload.get("limit") or 250

    try:
        query_text = _build_query(filters)
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
