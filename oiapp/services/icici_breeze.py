"""icici_breeze.py -- V113 addition.

Thin wrapper around ICICI Direct's Breeze Connect API for options order
placement. Built against Breeze's documented REST/SDK shape
(stock_code, exchange_code="NFO", product="options", right="call"/
"put", strike_price, expiry_date, action="buy"/"sell", order_type,
quantity, price, validity).

IMPORTANT -- needs live validation: this was written against Breeze's
published API docs, not tested against a live session (no credentials
available in this environment). Before trusting this with real orders,
run it once against a known small/cheap contract in isolation and
confirm the request/response shapes match what's coded here -- Breeze's
exact field names have changed between SDK versions in the past.

Auth flow (per Breeze docs, NOT a full OAuth): api_key + api_secret are
static credentials from the ICICI Direct developer portal. The session
token, however, is obtained by manually logging into a Breeze login URL
in a browser and copying a token from the redirect URL -- it is not
something this backend can obtain purely programmatically, and Breeze
sessions typically expire daily. `session_token` is therefore stored
as a value the user pastes in via the UI each trading day, not
something auto-refreshed like the Schwab OAuth flow elsewhere in this
app.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

# Credentials now stored in the `icici_config` DB table (same pattern
# as schwab_config -- see oiapp/schwab/schwab_routes.py) so they can be
# set from the UI instead of requiring env vars. Env vars are still
# checked as a fallback for anyone who prefers that route.
_ENV_API_KEY = os.environ.get("ICICI_BREEZE_API_KEY", "")
_ENV_API_SECRET = os.environ.get("ICICI_BREEZE_API_SECRET", "")


def _db_path() -> str:
    from ..db import DB_PATH
    return DB_PATH


def _ensure_config_table() -> None:
    con = sqlite3.connect(_db_path())
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS icici_config (
                id INTEGER PRIMARY KEY, api_key TEXT, api_secret TEXT,
                session_token TEXT, session_token_updated TEXT,
                live_trading_enabled INTEGER, updated TEXT
            )
        """)
        cols = {r[1] for r in con.execute("PRAGMA table_info(icici_config)").fetchall()}
        if "live_trading_enabled" not in cols:
            # Default 1 (live) -- matches the app's existing "live-capable
            # by default, no dry-run gate" behavior, so this toggle only
            # changes anything once someone deliberately flips it off.
            con.execute("ALTER TABLE icici_config ADD COLUMN live_trading_enabled INTEGER NOT NULL DEFAULT 1")
        con.commit()
    finally:
        con.close()


def is_live_trading_enabled() -> bool:
    _ensure_config_table()
    cfg = get_config() or {}
    val = cfg.get("live_trading_enabled")
    return True if val is None else bool(val)


def set_live_trading_enabled(enabled: bool) -> None:
    save_config(live_trading_enabled=int(bool(enabled)))


def is_nse_market_hours() -> bool:
    """Simple NSE market-hours gate (9:15 AM - 3:30 PM IST, Mon-Fri, no
    holiday calendar) -- used to stop the background P&L monitor and
    strategy evaluator from polling Breeze every 30-60s around the
    clock during a multi-day unattended run. Not used for any trading
    decision itself, only to reduce needless API calls / log noise
    outside hours when there's nothing tradeable happening anyway.
    Fails OPEN (returns True) if the timezone lookup itself fails, so
    a missing tzdata package makes the jobs run a bit more than
    necessary rather than silently stop running at all.
    """
    try:
        from zoneinfo import ZoneInfo
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return True
    if now_ist.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now_ist <= market_close


