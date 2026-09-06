"""schwab_trading.py -- V134 addition.

Order placement, quotes, and option chain access for Schwab, used by
the new multi-leg (options) + single-instrument (stock) auto-trading
system. Deliberately built ON TOP of the EXISTING, working Schwab
OAuth infrastructure in this codebase (oiapp.schwab.schwab_routes,
oiapp.services.futures_oi_schwab.refresh_schwab_access_token, and
oiapp.autotrading.schwab_eod's _schwab_request/_schwab_cfg_headers/
_place_schwab_order) -- no new auth flow, no new token storage. This
is a real advantage over the ICICI build: Schwab's OAuth here already
auto-refreshes on 401 without a daily manual token paste.

Order construction is genuinely NEW here (schwab_eod.py's own order
builder is scoped to short-equity OCO brackets only) -- long/short
equity, and options BUY_TO_OPEN/SELL_TO_OPEN/BUY_TO_CLOSE/SELL_TO_CLOSE,
have not been exercised against a live Schwab account in this codebase
before. Flagged clearly below wherever that applies.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _request(method: str, path: str, params: Optional[Dict[str, Any]] = None,
              payload: Optional[Any] = None, timeout: int = 20):
    from ..autotrading.schwab_eod import _schwab_request
    return _schwab_request(method, path, params=params, payload=payload, timeout=timeout)


def _account_hash() -> Optional[str]:
    from ..autotrading.schwab_eod import _schwab_cfg_headers
    _cfg, _headers, acct = _schwab_cfg_headers()
    return acct or None


def is_connected() -> bool:
    """Matches the SAME definition the existing Scheduler page's Schwab
    badge uses (valid access_token, not expired) -- NOT account_hash.
    Those are two different things: OAuth can succeed with no
    account_hash saved yet (it's a separate manual field), and this
    page's earlier version conflated them, showing "not connected"
    even when the other page correctly showed "connected". Trading
    specifically also needs account_hash -- see has_account_hash()."""
    try:
        from ..schwab.schwab_routes import _get_config
        cfg = _get_config() or {}
    except Exception:
        return False
    if not cfg.get("access_token"):
        return False
    try:
        expiry = float(cfg.get("token_expiry") or 0)
        import time as _time
        if expiry and expiry <= _time.time():
            return False  # expired; _schwab_request() will try to refresh on next real call, but don't claim "connected" here
    except Exception:
        pass
    return True


def has_account_hash() -> bool:
    """Separate from is_connected() -- this is specifically what
    ORDER PLACEMENT and position-listing need (Schwab's trading
    endpoints are scoped to /accounts/{accountHash}/...). OAuth being
    valid does not guarantee this is set; it's a distinct field saved
    via the existing /schwab/config form."""
    return bool(_account_hash())


def _ensure_live_toggle_table() -> None:
    import sqlite3
    from ..db import DB_PATH
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS schwab_auto_trading_config (id INTEGER PRIMARY KEY, live_trading_enabled INTEGER NOT NULL DEFAULT 1, updated TEXT)")
        con.commit()
    finally:
        con.close()


def is_live_trading_enabled() -> bool:
    """Separate from schwab_config (shared with other existing Schwab
    features like futures OI) -- this new system gets its own isolated
    toggle so flipping it can't affect anything else in the app."""
    import sqlite3
    from ..db import DB_PATH
    _ensure_live_toggle_table()
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT live_trading_enabled FROM schwab_auto_trading_config WHERE id=1").fetchone()
        return True if row is None else bool(row[0])
    finally:
        con.close()


def set_live_trading_enabled(enabled: bool) -> None:
    import sqlite3
    from datetime import datetime as _dt
    from ..db import DB_PATH
    _ensure_live_toggle_table()
    con = sqlite3.connect(DB_PATH)
    try:
        if con.execute("SELECT id FROM schwab_auto_trading_config WHERE id=1").fetchone():
            con.execute("UPDATE schwab_auto_trading_config SET live_trading_enabled=?, updated=? WHERE id=1", (int(enabled), _dt.now().isoformat()))
        else:
            con.execute("INSERT INTO schwab_auto_trading_config (id, live_trading_enabled, updated) VALUES (1, ?, ?)", (int(enabled), _dt.now().isoformat()))
        con.commit()
    finally:
        con.close()


def is_market_hours() -> bool:
    """US equity/options market hours gate (9:30 AM - 4:00 PM ET,
    Mon-Fri, no holiday calendar) -- same purpose as the ICICI build's
    NSE gate: stops background jobs from polling around the clock
    during a multi-day unattended run."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        now_et = _dt.now(ZoneInfo("America/New_York"))
    except Exception:
        return True
    if now_et.weekday() >= 5:
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et <= market_close


def to_occ_symbol(underlying: str, expiry_date: str, right: str, strike: float) -> str:
    """Schwab/OCC-style option symbol: "AAPL  250117C00150000" -- 6-char
    padded underlying, YYMMDD expiry, C/P, 8-digit strike*1000.
    `expiry_date` must be "YYYY-MM-DD". NEEDS LIVE VALIDATION -- built
    from the documented OCC convention Schwab's API uses, not
    confirmed against a real order in this codebase yet.
    """
    y, m, d = expiry_date.split("-")
    yymmdd = f"{y[2:]}{m}{d}"
    cp = "C" if right.lower() == "call" else "P"
    strike_str = f"{int(round(strike * 1000)):08d}"
    return f"{underlying.upper():<6}{yymmdd}{cp}{strike_str}"


def get_quote(symbol: str) -> Dict[str, Any]:
    """Live quote for a stock OR option symbol -- Schwab's /quotes
    endpoint handles both via the symbol format."""
    data, code = _request("GET", "/marketdata/v1/quotes", params={"symbols": symbol})
    if code != 200 or not data.get("ok"):
        return {"ok": False, "ltp": None, "error": (data or {}).get("error") or f"HTTP {code}"}
    payload = (data.get("data") or {}).get(symbol) or {}
    quote = payload.get("quote") or {}
    ltp = quote.get("lastPrice") or quote.get("mark") or quote.get("closePrice")
    return {"ok": ltp is not None, "ltp": ltp, "error": None if ltp is not None else "no price in quote response", "raw": payload}


def get_spot_price(symbol: str) -> Dict[str, Any]:
    q = get_quote(symbol)
    return {"ok": q["ok"], "spot": q["ltp"], "error": q["error"]}


def get_option_chain(symbol: str, expiry_date: str) -> Dict[str, Any]:
    """Full option chain for symbol/expiry. Schwab's documented
    /chains endpoint supports fromDate/toDate scoping to one expiry,
    unlike Breeze -- NEEDS LIVE VALIDATION but this is a much better-
    documented, more standard endpoint than ICICI's turned out to be.
    """
    params = {
        "symbol": symbol, "contractType": "ALL",
        "fromDate": expiry_date, "toDate": expiry_date,
        "includeUnderlyingQuote": "true",
    }
    data, code = _request("GET", "/marketdata/v1/chains", params=params)
    if code != 200 or not data.get("ok"):
        return {"ok": False, "rows": [], "error": (data or {}).get("error") or f"HTTP {code}"}
    payload = data.get("data") or {}
    rows: List[Dict[str, Any]] = []
    for map_key, right in (("callExpDateMap", "call"), ("putExpDateMap", "put")):
        exp_map = payload.get(map_key) or {}
        for exp_key, by_strike in exp_map.items():
            exp_date = exp_key.split(":")[0]
            if exp_date != expiry_date:
                continue
            for strike_str, contracts in (by_strike or {}).items():
                for c in (contracts or []):
                    rows.append({
                        "right": right, "strike_price": float(strike_str),
                        "ltp": c.get("last") or c.get("mark") or 0,
                        "bid": c.get("bid"), "ask": c.get("ask"),
                        "open_interest": int(c.get("openInterest") or 0),
                        "volume": int(c.get("totalVolume") or 0),
                        "delta": c.get("delta"), "gamma": c.get("gamma"), "iv": c.get("volatility"),
                    })
    return {"ok": True, "rows": rows, "spot": payload.get("underlyingPrice"), "error": None}


def get_available_expiries(symbol: str) -> Dict[str, Any]:
    """Chain without a date filter -- Schwab's /chains endpoint is
    documented to return contracts across multiple expiries when
    fromDate/toDate are omitted (unlike Breeze, which required them).
    NEEDS LIVE VALIDATION."""
    data, code = _request("GET", "/marketdata/v1/chains", params={"symbol": symbol, "contractType": "ALL"})
    if code != 200 or not data.get("ok"):
        return {"ok": False, "expiries": [], "error": (data or {}).get("error") or f"HTTP {code}"}
    payload = data.get("data") or {}
    expiries = set()
    for map_key in ("callExpDateMap", "putExpDateMap"):
        for exp_key in (payload.get(map_key) or {}).keys():
            expiries.add(exp_key.split(":")[0])
    return {"ok": bool(expiries), "expiries": sorted(expiries),
            "error": None if expiries else "no expiries found in response"}


def get_account_positions() -> Dict[str, Any]:
    acct = _account_hash()
    if not acct:
        return {"ok": False, "positions": [], "error": "no Schwab account_hash configured -- connect Schwab first"}
    data, code = _request("GET", f"/trader/v1/accounts/{acct}", params={"fields": "positions"})
    if code != 200 or not data.get("ok"):
        return {"ok": False, "positions": [], "error": (data or {}).get("error") or f"HTTP {code}"}
    raw_positions = ((data.get("data") or {}).get("securitiesAccount") or {}).get("positions") or []
    out = []
    for p in raw_positions:
        instrument = p.get("instrument") or {}
        long_qty = float(p.get("longQuantity") or 0)
        short_qty = float(p.get("shortQuantity") or 0)
        qty = long_qty - short_qty
        out.append({
            "symbol": instrument.get("symbol"),
            "asset_type": instrument.get("assetType"),
            "quantity": qty,
            "side": "LONG" if qty >= 0 else "SHORT",
            "average_price": p.get("averagePrice"),
            "raw": p,
        })
    return {"ok": True, "positions": out, "error": None}


def _apply_order_pricing(order: Dict[str, Any], order_type: str, price: Optional[float], stop_price: Optional[float]) -> Optional[str]:
    """Fills in price/stopPrice for whichever order type was requested.
    Shared by all three order builders so LIMIT/STOP/STOP_LIMIT behave
    identically everywhere, matching the exact JSON shape already
    proven working in schwab_eod.py's bracket-order builder (STOP =
    stopPrice only, STOP_LIMIT = stopPrice + price). Returns an error
    string if a required price is missing, else None."""
    ot = order_type.upper()
    if ot == "LIMIT":
        if price is None:
            return "LIMIT order requires a price"
        order["price"] = f"{price:.2f}"
    elif ot == "STOP":
        if stop_price is None:
            return "STOP order requires a stop_price"
        order["stopPrice"] = f"{stop_price:.2f}"
    elif ot == "STOP_LIMIT":
        if stop_price is None or price is None:
            return "STOP_LIMIT order requires both stop_price (trigger) and price (limit once triggered)"
        order["stopPrice"] = f"{stop_price:.2f}"
        order["price"] = f"{price:.2f}"
    return None


def place_equity_order(symbol: str, action: str, quantity: int, order_type: str = "MARKET", price: Optional[float] = None,
                        duration: str = "DAY", cancel_time: Optional[str] = None, stop_price: Optional[float] = None) -> Dict[str, Any]:
    """action: BUY | SELL | SELL_SHORT | BUY_TO_COVER
    order_type: MARKET | LIMIT | STOP | STOP_LIMIT
    duration: "DAY", "GOOD_TILL_CANCEL", "FILL_OR_KILL", or
    "IMMEDIATE_OR_CANCEL". cancel_time (YYYY-MM-DD) is only meaningful
    with GOOD_TILL_CANCEL -- Schwab auto-expires a GTC order at that
    date if it hasn't filled ("GTC until <date>")."""
    acct = _account_hash()
    if not acct:
        return {"ok": False, "order_id": None, "error": "no Schwab account_hash configured -- connect Schwab first"}
    order: Dict[str, Any] = {
        "session": "NORMAL", "duration": duration.upper(),
        "orderType": order_type.upper(), "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": action.upper(), "quantity": quantity,
            "instrument": {"symbol": symbol.upper(), "assetType": "EQUITY"},
        }],
    }
    if cancel_time and duration.upper() == "GOOD_TILL_CANCEL":
        order["cancelTime"] = cancel_time
    pricing_error = _apply_order_pricing(order, order_type, price, stop_price)
    if pricing_error:
        return {"ok": False, "order_id": None, "error": pricing_error}
    data, code = _request("POST", f"/trader/v1/accounts/{acct}/orders", payload=order, timeout=30)
    if code in (200, 201, 202) and data.get("ok"):
        loc = (data.get("headers") or {}).get("Location") or (data.get("headers") or {}).get("location") or ""
        order_id = str(loc).rstrip("/").split("/")[-1] if loc else ""
        return {"ok": True, "order_id": order_id, "error": None}
    return {"ok": False, "order_id": None, "error": (data or {}).get("error") or f"HTTP {code}", "detail": (data or {}).get("detail")}


