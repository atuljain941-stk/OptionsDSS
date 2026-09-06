"""
oiapp/scanners/realtime_dashboard.py
------------------------------------
Registers the live tastytrade-powered dashboard inside oiapp itself:

    GET /realtime/<symbol>                 -> chart page (lightweight-charts)
    GET /realtime/api/<symbol>/snapshot     -> JSON: quote + AVWAP + GEX + VolQuant
    GET /realtime/api/<symbol>/bars         -> JSON: recent OHLCV bars

This module wires together:
  - oiapp.services.tastytrade_feed   (live quote + historical candle data,
    both from tastytrade -- no yfinance anywhere in this module; candles
    come from tastytrade's DXLink subscribe_candle stream, see
    get_recent_bars() below)
  - oiapp.services.indicator_engines (AVWAP / GEX / VolQuant engines)
  - GEX prefers your existing DB-backed pipeline (spy_strategies._compute_gex)
    when available for a symbol, falling back to a live tastytrade
    option-chain + greeks fetch otherwise (see get_gex_from_db / get_option_chain)
"""

from __future__ import annotations

import math
import time
import threading
from datetime import datetime, timedelta
from typing import List

import pandas as pd
from flask import Blueprint, jsonify, render_template_string, request

from ..services.tastytrade_feed import feed
from ..services.indicator_engines import AVWAPEngine, VolQuantEngine, GEXEngine, ChainRow, TechnicalSeriesEngine, CompositeSignalEngine

realtime_bp = Blueprint("realtime_dashboard", __name__, url_prefix="/realtime")

_avwap_engine = AVWAPEngine()
_volquant_engine = VolQuantEngine(timeframe="5min")
_gex_engine = GEXEngine()
_technical_engine = TechnicalSeriesEngine()
_composite_engine = CompositeSignalEngine()

# Futures contract multipliers (per point), for GEXEngine — equities default to 100.
_CONTRACT_MULTIPLIERS = {
    "/MGC": 10, "MGC": 10,     # Micro Gold: 10 troy oz
    "/GC": 100, "GC": 100,     # Gold: 100 troy oz
    "/SI": 5000, "SI": 5000,   # Silver: 5,000 troy oz
}


# Timeframe -> (tastytrade candle interval string, how far back to backfill).
# tastytrade's subscribe_candle natively supports all of these interval
# strings directly (confirmed against the installed SDK's own docstring:
# '15s','5m','1h','3d','1w','1mo' etc.) -- no resampling from a finer
# interval needed, unlike the old yfinance-based version.
_TIMEFRAME_MAP = {
    "1m":  ("1m", timedelta(days=5)),
    "3m":  ("3m", timedelta(days=10)),
    "5m":  ("5m", timedelta(days=30)),
    "15m": ("15m", timedelta(days=60)),
    "1h":  ("1h", timedelta(days=180)),
    "2h":  ("2h", timedelta(days=180)),
    "4h":  ("4h", timedelta(days=365)),
    "1d":  ("1d", timedelta(days=730)),
    "1w":  ("1w", timedelta(days=365 * 5)),
    "1M":  ("1mo", timedelta(days=365 * 10)),
}
DEFAULT_TIMEFRAME = "1m"


def _decode_path_symbol(raw: str) -> str:
    """Every <path:symbol> route below is hit by frontend fetch() calls
    that build the URL as f"/realtime/api/{symbol}/..." -- fine for a
    normal ticker, but futures symbols always start with a literal "/"
    (e.g. "/MGCV6"), which collides with the URL path itself. Confirmed
    directly against a real Flask app that this is NOT just a cosmetic
    issue: Werkzeug's merge_slashes silently 308-redirects
    "/api//MGCV6/bars" to "/api/MGCV6/bars", so the view function DOES
    get called, but with the WRONG symbol (the leading "/" stripped) --
    which is also why nothing useful showed up when searching server
    logs for "/MGCV6" specifically: the log entries exist, just under
    "MGCV6" instead. Also confirmed percent-encoding the slash
    (%2FMGCV6) does NOT avoid this -- Werkzeug decodes %2F and merges
    slashes before route matching, so the exact same redirect happens
    either way; and disabling merge_slashes entirely just turns the
    redirect into a hard 404 instead (the <path:...> converter's own
    regex doesn't accept an effectively-empty leading segment).
    Real fix, verified end-to-end against actual Flask routing: the
    frontend substitutes a leading "/" with "~" before ever building the
    URL (see encodeSymbolForUrl() in the page's JS) -- "~" is a
    perfectly ordinary path character with no special meaning to
    Werkzeug's routing, so it round-trips with zero redirect/merge
    behavior. This function is the other half of that pair: translate
    "~MGCV6" back to "/MGCV6" before the symbol is used for anything.
    """
    return ("/" + raw[1:]) if raw.startswith("~") else raw

# Timeframe -> candidate (parent_timeframe, pandas_resample_rule) pairs, in
# preference order. If a compatible finer timeframe is already cached for
# the same symbol, switching to one of these derives the new bars by
# resampling in-memory instead of doing a fresh DXLink candle fetch --
# this is what makes timeframe switching instant instead of re-fetching,
# same idea as how TradingView avoids a network round-trip for every
# timeframe change. Daily/weekly/monthly are deliberately excluded: their
# lookback windows (years) are far longer than anything intraday data
# could ever cover, so they always fetch fresh (which is cheap anyway --
# a multi-year daily candle count isn't large).
_RESAMPLE_PARENTS = {
    "3m":  [("1m", "3min")],
    "15m": [("5m", "15min"), ("1m", "15min")],
    "1h":  [("15m", "1h"), ("5m", "1h")],
    "2h":  [("1h", "2h")],
    "4h":  [("2h", "4h"), ("1h", "4h")],
}


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df.resample(rule).agg(agg).dropna(subset=["open"])


_bars_cache: dict = {}  # (symbol, timeframe) -> (timestamp, DataFrame) -- IN-MEMORY ONLY,
# cleared on process restart, never written to disk/DB. This exists purely
# to dedupe the near-simultaneous /bars and /indicators calls the frontend
# fires together on every symbol/interval/refresh -- both would otherwise
# independently re-fetch identical candle data. TTL is longer than the old
# yfinance version's (20s vs 8s) since a DXLink candle backfill is a
# genuinely heavier round-trip than a single REST call.
_BARS_CACHE_TTL = 20.0

_indicators_cache: dict = {}  # (symbol, timeframe) -> (timestamp, dict) -- caches the full
# computed indicator result (EMA/RSI/MACD/ADX/AVWAP/VolQuant/S-R), so multiple
# windows sharing the same symbol+timeframe don't redundantly recompute it.
_INDICATORS_CACHE_TTL = 20.0

_last_fetch_error: dict = {}  # (symbol, timeframe) -> (timestamp, error_message) -- so
# a failed fetch's actual reason reaches the browser instead of only ever
# being printed to the server console. Cleared automatically on success.

# Single-flight coalescing: the cache above only prevents *sequential*
# duplicate fetches. /bars and /indicators are fired concurrently by the
# frontend (Promise.all), so both can see a cache miss at the same instant
# and each open their own DXLink websocket for the identical candle
# request -- confirmed as the actual cause of the connection bursts seen
# in server logs (multiple full SETUP/AUTH_STATE/CHANNEL_REQUEST handshakes
# within the same second). This lock ensures only the first caller for a
# given (symbol, timeframe) actually fetches; anyone arriving while that's
# in flight just waits for it and reads the result from cache, instead of
# starting a redundant connection.
_bars_inflight_locks: dict = {}
_bars_inflight_guard = threading.Lock()


def _candles_to_dataframe(candles: list) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()
    rows = []
    for c in candles:
        try:
            rows.append({
                "time": pd.to_datetime(int(c.time), unit="ms", utc=True),
                "open": float(c.open), "high": float(c.high),
                "low": float(c.low), "close": float(c.close),
                "volume": float(c.volume) if c.volume is not None else 0.0,
            })
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(subset="time").sort_values("time")
    return df.set_index("time")[["open", "high", "low", "close", "volume"]]


def get_recent_bars(symbol: str, n_bars: int = 300, timeframe: str = DEFAULT_TIMEFRAME,
                     extended_hours: bool = True) -> pd.DataFrame:
    """OHLCV bars with volume, for the price chart, AVWAP/VolQuant/overlays,
    and everything downstream of it -- sourced from tastytrade's own DXLink
    candle stream (subscribe_candle), NOT yfinance. `timeframe` picks the
    tastytrade candle interval + backfill window (see _TIMEFRAME_MAP).
    `extended_hours` defaults to True (include pre/post-market session data)
    to match how most charting platforms display by default -- pass False
    for regular-trading-hours-only candles."""
    cache_key = (symbol, timeframe, extended_hours)
    cached = _bars_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _BARS_CACHE_TTL:
        return cached[1].tail(n_bars)

    # Try deriving this timeframe from an already-cached finer one instead
    # of hitting tastytrade again -- this is what makes switching timeframes
    # on a symbol you're already viewing instant rather than a fresh fetch.
    for parent_tf, rule in _RESAMPLE_PARENTS.get(timeframe, []):
        parent_key = (symbol, parent_tf, extended_hours)
        parent_cached = _bars_cache.get(parent_key)
        if parent_cached and (time.time() - parent_cached[0]) < _BARS_CACHE_TTL:
            try:
                resampled = _resample_ohlcv(parent_cached[1], rule)
            except Exception:
                continue
            if not resampled.empty:
                _bars_cache[cache_key] = (time.time(), resampled)
                return resampled.tail(n_bars)

    with _bars_inflight_guard:
        lock = _bars_inflight_locks.get(cache_key)
        is_leader = lock is None
        if is_leader:
            lock = threading.Lock()
            lock.acquire()
            _bars_inflight_locks[cache_key] = lock

    if not is_leader:
        # A fetch for this exact (symbol, timeframe) is already in flight --
        # wait for it to finish (blocks on the leader's lock release) rather
        # than starting a second redundant DXLink connection.
        lock.acquire()
        lock.release()
        cached = _bars_cache.get(cache_key)
        return cached[1].tail(n_bars) if cached else pd.DataFrame()

    try:
        interval, lookback = _TIMEFRAME_MAP.get(timeframe, _TIMEFRAME_MAP[DEFAULT_TIMEFRAME])
        start_time = datetime.utcnow() - lookback
        candles = feed.get_candles(symbol, interval, start_time, extended_hours=extended_hours, overall_timeout=25.0)
        df = _candles_to_dataframe(candles)
        if df.empty:
            _last_fetch_error[cache_key] = (time.time(), "No candles returned (empty result, not an exception)")
            return pd.DataFrame()
        _last_fetch_error.pop(cache_key, None)
        _bars_cache[cache_key] = (time.time(), df)
        return df.tail(n_bars)
    except Exception as e:  # noqa: BLE001
        error_msg = f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__} (no message)"
        print(f"[realtime_dashboard] tastytrade candle fetch failed for {symbol} ({timeframe}): {error_msg}")
        _last_fetch_error[cache_key] = (time.time(), error_msg)
        if cached:
            return cached[1].tail(n_bars)  # serve stale cache over nothing on a transient error
        return pd.DataFrame()
    finally:
        with _bars_inflight_guard:
            _bars_inflight_locks.pop(cache_key, None)
        lock.release()


async def _fetch_chain_async(session, symbol: str) -> List[ChainRow]:
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Greeks
    from tastytrade.instruments import get_option_chain
    from tastytrade.utils import get_tasty_monthly

    chain = await get_option_chain(session, symbol)
    exp = get_tasty_monthly()  # ~45 DTE; swap for a specific expiration if you want a fixed cycle
    options = chain.get(exp) if hasattr(chain, "get") else chain[exp]
    streamer_symbols = [o.streamer_symbol for o in options]

    greeks_map = {}
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Greeks, streamer_symbols)
        collected = 0
        async for g in streamer.listen(Greeks):
            greeks_map[g.event_symbol] = g
            collected += 1
            if collected >= len(streamer_symbols):
                break

    by_strike = {}
    for o in options:
        g = greeks_map.get(o.streamer_symbol)
        gamma = float(g.gamma) if g is not None else 0.0
        strike = float(o.strike_price)
        entry = by_strike.setdefault(
            strike, {"call_oi": 0.0, "put_oi": 0.0, "call_gamma": 0.0, "put_gamma": 0.0}
        )
        oi = float(getattr(o, "open_interest", 0) or 0)
        # option_type is typically 'C'/'P' or an enum with that value — check
        # against your installed SDK version if this comparison needs adjusting.
        is_call = str(getattr(o, "option_type", "")).upper().startswith("C")
        if is_call:
            entry["call_oi"], entry["call_gamma"] = oi, gamma
        else:
            entry["put_oi"], entry["put_gamma"] = oi, gamma

    return [ChainRow(strike=s, **v) for s, v in sorted(by_strike.items())]


# Realtime symbol -> (COT contract root, Schwab equity-proxy symbol). /MGC
# (micro gold) shares the same underlying market as /GC, so it's mapped to
# the regular gold COT report / OI history — there's no separate micro COT.
_FUTURES_POSITIONING_MAP = {
    "/MGC": ("GC", "GLD"), "MGC": ("GC", "GLD"),
    "/GC":  ("GC", "GLD"), "GC":  ("GC", "GLD"),
    "/SI":  ("SI", "SLV"), "SI":  ("SI", "SLV"),
    "SPY":  ("ES", "SPY"),
    "QQQ":  ("NQ", "QQQ"),
}


def get_futures_positioning(symbol: str) -> dict:
    """COT positioning + Schwab futures OI trend (existing services, not
    rebuilt) plus the nearest few expiries' OI, for symbols that have a
    mapped futures contract. Returns {"status": "not_applicable"} otherwise."""
    mapped = _FUTURES_POSITIONING_MAP.get(symbol)
    if not mapped:
        return {"status": "not_applicable"}
    contract, equity_sym = mapped

    try:
        from ..services.cftc_cot import get_combined_signal
        combined = get_combined_signal(contract)
    except Exception as e:  # noqa: BLE001
        combined = {"error": str(e)}

    expiries = []
    try:
        from ..services.futures_oi_schwab import get_latest_oi
        oi_data = get_latest_oi(equity_sym, days=90)
        table = sorted(
            oi_data.get("contract_table", []),
            key=lambda r: r.get("expiry") or "9999-99-99",
        )
        expiries = table[:3]
    except Exception as e:  # noqa: BLE001
        expiries = [{"error": str(e)}]

    return {
        "status": "ok",
        "contract": contract,
        "combined_score": combined.get("combined_score"),
        "label": combined.get("label"),
        "cot": combined.get("cot", {}),
        "schwab": combined.get("schwab", {}),
        "expiries": expiries,
    }


def get_gex_from_db(symbol: str, spot: float):
    """Reuses your existing DB-backed GEX pipeline (spy_strategies._compute_gex
    + _oi_rows) instead of live tastytrade chain+greeks fetching. Your OI data
    is refreshed by your existing scheduled fetch job, not on every poll --
    which is correct, since OI barely moves intraday; volume is read fresh
    from the same table each call. Returns None if there's no DB coverage for
    this symbol (e.g. futures options aren't in this equity-oriented store),
    so the caller falls back to the live tastytrade path for those."""
    try:
        from ..scanners.spy_strategies import _future_exps, _pick_exp, _oi_rows, _compute_gex, _compute_ta
    except Exception as e:  # noqa: BLE001
        print(f"[realtime_dashboard] spy_strategies import failed: {e}")
        return None

    try:
        exps = _future_exps(symbol) or []
        if not exps:
            return None
        exp, dte = _pick_exp(exps, 0, 5, 0)
        if not exp:
            return None
        rows = _oi_rows(symbol, exp)
        if not rows:
            return None
        ta = _compute_ta(symbol) or {}
        iv_atm = float(ta.get("iv_est", 20.0) or 20.0)
        gex = _compute_gex(rows, spot, max(1, dte or 1), iv_atm)
    except Exception as e:  # noqa: BLE001
        print(f"[realtime_dashboard] get_gex_from_db failed for {symbol}: {e}")
        return None

    call_oi, put_oi = {}, {}
    for r in rows:
        strike = float(r["strike"])
        oi = int(r["oi"] or 0)
        bucket = call_oi if r["type"] == "call" else put_oi
        bucket[strike] = bucket.get(strike, 0) + oi
    call_walls = sorted(
        [{"strike": k, "oi": v} for k, v in call_oi.items() if k >= spot], key=lambda x: x["oi"], reverse=True
    )[:3]
    put_walls = sorted(
        [{"strike": k, "oi": v} for k, v in put_oi.items() if k <= spot], key=lambda x: x["oi"], reverse=True
    )[:3]

    return {
        "spot": round(spot, 2),
        "total_gex": gex.get("total_gex"),
        "gross_gex": gex.get("gross_gex"),
        "gex_ratio": gex.get("gex_ratio"),
        "regime": "POSITIVE (mean-reversion)" if gex.get("regime") == "POSITIVE_GAMMA" else "NEGATIVE (trend-amplifying)",
        "gamma_flip": gex.get("gamma_flip"),
        "pin_strike": gex.get("pin_strike"),
        "max_pain": gex.get("max_pain"),
        "call_walls": call_walls,
        "put_walls": put_walls,
        "source": "db",
        "expiry": exp,
    }


_option_chain_cache: dict = {}  # symbol -> (timestamp, List[ChainRow])
_OPTION_CHAIN_TTL = 30.0  # seconds. This is the expensive call (full chain
# fetch + DXLink greeks subscription for every strike) -- without a cache,
# calling it on every 3s snapshot poll is what was causing the 429s and the
# "very slow" feel. GEX levels don't need to be per-3-second fresh; 30s is
# already far more current than most public GEX tools refresh.


_shutdown_error_logged = False


def _log_feed_error(context: str, e: Exception) -> None:
    """Logs a feed/quote error -- but if it's specifically the
    'event loop shutting down' error (asyncio.run_coroutine_threadsafe
    against a loop that's been torn down, which happens en masse the
    moment the app starts shutting down while requests are still
    in-flight), logs it ONCE for the whole process instead of once per
    symbol/leg. Before this, a shutdown with a handful of open journal
    positions being priced concurrently would print the identical
    'cannot schedule new futures after shutdown' line 5-10+ times in a
    row -- harmless (nothing hangs, nothing crashes, callers already get
    a clean error dict), but pure noise that makes a real error in the
    same window harder to spot.
    """
    global _shutdown_error_logged
    msg = str(e)
    is_shutdown = "cannot schedule new futures after shutdown" in msg or "Event loop is closed" in msg
    if is_shutdown:
        if not _shutdown_error_logged:
            _shutdown_error_logged = True
            print(f"[realtime_dashboard] feed loop is shutting down -- in-flight requests "
                  f"(starting with {context}) will fail gracefully with a clean error; "
                  f"further identical messages this shutdown are suppressed")
        return
    print(f"[realtime_dashboard] {context} failed: {e}")


def get_option_chain(symbol: str) -> List[ChainRow]:
    """Runs on the feed's persistent event loop (fixes 'Event loop is
    closed' -- the previous asyncio.run() per-call approach broke the
    cached Session's internal async client). Cached for _OPTION_CHAIN_TTL
    seconds per symbol since this is a genuinely expensive call."""
    now = time.time()
    cached = _option_chain_cache.get(symbol)
    if cached and (now - cached[0]) < _OPTION_CHAIN_TTL:
        return cached[1]

    try:
        session = feed.get_session()
        rows = feed.run_coro(_fetch_chain_async(session, symbol))
        _option_chain_cache[symbol] = (now, rows)
        return rows
    except Exception as e:  # noqa: BLE001
        _log_feed_error(f"get_option_chain({symbol})", e)
        # Serve stale cache rather than nothing if we have it -- a 30-90s
        # stale GEX read is still more useful than blanking the card on a
        # transient error.
        if cached:
            return cached[1]
        return []


# Separate cache from the GEX chain cache above -- keyed by the exact
# contract requested (symbol+expiry+strike+type), not just the underlying
# symbol, since this is a single-contract lookup rather than a whole
# expiry's chain.
_option_quote_cache: dict = {}
_OPTION_QUOTE_TTL = 15.0  # seconds -- a journal entry isn't as latency-
# sensitive as a live GEX card, but still wants a reasonably fresh price.


async def _fetch_option_quote_async(session, symbol: str, expiry: str, strike: float, option_type: str):
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote
    from tastytrade.instruments import get_option_chain
    import datetime as _dt

    exp_date = _dt.date.fromisoformat(str(expiry)[:10])
    chain = await get_option_chain(session, symbol.upper().strip())
    options = chain.get(exp_date) if hasattr(chain, "get") else chain.get(exp_date, [])
    if not options:
        return {"error": f"No options chain found for {symbol} expiring {expiry}"}

    is_call = str(option_type).upper().startswith("C")
    match = None
    for o in options:
        o_is_call = str(getattr(o, "option_type", "")).upper().startswith("C")
        if o_is_call == is_call and abs(float(o.strike_price) - float(strike)) < 0.005:
            match = o
            break
    if match is None:
        return {"error": f"No {'call' if is_call else 'put'} at strike {strike} for {symbol} {expiry}"}

    bid = ask = None
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, [match.streamer_symbol])
        async for q in streamer.listen(Quote):
            bid = float(q.bid_price) if q.bid_price is not None else None
            ask = float(q.ask_price) if q.ask_price is not None else None
            break

    if bid is None and ask is None:
        return {"error": "No live quote available for this contract right now"}
    mid = round((bid + ask) / 2, 2) if (bid is not None and ask is not None) else (bid or ask)
    return {
        "bid": round(bid, 2) if bid is not None else None,
        "ask": round(ask, 2) if ask is not None else None,
        "mid": mid,
        "symbol": symbol.upper().strip(),
        "expiry": exp_date.isoformat(),
        "strike": float(strike),
        "option_type": "call" if is_call else "put",
    }


_futures_oi_cache: dict = {}
_FUTURES_OI_TTL = 900.0  # 15 min -- futures OI updates once per exchange session, no need to re-poll often


async def _fetch_futures_summary_async(session, product_code: str):
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Summary
    from tastytrade.instruments import Future

    # Resolve the current front-month contract for this product code
    # (e.g. "ES", "GC", "CL") rather than requiring the caller to know
    # the exact streamer symbol format -- that format includes the
    # specific expiry month/year and an exchange suffix
    # (e.g. "/ESZ26:XCME"), which changes every quarter and isn't
    # something to hardcode or expect a caller to track.
    futures = await Future.get(session, product_codes=[product_code.upper().strip().lstrip("/")])
    if isinstance(futures, Future):
        futures = [futures]
    futures = [f for f in (futures or []) if getattr(f, "active", True)]
    if not futures:
        return {"error": f"No active future found for product code {product_code}"}
    # Sort by expiration ascending -- front month is whichever active
    # contract expires soonest. Don't trust API return order for this;
    # the SDK's own docstring doesn't guarantee front-month-first.
    futures.sort(key=lambda f: f.expiration_date or datetime.max.date())
    fut = futures[0]

    summary = {}
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Summary, [fut.streamer_symbol])
        async for s in streamer.listen(Summary):
            summary = {
                "open_interest": int(s.open_interest) if s.open_interest is not None else None,
                "prev_day_close": float(s.prev_day_close_price) if s.prev_day_close_price is not None else None,
            }
            break
    if summary.get("open_interest") is None:
        return {"error": f"No open interest in the Summary event for {fut.streamer_symbol} "
                          f"-- the exchange may not have published today's snapshot yet"}
    return {
        "product_code": product_code.upper(),
        "symbol": fut.symbol,
        "streamer_symbol": fut.streamer_symbol,
        "expiration_date": str(fut.expiration_date) if fut.expiration_date else None,
        "open_interest": summary["open_interest"],
        "prev_day_close": summary.get("prev_day_close"),
    }


def get_futures_open_interest(product_code: str) -> dict:
    """Front-month futures open interest via Tastytrade's DXLink feed --
    an alternative to Schwab for the daily Futures OI data source.
    Genuinely different mechanism from Schwab's REST poll: this is a
    live streaming subscription (the DXLink Summary event, which is
    where open_interest actually lives -- the static Future instrument
    definition has no OI field at all), briefly opened and closed per
    call, same pattern as get_option_quote() above.

    NOTE: this has been verified against the SDK's own data model
    (Summary genuinely has an open_interest field) and tested with
    mocked responses, but has NOT been validated against a live
    Tastytrade account in this environment -- worth confirming the
    resolved front-month contract and OI value look right the first
    time you use this against your real connection, the same way you'd
    sanity-check any new data source.
    """
    from ..services.tastytrade_feed import tastytrade_configured
    if not tastytrade_configured():
        return {"error": "Tastytrade isn't connected. Set it up at /realtime/setup to pull live futures OI."}

    key = product_code.upper().strip()
    now = time.time()
    cached = _futures_oi_cache.get(key)
    if cached and (now - cached[0]) < _FUTURES_OI_TTL:
        return cached[1]

    try:
        session = feed.get_session()
        result = feed.run_coro(_fetch_futures_summary_async(session, key))
        _futures_oi_cache[key] = (now, result)
        return result
    except Exception as e:  # noqa: BLE001
        _log_feed_error(f"get_futures_open_interest({product_code})", e)
        if cached:
            return cached[1]
        return {"error": str(e)}


def get_option_quote(symbol: str, expiry: str, strike: float, option_type: str) -> dict:
    """Live bid/ask/mid for ONE specific option contract, for the journal's
    Add/Edit Trade forms -- a targeted lookup (one contract), not the
    whole-chain-plus-greeks fetch get_option_chain() above does for GEX.
    Requires Tastytrade to be connected (see /realtime/setup); returns a
    clear error dict rather than raising if it isn't, or if the contract
    can't be found/quoted."""
    from ..services.tastytrade_feed import tastytrade_configured
    if not tastytrade_configured():
        return {"error": "Tastytrade isn't connected. Set it up at /realtime/setup to pull live option prices."}

    key = (symbol.upper().strip(), str(expiry)[:10], round(float(strike), 2), str(option_type).upper()[:1])
    now = time.time()
    cached = _option_quote_cache.get(key)
    if cached and (now - cached[0]) < _OPTION_QUOTE_TTL:
        return cached[1]

    try:
        session = feed.get_session()
        result = feed.run_coro(_fetch_option_quote_async(session, symbol, expiry, strike, option_type))
        _option_quote_cache[key] = (now, result)
        return result
    except Exception as e:  # noqa: BLE001
        _log_feed_error(f"get_option_quote({symbol} {expiry} {strike}{option_type})", e)
        if cached:
            return cached[1]
        return {"error": str(e)}


@realtime_bp.route("/futures_oi/<product_code>", methods=["GET"])
def api_futures_open_interest(product_code: str):
    """Front-month futures open interest via Tastytrade -- an
    alternative to the Schwab futures OI data source. Usage:
    /realtime/futures_oi/ES  (or GC, CL, SI, etc.)
    This is the piece worth testing first against a real connection
    before relying on it: confirm the resolved contract/expiration and
    OI value look right for a product you can cross-check elsewhere.
    """
    result = get_futures_open_interest(product_code)
    if result.get("error"):
        return jsonify(result), 502
    return jsonify(result)


