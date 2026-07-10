"""Chart presets and default primitive selections."""
from __future__ import annotations

CHART_TIMEFRAMES = ["1m", "1w", "1d", "4h", "1h", "15m", "5m"]

DEFAULT_PRIMITIVES = [
    "ema20", "ema50", "support", "resistance", "put_walls", "call_walls", "gamma_wall"
]

CHART_PRESETS = {
    "monthly-weekly-daily-intraday": ["1m", "1w", "1d", "4h"],
    "weekly-daily-4h-1h": ["1w", "1d", "4h", "1h"],
    "daily-4h-1h-15m": ["1d", "4h", "1h", "15m"],
}
