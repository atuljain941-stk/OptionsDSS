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
from typing import Optional
from datetime import datetime, date

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH
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


def _leg_price(c: sqlite3.Cursor, symbol: str, expiry: str, date_str: str, strike: float, option_type: str) -> Optional[float]:
    """Real stored premium for one specific contract, same 'options'
    table this whole scanner already reads OI from -- it has a price
    column that was simply never used here. Needed for actual RR, not
    just "does the range look wide enough" -- a condor's strikes can be
    OI-heavy and mechanically well-formed while paying almost nothing
    relative to the width, which is exactly what a BX-style spot-vs-
    strikes mismatch can produce."""
    type_prefix = "C" if str(option_type).upper().startswith("C") else "P"
    c.execute(
        f"SELECT price FROM {TABLE} WHERE symbol=? AND expiration=? AND date=? AND strike=? AND type LIKE ?",
        (symbol, expiry, date_str, strike, f"{type_prefix}%"),
    )
    row = c.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _spread_rr(credit: Optional[float], width: float) -> Optional[float]:
    """RR = reward / risk = credit / (width - credit) -- the standard
    credit-spread risk/reward, not a probability metric. A condor or
    vertical can have excellent wall-based mechanics and still be a bad
    trade if the width dwarfs the credit collected for it."""
    if credit is None or width <= 0:
        return None
    max_loss = width - credit
    if max_loss <= 0:
        return None  # credit >= width -- pricing data looks wrong, don't report a fake RR
    return round(credit / max_loss, 3)


def _get_pcr(puts_oi: int, calls_oi: int) -> float | None:
    if calls_oi == 0:
        return None
    return round(puts_oi / calls_oi, 3)


# ─── Iron Condor Scanner ──────────────────────────────────────────────────────

def _wall_freshness(symbol: str, expiry: str, strike: float, option_type: str) -> dict:
    """Is this specific wall's OI freshly building, flat, or unwinding --
    reuses _oi_rows()'s already-proven walk-back logic (finds the nearest
    genuinely different prior snapshot, not just yesterday's date label,
    same fix built for Wall Term Structure's OI-change chart) rather than
    reimplementing OI-change from scratch. A wall's raw OI count and
    same-day concentration ratio say nothing about whether that position
    was built last week or accumulated months ago and never revisited --
    exactly the gap that makes "how big is this wall really" unanswerable
    from the strength ratio alone."""
    try:
        from .spy_strategies import _oi_rows
        rows = _oi_rows(symbol, expiry)
        want_type = "call" if str(option_type).upper().startswith("C") else "put"
        for r in rows:
            if r.get("strike") == strike and (r.get("type") or "").lower() == want_type:
                chg = r.get("oi_change")
                chg_pct = r.get("oi_change_pct")
                if chg is None:
                    return {"status": "unknown", "oi_change": None, "oi_change_pct": None}
                if chg > 0 and (chg_pct or 0) >= 5:
                    status = "fresh"
                elif chg < 0 and (chg_pct or 0) <= -5:
                    status = "unwinding"
                else:
                    status = "flat"
                return {"status": status, "oi_change": chg, "oi_change_pct": chg_pct}
    except Exception:
        pass
    return {"status": "unknown", "oi_change": None, "oi_change_pct": None}