def get_config() -> Optional[Dict[str, Any]]:
    _ensure_config_table()
    con = sqlite3.connect(_db_path())
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM icici_config WHERE id=1").fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def save_config(**kw) -> None:
    _ensure_config_table()
    con = sqlite3.connect(_db_path())
    try:
        if con.execute("SELECT id FROM icici_config WHERE id=1").fetchone():
            sets = ", ".join(f"{k}=?" for k in kw)
            con.execute(f"UPDATE icici_config SET {sets}, updated=? WHERE id=1",
                        (*kw.values(), datetime.now().isoformat()))
        else:
            cols = ", ".join(kw.keys()) + ", updated"
            vals = ", ".join("?" * (len(kw) + 1))
            con.execute(f"INSERT INTO icici_config (id, {cols}) VALUES (1, {vals})",
                        (*kw.values(), datetime.now().isoformat()))
        con.commit()
    finally:
        con.close()


def _api_key() -> str:
    cfg = get_config() or {}
    return cfg.get("api_key") or _ENV_API_KEY


def _api_secret() -> str:
    cfg = get_config() or {}
    return cfg.get("api_secret") or _ENV_API_SECRET


_session_token: Optional[str] = None
_breeze_client = None


def _to_breeze_expiry(date_str: str) -> str:
    """Breeze's ACTUAL native expiry format, confirmed live: their own
    get_portfolio_positions() returns expiry_date as "28-Jul-2026"
    (DD-Mon-YYYY) -- not ISO-8601 datetime as originally guessed. That
    earlier guess was actively wrong: appending "T06:00:00.000Z" to an
    already-DD-Mon-YYYY string produced garbage like
    "28-Jul-2026T06:00:00.000Z", which is exactly why Breeze's API
    logged "Expiry-Date cannot be empty" -- it couldn't parse that
    string at all and treated it as blank.

    This now: passes through unchanged if already DD-Mon-YYYY (the
    native format, e.g. values read straight from
    get_portfolio_positions and stored on adopted/opened positions),
    and converts from ISO "YYYY-MM-DD" (what an HTML <input
    type="date"> or a plain date.isoformat() produces) into
    DD-Mon-YYYY otherwise.
    """
    import re
    s = (date_str or "").strip()
    if not s:
        return s
    if re.match(r"^\d{1,2}-[A-Za-z]{3}-\d{4}$", s):
        return s  # already Breeze's native format
    candidate = s.split("T")[0] if "T" in s else s
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(candidate, fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    # Unrecognized format -- return unchanged rather than guessing
    # further; whatever error Breeze returns for this will at least be
    # visible via the debug/error surfacing added in v121.
    return s


def set_session_token(token: str) -> Dict[str, Any]:
    """Called once per trading day after the user pastes in a fresh
    session token from the Breeze login redirect. Establishes the
    underlying breeze_connect client AND saves the token to the DB so
    an app restart can attempt to reuse it (see try_reconnect) instead
    of forcing a re-paste every time -- Breeze sessions still expire
    on their own schedule (typically daily), so this isn't a
    substitute for a fresh token once the old one has actually
    expired, just a way to survive a plain app restart on the same
    trading day."""
    global _session_token, _breeze_client
    token = (token or "").strip()
    if not token:
        return {"ok": False, "error": "empty session token"}
    api_key, api_secret = _api_key(), _api_secret()
    if not api_key or not api_secret:
        return {"ok": False, "error": "Breeze API key/secret not configured -- set them in the Breeze API Credentials panel above"}
    try:
        from breeze_connect import BreezeConnect  # pip install breeze-connect
    except ImportError:
        return {"ok": False, "error": "breeze-connect package not installed -- pip install breeze-connect"}
    try:
        client = BreezeConnect(api_key=api_key)
        client.generate_session(api_secret=api_secret, session_token=token)
        _breeze_client = client
        _session_token = token
        save_config(session_token=token, session_token_updated=datetime.now().isoformat())
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"session generation failed: {e}"}


