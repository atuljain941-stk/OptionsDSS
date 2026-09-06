"""
oi_utils.py – Open Interest weighting helpers
-------------------------------------------------

This module provides helper functions to compute the relative
open‑interest (OI) weight for a given symbol.  Many of the
scoring routines within the application rely on OI to gauge
conviction.  However, when a stock trades very little options
volume the raw OI readings can be misleading.  To address this
the `compute_oi_weight` function scales the recent average OI
against a configurable threshold.  Highly liquid names (with
large open interest) approach a weight of 1.0, while illiquid
names approach 0.0.  Downstream scoring functions can use this
weight to reduce the impact of OI for thinly traded symbols and
emphasise it for liquid names.

The `options_data.db` schema is assumed to contain a table
`options` with columns at least including:

  - symbol: ticker symbol (string)
  - date:   date of the snapshot (YYYY‑MM‑DD)
  - expiration: expiration date (YYYY‑MM‑DD)
  - type:  option type ('call' or 'put')
  - oi:    open interest (integer)

This helper connects to the same database used by other
scanners.  If no OI data is available the function returns 0.0.

Example:

    from oiapp.services.oi_utils import compute_oi_weight
    w = compute_oi_weight("AAPL")
    # Use w to downweight OI scores in conviction or ranking.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from functools import lru_cache

__all__ = ["compute_oi_weight"]

from .config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH


def _conn() -> sqlite3.Connection:
    """Return a connection to the options database with WAL enabled."""
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@lru_cache(maxsize=1024)
def compute_oi_weight(symbol: str, days: int = 45, threshold: float = 200_000.0) -> float:
    """Compute a relative open‑interest weight for a given symbol.

    The weight is calculated by averaging the total call + put open
    interest over the last ``days`` trading days and scaling that
    average by ``threshold``.  The result is clamped to the range
    ``[0.0, 1.0]``.  A larger threshold reduces the weight for the
    same average OI.

    Args:
        symbol:     The ticker symbol to compute the weight for.
        days:       The look‑back window in calendar days (default 45).
        threshold:  The open‑interest threshold corresponding to
                    weight == 1.0 (default 200_000 contracts).  Adjust
                    this based on typical OI ranges in your universe.

    Returns:
        A float between 0.0 and 1.0 indicating the relative weight
        that should be applied to OI scores.  If no data is
        available this function returns 0.0.
    """
    if not symbol:
        return 0.0
    sym = symbol.upper().strip()
    # Compute the date cutoff for our look‑back window.
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    try:
        conn = _conn()
        rows = conn.execute(
            """
            SELECT
                SUM(CASE WHEN type='call' THEN oi ELSE 0 END) AS call_oi,
                SUM(CASE WHEN type='put'  THEN oi ELSE 0 END) AS put_oi
            FROM options
            WHERE symbol = ?
              AND date >= ?
              AND expiration >= date
            GROUP BY date
            """,
            (sym, cutoff),
        ).fetchall()
        conn.close()
    except Exception:
        # On any DB error return zero weight; callers should handle this gracefully.
        return 0.0
    if not rows:
        return 0.0
    # Compute the average total OI across the period.
    totals = []
    for r in rows:
        try:
            call_oi = float(r["call_oi"] or 0.0)
            put_oi  = float(r["put_oi"]  or 0.0)
        except Exception:
            # Fallback to positional indices if row factory is default tuple
            call_oi = float(r[0] or 0.0)
            put_oi  = float(r[1] or 0.0)
        totals.append(call_oi + put_oi)
    if not totals:
        return 0.0
    avg_oi = sum(totals) / len(totals)
    # Scale by threshold and clamp into [0,1].  A small epsilon avoids
    # division by zero if threshold is zero.
    eps = 1e-9
    weight = avg_oi / max(threshold, eps)
    if weight < 0.0:
        weight = 0.0
    elif weight > 1.0:
        weight = 1.0
    return round(weight, 4)