from __future__ import annotations

import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, redirect, url_for

from .journal.journal_routes import _compute_live_pnl
from .scanners.maya_composite_logic import composite_overlay
from .scanners.scoring_service import attach_scanner_scores

maya_bp = Blueprint("maya_bp", __name__, url_prefix="/maya")
DB_PATH = str(Path(__file__).resolve().parents[1] / "options_data.db")


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _safe(v, dec: int = 2):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, dec)
    except Exception:
        return None


def _watchlist_symbols(watchlist_id: Optional[int] = None) -> List[str]:
    con = _conn()
    try:
        if watchlist_id:
            rows = con.execute(
                "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                (watchlist_id,),
            ).fetchall()
        else:
            rows = con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()
        return [r[0].upper() for r in rows if r and r[0]]
    finally:
        con.close()


def _history(symbol: str, period: str = "9mo") -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf

        df = yf.Ticker(symbol).history(period=period, auto_adjust=False)
        if df is None or df.empty:
            return None
        df = df.dropna(subset=["Close", "High", "Low", "Volume"]).copy()
        return df
    except Exception:
        return None


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


def _macd(close: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = _ema(close, 12) - _ema(close, 26)
    signal = _ema(macd_line, 9)
    hist = macd_line - signal
    return macd_line, signal, hist


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

def _parse_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _strictness_profile(level) -> Dict[str, Any]:
    labels = ["very loose", "loose", "normal", "strict", "very strict"]
    try:
        idx = int(level)
    except Exception:
        txt = str(level or "normal").strip().lower()
        idx = labels.index(txt) if txt in labels else 2
    idx = max(0, min(4, idx))
    return {
        "index": idx,
        "label": labels[idx],
        "required_confirmations": [1, 2, 3, 4, 5][idx],
        "min_score": [45, 55, 65, 75, 85][idx],
    }


def _next_earnings_days(symbol: str) -> Optional[int]:
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        ed = getattr(tk, "earnings_dates", None)
        if ed is None or getattr(ed, "empty", False):
            return None
        idx_obj = getattr(ed, "index", None)
        idx = list(idx_obj) if idx_obj is not None else []
        for item in idx:
            try:
                dt = pd.Timestamp(item).date()
                return (dt - date.today()).days
            except Exception:
                continue
        for col in ("Earnings Date", "earningsDate"):
            if col in getattr(ed, "columns", []):
                vals = ed[col].dropna().tolist()
                if vals:
                    try:
                        dt = pd.to_datetime(vals[0]).date()
                        return (dt - date.today()).days
                    except Exception:
                        pass
    except Exception:
        return None
    return None


def _squeeze_state(df: pd.DataFrame, period: int = 20) -> Dict[str, bool]:
    close = df["Close"].astype(float)
    if len(close) < period + 2:
        return {"squeeze_on": False, "release_up": True, "release_down": True}
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    bb_upper = mid + 2.0 * std
    bb_lower = mid - 2.0 * std
    atr = _atr(df, period)
    kc_upper = mid + 1.5 * atr
    kc_lower = mid - 1.5 * atr
    idx = -1
    prev = -2 if len(close) >= period + 3 else -1
    squeeze_on = bool(bb_upper.iloc[idx] < kc_upper.iloc[idx] and bb_lower.iloc[idx] > kc_lower.iloc[idx])
    prev_on = bool(bb_upper.iloc[prev] < kc_upper.iloc[prev] and bb_lower.iloc[prev] > kc_lower.iloc[prev]) if prev != idx else squeeze_on
    release_up = bool((prev_on and close.iloc[idx] > bb_upper.iloc[idx]) or (not squeeze_on and close.iloc[idx] > bb_upper.iloc[idx]))
    release_down = bool((prev_on and close.iloc[idx] < bb_lower.iloc[idx]) or (not squeeze_on and close.iloc[idx] < bb_lower.iloc[idx]))
    return {"squeeze_on": squeeze_on, "release_up": release_up, "release_down": release_down}


def _iv_proxy(df: pd.DataFrame) -> Optional[int]:
    close = df["Close"].astype(float)
    if len(close) < 30:
        return None
    rets30 = [math.log(close.iloc[i] / close.iloc[i - 1]) for i in range(max(1, len(close) - 29), len(close)) if close.iloc[i - 1] > 0]
    rets90 = [math.log(close.iloc[i] / close.iloc[i - 1]) for i in range(max(1, len(close) - 89), len(close)) if close.iloc[i - 1] > 0] if len(close) >= 90 else rets30
    if not rets30 or not rets90:
        return None
    rv30 = math.sqrt(sum(x * x for x in rets30) / len(rets30) * 252) * 100
    rv90 = math.sqrt(sum(x * x for x in rets90) / len(rets90) * 252) * 100
    if not rv90:
        return None
    return int(max(0, min(100, round((rv30 / rv90) * 50))))


def _adx_dmi(df: pd.DataFrame, period: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move[(up_move > down_move) & (up_move > 0)]
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move[(down_move > up_move) & (down_move > 0)]

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean().replace(0, 1e-9)

    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)) * 100
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx, plus_di, minus_di


