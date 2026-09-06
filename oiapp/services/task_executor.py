"""task_executor.py — one shared, bounded ThreadPoolExecutor for CPU/IO-bound
fan-out work (symbol scans, batch fetches, etc.), instead of every module
creating its own `with ThreadPoolExecutor(max_workers=N) as ex:` block.

Why this matters: as of this pass there are 40+ separate ad-hoc
ThreadPoolExecutor instantiations across the codebase (scanner_builder,
signal_notifier, regime_scanner, sr_breakout_scanner, options_analysis,
etc.), each sized independently (6, 8, 10, 12, 15, 20 workers). Because
they're independent, two features running at the same time -- e.g. an
interactive Scanner Builder scan (8 workers) overlapping with a
regime/sector sweep (20 workers) -- can put 20-30+ OS threads under load
concurrently even though the machine's actual useful parallelism for
GIL-bound + SQLite-bound work is much lower. A single shared, bounded
pool caps total concurrent fan-out regardless of how many features
happen to be active at once.

MIGRATION NOTE: This pass wires the two highest-traffic call sites
(Scanner Builder's per-symbol scan, and the daily futures-OI/regime
sweep in app_factory._run_all_tasks) through this shared pool as the
reference pattern. The remaining ~40 call sites listed in the codebase
scan are lower-traffic (interactive, one-off, or already low frequency)
and were intentionally left on their own dedicated pools rather than
risk migrating all of them in one pass -- each one has slightly
different sizing/error-handling assumptions worth reviewing individually.
Migrating a given call site is a 2-line change: replace
`with ThreadPoolExecutor(max_workers=N) as ex: ex.submit(...)` with
`get_executor().submit(...)` (no `with`/shutdown needed -- the shared
pool stays alive for the life of the process).
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

_MAX_WORKERS = 12
_BG_MAX_WORKERS = 8
_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None
_bg_executor: Optional[ThreadPoolExecutor] = None
_replaced_count = 0
_bg_replaced_count = 0


def get_executor() -> ThreadPoolExecutor:
    """The INTERACTIVE pool -- for user-initiated work that someone is
    actively waiting on a response for (Scanner Builder scans, and
    similar). Kept separate from get_background_executor() below on
    purpose: before this split, a scan competed for the same 12 workers
    as watchlist OI/price fetches (198 + 351 symbols' worth of work) --
    if either kind of operation was running when the other started, both
    starved each other, and a scan could time out at 100% (every single
    symbol) not because anything was individually stuck, but because
    there simply weren't enough free workers to make progress on
    anything. Auto-replacing the pool on timeout (see scanner_builder.py)
    only ever fixed the SYMPTOM (a permanently degraded pool) -- it
    couldn't fix a scan losing a fair fight for workers against a
    legitimately-running background fetch, since the replacement pool
    was immediately subject to the exact same contention.
    """
    global _executor
    if _executor is not None:
        return _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="oiapp-interactive-pool")
        return _executor


def get_background_executor() -> ThreadPoolExecutor:
    """The BACKGROUND pool -- for bulk operations nobody is actively
    waiting on (watchlist Fetch Price/Fetch OI, the 7:30 AM sweep, bulk
    backfills). Separate from get_executor() so these can never starve
    an interactive scan of workers, and vice versa -- a slow background
    fetch no longer has any way to make a scan time out, and a scan
    can't stall the 7:30 AM pipeline either.
    """
    global _bg_executor
    if _bg_executor is not None:
        return _bg_executor
    with _lock:
        if _bg_executor is None:
            _bg_executor = ThreadPoolExecutor(max_workers=_BG_MAX_WORKERS, thread_name_prefix="oiapp-background-pool")
        return _bg_executor


def pool_health() -> dict:
    """Direct visibility into whether either pool is degraded. Python
    cannot forcibly kill a thread -- a single genuinely-hung worker
    (a real network hang, a lock deadlock, anything that isn't caught by
    the bounded-deadline wrapper around the WHOLE scan) is gone from that
    pool FOREVER, silently, with no error and no log line. Over a long
    session with enough of those, a pool's effective capacity can
    quietly shrink toward zero, or -- separately -- a pool sized fine in
    isolation can still starve if two legitimately-busy operations (a
    scan and a bulk fetch) are both trying to use it at once, which is
    why interactive and background work are on separate pools now (see
    get_executor()/get_background_executor() above).
    """
    def _snapshot(ex, max_workers, replaced_count):
        with ex._shutdown_lock:  # noqa: SLF001 -- reading only, no mutation
            n_threads = len(ex._threads)
            n_queued = ex._work_queue.qsize()
        return {"max_workers": max_workers, "threads_alive": n_threads,
                "queued_tasks": n_queued, "pool_replaced_count": replaced_count}

    interactive = _snapshot(get_executor(), _MAX_WORKERS, _replaced_count)
    background = _snapshot(get_background_executor(), _BG_MAX_WORKERS, _bg_replaced_count)
    return {
        "interactive": interactive,
        "background": background,
        # Kept at top level too for any existing caller reading the old flat shape.
        **interactive,
    }


def replace_pool(which: str = "interactive") -> dict:
    """Abandons the current executor (whatever's stuck in it keeps
    running as orphaned threads until the process exits, since Python
    can't kill them) and starts a fresh one so NEW work isn't stuck
    waiting behind zombie workers that will never free up on their own.
    `which` is "interactive" (default, matches the old behavior) or
    "background". This is the only real recovery available short of
    restarting the whole app.
    """
    global _executor, _bg_executor, _replaced_count, _bg_replaced_count
    with _lock:
        if which == "background":
            old = _bg_executor
            _bg_executor = ThreadPoolExecutor(max_workers=_BG_MAX_WORKERS, thread_name_prefix="oiapp-background-pool")
            _bg_replaced_count += 1
            count = _bg_replaced_count
        else:
            old = _executor
            _executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="oiapp-interactive-pool")
            _replaced_count += 1
            count = _replaced_count
    old_alive = 0
    if old is not None:
        try:
            with old._shutdown_lock:  # noqa: SLF001
                old_alive = len(old._threads)
        except Exception:
            pass
    print(f"[task_executor] {which} pool replaced (replacement #{count}) -- "
          f"{old_alive} thread(s) from the old pool are now orphaned and will run until they "
          f"finish or the process exits, but new work no longer waits behind them")
    return {"ok": True, "orphaned_threads": old_alive, "replaced_count": count, "which": which}
