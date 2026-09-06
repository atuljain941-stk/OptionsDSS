#!/usr/bin/env python3
"""Regression suite for OI app baseline.

Checks:
- HTTP route availability
- Core JSON API health
- UI tab navigation through the main shell with Selenium
- Console/page errors
- Basic visual evidence via screenshots

Usage:
    python oiapp_regression_suite.py --base-url http://127.0.0.1:5050 --out report.json

Optional:
    --tabs dashboard scanner-builder scanner-dashboard journal gexplan alerts scheduler ...
    --skip-ui   only run HTTP/API checks
    --browser chrome|edge
    --headless / --no-headless
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from requests import Response

# Selenium is optional for route/API only mode.
try:
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.edge.options import Options as EdgeOptions
except Exception:  # pragma: no cover - runtime dependency
    webdriver = None  # type: ignore
    TimeoutException = Exception  # type: ignore
    WebDriverException = Exception  # type: ignore
    By = EC = WebDriverWait = None  # type: ignore
    ChromeOptions = EdgeOptions = None  # type: ignore


CORE_API_PATHS = [
    "/api/fetch_status",
    "/api/bootstrap",
    "/api/expirations",
    "/api/options",
    "/api/oi_intelligence",
    "/api/iv_rank",
    "/api/oi_change",
    "/api/pcr_snapshot",
    "/api/symbols",
]

# These are the app-shell tabs typically present in the baseline.
DEFAULT_TABS = [
    "dashboard",
    "aggregate",
    "news",
    "sectors",
    "heatmap",
    "gexplan",
    "intraday",
    "oibuildup",
    "regime",
    "inst-scan",
    "maya-scanner",
    "srbreak",
    "mom-retrace",
    "rsi-mtf",
    "spy",
    "scanner",
    "market-structure",
    "weeklyplan",
    "weeklyanalysis",
    "earnings",
    "planner",
    "candidate-board",
    "watchlists",
    "alerts",
    "scheduler",
    "scanner-builder",
    "scanner-dashboard",
    "journal",
    "maya-journal",
    "sql",
    "playbook",
]

TAB_FALLBACK_SELECTORS = [
    "[data-tab]",
    "button[data-tab]",
    "a[data-tab]",
]

KNOWN_ERROR_PATTERNS = [
    r"ReferenceError",
    r"TypeError",
    r"SyntaxError",
    r"Unhandled promise rejection",
    r"500 Internal Server Error",
    r"404 Not Found",
    r"t is not defined",
    r"currentSymbol is undefined",
]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class UiTabResult:
    tab: str
    ok: bool
    title: str = ""
    url: str = ""
    visible_text_sample: str = ""
    errors: List[str] = None
    screenshots: List[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.screenshots is None:
            self.screenshots = []


class Report:
    def __init__(self) -> None:
        self.route_checks: List[CheckResult] = []
        self.api_checks: List[CheckResult] = []
        self.ui_tabs: List[UiTabResult] = []
        self.meta: Dict[str, Any] = {}

    def summary(self) -> Tuple[int, int]:
        total = len(self.route_checks) + len(self.api_checks) + len(self.ui_tabs)
        passed = sum(1 for x in self.route_checks if x.ok) + sum(1 for x in self.api_checks if x.ok) + sum(1 for x in self.ui_tabs if x.ok)
        return passed, total

    def to_dict(self) -> Dict[str, Any]:
        return {
            "meta": self.meta,
            "route_checks": [asdict(x) for x in self.route_checks],
            "api_checks": [asdict(x) for x in self.api_checks],
            "ui_tabs": [asdict(x) for x in self.ui_tabs],
        }


def norm_base(url: str) -> str:
    return url.rstrip("/")


def http_get(url: str, timeout: float = 15.0) -> Response:
    return requests.get(url, timeout=timeout)


def check_paths(base_url: str, report: Report) -> None:
    # These are intentionally lightweight; they just confirm something meaningful responds.
    route_paths = [
        "/",
        "/scanner-builder",
        "/scanner-builder/dashboard",
        "/journal",
        "/api/journal",
        "/api/fetch_status",
        "/api/bootstrap",
    ]
    for path in route_paths:
        url = base_url + path
        try:
            resp = http_get(url)
            ok = resp.status_code in (200, 302, 401, 403)
            report.route_checks.append(
                CheckResult(
                    name=f"GET {path}",
                    ok=ok,
                    detail=f"status={resp.status_code} content-type={resp.headers.get('content-type','')}"
                    + ("" if ok else f" body={resp.text[:200]!r}"),
                )
            )
        except Exception as e:
            report.route_checks.append(CheckResult(name=f"GET {path}", ok=False, detail=str(e)))


def check_api(base_url: str, report: Report) -> None:
    for path in CORE_API_PATHS:
        url = base_url + path
        try:
            resp = http_get(url, timeout=20)
            ct = resp.headers.get("content-type", "")
            ok = resp.status_code == 200 and ("json" in ct.lower() or resp.text.strip().startswith("{") or resp.text.strip().startswith("["))
            detail = f"status={resp.status_code} ct={ct} body={resp.text[:250]!r}"
            report.api_checks.append(CheckResult(name=path, ok=ok, detail=detail))
        except Exception as e:
            report.api_checks.append(CheckResult(name=path, ok=False, detail=str(e)))


def build_driver(browser: str, headless: bool):
    if webdriver is None:
        raise RuntimeError("Selenium is not installed in this environment.")

    browser = browser.lower()
    if browser not in ("chrome", "edge"):
        raise ValueError("browser must be chrome or edge")

    if browser == "chrome":
        opts = ChromeOptions()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,2200")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--log-level=3")
        # Selenium Manager will usually obtain the driver automatically.
        return webdriver.Chrome(options=opts)
    else:
        opts = EdgeOptions()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,2200")
        opts.add_argument("--disable-gpu")
        return webdriver.Edge(options=opts)


def inject_console_capture(driver) -> None:
    driver.execute_script(
        """
        (function() {
          if (window.__oiapp_console_patched) return;
          window.__oiapp_console_patched = true;
          window.__oiapp_console_errors = [];
          window.__oiapp_page_errors = [];
          const push = function(kind, args) {
            try {
              const msg = Array.from(args).map(v => {
                try { return typeof v === 'string' ? v : JSON.stringify(v); } catch (e) { return String(v); }
              }).join(' ');
              window.__oiapp_console_errors.push({kind, msg, ts: Date.now()});
            } catch (e) {}
          };
          const origError = console.error;
          const origWarn = console.warn;
          console.error = function() { push('error', arguments); return origError.apply(console, arguments); };
          console.warn = function() { push('warn', arguments); return origWarn.apply(console, arguments); };
          window.addEventListener('error', function(ev) {
            try {
              window.__oiapp_page_errors.push({msg: ev.message || 'error', source: ev.filename || '', line: ev.lineno || 0, col: ev.colno || 0});
            } catch (e) {}
          });
          window.addEventListener('unhandledrejection', function(ev) {
            try {
              window.__oiapp_page_errors.push({msg: String(ev.reason || 'unhandledrejection')});
            } catch (e) {}
          });
        })();
        """
    )


def get_js_errors(driver) -> List[str]:
    errors: List[str] = []
    try:
        js = driver.execute_script(
            "return {console: window.__oiapp_console_errors || [], page: window.__oiapp_page_errors || []};"
        )
        for rec in js.get("console", []):
            errors.append(f"console[{rec.get('kind')}]: {rec.get('msg')}")
        for rec in js.get("page", []):
            errors.append(f"page: {rec.get('msg')} @ {rec.get('source')}:{rec.get('line')}:{rec.get('col')}")
    except Exception as e:
        errors.append(f"error reading JS errors: {e}")
    return errors


def page_has_known_errors(text: str, errors: List[str]) -> List[str]:
    found = []
    hay = (text or "") + "\n" + "\n".join(errors)
    for pat in KNOWN_ERROR_PATTERNS:
        if re.search(pat, hay, re.IGNORECASE):
            found.append(pat)
    return found


def find_visible_tabs(driver, requested_tabs: Iterable[str]) -> List[Tuple[str, Any]]:
    # Prefer actual [data-tab] elements and then filter to those that are visible/clickable.
    els = []
    for sel in TAB_FALLBACK_SELECTORS:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                break
        except Exception:
            continue
    if not els:
        return []

    seen = []
    for el in els:
        try:
            tab = el.get_attribute("data-tab") or ""
            if tab and tab not in seen:
                seen.append(tab)
        except Exception:
            pass

    requested = list(requested_tabs)
    if requested:
        # Keep requested order; if not found in DOM, still report it later.
        ordered = [t for t in requested if t in seen]
        # include any extra DOM tabs at end for discovery
        for t in seen:
            if t not in ordered:
                ordered.append(t)
        return [(t, None) for t in ordered]
    return [(t, None) for t in seen]


def open_and_probe_tab(driver, base_url: str, tab: str, out_dir: Path) -> UiTabResult:
    result = UiTabResult(tab=tab)
    # Click the nav item via JS by data-tab; if not present, we still try URL fallbacks.
    clicked = False
    selectors = [f'[data-tab="{tab}"]', f'button[data-tab="{tab}"]', f'a[data-tab="{tab}"]']
    for sel in selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                el = els[0]
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                try:
                    el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", el)
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        # fallback: if tab is not embedded, try navigating to route heuristically.
        route_guess = {
            "scanner-dashboard": "/scanner-builder/dashboard",
            "scanner-builder": "/scanner-builder",
            "journal": "/journal",
            "gexplan": "/",
        }.get(tab)
        if route_guess:
            driver.get(base_url + route_guess)
            clicked = True

    # Allow async render to settle.
    time.sleep(1.2)
    try:
        WebDriverWait(driver, 10).until(lambda d: d.execute_script("return document.readyState") == "complete")
    except Exception:
        pass

    try:
        result.title = driver.title
        result.url = driver.current_url
        body_text = driver.find_element(By.TAG_NAME, "body").text
        result.visible_text_sample = body_text[:1000]
        result.errors = get_js_errors(driver)
        result.screenshots = []
        shot_path = out_dir / f"{tab}.png"
        driver.save_screenshot(str(shot_path))
        result.screenshots.append(str(shot_path))
        known = page_has_known_errors(body_text, result.errors)
        # A tab is considered OK if it loaded some body text and no known errors were found.
        has_meaningful_text = bool(body_text.strip())
        result.ok = has_meaningful_text and not known
        if known:
            result.errors.extend([f"known-pattern: {x}" for x in known])
    except Exception as e:
        result.ok = False
        result.errors.append(str(e))
    return result


def run_ui(base_url: str, out_dir: Path, tabs: List[str], browser: str, headless: bool) -> List[UiTabResult]:
    out_dir.mkdir(parents=True, exist_ok=True)
    driver = build_driver(browser=browser, headless=headless)
    driver.set_page_load_timeout(30)
    results: List[UiTabResult] = []
    try:
        driver.get(base_url)
        try:
            WebDriverWait(driver, 15).until(lambda d: d.execute_script("return document.readyState") == "complete")
        except Exception:
            pass
        inject_console_capture(driver)
        # give SPA boot a moment
        time.sleep(1.5)

        # Take baseline screenshot
        try:
            driver.save_screenshot(str(out_dir / "__home.png"))
        except Exception:
            pass

        # Read nav tabs that exist on the page to avoid clicking non-existent elements.
        visible_tabs = [t for t, _ in find_visible_tabs(driver, tabs)]
        # If caller supplied tabs, probe those; otherwise probe visible tabs from DOM.
        if tabs:
            probe_tabs = [t for t in tabs if t in visible_tabs or True]
        else:
            probe_tabs = visible_tabs

        # Ensure the essential tabs are checked even if DOM discovery misses them.
        essentials = ["dashboard", "scanner-builder", "scanner-dashboard", "journal", "gexplan", "alerts", "scheduler"]
        for t in essentials:
            if t not in probe_tabs:
                probe_tabs.append(t)

        seen = set()
        for tab in probe_tabs:
            if tab in seen:
                continue
            seen.add(tab)
            res = open_and_probe_tab(driver, base_url, tab, out_dir)
            results.append(res)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5050")
    parser.add_argument("--out", default="oiapp_regression_report.json")
    parser.add_argument("--browser", choices=["chrome", "edge"], default="chrome")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-ui", action="store_true")
    parser.add_argument("--tabs", nargs="*", default=DEFAULT_TABS)
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()

    base_url = norm_base(args.base_url)
    out_path = Path(args.out)
    out_dir = out_path.with_suffix("")
    out_dir.mkdir(parents=True, exist_ok=True)

    report = Report()
    report.meta = {
        "base_url": base_url,
        "browser": args.browser,
        "headless": args.headless,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    print(f"[1/3] Checking routes at {base_url}...")
    check_paths(base_url, report)
    print(f"[2/3] Checking APIs...")
    check_api(base_url, report)

    if not args.skip_ui:
        print(f"[3/3] Running Selenium UI checks...")
        try:
            ui_results = run_ui(base_url, out_dir=out_dir, tabs=args.tabs, browser=args.browser, headless=args.headless)
            report.ui_tabs.extend(ui_results)
        except Exception as e:
            report.ui_tabs.append(UiTabResult(tab="__ui_bootstrap__", ok=False, errors=[str(e)]))

    passed, total = report.summary()
    out_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    print()
    print(f"Smoke/regression summary: {passed}/{total} passed")

    fail_lines = []
    for item in report.route_checks + report.api_checks:
        if not item.ok:
            fail_lines.append(f"- {item.name}: {item.detail}")
    for item in report.ui_tabs:
        if not item.ok:
            detail = "; ".join(item.errors[:8]) if item.errors else "failed"
            fail_lines.append(f"- UI {item.tab}: {detail}")
    if fail_lines:
        print("Failures:")
        for line in fail_lines:
            print(line)
    else:
        print("All checks passed.")

    print(f"JSON report written to {out_path}")
    print(f"Screenshots written to {out_dir}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