def try_reconnect() -> Dict[str, Any]:
    """Attempt to re-establish the Breeze session from whatever was
    last saved to the DB -- called automatically on first use after an
    app restart, and explicitly by the UI's "Connect" button. If the
    saved token has actually expired (not just "app restarted"),
    Breeze's generate_session() call will fail and this returns a
    clear "needs a fresh token" error rather than leaving the caller
    guessing why nothing is happening."""
    global _session_token, _breeze_client
    if _breeze_client is not None:
        return {"ok": True, "already_connected": True}
    cfg = get_config() or {}
    saved_token = cfg.get("session_token")
    if not saved_token:
        return {"ok": False, "error": "no saved session token -- paste one below", "needs_token": True}
    result = set_session_token(saved_token)
    if not result["ok"]:
        result["needs_token"] = True
        result["error"] = f"saved session token no longer valid ({result.get('error')}) -- please paste a fresh one"
    return result


def is_session_active() -> bool:
    if _breeze_client is not None:
        return True
    # Lazy auto-restore: an app restart clears the in-memory client but
    # not the saved token, so try once before reporting "inactive".
    try_reconnect()
    return _breeze_client is not None


def session_status() -> Dict[str, Any]:
    """Richer status than the plain boolean -- distinguishes
    not-configured / expired-needs-refresh / active, since "inactive"
    alone doesn't tell the user which of those it is."""
    cfg = get_config() or {}
    if not cfg.get("api_key") or not cfg.get("api_secret"):
        return {"active": False, "state": "not_configured", "message": "Enter API Key/Secret above first."}
    active = is_session_active()
    if active:
        return {"active": True, "state": "active", "message": "Session active.",
                "session_token_updated": cfg.get("session_token_updated")}
    if cfg.get("session_token"):
        return {"active": False, "state": "expired", "message": "Saved session token is no longer valid -- paste a fresh one.",
                "session_token_updated": cfg.get("session_token_updated")}
    return {"active": False, "state": "no_token", "message": "No session token yet -- paste today's token below."}


def _client():
    if _breeze_client is None:
        try_reconnect()
    if _breeze_client is None:
        raise RuntimeError("No active Breeze session -- click Connect or paste today's session token")
    return _breeze_client


def get_quote(stock_code: str, expiry_date: str, right: str, strike_price: float, _retries: int = 2) -> Dict[str, Any]:
    """Live quote for a single options contract. `right` is "call" or
    "put". Returns {"ok": bool, "ltp": float|None, "error": str|None}.

    Retries once (with a short delay) on what looks like a transient
    empty-response failure -- confirmed live: calling get_quote() for
    two legs of the same position back-to-back with no delay, the
    first succeeded and the second failed with "Expecting value: line
    1 column 1 (char 0)", a JSON-decode error on an empty response
    body. That pattern (works once, fails immediately after) is the
    classic signature of hitting a per-second rate limit, not a
    parameter problem -- so a short pause + retry is the right fix
    rather than a request-shape change.
    """
    import time as _time
    last_error = None
    for attempt in range(_retries + 1):
        try:
            c = _client()
            resp = c.get_quotes(
                stock_code=stock_code,
                exchange_code="NFO",
                expiry_date=_to_breeze_expiry(expiry_date),
                product_type="options",
                right=right,
                strike_price=str(strike_price),
            )
            rows = (resp or {}).get("Success") or []
            if not rows:
                last_error = (resp or {}).get("Error") or "no quote data returned"
                if attempt < _retries:
                    _time.sleep(0.5)
                    continue
                return {"ok": False, "ltp": None, "error": last_error}
            ltp = float(rows[0].get("ltp") or rows[0].get("last_traded_price") or 0)
            return {"ok": True, "ltp": ltp, "error": None, "raw": rows[0]}
        except Exception as e:
            last_error = str(e)
            if attempt < _retries:
                _time.sleep(0.5)
                continue
            return {"ok": False, "ltp": None, "error": last_error}
    return {"ok": False, "ltp": None, "error": last_error}


