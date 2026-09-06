# oiapp/scanners/scoring_params_routes.py
"""
Scoring Parameters
────────────────────
UI on top of services/scoring_params.py -- view/edit the entry-score
weights, see full version history, and run a live re-scan comparison
(old saved weights vs. a hypothetical edited set) before actually saving
anything.

Read the "Simulate" section's docstring on api_simulate() before
assuming this replays historical outcomes -- it deliberately doesn't,
because the raw per-alert inputs needed for that were never stored.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint, jsonify, render_template, request

scoring_params_bp = Blueprint("scoring_params", __name__, url_prefix="/scoring-params")


@scoring_params_bp.route("/")
def page():
    return render_template("scoring_params.html")


@scoring_params_bp.route("/api/current")
def api_current():
    from ..services.scoring_params import get_params, DEFAULT_PARAMS
    try:
        current = get_params(force_reload=True)
        return jsonify({"ok": True, "params": current, "defaults": DEFAULT_PARAMS})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@scoring_params_bp.route("/api/save", methods=["POST"])
def api_save():
    from ..services.scoring_params import save_params
    body = request.get_json(force=True, silent=True) or {}
    new_params = body.get("params") or {}
    note = (body.get("note") or "").strip()
    if not new_params:
        return jsonify({"ok": False, "error": "No parameters provided"}), 400
    try:
        saved = save_params(new_params, note=note)
        return jsonify({"ok": True, "params": saved})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@scoring_params_bp.route("/api/history")
def api_history():
    from ..services.scoring_params import get_history
    try:
        limit = int(request.args.get("limit", 50))
        return jsonify({"ok": True, "history": get_history(limit)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@scoring_params_bp.route("/api/reset", methods=["POST"])
def api_reset():
    from ..services.scoring_params import reset_to_defaults
    try:
        body = request.get_json(force=True, silent=True) or {}
        note = (body.get("note") or "Reset to defaults").strip()
        restored = reset_to_defaults(note=note)
        return jsonify({"ok": True, "params": restored})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@scoring_params_bp.route("/api/simulate", methods=["POST"])
def api_simulate():
    """
    Live re-scan comparison: runs a small, user-specified list of symbols
    through _scan_one() TWICE -- once under the currently-saved live
    parameters, once under the hypothetical edited set from the form --
    and returns both scores/grades side by side for each symbol.

    WHAT THIS IS: a "does this change move today's real trades in the
    direction I expect" sanity check, using live market data.

    WHAT THIS IS NOT, and can't be without new data: a backtest replay.
    It cannot tell you whether this change would have improved the
    Backtest page's calibration gaps on PAST alerts, because those
    alerts only stored their final score/grade and free text -- not the
    raw regime/RS/PCR/wall/gamma/rsi_diff/dte inputs that fed the
    original score. There's no way to reconstruct those retroactively.
    If you want real historical validation of a parameter change, that
    needs those raw inputs captured going forward from here (a separate,
    future change to how alerts are logged), then enough time for new
    alerts to accumulate and expire.
    """
    from .trade_opportunity_scanner import _scan_one, DTE_MIN, DTE_MAX, MIN_EARN_DAYS
    from ..services.scoring_params import get_params

    body = request.get_json(force=True, silent=True) or {}
    symbols = [s.strip().upper() for s in (body.get("symbols") or "").split(",") if s.strip()]
    proposed = body.get("proposed_params") or {}
    if not symbols:
        return jsonify({"ok": False, "error": "No symbols provided"}), 400
    if len(symbols) > 15:
        return jsonify({"ok": False, "error": "Max 15 symbols per simulation run -- each one re-runs the full scan pipeline twice."}), 400
    if not proposed:
        return jsonify({"ok": False, "error": "No proposed parameter changes provided"}), 400

    current_live = get_params(force_reload=True)
    hypothetical = {**current_live, **proposed}

    results = []
    for sym in symbols:
        row: Dict[str, Any] = {"symbol": sym}
        try:
            current_trade = _scan_one(sym, DTE_MIN, DTE_MAX, MIN_EARN_DAYS, min_score=0, params=current_live)
            row["current"] = {
                "score": current_trade.get("score"), "grade": current_trade.get("grade"),
                "trade_type": current_trade.get("trade_type"),
            } if current_trade else None
        except Exception as e:
            row["current"] = {"error": str(e)}
        try:
            proposed_trade = _scan_one(sym, DTE_MIN, DTE_MAX, MIN_EARN_DAYS, min_score=0, params=hypothetical)
            row["proposed"] = {
                "score": proposed_trade.get("score"), "grade": proposed_trade.get("grade"),
                "trade_type": proposed_trade.get("trade_type"),
            } if proposed_trade else None
        except Exception as e:
            row["proposed"] = {"error": str(e)}

        cur_score = (row.get("current") or {}).get("score")
        new_score = (row.get("proposed") or {}).get("score")
        row["score_delta"] = (new_score - cur_score) if (cur_score is not None and new_score is not None) else None
        results.append(row)

    return jsonify({"ok": True, "results": results,
                     "disclaimer": "Live re-scan under today's real market data, not a historical replay -- "
                                    "see this endpoint's docstring for why a true backtest of a parameter "
                                    "change isn't possible with what's currently stored per alert."})
