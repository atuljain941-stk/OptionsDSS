"""End-of-day 2-minute extended-hours cache for intraday backtests."""
from __future__ import annotations
import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo
from ..config import DB_PATH

_ET = ZoneInfo("America/New_York")

def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con

def ensure_tables():
    con = _conn()
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS intraday_2m_price_cache (
            symbol TEXT NOT NULL,
            ts_et TEXT NOT NULL,
            timeframe TEXT NOT NULL DEFAULT '2m',
            session TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            source TEXT NOT NULL DEFAULT 'tastytrade',
            fetched_at TEXT NOT NULL,
            PRIMARY KEY(symbol, timeframe, ts_et)
        );
        CREATE INDEX IF NOT EXISTS idx_intraday_2m_price_cache_symbol_day
          ON intraday_2m_price_cache(symbol, ts_et);
        CREATE TABLE IF NOT EXISTS premarket_levels (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            premarket_high REAL NOT NULL,
            premarket_low REAL NOT NULL,
            bar_count INTEGER NOT NULL,
            source TEXT NOT NULL,
            calculated_at TEXT NOT NULL,
            PRIMARY KEY(symbol, trade_date)
        );
        """)
        con.commit()
    finally:
        con.close()

def _value(candle, *names):
    for name in names:
        value = getattr(candle, name, None)
        if value is not None:
            return value
    return None

def fetch_symbol_intraday(symbol: str, trade_date=None) -> dict:
    """Fetch one completed ET session once from DXLink; never poll per minute."""
    ensure_tables()
    symbol = str(symbol).upper().strip()
    day = trade_date or datetime.now(_ET).date()
    start = datetime.combine(day, time(4, 0), tzinfo=_ET)
    from .tastytrade_feed import feed
    candles = feed.get_candles(symbol, "1m", start, extended_hours=True,
                               idle_timeout=5.0, overall_timeout=45.0)
    minute_rows = []
    for candle in candles or []:
        try:
            ts = datetime.fromtimestamp(int(candle.time) / 1000, tz=_ET)
        except Exception:
            continue
        if ts.date() != day or not (time(4, 0) <= ts.time() <= time(16, 0)):
            continue
        o = _value(candle, "open", "open_price")
        h = _value(candle, "high", "high_price")
        l = _value(candle, "low", "low_price")
        cl = _value(candle, "close", "close_price")
        if None in (o, h, l, cl):
            continue
        session = "premarket" if ts.time() < time(9, 30) else "regular"
        minute_rows.append((ts, session, float(o), float(h), float(l),
                     float(cl), float(_value(candle, "volume", "day_volume") or 0)))
    if not minute_rows:
        return {"ok": False, "symbol": symbol, "error": "Tastytrade returned no 1-minute extended-hours candles", "bars": 0}
    # The strategy runs on two-minute candles.  Request a single 1m snapshot
    # after the close, but aggregate each consecutive pair before touching SQLite:
    # one network call per symbol/day and roughly half the DB rows of raw 1m data.
    buckets = {}
    for ts, session, o, h, l, cl, volume in minute_rows:
        bucket = ts.replace(second=0, microsecond=0)
        bucket = bucket.replace(minute=bucket.minute - (bucket.minute % 2))
        prior = buckets.get(bucket)
        if prior is None:
            buckets[bucket] = [session, o, h, l, cl, volume]
        else:
            prior[2] = max(prior[2], h)
            prior[3] = min(prior[3], l)
            prior[4] = cl
            prior[5] += volume
    fetched_at = datetime.now(_ET).isoformat(timespec="seconds")
    rows = [
        (symbol, bucket.isoformat(), "2m", values[0], values[1], values[2], values[3],
         values[4], values[5], "tastytrade", fetched_at)
        for bucket, values in sorted(buckets.items())
    ]

    con = _conn()
    try:
        con.executemany("""
            INSERT INTO intraday_2m_price_cache
            (symbol,ts_et,timeframe,session,open,high,low,close,volume,source,fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol,timeframe,ts_et) DO UPDATE SET
              session=excluded.session, open=excluded.open, high=excluded.high, low=excluded.low,
              close=excluded.close, volume=excluded.volume, source=excluded.source, fetched_at=excluded.fetched_at
        """, rows)
        pm = [r for r in rows if r[3] == "premarket"]
        if pm:
            con.execute("""
              INSERT INTO premarket_levels(symbol,trade_date,premarket_high,premarket_low,bar_count,source,calculated_at)
              VALUES (?,?,?,?,?,?,?)
              ON CONFLICT(symbol,trade_date) DO UPDATE SET
                premarket_high=excluded.premarket_high,premarket_low=excluded.premarket_low,
                bar_count=excluded.bar_count,source=excluded.source,calculated_at=excluded.calculated_at
            """, (symbol, day.isoformat(), max(r[5] for r in pm), min(r[6] for r in pm), len(pm),
                  "tastytrade", datetime.now(_ET).isoformat(timespec="seconds")))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "symbol": symbol, "bars": len(rows), "premarket_bars": len(pm), "timeframe": "2m"}

def fetch_watchlist_intraday(watchlist_id: int) -> dict:
    ensure_tables()
    con = _conn()
    try:
        symbols = [r[0] for r in con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol", (int(watchlist_id),)
        ).fetchall()]
    finally:
        con.close()
    results = [fetch_symbol_intraday(symbol) for symbol in symbols]
    return {"ok": bool(symbols) and all(r.get("ok") for r in results), "symbols": len(symbols), "results": results}