def place_order(
    stock_code: str,
    expiry_date: str,
    right: str,           # "call" | "put"
    strike_price: float,
    action: str,           # "buy" | "sell"
    quantity: int,
    order_type: str = "market",
    price: float = 0.0,
    validity: str = "day",
) -> Dict[str, Any]:
    """Single-leg order placement -- the building block the leg
    sequencer (vertical_executor.py) calls once per leg, in the
    correct safe order. Never call this directly for a multi-leg
    strategy; go through vertical_executor so legs are sequenced
    correctly."""
    try:
        c = _client()
        resp = c.place_order(
            stock_code=stock_code,
            exchange_code="NFO",
            product="options",
            action=action,
            order_type=order_type,
            stoploss="",
            quantity=str(quantity),
            price=str(price) if order_type == "limit" else "",
            validity=validity,
            expiry_date=_to_breeze_expiry(expiry_date),
            right=right,
            strike_price=str(strike_price),
        )
        success = (resp or {}).get("Success") or {}
        order_id = success.get("order_id")
        if not order_id:
            return {"ok": False, "order_id": None, "error": (resp or {}).get("Error") or "no order_id in response", "raw": resp}
        return {"ok": True, "order_id": order_id, "error": None, "raw": resp}
    except Exception as e:
        return {"ok": False, "order_id": None, "error": str(e)}


def get_order_status(order_id: str) -> Dict[str, Any]:
    try:
        c = _client()
        resp = c.get_order_detail(exchange_code="NFO", order_id=order_id)
        rows = (resp or {}).get("Success") or []
        if not rows:
            return {"ok": False, "status": None, "error": (resp or {}).get("Error") or "no order detail returned"}
        row = rows[0]
        return {
            "ok": True, "error": None,
            "status": row.get("status"),
            "average_price": row.get("average_price"),
            "raw": row,
        }
    except Exception as e:
        return {"ok": False, "status": None, "error": str(e)}


# Statuses observed in Breeze docs/community reports that mean "filled".
# NEEDS LIVE VALIDATION -- confirm the exact string(s) your account
# actually returns for a completed market order and adjust this set if
# it doesn't match (e.g. via a manual get_order_status() call on a
# known-filled order id).
_FILLED_STATUSES = {"executed", "complete", "completed", "filled", "fully executed"}
_REJECTED_STATUSES = {"rejected", "cancelled", "canceled"}


def poll_order_fill(order_id: str, timeout_sec: float = 5.0, poll_interval_sec: float = 0.3) -> Dict[str, Any]:
    """Poll get_order_status() until the order is actually FILLED (not
    just accepted), or until timeout_sec elapses. This is what
    vertical_executor's safe-sequential mode calls between legs so
    "leg 1 succeeded" means "leg 1 is actually done", not just "the
    broker accepted the order" -- an accepted market order on a liquid
    index option normally fills in well under a second, but nothing
    guarantees it, and proceeding to a SHORT-buyback-then-LONG-sell
    close sequence on an unconfirmed fill defeats the point of
    sequencing at all.

    Returns {"ok": bool, "filled": bool, "status": str|None,
             "average_price": float|None, "elapsed_sec": float,
             "error": str|None}
    """
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
            return {
                "ok": True, "filled": True, "status": status,
                "average_price": result.get("average_price"),
                "elapsed_sec": round(_time.time() - start, 2), "error": None,
            }
        if status in _REJECTED_STATUSES:
            return {
                "ok": True, "filled": False, "status": status, "average_price": None,
                "elapsed_sec": round(_time.time() - start, 2),
                "error": f"order was {status}, not filled",
            }
        _time.sleep(poll_interval_sec)
    return {
        "ok": True, "filled": False, "status": last_status, "average_price": None,
        "elapsed_sec": round(_time.time() - start, 2),
        "error": f"fill not confirmed within {timeout_sec}s (last status: {last_status or 'unknown'})",
    }