@realtime_bp.route("/alerts", methods=["POST"])
def create_price_alert():
    """Creates a price alert using your existing alert_rules table
    (alert_kind='price') -- picked up automatically by your existing
    background alert rule watcher / Telegram notifier, no separate
    alerting system needed."""
    from ..scanners.watchlist_manager import _conn, _ensure_alert_rules_table
    import datetime

    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    price_value = data.get("price_value")
    price_operator = (data.get("price_operator") or ">=").strip()
    timeframe = (data.get("timeframe") or "1m").strip()

    if not symbol or price_value is None:
        return jsonify({"ok": False, "error": "symbol and price_value are required"}), 400
    try:
        price_value = float(price_value)
    except Exception:
        return jsonify({"ok": False, "error": "price_value must be a number"}), 400

    _ensure_alert_rules_table()
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    name = f"{symbol} price {price_operator} {price_value:g} ({ts})"

    con = _conn()
    try:
        con.execute(
            """INSERT INTO alert_rules
               (name, alert_kind, symbol, benchmark, trigger_mode, enabled,
                price_operator, price_value, timeframe, notes)
               VALUES (?, 'price', ?, 'SPY', 'once', 1, ?, ?, ?, ?)""",
            (name, symbol, price_operator, price_value, timeframe, "Created from Realtime Dashboard"),
        )
        con.commit()
    except Exception as e:  # noqa: BLE001
        con.close()
        return jsonify({"ok": False, "error": str(e)}), 500
    con.close()
    return jsonify({"ok": True, "name": name})


@realtime_bp.route("/system/pause", methods=["GET"])
def system_pause_get():
    """Status of the global background-process pause switch. Covers
    every watcher that checks job_registry.is_enabled() -- trade/health
    alerts, telegram alerts, signal notifier, agentic AI scanner, and
    scheduled jobs -- without touching any of their individual settings."""
    from ..services import job_registry
    return jsonify({"paused": job_registry.is_globally_paused()})


@realtime_bp.route("/system/pause", methods=["POST"])
def system_pause_set():
    from ..services import job_registry
    data = request.get_json(silent=True) or {}
    paused = bool(data.get("paused"))
    job_registry.set_global_pause(paused)
    return jsonify({"ok": True, "paused": paused})


@realtime_bp.route("/watchlists/scan", methods=["POST"])
def watchlist_scan_filter():
    """Filters a symbol list down to only those matching a scanner query,
    e.g. strongcandle("1d") -- reuses your existing scanner engine
    (scanner_builder._parse_query/_scan_symbol/_eval) rather than a
    separate implementation, so results match what Scanner Builder itself
    would return for the same query."""
    from ..scanners.scanner_builder import _parse_query, _expand_scan_nodes, _scan_symbol, _required_timeframes, _eval
    from concurrent.futures import ThreadPoolExecutor, as_completed

    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols") or []
    query_text = (data.get("query_text") or "").strip()
    benchmark = (data.get("benchmark") or "SPY").strip().upper() or "SPY"

    if not query_text:
        return jsonify({"matched": symbols, "error": None})
    if not symbols:
        return jsonify({"matched": [], "error": None})

    try:
        raw_root = _parse_query(query_text)
        root = _expand_scan_nodes(raw_root, ())
    except Exception as e:  # noqa: BLE001
        return jsonify({"matched": [], "error": f"Query error: {e}"}), 400

    req_tfs = _required_timeframes(root)
    symbols = list(dict.fromkeys(symbols))  # de-dupe, preserve order

    matched = []
    errors = []
    ex = ThreadPoolExecutor(max_workers=8)
    try:
        futs = {ex.submit(_scan_symbol, sym, root, benchmark, req_tfs): sym for sym in symbols}
        from ..services.bounded_wait import bounded_as_completed
        for fut, sym in bounded_as_completed(futs, timeout=60,
                on_timeout=lambda ks: print(f"[realtime_dashboard] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                errors.append({"symbol": sym, "error": "timed out"})
                continue
            try:
                ctx, err = fut.result()
            except Exception as e:  # noqa: BLE001
                ctx, err = None, str(e)
            if ctx is None:
                if err:
                    errors.append({"symbol": sym, "error": err})
                continue
            try:
                if bool(_eval(root, ctx, shift=0, tf_default="1d")):
                    matched.append(sym)
            except Exception as e:  # noqa: BLE001
                errors.append({"symbol": sym, "error": str(e)})
    finally:
        ex.shutdown(wait=False)

    # preserve original watchlist ordering rather than thread-completion order
    matched_set = set(matched)
    ordered = [s for s in symbols if s in matched_set]
    return jsonify({"matched": ordered, "errors": errors[:10]})


@realtime_bp.route("/watchlist")
def watchlist_symbols():
    """All symbols across all watchlists (union) -- kept for backward
    compatibility; prefer /watchlists + /watchlists/<id>/symbols for a
    specific named watchlist."""
    try:
        from ..scanners.watchlist_manager import get_all_watchlist_symbols
        symbols = get_all_watchlist_symbols()
    except Exception as e:  # noqa: BLE001
        return jsonify({"symbols": [], "error": str(e)})
    return jsonify({"symbols": symbols})


@realtime_bp.route("/watchlists")
def watchlists_list():
    """Lightweight list of your named watchlists (id, name, symbol_count) --
    deliberately skips the sector-coverage computation and live-price fetch
    that the main watchlist_manager routes do, since this is just for
    populating a dropdown, not display."""
    try:
        from ..scanners.watchlist_manager import _conn
        con = _conn()
        rows = con.execute("""
            SELECT w.id, w.name, COUNT(ws.id) as symbol_count
            FROM watchlists w
            LEFT JOIN watchlist_symbols ws ON ws.watchlist_id = w.id
            GROUP BY w.id ORDER BY w.name
        """).fetchall()
        con.close()
        return jsonify({"watchlists": [dict(r) for r in rows]})
    except Exception as e:  # noqa: BLE001
        return jsonify({"watchlists": [], "error": str(e)})


@realtime_bp.route("/watchlists/<int:wl_id>/symbols")
def watchlist_symbols_by_id(wl_id):
    """Just the symbol list for one named watchlist -- no live-price fetch
    (unlike watchlist_manager.get_symbols), since that's unnecessary load
    for populating a clickable sidebar list."""
    try:
        from ..scanners.watchlist_manager import _conn
        con = _conn()
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol", (wl_id,)
        ).fetchall()
        con.close()
        return jsonify({"symbols": [r[0] for r in rows]})
    except Exception as e:  # noqa: BLE001
        return jsonify({"symbols": [], "error": str(e)})


@realtime_bp.route("/layouts", methods=["GET"])
def layouts_list():
    """Named full-dashboard layouts (all windows, symbols, intervals, per-window
    template config, grid shape, sync settings) -- distinct from /last_layout,
    which is the single auto-saved 'restore on next load' slot. This is the
    explicit, named, multiple-save version, same pattern as per-window
    templates but scoped to the whole page."""
    from ..scanners.watchlist_manager import _get_setting
    import json
    raw = _get_setting("realtime_layout_index", "[]")
    try:
        names = json.loads(raw) or []
    except Exception:
        names = []
    return jsonify({"layouts": names})


@realtime_bp.route("/layouts", methods=["POST"])
def layouts_save():
    from ..scanners.watchlist_manager import _get_setting, _set_setting
    import json
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    layout = data.get("layout")
    if not name or layout is None:
        return jsonify({"ok": False, "error": "name and layout are both required"}), 400

    _set_setting(f"realtime_layout:{name}", json.dumps(layout))

    raw = _get_setting("realtime_layout_index", "[]")
    try:
        names = json.loads(raw) or []
    except Exception:
        names = []
    if name not in names:
        names.append(name)
    _set_setting("realtime_layout_index", json.dumps(names))

    return jsonify({"ok": True, "name": name})


@realtime_bp.route("/layouts/<name>", methods=["GET"])
def layouts_load(name):
    from ..scanners.watchlist_manager import _get_setting
    import json
    raw = _get_setting(f"realtime_layout:{name}", None)
    if raw is None:
        return jsonify({"ok": False, "error": "layout not found"}), 404
    try:
        layout = json.loads(raw)
    except Exception:
        return jsonify({"ok": False, "error": "corrupt layout data"}), 500
    return jsonify({"ok": True, "name": name, "layout": layout})


@realtime_bp.route("/layouts/<name>", methods=["DELETE"])
def layouts_delete(name):
    from ..scanners.watchlist_manager import _get_setting, _set_setting
    import json
    raw = _get_setting("realtime_layout_index", "[]")
    try:
        names = json.loads(raw) or []
    except Exception:
        names = []
    if name in names:
        names.remove(name)
    _set_setting("realtime_layout_index", json.dumps(names))
    _set_setting(f"realtime_layout:{name}", "")
    return jsonify({"ok": True})


@realtime_bp.route("/last_layout", methods=["GET"])
def last_layout_get():
    from ..scanners.watchlist_manager import _get_setting
    import json
    raw = _get_setting("realtime_last_layout", None)
    if raw is None:
        return jsonify({"ok": False, "layout": None})
    try:
        return jsonify({"ok": True, "layout": json.loads(raw)})
    except Exception:
        return jsonify({"ok": False, "layout": None})


@realtime_bp.route("/last_layout", methods=["POST"])
def last_layout_save():
    from ..scanners.watchlist_manager import _set_setting
    import json
    data = request.get_json(silent=True) or {}
    _set_setting("realtime_last_layout", json.dumps(data))
    return jsonify({"ok": True})


@realtime_bp.route("/templates", methods=["GET"])
def templates_list():
    from ..scanners.watchlist_manager import _get_setting
    import json
    raw = _get_setting("realtime_template_index", "[]")
    try:
        names = json.loads(raw) or []
    except Exception:
        names = []
    return jsonify({"templates": names})


@realtime_bp.route("/templates", methods=["POST"])
def templates_save():
    from ..scanners.watchlist_manager import _get_setting, _set_setting
    import json
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    config = data.get("config")
    if not name or config is None:
        return jsonify({"ok": False, "error": "name and config are both required"}), 400

    _set_setting(f"realtime_template:{name}", json.dumps(config))

    raw = _get_setting("realtime_template_index", "[]")
    try:
        names = json.loads(raw) or []
    except Exception:
        names = []
    if name not in names:
        names.append(name)
    _set_setting("realtime_template_index", json.dumps(names))

    return jsonify({"ok": True, "name": name})


@realtime_bp.route("/templates/<name>", methods=["GET"])
def templates_load(name):
    from ..scanners.watchlist_manager import _get_setting
    import json
    raw = _get_setting(f"realtime_template:{name}", None)
    if raw is None:
        return jsonify({"ok": False, "error": "template not found"}), 404
    try:
        config = json.loads(raw)
    except Exception:
        return jsonify({"ok": False, "error": "corrupt template data"}), 500
    return jsonify({"ok": True, "name": name, "config": config})


@realtime_bp.route("/templates/<name>", methods=["DELETE"])
def templates_delete(name):
    from ..scanners.watchlist_manager import _get_setting, _set_setting
    import json
    raw = _get_setting("realtime_template_index", "[]")
    try:
        names = json.loads(raw) or []
    except Exception:
        names = []
    if name in names:
        names.remove(name)
    _set_setting("realtime_template_index", json.dumps(names))
    _set_setting(f"realtime_template:{name}", "")
    return jsonify({"ok": True})


@realtime_bp.route("/config", methods=["GET"])
def tastytrade_config_get():
    from ..services.tastytrade_feed import tastytrade_configured, _runtime_credentials
    client_secret, _ = _runtime_credentials()
    return jsonify({
        "configured": tastytrade_configured(),
        # show only a masked hint, never the full secret back
        "client_secret_hint": (client_secret[:4] + "…") if client_secret else None,
    })


@realtime_bp.route("/config", methods=["POST"])
def tastytrade_config_set():
    from ..scanners.watchlist_manager import _set_setting
    data = request.get_json(silent=True) or request.form
    client_secret = (data.get("client_secret") or "").strip()
    refresh_token = (data.get("refresh_token") or "").strip()
    if not client_secret or not refresh_token:
        return jsonify({"ok": False, "error": "client_secret and refresh_token are both required"}), 400
    _set_setting("tastytrade_client_secret", client_secret)
    _set_setting("tastytrade_refresh_token", refresh_token)
    feed.reset()  # force re-login with the credentials just saved, not a stale cached session

    # feed.start() -> Session(client_secret, refresh_token) does a
    # SYNCHRONOUS, UNTIMED login HTTP call to Tastytrade's OAuth endpoint.
    # Previously that ran inline on this request's waitress worker thread
    # with no timeout at all -- a bad token or slow network meant this
    # handler simply never returned, the Save button looked stuck, and
    # every retry click consumed another one of the (default 8) waitress
    # worker threads until the whole app stopped responding to anything.
    # The credentials are already saved above regardless of what happens
    # next, so bound the connection attempt instead of blocking on it:
    # give it a few seconds inline (covers the common "it just works"
    # case with an instant response), and if it's still not done, return
    # right away and let it keep trying in the background.
    from ..services.task_executor import get_executor
    import concurrent.futures as _cf
    fut = get_executor().submit(feed.start)
    try:
        fut.result(timeout=6)
        start_error = None
        still_connecting = False
    except _cf.TimeoutError:
        start_error = None
        still_connecting = True
    except Exception as e:  # noqa: BLE001 - surface it, don't crash the request
        start_error = str(e)
        still_connecting = False

    return jsonify({
        "ok": True,
        "configured": True,
        "feed_start_error": start_error,
        "still_connecting": still_connecting,
    })


@realtime_bp.route("/api/<path:symbol>/snapshot")
def snapshot(symbol: str):
    symbol = _decode_path_symbol(symbol)
    timeframe = request.args.get("timeframe", DEFAULT_TIMEFRAME)
    tt_quote = feed.get_snapshot(symbol)
    result = {"symbol": symbol, "quote": tt_quote}

    df = get_recent_bars(symbol, 300, timeframe)
    if df is not None and not df.empty:
        result["avwap"] = _avwap_engine.compute(df)
        result["volquant"] = _volquant_engine.compute(df)
    else:
        result["avwap"] = {"status": "no_bars"}
        result["volquant"] = {"status": "no_bars"}

    if tt_quote.get("mid"):
        spot = tt_quote["mid"]
        db_gex = get_gex_from_db(symbol, spot)
        if db_gex is not None:
            result["gex"] = db_gex
        else:
            mult = _CONTRACT_MULTIPLIERS.get(symbol, 100)
            rows = get_option_chain(symbol)
            engine = GEXEngine(contract_multiplier=mult)
            result["gex"] = engine.compute(spot=spot, rows=rows) if rows else {"status": "no_chain_data"}
    else:
        result["gex"] = {"status": "no_quote_yet"}

    result["futures_positioning"] = get_futures_positioning(symbol)

    if df is not None and not df.empty:
        avwap_series = _avwap_engine.compute_series(df)
        result["composite_signal"] = _composite_engine.compute(df, avwap_series, result["gex"], result["volquant"])
    else:
        result["composite_signal"] = {"signal": "NONE", "tier": None, "confirming_modules": [], "modules": {}}

    return jsonify(result)


@realtime_bp.route("/api/<path:symbol>/price")
def latest_price(symbol: str):
    """Lightweight, fast price check for updating the currently-forming
    candle incrementally, instead of re-fetching the full historical
    backfill on every refresh tick. Uses the cheap REST quote
    (feed.get_snapshot -> get_market_data), NOT a DXLink candle fetch --
    this is the whole point: full bars/indicators only run on symbol
    change or the slower full-resync interval; every fast tick just needs
    a current price to update the last candle client-side."""
    symbol = _decode_path_symbol(symbol)
    snap = feed.get_snapshot(symbol)
    price = snap.get("mid") or snap.get("last") or snap.get("mark")
    if price is None:
        return jsonify({"status": snap.get("status", "no_data"), "error": snap.get("error")})
    return jsonify({"status": "live", "price": price, "volume": snap.get("volume")})


def _recent_fetch_error(symbol: str, timeframe: str, extended_hours: bool = True):
    """Returns the error message for this (symbol, timeframe) if it failed
    recently (within 60s), else None -- avoids showing a stale error
    indefinitely after a transient issue has since resolved."""
    entry = _last_fetch_error.get((symbol, timeframe, extended_hours))
    if entry and (time.time() - entry[0]) < 60:
        return entry[1]
    return None


@realtime_bp.route("/api/<path:symbol>/bars")
def bars(symbol: str):
    symbol = _decode_path_symbol(symbol)
    timeframe = request.args.get("timeframe", DEFAULT_TIMEFRAME)
    extended_hours = request.args.get("extended_hours", "1") == "1"
    df = get_recent_bars(symbol, 300, timeframe, extended_hours)
    if df is None or df.empty:
        return jsonify({"status": "no_bars", "error": _recent_fetch_error(symbol, timeframe, extended_hours)})

    # Strong-candle strength tier per bar, using the EXACT same formula
    # already established for StrongBullCandle/StrongBearCandle in
    # scanner_builder.py (imported directly, not re-derived here, so this
    # can never silently drift from what the scanners consider "strong"):
    #   change_pct = (close - prev_close) / abs(prev_close) * 100
    #   avg_abs    = EMA(abs(change_pct), span=60).shift(1)   <- known
    #                BEFORE the candidate bar, avoiding lookahead bias
    #   ratio      = abs(change_pct) / avg_abs
    # tier 2 (strongest) if ratio > 2, tier 1 (medium) if ratio > 1.5,
    # tier 0 (normal) otherwise. Direction (bull/bear) is already
    # derivable client-side from close >= open, same as existing
    # volume-bar coloring, so only the magnitude tier is sent here.
    try:
        from .scanner_builder import _change_pct_and_avg_abs_series
        change_pct, avg_abs = _change_pct_and_avg_abs_series(df["close"], avg_bars=60)
        ratio = (change_pct.abs() / avg_abs.replace(0, float("nan"))).fillna(0.0)
        tiers = pd.Series(0, index=df.index)
        tiers[ratio > 1.5] = 1
        tiers[ratio > 2.0] = 2
    except Exception as e:  # noqa: BLE001
        print(f"[realtime_dashboard] strong-candle tier computation failed for {symbol}: {e}")
        tiers = pd.Series(0, index=df.index)

    out = []
    for ts, row in df.iterrows():
        o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)
        # A bar with NaN OHLC isn't a valid candle to send at all -- Python's
        # json module serializes float('nan') as a literal, UNQUOTED `NaN`
        # token, which is not valid per strict JSON (RFC 8259). The browser's
        # response.json() (JSON.parse() under the hood) rejects it outright,
        # which is exactly what "Server returned an unreadable response
        # (HTTP 200)" meant -- the server thought it succeeded (a real 200,
        # a real Python dict), but the serialized body itself was malformed.
        # Confirmed indicator_engines.py's _to_series() already guards
        # against this correctly (filters NaN/None points) -- this route
        # just didn't have the same guard. Dropping the bar entirely rather
        # than substituting 0 or null: a 0-price candle would visually
        # corrupt the chart far worse than the bar simply being absent.
        if not (math.isfinite(o) and math.isfinite(h) and math.isfinite(l) and math.isfinite(c)):
            continue
        vol = float(row.volume) if row.volume is not None else 0.0
        if not math.isfinite(vol):
            vol = 0.0
        out.append({
            "time": int(ts.timestamp()),
            "open": round(o, 4),
            "high": round(h, 4),
            "low": round(l, 4),
            "close": round(c, 4),
            "volume": round(vol, 2),
            "strength_tier": int(tiers.loc[ts]) if ts in tiers.index else 0,
        })
    return jsonify(out)


@realtime_bp.route("/api/symbol-search")
def symbol_search_route():
    """Backs the Symbol Search modal -- wraps tastytrade's own
    /symbols/search/{q} endpoint (the same lookup their web platform's
    search box uses), returns [{symbol, description, instrument_type}].
    `category` filters client-recognizable results down to what the
    Symbol Search UI's category buttons ask for (stocks/futures/options);
    filtering happens here rather than asking tastytrade's search
    endpoint for a specific type, since that endpoint doesn't accept a
    type filter itself.
    """
    q = (request.args.get("q") or "").strip()
    category = (request.args.get("category") or "all").strip().lower()
    if not q:
        return jsonify({"results": []})
    try:
        results = feed.search_symbols(q, limit=40)
    except Exception as e:  # noqa: BLE001
        return jsonify({"results": [], "error": str(e)})

    if category != "all":
        wanted = {
            "stocks": {"Equity"},
            "futures": {"Future"},
            "options": {"Equity Option", "Future Option"},
        }.get(category)
        if wanted:
            results = [r for r in results if r.get("instrument_type") in wanted]
    return jsonify({"results": results})


@realtime_bp.route("/api/<path:symbol>/option-chain")
def option_chain_route(symbol: str):
    """Backs the Options Chain modal -- expiration tabs + strike/symbol
    columns for calls and puts, using tastytrade's OWN ready-made
    symbols for every contract (see get_nested_option_chain above).
    Picking a row here gives the frontend a real, tastytrade-confirmed
    streamer symbol to chart/stream -- nothing is hand-built.
    """
    try:
        chain = feed.get_nested_option_chain(_decode_path_symbol(symbol).upper().strip())
    except Exception as e:  # noqa: BLE001
        return jsonify({"underlying_symbol": symbol, "expirations": [], "error": str(e)})
    return jsonify(chain)


@realtime_bp.route("/api/<path:symbol>/indicators")
def indicators(symbol: str):
    """Full time-series (not just latest value) for EMA5/9/20/50/200,
    RSI14 + RSIDiff90 (your exact scanner_builder formula), MACD, ADX,
    and AVWAP bands — everything the chart needs to draw actual overlay
    lines and sub-panes instead of a static numbers panel.

    Accepts an optional ?want=ema5,ema9,avwap_bands,rsi14,... query param
    (comma-separated) to only include the requested keys in the response --
    trims payload size to whatever's actually toggled on in the frontend's
    Indicators popover, rather than always sending every series. Note: the
    underlying computation (_technical_engine.compute(), etc.) still runs
    in full either way -- it's cheap pandas math on already-fetched bars,
    not a separate network call, so this is a payload-size optimization,
    not a fetch-avoidance one. The actual expensive part (the DXLink
    candle fetch itself) is already deduped by get_recent_bars()'s cache
    + single-flight coalescing above, regardless of `want`."""
    symbol = _decode_path_symbol(symbol)
    timeframe = request.args.get("timeframe", DEFAULT_TIMEFRAME)
    want_param = request.args.get("want", "").strip()
    want = set(w for w in want_param.split(",") if w) if want_param else None

    # Cache the full (unfiltered) computed result per (symbol, timeframe) --
    # if two windows share the same symbol+timeframe (e.g. two windows both
    # on SPY 5m), the second one reuses this instead of redundantly
    # recomputing EMA/RSI/MACD/ADX/AVWAP/VolQuant/S-R from scratch. `want`
    # filtering happens after the cache lookup so different popover
    # selections don't cause unnecessary cache misses -- the full
    # computation is shared, only the returned subset differs per request.
    cache_key = (symbol, timeframe)
    cached = _indicators_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _INDICATORS_CACHE_TTL:
        result = cached[1]
    else:
        df = get_recent_bars(symbol, 500, timeframe)
        if df is None or df.empty:
            return jsonify({"status": "no_bars", "error": _recent_fetch_error(symbol, timeframe)})

        result = _technical_engine.compute(df)
        result["avwap_bands"] = _avwap_engine.compute_series(df)
        result["volquant_series"] = _volquant_engine.compute_series(df)
        result["_last_close"] = float(df["close"].iloc[-1])  # kept in the cached result so sr_count
                                                                # classification below works on cache
                                                                # hits too, without a second price fetch

        try:
            from ..charts.chart_primitives import tv_sr_channels
            sr_df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
            # Always compute a generous fixed max here (this branch is
            # cached across requests) -- how many the caller actually
            # wants to SEE (sr_count) is applied after the cache lookup
            # below via nearest_levels(), same pattern as `want` filtering
            # above, so different sr_count values don't each need their
            # own cache entry or redundant recomputation.
            result["sr_channels"] = tv_sr_channels(sr_df, max_sr=10)
        except Exception as e:  # noqa: BLE001
            print(f"[realtime_dashboard] tv_sr_channels failed for {symbol}: {e}")
            result["sr_channels"] = []

        # Volume Profile reference levels (POC/VAH/VAL) for today
        # (developing), D1, W1, M1 -- drawn as horizontal price lines on
        # the chart. Reads from a SEPARATE local cache (hourly bars, via
        # _history()) plus four _volume_profile() binning passes, not
        # derived from the already-fetched `df` above -- unlike every
        # other indicator in this cached branch, this genuinely costs an
        # extra DB round-trip. Computing it unconditionally on every
        # cache-miss (every 20s, per open window) regardless of whether
        # that window's "Volume Profile POC" overlay is even checked was
        # a real, avoidable cost -- gated behind `want` here as a
        # deliberate exception to the "compute everything, filter after"
        # pattern used above for sr_channels. Trade-off: if a window
        # toggles the overlay ON mid-cache-window, it can take up to the
        # remaining TTL (<=20s) to appear, since a cache entry populated
        # without it won't retroactively gain it until the next
        # recompute. Acceptable given the alternative was every window
        # paying this cost every cycle whether it wanted it or not.
        if want is None or "vp_levels" in want:
            try:
                from .volume_profile_scanner import get_chart_levels
                result["vp_levels"] = get_chart_levels(symbol)
            except Exception as e:  # noqa: BLE001
                print(f"[realtime_dashboard] volume profile levels failed for {symbol}: {e}")
                result["vp_levels"] = None

        _indicators_cache[cache_key] = (time.time(), result)

    # Classify the cached raw S/R channels into ranked support/resistance
    # levels (R1 = nearest resistance above spot, R2 = next, ... same for
    # S1/S2/... below) relative to the current close, sized to whatever
    # the frontend's "S/R count" control asked for. Runs on every
    # request (not just cache misses) since it's cheap sorting/filtering
    # over an already-computed channel list, not a recomputation.
    sr_count = request.args.get("sr_count", "3")
    try:
        sr_count = int(sr_count)
    except (TypeError, ValueError):
        sr_count = 3
    spot = result.get("_last_close")
    if spot is not None and result.get("sr_channels"):
        from ..charts.chart_primitives import nearest_levels
        supports, resistances = nearest_levels(result["sr_channels"], spot, count=sr_count)
        result["sr_levels"] = {"supports": supports, "resistances": resistances}
    else:
        result["sr_levels"] = {"supports": [], "resistances": []}

    if want:
        result = {k: v for k, v in result.items() if k in want}

    return jsonify(result)


@realtime_bp.route("/api/<path:symbol>/bubbles/start", methods=["POST"])
def bubbles_start(symbol: str):
    """Starts (idempotent) the live Trade+Quote classification stream for
    a symbol -- call this once when the bubble layer is toggled on for a
    chart window, then poll /bubbles below. See tastytrade_feed.py's
    ensure_trade_stream() docstring for the caveats on this being
    unverified against a live connection in this environment."""
    symbol = _decode_path_symbol(symbol)
    try:
        result = feed.ensure_trade_stream(symbol)
    except Exception as e:  # noqa: BLE001
        return jsonify({"started": False, "already_running": False, "error": str(e)})
    return jsonify(result)


@realtime_bp.route("/api/<path:symbol>/bubbles")
def bubbles_poll(symbol: str):
    """Poll for classified trades since `since` (unix seconds, optional --
    omit for the full buffer) with size >= `min_size`. Frontend calls this
    on a short timer once the bubble layer is on, tracking its own last-
    seen timestamp so each poll only needs the delta."""
    symbol = _decode_path_symbol(symbol)
    since = request.args.get("since")
    try:
        since_ts = float(since) if since else None
    except (TypeError, ValueError):
        since_ts = None
    try:
        min_size = float(request.args.get("min_size", "0") or "0")
    except (TypeError, ValueError):
        min_size = 0.0
    status = feed.trade_stream_status(symbol)
    trades = feed.get_recent_trades(symbol, since_ts=since_ts, min_size=min_size)
    return jsonify({"status": status, "trades": trades, "server_time": time.time()})


