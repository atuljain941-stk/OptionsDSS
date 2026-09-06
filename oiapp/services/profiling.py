"""profiling.py — lightweight, dependency-free instrumentation (Phase 5).

Provides three things the original performance review asked for:
  1. Per-request timing (wall-clock ms) for any endpoint, with a bounded
     in-memory history so regressions ("scans used to take 300ms, now
     take 3s") are visible without external tooling.
  2. A request-scoped stats bucket (cache hits/misses, DB connections
     opened, symbols scanned) that call sites push into during a request
     and read back at the end -- used to build the `timing` block
     returned by Scanner Builder's /api/run (see scanner_builder.py) and
     to drive the response-time display in the Scanner Builder UI.
  3. A cheap global counter for "how many times did some module open a
     new SQLite connection" -- a direct proxy for the "too many small
     per-connection reads" concern from the review, without needing a
     real profiler.

Scope/known limitation: the request-scoped bucket is a single global,
lock-protected object, not a true per-request-id context (this is a
single-operator local app, not a multi-tenant server, so concurrent
overlapping scans are rare in practice). If two scans truly overlap,
their counts can commingle for the duration of the overlap. Good enough
for "why did this scan feel slow" diagnosis; not a substitute for a real
APM if this app ever becomes multi-user.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

_lock = threading.Lock()

# ---------------------------------------------------------------------
# 1. Per-endpoint request timing history (for the /diagnostics page).
# ---------------------------------------------------------------------
_MAX_HISTORY_PER_ENDPOINT = 200
_endpoint_history: Dict[str, Deque[Dict[str, Any]]] = {}


def record_request(endpoint: str, elapsed_ms: float, meta: Optional[Dict[str, Any]] = None) -> None:
    with _lock:
        hist = _endpoint_history.setdefault(endpoint, deque(maxlen=_MAX_HISTORY_PER_ENDPOINT))
        hist.append({"ts": time.time(), "elapsed_ms": round(elapsed_ms, 1), "meta": meta or {}})


def _percentile(sorted_vals, pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round(pct / 100.0 * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def endpoint_stats(endpoint: Optional[str] = None) -> Dict[str, Any]:
    with _lock:
        keys = [endpoint] if endpoint else list(_endpoint_history.keys())
        out: Dict[str, Any] = {}
        for k in keys:
            hist = list(_endpoint_history.get(k, []))
            if not hist:
                out[k] = {"count": 0}
                continue
            vals = sorted(h["elapsed_ms"] for h in hist)
            out[k] = {
                "count": len(vals),
                "avg_ms": round(sum(vals) / len(vals), 1),
                "min_ms": vals[0],
                "max_ms": vals[-1],
                "p50_ms": round(_percentile(vals, 50), 1),
                "p95_ms": round(_percentile(vals, 95), 1),
                "last_ms": hist[-1]["elapsed_ms"],
                "last_meta": hist[-1]["meta"],
            }
        return out


# ---------------------------------------------------------------------
# 2. Request-scoped counters (cache hits, DB connections opened this
#    request, symbols scanned). "Started"/"finished" bracket a request;
#    increments in between land in the currently-open bucket.
# ---------------------------------------------------------------------
class Timer:
    """Context manager: `with Timer() as t: ...` then `t.elapsed_ms`."""

    def __enter__(self):
        self._start = time.perf_counter()
        self.elapsed_ms = 0.0
        return self

    def __exit__(self, *exc):
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        return False


_current_bucket: Optional[Dict[str, int]] = None
_bucket_lock = threading.Lock()


def request_scope_start() -> Dict[str, int]:
    """Opens a fresh counters bucket for the current request and makes it
    the active target for increment_*() calls from any thread until
    request_scope_end() is called. Returns the bucket dict directly too,
    in case the caller wants to read it without going through the global
    (safer under overlap)."""
    global _current_bucket
    bucket = {
        "snapshot_memory_hits": 0,
        "snapshot_sqlite_hits": 0,
        "snapshot_computed": 0,
        "db_connections_opened": 0,
    }
    with _bucket_lock:
        _current_bucket = bucket
    return bucket


def request_scope_end() -> None:
    global _current_bucket
    with _bucket_lock:
        _current_bucket = None


def increment(key: str, n: int = 1) -> None:
    with _bucket_lock:
        b = _current_bucket
        if b is not None and key in b:
            b[key] += n
    # Also maintain a cumulative, process-lifetime total for the
    # /diagnostics page (independent of any single request's bucket).
    with _lock:
        _cumulative_counters[key] = _cumulative_counters.get(key, 0) + n


_cumulative_counters: Dict[str, int] = {}


def cumulative_counters() -> Dict[str, int]:
    with _lock:
        return dict(_cumulative_counters)


# ---------------------------------------------------------------------
# 3. Process-wide snapshot for the /diagnostics page.
# ---------------------------------------------------------------------
def system_snapshot() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "thread_count": threading.active_count(),
        "thread_names": sorted(t.name for t in threading.enumerate()),
        "cumulative_counters": cumulative_counters(),
    }
    try:
        from . import unified_scheduler
        out["scheduler_hub"] = unified_scheduler.status()
    except Exception as e:  # noqa: BLE001
        out["scheduler_hub_error"] = str(e)
    try:
        from .task_executor import get_executor
        ex = get_executor()
        out["shared_task_pool"] = {
            "max_workers": ex._max_workers,  # noqa: SLF001 -- diagnostics only
        }
    except Exception as e:  # noqa: BLE001
        out["shared_task_pool_error"] = str(e)
    return out
