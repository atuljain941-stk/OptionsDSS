"""vertical_executor.py -- V113 addition.

THE CORE SAFETY LOGIC for this whole feature. ICICI Direct's Breeze API
has no native multi-leg/vertical order type -- each leg has to be sent
as an independent single-leg order. That means there's a real window,
between leg 1's fill and leg 2's fill, where the account is briefly in
whatever partial state the legs-so-far represent. The sequencing rule
this module enforces:

  OPENING a multi-leg position: execute every LONG leg first, then
  every SHORT leg. You're never left holding a naked short with no
  protection, even for a moment -- worst case if something fails after
  the long legs but before the short legs, you're just holding a long
  option (safe, capped risk), not short and uncovered.

  CLOSING a multi-leg position: reverse of the above -- buy back every
  SHORT leg first (removing the uncovered-risk side), then dispose of
  every LONG leg. Same principle: if something fails partway through
  the close, you end up still holding the (safe, capped-risk) long
  legs, never in a state where the short side is uncovered.

  SINGLE-LEG strategies (naked call/put, no partner leg): there's only
  one leg, so this collapses to a single straight order in both
  directions -- no sequencing needed, exactly as specified.

This same rule generalizes past simple 2-leg verticals to any
multi-leg structure (iron condors, iron butterflies, etc.) -- every
leg just needs a `side` of "LONG" or "SHORT" tagged on it, and the
sequencer group-sorts by that regardless of how many legs there are.

Action mapping (this is the part that's easy to get backwards):
  Opening a LONG leg  -> BUY
  Opening a SHORT leg -> SELL
  Closing a LONG leg  -> SELL  (dispose of the long)
  Closing a SHORT leg -> BUY   (buy back / cover the short)
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional

from . import icici_breeze as breeze

Side = Literal["LONG", "SHORT"]


def _sequence_for_open(legs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """LONG legs first, then SHORT legs. Stable sort preserves the
    caller's original ordering within each group."""
    return sorted(legs, key=lambda leg: 0 if leg["side"] == "LONG" else 1)


