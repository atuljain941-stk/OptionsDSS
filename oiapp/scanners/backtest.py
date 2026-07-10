from __future__ import annotations

import io
import json
import logging
import math
import statistics
from collections import defaultdict
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import uuid
import re
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from flask import Blueprint, jsonify, request, current_app

from .sqlite_store import get_market_data_store
try:
    from .earnings_calendar import get_symbol_calendar
except Exception:  # pragma: no cover - keep backtest usable if earnings module is unavailable
    get_symbol_calendar = None
from .scanner_builder import (
    BUILTIN_SCANNERS,
    TIMEFRAMES,
    _collect_function_periods,
    _conn as _scanner_conn,
    _ensure_tables as _ensure_scanner_tables,
    _eval,
    _expand_scan_nodes,
    _explain,
    _flatten_atoms,
    _flow_snapshot,
    _json_safe,
    _normalize_tf,
    _parse_query,
    _prepare_snapshot,
    _preferred_watchlist_id,
    _required_timeframes,
    _scanner_query_text,
    _series_latest,
    _watchlist_symbols,
    _watchlists,
)

backtest_bp = Blueprint("backtest_bp", __name__, url_prefix="/backtest")
DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")

SUPPORTED_DTES = [7, 14, 21, 28, 30, 35, 45]
SUPPORTED_TRADE_TYPES = {"CS", "PS", "IC", "CB", "PB", "CALL", "PUT"}
SUPPORTED_INTRADAY = {"5m", "15m", "1h", "2h", "4h"}
SUPPORTED_PRICE_TFS = {"1d", "1w", "1m", *SUPPORTED_INTRADAY}
PRICE_KEYS = (
    "close", "open", "high", "low", "volume", "rsi3", "rsi14",
    "ema5", "ema9", "ema20", "ema50", "ema200",
    "ema_rsi14_13", "ema_rsi14_90", "rsi_diff_90",
    "macd", "macd_signal", "macd_hist", "relative_strength",
)

API_TEST_DEFAULT_ENDPOINTS = [
    {"method": "GET", "path": "/api/fetch_status", "label": "Fetch status"},
    {"method": "GET", "path": "/watchlists/", "label": "Watchlists"},
    {"method": "GET", "path": "/telegram/config", "label": "Telegram config"},
    {"method": "GET", "path": "/agentic-ai-scanner/api/status", "label": "Agentic scanner status"},
    {"method": "GET", "path": "/backtest/api/market-data/status", "label": "Backtest cache status"},
    {"method": "GET", "path": "/backtest/api/runs?limit=5", "label": "Saved backtest runs"},
    {"method": "GET", "path": "/api/topbar_context?symbol=SPY&days_back=0&days_ahead=3&news_refresh_min=60", "label": "Topbar context"},
]



def _sample_api_test_path(rule: str) -> str:
    samples = {
        'symbol': 'SPY', 'ticker': 'SPY', 'watchlist_id': '1', 'run_id': 'sample-run', 'id': '1',
        'date': '2026-06-28', 'start_date': '2026-06-28', 'end_date': '2026-06-28',
        'name': 'sample', 'scanner_key': 'sample', 'limit': '5', 'days': '5', 'days_back': '0', 'days_ahead': '3',
        'query': 'sample', 'symbols': 'SPY,QQQ', 'interval': '1d', 'timeframe': '1d', 'path': 'sample',
    }
    def repl(m):
        expr = m.group(1)
        name = expr.split(':', 1)[-1].strip().lower()
        return samples.get(name, 'sample')
    return re.sub(r'<([^<>]+)>', repl, str(rule or ''))


