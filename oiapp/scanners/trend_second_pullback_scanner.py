from __future__ import annotations

import io
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import pandas as pd

from .edge_factors import _ema, _rsi, _atr, _macd, _safe_float
from .scoring_service import attach_scanner_scores

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

TF_MAP = {
    "1h": ("1h", "729d", None),
    "2h": ("1h", "729d", "2h"),
    "4h": ("1h", "729d", "4h"),
    "1d": ("1d", "5y", None),
    "1w": ("1wk", "10y", None),
    "1m": ("1mo", "10y", None),
}
TF_ORDER = {"1h": 0, "2h": 1, "4h": 2, "1d": 3, "1w": 4, "1m": 5}
TF_LABEL = {"1h": "1h", "2h": "2h", "4h": "4h", "1d": "1d", "1w": "1w", "1m": "1m"}


def _normalize_tf(tf: Optional[str]) -> str:
    if not tf:
        return "1d"
    t = str(tf).strip().lower().replace(" ", "")
    aliases = {
        "daily": "1d",
        "day": "1d",
        "1day": "1d",
        "weekly": "1w",
        "week": "1w",
        "1wk": "1w",
        "monthly": "1m",
        "month": "1m",
        "1mo": "1m",
    }
    return aliases.get(t, t if t in TF_MAP else "1d")


@contextmanager
def _suppress_yfinance_noise():
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


def _history_kwargs(period: str, interval: str) -> dict:
    interval = str(interval or "1d").lower()
    kwargs = {"interval": interval, "auto_adjust": False}
    if interval == "1h":
        end_day = date.today() + timedelta(days=1)
        start_day = date.today() - timedelta(days=729)
        kwargs.update({"start": start_day.isoformat(), "end": end_day.isoformat()})
    else:
        kwargs["period"] = period
    return kwargs


def _ticker_history_quiet(ticker, **kwargs):
    try:
        return ticker.history(**kwargs, raise_errors=True)
    except TypeError:
        # Older yfinance versions do not accept raise_errors. Fall back quietly.
        pass
    except Exception:
        return None
    try:
        with _suppress_yfinance_noise():
            return ticker.history(**kwargs)
    except Exception:
        return None


def _fetch_tf(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf

        tf = _normalize_tf(tf)
        interval, period, resample_rule = TF_MAP.get(tf, TF_MAP["1d"])
        df = _ticker_history_quiet(yf.Ticker(symbol), **_history_kwargs(period, interval))
        if df is None or df.empty:
            return None
        df = df.dropna(subset=["Close", "High", "Low", "Volume"]).copy()
        if resample_rule:
            agg = {
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }
            df = df.resample(resample_rule).agg(agg).dropna(subset=["Close"])
        return df if len(df) >= 25 else None
    except Exception:
        return None


def _symbols_from_db() -> List[str]:
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()
        con.close()
        return [r[0] for r in rows if r and r[0]]
    except Exception:
        return []


def _wl_symbols(watchlist_id: Optional[int]) -> Optional[List[str]]:
    if not watchlist_id:
        return None
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (int(watchlist_id),),
        ).fetchall()
        con.close()
        return [r[0] for r in rows if r and r[0]] or None
    except Exception:
        return None


def _compress_runs(deltas: Sequence[float]) -> List[Tuple[int, int, int]]:
    """Compress sign runs from a delta series."""
    runs: List[Tuple[int, int, int]] = []
    cur_sign = 0
    start = 0
    for i, d in enumerate(deltas):
        try:
            sign = 1 if float(d) > 0 else -1 if float(d) < 0 else 0
        except Exception:
            sign = 0
        if sign == 0:
            continue
        if cur_sign == 0:
            cur_sign = sign
            start = i
            continue
        if sign != cur_sign:
            runs.append((cur_sign, start, i - 1))
            cur_sign = sign
            start = i
    if cur_sign != 0:
        runs.append((cur_sign, start, len(deltas) - 1))
    return runs


def _slice_min(series: Sequence[float], start: int, end: int) -> float:
    start = max(0, start)
    end = max(start, end)
    vals = [float(x) for x in series[start : end + 1]]
    return min(vals) if vals else 0.0


def _slice_max(series: Sequence[float], start: int, end: int) -> float:
    start = max(0, start)
    end = max(start, end)
    vals = [float(x) for x in series[start : end + 1]]
    return max(vals) if vals else 0.0


