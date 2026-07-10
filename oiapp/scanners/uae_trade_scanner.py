# oiapp/scanners/uae_trade_scanner.py
"""
UAE Guide Trade Scanner
───────────────────────
A separate, guide-based trade scanner built from the uploaded
"UAE - Trend/Vol Analyzer v4" Pine logic and trading field guide.

This intentionally does NOT replace the existing Trade Opportunity Scanner.
It uses DTE-aware timeframe selection, the UAE trend/vol regime model,
pre-trade checklist scoring, option-strike OI/OI-change enrichment, and
RR-aware spread selection.
"""

from __future__ import annotations

import json
import math
import sqlite3
import traceback
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, jsonify, render_template, request

uae_trade_bp = Blueprint("uae_trade", __name__, url_prefix="/uae-trade-scanner")

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")
MAX_WORKERS = 4
PRICE_CACHE_TTL_SECONDS = 600
OPTION_CHAIN_CACHE_TTL_SECONDS = 300
MAX_SCAN_SYMBOLS_DEFAULT = 50
MAX_SCAN_SYMBOLS_HARD_CAP = 250
_PRICE_BATCH_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_OPTION_CHAIN_CACHE: Dict[str, Tuple[float, Optional[Any], Optional[Any], str]] = {}
_HIST_IV_CACHE: Dict[str, Tuple[float, float]] = {}
DEFAULT_MIN_SCORE = 65
DEFAULT_DTE = 21
DEFAULT_TARGET_RR = 1.0
DEFAULT_MIN_RR = 0.70
DEFAULT_SHORT_DELTA = 0.45
DEFAULT_WIDTH = 5.0
DEFAULT_EARN_GUARD = 14


REGIME_CHOICES = [
    {"value": "BULL", "label": "Bull"},
    {"value": "WEAK_BULL", "label": "Weak Bull"},
    {"value": "BEAR", "label": "Bear"},
    {"value": "WEAK_BEAR", "label": "Weak Bear"},
    {"value": "SIDEWAYS", "label": "Sideways"},
]
TIMEFRAME_CHOICES = [
    {"value": "5m", "label": "5 min"},
    {"value": "15m", "label": "15 min"},
    {"value": "1h", "label": "1 hour"},
    {"value": "4h", "label": "4 hour"},
    {"value": "1d", "label": "Daily"},
    {"value": "1wk", "label": "Weekly"},
]
DEFAULT_REGIME_FILTERS = ["WEAK_BULL", "BEAR"]

# Pine v4 auto timeframe table. The guide says the indicator auto-sets params by TF.
TF_PARAMS: Dict[str, Dict[str, Any]] = {
    "5m":     {"label": "5min",   "fast": 5, "slow": 13, "signal": 2, "roc": 3, "slope": 4,  "adx_thr": 18.0},
    "15m":    {"label": "15min",  "fast": 7, "slow": 15, "signal": 3, "roc": 4, "slope": 5,  "adx_thr": 18.0},
    "1h":     {"label": "1H",     "fast": 8, "slow": 20, "signal": 3, "roc": 5, "slope": 7,  "adx_thr": 20.0},
    "4h":     {"label": "4H",     "fast": 8, "slow": 20, "signal": 3, "roc": 5, "slope": 8,  "adx_thr": 20.0},
    "1d":     {"label": "Daily",  "fast": 8, "slow": 21, "signal": 3, "roc": 6, "slope": 10, "adx_thr": 20.0},
    "1wk":    {"label": "Weekly", "fast": 8, "slow": 21, "signal": 3, "roc": 7, "slope": 12, "adx_thr": 20.0},
}

DTE_CHOICES = [7, 14, 21, 28, 35, 45]


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return c


def _safe_float(v: Any, default: Optional[float] = None, ndigits: Optional[int] = None) -> Optional[float]:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        if ndigits is not None:
            return round(f, ndigits)
        return f
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return default
        return int(v)
    except Exception:
        return default