def _nearest_expiry(symbol: str, target_min: int = 28, target_max: int = 45) -> Optional[str]:
    try:
        import yfinance as yf

        opts = list(yf.Ticker(symbol).options or [])
        if not opts:
            return None
        today = date.today()
        candidates = []
        for e in opts:
            try:
                exp = datetime.strptime(e, "%Y-%m-%d").date()
            except Exception:
                continue
            dte = (exp - today).days
            if target_min <= dte <= target_max:
                candidates.append((abs(dte - (target_min + target_max) / 2), e))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        # fallback to closest future expiry
        future = []
        for e in opts:
            try:
                exp = datetime.strptime(e, "%Y-%m-%d").date()
            except Exception:
                continue
            dte = (exp - today).days
            if dte >= 0:
                future.append((dte, e))
        if future:
            future.sort(key=lambda x: x[0])
            return future[0][1]
    except Exception:
        return None
    return None


def _mid(row) -> Optional[float]:
    bid = _safe(row.get("bid"))
    ask = _safe(row.get("ask"))
    last = _safe(row.get("lastPrice"))
    if bid and ask and bid > 0 and ask > 0:
        return round((bid + ask) / 2, 2)
    return last


def _option_mid(symbol: str, expiry: str, strike: float, side: str) -> Optional[float]:
    try:
        import yfinance as yf

        tk = yf.Ticker(symbol)
        opts = list(tk.options or [])
        if not opts:
            return None
        if expiry not in opts:
            target = datetime.strptime(expiry, "%Y-%m-%d")
            nearest = min(opts, key=lambda e: abs((datetime.strptime(e, "%Y-%m-%d") - target).days))
            expiry = nearest
        chain = tk.option_chain(expiry)
        df = chain.calls if side == "call" else chain.puts
        row = df[(df["strike"] - float(strike)).abs() < 0.51]
        if row.empty:
            return None
        return _mid(row.iloc[0])
    except Exception:
        return None


def _strike_step(price: float) -> int:
    if price < 25:
        return 1
    if price < 100:
        return 1
    return 5


def _compute_pnr(long_strike: float, dte: int, atr: float) -> Optional[float]:
    if not (long_strike and dte and atr):
        return None
    return round(long_strike - (long_strike * dte * atr) / 2000, 2)


