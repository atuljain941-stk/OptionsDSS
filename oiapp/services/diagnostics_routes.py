# oiapp/services/diagnostics_routes.py
"""
Diagnostics — one page under Tools showing the instrumentation added in
Phase 5 of the performance pass: per-endpoint response time history
(avg/p50/p95), Scanner Builder cache hit/miss breakdown, DB connections
opened, live thread count/names, and the Scheduler Hub dispatcher's job
list -- all in one place so a regression ("scans used to take 300ms, now
take 3s") is visible without external tooling.
"""
from __future__ import annotations

import threading
from flask import Blueprint, jsonify, render_template, request

from . import profiling

diagnostics_bp = Blueprint("diagnostics", __name__, url_prefix="/diagnostics")


@diagnostics_bp.route("", strict_slashes=False)
def page():
    return render_template("diagnostics.html")


@diagnostics_bp.route("/api/snapshot", methods=["GET"])
def api_snapshot():
    return jsonify({
        "ok": True,
        "endpoints": profiling.endpoint_stats(),
        "system": profiling.system_snapshot(),
    })


@diagnostics_bp.route("/api/pool-health", methods=["GET"])
def api_pool_health():
    """Direct visibility into the shared task executor's health. See
    task_executor.pool_health() -- a stuck worker thread is gone from
    this pool FOREVER with no error, no log line, nothing visible until
    enough of them accumulate that everything sharing this pool (scans,
    watchlist fetches, the 7:30 AM sweep) starts timing out for what
    looks like no reason."""
    from ..services.task_executor import pool_health
    return jsonify({"ok": True, **pool_health()})


@diagnostics_bp.route("/api/pool-health/replace", methods=["POST"])
def api_pool_replace():
    """Abandons the current pool and starts a fresh one. Whatever was
    stuck in the old one keeps running as an orphaned thread until it
    finishes or the process exits, but new work no longer waits behind
    it. Pass ?which=background to replace the background pool instead
    of the interactive one (default)."""
    from ..services.task_executor import replace_pool
    which = request.args.get("which", "interactive")
    return jsonify(replace_pool(which=which))


@diagnostics_bp.route("/api/negative-cache", methods=["GET"])
def api_negative_cache_status():
    """How many symbols are currently cached as 'recently failed' (an
    in-memory cache, unrelated to the database -- see market.py). Useful
    when a fetch seems to silently skip everything: this is usually why."""
    from ..services import market
    keys = [k for k in market._cache.keys() if k.startswith("spot_fail:")]
    symbols = sorted(k.split(":", 1)[1] for k in keys)
    return jsonify({"ok": True, "count": len(symbols), "symbols": symbols})


@diagnostics_bp.route("/api/negative-cache/clear", methods=["POST"])
def api_negative_cache_clear():
    """Clears the 'recently failed' cache -- for one symbol (?symbol=X)
    or all of them. Manual Fetch Price/Fetch OI already bypasses this
    automatically (see watchlist_manager.py); this is for the automatic/
    scheduled paths, which still respect it by design, if you need to
    force an immediate retry there too."""
    from ..services.market import clear_recently_failed
    symbol = request.args.get("symbol")
    n = clear_recently_failed(symbol)
    return jsonify({"ok": True, "cleared": n})


@diagnostics_bp.route("/api/thread-dump", methods=["GET"])
def api_thread_dump():
    """Dumps the Python stack of every live thread, right now. This is
    the tool for "something is stuck and I don't know why" -- rather
    than guessing which of 136 yfinance call sites is hung, this shows
    exactly which thread is stuck and the exact line it's stuck on
    (a network call, a lock, a sleep, whatever). Safe to call anytime;
    read-only, doesn't affect the running threads."""
    import sys
    import traceback as _tb
    frames = sys._current_frames()
    out = []
    for t in threading.enumerate():
        frame = frames.get(t.ident)
        stack = "".join(_tb.format_stack(frame)) if frame else "(no frame available)"
        out.append({
            "name": t.name,
            "ident": t.ident,
            "daemon": t.daemon,
            "alive": t.is_alive(),
            "stack": stack,
        })
    return jsonify({"ok": True, "thread_count": len(out), "threads": out})
