from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, List, Optional

from .edge_factors import trend_exhaustion_snapshot
from .scoring_service import attach_scanner_scores, trend_exhaustion_state_from_profile

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")


def _symbols_from_db() -> List[str]:
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()
        con.close()
        return [r[0] for r in rows if r and r[0]]
    except Exception:
        return []


def _wl_symbols(watchlist_id: Optional[int]) -> Optional[List[str]]:
    if not watchlist_id:
        return None
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (int(watchlist_id),),
        ).fetchall()
        con.close()
        return [r[0] for r in rows if r and r[0]] or None
    except Exception:
        return None


def _one(sym: str) -> Optional[dict]:
    r = trend_exhaustion_snapshot(sym)
    if not r or r.get("status") not in {"found", "watch"}:
        return None
    # Convert to a scanner-row style result for the dashboard table.
    result = {
        "symbol": sym,
        "direction": r.get("direction"),
        "status": r.get("status"),
        "score": r.get("score", 0),
        "price": r.get("price"),
        "rsi": r.get("rsi"),
        "atr_pct": r.get("atr_pct"),
        "trend_age": r.get("trend_age"),
        "stretch_atr": r.get("stretch_atr"),
        "stretch_pct": r.get("stretch_pct"),
        "climax_vol": r.get("climax_vol"),
        "macd_hist": r.get("macd_hist"),
        "macd_roll": r.get("macd_roll"),
        "near_extreme": r.get("near_extreme"),
        "detail": r.get("detail"),
        "notes": r.get("notes", []),
        "trade_bias": r.get("contrarian_trade"),
        "signal": "Fade / reversal" if r.get("direction") == "BULL_EXHAUSTION" else "Cover / reversal",
    }
    return attach_scanner_scores(result, sym, setup_type="Trend Exhaustion", direction=r.get("direction"), native_score=result.get("score"), trend_age=result.get("trend_age"))


def run_trend_exhaustion_scan(symbols=None, watchlist_id: Optional[int] = None, workers: int = 18):
    if symbols is None:
        symbols = _wl_symbols(watchlist_id) or _symbols_from_db()
    if not symbols:
        symbols = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "META", "TSLA", "AMD", "AMZN", "GOOGL"]

    results: List[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, sym): sym for sym in symbols[:120]}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                continue

    results.sort(key=lambda x: (-x.get("score", 0), -x.get("edge_score", 0), x.get("symbol", "")))
    return {
        "count": len(results),
        "results": results,
        "completed_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"watchlist_id": watchlist_id, "workers": workers},
    }
