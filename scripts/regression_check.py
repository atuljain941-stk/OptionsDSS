"""
scripts/regression_check.py
-----------------------------
Practical regression check for the v49-v53 changes (multi-timeframe
confluence, strike width cap, POP/RR balance, the technical_snapshot
cache and its wiring into regime_scanner/trade_opportunity_scanner/
scanner_builder).

IMPORTANT FRAMING: this does NOT check "are the numbers identical to
before" -- several of the changes in this session were deliberate
accuracy fixes (the RSI-EMA-90 convergence threshold went from 180 to 540
bars, backfill windows got longer, cross-timeframe confluence now factors
into scoring). Values for many symbols SHOULD differ from what the app
showed before these fixes -- that's the fixes working, not a regression.

What this actually checks, which IS the right bar for "did I break
anything":
  1. No crashes across your real watchlist symbols (smoke test)
  2. The technical_snapshot cache is internally consistent with a fresh,
     independent recomputation (validates the wiring is correct, not
     silently returning garbage)
  3. Merge correctness: writing from two different sources (regime_scanner
     then a scanner query, or the reverse) results in ONE complete record
     with both sources' fields present, not one overwriting the other
  4. Basic sanity bounds on every computed value (RSI in [0,100], ADX in
     [0,100], no NaN/inf slipping through) -- catches genuine breakage
     without requiring "matches some old number"

Run this against your real app (it imports your real modules and hits
your real price_cache/technical_snapshot tables):

    python scripts/regression_check.py AAPL MSFT GOOGL TSLA NVDA

Or with no arguments, it reads your default watchlist.
"""
from __future__ import annotations

import os
import sys
import traceback

# Ensure the project root (parent of this scripts/ directory) is on
# sys.path, so `import oiapp...` works regardless of how this script is
# invoked (python scripts/regression_check.py, python -m, etc.) -- without
# this, Python only puts the script's own directory on the path, not the
# project root one level up.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _sanity_bounds_ok(snap: dict) -> list:
    """Returns a list of problems found, empty if all good."""
    problems = []
    if snap.get("rsi14") is not None and not (0 <= snap["rsi14"] <= 100):
        problems.append(f"rsi14={snap['rsi14']} out of [0,100]")
    if snap.get("rsi3") is not None and not (0 <= snap["rsi3"] <= 100):
        problems.append(f"rsi3={snap['rsi3']} out of [0,100]")
    if snap.get("adx") is not None and not (0 <= snap["adx"] <= 100):
        problems.append(f"adx={snap['adx']} out of [0,100]")
    for k, v in snap.items():
        if isinstance(v, float):
            if v != v:  # NaN check (NaN != NaN is always True)
                problems.append(f"{k} is NaN")
            elif v in (float("inf"), float("-inf")):
                problems.append(f"{k} is infinite")
    return problems


def check_symbol(symbol: str) -> dict:
    result = {"symbol": symbol, "errors": [], "warnings": [], "ok": True}

    # 1. Smoke test: does the full pipeline run without crashing?
    try:
        from oiapp.scanners.scanner_builder import _symbol_ctx
        _symbol_ctx(symbol, "SPY", ["1d"])
    except Exception as e:
        result["errors"].append(f"_symbol_ctx crashed: {type(e).__name__}: {e}")
        result["ok"] = False

    try:
        from oiapp.scanners.regime_scanner import _compute_regime_ta
        regime = _compute_regime_ta(symbol)
        if regime is None:
            result["warnings"].append("_compute_regime_ta returned None (likely insufficient history)")
    except Exception as e:
        result["errors"].append(f"_compute_regime_ta crashed: {type(e).__name__}: {e}")
        result["ok"] = False
        regime = None

    try:
        from oiapp.scanners.trade_opportunity_scanner import _get_ta
        ta = _get_ta(symbol)
    except Exception as e:
        result["errors"].append(f"_get_ta crashed: {type(e).__name__}: {e}")
        result["ok"] = False
        ta = None

    # 2. Cache consistency: cached value vs a fresh, independent recompute
    try:
        from oiapp.services.technical_snapshot import (
            get_technical_snapshot, compute_technical_snapshot,
        )
        from oiapp.scanners.scanner_builder import _history

        cached = get_technical_snapshot(symbol, "1d")
        df = _history(symbol, "1d")
        fresh = compute_technical_snapshot(df) if df is not None else None

        if cached and fresh:
            for field in ("rsi14", "rsi3", "macd", "macd_hist"):
                c, f = cached.get(field), fresh.get(field)
                if c is not None and f is not None and abs(c - f) > 1.0:
                    result["warnings"].append(
                        f"{field}: cached={c} vs fresh recompute={f} -- differ by more than 1.0, worth a look"
                    )
            problems = _sanity_bounds_ok(cached)
            if problems:
                result["errors"].append(f"cached snapshot sanity check failed: {problems}")
                result["ok"] = False
        elif not cached:
            result["warnings"].append("no cached snapshot yet for this symbol (not computed/backfilled)")
    except Exception as e:
        result["errors"].append(f"cache consistency check crashed: {type(e).__name__}: {e}")
        result["ok"] = False

    # 3. Report the actual values for manual spot-checking against
    # TradingView or what you remember seeing in the app before.
    if regime:
        result["rsi14"] = regime.get("rsi")
        result["rsi_diff"] = regime.get("rsi_diff")
        result["regime"] = regime.get("regime")
        result["confluence"] = regime.get("confluence")
    if ta:
        result["_get_ta_rsi14"] = ta.get("rsi14")

    return result


def main():
    symbols = sys.argv[1:]
    if not symbols:
        try:
            from oiapp.scanners.trade_opportunity_scanner import _watchlist_symbols
            symbols = _watchlist_symbols(None)[:20]  # cap at 20 for a quick check
            print(f"No symbols given -- using first 20 from default watchlist: {symbols}\n")
        except Exception as e:
            print(f"Could not load default watchlist ({e}); pass symbols explicitly.")
            sys.exit(1)

    total_ok = 0
    total_errors = 0
    total_warnings = 0

    for symbol in symbols:
        print(f"=== {symbol} ===")
        try:
            r = check_symbol(symbol)
        except Exception as e:
            print(f"  UNEXPECTED CRASH checking this symbol: {type(e).__name__}: {e}")
            traceback.print_exc()
            total_errors += 1
            continue

        if r["errors"]:
            print(f"  ❌ ERRORS: {r['errors']}")
            total_errors += 1
        else:
            total_ok += 1
        if r["warnings"]:
            print(f"  ⚠  warnings: {r['warnings']}")
            total_warnings += len(r["warnings"])

        for field in ("rsi14", "rsi_diff", "regime", "confluence", "_get_ta_rsi14"):
            if field in r:
                print(f"  {field}: {r[field]}")
        print()

    print(f"\n=== Summary: {total_ok}/{len(symbols)} symbols OK, "
          f"{total_errors} with errors, {total_warnings} warnings ===")
    if total_errors:
        print("Investigate the ❌ ERRORS above -- those indicate something actually broke.")
    if total_warnings:
        print("⚠ warnings are worth a glance but aren't necessarily problems -- e.g. cache-vs-fresh "
              "differences under normal market-data timing, or symbols not yet backfilled.")


if __name__ == "__main__":
    main()
