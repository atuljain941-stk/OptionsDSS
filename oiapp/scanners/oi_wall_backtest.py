"""oi_wall_backtest.py -- V136/V137/V138. Backtests OI-wall / OI-buildup
signals against the `options` table (oiapp/db.py) -- a genuine daily
append-only archive of the full options chain (OI, volume, bid/ask/
last, IV, underlying price), snapshotted once per day and never
overwritten across days. DIFFERENT from oi_positional_dte_cache and
gex_plan_snapshots, both of which are rolling caches/overwrite-in-place
and cannot support a real backtest -- confirmed by reading their
schemas (compound PRIMARY KEY on symbol+strike with no date, or
explicitly documented as an "intraday UI cache" purged daily).

V138 rewrites the core walk-forward logic per explicit spec:
  - Exact DTE (not a daily/weekly/monthly bucket) drives everything.
  - A fixed number of consecutive runs, starting from start_date, each
    landing on the entry day for that cycle's expiry (first weekly
    starting 6/1 with 5 DTE -> entry 6/1 for the 6/5 expiry, next run
    6/8 for 6/12, etc.). V139 fix: the cadence between runs is
    dte_days CALENDAR days, not a hardcoded +7 -- the +7-always
    version only happened to be right for ~5 DTE (5 days naturally
    rolls a Monday start to the next Monday) and was wrong for
    shorter/longer DTE (e.g. a 2-DTE backtest cycled weekly instead of
    every ~2 days, confirmed wrong against real test results).
  - Strategy is SELECTED per run, not assumed: IC if both walls
    qualify, CS if only the call wall does, PS if only the put wall
    does, no trade at all if neither does.
  - A symbol-level total-OI floor skips thin names before any
    strategy logic runs.
  - Results are grouped per symbol by strategy (CS/PS/IC) with
    win/loss counts, not just an overall containment rate.

Since this only has ~1 month of history (as of when this was built),
results should be read as "does this look promising enough to keep
collecting data and re-test," not a statistically reliable long-run
win rate -- once split by symbol AND strategy, sample sizes get small
fast.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..db import _connect


def explore_oi_history() -> Dict[str, Any]:
    """What's actually in the options table right now -- symbols,
    date range, expiry counts. Run this FIRST; the backtest below is
    only as good as what this reports actually exists."""
    con = _connect()
    try:
        symbols = con.execute("""
            SELECT symbol,
                   MIN(date) as first_date, MAX(date) as last_date,
                   COUNT(DISTINCT date) as distinct_days,
                   COUNT(DISTINCT expiration) as distinct_expirations,
                   COUNT(*) as total_rows
            FROM options
            GROUP BY symbol
            ORDER BY distinct_days DESC
        """).fetchall()
        out = []
        for r in symbols:
            d = dict(r)
            exps = con.execute("SELECT DISTINCT expiration FROM options WHERE symbol=? ORDER BY expiration", (d["symbol"],)).fetchall()
            exp_dates = [e["expiration"] for e in exps if e["expiration"]]
            d["sample_expirations"] = exp_dates[:10]
            out.append(d)
        return {"ok": True, "symbols": out}
    finally:
        con.close()


def _wall_metrics(con, symbol: str, expiration: str, date: str) -> Optional[Dict[str, Any]]:
    rows = con.execute(
        "SELECT type, strike, oi, underlying, iv FROM options WHERE symbol=? AND expiration=? AND date=?",
        (symbol, expiration, date)
    ).fetchall()
    if not rows:
        return None
    calls = [(r["strike"], r["oi"], r["iv"]) for r in rows if r["type"] == "call" and r["oi"]]
    puts = [(r["strike"], r["oi"], r["iv"]) for r in rows if r["type"] == "put" and r["oi"]]
    underlying = next((r["underlying"] for r in rows if r["underlying"]), None)
    if not calls or not puts or underlying is None:
        return None
    call_wall_strike, call_wall_oi, call_wall_iv = max(calls, key=lambda t: t[1])
    put_wall_strike, put_wall_oi, put_wall_iv = max(puts, key=lambda t: t[1])
    total_call_oi = sum(oi for _, oi, _ in calls)
    total_put_oi = sum(oi for _, oi, _ in puts)
    return {
        "date": date, "underlying": underlying,
        "call_wall_strike": call_wall_strike, "call_wall_oi": call_wall_oi, "call_wall_iv": call_wall_iv,
        "call_wall_strength_pct": round(call_wall_oi / total_call_oi * 100, 1) if total_call_oi else None,
        "put_wall_strike": put_wall_strike, "put_wall_oi": put_wall_oi, "put_wall_iv": put_wall_iv,
        "put_wall_strength_pct": round(put_wall_oi / total_put_oi * 100, 1) if total_put_oi else None,
        "total_call_oi": total_call_oi, "total_put_oi": total_put_oi,
        "total_oi": total_call_oi + total_put_oi,
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_delta(spot: float, strike: float, dte_days: float, iv: float, right: str, risk_free_rate: float = 0.05) -> Optional[float]:
    """Standard Black-Scholes delta, self-contained (no scipy dep)."""
    if not spot or not strike or not iv or iv <= 0 or dte_days <= 0:
        return None
    t = dte_days / 365.0
    try:
        d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    except (ValueError, ZeroDivisionError):
        return None
    if right == "call":
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0


def _wall_passes_filters(
    wall_strike: float, wall_strength_pct: Optional[float], wall_iv: Optional[float],
    entry_price: float, dte_days: int, right: str,
    min_wall_strength_pct: float, max_wall_distance_pct: Optional[float],
    use_delta_filter: bool, delta_min: float, delta_max: float,
) -> bool:
    if wall_strength_pct is None or wall_strength_pct < min_wall_strength_pct:
        return False
    if max_wall_distance_pct is not None:
        dist_pct = abs(wall_strike - entry_price) / entry_price * 100 if entry_price else None
        if dist_pct is None or dist_pct > max_wall_distance_pct:
            return False
    if use_delta_filter:
        delta = _bs_delta(entry_price, wall_strike, dte_days, wall_iv, right)
        if delta is None or not (delta_min <= abs(delta) <= delta_max):
            return False
    return True


def _nearest_date_on_or_after(con, symbol: str, target: str) -> Optional[str]:
    row = con.execute(
        "SELECT MIN(date) as d FROM options WHERE symbol=? AND date>=?", (symbol, target)
    ).fetchone()
    return row["d"] if row and row["d"] else None


def _nearest_date_on_or_before(con, symbol: str, target: str) -> Optional[str]:
    """Latest available snapshot date on/before `target` -- used for
    live/current-moment lookups (candle-context scan, general scanner
    conditions) where "as of right now" means the most recent capture,
    not a specific historical entry day."""
    row = con.execute(
        "SELECT MAX(date) as d FROM options WHERE symbol=? AND date<=?", (symbol, target)
    ).fetchone()
    return row["d"] if row and row["d"] else None


def get_oi_wall_snapshot(
    symbol: str, as_of_date: Optional[str] = None, dte_days: Optional[int] = None,
    min_wall_strength_pct: float = 15.0, max_wall_distance_pct: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """Live/current-moment OI wall lookup for ONE symbol -- the shared
    entrypoint for anything outside the dedicated backtest that needs
    wall info (candle-context scanner, general scanner conditions,
    strategy engines). Uses the latest available snapshot on/before
    as_of_date (defaults to today), and the nearest expiration to
    dte_days out if given, else the nearest upcoming expiration at all.
    Returns None if this symbol simply has no OI history captured --
    callers should treat that as "no data," not an error, and degrade
    gracefully (this table only covers symbols actively being
    archived, a subset of any given watchlist).
    """
    con = _connect()
    try:
        if not as_of_date:
            as_of_date = datetime.now().strftime("%Y-%m-%d")
        entry_date = _nearest_date_on_or_before(con, symbol, as_of_date)
        if not entry_date:
            return None

        if dte_days:
            target_exp = (datetime.strptime(as_of_date, "%Y-%m-%d") + timedelta(days=int(dte_days))).strftime("%Y-%m-%d")
            expiration = _nearest_expiration(con, symbol, target_exp)
        else:
            row = con.execute(
                "SELECT MIN(expiration) as e FROM options WHERE symbol=? AND expiration>=?", (symbol, as_of_date)
            ).fetchone()
            expiration = row["e"] if row and row["e"] else None
        if not expiration:
            return None

        metrics = _wall_metrics(con, symbol, expiration, entry_date)
        if not metrics:
            return None

        entry_price = metrics["underlying"]
        try:
            actual_dte = (datetime.strptime(expiration, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days
        except ValueError:
            actual_dte = int(dte_days or 5)

        call_wall_ok = _wall_passes_filters(
            metrics["call_wall_strike"], metrics["call_wall_strength_pct"], metrics["call_wall_iv"],
            entry_price, actual_dte, "call", min_wall_strength_pct, max_wall_distance_pct, False, 0, 1,
        )
        put_wall_ok = _wall_passes_filters(
            metrics["put_wall_strike"], metrics["put_wall_strength_pct"], metrics["put_wall_iv"],
            entry_price, actual_dte, "put", min_wall_strength_pct, max_wall_distance_pct, False, 0, 1,
        )
        if call_wall_ok and put_wall_ok:
            recommended_strategy = "IC"
        elif call_wall_ok:
            recommended_strategy = "CS"
        elif put_wall_ok:
            recommended_strategy = "PS"
        else:
            recommended_strategy = None

        call_dist_pct = abs(metrics["call_wall_strike"] - entry_price) / entry_price * 100 if entry_price else None
        put_dist_pct = abs(metrics["put_wall_strike"] - entry_price) / entry_price * 100 if entry_price else None

        return {
            "symbol": symbol, "as_of_date": entry_date, "expiration": expiration, "dte_days": actual_dte,
            "underlying": entry_price,
            "call_wall_strike": metrics["call_wall_strike"], "call_wall_strength_pct": metrics["call_wall_strength_pct"],
            "call_wall_distance_pct": round(call_dist_pct, 2) if call_dist_pct is not None else None,
            "call_wall_qualifies": call_wall_ok,
            "put_wall_strike": metrics["put_wall_strike"], "put_wall_strength_pct": metrics["put_wall_strength_pct"],
            "put_wall_distance_pct": round(put_dist_pct, 2) if put_dist_pct is not None else None,
            "put_wall_qualifies": put_wall_ok,
            "recommended_strategy": recommended_strategy,
        }
    finally:
        con.close()


def _nearest_expiration(con, symbol: str, target: str, tolerance_days: int = 5) -> Optional[str]:
    """Nearest actual expiration this symbol has ANY data for, closest
    to `target` (calendar days), within tolerance_days either way --
    matches "if there isn't an expiry exactly on the DTE target, use
    the nearest one instead of skipping the whole run"."""
    rows = con.execute("SELECT DISTINCT expiration FROM options WHERE symbol=?", (symbol,)).fetchall()
    target_dt = datetime.strptime(target, "%Y-%m-%d")
    best, best_diff = None, None
    for r in rows:
        exp = r["expiration"]
        if not exp:
            continue
        try:
            exp_dt = datetime.strptime(exp, "%Y-%m-%d")
        except ValueError:
            continue
        diff = abs((exp_dt - target_dt).days)
        if diff <= tolerance_days and (best_diff is None or diff < best_diff):
            best, best_diff = exp, diff
    return best


