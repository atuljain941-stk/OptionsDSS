"""Point-in-time option-IV context shared by watchlist scanners.

The options table stores chain snapshots rather than a canonical daily ATM IV
series. This module therefore uses the daily mean available chain IV as a
clearly labelled proxy. It never reads a snapshot later than the as_of date.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date
from typing import Any, Dict, Optional

from ..config import DB_PATH


def _num(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def option_iv_context(symbol: str, spot: Optional[float],
                      dte: int = 30, as_of: Optional[date] = None) -> Dict[str, Any]:
    """Return IV Rank, IV trend and expected move using only known snapshots.

    iv_change_5obs is expressed in IV percentage points across the last five
    stored daily observations, not necessarily five calendar days.
    """
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        query = (
            "SELECT date, AVG(iv) AS iv FROM options "
            "WHERE symbol=? AND iv IS NOT NULL AND iv>0"
        )
        values = [symbol.upper()]
        if as_of is not None:
            query += " AND date<=?"
            values.append(as_of.isoformat())
        rows = con.execute(query + " GROUP BY date ORDER BY date", values).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()

    history = []
    for row in rows:
        iv = _num(row["iv"])
        if iv is not None:
            history.append((str(row["date"])[:10], iv))
    if not history:
        return {
            "status": "unavailable", "rank": None, "current_iv": None,
            "observations": 0, "expected_move": None, "expected_move_pct": None,
            "iv_change_5obs": None, "reason": "No historical option IV snapshots",
        }

    observed_at, current = history[-1]
    values_only = [iv for _, iv in history]
    low, high = min(values_only), max(values_only)
    rank = 50.0 if high == low else round((current - low) / (high - low) * 100.0, 1)
    prior = history[-6][1] if len(history) >= 6 else None
    change = round((current - prior) * 100.0, 1) if prior is not None else None
    dte = max(1, min(365, int(dte or 30)))
    spot_value = _num(spot)
    expected_move = (
        round(spot_value * current * math.sqrt(dte / 365.0), 2)
        if spot_value is not None and spot_value > 0 else None
    )
    expected_move_pct = round(current * math.sqrt(dte / 365.0) * 100.0, 2)
    if rank <= 33:
        strategy_context = "low IV: premium buying/debit structures are relatively favored"
    elif rank >= 67:
        strategy_context = "high IV: defined-risk premium selling deserves review"
    else:
        strategy_context = "mid IV: use defined-risk directional structure and target discipline"
    trend_context = (
        "IV falling across five stored observations" if change is not None and change <= -2 else
        "IV rising across five stored observations" if change is not None and change >= 2 else
        "IV broadly stable or insufficient IV history"
    )
    return {
        "status": "ok", "rank": rank, "current_iv": round(current * 100.0, 1),
        "low_iv": round(low * 100.0, 1), "high_iv": round(high * 100.0, 1),
        "observations": len(history), "as_of": observed_at,
        "iv_change_5obs": change, "expected_move": expected_move,
        "expected_move_pct": expected_move_pct, "dte": dte,
        "strategy_context": strategy_context, "trend_context": trend_context,
        "reason": f"IV Rank {rank:.1f}; {trend_context}",
    }
