"""
smart_money_scanners.py

Python/pandas port of the ThinkScript accumulation/distribution scanners.
Designed to plug into oiapp's snapshot pipeline (price_cache -> scanner_snapshot_cache).

Each function takes a DataFrame of OHLCV data (indexed by date, ascending order,
columns: open, high, low, close, volume) for a single symbol, plus an optional
benchmark DataFrame (same shape, e.g. SPY) for relative-strength calcs.

Output: a DataFrame with the daily boolean scan result plus the underlying
factor columns, so you can either take the latest row for a live scan or
backtest the historical hit rate before wiring it into scanner_builder.py.

Suggested integration point:
    - Compute nightly per-symbol in your existing snapshot job (same place
      technical_snapshot.py runs).
    - Write the tail row (or last N rows) into scanner_snapshot_cache with
      primitive names "SmartMoneyAccum" / "SmartMoneyDist", consistent with
      your existing primitive naming (RSIDiff90, StrongBullCandle, etc.)
    - Add as selectable primitives in the Pattern Search / scanner builder UI.
"""

import pandas as pd
import numpy as np


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _pct_change(close: pd.Series) -> pd.Series:
    return close.pct_change() * 100


def _up_down_volume_ratio(close: pd.Series, volume: pd.Series, lookback: int) -> pd.Series:
    up_day = close > close.shift(1)
    down_day = close < close.shift(1)
    up_vol = volume.where(up_day, 0)
    down_vol = volume.where(down_day, 0)
    sum_up = up_vol.rolling(lookback).sum()
    sum_down = down_vol.rolling(lookback).sum()
    return sum_up / (sum_down + 1)


def _distribution_day_count(close: pd.Series, volume: pd.Series,
                             dist_lookback: int, pct_threshold: float,
                             vol_multiplier: float) -> pd.Series:
    pct_chg = _pct_change(close)
    avg_vol50 = volume.rolling(50).mean()
    is_dist_day = (pct_chg <= pct_threshold) & (volume > avg_vol50 * vol_multiplier)
    return is_dist_day.astype(int).rolling(dist_lookback).sum()


def _rs_line(close: pd.Series, bench_close: pd.Series) -> pd.Series:
    aligned = close.align(bench_close, join="left")
    return aligned[0] / aligned[1].reindex(aligned[0].index).ffill()


# ---------------------------------------------------------------------------
# Scanner 1: Smart Money Accumulation (pre-breakout, long)
# ---------------------------------------------------------------------------

def scan_accumulation(
    df: pd.DataFrame,
    bench_df: pd.DataFrame = None,
    udvr_lookback: int = 20,
    udvr_threshold: float = 1.3,
    dist_lookback: int = 25,
    max_dist_days: int = 2,
    dist_pct_threshold: float = -1.5,
    dist_vol_multiplier: float = 1.5,
    pct_from_high_max: float = 15.0,
    pocket_pivot_lookback: int = 10,
    min_avg_vol: float = 300_000,
) -> pd.DataFrame:
    """
    Returns df with added columns and a boolean 'scan' column.
    Requires columns: high, low, close, volume.
    bench_df (optional): same shape, used for RS-line leadership check.
    """
    out = df.copy()
    close, volume, high = out["close"], out["volume"], out["high"]

    # Up/Down Volume Ratio
    out["udvr"] = _up_down_volume_ratio(close, volume, udvr_lookback)

    # Distribution day count (want this LOW for accumulation)
    out["dist_day_count"] = _distribution_day_count(
        close, volume, dist_lookback, dist_pct_threshold, dist_vol_multiplier
    )

    # Base tightness: % below 52-week high
    high_52 = high.rolling(252, min_periods=20).max()
    out["pct_below_high"] = (high_52 - close) / high_52 * 100

    # Pocket pivot: up day, volume exceeds max down-day volume of last N days
    up_day = close > close.shift(1)
    down_day = close < close.shift(1)
    down_vol_only = volume.where(down_day, 0)
    max_down_vol = down_vol_only.rolling(pocket_pivot_lookback).max()
    out["pocket_pivot"] = up_day & (volume > max_down_vol)

    # RS line leadership vs benchmark
    if bench_df is not None:
        rs_line = _rs_line(close, bench_df["close"])
        rs_high20 = rs_line.rolling(20).max()
        out["rs_near_high"] = rs_line >= rs_high20 * 0.98
    else:
        out["rs_near_high"] = True  # skip filter if no benchmark provided

    # Trend structure
    ma50 = close.rolling(50).mean()
    ma150 = close.rolling(150).mean()
    out["uptrend"] = (close > ma50) & (ma50 > ma50.shift(5)) & (ma50 > ma150)

    avg_vol50 = volume.rolling(50).mean()
    out["avg_vol50"] = avg_vol50

    out["scan"] = (
        (out["udvr"] > udvr_threshold)
        & (out["dist_day_count"] <= max_dist_days)
        & (out["pct_below_high"] <= pct_from_high_max)
        & out["rs_near_high"]
        & out["uptrend"]
        & (avg_vol50 > min_avg_vol)
    )

    return out


