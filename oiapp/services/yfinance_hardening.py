# oiapp/services/yfinance_hardening.py
"""
Single choke-point fix for yfinance rate limiting, instead of touching the
~211 call sites across ~44 files that call `yf.Ticker(...)` / `.history()`
/ `.option_chain()` / etc.

Why this is possible: yfinance's `YfData` class (yfinance/data.py) is a
true process-wide singleton (`metaclass=SingletonMeta`) -- "one session,
one cookie, shared by all threads" per yfinance's own docstring. Every
yfinance call in this app, no matter which of the 211 call sites it comes
from, funnels through the same one `YfData` instance's `_make_request()`
method and (before that) its `_get_crumb_basic()` cookie/crumb handshake.
That means patching those two methods ONCE, at startup, hardens every
yfinance call in the app -- no per-call-site changes needed, same
leverage as the shared task executor and centralized DB_PATH fixes.

Two problems fixed:

1. No retry on the crumb-fetch handshake. Yahoo's crumb endpoint
   (`/v1/test/getcrumb`) is hit once per process (the crumb is cached
   after that) to authenticate every subsequent request. yfinance's own
   `_get_crumb_basic()` raises YFRateLimitError immediately on a 429
   with ZERO retry -- if that one handshake happens to land during a
   burst (e.g. several background watchers/scanners all waking near
   app startup and each trying to be the first to mint a crumb), the
   whole app can go a while without being able to fetch anything from
   yfinance until something calls it again. This patch adds bounded
   exponential-backoff retry (4 attempts, 1/2/4/8s) around just that
   handshake.

2. No global throttle. This app now has many independent
   scanners/watchers that each call yfinance, and while the Scheduler
   Hub (unified_scheduler.py) and shared task executor
   (task_executor.py) reduce *background* concurrency, interactive
   requests (like Scanner Builder's live_prices lookup) can still stack
   on top. This patch adds a lightweight minimum-interval throttle
   (default 150ms between calls, ~6-7 req/s ceiling) shared across
   every yfinance call in the process, configurable via the
   OIAPP_YF_MIN_INTERVAL_SEC environment variable.

Call install() once at app startup (see app_factory.py). Idempotent --
safe to call more than once (e.g. under a dev-server reloader).
"""
from __future__ import annotations

import os
import threading
import time

_installed = False
_install_lock = threading.Lock()


def install() -> bool:
    global _installed
    with _install_lock:
        if _installed:
            return False
        try:
            _patch_crumb_retry()
        except Exception as e:  # noqa: BLE001
            print(f"[yfinance_hardening] crumb-retry patch failed (non-fatal): {e}")
        try:
            _patch_global_throttle()
        except Exception as e:  # noqa: BLE001
            print(f"[yfinance_hardening] throttle patch failed (non-fatal): {e}")
        _installed = True
        print("[yfinance_hardening] installed: crumb-fetch retry + global request throttle")
        return True


def _patch_crumb_retry() -> None:
    from yfinance.data import YfData
    from yfinance.exceptions import YFRateLimitError

    if getattr(YfData._get_crumb_basic, "_oiapp_patched", False):
        return

    original = YfData._get_crumb_basic
    max_attempts = 4
    base_delay = 1.0

    def patched(self, timeout=30):
        last_exc = None
        for attempt in range(max_attempts):
            try:
                return original(self, timeout=timeout)
            except YFRateLimitError as e:
                last_exc = e
                if attempt < max_attempts - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"[yfinance_hardening] rate-limited fetching crumb "
                          f"(attempt {attempt + 1}/{max_attempts}), retrying in {delay:.0f}s")
                    time.sleep(delay)
        raise last_exc

    patched._oiapp_patched = True
    YfData._get_crumb_basic = patched


def _patch_global_throttle() -> None:
    from yfinance.data import YfData

    if getattr(YfData._make_request, "_oiapp_patched", False):
        return

    original = YfData._make_request
    min_interval = float(os.environ.get("OIAPP_YF_MIN_INTERVAL_SEC", "0.15"))
    lock = threading.Lock()
    state = {"last_call": 0.0}

    def patched(self, *args, **kwargs):
        with lock:
            now = time.monotonic()
            wait = min_interval - (now - state["last_call"])
            if wait > 0:
                time.sleep(wait)
            state["last_call"] = time.monotonic()
        return original(self, *args, **kwargs)

    patched._oiapp_patched = True
    YfData._make_request = patched