def _classify_setup(df: pd.DataFrame, symbol: str, expiry: Optional[str], width: int, mode: str = "core", controls: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    vol = df["Volume"].astype(float)

    price = float(close.iloc[-1])
    ema9 = float(_ema(close, 9).iloc[-1])
    ema21 = float(_ema(close, 21).iloc[-1])
    ema50 = float(_ema(close, 50).iloc[-1])
    ema200 = float(_ema(close, 200).iloc[-1]) if len(close) >= 200 else None
    rsi_series = _rsi(close, 14)
    rsi = float(rsi_series.iloc[-1])
    rsi_ema90 = float(_ema(rsi_series, 90).iloc[-1])
    rsi_diff = round(rsi - rsi_ema90, 1)
    macd_line, macd_signal, macd_hist = _macd(close)
    adx, plus_di, minus_di = _adx_dmi(df, 14)
    atr = float(_atr(df, 14).iloc[-1])
    iv_proxy = _iv_proxy(df)
    controls = controls or {}
    bias = str(controls.get("bias", "any") or "any").strip().lower()
    trade_type_filter = str(controls.get("trade_type", "any") or "any").strip().lower()
    min_rsi = controls.get("min_rsi")
    max_rsi = controls.get("max_rsi")
    avoid_earnings = _parse_bool(controls.get("avoid_earnings"), False)
    min_earn_days = controls.get("min_earn_days")
    try:
        min_earn_days = None if min_earn_days in (None, "", "None", "null") else int(min_earn_days)
    except Exception:
        min_earn_days = None
    width_override = str(controls.get("width", "") or "").strip().lower()
    strict = _strictness_profile(controls.get("strictness", 2))
    use_rsi = _parse_bool(controls.get("use_rsi"), True)
    use_dmi = _parse_bool(controls.get("use_dmi"), True)
    use_ema = _parse_bool(controls.get("use_ema"), True)
    use_macd = _parse_bool(controls.get("use_macd"), True)
    use_squeeze = _parse_bool(controls.get("use_squeeze"), False)

    macd_h = float(macd_hist.iloc[-1])
    macd_h_prev = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else macd_h
    adx_v = float(adx.iloc[-1])
    pdi = float(plus_di.iloc[-1])
    mdi = float(minus_di.iloc[-1])

    vol20 = float(vol.tail(20).mean()) if len(vol) >= 20 else float(vol.mean())
    vol5 = float(vol.tail(5).mean()) if len(vol) >= 5 else float(vol.mean())
    vol_ratio = round(vol5 / vol20, 2) if vol20 else 1.0

    atr_pct = round(atr / price * 100, 2) if price else 0
    red_reasons: List[str] = []
    if rsi > 80:
        red_reasons.append("RSI > 80")
    elif rsi < 20:
        red_reasons.append("RSI < 20")
    if rsi_diff > 20:
        red_reasons.append("RSI14 - EMA(RSI14,90) > 20")
    elif rsi_diff < -20:
        red_reasons.append("RSI14 - EMA(RSI14,90) < -20")
    red_signal = bool(red_reasons)
    step = _strike_step(price)
    exp = expiry or _nearest_expiry(symbol)
    dte = (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days if exp else 30
    pnr = _compute_pnr(price, max(dte, 1), atr)

    squeeze = _squeeze_state(df)
    strong_support = abs(price - ema21) / price <= 0.02 or abs(price - ema50) / price <= 0.02

    ed_days = _next_earnings_days(symbol)
    if avoid_earnings and ed_days is not None and -3 <= ed_days <= 21:
        return None
    if min_earn_days is not None and ed_days is not None and ed_days < min_earn_days:
        return None

    def _meets_rsi_window() -> bool:
        if min_rsi is not None and rsi < float(min_rsi):
            return False
        if max_rsi is not None and rsi > float(max_rsi):
            return False
        return True

    candidates: List[Dict[str, Any]] = []

    def _add_candidate(direction: str, trade_side: str, spread_kind: str, score_base: int, rationale_text: str,
                       ema_ok: bool, macd_ok: bool, dmi_ok: bool, rsi_ok: bool, squeeze_ok: bool, support_ok: bool) -> None:
        confirm_map = {"ema": ema_ok, "macd": macd_ok, "dmi": dmi_ok, "rsi": rsi_ok, "squeeze": squeeze_ok, "support": support_ok}
        enabled = {
            "ema": use_ema,
            "macd": use_macd,
            "dmi": use_dmi,
            "rsi": use_rsi,
            "squeeze": use_squeeze,
        }
        confirmations = [k for k, ok in confirm_map.items() if ok and enabled.get(k, False)]
        if len(confirmations) < strict["required_confirmations"]:
            return
        if bias in {"bullish", "bear", "bearish"} and direction != "BULLISH" and bias.startswith("bull"):
            return
        if bias in {"bearish", "bear"} and direction != "BEARISH":
            return
        if trade_type_filter in {"call", "calls"} and trade_side != "CALL":
            return
        if trade_type_filter in {"put", "puts"} and trade_side != "PUT":
            return
        score = score_base
        notes_local: List[str] = []
        if ema_ok:
            score += 30 if direction == "BULLISH" else 28
            notes_local.append("EMA stack aligned")
        if macd_ok:
            score += 25
            notes_local.append("MACD confirms momentum")
        elif use_macd:
            score += 8
            notes_local.append("MACD supportive")
        if dmi_ok:
            score += 15
            notes_local.append(f"ADX {adx_v:.1f} and DMI aligned")
        elif use_dmi:
            score += 5
            notes_local.append(f"ADX {adx_v:.1f} acceptable")
        if rsi_ok:
            score += 10
            notes_local.append(f"RSI {rsi:.1f} in range")
        if squeeze_ok:
            score += 6
            notes_local.append("Squeeze release")
        if support_ok:
            score += 4
            notes_local.append("Near EMA support/resistance")
        if vol_ratio >= 1.2:
            score += 5
            notes_local.append(f"Volume {vol_ratio:.1f}x 20d avg")
        if atr_pct >= 6:
            score -= 8
            notes_local.append(f"ATR {atr_pct:.1f}% high")
        elif atr_pct <= 2:
            score += 4
            notes_local.append(f"ATR {atr_pct:.1f}% contained")
        candidates.append({
            "direction": direction,
            "trade_side": trade_side,
            "spread_kind": spread_kind,
            "score": score,
            "notes": notes_local,
            "rationale": rationale_text,
            "confirmations": confirmations,
            "earn_days": ed_days,
            "earn_date": None if ed_days is None else (date.today() + timedelta(days=int(ed_days))).isoformat(),
        })

    # Bullish setups
    above_stack = price > ema9 > ema21 > ema50
    bullish_momo = macd_h > 0 and macd_h > macd_h_prev and pdi > mdi and adx_v >= (25 if strict["index"] >= 3 else 20)
    pullback_bull = price >= ema21 and ema9 >= ema21 >= ema50 and 45 <= rsi <= 62 and pdi >= mdi
    squeeze_bull = squeeze["release_up"]
    if bias in {"any", "bull", "bullish", "long"} and _meets_rsi_window():
        if above_stack:
            _add_candidate("BULLISH", "CALL", "BULL CALL", 40,
                           "Trend continuation with bullish EMA stack, MACD expansion and DMI confirmation.",
                           ema_ok=above_stack, macd_ok=(macd_h > 0 and macd_h > macd_h_prev) if use_macd else True,
                           dmi_ok=(pdi > mdi and adx_v >= (25 if strict["index"] >= 3 else 20)) if use_dmi else True,
                           rsi_ok=(rsi >= 52) if use_rsi else True, squeeze_ok=(squeeze_bull if use_squeeze else True), support_ok=strong_support)
        if pullback_bull:
            _add_candidate("BULLISH", "PUT", "BULL PUT", 30,
                           "Bullish pullback setup: price holding support, RSI in healthy pullback range, DMI still positive.",
                           ema_ok=ema9 >= ema21 >= ema50 if use_ema else True,
                           macd_ok=(macd_h > 0) if use_macd else True,
                           dmi_ok=(pdi >= mdi and adx_v >= 18) if use_dmi else True,
                           rsi_ok=(45 <= rsi <= 62) if use_rsi else True, squeeze_ok=(squeeze_bull if use_squeeze else True), support_ok=strong_support)

    # Bearish setups
    below_stack = price < ema9 < ema21 < ema50
    bearish_momo = macd_h < 0 and macd_h < macd_h_prev and mdi > pdi and adx_v >= (25 if strict["index"] >= 3 else 20)
    pullback_bear = price <= ema21 and ema9 <= ema21 <= ema50 and 38 <= rsi <= 55 and mdi >= pdi
    squeeze_bear = squeeze["release_down"]
    if bias in {"any", "bear", "bearish", "short"} and _meets_rsi_window():
        if below_stack:
            _add_candidate("BEARISH", "PUT", "BEAR PUT", 40,
                           "Trend continuation with bearish EMA stack, MACD expansion and DMI confirmation.",
                           ema_ok=below_stack, macd_ok=(macd_h < 0 and macd_h < macd_h_prev) if use_macd else True,
                           dmi_ok=(mdi > pdi and adx_v >= (25 if strict["index"] >= 3 else 20)) if use_dmi else True,
                           rsi_ok=(rsi <= 48) if use_rsi else True, squeeze_ok=(squeeze_bear if use_squeeze else True), support_ok=strong_support)
        if pullback_bear:
            _add_candidate("BEARISH", "CALL", "BEAR CALL", 30,
                           "Bearish pullback setup: price rejecting resistance, RSI weak, DMI still negative.",
                           ema_ok=ema9 <= ema21 <= ema50 if use_ema else True,
                           macd_ok=(macd_h < 0) if use_macd else True,
                           dmi_ok=(mdi >= pdi and adx_v >= 18) if use_dmi else True,
                           rsi_ok=(38 <= rsi <= 55) if use_rsi else True, squeeze_ok=(squeeze_bear if use_squeeze else True), support_ok=strong_support)

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x["score"], len(x.get("confirmations", []))), reverse=True)
    chosen = candidates[0]
    score = chosen["score"]
    notes = chosen["notes"]
    rationale = chosen["rationale"]
    direction = chosen["direction"]
    trade_side = chosen["trade_side"]
    spread_kind = chosen["spread_kind"]

    # Width choice: user can override, auto uses 1 for ETFs / low ATR, else 5
    if width not in (1, 5):
        if width_override in {"1", "1-wide", "1wide"}:
            width = 1
        elif width_override in {"5", "5-wide", "5wide"}:
            width = 5
        else:
            width = 1 if (price < 100 or atr_pct <= 2.5 or symbol in {"SPY", "QQQ", "IWM", "IVV", "DIA", "XLF", "XLK", "XLV", "XLE"}) else 5

    # Strike selection: close to ATM / slightly ITM
    inc = 1 if (width == 1 or price < 100) else 5
    if trade_side == "CALL":
        if direction == "BULLISH":
            long_strike = math.floor(price / inc) * inc
            if long_strike >= price:
                long_strike -= inc
            short_strike = long_strike + width
        else:
            short_strike = math.ceil(price / inc) * inc
            if short_strike <= price:
                short_strike += inc
            long_strike = short_strike + width
    else:
        if direction == "BULLISH":
            short_strike = math.ceil(price / inc) * inc
            if short_strike <= price:
                short_strike += inc
            long_strike = short_strike - width
        else:
            short_strike = math.floor(price / inc) * inc
            if short_strike >= price:
                short_strike -= inc
            long_strike = short_strike + width

    # Estimate vertical entry from live option chain if available.
    entry = None
    legs = {}
    if exp:
        if trade_side == "CALL":
            long_px = _option_mid(symbol, exp, long_strike, "call")
            short_px = _option_mid(symbol, exp, short_strike, "call")
            if long_px is not None and short_px is not None:
                entry = round(max(long_px - short_px, 0.01), 2)
                legs = {"long_mid": long_px, "short_mid": short_px}
        else:
            short_px = _option_mid(symbol, exp, short_strike, "put")
            long_px = _option_mid(symbol, exp, long_strike, "put")
            if long_px is not None and short_px is not None:
                entry = round(max(short_px - long_px, 0.01), 2)
                legs = {"short_mid": short_px, "long_mid": long_px}

    rr = None
    max_risk = None
    max_reward = None
    if entry is not None:
        if trade_side == "CALL":
            max_risk = round(entry * 100, 2)
            max_reward = round((width - entry) * 100, 2)
        else:
            max_risk = round((width - entry) * 100, 2)
            max_reward = round(entry * 100, 2)
        rr = round(max_reward / max_risk, 2) if max_risk and max_risk > 0 else None

    # PNR for the planned trade
    pnr_dte = max(dte, 1)
    pnr = _compute_pnr(long_strike, pnr_dte, atr)
    pnr_gap = round((price - pnr) / price * 100, 2) if pnr else None

    if iv_proxy is not None:
        if iv_proxy >= 60:
            structure_bias = "Credit spreads / iron condors favored"
        elif iv_proxy <= 40:
            structure_bias = "Debit spreads favored"
        else:
            structure_bias = "Either structure; wait for cleaner edge"
    else:
        structure_bias = "IV proxy unavailable"

    composite = {"score_delta": 0, "notes": [], "flags": [], "contrarian": False, "market_bias": "NEUTRAL", "market_note": "", "market_score": 0, "sector_bias": "NEUTRAL", "sector_note": "", "sector_score": 0, "sector_name": None, "sector_etf": None, "oi_bias": "NEUTRAL", "oi_note": "", "sr_bias": "NEUTRAL", "sr_note": ""}
    if mode == "composite":
        try:
            composite = composite_overlay(symbol, df, len(df) - 1, direction, price)
            score = int(min(100, max(0, score + int(composite.get("score_delta") or 0))))
            notes.extend([f"Composite: {n}" for n in composite.get("notes", [])])
            if composite.get("contrarian"):
                notes.append("Composite warning: trade is against market/sector/flow context")
        except Exception as _e:
            notes.append(f"Composite overlay error: {_e}")

    return {
        "symbol": symbol,
        "price": round(price, 2),
        "direction": direction,
        "trade_side": trade_side,
        "spread_kind": spread_kind,
        "width": width,
        "long_strike": round(long_strike, 2),
        "short_strike": round(short_strike, 2),
        "expiry": exp,
        "dte": dte,
        "entry_est": entry,
        "max_risk": max_risk,
        "max_reward": max_reward,
        "rr": rr,
        "pnr": pnr,
        "pnr_gap_pct": pnr_gap,
        "score": int(min(100, score)),
        "ema9": round(ema9, 2),
        "ema21": round(ema21, 2),
        "ema50": round(ema50, 2),
        "ema200": round(ema200, 2) if ema200 else None,
        "rsi": round(rsi, 1),
        "macd_hist": round(macd_h, 4),
        "adx": round(adx_v, 1),
        "plus_di": round(pdi, 1),
        "minus_di": round(mdi, 1),
        "atr": round(atr, 2),
        "atr_pct": round(atr_pct, 2),
        "red_signal": red_signal,
        "red_reasons": red_reasons,
        "iv_proxy": iv_proxy,
        "structure_bias": structure_bias,
        "vol_ratio": vol_ratio,
        "notes": notes[:5],
        "rationale": rationale,
        **legs,
    }