def place_option_order(occ_symbol: str, action: str, quantity: int, order_type: str = "MARKET", price: Optional[float] = None,
                        duration: str = "DAY", cancel_time: Optional[str] = None, stop_price: Optional[float] = None) -> Dict[str, Any]:
    """action: BUY_TO_OPEN | SELL_TO_OPEN | BUY_TO_CLOSE | SELL_TO_CLOSE
    order_type: MARKET | LIMIT | STOP | STOP_LIMIT
    NEEDS LIVE VALIDATION -- option order construction (assetType
    OPTION with an OCC symbol) has not been exercised against a real
    Schwab account in this codebase before; schwab_eod.py's own order
    builder only ever placed equity orders."""
    acct = _account_hash()
    if not acct:
        return {"ok": False, "order_id": None, "error": "no Schwab account_hash configured -- connect Schwab first"}
    order: Dict[str, Any] = {
        "session": "NORMAL", "duration": duration.upper(),
        "orderType": order_type.upper(), "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": action.upper(), "quantity": quantity,
            "instrument": {"symbol": occ_symbol, "assetType": "OPTION"},
        }],
    }
    if cancel_time and duration.upper() == "GOOD_TILL_CANCEL":
        order["cancelTime"] = cancel_time
    pricing_error = _apply_order_pricing(order, order_type, price, stop_price)
    if pricing_error:
        return {"ok": False, "order_id": None, "error": pricing_error}
    data, code = _request("POST", f"/trader/v1/accounts/{acct}/orders", payload=order, timeout=30)
    if code in (200, 201, 202) and data.get("ok"):
        loc = (data.get("headers") or {}).get("Location") or (data.get("headers") or {}).get("location") or ""
        order_id = str(loc).rstrip("/").split("/")[-1] if loc else ""
        return {"ok": True, "order_id": order_id, "error": None}
    return {"ok": False, "order_id": None, "error": (data or {}).get("error") or f"HTTP {code}", "detail": (data or {}).get("detail")}


