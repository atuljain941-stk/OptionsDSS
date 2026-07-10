"""Pure pandas EMA5 body-breakdown short strategy helpers.

This file has no broker dependencies.  It can be used by UI scans, dry-runs,
and future backtests.  The EOD order workflow uses the latest completed candle
as the signal candle, then creates a next-session broker stop-entry order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional

import math

try:
    import pandas as pd
except Exception:  # pragma: no cover - import-time guard for environments without pandas
    pd = None  # type: ignore


@dataclass
class Ema5Signal:
    symbol: str
    candle_time: str
    open: float
    high: float
    low: float
    close: float
    ema5: float
    body_low: float
    body_ratio: float
    valid: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EodShortOrderPlan:
    symbol: str
    signal_time: str
    entry_stop: float
    entry_limit: Optional[float]
    stop_loss: float
    target: float
    risk_per_share: float
    reward_per_share: float
    rr: float
    qty: int
    risk_dollars: float
    body_ratio: float
    ema5: float
    signal_high: float
    signal_low: float
    signal_open: float
    signal_close: float
    notes: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _round_price(x: float) -> float:
    try:
        val = float(x)
    except Exception:
        val = 0.0
    if abs(val) >= 1:
        return round(val + 0.0, 2)
    return round(val + 0.0, 4)


def _coerce_ohlc_df(rows: Any):
    if pd is None:
        raise RuntimeError("pandas is required for the EMA5 signal engine")
    df = pd.DataFrame(rows or [])
    if df.empty:
        return df
    rename = {}
    for c in list(df.columns):
        lc = str(c).strip().lower()
        if lc in {"datetime", "date", "time", "timestamp"}:
            rename[c] = "datetime"
        elif lc in {"open", "o"}:
            rename[c] = "open"
        elif lc in {"high", "h"}:
            rename[c] = "high"
        elif lc in {"low", "l"}:
            rename[c] = "low"
        elif lc in {"close", "c", "last"}:
            rename[c] = "close"
        elif lc in {"volume", "vol", "v"}:
            rename[c] = "volume"
    if rename:
        df = df.rename(columns=rename)
    required = ["open", "high", "low", "close"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"OHLC data missing required column: {col}")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "datetime" not in df.columns:
        df["datetime"] = list(range(len(df)))
    df = df.dropna(subset=required).copy()
    if not df.empty:
        # Preserve provider order if datetime parse fails, but normally sort by time.
        try:
            sort_key = pd.to_datetime(df["datetime"], errors="coerce")
            if sort_key.notna().any():
                df = df.assign(_sort_dt=sort_key).sort_values("_sort_dt").drop(columns=["_sort_dt"])
        except Exception:
            pass
    return df.reset_index(drop=True)


def add_ema5(rows: Any):
    """Return OHLC DataFrame with ema5 and body_ratio columns."""
    df = _coerce_ohlc_df(rows)
    if df.empty:
        return df
    df["ema5"] = df["close"].ewm(span=5, adjust=False).mean()
    rng = (df["high"] - df["low"]).replace(0, float("nan"))
    df["body_ratio"] = (df["close"] - df["open"]).abs() / rng
    return df


def evaluate_latest_signal(symbol: str, rows: Any, body_ratio_threshold: float = 0.5) -> Ema5Signal:
    """Evaluate whether the latest completed candle is a valid signal candle.

    Signal candle rule:
      low > EMA5 AND abs(close-open)/(high-low) >= threshold.
    """
    sym = str(symbol or "").strip().upper()
    df = add_ema5(rows)
    if df.empty or len(df) < 5:
        return Ema5Signal(sym, "", 0, 0, 0, 0, 0, 0, 0, False, "Need at least 5 completed candles")
    row = df.iloc[-1]
    o = float(row["open"]); h = float(row["high"]); l = float(row["low"]); c = float(row["close"])
    ema = float(row["ema5"])
    body_ratio = float(row.get("body_ratio", 0) or 0)
    body_low = min(o, c)
    candle_time = str(row.get("datetime", ""))
    if not all(math.isfinite(x) for x in [o, h, l, c, ema]):
        return Ema5Signal(sym, candle_time, o, h, l, c, ema, body_low, body_ratio, False, "Invalid OHLC/EMA values")
    if l <= ema:
        return Ema5Signal(sym, candle_time, o, h, l, c, ema, body_low, body_ratio, False, "Signal low is at or below EMA5")
    if body_ratio < float(body_ratio_threshold or 0.5):
        return Ema5Signal(sym, candle_time, o, h, l, c, ema, body_low, body_ratio, False, "Body ratio below threshold")
    return Ema5Signal(sym, candle_time, o, h, l, c, ema, body_low, body_ratio, True, "Valid signal candle")


def build_eod_short_plan(
    signal: Ema5Signal,
    *,
    risk_budget: float = 100.0,
    rr: float = 3.0,
    entry_order_type: str = "STOP_LIMIT",
    stop_limit_buffer_pct: float = 0.10,
    max_qty: int = 1000,
) -> Optional[EodShortOrderPlan]:
    """Build a broker-resident short stop-entry + OCO plan from a signal candle.

    For the EOD variant the parent entry stop is the signal body low.  If price
    trades below that level next session, Schwab can trigger the short entry and
    activate the child OCO exit orders.
    """
    if not signal or not signal.valid:
        return None
    entry_stop = _round_price(signal.body_low)
    stop_loss = _round_price(signal.high)
    risk_per_share = stop_loss - entry_stop
    if not math.isfinite(risk_per_share) or risk_per_share <= 0:
        return None
    reward_per_share = risk_per_share * float(rr or 3.0)
    target = _round_price(entry_stop - reward_per_share)
    risk = max(0.0, float(risk_budget or 0.0))
    qty = int(math.floor(risk / risk_per_share)) if risk > 0 else 0
    qty = max(0, min(int(max_qty or 0) if max_qty else qty, qty))
    if qty < 1:
        notes = f"Risk/share {risk_per_share:.2f} exceeds risk budget {risk:.2f}; qty would be 0"
    else:
        notes = "Broker stop-entry version: enters if price trades below body low next session."
    entry_limit = None
    if str(entry_order_type or "STOP_LIMIT").upper() == "STOP_LIMIT":
        # For a sell-short stop-limit, limit below stop allows limited slippage.
        entry_limit = _round_price(entry_stop * (1.0 - max(0.0, float(stop_limit_buffer_pct or 0.0)) / 100.0))
    return EodShortOrderPlan(
        symbol=signal.symbol,
        signal_time=signal.candle_time,
        entry_stop=entry_stop,
        entry_limit=entry_limit,
        stop_loss=stop_loss,
        target=target,
        risk_per_share=_round_price(risk_per_share),
        reward_per_share=_round_price(reward_per_share),
        rr=float(rr or 3.0),
        qty=qty,
        risk_dollars=_round_price(qty * risk_per_share),
        body_ratio=float(signal.body_ratio),
        ema5=_round_price(signal.ema5),
        signal_high=_round_price(signal.high),
        signal_low=_round_price(signal.low),
        signal_open=_round_price(signal.open),
        signal_close=_round_price(signal.close),
        notes=notes,
    )


def simple_backtest_short_stop_entry(rows: Any, *, body_ratio_threshold: float = 0.5, rr: float = 3.0) -> Dict[str, Any]:
    """Small offline simulation for the broker stop-entry variant.

    If a signal occurs on bar i, the entry stop is evaluated using bar i+1.  If
    next bar trades at or below entry, the model assumes entry at entry_stop.
    Stop/target are then evaluated on that same/next bars using OHLC only.
    This is intentionally conservative and for preflight only, not tick-accurate.
    """
    df = add_ema5(rows)
    if df.empty or len(df) < 7:
        return {"trades": 0, "win_rate": 0, "avg_r": 0, "equity_curve": []}
    trades: List[Dict[str, Any]] = []
    i = 4
    while i < len(df) - 1:
        sig = evaluate_latest_signal("TEST", df.iloc[: i + 1].to_dict("records"), body_ratio_threshold)
        if not sig.valid:
            i += 1; continue
        plan = build_eod_short_plan(sig, risk_budget=100.0, rr=rr, entry_order_type="STOP")
        if not plan or plan.qty < 1:
            i += 1; continue
        entered = False; exit_r = 0.0; exit_idx = None; outcome = "expired"
        for j in range(i + 1, len(df)):
            bar = df.iloc[j]
            if not entered:
                if float(bar["low"]) <= plan.entry_stop:
                    entered = True
                else:
                    # EOD mode parent order is DAY; if not touched next bar, expire.
                    break
            if entered:
                hit_stop = float(bar["high"]) >= plan.stop_loss
                hit_target = float(bar["low"]) <= plan.target
                if hit_stop and hit_target:
                    # Ambiguous OHLC ordering; count stop first to stay conservative.
                    exit_r = -1.0; outcome = "stop"; exit_idx = j; break
                if hit_stop:
                    exit_r = -1.0; outcome = "stop"; exit_idx = j; break
                if hit_target:
                    exit_r = float(rr or 3.0); outcome = "target"; exit_idx = j; break
        if entered and exit_idx is None:
            last_close = float(df.iloc[-1]["close"])
            exit_r = (plan.entry_stop - last_close) / max(plan.risk_per_share, 0.0001)
            outcome = "open_mark"
            exit_idx = len(df) - 1
        if entered:
            trades.append({"signal_idx": i, "exit_idx": exit_idx, "r": exit_r, "outcome": outcome})
            i = max(i + 1, int(exit_idx or i) + 1)
        else:
            i += 1
    eq = []
    total = 0.0
    for t in trades:
        total += float(t.get("r", 0.0))
        eq.append(round(total, 4))
    wins = [t for t in trades if float(t.get("r", 0.0)) > 0]
    return {
        "trades": len(trades),
        "win_rate": round(100.0 * len(wins) / len(trades), 2) if trades else 0.0,
        "avg_r": round(sum(float(t.get("r", 0.0)) for t in trades) / len(trades), 4) if trades else 0.0,
        "equity_curve": eq,
        "outcomes": trades[-50:],
    }