@maya_bp.route("/")
def maya_root():
    return redirect(url_for("maya_bp.scanner_page"))


@maya_bp.route("/scanner")
def scanner_page():
    return render_template("maya_scanner.html")


@maya_bp.route("/journal")
def journal_page():
    return render_template("maya_journal.html")


@maya_bp.route("/api/scanner")
def api_scanner():
    watchlist_id = request.args.get("watchlist_id", type=int)
    width = request.args.get("width", default=0, type=int)  # 0=auto
    min_score = request.args.get("min_score", default=60, type=int)
    mode = (request.args.get("mode", "core") or "core").strip().lower()
    controls = {
        "trade_type": (request.args.get("trade_type") or "any").strip().lower(),
        "bias": (request.args.get("bias") or "any").strip().lower(),
        "min_rsi": request.args.get("min_rsi", type=float),
        "max_rsi": request.args.get("max_rsi", type=float),
        "avoid_earnings": _parse_bool(request.args.get("avoid_earnings"), False),
        "min_earn_days": request.args.get("min_earn_days", type=int),
        "strictness": request.args.get("strictness", 2),
        "use_rsi": _parse_bool(request.args.get("use_rsi"), True),
        "use_dmi": _parse_bool(request.args.get("use_dmi"), True),
        "use_ema": _parse_bool(request.args.get("use_ema"), True),
        "use_macd": _parse_bool(request.args.get("use_macd"), True),
        "use_squeeze": _parse_bool(request.args.get("use_squeeze"), False),
        "width": request.args.get("width", "auto"),
    }
    symbols = _watchlist_symbols(watchlist_id)
    if not symbols:
        return jsonify({"watchlist_id": watchlist_id, "symbols": [], "results": [], "error": "No symbols found"})

    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    def _scan_symbol(sym: str):
        df = _history(sym)
        if df is None or len(df) < 90:
            return None, {"symbol": sym, "error": "insufficient price history"}
        try:
            return _classify_setup(df, sym, None, width, mode, controls), None
        except Exception as e:
            return None, {"symbol": sym, "error": str(e)[:140]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_scan_symbol, s): s for s in symbols}
        for fut in as_completed(futs):
            cand, err = fut.result()
            if cand:
                if cand["score"] >= min_score:
                    results.append(
                        attach_scanner_scores(
                            cand,
                            cand.get("symbol") or futs.get(fut) or "",
                            setup_type=cand.get("setup_type", "Maya"),
                            direction=cand.get("direction", "NEUTRAL"),
                            native_score=cand.get("score"),
                            trend_age=cand.get("trend_age"),
                            benchmark="SPY",
                        )
                    )
            elif err:
                errors.append(err)

    results.sort(key=lambda x: (x["score"], x["vol_ratio"], -x["atr_pct"]), reverse=True)

    # trim result fields for UI friendliness
    for r in results:
        r["notes_text"] = " · ".join(r.get("notes") or [])
        if r.get("entry_est") is None:
            # give a simple theoretical placeholder so the card still works
            if r["trade_side"] == "CALL":
                r["entry_est"] = round(max(min(r["width"] * 0.55, r["width"] - 0.1), 0.05), 2)
            else:
                r["entry_est"] = round(max(min(r["width"] * 0.45, r["width"] - 0.1), 0.05), 2)

    return jsonify({
        "watchlist_id": watchlist_id,
        "symbols": symbols,
        "count": len(results),
        "results": results,
        "errors": errors[:20],
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "logic": {
            "mode": mode,
            "ema": "EMA 9/21/50 stack",
            "momentum": "MACD histogram + ADX/DMI confirmation",
            "rsi": "healthy bullish range or pullback zone",
            "pnr": "PNR = long strike - (long strike × DTE × ATR) / 2000",
            "width": "1-wide for ETFs/low ATR, 5-wide for stronger movers unless manually overridden",
            "strictness": _strictness_profile(controls.get("strictness"))["label"],
        },
    })