def _leg_instrument(stock_code: str, leg: Dict[str, Any]) -> Dict[str, Any]:
    if leg["instrument_type"] == "stock":
        return {"symbol": stock_code.upper(), "assetType": "EQUITY"}
    occ = to_occ_symbol(stock_code, leg["expiry_date"], leg["right"], leg["strike_price"])
    return {"symbol": occ, "assetType": "OPTION"}


def _classify_complex_order_type(legs: List[Dict[str, Any]]) -> str:
    """Schwab classifies multi-leg combos with a complexOrderStrategyType
    (VERTICAL, IRON_CONDOR, STRADDLE, etc.) that affects margin/
    execution treatment -- sending everything as generic CUSTOM (the
    prior behavior) works but doesn't get the same handling a properly
    classified combo does. Best-effort classification from leg shape;
    falls back to CUSTOM for anything that doesn't match a standard
    pattern."""
    opt_legs = [l for l in legs if l["instrument_type"] == "option"]
    if len(legs) != len(opt_legs) or len(opt_legs) < 2:
        return "CUSTOM"  # any stock leg present, or too few legs, doesn't map to a named options combo
    rights = {l["right"] for l in opt_legs}
    sides = {l["side"] for l in opt_legs}
    if len(opt_legs) == 2:
        if len(rights) == 1 and len(sides) == 2:
            return "VERTICAL"  # one call spread or one put spread
        if rights == {"call", "put"} and len(sides) == 1 and list(sides)[0] == "LONG":
            return "STRADDLE" if opt_legs[0]["strike_price"] == opt_legs[1]["strike_price"] else "STRANGLE"
    if len(opt_legs) == 4 and rights == {"call", "put"}:
        return "IRON_CONDOR"
    return "CUSTOM"


