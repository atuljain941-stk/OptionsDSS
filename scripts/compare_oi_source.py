"""
scripts/compare_oi_source.py
-----------------------------
Prototype comparing tastytrade vs yfinance as the data source for daily
option-chain open interest + volume (the data oiapp.services.market.
fetch_store_for() currently gets from yfinance).

Run this against your real accounts to get an actual answer on speed and
accuracy -- I can't execute it myself (no live tastytrade/yfinance access
from this environment), so this is built to run standalone and print a
clear comparison report.

Usage:
    python scripts/compare_oi_source.py AAPL

What it does for the given symbol:
  1. Fetches the option chain via tastytrade:
       - Option.get() / get_option_chain() for strikes/expirations (static
         metadata -- fast, one REST call)
       - DXLink Summary events (NOT Greeks -- see note below) for real
         open_interest + prev_day_volume per strike
  2. Fetches the same via your existing yfinance path (market.fetch_store_for
     / tk.option_chain()) for a side-by-side comparison
  3. Times both, and diffs OI values per strike where both sources have data

IMPORTANT BUG FOUND WHILE BUILDING THIS: the existing live-GEX-fallback
code (oiapp/scanners/realtime_dashboard.py -- _fetch_chain_async) reads
open_interest via `getattr(option_instrument, "open_interest", 0)` on the
static Option object, which doesn't have that field at all (confirmed via
Option.model_fields -- it's not there), so it's always silently returned
0. Open interest actually lives on the DXLink `Summary` event, not
`Greeks` (which only has delta/gamma/theta/rho/vega). That existing code
subscribes to Greeks only, never Summary, so real OI was never available
through that fallback path. Worth fixing separately from this prototype.
"""
from __future__ import annotations

import asyncio
import sys
import time


async def fetch_via_tastytrade(symbol: str):
    from oiapp.services.tastytrade_feed import feed as tt_feed
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Summary
    from tastytrade.instruments import get_option_chain

    session = tt_feed.get_session()

    t0 = time.time()
    chain = await get_option_chain(session, symbol)
    t1 = time.time()
    print(f"  [tastytrade] get_option_chain() metadata fetch: {t1 - t0:.2f}s")

    # Flatten all expirations' options into one list for this prototype --
    # a real integration would likely target one expiration at a time,
    # matching fetch_store_for()'s existing per-expiration loop.
    all_options = []
    if hasattr(chain, "items"):
        for exp, options in chain.items():
            all_options.extend(options)
    else:
        for exp in chain:
            all_options.extend(chain[exp])

    streamer_symbols = [o.streamer_symbol for o in all_options]
    print(f"  [tastytrade] {len(all_options)} option contracts across all expirations")

    t2 = time.time()
    summary_map = {}
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Summary, streamer_symbols)
        collected = 0
        try:
            while collected < len(streamer_symbols):
                s = await asyncio.wait_for(streamer.get_event(Summary), timeout=5.0)
                summary_map[s.event_symbol] = s
                collected += 1
        except asyncio.TimeoutError:
            pass  # partial results are still useful for this comparison
    t3 = time.time()
    print(f"  [tastytrade] Summary (OI+volume) stream: {t3 - t2:.2f}s, "
          f"got {len(summary_map)}/{len(streamer_symbols)} contracts")

    rows = {}
    for o in all_options:
        s = summary_map.get(o.streamer_symbol)
        oi = float(s.open_interest) if s and s.open_interest is not None else None
        vol = float(s.prev_day_volume) if s and s.prev_day_volume is not None else None
        rows[(float(o.strike_price), str(o.option_type), str(o.expiration_date))] = {
            "oi": oi, "volume": vol,
        }

    total_time = time.time() - t0
    print(f"  [tastytrade] TOTAL: {total_time:.2f}s for {len(rows)} strikes")
    return rows, total_time


def fetch_via_yfinance(symbol: str):
    import yfinance as yf

    t0 = time.time()
    tk = yf.Ticker(symbol)
    expirations = list(tk.options or [])[:3]  # first 3 expirations, matching fetch_store_for's cap
    print(f"  [yfinance] {len(expirations)} expirations to check: {expirations}")

    rows = {}
    for exp in expirations:
        try:
            oc = tk.option_chain(exp)
        except Exception as e:
            print(f"  [yfinance] {exp}: failed ({type(e).__name__}: {e})")
            continue
        for _, row in oc.calls.iterrows():
            rows[(float(row["strike"]), "C", exp)] = {"oi": float(row.get("openInterest", 0) or 0),
                                                        "volume": float(row.get("volume", 0) or 0)}
        for _, row in oc.puts.iterrows():
            rows[(float(row["strike"]), "P", exp)] = {"oi": float(row.get("openInterest", 0) or 0),
                                                        "volume": float(row.get("volume", 0) or 0)}

    total_time = time.time() - t0
    print(f"  [yfinance] TOTAL: {total_time:.2f}s for {len(rows)} strikes")
    return rows, total_time


def compare(tt_rows, yf_rows):
    print("\n=== Comparison ===")
    common_keys = set(tt_rows) & set(yf_rows)
    print(f"Strikes present in both sources: {len(common_keys)} / "
          f"tastytrade={len(tt_rows)}, yfinance={len(yf_rows)}")

    if not common_keys:
        print("No overlapping strikes to compare -- likely an expiration format "
              "mismatch between the two sources (check the printed keys above).")
        return

    diffs = []
    for k in common_keys:
        tt_oi = tt_rows[k]["oi"]
        yf_oi = yf_rows[k]["oi"]
        if tt_oi is not None and yf_oi is not None:
            diffs.append(abs(tt_oi - yf_oi))

    if diffs:
        print(f"OI differences across {len(diffs)} common strikes: "
              f"mean={sum(diffs)/len(diffs):.1f}, max={max(diffs):.1f}")
        print("(some difference is expected -- tastytrade and yfinance's consolidated "
              "tape/OI reporting can lag each other by up to a day)")


async def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/compare_oi_source.py SYMBOL")
        sys.exit(1)
    symbol = sys.argv[1].upper()

    print(f"=== Fetching {symbol} via tastytrade ===")
    tt_rows, tt_time = await fetch_via_tastytrade(symbol)

    print(f"\n=== Fetching {symbol} via yfinance ===")
    yf_rows, yf_time = fetch_via_yfinance(symbol)

    compare(tt_rows, yf_rows)

    print(f"\n=== Speed summary ===")
    print(f"tastytrade: {tt_time:.2f}s")
    print(f"yfinance:   {yf_time:.2f}s")
    faster = "tastytrade" if tt_time < yf_time else "yfinance"
    print(f"-> {faster} was faster for this symbol")


if __name__ == "__main__":
    asyncio.run(main())
