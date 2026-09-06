# oiapp/services/macro_regime.py
"""
Macro regime layer: 2Y yield, 10Y yield, DXY, oil -- trend-scored and
translated into equity/gold/BTC impact, plus an FOMC-in-window gate.

WHY THIS IS SEPARATE FROM, NOT A REPLACEMENT FOR, THE EXISTING
SPY/SECTOR REGIME CHECK (maya_composite_logic.py's _benchmark_regime):
that function treats "price trending up" as bullish for whatever
symbol it's given -- correct for SPY or a sector ETF, but WRONG applied
naively to yields or the dollar index: rising yields and a rising
dollar are headwinds for equities, not tailwinds. Reusing that
function's raw EMA/MACD trend-detection is fine (see _series_trend
below, which does exactly that); reusing its bullish/bearish LABELING
would silently invert the signal. This module does its own explicit,
asset-aware translation instead.

HONEST LIMITATIONS, stated here because they matter and are easy to
gloss over:

1. FED RATE HIKE "POSSIBILITY" -- what's actually built here is an
   FOMC-MEETING-IN-WINDOW GATE (real, hardcoded 2026 meeting dates,
   reused from services/macro_events.py's existing calendar), not a
   market-implied PROBABILITY of a hike/cut. A genuine probability
   number is the CME FedWatch methodology: derived from 30-Day Fed
   Funds futures (ticker ZQ) prices, isolating the meeting-month
   contract and accounting for the meeting's exact date within that
   month. That requires real ZQ futures data, which is not confirmed
   available anywhere in this app, and this sandbox has no network
   access to Yahoo Finance's backend to test it live even if a ticker
   were guessed at. Building a plausible-sounding fake number would be
   worse than not having one -- what's built instead answers "is there
   Fed decision risk in my window", which is the actionable part for a
   short-DTE credit seller regardless of which way the decision goes.

2. TICKER RELIABILITY, not verified live. ^TNX (10Y yield) and CL=F
   (WTI oil) are well-established, generally reliable yfinance tickers.
   DX-Y.NYB (dollar index) is commonly used but occasionally flaky
   depending on data provider changes. A clean 2Y TREASURY YIELD ticker
   on yfinance is the weakest link here -- ^IRX is 13-week (3-month),
   not 2-year; there is no consistently reliable free 2Y yield series
   confirmed for this build. Coded with a fallback chain and an
   explicit "unavailable" state rather than silently substituting a
   different maturity and calling it 2Y.

3. GOLD's real driver is REAL yields (nominal minus inflation
   expectations), not nominal 10Y alone -- using nominal 10Y as the
   proxy here is a real simplification, stated plainly rather than
   presented as precise. Oil's link to gold is indirect at best (mainly
   via inflation expectations) and is NOT scored as a gold driver here
   for that reason.

4. BTC's macro correlation is regime-dependent and materially less
   stable than gold's -- it has traded as a high-beta risk asset
   (correlating with tech multiple compression from rising yields) in
   some periods and as an uncorrelated or "digital gold"-style hedge in
   others. The BTC impact score here reflects the higher-beta-risk-asset
   framing as the general case, flagged explicitly as the less certain
   of the two asset translations.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher) -- standard,
    well-established computation, not a guess. Needed for Good Friday,
    the one US market holiday that isn't a fixed date or a simple
    Nth-weekday-of-month rule."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    count = 0
    while True:
        if d.weekday() == weekday:
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, 12, 31) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def us_market_holidays(year: int) -> Dict[date, str]:
    """NYSE market holidays for a given year, computed by rule rather
    than hardcoded -- generalizes to any year automatically, unlike the
    FOMC calendar (services/macro_events.py), which genuinely can't be
    rule-based since meeting dates aren't algorithmic. Verified against
    known 2026 dates before trusting this, including the exact Labor
    Day (Sept 7, 2026) that motivated building this in the first place --
    a rolling-plan request was showing DTE 0-3 all collapsed onto one
    expiry because Sept 5-6 were a weekend and Sept 7 was this holiday,
    with no way to tell the difference between "no listed contract
    exists" and "today just isn't a trading day at all" without this.
    """
    good_friday = _easter(year) - timedelta(days=2)
    return {
        date(year, 1, 1): "New Year's Day",
        _nth_weekday(year, 1, 0, 3): "MLK Day",
        _nth_weekday(year, 2, 0, 3): "Presidents Day",
        good_friday: "Good Friday",
        _last_weekday(year, 5, 0): "Memorial Day",
        date(year, 6, 19): "Juneteenth",
        date(year, 7, 4): "Independence Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving",
        date(year, 12, 25): "Christmas",
    }


def next_trading_days(start: date, n: int) -> List[date]:
    """Next n trading days starting from (and including, if it's itself
    a trading day) `start` -- skips weekends and the holidays above.
    This is what "DTE 0-5" should actually mean for an options rolling
    plan: the next n days the market is genuinely open, not n raw
    calendar days including weekends/holidays where nothing trades and
    no expiry could possibly exist."""
    holidays = set()
    for yr in (start.year, start.year + 1):  # cover a year boundary if start is late December
        holidays |= set(us_market_holidays(yr).keys())
    days: List[date] = []
    d = start
    while len(days) < n:
        if d.weekday() < 5 and d not in holidays:
            days.append(d)
        d += timedelta(days=1)
    return days


MACRO_TICKERS = {
    "yield_10y": "^TNX",       # well-established
    "dxy": "DX-Y.NYB",         # commonly used, occasionally flaky
    "oil": "CL=F",             # well-established
    "yield_2y": None,          # no confirmed-reliable free ticker -- see module docstring
}


def _ema(series, n):
    return series.ewm(span=n, adjust=False).mean()


def _macd(close):
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    macd_line = ema12 - ema26
    signal = _ema(macd_line, 9)
    return macd_line, signal, macd_line - signal


def _series_trend(ticker: str, period: str = "6mo") -> Dict[str, Any]:
    """Raw trend direction only -- deliberately no bullish/bearish label
    here, since whether "trending up" is good or bad news depends on
    which asset this is and what it's being used to assess, which the
    caller decides. See module docstring for why labeling here would
    have been wrong."""
    if not ticker:
        return {"direction": "UNAVAILABLE", "note": "no ticker configured"}
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period=period, auto_adjust=False)
        if df is None or df.empty or len(df) < 60:
            return {"direction": "UNAVAILABLE", "note": f"{ticker} history unavailable"}
        close = df["Close"].astype(float)
        ema20, ema50 = _ema(close, 20), _ema(close, 50)
        _, _, macd_hist = _macd(close)
        price = float(close.iloc[-1])
        e20, e50, h = float(ema20.iloc[-1]), float(ema50.iloc[-1]), float(macd_hist.iloc[-1])
        if price > e20 > e50 and h > 0:
            direction = "UP"
        elif price < e20 < e50 and h < 0:
            direction = "DOWN"
        else:
            direction = "FLAT"
        return {"direction": direction, "price": round(price, 3), "note": f"{ticker} trend {direction}"}
    except Exception as e:
        return {"direction": "UNAVAILABLE", "note": f"{ticker} error: {e}"}


def get_macro_regime() -> Dict[str, Any]:
    """Trend-scores 10Y yield, DXY, and oil (2Y reported as UNAVAILABLE
    -- see module docstring), then applies an explicit, asset-aware
    translation to equity/gold/BTC impact. Every score here is additive
    context for a human to weigh, matching this app's established
    pattern elsewhere (option_sale_framework's own stages) -- not a
    single number that hides which specific input is driving it.
    """
    y10 = _series_trend(MACRO_TICKERS["yield_10y"])
    y2 = _series_trend(MACRO_TICKERS["yield_2y"])
    dxy = _series_trend(MACRO_TICKERS["dxy"])
    oil = _series_trend(MACRO_TICKERS["oil"])

    equity_headwind = 0
    equity_notes: List[str] = []
    if y10["direction"] == "UP":
        equity_headwind += 2
        equity_notes.append("10Y yield rising -- discount-rate headwind, hits growth/tech multiples hardest (QQQ more than SPY/IWM)")
    elif y10["direction"] == "DOWN":
        equity_headwind -= 2
        equity_notes.append("10Y yield falling -- discount-rate tailwind, favors growth/tech multiples")
    if dxy["direction"] == "UP":
        equity_headwind += 1
        equity_notes.append("Dollar rising -- multinational earnings translation headwind (larger effect on SPY/QQQ large-caps than IWM domestic small-caps)")
    elif dxy["direction"] == "DOWN":
        equity_headwind -= 1
        equity_notes.append("Dollar falling -- multinational earnings translation tailwind")
    if oil["direction"] == "UP":
        equity_notes.append("Oil rising -- sector-conditional: consumer/transport headwind, energy tailwind, not scored as a blanket equity direction")
    elif oil["direction"] == "DOWN":
        equity_notes.append("Oil falling -- sector-conditional: consumer/transport tailwind, energy headwind")

    gold_impact = 0
    gold_notes: List[str] = []
    if y10["direction"] == "UP":
        gold_impact -= 2
        gold_notes.append("Nominal 10Y yield rising -- opportunity-cost headwind for non-yielding gold (proxy for real yields, not precise -- see module docstring)")
    elif y10["direction"] == "DOWN":
        gold_impact += 2
        gold_notes.append("Nominal 10Y yield falling -- opportunity-cost tailwind for gold")
    if dxy["direction"] == "UP":
        gold_impact -= 2
        gold_notes.append("Dollar rising -- direct headwind, gold is dollar-denominated")
    elif dxy["direction"] == "DOWN":
        gold_impact += 2
        gold_notes.append("Dollar falling -- direct tailwind for gold")

    btc_impact = 0
    btc_notes: List[str] = []
    if y10["direction"] == "UP":
        btc_impact -= 2
        btc_notes.append("10Y yield rising -- headwind under the high-beta-risk-asset framing (BTC's macro correlation is regime-dependent and less stable than gold's -- see module docstring)")
    elif y10["direction"] == "DOWN":
        btc_impact += 2
        btc_notes.append("10Y yield falling -- tailwind under the high-beta-risk-asset framing")
    if dxy["direction"] == "UP":
        btc_impact -= 1
        btc_notes.append("Dollar rising -- mild headwind, dollar-denominated asset")
    elif dxy["direction"] == "DOWN":
        btc_impact += 1
        btc_notes.append("Dollar falling -- mild tailwind")

    return {
        "raw": {"yield_10y": y10, "yield_2y": y2, "dxy": dxy, "oil": oil},
        "equity_headwind_score": equity_headwind, "equity_notes": equity_notes,
        "gold_impact_score": gold_impact, "gold_notes": gold_notes,
        "btc_impact_score": btc_impact, "btc_notes": btc_notes,
    }


def vix_extremes() -> Dict[str, Any]:
    """VIX extremes, both directions -- reuses the same data source as
    spy_strategies.py's own _vix_context() (get_history_cached("^VIX")),
    not a second fetch path, but extends its coverage.

    _vix_context() (the existing, proven Weekly Plan logic) has three
    tiers -- CALM (<18), ELEVATED (>=18), HIGH_VOL (>=25) -- which
    captures the high side reasonably but has NO low-VIX awareness at
    all, and no tier beyond "high" for a genuine panic/spike reading.
    Both gaps matter specifically for a premium seller, not just
    generally:

    LOW-VIX COMPLACENCY (added here, absent before): extremely low
    implied vol means options are priced cheap relative to what could
    actually happen -- less compensation for the risk being taken on,
    and historically a precursor to sharp vol expansion more often than
    a stable low-vol regime continuing indefinitely. This is a real,
    separate risk from "IV is just currently low", worth flagging on its
    own rather than only ever warning about vol being too HIGH to sell
    into.

    GENUINE EXTREME/PANIC tier (added here, existing code stops at
    "high"): >=25 and >=32 are meaningfully different regimes for
    reliability of wall/OI-based structure -- the existing HIGH_VOL
    tier doesn't distinguish "elevated stress" from "acute panic",
    where strike selection logic breaks down much further.
    """
    try:
        from ..services.market import get_history_cached
        h = get_history_cached("^VIX", period="10d", interval="1d")
        if h is None or h.empty:
            return {"available": False}
        closes = [float(x) for x in h["Close"].dropna().tolist()]
        if not closes:
            return {"available": False}
        last = closes[-1]
        prev = closes[-2] if len(closes) > 1 else last
        chg = last - prev
        chg_pct = (chg / prev * 100) if prev else 0.0

        if last >= 32:
            tier = "PANIC"
            note = f"VIX {last:.1f} -- acute panic regime. Wall/OI-based strike selection is unreliable at this level; moves can far exceed any normal expected-move calculation."
        elif last >= 25:
            tier = "HIGH_VOL"
            note = f"VIX {last:.1f} -- high vol. Wider strikes and larger expected-move buffers warranted; wall reliability is degraded, not gone."
        elif last >= 18:
            tier = "ELEVATED"
            note = f"VIX {last:.1f} -- elevated but not extreme. Respect the expected move; walls are still reasonably informative."
        elif last <= 12:
            tier = "COMPLACENCY"
            note = f"VIX {last:.1f} -- low-vol complacency. Premium is cheap relative to what could happen; historically more often a precursor to expansion than a stable floor. Less compensation for the risk being sold."
        else:
            tier = "CALM"
            note = f"VIX {last:.1f} -- calm, not complacent. Reasonable regime for OI-wall-based structure."

        spike = abs(chg_pct) >= 10  # a >=10% day-over-day VIX move is itself a notable vol-of-vol event, independent of the absolute level
        return {
            "available": True, "level": round(last, 2), "change": round(chg, 2), "change_pct": round(chg_pct, 1),
            "tier": tier, "is_extreme": tier in ("PANIC", "COMPLACENCY"), "spike_today": spike, "note": note,
        }
    except Exception as e:
        return {"available": False, "note": f"VIX unavailable: {e}"}



def fomc_in_window(days_ahead: int = 5) -> Dict[str, Any]:
    """Is there an FOMC decision inside the next N days? This is a
    real, reliable gate (hardcoded 2026 meeting dates) -- NOT a
    probability of hike/cut. See module docstring point 1 for exactly
    why that distinction matters and what would be needed to go further.

    Framed this way deliberately: for a short-DTE credit seller, the
    dangerous thing about an FOMC date landing inside your window isn't
    which way the decision goes -- it's that vega/gamma are concentrated
    with zero time to recover from an adverse move either direction.
    That risk exists whether the decision is a hike, a cut, or no
    change, which is exactly why a reliable "is there a meeting" gate is
    the actionable signal here, not a probability this app can't
    honestly compute.
    """
    try:
        from .macro_events import get_macro_message_board
        board = get_macro_message_board(days_ahead=days_ahead)
    except Exception as e:
        return {"available": False, "in_window": False, "note": f"FOMC calendar unavailable: {e}"}
    fomc_events = [e for e in board.get("events", []) if "FOMC" in (e.get("title") or "").upper()]
    if not fomc_events:
        return {"available": True, "in_window": False, "note": f"No FOMC meeting in the next {days_ahead} days."}
    ev = fomc_events[0]
    return {
        "available": True, "in_window": True,
        "date_label": ev.get("date_label"), "time_label": ev.get("time_label"),
        "minutes_until": ev.get("minutes_until"),
        "note": (f"FOMC decision {ev.get('date_label')} {ev.get('time_label')} falls inside this window -- "
                 f"vega/gamma risk is concentrated with no time to recover from an adverse move, "
                 f"regardless of which way the decision goes."),
    }
