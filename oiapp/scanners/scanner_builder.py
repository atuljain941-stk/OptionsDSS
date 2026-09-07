from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from flask import Blueprint, jsonify, render_template, request

from ..scanners.watchlist_manager import _ensure_tables as _ensure_watchlist_tables
from ..services.fundamentals import beta_info, get_beta
from .earnings_calendar import get_earnings_info

scanner_builder_bp = Blueprint("scanner_builder_bp", __name__, url_prefix="/scanner-builder")
from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

# Hard ceiling on total scan wall-clock time (seconds). Configurable via
# env var for anyone who genuinely needs longer (a very large watchlist
# on a slow connection), but the point of having a default at all is that
# NO scan should be able to hang indefinitely -- see api_run()'s use of
# this alongside as_completed(timeout=...).
SCAN_DEADLINE_SECONDS = int(os.environ.get("OIAPP_SCAN_DEADLINE_SECONDS", "120"))

_SCHEMA_INIT_LOCK = threading.RLock()
_SCHEMA_INITIALIZED = False

TIMEFRAMES = ["5m", "15m", "1h", "2h", "4h", "1d", "1w", "1m", "3m"]
INDICATORS = [
    "close", "open", "high", "low", "volume",
    "rsi3", "rsi14", "ema5", "ema9", "ema13", "ema20", "ema50", "ema200",
    "ema_rsi14_13", "ema_rsi14_90", "rsi_diff_90", "macd", "macd_signal", "macd_hist",
    "relative_strength", "leadership", "mansfield_rs", "rs_rank", "beta", "earn_days", "earn_score",
    "oi", "pcr", "oi_change", "oi_change_pct", "pcr_change", "pcr_change_pct",
    "iv_est", "iv_rank", "iv_change", "flow_score", "flow_bias", "pcr_shift",
    "resistance", "support", "supportupper", "supportlower", "resistanceupper", "resistancelower", "touchcount", "breakoutstrength", "breakoutage",
    "distancefromresistance", "distancefromsupport", "atr", "atrcompression", "rangecompression", "volumedryup",
    "resistancestrength", "supportstrength", "failedbreakoutstrength", "failedbreakdownstrength", "volumeatlevel",
    "isath", "isatl", "athdistance", "atldistance", "fibresistance", "fibsupport",
]

FUNCTION_CATALOG = [
    {"name": "scan", "signature": "scan(name)", "description": "Expand a saved scanner by name"},
    {"name": "expand", "signature": "expand(name)", "description": "Insert a saved scanner's code block"},
    {"name": "lookback", "signature": "lookback(expr, bars)", "description": "True if expression was true in the last N bars"},
    {"name": "priorDay", "signature": "priorDay(expr, bars)", "description": "Expression value from N bars ago"},
    {"name": "Shift", "signature": "Shift(expr, bars)", "description": "Alias for priorDay(expr, bars)"},
    {"name": "ChangePct", "signature": "ChangePct(expr[, bars=1][, timeframe=1d])", "description": "Percent change over N bars; default bars=1"},
    {"name": "rsidiff90", "signature": "rsidiff90([period=90][, timeframe=1d])", "description": "rsi14 - EMA(rsi14, period); default period=90. Needs ~6x period valid bars to be trusted (e.g. ~540 daily bars, ~540 WEEKLY bars = 10+ years) -- returns null until then. Use rsidiff90sma on weekly/less-liquid symbols where that much history isn't available."},
    {"name": "rsidiff90sma", "signature": "rsidiff90sma([period=90][, timeframe=1d])", "description": "rsi14 - SMA(rsi14, period) -- same idea as rsidiff90 but with a simple moving average instead of an EMA, so it needs far less history to be trusted (~202 bars for the default period=90, vs rsidiff90's ~540) since a SMA's window is a hard cutoff rather than an exponentially-decaying one. Smoother/more lagging than the EMA version by design -- weights all bars in the window equally instead of favoring recent ones."},
    {"name": "CallWallStrike", "signature": "CallWallStrike([expiry=all])", "description": "The strike with the largest call open interest (the 'call wall') for the given expiry (e.g. \"2026-07-24\"), or across all expiries if omitted. Same wall definition used by Trade Opportunity Scanner and the Iron Condor candidate finder."},
    {"name": "PE", "signature": "PE([\"fwd\"|\"trailing\"] = \"fwd\")", "description": "Forward P/E by default, or trailing with PE(\"trailing\"). Meant to be combined with technical primitives in one query, e.g. RSIdiff90(\"1d\")<-20 and PE()<15 for \"oversold AND cheap\"."},
    {"name": "ProfitMargin", "signature": "ProfitMargin()", "description": "Profit margin %."},
    {"name": "RevenueGrowthPct", "signature": "RevenueGrowthPct()", "description": "YoY revenue growth %."},
    {"name": "EarningsGrowthPct", "signature": "EarningsGrowthPct()", "description": "YoY earnings growth %."},
    {"name": "AnalystRec", "signature": "AnalystRec()", "description": "Analyst consensus, 1=Strong Buy .. 5=Strong Sell, same scale as the Earnings page."},
    {"name": "AnalystUpside", "signature": "AnalystUpside()", "description": "% upside from current price to analyst mean price target."},
    {"name": "EpsRevisionTrend", "signature": "EpsRevisionTrend()", "description": "RISING, STABLE, or FALLING -- whether analysts have been revising the CURRENT quarter EPS estimate up or down over the last ~30 days. Direct numeric version of company-vs-analyst mismatch: a beat against a quietly-lowered estimate is a weaker signal than the beat% alone suggests. Same signal weighted into the Earnings page outlook score."},
    {"name": "EpsRevisionNet30d", "signature": "EpsRevisionNet30d()", "description": "Net analyst up-revisions minus down-revisions in the last 30 days. Negative means more analysts cutting than raising, e.g. EpsRevisionNet30d()<-3."},
    {"name": "IsDoji", "signature": "IsDoji([tf=1d])", "description": "Open approximately equals close -- small body relative to the bar's total range. Neutral/indecision candle."},
    {"name": "IsHammer", "signature": "IsHammer([tf=1d])", "description": "Small body near the TOP of the range, long lower wick, little/no upper wick. Bullish reversal signal, especially after a downtrend (combine with e.g. RSI<30 to check that context)."},
    {"name": "IsShootingStar", "signature": "IsShootingStar([tf=1d])", "description": "Small body near the BOTTOM of the range, long upper wick, little/no lower wick. Bearish reversal signal after an uptrend -- the mirror of a hammer."},
    {"name": "IsBullishEngulfing", "signature": "IsBullishEngulfing([tf=1d])", "description": "Prior bar bearish, current bar bullish, current bar's body fully engulfs the prior bar's body. 2-bar bullish reversal signal."},
    {"name": "IsBearishEngulfing", "signature": "IsBearishEngulfing([tf=1d])", "description": "Mirror of IsBullishEngulfing -- prior bullish, current bearish and engulfing."},
    {"name": "IsBullishHarami", "signature": "IsBullishHarami([tf=1d])", "description": "Prior candle has a large bearish body; current candle's entire body sits INSIDE the prior body -- containment, the opposite of engulfing. Signals the prior down-move losing momentum. 2-candle pattern, generally a stronger/more reliable signal than a single-candle pattern."},
    {"name": "IsBearishHarami", "signature": "IsBearishHarami([tf=1d])", "description": "Mirror of IsBullishHarami -- prior bullish, current body fully contained within it."},
    {"name": "IsPiercingLine", "signature": "IsPiercingLine([tf=1d])", "description": "Prior bearish, current bullish, opens below prior's low (gap down) then closes back up past the midpoint of prior's body -- but not all the way past prior's open (that would be IsBullishEngulfing instead). Partial penetration, a genuinely different and weaker-but-real reversal signal than full engulfing."},
    {"name": "IsDarkCloudCover", "signature": "IsDarkCloudCover([tf=1d])", "description": "Mirror of IsPiercingLine -- prior bullish, current bearish, opens above prior's high then closes back down past the midpoint of prior's body but not past prior's open."},
    {"name": "IsMorningStar", "signature": "IsMorningStar([tf=1d])", "description": "3-bar bullish reversal: large bearish candle, small-bodied 'star', then a large bullish candle closing back into the first bar's body."},
    {"name": "IsEveningStar", "signature": "IsEveningStar([tf=1d])", "description": "Mirror of IsMorningStar -- 3-bar bearish reversal."},
    {"name": "IsThreeWhiteSoldiers", "signature": "IsThreeWhiteSoldiers([tf=1d])", "description": "3 consecutive bullish candles, each closing higher, each opening within the prior bar's body. Bullish continuation."},
    {"name": "IsThreeBlackCrows", "signature": "IsThreeBlackCrows([tf=1d])", "description": "Mirror of IsThreeWhiteSoldiers -- bearish continuation."},
    {"name": "CandlePatternBullish", "signature": "CandlePatternBullish([tf=1d])", "description": "True if ANY bullish pattern (hammer, bullish engulfing, morning star, three white soldiers) fires on the current bar -- a category-level scan, matching a 'Bullish Scans' style filter without needing every pattern OR'd together manually."},
    {"name": "CandlePatternBearish", "signature": "CandlePatternBearish([tf=1d])", "description": "True if ANY bearish pattern (shooting star, bearish engulfing, evening star, three black crows) fires -- category-level 'Bearish Scans'."},
    {"name": "TrendlineBreak", "signature": "TrendlineBreak(side, lookback[, break_pct=0.3][, tf])", "description": "side is \"resistance\" or \"support\". Fits a straight line across highs (resistance) or lows (support) over the lookback window via linear regression, extrapolates one bar forward, and checks whether the current close has broken beyond it by break_pct%. This is a regression fit across the window, not a swing-point trendline drawn by eye -- useful for screening candidates, not a substitute for visually confirming the line."},
    {"name": "ChartPattern", "signature": "ChartPattern([lookback=40][, tf])", "description": "Detects the current swing-point-based chart pattern -- returns one of \"falling_wedge\", \"rising_wedge\", \"symmetrical_triangle\", \"ascending_triangle\", \"descending_triangle\", \"rising_channel\", \"falling_channel\", or None if nothing classifies cleanly. Fits a line through swing highs and one through swing lows, then classifies from the two slopes -- a candidate signal worth a manual chart check, not a certified pattern the way a human chartist draws one."},
    {"name": "ChartPatternBreakout", "signature": "ChartPatternBreakout(pattern[, lookback=40][, break_pct=0.3][, tf])", "description": "True only if the named pattern (see ChartPattern()) is BOTH currently classified AND price has broken out of it in that pattern's typical direction (up for falling wedges/ascending triangles, down for rising wedges/descending triangles, either way for symmetrical triangles/channels). This is the actual trade signal -- the pattern forming is context, the break is the event worth screening for, e.g. ChartPatternBreakout(\"falling_wedge\", 40)."},
    {"name": "IsDoubleTop", "signature": "IsDoubleTop([lookback=40][, tolerance_pct=2][, tf])", "description": "Two swing highs within tolerance_pct% of each other, with a swing low meaningfully below both in between -- classic bearish reversal setup."},
    {"name": "IsDoubleBottom", "signature": "IsDoubleBottom([lookback=40][, tolerance_pct=2][, tf])", "description": "Mirror of IsDoubleTop -- two swing lows at a similar level with a swing high between them, bullish reversal setup."},
    {"name": "WatchlistBreadth", "signature": "WatchlistBreadth(watchlist[, ma_period=20][, sector])", "description": "% of a watchlist's symbols trading above their own N-day moving average -- a market-health/regime signal, not a per-symbol one (same value returned for every symbol in the scan). Optional sector filter narrows it to one sector within that watchlist, e.g. WatchlistBreadth(\"Options Watchlist\", 50, \"Technology\"). Use as a regime gate on an otherwise per-symbol scan, e.g. WatchlistBreadth(\"Options Watchlist\")>60 and RSIdiff90(\"1d\")<-20."},
    {"name": "WatchlistAdvanceDecline", "signature": "WatchlistAdvanceDecline(watchlist[, sector=all])", "description": "Advance/decline ratio for a watchlist (optionally filtered to one sector): count of symbols whose latest close is above the prior close, divided by the count below. >1 means more advancers than decliners. Same watchlist-level (not per-symbol) semantics as WatchlistBreadth."},
    {"name": "PutWallStrike", "signature": "PutWallStrike([expiry=all])", "description": "The strike with the largest put open interest (the 'put wall') for the given expiry, or across all expiries if omitted."},
    {"name": "CallWallOI", "signature": "CallWallOI([expiry=all])", "description": "The open interest AT the call wall strike -- use this to filter for how strong/significant the wall is, e.g. CallWallOI(\"2026-07-24\") > 10000."},
    {"name": "PutWallOI", "signature": "PutWallOI([expiry=all])", "description": "The open interest AT the put wall strike."},
    {"name": "DistanceToCallWall", "signature": "DistanceToCallWall([expiry=all])", "description": "% distance from current price up to the call wall strike (positive = wall is above price). E.g. DistanceToCallWall(\"2026-07-24\") < 5 finds price sitting close under a call wall -- a classic short-call-spread setup."},
    {"name": "DistanceToPutWall", "signature": "DistanceToPutWall([expiry=all])", "description": "% distance from current price down to the put wall strike (positive = wall is below price)."},
    {"name": "InsideOiWalls", "signature": "InsideOiWalls([expiry=all])", "description": "True if price is currently trading between the put wall (below) and the call wall (above) -- the classic iron-condor setup: a range the market has already built substantial open interest around on both sides."},
    {"name": "CallWallStrength", "signature": "CallWallStrength([expiry=all])", "description": "What % of ALL call open interest for this expiry is concentrated at the call wall strike -- the actual significance measure a raw OI count can't give you. 10,000 contracts is a dominant wall if total call OI is 15,000, and background noise if it's 300,000. Use this instead of (or alongside) CallWallOI for a threshold that's comparable across different stocks, e.g. CallWallStrength(\"2026-07-24\")>25."},
    {"name": "PutWallStrength", "signature": "PutWallStrength([expiry=all])", "description": "Same as CallWallStrength but for the put wall -- % of all put OI for this expiry concentrated at the single put wall strike."},
    {"name": "CallWallBuildup", "signature": "CallWallBuildup([expiry][, days_back=7])", "description": "% change in OI at the call wall strike specifically, over the last N days (default 7). Distinct from CallWallStrength (how big the wall is right now): a strike can be the biggest wall on the board and be stale (built up months ago, nothing new this week), or genuinely fresh (rapid recent accumulation). Combine both: CallWallStrength(\"2026-07-24\")>25 and CallWallBuildup(\"2026-07-24\",7)>30 finds walls that are both big AND actively being built right now."},
    {"name": "PutWallBuildup", "signature": "PutWallBuildup([expiry][, days_back=7])", "description": "Same as CallWallBuildup but for the put wall."},
    {"name": "TotalOI", "signature": "TotalOI([expiry=all])", "description": "Total call+put open interest for the expiry -- a liquidity floor. Use alongside CallWallStrength/PutWallStrength so a 30% concentration on a name with only 2,000 contracts total (noise) doesn't pass the same filter as a 30% concentration on a name with 200,000 (a real signal), e.g. TotalOI(\"2026-07-24\")>50000."},
    {"name": "AvgAbsChangePct", "signature": "AvgAbsChangePct(bars[timeframe=1d])", "description": "EMA of absolute one-bar ChangePct over prior N bars"},
    {"name": "EMAAbsChangePct", "signature": "EMAAbsChangePct(bars[timeframe=1d])", "description": "Alias for AvgAbsChangePct; EMA(abs(ChangePct), N)"},
    {"name": "Abs", "signature": "Abs(expr)", "description": "Absolute value"},
    {"name": "Round", "signature": "Round(expr[, decimals=0])", "description": "Round a primitive/expression. Round(x) returns an integer; Round(x, 2) keeps two decimals."},
    {"name": "Between", "signature": "Between(value, low, high)", "description": "True when value is between low and high, inclusive"},
    {"name": "NotBetween", "signature": "NotBetween(value, low, high)", "description": "True when value is outside low/high, inclusive bounds"},
    {"name": "Slope", "signature": "Slope(expr, bars[timeframe=1d])", "description": "Raw endpoint slope per bar over the lookback window"},
    {"name": "SlopePct", "signature": "SlopePct(expr, bars[timeframe=1d])", "description": "Total endpoint percent move from expr[bars] to current, e.g. close vs close[10]"},
    {"name": "SlopeDeg", "signature": "SlopeDeg(expr, bars[timeframe=1d])", "description": "Raw endpoint angle: degrees(atan((current - expr[bars]) / bars))"},
    {"name": "SlopePctPerBar", "signature": "SlopePctPerBar(expr, bars[timeframe=1d])", "description": "Endpoint percent move divided by bars; old SlopePct behavior"},
    {"name": "SlopeDegPerBar", "signature": "SlopeDegPerBar(expr, bars[timeframe=1d])", "description": "Endpoint percent-per-bar angle; old SlopeDeg behavior"},
    {"name": "SlopeDegRaw", "signature": "SlopeDegRaw(expr, bars[timeframe=1d])", "description": "Alias for SlopeDeg; raw endpoint angle"},
    {"name": "SlopeATR", "signature": "SlopeATR(expr, bars[timeframe=1d])", "description": "Endpoint slope measured in ATRs per bar"},
    {"name": "SlopeATRDeg", "signature": "SlopeATRDeg(expr, bars[timeframe=1d])", "description": "Endpoint ATR-normalized slope angle in degrees"},
    {"name": "RegSlopePct", "signature": "RegSlopePct(expr, bars[timeframe=1d])", "description": "Least-squares regression slope over the lookback window, normalized to percent per bar"},
    {"name": "RegSlopeDeg", "signature": "RegSlopeDeg(expr, bars[timeframe=1d])", "description": "Least-squares regression angle over the lookback window, using percent-per-bar slope"},
    {"name": "RegSlopeATR", "signature": "RegSlopeATR(expr, bars[timeframe=1d])", "description": "Least-squares regression slope over the lookback window, measured in ATRs per bar"},
    {"name": "RegSlopeATRDeg", "signature": "RegSlopeATRDeg(expr, bars[timeframe=1d])", "description": "Least-squares regression ATR-normalized slope angle in degrees"},
    {"name": "ema", "signature": "ema(expr, period)", "description": "Exponential moving average"},
    {"name": "sma", "signature": "sma(expr, period)", "description": "Simple moving average"},
    {"name": "average", "signature": "average(expr, period)", "description": "Average of an expression"},
    {"name": "Min", "signature": "Min(a, b, c, ...)", "description": "Smallest value across arguments"},
    {"name": "Max", "signature": "Max(a, b, c, ...)", "description": "Largest value across arguments"},
    {"name": "Highest", "signature": "Highest(expr, bars[timeframe=1d])", "description": "Highest value of an expression"},
    {"name": "Lowest", "signature": "Lowest(expr, bars[timeframe=1d])", "description": "Lowest value of an expression"},
    {"name": "stddev", "signature": "stddev(expr, bars)", "description": "Standard deviation of an expression"},
    {"name": "CrossAbove", "signature": "CrossAbove(series, level[timeframe=1d])", "description": "True when a series crosses above a level"},
    {"name": "CrossOver", "signature": "CrossOver(series, level[timeframe=1d])", "description": "Alias for CrossAbove(series, level[, timeframe])"},
    {"name": "CrossBelow", "signature": "CrossBelow(series, level[timeframe=1d])", "description": "True when a series crosses below a level"},
    {"name": "CrossUnder", "signature": "CrossUnder(series, level[timeframe=1d])", "description": "Alias for CrossBelow(series, level[, timeframe])"},
    {"name": "LastSwingHigh", "signature": "LastSwingHigh(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "High price of the most recent confirmed swing-high pivot"},
    {"name": "LastSwingHighClose", "signature": "LastSwingHighClose(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Close of the most recent confirmed swing-high candle"},
    {"name": "LastSwingHighOpen", "signature": "LastSwingHighOpen(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Open of the most recent confirmed swing-high candle"},
    {"name": "LastSwingHighLow", "signature": "LastSwingHighLow(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Low of the most recent confirmed swing-high candle"},
    {"name": "LastSwingHighAge", "signature": "LastSwingHighAge(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Bars since the most recent confirmed swing high"},
    {"name": "DaysSinceSwingHigh", "signature": "DaysSinceSwingHigh(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Alias for LastSwingHighAge; on daily charts this is trading days"},
    {"name": "PullbackFromSwingHighPct", "signature": "PullbackFromSwingHighPct(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Percent pullback from the last swing-high price to the lowest low after that swing"},
    {"name": "PullbackFromSwingHighATR", "signature": "PullbackFromSwingHighATR(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Pullback depth from last swing high measured in current ATR(14) multiples"},
    {"name": "LastSwingLow", "signature": "LastSwingLow(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Low price of the most recent confirmed swing-low pivot"},
    {"name": "LastSwingLowClose", "signature": "LastSwingLowClose(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Close of the most recent confirmed swing-low candle"},
    {"name": "LastSwingLowOpen", "signature": "LastSwingLowOpen(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Open of the most recent confirmed swing-low candle"},
    {"name": "LastSwingLowHigh", "signature": "LastSwingLowHigh(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "High of the most recent confirmed swing-low candle"},
    {"name": "LastSwingLowAge", "signature": "LastSwingLowAge(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Bars since the most recent confirmed swing low"},
    {"name": "DaysSinceSwingLow", "signature": "DaysSinceSwingLow(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Alias for LastSwingLowAge; on daily charts this is trading days"},
    {"name": "BounceFromSwingLowPct", "signature": "BounceFromSwingLowPct(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Percent bounce from the last swing-low price to the highest high after that swing"},
    {"name": "BounceFromSwingLowATR", "signature": "BounceFromSwingLowATR(lookback=60[, left=2[, right=2[, timeframe=1d]]])", "description": "Bounce depth from last swing low measured in current ATR(14) multiples"},
    {"name": "HigherLowConfirmed", "signature": "HigherLowConfirmed([count=3[, minSeparationPct=0.5[, requirePriceIntact=1[, lookback=60[, left=2[, right=2[, timeframe]]]]]]])", "description": "True if the last `count` (default 3) confirmed swing lows are ALL monotonically ascending, each by at least minSeparationPct (default 0.5%, filters noise from a single small bounce). requirePriceIntact (default true) also fails the check if current price has already broken below the most recent confirmed low, even if no new pivot has had time to confirm yet. Purpose-built to replace hand-composed Shift()-based higher-low formulas, which are easy to get subtly wrong (mismatched lookback windows between the two sides, or only comparing 2 pivots instead of a real sequence)."},
    {"name": "LowerHighConfirmed", "signature": "LowerHighConfirmed([count=3[, minSeparationPct=0.5[, requirePriceIntact=1[, lookback=60[, left=2[, right=2[, timeframe]]]]]]])", "description": "Mirror of HigherLowConfirmed for swing highs -- true if the last `count` confirmed swing highs are all monotonically descending by at least minSeparationPct, with the same requirePriceIntact guard against confirmation lag."},
    {"name": "Score", "signature": "Score()", "description": "The Candle Context scanner's own 0-10 score for this symbol, computed fresh using its default criteria -- runs the SAME scoring pipeline the Candle Context scanner uses (not a separate reimplementation), so it can never drift out of sync. Returns null if no qualifying candle exists at this bar (Candle Context's own hard gate) or there isn't enough price history. Compose with other primitives, e.g. Score() > 6 and RSI14 < 70."},
    {"name": "ConfScore", "signature": "ConfScore()", "description": "How many independent scoring dimensions actually agree for this symbol right now (0-7), from the same Candle Context scoring pass as Score() -- a separate, stricter signal than the score itself (a high Score() can still have a low ConfScore() if one or two dimensions are carrying the total). ConfluenceRatio() returns the 0-1 ratio instead of the raw count."},
    {"name": "ChochBullish", "signature": "ChochBullish([lookback=60[, left=2[, right=2[, timeframe]]]])", "description": "Change of Character (bullish): the two most recent confirmed swing highs show a downtrend's pattern (descending highs), and current price has now closed above the more recent one -- the first crack in that pattern, not yet a confirmed new trend. ChochBearish is the mirror on swing lows."},
    {"name": "ChochBearish", "signature": "ChochBearish([lookback=60[, left=2[, right=2[, timeframe]]]])", "description": "Mirror of ChochBullish for a downtrend character-change: ascending lows broken to the downside."},
    {"name": "BosBullish", "signature": "BosBullish([lookback=60[, left=2[, right=2[, timeframe]]]])", "description": "Break of Structure (bullish): the pullback low is higher than the one before it (same check as HigherLowConfirmed) AND price has broken above the most recent swing high -- confirms the new uptrend structure, typically checked after ChochBullish. BosBearish is the mirror."},
    {"name": "BosBearish", "signature": "BosBearish([lookback=60[, left=2[, right=2[, timeframe]]]])", "description": "Mirror of BosBullish for downtrend structure confirmation."},
    {"name": "RetracementPct", "signature": "RetracementPct(direction[, lookback=60[, left=2[, right=2[, timeframe]]]])", "description": "How far current price has pulled back into the most recent impulsive swing leg, as a %: 0 = at the extreme just made, 100 = fully back to the leg's origin. direction is \"bullish\" or \"bearish\". Check against a band (e.g. RetracementPct(\"bullish\")>=50 and RetracementPct(\"bullish\")<=61.8) to define \"price is back in the demand/supply zone\" -- the standard Fibonacci-band definition, used here instead of committing to one specific order-block/candle definition. Combine with BosBullish for a full CHoCH -> BOS -> zone-entry query."},
    {"name": "BBUpper", "signature": "BBUpper([period=20[, mult=2.0[, timeframe]]])", "description": "Bollinger Band upper: SMA(period) + stdev(period)*mult. Defaults period=20, mult=2.0."},
    {"name": "BBLower", "signature": "BBLower([period=20[, mult=2.0[, timeframe]]])", "description": "Bollinger Band lower: SMA(period) - stdev(period)*mult."},
    {"name": "BBMiddle", "signature": "BBMiddle([period=20[, timeframe]])", "description": "Bollinger Band basis (the SMA itself)."},
    {"name": "BBWidth", "signature": "BBWidth([period=20[, mult=2.0[, timeframe]]])", "description": "Band width as a % of the middle band -- (upper-lower)/middle*100. A shrinking BBWidth is the classic Bollinger squeeze."},
    {"name": "BBPercent", "signature": "BBPercent([period=20[, mult=2.0[, timeframe]]])", "description": "%B: where price sits within the bands. 0 = at the lower band, 1 = at the upper band, can go outside 0-1 if price is beyond the bands entirely."},
    {"name": "KCUpper", "signature": "KCUpper([period=20[, atrPeriod=10[, mult=2.0[, timeframe]]]])", "description": "Keltner Channel upper: EMA(period) + ATR(atrPeriod)*mult. Defaults period=20, atrPeriod=10, mult=2.0 -- same defaults as the EMA_BB Pine indicator built earlier this session."},
    {"name": "KCLower", "signature": "KCLower([period=20[, atrPeriod=10[, mult=2.0[, timeframe]]]])", "description": "Keltner Channel lower: EMA(period) - ATR(atrPeriod)*mult."},
    {"name": "KCMiddle", "signature": "KCMiddle([period=20[, atrPeriod=10[, mult=2.0[, timeframe]]]])", "description": "Keltner Channel basis (the EMA itself)."},
    {"name": "SqueezeOn", "signature": "SqueezeOn([bbPeriod=20[, bbMult=2.0[, kcPeriod=20[, kcAtrPeriod=10[, kcMult=1.5[, timeframe]]]]]]])", "description": "TTM Squeeze (John Carter): true when the Bollinger Bands sit entirely INSIDE the Keltner Channel -- two different volatility measures (stdev-based vs ATR-based) agreeing the range has compressed unusually tight, historically a precursor to an expansion move either direction. Defaults bbPeriod=20/bbMult=2.0, kcPeriod=20/kcAtrPeriod=10/kcMult=1.5 (the standard TTM Squeeze parameters -- note kcMult defaults tighter than KCUpper/KCLower's own 2.0 default, since a looser KC would rarely contain the BB at all). Complements ATRCompression/RangeCompression/VolumeDryup as a different, well-known way to define the same 'coiled spring' concept."},
    {"name": "SectorStrength", "signature": "SectorStrength([period=20[, timeframe]])", "description": "Is THIS symbol's own sector (via its ETF proxy) outperforming SPY over the period -- the sector-rotation half of the analysis, computed per-symbol so it composes with SectorRS() in one query: SectorStrength()>0 and SectorRS()>0 finds stocks whose sector is strengthening AND who are themselves leading that sector, not just one or the other."},
    {"name": "BounceOffSwingHigh", "signature": "BounceOffSwingHigh([tolerancePct=0.5[, lookback=60[, left=2[, right=2[, timeframe]]]]])", "description": "Practical retest-and-reject: current bar's high touched (within tolerancePct%) a PRIOR confirmed swing high, and closed back below it -- a real rejection off resistance, not just the raw swing-high value. BounceOffSwingLow is the mirror at support."},
    {"name": "BounceOffSwingLow", "signature": "BounceOffSwingLow([tolerancePct=0.5[, lookback=60[, left=2[, right=2[, timeframe]]]]])", "description": "Mirror of BounceOffSwingHigh -- current bar's low touched a prior swing low and closed back above it."},
    {"name": "PinBarAtSupport", "signature": "PinBarAtSupport([tolerancePct=0.5[, lookback=60[, left=2[, right=2[, timeframe]]]]])", "description": "The practical version of a pin-bar reversal -- combines the existing IsHammer shape check (which is context-free on its own) with BounceOffSwingLow's proximity check, so this fires only when the hammer shape occurs AT a real prior swing low, not anywhere. PinBarAtResistance is the mirror (shooting-star shape at a prior swing high)."},
    {"name": "PinBarAtResistance", "signature": "PinBarAtResistance([tolerancePct=0.5[, lookback=60[, left=2[, right=2[, timeframe]]]]])", "description": "Mirror of PinBarAtSupport -- shooting-star shape occurring at a prior swing high."},
    {"name": "IsHeadAndShoulders", "signature": "IsHeadAndShoulders([shoulderTolerancePct=8[, lookback=30[, left=2[, right=2[, timeframe]]]]])", "description": "Classic topping pattern: chains 5 alternating swing pivots (shoulder-trough-head-trough-shoulder), requires the head to be the tallest of the 3 highs and the two shoulders within shoulderTolerancePct% of each other, then checks price has broken below the neckline (the line between the two troughs). IsInverseHeadAndShoulders is the bullish/bottoming mirror. This chains more moving parts than a 2-3 candle pattern, so it's a genuinely harder pattern to detect reliably -- worth verifying against charts you know before trusting it in a live scan. NecklineLevel() returns the actual neckline price level."},
    {"name": "IsInverseHeadAndShoulders", "signature": "IsInverseHeadAndShoulders([shoulderTolerancePct=8[, lookback=30[, left=2[, right=2[, timeframe]]]]])", "description": "Bullish/bottoming mirror of IsHeadAndShoulders -- head is the deepest of 3 swing lows, confirmed by price breaking above the neckline."},
    {"name": "IsBullFlag", "signature": "IsBullFlag([polePct=15[, poleBars=10[, flagBars=8[, flagMaxRetracePct=50[, timeframe=1d]]]]])", "description": "Sharp upward 'pole' move (min polePct% over poleBars bars) followed by a tight, controlled consolidation (the 'flag', flagBars bars) that gives back no more than flagMaxRetracePct% of the pole's gain, then confirmed only once price breaks out above the flag's high. IsBearFlag is the mirror. A continuation pattern, not a reversal -- the strongest confirmation is the pole itself being genuinely sharp and the flag genuinely tight/controlled, not a full retracement."},
    {"name": "IsBearFlag", "signature": "IsBearFlag([polePct=15[, poleBars=10[, flagBars=8[, flagMaxRetracePct=50[, timeframe=1d]]]]])", "description": "Bearish mirror of IsBullFlag -- sharp down-move pole, tight consolidation, confirmed on a breakdown below the flag's low."},
    {"name": "IsCupAndHandle", "signature": "IsCupAndHandle([cupLookback=60[, handleBars=10[, handleMaxPct=35[, rimTolerancePct=3[, timeframe=1d]]]]])", "description": "Simplified geometric approximation, not true curve-fitting -- worth knowing before trusting it. Finds a left rim (prior high), a cup bottom (lowest point after it), price recovering back within rimTolerancePct% of the rim, a shallow handle pullback (handleMaxPct% of the cup's depth, in the most recent handleBars), confirmed by price breaking above the left rim. This checks price levels and handle shallowness, not the cup's rounded smoothness, so it will accept a sharper V-shaped recovery a strict classic definition would reject."},
    {"name": "Retest", "signature": "Retest(level, tolerancePct, bars[timeframe=1d])", "description": "True when price retests a level after a cross"},
    {"name": "StrongBullCandle", "signature": "StrongBullCandle(lookback=20[, timeframe=1d[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "True when a prior bull candle ChangePct >= moveMult x EMA(abs(ChangePct), avgBars)"},
    {"name": "StrongBearCandle", "signature": "StrongBearCandle(lookback=20[, timeframe=1d[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "True when a prior bear candle ChangePct <= -moveMult x EMA(abs(ChangePct), avgBars)"},
    {"name": "StrongCandleAge", "signature": "StrongCandleAge(side, lookback=20[, timeframe[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "Bars ago for the most recent strong bull/bear anchor candle"},
    {"name": "StrongCandleHigh", "signature": "StrongCandleHigh(side, lookback=20[, timeframe[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "High of the most recent strong bull/bear anchor candle"},
    {"name": "StrongCandleLow", "signature": "StrongCandleLow(side, lookback=20[, timeframe[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "Low of the most recent strong bull/bear anchor candle"},
    {"name": "StrongCandleOpen", "signature": "StrongCandleOpen(side, lookback=20[, timeframe[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "Open of the most recent strong bull/bear anchor candle"},
    {"name": "StrongCandleClose", "signature": "StrongCandleClose(side, lookback=20[, timeframe[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "Close of the most recent strong bull/bear anchor candle"},
    {"name": "StrongCandleChangePct", "signature": "StrongCandleChangePct(side, lookback=20[, timeframe[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "One-bar percent change of the matched strong candle"},
    {"name": "StrongCandleAvgAbsChangePct", "signature": "StrongCandleAvgAbsChangePct(side, lookback=20[, timeframe[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "Prior EMA(abs(ChangePct), avgBars) baseline used by the matched strong candle"},
    {"name": "StrongCandleMoveMultiple", "signature": "StrongCandleMoveMultiple(side, lookback=20[, timeframe[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "abs(ChangePct) divided by prior EMA(abs(ChangePct), avgBars) for the matched strong candle"},
    {"name": "StrongCandleLevel", "signature": "StrongCandleLevel(side, level, lookback=20[, timeframe[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "Price level inside the strong candle: low/high/open/close/mid or 0-100 percent"},
    {"name": "TouchStrongCandleLevel", "signature": "TouchStrongCandleLevel(side, level, lookback=20[, tolerancePct=0.75][, timeframe[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "True when current bar touches a level from the strong candle"},
    {"name": "NearStrongCandleLevel", "signature": "NearStrongCandleLevel(side, level, lookback=20[, tolerancePct=0.75][, timeframe[, minBodyPct=50[, moveMult=2[, minVolMult=1.1[, avgBars=60]]]]])", "description": "Alias for TouchStrongCandleLevel"},
    {"name": "SecondLegUp", "signature": "SecondLegUp(lookback=45[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Potential M-top: price is retesting a prior swing high after a pullback"},
    {"name": "SecondLegDown", "signature": "SecondLegDown(lookback=45[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Potential W-bottom: price is retesting a prior swing low after a bounce"},
    {"name": "LooseMPattern", "signature": "LooseMPattern(lookback=45[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Alias for SecondLegUp / loose double-top pattern"},
    {"name": "LooseWPattern", "signature": "LooseWPattern(lookback=45[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Alias for SecondLegDown / loose double-bottom pattern"},
    {"name": "DoubleTop", "signature": "DoubleTop(lookback=45[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Alias for SecondLegUp"},
    {"name": "DoubleBottom", "signature": "DoubleBottom(lookback=45[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Alias for SecondLegDown"},
    {"name": "SecondLegScore", "signature": "SecondLegScore(side, lookback[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "0-100 quality score for loose M/W second-leg pattern"},
    {"name": "SecondLegLevel", "signature": "SecondLegLevel(side, which, lookback[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Pattern level: first, second, neckline, midpoint, target"},
    {"name": "SecondLegFirst", "signature": "SecondLegFirst(side, lookback[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "First swing high/low price in the M/W pattern"},
    {"name": "SecondLegSecond", "signature": "SecondLegSecond(side, lookback[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Second-leg retest price in the M/W pattern"},
    {"name": "SecondLegNeckline", "signature": "SecondLegNeckline(side, lookback[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Valley/peak neckline between the two legs"},
    {"name": "SecondLegMatchPct", "signature": "SecondLegMatchPct(side, lookback[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Percent distance between first swing and second-leg retest"},
    {"name": "SecondLegSwingPct", "signature": "SecondLegSwingPct(side, lookback[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Depth/height of the pullback between first leg and neckline"},
    {"name": "SecondLegAge", "signature": "SecondLegAge(side, lookback[, timeframe=1d[, tolerancePct=5.0[, minSwingPct=3.0[, recentBars=3]]]])", "description": "Bars since the second-leg retest was touched"},
    {"name": "UAERegime", "signature": "UAERegime([timeframe=1d])", "description": "UAE regime label: BULL, WEAK_BULL, BEAR, WEAK_BEAR, or SIDEWAYS"},
    {"name": "IsUAERegime", "signature": "IsUAERegime(regime[timeframe=1d])", "description": "True when the selected timeframe is in a UAE regime"},
    {"name": "UAEBull", "signature": "UAEBull([timeframe=1d])", "description": "True when UAE regime is BULL"},
    {"name": "UAEWeakBull", "signature": "UAEWeakBull([timeframe=1d])", "description": "True when UAE regime is WEAK_BULL"},
    {"name": "UAEBear", "signature": "UAEBear([timeframe=1d])", "description": "True when UAE regime is BEAR"},
    {"name": "UAEWeakBear", "signature": "UAEWeakBear([timeframe=1d])", "description": "True when UAE regime is WEAK_BEAR"},
    {"name": "UAESideways", "signature": "UAESideways([timeframe=1d])", "description": "True when UAE regime is SIDEWAYS"},
    {"name": "UAERegimeScore", "signature": "UAERegimeScore([timeframe=1d])", "description": "0-100 quality/confidence score for the current UAE regime"},
    {"name": "UAEADX", "signature": "UAEADX([timeframe=1d])", "description": "UAE smoothed ADX value for the selected timeframe"},
    {"name": "UAEADXRising", "signature": "UAEADXRising([timeframe=1d])", "description": "True when UAE smoothed ADX is rising"},
    {"name": "UAETrending", "signature": "UAETrending([timeframe=1d])", "description": "True when RSIdiff is beyond +/-12 or UAE ADX is above the timeframe threshold"},
    {"name": "UAERSIDiff", "signature": "UAERSIDiff([timeframe=1d])", "description": "Exact UAE RSIdiff value: RSI(14) minus EMA(RSI,90)"},
    {"name": "UAEMACD", "signature": "UAEMACD([timeframe=1d])", "description": "Exact UAE custom MACD line from the Pine Trend/Vol Analyzer"},
    {"name": "UAESignal", "signature": "UAESignal([timeframe=1d])", "description": "Exact UAE custom signal line from the Pine Trend/Vol Analyzer"},
    {"name": "UAEHist", "signature": "UAEHist([timeframe=1d])", "description": "UAE custom histogram value"},
    {"name": "UAEHistThreshold", "signature": "UAEHistThreshold([timeframe=1d])", "description": "60th percentile strong-hist threshold over the 100-bar UAE lookback"},
    {"name": "UAEStrongHist", "signature": "UAEStrongHist([timeframe=1d])", "description": "True when abs(UAE hist) is above the strong-hist percentile threshold"},
    {"name": "UAEHistGrowing", "signature": "UAEHistGrowing([timeframe=1d])", "description": "True when absolute UAE histogram is growing"},
    {"name": "MACDDiff", "signature": "MACDDiff([timeframe=1d])", "description": "MACD line minus MACD signal line; same as MACD histogram"},
    {"name": "MACDSpread", "signature": "MACDSpread([timeframe=1d])", "description": "Alias for MACDDiff"},
    {"name": "MACDGap", "signature": "MACDGap([timeframe=1d])", "description": "Alias for MACDDiff"},
    {"name": "MACDDiffAbs", "signature": "MACDDiffAbs([timeframe=1d])", "description": "Absolute distance between MACD and signal"},
    {"name": "MACDDiffPct", "signature": "MACDDiffPct([timeframe=1d])", "description": "MACD minus signal as percent of close"},
    {"name": "MACDSpreadRatio", "signature": "MACDSpreadRatio([lookback=20][, timeframe=1d])", "description": "Abs MACD spread divided by its average abs spread over lookback"},
    {"name": "MACDDiffShrinkPct", "signature": "MACDDiffShrinkPct([bars=1][, timeframe=1d])", "description": "Percent shrink in abs MACD-signal gap versus N bars ago; negative means expanding"},
    {"name": "MACDSpreadStable", "signature": "MACDSpreadStable([side,][maxShrinkPct=25.0[, bars=1]][, timeframe=1d])", "description": "True when MACD spread has not shrunk much; optional bull/bear side"},
    {"name": "MACDFarFromSignal", "signature": "MACDFarFromSignal([side,] minPct=0.25[, timeframe=1d])", "description": "True when MACD is still meaningfully separated from signal"},
    {"name": "UAEDiamond", "signature": "UAEDiamond(side[timeframe=1d])", "description": "Optional UAE strong-hist diamond for side bull/bear"},
    {"name": "UAETrendTriangle", "signature": "UAETrendTriangle([side=bull][, timeframe=1d[, mode=confirmed]])", "description": "Visible Pine v5 trend triangle: MACD zero-cross + UAE trending + strong hist, suppressed on fade-arrow bars. Weekly/monthly default to confirmed bars; pass \"live\" to include the forming bar."},
    {"name": "UAETrendTriangleAge", "signature": "UAETrendTriangleAge([side=bull][, timeframe=1d[, bars=20]])", "description": "Bars ago for the most recent visible UAE trend triangle; weekly/monthly use confirmed bars by default"},
    {"name": "UAETrendTriangleDate", "signature": "UAETrendTriangleDate([side=bull][, timeframe=1d[, bars=20]])", "description": "Date of the most recent visible UAE trend triangle inside the lookback window"},
    {"name": "UAELastMarker", "signature": "UAELastMarker([timeframe=1d])", "description": "Last visible UAE marker on that timeframe: TREND_BULL, TREND_BEAR, MRT_BUY, MRT_SELL, or blank"},
    {"name": "UAELastMarkerAge", "signature": "UAELastMarkerAge([timeframe=1d[, bars=20]])", "description": "Bars ago for the most recent visible UAE marker"},
    {"name": "UAESolidTriangle", "signature": "UAESolidTriangle(side[timeframe=1d])", "description": "Alias for UAETrendTriangle"},
    {"name": "UAEWeakTriangle", "signature": "UAEWeakTriangle(side[timeframe=1d])", "description": "Legacy helper: MACD zero-cross while trending but below strong-hist threshold"},
    {"name": "UAEFadeArrow", "signature": "UAEFadeArrow([side=bull][, timeframe=1d[, mode=confirmed]])", "description": "Visible Pine v5 fade/MRT arrow: bear = RSIdiff crosses below +20; bull = RSIdiff crosses above -20. Weekly/monthly default to confirmed bars; pass \"live\" to include the forming bar."},
    {"name": "UAEFadeArrowAge", "signature": "UAEFadeArrowAge([side=bull][, timeframe=1d[, bars=20]])", "description": "Bars ago for the most recent visible UAE fade/MRT arrow"},
    {"name": "UAEMRTArrow", "signature": "UAEMRTArrow(side[timeframe=1d])", "description": "Alias for UAEFadeArrow"},
    {"name": "UAECircle", "signature": "UAECircle(side[timeframe=1d])", "description": "Optional UAE histogram zero-cross circle"},
    {"name": "UAEConfluence", "signature": "UAEConfluence([side=bull][, timeframe=1d[, bars=1]])", "description": "Counts trend triangle, circle, diamond, and fade-arrow signals over the lookback window"},
    {"name": "UAEHigherTFAligned", "signature": "UAEHigherTFAligned(side, entryTimeframe)", "description": "True when the guide's higher timeframe bias supports the entry side"},
    {"name": "UAEMultiTFAligned", "signature": "UAEMultiTFAligned(side, entryTimeframe)", "description": "True when the next one/two higher UAE timeframes align with the side"},
    {"name": "RSIDiff", "signature": "RSIDiff(period[timeframe=1d])", "description": "RSI minus its EMA"},
    {"name": "RSIDiff90", "signature": "RSIDiff90([timeframe=1d])", "description": "RSI minus its 90 EMA"},
    {"name": "Sector", "signature": "Sector()", "description": "Current stock sector ETF code/name. Filter examples: sector equals XLC or SectorName() equals Technology"},
    {"name": "SectorRS", "signature": "SectorRS([period=20[, timeframe]])", "description": "Stock return minus its sector ETF return over the lookback period; positive means outperforming sector"},
    {"name": "RelativeStrength", "signature": "RelativeStrength(benchmark=SPY[, period=90[, timeframe=1d]])", "description": "Stock return minus benchmark return over the lookback period"},
    {"name": "MansfieldRS", "signature": "MansfieldRS([period=52[, benchmark=SPY[, timeframe=1d]]])", "description": "Mansfield-style RS line vs benchmark"},
    {"name": "RSRank", "signature": "RSRank([period=252[, benchmark[, timeframe=1d]]])", "description": "Relative-strength percentile rank"},
    {"name": "IVRank", "signature": "IVRank([timeframe=1d])", "description": "Implied-volatility rank / fear percentile (proxy from historical vol)"},
    {"name": "IVChange", "signature": "IVChange([timeframe=1d])", "description": "Change in implied-volatility rank / fear factor"},
    {"name": "FlowScore", "signature": "FlowScore([timeframe=1d])", "description": "Composite weekly options flow score"},
    {"name": "FlowBias", "signature": "FlowBias([timeframe=1d])", "description": "Composite flow bias: BULL, BEAR, or NEUTRAL"},
    {"name": "PCRShift", "signature": "PCRShift([bars=1])", "description": "Change in put/call ratio over the lookback window"},
    {"name": "Resistance", "signature": "Resistance(period[timeframe=1d])", "description": "Rolling resistance level"},
    {"name": "Support", "signature": "Support(period[timeframe=1d])", "description": "Rolling support level"},
    {"name": "ResistanceUpper", "signature": "ResistanceUpper(period[timeframe=1d])", "description": "Upper edge of the selected resistance zone"},
    {"name": "ResistanceLower", "signature": "ResistanceLower(period[timeframe=1d])", "description": "Lower edge of the selected resistance zone"},
    {"name": "SupportUpper", "signature": "SupportUpper(period[timeframe=1d])", "description": "Upper edge of the selected support zone"},
    {"name": "SupportLower", "signature": "SupportLower(period[timeframe=1d])", "description": "Lower edge of the selected support zone"},
    {"name": "InsideResistanceZone", "signature": "InsideResistanceZone(period[timeframe=1d])", "description": "True when price is inside the selected resistance zone"},
    {"name": "InsideSupportZone", "signature": "InsideSupportZone(period[timeframe=1d])", "description": "True when price is inside the selected support zone"},
    {"name": "TouchCount", "signature": "TouchCount(level, tolerance, bars[timeframe=1d])", "description": "Touch count around a level"},
    {"name": "BreakoutStrength", "signature": "BreakoutStrength([timeframe=1d])", "description": "Quality of the breakout candle"},
    {"name": "BreakoutAge", "signature": "BreakoutAge(level, direction, bars[timeframe=1d])", "description": "Bars since breakout"},
    {"name": "DistanceFromResistance", "signature": "DistanceFromResistance(period[timeframe=1d])", "description": "Distance from rolling resistance"},
    {"name": "DistanceFromSupport", "signature": "DistanceFromSupport(period[timeframe=1d])", "description": "Distance from rolling support"},
    {"name": "ATRCompression", "signature": "ATRCompression([period=14[, timeframe=1d]])", "description": "ATR compression score"},
    {"name": "RangeCompression", "signature": "RangeCompression([period=20[, timeframe=1d]])", "description": "Range compression score"},
    {"name": "VolumeDryup", "signature": "VolumeDryup([period=20[, timeframe=1d]])", "description": "Volume dry-up score"},
    {"name": "ResistanceStrength", "signature": "ResistanceStrength([timeframe=1d]) -- NOTE: period/bars/tolerance are accepted but currently unused by the implementation; only timeframe has any effect", "description": "Composite resistance strength score"},
    {"name": "SupportStrength", "signature": "SupportStrength([timeframe=1d]) -- NOTE: period/bars/tolerance are accepted but currently unused by the implementation; only timeframe has any effect", "description": "Composite support strength score"},
    {"name": "FailedBreakoutStrength", "signature": "FailedBreakoutStrength([timeframe=1d]) -- NOTE: period/bars/tolerance are accepted but currently unused by the implementation; only timeframe has any effect", "description": "Failed breakout trap score"},
    {"name": "BreakoutFailedStrength", "signature": "BreakoutFailedStrength([timeframe=1d]) -- NOTE: period/bars/tolerance are accepted but currently unused by the implementation; only timeframe has any effect", "description": "Alias for failed breakout trap score"},
    {"name": "FailedBreakdownStrength", "signature": "FailedBreakdownStrength([timeframe=1d]) -- NOTE: period/bars/tolerance are accepted but currently unused by the implementation; only timeframe has any effect", "description": "Failed breakdown reclaim score"},
    {"name": "VolumeAtLevel", "signature": "VolumeAtLevel(level[, tolerance=0.01[, bars=60[, timeframe=1d]]])", "description": "Average volume at a level"},
    {"name": "OIChange", "signature": "OIChange([bars=1])", "description": "Open interest change"},
    {"name": "OIChangePct", "signature": "OIChangePct([bars=1])", "description": "Open interest percent change"},
    {"name": "PCRChange", "signature": "PCRChange([bars=1])", "description": "Put/call ratio change"},
    {"name": "PCRChangePct", "signature": "PCRChangePct([bars=1])", "description": "Put/call ratio percent change"},
    {"name": "EarningsDays", "signature": "EarningsDays()", "description": "Days until next earnings"},
    {"name": "EarningsScore", "signature": "EarningsScore()", "description": "Cached earnings score"},
    {"name": "EarningsQualityScore", "signature": "EarningsQualityScore()", "description": "0-10 composite of 5 of the Earnings page's 6 factors -- Business Quality, Earnings (beat rate/surprise), Guidance (EPS revision proxy), Long-term Growth, Near-term Momentum. Deliberately excludes Analyst Sentiment, which stays separately queryable via AnalystRec()/AnalystUpside(). NOT the same primitive as EarningsScore() -- that one is a calendar-timing-risk score (days to next earnings + beat streak), this one is a fundamentals-quality composite. Same underlying math as the Earnings page's own recommendation model, not a separate implementation."},
    {"name": "Beta", "signature": "Beta()", "description": "Cached stock beta"},
    {"name": "ConvictionScore", "signature": "ConvictionScore([side=bull][, timeframe=1d[, lookback=20]])", "description": "Composite conviction score from OI, sector, market, RS and flow"},
    {"name": "ConvictionBreakdown", "signature": "ConvictionBreakdown([side=bull][, timeframe=1d[, lookback=20]])", "description": "Component breakdown for the conviction score"},
    {"name": "WPattern", "signature": "WPattern(bars[, timeframe=1d[, strictness=2]])", "description": "Bullish W / double-bottom pattern confirmation"},
    {"name": "WPatternStrength", "signature": "WPatternStrength(bars[, timeframe=1d[, strictness=2]])", "description": "0-100 strength score for a W pattern"},
    {"name": "MPattern", "signature": "MPattern(bars[, timeframe=1d[, strictness=2]])", "description": "Bearish M / double-top pattern confirmation"},
    {"name": "MPatternStrength", "signature": "MPatternStrength(bars[, timeframe=1d[, strictness=2]])", "description": "0-100 strength score for an M pattern"},
    {"name": "PatternBreakout", "signature": "PatternBreakout(patternType, bars[, timeframe=1d[, strictness=2]])", "description": "Confirm a W or M pattern breakout/breakdown"},
    {"name": "IsATH", "signature": "IsATH([proximityPct=2.0[, lookbackSpec=52w]])", "description": "Near all-time or rolling highs"},
    {"name": "IsATL", "signature": "IsATL([proximityPct=2.0[, lookbackSpec=52w]])", "description": "Near all-time or rolling lows"},
    {"name": "ATHDistance", "signature": "ATHDistance([lookbackSpec=52w])", "description": "Distance to ATH"},
    {"name": "ATLDistance", "signature": "ATLDistance([lookbackSpec=52w])", "description": "Distance to ATL"},
    {"name": "FibResistance", "signature": "FibResistance(level, swingLookback, timeframe)", "description": "Fibonacci resistance projection"},
    {"name": "FibSupport", "signature": "FibSupport(level, swingLookback, timeframe)", "description": "Fibonacci support projection"},
    {"name": "StrongBullBar", "signature": "StrongBullBar([timeframe=1d])", "description": "Strong bullish candle on high participation"},
    {"name": "StrongBearBar", "signature": "StrongBearBar([timeframe=1d])", "description": "Strong bearish candle on high participation"},
    {"name": "StrongCandle", "signature": "StrongCandle([timeframe=1d])", "description": "Strong directional candle on high participation"},
    {"name": "SignalBar", "signature": "SignalBar(lookbackBars[timeframe=1d])", "description": "High-volume low-move signal bar"},
    {"name": "SignalBarHigh", "signature": "SignalBarHigh(lookbackBars[timeframe=1d])", "description": "High of the most recent signal bar"},
    {"name": "SignalBarLow", "signature": "SignalBarLow(lookbackBars[timeframe=1d])", "description": "Low of the most recent signal bar"},
]

# Enrich autocomplete metadata with return type/unit/help. Keep the core
# catalog compact above and apply metadata here so every API/catalog consumer
# gets the same hover-help contract.
_FUNCTION_RETURN_OVERRIDES = {
    "scan": {"return_type": "boolean", "format": "raw", "unit": "true/false", "help": "Expands a saved scanner query. Usually used inside a larger boolean expression."},
    "expand": {"return_type": "query", "format": "text", "unit": "query text", "help": "Inserts a saved scanner code block rather than a numeric value."},
    "lookback": {"return_type": "boolean", "format": "raw", "unit": "true/false", "help": "True if the inner expression was true at least once in the requested prior bars."},
    "priorDay": {"return_type": "same as expression", "format": "raw", "unit": "shifted value", "help": "Returns the expression value from N bars ago."},
    "Shift": {"return_type": "same as expression", "format": "raw", "unit": "shifted value", "help": "Alias for priorDay(expr, bars)."},
    "ChangePct": {"return_type": "percent", "format": "pct1", "unit": "% change", "help": "Percent change over the requested bars. +5 means +5%, not 0.05."},
    "AvgAbsChangePct": {"return_type": "percent", "format": "pct1", "unit": "% per bar", "help": "EMA baseline of absolute one-bar percent moves."},
    "EMAAbsChangePct": {"return_type": "percent", "format": "pct1", "unit": "% per bar", "help": "Alias for AvgAbsChangePct; returns a percent baseline."},
    "Abs": {"return_type": "number", "format": "number", "unit": "same as input", "help": "Absolute value of the input expression."},
    "Round": {"return_type": "number", "format": "number", "unit": "same as input", "help": "Rounded numeric value."},
    "Between": {"return_type": "boolean", "format": "raw", "unit": "true/false", "help": "True when value is between low and high, inclusive."},
    "NotBetween": {"return_type": "boolean", "format": "raw", "unit": "true/false", "help": "True when value is outside the low/high bounds."},
    "Slope": {"return_type": "number", "format": "number", "unit": "raw units/bar", "help": "Raw endpoint slope per bar: (current - value[bars]) / bars. For price this is dollars per bar; for RSI it is RSI points per bar.", "examples": ["Slope(close, 5, \"1d\") > 1"]},
    "SlopePct": {"return_type": "percent", "format": "pct2", "unit": "% total move", "help": "Total endpoint percent move from expr[bars] to the current bar: ((current / value[bars]) - 1) * 100. For close, SlopePct(close, 10, \"1d\") is close vs close[10].", "examples": ["SlopePct(close, 10, \"1d\") > 2"]},
    "SlopeDeg": {"return_type": "degree", "format": "number1", "unit": "degrees", "help": "Raw endpoint angle from expr[bars] to current: degrees(atan((current - value[bars]) / bars)). For close, SlopeDeg(close, 10, \"1d\") uses (current close - close[10]) / 10; it is not percent-normalized and it is not a regression.", "examples": ["Between(SlopeDeg(close, 10, \"1d\"), -30, 30)", "SlopeDeg(close, 10, \"1d\") > 45"]},
    "SlopePctPerBar": {"return_type": "percent", "format": "pct2", "unit": "% per bar", "help": "Endpoint percent move divided by bars. This preserves the old SlopePct behavior: total percent move / bars.", "examples": ["SlopePctPerBar(close, 10, \"1d\") > 0.5"]},
    "SlopeDegPerBar": {"return_type": "degree", "format": "number1", "unit": "degrees", "help": "Endpoint angle using percent move per bar. This preserves the old SlopeDeg behavior; +1% per bar is about +45 degrees.", "examples": ["SlopeDegPerBar(close, 10, \"1d\") < 35"]},
    "SlopeDegRaw": {"return_type": "degree", "format": "number1", "unit": "degrees", "help": "Alias for SlopeDeg. Raw endpoint angle: degrees(atan((current - value[bars]) / bars)).", "examples": ["SlopeDegRaw(close, 5, \"1d\") < 45"]},
    "SlopeATR": {"return_type": "number", "format": "number2", "unit": "ATR/bar", "help": "Endpoint slope from expr[bars] to current, measured in current ATR multiples per bar.", "examples": ["SlopeATR(close, 10, \"1d\") > 0.25"]},
    "SlopeATRDeg": {"return_type": "degree", "format": "number1", "unit": "degrees", "help": "Endpoint ATR-normalized slope angle. Useful for vertical/exhaustion filters without price-scale bias.", "examples": ["SlopeATRDeg(close, 10, \"1d\") < 35"]},
    "RegSlopePct": {"return_type": "percent", "format": "pct2", "unit": "% per bar", "help": "Least-squares regression slope over the lookback window, normalized to percent per bar. This preserves the old smoothed SlopePct behavior.", "examples": ["RegSlopePct(close, 10, \"1d\") > 0.5"]},
    "RegSlopeDeg": {"return_type": "degree", "format": "number1", "unit": "degrees", "help": "Least-squares regression angle over the lookback window using percent-per-bar slope. This preserves the old smoothed SlopeDeg behavior.", "examples": ["RegSlopeDeg(close, 10, \"1d\") < 35"]},
    "RegSlopeATR": {"return_type": "number", "format": "number2", "unit": "ATR/bar", "help": "Least-squares regression slope measured in ATR multiples per bar.", "examples": ["RegSlopeATR(close, 10, \"1d\") > 0.25"]},
    "RegSlopeATRDeg": {"return_type": "degree", "format": "number1", "unit": "degrees", "help": "Least-squares regression ATR-normalized slope angle in degrees.", "examples": ["RegSlopeATRDeg(close, 10, \"1d\") < 35"]},
    "ema": {"return_type": "number", "format": "number", "unit": "same as input", "help": "Exponential moving average of the input expression."},
    "sma": {"return_type": "number", "format": "number", "unit": "same as input", "help": "Simple moving average of the input expression."},
    "average": {"return_type": "number", "format": "number", "unit": "same as input", "help": "Average of the input expression."},
    "Min": {"return_type": "number", "format": "number", "unit": "same as inputs", "help": "Smallest numeric argument."},
    "Max": {"return_type": "number", "format": "number", "unit": "same as inputs", "help": "Largest numeric argument."},
    "Highest": {"return_type": "number", "format": "number", "unit": "same as input", "help": "Highest value over the lookback. Highest(high, 20, \"1d\") returns a price."},
    "Lowest": {"return_type": "number", "format": "number", "unit": "same as input", "help": "Lowest value over the lookback. Lowest(low, 20, \"1d\") returns a price."},
    "stddev": {"return_type": "number", "format": "number", "unit": "same as input", "help": "Standard deviation of the input expression."},
    "CrossAbove": {"return_type": "boolean", "format": "raw", "unit": "true/false", "help": "True only on the bar where the series crosses above the level."},
    "CrossOver": {"return_type": "boolean", "format": "raw", "unit": "true/false", "help": "Alias for CrossAbove."},
    "CrossBelow": {"return_type": "boolean", "format": "raw", "unit": "true/false", "help": "True only on the bar where the series crosses below the level."},
    "CrossUnder": {"return_type": "boolean", "format": "raw", "unit": "true/false", "help": "Alias for CrossBelow."},
}

_TEXT_RETURN_NAMES = {
    "Sector", "SectorName", "SectorETF", "UAERegime", "UAELastMarker", "FlowBias",
    "ConvictionBreakdown", "UAETrendTriangleDate",
}
_BOOLEAN_PREFIXES = ("Is", "Touch", "Near", "Inside")
_BOOLEAN_NAMES = {"Between", "NotBetween", "CrossAbove", "CrossOver", "CrossBelow", "CrossUnder", "Retest", "UAEBull", "UAEWeakBull", "UAEBear", "UAEWeakBear", "UAESideways", "UAEADXRising", "UAETrending", "UAEStrongHist", "UAEHistGrowing", "UAEDiamond", "UAETrendTriangle", "UAESolidTriangle", "UAEWeakTriangle", "UAEFadeArrow", "UAEMRTArrow", "UAECircle", "UAEHigherTFAligned", "UAEMultiTFAligned", "StrongBullCandle", "StrongBearCandle", "StrongBullBar", "StrongBearBar", "StrongCandle", "SignalBar", "SecondLegUp", "SecondLegDown", "LooseMPattern", "LooseWPattern", "DoubleTop", "DoubleBottom", "WPattern", "MPattern", "PatternBreakout"}

def _infer_function_return_meta(item: Dict[str, Any]) -> Dict[str, Any]:
    name = str(item.get("name") or "")
    lname = name.lower()
    if name in _FUNCTION_RETURN_OVERRIDES:
        return dict(_FUNCTION_RETURN_OVERRIDES[name])
    if name in _TEXT_RETURN_NAMES or lname.endswith("date") or lname.endswith("marker") or lname.endswith("bias") or lname.endswith("regime"):
        return {"return_type": "text", "format": "text", "unit": "label", "help": "Returns a text label; compare with quoted strings."}
    if name in _BOOLEAN_NAMES or any(name.startswith(pfx) for pfx in _BOOLEAN_PREFIXES):
        return {"return_type": "boolean", "format": "raw", "unit": "true/false", "help": "Returns true/false and can be used directly in AND/OR filters."}
    if any(k in lname for k in ("age", "days", "count")):
        return {"return_type": "integer", "format": "integer", "unit": "bars/days/count", "help": "Returns a whole-number count such as bars ago, days, or touches."}
    if any(k in lname for k in ("high", "low", "open", "close", "support", "resistance", "fib", "level", "strike")) and "pct" not in lname and "percent" not in lname:
        return {"return_type": "price", "format": "price", "unit": "price level", "help": "Returns a price/level value."}
    if any(k in lname for k in ("pct", "percent", "distance", "pullback", "bounce", "change")):
        return {"return_type": "percent", "format": "pct1", "unit": "%", "help": "Returns a percent value. +5 means +5%, not 0.05."}
    if any(k in lname for k in ("score", "strength", "compression", "dryup", "rank", "adx", "rsi", "macd", "hist", "beta", "rs")):
        return {"return_type": "number", "format": "number", "unit": "score/value", "help": "Returns a numeric value/score. Check examples for the typical threshold range."}
    return {"return_type": "number", "format": "number", "unit": "numeric value", "help": "Returns a numeric value."}

for _fn in FUNCTION_CATALOG:
    _meta = _infer_function_return_meta(_fn)
    for _k, _v in _meta.items():
        _fn.setdefault(_k, _v)
    _fn.setdefault("help", _fn.get("description", ""))

FUNCTION_SIGNATURES = [item["signature"] for item in FUNCTION_CATALOG]


BUILTIN_SCANNERS = [
    {"name": "Large OI Change", "category": "Options flow", "description": "Spot unusual open interest expansion", "query_text": 'OIChange() > 1000 OR OIChangePct() > 15'},
    {"name": "PCR Change", "category": "Options flow", "description": "Find sharp put/call ratio shifts", "query_text": 'PCRChangePct() > 10 OR PCRChange() > 0.1'},
    {"name": "Weekly Pin", "category": "Options flow", "description": "Weekly or monthly pinning levels", "query_text": 'lookback(OIChange() > 0, 3) AND lookback(PCRChange() > 0, 3)'},
    {"name": "Weekly Flow Structure", "category": "Weekly", "description": "Composite 1-6 week options flow, IV, and price structure", "query_text": 'FlowScore() >= 60 AND IVRank() <= 80 AND volume[1d] > ema(volume, 20) AND (close[1d] > ema20[1d] OR close[1d] < ema20[1d])'},
    {"name": "Weekly Bull Flow", "category": "Weekly", "description": "Bullish weekly flow and accumulation", "query_text": 'FlowBias() = "BULL" AND FlowScore() >= 60'},
    {"name": "Weekly Bear Flow", "category": "Weekly", "description": "Bearish weekly flow and distribution", "query_text": 'FlowBias() = "BEAR" AND FlowScore() >= 60'},
    {"name": "Opportunity Scanner", "category": "Trade ideas", "description": "Credit spread candidates", "query_text": 'rsi14[1d] > 50 AND leadership > 60'},
    {"name": "SR Breakout", "category": "Price action", "description": "Support/resistance zone breakouts", "query_text": 'close[1d] > ema20[1d] AND close[1d] > ema50[1d] AND volume[1d] > ema(volume, 20) AND CrossAbove(close, ResistanceUpper(20, "1d"), "1d")'},
    {"name": "SR Breakout Age", "category": "Price action", "description": "Recent breakout follow-through", "query_text": 'scan(SR Breakout) AND lookback(close[1d] > ema20[1d], 5) AND CrossAbove(close, ResistanceUpper(20, "1d"), "1d")'},
    {"name": "Trend Exhaustion", "category": "Price action", "description": "Late-stage trend exhaustion", "query_text": 'rsi14[1d] > 75 OR rsi14[1d] < 25'},
    {"name": "Second Pullback", "category": "Price action", "description": "Second pullback / trend reset", "query_text": 'scan(Trend Exhaustion) AND lookback(rsi14[1d] > 50, 5)'},
    {"name": "Momentum Retrace", "category": "Momentum", "description": "Momentum pullbacks with strength", "query_text": 'close[1d] > ema20[1d] AND rsi14[1d] > 40 AND rsi14[1d] < 70'},
    {"name": "RSI MTF", "category": "Momentum", "description": "Multi-timeframe RSI setups", "query_text": 'rsi14[1d] > 50 AND rsi14[1w] > 50'},
    {"name": "RSI Diff Momentum", "category": "Momentum", "description": "RSI minus its 90 EMA plus weekly resistance context", "query_text": 'RSIDiff90() >= 20 AND DistanceFromResistance(20, "1w") <= 1'},
    {"name": "Weekly MACD Intact Daily Dip", "category": "Momentum / Swing", "description": "Weekly RSIDiff was hot recently and weekly MACD gap remains intact, but daily price pulled back sharply", "query_text": 'Lookback(RSIDiff90("1w") >= 20, 8) AND MACDSpread("1w") > 0 AND MACDSpreadRatio(20, "1w") >= 1.0 AND MACDSpreadStable("bull", 25, 1, "1w") AND Between(ChangePct(close, 5, "1d"), -18, -4) AND close[1d] > ema50[1d]'},
    {"name": "Weekly MACD Intact EMA20 Dip", "category": "Momentum / Swing", "description": "Same swing pullback, with daily price dipping into the EMA20 area", "query_text": 'Lookback(RSIDiff90("1w") >= 20, 8) AND MACDSpread("1w") > 0 AND MACDSpreadStable("bull", 25, 1, "1w") AND Between(ChangePct(close, 5, "1d"), -18, -4) AND low[1d] <= ema20[1d] * 1.03 AND close[1d] > ema50[1d]'},
    {"name": "Weekly Bear MACD Intact Daily Bounce", "category": "Momentum / Swing", "description": "Weekly downside momentum remains intact, but daily price bounced sharply into possible short setup", "query_text": 'Lookback(RSIDiff90("1w") <= -20, 8) AND MACDSpread("1w") < 0 AND MACDSpreadRatio(20, "1w") >= 1.0 AND MACDSpreadStable("bear", 25, 1, "1w") AND Between(ChangePct(close, 5, "1d"), 4, 18) AND close[1d] < ema50[1d]'},
    {"name": "Relative Strength Leaders", "category": "Relative strength", "description": "High RS rank names with sustained outperformance", "query_text": 'RelativeStrength("SPY", 126) > 10 AND RSRank(252) >= 80'},
    {"name": "Sector Relative Strength", "category": "Relative strength", "description": "Names outperforming their own sector ETF", "query_text": 'SectorRS(20) > 2 AND RelativeStrength("SPY", 20) > 0'},
    {"name": "XLC Momentum Filter", "category": "Relative strength", "description": "Example sector filter plus RSI momentum", "query_text": 'sector="XLC" AND RSIDiff90() > 20'},
    {"name": "Relative Weakness", "category": "Relative strength", "description": "Weak names underperforming the benchmark", "query_text": 'RelativeStrength("SPY", 126) < -10 AND RSRank(252) <= 20'},
    {"name": "Regime Scan", "category": "Regime", "description": "Market regime and bias", "query_text": 'rsi14[1d] > 50 AND rsi14[1w] > 50 AND relative_strength[1d] > 50'},
    {"name": "UAE Bull Regime", "category": "UAE", "description": "Symbols in clean UAE bull regime on Daily", "query_text": 'UAEBull("1d") AND UAERegimeScore("1d") >= 60'},
    {"name": "UAE Weak Bull Pullback", "category": "UAE", "description": "Bullish trend pullback state; watch for resumption", "query_text": 'UAEWeakBull("1d") AND UAERegimeScore("1d") >= 55'},
    {"name": "UAE Bear Regime", "category": "UAE", "description": "Symbols in clean UAE bear regime on Daily", "query_text": 'UAEBear("1d") AND UAERegimeScore("1d") >= 60'},
    {"name": "UAE Weak Bear Bounce", "category": "UAE", "description": "Bearish trend relief bounce state; watch for rollover", "query_text": 'UAEWeakBear("1d") AND UAERegimeScore("1d") >= 55'},
    {"name": "UAE Sideways", "category": "UAE", "description": "No-trend/chop state by UAE ADX threshold", "query_text": 'UAESideways("1d")'},
    {"name": "UAE 1H Bull Entry Confluence", "category": "UAE", "description": "1H bull signal with 4H bias support", "query_text": 'UAEHigherTFAligned("bull", "1h") AND UAEConfluence("bull", "1h", 3) >= 2 AND NOT UAESideways("1h")'},
    {"name": "UAE 1H Bear Entry Confluence", "category": "UAE", "description": "1H bear signal with 4H bias support", "query_text": 'UAEHigherTFAligned("bear", "1h") AND UAEConfluence("bear", "1h", 3) >= 2 AND NOT UAESideways("1h")'},
    {"name": "Institutional Scan", "category": "Flow", "description": "Institutional accumulation / distribution", "query_text": 'volume[1d] > ema(volume, 20) AND leadership > 60'},
    {"name": "Iron Condor Candidates", "category": "Options strategies", "description": "Defined-risk neutral setups", "query_text": 'PCRChangePct() < 10 AND OIChange() > 0 AND rsi14[1d] > 40 AND rsi14[1d] < 60'},
    {"name": "VCP Compression", "category": "Price action", "description": "Volatility contraction before expansion", "query_text": 'ATRCompression(14) < 70 AND RangeCompression(20) < 70 AND VolumeDryup(20) < 80'},
    {"name": "Signal Bar Bull Break", "category": "Price action", "description": "High-volume quiet bar then upside confirmation", "query_text": "SignalBar(2, \"1d\") AND CrossAbove(close, SignalBarHigh(2, \"1d\"))"},
    {"name": "Signal Bar Bear Break", "category": "Price action", "description": "High-volume quiet bar then downside confirmation", "query_text": "SignalBar(2, \"1d\") AND CrossBelow(close, SignalBarLow(2, \"1d\"))"},
    {"name": "Shallow Pullback Swing High Break", "category": "Price action / Swing", "description": "Bullish: shallow pullback after a recent swing high, then close crosses back above the swing-high candle close", "query_text": 'Between(DaysSinceSwingHigh(80, 2, 2, "1d"), 2, 10) AND PullbackFromSwingHighATR(80, 2, 2, "1d") <= 1.5 AND close[1d] > ema20[1d] AND CrossAbove(close, LastSwingHighClose(80, 2, 2, "1d"), "1d")'},
    {"name": "Shallow Bounce Swing Low Break", "category": "Price action / Swing", "description": "Bearish: shallow bounce after a recent swing low, then close crosses below the swing-low candle close", "query_text": 'Between(DaysSinceSwingLow(80, 2, 2, "1d"), 2, 10) AND BounceFromSwingLowATR(80, 2, 2, "1d") <= 1.5 AND close[1d] < ema20[1d] AND CrossBelow(close, LastSwingLowClose(80, 2, 2, "1d"), "1d")'},
    {"name": "Strong RS Swing Breakout", "category": "Price action / Swing", "description": "Market/sector leader crossing above recent swing-high candle close after controlled pullback", "query_text": 'RelativeStrength("SPY", 20, "1d") > 0 AND SectorRS(20, "1d") > 0 AND Between(DaysSinceSwingHigh(80, 2, 2, "1d"), 2, 10) AND PullbackFromSwingHighATR(80, 2, 2, "1d") <= 1.5 AND CrossAbove(close, LastSwingHighClose(80, 2, 2, "1d"), "1d")'},
    {"name": "Higher Low Pullback (1h)", "category": "Price action / Swing", "description": "Bullish continuation entry: latest confirmed swing low sits above the prior swing low (structure improving), with the last 2 candles closing up to confirm buyers stepping back in. Stop sits just below the new swing low.", "query_text": 'LastSwingLow(60, 2, 2, "1h") > Shift(LastSwingLow(60, 2, 2, "1h"), LastSwingLowAge(60, 2, 2, "1h") + 3) AND Between(LastSwingLowAge(60, 2, 2, "1h"), 2, 6) AND close[1h] > open[1h] AND Shift(close[1h], 1) > Shift(open[1h], 1)'},
    {"name": "Lower High Pullback (1h)", "category": "Price action / Swing", "description": "Bearish continuation entry: latest confirmed swing high sits below the prior swing high (structure weakening), with the last 2 candles closing down to confirm sellers stepping back in. Stop sits just above the new swing high.", "query_text": 'LastSwingHigh(60, 2, 2, "1h") < Shift(LastSwingHigh(60, 2, 2, "1h"), LastSwingHighAge(60, 2, 2, "1h") + 3) AND Between(LastSwingHighAge(60, 2, 2, "1h"), 2, 6) AND close[1h] < open[1h] AND Shift(close[1h], 1) < Shift(open[1h], 1)'},
    {"name": "Strong Candle", "category": "Price action", "description": "Strong directional candle on participation", "query_text": "StrongCandle(\"1d\")"},
    {"name": "Tight Consolidation", "category": "Price action", "description": "Low volatility coil", "query_text": 'DistanceFromResistance(20) < 2 AND ATRCompression(14) < 60 AND VolumeDryup(20) < 70'},
    {"name": "Breakout Expansion", "category": "Price action", "description": "Compression followed by breakout", "query_text": 'scan(VCP Compression) AND CrossAbove(close, ResistanceUpper(20, "1d"), "1d") AND BreakoutStrength() > 0.75 AND volume[1d] > ema(volume,20)*1.5'},
    {"name": "Controlled Volume Breakout", "category": "Price action", "description": "Resistance breakout after compression with volume support and not-too-vertical slope", "query_text": 'scan(VCP Compression) AND CrossAbove(close, ResistanceUpper(20, "1d"), "1d") AND BreakoutStrength("1d") >= 0.75 AND volume[1d] >= ema(volume, 20) * 1.5 AND SlopeDegPerBar(close, 5, "1d") < 35 AND UAEADXRising("1d")'},
    {"name": "Exhausted Breakout Risk", "category": "Price action", "description": "High-slope resistance break without volume confirmation; watch for fallback", "query_text": 'CrossAbove(close, ResistanceUpper(20, "1d"), "1d") AND SlopeDegPerBar(close, 5, "1d") >= 45 AND volume[1d] < ema(volume, 20) * 1.2 AND (NOT UAEHistGrowing("1d") OR RSIDiff90("1w") > 20)'},
    {"name": "10-Bar Up Move", "category": "Momentum / Change", "description": "Price gained more than 10% over the last 10 daily bars", "query_text": 'ChangePct(close, 10, "1d") > 10'},
    {"name": "10-Bar Down Move", "category": "Momentum / Change", "description": "Price fell more than 10% over the last 10 daily bars", "query_text": 'ChangePct(close, 10, "1d") < -10'},
    {"name": "Strong Weekly Resistance", "category": "Price action", "description": "Strong weekly wall near price", "query_text": 'DistanceFromResistance(20, "1w") <= 1 AND ResistanceStrength(20, "1w") >= 70'},
    {"name": "Failed Breakout Trap", "category": "Price action", "description": "Breakout that failed back under resistance", "query_text": 'BreakoutFailedStrength(20, "1w") >= 60 AND DistanceFromResistance(20, "1w") <= 1'},
    {"name": "Strong Support Reclaim", "category": "Price action", "description": "Support defended and reclaimed", "query_text": 'SupportStrength(20, "1w") >= 70 AND DistanceFromSupport(20, "1w") <= 1'},
    {"name": "Strong Bull Candle Retest Low", "category": "Price action / Regime", "description": "Bull/Weak Bull names pulling back to the low of a prior strong bull candle", "query_text": '(UAEBull("1d") OR UAEWeakBull("1d")) AND StrongBullCandle(20, "1d", 55, 2, 1.2, 60) AND TouchStrongCandleLevel("bull", "low", 20, 0.75, "1d", 55, 2, 1.2, 60)'},
    {"name": "Strong Bull Candle Retest Mid", "category": "Price action / Regime", "description": "Bull/Weak Bull names retesting the midpoint of a prior strong bull candle", "query_text": '(UAEBull("1d") OR UAEWeakBull("1d")) AND StrongBullCandle(20, "1d", 55, 2, 1.2, 60) AND TouchStrongCandleLevel("bull", 50, 20, 0.75, "1d", 55, 2, 1.2, 60)'},
    {"name": "Strong Bear Candle Retest High", "category": "Price action / Regime", "description": "Bear/Weak Bear names bouncing into the high of a prior strong bear candle", "query_text": '(UAEBear("1d") OR UAEWeakBear("1d")) AND StrongBearCandle(20, "1d", 55, 2, 1.2, 60) AND TouchStrongCandleLevel("bear", "high", 20, 0.75, "1d", 55, 2, 1.2, 60)'},
    {"name": "Strong Bear Candle Retest Mid", "category": "Price action / Regime", "description": "Bear/Weak Bear names retesting the midpoint of a prior strong bear candle", "query_text": '(UAEBear("1d") OR UAEWeakBear("1d")) AND StrongBearCandle(20, "1d", 55, 2, 1.2, 60) AND TouchStrongCandleLevel("bear", 50, 20, 0.75, "1d", 55, 2, 1.2, 60)'},
    {"name": "Second Leg Up / M Top Zone", "category": "Price action / Reversal", "description": "Loose M/double-top retest: price rallied back near a prior swing high after a pullback", "query_text": 'SecondLegUp(45, "1d", 5, 3, 3) AND SecondLegScore("up", 45, "1d") >= 60'},
    {"name": "Second Leg Down / W Bottom Zone", "category": "Price action / Reversal", "description": "Loose W/double-bottom retest: price sold back near a prior swing low after a bounce", "query_text": 'SecondLegDown(45, "1d", 5, 3, 3) AND SecondLegScore("down", 45, "1d") >= 60'},
    {"name": "Confirmed M Breakdown", "category": "Price action / Reversal", "description": "Potential M top confirmed by a move below the neckline", "query_text": 'SecondLegUp(60, "1d", 6, 3, 5) AND close[1d] < SecondLegNeckline("up", 60, "1d", 6, 3, 5)'},
    {"name": "Confirmed W Breakout", "category": "Price action / Reversal", "description": "Potential W bottom confirmed by a move above the neckline", "query_text": 'SecondLegDown(60, "1d", 6, 3, 5) AND close[1d] > SecondLegNeckline("down", 60, "1d", 6, 3, 5)'},
    {"name": "Bear Mean Reversion Second Leg", "category": "Price action / Reversal", "description": "Second leg up into a prior high with stretched RSI-diff and weak/growing less momentum", "query_text": 'SecondLegUp(45, "1d", 5, 3, 3) AND RSIDiff90("1d") > 8 AND NOT UAEHistGrowing("1d")'},
    {"name": "Bull Mean Reversion Second Leg", "category": "Price action / Reversal", "description": "Second leg down into a prior low with negative RSI-diff and easing downside momentum", "query_text": 'SecondLegDown(45, "1d", 5, 3, 3) AND RSIDiff90("1d") < -8 AND NOT UAEHistGrowing("1d")'},
    {"name": "ATH Momentum", "category": "Price action", "description": "Near all-time highs with momentum", "query_text": 'IsATH(2, 52w) AND RSIDiff90() >= 20'},
    {"name": "ATL Breakdown", "category": "Price action", "description": "Near all-time lows with weakness", "query_text": 'IsATL(2, 52w) AND RSIDiff90() <= -20'},
    {"name": "Fib Extension Breakout", "category": "Price action", "description": "Breakout projected to next fib extension", "query_text": 'IsATH(2, 52w) AND FibResistance(1.272, 52, "1w") > close[1d]'},
    {"name": "Fib Reversal Support", "category": "Price action", "description": "Reversal support near fib retracement", "query_text": 'IsATL(2, 52w) AND FibSupport(0.786, 52, "1w") < close[1d]'},
    {"name": "High Beta Momentum", "category": "Risk", "description": "Momentum names with elevated beta", "query_text": 'Beta() > 1.5 AND close[1d] > ema20[1d] AND volume[1d] > ema(volume,20)'},
    {"name": "Low Beta Mean Reversion", "category": "Risk", "description": "Defensive low-beta setups", "query_text": 'Beta() < 0.8 AND rsi14[1d] < 45 AND close[1d] > SupportUpper(20, "1d")'},
    {"name": "Bear MRT Credit Call", "category": "MRT / Credit Spreads", "description": "Strong trend near resistance with cooling momentum", "query_text": 'EarningsDays() > 30 AND RSIDiff90() > 8 AND Shift(SMA(RSIDiff90(),8),5) > 20 AND SMA(RSIDiff90(),5) < 12 AND (IsATH(5, "52w") OR DistanceFromResistance(20, "1w") <= 10) AND ResistanceStrength(20, "1w") >= 20'},
    {"name": "Bull MRT Credit Put", "category": "MRT / Credit Spreads", "description": "Strong downtrend near support with cooling momentum", "query_text": 'EarningsDays() > 30 AND RSIDiff90() < -8 AND Shift(SMA(RSIDiff90(),8),5) < -20 AND SMA(RSIDiff90(),5) > -12 AND (IsATL(5, "52w") OR DistanceFromSupport(20, "1w") <= 10) AND SupportStrength(20, "1w") >= 20'},
    {"name": "Iron Condor", "category": "MRT / Credit Spreads", "description": "Range-bound setups for neutral premium selling", "query_text": 'EarningsDays() > 30 AND Abs(RSIDiff90()) <= 10 AND ResistanceStrength(20, "1w") >= 20 AND SupportStrength(20, "1w") >= 20 AND DistanceFromResistance(20, "1w") > 5 AND DistanceFromSupport(20, "1w") > 5 AND ATRCompression(14) <= 85 AND VolumeDryup(20) <= 85'},
    {"name": "Bull Momentum Credit Put", "category": "Momentum Spreads", "description": "Breakout plus retest for bullish premium selling", "query_text": 'EarningsDays() > 30 AND CrossAbove(close, ResistanceUpper(20, "1d"), "1d") AND Retest(ResistanceUpper(20, "1d"), 1, 5, "1d") AND ChangePct(close) > 2 * SMA(Abs(ChangePct(close)), 60) AND SlopeDeg(SMA(RSIDiff90(),20), 20, "1d") > 0'},
    {"name": "Bear Momentum Credit Call", "category": "Momentum Spreads", "description": "Breakdown plus retest for bearish premium selling", "query_text": 'EarningsDays() > 30 AND CrossBelow(close, SupportLower(20, "1d"), "1d") AND Retest(SupportLower(20, "1d"), 1, 5, "1d") AND ChangePct(close) < -2 * SMA(Abs(ChangePct(close)), 60) AND SlopeDeg(SMA(RSIDiff90(),20), 20, "1d") < 0'},
    {"name": "Failed Breakout", "category": "Momentum Spreads", "description": "Useful for bear call spreads", "query_text": 'CrossAbove(close, ResistanceUpper(20, "1d"), "1d") AND FailedBreakoutStrength(ResistanceUpper(20, "1d"), 10) >= 40 AND RSIDiff90() < 10'},
    {"name": "First Pullback After Breakout", "category": "Momentum Spreads", "description": "High-quality bull put spread setup", "query_text": 'CrossAbove(close, ResistanceUpper(20, "1d"), "1d") AND Retest(ResistanceUpper(20, "1d"), 1, 10, "1d") AND RSIDiff90() > 10 AND SlopeDeg(SMA(RSIDiff90(),20), 20, "1d") > 0'},
    # --- Minervini SEPA / Trend Template pipeline -----------------------
    # Stage 1: structural filter only (Minervini's 8-point Trend Template).
    # sma()/ema() args are (expr, period[, timeframe]); Highest/Lowest args
    # are (expr, bars[, timeframe]) -- all confirmed against this file's
    # generic series-function evaluator before use, to avoid the
    # StrongCandleLevel-style argument-shift bug. 52-week window uses 252
    # daily bars. Criterion 3 (200D MA sloping up, min ~1 month) uses
    # SlopeDegPerBar for a percent-normalized angle rather than raw SlopeDeg,
    # matching how the rest of this file grades trend/momentum slope.
    {"name": "Minervini Trend Template", "category": "Price action / Trend", "description": "Minervini's 8-point structural trend filter: price/50/150/200D MA stack, rising 200D MA, price 30%+ above 52w low and within 25% of 52w high, RS rank top ~30%", "query_text": 'close[1d] > sma(close, 150, "1d") AND close[1d] > sma(close, 200, "1d") AND sma(close, 150, "1d") > sma(close, 200, "1d") AND SlopeDegPerBar(sma(close, 200, "1d"), 21, "1d") > 0 AND sma(close, 50, "1d") > sma(close, 150, "1d") AND sma(close, 50, "1d") > sma(close, 200, "1d") AND close[1d] > sma(close, 50, "1d") AND close[1d] >= Lowest(low, 252, "1d") * 1.30 AND close[1d] >= Highest(high, 252, "1d") * 0.75 AND RSRank(252) >= 70'},
    # Stage 2: SEPA entry setup -- Trend Template qualifies the name, VCP
    # Compression (existing scanner: ATRCompression/RangeCompression/
    # VolumeDryup) supplies the "volatility contraction" pattern, and
    # VolumeDryup below 70 confirms the tightening is on fading volume
    # (not fresh distribution). No earnings/catalyst leg -- see note below
    # for equities vs. commodities (MGC/GC, SPY, QQQ) usage.
    {"name": "Minervini SEPA Setup", "category": "Price action / Swing", "description": "Trend Template qualified name showing VCP-style volatility contraction on drying volume -- pre-breakout watchlist state", "query_text": 'scan(Minervini Trend Template) AND scan(VCP Compression) AND VolumeDryup(20) < 70'},
    # Stage 3: execution trigger -- breakout above the contraction range on
    # expanding volume, slope capped so entries aren't chased too far past
    # the pivot (mirrors "Controlled Volume Breakout" already in this file).
    {"name": "Minervini SEPA Breakout Trigger", "category": "Price action / Swing", "description": "SEPA setup confirmed: breaks contraction-range resistance on expanding volume without an already-vertical slope", "query_text": 'scan(Minervini SEPA Setup) AND CrossAbove(close, ResistanceUpper(20, "1d"), "1d") AND volume[1d] >= ema(volume, 20) * 1.5 AND SlopeDegPerBar(close, 5, "1d") < 35'},
    # --- V-Reversal / "right side of the V" pipeline --------------------
    # Translates a discretionary checklist (undercut of base + reclaim,
    # back above key EMAs, wedge pop, volume off lows, retest-and-hold)
    # into existing primitives. SecondLegDown/SecondLegNeckline is the
    # closest available proxy for "undercut of base and reclaim" -- it is
    # a tolerance-banded retest of a prior swing low, not a purpose-built
    # undercut/spring detector, so treat it as a reasonable proxy worth a
    # manual chart check, same caveat this file already gives ChartPattern.
    # RSIDiff90's own ~540-bar warm-up requirement (see its primitive
    # description) applies here too -- young tickers will show null, not
    # a false negative.
    #
    # Stage 2 and 3 each inline the Stage 1 (SecondLegDown) condition
    # directly rather than chaining scan() into a Lookback(), because
    # SecondLegDown/scan() are same-bar zone checks (proven safe by this
    # file's own "Confirmed W Breakout" pattern) whereas the EMA retest
    # in Stage 3 happens on a LATER bar than the reclaim -- mixing scan()
    # with Lookback() across different bars would silently evaluate scan()
    # at the current bar only, not "at any point in the lookback window",
    # so inlining avoids that timing ambiguity entirely.
    {"name": "V Reversal Base", "category": "Price action / Reversal", "description": "Real selloff (RSIDiff90 was meaningfully negative recently) and price is currently in a W-bottom second-leg/undercut zone -- not yet reclaimed, watchlist-only stage", "query_text": 'Lookback(RSIDiff90("1d") <= -15, 15) AND SecondLegDown(45, "1d", 5, 3, 3)'},
    {"name": "V Reversal Reclaim", "category": "Price action / Reversal", "description": "Undercut zone breaks its neckline, price reclaims both key EMAs, volume expands off the low, and RSIDiff90 is turning up -- the actual turn signal, not the exact low", "query_text": 'SecondLegDown(45, "1d", 5, 3, 3) AND close[1d] > SecondLegNeckline("down", 45, "1d", 5, 3, 3) AND close[1d] > ema20[1d] AND close[1d] > ema50[1d] AND volume[1d] > ema(volume, 20) * 1.3 AND RSIDiff90() > Shift(RSIDiff90(), 10)'},
    {"name": "V Reversal EMA Retest Hold", "category": "Price action / Reversal", "description": "Reclaim happened within the last 10 bars, and price has now pulled back to retest the EMA20 and held -- sacrifices some of the initial move for a higher-probability, lower-risk entry", "query_text": 'Lookback(SecondLegDown(45, "1d", 5, 3, 3) AND close[1d] > SecondLegNeckline("down", 45, "1d", 5, 3, 3) AND close[1d] > ema20[1d] AND close[1d] > ema50[1d], 10) AND Retest(ema20[1d], 1.5, 8, "1d") AND close[1d] > ema20[1d] AND RSIDiff90() > -10'},
    {"name": "V Reversal Wedge Pop Confluence", "category": "Price action / Reversal", "description": "V Reversal Base plus a falling-wedge pattern breakout -- optional bonus confluence, not required for the core signal", "query_text": 'scan(V Reversal Base) AND close[1d] > SecondLegNeckline("down", 45, "1d", 5, 3, 3) AND ChartPatternBreakout("falling_wedge", 40)'},
    # --- 3-month (quarterly) EMA13 bounce -------------------------------
    # "3m" timeframe: 3 raw monthly bars resampled into one quarterly bar
    # (pandas "3ME" rule, cached-resample path added to
    # _resampled_history_cached). ema(expr, period, tf) is the generic
    # primitive already used throughout this file (confirmed via its
    # evaluator branch before use) -- no new primitive needed, just the
    # new timeframe. Two variants: EXACT requires the quarterly low at or
    # below EMA13; TOLERANCE allows the low to sit within 1% above EMA13
    # too, since real data rarely touches a moving average to the exact
    # tick -- exact touches are the rarer, higher-conviction case.
    {"name": "EMA13 Bounce 3M Exact", "category": "Price action / Trend", "description": "Quarterly close is above EMA13(3M) and the quarterly low touched or dipped through it -- an exact touch, not just a close nearby", "query_text": 'close[3m] > ema(close, 13, "3m") AND low[3m] <= ema(close, 13, "3m")'},
    {"name": "EMA13 Bounce 3M Tolerance", "category": "Price action / Trend", "description": "Same as EMA13 Bounce 3M Exact but allows the quarterly low to sit within 1% above EMA13 as a near-touch, since real data rarely hits a moving average to the exact tick", "query_text": 'close[3m] > ema(close, 13, "3m") AND low[3m] <= ema(close, 13, "3m") * 1.01'},
]

BUILTIN_SCANNER_QUERY_MAP = {item["name"].lower(): item.get("query_text", "") for item in BUILTIN_SCANNERS}

DEFAULT_RESULT_COLUMNS = [
    {"label": "Symbol", "expr": "symbol", "format": "text", "locked": True},
    {"label": "Price", "expr": "close[1d]", "format": "price"},
    {"label": "Sector", "expr": "Sector()", "format": "text"},
    {"label": "RS", "expr": "RelativeStrength(20, \"1d\")", "format": "number"},
    {"label": "SectorRS", "expr": "SectorRS(20, \"1d\")", "format": "number"},
    {"label": "Leadership", "expr": "leadership", "format": "pct0"},
    {"label": "RSI", "expr": "rsi14[1d]", "format": "number"},
    {"label": "RSIDiff90", "expr": "RSIDiff90(90, \"1d\")", "format": "number"},
    {"label": "UAE 1D", "expr": "UAERegime(\"1d\")", "format": "text"},
    {"label": "Reason", "expr": "reason", "format": "text", "locked": True},
]

DEFAULT_COLUMN_TEMPLATES = [
    {
        "name": "Core Momentum",
        "description": "Compact price, RS, RSI, RSIDiff90, UAE, and match reason columns.",
        "columns": DEFAULT_RESULT_COLUMNS,
        "is_default": 1,
    },
    {
        "name": "UAE Multi-Timeframe",
        "description": "UAE trend/regime columns for entry and higher timeframes.",
        "columns": [
            {"label": "Symbol", "expr": "symbol", "format": "text", "locked": True},
            {"label": "Price", "expr": "close[1d]", "format": "price"},
            {"label": "UAE 4H", "expr": "UAERegime(\"4h\")", "format": "text"},
            {"label": "UAE 1D", "expr": "UAERegime(\"1d\")", "format": "text"},
            {"label": "UAE 1W", "expr": "UAERegime(\"1w\")", "format": "text"},
            {"label": "UAE Score", "expr": "UAERegimeScore(\"1d\")", "format": "number"},
            {"label": "Hist", "expr": "UAEHist(\"1d\")", "format": "number"},
            {"label": "Reason", "expr": "reason", "format": "text", "locked": True},
        ],
        "is_default": 0,
    },
    {
        "name": "Options Flow",
        "description": "OI/PCR, IV, flow, and earnings primitives for options-aware scans.",
        "columns": [
            {"label": "Symbol", "expr": "symbol", "format": "text", "locked": True},
            {"label": "Price", "expr": "close[1d]", "format": "price"},
            {"label": "OI", "expr": "oi", "format": "integer"},
            {"label": "OI %", "expr": "OIChangePct(5)", "format": "pct1"},
            {"label": "PCR", "expr": "pcr", "format": "number"},
            {"label": "PCR Δ", "expr": "PCRShift(5)", "format": "number"},
            {"label": "Flow", "expr": "FlowBias()", "format": "text"},
            {"label": "IVR", "expr": "IVRank()", "format": "pct0"},
            {"label": "Earn", "expr": "EarningsDays()", "format": "integer"},
            {"label": "Reason", "expr": "reason", "format": "text", "locked": True},
        ],
        "is_default": 0,
    },
]



# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _conn():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    # WAL is configured once during application startup. Setting journal_mode
    # for every short-lived worker connection can contend with active writers.
    c.execute("PRAGMA busy_timeout=10000")
    try:
        from ..services.profiling import increment as _prof_increment
        _prof_increment("db_connections_opened")
    except Exception:
        pass
    return c


def _ensure_tables():
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return

    with _SCHEMA_INIT_LOCK:
        if _SCHEMA_INITIALIZED:
            return

        _ensure_watchlist_tables()
        con = _conn()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS scanner_definitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT DEFAULT '',
                    query_text TEXT NOT NULL,
                    builder_json TEXT DEFAULT '[]',
                    watchlist_id INTEGER,
                    benchmark TEXT DEFAULT 'SPY',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    last_run_at TEXT,
                    last_run_count INTEGER DEFAULT 0,
                    last_results_json TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS scanner_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    definition_id INTEGER,
                    run_at TEXT DEFAULT (datetime('now')),
                    watchlist_id INTEGER,
                    benchmark TEXT DEFAULT 'SPY',
                    query_text TEXT,
                    result_count INTEGER DEFAULT 0,
                    results_json TEXT,
                    error_text TEXT
                );
                CREATE TABLE IF NOT EXISTS scanner_column_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT DEFAULT '',
                    columns_json TEXT NOT NULL DEFAULT '[]',
                    is_default INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS symbol_fundamentals (
                    symbol TEXT PRIMARY KEY,
                    beta REAL,
                    source TEXT,
                    updated TEXT
                );
                """
            )
            migration_sql = [
                "ALTER TABLE scanner_definitions ADD COLUMN builder_json TEXT DEFAULT '[]'",
                "ALTER TABLE scanner_definitions ADD COLUMN watchlist_id INTEGER",
                "ALTER TABLE scanner_definitions ADD COLUMN benchmark TEXT DEFAULT 'SPY'",
                "ALTER TABLE scanner_definitions ADD COLUMN last_run_at TEXT",
                "ALTER TABLE scanner_definitions ADD COLUMN last_run_count INTEGER DEFAULT 0",
                "ALTER TABLE scanner_definitions ADD COLUMN last_results_json TEXT",
                "ALTER TABLE scanner_definitions ADD COLUMN last_error TEXT",
                "ALTER TABLE scanner_definitions ADD COLUMN result_columns_json TEXT",
                "ALTER TABLE scanner_definitions ADD COLUMN result_template_id INTEGER",
            ]
            for _sql in migration_sql:
                try:
                    con.execute(_sql)
                except sqlite3.OperationalError:
                    pass
                except Exception:
                    pass
            con.commit()
            _seed_builtin_scanners(con)
            _seed_column_templates(con)
            con.commit()
            _SCHEMA_INITIALIZED = True
        finally:
            con.close()


def _seed_builtin_scanners(con: Optional[sqlite3.Connection] = None):
    close_con = False
    if con is None:
        con = _conn()
        close_con = True
    try:
        rows = []
        for item in BUILTIN_SCANNERS:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            q = (item.get("query_text") or "").strip()
            desc = (item.get("description") or "").strip()
            rows.append((name, desc, q))

        con.executemany(
            """
            INSERT INTO scanner_definitions
              (name, description, query_text, builder_json, benchmark, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(name) DO UPDATE SET
              description = excluded.description,
              query_text = excluded.query_text,
              benchmark = excluded.benchmark,
              updated_at = datetime('now')
            """,
            [(name, desc, q, json.dumps([]), "SPY") for name, desc, q in rows],
        )
    finally:
        if close_con:
            con.close()


def _seed_column_templates(con: Optional[sqlite3.Connection] = None):
    close_con = False
    if con is None:
        con = _conn()
        close_con = True
    try:
        for item in DEFAULT_COLUMN_TEMPLATES:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            con.execute(
                """
                INSERT INTO scanner_column_templates
                  (name, description, columns_json, is_default, created_at, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                  description = excluded.description,
                  columns_json = excluded.columns_json,
                  is_default = CASE
                    WHEN scanner_column_templates.is_default IS NULL THEN excluded.is_default
                    ELSE scanner_column_templates.is_default
                  END,
                  updated_at = datetime('now')
                """,
                (
                    name,
                    str(item.get("description") or ""),
                    json.dumps(item.get("columns") or []),
                    int(item.get("is_default") or 0),
                ),
            )
    finally:
        if close_con:
            con.close()


# ---------------------------------------------------------------------------
# Timeframe / indicator helpers
# ---------------------------------------------------------------------------

def _normalize_tf(tf: Optional[str]) -> str:
    if not tf:
        return "1d"
    t = str(tf).strip().lower().replace(" ", "")
    aliases = {
        "5min": "5m",
        "5minute": "5m",
        "5minutes": "5m",
        "15min": "15m",
        "15minute": "15m",
        "15minutes": "15m",
        "daily": "1d",
        "day": "1d",
        "1day": "1d",
        "1d": "1d",
        "1hr": "1h",
        "1hour": "1h",
        "1hours": "1h",
        "weekly": "1w",
        "week": "1w",
        "1wk": "1w",
        "monthly": "1m",
        "month": "1m",
        "1mo": "1m",
        "quarterly": "3m",
        "quarter": "3m",
        "qtr": "3m",
        "3mo": "3m",
        "3month": "3m",
        "3months": "3m",
        "3mth": "3m",
    }
    return aliases.get(t, t)


def _normalize_indicator(raw: str) -> str:
    s = str(raw or "").strip().lower().replace(" ", "")
    s = s.replace("ema(rsi14,90)", "ema_rsi14_90")
    s = s.replace("rsidiff90", "rsi_diff_90")
    s = s.replace("rsi-diff-90", "rsi_diff_90")
    s = s.replace("ema(rsi14,13)", "ema_rsi14_13")
    s = s.replace("relative-strength", "relative_strength")
    s = s.replace("relative strength", "relative_strength")
    s = s.replace("sector-rs", "sector_rs")
    s = s.replace("sector rs", "sector_rs")
    if s == "sectorrs":
        return "sector_rs"
    if s in {"macdline", "macd_line"}:
        return "macd"
    if s in {"macdsignal", "macd_signal"}:
        return "macd_signal"
    if s in {"macdhist", "macd_histogram", "macdhistogram"}:
        return "macd_hist"
    return s


def _tf_interval_period(tf: str) -> Tuple[str, str, Optional[str]]:
    tf = _normalize_tf(tf)
    # Keep intraday requests just inside Yahoo's retention windows.  Asking for
    # the exact boundary (60d/730d) can produce noisy yfinance messages such as
    # "possibly delisted" even when the symbol is valid.
    if tf == "5m":
        return "5m", "59d", None
    if tf == "15m":
        return "15m", "59d", None
    if tf == "1h":
        return "1h", "729d", None
    if tf == "2h":
        return "1h", "729d", "2h"
    if tf == "4h":
        return "1h", "729d", "4h"
    if tf == "1d":
        return "1d", "5y", None
    if tf == "1w":
        return "1wk", "10y", None
    if tf == "1m":
        return "1mo", "10y", None
    if tf == "3m":
        # Fetch native monthly bars over a longer window (more raw bars
        # needed since 3 of them collapse into one quarterly candle), then
        # resample up -- same "fetch fine, resample coarse" pattern 2h/4h
        # use with 1h as their base. "max" instead of a fixed "10y" because
        # a 10-year monthly fetch only yields ~40 quarterly bars, and the
        # local-cache path's own 25-bar minimum (see _history_from_local_daily)
        # is easier to clear with more raw history to resample from.
        return "1mo", "max", "3ME"
    return "1d", "5y", None


def _yf_request_kwargs(period: str, interval: str) -> Dict[str, Any]:
    """Build yfinance history kwargs with bounded intraday ranges.

    yfinance/Yahoo only keeps short windows for intraday bars.  Explicit
    start/end dates are more reliable than period strings at those limits and
    avoid noisy stderr output for newer IPOs or thin symbols.
    """
    interval = str(interval or "1d").lower()
    kwargs: Dict[str, Any] = {"interval": interval, "auto_adjust": False}
    if interval in {"5m", "15m", "1h"}:
        days = 59 if interval in {"5m", "15m"} else 729
        end_day = date.today() + timedelta(days=1)
        start_day = date.today() - timedelta(days=days)
        kwargs["start"] = start_day.isoformat()
        kwargs["end"] = end_day.isoformat()
    else:
        kwargs["period"] = period
    return kwargs


@contextmanager
def _suppress_yfinance_noise():
    """Suppress yfinance's non-actionable console/log noise for empty symbols."""
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


def _ticker_history_quiet(ticker: Any, **kwargs: Any) -> Optional[pd.DataFrame]:
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


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> Optional[pd.DataFrame]:
    try:
        if df is None or df.empty:
            return None
        agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        # pandas deprecated uppercase intraday aliases (for example "2H").
        rule = str(rule or "").replace("H", "h")
        out = df.resample(rule).agg(agg).dropna(subset=["Close"])
        return out if not out.empty else None
    except Exception:
        return None


def _normalise_ohlcv_frame(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """Return an ascending OHLCV frame with canonical TradingView-style columns."""
    try:
        if df is None or df.empty:
            return None
        out = df.copy()
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [str(c[-1] if c[-1] else c[0]).strip().title() for c in out.columns]
        else:
            out.columns = [str(c).strip().title() for c in out.columns]
        rename = {
            "O": "Open", "H": "High", "L": "Low", "C": "Close", "V": "Volume",
            "Close_Price": "Close",
        }
        if "Close" not in out.columns and "Adj Close" in out.columns:
            rename["Adj Close"] = "Close"
        out = out.rename(columns={k: v for k, v in rename.items() if k in out.columns})
        needed = ["Open", "High", "Low", "Close", "Volume"]
        if any(c not in out.columns for c in needed):
            return None
        out = out[needed].copy()
        out.index = pd.to_datetime(out.index, errors="coerce")
        out = out[~out.index.isna()]
        try:
            if getattr(out.index, "tz", None) is not None:
                out.index = out.index.tz_convert(None)
        except Exception:
            try:
                out.index = out.index.tz_localize(None)
            except Exception:
                pass
        out = out.sort_index()
        for c in needed:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=needed)
        # Guard against duplicate cache rows for the same calendar date.
        if len(out.index):
            try:
                out = out[~out.index.duplicated(keep="last")]
            except Exception:
                pass
        return out if not out.empty else None
    except Exception:
        return None


def _scanner_history_source_mode() -> str:
    return str(os.environ.get("SCANNER_HISTORY_SOURCE") or "auto").strip().lower()


def _db_table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
        row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return bool(row)
    except Exception:
        return False


# ---------------------------------------------------------------------
# Bulk preload memory tier for price_cache -- addresses the "many small
# per-symbol SQLite reads instead of one batch read" pattern. A scan over
# an N-symbol watchlist used to potentially open N separate connections
# and issue N separate "WHERE symbol=?" queries (one per cache-miss
# worker in the ThreadPoolExecutor). _bulk_preload_daily_history() runs
# ONE "WHERE symbol IN (...)" query up front for the whole symbol list
# and stores each symbol's frame here; _price_cache_daily_history() below
# checks this dict before ever touching SQLite. Populated once per scan
# request and intentionally left small/short-lived (not a substitute for
# the existing lru_cache on _local_daily_history_cached, which persists
# across requests -- this only smooths out the *first* scan / cache-cold
# case within a single request).
# ---------------------------------------------------------------------
_bulk_daily_history_cache: Dict[str, Optional[pd.DataFrame]] = {}
_bulk_daily_history_lock = threading.Lock()


def _bulk_preload_daily_history(symbols: List[str]) -> None:
    if not symbols:
        return
    try:
        syms = sorted({str(s or "").upper().strip() for s in symbols if s})
        if not syms:
            return
        con = _conn()
        try:
            if not _db_table_exists(con, "price_cache"):
                return
            placeholders = ",".join("?" for _ in syms)
            rows = con.execute(
                f"""
                SELECT symbol, date, open, high, low, close, volume
                FROM price_cache
                WHERE symbol IN ({placeholders}) AND close IS NOT NULL AND close>0
                ORDER BY symbol ASC, date ASC
                """,
                syms,
            ).fetchall()
        finally:
            con.close()
        by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            d = dict(r)
            by_symbol.setdefault(d["symbol"], []).append(d)
        with _bulk_daily_history_lock:
            for sym in syms:
                sym_rows = by_symbol.get(sym)
                if not sym_rows:
                    _bulk_daily_history_cache[sym] = None
                    continue
                df = pd.DataFrame(sym_rows)
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                df = df.dropna(subset=["date", "close"]).sort_values("date")
                if df.empty:
                    _bulk_daily_history_cache[sym] = None
                    continue
                for c in ("open", "high", "low", "close", "volume"):
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df["open"] = df["open"].fillna(df["close"])
                df["high"] = df["high"].fillna(df[["open", "close"]].max(axis=1))
                df["low"] = df["low"].fillna(df[["open", "close"]].min(axis=1))
                df["volume"] = df["volume"].fillna(0)
                df = df.dropna(subset=["open", "high", "low", "close"])
                if df.empty:
                    _bulk_daily_history_cache[sym] = None
                    continue
                out = pd.DataFrame(
                    {
                        "Open": df["open"].astype(float).values,
                        "High": df["high"].astype(float).values,
                        "Low": df["low"].astype(float).values,
                        "Close": df["close"].astype(float).values,
                        "Volume": df["volume"].astype(float).values,
                    },
                    index=pd.DatetimeIndex(df["date"]),
                )
                _bulk_daily_history_cache[sym] = _normalise_ohlcv_frame(out)
    except Exception as e:  # noqa: BLE001
        print(f"[scanner_builder] bulk daily history preload failed (non-fatal, falls back to per-symbol reads): {e}")


def _price_cache_daily_history(symbol: str) -> Optional[pd.DataFrame]:
    """Load local daily OHLCV from options_data.price_cache, newest app data first.

    Scanner Builder should evaluate UAE/lookback logic against the same local

    OHLCV cache used by the dashboard whenever that cache has enough bars.
    Otherwise yfinance can be stale/misaligned and old weekly signals may look
    recent to Lookback(...).
    """
    try:
        symbol_u = symbol.upper().strip()
        with _bulk_daily_history_lock:
            if symbol_u in _bulk_daily_history_cache:
                cached = _bulk_daily_history_cache[symbol_u]
                return None if cached is None else cached.copy()
        con = _conn()
        try:
            if not _db_table_exists(con, "price_cache"):
                return None
            rows = con.execute(
                """
                SELECT date, open, high, low, close, volume
                FROM price_cache
                WHERE symbol=? AND close IS NOT NULL AND close>0
                ORDER BY date ASC
                """,
                (symbol_u,),
            ).fetchall()
        finally:
            con.close()
        if not rows:
            return None
        df = pd.DataFrame([dict(r) for r in rows])
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date", "close"]).sort_values("date")
        if df.empty:
            return None
        for c in ("open", "high", "low", "close", "volume"):
            if c not in df.columns:
                df[c] = df.get("close", 0) if c != "volume" else 0
            df[c] = pd.to_numeric(df[c], errors="coerce")
        # Repair missing OHLC around close rather than discarding otherwise useful bars.
        df["open"] = df["open"].fillna(df["close"])
        df["high"] = df["high"].fillna(df[["open", "close"]].max(axis=1))
        df["low"] = df["low"].fillna(df[["open", "close"]].min(axis=1))
        df["volume"] = df["volume"].fillna(0)
        df = df.dropna(subset=["open", "high", "low", "close"])
        if df.empty:
            return None
        out = pd.DataFrame(
            {
                "Open": df["open"].astype(float).values,
                "High": df["high"].astype(float).values,
                "Low": df["low"].astype(float).values,
                "Close": df["close"].astype(float).values,
                "Volume": df["volume"].astype(float).values,
            },
            index=pd.DatetimeIndex(df["date"]),
        )
        return _normalise_ohlcv_frame(out)
    except Exception:
        return None


_market_data_db_path_cache: Dict[str, Any] = {"checked": False, "path": None}


def _market_data_db_path() -> Optional[str]:
    """Resolves + existence-checks data/market_data.db exactly once per
    process instead of doing a filesystem stat() for every symbol that
    goes through _local_daily_history_cached (previously: one stat per
    unique symbol per process, since Path.exists() ran unconditionally
    inside _market_bars_daily_history on every lru_cache miss)."""
    if _market_data_db_path_cache["checked"]:
        return _market_data_db_path_cache["path"]
    db_path = (
        os.environ.get("MARKET_DATA_DB")
        or os.environ.get("BACKTEST_DB_PATH")
        or str(Path(__file__).resolve().parents[2] / "data" / "market_data.db")
    )
    resolved = db_path if (db_path and Path(db_path).exists()) else None
    _market_data_db_path_cache["checked"] = True
    _market_data_db_path_cache["path"] = resolved
    return resolved


def _market_bars_daily_history(symbol: str) -> Optional[pd.DataFrame]:
    """Load optional historical bars from data/market_data.db when present."""
    try:
        db_path = _market_data_db_path()
        if not db_path:
            return None
        con = sqlite3.connect(str(db_path), timeout=10)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_bars'").fetchone()
            if not row:
                return None
            rows = con.execute(
                """
                SELECT bar_time AS date, open, high, low, close, volume
                FROM market_bars
                WHERE symbol=? AND interval='1d' AND close IS NOT NULL AND close>0
                ORDER BY bar_time ASC
                """,
                (symbol.upper().strip(),),
            ).fetchall()
        finally:
            con.close()
        if not rows:
            return None
        df = pd.DataFrame([dict(r) for r in rows])
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date", "close"]).sort_values("date")
        if df.empty:
            return None
        out = pd.DataFrame(
            {
                "Open": pd.to_numeric(df["open"], errors="coerce"),
                "High": pd.to_numeric(df["high"], errors="coerce"),
                "Low": pd.to_numeric(df["low"], errors="coerce"),
                "Close": pd.to_numeric(df["close"], errors="coerce"),
                "Volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0),
            },
            index=pd.DatetimeIndex(df["date"]),
        )
        return _normalise_ohlcv_frame(out)
    except Exception:
        return None


def _local_daily_history_cached(symbol: str) -> Optional[pd.DataFrame]:
    # Hour-bucketed, not truly unbounded -- a naked @lru_cache here meant
    # that once a symbol's DataFrame was cached, it would never pick up
    # newly-backfilled/ingested price_cache rows for the rest of the
    # process's lifetime, even after the DB itself was updated. Confirmed
    # as a real contributor to a scanner returning indicator values
    # derived from price data that had silently stopped updating weeks
    # earlier, with no visible error -- an incremental backfill catching
    # a symbol up wouldn't actually change what the scanner computed
    # until the process restarted. Bucketing by hour bounds staleness to
    # at most an hour without needing a full cache-invalidation rewrite.
    hour_bucket = int(time.time() // 3600)
    return _local_daily_history_cached_bucketed(symbol, hour_bucket)


@lru_cache(maxsize=1024)
def _get_splits_cached(symbol: str, _day_bucket: int) -> Dict[Any, float]:
    """Confirmed corporate-action split ratios for this symbol, from
    yfinance's own authoritative split-event data -- not a heuristic
    price-jump guess, since misfiring on a large but genuine single-day
    move would introduce a worse bug than the one this fixes. Day-
    bucketed (not hour) since splits are announced well in advance and
    don't need fresher-than-daily lookup; even a stale cache costs
    nothing in practice given how rare they are."""
    try:
        import yfinance as yf
        splits = yf.Ticker(symbol).splits
        if splits is None or splits.empty:
            return {}
        # yfinance's splits Series index comes back tz-aware (localized to
        # the exchange timezone), while the local daily price cache this
        # gets compared against (df.index.min()/.max() in
        # _apply_split_adjustment below) is tz-naive. Comparing the two
        # directly raised "Cannot compare tz-naive and tz-aware
        # timestamps" for almost every symbol computed at the "1d"
        # timeframe -- stripping tz here, at the single source of these
        # Timestamps, keeps every downstream comparison tz-naive instead
        # of patching each comparison site separately.
        def _norm_ts(ts):
            t = pd.Timestamp(ts).normalize()
            return t.tz_localize(None) if t.tzinfo is not None else t
        return {_norm_ts(ts): float(ratio) for ts, ratio in splits.items() if ratio and ratio > 0 and ratio != 1.0}
    except Exception:
        return {}


def _apply_split_adjustment(df: Optional[pd.DataFrame], symbol: str) -> Optional[pd.DataFrame]:
    """auto_adjust=False is used throughout this file's yfinance fetches
    on purpose (see the comment near _backfill_price_history_to_cache) --
    the stored close is meant to be the actual traded price on each
    historical day, matching what a real chart shows, not a silently
    back-adjusted series. That's the right call for display, but it
    means a stock split creates a genuine, large discontinuity in the
    raw close series: a 2-for-1 split looks like an overnight ~50% price
    collapse to anything computing rolling changes across it (RSI,
    EMA-based indicators, etc) -- confirmed as the actual cause of a
    scanner returning deeply-oversold RSI for a stock that had, in
    reality, just rallied, because its 90-period EMA was still digesting
    an unadjusted split from days earlier. This adjusts a COPY for
    calculation purposes only -- the stored price_cache table itself is
    untouched -- dividing price columns and multiplying volume for every
    row strictly BEFORE each confirmed split date by that split's ratio,
    so rolling indicators see a continuous series instead of a fake
    crash. Ratio convention matches yfinance's own (e.g. 2.0 for a
    2-for-1): price / ratio, volume * ratio."""
    if df is None or df.empty:
        return df
    day_bucket = int(time.time() // 86400)
    splits = _get_splits_cached(symbol, day_bucket)
    if not splits:
        return df
    # Defensive backstop: _get_splits_cached() already normalizes its own
    # Timestamps to tz-naive (see its docstring), but if df.index itself
    # ever arrives tz-aware from a different caller, comparing it against
    # the tz-naive split dates below would hit the same "Cannot compare
    # tz-naive and tz-aware timestamps" error from the other side.
    idx_min = df.index.min()
    idx_max = df.index.max()
    if getattr(idx_min, "tzinfo", None) is not None:
        idx_min = idx_min.tz_localize(None)
    if getattr(idx_max, "tzinfo", None) is not None:
        idx_max = idx_max.tz_localize(None)
    in_range = {d: r for d, r in splits.items() if idx_min <= d <= idx_max + pd.Timedelta(days=1)}
    if not in_range:
        return df
    adjusted = df.copy()
    # Most recent split first, each only touching rows strictly before
    # its own date -- correct even with multiple splits in the window,
    # since earlier splits' adjustments compound naturally in this order.
    for split_date in sorted(in_range.keys(), reverse=True):
        ratio = in_range[split_date]
        mask = adjusted.index < split_date
        if not mask.any():
            continue
        for col in ("open", "high", "low", "close"):
            if col in adjusted.columns:
                adjusted.loc[mask, col] = adjusted.loc[mask, col] / ratio
        if "volume" in adjusted.columns:
            adjusted.loc[mask, "volume"] = adjusted.loc[mask, "volume"] * ratio
    return adjusted


@lru_cache(maxsize=1024)
def _local_daily_history_cached_bucketed(symbol: str, _hour_bucket: int) -> Optional[pd.DataFrame]:
    symbol = str(symbol or "").upper().strip()
    if not symbol:
        return None
    # Prefer richer long-history store, then app price_cache.  If market_bars is
    # absent, price_cache still keeps Scanner Builder aligned with dashboard data.
    frames = [_market_bars_daily_history(symbol), _price_cache_daily_history(symbol)]
    best = None
    for df in frames:
        if df is None or df.empty:
            continue
        if best is None or len(df) > len(best) or (len(df) == len(best) and df.index[-1] > best.index[-1]):
            best = df
    if best is None:
        return None
    return _apply_split_adjustment(best.copy(), symbol)


def _history_staleness_days(df: Optional[pd.DataFrame]) -> Optional[int]:
    try:
        if df is None or df.empty:
            return None
        last = pd.Timestamp(df.index[-1]).date()
        return abs((date.today() - last).days)
    except Exception:
        return None


def _is_history_fresh_enough(df: Optional[pd.DataFrame], tf: str) -> bool:
    # In replay/future-dated databases, date.today() may not match the cache.
    # Only reject obvious stale live-provider frames when the date is behind by a
    # large amount.  Local DB rows are accepted by source preference above.
    days = _history_staleness_days(df)
    if days is None:
        return False
    tf = _normalize_tf(tf)
    max_days = 7 if tf == "1d" else 14 if tf == "1w" else 45 if tf == "1m" else 100 if tf == "3m" else 3
    return days <= max_days or os.environ.get("SCANNER_ALLOW_STALE_YF_HISTORY", "0").strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=512)
def _resampled_history_cached(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    """Caches the weekly/monthly resample of the daily series, keyed by
    (symbol, tf). Before this, _history_from_local_daily re-ran the
    pandas resample from scratch on every single call -- the underlying
    daily data only changes once per backfill, but the resample itself
    was redone every time regardless, even for a symbol just scanned
    moments earlier in the same session. Invalidated at the same point
    _local_daily_history_cached is (see _backfill/update code -- both
    are cleared together, since a resample cached before a backfill
    would otherwise silently keep serving stale weekly/monthly bars)."""
    daily = _local_daily_history_cached(symbol.upper().strip())
    if daily is None or daily.empty:
        return None
    if tf == "1w":
        return _resample_ohlcv(daily, "W-FRI")
    if tf == "1m":
        monthly = _resample_ohlcv(daily, "ME")
        if monthly is None or monthly.empty:
            monthly = _resample_ohlcv(daily, "M")
        return monthly
    if tf == "3m":
        quarterly = _resample_ohlcv(daily, "3ME")
        if quarterly is None or quarterly.empty:
            quarterly = _resample_ohlcv(daily, "3M")
        return quarterly
    return None


def _history_from_local_daily(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    tf = _normalize_tf(tf)
    daily = _local_daily_history_cached(symbol.upper().strip())
    if daily is None or daily.empty:
        return None
    if tf == "1d":
        return daily.copy()
    if tf in ("1w", "1m", "3m"):
        cached = _resampled_history_cached(symbol.upper().strip(), tf)
        return cached.copy() if cached is not None else None
    return None


@lru_cache(maxsize=512)
def _fetch_history_cached(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf

        symbol = symbol.upper().strip()
        tf = _normalize_tf(tf)
        interval, period, resample_rule = _tf_interval_period(tf)
        ticker = yf.Ticker(symbol)
        df = None
        df = _ticker_history_quiet(ticker, **_yf_request_kwargs(period, interval))
        if (df is None or df.empty) and tf in {"1h", "2h", "4h"}:
            # Fallback: build higher timeframes from lower cached intraday bars when direct data is thin.
            for fb_interval, fb_period in (("5m", "60d"), ("15m", "60d")):
                fb = _ticker_history_quiet(ticker, **_yf_request_kwargs(fb_period, fb_interval))
                if fb is None or fb.empty:
                    continue
                fb = fb.dropna(subset=["Close", "High", "Low", "Volume"]).copy()
                fb = fb.rename(columns=str.title)
                rule = "1h" if tf == "1h" else "2h" if tf == "2h" else "4h"
                df = _resample_ohlcv(fb, rule)
                if df is not None and not df.empty:
                    break
        if df is None or df.empty:
            return None
        df = df.dropna(subset=["Close", "High", "Low", "Volume"]).copy()
        if resample_rule:
            df = _resample_ohlcv(df, resample_rule)
            if df is None or df.empty:
                return None
        return df
    except Exception:
        return None


def _get_max_cached_date(symbol: str) -> Optional[str]:
    """Most recent date already in price_cache for this symbol, or None
    if nothing is cached yet. Used to make backfills incremental instead
    of always re-fetching the entire window."""
    con = _conn()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS price_cache (
            symbol TEXT NOT NULL, date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (symbol, date))""")
        row = con.execute("SELECT MAX(date) FROM price_cache WHERE symbol=?", (symbol.upper().strip(),)).fetchone()
        return row[0] if row and row[0] else None
    finally:
        con.close()


def _get_min_cached_date(symbol: str) -> Optional[str]:
    """Earliest date already in price_cache for this symbol, or None if
    nothing is cached. Needed to detect the case _get_max_cached_date
    alone can't: a symbol that was previously backfilled for a SHORTER
    window (e.g. 120 days) and is now being asked for a LONGER one (e.g.
    1200 days, for rsidiff90sma's weekly history requirement) -- the old
    "incremental = has any data at all, so just catch up to today" logic
    would see recent data present and conclude nothing needs fetching,
    silently ignoring the ~1080 days of older history that were actually
    requested and are still missing."""
    con = _conn()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS price_cache (
            symbol TEXT NOT NULL, date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (symbol, date))""")
        row = con.execute("SELECT MIN(date) FROM price_cache WHERE symbol=?", (symbol.upper().strip(),)).fetchone()
        return row[0] if row and row[0] else None
    finally:
        con.close()


def _backfill_price_history_to_cache(symbol: str, months: int = 132, force_full: bool = False) -> bool:
    """Fetches daily OHLCV from yfinance and persists EVERY day to
    price_cache (not just the latest day, unlike the existing scheduled
    snapshot job) -- so a symbol that didn't have enough local history for
    indicators like rsidiff90 gets a durable backfill instead of either:
      (a) silently computing a distorted value from too little data, or
      (b) re-fetching live from yfinance on every single scan that touches
          this symbol, forever, with nothing ever persisted.

    Gap-aware (this is the important part): if the symbol already has
    cached data, this fetches ONLY the days since the last cached date --
    not the entire `months` window again. A symbol backfilled yesterday
    and checked again today fetches ~1 day of data, not 11 years of it.
    Pass force_full=True to force a complete re-fetch regardless of what's
    already cached.

    Default full-backfill window is 132 months (11 years) -- confirmed
    directly that a 36-month backfill only produces ~150 weekly bars after
    resampling, far short of the ~540-bar convergence threshold
    rsidiff90("1w") needs (calibrated on bar COUNT regardless of
    timeframe, so weekly EMA-90 genuinely needs ~10.4 calendar years).
    132 months gives weekly a real margin above that while daily benefits
    even further (its own threshold only needs ~2.15 years).

    Returns True if the backfill/update produced and stored usable data,
    or if the symbol was already up to date (nothing to do is success,
    not failure).
    """
    symbol = symbol.upper().strip()
    try:
        import yfinance as yf
    except Exception:
        return False

    from ..services.market import is_recently_failed, mark_fetch_failed
    if is_recently_failed(symbol):
        return False  # this symbol failed a fetch recently anywhere in the app -- don't retry yet

    max_date_str = None if force_full else _get_max_cached_date(symbol)
    min_date_str = None if force_full else _get_min_cached_date(symbol)
    is_incremental = max_date_str is not None

    # What the requested `months` window implies as an earliest date --
    # used below to detect "the cache doesn't go back far enough for what
    # was just asked for", not just "is the cache stale going forward".
    requested_start = (datetime.now() - timedelta(days=int(max(1, months)) * 30)).strftime("%Y-%m-%d")

    try:
        tk = yf.Ticker(symbol)
        frames = []
        if is_incremental:
            # Forward catch-up: new days since the last cached one.
            start_date = (datetime.strptime(max_date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            today_str = datetime.now().strftime("%Y-%m-%d")
            if start_date <= today_str:
                fwd = tk.history(start=start_date, interval="1d", auto_adjust=False)
                if fwd is not None and not fwd.empty:
                    frames.append(fwd)

            # Backward gap-fill: this is the fix. A symbol previously
            # backfilled for a SHORTER window (e.g. 120 days) and now
            # asked for a LONGER one (e.g. 1200 days, for rsidiff90sma's
            # weekly requirement) has recent data present, so the forward
            # catch-up above sees "nothing new" and would otherwise
            # report success without ever fetching the ~1080 days of
            # older history that were actually requested. Only fetch the
            # gap that's actually missing -- not the whole window again.
            if min_date_str and requested_start < min_date_str:
                end_date = (datetime.strptime(min_date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                if requested_start <= end_date:
                    bwd = tk.history(start=requested_start, end=end_date, interval="1d", auto_adjust=False)
                    if bwd is not None and not bwd.empty:
                        frames.append(bwd)
                        print(f"[scanner_builder] {symbol}: extending backfill further back "
                              f"({requested_start} to {end_date}, {len(bwd)} bars) -- a longer "
                              f"history window was requested than what was already cached")

            df = pd.concat(frames) if frames else pd.DataFrame()
        else:
            # auto_adjust defaults to True in yfinance, which silently adjusts
            # historical Close for dividends/splits -- yfinance's own docs
            # recommend auto_adjust=False specifically for price charts/technical
            # analysis, since that's what shows the ACTUAL traded price (what
            # TradingView's standard chart displays), not a dividend-adjusted
            # series. Left as the implicit default, this is a real, silent
            # source of RSI/indicator discrepancy vs TradingView whenever a
            # dividend or split falls within the lookback window.
            df = tk.history(period=f"{max(1, int(months))}mo", interval="1d", auto_adjust=False)
    except Exception as e:  # noqa: BLE001
        print(f"[scanner_builder] backfill failed for {symbol}: {type(e).__name__}: {e}")
        mark_fetch_failed(symbol)
        return False
    if df is None or df.empty:
        # For an incremental update, "empty" commonly just means "no new
        # trading days since the last cached one" (e.g. checked over a
        # weekend or the same day twice) -- that's success, not failure.
        if not is_incremental:
            mark_fetch_failed(symbol)
        return is_incremental

    df = df.dropna(subset=["Close"])
    if df.empty:
        return is_incremental

    con = _conn()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS price_cache (
            symbol TEXT NOT NULL, date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (symbol, date))""")
        rows = []
        for ts, row in df.iterrows():
            try:
                date_str = pd.Timestamp(ts).strftime("%Y-%m-%d")
                close = float(row["Close"])
                rows.append((
                    symbol, date_str,
                    round(float(row.get("Open", close)), 4),
                    round(float(row.get("High", close)), 4),
                    round(float(row.get("Low", close)), 4),
                    round(close, 4),
                    int(row.get("Volume", 0) or 0),
                ))
            except Exception:
                continue
        if not rows:
            return is_incremental
        con.executemany(
            "INSERT OR REPLACE INTO price_cache (symbol, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        con.commit()
        kind = f"incremental update ({max_date_str} -> now)" if is_incremental else "full backfill"
        print(f"[scanner_builder] {kind}: stored {len(rows)} day(s) of history for {symbol} into price_cache")
        # _local_daily_history_cached (and the _history() call above it) is
        # lru_cache'd -- without clearing it, the stale "insufficient/no
        # data" result cached from before this backfill would keep being
        # served for the rest of the process's life, making the backfill
        # write successful but invisible to every future read.
        try:
            _local_daily_history_cached.cache_clear()
            _resampled_history_cached.cache_clear()
        except Exception:
            pass
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[scanner_builder] backfill store failed for {symbol}: {type(e).__name__}: {e}")
        return False
    finally:
        con.close()


# ---------------------------------------------------------------------
# Intraday (1h/2h/4h) backfill -- separate table from price_cache (daily),
# by design. Row density is the driver: 2 years of hourly data is ~3,300
# rows/symbol vs. a few hundred for daily, and across a large watchlist
# that's easily 1M+ rows -- keeping it separate means daily-only consumers
# (the majority) never pay any cost for it, and intraday can have its own
# retention policy (auto-pruned to ~2 years, matching yfinance's own
# hourly-data limit) without touching the daily table's growth at all.
# 1h is the single base resolution; 2h and 4h are derived by resampling
# from the same cached 1h data, exactly like the existing daily->weekly
# pattern -- one backfill covers all three timeframes.
# ---------------------------------------------------------------------
INTRADAY_RETENTION_DAYS = 729  # yfinance's own max for 1h-interval data


def _ensure_intraday_table(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS intraday_price_cache (
        symbol TEXT NOT NULL, ts TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume INTEGER,
        PRIMARY KEY (symbol, ts))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_intraday_symbol ON intraday_price_cache(symbol)")


def _get_max_cached_intraday_ts(symbol: str) -> Optional[str]:
    """Most recent timestamp already in intraday_price_cache for this
    symbol, or None if nothing is cached yet."""
    con = _conn()
    try:
        _ensure_intraday_table(con)
        row = con.execute("SELECT MAX(ts) FROM intraday_price_cache WHERE symbol=?", (symbol.upper().strip(),)).fetchone()
        return row[0] if row and row[0] else None
    finally:
        con.close()


def _get_min_cached_intraday_ts(symbol: str) -> Optional[str]:
    """Earliest timestamp already in intraday_price_cache for this
    symbol, or None if nothing is cached. Same reasoning as
    _get_min_cached_date() for the daily table: without this, a symbol
    previously backfilled for a shorter intraday window and then asked
    for a longer one would silently keep whatever shorter history it
    already had."""
    con = _conn()
    try:
        _ensure_intraday_table(con)
        row = con.execute("SELECT MIN(ts) FROM intraday_price_cache WHERE symbol=?", (symbol.upper().strip(),)).fetchone()
        return row[0] if row and row[0] else None
    finally:
        con.close()


def _backfill_intraday_history_to_cache(symbol: str, days: int = INTRADAY_RETENTION_DAYS, force_full: bool = False) -> bool:
    """Fetches hourly OHLCV from yfinance (capped at yfinance's own ~729-day
    limit for 1h-interval data) and persists every bar to
    intraday_price_cache. 2h and 4h are NOT fetched separately -- they're
    derived from this same 1h data via resampling on read, matching the
    existing daily->weekly pattern.

    Gap-aware, same principle as the daily version: if this symbol already
    has cached intraday data, fetches only from the last cached bar's date
    forward, not the entire retention window again. Uses the cached
    timestamp's DATE (not exact hour) as the fetch start -- a day's worth
    of overlap with already-cached bars is harmless (INSERT OR REPLACE
    just re-writes identical rows) and avoids any edge case around
    yfinance's exact intraday start-time handling.

    Returns True if the backfill/update produced and stored usable data,
    or if the symbol was already up to date.
    """
    symbol = symbol.upper().strip()
    try:
        import yfinance as yf
    except Exception:
        return False

    from ..services.market import is_recently_failed, mark_fetch_failed
    if is_recently_failed(symbol):
        return False

    max_ts_str = None if force_full else _get_max_cached_intraday_ts(symbol)
    min_ts_str = None if force_full else _get_min_cached_intraday_ts(symbol)
    is_incremental = max_ts_str is not None

    # yfinance hard-caps 1h-interval history at ~729 days regardless of
    # what's requested -- cap the implied "requested start" the same way
    # so we don't ask for something yfinance would reject anyway.
    requested_start_dt = datetime.now() - timedelta(days=min(int(max(1, days)), INTRADAY_RETENTION_DAYS))
    requested_start = requested_start_dt.strftime("%Y-%m-%d")

    try:
        tk = yf.Ticker(symbol)
        if is_incremental:
            frames = []
            start_date = pd.Timestamp(max_ts_str).strftime("%Y-%m-%d")
            fwd = tk.history(start=start_date, interval="1h", auto_adjust=False)
            if fwd is not None and not fwd.empty:
                frames.append(fwd)

            # Backward gap-fill -- same fix as the daily backfill: a
            # symbol previously backfilled for a shorter intraday window
            # and now asked for a longer one has recent bars present, so
            # the forward catch-up above sees "nothing new" and would
            # otherwise report success without ever fetching the missing
            # older bars.
            min_date_str = pd.Timestamp(min_ts_str).strftime("%Y-%m-%d") if min_ts_str else None
            if min_date_str and requested_start < min_date_str:
                end_date = (datetime.strptime(min_date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                if requested_start <= end_date:
                    bwd = tk.history(start=requested_start, end=end_date, interval="1h", auto_adjust=False)
                    if bwd is not None and not bwd.empty:
                        frames.append(bwd)
                        print(f"[scanner_builder] {symbol}: extending intraday backfill further back "
                              f"({requested_start} to {end_date}, {len(bwd)} bars) -- a longer history "
                              f"window was requested than what was already cached")
            df = pd.concat(frames) if frames else pd.DataFrame()
        else:
            df = tk.history(period=f"{max(1, int(days))}d", interval="1h", auto_adjust=False)
    except Exception as e:  # noqa: BLE001
        print(f"[scanner_builder] intraday backfill failed for {symbol}: {type(e).__name__}: {e}")
        mark_fetch_failed(symbol)
        return False
    if df is None or df.empty:
        if not is_incremental:
            mark_fetch_failed(symbol)
        return is_incremental

    df = df.dropna(subset=["Close"])
    if df.empty:
        return is_incremental

    con = _conn()
    try:
        _ensure_intraday_table(con)
        rows = []
        for ts, row in df.iterrows():
            try:
                ts_str = pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                close = float(row["Close"])
                rows.append((
                    symbol, ts_str,
                    round(float(row.get("Open", close)), 4),
                    round(float(row.get("High", close)), 4),
                    round(float(row.get("Low", close)), 4),
                    round(close, 4),
                    int(row.get("Volume", 0) or 0),
                ))
            except Exception:
                continue
        if not rows:
            return is_incremental
        con.executemany(
            "INSERT OR REPLACE INTO intraday_price_cache (symbol, ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        con.commit()

        # Prune anything past the retention window -- no reason to keep
        # data yfinance itself won't extend further back, and an
        # ever-growing table with no upper bound is exactly the row-count
        # problem this separate-table design was meant to avoid.
        cutoff = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute("DELETE FROM intraday_price_cache WHERE symbol=? AND ts<?", (symbol, cutoff))
        con.commit()

        kind = f"incremental update (since {max_ts_str})" if is_incremental else "full backfill"
        print(f"[scanner_builder] {kind}: stored {len(rows)} hourly bar(s) for {symbol} into intraday_price_cache")
        try:
            _local_intraday_history_cached.cache_clear()
        except Exception:
            pass
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[scanner_builder] intraday backfill store failed for {symbol}: {type(e).__name__}: {e}")
        return False
    finally:
        con.close()


_bulk_intraday_history_cache: Dict[str, Optional[pd.DataFrame]] = {}
_bulk_intraday_history_lock = threading.Lock()


def _bulk_preload_intraday_history(symbols: List[str]) -> None:
    """Same idea as _bulk_preload_daily_history() but for the 1h base
    table backing 1h/2h/4h timeframes. Only worth calling when a scan's
    required timeframes actually include an intraday one -- callers
    should gate this (see api_run()) rather than pay for it on every
    scan."""
    if not symbols:
        return
    try:
        syms = sorted({str(s or "").upper().strip() for s in symbols if s})
        if not syms:
            return
        con = _conn()
        try:
            _ensure_intraday_table(con)
            placeholders = ",".join("?" for _ in syms)
            rows = con.execute(
                f"""
                SELECT symbol, ts, open, high, low, close, volume
                FROM intraday_price_cache
                WHERE symbol IN ({placeholders})
                ORDER BY symbol ASC, ts ASC
                """,
                syms,
            ).fetchall()
        finally:
            con.close()
        by_symbol: Dict[str, List[Any]] = {}
        for r in rows:
            by_symbol.setdefault(r[0], []).append(r)
        with _bulk_intraday_history_lock:
            for sym in syms:
                sym_rows = by_symbol.get(sym)
                if not sym_rows:
                    _bulk_intraday_history_cache[sym] = None
                    continue
                idx = pd.to_datetime([r[1] for r in sym_rows])
                df = pd.DataFrame({
                    "Open": [r[2] for r in sym_rows], "High": [r[3] for r in sym_rows],
                    "Low": [r[4] for r in sym_rows], "Close": [r[5] for r in sym_rows],
                    "Volume": [r[6] for r in sym_rows],
                }, index=idx)
                _bulk_intraday_history_cache[sym] = df
    except Exception as e:  # noqa: BLE001
        print(f"[scanner_builder] bulk intraday history preload failed (non-fatal, falls back to per-symbol reads): {e}")


@lru_cache(maxsize=512)
def _local_intraday_history_cached(symbol: str) -> Optional[pd.DataFrame]:
    """Reads the raw 1h base data for a symbol from intraday_price_cache.
    lru_cache'd the same way _local_daily_history_cached is -- cleared
    explicitly after a successful backfill (see above), same pattern as
    the daily cache to avoid serving a stale "no data" result forever."""
    symbol = symbol.upper().strip()
    with _bulk_intraday_history_lock:
        if symbol in _bulk_intraday_history_cache:
            cached = _bulk_intraday_history_cache[symbol]
            return None if cached is None else cached.copy()
    con = _conn()
    try:
        _ensure_intraday_table(con)
        rows = con.execute(
            "SELECT ts, open, high, low, close, volume FROM intraday_price_cache WHERE symbol=? ORDER BY ts ASC",
            (symbol,),
        ).fetchall()
    except Exception:
        return None
    finally:
        con.close()
    if not rows:
        return None
    idx = pd.to_datetime([r[0] for r in rows])
    df = pd.DataFrame({
        "Open": [r[1] for r in rows], "High": [r[2] for r in rows],
        "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
        "Volume": [r[5] for r in rows],
    }, index=idx)
    return df


def _history_from_local_intraday(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    """tf must be one of 1h/2h/4h. 1h reads the cached base data directly;
    2h/4h resample from that same cached 1h data on read."""
    base = _local_intraday_history_cached(symbol.upper().strip())
    if base is None or base.empty:
        return None
    if tf == "1h":
        return base.copy()
    if tf in ("2h", "4h"):
        return _resample_ohlcv(base, tf)
    return None


def _ensure_backfill_queue_table(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS price_backfill_queue (
        symbol TEXT PRIMARY KEY,
        queued_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
    )""")


import threading as _bf_track_threading
_recently_enqueued_backfills_lock = _bf_track_threading.Lock()
_recently_enqueued_backfills: set = set()  # {(symbol, "daily"|"intraday"), ...} -- cleared per scan run


def _track_enqueued_backfill(symbol: str, kind: str) -> None:
    with _recently_enqueued_backfills_lock:
        _recently_enqueued_backfills.add((symbol.upper().strip(), kind))


def _reset_enqueued_backfill_tracking() -> None:
    with _recently_enqueued_backfills_lock:
        _recently_enqueued_backfills.clear()


def _get_enqueued_backfill_summary() -> Dict[str, List[str]]:
    with _recently_enqueued_backfills_lock:
        daily = sorted(s for s, k in _recently_enqueued_backfills if k == "daily")
        intraday = sorted(s for s, k in _recently_enqueued_backfills if k == "intraday")
    return {"daily": daily, "intraday": intraday}


def _enqueue_price_backfill(symbol: str) -> None:
    """Non-blocking: just records that this symbol needs a historical
    backfill (a cheap DB write, no network call) -- safe to call from
    inside an interactive scan's hot path. The actual fetch happens later,
    off this request entirely, via run_pending_price_backfills()."""
    con = _conn()
    try:
        _ensure_backfill_queue_table(con)
        con.execute(
            "INSERT OR IGNORE INTO price_backfill_queue (symbol, queued_at, status) VALUES (?, datetime('now'), 'pending')",
            (symbol,),
        )
        con.commit()
    finally:
        con.close()
    _track_enqueued_backfill(symbol, "daily")


def run_pending_price_backfills(max_symbols: int = 5) -> Dict[str, int]:
    """Background job: processes a SMALL batch of queued symbols per call
    (default 5), each a real yfinance network fetch -- meant to be called
    periodically (e.g. every few minutes) by a scheduler, NOT synchronously
    from within a scan. This is what actually performs the backfills that
    _history() only *queues* during interactive use, keeping live queries
    fast while still catching every thin-history symbol up over time.
    Register with job_registry to run on a schedule; see app_factory.py's
    existing job registration pattern for other periodic jobs.
    """
    con = _conn()
    try:
        _ensure_backfill_queue_table(con)
        rows = con.execute(
            "SELECT symbol FROM price_backfill_queue WHERE status='pending' ORDER BY queued_at ASC LIMIT ?",
            (max_symbols,),
        ).fetchall()
    finally:
        con.close()

    result = {"processed": 0, "succeeded": 0, "failed": 0}
    for row in rows:
        symbol = row[0] if not hasattr(row, "keys") else row["symbol"]
        result["processed"] += 1
        ok = _backfill_price_history_to_cache(symbol)
        con2 = _conn()
        try:
            _ensure_backfill_queue_table(con2)
            con2.execute(
                "UPDATE price_backfill_queue SET status=? WHERE symbol=?",
                ("done" if ok else "failed", symbol),
            )
            con2.commit()
        finally:
            con2.close()
        result["succeeded" if ok else "failed"] += 1
    return result


_price_backfill_watcher_started = False
_price_backfill_watcher_lock = None


def start_price_backfill_watcher(interval_seconds: int = 120, batch_size: int = 5):
    """Historical backfill is a manual, one-time action (per explicit
    design decision): this now only registers with job_registry so it's
    visible on the Scheduler Hub page with a "Run Now" button -- it does
    NOT register with unified_scheduler, so it never runs automatically
    in the background. Symbols still get queued for backfill on demand
    by _history() when a scan hits one with too little local data; this
    is what you manually trigger (or automate on your own schedule if
    you want it back) to actually process that queue."""
    global _price_backfill_watcher_started, _price_backfill_watcher_lock
    import threading
    if _price_backfill_watcher_lock is None:
        _price_backfill_watcher_lock = threading.Lock()
    with _price_backfill_watcher_lock:
        if _price_backfill_watcher_started:
            return False
        _price_backfill_watcher_started = True

    from ..services.job_registry import register_job
    register_job(
        "scanner_price_backfill", "Scanner price history backfill (manual)",
        "Backfills 3 years of daily history for symbols the scanner engine "
        "found with too little local data to compute slow indicators "
        "(e.g. rsidiff90) reliably. Manual/one-time by design -- use Run Now "
        "when you add new symbols, rather than running continuously in the "
        "background.",
        kind="interval", default_schedule={"interval_min": max(1, int(interval_seconds / 60))},
        group="Manual / One-Time", run_now_fn=lambda: run_pending_price_backfills(9999),
    )
    return True



def _ensure_intraday_backfill_queue_table(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS intraday_backfill_queue (
        symbol TEXT PRIMARY KEY,
        queued_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
    )""")


def _enqueue_intraday_backfill(symbol: str) -> None:
    """Non-blocking: records that this symbol needs an hourly-data
    backfill. Kept in its own queue table, separate from
    price_backfill_queue (daily) -- intraday backfills are a materially
    heavier fetch (~3,300 hourly bars vs ~2,800 daily bars over a similar
    window) and worth rate-limiting independently rather than competing
    with daily backfills for the same queue's throughput."""
    con = _conn()
    try:
        _ensure_intraday_backfill_queue_table(con)
        con.execute(
            "INSERT OR IGNORE INTO intraday_backfill_queue (symbol, queued_at, status) VALUES (?, datetime('now'), 'pending')",
            (symbol,),
        )
        con.commit()
    finally:
        con.close()
    _track_enqueued_backfill(symbol, "intraday")


def run_pending_intraday_backfills(max_symbols: int = 3) -> Dict[str, int]:
    """Background job: processes a small batch of queued symbols per call
    (default 3 -- smaller than daily's default 5, since each intraday
    fetch is a heavier request). Same non-blocking-for-scans design as
    run_pending_price_backfills."""
    con = _conn()
    try:
        _ensure_intraday_backfill_queue_table(con)
        rows = con.execute(
            "SELECT symbol FROM intraday_backfill_queue WHERE status='pending' ORDER BY queued_at ASC LIMIT ?",
            (max_symbols,),
        ).fetchall()
    finally:
        con.close()

    result = {"processed": 0, "succeeded": 0, "failed": 0}
    for row in rows:
        symbol = row[0] if not hasattr(row, "keys") else row["symbol"]
        result["processed"] += 1
        ok = _backfill_intraday_history_to_cache(symbol)
        con2 = _conn()
        try:
            _ensure_intraday_backfill_queue_table(con2)
            con2.execute(
                "UPDATE intraday_backfill_queue SET status=? WHERE symbol=?",
                ("done" if ok else "failed", symbol),
            )
            con2.commit()
        finally:
            con2.close()
        result["succeeded" if ok else "failed"] += 1
    return result


_intraday_backfill_watcher_started = False
_intraday_backfill_watcher_lock = None


def start_intraday_backfill_watcher(interval_seconds: int = 150, batch_size: int = 3):
    """Same pattern as start_price_backfill_watcher, for hourly data.
    Slightly longer interval and smaller batch than the daily watcher,
    since each intraday fetch is heavier."""
    global _intraday_backfill_watcher_started, _intraday_backfill_watcher_lock
    import threading
    if _intraday_backfill_watcher_lock is None:
        _intraday_backfill_watcher_lock = threading.Lock()
    with _intraday_backfill_watcher_lock:
        if _intraday_backfill_watcher_started:
            return False
        _intraday_backfill_watcher_started = True

    from ..services.job_registry import register_job
    register_job(
        "scanner_intraday_backfill", "Scanner intraday (1h/2h/4h) history backfill (manual)",
        "Backfills ~2 years of hourly history for symbols the scanner engine "
        "found with too little local intraday data -- 2h/4h are derived from "
        "this same 1h data by resampling, not fetched separately. Manual/"
        "one-time by design -- use Run Now when you add new symbols.",
        kind="interval", default_schedule={"interval_min": max(1, int(interval_seconds / 60))},
        group="Manual / One-Time", run_now_fn=lambda: run_pending_intraday_backfills(9999),
    )
    return True


# ---------------------------------------------------------------------
# "Keep fresh" jobs -- distinct from the watchers above. Those only
# process symbols explicitly queued because they were too THIN when a
# scan touched them. These instead cycle through every symbol that
# ALREADY has cached data and refreshes it, so a symbol backfilled once
# doesn't silently go stale over time with nothing ever re-checking it.
# Cheap to run broadly now that the backfill functions are gap-aware --
# an already-current symbol costs one small incremental request, not a
# full re-fetch, so cycling the whole cached universe through this
# regularly is affordable.
# ---------------------------------------------------------------------
def get_all_cached_daily_symbols() -> List[str]:
    con = _conn()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS price_cache (
            symbol TEXT NOT NULL, date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (symbol, date))""")
        rows = con.execute("SELECT DISTINCT symbol FROM price_cache").fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def get_all_cached_intraday_symbols() -> List[str]:
    con = _conn()
    try:
        _ensure_intraday_table(con)
        rows = con.execute("SELECT DISTINCT symbol FROM intraday_price_cache").fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def _rotate_oldest_first(all_symbols: List[str], last_touched: Dict[str, str], n: int) -> List[str]:
    ranked = sorted(all_symbols, key=lambda s: last_touched.get(s) or "")
    return ranked[:n]


def run_daily_price_refresh(max_symbols: int = 25) -> Dict[str, int]:
    """Keeps already-cached DAILY symbols fresh: a rotating batch each
    call, oldest-refreshed-first, so every cached symbol gets touched
    roughly once per day given a reasonable interval. Each call is cheap
    for symbols already current (gap-aware backfill does a tiny
    incremental check, not a full re-fetch)."""
    all_symbols = get_all_cached_daily_symbols()
    if not all_symbols:
        return {"processed": 0, "succeeded": 0, "failed": 0}
    con = _conn()
    try:
        rows = con.execute(
            "SELECT symbol, MAX(date) as last FROM price_cache WHERE symbol IN ({}) GROUP BY symbol".format(
                ",".join("?" * len(all_symbols))),
            all_symbols,
        ).fetchall()
        last_touched = {r[0]: r[1] for r in rows}
    finally:
        con.close()
    batch = _rotate_oldest_first(all_symbols, last_touched, max_symbols)

    result = {"processed": 0, "succeeded": 0, "failed": 0}
    for symbol in batch:
        result["processed"] += 1
        try:
            ok = _backfill_price_history_to_cache(symbol)
        except Exception as e:  # noqa: BLE001
            print(f"[scanner_builder] daily refresh failed for {symbol}: {type(e).__name__}: {e}")
            ok = False
        result["succeeded" if ok else "failed"] += 1
    return result


def run_intraday_price_refresh(max_symbols: int = 15) -> Dict[str, int]:
    """Same as run_daily_price_refresh, for already-cached intraday
    symbols. Smaller batch than daily since each fetch is heavier."""
    all_symbols = get_all_cached_intraday_symbols()
    if not all_symbols:
        return {"processed": 0, "succeeded": 0, "failed": 0}
    con = _conn()
    try:
        rows = con.execute(
            "SELECT symbol, MAX(ts) as last FROM intraday_price_cache WHERE symbol IN ({}) GROUP BY symbol".format(
                ",".join("?" * len(all_symbols))),
            all_symbols,
        ).fetchall()
        last_touched = {r[0]: r[1] for r in rows}
    finally:
        con.close()
    batch = _rotate_oldest_first(all_symbols, last_touched, max_symbols)

    result = {"processed": 0, "succeeded": 0, "failed": 0}
    for symbol in batch:
        result["processed"] += 1
        try:
            ok = _backfill_intraday_history_to_cache(symbol)
        except Exception as e:  # noqa: BLE001
            print(f"[scanner_builder] intraday refresh failed for {symbol}: {type(e).__name__}: {e}")
            ok = False
        result["succeeded" if ok else "failed"] += 1
    return result


_daily_refresh_watcher_started = False
_daily_refresh_watcher_lock = None


def start_daily_price_refresh_watcher(interval_seconds: int = 300, batch_size: int = 25):
    """This work now happens as step 3 of the 7:30 AM morning_data_pipeline
    job (see scheduled_jobs.py) instead of running continuously in the
    background all day. Still registered here (job_registry only, no
    unified_scheduler dispatch) so it stays visible on the Scheduler Hub
    page with a manual "Run Now" for an ad-hoc refresh outside the 7:30
    window."""
    global _daily_refresh_watcher_started, _daily_refresh_watcher_lock
    import threading
    if _daily_refresh_watcher_lock is None:
        _daily_refresh_watcher_lock = threading.Lock()
    with _daily_refresh_watcher_lock:
        if _daily_refresh_watcher_started:
            return False
        _daily_refresh_watcher_started = True

    from ..services.job_registry import register_job
    register_job(
        "scanner_daily_price_refresh", "Scanner daily price cache refresh (runs at 7:30 AM)",
        "Keeps already-backfilled daily price_cache symbols current going forward "
        "(gap-aware -- only fetches new days since each symbol's last cached date, "
        "not a full re-fetch). Runs automatically as part of the 7:30 AM morning "
        "data pipeline; Run Now here triggers an extra ad-hoc refresh.",
        kind="interval", default_schedule={"interval_min": max(1, int(interval_seconds / 60))},
        group="Manual / One-Time", run_now_fn=lambda: run_daily_price_refresh(9999),
    )
    return True


_intraday_refresh_watcher_started = False
_intraday_refresh_watcher_lock = None


def start_intraday_price_refresh_watcher(interval_seconds: int = 420, batch_size: int = 15):
    """This work now happens as step 4 of the 7:30 AM morning_data_pipeline
    job (see scheduled_jobs.py). Still registered here (job_registry
    only, no unified_scheduler dispatch) for Scheduler Hub visibility and
    a manual "Run Now" for an ad-hoc refresh outside the 7:30 window."""
    global _intraday_refresh_watcher_started, _intraday_refresh_watcher_lock
    import threading
    if _intraday_refresh_watcher_lock is None:
        _intraday_refresh_watcher_lock = threading.Lock()
    with _intraday_refresh_watcher_lock:
        if _intraday_refresh_watcher_started:
            return False
        _intraday_refresh_watcher_started = True

    from ..services.job_registry import register_job
    register_job(
        "scanner_intraday_price_refresh", "Scanner intraday price cache refresh (runs at 7:30 AM)",
        "Keeps already-backfilled intraday (1h base) symbols current going forward, "
        "gap-aware -- fetches only new bars since each symbol's last cached timestamp. "
        "Runs automatically as part of the 7:30 AM morning data pipeline; Run Now "
        "here triggers an extra ad-hoc refresh.",
        kind="interval", default_schedule={"interval_min": max(1, int(interval_seconds / 60))},
        group="Manual / One-Time", run_now_fn=lambda: run_intraday_price_refresh(9999),
    )
    return True


# ---------------------------------------------------------------------
# Manual, on-demand bulk backfill -- separate from the automatic watcher
# above entirely. That watcher only processes a small rate-limited batch
# (5 symbols/2min) continuously; this is for deliberately backfilling an
# entire watchlist's price+volume history in one go (e.g. "run this over
# the weekend"), triggered explicitly, never automatically. Doesn't touch
# or interfere with the automatic watcher's queue -- this works directly
# off a symbol list you provide.
# ---------------------------------------------------------------------
_bulk_backfill_status: Dict[str, Any] = {
    "running": False, "processed": 0, "total": 0, "succeeded": 0, "failed": 0,
    "current_symbol": None, "started_at": None, "finished_at": None,
}
_bulk_backfill_lock = None


def _is_daily_data_fresh(symbol: str, max_age_days: int = 4, months: int = None) -> bool:
    """True if this symbol's price_cache data is already current enough
    to skip entirely in a bulk operation -- not just "cheap to check" but
    "not worth even the network round-trip or the polite delay between
    symbols." 4 days covers weekends/holidays without needing a real
    market-calendar lookup.

    Checks BOTH directions, not just "is the latest date recent": a
    symbol whose daily 7:30 AM refresh keeps it current going forward
    will ALWAYS look fresh by the max-date check alone, even if it only
    has 120 days of history and 1200 were just requested -- that's
    exactly the bug that let a "1200 day" bulk backfill finish in under a
    minute having fetched almost nothing. If `months` is given, also
    requires the cache's EARLIEST date to reach back far enough for that
    window; otherwise this only protects the "already fully backfilled,
    a plain re-run should be fast" case it was originally built for.
    """
    max_date = _get_max_cached_date(symbol)
    if not max_date:
        return False
    try:
        max_dt = datetime.strptime(max_date, "%Y-%m-%d")
    except Exception:
        return False
    if (datetime.now() - max_dt).days > max_age_days:
        return False
    if months is not None:
        min_date = _get_min_cached_date(symbol)
        if not min_date:
            return False
        requested_start = (datetime.now() - timedelta(days=int(max(1, months)) * 30)).strftime("%Y-%m-%d")
        if min_date > requested_start:
            return False  # cache doesn't go back far enough for what was just requested
    return True


def bulk_backfill_symbols(symbols: List[str], months: int = 132, delay_seconds: float = 1.0) -> None:
    """Runs synchronously in whatever thread calls it -- callers that want
    this to not block (e.g. the API route below) should run it in a
    background thread. Price + volume only, same fields the automatic
    backfill already stores -- nothing else.

    Symbols that already have fresh data are skipped entirely -- no fetch,
    no per-symbol delay. This is what makes re-running this button on an
    already-backfilled watchlist fast (seconds, touching only what
    actually changed) instead of slow (minutes, from the cumulative
    polite-delay across hundreds of symbols that had nothing to update)."""
    global _bulk_backfill_status
    import threading
    import time as _time
    global _bulk_backfill_lock
    if _bulk_backfill_lock is None:
        _bulk_backfill_lock = threading.Lock()

    with _bulk_backfill_lock:
        if _bulk_backfill_status["running"]:
            return
        _bulk_backfill_status = {
            "running": True, "processed": 0, "total": len(symbols),
            "succeeded": 0, "failed": 0, "skipped_fresh": 0, "current_symbol": None,
            "started_at": datetime.now().isoformat(timespec="seconds"), "finished_at": None,
        }

    for sym in symbols:
        with _bulk_backfill_lock:
            _bulk_backfill_status["current_symbol"] = sym
        if _is_daily_data_fresh(sym, months=months):
            with _bulk_backfill_lock:
                _bulk_backfill_status["processed"] += 1
                _bulk_backfill_status["succeeded"] += 1
                _bulk_backfill_status["skipped_fresh"] += 1
            continue  # no fetch, no delay -- this symbol needed nothing
        ok = _backfill_price_history_to_cache(sym, months=months)
        with _bulk_backfill_lock:
            _bulk_backfill_status["processed"] += 1
            _bulk_backfill_status["succeeded" if ok else "failed"] += 1
        _time.sleep(max(0.0, delay_seconds))

    with _bulk_backfill_lock:
        _bulk_backfill_status["running"] = False
        _bulk_backfill_status["current_symbol"] = None
        _bulk_backfill_status["finished_at"] = datetime.now().isoformat(timespec="seconds")


@scanner_builder_bp.route("/api/bulk-backfill", methods=["POST"])
def api_bulk_backfill():
    """Manually trigger a full watchlist price+volume backfill. Runs in a
    background thread -- this returns immediately with 'started': true;
    poll /api/bulk-backfill/status for progress. Body:
        {"watchlist_id": 3, "months": 36}          -- backfill a watchlist, or
        {"symbols": ["AAPL","MSFT"], "months": 36} -- backfill an explicit list
    """
    import threading
    payload = request.get_json(force=True) or {}
    months = int(payload.get("months") or 132)
    symbols = payload.get("symbols")
    if not symbols:
        watchlist_id = payload.get("watchlist_id")
        symbols = _watchlist_symbols(watchlist_id)
    symbols = list(dict.fromkeys(str(s).upper().strip() for s in (symbols or []) if s))
    if not symbols:
        return jsonify({"error": "No symbols found -- pass watchlist_id or symbols"}), 400
    if _bulk_backfill_status["running"]:
        return jsonify({"error": "A bulk backfill is already running", "status": _bulk_backfill_status}), 409

    action_key = f"wl_backfill_history_{payload.get('watchlist_id')}" if payload.get("watchlist_id") else "bulk_backfill_history"

    def _run_with_log():
        from ..services.job_registry import log_run_start, log_run_finish
        run_id = log_run_start(action_key)
        try:
            bulk_backfill_symbols(symbols, months)
            log_run_finish(run_id, True, f"{len(symbols)} symbols, {months} months")
        except Exception as e:
            log_run_finish(run_id, False, str(e))

    t = threading.Thread(target=_run_with_log, daemon=True)
    t.start()
    return jsonify({"ok": True, "started": True, "symbol_count": len(symbols), "months": months})


@scanner_builder_bp.route("/api/bulk-backfill/status")
def api_bulk_backfill_status():
    return jsonify(_bulk_backfill_status)


_bulk_intraday_status: Dict[str, Any] = {
    "running": False, "processed": 0, "total": 0, "succeeded": 0, "failed": 0,
    "current_symbol": None, "started_at": None, "finished_at": None,
}
_bulk_intraday_lock = None


def _is_intraday_data_fresh(symbol: str, max_age_hours: int = 24, days: int = None) -> bool:
    """Same principle as _is_daily_data_fresh, for intraday -- true if
    this symbol's most recent cached hourly bar is recent enough AND (if
    `days` is given) the cache already reaches back far enough for the
    requested window, not just "is it current going forward." Same bug
    fix as the daily version: without the backward check, a symbol kept
    current by routine refreshes always looks "fresh" regardless of how
    much history was actually requested."""
    max_ts = _get_max_cached_intraday_ts(symbol)
    if not max_ts:
        return False
    try:
        max_dt = pd.Timestamp(max_ts)
    except Exception:
        return False
    if (pd.Timestamp.now() - max_dt).total_seconds() / 3600.0 > max_age_hours:
        return False
    if days is not None:
        min_ts = _get_min_cached_intraday_ts(symbol)
        if not min_ts:
            return False
        requested_start = pd.Timestamp.now() - pd.Timedelta(days=min(int(max(1, days)), INTRADAY_RETENTION_DAYS))
        if pd.Timestamp(min_ts) > requested_start:
            return False  # cache doesn't go back far enough for what was just requested
    return True


def bulk_backfill_intraday_symbols(symbols: List[str], days: int = INTRADAY_RETENTION_DAYS,
                                    delay_seconds: float = 1.5) -> None:
    """Same pattern as bulk_backfill_symbols (daily), for hourly data.
    Slightly longer delay between symbols than the daily version, since
    each intraday fetch is a heavier request. Same skip-if-fresh behavior
    too -- symbols with recent enough data are skipped entirely, no fetch
    and no delay, so re-running this on an already-current watchlist is
    fast rather than slow."""
    global _bulk_intraday_status
    import threading
    import time as _time
    global _bulk_intraday_lock
    if _bulk_intraday_lock is None:
        _bulk_intraday_lock = threading.Lock()

    with _bulk_intraday_lock:
        if _bulk_intraday_status["running"]:
            return
        _bulk_intraday_status = {
            "running": True, "processed": 0, "total": len(symbols),
            "succeeded": 0, "failed": 0, "skipped_fresh": 0, "current_symbol": None,
            "started_at": datetime.now().isoformat(timespec="seconds"), "finished_at": None,
        }

    for sym in symbols:
        with _bulk_intraday_lock:
            _bulk_intraday_status["current_symbol"] = sym
        if _is_intraday_data_fresh(sym, days=days):
            with _bulk_intraday_lock:
                _bulk_intraday_status["processed"] += 1
                _bulk_intraday_status["succeeded"] += 1
                _bulk_intraday_status["skipped_fresh"] += 1
            continue
        ok = _backfill_intraday_history_to_cache(sym, days=days)
        with _bulk_intraday_lock:
            _bulk_intraday_status["processed"] += 1
            _bulk_intraday_status["succeeded" if ok else "failed"] += 1
        _time.sleep(max(0.0, delay_seconds))

    with _bulk_intraday_lock:
        _bulk_intraday_status["running"] = False
        _bulk_intraday_status["current_symbol"] = None
        _bulk_intraday_status["finished_at"] = datetime.now().isoformat(timespec="seconds")


@scanner_builder_bp.route("/api/bulk-backfill-intraday", methods=["POST"])
def api_bulk_backfill_intraday():
    """Manually trigger a full watchlist hourly (1h base, 2h/4h derived)
    backfill. Same usage pattern as /api/bulk-backfill:
        {"watchlist_id": 3, "days": 729}          -- backfill a watchlist, or
        {"symbols": ["AAPL","MSFT"], "days": 729} -- backfill an explicit list
    """
    import threading
    payload = request.get_json(force=True) or {}
    days = int(payload.get("days") or INTRADAY_RETENTION_DAYS)
    symbols = payload.get("symbols")
    if not symbols:
        watchlist_id = payload.get("watchlist_id")
        symbols = _watchlist_symbols(watchlist_id)
    symbols = list(dict.fromkeys(str(s).upper().strip() for s in (symbols or []) if s))
    if not symbols:
        return jsonify({"error": "No symbols found -- pass watchlist_id or symbols"}), 400
    if _bulk_intraday_status["running"]:
        return jsonify({"error": "A bulk intraday backfill is already running", "status": _bulk_intraday_status}), 409

    action_key = f"wl_backfill_intraday_{payload.get('watchlist_id')}" if payload.get("watchlist_id") else "bulk_backfill_intraday"

    def _run_with_log():
        from ..services.job_registry import log_run_start, log_run_finish
        run_id = log_run_start(action_key)
        try:
            bulk_backfill_intraday_symbols(symbols, days)
            log_run_finish(run_id, True, f"{len(symbols)} symbols, {days} days")
        except Exception as e:
            log_run_finish(run_id, False, str(e))

    t = threading.Thread(target=_run_with_log, daemon=True)
    t.start()
    return jsonify({"ok": True, "started": True, "symbol_count": len(symbols), "days": days})


@scanner_builder_bp.route("/api/bulk-backfill-intraday/status")
def api_bulk_backfill_intraday_status():
    return jsonify(_bulk_intraday_status)


@scanner_builder_bp.route("/api/debug/rsi/<symbol>")
def api_debug_rsi(symbol: str):
    """Dumps exactly what oiapp has stored and computed for a symbol, for
    direct digit-by-digit comparison against another source (e.g. a
    TradingView chart) -- the fastest way to tell whether a discrepancy is
    a genuine data difference (different close prices) versus comparing
    against a different indicator/formula entirely.
    Usage: /scanner-builder/api/debug/rsi/BABA?days=10&tf=1w

    ?tf= defaults to 1d. This matters a lot for rsidiff90: it needs 6x its
    period in valid bars to be trusted (~540 bars for the default
    period=90) -- for daily that's ~2.1 years, easily met by most symbols'
    backfilled history; for WEEKLY that's ~540 WEEKS, over 10 years of
    data. Most symbols (especially anything that IPO'd more recently than
    that, or wherever backfill history is shorter than the full target
    window) will show rsidiff90_trusted=false for "1w" even though the
    exact same query works fine on daily -- that's this threshold doing
    its job, not a bug in the indicator.
    """
    symbol = symbol.upper().strip()
    days = int(request.args.get("days") or 10)
    tf = (request.args.get("tf") or "1d").strip()

    df = _history(symbol, tf)
    if df is None or df.empty:
        return jsonify({"symbol": symbol, "timeframe": tf, "error": "no local history available for this symbol/timeframe"})

    close = df["Close"].astype(float)
    rsi14 = _rsi(close, 14)
    valid_rsi_bars = int(rsi14.notna().sum())
    ema90 = _ema(rsi14, 90) if valid_rsi_bars >= 540 else None
    rsidiff90 = (rsi14 - ema90) if ema90 is not None else None

    recent = df.tail(days)
    rows = []
    for ts in recent.index:
        rows.append({
            "date": pd.Timestamp(ts).strftime("%Y-%m-%d"),
            "close": round(float(close.loc[ts]), 4),
            "rsi14": round(float(rsi14.loc[ts]), 4) if pd.notna(rsi14.loc[ts]) else None,
            "rsidiff90": round(float(rsidiff90.loc[ts]), 4) if rsidiff90 is not None and pd.notna(rsidiff90.loc[ts]) else None,
        })

    return jsonify({
        "symbol": symbol,
        "timeframe": tf,
        "total_bars_available": len(df),
        "valid_rsi_bars": valid_rsi_bars,
        "rsidiff90_convergence_threshold": 540,
        "rsidiff90_trusted": valid_rsi_bars >= 540,
        "bars_needed_for_trust": max(0, 540 - valid_rsi_bars),
        "recent_days": rows,
        "latest_close": round(float(close.iloc[-1]), 4),
        "latest_date": pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d"),
    })


def _history(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    symbol = str(symbol or "").upper().strip()
    tf = _normalize_tf(tf)
    mode = _scanner_history_source_mode()

    # Intraday (1h/2h/4h): completely separate path from daily/weekly below
    # -- daily-cached data can only be resampled to COARSER timeframes
    # (daily->weekly->monthly), never finer ones, so 1h/2h/4h need their
    # own base-resolution cache (intraday_price_cache, 1h as the base,
    # 2h/4h derived by resampling on read -- see _history_from_local_intraday).
    if tf in ("1h", "2h", "4h") and mode in {"auto", "db", "local", "cache", "price_cache"}:
        local = _history_from_local_intraday(symbol, tf)
        # Same 500-bar threshold rationale as daily (see below) -- 540 is
        # rsidiff90's own convergence requirement; 500 gives a small margin
        # for "trust this without a backfill" while still comfortably
        # covering the ~843 four-hour bars a full 729-day 1h backfill
        # produces.
        # Row count alone isn't enough -- a symbol whose ingestion silently
        # stalled weeks/months ago but still has plenty of OLD rows would
        # pass a count-only check forever, computing indicators from data
        # that never caught up. Reuses the same freshness check already
        # applied to the fallback provider path below (previously only
        # wired there, not here -- "local DB rows are accepted by source
        # preference" was the original assumption, which is exactly what
        # breaks when ingestion stalls for one symbol without anyone
        # noticing).
        if local is not None and not local.empty and len(local) >= 500:
            if _is_history_fresh_enough(local, tf):
                return local.copy()
            # Sufficient count, but stale -- do NOT fall through to the
            # low-bar (>=25) acceptance below; that path exists for
            # genuinely-thin history, not this case, and falling through
            # would return the exact stale data this check exists to
            # reject.
            try:
                _enqueue_intraday_backfill(symbol)
            except Exception:
                pass
            if mode in {"db", "local", "cache", "price_cache"}:
                return None
        else:
            try:
                _enqueue_intraday_backfill(symbol)
            except Exception:
                pass
            if local is not None and not local.empty and len(local) >= 25:
                return local.copy()
            if mode in {"db", "local", "cache", "price_cache"}:
                return None

    # DB-first for daily/weekly/monthly scanner signals.  This keeps Lookback()
    # and UAE primitives aligned with the dashboard/chart cache instead of a
    # separate provider feed that can be stale or missing recent bars.
    if mode in {"auto", "db", "local", "cache", "price_cache"}:
        local = _history_from_local_daily(symbol, tf)
        # Directly measured (not just estimated): rsidiff90 = rsi14 - ema(rsi14,90)
        # computed from only 200 bars can be off from its properly-converged
        # value by 0.5-0.7+ points on typical data -- 200 was too low a bar,
        # not a safe threshold. Convergence to a negligible (<0.01) difference
        # empirically starts around 400-500 bars, so 500 is used here as the
        # threshold for "trust this without a backfill."
        #
        # Row count alone isn't enough, though -- confirmed as a real bug:
        # a symbol whose daily ingestion silently stalled (weeks or months
        # ago) but still has plenty of OLD rows passed this count-only
        # check indefinitely, with indicators like RSI computed from a
        # price series that never caught up to current price action --
        # no error, just a confidently-wrong number (e.g. RSI reading
        # deeply oversold from stale data while the real, current chart
        # was well into overbought territory after a since-happened
        # rally). Reuses the same _is_history_fresh_enough() check
        # already applied to the fallback provider path further below --
        # it just wasn't wired into this, the primary DB-first path most
        # symbols actually go through.
        if local is not None and not local.empty and len(local) >= 500:
            if _is_history_fresh_enough(local, tf):
                return local.copy()
            # Sufficient count, but stale -- do NOT fall through to the
            # low-bar (>=25) acceptance below; that path exists for
            # genuinely-thin history, not this case, and falling through
            # would return the exact stale data this check exists to
            # reject.
            if tf == "1d":
                try:
                    _enqueue_price_backfill(symbol)
                except Exception:
                    pass
            if mode in {"db", "local", "cache", "price_cache"}:
                return None
        else:
            # IMPORTANT: do NOT backfill synchronously here. This function runs
            # inside every symbol's scan (up to 8 concurrent per api_run()'s
            # ThreadPoolExecutor) -- a live yfinance fetch per thin-history
            # symbol here means a watchlist where most symbols need backfilling
            # turns one interactive query into potentially 100+ blocking network
            # calls serialized through a handful of worker threads, which is
            # exactly what made a scan take 30+ minutes with no response.
            # Instead: just record that this symbol needs backfilling (cheap,
            # non-blocking) and let a separate background job (see
            # run_pending_price_backfills below) process a few at a time,
            # rate-limited, without ever blocking an interactive scan.
            if tf == "1d":
                try:
                    _enqueue_price_backfill(symbol)
                except Exception:
                    pass
            if local is not None and not local.empty and len(local) >= 25:
                return local.copy()
            if mode in {"db", "local", "cache", "price_cache"}:
                return None

    df = _fetch_history_cached(symbol, tf)
    if df is None or df.empty:
        return None
    df = _normalise_ohlcv_frame(df)
    if df is None or df.empty:
        return None
    # Avoid treating old provider data as current.  This was the likely cause of
    # old weekly UAE triangles appearing inside a short Lookback() window.
    if mode != "yf" and tf in {"1d", "1w", "1m", "3m"} and not _is_history_fresh_enough(df, tf):
        return None
    return df.copy()


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


def _relative_strength(close: pd.Series, bench_close: pd.Series) -> pd.Series:
    sym_ret = close.pct_change(20) * 100.0
    bench_ret = bench_close.pct_change(20) * 100.0
    rs = 50.0 + 4.0 * (sym_ret - bench_ret)
    return rs.clip(lower=0, upper=100)


def _period_return_pct(close: pd.Series, period: int, shift: int = 0) -> Optional[float]:
    period = max(1, int(period or 1))
    if close is None:
        return None
    idx_now = len(close) - 1 - shift
    idx_prev = idx_now - period
    if idx_prev < 0 or idx_now < 0 or idx_now >= len(close):
        return None
    try:
        prev = float(close.iloc[idx_prev])
        now = float(close.iloc[idx_now])
    except Exception:
        return None
    if prev == 0:
        return None
    return ((now / prev) - 1.0) * 100.0


def _benchmark_df_from_ctx(ctx: Dict[str, Any], benchmark: str, tf: str) -> Optional[pd.DataFrame]:
    """Return benchmark history already scoped to the current scan context.

    Backtests build a context from data available only up to the simulated
    trading day.  When that scoped benchmark history is present, use it instead
    of calling _history(), which would fetch data through the present and leak
    future bars into historical RelativeStrength()/MansfieldRS() evaluations.
    Live scans do not provide benchmark_history, so they keep the existing
    yfinance-backed behavior.
    """
    tf = _normalize_tf(tf)
    maps = (
        ctx.get("benchmark_history") or {},
        ctx.get("benchmark_histories") or {},
        ctx.get("benchmark_timeframes_raw") or {},
    )
    for m in maps:
        try:
            df = m.get(tf)
        except Exception:
            df = None
        if isinstance(df, pd.DataFrame) and not df.empty and "Close" in df.columns:
            return df
    if any(k in ctx for k in ("benchmark_history", "benchmark_histories", "benchmark_timeframes_raw")):
        return None
    bench = str(benchmark or ctx.get('benchmark') or 'SPY').strip().upper() or 'SPY'
    return _history(bench, tf)


def _relative_strength_value(ctx: Dict[str, Any], benchmark: str, period: int, tf: str = '1d', shift: int = 0) -> Optional[float]:
    tf = _normalize_tf(tf)
    snap = ctx.get('timeframes', {}).get(tf)
    if not snap:
        return None
    close = snap.get('series', {}).get('close')
    if close is None:
        return None
    bench_df = _benchmark_df_from_ctx(ctx, benchmark, tf)
    if bench_df is None or bench_df.empty:
        return None
    bench_close = bench_df['Close'].astype(float)
    sym_ret = _period_return_pct(close, period, shift=shift)
    bench_ret = _period_return_pct(bench_close, period, shift=shift)
    if sym_ret is None or bench_ret is None:
        return None
    return float(sym_ret - bench_ret)


def _sector_rs_value(ctx: Dict[str, Any], period: int = 20, tf: str = '1d', shift: int = 0) -> Optional[float]:
    """Return stock percent-return minus its sector ETF percent-return.

    Positive values mean the stock is outperforming its own sector; negative
    values mean it is lagging the sector. The result is expressed in percentage
    points, e.g. +3.5 means the stock beat the sector ETF by 3.5 points.
    """
    tf = _normalize_tf(tf)
    snap = ctx.get('timeframes', {}).get(tf)
    if not snap:
        return None
    close = snap.get('series', {}).get('close')
    if close is None:
        return None
    etf = str(ctx.get('sector_etf') or '').strip().upper()
    if not etf:
        return None
    sec_df = _history(etf, tf)
    if sec_df is None or sec_df.empty or 'Close' not in sec_df.columns:
        return None
    sec_close = sec_df['Close'].astype(float)
    sym_ret = _period_return_pct(close, period, shift=shift)
    sec_ret = _period_return_pct(sec_close, period, shift=shift)
    if sym_ret is None or sec_ret is None:
        return None
    return float(sym_ret - sec_ret)


def _mansfield_rs_value(ctx: Dict[str, Any], benchmark: str, period: int, tf: str = '1d', shift: int = 0) -> Optional[float]:
    tf = _normalize_tf(tf)
    snap = ctx.get('timeframes', {}).get(tf)
    if not snap:
        return None
    close = snap.get('series', {}).get('close')
    if close is None:
        return None
    bench_df = _benchmark_df_from_ctx(ctx, benchmark, tf)
    if bench_df is None or bench_df.empty:
        return None
    bench_close = bench_df['Close'].astype(float)
    aligned = pd.concat([close, bench_close], axis=1, join='inner').dropna()
    if aligned.empty:
        return None
    if shift > 0:
        if len(aligned) <= shift:
            return None
        aligned = aligned.iloc[:-shift]
    period = max(1, int(period or 1))
    if len(aligned) < period:
        return None
    ratio = aligned.iloc[:, 0] / aligned.iloc[:, 1]
    ma = ratio.rolling(window=period, min_periods=period).mean()
    current = float(ratio.iloc[-1])
    base = ma.iloc[-1]
    if pd.isna(base) or base == 0:
        return None
    return ((current / float(base)) - 1.0) * 100.0



def _prepare_snapshot(df: pd.DataFrame, bench_df: Optional[pd.DataFrame], tf: str,
                       symbol: Optional[str] = None) -> Dict[str, Any]:
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    open_ = df["Open"].astype(float)
    vol = df["Volume"].astype(float)

    rsi3 = _rsi(close, 3)
    rsi14 = _rsi(close, 14)
    ema_rsi13 = _ema(rsi14, 13)
    # EMA(RSI,90) needs a genuinely long, mature RSI history to mean anything.
    # Originally used a 180-bar threshold ("~2x the span"); directly measured
    # since then (holding a fixed point in time and varying how much history
    # feeds the calculation) that 200 bars can still be off from the fully-
    # converged value by 0.5-0.7+ points, and convergence to a negligible
    # (<0.01) difference only starts around 400-500 bars. Updated accordingly.
    valid_rsi_bars = int(rsi14.notna().sum())
    if valid_rsi_bars >= 500:
        ema_rsi90 = _ema(rsi14, 90)
        rsi_diff_90 = rsi14 - ema_rsi90
    else:
        ema_rsi90 = pd.Series([float("nan")] * len(rsi14), index=rsi14.index)
        rsi_diff_90 = pd.Series([float("nan")] * len(rsi14), index=rsi14.index)
    ema5 = _ema(close, 5)
    ema9 = _ema(close, 9)
    ema13 = _ema(close, 13)
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    ema200 = _ema(close, 200)
    macd_line, macd_signal, macd_hist = _macd(close)

    rs = None
    if bench_df is not None and not bench_df.empty:
        bench_close = bench_df["Close"].astype(float)
        aligned = pd.concat([close, bench_close], axis=1, join="inner").dropna()
        if len(aligned) >= 25:
            rs = _relative_strength(aligned.iloc[:, 0], aligned.iloc[:, 1])
            rs = rs.reindex(close.index, method="ffill")
    if rs is None:
        rs = pd.Series([50.0] * len(close), index=close.index)

    # Write-through to the technical_snapshot cache when we have a symbol
    # and this is a timeframe the cache tracks (1d/1w). This function
    # already computes all of this for its own query engine purposes --
    # sharing it costs nothing extra and means other consumers
    # (trade_opportunity_scanner's _get_ta, conviction_scorer, future
    # wiring) can read an already-computed value instead of redoing the
    # same work. Merges with any existing record rather than overwriting
    # (see store_technical_snapshot) -- this function doesn't compute
    # ADX/DI+/DI-/S-R, so a blind overwrite would wipe those out if
    # regime_scanner had already filled them in for today.
    if symbol and tf in ("1d", "1w") and len(close) > 0:
        try:
            from ..services.technical_snapshot import queue_technical_snapshot_write, is_snapshot_complete_today
            # Cheap check first (one indexed SELECT) -- skip the more
            # expensive merge-write entirely if today's record is already
            # complete. Without this, every symbol in every scanner query
            # did a full SELECT+INSERT merge on every single pass,
            # regardless of whether anything needed to change -- directly
            # measurable in a 38-candidate scanner pass as 38 redundant
            # writes per run, most of which had nothing new to contribute.
            if not is_snapshot_complete_today(symbol.upper().strip(), tf):
                valid_rsi_bars_wt = int(rsi14.notna().sum())
                rsidiff90_ok_wt = valid_rsi_bars_wt >= 540
                queue_technical_snapshot_write(symbol.upper().strip(), tf, close.index[-1].strftime("%Y-%m-%d"), {
                    "close": round(float(close.iloc[-1]), 4),
                    "rsi3": round(float(rsi3.iloc[-1]), 4) if pd.notna(rsi3.iloc[-1]) else None,
                    "rsi14": round(float(rsi14.iloc[-1]), 4) if pd.notna(rsi14.iloc[-1]) else None,
                    "ema_rsi14_13": round(float(ema_rsi13.iloc[-1]), 4) if pd.notna(ema_rsi13.iloc[-1]) else None,
                    "ema_rsi14_90": round(float(ema_rsi90.iloc[-1]), 4) if rsidiff90_ok_wt and pd.notna(ema_rsi90.iloc[-1]) else None,
                    "rsidiff90": round(float(rsi_diff_90.iloc[-1]), 4) if rsidiff90_ok_wt and pd.notna(rsi_diff_90.iloc[-1]) else None,
                    "rsidiff90_trusted": rsidiff90_ok_wt,
                    "ema9": round(float(ema9.iloc[-1]), 4), "ema13": round(float(ema13.iloc[-1]), 4), "ema20": round(float(ema20.iloc[-1]), 4),
                    "ema50": round(float(ema50.iloc[-1]), 4), "ema60": None,
                    "ema200": round(float(ema200.iloc[-1]), 4) if ema200 is not None and pd.notna(ema200.iloc[-1]) else None,
                    "bar_strength_vs_ema60": None,
                    "macd": round(float(macd_line.iloc[-1]), 4), "macd_signal": round(float(macd_signal.iloc[-1]), 4),
                    "macd_hist": round(float(macd_hist.iloc[-1]), 4),
                    "adx": None, "di_plus": None, "di_minus": None,
                    "sr_support": None, "sr_resistance": None,
                })
        except Exception:
            pass  # never let a cache write failure interrupt the scanner query engine

    return {
        "tf": tf,
        "index": close.index,
        "series": {
            "close": close,
            "open": open_,
            "high": high,
            "low": low,
            "volume": vol,
            "rsi3": rsi3,
            "rsi14": rsi14,
            "ema_rsi14_13": ema_rsi13,
            "ema_rsi14_90": ema_rsi90,
            "rsi_diff_90": rsi_diff_90,
            "ema5": ema5,
            "ema9": ema9,
            "ema13": ema13,
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "macd": macd_line,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "relative_strength": rs,
        },
    }


# ---------------------------------------------------------------------
# Full-series snapshot cache -- distinct from technical_snapshot.py's
# cache, which only stores TODAY's single scalar value per indicator.
# _prepare_snapshot() above recomputes the ENTIRE historical series (RSI,
# EMAs, MACD, etc. across the whole lookback window) from raw price data
# on every single call -- and every scanner query, for every symbol,
# calls this. Repeated scans against the same watchlist on the same day
# were redoing this full-series computation from scratch every time, even
# though nothing about the underlying data had changed since the last
# scan. This cache stores the ENTIRE computed series (not just the latest
# value), keyed by the data's own last-bar date, so a second scan the
# same day loads a cached result instead of recomputing everything.
# ---------------------------------------------------------------------
_snapshot_cache_table_ready = False

# In-process memory tier in front of the scanner_snapshot_cache SQLite
# table. The SQLite cache already avoids recomputing a symbol's full
# indicator series more than once per trading day -- this dict avoids
# even the SQLite round-trip (connection open + SELECT + JSON decode) for
# repeat lookups of the same symbol/timeframe within this process's
# lifetime (e.g. the same symbol appearing in several scanner
# definitions run back-to-back, or a column template re-evaluating a
# symbol already touched earlier in the same scan). Bounded size with
# simple FIFO eviction so it can't grow unbounded across a long-running
# process watching a large universe.
_snapshot_memory_cache: "OrderedDict[Tuple[str, str, str], Dict[str, Any]]" = OrderedDict()
_snapshot_memory_cache_lock = threading.Lock()
_SNAPSHOT_MEMORY_CACHE_MAX = 2000


def _snapshot_memory_get(symbol_u: str, tf: str, as_of_date: str) -> Optional[Dict[str, Any]]:
    key = (symbol_u, tf, as_of_date)
    with _snapshot_memory_cache_lock:
        snap = _snapshot_memory_cache.get(key)
        if snap is not None:
            _snapshot_memory_cache.move_to_end(key)
        return snap


def _snapshot_memory_put(symbol_u: str, tf: str, as_of_date: str, snap: Dict[str, Any]) -> None:
    key = (symbol_u, tf, as_of_date)
    with _snapshot_memory_cache_lock:
        _snapshot_memory_cache[key] = snap
        _snapshot_memory_cache.move_to_end(key)
        while len(_snapshot_memory_cache) > _SNAPSHOT_MEMORY_CACHE_MAX:
            _snapshot_memory_cache.popitem(last=False)


def _ensure_snapshot_cache_table(con) -> None:
    global _snapshot_cache_table_ready
    if _snapshot_cache_table_ready:
        return
    con.execute("""CREATE TABLE IF NOT EXISTS scanner_snapshot_cache (
        symbol TEXT NOT NULL, timeframe TEXT NOT NULL, date TEXT NOT NULL,
        payload TEXT NOT NULL, computed_at TEXT NOT NULL,
        PRIMARY KEY (symbol, timeframe, date))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_snapcache_symbol_tf ON scanner_snapshot_cache(symbol, timeframe)")
    _snapshot_cache_table_ready = True


def _serialize_snapshot(snap: Dict[str, Any]) -> str:
    """snap is a _prepare_snapshot()-shaped dict: {"tf": str, "index":
    DatetimeIndex, "series": {name: pd.Series, ...}} -- every series
    shares the same index. Converts to a compact JSON string."""
    index_iso = [pd.Timestamp(t).isoformat() for t in snap["index"]]
    series_out = {}
    for key, s in snap["series"].items():
        series_out[key] = [None if (v is None or (isinstance(v, float) and v != v)) else float(v) for v in s.tolist()]
    return json.dumps({"tf": snap["tf"], "index": index_iso, "series": series_out})


def _deserialize_snapshot(payload: str) -> Dict[str, Any]:
    """Inverse of _serialize_snapshot -- reconstructs the exact same
    shape _prepare_snapshot() returns, with real pandas Series rebuilt
    against the original DatetimeIndex."""
    data = json.loads(payload)
    idx = pd.DatetimeIndex([pd.Timestamp(t) for t in data["index"]])
    series = {}
    for key, vals in data["series"].items():
        series[key] = pd.Series(vals, index=idx, dtype="float64")
    return {"tf": data["tf"], "index": idx, "series": series}


import queue as _snap_queue_mod
# Keep cache work bounded. Cache entries are optional and must never build an
# unbounded backlog after a scan deadline or block new user work.
_snapshot_write_queue: "_snap_queue_mod.Queue" = _snap_queue_mod.Queue(maxsize=512)
_snapshot_write_worker_started = False
_snapshot_write_worker_lock = None


def _ensure_snapshot_write_worker() -> None:
    """Single background thread that performs the actual snapshot cache
    write (serialize + INSERT + prune) -- moved off the scanning thread
    entirely. Measured directly: serializing + writing an 11-year daily
    series costs ~70ms combined. Paying that synchronously on every cache
    MISS would make the cache a net slowdown for any usage pattern with a
    low repeat rate across symbols/timeframes (confirmed: this was adding
    ~750% overhead per miss before this fix) -- exactly the concern with
    varied queries across many different symbols and timeframes. The
    scanning thread now only pays for a cheap cache-check SELECT and an
    in-memory enqueue; the actual write happens later, off this request."""
    global _snapshot_write_worker_started, _snapshot_write_worker_lock
    import threading
    if _snapshot_write_worker_lock is None:
        _snapshot_write_worker_lock = threading.Lock()
    with _snapshot_write_worker_lock:
        if _snapshot_write_worker_started:
            return
        _snapshot_write_worker_started = True

    def _worker():
        while True:
            item = _snapshot_write_queue.get()
            try:
                symbol_u, tf, as_of_date, snap = item
                payload = _serialize_snapshot(snap)
                con = _conn()
                try:
                    _ensure_snapshot_cache_table(con)
                    con.execute(
                        "INSERT OR REPLACE INTO scanner_snapshot_cache (symbol, timeframe, date, payload, computed_at) VALUES (?,?,?,?,?)",
                        (symbol_u, tf, as_of_date, payload, datetime.now().isoformat(timespec="seconds")),
                    )
                    con.execute(
                        "DELETE FROM scanner_snapshot_cache WHERE symbol=? AND timeframe=? AND date<?",
                        (symbol_u, tf, as_of_date),
                    )
                    con.commit()
                finally:
                    con.close()
            except Exception as e:  # noqa: BLE001
                print(f"[scanner_builder] snapshot cache background write failed: {type(e).__name__}: {e}")
            finally:
                _snapshot_write_queue.task_done()

    t = threading.Thread(target=_worker, name="scanner-snapshot-cache-writer", daemon=True)
    t.start()


def _prepare_snapshot_cached(df: pd.DataFrame, bench_df: Optional[pd.DataFrame], tf: str,
                              symbol: Optional[str] = None) -> Dict[str, Any]:
    """Cache-aware wrapper around _prepare_snapshot(). If a cached
    snapshot already exists for this symbol+timeframe as of the data's
    own latest bar date, deserialize and return it directly -- skipping
    the full recomputation entirely. Otherwise compute fresh and enqueue
    the result for a background thread to write to cache (see
    _ensure_snapshot_write_worker for why this is async, not synchronous)
    -- keyed by the data's own last-bar date, so a new trading day
    naturally invalidates the previous day's cache without an explicit
    expiry check."""
    if not symbol or df is None or df.empty:
        return _prepare_snapshot(df, bench_df, tf, symbol=symbol)

    from ..services.profiling import increment as _prof_increment

    symbol_u = symbol.upper().strip()
    as_of_date = pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")

    mem_hit = _snapshot_memory_get(symbol_u, tf, as_of_date)
    if mem_hit is not None:
        _prof_increment("snapshot_memory_hits")
        return mem_hit

    # Genuinely read-only connection (SQLite URI mode=ro), not just a
    # normal connection used read-only by convention. This matters
    # concretely, not just in principle: the previous version called
    # _ensure_snapshot_cache_table(con) on every single cache check --
    # a CREATE TABLE/CREATE INDEX IF NOT EXISTS statement, run once per
    # symbol per timeframe, so thousands of times across a large scan.
    # Even as a no-op once the table exists, that's a schema-touching
    # statement on a writable connection, confirmed as a real
    # contributor to lock contention during a live production scan that
    # was running concurrently with a long tastytrade backfill (both
    # writing to the same file). A mode=ro connection makes it
    # STRUCTURALLY IMPOSSIBLE for this read path to ever acquire a write
    # lock, rather than just avoiding it by convention -- if the table
    # doesn't exist yet (a fresh DB with no writes ever committed), the
    # SELECT below fails cleanly and is treated as an ordinary cache
    # miss, same as any other miss.
    row = None
    try:
        ro_uri = f"file:{DB_PATH}?mode=ro"
        con = sqlite3.connect(ro_uri, uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT payload FROM scanner_snapshot_cache WHERE symbol=? AND timeframe=? AND date=?",
                (symbol_u, tf, as_of_date),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.OperationalError:
        row = None  # table/file doesn't exist yet, or another genuine open failure -- ordinary cache miss, not an error worth surfacing

    if row is not None:
        try:
            snap = _deserialize_snapshot(row[0])
            _snapshot_memory_put(symbol_u, tf, as_of_date, snap)
            _prof_increment("snapshot_sqlite_hits")
            return snap
        except Exception:
            pass  # corrupted/incompatible cache entry -- fall through to a fresh compute

    snap = _prepare_snapshot(df, bench_df, tf, symbol=symbol)
    _snapshot_memory_put(symbol_u, tf, as_of_date, snap)
    _prof_increment("snapshot_computed")

    try:
        _ensure_snapshot_write_worker()
        _snapshot_write_queue.put_nowait((symbol_u, tf, as_of_date, snap))
    except _snap_queue_mod.Full:
        # The cache is an optimization, not part of the scan result. Dropping
        # excess work is safer than letting timed-out scans keep writing later.
        pass
    except Exception:
        pass  # queueing failure should never break the actual scan result

    return snap


def _series_latest(series: pd.Series, shift: int = 0) -> Tuple[Optional[float], Optional[float]]:
    if series is None or len(series) <= shift:
        return None, None
    idx_now = len(series) - 1 - shift
    idx_prev = idx_now - 1
    now = series.iloc[idx_now]
    prev = series.iloc[idx_prev] if idx_prev >= 0 else series.iloc[idx_now]
    try:
        return float(prev), float(now)
    except Exception:
        return None, None


@lru_cache(maxsize=256)
def _options_history(symbol: str, limit: int = 60) -> Tuple[Dict[str, Any], ...]:
    try:
        con = _conn()
        try:
            rows = con.execute(
                """
                SELECT date,
                       SUM(CASE WHEN type LIKE 'P%' THEN oi ELSE 0 END) AS put_oi,
                       SUM(CASE WHEN type LIKE 'C%' THEN oi ELSE 0 END) AS call_oi,
                       SUM(COALESCE(oi,0)) AS total_oi
                FROM options
                WHERE symbol=?
                GROUP BY date
                ORDER BY date ASC
                """,
                (symbol.upper().strip(),),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return tuple()

    out: List[Dict[str, Any]] = []
    for r in rows[-max(5, int(limit)) :]:
        put_oi = float(r[1] or 0)
        call_oi = float(r[2] or 0)
        total_oi = float(r[3] or 0)
        pcr = (put_oi / call_oi) if call_oi else None
        out.append({
            "date": r[0],
            "put_oi": put_oi,
            "call_oi": call_oi,
            "total_oi": total_oi,
            "pcr": pcr,
        })
    for i, row in enumerate(out):
        prev = out[i - 1] if i > 0 else None
        if prev:
            row["oi_change"] = row["total_oi"] - prev["total_oi"]
            row["oi_change_pct"] = ((row["total_oi"] - prev["total_oi"]) / prev["total_oi"] * 100.0) if prev["total_oi"] else None
            if prev.get("pcr") is not None and row.get("pcr") is not None:
                row["pcr_change"] = row["pcr"] - prev["pcr"]
                row["pcr_change_pct"] = ((row["pcr"] - prev["pcr"]) / prev["pcr"] * 100.0) if prev["pcr"] else None
            else:
                row["pcr_change"] = None
                row["pcr_change_pct"] = None
        else:
            row["oi_change"] = None
            row["oi_change_pct"] = None
            row["pcr_change"] = None
            row["pcr_change_pct"] = None
    return tuple(out)


def _safe_number(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        if isinstance(val, bool):
            return float(int(val))
        v = float(val)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _annualized_vol_pct(close: pd.Series, window: int = 20) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    try:
        s = pd.to_numeric(close, errors="coerce").dropna()
        if len(s) < window + 2:
            return None, None, None
        rets: List[float] = []
        for i in range(1, len(s)):
            prev = _safe_number(s.iloc[i - 1])
            cur = _safe_number(s.iloc[i])
            if prev is None or cur is None or prev <= 0 or cur <= 0:
                continue
            rets.append(math.log(cur / prev))
        if len(rets) < window:
            return None, None, None
        ser = pd.Series(rets, dtype="float64")
        roll = ser.rolling(window=window).std(ddof=0) * math.sqrt(252) * 100.0
        current = _safe_number(roll.iloc[-1])
        prev = _safe_number(roll.iloc[-2]) if len(roll) > 1 else None
        hist = roll.dropna().tail(252)
        if current is None or hist.empty:
            return current, prev, None
        lo = float(hist.min())
        hi = float(hist.max())
        if hi <= lo:
            return current, prev, 50.0
        rank = (current - lo) / (hi - lo) * 100.0
        return current, prev, max(0.0, min(100.0, rank))
    except Exception:
        return None, None, None


def _flow_snapshot(ctx: Dict[str, Any], tf: str = "1d") -> Dict[str, Any]:
    tf = _normalize_tf(tf)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return {}

    series = snap.get("series", {})
    close = series.get("close")
    volume = series.get("volume")
    ema20 = series.get("ema20")
    ema50 = series.get("ema50")
    rsi14 = series.get("rsi14")
    rsi_diff_90 = series.get("rsi_diff_90")

    iv_est, iv_prev, iv_rank = _annualized_vol_pct(close, 20) if close is not None else (None, None, None)
    iv_change = None if iv_est is None or iv_prev is None else iv_est - iv_prev

    hist = _opt_history_list(ctx)
    latest = hist[-1] if hist else {}
    prev = hist[-2] if len(hist) > 1 else {}

    def _delta(key: str) -> Optional[float]:
        now = _safe_number(latest.get(key))
        old = _safe_number(prev.get(key))
        if now is None or old is None:
            return None
        return now - old

    call_delta = _delta("call_oi")
    put_delta = _delta("put_oi")
    pcr_shift = _safe_number(latest.get("pcr_change_pct"))
    oi_change_pct = _safe_number(latest.get("oi_change_pct"))

    cur_close = _safe_number(close.iloc[-1]) if close is not None and len(close) else None
    cur_ema20 = _safe_number(ema20.iloc[-1]) if ema20 is not None and len(ema20) else None
    cur_ema50 = _safe_number(ema50.iloc[-1]) if ema50 is not None and len(ema50) else None
    cur_rsi14 = _safe_number(rsi14.iloc[-1]) if rsi14 is not None and len(rsi14) else None
    cur_rsi_diff = _safe_number(rsi_diff_90.iloc[-1]) if rsi_diff_90 is not None and len(rsi_diff_90) else None

    vol_ratio = None
    if volume is not None and len(volume):
        vol_series = pd.to_numeric(volume, errors="coerce")
        if len(vol_series) >= 2:
            vol_ema20 = vol_series.ewm(span=20, adjust=False).mean().iloc[-1]
            cur_vol = _safe_number(vol_series.iloc[-1])
            vol_ema20 = _safe_number(vol_ema20)
            if cur_vol is not None and vol_ema20 not in (None, 0):
                vol_ratio = cur_vol / vol_ema20

    bull = 0
    bear = 0

    if cur_close is not None and cur_ema20 is not None and cur_ema50 is not None:
        if cur_close > cur_ema20 > cur_ema50:
            bull += 2
        elif cur_close < cur_ema20 < cur_ema50:
            bear += 2
        elif cur_close > cur_ema20:
            bull += 1
        elif cur_close < cur_ema20:
            bear += 1

    if cur_rsi_diff is not None:
        if cur_rsi_diff > 5:
            bull += 1
        elif cur_rsi_diff < -5:
            bear += 1

    if call_delta is not None and put_delta is not None:
        if call_delta > put_delta:
            bull += 1
        elif put_delta > call_delta:
            bear += 1

    if pcr_shift is not None:
        if pcr_shift < 0:
            bull += 1
        elif pcr_shift > 0:
            bear += 1

    if iv_change is not None:
        if iv_change < -1.5:
            bull += 1
        elif iv_change > 1.5:
            bear += 1

    score = 50 + bull * 10 - bear * 10
    if vol_ratio is not None:
        score += max(-5.0, min(10.0, (vol_ratio - 1.0) * 5.0))
    if iv_rank is not None:
        score += 5.0 if iv_rank <= 35 else 2.0 if iv_rank <= 60 else 0.0
    score = max(0, min(100, int(round(score))))

    if bull >= bear + 2 and score >= 55:
        flow_bias = "BULL"
    elif bear >= bull + 2 and score <= 45:
        flow_bias = "BEAR"
    else:
        flow_bias = "NEUTRAL"

    if flow_bias == "BULL":
        if call_delta is not None and put_delta is not None and call_delta > put_delta and (iv_change is None or iv_change <= 0):
            flow_class = "Real Accumulation"
        elif iv_change is not None and iv_change > 1.5 and (vol_ratio is None or vol_ratio < 1.2):
            flow_class = "Bull Trap Risk"
        else:
            flow_class = "Speculative Accumulation"
    elif flow_bias == "BEAR":
        if put_delta is not None and call_delta is not None and put_delta > call_delta and (iv_change is None or iv_change >= 0):
            flow_class = "Real Distribution"
        elif iv_change is not None and iv_change > 1.5 and (vol_ratio is None or vol_ratio < 1.2):
            flow_class = "Bear Trap Risk"
        else:
            flow_class = "Speculative Distribution"
    else:
        flow_class = "Neutral"

    return {
        "iv_est": iv_est,
        "iv_prev": iv_prev,
        "iv_rank": iv_rank,
        "iv_change": iv_change,
        "pcr_shift": pcr_shift,
        "oi_change_pct": oi_change_pct,
        "call_oi_change": call_delta,
        "put_oi_change": put_delta,
        "vol_ratio": vol_ratio,
        "bull_points": bull,
        "bear_points": bear,
        "flow_score": score,
        "flow_bias": flow_bias,
        "flow_classification": flow_class,
    }


def _build_scan_summary(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(results or [])
    bull = bear = neutral = 0
    iv_changes: List[float] = []
    iv_ranks: List[float] = []
    flow_scores: List[float] = []
    pcr_shifts: List[float] = []
    oi_shifts: List[float] = []

    for row in results or []:
        bias = str(row.get("flow_bias") or "NEUTRAL").upper()
        if bias == "BULL":
            bull += 1
        elif bias == "BEAR":
            bear += 1
        else:
            neutral += 1
        for key, bucket in (("iv_change", iv_changes), ("iv_rank", iv_ranks), ("flow_score", flow_scores), ("pcr_shift", pcr_shifts), ("oi_change_pct", oi_shifts)):
            val = _safe_number(row.get(key))
            if val is not None:
                bucket.append(val)

    def _avg(vals: List[float]) -> Optional[float]:
        return round(sum(vals) / len(vals), 2) if vals else None

    market_view = "Balanced"
    if bull > bear + 2:
        market_view = "Bullish"
    elif bear > bull + 2:
        market_view = "Bearish"

    return {
        "total": total,
        "bullish": bull,
        "bearish": bear,
        "neutral": neutral,
        "avg_flow_score": _avg(flow_scores),
        "avg_iv_rank": _avg(iv_ranks),
        "avg_iv_change": _avg(iv_changes),
        "avg_pcr_shift": _avg(pcr_shifts),
        "avg_oi_change_pct": _avg(oi_shifts),
        "market_view": market_view,
    }


def _json_safe(value: Any) -> Any:
    """Recursively make scanner output safe for Flask's JSON encoder.

    Evaluation contexts can contain Pandas objects when a primitive returns
    a sliced history series.  They are useful during evaluation but cannot
    be emitted directly in the API result or persisted as a saved scan.
    """
    if isinstance(value, pd.Series):
        return _json_safe(value.to_dict())
    if isinstance(value, pd.DataFrame):
        return _json_safe(value.to_dict(orient="records"))
    if isinstance(value, pd.Index):
        return [_json_safe(v) for v in value.tolist()]
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    # NumPy scalar types expose item(); this avoids importing NumPy only
    # for serialization while preserving normal Python values unchanged.
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        return _json_safe(value.item())
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, (str, int, float, bool)) or k is None:
                safe_k = k
            else:
                # e.g. a tuple key (as used internally by _eval()'s memo
                # cache) -- json.dumps cannot use it as a dict key at all,
                # so stringify rather than let the whole response fail.
                safe_k = str(k)
            out[safe_k] = _json_safe(v)
        return out
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _infer_result_column_format(expr: str, requested: Any = None) -> str:
    """Infer display format for primitive result columns.

    Older saved templates could store manually typed numeric primitives as
    format="text".  For string-returning primitives we keep text; otherwise
    text/auto is upgraded to the correct numeric/price/percent display format.
    """
    raw_fmt = str(requested or "auto").strip().lower() or "auto"
    expr_l = str(expr or "").strip().lower()
    if raw_fmt not in {"", "auto", "text"}:
        return raw_fmt
    if expr_l in {"symbol", "reason", "scanner_reason", "match_reason"}:
        return "text"
    if any(m in expr_l for m in ("uaeregime", "uae_regime", "flowbias", "flow_bias", "sector", "regime", "signal", "bias")):
        return "text"
    if any(m in expr_l for m in ("price", "close", "open", "high", "low", "ema", "support", "resistance", "fib")):
        return "price"
    if any(m in expr_l for m in ("pct", "percent", "leadership", "rank")):
        return "pct1"
    if any(m in expr_l for m in ("days", "count", "age")) or expr_l == "oi":
        return "integer"
    return "number"


def _format_column_label(expr: str) -> str:
    raw = str(expr or "").strip()
    if not raw:
        return "Column"
    aliases = {
        "symbol": "Symbol",
        "reason": "Reason",
        "close[1d]": "Price",
        "price": "Price",
        "leadership": "Leadership",
        "sector": "Sector",
        "sector_name": "Sector Name",
        "sector_etf": "Sector ETF",
        "sector_rs": "SectorRS",
        "rsi14[1d]": "RSI",
        "rsi_diff_90": "RSIDiff90",
    }
    if raw.lower() in aliases:
        return aliases[raw.lower()]
    s = re.sub(r"\s+", " ", raw)
    s = re.sub(r"\(\s*\)", "", s)
    s = s.replace("_", " ")
    return s[:40]


def _normalize_result_columns(columns: Any, fallback: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    if isinstance(columns, str):
        try:
            columns = json.loads(columns)
        except Exception:
            columns = []
    if not isinstance(columns, list):
        columns = []
    out: List[Dict[str, Any]] = []
    for item in columns:
        if isinstance(item, str):
            spec = {"expr": item, "label": _format_column_label(item)}
        elif isinstance(item, dict):
            spec = dict(item)
        else:
            continue
        expr = str(spec.get("expr") or spec.get("primitive") or spec.get("key") or "").strip()
        if not expr:
            continue
        label = str(spec.get("label") or _format_column_label(expr)).strip()[:48] or _format_column_label(expr)
        fmt = _infer_result_column_format(expr, spec.get("format"))
        out.append({
            "label": label,
            "expr": expr,
            "format": fmt,
            "locked": bool(spec.get("locked")),
        })
    if not out and fallback is not None:
        return _normalize_result_columns(fallback, fallback=None)
    return out


def _default_result_columns() -> List[Dict[str, Any]]:
    return _normalize_result_columns(DEFAULT_RESULT_COLUMNS, fallback=None)


def _column_primitives_payload() -> List[Dict[str, Any]]:
    base: List[Dict[str, Any]] = []
    for tmpl in DEFAULT_COLUMN_TEMPLATES:
        for col in tmpl.get("columns") or []:
            expr = str(col.get("expr") or "").strip()
            if not expr:
                continue
            base.append({
                "label": col.get("label") or _format_column_label(expr),
                "expr": expr,
                "format": col.get("format") or "auto",
                "category": tmpl.get("name") or "Template",
                "description": tmpl.get("description") or "Built-in result primitive",
            })
    for ind in INDICATORS:
        expr = f"{ind}[1d]"
        base.append({"label": _format_column_label(expr), "expr": expr, "format": "number", "category": "Indicator", "description": "Indicator primitive on daily timeframe"})
    examples = [
        ("ChangePct 1D", "ChangePct(close, 1, \"1d\")", "pct1", "Price"),
        ("ChangePct 10D", "ChangePct(close, 10, \"1d\")", "pct1", "Price"),
        ("SlopeDeg 20D", "SlopeDeg(close, 20, \"1d\")", "number", "Price"),
        ("SlopePct 20D", "SlopePct(close, 20, \"1d\")", "pct2", "Price"),
        ("SlopeDegPerBar 20D", "SlopeDegPerBar(close, 20, \"1d\")", "number", "Price"),
        ("RegSlopeDeg 20D", "RegSlopeDeg(close, 20, \"1d\")", "number", "Price"),
        ("RSIDiff90 Round", "Round(RSIDiff90(90, \"1d\"), 2)", "number", "Momentum"),
        ("RSIDiff90 Int", "Round(RSIDiff90(90, \"1d\"), 0)", "integer", "Momentum"),
        ("Sector", "Sector()", "text", "Relative Strength"),
        ("SectorRS 20D", "SectorRS(20, \"1d\")", "number", "Relative Strength"),
        ("RS 20D", "RelativeStrength(20, \"1d\")", "number", "Relative Strength"),
        ("Mansfield RS", "MansfieldRS(52, \"1w\")", "number", "Relative Strength"),
        ("Resistance Strength", "ResistanceStrength(20, \"1w\")", "number", "Price Action"),
        ("Support Strength", "SupportStrength(20, \"1w\")", "number", "Price Action"),
        ("Dist Resistance", "DistanceFromResistance(20, \"1w\")", "pct1", "Price Action"),
        ("Dist Support", "DistanceFromSupport(20, \"1w\")", "pct1", "Price Action"),
        ("Last Swing High Close", "LastSwingHighClose(80, 2, 2, \"1d\")", "price", "Price Action"),
        ("Last Swing Low Close", "LastSwingLowClose(80, 2, 2, \"1d\")", "price", "Price Action"),
        ("Swing High Age", "DaysSinceSwingHigh(80, 2, 2, \"1d\")", "integer", "Price Action"),
        ("Swing Low Age", "DaysSinceSwingLow(80, 2, 2, \"1d\")", "integer", "Price Action"),
        ("Pullback ATR", "PullbackFromSwingHighATR(80, 2, 2, \"1d\")", "number", "Price Action"),
        ("Bounce ATR", "BounceFromSwingLowATR(80, 2, 2, \"1d\")", "number", "Price Action"),
        ("ATR Compression", "ATRCompression(14, \"1d\")", "number", "Volatility"),
        ("Volume Dry-up", "VolumeDryup(20, \"1d\")", "number", "Volume"),
        ("UAE 4H", "UAERegime(\"4h\")", "text", "UAE"),
        ("UAE 1D", "UAERegime(\"1d\")", "text", "UAE"),
        ("UAE 1W", "UAERegime(\"1w\")", "text", "UAE"),
        ("UAE Score", "UAERegimeScore(\"1d\")", "number", "UAE"),
        ("UAE RSIDiff", "UAERSIDiff(\"1d\")", "number", "UAE"),
        ("UAE MACD", "UAEMACD(\"1d\")", "number", "UAE"),
        ("UAE Hist", "UAEHist(\"1d\")", "number", "UAE"),
        ("UAE Bull Triangle", "UAETrendTriangle(\"bull\", \"1d\")", "text", "UAE"),
        ("UAE Bear Triangle", "UAETrendTriangle(\"bear\", \"1d\")", "text", "UAE"),
        ("UAE MRT Buy Arrow", "UAEFadeArrow(\"bull\", \"1d\")", "text", "UAE"),
        ("UAE MRT Sell Arrow", "UAEFadeArrow(\"bear\", \"1d\")", "text", "UAE"),
        ("UAE Bull Diamond", "UAEDiamond(\"bull\", \"1d\")", "text", "UAE"),
        ("UAE Bear Diamond", "UAEDiamond(\"bear\", \"1d\")", "text", "UAE"),
        ("UAE Weekly Bull Triangle Age", "UAETrendTriangleAge(\"bull\", \"1w\", 8)", "integer", "UAE"),
        ("UAE Weekly Bear Triangle Age", "UAETrendTriangleAge(\"bear\", \"1w\", 8)", "integer", "UAE"),
        ("UAE Weekly Bull Triangle Date", "UAETrendTriangleDate(\"bull\", \"1w\", 8)", "text", "UAE"),
        ("UAE Weekly Bear Triangle Date", "UAETrendTriangleDate(\"bear\", \"1w\", 8)", "text", "UAE"),
        ("UAE Weekly Last Marker", "UAELastMarker(\"1w\")", "text", "UAE"),
        ("MACD Spread", "MACDSpread(\"1d\")", "number", "Momentum"),
        ("OI Change 5", "OIChangePct(5)", "pct1", "Options"),
        ("PCR Shift 5", "PCRShift(5)", "number", "Options"),
        ("Flow Bias", "FlowBias()", "text", "Options"),
        ("IV Rank", "IVRank()", "pct0", "Options"),
        ("Earnings Days", "EarningsDays()", "integer", "Earnings"),
        ("Beta", "Beta()", "number", "Risk"),
    ]
    for label, expr, fmt, cat in examples:
        base.append({"label": label, "expr": expr, "format": fmt, "category": cat, "description": "Runnable primitive example"})
    seen = set()
    out = []
    for item in base:
        key = str(item.get("expr") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _column_template_rows() -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute(
            """
            SELECT id, name, description, columns_json, is_default, created_at, updated_at
            FROM scanner_column_templates
            ORDER BY is_default DESC, lower(name)
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["columns"] = _normalize_result_columns(d.get("columns_json"), fallback=DEFAULT_RESULT_COLUMNS)
            out.append(d)
        return out
    finally:
        con.close()


def _columns_from_template_id(template_id: Any) -> List[Dict[str, Any]]:
    if template_id in (None, "", 0, "0"):
        return []
    try:
        tid = int(template_id)
    except Exception:
        return []
    con = _conn()
    try:
        row = con.execute("SELECT columns_json FROM scanner_column_templates WHERE id=?", (tid,)).fetchone()
        if not row:
            return []
        return _normalize_result_columns(row[0], fallback=DEFAULT_RESULT_COLUMNS)
    finally:
        con.close()


def _columns_from_definition(definition_id: Any) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    if definition_id in (None, "", 0, "0"):
        return [], None
    try:
        did = int(definition_id)
    except Exception:
        return [], None
    con = _conn()
    try:
        row = con.execute(
            "SELECT result_columns_json, result_template_id FROM scanner_definitions WHERE id=?",
            (did,),
        ).fetchone()
        if not row:
            return [], None
        cols = _normalize_result_columns(row[0], fallback=None)
        template_id = row[1]
        if not cols and template_id:
            cols = _columns_from_template_id(template_id)
        return cols, int(template_id) if template_id else None
    finally:
        con.close()


def _parse_column_nodes(columns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for col in _normalize_result_columns(columns, fallback=None):
        expr = str(col.get("expr") or "").strip()
        expr_l = expr.lower()
        node: Optional[Node] = None
        err: Optional[str] = None
        if expr_l in {"symbol", "reason", "scanner_reason", "match_reason"}:
            parsed.append({**col, "node": None, "direct": expr_l})
            continue
        if expr_l in {"price", "spot"}:
            expr = "close[1d]"
            col["expr"] = expr
        try:
            node = _parse_query(expr)
        except Exception as e:
            err = str(e)
        parsed.append({**col, "node": node, "error": err, "direct": None})
    return parsed


def _result_column_required_tfs(parsed_columns: List[Dict[str, Any]]) -> List[str]:
    tfs = set()
    for col in parsed_columns or []:
        node = col.get("node")
        if node is None:
            continue
        try:
            for tf in _required_timeframes(node):
                tfs.add(tf)
        except Exception:
            pass
    return [tf for tf in TIMEFRAMES if tf in tfs]


def _collect_rsrank_periods_for_nodes(nodes: List[Node]) -> set[int]:
    periods: set[int] = set()
    for n in nodes:
        try:
            periods.update(_collect_function_periods(n, {"rsrank", "rs_rank"}))
        except Exception:
            pass
    return periods


def _eval_result_columns(ctx: Dict[str, Any], parsed_columns: List[Dict[str, Any]]) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for col in parsed_columns or []:
        label = str(col.get("label") or col.get("expr") or "Column")
        direct = col.get("direct")
        if direct == "symbol":
            values[label] = ctx.get("symbol")
            continue
        if direct in {"reason", "scanner_reason", "match_reason"}:
            values[label] = " | ".join(ctx.get("reason") or [])
            continue
        node = col.get("node")
        if node is None:
            values[label] = None
            if col.get("error"):
                errors[label] = str(col.get("error"))
            continue
        try:
            values[label] = _eval(node, ctx, shift=0, tf_default="1d")
        except Exception as e:
            values[label] = None
            errors[label] = str(e)
    if errors:
        values["_errors"] = errors
    return _json_safe(values)


def _opt_history_list(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(ctx.get("options_history") or [])


def _opt_hist_at(ctx: Dict[str, Any], key: str, shift: int = 0) -> Optional[float]:
    hist = _opt_history_list(ctx)
    if not hist:
        return None
    idx = len(hist) - 1 - shift
    if idx < 0 or idx >= len(hist):
        return None
    val = hist[idx].get(key)
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        return None


def _price_hist_len(ctx: Dict[str, Any], tf: str) -> int:
    snap = ctx.get("timeframes", {}).get(_normalize_tf(tf))
    if not snap:
        return 0
    series = snap.get("series", {}).get("close")
    return len(series) if series is not None else 0


def _series_at(ctx: Dict[str, Any], name: str, shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    series = snap.get("series", {}).get(_normalize_indicator(name))
    if series is None:
        return None
    idx = len(series) - 1 - shift
    if idx < 0 or idx >= len(series):
        return None
    try:
        return float(series.iloc[idx])
    except Exception:
        return None




def _macd_diff_at(ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    """Return MACD line minus signal line for the selected timeframe."""
    hist = _series_at(ctx, "macd_hist", shift=shift, tf_default=tf_default)
    if hist is not None:
        return hist
    macd_val = _series_at(ctx, "macd", shift=shift, tf_default=tf_default)
    signal_val = _series_at(ctx, "macd_signal", shift=shift, tf_default=tf_default)
    if macd_val is None or signal_val is None:
        return None
    try:
        out = float(macd_val) - float(signal_val)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _macd_diff_series(ctx: Dict[str, Any], tf_default: str = "1d") -> Optional[pd.Series]:
    """Return the full MACD-signal series.  Uses macd_hist when present."""
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    series = snap.get("series", {})
    hist = series.get("macd_hist")
    if hist is not None:
        return pd.to_numeric(hist, errors="coerce")
    macd_s = series.get("macd")
    sig_s = series.get("macd_signal")
    if macd_s is None or sig_s is None:
        return None
    try:
        return pd.to_numeric(macd_s, errors="coerce") - pd.to_numeric(sig_s, errors="coerce")
    except Exception:
        return None


def _macd_diff_pct_at(ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    """Return MACD-signal gap normalized by close price as a percent."""
    diff = _macd_diff_at(ctx, shift=shift, tf_default=tf_default)
    close = _series_at(ctx, "close", shift=shift, tf_default=tf_default)
    if diff is None or close in (None, 0):
        return None
    try:
        return (float(diff) / abs(float(close))) * 100.0
    except Exception:
        return None


def _macd_diff_ratio(ctx: Dict[str, Any], lookback: int = 20, shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    """Abs current MACD spread divided by average abs spread over lookback."""
    lookback = max(2, int(lookback or 20))
    spread = _macd_diff_series(ctx, tf_default=tf_default)
    if spread is None or spread.empty:
        return None
    idx = len(spread) - 1 - shift
    if idx < 0 or idx >= len(spread):
        return None
    window = spread.abs().iloc[max(0, idx - lookback + 1): idx + 1].dropna()
    if window.empty:
        return None
    avg = float(window.mean())
    cur = spread.iloc[idx]
    if pd.isna(cur) or not math.isfinite(avg) or avg <= 1e-12:
        return None
    try:
        return abs(float(cur)) / avg
    except Exception:
        return None


def _macd_diff_shrink_pct(ctx: Dict[str, Any], bars: int = 1, shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    """Percent shrink in absolute MACD-signal gap versus N bars ago.

    Positive means the histogram/gap got smaller.  Negative means it expanded.
    """
    bars = max(1, int(bars or 1))
    now = _macd_diff_at(ctx, shift=shift, tf_default=tf_default)
    prev = _macd_diff_at(ctx, shift=shift + bars, tf_default=tf_default)
    if now is None or prev is None:
        return None
    prev_abs = abs(float(prev))
    now_abs = abs(float(now))
    if not math.isfinite(prev_abs) or not math.isfinite(now_abs):
        return None
    if prev_abs <= 1e-12:
        return 0.0 if now_abs <= 1e-12 else -100.0
    return ((prev_abs - now_abs) / prev_abs) * 100.0


def _macd_gap_stable(ctx: Dict[str, Any], side: Optional[str] = None, max_shrink_pct: float = 25.0, bars: int = 1, shift: int = 0, tf_default: str = "1d") -> bool:
    """True when MACD spread is on the requested side and not shrinking much."""
    diff = _macd_diff_at(ctx, shift=shift, tf_default=tf_default)
    shrink = _macd_diff_shrink_pct(ctx, bars=bars, shift=shift, tf_default=tf_default)
    if diff is None or shrink is None:
        return False
    side_norm = _side_arg(side) if side is not None else "any"
    if side_norm == "bull" and float(diff) <= 0:
        return False
    if side_norm == "bear" and float(diff) >= 0:
        return False
    return float(shrink) <= abs(float(max_shrink_pct))

def _level_at(node_or_value: Node, ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d", stack: Tuple[str, ...] = ()) -> Optional[float]:
    val = _eval(node_or_value, ctx, shift=shift, tf_default=tf_default, stack=stack)
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        return None


def _touch_count(ctx: Dict[str, Any], level: float, tolerance: float, bars: int, shift: int = 0, tf_default: str = "1d") -> Optional[int]:
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    high = snap.get("series", {}).get("high")
    low = snap.get("series", {}).get("low")
    if high is None or low is None:
        return None
    tolerance = abs(float(tolerance or 0.0))
    bars = max(1, int(bars or 1))
    count = 0
    for i in range(min(bars, len(high) - shift)):
        idx = len(high) - 1 - shift - i
        if idx < 0:
            break
        hi = float(high.iloc[idx])
        lo = float(low.iloc[idx])
        band = abs(level) * tolerance if level else tolerance
        if lo <= level + band and hi >= level - band:
            count += 1
    return count


def _breakout_strength(ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    close = _series_at(ctx, "close", shift=shift, tf_default=tf_default)
    open_ = _series_at(ctx, "open", shift=shift, tf_default=tf_default)
    high = _series_at(ctx, "high", shift=shift, tf_default=tf_default)
    low = _series_at(ctx, "low", shift=shift, tf_default=tf_default)
    if None in {close, open_, high, low}:
        return None
    rng = abs(high - low)
    if rng <= 0:
        return None
    return abs(close - open_) / rng


def _breakout_age(ctx: Dict[str, Any], level: float, direction: str, max_bars: int, shift: int = 0, tf_default: str = "1d") -> Optional[int]:
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    close = snap.get("series", {}).get("close")
    if close is None:
        return None
    max_bars = max(1, int(max_bars or 1))
    direction = (direction or "both").strip().lower()
    if direction not in {"above", "below", "both"}:
        direction = "both"
    current = float(close.iloc[len(close) - 1 - shift])
    if direction == "above" and current <= level:
        return None
    if direction == "below" and current >= level:
        return None
    if direction == "both":
        if current > level:
            direction = "above"
        elif current < level:
            direction = "below"
        else:
            return None
    for age in range(max_bars + 1):
        idx = len(close) - 1 - shift - age
        if idx <= 0:
            break
        now = float(close.iloc[idx])
        prev = float(close.iloc[idx - 1])
        if direction == "above" and prev <= level < now:
            return age
        if direction == "below" and prev >= level > now:
            return age
    return None


def _distance_from_level(ctx: Dict[str, Any], level: float, shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    close = _series_at(ctx, "close", shift=shift, tf_default=tf_default)
    if close is None or level in (None, 0):
        return None
    try:
        return abs((close - level) / abs(level)) * 100.0
    except Exception:
        return None

def _string_arg(node: Node, ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d", stack: Tuple[str, ...] = (), default: str = "") -> str:
    try:
        val = _eval(node, ctx, shift=shift, tf_default=tf_default, stack=stack)
    except Exception:
        val = None
    if val is None:
        return default
    return str(val).strip()


def _side_arg(value: str) -> str:
    s = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if s in {"bull", "bullish", "long", "call", "up", "green", "b"}:
        return "bull"
    if s in {"bear", "bearish", "short", "put", "down", "red", "s"}:
        return "bear"
    return s or "bull"



def _second_leg_side_arg(value: str) -> str:
    """Normalize M/W second-leg side.

    up/bear/top => M-style second leg up into a prior high.
    down/bull/bottom => W-style second leg down into a prior low.
    """
    s = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if s in {"up", "m", "top", "double top", "second leg up", "secondlegup", "bear", "bearish", "short", "put", "call credit"}:
        return "up"
    if s in {"down", "w", "bottom", "double bottom", "second leg down", "secondlegdown", "bull", "bullish", "long", "call", "put credit"}:
        return "down"
    return "up" if s not in {"down"} else "down"


def _pivot_indices(series: pd.Series, start: int, end: int, kind: str = "high", span: int = 2) -> List[int]:
    try:
        vals = pd.to_numeric(series, errors="coerce")
    except Exception:
        return []
    n = len(vals)
    if n <= 0:
        return []
    start = max(int(start or 0), int(span))
    end = min(int(end), n - 1 - int(span))
    if end < start:
        return []
    out: List[int] = []
    span = max(1, int(span or 1))
    for idx in range(start, end + 1):
        cur = _safe_number(vals.iloc[idx])
        if cur is None:
            continue
        window = pd.to_numeric(vals.iloc[idx - span: idx + span + 1], errors="coerce").dropna()
        if len(window) < span + 1:
            continue
        if kind == "high":
            extreme = float(window.max())
            if cur >= extreme and (not out or idx - out[-1] > span):
                out.append(idx)
        else:
            extreme = float(window.min())
            if cur <= extreme and (not out or idx - out[-1] > span):
                out.append(idx)
    return out




def _swing_args(args: List[Node], ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d", stack: Tuple[str, ...] = ()) -> Tuple[int, int, int, str]:
    """Parse common swing primitive args.

    Supported forms:
      LastSwingHighClose(80)
      LastSwingHighClose(80, 2, 2)
      LastSwingHighClose(80, "1d")
      LastSwingHighClose(80, 2, 2, "1d")
    """
    args2, tf = _split_timeframe_args(list(args), tf_default)
    lookback = 60
    left = 2
    right = 2
    if len(args2) >= 1:
        try:
            lookback = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or lookback))
        except Exception:
            lookback = 60
    if len(args2) >= 2:
        try:
            left = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or left))
        except Exception:
            left = 2
    if len(args2) >= 3:
        try:
            right = int(float(_eval(args2[2], ctx, shift=shift, tf_default=tf, stack=stack) or right))
        except Exception:
            right = 2
    return max(3, lookback), max(1, left), max(1, right), _normalize_tf(tf)


def _series_date_label(series: pd.Series, idx: int) -> Optional[str]:
    try:
        label = series.index[idx]
    except Exception:
        return None
    try:
        if hasattr(label, "to_pydatetime"):
            return label.to_pydatetime().isoformat(sep=" ", timespec="seconds")
        if hasattr(label, "isoformat"):
            return label.isoformat()
        return str(label)
    except Exception:
        return str(label)


def _last_swing_pivot(
    ctx: Dict[str, Any],
    side: str = "high",
    lookback: int = 60,
    left: int = 2,
    right: int = 2,
    shift: int = 0,
    tf_default: str = "1d",
) -> Optional[Dict[str, Any]]:
    """Most recent confirmed swing high/low without leaking future bars.

    A swing high at bar i requires high[i] to be the highest value across
    left bars before i and right bars after i. A swing low uses the lowest low.
    For shift/lookback simulations, candidate pivots stop at idx_now - right so
    bars beyond the evaluated point are never used.
    """
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    series = snap.get("series", {})
    high_s = pd.to_numeric(series.get("high"), errors="coerce") if series.get("high") is not None else None
    low_s = pd.to_numeric(series.get("low"), errors="coerce") if series.get("low") is not None else None
    open_s = pd.to_numeric(series.get("open"), errors="coerce") if series.get("open") is not None else None
    close_s = pd.to_numeric(series.get("close"), errors="coerce") if series.get("close") is not None else None
    if high_s is None or low_s is None or open_s is None or close_s is None:
        return None
    n = min(len(high_s), len(low_s), len(open_s), len(close_s))
    if n <= left + right + 2:
        return None
    idx_now = n - 1 - max(0, int(shift or 0))
    if idx_now < left + right:
        return None
    lookback = max(left + right + 1, int(lookback or 60))
    left = max(1, int(left or 2))
    right = max(1, int(right or 2))
    start = max(left, idx_now - lookback + 1)
    end = min(idx_now - right, n - 1 - right)
    if end < start:
        return None
    side = "low" if str(side).lower().startswith("l") else "high"
    source = high_s if side == "high" else low_s
    # Scan from newest to oldest so the first match is the most recent pivot.
    for idx in range(end, start - 1, -1):
        cur = _safe_number(source.iloc[idx])
        if cur is None:
            continue
        left_window = pd.to_numeric(source.iloc[idx - left: idx], errors="coerce").dropna()
        right_window = pd.to_numeric(source.iloc[idx + 1: idx + 1 + right], errors="coerce").dropna()
        if len(left_window) < left or len(right_window) < right:
            continue
        if side == "high":
            if cur < float(left_window.max()) or cur < float(right_window.max()):
                continue
            pivot_price = cur
        else:
            if cur > float(left_window.min()) or cur > float(right_window.min()):
                continue
            pivot_price = cur
        h = _safe_number(high_s.iloc[idx])
        l = _safe_number(low_s.iloc[idx])
        o = _safe_number(open_s.iloc[idx])
        c = _safe_number(close_s.iloc[idx])
        if h is None or l is None or o is None or c is None:
            continue
        out: Dict[str, Any] = {
            "side": side,
            "idx": idx,
            "age": idx_now - idx,
            "price": pivot_price,
            "high": h,
            "low": l,
            "open": o,
            "close": c,
            "body_high": max(o, c),
            "body_low": min(o, c),
            "date": _series_date_label(close_s, idx),
        }
        if side == "high":
            lows_after = pd.to_numeric(low_s.iloc[idx + 1: idx_now + 1], errors="coerce").dropna()
            if not lows_after.empty and pivot_price:
                lo_after = float(lows_after.min())
                out["pullback_pct"] = max(0.0, (pivot_price - lo_after) / abs(pivot_price) * 100.0)
                out["pullback_price"] = max(0.0, pivot_price - lo_after)
            else:
                out["pullback_pct"] = 0.0
                out["pullback_price"] = 0.0
        else:
            highs_after = pd.to_numeric(high_s.iloc[idx + 1: idx_now + 1], errors="coerce").dropna()
            if not highs_after.empty and pivot_price:
                hi_after = float(highs_after.max())
                out["bounce_pct"] = max(0.0, (hi_after - pivot_price) / abs(pivot_price) * 100.0)
                out["bounce_price"] = max(0.0, hi_after - pivot_price)
            else:
                out["bounce_pct"] = 0.0
                out["bounce_price"] = 0.0
        try:
            atr = _atr_series(high_s.iloc[: idx_now + 1], low_s.iloc[: idx_now + 1], close_s.iloc[: idx_now + 1], 14)
            atr_val = _safe_number(atr.iloc[-1])
        except Exception:
            atr_val = None
        if atr_val and atr_val > 1e-12:
            if side == "high":
                out["pullback_atr"] = float(out.get("pullback_price") or 0.0) / atr_val
            else:
                out["bounce_atr"] = float(out.get("bounce_price") or 0.0) / atr_val
        return out
    return None


def _swing_value(pivot: Optional[Dict[str, Any]], which: str) -> Any:
    if not pivot:
        return None
    key = str(which or "price").strip().lower().replace("_", "").replace(" ", "")
    aliases = {
        "price": "price", "value": "price", "level": "price",
        "high": "high", "low": "low", "open": "open", "close": "close",
        "bodyhigh": "body_high", "bodylow": "body_low",
        "age": "age", "days": "age", "barssince": "age", "dayssince": "age",
        "date": "date", "time": "date", "datetime": "date",
        "pullbackpct": "pullback_pct", "pullbackpercent": "pullback_pct",
        "pullbackatr": "pullback_atr", "bouncepct": "bounce_pct", "bouncepercent": "bounce_pct", "bounceatr": "bounce_atr",
    }
    val = pivot.get(aliases.get(key, key))
    if isinstance(val, str):
        return val
    return _safe_number(val)

def _swing_sequence_check(
    ctx: Dict[str, Any], side: str, count: int, min_separation_pct: float,
    require_price_intact: bool, lookback: int, left: int, right: int,
    shift: int, tf_default: str,
) -> Optional[bool]:
    """Backing implementation for HigherLowConfirmed / LowerHighConfirmed.

    Fixes three separate failure modes that a hand-composed
    Shift()-based query kept hitting in practice:
      1. Lookback-mismatch bugs (comparing LastSwingLow(N) against a
         differently-windowed shifted call) -- impossible here, the
         window is applied once, consistently, internally.
      2. Noise from comparing only two adjacent pivots -- a single
         two-bar bounce mid-decline can register as "higher," even
         though the broader structure is still falling. This checks
         `count` consecutive confirmed pivots (default 3) and requires
         ALL of them to be monotonically ascending/descending, each by
         at least min_separation_pct, not just the most recent two.
      3. Confirmation lag -- a pivot only registers `right` bars after
         it forms, so if price just broke down hard in the last 1-2
         bars, the "most recent confirmed low" this sees is stale, from
         before that breakdown. require_price_intact (on by default)
         checks current price directly against the most recent
         confirmed pivot's level and fails the check if price has
         already broken past it, even though no NEW pivot has had time
         to confirm yet.

    Returns True/False, or None if there isn't enough pivot history to
    evaluate at all (caller should treat None like any other missing
    value -- no match, not an error).
    """
    count = max(2, int(count or 3))
    pivots: List[Dict[str, Any]] = []
    cur_shift = shift
    for _ in range(count):
        p = _last_swing_pivot(ctx, side=side, lookback=lookback, left=left, right=right, shift=cur_shift, tf_default=tf_default)
        if p is None:
            return None  # not enough confirmed pivot history to judge this yet
        pivots.append(p)
        # Jump the next search to just before this pivot's own bar so it
        # can never be re-found -- one bar back is enough regardless of
        # `right`, since _last_swing_pivot's own end=idx_now-right buffer
        # already keeps it from touching anything at/after idx_now.
        cur_shift = shift + int(p["age"]) + 1

    for i in range(len(pivots) - 1):
        newer, older = pivots[i], pivots[i + 1]
        newer_price, older_price = newer.get("price"), older.get("price")
        if newer_price is None or older_price is None or not older_price:
            return None
        if side == "low":
            if not (newer_price > older_price):
                return False
            sep_pct = (newer_price - older_price) / abs(older_price) * 100.0
        else:
            if not (newer_price < older_price):
                return False
            sep_pct = (older_price - newer_price) / abs(older_price) * 100.0
        if min_separation_pct > 0 and sep_pct < min_separation_pct:
            return False

    if require_price_intact:
        snap = ctx.get("timeframes", {}).get(tf_default)
        series = (snap or {}).get("series", {})
        close_s = series.get("close") if series else None
        if close_s is not None:
            idx_now = len(close_s) - 1 - shift
            if 0 <= idx_now < len(close_s):
                cur_price = _safe_number(close_s.iloc[idx_now])
                most_recent = pivots[0].get("price")
                if cur_price is not None and most_recent is not None:
                    if side == "low" and cur_price < most_recent:
                        return False
                    if side == "high" and cur_price > most_recent:
                        return False
    return True

def _choch_bos_close(ctx: Dict[str, Any], tf_default: str, shift: int) -> Optional[float]:
    snap = ctx.get("timeframes", {}).get(tf_default)
    close_s = (snap or {}).get("series", {}).get("close")
    if close_s is None:
        return None
    idx_now = len(close_s) - 1 - shift
    if not (0 <= idx_now < len(close_s)):
        return None
    return _safe_number(close_s.iloc[idx_now])


def _detect_choch(ctx: Dict[str, Any], direction: str, lookback: int, left: int, right: int, shift: int, tf_default: str) -> Optional[Dict[str, Any]]:
    """Change of Character: the two most recent CONFIRMED swing points
    on the relevant side show the OLD trend's pattern (descending
    highs for a downtrend, ascending lows for an uptrend), and current
    price has now closed past the more recent one -- the first
    violation of that pattern, which is what CHoCH actually means
    (not yet a confirmed new trend, just the first crack in the old
    one)."""
    side = "high" if direction == "bullish" else "low"
    p_recent = _last_swing_pivot(ctx, side=side, lookback=lookback, left=left, right=right, shift=shift, tf_default=tf_default)
    if p_recent is None:
        return None
    p_prior = _last_swing_pivot(ctx, side=side, lookback=lookback, left=left, right=right,
                                 shift=shift + int(p_recent["age"]) + 1, tf_default=tf_default)
    if p_prior is None:
        return None
    cur_price = _choch_bos_close(ctx, tf_default, shift)
    if cur_price is None:
        return None
    if direction == "bullish":
        old_structure = p_recent["price"] < p_prior["price"]  # descending highs = downtrend structure
        choch = old_structure and cur_price > p_recent["price"]
    else:
        old_structure = p_recent["price"] > p_prior["price"]  # ascending lows = uptrend structure
        choch = old_structure and cur_price < p_recent["price"]
    return {"choch": bool(choch), "level": p_recent["price"]}


def _detect_bos(ctx: Dict[str, Any], direction: str, lookback: int, left: int, right: int, shift: int, tf_default: str) -> Optional[Dict[str, Any]]:
    """Break of Structure: confirms the NEW direction once the
    confirmation side (lows for a bullish move, highs for a bearish
    one) starts agreeing too -- the exact same "two consecutive
    pivots ascending/descending" check as HigherLowConfirmed /
    LowerHighConfirmed -- combined with a fresh break of the most
    recent same-direction swing extreme, so this means "the pullback
    held AND price is pushing to a new extreme," not just one or the
    other."""
    confirm_side = "low" if direction == "bullish" else "high"
    break_side = "high" if direction == "bullish" else "low"

    p_recent = _last_swing_pivot(ctx, side=confirm_side, lookback=lookback, left=left, right=right, shift=shift, tf_default=tf_default)
    if p_recent is None:
        return None
    p_prior = _last_swing_pivot(ctx, side=confirm_side, lookback=lookback, left=left, right=right,
                                 shift=shift + int(p_recent["age"]) + 1, tf_default=tf_default)
    if p_prior is None:
        return None
    structure_confirmed = (p_recent["price"] > p_prior["price"]) if direction == "bullish" else (p_recent["price"] < p_prior["price"])

    break_pivot = _last_swing_pivot(ctx, side=break_side, lookback=lookback, left=left, right=right, shift=shift, tf_default=tf_default)
    if break_pivot is None:
        return None
    cur_price = _choch_bos_close(ctx, tf_default, shift)
    if cur_price is None:
        return None
    bos = structure_confirmed and ((cur_price > break_pivot["price"]) if direction == "bullish" else (cur_price < break_pivot["price"]))
    return {"bos": bool(bos), "level": break_pivot["price"], "pullback_level": p_recent["price"]}


def _retracement_pct(ctx: Dict[str, Any], direction: str, lookback: int, left: int, right: int, shift: int, tf_default: str) -> Optional[float]:
    """How far current price has pulled back into the most recent
    impulsive leg (swing low->high for bullish, high->low for
    bearish), as a %: 0 = at the extreme just made, 100 = fully back
    to the leg's origin. Checking this against a band (commonly
    50-61.8%) is the standard, least-ambiguous way to define "price
    is back in the demand/supply zone" without committing to one
    specific order-block definition."""
    if direction == "bullish":
        extreme = _last_swing_pivot(ctx, side="high", lookback=lookback, left=left, right=right, shift=shift, tf_default=tf_default)
        if extreme is None:
            return None
        origin = _last_swing_pivot(ctx, side="low", lookback=lookback, left=left, right=right,
                                    shift=shift + int(extreme["age"]) + 1, tf_default=tf_default)
    else:
        extreme = _last_swing_pivot(ctx, side="low", lookback=lookback, left=left, right=right, shift=shift, tf_default=tf_default)
        if extreme is None:
            return None
        origin = _last_swing_pivot(ctx, side="high", lookback=lookback, left=left, right=right,
                                    shift=shift + int(extreme["age"]) + 1, tf_default=tf_default)
    if origin is None:
        return None
    leg = abs(extreme["price"] - origin["price"])
    if leg <= 0:
        return None
    cur_price = _choch_bos_close(ctx, tf_default, shift)
    if cur_price is None:
        return None
    if direction == "bullish":
        return round((extreme["price"] - cur_price) / leg * 100.0, 1)
    return round((cur_price - extreme["price"]) / leg * 100.0, 1)

def _bounce_off_swing(ctx: Dict[str, Any], side: str, tolerance_pct: float, lookback: int, left: int, right: int, shift: int, tf_default: str, max_pivot_age: int = 15) -> Optional[bool]:
    """Practical price-action version of "tested a level and rejected"
    -- NOT the raw swing-high/low value itself, but whether the CURRENT
    bar actually touched a PRIOR (older) swing level and closed back
    away from it. side="high": price wicked up into/near a prior
    swing high (resistance) and closed back below it -- a bounce-down
    rejection. side="low": mirror, support test and bounce up.

    Deliberately excludes the CURRENT bar's own swing pivot from being
    the level tested against (that would be circular -- a bar can't
    "bounce off" a level it just created) by requiring the prior swing
    pivot with age > 0 relative to now.

    max_pivot_age caps how OLD the referenced pivot can be (default 15
    bars) -- without this, a stock that's been range-bound for weeks
    near some local peak from 40+ bars back would register a "bounce"
    on EVERY day it happens to sit near that stale level, since the
    level itself never goes away within the lookback window. A real
    bounce is a FRESH rejection off a level price only recently
    approached again, not ongoing chop under an old ceiling.
    """
    snap = ctx.get("timeframes", {}).get(tf_default)
    series = (snap or {}).get("series", {})
    high, low, close = series.get("high"), series.get("low"), series.get("close")
    if high is None or low is None or close is None:
        return None
    idx_now = len(close) - 1 - shift
    if idx_now < 0:
        return None

    # Find the most recent CONFIRMED swing pivot on the requested side,
    # excluding today (right=1 minimum ensures the pivot search doesn't
    # touch the current/most recent bars at all).
    pivot = _last_swing_pivot(ctx, side=side, lookback=lookback, left=left, right=max(2, right), shift=shift + 1, tf_default=tf_default)
    if pivot is None or pivot.get("age", 0) < 1:
        return None
    if pivot.get("age", 0) > max_pivot_age:
        return None  # too stale -- this is ongoing chop under an old level, not a fresh test-and-reject
    level = pivot["price"]

    bar_high, bar_low, bar_close = float(high.iloc[idx_now]), float(low.iloc[idx_now]), float(close.iloc[idx_now])
    tol = level * (tolerance_pct / 100.0)

    if side == "high":
        touched = bar_high >= level - tol
        rejected = bar_close < level
        return bool(touched and rejected)
    touched = bar_low <= level + tol
    rejected = bar_close > level
    return bool(touched and rejected)

def _pin_bar_at_level(ctx: Dict[str, Any], side: str, tolerance_pct: float, lookback: int, left: int, right: int, shift: int, tf_default: str, max_pivot_age: int = 15) -> Optional[bool]:
    """The practical version of a pin-bar reversal -- not just the
    candle shape (that's what IsHammer/IsShootingStar already check
    context-free), but the shape occurring AT a real prior swing
    level, which is what actually makes a pin bar meaningful in
    practice rather than just noise. side="low": hammer shape at a
    prior swing low (support). side="high": shooting-star shape at a
    prior swing high (resistance)."""
    bar = _get_ohlc_bar(ctx, tf_default, shift, bars_back=0)
    if not bar:
        return None
    shape_ok = _is_hammer(bar) if side == "low" else _is_shooting_star(bar)
    if not shape_ok:
        return False
    return _bounce_off_swing(ctx, "low" if side == "low" else "high", tolerance_pct, lookback, left, right, shift, tf_default, max_pivot_age=max_pivot_age)

def _detect_head_and_shoulders(ctx: Dict[str, Any], direction: str, shoulder_tol_pct: float, lookback: int, left: int, right: int, shift: int, tf_default: str) -> Optional[Dict[str, Any]]:
    """direction: "bearish" (regular H&S, topping -- head is the
    tallest of 3 swing highs) or "bullish" (inverse H&S, bottoming --
    head is the deepest of 3 swing lows). Chains 5 alternating swing
    pivots (shoulder-trough-head-trough-shoulder), same shift-chaining
    technique as the CHoCH/BOS detectors. This is a harder pattern to
    detect reliably than a 2-3 candle pattern -- more moving parts,
    more chances for a coincidental near-match -- worth verifying
    against charts you know before trusting it in a live scan."""
    peak_side = "high" if direction == "bearish" else "low"
    trough_side = "low" if direction == "bearish" else "high"

    cur_shift = shift
    rs = _last_swing_pivot(ctx, side=peak_side, lookback=lookback, left=left, right=right, shift=cur_shift, tf_default=tf_default)
    if rs is None:
        return None
    cur_shift = shift + int(rs["age"]) + 1
    rt = _last_swing_pivot(ctx, side=trough_side, lookback=lookback, left=left, right=right, shift=cur_shift, tf_default=tf_default)
    if rt is None:
        return None
    cur_shift += int(rt["age"]) + 1
    head = _last_swing_pivot(ctx, side=peak_side, lookback=lookback, left=left, right=right, shift=cur_shift, tf_default=tf_default)
    if head is None:
        return None
    cur_shift += int(head["age"]) + 1
    lt = _last_swing_pivot(ctx, side=trough_side, lookback=lookback, left=left, right=right, shift=cur_shift, tf_default=tf_default)
    if lt is None:
        return None
    cur_shift += int(lt["age"]) + 1
    ls = _last_swing_pivot(ctx, side=peak_side, lookback=lookback, left=left, right=right, shift=cur_shift, tf_default=tf_default)
    if ls is None:
        return None

    if direction == "bearish":
        head_is_tallest = head["price"] > rs["price"] and head["price"] > ls["price"]
    else:
        head_is_tallest = head["price"] < rs["price"] and head["price"] < ls["price"]
    if not head_is_tallest or not ls["price"]:
        return {"detected": False}

    shoulder_diff_pct = abs(rs["price"] - ls["price"]) / abs(ls["price"]) * 100.0
    if shoulder_diff_pct > shoulder_tol_pct:
        return {"detected": False}

    neckline = (rt["price"] + lt["price"]) / 2.0
    cur_price = _choch_bos_close(ctx, tf_default, shift)
    if cur_price is None:
        return {"detected": False}
    broke_neckline = (cur_price < neckline) if direction == "bearish" else (cur_price > neckline)

    return {"detected": True, "neckline": round(neckline, 4), "broke_neckline": bool(broke_neckline), "head": round(head["price"], 4)}

def _detect_flag(ctx: Dict[str, Any], direction: str, pole_pct: float, pole_bars: int, flag_bars: int, flag_max_retrace_pct: float, tf_default: str, shift: int) -> Optional[Dict[str, Any]]:
    """Bull/bear flag: a sharp, single-direction "pole" move followed
    by a tight, controlled consolidation (the "flag") that only
    partially retraces the pole, then a breakout continuing the pole's
    direction. direction="bullish": pole moves up, flag should hold
    above flag_max_retrace_pct of the pole's gain; direction="bearish":
    mirror. The pole window sits immediately BEFORE the flag window in
    time -- pole_bars back for the pole, then flag_bars more recent
    bars for the flag itself."""
    snap = ctx.get("timeframes", {}).get(tf_default)
    close = (snap or {}).get("series", {}).get("close")
    if close is None:
        return None
    idx_now = len(close) - 1 - shift
    if idx_now < pole_bars + flag_bars:
        return None

    flag_start = idx_now - flag_bars + 1
    pole_start = flag_start - pole_bars
    if pole_start < 0:
        return None

    pole_open = float(close.iloc[pole_start])
    pole_close = float(close.iloc[flag_start - 1]) if flag_start > 0 else float(close.iloc[pole_start])
    if pole_open == 0:
        return None
    pole_move_pct = (pole_close - pole_open) / abs(pole_open) * 100.0

    if direction == "bullish":
        if pole_move_pct < pole_pct:
            return {"detected": False}
    else:
        if pole_move_pct > -pole_pct:
            return {"detected": False}

    flag_window = close.iloc[flag_start:idx_now + 1]
    flag_high, flag_low = float(flag_window.max()), float(flag_window.min())
    pole_range = abs(pole_close - pole_open)
    if pole_range == 0:
        return {"detected": False}

    if direction == "bullish":
        retrace_pct = (pole_close - flag_low) / pole_range * 100.0
    else:
        retrace_pct = (flag_high - pole_close) / pole_range * 100.0
    if retrace_pct > flag_max_retrace_pct:
        return {"detected": False}  # flag consolidation gave back too much of the pole -- not a controlled pullback

    cur_price = float(close.iloc[idx_now])
    breakout = (cur_price > flag_high) if direction == "bullish" else (cur_price < flag_low)
    return {"detected": True, "pole_move_pct": round(pole_move_pct, 1), "retrace_pct": round(retrace_pct, 1), "breakout": bool(breakout)}


def _detect_cup_and_handle(ctx: Dict[str, Any], cup_lookback: int, handle_bars: int, handle_max_pct: float, rim_tolerance_pct: float, tf_default: str, shift: int) -> Optional[Dict[str, Any]]:
    """Simplified geometric approximation, not true curve-fitting --
    worth being upfront about that rather than overclaiming precision.
    Looks for: a left rim (the high before a decline), a cup bottom
    (the lowest point after that), price recovering back within
    rim_tolerance_pct of the left rim (the right rim), then a shallow
    "handle" pullback in the most recent handle_bars (small relative to
    the cup's full depth), with the breakout confirmed by price
    clearing the left rim. Classic O'Neil cup-and-handle also cares
    about the cup's rounded shape and multi-week duration -- this
    checks the price levels and handle shallowness, not the shape's
    smoothness, so it will accept a sharper V-shaped recovery that a
    strict definition would reject."""
    snap = ctx.get("timeframes", {}).get(tf_default)
    close = (snap or {}).get("series", {}).get("close")
    if close is None:
        return None
    idx_now = len(close) - 1 - shift
    if idx_now < cup_lookback:
        return None

    cup_window = close.iloc[idx_now - cup_lookback + 1: idx_now + 1]
    left_rim_idx = int(cup_window.values.argmax())
    left_rim = float(cup_window.iloc[left_rim_idx])
    after_rim = cup_window.iloc[left_rim_idx:]
    if len(after_rim) < 3:
        return {"detected": False}
    cup_bottom = float(after_rim.min())
    cup_depth = left_rim - cup_bottom
    if cup_depth <= 0:
        return {"detected": False}

    cur_price = float(close.iloc[idx_now])
    near_rim = cur_price >= left_rim * (1 - rim_tolerance_pct / 100.0)
    if not near_rim:
        return {"detected": False}

    if idx_now < handle_bars:
        return {"detected": False}
    handle_window = close.iloc[idx_now - handle_bars + 1: idx_now + 1]
    handle_high, handle_low = float(handle_window.max()), float(handle_window.min())
    handle_depth_pct = (handle_high - handle_low) / cup_depth * 100.0
    if handle_depth_pct > handle_max_pct:
        return {"detected": False}  # pullback too deep to be a controlled handle, not just a normal cup wiggle

    breakout = cur_price > left_rim
    return {"detected": True, "left_rim": round(left_rim, 4), "cup_depth_pct": round(cup_depth / left_rim * 100.0, 1),
            "handle_depth_pct": round(handle_depth_pct, 1), "breakout": bool(breakout)}

def _sector_etf_strength(ctx: Dict[str, Any], sector_etf: str, period: int, tf: str, shift: int) -> Optional[float]:
    """Is a sector ETF outperforming SPY -- sector-level rotation
    strength, not an individual stock's own RS. Takes the sector ETF
    directly (already resolved via ctx["sector_etf"], the SAME
    _sector_etf_for_symbol() lookup SectorRS() itself uses -- not a
    second, separate sector-mapping system). Neither side of this
    comparison is the stock being scanned, so it fetches both series
    directly via the same _benchmark_df_from_ctx used for
    RelativeStrength's benchmark side, rather than reusing ctx's own
    (stock-specific) close series."""
    if not sector_etf:
        return None
    sector_df = _benchmark_df_from_ctx(ctx, sector_etf, tf)
    spy_df = _benchmark_df_from_ctx(ctx, "SPY", tf)
    if sector_df is None or sector_df.empty or spy_df is None or spy_df.empty:
        return None
    sector_ret = _period_return_pct(sector_df["Close"].astype(float), period, shift=shift)
    spy_ret = _period_return_pct(spy_df["Close"].astype(float), period, shift=shift)
    if sector_ret is None or spy_ret is None:
        return None
    return round(sector_ret - spy_ret, 2)

def _read_precomputed_rs(ctx: Dict[str, Any], field: str, period: int, tf: str, shift: int) -> Optional[float]:
    """Reads a pre-computed RS field (rs_vs_spy / sector_rs /
    sector_strength) from technical_snapshot.py's cache -- populated
    by the SAME background batch job that already computes RSI/EMA/
    MACD/Score/ConfScore for every watchlist symbol (including SPY,
    SPX, and the sector ETFs, registered specifically so this cache
    stays warm). Only applies for shift=0 (live, not historical) and
    period=20/tf=1d (what gets pre-computed) -- any other period or
    timeframe falls through to live computation, since the cache has
    no way to serve a different window. Returns None on any cache
    miss or mismatch; callers are expected to fall back to computing
    live."""
    if shift != 0 or period != 20 or _normalize_tf(tf) != "1d":
        return None
    symbol = str(ctx.get("symbol") or "").upper()
    if not symbol:
        return None
    try:
        from ..services.technical_snapshot import get_or_compute_technical_snapshot
        snap = get_or_compute_technical_snapshot(symbol, "1d")
    except Exception:
        return None
    if snap is None:
        return None
    return snap.get(field)

def _bollinger_bands(ctx: Dict[str, Any], period: int, mult: float, shift: int, tf_default: str) -> Optional[Tuple[float, float, float]]:
    """Standard Bollinger Bands: SMA basis +/- (stdev * mult).
    Returns (upper, middle, lower) at the requested bar, or None if
    there isn't enough history."""
    snap = ctx.get("timeframes", {}).get(tf_default)
    close = (snap or {}).get("series", {}).get("close")
    if close is None:
        return None
    period = max(1, int(period or 20))
    idx_now = len(close) - 1 - shift
    if idx_now < period - 1:
        return None
    window = close.iloc[max(0, idx_now - period + 1): idx_now + 1]
    middle = float(window.mean())
    stdev = float(window.std(ddof=0))
    upper = middle + stdev * mult
    lower = middle - stdev * mult
    return upper, middle, lower


def _keltner_channel(ctx: Dict[str, Any], period: int, atr_period: int, mult: float, shift: int, tf_default: str) -> Optional[Tuple[float, float, float]]:
    """Standard Keltner Channel: EMA basis +/- (ATR * mult). Same
    defaults as the EMA_BB Pine indicator built earlier this session
    (period=20, atr_period=10, mult=2.0)."""
    snap = ctx.get("timeframes", {}).get(tf_default)
    series = (snap or {}).get("series", {})
    high, low, close = series.get("high"), series.get("low"), series.get("close")
    if high is None or low is None or close is None:
        return None
    period = max(1, int(period or 20))
    atr_period = max(1, int(atr_period or 10))
    idx_now = len(close) - 1 - shift
    if idx_now < max(period, atr_period):
        return None
    ema_basis = _ema(close, period)
    atr = _atr_series(high, low, close, atr_period)
    middle = float(ema_basis.iloc[idx_now])
    band = float(atr.iloc[idx_now]) * mult
    return middle + band, middle, middle - band

def _best_second_leg_pattern(
    ctx: Dict[str, Any],
    side: str = "up",
    lookback: int = 45,
    shift: int = 0,
    tf_default: str = "1d",
    tolerance_pct: float = 5.0,
    min_swing_pct: float = 3.0,
    recent_bars: int = 3,
) -> Optional[Dict[str, float]]:
    """Detect a loose second-leg / M-W pattern without future leakage.

    side="up"  => H1 -> valley -> H2/current retest of H1.  This is a potential
                  M-top / second leg up / bearish mean-reversion zone.
    side="down"=> L1 -> peak   -> L2/current retest of L1.  This is a potential
                  W-bottom / second leg down / bullish mean-reversion zone.

    The second leg can be a little higher or lower than the first leg.  The
    tolerance is measured as percent distance between first and second swing.
    """
    side = _second_leg_side_arg(side)
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    series = snap.get("series", {})
    high_s = pd.to_numeric(series.get("high"), errors="coerce") if series.get("high") is not None else None
    low_s = pd.to_numeric(series.get("low"), errors="coerce") if series.get("low") is not None else None
    close_s = pd.to_numeric(series.get("close"), errors="coerce") if series.get("close") is not None else None
    if high_s is None or low_s is None or close_s is None:
        return None
    n = min(len(high_s), len(low_s), len(close_s))
    if n < 8:
        return None
    idx_now = n - 1 - max(0, int(shift or 0))
    if idx_now < 6:
        return None
    lookback = max(8, int(float(lookback or 45)))
    tolerance_pct = max(0.1, float(tolerance_pct if tolerance_pct is not None else 5.0))
    min_swing_pct = max(0.0, float(min_swing_pct if min_swing_pct is not None else 3.0))
    recent_bars = max(1, int(float(recent_bars if recent_bars is not None else 3)))
    start = max(0, idx_now - lookback)
    # Evaluate the current bar and a few recent bars so a pattern remains visible
    # briefly after the second touch/rejection instead of disappearing instantly.
    probe_start = max(start + 4, idx_now - recent_bars + 1)
    probe_indices = list(range(idx_now, probe_start - 1, -1))
    best: Optional[Dict[str, float]] = None

    if side == "up":
        # H1 -> valley -> H2/retest. H1 must occur before the second leg.
        pivot_highs = _pivot_indices(high_s, start, idx_now - 2, kind="high", span=2)
        if not pivot_highs:
            try:
                prior = pd.to_numeric(high_s.iloc[start:max(start + 1, idx_now - 2)], errors="coerce")
                if not prior.dropna().empty:
                    pivot_highs = [int(prior.idxmax()) if isinstance(prior.index, pd.RangeIndex) else int(high_s.index.get_loc(prior.idxmax()))]
            except Exception:
                pivot_highs = []
        for probe_idx in probe_indices:
            h2 = _safe_number(high_s.iloc[probe_idx])
            cur_close = _safe_number(close_s.iloc[probe_idx])
            if h2 is None:
                continue
            for h1_idx in pivot_highs:
                if h1_idx >= probe_idx - 2 or h1_idx < start:
                    continue
                h1 = _safe_number(high_s.iloc[h1_idx])
                if h1 is None or h1 <= 0:
                    continue
                valley_window = pd.to_numeric(low_s.iloc[h1_idx + 1:probe_idx], errors="coerce").dropna()
                if valley_window.empty:
                    continue
                try:
                    valley_label = valley_window.idxmin()
                    valley_idx = int(low_s.index.get_loc(valley_label)) if not isinstance(low_s.index, pd.RangeIndex) else int(valley_label)
                except Exception:
                    # Fallback for unusual indexes.
                    rel = int(valley_window.reset_index(drop=True).idxmin())
                    valley_idx = h1_idx + 1 + rel
                if valley_idx <= h1_idx or valley_idx >= probe_idx:
                    continue
                neckline = _safe_number(low_s.iloc[valley_idx])
                if neckline is None or neckline <= 0:
                    continue
                drop_pct = (h1 - neckline) / h1 * 100.0
                rally_pct = (h2 - neckline) / neckline * 100.0
                if drop_pct < min_swing_pct or rally_pct < min_swing_pct:
                    continue
                match_pct = abs(h2 - h1) / h1 * 100.0
                if match_pct > tolerance_pct:
                    continue
                # Avoid calling a runaway breakout an M top.  If it closes well
                # above the tolerance band, it is no longer just a retest.
                if cur_close is not None and cur_close > h1 * (1.0 + tolerance_pct / 100.0):
                    continue
                age = idx_now - probe_idx
                match_score = max(0.0, 1.0 - match_pct / tolerance_pct) * 35.0
                swing_score = min(1.0, drop_pct / max(min_swing_pct * 2.0, min_swing_pct + 1.0, 1.0)) * 25.0
                rally_score = min(1.0, rally_pct / max(min_swing_pct * 2.0, min_swing_pct + 1.0, 1.0)) * 20.0
                recency_score = max(0.0, 1.0 - age / max(1.0, float(recent_bars))) * 10.0
                symmetry_score = max(0.0, 1.0 - abs((probe_idx - valley_idx) - (valley_idx - h1_idx)) / max(1.0, float(probe_idx - h1_idx))) * 10.0
                score = max(0.0, min(100.0, match_score + swing_score + rally_score + recency_score + symmetry_score))
                item = {
                    "found": 1.0,
                    "side": "up",
                    "pattern": "M",
                    "score": score,
                    "first_idx": float(h1_idx),
                    "neckline_idx": float(valley_idx),
                    "second_idx": float(probe_idx),
                    "age": float(age),
                    "first": h1,
                    "first_price": h1,
                    "neckline": neckline,
                    "second": h2,
                    "second_price": h2,
                    "match_pct": match_pct,
                    "swing_pct": drop_pct,
                    "rebound_pct": rally_pct,
                    "midpoint": (h1 + neckline) / 2.0,
                    "target": neckline - (h1 - neckline),
                }
                if best is None or score > best.get("score", 0):
                    best = item
    else:
        # L1 -> peak -> L2/retest. L1 must occur before the second leg.
        pivot_lows = _pivot_indices(low_s, start, idx_now - 2, kind="low", span=2)
        if not pivot_lows:
            try:
                prior = pd.to_numeric(low_s.iloc[start:max(start + 1, idx_now - 2)], errors="coerce")
                if not prior.dropna().empty:
                    pivot_lows = [int(prior.idxmin()) if isinstance(prior.index, pd.RangeIndex) else int(low_s.index.get_loc(prior.idxmin()))]
            except Exception:
                pivot_lows = []
        for probe_idx in probe_indices:
            l2 = _safe_number(low_s.iloc[probe_idx])
            cur_close = _safe_number(close_s.iloc[probe_idx])
            if l2 is None or l2 <= 0:
                continue
            for l1_idx in pivot_lows:
                if l1_idx >= probe_idx - 2 or l1_idx < start:
                    continue
                l1 = _safe_number(low_s.iloc[l1_idx])
                if l1 is None or l1 <= 0:
                    continue
                peak_window = pd.to_numeric(high_s.iloc[l1_idx + 1:probe_idx], errors="coerce").dropna()
                if peak_window.empty:
                    continue
                try:
                    peak_label = peak_window.idxmax()
                    peak_idx = int(high_s.index.get_loc(peak_label)) if not isinstance(high_s.index, pd.RangeIndex) else int(peak_label)
                except Exception:
                    rel = int(peak_window.reset_index(drop=True).idxmax())
                    peak_idx = l1_idx + 1 + rel
                if peak_idx <= l1_idx or peak_idx >= probe_idx:
                    continue
                neckline = _safe_number(high_s.iloc[peak_idx])
                if neckline is None or neckline <= 0:
                    continue
                bounce_pct = (neckline - l1) / l1 * 100.0
                selloff_pct = (neckline - l2) / neckline * 100.0
                if bounce_pct < min_swing_pct or selloff_pct < min_swing_pct:
                    continue
                match_pct = abs(l2 - l1) / l1 * 100.0
                if match_pct > tolerance_pct:
                    continue
                # Avoid calling a runaway breakdown a W bottom.
                if cur_close is not None and cur_close < l1 * (1.0 - tolerance_pct / 100.0):
                    continue
                age = idx_now - probe_idx
                match_score = max(0.0, 1.0 - match_pct / tolerance_pct) * 35.0
                bounce_score = min(1.0, bounce_pct / max(min_swing_pct * 2.0, min_swing_pct + 1.0, 1.0)) * 25.0
                selloff_score = min(1.0, selloff_pct / max(min_swing_pct * 2.0, min_swing_pct + 1.0, 1.0)) * 20.0
                recency_score = max(0.0, 1.0 - age / max(1.0, float(recent_bars))) * 10.0
                symmetry_score = max(0.0, 1.0 - abs((probe_idx - peak_idx) - (peak_idx - l1_idx)) / max(1.0, float(probe_idx - l1_idx))) * 10.0
                score = max(0.0, min(100.0, match_score + bounce_score + selloff_score + recency_score + symmetry_score))
                item = {
                    "found": 1.0,
                    "side": "down",
                    "pattern": "W",
                    "score": score,
                    "first_idx": float(l1_idx),
                    "neckline_idx": float(peak_idx),
                    "second_idx": float(probe_idx),
                    "age": float(age),
                    "first": l1,
                    "first_price": l1,
                    "neckline": neckline,
                    "second": l2,
                    "second_price": l2,
                    "match_pct": match_pct,
                    "swing_pct": bounce_pct,
                    "selloff_pct": selloff_pct,
                    "midpoint": (l1 + neckline) / 2.0,
                    "target": neckline + (neckline - l1),
                }
                if best is None or score > best.get("score", 0):
                    best = item
    return best


def _second_leg_level_value(pattern: Optional[Dict[str, float]], which: Any) -> Optional[float]:
    if not pattern:
        return None
    text = str(which or "").strip().lower().replace("_", " ").replace("-", " ")
    aliases = {
        "1": "first", "first": "first", "first price": "first", "h1": "first", "l1": "first", "prior": "first", "prior swing": "first",
        "2": "second", "second": "second", "second price": "second", "h2": "second", "l2": "second", "current": "second", "retest": "second",
        "neck": "neckline", "neckline": "neckline", "valley": "neckline", "peak": "neckline",
        "mid": "midpoint", "middle": "midpoint", "midpoint": "midpoint", "50": "midpoint", "50%": "midpoint",
        "target": "target", "measured move": "target",
    }
    key = aliases.get(text, text)
    return _safe_number(pattern.get(key))


def _change_pct_and_avg_abs_series(close_s: pd.Series, avg_bars: int = 60) -> Tuple[pd.Series, pd.Series]:
    """One-bar ChangePct and prior EMA of absolute ChangePct.

    The baseline is shifted by one bar so a candidate strong candle is compared
    against the volatility that was known before that candle formed.  Values are
    returned in percent points, e.g. 2.4 means +2.4%.  This follows the user's
    preferred definition: EMA(abs(ChangePct), avg_bars), not SMA/ATR.
    """
    avg_bars = max(1, int(avg_bars or 60))
    close_s = pd.to_numeric(close_s, errors="coerce")
    prev = close_s.shift(1).abs()
    change_pct = ((close_s - close_s.shift(1)) / prev.replace(0, math.nan)) * 100.0
    min_periods = min(max(5, avg_bars // 4), avg_bars)
    avg_abs = change_pct.abs().shift(1).ewm(span=avg_bars, adjust=False, min_periods=min_periods).mean()
    return change_pct, avg_abs


def _avg_abs_change_pct(ctx: Dict[str, Any], bars: int = 60, shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    close_s = snap.get("series", {}).get("close")
    if close_s is None:
        return None
    try:
        _, avg_abs = _change_pct_and_avg_abs_series(close_s, avg_bars=bars)
        idx = len(avg_abs) - 1 - max(0, int(shift or 0))
        if idx < 0 or idx >= len(avg_abs):
            return None
        val = float(avg_abs.iloc[idx])
        return val if math.isfinite(val) else None
    except Exception:
        return None

def _strong_candle_anchor(
    ctx: Dict[str, Any],
    side: str = "bull",
    lookback: int = 20,
    shift: int = 0,
    tf_default: str = "1d",
    min_body_pct: float = 50.0,
    min_range_atr: float = 2.0,
    min_vol_mult: float = 1.1,
    avg_change_bars: int = 60,
) -> Optional[Dict[str, float]]:
    """Return the most recent prior strong bull/bear candle anchor.

    The current bar is intentionally excluded.  These primitives are designed
    for retest scans: first find a strong candle that already happened, then
    test whether the current bar is touching that candle's high/low/mid/etc.
    """
    side = _side_arg(side)
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    series = snap.get("series", {})
    open_s = pd.to_numeric(series.get("open"), errors="coerce") if series.get("open") is not None else None
    high_s = pd.to_numeric(series.get("high"), errors="coerce") if series.get("high") is not None else None
    low_s = pd.to_numeric(series.get("low"), errors="coerce") if series.get("low") is not None else None
    close_s = pd.to_numeric(series.get("close"), errors="coerce") if series.get("close") is not None else None
    vol_s = pd.to_numeric(series.get("volume"), errors="coerce") if series.get("volume") is not None else None
    if open_s is None or high_s is None or low_s is None or close_s is None:
        return None
    n = min(len(open_s), len(high_s), len(low_s), len(close_s))
    if n < 3:
        return None
    idx_now = n - 1 - max(0, int(shift or 0))
    if idx_now <= 0:
        return None

    lookback = max(1, int(lookback or 1))
    min_body_pct = max(0.0, float(min_body_pct if min_body_pct is not None else 50.0))
    # min_range_atr is kept as the internal/backward-compatible parameter name,
    # but it now means: move multiplier versus AvgAbsChangePct(avg_change_bars).
    move_mult = max(0.0, float(min_range_atr if min_range_atr is not None else 2.0))
    min_vol_mult = max(0.0, float(min_vol_mult if min_vol_mult is not None else 1.1))
    avg_change_bars = max(1, int(avg_change_bars if avg_change_bars is not None else 60))

    try:
        change_pct_s, avg_abs_change_s = _change_pct_and_avg_abs_series(close_s, avg_bars=avg_change_bars)
    except Exception:
        change_pct_s, avg_abs_change_s = None, None
    vol_avg = None
    if vol_s is not None and len(vol_s):
        try:
            # User preference: compare candle volume to EMA(volume,20), not SMA.
            vol_avg = vol_s.ewm(span=20, adjust=False, min_periods=5).mean()
        except Exception:
            vol_avg = None

    for age in range(1, lookback + 1):
        idx = idx_now - age
        if idx < 0:
            break
        try:
            o = float(open_s.iloc[idx]); h = float(high_s.iloc[idx]); l = float(low_s.iloc[idx]); c = float(close_s.iloc[idx])
        except Exception:
            continue
        if not all(math.isfinite(v) for v in (o, h, l, c)):
            continue
        rng = h - l
        if rng <= 0:
            continue
        body = abs(c - o)
        body_pct = body / rng * 100.0
        close_pos = (c - l) / rng * 100.0
        if body_pct < min_body_pct:
            continue
        if side == "bull":
            if c <= o or close_pos < 60.0:
                continue
        elif side == "bear":
            if c >= o or close_pos > 40.0:
                continue
        else:
            return None

        change_pct = None
        avg_abs_change_pct = None
        move_multiple = None
        if change_pct_s is not None and avg_abs_change_s is not None and idx < len(change_pct_s) and idx < len(avg_abs_change_s):
            try:
                change_pct = float(change_pct_s.iloc[idx])
                avg_abs_change_pct = float(avg_abs_change_s.iloc[idx])
                if math.isfinite(change_pct) and math.isfinite(avg_abs_change_pct) and avg_abs_change_pct > 0:
                    move_multiple = abs(change_pct) / avg_abs_change_pct
            except Exception:
                change_pct = None
                avg_abs_change_pct = None
                move_multiple = None
        if move_mult > 0 and move_multiple is not None:
            if side == "bull" and change_pct < avg_abs_change_pct * move_mult:
                continue
            if side == "bear" and change_pct > -avg_abs_change_pct * move_mult:
                continue
        # If there is not enough change history for the baseline, do not reject solely on that.

        vol_ratio = None
        if vol_s is not None and vol_avg is not None and idx < len(vol_s) and idx < len(vol_avg):
            try:
                v = float(vol_s.iloc[idx]); avgv = float(vol_avg.iloc[idx])
                if math.isfinite(v) and math.isfinite(avgv) and avgv > 0:
                    vol_ratio = v / avgv
            except Exception:
                vol_ratio = None
        if min_vol_mult > 0 and vol_ratio is not None and vol_ratio < min_vol_mult:
            continue
        # If volume history is unavailable, do not reject the candle solely on volume.
        # When available, minVolMult is checked against EMA(volume,20).

        return {
            "age": float(age),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "range": rng,
            "body_pct": body_pct,
            "close_pos": close_pos,
            "change_pct": change_pct if change_pct is not None else float("nan"),
            "avg_abs_change_pct": avg_abs_change_pct if avg_abs_change_pct is not None else float("nan"),
            "move_multiple": move_multiple if move_multiple is not None else float("nan"),
            "volume_ratio": vol_ratio if vol_ratio is not None else float("nan"),
        }
    return None


def _strong_candle_level(anchor: Optional[Dict[str, float]], level: Any) -> Optional[float]:
    if not anchor:
        return None
    lo = _safe_number(anchor.get("low"))
    hi = _safe_number(anchor.get("high"))
    if lo is None or hi is None:
        return None
    rng = hi - lo
    text = str(level).strip().lower().replace("_", " ").replace("-", " ") if level is not None else "mid"
    aliases = {
        "l": "low", "lo": "low", "low": "low",
        "h": "high", "hi": "high", "high": "high",
        "o": "open", "open": "open",
        "c": "close", "close": "close",
        "m": "mid", "mid": "mid", "middle": "mid", "half": "mid", "50%": "mid", "50": "mid",
    }
    key = aliases.get(text)
    if key in {"low", "high", "open", "close"}:
        return _safe_number(anchor.get(key))
    if key == "mid":
        return lo + rng * 0.5
    # Numeric level means percent of the candle's low-to-high range.
    # 0 = low, 50 = midpoint, 100 = high.  Works the same for bull and bear anchors.
    try:
        pct = float(str(level).strip().rstrip("%"))
    except Exception:
        return None
    return lo + rng * (pct / 100.0)


def _touch_strong_candle_level(
    ctx: Dict[str, Any],
    side: str,
    level: Any,
    lookback: int,
    tolerance_pct: float = 0.75,
    shift: int = 0,
    tf_default: str = "1d",
    min_body_pct: float = 50.0,
    min_range_atr: float = 2.0,
    min_vol_mult: float = 1.1,
    avg_change_bars: int = 60,
) -> Optional[bool]:
    anchor = _strong_candle_anchor(
        ctx,
        side=side,
        lookback=lookback,
        shift=shift,
        tf_default=tf_default,
        min_body_pct=min_body_pct,
        min_range_atr=min_range_atr,
        min_vol_mult=min_vol_mult,
        avg_change_bars=avg_change_bars,
    )
    price_level = _strong_candle_level(anchor, level)
    if price_level is None:
        return None
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    high_s = snap.get("series", {}).get("high")
    low_s = snap.get("series", {}).get("low")
    close_s = snap.get("series", {}).get("close")
    idx = None
    if close_s is not None:
        idx = len(close_s) - 1 - max(0, int(shift or 0))
    if idx is None or idx < 0:
        return None
    try:
        cur_hi = float(high_s.iloc[idx]) if high_s is not None else float(close_s.iloc[idx])
        cur_lo = float(low_s.iloc[idx]) if low_s is not None else float(close_s.iloc[idx])
        cur_close = float(close_s.iloc[idx])
    except Exception:
        return None
    tol = abs(float(tolerance_pct if tolerance_pct is not None else 0.75))
    band = abs(price_level) * tol / 100.0 if price_level else tol / 100.0
    if cur_lo <= price_level + band and cur_hi >= price_level - band:
        return True
    return abs(cur_close - price_level) <= band


def _tv_sr_channels(ctx: Dict[str, Any], tf_default: str = "1d", prd: int = 10, channel_w_pct: float = 5.0, loopback: int = 290, max_sr: int = 6) -> List[Dict[str, float]]:
    """TradingView-style Support Resistance Channels based on pivot highs/lows.

    Defaults mirror the indicator shown by the user:
    Pivot Period=10, Source=High/Low, Channel Width=5%, Minimum Strength=1,
    Maximum Number of S/R=6, Loopback=290.
    """
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return []
    series = snap.get("series", {})
    high = series.get("high")
    low = series.get("low")
    close = series.get("close")
    open_ = series.get("open")
    if high is None or low is None or close is None or open_ is None:
        return []
    high = pd.to_numeric(high, errors="coerce")
    low = pd.to_numeric(low, errors="coerce")
    close = pd.to_numeric(close, errors="coerce")
    open_ = pd.to_numeric(open_, errors="coerce")
    if high.isna().all() or low.isna().all() or close.isna().all() or open_.isna().all():
        return []

    n = len(close)
    prd = max(4, int(prd or 10))
    channel_w_pct = float(channel_w_pct or 5.0)
    loopback = max(1, int(loopback or 290))
    max_sr = max(1, int(max_sr or 6))
    if n < prd * 2 + 5:
        return []

    def is_pivot_high(i: int) -> bool:
        if i < prd or i + prd >= n:
            return False
        try:
            return all(float(high.iloc[i]) >= float(high.iloc[i - j]) for j in range(1, prd + 1)) and all(float(high.iloc[i]) >= float(high.iloc[i + j]) for j in range(1, prd + 1))
        except Exception:
            return False

    def is_pivot_low(i: int) -> bool:
        if i < prd or i + prd >= n:
            return False
        try:
            return all(float(low.iloc[i]) <= float(low.iloc[i - j]) for j in range(1, prd + 1)) and all(float(low.iloc[i]) <= float(low.iloc[i + j]) for j in range(1, prd + 1))
        except Exception:
            return False

    # Collect confirmed pivots inside the TradingView loopback window.
    pivot_vals: List[float] = []
    start_idx = max(prd, n - loopback - prd)
    end_idx = max(prd, n - prd)
    for i in range(start_idx, end_idx):
        if is_pivot_high(i):
            pivot_vals.append(float(high.iloc[i]))
        elif is_pivot_low(i):
            pivot_vals.append(float(low.iloc[i]))

    if not pivot_vals:
        return []

    recent_high = float(high.iloc[max(0, n - 300):].max())
    recent_low = float(low.iloc[max(0, n - 300):].min())
    cwidth = (recent_high - recent_low) * channel_w_pct / 100.0
    if not math.isfinite(cwidth) or cwidth <= 0:
        return []

    def get_sr_vals(pivot_idx: int) -> Tuple[float, float, int]:
        lo = hi = pivot_vals[pivot_idx]
        numpp = 0
        for pv in pivot_vals:
            wdth = (hi - pv) if pv <= hi else (pv - lo)
            if wdth <= cwidth:
                if pv <= hi:
                    lo = min(lo, pv)
                else:
                    hi = max(hi, pv)
                numpp += 20
        return hi, lo, numpp

    # Add touches, similar to the TradingView script.
    channels: List[Dict[str, float]] = []
    used: set[int] = set()
    for i, pivot in enumerate(pivot_vals):
        if i in used:
            continue
        hi, lo, strength = get_sr_vals(i)
        touches = 0
        for j in range(max(0, n - loopback), n):
            hj = float(high.iloc[j])
            lj = float(low.iloc[j])
            if (hj <= hi and hj >= lo) or (lj <= hi and lj >= lo):
                touches += 1
        strength += touches
        for k, pv in enumerate(pivot_vals):
            if lo <= pv <= hi:
                used.add(k)
        channels.append({"hi": float(hi), "lo": float(lo), "strength": float(strength), "mid": float((hi + lo) / 2.0)})
        if len(channels) >= max_sr * 3:
            # Keep the working set bounded like the TV indicator.
            break

    if not channels:
        return []

    channels.sort(key=lambda c: (-c["strength"], abs(float(close.iloc[-1]) - c["mid"])))
    final: List[Dict[str, float]] = []
    for ch in channels:
        overlap = any(not (ch["hi"] < f["lo"] or ch["lo"] > f["hi"]) for f in final)
        if not overlap:
            final.append(ch)
        if len(final) >= max_sr:
            break
    return final


def _tv_sr_selected_channel(ctx: Dict[str, Any], direction: str, shift: int = 0, tf_default: str = "1d", prd: int = 10) -> Optional[Dict[str, float]]:
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    close = snap.get("series", {}).get("close")
    if close is None:
        return None
    close = pd.to_numeric(close, errors="coerce")
    idx = len(close) - 1 - shift
    if idx < 0:
        return None
    spot = float(close.iloc[idx])
    channels = _tv_sr_channels(ctx, tf_default=tf, prd=max(1, int(prd or 10)), channel_w_pct=5.0, loopback=290, max_sr=6)
    if not channels:
        return None

    direction = (direction or "support").strip().lower()
    candidates: List[Tuple[float, float, Dict[str, float]]] = []
    for ch in channels:
        lo, hi = float(ch["lo"]), float(ch["hi"])
        if direction == "support":
            # Support zones are below or intersecting current price.
            if hi <= spot:
                dist = abs(spot - hi)
                candidates.append((dist, -float(ch["strength"]), ch))
            elif lo <= spot <= hi:
                dist = 0.0
                candidates.append((dist, -float(ch["strength"]), ch))
        else:
            # Resistance zones are above or intersecting current price.
            if lo >= spot:
                dist = abs(lo - spot)
                candidates.append((dist, -float(ch["strength"]), ch))
            elif lo <= spot <= hi:
                dist = 0.0
                candidates.append((dist, -float(ch["strength"]), ch))

    if not candidates:
        # Fallback to the nearest zone regardless of side.
        for ch in channels:
            lo, hi = float(ch["lo"]), float(ch["hi"])
            if spot < lo:
                dist = lo - spot
            elif spot > hi:
                dist = spot - hi
            else:
                dist = 0.0
            candidates.append((dist, -float(ch["strength"]), ch))

    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]

def _tv_sr_zone_bounds(ctx: Dict[str, Any], direction: str, shift: int = 0, tf_default: str = "1d", prd: int = 10) -> Optional[Tuple[Dict[str, float], float, float, float]]:
    ch = _tv_sr_selected_channel(ctx, direction, shift=shift, tf_default=tf_default, prd=prd)
    if not ch:
        return None
    lo = float(ch["lo"])
    hi = float(ch["hi"])
    close = _series_at(ctx, "close", shift=shift, tf_default=tf_default)
    if close is None:
        return None
    return ch, lo, hi, float(close)



def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr1 = (high - low).abs()
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def _atr_series(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    period = max(1, int(period or 14))
    return _true_range(high, low, close).ewm(alpha=1 / period, adjust=False).mean()


def _rma(series: pd.Series, period: int) -> pd.Series:
    period = max(1, int(period or 1))
    return pd.to_numeric(series, errors="coerce").ewm(alpha=1 / period, adjust=False).mean()


_UAE_TF_PARAMS = {
    "5m": {"fast": 5, "slow": 13, "signal": 2, "roc": 3, "slope": 4, "adx_thr": 18.0},
    "15m": {"fast": 7, "slow": 15, "signal": 3, "roc": 4, "slope": 5, "adx_thr": 18.0},
    "1h": {"fast": 8, "slow": 20, "signal": 3, "roc": 5, "slope": 7, "adx_thr": 20.0},
    "2h": {"fast": 8, "slow": 20, "signal": 3, "roc": 5, "slope": 7, "adx_thr": 20.0},
    "4h": {"fast": 8, "slow": 20, "signal": 3, "roc": 5, "slope": 8, "adx_thr": 20.0},
    "1d": {"fast": 8, "slow": 21, "signal": 3, "roc": 6, "slope": 10, "adx_thr": 20.0},
    "1w": {"fast": 8, "slow": 21, "signal": 3, "roc": 7, "slope": 12, "adx_thr": 20.0},
    "1m": {"fast": 8, "slow": 21, "signal": 3, "roc": 7, "slope": 12, "adx_thr": 20.0},
}


def _uae_params(tf: str) -> Dict[str, Any]:
    return dict(_UAE_TF_PARAMS.get(_normalize_tf(tf), _UAE_TF_PARAMS["1d"]))


def _uae_clean_regime(value: Any) -> str:
    raw = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "BULLISH": "BULL",
        "LONG": "BULL",
        "UP": "BULL",
        "WEAKBULL": "WEAK_BULL",
        "WEAK_BULLISH": "WEAK_BULL",
        "BEARISH": "BEAR",
        "SHORT": "BEAR",
        "DOWN": "BEAR",
        "WEAKBEAR": "WEAK_BEAR",
        "WEAK_BEARISH": "WEAK_BEAR",
        "SIDE": "SIDEWAYS",
        "NEUTRAL": "SIDEWAYS",
        "CHOP": "SIDEWAYS",
        "RANGE": "SIDEWAYS",
    }
    return aliases.get(raw, raw)


def _uae_side(value: Any) -> Optional[str]:
    raw = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if raw in {"bull", "bullish", "long", "call", "up", "positive", "buy", "fade long", "mrt buy", "arrow up"}:
        return "bull"
    if raw in {"bear", "bearish", "short", "put", "down", "negative", "sell", "fade short", "mrt sell", "arrow down"}:
        return "bear"
    return None


def _uae_tf_higher(tf: str) -> str:
    tf = _normalize_tf(tf)
    order = ["5m", "15m", "1h", "4h", "1d", "1w"]
    if tf not in order:
        return "1d"
    return order[min(len(order) - 1, order.index(tf) + 1)]


def _uae_tf_stack(tf: str) -> List[str]:
    tf = _normalize_tf(tf)
    first = _uae_tf_higher(tf)
    if tf == "5m":
        return ["15m", "1h"]
    if first == tf:
        return [first]
    return [first]


def _uae_series_bundle(ctx: Dict[str, Any], tf: str = "1d") -> Optional[Dict[str, Any]]:
    tf = _normalize_tf(tf)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    series = snap.get("series", {})
    try:
        close = pd.to_numeric(series.get("close"), errors="coerce")
        high = pd.to_numeric(series.get("high"), errors="coerce")
        low = pd.to_numeric(series.get("low"), errors="coerce")
        volume = pd.to_numeric(series.get("volume"), errors="coerce")
    except Exception:
        return None
    if close is None or high is None or low is None or volume is None or len(close) < 30:
        return None

    params = _uae_params(tf)
    fast_len = int(params["fast"])
    slow_len = int(params["slow"])
    signal_len = int(params["signal"])
    roc_len = int(params["roc"])
    slope_len = int(params["slope"])
    adx_thr = float(params["adx_thr"])
    atr_len = 14
    adx_len = 14
    adx_smooth_len = 3
    hist_lookback = 100
    hist_pct_thr = 0.60
    vol_mult = 1.5
    rsi_len = 14
    rsi_ema_len = 90
    rsi_trend_thr = 12.0
    rsi_ob_thr = 20.0
    rsi_os_thr = -20.0
    min_slope_thr = 0.001

    atrv = _atr_series(high, low, close, atr_len)
    atr_base = _ema(atrv, slow_len)
    safe_atr = atr_base.mask(atr_base.abs() < 1e-9, 1e-9)
    slow_ema = _ema(close, slow_len)

    trend_pos = (close - slow_ema) / safe_atr
    # Pine uses nz(trendPos[rocLen]), so the shifted value is zero while warming up.
    trend_mom = trend_pos - trend_pos.shift(roc_len).fillna(0.0)
    vol_amp = (atrv / safe_atr).clip(lower=0.1)
    raw_sig = trend_mom * (vol_amp ** vol_mult)
    macd_line = _ema(raw_sig, fast_len)
    signal_line = _ema(macd_line, signal_len)
    hist = macd_line - signal_line

    hist_abs = hist.abs()
    # Pine uses ta.percentile_linear_interpolation(abs(hist), 100, 60).
    # Pandas rolling quantile is the closest stable equivalent for the scanner.
    # Pine ta.percentile_linear_interpolation(source, 100, 60) is not available
    # until the full lookback is present. Do not use partial windows, otherwise
    # early/weekly symbols can produce markers that TradingView does not show.
    hist_thresh = hist_abs.rolling(hist_lookback, min_periods=hist_lookback).quantile(hist_pct_thr)
    is_strong_hist = hist_abs > hist_thresh

    rsi_val = _rsi(close, rsi_len)
    rsi_ema_val = _ema(rsi_val, rsi_ema_len)
    rsi_diff = rsi_val - rsi_ema_val

    up_move = high - high.shift(1)
    dn_move = low.shift(1) - low
    plus_dm = up_move.where((up_move > dn_move) & (up_move > 0), 0.0)
    minus_dm = dn_move.where((dn_move > up_move) & (dn_move > 0), 0.0)
    sm_tr = _rma(_true_range(high, low, close), adx_len).replace(0, 1e-9)
    pdi = 100.0 * _rma(plus_dm, adx_len) / sm_tr
    mdi = 100.0 * _rma(minus_dm, adx_len) / sm_tr
    di_sum = (pdi + mdi).replace(0, 1e-9)
    adx_raw = 100.0 * _rma((pdi - mdi).abs() / di_sum, adx_len)
    adx_val = _ema(adx_raw, adx_smooth_len)

    ema_slope = (slow_ema - slow_ema.shift(slope_len)) / float(max(1, slope_len)) / safe_atr
    # Exact v5 Pine trending rule: RSIdiff beyond +/-12 OR smoothed ADX above TF threshold.
    is_trending = (rsi_diff > rsi_trend_thr) | (rsi_diff < -rsi_trend_thr) | (adx_val > adx_thr)
    # Exact v5 Pine slope rule: require minimum normalized EMA slope, not just slope > 0.
    is_bull_slope = ema_slope > min_slope_thr
    is_bear_slope = ema_slope < -min_slope_thr

    regime = pd.Series("SIDEWAYS", index=close.index, dtype="object")
    regime[(is_trending) & (is_bull_slope) & (hist > 0)] = "BULL"
    regime[(is_trending) & (is_bull_slope) & (hist <= 0)] = "WEAK_BULL"
    regime[(is_trending) & (is_bear_slope) & (hist < 0)] = "BEAR"
    regime[(is_trending) & (is_bear_slope) & (hist >= 0)] = "WEAK_BEAR"

    fade_sell = (rsi_diff.shift(1) >= rsi_ob_thr) & (rsi_diff < rsi_ob_thr)
    fade_buy = (rsi_diff.shift(1) <= rsi_os_thr) & (rsi_diff > rsi_os_thr)
    fade_any = fade_sell | fade_buy

    # Exact v5 Pine visible-marker priority:
    #   1) fade/MRT arrow
    #   2) trend triangle
    #   3) nothing
    # Keep a marker_kind series so Scanner Builder can prove which visible marker
    # fired. This prevents a same-bar MRT arrow from being reported as a trend
    # triangle.
    raw_bull_triangle = (macd_line.shift(1) <= 0) & (macd_line > 0) & is_trending & is_strong_hist
    raw_bear_triangle = (macd_line.shift(1) >= 0) & (macd_line < 0) & is_trending & is_strong_hist
    marker_kind = pd.Series("", index=close.index, dtype="object")
    marker_kind[fade_buy.fillna(False)] = "MRT_BUY"
    marker_kind[fade_sell.fillna(False)] = "MRT_SELL"
    marker_kind[(marker_kind == "") & raw_bull_triangle.fillna(False)] = "TREND_BULL"
    marker_kind[(marker_kind == "") & raw_bear_triangle.fillna(False)] = "TREND_BEAR"

    bull_triangle_strong = marker_kind == "TREND_BULL"
    bear_triangle_strong = marker_kind == "TREND_BEAR"
    # Legacy weak-triangle helper retained for older saved scanners; it is not displayed by the Pine v5 default script.
    bull_triangle_weak = (macd_line.shift(1) <= 0) & (macd_line > 0) & is_trending & (~is_strong_hist.fillna(False)) & (marker_kind == "")
    bear_triangle_weak = (macd_line.shift(1) >= 0) & (macd_line < 0) & is_trending & (~is_strong_hist.fillna(False)) & (marker_kind == "")
    bull_circle = (hist.shift(1) <= 0) & (hist > 0) & is_trending
    bear_circle = (hist.shift(1) >= 0) & (hist < 0) & is_trending
    bull_diamond = is_strong_hist & (hist > 0) & is_trending
    bear_diamond = is_strong_hist & (hist < 0) & is_trending

    adx_rising = adx_val > adx_val.shift(1)
    hist_growing = hist_abs > hist_abs.shift(1)
    slope_norm = (ema_slope.abs() * 100.0).clip(upper=25.0)
    adx_component = ((adx_val - (adx_thr - 5.0)) / 15.0 * 35.0).clip(lower=0.0, upper=35.0)
    slope_component = (slope_norm / 25.0 * 25.0).clip(lower=0.0, upper=25.0)
    hist_component = ((hist_abs / hist_thresh.replace(0, pd.NA)).clip(upper=2.0).fillna(0.0) / 2.0 * 25.0)
    accel_component = (adx_rising.astype(float) * 7.5) + (hist_growing.astype(float) * 7.5)
    regime_score = (adx_component + slope_component + hist_component + accel_component).clip(lower=0.0, upper=100.0)

    return {
        "tf": tf,
        "params": params,
        "close": close,
        "adx": adx_val,
        "adx_threshold": adx_thr,
        "adx_rising": adx_rising,
        "trending": is_trending,
        "ema_slope": ema_slope,
        "rsi": rsi_val,
        "rsi_ema": rsi_ema_val,
        "rsidiff": rsi_diff,
        "hist": hist,
        "hist_threshold": hist_thresh,
        "hist_growing": hist_growing,
        "macd": macd_line,
        "signal": signal_line,
        "strong_hist": is_strong_hist,
        "regime": regime,
        "score": regime_score,
        "bull_triangle_strong": bull_triangle_strong,
        "bear_triangle_strong": bear_triangle_strong,
        "bull_triangle_weak": bull_triangle_weak,
        "bear_triangle_weak": bear_triangle_weak,
        "bull_circle": bull_circle,
        "bear_circle": bear_circle,
        "bull_diamond": bull_diamond,
        "bear_diamond": bear_diamond,
        "bull_fade_arrow": fade_buy,
        "bear_fade_arrow": fade_sell,
        "marker_kind": marker_kind,
    }


def _uae_series_value(bundle: Optional[Dict[str, Any]], key: str, shift: int = 0) -> Any:
    if not bundle or key not in bundle:
        return None
    s = bundle.get(key)
    if isinstance(s, pd.Series):
        idx = len(s) - 1 - max(0, int(shift or 0))
        if idx < 0 or idx >= len(s):
            return None
        val = s.iloc[idx]
        if pd.isna(val):
            return None
        if isinstance(val, (bool, str)):
            return val
        try:
            return float(val)
        except Exception:
            return val
    return s


def _uae_signal_date(bundle: Optional[Dict[str, Any]], shift: int = 0) -> Optional[str]:
    try:
        if not bundle:
            return None
        close = bundle.get("close")
        if not isinstance(close, pd.Series) or close.empty:
            return None
        idx = len(close) - 1 - max(0, int(shift or 0))
        if idx < 0 or idx >= len(close):
            return None
        ts = pd.Timestamp(close.index[idx])
        if pd.isna(ts):
            return None
        return ts.date().isoformat()
    except Exception:
        return None


def _uae_bool_value(bundle: Optional[Dict[str, Any]], key: str, shift: int = 0) -> bool:
    return bool(_uae_series_value(bundle, key, shift=shift))


def _uae_latest_bar_confirm_offset(bundle: Optional[Dict[str, Any]], tf: str, confirmed: bool = True) -> int:
    """Return 1 when a higher-timeframe bar is probably still forming.

    TradingView's UAE script can repaint on an unfinished weekly/monthly bar. For scanner
    queries, weekly/monthly marker primitives default to confirmed bars unless the user
    passes "live"/"current" as the mode argument.
    """
    if not confirmed or not bundle:
        return 0
    tf = _normalize_tf(tf)
    if tf not in {"1w", "1m", "3m"}:
        return 0
    idx = bundle.get("close").index if isinstance(bundle.get("close"), pd.Series) else None
    if idx is None or len(idx) == 0:
        return 0
    try:
        last_ts = pd.Timestamp(idx[-1])
        if last_ts.tzinfo is not None:
            now_ts = pd.Timestamp.now(tz=last_ts.tzinfo)
        else:
            now_ts = pd.Timestamp.now()
        last_day = last_ts.date()
        today = now_ts.date()
        if tf == "1w":
            current_week_start = today - timedelta(days=today.weekday())
            # Yahoo weekly bars are commonly labelled with the week start. If the
            # latest bar is the current week and the week has not finished, skip it.
            if last_day >= current_week_start and today.weekday() < 5:
                return 1
        if tf == "1m":
            if last_day.year == today.year and last_day.month == today.month:
                next_month = date(today.year + (today.month // 12), (today.month % 12) + 1, 1)
                if today < next_month - timedelta(days=1):
                    return 1
        if tf == "3m":
            # Same idea as the "1m" check above, over a calendar quarter
            # instead of a calendar month: is the most recent cached bar
            # inside the SAME quarter as today, and that quarter hasn't
            # closed yet? Quarters end at months 3/6/9/12.
            last_q_end_month = ((last_day.month - 1) // 3 + 1) * 3
            today_q_end_month = ((today.month - 1) // 3 + 1) * 3
            if last_day.year == today.year and last_q_end_month == today_q_end_month:
                next_q_month = today_q_end_month + 1
                next_q_year = today.year
                if next_q_month > 12:
                    next_q_month = 1
                    next_q_year += 1
                next_q_start = date(next_q_year, next_q_month, 1)
                if today < next_q_start - timedelta(days=1):
                    return 1
    except Exception:
        return 0
    return 0


def _uae_mode_confirmed(args: List[Node], ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d", stack: Tuple[str, ...] = ()) -> bool:
    confirmed = True
    for arg in args:
        try:
            val = _eval(arg, ctx, shift=shift, tf_default=tf_default, stack=stack)
        except Exception:
            val = None
        raw = str(val).strip().lower() if val is not None else ""
        if raw in {"live", "current", "unconfirmed", "forming", "realtime", "rt", "false", "0"}:
            confirmed = False
        elif raw in {"confirmed", "closed", "complete", "true", "1"}:
            confirmed = True
    return confirmed


def _uae_signal_age(bundle: Optional[Dict[str, Any]], key: str, max_bars: int = 20, shift: int = 0, base_offset: int = 0) -> Optional[int]:
    if not bundle or key not in bundle:
        return None
    max_bars = max(1, int(max_bars or 20))
    for age in range(max_bars):
        eff_shift = max(0, int(shift or 0)) + base_offset + age
        if _uae_bool_value(bundle, key, shift=eff_shift):
            return age
    return None


def _uae_marker_age(bundle: Optional[Dict[str, Any]], max_bars: int = 20, shift: int = 0, base_offset: int = 0) -> Optional[int]:
    if not bundle or "marker_kind" not in bundle:
        return None
    max_bars = max(1, int(max_bars or 20))
    for age in range(max_bars):
        eff_shift = max(0, int(shift or 0)) + base_offset + age
        val = _uae_series_value(bundle, "marker_kind", shift=eff_shift)
        if str(val or "").strip():
            return age
    return None


def _uae_marker_value(bundle: Optional[Dict[str, Any]], max_bars: int = 1, shift: int = 0, base_offset: int = 0) -> Optional[str]:
    age = _uae_marker_age(bundle, max_bars=max_bars, shift=shift, base_offset=base_offset)
    if age is None:
        return None
    val = _uae_series_value(bundle, "marker_kind", shift=max(0, int(shift or 0)) + base_offset + age)
    return str(val) if val is not None else None


def _uae_event_age_for_node(
    expr: Node,
    ctx: Dict[str, Any],
    max_bars: int,
    shift: int = 0,
    tf_default: str = "1d",
    stack: Tuple[str, ...] = (),
) -> Tuple[bool, Optional[int], str]:
    """Return exact visible UAE marker age for UAE marker primitives.

    Generic Lookback(expr, N) re-evaluates expr at shifts 0..N-1.  That is
    fine for normal bar series, but UAE marker primitives have extra Pine-style
    rules: one visible marker per bar, fade arrows suppress triangles, and
    weekly/monthly signals default to confirmed bars.  For queries such as
    Lookback(UAETrendTriangle("bull", "1w"), 2), use the precomputed visible
    marker series directly and require age < N.  This prevents a much older
    triangle from leaking through a short lookback window.
    """
    if not isinstance(expr, FuncCallNode):
        return False, None, ""
    fname = expr.name.lower().strip()
    signal_names = {
        "uaetrendtriangle", "uae_trend_triangle", "uaesolidtriangle", "uae_solid_triangle",
        "uaeweaktriangle", "uae_weak_triangle",
        "uaefadearrow", "uae_fade_arrow", "uaemrtarrow", "uae_mrt_arrow", "mrtarrow",
        "uaediamond", "uae_diamond",
        "uaecircle", "uae_circle",
    }
    if fname not in signal_names:
        return False, None, ""
    args2, tf = _split_timeframe_args(list(expr.args), tf_default)
    side = _uae_side(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) if args2 else "bull") or "bull"
    confirmed = _uae_mode_confirmed(args2[1:], ctx, shift=shift, tf_default=tf, stack=stack) if len(args2) > 1 else True
    bundle = _uae_series_bundle(ctx, tf)
    base_offset = _uae_latest_bar_confirm_offset(bundle, tf, confirmed=confirmed)
    if fname in {"uaetrendtriangle", "uae_trend_triangle", "uaesolidtriangle", "uae_solid_triangle"}:
        key = f"{side}_triangle_strong"
        label = f"{tf} {side} trend triangle"
    elif fname in {"uaeweaktriangle", "uae_weak_triangle"}:
        key = f"{side}_triangle_weak"
        label = f"{tf} {side} weak triangle"
    elif fname in {"uaefadearrow", "uae_fade_arrow", "uaemrtarrow", "uae_mrt_arrow", "mrtarrow"}:
        key = f"{side}_fade_arrow"
        label = f"{tf} {side} MRT/fade arrow"
    elif fname in {"uaediamond", "uae_diamond"}:
        key = f"{side}_diamond"
        label = f"{tf} {side} diamond"
    else:
        key = f"{side}_circle"
        label = f"{tf} {side} circle"
    age = _uae_signal_age(bundle, key, max_bars=max_bars, shift=shift, base_offset=base_offset)
    if age is not None:
        dt = _uae_signal_date(bundle, shift=max(0, int(shift or 0)) + base_offset + int(age))
        if dt:
            label = f"{label} on {dt}"
    return True, age, label


def _uae_regime_at(ctx: Dict[str, Any], tf: str = "1d", shift: int = 0) -> Optional[str]:
    val = _uae_series_value(_uae_series_bundle(ctx, tf), "regime", shift=shift)
    return str(val) if val is not None else None


def _uae_regime_score_at(ctx: Dict[str, Any], tf: str = "1d", shift: int = 0) -> Optional[float]:
    val = _uae_series_value(_uae_series_bundle(ctx, tf), "score", shift=shift)
    try:
        return None if val is None else float(val)
    except Exception:
        return None


def _compression_ratio(current: Optional[float], average: Optional[float]) -> Optional[float]:
    if current is None or average in (None, 0):
        return None
    try:
        return float(current) / float(average) * 100.0
    except Exception:
        return None


def _split_timeframe_args(args: List[Node], default_tf: str = "1d") -> Tuple[List[Node], str]:
    cleaned: List[Node] = []
    tf = _normalize_tf(default_tf)
    for arg in args:
        if isinstance(arg, StringNode):
            maybe = _normalize_tf(arg.value)
            if maybe in TIMEFRAMES:
                tf = maybe
                continue
        cleaned.append(arg)
    return cleaned, tf


def _first_numeric_arg(args: List[Node], default: int) -> int:
    for arg in args:
        if isinstance(arg, NumberNode):
            try:
                return max(1, int(float(arg.value)))
            except Exception:
                continue
    return max(1, int(default or 1))


def _numeric_arg(node: Node, ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d", stack: Tuple[str, ...] = (), default: Optional[float] = None) -> Optional[float]:
    try:
        val = _eval(node, ctx, shift=shift, tf_default=tf_default, stack=stack)
    except Exception:
        val = None
    if val is None:
        return default
    try:
        return float(val)
    except Exception:
        return default

def _touch_indices(ctx: Dict[str, Any], level: float, tolerance: float, bars: int, shift: int = 0, tf_default: str = "1d") -> List[int]:
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return []
    high = snap.get("series", {}).get("high")
    low = snap.get("series", {}).get("low")
    if high is None or low is None:
        return []
    tolerance = abs(float(tolerance or 0.0))
    bars = max(1, int(bars or 1))
    out: List[int] = []
    for i in range(min(bars, len(high) - shift)):
        idx = len(high) - 1 - shift - i
        if idx < 0:
            break
        hi = float(high.iloc[idx])
        lo = float(low.iloc[idx])
        band = abs(level) * tolerance if level else tolerance
        if lo <= level + band and hi >= level - band:
            out.append(idx)
    return out


def _volume_at_level(ctx: Dict[str, Any], level: float, tolerance: float, bars: int, shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    vol = snap.get("series", {}).get("volume")
    if vol is None:
        return None
    idxs = _touch_indices(ctx, level, tolerance, bars, shift=shift, tf_default=tf)
    if not idxs:
        return None
    touch_avg = float(pd.Series([float(vol.iloc[i]) for i in idxs]).mean())
    window_start = max(0, len(vol) - 1 - shift - max(1, int(bars or 1)) + 1)
    window = vol.iloc[window_start: len(vol) - shift]
    if window.empty:
        return None
    base_avg = float(window.mean())
    if base_avg <= 0:
        return None
    return (touch_avg / base_avg) * 100.0
def _strong_signal_frames(ctx: Dict[str, Any], tf_default: str = "1d", shift: int = 0):
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    close = snap.get("series", {}).get("close")
    high = snap.get("series", {}).get("high")
    low = snap.get("series", {}).get("low")
    volume = snap.get("series", {}).get("volume")
    if close is None or high is None or low is None or volume is None:
        return None
    close_s = pd.to_numeric(close, errors="coerce")
    high_s = pd.to_numeric(high, errors="coerce")
    low_s = pd.to_numeric(low, errors="coerce")
    volume_s = pd.to_numeric(volume, errors="coerce")
    change_pct = close_s.pct_change() * 100.0
    abs_change = change_pct.abs()
    abs_change_ema = abs_change.ewm(span=60, adjust=False).mean()
    vol_ema = volume_s.ewm(span=20, adjust=False).mean().replace(0, pd.NA)
    rel_volume = volume_s / vol_ema
    strong_score = abs_change * rel_volume
    strong_score_ema = strong_score.ewm(span=60, adjust=False).mean()
    return {
        "tf": tf,
        "close": close_s,
        "high": high_s,
        "low": low_s,
        "volume": volume_s,
        "change_pct": change_pct,
        "abs_change": abs_change,
        "abs_change_ema": abs_change_ema,
        "rel_volume": rel_volume,
        "strong_score": strong_score,
        "strong_score_ema": strong_score_ema,
    }


def _signal_bar_index(ctx: Dict[str, Any], lookback_bars: int, shift: int = 0, tf_default: str = "1d") -> Optional[int]:
    frames = _strong_signal_frames(ctx, tf_default=tf_default, shift=shift)
    if not frames:
        return None
    close = frames["close"]
    volume = frames["volume"]
    abs_change = frames["abs_change"]
    abs_change_ema = frames["abs_change_ema"]
    rel_volume = frames["rel_volume"]
    idx_cur = len(close) - 1 - shift
    if idx_cur < 1:
        return None
    lookback_bars = max(1, int(lookback_bars or 1))
    start = max(1, idx_cur - lookback_bars + 1)
    for idx in range(idx_cur, start - 1, -1):
        try:
            rv = rel_volume.iloc[idx]
            ac = abs_change.iloc[idx]
            ace = abs_change_ema.iloc[idx]
        except Exception:
            continue
        if pd.isna(rv) or pd.isna(ac) or pd.isna(ace):
            continue
        if float(rv) >= 2.0 and float(ac) <= float(ace) * 0.5:
            return idx
    return None


def _level_strength_score(ctx: Dict[str, Any], level: float, direction: str, bars: int = 60, tolerance: float = 0.01, shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    high = snap.get("series", {}).get("high")
    low = snap.get("series", {}).get("low")
    close = snap.get("series", {}).get("close")
    vol = snap.get("series", {}).get("volume")
    if high is None or low is None or close is None or vol is None:
        return None
    bars = max(1, int(bars or 1))
    direction = (direction or "resistance").strip().lower()
    if direction not in {"resistance", "support"}:
        direction = "resistance"
    idxs = _touch_indices(ctx, level, tolerance, bars, shift=shift, tf_default=tf)
    if not idxs:
        return 0.0

    touch_component = min(30.0, len(idxs) * 10.0)
    rej_scores: List[float] = []
    vol_scores: List[float] = []
    comp_scores: List[float] = []

    window_start = max(0, len(close) - 1 - shift - bars + 1)
    window_end = len(close) - shift
    if window_end <= window_start:
        return None
    vol_window = vol.iloc[window_start:window_end]
    base_avg_vol = float(vol_window.mean()) if not vol_window.empty else None
    if base_avg_vol and base_avg_vol > 0:
        touch_vols = [float(vol.iloc[i]) for i in idxs if i < len(vol)]
        if touch_vols:
            vol_ratio = float(pd.Series(touch_vols).mean()) / base_avg_vol
            vol_scores.append(max(0.0, min(20.0, (vol_ratio - 1.0) * 20.0)))

    for i in idxs:
        hi = float(high.iloc[i])
        lo = float(low.iloc[i])
        cl = float(close.iloc[i])
        rng = max(1e-9, hi - lo)
        if direction == "resistance":
            rej = max(0.0, min(1.0, (hi - cl) / rng))
        else:
            rej = max(0.0, min(1.0, (cl - lo) / rng))
        rej_scores.append(rej)

    if rej_scores:
        rejection_component = min(25.0, (sum(rej_scores) / len(rej_scores)) * 25.0)
    else:
        rejection_component = 0.0

    if idxs:
        span = max(idxs) - min(idxs)
        time_component = min(15.0, (span / max(1, bars - 1)) * 15.0)
    else:
        time_component = 0.0

    # current compression on the same timeframe (lower is tighter -> higher score)
    atrc = _eval(FuncCallNode("atrcompression", [NumberNode(14.0)]), ctx, shift=shift, tf_default=tf, stack=())
    rngc = _eval(FuncCallNode("rangecompression", [NumberNode(20.0)]), ctx, shift=shift, tf_default=tf, stack=())
    vdc = _eval(FuncCallNode("volumedryup", [NumberNode(20.0)]), ctx, shift=shift, tf_default=tf, stack=())
    comp_vals = [float(v) for v in (atrc, rngc, vdc) if v is not None]
    if comp_vals:
        comp_avg = sum(comp_vals) / len(comp_vals)
        compression_component = max(0.0, min(10.0, (100.0 - comp_avg) / 10.0))
    else:
        compression_component = 0.0

    return round(touch_component + rejection_component + (vol_scores[0] if vol_scores else 0.0) + time_component + compression_component, 2)


def _failed_break_strength(ctx: Dict[str, Any], level: float, direction: str, bars: int = 60, tolerance: float = 0.01, shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    high = snap.get("series", {}).get("high")
    low = snap.get("series", {}).get("low")
    close = snap.get("series", {}).get("close")
    if high is None or low is None or close is None:
        return None
    bars = max(1, int(bars or 1))
    direction = (direction or "resistance").strip().lower()
    idxs = _touch_indices(ctx, level, tolerance, bars, shift=shift, tf_default=tf)
    if not idxs:
        return 0.0
    curr_idx = len(close) - 1 - shift
    curr_close = float(close.iloc[curr_idx])
    window = slice(max(0, curr_idx - bars + 1), curr_idx + 1)
    if direction in {"resistance", "breakout", "above", "up"}:
        peak = float(high.iloc[window].max())
        if peak <= level or curr_close >= level:
            return 0.0
        failure_dist = max(0.0, peak - curr_close) / max(1e-9, abs(level)) * 100.0
        base = _level_strength_score(ctx, level, "resistance", bars=bars, tolerance=tolerance, shift=shift, tf_default=tf) or 0.0
        return round(min(100.0, base * 0.55 + min(35.0, failure_dist * 1.5)), 2)
    else:
        trough = float(low.iloc[window].min())
        if trough >= level or curr_close <= level:
            return 0.0
        failure_dist = max(0.0, curr_close - trough) / max(1e-9, abs(level)) * 100.0
        base = _level_strength_score(ctx, level, "support", bars=bars, tolerance=tolerance, shift=shift, tf_default=tf_default) or 0.0
        return round(min(100.0, base * 0.55 + min(35.0, failure_dist * 1.5)), 2)

def _parse_lookback_spec(spec: Any, default_tf: str = "1w", default_bars: int = 52) -> Tuple[str, int]:
    tf = _normalize_tf(default_tf)
    bars = max(1, int(default_bars or 1))
    if spec is None:
        return tf, bars
    if isinstance(spec, (int, float)):
        return tf, max(1, int(float(spec)))
    s = str(spec).strip().lower().replace(" ", "")
    if not s:
        return tf, bars
    if s in TIMEFRAMES:
        if s == "1d":
            return s, 252
        if s == "1w":
            return s, 52
        if s == "1m":
            return s, 12
        return s, bars
    m = re.fullmatch(r"(\d+)(d|w|m|y|day|days|week|weeks|mo|mos|month|months|yr|yrs|year|years|wk|wks)", s)
    if m:
        n = max(1, int(m.group(1)))
        unit = m.group(2)
        if unit in {"d", "day", "days"}:
            return "1d", n
        if unit in {"w", "wk", "wks", "week", "weeks"}:
            return "1w", n
        if unit in {"m", "mo", "mos", "month", "months"}:
            return "1m", n
        if unit in {"y", "yr", "yrs", "year", "years"}:
            return "1w", n * 52
    tf2 = _normalize_tf(s)
    if tf2 in TIMEFRAMES:
        if tf2 == "1d":
            return tf2, 252
        if tf2 == "1w":
            return tf2, 52
        if tf2 == "1m":
            return tf2, 12
        return tf2, bars
    return tf, bars


def _window_extremes(ctx: Dict[str, Any], tf: str, bars: int, shift: int = 0) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    tf = _normalize_tf(tf)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None, None, None
    series = snap.get("series", {})
    high = series.get("high")
    low = series.get("low")
    close = series.get("close")
    if high is None or low is None or close is None:
        return None, None, None
    bars = max(1, int(bars or 1))
    end = len(close) - 1 - shift
    start = max(0, end - bars + 1)
    if end < 0 or end < start:
        return None, None, None
    try:
        return float(high.iloc[start:end + 1].max()), float(low.iloc[start:end + 1].min()), float(close.iloc[end])
    except Exception:
        return None, None, None


def _ath_distance_pct(ctx: Dict[str, Any], spec: Any = None, shift: int = 0) -> Optional[float]:
    tf, bars = _parse_lookback_spec(spec, default_tf="1w", default_bars=52)
    hi, _, close = _window_extremes(ctx, tf, bars, shift=shift)
    if hi in (None, 0) or close is None:
        return None
    return abs((close - hi) / abs(hi)) * 100.0


def _atl_distance_pct(ctx: Dict[str, Any], spec: Any = None, shift: int = 0) -> Optional[float]:
    tf, bars = _parse_lookback_spec(spec, default_tf="1w", default_bars=52)
    _, lo, close = _window_extremes(ctx, tf, bars, shift=shift)
    if lo in (None, 0) or close is None:
        return None
    return abs((close - lo) / abs(lo)) * 100.0


def _fib_resistance(ctx: Dict[str, Any], level: float, bars: int = 52, tf: str = "1w", shift: int = 0) -> Optional[float]:
    hi, lo, _ = _window_extremes(ctx, tf, bars, shift=shift)
    if hi is None or lo is None:
        return None
    rng = hi - lo
    if rng <= 0:
        return None
    return round(lo + (rng * float(level)), 2)


def _fib_support(ctx: Dict[str, Any], level: float, bars: int = 52, tf: str = "1w", shift: int = 0) -> Optional[float]:
    hi, lo, _ = _window_extremes(ctx, tf, bars, shift=shift)
    if hi is None or lo is None:
        return None
    rng = hi - lo
    if rng <= 0:
        return None
    return round(hi - (rng * float(level)), 2)


def _is_price_identifier(name: str) -> bool:
    return _normalize_indicator(name) in {
        "close", "open", "high", "low", "volume",
        "rsi3", "rsi14", "ema5", "ema9", "ema13", "ema20", "ema50", "ema200",
        "ema_rsi14_13", "ema_rsi14_90", "rsi_diff_90", "macd", "macd_signal", "macd_hist",
        "relative_strength", "leadership", "mansfield_rs", "rs_rank", "beta", "earn_days", "earn_score",
    }


def _is_option_identifier(name: str) -> bool:
    return _normalize_indicator(name) in {
        "oi", "pcr", "oi_change", "oi_change_pct", "pcr_change", "pcr_change_pct",
    }


_SECTOR_ETF_BY_NAME = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financials": "XLF",
    "Energy": "XLE",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}

_SECTOR_NAME_BY_ETF = {v.upper(): k for k, v in _SECTOR_ETF_BY_NAME.items()}
_SECTOR_COMPARE_ALIASES = {
    "TECH": "XLK",
    "TECHNOLOGY": "XLK",
    "HEALTHCARE": "XLV",
    "HEALTH CARE": "XLV",
    "FINANCIAL": "XLF",
    "FINANCIALS": "XLF",
    "FINANCIAL SERVICES": "XLF",
    "ENERGY": "XLE",
    "CONSUMER DISCRETIONARY": "XLY",
    "DISCRETIONARY": "XLY",
    "CONSUMER CYCLICAL": "XLY",
    "CONSUMER STAPLES": "XLP",
    "STAPLES": "XLP",
    "CONSUMER DEFENSIVE": "XLP",
    "INDUSTRIAL": "XLI",
    "INDUSTRIALS": "XLI",
    "MATERIAL": "XLB",
    "MATERIALS": "XLB",
    "BASIC MATERIALS": "XLB",
    "UTILITY": "XLU",
    "UTILITIES": "XLU",
    "REAL ESTATE": "XLRE",
    "REIT": "XLRE",
    "COMMUNICATION SERVICES": "XLC",
    "COMMUNICATION": "XLC",
    "COMMUNICATIONS": "XLC",
    "TELECOM": "XLC",
}
_SECTOR_COMPARE_ALIASES.update({etf: etf for etf in _SECTOR_NAME_BY_ETF})
_SECTOR_COMPARE_ALIASES.update({name.upper(): etf for name, etf in _SECTOR_ETF_BY_NAME.items()})


def _sector_compare_key(value: Any) -> str:
    """Normalize sector names and ETF symbols so sector="XLC" works."""
    raw = str(value or "").strip().strip("\"'").upper()
    if not raw:
        return ""
    raw = re.sub(r"\s+", " ", raw)
    compact = raw.replace("&", "AND")
    return _SECTOR_COMPARE_ALIASES.get(compact, _SECTOR_COMPARE_ALIASES.get(raw, raw))


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) or isinstance(right, str):
        l_raw = str(left or "").strip()
        r_raw = str(right or "").strip()
        l_sec = _sector_compare_key(l_raw)
        r_sec = _sector_compare_key(r_raw)
        if l_sec and r_sec and (l_sec in _SECTOR_NAME_BY_ETF or r_sec in _SECTOR_NAME_BY_ETF):
            return l_sec == r_sec
        return l_raw.casefold() == r_raw.casefold()
    try:
        return abs(float(left) - float(right)) < 1e-9
    except Exception:
        return left == right


def _clamp_score(val: float, lo: float = 0.0, hi: float = 100.0) -> int:
    try:
        return int(round(max(lo, min(hi, float(val)))))
    except Exception:
        return int(lo)


_watchlist_breadth_cache: Dict[Any, Tuple[float, Any]] = {}
_WATCHLIST_BREADTH_CACHE_TTL = 120.0  # short TTL: same value for every symbol in one scan run, but shouldn't go stale across separate scans


def _resolve_watchlist_symbols_for_breadth(watchlist_ref: str, sector: Optional[str] = None) -> List[str]:
    """Resolve a watchlist name or id to its symbol list, optionally
    filtered to one sector. Accepts either -- "Options Watchlist" or its
    numeric id -- same flexibility as watchlist_id elsewhere in this app.
    """
    con = _conn()
    try:
        wl_id = None
        try:
            wl_id = int(watchlist_ref)
        except (ValueError, TypeError):
            row = con.execute("SELECT id FROM watchlists WHERE lower(name)=lower(?)", (str(watchlist_ref).strip(),)).fetchone()
            if row:
                wl_id = row[0]
        if wl_id is None:
            return []
        symbols = [r[0] for r in con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=?", (wl_id,)
        ).fetchall()]
        if sector and symbols:
            placeholders = ",".join("?" for _ in symbols)
            sector_rows = con.execute(
                f"SELECT symbol FROM sector_cache WHERE symbol IN ({placeholders}) AND lower(sector)=lower(?)",
                symbols + [str(sector).strip()],
            ).fetchall()
            sector_set = {r[0] for r in sector_rows}
            symbols = [s for s in symbols if s in sector_set]
        return symbols
    finally:
        con.close()


def _compute_watchlist_breadth(watchlist_ref: str, ma_period: int = 20, sector: Optional[str] = None) -> Optional[float]:
    """% of a watchlist's symbols (optionally filtered to one sector)
    trading above their own N-day moving average. Same methodology as
    the Weekly Plan's breadth section, generalized here to accept any
    watchlist/sector combination as a scanner-callable primitive rather
    than being fixed to whatever watchlist a weekly plan run picked.
    """
    cache_key = ("breadth", str(watchlist_ref).lower(), ma_period, str(sector or "").lower())
    now = time.time()
    cached = _watchlist_breadth_cache.get(cache_key)
    if cached and (now - cached[0]) < _WATCHLIST_BREADTH_CACHE_TTL:
        return cached[1]

    symbols = _resolve_watchlist_symbols_for_breadth(watchlist_ref, sector)
    result = None
    if symbols:
        con = _conn()
        try:
            above, checked = 0, 0
            for sym in symbols[:300]:
                rows = con.execute(
                    "SELECT close FROM price_cache WHERE symbol=? ORDER BY date DESC LIMIT ?",
                    (sym, ma_period),
                ).fetchall()
                if len(rows) < ma_period:
                    continue
                closes = [r[0] for r in rows if r[0] is not None]
                if len(closes) < ma_period:
                    continue
                ma = sum(closes) / len(closes)
                checked += 1
                if closes[0] >= ma:
                    above += 1
            if checked > 0:
                result = round(above / checked * 100, 2)
        finally:
            con.close()
    _watchlist_breadth_cache[cache_key] = (now, result)
    return result


def _compute_watchlist_advance_decline(watchlist_ref: str, sector: Optional[str] = None) -> Optional[float]:
    """Advance/decline ratio for a watchlist (optionally filtered to one
    sector): count of symbols whose latest close is above the prior
    close, divided by the count below. >1 means more advancers than
    decliners; a large or infinite ratio (returned as the advancer
    count itself when there are zero decliners) means broad one-sided
    strength, not just a strong index level.
    """
    cache_key = ("ad", str(watchlist_ref).lower(), str(sector or "").lower())
    now = time.time()
    cached = _watchlist_breadth_cache.get(cache_key)
    if cached and (now - cached[0]) < _WATCHLIST_BREADTH_CACHE_TTL:
        return cached[1]

    symbols = _resolve_watchlist_symbols_for_breadth(watchlist_ref, sector)
    result = None
    if symbols:
        con = _conn()
        try:
            advancing, declining = 0, 0
            for sym in symbols[:300]:
                rows = con.execute(
                    "SELECT close FROM price_cache WHERE symbol=? ORDER BY date DESC LIMIT 2",
                    (sym,),
                ).fetchall()
                if len(rows) < 2:
                    continue
                today, prior = rows[0][0], rows[1][0]
                if today is None or prior is None:
                    continue
                if today > prior:
                    advancing += 1
                elif today < prior:
                    declining += 1
            if advancing or declining:
                result = round(advancing / declining, 2) if declining > 0 else float(advancing)
        finally:
            con.close()
    _watchlist_breadth_cache[cache_key] = (now, result)
    return result


@lru_cache(maxsize=4096)
def _symbol_sector(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return "Other"
    try:
        con = _conn()
        try:
            row = con.execute("SELECT sector FROM sector_cache WHERE symbol=?", (sym,)).fetchone()
        finally:
            con.close()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    try:
        from ..services.sector_service import get_symbol_sector
        return str(get_symbol_sector(sym) or "Other")
    except Exception:
        return "Other"


def _sector_etf_for_symbol(symbol: str) -> Optional[str]:
    sector = _symbol_sector(symbol)
    direct = _SECTOR_ETF_BY_NAME.get(sector)
    if direct:
        return direct
    mapped = _sector_compare_key(sector)
    return mapped if mapped in _SECTOR_NAME_BY_ETF else None


def _trend_alignment_score(close: Optional[pd.Series], bullish: bool = True, shift: int = 0) -> Optional[int]:
    if close is None or len(close) <= max(shift, 1):
        return None
    s = pd.to_numeric(close, errors="coerce").dropna()
    if len(s) < 5:
        return None
    ema20 = s.ewm(span=20, adjust=False).mean()
    ema50 = s.ewm(span=50, adjust=False).mean()
    rsi = _rsi(s, 14)
    rsi_diff = rsi - _ema(rsi, 90)
    idx = len(s) - 1 - shift
    if idx < 0 or idx >= len(s):
        return None
    cur = float(s.iloc[idx])
    e20 = float(ema20.iloc[idx])
    e50 = float(ema50.iloc[idx])
    rd = float(rsi_diff.iloc[idx]) if idx < len(rsi_diff) else 0.0
    score = 0.0
    if bullish:
        if cur > e20 > e50:
            score += 10.0
        elif cur > e20 or e20 > e50:
            score += 6.0
        if rd > 10:
            score += 8.0
        elif rd > 0:
            score += 5.0
    else:
        if cur < e20 < e50:
            score += 10.0
        elif cur < e20 or e20 < e50:
            score += 6.0
        if rd < -10:
            score += 8.0
        elif rd < 0:
            score += 5.0
    return _clamp_score(score, 0, 20)


def _benchmark_regime_score(ctx: Dict[str, Any], side: str, tf: str = "1d", shift: int = 0) -> Optional[int]:
    tf = _normalize_tf(tf)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    close = snap.get("series", {}).get("close")
    if close is None:
        return None
    side = (side or "bull").strip().lower()
    bullish = not side.startswith("bear")
    return _trend_alignment_score(close, bullish=bullish, shift=shift)


def _sector_regime_score(symbol: str, benchmark: str, side: str, tf: str = "1d", shift: int = 0) -> Optional[int]:
    etf = _sector_etf_for_symbol(symbol)
    if not etf:
        return 10
    tf = _normalize_tf(tf)
    sec_df = _history(etf, tf)
    if sec_df is None or sec_df.empty:
        return None
    sec_close = sec_df["Close"].astype(float)
    side = (side or "bull").strip().lower()
    bullish = not side.startswith("bear")
    trend = _trend_alignment_score(sec_close, bullish=bullish, shift=shift)
    if trend is None:
        return None
    # relative strength of the sector ETF versus benchmark over the same lookback window
    try:
        rs_period = 20
        sec_ret = _period_return_pct(sec_close, rs_period, shift=shift)
        bench_df = _history(benchmark, tf)
        bench_ret = _period_return_pct(bench_df["Close"].astype(float), rs_period, shift=shift) if bench_df is not None and not bench_df.empty else None
        rs_bonus = 0
        if sec_ret is not None and bench_ret is not None:
            delta = sec_ret - bench_ret
            if bullish:
                rs_bonus = 0 if delta < -2 else 2 if delta < 2 else 5 if delta < 6 else 8
            else:
                rs_bonus = 0 if delta > 2 else 2 if delta > -2 else 5 if delta > -6 else 8
        return _clamp_score(trend * 0.7 + rs_bonus, 0, 20)
    except Exception:
        return trend


def _oi_pcr_conviction_score(ctx: Dict[str, Any], side: str, lookback: int = 20) -> Optional[int]:
    hist = list(ctx.get("options_history") or [])
    if len(hist) < 2:
        return 10
    side = (side or "bull").strip().lower()
    bullish = not side.startswith("bear")
    lookback = max(2, int(lookback or 20))
    window = hist[-lookback:]
    first = window[0]
    last = window[-1]
    put_delta = float(last.get("put_oi") or 0) - float(first.get("put_oi") or 0)
    call_delta = float(last.get("call_oi") or 0) - float(first.get("call_oi") or 0)
    total_delta = float(last.get("total_oi") or 0) - float(first.get("total_oi") or 0)
    pcr_first = first.get("pcr")
    pcr_last = last.get("pcr")
    pcr_delta = None
    if pcr_first is not None and pcr_last is not None:
        pcr_delta = float(pcr_last) - float(pcr_first)

    dir_score = 0.0
    flow_score = 0.0
    activity_score = 0.0

    if bullish:
        if call_delta > 0:
            dir_score += 8.0
        if call_delta > put_delta:
            dir_score += 5.0
        if put_delta < 0:
            dir_score += 2.0
        if pcr_delta is not None:
            if pcr_delta < 0:
                flow_score += 8.0
            elif pcr_delta < 0.15:
                flow_score += 4.0
        if total_delta > 0:
            activity_score += 5.0
    else:
        if put_delta > 0:
            dir_score += 8.0
        if put_delta > call_delta:
            dir_score += 5.0
        if call_delta < 0:
            dir_score += 2.0
        if pcr_delta is not None:
            if pcr_delta > 0:
                flow_score += 8.0
            elif pcr_delta > -0.15:
                flow_score += 4.0
        if total_delta > 0:
            activity_score += 5.0

    # Normalize versus the move magnitude so large contractions don't dominate.
    denom = max(1.0, abs(float(first.get("total_oi") or 1.0)))
    pct_move = abs(total_delta) / denom * 100.0
    if pct_move > 10:
        activity_score += 2.0
    if pct_move > 25:
        activity_score += 2.0

    score = dir_score + flow_score + activity_score
    return _clamp_score(score, 0, 25)


def _relative_strength_conviction_score(ctx: Dict[str, Any], side: str, tf: str = "1d", shift: int = 0) -> Optional[int]:
    side = (side or "bull").strip().lower()
    bullish = not side.startswith("bear")
    tf = _normalize_tf(tf)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    close = snap.get("series", {}).get("close")
    if close is None:
        return None
    # Leadership is computed after universe ranking in api/run; use it if present.
    leadership = ctx.get("leadership")
    rs_value = ctx.get("relative_strength")
    score = 0.0
    if leadership is not None:
        lead = float(leadership)
        score += lead * (0.12 if bullish else 0.08)
        score += max(0.0, min(8.0, (lead - 50.0) / 10.0 if bullish else (50.0 - lead) / 10.0))
    elif rs_value is not None:
        rs = float(rs_value)
        score += max(0.0, min(10.0, 10.0 + rs / 10.0 if bullish else 10.0 - rs / 10.0))
    # Add RSI diff / trend confirmation.
    rsi = _rsi(close, 14)
    diff = rsi - _ema(rsi, 90)
    idx = len(diff) - 1 - shift
    if idx >= 0 and idx < len(diff):
        try:
            d = float(diff.iloc[idx])
            if bullish:
                if d > 20:
                    score += 8.0
                elif d > 10:
                    score += 5.0
                elif d > 0:
                    score += 3.0
            else:
                if d < -20:
                    score += 8.0
                elif d < -10:
                    score += 5.0
                elif d < 0:
                    score += 3.0
        except Exception:
            pass
    return _clamp_score(score, 0, 20)


def _conviction_breakdown(ctx: Dict[str, Any], side: str, tf: str = "1d", lookback: int = 20, shift: int = 0) -> Dict[str, Any]:
    symbol = str(ctx.get("symbol") or "").upper()
    benchmark = str(ctx.get("benchmark") or "SPY").strip().upper() or "SPY"
    market = _benchmark_regime_score(ctx, side, tf=tf, shift=shift) or 0
    sector = _sector_regime_score(symbol, benchmark, side, tf=tf, shift=shift) or 0
    oi = _oi_pcr_conviction_score(ctx, side, lookback=lookback) or 0
    rs = _relative_strength_conviction_score(ctx, side, tf=tf, shift=shift) or 0
    side_norm = (side or "bull").strip().lower()
    bullish = not side_norm.startswith("bear")
    flow = 0
    # Flow is intentionally narrower than OI/PCR; use recent PCR change and benchmark confirmation.
    try:
        hist = list(ctx.get("options_history") or [])
        if len(hist) >= 2:
            first = hist[-max(2, lookback)]
            last = hist[-1]
            pcr_first = first.get("pcr")
            pcr_last = last.get("pcr")
            if pcr_first is not None and pcr_last is not None:
                delta = float(pcr_last) - float(pcr_first)
                if bullish:
                    flow = _clamp_score(7.5 - (delta * 8.0), 0, 15)
                else:
                    flow = _clamp_score(7.5 + (delta * 8.0), 0, 15)
            else:
                flow = 8
        else:
            flow = 8
    except Exception:
        flow = 8
    total = _clamp_score((oi * 1.0) + (sector * 1.0) + (market * 1.0) + (rs * 1.0) + (flow * 0.6), 0, 100)
    return {
        "total": total,
        "oi": oi,
        "sector": sector,
        "market": market,
        "relative_strength": rs,
        "flow": flow,
        "side": side_norm,
        "timeframe": tf,
        "lookback": lookback,
    }


def _conviction_score(ctx: Dict[str, Any], side: str, tf: str = "1d", lookback: int = 20, shift: int = 0) -> int:
    return int((_conviction_breakdown(ctx, side, tf=tf, lookback=lookback, shift=shift) or {}).get("total", 0))



def _pattern_strictness_value(raw: Any, default: int = 2) -> int:
    try:
        val = int(float(raw))
    except Exception:
        val = default
    return max(1, min(4, val))


def _pattern_threshold(strictness: int, breakout: bool = False) -> int:
    """
    Strictness scale:
      1 = loose
      2 = normal
      3 = strict
      4 = very strict

    Formation scans use lower thresholds.
    Breakout confirmation scans use higher thresholds.
    """
    strictness = _pattern_strictness_value(strictness, 2)
    formation = {1: 35, 2: 45, 3: 55, 4: 65}
    breakout_map = {1: 50, 2: 60, 3: 70, 4: 80}
    return breakout_map[strictness] if breakout else formation[strictness]


def _pattern_score(
    ctx: Dict[str, Any],
    pattern: str,
    bars: int,
    tf: str = "1d",
    shift: int = 0,
    strictness: int = 2,
) -> Optional[float]:
    """
    Heuristic M/W pattern score (0-100).

    W = double-bottom / bullish reversal with neckline reference.
    M = double-top / bearish reversal with neckline reference.

    Strictness controls how much slack is allowed:
      1 = loose
      2 = normal
      3 = strict
      4 = very strict
    """
    pattern = (pattern or "").strip().upper()
    if pattern not in {"W", "M"}:
        return None

    tf = _normalize_tf(tf)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None

    series = snap.get("series", {})
    high = series.get("high")
    low = series.get("low")
    close = series.get("close")
    volume = series.get("volume")
    if high is None or low is None or close is None:
        return None

    bars = max(6, int(bars or 20))
    strictness = _pattern_strictness_value(strictness, 2)

    cur_idx = len(close) - 1 - shift
    if cur_idx <= 1:
        return None

    # Use the bars immediately before the confirmation candle as the pattern base.
    struct_end = cur_idx - 1
    struct_start = max(0, struct_end - (bars - 2))
    if struct_end <= struct_start:
        return None

    try:
        h = pd.to_numeric(high.iloc[struct_start:struct_end + 1], errors="coerce")
        l = pd.to_numeric(low.iloc[struct_start:struct_end + 1], errors="coerce")
        c = pd.to_numeric(close.iloc[struct_start:struct_end + 1], errors="coerce")
        v = pd.to_numeric(volume.iloc[struct_start:struct_end + 1], errors="coerce") if volume is not None else None
        if h.empty or l.empty or c.empty:
            return None
        if c.isna().all() or h.isna().all() or l.isna().all():
            return None
    except Exception:
        return None

    n = len(c)
    if n < 4:
        return None

    # Split into left / middle / right thirds to approximate the pattern.
    t1 = max(1, n // 3)
    t2 = max(t1 + 1, (2 * n) // 3)
    left_h, left_l = h.iloc[:t1], l.iloc[:t1]
    mid_h, mid_l = h.iloc[t1:t2], l.iloc[t1:t2]
    right_h, right_l = h.iloc[t2:], l.iloc[t2:]

    if left_h.empty or mid_h.empty or right_h.empty:
        return None

    last_close = float(close.iloc[cur_idx])
    last_vol = float(volume.iloc[cur_idx]) if volume is not None and not pd.isna(volume.iloc[cur_idx]) else None
    avg_vol = float(pd.to_numeric(v, errors="coerce").mean()) if v is not None else None

    def _pct_diff(a: float, b: float) -> float:
        base = max(abs(a), abs(b), 1e-9)
        return abs(a - b) / base * 100.0

    # Strictness controls how far apart lows/highs can be and how much breakout confirmation is required.
    twin_tol = {1: 6.0, 2: 4.0, 3: 2.5, 4: 1.5}[strictness]
    breakout_pct = {1: 0.25, 2: 0.5, 3: 0.75, 4: 1.0}[strictness]
    volume_weight = {1: 0.08, 2: 0.10, 3: 0.14, 4: 0.18}[strictness]

    if pattern == "W":
        left_low = float(left_l.min())
        right_low = float(right_l.min())
        neckline = float(mid_h.max())
        if any(pd.isna(x) for x in [left_low, right_low, neckline, last_close]):
            return None

        similarity = max(0.0, 100.0 - (_pct_diff(left_low, right_low) * 6.0))
        if _pct_diff(left_low, right_low) > twin_tol:
            similarity *= 0.75 if strictness <= 2 else 0.6 if strictness == 3 else 0.45

        depth = max(0.0, min(100.0, ((neckline - min(left_low, right_low)) / max(abs(min(left_low, right_low)), 1e-9)) * 100.0))
        breakout = 100.0 if last_close >= neckline * (1.0 + breakout_pct / 100.0) else max(0.0, 100.0 - (_pct_diff(last_close, neckline) * 10.0))
        symmetry = 100.0 - (abs(int(left_l.idxmin()) - int(right_l.idxmin())) / max(1, n) * 100.0)
        vol_score = 50.0
        if avg_vol not in (None, 0) and last_vol is not None:
            vol_score = max(0.0, min(100.0, (last_vol / avg_vol) * 50.0))

        score = (
            (similarity * 0.28) +
            (depth * 0.20) +
            (breakout * 0.34) +
            (symmetry * 0.10) +
            (vol_score * volume_weight)
        )
        return _clamp_score(score, 0, 100)

    left_high = float(left_h.max())
    right_high = float(right_h.max())
    neckline = float(mid_l.min())
    if any(pd.isna(x) for x in [left_high, right_high, neckline, last_close]):
        return None

    similarity = max(0.0, 100.0 - (_pct_diff(left_high, right_high) * 6.0))
    if _pct_diff(left_high, right_high) > twin_tol:
        similarity *= 0.75 if strictness <= 2 else 0.6 if strictness == 3 else 0.45

    depth = max(0.0, min(100.0, ((max(left_high, right_high) - neckline) / max(abs(max(left_high, right_high)), 1e-9)) * 100.0))
    breakout = 100.0 if last_close <= neckline * (1.0 - breakout_pct / 100.0) else max(0.0, 100.0 - (_pct_diff(last_close, neckline) * 10.0))
    symmetry = 100.0 - (abs(int(left_h.idxmax()) - int(right_h.idxmax())) / max(1, n) * 100.0)
    vol_score = 50.0
    if avg_vol not in (None, 0) and last_vol is not None:
        vol_score = max(0.0, min(100.0, (last_vol / avg_vol) * 50.0))

    score = (
        (similarity * 0.28) +
        (depth * 0.20) +
        (breakout * 0.34) +
        (symmetry * 0.10) +
        (vol_score * volume_weight)
    )
    return _clamp_score(score, 0, 100)


def _pattern_breakout(ctx: Dict[str, Any], pattern: str, bars: int, tf: str = "1d", shift: int = 0, strictness: int = 2) -> Optional[bool]:
    score = _pattern_score(ctx, pattern, bars, tf=tf, shift=shift, strictness=strictness)
    if score is None:
        return None
    return bool(score >= _pattern_threshold(strictness, breakout=True))


def _pattern_strength(ctx: Dict[str, Any], pattern: str, bars: int, tf: str = "1d", shift: int = 0, strictness: int = 2) -> Optional[int]:
    score = _pattern_score(ctx, pattern, bars, tf=tf, shift=shift, strictness=strictness)
    return None if score is None else int(round(score))


# ---------------------------------------------------------------------------
# Expression AST / parser
# ---------------------------------------------------------------------------

@dataclass
class Node:
    pass


@dataclass
class NumberNode(Node):
    value: float


@dataclass
class StringNode(Node):
    value: str


@dataclass
class IdentifierNode(Node):
    name: str
    tf: Optional[str] = None


@dataclass
class FuncCallNode(Node):
    name: str
    args: List[Node]


@dataclass
class UnaryNode(Node):
    op: str
    expr: Node


@dataclass
class BinaryNode(Node):
    op: str
    left: Node
    right: Node


@dataclass
class CompareNode(Node):
    op: str
    left: Node
    right: Node


@dataclass
class IndexNode(Node):
    expr: Node
    bars: int


_TOKEN_RE = re.compile(
    r"""
    (?P<SPACE>\s+)
  | (?P<CROSS_ABOVE>crosses\s+above|crossed\s+above|cross\s+above)
  | (?P<CROSS_BELOW>crosses\s+below|crossed\s+below|cross\s+below)
  | (?P<AND>\bAND\b)
  | (?P<OR>\bOR\b)
  | (?P<NOT>\bNOT\b)
  | (?P<GE>>=)
  | (?P<LE><=)
  | (?P<GT>>)
  | (?P<LT><)
  | (?P<PLUS>\+)
  | (?P<MINUS>-)
  | (?P<STAR>\*)
  | (?P<SLASH>/)
  | (?P<EQ>=|\bequals\b)
  | (?P<LPAREN>\()
  | (?P<RPAREN>\))
  | (?P<LBRACK>\[)
  | (?P<RBRACK>\])
  | (?P<COMMA>,)
  | (?P<NUMBER>-?\d+(?:\.\d+)?)
  | (?P<STRING>"[^"]*"|'[^']*')
  | (?P<IDENT>[A-Za-z_][A-Za-z0-9_.:]*)
  | (?P<MISMATCH>.)
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class Token:
    type: str
    value: str


class _Parser:
    def __init__(self, text: str):
        self.tokens = self._tokenize(text)
        self.pos = 0

    def _tokenize(self, text: str) -> List[Token]:
        s = re.sub(r"[\n;]+", " ", (text or "")).strip()
        out: List[Token] = []
        i = 0
        while i < len(s):
            m = _TOKEN_RE.match(s, i)
            if not m:
                raise ValueError(f"Could not parse at: {s[i:i+20]}")
            kind = m.lastgroup or "MISMATCH"
            val = m.group(kind)
            i = m.end()
            if kind == "SPACE":
                continue
            if kind == "MISMATCH":
                raise ValueError(f"Unexpected token: {val}")
            out.append(Token(kind.upper(), val))
        return out

    def peek(self) -> Optional[Token]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def match(self, *types: str) -> Optional[Token]:
        tok = self.peek()
        if tok and tok.type in types:
            self.pos += 1
            return tok
        return None

    def expect(self, *types: str) -> Token:
        tok = self.match(*types)
        if not tok:
            expected = "/".join(types)
            got = self.peek().type if self.peek() else "EOF"
            raise ValueError(f"Expected {expected}, got {got}")
        return tok

    def parse(self) -> Node:
        node = self.parse_or()
        if self.peek() is not None:
            raise ValueError(f"Unexpected token near '{self.peek().value}'")
        return node

    def parse_or(self) -> Node:
        node = self.parse_and()
        while self.match("OR"):
            node = BinaryNode("OR", node, self.parse_and())
        return node

    def parse_and(self) -> Node:
        node = self.parse_not()
        while self.match("AND"):
            node = BinaryNode("AND", node, self.parse_not())
        return node

    def parse_not(self) -> Node:
        if self.match("NOT"):
            return UnaryNode("NOT", self.parse_not())
        return self.parse_compare()

    def parse_compare(self) -> Node:
        node = self.parse_arith()
        tok = self.peek()
        if tok and tok.type in {"GE", "LE", "GT", "LT", "EQ", "CROSS_ABOVE", "CROSS_BELOW"}:
            self.pos += 1
            rhs = self.parse_arith()
            op = {
                "GE": ">=",
                "LE": "<=",
                "GT": ">",
                "LT": "<",
                "EQ": "=",
                "CROSS_ABOVE": "cross_above",
                "CROSS_BELOW": "cross_below",
            }[tok.type]
            return CompareNode(op, node, rhs)
        return node

    def parse_arith(self) -> Node:
        node = self.parse_term()
        while True:
            tok = self.peek()
            if tok and tok.type in {"PLUS", "MINUS"}:
                self.pos += 1
                node = BinaryNode(tok.value, node, self.parse_term())
            else:
                break
        return node

    def parse_term(self) -> Node:
        node = self.parse_factor()
        while True:
            tok = self.peek()
            if tok and tok.type in {"STAR", "SLASH"}:
                self.pos += 1
                node = BinaryNode(tok.value, node, self.parse_factor())
            else:
                break
        return node

    def parse_factor(self) -> Node:
        tok = self.peek()
        if tok and tok.type in {"PLUS", "MINUS"}:
            self.pos += 1
            return UnaryNode(tok.value, self.parse_factor())
        return self.parse_primary()

    def parse_postfix(self, node: Node) -> Node:
        while True:
            tok = self.peek()
            if not tok or tok.type != "LBRACK":
                break
            self.pos += 1
            parts: List[str] = []
            depth = 0
            while True:
                nxt = self.peek()
                if nxt is None:
                    raise ValueError("Unclosed [ ] block")
                if nxt.type == "RBRACK" and depth == 0:
                    self.pos += 1
                    break
                if nxt.type == "LBRACK":
                    depth += 1
                elif nxt.type == "RBRACK" and depth > 0:
                    depth -= 1
                parts.append(nxt.value)
                self.pos += 1
            raw = "".join(parts).strip().strip('"').strip("'")
            # Identifier brackets continue to support timeframe syntax like close[1d].
            if isinstance(node, IdentifierNode):
                tf = _normalize_tf(raw)
                if tf in TIMEFRAMES and not re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
                    node = IdentifierNode(name=node.name, tf=tf)
                    continue
            # Numeric brackets are treated as historical offsets for any primitive/function.
            try:
                bars = int(float(raw))
            except Exception:
                tf = _normalize_tf(raw)
                if isinstance(node, IdentifierNode) and tf in TIMEFRAMES:
                    node = IdentifierNode(name=node.name, tf=tf)
                    continue
                raise ValueError(f"Invalid bracket suffix '[{raw}]'; use a numeric history offset or a timeframe like [1d]")
            node = IndexNode(expr=node, bars=max(0, bars))
        return node

    def parse_primary(self) -> Node:
        tok = self.peek()
        if tok is None:
            raise ValueError("Unexpected end of query")
        if tok.type == "NUMBER":
            self.pos += 1
            nxt = self.peek()
            if nxt and nxt.type == "IDENT" and nxt.value.lower() in {"d", "w", "m", "y", "day", "days", "week", "weeks", "mo", "mos", "month", "months", "yr", "yrs", "year", "years", "wk", "wks"}:
                self.pos += 1
                return StringNode(f"{tok.value}{nxt.value.lower()}")
            return NumberNode(float(tok.value))
        if tok.type == "STRING":
            self.pos += 1
            return StringNode(tok.value[1:-1])
        if tok.type == "LPAREN":
            self.pos += 1
            node = self.parse_or()
            self.expect("RPAREN")
            return self.parse_postfix(node)
        if tok.type == "IDENT":
            name = tok.value
            self.pos += 1
            if self.match("LPAREN"):
                # Special-case scan(...) so scanner names may contain spaces and other
                # non-expression text. We accept scan(Name), scan("Name"), or scan('Name').
                if name.lower() == "scan":
                    if self.match("RPAREN"):
                        return FuncCallNode(name=name, args=[])
                    parts: List[str] = []
                    depth = 0
                    while True:
                        nxt = self.peek()
                        if nxt is None:
                            raise ValueError("Unclosed scan(...) block")
                        if nxt.type == "LPAREN":
                            depth += 1
                        elif nxt.type == "RPAREN":
                            if depth == 0:
                                self.pos += 1
                                break
                            depth -= 1
                        parts.append(nxt.value)
                        self.pos += 1
                    raw = " ".join(parts).strip()
                    raw = raw.strip().strip('"').strip("'")
                    return FuncCallNode(name=name, args=[StringNode(raw)])
                args: List[Node] = []
                if not self.match("RPAREN"):
                    while True:
                        args.append(self.parse_or())
                        if self.match("COMMA"):
                            continue
                        self.expect("RPAREN")
                        break
                return self.parse_postfix(FuncCallNode(name=name, args=args))
            return self.parse_postfix(IdentifierNode(name=name, tf=None))
        raise ValueError(f"Could not parse token: {tok.value}")


# ================================================================
# LET BINDINGS -- lightweight local variables within a single scan
# query, e.g.:
#   let highCls = shift(Highest(close[1w],5),6)
#   shift(Highest(close[1w],5),1) < highCls and CrossAbove(close,highCls)
#
# Design note: this deliberately does NOT introduce a new Node type.
# Each `let NAME = EXPR` binding is parsed with the existing _Parser
# exactly like any other subexpression, then every later reference to
# NAME is replaced with a SHARED reference to that same parsed Node
# object (not a text copy, not a clone). Combined with the identity-
# based memoization added to _eval() above, the bound expression is
# genuinely evaluated once per (shift, timeframe) rather than once per
# occurrence in the query text. Every existing tree-walking helper in
# this file (_node_to_text, _collect_function_periods,
# _required_timeframes, optimize_query, _estimate_node_cost, etc.)
# continues to work completely unmodified, because the final tree is
# built entirely out of the same Node subclasses those helpers already
# understand -- a `let` reference is just an ordinary node that
# happens to be shared by reference from two places in the tree, not
# a special node type those ~20 functions would need to learn about.
#
# Backward compatibility: if a query contains no `let` statement,
# _parse_query takes the exact same code path as before this feature
# existed (see the `if not bindings_raw: return _Parser(text).parse()`
# early-out below) -- zero behavior change for any existing saved scan.
# ================================================================

_LET_RESERVED_NAMES = {
    "close", "open", "high", "low", "volume", "hl2", "hlc3", "ohlc4", "vwap",
    "oi", "pcr", "oi_change", "oi_change_pct", "pcr_change", "pcr_change_pct",
    "beta", "earn_days", "earn_score",
    "spot", "underlying", "short_strike", "long_strike",
    "sell_strike", "buy_strike", "put_sell", "put_buy",
    "call_sell", "call_buy", "short_put", "long_put",
    "short_call", "long_call", "breakeven", "breakeven_lower",
    "breakeven_upper", "lower_breakeven", "upper_breakeven",
    "dte", "days_to_expiry", "entry_price", "entry_net",
    "net_premium", "quantity", "qty", "pnl", "pnl_pct",
    "unrealised_pnl", "unrealized_pnl", "max_profit", "max_loss",
    "risk", "reward", "pnr", "pnr_upper", "distance_to_short",
    "distance_to_long", "distance_to_pnr", "distance_pct_to_short",
    "distance_pct_to_long", "distance_pct_to_pnr",
}

_LET_STMT_RE = re.compile(r'^\s*let\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.*)$', re.IGNORECASE | re.DOTALL)


def _extract_let_bindings(text: str) -> Tuple[List[Tuple[str, str]], str]:
    """Peels off leading `let NAME = EXPR;` (or newline-terminated)
    statements from the front of `text`. Returns (bindings, remaining_body).
    If there are no `let` statements, bindings is [] and remaining_body
    is the original text -- callers should treat that as "parse the
    original text exactly as before", the common case for the ~99% of
    existing queries that don't use this feature.
    """
    bindings: List[Tuple[str, str]] = []
    remaining = text

    while True:
        stripped = remaining.lstrip()
        m = _LET_STMT_RE.match(stripped)
        if not m:
            break
        name = m.group(1)
        rest = m.group(2)
        # Find the statement terminator (';' or newline) at the top
        # nesting level -- not inside parentheses/brackets -- so a
        # `let` RHS with its own nested function calls and commas
        # still terminates at the real end of the statement.
        depth = 0
        end = -1
        for i, ch in enumerate(rest):
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == ";" and depth <= 0:
                end = i
                break
            elif ch == "\n" and depth <= 0:
                end = i
                break
        if end == -1:
            # No terminator found -- nothing left over for a final body
            # expression. _parse_query will raise a clear error for this
            # rather than silently guessing what was meant.
            bindings.append((name, rest))
            remaining = ""
            break
        bindings.append((name, rest[:end]))
        remaining = rest[end + 1:]

    return bindings, remaining


def _substitute_let_refs(node: Node, bindings: Dict[str, Node]) -> Node:
    """Rebuilds `node`, replacing any IdentifierNode whose name matches a
    `let` binding with the SAME (shared, already-parsed) Node object from
    `bindings` -- not a text copy or a deep clone. Sharing the object
    reference is what lets the _eval() memoization evaluate the bound
    expression once instead of once per reference (see block comment
    above `_LET_RESERVED_NAMES`).
    """
    if isinstance(node, IdentifierNode):
        return bindings.get(node.name, node)
    if isinstance(node, (NumberNode, StringNode)):
        return node
    if isinstance(node, IndexNode):
        return IndexNode(expr=_substitute_let_refs(node.expr, bindings), bars=node.bars)
    if isinstance(node, FuncCallNode):
        return FuncCallNode(name=node.name, args=[_substitute_let_refs(a, bindings) for a in node.args])
    if isinstance(node, UnaryNode):
        return UnaryNode(op=node.op, expr=_substitute_let_refs(node.expr, bindings))
    if isinstance(node, BinaryNode):
        return BinaryNode(op=node.op, left=_substitute_let_refs(node.left, bindings), right=_substitute_let_refs(node.right, bindings))
    if isinstance(node, CompareNode):
        return CompareNode(op=node.op, left=_substitute_let_refs(node.left, bindings), right=_substitute_let_refs(node.right, bindings))
    return node


def _strip_line_comments(text: str) -> str:
    """Strips '// ...' to end-of-line comments before parsing, so a whole
    line -- or the tail of a line -- can be commented out of a query
    without deleting it. Quote-aware: scans character by character
    tracking whether it's currently inside a "..." or '...' string,
    rather than a naive regex, so a "//" that legitimately appears inside
    a quoted argument (e.g. a hypothetical timeframe or symbol string
    containing it) isn't mistaken for a comment marker mid-string."""
    out_lines = []
    for line in text.split("\n"):
        result = []
        in_string = None  # None, or the quote character currently open
        i = 0
        while i < len(line):
            ch = line[i]
            if in_string:
                result.append(ch)
                if ch == in_string:
                    in_string = None
                i += 1
                continue
            if ch in ('"', "'"):
                in_string = ch
                result.append(ch)
                i += 1
                continue
            if ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                break  # rest of this line is a comment -- stop here
            result.append(ch)
            i += 1
        out_lines.append("".join(result))
    return "\n".join(out_lines)


def _parse_query(query_text: str) -> Node:
    text = str(query_text or "").replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'").strip()
    text = _strip_line_comments(text).strip()
    if not text:
        raise ValueError("Query is empty")
    bindings_raw, body_text = _extract_let_bindings(text)
    if not bindings_raw:
        return _Parser(text).parse()

    bindings: Dict[str, Node] = {}
    for raw_name, rhs_text in bindings_raw:
        lname = raw_name.strip()
        if not rhs_text.strip():
            raise ValueError(f"'let {lname} = ...' is missing a value")
        if lname.lower() in _LET_RESERVED_NAMES:
            raise ValueError(f"'let {lname}' conflicts with a built-in identifier -- choose a different name")
        if lname in bindings:
            raise ValueError(f"'let {lname}' is defined more than once")
        rhs_node = _Parser(rhs_text.strip()).parse()
        # Substitute against bindings defined so far -- supports chained
        # lets (a later `let` can reference an earlier one).
        rhs_node = _substitute_let_refs(rhs_node, bindings)
        bindings[lname] = rhs_node

    if not body_text.strip():
        raise ValueError("Query is empty after 'let' bindings -- add a final expression to evaluate")
    body_node = _Parser(body_text.strip()).parse()
    return _substitute_let_refs(body_node, bindings)


def _node_to_text(node: Node) -> str:
    if isinstance(node, NumberNode):
        return str(int(node.value)) if float(node.value).is_integer() else str(node.value)
    if isinstance(node, StringNode):
        return f'"{node.value}"'
    if isinstance(node, IdentifierNode):
        return f"{node.name}[{node.tf}]" if node.tf else node.name
    if isinstance(node, IndexNode):
        return f"{_node_to_text(node.expr)}[{node.bars}]"
    if isinstance(node, FuncCallNode):
        return f"{node.name}({', '.join(_node_to_text(a) for a in node.args)})"
    if isinstance(node, UnaryNode):
        return f"{node.op}{_node_to_text(node.expr)}"
    if isinstance(node, BinaryNode):
        return f"({_node_to_text(node.left)} {node.op} {_node_to_text(node.right)})"
    if isinstance(node, CompareNode):
        op = "crosses above" if node.op == "cross_above" else "crosses below" if node.op == "cross_below" else node.op
        return f"{_node_to_text(node.left)} {op} {_node_to_text(node.right)}"
    return str(node)


def _flatten_atoms(node: Node) -> List[str]:
    if isinstance(node, BinaryNode) and node.op in {"AND", "OR"}:
        return _flatten_atoms(node.left) + _flatten_atoms(node.right)
    return [_node_to_text(node)]


def _collect_function_periods(node: Node, fnames: set[str]) -> set[int]:
    out: set[int] = set()

    def walk(n: Node):
        if isinstance(n, FuncCallNode):
            if n.name.lower().strip() in fnames:
                out.add(_first_numeric_arg(list(n.args), 252))
            for a in n.args:
                walk(a)
        elif isinstance(n, UnaryNode):
            walk(n.expr)
        elif isinstance(n, (BinaryNode, CompareNode)):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, IndexNode):
            walk(n.expr)

    walk(node)
    return out


def _required_timeframes(node: Node) -> List[str]:
    tfs = {"1d"}

    def walk(n: Node):
        if isinstance(n, IdentifierNode):
            if n.tf:
                tfs.add(_normalize_tf(n.tf))
            elif _is_price_identifier(n.name):
                tfs.add("1d")
        elif isinstance(n, StringNode):
            maybe = _normalize_tf(n.value)
            if maybe in TIMEFRAMES:
                tfs.add(maybe)
        elif isinstance(n, FuncCallNode):
            fname = n.name.lower().strip()
            if fname == "scan" and n.args:
                ref = _scanner_ref_name(n.args[0])
                try:
                    walk(_load_scanner_ast(ref, stack=()))
                except Exception:
                    pass
            if fname in {"uaehighertfaligned", "uae_higher_tf_aligned"} and len(n.args) >= 2:
                arg = n.args[1]
                if isinstance(arg, StringNode):
                    entry_tf = _normalize_tf(arg.value)
                    if entry_tf in TIMEFRAMES:
                        tfs.add(entry_tf)
                        tfs.add(_uae_tf_higher(entry_tf))
            if fname in {"uaemultitfaligned", "uae_multi_tf_aligned"} and len(n.args) >= 2:
                arg = n.args[1]
                if isinstance(arg, StringNode):
                    entry_tf = _normalize_tf(arg.value)
                    if entry_tf in TIMEFRAMES:
                        tfs.add(entry_tf)
                        for htf in _uae_tf_stack(entry_tf):
                            tfs.add(htf)
            for a in n.args:
                walk(a)
        elif isinstance(n, (UnaryNode, BinaryNode, CompareNode)):
            walk(n.expr if isinstance(n, UnaryNode) else n.left)
            walk(n.right if isinstance(n, (BinaryNode, CompareNode)) else n.expr)
        elif isinstance(n, IndexNode):
            walk(n.expr)

    walk(node)
    return [tf for tf in TIMEFRAMES if tf in tfs]


# ---------------------------------------------------------------------------
# Watchlists / scanners / option helpers
# ---------------------------------------------------------------------------

def _watchlists() -> List[Dict[str, Any]]:
    _ensure_watchlist_tables()
    con = _conn()
    try:
        rows = con.execute(
            """
            SELECT w.id, w.name, w.description, w.fetch_options_oi,
                   COALESCE(w.is_default,0) AS is_default,
                   COUNT(ws.id) AS symbol_count
            FROM watchlists w
            LEFT JOIN watchlist_symbols ws ON ws.watchlist_id = w.id
            GROUP BY w.id
            ORDER BY w.is_default DESC, w.name
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _preferred_watchlist_id() -> Optional[int]:
    _ensure_watchlist_tables()
    con = _conn()
    try:
        row = con.execute(
            """
            SELECT w.id
            FROM watchlists w
            LEFT JOIN watchlist_symbols ws ON ws.watchlist_id = w.id
            GROUP BY w.id
            ORDER BY COALESCE(w.is_default, 0) DESC, COUNT(ws.id) DESC, LOWER(w.name)
            LIMIT 1
            """
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    finally:
        con.close()


def _watchlist_symbols(watchlist_id: Optional[int] = None) -> List[str]:
    _ensure_watchlist_tables()
    con = _conn()
    try:
        target_id = watchlist_id if watchlist_id else _preferred_watchlist_id()
        if target_id:
            rows = con.execute(
                "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                (target_id,),
            ).fetchall()
            if not rows:
                rows = con.execute("SELECT DISTINCT symbol FROM watchlist_symbols ORDER BY symbol").fetchall()
        else:
            rows = con.execute("SELECT DISTINCT symbol FROM watchlist_symbols ORDER BY symbol").fetchall()
            if not rows:
                rows = con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()
        return [r[0].upper() for r in rows if r and r[0]]
    finally:
        con.close()


@lru_cache(maxsize=128)
def _scanner_query_text(name: str) -> Optional[str]:
    key = (name or "").strip().lower()
    con = _conn()
    try:
        row = con.execute("SELECT query_text FROM scanner_definitions WHERE lower(name)=lower(?)", (name,)).fetchone()
        if row and row[0]:
            return row[0]
    finally:
        con.close()
    return BUILTIN_SCANNER_QUERY_MAP.get(key)


def _scanner_ref_name(arg: Node) -> str:
    if isinstance(arg, StringNode):
        return arg.value.strip()
    if isinstance(arg, IdentifierNode):
        return arg.name.strip()
    if isinstance(arg, NumberNode):
        return str(int(arg.value)) if arg.value.is_integer() else str(arg.value)
    return _node_to_text(arg).strip().strip('"').strip("'")


def _load_scanner_ast(name: str, stack: Tuple[str, ...] = ()) -> Node:
    ref = (name or "").strip()
    if not ref:
        raise ValueError("scan() requires a scanner name")
    if ref.lower() in {s.lower() for s in stack}:
        raise ValueError(f"Circular scanner reference detected: {' -> '.join(stack + (ref,))}")
    query = _scanner_query_text(ref)
    if not query:
        raise ValueError(f"Unknown scanner referenced in scan(): {ref}")
    node = _parse_query(query)
    return _expand_scan_nodes(node, stack + (ref,))


def _expand_scan_nodes(node: Node, stack: Tuple[str, ...]) -> Node:
    if isinstance(node, FuncCallNode) and node.name.lower() == "scan" and node.args:
        ref = _scanner_ref_name(node.args[0])
        return _expand_scan_nodes(_load_scanner_ast(ref, stack=stack), stack)
    if isinstance(node, FuncCallNode):
        return FuncCallNode(node.name, [_expand_scan_nodes(a, stack) for a in node.args])
    if isinstance(node, UnaryNode):
        return UnaryNode(node.op, _expand_scan_nodes(node.expr, stack))
    if isinstance(node, BinaryNode):
        return BinaryNode(node.op, _expand_scan_nodes(node.left, stack), _expand_scan_nodes(node.right, stack))
    if isinstance(node, CompareNode):
        return CompareNode(node.op, _expand_scan_nodes(node.left, stack), _expand_scan_nodes(node.right, stack))
    return node


def _symbol_ctx(symbol: str, benchmark: str, required_tfs: List[str]) -> Dict[str, Any]:
    tfs = list(dict.fromkeys([_normalize_tf(tf) for tf in required_tfs if tf]))
    if "1d" not in tfs:
        tfs.append("1d")
    timeframes: Dict[str, Any] = {}
    bench_last_1d = None
    for tf in tfs:
        df = _history(symbol, tf)
        if df is None or len(df) < 25:
            raise ValueError(f"insufficient {tf} history")
        bench_df = _history(benchmark, tf)
        if bench_df is None or len(bench_df) < 25:
            bench_df = None
        # Bar-count alone doesn't catch STALE data -- a symbol can have
        # plenty of historical bars while its most RECENT one is days or
        # weeks old (a data-refresh gap, not a missing-history gap), and
        # this loop had nothing checking that. shift=0 for that symbol
        # then silently becomes ITS OWN stale last bar, not real "today"
        # -- so Lookback(expr, N) ends up checking N bars back from a
        # stale reference point, which can be well over N real trading
        # days in the past. Only checked for "1d" -- weekly/monthly
        # naturally have sparser expected cadence where a few days' lag
        # is normal, not a sign of a refresh problem.
        if tf == "1d" and bench_df is not None and df is not None and len(df) and len(bench_df):
            try:
                sym_last = pd.Timestamp(df.index[-1]).normalize()
                bench_last_1d = pd.Timestamp(bench_df.index[-1]).normalize()
                lag_days = (bench_last_1d - sym_last).days
                if lag_days > 3:
                    raise ValueError(f"stale 1d history ({lag_days}d behind {benchmark}, last bar {sym_last.date()})")
            except ValueError:
                raise
            except Exception:
                pass  # date parsing issue on either side -- don't block the scan over a diagnostic-only check
        timeframes[tf] = _prepare_snapshot_cached(df, bench_df, tf, symbol=symbol)

    base = timeframes["1d"]["series"]
    sector_name = _symbol_sector(symbol)
    sector_etf = _sector_etf_for_symbol(symbol)
    ctx = {
        "symbol": symbol,
        "benchmark": benchmark,
        "timeframes": timeframes,
        "options_history": list(_options_history(symbol)),
        "leadership": None,
        "beta": get_beta(symbol),
        "sector": sector_name,
        "sector_name": sector_name,
        "sector_etf": sector_etf,
    }
    earn = get_earnings_info(symbol) or {}
    ctx["earn_days"] = earn.get("earn_days")
    ctx["earn_score"] = earn.get("earn_score")
    ctx["earn_date"] = earn.get("earn_date")
    ctx["next_earn_date"] = earn.get("next_earn_date")
    ctx["last_earn_date"] = earn.get("last_earn_date")
    for key in ("close", "open", "high", "low", "volume", "rsi3", "rsi14", "ema5", "ema9", "ema13", "ema20", "ema50", "ema200", "ema_rsi14_13", "ema_rsi14_90", "rsi_diff_90", "macd", "macd_signal", "macd_hist", "relative_strength"):
        series = base.get(key)
        prev, now = _series_latest(series) if series is not None else (None, None)
        ctx[key] = now
        ctx[f"{key}_prev"] = prev
    ctx["price"] = ctx.get("close")
    ctx["sector_rs"] = _sector_rs_value(ctx, 20, tf="1d", shift=0)
    if ctx.get("rsi14") is not None and ctx.get("ema_rsi14_90") is not None:
        try:
            ctx["rsi_diff_90"] = float(ctx.get("rsi14")) - float(ctx.get("ema_rsi14_90"))
        except Exception:
            ctx["rsi_diff_90"] = None

    flow = _flow_snapshot(ctx, tf="1d")
    ctx.update(flow)
    ctx["iv_rank"] = flow.get("iv_rank")
    ctx["iv_est"] = flow.get("iv_est")
    ctx["iv_change"] = flow.get("iv_change")
    ctx["flow_score"] = flow.get("flow_score")
    ctx["flow_bias"] = flow.get("flow_bias")
    ctx["flow_classification"] = flow.get("flow_classification")
    ctx["pcr_shift"] = flow.get("pcr_shift")
    return ctx


def _series_for_expr(node: Node, ctx: Dict[str, Any], tf_default: str = "1d") -> Optional[pd.Series]:
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if snap is None:
        return None
    series_map = snap.get("series", {})
    if isinstance(node, IdentifierNode):
        name = _normalize_indicator(node.name)
        if name in series_map:
            return series_map[name]
        return None
    return None


_fundamentals_cache = {}
_FUNDAMENTALS_CACHE_TTL = 3600.0  # 1 hour -- fundamentals/analyst data don't move intraday like price does


def _get_fundamentals_from_db(symbol: str):
    """Read-only lookup into fundamentals_snapshot, the persistent cache
    populated by bulk_fetch_fundamentals (wired to the Watchlist
    Manager's "Earnings" button). Returns None if the symbol has never
    been bulk-fetched, in which case the caller falls back to a live
    yfinance fetch. Deliberately does NOT write back here -- keeping
    bulk_fetch_fundamentals as the single writer keeps its smart-refresh
    bookkeeping (last_pull_date, last_known_next_earn_date) consistent
    rather than having two different code paths racing to update it.
    """
    try:
        import sqlite3
        from ..config import DB_PATH as _OIAPP_DB_PATH
        con = sqlite3.connect(_OIAPP_DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM fundamentals_snapshot WHERE symbol=? AND fetch_error IS NULL",
            (symbol,)
        ).fetchone()
        con.close()
        if not row:
            return None
        return {
            "pe_fwd": row["pe_fwd"], "pe_trailing": row["pe_trailing"],
            "profit_margin": row["profit_margin"], "revenue_growth": row["revenue_growth"],
            "earnings_growth": row["earnings_growth"], "rec_mean": row["rec_mean"],
            "analyst_target": row["analyst_target"], "analyst_upside_pct": row["analyst_upside_pct"],
            "eps_revision_trend": row["eps_revision_trend"],
            "eps_revision_chg_pct": row["eps_revision_chg_pct"],
            "eps_revision_net_30d": row["eps_revision_net_30d"],
            "beat_rate_pct": row["beat_rate_pct"] if "beat_rate_pct" in row.keys() else None,
            "avg_eps_surprise_pct": row["avg_eps_surprise_pct"] if "avg_eps_surprise_pct" in row.keys() else None,
            "last_post_earnings_move_pct": row["last_post_earnings_move_pct"] if "last_post_earnings_move_pct" in row.keys() else None,
        }
    except Exception:
        return None


_corporate_events_cache = {}
_CORPORATE_EVENTS_CACHE_TTL = 3600.0  # 1 hour -- this data is only refreshed once a day
                                        # by the watchlist scheduler's Corporate Events
                                        # step anyway, no need for a shorter TTL


def _get_corporate_events_cached(symbol: str) -> dict:
    """One cached DB read per symbol backing InsiderNetDollars()/
    DebtChangePct()/VolumePctOfAvg()/etc below -- same one-fetch-backs-
    many-primitives shape as _get_fundamentals_cached above.

    Deliberately DOES NOT fall back to a live SEC EDGAR fetch on a cache
    miss the way _get_fundamentals_cached falls back to yfinance: a live
    Form 4 fetch is several HTTP round-trips per symbol (submissions +
    one per filing), rate-limited to 10/sec at the source, which would
    make a scan across a real watchlist unusably slow mid-query. This is
    read-only against corporate_events_snapshot, populated by the
    watchlist scheduler's Corporate Events step (oiapp/services/
    sec_edgar.py + scheduled_jobs.py) -- if a symbol has never been
    fetched by that step, these primitives correctly return None rather
    than blocking the query to go fetch it live.
    """
    now = time.time()
    cached = _corporate_events_cache.get(symbol)
    if cached and (now - cached[0]) < _CORPORATE_EVENTS_CACHE_TTL:
        return cached[1]
    out = {}
    try:
        import sqlite3
        from ..config import DB_PATH as _OIAPP_DB_PATH
        con = sqlite3.connect(_OIAPP_DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM corporate_events_snapshot WHERE symbol=? ORDER BY as_of_date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        con.close()
        if row:
            out = dict(row)
    except Exception:
        out = {}
    _corporate_events_cache[symbol] = (now, out)
    return out


def _get_fundamentals_cached(symbol: str) -> dict:
    """One cached fetch per symbol backing every fundamental primitive
    below, so a query referencing several of them (PE, margin, analyst
    upside, revision trend) only hits yfinance once per symbol, not once
    per primitive. Same estimate-revision-trend logic already built and
    tested for the Earnings page's outlook score -- one implementation,
    not two.

    Three-tier lookup: in-memory (this process, 1 hour) -> persistent DB
    cache (fundamentals_snapshot, populated by the Watchlist Manager's
    "Earnings" button via bulk_fetch_fundamentals's smart earnings-gated
    refresh) -> live yfinance fetch as a last resort for a symbol that's
    never been bulk-fetched. The DB tier is what makes a scan across a
    large watchlist fast: reading a row that's already there beats a
    live yfinance round-trip on every single query.
    """
    now = time.time()
    cached = _fundamentals_cache.get(symbol)
    if cached and (now - cached[0]) < _FUNDAMENTALS_CACHE_TTL:
        return cached[1]

    db_hit = _get_fundamentals_from_db(symbol)
    if db_hit is not None:
        _fundamentals_cache[symbol] = (now, db_hit)
        return db_hit

    out = {}
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
    except Exception as e:
        _fundamentals_cache[symbol] = (now, {"_error": str(e)[:80]})
        return _fundamentals_cache[symbol][1]

    try:
        info = tk.info or {}
        out["pe_fwd"] = info.get("forwardPE")
        out["pe_trailing"] = info.get("trailingPE")
        out["profit_margin"] = (info.get("profitMargins") or 0) * 100 if info.get("profitMargins") is not None else None
        out["revenue_growth"] = (info.get("revenueGrowth") or 0) * 100 if info.get("revenueGrowth") is not None else None
        out["earnings_growth"] = (info.get("earningsGrowth") or 0) * 100 if info.get("earningsGrowth") is not None else None
        out["rec_mean"] = info.get("recommendationMean")
        out["analyst_target"] = info.get("targetMeanPrice")
        price_now = info.get("currentPrice") or info.get("regularMarketPrice")
        if out["analyst_target"] and price_now:
            out["analyst_upside_pct"] = round((out["analyst_target"] - price_now) / price_now * 100, 2)
    except Exception as e:
        out["_error"] = str(e)[:80]

    try:
        trend_df = tk.eps_trend
        if trend_df is not None and not trend_df.empty and "0q" in trend_df.index:
            t0 = trend_df.loc["0q"]
            cur = t0.get("current")
            ago30 = t0.get("30daysAgo")
            ago90 = t0.get("90daysAgo")
            ref = ago30 if ago30 not in (None, 0) else ago90
            if cur is not None and ref not in (None, 0):
                chg_pct = round((cur - ref) / abs(ref) * 100, 2)
                out["eps_revision_chg_pct"] = chg_pct
                out["eps_revision_trend"] = "RISING" if chg_pct >= 3 else "FALLING" if chg_pct <= -3 else "STABLE"
    except Exception:
        pass
    try:
        rev_df = tk.eps_revisions
        if rev_df is not None and not rev_df.empty and "0q" in rev_df.index:
            r0 = rev_df.loc["0q"]
            up30 = r0.get("upLast30days") or 0
            down30 = r0.get("downLast30days") or 0
            out["eps_revision_net_30d"] = int(up30 - down30)
    except Exception:
        pass

    # Beat rate / average surprise / most recent post-earnings move --
    # reuses earnings.py's exact eps_history + _post_earnings_moves logic
    # (same beat-rate formula, same day-after-earnings % move calc used
    # on the Earnings page) rather than a second implementation. Uses the
    # lighter safe_earnings_dates() call, not the heavier tk.info fetch
    # already paid for above, and get_history_cached() for price data
    # (already cached elsewhere in the app, not a fresh fetch here).
    try:
        from .earnings import safe_earnings_dates, _post_earnings_moves, _safe as _earn_safe
        ed = safe_earnings_dates(symbol)
        if ed is not None and not ed.empty:
            actuals = ed[ed["Reported EPS"].notna()].copy()
            eps_hist = []
            for idx, erow in actuals.head(8).iterrows():
                try:
                    dt = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                except Exception:
                    dt = str(idx)[:10]
                eps_hist.append({"date": dt, "surprise_pct": _earn_safe(erow.get("Surprise(%)") or erow.get("Surprise (%)"))})
            # Matches _compute_recommendation_model's exact formula (the
            # function that actually produces the 6-factor Earnings-page
            # breakdown this is meant to mirror) -- NOT _rule_based_analysis's
            # slightly different one elsewhere in earnings.py (>0 vs >=0 for
            # a "beat", last-4-only vs all-surprises for the average). Two
            # similar-but-different formulas already existed in that file;
            # this one intentionally matches the one whose score this
            # primitive is standing in for.
            total = len(eps_hist)
            surprises = [h["surprise_pct"] for h in eps_hist if h.get("surprise_pct") is not None]
            if total:
                beats = sum(1 for s in surprises if s >= 0)
                out["beat_rate_pct"] = round(beats / total * 100)
            if surprises:
                out["avg_eps_surprise_pct"] = round(sum(surprises) / len(surprises), 1)
            moves = _post_earnings_moves(symbol, eps_hist)
            if moves:
                out["last_post_earnings_move_pct"] = moves[0]
    except Exception:
        pass

    _fundamentals_cache[symbol] = (now, out)
    return out


# ── Query optimizer ──────────────────────────────────────────────────────
# Purely a performance rewrite: AND/OR are commutative, so reordering
# their operands never changes what a query MATCHES, only how fast it
# gets there. Primitive computation happens lazily during the pass/fail
# _eval() call, not pre-computed upfront for every symbol -- so a symbol
# that fails a cheap, tier-0 condition first (Sector(), EarningsDays())
# never pays for a tier-4 fundamentals fetch or a tier-3 options-wall DB
# query at all. Without this, "RSIdiff90(...)<-20 and Sector()=='Tech'"
# and "Sector()=='Tech' and RSIdiff90(...)<-20" have identical results
# but very different costs, purely based on which order the user
# happened to type them in.
_COST_TIER_CHEAP_LOOKUP = {
    "sector", "sectoretf", "earningsdays", "earnscore", "beta", "symbol",
}
_COST_TIER_INDICATOR = {
    "rsi", "ema", "sma", "macd", "adx", "rsidiff90", "rsidiff90sma",
    "highest", "lowest", "stddev", "average", "atr", "vwap",
    "distancefromresistance", "distancefromsupport", "resistancestrength",
    "supportstrength", "crossabove", "crossover", "crossbelow", "crossunder",
    "isath", "isatl", "athdistance", "atldistance",
}
_COST_TIER_OPTIONS_DB = {
    "oi", "pcr", "oi_change", "oi_change_pct", "pcr_change", "pcr_change_pct",
    "callwallstrike", "putwallstrike", "callwalloi", "putwalloi",
    "distancetocallwall", "distancetoputwall", "insideoiwalls",
    "callwallstrength", "putwallstrength", "callwallbuildup", "putwallbuildup", "totaloi",
}
_COST_TIER_WATCHLIST_AGG = {"watchlistbreadth", "watchlistadvancedecline"}
_COST_TIER_FUNDAMENTALS = {
    "pe", "profitmargin", "revenuegrowthpct", "earningsgrowthpct",
    "analystrec", "analystupside", "epsrevisiontrend", "epsrevisionnet30d",
}
_COST_TIER_CORPORATE_EVENTS = {
    "insidernetdollars", "insiderdollarsbought", "insiderdollarssold",
    "insidertransactioncount", "debtchangepct", "debtchangedollars",
    "debtvalue", "volumepctofavg", "materialeventcount", "hasmaterialevent",
}


def _primitive_cost_tier(fname: str) -> int:
    fname = str(fname or "").lower()
    if fname in _COST_TIER_FUNDAMENTALS:
        return 4  # can trigger a live yfinance fetch if not DB-cached -- the most expensive tier
    if fname in _COST_TIER_CORPORATE_EVENTS:
        return 3  # a fresh DB query, but NEVER a live SEC fetch -- deliberately
                   # capped below the fundamentals tier since _get_corporate_events_cached
                   # has no live-fetch fallback at all (see its own docstring)
    if fname in _COST_TIER_WATCHLIST_AGG:
        return 3
    if fname in _COST_TIER_OPTIONS_DB:
        return 3  # a fresh DB query, even if cached within-symbol
    if fname in _COST_TIER_INDICATOR:
        return 2  # CPU work on already-loaded price series, no extra I/O
    if fname in _COST_TIER_CHEAP_LOOKUP:
        return 0
    return 1  # unknown primitive -- moderate default rather than assuming worst-case


def _estimate_node_cost(node) -> int:
    """Highest cost tier of any primitive referenced within this
    sub-expression -- evaluating the node requires evaluating
    everything inside it regardless of order, so its cost is bounded by
    its single most expensive part."""
    if node is None:
        return 0
    if isinstance(node, FuncCallNode):
        best = _primitive_cost_tier(node.name)
        for a in node.args:
            best = max(best, _estimate_node_cost(a))
        return best
    if isinstance(node, (NumberNode, StringNode, IdentifierNode)):
        return 0
    if isinstance(node, UnaryNode):
        return _estimate_node_cost(node.expr)
    if isinstance(node, IndexNode):
        return _estimate_node_cost(node.expr)
    if isinstance(node, (BinaryNode, CompareNode)):
        return max(_estimate_node_cost(node.left), _estimate_node_cost(node.right))
    return 1


def _flatten_chain(node, op):
    """Flatten a left-associative AND/OR chain into an ordered operand
    list, e.g. ((A and B) and C) -> [A, B, C]."""
    if isinstance(node, BinaryNode) and node.op == op:
        return _flatten_chain(node.left, op) + _flatten_chain(node.right, op)
    return [node]


def _rebuild_chain(operands, op):
    """Inverse of _flatten_chain: rebuilds a left-associative tree from
    an ordered operand list, matching the same shape the parser itself
    produces (so evaluation order is exactly the flattened list order --
    _eval's AND/OR handling always evaluates .left before .right)."""
    node = operands[0]
    for nxt in operands[1:]:
        node = BinaryNode(op, node, nxt)
    return node


def optimize_query(root):
    """Reorders every AND/OR chain (at every level of nesting) so
    cheaper, more selective conditions evaluate first. Never changes
    what a query matches. The ORIGINAL tree should still be used
    anywhere query structure matters to the user (the "reason" text,
    result columns) -- this is purely an internal fast-path for the
    pass/fail check.
    """
    if root is None:
        return root
    if isinstance(root, BinaryNode) and root.op in ("AND", "OR"):
        operands = [optimize_query(o) for o in _flatten_chain(root, root.op)]
        operands.sort(key=_estimate_node_cost)
        return _rebuild_chain(operands, root.op)
    if isinstance(root, UnaryNode):
        return UnaryNode(root.op, optimize_query(root.expr))
    if isinstance(root, IndexNode):
        return IndexNode(optimize_query(root.expr), root.bars)
    if isinstance(root, BinaryNode):
        return BinaryNode(root.op, optimize_query(root.left), optimize_query(root.right))
    if isinstance(root, CompareNode):
        return CompareNode(root.op, optimize_query(root.left), optimize_query(root.right))
    if isinstance(root, FuncCallNode):
        return FuncCallNode(root.name, [optimize_query(a) for a in root.args])
    return root


# ── Candlestick pattern detection ────────────────────────────────────────
# Single/multi-bar OHLC patterns, built from the price data already
# bulk-loaded per symbol (price_cache) -- no new data source needed. Each
# check is a real, precisely-defined pattern (not a fuzzy heuristic),
# taking raw OHLC values for one or more consecutive bars.

def _get_ohlc_bar(ctx: Dict[str, Any], tf_default: str, shift: int, bars_back: int = 0) -> Optional[Dict[str, float]]:
    """One bar's OHLCV, `bars_back` bars further back than `shift` from
    the most recent bar. bars_back=0 is the same bar `shift` already
    points to; bars_back=1 is the bar before that, etc. -- this is what
    lets multi-bar patterns (engulfing, morning star) look at 2-3
    consecutive bars ending at the same reference point every other
    primitive in this file uses.
    """
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    series = snap.get("series", {})
    o, h, l, c = series.get("open"), series.get("high"), series.get("low"), series.get("close")
    v = series.get("volume")
    if o is None or h is None or l is None or c is None:
        return None
    idx = len(c) - 1 - shift - bars_back
    if idx < 0 or idx >= len(c):
        return None
    try:
        return {
            "open": float(o.iloc[idx]), "high": float(h.iloc[idx]),
            "low": float(l.iloc[idx]), "close": float(c.iloc[idx]),
            "volume": float(v.iloc[idx]) if v is not None else None,
        }
    except Exception:
        return None


def _is_doji(bar: dict, body_threshold_pct: float = 10.0) -> bool:
    """Open ~= close -- body is a small fraction of the bar's total range."""
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return False
    body = abs(bar["close"] - bar["open"])
    return (body / rng * 100) <= body_threshold_pct


def _is_hammer(bar: dict) -> bool:
    """Small body near the TOP of the range, long lower wick (>=2x body),
    little/no upper wick. A bullish reversal signal specifically when it
    appears after a downtrend -- that context isn't checked here, it's
    left to the query (e.g. combine with RSI<30 or a recent downtrend
    check) since "after a downtrend" is a judgment this primitive alone
    can't make reliably.
    """
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return False
    body = abs(bar["close"] - bar["open"])
    body_top = max(bar["open"], bar["close"])
    body_bottom = min(bar["open"], bar["close"])
    lower_wick = body_bottom - bar["low"]
    upper_wick = bar["high"] - body_top
    return body > 0 and lower_wick >= body * 2 and upper_wick <= body * 0.5


def _is_shooting_star(bar: dict) -> bool:
    """Small body near the BOTTOM of the range, long upper wick (>=2x
    body), little/no lower wick -- the mirror of a hammer, a bearish
    reversal signal after an uptrend."""
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return False
    body = abs(bar["close"] - bar["open"])
    body_top = max(bar["open"], bar["close"])
    body_bottom = min(bar["open"], bar["close"])
    lower_wick = body_bottom - bar["low"]
    upper_wick = bar["high"] - body_top
    return body > 0 and upper_wick >= body * 2 and lower_wick <= body * 0.5


def _is_bullish_engulfing(prev_bar: dict, curr_bar: dict) -> bool:
    """Prior bar bearish, current bar bullish, and the current bar's
    real body fully engulfs the prior bar's real body."""
    prev_bearish = prev_bar["close"] < prev_bar["open"]
    curr_bullish = curr_bar["close"] > curr_bar["open"]
    return (prev_bearish and curr_bullish
            and curr_bar["open"] <= prev_bar["close"]
            and curr_bar["close"] >= prev_bar["open"])


def _is_bearish_engulfing(prev_bar: dict, curr_bar: dict) -> bool:
    prev_bullish = prev_bar["close"] > prev_bar["open"]
    curr_bearish = curr_bar["close"] < curr_bar["open"]
    return (prev_bullish and curr_bearish
            and curr_bar["open"] >= prev_bar["close"]
            and curr_bar["close"] <= prev_bar["open"])


def _is_bullish_harami(prev_bar: dict, curr_bar: dict) -> bool:
    """Prior candle has a large bearish body; current candle's entire
    body (both open and close) sits INSIDE the prior body -- the
    opposite of engulfing (containment, not overtake). Signals the
    prior strong down-move is losing momentum."""
    if prev_bar["close"] >= prev_bar["open"]:
        return False
    prev_top, prev_bot = prev_bar["open"], prev_bar["close"]
    return prev_bot < curr_bar["open"] < prev_top and prev_bot < curr_bar["close"] < prev_top


def _is_bearish_harami(prev_bar: dict, curr_bar: dict) -> bool:
    """Mirror of bullish harami -- prior bullish, current body fully
    contained within it."""
    if prev_bar["close"] <= prev_bar["open"]:
        return False
    prev_bot, prev_top = prev_bar["open"], prev_bar["close"]
    return prev_bot < curr_bar["open"] < prev_top and prev_bot < curr_bar["close"] < prev_top


def _is_piercing_line(prev_bar: dict, curr_bar: dict) -> bool:
    """Prior bearish, current bullish, opens BELOW prior's low (gap
    down) then closes back up past the midpoint of prior's real body
    -- but not all the way past prior's open, which would make it a
    bullish engulfing instead. Partial penetration, not a full
    overtake -- a genuinely different (weaker but still real)
    reversal signal than engulfing."""
    prev_bearish = prev_bar["close"] < prev_bar["open"]
    curr_bullish = curr_bar["close"] > curr_bar["open"]
    if not (prev_bearish and curr_bullish):
        return False
    prev_mid = (prev_bar["open"] + prev_bar["close"]) / 2
    return (curr_bar["open"] < prev_bar["low"]
            and prev_mid < curr_bar["close"] < prev_bar["open"])


def _is_dark_cloud_cover(prev_bar: dict, curr_bar: dict) -> bool:
    """Mirror of piercing line -- prior bullish, current bearish,
    opens ABOVE prior's high then closes back down past the midpoint
    of prior's body, but not all the way past prior's open (which
    would make it a bearish engulfing instead)."""
    prev_bullish = prev_bar["close"] > prev_bar["open"]
    curr_bearish = curr_bar["close"] < curr_bar["open"]
    if not (prev_bullish and curr_bearish):
        return False
    prev_mid = (prev_bar["open"] + prev_bar["close"]) / 2
    return (curr_bar["open"] > prev_bar["high"]
            and prev_bar["open"] < curr_bar["close"] < prev_mid)


def _is_morning_star(bar1: dict, bar2: dict, bar3: dict) -> bool:
    """3-bar bullish reversal: a large bearish candle, a small-bodied
    "star" bar, then a large bullish candle closing back into the first
    bar's body."""
    rng1 = bar1["high"] - bar1["low"]
    rng2 = bar2["high"] - bar2["low"]
    rng3 = bar3["high"] - bar3["low"]
    bar1_bearish_big = bar1["close"] < bar1["open"] and rng1 > 0 and abs(bar1["close"] - bar1["open"]) > rng1 * 0.5
    bar2_small = rng2 > 0 and abs(bar2["close"] - bar2["open"]) < rng2 * 0.3
    bar3_bullish_big = bar3["close"] > bar3["open"] and rng3 > 0 and abs(bar3["close"] - bar3["open"]) > rng3 * 0.5
    bar3_closes_into_bar1 = bar3["close"] > (bar1["open"] + bar1["close"]) / 2
    return bar1_bearish_big and bar2_small and bar3_bullish_big and bar3_closes_into_bar1


def _is_evening_star(bar1: dict, bar2: dict, bar3: dict) -> bool:
    rng1 = bar1["high"] - bar1["low"]
    rng2 = bar2["high"] - bar2["low"]
    rng3 = bar3["high"] - bar3["low"]
    bar1_bullish_big = bar1["close"] > bar1["open"] and rng1 > 0 and abs(bar1["close"] - bar1["open"]) > rng1 * 0.5
    bar2_small = rng2 > 0 and abs(bar2["close"] - bar2["open"]) < rng2 * 0.3
    bar3_bearish_big = bar3["close"] < bar3["open"] and rng3 > 0 and abs(bar3["close"] - bar3["open"]) > rng3 * 0.5
    bar3_closes_into_bar1 = bar3["close"] < (bar1["open"] + bar1["close"]) / 2
    return bar1_bullish_big and bar2_small and bar3_bearish_big and bar3_closes_into_bar1


def _is_three_white_soldiers(bar1: dict, bar2: dict, bar3: dict) -> bool:
    """3 consecutive bullish candles, each closing higher than the last,
    each opening within the prior bar's real body -- a bullish
    continuation signal."""
    all_bullish = bar1["close"] > bar1["open"] and bar2["close"] > bar2["open"] and bar3["close"] > bar3["open"]
    increasing_closes = bar3["close"] > bar2["close"] > bar1["close"]
    opens_within_prior = (bar1["open"] <= bar2["open"] <= bar1["close"]) and (bar2["open"] <= bar3["open"] <= bar2["close"])
    return all_bullish and increasing_closes and opens_within_prior


def _is_three_black_crows(bar1: dict, bar2: dict, bar3: dict) -> bool:
    all_bearish = bar1["close"] < bar1["open"] and bar2["close"] < bar2["open"] and bar3["close"] < bar3["open"]
    decreasing_closes = bar3["close"] < bar2["close"] < bar1["close"]
    opens_within_prior = (bar1["close"] <= bar2["open"] <= bar1["open"]) and (bar2["close"] <= bar3["open"] <= bar2["open"])
    return all_bearish and decreasing_closes and opens_within_prior


def _trendline_break(ctx: Dict[str, Any], tf_default: str, shift: int, side: str,
                      lookback: int, break_pct: float = 0.3) -> Optional[bool]:
    """Fits a straight line across the highs (side="resistance") or lows
    (side="support") over the lookback window via simple linear
    regression, then checks whether the current close has broken beyond
    it by at least break_pct%.

    Worth being upfront about the limitation: this is a least-squares
    fit across ALL bars in the window, not a line drawn through actual
    swing highs/lows the way a chartist would draw a trendline by eye --
    that's a more nuanced, genuinely harder-to-automate judgment (which
    swing points "count", ignoring wicks that don't represent the real
    trend, etc.). Treat this as a reasonable, useful approximation for
    screening candidates, not a substitute for visually confirming the
    line on a chart before acting on it.
    """
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    series = snap.get("series", {})
    high, low, close = series.get("high"), series.get("low"), series.get("close")
    if high is None or low is None or close is None:
        return None
    lookback = max(5, int(lookback or 20))
    n = len(close)
    current_idx = n - 1 - shift
    # Fit window is the `lookback` bars strictly BEFORE the current one --
    # the current bar is what we're testing for a breakout, not something
    # that should be part of the line it's being tested against.
    end_idx = current_idx - 1
    start_idx = end_idx - lookback + 1
    if start_idx < 0 or current_idx >= n:
        return None
    try:
        import numpy as np
        xs = np.arange(lookback, dtype=float)
        src = high if side == "resistance" else low
        ys = src.iloc[start_idx:end_idx + 1].values.astype(float)
        if len(ys) < lookback or np.any(np.isnan(ys)):
            return None
        slope, intercept = np.polyfit(xs, ys, 1)
        # Extrapolate one bar forward (position `lookback`, i.e. one past
        # the fit window's last point) to get the line's value AT the
        # current bar's position, then compare the current close against
        # that projected value.
        trendline_value_now = slope * lookback + intercept
        current_close = float(close.iloc[current_idx])
        if trendline_value_now == 0:
            return None
        pct_beyond = (current_close - trendline_value_now) / abs(trendline_value_now) * 100
        return bool(pct_beyond >= break_pct) if side == "resistance" else bool(pct_beyond <= -break_pct)
    except Exception:
        return None


# ── Chart pattern classification (triangles, wedges, channels) ──────────
# Built on top of swing-point detection: find local highs/lows, fit a
# trendline through each side, then classify the pattern from the two
# slopes' signs and relative steepness. This is a genuinely harder
# problem than the single-trendline break above -- it's judging the
# RELATIONSHIP between two independently-fitted lines, not just one
# line's position relative to price. Treat classifications as candidate
# signals worth a manual chart check, not a certified pattern the way a
# human chartist would draw one by hand (which also weighs which swing
# points "count" and ignores noise a rolling-window detector can't).

def _find_swing_points(series, window: int = 3):
    """Indices where `series` is a local max (for highs) or local min
    (for lows) within a symmetric `window`-bar neighborhood on each
    side. Standard swing-point definition: a bar "wins" against every
    other bar within `window` bars on both sides.
    """
    import numpy as np
    vals = series.values.astype(float)
    n = len(vals)
    highs_idx, lows_idx = [], []
    for i in range(window, n - window):
        seg = vals[i - window:i + window + 1]
        if vals[i] == seg.max() and np.sum(seg == seg.max()) == 1:
            highs_idx.append(i)
        if vals[i] == seg.min() and np.sum(seg == seg.min()) == 1:
            lows_idx.append(i)
    return highs_idx, lows_idx


def _fit_pattern_lines(ctx: Dict[str, Any], tf_default: str, shift: int, lookback: int, swing_window: int = 3):
    """Finds swing highs/lows within the lookback window (ending at the
    bar `shift` points back from the most recent), fits a straight line
    through each via linear regression, and returns both lines' slopes
    (normalized to %-of-price-per-bar, so patterns are comparable across
    different price levels) plus enough info to classify the pattern.
    Returns None if there aren't enough swing points on both sides to
    fit a meaningful line (need at least 2 each).
    """
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    series = snap.get("series", {})
    high, low, close = series.get("high"), series.get("low"), series.get("close")
    if high is None or low is None or close is None:
        return None
    lookback = max(15, int(lookback or 40))
    n = len(close)
    current_idx = n - 1 - shift
    end_idx = current_idx  # include the current bar's own high/low when finding swings
    start_idx = end_idx - lookback + 1
    if start_idx < 0 or current_idx >= n:
        return None
    try:
        import numpy as np
        high_win = high.iloc[start_idx:end_idx + 1].reset_index(drop=True)
        low_win = low.iloc[start_idx:end_idx + 1].reset_index(drop=True)
        highs_idx, _ = _find_swing_points(high_win, window=swing_window)
        _, lows_idx = _find_swing_points(low_win, window=swing_window)
        if len(highs_idx) < 2 or len(lows_idx) < 2:
            return None

        hx = np.array(highs_idx, dtype=float)
        hy = high_win.iloc[highs_idx].values.astype(float)
        lx = np.array(lows_idx, dtype=float)
        ly = low_win.iloc[lows_idx].values.astype(float)

        upper_slope, upper_intercept = np.polyfit(hx, hy, 1)
        lower_slope, lower_intercept = np.polyfit(lx, ly, 1)

        avg_price = float(close.iloc[start_idx:end_idx + 1].mean())
        if avg_price == 0:
            return None
        # Normalize slope to "% of average price per bar" so a $5 stock
        # and a $500 stock with the same SHAPE of pattern get comparable
        # slope values -- otherwise a wedge on an expensive stock could
        # look artificially "steeper" than an identical-shaped one on a
        # cheap stock purely from raw dollar slope.
        upper_slope_pct = upper_slope / avg_price * 100
        lower_slope_pct = lower_slope / avg_price * 100

        upper_now = upper_slope * (lookback - 1) + upper_intercept
        lower_now = lower_slope * (lookback - 1) + lower_intercept
        current_close = float(close.iloc[current_idx])

        return {
            "upper_slope_pct": upper_slope_pct, "lower_slope_pct": lower_slope_pct,
            "upper_value_now": upper_now, "lower_value_now": lower_now,
            "current_close": current_close, "lookback": lookback,
        }
    except Exception:
        return None


def _classify_chart_pattern(fit: dict, flat_threshold: float = 0.03, parallel_threshold: float = 0.04) -> Optional[str]:
    """Classifies a fitted upper/lower line pair into one of the
    standard chart pattern names, purely from the two slopes' signs and
    how close together they are (converging vs. roughly parallel vs.
    one flat). Thresholds are in %-of-price-per-bar.
    """
    us, ls = fit["upper_slope_pct"], fit["lower_slope_pct"]
    upper_flat = abs(us) <= flat_threshold
    lower_flat = abs(ls) <= flat_threshold
    converging = (us - ls) < -0.01  # upper line closing in on lower line over time
    roughly_parallel = abs(us - ls) <= parallel_threshold

    if upper_flat and ls > flat_threshold:
        return "ascending_triangle"
    if lower_flat and us < -flat_threshold:
        return "descending_triangle"
    if us < -flat_threshold and ls > flat_threshold:
        return "symmetrical_triangle"
    if us < -flat_threshold and ls < -flat_threshold:
        if converging:
            return "falling_wedge"
        if roughly_parallel:
            return "falling_channel"
    if us > flat_threshold and ls > flat_threshold:
        if converging:
            return "rising_wedge"
        if roughly_parallel:
            return "rising_channel"
    return None


def _chart_pattern_name(ctx: Dict[str, Any], tf_default: str, shift: int, lookback: int) -> Optional[str]:
    fit = _fit_pattern_lines(ctx, tf_default, shift, lookback)
    if not fit:
        return None
    return _classify_chart_pattern(fit)


def _chart_pattern_breakout(ctx: Dict[str, Any], tf_default: str, shift: int,
                             pattern: str, lookback: int, break_pct: float = 0.3) -> Optional[bool]:
    """True if the requested pattern is currently classified AND price
    has broken out of it in the pattern's typical breakout direction:
    upward for falling wedges and ascending triangles, downward for
    rising wedges and descending triangles, either direction for
    symmetrical triangles and channels (checked against whichever line
    price is closer to). This is the actual trade signal -- the pattern
    FORMING is context, the BREAK is what the query should actually be
    watching for.
    """
    fit = _fit_pattern_lines(ctx, tf_default, shift, lookback)
    if not fit:
        return None
    detected = _classify_chart_pattern(fit)
    if detected != pattern:
        return False

    upper_now, lower_now, cc = fit["upper_value_now"], fit["lower_value_now"], fit["current_close"]
    up_pct = (cc - upper_now) / abs(upper_now) * 100 if upper_now else 0
    down_pct = (lower_now - cc) / abs(lower_now) * 100 if lower_now else 0

    bullish_break = up_pct >= break_pct
    bearish_break = down_pct >= break_pct

    if pattern in ("falling_wedge", "ascending_triangle"):
        return bool(bullish_break)
    if pattern in ("rising_wedge", "descending_triangle"):
        return bool(bearish_break)
    if pattern in ("symmetrical_triangle", "rising_channel", "falling_channel"):
        return bool(bullish_break or bearish_break)
    return False


def _double_top_or_bottom(ctx: Dict[str, Any], tf_default: str, shift: int, kind: str,
                           lookback: int, tolerance_pct: float = 2.0, return_detail: bool = False):
    """Double Top: two swing highs within tolerance_pct% of each other,
    with a swing low (the "valley") meaningfully below both in between.
    Double Bottom is the mirror. Checks that the SECOND peak/trough is
    the most recent swing point (so this fires around when the pattern
    completes, not for an old pair buried earlier in the window).

    return_detail=True returns a dict with the two pivots' absolute bar
    indices and prices (instead of just True/False) -- used by the
    chart overlay to actually mark where the two peaks/troughs are,
    since a double top/bottom has no trendline to draw the way a
    triangle or wedge does.
    """
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    series = snap.get("series", {})
    high, low = series.get("high"), series.get("low")
    if high is None or low is None:
        return None
    lookback = max(15, int(lookback or 40))
    n = len(high)
    current_idx = n - 1 - shift
    end_idx = current_idx
    start_idx = end_idx - lookback + 1
    if start_idx < 0 or current_idx >= n:
        return None
    try:
        high_win = high.iloc[start_idx:end_idx + 1].reset_index(drop=True)
        low_win = low.iloc[start_idx:end_idx + 1].reset_index(drop=True)
        highs_idx, lows_idx = _find_swing_points(high_win if kind == "double_top" else low_win, window=3)
        pivots_idx = highs_idx if kind == "double_top" else lows_idx
        if len(pivots_idx) < 2:
            return {"hit": False} if return_detail else False
        src = high_win if kind == "double_top" else low_win
        p1_idx, p2_idx = pivots_idx[-2], pivots_idx[-1]
        p1, p2 = float(src.iloc[p1_idx]), float(src.iloc[p2_idx])
        if p1 == 0:
            return {"hit": False} if return_detail else False
        similar_level = abs(p2 - p1) / abs(p1) * 100 <= tolerance_pct
        between = (low_win if kind == "double_top" else high_win).iloc[p1_idx:p2_idx + 1]
        if between.empty:
            return {"hit": False} if return_detail else False
        mid_extreme = float(between.min() if kind == "double_top" else between.max())
        separation_ok = abs(mid_extreme - p1) / abs(p1) * 100 >= tolerance_pct * 1.5
        hit = bool(similar_level and separation_ok)
        if return_detail:
            return {
                "hit": hit,
                "p1_bar_index": start_idx + p1_idx, "p2_bar_index": start_idx + p2_idx,
                "p1_price": p1, "p2_price": p2,
            }
        return hit
    except Exception:
        return {"hit": False} if return_detail else None


# ── Staged pre-filter: a second, more powerful optimization on top of
# the reordering above. optimize_query() speeds up evaluation WITHIN one
# symbol's snapshot; this speeds things up BEFORE any snapshot gets
# built at all. Some conditions (Sector(), EarningsDays(), fundamentals,
# options-wall primitives) need no price history whatsoever -- each does
# its own fully self-contained DB/cache lookup. If a top-level AND query
# mixes these with price-dependent conditions (RSI, StrongCandle,
# volume, etc.), the snapshot-free ones can run across the WHOLE
# watchlist first, before bulk-loading a single bar of price history --
# and only the symbols that survive get that (comparatively much more
# expensive) work done for them at all.
_SNAPSHOT_FREE_PRIMITIVES = (
    _COST_TIER_CHEAP_LOOKUP | _COST_TIER_FUNDAMENTALS | _COST_TIER_WATCHLIST_AGG
    | (_COST_TIER_OPTIONS_DB - {"oi", "pcr", "oi_change", "oi_change_pct", "pcr_change", "pcr_change_pct"})
)
# oi/pcr and their change variants are deliberately excluded: unlike the
# wall primitives (which run their own DB query), these read from
# ctx["options_history"], which only exists once _scan_symbol has
# already built a symbol's full snapshot -- so they're NOT snapshot-free
# even though they live in the same options-DB cost tier.


def _is_snapshot_free(node) -> bool:
    """True if this sub-expression can be evaluated with nothing more
    than {"symbol": sym} in ctx -- no bulk-preloaded price history, no
    _scan_symbol snapshot at all. A bare price/volume/indicator
    identifier anywhere in the sub-expression makes the whole thing
    NOT snapshot-free, since there's no snapshot-free equivalent for
    those.
    """
    if node is None:
        return True
    if isinstance(node, FuncCallNode):
        if node.name.lower() not in _SNAPSHOT_FREE_PRIMITIVES:
            return False
        return all(_is_snapshot_free(a) for a in node.args)
    if isinstance(node, IdentifierNode):
        return False
    if isinstance(node, (NumberNode, StringNode)):
        return True
    if isinstance(node, UnaryNode):
        return _is_snapshot_free(node.expr)
    if isinstance(node, IndexNode):
        return _is_snapshot_free(node.expr)
    if isinstance(node, (BinaryNode, CompareNode)):
        return _is_snapshot_free(node.left) and _is_snapshot_free(node.right)
    return False


def split_prefilter_conditions(root):
    """For a top-level AND query, returns (prefilter_node, full_root) --
    prefilter_node is the AND of every operand that's snapshot-free (or
    None if there isn't at least one), full_root is the ORIGINAL,
    unmodified query for use in the actual scan afterward. Only applies
    at the top level of an AND chain: an OR at the top (or a query that
    IS just a single snapshot-free condition on its own) isn't split,
    since splitting an OR would still require evaluating every branch
    anyway -- either side alone could satisfy it, so there's no clean
    win there. The full_root scan re-checks the same conditions again
    for the surviving symbols, but that's negligible: they're the
    cheapest conditions in the query by construction, and
    optimize_query() already puts them first regardless.
    """
    if not (isinstance(root, BinaryNode) and root.op == "AND"):
        return None, root
    operands = _flatten_chain(root, "AND")
    prefilter_operands = [o for o in operands if _is_snapshot_free(o)]
    if not prefilter_operands or len(prefilter_operands) == len(operands):
        # Nothing to prefilter, or EVERYTHING is snapshot-free (in which
        # case the normal per-symbol path is already about as cheap as
        # this scan gets -- no separate pass needed).
        return None, root
    prefilter_node = _rebuild_chain(prefilter_operands, "AND") if len(prefilter_operands) > 1 else prefilter_operands[0]
    return prefilter_node, root


def prefilter_symbols(prefilter_node, symbols):
    """Evaluates a snapshot-free condition across a symbol list using
    only {"symbol": sym} as context -- no price history, no bulk
    preload, no per-symbol snapshot building. Returns the surviving
    subset, preserving original order. A symbol that errors on the
    prefilter check is kept (fails open) rather than silently dropped,
    so a bug in this fast path can never hide a symbol that would have
    matched -- worst case it just costs a bit more than the optimal
    savings, never a wrong/missing result.
    """
    if prefilter_node is None:
        return list(symbols)
    survivors = []
    for sym in symbols:
        try:
            ctx = {"symbol": sym}
            if bool(_eval(prefilter_node, ctx, shift=0, tf_default="1d")):
                survivors.append(sym)
        except Exception:
            survivors.append(sym)
    return survivors


def _eval(node: Node, ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d", stack: Tuple[str, ...] = ()):
    """Thin memoizing wrapper around _eval_inner (the actual evaluator,
    unchanged below). Caches by (object identity of node, shift,
    tf_default) in ctx["_eval_memo"] -- ctx is already rebuilt fresh per
    symbol, so this cache is correctly scoped and never leaks across
    symbols. This is what makes `let`-bound expressions (see
    _substitute_let_refs above _parse_query) genuinely evaluate once
    per (shift, timeframe) instead of once per occurrence in the query
    text: since a let reference is the SAME Node object shared by
    reference from multiple places in the tree, its id() is identical
    at every reference site, so the second (and later) evaluation is a
    cache hit. For ordinary queries with no `let` bindings, every node
    object is unique to begin with, so this is a no-op safety net
    (one dict lookup + one dict write per call) with no behavior change.

    IMPORTANT: id(node) is only guaranteed unique among objects that are
    CURRENTLY ALIVE. A common calling pattern elsewhere in this codebase
    is to parse a one-off expression string and evaluate it immediately,
    keeping no other reference to the resulting Node -- once _eval
    returns, that Node's refcount can drop to zero and CPython is free
    to reuse its memory address for a later, completely unrelated Node.
    Without a keepalive, that would produce a stale cache hit returning
    the WRONG value for a different expression (confirmed via direct
    testing during development -- this is not a theoretical concern).
    `_eval_memo_keepalive` holds a strong reference to every node that's
    ever been memoized, for as long as `ctx` itself is alive, so this
    can never happen.
    """
    memo = ctx.setdefault("_eval_memo", {})
    key = (id(node), shift, tf_default)
    if key in memo:
        cached_node, cached_result = memo[key]
        if cached_node is node:
            return cached_result
        # id() collision with a since-deallocated node -- not a real
        # cache hit, fall through and recompute.
    result = _eval_inner(node, ctx, shift=shift, tf_default=tf_default, stack=stack)
    memo[key] = (node, result)
    ctx.setdefault("_eval_memo_keepalive", []).append(node)
    return result


def _eval_inner(node: Node, ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d", stack: Tuple[str, ...] = ()):
    if isinstance(node, NumberNode):
        return node.value
    if isinstance(node, StringNode):
        return node.value
    if isinstance(node, IndexNode):
        return _eval(node.expr, ctx, shift=shift + max(0, int(node.bars or 0)), tf_default=tf_default, stack=stack)
    if isinstance(node, IdentifierNode):
        name = _normalize_indicator(node.name)
        if name in {"oi", "pcr", "oi_change", "oi_change_pct", "pcr_change", "pcr_change_pct"}:
            key = "total_oi" if name == "oi" else "pcr" if name == "pcr" else name
            val = _opt_hist_at(ctx, key, shift)
            return val
        if name == "beta":
            return ctx.get("beta")
        if name in {"earn_days", "earn_score"}:
            return ctx.get(name)
        # Do not return top-level current values for price/indicator names before
        # checking the timeframe series.  A previous shortcut made _node_series()
        # materialize constants for expressions like close, so SlopeDeg(close, 10)
        # evaluated to zero for every symbol.  Time-series indicators must always
        # respect shift/history first; scalar context values are handled below as
        # a fallback when no series exists.
        # Position-alert context variables.  These are injected only by the
        # journal custom position-alert scanner flow, so normal saved scanners
        # continue to read market series as before.
        position_ctx_keys = {
            "spot", "underlying", "short_strike", "long_strike",
            "sell_strike", "buy_strike", "put_sell", "put_buy",
            "call_sell", "call_buy", "short_put", "long_put",
            "short_call", "long_call", "breakeven", "breakeven_lower",
            "breakeven_upper", "lower_breakeven", "upper_breakeven",
            "dte", "days_to_expiry", "entry_price", "entry_net",
            "net_premium", "quantity", "qty", "pnl", "pnl_pct",
            "unrealised_pnl", "unrealized_pnl", "max_profit", "max_loss",
            "risk", "reward", "pnr", "pnr_upper", "distance_to_short",
            "distance_to_long", "distance_to_pnr", "distance_pct_to_short",
            "distance_pct_to_long", "distance_pct_to_pnr"
        }
        if name in position_ctx_keys:
            val = ctx.get(name)
            try:
                if val is None:
                    return None
                return float(val)
            except Exception:
                return None
        tf = _normalize_tf(node.tf or tf_default)
        snap = ctx.get("timeframes", {}).get(tf)
        series = snap.get("series", {}).get(name) if snap else None
        if series is not None:
            idx = len(series) - 1 - shift
            if idx < 0 or idx >= len(series):
                return None
            v = series.iloc[idx]
            try:
                return float(v)
            except Exception:
                return None
        if node.tf is None and name in ctx and name not in {"timeframes", "options_history"}:
            return ctx.get(name)
        return None
    if isinstance(node, UnaryNode):
        val = _eval(node.expr, ctx, shift=shift, tf_default=tf_default, stack=stack)
        if node.op == "NOT":
            return not bool(val)
        if node.op == "-":
            return -float(val) if val is not None else None
        if node.op == "+":
            return +float(val) if val is not None else None
        return val
    if isinstance(node, BinaryNode):
        if node.op in {"AND", "OR"}:
            left = bool(_eval(node.left, ctx, shift=shift, tf_default=tf_default, stack=stack))
            if node.op == "AND" and not left:
                return False
            if node.op == "OR" and left:
                return True
            right = bool(_eval(node.right, ctx, shift=shift, tf_default=tf_default, stack=stack))
            return (left and right) if node.op == "AND" else (left or right)
        left = _eval(node.left, ctx, shift=shift, tf_default=tf_default, stack=stack)
        right = _eval(node.right, ctx, shift=shift, tf_default=tf_default, stack=stack)
        if left is None or right is None:
            return None
        if node.op == "+":
            return float(left) + float(right)
        if node.op == "-":
            return float(left) - float(right)
        if node.op == "*":
            return float(left) * float(right)
        if node.op == "/":
            return float(left) / float(right) if float(right) != 0 else None
        return None
    if isinstance(node, CompareNode):
        left = _eval(node.left, ctx, shift=shift, tf_default=tf_default, stack=stack)
        right = _eval(node.right, ctx, shift=shift, tf_default=tf_default, stack=stack)
        if left is None or right is None:
            return False
        if node.op == ">":
            return left > right
        if node.op == ">=":
            return left >= right
        if node.op == "<":
            return left < right
        if node.op == "<=":
            return left <= right
        if node.op == "=":
            return _values_equal(left, right)
        if node.op == "cross_above":
            lprev = _eval(node.left, ctx, shift=shift + 1, tf_default=tf_default, stack=stack)
            rprev = _eval(node.right, ctx, shift=shift + 1, tf_default=tf_default, stack=stack)
            return lprev is not None and rprev is not None and lprev <= rprev and left > right
        if node.op == "cross_below":
            lprev = _eval(node.left, ctx, shift=shift + 1, tf_default=tf_default, stack=stack)
            rprev = _eval(node.right, ctx, shift=shift + 1, tf_default=tf_default, stack=stack)
            return lprev is not None and rprev is not None and lprev >= rprev and left < right
        return False
    if isinstance(node, FuncCallNode):
        fname = node.name.lower().strip()
        args = node.args
        if fname in {"uaeregime", "uae_regime"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if args2:
                maybe_tf = _eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
                if isinstance(maybe_tf, str) and _normalize_tf(maybe_tf) in TIMEFRAMES:
                    tf = _normalize_tf(maybe_tf)
            return _uae_regime_at(ctx, tf=tf, shift=shift)
        if fname in {"isuaeregime", "is_uae_regime", "uaeisregime"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if not args2:
                return False
            wanted = _uae_clean_regime(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack))
            current = _uae_clean_regime(_uae_regime_at(ctx, tf=tf, shift=shift))
            return bool(wanted and current and wanted == current)
        if fname in {"uaebull", "uae_bull", "uaeisbull"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_clean_regime(_uae_regime_at(ctx, tf=tf, shift=shift)) == "BULL"
        if fname in {"uaeweakbull", "uae_weak_bull", "uaeisweakbull"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_clean_regime(_uae_regime_at(ctx, tf=tf, shift=shift)) == "WEAK_BULL"
        if fname in {"uaebear", "uae_bear", "uaeisbear"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_clean_regime(_uae_regime_at(ctx, tf=tf, shift=shift)) == "BEAR"
        if fname in {"uaeweakbear", "uae_weak_bear", "uaeisweakbear"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_clean_regime(_uae_regime_at(ctx, tf=tf, shift=shift)) == "WEAK_BEAR"
        if fname in {"uaesideways", "uae_sideways", "uaeissideways"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_clean_regime(_uae_regime_at(ctx, tf=tf, shift=shift)) == "SIDEWAYS"
        if fname in {"uaeregimescore", "uae_regime_score", "uaescore", "uae_score"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_regime_score_at(ctx, tf=tf, shift=shift)
        if fname in {"uaeadx", "uae_adx"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_series_value(_uae_series_bundle(ctx, tf), "adx", shift=shift)
        if fname in {"uaeadxrising", "uae_adx_rising"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_bool_value(_uae_series_bundle(ctx, tf), "adx_rising", shift=shift)
        if fname in {"uaetrending", "uae_trending"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_bool_value(_uae_series_bundle(ctx, tf), "trending", shift=shift)
        if fname in {"uaersidiff", "uae_rsi_diff", "uaersidiff90", "uae_rsidiff90"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_series_value(_uae_series_bundle(ctx, tf), "rsidiff", shift=shift)
        if fname in {"uaemacd", "uae_macd", "uaemacdline", "uae_macd_line"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_series_value(_uae_series_bundle(ctx, tf), "macd", shift=shift)
        if fname in {"uaesignal", "uae_signal", "uaesignalline", "uae_signal_line"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_series_value(_uae_series_bundle(ctx, tf), "signal", shift=shift)
        if fname in {"uaehist", "uae_hist", "uaehistogram", "uae_histogram"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_series_value(_uae_series_bundle(ctx, tf), "hist", shift=shift)
        if fname in {"uaehistthreshold", "uae_hist_threshold", "uaestronghistthreshold"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_series_value(_uae_series_bundle(ctx, tf), "hist_threshold", shift=shift)
        if fname in {"uaestronghist", "uae_strong_hist", "uaeisstronghist"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_bool_value(_uae_series_bundle(ctx, tf), "strong_hist", shift=shift)
        if fname in {"uaehistgrowing", "uae_hist_growing", "uaehistogramgrowing"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _uae_bool_value(_uae_series_bundle(ctx, tf), "hist_growing", shift=shift)
        if fname in {"macddiff", "macdgap", "macdspread", "macdhistogramvalue", "macdhistvalue"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _macd_diff_at(ctx, shift=shift, tf_default=tf)
        if fname in {"macddiffabs", "macdgapabs", "macdspreadabs", "macdhistabs"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            val = _macd_diff_at(ctx, shift=shift, tf_default=tf)
            return None if val is None else abs(float(val))
        if fname in {"macddiffpct", "macdgappct", "macdspreadpct"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            return _macd_diff_pct_at(ctx, shift=shift, tf_default=tf)
        if fname in {"macdspreadratio", "macdgapratio", "macddiffratio"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            lookback = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 20)) if args2 else 20
            return _macd_diff_ratio(ctx, lookback=lookback, shift=shift, tf_default=tf)
        if fname in {"macddiffshrinkpct", "macdgapshrinkpct", "macdspreadshrinkpct", "macdhistshrinkpct"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            bars = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 1)) if args2 else 1
            return _macd_diff_shrink_pct(ctx, bars=bars, shift=shift, tf_default=tf)
        if fname in {"macdspreadstable", "macdgapstable", "macdhiststable", "macddiffstable"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            side = None
            max_shrink = 25.0
            bars = 1
            if args2:
                first = _eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
                if isinstance(first, str) and _side_arg(first) in {"bull", "bear"}:
                    side = _side_arg(first)
                    if len(args2) > 1:
                        max_shrink = float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or max_shrink)
                    if len(args2) > 2:
                        bars = int(float(_eval(args2[2], ctx, shift=shift, tf_default=tf, stack=stack) or bars))
                else:
                    max_shrink = float(first or max_shrink)
                    if len(args2) > 1:
                        bars = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or bars))
            return _macd_gap_stable(ctx, side=side, max_shrink_pct=max_shrink, bars=bars, shift=shift, tf_default=tf)
        if fname in {"macdfarfromsignal", "macdawayfromsignal"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            side = None
            min_pct = 0.25
            if args2:
                first = _eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
                if isinstance(first, str) and _side_arg(first) in {"bull", "bear"}:
                    side = _side_arg(first)
                    if len(args2) > 1:
                        min_pct = float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or min_pct)
                else:
                    min_pct = float(first or min_pct)
            diff = _macd_diff_at(ctx, shift=shift, tf_default=tf)
            pct = _macd_diff_pct_at(ctx, shift=shift, tf_default=tf)
            if diff is None or pct is None:
                return False
            if side == "bull" and float(diff) <= 0:
                return False
            if side == "bear" and float(diff) >= 0:
                return False
            return abs(float(pct)) >= abs(float(min_pct))
        if fname in {
            "uaediamond", "uae_diamond",
            "uaetrendtriangle", "uae_trend_triangle", "uaesolidtriangle", "uae_solid_triangle",
            "uaeweaktriangle", "uae_weak_triangle",
            "uaefadearrow", "uae_fade_arrow", "uaemrtarrow", "uae_mrt_arrow", "mrtarrow",
            "uaecircle", "uae_circle",
        }:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            side = _uae_side(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) if args2 else "bull") or "bull"
            bundle = _uae_series_bundle(ctx, tf)
            # Weekly/monthly visible signal primitives default to confirmed bars to avoid
            # provisional/repainting markers. Pass "live" as an extra arg to include the
            # current forming higher-timeframe bar.
            confirmed = _uae_mode_confirmed(args2[1:], ctx, shift=shift, tf_default=tf, stack=stack) if len(args2) > 1 else True
            eff_shift = shift + _uae_latest_bar_confirm_offset(bundle, tf, confirmed=confirmed)
            if fname in {"uaediamond", "uae_diamond"}:
                return _uae_bool_value(bundle, f"{side}_diamond", shift=eff_shift)
            if fname in {"uaetrendtriangle", "uae_trend_triangle", "uaesolidtriangle", "uae_solid_triangle"}:
                return _uae_bool_value(bundle, f"{side}_triangle_strong", shift=eff_shift)
            if fname in {"uaeweaktriangle", "uae_weak_triangle"}:
                return _uae_bool_value(bundle, f"{side}_triangle_weak", shift=eff_shift)
            if fname in {"uaefadearrow", "uae_fade_arrow", "uaemrtarrow", "uae_mrt_arrow", "mrtarrow"}:
                return _uae_bool_value(bundle, f"{side}_fade_arrow", shift=eff_shift)
            return _uae_bool_value(bundle, f"{side}_circle", shift=eff_shift)
        if fname in {"uaetrendtriangleage", "uae_trend_triangle_age", "uaesolidtriangleage", "uae_solid_triangle_age",
                     "uaetrendtriangledate", "uae_trend_triangle_date", "uaesolidtriangledate", "uae_solid_triangle_date",
                     "uaefadearrowage", "uae_fade_arrow_age", "uaemrtarrowage", "uae_mrt_arrow_age",
                     "uaefadearrowdate", "uae_fade_arrow_date", "uaemrtarrowdate", "uae_mrt_arrow_date",
                     "uaediamondage", "uae_diamond_age", "uaecircleage", "uae_circle_age",
                     "uaediamonddate", "uae_diamond_date", "uaecircledate", "uae_circle_date"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            side = _uae_side(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) if args2 else "bull") or "bull"
            max_bars = 20
            for a in args2[1:]:
                if isinstance(a, NumberNode):
                    try:
                        max_bars = int(float(_eval(a, ctx, shift=shift, tf_default=tf, stack=stack) or max_bars))
                        break
                    except Exception:
                        pass
            confirmed = _uae_mode_confirmed(args2[1:], ctx, shift=shift, tf_default=tf, stack=stack) if len(args2) > 1 else True
            bundle = _uae_series_bundle(ctx, tf)
            base_offset = _uae_latest_bar_confirm_offset(bundle, tf, confirmed=confirmed)
            if fname in {"uaetrendtriangleage", "uae_trend_triangle_age", "uaesolidtriangleage", "uae_solid_triangle_age",
                         "uaetrendtriangledate", "uae_trend_triangle_date", "uaesolidtriangledate", "uae_solid_triangle_date"}:
                key = f"{side}_triangle_strong"
            elif fname in {"uaefadearrowage", "uae_fade_arrow_age", "uaemrtarrowage", "uae_mrt_arrow_age",
                           "uaefadearrowdate", "uae_fade_arrow_date", "uaemrtarrowdate", "uae_mrt_arrow_date"}:
                key = f"{side}_fade_arrow"
            elif fname in {"uaediamondage", "uae_diamond_age", "uaediamonddate", "uae_diamond_date"}:
                key = f"{side}_diamond"
            else:
                key = f"{side}_circle"
            age = _uae_signal_age(bundle, key, max_bars=max_bars, shift=shift, base_offset=base_offset)
            if "date" in fname:
                if age is None:
                    return None
                return _uae_signal_date(bundle, shift=max(0, int(shift or 0)) + base_offset + int(age))
            return age
        if fname in {"uaelastmarker", "uae_last_marker", "uaelastsignal", "uae_last_signal"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            max_bars = 1
            for a in args2:
                if isinstance(a, NumberNode):
                    try:
                        max_bars = int(float(_eval(a, ctx, shift=shift, tf_default=tf, stack=stack) or max_bars))
                        break
                    except Exception:
                        pass
            confirmed = _uae_mode_confirmed(args2, ctx, shift=shift, tf_default=tf, stack=stack) if args2 else True
            bundle = _uae_series_bundle(ctx, tf)
            base_offset = _uae_latest_bar_confirm_offset(bundle, tf, confirmed=confirmed)
            return _uae_marker_value(bundle, max_bars=max_bars, shift=shift, base_offset=base_offset)
        if fname in {"uaelastmarkerage", "uae_last_marker_age", "uaelastsignalage", "uae_last_signal_age"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            max_bars = 20
            for a in args2:
                if isinstance(a, NumberNode):
                    try:
                        max_bars = int(float(_eval(a, ctx, shift=shift, tf_default=tf, stack=stack) or max_bars))
                        break
                    except Exception:
                        pass
            confirmed = _uae_mode_confirmed(args2, ctx, shift=shift, tf_default=tf, stack=stack) if args2 else True
            bundle = _uae_series_bundle(ctx, tf)
            base_offset = _uae_latest_bar_confirm_offset(bundle, tf, confirmed=confirmed)
            return _uae_marker_age(bundle, max_bars=max_bars, shift=shift, base_offset=base_offset)
        if fname in {"uaeconfluence", "uae_confluence"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            side = _uae_side(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) if args2 else "bull") or "bull"
            bars = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or 1)) if len(args2) > 1 else 1
            bars = max(1, min(50, bars))
            bundle = _uae_series_bundle(ctx, tf)
            count = 0
            for key in (f"{side}_triangle_strong", f"{side}_triangle_weak", f"{side}_circle", f"{side}_diamond"):
                found = False
                for i in range(bars):
                    if _uae_bool_value(bundle, key, shift=shift + i):
                        found = True
                        break
                if found:
                    count += 1
            # Triangle strong and weak are alternative versions of the same signal.
            tri_count = 1 if any(_uae_bool_value(bundle, f"{side}_triangle_strong", shift=shift + i) or _uae_bool_value(bundle, f"{side}_triangle_weak", shift=shift + i) for i in range(bars)) else 0
            circle_count = 1 if any(_uae_bool_value(bundle, f"{side}_circle", shift=shift + i) for i in range(bars)) else 0
            diamond_count = 1 if any(_uae_bool_value(bundle, f"{side}_diamond", shift=shift + i) for i in range(bars)) else 0
            fade_count = 1 if any(_uae_bool_value(bundle, f"{side}_fade_arrow", shift=shift + i) for i in range(bars)) else 0
            return tri_count + circle_count + diamond_count + fade_count
        if fname in {"uaehighertfaligned", "uae_higher_tf_aligned"}:
            side = _uae_side(_eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack) if args else "bull") or "bull"
            entry_raw = _eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack) if len(args) > 1 else tf_default
            entry_tf = _normalize_tf(entry_raw)
            higher = _uae_tf_higher(entry_tf)
            regime = _uae_clean_regime(_uae_regime_at(ctx, tf=higher, shift=shift))
            if side == "bull":
                return regime in {"BULL", "WEAK_BULL"}
            return regime in {"BEAR", "WEAK_BEAR"}
        if fname in {"uaemultitfaligned", "uae_multi_tf_aligned"}:
            side = _uae_side(_eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack) if args else "bull") or "bull"
            entry_raw = _eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack) if len(args) > 1 else tf_default
            entry_tf = _normalize_tf(entry_raw)
            tfs = _uae_tf_stack(entry_tf)
            if not tfs:
                return False
            for htf in tfs:
                regime = _uae_clean_regime(_uae_regime_at(ctx, tf=htf, shift=shift))
                if side == "bull" and regime not in {"BULL", "WEAK_BULL"}:
                    return False
                if side == "bear" and regime not in {"BEAR", "WEAK_BEAR"}:
                    return False
            return True
        if fname in {"sector", "sectorname", "sector_name", "sectoretf", "sector_etf"}:
            # Sector() returns the ETF code by default because it is compact and
            # filter-friendly. SectorName() returns the descriptive name.
            # Both fall back to a genuine standalone lookup (_symbol_sector /
            # _sector_etf_for_symbol) when ctx doesn't already have the
            # value cached -- without this, these only worked inside a
            # full _scan_symbol snapshot, which silently broke the
            # pre-filter optimizer's lightweight {"symbol": sym} context
            # (every symbol would look like it had no sector at all).
            symbol = ctx.get("symbol")
            if fname in {"sectorname", "sector_name"}:
                return ctx.get("sector_name") or ctx.get("sector") or (_symbol_sector(symbol) if symbol else None)
            if fname in {"sectoretf", "sector_etf", "sector"}:
                return ctx.get("sector_etf") or ctx.get("sector") or (_sector_etf_for_symbol(symbol) if symbol else None)
        if fname in {"sectorrs", "sector_rs"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = 20
            if args2:
                try:
                    # SectorRS("1w") is allowed; otherwise first arg is period.
                    first = _eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
                    if isinstance(first, str) and _normalize_tf(first) in TIMEFRAMES:
                        tf = _normalize_tf(first)
                    else:
                        period = int(float(first or period))
                except Exception:
                    period = 20
            period = max(1, period)
            cached = _read_precomputed_rs(ctx, "sector_rs", period, tf, shift)
            if cached is not None:
                return cached
            return _sector_rs_value(ctx, period, tf=tf, shift=shift)
        if fname in {"relativestrength", "rsstrength"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            benchmark = str(ctx.get('benchmark') or 'SPY').strip().upper() or 'SPY'
            period = 90
            if args2:
                if isinstance(args2[0], StringNode) and (len(args2) == 1 or not isinstance(args2[0], NumberNode)):
                    maybe_bench = str(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or '').strip()
                    if maybe_bench and _normalize_tf(maybe_bench) not in TIMEFRAMES:
                        benchmark = maybe_bench.upper()
                        if len(args2) > 1:
                            period = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or period))
                    else:
                        period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or period))
                elif len(args2) >= 2 and isinstance(args2[0], StringNode):
                    benchmark = str(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or benchmark).upper()
                    period = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or period))
                else:
                    period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or period))
                    if len(args2) > 1 and isinstance(args2[1], StringNode):
                        maybe_bench = str(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or '').strip()
                        if maybe_bench and _normalize_tf(maybe_bench) not in TIMEFRAMES:
                            benchmark = maybe_bench.upper()
            period = max(1, period)
            if benchmark == "SPY":
                cached = _read_precomputed_rs(ctx, "rs_vs_spy", period, tf, shift)
                if cached is not None:
                    return cached
            return _relative_strength_value(ctx, benchmark, period, tf=tf, shift=shift)
        if fname in {"mansfieldrs", "mansfield_rs"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            benchmark = str(ctx.get('benchmark') or 'SPY').strip().upper() or 'SPY'
            period = 52
            if args2:
                if isinstance(args2[0], StringNode) and (len(args2) > 1 or not isinstance(args2[0], NumberNode)):
                    maybe_bench = str(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or '').strip()
                    if maybe_bench and _normalize_tf(maybe_bench) not in TIMEFRAMES:
                        benchmark = maybe_bench.upper()
                        if len(args2) > 1:
                            period = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or period))
                    else:
                        period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or period))
                else:
                    period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or period))
                    if len(args2) > 1 and isinstance(args2[1], StringNode):
                        maybe_bench = str(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or '').strip()
                        if maybe_bench and _normalize_tf(maybe_bench) not in TIMEFRAMES:
                            benchmark = maybe_bench.upper()
            period = max(1, period)
            return _mansfield_rs_value(ctx, benchmark, period, tf=tf, shift=shift)
        if fname in {"rsrank", "rs_rank"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = 252
            if args2:
                if isinstance(args2[0], StringNode) and len(args2) > 1:
                    maybe_bench = str(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or '').strip()
                    if maybe_bench and _normalize_tf(maybe_bench) not in TIMEFRAMES:
                        period = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or period))
                    else:
                        period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or period))
                else:
                    period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or period))
            period = max(1, period)
            key = f'rs_rank_{period}'
            if key in ctx and ctx.get(key) is not None:
                return ctx.get(key)
            return ctx.get('leadership')
        if fname in {"scan", "expand"}:
            if not args:
                return False
            ref = _scanner_ref_name(args[0])
            inner = _load_scanner_ast(ref, stack=stack)
            return bool(_eval(inner, ctx, shift=shift, tf_default=tf_default, stack=stack + (ref,)))
        if fname in {"wpattern", "mpattern", "patternbreakout", "wpatternstrength", "mpatternstrength"}:
            #print("W/M EVAL", fname, args)
            args2, tf = _split_timeframe_args(list(args), tf_default)
            pattern = None
            bars = None
            strictness = 2
            if fname == "patternbreakout":
                if len(args2) < 2:
                    raise ValueError("PatternBreakout(patternType, bars[, timeframe=1d[, strictness=2]]) expects at least 2 arguments")
                pattern = str(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or "").strip().upper()
                bars = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or 0))
                if len(args2) > 2:
                    try:
                        strictness = _pattern_strictness_value(_eval(args2[2], ctx, shift=shift, tf_default=tf, stack=stack), 2)
                    except Exception:
                        strictness = 2
            else:
                if not args2:
                    raise ValueError(f"{fname}() expects at least (bars[, timeframe[, strictness]])")
                pattern = "W" if fname.startswith("w") else "M"
                bars = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 0))
                if len(args2) > 1:
                    try:
                        strictness = _pattern_strictness_value(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack), 2)
                    except Exception:
                        strictness = 2
            bars = max(1, bars or 1)
            score = _pattern_strength(ctx, pattern, bars, tf=tf, shift=shift, strictness=strictness)
            if fname.endswith("strength"):
                return score
            return None if score is None else bool(score >= _pattern_threshold(strictness, breakout=False))
        if fname in {"lookback", "within"}:
            if len(args) < 2:
                raise ValueError(f"{fname}() expects (expr, bars)")
            expr = args[0]
            bars = int(float(_eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack) or 0))
            bars = max(1, bars)
            # UAE visible-marker primitives need exact marker-age semantics.
            # Example: Lookback(UAETrendTriangle("bull", "1w"), 2) must pass
            # only when the visible weekly triangle is 0 or 1 bars ago; a
            # triangle 6-7 bars ago must not pass.
            supported, age, _label = _uae_event_age_for_node(expr, ctx, bars, shift=shift, tf_default=tf_default, stack=stack)
            if supported:
                return age is not None and 0 <= int(age) < bars
            for i in range(bars):
                if bool(_eval(expr, ctx, shift=shift + i, tf_default=tf_default, stack=stack)):
                    return True
            return False
        if fname in {"priorday", "prior", "shift"}:
            if not args:
                raise ValueError(f"{fname}() expects at least one argument")
            bars = int(float(_eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack))) if len(args) > 1 else 1
            bars = max(0, bars)
            return _eval(args[0], ctx, shift=shift + bars, tf_default=tf_default, stack=stack)
        if fname in {"abs", "absolute"}:
            if not args:
                return None
            val = _eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack)
            return None if val is None else abs(float(val))
        if fname in {"round", "rounded", "rnd"}:
            if not args:
                return None
            val = _eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack)
            if val is None:
                return None
            try:
                fv = float(val)
                if not math.isfinite(fv):
                    return None
            except Exception:
                return None
            decimals = 0
            if len(args) > 1:
                try:
                    decimals = int(float(_eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack) or 0))
                except Exception:
                    decimals = 0
            decimals = max(0, min(8, decimals))
            out = round(fv, decimals)
            return int(out) if decimals == 0 else float(out)
        if fname in {"between", "inrange", "withinrange"}:
            if len(args) < 3:
                raise ValueError("Between(value, low, high) expects 3 arguments")
            vals = [_eval(arg, ctx, shift=shift, tf_default=tf_default, stack=stack) for arg in args[:3]]
            nums = []
            for val in vals:
                try:
                    fv = float(val)
                    if pd.isna(fv) or not math.isfinite(fv):
                        return False
                    nums.append(fv)
                except Exception:
                    return False
            value, a, b = nums
            lo, hi = (a, b) if a <= b else (b, a)
            return lo <= value <= hi
        if fname in {"notbetween", "outside", "outsiderange"}:
            if len(args) < 3:
                raise ValueError("NotBetween(value, low, high) expects 3 arguments")
            vals = [_eval(arg, ctx, shift=shift, tf_default=tf_default, stack=stack) for arg in args[:3]]
            nums = []
            for val in vals:
                try:
                    fv = float(val)
                    if pd.isna(fv) or not math.isfinite(fv):
                        return False
                    nums.append(fv)
                except Exception:
                    return False
            value, a, b = nums
            lo, hi = (a, b) if a <= b else (b, a)
            return not (lo <= value <= hi)
        if fname in {"min", "max"}:
            vals = [_eval(arg, ctx, shift=shift, tf_default=tf_default, stack=stack) for arg in args]
            nums = _numeric_list(vals)
            if not nums:
                return None
            return min(nums) if fname == "min" else max(nums)
        if fname in {"strongbullbar", "strong_bull_bar"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            frames = _strong_signal_frames(ctx, tf_default=tf, shift=shift)
            if not frames:
                return None
            change = _numeric_arg(frames["change_pct"].iloc[-1 - shift] if False else args2[0] if False else args2[0], ctx, shift=shift, tf_default=tf, stack=stack) if False else None
            idx = len(frames["change_pct"]) - 1 - shift
            if idx < 1:
                return False
            ac = frames["abs_change"].iloc[idx]
            ace = frames["abs_change_ema"].iloc[idx]
            rv = frames["rel_volume"].iloc[idx]
            chg = frames["change_pct"].iloc[idx]
            relVol = frames["rel_volume"].iloc[idx]
            if pd.isna(ac) or pd.isna(ace) or pd.isna(rv) or pd.isna(chg):
                return False
            return float(chg) > 0 and float(rv) >= 2.0 and float(ac) <= float(ace) * 0.5 and (float(ac) * float(rv)) >= float(frames["strong_score_ema"].iloc[idx]) * 2.4
        if fname in {"strongbearbar", "strong_bear_bar"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            frames = _strong_signal_frames(ctx, tf_default=tf, shift=shift)
            if not frames:
                return None
            idx = len(frames["change_pct"]) - 1 - shift
            if idx < 1:
                return False
            ac = frames["abs_change"].iloc[idx]
            ace = frames["abs_change_ema"].iloc[idx]
            rv = frames["rel_volume"].iloc[idx]
            chg = frames["change_pct"].iloc[idx]
            if pd.isna(ac) or pd.isna(ace) or pd.isna(rv) or pd.isna(chg):
                return False
            return float(chg) < 0 and float(rv) >= 2.0 and float(ac) <= float(ace) * 0.5 and (float(ac) * float(rv)) >= float(frames["strong_score_ema"].iloc[idx]) * 2.4
        if fname in {"strongcandle", "strong_candle"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            frames = _strong_signal_frames(ctx, tf_default=tf, shift=shift)
            if not frames:
                return None
            idx = len(frames["change_pct"]) - 1 - shift
            if idx < 1:
                return False
            ac = frames["abs_change"].iloc[idx]
            ace = frames["abs_change_ema"].iloc[idx]
            rv = frames["rel_volume"].iloc[idx]
            chg = frames["change_pct"].iloc[idx]
            if pd.isna(ac) or pd.isna(ace) or pd.isna(rv) or pd.isna(chg):
                return False
            return float(ac) >= float(ace)*2.0 and float(rv) > 1 #and (float(ac) * float(rv)) >= float(frames["strong_score_ema"].iloc[idx]) * 2.4
        if fname in {"signalbar"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if not args2:
                raise ValueError("SignalBar(lookbackBars[, timeframe]) expects at least 1 argument")
            lookback_bars = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 0))
            idx = _signal_bar_index(ctx, lookback_bars, shift=shift, tf_default=tf)
            return idx is not None
        if fname in {"signalbarhigh"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if not args2:
                raise ValueError("SignalBarHigh(lookbackBars[, timeframe]) expects at least 1 argument")
            lookback_bars = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 0))
            idx = _signal_bar_index(ctx, lookback_bars, shift=shift, tf_default=tf)
            if idx is None:
                return None
            frames = _strong_signal_frames(ctx, tf_default=tf, shift=shift)
            if not frames:
                return None
            try:
                return float(frames["high"].iloc[idx])
            except Exception:
                return None
        if fname in {"signalbarlow"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if not args2:
                raise ValueError("SignalBarLow(lookbackBars[, timeframe]) expects at least 1 argument")
            lookback_bars = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 0))
            idx = _signal_bar_index(ctx, lookback_bars, shift=shift, tf_default=tf)
            if idx is None:
                return None
            frames = _strong_signal_frames(ctx, tf_default=tf, shift=shift)
            if not frames:
                return None
            try:
                return float(frames["low"].iloc[idx])
            except Exception:
                return None
        if fname == "changepct":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if not args2:
                return None
            bars = 1
            if len(args2) >= 2:
                try:
                    bars_val = _eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack)
                    bars = int(float(bars_val or 1))
                except Exception:
                    bars = 1
            bars = max(1, bars)
            cur = _eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
            prev = _eval(args2[0], ctx, shift=shift + bars, tf_default=tf, stack=stack)
            if cur is None or prev in (None, 0):
                return None
            return ((float(cur) - float(prev)) / abs(float(prev))) * 100.0
        if fname in {"avgabschangepct", "averageabschangepct", "absavgchangepct", "emaabschangepct", "emaabschange", "emaabschangepercent"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            bars = 60
            if args2:
                try:
                    bars = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 60))
                except Exception:
                    bars = 60
            return _avg_abs_change_pct(ctx, bars=max(1, bars), shift=shift, tf_default=tf)
        if fname in {
            "slope", "slopepct", "slopedeg", "slopepctperbar", "slopedegperbar", "slopedegraw", "sloperawdeg", "slopeatr", "slopeatrdeg",
            "regslopepct", "regslopedeg", "regressionslopepct", "regressionslopedeg",
            "regslopeatr", "regslopeatrdeg", "regressionslopeatr", "regressionslopeatrdeg",
        }:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if len(args2) < 2:
                raise ValueError(f"{fname}() expects (expr, bars[, timeframe])")
            bars = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or 0))
            bars = max(1, bars)
            series = _direct_series_for_identifier(args2[0], ctx, tf_default=tf)
            if series is None:
                series = _node_series(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
            if series is None or len(series) <= bars:
                return None
            idx = len(series) - 1 - shift
            prev_idx = idx - bars
            if idx < 0 or prev_idx < 0 or idx >= len(series) or prev_idx >= len(series):
                return None
            cur = series.iloc[idx]
            prev = series.iloc[prev_idx]
            if pd.isna(cur) or pd.isna(prev):
                return None
            cur_f = float(cur)
            prev_f = float(prev)
            raw_slope = (cur_f - prev_f) / float(bars)
            if fname == "slope":
                return raw_slope
            # Raw endpoint-angle semantics requested for SlopeDeg:
            # it is the angle of the straight line connecting value[bars] to
            # the current value, using raw units per bar.  For close:
            #   raw_slope = (current_close - close[10]) / 10
            #   SlopeDeg  = degrees(atan(raw_slope))
            # This is intentionally NOT percent-normalized and NOT a regression.
            if fname in {"slopedeg", "slopedegraw", "sloperawdeg"}:
                return math.degrees(math.atan(raw_slope))

            # Percent helpers remain available explicitly.  SlopePct is total
            # percent endpoint move; SlopePctPerBar/SlopeDegPerBar divide that
            # total move by bars for cross-symbol/timeframe filters.
            endpoint_base = abs(prev_f)
            if not math.isfinite(endpoint_base) or endpoint_base <= 1e-12:
                endpoint_base = max(abs(prev_f), abs(cur_f), 1.0)
            endpoint_pct_total = ((cur_f - prev_f) / endpoint_base) * 100.0
            endpoint_pct_per_bar = endpoint_pct_total / float(bars)
            if fname == "slopepct":
                return endpoint_pct_total
            if fname == "slopepctperbar":
                return endpoint_pct_per_bar
            if fname == "slopedegperbar":
                return math.degrees(math.atan(endpoint_pct_per_bar))

            # ATR-normalized endpoint slope.
            def _current_atr_value() -> Optional[float]:
                snap = ctx.get("timeframes", {}).get(_normalize_tf(tf))
                if not snap:
                    return None
                high = snap.get("series", {}).get("high")
                low = snap.get("series", {}).get("low")
                close_s = snap.get("series", {}).get("close")
                if high is None or low is None or close_s is None:
                    return None
                try:
                    atr = _atr_series(high, low, close_s, 14)
                    atr_val = float(atr.iloc[idx])
                except Exception:
                    return None
                if not math.isfinite(atr_val) or abs(atr_val) < 1e-12:
                    return None
                return atr_val

            if fname in {"slopeatr", "slopeatrdeg"}:
                atr_val = _current_atr_value()
                if atr_val is None:
                    return None
                atr_per_bar = raw_slope / atr_val
                if fname == "slopeatr":
                    return atr_per_bar
                return math.degrees(math.atan(atr_per_bar))

            # Explicit regression aliases preserve the old smoothed behavior.
            # The window includes the same endpoint span as bars ago -> current
            # (bars + 1 observations when available), so RegSlopeDeg(close, 10)
            # is still anchored to close[10] and current but fits all points in
            # between.
            window_start = max(0, idx - bars)
            window = pd.to_numeric(series.iloc[window_start:idx + 1], errors="coerce").dropna()
            if len(window) < 2:
                return None
            y = window.astype(float).to_numpy()
            x = list(range(len(y)))
            try:
                n = float(len(y))
                sx = float(sum(x))
                sy = float(sum(y))
                sxx = float(sum(v * v for v in x))
                sxy = float(sum(float(xi) * float(yi) for xi, yi in zip(x, y)))
                denom = n * sxx - sx * sx
                if abs(denom) < 1e-12:
                    return None
                reg_slope = (n * sxy - sx * sy) / denom
            except Exception:
                return None
            normalizer = float(pd.Series(y).abs().mean())
            if not math.isfinite(normalizer) or normalizer <= 1e-12:
                normalizer = max(abs(float(y[0])), abs(float(y[-1])), 1.0)
            reg_pct_per_bar = (reg_slope / normalizer) * 100.0
            if fname in {"regslopepct", "regressionslopepct"}:
                return reg_pct_per_bar
            if fname in {"regslopedeg", "regressionslopedeg"}:
                return math.degrees(math.atan(reg_pct_per_bar))
            atr_val = _current_atr_value()
            if atr_val is None:
                return None
            reg_atr_per_bar = reg_slope / atr_val
            if fname in {"regslopeatr", "regressionslopeatr"}:
                return reg_atr_per_bar
            if fname in {"regslopeatrdeg", "regressionslopeatrdeg"}:
                return math.degrees(math.atan(reg_atr_per_bar))
            return None
        if fname in {
            "lastswinghigh", "last_swing_high", "lastswinghighhigh", "last_swing_high_high",
            "lastswinghighclose", "last_swing_high_close", "lastswinghighopen", "last_swing_high_open", "lastswinghighlow", "last_swing_high_low",
            "lastswinghighbodylow", "last_swing_high_body_low", "lastswinghighbodyhigh", "last_swing_high_body_high",
            "lastswinghighage", "last_swing_high_age", "dayssinceswinghigh", "barsinceswinghigh", "barssinceswinghigh",
            "lastswinghighdate", "last_swing_high_date", "pullbackfromswinghighpct", "pullback_from_swing_high_pct", "pullbackfromswinghighatr", "pullback_from_swing_high_atr",
            "lastswinglow", "last_swing_low", "lastswinglowlow", "last_swing_low_low",
            "lastswinglowclose", "last_swing_low_close", "lastswinglowopen", "last_swing_low_open", "lastswinglowhigh", "last_swing_low_high",
            "lastswinglowbodylow", "last_swing_low_body_low", "lastswinglowbodyhigh", "last_swing_low_body_high",
            "lastswinglowage", "last_swing_low_age", "dayssinceswinglow", "barsinceswinglow", "barssinceswinglow",
            "lastswinglowdate", "last_swing_low_date", "bouncefromswinglowpct", "bounce_from_swing_low_pct", "bouncefromswinglowatr", "bounce_from_swing_low_atr",
        }:
            lookback, left_span, right_span, tf = _swing_args(list(args), ctx, shift=shift, tf_default=tf_default, stack=stack)
            side = "low" if "swinglow" in fname.replace("_", "") else "high"
            pivot = _last_swing_pivot(ctx, side=side, lookback=lookback, left=left_span, right=right_span, shift=shift, tf_default=tf)
            compact = fname.replace("_", "")
            if "date" in compact:
                return _swing_value(pivot, "date")
            if "age" in compact or "dayssince" in compact or "barsince" in compact:
                return _swing_value(pivot, "age")
            if "pullbackfromswinghighpct" in compact:
                return _swing_value(pivot, "pullback_pct")
            if "pullbackfromswinghighatr" in compact:
                return _swing_value(pivot, "pullback_atr")
            if "bouncefromswinglowpct" in compact:
                return _swing_value(pivot, "bounce_pct")
            if "bouncefromswinglowatr" in compact:
                return _swing_value(pivot, "bounce_atr")
            if "bodylow" in compact:
                return _swing_value(pivot, "body_low")
            if "bodyhigh" in compact:
                return _swing_value(pivot, "body_high")
            if "close" in compact:
                return _swing_value(pivot, "close")
            if "open" in compact:
                return _swing_value(pivot, "open")
            # LastSwingHighLow() asks for the low of the swing-high candle;
            # LastSwingLowHigh() asks for the high of the swing-low candle.
            if compact.endswith("low") and side == "high":
                return _swing_value(pivot, "low")
            if compact.endswith("high") and side == "low":
                return _swing_value(pivot, "high")
            return _swing_value(pivot, "price")
        if fname in {"score", "candlecontextscore", "confscore", "confluencescore", "confluencecount", "confluenceratio"}:
            # Reuses Candle Context's OWN scoring machinery (not a
            # reimplementation), and for live (shift=0) queries reads
            # from the pre-built watchlist-level cache in
            # technical_snapshot.py -- the SAME background batch job
            # that already computes RSI/EMA/MACD/etc for every
            # watchlist symbol also computes and stores this, so a
            # live Score()/ConfScore() call here is normally a cheap
            # indexed read, not a fresh price-history fetch + full
            # rescore on every query evaluation. Historical/shifted
            # lookups (backtests walking through past days) fall back
            # to live recompute, since the cache only ever holds the
            # latest snapshot per symbol -- it has no history to serve.
            symbol = str(ctx.get("symbol") or "").upper()
            if not symbol:
                return None
            cache_key = f"_candle_ctx_score_cache_shift{shift}"
            cached = ctx.get(cache_key)
            if cached is None and shift == 0:
                try:
                    from ..services.technical_snapshot import get_or_compute_technical_snapshot
                    snap = get_or_compute_technical_snapshot(symbol, "1d")
                except Exception:
                    snap = None
                if snap is not None and snap.get("candle_ctx_score") is not None:
                    cached = {
                        "score": snap.get("candle_ctx_score"),
                        "confluence": {"agreeing": snap.get("candle_ctx_confluence"), "total": snap.get("candle_ctx_confluence_total")},
                    }
            if cached is None:
                try:
                    from .candle_context_scanner import _get_price_history, _score_symbol_asof, DEFAULTS as _CCTX_DEFAULTS
                    full_hist = _get_price_history(symbol, min_days=250)
                    if full_hist is not None:
                        n = len(full_hist["close"])
                        end_idx = n - 1 - shift
                        if end_idx >= 50:
                            hist_slice = {
                                "date": full_hist["date"][:end_idx + 1],
                                "open": full_hist["open"].iloc[:end_idx + 1], "high": full_hist["high"].iloc[:end_idx + 1],
                                "low": full_hist["low"].iloc[:end_idx + 1], "close": full_hist["close"].iloc[:end_idx + 1],
                                "volume": full_hist["volume"].iloc[:end_idx + 1],
                            }
                            result, _gate_passed, raw_score = _score_symbol_asof(symbol, dict(_CCTX_DEFAULTS), hist_slice)
                            confluence = (result or {}).get("confluence") if result else None
                            cached = {"score": raw_score, "confluence": confluence}
                        else:
                            cached = {"score": None, "confluence": None}
                    else:
                        cached = {"score": None, "confluence": None}
                except Exception:
                    cached = {"score": None, "confluence": None}
                ctx[cache_key] = cached
            else:
                ctx[cache_key] = cached

            if fname in {"score", "candlecontextscore"}:
                return cached["score"]
            confluence = cached.get("confluence")
            if confluence is None:
                return None
            if fname == "confluenceratio":
                total, agreeing = confluence.get("total"), confluence.get("agreeing")
                return round(agreeing / total, 2) if total else None
            return confluence.get("agreeing")  # confscore/confluencescore/confluencecount all mean the same thing: the raw agreeing-dimension count
        if fname in {"higherlowconfirmed", "higher_low_confirmed", "lowerhighconfirmed", "lower_high_confirmed"}:
            side = "low" if "higherlow" in fname.replace("_", "") else "high"
            args2, tf = _split_timeframe_args(list(args), tf_default)
            def _arg(i, default):
                if len(args2) > i:
                    try:
                        return float(_eval(args2[i], ctx, shift=shift, tf_default=tf, stack=stack))
                    except Exception:
                        return default
                return default
            count = int(_arg(0, 3))
            min_sep_pct = _arg(1, 0.5)
            require_intact = bool(_arg(2, 1.0))
            lookback = int(_arg(3, 60))
            left = int(_arg(4, 2))
            right = int(_arg(5, 2))
            return _swing_sequence_check(ctx, side, count, min_sep_pct, require_intact,
                                          max(3, lookback), max(1, left), max(1, right), shift, _normalize_tf(tf))
        if fname in {"chochbullish", "choch_bullish", "chochbearish", "choch_bearish",
                     "bosbullish", "bos_bullish", "bosbearish", "bos_bearish"}:
            direction = "bullish" if "bullish" in fname else "bearish"
            is_bos = fname.startswith("bos")
            args2, tf = _split_timeframe_args(list(args), tf_default)
            def _arg(i, default):
                if len(args2) > i:
                    try:
                        return float(_eval(args2[i], ctx, shift=shift, tf_default=tf, stack=stack))
                    except Exception:
                        return default
                return default
            lookback = int(_arg(0, 60))
            left = int(_arg(1, 2))
            right = int(_arg(2, 2))
            tfn = _normalize_tf(tf)
            if is_bos:
                result = _detect_bos(ctx, direction, max(3, lookback), max(1, left), max(1, right), shift, tfn)
                return result["bos"] if result else None
            result = _detect_choch(ctx, direction, max(3, lookback), max(1, left), max(1, right), shift, tfn)
            return result["choch"] if result else None
        if fname in {"retracementpct", "retracement_pct"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if not args2:
                return None
            direction_val = _eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
            direction = str(direction_val or "bullish").lower()
            direction = "bullish" if direction.startswith("bull") else "bearish"
            def _arg(i, default):
                if len(args2) > i:
                    try:
                        return float(_eval(args2[i], ctx, shift=shift, tf_default=tf, stack=stack))
                    except Exception:
                        return default
                return default
            lookback = int(_arg(1, 60))
            left = int(_arg(2, 2))
            right = int(_arg(3, 2))
            return _retracement_pct(ctx, direction, max(3, lookback), max(1, left), max(1, right), shift, _normalize_tf(tf))
        if fname in {"bbupper", "bblower", "bbmiddle", "bbwidth", "bbpercent"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            def _arg(i, default):
                if len(args2) > i:
                    try:
                        return float(_eval(args2[i], ctx, shift=shift, tf_default=tf, stack=stack))
                    except Exception:
                        return default
                return default
            period = int(_arg(0, 20))
            mult = _arg(1, 2.0)
            bands = _bollinger_bands(ctx, period, mult, shift, _normalize_tf(tf))
            if bands is None:
                return None
            upper, middle, lower = bands
            if fname == "bbupper":
                return round(upper, 4)
            if fname == "bblower":
                return round(lower, 4)
            if fname == "bbmiddle":
                return round(middle, 4)
            if fname == "bbwidth":
                return round((upper - lower) / middle * 100.0, 2) if middle else None
            # bbpercent (%B): 0 = at lower band, 1 = at upper band, can go
            # outside 0-1 if price is beyond the bands entirely.
            cur_price = _choch_bos_close(ctx, _normalize_tf(tf), shift)
            if cur_price is None or upper == lower:
                return None
            return round((cur_price - lower) / (upper - lower), 3)
        if fname in {"kcupper", "kclower", "kcmiddle"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            def _arg(i, default):
                if len(args2) > i:
                    try:
                        return float(_eval(args2[i], ctx, shift=shift, tf_default=tf, stack=stack))
                    except Exception:
                        return default
                return default
            period = int(_arg(0, 20))
            atr_period = int(_arg(1, 10))
            mult = _arg(2, 2.0)
            bands = _keltner_channel(ctx, period, atr_period, mult, shift, _normalize_tf(tf))
            if bands is None:
                return None
            upper, middle, lower = bands
            if fname == "kcupper":
                return round(upper, 4)
            if fname == "kclower":
                return round(lower, 4)
            return round(middle, 4)
        if fname in {"squeezeon", "squeeze_on", "ttmsqueeze"}:
            # TTM Squeeze (John Carter): volatility contraction signal --
            # Bollinger Bands sitting entirely INSIDE the Keltner Channel
            # means price range has compressed enough that the two
            # different volatility measures (stdev-based vs ATR-based)
            # agree the range is unusually tight, historically a
            # precursor to an expansion move either direction. Directly
            # complements ATRCompression/RangeCompression/VolumeDryup --
            # this is a different, well-known way to define the same
            # "coiled spring" concept those primitives approximate.
            args2, tf = _split_timeframe_args(list(args), tf_default)
            def _arg(i, default):
                if len(args2) > i:
                    try:
                        return float(_eval(args2[i], ctx, shift=shift, tf_default=tf, stack=stack))
                    except Exception:
                        return default
                return default
            bb_period = int(_arg(0, 20))
            bb_mult = _arg(1, 2.0)
            kc_period = int(_arg(2, 20))
            kc_atr_period = int(_arg(3, 10))
            kc_mult = _arg(4, 1.5)
            tfn = _normalize_tf(tf)
            bb = _bollinger_bands(ctx, bb_period, bb_mult, shift, tfn)
            kc = _keltner_channel(ctx, kc_period, kc_atr_period, kc_mult, shift, tfn)
            if bb is None or kc is None:
                return None
            bb_upper, _, bb_lower = bb
            kc_upper, _, kc_lower = kc
            return bool(bb_upper < kc_upper and bb_lower > kc_lower)
        if fname in {"sectorstrength", "sector_strength"}:
            # Is the CURRENT symbol's own SECTOR (as a whole, via its
            # ETF) outperforming SPY -- sector-level rotation strength,
            # the "which sector is strengthening" half of sector
            # rotation analysis, computed per-symbol so it composes
            # with everything else in one query (e.g. "my sector is
            # strong AND I'm beating my sector" -- SectorStrength()>0
            # and SectorRS()>0, which already existed). Reuses ctx's
            # sector_etf (resolved once per symbol via the same
            # _sector_etf_for_symbol SectorRS itself uses -- NOT a
            # second, separate sector-mapping lookup).
            sector_etf = ctx.get("sector_etf")
            if not sector_etf:
                return None
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = 20
            if args2:
                try:
                    period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or period))
                except Exception:
                    pass
            cached = _read_precomputed_rs(ctx, "sector_strength", period, tf, shift)
            if cached is not None:
                return cached
            return _sector_etf_strength(ctx, sector_etf, max(1, period), _normalize_tf(tf), shift)
        if fname in {"bounceoffswinghigh", "bounce_off_swing_high", "bounceoffswinglow", "bounce_off_swing_low"}:
            side = "high" if "high" in fname else "low"
            args2, tf = _split_timeframe_args(list(args), tf_default)
            def _arg(i, default):
                if len(args2) > i:
                    try:
                        return float(_eval(args2[i], ctx, shift=shift, tf_default=tf, stack=stack))
                    except Exception:
                        return default
                return default
            tolerance_pct = _arg(0, 0.5)
            lookback = int(_arg(1, 60))
            left = int(_arg(2, 2))
            right = int(_arg(3, 2))
            max_pivot_age = int(_arg(4, 15))
            return _bounce_off_swing(ctx, side, tolerance_pct, max(3, lookback), max(1, left), max(1, right), shift, _normalize_tf(tf), max_pivot_age=max(1, max_pivot_age))
        if fname in {"pinbaratsupport", "pin_bar_at_support", "pinbaratresistance", "pin_bar_at_resistance"}:
            side = "low" if "support" in fname else "high"
            args2, tf = _split_timeframe_args(list(args), tf_default)
            def _arg(i, default):
                if len(args2) > i:
                    try:
                        return float(_eval(args2[i], ctx, shift=shift, tf_default=tf, stack=stack))
                    except Exception:
                        return default
                return default
            tolerance_pct = _arg(0, 0.5)
            lookback = int(_arg(1, 60))
            left = int(_arg(2, 2))
            right = int(_arg(3, 2))
            max_pivot_age = int(_arg(4, 15))
            return _pin_bar_at_level(ctx, side, tolerance_pct, max(3, lookback), max(1, left), max(1, right), shift, _normalize_tf(tf), max_pivot_age=max(1, max_pivot_age))
        if fname in {"isheadandshoulders", "is_head_and_shoulders", "isinverseheadandshoulders", "is_inverse_head_and_shoulders",
                     "necklinelevel", "neckline_level"}:
            direction = "bullish" if "inverse" in fname else "bearish"
            args2, tf = _split_timeframe_args(list(args), tf_default)
            def _arg(i, default):
                if len(args2) > i:
                    try:
                        return float(_eval(args2[i], ctx, shift=shift, tf_default=tf, stack=stack))
                    except Exception:
                        return default
                return default
            shoulder_tol = _arg(0, 8.0)
            lookback = int(_arg(1, 30))
            left = int(_arg(2, 2))
            right = int(_arg(3, 2))
            result = _detect_head_and_shoulders(ctx, direction, shoulder_tol, max(3, lookback), max(1, left), max(1, right), shift, _normalize_tf(tf))
            if result is None or not result.get("detected"):
                return None if fname.startswith("neckline") else False
            if fname.startswith("neckline"):
                return result.get("neckline")
            return result.get("broke_neckline")
        if fname in {"isbullflag", "is_bull_flag", "isbearflag", "is_bear_flag"}:
            direction = "bearish" if "bear" in fname else "bullish"
            args2, tf = _split_timeframe_args(list(args), tf_default)
            def _arg(i, default):
                if len(args2) > i:
                    try:
                        return float(_eval(args2[i], ctx, shift=shift, tf_default=tf, stack=stack))
                    except Exception:
                        return default
                return default
            pole_pct = _arg(0, 15.0)
            pole_bars = int(_arg(1, 10))
            flag_bars = int(_arg(2, 8))
            flag_max_retrace = _arg(3, 50.0)
            result = _detect_flag(ctx, direction, pole_pct, max(2, pole_bars), max(2, flag_bars), flag_max_retrace, _normalize_tf(tf), shift)
            return bool(result.get("detected") and result.get("breakout")) if result else None
        if fname in {"iscupandhandle", "is_cup_and_handle"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            def _arg(i, default):
                if len(args2) > i:
                    try:
                        return float(_eval(args2[i], ctx, shift=shift, tf_default=tf, stack=stack))
                    except Exception:
                        return default
                return default
            cup_lookback = int(_arg(0, 60))
            handle_bars = int(_arg(1, 10))
            handle_max_pct = _arg(2, 35.0)
            rim_tolerance = _arg(3, 3.0)
            result = _detect_cup_and_handle(ctx, max(10, cup_lookback), max(3, handle_bars), handle_max_pct, rim_tolerance, _normalize_tf(tf), shift)
            return bool(result.get("detected") and result.get("breakout")) if result else None
        if fname in {"secondlegup", "loosempattern", "loosetop", "mtop", "doubletop", "secondlegdown", "loosewpattern", "loosebottom", "wbottom", "doublebottom"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            lookback = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 45)) if args2 else 45
            tolerance = float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or 5.0) if len(args2) > 1 else 5.0
            min_swing = float(_eval(args2[2], ctx, shift=shift, tf_default=tf, stack=stack) or 3.0) if len(args2) > 2 else 3.0
            recent = int(float(_eval(args2[3], ctx, shift=shift, tf_default=tf, stack=stack) or 3)) if len(args2) > 3 else 3
            side = "up" if fname in {"secondlegup", "loosempattern", "loosetop", "mtop", "doubletop"} else "down"
            return _best_second_leg_pattern(ctx, side=side, lookback=lookback, shift=shift, tf_default=tf, tolerance_pct=tolerance, min_swing_pct=min_swing, recent_bars=recent) is not None
        if fname in {"secondlegscore", "secondleglevel", "secondlegfirst", "secondlegsecond", "secondlegneckline", "secondlegmatchpct", "secondlegswingpct", "secondlegage"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if len(args2) < 2:
                raise ValueError(f"{fname}() expects at least (side, lookback[, timeframe])")
            side = _second_leg_side_arg(_string_arg(args2[0], ctx, shift=shift, tf_default=tf, stack=stack, default="up"))
            # secondleglevel(side, which, lookback, ...) has an extra 'which' argument.
            if fname == "secondleglevel":
                if len(args2) < 3:
                    raise ValueError("SecondLegLevel(side, which, lookback[, timeframe]) expects 3 arguments")
                which_node = args2[1]
                lookback_idx = 2
            else:
                which_node = None
                lookback_idx = 1
            lookback = int(float(_eval(args2[lookback_idx], ctx, shift=shift, tf_default=tf, stack=stack) or 45))
            tolerance = float(_eval(args2[lookback_idx + 1], ctx, shift=shift, tf_default=tf, stack=stack) or 5.0) if len(args2) > lookback_idx + 1 else 5.0
            min_swing = float(_eval(args2[lookback_idx + 2], ctx, shift=shift, tf_default=tf, stack=stack) or 3.0) if len(args2) > lookback_idx + 2 else 3.0
            recent = int(float(_eval(args2[lookback_idx + 3], ctx, shift=shift, tf_default=tf, stack=stack) or 3)) if len(args2) > lookback_idx + 3 else 3
            pat = _best_second_leg_pattern(ctx, side=side, lookback=lookback, shift=shift, tf_default=tf, tolerance_pct=tolerance, min_swing_pct=min_swing, recent_bars=recent)
            if not pat:
                return None
            if fname == "secondlegscore":
                return _safe_number(pat.get("score"))
            if fname == "secondlegfirst":
                return _safe_number(pat.get("first"))
            if fname == "secondlegsecond":
                return _safe_number(pat.get("second"))
            if fname == "secondlegneckline":
                return _safe_number(pat.get("neckline"))
            if fname == "secondlegmatchpct":
                return _safe_number(pat.get("match_pct"))
            if fname == "secondlegswingpct":
                return _safe_number(pat.get("swing_pct"))
            if fname == "secondlegage":
                return _safe_number(pat.get("age"))
            if fname == "secondleglevel":
                which = _eval(which_node, ctx, shift=shift, tf_default=tf, stack=stack)
                return _second_leg_level_value(pat, which)
            return None
        if fname in {"strongbullcandle", "strongbearcandle"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            lookback = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 20)) if args2 else 20
            min_body = float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or 50.0) if len(args2) > 1 else 50.0
            min_range_atr = float(_eval(args2[2], ctx, shift=shift, tf_default=tf, stack=stack) or 2.0) if len(args2) > 2 else 2.0
            min_vol_mult = float(_eval(args2[3], ctx, shift=shift, tf_default=tf, stack=stack) or 1.1) if len(args2) > 3 else 1.1
            avg_bars = int(float(_eval(args2[4], ctx, shift=shift, tf_default=tf, stack=stack) or 60)) if len(args2) > 4 else 60
            side = "bull" if fname == "strongbullcandle" else "bear"
            return _strong_candle_anchor(ctx, side=side, lookback=lookback, shift=shift, tf_default=tf, min_body_pct=min_body, min_range_atr=min_range_atr, min_vol_mult=min_vol_mult, avg_change_bars=avg_bars) is not None
        if fname in {"strongcandleage", "strongcandlehigh", "strongcandlelow", "strongcandleopen", "strongcandleclose", "strongcandlechangepct", "strongcandleavgabschangepct", "strongcandlemovemultiple", "strongcandlelevel", "touchstrongcandlelevel", "nearstrongcandlelevel"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if len(args2) < 2:
                raise ValueError(f"{fname}() expects at least (side, lookback[, timeframe])")
            side = _side_arg(_string_arg(args2[0], ctx, shift=shift, tf_default=tf, stack=stack, default="bull"))
            # level functions take (side, level, lookback...), while age/high/low/open/close take (side, lookback...).
            if fname in {"strongcandlelevel", "touchstrongcandlelevel", "nearstrongcandlelevel"}:
                if len(args2) < 3:
                    raise ValueError(f"{fname}() expects (side, level, lookback[, tolerancePct][, timeframe])")
                level_val = _eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack)
                lookback = int(float(_eval(args2[2], ctx, shift=shift, tf_default=tf, stack=stack) or 20))
                if fname in {"touchstrongcandlelevel", "nearstrongcandlelevel"}:
                    # These two DO take a tolerancePct right after lookback, per their
                    # documented signature: (side, level, lookback, tolerancePct, ...).
                    tolerance = float(_eval(args2[3], ctx, shift=shift, tf_default=tf, stack=stack) or 0.75) if len(args2) > 3 else 0.75
                    min_body = float(_eval(args2[4], ctx, shift=shift, tf_default=tf, stack=stack) or 50.0) if len(args2) > 4 else 50.0
                    min_range_atr = float(_eval(args2[5], ctx, shift=shift, tf_default=tf, stack=stack) or 2.0) if len(args2) > 5 else 2.0
                    min_vol_mult = float(_eval(args2[6], ctx, shift=shift, tf_default=tf, stack=stack) or 1.1) if len(args2) > 6 else 1.1
                    avg_bars = int(float(_eval(args2[7], ctx, shift=shift, tf_default=tf, stack=stack) or 60)) if len(args2) > 7 else 60
                    return _touch_strong_candle_level(ctx, side, level_val, lookback, tolerance_pct=tolerance, shift=shift, tf_default=tf, min_body_pct=min_body, min_range_atr=min_range_atr, min_vol_mult=min_vol_mult, avg_change_bars=avg_bars)
                # Plain StrongCandleLevel has NO tolerancePct in its documented signature —
                # (side, level, lookback[, timeframe[, minBodyPct[, moveMult[, minVolMult[, avgBars]]]]]).
                # Don't consume an extra slot here or every parameter after lookback silently
                # shifts one position to the right of what the user actually typed.
                min_body = float(_eval(args2[3], ctx, shift=shift, tf_default=tf, stack=stack) or 50.0) if len(args2) > 3 else 50.0
                min_range_atr = float(_eval(args2[4], ctx, shift=shift, tf_default=tf, stack=stack) or 2.0) if len(args2) > 4 else 2.0
                min_vol_mult = float(_eval(args2[5], ctx, shift=shift, tf_default=tf, stack=stack) or 1.1) if len(args2) > 5 else 1.1
                avg_bars = int(float(_eval(args2[6], ctx, shift=shift, tf_default=tf, stack=stack) or 60)) if len(args2) > 6 else 60
                anchor = _strong_candle_anchor(ctx, side=side, lookback=lookback, shift=shift, tf_default=tf, min_body_pct=min_body, min_range_atr=min_range_atr, min_vol_mult=min_vol_mult, avg_change_bars=avg_bars)
                return _strong_candle_level(anchor, level_val)
            lookback = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or 20))
            min_body = float(_eval(args2[2], ctx, shift=shift, tf_default=tf, stack=stack) or 50.0) if len(args2) > 2 else 50.0
            min_range_atr = float(_eval(args2[3], ctx, shift=shift, tf_default=tf, stack=stack) or 2.0) if len(args2) > 3 else 2.0
            min_vol_mult = float(_eval(args2[4], ctx, shift=shift, tf_default=tf, stack=stack) or 1.1) if len(args2) > 4 else 1.1
            avg_bars = int(float(_eval(args2[5], ctx, shift=shift, tf_default=tf, stack=stack) or 60)) if len(args2) > 5 else 60
            anchor = _strong_candle_anchor(ctx, side=side, lookback=lookback, shift=shift, tf_default=tf, min_body_pct=min_body, min_range_atr=min_range_atr, min_vol_mult=min_vol_mult, avg_change_bars=avg_bars)
            if not anchor:
                return None
            if fname == "strongcandleage":
                return anchor.get("age")
            key_map = {
                "strongcandlehigh": "high",
                "strongcandlelow": "low",
                "strongcandleopen": "open",
                "strongcandleclose": "close",
                "strongcandlechangepct": "change_pct",
                "strongcandleavgabschangepct": "avg_abs_change_pct",
                "strongcandlemovemultiple": "move_multiple",
            }
            key = key_map.get(fname, fname.replace("strongcandle", ""))
            return _safe_number(anchor.get(key))
        if fname in {"crossabove", "crossover", "crossbelow", "crossunder"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if len(args2) < 2:
                raise ValueError(f"{fname}() expects (series, level[, timeframe])")
            return _cross_value(ctx, args2[0], args2[1], shift=shift, tf_default=tf, stack=stack, above=fname in {"crossabove", "crossover"})
        if fname == "retest":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if len(args2) < 3:
                raise ValueError("Retest(level, tolerancePct, bars[, timeframe]) expects 3 arguments")
            level = _level_at(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
            if level is None:
                return None
            tolerance = float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or 0)
            bars = int(float(_eval(args2[2], ctx, shift=shift, tf_default=tf, stack=stack) or 0))
            bars = max(1, bars)
            snap = ctx.get("timeframes", {}).get(_normalize_tf(tf))
            if not snap:
                return None
            close = snap.get("series", {}).get("close")
            if close is None:
                return None
            idx = len(close) - 1 - shift
            if idx < 1:
                return None
            band = abs(float(level)) * (abs(tolerance) / 100.0) if level else abs(tolerance) / 100.0
            within = False
            crossed = False
            for i in range(bars):
                cur_idx = idx - i
                prev_idx = cur_idx - 1
                if cur_idx < 0 or prev_idx < 0:
                    break
                cur = float(close.iloc[cur_idx])
                prev = float(close.iloc[prev_idx])
                if abs(cur - level) <= band:
                    within = True
                if (prev <= level < cur) or (prev >= level > cur):
                    crossed = True
                if within and crossed:
                    return True
            return False
        if fname in {"ema", "sma", "average", "highest", "lowest", "stddev"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if len(args2) != 2:
                raise ValueError(f"{fname}(expr, period[, timeframe]) expects 2 arguments")
            period = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or 0))
            period = max(1, period)
            kind = "ema" if fname == "ema" else "average" if fname == "average" else fname
            return _series_func_value(args2[0], ctx, period, tf_default=tf, stack=stack, kind=kind, shift=shift)
        if fname == "resistance":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 10)) if args2 else 10
            ch = _tv_sr_selected_channel(ctx, "resistance", shift=shift, tf_default=tf, prd=period)
            if not ch:
                return None
            return float(ch["lo"])
        if fname == "resistanceupper":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 10)) if args2 else 10
            zone = _tv_sr_zone_bounds(ctx, "resistance", shift=shift, tf_default=tf, prd=period)
            if not zone:
                return None
            _ch, _lo, hi, _close = zone
            return float(hi)
        if fname == "resistancelower":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 10)) if args2 else 10
            zone = _tv_sr_zone_bounds(ctx, "resistance", shift=shift, tf_default=tf, prd=period)
            if not zone:
                return None
            _ch, lo, _hi, _close = zone
            return float(lo)
        if fname == "support":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 10)) if args2 else 10
            ch = _tv_sr_selected_channel(ctx, "support", shift=shift, tf_default=tf, prd=period)
            if not ch:
                return None
            return float(ch["hi"])
        if fname == "supportupper":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 10)) if args2 else 10
            zone = _tv_sr_zone_bounds(ctx, "support", shift=shift, tf_default=tf, prd=period)
            if not zone:
                return None
            _ch, _lo, hi, _close = zone
            return float(hi)
        if fname == "supportlower":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 10)) if args2 else 10
            zone = _tv_sr_zone_bounds(ctx, "support", shift=shift, tf_default=tf, prd=period)
            if not zone:
                return None
            _ch, lo, _hi, _close = zone
            return float(lo)
        if fname in {"insideresistancezone", "insidesupportzone"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 10)) if args2 else 10
            direction = "resistance" if fname == "insideresistancezone" else "support"
            zone = _tv_sr_zone_bounds(ctx, direction, shift=shift, tf_default=tf, prd=period)
            if not zone:
                return None
            _ch, lo, hi, close = zone
            return lo <= close <= hi
        if fname == "touchcount":
            if not args:
                raise ValueError("TouchCount(level, tolerance, bars[, timeframe]) expects at least one argument")
            args2, tf = _split_timeframe_args(list(args), tf_default)
            level = _level_at(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
            if level is None:
                return None
            tolerance = float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or 0.01) if len(args2) > 1 else 0.01
            bars = int(float(_eval(args2[2], ctx, shift=shift, tf_default=tf, stack=stack) or 60)) if len(args2) > 2 else 60
            return _touch_count(ctx, level, tolerance, bars, shift=shift, tf_default=tf)
        if fname == "breakoutstrength":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            return _breakout_strength(ctx, shift=shift, tf_default=tf)
        if fname == "breakoutage":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if not args2:
                level = _eval(FuncCallNode("resistance", []), ctx, shift=shift, tf_default=tf, stack=stack)
                direction = "above"
                bars = 60
            elif len(args2) == 1:
                level = _level_at(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
                direction = "above"
                bars = 60
            else:
                level = _level_at(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
                direction = str(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or "above")
                bars = int(float(_eval(args2[2], ctx, shift=shift, tf_default=tf, stack=stack) or 60)) if len(args2) > 2 else 60
            if level is None:
                return None
            return _breakout_age(ctx, level, direction, bars, shift=shift, tf_default=tf)
        if fname == "distancefromresistance":
            _, tf = _split_timeframe_args(list(args), tf_default)
            ch = _tv_sr_selected_channel(ctx, "resistance", shift=shift, tf_default=tf)
            if not ch:
                return None
            close = _series_at(ctx, "close", shift=shift, tf_default=tf)
            if close is None:
                return None
            if float(ch["lo"]) <= float(close) <= float(ch["hi"]):
                return 0.0
            if float(close) < float(ch["lo"]):
                return abs((float(ch["lo"]) - float(close)) / abs(float(ch["lo"]))) * 100.0
            return abs((float(close) - float(ch["hi"])) / abs(float(ch["hi"]))) * 100.0
        if fname == "distancefromsupport":
            _, tf = _split_timeframe_args(list(args), tf_default)
            ch = _tv_sr_selected_channel(ctx, "support", shift=shift, tf_default=tf)
            if not ch:
                return None
            close = _series_at(ctx, "close", shift=shift, tf_default=tf)
            if close is None:
                return None
            if float(ch["lo"]) <= float(close) <= float(ch["hi"]):
                return 0.0
            if float(close) > float(ch["hi"]):
                return abs((float(close) - float(ch["hi"])) / abs(float(ch["hi"]))) * 100.0
            return abs((float(ch["lo"]) - float(close)) / abs(float(ch["lo"]))) * 100.0
        if fname == "atr":
            # Raw ATR value (Wilder's smoothing) -- ATRCompression() above
            # already existed for "is ATR compressed vs its own recent
            # average", but nothing returned the plain value itself,
            # needed e.g. to express a distance/gap in "how many ATRs
            # wide" terms for cross-symbol-comparable trend-strength
            # checks (a fixed dollar or even percentage gap threshold
            # means very different things for a low-vol vs high-vol
            # name; dividing by that symbol's own ATR normalizes for it).
            # Reuses the exact same _atr_series() already used by
            # ATRCompression and several internal candle/UAE-framework
            # calculations -- not a second, possibly-inconsistent ATR
            # implementation.
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 14)) if args2 else 14
            period = max(1, period)
            snap = ctx.get("timeframes", {}).get(tf)
            if not snap:
                return None
            high = snap.get("series", {}).get("high")
            low = snap.get("series", {}).get("low")
            close = snap.get("series", {}).get("close")
            if high is None or low is None or close is None:
                return None
            atr = _atr_series(high, low, close, period)
            idx = len(atr) - 1 - shift
            if idx < 0:
                return None
            return round(float(atr.iloc[idx]), 4)
        if fname == "atrcompression":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 14)) if args2 else 14
            period = max(1, period)
            snap = ctx.get("timeframes", {}).get(tf)
            if not snap:
                return None
            high = snap.get("series", {}).get("high")
            low = snap.get("series", {}).get("low")
            close = snap.get("series", {}).get("close")
            if high is None or low is None or close is None:
                return None
            atr = _atr_series(high, low, close, period)
            idx = len(atr) - 1 - shift
            if idx < 0:
                return None
            cur = float(atr.iloc[idx])
            start = max(0, idx - period + 1)
            avg = float(atr.iloc[start:idx + 1].mean()) if idx >= start else None
            return _compression_ratio(cur, avg)
        if fname == "rangecompression":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 20)) if args2 else 20
            period = max(1, period)
            snap = ctx.get("timeframes", {}).get(tf)
            if not snap:
                return None
            high = snap.get("series", {}).get("high")
            low = snap.get("series", {}).get("low")
            if high is None or low is None:
                return None
            rng = (high - low).abs()
            idx = len(rng) - 1 - shift
            if idx < 0:
                return None
            cur = float(rng.iloc[idx])
            start = max(0, idx - period + 1)
            avg = float(rng.iloc[start:idx + 1].mean()) if idx >= start else None
            return _compression_ratio(cur, avg)
        if fname == "volumedryup":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 20)) if args2 else 20
            period = max(1, period)
            snap = ctx.get("timeframes", {}).get(tf)
            if not snap:
                return None
            vol = snap.get("series", {}).get("volume")
            if vol is None:
                return None
            idx = len(vol) - 1 - shift
            if idx < 0:
                return None
            cur = float(vol.iloc[idx])
            start = max(0, idx - period + 1)
            avg = float(vol.iloc[start:idx + 1].mean()) if idx >= start else None
            return _compression_ratio(cur, avg)
        if fname in {"resistancestrength", "supportstrength"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            ch = _tv_sr_selected_channel(ctx, "resistance" if fname == "resistancestrength" else "support", shift=shift, tf_default=tf)
            if not ch:
                return None
            return float(ch["strength"])
        if fname in {"failedbreakoutstrength", "breakoutfailedstrength", "failedbreakdownstrength"}:
            _, tf = _split_timeframe_args(list(args), tf_default)
            ch = _tv_sr_selected_channel(ctx, "support" if fname == "failedbreakdownstrength" else "resistance", shift=shift, tf_default=tf)
            if not ch:
                return None
            # Reuse the TV-style strength and add a small confirmation bonus when price clearly breaks the channel.
            close = _series_at(ctx, "close", shift=shift, tf_default=tf)
            if close is None:
                return None
            lvl_lo = float(ch["lo"])
            lvl_hi = float(ch["hi"])
            spot = float(close)
            if fname == "failedbreakdownstrength":
                # Support failure / reclaim logic.
                broken = spot < lvl_lo or spot > lvl_hi
            else:
                # Resistance failure / breakout logic.
                broken = spot > lvl_hi or spot < lvl_lo
            return float(ch["strength"] + (8.0 if broken else 0.0))
        if fname == "volumeatlevel":
            args2, tf = _split_timeframe_args(list(args), tf_default)
            if not args2:
                return None
            level = _level_at(args2[0], ctx, shift=shift, tf_default=tf, stack=stack)
            if level is None:
                return None
            tolerance = float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or 0.01) if len(args2) > 1 else 0.01
            bars = int(float(_eval(args2[2], ctx, shift=shift, tf_default=tf, stack=stack) or 60)) if len(args2) > 2 else 60
            return _volume_at_level(ctx, level, tolerance, bars, shift=shift, tf_default=tf)
        if fname == "oichange":
            bars = int(float(_eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack))) if args else 1
            bars = max(1, bars)
            now = _opt_hist_at(ctx, "total_oi", shift)
            prev = _opt_hist_at(ctx, "total_oi", shift + bars)
            if now is None or prev is None:
                return None
            return now - prev
        if fname == "oichangepct":
            bars = int(float(_eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack))) if args else 1
            bars = max(1, bars)
            now = _opt_hist_at(ctx, "total_oi", shift)
            prev = _opt_hist_at(ctx, "total_oi", shift + bars)
            if now is None or prev in (None, 0):
                return None
            return ((now - prev) / abs(prev)) * 100.0
        if fname == "pcrchange":
            bars = int(float(_eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack))) if args else 1
            bars = max(1, bars)
            now = _opt_hist_at(ctx, "pcr", shift)
            prev = _opt_hist_at(ctx, "pcr", shift + bars)
            if now is None or prev is None:
                return None
            return now - prev
        if fname == "pcrchangepct":
            bars = int(float(_eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack))) if args else 1
            bars = max(1, bars)
            now = _opt_hist_at(ctx, "pcr", shift)
            prev = _opt_hist_at(ctx, "pcr", shift + bars)
            if now is None or prev in (None, 0):
                return None
            return ((now - prev) / abs(prev)) * 100.0
        if fname in {"rsidiff", "rsidiff90", "rsi_diff"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 90)) if args2 else 90
            period = max(1, period)
            snap = ctx.get("timeframes", {}).get(tf)
            if not snap:
                return None
            close = snap.get("series", {}).get("close")
            if close is None or len(close) <= shift:
                return None
            rsi = _rsi(close, 14)
            # Directly measured (holding a point in time fixed and varying
            # how much history feeds the calculation): with only 2x the
            # period's worth of bars, this can be off from its properly-
            # converged value by 0.5-0.7+ points on typical data -- 2x was
            # too low a bar, not a safe threshold. Convergence to a
            # negligible (<0.01) difference empirically starts around
            # 5.5-6x the period (e.g. ~500-540 bars for the default
            # period=90), so 6x is used here as the "trust this" threshold.
            valid_rsi_bars = int(rsi.notna().sum())
            if valid_rsi_bars < period * 6:
                return None
            diff = rsi - _ema(rsi, period)
            idx = len(diff) - 1 - shift
            if idx < 0 or idx >= len(diff):
                return None
            try:
                return float(diff.iloc[idx])
            except Exception:
                return None
        if fname in {"rsidiff90sma", "rsidiffsma", "rsi_diff_sma"}:
            # Same idea as rsidiff90 above, but rsi14 - SMA(rsi14, period)
            # instead of rsi14 - EMA(rsi14, period). SMA has a hard,
            # finite window -- once every RSI value inside that window is
            # itself already converged, the result is EXACTLY stable, no
            # further drift, unlike EMA which never fully stops drifting
            # (that's what makes EMA need ~6x period; SMA needs far less).
            # Directly measured the same way as rsidiff90's threshold:
            # RSI14 itself needs ~8x its own period (~112 bars) to
            # converge (it's Wilder/EMA-smoothed internally), plus the
            # SMA's own `period`-bar window on top of that. Verified
            # across multiple independent price series to stay within
            # ~0.01 of the fully-converged value at this threshold --
            # for the default period=90 that's 202 bars vs rsidiff90's
            # 540, which is the whole point of this variant: usable on
            # weekly bars (~4 years of data) where rsidiff90 needs 10+.
            args2, tf = _split_timeframe_args(list(args), tf_default)
            period = int(float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 90)) if args2 else 90
            period = max(1, period)
            snap = ctx.get("timeframes", {}).get(tf)
            if not snap:
                return None
            close = snap.get("series", {}).get("close")
            if close is None or len(close) <= shift:
                return None
            rsi = _rsi(close, 14)
            valid_rsi_bars = int(rsi.notna().sum())
            if valid_rsi_bars < (8 * 14 + period):
                return None
            diff = rsi - rsi.rolling(period).mean()
            idx = len(diff) - 1 - shift
            if idx < 0 or idx >= len(diff):
                return None
            try:
                return float(diff.iloc[idx])
            except Exception:
                return None
        if fname in {"pe", "profitmargin", "revenuegrowthpct", "earningsgrowthpct",
                     "analystrec", "analystupside", "epsrevisiontrend", "epsrevisionnet30d",
                     "earningsqualityscore"}:
            # Fundamentals, meant to be combined with technical primitives
            # in the same query -- e.g. RSIdiff90("1d")<-20 and PE()<15
            # and EpsRevisionTrend()!="FALLING" for "oversold technically,
            # cheap, AND analysts aren't losing confidence in it". One
            # cached fetch per symbol backs all of these (see
            # _get_fundamentals_cached above), so using several together
            # in one query costs one yfinance call per symbol, not one
            # per primitive.
            symbol = ctx.get("symbol")
            fd = _get_fundamentals_cached(symbol) if symbol else {}
            if fname == "pe":
                # Optional arg: "fwd" (default) or "trailing"
                which = _eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack) if args else "fwd"
                which = str(which or "fwd").lower()
                val = fd.get("pe_trailing") if which.startswith("trail") else fd.get("pe_fwd")
                return float(val) if val is not None else None
            if fname == "profitmargin":
                v = fd.get("profit_margin")
                return round(float(v), 2) if v is not None else None
            if fname == "revenuegrowthpct":
                v = fd.get("revenue_growth")
                return round(float(v), 2) if v is not None else None
            if fname == "earningsgrowthpct":
                v = fd.get("earnings_growth")
                return round(float(v), 2) if v is not None else None
            if fname == "analystrec":
                # 1=Strong Buy .. 5=Strong Sell, same scale used on the Earnings page
                v = fd.get("rec_mean")
                return round(float(v), 2) if v is not None else None
            if fname == "analystupside":
                v = fd.get("analyst_upside_pct")
                return round(float(v), 2) if v is not None else None
            if fname == "epsrevisiontrend":
                # Returns the string "RISING"/"STABLE"/"FALLING"/None --
                # compare with == in a query, e.g. EpsRevisionTrend()=="FALLING"
                return fd.get("eps_revision_trend")
            if fname == "epsrevisionnet30d":
                # Net analyst up-revisions minus down-revisions in the
                # last 30 days for the current quarter -- negative means
                # more analysts cutting than raising. Same "company vs
                # analyst mismatch" signal now weighted into the
                # Earnings page's outlook score, exposed here as a
                # queryable number instead of only a page you view one
                # symbol at a time.
                v = fd.get("eps_revision_net_30d")
                return int(v) if v is not None else None
            if fname == "earningsqualityscore":
                # Same math as earnings.py's _compute_recommendation_model
                # for 5 of its 6 factors (Business Quality, Earnings,
                # Guidance, Long-term Growth, Near-term Momentum) --
                # deliberately excludes Analyst Sentiment, which stays
                # separately queryable via AnalystRec()/AnalystUpside()
                # rather than being folded into one number. Replicated
                # here (not imported) since the source function takes a
                # differently-shaped data dict built for the Earnings
                # page's own display, not this cached-fundamentals shape
                # -- but every formula below is copied verbatim from it,
                # not re-derived, so the two stay in agreement.
                def _clamp10(x):
                    return max(0.0, min(10.0, round(x, 1)))

                margin = fd.get("profit_margin") or 0
                rev_growth = fd.get("revenue_growth") or 0
                eps_growth = fd.get("earnings_growth") or 0
                beat_rate = fd.get("beat_rate_pct")
                avg_surprise = fd.get("avg_eps_surprise_pct")
                eps_revision_trend = fd.get("eps_revision_trend")
                eps_revision_net_30d = fd.get("eps_revision_net_30d")
                recent_move = fd.get("last_post_earnings_move_pct")

                biz_quality = 5.0
                if margin:
                    biz_quality += min(3.0, margin / 10)
                if rev_growth:
                    biz_quality += min(2.0, rev_growth / 10)
                biz_quality = _clamp10(biz_quality)

                earnings_score = 5.0
                if beat_rate is not None:
                    earnings_score += (beat_rate - 50) / 12.5
                if avg_surprise is not None:
                    earnings_score += max(-2.0, min(2.0, avg_surprise / 5))
                earnings_score = _clamp10(earnings_score)

                guidance_score = 5.0
                if eps_revision_trend == "FALLING":
                    guidance_score -= 3.0
                elif eps_revision_trend == "RISING":
                    guidance_score += 3.0
                if eps_revision_net_30d is not None:
                    guidance_score += max(-2.0, min(2.0, eps_revision_net_30d / 3))
                guidance_score = _clamp10(guidance_score)

                growth_score = 5.0
                if rev_growth:
                    growth_score += min(2.5, rev_growth / 10)
                if eps_growth:
                    growth_score += min(2.5, eps_growth / 20)
                growth_score = _clamp10(growth_score)

                momentum_score = 5.0
                if recent_move is not None:
                    momentum_score += max(-4.0, min(4.0, recent_move / 2.5))
                momentum_score = _clamp10(momentum_score)

                return round((biz_quality + earnings_score + guidance_score + growth_score + momentum_score) / 5, 1)
        if fname in {"insidernetdollars", "insiderdollarsbought", "insiderdollarssold",
                     "insidertransactioncount", "debtchangepct", "debtchangedollars",
                     "debtvalue", "volumepctofavg", "materialeventcount", "hasmaterialevent"}:
            # Real quantitative corporate-events data (actual $ and share
            # figures from SEC Form 4/XBRL, not headline/news text) --
            # same "one cached fetch backs many primitives" shape as the
            # fundamentals block just above, e.g.
            # InsiderNetDollars()>1000000 and DebtChangePct()<-5 for
            # "insiders buying heavily while debt is being paid down".
            # Backed by corporate_events_snapshot, populated by the
            # watchlist scheduler's Corporate Events step -- a symbol
            # that hasn't been fetched by that step yet returns None
            # from every primitive here, not an error, same as any other
            # missing-data case elsewhere in this file.
            symbol = ctx.get("symbol")
            ce = _get_corporate_events_cached(symbol) if symbol else {}
            if fname == "insidernetdollars":
                v = ce.get("insider_net_dollars")
                return round(float(v), 2) if v is not None else None
            if fname == "insiderdollarsbought":
                v = ce.get("insider_dollars_bought")
                return round(float(v), 2) if v is not None else None
            if fname == "insiderdollarssold":
                v = ce.get("insider_dollars_sold")
                return round(float(v), 2) if v is not None else None
            if fname == "insidertransactioncount":
                v = ce.get("insider_transaction_count")
                return int(v) if v is not None else None
            if fname == "debtchangepct":
                v = ce.get("debt_pct_change")
                return round(float(v), 2) if v is not None else None
            if fname == "debtchangedollars":
                v = ce.get("debt_dollar_change")
                return round(float(v), 2) if v is not None else None
            if fname == "debtvalue":
                v = ce.get("debt_latest_value")
                return round(float(v), 2) if v is not None else None
            if fname == "volumepctofavg":
                v = ce.get("volume_pct_of_average")
                return round(float(v), 2) if v is not None else None
            if fname == "materialeventcount":
                raw = ce.get("material_events_json")
                if not raw:
                    return 0
                try:
                    return len(json.loads(raw))
                except Exception:
                    return 0
            if fname == "hasmaterialevent":
                raw = ce.get("material_events_json")
                if not raw:
                    return False
                try:
                    return len(json.loads(raw)) > 0
                except Exception:
                    return False
        if fname in {"isdoji", "ishammer", "isshootingstar",
                     "isbullishengulfing", "isbearishengulfing",
                     "ismorningstar", "iseveningstar",
                     "isthreewhitesoldiers", "isthreeblackcrows",
                     "isbullishharami", "isbearishharami",
                     "ispiercingline", "isdarkcloudcover",
                     "candlepatternbullish", "candlepatternbearish"}:
            # Candlestick pattern detection -- built from the OHLC series
            # already loaded per symbol, no new data source. Optional
            # timeframe arg (default the query's own tf_default), same
            # convention as everything else in this file.
            tf_arg = _eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack) if args else None
            tf_for_pattern = str(tf_arg).strip() if tf_arg else tf_default

            if fname == "isdoji":
                bar = _get_ohlc_bar(ctx, tf_for_pattern, shift)
                return _is_doji(bar) if bar else None
            if fname == "ishammer":
                bar = _get_ohlc_bar(ctx, tf_for_pattern, shift)
                return _is_hammer(bar) if bar else None
            if fname == "isshootingstar":
                bar = _get_ohlc_bar(ctx, tf_for_pattern, shift)
                return _is_shooting_star(bar) if bar else None
            if fname == "isbullishengulfing":
                curr = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=0)
                prev = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=1)
                return _is_bullish_engulfing(prev, curr) if (curr and prev) else None
            if fname == "isbearishengulfing":
                curr = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=0)
                prev = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=1)
                return _is_bearish_engulfing(prev, curr) if (curr and prev) else None
            if fname == "isbullishharami":
                curr = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=0)
                prev = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=1)
                return _is_bullish_harami(prev, curr) if (curr and prev) else None
            if fname == "isbearishharami":
                curr = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=0)
                prev = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=1)
                return _is_bearish_harami(prev, curr) if (curr and prev) else None
            if fname == "ispiercingline":
                curr = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=0)
                prev = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=1)
                return _is_piercing_line(prev, curr) if (curr and prev) else None
            if fname == "isdarkcloudcover":
                curr = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=0)
                prev = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=1)
                return _is_dark_cloud_cover(prev, curr) if (curr and prev) else None
            if fname == "ismorningstar":
                b3 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=0)
                b2 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=1)
                b1 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=2)
                return _is_morning_star(b1, b2, b3) if (b1 and b2 and b3) else None
            if fname == "iseveningstar":
                b3 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=0)
                b2 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=1)
                b1 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=2)
                return _is_evening_star(b1, b2, b3) if (b1 and b2 and b3) else None
            if fname == "isthreewhitesoldiers":
                b3 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=0)
                b2 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=1)
                b1 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=2)
                return _is_three_white_soldiers(b1, b2, b3) if (b1 and b2 and b3) else None
            if fname == "isthreeblackcrows":
                b3 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=0)
                b2 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=1)
                b1 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=2)
                return _is_three_black_crows(b1, b2, b3) if (b1 and b2 and b3) else None
            if fname in {"candlepatternbullish", "candlepatternbearish"}:
                # Category-level scan, matching "Bullish Scans"/"Bearish
                # Scans" -- true if ANY pattern in that direction fires,
                # so a single condition covers the whole category rather
                # than needing every individual pattern OR'd together.
                curr = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=0)
                prev = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=1)
                b1 = _get_ohlc_bar(ctx, tf_for_pattern, shift, bars_back=2)
                if not curr:
                    return None
                if fname == "candlepatternbullish":
                    hits = [_is_hammer(curr)]
                    if prev: hits.append(_is_bullish_engulfing(prev, curr))
                    if prev: hits.append(_is_bullish_harami(prev, curr))
                    if prev: hits.append(_is_piercing_line(prev, curr))
                    if prev: hits.append(_is_three_white_soldiers(b1, prev, curr) if b1 else False)
                    if prev and b1: hits.append(_is_morning_star(b1, prev, curr))
                    return any(hits)
                else:
                    hits = [_is_shooting_star(curr)]
                    if prev: hits.append(_is_bearish_engulfing(prev, curr))
                    if prev: hits.append(_is_bearish_harami(prev, curr))
                    if prev: hits.append(_is_dark_cloud_cover(prev, curr))
                    if prev and b1: hits.append(_is_three_black_crows(b1, prev, curr))
                    if prev and b1: hits.append(_is_evening_star(b1, prev, curr))
                    return any(hits)

        if fname == "trendlinebreak":
            # TrendlineBreak(side, lookback[, break_pct][, tf]) -- side is
            # "resistance" or "support". See _trendline_break()'s
            # docstring for the honest limitation: this is a linear-
            # regression fit across the window, not a swing-point
            # trendline drawn by eye.
            if len(args) < 2:
                raise ValueError('TrendlineBreak(side, lookback[, break_pct][, tf]) requires at least side and lookback, '
                                  'e.g. TrendlineBreak("resistance", 20)')
            side_arg = _eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack)
            side_val = str(side_arg or "").strip().lower()
            if side_val not in {"resistance", "support"}:
                raise ValueError('TrendlineBreak side must be "resistance" or "support"')
            lookback_val = int(float(_eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack) or 20))
            break_pct_val = 0.3
            if len(args) > 2:
                bp = _eval(args[2], ctx, shift=shift, tf_default=tf_default, stack=stack)
                if bp is not None:
                    break_pct_val = float(bp)
            tf_arg2 = _eval(args[3], ctx, shift=shift, tf_default=tf_default, stack=stack) if len(args) > 3 else None
            tf_for_trend = str(tf_arg2).strip() if tf_arg2 else tf_default
            return _trendline_break(ctx, tf_for_trend, shift, side_val, lookback_val, break_pct_val)

        if fname == "chartpattern":
            # ChartPattern([lookback=40][, tf]) -- returns the detected
            # pattern name as a string ("falling_wedge",
            # "symmetrical_triangle", "ascending_triangle",
            # "descending_triangle", "rising_wedge", "rising_channel",
            # "falling_channel") or None if nothing classifies cleanly.
            lookback_val = 40
            if args:
                lb = _eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack)
                if lb is not None:
                    lookback_val = int(float(lb))
            tf_arg = _eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack) if len(args) > 1 else None
            tf_for_pattern = str(tf_arg).strip() if tf_arg else tf_default
            return _chart_pattern_name(ctx, tf_for_pattern, shift, lookback_val)

        if fname == "chartpatternbreakout":
            # ChartPatternBreakout(pattern, [lookback=40][, break_pct=0.3][, tf])
            # -- true only if the pattern is BOTH currently classified
            # AND price has broken out of it in that pattern's typical
            # direction. This is the actual trade signal (the pattern
            # forming is context; the break is the event worth acting
            # on), matching valid pattern names from ChartPattern():
            # "falling_wedge", "rising_wedge", "symmetrical_triangle",
            # "ascending_triangle", "descending_triangle",
            # "rising_channel", "falling_channel".
            #
            # Named ChartPatternBreakout, not PatternBreakout: there's
            # already a PatternBreakout primitive in this file (W/M
            # pattern detection, a different pre-existing feature with a
            # different argument convention) -- reusing that name would
            # have silently collided with it, since _eval checks that
            # handler first and would have swallowed calls meant for
            # this one. Caught via testing, not by inspection.
            if not args:
                raise ValueError('ChartPatternBreakout(pattern[, lookback][, break_pct][, tf]) requires a pattern name, '
                                  'e.g. ChartPatternBreakout("falling_wedge")')
            pattern_arg = _eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack)
            pattern_val = str(pattern_arg or "").strip().lower()
            lookback_val = 40
            if len(args) > 1:
                lb = _eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack)
                if lb is not None:
                    lookback_val = int(float(lb))
            break_pct_val = 0.3
            if len(args) > 2:
                bp = _eval(args[2], ctx, shift=shift, tf_default=tf_default, stack=stack)
                if bp is not None:
                    break_pct_val = float(bp)
            tf_arg2 = _eval(args[3], ctx, shift=shift, tf_default=tf_default, stack=stack) if len(args) > 3 else None
            tf_for_break = str(tf_arg2).strip() if tf_arg2 else tf_default
            return _chart_pattern_breakout(ctx, tf_for_break, shift, pattern_val, lookback_val, break_pct_val)

        if fname in {"isdoubletop", "isdoublebottom"}:
            # IsDoubleTop/IsDoubleBottom([lookback=40][, tolerance_pct=2][, tf])
            kind = "double_top" if fname == "isdoubletop" else "double_bottom"
            lookback_val = 40
            if args:
                lb = _eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack)
                if lb is not None:
                    lookback_val = int(float(lb))
            tol_val = 2.0
            if len(args) > 1:
                t = _eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack)
                if t is not None:
                    tol_val = float(t)
            tf_arg3 = _eval(args[2], ctx, shift=shift, tf_default=tf_default, stack=stack) if len(args) > 2 else None
            tf_for_dt = str(tf_arg3).strip() if tf_arg3 else tf_default
            return _double_top_or_bottom(ctx, tf_for_dt, shift, kind, lookback_val, tol_val)

        if fname in {"watchlistbreadth", "watchlistadvancedecline"}:
            # Watchlist-level (optionally sector-filtered) aggregate --
            # same value for every symbol evaluated in this scan, unlike
            # every other primitive here which is per-symbol. Takes the
            # watchlist by NAME (or id) explicitly rather than trying to
            # auto-detect "the current scan's watchlist", since a scan
            # can run against an ad-hoc symbol list with no watchlist at
            # all, and an explicit reference is unambiguous either way.
            if not args:
                raise ValueError(f"{fname}() requires a watchlist name or id, e.g. {fname}(\"Options Watchlist\")")
            watchlist_ref = _eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack)
            if watchlist_ref is None:
                return None
            if fname == "watchlistbreadth":
                ma_period = 20
                if len(args) > 1:
                    try:
                        ma_period = int(float(_eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack) or 20))
                    except Exception:
                        ma_period = 20
                sector = _eval(args[2], ctx, shift=shift, tf_default=tf_default, stack=stack) if len(args) > 2 else None
                return _compute_watchlist_breadth(str(watchlist_ref), ma_period, str(sector) if sector else None)
            else:  # watchlistadvancedecline
                sector = _eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack) if len(args) > 1 else None
                return _compute_watchlist_advance_decline(str(watchlist_ref), str(sector) if sector else None)
        if fname in {"callwallstrike", "putwallstrike", "callwalloi", "putwalloi",
                     "distancetocallwall", "distancetoputwall", "insideoiwalls",
                     "callwallstrength", "putwallstrength",
                     "callwallbuildup", "putwallbuildup", "totaloi"}:
            # Built on the same get_oi_walls() service used elsewhere in
            # the app (Trade Opportunity Scanner, Iron Condor candidate
            # finder) rather than a separate implementation -- "wall"
            # means the same thing everywhere: the strike with the
            # largest open interest concentration on that side, for the
            # given expiry (or across all expiries if none is given).
            # Cached per-symbol on ctx so a query referencing several of
            # these primitives for the same symbol only hits the
            # database once, not once per primitive.
            symbol = ctx.get("symbol")
            expiry_arg = _eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack) if args else None
            expiry_str = str(expiry_arg).strip() if expiry_arg else None
            days_back = 7
            if fname in {"callwallbuildup", "putwallbuildup"} and len(args) > 1:
                try:
                    days_back = int(float(_eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack) or 7))
                except Exception:
                    days_back = 7
            cache_key = f"_oi_walls:{expiry_str or 'all'}"
            walls = ctx.get(cache_key)
            if walls is None:
                try:
                    from ..services.oi_wall_service import get_oi_walls
                    walls = get_oi_walls(symbol, expiry=expiry_str) or {}
                except Exception:
                    walls = {}
                ctx[cache_key] = walls
            call_walls = walls.get("call_walls") or []
            put_walls = walls.get("put_walls") or []
            spot = ctx.get("price") if ctx.get("price") is not None else ctx.get("close")
            if fname == "callwallstrike":
                return float(call_walls[0]["strike"]) if call_walls else None
            if fname == "putwallstrike":
                return float(put_walls[0]["strike"]) if put_walls else None
            if fname == "callwalloi":
                return float(call_walls[0]["oi"]) if call_walls else None
            if fname == "putwalloi":
                return float(put_walls[0]["oi"]) if put_walls else None
            if fname == "callwallstrength":
                # % of ALL call OI for this expiry sitting at the single
                # wall strike -- this is the actual significance the raw
                # OI count alone can't tell you. 10,000 contracts is a
                # dominant wall on a name where total call OI is 15,000,
                # and background noise on a name where it's 300,000.
                total_call_oi = walls.get("total_call_oi") or 0
                if not call_walls or not total_call_oi:
                    return None
                return round(float(call_walls[0]["oi"]) / float(total_call_oi) * 100.0, 2)
            if fname == "putwallstrength":
                total_put_oi = walls.get("total_put_oi") or 0
                if not put_walls or not total_put_oi:
                    return None
                return round(float(put_walls[0]["oi"]) / float(total_put_oi) * 100.0, 2)
            if fname == "callwallbuildup":
                # % OI change AT THE WALL STRIKE SPECIFICALLY over the
                # last `days_back` days -- distinct from CallWallStrength
                # (how big the wall is right now) and from the whole-
                # expiry aggregate ΔOI chart. A strike can be the biggest
                # wall on the board AND be stale (built up months ago,
                # nobody adding to it this week) or genuinely fresh
                # (rapid recent accumulation) -- this is what tells them
                # apart.
                if not call_walls:
                    return None
                strike = float(call_walls[0]["strike"])
                current_oi = float(call_walls[0]["oi"])
                bkey = f"_wall_buildup:call:{expiry_str or 'all'}:{strike}:{days_back}"
                past_oi = ctx.get(bkey)
                if past_oi is None:
                    try:
                        from ..services.oi_wall_service import get_strike_oi_days_ago
                        past_oi = get_strike_oi_days_ago(symbol, expiry_str, strike, "call", days_back=days_back)
                    except Exception:
                        past_oi = None
                    ctx[bkey] = past_oi if past_oi is not None else -1  # -1 = "looked up, nothing found"
                if not past_oi or past_oi <= 0:
                    return None
                return round((current_oi - past_oi) / past_oi * 100.0, 2)
            if fname == "putwallbuildup":
                if not put_walls:
                    return None
                strike = float(put_walls[0]["strike"])
                current_oi = float(put_walls[0]["oi"])
                bkey = f"_wall_buildup:put:{expiry_str or 'all'}:{strike}:{days_back}"
                past_oi = ctx.get(bkey)
                if past_oi is None:
                    try:
                        from ..services.oi_wall_service import get_strike_oi_days_ago
                        past_oi = get_strike_oi_days_ago(symbol, expiry_str, strike, "put", days_back=days_back)
                    except Exception:
                        past_oi = None
                    ctx[bkey] = past_oi if past_oi is not None else -1
                if not past_oi or past_oi <= 0:
                    return None
                return round((current_oi - past_oi) / past_oi * 100.0, 2)
            if fname == "totaloi":
                # Liquidity floor: total call+put OI for the expiry, so a
                # 30% concentration on a name with only 2,000 contracts
                # total (noise) can be filtered out separately from a 30%
                # concentration on a name with 200,000 (a real signal).
                total_call_oi = walls.get("total_call_oi") or 0
                total_put_oi = walls.get("total_put_oi") or 0
                total = total_call_oi + total_put_oi
                return float(total) if total else None
            if fname == "distancetocallwall":
                if not call_walls or not spot:
                    return None
                return round((float(call_walls[0]["strike"]) - float(spot)) / float(spot) * 100.0, 3)
            if fname == "distancetoputwall":
                if not put_walls or not spot:
                    return None
                return round((float(spot) - float(put_walls[0]["strike"])) / float(spot) * 100.0, 3)
            if fname == "insideoiwalls":
                if not call_walls or not put_walls or not spot:
                    return None
                return float(put_walls[0]["strike"]) < float(spot) < float(call_walls[0]["strike"])
            proximity = float(_eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack) or 2.0) if args else 2.0
            spec = _eval(args[1], ctx, shift=shift, tf_default=tf_default, stack=stack) if len(args) > 1 else "52w"
            if fname == "athdistance":
                return _ath_distance_pct(ctx, spec, shift=shift)
            if fname == "atldistance":
                return _atl_distance_pct(ctx, spec, shift=shift)
            dist = _ath_distance_pct(ctx, spec, shift=shift) if fname == "isath" else _atl_distance_pct(ctx, spec, shift=shift)
            return dist is not None and dist <= proximity
        if fname in {"fibresistance", "fibsupport"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            level = float(_eval(args2[0], ctx, shift=shift, tf_default=tf, stack=stack) or 1.272) if args2 else 1.272
            bars = int(float(_eval(args2[1], ctx, shift=shift, tf_default=tf, stack=stack) or 52)) if len(args2) > 1 else 52
            bars = max(2, bars)
            if fname == "fibresistance":
                return _fib_resistance(ctx, level, bars=bars, tf=tf, shift=shift)
            return _fib_support(ctx, level, bars=bars, tf=tf, shift=shift)
        if fname in {"convictionscore", "conviction_score"}:
            # Syntax: ConvictionScore([side=bull][, timeframe=1d[, lookback=20]])
            side = "bull"
            tf = tf_default
            lookback = 20
            if args:
                side = str(_eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack) or "bull").strip().lower() or "bull"
            remaining = list(args[1:])
            # Pull out timeframe if present.
            for idx, a in enumerate(list(remaining)):
                try:
                    val = _eval(a, ctx, shift=shift, tf_default=tf_default, stack=stack)
                except Exception:
                    val = None
                if isinstance(val, str) and _normalize_tf(val) in TIMEFRAMES:
                    tf = _normalize_tf(val)
                    remaining.pop(idx)
                    break
            if remaining:
                try:
                    lookback = int(float(_eval(remaining[0], ctx, shift=shift, tf_default=tf, stack=stack) or lookback))
                except Exception:
                    lookback = 20
            return _conviction_score(ctx, side, tf=tf, lookback=lookback, shift=shift)
        if fname in {"convictionbreakdown", "conviction_breakdown"}:
            side = "bull"
            tf = tf_default
            lookback = 20
            if args:
                side = str(_eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack) or "bull").strip().lower() or "bull"
            remaining = list(args[1:])
            for idx, a in enumerate(list(remaining)):
                try:
                    val = _eval(a, ctx, shift=shift, tf_default=tf_default, stack=stack)
                except Exception:
                    val = None
                if isinstance(val, str) and _normalize_tf(val) in TIMEFRAMES:
                    tf = _normalize_tf(val)
                    remaining.pop(idx)
                    break
            if remaining:
                try:
                    lookback = int(float(_eval(remaining[0], ctx, shift=shift, tf_default=tf, stack=stack) or lookback))
                except Exception:
                    lookback = 20
            return _conviction_breakdown(ctx, side, tf=tf, lookback=lookback, shift=shift)
        if fname in {"ivrank", "iv_rank"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            tf = _normalize_tf(tf)
            snap = ctx.get("timeframes", {}).get(tf)
            if not snap:
                return None
            flow = _flow_snapshot(ctx, tf=tf)
            return flow.get("iv_rank")
        if fname in {"ivchange", "iv_change"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            tf = _normalize_tf(tf)
            snap = ctx.get("timeframes", {}).get(tf)
            if not snap:
                return None
            flow = _flow_snapshot(ctx, tf=tf)
            return flow.get("iv_change")
        if fname in {"flowscore", "flow_score"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            tf = _normalize_tf(tf)
            snap = ctx.get("timeframes", {}).get(tf)
            if not snap:
                return None
            flow = _flow_snapshot(ctx, tf=tf)
            return flow.get("flow_score")
        if fname in {"flowbias", "flow_bias"}:
            args2, tf = _split_timeframe_args(list(args), tf_default)
            tf = _normalize_tf(tf)
            snap = ctx.get("timeframes", {}).get(tf)
            if not snap:
                return None
            flow = _flow_snapshot(ctx, tf=tf)
            return flow.get("flow_bias")
        if fname in {"pcrshift", "pcr_shift"}:
            bars = int(float(_eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack))) if args else 1
            bars = max(1, bars)
            now = _opt_hist_at(ctx, "pcr", shift)
            prev = _opt_hist_at(ctx, "pcr", shift + bars)
            if now is None or prev is None:
                return None
            return now - prev
        if fname in {"earningsdays", "earningsday", "earn_days"}:
            info = get_earnings_info(ctx.get("symbol") or "") or {}
            days = info.get("earn_days")
            return 999 if days is None else days
        if fname in {"earningsscore", "earnings_score", "earn_score"}:
            info = get_earnings_info(ctx.get("symbol") or "") or {}
            return info.get("earn_score", 0)
        if fname == "beta":
            symbol = ctx.get("symbol")
            return ctx.get("beta") if ctx.get("beta") is not None else (get_beta(symbol) if symbol else None)
        # Unknown functions can still work as wrappers around first arg for text-based usage.
        if len(args) == 1:
            return _eval(args[0], ctx, shift=shift, tf_default=tf_default, stack=stack)
        return None
    return None



def _direct_series_for_identifier(node: Node, ctx: Dict[str, Any], tf_default: str = "1d") -> Optional[pd.Series]:
    """Return a raw historical series for a simple indicator identifier.

    Slope/Change primitives should use the stored timeframe series directly for
    expressions such as close, high, RSI, etc.  This avoids any chance that
    scalar context values or expression materialization flatten the series and
    makes SlopeDeg(close, 10, "1d") exactly use current close vs close[10].
    """
    if not isinstance(node, IdentifierNode):
        return None
    try:
        tf = _normalize_tf(node.tf or tf_default)
        snap = ctx.get("timeframes", {}).get(tf)
        if not snap:
            return None
        series = (snap.get("series") or {}).get(_normalize_indicator(node.name))
        if series is None:
            return None
        return pd.to_numeric(series, errors="coerce")
    except Exception:
        return None


def _collect_function_nodes(node: Node, names: set[str], out: Optional[List[FuncCallNode]] = None) -> List[FuncCallNode]:
    out = out if out is not None else []
    try:
        if isinstance(node, FuncCallNode):
            if node.name.lower().strip() in names:
                out.append(node)
            for a in node.args:
                _collect_function_nodes(a, names, out)
        elif isinstance(node, UnaryNode):
            _collect_function_nodes(node.expr, names, out)
        elif isinstance(node, BinaryNode):
            _collect_function_nodes(node.left, names, out)
            _collect_function_nodes(node.right, names, out)
        elif isinstance(node, CompareNode):
            _collect_function_nodes(node.left, names, out)
            _collect_function_nodes(node.right, names, out)
        elif isinstance(node, IndexNode):
            _collect_function_nodes(node.expr, names, out)
    except Exception:
        pass
    return out


def _build_query_debug_summary(root: Node, snapshots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Summarize key numeric primitive values across the full watchlist.

    The first use-case is SlopeDeg diagnostics.  If a filter matches every
    symbol, this summary exposes whether the primitive truly spans the expected
    range or whether the chosen threshold is too wide for the selected data.
    """
    wanted = {
        "slope", "slopepct", "slopedeg", "slopedegraw", "sloperawdeg",
        "slopepctperbar", "slopedegperbar", "slopeatr", "slopeatrdeg",
        "regslopepct", "regslopedeg", "regressionslopepct", "regressionslopedeg",
        "regslopeatr", "regslopeatrdeg", "regressionslopeatr", "regressionslopeatrdeg",
        "changepct",
    }
    nodes = []
    seen = set()
    for n in _collect_function_nodes(root, wanted):
        txt = _node_to_text(n)
        key = txt.lower()
        if key in seen:
            continue
        seen.add(key)
        nodes.append(n)
    summaries: List[Dict[str, Any]] = []
    for n in nodes[:8]:
        vals: List[float] = []
        examples: List[Dict[str, Any]] = []
        for r in snapshots:
            try:
                v = _eval(n, r, shift=0, tf_default="1d")
                fv = float(v)
                if pd.isna(fv) or not math.isfinite(fv):
                    continue
                vals.append(fv)
                if len(examples) < 5:
                    examples.append({"symbol": r.get("symbol"), "value": round(fv, 4)})
            except Exception:
                continue
        if not vals:
            continue
        vals_sorted = sorted(vals)
        mid = vals_sorted[len(vals_sorted)//2]
        summaries.append({
            "expr": _node_to_text(n),
            "count": len(vals),
            "min": round(vals_sorted[0], 4),
            "median": round(mid, 4),
            "max": round(vals_sorted[-1], 4),
            "avg": round(sum(vals) / len(vals), 4),
            "samples": examples,
        })
    return summaries

def _node_series(node: Node, ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d", stack: Tuple[str, ...] = ()) -> Optional[pd.Series]:
    # Fast path: node is a bare field reference (close, ema20, rsi14,
    # etc., optionally with a [tf] bracket setting node.tf) with no
    # further computation applied to it. The full series for that field
    # already exists in the snapshot -- grab it directly instead of
    # rebuilding it one _eval() call per bar via the general path below.
    # Measured impact: for ema(close, 13, "1d") over ~2800 daily bars,
    # this replaces ~2800 _eval() calls (each doing a pandas .iloc
    # scalar lookup, non-trivial overhead per call) with a single dict
    # lookup. Combined with _series_func_value's own cache (which stops
    # this from being redone per shift value at all), a
    # Lookback(CrossAbove(ema13,ema48) or CrossBelow(ema13,ema48), 10)
    # query went from ~5600 _node_series-driven _eval calls to 2 total.
    # NOTE: node.tf, when set, wins over tf_default -- this matches how
    # the general path below resolves it (via _eval_inner's own
    # IdentifierNode handling), so close[1d] used inside sma(close[1d],
    # 20, "1w") still reads the 1d series, not 1w, exactly as before.
    # Only handles IdentifierNode; numeric-offset brackets like close[5]
    # parse to IndexNode (a real shift, not a plain field) and correctly
    # fall through to the general path, since there's no equivalent
    # precomputed series for "close shifted back 5 bars" to hand back.
    if isinstance(node, IdentifierNode):
        eff_tf = _normalize_tf(node.tf) if node.tf else _normalize_tf(tf_default)
        snap = ctx.get("timeframes", {}).get(eff_tf)
        if snap:
            series = snap.get("series", {}).get(_normalize_indicator(node.name))
            if series is not None:
                try:
                    return series.astype(float)
                except Exception:
                    return series
    # General path: anything else (arithmetic, nested function calls,
    # IndexNode shifts, etc.) -- materialize by evaluating the
    # expression at every index. Unchanged from before.
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    series_map = snap.get("series", {})
    base_len = len(next(iter(series_map.values()))) if series_map else 0
    if base_len <= 0:
        return None

    vals = []
    for i in range(base_len):
        s = base_len - 1 - i
        v = _eval(node, ctx, shift=s, tf_default=tf, stack=stack)
        vals.append(v)
    ser = pd.Series(vals)
    try:
        return ser.astype(float)
    except Exception:
        return ser


def _series_func_value(node: Node, ctx: Dict[str, Any], period: int, tf_default: str = "1d", stack: Tuple[str, ...] = (), kind: str = "sma", shift: int = 0) -> Optional[float]:
    """Computes the FULL ema/sma/highest/lowest/stddev series once per
    (node identity, kind, period, timeframe) and caches it in ctx, then
    just indexes into it for whatever `shift` this particular call needs.

    Without this cache, this function used to redo the entire
    s.ewm(...).mean() / s.rolling(...).mean() pass from scratch on EVERY
    call -- and Lookback(cond, N) calls the same expression at up to N+1
    different shift values (CrossAbove/CrossBelow each need shift AND
    shift+1). A `let`-bound ema13/ema48 referenced inside
    Lookback(CrossAbove(ema13,ema48) or CrossBelow(ema13,ema48), 10) was
    measured hitting this function 10-20 times for ONE symbol, each a
    full ~10ms recompute over a full daily history -- 100-200ms wasted
    per symbol, purely redundant since a single .ewm().mean() call
    already computes every index in one vectorized pass. `let`-binding
    itself was not the bug (it's already correctly deduping CrossAbove
    vs CrossBelow's shared reference at the same shift via _eval's own
    memo) -- this was one level deeper, in the series computation itself
    not being shift-aware. Cache key uses id(node) the same way _eval's
    own memo does, with the same id()-reuse defense: the node is kept
    alive via ctx["_eval_memo_keepalive"] (shared list, already used by
    _eval) for as long as ctx itself lives, so a stale cache hit from a
    deallocated-and-reused address can never happen.
    """
    period = max(1, int(period or 1))
    tf_key = _normalize_tf(tf_default)
    cache = ctx.setdefault("_series_func_cache", {})
    cache_key = (id(node), kind, period, tf_key)
    cached = cache.get(cache_key)
    if cached is not None:
        cached_node, out = cached
        if cached_node is not node:
            cached = None  # id() collision with a deallocated node -- recompute
    if cached is None:
        series = _node_series(node, ctx, shift=0, tf_default=tf_default, stack=stack)
        if series is None or series.empty:
            return None
        s = pd.to_numeric(series, errors="coerce")
        if kind == "ema":
            out = s.ewm(span=period, adjust=False).mean()
        elif kind in {"sma", "average"}:
            out = s.rolling(window=period, min_periods=1).mean()
        elif kind == "highest":
            out = s.rolling(window=period, min_periods=1).max()
        elif kind == "lowest":
            out = s.rolling(window=period, min_periods=1).min()
        elif kind == "stddev":
            out = s.rolling(window=period, min_periods=2).std(ddof=0)
        else:
            return None
        cache[cache_key] = (node, out)
        ctx.setdefault("_eval_memo_keepalive", []).append(node)
    idx = len(out) - 1 - shift
    if idx < 0 or idx >= len(out):
        return None
    try:
        v = out.iloc[idx]
        return None if pd.isna(v) else float(v)
    except Exception:
        return None


def _numeric_list(values: List[Any]) -> List[float]:
    out: List[float] = []
    for v in values:
        try:
            if v is None:
                continue
            fv = float(v)
            if pd.isna(fv):
                continue
            out.append(fv)
        except Exception:
            continue
    return out


def _level_strength_from_prices(ctx: Dict[str, Any], level: float, direction: str, bars: int, tolerance: float, shift: int = 0, tf_default: str = "1d") -> Optional[float]:
    # Lightweight helper for retest / cross logic.
    tf = _normalize_tf(tf_default)
    snap = ctx.get("timeframes", {}).get(tf)
    if not snap:
        return None
    close = snap.get("series", {}).get("close")
    if close is None:
        return None
    bars = max(1, int(bars or 1))
    tolerance = abs(float(tolerance or 0.0))
    total = min(bars, len(close) - shift)
    if total <= 0:
        return None
    crosses = 0
    retests = 0
    for i in range(total - 1):
        idx = len(close) - 1 - shift - i
        prev_idx = idx - 1
        if prev_idx < 0:
            break
        cur = float(close.iloc[idx])
        prev = float(close.iloc[prev_idx])
        if direction == "above":
            if prev <= level and cur > level:
                crosses += 1
        elif direction == "below":
            if prev >= level and cur < level:
                crosses += 1
        else:
            if (prev <= level < cur) or (prev >= level > cur):
                crosses += 1
        band = abs(level) * (tolerance / 100.0) if level else tolerance / 100.0
        if abs(cur - level) <= band:
            retests += 1
    score = min(100.0, (crosses * 40.0) + (retests * 20.0) + max(0.0, 100.0 - abs(float(close.iloc[len(close) - 1 - shift]) - level) / (abs(level) or 1.0) * 100.0))
    return score


def _cross_value(ctx: Dict[str, Any], left: Node, right: Node, shift: int = 0, tf_default: str = "1d", stack: Tuple[str, ...] = (), above: bool = True) -> Optional[bool]:
    cur_left = _eval(left, ctx, shift=shift, tf_default=tf_default, stack=stack)
    cur_right = _eval(right, ctx, shift=shift, tf_default=tf_default, stack=stack)
    prev_left = _eval(left, ctx, shift=shift + 1, tf_default=tf_default, stack=stack)
    prev_right = _eval(right, ctx, shift=shift + 1, tf_default=tf_default, stack=stack)
    if None in {cur_left, cur_right, prev_left, prev_right}:
        return None
    if above:
        return float(prev_left) <= float(prev_right) and float(cur_left) > float(cur_right)
    return float(prev_left) >= float(prev_right) and float(cur_left) < float(cur_right)


def _explain(node: Node, ctx: Dict[str, Any], shift: int = 0, tf_default: str = "1d", stack: Tuple[str, ...] = ()) -> List[str]:
    if isinstance(node, BinaryNode) and node.op in {"AND", "OR"}:
        return _explain(node.left, ctx, shift=shift, tf_default=tf_default, stack=stack) + _explain(node.right, ctx, shift=shift, tf_default=tf_default, stack=stack)
    if isinstance(node, UnaryNode):
        val = _eval(node, ctx, shift=shift, tf_default=tf_default, stack=stack)
        return [f"{_node_to_text(node)} => {'OK' if val else 'NO'}"]
    if isinstance(node, CompareNode):
        val = _eval(node, ctx, shift=shift, tf_default=tf_default, stack=stack)
        left_val = _eval(node.left, ctx, shift=shift, tf_default=tf_default, stack=stack)
        right_val = _eval(node.right, ctx, shift=shift, tf_default=tf_default, stack=stack)
        return [f"{_node_to_text(node)} | left={left_val} right={right_val} => {'OK' if val else 'NO'}"]
    if isinstance(node, FuncCallNode):
        val = _eval(node, ctx, shift=shift, tf_default=tf_default, stack=stack)
        if node.name.lower().strip() in {"lookback", "within"} and len(node.args) >= 2:
            try:
                bars = int(float(_eval(node.args[1], ctx, shift=shift, tf_default=tf_default, stack=stack) or 0))
                bars = max(1, bars)
                supported, age, label = _uae_event_age_for_node(node.args[0], ctx, bars, shift=shift, tf_default=tf_default, stack=stack)
                if supported:
                    detail = f"{label} age {age} bars" if age is not None else f"no {label} within {bars} bars"
                    return [f"{_node_to_text(node)} => {'OK' if val else 'NO'} ({detail})"]
                matched = None
                for i in range(bars):
                    if bool(_eval(node.args[0], ctx, shift=shift + i, tf_default=tf_default, stack=stack)):
                        matched = i
                        break
                detail = f"matched {matched} bars ago" if matched is not None else "no matching bar"
                return [f"{_node_to_text(node)} => {'OK' if val else 'NO'} ({detail})"]
            except Exception:
                pass
        if isinstance(val, bool):
            return [f"{_node_to_text(node)} => {'OK' if val else 'NO'}"]
        return [f"{_node_to_text(node)} => {val}"]
    if isinstance(node, IdentifierNode):
        val = _eval(node, ctx, shift=shift, tf_default=tf_default, stack=stack)
        return [f"{_node_to_text(node)} => {val}"]
    return [_node_to_text(node)]


# ---------------------------------------------------------------------------
# Scanner engine / routes
# ---------------------------------------------------------------------------

def _scan_symbol(symbol: str, root: Node, benchmark: str, required_tfs: List[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        ctx = _symbol_ctx(symbol, benchmark, required_tfs)
        return ctx, None
    except Exception as e:
        return None, str(e)


_DOUBLE_PATTERN_NAMES = {"double_top", "double_bottom"}


def _check_symbol_patterns(sym: str, benchmark: str, tf: str, patterns: List[str],
                            lookback: int, only_breakouts: bool):
    """Runs every requested pattern check against one symbol's snapshot.
    Returns None if nothing matches, otherwise a dict with the symbol's
    LTP and every matching pattern's detail (including the fitted line
    parameters the chart overlay needs to actually draw the pattern)."""
    ctx, err = _scan_symbol(sym, None, benchmark, [tf])
    if not ctx:
        return None
    matches = []
    for p in patterns:
        try:
            if p in _DOUBLE_PATTERN_NAMES:
                detail = _double_top_or_bottom(ctx, tf, 0, p, lookback, return_detail=True)
                if detail and detail.get("hit"):
                    matches.append({"pattern": p, "is_breakout": None, "fit": None, "double_detail": detail})
            else:
                detected = _chart_pattern_name(ctx, tf, 0, lookback)
                if detected != p:
                    continue
                breakout = _chart_pattern_breakout(ctx, tf, 0, p, lookback)
                if only_breakouts and not breakout:
                    continue
                fit = _fit_pattern_lines(ctx, tf, 0, lookback)
                matches.append({"pattern": p, "is_breakout": bool(breakout), "fit": fit})
        except Exception:
            continue
    if not matches:
        return None
    tf_norm = _normalize_tf(tf)
    snap = ctx.get("timeframes", {}).get(tf_norm, {})
    close_series = snap.get("series", {}).get("close")
    ltp = None
    if close_series is not None and len(close_series):
        try:
            ltp = float(close_series.iloc[-1])
        except Exception:
            ltp = None
    return {"symbol": sym, "ltp": ltp, "matches": matches}


@scanner_builder_bp.route("/api/pattern_search", methods=["POST"])
def api_pattern_search():
    """Scan a watchlist for one or more chart patterns -- Doji-style
    single-bar candles aren't included here (those are already a
    trivial per-bar check via the existing IsHammer()-etc. scanner
    primitives); this endpoint is specifically for the multi-bar
    geometric patterns (triangles, wedges, channels, double top/bottom)
    that benefit from a dedicated search UI with a chart overlay,
    matching the "search for patterns, click to see the chart" request.

    only_breakouts (default True): restrict results to symbols where
    the pattern has actually broken out, not just symbols where it's
    currently forming -- the pattern forming is context, the break is
    the trade signal.
    """
    from concurrent.futures import as_completed, TimeoutError as _cf_TimeoutError
    from ..services.task_executor import get_executor
    from ..services import unified_scheduler

    _ensure_tables()
    body = request.get_json(force=True, silent=True) or {}
    watchlist_id = body.get("watchlist_id")
    patterns = [str(p).strip().lower() for p in (body.get("patterns") or []) if p]
    only_breakouts = bool(body.get("only_breakouts", True))
    lookback = int(body.get("lookback") or 40)
    tf = str(body.get("timeframe") or "1d").strip() or "1d"
    benchmark = str(body.get("benchmark") or "SPY").strip().upper() or "SPY"

    valid_patterns = {"falling_wedge", "rising_wedge", "symmetrical_triangle",
                       "ascending_triangle", "descending_triangle",
                       "rising_channel", "falling_channel",
                       "double_top", "double_bottom"}
    unknown = [p for p in patterns if p not in valid_patterns]
    if unknown:
        return jsonify({"error": f"unknown pattern(s): {', '.join(unknown)}. "
                                  f"Valid: {', '.join(sorted(valid_patterns))}"}), 400
    if not patterns:
        return jsonify({"error": "at least one pattern must be selected"}), 400

    symbols = _watchlist_symbols(watchlist_id)
    if not symbols:
        return jsonify({"error": "no symbols found for the selected watchlist"}), 400

    try:
        print(f"[scanner_builder] pattern_search triggered from <{request.remote_addr}> | "
              f"patterns={patterns} | only_breakouts={only_breakouts} | watchlist_id={watchlist_id}")
    except Exception:
        pass

    _reset_enqueued_backfill_tracking()
    _bulk_preload_daily_history(symbols)

    results: List[Dict[str, Any]] = []
    unified_scheduler.scan_started()
    try:
        ex = get_executor()
        futs = {ex.submit(_check_symbol_patterns, sym, benchmark, tf, patterns, lookback, only_breakouts): sym
                for sym in symbols}
        try:
            for fut in as_completed(futs, timeout=SCAN_DEADLINE_SECONDS):
                try:
                    r = fut.result()
                except Exception:
                    r = None
                if r:
                    results.append(r)
        except _cf_TimeoutError:
            pass  # whatever finished in time is returned; same "partial results over hanging" philosophy as api_run
    finally:
        unified_scheduler.scan_finished()

    return jsonify({"count": len(results), "results": results})


@scanner_builder_bp.route("/api/settings/timeout", methods=["GET"])
def api_get_timeout_setting():
    """Global fetch timeout (seconds) used by every scanner-related page's
    front-end api() helper (Scanner Builder, Scanner Dashboard, Signal
    Notifier, AI Copilot). Stored once here so it only needs to be set in
    one place."""
    from .watchlist_manager import _get_setting
    sec = int(_get_setting("scanner_api_timeout_sec", "120") or 120)
    return jsonify({"timeout_sec": sec})


@scanner_builder_bp.route("/api/settings/timeout", methods=["POST"])
def api_set_timeout_setting():
    from .watchlist_manager import _set_setting
    body = request.get_json(force=True, silent=True) or {}
    try:
        sec = max(10, min(600, int(body.get("timeout_sec") or 120)))
    except Exception:
        return jsonify({"ok": False, "error": "timeout_sec must be a number"}), 400
    _set_setting("scanner_api_timeout_sec", str(sec))
    return jsonify({"ok": True, "timeout_sec": sec})


@scanner_builder_bp.route("/")
def page():
    _ensure_tables()
    return render_template("scanner_builder.html")


@scanner_builder_bp.route("/api/watchlists", methods=["GET"])
def api_watchlists():
    _ensure_tables()
    return jsonify({"watchlists": _watchlists()})


@scanner_builder_bp.route("/api/function_catalog", methods=["GET"])
def api_function_catalog():
    """Static registry data only -- FUNCTION_CATALOG and BUILTIN_SCANNERS
    are both plain in-memory Python lists, no database access needed at
    all. Split out from /api/catalog specifically for
    scanner_primitives_guide.py, which previously called the heavier
    endpoint and picked function_meta/builtin_scanners back out of a
    response that also runs a full saved_scanners DB query it never
    uses -- meaning a transient DB issue (lock contention, a missing
    table) could break the primitives reference page even though every
    byte of data it actually needs requires no database at all.
    """
    return jsonify({"function_meta": FUNCTION_CATALOG, "builtin_scanners": BUILTIN_SCANNERS})


@scanner_builder_bp.route("/api/catalog", methods=["GET"])
def api_catalog():
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute(
            """
            SELECT id, name, description, query_text, watchlist_id, benchmark, updated_at, last_run_count, result_columns_json, result_template_id
            FROM scanner_definitions
            ORDER BY lower(name)
            """
        ).fetchall()
        return jsonify({
            "saved_scanners": [dict(r) for r in rows],
            "builtin_scanners": BUILTIN_SCANNERS,
            "function_meta": FUNCTION_CATALOG,
            "functions": [item["signature"] for item in FUNCTION_CATALOG],
            "timeframes": TIMEFRAMES,
            "indicators": INDICATORS,
            "column_primitives": _column_primitives_payload(),
            "column_templates": _column_template_rows(),
        })
    finally:
        con.close()


@scanner_builder_bp.route("/api/column-templates", methods=["GET"])
@scanner_builder_bp.route("/api/result-column-templates", methods=["GET"])
def api_column_templates_list():
    _ensure_tables()
    return jsonify({
        "templates": _column_template_rows(),
        "default_columns": _default_result_columns(),
        "column_primitives": _column_primitives_payload(),
        "function_meta": FUNCTION_CATALOG,
        "timeframes": TIMEFRAMES,
        "indicators": INDICATORS,
    })


@scanner_builder_bp.route("/api/column-templates", methods=["POST"])
@scanner_builder_bp.route("/api/result-column-templates", methods=["POST"])
def api_column_templates_create():
    _ensure_tables()
    d = request.get_json(force=True) or {}
    name = str(d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    desc = str(d.get("description") or "").strip()
    cols = _normalize_result_columns(d.get("columns") or d.get("columns_json"), fallback=DEFAULT_RESULT_COLUMNS)
    is_default = int(bool(d.get("is_default")))
    con = _conn()
    try:
        if is_default:
            con.execute("UPDATE scanner_column_templates SET is_default=0")
        con.execute(
            """
            INSERT INTO scanner_column_templates (name, description, columns_json, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(name) DO UPDATE SET
              description=excluded.description,
              columns_json=excluded.columns_json,
              is_default=excluded.is_default,
              updated_at=datetime('now')
            """,
            (name, desc, json.dumps(cols), is_default),
        )
        con.commit()
        row = con.execute("SELECT id FROM scanner_column_templates WHERE lower(name)=lower(?)", (name,)).fetchone()
        return jsonify({"ok": True, "template_id": row[0] if row else None, "templates": _column_template_rows()})
    finally:
        con.close()


@scanner_builder_bp.route("/api/column-templates/<int:template_id>", methods=["PUT"])
@scanner_builder_bp.route("/api/result-column-templates/<int:template_id>", methods=["PUT"])
def api_column_templates_update(template_id: int):
    _ensure_tables()
    d = request.get_json(force=True) or {}
    fields = []
    vals: List[Any] = []
    if "name" in d:
        name = str(d.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name cannot be blank"}), 400
        fields.append("name=?")
        vals.append(name)
    if "description" in d:
        fields.append("description=?")
        vals.append(str(d.get("description") or ""))
    if "columns" in d or "columns_json" in d:
        cols = _normalize_result_columns(d.get("columns") if "columns" in d else d.get("columns_json"), fallback=DEFAULT_RESULT_COLUMNS)
        fields.append("columns_json=?")
        vals.append(json.dumps(cols))
    if "is_default" in d:
        is_default = int(bool(d.get("is_default")))
        fields.append("is_default=?")
        vals.append(is_default)
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    con = _conn()
    try:
        if int(bool(d.get("is_default"))):
            con.execute("UPDATE scanner_column_templates SET is_default=0 WHERE id<>?", (template_id,))
        vals.append(template_id)
        con.execute(f"UPDATE scanner_column_templates SET {', '.join(fields)}, updated_at=datetime('now') WHERE id=?", vals)
        con.commit()
        return jsonify({"ok": True, "templates": _column_template_rows()})
    finally:
        con.close()


@scanner_builder_bp.route("/api/column-templates/<int:template_id>", methods=["DELETE"])
@scanner_builder_bp.route("/api/result-column-templates/<int:template_id>", methods=["DELETE"])
def api_column_templates_delete(template_id: int):
    _ensure_tables()
    con = _conn()
    try:
        con.execute("DELETE FROM scanner_column_templates WHERE id=?", (template_id,))
        con.commit()
        return jsonify({"ok": True, "templates": _column_template_rows()})
    finally:
        con.close()


@scanner_builder_bp.route("/api/definitions", methods=["GET"])
def api_definitions_list():
    _ensure_tables()
    con = _conn()
    try:
        rows = con.execute(
            """
            SELECT id, name, description, query_text, builder_json, watchlist_id, benchmark,
                   created_at, updated_at, last_run_at, last_run_count, last_error, result_columns_json, result_template_id
            FROM scanner_definitions
            ORDER BY updated_at DESC, created_at DESC
            """
        ).fetchall()
        return jsonify({"definitions": [dict(r) for r in rows]})
    finally:
        con.close()


@scanner_builder_bp.route("/api/definitions", methods=["POST"])
def api_definitions_create():
    _ensure_tables()
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip()
    query_text = (d.get("query_text") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not query_text:
        return jsonify({"error": "query_text is required"}), 400
    builder_json = d.get("builder_json")
    if builder_json is None:
        builder_json = []
    if not isinstance(builder_json, str):
        builder_json = json.dumps(builder_json)
    watchlist_id = d.get("watchlist_id")
    benchmark = (d.get("benchmark") or "SPY").strip().upper() or "SPY"
    desc = (d.get("description") or "").strip()
    result_columns_json = d.get("result_columns_json")
    if result_columns_json is None:
        result_columns_json = d.get("columns") or []
    if not isinstance(result_columns_json, str):
        result_columns_json = json.dumps(_normalize_result_columns(result_columns_json, fallback=None))
    result_template_id = d.get("result_template_id") if d.get("result_template_id") not in ("", None) else None

    con = _conn()
    try:
        con.execute(
            """
            INSERT INTO scanner_definitions
              (name, description, query_text, builder_json, watchlist_id, benchmark, result_columns_json, result_template_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (name, desc, query_text, builder_json, watchlist_id, benchmark, result_columns_json, result_template_id),
        )
        con.commit()
        _scanner_query_text.cache_clear()
        row = con.execute("SELECT * FROM scanner_definitions WHERE name=?", (name,)).fetchone()
        return jsonify({"ok": True, "definition": dict(row)})
    except sqlite3.IntegrityError:
        return jsonify({"error": f"scanner '{name}' already exists"}), 409
    finally:
        con.close()


@scanner_builder_bp.route("/api/definitions/<int:def_id>", methods=["PUT"])
def api_definitions_update(def_id: int):
    _ensure_tables()
    d = request.get_json(force=True) or {}
    fields = []
    vals = []
    for key in ("name", "description", "query_text", "builder_json", "watchlist_id", "benchmark", "result_columns_json", "result_template_id"):
        if key in d:
            if key in {"builder_json", "result_columns_json"} and not isinstance(d[key], str):
                vals.append(json.dumps(d[key]))
            elif key == "result_template_id":
                vals.append(d[key] if d[key] not in ("", None) else None)
            elif key == "benchmark":
                vals.append((d[key] or "SPY").strip().upper() or "SPY")
            else:
                vals.append(d[key])
            fields.append(f"{key}=?")
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    vals.append(def_id)
    con = _conn()
    try:
        con.execute(f"UPDATE scanner_definitions SET {', '.join(fields)}, updated_at=datetime('now') WHERE id=?", vals)
        con.commit()
        _scanner_query_text.cache_clear()
        row = con.execute("SELECT * FROM scanner_definitions WHERE id=?", (def_id,)).fetchone()
        return jsonify({"ok": True, "definition": dict(row) if row else None})
    finally:
        con.close()


@scanner_builder_bp.route("/api/definitions/<int:def_id>", methods=["DELETE"])
def api_definitions_delete(def_id: int):
    _ensure_tables()
    con = _conn()
    try:
        con.execute("DELETE FROM scanner_definitions WHERE id=?", (def_id,))
        con.commit()
        _scanner_query_text.cache_clear()
        return jsonify({"ok": True})
    finally:
        con.close()


@scanner_builder_bp.route("/api/run", methods=["POST"])
def api_run():
    from ..services import profiling
    _req_timer = profiling.Timer()
    _req_timer.__enter__()
    _req_bucket = profiling.request_scope_start()

    _ensure_tables()
    payload = request.get_json(force=True) or {}
    query_text = (payload.get("query_text") or "").strip()
    benchmark = (payload.get("benchmark") or "SPY").strip().upper() or "SPY"
    watchlist_id = payload.get("watchlist_id")
    # Explicit symbol override -- lets a caller scope a live run to one
    # or more specific symbols regardless of watchlist membership (e.g.
    # an index like NIFTY that isn't in any equity watchlist). Matches
    # the same pattern backtest.py's _run_forward_return_backtest
    # already uses for its own "symbol" field. Optional/additive --
    # existing callers that only send watchlist_id are unaffected.
    raw_symbol_field = str(payload.get("symbol") or "").strip()
    explicit_symbols = [s.strip().upper() for s in raw_symbol_field.split(",") if s.strip()]
    limit = int(payload.get("limit") or 250)
    # Attribution logging -- this endpoint gets called from several
    # places (manual Scanner Builder runs, Scanner Dashboard tiles,
    # saved-scanner alert checks), and "what's triggering this scan
    # automatically" has come up before. Referer alone answers it:
    # scanner-builder page vs scanner_dashboard vs anything else.
    try:
        print(f"[scanner_builder] api_run triggered from <{request.remote_addr}> | "
              f"Referer: {request.headers.get('Referer', '(none)')} | watchlist_id={watchlist_id}")
    except Exception:
        pass
    if not query_text:
        profiling.request_scope_end()
        return jsonify({"error": "query_text is required"}), 400

    try:
        raw_root = _parse_query(query_text)
        root = _expand_scan_nodes(raw_root, ())
    except Exception as e:
        profiling.request_scope_end()
        return jsonify({"error": str(e)}), 400

    definition_id = payload.get("definition_id")
    requested_columns = payload.get("columns") or payload.get("result_columns") or payload.get("result_columns_json")
    requested_template_id = payload.get("result_template_id") or payload.get("column_template_id")
    result_template_id: Optional[int] = None
    if requested_columns:
        result_columns = _normalize_result_columns(requested_columns, fallback=DEFAULT_RESULT_COLUMNS)
        try:
            result_template_id = int(requested_template_id) if requested_template_id not in (None, "") else None
        except Exception:
            result_template_id = None
    elif requested_template_id:
        result_columns = _columns_from_template_id(requested_template_id)
        try:
            result_template_id = int(requested_template_id)
        except Exception:
            result_template_id = None
    elif definition_id:
        result_columns, result_template_id = _columns_from_definition(definition_id)
    else:
        result_columns = []
    if not result_columns:
        default_templates = _column_template_rows()
        default_template = next((t for t in default_templates if int(t.get("is_default") or 0) == 1), None)
        if default_template:
            result_columns = default_template.get("columns") or _default_result_columns()
            result_template_id = default_template.get("id")
        else:
            result_columns = _default_result_columns()
    parsed_columns = _parse_column_nodes(result_columns)

    if explicit_symbols:
        symbols = explicit_symbols
    else:
        if not watchlist_id:
            watchlist_id = _preferred_watchlist_id()
        symbols = _watchlist_symbols(watchlist_id)
    if not symbols:
        profiling.request_scope_end()
        return jsonify({"error": "No symbols found for the selected watchlist"}), 400

    symbols = list(dict.fromkeys(symbols))
    req_tfs = sorted(set(_required_timeframes(root)) | set(_result_column_required_tfs(parsed_columns)), key=lambda tf: TIMEFRAMES.index(tf) if tf in TIMEFRAMES else 99)

    # Staged pre-filter: run any snapshot-free conditions (Sector(),
    # EarningsDays(), fundamentals, options-wall primitives) across the
    # whole symbol list BEFORE bulk-loading price history for any of
    # them. Only symbols that survive this cheap pass get the expensive
    # work below done for them at all -- for a query like
    # "Sector()='XLK' and EarningsDays()>30 and <price-dependent stuff>"
    # against a 200-symbol watchlist, this can mean loading price
    # history for 15 symbols instead of 200.
    _prefilter_node, _ = split_prefilter_conditions(root)
    if _prefilter_node is not None:
        _before_prefilter = len(symbols)
        symbols = prefilter_symbols(_prefilter_node, symbols)
        print(f"[scanner_builder] pre-filter: {_before_prefilter} -> {len(symbols)} symbol(s) "
              f"before loading any price history")
        if not symbols:
            profiling.request_scope_end()
            return jsonify({"count": 0, "results": [], "errors": [], "timed_out_symbols": [],
                             "note": f"0 of {_before_prefilter} symbols passed the snapshot-free "
                                     f"pre-filter conditions -- no price history was loaded."})

    _reset_enqueued_backfill_tracking()

    from concurrent.futures import as_completed, TimeoutError as _cf_TimeoutError
    from ..services import unified_scheduler
    from ..services.task_executor import get_executor

    # Bulk-preload daily price history for every symbol in this scan with a
    # SINGLE "WHERE symbol IN (...)" query instead of letting each of the
    # (up to 8 concurrent) _scan_symbol() workers open its own connection
    # and issue its own per-symbol SELECT on a cache miss. Warms the
    # existing lru_cache'd _local_daily_history_cached() so per-symbol
    # lookups below hit memory, not SQLite.
    _bulk_preload_daily_history(symbols)
    if any(tf in ("1h", "2h", "4h") for tf in req_tfs):
        _bulk_preload_intraday_history(symbols)

    snapshots: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    timed_out_symbols: List[str] = []

    # Tell the shared Scheduler Hub dispatcher (unified_scheduler.py) that
    # an interactive scan is active -- low_priority jobs (backfill/refresh
    # sweeps) are skipped for the duration instead of competing with this
    # scan for SQLite I/O and worker threads.
    unified_scheduler.scan_started()
    try:
        ex = get_executor()
        futs = {ex.submit(_scan_symbol, sym, root, benchmark, req_tfs): sym for sym in symbols}
        # Hard ceiling on the WHOLE scan, not just per-symbol: without
        # this, a single symbol whose worker thread genuinely hangs (not
        # slow -- actually stuck, e.g. a network call that never times
        # out at the OS/socket level, or a lock that never releases)
        # blocks this loop FOREVER, since as_completed() with no timeout
        # waits for every future unconditionally. That's how a "simple"
        # query can show "scanning" for 10 hours -- this scan had zero
        # ability to give up. Whatever hasn't finished by the deadline is
        # reported as timed-out instead of silently hanging; the workers
        # for those symbols keep running in the background (Python can't
        # forcibly kill a thread) but they no longer hold this response
        # hostage.
        try:
            for fut in as_completed(futs, timeout=SCAN_DEADLINE_SECONDS):
                sym = futs[fut]
                try:
                    res, err = fut.result()
                except Exception as e:
                    res, err = None, str(e)
                if res:
                    snapshots.append(res)
                elif err:
                    errors.append({"symbol": sym, "error": err})
        except _cf_TimeoutError:
            done_syms = {futs[f] for f in futs if f.done()}
            timed_out_symbols = [s for s in symbols if s not in done_syms]
            print(f"[scanner_builder] scan hit its {SCAN_DEADLINE_SECONDS}s deadline with "
                  f"{len(timed_out_symbols)}/{len(symbols)} symbol(s) still not done -- "
                  f"returning partial results instead of hanging. Stuck symbols: "
                  f"{timed_out_symbols[:20]}{'...' if len(timed_out_symbols) > 20 else ''}")
            # Do not replace the shared pool here: replacement leaves the old
            # workers running, which doubles the number of live scanner jobs
            # and can keep database writers alive long after this response.
            # Cancel tasks that never started; running network calls cannot be
            # force-killed by Python, but they will no longer be followed by a
            # backlog of queued symbols from this timed-out request.
            cancelled = sum(1 for fut in futs if not fut.done() and fut.cancel())
            if cancelled:
                print(f"[scanner_builder] cancelled {cancelled} queued symbol task(s) after scan deadline")
    finally:
        unified_scheduler.scan_finished()

    rs_vals = [r.get("relative_strength") for r in snapshots if r.get("relative_strength") is not None]
    rs_sorted = sorted(rs_vals)
    n = len(rs_sorted)
    if n:
        for r in snapshots:
            rs = r.get("relative_strength")
            if rs is None:
                continue
            pct = sum(1 for v in rs_sorted if v <= rs) / n
            r["leadership"] = int(round(pct * 100))

    rsrank_periods = sorted(set(_collect_function_periods(root, {"rsrank", "rs_rank"})) | _collect_rsrank_periods_for_nodes([c.get("node") for c in parsed_columns if c.get("node") is not None]))
    for period in rsrank_periods:
        base_vals: List[float] = []
        for r in snapshots:
            try:
                v = _relative_strength_value(r, benchmark, period, tf='1d', shift=0)
            except Exception:
                v = None
            r[f"_rsrank_source_{period}"] = v
            if v is not None:
                base_vals.append(v)
        base_vals_sorted = sorted(base_vals)
        if not base_vals_sorted:
            continue
        total = len(base_vals_sorted)
        for r in snapshots:
            v = r.get(f"_rsrank_source_{period}")
            if v is None:
                continue
            pct = sum(1 for x in base_vals_sorted if x <= v) / total
            r[f"rs_rank_{period}"] = int(round(pct * 100))

    # Optimized once per scan (not per symbol -- reordering is a query-
    # level operation, the same rewritten tree applies to every symbol).
    # Used ONLY for the pass/fail check below; _explain() below still
    # uses the ORIGINAL, un-reordered `root` so the "reason" text a user
    # sees reflects the query the way they actually wrote it, not the
    # internal evaluation order.
    try:
        _optimized_root = optimize_query(root)
    except Exception:
        _optimized_root = root  # never let a rewrite bug block scanning -- fall back to the original tree

    passed: List[Dict[str, Any]] = []
    for r in snapshots:
        try:
            ok = bool(_eval(_optimized_root, r, shift=0, tf_default="1d"))
        except Exception as e:
            ok = False
            r.setdefault("scan_error", str(e))
        if ok:
            r["reason"] = _explain(root, r, shift=0, tf_default="1d")
            passed.append(r)

    passed.sort(key=lambda x: (x.get("leadership") or 0, x.get("relative_strength") or 0), reverse=True)
    if limit > 0:
        passed = passed[:limit]

    for r in passed:
        r["_result_columns"] = _eval_result_columns(r, parsed_columns)

    summary = _build_scan_summary(passed)
    query_debug = _build_query_debug_summary(root, snapshots)
    backfill_queued = _get_enqueued_backfill_summary()

    if definition_id:
        try:
            con = _conn()
            try:
                con.execute(
                    """
                    UPDATE scanner_definitions
                    SET last_run_at=datetime('now'), last_run_count=?, last_results_json=?, last_error=NULL,
                        result_columns_json=COALESCE(NULLIF(result_columns_json, ''), ?),
                        result_template_id=COALESCE(result_template_id, ?),
                        updated_at=datetime('now')
                    WHERE id=?
                    """,
                    (len(passed), json.dumps(passed[:200]), json.dumps(result_columns), result_template_id, definition_id),
                )
                con.execute(
                    """
                    INSERT INTO scanner_runs(definition_id, watchlist_id, benchmark, query_text, result_count, results_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (definition_id, watchlist_id, benchmark, query_text, len(passed), json.dumps(passed[:200])),
                )
                con.commit()
            finally:
                con.close()
        except Exception:
            pass

    for r in passed:
        r.pop("timeframes", None)
        r.pop("options_history", None)
        r.pop("_uae_cache", None)
        # Added by _eval()'s memoization wrapper -- these are internal
        # bookkeeping (the memo dict is keyed by (id(node), shift,
        # tf_default) tuples, which json.dumps cannot serialize as dict
        # keys). r IS the per-symbol ctx object itself (passed directly
        # as ctx to _eval() above), so anything _eval() adds to ctx
        # needs to be stripped here too, same as the pre-existing keys
        # above.
        r.pop("_eval_memo", None)
        r.pop("_eval_memo_keepalive", None)

    passed = _json_safe(passed)
    summary = _json_safe(summary)

    _req_timer.__exit__(None, None, None)
    _timing = {
        "elapsed_ms": round(_req_timer.elapsed_ms, 1),
        "symbols_requested": len(symbols),
        "symbols_scanned": len(snapshots),
        "cache": {
            "memory_hits": _req_bucket.get("snapshot_memory_hits", 0),
            "sqlite_hits": _req_bucket.get("snapshot_sqlite_hits", 0),
            "computed": _req_bucket.get("snapshot_computed", 0),
        },
        "db_connections_opened": _req_bucket.get("db_connections_opened", 0),
    }
    profiling.record_request("scanner_builder.api_run", _req_timer.elapsed_ms, {
        "symbols": len(symbols), "matches": len(passed), "cache": _timing["cache"],
    })
    profiling.request_scope_end()

    return jsonify({
        "ok": True,
        "query_text": query_text,
        "benchmark": benchmark,
        "watchlist_id": watchlist_id,
        "clauses": _flatten_atoms(root),
        "count": len(passed),
        "results": passed,
        "symbols_csv": ",".join([str(r.get("symbol") or "").upper() for r in passed if r.get("symbol")]),
        "result_columns": result_columns,
        "result_template_id": result_template_id,
        "summary": summary,
        "backfill_queued": backfill_queued,
        "query_debug": query_debug,
        "errors": errors[:50],
        "error_count": len(errors),
        "timed_out_symbols": timed_out_symbols,
        "loaded_count": len(snapshots),
        "symbols": symbols,
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timing": _timing,
        "logic": {
            "timeframes": req_tfs,
            "indicators": INDICATORS,
            "operators": [">", ">=", "<", "<=", "=", "crosses above", "crosses below"],
            "functions": FUNCTION_SIGNATURES,
            "function_meta": FUNCTION_CATALOG,
            "evaluation": "boolean precedence: NOT > AND > OR",
        },
    })


# ─────────────────────────────────────────────────────────────────────────────
# Trade Scoring Layer — augments scanner results with Entry Quality + Trade Rec
# ─────────────────────────────────────────────────────────────────────────────

def _infer_bias_from_clauses(clauses: List[str], query_text: str) -> str:
    """
    Infer bullish / bearish / neutral bias from scanner query clauses.
    Used to pick the right trade direction before scoring.

    Returns: "bull" | "bear" | "neutral"
    """
    q = (query_text or " ".join(clauses or [])).lower()

    bull_signals = 0
    bear_signals = 0

    # Explicit bias functions
    if "flowbias" in q.replace("_","") and "bull" in q:  bull_signals += 3
    if "flowbias" in q.replace("_","") and "bear" in q:  bear_signals += 3
    if "convictionscore" in q.replace("_",""):
        if '"bull"' in q or "'bull'" in q:  bull_signals += 3
        if '"bear"' in q or "'bear'" in q:  bear_signals += 3

    # RSI direction
    if "rsi14 >" in q or "rsi14>" in q:
        try:
            import re
            m = re.search(r'rsi14\s*[>>=]+\s*(\d+)', q)
            if m and int(m.group(1)) >= 50: bull_signals += 2
            elif m and int(m.group(1)) < 40: bear_signals += 2  # looking for oversold bounce
        except: pass
    if "rsi14 <" in q or "rsi14<" in q:
        try:
            import re
            m = re.search(r'rsi14\s*[<<]+\s*(\d+)', q)
            if m and int(m.group(1)) <= 30: bull_signals += 2   # oversold = bull bounce
            elif m and int(m.group(1)) < 50: bear_signals += 1
        except: pass

    # EMA stack
    if "ema20" in q and "ema50" in q:
        if "ema20 >" in q or "ema20>" in q: bull_signals += 2
        if "ema20 <" in q or "ema20<" in q: bear_signals += 2

    # RSI-EMA diff (RSIDiff90 / rsi_diff_90)
    if "rsidiff90" in q.replace("_","").replace(" ","") or "rsi_diff_90" in q:
        import re
        m = re.search(r'rsidiff90[^<>=]*[>>=]+\s*(-?\d+)', q.replace("_","").replace(" ",""))
        if m and int(m.group(1)) >= 10: bull_signals += 2
        m2 = re.search(r'rsidiff90[^<>=]*[<<]+\s*(-?\d+)', q.replace("_","").replace(" ",""))
        if m2 and int(m2.group(1)) <= -10: bear_signals += 2

    # Named scanner patterns
    bull_keywords = ["bull", "uptrend", "breakout", "momentum", "accumulation",
                     "oversold", "support reclaim", "second pullback", "sr breakout",
                     "first pullback", "atl breakdown"]  # ATL oversold → bull bounce
    bear_keywords = ["bear", "downtrend", "breakdown", "distribution", "overbought",
                     "resistance", "short", "cs ", "failed breakout", "ath momentum"]

    for kw in bull_keywords:
        if kw in q: bull_signals += 1
    for kw in bear_keywords:
        if kw in q: bear_signals += 1

    # RelativeStrength/Leadership direction
    import re as _re
    m = _re.search(r'relativestrength[^<>=]*[>>=]+\s*(\d+)', q.replace(" ",""))
    if m and int(m.group(1)) > 5: bull_signals += 1
    m2 = _re.search(r'relativestrength[^<>=]*[<<=]+\s*(-?\d+)', q.replace(" ",""))
    if m2 and int(m2.group(1)) < -5: bear_signals += 1

    if bull_signals > bear_signals + 1:   return "bull"
    if bear_signals > bull_signals + 1:   return "bear"
    return "neutral"


def _scanner_match_score(row: Dict[str, Any], clauses: List[str], query_text: str) -> Dict[str, Any]:
    """
    Scanner Match Score (0-100): How strongly does the symbol match the scanner criteria?

    Goes beyond pass/fail — measures:
      - Magnitude of each clause (e.g. RSI=28 is a stronger oversold than RSI=32)
      - Consistency across timeframes
      - Supporting signals (flow, volume, OI)
    """
    score = 60  # base: it passed the filter = starts at 60
    notes: List[str] = []

    rsi = _safe_number(row.get("rsi14")) or 50
    rsi_diff = _safe_number(row.get("rsi_diff_90")) or 0
    iv_rank = _safe_number(row.get("iv_rank")) or 50
    iv_change = _safe_number(row.get("iv_change")) or 0
    flow_score = _safe_number(row.get("flow_score")) or 50
    flow_bias = str(row.get("flow_bias") or "NEUTRAL").upper()
    leadership = _safe_number(row.get("leadership")) or 50
    rs = _safe_number(row.get("relative_strength")) or 0
    price = _safe_number(row.get("price")) or 1
    ema20 = _safe_number(row.get("ema20")) or price
    ema50 = _safe_number(row.get("ema50")) or price
    earn_days = _safe_number(row.get("earn_days")) or 999

    q = (query_text or "").lower()
    bias = _infer_bias_from_clauses(clauses, query_text)

    # ── RSI extremity bonus ────────────────────────────────────────────
    if "rsi" in q:
        if rsi < 25:   score += 15; notes.append(f"RSI {rsi:.0f} deeply oversold")
        elif rsi < 32: score += 10; notes.append(f"RSI {rsi:.0f} oversold")
        elif rsi > 75: score += 15; notes.append(f"RSI {rsi:.0f} deeply overbought")
        elif rsi > 68: score += 10; notes.append(f"RSI {rsi:.0f} overbought")
        elif 48 <= rsi <= 55: score += 5; notes.append(f"RSI {rsi:.0f} near centre")

    # ── RSI-EMA diff extremity ─────────────────────────────────────────
    if "rsidiff" in q.replace("_","").replace(" ",""):
        if abs(rsi_diff) >= 25: score += 12; notes.append(f"RSI-EMA diff {rsi_diff:+.1f} extreme")
        elif abs(rsi_diff) >= 15: score += 8; notes.append(f"RSI-EMA diff {rsi_diff:+.1f} strong")
        elif abs(rsi_diff) >= 8: score += 4; notes.append(f"RSI-EMA diff {rsi_diff:+.1f}")

    # ── EMA alignment bonus ────────────────────────────────────────────
    if bias == "bull" and price > ema20 > ema50:
        score += 8; notes.append("Price > EMA20 > EMA50 (bull stack)")
    elif bias == "bear" and price < ema20 < ema50:
        score += 8; notes.append("Price < EMA20 < EMA50 (bear stack)")
    elif bias == "bull" and price > ema20:
        score += 4
    elif bias == "bear" and price < ema20:
        score += 4

    # ── Flow confirmation ──────────────────────────────────────────────
    if (bias == "bull" and flow_bias == "BULL") or (bias == "bear" and flow_bias == "BEAR"):
        score += 8; notes.append(f"Flow {flow_bias} confirms direction")
    elif flow_bias != "NEUTRAL" and flow_bias != f"{'BULL' if bias=='bull' else 'BEAR'}":
        score -= 5; notes.append(f"Flow {flow_bias} contradicts direction")

    # ── Leadership / RS bonus ──────────────────────────────────────────
    if bias == "bull" and leadership >= 70:
        score += 6; notes.append(f"Leadership {leadership:.0f}% — top quartile")
    elif bias == "bear" and leadership <= 30:
        score += 6; notes.append(f"Leadership {leadership:.0f}% — bottom quartile")

    # ── IV context ─────────────────────────────────────────────────────
    if "ivrank" in q.replace("_","").replace(" ","") or "iv_rank" in q:
        if iv_rank < 20:   score += 8; notes.append(f"IVR {iv_rank:.0f}% very low — criterion met strongly")
        elif iv_rank < 30: score += 5; notes.append(f"IVR {iv_rank:.0f}% low")
        elif iv_rank > 70: score += 8; notes.append(f"IVR {iv_rank:.0f}% very high — criterion met strongly")
        elif iv_rank > 60: score += 5; notes.append(f"IVR {iv_rank:.0f}% elevated")

    # ── Earnings proximity penalty ─────────────────────────────────────
    if earn_days < 14:
        score -= 20; notes.append(f"⚠ Earnings in {earn_days:.0f} days — high risk")
    elif earn_days < 21:
        score -= 8; notes.append(f"Earnings in {earn_days:.0f} days — caution")

    score = max(5, min(97, round(score)))
    return {"match_score": score, "match_notes": notes[:4]}


def _trade_quality_score_from_ctx(row: Dict[str, Any], bias: str) -> Dict[str, Any]:
    """
    Trade Quality Score (0-100) using the same entry scoring engine.
    Works purely from scanner ctx fields (no live re-fetch needed).

    Factors:
      Regime / trend alignment  25 pts
      RS vs benchmark           20 pts
      IV Rank (trade fit)       15 pts
      Flow (PCR proxy)          15 pts
      EMA wall structure        15 pts
      Momentum confirmation     10 pts
    """
    score = 50
    pros: List[str] = []
    cons: List[str] = []

    price = _safe_number(row.get("price")) or 1
    rsi = _safe_number(row.get("rsi14")) or 50
    rsi_diff = _safe_number(row.get("rsi_diff_90")) or 0
    ema20 = _safe_number(row.get("ema20")) or price
    ema50 = _safe_number(row.get("ema50")) or price
    iv_rank = _safe_number(row.get("iv_rank")) or 50
    flow_bias = str(row.get("flow_bias") or "NEUTRAL").upper()
    flow_score_val = _safe_number(row.get("flow_score")) or 50
    rs = _safe_number(row.get("relative_strength")) or 0
    leadership = _safe_number(row.get("leadership")) or 50
    pcr_shift = _safe_number(row.get("pcr_shift")) or 0
    iv_change = _safe_number(row.get("iv_change")) or 0
    earn_days = _safe_number(row.get("earn_days")) or 999

    is_bull = bias == "bull"
    is_bear = bias == "bear"
    is_ic   = bias == "neutral"
    is_credit = True  # credit spreads are the default for all three

    # ── Regime / Trend (25 pts) ────────────────────────────────────────
    if is_bull:
        if price > ema20 > ema50 and rsi_diff > 5:
            score += 20; pros.append("Bull trend: price > EMA20 > EMA50 + momentum")
        elif price > ema20 > ema50:
            score += 14; pros.append("Bull trend: price > EMA20 > EMA50")
        elif price > ema20:
            score += 8;  pros.append("Price above EMA20 — mild bull")
        elif price < ema50:
            score -= 12; cons.append("Price below EMA50 — headwind for bulls")
        if rsi_diff > 15:
            score += 5;  pros.append(f"RSI-EMA diff +{rsi_diff:.0f} — strong momentum")
        elif rsi_diff < -10:
            score -= 8;  cons.append(f"RSI-EMA diff {rsi_diff:.0f} — momentum weak")
    elif is_bear:
        if price < ema20 < ema50 and rsi_diff < -5:
            score += 20; pros.append("Bear trend: price < EMA20 < EMA50 + momentum")
        elif price < ema20 < ema50:
            score += 14; pros.append("Bear trend: price < EMA20 < EMA50")
        elif price < ema20:
            score += 8;  pros.append("Price below EMA20 — mild bear")
        elif price > ema50:
            score -= 12; cons.append("Price above EMA50 — headwind for bears")
        if rsi_diff < -15:
            score += 5;  pros.append(f"RSI-EMA diff {rsi_diff:.0f} — strong downside momentum")
        elif rsi_diff > 10:
            score -= 8;  cons.append(f"RSI-EMA diff +{rsi_diff:.0f} — upside momentum headwind")
    else:  # IC
        on_ema20 = abs(price - ema20) / max(price, 1) < 0.02
        range_bound = abs(rsi_diff) <= 10
        if range_bound: score += 12; pros.append(f"RSI-EMA diff {rsi_diff:+.0f} — range-bound")
        if on_ema20:    score += 8;  pros.append("Price near EMA20 — coiling")
        if abs(rsi_diff) > 20: score -= 8; cons.append("Strong trend — IC risky")

    # ── RS vs Benchmark (20 pts) ───────────────────────────────────────
    if is_bull:
        if rs > 8:     score += 15; pros.append(f"RS +{rs:.1f} vs benchmark — strong outperformer")
        elif rs > 3:   score += 9;  pros.append(f"RS +{rs:.1f} vs benchmark")
        elif rs < -8:  score -= 15; cons.append(f"RS {rs:.1f} — heavy underperformer")
        elif rs < -3:  score -= 7;  cons.append(f"RS {rs:.1f} — underperforming")
    elif is_bear:
        if rs < -8:    score += 15; pros.append(f"RS {rs:.1f} vs benchmark — confirmed weakness")
        elif rs < -3:  score += 9;  pros.append(f"RS {rs:.1f} vs benchmark — underperforming")
        elif rs > 8:   score -= 15; cons.append(f"RS +{rs:.1f} — strong outperformer, bear headwind")
        elif rs > 3:   score -= 7;  cons.append(f"RS +{rs:.1f} — outperforming")
    else:
        if abs(rs) <= 4: score += 8; pros.append(f"RS {rs:+.1f} neutral — IC suitable")
        elif abs(rs) > 10: score -= 5; cons.append(f"RS {rs:+.1f} trending hard — IC risk")

    # ── IV Rank (15 pts) ──────────────────────────────────────────────
    if iv_rank > 65:   score += 12; pros.append(f"IVR {iv_rank:.0f}% — premium rich, sell favoured")
    elif iv_rank > 45: score += 7;  pros.append(f"IVR {iv_rank:.0f}% — good credit opportunity")
    elif iv_rank < 20: score -= 10; cons.append(f"IVR {iv_rank:.0f}% — thin premium, avoid selling")
    elif iv_rank < 30: score -= 5;  cons.append(f"IVR {iv_rank:.0f}% — low IV")

    # ── Flow / PCR (15 pts) ───────────────────────────────────────────
    if is_bull:
        if flow_bias == "BULL":   score += 12; pros.append(f"Flow BULL — bullish options flow")
        elif flow_bias == "BEAR": score -= 10; cons.append("Flow BEAR — options flow contradicts")
        if pcr_shift < -0.1:      score += 5;  pros.append(f"PCR falling — puts being sold")
    elif is_bear:
        if flow_bias == "BEAR":   score += 12; pros.append(f"Flow BEAR — bearish options flow")
        elif flow_bias == "BULL": score -= 10; cons.append("Flow BULL — options flow contradicts")
        if pcr_shift > 0.1:       score += 5;  pros.append(f"PCR rising — put accumulation")
    else:
        if flow_bias == "NEUTRAL": score += 8; pros.append("Flow NEUTRAL — ideal for IC")

    # ── Earnings (hard penalty) ───────────────────────────────────────
    if earn_days < 14:
        score -= 25; cons.append(f"Earnings in {earn_days:.0f}d — avoid")
    elif earn_days < 21:
        score -= 10; cons.append(f"Earnings in {earn_days:.0f}d — caution")
    elif earn_days >= 30:
        score += 3;  pros.append(f"Earnings safe ({earn_days:.0f}d away)")

    score = max(5, min(97, round(score)))

    if score >= 80:   grade, rec = "A", "OPEN"
    elif score >= 65: grade, rec = "B", "OPEN"
    elif score >= 50: grade, rec = "C", "OPEN_SMALL"
    elif score >= 35: grade, rec = "D", "OPEN_SMALL"
    else:             grade, rec = "F", "AVOID"

    return {
        "trade_score": score, "grade": grade, "recommendation": rec,
        "pros": pros[:4], "cons": cons[:3],
    }


def _recommend_trade_type(bias: str, rsi: float, rsi_diff: float, iv_rank: float,
                           flow_bias: str, price: float, ema20: float, ema50: float) -> Tuple[str, str, str]:
    """
    Given bias + conditions, return (trade_type, trade_name, rationale_snippet).
    """
    is_bull  = bias == "bull"
    is_bear  = bias == "bear"
    is_ic    = bias == "neutral"

    # Debit vs credit based on IV
    credit_ok = iv_rank >= 30

    if is_bull:
        if rsi < 35:  # deep oversold → anticipate bounce, debit is fine
            if credit_ok:
                return "PS", "Bull Put Spread", "Oversold bounce + credit: sell put below support"
            return "PB", "Bull Call Debit", "Oversold bounce: buy call cheaply while IV low"
        elif rsi_diff > 15 and price > ema20:  # momentum continuation
            if credit_ok:
                return "PS", "Bull Put Spread", "Momentum continuation: sell OTM put below trend"
            return "CB", "Bull Call Debit", "Momentum: buy call (IV low = cheap debit)"
        else:
            if credit_ok:
                return "PS", "Bull Put Spread", "Bull trend: sell put below EMA support for credit"
            return "PB", "Bull Put Debit", "Low IV bull: buy put spread for defined risk"

    elif is_bear:
        if rsi > 65:  # overbought → anticipate fade
            if credit_ok:
                return "CS", "Bear Call Spread", "Overbought fade: sell call above resistance for credit"
            return "CB", "Bear Put Debit", "Overbought: buy put while IV still reasonable"
        elif rsi_diff < -15 and price < ema20:
            if credit_ok:
                return "CS", "Bear Call Spread", "Downtrend: sell OTM call above declining EMAs"
            return "CB", "Bear Put Debit", "Momentum down: buy put (IV low = cheap)"
        else:
            if credit_ok:
                return "CS", "Bear Call Spread", "Bear trend: sell call above resistance"
            return "CB", "Bear Put Debit", "Bear: buy put spread for limited risk"

    else:  # neutral / IC
        return "IC", "Iron Condor", "Range-bound: sell both OTM put + call for premium"


def _build_trade_recommendation(row: Dict[str, Any], bias: str,
                                  trade_score: int, expiry_hint: str = "") -> Dict[str, Any]:
    """
    Build a concrete trade recommendation from scanner ctx.
    Uses Black-Scholes approximation with ATR-derived strikes.
    """
    price = _safe_number(row.get("price")) or 1
    rsi = _safe_number(row.get("rsi14")) or 50
    rsi_diff = _safe_number(row.get("rsi_diff_90")) or 0
    iv_rank = _safe_number(row.get("iv_rank")) or 50
    iv_est = _safe_number(row.get("iv_est")) or 20.0
    flow_bias = str(row.get("flow_bias") or "NEUTRAL").upper()
    ema20 = _safe_number(row.get("ema20")) or price
    ema50 = _safe_number(row.get("ema50")) or price
    earn_days = int(_safe_number(row.get("earn_days")) or 999)
    symbol = row.get("symbol", "")

    # Preferred DTE: 14-21 days
    dte = 17

    trade_type, trade_name, rationale_hint = _recommend_trade_type(
        bias, rsi, rsi_diff, iv_rank, flow_bias, price, ema20, ema50
    )

    # Strike interval from price
    if price < 20:    interval = 0.5
    elif price < 50:  interval = 1.0
    elif price < 200: interval = 2.5
    elif price < 500: interval = 5.0
    else:             interval = 10.0

    def _round(p):
        return round(round(p / interval) * interval, 2)

    def _bs(S, K, T_d, iv, is_call):
        import math as _m
        try:
            T = max(T_d, 1) / 252.0
            sig = max(iv / 100.0, 0.05)
            sqT = _m.sqrt(T)
            d1 = (_m.log(S / K) + 0.5 * sig * sig * T) / (sig * sqT)
            d2 = d1 - sig * sqT
            def _cdf(x):
                t = 1 / (1 + 0.2316419 * abs(x))
                p = 1 - 0.3989422803 * _m.exp(-0.5 * x * x) * t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
                return p if x >= 0 else 1 - p
            if is_call: return max(0.01, round(S * _cdf(d1) - K * _cdf(d2), 2))
            else:       return max(0.01, round(K * _cdf(-d2) - S * _cdf(-d1), 2))
        except: return max(0.05, round(abs(S - K) * 0.3 + 0.5, 2))

    legs_desc = ""
    est_credit = None
    max_loss = None
    pop = None
    pnr = None

    if trade_type == "PS":
        # sell 2% OTM put, buy 5 intervals lower
        sp = _round(price * 0.98)
        bp = _round(sp - interval * 5)
        sell_px = _bs(price, sp, dte, iv_est, False)
        buy_px  = _bs(price, bp, dte, iv_est, False)
        est_credit = round(sell_px - buy_px, 2)
        width = sp - bp
        max_loss   = round(width - est_credit, 2)
        legs_desc  = f"Sell ${sp}P / Buy ${bp}P  (~{dte} DTE)"
        pop        = min(88, max(52, round(65 + abs(price - sp) / price * 300)))
        pnr        = round(bp - (bp * dte * max(price * 0.01, 0.5)) / 2000, 2)

    elif trade_type == "CS":
        sc = _round(price * 1.02)
        bc = _round(sc + interval * 5)
        sell_px = _bs(price, sc, dte, iv_est, True)
        buy_px  = _bs(price, bc, dte, iv_est, True)
        est_credit = round(sell_px - buy_px, 2)
        width = bc - sc
        max_loss   = round(width - est_credit, 2)
        legs_desc  = f"Sell ${sc}C / Buy ${bc}C  (~{dte} DTE)"
        pop        = min(88, max(52, round(65 + abs(sc - price) / price * 300)))

    elif trade_type == "IC":
        sp = _round(price * 0.97); bp = _round(sp - interval * 5)
        sc = _round(price * 1.03); bc = _round(sc + interval * 5)
        cr_p = round(_bs(price, sp, dte, iv_est, False) - _bs(price, bp, dte, iv_est, False), 2)
        cr_c = round(_bs(price, sc, dte, iv_est, True)  - _bs(price, bc, dte, iv_est, True),  2)
        est_credit = round(cr_p + cr_c, 2)
        width = max(sp - bp, bc - sc)
        max_loss   = round(width - est_credit, 2)
        legs_desc  = f"Sell ${sp}P/Buy ${bp}P  ·  Sell ${sc}C/Buy ${bc}C  (~{dte} DTE)"
        pop        = 68

    elif trade_type in ("PB", "CB"):  # debit
        if trade_type == "PB":
            buy_k  = _round(price * 1.01)
            sell_k = _round(buy_k + interval * 5)
            buy_px  = _bs(price, buy_k, dte, iv_est, True)
            sell_px = _bs(price, sell_k, dte, iv_est, True)
        else:
            buy_k  = _round(price * 0.99)
            sell_k = _round(buy_k - interval * 5)
            buy_px  = _bs(price, buy_k, dte, iv_est, False)
            sell_px = _bs(price, sell_k, dte, iv_est, False)
        debit = round(buy_px - sell_px, 2)
        width = abs(buy_k - sell_k)
        est_credit = -debit  # negative = debit paid
        max_loss   = debit
        legs_desc  = f"Buy ${buy_k}{'C' if trade_type=='PB' else 'P'} / Sell ${sell_k}{'C' if trade_type=='PB' else 'P'}  (~{dte} DTE)"
        pop        = 55

    rr = round(abs(est_credit or 0) / max(max_loss or 0.01, 0.01), 2)
    manage = (f"Take profit at 50% credit (${round((est_credit or 0) * 0.5, 2)}). "
              f"Stop at 50% max loss. Close at <7 DTE if not profitable."
              if (est_credit or 0) > 0 else
              f"Take profit at 2× debit. Stop at full debit loss.")

    return {
        "trade_type": trade_type, "trade_name": trade_name,
        "legs": legs_desc, "dte_hint": dte,
        "est_credit": est_credit if (est_credit or 0) > 0 else None,
        "est_debit": abs(est_credit) if (est_credit or 0) < 0 else None,
        "max_loss": max_loss, "rr": rr, "pop": pop, "pnr": pnr,
        "rationale_hint": rationale_hint,
        "manage": manage,
    }


@scanner_builder_bp.route("/api/trade_score", methods=["POST"])
def api_trade_score():
    """
    Given a scanner result row + the query_text/clauses, returns:
      - scanner_match_score  (0-100): how strongly the symbol matched
      - trade_quality_score  (0-100): how good the entry looks
      - grade, recommendation, pros, cons
      - concrete trade recommendation with legs

    Can process a single row or a batch of rows.
    """
    payload = request.get_json(force=True) or {}
    query_text = payload.get("query_text", "")
    clauses    = payload.get("clauses", [])
    rows       = payload.get("rows")   # batch mode
    row        = payload.get("row")    # single mode

    if rows is not None:
        # Batch mode — infer bias once for the query
        bias = _infer_bias_from_clauses(clauses, query_text)
        results = []
        for r in (rows or []):
            try:
                match  = _scanner_match_score(r, clauses, query_text)
                trade_q = _trade_quality_score_from_ctx(r, bias)
                rec    = _build_trade_recommendation(r, bias, trade_q["trade_score"])
                results.append({
                    "symbol": r.get("symbol"),
                    **match, **trade_q,
                    "bias": bias,
                    "trade_rec": rec,
                    "combined_score": round((match["match_score"] * 0.4 + trade_q["trade_score"] * 0.6)),
                })
            except Exception as e:
                results.append({"symbol": r.get("symbol"), "error": str(e)[:80]})
        return jsonify({"ok": True, "bias": bias, "results": results})

    elif row is not None:
        bias   = _infer_bias_from_clauses(clauses, query_text)
        match  = _scanner_match_score(row, clauses, query_text)
        trade_q = _trade_quality_score_from_ctx(row, bias)
        rec    = _build_trade_recommendation(row, bias, trade_q["trade_score"])
        return jsonify({
            "ok": True, "symbol": row.get("symbol"), "bias": bias,
            **match, **trade_q,
            "trade_rec": rec,
            "combined_score": round((match["match_score"] * 0.4 + trade_q["trade_score"] * 0.6)),
        })
    else:
        return jsonify({"error": "Provide 'row' or 'rows'"}), 400
