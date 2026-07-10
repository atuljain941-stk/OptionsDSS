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
        print(f"[realtime_dashboard] get_option_chain failed for {symbol}: {e}")
        # Serve stale cache rather than nothing if we have it -- a 30-90s
        # stale GEX read is still more useful than blanking the card on a
        # transient error.
        if cached:
            return cached[1]
        return []


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
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_scan_symbol, sym, root, benchmark, req_tfs): sym for sym in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
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
    try:
        feed.start()
        start_error = None
    except Exception as e:  # noqa: BLE001 - surface it, don't crash the request
        start_error = str(e)
    return jsonify({"ok": True, "configured": True, "feed_start_error": start_error})


@realtime_bp.route("/api/<path:symbol>/snapshot")
def snapshot(symbol: str):
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
    timeframe = request.args.get("timeframe", DEFAULT_TIMEFRAME)
    extended_hours = request.args.get("extended_hours", "1") == "1"
    df = get_recent_bars(symbol, 300, timeframe, extended_hours)
    if df is None or df.empty:
        return jsonify({"status": "no_bars", "error": _recent_fetch_error(symbol, timeframe, extended_hours)})
    out = [
        {
            "time": int(ts.timestamp()),
            "open": round(float(row.open), 4),
            "high": round(float(row.high), 4),
            "low": round(float(row.low), 4),
            "close": round(float(row.close), 4),
            "volume": round(float(row.volume), 2) if row.volume is not None else 0,
        }
        for ts, row in df.iterrows()
    ]
    return jsonify(out)


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

        try:
            from ..charts.chart_primitives import tv_sr_channels
            sr_df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
            result["sr_channels"] = tv_sr_channels(sr_df)
        except Exception as e:  # noqa: BLE001
            print(f"[realtime_dashboard] tv_sr_channels failed for {symbol}: {e}")
            result["sr_channels"] = []

        _indicators_cache[cache_key] = (time.time(), result)

    if want:
        result = {k: v for k, v in result.items() if k in want}

    return jsonify(result)


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
  </style>
