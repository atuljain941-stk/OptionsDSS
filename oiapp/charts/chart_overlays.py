"""Technical indicator calculations used by chart payloads."""
from __future__ import annotations

from typing import Tuple
import numpy as np
import pandas as pd

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=max(1, int(n)), adjust=False).mean()

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(max(1, int(n))).mean()

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1 / period, adjust=False).mean().replace(0, np.nan)
    rs = avg_g / avg_l
    return (100 - (100 / (1 + rs))).fillna(50.0)

def macd(close: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(close, 12) - ema(close, 26)
    sig = ema(macd_line, 9)
    hist = macd_line - sig
    return macd_line, sig, hist

def bbands(close: pd.Series, period: int = 20, n_std: float = 2.0):
    mid = sma(close, period)
    std = close.rolling(period).std(ddof=0)
    upper = mid + n_std * std
    lower = mid - n_std * std
    return lower.bfill(), mid.bfill(), upper.bfill()

def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df['Close'].shift(1)
    tr1 = (df['High'] - df['Low']).abs()
    tr2 = (df['High'] - prev).abs()
    tr3 = (df['Low'] - prev).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
