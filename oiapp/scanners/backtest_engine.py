# oiapp/scanners/backtest_engine.py
"""
Backtests Signal Notifier alerts that came from the options-trade engine
(PS/CS/IC — Trade Opportunity Scanner and direction-tagged scanner_query/
dashboard_tile sources) against what the underlying actually did by
expiry, using locally cached daily closes (price_cache) — no live fetch
needed for symbols already tracked by a watchlist.

This is deliberately narrow in scope: it answers "if every one of these
alerts had been taken as a credit spread held to expiry with no early
management, what would the P&L have been, and how did the realized win
rate compare to the scanner's own POP estimate?" It does NOT simulate
early profit-taking/stop-outs (your actual Trade Management plan usually
exits well before expiry) — this is an expiry-only, worst-case-timing
baseline, useful for validating the scanner's calibration, not a precise
P&L replay of how you'd actually trade it.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")

_LEG_RE_SINGLE = re.compile(r"Sell\s*([\d.]+)\s*([PC])\s*/\s*Buy\s*([\d.]+)\s*([PC])", re.I)
_LEG_RE_IC = re.compile(
    r"Sell\s*([\d.]+)\s*P\s*/\s*Buy\s*([\d.]+)\s*P.*?Sell\s*([\d.]+)\s*C\s*/\s*Buy\s*([\d.]+)\s*C", re.I
)


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _parse_legs_fallback(trade_type: str, legs: str) -> Dict[str, Optional[float]]:
    """Only used for alerts logged before structured strike columns existed
    — new alerts already have short_put_strike etc. populated directly."""
    out = {"short_put": None, "long_put": None, "short_call": None, "long_call": None}
    if not legs:
        return out
    if trade_type == "IC":
        m = _LEG_RE_IC.search(legs)
        if m:
            out["short_put"], out["long_put"] = float(m.group(1)), float(m.group(2))
            out["short_call"], out["long_call"] = float(m.group(3)), float(m.group(4))
        return out
    m = _LEG_RE_SINGLE.search(legs)
    if not m:
        return out
    s1, side1, s2, side2 = float(m.group(1)), m.group(2).upper(), float(m.group(3)), m.group(4).upper()
    if side1 == "P":
        out["short_put"], out["long_put"] = s1, s2
    else:
        out["short_call"], out["long_call"] = s1, s2
    return out


def _price_near_date(symbol: str, target_date: str, lookahead_days: int = 6) -> Optional[Dict[str, Any]]:
    """First available close on or after target_date (handles the exact
    expiry date landing on a weekend/holiday). Returns {"date":..,"close":..}
    or None if nothing's cached in that window."""
    try:
        start = datetime.strptime(target_date, "%Y-%m-%d").date()
    except Exception:
        return None
    end = start + timedelta(days=lookahead_days)
    con = _conn()
    try:
        row = con.execute(
            """SELECT date, close FROM price_cache
               WHERE symbol=? AND date>=? AND date<=? AND close IS NOT NULL
               ORDER BY date ASC LIMIT 1""",
            (symbol, start.isoformat(), end.isoformat()),
        ).fetchone()
        return {"date": row["date"], "close": row["close"]} if row else None
    finally:
        con.close()


def _credit_spread_pnl(short: float, long: float, credit: float, is_short_lower: bool, expiry_price: float) -> float:
    """Generic credit-spread expiry P&L per contract (1 share-equivalent,
    i.e. matches the same $/point convention est_credit/max_loss already
    use elsewhere in this app — multiply by 100 for a standard contract).

    is_short_lower=True means the short strike is BELOW the long strike
    (call credit spread shape: short call < long call — max profit when
    price stays AT OR BELOW the short strike).
    is_short_lower=False means short strike is ABOVE long strike (put
    credit spread shape: short put > long put — max profit when price
    stays AT OR ABOVE the short strike).
    """
    width = abs(long - short)
    max_loss = max(0.0, width - credit)
    if is_short_lower:  # call side
        if expiry_price <= short:
            return credit
        if expiry_price >= long:
            return -max_loss
        return credit - (expiry_price - short)
    else:  # put side
        if expiry_price >= short:
            return credit
        if expiry_price <= long:
            return -max_loss
        return credit - (short - expiry_price)


