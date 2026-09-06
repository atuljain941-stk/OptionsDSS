from __future__ import annotations

from functools import lru_cache
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd

from ..services.sector_service import SECTOR_ETFS, get_symbol_sector
from ..services.oi_wall_service import oi_wall_context
from .edge_factors import edge_profile


def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean().replace(0, 1e-9)
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series):
    macd_line = _ema(close, 12) - _ema(close, 26)
    signal = _ema(macd_line, 9)
    hist = macd_line - signal
    return macd_line, signal, hist


@lru_cache(maxsize=32)
def _benchmark_regime(symbol: str, period: str = "6mo") -> Dict[str, Any]:
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period=period, auto_adjust=False)
        if df is None or df.empty or len(df) < 60:
            return {"bias": "NEUTRAL", "score": 0, "note": f"{symbol} history unavailable"}
        close = df["Close"].astype(float)
        ema20 = _ema(close, 20)
        ema50 = _ema(close, 50)
        macd_line, macd_signal, macd_hist = _macd(close)
        price = float(close.iloc[-1])
        bullish = price > float(ema20.iloc[-1]) > float(ema50.iloc[-1]) and float(macd_hist.iloc[-1]) > 0
        bearish = price < float(ema20.iloc[-1]) < float(ema50.iloc[-1]) and float(macd_hist.iloc[-1]) < 0
        if bullish:
            return {"bias": "BULLISH", "score": 10, "note": f"{symbol} trend bullish"}
        if bearish:
            return {"bias": "BEARISH", "score": -10, "note": f"{symbol} trend bearish"}
        return {"bias": "NEUTRAL", "score": 0, "note": f"{symbol} trend mixed"}
    except Exception as e:
        return {"bias": "NEUTRAL", "score": 0, "note": f"{symbol} regime error: {e}"}


def _sector_regime(symbol: str) -> Dict[str, Any]:
    try:
        sector = get_symbol_sector(symbol)
    except Exception:
        sector = "Other"
    etf = SECTOR_ETFS.get(sector)
    if not etf:
        return {"sector": sector, "bias": "NEUTRAL", "score": 0, "note": f"{sector} no ETF proxy"}
    bench = _benchmark_regime(etf)
    bench = dict(bench)
    bench.update({"sector": sector, "etf": etf})
    if bench["bias"] == "BULLISH":
        bench["note"] = f"{sector} / {etf} bullish"
    elif bench["bias"] == "BEARISH":
        bench["note"] = f"{sector} / {etf} bearish"
    else:
        bench["note"] = f"{sector} / {etf} mixed"
    return bench


def _sr_context(df: pd.DataFrame, idx: int, direction: str) -> Dict[str, Any]:
    try:
        if df is None or len(df) < 10 or idx < 5:
            return {"bias": "NEUTRAL", "score": 0, "note": "SR data unavailable"}
        close = df["Close"].astype(float)
        price = float(close.iloc[idx])
        start = max(0, idx - 20)
        hi20 = float(df["High"].iloc[start:idx + 1].max())
        lo20 = float(df["Low"].iloc[start:idx + 1].min())
        dist_hi = ((hi20 - price) / price) * 100 if price else 0
        dist_lo = ((price - lo20) / price) * 100 if price else 0
        if direction == "BULLISH":
            if price >= hi20 * 0.995:
                return {"bias": "BULLISH", "score": 8, "note": f"Breakout above 20d high ({hi20:.2f})"}
            if dist_hi <= 3:
                return {"bias": "MILD_BULLISH", "score": 4, "note": f"Near resistance / 20d high ({hi20:.2f})"}
            if dist_lo <= 3:
                return {"bias": "BULLISH", "score": 6, "note": f"Near support / 20d low ({lo20:.2f})"}
            return {"bias": "NEUTRAL", "score": 0, "note": f"20d range {lo20:.2f}-{hi20:.2f}"}
        if price <= lo20 * 1.005:
            return {"bias": "BEARISH", "score": 8, "note": f"Breakdown below 20d low ({lo20:.2f})"}
        if dist_lo <= 3:
            return {"bias": "MILD_BEARISH", "score": 4, "note": f"Near support / 20d low ({lo20:.2f})"}
        if dist_hi <= 3:
            return {"bias": "BEARISH", "score": 6, "note": f"Near resistance / 20d high ({hi20:.2f})"}
        return {"bias": "NEUTRAL", "score": 0, "note": f"20d range {lo20:.2f}-{hi20:.2f}"}
    except Exception as e:
        return {"bias": "NEUTRAL", "score": 0, "note": f"SR error: {e}"}


