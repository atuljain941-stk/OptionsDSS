# oiapp/services/bounded_wait.py
"""
Shared helper for the #1 lesson from the 10-hour scan hang: NO fan-out
across threads should ever wait unconditionally for every result. A
single stuck worker (hung network call, unexpected infinite loop,
whatever) should never be able to block an entire feature forever.

Audit finding: as of this pass, 33 of the app's 41 ThreadPoolExecutor
call sites used `as_completed(futures)` / `.result()` / `.map()` with NO
timeout at all -- the exact shape of bug that caused the scan to hang
for 10 hours, just not yet triggered in those other 33 places. This
module is the fix pattern, in one place, so it can be applied
consistently instead of each site needing its own bespoke deadline logic
(and each site being a new chance to forget one).

Usage (replaces `for fut in as_completed(futs): ...`):

    from ..services.bounded_wait import bounded_as_completed

    results = []
    timed_out = []
    for fut, key in bounded_as_completed(futs, timeout=60):
        if fut is None:
            timed_out.append(key)   # still running when the deadline hit
            continue
        try:
            results.append(fut.result())
        except Exception as e:
            ...  # handle per-item failure same as before

CRITICAL, easy to get wrong (caught this myself while migrating call sites):
do NOT use `with ThreadPoolExecutor(...) as ex:` around this. The context
manager's __exit__ calls shutdown(wait=True) unconditionally, which blocks
until every submitted future finishes regardless of this helper's timeout
-- silently undoing the entire point. Instead:

    ex = ThreadPoolExecutor(max_workers=N)
    try:
        futs = {ex.submit(fn, x): x for x in items}
        for fut, key in bounded_as_completed(futs, timeout=60):
            ...
    finally:
        ex.shutdown(wait=False)
"""
from __future__ import annotations

from concurrent.futures import Future, as_completed, TimeoutError as _TimeoutError
from typing import Any, Dict, Iterator, Optional, Tuple

DEFAULT_TIMEOUT_SECONDS = 90


def bounded_as_completed(
    futs: Dict[Future, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    on_timeout: Optional[callable] = None,
) -> Iterator[Tuple[Optional[Future], Any]]:
    """Drop-in replacement for `for fut in as_completed(futs): key = futs[fut]`.

    Yields (future, key) pairs as they complete, same as as_completed --
    but if `timeout` elapses before everything's done, yields (None, key)
    for each still-pending item instead of continuing to block, so the
    caller's loop always terminates within `timeout` seconds regardless
    of how many workers are genuinely stuck.

    `on_timeout`, if given, is called once with the list of keys that
    didn't finish in time (for logging) before those (None, key) pairs
    are yielded.
    """
    try:
        for fut in as_completed(futs, timeout=timeout):
            yield fut, futs[fut]
    except _TimeoutError:
        not_done = [key for fut, key in futs.items() if not fut.done()]
        if on_timeout:
            try:
                on_timeout(not_done)
            except Exception:
                pass
        for key in not_done:
            yield None, key