def _sequence_for_close(legs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """SHORT legs first (buy back / cover), then LONG legs (dispose)."""
    return sorted(legs, key=lambda leg: 0 if leg["side"] == "SHORT" else 1)


def _open_action_for_side(side: Side) -> str:
    return "buy" if side == "LONG" else "sell"


def _close_action_for_side(side: Side) -> str:
    # Reverse of the opening action for that side.
    return "sell" if side == "LONG" else "buy"


def _execute_sequence(
    stock_code: str,
    expiry_date: str,
    ordered: List[Dict[str, Any]],
    action_for_leg,
    order_type: str,
    price_by_leg: Dict[int, float],
    execution_mode: str,
    inter_leg_delay_sec: float,
    fill_timeout_sec: float,
    fill_poll_interval_sec: float,
    dry_run: bool,
) -> Dict[str, Any]:
    """Shared engine for both open and close. Two execution modes:

    "safe_sequential" (default): place leg 1, then POLL until it's
    actually confirmed FILLED (not just accepted -- see
    icici_breeze.poll_order_fill) before placing leg 2. This is the
    real safety guarantee: the account is genuinely never in a state
    where a SHORT leg exists without its LONG protection (open) or
    where the SHORT side has been removed but the LONG side hasn't
    (close, reversed). Tradeoff: the wait for fill confirmation is a
    window where the market can move against the *next* leg -- on a
    liquid index option this is normally well under a second, but
    it's not zero, and it's the honest cost of the safety guarantee.

    "simultaneous_market": fire every leg's market order back-to-back
    with no wait between them (not even for acceptance, let alone
    fill) -- minimizes the time window between legs to essentially
    just however long it takes this loop to iterate, which minimizes
    slippage risk from price movement between legs. Tradeoff: this
    gives up the "never naked" guarantee -- if leg 1 fails to fill
    (rejected, insufficient margin, etc.) after leg 2 has *also*
    already been fired, there's no chance to abort before leg 2 goes
    out. Only sensible for market orders on genuinely liquid
    instruments where near-simultaneous fill is a safe assumption.
    """
    leg_results: List[Dict[str, Any]] = []

    if execution_mode == "simultaneous_market":
        order_type = "market"  # simultaneous mode only makes sense at market
        for leg in ordered:
            action = action_for_leg(leg["side"])
            if dry_run:
                leg_results.append({"leg_index": leg["leg_index"], "side": leg["side"], "action": action,
                                     "dry_run": True, "ok": True, "order_id": None})
                continue
            result = breeze.place_order(
                stock_code=stock_code, expiry_date=expiry_date, right=leg["right"],
                strike_price=leg["strike_price"], action=action, quantity=leg["quantity"],
                order_type="market",
            )
            leg_results.append({
                "leg_index": leg["leg_index"], "side": leg["side"], "action": action,
                "ok": result["ok"], "order_id": result.get("order_id"), "error": result.get("error"),
                "fill_confirmed": False,  # simultaneous mode never confirms fills before moving on
            })
        # In simultaneous mode "success" means every order was accepted
        # by the broker -- fills themselves are not confirmed here.
        any_failed = any(not r["ok"] for r in leg_results)
        return {"ok": not any_failed, "leg_results": leg_results,
                "aborted_after_leg": None if not any_failed else next(i for i, r in enumerate(leg_results) if not r["ok"])}

    # safe_sequential (default)
    for i, leg in enumerate(ordered):
        action = action_for_leg(leg["side"])
        if dry_run:
            leg_results.append({"leg_index": leg["leg_index"], "side": leg["side"], "action": action,
                                 "dry_run": True, "ok": True, "order_id": None, "fill_confirmed": True})
            continue
        # Per-leg order type: a leg with a limit price in price_by_leg
        # goes as a limit order at that price; every other leg uses the
        # position's default order_type (normally "market"). This is
        # independent per leg -- one leg can be limit while another is
        # market in the same open/close sequence.
        leg_price = price_by_leg.get(leg["leg_index"])
        leg_order_type = "limit" if leg_price else order_type
        result = breeze.place_order(
            stock_code=stock_code, expiry_date=expiry_date, right=leg["right"],
            strike_price=leg["strike_price"], action=action, quantity=leg["quantity"],
            order_type=leg_order_type, price=leg_price or 0.0,
        )
        if not result["ok"]:
            leg_results.append({"leg_index": leg["leg_index"], "side": leg["side"], "action": action,
                                 "ok": False, "order_id": None, "error": result.get("error"), "fill_confirmed": False})
            return {"ok": False, "leg_results": leg_results, "aborted_after_leg": i}

        order_id = result.get("order_id")
        fill = breeze.poll_order_fill(order_id, timeout_sec=fill_timeout_sec, poll_interval_sec=fill_poll_interval_sec)
        leg_results.append({
            "leg_index": leg["leg_index"], "side": leg["side"], "action": action,
            "ok": bool(fill.get("filled")), "order_id": order_id, "error": fill.get("error"),
            "fill_confirmed": bool(fill.get("filled")), "fill_status": fill.get("status"),
            "fill_elapsed_sec": fill.get("elapsed_sec"),
            "fill_average_price": fill.get("average_price"),
        })
        if not fill.get("filled"):
            # Order was accepted but fill could not be confirmed within
            # the timeout -- stop here rather than guess. Same safe
            # partial-state guarantee as an outright rejection: nothing
            # past this leg has been touched.
            return {"ok": False, "leg_results": leg_results, "aborted_after_leg": i}
        if inter_leg_delay_sec > 0 and i < len(ordered) - 1:
            time.sleep(inter_leg_delay_sec)

    return {"ok": True, "leg_results": leg_results, "aborted_after_leg": None}


def execute_open(
    stock_code: str,
    expiry_date: str,
    legs: List[Dict[str, Any]],
    order_type: str = "market",
    price_by_leg: Optional[Dict[int, float]] = None,
    execution_mode: str = "safe_sequential",  # or "simultaneous_market"
    inter_leg_delay_sec: float = 0.5,
    fill_timeout_sec: float = 5.0,
    fill_poll_interval_sec: float = 0.3,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Open a multi-leg (or single-leg) position with safe sequencing.

    `legs`: list of {"leg_index": int, "right": "call"|"put",
             "strike_price": float, "side": "LONG"|"SHORT", "quantity": int}

    See _execute_sequence's docstring for the safe_sequential vs
    simultaneous_market tradeoff. Default is safe_sequential -- each
    leg's fill is confirmed (not just its acceptance) before the next
    leg is placed.
    """
    ordered = _sequence_for_open(legs)
    return _execute_sequence(
        stock_code, expiry_date, ordered, _open_action_for_side, order_type,
        price_by_leg or {}, execution_mode, inter_leg_delay_sec,
        fill_timeout_sec, fill_poll_interval_sec, dry_run,
    )


def execute_close(
    stock_code: str,
    expiry_date: str,
    legs: List[Dict[str, Any]],
    order_type: str = "market",
    price_by_leg: Optional[Dict[int, float]] = None,
    execution_mode: str = "safe_sequential",  # or "simultaneous_market"
    inter_leg_delay_sec: float = 0.5,
    fill_timeout_sec: float = 5.0,
    fill_poll_interval_sec: float = 0.3,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Close a multi-leg (or single-leg) position with safe sequencing
    -- SHORT legs bought back first, then LONG legs sold. See
    _execute_sequence's docstring for the safe_sequential vs
    simultaneous_market tradeoff."""
    ordered = _sequence_for_close(legs)
    return _execute_sequence(
        stock_code, expiry_date, ordered, _close_action_for_side, order_type,
        price_by_leg or {}, execution_mode, inter_leg_delay_sec,
        fill_timeout_sec, fill_poll_interval_sec, dry_run,
    )


def compute_combined_pnl_rupees(stock_code: str, expiry_date: str, legs: List[Dict[str, Any]], lot_size: int = 1) -> Dict[str, Any]:
    """Combined P&L across every leg, in rupees -- this is the basis
    used for target/stop-loss checks (per explicit choice: combined
    P&L across the whole position, not per-leg or underlying-price-based).

    For a LONG leg:  pnl = (current_ltp - entry_price) * qty * lot_size
    For a SHORT leg: pnl = (entry_price - current_ltp) * qty * lot_size
    """
    total = 0.0
    leg_pnls = []
    any_quote_failed = False
    for i, leg in enumerate(legs):
        if i > 0:
            time.sleep(0.3)  # proactive spacing -- get_quote() retries reactively too, this reduces how often it needs to
        q = breeze.get_quote(stock_code, expiry_date, leg["right"], leg["strike_price"])
        if not q["ok"] or q["ltp"] is None:
            any_quote_failed = True
            leg_pnls.append({"leg_index": leg["leg_index"], "pnl": None, "error": q.get("error")})
            continue
        ltp = q["ltp"]
        entry = float(leg.get("entry_price") or 0)
        qty = int(leg.get("quantity") or 0) * lot_size
        if leg["side"] == "LONG":
            pnl = (ltp - entry) * qty
        else:
            pnl = (entry - ltp) * qty
        total += pnl
        leg_pnls.append({"leg_index": leg["leg_index"], "pnl": round(pnl, 2), "ltp": ltp})

    return {
        "ok": not any_quote_failed,
        "combined_pnl_rupees": round(total, 2),
        "leg_pnls": leg_pnls,
        "partial": any_quote_failed,
    }