@backtest_bp.route("/api/api-tester/endpoints", methods=["GET"])
def api_backtest_api_tester_endpoints():
    prefixes = (
        '/api/', '/backtest/', '/replay-lab/', '/watchlists', '/telegram', '/agentic-ai-scanner',
        '/gex', '/journal', '/regime', '/sr', '/oibuildup', '/weeklyplan', '/earnings', '/alerts',
        '/inst-scan', '/heatmap', '/planner', '/candidate-board',
    )
    rows = []
    for rule in current_app.url_map.iter_rules():
        path = str(rule.rule or '')
        if not path or rule.endpoint == 'static':
            continue
        if path.startswith('/static/'):
            continue
        if not path.startswith(prefixes):
            continue
        methods = sorted(m for m in rule.methods if m in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'})
        if not methods:
            continue
        sample_path = _sample_api_test_path(path)
        label = str(rule.endpoint or path).replace('_', ' ')
        rows.append({
            'method': methods[0],
            'path': sample_path,
            'label': label[:120],
            'original_path': path,
            'dynamic': sample_path != path,
        })
    rows.sort(key=lambda x: (x['path'], x['method']))
    return jsonify({'ok': True, 'count': len(rows), 'endpoints': rows})


@dataclass
class SymbolHistory:
    symbol: str
    daily: pd.DataFrame
    intraday: Dict[str, pd.DataFrame]
    error: Optional[str] = None


def _parse_date(raw: Any, default: Optional[date] = None) -> date:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    s = str(raw or "").strip()
    if not s:
        if default is not None:
            return default
        raise ValueError("date is required")
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _clean_symbol(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(" ", "")


def _normalize_ohlcv(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [str(c[-1] if c[-1] else c[0]).title() for c in out.columns]
    else:
        out.columns = [str(c).strip().title() for c in out.columns]
    rename = {
        "Adj Close": "Adj Close",
        "Stock Splits": "Stock Splits",
    }
    out = out.rename(columns=rename)
    needed = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in out.columns]
    if len(needed) < 5:
        return None
    out = out[needed].copy()
    out.index = pd.to_datetime(out.index, errors="coerce")
    out = out[~out.index.isna()]
    try:
        if getattr(out.index, "tz", None) is not None:
            out.index = out.index.tz_convert(None)
    except Exception:
        try:
            out.index = out.index.tz_localize(None)
        except Exception:
            pass
    out = out.sort_index()
    out = out.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    return out if not out.empty else None


@contextmanager
def _suppress_yfinance_noise():
    """Keep yfinance empty-data/delisted messages out of the app console."""
    names = ("yfinance", "yfinance.ticker", "yfinance.multi", "yfinance.scrapers.history")
    states = []
    for name in names:
        logger = logging.getLogger(name)
        states.append((logger, logger.level, logger.disabled, logger.propagate))
        logger.setLevel(logging.CRITICAL + 1)
        logger.disabled = True
        logger.propagate = False
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            yield
    finally:
        for logger, level, disabled, propagate in states:
            logger.setLevel(level)
            logger.disabled = disabled
            logger.propagate = propagate


def _fetch_yfinance_history(symbol: str, start: date, end: date, interval: str = "1d") -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    try:
        import yfinance as yf
    except Exception as e:
        return None, f"yfinance is not available: {e}"

    try:
        # yfinance end is exclusive. Move by one calendar day so the intended
        # final trading day can be returned when available.
        kwargs = {
            "start": start.isoformat(),
            "end": (end + timedelta(days=1)).isoformat(),
            "interval": interval,
            "auto_adjust": False,
        }
        ticker = yf.Ticker(symbol)
        try:
            df = ticker.history(**kwargs, raise_errors=True)
        except TypeError:
            # Older yfinance versions do not accept raise_errors. Fall back quietly.
            with _suppress_yfinance_noise():
                df = ticker.history(**kwargs)
        out = _normalize_ohlcv(df)
        if out is None or out.empty:
            return None, f"no {interval} yfinance data"
        return out, None
    except Exception as e:
        return None, str(e) or f"no {interval} yfinance data"


def _intraday_interval(tf: str) -> Optional[str]:
    tf = _normalize_tf(tf)
    if tf in {"5m", "15m", "1h"}:
        return tf
    if tf in {"2h", "4h"}:
        return "1h"
    return None


def _slice_to_day(df: Optional[pd.DataFrame], day: date) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    end_ts = pd.Timestamp(day) + pd.Timedelta(days=1)
    out = df[df.index < end_ts]
    return out if out is not None and not out.empty else None


def _has_bar_on_day(df: Optional[pd.DataFrame], day: date) -> bool:
    if df is None or df.empty:
        return False
    target = pd.Timestamp(day).date()
    try:
        return any(idx.date() == target for idx in df.index[-5:]) or bool((df.index.normalize() == pd.Timestamp(day)).any())
    except Exception:
        return bool((df.index.date == target).any())


def _index_for_day(df: Optional[pd.DataFrame], day: date) -> Optional[int]:
    """Row position of a given calendar day in a daily-bar DataFrame, or
    None if that day has no bar (weekend/holiday/no data)."""
    if df is None or df.empty:
        return None
    try:
        target = pd.Timestamp(day)
        matches = df.index.get_indexer([target])
        if len(matches) and matches[0] != -1:
            return int(matches[0])
        # normalized fallback in case of any tz/time-of-day mismatch
        norm = df.index.normalize()
        hits = [i for i, idx in enumerate(norm) if idx.date() == day]
        return hits[0] if hits else None
    except Exception:
        return None


def _trading_dates(df: Optional[pd.DataFrame]) -> List[date]:
    if df is None or df.empty:
        return []
    return sorted({idx.date() for idx in df.index})


def _resample_daily(df: pd.DataFrame, tf: str) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    tf = _normalize_tf(tf)
    if tf == "1d":
        return df
    rule = "W-FRI" if tf == "1w" else "ME" if tf == "1m" else None
    if not rule:
        return None
    try:
        agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        out = df.resample(rule).agg(agg).dropna(subset=["Close"])
        return out if not out.empty else None
    except Exception:
        return None


def _resample_intraday(df: pd.DataFrame, tf: str) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    tf = _normalize_tf(tf)
    if tf in {"5m", "15m", "1h"}:
        return df
    rule = "2h" if tf == "2h" else "4h" if tf == "4h" else None
    if not rule:
        return None
    try:
        agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        out = df.resample(rule).agg(agg).dropna(subset=["Close"])
        return out if not out.empty else None
    except Exception:
        return None


def _load_symbol_history(
    symbol: str,
    required_tfs: Iterable[str],
    start: date,
    end: date,
    data_provider: str = "auto",
    auto_fetch: bool = True,
) -> SymbolHistory:
    # Keep a warmup window so scanner functions such as EMA200, RSI, and
    # rsidiff90() can be evaluated without looking into the future.  Daily bars
    # are read from the local SQLite cache when available and filled from yfinance when needed.
    warmup_days = 260
    fetch_start = start - timedelta(days=warmup_days)
    daily: Optional[pd.DataFrame] = None
    err: Optional[str] = None
    try:
        store = get_market_data_store()
        daily, meta = store.ensure_daily_history(symbol, fetch_start, end, provider=data_provider, auto_fetch=auto_fetch)
        err = meta.get("error") if isinstance(meta, dict) else None
    except Exception as e:
        err = str(e)

    if (daily is None or daily.empty) and data_provider not in {"sqlite"}:
        # Final safety fallback keeps the current yfinance-only behavior working
        # when the local SQLite cache is unavailable or empty.
        daily, err = _fetch_yfinance_history(symbol, fetch_start, end, "1d")

    if daily is None or daily.empty:
        return SymbolHistory(symbol=symbol, daily=pd.DataFrame(), intraday={}, error=err or "no daily data")

    intraday: Dict[str, pd.DataFrame] = {}
    for tf in sorted({_normalize_tf(t) for t in required_tfs if _normalize_tf(t) in SUPPORTED_INTRADAY}):
        interval = _intraday_interval(tf)
        if not interval:
            continue
        # yfinance intraday history is intentionally limited. Fetch what is
        # available and let the simulator skip dates without bars.  Keep the
        # request bounded relative to the requested backtest end date so older
        # historical runs do not accidentally ask for a start date after end.
        lookback = 60 if interval in {"5m", "15m"} else 729
        intraday_start = max(start - timedelta(days=10), end - timedelta(days=lookback))
        if intraday_start > end:
            intraday_start = max(start - timedelta(days=10), end - timedelta(days=lookback))
        idf, _ierr = _fetch_yfinance_history(symbol, intraday_start, end, interval)
        if idf is not None and not idf.empty:
            intraday[interval] = idf
    return SymbolHistory(symbol=symbol, daily=daily, intraday=intraday)


def _build_ctx_for_day(
    symbol: str,
    hist: SymbolHistory,
    bench_hist: SymbolHistory,
    day: date,
    benchmark: str,
    required_tfs: Iterable[str],
) -> Optional[Dict[str, Any]]:
    tfs = list(dict.fromkeys([_normalize_tf(t) for t in required_tfs if t]))
    if "1d" not in tfs:
        tfs.append("1d")

    daily_cut = _slice_to_day(hist.daily, day)
    bench_daily_cut = _slice_to_day(bench_hist.daily, day) if bench_hist and bench_hist.daily is not None else None
    if daily_cut is None or len(daily_cut) < 25:
        return None

    timeframes: Dict[str, Any] = {}
    benchmark_history: Dict[str, pd.DataFrame] = {}

    for tf in tfs:
        if tf in {"1d", "1w", "1m"}:
            df_tf = _resample_daily(daily_cut, tf)
            bench_tf = _resample_daily(bench_daily_cut, tf) if bench_daily_cut is not None else None
        elif tf in SUPPORTED_INTRADAY:
            interval = _intraday_interval(tf)
            raw = hist.intraday.get(interval or "")
            braw = bench_hist.intraday.get(interval or "") if bench_hist else None
            df_tf = _resample_intraday(_slice_to_day(raw, day), tf) if raw is not None else None
            bench_tf = _resample_intraday(_slice_to_day(braw, day), tf) if braw is not None else None
        else:
            continue

        if df_tf is None or len(df_tf) < 25:
            return None
        if bench_tf is not None and len(bench_tf) < 25:
            bench_tf = None
        timeframes[tf] = _prepare_snapshot(df_tf, bench_tf, tf)
        if bench_tf is not None and not bench_tf.empty:
            benchmark_history[tf] = bench_tf

    base = timeframes.get("1d", {}).get("series", {})
    ctx: Dict[str, Any] = {
        "symbol": symbol,
        "benchmark": benchmark,
        "timeframes": timeframes,
        "benchmark_history": benchmark_history,
        "options_history": [],
        "leadership": None,
        "beta": None,
        "sector": None,
        "sector_etf": None,
        "earn_days": None,
        "earn_score": None,
        "earn_date": None,
        "next_earn_date": None,
        "last_earn_date": None,
    }
    for key in PRICE_KEYS:
        series = base.get(key)
        prev, now = _series_latest(series) if series is not None else (None, None)
        ctx[key] = now
        ctx[f"{key}_prev"] = prev
    ctx["price"] = ctx.get("close")
    if ctx.get("rsi14") is not None and ctx.get("ema_rsi14_90") is not None:
        try:
            ctx["rsi_diff_90"] = float(ctx.get("rsi14")) - float(ctx.get("ema_rsi14_90"))
        except Exception:
            ctx["rsi_diff_90"] = None

    flow = _flow_snapshot(ctx, tf="1d")
    ctx.update(flow)
    ctx["iv_rank"] = flow.get("iv_rank")
    ctx["iv_est"] = flow.get("iv_est")
    ctx["iv_change"] = flow.get("iv_change")
    ctx["flow_score"] = flow.get("flow_score")
    ctx["flow_bias"] = flow.get("flow_bias")
    ctx["flow_classification"] = flow.get("flow_classification")
    ctx["pcr_shift"] = flow.get("pcr_shift")
    return ctx


def _apply_cross_sectional_fields(contexts: List[Dict[str, Any]], root: Any, benchmark: str) -> None:
    rs_vals = [r.get("relative_strength") for r in contexts if _is_finite(r.get("relative_strength"))]
    rs_sorted = sorted(float(v) for v in rs_vals)
    n = len(rs_sorted)
    if n:
        for r in contexts:
            rs = r.get("relative_strength")
            if not _is_finite(rs):
                continue
            pct = sum(1 for v in rs_sorted if v <= float(rs)) / n
            r["leadership"] = int(round(pct * 100))

    # The scanner expression supports RSRank().  Recalculate it per simulated
    # day across the active universe, never using future bars.
    try:
        from .scanner_builder import _relative_strength_value
        periods = sorted(_collect_function_periods(root, {"rsrank", "rs_rank"}))
        for period in periods:
            vals: List[float] = []
            for r in contexts:
                try:
                    v = _relative_strength_value(r, benchmark, period, tf="1d", shift=0)
                except Exception:
                    v = None
                r[f"_rsrank_source_{period}"] = v
                if _is_finite(v):
                    vals.append(float(v))
            if not vals:
                continue
            vals_sorted = sorted(vals)
            total = len(vals_sorted)
            for r in contexts:
                v = r.get(f"_rsrank_source_{period}")
                if not _is_finite(v):
                    continue
                pct = sum(1 for x in vals_sorted if x <= float(v)) / total
                r[f"rs_rank_{period}"] = int(round(pct * 100))
    except Exception:
        pass



def _earnings_snapshot_for_day(symbol: str, day: date, cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Return earnings fields relative to a simulated backtest day.

    The app's earnings cache stores the next and last known earnings dates.  For
    historical backtests this is intentionally a best-effort risk filter; when a
    cached next earnings date is available, entries can be skipped if that date
    is within the user-selected avoidance window.
    """
    sym = _clean_symbol(symbol)
    row = cache.get(sym) if isinstance(cache, dict) else None
    if row is None:
        row = {}
        if get_symbol_calendar is not None:
            try:
                row = get_symbol_calendar(sym) or {}
            except Exception:
                row = {}
        if isinstance(cache, dict):
            cache[sym] = row

    next_ed = str(row.get("next_earn_date") or "")[:10] or None
    last_ed = str(row.get("last_earn_date") or "")[:10] or None
    earn_days = 999
    earn_date = next_ed or last_ed

    if next_ed:
        try:
            diff = (date.fromisoformat(next_ed) - day).days
            # Only future/today earnings are entry risk.  Past earnings are not
            # considered an upcoming-event risk for this filter.
            earn_days = diff if diff >= 0 else 999
        except Exception:
            earn_days = 999

    return {
        "earn_days": earn_days,
        "earn_score": row.get("earn_score"),
        "earn_date": earn_date,
        "next_earn_date": next_ed,
        "last_earn_date": last_ed,
    }


def _parse_nonnegative_int(value: Any, default: int = 0, upper: int = 365) -> int:
    try:
        out = int(float(value))
    except Exception:
        out = default
    return max(0, min(out, upper))

def _is_finite(value: Any) -> bool:
    try:
        v = float(value)
        return math.isfinite(v)
    except Exception:
        return False


def _round_money(value: Any, places: int = 2) -> Optional[float]:
    try:
        v = float(value)
        if not math.isfinite(v):
            return None
        return round(v, places)
    except Exception:
        return None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(S: float, K: float, dte: int, sigma: float, option_type: str, r: float = 0.04) -> float:
    try:
        S = max(float(S), 0.01)
        K = max(float(K), 0.01)
        T = max(float(dte), 1.0) / 365.0
        sigma = max(0.05, min(float(sigma), 2.50))
        sqt = math.sqrt(T)
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqt)
        d2 = d1 - sigma * sqt
        if option_type.lower().startswith("c"):
            price = S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
        else:
            price = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
        return max(0.01, round(float(price), 4))
    except Exception:
        intrinsic = max(0.0, S - K) if option_type.lower().startswith("c") else max(0.0, K - S)
        return max(0.01, round(intrinsic + S * 0.01, 4))


def _hist_vol(close: pd.Series, idx: int, lookback: int = 20) -> float:
    try:
        s = pd.to_numeric(close.iloc[: idx + 1], errors="coerce").dropna()
        if len(s) < lookback + 2:
            return 0.30
        rets = (s / s.shift(1)).apply(lambda x: math.log(x) if x and x > 0 else None).dropna()
        if len(rets) < lookback:
            return 0.30
        vol = float(rets.tail(lookback).std(ddof=0) * math.sqrt(252))
        return max(0.08, min(vol, 1.50))
    except Exception:
        return 0.30


def _strike_interval(price: float) -> float:
    if price < 25:
        return 0.5
    if price < 100:
        return 1.0
    if price < 250:
        return 2.5
    if price < 500:
        return 5.0
    return 10.0


def _round_strike(value: float, interval: float) -> float:
    return round(round(float(value) / interval) * interval, 2)


def _option_delta(S: float, K: float, dte: int, sigma: float, option_type: str, r: float = 0.04) -> Optional[float]:
    """Black-Scholes delta helper used only to choose approximate historical strikes.

    The simple DTE backtest still scores trades from the underlying close; this
    function lets the user select a realistic short strike by target delta using
    only volatility known at the simulated entry date.
    """
    try:
        S = max(float(S), 0.01)
        K = max(float(K), 0.01)
        T = max(float(dte), 1.0) / 365.0
        sigma = max(0.05, min(float(sigma), 2.50))
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        if str(option_type).upper().startswith("C"):
            return _norm_cdf(d1)
        return _norm_cdf(d1) - 1.0
    except Exception:
        return None


def _strike_from_delta(S: float, dte: int, sigma: float, option_type: str, target_delta: float) -> float:
    """Return an approximate strike for a target absolute short delta.

    For a call spread this finds an OTM call with call delta ~= target_delta.
    For a put spread this finds an OTM put with abs(put delta) ~= target_delta.
    """
    S = max(float(S), 0.01)
    sigma = max(0.05, min(float(sigma or 0.30), 2.50))
    target = max(0.01, min(abs(float(target_delta or 0.20)), 0.49))
    opt = str(option_type or "CALL").upper()
    T = max(float(dte), 1.0) / 365.0
    move = max(0.08, min(1.50, sigma * math.sqrt(T) * 4.0 + 0.10))

    if opt.startswith("C"):
        lo = S
        hi = S * (1.0 + move)
        for _ in range(60):
            mid = (lo + hi) / 2.0
            delta = _option_delta(S, mid, dte, sigma, "CALL")
            if delta is None:
                break
            # Call delta decreases as strike moves higher.
            if delta > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    lo = max(0.01, S * (1.0 - move))
    hi = S
    for _ in range(60):
        mid = (lo + hi) / 2.0
        delta = _option_delta(S, mid, dte, sigma, "PUT")
        abs_delta = abs(delta) if delta is not None else None
        if abs_delta is None:
            break
        # Absolute put delta increases as strike moves higher.
        if abs_delta < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _spread_width(value: Any, spot: float, interval: float) -> float:
    try:
        width = float(value)
    except Exception:
        width = 0.0
    if not math.isfinite(width) or width <= 0:
        width = interval * 5.0
    # Avoid absurd wings on very small/large names while preserving user intent.
    width = max(interval, min(width, max(interval, float(spot) * 0.50)))
    return round(width, 2)


def _expiry_index(daily: pd.DataFrame, entry_idx: int, entry_day: date, dte: int) -> Optional[int]:
    target = entry_day + timedelta(days=int(dte))
    dates = [idx.date() for idx in daily.index]
    for i in range(entry_idx + 1, len(dates)):
        if dates[i] >= target:
            return i
    return None


def _price_row_for_day(daily: pd.DataFrame, day: date) -> Optional[int]:
    if daily is None or daily.empty:
        return None
    dates = [idx.date() for idx in daily.index]
    for i in range(len(dates) - 1, -1, -1):
        if dates[i] == day:
            return i
        if dates[i] < day:
            break
    return None


def _build_option_position(
    symbol: str,
    trade_type: str,
    entry_day: date,
    entry_idx: int,
    daily: pd.DataFrame,
    dte: int,
    strategy_label: str,
    query_text: str,
    reason: List[str],
    ctx: Dict[str, Any],
    otm_pct: float = 2.0,
    strike_width: float = 5.0,
    short_delta: float = 0.20,
    strike_mode: str = "delta",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Create a simple DTE outcome trade.

    This intentionally does not price options or compute dollars.  The trade is
    scored only by whether the stock closes in the favorable zone on the first
    available trading day on/after entry_date + DTE.
    """
    exit_idx = _expiry_index(daily, entry_idx, entry_day, dte)
    if exit_idx is None:
        return None, "not enough future bars to reach selected DTE"

    try:
        S = float(daily["Close"].iloc[entry_idx])
    except Exception:
        return None, "missing entry close"
    if not math.isfinite(S) or S <= 0:
        return None, "invalid entry close"

    trade_type = trade_type.upper()
    if trade_type == "CALL":
        trade_type = "CB"
    elif trade_type == "PUT":
        trade_type = "PB"

    # Keep this backtest intentionally simple: the entry spot is the short
    # strike/reference strike for directional spread logic.  This matches the
    # desired 30 DTE example: PS entered at spot 136 wins if DTE close remains
    # above 136.  A default synthetic width is still stored so between-wing
    # outcomes can be labeled as partial/inside-wing instead of pretending to
    # model dollars.
    otm = max(0.0, min(float(otm_pct or 0.0), 50.0)) / 100.0
    interval = _strike_interval(S)
    width = _spread_width(strike_width, S, interval)
    target_delta = max(0.01, min(abs(float(short_delta or 0.20)), 0.49))
    mode = "entry_spot"
    hv = _hist_vol(daily["Close"], entry_idx)

    spot_strike = round(S, 2)
    legs: List[Dict[str, Any]] = []
    short_call: Optional[float] = None
    short_put: Optional[float] = None
    long_call: Optional[float] = None
    long_put: Optional[float] = None
    favorable_rule = ""

    if trade_type == "CS":
        short_call = spot_strike
        long_call = round(short_call + width, 2)
        legs = [
            {"side": "SELL", "type": "CALL", "strike": short_call},
            {"side": "BUY", "type": "CALL", "strike": long_call},
        ]
        favorable_rule = f"credit bearish: winner if DTE close is at/below short call {short_call}; loser if at/above long call {long_call}"
    elif trade_type == "PS":
        short_put = spot_strike
        long_put = max(0.01, round(short_put - width, 2))
        legs = [
            {"side": "SELL", "type": "PUT", "strike": short_put},
            {"side": "BUY", "type": "PUT", "strike": long_put},
        ]
        favorable_rule = f"credit bullish: winner if DTE close is at/above short put {short_put}; loser if at/below long put {long_put}"
    elif trade_type == "IC":
        # Neutral credit strategy still needs a simple range.  Use a small
        # default OTM band around entry spot while keeping strikes automatic.
        band = max(width, S * max(otm, 0.02))
        short_put = round(max(0.01, S - band), 2)
        long_put = round(max(0.01, short_put - width), 2)
        short_call = round(S + band, 2)
        long_call = round(short_call + width, 2)
        legs = [
            {"side": "SELL", "type": "PUT", "strike": short_put},
            {"side": "BUY", "type": "PUT", "strike": long_put},
            {"side": "SELL", "type": "CALL", "strike": short_call},
            {"side": "BUY", "type": "CALL", "strike": long_call},
        ]
        favorable_rule = f"credit neutral: winner if DTE close stays between short put {short_put} and short call {short_call}"
    elif trade_type == "CB":
        long_call = spot_strike
        short_call = round(long_call + width, 2)
        legs = [
            {"side": "BUY", "type": "CALL", "strike": long_call},
            {"side": "SELL", "type": "CALL", "strike": short_call},
        ]
        favorable_rule = f"debit bullish: winner if DTE close is at/above short call {short_call}; loser if at/below long call {long_call}"
    elif trade_type == "PB":
        long_put = spot_strike
        short_put = max(0.01, round(long_put - width, 2))
        legs = [
            {"side": "BUY", "type": "PUT", "strike": long_put},
            {"side": "SELL", "type": "PUT", "strike": short_put},
        ]
        favorable_rule = f"debit bearish: winner if DTE close is at/below short put {short_put}; loser if at/above long put {long_put}"
    else:
        return None, f"unsupported trade type {trade_type}"

    actual_call_delta = _option_delta(S, short_call, dte, hv, "CALL") if short_call else None
    actual_put_delta = _option_delta(S, short_put, dte, hv, "PUT") if short_put else None
    exit_day = daily.index[exit_idx].date()

    # Black-Scholes premium at entry for each leg — used only by the optional
    # profit-target/stop-loss active-management path below. The existing
    # "hold to expiry, check OTM at expiry" scoring above is completely
    # unaffected by any of this; it's additive data, not a replacement.
    def _leg_price(strike, opt_type):
        return _bs_price(S, strike, dte, hv, opt_type) if strike is not None else 0.0

    entry_premium = 0.0
    max_profit_dollars = 0.0
    max_loss_dollars = 0.0
    is_credit = trade_type in ("CS", "PS", "IC")
    try:
        if trade_type == "CS":
            entry_premium = _leg_price(short_call, "CALL") - _leg_price(long_call, "CALL")
            max_profit_dollars = max(0.01, entry_premium)
            max_loss_dollars = max(0.01, width - entry_premium)
        elif trade_type == "PS":
            entry_premium = _leg_price(short_put, "PUT") - _leg_price(long_put, "PUT")
            max_profit_dollars = max(0.01, entry_premium)
            max_loss_dollars = max(0.01, width - entry_premium)
        elif trade_type == "IC":
            put_credit = _leg_price(short_put, "PUT") - _leg_price(long_put, "PUT")
            call_credit = _leg_price(short_call, "CALL") - _leg_price(long_call, "CALL")
            entry_premium = put_credit + call_credit
            max_profit_dollars = max(0.01, entry_premium)
            max_loss_dollars = max(0.01, width - entry_premium)
        elif trade_type == "CB":
            entry_premium = _leg_price(short_call, "CALL") - _leg_price(long_call, "CALL")  # negative = debit paid
            debit = max(0.01, -entry_premium)
            max_loss_dollars = debit
            max_profit_dollars = max(0.01, width - debit)
        elif trade_type == "PB":
            entry_premium = _leg_price(short_put, "PUT") - _leg_price(long_put, "PUT")
            debit = max(0.01, -entry_premium)
            max_loss_dollars = debit
            max_profit_dollars = max(0.01, width - debit)
    except Exception:
        entry_premium = 0.0
        max_profit_dollars = max(0.01, width * 0.3)
        max_loss_dollars = max(0.01, width * 0.7)

    return {
        "symbol": symbol,
        "strategy": strategy_label,
        "query_text": query_text,
        "trade_type": trade_type,
        "entry_date": entry_day.isoformat(),
        "target_expiry_date": (entry_day + timedelta(days=int(dte))).isoformat(),
        "expiry_date": exit_day.isoformat(),
        "entry_idx": entry_idx,
        "exit_idx": exit_idx,
        "entry_price": round(S, 2),
        "dte": int(dte),
        "expected_hold_days": (exit_day - entry_day).days,
        "strike_mode": mode,
        "target_short_delta": round(target_delta, 3),
        "short_delta": round(target_delta, 3),
        "actual_short_call_delta": round(actual_call_delta, 3) if actual_call_delta is not None and math.isfinite(actual_call_delta) else None,
        "actual_short_put_delta": round(actual_put_delta, 3) if actual_put_delta is not None and math.isfinite(actual_put_delta) else None,
        "strike_width": round(width, 2),
        "iv_proxy": round(hv, 4),
        "otm_pct": round(otm * 100.0, 2),
        "legs": legs,
        "strikes": _format_legs(legs),
        "short_call": short_call,
        "short_put": short_put,
        "long_call": long_call,
        "long_put": long_put,
        "favorable_rule": favorable_rule,
        "reason": reason[:8] if reason else [],
        "rsi14": _round_money(ctx.get("rsi14"), 2),
        "rsi_diff_90": _round_money(ctx.get("rsi_diff_90"), 2),
        "close": _round_money(ctx.get("close"), 2),
        "status": "OPEN",
        # Black-Scholes premium fields — only consumed if active management
        # (profit target / stop loss) is turned on; harmless extra data
        # otherwise.
        "entry_premium": round(entry_premium, 4),
        "is_credit_strategy": is_credit,
        "max_profit_dollars": round(max_profit_dollars, 4),
        "max_loss_dollars": round(max_loss_dollars, 4),
    }, None


def _close_position(position: Dict[str, Any], daily: pd.DataFrame) -> Dict[str, Any]:
    idx = int(position.get("exit_idx") or 0)
    exit_spot = float(daily["Close"].iloc[idx])
    exit_day = daily.index[idx].date()
    entry_day = _parse_date(position.get("entry_date"))
    trade_type = str(position.get("trade_type") or "").upper()
    entry_spot = float(position.get("entry_price") or 0.0)
    if trade_type == "CALL":
        trade_type = "CB"
    elif trade_type == "PUT":
        trade_type = "PB"
    short_call = position.get("short_call")
    short_put = position.get("short_put")
    long_call = position.get("long_call")
    long_put = position.get("long_put")

    winner = False
    exit_check = ""
    dollar_pnl = None
    entry_premium = float(position.get("entry_premium") or 0.0)
    max_profit_d = max(0.01, float(position.get("max_profit_dollars") or 0.01))
    max_loss_d = max(0.01, float(position.get("max_loss_dollars") or 0.01))
    is_credit = bool(position.get("is_credit_strategy"))

    def _spread_intrinsic_pnl(short_k, long_k, credit, spot, short_is_lower):
        """Linear intrinsic value at expiry between the two strikes —
        mirrors _mark_to_market's shape, just at DTE=0 (no time value left)."""
        width = abs(long_k - short_k)
        loss_cap = max(0.01, width - credit)
        if short_is_lower:  # call side: profit while spot stays at/below short strike
            if spot <= short_k:
                return credit
            if spot >= long_k:
                return -loss_cap
            return credit - (spot - short_k)
        else:  # put side: profit while spot stays at/above short strike
            if spot >= short_k:
                return credit
            if spot <= long_k:
                return -loss_cap
            return credit - (short_k - spot)

    try:
        if trade_type == "CS":
            short_k = float(short_call)
            long_k = float(long_call) if long_call is not None else short_k
            winner = exit_spot <= short_k
            zone = "WIN" if winner else "MAX/partial loss" if exit_spot >= long_k else "between short and long - partial loss"
            exit_check = f"Credit CS: DTE close {round(exit_spot, 2)} vs short call {round(short_k, 2)} / long call {round(long_k, 2)} => {zone}"
            dollar_pnl = _spread_intrinsic_pnl(short_k, long_k, entry_premium, exit_spot, short_is_lower=True)
        elif trade_type == "PS":
            short_k = float(short_put)
            long_k = float(long_put) if long_put is not None else short_k
            winner = exit_spot >= short_k
            zone = "WIN" if winner else "MAX/partial loss" if exit_spot <= long_k else "between short and long - partial loss"
            exit_check = f"Credit PS: DTE close {round(exit_spot, 2)} vs short put {round(short_k, 2)} / long put {round(long_k, 2)} => {zone}"
            dollar_pnl = _spread_intrinsic_pnl(short_k, long_k, entry_premium, exit_spot, short_is_lower=False)
        elif trade_type == "IC":
            sp = float(short_put)
            sc = float(short_call)
            lp = float(long_put) if long_put is not None else sp
            lc = float(long_call) if long_call is not None else sc
            winner = sp <= exit_spot <= sc
            zone = "WIN" if winner else "outside long wings" if (exit_spot <= lp or exit_spot >= lc) else "between short and long wing - partial loss"
            exit_check = f"Credit IC: DTE close {round(exit_spot, 2)} vs shorts {round(sp, 2)}-{round(sc, 2)} / longs {round(lp, 2)}-{round(lc, 2)} => {zone}"
            half = entry_premium / 2.0
            dollar_pnl = (_spread_intrinsic_pnl(sp, lp, half, exit_spot, short_is_lower=False)
                          + _spread_intrinsic_pnl(sc, lc, half, exit_spot, short_is_lower=True))
        elif trade_type == "CB":
            long_k = float(long_call) if long_call is not None else entry_spot
            short_k = float(short_call) if short_call is not None else long_k
            winner = exit_spot >= short_k
            zone = "WIN" if winner else "MAX/partial loss" if exit_spot <= long_k else "between long and short - partial gain/loss"
            exit_check = f"Debit CB: DTE close {round(exit_spot, 2)} vs long call {round(long_k, 2)} / short call {round(short_k, 2)} => {zone}"
            debit_paid = max(0.01, -entry_premium)
            spread_value = min(max(0.0, exit_spot - long_k), max(0.01, short_k - long_k))
            dollar_pnl = spread_value - debit_paid
        elif trade_type == "PB":
            long_k = float(long_put) if long_put is not None else entry_spot
            short_k = float(short_put) if short_put is not None else long_k
            winner = exit_spot <= short_k
            zone = "WIN" if winner else "MAX/partial loss" if exit_spot >= long_k else "between long and short - partial gain/loss"
            exit_check = f"Debit PB: DTE close {round(exit_spot, 2)} vs long put {round(long_k, 2)} / short put {round(short_k, 2)} => {zone}"
            debit_paid = max(0.01, -entry_premium)
            spread_value = min(max(0.0, long_k - exit_spot), max(0.01, long_k - short_k))
            dollar_pnl = spread_value - debit_paid
    except Exception as e:
        winner = False
        exit_check = f"outcome check failed: {e}"

    price_change = exit_spot - entry_spot
    pct = (price_change / entry_spot * 100.0) if entry_spot else None
    out = dict(position)
    dollar_pnl_pct_profit = round(dollar_pnl / max_profit_d * 100.0, 1) if dollar_pnl is not None else None
    dollar_pnl_pct_loss = round(dollar_pnl / max_loss_d * 100.0, 1) if dollar_pnl is not None else None
    out.update({
        "status": "CLOSED",
        "exit_date": exit_day.isoformat(),
        "exit_price": round(exit_spot, 2),
        "price_change": round(price_change, 2),
        "price_change_pct": round(pct, 2) if pct is not None and math.isfinite(pct) else None,
        "winner": bool(winner),
        "outcome": "WIN" if winner else "LOSS",
        "exit_check": exit_check,
        "days_held": (exit_day - entry_day).days,
        "exit_reason": "expiry",
        "dollar_pnl": round(dollar_pnl, 2) if dollar_pnl is not None else None,
        "dollar_pnl_pct_of_max_profit": dollar_pnl_pct_profit,
        "dollar_pnl_pct_of_max_loss": dollar_pnl_pct_loss,
    })
    return _strip_internal_trade_fields(out)


def _mark_to_market(position: Dict[str, Any], daily: pd.DataFrame, as_of_idx: int) -> Optional[Dict[str, Any]]:
    """Black-Scholes value of an open position as of a given day (not
    expiry) — this is what makes an early profit-target/stop-loss check
    possible at all, since the simple expiry-only mode never needs to know
    what a position is worth mid-life."""
    try:
        trade_type = str(position.get("trade_type") or "").upper()
        expiry_day = _parse_date(position.get("expiry_date"))
        as_of_day = daily.index[as_of_idx].date()
        dte_remaining = max(1, (expiry_day - as_of_day).days)
        S = float(daily["Close"].iloc[as_of_idx])
        if not math.isfinite(S) or S <= 0:
            return None
        hv = _hist_vol(daily["Close"], as_of_idx)

        short_call, long_call = position.get("short_call"), position.get("long_call")
        short_put, long_put = position.get("short_put"), position.get("long_put")

        def _leg(strike, opt_type):
            return _bs_price(S, strike, dte_remaining, hv, opt_type) if strike is not None else 0.0

        if trade_type == "CS":
            cost_to_close = _leg(short_call, "CALL") - _leg(long_call, "CALL")
        elif trade_type == "PS":
            cost_to_close = _leg(short_put, "PUT") - _leg(long_put, "PUT")
        elif trade_type == "IC":
            cost_to_close = (_leg(short_put, "PUT") - _leg(long_put, "PUT")) + (_leg(short_call, "CALL") - _leg(long_call, "CALL"))
        elif trade_type == "CB":
            cost_to_close = _leg(short_call, "CALL") - _leg(long_call, "CALL")
        elif trade_type == "PB":
            cost_to_close = _leg(short_put, "PUT") - _leg(long_put, "PUT")
        else:
            return None

        entry_premium = float(position.get("entry_premium") or 0.0)
        is_credit = bool(position.get("is_credit_strategy"))
        # Credit strategy: profit as the spread you sold decays in value.
        # Debit strategy: profit as the spread you bought gains value.
        pnl = (entry_premium - cost_to_close) if is_credit else (-cost_to_close - entry_premium)
        max_profit = max(0.01, float(position.get("max_profit_dollars") or 0.01))
        max_loss = max(0.01, float(position.get("max_loss_dollars") or 0.01))
        return {
            "as_of_date": as_of_day.isoformat(),
            "spot": round(S, 2),
            "dte_remaining": dte_remaining,
            "cost_to_close": round(cost_to_close, 4),
            "pnl": round(pnl, 4),
            "pnl_pct_of_max_profit": round(pnl / max_profit * 100.0, 1),
            "pnl_pct_of_max_loss": round(pnl / max_loss * 100.0, 1),
        }
    except Exception:
        return None


def _check_active_exit(position: Dict[str, Any], daily: pd.DataFrame, as_of_idx: int,
                        profit_target_pct: float, stop_loss_pct: float) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Returns (reason, mtm) where reason is 'profit_target', 'stop_loss', or
    None if neither threshold is hit yet on this day."""
    mtm = _mark_to_market(position, daily, as_of_idx)
    if not mtm:
        return None, None
    if profit_target_pct and mtm["pnl_pct_of_max_profit"] >= profit_target_pct:
        return "profit_target", mtm
    if stop_loss_pct and mtm["pnl_pct_of_max_loss"] <= -abs(stop_loss_pct):
        return "stop_loss", mtm
    return None, mtm


def _close_position_early(position: Dict[str, Any], daily: pd.DataFrame, as_of_idx: int,
                           reason: str, mtm: Dict[str, Any]) -> Dict[str, Any]:
    """Closes a position early using its Black-Scholes mark-to-market value,
    instead of the simple expiry-day OTM check _close_position does. Keeps
    the same winner/price_change_pct fields populated (so existing win-rate/
    summary aggregation code works unmodified for both paths), and adds the
    real dollar figures alongside for anyone who wants them."""
    exit_day = daily.index[as_of_idx].date()
    entry_day = _parse_date(position.get("entry_date"))
    pnl = mtm["pnl"]
    max_loss = max(0.01, float(position.get("max_loss_dollars") or 0.01))
    out = dict(position)
    out.update({
        "status": "CLOSED",
        "exit_date": exit_day.isoformat(),
        "exit_price": mtm["spot"],
        "price_change": round(mtm["spot"] - float(position.get("entry_price") or 0.0), 2),
        "price_change_pct": round(pnl / max_loss * 100.0, 2),  # return-on-risk, comparable to the expiry-mode field
        "winner": pnl > 0,
        "outcome": "WIN" if pnl > 0 else "LOSS",
        "exit_check": f"Active management: closed early on {reason.replace('_', ' ')} "
                       f"({mtm['pnl_pct_of_max_profit']}% of max profit, {mtm['pnl_pct_of_max_loss']}% of max loss)",
        "days_held": (exit_day - entry_day).days,
        "exit_reason": reason,
        "dollar_pnl": round(pnl, 2),
        "dollar_pnl_pct_of_max_profit": mtm["pnl_pct_of_max_profit"],
        "dollar_pnl_pct_of_max_loss": mtm["pnl_pct_of_max_loss"],
        "cost_to_close": mtm["cost_to_close"],
    })
    return _strip_internal_trade_fields(out)


def _strip_internal_trade_fields(trade: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(trade)
    out.pop("entry_idx", None)
    out.pop("exit_idx", None)
    return out


def _format_legs(legs: List[Dict[str, Any]]) -> str:
    parts = []
    for leg in legs:
        side = "S" if str(leg.get("side") or "").upper() == "SELL" else "B"
        typ = "C" if str(leg.get("type") or "").upper() == "CALL" else "P"
        k = leg.get("strike")
        parts.append(f"{side}{k}{typ}")
    return " / ".join(parts)


def _summaries(trades: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_strategy: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_strategy_symbol: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        sym = t.get("symbol") or "?"
        strat = t.get("strategy") or t.get("trade_type") or "Strategy"
        by_symbol[sym].append(t)
        by_strategy[strat].append(t)
        by_strategy_symbol[(strat, sym)].append(t)

    def summarize(rows: List[Dict[str, Any]], extra: Dict[str, Any]) -> Dict[str, Any]:
        total = len(rows)
        wins = sum(1 for r in rows if r.get("winner"))
        losses = total - wins
        moves = [float(r.get("price_change_pct")) for r in rows if _is_finite(r.get("price_change_pct"))]
        avg_move = statistics.mean(moves) if moves else 0.0
        best = max(moves) if moves else 0.0
        worst = min(moves) if moves else 0.0
        return {
            **extra,
            "trades": total,
            "winners": wins,
            "losers": losses,
            "win_rate": round((wins / total * 100.0) if total else 0.0, 2),
            "avg_price_change_pct": round(avg_move, 2),
            "best_price_change_pct": round(best, 2),
            "worst_price_change_pct": round(worst, 2),
        }

    symbol_rows = sorted([summarize(rows, {"symbol": sym}) for sym, rows in by_symbol.items()], key=lambda x: (x["win_rate"], x["trades"], x["avg_price_change_pct"]), reverse=True)
    strat_rows = sorted([summarize(rows, {"strategy": strat}) for strat, rows in by_strategy.items()], key=lambda x: (x["win_rate"], x["trades"], x["avg_price_change_pct"]), reverse=True)
    strat_sym_rows = sorted([summarize(rows, {"strategy": k[0], "symbol": k[1]}) for k, rows in by_strategy_symbol.items()], key=lambda x: (x["strategy"], -x["win_rate"], -x["trades"]))
    return symbol_rows, strat_rows, strat_sym_rows


def _max_drawdown(sorted_pnls: List[float]) -> Tuple[float, float]:
    """Max drawdown (in dollars, and as a % of the peak equity at the time
    the drawdown started) over a chronological sequence of trade P&Ls."""
    equity = 0.0
    peak = 0.0
    max_dd_dollars = 0.0
    max_dd_pct = 0.0
    for pnl in sorted_pnls:
        equity += pnl
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd_dollars:
            max_dd_dollars = dd
            max_dd_pct = (dd / peak * 100.0) if peak > 0 else 0.0
    return round(max_dd_dollars, 2), round(max_dd_pct, 2)


def _stats(trades: List[Dict[str, Any]], open_positions: List[Dict[str, Any]], skipped: List[Dict[str, Any]], days_processed: int) -> Dict[str, Any]:
    closed_total = len(trades)
    open_total = len(open_positions)
    taken_total = closed_total + open_total
    winners = sum(1 for t in trades if t.get("winner"))
    losers = closed_total - winners
    moves = [float(t.get("price_change_pct")) for t in trades if _is_finite(t.get("price_change_pct"))]

    pnl_trades = [t for t in trades if _is_finite(t.get("dollar_pnl"))]
    pnls = [float(t["dollar_pnl"]) for t in pnl_trades]
    pnl_trades_sorted = sorted(pnl_trades, key=lambda t: (t.get("exit_date") or "", t.get("entry_date") or ""))
    pnls_chronological = [float(t["dollar_pnl"]) for t in pnl_trades_sorted]
    max_dd_dollars, max_dd_pct = _max_drawdown(pnls_chronological) if pnls_chronological else (0.0, 0.0)

    return {
        "total_trades": taken_total,
        "closed_trades": closed_total,
        "winners": winners,
        "losers": losers,
        "win_rate": round((winners / closed_total * 100.0) if closed_total else 0.0, 2),
        "avg_price_change_pct": round(statistics.mean(moves), 2) if moves else 0.0,
        "best_price_change_pct": round(max(moves), 2) if moves else 0.0,
        "worst_price_change_pct": round(min(moves), 2) if moves else 0.0,
        "open_trades": open_total,
        "skipped_signals": len(skipped),
        "days_processed": days_processed,
        # Dollar-based figures (Black-Scholes entry premium vs. either the
        # expiry OTM/loss check or the active-management early-exit value —
        # populated for every trade regardless of which mode was used).
        "total_pnl_dollars": round(sum(pnls), 2) if pnls else 0.0,
        "avg_pnl_dollars": round(statistics.mean(pnls), 2) if pnls else 0.0,
        "best_pnl_dollars": round(max(pnls), 2) if pnls else 0.0,
        "worst_pnl_dollars": round(min(pnls), 2) if pnls else 0.0,
        "max_drawdown_dollars": max_dd_dollars,
        "max_drawdown_pct": max_dd_pct,
    }


def _run_chronological_backtest(config: Dict[str, Any]) -> Dict[str, Any]:
    start_date = _parse_date(config.get("start_date") or config.get("start"))
    end_date = _parse_date(config.get("end_date"), date.today())
    if start_date >= end_date:
        raise ValueError("Start date must be before the end of available data")

    dte = int(config.get("dte") or 28)
    if dte not in SUPPORTED_DTES:
        raise ValueError(f"DTE must be one of {', '.join(map(str, SUPPORTED_DTES))}")
    trade_type = str(config.get("trade_type") or "CS").strip().upper()
    if trade_type not in SUPPORTED_TRADE_TYPES:
        raise ValueError(f"Trade type must be one of {', '.join(sorted(SUPPORTED_TRADE_TYPES))}")
    benchmark = _clean_symbol(config.get("benchmark") or "SPY") or "SPY"
    allow_overlap = _as_bool(config.get("allow_overlap"), False)
    earn_raw = config.get("earnings_avoid_days") if "earnings_avoid_days" in config else config.get("earnings_avoid")
    earnings_avoid_days = _parse_nonnegative_int(earn_raw, 35, 365)
    data_provider = str(config.get("data_provider") or "auto").strip().lower()
    if data_provider == "mongo":
        data_provider = "sqlite"
    if data_provider not in {"auto", "sqlite", "yfinance"}:
        data_provider = "auto"
    auto_fetch = _as_bool(config.get("auto_fetch"), True)
    try:
        otm_pct = float(config.get("otm_pct") or 2.0)
    except Exception:
        otm_pct = 2.0
    otm_pct = max(0.0, min(otm_pct, 50.0))
    try:
        strike_width = float(config.get("strike_width") or 5.0)
    except Exception:
        strike_width = 5.0
    strike_width = max(0.25, min(strike_width, 250.0))
    try:
        short_delta = float(config.get("short_delta") or 0.20)
    except Exception:
        short_delta = 0.20
    short_delta = max(0.01, min(abs(short_delta), 0.49))
    strike_mode = str(config.get("strike_mode") or "delta").strip().lower()
    if strike_mode not in {"delta", "otm"}:
        strike_mode = "delta"

    # Optional profit-target/stop-loss exit management (Black-Scholes mark-
    # to-market each day). Off by default — bypassed entirely means the
    # original expiry-day-only OTM check runs exactly as before.
    use_active_management = _as_bool(config.get("use_active_management"), False)
    try:
        profit_target_pct = float(config.get("profit_target_pct") or 50)
    except Exception:
        profit_target_pct = 50.0
    profit_target_pct = max(1.0, min(profit_target_pct, 500.0))
    try:
        stop_loss_pct = float(config.get("stop_loss_pct") or 50)
    except Exception:
        stop_loss_pct = 50.0
    stop_loss_pct = max(1.0, min(stop_loss_pct, 500.0))

    query_text = str(config.get("query_text") or "").strip()
    scanner_name = str(config.get("scanner_name") or "").strip()
    if not query_text and scanner_name:
        query_text = _scanner_query_text(scanner_name) or ""
    if not query_text:
        raise ValueError("Strategy expression is required")
    strategy_label = scanner_name or query_text[:80]

    raw_root = _parse_query(query_text)
    root = _expand_scan_nodes(raw_root, ())
    required_tfs = _required_timeframes(root)
    unsupported = [tf for tf in required_tfs if _normalize_tf(tf) not in SUPPORTED_PRICE_TFS]
    if unsupported:
        raise ValueError(f"Unsupported timeframe(s) for backtest: {', '.join(unsupported)}")

    raw_symbol_field = str(config.get("symbol") or "").strip()
    explicit_symbols = [_clean_symbol(s) for s in raw_symbol_field.split(",") if _clean_symbol(s)]
    if explicit_symbols:
        symbols = list(dict.fromkeys(explicit_symbols))
        watchlist_id = None
    else:
        watchlist_id = config.get("watchlist_id") or _preferred_watchlist_id()
        symbols = [_clean_symbol(s) for s in _watchlist_symbols(watchlist_id) if _clean_symbol(s)]
    symbols = list(dict.fromkeys([s for s in symbols if s]))
    if not symbols:
        raise ValueError("No symbols found for the selected watchlist")

    max_symbols = int(config.get("max_symbols") or 0)
    if max_symbols > 0:
        symbols = symbols[:max_symbols]

    fetch_symbols = list(dict.fromkeys(symbols + ([benchmark] if benchmark not in symbols else [])))
    histories: Dict[str, SymbolHistory] = {}
    errors: List[Dict[str, Any]] = []

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(fetch_symbols)))) as ex:
        futures = {ex.submit(_load_symbol_history, sym, required_tfs, start_date, end_date, data_provider, auto_fetch): sym for sym in fetch_symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                hist = fut.result()
            except Exception as e:
                hist = SymbolHistory(sym, pd.DataFrame(), {}, str(e))
            histories[sym] = hist
            if hist.error:
                errors.append({"symbol": sym, "error": hist.error})

    benchmark_hist = histories.get(benchmark)
    if not benchmark_hist or benchmark_hist.daily.empty:
        # Use the first valid symbol as a benchmark fallback so non-RS scanners
        # can still run when SPY history is unavailable.
        benchmark_hist = next((h for h in histories.values() if h.daily is not None and not h.daily.empty), None)

    valid_symbols = [s for s in symbols if histories.get(s) and not histories[s].daily.empty]
    if not valid_symbols:
        raise ValueError("No selected symbols had usable daily data")

    earnings_cache: Dict[str, Dict[str, Any]] = {}
    if get_symbol_calendar is not None:
        for s in valid_symbols:
            try:
                earnings_cache[s] = get_symbol_calendar(s) or {}
            except Exception:
                earnings_cache[s] = {}

    all_days = sorted({d for sym in valid_symbols for d in _trading_dates(histories[sym].daily) if start_date <= d <= end_date})
    if not all_days:
        raise ValueError("No trading days found at or after the selected start date")

    open_positions: List[Dict[str, Any]] = []
    closed_trades: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    daily_log: List[Dict[str, Any]] = []

    for day in all_days:
        exits_today = 0
        still_open: List[Dict[str, Any]] = []
        for pos in open_positions:
            daily = histories.get(pos.get("symbol"), SymbolHistory("", pd.DataFrame(), {})).daily
            closed_early = False
            if use_active_management:
                as_of_idx = _index_for_day(daily, day)
                if as_of_idx is not None and as_of_idx > int(pos.get("entry_idx") or 0):
                    try:
                        reason, mtm = _check_active_exit(pos, daily, as_of_idx, profit_target_pct, stop_loss_pct)
                        if reason and mtm:
                            closed_trades.append(_close_position_early(pos, daily, as_of_idx, reason, mtm))
                            exits_today += 1
                            closed_early = True
                    except Exception as e:
                        skipped.append({"date": day.isoformat(), "symbol": pos.get("symbol"), "reason": f"active exit check failed: {e}"})
            if closed_early:
                continue
            if _parse_date(pos.get("expiry_date")) <= day:
                try:
                    closed_trades.append(_close_position(pos, daily))
                    exits_today += 1
                except Exception as e:
                    skipped.append({"date": day.isoformat(), "symbol": pos.get("symbol"), "reason": f"exit failed: {e}"})
            else:
                still_open.append(pos)
        open_positions = still_open

        open_symbols = {p.get("symbol") for p in open_positions}
        contexts: List[Dict[str, Any]] = []
        entry_indices: Dict[str, int] = {}
        for sym in valid_symbols:
            hist = histories[sym]
            if not _has_bar_on_day(hist.daily, day):
                continue
            if not allow_overlap and sym in open_symbols:
                continue
            entry_idx = _price_row_for_day(hist.daily, day)
            if entry_idx is None or entry_idx < 25:
                continue
            try:
                ctx = _build_ctx_for_day(sym, hist, benchmark_hist, day, benchmark, required_tfs)
            except Exception as e:
                skipped.append({"date": day.isoformat(), "symbol": sym, "reason": f"context failed: {e}"})
                continue
            if ctx is None:
                continue
            earn_snapshot = _earnings_snapshot_for_day(sym, day, earnings_cache)
            ctx.update(earn_snapshot)
            if earnings_avoid_days > 0:
                ed = ctx.get("earn_days")
                try:
                    ed_i = int(ed)
                except Exception:
                    ed_i = 999
                if 0 <= ed_i <= earnings_avoid_days:
                    skipped.append({"date": day.isoformat(), "symbol": sym, "reason": f"entry skipped: earnings in {ed_i}d within {earnings_avoid_days}d avoid window"})
                    continue
            contexts.append(ctx)
            entry_indices[sym] = entry_idx

        _apply_cross_sectional_fields(contexts, root, benchmark)

        signals_today = 0
        entries_today = 0
        for ctx in contexts:
            sym = ctx.get("symbol")
            try:
                ok = bool(_eval(root, ctx, shift=0, tf_default="1d"))
            except Exception as e:
                skipped.append({"date": day.isoformat(), "symbol": sym, "reason": f"scanner eval failed: {e}"})
                continue
            if not ok:
                continue
            signals_today += 1
            if not allow_overlap and sym in {p.get("symbol") for p in open_positions}:
                skipped.append({"date": day.isoformat(), "symbol": sym, "reason": "signal ignored because a trade is already open"})
                continue
            try:
                reason = _explain(root, ctx, shift=0, tf_default="1d")
            except Exception:
                reason = _flatten_atoms(root)
            pos, err = _build_option_position(
                sym,
                trade_type,
                day,
                entry_indices.get(sym, 0),
                histories[sym].daily,
                dte,
                strategy_label,
                query_text,
                reason,
                ctx,
                otm_pct,
                strike_width,
                short_delta,
                strike_mode,
            )
            if pos is None:
                skipped.append({"date": day.isoformat(), "symbol": sym, "reason": err or "could not create trade"})
                continue
            open_positions.append(pos)
            entries_today += 1

        if signals_today or entries_today or exits_today:
            daily_log.append({
                "date": day.isoformat(),
                "signals": signals_today,
                "entries": entries_today,
                "exits": exits_today,
                "open_positions": len(open_positions),
            })

    # Do not force-close positions that have not reached DTE. They are included
    # as open trades so win-rate metrics remain based on completed DTE exits.
    open_public = [_strip_internal_trade_fields(p) for p in open_positions]
    symbol_perf, strategy_perf, strategy_symbol_perf = _summaries(closed_trades)
    stats = _stats(closed_trades, open_public, skipped, len(all_days))

    result = {
        "ok": True,
        "run_id": str(uuid.uuid4()),
        "run_name": str(config.get("run_name") or strategy_label or "Backtest Run").strip()[:160],
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "engine": "event_driven_chronological_sqlite_daily_simple_dte",
        "config": {
            "query_text": query_text,
            "scanner_name": scanner_name,
            "strategy_label": strategy_label,
            "watchlist_id": watchlist_id,
            "symbol": ", ".join(explicit_symbols) if explicit_symbols else None,
            "benchmark": benchmark,
            "start_date": start_date.isoformat(),
            "end_date": all_days[-1].isoformat(),
            "trade_type": trade_type,
            "dte": dte,
            "strike_mode": strike_mode,
            "use_active_management": use_active_management,
            "profit_target_pct": profit_target_pct if use_active_management else None,
            "stop_loss_pct": stop_loss_pct if use_active_management else None,
            "short_delta": short_delta,
            "strike_width": strike_width,
            "otm_pct": otm_pct,
            "allow_overlap": allow_overlap,
            "earnings_avoid_days": earnings_avoid_days,
            "data_provider": data_provider,
            "auto_fetch": auto_fetch,
            "universe_count": len(valid_symbols),
            "universe": valid_symbols,
        },
        "stats": stats,
        "trades": closed_trades,
        "open_trades": open_public,
        "symbol_performance": symbol_perf,
        "strategy_summary": strategy_perf,
        "strategy_symbol_summary": strategy_symbol_perf,
        "daily_log": daily_log[-500:],
        "skipped": skipped[:500],
        "errors": errors,
        "notes": [
            "The simulator advances one trading day at a time and evaluates the scanner using only data available through that simulated day.",
            "Daily OHLCV is read from the local SQLite market-data cache when available; missing data can be filled from yfinance and cached for later runs.",
            "The current result mode is simple DTE scoring: no dollar P/L, only winner/loser based on the stock close at the selected DTE.",
            "For simple directional spreads, the entry spot is used as the short/reference strike. Example: a PS entered at 136 wins if the DTE close remains at or above 136.",
            "Credit strategies win when price stays beyond the short strike in the favorable direction; debit strategies require price to move through the opposite short strike. Profit targets, stop losses, option pricing, and intraday exit checks are reserved for the next exit-management layer.",
        ],
    }

    result = _json_safe(result)
    if _as_bool(config.get("save_run"), False):
        save = get_market_data_store().save_backtest_result(result, config.get("run_name"))
        result["saved"] = bool(save.get("ok"))
        result["save_result"] = save
    return result


@backtest_bp.route("/api/watchlists", methods=["GET"])
def api_backtest_watchlists():
    _ensure_scanner_tables()
    return jsonify({"watchlists": _watchlists(), "preferred_watchlist_id": _preferred_watchlist_id()})


@backtest_bp.route("/api/scanners", methods=["GET"])
def api_backtest_scanners():
    _ensure_scanner_tables()
    con = _scanner_conn()
    try:
        rows = con.execute(
            """
            SELECT id, name, description, query_text, benchmark, updated_at, last_run_count
            FROM scanner_definitions
            ORDER BY lower(name)
            """
        ).fetchall()
        saved = [dict(r) for r in rows]
    finally:
        con.close()
    return jsonify({"saved_scanners": saved, "builtin_scanners": BUILTIN_SCANNERS})


def _backtest_symbols_from_payload(payload: Dict[str, Any], include_benchmark: bool = True) -> Tuple[Optional[Any], List[str]]:
    symbol = _clean_symbol(payload.get("symbol"))
    if symbol:
        symbols = [symbol]
        watchlist_id = None
    else:
        watchlist_id = payload.get("watchlist_id") or _preferred_watchlist_id()
        symbols = [_clean_symbol(s) for s in _watchlist_symbols(watchlist_id) if _clean_symbol(s)]
    benchmark = _clean_symbol(payload.get("benchmark") or "SPY") or "SPY"
    if include_benchmark and benchmark:
        symbols.append(benchmark)
    max_symbols = int(payload.get("max_symbols") or 0)
    symbols = list(dict.fromkeys([s for s in symbols if s]))
    if max_symbols > 0 and not symbol:
        # Keep benchmark even when limiting the watchlist.
        core = [s for s in symbols if s != benchmark][:max_symbols]
        symbols = list(dict.fromkeys(core + ([benchmark] if benchmark else [])))
    return watchlist_id, symbols


@backtest_bp.route("/api/market-data/status", methods=["GET"])
def api_backtest_market_data_status():
    return jsonify(get_market_data_store().status())


@backtest_bp.route("/api/market-data/sync", methods=["POST"])
def api_backtest_market_data_sync():
    _ensure_scanner_tables()
    payload = request.get_json(force=True) or {}
    try:
        years = int(payload.get("years") or 5)
    except Exception:
        years = 5
    years = max(1, min(years, 20))
    _watchlist_id, symbols = _backtest_symbols_from_payload(payload, include_benchmark=True)
    if not symbols:
        return jsonify({"ok": False, "error": "No symbols found to sync"}), 400
    result = get_market_data_store().sync_daily_symbols(symbols, years=years)
    code = 200 if result.get("ok") else 503
    return jsonify(result), code


@backtest_bp.route("/api/runs", methods=["GET"])
def api_backtest_runs():
    try:
        limit = int(request.args.get("limit") or 50)
    except Exception:
        limit = 50
    run_name = (request.args.get("run_name") or request.args.get("name") or "").strip() or None
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    result = get_market_data_store().list_backtest_runs(limit=limit, run_name=run_name, date_from=date_from, date_to=date_to)
    code = 200 if result.get("ok") else 503
    return jsonify(result), code


@backtest_bp.route("/api/runs/<run_id>", methods=["GET"])
def api_backtest_run_detail(run_id: str):
    result = get_market_data_store().get_backtest_run(run_id)
    code = 200 if result.get("ok") else 404 if result.get("error") == "Run not found" else 503
    return jsonify(result), code


@backtest_bp.route("/api/save", methods=["POST"])
def api_backtest_save_result():
    payload = request.get_json(force=True) or {}
    result = payload.get("result") or payload
    if not isinstance(result, dict) or not result.get("stats"):
        return jsonify({"ok": False, "error": "Backtest result payload is required"}), 400
    if not result.get("run_id"):
        result["run_id"] = str(uuid.uuid4())
    save = get_market_data_store().save_backtest_result(result, payload.get("run_name") or result.get("run_name"))
    code = 200 if save.get("ok") else 503
    return jsonify(save), code


@backtest_bp.route("/api/run", methods=["POST"])
def api_backtest_run():
    _ensure_scanner_tables()
    payload = request.get_json(force=True) or {}
    try:
        result = _run_chronological_backtest(payload)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Backtest failed: {e}"}), 500


def _parse_api_test_targets(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        items = raw
    else:
        lines = str(raw or "").splitlines()
        items = []
        for line in lines:
            s = str(line or "").strip()
            if not s or s.startswith("#"):
                continue
            items.append(s)
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(items):
        if isinstance(item, dict):
            method = str(item.get("method") or "GET").strip().upper()
            path = str(item.get("path") or item.get("url") or "").strip()
            label = str(item.get("label") or path or f"Endpoint {idx + 1}").strip()
            body = item.get("body")
            timeout = float(item.get("timeout_secs") or 0) if item.get("timeout_secs") is not None else None
        else:
            raw_line = str(item).strip()
            method = "GET"
            body = None
            timeout = None
            label = ""
            if "||" in raw_line:
                raw_line, body_part = [x.strip() for x in raw_line.split("||", 1)]
                body = body_part or None
            parts = raw_line.split(None, 1)
            if parts and parts[0].upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
                method = parts[0].upper()
                path = parts[1].strip() if len(parts) > 1 else ""
            else:
                path = raw_line
            if " | " in path:
                path, label = [x.strip() for x in path.split(" | ", 1)]
            label = label or path or f"Endpoint {idx + 1}"
        path = str(path or "").strip()
        if not path:
            continue
        if not path.startswith("http://") and not path.startswith("https://"):
            if not path.startswith("/"):
                path = "/" + path
        out.append({
            "endpoint_key": f"{method} {path}",
            "method": method,
            "path": path,
            "label": label,
            "body": body,
            "timeout_secs": timeout,
        })
    return out


def _run_api_test_suite(config: Dict[str, Any]) -> Dict[str, Any]:
    base_url = str(config.get("base_url") or request.host_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("Base URL is required")
    targets = _parse_api_test_targets(config.get("endpoints") or config.get("endpoints_text") or API_TEST_DEFAULT_ENDPOINTS)
    if not targets:
        raise ValueError("No API endpoints were provided")
    top_n = max(1, min(int(config.get("top_n") or 10), 100))
    timeout_secs = float(config.get("timeout_secs") or 20)
    timeout_secs = max(1.0, min(timeout_secs, 120.0))
    run_id = str(config.get("run_id") or uuid.uuid4())
    created_at = datetime.utcnow().replace(microsecond=0).isoformat()
    results: List[Dict[str, Any]] = []
    notes: List[str] = []
    for idx, target in enumerate(targets, start=1):
        method = target.get("method", "GET")
        path = str(target.get("path") or "").strip()
        label = str(target.get("label") or path or f"Endpoint {idx}").strip()
        url = path if path.startswith(("http://", "https://")) else f"{base_url}{path}"
        body = target.get("body")
        req_timeout = float(target.get("timeout_secs") or timeout_secs)
        started = datetime.utcnow().replace(microsecond=0).isoformat()
        t0 = datetime.utcnow()
        status_code = None
        response_size = None
        content_type = None
        preview = ""
        error = None
        ok = False
        try:
            data = None
            headers = {"User-Agent": "OptionsTrader-ApiTester/1.0", "Accept": "application/json,text/plain,*/*"}
            if method in {"POST", "PUT", "PATCH"}:
                if isinstance(body, (dict, list)):
                    data = json.dumps(body).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                elif body not in {None, ""}:
                    data = str(body).encode("utf-8")
                    headers["Content-Type"] = "text/plain; charset=utf-8"
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                status_code = int(getattr(resp, "status", resp.getcode()))
                content_type = resp.headers.get_content_type() if getattr(resp, "headers", None) else None
                raw = resp.read(4096)
                response_size = len(raw)
                preview = raw.decode("utf-8", errors="replace")[:250]
                ok = 200 <= status_code < 500 and status_code < 400
        except urllib.error.HTTPError as e:
            status_code = int(getattr(e, 'code', 0) or 0)
            content_type = getattr(e.headers, 'get_content_type', lambda: None)() if getattr(e, 'headers', None) else None
            raw = e.read(4096) if hasattr(e, 'read') else b''
            response_size = len(raw)
            preview = raw.decode('utf-8', errors='replace')[:250]
            error = f'HTTP {status_code}'
            ok = False
        except Exception as e:
            error = str(e)
            ok = False
        elapsed_ms = round((datetime.utcnow() - t0).total_seconds() * 1000.0, 2)
        results.append({
            "endpoint_no": idx,
            "endpoint_key": target.get("endpoint_key") or f"{method} {path}",
            "method": method,
            "path": path,
            "label": label,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
            "ok": ok,
            "error": error,
            "response_size": response_size,
            "content_type": content_type,
            "response_preview": preview,
            "started_at": started,
            "created_at": created_at,
        })
    results.sort(key=lambda x: float(x.get("elapsed_ms") or 0.0), reverse=True)
    slowest = results[:top_n]
    failed = sum(1 for r in results if not r.get("ok"))
    ok_count = len(results) - failed
    avg_ms = round(sum(float(r.get("elapsed_ms") or 0.0) for r in results) / len(results), 2) if results else 0.0
    min_ms = round(min(float(r.get("elapsed_ms") or 0.0) for r in results), 2) if results else 0.0
    max_ms = round(max(float(r.get("elapsed_ms") or 0.0) for r in results), 2) if results else 0.0
    if failed:
        notes.append(f"{failed} endpoint(s) failed or returned non-2xx status.")
    notes.append("Endpoints are sorted by elapsed_ms descending so the slowest APIs appear first.")
    return {
        "ok": True,
        "run_id": run_id,
        "run_name": str(config.get("run_name") or "API Tester Run").strip()[:160],
        "created_at": created_at,
        "base_url": base_url,
        "top_n": top_n,
        "timeout_secs": timeout_secs,
        "request_count": len(results),
        "ok_count": ok_count,
        "failed_count": failed,
        "avg_ms": avg_ms,
        "min_ms": min_ms,
        "max_ms": max_ms,
        "results": results,
        "slowest": slowest,
        "notes": notes,
        "config": {
            "base_url": base_url,
            "top_n": top_n,
            "timeout_secs": timeout_secs,
            "endpoints_count": len(targets),
        },
    }


@backtest_bp.route("/api/api-tester/run", methods=["POST"])
def api_backtest_api_tester_run():
    payload = request.get_json(force=True) or {}
    try:
        result = _run_api_test_suite(payload)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"API test failed: {e}"}), 500
    if _as_bool(payload.get("save_run"), True):
        save = get_market_data_store().save_api_test_result(result, payload.get("run_name") or result.get("run_name"))
        result["saved"] = bool(save.get("ok"))
        result["save_result"] = save
    return jsonify(_json_safe(result))


@backtest_bp.route("/api/api-tester/runs", methods=["GET"])
def api_backtest_api_tester_runs():
    try:
        limit = int(request.args.get("limit") or 50)
    except Exception:
        limit = 50
    run_name = (request.args.get("run_name") or request.args.get("name") or "").strip() or None
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    result = get_market_data_store().list_api_test_runs(limit=limit, run_name=run_name, date_from=date_from, date_to=date_to)
    code = 200 if result.get("ok") else 503
    return jsonify(result), code


@backtest_bp.route("/api/api-tester/runs/<run_id>", methods=["GET"])
def api_backtest_api_tester_run_detail(run_id: str):
    result = get_market_data_store().get_api_test_run(run_id)
    code = 200 if result.get("ok") else 404 if result.get("error") == "Run not found" else 503
    return jsonify(result), code