def place_multileg_order(
    stock_code: str, legs: List[Dict[str, Any]], actions: Dict[int, str],
    order_type: str = "NET_CREDIT", net_price: Optional[float] = None,
    duration: str = "DAY", cancel_time: Optional[str] = None,
) -> Dict[str, Any]:
    """Places every leg of a strategy as ONE atomic order (Schwab's
    combo-order support), instead of the sequential single-leg
    approach schwab_vertical_executor.py uses -- this is the real
    upgrade Schwab enables that Breeze (ICICI) never did: no window
    where only some legs are filled, no inter-leg delay, Schwab's own
    matching engine fills the whole spread together or not at all.

    order_type: "NET_CREDIT" or "NET_DEBIT" for a priced combo (most
    common for verticals/condors -- net_price is the total credit/
    debit for the WHOLE combo, not per-leg), or "MARKET" for an
    unpriced combo fill. actions maps each leg's leg_index to its
    Schwab instruction string (BUY_TO_OPEN/SELL_TO_OPEN/etc or BUY/
    SELL_SHORT for a stock leg) -- computed by the caller using the
    same LONG/SHORT-based logic as the sequential executor, just
    applied to every leg in one shot instead of one at a time.

    NEEDS LIVE VALIDATION -- multi-leg combo orders with NET_CREDIT/
    NET_DEBIT pricing have not been exercised against a real Schwab
    account in this codebase; only single-leg equity orders (schwab_
    eod.py) have real trading history behind them.
    """
    acct = _account_hash()
    if not acct:
        return {"ok": False, "order_id": None, "error": "no Schwab account_hash configured -- connect Schwab first"}
    if not legs:
        return {"ok": False, "order_id": None, "error": "at least one leg is required"}

    order_leg_collection = []
    for leg in legs:
        action = actions.get(leg["leg_index"])
        if not action:
            return {"ok": False, "order_id": None, "error": f"no action resolved for leg {leg['leg_index']}"}
        order_leg_collection.append({
            "instruction": action, "quantity": leg["quantity"],
            "instrument": _leg_instrument(stock_code, leg),
        })

    order: Dict[str, Any] = {
        "session": "NORMAL", "duration": duration.upper(),
        "orderType": order_type.upper(), "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": _classify_complex_order_type(legs),
        "orderLegCollection": order_leg_collection,
    }
    if cancel_time and duration.upper() == "GOOD_TILL_CANCEL":
        order["cancelTime"] = cancel_time
    if order_type.upper() in ("NET_CREDIT", "NET_DEBIT") and net_price is not None:
        order["price"] = f"{abs(net_price):.2f}"

    data, code = _request("POST", f"/trader/v1/accounts/{acct}/orders", payload=order, timeout=30)
    if code in (200, 201, 202) and data.get("ok"):
        loc = (data.get("headers") or {}).get("Location") or (data.get("headers") or {}).get("location") or ""
        order_id = str(loc).rstrip("/").split("/")[-1] if loc else ""
        return {"ok": True, "order_id": order_id, "error": None}
    return {"ok": False, "order_id": None, "error": (data or {}).get("error") or f"HTTP {code}", "detail": (data or {}).get("detail")}