def get_portfolio_positions() -> Dict[str, Any]:
    """Live positions currently held at the broker -- includes anything
    open in the account, whether opened through this app or not (e.g.
    placed manually, or from before this system existed). Used to
    reconcile against our own tracked `icici_auto_positions` table so
    untracked positions can be surfaced and optionally adopted for
    monitoring/auto-close.

    NEEDS LIVE VALIDATION: Breeze's `get_portfolio_positions` response
    shape (quantity sign convention for long vs short in particular)
    is taken from their docs, not confirmed against a live account --
    confirmed WRONG in practice for at least one real vertical (both
    legs of a NIFTY put spread came back "LONG" when one should have
    been SHORT). Since the sign can't be trusted, `side` here is a
    best-effort GUESS only -- the UI lets the user correct it per leg
    before adopting, rather than silently trusting it.
    """
    try:
        c = _client()
        resp = c.get_portfolio_positions()
        rows = (resp or {}).get("Success") or []
        out = []
        for idx, r in enumerate(rows):
            if idx > 0:
                import time as _time
                _time.sleep(0.3)  # same rate-limit spacing as compute_combined_pnl_rupees
            try:
                qty = int(float(r.get("quantity") or 0))
            except Exception:
                qty = 0
            right = (r.get("right") or "").lower() or None
            strike = float(r.get("strike_price") or 0) if r.get("strike_price") else None
            stock_code = r.get("stock_code")
            expiry_date = r.get("expiry_date")
            ltp = None
            ltp_error = None
            if right and strike and stock_code and expiry_date:
                q = get_quote(stock_code, expiry_date, right, strike)
                if q.get("ok"):
                    ltp = q.get("ltp")
                else:
                    ltp_error = q.get("error")  # was silently discarded before -- this is what "no quote" actually meant
            else:
                ltp_error = "missing right/strike/stock_code/expiry_date on this leg"
            out.append({
                "stock_code": stock_code,
                "expiry_date": expiry_date,
                "right": right,
                "strike_price": strike,
                "quantity": qty,
                # GUESSED -- do not trust blindly, see docstring. UI
                # exposes this as an editable dropdown before adopt.
                "side": "LONG" if qty >= 0 else "SHORT",
                "side_is_guess": True,
                "average_price": float(r.get("average_price") or 0),
                "ltp": ltp,
                "ltp_error": ltp_error,
                "product_type": r.get("product_type"),
                "raw": r,
            })
        return {"ok": True, "positions": out, "error": None}
    except Exception as e:
        return {"ok": False, "positions": [], "error": str(e)}


def get_available_expiries(stock_code: str) -> Dict[str, Any]:
    """List of valid expiry dates Breeze actually has data for, for
    this symbol -- used to populate a dropdown instead of a free-text
    date picker where someone can type a date with no real expiry on
    it.

    CONFIRMED LIVE WRONG (round 1): calling get_option_chain_quotes()
    with right="call" and no expiry_date/strike_price at all returned
    zero rows and "no expiries found in response" -- omitting the
    parameter entirely isn't the same as "give me every expiry" the
    way it was for the "right or strike_price required" case.

    Attempt 2 here: pass expiry_date="" explicitly (empty string,
    not omitted) -- a common "wildcard/all" convention in broker APIs,
    but STILL UNVALIDATED. If this also comes back empty, the raw
    Error field from Breeze is returned so the actual reason is
    visible instead of another silent guess -- see the Diagnostics
    tab's SDK-signature introspection check for ground truth on what
    this method actually expects.
    """
    try:
        c = _client()
        resp = c.get_option_chain_quotes(
            stock_code=stock_code, exchange_code="NFO", product_type="options",
            right="call", expiry_date="",
        )
        rows = (resp or {}).get("Success") or []
        raw_error = (resp or {}).get("Error")
        expiries = sorted({r.get("expiry_date") for r in rows if r.get("expiry_date")})
        # Breeze's native format is "28-Jul-2026" (confirmed live) --
        # normalize defensively in case an ISO datetime ever comes
        # back instead, but otherwise pass through as-is.
        normalized = sorted({e.split("T")[0] for e in expiries if e})
        return {"ok": bool(normalized), "expiries": normalized,
                "error": None if normalized else (raw_error or "no expiries found in response (no Error field from Breeze either)")}
    except Exception as e:
        return {"ok": False, "expiries": [], "error": str(e)}


