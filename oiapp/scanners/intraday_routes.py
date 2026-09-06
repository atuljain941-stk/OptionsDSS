from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from flask import Blueprint, jsonify, request

from .scoring_service import attach_scanner_scores, filter_by_min_earnings

intraday_bp = Blueprint("intraday_bp", __name__, url_prefix="/intraday")
from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

# Default fallback universe for the intraday tab.
INTRADAY_CORE_40 = [
    "AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "TSLA",
    "PLTR", "AMD", "AVGO", "ARM", "SMCI", "CRWD", "SNOW", "PANW",
    "MU", "QCOM", "MRVL", "ANET", "LRCX", "AMAT", "KLAC",
    "COIN", "MSTR", "HOOD", "RBLX", "AFRM", "UPST",
    "RKLB", "ASTS", "HIMS", "APP", "CELH", "UBER", "SHOP", "NFLX",
    "SPY", "QQQ", "IWM", "SMH",
]

TF_CFG = {
    "1m":  {"interval": "1m",  "period": "5d",  "minutes": 1},
    "2m":  {"interval": "2m",  "period": "30d", "minutes": 2},
    "5m":  {"interval": "5m",  "period": "60d", "minutes": 5},
    "15m": {"interval": "15m", "period": "60d", "minutes": 15},
}


def _normalize_tf(tf: str) -> str:
    t = str(tf or "5m").strip().lower().replace(" ", "")
    aliases = {
        "1min": "1m",
        "1minute": "1m",
        "2min": "2m",
        "2minute": "2m",
        "5min": "5m",
        "5minute": "5m",
        "15min": "15m",
        "15minute": "15m",
    }
    return TF_CFG.get(aliases.get(t, t), TF_CFG["5m"])


def _tf_tuple(tf: str) -> Tuple[str, str, int]:
    cfg = _normalize_tf(tf)
    # _normalize_tf returns the dict when valid, but we want a direct tuple here.
    if isinstance(cfg, dict):
        return cfg["interval"], cfg["period"], int(cfg["minutes"])
    return "5m", "60d", 5


def _get_watchlist_symbols(wl_id: Any) -> Optional[List[str]]:
    if not wl_id:
        return None
    try:
        import sqlite3
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (int(wl_id),),
        ).fetchall()
        con.close()
        return [r[0] for r in rows] if rows else None
    except Exception:
        return None


@lru_cache(maxsize=512)
def _fetch_history_cached(symbol: str, interval: str, period: str) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf

        sym = symbol.upper().strip()
        df = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=False, prepost=False)
        if df is None or df.empty:
            return None
        df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).copy()
        df = df.sort_index()
        return df
    except Exception:
        return None