def place_oco_exit(
    stock_code: str, legs: List[Dict[str, Any]], close_actions: Dict[int, str],
    target_net_price: float, stop_net_price: float,
    trailing: bool = False, trail_amount: Optional[float] = None, trail_is_percent: bool = False,
) -> Dict[str, Any]:
    """Submits a standalone OCO (One-Cancels-Other) exit for an
    ALREADY-OPEN multi-leg position: one child order closes the whole
    combo at target_net_price (limit), the other closes it at
    stop_net_price (stop) -- whichever fills first cancels the other.
    This hands exit management to Schwab's own order book instead of
    relying purely on this app's 30-second P&L poller, which is
    strictly better for anything that can move between polls (a hard
    stop that only checks every 30s can still blow through a level in
    a fast move; a real broker-side stop order can't be beaten by that
    same gap). The poller still runs as a second, independent check
    (e.g. to catch a strategy-level closing CONDITION an OCO price
    order can't express, like an EMA crossover) -- if it fires and
    closes the position first, cancel the OCO order separately (not
    handled automatically here; see cancel_order).

    order_type shape follows the same TRIGGER/OCO JSON structure
    already proven working for equity brackets in schwab_eod.py,
    adapted to multi-leg orderLegCollections per child instead of a
    single equity leg. NEEDS LIVE VALIDATION for the options-combo
    case specifically.
    """
    acct = _account_hash()
    if not acct:
        return {"ok": False, "order_id": None, "error": "no Schwab account_hash configured -- connect Schwab first"}

    def _leg_collection():
        return [{"instruction": close_actions.get(leg["leg_index"]), "quantity": leg["quantity"],
                 "instrument": _leg_instrument(stock_code, leg)} for leg in legs]

    if trailing and trail_amount is not None:
        # Trailing stop -- the trigger price RE-BASES as the combo's
        # mark moves favorably, only locking in once it reverses by
        # trail_amount. Schwab expresses this via stopPriceLinkBasis/
        # stopPriceLinkType/stopPriceOffset instead of a fixed
        # stopPrice. NEEDS LIVE VALIDATION for the multi-leg case.
        stop_leg: Dict[str, Any] = {
            "session": "NORMAL", "duration": "GOOD_TILL_CANCEL",
            "orderType": "TRAILING_STOP",
            "stopPriceLinkBasis": "MARK",
            "stopPriceLinkType": "PERCENT" if trail_is_percent else "VALUE",
            "stopPriceOffset": abs(trail_amount),
            "orderStrategyType": "SINGLE", "orderLegCollection": _leg_collection(),
        }
    else:
        stop_leg = {
            "session": "NORMAL", "duration": "GOOD_TILL_CANCEL",
            "orderType": "STOP",
            "stopPrice": f"{abs(stop_net_price):.2f}",
            "orderStrategyType": "SINGLE", "orderLegCollection": _leg_collection(),
        }

    order: Dict[str, Any] = {
        "orderStrategyType": "OCO",
        "childOrderStrategies": [
            {
                "session": "NORMAL", "duration": "GOOD_TILL_CANCEL",
                "orderType": "NET_CREDIT" if target_net_price >= 0 else "NET_DEBIT",
                "price": f"{abs(target_net_price):.2f}",
                "orderStrategyType": "SINGLE", "orderLegCollection": _leg_collection(),
            },
            stop_leg,
        ],
    }
    data, code = _request("POST", f"/trader/v1/accounts/{acct}/orders", payload=order, timeout=30)
    if code in (200, 201, 202) and data.get("ok"):
        loc = (data.get("headers") or {}).get("Location") or (data.get("headers") or {}).get("location") or ""
        order_id = str(loc).rstrip("/").split("/")[-1] if loc else ""
        return {"ok": True, "order_id": order_id, "error": None}
    return {"ok": False, "order_id": None, "error": (data or {}).get("error") or f"HTTP {code}", "detail": (data or {}).get("detail")}


