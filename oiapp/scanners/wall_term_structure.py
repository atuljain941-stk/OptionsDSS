# oiapp/scanners/wall_term_structure.py
"""
Wall Term Structure
────────────────────
Multi-expiry view of significant call/put walls for one symbol, spot
price shown alongside as a separate synced panel, plus a rule-based
read of how the walls are positioned across expiries (converging
toward spot, building OI, near-term support/resistance anomalies).

Built entirely on the app's EXISTING wall-scoring engine
(spy_strategies._oi_rows / _walls / _wall_rows, the same 40% OI + 30%
significant ΔOI + 20% proximity + 10% GEX score already used by the
GEX Plan) — this just loops it across N expiries and adds a term-
structure interpretation layer on top. No new OI fetch, no new scoring
math.

IMPORTANT — what "analysis" means here: this is a heuristic pattern
read off wall positioning only (where the strongest walls sit, whether
they're moving closer to spot across expiries, whether their OI is
growing). It is NOT a backtested signal, does not know direction with
certainty, and should be read the same way GEX levels are read
elsewhere in this app — context to evaluate against your own
framework, not a standalone trade trigger.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

wall_term_bp = Blueprint("wall_term", __name__, url_prefix="/wall-term-structure")

NEAR_THRESHOLD_PCT = 1.5   # a wall within this % of spot counts as "close"
BUILD_THRESHOLD_PCT = 20.0  # OI growth vs near-term expiry to call it "building"


# ── Core read: loop the existing wall engine across expiries ─────────────

def _wall_item_summary(w: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not w:
        return None
    return {
        "strike": w.get("strike"), "type": w.get("type"), "oi": w.get("oi"), "oi_change": w.get("oi_change"),
        "oi_change_pct": w.get("oi_change_pct"), "score": w.get("score"),
        "pct_from_spot": w.get("pct_from_spot"), "fresh": w.get("fresh"),
        "unwinding": w.get("unwinding"), "label": w.get("label"), "note": w.get("note"),
    }


def _nearest_strike_values(rows: list, spot: float, num_strikes: int) -> set:
    """Just the set of strike VALUES within num_strikes of spot on each
    side -- used to gate the wall bubbles by proximity. Deliberately NOT
    reusing _nearest_strikes() below, which pre-filters to only strikes
    with a nonzero oi_change (correct for the OI-change chart, wrong
    here -- a strike can be a completely legitimate wall candidate with
    real OI and simply no computed change yet, and excluding it here
    would silently shrink which strikes can even be considered walls,
    not just how they're displayed)."""
    distinct_strikes = sorted({r["strike"] for r in rows if r.get("strike") is not None})
    if not distinct_strikes:
        return set()
    below = [s for s in distinct_strikes if s <= spot][-num_strikes:]
    above = [s for s in distinct_strikes if s > spot][:num_strikes]
    return set(below) | set(above)


def _nearest_strikes(rows: list, spot: float, num_strikes: int) -> List[Dict[str, Any]]:
    """Limits the per-strike OI-change data to num_strikes distinct
    strikes above spot and num_strikes below -- not a fixed price-range
    window, since strike spacing varies by symbol (e.g. $1 for most
    equities, $5+ for higher-priced ones); "N nearest strikes each side"
    stays proportionate regardless of spacing. Default 12 each side
    (~24-25 total) based on the observation that daily moves rarely
    exceed roughly 7-8 points -- comfortably covered by 12 strikes on a
    typical $1-2.5 grid, with headroom to spare.
    """
    candidates = [r for r in rows if r.get("oi_change")]  # skip zero/unknown-change strikes, nothing to plot
    distinct_strikes = sorted({r["strike"] for r in candidates if r.get("strike") is not None})
    if not distinct_strikes:
        return []
    below = [s for s in distinct_strikes if s <= spot][-num_strikes:]
    above = [s for s in distinct_strikes if s > spot][:num_strikes]
    keep = set(below) | set(above)
    return [
        {"strike": r.get("strike"), "type": r.get("type"), "oi": r.get("oi"),
         "oi_change": r.get("oi_change"), "oi_change_pct": r.get("oi_change_pct")}
        for r in candidates if r.get("strike") in keep
    ]


def read_term_structure(symbol: str, num_expiries: int = 6, side: int = 3, num_strikes: int = 12) -> Dict[str, Any]:
    from ..scanners.spy_strategies import _future_exps, _oi_rows, _walls, _expiry_dte
    from ..services.oi_significance import build_oi_change_filter_context
    from ..services.market import get_spot, get_history

    spot = get_spot(symbol)
    if not spot:
        return {"error": f"No spot price available for {symbol}"}

    all_exps = _future_exps(symbol)
    if not all_exps:
        return {"error": f"No stored option expiries found for {symbol}. "
                          f"Run the watchlist fetch for this symbol first."}
    exps = sorted(all_exps, key=lambda e: _expiry_dte(e))[:max(1, int(num_expiries))]
    num_strikes = max(1, int(num_strikes or 12))

    panels: List[Dict[str, Any]] = []
    # Cumulative OI: summed across ALL fetched expiries, per (strike, type)
    # -- but ONLY at strikes that were an actual significant wall (top
    # WALLS/SIDE by score) in at least one expiry, not every strike that
    # merely has nonzero OI somewhere. Previously summed from the FULL,
    # unfiltered row set each expiry contributes -- that meant a strike
    # could show a large cumulative diamond purely from being summed
    # across 6 expiries, even if it never scored as significant in any
    # single one (e.g. a strike showing zero put-wall bubbles on the
    # chart at DTE=0 while still showing a cumulative put diamond at the
    # same strike). Restricting to ever-significant strikes makes the
    # cumulative view a genuine superset of what the individual bubbles
    # already show, instead of an independently-scoped, broader dataset
    # that never had any reason to visually agree with them.
    cumulative_oi_map: Dict[tuple, float] = {}
    ever_significant: set = set()
    for expiry in exps:
        rows = _oi_rows(symbol, expiry)
        for r in rows:
            strike, typ = r.get("strike"), r.get("type")
            if strike is None or not typ:
                continue
            key = (strike, typ)
            cumulative_oi_map[key] = cumulative_oi_map.get(key, 0) + int(r.get("oi") or 0)
        if not rows:
            continue
        dte = _expiry_dte(expiry)

        # Diagnostic only -- doesn't touch _oi_rows() (shared by many
        # other pages), just a direct read of how many distinct dates are
        # actually stored for this symbol+expiry, so "no OI-change data"
        # shows exactly why instead of a guess. _oi_rows() itself already
        # only diffs the latest two distinct dates correctly; if this
        # shows 1, that's the real, verifiable reason there's nothing to
        # diff, not a bug in the comparison logic.
        stored_dates = []
        sample_strike_history = []
        try:
            from ..db import _connect
            _con = _connect()
            stored_dates = [r[0] for r in _con.execute(
                "SELECT DISTINCT date FROM options WHERE symbol=? AND expiration=? ORDER BY date DESC LIMIT 10",
                (symbol, expiry)
            ).fetchall()]
            # Extra diagnostic: the actual OI value across each stored
            # date for whichever strike currently has the highest OI --
            # this is what actually proves or disproves a "the daily
            # fetch is storing duplicate/unchanged snapshots" bug vs. a
            # bug in how this page compares them. If these values are
            # identical across every date, the fetch itself isn't
            # capturing fresh data each day; if they differ, the bug is
            # somewhere in this page's own comparison logic instead.
            if stored_dates:
                top_strike_row = _con.execute(
                    "SELECT strike, type FROM options WHERE symbol=? AND expiration=? AND date=? "
                    "ORDER BY oi DESC LIMIT 1",
                    (symbol, expiry, stored_dates[0])
                ).fetchone()
                if top_strike_row:
                    top_strike, top_type = top_strike_row[0], top_strike_row[1]
                    sample_strike_history = [
                        {"date": r[0], "oi": r[1]} for r in _con.execute(
                            "SELECT date, SUM(oi) FROM options WHERE symbol=? AND expiration=? "
                            "AND strike=? AND type=? GROUP BY date ORDER BY date DESC LIMIT 10",
                            (symbol, expiry, top_strike, top_type)
                        ).fetchall()
                    ]
                    sample_strike_history_label = f"{top_type} {top_strike}"
                else:
                    sample_strike_history_label = None
            else:
                sample_strike_history_label = None
            _con.close()
        except Exception:
            sample_strike_history_label = None

        oi_sig_ctx = build_oi_change_filter_context(
            symbol, rows, expiry=expiry, source="wall_term_structure"
        )
        wall_info = _walls(rows, spot, side=side, oi_change_filter=oi_sig_ctx)
        # _walls()'s significant_call_walls/significant_put_walls are
        # ranked purely by score, with NO distance-from-spot cap -- the
        # OI-change chart's num_strikes filter was only ever wired into
        # oi_by_strike below, never into the wall bubbles shown on the
        # left chart. That let a strike far outside num_strikes (e.g. a
        # stale, oversized position way below spot) still show up as one
        # of the top "WALLS/SIDE" bubbles, completely independent of the
        # # Strikes/side control the page displays right next to it --
        # the two looked like they should agree and didn't. Apply the
        # SAME nearest-num_strikes window used everywhere else on this
        # page before ranking by score, so both charts respect the one
        # control consistently.
        near_strike_set = set(_nearest_strike_values(rows, spot, num_strikes))
        sig_calls = [w for w in (wall_info.get("significant_call_walls") or []) if w.get("strike") in near_strike_set]
        sig_puts = [w for w in (wall_info.get("significant_put_walls") or []) if w.get("strike") in near_strike_set]
        for w in sig_calls:
            ever_significant.add((w.get("strike"), "call"))
        for w in sig_puts:
            ever_significant.add((w.get("strike"), "put"))
        top_call = max(sig_calls, key=lambda w: w.get("score", 0)) if sig_calls else None
        top_put = max(sig_puts, key=lambda w: w.get("score", 0)) if sig_puts else None

        panels.append({
            "expiry": expiry, "dte": dte,
            "stored_dates": stored_dates, "stored_dates_count": len(stored_dates),
            "sample_strike_history": sample_strike_history,
            "sample_strike_label": sample_strike_history_label,
            "support": wall_info.get("support"), "resistance": wall_info.get("resistance"),
            "gamma_wall": wall_info.get("gamma_wall"),
            "top_call_wall": _wall_item_summary(top_call),
            "top_put_wall": _wall_item_summary(top_put),
            "call_walls": [_wall_item_summary(w) for w in sig_calls],
            "put_walls": [_wall_item_summary(w) for w in sig_puts],
            # Full per-strike OI change, NOT wall-filtered -- _oi_rows()
            # already compares latest vs. nearest prior distinct date for
            # THIS specific expiry (same correct per-expiry, per-strike
            # comparison the OI Viewer page's /api/oi_change uses), but
            # the wall lists above only keep the top-N strikes that scored
            # as "significant walls" -- which inherently skews toward
            # accumulation, since a wall is by definition a place OI is
            # concentrated/building. That skew was making the OI-change
            # chart look almost entirely one-sided (green) even on
            # expiries with a real build/unwind mix, because the genuine
            # negative-change strikes were simply never wall-significant
            # enough to make that filtered list. This is the full set,
            # THEN limited to num_strikes on each side of spot (not
            # wall-filtered, just proximity-filtered) so the chart stays
            # readable -- a symbol with hundreds of strikes was rendering
            # every single one regardless of distance from spot, per the
            # observation that daily moves rarely exceed ~7-8 points.
            "oi_by_strike": _nearest_strikes(rows, spot, num_strikes),
        })

    if not panels:
        return {"error": f"No stored OI rows for any of the checked expiries for {symbol}."}

    prices = get_history(symbol, "3mo") or []

    # Same "N nearest strikes to spot" limiting as the rest of this page --
    # a symbol with hundreds of strikes accumulated across every expiry
    # would otherwise dump all of them into one column, which is exactly
    # the clutter problem num_strikes already solves everywhere else here.
    # Restricted to ever_significant on top of that -- see the comment
    # where cumulative_oi_map is built for why: without this, a strike
    # could be "nearest to spot" but never actually a significant wall in
    # any single expiry, showing a cumulative diamond with nothing on the
    # chart to relate it to.
    distinct_cum_strikes = sorted({k[0] for k in cumulative_oi_map})
    cum_below = [s for s in distinct_cum_strikes if s <= spot][-num_strikes:]
    cum_above = [s for s in distinct_cum_strikes if s > spot][:num_strikes]
    cum_keep = set(cum_below) | set(cum_above)
    cumulative_oi = [
        {"strike": strike, "type": typ, "total_oi": total}
        for (strike, typ), total in cumulative_oi_map.items()
        if strike in cum_keep and (strike, typ) in ever_significant
    ]

    return {
        "symbol": symbol, "spot": spot, "expiries": panels, "prices": prices,
        "analysis": _build_analysis(panels, spot),
        "trade_ideas": _build_top_trade_ideas(panels, spot),
        "cumulative_oi": cumulative_oi,
    }


# ── 0DTE/Weekly trade ideas with confidence ────────────────────────────────
# Scoped to the near-term expiries specifically (DTE <= 7) -- this is where
# a wall's exact position and freshness matters most, since there's no time
# left for it to drift the way it would at 30-45 DTE. Confidence blends the
# wall's own 40/30/20/10 score (already computed by _walls) with the
# fresh/unwinding flags that same engine already produces -- a fresh wall
# holding is a stronger read than an old, unwinding one at the same score.

CONFIDENCE_LABELS = [(70, "High"), (45, "Medium"), (0, "Low")]


def _confidence_pct(wall: Dict[str, Any]) -> float:
    base = float(wall.get("score") or 0)
    if wall.get("fresh"):
        base += 12
    if wall.get("unwinding"):
        base -= 18
    return max(0.0, min(100.0, base))


def _confidence_label(pct: float) -> str:
    for threshold, label in CONFIDENCE_LABELS:
        if pct >= threshold:
            return label
    return "Low"


def _ideas_for_panel(p: Dict[str, Any]) -> List[Dict[str, Any]]:
    """All candidate ideas for ONE expiry panel, unsorted. Caller picks
    how many of these to keep (nearest expiry keeps more than farthest)."""
    expiry, dte = p["expiry"], p["dte"]
    put_w, call_w = p.get("top_put_wall"), p.get("top_call_wall")
    ideas: List[Dict[str, Any]] = []

    if put_w and (put_w.get("pct_from_spot") or 0) < 0:
        conf = _confidence_pct(put_w)
        freshness = "fresh buildup" if put_w.get("fresh") else ("unwinding" if put_w.get("unwinding") else "established")
        if put_w.get("unwinding"):
            idea_type, direction = "breakout_watch", "downside break possible -- support looks weaker than its score alone suggests"
        else:
            idea_type, direction = "support_play", "support likely to hold -- bullish lean into this expiry"
        ideas.append({
            "expiry": expiry, "dte": dte, "type": idea_type, "side": "put",
            "strike": put_w.get("strike"), "pct_from_spot": put_w.get("pct_from_spot"),
            "confidence_pct": round(conf, 1), "confidence_label": _confidence_label(conf),
            "freshness": freshness,
            "rationale": f"{expiry} ({dte}d): put wall at {put_w.get('strike')} "
                         f"({put_w.get('pct_from_spot'):+.2f}% from spot), {freshness}, "
                         f"score {put_w.get('score')}. {direction}.",
        })

    if call_w and (call_w.get("pct_from_spot") or 0) > 0:
        conf = _confidence_pct(call_w)
        freshness = "fresh buildup" if call_w.get("fresh") else ("unwinding" if call_w.get("unwinding") else "established")
        if call_w.get("unwinding"):
            idea_type, direction = "breakout_watch", "upside break possible -- resistance looks weaker than its score alone suggests"
        else:
            idea_type, direction = "resistance_play", "resistance likely to cap -- bearish lean into this expiry"
        ideas.append({
            "expiry": expiry, "dte": dte, "type": idea_type, "side": "call",
            "strike": call_w.get("strike"), "pct_from_spot": call_w.get("pct_from_spot"),
            "confidence_pct": round(conf, 1), "confidence_label": _confidence_label(conf),
            "freshness": freshness,
            "rationale": f"{expiry} ({dte}d): call wall at {call_w.get('strike')} "
                         f"({call_w.get('pct_from_spot'):+.2f}% from spot), {freshness}, "
                         f"score {call_w.get('score')}. {direction}.",
        })

    if put_w and call_w:
        put_conf, call_conf = _confidence_pct(put_w), _confidence_pct(call_w)
        if abs(put_conf - call_conf) >= 15:
            stronger_side = "put (downside)" if put_conf > call_conf else "call (upside)"
            lean = "bullish (support stronger than resistance)" if put_conf > call_conf else "bearish (resistance stronger than support)"
            ideas.append({
                "expiry": expiry, "dte": dte, "type": "directional_lean", "side": "both",
                "strike": None, "pct_from_spot": None,
                "confidence_pct": round(abs(put_conf - call_conf), 1),
                "confidence_label": _confidence_label(abs(put_conf - call_conf)),
                "freshness": None,
                "rationale": f"{expiry} ({dte}d): {stronger_side} wall notably stronger than the other side "
                             f"(put conf {put_conf:.0f} vs call conf {call_conf:.0f}) -- {lean}.",
            })

    ideas.sort(key=lambda x: x["confidence_pct"], reverse=True)
    return ideas


def _build_top_trade_ideas(panels: List[Dict[str, Any]], spot: float) -> Dict[str, Any]:
    """Top 1-2 ideas for the NEAREST expiry, top 1 for the FARTHEST
    (outermost) one checked -- not every expiry in range, which was
    producing a wall of ideas nobody could actually act on. Nearest maps
    to the 0DTE/weekly read, farthest to the 30-45 DTE read, matching how
    this app's tools are meant to split by horizon."""
    if not panels:
        return {"ideas": [], "note": "No expiries available."}

    nearest, farthest = panels[0], panels[-1]
    nearest_ideas = _ideas_for_panel(nearest)[:2]
    for idea in nearest_ideas:
        idea["horizon"] = "nearest"

    farthest_ideas = []
    if farthest is not nearest and farthest.get("expiry") != nearest.get("expiry"):
        farthest_ideas = _ideas_for_panel(farthest)[:1]
        for idea in farthest_ideas:
            idea["horizon"] = "farthest"

    ideas = nearest_ideas + farthest_ideas
    if not ideas:
        return {"ideas": [], "note": "No clear wall-based idea at the nearest or farthest expiry checked -- "
                                      "walls may be too far from spot or too thin to score."}
    return {
        "ideas": ideas,
        "disclaimer": "Confidence blends this app's existing wall score with the wall's own fresh/unwinding "
                       "flag -- it is a structured read of positioning, not a backtested win-rate. Treat High "
                       "confidence as 'worth building a trade around', not 'will happen'.",
    }


# ── Rule-based term-structure read ────────────────────────────────────────

def _build_analysis(panels: List[Dict[str, Any]], spot: float) -> Dict[str, Any]:
    with_calls = [p for p in panels if p.get("top_call_wall")]
    with_puts = [p for p in panels if p.get("top_put_wall")]

    findings: List[str] = []
    flags = {
        "call_converging": False, "call_building": False,
        "near_term_put_pressure": False, "near_term_call_pressure": False,
    }

    # Call wall convergence: does the farthest expiry's top call wall sit
    # CLOSER to spot than the nearest expiry's? (walls "coming closer" as
    # you go further out is the specific pattern described.)
    if len(with_calls) >= 2:
        near_call, far_call = with_calls[0]["top_call_wall"], with_calls[-1]["top_call_wall"]
        near_dist = abs(near_call.get("pct_from_spot") or 999)
        far_dist = abs(far_call.get("pct_from_spot") or 999)
        if far_dist < near_dist - 0.1:
            flags["call_converging"] = True
            findings.append(
                f"Call wall is converging toward spot across expiries -- "
                f"{with_calls[0]['expiry']} call wall sits {near_dist:.2f}% from spot, "
                f"but by {with_calls[-1]['expiry']} the nearest significant call wall "
                f"has moved to {far_dist:.2f}% away."
            )
        # OI building on that converging call wall
        near_oi = near_call.get("oi") or 0
        far_oi = far_call.get("oi") or 0
        if near_oi and far_oi and (far_oi - near_oi) / max(1, near_oi) * 100 > BUILD_THRESHOLD_PCT:
            flags["call_building"] = True
            findings.append(
                f"OI at the dominant call wall is growing further out in time "
                f"({near_oi:,} at {with_calls[0]['expiry']} vs {far_oi:,} at "
                f"{with_calls[-1]['expiry']}) -- overhead resistance is being built "
                f"ahead of where price has actually moved yet."
            )

    # Near-term put wall sitting unusually close to / above spot
    if with_puts:
        near_put = with_puts[0]["top_put_wall"]
        put_dist = near_put.get("pct_from_spot")
        if put_dist is not None and put_dist > -NEAR_THRESHOLD_PCT:
            flags["near_term_put_pressure"] = True
            side_word = "above" if put_dist > 0 else "just below"
            findings.append(
                f"Near-term put wall ({with_puts[0]['expiry']}, strike "
                f"{near_put.get('strike')}) sits {side_word} spot ({put_dist:+.2f}%) "
                f"-- unusually close for a put strike, which normally sits further "
                f"below as pure downside protection. Worth checking whether this is "
                f"fresh building (\"{near_put.get('label')}\") or an older position."
            )

    # Near-term call wall sitting unusually close to spot (overhead lid right away)
    if with_calls:
        near_call = with_calls[0]["top_call_wall"]
        call_dist = near_call.get("pct_from_spot")
        if call_dist is not None and 0 <= call_dist < NEAR_THRESHOLD_PCT:
            flags["near_term_call_pressure"] = True
            findings.append(
                f"Near-term call wall ({with_calls[0]['expiry']}, strike "
                f"{near_call.get('strike')}) is only {call_dist:.2f}% above spot -- "
                f"immediate overhead resistance, not a further-out level."
            )

    # Combine into a plain-English read + heuristic trade-shape ideas.
    narrative: str
    trade_ideas: List[str] = []
    if flags["near_term_put_pressure"] and flags["call_converging"] and flags["call_building"]:
        narrative = (
            "Near-term downside pressure (put wall sitting close to/above spot) "
            "combined with call resistance building and moving closer across "
            "expiries suggests a possible squeeze-then-cap pattern: price may "
            "grind or pop toward the nearer converging call wall before that "
            "growing overhead supply caps it, rather than a clean directional move "
            "either way."
        )
        trade_ideas = [
            "If near-term support (the put wall) holds: a short-dated put credit "
            "spread below it, sized to the expiry where the put wall scored highest.",
            "If price pushes up toward the converging call wall: a call credit "
            "spread at/above that strike, using the expiry where convergence is "
            "tightest -- that's where the wall is doing the most work.",
            "A calendar/diagonal using the near-term put wall as the short leg "
            "reference and a further expiry's call wall as the long leg reference "
            "is also consistent with this shape, if you want defined risk on both sides.",
        ]
    elif flags["call_converging"] and flags["call_building"]:
        narrative = (
            "Call resistance is building and moving closer to spot across expiries "
            "without a matching near-term put signal -- leans toward capped upside "
            "rather than a clear downside trigger."
        )
        trade_ideas = ["Call credit spread near the converging wall is the most "
                        "directly supported idea here."]
    elif flags["near_term_put_pressure"]:
        narrative = ("Near-term put wall sitting unusually close to spot is the "
                      "dominant signal -- near-term support may be more fragile "
                      "than the distance alone suggests.")
        trade_ideas = ["Worth confirming this is fresh buildup (not stale OI) before "
                        "leaning on it as support; if fresh, a tighter stop below it "
                        "makes sense on any long exposure."]
    else:
        narrative = ("No strong term-structure pattern detected across the checked "
                      "expiries -- walls aren't showing a clear converging/building "
                      "shape right now.")
        trade_ideas = []

    return {
        "flags": flags, "findings": findings, "narrative": narrative,
        "trade_ideas": trade_ideas,
        "disclaimer": "Heuristic pattern read from wall positioning only -- not a "
                       "backtested signal. Confirm against price action and your own "
                       "framework before sizing anything.",
    }


# ── Routes ──────────────────────────────────────────────────────────────

@wall_term_bp.route("/")
def page():
    return render_template("wall_term_structure.html")


@wall_term_bp.route("/api/read")
def api_read():
    from .spy_strategies import _json_safe
    try:
        symbol = (request.args.get("symbol") or "SPY").upper().strip()
        num_expiries = int(request.args.get("num_expiries", 6))
        side = int(request.args.get("side", 3))
        num_strikes = int(request.args.get("num_strikes", 12))
        result = read_term_structure(symbol, num_expiries, side, num_strikes)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    if "error" in result:
        return jsonify(_json_safe(result)), 400
    return jsonify(_json_safe(result))
