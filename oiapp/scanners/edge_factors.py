from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Optional

import math

import pandas as pd

from ..services.sector_service import SECTOR_ETFS, get_symbol_sector


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


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _macd(close: pd.Series):
    macd_line = _ema(close, 12) - _ema(close, 26)
    signal = _ema(macd_line, 9)
    hist = macd_line - signal
    return macd_line, signal, hist


@lru_cache(maxsize=64)
def _history(symbol: str, period: str = "1y") -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf

        df = yf.Ticker(symbol).history(period=period, auto_adjust=False)
        if df is None or df.empty:
            return None
        return df.dropna(subset=["Close", "High", "Low", "Volume"]).copy()
    except Exception:
        return None


def _to_df(frame: Any) -> Optional[pd.DataFrame]:
    if frame is None:
        return None
    if isinstance(frame, pd.DataFrame):
        return frame.copy()
    try:
        data = pd.DataFrame(frame)
        if data.empty:
            return None
        return data
    except Exception:
        return None


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _latest_oi_snapshot(symbol: str) -> Dict[str, float]:
    """Aggregate the latest and prior option-day OI snapshots if the local DB has them."""
    try:
        import sqlite3
        from pathlib import Path

        db = str(Path(__file__).resolve().parents[2] / "options_data.db")
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT date, SUM(CASE WHEN type='call' THEN oi ELSE 0 END) call_oi, "
            "SUM(CASE WHEN type='put' THEN oi ELSE 0 END) put_oi, "
            "SUM(CASE WHEN type='call' THEN volume ELSE 0 END) call_vol, "
            "SUM(CASE WHEN type='put' THEN volume ELSE 0 END) put_vol "
            "FROM options WHERE symbol=? GROUP BY date ORDER BY date DESC LIMIT 3",
            (symbol.upper(),),
        ).fetchall()
        con.close()
        if not rows:
            return {}
        latest = rows[0]
        prev = rows[1] if len(rows) > 1 else None
        last_total = _safe_float(latest["call_oi"]) + _safe_float(latest["put_oi"])
        prev_total = _safe_float(prev["call_oi"]) + _safe_float(prev["put_oi"]) if prev else 0.0
        return {
            "oi_total": last_total,
            "oi_prev_total": prev_total,
            "oi_change_pct": ((last_total - prev_total) / prev_total * 100) if prev_total else 0.0,
            "call_share": (_safe_float(latest["call_oi"]) / max(1.0, last_total)) * 100.0,
            "put_share": (_safe_float(latest["put_oi"]) / max(1.0, last_total)) * 100.0,
            "call_vol": _safe_float(latest["call_vol"]),
            "put_vol": _safe_float(latest["put_vol"]),
        }
    except Exception:
        return {}


def _score_band(value: float, lo: float, hi: float, peak: float = 100.0) -> float:
    if value <= lo:
        return 0.0
    if value >= hi:
        return peak
    if hi <= lo:
        return 0.0
    return (value - lo) / (hi - lo) * peak


