# scanners/weekly_pin.py
"""
Enhanced Weekly Pin Scanner
────────────────────────────
Finds symbols likely to pin near high-OI strikes into weekly expiry.

New features vs v1:
  • Works even when "undl" column doesn't exist (uses yfinance for spot)
  • Identifies the specific pin strike (not just OI totals)
  • Computes distance from spot to pin strike (% and $)
  • IVP proxy and expected move — are we inside or outside the move?
  • Signal score and grade
  • Also scans for monthly pin (next monthly expiry)
"""

import sqlite3
import math
from datetime import datetime, date, timedelta

DB_PATH = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "options_data.db")
TABLE   = "options"


from ._spot_cache import get_spot as _get_spot


def _hv_30(symbol: str):
    try:
        import yfinance as yf
        import numpy as np
        df = yf.Ticker(symbol).history(period="60d")
        if len(df) >= 5:
            log_r = np.log(df["Close"].values[1:] / df["Close"].values[:-1])
            return float(np.std(log_r[-30:]) * math.sqrt(252) * 100)
    except Exception:
        pass
    return None


def _get_friday(today=None):
    today = today or date.today()
    delta = (4 - today.weekday()) % 7
    return (today + timedelta(days=delta)).strftime("%Y-%m-%d")


def _get_next_monthly(today=None):
    """Third Friday of current (or next) month."""
    today = today or date.today()
    year, month = today.year, today.month
    # find 3rd Friday
    for attempt in range(2):
        # first day of month
        d = date(year, month, 1)
        # find first Friday
        first_fri = d + timedelta(days=(4 - d.weekday()) % 7)
        third_fri = first_fri + timedelta(weeks=2)
        if third_fri > today:
            return third_fri.strftime("%Y-%m-%d")
        # move to next month
        if month == 12:
            year += 1; month = 1
        else:
            month += 1
    return None


def _scan_expiry(conn, expiry: str, window_pct: float = 5.0):
    """Scan a single expiry and return pin candidates."""
    c = conn.cursor()
    c.execute(f"SELECT DISTINCT symbol FROM {TABLE} WHERE expiration=?", (expiry,))
    symbols = [r[0] for r in c.fetchall()]
    if not symbols:
        return []

    c.execute(f"SELECT MAX(date) FROM {TABLE} WHERE expiration=?", (expiry,))
    latest = c.fetchone()[0]
    if not latest:
        return []

    dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days

    results = []
    for sym in symbols:
        spot = _get_spot(sym)
        if not spot:
            continue

        lo, hi = spot * (1 - window_pct / 100), spot * (1 + window_pct / 100)

        c.execute(f"""
            SELECT strike, type, SUM(oi) as total_oi
            FROM {TABLE}
            WHERE symbol=? AND expiration=? AND date=?
              AND strike BETWEEN ? AND ?
            GROUP BY strike, type
            ORDER BY strike
        """, (sym, expiry, latest, lo, hi))
        data = c.fetchall()
        if not data:
            continue

        # aggregate by strike (puts + calls combined)
        strike_oi = {}
        for strike, typ, oi in data:
            sf = float(strike)
            strike_oi[sf] = strike_oi.get(sf, {"put_oi": 0, "call_oi": 0})
            if typ.upper().startswith("P"):
                strike_oi[sf]["put_oi"] += oi
            else:
                strike_oi[sf]["call_oi"] += oi

        # find pin strike (highest combined OI)
        pin_strike = max(strike_oi, key=lambda s: strike_oi[s]["put_oi"] + strike_oi[s]["call_oi"])
        pin_data   = strike_oi[pin_strike]
        pin_put_oi  = pin_data["put_oi"]
        pin_call_oi = pin_data["call_oi"]
        total_pin_oi = pin_put_oi + pin_call_oi

        puts_total  = sum(v["put_oi"]  for v in strike_oi.values())
        calls_total = sum(v["call_oi"] for v in strike_oi.values())
        pcr = round(puts_total / calls_total, 3) if calls_total else None

        # distance from spot to pin
        pin_dist  = round(pin_strike - spot, 2)
        pin_dist_pct = round(100 * abs(pin_dist) / spot, 2)

        # expected move
        hv = _hv_30(sym)
        exp_move = round(spot * (hv / 100) * math.sqrt(max(dte, 1) / 252), 2) if hv else None

        # is pin within expected move?
        inside_move = bool(exp_move and abs(pin_dist) <= exp_move)

        # signal score
        score_oi    = min(30, int(total_pin_oi / 500))
        score_prox  = max(0, 25 - int(pin_dist_pct * 5))  # closer = better
        score_dte   = 20 if dte <= 3 else (15 if dte <= 7 else 8)
        score_move  = 15 if inside_move else 0
        signal_score = min(100, score_oi + score_prox + score_dte + score_move)

        results.append({
            "symbol":           sym,
            "expiry":           expiry,
            "dte":              dte,
            "spot":             round(spot, 2),
            "pin_strike":       pin_strike,
            "pin_put_oi":       int(pin_put_oi),
            "pin_call_oi":      int(pin_call_oi),
            "total_pin_oi":     int(total_pin_oi),
            "pin_dist_$":       pin_dist,
            "pin_dist_%":       pin_dist_pct,
            "window_pcr":       pcr,
            "hv_30d":           round(hv, 1) if hv else None,
            "exp_move_1sd":     exp_move,
            "inside_exp_move":  inside_move,
            "signal_score":     signal_score,
            "trade_idea": (
                f"IC: Sell straddle at {pin_strike} if pin holds"
                if pin_dist_pct < 0.5
                else f"CS: {'Put' if pin_dist < 0 else 'Call'} spread toward {pin_strike}"
            ),
        })

    results.sort(key=lambda r: -r["signal_score"])
    return results


def run_weekly_pin_scanner(window_pct=5.0, include_monthly=True):
    conn = sqlite3.connect(DB_PATH)

    weekly_expiry  = _get_friday()
    monthly_expiry = _get_next_monthly()

    weekly  = _scan_expiry(conn, weekly_expiry, window_pct)
    monthly = []
    if include_monthly and monthly_expiry and monthly_expiry != weekly_expiry:
        monthly = _scan_expiry(conn, monthly_expiry, window_pct)

    conn.close()

    # tag each row
    for r in weekly:  r["scan_type"] = "Weekly"
    for r in monthly: r["scan_type"] = "Monthly"

    combined = sorted(weekly + monthly, key=lambda r: -r["signal_score"])
    return combined


if __name__ == "__main__":
    import json
    print(json.dumps(run_weekly_pin_scanner(), indent=2))