def get_spot_price(stock_code: str) -> Dict[str, Any]:
    """Underlying spot/cash price for `stock_code` -- separate from
    get_quote() which is options-specific (needs right/strike/expiry).
    Uses exchange_code="NSE" (cash market) instead of "NFO".

    NEEDS LIVE VALIDATION: NSE cash-market quote call shape assumed
    from Breeze's general get_quotes() docs.
    """
    try:
        c = _client()
        resp = c.get_quotes(stock_code=stock_code, exchange_code="NSE", product_type="cash")
        rows = (resp or {}).get("Success") or []
        if not rows:
            return {"ok": False, "spot": None, "error": (resp or {}).get("Error") or "no spot data returned"}
        row = rows[0] if isinstance(rows, list) else rows
        spot = float(row.get("ltp") or row.get("last_traded_price") or 0)
        return {"ok": True, "spot": spot, "error": None}
    except Exception as e:
        return {"ok": False, "spot": None, "error": str(e)}


def get_option_chain(stock_code: str, expiry_date: str) -> Dict[str, Any]:
    """Full option chain for a symbol/expiry -- strike, right, LTP, OI,
    and change-in-OI if Breeze's response includes it natively (field
    name varies by provider; checks a few likely names and falls back
    to null with a note if none are present, rather than guessing).

    Confirmed live: Breeze's get_option_chain_quotes() rejects a call
    with BOTH right and strike_price empty ("Either Right or
    Strike-Price cannot be empty") -- so a single "give me the whole
    chain" call isn't valid. Fixed by making two calls instead, one
    per `right` with strike_price left blank -- that satisfies "at
    least one of the two, not empty" while still returning every
    strike for that side, then the two sides get merged here.
    """
    try:
        c = _client()
        converted_expiry = _to_breeze_expiry(expiry_date)
        all_rows: List[Dict[str, Any]] = []
        errors: List[str] = []
        last_resp = None
        for right in ("call", "put"):
            resp = c.get_option_chain_quotes(
                stock_code=stock_code, exchange_code="NFO",
                product_type="options", expiry_date=converted_expiry, right=right,
            )
            last_resp = resp
            rows = (resp or {}).get("Success") or []
            if not rows and (resp or {}).get("Error"):
                errors.append(f"{right}: {(resp or {}).get('Error')}")
                continue
            all_rows.extend(rows)

        out = []
        oi_change_missing = False
        for r in all_rows:
            oi_change = r.get("oi_change")
            if oi_change is None:
                oi_change = r.get("change_in_oi")
            if oi_change is None:
                oi_change = r.get("chnge_oi")
            if oi_change is None:
                oi_change_missing = True
            out.append({
                "right": (r.get("right") or "").lower(),
                "strike_price": float(r.get("strike_price") or 0),
                "ltp": float(r.get("ltp") or r.get("last_traded_price") or 0),
                "open_interest": int(float(r.get("open_interest") or 0)),
                "oi_change": int(float(oi_change)) if oi_change is not None else None,
                "volume": int(float(r.get("total_quantity_traded") or r.get("volume") or 0)),
            })
        result = {"ok": True, "rows": out, "oi_change_available": not oi_change_missing, "error": "; ".join(errors) or None}
        if not out:
            result["debug"] = {
                "sent_stock_code": stock_code,
                "sent_expiry_raw": expiry_date,
                "sent_expiry_converted": converted_expiry,
                "per_side_errors": errors,
                "response_top_level_keys": list((last_resp or {}).keys()),
                "response_error_field": (last_resp or {}).get("Error"),
                "response_success_field_type": type((last_resp or {}).get("Success")).__name__,
            }
        return result
    except Exception as e:
        return {"ok": False, "rows": [], "oi_change_available": False, "error": str(e)}