def find_condor_candidates(selected_expiry, min_oi_near_spot: int = 2000, min_rr: float = 0.5, min_ivp: float = 30):
    """
    Enhanced condor scan:
    - Requires put wall BELOW spot, call wall ABOVE spot
    - Adds IVP proxy, expected move, wing suggestions, setup grade
    - min_oi_near_spot: liquidity floor (OI within 2 strikes of spot) --
      was previously hardcoded to 2000 with no way to see or adjust it;
      now a real parameter with the same default, so nothing changes
      unless explicitly tightened or loosened.
    - min_rr: minimum credit/(width-credit) computed from real stored
      premiums -- filters out mechanically well-formed but poorly-paid
      setups (wide walls far from spot, small credit relative to risk).
      Candidates with no priceable data pass through unfiltered rather
      than being silently dropped -- RR shows as unavailable, not zero.
    - min_ivp: minimum IVP proxy (this scanner's own _get_iv_metrics,
      NOT true implied volatility -- its own docstring is explicit:
      "Real IV unavailable for free; HV ratio is a decent proxy"). A
      genuine, honestly-labeled 52-week rank of realized volatility, not
      a claim of options-derived IV. Selling premium (condors/verticals)
      is the textbook case for wanting elevated vol, so this filters
      OUT low-proxy candidates by default rather than just displaying
      the number and leaving the judgment call unenforced.
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

        if total_oi < min_oi_near_spot or symmetry < 0.6:
            continue

        # IV metrics
        if sym not in iv_cache:
            iv_cache[sym] = _get_iv_metrics(sym)
        hv_30, hv_252, ivp = iv_cache[sym]
        if ivp is not None and ivp < min_ivp:
            continue

        exp_move = _expected_move(spot, hv_30, dte)

        # Suggest wings: short strikes at OI walls, long strikes 1 interval beyond
        suggested_put_short  = pw_strike
        suggested_put_long   = round(pw_strike - strike_interval, 2)
        suggested_call_short = cw_strike
        suggested_call_long  = round(cw_strike + strike_interval, 2)

        # Real RR from actual stored premiums, not just "the range looks
        # wide enough" -- a condor whose walls sit far from spot (like a
        # 100/175 spread on a $143 stock) can be mechanically well-formed
        # by OI alone while paying almost nothing relative to the width.
        put_short_px = _leg_price(c, sym, selected_expiry, last_date, suggested_put_short, "P")
        put_long_px  = _leg_price(c, sym, selected_expiry, last_date, suggested_put_long, "P")
        call_short_px = _leg_price(c, sym, selected_expiry, last_date, suggested_call_short, "C")
        call_long_px  = _leg_price(c, sym, selected_expiry, last_date, suggested_call_long, "C")
        put_credit = (put_short_px - put_long_px) if (put_short_px is not None and put_long_px is not None) else None
        call_credit = (call_short_px - call_long_px) if (call_short_px is not None and call_long_px is not None) else None
        total_credit = (put_credit or 0) + (call_credit or 0) if (put_credit is not None or call_credit is not None) else None
        condor_width = abs(suggested_put_short - suggested_put_long) + abs(suggested_call_long - suggested_call_short)
        condor_rr = _spread_rr(total_credit, condor_width)
        if condor_rr is not None and condor_rr < min_rr:
            continue

        # Percentage distance of walls from spot
        put_dist_pct  = round(100 * abs(spot - pw_strike) / spot, 2)
        call_dist_pct = round(100 * abs(cw_strike - spot) / spot, 2)

        # Wall strength ratios
        puts_sorted   = sorted(puts_below,  key=lambda x: x[1])
        calls_sorted  = sorted(calls_above, key=lambda x: x[1])
        pw_strength   = _wall_strength_ratio(puts_sorted)
        cw_strength   = _wall_strength_ratio(calls_sorted)

        pcr = _get_pcr(put_sum, call_sum)

        # Wall freshness: how big a wall's raw OI is says nothing about
        # WHEN it was built -- a strike that looked like resistance a
        # month ago and hasn't been touched since is a much weaker
        # assumption than one actively being built right now.
        put_wall_fresh = _wall_freshness(sym, selected_expiry, pw_strike, "put")
        call_wall_fresh = _wall_freshness(sym, selected_expiry, cw_strike, "call")

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
            "put_wall_status":      put_wall_fresh["status"],
            "put_wall_oi_change_pct": put_wall_fresh["oi_change_pct"],
            "call_wall_strength":   cw_strength,
            "call_wall_status":     call_wall_fresh["status"],
            "call_wall_oi_change_pct": call_wall_fresh["oi_change_pct"],
            "pcr_near_spot":        pcr,
            "hv_30d":               hv_30,
            "ivp_proxy":            ivp,
            "expected_move_1sd":    exp_move,
            "rr":                   condor_rr,
            "total_credit":         round(total_credit, 2) if total_credit is not None else None,
            "condor_width":         round(condor_width, 2),
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

def find_verticals(selected_expiry, strike_distance=2, min_wall_oi: int = 1000, min_rr: float = 0.35, min_ivp: float = 30):
    """
    Enhanced vertical scanner:
    - Grades A/B/C
    - Shows wall strength ratio (wall OI vs 2nd-highest)
    - Shows PCR for directional bias
    - Suggests exact spread legs
    - IVP proxy included
    - min_wall_oi: liquidity floor on the wall's own OI -- was hardcoded
      to 1000 with no way to see or adjust it; same default now exposed.
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
        if ivp is not None and ivp < min_ivp:
            continue
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

            if pw_near and pw_oi > min_wall_oi:
                puts_sorted  = sorted(puts_below, key=lambda x: x[1])
                wall_strength = _wall_strength_ratio(puts_sorted)
                dist_pct      = round(100 * abs(spot - pw_str) / spot, 2)
                # suggest: sell the wall strike, buy 1 interval lower
                long_leg  = round(pw_str - strike_interval, 2)
                wall_fresh = _wall_freshness(sym, selected_expiry, pw_str, "put")

                short_px = _leg_price(c, sym, selected_expiry, last_date, pw_str, "P")
                long_px  = _leg_price(c, sym, selected_expiry, last_date, long_leg, "P")
                credit = (short_px - long_px) if (short_px is not None and long_px is not None) else None
                width = abs(pw_str - long_leg)
                rr = _spread_rr(credit, width)
                if rr is not None and rr < min_rr:
                    continue

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
                    "wall_status":        wall_fresh["status"],
                    "wall_oi_change_pct": wall_fresh["oi_change_pct"],
                    "rr": rr,
                    "credit": round(credit, 2) if credit is not None else None,
                    "width": round(width, 2),
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

            if cw_near and cw_oi > min_wall_oi:
                calls_sorted  = sorted(calls_above, key=lambda x: x[1])
                wall_strength  = _wall_strength_ratio(calls_sorted)
                dist_pct       = round(100 * abs(cw_str - spot) / spot, 2)
                long_leg       = round(cw_str + strike_interval, 2)
                wall_fresh = _wall_freshness(sym, selected_expiry, cw_str, "call")

                short_px = _leg_price(c, sym, selected_expiry, last_date, cw_str, "C")
                long_px  = _leg_price(c, sym, selected_expiry, last_date, long_leg, "C")
                credit = (short_px - long_px) if (short_px is not None and long_px is not None) else None
                width = abs(long_leg - cw_str)
                rr = _spread_rr(credit, width)
                if rr is not None and rr < min_rr:
                    continue

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
                    "wall_status":        wall_fresh["status"],
                    "wall_oi_change_pct": wall_fresh["oi_change_pct"],
                    "rr": rr,
                    "credit": round(credit, 2) if credit is not None else None,
                    "width": round(width, 2),
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


