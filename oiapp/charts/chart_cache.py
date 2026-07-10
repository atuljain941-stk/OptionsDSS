"""Simple cache helpers for chart data."""
from __future__ import annotations
from functools import lru_cache

@lru_cache(maxsize=256)
def cache_key(symbol: str, timeframe: str, expiry: str | None = None) -> str:
    return f"{(symbol or '').upper().strip()}|{(timeframe or '').lower().strip()}|{(expiry or '').strip()}"
