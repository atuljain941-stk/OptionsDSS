"""
smart_money_offline_scan.py

Offline/backtest runner for the pandas versions of the accumulation/
distribution scanners (smart_money_backtest.py). Unlike the live Flask
blueprints (oiapp/scanners/institutional_scanner.py and
smart_money_distribution_scanner.py, which fetch live from yfinance),
this reads directly from oiapp's price_cache table -- confirmed schema:

    price_cache(symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
                close REAL, volume INTEGER, PRIMARY KEY(symbol, date))

Use this for:
  - Backtesting udvr_threshold / dist_day_count defaults against your own
    watchlist history before trusting live thresholds.
  - A fast local scan without hitting yfinance rate limits.

Usage:
    python scripts/smart_money_offline_scan.py accumulation
    python scripts/smart_money_offline_scan.py distribution
    python scripts/smart_money_offline_scan.py accumulation --backtest   # full history, not just latest day
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from oiapp.config import DB_PATH  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smart_money_backtest import run_watchlist_scan  # noqa: E402
from smart_money_watchlist import WATCHLIST, BENCHMARK_SYMBOL, FULL_PULL_LIST  # noqa: E402


def load_price_data(db_path, symbols, lookback_days=400) -> dict:
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    placeholders = ",".join("?" for _ in symbols)
    query = f"""
        SELECT symbol, date, open, high, low, close, volume
        FROM price_cache
        WHERE symbol IN ({placeholders}) AND date >= ?
        ORDER BY symbol, date ASC
    """
    conn = sqlite3.connect(str(db_path))
    try:
        raw = pd.read_sql_query(query, conn, params=[*symbols, cutoff])
    finally:
        conn.close()

    if raw.empty:
        print(f"[smart_money_offline_scan] No rows in price_cache for requested "
              f"symbols/window. Run a backfill first (see scanner_builder.py's "
              f"_backfill_price_history_to_cache), or check {db_path} directly.")
        return {}

    raw["date"] = pd.to_datetime(raw["date"])
    return {
        sym: g.set_index("date")[["open", "high", "low", "close", "volume"]]
        for sym, g in raw.groupby("symbol")
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["accumulation", "distribution"])
    parser.add_argument("--lookback-days", type=int, default=400)
    parser.add_argument("--backtest", action="store_true",
                         help="Return full historical scan series per symbol instead of just the latest day")
    args = parser.parse_args()

    print(f"[smart_money_offline_scan] Reading price_cache from {DB_PATH}")
    price_data = load_price_data(DB_PATH, FULL_PULL_LIST, lookback_days=args.lookback_days)
    if not price_data:
        sys.exit(1)

    missing = set(WATCHLIST) - set(price_data.keys())
    if missing:
        print(f"[smart_money_offline_scan] {len(missing)} symbols had no cached "
              f"price history (run a backfill for these first): {sorted(missing)}")

    results = run_watchlist_scan(
        price_data,
        benchmark_symbol=BENCHMARK_SYMBOL,
        mode=args.mode,
        latest_only=not args.backtest,
    )

    if results.empty:
        print(f"No {args.mode} hits.")
        return

    if args.backtest:
        hit_rate = results.groupby("symbol")["scan"].mean().sort_values(ascending=False)
        print(f"\n{args.mode.upper()} historical hit rate by symbol (top 20):\n")
        print(hit_rate.head(20).to_string())
    else:
        display_cols = ["symbol", "udvr", "dist_day_count", "avg_vol50"]
        if args.mode == "accumulation":
            display_cols += ["pct_below_high", "pocket_pivot", "rs_near_high"]
        else:
            display_cols += ["churn_count", "rs_rolling_over"]
        display_cols = [c for c in display_cols if c in results.columns]
        print(f"\n{args.mode.upper()} hits today ({len(results)}):\n")
        print(results[display_cols].to_string(index=False))


if __name__ == "__main__":
    main()