_TERMINAL_STATUSES = {"filled", "executed", "canceled", "cancelled", "rejected", "expired", "replaced"}


def get_open_orders(days_back: int = 7) -> Dict[str, Any]:
    """Every order at the broker that hasn't reached a terminal state
    (filled/canceled/rejected/expired) -- this is what makes a GTC
    limit order sitting unfilled for days actually visible in the app,
    instead of only ever showing up once it fills."""
    from datetime import datetime as _dt, timedelta as _td
    acct = _account_hash()
    if not acct:
        return {"ok": False, "orders": [], "error": "no Schwab account_hash configured -- connect Schwab first"}
    from_date = (_dt.now() - _td(days=days_back)).strftime("%Y-%m-%dT00:00:00.000Z")
    to_date = (_dt.now() + _td(days=1)).strftime("%Y-%m-%dT00:00:00.000Z")
    data, code = _request("GET", f"/trader/v1/accounts/{acct}/orders",
                           params={"fromEnteredTime": from_date, "toEnteredTime": to_date, "maxResults": 200})
    if code != 200 or not data.get("ok"):
        return {"ok": False, "orders": [], "error": (data or {}).get("error") or f"HTTP {code}"}
    raw = data.get("data") or []
    if not isinstance(raw, list):
        raw = [raw]
    open_orders = []
    for o in raw:
        status = (o.get("status") or "").strip().lower()
        if status in _TERMINAL_STATUSES:
            continue
        legs = o.get("orderLegCollection") or []
        symbols = [leg.get("instrument", {}).get("symbol", "") for leg in legs]
        open_orders.append({
            "order_id": str(o.get("orderId") or ""),
            "status": o.get("status"),
            "order_type": o.get("orderType"),
            "duration": o.get("duration"),
            "price": o.get("price"),
            "quantity": o.get("quantity"),
            "filled_quantity": o.get("filledQuantity"),
            "symbols": symbols,
            "entered_time": o.get("enteredTime"),
            "cancel_time": o.get("cancelTime"),
        })
    return {"ok": True, "orders": open_orders, "error": None}


