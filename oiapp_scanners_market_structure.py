"""Market structure scanner / page.

Watchlist-driven multi-timeframe analysis with:
- Market regime on monthly / weekly / daily
- Volume profile: POC, VAH, VAL, HVNs, LVNs
- Support / resistance with confluence scoring
- Mean reversion assessment
- Reversal probability and best trade location
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from ..services.yf_session import safe_history

ms_bp = Blueprint("market_structure_bp", __name__, url_prefix="/scanner/market-structure")
DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")

# ---------------------------------------------------------------------------
# DB / watchlists
# ---------------------------------------------------------------------------

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _ensure_watchlists_exist() -> None:
    con = _conn()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                fetch_options_oi INTEGER DEFAULT 0,
                is_default INTEGER DEFAULT 0,
                color TEXT DEFAULT '#818cf8',
                created_at TEXT DEFAULT (datetime('now')),
                last_fetch_at TEXT,
                last_fetch_mode TEXT,
                last_fetch_count INTEGER DEFAULT 0
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist_symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                added_at TEXT DEFAULT (datetime('now')),
                UNIQUE(watchlist_id, symbol)
            )
            """
        )
        con.commit()
    finally:
        con.close()


@lru_cache(maxsize=1)
def _watchlists_cache() -> List[Dict[str, Any]]:
    _ensure_watchlists_exist()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT id, name, COALESCE(symbol_count, 0) AS symbol_count FROM (SELECT w.id, w.name, COUNT(ws.id) symbol_count FROM watchlists w LEFT JOIN watchlist_symbols ws ON ws.watchlist_id = w.id GROUP BY w.id) ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _watchlist_symbols(watchlist_id: Optional[int]) -> List[str]:
    if not watchlist_id:
        return []
    _ensure_watchlists_exist()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (int(watchlist_id),),
        ).fetchall()
        return [str(r[0]).upper() for r in rows if r and r[0]]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_g / avg_l.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def _macd_hist(series: pd.Series) -> pd.Series:
    ema12 = _ema(series, 12)
    ema26 = _ema(series, 26)
    macd = ema12 - ema26
    sig = _ema(macd, 9)
    return macd - sig


def _bbands(series: pd.Series, period: int = 20, n_std: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    mid = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)
    upper = mid + n_std * std
    lower = mid - n_std * std
    return lower.fillna(method="bfill"), mid.fillna(method="bfill"), upper.fillna(method="bfill")


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return _true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = (
        df[["Open", "High", "Low", "Close", "Volume"]]
        .resample(rule)
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        .dropna()
    )
    return out


# ---------------------------------------------------------------------------
# Data prep
# ---------------------------------------------------------------------------

def _safe_hist(symbol: str, period: str = "2y") -> pd.DataFrame:
    try:
        df = safe_history(symbol, period=period, interval="1d", retries=2)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        df = df[[c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]].dropna()
        return df
    except Exception:
        return pd.DataFrame()


def _bench_history(benchmark: str) -> pd.DataFrame:
    return _safe_hist(benchmark, period="3y")


# ---------------------------------------------------------------------------
# Regime analysis
# ---------------------------------------------------------------------------

def _label_from_scores(bull: int, bear: int) -> str:
    diff = bull - bear
    if diff >= 5:
        return "Strong Uptrend"
    if diff >= 2:
        return "Uptrend"
    if diff <= -5:
        return "Strong Downtrend"
    if diff <= -2:
        return "Downtrend"
    return "Range"


