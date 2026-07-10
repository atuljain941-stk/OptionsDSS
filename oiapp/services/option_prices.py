# oiapp/services/option_prices.py
"""
Fetch bid/ask/last prices for option strategies.

v79 hardening:
- Uses the local options DB first by default so journal/alerts/strategy refreshes do not
  repeatedly hit yfinance or fail on provider-side NaN conversion bugs.
- Falls back to yfinance only when the local snapshot is unavailable or explicitly
  requested with OPTION_PRICES_PREFER_LIVE=1.
- Sanitizes every numeric value before int/float conversion.
- Logs yfinance failures once per symbol/expiry/error TTL instead of spamming the console.
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import yfinance as yf  # type: ignore
except Exception:  # pragma: no cover - environments without yfinance
    yf = None

try:
    from ..db import DB_PATH as _APP_DB_PATH
except Exception:  # pragma: no cover
    _APP_DB_PATH = Path(__file__).resolve().parents[2] / "options_data.db"

_cache: Dict[str, Tuple[dict, float]] = {}
_TTL = 300  # standard local/strategy cache
_log_cache: Dict[str, float] = {}
_LOG_TTL = 300
_chain_source: Dict[str, str] = {}


def _ck(symbol: str, expiry: str, mode: str = "auto") -> str:
    return f"chain:{str(symbol).upper()}:{expiry}:{mode}"


def _get(key: str):
    e = _cache.get(key)
    return e[0] if e and time.time() < e[1] else None


def _set(key: str, val: dict, source: Optional[str] = None, ttl: Optional[int] = None):
    _cache[key] = (val, time.time() + (int(ttl) if ttl is not None else _TTL))
    if source:
        _chain_source[key] = source
    return val


def _source(key: str) -> str:
    return _chain_source.get(key, "unknown")


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


def _safe_float(v: Any, default=None, ndigits: Optional[int] = None):
    """Return a finite float or default. Handles pandas/numpy NaN/NA from yfinance."""
    try:
        # pandas.NA raises on float(); numpy.nan converts to nan.
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return round(f, ndigits) if ndigits is not None else f
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    """Return a finite integer or default. yfinance often returns NaN for OI/volume."""
    f = _safe_float(v, None)
    if f is None:
        return int(default)
    try:
        return int(f)
    except Exception:
        return int(default)


def _safe_price(v: Any):
    f = _safe_float(v, None)
    if f is None or f <= 0:
        return None
    return round(f, 2)


def _safe_strike(v: Any):
    f = _safe_float(v, None)
    if f is None or f <= 0:
        return None
    # Keep .5 strikes stable while avoiding excessive binary-float noise.
    return round(f, 4)


def _norm_type(v: Any) -> Optional[str]:
    s = str(v or "").strip().lower()
    if s in {"c", "call", "calls"}:
        return "call"
    if s in {"p", "put", "puts"}:
        return "put"
    return None


def _mid_from(bid: Any, ask: Any, last: Any = None, price: Any = None):
    b = _safe_price(bid)
    a = _safe_price(ask)
    if b is not None and a is not None and b > 0 and a > 0:
        return round((b + a) / 2, 2)
    l = _safe_price(last)
    if l is not None:
        return l
    p = _safe_price(price)
    if p is not None:
        return p
    return None


def _log_once(message: str):
    now = time.time()
    key = message[:240]
    if now < _log_cache.get(key, 0):
        return
    _log_cache[key] = now + _LOG_TTL
    print(message)


def _db_path() -> Path:
    try:
        return Path(_APP_DB_PATH)
    except Exception:
        return Path(__file__).resolve().parents[2] / "options_data.db"


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]).lower() for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _fetch_chain_from_db(symbol: str, expiry: str) -> dict:
    """Return latest local option snapshot keyed by (type, strike)."""
    path = _db_path()
    if not path.exists():
        return {}
    symbol = str(symbol or "").upper().strip()
    if not symbol or not expiry:
        return {}
    con = None
    try:
        con = sqlite3.connect(path, timeout=10)
        con.row_factory = sqlite3.Row
        cols = _table_columns(con, "options")
        if not cols:
            return {}
        snap = con.execute(
            "SELECT MAX(date) AS d FROM options WHERE UPPER(symbol)=? AND expiration=?",
            (symbol, expiry),
        ).fetchone()
        snap_date = snap["d"] if snap and snap["d"] else None
        if not snap_date:
            return {}

        # Build a SELECT compatible with older DBs that may not have bid/ask/last/iv.
        select_cols = ["type", "strike"]
        for col in ("price", "bid", "ask", "last", "iv", "oi", "volume", "date"):
            if col in cols:
                select_cols.append(col)
        rows = con.execute(
            f"SELECT {', '.join(select_cols)} FROM options WHERE UPPER(symbol)=? AND expiration=? AND date=?",
            (symbol, expiry, snap_date),
        ).fetchall()

        result = {}
        for r in rows:
            side = _norm_type(r["type"] if "type" in r.keys() else None)
            strike = _safe_strike(r["strike"] if "strike" in r.keys() else None)
            if not side or strike is None:
                continue
            bid = _safe_price(r["bid"]) if "bid" in r.keys() else None
            ask = _safe_price(r["ask"]) if "ask" in r.keys() else None
            last = _safe_price(r["last"]) if "last" in r.keys() else None
            price = _safe_price(r["price"]) if "price" in r.keys() else None
            mid = _mid_from(bid, ask, last, price)
            key = (side, strike)
            existing = result.get(key)
            item = {
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "iv": _safe_float(r["iv"], None) if "iv" in r.keys() else None,
                "oi": _safe_int(r["oi"], 0) if "oi" in r.keys() else 0,
                "volume": _safe_int(r["volume"], 0) if "volume" in r.keys() else 0,
                "snapshot": snap_date,
                "source": "local options DB",
            }
            if existing:
                # Defensive aggregation if duplicate rows exist for the same strike/date.
                existing["oi"] = _safe_int(existing.get("oi"), 0) + item["oi"]
                existing["volume"] = _safe_int(existing.get("volume"), 0) + item["volume"]
                for fld in ("bid", "ask", "mid", "iv"):
                    if existing.get(fld) is None and item.get(fld) is not None:
                        existing[fld] = item[fld]
            else:
                result[key] = item
        return result
    except Exception as e:
        _log_once(f"[option_prices][db] {symbol}/{expiry}: {e}")
        return {}
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def _iter_option_rows(df):
    """Yield rows from a yfinance DataFrame safely."""
    if df is None:
        return
    try:
        if getattr(df, "empty", False):
            return
    except Exception:
        pass
    try:
        # Convert provider NaNs to None before row access when pandas is present.
        try:
            df = df.where(df.notna(), None)
        except Exception:
            pass
        for _, row in df.iterrows():
            yield row
    except Exception:
        return


def _fetch_chain_from_yfinance(symbol: str, expiry: str) -> dict:
    if yf is None:
        return {}
    try:
        chain = yf.Ticker(symbol).option_chain(expiry)
        result = {}
        for side, df in [("call", getattr(chain, "calls", None)), ("put", getattr(chain, "puts", None))]:
            for row in _iter_option_rows(df) or []:
                try:
                    strike = _safe_strike(row.get("strike"))
                    if strike is None:
                        continue
                    bid = _safe_price(row.get("bid"))
                    ask = _safe_price(row.get("ask"))
                    last = _safe_price(row.get("lastPrice"))
                    mid = _mid_from(bid, ask, last, None)
                    result[(side, strike)] = {
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "iv": _safe_float(row.get("impliedVolatility"), None),
                        "oi": _safe_int(row.get("openInterest"), 0),
                        "volume": _safe_int(row.get("volume"), 0),
                        "snapshot": None,
                        "source": "yfinance bid/ask mid-price",
                    }
                except Exception as row_exc:
                    _log_once(f"[option_prices][row] {symbol}/{expiry}: skipped bad row: {row_exc}")
                    continue
        return result
    except Exception as e:
        # yfinance can throw internally before we see rows, including provider NaN -> int conversion.
        # The known NaN conversion issue is common during background Journal/Alert
        # refreshes; silently fall back unless explicit logging is enabled.
        msg = str(e or "")
        if _env_bool("OPTION_PRICES_LOG_YF_ERRORS", False):
            _log_once(f"[option_prices][yfinance] {symbol}/{expiry}: {msg or 'live provider chain unavailable'}; using local DB/estimated pricing if available")
        return {}


def fetch_chain(symbol: str, expiry: str, *, prefer_live: Optional[bool] = None, db_first: Optional[bool] = None, use_cache: bool = True) -> dict:
    """
    Returns dict keyed by (type, strike) -> {bid, ask, mid, iv, oi, volume}.
    type: 'call' | 'put'

    Default remains DB-first to keep strategy/dashboard scans fast. Journal and
    alert paths should call prefer_live=True so current marks come from live
    bid/ask when available, with DB fallback if yfinance fails.
    """
    symbol = str(symbol or "").upper().strip()
    if prefer_live is None:
        prefer_live = _env_bool("OPTION_PRICES_PREFER_LIVE", False)
    if db_first is None:
        db_first = _env_bool("OPTION_PRICES_DB_FIRST", True)
    mode = "live" if prefer_live else ("db" if db_first else "auto")
    key = _ck(symbol, expiry, mode)
    cached = _get(key) if use_cache else None
    if cached is not None:
        return cached
    live_ttl = max(10, _safe_int(os.getenv("OPTION_PRICES_LIVE_TTL_SECONDS", "60"), 60))
    ttl = live_ttl if prefer_live else None

    if db_first and not prefer_live:
        db_chain = _fetch_chain_from_db(symbol, expiry)
        if db_chain:
            return _set(key, db_chain, "local options DB", ttl)

    live_chain = _fetch_chain_from_yfinance(symbol, expiry)
    if live_chain:
        return _set(key, live_chain, "yfinance bid/ask mid-price", ttl)

    # Last fallback: local DB even when prefer_live was requested or DB-first missed.
    db_chain = _fetch_chain_from_db(symbol, expiry)
    if db_chain:
        return _set(key, db_chain, "local options DB fallback", ttl)

    return _set(key, {}, "unavailable", ttl)


def _lookup_leg(chain: dict, opt_type: str, strike: float) -> dict:
    opt_type = _norm_type(opt_type) or str(opt_type).lower()
    candidates = [
        _safe_strike(strike),
        _safe_strike(round(float(strike), 2)) if strike is not None else None,
        _safe_strike(round(float(strike))) if strike is not None else None,
        _safe_strike(round(float(strike) * 2) / 2) if strike is not None else None,
    ]
    seen = set()
    for s in candidates:
        if s is None or s in seen:
            continue
        seen.add(s)
        data = chain.get((opt_type, s))
        if data:
            return data
    return {}


def get_spread_prices(symbol: str, expiry: str,
                      sell_type: str, sell_strike: float,
                      buy_type: str, buy_strike: float,
                      *, prefer_live: Optional[bool] = None) -> dict:
    """
    Returns real/local net credit/debit for a two-leg spread.
    sell_type / buy_type: 'call' | 'put'.
    Net credit = sell_mid - buy_mid (positive = credit received).
    """
    symbol = str(symbol or "").upper().strip()
    live_pref = bool(prefer_live) if prefer_live is not None else False
    key = _ck(symbol, expiry, "live" if live_pref else "db")
    chain = fetch_chain(symbol, expiry, prefer_live=live_pref)
    if not chain:
        return {"error": "no chain data", "real_prices": False, "price_source": _source(key)}

    sell_data = _lookup_leg(chain, sell_type, sell_strike)
    buy_data = _lookup_leg(chain, buy_type, buy_strike)
    sell_mid = sell_data.get("mid")
    buy_mid = buy_data.get("mid")

    net = round(sell_mid - buy_mid, 2) if (sell_mid is not None and buy_mid is not None) else None
    width = abs(float(sell_strike) - float(buy_strike))
    src = sell_data.get("source") or buy_data.get("source") or _source(key)
    real_prices = bool(sell_mid is not None and buy_mid is not None)

    return {
        "sell_strike": sell_strike,
        "sell_bid": sell_data.get("bid"),
        "sell_ask": sell_data.get("ask"),
        "sell_mid": sell_mid,
        "sell_iv": sell_data.get("iv"),
        "sell_oi": sell_data.get("oi"),
        "buy_strike": buy_strike,
        "buy_bid": buy_data.get("bid"),
        "buy_ask": buy_data.get("ask"),
        "buy_mid": buy_mid,
        "buy_iv": buy_data.get("iv"),
        "buy_oi": buy_data.get("oi"),
        "net_credit": net,
        "spread_width": width,
        "max_profit": net,
        "max_loss": round(width - net, 2) if net is not None else None,
        "rr": round(net / (width - net), 2) if (net is not None and width > net > 0) else None,
        "pop_delta": round((1 - (sell_data.get("iv") or 0.3)) * 100) if sell_data.get("iv") else None,
        "real_prices": real_prices,
        "price_source": src or "local/yfinance option chain",
    }


def enrich_strategies(symbol: str, expiry: str, strategies: list, *, prefer_live: Optional[bool] = None) -> list:
    """
    Takes the strategy list from _build_strategies and enriches each one with
    bid/ask/last mid-prices from the local DB or yfinance. Falls back to estimates
    if chain unavailable.
    """
    symbol = str(symbol or "").upper().strip()
    live_pref = bool(prefer_live) if prefer_live is not None else False
    key = _ck(symbol, expiry, "live" if live_pref else "db")
    chain = fetch_chain(symbol, expiry, prefer_live=live_pref)
    if not chain:
        for s in strategies:
            s["price_source"] = f"estimated ({_source(key) or 'option chain unavailable'})"
            s["real_prices"] = False
        return strategies

    enriched = []
    import re

    def parse_legs(legs_str: str):
        """Extract list of (action, strike, type) from legs string."""
        pattern = r"(Sell|Buy)\s+\$?(\d+(?:\.\d+)?)(C|P)"
        out = []
        for m in re.finditer(pattern, str(legs_str or ""), re.IGNORECASE):
            side = "call" if m.group(3).upper() == "C" else "put"
            out.append((m.group(1), float(m.group(2)), side))
        return out

    for s in strategies:
        s = dict(s)
        name = s.get("name", "")
        try:
            parsed = parse_legs(s.get("legs", ""))
            if not parsed:
                s["price_source"] = "estimated (legs parse failed)"
                s["real_prices"] = False
                enriched.append(s)
                continue

            leg_prices = []
            total_credit = 0.0
            all_ok = True

            for action, strike, opt_type in parsed:
                data = _lookup_leg(chain, opt_type, strike)
                mid = data.get("mid")
                bid = data.get("bid")
                ask = data.get("ask")
                iv = data.get("iv")
                oi = data.get("oi")
                vol = data.get("volume")

                if mid is None:
                    all_ok = False
                    mid = 0.0

                signed = mid if action.lower() == "sell" else -mid
                total_credit += signed
                leg_prices.append({
                    "action": action,
                    "strike": strike,
                    "type": opt_type,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "iv": iv,
                    "oi": oi,
                    "volume": vol,
                    "signed_credit": round(signed, 2),
                    "source": data.get("source") or _source(key),
                })

            total_credit = round(total_credit, 2)
            width = 0.0
            if len(parsed) >= 2:
                width = abs(parsed[0][1] - parsed[1][1])
                if len(parsed) == 4:
                    width = abs(parsed[0][1] - parsed[1][1])

            max_profit = round(total_credit * 100, 2)
            max_loss = round((width - total_credit) * 100, 2) if width > 0 else None
            rr = round(total_credit / (width - total_credit), 2) if (width > 0 and width > total_credit > 0) else None

            short_ivs = [lp["iv"] for lp in leg_prices if lp["action"].lower() == "sell" and lp.get("iv")]
            avg_iv = sum(short_ivs) / len(short_ivs) if short_ivs else None

            s["leg_prices"] = leg_prices
            s["net_credit"] = total_credit
            s["net_credit_str"] = f"${total_credit:+.2f}" if total_credit else "—"
            s["max_profit_dollar"] = max_profit
            s["max_loss_dollar"] = max_loss
            s["spread_width"] = width
            s["rr"] = f"{rr:.2f}:1" if rr else s.get("rr", "—")
            s["real_prices"] = all_ok
            srcs = sorted({lp.get("source") for lp in leg_prices if lp.get("source")})
            s["price_source"] = ", ".join(srcs) if srcs else _source(key)
            if not all_ok:
                s["price_source"] = "partial/estimated (some strikes missing)"

            is_credit = total_credit > 0
            s["est_credit"] = f"${total_credit:.2f} {'credit' if is_credit else 'debit'}"
            s["max_gain"] = f"${max_profit:.0f}/contract" if max_profit else s.get("max_gain", "—")
            s["max_loss"] = f"${abs(max_loss):.0f}/contract" if max_loss else s.get("max_loss", "—")
            if total_credit > 0:
                stop_strike = parsed[1][1] if len(parsed) > 1 else "—"
                s["manage"] = (
                    f"Close at 50% credit (~${abs(total_credit * 0.5):.2f}). "
                    f"Stop if spot closes {'below' if 'Put' in name or 'Bull' in name else 'above'} ${stop_strike}."
                )
            else:
                s["manage"] = "Cut at 50% loss of debit."
            if avg_iv is not None:
                s["avg_short_iv"] = round(avg_iv, 4)

        except Exception as e:
            _log_once(f"[enrich_strategies] {name}: {e}")
            s["price_source"] = f"estimated (error: {e})"
            s["real_prices"] = False

        enriched.append(s)
    return enriched