def compute_outcome_for_alert(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Returns {"outcome": str, "pnl": float, "pnl_pct": float,
    "expiry_price": float, "expiry_date_used": str} or None if there isn't
    enough data yet (expiry hasn't actually happened, or no cached price)."""
    trade_type = row.get("trade_type")
    expiry = row.get("expiry")
    credit = row.get("est_credit")
    if trade_type not in ("PS", "CS", "IC") or not expiry or credit is None:
        return None

    try:
        if datetime.strptime(expiry, "%Y-%m-%d").date() > date.today():
            return None  # hasn't expired yet
    except Exception:
        return None

    short_put, long_put = row.get("short_put_strike"), row.get("long_put_strike")
    short_call, long_call = row.get("short_call_strike"), row.get("long_call_strike")
    if trade_type in ("PS", "CS") and short_put is None and short_call is None:
        parsed = _parse_legs_fallback(trade_type, row.get("legs") or "")
        short_put, long_put = parsed["short_put"], parsed["long_put"]
        short_call, long_call = parsed["short_call"], parsed["long_call"]
    elif trade_type == "IC" and short_put is None and short_call is None:
        parsed = _parse_legs_fallback(trade_type, row.get("legs") or "")
        short_put, long_put = parsed["short_put"], parsed["long_put"]
        short_call, long_call = parsed["short_call"], parsed["long_call"]

    price_info = _price_near_date(row.get("symbol"), expiry)
    if not price_info:
        return None
    expiry_price = price_info["close"]

    try:
        if trade_type == "PS":
            if short_put is None or long_put is None:
                return None
            pnl = _credit_spread_pnl(short_put, long_put, credit, is_short_lower=False, expiry_price=expiry_price)
            max_loss = row.get("max_loss_amt") or max(0.0, abs(short_put - long_put) - credit)
        elif trade_type == "CS":
            if short_call is None or long_call is None:
                return None
            pnl = _credit_spread_pnl(short_call, long_call, credit, is_short_lower=True, expiry_price=expiry_price)
            max_loss = row.get("max_loss_amt") or max(0.0, abs(long_call - short_call) - credit)
        else:  # IC
            if None in (short_put, long_put, short_call, long_call):
                return None
            put_credit = credit / 2.0
            call_credit = credit / 2.0
            pnl_put = _credit_spread_pnl(short_put, long_put, put_credit, is_short_lower=False, expiry_price=expiry_price)
            pnl_call = _credit_spread_pnl(short_call, long_call, call_credit, is_short_lower=True, expiry_price=expiry_price)
            pnl = pnl_put + pnl_call
            max_loss = row.get("max_loss_amt") or max(
                abs(short_put - long_put) - put_credit, abs(long_call - short_call) - call_credit
            )
    except Exception:
        return None

    max_loss = max_loss or 0.01
    pnl_pct = round((pnl / max_loss) * 100, 1) if max_loss else None
    if pnl >= credit * 0.99:
        outcome = "max_profit"
    elif pnl <= -max_loss * 0.99:
        outcome = "max_loss"
    elif pnl > 0:
        outcome = "partial_win"
    else:
        outcome = "partial_loss"

    return {
        "outcome": outcome,
        "pnl": round(pnl, 2),
        "pnl_pct": pnl_pct,
        "expiry_price": expiry_price,
        "expiry_date_used": price_info["date"],
    }


def run_backtest(date_from: str = "", date_to: str = "", force_recompute: bool = False) -> Dict[str, Any]:
    """Computes (and caches) outcomes for every expired PS/CS/IC alert in
    range, then returns both the per-alert results and aggregate stats
    comparing realized win rate to the scanner's own predicted POP."""
    con = _conn()
    try:
        clauses = ["trade_type IN ('PS','CS','IC')", "expiry IS NOT NULL", "expiry != ''"]
        params: List[Any] = []
        if date_from:
            clauses.append("alert_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("alert_date <= ?")
            params.append(date_to)
        if not force_recompute:
            clauses.append("(bt_outcome IS NULL OR bt_outcome = '')")
        where = " AND ".join(clauses)
        rows = con.execute(f"SELECT * FROM signal_notifier_alerts WHERE {where} ORDER BY id DESC", params).fetchall()

        computed = 0
        for r in rows:
            row = dict(r)
            outcome = compute_outcome_for_alert(row)
            if not outcome:
                continue
            con.execute(
                """UPDATE signal_notifier_alerts
                   SET bt_outcome=?, bt_pnl=?, bt_pnl_pct=?, bt_expiry_price=?, bt_computed_at=?
                   WHERE id=?""",
                (outcome["outcome"], outcome["pnl"], outcome["pnl_pct"], outcome["expiry_price"],
                 datetime.now().isoformat(), row["id"]),
            )
            computed += 1
        con.commit()

        # Pull everything with a computed outcome in range for the report
        clauses2 = ["trade_type IN ('PS','CS','IC')", "bt_outcome IS NOT NULL", "bt_outcome != ''"]
        params2: List[Any] = []
        if date_from:
            clauses2.append("alert_date >= ?")
            params2.append(date_from)
        if date_to:
            clauses2.append("alert_date <= ?")
            params2.append(date_to)
        where2 = " AND ".join(clauses2)
        results = [dict(r) for r in con.execute(
            f"SELECT * FROM signal_notifier_alerts WHERE {where2} ORDER BY expiry DESC", params2
        ).fetchall()]
    finally:
        con.close()

    def _bucket_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(rows)
        if n == 0:
            return {"count": 0}
        wins = [r for r in rows if (r.get("bt_pnl") or 0) > 0]
        total_pnl = sum((r.get("bt_pnl") or 0) for r in rows)
        pops = [r.get("pop") for r in rows if r.get("pop") is not None]
        avg_pop = round(sum(pops) / len(pops), 1) if pops else None
        return {
            "count": n,
            "realized_win_rate_pct": round(100 * len(wins) / n, 1),
            "avg_predicted_pop_pct": avg_pop,
            "calibration_gap_pts": round(round(100 * len(wins) / n, 1) - avg_pop, 1) if avg_pop is not None else None,
            "total_pnl_per_contract": round(total_pnl, 2),
            "avg_pnl_per_contract": round(total_pnl / n, 2),
        }

    by_grade: Dict[str, List[Dict]] = {}
    by_type: Dict[str, List[Dict]] = {}
    for r in results:
        by_grade.setdefault(r.get("grade") or "?", []).append(r)
        by_type.setdefault(r.get("trade_type") or "?", []).append(r)

    return {
        "ok": True,
        "computed_this_run": computed,
        "total_backtested": len(results),
        "overall": _bucket_stats(results),
        "by_grade": {k: _bucket_stats(v) for k, v in by_grade.items()},
        "by_trade_type": {k: _bucket_stats(v) for k, v in by_type.items()},
        "results": results,
    }