@realtime_bp.route("/setup")
def setup_page():
    return render_template_string(_SETUP_HTML)


@realtime_bp.route("/")
def index_page():
    from ..services.tastytrade_feed import tastytrade_configured
    return render_template_string(_PAGE_HTML, symbol="SPY", configured=tastytrade_configured())


@realtime_bp.route("/<path:symbol>")
def page(symbol: str):
    from ..services.tastytrade_feed import tastytrade_configured
    return render_template_string(_PAGE_HTML, symbol=symbol, configured=tastytrade_configured())


def init_realtime(default_symbols=None):
    """Call from app_factory.create_app(). Starts the background feed."""
    feed.start(symbols=default_symbols or [])


_SETUP_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Tastytrade Setup</title>
  <style>
    body { background:#0f172a; color:#e2e8f0; font-family: -apple-system, sans-serif;
           margin:0; display:flex; justify-content:center; padding:60px 20px; }
    .box { background:#1e293b; border-radius:12px; padding:32px; width:100%; max-width:460px; }
    h1 { font-size:18px; margin:0 0 4px; }
    p.sub { color:#94a3b8; font-size:13px; margin:0 0 16px; line-height:1.5; }
    p.sub a { color:#818cf8; }
    ol.steps { color:#94a3b8; font-size:12px; line-height:1.7; margin:0 0 24px; padding-left:18px; }
    ol.steps a { color:#818cf8; }
    label { display:block; font-size:12px; color:#94a3b8; margin:16px 0 6px; text-transform:uppercase; letter-spacing:.04em; }
    input { width:100%; box-sizing:border-box; background:#0f172a; border:1px solid #334155;
            border-radius:6px; padding:10px 12px; color:#e2e8f0; font-size:14px; font-family: monospace; }
    input:focus { outline:none; border-color:#6366f1; }
    button { margin-top:24px; width:100%; padding:12px; border:none; border-radius:6px;
             background:#6366f1; color:white; font-size:14px; font-weight:600; cursor:pointer; }
    button:hover { background:#4f46e5; }
    button:disabled { background:#334155; cursor:not-allowed; }
    #status { margin-top:16px; font-size:13px; padding:10px 12px; border-radius:6px; display:none; }
    #status.ok { display:block; background:#14532d; color:#bbf7d0; }
    #status.err { display:block; background:#7f1d1d; color:#fecaca; }
    .current { font-size:12px; color:#64748b; margin-top:20px; }
    .box-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
    .close-btn { background:transparent; border:1px solid #334155; color:#94a3b8; width:28px; height:28px;
                 border-radius:8px; font-size:16px; line-height:1; cursor:pointer; flex:0 0 auto;
                 display:flex; align-items:center; justify-content:center; padding:0; margin:0; }
    .close-btn:hover { background:#334155; color:#e2e8f0; }
  </style>
</head>
<body>
  <div class="box">
    <div class="box-head">
      <h1>Connect Tastytrade</h1>
      <button type="button" class="close-btn" id="closeBtn" title="Back to Scheduler">✕</button>
    </div>
    <p class="sub">Tastytrade requires OAuth2 — no username/password. Stored in oiapp's own settings database, same as your Telegram bot credentials.</p>

    <ol class="steps">
      <li>Create an OAuth app at <a href="https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications" target="_blank">my.tastytrade.com → API Access → OAuth Applications</a>. Add <code>http://localhost:8000</code> as a callback URL. Save the <b>client secret</b>.</li>
      <li>Go to <b>OAuth Applications → Manage → Create Grant</b> to generate a <b>refresh token</b> (it never expires). Save that too.</li>
      <li>Paste both below — this is a one-time setup.</li>
    </ol>

    <form id="f">
      <label for="client_secret">Client Secret</label>
      <input id="client_secret" type="password" autocomplete="off" required />

      <label for="refresh_token">Refresh Token</label>
      <input id="refresh_token" type="password" autocomplete="off" required />

      <button type="submit" id="submitBtn">Save &amp; Connect</button>
    </form>

    <div id="status"></div>
    <div class="current" id="current">Checking current status…</div>
  </div>

  <script>
    async function refreshStatus() {
      const r = await fetch('/realtime/config');
      const d = await r.json();
      const el = document.getElementById('current');
      el.textContent = d.configured
        ? `Currently connected (client secret starting: ${d.client_secret_hint})`
        : 'Not connected yet.';
    }

    document.getElementById('f').addEventListener('submit', async (e) => {
      e.preventDefault();
      const client_secret = document.getElementById('client_secret').value.trim();
      const refresh_token = document.getElementById('refresh_token').value.trim();
      const btn = document.getElementById('submitBtn');
      const status = document.getElementById('status');
      btn.disabled = true;
      btn.textContent = 'Connecting…';
      status.className = '';
      status.style.display = 'none';

      try {
        const r = await fetch('/realtime/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ client_secret, refresh_token }),
        });
        const d = await r.json();
        if (d.ok && d.still_connecting) {
          status.className = 'ok';
          status.textContent = '✓ Saved. Still connecting in the background (this can take a few seconds) — refresh this page shortly to confirm.';
          document.getElementById('client_secret').value = '';
          document.getElementById('refresh_token').value = '';
        } else if (d.ok && !d.feed_start_error) {
          status.className = 'ok';
          status.textContent = '✓ Saved and connected. You can go to /realtime/<symbol> now.';
          document.getElementById('client_secret').value = '';
          document.getElementById('refresh_token').value = '';
        } else if (d.ok && d.feed_start_error) {
          status.className = 'err';
          status.textContent = `Saved, but couldn't connect: ${d.feed_start_error}`;
        } else {
          status.className = 'err';
          status.textContent = d.error || 'Something went wrong.';
        }
      } catch (err) {
        status.className = 'err';
        status.textContent = 'Request failed: ' + err;
      } finally {
        btn.disabled = false;
        btn.textContent = 'Save & Connect';
        refreshStatus();
      }
    });

    refreshStatus();

    document.getElementById('closeBtn').addEventListener('click', () => {
      // Prefer going back to wherever they came from (e.g. Scheduler Hub's
      // "Set up" link); fall back to Scheduler Hub directly if this page
      // was opened fresh (no useful browser history -- e.g. a bookmark).
      if (document.referrer && document.referrer.includes(window.location.host)) {
        window.location.href = document.referrer;
      } else {
        window.location.href = '/scheduler-hub';
      }
    });
  </script>
</body>
</html>
"""

_PAGE_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Realtime Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    body { background:#0f172a; color:#e2e8f0; font-family: -apple-system, sans-serif; margin:0;
           height:100vh; overflow:hidden; display:flex; flex-direction:column; }
    .banner { background:#7c2d12; color:#fed7aa; padding:10px 20px; font-size:13px; flex-shrink:0; }
    .banner a { color:#fdba74; font-weight:600; }

    .top-toolbar { display:flex; align-items:center; gap:12px; padding:10px 20px; background:#111827;
                   border-bottom:1px solid #1f2937; flex-shrink:0; }
    .top-toolbar button { background:#6366f1; border:none; color:white; padding:7px 14px;
                           border-radius:6px; font-size:13px; cursor:pointer; font-weight:600; }
    .top-toolbar button:disabled { background:#334155; cursor:not-allowed; }
    .top-toolbar .count { font-size:12px; color:#64748b; }
    .top-toolbar .sidebar-toggle { margin-left:auto; background:#1e293b; border:1px solid #334155; color:#94a3b8; }
    .theme-toggle-btn { background:#1e293b !important; border:1px solid #334155; color:#94a3b8 !important; font-weight:600; }
    /* Scoped light theme: chart backgrounds/grid/text are handled per-chart
       via setChartTheme() in JS (Lightweight Charts options, not CSS) --
       this class only covers the surrounding page chrome (window cards,
       toolbars, popovers) so the two stay visually consistent. Not an
       exhaustive re-theme of every control in the page. */
    body.light-theme { background:#f1f5f9; color:#1e293b; }
    body.light-theme .top-toolbar, body.light-theme .win-toolbar,
    body.light-theme #watchlist-sidebar { background:#ffffff; border-color:#e2e8f0; color:#1e293b; }
    body.light-theme .window { background:#ffffff; border-color:#e2e8f0; }
    body.light-theme .symInput, body.light-theme select, body.light-theme input[type=number] {
      background:#f8fafc; border-color:#cbd5e1; color:#1e293b;
    }
    body.light-theme .ind-popover-panel { background:#ffffff; border-color:#e2e8f0; color:#1e293b; }
    body.light-theme .sr-count-label, body.light-theme .grp-label { color:#64748b; }
    body.light-theme .tf-btn { color:#475569; }
    body.light-theme .tf-btn.active { background:#6366f1; color:white; }
    #refreshRateSelect { background:#1e293b; border:1px solid #334155; color:#e2e8f0; padding:4px 6px;
                          border-radius:5px; font-size:12px; }
    .tf-group { display:flex; gap:2px; background:#1e293b; border-radius:6px; padding:2px; }
    .tf-btn { background:transparent; border:none; color:#94a3b8; padding:5px 10px; border-radius:4px;
              font-size:12px; cursor:pointer; }
    .tf-btn.active { background:#6366f1; color:white; }
    .tf-btn:hover:not(.active) { color:#e2e8f0; }

    /* Previously: no defined height anywhere from body down to #grid, and
       .app-layout used align-items:flex-start (content-sized children) --
       so #grid's flex:1 had no bounded parent height to divide, and each
       .window just grew to its natural (fixed-px) content height. A 2x2
       layout's total content height routinely exceeded the viewport,
       forcing the whole page to scroll instead of the 4 windows actually
       fitting on screen. Fixed by pinning body to 100vh and threading
       flex:1/min-height:0 all the way down to .window, so grid rows
       genuinely divide the available viewport height and each window's
       own content (chart + panes) is what shrinks to fit, not the page. */
    .app-layout { display:flex; align-items:stretch; flex:1; min-height:0; }
    #grid { display:grid; gap:8px; padding:8px; grid-template-columns: 1fr; flex:1; min-width:0; min-height:0; }
    #grid.cols-2 { grid-template-columns: 1fr 1fr; }

    #watchlist-sidebar { width:260px; flex-shrink:0; background:#111827; border-left:1px solid #1f2937;
                          height:100%; overflow-y:auto; display:block; }
    #watchlist-sidebar.collapsed { display:none; }
    #watchlist-sidebar .wl-header { padding:10px 14px; font-size:11px; text-transform:uppercase;
                                     letter-spacing:.05em; color:#64748b; border-bottom:1px solid #1f2937; }
    #watchlist-sidebar .wl-search { width:calc(100% - 24px); margin:8px 12px; padding:6px 10px;
                                     background:#1e293b; border:1px solid #334155; border-radius:5px;
                                     color:#e2e8f0; font-size:12px; box-sizing:border-box; }
    #watchlist-sidebar .wl-select { width:calc(100% - 24px); margin:8px 12px 0; padding:6px 8px;
                                     background:#1e293b; border:1px solid #334155; border-radius:5px;
                                     color:#e2e8f0; font-size:12px; box-sizing:border-box; }
    .wl-item { display:flex; justify-content:space-between; padding:6px 14px; font-size:12px; cursor:pointer; }
    .wl-item:hover { background:#1e293b; }
    .wl-item .sym { font-weight:600; }

    .panel-toggle-row { display:flex; align-items:center; gap:6px; padding:4px 12px; cursor:pointer;
                         font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:.05em; }
    .panel-toggle-row:hover { color:#e2e8f0; }
    .panel.collapsed { display:none; }

    /* Was height:14px with margin:-4px 0 (a "large hit area, small visual
       footprint" trick) -- negative margins on flex items don't collapse
       the way block-flow margins do, but the exact net visual gap that
       arithmetic produces is hard to verify without a live render, and
       it's exactly the kind of thing that can look like an unwanted gap
       between the price and RSI/MACD panels. Simplified to a small
       positive height and no margin trick at all -- guarantees the
       handle only ever occupies exactly its own height, nothing more,
       at the cost of a slightly smaller (but still comfortably
       draggable) hit target. */
    .resize-handle { height:8px; cursor:row-resize; background:transparent; position:relative;
                     z-index:5; user-select:none; touch-action:none; flex-shrink:0; }
    .resize-handle::after { content:''; position:absolute; left:50%; top:50%; transform:translate(-50%,-50%);
                             width:40px; height:4px; background:#334155; border-radius:2px; }
    .resize-handle:hover::after, .resize-handle.dragging::after { background:#6366f1; height:5px; }

    /* Draggable divider BETWEEN windows in the grid (2-column and/or
       2-row splits only -- see buildTrackTemplate()'s comment). Placed
       into its own 6px grid track by JS (grid-column/grid-row set
       inline), so its position is entirely grid-managed -- no manual
       geometry/offset calculation needed. The thin visible bar is an
       ::after pseudo-element centered within that 6px track, same
       "narrow hit target, wider visible+hover bar" idea as
       .resize-handle above, just oriented per axis. */
    .grid-divider { background:transparent; position:relative; z-index:8; user-select:none; touch-action:none; }
    .grid-divider-vertical { cursor:col-resize; }
    .grid-divider-horizontal { cursor:row-resize; }
    .grid-divider::after { content:''; position:absolute; background:#334155; border-radius:2px; }
    .grid-divider-vertical::after { top:8px; bottom:8px; left:50%; width:2px; transform:translateX(-50%); }
    .grid-divider-horizontal::after { left:8px; right:8px; top:50%; height:2px; transform:translateY(-50%); }
    .grid-divider:hover::after, .grid-divider.dragging::after { background:#6366f1; }
    .grid-divider-vertical:hover::after, .grid-divider-vertical.dragging::after { width:3px; }
    .grid-divider-horizontal:hover::after, .grid-divider-horizontal.dragging::after { height:3px; }

    .fetch-error-banner { margin:0 12px 8px; padding:10px 14px; border-radius:8px; font-size:13px;
                           font-weight:600; background:#7f1d1d; color:#fecaca; flex-shrink:0; }
    .replay-pick-banner { position:absolute; top:8px; left:8px; right:8px; z-index:15;
                           padding:8px 14px; border-radius:8px; font-size:12px;
                           font-weight:600; background:#1e3a8a; color:#bfdbfe;
                           display:flex; align-items:center; gap:8px; box-shadow:0 4px 12px rgba(0,0,0,.35); }
    .rvp-pick-banner { position:absolute; top:8px; left:8px; right:8px; z-index:15;
                        padding:8px 14px; border-radius:8px; font-size:12px;
                        font-weight:600; background:#78350f; color:#fde68a;
                        display:flex; align-items:center; gap:8px; box-shadow:0 4px 12px rgba(0,0,0,.35); }
    .replay-controls { margin:0 12px 8px; padding:6px 10px; border-radius:8px; flex-shrink:0;
                        background:#111827; border:1px solid #334155;
                        display:flex; align-items:center; gap:8px; }
    .replayBtn.active { background:#6366f1; color:#fff; }
    .replayPlayBtn.playing { background:#dc2626; color:#fff; }

    .alert-menu { position:fixed; z-index:100; background:#1e293b; border:1px solid #334155; border-radius:8px;
                  padding:8px; box-shadow:0 8px 24px rgba(0,0,0,0.5); font-size:13px; min-width:220px; }
    .alert-menu .price-line { color:#94a3b8; font-size:11px; padding:2px 4px 8px; border-bottom:1px solid #334155; margin-bottom:6px; }
    .alert-menu button { display:block; width:100%; text-align:left; background:transparent; border:none;
                          color:#e2e8f0; padding:7px 8px; border-radius:5px; cursor:pointer; font-size:13px; }
    .alert-menu button:hover { background:#334155; }

    .window { background:#0b1220; border:2px solid #1f2937; border-radius:8px; overflow-x:hidden; overflow-y:auto;
              height:100%; min-height:0; display:flex; flex-direction:column; }
    .window.active { border-color:#6366f1; }

    .win-toolbar { display:flex; align-items:center; gap:8px; padding:8px 12px; background:#111827; flex-shrink:0;
                   border-bottom:1px solid #1f2937; flex-wrap:wrap; position:sticky; top:0; z-index:20; }
    .win-toolbar .sym-label { font-weight:700; font-size:14px; color:#e2e8f0; min-width:50px; }
    .win-toolbar input[type=text] { width:70px; background:#1e293b; border:1px solid #334155; color:#e2e8f0;
                                     padding:5px 8px; border-radius:5px; font-size:12px; text-transform:uppercase; }
    .win-toolbar button.small { background:#1e293b; border:1px solid #334155; color:#94a3b8; padding:5px 10px;
                                 border-radius:5px; font-size:12px; cursor:pointer; }
    .win-toolbar button.small:hover { border-color:#6366f1; color:#e2e8f0; }
    .win-toolbar button.small.active { background:#6366f1; border-color:#818cf8; color:#fff; font-weight:600; }
    .win-toolbar button.small.active:hover { background:#4f46e5; border-color:#818cf8; color:#fff; }
    .win-toolbar .close-btn { margin-left:auto; background:none; border:none; color:#64748b; cursor:pointer;
                               font-size:16px; padding:0 6px; }
    .win-toolbar .close-btn:hover { color:#ef4444; }

    .interval-row { display:flex; gap:2px; background:#1e293b; border-radius:6px; padding:2px; }
    .interval-row button { background:transparent; border:none; color:#94a3b8; padding:4px 8px;
                            border-radius:4px; font-size:11px; cursor:pointer; }
    .interval-row button.active { background:#6366f1; color:white; }
    .interval-row button:hover:not(.active) { color:#e2e8f0; }

    .ind-popover { position:relative; }
    .ind-popover-panel { display:none; position:absolute; top:110%; left:0; z-index:10; background:#1e293b;
                          border:1px solid #334155; border-radius:8px; padding:10px; min-width:220px;
                          box-shadow:0 8px 24px rgba(0,0,0,0.4); }
    .ind-popover-panel.open { display:block; }
    .ind-popover-panel .grp-label { font-size:10px; color:#64748b; text-transform:uppercase; letter-spacing:.05em;
                                     margin:8px 0 4px; }
    .ind-popover-panel label { display:flex; align-items:center; gap:6px; font-size:12px; color:#e2e8f0;
                                padding:2px 0; cursor:pointer; }
    .ind-popover-panel input { accent-color:#6366f1; }

    .panel { display:flex; gap:10px; padding:8px 12px; flex-wrap:wrap; }
    .card { background:#1e293b; border-radius:8px; padding:10px 12px; min-width:150px; font-size:12px; }
    .card h3 { margin:0 0 6px; font-size:10px; color:#94a3b8; text-transform:uppercase; letter-spacing:.05em; }
    .row { display:flex; justify-content:space-between; font-size:12px; padding:1px 0; gap:10px; }
    .muted { color:#64748b; }
    .up { color:#22c55e; } .down { color:#ef4444; }

    .signal-banner { display:none; margin:0 12px 8px; padding:10px 14px; border-radius:8px; font-size:13px; font-weight:600; }

    .main-chart-wrap { flex:1; min-height:80px; position:relative; }
    .main-chart { width:100%; height:100%; }
    .scroll-latest-btn {
      position:absolute; right:10px; bottom:10px; z-index:5;
      width:30px; height:30px; border-radius:50%; border:1px solid #334155;
      background:rgba(15,23,42,.85); color:#e2e8f0; font-size:15px; line-height:1;
      cursor:pointer; display:flex; align-items:center; justify-content:center;
      opacity:.55; transition:opacity .15s;
    }
    .scroll-latest-btn:hover { opacity:1; background:#1e293b; }
    .ema-slope-badge {
      position:absolute; top:8px; right:10px; z-index:5;
      padding:3px 8px; border-radius:5px; border:1px solid #334155;
      background:rgba(15,23,42,.85); font-size:11px; font-weight:700; font-family:ui-monospace,monospace;
      pointer-events:none;
    }
    .subpane { width:100%; height:110px; border-top:1px solid #1e293b; position:relative; display:none; flex-shrink:0; }
    .subpane.visible { display:block; }
    .subpane-label { position:absolute; top:4px; left:10px; font-size:10px; color:#64748b;
                      text-transform:uppercase; letter-spacing:.05em; z-index:2; pointer-events:none; }
  </style>
</head>
<body>
  {% if not configured %}
  <div class="banner">⚠ Tastytrade isn't connected yet. <a href="/realtime/setup">Set up your credentials →</a></div>
  {% endif %}

  <div class="top-toolbar">
    <button id="addWindowBtn">+ Add Window</button>
    <span class="count" id="windowCount"></span>
    <div class="tf-group" id="layoutGroup">
      <button class="tf-btn" data-layout="1x1">1×1</button>
      <button class="tf-btn" data-layout="1x2">1×2</button>
      <button class="tf-btn" data-layout="2x2">2×2</button>
      <button class="tf-btn" data-layout="1x3">1×3</button>
      <button class="tf-btn" data-layout="2x3">2×3</button>
    </div>
    <label style="font-size:12px; color:#94a3b8; display:flex; align-items:center; gap:6px;">
      <input type="checkbox" id="syncSymbolToggle" /> Sync Symbol
    </label>
    <label style="font-size:12px; color:#94a3b8; display:flex; align-items:center; gap:6px;">
      <input type="checkbox" id="syncIntervalToggle" /> Sync Interval
    </label>
    <label style="font-size:12px; color:#94a3b8; display:flex; align-items:center; gap:6px;" title="Type an underlying's symbol into a window and the next two OTHER windows (by creation order) auto-fill with its ATM call and ATM put, nearest expiration. You can still manually retype either afterward -- this only fires once, right when the underlying's symbol changes, it doesn't keep re-enforcing itself.">
      <input type="checkbox" id="syncOptionsToggle" /> Sync Options (ATM)
    </label>
    <button id="themeToggleBtn" class="theme-toggle-btn" title="Switch chart/window background between dark and light">🌓 Theme</button>
    <label style="font-size:12px; color:#94a3b8; display:flex; align-items:center; gap:6px;">
      Refresh:
      <select id="refreshRateSelect">
        <option value="3000">3s</option>
        <option value="5000">5s</option>
        <option value="10000">10s</option>
        <option value="30000">30s</option>
        <option value="60000" selected>1m</option>
        <option value="300000">5m</option>
        <option value="0">Off</option>
      </select>
    </label>
    <button class="sidebar-toggle" id="sidebarToggleBtn">☰ Watchlist</button>
    <button class="sidebar-toggle" id="saveLayoutBtn">💾 Save Layout</button>
    <select id="loadLayoutSelect" class="sidebar-toggle" style="max-width:160px;"><option value="">Load layout…</option></select>
    <span id="layoutLoadingIndicator" style="display:none; font-size:12px; color:#f59e0b;">⏳ Loading layout…</span>
    <label style="font-size:12px; color:#f59e0b; display:flex; align-items:center; gap:6px; padding:4px 10px;
                  background:#1e293b; border:1px solid #334155; border-radius:6px;">
      <input type="checkbox" id="pauseBgToggle" /> ⚡ Pause background processes
    </label>
  </div>

  <div class="app-layout">
    <div id="grid"></div>
    <div id="watchlist-sidebar">
      <div class="wl-header">Watchlist — click a symbol to load it in the active window</div>
      <div style="padding:0 12px; font-size:11px; color:#94a3b8; text-transform:uppercase; letter-spacing:.05em; margin-top:6px;">Select watchlist</div>
      <select class="wl-select" style="border-color:#6366f1;"><option value="">All watchlists</option></select>
      <input type="text" class="wl-search" placeholder="Filter, or scanner query + Enter e.g. strongcandle(&quot;1d&quot;)" />
      <div class="wl-list"></div>
    </div>
  </div>

  <script>
    const INITIAL_SYMBOL = {{ symbol|tojson }};
    const INTERVALS = ['1m','3m','5m','15m','1h','2h','4h','1d','1w','1M'];
    const INTERVAL_LABELS = { '1m':'1m','3m':'3m','5m':'5m','15m':'15m','1h':'1H','2h':'2H','4h':'4H','1d':'D','1w':'W','1M':'M' };
    const OVERLAY_COLORS = {
      ema5: '#f59e0b', ema9: '#38bdf8', ema13: '#22d3ee', ema20: '#a78bfa', ema50: '#fb923c', ema200: '#f472b6',
      avwap_mid: '#2dd4bf', avwap_upper: 'rgba(45,212,191,0.5)', avwap_lower: 'rgba(45,212,191,0.5)',
    };
    const LAYOUTS = { '1x1': { rows: 1, cols: 1 }, '1x2': { rows: 1, cols: 2 }, '2x2': { rows: 2, cols: 2 }, '1x3': { rows: 1, cols: 3 }, '2x3': { rows: 2, cols: 3 } };

    let widgetCount = 0;
    let activeWindowId = null;
    const widgets = {};
    window.currentRefreshRateMs = 60000;  // matches the <select> default (1m)
    let currentLayoutKey = '2x2';
    let colSplit = 0.5;  // vertical divider position (fraction, 0.15-0.85), only meaningful
                          // when the current layout has exactly 2 columns (1x2, 2x2)
    let rowSplit = 0.5;  // horizontal divider position, only meaningful when exactly 2 rows
                          // (2x2, 2x3). Reset to 0.5 whenever the layout key itself changes --
                          // a custom split ratio from a different grid shape doesn't carry
                          // over meaningfully (see the layout-button click handler below).
    let chartTheme = 'dark';  // 'dark' or 'light' -- global (all windows share one theme,
                               // matching how Sync Symbol/Interval/Options are also global
                               // rather than per-window). Scoped to chart backgrounds/grid/
                               // text and the window chrome (toolbar/card backgrounds) --
                               // not an exhaustive re-theme of every popup/button in the page.
    // Every fetch() to /realtime/api/<symbol>/... builds the URL as a
    // literal template-string path segment -- fine for a normal ticker,
    // but futures symbols always start with "/" (e.g. "/MGCV6"), which
    // collides with the URL's own path structure. Confirmed directly
    // against real Flask/Werkzeug routing (not assumed): the resulting
    // "/api//MGCV6/bars" gets silently 308-redirected to
    // "/api/MGCV6/bars" -- the view function DOES run, just with the
    // WRONG symbol (leading "/" stripped), which is also why searching
    // server logs for "/MGCV6" found nothing: the log entries exist,
    // just under "MGCV6". Percent-encoding the slash (%2FMGCV6) does
    // NOT fix this either -- Werkzeug decodes %2F and merges slashes
    // before route matching runs, so the identical redirect happens
    // either way. The actual fix: substitute the leading "/" with "~"
    // before it ever reaches fetch() -- "~" is an ordinary path
    // character with zero special meaning to Werkzeug's routing, so it
    // round-trips with no redirect at all. _decode_path_symbol() on the
    // backend (realtime_dashboard.py) is the other half of this pair,
    // translating "~MGCV6" back to "/MGCV6" before the symbol is used
    // for anything.
    function encodeSymbolForUrl(sym) {
      return (sym && sym.startsWith('/')) ? ('~' + sym.slice(1)) : sym;
    }

    function chartThemeColors(theme) {
      return theme === 'light'
        ? { bg: '#ffffff', text: '#1e293b', grid: '#e5e7eb' }
        : { bg: '#0b1220', text: '#e2e8f0', grid: '#1e293b' };
    }
    function applyPageTheme(theme) {
      document.body.classList.toggle('light-theme', theme === 'light');
    }
    let syncSymbol = false;
    let syncInterval = false;
    let syncOptions = false;  // when true, setting a window's symbol to a non-option underlying
                               // auto-fills the next two OTHER windows with its ATM call/put
                               // (see maybeAutoFillAtmOptions() near userSetSymbol below)
    let suppressSave = false;  // true while restoring a saved layout, so restoring doesn't immediately re-save

    function currentMaxWindows() {
      const l = LAYOUTS[currentLayoutKey];
      return l.rows * l.cols;
    }

    // ---------- Sync Options (ATM call/put auto-fill) ----------
    // Fired from userSetSymbol() below whenever Sync Options is checked
    // and the symbol just typed looks like a plain underlying (not
    // itself already an option/future/crypto symbol -- see the guard
    // regex). Picks the next two OTHER windows by creation order
    // (Object.keys() on a plain object preserves insertion order for
    // non-numeric-looking string keys, which these widget ids are) and
    // fills them with the ATM call and ATM put for the nearest
    // expiration, using tastytrade's own ready-made streamer symbols
    // from get_nested_option_chain() -- nothing hand-built here either.
    // Fires ONCE per symbol change, not a standing lock: the two target
    // windows can still be freely retyped by hand afterward, same as any
    // other window.
    // sourceTimeframe: passed through from the calling window's own
    // `timeframe` closure variable (see userSetSymbol below) so that,
    // when Sync Interval is ALSO checked, the two auto-filled option
    // windows adopt the source window's CURRENT interval too -- not just
    // its symbol. Previously setSymbol() alone left each target window
    // on whatever interval it already happened to be on, which is why
    // "it pulled the 2 option contracts but the intervals were not in
    // sync" -- Sync Options and Sync Interval are separate toggles and
    // this path wasn't consulting the latter at all before.
    async function maybeAutoFillAtmOptions(sourceId, underlyingSymbol, sourceTimeframe) {
      if (!syncOptions) return;
      const sym = String(underlyingSymbol || '').trim().toUpperCase();
      // Skip if what was typed is itself already an option/future
      // streamer symbol (starts with '.' or '/') or a crypto pair
      // (contains '/' anywhere) -- auto-filling "ATM options on an
      // option" doesn't make sense.
      if (!sym || sym.startsWith('.') || sym.startsWith('/') || sym.includes('/')) return;

      const ids = Object.keys(widgets);
      const others = ids.filter((wId) => wId !== sourceId);
      if (others.length < 2) return;  // need at least 2 OTHER windows to fill
      const [callTargetId, putTargetId] = others;

      try {
        const priceResp = await fetch(`/realtime/api/${encodeSymbolForUrl(sym)}/price`);
        const priceData = await priceResp.json();
        if (priceData.status !== 'live' || priceData.price == null) return;
        const spot = priceData.price;

        const chainResp = await fetch(`/realtime/api/${encodeSymbolForUrl(sym)}/option-chain`);
        const chain = await chainResp.json();
        if (chain.error || !chain.expirations || !chain.expirations.length) return;
        const nearestExp = chain.expirations[0];  // backend already sorts ascending by date
        if (!nearestExp.strikes || !nearestExp.strikes.length) return;

        let best = nearestExp.strikes[0];
        let bestDiff = Math.abs(best.strike - spot);
        for (const s of nearestExp.strikes) {
          const diff = Math.abs(s.strike - spot);
          if (diff < bestDiff) { best = s; bestDiff = diff; }
        }

        if (widgets[callTargetId] && best.call_streamer_symbol) {
          widgets[callTargetId].setSymbol(best.call_streamer_symbol);
          if (syncInterval && sourceTimeframe) widgets[callTargetId].setTimeframe(sourceTimeframe);
        }
        if (widgets[putTargetId] && best.put_streamer_symbol) {
          widgets[putTargetId].setSymbol(best.put_streamer_symbol);
          if (syncInterval && sourceTimeframe) widgets[putTargetId].setTimeframe(sourceTimeframe);
        }
      } catch (e) { console.warn('Sync Options ATM auto-fill failed', e); }
    }

    function setActiveWindow(id) {
      activeWindowId = id;
      document.querySelectorAll('.window').forEach((el) => {
        el.classList.toggle('active', el.id === id);
      });
    }

    // Builds a grid-template-columns/rows value plus the 1-based grid
    // line each window-slot along this axis should sit at. For exactly
    // 2 tracks, inserts a 6px divider track between them (draggable --
    // see wireDividerDrag below) so the split ratio can be adjusted
    // instead of being locked to an even 1fr/1fr forever. For 1 or 3+
    // tracks, falls back to a plain equal split with no divider --
    // genuine per-track dragging for 3-way splits (1x3, 2x3's columns)
    // is a bigger jump in complexity and isn't included in this pass.
    function buildTrackTemplate(count, splitFraction) {
      if (count === 2) {
        const s = Math.min(0.85, Math.max(0.15, splitFraction || 0.5));
        return { template: `${s}fr 6px ${(1 - s)}fr`, hasDivider: true, positions: [1, 3] };
      }
      return { template: `repeat(${count}, 1fr)`, hasDivider: false, positions: Array.from({ length: count }, (_, i) => i + 1) };
    }

    function wireDividerDrag(handle, axis) {
      // axis: 'col' (drag left/right, adjusts colSplit) or 'row' (drag
      // up/down, adjusts rowSplit). Same mousedown/mousemove/mouseup
      // pattern already proven for per-pane resize handles elsewhere in
      // this file, just measuring against the #grid container instead
      // of a single pane.
      let startPos = 0, startSplit = 0.5;
      const onMove = (e) => {
        const grid = document.getElementById('grid');
        const rect = grid.getBoundingClientRect();
        if (axis === 'col') {
          const dx = e.clientX - startPos;
          colSplit = Math.min(0.85, Math.max(0.15, startSplit + dx / Math.max(1, rect.width)));
        } else {
          const dy = e.clientY - startPos;
          rowSplit = Math.min(0.85, Math.max(0.15, startSplit + dy / Math.max(1, rect.height)));
        }
        applyLayoutGrid();
      };
      const onUp = () => {
        handle.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        // Track sizes just changed for every window sharing that
        // row/column, not just one -- resize them all, same
        // post-reflow-frame pattern as updateGridLayout().
        requestAnimationFrame(() => {
          Object.values(widgets).forEach((w) => { try { w.resize(); } catch (e) {} });
        });
        saveLayout();
      };
      handle.addEventListener('mousedown', (e) => {
        handle.classList.add('dragging');
        startPos = axis === 'col' ? e.clientX : e.clientY;
        startSplit = axis === 'col' ? colSplit : rowSplit;
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
        e.preventDefault();
      });
    }

    function updateGridDividers(colInfo, rowInfo) {
      const grid = document.getElementById('grid');
      let colDivider = document.getElementById('colDivider');
      if (colInfo.hasDivider) {
        if (!colDivider) {
          colDivider = document.createElement('div');
          colDivider.id = 'colDivider';
          colDivider.className = 'grid-divider grid-divider-vertical';
          grid.appendChild(colDivider);
          wireDividerDrag(colDivider, 'col');
        }
        colDivider.style.display = 'block';
        colDivider.style.gridColumn = '2';
        colDivider.style.gridRow = '1 / -1';
      } else if (colDivider) {
        colDivider.style.display = 'none';
      }

      let rowDivider = document.getElementById('rowDivider');
      if (rowInfo.hasDivider) {
        if (!rowDivider) {
          rowDivider = document.createElement('div');
          rowDivider.id = 'rowDivider';
          rowDivider.className = 'grid-divider grid-divider-horizontal';
          grid.appendChild(rowDivider);
          wireDividerDrag(rowDivider, 'row');
        }
        rowDivider.style.display = 'block';
        rowDivider.style.gridRow = '2';
        rowDivider.style.gridColumn = '1 / -1';
      } else if (rowDivider) {
        rowDivider.style.display = 'none';
      }
    }

    let windowOrder = [];  // array of window IDs (or null for an empty
                            // slot), length kept in sync with rows*cols
                            // for the CURRENT layout -- drives grid
                            // placement instead of raw creation order, so
                            // a window can be moved to any slot (e.g.
                            // "call and put stacked on the right, symbol
                            // alone on the left" in a 2x2). Session-only,
                            // not persisted across a page reload: window
                            // IDs (w1, w2, ...) are regenerated fresh
                            // every load, so a saved order from a
                            // previous session wouldn't map onto anything
                            // meaningful after reload -- a real, disclosed
                            // limitation, not an oversight.
    function ensureWindowOrderLength() {
      const l = LAYOUTS[currentLayoutKey];
      const total = l.rows * l.cols;
      while (windowOrder.length < total) windowOrder.push(null);
      windowOrder = windowOrder.slice(0, total);
      // Drop any stale IDs (window since closed)
      windowOrder = windowOrder.map((id) => (id && widgets[id]) ? id : null);
      // Any window not yet placed anywhere goes into the first empty
      // slot. Deliberately does NOT fall back to push()-ing past `total`
      // when there's no empty slot -- switching to a SMALLER layout
      // (e.g. 2x2 -> 1x2) while more windows are open than the new
      // layout has room for is a real, reachable case (the layout
      // switch handler doesn't auto-close excess windows, a pre-existing
      // characteristic, not something this introduced): the slice()
      // above already dropped the excess window from windowOrder, and
      // pushing it back on here would silently grow the array past
      // `total` again, corrupting the row/col math in applyLayoutGrid's
      // placement loop for every window after it. Left genuinely
      // unplaced instead -- it keeps whatever grid position it last had
      // rather than the array's length invariant breaking.
      Object.keys(widgets).forEach((id) => {
        if (!windowOrder.includes(id)) {
          const emptyIdx = windowOrder.indexOf(null);
          if (emptyIdx !== -1) windowOrder[emptyIdx] = id;
        }
      });
    }
    function moveWindowToSlot(windowId, targetSlotIdx) {
      ensureWindowOrderLength();
      const currentIdx = windowOrder.indexOf(windowId);
      if (currentIdx === -1 || targetSlotIdx < 0 || targetSlotIdx >= windowOrder.length) return;
      const swapped = windowOrder[targetSlotIdx];
      windowOrder[targetSlotIdx] = windowId;
      windowOrder[currentIdx] = swapped;  // whatever was in the target slot takes this window's old slot
      applyLayoutGrid();
      saveLayout();
    }

    function applyLayoutGrid() {
      const l = LAYOUTS[currentLayoutKey];
      const grid = document.getElementById('grid');
      const colInfo = buildTrackTemplate(l.cols, colSplit);
      const rowInfo = buildTrackTemplate(l.rows, rowSplit);
      grid.style.gridTemplateColumns = colInfo.template;
      grid.style.gridTemplateRows = rowInfo.template;

      // Placement now driven by windowOrder (user-adjustable via each
      // window's "Move to" control), not raw creation order -- still
      // row-major (slot 0 = top-left, filling left-to-right then down),
      // just with slot assignment overridable per window instead of
      // fixed to whenever it happened to be added.
      ensureWindowOrderLength();
      windowOrder.forEach((wid, i) => {
        if (!wid) return;  // empty slot, nothing to place
        const row = Math.floor(i / l.cols);
        const col = i % l.cols;
        const el = document.getElementById(wid);
        if (el) {
          el.style.gridColumn = String(colInfo.positions[col] || 1);
          el.style.gridRow = String(rowInfo.positions[row] || 1);
        }
      });

      document.querySelectorAll('[data-layout]').forEach((b) => {
        b.classList.toggle('active', b.dataset.layout === currentLayoutKey);
      });

      updateGridDividers(colInfo, rowInfo);
      updateMoveToSlotOptions();
    }

    // Populates every window's "Move to" dropdown with one option per
    // grid slot in the CURRENT layout (labeled by row/col position, e.g.
    // "Row 1, Col 2"), selecting whichever slot that window currently
    // occupies. Re-run on every applyLayoutGrid() call since the slot
    // count changes with the layout (1x2 has 2 slots, 2x3 has 6, etc).
    function updateMoveToSlotOptions() {
      const l = LAYOUTS[currentLayoutKey];
      const total = l.rows * l.cols;
      const labels = [];
      for (let i = 0; i < total; i++) {
        const row = Math.floor(i / l.cols) + 1;
        const col = (i % l.cols) + 1;
        labels.push(l.rows > 1 ? `Row ${row}, Col ${col}` : `Position ${col}`);
      }
      windowOrder.forEach((wid, slotIdx) => {
        if (!wid) return;
        const sel = document.querySelector(`#${wid} .moveToSlotSelect`);
        if (!sel) return;
        sel.innerHTML = labels.map((lbl, i) => `<option value="${i}">${lbl}</option>`).join('');
        sel.value = String(slotIdx);
      });
    }

    function updateGridLayout() {
      const n = Object.keys(widgets).length;
      const max = currentMaxWindows();
      document.getElementById('windowCount').textContent = `${n} / ${max} windows`;
      document.getElementById('addWindowBtn').disabled = n >= max;
      applyLayoutGrid();
      // Grid template just changed (e.g. 1x2 -> 2x2), which changes every
      // window's actual pixel height -- but clientHeight won't reflect
      // that until the browser finishes reflowing, so wait one frame
      // before asking each chart to resize itself to the new space.
      requestAnimationFrame(() => {
        Object.values(widgets).forEach((w) => { try { w.resize(); } catch (e) {} });
      });
    }

    function addWindow(initialSymbol, initialTimeframe) {
      const n = Object.keys(widgets).length;
      if (n >= currentMaxWindows()) return null;
      widgetCount += 1;
      const id = 'w' + widgetCount;
      const grid = document.getElementById('grid');
      const root = document.createElement('div');
      root.className = 'window';
      root.id = id;
      root.innerHTML = windowTemplate(id);
      root.addEventListener('mousedown', () => setActiveWindow(id));
      grid.appendChild(root);
      widgets[id] = createWidget(root, id, initialSymbol || 'SPY', initialTimeframe || '1m');
      setActiveWindow(id);
      updateGridLayout();
      saveLayout();
      return id;
    }

    function removeWindow(id) {
      const w = widgets[id];
      if (!w) return;
      w.destroy();
      const el = document.getElementById(id);
      if (el) el.remove();
      delete widgets[id];
      if (activeWindowId === id) {
        const remaining = Object.keys(widgets);
        if (remaining.length) setActiveWindow(remaining[0]);
        else activeWindowId = null;
      }
      updateGridLayout();
      saveLayout();
    }

    // ---------- Layout persistence ----------
    // Two layers sharing the same underlying shape:
    //   - /last_layout: single auto-saved slot, restored automatically on page load
    //   - /layouts/<name>: named, multiple, explicit "Save Layout" / "Load Layout" picks
    function collectLayoutState() {
      const windowsState = Object.entries(widgets).map(([id, w]) => w.getState());
      return { layoutKey: currentLayoutKey, syncSymbol, syncInterval, syncOptions, colSplit, rowSplit, chartTheme, windows: windowsState };
    }

    function clearAllWindows() {
      Object.keys(widgets).forEach((id) => removeWindow(id));
    }

    function setLayoutLoading(loading) {
      document.getElementById('layoutLoadingIndicator').style.display = loading ? 'inline' : 'none';
    }

    function applyLayoutState(layout) {
      if (!layout || !layout.windows || !layout.windows.length) return;
      setLayoutLoading(true);
      suppressSave = true;
      clearAllWindows();
      currentLayoutKey = LAYOUTS[layout.layoutKey] ? layout.layoutKey : '2x2';
      syncSymbol = !!layout.syncSymbol;
      syncInterval = !!layout.syncInterval;
      syncOptions = !!layout.syncOptions;
      colSplit = (typeof layout.colSplit === 'number') ? layout.colSplit : 0.5;
      rowSplit = (typeof layout.rowSplit === 'number') ? layout.rowSplit : 0.5;
      chartTheme = layout.chartTheme === 'light' ? 'light' : 'dark';
      applyPageTheme(chartTheme);  // each window picks up the current chartTheme automatically
                                     // at creation via chartOpts below -- no per-widget call needed
                                     // here since none exist yet (addWindow runs next, per-window)
      document.getElementById('syncSymbolToggle').checked = syncSymbol;
      document.getElementById('syncIntervalToggle').checked = syncInterval;
      document.getElementById('syncOptionsToggle').checked = syncOptions;
      layout.windows.forEach((ws) => {
        const id = addWindow(ws.symbol || 'SPY', ws.timeframe || '1m');
        if (id && widgets[id] && ws.templateConfig) {
          widgets[id].applyTemplateConfig(ws.templateConfig);
        }
      });
      suppressSave = false;
      saveLayout();  // persist the now-active state as the "last layout" too
      setLayoutLoading(false);
    }

    let saveLayoutTimer = null;
    function saveLayout() {
      if (suppressSave) return;
      clearTimeout(saveLayoutTimer);
      saveLayoutTimer = setTimeout(async () => {
        try {
          await fetch('/realtime/last_layout', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(collectLayoutState()),
          });
        } catch (e) { /* non-fatal -- layout just won't restore next time */ }
      }, 800);
    }

    async function restoreLayout() {
      setLayoutLoading(true);
      try {
        const r = await fetch('/realtime/last_layout');
        const d = await r.json();
        if (!d.ok || !d.layout || !d.layout.windows || !d.layout.windows.length) {
          addWindow(INITIAL_SYMBOL, '1m');
          return;
        }
        suppressSave = true;
        currentLayoutKey = LAYOUTS[d.layout.layoutKey] ? d.layout.layoutKey : '2x2';
        syncSymbol = !!d.layout.syncSymbol;
        syncInterval = !!d.layout.syncInterval;
        syncOptions = !!d.layout.syncOptions;
        colSplit = (typeof d.layout.colSplit === 'number') ? d.layout.colSplit : 0.5;
        rowSplit = (typeof d.layout.rowSplit === 'number') ? d.layout.rowSplit : 0.5;
        chartTheme = d.layout.chartTheme === 'light' ? 'light' : 'dark';
        applyPageTheme(chartTheme);
        document.getElementById('syncSymbolToggle').checked = syncSymbol;
        document.getElementById('syncIntervalToggle').checked = syncInterval;
        document.getElementById('syncOptionsToggle').checked = syncOptions;
        d.layout.windows.forEach((ws) => {
          const id = addWindow(ws.symbol || 'SPY', ws.timeframe || '1m');
          if (id && widgets[id] && ws.templateConfig) {
            widgets[id].applyTemplateConfig(ws.templateConfig);
          }
        });
        suppressSave = false;
      } catch (e) {
        addWindow(INITIAL_SYMBOL, '1m');
      } finally {
        setLayoutLoading(false);
      }
    }

    async function loadNamedLayoutsDropdown() {
      const sel = document.getElementById('loadLayoutSelect');
      try {
        const r = await fetch('/realtime/layouts');
        const d = await r.json();
        sel.innerHTML = '<option value="">Load layout…</option>' +
          (d.layouts || []).map(n => `<option value="${n}">${n}</option>`).join('');
      } catch (e) { /* leave default option */ }
    }

    async function saveNamedLayout() {
      const name = prompt('Save the entire current layout (all windows, symbols, intervals, indicators) as:');
      if (!name) return;
      await fetch('/realtime/layouts', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, layout: collectLayoutState() }),
      });
      loadNamedLayoutsDropdown();
    }

    async function loadNamedLayout(name) {
      if (!name) return;
      setLayoutLoading(true);
      try {
        const r = await fetch(`/realtime/layouts/${encodeURIComponent(name)}`);
        const d = await r.json();
        if (d.ok) applyLayoutState(d.layout);
      } finally {
        setLayoutLoading(false);
      }
    }

    function windowTemplate(id) {
      const intervalButtons = INTERVALS.map(iv =>
        `<button data-tf="${iv}">${INTERVAL_LABELS[iv]}</button>`
      ).join('');
      return `
        <div class="win-toolbar">
          <span class="sym-label symLabel"></span>
          <span class="loading-indicator" style="display:none; font-size:11px; color:#f59e0b;">⏳ Loading…</span>
          <input type="text" class="symInput" value="" placeholder="SYMBOL" />
          <button class="small goBtn">Go</button>
          <div class="interval-row">${intervalButtons}</div>
          <select class="templateSelect"><option value="">Load template…</option></select>
          <button class="small saveTemplateBtn">Save as…</button>
          <div class="ind-popover">
            <button class="small indBtn">⚙ Indicators</button>
            <div class="ind-popover-panel">
              <div class="grp-label">Overlays</div>
              <label><input type="checkbox" data-ov="ema5" checked> EMA5</label>
              <label><input type="checkbox" data-ov="ema9" checked> EMA9</label>
              <label><input type="checkbox" data-ov="ema13"> EMA13</label>
              <label><input type="checkbox" data-ov="ema20" checked> EMA20</label>
              <label><input type="checkbox" data-ov="ema50"> EMA50</label>
              <label><input type="checkbox" data-ov="ema200"> EMA200</label>
              <label><input type="checkbox" data-ov="avwap" checked> AVWAP</label>
              <label><input type="checkbox" data-ov="sr" checked> Support/Resistance</label>
              <label><input type="checkbox" data-ov="vp" checked> Volume Profile POC</label>
              <label style="padding-left:18px; display:flex; align-items:center; gap:6px;">
                Count: <input type="number" class="srCountInput" value="3" min="1" max="10" style="width:44px" />
              </label>
              <div class="grp-label">Corner Badges</div>
              <label title="Slope of the EMA13-EMA50 spread, last 10 bars, degrees(atan(...)) -- same convention as SlopeDeg() in the scanner engine. Fetches EMA13/EMA50 data as needed even if those overlay lines themselves aren't checked.">
                <input type="checkbox" data-badge="ema-slope"> EMA13/50 Slope (10-bar)
              </label>
              <div class="grp-label">Panes</div>
              <label><input type="checkbox" data-pane="rsi" checked> RSI/RSIDiff90</label>
              <div style="padding-left:18px; display:flex; flex-direction:column; gap:2px; margin-bottom:4px;">
                <label style="font-size:11px"><input type="checkbox" data-rsisub="rsi3" checked> RSI3</label>
                <label style="font-size:11px"><input type="checkbox" data-rsisub="rsi14" checked> RSI14</label>
                <label style="font-size:11px"><input type="checkbox" data-rsisub="rsi_ema13"> RSI-EMA13</label>
                <label style="font-size:11px"><input type="checkbox" data-rsisub="rsi_ema90"> RSI-EMA90</label>
                <label style="font-size:11px"><input type="checkbox" data-rsisub="rsi_diff_90" checked> RSIDiff90</label>
              </div>
              <label><input type="checkbox" data-pane="macd" checked> MACD</label>
              <label><input type="checkbox" data-pane="adx"> ADX</label>
              <label><input type="checkbox" data-pane="volquant" checked> VolQuant</label>
              <div class="grp-label">Session</div>
              <label><input type="checkbox" class="extendedHoursCb" checked> Extended Hours (pre/post-market)</label>
            </div>
          </div>
          <button class="small replayBtn" title="Pick a candle on the chart and step through history one bar at a time. Pauses this window's live updates until stopped.">▶ Replay</button>
          <button class="small rvpBtn" title="Click two candles to select a range, then see the volume profile for that range -- major volume nodes as bar length, colored by which side (buy/sell) dominated each level.">📊 Volume Profile</button>
          <button class="small rvpClearBtn" style="display:none">✕ Clear Profile</button>
          <button class="small cvdBtn" title="Click two candles to select a range, then see net delta (CVD) at each price level -- bar length shows how imbalanced that level was, color shows which way, independent of how much total volume traded there.">📈 CVD Profile</button>
          <button class="small cvdClearBtn" style="display:none">✕ Clear CVD</button>
          <button class="small bubbleBtn" title="Live large-trade bubbles: size-scaled, green=buy-initiated / red=sell-initiated. Filtered to trades at or above the threshold below. Requires tastytrade Trade-level DXLink data.">🫧 Bubbles</button>
          <input type="number" class="bubbleThreshold" value="10" min="1" step="1" style="width:56px;font-size:11px;padding:2px 4px" title="Minimum trade size to show as a bubble (contracts/shares)"/>
          <select class="moveToSlotSelect" title="Move this window to a different position in the grid"></select>
          <button class="close-btn" title="Close window">✕</button>
        </div>
        <div class="fetch-error-banner" style="display:none;"></div>
        <div class="bubble-status-banner" style="display:none;font-size:11px;padding:4px 12px;color:var(--muted,#94a3b8)"></div>
        <div class="replay-controls" style="display:none;">
          <button class="small replayStepBtn" title="Reveal one more bar">⏭ Step</button>
          <button class="small replayPlayBtn" title="Auto-step until stopped">▶ Play</button>
          <select class="replaySpeedSelect" title="Auto-play speed">
            <option value="1500">Slow</option>
            <option value="700" selected>Normal</option>
            <option value="250">Fast</option>
          </select>
          <span class="replay-position" style="font-size:11px;color:var(--muted,#94a3b8)"></span>
          <button class="small replayStopBtn" title="Exit replay, restore live data" style="margin-left:auto">⏹ Stop Replay</button>
        </div>
        <div class="main-chart-wrap" style="position:relative">
          <div class="main-chart"></div>
          <canvas class="rvp-canvas" style="position:absolute;top:0;left:0;pointer-events:none;z-index:5"></canvas>
          <canvas class="cvd-canvas" style="position:absolute;top:0;left:0;pointer-events:none;z-index:5"></canvas>
          <canvas class="bubble-canvas" style="position:absolute;top:0;left:0;pointer-events:none;z-index:6"></canvas>
          <div class="chart-legend-badge" style="position:absolute;top:6px;left:8px;z-index:7;pointer-events:none;font-family:monospace;font-size:11px;line-height:1.5;color:#e2e8f0;background:rgba(15,23,42,.55);padding:3px 8px;border-radius:5px;display:none;white-space:nowrap"></div>
          <button class="scroll-latest-btn" title="Jump to latest price">→</button>
          <div class="ema-slope-badge" style="display:none" title="Slope of the EMA13-EMA50 spread over the last 10 bars, same degrees(atan(...)) convention as SlopeDeg() in the scanner engine -- raw price-unit based, not percent-normalized (percent-normalizing a spread that can cross zero would be numerically unstable near a crossover)."></div>
          <!-- All picking banners live HERE, as absolute overlays, not
               as normal flex-flow siblings above main-chart-wrap. That
               positioning was the actual bug: a normal-flow element
               appearing/disappearing changes how much vertical space
               main-chart-wrap measures as available, which triggers the
               ResizeObserver -> resizeAll() -> pane-height-fraction
               recompute -- visibly resizing/repositioning the chart
               purely because a banner popped in, and very plausibly
               invalidating the timeToCoordinate()/priceToCoordinate()
               lookups the profile-drawing functions depend on at the
               exact moment picking finishes. Absolute overlays can
               never affect layout flow, which removes the mechanism
               entirely rather than trying to patch around its symptoms.
               The CVD banner is built as an overlay from the start,
               not retrofitted, for exactly this reason. -->
          <div class="replay-pick-banner" style="display:none;">
            🎯 Click a candle to start replay from there &nbsp;
            <button class="small replayPickCancel">Cancel</button>
          </div>
          <div class="rvp-pick-banner" style="display:none;">
            📊 <span class="rvp-pick-msg">Click the START of the range</span> &nbsp;
            <button class="small rvpPickCancel">Cancel</button>
          </div>
          <div class="cvd-pick-banner" style="display:none;">
            📈 <span class="cvd-pick-msg">Click the START of the range</span> &nbsp;
            <button class="small cvdPickCancel">Cancel</button>
          </div>
        </div>
        <div class="resize-handle" data-resize-target="main" style="display:none" title="Main chart height follows the window automatically now"></div>
        <div class="subpane" data-pane-el="rsi"><div class="subpane-label">RSI(14) &amp; RSIDiff90</div></div>
        <div class="resize-handle" data-resize-target="rsi"></div>
        <div class="subpane" data-pane-el="macd"><div class="subpane-label">MACD (12,26,9)</div></div>
        <div class="resize-handle" data-resize-target="macd"></div>
        <div class="subpane" data-pane-el="adx"><div class="subpane-label">ADX(14)</div></div>
        <div class="resize-handle" data-resize-target="adx"></div>
        <div class="subpane" data-pane-el="volquant"><div class="subpane-label">VolQuant</div></div>
        <div class="resize-handle" data-resize-target="volquant"></div>
      `;
    }

    function createWidget(root, id, initialSymbol, initialTimeframe) {
      let symbol = initialSymbol;
      let extendedHours = true;
      let timeframe = initialTimeframe;
      let srLines = [];
      let vpLines = [];
      let lastIndicatorsData = null;
      // Replay state -- 'off' | 'picking' | 'active'. Per-window (each
      // window's own closure), so replaying one window doesn't affect
      // any other. replayIndex is a position into chartData/lastIndicatorsData
      // (the already chart-time-mapped, already-fetched full dataset --
      // replay doesn't re-fetch anything, it slices what's already loaded).
      let replayMode = 'off';
      let replayIndex = 0;
      let replayPlayTimer = null;
      let currentRefreshMs = window.currentRefreshRateMs;  // tracked so replay can
                                                              // restore the exact rate
                                                              // it paused, not a guess
      // Range Volume Profile state -- 'off' | 'picking-start' | 'picking-end' | 'active'.
      // Two-click selection (same interaction model TradingView's own
      // Fixed Range Volume Profile tool uses), computed entirely client-side
      // from currentBars (already loaded, no server round-trip needed) and
      // redrawn on pan/zoom/resize since it's a plain canvas overlay, not a
      // chart-native primitive.
      let rvpMode = 'off';
      let rvpStartIdx = null;
      let rvpEndIdx = null;
      let rvpData = null;   // computed profile, see computeRangeVolumeProfile()

      // CVD Profile -- fully independent tool, its own state, own canvas,
      // own picking flow. Deliberately not sharing rvp's state at all,
      // even though both reuse the same underlying computeRangeVolumeProfile()
      // -- keeping the two tools' state independent means a bug in one
      // can't touch the other, and each is simple enough to verify alone.
      let cvdMode = 'off';
      let cvdStartIdx = null;
      let cvdEndIdx = null;
      let cvdData = null;
      const overlaySeries = {};

      // Converts a time value actually plotted on this chart back to a
      // human-readable real date/time, for tick labels and the crosshair
      // tooltip. Used by both timeScale.tickMarkFormatter and
      // localization.timeFormatter below -- defined once here rather
      // than inline so both stay consistent. References chartBucketSeconds/
      // chartTimeBase/currentBars/chartTimeToIndex which are declared
      // further down in this same function scope; safe because this is
      // only ever CALLED by the charting library well after the rest of
      // widget setup has run (same hoisting reasoning as the click
      // handler above), not evaluated at chartOpts-creation time.
      // Converts a time value actually plotted on this chart back to the
      // real unix timestamp it represents -- needed wherever gap
      // compression is in play (chartBucketSeconds set), since the
      // plotted time is a synthetic sequential value, not the real bar
      // time. Split out from formatRealTime so the crosshair legend
      // below can build a full date+time readout from the same
      // resolution logic instead of formatRealTime's display-only
      // (time-only OR date-only, never both) string.
      function realTimeFor(chartTime) {
        let realTime = chartTime;
        if (chartBucketSeconds) {
          const idx = chartTimeToIndex(chartTime);
          if (idx != null && idx >= 0 && idx < currentBars.length) {
            realTime = currentBars[idx].time;
          } else if (idx != null && idx >= currentBars.length) {
            realTime = Math.floor(Date.now() / 1000);
          }
        }
        return realTime;
      }
      function formatRealTime(chartTime, tickMarkType) {
        const d = new Date(realTimeFor(chartTime) * 1000);
        if (chartBucketSeconds) {
          return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        // Previously never showed the year at all on Daily/Weekly/Monthly
        // ticks -- "Jul 7" ... "Oct 22" with no year anywhere on the axis
        // is genuinely ambiguous (could be any year, or even span more
        // than one if something upstream ever mixed date ranges), which
        // is exactly the kind of thing that makes a chart's own data look
        // untrustworthy at a glance even when it's correct. Uses LWC's
        // own tick-boundary detection (tickMarkType) to show the year
        // right where it actually changes -- same convention
        // TradingView's native axis uses -- rather than cluttering every
        // single tick with it.
        const isYearBoundary = tickMarkType != null && LightweightCharts.TickMarkType && tickMarkType === LightweightCharts.TickMarkType.Year;
        return isYearBoundary
          ? d.toLocaleDateString([], { year: 'numeric' })
          : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
      }

      const chartOpts = {
        layout: { background: { color: chartThemeColors(chartTheme).bg }, textColor: chartThemeColors(chartTheme).text },
        grid: { vertLines: { color: chartThemeColors(chartTheme).grid }, horzLines: { color: chartThemeColors(chartTheme).grid } },
        timeScale: { timeVisible: true, secondsVisible: false, tickMarkFormatter: (t, tickMarkType) => formatRealTime(t, tickMarkType) },
        localization: { timeFormatter: (t) => formatRealTime(t) },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        // Native auto-fit (internally managed ResizeObserver, continuously
        // keeps the chart's canvas matched to its container's actual
        // rendered size) -- this is the real fix for "chart doesn't fill
        // the window, sometimes goes extreme left, sometimes isn't even
        // visible." Previously the chart was created with only a fixed
        // height and NO width at all, meaning it snapshotted whatever
        // clientWidth the container happened to have at the EXACT
        // instant createChart() ran -- if the container wasn't fully
        // laid out yet (very plausible right as a window is added or
        // data is still loading), that snapshot could be 0 or wrong,
        // and without autoSize nothing would ever self-correct until an
        // unrelated resize event happened to trigger the old manual
        // resizeAll() path. autoSize applies to every chart created with
        // these options -- main chart AND all 4 indicator subpanes.
        autoSize: true,
      };

      const mainEl = root.querySelector('.main-chart');
      const mainChart = LightweightCharts.createChart(mainEl, chartOpts);

      // ResizeObserver, not manually-placed resize() calls at specific
      // trigger points: this is the actual fix for "chart doesn't fill
      // the window" showing up inconsistently (worked after some
      // actions, not others). Every prior fix (updateGridLayout,
      // applyPaneVisibility, the divider drag handlers) manually called
      // resize() at the specific moments EACH of them individually knew
      // the layout had changed -- but that's an open-ended list, and
      // missing even one path (e.g. a 3rd/4th window being added while
      // others already existed, or some other layout interaction) means
      // the affected chart's underlying <canvas> silently stops tracking
      // its container's actual size, even though the container ITSELF
      // (.main-chart-wrap, flex:1) is correctly stretching via CSS --
      // exactly the "gap below the chart, no way to make it bigger"
      // symptom. ResizeObserver instead fires automatically whenever the
      // observed element's rendered size ACTUALLY changes, for ANY
      // reason, eliminating the whole category of "did I remember to
      // call resize here too" bugs at the source. The existing manual
      // resizeAll() calls elsewhere are left in place (harmless,
      // slightly redundant) rather than removed, since some of them run
      // before layout/reflow settles and this observer's callback only
      // fires once the browser has actually finished resizing the
      // element -- belt and suspenders, not a replacement.
      const mainChartWrap = root.querySelector('.main-chart-wrap');
      if (mainChartWrap && typeof ResizeObserver !== 'undefined') {
        new ResizeObserver(() => resizeAll()).observe(mainChartWrap);
      }
      // Redundant, more direct trigger alongside the ResizeObserver above:
      // this page runs inside an <iframe style="height:90vh"> embedded in
      // index.html's tab system, so "resize the window" for anyone using
      // it that way means resizing the OUTER app window/container, one
      // layer removed from this iframe's own DOM. The iframe's internal
      // `window` still fires a real, standard resize event when its
      // rendered size changes for any reason (including the outer 90vh
      // recalculating) -- listening for it directly here doesn't depend
      // on ResizeObserver correctly noticing the resulting internal
      // reflow, it responds to the resize itself.
      window.addEventListener('resize', () => resizeAll());
      // Strong-candle 3-tier coloring: tier 0 (normal) uses the base
      // up/down colors set here; tiers 1/2 override per-bar via the
      // `color`/`wickColor`/`borderColor` fields Lightweight Charts'
      // candlestick series data points support directly (see
      // applyStrengthColors() in loadBars() below). Tier boundaries and
      // the underlying ratio computation come from the backend's /bars
      // route, using the exact same formula as StrongBullCandle/
      // StrongBearCandle in scanner_builder.py.
      const CANDLE_TIER_COLORS = {
        bull: ['#93c5fd', '#3b82f6', '#1e3a8a'],  // tier 0, 1, 2 -- light to dark blue
        bear: ['#fca5a5', '#ef4444', '#7f1d1d'],  // tier 0, 1, 2 -- light to dark red
      };
      function applyStrengthColors(bar) {
        const tier = Math.max(0, Math.min(2, bar.strength_tier || 0));
        const isBull = bar.close >= bar.open;
        const color = isBull ? CANDLE_TIER_COLORS.bull[tier] : CANDLE_TIER_COLORS.bear[tier];
        return { ...bar, color, wickColor: color, borderColor: color };
      }

      const candleSeries = mainChart.addCandlestickSeries({
        upColor: CANDLE_TIER_COLORS.bull[0], downColor: CANDLE_TIER_COLORS.bear[0],
        wickUpColor: CANDLE_TIER_COLORS.bull[0], wickDownColor: CANDLE_TIER_COLORS.bear[0],
        borderUpColor: CANDLE_TIER_COLORS.bull[0], borderDownColor: CANDLE_TIER_COLORS.bear[0],
      });
      const volumeSeries = mainChart.addHistogramSeries({
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume_scale',
      });
      mainChart.priceScale('volume_scale').applyOptions({
        scaleMargins: { top: 0.85, bottom: 0 },  // squeeze volume bars into the bottom 15% of the pane
      });
      // 20-bar volume average line, same price scale as the volume bars
      // so it overlays correctly rather than getting its own axis.
      const volumeAvgSeries = mainChart.addLineSeries({
        priceScaleId: 'volume_scale', color: '#f59e0b', lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      });
      function computeVolumeAvg(chartData, period) {
        const out = [];
        for (let i = 0; i < chartData.length; i++) {
          if (i < period - 1) continue;
          let sum = 0;
          for (let j = i - period + 1; j <= i; j++) sum += (chartData[j].volume || 0);
          out.push({ time: chartData[i].time, value: sum / period });
        }
        return out;
      }

      // Click-to-position sync: clicking a point in time on this chart
      // broadcasts that time to other windows (subject to Sync
      // Symbol/Interval, see userSetTimePosition below). userSetTimePosition
      // is a function declaration further down in this same scope --
      // safe to reference here since function declarations are hoisted,
      // and this callback only ever actually runs on a real click, well
      // after the rest of widget setup has completed.
      //
      // param.time here is whatever time value is actually plotted on
      // THIS chart -- which, for intraday timeframes, is a synthetic
      // gap-compressed time (see chartTimeBase/chartBucketSeconds below),
      // not the bar's real timestamp. chartTimeToIndex() inverts that
      // back to a bar index, then currentBars[idx].time gives the real
      // timestamp to actually broadcast -- other windows compare against
      // real time in their own setTimePosition(), regardless of whether
      // THEY are also gap-compressed.
      // Unified "which bar was clicked" helper, working for both the
      // gap-compressed (synthetic time) and uncompressed (daily+, real
      // time) cases -- reused by both replay-picking below and the
      // existing click-to-sync logic, which already had to solve this
      // same problem for the compressed case via chartTimeToIndex().
      function getClickedBarIndex(param) {
        if (!param || param.time == null) return null;
        if (!chartBucketSeconds) {
          const idx = realTimeToIndexMap.get(param.time);
          return (idx == null) ? null : idx;
        }
        const idx = chartTimeToIndex(param.time);
        if (idx == null || idx < 0 || idx >= currentBars.length) return null;
        return idx;
      }

      mainChart.subscribeClick((param) => {
        if (!param || param.time == null) return; // click landed outside the plotted bars (e.g. in the margin)
        if (replayMode === 'picking') {
          const idx = getClickedBarIndex(param);
          if (idx != null) startReplayAt(idx);
          return;
        }
        if (rvpMode === 'picking-start' || rvpMode === 'picking-end') {
          const idx = getClickedBarIndex(param);
          if (idx != null) rvpHandlePick(idx);
          return;
        }
        if (cvdMode === 'picking-start' || cvdMode === 'picking-end') {
          const idx = getClickedBarIndex(param);
          if (idx != null) cvdHandlePick(idx);
          return;
        }
        if (!chartBucketSeconds) {
          // Daily+ timeframe: not gap-compressed, so the plotted time
          // already IS the real bar time -- no inversion needed.
          userSetTimePosition(param.time);
          return;
        }
        const idx = chartTimeToIndex(param.time);
        if (idx == null || idx < 0 || idx >= currentBars.length) return;
        userSetTimePosition(currentBars[idx].time);
      });

      // Jump-to-latest button: previously used Lightweight Charts' own
      // scrollToRealTime(), which scrolls to align with actual wall-clock
      // Date.now() -- broken by the gap-compression work above, since
      // bars now plot at a SYNTHETIC time that has no relationship to
      // real time at all, so "scroll to real time" was scrolling to a
      // position that doesn't correspond to any actual bar. Fixed the
      // same way setTimePosition() already does it: logical
      // (index-based) range, not time-based, ending just past the last
      // bar's index with a small right margin so it's not flush against
      // the edge -- same convention most charting platforms use for this
      // button -- while preserving whatever zoom width was already set.
      const scrollLatestBtn = root.querySelector('.scroll-latest-btn');
      if (scrollLatestBtn) {
        scrollLatestBtn.addEventListener('click', () => {
          try {
            const ts = mainChart.timeScale();
            const lastIdx = currentBars.length - 1;
            if (lastIdx < 0) return;
            const currentRange = ts.getVisibleLogicalRange();
            const width = (currentRange && isFinite(currentRange.to - currentRange.from))
              ? (currentRange.to - currentRange.from) : 60;
            const rightMargin = Math.max(2, Math.round(width * 0.05));
            ts.setVisibleLogicalRange({ from: lastIdx - width + rightMargin, to: lastIdx + rightMargin });
          } catch (e) {
            console.warn(`[realtime] ${symbol}: scroll-to-latest failed -- ${e.message || e}`);
          }
        });
      }

      root.querySelector('.replayBtn').addEventListener('click', () => {
        if (replayMode === 'picking') { cancelReplayPick(); return; }
        if (replayMode === 'active') return;  // use Stop Replay to exit, not this button again
        enterReplayPickMode();
      });
      root.querySelector('.replayPickCancel').addEventListener('click', cancelReplayPick);
      root.querySelector('.replayStepBtn').addEventListener('click', replayStep);
      root.querySelector('.replayPlayBtn').addEventListener('click', replayTogglePlay);
      root.querySelector('.replayStopBtn').addEventListener('click', stopReplay);

      const paneCharts = {};
      // Subpane heights are stored as FRACTIONS of total content height
      // (mainChartWrap + all visible subpanes combined), not fixed
      // pixels -- this is what actually makes "resize the window" keep
      // panes in the same ratio instead of the main chart absorbing
      // 100% of the size change while subpanes stay pixel-locked (which
      // is what was happening before: a smaller window meant fixed-
      // height subpanes ate a growing SHARE of the shrunk space). Reapplied
      // against the CURRENT total content height on every window resize
      // (see resizeAll()) and on layout/template load, so it's always
      // relative to whatever size the window actually is right now, not
      // frozen at whatever size it was when last saved.
      let paneHeightFractions = { rsi: 0.28, macd: 0.24, adx: 0.24, volquant: 0.24 };
      function totalContentHeight() {
        let total = mainEl.clientHeight;
        Object.entries(paneCharts).forEach(([name, { el }]) => {
          if (isPaneChecked(name)) total += el.clientHeight;
        });
        return total;
      }
      function applyPaneHeightFractions() {
        const total = totalContentHeight();
        if (total <= 0) return;
        Object.entries(paneCharts).forEach(([name, { el }]) => {
          if (!isPaneChecked(name)) return;
          const frac = paneHeightFractions[name];
          if (!frac) return;
          const px = Math.max(60, Math.round(frac * total));
          el.style.height = px + 'px';
        });
      }
      const paneSeries = {};
      ['rsi', 'macd', 'adx', 'volquant'].forEach((name) => {
        const el = root.querySelector(`[data-pane-el="${name}"]`);
        const chart = LightweightCharts.createChart(el, chartOpts);
        paneCharts[name] = { chart, el };
      });

      // Colors matched to the reference Pine Script exactly: RSI3=black,
      // RSI14=blue, RSI-EMA13=green, RSI-EMA90=red (rsiemalength1/2 in
      // that source). RSI14's color corrected here from the previous
      // indigo placeholder to the actual blue the reference uses.
      paneSeries.rsiLine = paneCharts.rsi.chart.addLineSeries({ color: '#2563eb', lineWidth: 2 });
      paneSeries.rsi3Line = paneCharts.rsi.chart.addLineSeries({ color: '#111827', lineWidth: 1 });
      paneSeries.rsiEma13Line = paneCharts.rsi.chart.addLineSeries({ color: '#16a34a', lineWidth: 2 });
      paneSeries.rsiEma90Line = paneCharts.rsi.chart.addLineSeries({ color: '#dc2626', lineWidth: 1 });
      paneSeries.rsiDiffHist = paneCharts.rsi.chart.addHistogramSeries({ color: '#475569' });
      function isRsiSubChecked(name) {
        const cb = root.querySelector(`[data-rsisub="${name}"]`);
        return cb ? cb.checked : false;
      }
      paneSeries.macdLine = paneCharts.macd.chart.addLineSeries({ color: '#1d6fdb', lineWidth: 1.5 });
      paneSeries.macdSignal = paneCharts.macd.chart.addLineSeries({ color: '#000000', lineWidth: 1 });
      paneSeries.macdHist = paneCharts.macd.chart.addHistogramSeries({ color: '#475569' });
      paneSeries.adxLine = paneCharts.adx.chart.addLineSeries({ color: '#eab308', lineWidth: 1.5 });
      paneSeries.vqHist = paneCharts.volquant.chart.addHistogramSeries({ color: '#475569' });
      paneSeries.vqLine = paneCharts.volquant.chart.addLineSeries({ color: '#2dd4bf', lineWidth: 1 });

      const allCharts = [mainChart, paneCharts.rsi.chart, paneCharts.macd.chart, paneCharts.adx.chart, paneCharts.volquant.chart];
      allCharts.forEach((c) => {
        c.timeScale().subscribeVisibleLogicalRangeChange((range) => {
          if (!range) return;
          allCharts.forEach((other) => { if (other !== c) other.timeScale().setVisibleLogicalRange(range); });
        });
      });

      // TradingView-style synced crosshair: one anchor series per pane
      // (whatever that pane's primary series is), so hovering ANY pane
      // draws the vertical line at the same bar across ALL of them, not
      // just the one under the mouse. setCrosshairPosition() doesn't
      // re-fire subscribeCrosshairMove on the chart it's called on, so
      // this doesn't need a re-entrancy guard -- that's the documented
      // behavior this pattern relies on, not an assumption.
      const crosshairAnchors = [
        { chart: mainChart, series: candleSeries },
        { chart: paneCharts.rsi.chart, series: paneSeries.rsiLine },
        { chart: paneCharts.macd.chart, series: paneSeries.macdLine },
        { chart: paneCharts.adx.chart, series: paneSeries.adxLine },
        { chart: paneCharts.volquant.chart, series: paneSeries.vqLine },
      ];
      const legendEl = root.querySelector('.chart-legend-badge');
      function fmtLegendNum(v) {
        return (v == null || Number.isNaN(v)) ? '-' : Number(v).toFixed(2);
      }
      crosshairAnchors.forEach(({ chart, series }) => {
        chart.subscribeCrosshairMove((param) => {
          crosshairAnchors.forEach((other) => {
            if (other.chart === chart) return;
            if (param.time != null) other.chart.setCrosshairPosition(0, param.time, other.series);
            else other.chart.clearCrosshairPosition();
          });
          if (chart !== mainChart || !legendEl) return;
          if (param.time == null) { legendEl.style.display = 'none'; return; }
          const real = realTimeFor(param.time);
          const d = new Date(real * 1000);
          const dateStr = d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
          const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
          const bar = param.seriesData && param.seriesData.get(candleSeries);
          const vol = param.seriesData && param.seriesData.get(volumeSeries);
          legendEl.innerHTML = bar
            ? `${dateStr} ${chartBucketSeconds ? timeStr : ''} &nbsp; O <b>${fmtLegendNum(bar.open)}</b> H <b>${fmtLegendNum(bar.high)}</b> L <b>${fmtLegendNum(bar.low)}</b> C <b>${fmtLegendNum(bar.close)}</b>${vol ? ` &nbsp; Vol <b>${Math.round(vol.value).toLocaleString()}</b>` : ''}`
            : `${dateStr} ${chartBucketSeconds ? timeStr : ''}`;
          legendEl.style.display = 'block';
        });
      });
      mainChart.timeScale().subscribeVisibleTimeRangeChange(() => {
        // Hide the legend whenever the visible range changes (pan/zoom/
        // symbol switch) rather than leave a stale readout from wherever
        // the mouse last was showing -- it'll reappear on the next hover.
        if (legendEl) legendEl.style.display = 'none';
      });

      function ensureOverlay(name, color, lineWidth) {
        if (!overlaySeries[name]) {
          overlaySeries[name] = mainChart.addLineSeries({
            color, lineWidth: lineWidth || 1.5, priceLineVisible: false, lastValueVisible: false,
          });
        }
        return overlaySeries[name];
      }

      function isOvChecked(name) {
        const cb = root.querySelector(`[data-ov="${name}"]`);
        return cb ? cb.checked : false;
      }
      function isPaneChecked(name) {
        const cb = root.querySelector(`[data-pane="${name}"]`);
        return cb ? cb.checked : false;
      }
      function isBadgeChecked(name) {
        const cb = root.querySelector(`[data-badge="${name}"]`);
        return cb ? cb.checked : false;
      }

      function clearSrLines() {
        srLines.forEach((l) => candleSeries.removePriceLine(l));
        srLines = [];
      }
      // sr_levels = {supports: [...], resistances: [...]}, each already
      // sorted nearest-to-spot first by the backend (nearest_levels()) --
      // index 0 becomes R1/S1, index 1 becomes R2/S2, etc. Resistance
      // (above price) drawn in a red/orange family, support (below
      // price) in a green family, brightest/most opaque for the nearest
      // level and fading for farther ones, so R1 reads as more
      // significant than R3 at a glance.
      const SR_RES_COLORS = ['#ef4444', '#f97316', '#fb923c', '#fdba74', '#fed7aa'];
      const SR_SUP_COLORS = ['#22c55e', '#4ade80', '#86efac', '#bbf7d0', '#dcfce7'];
      function drawSrLines(levels) {
        clearSrLines();
        if (!isOvChecked('sr') || !levels) return;
        (levels.resistances || []).forEach((lvl, i) => {
          srLines.push(candleSeries.createPriceLine({
            price: lvl.level, color: SR_RES_COLORS[Math.min(i, SR_RES_COLORS.length - 1)],
            lineWidth: i === 0 ? 2 : 1, lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true, title: `R${i + 1}`,
          }));
        });
        (levels.supports || []).forEach((lvl, i) => {
          srLines.push(candleSeries.createPriceLine({
            price: lvl.level, color: SR_SUP_COLORS[Math.min(i, SR_SUP_COLORS.length - 1)],
            lineWidth: i === 0 ? 2 : 1, lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true, title: `S${i + 1}`,
          }));
        });
      }

      // Deliberately just 4 levels (developing today's POC + D1/W1/M1 --
      // NOT the full VAH/VAL or the whole day/week/month pools the scanner
      // keeps internally): the Pine indicator version of this went through
      // several rounds of "too many lines, chart is noisy" before landing
      // on a minimal single-line-per-period set, so this starts there
      // directly instead of repeating that trial and error on this chart.
      function clearVpLines() {
        vpLines.forEach((l) => candleSeries.removePriceLine(l));
        vpLines = [];
      }
      function drawVpLines(levels) {
        clearVpLines();
        if (!isOvChecked('vp') || !levels) return;
        const specs = [
          { key: 'developing', title: 'Dev POC', color: '#14b8a6', width: 2, dashed: true },
          { key: 'd1', title: 'D1 POC', color: '#eab308', width: 2, dashed: false },
          { key: 'w1', title: 'W1 POC', color: '#ec4899', width: 2, dashed: false },
          { key: 'm1', title: 'M1 POC', color: '#8b5cf6', width: 1, dashed: false },
        ];
        specs.forEach((s) => {
          const prof = levels[s.key];
          if (!prof || prof.poc == null) return;
          vpLines.push(candleSeries.createPriceLine({
            price: prof.poc, color: s.color, lineWidth: s.width,
            lineStyle: s.dashed ? LightweightCharts.LineStyle.Dotted : LightweightCharts.LineStyle.Solid,
            axisLabelVisible: true, title: s.title,
          }));
        });
      }

      // Slope of the EMA13-EMA50 spread over the last 10 bars, exact
      // same degrees(atan(...)) formula as SlopeDeg() in scanner_builder.py
      // (verified against that file's real implementation before use,
      // not re-derived): raw_slope = (spread_now - spread_10_bars_ago) / 10,
      // slope_deg = degrees(atan(raw_slope)). Deliberately NOT the
      // percent-normalized SlopeDegPerBar variant -- that divides by the
      // PRIOR value as its base, which is fine for a price series (always
      // positive, never near zero) but numerically unstable for a spread
      // that can itself cross zero near an EMA13/EMA50 crossover, exactly
      // the moment this badge is most useful for.
      // ema13Arr/ema50Arr are the SAME chronologically-ascending arrays
      // fed to the overlay lines (oldest to newest, real timestamps) --
      // assumed equal length since EMA (unlike SMA) has no NaN warm-up
      // period, only settles in accuracy over time, not literal missing
      // values; a defensive length/short-array check below fails safe
      // (hides the badge) rather than computing something wrong if that
      // assumption is ever violated.
      function updateEmaSlopeBadge(ema13Arr, ema50Arr) {
        const badge = root.querySelector('.ema-slope-badge');
        if (!badge) return;
        if (!isBadgeChecked('ema-slope') || !ema13Arr || !ema50Arr) {
          badge.style.display = 'none';
          return;
        }
        const bars = 10;
        const n = Math.min(ema13Arr.length, ema50Arr.length);
        if (n <= bars) {
          badge.style.display = 'none';
          return;
        }
        const idx = n - 1;
        const prevIdx = idx - bars;
        const curSpread = ema13Arr[idx].value - ema50Arr[idx].value;
        const prevSpread = ema13Arr[prevIdx].value - ema50Arr[prevIdx].value;
        const rawSlope = (curSpread - prevSpread) / bars;
        const slopeDeg = Math.atan(rawSlope) * (180 / Math.PI);

        badge.style.display = 'block';
        badge.textContent = `EMA13/50 slope: ${slopeDeg >= 0 ? '+' : ''}${slopeDeg.toFixed(1)}°`;
        badge.style.color = slopeDeg > 0.5 ? '#22c55e' : slopeDeg < -0.5 ? '#ef4444' : '#94a3b8';
        badge.style.borderColor = slopeDeg > 0.5 ? '#166534' : slopeDeg < -0.5 ? '#7f1d1d' : '#334155';
      }

      // ---------- Range Volume Profile ----------
      // Computes a delta-colored (buy/sell split) volume-at-price profile
      // over a user-picked bar range -- Fabio's "who controls this
      // consolidation zone" analysis. Delta split uses the same
      // close-location-value proxy as the CVD confluence check elsewhere
      // in this app (no real Trade-level DXLink data wired up yet -- see
      // the bubble-layer work planned next): clv = (2*(close-low)/(high-low))-1,
      // buyFraction = (clv+1)/2. Until per-trade data exists, this is the
      // best available approximation of who was in control at each price.
      const RVP_ROWS = 24;
      const RVP_VA_PCT = 0.70;
      const RVP_MAX_BAR_PX = 130;   // max pixel width a fully-dominant bin's bar can reach

      function computeRangeVolumeProfile(bars) {
        if (!bars || bars.length === 0) return null;
        let lo = Infinity, hi = -Infinity;
        bars.forEach((b) => { lo = Math.min(lo, b.low); hi = Math.max(hi, b.high); });
        if (!(hi > lo)) return null;
        const step = (hi - lo) / RVP_ROWS;
        const buyBins = new Array(RVP_ROWS).fill(0);
        const sellBins = new Array(RVP_ROWS).fill(0);
        bars.forEach((b) => {
          const mid = (b.high + b.low) / 2;
          let idx = Math.floor((mid - lo) / step);
          idx = Math.max(0, Math.min(RVP_ROWS - 1, idx));
          const range = Math.max(b.high - b.low, 1e-9);
          const clv = (2 * (b.close - b.low) / range) - 1;
          const buyFrac = (clv + 1) / 2;
          const vol = b.volume || 0;
          buyBins[idx] += vol * buyFrac;
          sellBins[idx] += vol * (1 - buyFrac);
        });
        const totalBins = buyBins.map((v, i) => v + sellBins[i]);
        const total = totalBins.reduce((a, v) => a + v, 0);
        if (total <= 0) return null;
        let pocIdx = 0;
        for (let i = 1; i < RVP_ROWS; i++) if (totalBins[i] > totalBins[pocIdx]) pocIdx = i;
        const poc = lo + (pocIdx + 0.5) * step;
        const targetVol = total * RVP_VA_PCT;
        let cum = totalBins[pocIdx], left = pocIdx, right = pocIdx;
        while (cum < targetVol && (left > 0 || right < RVP_ROWS - 1)) {
          const nl = left > 0 ? totalBins[left - 1] : -1;
          const nr = right < RVP_ROWS - 1 ? totalBins[right + 1] : -1;
          if (nr >= nl && right < RVP_ROWS - 1) { right++; cum += nr; }
          else if (left > 0) { left--; cum += nl; }
          else break;
        }
        const vah = lo + (right + 1) * step;
        const val = lo + left * step;
        const maxBinVol = Math.max(...totalBins);
        // Max |buy-sell| across bins, used to scale the CVD (left) side --
        // deliberately a SEPARATE scale from maxBinVol (right side),
        // since delta magnitude and total-volume magnitude aren't the
        // same thing: a bin can have huge volume but near-zero net
        // delta (balanced), or modest volume that's almost entirely
        // one-sided.
        const maxAbsDelta = Math.max(...buyBins.map((v, i) => Math.abs(v - sellBins[i])), 1e-9);
        const totalBuy = buyBins.reduce((a, v) => a + v, 0);
        const totalSell = sellBins.reduce((a, v) => a + v, 0);
        return { lo, hi, step, buyBins, sellBins, totalBins, maxBinVol, maxAbsDelta, poc, vah, val, totalBuy, totalSell };
      }

      function rvpCanvas() {
        return root.querySelector('.rvp-canvas');
      }

      function resizeRvpCanvas() {
        const cv = rvpCanvas();
        if (!cv) return;
        const w = mainEl.clientWidth, h = mainEl.clientHeight;
        if (cv.width !== w) cv.width = w;
        if (cv.height !== h) cv.height = h;
      }

      function clearRangeProfile() {
        const cv = rvpCanvas();
        if (cv) { const ctx = cv.getContext('2d'); ctx.clearRect(0, 0, cv.width, cv.height); }
      }

      function drawRangeProfile() {
        const cv = rvpCanvas();
        if (!cv || !rvpData || rvpStartIdx == null || rvpEndIdx == null) return;
        resizeRvpCanvas();
        const ctx = cv.getContext('2d');
        ctx.clearRect(0, 0, cv.width, cv.height);

        const startBar = currentBars[Math.min(rvpStartIdx, rvpEndIdx)];
        const endBar = currentBars[Math.max(rvpStartIdx, rvpEndIdx)];
        if (!startBar || !endBar) return;
        const x1 = mainChart.timeScale().timeToCoordinate(startBar.time);
        const x2 = mainChart.timeScale().timeToCoordinate(endBar.time);
        const yLo = candleSeries.priceToCoordinate(rvpData.lo);
        const yHi = candleSeries.priceToCoordinate(rvpData.hi);
        if (x1 == null || x2 == null || yLo == null || yHi == null) return;  // scrolled off-screen this frame

        // Bounding box, same visual language as the yellow consolidation-zone box.
        ctx.strokeStyle = 'rgba(234,179,8,0.7)';
        ctx.fillStyle = 'rgba(234,179,8,0.06)';
        ctx.lineWidth = 1;
        ctx.fillRect(x1, yHi, x2 - x1, yLo - yHi);
        ctx.strokeRect(x1, yHi, x2 - x1, yLo - yHi);

        // Simple, single-sided Volume Profile: bars grow RIGHT from the
        // anchor, length = total volume at that price level (the major
        // volume nodes stand out directly by bar length), colored by
        // whichever side had more volume there. Deliberately NOT
        // combined with a CVD/delta display in the same drawing --
        // that combined version kept breaking in ways that were hard to
        // isolate; this and the separate CVD Profile tool below are
        // fully independent, each simple enough to verify in isolation.
        const BUY_COLOR = 'rgba(59,130,246,0.60)';   // blue
        const SELL_COLOR = 'rgba(239,68,68,0.60)';   // red

        for (let i = 0; i < RVP_ROWS; i++) {
          const binLo = rvpData.lo + i * rvpData.step;
          const binHi = binLo + rvpData.step;
          const yBinTop = candleSeries.priceToCoordinate(binHi);
          const yBinBot = candleSeries.priceToCoordinate(binLo);
          if (yBinTop == null || yBinBot == null) continue;
          const binH = Math.max(1, yBinBot - yBinTop);
          const buyVol = rvpData.buyBins[i], sellVol = rvpData.sellBins[i];
          const totalVol = buyVol + sellVol;
          if (totalVol <= 0) continue;

          const volPx = (totalVol / rvpData.maxBinVol) * RVP_MAX_BAR_PX;
          ctx.fillStyle = buyVol >= sellVol ? BUY_COLOR : SELL_COLOR;
          ctx.fillRect(x1, yBinTop, volPx, binH - 1);
        }

        // POC / VAH / VAL reference lines across the box width, with labels.
        function hline(price, color, width, dashed, label) {
          const y = candleSeries.priceToCoordinate(price);
          if (y == null) return;
          ctx.strokeStyle = color;
          ctx.lineWidth = width;
          ctx.setLineDash(dashed ? [4, 3] : []);
          ctx.beginPath();
          ctx.moveTo(x1, y);
          ctx.lineTo(x2, y);
          ctx.stroke();
          ctx.setLineDash([]);
          if (label) {
            ctx.fillStyle = color;
            ctx.font = '10px system-ui, sans-serif';
            ctx.fillText(`${label} ${price.toFixed(2)}`, x2 + 4, y + 3);
          }
        }
        hline(rvpData.vah, 'rgba(148,163,184,0.8)', 1, true, 'VAH');
        hline(rvpData.val, 'rgba(148,163,184,0.8)', 1, true, 'VAL');
        hline(rvpData.poc, '#eab308', 2, false, 'POC');

        // Delta summary in the box's top-left corner.
        const buyPct = Math.round((rvpData.totalBuy / (rvpData.totalBuy + rvpData.totalSell)) * 100);
        ctx.fillStyle = buyPct >= 50 ? '#3b82f6' : '#ef4444';
        ctx.font = 'bold 11px system-ui, sans-serif';
        ctx.fillText(`Delta: ${buyPct}% buy / ${100 - buyPct}% sell`, x1 + 6, yHi + 14);
      }

      function rvpHandlePick(idx) {
        if (rvpMode === 'picking-start') {
          rvpStartIdx = idx;
          rvpMode = 'picking-end';
          root.querySelector('.rvp-pick-msg').textContent = 'Click the END of the range';
          return;
        }
        if (rvpMode === 'picking-end') {
          rvpEndIdx = idx;
          const lo = Math.min(rvpStartIdx, rvpEndIdx), hi = Math.max(rvpStartIdx, rvpEndIdx);
          const bars = currentBars.slice(lo, hi + 1);
          rvpData = computeRangeVolumeProfile(bars);
          rvpMode = rvpData ? 'active' : 'off';
          root.querySelector('.rvp-pick-banner').style.display = 'none';
          root.querySelector('.rvpBtn').classList.toggle('active', rvpMode === 'active');
          root.querySelector('.rvpClearBtn').style.display = rvpMode === 'active' ? 'inline-block' : 'none';
          drawRangeProfile();
        }
      }

      function rvpStartPicking() {
        if (replayMode !== 'off') return;  // don't overlap with replay's own click-picking mode
        rvpMode = 'picking-start';
        rvpStartIdx = null; rvpEndIdx = null;
        root.querySelector('.rvp-pick-msg').textContent = 'Click the START of the range';
        root.querySelector('.rvp-pick-banner').style.display = 'flex';
        root.querySelector('.rvpClearBtn').style.display = 'none';
        root.querySelector('.rvpBtn').classList.add('active');
      }

      function rvpCancelPicking() {
        rvpMode = rvpData ? 'active' : 'off';
        root.querySelector('.rvp-pick-banner').style.display = 'none';
        root.querySelector('.rvpBtn').classList.toggle('active', rvpMode === 'active');
        root.querySelector('.rvpClearBtn').style.display = rvpMode === 'active' ? 'inline-block' : 'none';
      }

      function rvpClear() {
        rvpMode = 'off'; rvpStartIdx = null; rvpEndIdx = null; rvpData = null;
        clearRangeProfile();
        root.querySelector('.rvpBtn').classList.remove('active');
        root.querySelector('.rvpClearBtn').style.display = 'none';
        root.querySelector('.rvp-pick-banner').style.display = 'none';
      }

      root.querySelector('.rvpBtn').addEventListener('click', () => {
        if (rvpMode === 'picking-start' || rvpMode === 'picking-end') { rvpCancelPicking(); return; }
        rvpStartPicking();
      });
      root.querySelector('.rvpPickCancel').addEventListener('click', rvpCancelPicking);
      root.querySelector('.rvpClearBtn').addEventListener('click', rvpClear);

      // Keep the overlay anchored correctly as the user pans/zooms --
      // it's a plain canvas, not a chart-native primitive, so it has no
      // built-in way to track coordinate changes on its own. Resize is
      // handled inside resizeAll() itself (see above), not a second
      // ResizeObserver here -- see that function's comment for why.
      mainChart.timeScale().subscribeVisibleTimeRangeChange(() => {
        if (rvpData) drawRangeProfile();
        if (bubbleMode === 'on') drawBubbles();
      });

      // ---------- CVD Profile (Tool 2 -- independent of Volume Profile) ----------
      // Same picking flow, same underlying computeRangeVolumeProfile(),
      // but its own state and its own canvas -- see the state
      // declarations above for why these two tools are kept fully
      // separate rather than merged into one with a mode switch.
      function cvdCanvas() {
        return root.querySelector('.cvd-canvas');
      }

      function resizeCvdCanvas() {
        const cv = cvdCanvas();
        if (!cv) return;
        const w = mainEl.clientWidth, h = mainEl.clientHeight;
        if (cv.width !== w) cv.width = w;
        if (cv.height !== h) cv.height = h;
      }

      function clearCvdProfile() {
        const cv = cvdCanvas();
        if (cv) { const ctx = cv.getContext('2d'); ctx.clearRect(0, 0, cv.width, cv.height); }
      }

      function drawCvdProfile() {
        const cv = cvdCanvas();
        if (!cv || !cvdData || cvdStartIdx == null || cvdEndIdx == null) return;
        resizeCvdCanvas();
        const ctx = cv.getContext('2d');
        ctx.clearRect(0, 0, cv.width, cv.height);

        const startBar = currentBars[Math.min(cvdStartIdx, cvdEndIdx)];
        const endBar = currentBars[Math.max(cvdStartIdx, cvdEndIdx)];
        if (!startBar || !endBar) return;
        const x1 = mainChart.timeScale().timeToCoordinate(startBar.time);
        const x2 = mainChart.timeScale().timeToCoordinate(endBar.time);
        const yLo = candleSeries.priceToCoordinate(cvdData.lo);
        const yHi = candleSeries.priceToCoordinate(cvdData.hi);
        if (x1 == null || x2 == null || yLo == null || yHi == null) return;

        // Bounding box, same visual language as Volume Profile's -- a
        // different border color (teal, not yellow) so it's visually
        // distinguishable if both tools happen to be active at once.
        ctx.strokeStyle = 'rgba(45,212,191,0.7)';
        ctx.fillStyle = 'rgba(45,212,191,0.06)';
        ctx.lineWidth = 1;
        ctx.fillRect(x1, yHi, x2 - x1, yLo - yHi);
        ctx.strokeRect(x1, yHi, x2 - x1, yLo - yHi);

        // Net delta (CVD) at each price level -- bar length shows how
        // imbalanced that level was, color shows which way. Independent
        // of total volume at that level (a level can have huge volume
        // but near-zero delta, or tiny volume that was almost entirely
        // one-sided) -- that's specifically what this tool isolates,
        // separate from the total-volume view Tool 1 shows.
        const BUY_COLOR = 'rgba(59,130,246,0.60)';   // blue
        const SELL_COLOR = 'rgba(239,68,68,0.60)';   // red

        for (let i = 0; i < RVP_ROWS; i++) {
          const binLo = cvdData.lo + i * cvdData.step;
          const binHi = binLo + cvdData.step;
          const yBinTop = candleSeries.priceToCoordinate(binHi);
          const yBinBot = candleSeries.priceToCoordinate(binLo);
          if (yBinTop == null || yBinBot == null) continue;
          const binH = Math.max(1, yBinBot - yBinTop);
          const buyVol = cvdData.buyBins[i], sellVol = cvdData.sellBins[i];
          const netDelta = buyVol - sellVol;
          if (netDelta === 0) continue;

          const deltaPx = (Math.abs(netDelta) / cvdData.maxAbsDelta) * RVP_MAX_BAR_PX;
          ctx.fillStyle = netDelta >= 0 ? BUY_COLOR : SELL_COLOR;
          ctx.fillRect(x1, yBinTop, deltaPx, binH - 1);
        }

        // POC line only (the fair-value reference is still useful context
        // on a delta-focused view) -- no VAH/VAL here, keeping this
        // tool's output focused on what it's actually for.
        const pocY = candleSeries.priceToCoordinate(cvdData.poc);
        if (pocY != null) {
          ctx.strokeStyle = '#eab308';
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.moveTo(x1, pocY);
          ctx.lineTo(x2, pocY);
          ctx.stroke();
          ctx.fillStyle = '#eab308';
          ctx.font = '10px system-ui, sans-serif';
          ctx.fillText(`POC ${cvdData.poc.toFixed(2)}`, x2 + 4, pocY + 3);
        }

        const buyPct = Math.round((cvdData.totalBuy / (cvdData.totalBuy + cvdData.totalSell)) * 100);
        ctx.fillStyle = buyPct >= 50 ? '#3b82f6' : '#ef4444';
        ctx.font = 'bold 11px system-ui, sans-serif';
        ctx.fillText(`CVD: ${buyPct}% buy / ${100 - buyPct}% sell`, x1 + 6, yHi + 14);
      }

      function cvdHandlePick(idx) {
        if (cvdMode === 'picking-start') {
          cvdStartIdx = idx;
          cvdMode = 'picking-end';
          root.querySelector('.cvd-pick-msg').textContent = 'Click the END of the range';
          return;
        }
        if (cvdMode === 'picking-end') {
          cvdEndIdx = idx;
          const lo = Math.min(cvdStartIdx, cvdEndIdx), hi = Math.max(cvdStartIdx, cvdEndIdx);
          const bars = currentBars.slice(lo, hi + 1);
          cvdData = computeRangeVolumeProfile(bars);
          cvdMode = cvdData ? 'active' : 'off';
          root.querySelector('.cvd-pick-banner').style.display = 'none';
          root.querySelector('.cvdBtn').classList.toggle('active', cvdMode === 'active');
          root.querySelector('.cvdClearBtn').style.display = cvdMode === 'active' ? 'inline-block' : 'none';
          drawCvdProfile();
        }
      }

      function cvdStartPicking() {
        if (replayMode !== 'off') return;
        cvdMode = 'picking-start';
        cvdStartIdx = null; cvdEndIdx = null;
        root.querySelector('.cvd-pick-msg').textContent = 'Click the START of the range';
        root.querySelector('.cvd-pick-banner').style.display = 'flex';
        root.querySelector('.cvdClearBtn').style.display = 'none';
        root.querySelector('.cvdBtn').classList.add('active');
      }

      function cvdCancelPicking() {
        cvdMode = cvdData ? 'active' : 'off';
        root.querySelector('.cvd-pick-banner').style.display = 'none';
        root.querySelector('.cvdBtn').classList.toggle('active', cvdMode === 'active');
        root.querySelector('.cvdClearBtn').style.display = cvdMode === 'active' ? 'inline-block' : 'none';
      }

      function cvdClear() {
        cvdMode = 'off'; cvdStartIdx = null; cvdEndIdx = null; cvdData = null;
        clearCvdProfile();
        root.querySelector('.cvdBtn').classList.remove('active');
        root.querySelector('.cvdClearBtn').style.display = 'none';
        root.querySelector('.cvd-pick-banner').style.display = 'none';
      }

      root.querySelector('.cvdBtn').addEventListener('click', () => {
        if (cvdMode === 'picking-start' || cvdMode === 'picking-end') { cvdCancelPicking(); return; }
        cvdStartPicking();
      });
      root.querySelector('.cvdPickCancel').addEventListener('click', cvdCancelPicking);
      root.querySelector('.cvdClearBtn').addEventListener('click', cvdClear);

      mainChart.timeScale().subscribeVisibleTimeRangeChange(() => {
        if (cvdData) drawCvdProfile();
      });

      // ---------- Bubble layer (live large trades) ----------
      // Fabio's "watch individual prints" tool: each classified trade
      // above the size threshold draws as a circle sized by trade size,
      // green = buy-initiated, red = sell-initiated, gray = couldn't be
      // classified (no bid/ask at that moment). This is the live,
      // discretionary counterpart to the codified "no first touch,
      // require retest/hold" rule now built into the scanner -- here
      // it's your own eyes doing that judgment call in real time, same
      // as Fabio actually works.
      //
      // Requires tastytrade Trade-level DXLink data (not just Quote/
      // Candle) -- see tastytrade_feed.py's ensure_trade_stream()
      // docstring. If your account's entitlement doesn't include it, the
      // status banner below will say so plainly rather than silently
      // showing nothing.
      let bubbleMode = 'off';         // 'off' | 'on'
      let bubbleThresholdVal = 10;
      let bubbleBuffer = [];          // classified trades kept for drawing, capped below
      let bubbleLastSeenTs = null;    // server timestamp of the newest trade already fetched
      let bubblePollTimer = null;
      const BUBBLE_MAX_KEPT = 400;         // oldest trades drop off past this
      const BUBBLE_MAX_AGE_SEC = 20 * 60;  // also drop anything older than 20 min
      const BUBBLE_MAX_RADIUS_PX = 22;

      function bubbleCanvasEl() { return root.querySelector('.bubble-canvas'); }
      function resizeBubbleCanvas() {
        const cv = bubbleCanvasEl();
        if (!cv) return;
        const w = mainEl.clientWidth, h = mainEl.clientHeight;
        if (cv.width !== w) cv.width = w;
        if (cv.height !== h) cv.height = h;
      }
      function clearBubbleCanvas() {
        const cv = bubbleCanvasEl();
        if (cv) { const ctx = cv.getContext('2d'); ctx.clearRect(0, 0, cv.width, cv.height); }
      }

      function bubbleStatusBanner(text, show) {
        const el = root.querySelector('.bubble-status-banner');
        if (!el) return;
        el.textContent = text || '';
        el.style.display = show ? 'block' : 'none';
      }

      async function bubblePoll() {
        try {
          const params = new URLSearchParams({ min_size: String(bubbleThresholdVal) });
          if (bubbleLastSeenTs != null) params.set('since', String(bubbleLastSeenTs));
          const r = await fetch(`/realtime/api/${encodeURIComponent(symbol)}/bubbles?${params}`);
          const d = await r.json();
          const trades = d.trades || [];
          if (trades.length) {
            bubbleBuffer.push(...trades);
            bubbleLastSeenTs = trades[trades.length - 1].time;
          }
          const cutoff = (d.server_time || (Date.now() / 1000)) - BUBBLE_MAX_AGE_SEC;
          bubbleBuffer = bubbleBuffer.filter((t) => t.time >= cutoff);
          if (bubbleBuffer.length > BUBBLE_MAX_KEPT) {
            bubbleBuffer = bubbleBuffer.slice(bubbleBuffer.length - BUBBLE_MAX_KEPT);
          }
          if (d.status && d.status.error) {
            bubbleStatusBanner(`⚠ Bubble stream error: ${d.status.error}`, true);
          } else if (d.status && !d.status.running) {
            bubbleStatusBanner('⏳ Starting trade stream…', true);
          } else if (bubbleBuffer.length === 0) {
            // Stream IS running but nothing >= threshold has arrived yet --
            // this is a legitimate state (illiquid symbol, high threshold,
            // or just between trades), but silently hiding the banner here
            // was indistinguishable from "broken": positive confirmation
            // that the stream is genuinely live, not just quiet, so a
            // permanently-empty chart doesn't look identical to a dead one.
            bubbleStatusBanner(`🫧 Live -- waiting for trades ≥ ${bubbleThresholdVal}`, true);
          } else {
            bubbleStatusBanner(`🫧 Live -- ${bubbleBuffer.length} trade(s) in the last ${Math.round(BUBBLE_MAX_AGE_SEC/60)}m`, true);
          }
          drawBubbles();
        } catch (e) {
          bubbleStatusBanner(`⚠ Bubble poll failed: ${e}`, true);
        }
      }

      function drawBubbles() {
        const cv = bubbleCanvasEl();
        if (!cv) return;
        resizeBubbleCanvas();
        const ctx = cv.getContext('2d');
        ctx.clearRect(0, 0, cv.width, cv.height);
        if (bubbleMode !== 'on' || !bubbleBuffer.length) return;
        const maxSize = Math.max(...bubbleBuffer.map((t) => t.size), 1);
        bubbleBuffer.forEach((t) => {
          // Trade time is a server unix timestamp (real wall-clock), not
          // the chart's own bar time -- lightweight-charts' timeToCoordinate()
          // requires an EXACT match against an existing bar's time value,
          // it does not snap to the nearest bar. On an intraday
          // (gap-compressed) chart, reuse the same real->chart mapping
          // the click handlers already solved. On Daily/Weekly/Monthly,
          // there IS no bucket to look up -- a live trade happening right
          // now always belongs to the single currently-forming bar (e.g.
          // "today" on a daily chart), so map directly to that bar's own
          // time instead of the trade's raw timestamp, which will never
          // exactly equal a daily/weekly/monthly bar boundary and was
          // silently dropping every bubble on those timeframes.
          let chartTime;
          if (chartBucketSeconds) {
            const idx = realTimeToIndexMap.get(Math.floor(t.time)) ?? null;
            if (idx == null) return;  // outside the currently-loaded/visible bar range
            chartTime = currentBars[idx] ? currentBars[idx].time : null;
            if (chartTime == null) return;
          } else {
            if (!currentBars.length) return;
            chartTime = currentBars[currentBars.length - 1].time;
          }
          const x = mainChart.timeScale().timeToCoordinate(chartTime);
          const y = candleSeries.priceToCoordinate(t.price);
          if (x == null || y == null) return;
          const r = Math.max(3, Math.sqrt(t.size / maxSize) * BUBBLE_MAX_RADIUS_PX);
          const color = t.side === 'buy' ? 'rgba(34,197,94,0.55)'
            : t.side === 'sell' ? 'rgba(239,68,68,0.55)'
            : 'rgba(148,163,184,0.45)';
          ctx.beginPath();
          ctx.arc(x, y, r, 0, 2 * Math.PI);
          ctx.fillStyle = color;
          ctx.fill();
        });
      }

      async function bubbleTurnOn() {
        bubbleMode = 'on';
        root.querySelector('.bubbleBtn').classList.add('active');
        bubbleBuffer = [];
        bubbleLastSeenTs = null;
        bubbleStatusBanner('⏳ Starting trade stream…', true);
        try {
          const r = await fetch(`/realtime/api/${encodeURIComponent(symbol)}/bubbles/start`, { method: 'POST' });
          const d = await r.json();
          if (d.error) { bubbleStatusBanner(`⚠ ${d.error}`, true); }
        } catch (e) {
          bubbleStatusBanner(`⚠ Could not start trade stream: ${e}`, true);
        }
        if (bubblePollTimer) clearInterval(bubblePollTimer);
        bubblePollTimer = setInterval(bubblePoll, 2000);
        bubblePoll();
      }

      function bubbleTurnOff() {
        bubbleMode = 'off';
        root.querySelector('.bubbleBtn').classList.remove('active');
        if (bubblePollTimer) { clearInterval(bubblePollTimer); bubblePollTimer = null; }
        bubbleBuffer = [];
        clearBubbleCanvas();
        bubbleStatusBanner('', false);
      }

      root.querySelector('.bubbleBtn').addEventListener('click', () => {
        if (bubbleMode === 'on') bubbleTurnOff(); else bubbleTurnOn();
      });
      const bubbleThresholdInput = root.querySelector('.bubbleThreshold');
      if (bubbleThresholdInput) {
        bubbleThresholdVal = parseFloat(bubbleThresholdInput.value) || 10;
        bubbleThresholdInput.addEventListener('change', () => {
          bubbleThresholdVal = parseFloat(bubbleThresholdInput.value) || 10;
          bubbleBuffer = [];
          bubbleLastSeenTs = null;  // re-fetch from scratch at the new threshold
          if (bubbleMode === 'on') bubblePoll();
        });
      }

      // ---------- Chart replay ----------
      // Doesn't re-fetch anything: slices the ALREADY-loaded full dataset
      // (currentBars for candles, lastIndicatorsData for every overlay/
      // pane series) down to "everything at or before this bar", so the
      // chart genuinely can't show anything past the replay position --
      // not just scrolled out of view (which the user could scroll back
      // into), the data for later bars simply isn't in the series at all.
      // This is also why every currently-visible overlay/pane gets
      // re-sliced on every step, not just the candles: leaving an EMA
      // line drawn out to the full live dataset while candles stop at
      // bar 45 would show the "future" shape of that line, defeating the
      // whole point.
      function fullChartData() {
        return currentBars.map((d, i) => applyStrengthColors({ ...d, time: toChartTime(d.time, i) }));
      }

      function pauseLiveUpdatesForReplay() {
        applyRefreshRate(0);
        if (resyncTimeoutHandle) clearTimeout(resyncTimeoutHandle);
        resyncTimeoutHandle = null;
      }
      function resumeLiveUpdatesAfterReplay() {
        applyRefreshRate(currentRefreshMs || window.currentRefreshRateMs);
        scheduleNextResync();
      }

      function enterReplayPickMode() {
        if (replayMode === 'active') return;  // step out of an active replay via Stop, not by picking again mid-session
        replayMode = 'picking';
        root.querySelector('.replay-pick-banner').style.display = 'flex';
        root.querySelector('.replayBtn').classList.add('active');
      }
      function cancelReplayPick() {
        replayMode = 'off';
        root.querySelector('.replay-pick-banner').style.display = 'none';
        root.querySelector('.replayBtn').classList.remove('active');
      }

      function renderReplayFrame(idx) {
        const full = fullChartData();
        idx = Math.max(0, Math.min(idx, full.length - 1));
        replayIndex = idx;
        const sliced = full.slice(0, idx + 1);
        const cutoffTime = sliced[sliced.length - 1].time;

        candleSeries.setData(sliced);
        volumeSeries.setData(sliced.map(d => ({
          time: d.time, value: d.volume || 0,
          color: d.close >= d.open ? CANDLE_TIER_COLORS.bull[0] : CANDLE_TIER_COLORS.bear[0],
        })));
        volumeAvgSeries.setData(computeVolumeAvg(sliced, 20));

        const d = lastIndicatorsData || {};
        const sliceToCutoff = (arr) => mapPointsToChartTime(arr || []).filter(p => p.time <= cutoffTime);

        ['ema5', 'ema9', 'ema13', 'ema20', 'ema50', 'ema200'].forEach((k) => {
          if (overlaySeries[k]) overlaySeries[k].setData(d[k] ? sliceToCutoff(d[k]) : []);
        });
        const bands = d.avwap_bands || {};
        ['mid', 'upper', 'lower'].forEach((k) => {
          const name = 'avwap_' + k;
          if (overlaySeries[name]) overlaySeries[name].setData(bands[k] ? sliceToCutoff(bands[k]) : []);
        });
        // S/R levels are a live, current-price-relative classification
        // computed server-side (see /indicators' sr_levels) -- there's
        // no meaningful per-bar-in-history version of them without a
        // backend recompute per step, which is out of scope here.
        // Simplest correct behavior: just hide them during replay rather
        // than show today's S/R levels superimposed on historical bars.
        clearSrLines();

        if (d.rsi14) paneSeries.rsiLine.setData(sliceToCutoff(d.rsi14));
        if (d.rsi3) paneSeries.rsi3Line.setData(sliceToCutoff(d.rsi3));
        if (d.rsi_ema13) paneSeries.rsiEma13Line.setData(sliceToCutoff(d.rsi_ema13));
        if (d.rsi_ema90) paneSeries.rsiEma90Line.setData(sliceToCutoff(d.rsi_ema90));
        if (d.rsi_diff_90_colored) {
          const RSI_DIFF_COLOR_MAP = { red: '#e03030', blue: '#1d6fdb', orange: '#f97316', yellow: '#eab308', gray: '#94a3b8' };
          paneSeries.rsiDiffHist.setData(sliceToCutoff(d.rsi_diff_90_colored.map(p => ({ ...p, color: RSI_DIFF_COLOR_MAP[p.color] || '#94a3b8' }))));
        }
        if (d.macd) paneSeries.macdLine.setData(sliceToCutoff(d.macd));
        if (d.macd_signal) paneSeries.macdSignal.setData(sliceToCutoff(d.macd_signal));
        if (d.macd_hist) paneSeries.macdHist.setData(sliceToCutoff(d.macd_hist.map(p => ({ ...p, color: p.value >= 0 ? '#1d6fdb' : '#e03030' }))));
        if (d.adx14) paneSeries.adxLine.setData(sliceToCutoff(d.adx14));
        const vq = d.volquant_series || {};
        if (vq.histogram) paneSeries.vqHist.setData(sliceToCutoff(vq.histogram.map(p => ({ ...p, color: p.value >= 0 ? '#1d6fdb' : '#e03030' }))));
        if (vq.vol_amplification) paneSeries.vqLine.setData(sliceToCutoff(vq.vol_amplification));

        if (isBadgeChecked('ema-slope') && d.ema13 && d.ema50) {
          updateEmaSlopeBadge(sliceToCutoff(d.ema13), sliceToCutoff(d.ema50));
        }

        const width = Math.min(80, sliced.length);
        mainChart.timeScale().setVisibleLogicalRange({ from: idx - width + 1, to: idx + 2 });

        const posEl = root.querySelector('.replay-position');
        if (posEl) {
          const dt = new Date(currentBars[idx].time * 1000);
          posEl.textContent = `Bar ${idx + 1} / ${full.length} · ${dt.toLocaleDateString()} ${dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
        }
        if (idx >= full.length - 1) replayStopAutoPlay();  // reached the end -- auto-play has nothing left to reveal
      }

      function startReplayAt(idx) {
        replayMode = 'active';
        pauseLiveUpdatesForReplay();
        root.querySelector('.replay-pick-banner').style.display = 'none';
        root.querySelector('.replay-controls').style.display = 'flex';
        root.querySelector('.replayBtn').classList.add('active');
        renderReplayFrame(idx);
      }

      function replayStep() {
        if (replayMode !== 'active') return;
        renderReplayFrame(replayIndex + 1);
      }

      function replayStopAutoPlay() {
        if (replayPlayTimer) clearInterval(replayPlayTimer);
        replayPlayTimer = null;
        const btn = root.querySelector('.replayPlayBtn');
        if (btn) { btn.classList.remove('playing'); btn.textContent = '▶ Play'; }
      }

      function replayTogglePlay() {
        if (replayMode !== 'active') return;
        const btn = root.querySelector('.replayPlayBtn');
        if (replayPlayTimer) {
          replayStopAutoPlay();
          return;
        }
        const speed = parseInt(root.querySelector('.replaySpeedSelect').value, 10) || 700;
        btn.classList.add('playing');
        btn.textContent = '⏸ Pause';
        replayPlayTimer = setInterval(replayStep, speed);
      }

      function stopReplay() {
        replayStopAutoPlay();
        replayMode = 'off';
        root.querySelector('.replay-controls').style.display = 'none';
        root.querySelector('.replay-pick-banner').style.display = 'none';
        root.querySelector('.replayBtn').classList.remove('active');
        resumeLiveUpdatesAfterReplay();
        refreshAll();  // genuine fresh fetch+render restores full live state, rather
                        // than trying to reconstruct it from whatever was in memory
      }

      // Bucket duration in seconds per timeframe -- used to figure out
      // whether a new price tick belongs to the currently-forming candle
      // (update in place) or starts a new one (append), without needing a
      // full historical re-fetch on every fast refresh tick.
      const BUCKET_SECONDS = {
        '1m': 60, '3m': 180, '5m': 300, '15m': 900, '1h': 3600, '2h': 7200,
        '4h': 14400, '1d': 86400, '1w': 604800, '1M': 2592000,
      };
      let lastBar = null;  // the most recent candle, kept in sync by both loadBars() and updateLatestPrice()
      let currentBars = [];  // this window's own full bar array (its own timeframe/granularity, REAL
                              // timestamps always) -- used for setTimePosition()'s nearest-bar search AND
                              // as the source of truth for converting a plotted (possibly synthetic) chart
                              // time back to a real timestamp, since candleSeries itself only stores
                              // whatever time value was actually plotted (see chartTimeBase below).

      // --- Gap-compressed intraday time axis --------------------------
      // Lightweight Charts has no Plotly-style rangebreaks: it places
      // bars by real elapsed time, so a real overnight/weekend gap (or
      // the pre/post-market span when Extended Hours is off) renders as
      // a big dead flat stretch. Fix: for intraday timeframes, plot bars
      // at a synthetic, perfectly-sequential time (chartTimeBase + i *
      // chartBucketSeconds) instead of their real timestamp, so
      // consecutive bars are always back-to-back on screen regardless of
      // the real time between them. currentBars keeps the REAL
      // timestamps (index-aligned 1:1 with what's plotted) so anything
      // that needs the actual date/time -- click-to-sync, axis label
      // formatting, tooltips -- can recover it via index. Daily+
      // timeframes are left on real time (chartBucketSeconds = null):
      // aggregated bars don't have this problem, and the coarser
      // granularity means occasional real gaps (holidays) are too small
      // to visually matter.
      let chartTimeBase = 0;
      let chartBucketSeconds = null; // null = use real time as-is (daily+ timeframes)
      let lastBarChartTime = 0;      // synthetic time of the currently-forming live bar

      function isIntradayTf(tf) {
        return ['1m', '3m', '5m', '15m', '1h', '2h', '4h'].includes(tf);
      }
      function toChartTime(realTime, index) {
        return chartBucketSeconds ? (chartTimeBase + index * chartBucketSeconds) : realTime;
      }
      // Inverse of toChartTime -- given a time value actually plotted on
      // this chart, recover the bar index it corresponds to (or null if
      // this chart isn't gap-compressed, i.e. daily+ timeframe, where
      // the plotted time already IS the real time and no inversion is
      // needed by callers -- they should just use param.time directly).
      function chartTimeToIndex(chartTime) {
        if (!chartBucketSeconds) return null;
        return Math.round((chartTime - chartTimeBase) / chartBucketSeconds);
      }

      // Indicator series (EMA/RSI/MACD/etc, from loadIndicators() below)
      // come from a SEPARATE backend call than the candles and are
      // stamped with REAL timestamps -- they know nothing about this
      // window's synthetic gap-compressed time axis. Plotting them with
      // their real time directly (as before) meant they landed at the
      // wrong x-position relative to the candles whenever compression is
      // active: recent points would appear shifted to the right of where
      // their actual candle is, looking like they were "drawn in the
      // future" or floating past the last real candle. Fix: remap every
      // point through the same real-time -> index -> synthetic-time path
      // as the candles, via realTimeToIndexMap (built in loadBars() below
      // from the exact same currentBars each point needs to align to). A
      // point whose real time has no matching bar in currentBars (should
      // be rare -- would mean the indicators endpoint and the bars
      // endpoint disagree on the underlying bar sequence) is dropped
      // rather than guessed at, since a wrong position is worse than a
      // missing one point.
      let realTimeToIndexMap = new Map();
      function mapPointsToChartTime(points) {
        // Defensive filter: a single null/undefined/non-finite `value`
        // anywhere in an indicator series' response was propagating
        // straight into a .setData() call and throwing ("Value is
        // null") -- and since the two functions that call setData with
        // this data (loadIndicators, the replay step handler) don't all
        // have their own try/catch around every individual call, one bad
        // point was silently killing every setData() call AFTER it in
        // that same pass, not just the one series that actually had bad
        // data. This is believed to be the real mechanism behind "RSI
        // not computed" / "EMA sometimes missing" -- both functions
        // route every series through this one shared function before
        // ever reaching setData, so fixing it here covers every call
        // site at once instead of patching ~30 individually.
        const isFiniteNum = (v) => typeof v === 'number' && Number.isFinite(v);
        const clean = (points || []).filter((p) => p && p.time != null && isFiniteNum(p.value));
        if (!chartBucketSeconds) return clean;
        const out = [];
        for (const p of clean) {
          const idx = realTimeToIndexMap.get(p.time);
          if (idx === undefined) continue;
          out.push({ ...p, time: toChartTime(p.time, idx) });
        }
        return out;
      }

      function showFetchError(msg) {
        const el = root.querySelector('.fetch-error-banner');
        if (msg) {
          el.textContent = `⚠ ${symbol} (${timeframe}): ${msg}`;
          el.style.display = 'block';
        } else {
          el.style.display = 'none';
        }
      }

      async function loadBars(resetView = false) {
        const ehParam = extendedHours ? '1' : '0';
        // Wrapped in try/catch now -- previously any network failure or
        // non-JSON response (a Flask 500 error page, for instance,
        // rather than the graceful {status:'no_bars',...} JSON this code
        // already handled below) would reject with NOTHING catching it,
        // meaning nothing ever failed loudly: no error banner, chart
        // just silently never updates. This is very likely what was
        // actually happening for a futures symbol whose resolution or
        // streaming subscription fails in a way that doesn't cleanly
        // produce the graceful no_bars JSON response.
        try {
          const r = await fetch(`/realtime/api/${encodeSymbolForUrl(symbol)}/bars?timeframe=${timeframe}&extended_hours=${ehParam}`);
          let data;
          try {
            data = await r.json();
          } catch (parseErr) {
            showFetchError(`Server returned an unreadable response (HTTP ${r.status}) -- check server logs`);
            return;
          }
          if (Array.isArray(data)) {
            showFetchError(null);
            currentBars = data; // real timestamps, always -- source of truth for anything date/time-related
            realTimeToIndexMap = new Map(data.map((d, i) => [d.time, i]));

            chartBucketSeconds = isIntradayTf(timeframe) ? (BUCKET_SECONDS[timeframe] || 60) : null;
            chartTimeBase = data.length ? data[0].time : 0;
            let chartData = data.map((d, i) => applyStrengthColors({ ...d, time: toChartTime(d.time, i) }));

            // Defensive filter: lightweight-charts throws ("Value is
            // null") and aborts the ENTIRE setData call -- including
            // every bar after the bad one -- the instant it hits a
            // single bar with a null/undefined/non-finite OHLC value.
            // Since candleSeries/volumeSeries/volumeAvgSeries all get set
            // sequentially in this same block, one malformed bar
            // anywhere in the response was silently taking down candles
            // AND volume together with no partial recovery -- confirmed
            // against a real "Fetch failed: Value is null" report where
            // neither ever rendered. Dropping just the bad bars (logged,
            // not silent) lets the rest of a mostly-good response still
            // render instead of discarding the whole fetch over one
            // point.
            const isFiniteNum = (v) => typeof v === 'number' && Number.isFinite(v);
            const badCount = chartData.filter((d) => !(isFiniteNum(d.open) && isFiniteNum(d.high) && isFiniteNum(d.low) && isFiniteNum(d.close))).length;
            if (badCount > 0) {
              console.warn(`[realtime] ${symbol} (${timeframe}): dropped ${badCount} bar(s) with null/invalid OHLC values before rendering`);
              chartData = chartData.filter((d) => isFiniteNum(d.open) && isFiniteNum(d.high) && isFiniteNum(d.low) && isFiniteNum(d.close));
            }
            if (!chartData.length) {
              showFetchError('All returned bars had invalid OHLC values -- check server-side data for this symbol/timeframe');
              return;
            }

            candleSeries.setData(chartData);
            // Reverted from the strength-tier candle colors: simple 2-color
            // blue/red (matching direction only, same as the candle body's
            // BASE tier-0 colors) reads more clearly at a glance than the
            // full tiered palette repeated on both panes at once.
            volumeSeries.setData(chartData.map(d => ({
              time: d.time, value: d.volume || 0,
              color: d.close >= d.open ? CANDLE_TIER_COLORS.bull[0] : CANDLE_TIER_COLORS.bear[0],
            })));
            volumeAvgSeries.setData(computeVolumeAvg(chartData, 20));
            lastBar = data.length ? { ...data[data.length - 1] } : null;
            lastBarChartTime = chartData.length ? chartData[chartData.length - 1].time : 0;

            // Fix for "only 1-2 candles visible, have to drag right" on
            // symbol/timeframe change: Lightweight Charts' setData()
            // does NOT change the visible range on its own -- if the
            // PREVIOUS symbol/timeframe had you zoomed into a specific
            // logical bar-index window (e.g. bars 280-300 of a
            // 300-bar series), and the NEW data has a completely
            // different bar count/density, that same logical range can
            // land almost entirely past the end of the new data,
            // showing only whatever sliver of real bars happens to
            // still fall inside it. resetView is true only from
            // refreshAll() (symbol change, timeframe change, template
            // load, initial widget setup) -- NOT from the periodic
            // full-resync timer further below, which intentionally
            // preserves wherever the user has manually panned/zoomed to
            // on an unchanged symbol/timeframe.
            if (resetView && chartData.length) {
              const lastIdx = chartData.length - 1;
              const width = Math.min(80, chartData.length);
              mainChart.timeScale().setVisibleLogicalRange({ from: lastIdx - width + 1, to: lastIdx + 3 });
            }
          } else if (data && data.status === 'no_bars') {
            showFetchError(data.error || 'No data returned (check server console for details)');
          } else {
            showFetchError('Unexpected response from server -- check server logs');
          }
        } catch (fetchErr) {
          showFetchError(`Fetch failed: ${fetchErr.message || fetchErr}`);
        }
      }

      // Fast, cheap path: fetch just the current price and update the
      // last candle in place (or start a new one if enough time has
      // passed), instead of re-running the full historical backfill on
      // every refresh tick. This is what makes frequent refresh actually
      // cheap -- the expensive full fetch only happens on symbol/timeframe
      // change and the slower full-resync interval.
      //
      // lastBar tracks REAL time throughout (unchanged logic from
      // before, comparing real bucketStart against lastBar.time) so
      // "is this the same bar still forming, or has a new bar started"
      // is unaffected by gap compression. lastBarChartTime is the
      // SEPARATE synthetic position actually pushed to the chart --
      // advances by exactly one bucket step when a new bar starts,
      // keeping the live bar perfectly back-to-back with history,
      // exactly like every other plotted bar.
      async function updateLatestPrice() {
        if (!lastBar) return;  // no baseline yet -- wait for the next full loadBars()
        try {
          const r = await fetch(`/realtime/api/${encodeSymbolForUrl(symbol)}/price`);
          let d;
          try {
            d = await r.json();
          } catch (parseErr) {
            showFetchError(`Price fetch: unreadable response (HTTP ${r.status})`);
            return;
          }
          if (d.status !== 'live' || d.price == null) {
            // Previously silent here regardless of WHY -- a genuine
            // resolution/streaming problem (status: 'error', with a real
            // message from the backend) looked identical to a normal
            // "no quote this instant" blip. Surface the former; leave
            // the latter alone rather than showing a persistent banner
            // for what may just be a quiet moment for this symbol.
            if (d.status === 'error') {
              showFetchError(`Price fetch failed: ${d.error || 'unknown error'}`);
            }
            return;
          }
          showFetchError(null);  // clears a previous price-fetch error once it recovers
          const price = d.price;
          const bucketSeconds = BUCKET_SECONDS[timeframe] || 60;
          const bucketStart = Math.floor(Date.now() / 1000 / bucketSeconds) * bucketSeconds;

          if (bucketStart === lastBar.time) {
            lastBar = {
              ...lastBar,
              high: Math.max(lastBar.high, price),
              low: Math.min(lastBar.low, price),
              close: price,
            };
          } else if (bucketStart > lastBar.time) {
            lastBar = { time: bucketStart, open: lastBar.close, high: price, low: price, close: price, volume: 0 };
            if (chartBucketSeconds) lastBarChartTime += chartBucketSeconds;
            else lastBarChartTime = bucketStart;
          } else {
            return;  // stale/out-of-order tick -- ignore rather than corrupt the series
          }
          const coloredBar = applyStrengthColors({ ...lastBar, time: lastBarChartTime });
          candleSeries.update(coloredBar);
          volumeSeries.update({
            time: lastBarChartTime, value: lastBar.volume || 0,
            color: lastBar.close >= lastBar.open ? CANDLE_TIER_COLORS.bull[0] : CANDLE_TIER_COLORS.bear[0],
          });
        } catch (fetchErr) {
          showFetchError(`Price fetch failed: ${fetchErr.message || fetchErr}`);
        }
      }

      async function loadIndicators() {
        try {
        // Only request indicator series that are actually toggled on --
        // trims response payload to what's in use. (The DXLink candle
        // fetch itself is already deduped server-side regardless of this;
        // this only reduces JSON payload size / redundant computation.)
        const want = [];
        ['ema5', 'ema9', 'ema13', 'ema20', 'ema50', 'ema200'].forEach((k) => { if (isOvChecked(k)) want.push(k); });
        // EMA13/50 Slope badge needs both series regardless of whether
        // their own overlay LINES are checked -- the badge is a separate,
        // independent toggle from the overlay checkboxes it happens to
        // derive its data from.
        if (isBadgeChecked('ema-slope')) {
          if (!want.includes('ema13')) want.push('ema13');
          if (!want.includes('ema50')) want.push('ema50');
        }
        if (isOvChecked('avwap')) want.push('avwap_bands');
        if (isOvChecked('sr')) want.push('sr_levels');
        if (isOvChecked('vp')) want.push('vp_levels');
        if (isPaneChecked('rsi')) want.push('rsi3', 'rsi14', 'rsi_ema13', 'rsi_ema90', 'rsi_diff_90_colored');
        if (isPaneChecked('macd')) want.push('macd', 'macd_signal', 'macd_hist');
        if (isPaneChecked('adx')) want.push('adx14');
        if (isPaneChecked('volquant')) want.push('volquant_series');
        const wantParam = want.length ? `&want=${want.join(',')}` : '';
        const srCount = root.querySelector('.srCountInput');
        const srCountParam = `&sr_count=${srCount ? (parseInt(srCount.value, 10) || 3) : 3}`;

        const r = await fetch(`/realtime/api/${encodeSymbolForUrl(symbol)}/indicators?timeframe=${timeframe}${wantParam}${srCountParam}`);
        const d = await r.json();
        if (d.status) return;
        lastIndicatorsData = d;

        // Every series below comes from a call keyed on REAL time, not
        // this window's own (possibly gap-compressed) chart time --
        // mapPointsToChartTime() re-aligns each to the exact same x
        // position as its actual candle. See that function's comment
        // above for why this was needed (previously-misaligned EMAs).
        ['ema5', 'ema9', 'ema13', 'ema20', 'ema50', 'ema200'].forEach((k) => {
          if (isOvChecked(k) && d[k] && d[k].length) {
            ensureOverlay(k, OVERLAY_COLORS[k]).setData(mapPointsToChartTime(d[k]));
          } else if (overlaySeries[k]) {
            overlaySeries[k].setData([]);
          }
        });

        updateEmaSlopeBadge(d.ema13, d.ema50);

        drawSrLines(d.sr_levels);
        drawVpLines(d.vp_levels);

        const avwapOn = isOvChecked('avwap');
        const bands = d.avwap_bands || {};
        ['mid', 'upper', 'lower'].forEach((k) => {
          const name = 'avwap_' + k;
          if (avwapOn && bands[k] && bands[k].length) {
            ensureOverlay(name, OVERLAY_COLORS[name], k === 'mid' ? 1.5 : 1).setData(mapPointsToChartTime(bands[k]));
          } else if (overlaySeries[name]) {
            overlaySeries[name].setData([]);
          }
        });

        // Each RSI-pane series independently toggleable via [data-rsisub]
        // checkboxes -- when off, clear that series rather than leaving
        // stale data plotted. Colors and the RSIDiff90 ladder now match
        // the reference Pine Script exactly (see
        // TechnicalSeriesEngine._rsi_diff_90_colored's docstring for the
        // precise condition-by-condition mapping) rather than the
        // earlier magnitude-only 3-tier approximation.
        if (isRsiSubChecked('rsi14') && d.rsi14) paneSeries.rsiLine.setData(mapPointsToChartTime(d.rsi14));
        else paneSeries.rsiLine.setData([]);
        if (isRsiSubChecked('rsi3') && d.rsi3) paneSeries.rsi3Line.setData(mapPointsToChartTime(d.rsi3));
        else paneSeries.rsi3Line.setData([]);
        if (isRsiSubChecked('rsi_ema13') && d.rsi_ema13) paneSeries.rsiEma13Line.setData(mapPointsToChartTime(d.rsi_ema13));
        else paneSeries.rsiEma13Line.setData([]);
        if (isRsiSubChecked('rsi_ema90') && d.rsi_ema90) paneSeries.rsiEma90Line.setData(mapPointsToChartTime(d.rsi_ema90));
        else paneSeries.rsiEma90Line.setData([]);
        const RSI_DIFF_COLOR_MAP = { red: '#e03030', blue: '#1d6fdb', orange: '#f97316', yellow: '#eab308', gray: '#94a3b8' };
        if (isRsiSubChecked('rsi_diff_90') && d.rsi_diff_90_colored) {
          paneSeries.rsiDiffHist.setData(mapPointsToChartTime(d.rsi_diff_90_colored.map(p => ({
            time: p.time, value: p.value, color: RSI_DIFF_COLOR_MAP[p.color] || '#94a3b8',
          }))));
        } else {
          paneSeries.rsiDiffHist.setData([]);
        }
        if (d.macd) paneSeries.macdLine.setData(mapPointsToChartTime(d.macd));
        if (d.macd_signal) paneSeries.macdSignal.setData(mapPointsToChartTime(d.macd_signal));
        if (d.macd_hist) paneSeries.macdHist.setData(mapPointsToChartTime(d.macd_hist.map(p => ({ time: p.time, value: p.value, color: p.value >= 0 ? '#1d6fdb' : '#e03030' }))));
        if (d.adx14) paneSeries.adxLine.setData(mapPointsToChartTime(d.adx14));

        const vq = d.volquant_series || {};
        if (vq.histogram) paneSeries.vqHist.setData(mapPointsToChartTime(vq.histogram.map(p => ({ time: p.time, value: p.value, color: p.value >= 0 ? '#1d6fdb' : '#e03030' }))));
        if (vq.vol_amplification) paneSeries.vqLine.setData(mapPointsToChartTime(vq.vol_amplification));
        } catch (indErr) {
          // Indicators are supplementary overlays, not the critical-path
          // candle/volume data -- a soft console warning here rather
          // than a blocking error banner, but critically this now
          // actually EXISTS: previously this whole function had no
          // try/catch at all, so any single throw (a bad point in one
          // series slipping past the mapPointsToChartTime sanitization
          // above, a malformed response, etc.) silently killed every
          // setData() call after it with zero visibility -- exactly
          // "sometimes RSI/EMA just don't show up," diagnosable only by
          // opening devtools, which is now at least logged here instead.
          console.warn(`[realtime] ${symbol} (${timeframe}): loadIndicators failed partway through -- ${indErr.message || indErr}`);
        }
      }

      async function refreshAll() {
        const indicator = root.querySelector('.loading-indicator');
        indicator.style.display = 'inline';
        try {
          // MUST be sequential, not Promise.all -- loadIndicators() maps
          // every point through currentBars/realTimeToIndexMap/
          // chartTimeBase, which loadBars() is the one that rebuilds.
          // Running them concurrently raced: if loadIndicators()'s
          // response happened to resolve before loadBars() finished
          // updating that shared state (plausible -- indicators is
          // typically a smaller/faster response), it would map every
          // point against the PREVIOUS timeframe/symbol's stale bar
          // positions instead of the new ones. This is believed to be
          // the actual mechanism behind "EMA/RSI out of range" and
          // "chart out of range on refetch or changing timeframe" --
          // confirmed matches the exact visual signature reported: an
          // overlay line spread across a wide, stale range while fresh
          // candles cluster correctly at the new position.
          await loadBars(true);
          await loadIndicators();
        } finally {
          indicator.style.display = 'none';
        }
      }

      // Only the bottom-most VISIBLE chart (main chart, or whichever
      // pane is currently the last one shown) displays time-axis labels
      // -- matches the reference chart's single shared timeline instead
      // of every panel repeating its own. Purely a timeScale.visible
      // toggle (labels/ticks only); the existing cross-chart logical
      // range sync is unaffected since that's independent of this.
      const PANE_ORDER = ['rsi', 'macd', 'adx', 'volquant'];
      function updateSharedTimeAxis() {
        let lastVisible = mainChart;
        PANE_ORDER.forEach((name) => {
          if (isPaneChecked(name)) lastVisible = paneCharts[name].chart;
        });
        mainChart.applyOptions({ timeScale: { visible: mainChart === lastVisible } });
        PANE_ORDER.forEach((name) => {
          const c = paneCharts[name].chart;
          c.applyOptions({ timeScale: { visible: c === lastVisible } });
        });
      }

      function applyPaneVisibility() {
        ['rsi', 'macd', 'adx', 'volquant'].forEach((name) => {
          const visible = isPaneChecked(name);
          paneCharts[name].el.classList.toggle('visible', visible);
          const handle = root.querySelector(`[data-resize-target="${name}"]`);
          if (handle) handle.style.display = visible ? 'block' : 'none';
        });
        updateSharedTimeAxis();
        // Toggling display:none/block just above changes how much height
        // .main-chart-wrap's flex:1 actually resolves to, but the
        // browser hasn't necessarily finished reflowing by the time the
        // very next line runs synchronously -- resizeAll() would then
        // read a STALE mainEl.clientHeight (from before the pane was
        // added/removed), so the chart wouldn't actually grow/shrink to
        // fill the freed/consumed space until some LATER unrelated
        // resize happened to trigger a correct read. Same fix already
        // applied to updateGridLayout() for the same underlying reason.
        requestAnimationFrame(resizeAll);
      }

      function resizeAll() {
        // Chart sizing itself is now handled natively by autoSize:true
        // (set on every chart via chartOpts above) -- this function's
        // only remaining job is redrawing the rvp/bubble canvas overlays,
        // which still can't track coordinate changes on their own since
        // they're plain <canvas> elements, not chart-native primitives.
        //
        // Wrapped in requestAnimationFrame: this function is called from
        // a ResizeObserver on mainChartWrap, which is a SEPARATE observer
        // instance from the one autoSize manages internally on mainEl --
        // two independent observers watching related-but-different
        // elements have no guaranteed relative firing order, so without
        // this the overlay could still redraw using a stale pre-resize
        // coordinate mapping if it runs before autoSize's own internal
        // resize completes. requestAnimationFrame defers to the next
        // paint, by which point the browser has settled all resize
        // observer callbacks queued in this cycle, including autoSize's.
        requestAnimationFrame(() => {
          applyPaneHeightFractions();
          if (rvpData) drawRangeProfile();
          if (cvdData) drawCvdProfile();
          if (bubbleMode === 'on') drawBubbles();
        });
      }

      function setChartTheme(theme) {
        const c = chartThemeColors(theme);
        const opts = { layout: { background: { color: c.bg }, textColor: c.text }, grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } } };
        mainChart.applyOptions(opts);
        Object.values(paneCharts).forEach(({ chart }) => chart.applyOptions(opts));
      }

      function setIntervalButtons() {
        root.querySelectorAll('.interval-row button').forEach((b) => {
          b.classList.toggle('active', b.dataset.tf === timeframe);
        });
      }

      function setSymbol(newSymbol) {
        symbol = String(newSymbol || '').trim().toUpperCase();
        if (!symbol) return;
        root.querySelector('.symLabel').textContent = symbol;
        root.querySelector('.symInput').value = symbol;
        // Clear any error from the PREVIOUS symbol immediately, rather
        // than leaving it showing (now misleadingly) until/unless the
        // new fetch happens to also fail with its own message. If the
        // new fetch fails too, loadBars()/updateLatestPrice() below will
        // show its own (correct, current) error.
        showFetchError(null);
        // Range/CVD profile bar indices and the bubble stream are all
        // tied to the OLD symbol -- stale/meaningless (or actively
        // wrong) once the symbol changes, so reset rather than let them
        // linger.
        rvpClear();
        cvdClear();
        if (bubbleMode === 'on') { bubbleTurnOff(); bubbleTurnOn(); }
        refreshAll();
      }

      function setTimeframe(tf) {
        timeframe = tf;
        setIntervalButtons();
        refreshAll();
      }

      // Called only from user-driven controls (not from setSymbol/setTimeframe
      // directly, to avoid re-propagating when this window is itself the
      // target of another window's sync broadcast).
      function userSetSymbol(newSymbol) {
        setSymbol(newSymbol);
        if (syncSymbol) {
          Object.entries(widgets).forEach(([wId, w]) => { if (wId !== id) w.setSymbol(newSymbol); });
        }
        maybeAutoFillAtmOptions(id, newSymbol, timeframe);
        saveLayout();
      }
      function userSetTimeframe(tf) {
        setTimeframe(tf);
        if (syncInterval) {
          Object.entries(widgets).forEach(([wId, w]) => { if (wId !== id) w.setTimeframe(tf); });
        }
        saveLayout();
      }

      // ---------- Click-to-position time sync ----------
      // Finds the bar in THIS window's own bars (its own timeframe/
      // granularity, which can differ entirely from whatever window the
      // click originated on -- e.g. Daily vs 15m) whose time is closest
      // to targetTime, then repositions the visible range to be centered
      // on that bar while preserving this window's current zoom (bar
      // count on screen), rather than resetting to some default zoom.
      // Does NOT change symbol or timeframe -- purely a time-axis
      // position, so it composes independently of setSymbol/setTimeframe.
      function setTimePosition(targetTime) {
        if (!currentBars.length || targetTime == null) return;
        // currentBars is time-ascending (oldest first, per how loadBars()
        // receives it from the backend) -- binary search for the closest
        // bar rather than a linear scan, since this runs on every click
        // broadcast to every other window and history can be thousands
        // of bars for daily/weekly timeframes.
        let lo = 0, hi = currentBars.length - 1;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (currentBars[mid].time < targetTime) lo = mid + 1; else hi = mid;
        }
        // lo is now the first bar with time >= targetTime (or the last
        // bar if targetTime is beyond all of them); compare against the
        // bar just before it too, since that may actually be closer.
        let nearestIdx = lo;
        if (lo > 0 && Math.abs(currentBars[lo - 1].time - targetTime) <= Math.abs(currentBars[lo].time - targetTime)) {
          nearestIdx = lo - 1;
        }

        try {
          const ts = mainChart.timeScale();
          const currentRange = ts.getVisibleLogicalRange();
          // Preserve current zoom width; if no valid range yet (chart
          // just created), fall back to a reasonable default window.
          const width = (currentRange && isFinite(currentRange.to - currentRange.from))
            ? (currentRange.to - currentRange.from) : 60;
          const half = width / 2;
          ts.setVisibleLogicalRange({ from: nearestIdx - half, to: nearestIdx + half });
        } catch (e) { /* chart not ready / mid-teardown -- non-fatal */ }
      }

      // Called only from this window's own click handler (not from
      // setTimePosition directly), same "avoid re-propagating a broadcast
      // back out" guard userSetSymbol/userSetTimeframe already use above.
      // Click-to-position sync fires whenever EITHER Sync Symbol or Sync
      // Interval is on -- it's a time-axis link, not tied specifically to
      // symbol identity or interval identity, so either existing sync
      // scope enables it. With both off, clicking stays purely local.
      function userSetTimePosition(targetTime) {
        if (!syncSymbol && !syncInterval) return;
        Object.entries(widgets).forEach(([wId, w]) => { if (wId !== id) w.setTimePosition(targetTime); });
      }

      // ---------- Template save/load ----------
      function getTemplateConfig() {
        const overlays = {};
        root.querySelectorAll('[data-ov]').forEach((cb) => { overlays[cb.dataset.ov] = cb.checked; });
        const panes = {};
        root.querySelectorAll('[data-pane]').forEach((cb) => { panes[cb.dataset.pane] = cb.checked; });
        // Fractions of total content height, not raw pixels -- see
        // paneHeightFractions declaration above for why. Deliberately no
        // "main" entry: the main chart is always flex:1 + autoSize, never
        // an explicit saved height (a previous version of this function
        // saved mainEl.clientHeight and restored it as an inline style,
        // which permanently pinned the main chart to whatever pixel size
        // it happened to be at save time -- overriding both the
        // responsive flex layout and autoSize on every future load,
        // regardless of the window's actual current size).
        return { timeframe, overlays, panes, heights: { ...paneHeightFractions }, extendedHours };
      }

      function getState() {
        return { symbol, timeframe, templateConfig: getTemplateConfig() };
      }

      function applyTemplateConfig(cfg) {
        if (!cfg) return;
        if (cfg.timeframe) { timeframe = cfg.timeframe; setIntervalButtons(); }
        if (cfg.overlays) {
          Object.entries(cfg.overlays).forEach(([k, v]) => {
            const cb = root.querySelector(`[data-ov="${k}"]`);
            if (cb) cb.checked = !!v;
          });
        }
        if (cfg.panes) {
          Object.entries(cfg.panes).forEach(([k, v]) => {
            const cb = root.querySelector(`[data-pane="${k}"]`);
            if (cb) cb.checked = !!v;
          });
        }
        if (typeof cfg.extendedHours === 'boolean') {
          extendedHours = cfg.extendedHours;
          root.querySelector('.extendedHoursCb').checked = extendedHours;
        }
        applyPaneVisibility();
        if (cfg.heights) {
          // cfg.heights is fractions now (see getTemplateConfig) -- older
          // saved layouts from before this fix may still have raw pixel
          // values here (typically > 1), which would misapply as a
          // fraction; guard by only accepting values in a sane 0-1 range
          // and falling back to the current defaults otherwise, rather
          // than silently producing a broken (near-zero or absurdly
          // tall) pane height from stale data.
          Object.entries(cfg.heights).forEach(([name, v]) => {
            if (name !== 'main' && typeof v === 'number' && v > 0 && v <= 1) {
              paneHeightFractions[name] = v;
            }
          });
          requestAnimationFrame(applyPaneHeightFractions);
        }
        refreshAll();
      }

      async function loadTemplateList() {
        const sel = root.querySelector('.templateSelect');
        const r = await fetch('/realtime/templates');
        const d = await r.json();
        sel.innerHTML = '<option value="">Load template…</option>' +
          (d.templates || []).map(t => `<option value="${t}">${t}</option>`).join('');
      }

      // ---------- Wire up controls ----------
      root.querySelector('.symLabel').textContent = symbol;
      const symInput = root.querySelector('.symInput');
      symInput.value = symbol;
      root.querySelector('.goBtn').addEventListener('click', () => userSetSymbol(symInput.value));
      symInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') userSetSymbol(symInput.value); });

      root.querySelectorAll('.interval-row button').forEach((b) => {
        b.addEventListener('click', () => userSetTimeframe(b.dataset.tf));
      });
      setIntervalButtons();

      root.querySelectorAll('[data-ov]').forEach((cb) => {
        cb.addEventListener('change', () => {
          loadIndicators();
          saveLayout();
        });
      });
      const srCountInput = root.querySelector('.srCountInput');
      if (srCountInput) {
        srCountInput.addEventListener('change', () => { loadIndicators(); saveLayout(); });
      }
      root.querySelectorAll('[data-rsisub]').forEach((cb) => {
        cb.addEventListener('change', () => { loadIndicators(); saveLayout(); });
      });
      root.querySelectorAll('[data-badge]').forEach((cb) => {
        cb.addEventListener('change', () => { loadIndicators(); saveLayout(); });
      });
      root.querySelectorAll('[data-pane]').forEach((cb) => {
        cb.addEventListener('change', () => { applyPaneVisibility(); saveLayout(); });
      });

      root.querySelector('.extendedHoursCb').addEventListener('change', (e) => {
        extendedHours = e.target.checked;
        loadBars();
        saveLayout();
      });

      const indBtn = root.querySelector('.indBtn');
      const indPanel = root.querySelector('.ind-popover-panel');
      indBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        indPanel.classList.toggle('open');
      });
      document.addEventListener('click', (e) => {
        if (!root.contains(e.target)) indPanel.classList.remove('open');
      });

      function makeResizable(targetName, targetEl, chartObj) {
        const handle = root.querySelector(`[data-resize-target="${targetName}"]`);
        if (!handle) return;
        let startY = 0, startHeight = 0;
        const onMove = (e) => {
          if (targetName === 'main') return;  // never pixel-pin the main chart -- see makeResizable('main', ...) call site below
          const dy = e.clientY - startY;
          const newHeight = Math.max(60, startHeight + dy);
          targetEl.style.height = newHeight + 'px';
          // No explicit applyOptions({height}) here -- autoSize (set in
          // chartOpts) tracks the container's CSS height automatically;
          // calling both would just have autoSize immediately override
          // the manual value on its next observer tick anyway.
        };
        const onUp = () => {
          handle.classList.remove('dragging');
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
          // Convert the pixel height the user just dragged to into a
          // fraction of CURRENT total content height, so it's preserved
          // correctly on future window resizes instead of staying frozen
          // in pixels while everything else scales.
          const total = totalContentHeight();
          if (total > 0) paneHeightFractions[targetName] = targetEl.clientHeight / total;
          saveLayout();
        };
        handle.addEventListener('mousedown', (e) => {
          handle.classList.add('dragging');
          startY = e.clientY;
          startHeight = targetEl.clientHeight;
          document.addEventListener('mousemove', onMove);
          document.addEventListener('mouseup', onUp);
          e.preventDefault();
        });
      }
      makeResizable('main', mainEl, mainChart);
      ['rsi', 'macd', 'adx', 'volquant'].forEach((name) => {
        makeResizable(name, paneCharts[name].el, paneCharts[name].chart);
      });

      root.querySelector('.close-btn').addEventListener('click', () => removeWindow(id));
      const moveSelect = root.querySelector('.moveToSlotSelect');
      if (moveSelect) {
        moveSelect.addEventListener('change', () => moveWindowToSlot(id, parseInt(moveSelect.value, 10)));
      }

      // ---------- Right-click price alert ----------
      function closeAlertMenu() {
        const existing = document.querySelector('.alert-menu');
        if (existing) existing.remove();
      }

      async function submitPriceAlert(price, operator) {
        closeAlertMenu();
        try {
          const r = await fetch('/realtime/alerts', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbol, price_value: price, price_operator: operator, timeframe }),
          });
          const d = await r.json();
          if (d.ok) {
            alert(`Alert created: ${d.name}`);
          } else {
            alert(`Failed to create alert: ${d.error || 'unknown error'}`);
          }
        } catch (e) {
          alert('Failed to create alert: ' + e);
        }
      }

      mainEl.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        closeAlertMenu();
        const rect = mainEl.getBoundingClientRect();
        const y = e.clientY - rect.top;
        let price;
        try {
          price = candleSeries.coordinateToPrice(y);
        } catch (err) {
          price = null;
        }
        if (price == null) return;
        price = Math.round(price * 100) / 100;

        const menu = document.createElement('div');
        menu.className = 'alert-menu';
        menu.style.left = e.clientX + 'px';
        menu.style.top = e.clientY + 'px';
        menu.innerHTML = `
          <div class="price-line">${symbol} @ $${price}</div>
          <button data-op=">=">🔔 Alert when price ≥ $${price}</button>
          <button data-op="<=">🔔 Alert when price ≤ $${price}</button>
        `;
        document.body.appendChild(menu);
        menu.querySelectorAll('button').forEach((b) => {
          b.addEventListener('click', () => submitPriceAlert(price, b.dataset.op));
        });
        setTimeout(() => {
          document.addEventListener('click', closeAlertMenu, { once: true });
        }, 0);
      });

      root.querySelector('.saveTemplateBtn').addEventListener('click', async () => {
        const name = prompt('Save this window indicators + timeframe as a template named:');
        if (!name) return;
        await fetch('/realtime/templates', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, config: getTemplateConfig() }),
        });
        loadTemplateList();
      });

      root.querySelector('.templateSelect').addEventListener('change', async (e) => {
        const name = e.target.value;
        if (!name) return;
        const r = await fetch(`/realtime/templates/${encodeURIComponent(name)}`);
        const d = await r.json();
        if (d.ok) { applyTemplateConfig(d.config); saveLayout(); }
      });

      window.addEventListener('resize', resizeAll);

      applyPaneVisibility();
      loadTemplateList();
      refreshAll();

      // Two-tier refresh: the fast, user-configured rate now only does the
      // cheap price-tick update (updateLatestPrice) instead of re-running
      // the full historical backfill + all indicators on every tick -- that
      // full-resync work happens on its own, much slower, fixed interval
      // instead, since indicators/AVWAP/etc. don't need to be that fresh
      // and re-fetching them constantly was the actual cause of "impossible
      // to use" at faster refresh rates.
      //
      // JITTER: with multiple windows open, plain setInterval() on all of
      // them fires in lockstep (they were all created within the same
      // second), causing a "thundering herd" -- every window's full-resync
      // landing on the exact same tick, all hitting the server at once.
      // This is what actually produced the waitress "Task queue depth"
      // warning burst. Fixed by using a self-rescheduling setTimeout with
      // randomized jitter each cycle (for resync) and a randomized initial
      // delay (for the price timer), so windows spread out over time
      // instead of syncing up.
      const FULL_RESYNC_MS = 60000;
      const FULL_RESYNC_JITTER_MS = 15000;  // +/- up to 15s spread
      let priceTimer = null;
      let resyncTimeoutHandle = null;
      let priceTimerStartHandle = null;
      let destroyed = false;

      function scheduleNextResync() {
        const jitter = (Math.random() - 0.5) * 2 * FULL_RESYNC_JITTER_MS;
        const delay = Math.max(20000, FULL_RESYNC_MS + jitter);
        resyncTimeoutHandle = setTimeout(async () => {
          if (destroyed) return;
          // Sequential, not Promise.all -- see refreshAll()'s comment for why.
          await loadBars();
          await loadIndicators();
          if (!destroyed) scheduleNextResync();
        }, delay);
      }

      function applyRefreshRate(ms) {
        currentRefreshMs = ms;
        if (priceTimer) clearInterval(priceTimer);
        if (priceTimerStartHandle) clearTimeout(priceTimerStartHandle);
        priceTimer = null;
        if (ms > 0) {
          // Randomized initial delay so windows sharing the same configured
          // rate don't all tick on the same instant either.
          const initialJitter = Math.random() * ms;
          priceTimerStartHandle = setTimeout(() => {
            if (destroyed) return;
            priceTimer = setInterval(updateLatestPrice, ms);
          }, initialJitter);
        }
      }
      applyRefreshRate(window.currentRefreshRateMs);
      scheduleNextResync();

      return {
        setSymbol,
        setTimeframe,
        setTimePosition,
        getState,
        applyTemplateConfig,
        resize: resizeAll,
        setChartTheme,
        setRefreshRate: applyRefreshRate,
        destroy() {
          destroyed = true;
          if (priceTimer) clearInterval(priceTimer);
          if (priceTimerStartHandle) clearTimeout(priceTimerStartHandle);
          if (resyncTimeoutHandle) clearTimeout(resyncTimeoutHandle);
          if (replayPlayTimer) clearInterval(replayPlayTimer);
        },
      };
    }

    // ---------- Watchlist sidebar ----------
    async function loadWatchlistDropdown() {
      const sel = document.querySelector('.wl-select');
      try {
        const r = await fetch('/realtime/watchlists');
        const d = await r.json();
        if (d.error) {
          sel.innerHTML = `<option value="">Error: ${d.error}</option>`;
          return;
        }
        if (!d.watchlists || !d.watchlists.length) {
          sel.innerHTML = '<option value="">No named watchlists found</option>';
          return;
        }
        sel.innerHTML = '<option value="">All watchlists</option>' +
          d.watchlists.map(w => `<option value="${w.id}">${w.name} (${w.symbol_count})</option>`).join('');
      } catch (e) {
        sel.innerHTML = `<option value="">Failed to load: ${e}</option>`;
      }
    }

    async function loadWatchlistSidebar(wlId) {
      const list = document.querySelector('.wl-list');
      try {
        const url = wlId ? `/realtime/watchlists/${wlId}/symbols` : '/realtime/watchlist';
        const r = await fetch(url);
        const d = await r.json();
        const symbols = d.symbols || [];
        list.dataset.all = JSON.stringify(symbols);
        renderWatchlist(symbols);
      } catch (e) {
        list.innerHTML = '<div class="wl-item muted">Failed to load watchlist</div>';
      }
    }

    function renderWatchlist(symbols) {
      const list = document.querySelector('.wl-list');
      list.innerHTML = symbols.map(s =>
        `<div class="wl-item" data-sym="${s}"><span class="sym">${s}</span></div>`
      ).join('');
      list.querySelectorAll('.wl-item').forEach((el) => {
        el.addEventListener('click', () => {
          const sym = el.dataset.sym;
          if (activeWindowId && widgets[activeWindowId]) {
            widgets[activeWindowId].setSymbol(sym);
          } else {
            addWindow(sym);
          }
        });
      });
    }

    document.querySelector('.wl-select').addEventListener('change', (e) => {
      loadWatchlistSidebar(e.target.value || null);
    });

    document.querySelector('.wl-search').addEventListener('input', (e) => {
      const list = document.querySelector('.wl-list');
      const all = JSON.parse(list.dataset.all || '[]');
      const q = e.target.value.trim().toUpperCase();
      // '(' signals a scanner query (e.g. strongcandle("1d")) -- those only
      // run on Enter (see below), not on every keystroke, since they're a
      // real backend scan, not a cheap substring match.
      if (q.includes('(')) return;
      renderWatchlist(q ? all.filter(s => s.includes(q)) : all);
    });

    document.querySelector('.wl-search').addEventListener('keydown', async (e) => {
      if (e.key !== 'Enter') return;
      const input = e.target;
      const queryText = input.value.trim();
      const list = document.querySelector('.wl-list');
      const all = JSON.parse(list.dataset.all || '[]');
      if (!queryText.includes('(')) return;  // not a scanner query -- plain substring filter already applied live

      list.innerHTML = '<div class="wl-item muted">⏳ Running scanner query…</div>';
      try {
        const r = await fetch('/realtime/watchlists/scan', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbols: all, query_text: queryText }),
        });
        const d = await r.json();
        if (d.error) {
          list.innerHTML = `<div class="wl-item muted">${d.error}</div>`;
          return;
        }
        renderWatchlist(d.matched || []);
      } catch (err) {
        list.innerHTML = `<div class="wl-item muted">Scan failed: ${err}</div>`;
      }
    });

    document.getElementById('sidebarToggleBtn').addEventListener('click', () => {
      document.getElementById('watchlist-sidebar').classList.toggle('collapsed');
      // Charts are canvas-sized to their container's clientWidth at last
      // resize; toggling the sidebar changes that width, so every open
      // widget needs to re-measure and resize. A short delay lets the CSS
      // reflow (display:none/block) complete before we read clientWidth.
      setTimeout(() => {
        Object.values(widgets).forEach((w) => w.resize());
      }, 50);
    });

    document.getElementById('refreshRateSelect').addEventListener('change', (e) => {
      const ms = parseInt(e.target.value, 10);
      window.currentRefreshRateMs = ms;
      Object.values(widgets).forEach((w) => w.setRefreshRate(ms));
    });

    // ---------- Global background-process pause ----------
    // Note: this pauses your background watchers/schedulers (alert rules,
    // telegram, signal notifier, agentic AI scanner, scheduled jobs) --
    // it reduces CPU/thread contention and SQLite lock contention those
    // create, which can genuinely help this dashboard's own DB reads.
    // It does NOT speed up yfinance/tastytrade network fetches themselves,
    // since those aren't background jobs -- they're this dashboard's own
    // per-request calls.
    async function loadPauseState() {
      try {
        const r = await fetch('/realtime/system/pause');
        const d = await r.json();
        document.getElementById('pauseBgToggle').checked = !!d.paused;
      } catch (e) { /* leave unchecked */ }
    }
    document.getElementById('pauseBgToggle').addEventListener('change', async (e) => {
      await fetch('/realtime/system/pause', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paused: e.target.checked }),
      });
    });
    loadPauseState();

    document.getElementById('layoutGroup').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-layout]');
      if (!btn) return;
      currentLayoutKey = btn.dataset.layout;
      colSplit = 0.5;  // a custom split ratio from the previous grid shape doesn't
      rowSplit = 0.5;   // carry over meaningfully to a different one -- reset to even
      updateGridLayout();
      saveLayout();
    });

    document.getElementById('syncSymbolToggle').addEventListener('change', (e) => {
      syncSymbol = e.target.checked;
      saveLayout();
    });
    document.getElementById('syncIntervalToggle').addEventListener('change', (e) => {
      syncInterval = e.target.checked;
      saveLayout();
    });
    document.getElementById('syncOptionsToggle').addEventListener('change', (e) => {
      syncOptions = e.target.checked;
      saveLayout();
    });
    document.getElementById('themeToggleBtn').addEventListener('click', () => {
      chartTheme = chartTheme === 'dark' ? 'light' : 'dark';
      applyPageTheme(chartTheme);
      Object.values(widgets).forEach((w) => { try { w.setChartTheme(chartTheme); } catch (e) {} });
      saveLayout();
    });

    document.getElementById('addWindowBtn').addEventListener('click', () => addWindow('SPY'));
    document.getElementById('saveLayoutBtn').addEventListener('click', saveNamedLayout);
    document.getElementById('loadLayoutSelect').addEventListener('change', (e) => {
      loadNamedLayout(e.target.value);
      e.target.value = '';  // reset to placeholder after loading, like the per-window template dropdown
    });
    restoreLayout();
    loadNamedLayoutsDropdown();
    loadWatchlistDropdown();
    loadWatchlistSidebar(null);

    // ---------- Pause everything when this page isn't actually visible ----------
    // This page is embedded as an iframe inside a tab on the main app --
    // switching to a different tab there almost certainly just CSS-hides
    // this tab's container (display:none) rather than unloading the
    // iframe, so without this, every window's polling timer keeps firing
    // and hitting tastytrade indefinitely in the background regardless of
    // which tab is actually showing. Two signals combined, since neither
    // alone reliably covers both cases:
    //   - Page Visibility API: catches the browser tab/window itself being
    //     switched away from or minimized.
    //   - IntersectionObserver on <html>: catches this iframe's container
    //     being hidden via CSS by the parent page's own tab-switcher,
    //     which the Visibility API does not reliably report on its own.
    let pageIsVisible = true;
    function applyPageVisibility() {
      const ms = pageIsVisible ? window.currentRefreshRateMs : 0;
      Object.values(widgets).forEach((w) => w.setRefreshRate(ms));
    }
    document.addEventListener('visibilitychange', () => {
      pageIsVisible = document.visibilityState === 'visible';
      applyPageVisibility();
    });
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((entries) => {
        pageIsVisible = entries[0].isIntersecting && document.visibilityState === 'visible';
        applyPageVisibility();
      }).observe(document.documentElement);
    }
  </script>
</body>
</html>
"""
