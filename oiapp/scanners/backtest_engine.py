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

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

_LEG_RE_SINGLE = re.compile(r"Sell\s*([\d.]+)\s*([PC])\s*/\s*Buy\s*([\d.]+)\s*([PC])", re.I)
_LEG_RE_IC = re.compile(
    r"Sell\s*([\d.]+)\s*P\s*/\s*Buy\s*([\d.]+)\s*P.*?Sell\s*([\d.]+)\s*C\s*/\s*Buy\s*([\d.]+)\s*C", re.I
)


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
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


def run_backtest(date_from: str = "", date_to: str = "", force_recompute: bool = False,
                  symbol: str = "", source_label: str = "") -> Dict[str, Any]:
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
        if symbol:
            clauses.append("UPPER(symbol) = ?")
            params.append(symbol.upper().strip())
        if source_label:
            clauses.append("source_label = ?")
            params.append(source_label)
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
        if symbol:
            clauses2.append("UPPER(symbol) = ?")
            params2.append(symbol.upper().strip())
        if source_label:
            clauses2.append("source_label = ?")
            params2.append(source_label)
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

    by_grade_stats = {k: _bucket_stats(v) for k, v in by_grade.items()}
    by_type_stats = {k: _bucket_stats(v) for k, v in by_type.items()}

    factor_cal = _factor_calibration(results)
    try:
        from ..services.scoring_params import get_params
        current_live_params = get_params()
    except Exception:
        current_live_params = {}

    return {
        "ok": True,
        "computed_this_run": computed,
        "total_backtested": len(results),
        "overall": _bucket_stats(results),
        "by_grade": by_grade_stats,
        "by_trade_type": by_type_stats,
        "results": results,
        "calibration_suggestions": _calibration_suggestions(by_grade_stats, by_type_stats),
        "factor_calibration": factor_cal,
        "specific_param_suggestions": _specific_param_suggestions(factor_cal, current_live_params),
    }


# ── Calibration suggestions ──────────────────────────────────────────────
# NOT automatic reweighting -- deliberately a suggestions list a human
# reviews, not code that changes _entry_score's weights on its own. Full
# automatic tuning would need the individual scoring factors (pros/cons)
# that produced each score, which this app has only just started
# persisting per alert (see signal_notifier._log_alert's score_factors
# column) -- there isn't enough factor-level history yet to do that
# properly. This works with what's available today: grade/type-level
# calibration gaps, translated into concrete, reviewable suggestions
# rather than a raw number to interpret yourself.

# ── Factor-level calibration (parses the actual "Why:"/"Caution:" text
# already sent in each alert's `message` column) ──────────────────────────
# _format_message() in signal_notifier.py builds these deterministically:
# "Why: " + "; ".join(pros), "Caution: " + "; ".join(cons) -- the exact
# same pros/cons list _entry_score() returns, already sitting in every
# alert ever sent (message column), not something that needs new logging
# to start accumulating. This parses that text back into per-factor
# presence per alert, then compares realized win rate for alerts where a
# factor appeared as a pro vs alerts where it didn't appear at all --
# that comparison is the actual answer to "does this factor deserve the
# weight _entry_score gives it." A factor whose "present as pro" bucket
# doesn't win more than the "absent" baseline isn't earning its weight;
# one that separates cleanly is validated by real outcomes, not just the
# grade/type-level view above.
#
# Keyword matching is intentionally loose (case-insensitive substring),
# not a strict parser -- alert message wording has changed as the scoring
# engine has been tuned, so this needs to still match older phrasing
# ("IVR 58%") alongside newer phrasing ("RS +2.7% vs SPY (mild)") rather
# than only understanding today's exact strings.
FACTOR_KEYWORDS = {
    "Regime": ["regime"],
    "RS vs SPY": ["rs +", "rs -", "rs vs spy", " rs "],
    "RSIdiff90 / momentum": ["rsidiff", "momentum"],
    "PCR": ["pcr "],
    "IV Rank": ["ivr ", "iv rank"],
    "Put wall": ["put wall"],
    "Call wall": ["call wall"],
    "Gamma flip": ["gamma flip"],
    "Max pain": ["max pain"],
    "POP/RR breakeven": ["breakeven", "unfavorable math"],
}