# ---------------------------------------------------------------------------
# Scanner 2: Smart Money Distribution (pre-breakdown, short)
# ---------------------------------------------------------------------------

def scan_distribution(
    df: pd.DataFrame,
    bench_df: pd.DataFrame = None,
    udvr_lookback: int = 20,
    udvr_threshold: float = 0.77,
    dist_lookback: int = 25,
    min_dist_days: int = 4,
    dist_pct_threshold: float = -1.5,
    dist_vol_multiplier: float = 1.5,
    stall_lookback: int = 10,
    min_avg_vol: float = 300_000,
) -> pd.DataFrame:
    """
    Returns df with added columns and a boolean 'scan' column.
    Requires columns: high, low, close, volume.
    bench_df (optional): same shape, used for RS-line rollover divergence check.
    """
    out = df.copy()
    close, volume, high, low = out["close"], out["volume"], out["high"], out["low"]

    out["udvr"] = _up_down_volume_ratio(close, volume, udvr_lookback)

    out["dist_day_count"] = _distribution_day_count(
        close, volume, dist_lookback, dist_pct_threshold, dist_vol_multiplier
    )

    # Churn/stall day: wide range, weak close, heavy volume
    day_range = high - low
    avg_range20 = day_range.rolling(20).mean()
    avg_vol50 = volume.rolling(50).mean()
    close_position = (close - low) / (day_range + 1e-4)
    is_churn_day = (
        (day_range > avg_range20 * 1.2)
        & (close_position < 0.35)
        & (volume > avg_vol50 * dist_vol_multiplier)
    )
    out["is_churn_day"] = is_churn_day
    out["churn_count"] = is_churn_day.astype(int).rolling(stall_lookback).sum()

    # RS line rolling over while price still near highs (leading divergence)
    if bench_df is not None:
        rs_line = _rs_line(close, bench_df["close"])
        rs_high20 = rs_line.rolling(20).max()
        price_high20 = close.rolling(20).max()
        out["rs_rolling_over"] = (rs_line < rs_high20 * 0.97) & (close >= price_high20 * 0.97)
    else:
        out["rs_rolling_over"] = False  # can't assess without benchmark

    # Trend weakening
    ma50 = close.rolling(50).mean()
    out["trend_weakening"] = (close < ma50) | (ma50 < ma50.shift(5))

    out["avg_vol50"] = avg_vol50

    out["scan"] = (
        (out["udvr"] < udvr_threshold)
        & (out["dist_day_count"] >= min_dist_days)
        & (out["rs_rolling_over"] | (out["churn_count"] >= 2))
        & out["trend_weakening"]
        & (avg_vol50 > min_avg_vol)
    )

    return out


# ---------------------------------------------------------------------------
# Batch runner for a watchlist -- mirrors how scanner_builder.py likely
# iterates over your 104-ticker watchlist against price_cache
# ---------------------------------------------------------------------------

def run_watchlist_scan(
    price_data: dict,          # {symbol: DataFrame} pulled from price_cache
    benchmark_symbol: str = "SPY",
    mode: str = "accumulation",  # or "distribution"
    latest_only: bool = True,
    **kwargs,
) -> pd.DataFrame:
    """
    Runs the chosen scanner across every symbol in price_data.
    Returns a summary DataFrame: symbol, date, scan (bool), plus key factor columns.
    """
    scan_fn = scan_accumulation if mode == "accumulation" else scan_distribution
    bench_df = price_data.get(benchmark_symbol)

    results = []
    for symbol, df in price_data.items():
        if symbol == benchmark_symbol or df is None or len(df) < 60:
            continue
        try:
            scanned = scan_fn(df, bench_df=bench_df, **kwargs)
        except Exception as e:
            # keep the batch running even if one symbol has bad/missing data
            print(f"[scan_watchlist] skipped {symbol}: {e}")
            continue

        row = scanned.iloc[[-1]].copy() if latest_only else scanned.copy()
        row.insert(0, "symbol", symbol)
        results.append(row)

    if not results:
        return pd.DataFrame()

    summary = pd.concat(results, ignore_index=latest_only)
    if latest_only:
        summary = summary[summary["scan"]].sort_values("udvr", ascending=(mode == "distribution"))
    return summary


if __name__ == "__main__":
    # Minimal smoke test with synthetic data -- replace with a real
    # price_cache pull when wiring into oiapp.
    rng = pd.date_range("2025-01-01", periods=300, freq="B")
    np.random.seed(42)
    price = 100 + np.cumsum(np.random.randn(300)) 
    vol = np.random.randint(200_000, 800_000, size=300)
    df = pd.DataFrame({
        "open": price, "high": price + np.random.rand(300),
        "low": price - np.random.rand(300), "close": price,
        "volume": vol,
    }, index=rng)
    bench = df.copy()  # dummy benchmark for smoke test

    accum = scan_accumulation(df, bench_df=bench)
    dist = scan_distribution(df, bench_df=bench)
    print("Accumulation hits:", accum["scan"].sum())
    print("Distribution hits:", dist["scan"].sum())
