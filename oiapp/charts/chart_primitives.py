"""Support/resistance primitives and channel selection."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple
import pandas as pd

def normalize_tf(tf: str) -> str:
    tf = str(tf or '1d').strip().lower()
    aliases = {
        'daily': '1d', 'day': '1d', '1d': '1d',
        'weekly': '1w', 'week': '1w', '1wk': '1w', '1w': '1w',
        'monthly': '1m', 'month': '1m', '1mo': '1m', '1m': '1m',
        '4h': '4h', '2h': '2h', '1h': '1h', '60m': '1h', '60min': '1h',
        '15m': '15m', '5m': '5m',
    }
    return aliases.get(tf, tf)

def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        return df[['Open', 'High', 'Low', 'Close', 'Volume']].resample(rule).agg({
            'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum',
        }).dropna()
    except Exception:
        return pd.DataFrame()

def parse_lookback_spec(spec: str) -> Tuple[str, int]:
    s = str(spec or '1d').strip().lower()
    if s in {'1m', 'monthly', 'month'}:
        return '1m', 10
    if s in {'1w', 'weekly', 'week', '1wk'}:
        return '1w', 52
    if s in {'1d', 'daily', 'day'}:
        return '1d', 252
    if s in {'4h', '2h', '1h', '15m', '5m'}:
        return s, 0
    return '1d', 252

def tv_sr_channels(df: pd.DataFrame, prd: int = 10, channel_w_pct: float = 5.0, loopback: int = 290, max_sr: int = 6) -> List[Dict[str, float]]:
    if df is None or df.empty or len(df) < prd * 2 + 5:
        return []
    high = pd.to_numeric(df['High'], errors='coerce')
    low = pd.to_numeric(df['Low'], errors='coerce')
    close = pd.to_numeric(df['Close'], errors='coerce')
    open_ = pd.to_numeric(df['Open'], errors='coerce')
    if high.isna().all() or low.isna().all() or close.isna().all() or open_.isna().all():
        return []

    n = len(close)
    prd = max(4, int(prd or 10))
    loopback = max(1, int(loopback or 290))
    max_sr = max(1, int(max_sr or 6))
    recent_hi = float(high.iloc[max(0, n - 300):].max())
    recent_lo = float(low.iloc[max(0, n - 300):].min())
    cwidth = (recent_hi - recent_lo) * float(channel_w_pct or 5.0) / 100.0
    if not math.isfinite(cwidth) or cwidth <= 0:
        return []

    def is_ph(i: int) -> bool:
        if i < prd or i + prd >= n:
            return False
        v = float(high.iloc[i])
        return all(v >= float(high.iloc[i - j]) for j in range(1, prd + 1)) and all(v >= float(high.iloc[i + j]) for j in range(1, prd + 1))

    def is_pl(i: int) -> bool:
        if i < prd or i + prd >= n:
            return False
        v = float(low.iloc[i])
        return all(v <= float(low.iloc[i - j]) for j in range(1, prd + 1)) and all(v <= float(low.iloc[i + j]) for j in range(1, prd + 1))

    pivot_vals: List[float] = []
    start_idx = max(prd, n - loopback - prd)
    end_idx = max(prd, n - prd)
    for i in range(start_idx, end_idx):
        if is_ph(i):
            pivot_vals.append(float(high.iloc[i]))
        elif is_pl(i):
            pivot_vals.append(float(low.iloc[i]))
    if not pivot_vals:
        return []

    def get_sr_vals(idx: int):
        lo = hi = pivot_vals[idx]
        numpp = 0
        for pv in pivot_vals:
            wdth = (hi - pv) if pv <= hi else (pv - lo)
            if wdth <= cwidth:
                if pv <= hi:
                    lo = min(lo, pv)
                else:
                    hi = max(hi, pv)
                numpp += 20
        return hi, lo, numpp

    channels: List[Dict[str, float]] = []
    used: set[int] = set()
    for i in range(len(pivot_vals)):
        if i in used:
            continue
        hi, lo, strength = get_sr_vals(i)
        touches = 0
        for j in range(max(0, n - loopback), n):
            hj = float(high.iloc[j])
            lj = float(low.iloc[j])
            if (lj <= hi and lj >= lo) or (hj <= hi and hj >= lo):
                touches += 1
        strength += touches
        for k, pv in enumerate(pivot_vals):
            if lo <= pv <= hi:
                used.add(k)
        channels.append({'hi': float(hi), 'lo': float(lo), 'strength': float(strength), 'mid': float((hi + lo) / 2.0)})
        if len(channels) >= max_sr * 3:
            break

    channels.sort(key=lambda c: (-c['strength'], abs(float(close.iloc[-1]) - c['mid'])))
    final: List[Dict[str, float]] = []
    for ch in channels:
        overlap = any(not (ch['hi'] < f['lo'] or ch['lo'] > f['hi']) for f in final)
        if not overlap:
            final.append(ch)
        if len(final) >= max_sr:
            break
    return final

def nearest_levels(channels: List[Dict[str, float]], spot: float, count: int = 3) -> Tuple[List[Dict[str, float]], List[Dict[str, float]]]:
    count = max(1, min(int(count or 3), 10))
    supports = []
    resistances = []
    for ch in channels:
        if ch['hi'] <= spot:
            supports.append({'level': ch['hi'], 'lo': ch['lo'], 'hi': ch['hi'], 'strength': ch['strength'], 'distance_pct': abs((spot - ch['hi']) / max(1e-9, ch['hi'])) * 100.0, 'type': 'support'})
        elif ch['lo'] >= spot:
            resistances.append({'level': ch['lo'], 'lo': ch['lo'], 'hi': ch['hi'], 'strength': ch['strength'], 'distance_pct': abs((ch['lo'] - spot) / max(1e-9, ch['lo'])) * 100.0, 'type': 'resistance'})
        else:
            supports.append({'level': ch['hi'], 'lo': ch['lo'], 'hi': ch['hi'], 'strength': ch['strength'], 'distance_pct': 0.0, 'type': 'support'})
            resistances.append({'level': ch['lo'], 'lo': ch['lo'], 'hi': ch['hi'], 'strength': ch['strength'], 'distance_pct': 0.0, 'type': 'resistance'})
    supports.sort(key=lambda x: (x['distance_pct'], -x['strength']))
    resistances.sort(key=lambda x: (x['distance_pct'], -x['strength']))
    return supports[:count], resistances[:count]