# Maps each factor category to the ACTUAL scoring_params.py keys that
# control its weight, so a calibration finding can turn into a specific
# "change X from A to B" instruction instead of "consider adjusting this
# factor's weight." Put wall and Call wall share the same underlying
# params (wall_strong/wall_mild/wall_far_penalty) since _entry_score
# doesn't weight them separately by side.
FACTOR_TO_PARAM_KEYS = {
    "Regime": ["regime_points"],  # nested dict -- all 9 graduated values scale together
    "RS vs SPY": ["rs_bull_strong", "rs_bull_mild", "rs_bull_weak_neg", "rs_bull_strong_neg"],
    "RSIdiff90 / momentum": ["rsi_diff_strong_bonus", "rsi_diff_mild_bonus",
                              "rsi_diff_strong_penalty", "rsi_diff_mild_penalty"],
    "PCR": ["pcr_strong", "pcr_mild", "pcr_strong_against", "pcr_mild_against"],
    "IV Rank": ["iv_rank_credit_high", "iv_rank_credit_good", "iv_rank_credit_low", "iv_rank_credit_thin",
                "iv_rank_debit_cheap", "iv_rank_debit_reasonable", "iv_rank_debit_expensive"],
    "Put wall": ["wall_strong", "wall_mild", "wall_far_penalty"],
    "Call wall": ["wall_strong", "wall_mild", "wall_far_penalty"],
    "Gamma flip": ["gamma_flip_base_points", "gamma_flip_near_penalty"],
    "Max pain": ["max_pain_bonus", "max_pain_penalty"],
    "POP/RR breakeven": ["pop_rr_penalty", "pop_rr_thin_margin_penalty",
                          "pop_rr_strong_edge_bonus", "pop_rr_mild_edge_bonus"],
}

# Separation-to-scale mapping: how much to nudge the mapped params based
# on how well (or poorly) a factor's presence actually predicted the
# realized outcome. Deliberately conservative moves (15-30%), not wholesale
# rewrites -- these are suggestions to review and apply via the Scoring
# Parameters page, not an automatic rewrite of the scoring engine.
def _suggest_scale(separation_pts: float) -> Optional[float]:
    if separation_pts is None:
        return None
    if separation_pts >= 15:
        return 1.25   # strongly validated -- worth carrying more weight
    if separation_pts >= 8:
        return 1.10   # validated -- small increase
    if separation_pts < 0:
        return 0.60   # actively predicting the WRONG direction -- cut hard
    if separation_pts < 3:
        return 0.70   # weak -- not earning its current weight
    return None        # 3-8pts: roughly calibrated already, no suggestion


