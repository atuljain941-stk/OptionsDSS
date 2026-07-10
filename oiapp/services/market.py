# oiapp/services/market.py  — with in-memory caching for spot + expirations
import math
import time
from datetime import datetime, time as dt_time, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None
try:
    import yfinance as yf
except Exception:
    yf = None
from ..db import (
    get_oi_fromdb, store_option_chain, has_option_chain_for_date,
    record_fetch_attempt,
)

# ── In-memory cache ────────────────────────────────────────────────────────
_cache = {}          # key → (value, expires_at)
_SPOT_TTL  = 60      # seconds — spot price cache
_EXP_TTL   = 300     # seconds — expirations cache (5 min)
_HIST_TTL  = 3600    # seconds — history cache (1 hr)

def _get(key):
    entry = _cache.get(key)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None

def _set(key, val, ttl):
    _cache[key] = (val, time.time() + ttl)
    return val


def sanitize_symbol(s):
    return s.strip().upper() if s else None


def _finite_float(value, default=None):
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _timestamp_to_iso(ts):
    try:
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat()
    except Exception:
        return None


def _timestamp_to_et(ts):
    try:
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ZoneInfo is not None:
            return ts.astimezone(ZoneInfo("America/New_York"))
        return ts
    except Exception:
        return None


def _spot_source_from_timestamp(ts):
    """Classify the latest yfinance bar so the UI can show pre/post-market usage."""
    et = _timestamp_to_et(ts)
    if et is None:
        return "intraday"
    t = et.time()
    if t < dt_time(9, 30):
        return "premarket"
    if t >= dt_time(16, 0):
        return "after-hours"
    return "regular"


def _last_valid_close_with_ts(df):
    if df is None or getattr(df, "empty", True) or "Close" not in df:
        return None, None
    try:
        closes = df["Close"]
        for idx in range(len(closes) - 1, -1, -1):
            v = _finite_float(closes.iloc[idx])
            if v is not None and v > 0:
                return v, closes.index[idx]
    except Exception:
        pass
    return None, None


def get_spot_snapshot(symbol: str):
    """
    Return a dict with the best available spot price and metadata.

    yfinance daily bars usually show the prior regular close before the market opens.
    For GEX and morning planning, use pre/post-market bars when they are available,
    then fall back to fast_info/daily close.
    """
    symbol = sanitize_symbol(symbol)
    if not symbol or yf is None:
        return None
    key = f"spot_snapshot:{symbol}"
    cached = _get(key)
    if cached is not None:
        return cached

    tk = yf.Ticker(symbol)
    prev_close = None
    try:
        hist_d = tk.history(period="7d", interval="1d", prepost=False, auto_adjust=False)
        if hist_d is not None and not hist_d.empty:
            daily_closes = [_finite_float(v) for v in hist_d.get("Close", []).tolist()]
            daily_closes = [v for v in daily_closes if v is not None and v > 0]
            if daily_closes:
                prev_close = daily_closes[-1]
    except Exception:
        pass

    # Prefer latest pre/post/regular intraday bar.  The 1m request is first because it
    # captures premarket values before the regular session opens.  Wider intervals are
    # fallbacks for yfinance throttling or delayed symbols.
    for period, interval in (("1d", "1m"), ("2d", "1m"), ("5d", "5m"), ("1mo", "15m")):
        try:
            df = tk.history(period=period, interval=interval, prepost=True, auto_adjust=False)
        except Exception:
            df = None
        price, ts = _last_valid_close_with_ts(df)
        if price is not None:
            source = _spot_source_from_timestamp(ts)
            snap = {
                "price": round(price, 4),
                "source": source,
                "timestamp": _timestamp_to_iso(ts),
                "prev_close": round(prev_close, 4) if prev_close else None,
                "change_pct": round((price - prev_close) / prev_close * 100, 3) if prev_close else None,
                "period": period,
                "interval": interval,
            }
            return _set(key, snap, _SPOT_TTL)

    try:
        fi = tk.fast_info
        price = _finite_float(getattr(fi, "last_price", None))
        if price is None:
            price = _finite_float(getattr(fi, "lastPrice", None))
        if price is not None and price > 0:
            pc = prev_close or _finite_float(getattr(fi, "previous_close", None))
            snap = {
                "price": round(price, 4),
                "source": "fast_info",
                "timestamp": None,
                "prev_close": round(pc, 4) if pc else None,
                "change_pct": round((price - pc) / pc * 100, 3) if pc else None,
                "period": None,
                "interval": None,
            }
            return _set(key, snap, _SPOT_TTL)
    except Exception as e:
        print("[spot] fast_info error:", e)

    if prev_close is not None:
        return _set(key, {
            "price": round(prev_close, 4),
            "source": "previous_close",
            "timestamp": None,
            "prev_close": round(prev_close, 4),
            "change_pct": 0.0,
            "period": "7d",
            "interval": "1d",
        }, _SPOT_TTL)
    return None


def get_spot(symbol: str):
    snap = get_spot_snapshot(symbol)
    if isinstance(snap, dict):
        return snap.get("price")
    return None


def get_expirations(symbol: str):
    """Live yfinance expirations — cached 5 min."""
    key = f"expirations:{symbol}"
    cached = _get(key)
    if cached is not None:
        return cached
    if yf is None:
        return []
    try:
        exps = list(yf.Ticker(symbol).options or [])
        return _set(key, exps, _EXP_TTL)
    except Exception as e:
        print("[expirations] error:", e)
        return []