def composite_overlay(symbol: str, df: pd.DataFrame, idx: int, direction: str, price: float) -> Dict[str, Any]:
    """Return composite-mode score delta and diagnostics."""
    market_q = _benchmark_regime("QQQ")
    market_s = _benchmark_regime("SPY")
    if market_q["bias"] == market_s["bias"]:
        market_bias = market_q["bias"]
        market_note = f"QQQ/SPY {market_bias.lower()}"
        market_score = market_q["score"] + market_s["score"]
    else:
        market_bias = "NEUTRAL"
        market_note = f"QQQ {market_q['bias'].lower()} · SPY {market_s['bias'].lower()}"
        market_score = int((market_q["score"] + market_s["score"]) / 2)

    sector = _sector_regime(symbol)
    oi = oi_wall_context(symbol, price)
    oi_bias = (oi or {}).get("bias", "NEUTRAL")
    sr = _sr_context(df, idx, direction)
    edge = edge_profile(symbol, frame=df)

    score_delta = 0
    notes: List[str] = []
    flags: List[str] = []

    bullish = direction == "BULLISH"
    # Shared edge factors
    if edge.get("available"):
        if bullish and edge.get("rs_bias") == "bullish":
            score_delta += 6; notes.append("Relative strength supports upside")
        elif bullish and edge.get("rs_bias") == "bearish":
            score_delta -= 6; flags.append("Relative strength lagging")
        if not bullish and edge.get("rs_bias") == "bearish":
            score_delta += 6; notes.append("Relative strength supports downside")
        elif not bullish and edge.get("rs_bias") == "bullish":
            score_delta -= 6; flags.append("Relative strength lagging")

        if edge.get("vol_regime") == "compression":
            score_delta += 3; notes.append("Volatility compression")
        elif edge.get("vol_regime") == "expanded":
            score_delta += 1

        if edge.get("sector_score", 0) >= 60:
            score_delta += 3; notes.append(f"Sector tailwind: {edge.get('sector')}")
        if edge.get("institutional_score", 0) >= 60:
            score_delta += 4; notes.append("Institutional footprint improving")
        if edge.get("expected_move_score", 0) >= 60:
            score_delta += 2; notes.append("Room to expected move")

    # Market
    if market_bias == "BULLISH" and bullish:
        score_delta += 12; notes.append("Market regime aligned")
    elif market_bias == "BEARISH" and bullish:
        score_delta -= 10; flags.append("Market regime contrarian")
        notes.append("Market regime against trade")

    # Sector
    if sector.get("bias") == "BULLISH" and bullish:
        score_delta += 8; notes.append("Sector regime aligned")
    elif sector.get("bias") == "BEARISH" and bullish:
        score_delta -= 8; flags.append("Sector regime contrarian")
        notes.append("Sector regime against trade")

    # OI walls
    if oi_bias.startswith("BULL") and bullish:
        score_delta += 10; notes.append("OI walls support upside")
    elif oi_bias.startswith("BEAR") and bullish:
        score_delta -= 10; flags.append("OI wall resistance / bearish bias")
        notes.append("OI walls suggest resistance")

    # Support / resistance
    sr_bias = sr.get("bias", "NEUTRAL")
    if sr_bias.startswith("BULL") and bullish:
        score_delta += 8; notes.append(sr.get("note", ""))
    elif sr_bias.startswith("BEAR") and bullish:
        score_delta -= 8; flags.append("S/R looks stretched / contrarian")
        notes.append(sr.get("note", ""))

    contrarian = any("contrarian" in f.lower() or "bearish" in f.lower() for f in flags)

    return {
        "score_delta": score_delta,
        "notes": [n for n in notes if n],
        "flags": flags,
        "contrarian": contrarian,
        "market_bias": market_bias,
        "market_note": market_note,
        "market_score": market_score,
        "sector_bias": sector.get("bias", "NEUTRAL"),
        "sector_note": sector.get("note", ""),
        "sector_score": sector.get("score", 0),
        "sector_name": sector.get("sector"),
        "sector_etf": sector.get("etf"),
        "oi_bias": oi_bias,
        "oi_note": (oi or {}).get("breach_context") or (oi or {}).get("note") or "",
        "sr_bias": sr_bias,
        "sr_note": sr.get("note", ""),
        "edge_score": edge.get("edge_score", 0),
        "rs_score": edge.get("rs_score", 0),
        "vol_score": edge.get("vol_score", 0),
        "sector_score_raw": edge.get("sector_score", 0),
        "institutional_score_raw": edge.get("institutional_score", 0),
        "expected_move_score_raw": edge.get("expected_move_score", 0),
        "edge_notes": edge.get("edge_notes", []),
    }