# ─── Recent price-action context ──────────────────────────────────────────
# Neither scanner above factors in recent price action at all -- both are
# built entirely from wall OI/strength/distance plus HV/IVP and PCR. That's
# a real gap for condors specifically: a symbol trending hard toward one
# wall is a worse condor candidate than the OI/IV numbers alone suggest,
# regardless of how well-formed the walls look, since a live trend is
# more likely to run through a wall than respect it. Added as a light
# enrichment using already-stored daily history (same fast path Swing
# Positioning Scanner uses, no new live fetch), not a rewrite of the
# underlying wall-based scoring.
def _price_action_context(symbol: str, lookback_days: int = 5):
    try:
        from .scanner_builder import _price_cache_daily_history
        df = _price_cache_daily_history(symbol)
        if df is None or len(df) < lookback_days + 1:
            return None
        recent = df.tail(lookback_days + 1)
        start_close = float(recent["close"].iloc[0])
        end_close = float(recent["close"].iloc[-1])
        if not start_close:
            return None
        pct_change = round((end_close - start_close) / start_close * 100, 2)
        if pct_change > 2.5:
            trend = "trending up"
        elif pct_change < -2.5:
            trend = "trending down"
        else:
            trend = "range-bound"
        return {"pct_change_5d": pct_change, "trend": trend}
    except Exception:
        return None