def _sanitize(obj: Any) -> Any:
    """JSON-safe sanitizer: converts NaN/Inf and pandas/numpy scalars."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize(v) for v in obj]
    try:
        # pandas/numpy scalars support item().
        return _sanitize(obj.item())
    except Exception:
        return str(obj)


# ─────────────────────────────────────────────────────────────────────────────
# Watchlists / symbols
# ─────────────────────────────────────────────────────────────────────────────


def _watchlists() -> List[Dict[str, Any]]:
    try:
        con = _conn()
        rows = con.execute(
            "SELECT w.id, w.name, COUNT(ws.id) AS cnt "
            "FROM watchlists w LEFT JOIN watchlist_symbols ws ON ws.watchlist_id=w.id "
            "GROUP BY w.id ORDER BY w.name"
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _watchlist_symbols(watchlist_id: Optional[Any] = None) -> List[str]:
    try:
        con = _conn()
        if watchlist_id:
            rows = con.execute(
                "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                (int(watchlist_id),),
            ).fetchall()
        else:
            rows = con.execute("SELECT DISTINCT symbol FROM symbols ORDER BY symbol").fetchall()
        con.close()
        return sorted({str(r[0]).upper().strip() for r in rows if r[0]})
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# DTE-aware guide plan
# ─────────────────────────────────────────────────────────────────────────────


def _dte_plan(dte: int) -> Dict[str, Any]:
    """
    Select entry and bias timeframes from the guide.

    Guide mapping used:
      * 15m entries require 1H as the mandatory filter.
      * 1H entries use 4H as the bias filter.
      * 4H swing entries require Daily alignment.
      * Daily entries use Weekly macro bias.
    """
    d = int(dte or DEFAULT_DTE)
    if d <= 7:
        return {
            "style": "short-term / intraday swing",
            "entry_tf": "15m", "bias_tf": "1h", "macro_tf": "4h",
            "required_tfs": ["15m", "1h", "4h", "1d"],
            "note": "7 DTE: use 15m entry, 1H mandatory filter, 4H context.",
        }
    if d <= 21:
        return {
            "style": "core swing",
            "entry_tf": "1h", "bias_tf": "4h", "macro_tf": "1d",
            "required_tfs": ["1h", "4h", "1d"],
            "note": "14-21 DTE: use 1H entry and 4H/Daily bias.",
        }
    if d <= 35:
        return {
            "style": "multi-day swing",
            "entry_tf": "4h", "bias_tf": "1d", "macro_tf": "1wk",
            "required_tfs": ["4h", "1d", "1wk"],
            "note": "28-35 DTE: use 4H entries with Daily and Weekly bias.",
        }
    return {
        "style": "position swing",
        "entry_tf": "1d", "bias_tf": "1wk", "macro_tf": "1wk",
        "required_tfs": ["1d", "1wk"],
        "note": "45 DTE: use Daily entries with Weekly macro bias.",
    }


def _tf_label(tf: str) -> str:
    return TF_PARAMS.get(tf, {}).get("label", tf)


# ─────────────────────────────────────────────────────────────────────────────
# yfinance data and indicator logic translated from Pine
# ─────────────────────────────────────────────────────────────────────────────


def _normalise_ohlcv(df):
    import pandas as pd

    if df is None or len(df) == 0:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    rename = {c: str(c).title().replace(" ", "_") for c in df.columns}
    df = df.rename(columns=rename)
    # yfinance normally returns Open/High/Low/Close/Volume.
    needed = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[needed].copy()
    for col in needed:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if "Volume" not in df.columns:
        df["Volume"] = 0
    return df


def _resample_ohlcv(df, rule: str):
    if df is None or df.empty:
        return df
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    return df.resample(rule).agg(agg).dropna(subset=["Close"])


def _yf_history(symbol: str, tf: str):
    """Fetch just enough data for one symbol. Used as a fallback and for detail views."""
    import yfinance as yf

    sym = symbol.upper().strip()
    t = yf.Ticker(sym)
    if tf == "1wk":
        return _normalise_ohlcv(t.history(period="5y", interval="1wk", auto_adjust=False, actions=False))
    if tf == "1d":
        return _normalise_ohlcv(t.history(period="2y", interval="1d", auto_adjust=False, actions=False))
    if tf == "4h":
        # Yahoo has no stable 4h interval. Resample recent 1h bars.
        one_h = _normalise_ohlcv(t.history(period="6mo", interval="1h", auto_adjust=False, actions=False))
        return _resample_ohlcv(one_h, "4h")
    if tf == "1h":
        return _normalise_ohlcv(t.history(period="6mo", interval="1h", auto_adjust=False, actions=False))
    if tf == "15m":
        return _normalise_ohlcv(t.history(period="30d", interval="15m", auto_adjust=False, actions=False))
    if tf == "5m":
        return _normalise_ohlcv(t.history(period="10d", interval="5m", auto_adjust=False, actions=False))
    return _normalise_ohlcv(t.history(period="1y", interval="1d", auto_adjust=False, actions=False))


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    try:
        ts, val = _PRICE_BATCH_CACHE.get(key, (0, None))
        if val is not None and time.time() - ts <= PRICE_CACHE_TTL_SECONDS:
            return val
    except Exception:
        pass
    return None


def _cache_set(key: str, val: Dict[str, Any]) -> Dict[str, Any]:
    try:
        _PRICE_BATCH_CACHE[key] = (time.time(), val)
        # Keep cache bounded; each entry can contain several DataFrames.
        if len(_PRICE_BATCH_CACHE) > 18:
            oldest = sorted(_PRICE_BATCH_CACHE.items(), key=lambda kv: kv[1][0])[:6]
            for k, _ in oldest:
                _PRICE_BATCH_CACHE.pop(k, None)
    except Exception:
        pass
    return val


def _split_multi_download(df, symbols: List[str]) -> Dict[str, Any]:
    """Convert yfinance multi-symbol download output into symbol->DataFrame."""
    import pandas as pd

    symbols = [s.upper().strip() for s in symbols if s]
    out: Dict[str, Any] = {s: pd.DataFrame() for s in symbols}
    if df is None or len(df) == 0:
        return out
    if not isinstance(df.columns, pd.MultiIndex):
        # Single symbol request.
        if len(symbols) == 1:
            out[symbols[0]] = _normalise_ohlcv(df)
        return out
    levels = [list(map(str, df.columns.get_level_values(i).unique())) for i in range(df.columns.nlevels)]
    for sym in symbols:
        sub = None
        try:
            if sym in levels[0]:
                sub = df[sym]
            elif df.columns.nlevels > 1 and sym in levels[1]:
                sub = df.xs(sym, axis=1, level=1)
        except Exception:
            sub = None
        out[sym] = _normalise_ohlcv(sub) if sub is not None else pd.DataFrame()
    return out


def _yf_history_many(symbols: List[str], tf: str) -> Dict[str, Any]:
    """Batch fetch OHLCV for many symbols/timeframe.

    This is the main speed fix: the scanner no longer calls yfinance once per
    symbol per timeframe and no longer loads option chains during the first pass.
    """
    import yfinance as yf

    syms = sorted({str(s).upper().strip() for s in symbols if s})
    if not syms:
        return {}
    # 4H is derived from one batch 1H request.
    dl_tf = "1h" if tf == "4h" else tf
    if dl_tf == "1wk":
        period, interval = "5y", "1wk"
    elif dl_tf == "1d":
        period, interval = "2y", "1d"
    elif dl_tf == "1h":
        period, interval = "6mo", "1h"
    elif dl_tf == "15m":
        period, interval = "30d", "15m"
    elif dl_tf == "5m":
        period, interval = "10d", "5m"
    else:
        period, interval = "1y", "1d"
    key = "|".join([tf, period, interval, ",".join(syms)])
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        df = yf.download(
            tickers=" ".join(syms),
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=True,
        )
        frames = _split_multi_download(df, syms)
        if tf == "4h":
            frames = {sym: _resample_ohlcv(frame, "4h") if frame is not None and not frame.empty else frame for sym, frame in frames.items()}
        return _cache_set(key, frames)
    except Exception:
        # Avoid a slow per-symbol fallback for large watchlists. For a single symbol,
        # fallback is acceptable and makes detail/symbol scans resilient.
        if len(syms) == 1:
            return {syms[0]: _yf_history(syms[0], tf)}
        return _cache_set(key, {sym: None for sym in syms})


def _build_indicator_cache(symbols: List[str], plan: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, str]], Dict[str, Dict[str, Any]]]:
    """Batch fetch and compute UAE indicators for all symbols required by the DTE plan."""
    symbols = [s.upper().strip() for s in symbols if s]
    indicators: Dict[str, Dict[str, Any]] = {s: {} for s in symbols}
    errors: Dict[str, Dict[str, str]] = {s: {} for s in symbols}
    frames_by_symbol: Dict[str, Dict[str, Any]] = {s: {} for s in symbols}
    for tf in plan.get("required_tfs", []):
        frames = _yf_history_many(symbols, tf)
        for sym in symbols:
            df = frames.get(sym)
            frames_by_symbol.setdefault(sym, {})[tf] = df
            if df is None or getattr(df, "empty", True):
                errors.setdefault(sym, {})[tf] = "no OHLCV bars"
                continue
            try:
                ind = _compute_uae(df, tf)
                if ind:
                    indicators.setdefault(sym, {})[tf] = ind
                else:
                    errors.setdefault(sym, {})[tf] = "not enough OHLCV bars"
            except Exception as e:
                errors.setdefault(sym, {})[tf] = str(e)[:140]
    return indicators, errors, frames_by_symbol


def _hist_iv_from_frame(df: Any) -> float:
    try:
        if df is None or getattr(df, "empty", True) or "Close" not in df:
            return 25.0
        close = df["Close"].astype(float).dropna()
        if len(close) < 25:
            return 25.0
        rets = (close / close.shift(1)).apply(lambda x: math.log(x) if x and x > 0 else None).dropna()
        if len(rets) < 10:
            return 25.0
        return round(float(rets.tail(30).std() * math.sqrt(252) * 100), 1)
    except Exception:
        return 25.0


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))


def _target_delta_strike(spot: float, dte: int, iv_pct: float, direction: str, short_delta: float) -> float:
    """Approximate a strike from target short delta without loading an option chain."""
    S = max(float(spot), 0.01)
    target = max(0.05, min(0.49, abs(float(short_delta or DEFAULT_SHORT_DELTA))))
    is_call = direction == "bear"
    lo = S * (1.001 if is_call else 0.50)
    hi = S * (1.60 if is_call else 0.999)
    for _ in range(42):
        mid = (lo + hi) / 2.0
        d = abs(_bs_delta(S, mid, dte, iv_pct, is_call))
        if is_call:
            # Higher strike lowers call delta.
            if d > target:
                lo = mid
            else:
                hi = mid
        else:
            # Higher strike raises absolute put delta.
            if d > target:
                hi = mid
            else:
                lo = mid
    return (lo + hi) / 2.0


def _strike_increment(symbol: str, spot: float) -> float:
    s = symbol.upper()
    if s in {"SPY", "QQQ", "IWM", "DIA", "TLT", "XLF", "XLK", "XLE"}:
        return 1.0
    if s in {"SPX", "NDX", "RUT"}:
        return 5.0
    if spot >= 300:
        return 5.0
    if spot >= 100:
        return 2.5
    return 1.0


def _round_to_increment(value: float, inc: float) -> float:
    try:
        return round(round(float(value) / float(inc)) * float(inc), 2)
    except Exception:
        return round(float(value), 2)


def _approx_expiry_for_dte(target_dte: int) -> Tuple[str, int]:
    base = date.today() + timedelta(days=max(1, int(target_dte or DEFAULT_DTE)))
    # Prefer Friday expiries for listed options. If target lands after Friday, use the next Friday.
    days_to_friday = (4 - base.weekday()) % 7
    exp = base + timedelta(days=days_to_friday)
    return exp.isoformat(), (exp - date.today()).days


def _estimate_trade_fast(
    symbol: str,
    spot: float,
    target_dte: int,
    direction: str,
    trade_filter: str,
    width: float,
    short_delta: float,
    target_rr: float,
    min_rr: float,
    iv_proxy: float,
) -> Optional[Dict[str, Any]]:
    """Build a fast, approximate trade preview using only spot + BS math.

    Exact option chain, OI, and OI-change are loaded only when the user expands
    the tile. This keeps watchlist scans fast.
    """
    expiry, actual_dte = _approx_expiry_for_dte(target_dte)
    filt = (trade_filter or "AUTO").upper()
    inc = _strike_increment(symbol, spot)
    width = max(inc, _round_to_increment(width, inc))

    def vertical(dirn: str) -> Dict[str, Any]:
        is_bull = dirn == "bull"
        short_k_raw = _target_delta_strike(spot, actual_dte, iv_proxy, "bull" if is_bull else "bear", short_delta)
        short_k = _round_to_increment(short_k_raw, inc)
        if is_bull and short_k >= spot:
            short_k = _round_to_increment(spot - inc, inc)
        if (not is_bull) and short_k <= spot:
            short_k = _round_to_increment(spot + inc, inc)
        long_k = _round_to_increment(short_k - width if is_bull else short_k + width, inc)
        is_call = not is_bull
        short_mid = _bs_price(spot, short_k, actual_dte, iv_proxy, is_call)
        long_mid = _bs_price(spot, long_k, actual_dte, iv_proxy, is_call)
        credit = round(max(0.01, short_mid - long_mid), 2)
        actual_width = round(abs(short_k - long_k), 2)
        max_loss = round(max(0.01, actual_width - credit), 2)
        rr = round(credit / max_loss, 2) if max_loss > 0 else 0.0
        delta = _bs_delta(spot, short_k, actual_dte, iv_proxy, is_call)
        return {
            "trade_type": "PS" if is_bull else "CS",
            "bias": "Bullish" if is_bull else "Bearish",
            "direction": dirn,
            "expiry": expiry,
            "dte": actual_dte,
            "requested_dte": target_dte,
            "actual_dte": actual_dte,
            "sell_strike": short_k,
            "buy_strike": long_k,
            "legs": f"Preview: Sell {short_k}{'P' if is_bull else 'C'} / Buy {long_k}{'P' if is_bull else 'C'}",
            "width": actual_width,
            "credit": credit,
            "max_loss": max_loss,
            "rr": rr,
            "target_rr": target_rr,
            "min_rr": min_rr,
            "short_delta": round(delta, 3),
            "short_oi": None,
            "short_oi_change": None,
            "short_oi_change_pct": None,
            "iv_proxy": iv_proxy,
            "approximate": True,
            "details_pending": True,
            "oi_pending": True,
        }

    def debit(dirn: str) -> Dict[str, Any]:
        is_call = dirn == "bull"
        k = _round_to_increment(spot, inc)
        premium = _bs_price(spot, k, actual_dte, iv_proxy, is_call)
        return {
            "trade_type": "CALL" if is_call else "PUT",
            "bias": "Bullish" if is_call else "Bearish",
            "direction": dirn,
            "expiry": expiry,
            "dte": actual_dte,
            "requested_dte": target_dte,
            "actual_dte": actual_dte,
            "buy_strike": k,
            "legs": f"Preview: Buy {k}{'C' if is_call else 'P'}",
            "debit": premium,
            "max_loss": premium,
            "rr": 1.0,
            "target_rr": 1.0,
            "min_rr": 0.0,
            "short_oi": None,
            "short_oi_change": None,
            "short_oi_change_pct": None,
            "iv_proxy": iv_proxy,
            "approximate": True,
            "details_pending": True,
            "oi_pending": True,
        }

    def ic() -> Dict[str, Any]:
        ps = vertical("bull")
        cs = vertical("bear")
        total_credit = round((ps.get("credit") or 0) + (cs.get("credit") or 0), 2)
        max_loss = round(max(ps.get("max_loss") or 0, cs.get("max_loss") or 0), 2)
        rr = round(total_credit / max_loss, 2) if max_loss > 0 else 0.0
        return {
            "trade_type": "IC",
            "bias": "Neutral",
            "direction": "neutral",
            "expiry": expiry,
            "dte": actual_dte,
            "requested_dte": target_dte,
            "actual_dte": actual_dte,
            "put_sell": ps["sell_strike"], "put_buy": ps["buy_strike"],
            "call_sell": cs["sell_strike"], "call_buy": cs["buy_strike"],
            "legs": f"Preview: Sell {ps['sell_strike']}P / Buy {ps['buy_strike']}P · Sell {cs['sell_strike']}C / Buy {cs['buy_strike']}C",
            "credit": total_credit,
            "max_loss": max_loss,
            "rr": rr,
            "target_rr": target_rr,
            "min_rr": min_rr,
            "width": width,
            "short_oi": None,
            "short_oi_change": None,
            "short_oi_change_pct": None,
            "iv_proxy": iv_proxy,
            "approximate": True,
            "details_pending": True,
            "oi_pending": True,
        }

    if filt == "PS":
        return vertical("bull")
    if filt == "CS":
        return vertical("bear")
    if filt == "CALL":
        return debit("bull")
    if filt == "PUT":
        return debit("bear")
    if filt == "IC" or direction == "neutral":
        return ic()
    if direction == "bull":
        return vertical("bull")
    if direction == "bear":
        return vertical("bear")
    return ic()


def _ema(series, span: int):
    return series.ewm(span=span, adjust=False).mean()


def _rma(series, length: int):
    # TradingView ta.rma approximation with alpha=1/length.
    return series.ewm(alpha=1.0 / max(1, int(length)), adjust=False).mean()


def _rsi(series, length: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = _rma(gain, length)
    avg_loss = _rma(loss, length).replace(0, 1e-9)
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _compute_uae(df, tf: str) -> Optional[Dict[str, Any]]:
    import pandas as pd

    if df is None or df.empty or len(df) < 35:
        return None
    p = TF_PARAMS.get(tf, TF_PARAMS["1d"])
    fast_len = int(p["fast"])
    slow_len = int(p["slow"])
    signal_len = int(p["signal"])
    roc_len = int(p["roc"])
    slope_len = int(p["slope"])
    adx_thr = float(p["adx_thr"])
    adx_len = 14
    adx_smooth_len = 3
    atr_len = 14
    vol_mult = 1.5
    hist_pct_thr = 60
    hist_lookback = 100
    rsi_len = 14
    rsi_ema_len = 90
    rsi_trend_thr = 12.0
    rsi_ob_thr = 20.0
    rsi_os_thr = -20.0
    min_slope_thr = 0.001

    d = df.copy()
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["Open", "High", "Low", "Close"])
    if len(d) < max(35, slow_len + slope_len + 5):
        return None

    high, low, close = d["High"], d["Low"], d["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atrv = _rma(tr, atr_len)
    atr_base = _ema(atrv, slow_len)
    safe_atr = atr_base.clip(lower=1e-6)

    slow_ema = _ema(close, slow_len)
    fast_ema = _ema(close, fast_len)
    trend_pos = (close - slow_ema) / safe_atr
    # Pine uses nz(trendPos[rocLen]), so warm-up shifted values are zero.
    trend_mom = trend_pos - trend_pos.shift(roc_len).fillna(0.0)
    vol_amp = (atrv / safe_atr).clip(lower=0.1)
    raw_sig = trend_mom * (vol_amp ** vol_mult)

    macd_line = _ema(raw_sig, fast_len)
    signal_line = _ema(macd_line, signal_len)
    hist = macd_line - signal_line
    hist_abs = hist.abs()
    # Match Pine ta.percentile_linear_interpolation(abs(hist), 100, 60): no partial-window threshold.
    hist_thresh = hist_abs.rolling(hist_lookback, min_periods=hist_lookback).quantile(hist_pct_thr / 100.0)
    is_strong_hist = hist_abs > hist_thresh

    rsi_val = _rsi(close, rsi_len)
    rsi_ema_val = _ema(rsi_val, rsi_ema_len)
    rsi_diff = rsi_val - rsi_ema_val

    up_move = high - high.shift(1)
    dn_move = low.shift(1) - low
    plus_dm = up_move.where((up_move > dn_move) & (up_move > 0), 0.0)
    minus_dm = dn_move.where((dn_move > up_move) & (dn_move > 0), 0.0)
    sm_tr = _rma(tr, adx_len).clip(lower=1e-6)
    pdi = 100 * _rma(plus_dm, adx_len) / sm_tr
    mdi = 100 * _rma(minus_dm, adx_len) / sm_tr
    di_sum = (pdi + mdi).clip(lower=1e-6)
    adx_raw = 100 * _rma((pdi - mdi).abs() / di_sum, adx_len)
    adx_val = _ema(adx_raw, adx_smooth_len)

    ema_slope = (slow_ema - slow_ema.shift(slope_len)) / max(1, slope_len) / safe_atr
    # Align with AJ Trend/Vol Analyzer v5 Pine: RSIdiff threshold is the primary trend gate,
    # with smoothed ADX as the secondary backup.
    is_trending = (rsi_diff > rsi_trend_thr) | (rsi_diff < -rsi_trend_thr) | (adx_val > adx_thr)
    is_bull_slope = ema_slope > min_slope_thr
    is_bear_slope = ema_slope < -min_slope_thr

    reg_bull = is_trending & is_bull_slope & (hist > 0)
    reg_weak_bull = is_trending & is_bull_slope & (hist <= 0)
    reg_bear = is_trending & is_bear_slope & (hist < 0)
    reg_weak_bear = is_trending & is_bear_slope & (hist >= 0)

    def _regime_at(i: int) -> str:
        if bool(reg_bull.iloc[i]):
            return "BULL"
        if bool(reg_weak_bull.iloc[i]):
            return "WEAK_BULL"
        if bool(reg_bear.iloc[i]):
            return "BEAR"
        if bool(reg_weak_bear.iloc[i]):
            return "WEAK_BEAR"
        return "SIDEWAYS"

    i = len(d) - 1
    j = len(d) - 2
    h0, h1 = _safe_float(hist.iloc[i], 0.0), _safe_float(hist.iloc[j], 0.0)
    m0, m1 = _safe_float(macd_line.iloc[i], 0.0), _safe_float(macd_line.iloc[j], 0.0)
    adx0, adx1 = _safe_float(adx_val.iloc[i], 0.0), _safe_float(adx_val.iloc[j], 0.0)
    strong0 = bool(is_strong_hist.iloc[i]) if not pd.isna(is_strong_hist.iloc[i]) else False
    trending0 = bool(is_trending.iloc[i]) if not pd.isna(is_trending.iloc[i]) else False
    rdiff0 = _safe_float(rsi_diff.iloc[i], 0.0)
    rdiff1 = _safe_float(rsi_diff.iloc[j], 0.0)
    fade_sell = (rdiff1 >= rsi_ob_thr) and (rdiff0 < rsi_ob_thr)
    fade_buy = (rdiff1 <= rsi_os_thr) and (rdiff0 > rsi_os_thr)
    fade_any = fade_sell or fade_buy

    bull_triangle = (m1 <= 0 < m0) and trending0 and strong0 and not fade_any
    bear_triangle = (m1 >= 0 > m0) and trending0 and strong0 and not fade_any
    bull_circle = (h1 <= 0 < h0) and trending0
    bear_circle = (h1 >= 0 > h0) and trending0
    bull_diamond = strong0 and h0 > 0 and trending0
    bear_diamond = strong0 and h0 < 0 and trending0

    # Momentum direction includes both strong triangle/circle and continuation diamond.
    bull_confluence = int(bull_triangle) + int(bull_circle) + int(bull_diamond)
    bear_confluence = int(bear_triangle) + int(bear_circle) + int(bear_diamond)

    hist_dir_bull = h0 > h1 and h0 > 0
    hist_dir_bear = h0 < h1 and h0 < 0
    macd_above_zero = m0 > 0

    regime = _regime_at(i)
    prev_regime = _regime_at(j)
    direction_bias = "bull" if regime in ("BULL", "WEAK_BULL") else "bear" if regime in ("BEAR", "WEAK_BEAR") else "sideways"

    # Daily S/R helpers use recent high/low levels.
    look = min(60, len(d))
    recent_high = _safe_float(high.tail(look).max(), None, 2)
    recent_low = _safe_float(low.tail(look).min(), None, 2)
    atr0 = _safe_float(atrv.iloc[i], None, 2)

    return {
        "tf": tf,
        "label": _tf_label(tf),
        "bars": len(d),
        "last_time": d.index[-1].isoformat() if hasattr(d.index[-1], "isoformat") else str(d.index[-1]),
        "close": _safe_float(close.iloc[i], None, 2),
        "atr": atr0,
        "slow_ema": _safe_float(slow_ema.iloc[i], None, 2),
        "fast_ema": _safe_float(fast_ema.iloc[i], None, 2),
        "macd_line": _safe_float(m0, None, 4),
        "signal_line": _safe_float(signal_line.iloc[i], None, 4),
        "hist": _safe_float(h0, None, 4),
        "hist_prev": _safe_float(h1, None, 4),
        "hist_thresh": _safe_float(hist_thresh.iloc[i], None, 4),
        "is_strong_hist": strong0,
        "rsi": _safe_float(rsi_val.iloc[i], None, 2),
        "rsi_ema_90": _safe_float(rsi_ema_val.iloc[i], None, 2),
        "rsidiff": _safe_float(rdiff0, None, 2),
        "adx": _safe_float(adx0, None, 2),
        "adx_prev": _safe_float(adx1, None, 2),
        "adx_thr": adx_thr,
        "adx_rising": bool(adx0 > adx1),
        "is_trending": trending0,
        "ema_slope": _safe_float(ema_slope.iloc[i], None, 4),
        "regime": regime,
        "prev_regime": prev_regime,
        "regime_changed": regime != prev_regime,
        "direction_bias": direction_bias,
        "bull_triangle": bool(bull_triangle),
        "bear_triangle": bool(bear_triangle),
        "bull_triangle_strong": bool(bull_triangle and strong0),
        "bear_triangle_strong": bool(bear_triangle and strong0),
        "bull_circle": bool(bull_circle),
        "bear_circle": bool(bear_circle),
        "bull_diamond": bool(bull_diamond),
        "bear_diamond": bool(bear_diamond),
        "bull_fade_arrow": bool(fade_buy),
        "bear_fade_arrow": bool(fade_sell),
        "bull_confluence": bull_confluence,
        "bear_confluence": bear_confluence,
        "hist_dir_bull": bool(hist_dir_bull),
        "hist_dir_bear": bool(hist_dir_bear),
        "macd_above_zero": bool(macd_above_zero),
        "recent_high": recent_high,
        "recent_low": recent_low,
    }


def _fetch_uae_bundle(symbol: str, plan: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    out: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for tf in plan.get("required_tfs", []):
        try:
            df = _yf_history(symbol, tf)
            ind = _compute_uae(df, tf)
            if ind:
                out[tf] = ind
            else:
                errors[tf] = "not enough OHLCV bars"
        except Exception as e:
            errors[tf] = str(e)[:140]
    return out, errors


# ─────────────────────────────────────────────────────────────────────────────
# Option chain / strikes / OI enrichment
# ─────────────────────────────────────────────────────────────────────────────


def _pick_expiry(symbol: str, target_dte: int) -> Tuple[Optional[str], int, List[str]]:
    """Pick an expiry DB-first, falling back to yfinance only when needed."""
    sym = (symbol or "").upper().strip()
    today = date.today()
    opts: List[str] = []
    try:
        con = _conn()
        rows = con.execute(
            "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration",
            (sym, today.isoformat()),
        ).fetchall()
        con.close()
        opts = [str(r["expiration"]) for r in rows if r["expiration"]]
    except Exception:
        opts = []
    if not opts:
        try:
            import yfinance as yf
            opts = list(yf.Ticker(sym).options or [])
        except Exception:
            opts = []
    candidates = []
    for exp in opts:
        try:
            d = datetime.strptime(exp, "%Y-%m-%d").date()
            dte = (d - today).days
            if dte >= max(1, target_dte - 5):
                is_friday = 1 if d.weekday() == 4 else 0
                candidates.append((exp, dte, abs(dte - target_dte), -is_friday))
        except Exception:
            continue
    if not candidates:
        return None, 0, opts
    candidates.sort(key=lambda x: (x[2], x[3], x[1]))
    return candidates[0][0], candidates[0][1], opts


def _mid_price(row: Any, is_call: bool, spot: float, dte: int, fallback_iv: float) -> float:
    bid = _safe_float(row.get("bid"), None)
    ask = _safe_float(row.get("ask"), None)
    last = _safe_float(row.get("lastPrice"), None)
    if bid is not None and ask is not None and ask > 0 and ask >= bid:
        return round((bid + ask) / 2.0, 2)
    if last is not None and last > 0:
        return round(last, 2)
    strike = _safe_float(row.get("strike"), spot)
    iv = _safe_float(row.get("impliedVolatility"), None)
    iv_pct = (iv * 100.0) if iv and iv < 3 else (iv or fallback_iv)
    return _bs_price(spot, float(strike), dte, iv_pct, is_call)


def _bs_price(S: float, K: float, T_days: int, iv_pct: float, is_call: bool) -> float:
    try:
        T = max(float(T_days), 1.0) / 365.0
        sig = max(float(iv_pct or 20.0) / 100.0, 0.05)
        sqt = math.sqrt(T)
        d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * sqt)
        d2 = d1 - sig * sqt

        def cdf(x: float) -> float:
            return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

        if is_call:
            return round(max(0.01, S * cdf(d1) - K * cdf(d2)), 2)
        return round(max(0.01, K * cdf(-d2) - S * cdf(-d1)), 2)
    except Exception:
        intrinsic = max(0.0, (S - K) if is_call else (K - S))
        return round(max(0.05, intrinsic + 0.20), 2)


def _bs_delta(S: float, K: float, T_days: int, iv_pct: float, is_call: bool) -> float:
    try:
        T = max(float(T_days), 1.0) / 365.0
        sig = max(float(iv_pct or 20.0) / 100.0, 0.05)
        d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
        cdf = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        return round(cdf if is_call else cdf - 1.0, 3)
    except Exception:
        return 0.5 if is_call else -0.5


def _hist_iv_proxy(symbol: str) -> float:
    sym = (symbol or "").upper().strip()
    try:
        ts, val = _HIST_IV_CACHE.get(sym, (0, None))
        if val is not None and time.time() - ts <= PRICE_CACHE_TTL_SECONDS:
            return float(val)
    except Exception:
        pass
    try:
        import yfinance as yf
        df = yf.Ticker(sym).history(period="3mo", interval="1d", auto_adjust=False, actions=False)
        df = _normalise_ohlcv(df)
        if df.empty or len(df) < 25:
            val = 25.0
        else:
            close = df["Close"].astype(float)
            rets = (close / close.shift(1)).apply(lambda x: math.log(x) if x and x > 0 else None).dropna()
            val = 25.0 if len(rets) < 10 else round(float(rets.tail(30).std() * math.sqrt(252) * 100), 1)
    except Exception:
        val = 25.0
    try:
        _HIST_IV_CACHE[sym] = (time.time(), float(val))
        if len(_HIST_IV_CACHE) > 64:
            for k, _ in sorted(_HIST_IV_CACHE.items(), key=lambda kv: kv[1][0])[:16]:
                _HIST_IV_CACHE.pop(k, None)
    except Exception:
        pass
    return float(val)


def _chain_from_db(symbol: str, expiry: str) -> Tuple[Optional[Any], Optional[Any], str]:
    """Build yfinance-like calls/puts DataFrames from the local options table."""
    try:
        import pandas as pd
    except Exception as exc:
        return None, None, f"local-db unavailable: pandas import failed: {exc}"
    sym = (symbol or "").upper().strip()
    con = _conn()
    try:
        drow = con.execute(
            "SELECT MAX(date) AS d FROM options WHERE symbol=? AND expiration=?",
            (sym, expiry),
        ).fetchone()
        latest = drow["d"] if drow else None
        if not latest:
            return None, None, "local-db: no rows"
        rows = con.execute(
            """
            SELECT type, strike, SUM(oi) AS openInterest, SUM(volume) AS volume,
                   CASE WHEN SUM(oi)>0 THEN SUM(COALESCE(price,0)*oi)/SUM(oi) ELSE AVG(price) END AS lastPrice
            FROM options
            WHERE symbol=? AND expiration=? AND date=?
            GROUP BY type, strike
            HAVING SUM(oi)>0
            ORDER BY strike
            """,
            (sym, expiry, latest),
        ).fetchall()
    except Exception as exc:
        return None, None, f"local-db error: {str(exc)[:100]}"
    finally:
        con.close()
    calls, puts = [], []
    for r in rows:
        try:
            price = _safe_float(r["lastPrice"], 0.0) or 0.0
            item = {
                "strike": _safe_float(r["strike"], 0.0) or 0.0,
                "bid": 0.0,
                "ask": 0.0,
                "lastPrice": price,
                "impliedVolatility": None,
                "openInterest": _safe_int(r["openInterest"], 0),
                "volume": _safe_int(r["volume"], 0),
            }
            if str(r["type"] or "").lower() == "call":
                calls.append(item)
            elif str(r["type"] or "").lower() == "put":
                puts.append(item)
        except Exception:
            continue
    calls_df = pd.DataFrame(calls) if calls else None
    puts_df = pd.DataFrame(puts) if puts else None
    if calls_df is None or puts_df is None:
        return calls_df, puts_df, f"local-db partial snapshot {latest}"
    return calls_df, puts_df, f"local-db snapshot {latest}"


def _get_chain(symbol: str, expiry: str) -> Tuple[Optional[Any], Optional[Any], str]:
    sym = (symbol or "").upper().strip()
    key = f"{sym}|{expiry}"
    try:
        ts, calls_cached, puts_cached, src_cached = _OPTION_CHAIN_CACHE.get(key, (0, None, None, ""))
        if (calls_cached is not None or puts_cached is not None) and time.time() - ts <= OPTION_CHAIN_CACHE_TTL_SECONDS:
            return calls_cached, puts_cached, src_cached + " cached"
    except Exception:
        pass

    # Prefer local snapshots; they are what the OI/GEX dashboards use and avoid
    # repeated live option-chain calls during AI Hub best-strategy ranking.
    calls, puts, src = _chain_from_db(sym, expiry)
    if calls is not None and puts is not None:
        try:
            _OPTION_CHAIN_CACHE[key] = (time.time(), calls, puts, src)
        except Exception:
            pass
        return calls, puts, src

    try:
        import yfinance as yf
        ch = yf.Ticker(sym).option_chain(expiry)
        calls = ch.calls.copy() if ch and ch.calls is not None else None
        puts = ch.puts.copy() if ch and ch.puts is not None else None
        src = "yfinance"
    except Exception as e:
        return calls, puts, (src + "; " if src else "") + str(e)[:120]
    try:
        _OPTION_CHAIN_CACHE[key] = (time.time(), calls, puts, src)
        if len(_OPTION_CHAIN_CACHE) > 48:
            for k, _ in sorted(_OPTION_CHAIN_CACHE.items(), key=lambda kv: kv[1][0])[:16]:
                _OPTION_CHAIN_CACHE.pop(k, None)
    except Exception:
        pass
    return calls, puts, src


def _db_oi_change(symbol: str, expiry: str, opt_type: str, strike: float) -> Dict[str, Any]:
    """Return latest/previous OI from SQLite options snapshots for one exact strike."""
    out = {"oi_change": None, "oi_change_pct": None, "latest_oi": None, "prev_oi": None, "latest_date": None, "prev_date": None}
    try:
        con = _conn()
        dates = con.execute(
            "SELECT DISTINCT date FROM options WHERE symbol=? AND expiration=? ORDER BY date DESC LIMIT 2",
            (symbol.upper(), expiry),
        ).fetchall()
        if not dates:
            con.close()
            return out
        latest_date = dates[0][0]
        prev_date = dates[1][0] if len(dates) > 1 else None
        latest = con.execute(
            "SELECT SUM(oi) AS oi FROM options WHERE symbol=? AND expiration=? AND type=? AND ABS(strike-?)<0.0001 AND date=?",
            (symbol.upper(), expiry, opt_type.lower(), float(strike), latest_date),
        ).fetchone()
        latest_oi = _safe_int(latest["oi"] if latest else None, 0)
        prev_oi = None
        if prev_date:
            prev = con.execute(
                "SELECT SUM(oi) AS oi FROM options WHERE symbol=? AND expiration=? AND type=? AND ABS(strike-?)<0.0001 AND date=?",
                (symbol.upper(), expiry, opt_type.lower(), float(strike), prev_date),
            ).fetchone()
            prev_oi = _safe_int(prev["oi"] if prev else None, 0)
        con.close()
        out["latest_oi"] = latest_oi
        out["prev_oi"] = prev_oi
        out["latest_date"] = latest_date
        out["prev_date"] = prev_date
        if prev_oi is not None:
            chg = latest_oi - prev_oi
            out["oi_change"] = chg
            out["oi_change_pct"] = round(chg / max(1, prev_oi) * 100.0, 2) if prev_oi else None
    except Exception:
        pass
    return out


def _leg_from_row(symbol: str, expiry: str, row: Any, opt_type: str, spot: float, dte: int, fallback_iv: float) -> Dict[str, Any]:
    strike = _safe_float(row.get("strike"), None, 2)
    if strike is None:
        strike = 0.0
    is_call = opt_type.lower() == "call"
    iv_raw = _safe_float(row.get("impliedVolatility"), None)
    iv_pct = (iv_raw * 100.0) if iv_raw and iv_raw < 3 else (iv_raw or fallback_iv)
    mid = _mid_price(row, is_call, spot, dte, fallback_iv)
    oi_chain = _safe_int(row.get("openInterest"), 0)
    vol = _safe_int(row.get("volume"), 0)
    db_oi = _db_oi_change(symbol, expiry, opt_type, strike)
    latest_oi = db_oi.get("latest_oi") if db_oi.get("latest_oi") is not None else oi_chain
    return {
        "type": opt_type.lower(),
        "strike": strike,
        "mid": mid,
        "bid": _safe_float(row.get("bid"), None, 2),
        "ask": _safe_float(row.get("ask"), None, 2),
        "last": _safe_float(row.get("lastPrice"), None, 2),
        "iv_pct": _safe_float(iv_pct, fallback_iv, 1),
        "delta": _bs_delta(spot, strike, dte, iv_pct or fallback_iv, is_call),
        "open_interest": _safe_int(latest_oi, oi_chain),
        "chain_open_interest": oi_chain,
        "volume": vol,
        "oi_change": db_oi.get("oi_change"),
        "oi_change_pct": db_oi.get("oi_change_pct"),
        "oi_latest_date": db_oi.get("latest_date"),
        "oi_prev_date": db_oi.get("prev_date"),
    }


def _nearest_row(df, strike: float):
    if df is None or df.empty:
        return None
    try:
        tmp = df.copy()
        tmp["_dist"] = (tmp["strike"].astype(float) - float(strike)).abs()
        return tmp.sort_values("_dist").iloc[0]
    except Exception:
        return None


def _option_rows_sorted(df):
    if df is None or df.empty or "strike" not in df.columns:
        return []
    try:
        return [r for _, r in df.sort_values("strike").iterrows()]
    except Exception:
        return []


def _choose_vertical(
    symbol: str,
    expiry: str,
    dte: int,
    spot: float,
    direction: str,
    width: float,
    short_delta_target: float,
    target_rr: float,
    min_rr: float,
    fallback_iv: float,
    calls: Any,
    puts: Any,
) -> Optional[Dict[str, Any]]:
    is_bull = direction == "bull"
    opt_type = "put" if is_bull else "call"
    df = puts if is_bull else calls
    rows = _option_rows_sorted(df)
    if not rows:
        return None
    candidates = []
    side_max_oi = 0
    try:
        for _r in rows:
            side_max_oi = max(side_max_oi, _safe_int(_r.get("openInterest"), 0))
    except Exception:
        side_max_oi = 0
    for row in rows:
        short_k = _safe_float(row.get("strike"), None)
        if short_k is None:
            continue
        if is_bull and short_k >= spot:
            continue
        if (not is_bull) and short_k <= spot:
            continue
        long_k = short_k - abs(width) if is_bull else short_k + abs(width)
        long_row = _nearest_row(df, long_k)
        if long_row is None:
            continue
        long_k_real = _safe_float(long_row.get("strike"), None)
        if long_k_real is None:
            continue
        if is_bull and long_k_real >= short_k:
            continue
        if (not is_bull) and long_k_real <= short_k:
            continue
        short_leg = _leg_from_row(symbol, expiry, row, opt_type, spot, dte, fallback_iv)
        long_leg = _leg_from_row(symbol, expiry, long_row, opt_type, spot, dte, fallback_iv)
        credit = round(max(0.0, short_leg["mid"] - long_leg["mid"]), 2)
        actual_width = round(abs(short_leg["strike"] - long_leg["strike"]), 2)
        if actual_width <= 0 or credit <= 0:
            continue
        max_loss = round(max(0.01, actual_width - credit), 2)
        rr = round(credit / max_loss, 2)
        abs_delta = abs(short_leg.get("delta") or 0.0)
        otm_pct = abs(short_leg["strike"] - spot) / max(spot, 0.01) * 100.0
        oi = short_leg.get("open_interest") or 0
        # Better rank: near 1:1 RR, adequate OI, close to requested delta, not too far OTM.
        # Do not let a mathematically attractive spread with 0-1 OI beat an
        # active strike.  The final guardrail still blocks ultra-thin shorts,
        # but the selector should prefer real OI walls when they exist.
        rr_penalty = abs(rr - target_rr)
        delta_penalty = abs(abs_delta - short_delta_target)
        oi_floor = max(25, int(side_max_oi * 0.10)) if side_max_oi > 0 else 25
        oi_ok = oi >= oi_floor
        oi_bonus = min(1.5, math.log1p(max(oi, 0)) / math.log1p(max(side_max_oi, 1))) if side_max_oi > 0 else min(1.0, oi / 100.0)
        thin_oi_penalty = 1 if oi < 25 else 0
        rank = (rr >= min_rr, oi_ok, oi_bonus, -thin_oi_penalty, -rr_penalty, -delta_penalty, -otm_pct)
        candidates.append((rank, rr, {
            "trade_type": "PS" if is_bull else "CS",
            "bias": "Bullish" if is_bull else "Bearish",
            "direction": direction,
            "expiry": expiry,
            "dte": dte,
            "sell_strike": short_leg["strike"],
            "buy_strike": long_leg["strike"],
            "legs": f"Sell {short_leg['strike']}{'P' if is_bull else 'C'} / Buy {long_leg['strike']}{'P' if is_bull else 'C'}",
            "short_leg": short_leg,
            "long_leg": long_leg,
            "width": actual_width,
            "credit": credit,
            "max_loss": max_loss,
            "rr": rr,
            "target_rr": target_rr,
            "min_rr": min_rr,
            "otm_pct": round(otm_pct, 2),
            "short_delta": round(short_leg.get("delta") or 0.0, 3),
            "short_oi": short_leg.get("open_interest"),
            "short_oi_change": short_leg.get("oi_change"),
            "short_oi_change_pct": short_leg.get("oi_change_pct"),
        }))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    # Prefer candidates that pass min RR. If none pass, return best with warning so UI can show why it was filtered.
    return candidates[0][2]


def _choose_debit(
    symbol: str,
    expiry: str,
    dte: int,
    spot: float,
    direction: str,
    fallback_iv: float,
    calls: Any,
    puts: Any,
) -> Optional[Dict[str, Any]]:
    is_call = direction == "bull"
    opt_type = "call" if is_call else "put"
    df = calls if is_call else puts
    if df is None or df.empty:
        return None
    # Use near-ATM 0.50 delta option.
    rows = _option_rows_sorted(df)
    candidates = []
    for row in rows:
        k = _safe_float(row.get("strike"), None)
        if k is None:
            continue
        if is_call and k < spot * 0.98:
            continue
        if (not is_call) and k > spot * 1.02:
            continue
        leg = _leg_from_row(symbol, expiry, row, opt_type, spot, dte, fallback_iv)
        delta_pen = abs(abs(leg.get("delta") or 0.5) - 0.50)
        candidates.append((delta_pen, abs(k - spot), leg))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    leg = candidates[0][2]
    premium = max(0.01, leg.get("mid") or 0.01)
    # Conservative RR proxy: one ATR move target vs half-premium risk.
    return {
        "trade_type": "CALL" if is_call else "PUT",
        "bias": "Bullish" if is_call else "Bearish",
        "direction": direction,
        "expiry": expiry,
        "dte": dte,
        "buy_strike": leg["strike"],
        "legs": f"Buy {leg['strike']}{'C' if is_call else 'P'}",
        "long_leg": leg,
        "debit": round(premium, 2),
        "max_loss": round(premium, 2),
        "rr": 1.0,
        "target_rr": 1.0,
        "min_rr": 0.0,
        "short_oi": None,
        "short_oi_change": None,
        "short_oi_change_pct": None,
    }


def _choose_ic(
    symbol: str,
    expiry: str,
    dte: int,
    spot: float,
    width: float,
    short_delta_target: float,
    target_rr: float,
    min_rr: float,
    fallback_iv: float,
    calls: Any,
    puts: Any,
) -> Optional[Dict[str, Any]]:
    ps = _choose_vertical(symbol, expiry, dte, spot, "bull", width, short_delta_target, target_rr, min_rr, fallback_iv, calls, puts)
    cs = _choose_vertical(symbol, expiry, dte, spot, "bear", width, short_delta_target, target_rr, min_rr, fallback_iv, calls, puts)
    if not ps or not cs:
        return None
    total_credit = round((ps.get("credit") or 0) + (cs.get("credit") or 0), 2)
    max_loss = round(max(ps.get("max_loss") or 0, cs.get("max_loss") or 0), 2)
    rr = round(total_credit / max_loss, 2) if max_loss > 0 else 0.0
    return {
        "trade_type": "IC",
        "bias": "Neutral",
        "direction": "neutral",
        "expiry": expiry,
        "dte": dte,
        "put_sell": ps["sell_strike"],
        "put_buy": ps["buy_strike"],
        "call_sell": cs["sell_strike"],
        "call_buy": cs["buy_strike"],
        "legs": f"Sell {ps['sell_strike']}P / Buy {ps['buy_strike']}P · Sell {cs['sell_strike']}C / Buy {cs['buy_strike']}C",
        "put_short_leg": ps["short_leg"],
        "call_short_leg": cs["short_leg"],
        "put_long_leg": ps["long_leg"],
        "call_long_leg": cs["long_leg"],
        "credit": total_credit,
        "max_loss": max_loss,
        "rr": rr,
        "target_rr": target_rr,
        "min_rr": min_rr,
        "width": width,
        "short_oi": (ps.get("short_oi") or 0) + (cs.get("short_oi") or 0),
        "short_oi_change": None if ps.get("short_oi_change") is None and cs.get("short_oi_change") is None else (ps.get("short_oi_change") or 0) + (cs.get("short_oi_change") or 0),
        "short_oi_change_pct": None,
    }


def _suggest_trade(
    symbol: str,
    spot: float,
    target_dte: int,
    direction: str,
    trade_filter: str,
    width: float,
    short_delta: float,
    target_rr: float,
    min_rr: float,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    expiry, dte, exps = _pick_expiry(symbol, target_dte)
    meta = {"requested_dte": target_dte, "available_expiries": exps[:8], "expiry_error": None}
    if not expiry:
        meta["expiry_error"] = "No listed expiry found near requested DTE"
        return None, meta
    calls, puts, chain_source = _get_chain(symbol, expiry)
    meta.update({"expiry": expiry, "actual_dte": dte, "chain_source": chain_source})
    if calls is None or puts is None:
        meta["expiry_error"] = f"Option chain unavailable: {chain_source}"
        return None, meta
    fallback_iv = _hist_iv_proxy(symbol)
    filt = (trade_filter or "AUTO").upper()

    if filt == "PS":
        trade = _choose_vertical(symbol, expiry, dte, spot, "bull", width, short_delta, target_rr, min_rr, fallback_iv, calls, puts)
    elif filt == "CS":
        trade = _choose_vertical(symbol, expiry, dte, spot, "bear", width, short_delta, target_rr, min_rr, fallback_iv, calls, puts)
    elif filt == "CALL":
        trade = _choose_debit(symbol, expiry, dte, spot, "bull", fallback_iv, calls, puts)
    elif filt == "PUT":
        trade = _choose_debit(symbol, expiry, dte, spot, "bear", fallback_iv, calls, puts)
    elif filt == "IC":
        trade = _choose_ic(symbol, expiry, dte, spot, width, short_delta, target_rr, min_rr, fallback_iv, calls, puts)
    else:
        if direction == "bull":
            trade = _choose_vertical(symbol, expiry, dte, spot, "bull", width, short_delta, target_rr, min_rr, fallback_iv, calls, puts)
        elif direction == "bear":
            trade = _choose_vertical(symbol, expiry, dte, spot, "bear", width, short_delta, target_rr, min_rr, fallback_iv, calls, puts)
        else:
            trade = _choose_ic(symbol, expiry, dte, spot, width, short_delta, target_rr, min_rr, fallback_iv, calls, puts)
    if trade:
        trade["requested_dte"] = target_dte
        trade["actual_dte"] = dte
        trade["expiry"] = expiry
        trade["iv_proxy"] = fallback_iv
    return trade, meta


# ─────────────────────────────────────────────────────────────────────────────
# Market context / guide scoring
# ─────────────────────────────────────────────────────────────────────────────


def _get_earn_days(symbol: str) -> int:
    try:
        from .earnings_calendar import get_earnings_info
        info = get_earnings_info(symbol) or {}
        days = info.get("earn_days")
        return int(days) if days is not None else 999
    except Exception:
        return 999


def _wall_proxy_from_chain(symbol: str, expiry: str) -> Dict[str, Any]:
    """Simple dealer/OI proxy: largest call and put OI strikes for chosen DTE."""
    out = {"put_wall": None, "call_wall": None, "top_put_walls": [], "top_call_walls": [], "pcr": None}
    try:
        calls, puts, _ = _get_chain(symbol, expiry)
        if calls is not None and not calls.empty:
            c = calls.copy()
            c["openInterest"] = c["openInterest"].fillna(0).astype(float)
            top = c.sort_values("openInterest", ascending=False).head(5)
            out["top_call_walls"] = [float(x) for x in top["strike"].tolist()]
            out["call_wall"] = out["top_call_walls"][0] if out["top_call_walls"] else None
        if puts is not None and not puts.empty:
            p = puts.copy()
            p["openInterest"] = p["openInterest"].fillna(0).astype(float)
            top = p.sort_values("openInterest", ascending=False).head(5)
            out["top_put_walls"] = [float(x) for x in top["strike"].tolist()]
            out["put_wall"] = out["top_put_walls"][0] if out["top_put_walls"] else None
        csum = float(calls["openInterest"].fillna(0).sum()) if calls is not None and "openInterest" in calls else 0.0
        psum = float(puts["openInterest"].fillna(0).sum()) if puts is not None and "openInterest" in puts else 0.0
        out["pcr"] = round(psum / csum, 2) if csum else None
    except Exception:
        pass
    return out


def _spy_gex_context(symbol: str, expiry: str, dte: int, spot: float, iv_proxy: float) -> Dict[str, Any]:
    if symbol.upper() not in {"SPY", "SPX", "QQQ", "IWM"}:
        return {"available": False, "note": "GEX/dealer check is only applied to SPY/index-style symbols."}
    try:
        from .spy_strategies import _compute_gex, _oi_rows
        rows = _oi_rows(symbol.upper(), expiry)
        if not rows:
            return {"available": False, "note": "No local OI rows for GEX calculation. Run Scheduler to populate option OI."}
        gex = _compute_gex(rows, spot, dte, iv_proxy or 25.0) or {}
        return {
            "available": True,
            "total_gex": _safe_float(gex.get("total_gex"), None, 2),
            "gex_ratio": _safe_float(gex.get("gex_ratio"), None, 4),
            "regime": gex.get("regime"),
            "gamma_flip": _safe_float(gex.get("gamma_flip"), None, 2),
            "pin_strike": _safe_float(gex.get("pin_strike"), None, 2),
            "max_pain": _safe_float(gex.get("max_pain"), None, 2),
            "note": "GEX computed from local option OI snapshot.",
        }
    except Exception as e:
        return {"available": False, "note": f"GEX unavailable: {str(e)[:100]}"}


def _regime_matches_direction(regime: str, direction: str, allow_weak: bool = True) -> bool:
    r = (regime or "").upper()
    if direction == "bull":
        return r == "BULL" or (allow_weak and r == "WEAK_BULL")
    if direction == "bear":
        return r == "BEAR" or (allow_weak and r == "WEAK_BEAR")
    if direction == "neutral":
        return r == "SIDEWAYS"
    return False


def _choose_direction(indicators: Dict[str, Any], plan: Dict[str, Any], trade_filter: str) -> Tuple[str, str]:
    filt = (trade_filter or "AUTO").upper()
    if filt in {"PS", "CALL"}:
        return "bull", f"Forced by strategy filter {filt}"
    if filt in {"CS", "PUT"}:
        return "bear", f"Forced by strategy filter {filt}"
    if filt == "IC":
        return "neutral", "Forced by strategy filter IC"

    entry = indicators.get(plan["entry_tf"], {})
    bias = indicators.get(plan["bias_tf"], {})
    macro = indicators.get(plan["macro_tf"], {})

    bull_setup = (
        _regime_matches_direction(bias.get("regime"), "bull") and
        (entry.get("bull_confluence", 0) >= 2 or (entry.get("bull_diamond") and entry.get("hist_dir_bull")) or _regime_matches_direction(entry.get("regime"), "bull"))
    )
    bear_setup = (
        _regime_matches_direction(bias.get("regime"), "bear") and
        (entry.get("bear_confluence", 0) >= 2 or (entry.get("bear_diamond") and entry.get("hist_dir_bear")) or _regime_matches_direction(entry.get("regime"), "bear"))
    )
    if macro and macro.get("regime") == "SIDEWAYS" and not (bull_setup or bear_setup):
        return "neutral", "Macro regime is SIDEWAYS and no directional confluence."
    if bull_setup and not bear_setup:
        return "bull", "Entry signals and higher-TF bias lean bullish."
    if bear_setup and not bull_setup:
        return "bear", "Entry signals and higher-TF bias lean bearish."
    if entry.get("regime") == "SIDEWAYS" and bias.get("regime") == "SIDEWAYS":
        return "neutral", "Entry and bias timeframes are SIDEWAYS."
    # Tie-breaker by higher timeframe regime.
    if _regime_matches_direction(bias.get("regime"), "bull") or _regime_matches_direction(macro.get("regime"), "bull"):
        return "bull", "Higher-TF bias is bullish, but entry confluence is not complete."
    if _regime_matches_direction(bias.get("regime"), "bear") or _regime_matches_direction(macro.get("regime"), "bear"):
        return "bear", "Higher-TF bias is bearish, but entry confluence is not complete."
    return "neutral", "No clear directional edge; neutral only if RR/OI supports it."


def _checklist_score(
    symbol: str,
    direction: str,
    trade: Dict[str, Any],
    indicators: Dict[str, Any],
    plan: Dict[str, Any],
    earn_days: int,
    earn_guard: int,
    walls: Dict[str, Any],
    gex: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], int, str]:
    entry = indicators.get(plan["entry_tf"], {})
    bias = indicators.get(plan["bias_tf"], {})
    macro = indicators.get(plan["macro_tf"], {})
    daily = indicators.get("1d") or indicators.get(plan["bias_tf"], {}) or entry
    checks: List[Dict[str, Any]] = []

    def add(name: str, status: str, points: float, max_points: float, note: str):
        checks.append({
            "name": name,
            "status": status,
            "points": round(points, 1),
            "max_points": round(max_points, 1),
            "note": note,
        })

    # 1. Higher-TF regime bias
    maxp = 15
    if direction == "neutral":
        ok = bias.get("regime") == "SIDEWAYS" or macro.get("regime") == "SIDEWAYS"
        add("Higher-TF regime", "pass" if ok else "warn", 12 if ok else 6, maxp,
            f"Bias {bias.get('label','')}: {bias.get('regime','?')}; macro {macro.get('label','')}: {macro.get('regime','?')}.")
    else:
        b_ok = _regime_matches_direction(bias.get("regime"), direction)
        m_ok = _regime_matches_direction(macro.get("regime"), direction) if macro else False
        if b_ok and (m_ok or plan["bias_tf"] == plan["macro_tf"]):
            pts, st = 15, "pass"
        elif b_ok:
            pts, st = 10, "warn"
        elif bias.get("regime") == "SIDEWAYS":
            pts, st = 4, "fail"
        else:
            pts, st = 0, "fail"
        add("Higher-TF regime", st, pts, maxp,
            f"Entry {entry.get('label','')}: {entry.get('regime','?')}; bias {bias.get('label','')}: {bias.get('regime','?')}; macro {macro.get('label','')}: {macro.get('regime','?')}.")

    # 2. ADX rising
    maxp = 10
    if entry.get("is_trending") and entry.get("adx_rising"):
        add("ADX rising", "pass", 10, maxp, f"ADX {entry.get('adx')} > threshold {entry.get('adx_thr')} and rising.")
    elif entry.get("is_trending"):
        add("ADX rising", "warn", 6, maxp, f"ADX {entry.get('adx')} is above threshold but not rising.")
    else:
        add("ADX rising", "fail", 0, maxp, f"ADX {entry.get('adx')} is below threshold {entry.get('adx_thr')}; trend is not confirmed.")

    # 3. Signal confluence count
    maxp = 15
    if direction == "bull":
        count = int(entry.get("bull_confluence", 0))
        note = f"Bull triangle={entry.get('bull_triangle')}, circle={entry.get('bull_circle')}, diamond={entry.get('bull_diamond')} on {entry.get('label')}."
    elif direction == "bear":
        count = int(entry.get("bear_confluence", 0))
        note = f"Bear triangle={entry.get('bear_triangle')}, circle={entry.get('bear_circle')}, diamond={entry.get('bear_diamond')} on {entry.get('label')}."
    else:
        count = 2 if entry.get("regime") == "SIDEWAYS" and not entry.get("is_strong_hist") else 1
        note = f"Neutral setup: entry regime {entry.get('regime')}, strong-hist={entry.get('is_strong_hist')}."
    pts = 15 if count >= 3 else 10 if count == 2 else 5 if count == 1 else 0
    add("Signal confluence", "pass" if count >= 2 else "warn" if count == 1 else "fail", pts, maxp, f"{count}/3 confluence. {note}")

    # 4. S/R levels
    maxp = 10
    atr = _safe_float(daily.get("atr"), 0.0) or 0.0
    high = _safe_float(daily.get("recent_high"), None)
    low = _safe_float(daily.get("recent_low"), None)
    spot = _safe_float(entry.get("close") or daily.get("close"), None) or 0.0
    sr_pts = 5
    sr_status = "warn"
    sr_note = "S/R could not be fully determined."
    if direction == "bull":
        short_k = _safe_float(trade.get("sell_strike"), None)
        room = (high - spot) if high and spot else None
        if short_k and low:
            if short_k <= low or (spot - short_k) >= atr:
                sr_pts, sr_status = 10, "pass"
            elif room is not None and room < atr:
                sr_pts, sr_status = 3, "fail"
            sr_note = f"Short put {short_k}; recent support {low}; resistance {high}; ATR {atr}."
    elif direction == "bear":
        short_k = _safe_float(trade.get("sell_strike"), None)
        room = (spot - low) if low and spot else None
        if short_k and high:
            if short_k >= high or (short_k - spot) >= atr:
                sr_pts, sr_status = 10, "pass"
            elif room is not None and room < atr:
                sr_pts, sr_status = 3, "fail"
            sr_note = f"Short call {short_k}; recent resistance {high}; support {low}; ATR {atr}."
    else:
        put_s = _safe_float(trade.get("put_sell"), None)
        call_s = _safe_float(trade.get("call_sell"), None)
        if put_s and call_s and low and high:
            if put_s <= low and call_s >= high:
                sr_pts, sr_status = 10, "pass"
            elif put_s < spot < call_s:
                sr_pts, sr_status = 7, "warn"
            sr_note = f"IC range {put_s}-{call_s}; recent range {low}-{high}; ATR {atr}."
    add("S/R levels", sr_status, sr_pts, maxp, sr_note)

    # 5. Macro event / earnings risk
    maxp = 8
    if earn_days < earn_guard:
        add("Macro/earnings risk", "fail", 0, maxp, f"Earnings in {earn_days} days, inside guard of {earn_guard} days.")
    elif symbol.upper() in {"SPY", "QQQ", "IWM", "SPX"}:
        add("Macro/earnings risk", "warn", 5, maxp, "Index ETF: earnings risk not applicable; manually check CPI/FOMC/NFP calendar.")
    else:
        add("Macro/earnings risk", "pass", 8, maxp, f"No known earnings event inside {earn_guard} days.")

    # 6. Histogram bar direction
    maxp = 10
    if direction == "bull":
        ok = bool(entry.get("hist_dir_bull") or entry.get("bull_diamond"))
        add("Histogram direction", "pass" if ok else "warn", 10 if ok else 4, maxp,
            f"Hist {entry.get('hist')} vs previous {entry.get('hist_prev')}; diamond={entry.get('bull_diamond')}.")
    elif direction == "bear":
        ok = bool(entry.get("hist_dir_bear") or entry.get("bear_diamond"))
        add("Histogram direction", "pass" if ok else "warn", 10 if ok else 4, maxp,
            f"Hist {entry.get('hist')} vs previous {entry.get('hist_prev')}; diamond={entry.get('bear_diamond')}.")
    else:
        ok = not bool(entry.get("bull_diamond") or entry.get("bear_diamond"))
        add("Histogram direction", "pass" if ok else "warn", 8 if ok else 4, maxp,
            f"Neutral prefers no hot momentum. Strong-hist={entry.get('is_strong_hist')}.")

    # 7. RR ratio
    maxp = 15
    rr = _safe_float(trade.get("rr"), 0.0) or 0.0
    min_rr = _safe_float(trade.get("min_rr"), DEFAULT_MIN_RR) or DEFAULT_MIN_RR
    target_rr = _safe_float(trade.get("target_rr"), DEFAULT_TARGET_RR) or DEFAULT_TARGET_RR
    if rr >= target_rr * 0.9:
        add("R:R ratio", "pass", 15, maxp, f"Estimated RR {rr:.2f}:1, near target {target_rr:.2f}:1.")
    elif rr >= min_rr:
        add("R:R ratio", "warn", 10, maxp, f"Estimated RR {rr:.2f}:1 passes minimum {min_rr:.2f}:1 but is below target {target_rr:.2f}:1.")
    elif rr >= 0.5:
        add("R:R ratio", "warn", 5, maxp, f"Estimated RR {rr:.2f}:1 is below preferred range.")
    else:
        add("R:R ratio", "fail", 0, maxp, f"Estimated RR {rr:.2f}:1 is weak; skip unless manually justified.")

    # 8. GEX / OI / dealer positioning
    maxp = 17
    short_oi = _safe_int(trade.get("short_oi"), 0)
    oi_chg = trade.get("short_oi_change")
    if trade.get("oi_pending") or trade.get("details_pending"):
        add("GEX / OI detail", "warn", 8, maxp,
            "Option OI/GEX is loaded only when the result tile is expanded, so the first pass stays fast.")
    elif symbol.upper() in {"SPY", "QQQ", "IWM", "SPX"}:
        if gex.get("available"):
            total_gex = _safe_float(gex.get("total_gex"), 0.0) or 0.0
            gamma_flip = _safe_float(gex.get("gamma_flip"), None)
            if direction == "neutral" and total_gex >= 0:
                pts, st = 17, "pass"
                note = f"Positive GEX supports range/IC. Total GEX {total_gex:,.0f}; pin {gex.get('pin_strike')}; max pain {gex.get('max_pain')}."
            elif direction == "bear" and total_gex < 0:
                pts, st = 17, "pass"
                note = f"Negative GEX can amplify bearish moves. Total GEX {total_gex:,.0f}; flip {gamma_flip}."
            elif direction == "bull" and (gamma_flip is None or spot >= gamma_flip):
                pts, st = 13, "pass"
                note = f"Spot above gamma flip {gamma_flip}; dealer positioning not hostile. Total GEX {total_gex:,.0f}."
            else:
                pts, st = 7, "warn"
                note = f"GEX is not strongly aligned. Total GEX {total_gex:,.0f}; flip {gamma_flip}."
            add("GEX / dealer positioning", st, pts, maxp, note)
        else:
            pts = 8 if short_oi >= 100 else 4
            add("GEX / dealer positioning", "warn", pts, maxp,
                f"{gex.get('note') or 'GEX unavailable'} Short-strike OI={short_oi}, OI change={oi_chg}.")
    else:
        pts = 14 if short_oi >= 500 else 10 if short_oi >= 100 else 5 if short_oi > 0 else 0
        st = "pass" if pts >= 10 else "warn" if pts > 0 else "fail"
        add("Strike OI / dealer proxy", st, pts, maxp,
            f"Suggested short strike OI={short_oi}; OI change={oi_chg if oi_chg is not None else 'n/a'}; PCR={walls.get('pcr')}.")

    total = round(sum(c["points"] for c in checks))
    # Small bonus if most checks pass, cap at 100.
    passed = sum(1 for c in checks if c["status"] == "pass")
    if passed >= 6:
        total += 3
    total = int(max(0, min(100, total)))

    # Hard liquidity cap for credit structures.  A short strike with almost no
    # OI can look good on RR/delta but is not a clean tradeable wall.  This cap
    # prevents exact-chain scans and Agentic alerts from treating 1-OI shorts as
    # OPEN setups.
    try:
        ttype = str(trade.get("trade_type") or "").upper()
        if ttype in {"PS", "CS"}:
            if short_oi < 25:
                total = min(total, 62)
            elif short_oi < 75:
                total = min(total, 72)
        elif ttype == "IC":
            put_oi = _safe_int((trade.get("put_short_leg") or {}).get("open_interest"), 0)
            call_oi = _safe_int((trade.get("call_short_leg") or {}).get("open_interest"), 0)
            if min(put_oi, call_oi) < 25:
                total = min(total, 62)
            elif min(put_oi, call_oi) < 75:
                total = min(total, 72)
    except Exception:
        pass

    if total >= 85:
        grade = "A"
    elif total >= 75:
        grade = "B"
    elif total >= 65:
        grade = "C"
    elif total >= 50:
        grade = "D"
    else:
        grade = "F"
    return checks, total, grade


def _scan_one(symbol: str, params: Dict[str, Any]) -> Dict[str, Any]:
    sym = symbol.upper().strip()
    target_dte = int(params.get("dte") or DEFAULT_DTE)
    trade_filter = (params.get("trade_type") or "AUTO").upper()
    min_score = int(params.get("min_score") or DEFAULT_MIN_SCORE)
    earn_guard = int(params.get("earn_guard") or DEFAULT_EARN_GUARD)
    width = max(0.5, float(params.get("strike_width") or DEFAULT_WIDTH))
    short_delta = min(0.49, max(0.10, float(params.get("short_delta") or DEFAULT_SHORT_DELTA)))
    target_rr = max(0.10, float(params.get("target_rr") or DEFAULT_TARGET_RR))
    min_rr = max(0.0, float(params.get("min_rr") or DEFAULT_MIN_RR))

    try:
        earn_days = _get_earn_days(sym)
        if earn_days < earn_guard:
            return {"symbol": sym, "filtered": True, "filter_reason": f"Earnings in {earn_days} days", "earn_days": earn_days}

        plan = _dte_plan(target_dte)
        indicators, tf_errors = _fetch_uae_bundle(sym, plan)
        entry = indicators.get(plan["entry_tf"])
        if not entry:
            return {"symbol": sym, "filtered": True, "filter_reason": f"Missing entry timeframe data: {tf_errors}", "tf_errors": tf_errors}
        spot = _safe_float(entry.get("close"), None)
        if not spot or spot <= 0:
            return {"symbol": sym, "filtered": True, "filter_reason": "No valid spot price"}

        direction, direction_reason = _choose_direction(indicators, plan, trade_filter)
        if trade_filter == "AUTO" and direction == "neutral":
            # Let IC handle neutral. Directional scans need clear guide bias.
            pass

        trade, opt_meta = _suggest_trade(sym, spot, target_dte, direction, trade_filter, width, short_delta, target_rr, min_rr)
        if not trade:
            return {"symbol": sym, "filtered": True, "filter_reason": opt_meta.get("expiry_error") or "Could not build trade with requested DTE/strikes", "option_meta": opt_meta}

        walls = _wall_proxy_from_chain(sym, trade["expiry"])
        gex = _spy_gex_context(sym, trade["expiry"], int(trade.get("actual_dte") or target_dte), spot, trade.get("iv_proxy") or 25.0)
        checks, score, grade = _checklist_score(sym, trade.get("direction") or direction, trade, indicators, plan, earn_days, earn_guard, walls, gex)
        passed = sum(1 for c in checks if c["status"] == "pass")
        failed = sum(1 for c in checks if c["status"] == "fail")

        # Strong RR preference: below min RR may still be returned as filtered with reason.
        rr = _safe_float(trade.get("rr"), 0.0) or 0.0
        if trade.get("trade_type") in {"PS", "CS", "IC"} and rr < min_rr:
            return {
                "symbol": sym, "filtered": True,
                "filter_reason": f"RR {rr:.2f}:1 below minimum {min_rr:.2f}:1",
                "score": score, "grade": grade, "trade": trade,
                "checks": checks,
            }
        if score < min_score:
            return {
                "symbol": sym, "filtered": True,
                "filter_reason": f"Guide score {score} below minimum {min_score}",
                "score": score, "grade": grade, "trade": trade,
                "checks": checks,
            }

        rationale_bits = [
            f"{sym} {trade.get('trade_type')} selected from {plan['style']} plan ({plan['note']})",
            direction_reason,
            f"Score {score}/100 ({grade}); checklist {passed}/8 pass, {failed}/8 fail.",
            f"RR {rr:.2f}:1 vs target {target_rr:.2f}:1.",
        ]
        if trade.get("short_oi") is not None:
            rationale_bits.append(f"Suggested short strike OI {trade.get('short_oi')}; OI change {trade.get('short_oi_change') if trade.get('short_oi_change') is not None else 'n/a'}.")

        return {
            "symbol": sym,
            "filtered": False,
            "spot": spot,
            "plan": plan,
            "direction": trade.get("direction") or direction,
            "direction_reason": direction_reason,
            "score": score,
            "grade": grade,
            "recommendation": "OPEN" if score >= 75 else "OPEN_SMALL" if score >= min_score else "WATCH",
            "checks_passed": passed,
            "checks_failed": failed,
            "checklist": checks,
            "indicators": indicators,
            "tf_errors": tf_errors,
            "walls": walls,
            "gex": gex,
            "earn_days": earn_days,
            "rationale": " ".join(rationale_bits),
            **trade,
        }
    except Exception as e:
        return {"symbol": sym, "filtered": True, "filter_reason": f"Error: {str(e)[:120]}", "trace": traceback.format_exc(limit=3)}


def _scan_one_fast(
    symbol: str,
    params: Dict[str, Any],
    indicators: Dict[str, Any],
    tf_errors: Dict[str, str],
    frames: Dict[str, Any],
) -> Dict[str, Any]:
    """Fast first-pass scanner.

    It uses batch-loaded OHLCV and approximate BS strikes. It intentionally does
    not load yfinance option chains or local OI/GEX until the tile is expanded.
    """
    sym = symbol.upper().strip()
    target_dte = int(params.get("dte") or DEFAULT_DTE)
    trade_filter = (params.get("trade_type") or "AUTO").upper()
    min_score = int(params.get("min_score") or DEFAULT_MIN_SCORE)
    earn_guard = int(params.get("earn_guard") or DEFAULT_EARN_GUARD)
    width = max(0.5, float(params.get("strike_width") or DEFAULT_WIDTH))
    short_delta = min(0.49, max(0.10, float(params.get("short_delta") or DEFAULT_SHORT_DELTA)))
    target_rr = max(0.10, float(params.get("target_rr") or DEFAULT_TARGET_RR))
    min_rr = max(0.0, float(params.get("min_rr") or DEFAULT_MIN_RR))

    try:
        earn_days = _get_earn_days(sym)
        if earn_days < earn_guard:
            return {"symbol": sym, "filtered": True, "filter_reason": f"Earnings in {earn_days} days", "earn_days": earn_days}

        plan = _dte_plan(target_dte)
        entry = indicators.get(plan["entry_tf"])
        if not entry:
            return {"symbol": sym, "filtered": True, "filter_reason": f"Missing entry timeframe data: {tf_errors}", "tf_errors": tf_errors}
        spot = _safe_float(entry.get("close"), None)
        if not spot or spot <= 0:
            return {"symbol": sym, "filtered": True, "filter_reason": "No valid spot price"}

        direction, direction_reason = _choose_direction(indicators, plan, trade_filter)
        daily_df = frames.get("1d")
        if daily_df is None or getattr(daily_df, "empty", True):
            daily_df = frames.get(plan.get("bias_tf"))
        iv_proxy = _hist_iv_from_frame(daily_df)
        trade = _estimate_trade_fast(sym, spot, target_dte, direction, trade_filter, width, short_delta, target_rr, min_rr, iv_proxy)
        if not trade:
            return {"symbol": sym, "filtered": True, "filter_reason": "Could not build fast trade preview"}

        walls = {"put_wall": None, "call_wall": None, "top_put_walls": [], "top_call_walls": [], "pcr": None, "pending": True}
        gex = {"available": False, "pending": True, "note": "Loaded when you expand the tile."}
        checks, score, grade = _checklist_score(sym, trade.get("direction") or direction, trade, indicators, plan, earn_days, earn_guard, walls, gex)
        passed = sum(1 for c in checks if c["status"] == "pass")
        failed = sum(1 for c in checks if c["status"] == "fail")

        rr = _safe_float(trade.get("rr"), 0.0) or 0.0
        if trade.get("trade_type") in {"PS", "CS", "IC"} and rr < min_rr:
            return {
                "symbol": sym, "filtered": True,
                "filter_reason": f"Preview RR {rr:.2f}:1 below minimum {min_rr:.2f}:1",
                "score": score, "grade": grade, "trade": trade, "checks": checks,
            }
        if score < min_score:
            return {
                "symbol": sym, "filtered": True,
                "filter_reason": f"Guide score {score} below minimum {min_score}",
                "score": score, "grade": grade, "trade": trade, "checks": checks,
            }

        rationale_bits = [
            f"{sym} {trade.get('trade_type')} preview from {plan['style']} plan ({plan['note']})",
            direction_reason,
            f"Score {score}/100 ({grade}); checklist {passed}/8 pass, {failed}/8 fail.",
            f"Preview RR {rr:.2f}:1 vs target {target_rr:.2f}:1.",
            "Expand the tile to fetch exact option chain, short-strike OI/OI change, and call/put OI graph.",
        ]

        return {
            "symbol": sym,
            "filtered": False,
            "spot": spot,
            "plan": plan,
            "direction": trade.get("direction") or direction,
            "direction_reason": direction_reason,
            "score": score,
            "grade": grade,
            "recommendation": "OPEN" if score >= 75 else "OPEN_SMALL" if score >= min_score else "WATCH",
            "checks_passed": passed,
            "checks_failed": failed,
            "checklist": checks,
            "indicators": indicators,
            "tf_errors": tf_errors,
            "walls": walls,
            "gex": gex,
            "earn_days": earn_days,
            "rationale": " ".join(rationale_bits),
            **trade,
        }
    except Exception as e:
        return {"symbol": sym, "filtered": True, "filter_reason": f"Error: {str(e)[:120]}", "trace": traceback.format_exc(limit=3)}


def _oi_graph_data(symbol: str, expiry: str, spot: Optional[float] = None, per_side: int = 14) -> Dict[str, Any]:
    """Return call/put OI bars in the same shape as the Dashboard options chart."""
    out = {"symbol": symbol, "expiration": expiry, "spot": spot, "calls": [], "puts": [], "oi_snapshot_day": None, "message": None}
    try:
        from ..services.market import get_spot, get_live_strikes_and_volume, get_oi_map_fromDB, fetch_store_for, select_strikes_around_atm
        from ..db import get_oi_fromdb, get_two_latest_dates, get_expirationOI_date

        sym = symbol.upper().strip()
        sp = _safe_float(spot, None) or _safe_float(get_spot(sym), None)
        out["spot"] = sp
        oi_map, oi_day = get_oi_map_fromDB(sym, expiry)
        rows = get_oi_fromdb(sym, expiry)
        if not oi_map:
            try:
                fetch_store_for(sym, expirations=[expiry], per_side=max(8, int(per_side)))
                oi_map, oi_day = get_oi_map_fromDB(sym, expiry)
                rows = get_oi_fromdb(sym, expiry)
            except Exception as e:
                out["message"] = f"No local OI snapshot and auto-fetch failed: {str(e)[:100]}"
        price_map = {}
        for r in rows or []:
            try:
                price_map[(str(r.get("type")).lower(), float(r.get("strike")))] = r.get("price")
            except Exception:
                pass
        db_strikes = sorted({float(s) for (_typ, s) in (oi_map or {}).keys()})
        if not db_strikes:
            out["message"] = out["message"] or "No OI data in DB. Run Scheduler to fetch options OI."
            return out
        strikes = select_strikes_around_atm(db_strikes, sp, int(per_side)) if sp else db_strikes[:int(per_side) * 2]
        vol_map = {}
        try:
            _, vol_map = get_live_strikes_and_volume(sym, expiry)
        except Exception:
            vol_map = {}

        # OI change maps from latest two local snapshots.
        change_map = {}
        latest_date = prev_date = None
        try:
            latest_date, prev_date = get_two_latest_dates(sym, expiry)
            if latest_date and prev_date:
                latest_rows = get_expirationOI_date(sym, expiry, latest_date)
                prev_rows = get_expirationOI_date(sym, expiry, prev_date)
                prev = {(str(r["type"]).lower(), float(r["strike"])): r for r in prev_rows}
                for r in latest_rows:
                    key = (str(r["type"]).lower(), float(r["strike"]))
                    old = prev.get(key)
                    if old:
                        latest_oi = _safe_int(r["oi"], 0)
                        prev_oi = _safe_int(old["oi"], 0)
                        change_map[key] = {
                            "oi_change": latest_oi - prev_oi,
                            "oi_change_pct": round((latest_oi - prev_oi) / max(1, prev_oi) * 100.0, 2) if prev_oi else None,
                        }
        except Exception:
            pass

        for s in strikes:
            s_f = float(s)
            for typ, dest in (("call", out["calls"]), ("put", out["puts"])):
                ch = change_map.get((typ, s_f), {})
                dest.append({
                    "strike": s_f,
                    "price": price_map.get((typ, s_f)),
                    "oi": _safe_int((oi_map or {}).get((typ, s_f), 0), 0),
                    "volume": _safe_int(vol_map.get((typ, s_f), 0), 0),
                    "oi_change": ch.get("oi_change"),
                    "oi_change_pct": ch.get("oi_change_pct"),
                })
        out["oi_snapshot_day"] = oi_day
        out["latest_change_date"] = latest_date
        out["previous_change_date"] = prev_date
        return out
    except Exception as e:
        out["message"] = f"OI graph unavailable: {str(e)[:120]}"
        return out



# ─────────────────────────────────────────────────────────────────────────────
# Simple timeframe/regime stock scanner
# ─────────────────────────────────────────────────────────────────────────────


def _normalize_timeframe(tf: Any) -> str:
    val = str(tf or "1d").strip().lower()
    aliases = {
        "5": "5m", "5min": "5m", "5minute": "5m", "5 minutes": "5m",
        "15": "15m", "15min": "15m", "15minute": "15m", "15 minutes": "15m",
        "60": "1h", "1hr": "1h", "1hour": "1h", "1 hour": "1h",
        "4hr": "4h", "4hour": "4h", "4 hour": "4h",
        "d": "1d", "day": "1d", "daily": "1d",
        "w": "1wk", "1w": "1wk", "week": "1wk", "weekly": "1wk",
    }
    val = aliases.get(val, val)
    return val if val in TF_PARAMS else "1d"


def _pretty_regime(regime: Any) -> str:
    r = str(regime or "SIDEWAYS").upper()
    return {
        "BULL": "Bull",
        "WEAK_BULL": "Weak Bull",
        "BEAR": "Bear",
        "WEAK_BEAR": "Weak Bear",
        "SIDEWAYS": "Sideways",
    }.get(r, r.replace("_", " ").title())


def _regime_color(regime: Any) -> str:
    r = str(regime or "").upper()
    if r == "BULL":
        return "green"
    if r == "WEAK_BULL":
        return "blue"
    if r == "BEAR":
        return "red"
    if r == "WEAK_BEAR":
        return "amber"
    return "neutral"


def _quality_grade(score: int) -> str:
    if score >= 85:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    if score >= 60:
        return "C"
    if score >= 50:
        return "D"
    return "F"


def _score_regime_indicator(ind: Dict[str, Any]) -> Tuple[int, str, List[str], str]:
    """Return a simple 0-100 quality score for the selected timeframe.

    This intentionally does not create option trades. It scores how cleanly the
    symbol fits its current UAE regime on the selected timeframe.
    """
    regime = str(ind.get("regime") or "SIDEWAYS").upper()
    hist = _safe_float(ind.get("hist"), 0.0) or 0.0
    hist_prev = _safe_float(ind.get("hist_prev"), 0.0) or 0.0
    adx = _safe_float(ind.get("adx"), 0.0) or 0.0
    adx_thr = _safe_float(ind.get("adx_thr"), 20.0) or 20.0
    adx_rising = bool(ind.get("adx_rising"))
    trending = bool(ind.get("is_trending"))
    strong_hist = bool(ind.get("is_strong_hist"))
    regime_changed = bool(ind.get("regime_changed"))
    notes: List[str] = []

    if regime in {"BULL", "BEAR"}:
        score = 52
    elif regime in {"WEAK_BULL", "WEAK_BEAR"}:
        score = 48
    else:
        score = 42

    if trending:
        score += 12
        notes.append(f"ADX {adx:.1f} is above threshold {adx_thr:.1f}")
    elif regime == "SIDEWAYS":
        score += 12
        notes.append(f"ADX {adx:.1f} is below threshold {adx_thr:.1f}, confirming sideways regime")
    else:
        score -= 8
        notes.append(f"ADX {adx:.1f} is not confirming trend")

    if adx_rising:
        score += 10
        notes.append("ADX is rising")
    elif trending:
        score += 3
        notes.append("ADX is trending but not rising")

    hist_delta = hist - hist_prev
    if regime == "BULL":
        conf = int(ind.get("bull_confluence", 0) or 0)
        if hist > 0:
            score += 7
        if hist_delta > 0:
            score += 8
            notes.append("bullish histogram is expanding")
        else:
            score -= 4
            notes.append("bullish histogram is not expanding")
        if strong_hist:
            score += 7
        score += min(12, conf * 5)
        action = "Bullish trend candidate; do not short against this regime."
    elif regime == "WEAK_BULL":
        conf = int(ind.get("bull_confluence", 0) or 0)
        # Weak bull is a bullish-trend pullback, not a bearish trade call.
        if hist_delta > 0:
            score += 12
            notes.append("pullback momentum is improving")
        else:
            score += 2
            notes.append("pullback is still weakening; wait for upside confirmation")
        if ind.get("bull_circle") or ind.get("bull_triangle"):
            score += 10
            notes.append("bullish resumption signal is present")
        if strong_hist and hist < 0:
            score -= 5
            notes.append("negative momentum is still hot")
        score += min(8, conf * 4)
        action = "Bullish pullback/watchlist candidate; wait for histogram recross or bull signal."
    elif regime == "BEAR":
        conf = int(ind.get("bear_confluence", 0) or 0)
        if hist < 0:
            score += 7
        if hist_delta < 0:
            score += 8
            notes.append("bearish histogram is expanding")
        else:
            score -= 4
            notes.append("bearish histogram is not expanding")
        if strong_hist:
            score += 7
        score += min(12, conf * 5)
        action = "Bearish trend candidate; avoid bullish trades until regime improves."
    elif regime == "WEAK_BEAR":
        conf = int(ind.get("bear_confluence", 0) or 0)
        # Weak bear is a bearish-trend bounce, not a bullish trade call.
        if hist_delta < 0:
            score += 12
            notes.append("bounce momentum is rolling over")
        else:
            score += 2
            notes.append("bounce is still firm; wait for downside confirmation")
        if ind.get("bear_circle") or ind.get("bear_triangle"):
            score += 10
            notes.append("bearish resumption signal is present")
        if strong_hist and hist > 0:
            score -= 5
            notes.append("positive countertrend momentum is still hot")
        score += min(8, conf * 4)
        action = "Bearish bounce/watchlist candidate; wait for histogram rollover or bear signal."
    else:
        if not strong_hist:
            score += 8
            notes.append("no hot momentum; sideways classification is cleaner")
        else:
            score -= 6
            notes.append("strong momentum exists inside sideways regime; watch for breakout")
        if adx_rising:
            notes.append("ADX is rising from sideways; possible trend restart")
        action = "Sideways/range candidate; avoid directional entries until breakout confirms."

    if regime_changed:
        score += 4
        notes.append(f"regime just changed from {_pretty_regime(ind.get('prev_regime'))}")

    score = int(max(0, min(100, round(score))))
    if not notes:
        notes.append("regime matched selected filter")
    return score, _quality_grade(score), notes, action


def _signal_text(ind: Dict[str, Any]) -> str:
    bits = []
    if ind.get("bull_triangle"):
        bits.append("Bull triangle")
    if ind.get("bear_triangle"):
        bits.append("Bear triangle")
    if ind.get("bull_circle"):
        bits.append("Bull circle")
    if ind.get("bear_circle"):
        bits.append("Bear circle")
    if ind.get("bull_diamond"):
        bits.append("Bull diamond")
    if ind.get("bear_diamond"):
        bits.append("Bear diamond")
    return ", ".join(bits) if bits else "No fresh signal marker"


def _make_regime_match(symbol: str, tf: str, ind: Dict[str, Any]) -> Dict[str, Any]:
    score, grade, notes, action = _score_regime_indicator(ind)
    hist = _safe_float(ind.get("hist"), None, 4)
    hist_prev = _safe_float(ind.get("hist_prev"), None, 4)
    hist_delta = None if hist is None or hist_prev is None else round(hist - hist_prev, 4)
    regime = str(ind.get("regime") or "SIDEWAYS").upper()
    return {
        "symbol": symbol.upper(),
        "timeframe": tf,
        "timeframe_label": _tf_label(tf),
        "regime": regime,
        "regime_label": _pretty_regime(regime),
        "regime_color": _regime_color(regime),
        "score": score,
        "grade": grade,
        "close": _safe_float(ind.get("close"), None, 2),
        "last_time": ind.get("last_time"),
        "adx": _safe_float(ind.get("adx"), None, 2),
        "adx_thr": _safe_float(ind.get("adx_thr"), None, 2),
        "adx_rising": bool(ind.get("adx_rising")),
        "is_trending": bool(ind.get("is_trending")),
        "hist": hist,
        "hist_prev": hist_prev,
        "hist_delta": hist_delta,
        "is_strong_hist": bool(ind.get("is_strong_hist")),
        "ema_slope": _safe_float(ind.get("ema_slope"), None, 4),
        "slow_ema": _safe_float(ind.get("slow_ema"), None, 2),
        "recent_high": _safe_float(ind.get("recent_high"), None, 2),
        "recent_low": _safe_float(ind.get("recent_low"), None, 2),
        "regime_changed": bool(ind.get("regime_changed")),
        "prev_regime": ind.get("prev_regime"),
        "signals": _signal_text(ind),
        "notes": notes,
        "action": action,
    }


def _parse_regime_filters(raw: Any) -> List[str]:
    if isinstance(raw, str):
        items = [x.strip().upper() for x in raw.split(",") if x.strip()]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x).strip().upper() for x in raw if str(x).strip()]
    else:
        items = list(DEFAULT_REGIME_FILTERS)
    valid = {r["value"] for r in REGIME_CHOICES}
    out = [x for x in items if x in valid]
    return out or list(DEFAULT_REGIME_FILTERS)

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────


@uae_trade_bp.route("/")
def page():
    return render_template("uae_trade_scanner.html")


@uae_trade_bp.route("/api/watchlists")
def api_watchlists():
    return jsonify(_sanitize({
        "watchlists": _watchlists(),
        "dte_choices": DTE_CHOICES,
        "timeframes": TIMEFRAME_CHOICES,
        "regimes": REGIME_CHOICES,
        "default_regimes": DEFAULT_REGIME_FILTERS,
    }))


@uae_trade_bp.route("/api/scan", methods=["POST"])
def api_scan():
    """Simple UAE regime scanner.

    User selects one timeframe and one or more regimes. The API returns only
    symbols that currently match those regimes, with an overall quality score.
    No option strikes, RR, OI, GEX, or trade construction is performed here.
    """
    payload = request.get_json(force=True) or {}
    watchlist_id = payload.get("watchlist_id") or None
    symbol_override = (payload.get("symbol") or "").upper().strip()
    timeframe = _normalize_timeframe(payload.get("timeframe") or payload.get("tf") or "1d")
    selected_regimes = _parse_regime_filters(payload.get("regimes"))
    limit = max(1, min(250, int(payload.get("limit") or 100)))
    max_symbols = max(1, min(MAX_SCAN_SYMBOLS_HARD_CAP, int(payload.get("max_symbols") or 100)))
    min_score = max(0, min(100, int(payload.get("min_score") or 0)))

    if symbol_override:
        symbols = [symbol_override]
        universe_mode = "symbol"
    elif watchlist_id:
        symbols = _watchlist_symbols(watchlist_id)
        universe_mode = "watchlist"
    else:
        symbols = _watchlist_symbols(None)
        universe_mode = "all_symbols"
        if not symbols:
            symbols = ["SPY"]
            universe_mode = "default_spy"

    total_universe = len(symbols)
    symbols = sorted({s.upper().strip() for s in symbols if s and s.upper().strip()})[:max_symbols]
    if not symbols:
        return jsonify({"ok": False, "error": "No valid symbols found. Select a watchlist or enter a symbol."}), 400

    t0 = time.time()
    plan = {
        "style": "simple regime filter",
        "entry_tf": timeframe,
        "bias_tf": timeframe,
        "macro_tf": timeframe,
        "required_tfs": [timeframe],
        "note": f"Regime-only scan on {_tf_label(timeframe)}.",
    }
    indicators_by_symbol, tf_errors_by_symbol, _frames = _build_indicator_cache(symbols, plan)

    matches: List[Dict[str, Any]] = []
    filtered: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for sym in symbols:
        ind = (indicators_by_symbol.get(sym) or {}).get(timeframe)
        if not ind:
            err = (tf_errors_by_symbol.get(sym) or {}).get(timeframe) or "not enough data"
            errors.append({"symbol": sym, "error": err})
            continue
        row = _make_regime_match(sym, timeframe, ind)
        if row["regime"] not in selected_regimes:
            filtered.append({
                "symbol": sym,
                "filter_reason": f"Regime {_pretty_regime(row['regime'])} is not selected",
                "regime": row["regime"],
                "score": row["score"],
            })
            continue
        if row["score"] < min_score:
            filtered.append({
                "symbol": sym,
                "filter_reason": f"Score {row['score']} is below minimum {min_score}",
                "regime": row["regime"],
                "score": row["score"],
            })
            continue
        matches.append(row)

    matches.sort(key=lambda r: (r.get("score", 0), r.get("symbol", "")), reverse=True)
    matches = matches[:limit]
    counts = {choice["value"]: 0 for choice in REGIME_CHOICES}
    for row in matches:
        counts[row.get("regime") or "SIDEWAYS"] = counts.get(row.get("regime") or "SIDEWAYS", 0) + 1
    summary = {
        "scanned": len(symbols),
        "total_universe": total_universe,
        "limited": total_universe > len(symbols),
        "max_symbols": max_symbols,
        "universe_mode": universe_mode,
        "returned": len(matches),
        "filtered": len(filtered),
        "errors": len(errors),
        "avg_score": round(sum(r.get("score", 0) for r in matches) / max(1, len(matches)), 1),
        "timeframe": timeframe,
        "timeframe_label": _tf_label(timeframe),
        "selected_regimes": selected_regimes,
        "selected_regime_labels": [_pretty_regime(r) for r in selected_regimes],
        "regime_counts": counts,
        "min_score": min_score,
        "elapsed_seconds": round(time.time() - t0, 2),
        "detail_mode": "none",
    }
    return jsonify(_sanitize({
        "ok": True,
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "matches": matches,
        "opportunities": matches,
        "filtered": filtered[:100],
        "errors": errors[:50],
    }))


@uae_trade_bp.route("/api/detail", methods=["POST"])
def api_detail():
    """Load exact option-chain/OI details for one expanded result tile."""
    payload = request.get_json(force=True) or {}
    sym = (payload.get("symbol") or "").upper().strip()
    if not sym:
        return jsonify({"ok": False, "error": "missing symbol"}), 400
    target_dte = int(payload.get("dte") or payload.get("requested_dte") or DEFAULT_DTE)
    trade_filter = (payload.get("trade_type") or "AUTO").upper()
    direction = (payload.get("direction") or "").lower().strip()
    width = max(0.5, float(payload.get("strike_width") or payload.get("width") or DEFAULT_WIDTH))
    short_delta = min(0.49, max(0.10, float(payload.get("short_delta") or DEFAULT_SHORT_DELTA)))
    target_rr = max(0.10, float(payload.get("target_rr") or DEFAULT_TARGET_RR))
    min_rr = max(0.0, float(payload.get("min_rr") or DEFAULT_MIN_RR))
    spot = _safe_float(payload.get("spot"), None)
    if spot is None or spot <= 0:
        try:
            df = _yf_history(sym, "1d")
            spot = _safe_float(df["Close"].iloc[-1], None) if df is not None and not df.empty else None
        except Exception:
            spot = None
    if spot is None or spot <= 0:
        return jsonify({"ok": False, "error": "could not resolve spot"}), 400

    # If the tile was auto-generated, preserve its chosen direction while still
    # allowing explicit strategy filters to override.
    if trade_filter in {"PS", "CALL"}:
        direction = "bull"
    elif trade_filter in {"CS", "PUT"}:
        direction = "bear"
    elif trade_filter == "IC":
        direction = "neutral"
    elif direction not in {"bull", "bear", "neutral"}:
        direction = "neutral"

    trade, opt_meta = _suggest_trade(sym, spot, target_dte, direction, trade_filter, width, short_delta, target_rr, min_rr)
    if not trade:
        return jsonify(_sanitize({
            "ok": False,
            "error": opt_meta.get("expiry_error") or "Could not build exact option trade",
            "option_meta": opt_meta,
        })), 400
    expiry = trade.get("expiry")
    actual_dte = int(trade.get("actual_dte") or target_dte)
    walls = _wall_proxy_from_chain(sym, expiry)
    gex = _spy_gex_context(sym, expiry, actual_dte, spot, trade.get("iv_proxy") or 25.0)
    oi_chart = _oi_graph_data(sym, expiry, spot, per_side=int(payload.get("per_side") or 14))
    return jsonify(_sanitize({
        "ok": True,
        "symbol": sym,
        "spot": spot,
        "trade": trade,
        "option_meta": opt_meta,
        "walls": walls,
        "gex": gex,
        "oi_chart": oi_chart,
        "loaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }))
