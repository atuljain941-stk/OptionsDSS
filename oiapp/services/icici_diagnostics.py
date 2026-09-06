"""icici_diagnostics.py -- V127 addition.

One-click test suite covering every integration point in this build
that's been flagged "NEEDS LIVE VALIDATION" across icici_breeze.py,
icici_positions.py, icici_strategy_engine.py, and the Scanner Builder
symbol-override addition -- run them all at once and see exactly which
ones actually work against your real Breeze session, instead of
finding out one bug at a time through a screenshot-and-guess cycle.

Each check is independent and never places a real order or opens a
position -- read-only calls only (quotes, chain, expiries, session
status, job timestamps), plus one harmless scanner-condition
evaluation against a query that's expected to just return true/false.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from . import icici_breeze as breeze


def _check(name: str, fn) -> Dict[str, Any]:
    start = time.time()
    try:
        detail = fn()
        ok = bool(detail.get("ok", True)) if isinstance(detail, dict) else True
        return {"name": name, "status": "pass" if ok else "fail",
                "detail": detail, "elapsed_sec": round(time.time() - start, 2)}
    except Exception as e:
        return {"name": name, "status": "error", "detail": {"error": str(e)},
                "elapsed_sec": round(time.time() - start, 2)}


def run_diagnostics(test_stock_code: str = "NIFTY") -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []

    # 1. Credentials configured at all
    def check_credentials():
        cfg = breeze.get_config() or {}
        configured = bool(cfg.get("api_key") and cfg.get("api_secret"))
        return {"ok": configured, "api_key_set": bool(cfg.get("api_key")), "api_secret_set": bool(cfg.get("api_secret"))}
    results.append(_check("Breeze API credentials configured", check_credentials))

    # 2. Session active (or auto-reconnectable from a saved token)
    def check_session():
        status = breeze.session_status()
        return {"ok": status.get("active"), **status}
    session_result = _check("Breeze session active", check_session)
    results.append(session_result)

    session_ok = session_result["status"] == "pass"
    if not session_ok:
        # Every check below needs a live session -- report them as
        # skipped rather than letting each one fail with the same
        # "no active session" error individually.
        for name in [
            "Spot price fetch (get_spot_price)",
            "Options quote fetch (get_quote)",
            "Option chain fetch (get_option_chain)",
            "Available expiries fetch (get_available_expiries)",
            "Portfolio positions fetch (get_portfolio_positions)",
        ]:
            results.append({"name": name, "status": "skipped",
                             "detail": {"reason": "no active Breeze session -- fix check #2 first"},
                             "elapsed_sec": 0})
    else:
        # 3. Spot price
        spot_holder = {}
        def check_spot():
            r = breeze.get_spot_price(test_stock_code)
            spot_holder["spot"] = r.get("spot")
            return {"ok": r.get("ok"), **r}
        results.append(_check(f"Spot price fetch ({test_stock_code})", check_spot))

        # 4. Expiries
        expiry_holder = {}
        def check_expiries():
            r = breeze.get_available_expiries(test_stock_code)
            if r.get("expiries"):
                expiry_holder["expiry"] = r["expiries"][0]
            return {"ok": r.get("ok"), "count": len(r.get("expiries") or []), "sample": (r.get("expiries") or [])[:5], "error": r.get("error")}
        results.append(_check(f"Available expiries fetch ({test_stock_code})", check_expiries))

        test_expiry = expiry_holder.get("expiry")

        # 5. Option chain (needs an expiry from #4)
        chain_holder = {}
        def check_chain():
            if not test_expiry:
                return {"ok": False, "error": "no expiry available from check #4 -- cannot test chain"}
            r = breeze.get_option_chain(test_stock_code, test_expiry)
            if r.get("rows"):
                chain_holder["sample_row"] = r["rows"][0]
            return {"ok": r.get("ok") and bool(r.get("rows")), "row_count": len(r.get("rows") or []),
                    "spot": r.get("spot"), "sample_row": chain_holder.get("sample_row"),
                    "debug": r.get("debug"), "error": r.get("error")}
        results.append(_check(f"Option chain fetch ({test_stock_code}, expiry={test_expiry or 'N/A'})", check_chain))

        # 6. Single quote (needs a strike from #5's sample row)
        def check_quote():
            sample = chain_holder.get("sample_row")
            if not sample or not test_expiry:
                return {"ok": False, "error": "no sample strike available from check #5 -- cannot test quote"}
            r = breeze.get_quote(test_stock_code, test_expiry, sample["right"], sample["strike_price"])
            return {"ok": r.get("ok"), "tested_strike": sample["strike_price"], "tested_right": sample["right"],
                    "ltp": r.get("ltp"), "error": r.get("error")}
        results.append(_check("Options quote fetch (get_quote)", check_quote))

        # 7. Portfolio positions (real account data, read-only)
        def check_portfolio():
            r = breeze.get_portfolio_positions()
            return {"ok": r.get("ok"), "position_count": len(r.get("positions") or []), "error": r.get("error")}
        results.append(_check("Portfolio positions fetch", check_portfolio))

    # 8. Scanner condition integration (doesn't need a live session --
    # tests the internal Flask call + the symbol-override addition to
    # /scanner-builder/api/run, independent of Breeze entirely)
    def check_scanner():
        from . import icici_strategy_engine as strat
        r = strat.evaluate_scanner_condition(test_stock_code, "ChangePct(close, 1, \"1d\") > -1000")  # trivially near-always-true sanity query
        return {"ok": r.get("ok"), "met": r.get("met"), "error": r.get("error")}
    results.append(_check("Scanner condition integration (Scanner Builder symbol override)", check_scanner))

    # 9a. SDK ground truth -- introspects the ACTUAL installed
    # breeze_connect package's get_option_chain_quotes signature/
    # docstring rather than continuing to guess at required params
    # from general docs. Runs regardless of session state (pure
    # local introspection, no API call).
    def check_sdk_signature():
        try:
            import inspect
            from breeze_connect import BreezeConnect
            sig = str(inspect.signature(BreezeConnect.get_option_chain_quotes))
            doc = (inspect.getdoc(BreezeConnect.get_option_chain_quotes) or "").strip()
            return {"ok": True, "signature": sig, "docstring": doc[:800]}
        except ImportError:
            return {"ok": False, "error": "breeze-connect not installed in this environment"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    results.append(_check("Breeze SDK get_option_chain_quotes() actual signature", check_sdk_signature))

    # 9b. Full method list -- get_option_chain_quotes() apparently has
    # no "discover all expiries" mode at all (two different guesses at
    # satisfying its required-params validation both failed with two
    # DIFFERENT error messages, suggesting expiry_date is essentially
    # always required by this specific method). Rather than guess a
    # third time, list every method the SDK actually exposes so a real
    # alternative (e.g. a dedicated instrument-master/names lookup) can
    # be identified from real evidence instead of documentation guesses.
    def check_sdk_methods():
        try:
            from breeze_connect import BreezeConnect
            methods = sorted(m for m in dir(BreezeConnect) if not m.startswith("_") and callable(getattr(BreezeConnect, m, None)))
            return {"ok": True, "method_count": len(methods), "methods": methods}
        except ImportError:
            return {"ok": False, "error": "breeze-connect not installed in this environment"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    results.append(_check("Breeze SDK -- full method list (for expiry-discovery alternatives)", check_sdk_methods))

    # 9. Background jobs actually ticking
    def check_jobs():
        from .job_registry import get_last_run_at
        pnl_last = get_last_run_at("icici_pnl_monitor")
        strat_last = get_last_run_at("icici_strategy_evaluator")
        now = time.time()
        pnl_age = round(now - pnl_last, 1) if pnl_last else None
        strat_age = round(now - strat_last, 1) if strat_last else None
        ok = (pnl_age is not None and pnl_age < 120) and (strat_age is not None and strat_age < 180)
        return {"ok": ok, "pnl_monitor_seconds_ago": pnl_age, "strategy_evaluator_seconds_ago": strat_age}
    results.append(_check("Background jobs ticking (P&L monitor + strategy evaluator)", check_jobs))

    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] in ("fail", "error"))
    skipped = sum(1 for r in results if r["status"] == "skipped")
    return {
        "ok": True, "test_stock_code": test_stock_code,
        "summary": {"passed": passed, "failed": failed, "skipped": skipped, "total": len(results)},
        "results": results,
    }