</head>
<body>
  <div class="box">
    <h1>Connect Tastytrade</h1>
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
        if (d.ok && !d.feed_start_error) {
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
    body { background:#0f172a; color:#e2e8f0; font-family: -apple-system, sans-serif; margin:0; }
    .banner { background:#7c2d12; color:#fed7aa; padding:10px 20px; font-size:13px; }
    .banner a { color:#fdba74; font-weight:600; }

    .top-toolbar { display:flex; align-items:center; gap:12px; padding:10px 20px; background:#111827;
                   border-bottom:1px solid #1f2937; }
    .top-toolbar button { background:#6366f1; border:none; color:white; padding:7px 14px;
                           border-radius:6px; font-size:13px; cursor:pointer; font-weight:600; }
    .top-toolbar button:disabled { background:#334155; cursor:not-allowed; }
    .top-toolbar .count { font-size:12px; color:#64748b; }
    .top-toolbar .sidebar-toggle { margin-left:auto; background:#1e293b; border:1px solid #334155; color:#94a3b8; }
    #refreshRateSelect { background:#1e293b; border:1px solid #334155; color:#e2e8f0; padding:4px 6px;
                          border-radius:5px; font-size:12px; }
    .tf-group { display:flex; gap:2px; background:#1e293b; border-radius:6px; padding:2px; }
    .tf-btn { background:transparent; border:none; color:#94a3b8; padding:5px 10px; border-radius:4px;
              font-size:12px; cursor:pointer; }
    .tf-btn.active { background:#6366f1; color:white; }
    .tf-btn:hover:not(.active) { color:#e2e8f0; }

    .app-layout { display:flex; align-items:flex-start; }
    #grid { display:grid; gap:8px; padding:8px; grid-template-columns: 1fr; flex:1; min-width:0; }
    #grid.cols-2 { grid-template-columns: 1fr 1fr; }

    #watchlist-sidebar { width:260px; flex-shrink:0; background:#111827; border-left:1px solid #1f2937;
                          height:calc(100vh - 49px); overflow-y:auto; display:block; }
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

    .resize-handle { height:14px; margin:-4px 0; cursor:row-resize; background:transparent; position:relative;
                     z-index:5; user-select:none; touch-action:none; }
    .resize-handle::after { content:''; position:absolute; left:50%; top:50%; transform:translate(-50%,-50%);
                             width:40px; height:4px; background:#334155; border-radius:2px; }
    .resize-handle:hover::after, .resize-handle.dragging::after { background:#6366f1; height:5px; }

    .fetch-error-banner { margin:0 12px 8px; padding:10px 14px; border-radius:8px; font-size:13px;
                           font-weight:600; background:#7f1d1d; color:#fecaca; }

    .alert-menu { position:fixed; z-index:100; background:#1e293b; border:1px solid #334155; border-radius:8px;
                  padding:8px; box-shadow:0 8px 24px rgba(0,0,0,0.5); font-size:13px; min-width:220px; }
    .alert-menu .price-line { color:#94a3b8; font-size:11px; padding:2px 4px 8px; border-bottom:1px solid #334155; margin-bottom:6px; }
    .alert-menu button { display:block; width:100%; text-align:left; background:transparent; border:none;
                          color:#e2e8f0; padding:7px 8px; border-radius:5px; cursor:pointer; font-size:13px; }
    .alert-menu button:hover { background:#334155; }

    .window { background:#0b1220; border:2px solid #1f2937; border-radius:8px; overflow:hidden; }
    .window.active { border-color:#6366f1; }

    .win-toolbar { display:flex; align-items:center; gap:8px; padding:8px 12px; background:#111827;
                   border-bottom:1px solid #1f2937; flex-wrap:wrap; }
    .win-toolbar .sym-label { font-weight:700; font-size:14px; color:#e2e8f0; min-width:50px; }
    .win-toolbar input[type=text] { width:70px; background:#1e293b; border:1px solid #334155; color:#e2e8f0;
                                     padding:5px 8px; border-radius:5px; font-size:12px; text-transform:uppercase; }
    .win-toolbar button.small { background:#1e293b; border:1px solid #334155; color:#94a3b8; padding:5px 10px;
                                 border-radius:5px; font-size:12px; cursor:pointer; }
    .win-toolbar button.small:hover { border-color:#6366f1; color:#e2e8f0; }
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

    .main-chart { width:100%; height:360px; }
    .subpane { width:100%; height:110px; border-top:1px solid #1e293b; position:relative; display:none; }
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
      ema5: '#f59e0b', ema9: '#38bdf8', ema20: '#a78bfa', ema50: '#fb923c', ema200: '#f472b6',
      avwap_mid: '#2dd4bf', avwap_upper: 'rgba(45,212,191,0.5)', avwap_lower: 'rgba(45,212,191,0.5)',
    };
    const LAYOUTS = { '1x2': { rows: 1, cols: 2 }, '2x2': { rows: 2, cols: 2 }, '1x3': { rows: 1, cols: 3 }, '2x3': { rows: 2, cols: 3 } };

    let widgetCount = 0;
    let activeWindowId = null;
    const widgets = {};
    window.currentRefreshRateMs = 60000;  // matches the <select> default (1m)
    let currentLayoutKey = '2x2';
    let syncSymbol = false;
    let syncInterval = false;
    let suppressSave = false;  // true while restoring a saved layout, so restoring doesn't immediately re-save

    function currentMaxWindows() {
      const l = LAYOUTS[currentLayoutKey];
      return l.rows * l.cols;
    }

    function setActiveWindow(id) {
      activeWindowId = id;
      document.querySelectorAll('.window').forEach((el) => {
        el.classList.toggle('active', el.id === id);
      });
    }

    function applyLayoutGrid() {
      const l = LAYOUTS[currentLayoutKey];
      const grid = document.getElementById('grid');
      grid.style.gridTemplateColumns = `repeat(${l.cols}, 1fr)`;
      grid.style.gridTemplateRows = `repeat(${l.rows}, 1fr)`;
      document.querySelectorAll('[data-layout]').forEach((b) => {
        b.classList.toggle('active', b.dataset.layout === currentLayoutKey);
      });
    }

    function updateGridLayout() {
      const n = Object.keys(widgets).length;
      const max = currentMaxWindows();
      document.getElementById('windowCount').textContent = `${n} / ${max} windows`;
      document.getElementById('addWindowBtn').disabled = n >= max;
      applyLayoutGrid();
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
      return { layoutKey: currentLayoutKey, syncSymbol, syncInterval, windows: windowsState };
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
      document.getElementById('syncSymbolToggle').checked = syncSymbol;
      document.getElementById('syncIntervalToggle').checked = syncInterval;
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
        document.getElementById('syncSymbolToggle').checked = syncSymbol;
        document.getElementById('syncIntervalToggle').checked = syncInterval;
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
              <label><input type="checkbox" data-ov="ema20" checked> EMA20</label>
              <label><input type="checkbox" data-ov="ema50"> EMA50</label>
              <label><input type="checkbox" data-ov="ema200"> EMA200</label>
              <label><input type="checkbox" data-ov="avwap" checked> AVWAP</label>
              <label><input type="checkbox" data-ov="sr" checked> Support/Resistance</label>
              <div class="grp-label">Panes</div>
              <label><input type="checkbox" data-pane="rsi" checked> RSI/RSIDiff90</label>
              <label><input type="checkbox" data-pane="macd" checked> MACD</label>
              <label><input type="checkbox" data-pane="adx"> ADX</label>
              <label><input type="checkbox" data-pane="volquant" checked> VolQuant</label>
              <div class="grp-label">Session</div>
              <label><input type="checkbox" class="extendedHoursCb" checked> Extended Hours (pre/post-market)</label>
            </div>
          </div>
          <button class="close-btn" title="Close window">✕</button>
        </div>
        <div class="fetch-error-banner" style="display:none;"></div>
        <div class="main-chart"></div>
        <div class="resize-handle" data-resize-target="main"></div>
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
      let lastIndicatorsData = null;
      const overlaySeries = {};

      const chartOpts = {
        layout: { background: { color: '#0b1220' }, textColor: '#e2e8f0' },
        grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
        timeScale: { timeVisible: true, secondsVisible: false },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      };

      const mainEl = root.querySelector('.main-chart');
      const mainChart = LightweightCharts.createChart(mainEl, { ...chartOpts, height: 360 });
      const candleSeries = mainChart.addCandlestickSeries();
      const volumeSeries = mainChart.addHistogramSeries({
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume_scale',
      });
      mainChart.priceScale('volume_scale').applyOptions({
        scaleMargins: { top: 0.85, bottom: 0 },  // squeeze volume bars into the bottom 15% of the pane
      });

      const paneCharts = {};
      const paneSeries = {};
      ['rsi', 'macd', 'adx', 'volquant'].forEach((name) => {
        const el = root.querySelector(`[data-pane-el="${name}"]`);
        const chart = LightweightCharts.createChart(el, { ...chartOpts, height: 110 });
        paneCharts[name] = { chart, el };
      });

      paneSeries.rsiLine = paneCharts.rsi.chart.addLineSeries({ color: '#818cf8', lineWidth: 1.5 });
      paneSeries.rsiDiffHist = paneCharts.rsi.chart.addHistogramSeries({ color: '#475569' });
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

      function clearSrLines() {
        srLines.forEach((l) => candleSeries.removePriceLine(l));
        srLines = [];
      }
      function drawSrLines(channels) {
        clearSrLines();
        if (!isOvChecked('sr') || !channels) return;
        channels.forEach((ch, i) => {
          const label = i === 0 ? 'S/R' : '';
          srLines.push(candleSeries.createPriceLine({
            price: ch.hi, color: '#94a3b8', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
            axisLabelVisible: true, title: label,
          }));
          srLines.push(candleSeries.createPriceLine({
            price: ch.lo, color: '#94a3b8', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
            axisLabelVisible: false, title: '',
          }));
        });
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

      function showFetchError(msg) {
        const el = root.querySelector('.fetch-error-banner');
        if (msg) {
          el.textContent = `⚠ ${symbol} (${timeframe}): ${msg}`;
          el.style.display = 'block';
        } else {
          el.style.display = 'none';
        }
      }

      async function loadBars() {
        const ehParam = extendedHours ? '1' : '0';
        const r = await fetch(`/realtime/api/${symbol}/bars?timeframe=${timeframe}&extended_hours=${ehParam}`);
        const data = await r.json();
        if (Array.isArray(data)) {
          showFetchError(null);
          candleSeries.setData(data);
          volumeSeries.setData(data.map(d => ({
            time: d.time, value: d.volume || 0,
            color: d.close >= d.open ? 'rgba(34,197,94,0.5)' : 'rgba(239,68,68,0.5)',
          })));
          lastBar = data.length ? { ...data[data.length - 1] } : null;
        } else if (data.status === 'no_bars') {
          showFetchError(data.error || 'No data returned (check server console for details)');
        }
      }

      // Fast, cheap path: fetch just the current price and update the
      // last candle in place (or start a new one if enough time has
      // passed), instead of re-running the full historical backfill on
      // every refresh tick. This is what makes frequent refresh actually
      // cheap -- the expensive full fetch only happens on symbol/timeframe
      // change and the slower full-resync interval.
      async function updateLatestPrice() {
        if (!lastBar) return;  // no baseline yet -- wait for the next full loadBars()
        try {
          const r = await fetch(`/realtime/api/${symbol}/price`);
          const d = await r.json();
          if (d.status !== 'live' || d.price == null) return;
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
          } else {
            return;  // stale/out-of-order tick -- ignore rather than corrupt the series
          }
          candleSeries.update(lastBar);
        } catch (e) { /* transient -- next tick will retry */ }
      }

      async function loadIndicators() {
        // Only request indicator series that are actually toggled on --
        // trims response payload to what's in use. (The DXLink candle
        // fetch itself is already deduped server-side regardless of this;
        // this only reduces JSON payload size / redundant computation.)
        const want = [];
        ['ema5', 'ema9', 'ema20', 'ema50', 'ema200'].forEach((k) => { if (isOvChecked(k)) want.push(k); });
        if (isOvChecked('avwap')) want.push('avwap_bands');
        if (isOvChecked('sr')) want.push('sr_channels');
        if (isPaneChecked('rsi')) want.push('rsi14', 'rsi_diff_90');
        if (isPaneChecked('macd')) want.push('macd', 'macd_signal', 'macd_hist');
        if (isPaneChecked('adx')) want.push('adx14');
        if (isPaneChecked('volquant')) want.push('volquant_series');
        const wantParam = want.length ? `&want=${want.join(',')}` : '';

        const r = await fetch(`/realtime/api/${symbol}/indicators?timeframe=${timeframe}${wantParam}`);
        const d = await r.json();
        if (d.status) return;
        lastIndicatorsData = d;

        ['ema5', 'ema9', 'ema20', 'ema50', 'ema200'].forEach((k) => {
          if (isOvChecked(k) && d[k] && d[k].length) {
            ensureOverlay(k, OVERLAY_COLORS[k]).setData(d[k]);
          } else if (overlaySeries[k]) {
            overlaySeries[k].setData([]);
          }
        });

        drawSrLines(d.sr_channels);

        const avwapOn = isOvChecked('avwap');
        const bands = d.avwap_bands || {};
        ['mid', 'upper', 'lower'].forEach((k) => {
          const name = 'avwap_' + k;
          if (avwapOn && bands[k] && bands[k].length) {
            ensureOverlay(name, OVERLAY_COLORS[name], k === 'mid' ? 1.5 : 1).setData(bands[k]);
          } else if (overlaySeries[name]) {
            overlaySeries[name].setData([]);
          }
        });

        if (d.rsi14) paneSeries.rsiLine.setData(d.rsi14);
        if (d.rsi_diff_90) paneSeries.rsiDiffHist.setData(d.rsi_diff_90.map(p => ({ time: p.time, value: p.value, color: p.value >= 0 ? '#1d6fdb' : '#e03030' })));
        if (d.macd) paneSeries.macdLine.setData(d.macd);
        if (d.macd_signal) paneSeries.macdSignal.setData(d.macd_signal);
        if (d.macd_hist) paneSeries.macdHist.setData(d.macd_hist.map(p => ({ time: p.time, value: p.value, color: p.value >= 0 ? '#1d6fdb' : '#e03030' })));
        if (d.adx14) paneSeries.adxLine.setData(d.adx14);

        const vq = d.volquant_series || {};
        if (vq.histogram) paneSeries.vqHist.setData(vq.histogram.map(p => ({ time: p.time, value: p.value, color: p.value >= 0 ? '#1d6fdb' : '#e03030' })));
        if (vq.vol_amplification) paneSeries.vqLine.setData(vq.vol_amplification);
      }

      function refreshAll() {
        const indicator = root.querySelector('.loading-indicator');
        indicator.style.display = 'inline';
        Promise.all([loadBars(), loadIndicators()]).finally(() => {
          indicator.style.display = 'none';
        });
      }

      function applyPaneVisibility() {
        ['rsi', 'macd', 'adx', 'volquant'].forEach((name) => {
          const visible = isPaneChecked(name);
          paneCharts[name].el.classList.toggle('visible', visible);
          const handle = root.querySelector(`[data-resize-target="${name}"]`);
          if (handle) handle.style.display = visible ? 'block' : 'none';
        });
        resizeAll();
      }

      function resizeAll() {
        const w = mainEl.clientWidth;
        mainChart.applyOptions({ width: w });
        Object.values(paneCharts).forEach(({ chart, el }) => chart.applyOptions({ width: el.clientWidth }));
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
        saveLayout();
      }
      function userSetTimeframe(tf) {
        setTimeframe(tf);
        if (syncInterval) {
          Object.entries(widgets).forEach(([wId, w]) => { if (wId !== id) w.setTimeframe(tf); });
        }
        saveLayout();
      }

      // ---------- Template save/load ----------
      function getTemplateConfig() {
        const overlays = {};
        root.querySelectorAll('[data-ov]').forEach((cb) => { overlays[cb.dataset.ov] = cb.checked; });
        const panes = {};
        root.querySelectorAll('[data-pane]').forEach((cb) => { panes[cb.dataset.pane] = cb.checked; });
        const heights = { main: mainEl.clientHeight };
        Object.entries(paneCharts).forEach(([name, { el }]) => { heights[name] = el.clientHeight; });
        return { timeframe, overlays, panes, heights, extendedHours };
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
          if (cfg.heights.main) {
            mainEl.style.height = cfg.heights.main + 'px';
            mainChart.applyOptions({ height: cfg.heights.main });
          }
          Object.entries(paneCharts).forEach(([name, { el, chart }]) => {
            const h = cfg.heights[name];
            if (h) { el.style.height = h + 'px'; chart.applyOptions({ height: h }); }
          });
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
          const dy = e.clientY - startY;
          const newHeight = Math.max(60, startHeight + dy);
          targetEl.style.height = newHeight + 'px';
          chartObj.applyOptions({ height: newHeight });
        };
        const onUp = () => {
          handle.classList.remove('dragging');
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
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
          await Promise.all([loadBars(), loadIndicators()]);
          if (!destroyed) scheduleNextResync();
        }, delay);
      }

      function applyRefreshRate(ms) {
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
        getState,
        applyTemplateConfig,
        resize: resizeAll,
        setRefreshRate: applyRefreshRate,
        destroy() {
          destroyed = true;
          if (priceTimer) clearInterval(priceTimer);
          if (priceTimerStartHandle) clearTimeout(priceTimerStartHandle);
          if (resyncTimeoutHandle) clearTimeout(resyncTimeoutHandle);
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
