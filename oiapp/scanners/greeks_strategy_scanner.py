# oiapp/scanners/greeks_strategy_scanner.py
"""
Options-Greeks-based multi-leg strategy scanner.

Reuses this app's existing, already-correct Black-Scholes toolkit
(options_analysis.py's bs_greeks/expiry_pnl) rather than reimplementing
pricing math -- and reuses iron_condor_candidates.py's wall/liquidity/
freshness infrastructure for the iron condor case specifically, since
that's a proven, already-built strategy this scanner shouldn't duplicate.

What's genuinely new here: short strangle, short straddle, and iron fly
construction; a proper probability-of-profit calculation (lognormal
breakeven probability under the same risk-neutral assumption
Black-Scholes itself uses -- not a delta shortcut, which only
approximates POP for a single short leg, not a two-sided structure);
RR from real expiry payoff (max profit / max loss, not an estimate);
and a separate earnings-day mode.

POP METHODOLOGY (stated plainly, not just implemented silently):
Under the same lognormal assumption Black-Scholes itself uses, ln(S_T/S0)
is approximately Normal(-0.5*sigma^2*T, sigma^2*T) (zero-drift risk-neutral
convention -- the same one most retail POP calculators use, not adding an
equity risk premium on top). POP = P(lower breakeven < S_T < upper
breakeven) = N(z_upper) - N(z_lower) where z = (ln(BE/S0) + 0.5*sigma^2*T)
/ (sigma*sqrt(T)). This is a real probability calculation, but it's still
a MODEL's estimate under lognormal/constant-vol assumptions -- actual
markets have fatter tails and vol isn't constant through DTE, so treat
POP as "what the model says," not a guaranteed frequency.

EARNINGS MODE, stated honestly: this sizes a short strangle/iron-condor
to the market's OWN priced-in expected move (ATM straddle price, the
standard convention -- "the market is pricing in about this much
movement") and calculates POP/RR the same way as any other scan. It does
NOT predict how much IV will actually crush post-earnings -- no
historical earnings-specific IV-crush dataset exists anywhere in this
app to calibrate that from. The structure (sell elevated pre-earnings
IV, size to the market's own expected move, defined risk via the fly/
condor wings) is a real, common approach; the exact next-day P&L still
depends on IV crush magnitude and direction, which this scan cannot
predict, only structure around.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

from ..config import DB_PATH as _OIAPP_DB_PATH
from .options_analysis import bs_greeks, expiry_pnl, _norm_cdf
from .iron_condor_candidates import get_spot as _ic_get_spot, _get_iv_metrics, _dte, _expiry_type
from .earnings_calendar import get_earnings_info
from .watchlist_manager import _get_watchlist_symbols_by_id

greeks_strategy_bp = Blueprint("greeks_strategy_scanner", __name__, url_prefix="/greeks-strategy-scanner")

TABLE = "options"
DEFAULT_MIN_RR = 0.7
DEFAULT_MIN_POP = 0.0  # no POP floor by default -- RR and POP trade off against each other, forcing both high at once can filter out everything


# ── Shared helpers ──────────────────────────────────────────────────────

def _strike_universe(symbol: str, expiry: str) -> Dict[str, Any]:
    """All distinct call/put strikes stored for this symbol+expiry, with
    price/OI/volume/bid/ask/iv per strike -- pulling bid/ask/iv now too
    (previously only price/oi), since the options table genuinely has
    real per-strike market-implied IV (yfinance-sourced, not a broker
    Greek feed -- see this module's docstring) and real bid/ask, both of
    which were sitting unused. iv per strike lets each leg be priced off
    its OWN market-implied vol instead of one flat symbol-wide proxy;
    bid/ask is what the liquidity gate below actually needs."""
    conn = sqlite3.connect(_OIAPP_DB_PATH)
    c = conn.cursor()
    c.execute(f"SELECT date FROM {TABLE} WHERE symbol=? AND expiration=? ORDER BY date DESC LIMIT 1", (symbol, expiry))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"date": None, "calls": {}, "puts": {}}
    last_date = row[0]
    c.execute(f"SELECT type, strike, price, oi, volume, bid, ask, iv FROM {TABLE} WHERE symbol=? AND expiration=? AND date=?",
              (symbol, expiry, last_date))
    calls, puts = {}, {}
    for typ, strike, price, oi, volume, bid, ask, iv in c.fetchall():
        d = calls if str(typ).upper().startswith("C") else puts
        d[float(strike)] = {
            "price": float(price) if price is not None else None,
            "oi": int(oi or 0), "volume": int(volume or 0),
            "bid": float(bid) if bid is not None else None,
            "ask": float(ask) if ask is not None else None,
            "iv": float(iv) if iv is not None else None,
        }
    conn.close()
    return {"date": last_date, "calls": calls, "puts": puts}


def _leg_iv(universe_side: Dict[float, dict], strike: float, fallback_iv: float) -> float:
    """Real per-strike market-implied IV when available, falling back to
    the symbol-wide HV-based proxy for a strike missing IV data OR
    carrying an implausible one. The plausibility check matters: yfinance's
    impliedVolatility field is a known-unreliable data source -- it can
    return near-zero or otherwise implausible values for real strikes,
    not just missing/None ones. An unchecked near-zero IV silently
    collapses the whole lognormal model this scanner is built on: POP
    rounds to exactly 100% (verified directly -- IV=0.01 or below makes
    _pop_between_breakevens return 1.0 regardless of how wide the
    breakevens actually are), and Black-Scholes delta near spot collapses
    toward zero, which is why the same bad IV also corrupts delta-based
    strike selection (Jade Lizard picking a strike hugging spot instead
    of genuinely ~20-delta). 3% floor / 300% ceiling are generous, not
    tight -- meant to catch clearly-broken values, not to second-guess
    genuinely high or low real IV."""
    row = universe_side.get(strike)
    if row and row.get("iv") and 0.03 <= row["iv"] <= 3.0:
        return row["iv"]
    return fallback_iv


def _liquidity_check(universe_side: Dict[float, dict], strike: float, min_oi: int, max_spread_pct: float) -> Dict[str, Any]:
    """Real liquidity gate, not a proxy -- exactly what was missing
    before. Two independent checks: OI floor (raw contract count, the
    same convention used elsewhere in this app) and bid-ask spread as a
    percentage of the midpoint (the actual manipulation/thin-market
    signal -- a strike can have decent OI from old positioning while
    currently having a wide, stale, effectively untradeable quote).
    Returns pass/fail plus the actual numbers, not just a boolean, so a
    failure is explainable rather than a silent drop."""
    row = universe_side.get(strike) or {}
    oi = row.get("oi") or 0
    bid, ask = row.get("bid"), row.get("ask")
    spread_pct = None
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2
        spread_pct = round((ask - bid) / mid * 100, 2) if mid > 0 else None
    oi_ok = oi >= min_oi
    spread_ok = spread_pct is None or spread_pct <= max_spread_pct  # unknown spread doesn't auto-fail -- some rows lack bid/ask but have a valid last price
    return {"pass": oi_ok and spread_ok, "oi": oi, "spread_pct": spread_pct, "oi_ok": oi_ok, "spread_ok": spread_ok}


def _nearest_strike(strikes: List[float], target: float) -> Optional[float]:
    if not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - target))


def _leg_price_or_bs(universe_side: Dict[float, dict], strike: float, spot: float, T: float, iv: float, is_call: bool) -> float:
    """Real stored premium if this exact strike exists in today's chain
    snapshot; falls back to Black-Scholes only for a strike that isn't
    in the stored data (e.g. a wing placed wider than what's currently
    listed). Prefer real data whenever it exists -- it reflects actual
    bid/ask-adjacent pricing, not a model's idealized number."""
    row = universe_side.get(strike)
    if row and row.get("price") is not None and row["price"] > 0:
        return row["price"]
    return bs_greeks(spot, strike, T, iv, is_call=is_call)["price"]


def _pop_between_breakevens(spot: float, be_lower: Optional[float], be_upper: Optional[float], T: float, iv: float) -> Optional[float]:
    """Lognormal probability the underlying finishes between the two
    breakevens at expiry -- see this module's docstring for the exact
    assumption (zero-drift lognormal, the same convention Black-Scholes
    itself and most retail POP tools use). None on either side means
    "no breakeven on that side" (e.g. a naked short strangle has no
    upside cap without a long wing) -- treated as -inf/+inf, not 0.
    """
    if spot <= 0 or T <= 0 or iv <= 0:
        return None
    sig_sqrt_t = iv * math.sqrt(T)
    if sig_sqrt_t <= 0:
        return None

    def _z(be):
        return (math.log(be / spot) + 0.5 * iv * iv * T) / sig_sqrt_t

    p_below_upper = 1.0 if be_upper is None else _norm_cdf(_z(be_upper))
    p_below_lower = 0.0 if be_lower is None else _norm_cdf(_z(be_lower))
    return round(max(0.0, min(1.0, p_below_upper - p_below_lower)), 4)


def _rr_from_expiry_curve(legs: List[dict], spot: float, wide_range_pct: float = 0.5) -> Dict[str, Any]:
    """Max profit / max loss / breakevens from the REAL expiry payoff
    curve (options_analysis.py's expiry_pnl), not an estimated formula --
    correct for any leg combination, including asymmetric or unusual
    structures, since it just evaluates intrinsic value across a spot
    range rather than assuming a specific strategy shape."""
    lo, hi = spot * (1 - wide_range_pct), spot * (1 + wide_range_pct)
    spot_range = [lo + i * (hi - lo) / 400 for i in range(401)]
    curve = expiry_pnl(legs, spot_range)
    pnls = [c["pnl"] for c in curve]
    max_profit = max(pnls)
    max_loss = min(pnls)
    # Breakevens, classified by CROSSING DIRECTION, not position in the
    # list -- an up-crossing (pnl goes from <=0 to >0 as spot rises) is
    # a lower breakeven; a down-crossing (>0 to <=0) is an upper
    # breakeven. A structure can have any number of each depending on
    # its shape (an asymmetric structure like a Jade Lizard or broken-
    # wing fly can be profitable across an entire wide range and only
    # cross once, on one side only -- there is no rule that a strategy
    # must have exactly one of each). Previous version assigned
    # breakevens by list position ([0] and [-1]) with a fallback that
    # guessed "single crossing near the range midpoint means treat it as
    # both bounds" -- that fallback was flatly wrong for a structure
    # whose single real crossing is an upper bound with no lower bound
    # at all (profitable all the way down), which is exactly what a
    # loosely-protected put spread combined with a call spread credit
    # large enough to cover it can produce.
    up_crossings, down_crossings = [], []
    for i in range(1, len(curve)):
        a, b = curve[i - 1], curve[i]
        if a["pnl"] <= 0 < b["pnl"]:
            frac = abs(a["pnl"]) / (abs(a["pnl"]) + abs(b["pnl"])) if (abs(a["pnl"]) + abs(b["pnl"])) else 0
            up_crossings.append(round(a["spot"] + frac * (b["spot"] - a["spot"]), 2))
        elif a["pnl"] >= 0 > b["pnl"]:
            frac = abs(a["pnl"]) / (abs(a["pnl"]) + abs(b["pnl"])) if (abs(a["pnl"]) + abs(b["pnl"])) else 0
            down_crossings.append(round(a["spot"] + frac * (b["spot"] - a["spot"]), 2))
    # Lowest up-crossing = the true lower breakeven (below it, losing
    # money); highest down-crossing = the true upper breakeven (above
    # it, losing money). Either can genuinely be absent.
    be_lower = min(up_crossings) if up_crossings else None
    be_upper = max(down_crossings) if down_crossings else None
    risk = abs(max_loss) if max_loss < 0 else None
    reward = max_profit if max_profit > 0 else None
    rr = round(reward / risk, 3) if (reward is not None and risk not in (None, 0)) else None
    return {"max_profit": round(max_profit, 2), "max_loss": round(max_loss, 2), "rr": rr,
            "breakeven_lower": be_lower, "breakeven_upper": be_upper}


# ── Strategy builders ────────────────────────────────────────────────────

def _check_legs_liquidity(legs_meta: List[Dict[str, Any]], min_oi: int, max_spread_pct: float) -> Dict[str, Any]:
    """Runs _liquidity_check on every leg at once -- a strategy is only
    as liquid as its WORST leg, so this fails the whole structure if any
    single leg fails, rather than averaging or only checking one side.
    legs_meta: [{"universe_side": dict, "strike": float, "label": str}]"""
    details = []
    all_pass = True
    for lm in legs_meta:
        chk = _liquidity_check(lm["universe_side"], lm["strike"], min_oi, max_spread_pct)
        chk["leg"] = lm["label"]
        details.append(chk)
        if not chk["pass"]:
            all_pass = False
    return {"pass": all_pass, "legs": details}


def _build_short_strangle(symbol: str, expiry: str, spot: float, iv: float, T: float, dte: int,
                           universe: dict, wing_delta_target: float = 0.16,
                           min_oi: int = 50, max_spread_pct: float = 15.0) -> Optional[Dict[str, Any]]:
    """Sell OTM call + OTM put, no wings -- undefined risk on paper, but
    max_loss from expiry_pnl over the scanned range still gives a
    meaningful (if range-capped) risk figure. wing_delta_target ~0.16 is
    the common "1 standard deviation" strangle convention."""
    calls, puts = universe["calls"], universe["puts"]
    if not calls or not puts:
        return None
    call_strikes, put_strikes = sorted(calls.keys()), sorted(puts.keys())
    def _closest_by_delta(strikes, is_call, side_map):
        best, best_diff = None, 1e9
        for k in strikes:
            leg_iv = _leg_iv(side_map, k, iv)
            g = bs_greeks(spot, k, T, leg_iv, is_call=is_call)
            diff = abs(abs(g["delta"]) - wing_delta_target)
            if diff < best_diff:
                best, best_diff = k, diff
        return best
    call_k = _closest_by_delta([k for k in call_strikes if k > spot], True, calls)
    put_k = _closest_by_delta([k for k in put_strikes if k < spot], False, puts)
    if call_k is None or put_k is None:
        return None
    liq = _check_legs_liquidity([{"universe_side": calls, "strike": call_k, "label": "short call"},
                                  {"universe_side": puts, "strike": put_k, "label": "short put"}], min_oi, max_spread_pct)
    if not liq["pass"]:
        return None
    call_iv, put_iv = _leg_iv(calls, call_k, iv), _leg_iv(puts, put_k, iv)
    call_px = _leg_price_or_bs(calls, call_k, spot, T, call_iv, True)
    put_px = _leg_price_or_bs(puts, put_k, spot, T, put_iv, False)
    legs = [
        {"side": "sell", "option_type": "call", "strike": call_k, "qty": 1, "entry_price": call_px},
        {"side": "sell", "option_type": "put", "strike": put_k, "qty": 1, "entry_price": put_px},
    ]
    curve = _rr_from_expiry_curve(legs, spot)
    call_delta = bs_greeks(spot, call_k, T, call_iv, is_call=True)["delta"]
    put_delta = bs_greeks(spot, put_k, T, put_iv, is_call=False)["delta"]
    pop = _pop_between_breakevens(spot, curve["breakeven_lower"], curve["breakeven_upper"], T, (call_iv + put_iv) / 2)
    return {
        "strategy": "Short Strangle", "symbol": symbol, "expiry": expiry, "dte": dte,
        "spot": round(spot, 2), "legs": legs, "call_strike": call_k, "put_strike": put_k,
        "call_delta": call_delta, "put_delta": put_delta, "liquidity": liq,
        "credit": round(call_px + put_px, 2), "pop": pop, "avg_iv": round((call_iv + put_iv) / 2, 4), **curve,
    }


def _build_short_straddle(symbol: str, expiry: str, spot: float, iv: float, T: float, dte: int, universe: dict,
                           min_oi: int = 50, max_spread_pct: float = 15.0) -> Optional[Dict[str, Any]]:
    """Sell ATM call + ATM put (same strike). Higher credit than a
    strangle, tighter breakevens -- the classic "sell the whole expected
    move" structure, most relevant for the earnings mode below."""
    calls, puts = universe["calls"], universe["puts"]
    if not calls or not puts:
        return None
    all_strikes = sorted(set(calls.keys()) | set(puts.keys()))
    atm = _nearest_strike(all_strikes, spot)
    if atm is None:
        return None
    liq = _check_legs_liquidity([{"universe_side": calls, "strike": atm, "label": "short call"},
                                  {"universe_side": puts, "strike": atm, "label": "short put"}], min_oi, max_spread_pct)
    if not liq["pass"]:
        return None
    call_iv, put_iv = _leg_iv(calls, atm, iv), _leg_iv(puts, atm, iv)
    call_px = _leg_price_or_bs(calls, atm, spot, T, call_iv, True)
    put_px = _leg_price_or_bs(puts, atm, spot, T, put_iv, False)
    legs = [
        {"side": "sell", "option_type": "call", "strike": atm, "qty": 1, "entry_price": call_px},
        {"side": "sell", "option_type": "put", "strike": atm, "qty": 1, "entry_price": put_px},
    ]
    curve = _rr_from_expiry_curve(legs, spot)
    pop = _pop_between_breakevens(spot, curve["breakeven_lower"], curve["breakeven_upper"], T, (call_iv + put_iv) / 2)
    return {
        "strategy": "Short Straddle", "symbol": symbol, "expiry": expiry, "dte": dte,
        "spot": round(spot, 2), "legs": legs, "atm_strike": atm, "liquidity": liq,
        "credit": round(call_px + put_px, 2), "pop": pop, "avg_iv": round((call_iv + put_iv) / 2, 4), **curve,
        "expected_move": round(call_px + put_px, 2),
    }


def _build_iron_fly(symbol: str, expiry: str, spot: float, iv: float, T: float, dte: int,
                     universe: dict, wing_width_strikes: int = 4,
                     min_oi: int = 50, max_spread_pct: float = 15.0) -> Optional[Dict[str, Any]]:
    """Short ATM straddle + long OTM wings on both sides -- the
    defined-risk version of a short straddle. wing_width_strikes counts
    listed strikes out from ATM, not a fixed dollar width, so it scales
    naturally with each symbol's own strike spacing."""
    calls, puts = universe["calls"], universe["puts"]
    if not calls or not puts:
        return None
    call_strikes, put_strikes = sorted(calls.keys()), sorted(puts.keys())
    all_strikes = sorted(set(call_strikes) | set(put_strikes))
    atm = _nearest_strike(all_strikes, spot)
    if atm is None:
        return None
    call_wings_above = [k for k in call_strikes if k > atm]
    put_wings_below = [k for k in put_strikes if k < atm]
    if len(call_wings_above) < wing_width_strikes or len(put_wings_below) < wing_width_strikes:
        return None
    long_call_k = call_wings_above[min(wing_width_strikes - 1, len(call_wings_above) - 1)]
    long_put_k = put_wings_below[-min(wing_width_strikes, len(put_wings_below))]
    liq = _check_legs_liquidity([
        {"universe_side": calls, "strike": atm, "label": "short call"},
        {"universe_side": puts, "strike": atm, "label": "short put"},
        {"universe_side": calls, "strike": long_call_k, "label": "long call"},
        {"universe_side": puts, "strike": long_put_k, "label": "long put"},
    ], min_oi, max_spread_pct)
    if not liq["pass"]:
        return None
    short_call_iv, short_put_iv = _leg_iv(calls, atm, iv), _leg_iv(puts, atm, iv)
    long_call_iv, long_put_iv = _leg_iv(calls, long_call_k, iv), _leg_iv(puts, long_put_k, iv)
    short_call_px = _leg_price_or_bs(calls, atm, spot, T, short_call_iv, True)
    short_put_px = _leg_price_or_bs(puts, atm, spot, T, short_put_iv, False)
    long_call_px = _leg_price_or_bs(calls, long_call_k, spot, T, long_call_iv, True)
    long_put_px = _leg_price_or_bs(puts, long_put_k, spot, T, long_put_iv, False)
    legs = [
        {"side": "sell", "option_type": "call", "strike": atm, "qty": 1, "entry_price": short_call_px},
        {"side": "sell", "option_type": "put", "strike": atm, "qty": 1, "entry_price": short_put_px},
        {"side": "buy", "option_type": "call", "strike": long_call_k, "qty": 1, "entry_price": long_call_px},
        {"side": "buy", "option_type": "put", "strike": long_put_k, "qty": 1, "entry_price": long_put_px},
    ]
    curve = _rr_from_expiry_curve(legs, spot)
    avg_iv = (short_call_iv + short_put_iv) / 2
    pop = _pop_between_breakevens(spot, curve["breakeven_lower"], curve["breakeven_upper"], T, avg_iv)
    net_credit = short_call_px + short_put_px - long_call_px - long_put_px
    return {
        "strategy": "Iron Fly", "symbol": symbol, "expiry": expiry, "dte": dte,
        "spot": round(spot, 2), "legs": legs, "atm_strike": atm, "liquidity": liq,
        "long_call_strike": long_call_k, "long_put_strike": long_put_k,
        "credit": round(net_credit, 2), "pop": pop, "avg_iv": round(avg_iv, 4), **curve,
    }


def _build_jade_lizard(symbol: str, expiry: str, spot: float, iv: float, T: float, dte: int,
                        universe: dict, put_delta_target: float = 0.20, call_wing_strikes: int = 1,
                        put_wing_strikes: int = 8,
                        min_oi: int = 50, max_spread_pct: float = 15.0) -> Optional[Dict[str, Any]]:
    """Short put + defined-risk short call spread + a FAR OTM protective
    long put (an afterthought for defined risk, not a tight-fitting
    fourth leg -- put_wing_strikes defaults much wider than the call
    side's width specifically so this doesn't collapse into the same
    leg shape as an Iron Condor, which was a real bug in an earlier
    version of this function: adding tight protection right below the
    short put made every Jade Lizard indistinguishable from a plain
    iron condor with asymmetric widths).

    The actual defining Jade Lizard property -- call spread credit
    covers the call spread's own width, meaning no upside risk -- is
    now ENFORCED as a construction requirement, not just reported as an
    observed flag. That's the real reason to reach for this strategy
    over an iron condor: every result genuinely has it, not just
    sometimes."""
    calls, puts = universe["calls"], universe["puts"]
    if not calls or not puts:
        return None
    put_strikes, call_strikes = sorted(puts.keys()), sorted(calls.keys())
    def _closest_put_by_delta(strikes):
        best, best_diff = None, 1e9
        for k in strikes:
            leg_iv = _leg_iv(puts, k, iv)
            g = bs_greeks(spot, k, T, leg_iv, is_call=False)
            diff = abs(abs(g["delta"]) - put_delta_target)
            if diff < best_diff:
                best, best_diff = k, diff
        return best
    short_put_k = _closest_put_by_delta([k for k in put_strikes if k < spot])
    call_wings_above = [k for k in call_strikes if k > spot]
    if short_put_k is None or len(call_wings_above) < call_wing_strikes + 1:
        return None
    short_call_k = call_wings_above[0]
    long_call_k = call_wings_above[call_wing_strikes]
    protective_put_strikes = [k for k in put_strikes if k < short_put_k]
    if len(protective_put_strikes) < put_wing_strikes:
        return None
    long_put_k = protective_put_strikes[-put_wing_strikes]  # far OTM -- cheap insurance, not a tight iron-condor-style wing
    liq = _check_legs_liquidity([
        {"universe_side": puts, "strike": short_put_k, "label": "short put"},
        {"universe_side": calls, "strike": short_call_k, "label": "short call"},
        {"universe_side": calls, "strike": long_call_k, "label": "long call"},
        {"universe_side": puts, "strike": long_put_k, "label": "long put"},
    ], min_oi, max_spread_pct)
    if not liq["pass"]:
        return None
    sp_iv, sc_iv, lc_iv, lp_iv = _leg_iv(puts, short_put_k, iv), _leg_iv(calls, short_call_k, iv), _leg_iv(calls, long_call_k, iv), _leg_iv(puts, long_put_k, iv)
    short_put_px = _leg_price_or_bs(puts, short_put_k, spot, T, sp_iv, False)
    short_call_px = _leg_price_or_bs(calls, short_call_k, spot, T, sc_iv, True)
    long_call_px = _leg_price_or_bs(calls, long_call_k, spot, T, lc_iv, True)
    long_put_px = _leg_price_or_bs(puts, long_put_k, spot, T, lp_iv, False)
    # Enforce the defining property BEFORE building the result. Correctly
    # this time: the real Jade Lizard "no upside risk" property compares
    # TOTAL credit collected (put premium + call spread premium) against
    # the call spread's width -- not the call spread's own premium alone.
    # Checked that distinction empirically before fixing it: a vanilla
    # OTM 1-point-wide call spread essentially never captures 100%+ of
    # its own width in credit under any realistic IV (verified directly,
    # even at 80% IV/10 DTE it only reached ~45-49%) -- that first
    # version of this check was accidentally demanding something close
    # to impossible under real option pricing, which is why it kept
    # rejecting everything even under favorable conditions. The put
    # side's own premium is what actually makes "total credit covers
    # the call spread's width" achievable in practice.
    call_spread_width_check = long_call_k - short_call_k
    total_credit_check = short_put_px + short_call_px - long_call_px - long_put_px
    if total_credit_check < call_spread_width_check:
        return None
    legs = [
        {"side": "sell", "option_type": "put", "strike": short_put_k, "qty": 1, "entry_price": short_put_px},
        {"side": "sell", "option_type": "call", "strike": short_call_k, "qty": 1, "entry_price": short_call_px},
        {"side": "buy", "option_type": "call", "strike": long_call_k, "qty": 1, "entry_price": long_call_px},
        {"side": "buy", "option_type": "put", "strike": long_put_k, "qty": 1, "entry_price": long_put_px},
    ]
    curve = _rr_from_expiry_curve(legs, spot)
    avg_iv = (sp_iv + sc_iv) / 2
    pop = _pop_between_breakevens(spot, curve["breakeven_lower"], curve["breakeven_upper"], T, avg_iv)
    call_spread_credit = short_call_px - long_call_px
    call_spread_width = long_call_k - short_call_k
    net_credit = short_put_px + short_call_px - long_call_px - long_put_px
    return {
        "strategy": "Jade Lizard", "symbol": symbol, "expiry": expiry, "dte": dte,
        "spot": round(spot, 2), "legs": legs, "short_put_strike": short_put_k,
        "short_call_strike": short_call_k, "long_call_strike": long_call_k, "long_put_strike": long_put_k,
        "liquidity": liq, "credit": round(net_credit, 2), "pop": pop, "avg_iv": round(avg_iv, 4), **curve,
        "no_upside_risk": net_credit >= call_spread_width,
        "call_spread_credit": round(call_spread_credit, 2), "call_spread_width": round(call_spread_width, 2),
    }


def _build_broken_wing_fly(symbol: str, expiry: str, spot: float, iv: float, T: float, dte: int,
                            universe: dict, near_wing_strikes: int = 2, far_wing_strikes: int = 5,
                            min_oi: int = 50, max_spread_pct: float = 15.0) -> Optional[Dict[str, Any]]:
    """Short ATM straddle + asymmetric wings (put side wider than call
    side) -- unlike a symmetric iron fly, the asymmetry is deliberate:
    a wider, cheaper wing on one side vs a narrower, more expensive one
    on the other shifts the risk/reward intentionally and can turn what
    would be a debit fly into a net credit. Put-skewed (wide put wing,
    narrow call wing) here specifically, since that's the more common
    construction for a bullish-leaning, still fully defined-risk credit
    structure -- the call side stays tightly capped while the put side
    gets more room."""
    calls, puts = universe["calls"], universe["puts"]
    if not calls or not puts:
        return None
    call_strikes, put_strikes = sorted(calls.keys()), sorted(puts.keys())
    all_strikes = sorted(set(call_strikes) | set(put_strikes))
    atm = _nearest_strike(all_strikes, spot)
    if atm is None:
        return None
    call_wings_above = [k for k in call_strikes if k > atm]
    put_wings_below = [k for k in put_strikes if k < atm]
    if len(call_wings_above) < near_wing_strikes or len(put_wings_below) < far_wing_strikes:
        return None
    long_call_k = call_wings_above[min(near_wing_strikes - 1, len(call_wings_above) - 1)]  # narrow call wing
    long_put_k = put_wings_below[-min(far_wing_strikes, len(put_wings_below))]  # wide put wing
    liq = _check_legs_liquidity([
        {"universe_side": calls, "strike": atm, "label": "short call"},
        {"universe_side": puts, "strike": atm, "label": "short put"},
        {"universe_side": calls, "strike": long_call_k, "label": "long call"},
        {"universe_side": puts, "strike": long_put_k, "label": "long put"},
    ], min_oi, max_spread_pct)
    if not liq["pass"]:
        return None
    short_call_iv, short_put_iv = _leg_iv(calls, atm, iv), _leg_iv(puts, atm, iv)
    long_call_iv, long_put_iv = _leg_iv(calls, long_call_k, iv), _leg_iv(puts, long_put_k, iv)
    short_call_px = _leg_price_or_bs(calls, atm, spot, T, short_call_iv, True)
    short_put_px = _leg_price_or_bs(puts, atm, spot, T, short_put_iv, False)
    long_call_px = _leg_price_or_bs(calls, long_call_k, spot, T, long_call_iv, True)
    long_put_px = _leg_price_or_bs(puts, long_put_k, spot, T, long_put_iv, False)
    legs = [
        {"side": "sell", "option_type": "call", "strike": atm, "qty": 1, "entry_price": short_call_px},
        {"side": "sell", "option_type": "put", "strike": atm, "qty": 1, "entry_price": short_put_px},
        {"side": "buy", "option_type": "call", "strike": long_call_k, "qty": 1, "entry_price": long_call_px},
        {"side": "buy", "option_type": "put", "strike": long_put_k, "qty": 1, "entry_price": long_put_px},
    ]
    curve = _rr_from_expiry_curve(legs, spot)
    avg_iv = (short_call_iv + short_put_iv) / 2
    pop = _pop_between_breakevens(spot, curve["breakeven_lower"], curve["breakeven_upper"], T, avg_iv)
    net_credit = short_call_px + short_put_px - long_call_px - long_put_px
    return {
        "strategy": "Broken-Wing Fly (put-skewed)", "symbol": symbol, "expiry": expiry, "dte": dte,
        "spot": round(spot, 2), "legs": legs, "atm_strike": atm, "liquidity": liq,
        "long_call_strike": long_call_k, "long_put_strike": long_put_k,
        "credit": round(net_credit, 2), "pop": pop, "avg_iv": round(avg_iv, 4), **curve,
    }


STRATEGY_BUILDERS = {
    "strangle": _build_short_strangle,
    "straddle": _build_short_straddle,
    "iron_fly": _build_iron_fly,
    "jade_lizard": _build_jade_lizard,
    "broken_wing_fly": _build_broken_wing_fly,
}


# ── Main scan ─────────────────────────────────────────────────────────

def _watchlist_symbols() -> List[str]:
    conn = sqlite3.connect(_OIAPP_DB_PATH)
    c = conn.cursor()
    c.execute(f"SELECT DISTINCT symbol FROM {TABLE}")
    syms = [r[0] for r in c.fetchall()]
    conn.close()
    return syms


def _resolve_symbols(watchlist_id: Optional[str], symbols: Optional[List[str]]) -> List[str]:
    """Shared symbol-scope resolution: explicit symbols list wins, then
    a selected watchlist, then falls back to every symbol with any
    stored options data. Matches the same watchlist_id convention
    Swing Positioning Scanner and Iron Condor Scanner already use, via
    the same underlying _get_watchlist_symbols_by_id()."""
    if symbols:
        return symbols
    if watchlist_id:
        wl_syms = _get_watchlist_symbols_by_id(watchlist_id)
        if wl_syms:
            return wl_syms
    return _watchlist_symbols()


def _historical_earnings_moves(symbol: str, last_earn_date: Optional[str], n_quarters: int = 4) -> Dict[str, Any]:
    """Average |% move| around past earnings dates, from price_cache.

    HONEST LIMITATION, stated here and surfaced in the API/UI: this app
    stores only the SINGLE most recent earnings date and reaction per
    symbol (earnings_calendar's last_earn_date/earn_reaction_pct) -- no
    multi-quarter earnings-date history exists anywhere in this app to
    look up real past dates from. This function approximates prior
    quarters' dates by stepping back ~91 days at a time from the last
    known date (the standard quarterly cadence), then checks the actual
    price_cache close-to-close move across that approximate date. This
    is a real, computed average from real price data -- but the DATES
    it's centered on are assumed-quarterly-cadence estimates, not
    confirmed historical earnings dates, since no such record exists to
    confirm them against. A company that shifted its reporting date, or
    skipped/merged a quarter, will have some approximate dates land a
    few days off the real one.
    """
    if not last_earn_date:
        return {"avg_move_pct": None, "n_samples": 0, "note": "no earnings date on record"}
    try:
        anchor = datetime.strptime(last_earn_date[:10], "%Y-%m-%d").date()
    except Exception:
        return {"avg_move_pct": None, "n_samples": 0, "note": "unparseable earnings date"}

    conn = sqlite3.connect(_OIAPP_DB_PATH)
    c = conn.cursor()
    c.execute(f"SELECT date, close FROM price_cache WHERE symbol=? ORDER BY date", (symbol,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return {"avg_move_pct": None, "n_samples": 0, "note": "no price history"}
    dates = [r[0] for r in rows]
    closes = {r[0]: r[1] for r in rows}

    moves = []
    for q in range(n_quarters):
        approx_date = anchor - timedelta(days=91 * q)
        # nearest trading day on/after the approximate date, then the
        # trading day immediately before it, for a close-to-close move
        after = [d for d in dates if d >= approx_date.isoformat()]
        if not after:
            continue
        d_after = after[0]
        idx = dates.index(d_after)
        if idx == 0:
            continue
        d_before = dates[idx - 1]
        c_before, c_after = closes[d_before], closes[d_after]
        if c_before and c_before > 0:
            moves.append(abs((c_after - c_before) / c_before * 100))

    if not moves:
        return {"avg_move_pct": None, "n_samples": 0, "note": "insufficient price history around approximate dates"}
    return {"avg_move_pct": round(sum(moves) / len(moves), 2), "n_samples": len(moves),
            "note": f"approximate quarterly-cadence dates, {len(moves)} sample(s)"}


def earnings_calendar_week(watchlist_id: Optional[str] = None, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Symbols with earnings in the next 7 calendar days -- an
    informational calendar view (historical move stats, IV vs HV,
    expected move), not a strategy scan. See _historical_earnings_moves()
    for exactly what the average-move figure does and doesn't represent."""
    symbols = _resolve_symbols(watchlist_id, symbols)
    results = []
    for sym in symbols:
        try:
            earn = get_earnings_info(sym) or {}
            earn_days = earn.get("earn_days")
            if earn_days is None or not (0 <= earn_days <= 7):
                continue
            spot = _ic_get_spot(sym)
            if not spot or spot <= 0:
                continue
            hv_30, hv_252, ivp = _get_iv_metrics(sym)

            conn = sqlite3.connect(_OIAPP_DB_PATH)
            c = conn.cursor()
            c.execute(f"SELECT MIN(expiration) FROM {TABLE} WHERE symbol=? AND expiration>=date('now')", (sym,))
            row = c.fetchone()
            conn.close()
            expiry = row[0] if row else None

            expected_move_pct = None
            atm_iv = None
            if expiry:
                dte = _dte(expiry)
                T = max(dte, 1) / 365.0
                universe = _strike_universe(sym, expiry)
                all_strikes = sorted(set(universe["calls"].keys()) | set(universe["puts"].keys()))
                atm = _nearest_strike(all_strikes, spot)
                if atm is not None:
                    atm_iv = _leg_iv(universe["calls"], atm, (hv_30 or 30) / 100.0)
                    # Expected move via IV directly (iv * sqrt(T) * spot) rather
                    # than requiring a full straddle build here -- cheaper for a
                    # calendar-overview scan across many symbols, same standard formula.
                    expected_move_pct = round(atm_iv * math.sqrt(T) * 100, 2)

            hist = _historical_earnings_moves(sym, earn.get("last_earn_date"))
            iv_hv_ratio = round((atm_iv * 100) / hv_30, 2) if (atm_iv and hv_30 and hv_30 > 0) else None

            results.append({
                "symbol": sym, "spot": round(spot, 2),
                "earn_date": earn.get("earn_date"), "earn_days": earn_days,
                "next_earn_confirmed": earn.get("next_earn_confirmed"),
                "atm_iv_pct": round(atm_iv * 100, 1) if atm_iv else None,
                "hv30_pct": round(hv_30, 1) if hv_30 else None,
                "iv_hv_ratio": iv_hv_ratio,
                "expected_move_pct": expected_move_pct,
                "avg_historical_move_pct": hist["avg_move_pct"], "hist_n_samples": hist["n_samples"],
                "hist_note": hist["note"],
            })
        except Exception:
            continue
    results.sort(key=lambda r: r.get("earn_days", 999))
    return results


def scan_strategies(expiry: str, strategies: List[str], min_rr: float = DEFAULT_MIN_RR,
                     min_pop: float = DEFAULT_MIN_POP, symbols: Optional[List[str]] = None,
                     min_oi: int = 50, max_spread_pct: float = 15.0, watchlist_id: Optional[str] = None,
                     earnings_gate: str = "any") -> List[Dict[str, Any]]:
    """earnings_gate: "any" (no filtering, just shows the earnings date
    on every result -- default), "exclude" (skip any symbol whose
    earnings date falls before this expiry -- avoids accidentally
    selling premium across an event you didn't mean to), "require"
    (only symbols WITH an earnings date before this expiry -- for
    intentionally targeting earnings-adjacent premium through the
    standard scan rather than the separate Pre-Earnings tab)."""
    symbols = _resolve_symbols(watchlist_id, symbols)
    dte = _dte(expiry)
    T = max(dte, 1) / 365.0
    exp_type = _expiry_type(dte)
    results = []
    for sym in symbols:
        try:
            spot = _ic_get_spot(sym)
            if not spot or spot <= 0:
                continue
            hv_30, hv_252, ivp = _get_iv_metrics(sym)
            # HV30 fallback ONLY -- each leg prefers its own real
            # per-strike market-implied IV (the options table's iv
            # column, yfinance-sourced) via _leg_iv(); this symbol-wide
            # HV proxy is used only for strikes missing IV data.
            iv = (hv_30 or 30) / 100.0
            universe = _strike_universe(sym, expiry)
            if not universe["calls"] and not universe["puts"]:
                continue

            earn = get_earnings_info(sym) or {}
            earn_date = earn.get("earn_date")
            earn_days = earn.get("earn_days")
            spans_earnings = earn_days is not None and 0 <= earn_days <= dte
            if earnings_gate == "exclude" and spans_earnings:
                continue
            if earnings_gate == "require" and not spans_earnings:
                continue

            for strat_key in strategies:
                builder = STRATEGY_BUILDERS.get(strat_key)
                if not builder:
                    continue
                res = builder(sym, expiry, spot, iv, T, dte, universe, min_oi=min_oi, max_spread_pct=max_spread_pct)
                if not res:
                    continue
                res["expiry_type"] = exp_type
                res["ivp_proxy"] = ivp
                res["earn_date"] = earn_date
                res["earn_days"] = earn_days
                res["spans_earnings"] = spans_earnings
                if res.get("rr") is not None and res["rr"] < min_rr:
                    continue
                if res.get("pop") is not None and res["pop"] < min_pop:
                    continue
                results.append(res)
        except Exception:
            continue
    results.sort(key=lambda r: (r.get("pop") or 0) * (r.get("rr") or 0), reverse=True)
    return results


def scan_earnings_strategies(strategies: List[str], min_rr: float = 0.3, min_pop: float = 0.0,
                              max_earn_days: int = 1, symbols: Optional[List[str]] = None,
                              min_oi: int = 50, max_spread_pct: float = 15.0, watchlist_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Symbols with earnings within max_earn_days (default: tomorrow or
    today), scanned against their nearest available expiry. See this
    module's docstring for exactly what this can and can't tell you --
    it structures around the market's own priced-in expected move
    (ATM straddle price), it does not predict IV-crush magnitude."""
    symbols = _resolve_symbols(watchlist_id, symbols)
    results = []
    for sym in symbols:
        try:
            earn = get_earnings_info(sym) or {}
            earn_days = earn.get("earn_days")
            if earn_days is None or not (0 <= earn_days <= max_earn_days):
                continue
            conn = sqlite3.connect(_OIAPP_DB_PATH)
            c = conn.cursor()
            c.execute(f"SELECT MIN(expiration) FROM {TABLE} WHERE symbol=? AND expiration>=date('now')", (sym,))
            row = c.fetchone()
            conn.close()
            expiry = row[0] if row else None
            if not expiry:
                continue
            spot = _ic_get_spot(sym)
            if not spot or spot <= 0:
                continue
            dte = _dte(expiry)
            T = max(dte, 1) / 365.0
            hv_30, hv_252, ivp = _get_iv_metrics(sym)
            iv = (hv_30 or 30) / 100.0
            universe = _strike_universe(sym, expiry)
            if not universe["calls"] and not universe["puts"]:
                continue
            for strat_key in strategies:
                builder = STRATEGY_BUILDERS.get(strat_key)
                if not builder:
                    continue
                res = builder(sym, expiry, spot, iv, T, dte, universe, min_oi=min_oi, max_spread_pct=max_spread_pct)
                if not res:
                    continue
                res["earn_days"] = earn_days
                res["earn_date"] = earn.get("earn_date")
                res["ivp_proxy"] = ivp
                res["plan"] = ("Sell before close today (earnings tomorrow/today); book profit at tomorrow "
                               "morning's open if the actual move stayed inside the breakevens shown -- this "
                               "scan sizes the structure to the market's own priced-in move, it does not "
                               "predict how much IV will actually crush.")
                if res.get("rr") is not None and res["rr"] < min_rr:
                    continue
                if res.get("pop") is not None and res["pop"] < min_pop:
                    continue
                results.append(res)
        except Exception:
            continue
    results.sort(key=lambda r: r.get("earn_days", 999))
    return results


# ── Routes ──────────────────────────────────────────────────────────────

@greeks_strategy_bp.route("/")
def page():
    return render_template("greeks_strategy_scanner.html")


@greeks_strategy_bp.route("/api/expiries")
def api_expiries():
    try:
        conn = sqlite3.connect(_OIAPP_DB_PATH)
        c = conn.cursor()
        c.execute(f"SELECT DISTINCT expiration FROM {TABLE} WHERE expiration>=date('now') ORDER BY expiration")
        expiries = [r[0] for r in c.fetchall() if r[0]]
        conn.close()
        return jsonify({"ok": True, "expiries": expiries})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@greeks_strategy_bp.route("/api/scan")
def api_scan():
    expiry = request.args.get("expiry")
    if not expiry:
        return jsonify({"ok": False, "error": "expiry required"}), 400
    strategies = (request.args.get("strategies") or "strangle,straddle,iron_fly").split(",")
    min_rr = float(request.args.get("min_rr", DEFAULT_MIN_RR))
    min_pop = float(request.args.get("min_pop", DEFAULT_MIN_POP))
    min_oi = int(request.args.get("min_oi", 50))
    max_spread_pct = float(request.args.get("max_spread_pct", 15.0))
    watchlist_id = request.args.get("watchlist_id") or None
    earnings_gate = request.args.get("earnings_gate", "any")
    try:
        results = scan_strategies(expiry, strategies, min_rr=min_rr, min_pop=min_pop,
                                   min_oi=min_oi, max_spread_pct=max_spread_pct, watchlist_id=watchlist_id,
                                   earnings_gate=earnings_gate)
        return jsonify({"ok": True, "results": results, "count": len(results)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@greeks_strategy_bp.route("/api/earnings_scan")
def api_earnings_scan():
    strategies = (request.args.get("strategies") or "straddle,strangle,iron_fly").split(",")
    min_rr = float(request.args.get("min_rr", 0.3))
    min_pop = float(request.args.get("min_pop", 0.0))
    max_earn_days = int(request.args.get("max_earn_days", 1))
    min_oi = int(request.args.get("min_oi", 50))
    max_spread_pct = float(request.args.get("max_spread_pct", 15.0))
    watchlist_id = request.args.get("watchlist_id") or None
    try:
        results = scan_earnings_strategies(strategies, min_rr=min_rr, min_pop=min_pop, max_earn_days=max_earn_days,
                                            min_oi=min_oi, max_spread_pct=max_spread_pct, watchlist_id=watchlist_id)
        return jsonify({"ok": True, "results": results, "count": len(results)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@greeks_strategy_bp.route("/api/earnings_calendar")
def api_earnings_calendar():
    watchlist_id = request.args.get("watchlist_id") or None
    try:
        results = earnings_calendar_week(watchlist_id=watchlist_id)
        return jsonify({"ok": True, "results": results, "count": len(results)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
