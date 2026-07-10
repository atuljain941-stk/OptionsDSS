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
