# scanners/common.py
import sqlite3
from pathlib import Path
from typing import Tuple

# Always resolve to the same DB as db.py — two levels up from this file
DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")
TABLE = "options"

COLS = {
    "symbol": "symbol",
    "expiry":  "expiration",
    "strike":  "strike",
    "type":    "type",
    "oi":      "oi",
    "asof":    "date",
}

def get_conn():
    return sqlite3.connect(DB_PATH)

def get_latest_two_asof(conn) -> Tuple[str, str]:
    q = f"SELECT DISTINCT {COLS['asof']} FROM {TABLE} ORDER BY {COLS['asof']} DESC LIMIT 2"
    rows = conn.execute(q).fetchall()
    if len(rows) < 2:
        raise RuntimeError("Need at least two daily snapshots in the DB. Run the scheduler first.")
    return rows[0][0], rows[1][0]
