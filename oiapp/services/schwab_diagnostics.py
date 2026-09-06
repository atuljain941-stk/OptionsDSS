"""schwab_diagnostics.py -- V134 addition. Mirrors icici_diagnostics.py.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from . import schwab_trading as trading


def _check(name: str, fn) -> Dict[str, Any]:
    start = time.time()
    try:
        detail = fn()
        ok = bool(detail.get("ok", True)) if isinstance(detail, dict) else True
        return {"name": name, "status": "pass" if ok else "fail", "detail": detail, "elapsed_sec": round(time.time() - start, 2)}
    except Exception as e:
        return {"name": name, "status": "error", "detail": {"error": str(e)}, "elapsed_sec": round(time.time() - start, 2)}


def run_diagnostics(test_symbol: str = "AAPL") -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []

    def check_connected():
        return {"ok": trading.is_connected()}
    conn_result = _check("Schwab OAuth connected (token valid)", check_connected)
    results.append(conn_result)

    def check_account_hash():
        ok = trading.has_account_hash()
        return {"ok": ok, "note": None if ok else "OAuth is valid but no account_hash is saved -- trading/positions calls need this. Set it via the existing Schwab config form (App Key/Secret panel), separate from the OAuth token itself."}
    results.append(_check("Trading account identified (account_hash)", check_account_hash))

    if conn_result["status"] != "pass":
        for name in ["Spot price fetch", "Available expiries fetch", "Option chain fetch", "Options quote fetch", "Account positions fetch"]:
            results.append({"name": name, "status": "skipped", "detail": {"reason": "not connected -- fix check #1 first"}, "elapsed_sec": 0})
    else:
        spot_holder, expiry_holder, chain_holder = {}, {}, {}

        def check_spot():
            r = trading.get_spot_price(test_symbol)
            spot_holder["spot"] = r.get("spot")
            return {"ok": r.get("ok"), **r}
        results.append(_check(f"Spot price fetch ({test_symbol})", check_spot))

        def check_expiries():
            r = trading.get_available_expiries(test_symbol)
            if r.get("expiries"):
                expiry_holder["expiry"] = r["expiries"][0]
            return {"ok": r.get("ok"), "count": len(r.get("expiries") or []), "sample": (r.get("expiries") or [])[:5], "error": r.get("error")}
        results.append(_check(f"Available expiries fetch ({test_symbol})", check_expiries))

        test_expiry = expiry_holder.get("expiry")

        def check_chain():
            if not test_expiry:
                return {"ok": False, "error": "no expiry from check #3"}
            r = trading.get_option_chain(test_symbol, test_expiry)
            if r.get("rows"):
                chain_holder["sample_row"] = r["rows"][0]
            return {"ok": r.get("ok") and bool(r.get("rows")), "row_count": len(r.get("rows") or []), "spot": r.get("spot"),
                    "sample_row": chain_holder.get("sample_row"), "error": r.get("error")}
        results.append(_check(f"Option chain fetch ({test_symbol}, expiry={test_expiry or 'N/A'})", check_chain))

        def check_quote():
            sample = chain_holder.get("sample_row")
            if not sample or not test_expiry:
                return {"ok": False, "error": "no sample strike from check #4"}
            occ = trading.to_occ_symbol(test_symbol, test_expiry, sample["right"], sample["strike_price"])
            r = trading.get_quote(occ)
            return {"ok": r.get("ok"), "occ_symbol": occ, "ltp": r.get("ltp"), "error": r.get("error")}
        results.append(_check("Options quote fetch (OCC symbol)", check_quote))

        def check_positions():
            r = trading.get_account_positions()
            return {"ok": r.get("ok"), "position_count": len(r.get("positions") or []), "error": r.get("error")}
        results.append(_check("Account positions fetch", check_positions))

    def check_scanner():
        from . import schwab_strategy_engine as strat
        r = strat.evaluate_scanner_condition(test_symbol, "ChangePct(close, 1, \"1d\") > -1000")
        return {"ok": r.get("ok"), "met": r.get("met"), "error": r.get("error")}
    results.append(_check("Scanner condition integration", check_scanner))

    def check_jobs():
        from .job_registry import get_last_run_at
        pnl_last = get_last_run_at("schwab_pnl_monitor")
        strat_last = get_last_run_at("schwab_strategy_evaluator")
        now = time.time()
        pnl_age = round(now - pnl_last, 1) if pnl_last else None
        strat_age = round(now - strat_last, 1) if strat_last else None
        ok = (pnl_age is not None and pnl_age < 120) and (strat_age is not None and strat_age < 180)
        return {"ok": ok, "pnl_monitor_seconds_ago": pnl_age, "strategy_evaluator_seconds_ago": strat_age}
    results.append(_check("Background jobs ticking", check_jobs))

    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] in ("fail", "error"))
    skipped = sum(1 for r in results if r["status"] == "skipped")
    return {"ok": True, "test_symbol": test_symbol,
            "summary": {"passed": passed, "failed": failed, "skipped": skipped, "total": len(results)},
            "results": results}
