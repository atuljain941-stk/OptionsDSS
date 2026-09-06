"""
One-time cleanup: remove price_cache rows mislabeled with a weekend date.

Root cause (now fixed in oiapp/services/scheduler.py and
oiapp/scanners/watchlist_manager.py): two of the three price_cache write
paths stamped fetched OHLCV data with datetime.date.today() instead of
the data's own actual trading date. Running either job on a Saturday or
Sunday stored the last real trading day's data (e.g. Friday's close)
under a weekend date key -- a real, distinct row that any classifier
reading price_cache chronologically would see as if a live trading
session happened that weekend, with the volume/price-change math it
implies.

This script only DELETES rows dated on a weekend -- it doesn't try to
guess what the correct date should have been and re-insert under that
key. If the underlying real trading day's row already exists separately
(the common case, since it was almost certainly also stored correctly on
the actual trading day), nothing is lost by removing the weekend
duplicate. If it doesn't, that day's data is simply gone until the next
scheduled fetch naturally refreshes recent history.

Usage:
    python cleanup_weekend_price_cache.py            # dry run, shows what would be deleted
    python cleanup_weekend_price_cache.py --apply     # actually deletes
"""
import argparse
import datetime
import sqlite3
import sys

DB_PATH = r"T:\ajain33\data\options_data.db"  # matches this app's known production DB path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually delete rows (default is dry-run)")
    parser.add_argument("--db", default=DB_PATH, help="Path to options_data.db")
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    try:
        rows = cur.execute("SELECT symbol, date, volume FROM price_cache").fetchall()
    except sqlite3.OperationalError as e:
        print(f"Could not read price_cache: {e}")
        sys.exit(1)

    weekend_rows = []
    for symbol, date_str, volume in rows:
        try:
            d = datetime.date.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue  # malformed date string -- leave it, not what this script targets
        if d.weekday() >= 5:  # 5=Saturday, 6=Sunday
            weekend_rows.append((symbol, date_str, volume))

    print(f"Scanned {len(rows)} price_cache rows, found {len(weekend_rows)} dated on a weekend.")
    if weekend_rows:
        by_date = {}
        for symbol, date_str, volume in weekend_rows:
            by_date.setdefault(date_str, []).append(symbol)
        print("\nBy date:")
        for date_str in sorted(by_date):
            syms = by_date[date_str]
            preview = ", ".join(syms[:10]) + (f" ... (+{len(syms)-10} more)" if len(syms) > 10 else "")
            print(f"  {date_str}: {len(syms)} symbols -- {preview}")

    if not args.apply:
        print(f"\nDry run only -- no rows deleted. Re-run with --apply to actually remove these {len(weekend_rows)} rows.")
        con.close()
        return

    cur.executemany("DELETE FROM price_cache WHERE symbol=? AND date=?",
                     [(s, d) for s, d, _ in weekend_rows])
    con.commit()
    print(f"\nDeleted {len(weekend_rows)} weekend-dated rows from price_cache.")
    con.close()


if __name__ == "__main__":
    main()
