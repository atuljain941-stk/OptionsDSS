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
import threading
from typing import Optional

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


def guess_instrument_type(symbol: str):
    """Best-effort instrument type from a bare symbol string. Override
    explicitly wherever you already know the type (e.g. options chains)."""
    if InstrumentType is None:
        return None
    if symbol.startswith("./") or (symbol.startswith(".") and " " in symbol):
        return InstrumentType.FUTURE_OPTION
    if symbol.startswith("/"):
        return InstrumentType.FUTURE
    if symbol in ("SPX", "VIX", "NDX", "RUT"):
        return InstrumentType.INDEX
    if any(c.isdigit() for c in symbol) and (" C" in symbol or " P" in symbol or symbol.count(" ") >= 1) and len(symbol) > 10:
        return InstrumentType.EQUITY_OPTION
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
        self._resolved_symbols[symbol] = resolved
        return resolved

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
    # Market data
    # ---------------------------------------------------------------
    def get_snapshot(self, symbol: str, instrument_type=None) -> dict:
        try:
            session = self._ensure_session()
        except Exception as e:  # noqa: BLE001
            return {"symbol": symbol, "status": "error", "error": str(e)}

        itype = instrument_type or guess_instrument_type(symbol)
        try:
            data = self._run_coro(get_market_data(session, symbol, itype))
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