@maya_bp.route("/api/journal")
def api_journal():
    status = request.args.get("status", "open").lower()
    con = _conn()
    try:
        if status == "all":
            rows = con.execute("SELECT * FROM trades ORDER BY entry_date DESC").fetchall()
        else:
            rows = con.execute("SELECT * FROM trades WHERE status=? ORDER BY entry_date DESC", (status.upper(),)).fetchall()
    finally:
        con.close()

    trades = []
    for row in rows:
        t = dict(row)
        try:
            live = _compute_live_pnl(row)
        except Exception as e:
            live = {"error": str(e)}
        # keep the most useful fields for the journal card/table
        trades.append({
            "id": t.get("id"),
            "symbol": t.get("symbol"),
            "trade_type": t.get("trade_type"),
            "entry_date": t.get("entry_date"),
            "expiry": t.get("expiry"),
            "long_strike": t.get("long_strike"),
            "short_strike": t.get("short_strike"),
            "entry_price": t.get("entry_price"),
            "current_price": live.get("current_mark"),
            "stock_price": live.get("spot"),
            "current_vertical_price": live.get("current_mark"),
            "current_pnl": live.get("unrealised_pnl"),
            "pnl_pct_max": live.get("pct_of_max_profit"),
            "pnr": live.get("pnr"),
            "pnr_upper": live.get("pnr_upper"),
            "pnr_breached": live.get("pnr_breached"),
            "dte": live.get("dte"),
            "outlook": live.get("outlook"),
            "action": live.get("action"),
            "action_reason": live.get("action_reason"),
            "urgency": live.get("urgency"),
            "probability_score": live.get("probability_score"),
            "recommendation": live.get("recommendation"),
            "rec_reason": live.get("rec_reason"),
            "suggestions": live.get("suggestions", []),
            "max_profit": live.get("max_profit"),
            "max_loss": live.get("max_loss"),
            "analytics": live,
            "status": t.get("status"),
            "notes": t.get("entry_reason") or t.get("exit_reason") or "",
        })

    # Portfolio-level summary from existing logic where possible
    open_count = sum(1 for t in trades if t["status"] == "OPEN")
    closed_count = sum(1 for t in trades if t["status"] == "CLOSED")
    projected_winners = sum(1 for t in trades if "WINNER" in (t.get("outlook") or "").upper())
    projected_losers = sum(1 for t in trades if "LOSER" in (t.get("outlook") or "").upper())
    current_pnl = round(sum((t.get("current_pnl") or 0) for t in trades if t["status"] == "OPEN"), 2)

    return jsonify({
        "summary": {
            "open_count": open_count,
            "closed_count": closed_count,
            "projected_winners": projected_winners,
            "projected_losers": projected_losers,
            "current_pnl": current_pnl,
        },
        "trades": trades,
    })