def _align_pullback_start(
    pb_index: pd.Index,
    exhaustion_ts: pd.Timestamp,
    exhaustion_tf: str,
    pullback_tf: str,
) -> Optional[int]:
    if pb_index is None or len(pb_index) == 0:
        return None

    idx = pd.DatetimeIndex(pb_index)
    try:
        idx = idx.tz_localize(None)
    except Exception:
        try:
            idx = idx.tz_convert(None)
        except Exception:
            pass

    ex_tf = _normalize_tf(exhaustion_tf)
    pb_tf = _normalize_tf(pullback_tf)
    ex_ts = pd.Timestamp(exhaustion_ts)
    try:
        ex_ts = ex_ts.tz_localize(None)
    except Exception:
        try:
            ex_ts = ex_ts.tz_convert(None)
        except Exception:
            pass

    # If the exhaustion timeframe is daily or higher and the pullback timeframe
    # is lower, start from the next calendar date so we don't include bars that
    # happened before the higher timeframe bar closed.
    if TF_ORDER.get(ex_tf, 3) >= TF_ORDER["1d"] and TF_ORDER.get(pb_tf, 0) < TF_ORDER.get(ex_tf, 3):
        start_mask = idx.normalize() > ex_ts.normalize()
    else:
        start_mask = idx >= ex_ts

    hits = [i for i, flag in enumerate(start_mask) if bool(flag)]
    return hits[0] if hits else None


