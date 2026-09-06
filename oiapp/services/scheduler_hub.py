# oiapp/services/scheduler_hub.py
"""
Scheduler Hub — one page under Tools that lists every registered background
job (see job_registry.py), showing its schedule, enabled state, and last
run, with controls to change the schedule, enable/disable, or run it now.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from .job_registry import list_jobs, set_enabled, set_schedule, run_now, get_schedule

scheduler_hub_bp = Blueprint("scheduler_hub", __name__, url_prefix="/scheduler-hub")


@scheduler_hub_bp.route("", strict_slashes=False)
def page():
    return render_template("scheduler_hub.html")


@scheduler_hub_bp.route("/api/jobs", methods=["GET"])
def api_list_jobs():
    return jsonify({"ok": True, "jobs": list_jobs()})


@scheduler_hub_bp.route("/api/jobs/<job_key>/enabled", methods=["POST"])
def api_set_enabled(job_key: str):
    body = request.get_json(force=True, silent=True) or {}
    set_enabled(job_key, bool(body.get("enabled", True)))
    return jsonify({"ok": True})


@scheduler_hub_bp.route("/api/jobs/<job_key>/schedule", methods=["POST"])
def api_set_schedule(job_key: str):
    body = request.get_json(force=True, silent=True) or {}
    kind = body.get("kind")
    try:
        if kind == "interval":
            minutes = max(1, int(body.get("interval_min") or 15))
            set_schedule(job_key, {"interval_min": minutes})
        elif kind == "time":
            times = [t.strip() for t in (body.get("times") or []) if t and t.strip()]
            if not times:
                return jsonify({"ok": False, "error": "At least one time (HH:MM) is required"}), 400
            for t in times:
                parts = t.split(":")
                if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
                    return jsonify({"ok": False, "error": f"Invalid time format: {t} (use HH:MM)"}), 400
            weekdays = body.get("weekdays")
            if weekdays is not None:
                weekdays = [int(w) for w in weekdays]
            set_schedule(job_key, {"times": times, "weekdays": weekdays})
        else:
            return jsonify({"ok": False, "error": "kind must be 'interval' or 'time'"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "schedule": get_schedule(job_key)})


@scheduler_hub_bp.route("/api/jobs/<job_key>/run", methods=["POST"])
def api_run_now(job_key: str):
    result = run_now(job_key)
    return jsonify(result)


@scheduler_hub_bp.route("/api/history", methods=["GET"])
def api_history():
    """Persistent run history (start/end/duration/status) for every job --
    not just 'last run'. This is the answer to 'did the 7:30 pipeline run,
    is it done, how long did it take' -- pass ?job_key=X to filter to one
    job, or omit it for a combined feed across every job (a
    notifications-style view)."""
    from .job_registry import get_run_history
    job_key = request.args.get("job_key")
    limit = int(request.args.get("limit", "50"))
    return jsonify({"ok": True, "history": get_run_history(job_key, limit=limit)})


@scheduler_hub_bp.route("/api/jobs/<job_key>/stop", methods=["POST"])
def api_stop_job(job_key: str):
    """Requests that a currently-running job stop at its next checkpoint.
    This is cooperative, not a forced kill -- Python can't safely
    terminate a running thread mid-operation. A job that isn't currently
    running, or whose code doesn't check for a stop request between its
    own internal steps, will just keep going; the flag is checked at
    natural break points (e.g. Signal Notifier checks between each
    configured alert source)."""
    from .job_registry import request_stop, list_jobs
    job = next((j for j in list_jobs() if j["key"] == job_key), None)
    if job is None:
        return jsonify({"ok": False, "error": "Unknown job"}), 404
    if not job.get("is_running"):
        return jsonify({"ok": False, "error": "This job isn't currently running."}), 400
    request_stop(job_key)
    return jsonify({"ok": True, "message": "Stop requested -- will take effect at the next checkpoint."})


@scheduler_hub_bp.route("/api/integrations", methods=["GET"])
def api_integrations():
    """Connection status for external API credentials, so 'is X configured'
    is visible from the same page you're already checking when something
    isn't running -- instead of only surfacing as a warning buried in the
    startup log (which is what prompted adding this)."""
    integrations = []

    try:
        from ..services.tastytrade_feed import tastytrade_configured, _runtime_credentials
        client_secret, _ = _runtime_credentials()
        integrations.append({
            "name": "Tastytrade",
            "configured": tastytrade_configured(),
            "detail": (f"client_secret {client_secret[:4]}…" if client_secret else "not set"),
            "setup_url": "/realtime/setup",
        })
    except Exception as e:  # noqa: BLE001
        integrations.append({"name": "Tastytrade", "configured": False, "detail": f"error: {e}", "setup_url": "/realtime/setup"})

    try:
        from ..schwab.schwab_routes import _get_config
        cfg = _get_config()
        configured = bool(cfg and cfg.get("refresh_token"))
        integrations.append({
            "name": "Schwab",
            "configured": configured,
            "detail": "connected" if configured else "not set",
            "setup_url": "/auto-trading",
        })
    except Exception as e:  # noqa: BLE001
        integrations.append({"name": "Schwab", "configured": False, "detail": f"error: {e}", "setup_url": "/auto-trading"})

    try:
        from ..services.telegram_alerts import telegram_configured
        integrations.append({
            "name": "Telegram",
            "configured": telegram_configured(),
            "detail": "connected" if telegram_configured() else "not set",
            "setup_url": "/scheduler-hub",
        })
    except Exception as e:  # noqa: BLE001
        integrations.append({"name": "Telegram", "configured": False, "detail": f"error: {e}", "setup_url": "/scheduler-hub"})

    return jsonify({"ok": True, "integrations": integrations})
