# oiapp/services/connections_status.py
"""
Connections
────────────
One screen showing the status of every credential-based connection this
app uses (Schwab, tastytrade, ICICI/Breeze, Telegram) instead of having
to check the Schwab config page, the ICICI page, and the Signal Notifier
settings separately to answer "is everything actually connected right
now."

Strictly read-only. Every function called here either reads already-
stored config/token state or (for the on-demand "Run full test" button)
makes a real but non-mutating API call to verify the connection works --
nothing here writes, refreshes, or re-authorizes a credential. Re-auth
still happens on each service's own existing page, unchanged; this is
purely a status dashboard on top of what's already stored.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template

connections_bp = Blueprint("connections", __name__, url_prefix="/connections")


@connections_bp.route("/")
def page():
    return render_template("connections.html")


@connections_bp.route("/api/status")
def api_status():
    """Cheap status for every connection -- local config/token checks
    only, no network calls, safe to run on every page load."""
    out = {}

    try:
        from .schwab_trading import is_connected, has_account_hash
        connected = is_connected()
        out["schwab"] = {
            "label": "Schwab", "configured": True, "connected": connected,
            "has_account_hash": has_account_hash(),
            "note": "OAuth token valid" if connected else "Token missing or expired -- re-authorize on the Schwab config page.",
        }
    except Exception as e:
        out["schwab"] = {"label": "Schwab", "configured": False, "connected": False, "error": str(e)}

    try:
        from .tastytrade_feed import tastytrade_configured
        configured = tastytrade_configured()
        out["tastytrade"] = {
            "label": "tastytrade", "configured": configured, "connected": configured,
            "note": "Client secret + refresh token present" if configured else "Not configured -- credentials missing.",
        }
    except Exception as e:
        out["tastytrade"] = {"label": "tastytrade", "configured": False, "connected": False, "error": str(e)}

    try:
        from .icici_breeze import session_status
        s = session_status() or {}
        out["icici"] = {
            "label": "ICICI / Breeze", "configured": s.get("state") != "not_configured",
            "connected": bool(s.get("active")), "note": s.get("message", ""),
            "state": s.get("state"),
        }
    except Exception as e:
        out["icici"] = {"label": "ICICI / Breeze", "configured": False, "connected": False, "error": str(e)}

    try:
        from .telegram_alerts import telegram_configured
        configured = telegram_configured()
        out["telegram"] = {
            "label": "Telegram", "configured": configured, "connected": configured,
            "note": "Bot token + chat ID present" if configured else "Not configured -- set bot token/chat ID in Signal Notifier settings.",
        }
    except Exception as e:
        out["telegram"] = {"label": "Telegram", "configured": False, "connected": False, "error": str(e)}

    return jsonify(out)


@connections_bp.route("/api/test/<service>", methods=["POST"])
def api_test(service: str):
    """On-demand full test -- only runs when explicitly clicked, makes
    real (but non-mutating) API calls to actually verify the connection
    works, not just that credentials are present."""
    try:
        if service == "schwab":
            from .schwab_diagnostics import run_diagnostics
            return jsonify(run_diagnostics())

        elif service == "icici":
            from .icici_diagnostics import run_diagnostics
            return jsonify(run_diagnostics())

        elif service == "tastytrade":
            from .tastytrade_feed import feed
            snap = feed.get_snapshot("SPY")
            ok = snap.get("status") == "live"
            return jsonify({
                "results": [{
                    "name": "Live snapshot fetch (SPY)", "status": "pass" if ok else "fail",
                    "detail": snap,
                }],
            })

        elif service == "telegram":
            from .telegram_alerts import send_telegram_message
            result = send_telegram_message("Connection test from the Connections page.")
            return jsonify({
                "results": [{
                    "name": "Test message send", "status": "pass" if result.get("ok") else "fail",
                    "detail": result,
                }],
            })

        else:
            return jsonify({"error": f"Unknown service: {service}"}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
