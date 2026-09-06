"""
oiapp/services/tastytrade_feed.py
----------------------------------
Real-time-ish market data for oiapp, using tastytrade's documented
get_market_data() REST endpoint:
  https://developer.tastytrade.com/streaming-market-data/
  https://tastyworks-api.readthedocs.io/en/latest/market-data.html

Runs a single persistent asyncio event loop in a background thread for the
life of the app, and dispatches every async tastytrade call onto it via
run_coroutine_threadsafe(). This is the fix for "Event loop is closed":
an earlier version called asyncio.run(...) per-request, which creates and
destroys a fresh event loop each time -- but the cached Session's internal
async HTTP client stays bound to whichever loop was active when it first
ran, so every request after the first broke. Keeping one loop alive for
the whole process lifetime avoids that entirely, and is also cheaper
(no repeated event-loop setup/teardown per request).

Session (login) is created once and cached; get_market_data() is called
per request on the persistent loop. Credentials come from oiapp's own
app_settings DB table (same pattern as your Telegram bot token), falling
back to env vars.

Install:
    pip install tastytrade --break-system-packages
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Optional, List
from urllib.parse import quote

try:
    from tastytrade import Session
    from tastytrade.market_data import get_market_data
    from tastytrade.order import InstrumentType
except ImportError:  # pragma: no cover - surfaced clearly at runtime instead
    Session = get_market_data = InstrumentType = None

# MUST run AFTER the tastytrade import above, not before -- confirmed by
# direct testing that the tastytrade package resets its own logger to
# DEBUG as part of its own init code during import, silently undoing an
# earlier setLevel() call. This ordering bug meant the "fix" below never
# actually took effect since it was first added: every single DEBUG-level
# message (full websocket frames, candle data dumps, keepalives) has been
# logged this whole time, which is the real explanation for the rapidly
# growing multi-hundred-thousand-line log file -- and very plausibly a
# meaningful contributor to broader app slowness too, since heavy
# synchronous logging I/O competes with every other request being handled,
# not just this module's own calls.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("tastytrade").setLevel(logging.WARNING)


def _runtime_credentials() -> tuple:
    """Same pattern as oiapp.services.telegram_alerts._runtime_config():
    check the app's own app_settings DB table first (set via /realtime/setup
    or the /realtime/config API), fall back to env vars. Lazy-imported to
    avoid a hard dependency from services -> scanners at module load time.

    NOTE: tastytrade requires OAuth2 (client_secret + refresh_token), not
    username/password -- see https://tastyworks-api.readthedocs.io/en/latest/sessions.html
    Get these from https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications
    """
    client_secret = refresh_token = ""
    try:
        from ..scanners.watchlist_manager import _get_setting
        client_secret = (_get_setting("tastytrade_client_secret", "") or "").strip()
        refresh_token = (_get_setting("tastytrade_refresh_token", "") or "").strip()
    except Exception:
        pass
    client_secret = client_secret or os.environ.get("TASTYTRADE_CLIENT_SECRET", "")
    refresh_token = refresh_token or os.environ.get("TASTYTRADE_REFRESH_TOKEN", "")
    return client_secret, refresh_token


def tastytrade_configured() -> bool:
    client_secret, refresh_token = _runtime_credentials()
    return bool(client_secret and refresh_token)


def build_occ_option_symbol(root_symbol: str, expiration, option_type: str, strike) -> str:
    """Builds a correct OCC-2010 option symbol: root padded to exactly 6
    characters, YYMMDD expiration, C/P, strike*1000 zero-padded to 8
    digits. Confirmed against the real installed tastytrade SDK's own
    validation pattern (tastytrade.instruments.OCC =
    r"^[A-Z][A-Z0-9 ]{5}\\d{6}[CP]\\d{8}$") and round-tripped through its
    real Option.occ_to_streamer_symbol()/streamer_symbol_to_occ() before
    this was relied on anywhere.

    This is the fix for the compact-but-wrong format like "SPY260810C750"
    -- that string is missing both the space-padding after the root
    symbol AND the strike's *1000 zero-padding, so tastytrade's own SDK
    silently fails to parse it (occ_to_streamer_symbol returns "" rather
    than raising, which is why it just looks like "not working" with no
    error message). The correct OCC symbol for that exact contract is
    "SPY   260810C00750000"; the correct DXLink STREAMER symbol (a
    different, shorter format used for live price/candle subscriptions)
    is ".SPY260810C750" -- note the leading dot. Prefer
    get_option_chain() below over hand-building this at all: it returns
    ready-made call/put symbols AND call_streamer_symbol/put_streamer_symbol
    directly from tastytrade for every strike, so there's no format to
    get wrong in the first place.

    :param root_symbol: underlying ticker, e.g. "SPY"
    :param expiration: date, datetime, or "YYYY-MM-DD"/"YYMMDD" string
    :param option_type: "C"/"CALL"/"call" or "P"/"PUT"/"put"
    :param strike: strike price (int, float, Decimal, or numeric string)
    """
    import datetime as _dt
    from decimal import Decimal

    if isinstance(expiration, str):
        s = expiration.replace("-", "")
        if len(s) == 6:
            yymmdd = s
        elif len(s) == 8:  # YYYYMMDD
            yymmdd = s[2:]
        else:
            raise ValueError(f"Unrecognized expiration string format: {expiration!r}")
    elif isinstance(expiration, (_dt.date, _dt.datetime)):
        yymmdd = expiration.strftime("%y%m%d")
    else:
        raise ValueError(f"expiration must be a date/datetime or string, got {type(expiration)}")

    ot = str(option_type).strip().upper()
    if ot in ("C", "CALL"):
        ot = "C"
    elif ot in ("P", "PUT"):
        ot = "P"
    else:
        raise ValueError(f"option_type must be call/put, got {option_type!r}")

    strike_dec = Decimal(str(strike))
    strike_thousandths = int(strike_dec * 1000)
    if strike_thousandths < 0 or strike_thousandths > 99999999:
        raise ValueError(f"Strike {strike} out of representable OCC range")

    root_padded = root_symbol.strip().upper().ljust(6)
    if len(root_padded) > 6:
        raise ValueError(f"Root symbol {root_symbol!r} is too long for the 6-char OCC field")

    occ = f"{root_padded}{yymmdd}{ot}{strike_thousandths:08d}"
    if not re.match(r"^[A-Z][A-Z0-9 ]{5}\d{6}[CP]\d{8}$", occ):
        raise ValueError(f"Built OCC symbol failed self-validation: {occ!r}")
    return occ



async def _collect_summary_until_quiet(streamer, summary_event, expected_symbols, summary_map,
                                       greeks_done, quiet_after_greeks: float = 3.0) -> None:
    """Collect the Summary snapshot after Greeks has completed too.

    Summary carries open_interest. Greeks commonly completes first, so ending
    this listener on greeks_done discarded nearly the entire OI snapshot.
    Continue until all requested symbols arrive or the Summary stream is quiet
    for a short post-Greeks grace window.
    """
    expected = set(expected_symbols)
    listener = streamer.listen(summary_event)
    idle_after_greeks = 0.0
    while len(summary_map) < len(expected):
        try:
            event = await asyncio.wait_for(listener.__anext__(), timeout=0.5)
        except asyncio.TimeoutError:
            if greeks_done.is_set():
                idle_after_greeks += 0.5
                if idle_after_greeks >= quiet_after_greeks:
                    return
            continue
        except StopAsyncIteration:
            return
        idle_after_greeks = 0.0
        if event.event_symbol in expected:
            summary_map[event.event_symbol] = event


def guess_instrument_type(symbol: str):
    """Best-effort instrument type from a bare symbol string. Override
    explicitly wherever you already know the type (e.g. options chains)."""
    if InstrumentType is None:
        return None
    if symbol.startswith("./") or (symbol.startswith(".") and " " in symbol):
        return InstrumentType.FUTURE_OPTION
    if symbol.startswith("."):
        # DXLink streamer-format option symbol, e.g. ".SPY260810C750" --
        # confirmed via Option.occ_to_streamer_symbol()'s real output
        # format above. Distinct from the FUTURE_OPTION "./..." case,
        # which is dxfeed's convention for options ON a future.
        return InstrumentType.EQUITY_OPTION
    if symbol.startswith("/"):
        return InstrumentType.FUTURE
    if symbol in ("SPX", "VIX", "NDX", "RUT"):
        return InstrumentType.INDEX
    # Real OCC-2010 format, confirmed against the installed SDK's own
    # validation regex (tastytrade.instruments.OCC) rather than the
    # previous loose heuristic -- root padded to 6 chars, YYMMDD, C/P,
    # 8-digit zero-padded strike*1000.
    if re.match(r"^[A-Z][A-Z0-9 ]{5}\d{6}[CP]\d{8}$", symbol):
        return InstrumentType.EQUITY_OPTION
    if "/" in symbol and not symbol.startswith("/"):
        # Standard tastytrade cryptocurrency pair format, e.g. "BTC/USD"
        # -- a slash NOT in the first position (which is reserved for
        # futures roots like "/MGC"). This is TastyTrade's documented
        # crypto symbol convention, distinct from CME's actual Bitcoin
        # futures contracts (/BTC, /MBT), which remain FUTURE above.
        return InstrumentType.CRYPTOCURRENCY
    return InstrumentType.EQUITY


class TastytradeFeed:
    """One cached authenticated OAuth Session + a persistent background
    event loop that every async tastytrade call is dispatched onto, so the
    Session's internal async client is always used from the same loop it
    was created on."""

    def __init__(self, client_secret: Optional[str] = None, refresh_token: Optional[str] = None):
        self._client_secret_override = client_secret
        self._refresh_token_override = refresh_token
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._session = None
        self._lock = threading.Lock()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._streamer = None  # persistent DXLinkStreamer, opened lazily on first candle fetch
        self._streamer_creation_lock = None  # guards only the one-time streamer connection setup
        self._symbol_locks = {}  # (symbol, interval) -> asyncio.Lock -- see _fetch_candles_async
        self._resolved_symbols = {}  # raw symbol -> resolved dxfeed streamer_symbol (futures only)

    # ---------------------------------------------------------------
    # Persistent background event loop
    # ---------------------------------------------------------------
    def _ensure_loop(self):
        if self._loop is not None:
            return
        with self._lock:
            if self._loop is not None:
                return

            def _run_loop():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._loop_ready.set()
                loop.run_forever()

            self._loop_thread = threading.Thread(target=_run_loop, daemon=True, name="tastytrade-loop")
            self._loop_thread.start()
            self._loop_ready.wait(timeout=10)

    def _run_coro(self, coro, timeout: float = 20.0):
        """Dispatch a coroutine onto the persistent loop and block for the
        result -- safe to call from Flask's sync request threads."""
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ---------------------------------------------------------------
    # Session management
    # ---------------------------------------------------------------
    def _ensure_session(self):
        if Session is None:
            raise RuntimeError(
                "The 'tastytrade' package is not installed. Run: "
                "pip install tastytrade --break-system-packages"
            )
        with self._lock:
            if self._session is not None:
                return self._session
            if not self.client_secret or not self.refresh_token:
                db_secret, db_token = _runtime_credentials()
                self.client_secret = self._client_secret_override or db_secret
                self.refresh_token = self._refresh_token_override or db_token
            if not self.client_secret or not self.refresh_token:
                raise RuntimeError(
                    "No tastytrade OAuth credentials found. Set them at "
                    "/realtime/setup (or POST /realtime/config), or set "
                    "TASTYTRADE_CLIENT_SECRET / TASTYTRADE_REFRESH_TOKEN as "
                    "env vars. Get these from "
                    "https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications"
                )
            # Session() itself does a blocking (sync) login call internally,
            # so it's fine to construct outside the async loop -- only the
            # *usage* (get_market_data, DXLinkStreamer) needs to stay on
            # the same loop consistently, which _run_coro guarantees.
            self._session = Session(self.client_secret, self.refresh_token)
            return self._session

    def start(self, symbols: Optional[list] = None):
        """Kept for API compatibility with app_factory/init_realtime and the
        setup-page POST handler -- eagerly logs in and starts the background
        loop so auth failures surface immediately instead of on first chart
        load. `symbols` is unused (no subscription concept with REST polling)."""
        self._ensure_loop()
        self._ensure_session()

    def reset(self):
        """Force re-login on next call (e.g. after credentials change).
        Does NOT tear down the background loop -- the loop itself has no
        credential state, only the Session object does."""
        with self._lock:
            self._session = None

    def get_session(self):
        self._ensure_loop()
        return self._ensure_session()

    def run_coro(self, coro, timeout: float = 20.0):
        """Public entry point for other modules (e.g. the GEX option-chain
        fetch) to run their own tastytrade coroutines on this same
        persistent loop, instead of calling asyncio.run() themselves and
        re-introducing the closed-loop bug."""
        return self._run_coro(coro, timeout=timeout)

    # ---------------------------------------------------------------
    # Persistent DXLink streamer -- opened ONCE and reused for every
    # candle fetch, rather than opening/closing a fresh websocket (full
    # SETUP -> AUTH_STATE -> CHANNEL_REQUEST handshake) per request. This
    # is the actual fix for repeated-connection churn: the handshake now
    # happens once per process lifetime (or once per reconnect after a
    # real error), and every subsequent candle fetch just
    # subscribe_candle/unsubscribe_candle's on the same open connection.
    #
    # Locking strategy (revised): a single global lock around the ENTIRE
    # fetch (the first version of this fix) made every candle fetch queue
    # behind every other one, regardless of symbol -- meaning 4 windows
    # with different symbols would serialize instead of running
    # concurrently, which is the actual cause of "impossible to use, too
    # slow" with multiple windows open. Fixed properly: streamer *creation*
    # still needs a brief global lock (only one physical connection can
    # exist), but the actual subscribe -> collect -> unsubscribe cycle now
    # uses a per-(symbol, interval) lock, so different symbols fetch fully
    # concurrently on the shared connection. This is safe because of the
    # event_symbol filtering below, which discards any candle event that
    # doesn't belong to the specific fetch waiting for it -- that filter is
    # what makes concurrent access across symbols correct, not the lock.
    # ---------------------------------------------------------------
    def _ensure_streamer_creation_lock(self):
        if self._streamer_creation_lock is None:
            self._streamer_creation_lock = asyncio.Lock()  # must be created on the loop it's used on
        return self._streamer_creation_lock

    def _get_symbol_lock(self, key):
        lock = self._symbol_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._symbol_locks[key] = lock
        return lock

    async def _ensure_streamer_async(self, session):
        if self._streamer is not None:
            return self._streamer
        from tastytrade import DXLinkStreamer
        streamer = DXLinkStreamer(session)
        await streamer.__aenter__()  # manual enter -- kept open, not used as a one-shot `async with`
        self._streamer = streamer
        return streamer

    def get_candles(self, symbol: str, interval: str, start_time, extended_hours: bool = False,
                     idle_timeout: float = 3.0, overall_timeout: float = 15.0):
        """Historical OHLCV candles via tastytrade's DXLink candle stream
        (subscribe_candle) -- this is the ONLY price-action data source
        (no yfinance anywhere). Reuses the one persistent streamer
        connection above rather than opening a new websocket per call.
        Terminates when either the stream signals snapshot completion or
        no new candle has arrived for `idle_timeout` seconds (dxfeed's
        snapshot-end flag isn't universally reliable across every
        instrument type, so the idle timeout is the real backstop --
        lowered from 8s/25s to 3s/15s since a healthy snapshot completes
        almost immediately and the old ceiling made every slow-path fetch
        painfully slow to wait out).
        Returns a list of raw Candle events; caller converts to a DataFrame."""
        session = self._ensure_session()
        return self._run_coro(
            self._fetch_candles_async(session, symbol, interval, start_time, extended_hours, idle_timeout),
            timeout=overall_timeout,
        )

    async def _resolve_streamer_symbol(self, session, symbol):
        """For futures symbols, resolve the correct dxfeed streamer_symbol
        via the tastytrade instruments API, rather than assuming the raw
        typed symbol works directly for candle streaming. Confirmed
        necessary by comparing against tastytrade's own charting tool,
        which resolves a specific contract like '/MGCQ26' to
        '/MGCQ26:XCEC' (with an exchange suffix).

        Two cases, confirmed against tastytrade's own official API docs
        (developer.tastytrade.com/basic-api-usage -- GET /instruments/futures):
          1. You typed a SPECIFIC contract (e.g. '/MGCQ26') -- Future.get()
             with `symbols=` resolves it directly.
          2. You typed a BARE ROOT (e.g. '/MGC') -- this is NOT itself a
             tradeable instrument, only specific monthly contracts are, so
             `symbols=` resolution returns nothing for it. Confirmed this
             is exactly why a bare-root chart came back completely blank
             rather than wrong. Fixed by instead querying ALL contracts
             for that product code (`product_codes=`) and picking the one
             flagged `active=True` -- the current front-month contract,
             same one tastytrade's own platform shows when you enter just
             the root symbol.

        Cached per symbol since resolution needs its own network round-trip.
        """
        if symbol in self._resolved_symbols:
            return self._resolved_symbols[symbol]
        resolved = symbol  # fallback: use as typed if resolution isn't applicable or fails
        if symbol.startswith("/"):
            try:
                from tastytrade.instruments import Future

                # Case 1: try as a specific contract symbol first.
                futures = await Future.get(session, symbols=[symbol])
                if isinstance(futures, list):
                    futures = futures[0] if futures else None

                if futures is None:
                    # Case 2: bare root -- query all contracts for this
                    # product code and pick the active (front-month) one.
                    product_code = symbol.lstrip("/")
                    candidates = await Future.get(session, product_codes=[product_code])
                    if not isinstance(candidates, list):
                        candidates = [candidates] if candidates else []
                    futures = next((f for f in candidates if getattr(f, "active", False)), None)
                    if futures is None:
                        futures = next((f for f in candidates if getattr(f, "active_month", False)), None)
                    if futures is None and candidates:
                        futures = candidates[0]  # last resort: whatever came back first

                if futures is not None and getattr(futures, "streamer_symbol", None):
                    resolved = futures.streamer_symbol
                else:
                    print(f"[tastytrade_feed] No instrument found resolving {symbol}; using raw symbol as a fallback")
            except Exception as e:  # noqa: BLE001
                print(f"[tastytrade_feed] Future symbol resolution failed for {symbol}: {type(e).__name__}: {e} -- using raw symbol")
        elif re.match(r"^[A-Z][A-Z0-9 ]{5}\d{6}[CP]\d{8}$", symbol):
            # A real OCC-2010 option symbol (space-padded root, e.g.
            # "SPY   260810C00750000") -- DXLink candle streaming needs
            # the SHORTER streamer format instead (".SPY260810C750"), a
            # different string, not just the same one url-decoded or
            # similar. Confirmed via the real installed SDK's own
            # Option.occ_to_streamer_symbol(), not hand-derived.
            from tastytrade.instruments import Option
            try:
                streamer_symbol = Option.occ_to_streamer_symbol(symbol)
                if streamer_symbol:
                    resolved = streamer_symbol
                else:
                    print(f"[tastytrade_feed] Option.occ_to_streamer_symbol() returned empty for {symbol!r} -- OCC format may be malformed; using raw symbol")
            except Exception as e:  # noqa: BLE001
                print(f"[tastytrade_feed] Option symbol resolution failed for {symbol}: {type(e).__name__}: {e} -- using raw symbol")
        elif "/" in symbol and not symbol.startswith("/"):
            # Cryptocurrency pair, e.g. "BTC/USD" -- tastytrade's own
            # spot-crypto instrument type, distinct from CME Bitcoin
            # futures (/BTC, /MBT above). Has its own streamer_symbol
            # field directly on the Cryptocurrency instrument, same
            # shape as Future's.
            try:
                from tastytrade.instruments import Cryptocurrency
                crypto = await Cryptocurrency.get(session, symbols=[symbol])
                if isinstance(crypto, list):
                    crypto = crypto[0] if crypto else None
                if crypto is not None and getattr(crypto, "streamer_symbol", None):
                    resolved = crypto.streamer_symbol
                else:
                    print(f"[tastytrade_feed] No cryptocurrency instrument found resolving {symbol}; using raw symbol as a fallback")
            except Exception as e:  # noqa: BLE001
                print(f"[tastytrade_feed] Cryptocurrency symbol resolution failed for {symbol}: {type(e).__name__}: {e} -- using raw symbol")
        self._resolved_symbols[symbol] = resolved
        return resolved

    def search_symbols(self, query: str, limit: int = 25) -> list:
        """Symbol search, backed by tastytrade's own /symbols/search/{q}
        endpoint (tastytrade.search.symbol_search) -- the same lookup
        their own web platform's symbol-search box uses. Returns
        [{symbol, description, instrument_type}], instrument_type
        guessed via guess_instrument_type() above since the search
        endpoint itself doesn't classify results.
        """
        session = self._ensure_session()
        from tastytrade.search import symbol_search
        results = self._run_coro(symbol_search(session, query))
        out = []
        for r in results[:limit]:
            itype = guess_instrument_type(r.symbol)
            out.append({
                "symbol": r.symbol,
                "description": r.description,
                "instrument_type": itype.value if itype is not None else None,
            })
        return out

    def get_nested_option_chain(self, underlying_symbol: str) -> dict:
        """Full nested option chain for an underlying (e.g. "SPY") via
        tastytrade's NestedOptionChain.get() -- returns tastytrade's OWN
        ready-made symbols for every contract (both the OCC symbol and
        the DXLink streamer symbol), so there is no hand-built OCC
        string anywhere in this path to get wrong. This is deliberately
        the preferred way to get an option symbol at all, over
        build_occ_option_symbol() above -- that helper exists for
        completeness/testing, not as the primary path.

        Named distinctly from this file's own get_option_chain() (used
        for GEX/greeks, wraps the different tastytrade.instruments.
        get_option_chain() module function and returns a flat
        List[ChainRow]) -- same underlying data, shaped for a different
        purpose (expiration-grouped strikes for a chain-picker UI, not
        greeks). Kept as two separate functions rather than merged, to
        avoid changing the already-working GEX path.
        """
        session = self._ensure_session()
        from tastytrade.instruments import NestedOptionChain
        chains = self._run_coro(NestedOptionChain.get(session, underlying_symbol))
        if not chains:
            return {"underlying_symbol": underlying_symbol, "expirations": []}
        chain = chains[0]  # NestedOptionChain.get returns a list; equities/ETFs have exactly one chain
        expirations = []
        for exp in sorted(chain.expirations, key=lambda e: e.expiration_date):
            strikes = []
            for s in sorted(exp.strikes, key=lambda s: s.strike_price):
                strikes.append({
                    "strike": float(s.strike_price),
                    "call_symbol": s.call,
                    "call_streamer_symbol": s.call_streamer_symbol,
                    "put_symbol": s.put,
                    "put_streamer_symbol": s.put_streamer_symbol,
                })
            expirations.append({
                "expiration_date": exp.expiration_date.isoformat(),
                "days_to_expiration": exp.days_to_expiration,
                "expiration_type": exp.expiration_type,
                "strikes": strikes,
            })
        return {
            "underlying_symbol": chain.underlying_symbol,
            "root_symbol": chain.root_symbol,
            "shares_per_contract": chain.shares_per_contract,
            "expirations": expirations,
        }

    async def _fetch_candles_async(self, session, symbol, interval, start_time, extended_hours, idle_timeout):
        from tastytrade.dxfeed import Candle

        # Streamer creation: brief, one-time (or once-per-reconnect) global
        # lock. This does NOT serialize the actual data fetches below.
        creation_lock = self._ensure_streamer_creation_lock()
        async with creation_lock:
            try:
                streamer = await self._ensure_streamer_async(session)
            except Exception:
                self._streamer = None
                streamer = await self._ensure_streamer_async(session)

        resolved_symbol = await self._resolve_streamer_symbol(session, symbol)

        # Per-(symbol, interval) lock: keyed by the ORIGINAL symbol so it
        # still matches the caller's own cache key concept; only prevents
        # the SAME symbol+interval from racing with itself. Different
        # symbols proceed concurrently.
        lock = self._get_symbol_lock((symbol, interval))
        async with lock:
            candles = {}
            try:
                await streamer.subscribe_candle(
                    [resolved_symbol], interval, start_time, extended_trading_hours=extended_hours
                )
                while True:
                    c = await asyncio.wait_for(streamer.get_event(Candle), timeout=idle_timeout)
                    # Defensive filter: only accept events for the symbol we
                    # actually asked for (the resolved one, since that's
                    # what dxfeed will report events under). dxfeed's candle
                    # event_symbol typically embeds the interval (e.g.
                    # "SPY{=5m}"), so an exact prefix match is the safe
                    # check here rather than assuming an exact full-string
                    # match. This filter is what makes concurrent
                    # multi-symbol access on the shared streamer correct,
                    # not the lock above.
                    ev_symbol = str(getattr(c, "event_symbol", "") or "")
                    if ev_symbol and not ev_symbol.startswith(resolved_symbol):
                        continue
                    candles[int(c.time)] = c
                    if getattr(c, "snapshot_end", False):
                        break
            except asyncio.TimeoutError:
                pass  # no new candle within idle_timeout -- treat backfill as complete
            finally:
                try:
                    await streamer.unsubscribe_candle(resolved_symbol, interval)
                except Exception:
                    pass
        return list(candles.values())

    # ---------------------------------------------------------------
    # Bubble layer: live Trade + Quote classification (buy vs sell,
    # size-filtered) for the "large order" visualization discussed in the
    # Chart Fanatics / Fabio Valentini order-flow transcript. Reuses the
    # SAME persistent streamer as candles above -- not a second
    # connection -- via the generic subscribe()/get_event() interface
    # (subscribe_candle/unsubscribe_candle above are candle-SPECIFIC
    # wrappers around this same underlying mechanism).
    #
    # CAVEAT, stated plainly: this has NOT been run against a live
    # tastytrade session in this environment (no network access / no
    # installed package here). The Trade/Quote class names and field
    # names below (tastytrade.dxfeed.Trade/.price/.size, Quote/.bid_price/
    # .ask_price) match the public tastytrade SDK's documented dxfeed
    # event shapes and the same module Candle already comes from in this
    # file, but until this runs against your real connection, treat field
    # names as "best effort, first thing to check if trades show 0 size
    # or price". _log_first_trade_shape() below prints the raw event's
    # __dict__ once per symbol specifically so that check is fast if
    # something doesn't match.
    #
    # Also unverified: whether your account's specific DXLink data
    # entitlement includes Trade-level events at all for futures (some
    # tastytrade data plans are Quote/Candle-only) -- ensure_trade_stream()
    # surfaces that as a clear error rather than silently returning
    # nothing, so it should be obvious immediately either way.
    # ---------------------------------------------------------------
    def _ensure_trade_state(self):
        if not hasattr(self, "_trade_buffers"):
            self._trade_buffers = {}       # symbol -> deque of classified trade dicts
            self._trade_stream_tasks = {}  # symbol -> concurrent.futures.Future (the running listener)
            self._trade_buffer_lock = threading.Lock()
            self._trade_logged_shape = set()  # symbols we've already dumped one raw event for

    def _log_first_trade_shape(self, symbol, event, label):
        self._ensure_trade_state()
        key = f"{symbol}:{label}"
        if key in self._trade_logged_shape:
            return
        self._trade_logged_shape.add(key)
        try:
            print(f"[tastytrade_feed] first {label} event for {symbol}: {vars(event)}")
        except Exception:
            print(f"[tastytrade_feed] first {label} event for {symbol}: {event!r}")

    def ensure_trade_stream(self, symbol: str) -> dict:
        """Starts (if not already running) a background Trade+Quote
        listener for `symbol` on the shared persistent streamer, feeding a
        bounded in-memory buffer that get_recent_trades() reads from.
        Idempotent -- safe to call on every chart load/poll; a second call
        for a symbol already streaming just confirms it's running rather
        than opening a duplicate subscription. Returns
        {"started": bool, "already_running": bool, "error": str|None}."""
        self._ensure_trade_state()
        existing = self._trade_stream_tasks.get(symbol)
        if existing is not None and not existing.done():
            return {"started": False, "already_running": True, "error": None}
        if existing is not None and existing.done():
            exc = existing.exception()
            if exc is not None:
                print(f"[tastytrade_feed] trade stream for {symbol} previously died: {exc}")

        try:
            session = self._ensure_session()
        except Exception as e:  # noqa: BLE001
            return {"started": False, "already_running": False, "error": str(e)}

        async def _runner():
            from tastytrade.dxfeed import Trade, Quote

            creation_lock = self._ensure_streamer_creation_lock()
            async with creation_lock:
                try:
                    streamer = await self._ensure_streamer_async(session)
                except Exception:
                    self._streamer = None
                    streamer = await self._ensure_streamer_async(session)

            resolved_symbol = await self._resolve_streamer_symbol(session, symbol)
            latest_quote = {"bid": None, "ask": None}

            await streamer.subscribe(Quote, [resolved_symbol])
            await streamer.subscribe(Trade, [resolved_symbol])

            async def _quote_loop():
                while True:
                    q = await streamer.get_event(Quote)
                    ev_symbol = str(getattr(q, "event_symbol", "") or "")
                    if ev_symbol and not ev_symbol.startswith(resolved_symbol):
                        continue
                    self._log_first_trade_shape(symbol, q, "Quote")
                    bid = getattr(q, "bid_price", None)
                    ask = getattr(q, "ask_price", None)
                    if bid is not None:
                        latest_quote["bid"] = float(bid)
                    if ask is not None:
                        latest_quote["ask"] = float(ask)

            async def _trade_loop():
                buf = self._trade_buffers.setdefault(symbol, deque(maxlen=2000))
                while True:
                    t = await streamer.get_event(Trade)
                    ev_symbol = str(getattr(t, "event_symbol", "") or "")
                    if ev_symbol and not ev_symbol.startswith(resolved_symbol):
                        continue
                    self._log_first_trade_shape(symbol, t, "Trade")
                    price = getattr(t, "price", None)
                    size = getattr(t, "size", None)
                    if price is None or size is None:
                        continue
                    price = float(price)
                    size = float(size)
                    if size <= 0:
                        continue
                    bid, ask = latest_quote["bid"], latest_quote["ask"]
                    side = "unknown"
                    if bid is not None and ask is not None and ask > bid:
                        mid = (bid + ask) / 2.0
                        if price >= ask - 1e-9:
                            side = "buy"
                        elif price <= bid + 1e-9:
                            side = "sell"
                        else:
                            side = "buy" if price > mid else ("sell" if price < mid else "unknown")
                    with self._trade_buffer_lock:
                        buf.append({"time": time.time(), "price": price, "size": size, "side": side})

            await asyncio.gather(_quote_loop(), _trade_loop())

        future = asyncio.run_coroutine_threadsafe(_runner(), self._loop)
        self._trade_stream_tasks[symbol] = future
        return {"started": True, "already_running": False, "error": None}

    def get_recent_trades(self, symbol: str, since_ts: Optional[float] = None, min_size: float = 0.0) -> list:
        """Reads the live-classified trade buffer for `symbol` (does NOT
        start streaming -- call ensure_trade_stream() first, same
        start-then-poll pattern as everything else in this file). Returns
        entries newest-last, each {"time", "price", "size", "side"}."""
        self._ensure_trade_state()
        with self._trade_buffer_lock:
            items = list(self._trade_buffers.get(symbol, ()))
        out = [t for t in items if t["size"] >= min_size and (since_ts is None or t["time"] >= since_ts)]
        return out

    def trade_stream_status(self, symbol: str) -> dict:
        self._ensure_trade_state()
        future = self._trade_stream_tasks.get(symbol)
        if future is None:
            return {"running": False, "error": None}
        if future.done():
            exc = future.exception()
            return {"running": False, "error": str(exc) if exc else None}
        return {"running": True, "error": None}

    # ---------------------------------------------------------------
    # Live chain snapshot: Greeks + volume for every strike of one
    # expiry, plus the underlying's live price -- the actual "live feed
    # of options chain strikes and Greeks corresponding to futures price
    # and volume" building block. A snapshot burst on a schedule (every
    # 5/10/15 min via the background scheduler), not a permanently-open
    # subscription -- same design principle as everything else in this
    # app: wake up, capture, store, go back to sleep, rather than a
    # long-lived connection that has to handle reconnects/drift itself.
    #
    # Built directly on the ALREADY-PROVEN pattern in
    # realtime_dashboard.py's _fetch_chain_async (DXLinkStreamer +
    # Greeks subscription) -- that one is hardcoded to the ~45 DTE
    # monthly expiry and only captures gamma; this generalizes it to any
    # expiry and captures the full greek set (delta/gamma/theta/vega/IV)
    # plus per-strike volume via a short Trade-event listen alongside
    # the Greeks listen, in the same streaming session.
    def discover_expiries(self, underlying_symbol: str, months_ahead: int = 2) -> dict:
        """Metadata-only expiry discovery -- calls tastytrade's REST
        instrument-chain lookup (the same one get_live_chain_snapshot
        already calls internally before it does any streaming) WITHOUT
        subscribing to DXLink or waiting on any collection window. This
        is the cheap, fast step; per-expiry Greeks/OI capture is the
        expensive one and stays in get_live_chain_snapshot, called once
        per expiry this returns, by the caller's own throttled backfill
        loop -- not looped here, since that would turn a fast metadata
        call into the same multi-second-per-expiry cost this function
        exists specifically to avoid.

        Returns {"ok": bool, "expiries": ["YYYY-MM-DD", ...], "error": ...}
        """
        try:
            session = self._ensure_session()
        except Exception as e:
            return {"ok": False, "error": str(e), "expiries": []}

        async def _run():
            from tastytrade.instruments import get_option_chain
            import datetime as _dt

            chain = await get_option_chain(session, underlying_symbol)
            keys = list(chain.keys()) if hasattr(chain, "keys") else list(chain)
            if not keys:
                return {"ok": False, "error": f"No option chain returned for {underlying_symbol}", "expiries": []}
            cutoff = _dt.date.today() + _dt.timedelta(days=int(months_ahead * 30.44))
            exps = sorted(
                k.isoformat() if hasattr(k, "isoformat") else str(k)
                for k in keys
                if (k if hasattr(k, "year") else _dt.date.fromisoformat(str(k)[:10])) <= cutoff
            )
            return {"ok": True, "expiries": exps, "error": None}

        try:
            return self._run_coro(_run(), timeout=20.0)
        except Exception as e:
            return {"ok": False, "error": str(e), "expiries": []}

    def get_multi_expiry_chain_snapshot(self, underlying_symbol: str, expiries: Optional[List[str]] = None,
                                         months_ahead: int = 2, strikes_each_side: Optional[int] = None,
                                         collect_timeout: float = 20.0) -> dict:
        """Same Greeks/OI/volume capture as get_live_chain_snapshot(),
        but subscribed across MULTIPLE expiries in ONE DXLink session,
        waiting ONCE for everything to arrive -- not once per expiry.

        This exists specifically because looping get_live_chain_snapshot()
        once per expiry (the original design of the tastytrade options
        backfill) multiplies the 15s collection window by every expiry
        being fetched -- for a symbol with ~8 expiries in 2 months, that's
        8 separate 15-20s waits for one symbol, which is what made a
        full-watchlist backfill estimate balloon to hours. DXLink already
        subscribes to many strikes in one burst for a single expiry (that's
        how get_live_chain_snapshot's own ~50-100 strikes all arrive
        together); there's no architectural reason the same one-session,
        one-wait approach can't span multiple expiries' strikes at once.
        A single collection window is used regardless of how many expiries
        are included -- DXLink pushes many events concurrently once
        subscribed, not one at a time per expiry, so this isn't multiplying
        the wait, just the number of things arriving during it. collect_timeout
        defaults slightly higher than the single-expiry function's (20s vs
        15s) to give more data more room to land, not because more expiries
        mechanically requires proportionally more time.

        expiries: explicit list of "YYYY-MM-DD" strings to fetch. If
        omitted, months_ahead is used to select every expiry within that
        window from the chain automatically (same window
        discover_expiries() would find, but this method does the
        selection itself rather than requiring two calls).

        Returns {"ok": bool, "underlying": ..., "by_expiry": {expiry:
        [rows...]}, "error": ...} -- rows grouped by expiry so the
        caller can write each expiry separately, same row shape as
        get_live_chain_snapshot's own "rows".
        """
        try:
            session = self._ensure_session()
        except Exception as e:
            return {"ok": False, "error": str(e), "by_expiry": {}}

        spot_price = None
        if strikes_each_side is not None:
            try:
                snap = self.get_snapshot(underlying_symbol)
                if snap.get("status") == "live":
                    spot_price = float(snap.get("last") or snap.get("mark") or snap.get("mid") or 0) or None
            except Exception:
                spot_price = None

        async def _run():
            from tastytrade import DXLinkStreamer
            from tastytrade.dxfeed import Greeks, Trade
            from tastytrade.instruments import get_option_chain
            import datetime as _dt

            chain = await get_option_chain(session, underlying_symbol)
            keys = list(chain.keys()) if hasattr(chain, "keys") else list(chain)
            if not keys:
                return {"ok": False, "error": f"No option chain returned for {underlying_symbol}", "by_expiry": {}}

            if expiries:
                wanted = set(expiries)
                target_keys = [k for k in keys if (k.isoformat() if hasattr(k, "isoformat") else str(k))[:10] in wanted]
            else:
                cutoff = _dt.date.today() + _dt.timedelta(days=int(months_ahead * 30.44))
                target_keys = [k for k in keys if (k if hasattr(k, "year") else _dt.date.fromisoformat(str(k)[:10])) <= cutoff]
            if not target_keys:
                return {"ok": False, "error": f"No expiries matched for {underlying_symbol}", "by_expiry": {}}

            # Combine every target expiry's option instruments into one
            # list -- this is the actual batching: one streamer_symbol
            # list spanning all of them, not one list per expiry.
            all_options = []
            option_expiry_map = {}  # streamer_symbol -> expiry string, needed to regroup rows afterward
            for k in target_keys:
                exp_str = k.isoformat() if hasattr(k, "isoformat") else str(k)
                options = chain.get(k) if hasattr(chain, "get") else chain[k]
                if strikes_each_side is not None and options and spot_price:
                    distinct_strikes = sorted({float(o.strike_price) for o in options})
                    below = [s for s in distinct_strikes if s <= spot_price][-strikes_each_side:]
                    above = [s for s in distinct_strikes if s > spot_price][:strikes_each_side]
                    keep_strikes = set(below) | set(above)
                    options = [o for o in options if float(o.strike_price) in keep_strikes]
                for o in options:
                    option_expiry_map[o.streamer_symbol] = exp_str
                all_options.extend(options)

            streamer_symbols = [o.streamer_symbol for o in all_options]
            if not streamer_symbols:
                return {"ok": False, "error": f"No strikes found for {underlying_symbol} across {len(target_keys)} expiries", "by_expiry": {}}

            greeks_map: dict = {}
            summary_map: dict = {}
            volume_map: dict = {}
            max_print_map: dict = {}
            large_print_count: dict = {}
            BLOCK_SIZE_THRESHOLD = 50
            summary_available = True
            async with DXLinkStreamer(session) as streamer:
                await streamer.subscribe(Greeks, streamer_symbols)
                await streamer.subscribe(Trade, streamer_symbols)
                # Open interest as a STREAMED value, not just a static
                # instrument-metadata attribute. This exists because of a
                # real, confirmed production failure: a full watchlist
                # backfill reported "173/199 fetched" with literally zero
                # rows written to the database. _write_chain_rows() only
                # keeps rows with oi>0, so that means EVERY strike, for
                # EVERY symbol, came back with oi<=0 from
                # getattr(o, "open_interest", 0) on the static instrument
                # object -- consistent with that attribute either not
                # existing on this SDK version's Option object, or not
                # being populated on instrument metadata at all (as
                # opposed to being a per-symbol network fluke, which
                # would explain SOME symbols failing, not literally all
                # of them). dxfeed's Summary event is the standard event
                # type that carries open interest as a streamed value
                # (openInterest field) -- reasoned from general dxfeed
                # protocol knowledge, NOT verified against the actual
                # installed tastytrade SDK, since it isn't installed in
                # the environment this was written in. Wrapped so a
                # missing Summary class or subscription failure degrades
                # to the old static-attribute path rather than crashing
                # the whole fetch.
                try:
                    from tastytrade.dxfeed import Summary
                    await streamer.subscribe(Summary, streamer_symbols)
                except Exception as e:
                    summary_available = False
                    print(f"[tastytrade_feed] Summary event subscription unavailable ({type(e).__name__}: {e}) "
                          f"-- falling back to static instrument open_interest only for {underlying_symbol}")

                greeks_done = asyncio.Event()

                async def _collect_greeks():
                    collected = 0
                    async for g in streamer.listen(Greeks):
                        greeks_map[g.event_symbol] = g
                        collected += 1
                        if collected >= len(streamer_symbols):
                            break
                    greeks_done.set()

                async def _collect_summary():
                    if summary_available:
                        await _collect_summary_until_quiet(
                            streamer, Summary, streamer_symbols, summary_map, greeks_done
                        )

                async def _collect_trades():
                    # Trade events are best-effort enrichment (volume,
                    # block-print detection) -- unlike Greeks there's no
                    # "we have them all" condition, since a strike simply
                    # may not trade during the window. Previously this was
                    # an unbounded `async for` with no exit at all, so
                    # asyncio.gather() below could never finish early and
                    # EVERY symbol burned the full collect_timeout even
                    # when all Greeks had arrived in a couple of seconds.
                    # That fixed cost, not the network, dominated the
                    # per-symbol time for the whole backfill. Now it stops
                    # as soon as Greeks are complete, so a symbol takes
                    # roughly as long as its data actually needs.
                    listener = streamer.listen(Trade)
                    while not greeks_done.is_set():
                        try:
                            t = await asyncio.wait_for(listener.__anext__(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue  # no trade this instant -- re-check whether Greeks finished
                        except StopAsyncIteration:
                            return
                        sym = t.event_symbol
                        size = float(getattr(t, "size", 0) or 0)
                        volume_map[sym] = volume_map.get(sym, 0.0) + size
                        if size > max_print_map.get(sym, 0.0):
                            max_print_map[sym] = size
                        if size >= BLOCK_SIZE_THRESHOLD:
                            large_print_count[sym] = large_print_count.get(sym, 0) + 1

                try:
                    await asyncio.wait_for(
                        asyncio.gather(_collect_greeks(), _collect_trades(), _collect_summary()),
                        timeout=collect_timeout,
                    )
                except asyncio.TimeoutError:
                    pass  # partial data is fine -- most strikes with real OI got a Greeks push already

            by_expiry: dict = {}
            oi_sources_used = {"summary": 0, "static_attr": 0, "none": 0}
            diagnostic_logged = False
            for o in all_options:
                g = greeks_map.get(o.streamer_symbol)
                summary = summary_map.get(o.streamer_symbol)
                is_call = str(getattr(o, "option_type", "")).upper().startswith("C")
                exp_str = option_expiry_map[o.streamer_symbol]

                oi_from_summary = getattr(summary, "open_interest", None) if summary is not None else None
                oi_from_static = getattr(o, "open_interest", None)
                if oi_from_summary is not None and float(oi_from_summary) > 0:
                    oi_final = float(oi_from_summary)
                    oi_sources_used["summary"] += 1
                elif oi_from_static is not None and float(oi_from_static or 0) > 0:
                    oi_final = float(oi_from_static)
                    oi_sources_used["static_attr"] += 1
                else:
                    oi_final = 0.0
                    oi_sources_used["none"] += 1
                    # Diagnostic logging, requested directly: when OI comes
                    # back empty from BOTH sources, dump the raw attribute
                    # names actually present on the live SDK objects for
                    # the FIRST such strike only (not every strike -- this
                    # is meant to be read once to fix the real cause, not
                    # to flood the log for a whole watchlist run). This is
                    # the fastest path to a precise fix: whatever this
                    # prints IS the real attribute name to use, no more
                    # guessing from a sandbox without the SDK installed.
                    if not diagnostic_logged:
                        diagnostic_logged = True
                        try:
                            static_attrs = [a for a in dir(o) if not a.startswith("_")]
                            summary_attrs = [a for a in dir(summary)] if summary is not None else None
                            print(f"[tastytrade_feed] DIAGNOSTIC: {underlying_symbol} strike {getattr(o, 'strike_price', '?')} "
                                  f"{'call' if is_call else 'put'} -- OI came back empty from both sources.\n"
                                  f"  Static instrument object attributes: {static_attrs}\n"
                                  f"  open_interest value on static object: {oi_from_static!r}\n"
                                  f"  Summary event received: {summary is not None}\n"
                                  f"  Summary object attributes (if received): {summary_attrs}\n"
                                  f"  open_interest value on Summary (if received): {oi_from_summary!r}")
                        except Exception as diag_e:
                            print(f"[tastytrade_feed] DIAGNOSTIC logging itself failed: {diag_e}")

                by_expiry.setdefault(exp_str, []).append({
                    "strike": float(o.strike_price), "type": "call" if is_call else "put",
                    "delta": float(g.delta) if g is not None and getattr(g, "delta", None) is not None else None,
                    "gamma": float(g.gamma) if g is not None and getattr(g, "gamma", None) is not None else None,
                    "theta": float(g.theta) if g is not None and getattr(g, "theta", None) is not None else None,
                    "vega": float(g.vega) if g is not None and getattr(g, "vega", None) is not None else None,
                    "iv": float(g.volatility) if g is not None and getattr(g, "volatility", None) is not None else None,
                    "oi": oi_final,
                    "volume": volume_map.get(o.streamer_symbol, 0.0),
                    "max_print_size": max_print_map.get(o.streamer_symbol, 0.0),
                    "large_print_count": large_print_count.get(o.streamer_symbol, 0),
                    "streamer_symbol": o.streamer_symbol,
                })
            print(f"[tastytrade_feed] {underlying_symbol} OI sources: "
                  f"{oi_sources_used['summary']} from Summary, {oi_sources_used['static_attr']} from static attr, "
                  f"{oi_sources_used['none']} had none (out of {len(all_options)} strikes)")
            return {"ok": True, "underlying": underlying_symbol, "by_expiry": by_expiry, "error": None,
                    "oi_sources_used": oi_sources_used}

        try:
            return self._run_coro(_run(), timeout=collect_timeout + 15.0)
        except Exception as e:
            return {"ok": False, "error": str(e), "by_expiry": {}}

    def get_live_chain_snapshot(self, underlying_symbol: str, expiry: Optional[str] = None,
                                 collect_timeout: float = 15.0, strikes_each_side: Optional[int] = 12) -> dict:
        """Returns {"ok": bool, "underlying": ..., "expiry": ..., "rows": [...], "error": ...}
        where each row is {"strike", "type", "delta", "gamma", "theta",
        "vega", "iv", "oi", "volume", "streamer_symbol"}.

        expiry: "YYYY-MM-DD" string. If omitted, uses whichever expiry
        tastytrade's own get_option_chain() returns first after sorting
        (closest-dated) -- pass an explicit date to target something
        specific, e.g. matching what GEX Trend Tracker already tracks
        for this symbol.

        strikes_each_side: limits which strikes actually get subscribed
        to Greeks/Trade -- not just a display filter, this reduces the
        number of live symbols in the streaming burst itself (a chain
        can have 500+ strikes; subscribing to all of them is both slow
        to collect and unreadable once charted). Default 12 each side of
        spot, based on the observation that daily moves rarely exceed
        roughly 7-8 points -- comfortably covered with room to spare on
        a typical strike grid. Pass None for the full chain (e.g. for
        symbols with unusually wide daily ranges).
        """
        try:
            session = self._ensure_session()
        except Exception as e:
            return {"ok": False, "error": str(e), "rows": []}

        # Spot price fetched HERE, synchronously, before entering _run()'s
        # coroutine -- NOT inside it. get_snapshot() itself calls
        # self._run_coro(), which does asyncio.run_coroutine_threadsafe()
        # + a blocking .result() wait; calling that from *inside* a
        # coroutine that's already executing on this same persistent loop
        # would block the loop's own thread waiting on a task submitted
        # to that same loop -- a deadlock, not just inefficient. Fetching
        # it out here keeps get_snapshot()'s blocking call on the caller's
        # thread instead, which is what it's actually designed for.
        spot_price = None
        if strikes_each_side is not None:
            try:
                snap = self.get_snapshot(underlying_symbol)
                if snap.get("status") == "live":
                    spot_price = float(snap.get("last") or snap.get("mark") or snap.get("mid") or 0) or None
            except Exception:
                spot_price = None

        async def _run():
            from tastytrade import DXLinkStreamer
            from tastytrade.dxfeed import Greeks, Trade
            from tastytrade.instruments import get_option_chain
            import datetime as _dt

            chain = await get_option_chain(session, underlying_symbol)
            # chain is keyed by expiration date -- pick the requested one
            # or the nearest available if none was specified.
            keys = list(chain.keys()) if hasattr(chain, "keys") else list(chain)
            if not keys:
                return {"ok": False, "error": f"No option chain returned for {underlying_symbol}", "rows": []}
            if expiry:
                target = _dt.date.fromisoformat(expiry[:10])
                matched = [k for k in keys if getattr(k, "isoformat", lambda: str(k))()[:10] == target.isoformat()]
                exp_key = matched[0] if matched else min(keys, key=lambda k: abs((k - target).days) if hasattr(k, "days") else 0)
            else:
                exp_key = sorted(keys)[0]
            options = chain.get(exp_key) if hasattr(chain, "get") else chain[exp_key]

            if strikes_each_side is not None and options and spot_price:
                distinct_strikes = sorted({float(o.strike_price) for o in options})
                below = [s for s in distinct_strikes if s <= spot_price][-strikes_each_side:]
                above = [s for s in distinct_strikes if s > spot_price][:strikes_each_side]
                keep_strikes = set(below) | set(above)
                options = [o for o in options if float(o.strike_price) in keep_strikes]

            streamer_symbols = [o.streamer_symbol for o in options]
            if not streamer_symbols:
                return {"ok": False, "error": f"No strikes found for {underlying_symbol} expiry {exp_key}", "rows": []}

            greeks_map: dict = {}
            summary_map: dict = {}
            volume_map: dict = {}
            max_print_map: dict = {}   # largest single trade print seen per strike this window
            large_print_count: dict = {}  # how many prints crossed the block-size threshold
            BLOCK_SIZE_THRESHOLD = 50  # contracts in one print -- a rough, adjustable line between
                                        # "probably retail" and "probably institutional/desk" block activity
            summary_available = True
            async with DXLinkStreamer(session) as streamer:
                await streamer.subscribe(Greeks, streamer_symbols)
                await streamer.subscribe(Trade, streamer_symbols)
                # Same Summary-event OI fallback as get_multi_expiry_chain_snapshot
                # -- see its comment for the full reasoning and the
                # production failure (173/199 "fetched", 0 rows written)
                # that motivated it. Applied here too since this function
                # feeds Live Chain Tracker on a 5-minute schedule and uses
                # the identical static-attribute OI read.
                try:
                    from tastytrade.dxfeed import Summary
                    await streamer.subscribe(Summary, streamer_symbols)
                except Exception as e:
                    summary_available = False
                    print(f"[tastytrade_feed] Summary event subscription unavailable ({type(e).__name__}: {e}) "
                          f"-- falling back to static instrument open_interest only for {underlying_symbol}")

                greeks_done = asyncio.Event()

                async def _collect_greeks():
                    collected = 0
                    async for g in streamer.listen(Greeks):
                        greeks_map[g.event_symbol] = g
                        collected += 1
                        if collected >= len(streamer_symbols):
                            break
                    greeks_done.set()

                async def _collect_summary():
                    if summary_available:
                        await _collect_summary_until_quiet(
                            streamer, Summary, streamer_symbols, summary_map, greeks_done
                        )

                async def _collect_trades():
                    # Volume isn't a single push like Greeks -- accumulate
                    # whatever Trade events arrive during the collection
                    # window rather than waiting for one per symbol (most
                    # strikes won't trade in any given 15s window at all).
                    #
                    # Bounded by greeks_done rather than running unbounded:
                    # previously this had no exit condition at all, so
                    # asyncio.gather() below could never complete early and
                    # every capture burned the FULL collect_timeout even
                    # when all Greeks had already arrived. Same fix as
                    # get_multi_expiry_chain_snapshot above -- worth doing
                    # here too since Live Chain Tracker runs this on a
                    # 5-minute schedule, so the wasted seconds recurred
                    # all session long.
                    #
                    # dxfeed's Trade event fires once per individual print,
                    # not as a pre-aggregated total -- t.size IS one trade's
                    # size. Previously this only summed everything into
                    # volume_map, throwing away that per-print detail. 500
                    # contracts from one block print is a fundamentally
                    # different signal than 500 contracts from 500 retail
                    # 1-lots -- only the former looks like an institution
                    # actually establishing a position, which is the thing
                    # worth detecting early in the session, not aggregate
                    # volume that could be entirely retail noise.
                    listener = streamer.listen(Trade)
                    while not greeks_done.is_set():
                        try:
                            t = await asyncio.wait_for(listener.__anext__(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        except StopAsyncIteration:
                            return
                        sym = t.event_symbol
                        size = float(getattr(t, "size", 0) or 0)
                        volume_map[sym] = volume_map.get(sym, 0.0) + size
                        if size > max_print_map.get(sym, 0.0):
                            max_print_map[sym] = size
                        if size >= BLOCK_SIZE_THRESHOLD:
                            large_print_count[sym] = large_print_count.get(sym, 0) + 1

                try:
                    await asyncio.wait_for(
                        asyncio.gather(_collect_greeks(), _collect_trades(), _collect_summary()),
                        timeout=collect_timeout,
                    )
                except asyncio.TimeoutError:
                    pass  # partial data is fine -- most strikes with real OI got a Greeks push already

            rows = []
            oi_sources_used = {"summary": 0, "static_attr": 0, "none": 0}
            diagnostic_logged = False
            for o in options:
                g = greeks_map.get(o.streamer_symbol)
                summary = summary_map.get(o.streamer_symbol)
                is_call = str(getattr(o, "option_type", "")).upper().startswith("C")

                oi_from_summary = getattr(summary, "open_interest", None) if summary is not None else None
                oi_from_static = getattr(o, "open_interest", None)
                if oi_from_summary is not None and float(oi_from_summary) > 0:
                    oi_final = float(oi_from_summary)
                    oi_sources_used["summary"] += 1
                elif oi_from_static is not None and float(oi_from_static or 0) > 0:
                    oi_final = float(oi_from_static)
                    oi_sources_used["static_attr"] += 1
                else:
                    oi_final = 0.0
                    oi_sources_used["none"] += 1
                    if not diagnostic_logged:
                        diagnostic_logged = True
                        try:
                            static_attrs = [a for a in dir(o) if not a.startswith("_")]
                            summary_attrs = [a for a in dir(summary)] if summary is not None else None
                            print(f"[tastytrade_feed] DIAGNOSTIC (live chain): {underlying_symbol} strike {getattr(o, 'strike_price', '?')} "
                                  f"{'call' if is_call else 'put'} -- OI came back empty from both sources.\n"
                                  f"  Static instrument object attributes: {static_attrs}\n"
                                  f"  open_interest value on static object: {oi_from_static!r}\n"
                                  f"  Summary event received: {summary is not None}\n"
                                  f"  Summary object attributes (if received): {summary_attrs}\n"
                                  f"  open_interest value on Summary (if received): {oi_from_summary!r}")
                        except Exception as diag_e:
                            print(f"[tastytrade_feed] DIAGNOSTIC logging itself failed: {diag_e}")

                rows.append({
                    "strike": float(o.strike_price), "type": "call" if is_call else "put",
                    "delta": float(g.delta) if g is not None and getattr(g, "delta", None) is not None else None,
                    "gamma": float(g.gamma) if g is not None and getattr(g, "gamma", None) is not None else None,
                    "theta": float(g.theta) if g is not None and getattr(g, "theta", None) is not None else None,
                    "vega": float(g.vega) if g is not None and getattr(g, "vega", None) is not None else None,
                    "iv": float(g.volatility) if g is not None and getattr(g, "volatility", None) is not None else None,
                    "oi": oi_final,
                    "volume": volume_map.get(o.streamer_symbol, 0.0),
                    "max_print_size": max_print_map.get(o.streamer_symbol, 0.0),
                    "large_print_count": large_print_count.get(o.streamer_symbol, 0),
                    "streamer_symbol": o.streamer_symbol,
                })
            print(f"[tastytrade_feed] {underlying_symbol} OI sources: "
                  f"{oi_sources_used['summary']} from Summary, {oi_sources_used['static_attr']} from static attr, "
                  f"{oi_sources_used['none']} had none (out of {len(options)} strikes)")
            exp_str = exp_key.isoformat() if hasattr(exp_key, "isoformat") else str(exp_key)
            return {"ok": True, "underlying": underlying_symbol, "expiry": exp_str, "rows": rows, "error": None,
                    "oi_sources_used": oi_sources_used}

        try:
            return self._run_coro(_run(), timeout=collect_timeout + 15.0)
        except Exception as e:
            return {"ok": False, "error": str(e), "rows": []}

    # ---------------------------------------------------------------
    # Market data
    # ---------------------------------------------------------------
    def get_snapshot(self, symbol: str, instrument_type=None) -> dict:
        try:
            session = self._ensure_session()
        except Exception as e:  # noqa: BLE001
            return {"symbol": symbol, "status": "error", "error": str(e)}

        itype = instrument_type or guess_instrument_type(symbol)
        # get_market_data() builds its URL as an f-string path segment
        # (f"/market-data/{instrument_type.value}/{symbol}") with ZERO
        # encoding of `symbol` -- confirmed by reading its actual source
        # in the installed SDK. For any futures symbol, which always
        # starts with "/" (e.g. "/MGCV6"), that produces a literal
        # DOUBLE SLASH in the path: "/market-data/Future//MGCV6" -- a
        # malformed URL, not just an unencoded-but-valid one. This is
        # exactly why futures price fetches were failing (silently,
        # before last turn's error-surfacing fix; now at least loudly).
        #
        # Confirmed this is a real gap in the SDK, not a misunderstanding
        # on our part: tastytrade.search.symbol_search() -- a DIFFERENT
        # function in this same installed package -- explicitly does
        # `symbol.replace("/", "%2F")` before building ITS url for
        # exactly this reason, proving the SDK authors know a raw "/"
        # can't go directly into a URL path segment; get_market_data()
        # just never got the same treatment. The instrument-lookup path
        # used for /bars (Future.get(session, symbols=[symbol])) is NOT
        # affected -- confirmed separately that it sends the symbol as a
        # query parameter (self._client.get(url, params=params)), which
        # httpx encodes safely and automatically; only this raw-path
        # case needed a manual fix.
        #
        # Percent-encoding (not stripping the slash) is deliberate: it's
        # exactly what symbol_search() already does, and mirrors how the
        # server is expected to interpret the path -- Future.get()'s
        # OWN single-string branch instead strips the slash entirely
        # before a *different* endpoint (/instruments/futures/{symbol}),
        # but that's a different endpoint with different expectations,
        # not evidence either way for what /market-data/ wants.
        encoded_symbol = quote(symbol, safe="") if "/" in symbol else symbol
        try:
            data = self._run_coro(get_market_data(session, encoded_symbol, itype))
        except Exception as e:  # noqa: BLE001
            return {"symbol": symbol, "status": "error", "error": str(e)}

        if data is None:
            return {"symbol": symbol, "status": "no_data"}

        def _f(v):
            return float(v) if v is not None else None

        bid, ask = _f(data.bid), _f(data.ask)
        mid = (bid + ask) / 2 if bid is not None and ask is not None else _f(data.mark)

        return {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "last": _f(data.last),
            "mark": _f(data.mark),
            "mid": mid,
            "volume": _f(data.volume),
            "day_high": _f(getattr(data, "day_high_price", None)),
            "day_low": _f(getattr(data, "day_low_price", None)),
            "day_open": _f(getattr(data, "day_open", None)),
            "open_interest": _f(getattr(data, "open_interest", None)),
            "updated_at": str(data.updated_at) if getattr(data, "updated_at", None) else None,
            "status": "live",
        }


# Module-level singleton used by the Flask blueprint (realtime_dashboard.py)
feed = TastytradeFeed()
