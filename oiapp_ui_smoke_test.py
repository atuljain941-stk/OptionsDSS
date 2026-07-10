#!/usr/bin/env python3
"""UI/API smoke test for the OI app.

Covers:
- app shell loads
- nav/tab clicks inside the main container
- route and API failures (4xx/5xx) during interaction
- console/page errors
- screenshots per visited tab

Usage:
  python oiapp_ui_smoke_test.py --base-url http://127.0.0.1:5050 --out report.json

Optional:
  python oiapp_ui_smoke_test.py --base-url http://127.0.0.1:5050 --headless false
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


SKIP_URL_PATTERNS = [
    re.compile(r"/favicon\.ico$"),
    re.compile(r"\.(css|js|map|png|jpg|jpeg|gif|svg|webp|ico|woff2?)($|\?)", re.I),
    re.compile(r"/static/", re.I),
    re.compile(r"/assets/", re.I),
]

NAV_CONTAINER_SELECTORS = [
    "nav",
    "aside",
    "[role='tablist']",
    ".sidebar",
    ".menu",
    ".navbar",
    ".app-nav",
]

CANDIDATE_SELECTORS = [
    "a[href]",
    "button",
    "[role='tab']",
]


@dataclass
class VisitResult:
    label: str
    kind: str
    url: str
    title: str
    ok: bool
    status: str
    errors: List[str]
    page_errors: List[str]
    screenshots: List[str]


@dataclass
class Failure:
    category: str
    where: str
    detail: str


def is_skipped_url(url: str) -> bool:
    return any(p.search(url) for p in SKIP_URL_PATTERNS)


def same_origin(base: str, target: str) -> bool:
    b = urlparse(base)
    t = urlparse(target)
    return (t.scheme in ("http", "https") and t.netloc == b.netloc) or target.startswith("/")


def normalize_href(base_url: str, href: str) -> str:
    return urljoin(base_url, href)


async def collect_nav_candidates(page) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen = set()

    for container in NAV_CONTAINER_SELECTORS:
        loc = page.locator(container)
        count = await loc.count()
        for idx in range(count):
            box = loc.nth(idx)
            for sel in CANDIDATE_SELECTORS:
                items = box.locator(sel)
                n = await items.count()
                for j in range(n):
                    el = items.nth(j)
                    try:
                        text = (await el.inner_text()).strip()
                    except Exception:
                        text = ""
                    try:
                        href = await el.get_attribute("href")
                    except Exception:
                        href = None
                    try:
                        aria = await el.get_attribute("aria-label")
                    except Exception:
                        aria = None
                    label = text or (aria or "").strip()
                    if not label:
                        continue
                    if len(label) > 120:
                        label = label[:120]
                    key = (container, sel, label, href or "")
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append({
                        "container": container,
                        "selector": sel,
                        "label": label,
                        "href": href or "",
                    })
    return candidates


async def run(base_url: str, out_path: Path, headless: bool = True, timeout_ms: int = 20000) -> int:
    failures: List[Failure] = []
    visits: List[VisitResult] = []
    network_failures: List[Dict[str, Any]] = []
    all_requests: List[Dict[str, Any]] = []
    page_errors: List[str] = []

    out_dir = out_path.parent / f"{out_path.stem}_screens"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        r = requests.get(base_url, timeout=10)
        if r.status_code >= 400:
            failures.append(Failure("bootstrap", base_url, f"HTTP {r.status_code}"))
    except Exception as e:
        failures.append(Failure("bootstrap", base_url, f"request failed: {e}"))
        out_path.write_text(json.dumps({"failures": [asdict(f) for f in failures]}, indent=2), encoding="utf-8")
        return 2

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1600, "height": 1100})
        page = await context.new_page()

        def on_request(req):
            all_requests.append({"url": req.url, "method": req.method, "resource_type": req.resource_type})

        def on_response(resp):
            url = resp.url
            if is_skipped_url(url):
                return
            if resp.status >= 400:
                network_failures.append({"url": url, "status": resp.status, "method": resp.request.method})

        def on_console(msg):
            if msg.type in {"error", "warning"}:
                page_errors.append(f"{msg.type.upper()}: {msg.text}")

        def on_page_error(exc):
            page_errors.append(f"PAGEERROR: {exc}")

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("console", on_console)
        page.on("pageerror", on_page_error)

        await page.goto(base_url, wait_until="networkidle", timeout=timeout_ms)
        await page.wait_for_timeout(1500)

        # collect candidates after the app shell has rendered
        candidates = await collect_nav_candidates(page)

        # Always include the current page itself
        candidates.insert(0, {"container": "initial", "selector": "goto", "label": "Initial load", "href": base_url})

        seen_visit_keys = set()
        for item in candidates:
            label = item["label"]
            href = item["href"]
            kind = item["selector"]
            visit_key = (label, href, kind)
            if visit_key in seen_visit_keys:
                continue
            seen_visit_keys.add(visit_key)

            local_errors_start = len(page_errors)
            local_net_start = len(network_failures)
            screenshot_paths: List[str] = []
            ok = True
            status = "ok"
            target_url = page.url
            title = ""

            try:
                if href and href != "#" and same_origin(base_url, href):
                    full = normalize_href(base_url, href)
                    await page.goto(full, wait_until="networkidle", timeout=timeout_ms)
                else:
                    # Try to click the element in its container.
                    # Re-query by text to avoid stale locators after navigation.
                    locator = None
                    for container in NAV_CONTAINER_SELECTORS:
                        box = page.locator(container)
                        count = await box.count()
                        for idx in range(count):
                            scoped = box.nth(idx)
                            for sel in CANDIDATE_SELECTORS:
                                loc = scoped.locator(sel, has_text=label)
                                if await loc.count():
                                    locator = loc.first
                                    break
                            if locator:
                                break
                        if locator:
                            break
                    if locator is None:
                        # fallback: search by text anywhere visible
                        locator = page.get_by_text(label, exact=False).first
                    await locator.click(timeout=timeout_ms)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
                    except PlaywrightTimeoutError:
                        await page.wait_for_timeout(1200)
                await page.wait_for_timeout(700)
                target_url = page.url
                title = await page.title()
                shot = out_dir / f"{len(visits):03d}_{re.sub(r'[^A-Za-z0-9_-]+', '_', label)[:50]}.png"
                await page.screenshot(path=str(shot), full_page=True)
                screenshot_paths.append(str(shot))
                # basic sanity: page should not be a generic 404 page
                body_text = await page.locator("body").inner_text(timeout=3000)
                if "Not Found" in body_text and "requested URL was not found" in body_text:
                    ok = False
                    status = "404 page"
                    failures.append(Failure("ui-route", label, f"404 page at {target_url}"))
            except Exception as e:
                ok = False
                status = f"error: {e}"
                failures.append(Failure("ui-navigation", label, str(e)))

            new_errors = page_errors[local_errors_start:]
            new_net = network_failures[local_net_start:]
            if new_errors:
                ok = False
            if new_net:
                ok = False

            visits.append(VisitResult(
                label=label,
                kind=kind,
                url=target_url,
                title=title,
                ok=ok,
                status=status,
                errors=new_net and [f"{n['method']} {n['status']} {n['url']}" for n in new_net] or [],
                page_errors=new_errors,
                screenshots=screenshot_paths,
            ))

        # Summarize all API/network failures excluding static assets.
        for n in network_failures:
            failures.append(Failure("network", n["url"], f"{n['method']} {n['status']}"))

        await browser.close()

    report = {
        "base_url": base_url,
        "passed": sum(1 for v in visits if v.ok),
        "failed": sum(1 for v in visits if not v.ok),
        "visits": [asdict(v) for v in visits],
        "failures": [asdict(f) for f in failures],
        "page_errors": page_errors,
        "network_failures": network_failures,
        "all_requests_count": len(all_requests),
    }
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Smoke crawl summary: {report['passed']}/{len(visits)} passed")
    if failures:
        print("\nFailures:")
        for f in failures[:100]:
            print(f"- {f.category}: {f.where}: {f.detail}")
    print(f"\nJSON report written to {out_path}")
    print(f"Screenshots saved to {out_dir}")

    # non-zero if any failures
    return 1 if failures else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--out", default="ui_smoke_report.json")
    ap.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--timeout-ms", type=int, default=20000)
    args = ap.parse_args(argv)

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run(args.base_url, out_path, headless=args.headless, timeout_ms=args.timeout_ms))


if __name__ == "__main__":
    raise SystemExit(main())
