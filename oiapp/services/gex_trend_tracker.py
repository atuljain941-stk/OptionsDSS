# oiapp/services/gex_trend_tracker.py
"""
GEX Trend Tracker (QQQ / SPY / IWM)
────────────────────────────────────
Intraday trend of the GEX profile for a small, session-configurable
symbol set. Built on top of the app's EXISTING GEX engine
(spy_strategies._compute_gex / _oi_rows / _pick_exp) rather than a new
DXLink-based gamma calc — same Black-Scholes gamma math, same
gamma-flip/pin/regime logic already powering the GEX Plan and the Pine
export, just called repeatedly through the day and logged.

WHAT THIS ACTUALLY GIVES YOU, HONESTLY:

  "Gamma drift" series (real, and new — this app doesn't currently
  recompute GEX against a live-moving spot through the day anywhere
  else): _compute_gex() re-run every N minutes at the CURRENT spot
  price and CURRENT time-to-expiry, against the SAME morning OI
  baseline. Isolates how the gamma-flip/pin/wall levels shift purely
  from spot moving and the day burning down toward expiry — real
  mechanical effect, not new positioning.

  What this does NOT give you: a live "fresh flow today" signal.
  _oi_rows() reads from the `options` table, which this app populates
  once per day via the scheduled fetch — same EOD-snapshot constraint
  as every other OI source discussed. Re-querying it mid-session
  returns the same frozen numbers until tomorrow's fetch, so there is
  no honest live volume-weighted "flow" series to log here without a
  genuinely new live data source (this was the DXLink-Trade-event
  design from the original standalone version of this tool — dropped
  here in favor of reusing what's already proven in this app, per your
  call to not add new GEX infrastructure). What IS shown is each
  strike's already-realized day-over-day oi_change/volume from
  _oi_rows() itself — yesterday's build, not today's live flow. Labeled
  as such in the UI, not blended into the drift number.

INTEGRATION:
  - Scheduling via unified_scheduler.register(), same dispatcher every
    other watcher in this app uses — no new scheduler process.
  - DB via oiapp.config.DB_PATH, same SQLite file as everything else.
  - No new tastytrade/DXLink dependency at all for this piece.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, render_template, request

from ..config import DB_PATH

gex_trend_bp = Blueprint("gex_trend", __name__, url_prefix="/gex-trend")

ET = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
DEFAULT_INTERVAL_SECONDS = 5 * 60

_tracked_symbols: list[str] = ["QQQ", "SPY", "IWM"]
_baselines: Dict[str, Dict[str, Any]] = {}  # symbol -> {rows, expiry, dte0, iv_atm, captured_at}


def set_tracked_symbols(symbols: list[str]) -> None:
    global _tracked_symbols
    _tracked_symbols = [s.upper() for s in symbols]


def get_tracked_symbols() -> list[str]:
    return list(_tracked_symbols)


# ── Schema ────────────────────────────────────────────────────────────────

def _ensure_table():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS gex_trend_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        ts TEXT NOT NULL,
        spot REAL,
        expiry TEXT,
        dte INTEGER,
        net_gex REAL,
        gamma_flip REAL,
        pin_strike REAL,
        regime TEXT,
        regime_strength REAL,
        baseline_captured_at TEXT
    )""")
    # Historically this table never persisted wall strikes, wall $gamma
    # magnitude, or the other Key Levels (call/put wall, breakout,
    # breakdown, max pain) -- all of it was computed fresh on every
    # request and shown live, but silently discarded rather than logged,
    # so none of it was ever recoverable for historical analysis. Adding
    # it now, migration-safe (existing rows just get NULLs for these
    # columns going back, nothing destructive) -- see _log()'s docstring
    # for exactly where each value comes from.
    cols = {r[1] for r in con.execute("PRAGMA table_info(gex_trend_log)").fetchall()}
    for col, ddl in [
        ("call_wall", "REAL"), ("put_wall", "REAL"), ("breakout", "REAL"),
        ("breakdown", "REAL"), ("max_pain", "REAL"),
        ("call_wall_gamma", "REAL"), ("put_wall_gamma", "REAL"),
    ]:
        if col not in cols:
            con.execute(f"ALTER TABLE gex_trend_log ADD COLUMN {col} {ddl}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_gex_trend_symbol_ts ON gex_trend_log(symbol, ts)")
    con.commit()
    con.close()


def _log(symbol: str, spot: float, expiry: str, dte: int, gex_info: dict, baseline_ts: str):
    """gex_info now expects the richer dict read_tick() builds (key
    levels + dealer_positioning), not just total_gex/gamma_flip/pin/
    regime -- call_wall/put_wall/breakout/breakdown/max_pain come from
    _build_trade_plan()'s "key" dict (already computed on every tick,
    just never logged before); call_wall_gamma/put_wall_gamma are looked
    up from dealer_positioning (the top-8-by-magnitude per-strike GEX
    list) by matching strike to call_wall/put_wall -- None if that exact
    strike didn't make the top-8 cut this tick, not a claim that no
    gamma exists there."""
    key = gex_info.get("key") or {}
    dealer_positioning = gex_info.get("dealer_positioning") or []

    def _gamma_at_strike(strike):
        if strike is None:
            return None
        for d in dealer_positioning:
            if d.get("strike") == strike:
                return d.get("gex")
        return None

    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT INTO gex_trend_log
           (symbol, ts, spot, expiry, dte, net_gex, gamma_flip, pin_strike,
            regime, regime_strength, baseline_captured_at,
            call_wall, put_wall, breakout, breakdown, max_pain,
            call_wall_gamma, put_wall_gamma)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            symbol, datetime.now(tz=ET).isoformat(), spot, expiry, dte,
            gex_info.get("total_gex"), gex_info.get("gamma_flip"), gex_info.get("pin_strike"),
            gex_info.get("regime"), gex_info.get("regime_strength"), baseline_ts,
            key.get("call_wall"), key.get("put_wall"), key.get("breakout"), key.get("breakdown"), key.get("max_pain"),
            _gamma_at_strike(key.get("call_wall")), _gamma_at_strike(key.get("put_wall")),
        ),
    )
    con.commit()
    con.close()