def backtest_oi_walls(
    symbols: List[str],
    dte_days: int = 5,
    start_date: str = "",
    num_runs: int = 3,
    min_wall_strength_pct: float = 15.0,
    min_total_oi: int = 1000,
    max_wall_distance_pct: Optional[float] = None,
    use_delta_filter: bool = False,
    delta_min: float = 0.10,
    delta_max: float = 0.40,
) -> Dict[str, Any]:
    """Walk-forward schedule: run 1 enters on start_date (or the
    nearest captured trading day on/after it), targeting an expiry
    dte_days out (nearest actual expiration within +/-5 calendar days
    if there's no exact match). Each subsequent run advances dte_days
    CALENDAR days from the previous entry (matches how a real options
    cycle actually recurs -- a 5-DTE weekly's next cycle starts ~5
    calendar days later, which naturally lands on the next Monday;
    a 2-DTE cycle's next entry is ~2 days later, cycling much faster).
    If that lands on a weekend/non-trading day, _nearest_date_on_or_after
    rolls it forward to the next day data actually exists for. Earlier
    versions of this hardcoded a fixed +7 days regardless of dte_days,
    which only happened to be correct for ~5 DTE by coincidence and
    was wrong for anything shorter or longer -- fixed to scale with
    dte_days instead. Continues for num_runs.

    Per run: computes both walls' strength/distance/delta at entry,
    and SELECTS a strategy rather than assuming one --
      - both walls qualify -> Iron Condor (IC)
      - only the call wall qualifies -> Credit Call Spread (CS)
      - only the put wall qualifies -> Credit Put Spread (PS)
      - neither qualifies -> no trade this run
    Win/loss uses the same simple "stayed on the safe side of the
    short strike at expiry" convention as the existing chronological
    options backtest (checked at the last captured date for that
    expiration, the closest proxy to expiry price available).
    """
    if not start_date:
        return {"ok": False, "error": "start_date is required"}
    con = _connect()
    try:
        try:
            cursor_date = datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            return {"ok": False, "error": "start_date must be YYYY-MM-DD"}

        per_symbol: Dict[str, Any] = {}
        all_trades: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        for symbol in symbols:
            sym_result = {"symbol": symbol, "trades": 0,
                          "CS": {"wins": 0, "losses": 0}, "PS": {"wins": 0, "losses": 0}, "IC": {"wins": 0, "losses": 0},
                          "no_trade_runs": 0}
            run_cursor = cursor_date

            for run_idx in range(int(num_runs)):
                entry_target = run_cursor.strftime("%Y-%m-%d")
                entry_date = _nearest_date_on_or_after(con, symbol, entry_target)
                if not entry_date:
                    skipped.append({"symbol": symbol, "run": run_idx + 1, "reason": f"no captured data on/after {entry_target}"})
                    run_cursor += timedelta(days=int(dte_days))
                    continue

                expiry_target = (run_cursor + timedelta(days=int(dte_days))).strftime("%Y-%m-%d")
                expiration = _nearest_expiration(con, symbol, expiry_target)
                if not expiration:
                    skipped.append({"symbol": symbol, "run": run_idx + 1, "reason": f"no expiration found near {expiry_target} (+/-5 days)"})
                    run_cursor += timedelta(days=int(dte_days))
                    continue

                entry_metrics = _wall_metrics(con, symbol, expiration, entry_date)
                if not entry_metrics:
                    skipped.append({"symbol": symbol, "run": run_idx + 1, "expiration": expiration, "reason": "missing call or put data on entry day"})
                    run_cursor += timedelta(days=int(dte_days))
                    continue

                if entry_metrics["total_oi"] < min_total_oi:
                    skipped.append({"symbol": symbol, "run": run_idx + 1, "expiration": expiration,
                                     "reason": f"total OI {entry_metrics['total_oi']} below floor ({min_total_oi})"})
                    run_cursor += timedelta(days=int(dte_days))
                    continue

                try:
                    actual_dte = (datetime.strptime(expiration, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days
                except ValueError:
                    actual_dte = int(dte_days)

                entry_price = entry_metrics["underlying"]
                call_wall, put_wall = entry_metrics["call_wall_strike"], entry_metrics["put_wall_strike"]

                call_wall_ok = _wall_passes_filters(
                    call_wall, entry_metrics["call_wall_strength_pct"], entry_metrics["call_wall_iv"],
                    entry_price, actual_dte, "call", min_wall_strength_pct, max_wall_distance_pct,
                    use_delta_filter, delta_min, delta_max,
                )
                put_wall_ok = _wall_passes_filters(
                    put_wall, entry_metrics["put_wall_strength_pct"], entry_metrics["put_wall_iv"],
                    entry_price, actual_dte, "put", min_wall_strength_pct, max_wall_distance_pct,
                    use_delta_filter, delta_min, delta_max,
                )

                if call_wall_ok and put_wall_ok:
                    strategy = "IC"
                elif call_wall_ok:
                    strategy = "CS"
                elif put_wall_ok:
                    strategy = "PS"
                else:
                    strategy = None

                if strategy is None:
                    sym_result["no_trade_runs"] += 1
                    skipped.append({"symbol": symbol, "run": run_idx + 1, "expiration": expiration,
                                     "reason": "neither wall qualified within strength/distance/delta filters -- no trade"})
                    run_cursor += timedelta(days=int(dte_days))
                    continue

                last_row = con.execute(
                    "SELECT date, underlying FROM options WHERE symbol=? AND expiration=? AND underlying IS NOT NULL ORDER BY date DESC LIMIT 1",
                    (symbol, expiration)
                ).fetchone()
                if not last_row:
                    skipped.append({"symbol": symbol, "run": run_idx + 1, "expiration": expiration, "reason": "no outcome price available"})
                    run_cursor += timedelta(days=int(dte_days))
                    continue
                final_price = last_row["underlying"]

                if strategy == "CS":
                    win = final_price <= call_wall
                elif strategy == "PS":
                    win = final_price >= put_wall
                else:
                    win = (final_price <= call_wall) and (final_price >= put_wall)

                trade = {
                    "symbol": symbol, "run": run_idx + 1, "strategy": strategy,
                    "entry_date": entry_date, "expiration": expiration, "dte_days": actual_dte,
                    "entry_price": entry_price, "final_price": final_price,
                    "call_wall_strike": call_wall if call_wall_ok else None,
                    "put_wall_strike": put_wall if put_wall_ok else None,
                    "win": win,
                }
                all_trades.append(trade)
                sym_result["trades"] += 1
                sym_result[strategy]["wins" if win else "losses"] += 1

                run_cursor += timedelta(days=int(dte_days))

            per_symbol[symbol] = sym_result

        skip_reason_summary = _summarize_skip_reasons(skipped)
        overall_summary = _build_overall_summary(all_trades, per_symbol)

        return {
            "ok": True, "dte_days": dte_days, "start_date": start_date, "num_runs": num_runs,
            "trade_count": len(all_trades),
            "overall_summary": overall_summary,
            "per_symbol": list(per_symbol.values()),
            "trades": all_trades, "skipped": skipped[:500],
            "skip_reason_summary": skip_reason_summary,
        }
    finally:
        con.close()


def _build_overall_summary(all_trades: List[Dict[str, Any]], per_symbol: Dict[str, Any]) -> Dict[str, Any]:
    by_strategy: Dict[str, Dict[str, int]] = {"CS": {"wins": 0, "losses": 0}, "PS": {"wins": 0, "losses": 0}, "IC": {"wins": 0, "losses": 0}}
    for t in all_trades:
        by_strategy[t["strategy"]]["wins" if t["win"] else "losses"] += 1

    def _wr(d: Dict[str, int]) -> Optional[float]:
        total = d["wins"] + d["losses"]
        return round(d["wins"] / total * 100, 1) if total else None

    total_wins = sum(d["wins"] for d in by_strategy.values())
    total_losses = sum(d["losses"] for d in by_strategy.values())
    symbols_with_trades = sum(1 for s in per_symbol.values() if s["trades"] > 0)
    symbols_no_trade_only = sum(1 for s in per_symbol.values() if s["trades"] == 0 and s["no_trade_runs"] > 0)

    return {
        "total_trades": len(all_trades),
        "total_wins": total_wins, "total_losses": total_losses,
        "overall_win_rate_pct": round(total_wins / (total_wins + total_losses) * 100, 1) if (total_wins + total_losses) else None,
        "symbols_with_at_least_one_trade": symbols_with_trades,
        "symbols_scanned": len(per_symbol),
        "by_strategy": {
            "CS": {**by_strategy["CS"], "win_rate_pct": _wr(by_strategy["CS"])},
            "PS": {**by_strategy["PS"], "win_rate_pct": _wr(by_strategy["PS"])},
            "IC": {**by_strategy["IC"], "win_rate_pct": _wr(by_strategy["IC"])},
        },
    }


def _summarize_skip_reasons(skipped: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Groups skip reasons by CATEGORY (stripping the dynamic values,
    e.g. "total OI 420 below floor (1000)" -> "total OI below floor")
    so a 0-trade run tells you WHERE it failed at a glance instead of
    a bare count -- e.g. "990 skipped: no expiration found near
    target" points straight at the DTE/tolerance settings, while
    "990 skipped: neither wall qualified" points at the strength/
    distance/delta filters instead. Different problems, different fix.
    """
    import re
    categories: Dict[str, Dict[str, Any]] = {}
    for s in skipped:
        reason = s.get("reason", "unknown")
        # Strip specific numbers/dates so "total OI 420 below floor
        # (1000)" and "total OI 87 below floor (1000)" collapse into
        # one category instead of hundreds of near-duplicate rows.
        category = re.sub(r"[\d.]+", "#", reason)
        category = re.sub(r"#-#-#|#/#/#", "<date>", category)
        if category not in categories:
            categories[category] = {"reason_pattern": category, "count": 0, "example": reason}
        categories[category]["count"] += 1
    return sorted(categories.values(), key=lambda c: c["count"], reverse=True)