def edge_profile(symbol: str, frame: Any = None, benchmark: str = "SPY") -> Dict[str, Any]:
    """Shared option-trader edge profile used across scanners.

    Outputs five reusable gap scores:
      RS, volatility regime, sector rotation, institutional footprint, expected move.
    """
    sym = symbol.upper().strip()
    df = _to_df(frame)
    if df is None:
        df = _history(sym)
    if df is None or df.empty or len(df) < 30:
        return {
            "symbol": sym,
            "available": False,
            "edge_score": 0,
            "edge_label": "No data",
            "edge_notes": ["Price history unavailable"],
        }

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    vol = df["Volume"].astype(float)
    price = _safe_float(close.iloc[-1])

    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    ema200 = _ema(close, 200) if len(close) >= 200 else _ema(close, min(50, max(10, len(close) - 1)))
    rsi14 = _rsi(close, 14)
    atr14 = _atr(df, 14)
    macd_line, macd_signal, macd_hist = _macd(close)

    # Relative strength vs benchmark and sector ETF
    rs_notes = []
    rs_score = 0.0
    bench_name = benchmark.upper() if benchmark else "SPY"
    bench_df = _history(bench_name)
    if bench_df is not None and len(bench_df) >= 30:
        bench_close = bench_df["Close"].astype(float)
        aligned = min(len(close), len(bench_close))
        if aligned >= 20:
            sym_ret20 = (close.iloc[-1] / close.iloc[-20] - 1) * 100
            bench_ret20 = (bench_close.iloc[-1] / bench_close.iloc[-20] - 1) * 100
            rs_vs_bench = sym_ret20 - bench_ret20
            rs_score += _score_band(rs_vs_bench, -10, 10, 55)
            rs_notes.append(f"RS vs {bench_name}: {rs_vs_bench:+.1f}%")
    else:
        rs_notes.append(f"{bench_name} history unavailable")

    sector_name = "Other"
    sector_etf = None
    try:
        sector_name = get_symbol_sector(sym) or "Other"
        sector_etf = SECTOR_ETFS.get(sector_name)
    except Exception:
        sector_name = "Other"
    if sector_etf:
        sec_df = _history(sector_etf)
        if sec_df is not None and len(sec_df) >= 20:
            sec_close = sec_df["Close"].astype(float)
            sym_ret20 = (close.iloc[-1] / close.iloc[-20] - 1) * 100
            sec_ret20 = (sec_close.iloc[-1] / sec_close.iloc[-20] - 1) * 100
            rs_vs_sec = sym_ret20 - sec_ret20
            rs_score += _score_band(rs_vs_sec, -8, 12, 45)
            rs_notes.append(f"RS vs {sector_etf}: {rs_vs_sec:+.1f}%")
    else:
        rs_notes.append(f"Sector: {sector_name}")

    rs_score = round(min(100.0, rs_score), 1)
    rs_bias = "bullish" if rs_score >= 55 else "bearish" if rs_score <= 30 else "neutral"

    # Volatility regime: compression is good for breakout/retrace; expansion for exhaustion.
    atr_pct = (_safe_float(atr14.iloc[-1]) / max(price, 1e-9)) * 100 if len(atr14) else 0.0
    rv20 = close.pct_change().tail(20).std(ddof=0) * (252 ** 0.5) * 100 if len(close) >= 20 else 0.0
    rv90 = close.pct_change().tail(90).std(ddof=0) * (252 ** 0.5) * 100 if len(close) >= 90 else rv20
    compression = 0.0
    if rv90:
        compression = max(0.0, min(2.0, rv90 / max(rv20, 1e-9)))
    vol_score = 0.0
    if rv20 and rv90:
        if compression >= 1.2:
            vol_score += 35
        elif compression >= 1.0:
            vol_score += 24
        elif compression >= 0.8:
            vol_score += 14
        else:
            vol_score += 6
    vol_score += _score_band(max(0.0, 8.0 - atr_pct), 0, 8, 40)
    vol_score = round(min(100.0, vol_score), 1)
    vol_regime = "compression" if compression >= 1.15 else "balanced" if atr_pct < 4.5 else "expanded"

    # Sector rotation proxy: favor symbols in strong sectors or showing a positive 20d EMA stack.
    sector_score = 30.0
    sector_notes = [f"Sector: {sector_name}"]
    if sector_etf:
        sec_df = _history(sector_etf)
        if sec_df is not None and len(sec_df) >= 50:
            sec_close = sec_df["Close"].astype(float)
            sec_ema20 = _ema(sec_close, 20).iloc[-1]
            sec_ema50 = _ema(sec_close, 50).iloc[-1]
            sec_price = sec_close.iloc[-1]
            if sec_price > sec_ema20 > sec_ema50:
                sector_score += 35
                sector_notes.append(f"{sector_etf} trend bullish")
            elif sec_price < sec_ema20 < sec_ema50:
                sector_score += 5
                sector_notes.append(f"{sector_etf} trend weak")
            else:
                sector_score += 18
                sector_notes.append(f"{sector_etf} mixed")
    sector_score = round(min(100.0, sector_score), 1)

    # Institutional footprint proxy: relative volume + acceptance + gap follow-through + OI change.
    avg20_vol = float(vol.tail(20).mean()) if len(vol) >= 20 else float(vol.mean()) if len(vol) else 0.0
    rel_vol = _safe_float(vol.iloc[-1]) / max(avg20_vol, 1e-9) if avg20_vol else 0.0
    acc = 0.0
    if len(close) >= 10:
        recent_hi = float(high.tail(10).max())
        recent_lo = float(low.tail(10).min())
        rng = max(recent_hi - recent_lo, 1e-9)
        acc = abs(price - float(ema20.iloc[-1])) / rng
    oi = _latest_oi_snapshot(sym)
    inst_score = 18.0
    inst_notes = []
    if rel_vol >= 2.0:
        inst_score += 34; inst_notes.append(f"RelVol {rel_vol:.1f}x")
    elif rel_vol >= 1.3:
        inst_score += 20; inst_notes.append(f"RelVol {rel_vol:.1f}x")
    elif rel_vol >= 0.8:
        inst_score += 10; inst_notes.append(f"RelVol {rel_vol:.1f}x")
    if price > float(ema20.iloc[-1]) and _safe_float(close.iloc[-1]) > _safe_float(close.iloc[-2]) if len(close) > 1 else False:
        inst_score += 10; inst_notes.append("Acceptance above EMA20")
    if len(close) >= 2 and abs((close.iloc[-1] / close.iloc[-2] - 1) * 100) > 2.0:
        inst_score += 8; inst_notes.append("Strong daily acceptance")
    if oi:
        if oi.get("oi_change_pct", 0) > 8:
            inst_score += 14; inst_notes.append(f"OI +{oi['oi_change_pct']:.1f}%")
        elif oi.get("oi_change_pct", 0) < -8:
            inst_score += 4; inst_notes.append(f"OI {oi['oi_change_pct']:.1f}%")
        if oi.get("call_share", 0) >= 55:
            inst_score += 8; inst_notes.append(f"Calls {oi['call_share']:.0f}%")
        elif oi.get("put_share", 0) >= 55:
            inst_score += 4; inst_notes.append(f"Puts {oi['put_share']:.0f}%")
    inst_score = round(min(100.0, inst_score), 1)

    # Expected move: how much room exists before the 20d range or ATR envelope is consumed.
    hi20 = float(high.tail(20).max()) if len(high) >= 20 else float(high.max())
    lo20 = float(low.tail(20).min()) if len(low) >= 20 else float(low.min())
    room_up = max(0.0, (hi20 - price) / max(price, 1e-9) * 100)
    room_dn = max(0.0, (price - lo20) / max(price, 1e-9) * 100)
    atr_now = _safe_float(atr14.iloc[-1]) if len(atr14) else 0.0
    expected_move_pct = (atr_now / max(price, 1e-9)) * 100 if price else 0.0
    em_score = 25.0
    if expected_move_pct >= 4.0:
        em_score += 25
    elif expected_move_pct >= 2.0:
        em_score += 18
    else:
        em_score += 8
    if room_up >= expected_move_pct or room_dn >= expected_move_pct:
        em_score += 20
    elif max(room_up, room_dn) >= expected_move_pct * 0.7:
        em_score += 10
    if float(close.iloc[-1]) > float(ema20.iloc[-1]) > float(ema50.iloc[-1]):
        em_score += 8
    elif float(close.iloc[-1]) < float(ema20.iloc[-1]) < float(ema50.iloc[-1]):
        em_score += 4
    em_score = round(min(100.0, em_score), 1)

    edge_score = round(min(100.0, rs_score * 0.28 + vol_score * 0.18 + sector_score * 0.18 + inst_score * 0.20 + em_score * 0.16), 1)
    edge_label = "Strong" if edge_score >= 70 else "Good" if edge_score >= 55 else "Mixed" if edge_score >= 40 else "Weak"

    bullish_stack = price > float(ema20.iloc[-1]) > float(ema50.iloc[-1]) > float(ema200.iloc[-1])
    bearish_stack = price < float(ema20.iloc[-1]) < float(ema50.iloc[-1])

    return {
        "symbol": sym,
        "available": True,
        "price": round(price, 2),
        "benchmark": bench_name,
        "sector": sector_name,
        "sector_etf": sector_etf,
        "rs_score": rs_score,
        "rs_bias": rs_bias,
        "vol_score": vol_score,
        "vol_regime": vol_regime,
        "sector_score": sector_score,
        "institutional_score": inst_score,
        "expected_move_score": em_score,
        "expected_move_pct": round(expected_move_pct, 2),
        "room_up_pct": round(room_up, 2),
        "room_down_pct": round(room_dn, 2),
        "edge_score": edge_score,
        "edge_label": edge_label,
        "edge_notes": rs_notes + sector_notes + inst_notes,
        "trend": "bullish" if bullish_stack else "bearish" if bearish_stack else "mixed",
        "trend_age": int(min(200, max(1, (close.tail(20) > ema20.tail(20)).sum()))) if len(close) >= 20 else None,
        "rsi": round(_safe_float(rsi14.iloc[-1]), 1),
        "atr_pct": round(atr_pct, 2),
        "rel_vol": round(rel_vol, 2),
        "oi_change_pct": round(oi.get("oi_change_pct", 0.0), 1) if oi else 0.0,
        "call_share": round(oi.get("call_share", 0.0), 1) if oi else None,
        "put_share": round(oi.get("put_share", 0.0), 1) if oi else None,
        "macd_hist": round(_safe_float(macd_hist.iloc[-1]), 4),
        "ema20": round(_safe_float(ema20.iloc[-1]), 2),
        "ema50": round(_safe_float(ema50.iloc[-1]), 2),
        "ema200": round(_safe_float(ema200.iloc[-1]), 2),
        "bullish_stack": bullish_stack,
        "bearish_stack": bearish_stack,
    }


