# oiapp/services/schema_registry.py
"""
Single place that knows about every table-creation function in the app,
and calls all of them once at startup.

Why this exists: the app has 19 separate `ensure_table()`/`ensure_tables()`
functions, each called lazily -- only when that module's own code path
happens to run first (a specific route being hit, a specific watcher
firing). That's how `trade_snapshot` ended up missing a column on a real
database: the table already existed from months ago, so
`CREATE TABLE IF NOT EXISTS` was a no-op, and nothing else ever re-checked
it. It's also how a handful of tables (app_cache, app_config -- see the
DB validation pass) only get created the very first time a specific
button is clicked, rather than being ready from boot.

This module doesn't replace those 19 functions or duplicate their schema
definitions (a hand-maintained parallel registry would just be a new
place to drift out of sync with the real CREATE TABLE statements). It
just calls all of them, in one place, at startup -- so every table a
fresh install needs exists before the first request, instead of
depending on which feature happens to be used first.

Usage: call ensure_all_schemas() once from create_app().
"""
from __future__ import annotations

from typing import Callable, List, Tuple

# (label, import path, callable name) -- label is just for readable
# startup logging; nothing here duplicates the actual schema.
_ENSURE_FUNCTIONS: List[Tuple[str, str, str]] = [
    ("AI Hub", "oiapp.ai.ai_hub", "_ensure_tables"),
    ("AI Copilot", "oiapp.ai.copilot", "_ensure_tables"),
    ("Journal snapshot", "oiapp.journal.journal_snapshot", "ensure_snapshot_table"),
    ("CFTC COT", "oiapp.services.cftc_cot", "_ensure_table"),
    ("Metals OI Gate", "oiapp.services.metals_oi_gate", "_ensure_table"),
    ("Scoring Parameters", "oiapp.services.scoring_params", "_ensure_table"),
    ("Live Chain Tracker", "oiapp.services.live_chain_tracker", "_ensure_table"),
    ("GEX Trend Tracker", "oiapp.services.gex_trend_tracker", "_ensure_table"),
    ("Job registry", "oiapp.services.job_registry", "_ensure_table"),
    ("Futures OI (Schwab)", "oiapp.services.futures_oi_schwab", "_ensure_table"),
    ("Futures OI", "oiapp.services.futures_oi", "_ensure_table"),
    ("Recommendation history", "oiapp.services.recommendation_history", "_ensure_table"),
    ("Futures OI (real)", "oiapp.services.futures_oi_real", "_ensure_table"),
    ("Fundamentals", "oiapp.services.fundamentals", "_ensure_table"),
    ("Alert outbox", "oiapp.services.alert_outbox", "_ensure_table"),
    ("Technical snapshot", "oiapp.services.technical_snapshot", "_ensure_table"),
    ("Scanner Builder", "oiapp.scanners.scanner_builder", "_ensure_tables"),
    ("Signal Notifier", "oiapp.scanners.signal_notifier", "_ensure_table"),
    ("Regime scanner", "oiapp.scanners.regime_scanner", "_ensure_table"),
    ("Agentic AI Scanner", "oiapp.scanners.agentic_ai_scanner", "_ensure_tables"),
    ("Watchlist manager", "oiapp.scanners.watchlist_manager", "_ensure_tables"),
    ("Schwab EOD (auto-trading)", "oiapp.autotrading.schwab_eod", "_ensure_tables"),
    ("Schwab routes", "oiapp.schwab.schwab_routes", "_ensure_table"),
]


def ensure_all_schemas() -> dict:
    """Calls every registered ensure_table function once. Each is
    isolated -- one failing (e.g. a module with an optional dependency
    not installed) doesn't block the rest from running. Returns a
    summary dict for startup logging / a diagnostics check."""
    import importlib

    ok, failed = [], []
    for label, module_path, fn_name in _ENSURE_FUNCTIONS:
        try:
            mod = importlib.import_module(module_path)
            fn: Callable = getattr(mod, fn_name)
            fn()
            ok.append(label)
        except Exception as e:  # noqa: BLE001
            failed.append({"label": label, "module": module_path, "error": str(e)})

    print(f"[schema_registry] {len(ok)}/{len(_ENSURE_FUNCTIONS)} table schemas ready at startup"
          + (f", {len(failed)} failed" if failed else ""))
    for f in failed:
        print(f"[schema_registry] FAILED: {f['label']} ({f['module']}): {f['error']}")

    return {"ok": ok, "failed": failed, "total": len(_ENSURE_FUNCTIONS)}
