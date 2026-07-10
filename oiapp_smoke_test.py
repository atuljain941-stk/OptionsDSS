#!/usr/bin/env python3
"""OI app smoke test suite.

Run from the app source root, or point --base-url at a running server.

Examples:
  python oiapp_smoke_test.py
  python oiapp_smoke_test.py --base-url http://127.0.0.1:5050 --ui
  python oiapp_smoke_test.py --json-report smoke_report.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _log(msg: str) -> None:
    print(msg, flush=True)


def _http_get(url: str, timeout: int = 20) -> Tuple[int, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "oiapp-smoke-test/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get_content_type() or ""
            return resp.status, body, ctype
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        ctype = e.headers.get_content_type() if getattr(e, "headers", None) else ""
        return e.code, body, ctype


def _http_post_json(url: str, payload: Dict[str, Any], timeout: int = 20) -> Tuple[int, str, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "oiapp-smoke-test/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get_content_type() or ""
            return resp.status, body, ctype
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        ctype = e.headers.get_content_type() if getattr(e, "headers", None) else ""
        return e.code, body, ctype


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_json_or_text(text: str) -> str:
    t = text.strip()
    if len(t) > 240:
        return t[:240] + "…"
    return t


def _import_app_and_get_flask_app() -> Any:
    # Prefer local source root first.
    sys.path.insert(0, os.getcwd())
    try:
        from app import app as flask_app  # type: ignore
        return flask_app
    except Exception:
        # Try alternate common entry points.
        try:
            from oiapp.app_factory import create_app  # type: ignore
            return create_app()
        except Exception as e:
            raise RuntimeError(f"Could not import app: {e}") from e


def _check_route_presence(flask_app: Any, expected: List[str]) -> List[CheckResult]:
    rules = {r.rule for r in flask_app.url_map.iter_rules()}
    results: List[CheckResult] = []
    for route in expected:
        ok = route in rules
        detail = "present" if ok else f"missing (have {len(rules)} routes total)"
        results.append(CheckResult(f"route exists: {route}", ok, detail))
    return results


def _first_trade_id() -> Optional[int]:
    try:
        from oiapp.db import DB_PATH  # type: ignore
    except Exception:
        return None
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.execute("SELECT id FROM trades ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()
        con.close()
        return int(row[0]) if row else None
    except Exception:
        return None


def _test_client_endpoints(flask_app: Any, base_url: str) -> List[CheckResult]:
    results: List[CheckResult] = []
    client = flask_app.test_client()

    def get(path: str) -> Tuple[int, str, str]:
        resp = client.get(path)
        body = resp.get_data(as_text=True)
        ctype = resp.headers.get("Content-Type", "")
        return resp.status_code, body, ctype

    def post_json(path: str, payload: Dict[str, Any]) -> Tuple[int, str, str]:
        resp = client.post(path, json=payload)
        body = resp.get_data(as_text=True)
        ctype = resp.headers.get("Content-Type", "")
        return resp.status_code, body, ctype

    # Core pages / shell.
    page_paths = [
        "/",
        "/scanner",
        "/journal",
        "/scanner-builder/dashboard",
        "/api/scanner",
        "/api/journal",
        "/journal/trades",
        "/journal/trade-alerts",
        "/journal/portfolio_stats",
        "/journal/analytics",
        "/journal/portfolio_alignment",
        "/api/fetch_status",
        "/api/bootstrap",
        "/api/futures/config",
        "/scanner-builder/dashboard/api/watchlists",
        "/scanner-builder/dashboard/api/dashboards",
    ]
    for path in page_paths:
        status, body, ctype = get(path)
        ok = status == 200
        results.append(CheckResult(f"GET {path}", ok, f"status={status} content-type={ctype} body={_extract_json_or_text(body)}"))

    # Symbol/expiry driven API sanity.
    status, body, ctype = get("/api/expirations?symbol=SPY")
    exp_json = _safe_json(body)
    ok = status == 200 and isinstance(exp_json, (list, dict))
    results.append(CheckResult("GET /api/expirations?symbol=SPY", ok, f"status={status} body={_extract_json_or_text(body)}"))

    expiry = None
    if isinstance(exp_json, list) and exp_json:
        first = exp_json[0]
        if isinstance(first, dict):
            expiry = first.get("expiration") or first.get("expiry") or first.get("date")
        elif isinstance(first, str):
            expiry = first
    elif isinstance(exp_json, dict):
        for key in ("expirations", "data", "results"):
            v = exp_json.get(key)
            if isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, dict):
                    expiry = first.get("expiration") or first.get("expiry") or first.get("date")
                elif isinstance(first, str):
                    expiry = first
                break

    if expiry:
        for path in [
            f"/api/options?symbol=SPY&expiration={urllib.parse.quote(str(expiry))}",
            f"/api/oi_intelligence?symbol=SPY&expiration={urllib.parse.quote(str(expiry))}",
            f"/api/oi_change?symbol=SPY&expiration={urllib.parse.quote(str(expiry))}",
            f"/api/pcr_snapshot?symbol=SPY&expiration={urllib.parse.quote(str(expiry))}",
            f"/api/iv_rank/SPY",
        ]:
            status, body, ctype = get(path)
            ok = status == 200
            results.append(CheckResult(f"GET {path}", ok, f"status={status} body={_extract_json_or_text(body)}"))
    else:
        results.append(CheckResult("derive expiry from /api/expirations", False, "No expirations returned for SPY"))

    # Current trade live endpoints if DB has any trades.
    tid = _first_trade_id()
    if tid is not None:
        for path in [
            f"/journal/trade/{tid}/live",
            f"/journal/trade/{tid}/live_pnl",
            f"/journal/trade/{tid}/pnr_alert/test",
        ]:
            status, body, ctype = get(path)
            # We accept 200/4xx for invalid state, but not 500.
            ok = status < 500
            results.append(CheckResult(f"GET {path}", ok, f"status={status} body={_extract_json_or_text(body)}"))
    else:
        results.append(CheckResult("find first trade id", False, "No trade row found in DB; live trade endpoints skipped"))

    # POST sanity for routes that should not crash.
    for path, payload in [
        ("/api/fetch_now", {"symbols": ["SPY"]}),
        ("/api/fetch_stop", {}),
        ("/api/run_scheduler", {}),
    ]:
        status, body, ctype = post_json(path, payload)
        ok = status < 500
        results.append(CheckResult(f"POST {path}", ok, f"status={status} body={_extract_json_or_text(body)}"))

    return results


def _ui_smoke_test(base_url: str) -> List[CheckResult]:
    results: List[CheckResult] = []
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as e:
        return [CheckResult("playwright available", False, f"Playwright not installed: {e}")]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 1100})
            page.goto(base_url, wait_until="networkidle", timeout=60000)

            # Basic shell checks.
            title = page.title()
            results.append(CheckResult("page title loaded", bool(title), f"title={title!r}"))

            nav_text = page.locator("body").inner_text(timeout=10000)
            for token in ["Scanner Dashboard", "Journal", "GEX", "Alerts"]:
                ok = token.lower() in nav_text.lower()
                results.append(CheckResult(f"ui contains '{token}'", ok, f"present={ok}"))

            # Attempt to click scanner dashboard if visible.
            if page.get_by_text("Scanner Dashboard", exact=False).count() > 0:
                page.get_by_text("Scanner Dashboard", exact=False).first.click(timeout=15000)
                page.wait_for_load_state("networkidle", timeout=30000)
                body = page.locator("body").inner_text(timeout=10000)
                # Confirm menu is still visible after navigation.
                for token in ["Scanner Dashboard", "Journal", "GEX"]:
                    ok = token.lower() in body.lower()
                    results.append(CheckResult(f"after nav contains '{token}'", ok, f"present={ok}"))
            else:
                results.append(CheckResult("scanner dashboard nav click", False, "No visible 'Scanner Dashboard' text found"))

            browser.close()
    except Exception as e:
        results.append(CheckResult("browser smoke test", False, f"{e}\n{traceback.format_exc(limit=3)}"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="OI app smoke test suite")
    parser.add_argument("--base-url", default="http://127.0.0.1:5050", help="Running app base URL")
    parser.add_argument("--ui", action="store_true", help="Run optional Playwright UI smoke test")
    parser.add_argument("--json-report", default="", help="Write JSON report to this path")
    args = parser.parse_args()

    all_results: List[CheckResult] = []

    # Route presence + endpoint checks from local app import (best signal).
    try:
        flask_app = _import_app_and_get_flask_app()
        expected_routes = [
            "/",
            "/scanner",
            "/journal",
            "/scanner-builder/dashboard",
            "/api/scanner",
            "/api/journal",
            "/api/fetch_status",
            "/api/bootstrap",
            "/api/options",
            "/api/oi_intelligence",
            "/api/expirations",
            "/api/iv_rank/<symbol>",
            "/journal/trades",
            "/journal/trade-alerts",
            "/journal/trade/<int:tid>/live",
            "/journal/trade/<int:tid>/live_pnl",
        ]
        all_results.extend(_check_route_presence(flask_app, expected_routes))
        all_results.extend(_test_client_endpoints(flask_app, args.base_url))
    except Exception as e:
        all_results.append(CheckResult("import local app", False, f"{e}\n{traceback.format_exc(limit=3)}"))
        # Fallback to direct HTTP tests if import fails.
        for path in ["/", "/journal", "/scanner", "/scanner-builder/dashboard", "/api/fetch_status"]:
            status, body, ctype = _http_get(args.base_url + path)
            all_results.append(CheckResult(f"GET {path} (HTTP fallback)", status == 200, f"status={status} body={_extract_json_or_text(body)}"))

    if args.ui:
        all_results.extend(_ui_smoke_test(args.base_url))

    # Print summary.
    passed = sum(1 for r in all_results if r.ok)
    failed = [r for r in all_results if not r.ok]
    _log(f"\nSmoke test summary: {passed}/{len(all_results)} passed")
    if failed:
        _log("\nFailures:")
        for r in failed:
            _log(f"- {r.name}: {r.detail}")
    else:
        _log("\nAll checks passed.")

    if args.json_report:
        report = {
            "passed": passed,
            "total": len(all_results),
            "results": [asdict(r) for r in all_results],
        }
        Path(args.json_report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        _log(f"JSON report written to {args.json_report}")

    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