def enrich_with_price_action(results: list) -> list:
    """Adds recent-price-action context to condor/vertical results in
    place, plus a lightweight consistency flag: a condor on a symbol
    that's trending hard toward one of its own walls right now is worth
    flagging even though the wall-based score doesn't see it -- OI can be
    real and well-formed while still being at risk from a live trend
    that hasn't caught up to it yet. Doesn't touch signal_score/grade --
    additive context, not a rescoring of the existing logic."""
    cache: dict = {}
    for r in results:
        sym = r.get("symbol")
        if sym not in cache:
            cache[sym] = _price_action_context(sym)
        ctx = cache[sym]
        if not ctx:
            continue
        r["price_action_5d"] = ctx
        strategy = r.get("strategy")
        if strategy == "Bull Put Spread" and ctx["trend"] == "trending down":
            r["price_action_flag"] = "Trending DOWN toward the put wall right now -- OI looks fine, but a live trend is more likely to run through a wall than respect it."
        elif strategy == "Bear Call Spread" and ctx["trend"] == "trending up":
            r["price_action_flag"] = "Trending UP toward the call wall right now -- same caution as above, from the other side."
        elif r.get("put_wall_strike") is not None:  # condor result shape
            if ctx["trend"] == "trending down":
                r["price_action_flag"] = "Trending DOWN toward the put wall right now -- worth checking whether the range this condor assumes is still holding."
            elif ctx["trend"] == "trending up":
                r["price_action_flag"] = "Trending UP toward the call wall right now -- same caution, from the other side."
    return results


# ─── Routes ────────────────────────────────────────────────────────────────

from flask import Blueprint, jsonify, render_template, request

iron_condor_bp = Blueprint("iron_condor_candidates", __name__, url_prefix="/iron-condor-scanner")


@iron_condor_bp.route("/")
def page():
    return render_template("iron_condor_scanner.html")


@iron_condor_bp.route("/api/expiries")
def api_expiries():
    """Distinct stored expiries to populate the picker -- reads directly
    from the options table, same TABLE constant the scanners themselves use."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(f"SELECT DISTINCT expiration FROM {TABLE} ORDER BY expiration")
        expiries = [r[0] for r in c.fetchall() if r[0]]
        conn.close()
        return jsonify({"ok": True, "expiries": expiries})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@iron_condor_bp.route("/api/condors")
def api_condors():
    expiry = request.args.get("expiry")
    if not expiry:
        return jsonify({"ok": False, "error": "expiry required"}), 400
    min_oi = int(request.args.get("min_oi", 2000))
    min_rr = float(request.args.get("min_rr", 0.5))
    min_ivp = float(request.args.get("min_ivp", 30))
    try:
        results = find_condor_candidates(expiry, min_oi_near_spot=min_oi, min_rr=min_rr, min_ivp=min_ivp)
        results = enrich_with_price_action(results)
        return jsonify({"ok": True, "results": results, "count": len(results)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@iron_condor_bp.route("/api/verticals")
def api_verticals():
    expiry = request.args.get("expiry")
    if not expiry:
        return jsonify({"ok": False, "error": "expiry required"}), 400
    strike_distance = int(request.args.get("strike_distance", 2))
    min_oi = int(request.args.get("min_oi", 1000))
    min_rr = float(request.args.get("min_rr", 0.35))
    min_ivp = float(request.args.get("min_ivp", 30))
    try:
        results = find_verticals(expiry, strike_distance=strike_distance, min_wall_oi=min_oi, min_rr=min_rr, min_ivp=min_ivp)
        results = enrich_with_price_action(results)
        return jsonify({"ok": True, "results": results, "count": len(results)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