def _scan_one(
    sym: str,
    lookback_days: int = 20,
    rsi_hi: float = 68.0,
    rsi_lo: float = 32.0,
    diff_thr: float = 20.0,
    min_bounce_pct: float = 4.0,
    min_second_bars: int = 1,
    workers_hint: int = 1,
    exhaust_tf: str = "1d",
    pullback_tf: str = "1h",
) -> Optional[dict]:
    try:
        exhaust_tf = _normalize_tf(exhaust_tf)
        pullback_tf = _normalize_tf(pullback_tf)

        exhaust_df = _fetch_tf(sym, exhaust_tf)
        if exhaust_df is None or exhaust_df.empty or len(exhaust_df) < 60:
            return None

        if pullback_tf == exhaust_tf:
            pullback_df = exhaust_df
        else:
            pullback_df = _fetch_tf(sym, pullback_tf)
        if pullback_df is None or pullback_df.empty or len(pullback_df) < 60:
            return None

        close = exhaust_df["Close"].astype(float)
        high = exhaust_df["High"].astype(float)
        low = exhaust_df["Low"].astype(float)
        vol = exhaust_df["Volume"].astype(float)

        price = _safe_float(close.iloc[-1])
        ema20 = _ema(close, 20)
        ema50 = _ema(close, 50)
        ema200 = _ema(close, 200) if len(close) >= 200 else _ema(close, min(50, max(10, len(close) - 1)))
        rsi14 = _rsi(close, 14)
        rsi_ema90 = _ema(rsi14.fillna(50.0), 90)
        diff = rsi14 - rsi_ema90
        atr14 = _atr(exhaust_df, 14)
        macd_line, macd_sig, macd_hist = _macd(close)

        n = len(close) - 1
        start_idx = max(20, n - int(lookback_days) + 1)

        bull_candidates = [
            i for i in range(start_idx, n + 1)
            if _safe_float(rsi14.iloc[i]) >= float(rsi_hi)
            and _safe_float(diff.iloc[i]) >= float(diff_thr)
        ]
        bear_candidates = [
            i for i in range(start_idx, n + 1)
            if _safe_float(rsi14.iloc[i]) <= float(rsi_lo)
            and _safe_float(diff.iloc[i]) <= -float(diff_thr)
        ]

        def _build(direction: str, ex_idx: int) -> Optional[dict]:
            ex_ts = pd.Timestamp(exhaust_df.index[ex_idx])
            ex_date = ex_ts.date().isoformat() if hasattr(ex_ts, "date") else str(ex_ts)[:10]
            ex_price = float(close.iloc[ex_idx])

            pb_start = ex_idx if pullback_tf == exhaust_tf else _align_pullback_start(
                pullback_df.index, ex_ts, exhaust_tf, pullback_tf
            )
            if pb_start is None:
                return None

            pb_close = pullback_df["Close"].astype(float).iloc[pb_start:].tolist()
            if len(pb_close) < 6:
                return None

            cur = float(pb_close[-1])
            deltas = [pb_close[i] - pb_close[i - 1] for i in range(1, len(pb_close))]
            runs = _compress_runs(deltas)
            if len(runs) < 3:
                return None

            s1, s2, s3 = runs[-3], runs[-2], runs[-1]
            pb_label = TF_LABEL.get(pullback_tf, pullback_tf)
            ex_label = TF_LABEL.get(exhaust_tf, exhaust_tf)

            if direction == "BULL":
                # down -> up -> down (oversold exhaustion, then second pullback)
                if not (s1[0] == -1 and s2[0] == 1 and s3[0] == -1):
                    return None
                pull1_lo = _slice_min(pb_close, s1[1], s1[2] + 1)
                bounce_hi = _slice_max(pb_close, s2[1], s2[2] + 1)
                second_lo = _slice_min(pb_close, s3[1], s3[2] + 1)
                bounce_pct = ((bounce_hi - pull1_lo) / max(pull1_lo, 1e-9)) * 100 if pull1_lo else 0.0
                second_pullback_pct = ((bounce_hi - cur) / max(bounce_hi, 1e-9)) * 100 if bounce_hi else 0.0
                first_retreat_pct = ((ex_price - pull1_lo) / max(ex_price, 1e-9)) * 100 if ex_price else 0.0
                second_run_bars = (runs[-1][2] - runs[-1][1] + 1) if runs else 0
                second_stage = cur < float(pb_close[-2]) and second_run_bars >= int(min_second_bars)
                stage_ok = second_stage and second_pullback_pct >= 0.5 and bounce_pct >= float(min_bounce_pct)
                trend_ok = float(close.iloc[ex_idx]) < _safe_float(ema20.iloc[ex_idx]) < _safe_float(ema50.iloc[ex_idx])
                ext_rsi = _safe_float(rsi14.iloc[ex_idx])
                ext_diff = _safe_float(diff.iloc[ex_idx])
                score = 55
                score += min(15, int(max(0.0, float(rsi_lo) - ext_rsi) * 1.2))
                score += min(15, int(max(0.0, abs(ext_diff) - float(diff_thr)) * 0.7))
                score += min(10, int(max(0.0, bounce_pct - float(min_bounce_pct)) * 1.0))
                score += min(8, int(max(0.0, second_pullback_pct) * 1.5))
                score += 6 if trend_ok else 0
                score += 4 if n - ex_idx <= max(3, int(lookback_days / 2)) else 0
                future_max = _safe_float(diff.iloc[max(0, ex_idx + 1) : min(len(diff), ex_idx + 8)].max())
                score += 4 if future_max >= float(diff_thr) else 0
                score = min(100, score)
                if not stage_ok:
                    return None
                result = {
                    "symbol": sym,
                    "direction": "BULL_SECOND_PULLBACK",
                    "setup_type": "Trend Exhaustion / Second Pullback",
                    "setup": "Oversold exhaustion -> 2nd pullback",
                    "trade_bias": "CALLS",
                    "signal": "Buy the second pullback",
                    "score": score,
                    "native_score": score,
                    "price": round(cur, 2),
                    "exhaustion_tf": ex_label,
                    "pullback_tf": pb_label,
                    "timeframe_combo": f"{ex_label}->{pb_label}",
                    "exhaustion_date": ex_date,
                    "exhaustion_age": int(n - ex_idx),
                    "exhaustion_rsi": round(ext_rsi, 1),
                    "exhaustion_diff": round(ext_diff, 1),
                    "bounce_pct": round(bounce_pct, 1),
                    "first_pullback_pct": round(first_retreat_pct, 1),
                    "second_pullback_pct": round(second_pullback_pct, 1),
                    "pullback_stage": 2,
                    "run_pattern": "down-up-down",
                    "pull1_lo": round(pull1_lo, 2),
                    "bounce_hi": round(bounce_hi, 2),
                    "second_lo": round(second_lo, 2),
                    "rsi": round(_safe_float(rsi14.iloc[-1]), 1),
                    "rsi_ema_diff": round(_safe_float(diff.iloc[-1]), 1),
                    "atr_pct": round((_safe_float(atr14.iloc[-1]) / max(cur, 1e-9)) * 100, 2),
                    "trend_age": int((close.tail(20) < ema20.tail(20)).sum()),
                    "macd_hist": round(_safe_float(macd_hist.iloc[-1]), 4),
                    "macd_roll": bool(_safe_float(macd_hist.iloc[-1]) < _safe_float(macd_hist.iloc[-2]) if len(macd_hist) >= 2 else False),
                    "ema20": round(_safe_float(ema20.iloc[-1]), 2),
                    "ema50": round(_safe_float(ema50.iloc[-1]), 2),
                    "ema200": round(_safe_float(ema200.iloc[-1]), 2),
                    "detail": f"Oversold exhaustion on {ex_label} at RSI {ext_rsi:.0f} and RSI-EMA90 {ext_diff:+.0f}; {pb_label} bounce {bounce_pct:.1f}% then second pullback",
                    "notes": [
                        f"Exhaustion {ex_date}",
                        f"Exh TF {ex_label}",
                        f"PB TF {pb_label}",
                        f"RSI {ext_rsi:.0f}",
                        f"RSI-EMA {ext_diff:+.0f}",
                        f"Bounce {bounce_pct:.1f}%",
                        f"2nd pullback {second_pullback_pct:.1f}%",
                    ],
                    "stage_ok": True,
                }
                return attach_scanner_scores(result, sym, frame=exhaust_df, setup_type="Trend Exhaustion / Second Pullback", direction=result["direction"], native_score=result["native_score"], trend_age=result["trend_age"])

            # BEAR: up -> down -> up
            if not (s1[0] == 1 and s2[0] == -1 and s3[0] == 1):
                return None
            pull1_hi = _slice_max(pb_close, s1[1], s1[2] + 1)
            bounce_lo = _slice_min(pb_close, s2[1], s2[2] + 1)
            second_hi = _slice_max(pb_close, s3[1], s3[2] + 1)
            bounce_pct = ((pull1_hi - bounce_lo) / max(pull1_hi, 1e-9)) * 100 if pull1_hi else 0.0
            second_pullback_pct = ((cur - bounce_lo) / max(bounce_lo, 1e-9)) * 100 if bounce_lo else 0.0
            first_retreat_pct = ((pull1_hi - ex_price) / max(ex_price, 1e-9)) * 100 if ex_price else 0.0
            second_run_bars = (runs[-1][2] - runs[-1][1] + 1) if runs else 0
            second_stage = cur > float(pb_close[-2]) and second_run_bars >= int(min_second_bars)
            stage_ok = second_stage and bounce_pct >= float(min_bounce_pct) and second_pullback_pct >= 0.5
            trend_ok = float(close.iloc[ex_idx]) > _safe_float(ema20.iloc[ex_idx]) > _safe_float(ema50.iloc[ex_idx])
            ext_rsi = _safe_float(rsi14.iloc[ex_idx])
            ext_diff = _safe_float(diff.iloc[ex_idx])
            score = 55
            score += min(15, int(max(0.0, ext_rsi - float(rsi_hi)) * 1.2))
            score += min(15, int(max(0.0, abs(ext_diff) - float(diff_thr)) * 0.7))
            score += min(10, int(max(0.0, bounce_pct - float(min_bounce_pct)) * 1.0))
            score += min(8, int(max(0.0, second_pullback_pct) * 1.5))
            score += 6 if trend_ok else 0
            score += 4 if n - ex_idx <= max(3, int(lookback_days / 2)) else 0
            future_min = _safe_float(diff.iloc[max(0, ex_idx + 1) : min(len(diff), ex_idx + 8)].min())
            score += 4 if future_min <= -float(diff_thr) else 0
            score = min(100, score)
            if not stage_ok:
                return None
            return {
                "symbol": sym,
                "direction": "BEAR_SECOND_PULLBACK",
                "setup": "Overbought exhaustion -> 2nd pullback",
                "trade_bias": "PUTS",
                "signal": "Fade the second pullback",
                "score": score,
                "price": round(cur, 2),
                "exhaustion_tf": ex_label,
                "pullback_tf": pb_label,
                "timeframe_combo": f"{ex_label}->{pb_label}",
                "exhaustion_date": ex_date,
                "exhaustion_age": int(n - ex_idx),
                "exhaustion_rsi": round(ext_rsi, 1),
                "exhaustion_diff": round(ext_diff, 1),
                "bounce_pct": round(bounce_pct, 1),
                "first_pullback_pct": round(first_retreat_pct, 1),
                "second_pullback_pct": round(second_pullback_pct, 1),
                "pullback_stage": 2,
                "run_pattern": "up-down-up",
                "pull1_hi": round(pull1_hi, 2),
                "bounce_lo": round(bounce_lo, 2),
                "second_hi": round(second_hi, 2),
                "rsi": round(_safe_float(rsi14.iloc[-1]), 1),
                "rsi_ema_diff": round(_safe_float(diff.iloc[-1]), 1),
                "atr_pct": round((_safe_float(atr14.iloc[-1]) / max(cur, 1e-9)) * 100, 2),
                "trend_age": int((close.tail(20) > ema20.tail(20)).sum()),
                "macd_hist": round(_safe_float(macd_hist.iloc[-1]), 4),
                "macd_roll": bool(_safe_float(macd_hist.iloc[-1]) > _safe_float(macd_hist.iloc[-2]) if len(macd_hist) >= 2 else False),
                "ema20": round(_safe_float(ema20.iloc[-1]), 2),
                "ema50": round(_safe_float(ema50.iloc[-1]), 2),
                "ema200": round(_safe_float(ema200.iloc[-1]), 2),
                "detail": f"Overbought exhaustion on {ex_label} at RSI {ext_rsi:.0f} and RSI-EMA90 {ext_diff:+.0f}; {pb_label} bounce {bounce_pct:.1f}% then second pullback",
                "notes": [
                    f"Exhaustion {ex_date}",
                    f"Exh TF {ex_label}",
                    f"PB TF {pb_label}",
                    f"RSI {ext_rsi:.0f}",
                    f"RSI-EMA {ext_diff:+.0f}",
                    f"Bounce {bounce_pct:.1f}%",
                    f"2nd pullback {second_pullback_pct:.1f}%",
                ],
                "stage_ok": True,
            }
            return attach_scanner_scores(result, sym, frame=exhaust_df, setup_type="Trend Exhaustion / Second Pullback", direction=result["direction"], native_score=result["native_score"], trend_age=result["trend_age"])

        for idx in reversed(bull_candidates):
            r = _build("BULL", idx)
            if r:
                return r
        for idx in reversed(bear_candidates):
            r = _build("BEAR", idx)
            if r:
                return r
        return None
    except Exception:
        return None


