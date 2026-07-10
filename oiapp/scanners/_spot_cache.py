"""
Shared spot-price cache — fetches from yfinance with a 5-min TTL.
All scanners and strategy import from here so there's one cache,
not N separate calls per symbol per request.
"""
import time
import threading

_cache: dict[str, tuple[float, float]] = {}   # symbol -> (price, fetched_at)
_lock  = threading.Lock()
TTL    = 300   # seconds before a refresh


def get_spot(symbol: str) -> float | None:
    """Return live spot price for symbol.  Uses cache; refreshes after TTL."""
    now = time.time()
    with _lock:
        if symbol in _cache:
            price, fetched_at = _cache[symbol]
            if now - fetched_at < TTL:
                return price

    # Outside lock: do the slow yfinance call
    price = _fetch(symbol)

    with _lock:
        if price is not None:
            _cache[symbol] = (price, now)
    return price


def get_spot_and_prev(symbol: str) -> tuple[float | None, float | None]:
    """Return (today_close, prev_close) for PCR divergence check."""
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="5d")
        if len(df) >= 2:
            return float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
        if not df.empty:
            return float(df["Close"].iloc[-1]), None
    except Exception:
        pass
    return None, None


def _fetch(symbol: str) -> float | None:
    try:
        import yfinance as yf
        import signal

        def _alarm(s, f):
            raise TimeoutError()

        # SIGALRM only works on Unix; skip gracefully on Windows
        try:
            signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(6)
        except (AttributeError, OSError):
            pass

        try:
            df = yf.Ticker(symbol).history(period="1d")
            result = float(df["Close"].iloc[-1]) if not df.empty else None
        finally:
            try:
                signal.alarm(0)
            except (AttributeError, OSError):
                pass

        return result
    except Exception:
        return None
