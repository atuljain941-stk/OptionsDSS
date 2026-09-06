"""schwab_vertical_executor.py -- V134 addition.

Same safety logic as the ICICI vertical_executor.py, generalized to
handle TWO leg types in one uniform model:

  {"instrument_type": "option", "right": "call"|"put",
   "strike_price": float, "expiry_date": "YYYY-MM-DD",
   "side": "LONG"|"SHORT", "quantity": int}

  {"instrument_type": "stock", "side": "LONG"|"SHORT", "quantity": int}
  (symbol comes from the position's own top-level stock_code, same as
  every option leg in a position shares one underlying)

Sequencing rule (identical to ICICI, restated because it's the part
that matters most): opening a position executes every LONG leg first,
then every SHORT leg -- never a moment of naked short exposure, even
across a mix of stock and option legs in the same strategy. Closing
reverses that -- SHORT legs (buy back / cover) first, then LONG legs
(sell/close). A single-leg position (just one stock buy, or one naked
option) collapses to a straight order with no sequencing at all, same
as the ICICI build.

Fill confirmation (poll_order_fill, not just "order accepted") is the
default mode here too, for the same reason it was added to ICICI: an
accepted order isn't the same as a filled one, and proceeding to the
next leg on an unconfirmed fill defeats the whole point of sequencing.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional

from . import schwab_trading as trading

Side = Literal["LONG", "SHORT"]


def _sequence_for_open(legs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(legs, key=lambda leg: 0 if leg["side"] == "LONG" else 1)


def _sequence_for_close(legs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(legs, key=lambda leg: 0 if leg["side"] == "SHORT" else 1)


def _open_action(leg: Dict[str, Any]) -> str:
    if leg["instrument_type"] == "stock":
        return "BUY" if leg["side"] == "LONG" else "SELL_SHORT"
    return "BUY_TO_OPEN" if leg["side"] == "LONG" else "SELL_TO_OPEN"


def _close_action(leg: Dict[str, Any]) -> str:
    if leg["instrument_type"] == "stock":
        return "SELL" if leg["side"] == "LONG" else "BUY_TO_COVER"
    return "SELL_TO_CLOSE" if leg["side"] == "LONG" else "BUY_TO_CLOSE"


def _place_leg_order(stock_code: str, leg: Dict[str, Any], action: str, order_type: str, price: Optional[float]) -> Dict[str, Any]:
    if leg["instrument_type"] == "stock":
        return trading.place_equity_order(stock_code, action, leg["quantity"], order_type, price)
    occ_symbol = trading.to_occ_symbol(stock_code, leg["expiry_date"], leg["right"], leg["strike_price"])
    return trading.place_option_order(occ_symbol, action, leg["quantity"], order_type, price)


def _execute_sequence(
    stock_code: str, ordered: List[Dict[str, Any]], action_for_leg,
    order_type: str, price_by_leg: Dict[int, float], execution_mode: str,
    inter_leg_delay_sec: float, fill_timeout_sec: float, fill_poll_interval_sec: float,
    dry_run: bool,
) -> Dict[str, Any]:
    leg_results: List[Dict[str, Any]] = []

    if execution_mode == "simultaneous_market":
        for leg in ordered:
            action = action_for_leg(leg)
            if dry_run:
                leg_results.append({"leg_index": leg["leg_index"], "side": leg["side"], "action": action,
                                     "dry_run": True, "ok": True, "order_id": None})
                continue
            result = _place_leg_order(stock_code, leg, action, "MARKET", None)
            leg_results.append({"leg_index": leg["leg_index"], "side": leg["side"], "action": action,
                                 "ok": result["ok"], "order_id": result.get("order_id"), "error": result.get("error"),
                                 "fill_confirmed": False})
        any_failed = any(not r["ok"] for r in leg_results)
        return {"ok": not any_failed, "leg_results": leg_results,
                "aborted_after_leg": None if not any_failed else next(i for i, r in enumerate(leg_results) if not r["ok"])}

    for i, leg in enumerate(ordered):
        action = action_for_leg(leg)
        if dry_run:
            leg_results.append({"leg_index": leg["leg_index"], "side": leg["side"], "action": action,
                                 "dry_run": True, "ok": True, "order_id": None, "fill_confirmed": True})
            continue
        result = _place_leg_order(stock_code, leg, action, order_type, price_by_leg.get(leg["leg_index"]))
        if not result["ok"]:
            leg_results.append({"leg_index": leg["leg_index"], "side": leg["side"], "action": action,
                                 "ok": False, "order_id": None, "error": result.get("error"), "fill_confirmed": False})
            return {"ok": False, "leg_results": leg_results, "aborted_after_leg": i}

        order_id = result.get("order_id")
        fill = trading.poll_order_fill(order_id, timeout_sec=fill_timeout_sec, poll_interval_sec=fill_poll_interval_sec)
        leg_results.append({
            "leg_index": leg["leg_index"], "side": leg["side"], "action": action,
            "ok": bool(fill.get("filled")), "order_id": order_id, "error": fill.get("error"),
            "fill_confirmed": bool(fill.get("filled")), "fill_status": fill.get("status"),
            "fill_average_price": fill.get("average_price"),
        })
        if not fill.get("filled"):
            return {"ok": False, "leg_results": leg_results, "aborted_after_leg": i}
        if inter_leg_delay_sec > 0 and i < len(ordered) - 1:
            time.sleep(inter_leg_delay_sec)

    return {"ok": True, "leg_results": leg_results, "aborted_after_leg": None}


def execute_open(stock_code: str, legs: List[Dict[str, Any]], order_type: str = "MARKET",
                  price_by_leg: Optional[Dict[int, float]] = None, execution_mode: str = "safe_sequential",
                  inter_leg_delay_sec: float = 0.5, fill_timeout_sec: float = 5.0,
                  fill_poll_interval_sec: float = 0.3, dry_run: bool = False) -> Dict[str, Any]:
    ordered = _sequence_for_open(legs)
    return _execute_sequence(stock_code, ordered, _open_action, order_type, price_by_leg or {},
                              execution_mode, inter_leg_delay_sec, fill_timeout_sec, fill_poll_interval_sec, dry_run)


def execute_close(stock_code: str, legs: List[Dict[str, Any]], order_type: str = "MARKET",
                   price_by_leg: Optional[Dict[int, float]] = None, execution_mode: str = "safe_sequential",
                   inter_leg_delay_sec: float = 0.5, fill_timeout_sec: float = 5.0,
                   fill_poll_interval_sec: float = 0.3, dry_run: bool = False) -> Dict[str, Any]:
    ordered = _sequence_for_close(legs)
    return _execute_sequence(stock_code, ordered, _close_action, order_type, price_by_leg or {},
                              execution_mode, inter_leg_delay_sec, fill_timeout_sec, fill_poll_interval_sec, dry_run)


def compute_combined_pnl(stock_code: str, legs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Combined P&L across every leg (stock + option mixed), same
    basis as ICICI: LONG = (ltp-entry)*qty, SHORT = (entry-ltp)*qty."""
    total = 0.0
    leg_pnls = []
    any_failed = False
    for leg in legs:
        if leg["instrument_type"] == "stock":
            q = trading.get_quote(stock_code)
        else:
            occ_symbol = trading.to_occ_symbol(stock_code, leg["expiry_date"], leg["right"], leg["strike_price"])
            q = trading.get_quote(occ_symbol)
        if not q["ok"] or q["ltp"] is None:
            any_failed = True
            leg_pnls.append({"leg_index": leg["leg_index"], "pnl": None, "error": q.get("error")})
            continue
        ltp = q["ltp"]
        entry = float(leg.get("entry_price") or 0)
        qty = int(leg.get("quantity") or 0)
        multiplier = 1 if leg["instrument_type"] == "stock" else 100  # options are per-contract, x100 shares
        pnl = ((ltp - entry) if leg["side"] == "LONG" else (entry - ltp)) * qty * multiplier
        total += pnl
        leg_pnls.append({"leg_index": leg["leg_index"], "pnl": round(pnl, 2), "ltp": ltp})
    return {"ok": not any_failed, "combined_pnl": round(total, 2), "leg_pnls": leg_pnls, "partial": any_failed}