def merge_edge_fields(target: Dict[str, Any], symbol: str, frame: Any = None, benchmark: str = "SPY") -> Dict[str, Any]:
    edge = edge_profile(symbol, frame=frame, benchmark=benchmark)
    if not edge.get("available"):
        target["edge"] = edge
        return target
    target.update({
        "edge_score": edge.get("edge_score"),
        "edge_label": edge.get("edge_label"),
        "rs_score": edge.get("rs_score"),
        "rs_bias": edge.get("rs_bias"),
        "vol_score": edge.get("vol_score"),
        "vol_regime": edge.get("vol_regime"),
        "sector_score": edge.get("sector_score"),
        "institutional_score": edge.get("institutional_score"),
        "expected_move_score": edge.get("expected_move_score"),
        "expected_move_pct": edge.get("expected_move_pct"),
        "room_up_pct": edge.get("room_up_pct"),
        "room_down_pct": edge.get("room_down_pct"),
        "sector": edge.get("sector"),
        "sector_etf": edge.get("sector_etf"),
        "rsi": edge.get("rsi"),
        "atr_pct": edge.get("atr_pct"),
        "rel_vol": edge.get("rel_vol"),
        "oi_change_pct": edge.get("oi_change_pct"),
        "call_share": edge.get("call_share"),
        "put_share": edge.get("put_share"),
        "edge_notes": edge.get("edge_notes", []),
        "edge": edge,
    })
    return target