def _regime_for_frame(df: pd.DataFrame, bench: pd.DataFrame, timeframe: str) -> Dict[str, Any]:
    if df is None or df.empty or len(df) < 30:
        return {"timeframe": timeframe, "label": "Insufficient Data", "confidence": 0, "reasons": ["not enough bars"]}

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    price = float(close.iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else price
    ma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else ma20
    ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else ma50
    rs = None
    rs_ma20 = None
    rs_slope = None
    if bench is not None and not bench.empty:
        b = bench.reindex(df.index, method="ffill")[["Close"]].dropna()
        aligned = df[["Close"]].join(b, how="inner", rsuffix="_bench")
        if len(aligned) >= 10:
            rel = aligned["Close"] / aligned["Close_bench"].replace(0, np.nan)
            rs = float(rel.iloc[-1])
            rs_ma20 = float(rel.rolling(20).mean().iloc[-1]) if len(rel) >= 20 else rs
            rs_slope = float((rel.iloc[-1] / rel.iloc[-min(6, len(rel))] - 1) * 100)
    hh = float(high.iloc[-1]) >= float(high.iloc[-20:].max()) * 0.995 if len(high) >= 20 else False
    ll = float(low.iloc[-1]) <= float(low.iloc[-20:].min()) * 1.005 if len(low) >= 20 else False
    high_range = float(high.iloc[-20:].max()) if len(high) >= 20 else float(high.max())
    low_range = float(low.iloc[-20:].min()) if len(low) >= 20 else float(low.min())
    range_pos = (price - low_range) / max(1e-9, (high_range - low_range)) if high_range > low_range else 0.5

    bull = 0
    bear = 0
    reasons: List[str] = []

    if price > ma20: bull += 1
    else: bear += 1
    if price > ma50: bull += 1
    else: bear += 1
    if price > ma200: bull += 1
    else: bear += 1
    if ma20 > ma50 > ma200: bull += 2
    if ma20 < ma50 < ma200: bear += 2
    if hh: bull += 1; reasons.append("higher high")
    if ll: bear += 1; reasons.append("lower low")
    if range_pos > 0.8: bull += 1
    if range_pos < 0.2: bear += 1
    if rs is not None and rs_ma20 is not None:
        if rs > rs_ma20: bull += 1
        else: bear += 1
    if rs_slope is not None:
        if rs_slope > 0.5: bull += 1
        elif rs_slope < -0.5: bear += 1

    label = _label_from_scores(bull, bear)
    if label == "Strong Uptrend":
        label = "Strong Uptrend"
    elif label == "Uptrend":
        label = "Uptrend"
    elif label == "Strong Downtrend":
        label = "Strong Downtrend"
    elif label == "Downtrend":
        label = "Downtrend"

    if label in ("Uptrend", "Strong Uptrend"):
        summary = f"{timeframe}: price above key MAs; bullish structure intact"
    elif label in ("Downtrend", "Strong Downtrend"):
        summary = f"{timeframe}: price below key MAs; bearish structure intact"
    else:
        summary = f"{timeframe}: mixed moving-average alignment; range conditions dominate"

    confidence = min(100, 40 + abs(bull - bear) * 10 + (10 if rs_slope and abs(rs_slope) > 2 else 0))
    return {
        "timeframe": timeframe,
        "label": label,
        "bull_score": bull,
        "bear_score": bear,
        "confidence": int(confidence),
        "reasons": reasons[:3] + [summary],
        "price": round(price, 2),
        "ma20": round(ma20, 2),
        "ma50": round(ma50, 2),
        "ma200": round(ma200, 2),
        "relative_strength": round(rs, 4) if rs is not None else None,
        "relative_strength_slope": round(rs_slope, 2) if rs_slope is not None else None,
    }


# ---------------------------------------------------------------------------
# Volume profile
# ---------------------------------------------------------------------------

def _volume_profile(df: pd.DataFrame, bins: int = 72) -> Dict[str, Any]:
    if df is None or df.empty:
        return {"poc": None, "vah": None, "val": None, "hvn": [], "lvn": [], "profile_confidence": 0}

    data = df[["High", "Low", "Close", "Volume"]].dropna().copy()
    if data.empty:
        return {"poc": None, "vah": None, "val": None, "hvn": [], "lvn": [], "profile_confidence": 0}

    pmin = float(data["Low"].min())
    pmax = float(data["High"].max())
    if pmax <= pmin:
        price = float(data["Close"].iloc[-1])
        return {"poc": price, "vah": price, "val": price, "hvn": [], "lvn": [], "profile_confidence": 0}

    edges = np.linspace(pmin, pmax, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    profile = np.zeros(bins, dtype=float)

    for _, row in data.iterrows():
        low = float(row["Low"])
        high = float(row["High"])
        vol = float(row["Volume"] or 0)
        if vol <= 0:
            continue
        mask = (edges[:-1] <= high) & (edges[1:] >= low)
        idxs = np.where(mask)[0]
        if len(idxs) == 0:
            idx = int(np.clip(np.searchsorted(centers, float(row["Close"])), 0, bins - 1))
            profile[idx] += vol
        else:
            profile[idxs] += vol / len(idxs)

    poc_idx = int(np.argmax(profile))
    poc = float(centers[poc_idx])
    total = float(profile.sum())
    if total <= 0:
        return {"poc": round(poc, 2), "vah": round(poc, 2), "val": round(poc, 2), "hvn": [], "lvn": [], "profile_confidence": 0}

    # Value area expansion around POC to 70% of volume
    included = {poc_idx}
    cum = float(profile[poc_idx])
    left = poc_idx - 1
    right = poc_idx + 1
    while cum / total < 0.70 and (left >= 0 or right < bins):
        left_v = profile[left] if left >= 0 else -1
        right_v = profile[right] if right < bins else -1
        if right_v >= left_v:
            if right < bins:
                included.add(right)
                cum += float(profile[right])
                right += 1
            elif left >= 0:
                included.add(left)
                cum += float(profile[left])
                left -= 1
        else:
            if left >= 0:
                included.add(left)
                cum += float(profile[left])
                left -= 1
            elif right < bins:
                included.add(right)
                cum += float(profile[right])
                right += 1

    vah = float(edges[max(included) + 1])
    val = float(edges[min(included)])

    pct75 = float(np.percentile(profile, 75))
    pct50 = float(np.percentile(profile, 50))
    pct25 = float(np.percentile(profile, 25))

    peaks = []
    troughs = []
    for i in range(bins):
        left_v = profile[i - 1] if i > 0 else -1
        right_v = profile[i + 1] if i < bins - 1 else -1
        v = profile[i]
        if v >= left_v and v >= right_v and v >= pct75:
            peaks.append((i, v))
        if v <= left_v and v <= right_v and v <= pct25:
            troughs.append((i, v))

    # Rank HVNs into major / intermediate / minor
    major_cut = float(np.percentile(profile[profile > 0], 90)) if np.any(profile > 0) else pct75
    inter_cut = float(np.percentile(profile[profile > 0], 75)) if np.any(profile > 0) else pct50

    hvn = []
    for idx, vol in sorted(peaks, key=lambda x: -x[1]):
        if vol >= major_cut:
            rank = "Major"
        elif vol >= inter_cut:
            rank = "Intermediate"
        else:
            rank = "Minor"
        hvn.append({
            "level": round(float(centers[idx]), 2),
            "volume": round(float(vol), 0),
            "rank": rank,
            "strength": round(100 * vol / max(profile.max(), 1), 1),
        })

    # Keep a concise set
    hvn = hvn[:8]

    # LVNs as the deepest valleys; keep around 5
    lvn = []
    for idx, vol in sorted(troughs, key=lambda x: x[1]):
        lvn.append({
            "level": round(float(centers[idx]), 2),
            "volume": round(float(vol), 0),
            "strength": round(100 * (1 - vol / max(profile.max(), 1)), 1),
        })
    lvn = lvn[:5]

    confidence = min(100, 35 + len(peaks) * 5 + len(troughs) * 2)
    return {
        "poc": round(poc, 2),
        "vah": round(vah, 2),
        "val": round(val, 2),
        "hvn": hvn,
        "lvn": lvn,
        "profile_confidence": int(confidence),
        "low": round(pmin, 2),
        "high": round(pmax, 2),
    }


# ---------------------------------------------------------------------------
# Support / resistance helpers
# ---------------------------------------------------------------------------

def _swing_levels(df: pd.DataFrame, lookback: int = 120, window: int = 3) -> Tuple[List[float], List[float]]:
    if df is None or df.empty:
        return [], []
    d = df.tail(lookback).copy()
    highs = d["High"].astype(float).tolist()
    lows = d["Low"].astype(float).tolist()
    swing_highs = []
    swing_lows = []
    for i in range(window, len(d) - window):
        hi = highs[i]
        lo = lows[i]
        if hi == max(highs[i - window:i + window + 1]):
            swing_highs.append(hi)
        if lo == min(lows[i - window:i + window + 1]):
            swing_lows.append(lo)
    return swing_highs, swing_lows


def _dedupe_levels(levels: Iterable[float], pct_tol: float = 0.7) -> List[float]:
    out: List[float] = []
    for lv in sorted(levels):
        if not out:
            out.append(float(lv))
            continue
        if abs(lv - out[-1]) / max(1e-9, out[-1]) * 100 > pct_tol:
            out.append(float(lv))
        else:
            out[-1] = (out[-1] + float(lv)) / 2
    return out


def _level_reasons(level: float, price: float, items: List[Tuple[str, float, float]]) -> Tuple[int, List[str]]:
    score = 0
    reasons = []
    for label, value, weight in items:
        if value is None:
            continue
        pct = abs(level - value) / max(1e-9, price) * 100
        if pct <= 0.75:
            score += int(weight)
            reasons.append(label)
        elif pct <= 1.5:
            score += int(weight * 0.6)
            reasons.append(f"near {label}")
    return score, reasons


def _zones_from_candidates(price: float, candidates: List[Dict[str, Any]], side: str, top_n: int = 3) -> List[Dict[str, Any]]:
    if side == "support":
        filtered = [c for c in candidates if c["level"] <= price]
        filtered.sort(key=lambda x: (price - x["level"], -x["score"]))
    else:
        filtered = [c for c in candidates if c["level"] >= price]
        filtered.sort(key=lambda x: (x["level"] - price, -x["score"]))
    return filtered[:top_n]


# ---------------------------------------------------------------------------
# Trade idea helpers
# ---------------------------------------------------------------------------

def _score_setup(trend_label: str, mean_rev_score: float, bull_rev: float, bear_rev: float, rr: float, confluence: float) -> Tuple[str, int]:
    quality = 0
    if rr >= 2.5: quality += 2
    elif rr >= 1.8: quality += 1
    if confluence >= 70: quality += 2
    elif confluence >= 50: quality += 1
    if trend_label in ("Strong Uptrend", "Uptrend") and bull_rev >= bear_rev:
        quality += 1
    if trend_label in ("Strong Downtrend", "Downtrend") and bear_rev >= bull_rev:
        quality += 1
    if mean_rev_score >= 65 and bull_rev > bear_rev:
        quality += 1
    if mean_rev_score >= 65 and bear_rev > bull_rev:
        quality += 1

    if quality >= 5:
        return "A+ Setup", quality
    if quality >= 4:
        return "A Setup", quality
    if quality >= 2:
        return "B Setup", quality
    return "No Trade", quality


# ---------------------------------------------------------------------------
# Per-symbol analysis
# ---------------------------------------------------------------------------

def _analyze_symbol(sym: str, benchmark: str = "SPY") -> Dict[str, Any]:
    sym = str(sym or "").strip().upper()
    if not sym:
        return {"symbol": sym, "error": "empty symbol"}

    df = _safe_hist(sym, period="3y")
    if df.empty or len(df) < 80:
        return {"symbol": sym, "error": "insufficient history"}

    bench = _bench_history(benchmark)
    if bench.empty:
        bench = df.copy()
        bench["Close"] = 1.0

    # Monthly / weekly / daily data
    daily = df.copy()
    weekly = _resample_ohlcv(df, "W-FRI")
    monthly = _resample_ohlcv(df, "M")

    regime_m = _regime_for_frame(monthly, _resample_ohlcv(bench, "M") if not bench.empty else bench, "Monthly")
    regime_w = _regime_for_frame(weekly, _resample_ohlcv(bench, "W-FRI") if not bench.empty else bench, "Weekly")
    regime_d = _regime_for_frame(daily, bench, "Daily")

    # Monthly RSI diff (user's rsidiff90("1m") analogue)
    monthly_rsi = _rsi(monthly["Close"], 14) if len(monthly) >= 20 else pd.Series([50.0] * len(monthly), index=monthly.index)
    monthly_rsi_ema90 = _ema(monthly_rsi, 90)
    monthly_rsidiff90 = float(monthly_rsi.iloc[-1] - monthly_rsi_ema90.iloc[-1]) if len(monthly_rsi) else 0.0
    monthly_ob_os = "Overbought" if monthly_rsidiff90 > 20 else "Oversold" if monthly_rsidiff90 < -20 else "Neutral"

    # Volume profile on daily data (main profile)
    vp = _volume_profile(daily.tail(252), bins=72)
    spot = float(daily["Close"].iloc[-1])

    # Support / resistance candidate levels from multiple sources
    swing_highs, swing_lows = _swing_levels(daily, lookback=160, window=3)
    ma20 = float(daily["Close"].rolling(20).mean().iloc[-1])
    ma50 = float(daily["Close"].rolling(50).mean().iloc[-1])
    ma200 = float(daily["Close"].rolling(200).mean().iloc[-1]) if len(daily) >= 200 else float(daily["Close"].rolling(min(100, len(daily))).mean().iloc[-1])
    lower_bb, mid_bb, upper_bb = _bbands(daily["Close"], 20, 2)
    atr14 = _atr(daily, 14).iloc[-1]

    all_candidates: List[Dict[str, Any]] = []

    # Profile levels weighted by rank
    hvn_weight = {"Major": 28, "Intermediate": 18, "Minor": 10}
    for h in vp["hvn"]:
        all_candidates.append({
            "level": float(h["level"]),
            "score": hvn_weight.get(h["rank"], 8),
            "kind": f"{h['rank']} HVN",
            "source": "volume profile",
            "strength": h["strength"],
        })

    # LVNs as breakout/rejection areas (scored lower for support/resistance, higher for trade location)
    for l in vp["lvn"]:
        all_candidates.append({
            "level": float(l["level"]),
            "score": 8,
            "kind": "LVN",
            "source": "volume profile",
            "strength": l["strength"],
        })

    # Swing levels
    for hi in _dedupe_levels(swing_highs[-20:]):
        all_candidates.append({"level": float(hi), "score": 14, "kind": "Swing High", "source": "price structure", "strength": 50})
    for lo in _dedupe_levels(swing_lows[-20:]):
        all_candidates.append({"level": float(lo), "score": 14, "kind": "Swing Low", "source": "price structure", "strength": 50})

    # MAs and Bollinger bands
    for level, kind in [
        (ma20, "MA20"),
        (ma50, "MA50"),
        (ma200, "MA200"),
        (float(lower_bb.iloc[-1]), "BB Lower"),
        (float(mid_bb.iloc[-1]), "BB Mid"),
        (float(upper_bb.iloc[-1]), "BB Upper"),
        (vp["poc"], "POC"),
        (vp["vah"], "VAH"),
        (vp["val"], "VAL"),
    ]:
        if level is not None and not math.isnan(level):
            score = 12 if kind.startswith("MA") else 18 if kind in {"POC", "VAH", "VAL"} else 10
            all_candidates.append({"level": float(level), "score": score, "kind": kind, "source": "confluence", "strength": 55})

    # Multi-source confluence scoring
    supported_candidates = []
    for c in all_candidates:
        lvl = c["level"]
        reasons = []
        score = int(c["score"])
        if vp["poc"] and abs(lvl - vp["poc"]) / max(spot, 1e-9) * 100 <= 1.0:
            score += 18; reasons.append("near POC")
        if vp["vah"] and abs(lvl - vp["vah"]) / max(spot, 1e-9) * 100 <= 1.0:
            score += 14; reasons.append("near VAH")
        if vp["val"] and abs(lvl - vp["val"]) / max(spot, 1e-9) * 100 <= 1.0:
            score += 14; reasons.append("near VAL")
        if abs(lvl - ma20) / max(spot, 1e-9) * 100 <= 0.8:
            score += 10; reasons.append("MA20")
        if abs(lvl - ma50) / max(spot, 1e-9) * 100 <= 1.0:
            score += 10; reasons.append("MA50")
        if abs(lvl - ma200) / max(spot, 1e-9) * 100 <= 1.5:
            score += 8; reasons.append("MA200")
        if abs(lvl - float(lower_bb.iloc[-1])) / max(spot, 1e-9) * 100 <= 0.8:
            score += 8; reasons.append("BB lower")
        if abs(lvl - float(upper_bb.iloc[-1])) / max(spot, 1e-9) * 100 <= 0.8:
            score += 8; reasons.append("BB upper")
        if lvl < spot:
            score += 4
        elif lvl > spot:
            score += 2
        supported_candidates.append({
            **c,
            "score": int(min(100, score)),
            "reasons": reasons,
        })

    supports = _zones_from_candidates(spot, supported_candidates, "support", 3)
    resistances = _zones_from_candidates(spot, supported_candidates, "resistance", 3)

    def _zone_payload(z: Dict[str, Any], side: str) -> Dict[str, Any]:
        level = float(z["level"])
        conf = min(100, int(z["score"]))
        dist_pct = round(abs(spot - level) / max(1e-9, spot) * 100, 2)
        return {
            "level": round(level, 2),
            "score": int(z["score"]),
            "confidence": conf,
            "type": z["kind"],
            "source": z["source"],
            "distance_pct": dist_pct,
            "reasons": z.get("reasons", []),
        }

    support_zones = [_zone_payload(z, "support") for z in supports]
    resistance_zones = [_zone_payload(z, "resistance") for z in resistances]

    # Mean reversion assessment
    close = daily["Close"].astype(float)
    rsi14 = _rsi(close, 14)
    macd_hist = _macd_hist(close)
    ma20_d = close.rolling(20).mean()
    ma50_d = close.rolling(50).mean()
    ma200_d = close.rolling(200).mean()
    bb_low, bb_mid, bb_up = _bbands(close, 20, 2)
    bb_pos = 50.0
    if float(bb_up.iloc[-1]) > float(bb_low.iloc[-1]):
        bb_pos = (spot - float(bb_low.iloc[-1])) / max(1e-9, float(bb_up.iloc[-1]) - float(bb_low.iloc[-1])) * 100
    dist20 = (spot - float(ma20_d.iloc[-1])) / max(1e-9, float(ma20_d.iloc[-1])) * 100
    dist50 = (spot - float(ma50_d.iloc[-1])) / max(1e-9, float(ma50_d.iloc[-1])) * 100
    dist200 = (spot - float(ma200_d.iloc[-1])) / max(1e-9, float(ma200_d.iloc[-1])) * 100
    rsi_now = float(rsi14.iloc[-1])
    macd_now = float(macd_hist.iloc[-1])
    macd_prev = float(macd_hist.iloc[-3]) if len(macd_hist) >= 3 else macd_now
    macd_improving = macd_now > macd_prev

    near_major_hvn = False
    major_hvn_level = None
    if vp["hvn"]:
        majors = [h for h in vp["hvn"] if h["rank"] == "Major"]
        if majors:
            majors.sort(key=lambda x: abs(float(x["level"]) - spot))
            major_hvn_level = float(majors[0]["level"])
            near_major_hvn = abs(major_hvn_level - spot) / max(1e-9, spot) * 100 <= 2.0

    mr_score = 50
    mr_reasons = []
    if rsi_now < 30:
        mr_score += 20; mr_reasons.append(f"RSI {rsi_now:.1f} oversold")
    elif rsi_now > 70:
        mr_score += 20; mr_reasons.append(f"RSI {rsi_now:.1f} overbought")
    elif rsi_now < 40 or rsi_now > 60:
        mr_score += 8
    if bb_pos < 20:
        mr_score += 16; mr_reasons.append(f"BB position {bb_pos:.0f}% low")
    elif bb_pos > 80:
        mr_score += 16; mr_reasons.append(f"BB position {bb_pos:.0f}% high")
    if abs(dist20) > 8:
        mr_score += 10; mr_reasons.append(f"{dist20:+.1f}% vs MA20")
    if abs(dist50) > 10:
        mr_score += 8; mr_reasons.append(f"{dist50:+.1f}% vs MA50")
    if abs(dist200) > 15:
        mr_score += 6; mr_reasons.append(f"{dist200:+.1f}% vs MA200")
    if near_major_hvn:
        mr_score += 12; mr_reasons.append(f"near major HVN {major_hvn_level:.2f}")
    if macd_improving:
        mr_score += 6; mr_reasons.append("MACD histogram improving")
    mr_score = int(max(0, min(100, mr_score)))

    if rsi_now <= 45 or bb_pos <= 40 or dist20 < 0:
        mr_direction = "Bullish fade / bounce"
    elif rsi_now >= 55 or bb_pos >= 60 or dist20 > 0:
        mr_direction = "Bearish fade / pullback"
    else:
        mr_direction = "Neutral"

    # Reversal assessment
    recent_close = close.tail(5)
    recent_rsi = rsi14.tail(5)
    recent_macd = macd_hist.tail(5)
    price_reclaim_20 = spot > float(ma20_d.iloc[-1]) and recent_close.iloc[-1] > recent_close.iloc[-2]
    price_loss_20 = spot < float(ma20_d.iloc[-1]) and recent_close.iloc[-1] < recent_close.iloc[-2]

    bull_prob = 10
    bear_prob = 10
    bull_triggers = []
    bear_triggers = []

    if near_major_hvn or (support_zones and support_zones[0]["distance_pct"] <= 2.0):
        bull_prob += 20; bull_triggers.append("support / HVN nearby")
    if rsi_now <= 40:
        bull_prob += 20; bull_triggers.append(f"RSI {rsi_now:.1f} low")
    if recent_rsi.iloc[-1] > recent_rsi.iloc[-3] if len(recent_rsi) >= 3 else False:
        bull_prob += 12; bull_triggers.append("RSI improving")
    if macd_improving:
        bull_prob += 12; bull_triggers.append("MACD improving")
    if price_reclaim_20:
        bull_prob += 16; bull_triggers.append("reclaims MA20")

    if resistances and resistances[0]["distance_pct"] <= 2.0:
        bear_prob += 20; bear_triggers.append("resistance / HVN overhead")
    if rsi_now >= 60:
        bear_prob += 18; bear_triggers.append(f"RSI {rsi_now:.1f} elevated")
    if len(recent_rsi) >= 3 and recent_rsi.iloc[-1] < recent_rsi.iloc[-3]:
        bear_prob += 12; bear_triggers.append("RSI rolling over")
    if len(recent_macd) >= 3 and recent_macd.iloc[-1] < recent_macd.iloc[-3]:
        bear_prob += 12; bear_triggers.append("MACD weakening")
    if price_loss_20:
        bear_prob += 16; bear_triggers.append("loses MA20")

    bull_prob = int(min(100, bull_prob))
    bear_prob = int(min(100, bear_prob))

    # Trade setup / R:R
    trend_tf = regime_m["label"] if regime_m["label"] not in {"Insufficient Data"} else regime_w["label"]
    bullish_bias = bull_prob >= bear_prob
    if bullish_bias:
        entry_zone = support_zones[0]["level"] if support_zones else round(vp["val"] or spot * 0.98, 2)
        stop = support_zones[1]["level"] if len(support_zones) > 1 else round(entry_zone - max(atr14, spot * 0.02), 2)
        target1 = resistances[0]["level"] if resistances else round(spot + max(atr14, spot * 0.03), 2)
        target2 = resistances[1]["level"] if len(resistances) > 1 else round(target1 + max(atr14, spot * 0.03), 2)
    else:
        entry_zone = resistances[0]["level"] if resistances else round(vp["vah"] or spot * 1.02, 2)
        stop = resistances[1]["level"] if len(resistances) > 1 else round(entry_zone + max(atr14, spot * 0.02), 2)
        target1 = support_zones[0]["level"] if support_zones else round(spot - max(atr14, spot * 0.03), 2)
        target2 = support_zones[1]["level"] if len(support_zones) > 1 else round(target1 - max(atr14, spot * 0.03), 2)

    risk = abs(entry_zone - stop)
    reward = abs(target2 - entry_zone)
    rr = round(reward / max(1e-9, risk), 2) if risk else 0.0

    best_setup, quality_score = _score_setup(trend_tf, mr_score, bull_prob, bear_prob, rr, max([z["score"] for z in supported_candidates], default=0))

    summary_thesis = (
        f"{sym} is in a {trend_tf.lower()} context with {monthly_ob_os.lower()} monthly momentum. "
        f"Price is centered around {'value' if vp['val'] and vp['vah'] and vp['val'] <= spot <= vp['vah'] else 'an edge of value'}. "
        f"Best opportunity skews toward {'mean reversion' if mr_score >= 60 else 'continuation' if trend_tf in ('Uptrend', 'Strong Uptrend') else 'patience'}.")

    # confidence scores
    regime_conf = int(round((regime_m["confidence"] * 0.45) + (regime_w["confidence"] * 0.35) + (regime_d["confidence"] * 0.20)))
    support_conf = int(min(100, max([z["confidence"] for z in support_zones], default=20)))
    resistance_conf = int(min(100, max([z["confidence"] for z in resistance_zones], default=20)))
    reversal_conf = int(round((bull_prob + bear_prob) / 2))
    trade_conf = int(min(100, max(20, quality_score * 18)))
    overall_conf = int(round((regime_conf + vp["profile_confidence"] + mr_score + reversal_conf + trade_conf) / 5))

    return {
        "symbol": sym,
        "spot": round(spot, 2),
        "benchmark": benchmark,
        "monthly_rsidiff90": round(monthly_rsidiff90, 2),
        "monthly_momentum_state": monthly_ob_os,
        "regime": {
            "monthly": regime_m,
            "weekly": regime_w,
            "daily": regime_d,
            "overall": trend_tf,
            "confidence": regime_conf,
        },
        "volume_profile": {
            **vp,
            "confidence": vp["profile_confidence"],
            "major_hvn_level": round(major_hvn_level, 2) if major_hvn_level is not None else None,
            "value_area_state": "inside value" if vp["val"] and vp["vah"] and vp["val"] <= spot <= vp["vah"] else "outside value",
        },
        "support_zones": support_zones,
        "resistance_zones": resistance_zones,
        "mean_reversion": {
            "score": mr_score,
            "direction": mr_direction,
            "reasons": mr_reasons[:6],
            "confidence": int(min(100, max(35, mr_score))),
            "distance_from_ma20_pct": round(dist20, 2),
            "distance_from_ma50_pct": round(dist50, 2),
            "distance_from_ma200_pct": round(dist200, 2),
            "bb_position": round(bb_pos, 1),
            "rsi": round(rsi_now, 1),
            "macd_hist": round(macd_now, 3),
        },
        "reversal": {
            "bullish_probability": bull_prob,
            "bearish_probability": bear_prob,
            "bullish_trigger": "; ".join(bull_triggers) or "none",
            "bearish_trigger": "; ".join(bear_triggers) or "none",
            "confidence": reversal_conf,
        },
        "best_trade": {
            "setup": best_setup,
            "entry_zone": round(entry_zone, 2),
            "stop": round(stop, 2),
            "target1": round(target1, 2),
            "target2": round(target2, 2),
            "risk_reward": rr,
            "bias": "Bullish" if bullish_bias else "Bearish",
            "confidence": trade_conf,
        },
        "summary": summary_thesis,
        "confidence": {
            "regime": regime_conf,
            "volume_profile": vp["profile_confidence"],
            "support": support_conf,
            "resistance": resistance_conf,
            "mean_reversion": int(min(100, max(35, mr_score))),
            "reversal": reversal_conf,
            "trade_setup": trade_conf,
            "overall": overall_conf,
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@ms_bp.route("/")
def page():
    return redirect(url_for("scanner.home", tab="market-structure"))


@ms_bp.route("/watchlists")
def api_watchlists():
    _ensure_watchlists_exist()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT w.id, w.name, COUNT(ws.id) AS symbol_count FROM watchlists w LEFT JOIN watchlist_symbols ws ON ws.watchlist_id = w.id GROUP BY w.id ORDER BY w.name"
        ).fetchall()
        return jsonify({"watchlists": [dict(r) for r in rows]})
    finally:
        con.close()


@ms_bp.route("/scan")
def scan():
    watchlist_id = request.args.get("watchlist_id", type=int)
    benchmark = (request.args.get("benchmark") or "SPY").strip().upper() or "SPY"
    monthly_filter = (request.args.get("monthly_filter") or "all").strip().lower()  # all | overbought | oversold
    threshold = request.args.get("threshold", 20, type=float)
    max_symbols = request.args.get("max_symbols", 100, type=int)
    limit = request.args.get("limit", 100, type=int)

    symbols = _watchlist_symbols(watchlist_id)
    if not symbols:
        # fallback to a modest universe for safety
        con = _conn()
        try:
            rows = con.execute("SELECT symbol FROM symbols WHERE symbol IS NOT NULL ORDER BY symbol LIMIT ?", (max_symbols,)).fetchall()
            symbols = [r[0] for r in rows]
        except Exception:
            symbols = []
        finally:
            con.close()

    symbols = [s for s in symbols if s]
    if max_symbols:
        symbols = symbols[:max_symbols]

    if not symbols:
        return jsonify({"results": [], "count": 0, "error": "No symbols found"})

    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    workers = min(8, max(2, min(len(symbols), 8)))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_analyze_symbol, sym, benchmark): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {"symbol": sym, "error": str(e)}
            if res.get("error"):
                errors.append(res)
            else:
                results.append(res)

    # Monthly overbought / oversold filter
    if monthly_filter in {"overbought", "oversold"}:
        sign = 1 if monthly_filter == "overbought" else -1
        results = [r for r in results if (r.get("monthly_rsidiff90", 0) * sign) > threshold]

    # Sort by overall confidence / setup quality and then mean reversion / reversal
    results.sort(
        key=lambda r: (
            r.get("confidence", {}).get("overall", 0),
            r.get("best_trade", {}).get("confidence", 0),
            r.get("mean_reversion", {}).get("score", 0),
            r.get("reversal", {}).get("bullish_probability", 0) + r.get("reversal", {}).get("bearish_probability", 0),
        ),
        reverse=True,
    )
    results = results[:limit]

    regime_counts = {k: 0 for k in ["Strong Uptrend", "Uptrend", "Range", "Downtrend", "Strong Downtrend"]}
    for r in results:
        label = r.get("regime", {}).get("overall", "Range")
        if label in regime_counts:
            regime_counts[label] += 1
        else:
            regime_counts["Range"] += 1

    return jsonify(
        {
            "results": results,
            "errors": errors[:25],
            "count": len(results),
            "watchlist_id": watchlist_id,
            "benchmark": benchmark,
            "filters": {"monthly_filter": monthly_filter, "threshold": threshold, "max_symbols": max_symbols, "limit": limit},
            "summary": {
                "regime_counts": regime_counts,
                "average_confidence": round(sum(r.get("confidence", {}).get("overall", 0) for r in results) / max(1, len(results)), 1),
                "monthly_overbought": sum(1 for r in results if r.get("monthly_rsidiff90", 0) > threshold),
                "monthly_oversold": sum(1 for r in results if r.get("monthly_rsidiff90", 0) < -threshold),
            },
            "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(_analyze_symbol("SPY"), indent=2))
