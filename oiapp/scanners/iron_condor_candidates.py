# scanners/iron_condor_candidates.py
"""
Enhanced Iron Condor & Vertical Scanner
─────────────────────────────────────────
Iron Condor scanner improvements:
  • Requires put wall BELOW spot, call wall ABOVE spot (not just highest)
  • IVP proxy via 30-day HV ratio (from yfinance) — free, no extra API
  • Wing-width suggestion based on 1-SD expected move
  • Expected-range score: tight OI walls = better condor
  • DTE tagging and weekly/monthly preference

Vertical scanner improvements:
  • Grades each setup: A / B / C based on OI wall strength + DTE + PCR
  • Suggests specific strikes for bull-put spread and bear-call spread
  • Shows strike distance from spot as % (easy entry/risk assessment)
  • Support/Resistance strength: ratio of wall OI to next-highest strike OI
"""

import sqlite3
import math
from datetime import datetime, date

DB_PATH = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "options_data.db")
TABLE   = "options"

# ─── helpers ─────────────────────────────────────────────────────────────────

def _dte(expiry_str: str) -> int:
    try:
        return (datetime.strptime(expiry_str, "%Y-%m-%d").date() - date.today()).days
    except Exception:
        return 999


def _expiry_type(dte: int) -> str:
    if dte <= 0:   return "0DTE"
    if dte <= 7:   return "Weekly"
    if dte <= 35:  return "Monthly"
    return "LEAPS"


def get_spot(symbol: str):
    try:
        import requests
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="1d")
        if not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception:
        pass
    return None


def _get_iv_metrics(symbol: str):
    """
    IVP proxy using 30-day historical vs 252-day historical volatility.
    Returns: (iv_30d_annualized, hv_252d_annualized, iv_rank_proxy 0-100)
    All from yfinance — completely free.
    """
    try:
        import yfinance as yf
        import numpy as np
        df = yf.Ticker(symbol).history(period="1y")
        if len(df) < 30:
            return None, None, None
        closes = df["Close"].values
        log_returns = np.log(closes[1:] / closes[:-1])
        hv_30  = float(np.std(log_returns[-30:])  * math.sqrt(252) * 100)
        hv_252 = float(np.std(log_returns)         * math.sqrt(252) * 100)
        # IVP proxy: where does current 30d HV sit in the past year's range?
        # (Real IV unavailable for free; HV ratio is a decent proxy)
        rolling_30d_hvs = [
            float(np.std(log_returns[max(0,i-30):i]) * math.sqrt(252) * 100)
            for i in range(30, len(log_returns))
        ]
        if not rolling_30d_hvs:
            return hv_30, hv_252, None
        low_hv  = min(rolling_30d_hvs)
        high_hv = max(rolling_30d_hvs)
        ivp = round(100 * (hv_30 - low_hv) / (high_hv - low_hv), 1) if high_hv > low_hv else 50
        return round(hv_30, 1), round(hv_252, 1), ivp
    except Exception:
        return None, None, None


def _expected_move(spot: float, hv_30: float | None, dte: int) -> float | None:
    """1-SD expected move in dollars."""
    if spot and hv_30 and dte > 0:
        return round(spot * (hv_30 / 100) * math.sqrt(dte / 252), 2)
    return None


def _wall_strength_ratio(sorted_oi_list: list) -> float:
    """Ratio of top OI strike to 2nd-highest — higher = stronger wall."""
    if len(sorted_oi_list) < 2:
        return 1.0
    return round(sorted_oi_list[-1][1] / max(sorted_oi_list[-2][1], 1), 2)


def _grade(score: int) -> str:
    if score >= 75: return "A"
    if score >= 50: return "B"
    return "C"


def _get_pcr(puts_oi: int, calls_oi: int) -> float | None:
    if calls_oi == 0:
        return None
    return round(puts_oi / calls_oi, 3)


# ─── Iron Condor Scanner ──────────────────────────────────────────────────────