def trend_exhaustion_snapshot(symbol: str, frame: Any = None) -> Dict[str, Any]:
    """Detect late-stage trend exhaustion for contrarian option setups."""
    sym = symbol.upper().strip()
    df = _to_df(frame)
    if df is None:
        df = _history(sym, period="9mo")
    if df is None or df.empty or len(df) < 50:
        return {"symbol": sym, "status": "no_data"}

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    vol = df["Volume"].astype(float)
    price = _safe_float(close.iloc[-1])
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    rsi14 = _rsi(close, 14)
    atr14 = _atr(df, 14)
    macd_line, macd_sig, macd_hist = _macd(close)

    trend_up = price > _safe_float(ema20.iloc[-1]) > _safe_float(ema50.iloc[-1])
    trend_dn = price < _safe_float(ema20.iloc[-1]) < _safe_float(ema50.iloc[-1])

    # Trend maturity: consecutive closes beyond EMA20 and distance from 20d average.
    trend_age = int((close.tail(20) > ema20.tail(20)).sum()) if trend_up else int((close.tail(20) < ema20.tail(20)).sum())
    atr_now = _safe_float(atr14.iloc[-1])
    stretch_atr = abs(price - _safe_float(ema20.iloc[-1])) / max(atr_now, 1e-9) if atr_now else 0.0
    stretch_pct = abs(price - _safe_float(ema20.iloc[-1])) / max(price, 1e-9) * 100 if price else 0.0
    climax_vol = _safe_float(vol.iloc[-1]) / max(float(vol.tail(20).mean()), 1e-9) if len(vol) >= 20 else 0.0

    # Detect RSI/MACD roll-over versus prior swing.
    rsi_now = _safe_float(rsi14.iloc[-1])
    rsi_prev5 = _safe_float(rsi14.iloc[-6]) if len(rsi14) >= 6 else rsi_now
    macd_roll = _safe_float(macd_hist.iloc[-1]) < _safe_float(macd_hist.iloc[-2]) if len(macd_hist) >= 2 else False
    near_extreme = (_safe_float(close.tail(10).max()) - price) / max(price, 1e-9) * 100 < 2.0 if price else False

    bull_exh = trend_up and trend_age >= 10 and stretch_atr >= 1.8 and (rsi_now >= 68 or rsi_now < rsi_prev5) and climax_vol >= 1.1
    bear_exh = trend_dn and trend_age >= 10 and stretch_atr >= 1.8 and (rsi_now <= 32 or rsi_now > rsi_prev5) and climax_vol >= 1.1

    if not (bull_exh or bear_exh):
        # softer watchlist signal if trend is mature but not fully exhausted
        if trend_up and stretch_atr >= 1.2 and rsi_now >= 60:
            return {
                "symbol": sym,
                "status": "watch",
                "direction": "BULL_EXHAUSTION",
                "score": 48,
                "price": round(price, 2),
                "rsi": round(rsi_now, 1),
                "atr_pct": round((_safe_float(atr14.iloc[-1]) / max(price, 1e-9)) * 100, 2),
                "trend_age": trend_age,
                "stretch_atr": round(stretch_atr, 2),
                "stretch_pct": round(stretch_pct, 2),
                "climax_vol": round(climax_vol, 2),
                "detail": "Mature uptrend; watch for exhaustion or last push",
            }
        if trend_dn and stretch_atr >= 1.2 and rsi_now <= 40:
            return {
                "symbol": sym,
                "status": "watch",
                "direction": "BEAR_EXHAUSTION",
                "score": 48,
                "price": round(price, 2),
                "rsi": round(rsi_now, 1),
                "atr_pct": round((_safe_float(atr14.iloc[-1]) / max(price, 1e-9)) * 100, 2),
                "trend_age": trend_age,
                "stretch_atr": round(stretch_atr, 2),
                "stretch_pct": round(stretch_pct, 2),
                "climax_vol": round(climax_vol, 2),
                "detail": "Mature downtrend; watch for cover bounce or continuation flush",
            }
        return {"symbol": sym, "status": "none"}

    direction = "BULL_EXHAUSTION" if bull_exh else "BEAR_EXHAUSTION"
    contrarian = "PUTS" if bull_exh else "CALLS"
    base_score = 60
    base_score += min(16, int(max(0.0, stretch_atr - 1.5) * 6))
    base_score += 8 if climax_vol >= 1.5 else 4 if climax_vol >= 1.1 else 0
    base_score += 6 if macd_roll else 0
    base_score += 6 if (bull_exh and rsi_now >= 72) or (bear_exh and rsi_now <= 28) else 0
    base_score = min(100, base_score)

    notes = []
    if bull_exh:
        notes.append("Uptrend extended")
        notes.append(f"RSI {rsi_now:.0f}")
        notes.append(f"Stretch {stretch_atr:.1f} ATR")
    else:
        notes.append("Downtrend extended")
        notes.append(f"RSI {rsi_now:.0f}")
        notes.append(f"Stretch {stretch_atr:.1f} ATR")
    if climax_vol >= 1.25:
        notes.append(f"Volume {climax_vol:.1f}x avg")
    if macd_roll:
        notes.append("MACD rolling over")

    return {
        "symbol": sym,
        "status": "found",
        "direction": direction,
        "contrarian_trade": contrarian,
        "score": base_score,
        "max": 100,
        "price": round(price, 2),
        "rsi": round(rsi_now, 1),
        "atr_pct": round((_safe_float(atr14.iloc[-1]) / max(price, 1e-9)) * 100, 2),
        "trend_age": trend_age,
        "stretch_atr": round(stretch_atr, 2),
        "stretch_pct": round(stretch_pct, 2),
        "climax_vol": round(climax_vol, 2),
        "macd_hist": round(_safe_float(macd_hist.iloc[-1]), 4),
        "macd_roll": bool(macd_roll),
        "near_extreme": bool(near_extreme),
        "notes": notes,
        "detail": " | ".join(notes),
    }