def _specific_param_suggestions(factor_results: List[Dict[str, Any]], current_params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Turns each factor_calibration() finding into a concrete parameter
    change: which key(s) in scoring_params.py, current value, suggested
    new value, and why. This is what actually answers "what value do I
    set" instead of leaving that translation to the person reading the
    table.
    """
    out = []
    for f in factor_results:
        factor = f.get("factor")
        sep = f.get("separation_pts")
        n = f.get("as_pro_count", 0)
        scale = _suggest_scale(sep)
        if scale is None or n < 10:
            continue
        keys = FACTOR_TO_PARAM_KEYS.get(factor, [])
        if not keys:
            continue
        for key in keys:
            current_val = current_params.get(key)
            if current_val is None:
                continue
            if isinstance(current_val, dict):
                # regime_points: scale every value in the nested dict,
                # preserving sign (a -18 stays negative, scaled by magnitude)
                new_val = {k: round(v * scale, 1) if isinstance(v, (int, float)) else v
                           for k, v in current_val.items()}
                changed = any(new_val[k] != current_val[k] for k in current_val)
                if not changed:
                    continue
            elif isinstance(current_val, (int, float)):
                new_val = round(current_val * scale, 2)
                if new_val == current_val:
                    continue
            else:
                continue
            direction = "increase" if scale > 1 else "decrease"
            out.append({
                "factor": factor, "param_key": key,
                "current_value": current_val, "suggested_value": new_val,
                "scale_pct": round((scale - 1) * 100, 0),
                "reason": f"{factor}: {sep:+.1f}pts separation across {n} alerts citing it as a pro -- "
                          f"{'well validated, worth carrying more weight' if scale > 1 else 'not earning its current weight, worth reducing'}. "
                          f"Suggest {direction} {key} by {abs(round((scale-1)*100)):.0f}%.",
            })
    return out



def _parse_why_caution(message: str) -> Dict[str, List[str]]:
    if not message:
        return {"pros": [], "cons": []}
    pros, cons = [], []
    for line in message.split("\n"):
        if line.startswith("Why: "):
            pros = [p.strip() for p in line[len("Why: "):].split("; ") if p.strip()]
        elif line.startswith("Caution: "):
            cons = [c.strip() for c in line[len("Caution: "):].split("; ") if c.strip()]
    return {"pros": pros, "cons": cons}


def _factor_calibration(results: List[Dict[str, Any]], min_sample: int = 10) -> List[Dict[str, Any]]:
    out = []
    for factor_name, keywords in FACTOR_KEYWORDS.items():
        present_as_pro, present_as_con, absent = [], [], []
        for r in results:
            parsed = _parse_why_caution(r.get("message") or r.get("rationale") or "")
            pro_hit = any(any(kw in p.lower() for kw in keywords) for p in parsed["pros"])
            con_hit = any(any(kw in c.lower() for kw in keywords) for c in parsed["cons"])
            if pro_hit:
                present_as_pro.append(r)
            elif con_hit:
                present_as_con.append(r)
            else:
                absent.append(r)

        def _win_rate(rows):
            if not rows:
                return None
            wins = sum(1 for r in rows if (r.get("bt_pnl") or 0) > 0)
            return round(100 * wins / len(rows), 1)

        pro_wr = _win_rate(present_as_pro)
        con_wr = _win_rate(present_as_con)
        absent_wr = _win_rate(absent)
        if pro_wr is None or absent_wr is None or len(present_as_pro) < min_sample:
            out.append({
                "factor": factor_name, "as_pro_count": len(present_as_pro),
                "as_pro_win_rate": pro_wr, "as_con_count": len(present_as_con),
                "as_con_win_rate": con_wr, "absent_count": len(absent), "absent_win_rate": absent_wr,
                "separation_pts": None,
                "note": f"Only {len(present_as_pro)} alerts cited this as a pro -- needs {min_sample}+ "
                        f"to say anything reliable.",
            })
            continue
        separation = round(pro_wr - absent_wr, 1)
        out.append({
            "factor": factor_name, "as_pro_count": len(present_as_pro), "as_pro_win_rate": pro_wr,
            "as_con_count": len(present_as_con), "as_con_win_rate": con_wr,
            "absent_count": len(absent), "absent_win_rate": absent_wr,
            "separation_pts": separation,
            "note": (f"Validated: alerts citing this as a pro won {separation:+.1f}pts more than alerts "
                     f"where it wasn't mentioned -- current weight looks earned." if separation >= 8 else
                     f"Weak: only {separation:+.1f}pts separation between citing this as a pro vs not "
                     f"mentioning it -- may be over-weighted relative to what it actually predicts." if separation < 3 else
                     f"Moderate: {separation:+.1f}pts separation -- roughly earning its current weight."),
        })
    out.sort(key=lambda x: (x["separation_pts"] is None, -(x["separation_pts"] or 0)))
    return out


MIN_SAMPLE_FOR_SUGGESTION = 15
LARGE_GAP_PTS = 10


def _calibration_suggestions(by_grade: Dict[str, Dict], by_type: Dict[str, Dict]) -> List[Dict[str, Any]]:
    suggestions = []

    for grade, stats in sorted(by_grade.items()):
        n = stats.get("count", 0)
        gap = stats.get("calibration_gap_pts")
        if gap is None:
            continue
        if n < MIN_SAMPLE_FOR_SUGGESTION:
            suggestions.append({
                "bucket": f"Grade {grade}", "count": n, "gap_pts": gap,
                "severity": "low_sample",
                "text": f"Grade {grade}: only {n} backtested trades -- gap of {gap:+.1f}pts isn't "
                        f"reliable yet at this sample size, needs {MIN_SAMPLE_FOR_SUGGESTION - n} more "
                        f"before acting on it.",
            })
            continue
        if abs(gap) >= LARGE_GAP_PTS:
            if gap > 0:
                text = (f"Grade {grade}: realized win rate beats predicted POP by {gap:+.1f}pts across "
                        f"{n} trades -- this grade is underconfident. Consider whether the factors that "
                        f"commonly appear on {grade}-grade trades deserve more weight, or whether the "
                        f"grade threshold band is set too conservatively.")
            else:
                text = (f"Grade {grade}: realized win rate falls short of predicted POP by {gap:+.1f}pts "
                        f"across {n} trades -- this grade is overconfident. Worth checking which factors "
                        f"are inflating the score here relative to what actually happened.")
            suggestions.append({"bucket": f"Grade {grade}", "count": n, "gap_pts": gap,
                                 "severity": "high", "text": text})

    for tt, stats in sorted(by_type.items()):
        n = stats.get("count", 0)
        gap = stats.get("calibration_gap_pts")
        if gap is None:
            continue
        if n < MIN_SAMPLE_FOR_SUGGESTION:
            suggestions.append({
                "bucket": f"Type {tt}", "count": n, "gap_pts": gap,
                "severity": "low_sample",
                "text": f"Type {tt}: only {n} backtested trades -- gap of {gap:+.1f}pts isn't reliable "
                        f"yet, needs {MIN_SAMPLE_FOR_SUGGESTION - n} more before acting on it.",
            })
            continue
        if abs(gap) >= LARGE_GAP_PTS:
            direction = "underconfident" if gap > 0 else "overconfident"
            suggestions.append({
                "bucket": f"Type {tt}", "count": n, "gap_pts": gap, "severity": "high",
                "text": f"Trade type {tt}: {direction} by {gap:+.1f}pts across {n} trades -- worth a "
                        f"type-specific look at whether {tt}'s scoring factors need adjustment "
                        f"independent of the other trade types.",
            })

    suggestions.sort(key=lambda s: (0 if s["severity"] == "high" else 1, -abs(s["gap_pts"])))
    return suggestions
