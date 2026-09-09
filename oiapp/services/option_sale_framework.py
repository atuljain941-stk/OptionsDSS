# oiapp/services/option_sale_framework.py
"""
Sequential options-selling evaluation framework, run in this exact
order per your specification:

  1. Market and sector regime
  2. Weekly trend
  3. Daily setup
  4. IV versus HV
  5. Expected move and option strikes
  6. Trade structure and risk
  7. Beta and correlation as position-sizing filters

DESIGN PRINCIPLE -- every stage runs, none are skipped, but each
stage's verdict FEEDS the next rather than the pipeline just running
seven independent checks in parallel and stapling the results together:
market/sector regime sets a directional lean that weekly/daily trend
get checked against (agreement vs conflict, not evaluated in a vacuum);
IV vs HV determines whether selling premium is even structurally
favored before strikes get picked; expected move directly sizes the
strike search in stage 5; stage 6 reuses that same strike search to
build and price the actual structure via greeks_strategy_scanner.py's
existing, tested strategy builders (not reimplemented here); and stage
7 is explicitly a SIZING filter, not a go/no-go gate -- beta and
correlation don't override everything upstream, they answer "how much"
given everything upstream already said "yes, structurally".

REUSED, NOT REBUILT (this is what makes stages 1, 4, 6, 7 fast to write
correctly -- verified, not guessed):
  - Stage 1: maya_composite_logic.py's _benchmark_regime()/_sector_regime()
    (EMA20/50 + MACD histogram regime classification, already proven)
  - Stage 4/5/6: greeks_strategy_scanner.py's _get_iv_metrics,
    _strike_universe, _leg_iv, and the full STRATEGY_BUILDERS set
    (strangle/straddle/iron_fly/jade_lizard/broken_wing_fly), including
    their real RR/POP/liquidity-gate math -- not reimplemented, called
    directly
  - Stage 7: services/fundamentals.py's get_beta()

GENUINELY NEW HERE:
  - Stage 2/3: weekly and daily trend/setup classification (EMA
    structure + RSI), built directly against scanner_builder.py's
    _history() (same price-data function the whole app's DSL scanner
    already relies on, not a separate fetch path)
  - Stage 7's correlation component: daily-return correlation to SPY
    and, if given, to a list of existing position symbols -- no
    correlation calculation existed anywhere in this app before this
  - The sequential orchestration and final verdict aggregation itself
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd


# ── Stage 1: Market and sector regime ───────────────────────────────────

def _stage_market_sector_regime(symbol: str) -> Dict[str, Any]:
    from ..scanners.maya_composite_logic import _benchmark_regime, _sector_regime
    from ..services.macro_regime import get_macro_regime, vix_extremes
    market = _benchmark_regime("SPY")
    sector = _sector_regime(symbol)
    # Combined lean: only call it a clean directional lean when market
    # and sector actually agree -- if they conflict (e.g. market bullish
    # but this symbol's own sector bearish), that conflict IS the
    # finding, not something to average away into a false "neutral".
    if market["bias"] == sector["bias"] and market["bias"] != "NEUTRAL":
        lean, verdict = market["bias"], "AGREE"
    elif market["bias"] == "NEUTRAL" or sector["bias"] == "NEUTRAL":
        lean, verdict = (market["bias"] if market["bias"] != "NEUTRAL" else sector["bias"]), "PARTIAL"
    else:
        lean, verdict = "CONFLICTED", "CONFLICT"

    # Macro is additive context, not blended into lean/agreement above --
    # see macro_regime.py's own docstring for why: yields/DXY/oil each
    # have a DIFFERENT directional meaning for equities than "price
    # trending up = bullish", so folding a macro score into the same
    # lean calculation the technical market/sector read uses would risk
    # quietly averaging away exactly the kind of disagreement worth
    # surfacing explicitly (e.g. "SPY technically bullish, but 10Y and
    # DXY both rising" is a real tension a premium seller should see,
    # not a number that nets to a false "still fine").
    try:
        macro = get_macro_regime()
    except Exception as e:
        macro = {"equity_headwind_score": 0, "equity_notes": [f"macro regime unavailable: {e}"]}
    macro_conflict = None
    headwind = macro.get("equity_headwind_score", 0)
    if lean == "BULLISH" and headwind >= 3:
        macro_conflict = f"Technical regime is bullish, but macro headwind score is {headwind} (yields/dollar both working against equities) -- a real tension, not netted away."
    elif lean == "BEARISH" and headwind <= -3:
        macro_conflict = f"Technical regime is bearish, but macro tailwind score is {headwind} (yields/dollar both easing) -- a real tension, not netted away."

    try:
        vix = vix_extremes()
    except Exception as e:
        vix = {"available": False, "note": f"VIX unavailable: {e}"}

    return {
        "market": market, "sector": sector, "combined_lean": lean, "agreement": verdict,
        "macro": macro, "macro_conflict": macro_conflict, "vix": vix,
        "note": f"Market {market['bias']} / Sector ({sector.get('sector')}) {sector['bias']} -> {verdict}",
    }


# ── Stage 2: Weekly trend / Stage 3: Daily setup ────────────────────────

def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _trend_classify(df: pd.DataFrame, label: str, min_atr_multiple: float = 1.0) -> Dict[str, Any]:
    """min_atr_multiple: minimum required gap between price/EMA13/EMA50
    (each pairwise), expressed in multiples of that symbol's own ATR(14)
    on this timeframe -- not a fixed percentage. A fixed percentage
    threshold treats a low-vol and high-vol name identically even though
    the same 1% gap means very different things for each; normalizing
    by the symbol's own ATR is the standard fix, reusing
    scanner_builder.py's own _atr_series() (the same implementation
    ATRCompression/SlopeATR/Keltner Channels already use, not a second
    one). This replaced an earlier fixed-0.5%-threshold version --
    verified that one's failure mode empirically (pure noise falsely
    classified as trending) before deciding a volatility-normalized
    threshold was the right fix, not just a nicer-sounding one.

    THRESHOLD VALIDATION -- what's actually been checked and what hasn't:

    A 30-seed test using i.i.d. noise around a FIXED mean (no drift at
    all, mean-reverting-flavored) came back 30/30 correctly CHOPPY at
    the 1.0x default.

    A separate, arguably more realistic sweep -- a zero-drift RANDOM
    WALK (cumulative noise, which can wander away from its start purely
    by chance the way real prices without genuine directional edge
    still can) against varying drift strengths and threshold values --
    showed real, worth-knowing sensitivity:

      drift/bar | thr=0.5 | thr=0.75 | thr=1.0 | thr=1.25 | thr=1.5
           0.0  |    20%  |    20%   |   13%   |    0%    |   0%     <- false positive rate on pure noise
           0.05 |    53%  |    47%   |   33%   |   27%    |  13%     <- weak real drift, detection rate
           0.10 |    73%  |    60%   |   53%   |   40%    |  33%
           0.20 |    87%  |    87%   |   73%   |   67%    |  60%
           0.30 |   100%  |    93%   |   93%   |   87%    |  80%
           0.50 |   100%  |   100%   |  100%   |  100%    | 100%

    (each cell: % of 15 random seeds classified UPTREND at that
    drift/threshold combination -- row 0.0 is the false-positive rate on
    a genuine null, later rows are detection rate on real drift)

    Reading this honestly: 1.0x has a real ~13% false-positive rate
    against a zero-drift random walk (not zero, contrary to what the
    single mean-reverting-noise test alone would suggest), and only
    catches a mild real trend (drift=0.05) about a third of the time.
    Raising the threshold reduces false positives but costs real
    detection of weaker trends; there is no threshold in this sweep that
    is free on both axes. 1.0x remains a reasonable middle default, not
    a validated-optimal one -- min_atr_multiple is exposed as a real
    parameter specifically so it can be tuned against how this actually
    performs on real symbols, which requires live price data this
    environment doesn't have access to. Nothing here has been checked
    against real market history.
    """
    if df is None or df.empty or len(df) < 55:
        return {"available": False, "note": f"insufficient {label} history"}
    from ..scanners.scanner_builder import _atr_series
    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df.columns else close
    low = df["low"].astype(float) if "low" in df.columns else close
    ema13, ema50 = _ema(close, 13), _ema(close, 50)
    rsi = _rsi(close, 14)
    rsi_ema90 = _ema(rsi, 90)
    atr = _atr_series(high, low, close, 14)
    price, e13, e50 = float(close.iloc[-1]), float(ema13.iloc[-1]), float(ema50.iloc[-1])
    r = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None
    rsi_diff90 = (float(rsi.iloc[-1] - rsi_ema90.iloc[-1]) if not pd.isna(rsi.iloc[-1]) and not pd.isna(rsi_ema90.iloc[-1]) else None)
    atr_val = float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) and atr.iloc[-1] > 0 else None
    gap_price_e13 = price - e13
    gap_e13_e50 = e13 - e50
    if atr_val:
        gap_price_e13_atr = gap_price_e13 / atr_val
        gap_e13_e50_atr = gap_e13_e50 / atr_val
        if gap_price_e13_atr >= min_atr_multiple and gap_e13_e50_atr >= min_atr_multiple:
            trend = "UPTREND"
        elif gap_price_e13_atr <= -min_atr_multiple and gap_e13_e50_atr <= -min_atr_multiple:
            trend = "DOWNTREND"
        else:
            trend = "CHOPPY"
    else:
        # ATR unavailable (e.g. no high/low columns in this history
        # source) -- fall back to the original percentage-based check
        # rather than failing outright.
        gap_price_e13_atr = None
        pct = gap_price_e13 / price * 100 if price else 0
        pct2 = gap_e13_e50 / price * 100 if price else 0
        trend = "UPTREND" if (pct >= 0.5 and pct2 >= 0.5) else ("DOWNTREND" if (pct <= -0.5 and pct2 <= -0.5) else "CHOPPY")
    dist_from_ema13_pct = round(gap_price_e13 / price * 100, 2) if price else None
    dist_from_ema13_atr = round(gap_price_e13_atr, 2) if gap_price_e13_atr is not None else None
    return {
        "available": True, "trend": trend, "price": round(price, 2),
        "ema13": round(e13, 2), "ema50": round(e50, 2), "ema13_ema50": round(e13 / e50, 4) if e50 else None,
        "rsi": round(r, 1) if r is not None else None, "rsi_diff90": round(rsi_diff90, 2) if rsi_diff90 is not None else None,
        "atr": round(atr_val, 2) if atr_val else None,
        "dist_from_ema13_pct": dist_from_ema13_pct, "dist_from_ema13_atr": dist_from_ema13_atr,
        "note": (f"{label}: {trend}, {dist_from_ema13_atr}x ATR from EMA13 ({dist_from_ema13_pct}%), RSI {round(r,1) if r is not None else 'n/a'}"
                 if dist_from_ema13_atr is not None else
                 f"{label}: {trend}, {dist_from_ema13_pct}% from EMA13 (ATR unavailable, used % fallback), RSI {round(r,1) if r is not None else 'n/a'}"),
    }


def _stage_weekly_trend(symbol: str, min_atr_multiple: float = 1.0) -> Dict[str, Any]:
    from ..scanners.scanner_builder import _history
    df = _history(symbol, "1w")
    return _trend_classify(df, "Weekly", min_atr_multiple=min_atr_multiple)


def _stage_daily_setup(symbol: str, min_atr_multiple: float = 1.0) -> Dict[str, Any]:
    from ..scanners.scanner_builder import _history
    df = _history(symbol, "1d")
    result = _trend_classify(df, "Daily", min_atr_multiple=min_atr_multiple)
    if result.get("available") and result.get("rsi") is not None:
        r = result["rsi"]
        # Setup quality on top of raw trend -- an uptrend with RSI
        # already >70 is a worse premium-selling setup (chasing an
        # extended move) than the same uptrend with RSI in a healthier
        # 45-65 range (room to keep grinding without an imminent
        # mean-reversion risk to the short strikes).
        if result["trend"] == "UPTREND":
            result["setup_quality"] = "EXTENDED" if r > 70 else ("HEALTHY" if 40 <= r <= 65 else "EARLY_OR_WEAK")
        elif result["trend"] == "DOWNTREND":
            result["setup_quality"] = "EXTENDED" if r < 30 else ("HEALTHY" if 35 <= r <= 60 else "EARLY_OR_WEAK")
        else:
            result["setup_quality"] = "CHOPPY_NO_EDGE"
    return result


def _real_atm_iv(universe: Dict[str, Any], atm: float) -> Dict[str, Any]:
    """ATM implied vol averaged across BOTH call and put sides, and --
    critically -- a flag for whether it came from real stored market IV
    at all.

    Two problems this fixes, both real:

    (1) Silent fallback masquerading as a measurement. greeks_strategy_scanner's
        _leg_iv() falls back to a symbol-wide HV proxy when a strike has
        no usable IV. Stage 4 then divides that by the same HV to get an
        IV/HV ratio -- which is arithmetically guaranteed to be exactly
        1.0, producing a confident-looking "NEUTRAL" verdict that is
        actually just "we had no IV data". Indistinguishable from a real
        1.0 reading. Given the yfinance IV quality already observed in
        this app's own stored rows (values like 0.00001), this fires on
        real symbols. This function reports has_real_iv=False instead, so
        the caller can say UNKNOWN honestly.

    (2) Call-side-only IV. The previous code read only universe["calls"].
        Most equities carry meaningful put skew -- put IV routinely runs
        above call IV -- so a call-only reading systematically understates
        the vol actually being sold on the put side of a condor or a put
        spread. Averaging both sides at the ATM strike is the more honest
        single number for "what is ATM vol here".

    Returns {"iv": float|None, "has_real_iv": bool, "call_iv": ..., "put_iv": ...}
    where iv is None only when NEITHER side had usable real data.
    """
    def _raw(side_map):
        row = side_map.get(atm)
        if row and row.get("iv") and 0.03 <= row["iv"] <= 3.0:
            return float(row["iv"])
        return None

    call_iv = _raw(universe.get("calls") or {})
    put_iv = _raw(universe.get("puts") or {})
    reals = [v for v in (call_iv, put_iv) if v is not None]
    if not reals:
        return {"iv": None, "has_real_iv": False, "call_iv": None, "put_iv": None}
    return {
        "iv": sum(reals) / len(reals), "has_real_iv": True,
        "call_iv": call_iv, "put_iv": put_iv,
    }


# ── Stage 4: IV versus HV ───────────────────────────────────────────────

def _stage_iv_vs_hv(symbol: str, expiry: str) -> Dict[str, Any]:
    from ..scanners.iron_condor_candidates import _get_iv_metrics, get_spot
    from ..scanners.greeks_strategy_scanner import _strike_universe, _nearest_strike, _dte
    hv_30, hv_252, ivp = _get_iv_metrics(symbol)
    spot = get_spot(symbol)
    atm_iv = None
    iv_meta = {"has_real_iv": False, "call_iv": None, "put_iv": None}
    if spot:
        universe = _strike_universe(symbol, expiry)
        all_strikes = sorted(set(universe["calls"].keys()) | set(universe["puts"].keys()))
        atm = _nearest_strike(all_strikes, spot)
        if atm is not None:
            iv_meta = _real_atm_iv(universe, atm)
            atm_iv = iv_meta["iv"]

    # UNKNOWN when the IV isn't real, rather than computing a ratio
    # against a fallback that would come out to a meaningless 1.0.
    if not iv_meta.get("has_real_iv") or not atm_iv:
        return {
            "spot": spot, "hv30_pct": round(hv_30, 1) if hv_30 else None, "ivp_proxy": ivp,
            "atm_iv_pct": None, "iv_hv_ratio": None, "verdict": "UNKNOWN",
            "has_real_iv": False, "call_iv_pct": None, "put_iv_pct": None,
            "note": ("No usable market IV stored at the ATM strike for this expiry, so IV/HV can't be "
                     "computed. Not a NEUTRAL reading -- genuinely unknown. Stage 5's expected move and "
                     "stage 6's pricing fall back to a historical-volatility proxy, which is a model "
                     "estimate rather than what the market is actually pricing."),
        }

    iv_hv_ratio = round((atm_iv * 100) / hv_30, 2) if (hv_30 and hv_30 > 0) else None
    if iv_hv_ratio is None:
        verdict = "UNKNOWN"
    elif iv_hv_ratio >= 1.3:
        verdict = "FAVORABLE_FOR_SELLING"
    elif iv_hv_ratio >= 1.0:
        verdict = "NEUTRAL"
    else:
        verdict = "UNFAVORABLE_FOR_SELLING"

    skew_note = ""
    if iv_meta.get("call_iv") and iv_meta.get("put_iv"):
        diff = (iv_meta["put_iv"] - iv_meta["call_iv"]) * 100
        if abs(diff) >= 2:
            richer = "put" if diff > 0 else "call"
            skew_note = (f" ATM skew: {richer} side richer by {abs(diff):.1f} vol pts "
                         f"(call {iv_meta['call_iv']*100:.1f}% / put {iv_meta['put_iv']*100:.1f}%) -- "
                         f"the {richer} side is where the premium actually is.")

    return {
        "spot": spot, "hv30_pct": round(hv_30, 1) if hv_30 else None, "ivp_proxy": ivp,
        "atm_iv_pct": round(atm_iv * 100, 1), "iv_hv_ratio": iv_hv_ratio,
        "verdict": verdict, "has_real_iv": True,
        "call_iv_pct": round(iv_meta["call_iv"] * 100, 1) if iv_meta.get("call_iv") else None,
        "put_iv_pct": round(iv_meta["put_iv"] * 100, 1) if iv_meta.get("put_iv") else None,
        "note": f"IV/HV={iv_hv_ratio}x -> {verdict}.{skew_note}",
    }


# ── Stage 5: Expected move and option strikes ───────────────────────────

def _stage_expected_move(symbol: str, expiry: str, iv_stage: Dict[str, Any]) -> Dict[str, Any]:
    from ..scanners.greeks_strategy_scanner import _dte
    spot = iv_stage.get("spot")
    atm_iv_pct = iv_stage.get("atm_iv_pct")
    if not spot:
        return {"available": False, "note": "missing spot from stage 4"}
    # Stage 4 now reports atm_iv_pct=None (rather than a fallback value
    # dressed up as a measurement) when no real market IV was stored.
    # Falling back to HV here keeps the expected-move estimate available
    # rather than blanking the stage entirely -- but it's labelled as a
    # proxy, because an HV-derived "expected move" is a historical
    # estimate, not what the market is actually pricing in.
    using_proxy = False
    if not atm_iv_pct:
        atm_iv_pct = iv_stage.get("hv30_pct")
        using_proxy = True
    if not atm_iv_pct:
        return {"available": False, "note": "no usable IV or HV to compute an expected move"}
    dte = _dte(expiry)
    T = max(dte, 1) / 365.0
    iv = atm_iv_pct / 100.0
    expected_move_pct = round(iv * math.sqrt(T) * 100, 2)
    expected_move_dollars = round(spot * expected_move_pct / 100, 2)
    proxy_note = (" (from HV30, not market IV -- no usable stored IV at the ATM strike, "
                  "so this is a historical estimate rather than the market's own pricing)"
                  if using_proxy else "")
    return {
        "available": True, "dte": dte, "expected_move_pct": expected_move_pct,
        "using_hv_proxy": using_proxy,
        "expected_move_dollars": expected_move_dollars,
        "range_low": round(spot - expected_move_dollars, 2), "range_high": round(spot + expected_move_dollars, 2),
        "note": f"Expected move +/-{expected_move_pct}% (+/-${expected_move_dollars}) by {expiry} ({dte}d){proxy_note}",
    }


# ── Stage 6: Trade structure and risk ───────────────────────────────────

def _stage_trade_structure(symbol: str, expiry: str, strategy: str, iv_stage: Dict[str, Any],
                            min_rr: float = 0.5, min_oi: int = 50, max_spread_pct: float = 15.0) -> Dict[str, Any]:
    from ..scanners.greeks_strategy_scanner import STRATEGY_BUILDERS, _strike_universe, _dte
    builder = STRATEGY_BUILDERS.get(strategy)
    if not builder:
        return {"available": False, "note": f"unknown strategy '{strategy}' -- choose one of {list(STRATEGY_BUILDERS.keys())}"}
    spot = iv_stage.get("spot")
    hv_30 = None  # fallback iv path inside the builder only matters for strikes missing real per-strike iv
    if not spot:
        return {"available": False, "note": "missing spot from stage 4"}
    dte = _dte(expiry)
    T = max(dte, 1) / 365.0
    iv = (iv_stage.get("atm_iv_pct") or iv_stage.get("hv30_pct") or 30) / 100.0
    universe = _strike_universe(symbol, expiry)
    if not universe["calls"] and not universe["puts"]:
        return {"available": False, "note": "no stored option chain for this symbol/expiry"}
    res = builder(symbol, expiry, spot, iv, T, dte, universe, min_oi=min_oi, max_spread_pct=max_spread_pct)
    if not res:
        return {"available": False, "note": "no candidate passed liquidity/construction gates for this strategy/expiry"}
    verdict = "PASS" if (res.get("rr") is not None and res["rr"] >= min_rr) else "BELOW_MIN_RR"
    res["stage_verdict"] = verdict
    return res


# ── Stage 7: Beta and correlation as position-sizing filters ───────────

def _daily_returns(symbol: str) -> Optional[pd.Series]:
    from ..scanners.scanner_builder import _history
    df = _history(symbol, "1d")
    if df is None or df.empty or len(df) < 30:
        return None
    return df["close"].astype(float).pct_change().dropna()


def _stage_earnings_check(symbol: str, expiry: str) -> Dict[str, Any]:
    """Does an earnings event fall inside this trade's life?

    Selling premium across earnings is a materially different trade than
    selling it in a quiet stretch -- an overnight gap can blow through
    short strikes that every other stage said were comfortably clear,
    and IV typically collapses right after the event regardless of
    direction. This is a genuinely different risk from anything stages
    1-3 measure (they read price structure, which says nothing about a
    scheduled binary event), which is why it gets its own check rather
    than being folded into the trend stages.

    Reuses get_earnings_info() -- already wired into the Greeks Strategy
    Scanner for exactly this purpose, so this is connecting existing
    infrastructure rather than adding a new data dependency.
    """
    try:
        from ..scanners.earnings_calendar import get_earnings_info
        from ..scanners.greeks_strategy_scanner import _dte
    except Exception:
        return {"available": False, "note": "earnings calendar unavailable"}
    try:
        earn = get_earnings_info(symbol) or {}
    except Exception:
        return {"available": False, "note": "earnings lookup failed"}
    earn_days = earn.get("earn_days")
    earn_date = earn.get("earn_date")
    dte = _dte(expiry)
    if earn_days is None:
        return {"available": True, "spans_earnings": False, "earn_date": None, "earn_days": None,
                "note": "No earnings date on record for this symbol -- treat as unknown, not as 'no earnings'."}
    spans = 0 <= earn_days <= dte
    return {
        "available": True, "spans_earnings": spans, "earn_date": earn_date, "earn_days": earn_days,
        "dte": dte, "confirmed": bool(earn.get("next_earn_confirmed")),
        "note": (
            f"Earnings {earn_date} ({earn_days}d out) falls INSIDE this {dte}-day trade"
            f"{'' if earn.get('next_earn_confirmed') else ' (date unconfirmed)'} -- an overnight gap can "
            f"clear short strikes that look safe on every price-based measure, and IV usually collapses "
            f"right after regardless of direction."
            if spans else
            f"Earnings {earn_date} ({earn_days}d out) falls after this {dte}-day expiry -- not a factor for this trade."
        ),
    }


def _stage_beta_correlation(symbol: str, existing_position_symbols: Optional[List[str]] = None,
                             beta_neutral_point: float = 1.0, beta_floor_multiplier: float = 0.5,
                             correlation_floor: float = 0.3, correlation_full_penalty: float = 0.9) -> Dict[str, Any]:
    """Sizing multiplier, not a go/no-go gate -- everything upstream
    already determined whether this trade is structurally sound; this
    stage only answers how much size is prudent given how much this
    symbol amplifies market moves (beta) and how much it duplicates
    risk already on the book (correlation to existing positions).

    REVISED from an earlier version that used hard threshold cliffs
    (beta>=1.5 -> flat 0.7x, correlation>=0.7 -> flat 0.6x) with numbers
    that were simply invented, not derived from any stated convention or
    backtest -- and the cliffs themselves were arbitrary in a way that
    mattered: beta 1.49 got zero adjustment while 1.50 got a 30% cut for
    no principled reason. Replaced with two continuous curves, each
    grounded in a stated, checkable principle rather than a chosen
    number:

    Beta: 1/beta scaling. This is the standard volatility-adjusted
    (risk-parity-style) sizing principle -- rather than sizing every
    symbol the same dollar amount regardless of how much it moves,
    size inversely to how much it amplifies market moves, so a beta-2
    stock and a beta-1 stock end up contributing comparable risk rather
    than the beta-2 name silently carrying twice the exposure per
    dollar. Below beta_neutral_point (default 1.0), no adjustment --
    below-market sensitivity doesn't need to be sized UP under this
    principle, only at-or-above-market sensitivity gets sized down.
    Floored at beta_floor_multiplier (default 0.5x) so a very high-beta
    name doesn't get sized to near-zero automatically.

    Correlation: linear penalty above a floor. Below correlation_floor
    (default 0.3), no penalty -- mild correlation is normal across a
    watchlist and isn't a real duplication-of-risk signal. Above it,
    the penalty scales linearly up to correlation_full_penalty (default
    0.9 correlation -> full floor applied), using the SINGLE most
    correlated existing position, not the product across all of them --
    multiplying penalties across several moderately-correlated positions
    would compound toward near-zero size for reasons that don't actually
    reflect the real duplicated risk, which is bounded by the worst
    single overlap, not the count of positions checked against.

    These parameters are exposed specifically so they can be tuned to
    an actual stated risk convention -- they are defensible on stated
    principle, not asserted as "correct" for any particular trader's
    actual sizing rules, which this code has no way to know.
    """
    from ..services.fundamentals import get_beta
    beta = get_beta(symbol)
    spy_returns = _daily_returns("SPY")
    sym_returns = _daily_returns(symbol)
    corr_spy = None
    if spy_returns is not None and sym_returns is not None:
        aligned = pd.concat([sym_returns, spy_returns], axis=1, join="inner")
        if len(aligned) >= 20:
            corr_spy = round(float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1])), 2)

    correlated_positions = []
    if existing_position_symbols:
        for other in existing_position_symbols:
            if other.upper() == symbol.upper():
                continue
            other_returns = _daily_returns(other)
            if other_returns is None or sym_returns is None:
                continue
            aligned = pd.concat([sym_returns, other_returns], axis=1, join="inner")
            if len(aligned) >= 20:
                c = round(float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1])), 2)
                correlated_positions.append({"symbol": other, "correlation": c})

    size_multiplier = 1.0
    reasons = []

    if beta is not None and beta > beta_neutral_point:
        beta_mult = max(beta_floor_multiplier, beta_neutral_point / beta)
        size_multiplier *= beta_mult
        reasons.append(f"beta {beta} > {beta_neutral_point} -- sized to {round(beta_mult,2)}x via 1/beta "
                        f"(risk-parity-style: size inversely to market sensitivity, floored at {beta_floor_multiplier}x)")
    elif beta is not None:
        reasons.append(f"beta {beta} <= {beta_neutral_point} -- at or below market sensitivity, no adjustment")

    if correlated_positions:
        worst = max(correlated_positions, key=lambda c: abs(c["correlation"]))
        worst_abs = abs(worst["correlation"])
        if worst_abs > correlation_floor:
            span = max(1e-6, correlation_full_penalty - correlation_floor)
            frac = min(1.0, (worst_abs - correlation_floor) / span)
            corr_mult = 1.0 - frac * (1.0 - beta_floor_multiplier)
            size_multiplier *= corr_mult
            reasons.append(f"correlation {worst['correlation']} to existing position {worst['symbol']} "
                            f"(worst of {len(correlated_positions)} checked) -- sized to {round(corr_mult,2)}x, "
                            f"linear penalty above {correlation_floor}")
    if not reasons:
        reasons.append("no beta or correlation concerns found -- standard size")

    return {
        "beta": beta, "correlation_to_spy": corr_spy, "correlated_existing_positions": correlated_positions,
        "size_multiplier": round(size_multiplier, 2), "reasons": reasons,
    }


# ── Orchestration ────────────────────────────────────────────────────────

def evaluate_trade(symbol: str, expiry: str, strategy: str = "iron_fly",
                    existing_position_symbols: Optional[List[str]] = None,
                    min_rr: float = 0.5, min_oi: int = 50, max_spread_pct: float = 15.0,
                    min_atr_multiple: float = 1.0,
                    beta_neutral_point: float = 1.0, beta_floor_multiplier: float = 0.5,
                    correlation_floor: float = 0.3, correlation_full_penalty: float = 0.9) -> Dict[str, Any]:
    """Runs all seven stages in the specified order. Every stage runs
    (nothing is skipped even if an earlier stage looks unfavorable) --
    the point is a complete picture for a human to weigh, not an
    automated kill-switch that hides stages 4-7 just because stage 1
    looked mixed. The final verdict summarizes agreement/conflict
    across stages rather than forcing a single pass/fail number that
    would hide exactly the kind of nuance ("great structure, bad
    regime" vs "so-so structure, everything else aligned") worth
    actually seeing.

    min_atr_multiple, beta_*, correlation_*: exposed here rather than
    buried as internal constants specifically so they can be tuned to
    an actual stated convention. min_atr_multiple was validated against
    30 seeds of synthetic pure-noise data (correctly classified CHOPPY
    every time) and clean synthetic trend data (correctly classified
    UPTREND/DOWNTREND) -- it has NOT been validated against real market
    data, since no live price feed is reachable from where this was
    built. See _trend_classify's docstring for the full picture,
    including what a sensitivity sweep across candidate thresholds
    actually shows.
    """
    stage1 = _stage_market_sector_regime(symbol)
    stage2 = _stage_weekly_trend(symbol, min_atr_multiple=min_atr_multiple)
    stage3 = _stage_daily_setup(symbol, min_atr_multiple=min_atr_multiple)
    stage4 = _stage_iv_vs_hv(symbol, expiry)
    stage5 = _stage_expected_move(symbol, expiry, stage4)
    stage6 = _stage_trade_structure(symbol, expiry, strategy, stage4, min_rr=min_rr, min_oi=min_oi, max_spread_pct=max_spread_pct)
    stage7 = _stage_beta_correlation(symbol, existing_position_symbols,
                                      beta_neutral_point=beta_neutral_point, beta_floor_multiplier=beta_floor_multiplier,
                                      correlation_floor=correlation_floor, correlation_full_penalty=correlation_full_penalty)
    earnings = _stage_earnings_check(symbol, expiry)

    # ── Cross-stage synthesis ───────────────────────────────────────────
    # Stages 1-3 read price structure; stages 4-6 price the trade. Run
    # independently they're just a checklist -- each stage answering its
    # own question and never informing the others. What follows is the
    # part that actually connects them: reading the regime/trend findings
    # AGAINST the chosen structure's own directional exposure, which is
    # where a premium seller's real risk lives (a structure whose
    # threatened side faces into a confirmed trend is a materially worse
    # trade than the same structure with the trend at its back, even
    # though stages 4-6 would price both identically).
    directional_conflicts = []
    strat = (stage6.get("strategy") or "").lower()
    weekly_trend = stage2.get("trend")
    daily_trend = stage3.get("trend")
    lean = stage1.get("combined_lean")

    # Which side of the structure gets hurt by which direction
    threatened_by_up = "call" in strat or "condor" in strat or "fly" in strat or "strangle" in strat or "straddle" in strat
    threatened_by_down = "put" in strat or "condor" in strat or "fly" in strat or "strangle" in strat or "straddle" in strat or "lizard" in strat

    if weekly_trend == "UPTREND" and daily_trend == "UPTREND" and threatened_by_up:
        directional_conflicts.append(
            "Weekly AND daily both in confirmed uptrends while this structure's call side is the exposed one -- "
            "trend agreement across timeframes is exactly when a short call strike is most likely to be run through."
        )
    if weekly_trend == "DOWNTREND" and daily_trend == "DOWNTREND" and threatened_by_down:
        directional_conflicts.append(
            "Weekly AND daily both in confirmed downtrends while this structure's put side is the exposed one -- "
            "aligned downtrends are exactly when a short put strike is most likely to be breached."
        )
    if lean == "CONFLICTED" and stage6.get("stage_verdict") == "PASS":
        directional_conflicts.append(
            "Structure prices well, but market and sector regimes disagree -- the premium may be compensating "
            "for genuine directional uncertainty rather than representing free edge."
        )
    if stage3.get("setup_quality") == "EXTENDED" and stage4.get("verdict") == "FAVORABLE_FOR_SELLING":
        directional_conflicts.append(
            "IV looks rich relative to HV, but the daily setup is extended -- elevated IV after an extended move "
            "often reflects real expected movement rather than mispricing, so 'rich IV' here is weaker evidence than it looks."
        )

    # Flags carry a severity, not just text -- a structure that fails its
    # RR floor is disqualifying in a way a choppy weekly trend simply
    # isn't, and counting them equally (the previous `len(flags) <= 2`
    # rule) made a structurally broken trade read the same as one with
    # soft context. "blocking" means the trade doesn't clear on its own
    # terms; "caution" means it clears but with real reservations.
    flags = []
    def _flag(sev, text):
        flags.append({"severity": sev, "text": text})

    if stage6.get("stage_verdict") not in ("PASS",):
        _flag("blocking", f"Trade structure: {stage6.get('note') or stage6.get('stage_verdict')}")
    if stage4.get("verdict") == "UNFAVORABLE_FOR_SELLING":
        _flag("blocking", "IV/HV ratio favors BUYING premium over selling it right now -- the core premise of this trade is working against you")
    if earnings.get("spans_earnings"):
        _flag("blocking", earnings.get("note") or "Earnings falls inside this trade's life")
    if stage4.get("verdict") == "UNKNOWN":
        _flag("caution", "IV/HV could not be computed from real market IV -- pricing below leans on a historical-volatility proxy, so treat the edge as unverified")
    if stage1.get("agreement") == "CONFLICT":
        _flag("caution", "Market and sector regimes disagree -- mixed backdrop before even looking at this symbol's own chart")
    if stage1.get("macro_conflict"):
        _flag("caution", stage1["macro_conflict"])
    vix_info = stage1.get("vix") or {}
    if vix_info.get("tier") == "PANIC":
        _flag("blocking", vix_info.get("note") or "VIX in panic regime -- strike selection logic breaks down at this level")
    elif vix_info.get("tier") == "COMPLACENCY":
        _flag("caution", vix_info.get("note") or "VIX at complacency lows -- cheap premium, elevated risk of sudden expansion")
    elif vix_info.get("spike_today"):
        _flag("caution", f"VIX moved {vix_info.get('change_pct')}% today -- a notable vol-of-vol event independent of the absolute level")
    if stage2.get("trend") == "CHOPPY":
        _flag("caution", "Weekly trend is choppy -- no clear structure to lean on")
    if stage3.get("setup_quality") == "EXTENDED":
        _flag("caution", "Daily setup is extended (RSI far from a healthy range) -- higher mean-reversion risk to short strikes")
    for c in directional_conflicts:
        _flag("caution", c)
    if stage7.get("size_multiplier", 1.0) < 1.0:
        _flag("caution", f"Position sizing reduced to {stage7['size_multiplier']}x -- {'; '.join(stage7['reasons'])}")

    blocking = [f for f in flags if f["severity"] == "blocking"]
    cautions = [f for f in flags if f["severity"] == "caution"]
    if blocking:
        overall = "STAND_ASIDE"
    elif len(cautions) >= 3:
        overall = "REDUCE_SIZE_OR_STAND_ASIDE"
    elif cautions:
        overall = "CAUTION"
    else:
        overall = "GO"

    return {
        "symbol": symbol, "expiry": expiry, "strategy": strategy,
        "stage_1_market_sector_regime": stage1,
        "stage_2_weekly_trend": stage2,
        "stage_3_daily_setup": stage3,
        "stage_4_iv_vs_hv": stage4,
        "stage_5_expected_move_strikes": stage5,
        "stage_6_trade_structure_risk": stage6,
        "stage_7_beta_correlation_sizing": stage7,
        "earnings_check": earnings,
        "cross_stage_conflicts": directional_conflicts,
        "flags": [f["text"] for f in flags],          # kept for the existing UI, which renders plain strings
        "flags_detailed": flags,                       # severity-aware, for anything that wants to weigh them
        "blocking_count": len(blocking), "caution_count": len(cautions),
        "overall_verdict": overall,
    }


def _strategy_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """Compact comparison payload for Auto mode and the results table."""
    s4 = result.get("stage_4_iv_vs_hv") or {}
    s3 = result.get("stage_3_daily_setup") or {}
    s6 = result.get("stage_6_trade_structure_risk") or {}
    return {"strategy": s6.get("strategy") or result.get("strategy"), "stage_verdict": s6.get("stage_verdict"),
            "overall_verdict": result.get("overall_verdict"), "pop": s6.get("pop"), "rr": s6.get("rr"),
            "iv": s4.get("atm_iv_pct"), "rsi_diff90": s3.get("rsi_diff90"),
            "ema13_ema50": s3.get("ema13_ema50"), "note": s6.get("note")}


def evaluate_trade_auto(symbol: str, expiry: str, **kwargs) -> Dict[str, Any]:
    """Evaluate supported structures and expose a transparent ranking."""
    from ..scanners.greeks_strategy_scanner import STRATEGY_BUILDERS
    candidates = []
    for key in STRATEGY_BUILDERS:
        try:
            candidate = evaluate_trade(symbol, expiry, strategy=key, **kwargs)
            stage6 = candidate.get("stage_6_trade_structure_risk") or {}
            structural_pass = stage6.get("stage_verdict") == "PASS"
            pop = float(stage6.get("pop") or 0)
            rr = float(stage6.get("rr") or 0)
            no_blockers = not candidate.get("blocking_count", 0)
            score = (1000 if structural_pass else 0) + (100 if no_blockers else 0) + pop * 100 + min(rr, 10) * 10
            candidates.append((score, key, candidate))
        except Exception as exc:
            candidates.append((-1, key, {"symbol": symbol, "expiry": expiry, "strategy": key, "overall_verdict": "UNAVAILABLE",
                                          "stage_6_trade_structure_risk": {"available": False, "note": str(exc)}}))
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = candidates[0][2]
    comparisons = []
    for score, key, candidate in candidates:
        item = _strategy_summary(candidate)
        item["key"] = key
        item["selected"] = candidate is selected
        comparisons.append(item)
    selected["requested_strategy"] = "auto"
    selected["strategy_comparisons"] = comparisons
    selected_summary = comparisons[0]
    viable = [c for c in comparisons if c.get("stage_verdict") == "PASS"]
    if len(viable) > 1:
        selected["auto_rationale"] = ("Auto selected {} from {} structurally viable choices; it ranked highest on framework verdict, then POP ({:.0f}%) and RR ({}). Review alternatives before placing a trade.".format(
            selected_summary.get("strategy"), len(viable), (selected_summary.get("pop") or 0) * 100, selected_summary.get("rr") if selected_summary.get("rr") is not None else "n/a"))
    else:
        selected["auto_rationale"] = "Auto selected {}; it was the strongest available framework result. Review POP, RR and flags before placing a trade.".format(selected_summary.get("strategy"))
    return selected


# ── Routes ──────────────────────────────────────────────────────────────

import sqlite3
from flask import Blueprint, jsonify, render_template, request
from ..config import DB_PATH

option_sale_framework_bp = Blueprint("option_sale_framework", __name__, url_prefix="/option-sale-framework")


def _watchlist_symbols(watchlist_id):
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        rows = con.execute(
            "SELECT DISTINCT upper(symbol) FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (watchlist_id,),
        ).fetchall()
    return [row[0] for row in rows if row[0]]


def _scope_symbols(symbol, watchlist_id):
    return [symbol] if symbol else (_watchlist_symbols(watchlist_id) if watchlist_id else [])


@option_sale_framework_bp.route("/")
def page():
    return render_template("option_sale_framework.html")


@option_sale_framework_bp.route("/api/watchlists")
def api_watchlists():
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as con:
            rows = con.execute(
                """SELECT w.id, w.name, COUNT(ws.symbol) AS symbol_count
                   FROM watchlists w LEFT JOIN watchlist_symbols ws ON ws.watchlist_id=w.id
                   GROUP BY w.id, w.name ORDER BY COALESCE(w.is_default,0) DESC, lower(w.name)"""
            ).fetchall()
        return jsonify({"ok": True, "watchlists": [
            {"id": row[0], "name": row[1], "symbol_count": row[2]} for row in rows
        ]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@option_sale_framework_bp.route("/api/expiries")
def api_expiries():
    symbol = (request.args.get("symbol") or "").strip().upper()
    raw_watchlist_id = request.args.get("watchlist_id")
    try:
        watchlist_id = int(raw_watchlist_id) if raw_watchlist_id else None
    except ValueError:
        return jsonify({"ok": False, "error": "invalid watchlist_id"}), 400
    symbols = _scope_symbols(symbol, watchlist_id)
    if not symbols:
        return jsonify({"ok": False, "error": "select a symbol or watchlist"}), 400
    placeholders = ",".join("?" for _ in symbols)
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as con:
            rows = con.execute(
                f"""SELECT expiration, COUNT(DISTINCT symbol) AS coverage
                    FROM options WHERE symbol IN ({placeholders}) AND expiration IS NOT NULL
                      AND date(expiration) >= date('now')
                    GROUP BY expiration ORDER BY expiration""",
                symbols,
            ).fetchall()
        return jsonify({"ok": True, "symbol_count": len(symbols),
                        "expiries": [{"value": row[0], "coverage": row[1]} for row in rows if row[0]]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@option_sale_framework_bp.route("/api/evaluate")
def api_evaluate():
    symbol = (request.args.get("symbol") or "").strip().upper()
    raw_watchlist_id = request.args.get("watchlist_id")
    expiry = request.args.get("expiry")
    try:
        watchlist_id = int(raw_watchlist_id) if raw_watchlist_id else None
    except ValueError:
        return jsonify({"ok": False, "error": "invalid watchlist_id"}), 400
    symbols = _scope_symbols(symbol, watchlist_id)
    if not symbols or not expiry:
        return jsonify({"ok": False, "error": "select a symbol or watchlist, then an expiry"}), 400

    strategy = request.args.get("strategy", "auto")
    min_rr = float(request.args.get("min_rr", 0.5))
    min_oi = int(request.args.get("min_oi", 50))
    max_spread_pct = float(request.args.get("max_spread_pct", 15.0))
    min_atr_multiple = float(request.args.get("min_atr_multiple", 1.0))
    beta_neutral_point = float(request.args.get("beta_neutral_point", 1.0))
    beta_floor_multiplier = float(request.args.get("beta_floor_multiplier", 0.5))
    correlation_floor = float(request.args.get("correlation_floor", 0.3))
    correlation_full_penalty = float(request.args.get("correlation_full_penalty", 0.9))
    positions_raw = request.args.get("existing_positions", "")
    existing_position_symbols = [item.strip().upper() for item in positions_raw.split(",") if item.strip()] or None

    results = []
    for target_symbol in symbols:
        try:
            common_kwargs = dict(
                existing_position_symbols=existing_position_symbols, min_rr=min_rr, min_oi=min_oi,
                max_spread_pct=max_spread_pct, min_atr_multiple=min_atr_multiple,
                beta_neutral_point=beta_neutral_point, beta_floor_multiplier=beta_floor_multiplier,
                correlation_floor=correlation_floor, correlation_full_penalty=correlation_full_penalty,
            )
            result = (evaluate_trade_auto(target_symbol, expiry, **common_kwargs) if strategy == 'auto'
                      else evaluate_trade(target_symbol, expiry, strategy=strategy, **common_kwargs))
            results.append({"symbol": target_symbol, "result": result})
        except Exception as exc:
            results.append({"symbol": target_symbol, "error": f"{type(exc).__name__}: {exc}"})

    return jsonify({"ok": True, "scope": "symbol" if symbol else "watchlist",
                    "requested_expiry": expiry, "results": results,
                    "result": results[0].get("result") if len(results) == 1 and results[0].get("result") else None})