def get_trend(symbol: str, since: Optional[str] = None, limit: int = 200) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    q = "SELECT * FROM gex_trend_log WHERE symbol=?"
    params: list = [symbol.upper()]
    if since:
        q += " AND ts>=?"
        params.append(since)
    q += " ORDER BY ts ASC LIMIT ?"
    params.append(limit)
    rows = con.execute(q, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ── Full levels + trade plan (reuses the EXACT GEX Plan pipeline) ──────────
# GEX Plan's "Key Levels" card (call wall / breakout / gamma flip / spot /
# breakdown / balance-pin / max pain / put wall) and its primary/counter
# trade plan come from a specific 5-function pipeline in spy_strategies.py
# (_compute_gex -> _walls -> _compute_skew_rr -> _five_factor_score ->
# _build_trade_plan). This mirrors that exact sequence so the numbers
# shown here match GEX Plan precisely rather than approximating a subset
# of them -- the whole point of putting them side by side is that they
# should agree.

def _gex_profile_by_spot(rows: list, current_spot: float, T_days: int, iv_atm: float,
                          range_pct: float = 5.0, steps: int = 60) -> Dict[str, Any]:
    """Recreates the specific chart GEXBOT calls a "gamma profile" -- NOT
    gamma-by-strike (that's the dealer_positioning table above), this is
    total portfolio GEX recomputed at many HYPOTHETICAL spot levels using
    the SAME OI/strikes, answering "if spot were at price X, what would
    aggregate dealer GEX be" rather than "which strikes have big GEX
    right now." The zero-crossing of this curve is the gamma flip level
    (already computed elsewhere via _compute_gex's own gamma_flip, this
    should land close to the same price); the local extremes are what
    GEXBOT labels "major positive"/"major negative."

    Reuses _compute_gex() as-is, called once per hypothetical spot level
    -- no new fetch, no new math, just re-running existing Black-Scholes
    GEX math against the same rows at different spot inputs.
    """
    from ..scanners.spy_strategies import _compute_gex

    if not current_spot or current_spot <= 0:
        return {"available": False}

    lo = current_spot * (1 - range_pct / 100)
    hi = current_spot * (1 + range_pct / 100)
    step_size = (hi - lo) / max(1, steps)

    curve = []
    for i in range(steps + 1):
        level = lo + i * step_size
        try:
            g = _compute_gex(rows, level, T_days, iv_atm)
            curve.append({"spot": round(level, 2), "total_gex": g.get("total_gex") or 0.0})
        except Exception:
            continue
    if not curve:
        return {"available": False}

    zero_gamma = None
    for a, b in zip(curve, curve[1:]):
        if (a["total_gex"] <= 0) != (b["total_gex"] <= 0):
            # linear interpolation between the two straddling points for a cleaner crossing estimate
            span = b["total_gex"] - a["total_gex"]
            frac = (-a["total_gex"] / span) if span else 0
            zero_gamma = round(a["spot"] + frac * (b["spot"] - a["spot"]), 2)
            break

    major_positive = max(curve, key=lambda c: c["total_gex"])
    major_negative = min(curve, key=lambda c: c["total_gex"])

    return {
        "available": True, "curve": curve,
        "zero_gamma": zero_gamma,
        "major_positive": major_positive["spot"] if major_positive["total_gex"] > 0 else None,
        "major_negative": major_negative["spot"] if major_negative["total_gex"] < 0 else None,
        "net_gex_at_spot": next((c["total_gex"] for c in curve if abs(c["spot"] - current_spot) < step_size), None),
        "range_lo": round(lo, 2), "range_hi": round(hi, 2),
    }


def _gex_profile_playbook(profile: Dict[str, Any], spot: float, first_two_hours: Optional[Dict[str, Any]] = None,
                            dte: Optional[int] = None) -> Dict[str, Any]:
    """Turns the gamma profile shape into one of three concrete calls --
    naked long (direction), credit vertical (direction), or no trade --
    matched to the two structures actually available (per the person's
    own constraint: credit verticals or naked long options, nothing
    else). Not a third abstract "bullish/bearish" label on top of what
    the chart already shows; this is the specific instrument choice that
    follows FROM the shape, stated as a reason someone could act on.

    Core logic, stated plainly:
    - Positive gamma at spot (dealers stabilizing there) -> dealer
      hedging dampens moves -> theta/range-bound edge -> favors SELLING
      premium (credit vertical), not owning it.
    - Negative gamma at spot AND the curve is still getting worse at the
      edge of the scanned range (no interior floor found) -> nothing
      mechanically stops the move from here -> favors OWNING convexity
      (naked long) in that direction; a credit vertical sold against an
      accelerating, unsupported move risks getting blown through fast.
    - Negative gamma but a real interior floor/ceiling shows up nearby ->
      still amplifying, but there IS a level -- lower conviction either
      way, closer to a coin flip than the other two cases.
    - Spot sitting right on top of zero gamma, or no clear open-session
      signal to lean on -> explicitly no trade, not a forced pick.
    """
    if not profile or not profile.get("available") or not spot:
        return {"available": False}

    zero_gamma = profile.get("zero_gamma")
    curve = profile.get("curve") or []
    if zero_gamma is None or not curve:
        return {"available": True, "call": "no_trade",
                "reason": "No clean gamma-flip crossing found in the scanned range -- nothing structural to lean on here."}

    dist_pct = abs(spot - zero_gamma) / spot * 100 if spot else 0
    positive_regime = spot > zero_gamma

    # Same DTE-scaling Peak Gamma Zone already applies, now applied here
    # too -- this whole framework (the gamma profile shape, the amplifying/
    # stabilizing read driving every call below) rests on dealer hedging
    # pressure, and that pressure is mechanically weaker the further out
    # expiry sits. Leaving this playbook's confidence flat regardless of
    # DTE was an inconsistency: the SAME underlying mechanic already gets
    # scaled elsewhere in this module, it just wasn't wired in here too.
    dte_conviction = None
    if dte is not None:
        if dte <= 2:
            dte_conviction = {"level": "high", "dte": dte,
                "text": f"{dte} DTE -- gamma is sharply peaked at this horizon (leptokurtic), so the dealer-"
                        f"hedging pressure driving this whole read is at its strongest. This is the regime "
                        f"this framework is built around."}
        elif dte <= 10:
            dte_conviction = {"level": "medium", "dte": dte,
                "text": f"{dte} DTE -- real dealer-hedging pressure, but softer than 0DTE, not the sharp "
                        f"leptokurtic peak. The directional read below still applies, but treat it as lower-"
                        f"conviction than the same shape would be at 0-2 DTE, and don't expect same-day "
                        f"resolution the way a 0DTE setup implies -- there's no same-session theta cliff "
                        f"forcing anyone to close by the close."}
        else:
            dte_conviction = {"level": "low", "dte": dte,
                "text": f"{dte} DTE -- at this horizon gamma is close to flat; the dealer-hedging mechanic "
                        f"this whole framework is built on barely applies yet. The shape below is real, but "
                        f"weak. This is closer to Swing Positioning Scanner's territory than a live GEX read "
                        f"-- worth checking that page's OI-and-volume cross-reference instead of leaning "
                        f"heavily on this one at this DTE."}

    if dist_pct < 0.3:
        return {"available": True, "call": "no_trade",
                "reason": f"Spot (${spot:.2f}) is sitting right on the zero-gamma line (${zero_gamma:.2f}, "
                          f"{dist_pct:.2f}% away) -- too close to the flip to lean either way mechanically. "
                          f"A small move could flip the whole regime; wait for it to resolve one direction "
                          f"before picking a structure."}

    if positive_regime:
        near_wall = profile.get("major_positive")
        direction = "put" if (near_wall and near_wall < spot) else "call"
        return {
            "available": True, "call": "credit_vertical", "direction": direction, "positive_regime": True,
            "reason": f"Spot is above the zero-gamma line (${zero_gamma:.2f}) -- dealers are net stabilizing "
                      f"here, hedging flow dampens moves rather than reinforcing them. That's a theta/range-"
                      f"bound edge, not a trending one: favors selling a {direction} credit vertical over "
                      f"owning premium outright, since a big move isn't the mechanically favored outcome "
                      f"from this level.",
            "iron_condor_note": (f"A condor is a more natural fit in this regime than in an amplifying one -- "
                                  f"spot itself is already on the stabilizing side of ${zero_gamma:.2f}. Still "
                                  f"worth checking both short strikes against that line individually: the side "
                                  f"that stays above it keeps the same dampening support driving this whole "
                                  f"read; a strike placed back across ${zero_gamma:.2f} on the far side gives "
                                  f"that leg up the same protection this level provides right now."),
            "dte_conviction": dte_conviction,
        }

    # Negative regime -- check whether the curve found an interior floor/
    # ceiling or was still moving toward the edge of the scanned range
    # (no floor found = nothing mechanically stops it from here).
    extreme_spot = profile.get("major_negative") if spot < zero_gamma else profile.get("major_positive")
    range_lo, range_hi = profile.get("range_lo"), profile.get("range_hi")
    at_edge = False
    if extreme_spot is not None and range_lo is not None and range_hi is not None:
        span = range_hi - range_lo
        if span > 0:
            at_edge = (extreme_spot - range_lo) / span < 0.08 or (range_hi - extreme_spot) / span < 0.08

    direction = "put" if spot < zero_gamma else "call"
    session_note = ""
    if first_two_hours and first_two_hours.get("phase") == "in_window":
        session_note = " Still inside the first-two-hours window, so this read carries more weight than it would later in the day."

    # Someone running credit verticals as their only structure (not naked
    # longs) still needs an answer even in the naked-long-favored case --
    # not a second competing recommendation, a note on how to place it if
    # they go that route anyway. The zero-gamma line itself is the thing
    # that matters: a short strike placed on the STABILIZING side of it
    # gets real structural help (dealer hedging caps a move before it
    # gets there); one placed on the amplifying side is naked into the
    # same unprotected territory the naked-long idea exists to exploit,
    # just from the other side of the trade.
    opposite_side = "call" if direction == "put" else "put"
    alt_reason = (f"If a credit vertical is preferred over a naked long here: the zero-gamma line "
                  f"(${zero_gamma:.2f}) is the level that actually matters for strike placement, not just "
                  f"direction. A short strike placed on the far side of ${zero_gamma:.2f} from spot picks up "
                  f"real structural help -- that's the stabilizing zone, where dealer hedging tends to cap a "
                  f"move before it reaches the strike. A short strike placed between spot and ${zero_gamma:.2f} "
                  f"is still sitting in the amplifying zone with no such protection -- selling premium there "
                  f"carries the same blow-through risk the naked-long idea is built to capture, just from the "
                  f"short side instead of the long side.")

    # An iron condor is a fundamentally different bet than either of the
    # above -- it wants price to stay BETWEEN two short strikes, which is
    # mechanically the opposite of what an amplifying regime with no
    # interior floor implies (moves reinforced, not dampened). Worse, its
    # two sides aren't symmetric here: whichever side sits on the far
    # side of zero_gamma from spot gets real dealer-hedging support, the
    # side that stays between spot and zero_gamma doesn't. A standard
    # equal-width condor prices both sides as if they were equally safe;
    # they aren't on this chart.
    condor_reason = (f"An iron condor is a bet on price staying between two strikes -- structurally the "
                      f"opposite of what this regime implies (amplifying, moves reinforced not dampened). It "
                      f"also has an asymmetry problem specific to this chart: whichever side lands past "
                      f"${zero_gamma:.2f} (the stabilizing zone) gets real dealer-hedging support; the side "
                      f"that stays between spot and ${zero_gamma:.2f} doesn't -- it's sitting in the same "
                      f"unprotected amplifying zone the naked-long idea is built around. A standard equal-"
                      f"width condor treats both sides as equally safe, which isn't true here -- the "
                      f"unprotected side needs meaningfully more room than the protected side, not matching "
                      f"widths.")

    # Debit vertical: the actual risk-defined SIBLING of the naked long
    # (same directional thesis, capped cost/reward), unlike the credit
    # vertical and condor above, which are different theses entirely
    # (range-bound / mean-reverting). Only real caveat is strike
    # selection: a short leg placed too close to the long leg caps away
    # the very convexity the naked-long idea exists to capture in an
    # unsupported, accelerating move.
    debit_reason = (f"The direct risk-defined version of the naked long itself: buy the {direction}, sell a "
                     f"further OTM {direction} against it. Same directional thesis as above, same "
                     f"zero-gamma logic applies to WHERE it can run to -- just caps the cost and the max "
                     f"payout instead of leaving the full premium at risk. The one thing that actually "
                     f"matters here: don't set the short leg too close to the long one. This setup's whole "
                     f"case is an accelerating, unsupported move -- a tight debit spread caps away most of "
                     f"that convexity right when it would matter most, same problem a credit vertical or "
                     f"condor has, just on the payoff side instead of the risk side.")

    if at_edge:
        return {
            "available": True, "call": "naked_long", "direction": direction, "positive_regime": False,
            "reason": f"Spot is below the zero-gamma line (${zero_gamma:.2f}) and total dealer GEX keeps "
                      f"getting more negative all the way to the edge of the scanned range -- no interior "
                      f"floor found nearby. Nothing mechanical stops a move lower from here, which is exactly "
                      f"the setup where owning convexity (a naked long {direction}) captures an accelerating "
                      f"move better than a credit vertical, which risks getting blown through fast in this "
                      f"kind of unsupported negative-gamma stretch.{session_note}",
            "alternative": {"structure": "credit_vertical", "direction": opposite_side, "reason": alt_reason},
            "iron_condor_note": condor_reason,
            "debit_vertical_note": debit_reason,
            "dte_conviction": dte_conviction,
        }
    else:
        return {
            "available": True, "call": "naked_long", "direction": direction, "positive_regime": False,
            "confidence": "lower",
            "reason": f"Spot is below the zero-gamma line (${zero_gamma:.2f}) -- amplifying regime -- but the "
                      f"curve does show a level nearby (${extreme_spot:.2f}) rather than accelerating "
                      f"unchecked. Still leans toward owning convexity over selling it here, but this is "
                      f"closer to a coin flip than a clean setup -- size accordingly.{session_note}",
            "alternative": {"structure": "credit_vertical", "direction": opposite_side, "reason": alt_reason},
            "iron_condor_note": condor_reason,
            "debit_vertical_note": debit_reason,
            "dte_conviction": dte_conviction,
        }


def _compute_full_levels(symbol: str, rows: list, expiry: str, spot: float,
                          dte: int, iv_atm: float) -> Dict[str, Any]:
    from ..scanners.spy_strategies import (
        _compute_gex, _walls, _compute_skew_rr, _five_factor_score,
        _build_trade_plan, _oi_change_filter_context, _finite_number,
    )
    import math as _math

    T_days = max(1, dte)
    gex_info = dict(_compute_gex(rows, spot, T_days, iv_atm) or {})
    gex_info["gamma_flip"] = _finite_number(gex_info.get("gamma_flip")) or spot
    gex_info["pin_strike"] = _finite_number(gex_info.get("pin_strike")) or spot
    gex_info["max_pain"] = _finite_number(gex_info.get("max_pain")) or gex_info["pin_strike"]

    # Single-expiry PCR (GEX Plan uses a multi-expiry PCR across the
    # nearest 5 expiries; approximated here from this one expiry's rows
    # to avoid N extra _oi_rows() calls on every tick -- close enough for
    # the five-factor score's PCR component, not worth the extra query
    # load at a 1-5 min refresh interval).
    total_calls = sum(int(r.get("oi") or 0) for r in rows if r.get("type") == "call")
    total_puts = sum(int(r.get("oi") or 0) for r in rows if r.get("type") == "put")
    pcr = round(total_puts / max(1, total_calls), 3)

    skew_rr = _compute_skew_rr(rows, spot, T_days, iv_atm)
    sigma_1d = round(spot * (iv_atm / 100) / _math.sqrt(252), 2) if spot and iv_atm else 0.0

    oi_change_filter = _oi_change_filter_context(symbol, rows, expiry=expiry, source="gex_trend_tracker")
    walls = dict(_walls(rows, spot, gex_info=gex_info, sigma_1d=sigma_1d, oi_change_filter=oi_change_filter) or {})
    walls["support"] = _finite_number(walls.get("support")) or round(spot * 0.985, 2)
    walls["resistance"] = _finite_number(walls.get("resistance")) or round(spot * 1.015, 2)

    score, confidence, regime_label, factors = _five_factor_score(
        gex_info.get("total_gex"), pcr, skew_rr, spot,
        gex_info["pin_strike"], gex_info["gamma_flip"], rows,
        gex_ratio=gex_info.get("gex_ratio"), max_pain=gex_info.get("max_pain"),
        dte=dte, vix_level=None, gex_strength_pct=gex_info.get("regime_strength"),
    )

    plan = _build_trade_plan(spot, gex_info, walls, {}, score, sigma_1d)

    # Dealer/market-maker positioning by strike: _compute_gex already
    # builds this (gex_per_strike), it just wasn't surfaced anywhere.
    # Sign convention per _compute_gex's own docstring: dealers short
    # calls -> +GEX at that strike (stabilizing, dealer buys dips/sells
    # rips there), dealers long puts -> -GEX (amplifying, dealer's hedge
    # reinforces the move rather than dampening it). Top N by magnitude =
    # where dealer exposure actually concentrates, not every strike.
    gex_per_strike = gex_info.get("gex_per_strike") or {}
    dealer_positioning = []
    for k_str, gex_val in gex_per_strike.items():
        try:
            k = float(k_str)
        except Exception:
            continue
        dealer_positioning.append({
            "strike": k, "gex": gex_val,
            "effect": "stabilizing" if gex_val > 0 else "amplifying",
            "pct_from_spot": round((k - spot) / spot * 100, 2) if spot else None,
        })
    dealer_positioning.sort(key=lambda x: abs(x["gex"]), reverse=True)
    dealer_positioning = dealer_positioning[:8]

    # Peak gamma zone: the specific mechanic Fredy Sarmiento describes --
    # a long option holder's gamma (and therefore convexity) peaks exactly
    # at-the-money (delta ~0.5), so once spot reaches a strike carrying
    # large pre-existing OI, that position is at its most convex point.
    # Beyond it, gains stop compounding the same way, which is exactly
    # why it's the natural point for the holder to sell back to the
    # dealer -- and when they do, the dealer's offsetting futures hedge
    # becomes unnecessary and gets unwound (sold off if it was hedging a
    # long call, bought back if hedging a long put), which is the
    # mechanical pressure toward reversal.
    #
    # dealer_positioning above already reflects this partially (gex_per_strike
    # is gamma-weighted, and gamma is peaked ATM by construction), but it's
    # shown as a ranked table without flagging WHEN spot is actually
    # sitting on top of one of these strikes right now, or that the effect
    # is sharply DTE-dependent -- a leptokurtic (near-vertical) gamma peak
    # with hours left, a soft, barely-there peak a month out. This makes
    # that explicit instead of leaving it for the table to imply.
    ZONE_TOLERANCE_PCT = 0.35  # how close "at" a strike needs to be, in %
    peak_gamma_zone = None
    for dp in dealer_positioning:
        if dp.get("pct_from_spot") is not None and abs(dp["pct_from_spot"]) <= ZONE_TOLERANCE_PCT:
            if dte is not None and dte <= 2:
                dte_note = "0-2 DTE — the gamma peak here is sharp (leptokurtic), so this effect is at its strongest."
            elif dte is not None and dte <= 10:
                dte_note = f"{dte} DTE — the peak is real but softer than 0DTE; less violent, still relevant."
            else:
                dte_note = f"{dte} DTE — at this horizon the gamma curve is close to flat, this mechanic barely applies yet."
            side = "call" if dp["gex"] > 0 else "put"
            peak_gamma_zone = {
                "strike": dp["strike"], "gex": dp["gex"], "side": side,
                "pct_from_spot": dp["pct_from_spot"], "dte_note": dte_note,
                "note": f"Spot is sitting right at ${dp['strike']:.2f} — a strike carrying large pre-existing "
                        f"{side} exposure. Gamma is at (or very near) its peak for any position opened here, "
                        f"making this the natural profit-taking point for a long holder; if they sell back to "
                        f"the dealer, the dealer's offsetting hedge unwinds, pressuring price the other way. {dte_note}",
            }
            break  # only the nearest/strongest match -- one zone, not a list

    return {
        "spot": spot, "levels": plan.get("levels", []), "key": plan.get("key", {}),
        "score": score, "confidence": confidence, "regime_label": regime_label,
        "plan": {"primary": plan.get("primary", {}), "counter": plan.get("counter", {})},
        "total_gex": gex_info.get("total_gex"),
        "skew_rr": skew_rr,
        "dealer_positioning": dealer_positioning,
        "peak_gamma_zone": peak_gamma_zone,
    }


def _levels_dict(full_levels: Dict[str, Any]) -> Dict[str, float]:
    """Flattens the level list into {label: price} for easy diffing."""
    return {lv["label"]: lv["price"] for lv in (full_levels or {}).get("levels", []) if lv.get("price") is not None}


def _diff_levels(baseline_levels: Dict[str, Any], current_levels: Dict[str, Any],
                  move_threshold_pct: float = 0.3) -> list:
    """Flags any level that moved more than move_threshold_pct since
    baseline, or where spot has crossed it since baseline (the more
    actionable of the two -- a level spot has actually traded through is
    a bigger deal than one that merely drifted a bit from decay)."""
    base_map = _levels_dict(baseline_levels)
    cur_map = _levels_dict(current_levels)
    base_spot = (baseline_levels or {}).get("spot")
    cur_spot = (current_levels or {}).get("spot")
    changes = []
    for label, cur_price in cur_map.items():
        base_price = base_map.get(label)
        if base_price is None or not base_price:
            continue
        pct_change = (cur_price - base_price) / base_price * 100
        crossed = None
        if base_spot and cur_spot and label not in ("SPOT",):
            was_above = base_spot > base_price
            now_above = cur_spot > cur_price
            if was_above != now_above:
                crossed = "up" if now_above else "down"
        if abs(pct_change) >= move_threshold_pct or crossed:
            changes.append({
                "label": label, "baseline_price": base_price, "current_price": cur_price,
                "pct_change": round(pct_change, 2), "crossed": crossed,
            })
    return changes


def _build_guidance(baseline_full: Dict[str, Any], current_full: Dict[str, Any], changes: list) -> str:
    regime_changed = (baseline_full or {}).get("regime_label") != (current_full or {}).get("regime_label")
    crossed = [c for c in changes if c.get("crossed")]
    parts = []
    if regime_changed:
        parts.append(f"Regime shifted from {(baseline_full or {}).get('regime_label')} at the open to "
                      f"{(current_full or {}).get('regime_label')} now -- the dealer-hedging backdrop for "
                      f"0DTE has actually changed today, not just drifted.")
    if crossed:
        cross_desc = "; ".join(f"{c['label']} (${c['baseline_price']:.2f}), crossed {c['crossed']}" for c in crossed)
        parts.append(f"Spot has crossed since the open: {cross_desc}. Levels that get traded through "
                      f"intraday are a stronger signal than ones spot just drifted near.")
    primary = (current_full or {}).get("plan", {}).get("primary", {})
    if primary.get("trigger"):
        parts.append(f"Current live read ({primary.get('direction', '')}): {primary.get('trigger', '')}")
    if not parts:
        parts.append("No major level crossings or regime change since the open -- current plan is "
                      "largely unchanged from the morning baseline.")
    return " ".join(parts)



def _session_commentary(symbol: str, spot: float) -> Dict[str, Any]:
    """Plain-English read of today's actual price action so far, separate
    from the GEX-based guidance above -- this is "what has price actually
    done today" (today's own open/high/low/range/trend), not options
    positioning. Complements the levels/guidance rather than replacing
    them: GEX tells you where dealers are exposed, this tells you what
    the tape has actually done relative to those levels today.
    Uses today's 5-min bars (get_history_cached, period=1d/interval=5m --
    already-cached, already-throttled, same helper used elsewhere in this
    app) rather than a new fetch path.
    """
    from .market import get_history_cached
    try:
        df = get_history_cached(symbol, period="1d", interval="5m")
    except Exception:
        df = None
    if df is None or df.empty:
        return {"available": False, "note": "No intraday bars available for today yet."}

    day_open = float(df["Open"].iloc[0])
    day_high = float(df["High"].max())
    day_low = float(df["Low"].min())
    last_close = float(df["Close"].iloc[-1])
    cur = spot or last_close

    day_range = day_high - day_low
    range_pos_pct = round((cur - day_low) / day_range * 100, 1) if day_range > 0 else 50.0
    pct_from_open = round((cur - day_open) / day_open * 100, 2) if day_open else 0.0

    # Simple trend read: compare the average close of the first third of
    # today's bars vs the last third -- not a sophisticated model, just
    # enough to distinguish "ground higher/lower all day" from "chopped
    # around the open" without needing a full regression.
    closes = df["Close"].tolist()
    n = len(closes)
    trend = "choppy / range-bound"
    if n >= 6:
        third = max(1, n // 3)
        first_avg = sum(closes[:third]) / third
        last_avg = sum(closes[-third:]) / third
        drift_pct = (last_avg - first_avg) / first_avg * 100 if first_avg else 0.0
        if drift_pct > 0.15:
            trend = "grinding higher through the session"
        elif drift_pct < -0.15:
            trend = "grinding lower through the session"

    near_high = range_pos_pct >= 85
    near_low = range_pos_pct <= 15

    parts = [
        f"{symbol} opened today at ${day_open:.2f}, has ranged ${day_low:.2f}-${day_high:.2f} "
        f"(${day_range:.2f} wide), and is {trend}.",
        f"Currently ${cur:.2f}, {pct_from_open:+.2f}% from the open, sitting at the "
        f"{range_pos_pct:.0f}th percentile of today's range" +
        (" -- near the highs" if near_high else " -- near the lows" if near_low else " -- mid-range") + ".",
    ]

    return {
        "available": True, "day_open": round(day_open, 2), "day_high": round(day_high, 2),
        "day_low": round(day_low, 2), "day_range": round(day_range, 2),
        "pct_from_open": pct_from_open, "range_position_pct": range_pos_pct,
        "trend": trend, "commentary": " ".join(parts),
    }


def _first_two_hours_read(symbol: str, current_full: Dict[str, Any], guidance: str,
                            session: Dict[str, Any], changes: list) -> Dict[str, Any]:
    """The specific "how is the market shaping up" synthesis Fredy
    Sarmiento's framework is built around -- institutions position in
    roughly the first two hours, so that window is when the highest-
    conviction reads happen. Combines pieces this module already computes
    (dealer positioning ratio, session commentary, crossed levels, peak
    gamma zone) into one time-aware headline instead of leaving the
    person to mentally combine four separate panels themselves.
    """
    now = datetime.now(tz=ET)
    market_open_dt = now.replace(hour=9, minute=30, second=0, microsecond=0)
    minutes_since_open = (now - market_open_dt).total_seconds() / 60

    if now.weekday() >= 5:
        return {"phase": "closed", "text": "Market closed (weekend) -- no session to read."}
    if now.time() < MARKET_OPEN:
        return {"phase": "pre_market", "text": "Before the open -- the first-two-hours window "
                "(9:30-11:30 ET) hasn't started yet."}
    if now.time() > MARKET_CLOSE:
        return {"phase": "after_hours", "text": "After the close -- today's first-two-hours window has "
                "long since passed."}

    dealer_positioning = current_full.get("dealer_positioning") or []
    amplifying = sum(1 for d in dealer_positioning if (d.get("gex") or 0) < 0)
    stabilizing = sum(1 for d in dealer_positioning if (d.get("gex") or 0) > 0)
    total_nearby = amplifying + stabilizing

    regime_bits = []
    if total_nearby:
        if amplifying > stabilizing:
            regime_bits.append(
                f"{amplifying} of {total_nearby} nearby strikes are Amplifying vs {stabilizing} Stabilizing -- "
                f"dealers are net short gamma close to spot, meaning moves that get going are more likely to "
                f"extend/reinforce than get dampened. Favors trading WITH momentum through a confirmed level "
                f"rather than fading small moves back toward spot.")
        elif stabilizing > amplifying:
            regime_bits.append(
                f"{stabilizing} of {total_nearby} nearby strikes are Stabilizing vs {amplifying} Amplifying -- "
                f"dealers are net long gamma close to spot, meaning moves toward a wall are more likely to get "
                f"absorbed/rejected than extend. Favors fading moves into a level rather than chasing a breakout.")
        else:
            regime_bits.append(f"Dealer effect is split evenly ({amplifying} amplifying / {stabilizing} "
                                f"stabilizing) near spot -- no clear mechanical lean either way right now.")

    peak_zone = current_full.get("peak_gamma_zone")
    if peak_zone:
        regime_bits.append(peak_zone.get("note", ""))

    crossed = [c for c in changes if c.get("crossed")]
    if crossed:
        regime_bits.append(f"Since the open, spot has crossed: " +
                            "; ".join(f"{c['label']} (${c['baseline_price']:.2f})" for c in crossed) + ".")

    if session.get("available"):
        regime_bits.append(session.get("commentary", ""))

    if minutes_since_open <= 120:
        phase = "in_window"
        header = (f"{int(minutes_since_open)} min into the session, still inside the first-two-hours "
                  f"window (closes at 11:30 ET) -- per the institutional-positioning framework, this is "
                  f"the highest-conviction read of the day.")
    else:
        phase = "past_window"
        header = (f"First-two-hours window closed {int(minutes_since_open - 120)} min ago -- the highest-"
                  f"conviction setups per that framework were earlier; from here, be more selective about "
                  f"new entries, not more aggressive.")

    return {"phase": phase, "minutes_since_open": round(minutes_since_open),
            "amplifying_count": amplifying, "stabilizing_count": stabilizing,
            "text": header + " " + " ".join(b for b in regime_bits if b)}


def _high_volume_strikes(rows: list, spot: float, top_n: int = 8, far_otm_threshold_pct: float = 3.0) -> list:
    """Top strikes by volume (not OI) -- same daily-snapshot granularity
    as everything else here (_oi_rows' vol column, not live intraday
    ticks), but a genuinely different signal from OI: OI is existing
    positions, volume is how much traded on the snapshot day. A strike
    with heavy volume well OTM specifically stands out because near-the-
    money volume is just normal trading -- volume concentrated far from
    spot is more likely a same-day directional bet or a tail hedge than
    routine activity, so those get flagged separately.
    Also reports vol/OI ratio: high volume against thin existing OI
    means most of that volume was NEW positioning today, not existing
    holders trading around a position they already had.
    """
    if not rows or not spot:
        return []
    out = []
    for r in rows:
        vol = int(r.get("vol") or 0)
        if vol <= 0:
            continue
        strike = float(r.get("strike") or 0)
        oi = int(r.get("oi") or 0)
        if not strike:
            continue
        pct_otm = (strike - spot) / spot * 100  # positive = OTM call direction, negative = OTM put direction
        out.append({
            "strike": strike, "type": r.get("type"), "volume": vol, "oi": oi,
            "vol_oi_ratio": round(vol / max(1, oi), 2),
            "pct_from_spot": round(pct_otm, 2),
            "far_otm": abs(pct_otm) >= far_otm_threshold_pct,
        })
    out.sort(key=lambda x: x["volume"], reverse=True)
    return out[:top_n]


def _within_market_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(tz=ET)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def capture_baseline(symbol: str) -> Optional[Dict[str, Any]]:
    """Once per session: freeze the OI rows + expiry + starting IV proxy
    AND the full start-of-day levels/plan snapshot -- this is the frozen
    "left column" that stays fixed all session, computed once here rather
    than re-derived later from live data."""
    from ..scanners.spy_strategies import _oi_rows, _future_exps, _pick_exp, _compute_ta
    from .market import get_spot_snapshot

    exps = _future_exps(symbol)
    expiry, dte = _pick_exp(exps, min_dte=0, max_dte=45, fallback_index=0)
    if not expiry:
        return None
    rows = _oi_rows(symbol, expiry)
    if not rows:
        return None
    try:
        ta = _compute_ta(symbol) or {}
        iv_atm = float(ta.get("iv_atm") or 16.0)
    except Exception:
        iv_atm = 16.0

    spot_snap = get_spot_snapshot(symbol) or {}
    spot0 = float(spot_snap.get("price") or spot_snap.get("spot") or spot_snap.get("last") or 0)

    baseline = {
        "rows": rows, "expiry": expiry, "dte0": dte, "iv_atm": iv_atm,
        "captured_at": datetime.now(tz=ET).isoformat(),
        "spot0": spot0,
    }
    if spot0:
        try:
            baseline["levels_snapshot"] = _compute_full_levels(symbol, rows, expiry, spot0, dte, iv_atm)
        except Exception:
            baseline["levels_snapshot"] = None
    _baselines[symbol] = baseline
    return baseline


def _dte_now(expiry: str) -> int:
    from datetime import date
    try:
        d = date.fromisoformat(str(expiry)[:10])
        return max(1, (d - date.today()).days)
    except Exception:
        return 1


def read_tick(symbol: str, log: bool = True) -> Dict[str, Any]:
    """Two snapshots side by side: the FROZEN start-of-day levels (left
    column, computed once in capture_baseline and never recomputed) and
    the CURRENT live levels (right column, same OI baseline but live spot
    + live time-to-expiry) -- plus a diff flagging which levels moved
    meaningfully or got crossed by spot since the open, and a plain-
    English 0DTE read combining both."""
    from .market import get_spot_snapshot

    baseline = _baselines.get(symbol) or capture_baseline(symbol)
    if not baseline:
        return {"symbol": symbol, "error": "No option chain OI rows available yet for this symbol/expiry."}

    spot_snap = get_spot_snapshot(symbol) or {}
    spot = float(spot_snap.get("price") or spot_snap.get("spot") or spot_snap.get("last") or 0)
    if not spot:
        return {"symbol": symbol, "error": "No live spot price available."}

    dte_now = _dte_now(baseline["expiry"])
    current_full = _compute_full_levels(symbol, baseline["rows"], baseline["expiry"], spot, dte_now, baseline["iv_atm"])
    baseline_full = baseline.get("levels_snapshot")

    changes = _diff_levels(baseline_full, current_full) if baseline_full else []
    guidance = _build_guidance(baseline_full, current_full, changes) if baseline_full else \
        "Baseline levels weren't available at capture time (no spot price then) -- showing live read only."

    result = {
        "symbol": symbol, "spot": spot, "expiry": baseline["expiry"], "dte": dte_now,
        "baseline_captured_at": baseline["captured_at"],
        "baseline": baseline_full,
        "current": current_full,
        "changes": changes,
        "guidance": guidance,
        # Kept for backward-compat with the trend log / chart below --
        # same fields as before, sourced from current_full now.
        "net_gex": current_full.get("total_gex"),
        "gamma_flip": (current_full.get("key") or {}).get("gamma_flip"),
        "pin_strike": (current_full.get("key") or {}).get("pin") or (current_full.get("key") or {}).get("balance"),
        "regime": current_full.get("regime_label"),
        "regime_strength": current_full.get("confidence"),
        # yesterday's realized OI build per strike, from the same baseline rows --
        # NOT live intraday flow, see module docstring.
        "top_oi_builds": sorted(
            ({"strike": r["strike"], "type": r["type"], "oi_change": r.get("oi_change", 0)}
             for r in baseline["rows"]),
            key=lambda x: abs(x["oi_change"] or 0), reverse=True,
        )[:5],
        "high_volume_strikes": _high_volume_strikes(baseline["rows"], spot),
        "session": _session_commentary(symbol, spot),
    }
    result["first_two_hours"] = _first_two_hours_read(symbol, current_full, guidance, result["session"], changes)
    result["gex_profile"] = _gex_profile_by_spot(baseline["rows"], spot, dte_now, baseline["iv_atm"])
    result["gex_playbook"] = _gex_profile_playbook(result["gex_profile"], spot, result["first_two_hours"], dte=dte_now)
    if log:
        try:
            gex_info_for_log = {"total_gex": None, "gamma_flip": result["gamma_flip"],
                                 "pin_strike": result["pin_strike"], "regime": result["regime"],
                                 "regime_strength": result["regime_strength"],
                                 "key": current_full.get("key"), "dealer_positioning": current_full.get("dealer_positioning")}
            _log(symbol, spot, baseline["expiry"], dte_now, gex_info_for_log, baseline["captured_at"])
        except Exception:
            pass
    return result


def run_all_ticks():
    """Called by the scheduler every DEFAULT_INTERVAL_SECONDS. Loops all
    tracked symbols; one symbol failing doesn't block the others."""
    if not _within_market_hours():
        return
    for symbol in get_tracked_symbols():
        try:
            read_tick(symbol)
        except Exception as e:
            print(f"[gex_trend_tracker] tick failed for {symbol}: {e}")


def register_scheduler_job(interval_seconds: int = DEFAULT_INTERVAL_SECONDS):
    from . import unified_scheduler
    from .job_registry import register_job
    # Same reasoning as live_chain_tracker.py's identical addition: this
    # is what makes the job show up in Scheduler Hub at all. The dispatcher
    # already calls job_registry's mark_run() automatically after every
    # tick regardless -- that last-run state existed already, it just had
    # no registered metadata to attach to, so it never appeared anywhere.
    register_job(
        "gex_trend_tracker", "GEX Trend Tracker", "Start-of-day baseline + live GEX tick capture (SPY/QQQ/IWM)",
        kind="interval", default_schedule={"interval_min": max(1, int(interval_seconds / 60))},
        group="Live Capture", run_now_fn=run_all_ticks, editable=True,
    )
    # NOT low_priority -- see live_chain_tracker.py's identical fix for the
    # exact reasoning: that flag skips the job entirely (not just delays
    # it) for as long as any Scanner Builder query is active anywhere in
    # the app, which starves a time-sensitive live-read job the person is
    # actively depending on, not just a background backfill that can wait.
    return unified_scheduler.register(
        "gex_trend_tracker", run_all_ticks,
        interval_seconds=interval_seconds, low_priority=False,
    )


# ── Routes ──────────────────────────────────────────────────────────────

@gex_trend_bp.route("/api/interval", methods=["GET", "POST"])
def api_interval():
    """Reads/writes the SAME live schedule override the Scheduler Hub page
    already uses for every registered job -- unified_scheduler's dispatcher
    already prefers job_registry's stored interval_min over the interval
    passed at registration time, so this doesn't need a new mechanism,
    just a convenient control for this one job right on this page instead
    of making you go find it in the Scheduler Hub symbol list."""
    from .job_registry import get_schedule, set_schedule
    try:
        if request.method == "POST":
            minutes = float((request.json or {}).get("minutes") or 5)
            minutes = max(1.0, min(60.0, minutes))
            set_schedule("gex_trend_tracker", {"interval_min": minutes})
            return jsonify({"interval_min": minutes})
        sched = get_schedule("gex_trend_tracker")
        return jsonify({"interval_min": sched.get("interval_min", DEFAULT_INTERVAL_SECONDS / 60)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@gex_trend_bp.route("/")
def page():
    return render_template("gex_trend.html")


@gex_trend_bp.route("/api/config", methods=["GET", "POST"])
def api_config():
    try:
        if request.method == "POST":
            symbols = (request.json or {}).get("symbols") or []
            if not symbols:
                return jsonify({"error": "symbols list required"}), 400
            set_tracked_symbols(symbols)
            return jsonify({"tracked_symbols": get_tracked_symbols()})
        return jsonify({"tracked_symbols": get_tracked_symbols()})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@gex_trend_bp.route("/api/tick")
def api_tick():
    """Manual on-demand read (also what the scheduler calls internally) --
    lets the page show a fresh value immediately on load/refresh instead
    of waiting for the next scheduled tick."""
    from ..scanners.spy_strategies import _json_safe
    try:
        symbol = (request.args.get("symbol") or "SPY").upper()
        result = read_tick(symbol)
        return jsonify(_json_safe(result))
    except Exception as e:
        # Any uncaught exception here previously fell through to Flask's
        # default HTML error page -- which the page's fetch().json() call
        # then failed to parse with a confusing "Unexpected token '<'"
        # error instead of showing what actually went wrong. Always
        # return JSON from this route, even on failure.
        import traceback
        traceback.print_exc()
        return jsonify({"symbol": request.args.get("symbol", "?"), "error": f"{type(e).__name__}: {e}"}), 500


@gex_trend_bp.route("/api/trend")
def api_trend():
    from ..scanners.spy_strategies import _json_safe
    try:
        symbol = (request.args.get("symbol") or "SPY").upper()
        since = request.args.get("since")
        limit = int(request.args.get("limit", 200))
        return jsonify(_json_safe({"symbol": symbol, "points": get_trend(symbol, since, limit)}))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
