# scanners/oi_wall_map.py
"""
OI Wall Map Scanner  (NEW)
───────────────────────────
Builds a full support/resistance map for a symbol across ALL expirations.

Use case: before placing a vertical or condor, see where the major OI
walls are stacked across the chain — like a heatmap in table form.

Returns (per symbol):
  - Top N put walls (support levels) with total OI and DTE breakdown
  - Top N call walls (resistance levels)
  - "Pinch zone" (where put wall and call wall are closest together → IC target)
  - Aggregate strike OI across all expirations (gamma wall equivalent)
"""

import sqlite3
from datetime import datetime, date

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH
TABLE   = "options"

def _dte(expiry_str):
    try:
        return (datetime.strptime(expiry_str, "%Y-%m-%d").date() - date.today()).days
    except Exception:
        return 999


def get_oi_wall_map(symbol: str, top_n: int = 10, max_dte: int = 60):
    """
    Returns dict with:
      - put_walls: [{strike, total_oi, expiry_breakdown}]
      - call_walls: [{strike, total_oi, expiry_breakdown}]
      - pinch_zone: {put_strike, call_strike, width, combined_oi}
      - gamma_wall: strike with highest combined put+call OI across all expiries
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Get latest snapshot date for this symbol
    c.execute(f"SELECT MAX(date) FROM {TABLE} WHERE symbol=?", (symbol,))
    last_date = c.fetchone()[0]
    if not last_date:
        conn.close()
        return {"error": f"No data for {symbol}"}

    # All OI across expirations, grouped by type + strike + expiry
    c.execute(f"""
        SELECT type, strike, expiration, SUM(oi) as oi
        FROM {TABLE}
        WHERE symbol=? AND date=?
        GROUP BY type, strike, expiration
        ORDER BY strike
    """, (symbol, last_date))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return {"error": "No data"}

    # Filter by DTE
    puts_by_strike  = {}
    calls_by_strike = {}

    for typ, strike, expiry, oi in rows:
        dte = _dte(expiry)
        if dte > max_dte or dte < 0:
            continue
        sf = float(strike)
        entry = {"expiry": expiry, "dte": dte, "oi": int(oi)}
        if typ.upper().startswith("P"):
            puts_by_strike.setdefault(sf, []).append(entry)
        else:
            calls_by_strike.setdefault(sf, []).append(entry)

    def _aggregate(by_strike):
        result = []
        for strike, entries in by_strike.items():
            total_oi = sum(e["oi"] for e in entries)
            result.append({
                "strike": strike,
                "total_oi": total_oi,
                "expiry_breakdown": sorted(entries, key=lambda e: e["expiry"]),
            })
        result.sort(key=lambda x: -x["total_oi"])
        return result[:top_n]

    put_walls  = _aggregate(puts_by_strike)
    call_walls = _aggregate(calls_by_strike)

    # Gamma wall: highest combined OI strike
    combined = {}
    for sf, entries in puts_by_strike.items():
        combined[sf] = combined.get(sf, 0) + sum(e["oi"] for e in entries)
    for sf, entries in calls_by_strike.items():
        combined[sf] = combined.get(sf, 0) + sum(e["oi"] for e in entries)
    gamma_wall = max(combined, key=combined.get) if combined else None

    # Pinch zone: find the pair (put_wall_below_spot, call_wall_above_spot) with
    # smallest gap and largest combined OI
    from ._spot_cache import get_spot as _get_spot_cached
    spot = _get_spot_cached(symbol)

    pinch_zone = None
    if spot and put_walls and call_walls:
        puts_below  = [w for w in put_walls  if w["strike"] <= spot]
        calls_above = [w for w in call_walls if w["strike"] >= spot]
        if puts_below and calls_above:
            # find combo with smallest gap and high OI
            best = None
            best_score = -1
            for pw in puts_below[:5]:
                for cw in calls_above[:5]:
                    gap = cw["strike"] - pw["strike"]
                    combo_oi = pw["total_oi"] + cw["total_oi"]
                    # score: penalize wide gap, reward high OI
                    score = combo_oi / max(gap, 1)
                    if score > best_score:
                        best_score = score
                        best = {
                            "put_strike":   pw["strike"],
                            "put_oi":       pw["total_oi"],
                            "call_strike":  cw["strike"],
                            "call_oi":      cw["total_oi"],
                            "width":        round(gap, 2),
                            "combined_oi":  combo_oi,
                        }
            pinch_zone = best

    return {
        "symbol":      symbol,
        "snapshot":    last_date,
        "spot":        round(spot, 2) if spot else None,
        "put_walls":   put_walls,
        "call_walls":  call_walls,
        "gamma_wall":  gamma_wall,
        "pinch_zone":  pinch_zone,
    }


if __name__ == "__main__":
    import json, sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    print(json.dumps(get_oi_wall_map(sym), indent=2))
