"""
yfinance session helper — handles Yahoo Finance 401/crumb errors.
Import get_ticker() instead of yf.Ticker() for auto-retry.
"""
try:
    import yfinance as yf
except Exception:
    yf = None
import time, threading

_lock   = threading.Lock()
_warmed = False

def _warm_session():
    """Download a tiny SPY chunk to refresh the yfinance crumb/session."""
    global _warmed
    if yf is None:
        _warmed = True
        return
    try:
        yf.download("SPY", period="1d", progress=False, auto_adjust=True)
        _warmed = True
    except: pass

def get_ticker(sym: str):
    """Return a yf.Ticker-like object, or None if yfinance is unavailable."""
    global _warmed
    if yf is None:
        return None
    if not _warmed:
        with _lock:
            if not _warmed:
                _warm_session()
    return yf.Ticker(sym)

def safe_history(sym: str, period="1y", interval="1d", retries=2):
    """Fetch history with automatic session retry on 401."""
    if yf is None:
        return None
    for attempt in range(retries + 1):
        try:
            tk = yf.Ticker(sym)
            h  = tk.history(period=period, interval=interval)
            if h is not None and not h.empty:
                return h
        except Exception as e:
            if "401" in str(e) and attempt < retries:
                _warm_session()
                time.sleep(0.5)
                continue
        break
    return None

def safe_calendar(sym: str, retries=2):
    """Fetch tk.calendar with retry."""
    if yf is None:
        return None
    for attempt in range(retries + 1):
        try:
            tk  = yf.Ticker(sym)
            cal = tk.calendar
            if cal is not None:
                return cal
        except Exception as e:
            if "401" in str(e) and attempt < retries:
                _warm_session(); time.sleep(0.3); continue
        break
    return None

def safe_earnings_dates(sym: str, retries=2):
    """Fetch tk.earnings_dates with retry."""
    if yf is None:
        return None
    for attempt in range(retries + 1):
        try:
            tk  = yf.Ticker(sym)
            ed  = tk.earnings_dates
            if ed is not None and not ed.empty:
                return ed
        except Exception as e:
            if "401" in str(e) and attempt < retries:
                _warm_session(); time.sleep(0.3); continue
        break
    return None