def _history(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    interval, period, _ = _tf_tuple(tf)
    df = _fetch_history_cached(symbol.upper().strip(), interval, period)
    return None if df is None else df.copy()


def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _classify_state(score: float) -> str:
    if score >= 80:
        return "Strong Trend"
    if score >= 60:
        return "Trend"
    if score >= 45:
        return "Neutral"
    if score >= 30:
        return "Range"
    return "Chop"


def _session_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    try:
        last_day = df.index[-1].date()
        sess = df[df.index.date == last_day].copy()
        return sess if not sess.empty else df.copy()
    except Exception:
        return df.copy()


def _intraday_snapshot(symbol: str, tf: str) -> Optional[Dict[str, Any]]:
    df = _history(symbol, tf)
    if df is None or df.empty:
        return None
    interval, _, minutes = _tf_tuple(tf)
    sess = _session_frame(df)
    if sess is None or sess.empty:
        return None

    close = sess["Close"].astype(float)
    open_ = sess["Open"].astype(float)
    high = sess["High"].astype(float)
    low = sess["Low"].astype(float)
    vol = sess["Volume"].astype(float)

    typ = (high + low + close) / 3.0
    vwap = (typ * vol).cumsum() / vol.cumsum().replace(0, pd.NA)
    vwap = vwap.fillna(method="ffill").fillna(close)
    ema9 = _ema(close, 9)
    ema20 = _ema(close, 20)
    atr14 = _atr(sess, 14)

    last = sess.iloc[-1]
    last_idx = len(sess) - 1
    orb_bars = max(1, int(round(30 / max(minutes, 1))))
    orb_bars = min(orb_bars, len(sess))
    orb_high = float(high.iloc[:orb_bars].max())
    orb_low = float(low.iloc[:orb_bars].min())
    orb_break_up = float(last["Close"]) > orb_high and len(sess) > orb_bars
    orb_break_dn = float(last["Close"]) < orb_low and len(sess) > orb_bars

    avg_vol20 = float(vol.tail(20).mean()) if len(vol) else 0.0
    rel_vol = float(last["Volume"]) / max(avg_vol20, 1.0)
    candle_range = float(last["High"] - last["Low"])
    candle_body = abs(float(last["Close"]) - float(last["Open"]))
    body_ratio = candle_body / candle_range if candle_range > 0 else 0.0
    atr_now = float(atr14.iloc[-1]) if len(atr14) else 0.0
    atr_multiple = candle_range / max(atr_now, 1e-9) if atr_now > 0 else 0.0
    close_pos = (float(last["Close"]) - float(last["Low"])) / max(candle_range, 1e-9) if candle_range > 0 else 0.5
    ema20_slope = float(ema20.iloc[-1] - ema20.iloc[max(0, len(ema20) - 6)]) / max(min(5, len(ema20) - 1), 1)

    above_vwap = float(last["Close"]) > float(vwap.iloc[-1])
    ema_bull = float(ema9.iloc[-1]) > float(ema20.iloc[-1])
    ema_bear = float(ema9.iloc[-1]) < float(ema20.iloc[-1])
    ema_up = ema20_slope > 0
    ema_down = ema20_slope < 0

    bull = 0.0
    bear = 0.0
    bull_tags: List[str] = []
    bear_tags: List[str] = []

    if above_vwap:
        bull += 20
        bull_tags.append("Above VWAP")
    else:
        bear += 20
        bear_tags.append("Below VWAP")

    if ema_bull:
        bull += 15
        bull_tags.append("EMA9 > EMA20")
    elif ema_bear:
        bear += 15
        bear_tags.append("EMA9 < EMA20")

    if ema_up:
        bull += 10
        bull_tags.append("EMA20 rising")
    elif ema_down:
        bear += 10
        bear_tags.append("EMA20 falling")

    if rel_vol >= 2.0:
        if float(last["Close"]) >= float(last["Open"]):
            bull += 15
            bull_tags.append(f"RVOL {rel_vol:.1f}x")
        else:
            bear += 15
            bear_tags.append(f"RVOL {rel_vol:.1f}x")
    elif rel_vol >= 1.5:
        if float(last["Close"]) >= float(last["Open"]):
            bull += 10
            bull_tags.append(f"RVOL {rel_vol:.1f}x")
        else:
            bear += 10
            bear_tags.append(f"RVOL {rel_vol:.1f}x")

    if candle_range > 0 and body_ratio >= 0.6:
        if float(last["Close"]) >= float(last["Open"]):
            bull += 10
            bull_tags.append(f"Strong candle {body_ratio*100:.0f}% body")
        else:
            bear += 10
            bear_tags.append(f"Strong candle {body_ratio*100:.0f}% body")

    if atr_now > 0 and atr_multiple >= 1.0:
        if float(last["Close"]) >= float(last["Open"]):
            bull += 10
            bull_tags.append(f"Range {atr_multiple:.1f}x ATR")
        else:
            bear += 10
            bear_tags.append(f"Range {atr_multiple:.1f}x ATR")

    if orb_break_up:
        bull += 20
        bull_tags.append(f"ORB break > ${orb_high:.2f}")
    elif orb_break_dn:
        bear += 20
        bear_tags.append(f"ORB break < ${orb_low:.2f}")

    if close_pos >= 0.75:
        bull += 10
        bull_tags.append("Closes near highs")
    elif close_pos <= 0.25:
        bear += 10
        bear_tags.append("Closes near lows")

    direction = "BULLISH" if bull > bear and bull >= 35 else "BEARISH" if bear > bull and bear >= 35 else "NEUTRAL"
    native_score = max(bull, bear)
    tags = bull_tags if direction == "BULLISH" else bear_tags if direction == "BEARISH" else sorted(set(bull_tags + bear_tags))

    trigger_bars = len(sess) - 1
    if direction == "BULLISH" and orb_break_up:
        try:
            trig = sess.index[sess["Close"].astype(float) > orb_high][0]
            trigger_bars = max(0, len(sess) - 1 - int(sess.index.get_loc(trig)))
        except Exception:
            pass
    elif direction == "BEARISH" and orb_break_dn:
        try:
            trig = sess.index[sess["Close"].astype(float) < orb_low][0]
            trigger_bars = max(0, len(sess) - 1 - int(sess.index.get_loc(trig)))
        except Exception:
            pass

    trend_state = _classify_state(native_score)
    chop_score = max(0.0, min(100.0, 100.0 - native_score + max(0, len(sess) // max(1, orb_bars)) * 2))

    result: Dict[str, Any] = {
        "symbol": symbol.upper(),
        "timeframe": tf,
        "direction": direction,
        "state": trend_state,
        "trend_score": round(native_score, 1),
        "chop_score": round(chop_score, 1),
        "price": round(float(last["Close"]), 2),
        "open": round(float(last["Open"]), 2),
        "high": round(float(last["High"]), 2),
        "low": round(float(last["Low"]), 2),
        "vwap": round(float(vwap.iloc[-1]), 2),
        "ema9": round(float(ema9.iloc[-1]), 2),
        "ema20": round(float(ema20.iloc[-1]), 2),
        "ema20_slope": round(float(ema20_slope), 4),
        "atr14": round(float(atr_now), 3),
        "rel_vol": round(float(rel_vol), 2),
        "candle_body_pct": round(float(body_ratio) * 100.0, 1),
        "candle_range_atr": round(float(atr_multiple), 2),
        "orb_high": round(float(orb_high), 2),
        "orb_low": round(float(orb_low), 2),
        "orb_breakup": bool(orb_break_up),
        "orb_breakdown": bool(orb_break_dn),
        "vwap_bias": "Bullish" if above_vwap else "Bearish",
        "ema_bias": "Bullish" if ema_bull else "Bearish" if ema_bear else "Flat",
        "signals": tags,
        "trend_age": int(trigger_bars),
        "session_bars": int(len(sess)),
    }
    return result


def _overall_regime(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return {"state": "No data", "direction": "NEUTRAL", "score": 0.0, "chop_score": 100.0}
    avg_score = sum(float(i.get("trend_score", 0) or 0) for i in items) / max(len(items), 1)
    dirs = [str(i.get("direction") or "NEUTRAL") for i in items]
    bull_ct = sum(1 for d in dirs if d == "BULLISH")
    bear_ct = sum(1 for d in dirs if d == "BEARISH")
    if bull_ct > bear_ct:
        direction = "BULLISH"
    elif bear_ct > bull_ct:
        direction = "BEARISH"
    else:
        direction = "MIXED"
    if avg_score >= 80 and direction in ("BULLISH", "BEARISH"):
        state = f"Strong {'Bull' if direction=='BULLISH' else 'Bear'} Trend Day"
    elif avg_score >= 60:
        state = "Trend Day"
    elif avg_score >= 45:
        state = "Neutral"
    elif avg_score >= 30:
        state = "Range Day"
    else:
        state = "Chop Day"
    chop = max(0.0, min(100.0, 100.0 - avg_score + abs(bull_ct - bear_ct) * 5))
    return {"state": state, "direction": direction, "score": round(avg_score, 1), "chop_score": round(chop, 1)}


@intraday_bp.route("/regime", methods=["GET", "POST"])
def market_regime():
    d = request.get_json(silent=True) or {}
    def _p(k, default=None):
        return request.args.get(k, d.get(k, default))

    tf = str(_p("timeframe", "5m")).strip() or "5m"
    symbols = [s.strip().upper() for s in str(_p("symbols", "SPY,QQQ,IWM")).split(",") if s.strip()] or ["SPY", "QQQ", "IWM"]
    workers = int(_p("workers", 4) or 4)

    try:
        rows: List[Dict[str, Any]] = []
        ex = ThreadPoolExecutor(max_workers=max(1, min(workers, len(symbols) or 1)))
        try:
            futs = {ex.submit(_intraday_snapshot, sym, tf): sym for sym in symbols}
            from ..services.bounded_wait import bounded_as_completed
            for fut, sym in bounded_as_completed(futs, timeout=45,
                    on_timeout=lambda ks: print(f"[intraday_routes] {len(ks)} symbol(s) timed out: {ks[:20]}")):
                if fut is None:
                    continue
                row = fut.result()
                if row:
                    rows.append(row)
        finally:
            ex.shutdown(wait=False)
        rows.sort(key=lambda x: x.get("trend_score", 0), reverse=True)
        overall = _overall_regime(rows)
        return jsonify({
            "timeframe": tf,
            "count": len(rows),
            "summary": overall,
            "results": rows,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500


@intraday_bp.route("/momentum_ignition", methods=["GET", "POST"])
def momentum_ignition():
    d = request.get_json(silent=True) or {}
    def _p(k, default=None):
        return request.args.get(k, d.get(k, default))

    tf = str(_p("timeframe", "5m")).strip() or "5m"
    watchlist_id = _p("watchlist_id", None)
    symbols_raw = _p("symbols", "") or ""
    min_rvol = float(_p("min_rvol", 1.2) or 1.2)
    min_candle_atr = float(_p("min_candle_atr", 0.5) or 0.5)
    min_score = float(_p("min_score", 45) or 45)
    min_price = float(_p("min_price", 20) or 20)
    min_earn_days = _p("min_earn_days", None)
    if min_earn_days in (None, "", "None", "null"):
        min_earn_days = None
    else:
        try:
            min_earn_days = int(min_earn_days)
        except Exception:
            min_earn_days = None
    workers = int(_p("workers", 8) or 8)

    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()] or None
    if not symbols and watchlist_id:
        symbols = _get_watchlist_symbols(watchlist_id)
    if not symbols:
        symbols = list(INTRADAY_CORE_40)

    results: List[Dict[str, Any]] = []
    errs: List[str] = []
    try:
        ex = ThreadPoolExecutor(max_workers=max(2, min(workers, 12)))
        try:
            futs = {ex.submit(_intraday_snapshot, sym, tf): sym for sym in symbols}
            from ..services.bounded_wait import bounded_as_completed
            for fut, sym in bounded_as_completed(futs, timeout=60,
                    on_timeout=lambda ks: print(f"[intraday_routes] {len(ks)} symbol(s) timed out: {ks[:20]}")):
                if fut is None:
                    continue
                try:
                    snap = fut.result()
                    if not snap:
                        continue
                    if snap.get("price", 0) < min_price:
                        continue
                    if snap.get("rel_vol", 0) < min_rvol:
                        continue
                    if snap.get("candle_range_atr", 0) < min_candle_atr:
                        continue
                    if snap.get("trend_score", 0) < min_score:
                        continue
                    frame = _history(sym, tf)
                    if frame is None or frame.empty:
                        continue
                    setup = attach_scanner_scores(
                        snap,
                        sym,
                        frame=frame,
                        setup_type="Momentum Ignition",
                        direction=snap.get("direction", "NEUTRAL"),
                        native_score=snap.get("trend_score", 0),
                        trend_age=snap.get("trend_age", 0),
                        benchmark="SPY",
                    )
                    results.append(setup)
                except Exception as inner_e:
                    errs.append(f"{sym}: {inner_e}")
        finally:
            ex.shutdown(wait=False)
        results.sort(key=lambda r: (r.get("final_score") or r.get("score") or 0), reverse=True)
        if min_earn_days is not None:
            results = filter_by_min_earnings(results, min_earn_days)
        return jsonify({
            "timeframe": tf,
            "count": len(results),
            "results": results,
            "errors": errs[:10],
            "params": {
                "watchlist_id": int(watchlist_id) if watchlist_id else None,
                "timeframe": tf,
                "min_rvol": min_rvol,
                "min_candle_atr": min_candle_atr,
                "min_score": min_score,
                "min_price": min_price,
                "min_earn_days": min_earn_days,
            },
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500