def get_history(symbol: str, period="90d"):
    """OHLCV history — cached 1 hr."""
    key = f"history:{symbol}:{period}"
    cached = _get(key)
    if cached is not None:
        return cached
    _period_map = {"90d": "3mo", "60d": "3mo", "30d": "1mo",
                   "180d": "6mo", "1y": "1y"}
    yf_period = _period_map.get(period, period)
    if yf is None:
        return []
    try:
        df = yf.Ticker(symbol).history(period=yf_period)
        if df.empty:
            return _set(key, [], _HIST_TTL)
        rows = [
            {"date":  dt.strftime("%Y-%m-%d"),
             "open":  float(row["Open"]),
             "high":  float(row["High"]),
             "low":   float(row["Low"]),
             "close": float(row["Close"])}
            for dt, row in df.iterrows()
        ]
        return _set(key, rows, _HIST_TTL)
    except Exception as e:
        print("[history] error:", e)
        return []


def get_live_strikes_and_volume(symbol: str, expiration: str):
    key = f"strikes:{symbol}:{expiration}"
    cached = _get(key)
    if cached is not None:
        return cached
    if yf is None:
        return [], {}

    def _safe_int(value, default=0):
        f = _finite_float(value, None)
        if f is None:
            return int(default)
        try:
            return int(f)
        except Exception:
            return int(default)

    try:
        oc = yf.Ticker(symbol).option_chain(expiration)
        vol_map = {}
        strikes = []
        for side, df in (("call", getattr(oc, "calls", None)), ("put", getattr(oc, "puts", None))):
            if df is None or getattr(df, "empty", False):
                continue
            try:
                df = df[["strike", "volume"]].where(df[["strike", "volume"]].notna(), None)
            except Exception:
                pass
            for _, r in df.iterrows():
                strike = _finite_float(r.get("strike"), None)
                if strike is None:
                    continue
                strike = float(strike)
                strikes.append(strike)
                vol_map[(side, strike)] = _safe_int(r.get("volume"), 0)
        result = (sorted(set(strikes)), vol_map)
        return _set(key, result, _SPOT_TTL)
    except Exception as e:
        print("[live volume] error:", e)
        return [], {}


def fetch_store_for(symbol: str, expirations=None, per_side: int = 12):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TE
    symbol = sanitize_symbol(symbol)
    if not symbol or yf is None:
        return
    tk = yf.Ticker(symbol)
    underlying = None
    try:
        fi = getattr(tk, "fast_info", {}) or {}
        underlying = fi.get("last_price") or fi.get("lastPrice") or fi.get("regular_market_price")
    except Exception:
        underlying = None
    if underlying is None:
        try:
            hist = tk.history(period="2d", interval="1d")
            if hist is not None and not hist.empty:
                underlying = float(hist["Close"].iloc[-1])
        except Exception:
            underlying = None
    if expirations is None:
        try:
            expirations = list(tk.options or [])
        except Exception:
            expirations = []

    # Limit to a small set of expirations to reduce lock pressure and avoid
    # needlessly re-fetching deep chains during routine scans.
    expirations = [e for e in (expirations or [])[:5] if e]

    for exp in expirations:
        try:
            if has_option_chain_for_date(symbol, exp):
                continue  # only skip if already SUCCESSFULLY fetched today
        except Exception:
            pass

        def _fetch_exp(e=exp):
            oc = tk.option_chain(e)
            store_option_chain(symbol, e, oc, underlying=underlying)

        # Run the fetch off-thread and stop waiting once the timeout is hit.
        # Timeout raised from 10s -> 25s: failures are retried (not skipped)
        # on the next attempt, so the better fix for "wasting time on
        # failures" is giving genuinely slow-but-working fetches more room
        # to actually succeed, rather than giving up on them.
        ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"fetch-{symbol}")
        fut = ex.submit(_fetch_exp)
        try:
            fut.result(timeout=25)
            try:
                record_fetch_attempt(symbol, exp, "success")
            except Exception:
                pass
        except _TE:
            print(f"[fetch_store] {symbol}/{exp}: timeout after 25s — will retry next run")
            try:
                record_fetch_attempt(symbol, exp, "timeout")  # logged for diagnostics only, not used to skip
            except Exception:
                pass
        except Exception as e:
            print(f"[fetch_store] {symbol}/{exp}: {e}")
            try:
                record_fetch_attempt(symbol, exp, "error")
            except Exception:
                pass
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass


def get_oi_map_fromDB(symbol: str, expiration: str):
    """Returns ({(type,strike): oi}, snapshot_date) as expected by api_options."""
    rows = get_oi_fromdb(symbol, expiration)  # list of {type,strike,oi,volume,date}
    if not rows:
        return {}, None
    snap_date = rows[0].get("date") if rows else None
    oi_map = {}
    for r in rows:
        try:
            key = (r["type"], float(r["strike"]))
            oi_map[key] = int(r.get("oi") or 0)
        except Exception:
            pass
    return oi_map, snap_date


def select_strikes_around_atm(strikes: list, spot: float, per_side: int = 12):
    """Return up to per_side strikes above and below spot."""
    if not strikes or spot is None:
        return strikes or []
    below = [s for s in strikes if s <= spot]
    above = [s for s in strikes if s > spot]
    return below[-per_side:] + above[:per_side]