def find_condor_candidates(selected_expiry):
    """
    Enhanced condor scan:
    - Requires put wall BELOW spot, call wall ABOVE spot
    - Adds IVP proxy, expected move, wing suggestions, setup grade
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(f"SELECT DISTINCT symbol FROM {TABLE} WHERE expiration=?", (selected_expiry,))
    symbols = [r[0] for r in c.fetchall()]
    if not symbols:
        conn.close()
        return []

    dte      = _dte(selected_expiry)
    exp_type = _expiry_type(dte)
    results  = []
    iv_cache = {}

    for sym in symbols:
        c.execute(f"SELECT MAX(date) FROM {TABLE} WHERE symbol=? AND expiration=?", (sym, selected_expiry))
        last_date = c.fetchone()[0]
        if not last_date:
            continue

        c.execute(f"""
            SELECT type, strike, SUM(oi) as total_oi
            FROM {TABLE}
            WHERE symbol=? AND expiration=? AND date=?
            GROUP BY type, strike
            ORDER BY strike
        """, (sym, selected_expiry, last_date))
        rows = c.fetchall()
        if not rows:
            continue

        puts  = sorted([(float(s), int(oi)) for t, s, oi in rows if t.upper().startswith("P")], key=lambda x: x[0])
        calls = sorted([(float(s), int(oi)) for t, s, oi in rows if t.upper().startswith("C")], key=lambda x: x[0])
        if not puts or not calls:
            continue

        spot = get_spot(sym)
        if spot is None:
            spot = round((puts[-1][0] + calls[0][0]) / 2, 2)

        # ── find highest-OI PUT below spot, highest-OI CALL above spot ──
        puts_below  = [(s, oi) for s, oi in puts  if s <= spot]
        calls_above = [(s, oi) for s, oi in calls if s >= spot]
        if not puts_below or not calls_above:
            continue

        put_wall  = max(puts_below,  key=lambda x: x[1])
        call_wall = max(calls_above, key=lambda x: x[1])
        pw_strike, pw_oi   = put_wall
        cw_strike, cw_oi   = call_wall
        range_width = round(cw_strike - pw_strike, 2)

        # OI near spot (within 2 strikes each side)
        try:
            strike_interval = abs(calls[1][0] - calls[0][0])
        except Exception:
            strike_interval = 1.0
        nearby_puts  = [oi for s, oi in puts  if abs(s - spot) <= 2 * strike_interval]
        nearby_calls = [oi for s, oi in calls if abs(s - spot) <= 2 * strike_interval]
        total_oi     = sum(nearby_puts) + sum(nearby_calls)
        put_sum      = sum(nearby_puts)
        call_sum     = sum(nearby_calls)
        symmetry     = round(min(put_sum, call_sum) / max(put_sum, call_sum), 2) if (put_sum and call_sum) else 0

        if total_oi < 2000 or symmetry < 0.6:
            continue

        # IV metrics
        if sym not in iv_cache:
            iv_cache[sym] = _get_iv_metrics(sym)
        hv_30, hv_252, ivp = iv_cache[sym]

        exp_move = _expected_move(spot, hv_30, dte)

        # Suggest wings: short strikes at OI walls, long strikes 1 interval beyond
        suggested_put_short  = pw_strike
        suggested_put_long   = round(pw_strike - strike_interval, 2)
        suggested_call_short = cw_strike
        suggested_call_long  = round(cw_strike + strike_interval, 2)

        # Percentage distance of walls from spot
        put_dist_pct  = round(100 * abs(spot - pw_strike) / spot, 2)
        call_dist_pct = round(100 * abs(cw_strike - spot) / spot, 2)

        # Wall strength ratios
        puts_sorted   = sorted(puts_below,  key=lambda x: x[1])
        calls_sorted  = sorted(calls_above, key=lambda x: x[1])
        pw_strength   = _wall_strength_ratio(puts_sorted)
        cw_strength   = _wall_strength_ratio(calls_sorted)

        pcr = _get_pcr(put_sum, call_sum)

        # ── signal score ─────────────────────────────────────────────────
        score_sym   = min(20, int(symmetry * 20))
        score_oi    = min(20, int(total_oi / 500))
        score_dte   = 20 if 7 <= dte <= 21 else (15 if dte <= 35 else 8)
        score_ivp   = min(20, int((ivp or 50) / 5)) if ivp else 10
        score_walls = min(20, int((pw_strength + cw_strength) * 5))
        signal_score = min(100, score_sym + score_oi + score_dte + score_ivp + score_walls)

        results.append({
            "symbol":               sym,
            "expiration":           selected_expiry,
            "expiry_type":          exp_type,
            "dte":                  dte,
            "spot":                 round(spot, 2),
            "put_wall_strike":      pw_strike,
            "put_wall_oi":          pw_oi,
            "put_dist_pct":         put_dist_pct,
            "call_wall_strike":     cw_strike,
            "call_wall_oi":         cw_oi,
            "call_dist_pct":        call_dist_pct,
            "range_width":          range_width,
            "symmetry":             symmetry,
            "total_oi_near_spot":   total_oi,
            "put_wall_strength":    pw_strength,
            "call_wall_strength":   cw_strength,
            "pcr_near_spot":        pcr,
            "hv_30d":               hv_30,
            "ivp_proxy":            ivp,
            "expected_move_1sd":    exp_move,
            "suggested_spread":     (
                f"Sell {suggested_put_short}P / Buy {suggested_put_long}P  |  "
                f"Sell {suggested_call_short}C / Buy {suggested_call_long}C"
            ),
            "signal_score":         signal_score,
            "grade":                _grade(signal_score),
            "rationale": (
                f"OI walls: Put {pw_strike} ({pw_oi:,}) / Call {cw_strike} ({cw_oi:,}). "
                f"Range {range_width} pts. Symmetry {symmetry}. "
                + (f"IVP proxy {ivp}%. " if ivp else "")
                + (f"1SD move ≈ ${exp_move}." if exp_move else "")
            ),
        })

    conn.close()
    results.sort(key=lambda x: (-x["signal_score"], x["dte"]))
    return results


# ─── Vertical (Bull-Put / Bear-Call) Scanner ──────────────────────────────────

def find_verticals(selected_expiry, strike_distance=2):
    """
    Enhanced vertical scanner:
    - Grades A/B/C
    - Shows wall strength ratio (wall OI vs 2nd-highest)
    - Shows PCR for directional bias
    - Suggests exact spread legs
    - IVP proxy included
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(f"SELECT DISTINCT symbol FROM {TABLE} WHERE expiration=?", (selected_expiry,))
    symbols = [r[0] for r in c.fetchall()]
    if not symbols:
        conn.close()
        return []

    dte      = _dte(selected_expiry)
    exp_type = _expiry_type(dte)
    results  = []
    iv_cache = {}

    for sym in symbols:
        c.execute(f"SELECT MAX(date) FROM {TABLE} WHERE symbol=? AND expiration=?", (sym, selected_expiry))
        last_date = c.fetchone()[0]
        if not last_date:
            continue

        c.execute(f"""
            SELECT type, strike, SUM(oi) as total_oi
            FROM {TABLE}
            WHERE symbol=? AND expiration=? AND date=?
            GROUP BY type, strike
            ORDER BY strike
        """, (sym, selected_expiry, last_date))
        rows = c.fetchall()
        if not rows:
            continue

        puts  = sorted([(float(s), int(oi)) for t, s, oi in rows if t.upper().startswith("P")], key=lambda x: x[0])
        calls = sorted([(float(s), int(oi)) for t, s, oi in rows if t.upper().startswith("C")], key=lambda x: x[0])
        if not puts or not calls:
            continue

        spot = get_spot(sym)
        if spot is None:
            spot = round((puts[-1][0] + calls[0][0]) / 2, 2)

        try:
            strike_interval = abs(calls[1][0] - calls[0][0]) if len(calls) >= 2 else 1.0
        except Exception:
            strike_interval = 1.0

        # IV metrics
        if sym not in iv_cache:
            iv_cache[sym] = _get_iv_metrics(sym)
        hv_30, hv_252, ivp = iv_cache[sym]
        exp_move = _expected_move(spot, hv_30, dte)

        # full-chain PCR
        total_put_oi  = sum(oi for _, oi in puts)
        total_call_oi = sum(oi for _, oi in calls)
        pcr_full = _get_pcr(total_put_oi, total_call_oi)

        # ── Put Support analysis ─────────────────────────────────────────
        puts_below = [(s, oi) for s, oi in puts if s <= spot]
        if puts_below:
            top_put = max(puts_below, key=lambda x: x[1])
            pw_str, pw_oi = top_put
            pw_near = abs(spot - pw_str) <= strike_distance * strike_interval

            if pw_near and pw_oi > 1000:
                puts_sorted  = sorted(puts_below, key=lambda x: x[1])
                wall_strength = _wall_strength_ratio(puts_sorted)
                dist_pct      = round(100 * abs(spot - pw_str) / spot, 2)
                # suggest: sell the wall strike, buy 1 interval lower
                long_leg  = round(pw_str - strike_interval, 2)

                score_oi   = min(30, int(pw_oi / 333))
                score_dte  = 20 if 5 <= dte <= 21 else (15 if dte <= 35 else 8)
                score_wall = min(20, int(wall_strength * 10))
                score_ivp  = min(15, int((ivp or 50) / 7)) if ivp else 7
                score_pcr  = 10 if (pcr_full and pcr_full < 0.8) else (5 if pcr_full and pcr_full < 1.1 else 0)
                signal_score = min(100, score_oi + score_dte + score_wall + score_ivp + score_pcr)

                results.append({
                    "strategy":           "Bull Put Spread",
                    "symbol":             sym,
                    "expiration":         selected_expiry,
                    "expiry_type":        exp_type,
                    "dte":                dte,
                    "spot":               round(spot, 2),
                    "sell_strike":        pw_str,
                    "buy_strike":         long_leg,
                    "wall_oi":            pw_oi,
                    "wall_strength":      wall_strength,
                    "strike_dist_pct":    dist_pct,
                    "full_chain_pcr":     pcr_full,
                    "hv_30d":             hv_30,
                    "ivp_proxy":          ivp,
                    "expected_move_1sd":  exp_move,
                    "signal_score":       signal_score,
                    "grade":              _grade(signal_score),
                    "bias":               "Bullish – put wall as support",
                    "suggested_trade":    f"Sell {pw_str}P / Buy {long_leg}P  exp {selected_expiry}",
                    "rationale": (
                        f"Put OI wall at {pw_str} ({pw_oi:,} OI, strength {wall_strength}x). "
                        f"Spot {spot} is {dist_pct}% above wall. "
                        f"PCR {pcr_full}."
                        + (f" IVP proxy {ivp}%." if ivp else "")
                    ),
                })

        # ── Call Resistance analysis ─────────────────────────────────────
        calls_above = [(s, oi) for s, oi in calls if s >= spot]
        if calls_above:
            top_call = max(calls_above, key=lambda x: x[1])
            cw_str, cw_oi = top_call
            cw_near = abs(spot - cw_str) <= strike_distance * strike_interval

            if cw_near and cw_oi > 1000:
                calls_sorted  = sorted(calls_above, key=lambda x: x[1])
                wall_strength  = _wall_strength_ratio(calls_sorted)
                dist_pct       = round(100 * abs(cw_str - spot) / spot, 2)
                long_leg       = round(cw_str + strike_interval, 2)

                score_oi   = min(30, int(cw_oi / 333))
                score_dte  = 20 if 5 <= dte <= 21 else (15 if dte <= 35 else 8)
                score_wall = min(20, int(wall_strength * 10))
                score_ivp  = min(15, int((ivp or 50) / 7)) if ivp else 7
                score_pcr  = 10 if (pcr_full and pcr_full > 1.2) else (5 if pcr_full and pcr_full > 0.9 else 0)
                signal_score = min(100, score_oi + score_dte + score_wall + score_ivp + score_pcr)

                results.append({
                    "strategy":           "Bear Call Spread",
                    "symbol":             sym,
                    "expiration":         selected_expiry,
                    "expiry_type":        exp_type,
                    "dte":                dte,
                    "spot":               round(spot, 2),
                    "sell_strike":        cw_str,
                    "buy_strike":         long_leg,
                    "wall_oi":            cw_oi,
                    "wall_strength":      wall_strength,
                    "strike_dist_pct":    dist_pct,
                    "full_chain_pcr":     pcr_full,
                    "hv_30d":             hv_30,
                    "ivp_proxy":          ivp,
                    "expected_move_1sd":  exp_move,
                    "signal_score":       signal_score,
                    "grade":              _grade(signal_score),
                    "bias":               "Bearish – call wall as resistance",
                    "suggested_trade":    f"Sell {cw_str}C / Buy {long_leg}C  exp {selected_expiry}",
                    "rationale": (
                        f"Call OI wall at {cw_str} ({cw_oi:,} OI, strength {wall_strength}x). "
                        f"Spot {spot} is {dist_pct}% below wall. "
                        f"PCR {pcr_full}."
                        + (f" IVP proxy {ivp}%." if ivp else "")
                    ),
                })

    conn.close()
    results.sort(key=lambda x: (-x["signal_score"], x["dte"]))
    return results