def run_trend_second_pullback_scan(
    symbols: Optional[Sequence[str]] = None,
    watchlist_id: Optional[int] = None,
    workers: int = 18,
    lookback_days: int = 20,
    rsi_hi: float = 68.0,
    rsi_lo: float = 32.0,
    diff_thr: float = 20.0,
    min_bounce_pct: float = 4.0,
    min_second_bars: int = 1,
    exhaust_tf: str = "1d",
    pullback_tf: str = "1h",
) -> dict:
    if symbols is None:
        symbols = _wl_symbols(watchlist_id) or _symbols_from_db()
    if not symbols:
        symbols = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "META", "TSLA", "AMD", "AMZN", "GOOGL"]

    results: List[dict] = []
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {
            ex.submit(
                _scan_one,
                sym,
                lookback_days,
                rsi_hi,
                rsi_lo,
                diff_thr,
                min_bounce_pct,
                min_second_bars,
                workers,
                exhaust_tf,
                pullback_tf,
            ): sym
            for sym in symbols[:160]
        }
        from ..services.bounded_wait import bounded_as_completed
        for fut, sym in bounded_as_completed(futs, timeout=90,
                on_timeout=lambda ks: print(f"[trend_second_pullback_scanner] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                continue
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                continue
    finally:
        ex.shutdown(wait=False)

    results.sort(key=lambda x: (-x.get("score", 0), -x.get("edge_score", 0), x.get("symbol", "")))
    bulls = [r for r in results if r.get("direction") == "BULL_SECOND_PULLBACK"]
    bears = [r for r in results if r.get("direction") == "BEAR_SECOND_PULLBACK"]

    return {
        "count": len(results),
        "bull_count": len(bulls),
        "bear_count": len(bears),
        "bulls": bulls,
        "bears": bears,
        "results": results,
        "completed_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            "watchlist_id": watchlist_id,
            "lookback_days": lookback_days,
            "rsi_hi": rsi_hi,
            "rsi_lo": rsi_lo,
            "diff_thr": diff_thr,
            "min_bounce_pct": min_bounce_pct,
            "min_second_bars": min_second_bars,
            "exhaust_tf": _normalize_tf(exhaust_tf),
            "pullback_tf": _normalize_tf(pullback_tf),
            "workers": workers,
        },
    }
