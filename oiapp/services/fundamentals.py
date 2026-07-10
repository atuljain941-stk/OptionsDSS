from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Iterable, List
import sqlite3

from .yf_session import get_ticker

DB_PATH = str(Path(__file__).resolve().parents[2] / 'options_data.db')
_BETA_TTL_DAYS = 30


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=5000')
    return con


def _ensure_table() -> None:
    con = _conn()
    try:
        con.execute(
            '''
            CREATE TABLE IF NOT EXISTS symbol_fundamentals (
                symbol TEXT PRIMARY KEY,
                beta REAL,
                source TEXT,
                updated TEXT
            )
            '''
        )
        con.commit()
    finally:
        con.close()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        try:
            return datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S')
        except Exception:
            return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        if x != x or x in (float('inf'), float('-inf')):
            return None
        return x
    except Exception:
        return None


@lru_cache(maxsize=4096)
def beta_info(symbol: str, refresh: bool = False) -> Dict[str, Any]:
    sym = str(symbol or '').strip().upper()
    if not sym:
        return {'beta': None, 'beta_source': None, 'beta_updated': None}

    _ensure_table()

    con = _conn()
    try:
        row = con.execute(
            'SELECT beta, source, updated FROM symbol_fundamentals WHERE symbol=?',
            (sym,),
        ).fetchone()
        if row and not refresh:
            updated = _parse_ts(row['updated'])
            if updated and (datetime.now() - updated) <= timedelta(days=_BETA_TTL_DAYS):
                return {
                    'beta': _safe_float(row['beta']),
                    'beta_source': row['source'],
                    'beta_updated': row['updated'],
                }
    finally:
        con.close()

    beta_val = None
    source = None
    try:
        tk = get_ticker(sym)
        fi = getattr(tk, 'fast_info', None)
        if fi is not None:
            beta_val = _safe_float(getattr(fi, 'beta', None))
            if beta_val is not None:
                source = 'fast_info'
        if beta_val is None:
            info = tk.info or {}
            beta_val = _safe_float(info.get('beta') or info.get('betaValue'))
            if beta_val is not None:
                source = 'info'
    except Exception:
        beta_val = None

    updated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    con = _conn()
    try:
        con.execute(
            'INSERT OR REPLACE INTO symbol_fundamentals(symbol, beta, source, updated) VALUES (?, ?, ?, ?)',
            (sym, beta_val, source, updated),
        )
        con.commit()
    finally:
        con.close()

    return {'beta': beta_val, 'beta_source': source, 'beta_updated': updated}


def get_beta(symbol: str, refresh: bool = False) -> Optional[float]:
    return beta_info(symbol, refresh=refresh).get('beta')


def refresh_betas(symbols: Iterable[str], refresh: bool = False) -> Dict[str, Any]:
    """Refresh cached beta values for a collection of symbols."""
    sym_list: List[str] = []
    for sym in symbols or []:
        s = str(sym or '').strip().upper()
        if s and s not in sym_list:
            sym_list.append(s)
    results: Dict[str, Any] = {}
    if not sym_list:
        return results
    for sym in sym_list:
        results[sym] = beta_info(sym, refresh=refresh)
    try:
        beta_info.cache_clear()
    except Exception:
        pass
    return results


def get_betas(symbols: Iterable[str], refresh: bool = False) -> Dict[str, Any]:
    return refresh_betas(tuple(symbols or []), refresh=refresh)
