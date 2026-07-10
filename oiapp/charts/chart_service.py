"""Chart data fetching and payload assembly."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from ..services.yf_session import get_ticker, safe_history
from ..services.market import get_spot
from ..services.oi_wall_service import oi_wall_context
from .chart_cache import cache_key
from .chart_layouts import CHART_TIMEFRAMES
from .chart_overlays import ema, sma, rsi, macd, bbands, true_range
from .chart_primitives import normalize_tf, resample, tv_sr_channels, nearest_levels, parse_lookback_spec

def _pick_frame(primary: Optional[pd.DataFrame], fallback: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if primary is not None and not primary.empty:
        return primary
    if fallback is not None and not fallback.empty:
        return fallback
    return None


def _safe_spot_value(spot: Any, fallback: Optional[float]) -> Optional[float]:
    try:
        if spot is None:
            raise ValueError
        val = float(spot)
        if pd.isna(val) or not pd.notna(val):
            raise ValueError
        return val
    except Exception:
        try:
            if fallback is None:
                return None
            val = float(fallback)
            return None if pd.isna(val) else val
        except Exception:
            return None


def history_tf(symbol: str, timeframe: str) -> Tuple[pd.DataFrame, str]:
    symbol = str(symbol or '').strip().upper() or 'SPY'
    tf = normalize_tf(timeframe)
    try:
        tk = get_ticker(symbol)
    except Exception:
        tk = None

    def _fetch(period: str, interval: str, prepost: bool = False):
        if tk is None:
            return None
        try:
            return tk.history(period=period, interval=interval, prepost=prepost, auto_adjust=False)
        except Exception:
            return None

    df = None
    if tf == '5m':
        df = _pick_frame(_fetch('60d', '5m', True), safe_history(symbol, period='60d', interval='5m', retries=2))
    elif tf == '15m':
        df = _pick_frame(_fetch('60d', '15m', True), safe_history(symbol, period='60d', interval='15m', retries=2))
    elif tf == '1h':
        df = _pick_frame(_fetch('730d', '1h', True), safe_history(symbol, period='730d', interval='1h', retries=2))
    elif tf == '2h':
        df = _pick_frame(_fetch('730d', '1h', True), safe_history(symbol, period='730d', interval='1h', retries=2))
        if df is not None and not df.empty:
            df = resample(df, '2H')
    elif tf == '4h':
        df = _pick_frame(_fetch('730d', '1h', True), safe_history(symbol, period='730d', interval='1h', retries=2))
        if df is not None and not df.empty:
            df = resample(df, '4H')
    elif tf == '1w':
        df = _pick_frame(_fetch('10y', '1wk', False), safe_history(symbol, period='10y', interval='1wk', retries=2))
    elif tf == '1m':
        df = _pick_frame(_fetch('10y', '1mo', False), safe_history(symbol, period='10y', interval='1mo', retries=2))
    else:
        df = _pick_frame(_fetch('5y', '1d', True), safe_history(symbol, period='5y', interval='1d', retries=2))

    if df is None or getattr(df, 'empty', True):
        return pd.DataFrame(), tf

    df = df.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors='coerce')
    df = df[[c for c in ['Open', 'High', 'Low', 'Close', 'Volume'] if c in df.columns]].dropna()
    return df if not df.empty else pd.DataFrame(), tf

def serialize_df(df: pd.DataFrame) -> List[Dict[str, Any]]:
    rows = []
    for idx, row in df.iterrows():
        ts = idx.to_pydatetime() if hasattr(idx, 'to_pydatetime') else idx
        if hasattr(ts, 'strftime'):
            ts = ts.strftime('%Y-%m-%d %H:%M:%S')
        rows.append({'ts': ts, 'open': float(row['Open']), 'high': float(row['High']), 'low': float(row['Low']), 'close': float(row['Close']), 'volume': float(row['Volume'])})
    return rows

def make_series(df: pd.DataFrame) -> Dict[str, List[Optional[float]]]:
    c = df['Close'].astype(float)
    rsi14 = rsi(c, 14)
    ema90 = ema(rsi14, 90)
    macd_line, macd_sig, macd_hist = macd(c)
    bb_lower, bb_mid, bb_upper = bbands(c, 20, 2.0)
    return {
        'ema20': ema(c, 20).round(4).tolist(),
        'ema50': ema(c, 50).round(4).tolist(),
        'ema200': ema(c, 200).round(4).tolist(),
        'sma20': sma(c, 20).round(4).tolist(),
        'sma50': sma(c, 50).round(4).tolist(),
        'rsi14': rsi14.round(4).tolist(),
        'ema_rsi90': ema90.round(4).tolist(),
        'rsi_diff_90': (rsi14 - ema90).round(4).tolist(),
        'macd': macd_line.round(4).tolist(),
        'macd_signal': macd_sig.round(4).tolist(),
        'macd_hist': macd_hist.round(4).tolist(),
        'bb_lower': bb_lower.round(4).tolist(),
        'bb_mid': bb_mid.round(4).tolist(),
        'bb_upper': bb_upper.round(4).tolist(),
        'atr14': true_range(df).ewm(alpha=1/14, adjust=False).mean().round(4).tolist(),
        'vol_sma20': sma(df['Volume'].astype(float), 20).round(4).tolist(),
    }

def build_chart_payload(symbol: str, timeframe: str = '1d', expiry: str | None = None) -> Dict[str, Any]:
    df, tf = history_tf(symbol, timeframe)
    if df is None or df.empty:
        raise ValueError(f'No chart data available for {symbol} {tf}')

    spot = _safe_spot_value(get_spot(symbol), None)
    if spot is None:
        try:
            spot = float(df['Close'].iloc[-1])
            if pd.isna(spot):
                spot = None
        except Exception:
            spot = None

    close_last = None
    try:
        close_last = float(df['Close'].iloc[-1])
        if pd.isna(close_last):
            close_last = None
    except Exception:
        close_last = None

    spot_for_levels = spot if spot is not None else close_last
    if spot_for_levels is None:
        raise ValueError(f'No valid spot available for {symbol} {tf}')

    channels = tv_sr_channels(df)
    supports, resistances = nearest_levels(channels, float(spot_for_levels))
    try:
        walls = oi_wall_context(symbol, float(spot_for_levels), expiry=expiry)
    except Exception:
        walls = {'bias': 'NEUTRAL', 'walls': None, 'breach_context': 'Unavailable'}

    latest = df.iloc[-1]
    series = make_series(df)
    return {
        'symbol': symbol,
        'timeframe': tf,
        'spot': float(spot_for_levels) if spot_for_levels is not None else None,
        'bars': serialize_df(df),
        'series': series,
        'levels': {'supports': supports, 'resistances': resistances, 'channels': channels},
        'walls': walls,
        'latest': {
            'open': float(latest['Open']),
            'high': float(latest['High']),
            'low': float(latest['Low']),
            'close': float(latest['Close']),
            'volume': float(latest['Volume']),
            'rsi14': float(series['rsi14'][-1]) if series.get('rsi14') else None,
            'rsi_diff_90': float(series['rsi_diff_90'][-1]) if series.get('rsi_diff_90') else None,
            'macd_hist': float(series['macd_hist'][-1]) if series.get('macd_hist') else None,
            'atr14': float(series['atr14'][-1]) if series.get('atr14') else None,
            'vol_ratio': (float(latest['Volume']) / float(series['vol_sma20'][-1])) if series.get('vol_sma20') and series['vol_sma20'][-1] not in (None, 0) else None,
        },
        'meta': {'bars': int(len(df)), 'timeframe_label': tf, 'chart_period': parse_lookback_spec(tf)[1]},
    }
