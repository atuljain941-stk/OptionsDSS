"""
indicator_engines.py
--------------------
Python ports of your three TradingView indicators, so they can run against
live tastytrade data inside oiapp instead of TradingView's Pine sandbox.

  1. AVWAPEngine        -> ports "Quantgym-AutoAVWAP" (anchored VWAP + bands)
  2. GEXEngine           -> ports "GEX Levels & Gamma Value Table"
                            (your Pine script takes GEX numbers as manual
                            inputs computed elsewhere in your OI App stack;
                            this engine does the actual computation from a
                            live option chain + greeks, which is what your
                            OI App's GEX Plan tab already does — wire this
                            to reuse that same logic if you have it, or use
                            this as the reference implementation)
  3. VolQuantEngine      -> ports "UAE - Trend/Vol Analyzer v4"
                            (EMA fast/slow, MACD hist, ADX regime, vol-amp)

All three take a pandas DataFrame of OHLCV bars (from tastytrade candles or
your existing market_data.db) and/or an option chain snapshot, and return
plain dicts — easy to serialize to JSON for the Flask blueprint / frontend
chart, and structurally similar to what oiapp_scanners_market_structure.py
already does (_ema, _rsi, _atr helpers) so you can merge these in directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# =====================================================================
# 1. AVWAP ENGINE  (Anchored VWAP + upper/mid/lower bands)
# =====================================================================
class AVWAPEngine:
    """
    Anchored VWAP from a chosen anchor (session open, week open, or a
    specific timestamp/swing), with standard-deviation bands — mirrors
    the "AVWAP Levels" panel in your screenshot (Price / Upper / Mid /
    Lower AVWAP).
    """

    def __init__(self, band_mult: float = 1.0):
        self.band_mult = band_mult

    @staticmethod
    def _session_anchor_index(df: pd.DataFrame, session_start: dtime = dtime(9, 30)) -> pd.Timestamp:
        """Find the timestamp of the most recent session open at/before the last bar."""
        idx = df.index
        today = idx[-1].date()
        anchor = pd.Timestamp.combine(today, session_start).tz_localize(idx.tz) if idx.tz else \
            pd.Timestamp.combine(today, session_start)
        candidates = idx[idx <= idx[-1]]
        same_day = candidates[candidates.date == today] if hasattr(candidates, "date") else candidates
        return same_day[0] if len(same_day) else idx[0]

    def compute(self, df: pd.DataFrame, anchor: Optional[pd.Timestamp] = None) -> Dict:
        """
        df: DataFrame with columns [open, high, low, close, volume], indexed by timestamp.
        anchor: explicit anchor timestamp; defaults to today's session open.
        """
        if df.empty:
            return {"status": "no_data"}

        anchor = anchor or self._session_anchor_index(df)
        window = df[df.index >= anchor].copy()
        if window.empty:
            window = df.copy()

        typical = (window["high"] + window["low"] + window["close"]) / 3.0
        vol = window["volume"].replace(0, np.nan)
        cum_vol = vol.cumsum()
        cum_pv = (typical * vol).cumsum()
        avwap = cum_pv / cum_vol

        # Volume-weighted variance around AVWAP for bands
        variance = ((typical - avwap) ** 2 * vol).cumsum() / cum_vol
        stdev = np.sqrt(variance)

        # The most recent bar is often still-forming with volume=0 (yfinance
        # hasn't closed it yet), which makes iloc[-1] NaN even though every
        # prior bar computed fine. Forward-fill so we read the last *valid*
        # cumulative value instead of blindly trusting the very last row.
        avwap = avwap.ffill()
        stdev = stdev.ffill()

        last_avwap = float(avwap.iloc[-1]) if not avwap.empty and not math.isnan(avwap.iloc[-1]) else None
        last_std = float(stdev.iloc[-1]) if not stdev.empty and not math.isnan(stdev.iloc[-1]) else 0.0
        price = float(df["close"].iloc[-1])

        return {
            "price": round(price, 2),
            "avwap_mid": round(last_avwap, 2) if last_avwap else None,
            "avwap_upper": round(last_avwap + self.band_mult * last_std, 2) if last_avwap else None,
            "avwap_lower": round(last_avwap - self.band_mult * last_std, 2) if last_avwap else None,
            "anchor_time": str(anchor),
            "bars_used": len(window),
        }

    def compute_series(self, df: pd.DataFrame, anchor: Optional[pd.Timestamp] = None) -> Dict:
        """Full time-series version (every bar, not just the latest) for
        plotting AVWAP + bands as actual lines on the chart."""
        if df.empty:
            return {"mid": [], "upper": [], "lower": []}

        anchor = anchor or self._session_anchor_index(df)
        window = df[df.index >= anchor].copy()
        if window.empty:
            window = df.copy()

        typical = (window["high"] + window["low"] + window["close"]) / 3.0
        vol = window["volume"].replace(0, np.nan)
        cum_vol = vol.cumsum().ffill()
        cum_pv = (typical * vol).cumsum().ffill()
        avwap = (cum_pv / cum_vol).ffill()
        variance = (((typical - avwap) ** 2 * vol).cumsum() / cum_vol).ffill()
        stdev = np.sqrt(variance).fillna(0)

        upper = avwap + self.band_mult * stdev
        lower = avwap - self.band_mult * stdev

        def _series(s):
            return [{"time": int(ts.timestamp()), "value": round(float(v), 4)}
                    for ts, v in s.items() if not math.isnan(v)]

        return {"mid": _series(avwap), "upper": _series(upper), "lower": _series(lower)}


# =====================================================================
# TECHNICAL OVERLAY ENGINE — EMAs, RSI/RSIDiff90 (exact formula from
# oiapp.scanners.scanner_builder), MACD, ADX — as full time series for
# chart overlays, not just latest-bar snapshots.
# =====================================================================
class TechnicalSeriesEngine:
    """
    Ports the exact indicator math from oiapp/scanners/scanner_builder.py
    (_ema, _rsi, _macd, and the rsi_diff_90 = rsi14 - ema(rsi14, 90)
    formula used throughout your scanner query language) so the chart
    shows the *same* numbers your scanners and AI Hub already use —
    not a re-derived approximation.
    """

    @staticmethod
    def _ema(series: pd.Series, n: int) -> pd.Series:
        return series.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean().replace(0, 1e-9)
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _macd(self, close: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
        macd_line = self._ema(close, 12) - self._ema(close, 26)
        signal = self._ema(macd_line, 9)
        hist = macd_line - signal
        return macd_line, signal, hist

    @staticmethod
    def _true_range(df: pd.DataFrame) -> pd.Series:
        prev_close = df["close"].shift(1)
        return pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)

    def _adx(self, df: pd.DataFrame, length: int = 14) -> pd.Series:
        up_move = df["high"].diff()
        down_move = -df["low"].diff()
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        tr = self._true_range(df)
        atr = tr.ewm(alpha=1 / length, adjust=False).mean()
        plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / length, adjust=False).mean() / atr
        minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / length, adjust=False).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.ewm(alpha=1 / length, adjust=False).mean()

    @staticmethod
    def _to_series(s: pd.Series) -> List[Dict]:
        return [{"time": int(ts.timestamp()), "value": round(float(v), 4)}
                for ts, v in s.items() if not (v is None or (isinstance(v, float) and math.isnan(v)))]

    def compute(self, df: pd.DataFrame) -> Dict:
        if df.empty or len(df) < 30:
            return {"status": "insufficient_data"}

        close = df["close"]
        rsi14 = self._rsi(close, 14)
        rsi_ema90 = self._ema(rsi14, 90)
        rsi_diff_90 = rsi14 - rsi_ema90
        macd_line, macd_signal, macd_hist = self._macd(close)
        adx14 = self._adx(df, 14)

        return {
            "ema5": self._to_series(self._ema(close, 5)),
            "ema9": self._to_series(self._ema(close, 9)),
            "ema20": self._to_series(self._ema(close, 20)),
            "ema50": self._to_series(self._ema(close, 50)),
            "ema200": self._to_series(self._ema(close, 200)),
            "rsi14": self._to_series(rsi14),
            "rsi_ema90": self._to_series(rsi_ema90),
            "rsi_diff_90": self._to_series(rsi_diff_90),
            "macd": self._to_series(macd_line),
            "macd_signal": self._to_series(macd_signal),
            "macd_hist": self._to_series(macd_hist),
            "adx14": self._to_series(adx14),
        }


# =====================================================================
# COMPOSITE SIGNAL ENGINE — "AVWAP Navigator" abnormal-flow detector.
#
# Philosophy: no single module's opinion is a signal. AVWAP (price/volume/
# value-zone structure), GEX (options positioning/dealer gamma pressure),
# and VolQuant (volatility regime shift) each independently vote BULLISH,
# BEARISH, or NEUTRAL, and each independently decides whether *it* is
# currently seeing abnormal conditions. A composite signal only fires when
# at least 2 of the 3 modules agree on direction AND are each individually
# flagging abnormal conditions — not just any directional lean. This is
# what turns three side-by-side panels into one "abnormal flow detected"
# read, the way the AVWAP Navigator philosophy describes.
# =====================================================================
class CompositeSignalEngine:

    def __init__(self, volume_surge_mult: float = 1.5, vol_expansion_threshold: float = 1.15,
                 cross_lookback: int = 5):
        self.volume_surge_mult = volume_surge_mult
        self.vol_expansion_threshold = vol_expansion_threshold
        self.cross_lookback = cross_lookback

    # ---------------- Module 1: AVWAP (price/volume/value-zone) ----------------
    def _avwap_vote(self, df: pd.DataFrame, avwap_series: Dict) -> Dict:
        mid = avwap_series.get("mid") or []
        upper = avwap_series.get("upper") or []
        lower = avwap_series.get("lower") or []
        if len(mid) < self.cross_lookback + 1 or df.empty:
            return {"direction": "NEUTRAL", "abnormal": False, "reason": "insufficient AVWAP history"}

        price = float(df["close"].iloc[-1])
        mid_now = mid[-1]["value"]
        upper_now = upper[-1]["value"] if upper else None
        lower_now = lower[-1]["value"] if lower else None

        # Fresh cross of the mid AVWAP within the lookback window = shift in
        # demand/supply balance, not just "currently above/below" (which is
        # mostly noise intraday).
        recent_price = df["close"].tail(self.cross_lookback + 1)
        recent_mid = [p["value"] for p in mid[-(self.cross_lookback + 1):]]
        crossed_down = recent_price.iloc[0] >= recent_mid[0] and price < mid_now
        crossed_up = recent_price.iloc[0] <= recent_mid[0] and price > mid_now

        # Volume surge: last bar vs rolling 20-bar average.
        vol = df["volume"].replace(0, np.nan)
        recent_vol = float(vol.iloc[-1]) if not math.isnan(vol.iloc[-1]) else 0.0
        avg_vol = float(vol.tail(21).iloc[:-1].mean()) if len(vol) > 21 else float(vol.mean() or 1)
        volume_surge = avg_vol > 0 and recent_vol >= avg_vol * self.volume_surge_mult

        outside_band = (upper_now is not None and price > upper_now) or \
                        (lower_now is not None and price < lower_now)

        direction = "BEARISH" if (crossed_down or (lower_now and price < lower_now)) else \
                    "BULLISH" if (crossed_up or (upper_now and price > upper_now)) else "NEUTRAL"

        abnormal = bool((crossed_down or crossed_up) and volume_surge) or outside_band
        reason = []
        if crossed_down: reason.append("fresh cross below AVWAP mid")
        if crossed_up: reason.append("fresh cross above AVWAP mid")
        if volume_surge: reason.append(f"volume {recent_vol/avg_vol:.1f}x average")
        if outside_band: reason.append("price outside AVWAP bands")

        return {"direction": direction, "abnormal": abnormal, "reason": "; ".join(reason) or "no abnormal shift"}

    # ---------------- Module 2: GEX (options flow / dealer gamma pressure) ----------------
    def _gex_vote(self, gex: Dict) -> Dict:
        if not gex or gex.get("status"):
            return {"direction": "NEUTRAL", "abnormal": False, "reason": gex.get("status", "no GEX data")}

        spot = gex.get("spot")
        flip = gex.get("gamma_flip")
        regime = gex.get("regime", "")
        is_negative = "NEGATIVE" in regime

        if spot is None or flip is None:
            return {"direction": "NEUTRAL", "abnormal": False, "reason": "missing spot/gamma flip"}

        below_flip = spot < flip
        # Negative gamma regime = dealers hedge in the same direction as the
        # move (trend-amplifying), which is the "abnormal / dangerous" state
        # this module is meant to flag — not the regime alone, but regime
        # combined with which side of the flip price sits on.
        if is_negative and below_flip:
            return {"direction": "BEARISH", "abnormal": True,
                    "reason": f"negative gamma below flip ({spot:.2f} < {flip:.2f}) — downside amplification risk"}
        if is_negative and not below_flip:
            return {"direction": "BULLISH", "abnormal": True,
                    "reason": f"negative gamma above flip ({spot:.2f} > {flip:.2f}) — upside amplification risk"}
        # Positive gamma = dealers dampen moves (mean-reversion/pinning) —
        # this is the normal/stable state, not abnormal.
        return {"direction": "NEUTRAL", "abnormal": False,
                "reason": "positive gamma regime — dealer hedging dampens moves"}

    # ---------------- Module 3: VolQuant (volatility regime shift) ----------------
    def _volquant_vote(self, vq: Dict) -> Dict:
        if not vq or vq.get("status"):
            return {"direction": "NEUTRAL", "abnormal": False, "reason": vq.get("status", "no VolQuant data")}

        regime = vq.get("regime", "SIDEWAYS")
        vol_amp = vq.get("vol_amplification", 1.0)
        is_trending = vq.get("is_trending", False)

        direction = "BULLISH" if "BULL" in regime else "BEARISH" if "BEAR" in regime else "NEUTRAL"
        # Abnormal = volatility is actively expanding (not just "trending"),
        # which is the regime-shift signature this module exists to catch.
        abnormal = bool(is_trending and vol_amp >= self.vol_expansion_threshold)
        reason = f"vol expansion {vol_amp:.2f}x" if abnormal else "no volatility expansion"

        return {"direction": direction, "abnormal": abnormal, "reason": reason}

    # ---------------- Composite ----------------
    def compute(self, df: pd.DataFrame, avwap_series: Dict, gex: Dict, volquant: Dict) -> Dict:
        avwap_v = self._avwap_vote(df, avwap_series)
        gex_v = self._gex_vote(gex)
        vq_v = self._volquant_vote(volquant)

        modules = {"avwap": avwap_v, "gex": gex_v, "volquant": vq_v}
        agreeing = {name: m for name, m in modules.items()
                    if m["abnormal"] and m["direction"] in ("BULLISH", "BEARISH")}

        bullish_votes = [n for n, m in agreeing.items() if m["direction"] == "BULLISH"]
        bearish_votes = [n for n, m in agreeing.items() if m["direction"] == "BEARISH"]

        if len(bearish_votes) >= 2:
            signal, tier, confirming = "SELL", ("STRONG" if len(bearish_votes) == 3 else "CONFIRMED"), bearish_votes
        elif len(bullish_votes) >= 2:
            signal, tier, confirming = "BUY", ("STRONG" if len(bullish_votes) == 3 else "CONFIRMED"), bullish_votes
        else:
            signal, tier, confirming = "NONE", None, []

        return {
            "signal": signal,
            "tier": tier,
            "confirming_modules": confirming,
            "modules": modules,
        }


# =====================================================================
# 2. GEX / OPTION CHAIN ENGINE  (Gamma Flip, Pin, Max Pain, OI Walls)
# =====================================================================
@dataclass
class ChainRow:
    strike: float
    call_oi: float
    put_oi: float
    call_gamma: float   # per-contract gamma from greeks stream
    put_gamma: float


class GEXEngine:
    """
    Computes dealer gamma exposure levels from a live option chain snapshot
    (strikes + open interest + greeks). Feed it a list of ChainRow built
    from tastytrade's option chain + DXLink Greeks events.

    Convention: dealers are assumed short calls / short puts sold to
    customers (standard retail-flow assumption used by most public GEX
    tools) -> dealer gamma = -(call_oi*call_gamma) + (put_oi*put_gamma),
    scaled by contract multiplier and spot^2/100 to express $ per 1% move.
    Adjust the sign convention if your existing OI App uses a different
    assumption — keep it consistent with what you already have.
    """

    def __init__(self, contract_multiplier: int = 100):
        self.multiplier = contract_multiplier

    def compute(self, spot: float, rows: List[ChainRow]) -> Dict:
        if not rows:
            return {"status": "no_data"}

        strikes = sorted(rows, key=lambda r: r.strike)

        total_gex = 0.0
        gross_gex = 0.0
        per_strike_gex: List[Tuple[float, float]] = []

        for r in strikes:
            dealer_gamma = (-r.call_oi * r.call_gamma + r.put_oi * r.put_gamma)
            dollar_gex = dealer_gamma * self.multiplier * (spot ** 2) / 100.0
            per_strike_gex.append((r.strike, dollar_gex))
            total_gex += dollar_gex
            gross_gex += abs(dollar_gex)

        # Gamma flip: strike nearest zero cumulative GEX crossing, scanning
        # from low to high strike and finding the sign change of cum sum.
        cum = 0.0
        gamma_flip = None
        prev_strike, prev_cum = None, None
        for strike, g in per_strike_gex:
            cum += g
            if prev_cum is not None and (prev_cum < 0 <= cum or prev_cum > 0 >= cum):
                # linear interpolate between prev_strike and strike
                span = strike - prev_strike
                gamma_flip = prev_strike + span * (0 - prev_cum) / (cum - prev_cum) if cum != prev_cum else strike
            prev_strike, prev_cum = strike, cum
        if gamma_flip is None and per_strike_gex:
            gamma_flip = per_strike_gex[len(per_strike_gex) // 2][0]

        # Pin strike = strike with max |GEX|
        pin_strike = max(per_strike_gex, key=lambda x: abs(x[1]))[0]

        # Max pain = strike minimizing total option payout at expiry
        max_pain = self._max_pain(strikes)

        # OI walls: top 3 call OI strikes above spot, top 3 put OI strikes below spot
        call_walls = sorted(
            [r for r in strikes if r.strike >= spot], key=lambda r: r.call_oi, reverse=True
        )[:3]
        put_walls = sorted(
            [r for r in strikes if r.strike <= spot], key=lambda r: r.put_oi, reverse=True
        )[:3]

        gex_ratio = (total_gex / gross_gex) if gross_gex else 0.0
        regime = "POSITIVE (mean-reversion)" if total_gex >= 0 else "NEGATIVE (trend-amplifying)"

        return {
            "spot": round(spot, 2),
            "total_gex": round(total_gex, 0),
            "gross_gex": round(gross_gex, 0),
            "gex_ratio": round(gex_ratio, 3),
            "regime": regime,
            "gamma_flip": round(gamma_flip, 2) if gamma_flip else None,
            "pin_strike": round(pin_strike, 2) if pin_strike else None,
            "max_pain": round(max_pain, 2) if max_pain else None,
            "call_walls": [{"strike": r.strike, "oi": r.call_oi} for r in call_walls],
            "put_walls": [{"strike": r.strike, "oi": r.put_oi} for r in put_walls],
        }

    @staticmethod
    def _max_pain(rows: List[ChainRow]) -> Optional[float]:
        if not rows:
            return None
        strikes = [r.strike for r in rows]
        best_strike, best_payout = None, None
        for candidate in strikes:
            payout = 0.0
            for r in rows:
                # value of calls in the money at expiry priced at `candidate`
                payout += max(candidate - r.strike, 0) * r.call_oi
                payout += max(r.strike - candidate, 0) * r.put_oi
            if best_payout is None or payout < best_payout:
                best_payout, best_strike = payout, candidate
        return best_strike


# =====================================================================
# 3. VOLATILITY QUANT ENGINE  (regime oscillator, ported from UAE v4 pine)
# =====================================================================
class VolQuantEngine:
    """
    Port of UAE_TrendVol_Analyzer_v4.pine: EMA fast/slow MACD-style
    histogram, ADX-based trending/sideways regime, ATR-based vol
    amplification, and the Vol Score (z) / Vol Gradient / Persistence
    metrics shown in your Volquant panel.
    """

    TF_PARAMS = {
        "5min":  dict(fast=5, slow=13, roc=3, slope=4,  signal=2, adx_thr=18.0),
        "15min": dict(fast=7, slow=15, roc=4, slope=5,  signal=3, adx_thr=18.0),
        "1H":    dict(fast=8, slow=20, roc=5, slope=7,  signal=3, adx_thr=20.0),
        "4H":    dict(fast=8, slow=20, roc=5, slope=8,  signal=3, adx_thr=20.0),
        "Daily": dict(fast=8, slow=21, roc=6, slope=10, signal=3, adx_thr=20.0),
        "Weekly":dict(fast=8, slow=21, roc=7, slope=12, signal=3, adx_thr=20.0),
    }

    def __init__(self, timeframe: str = "5min", vol_mult: float = 1.5,
                 adx_len: int = 14, adx_smooth: int = 3):
        self.params = self.TF_PARAMS.get(timeframe, self.TF_PARAMS["5min"])
        self.vol_mult = vol_mult
        self.adx_len = adx_len
        self.adx_smooth = adx_smooth

    @staticmethod
    def _ema(s: pd.Series, n: int) -> pd.Series:
        return s.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _true_range(df: pd.DataFrame) -> pd.Series:
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr

    def _adx(self, df: pd.DataFrame) -> pd.Series:
        up_move = df["high"].diff()
        down_move = -df["low"].diff()
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        tr = self._true_range(df)
        atr = tr.ewm(alpha=1 / self.adx_len, adjust=False).mean()
        plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / self.adx_len, adjust=False).mean() / atr
        minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / self.adx_len, adjust=False).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.ewm(alpha=1 / self.adx_len, adjust=False).mean()
        return adx.ewm(span=self.adx_smooth, adjust=False).mean()

    def compute(self, df: pd.DataFrame) -> Dict:
        if df.empty or len(df) < max(self.params["slow"], self.adx_len) + 5:
            return {"status": "insufficient_data"}

        p = self.params
        fast_ema = self._ema(df["close"], p["fast"])
        slow_ema = self._ema(df["close"], p["slow"])
        macd_line = fast_ema - slow_ema
        signal_line = self._ema(macd_line, p["signal"])
        hist = macd_line - signal_line

        adx = self._adx(df)
        is_trending = adx.iloc[-1] >= p["adx_thr"]

        ema_slope = (slow_ema.iloc[-1] - slow_ema.iloc[-1 - p["slope"]]) / p["slope"] \
            if len(slow_ema) > p["slope"] else 0.0

        atr = self._true_range(df).ewm(alpha=1 / 14, adjust=False).mean()
        atr_base = self._ema(atr, p["slow"])
        vol_amp = 1.0 + self.vol_mult * ((atr.iloc[-1] / atr_base.iloc[-1]) - 1.0) if atr_base.iloc[-1] else 1.0

        # Vol Score (z): z-score of current ATR vs its own rolling distribution
        atr_window = atr.tail(100)
        vol_score_z = float((atr.iloc[-1] - atr_window.mean()) / atr_window.std()) if atr_window.std() else 0.0

        # Vol Gradient: rate of change of ATR (normalized)
        vol_gradient = float((atr.iloc[-1] - atr.iloc[-6]) / atr.iloc[-6]) if len(atr) > 6 and atr.iloc[-6] else 0.0

        # Persistence: how many of the last 10 bars had histogram same sign as now
        recent_hist = hist.tail(10)
        current_sign = np.sign(hist.iloc[-1])
        persistence = float((np.sign(recent_hist) == current_sign).mean()) if current_sign != 0 else 0.0

        if is_trending:
            regime = "BULL" if hist.iloc[-1] > 0 and ema_slope > 0 else "BEAR" if hist.iloc[-1] < 0 and ema_slope < 0 else "WEAK BULL" if hist.iloc[-1] > 0 else "WEAK BEAR"
        else:
            regime = "SIDEWAYS"

        return {
            "regime": regime,
            "is_trending": bool(is_trending),
            "adx": round(float(adx.iloc[-1]), 2),
            "macd": round(float(macd_line.iloc[-1]), 4),
            "signal": round(float(signal_line.iloc[-1]), 4),
            "histogram": round(float(hist.iloc[-1]), 4),
            "ema_slope": round(float(ema_slope), 5),
            "vol_amplification": round(float(vol_amp), 3),
            "vol_score_z": round(vol_score_z, 2),
            "vol_gradient": round(vol_gradient, 2),
            "persistence": round(persistence, 2),
        }

    def compute_series(self, df: pd.DataFrame) -> Dict:
        """Full time-series version of the VolQuant engine's own histogram
        + ATR-based vol-amplification, for a dedicated chart pane (matching
        the Volquant panel style: line + colored histogram) instead of only
        the latest-bar numbers card."""
        if df.empty or len(df) < max(self.params["slow"], self.adx_len) + 5:
            return {"histogram": [], "vol_amplification": [], "adx": []}

        p = self.params
        fast_ema = self._ema(df["close"], p["fast"])
        slow_ema = self._ema(df["close"], p["slow"])
        macd_line = fast_ema - slow_ema
        signal_line = self._ema(macd_line, p["signal"])
        hist = macd_line - signal_line
        adx = self._adx(df)

        atr = self._true_range(df).ewm(alpha=1 / 14, adjust=False).mean()
        atr_base = self._ema(atr, p["slow"])
        vol_amp = 1.0 + self.vol_mult * ((atr / atr_base) - 1.0)

        def _series(s):
            return [{"time": int(ts.timestamp()), "value": round(float(v), 4)}
                    for ts, v in s.items() if not (isinstance(v, float) and math.isnan(v))]

        return {
            "histogram": _series(hist),
            "vol_amplification": _series(vol_amp),
            "adx": _series(adx),
        }