def cancel_order(order_id: str) -> Dict[str, Any]:
    acct = _account_hash()
    if not acct:
        return {"ok": False, "error": "no Schwab account_hash configured"}
    data, code = _request("DELETE", f"/trader/v1/accounts/{acct}/orders/{order_id}")
    return {"ok": code in (200, 201, 204), "error": None if code in (200, 201, 204) else ((data or {}).get("error") or f"HTTP {code}")}


_FILLED_STATUSES = {"filled", "executed"}
_REJECTED_STATUSES = {"rejected", "canceled", "expired"}


def get_order_status(order_id: str) -> Dict[str, Any]:
    acct = _account_hash()
    if not acct:
        return {"ok": False, "status": None, "error": "no Schwab account_hash configured"}
    data, code = _request("GET", f"/trader/v1/accounts/{acct}/orders/{order_id}")
    if code != 200 or not data.get("ok"):
        return {"ok": False, "status": None, "error": (data or {}).get("error") or f"HTTP {code}"}
    payload = data.get("data") or {}
    return {"ok": True, "status": payload.get("status"), "average_price": payload.get("price"), "raw": payload}


def poll_order_fill(order_id: str, timeout_sec: float = 5.0, poll_interval_sec: float = 0.3) -> Dict[str, Any]:
    import time as _time
    start = _time.time()
    last_status = None
    while _time.time() - start < timeout_sec:
        result = get_order_status(order_id)
        if not result["ok"]:
            _time.sleep(poll_interval_sec)
            continue
        status = (result.get("status") or "").strip().lower()
        last_status = status
        if status in _FILLED_STATUSES:
            return {"ok": True, "filled": True, "status": status, "average_price": result.get("average_price"),
                    "elapsed_sec": round(_time.time() - start, 2), "error": None}
        if status in _REJECTED_STATUSES:
            return {"ok": True, "filled": False, "status": status, "average_price": None,
                    "elapsed_sec": round(_time.time() - start, 2), "error": f"order was {status}, not filled"}
        _time.sleep(poll_interval_sec)
    return {"ok": True, "filled": False, "status": last_status, "average_price": None,
            "elapsed_sec": round(_time.time() - start, 2),
            "error": f"fill not confirmed within {timeout_sec}s (last status: {last_status or 'unknown'})"}
