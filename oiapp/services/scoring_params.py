# oiapp/services/scoring_params.py
"""
Scoring Parameters
────────────────────
Every tunable constant in trade_opportunity_scanner._entry_score(),
extracted into one structured, versioned, DB-backed config instead of
hardcoded literals scattered through the function. Built specifically so
the calibration suggestions the Backtest page already surfaces (e.g.
"Grade C underconfident by +18.2pts") can actually be acted on from a
screen instead of requiring a code edit each time.

DEFAULT_PARAMS below matches the exact values _entry_score() had
hardcoded before this existed -- verified line by line against that
function -- so installing this changes nothing until someone actually
edits a value and saves.

Versioning: every save INSERTS a new row rather than updating in place,
so the full history is just "every row ever inserted, ordered by
created_at" -- nothing to separately maintain, nothing that can silently
lose history. get_params() always returns the latest row's values.

IMPORTANT HONEST LIMITATION, not fixed by this module: this makes
FUTURE scoring configurable and lets you compare old-vs-new weights
against a LIVE re-scan of today's watchlist. It does NOT let you replay
PAST alerts under new weights -- the raw factor inputs (regime, RS, PCR,
walls, rsi_diff, dte, max_pain) that fed each historical score were
never stored, only the final score/grade and free text. True historical
backtesting of a parameter change needs those inputs captured going
forward from here, not something this module can retroactively produce.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import DB_PATH

# ── Defaults -- exact match of what was previously hardcoded ──────────────

DEFAULT_PARAMS: Dict[str, Any] = {
    "base_score": 50,
    "score_floor": 5,
    "score_ceiling": 97,

    "grade_thresholds": {"A": 80, "B": 65, "C": 50, "D": 35},  # score >= threshold

    "regime_points": {
        "TRENDING_UP": 18, "TRENDING_UP_OB": 12, "MILD_BULLISH": 9, "MEAN_REV_BULL": 10,
        "TRENDING_DOWN": -18, "TRENDING_DOWN_OS": -12, "MILD_BEARISH": -9, "MEAN_REV_BEAR": -10,
        "SIDEWAYS": 0,
    },
    "regime_flat_fallback_bull": 11,   # used only when regime_detail wasn't passed
    "regime_flat_fallback_bear": -9,
    "rsi_trend_against_penalty": 8,    # RSI trending against the trade's direction

    "confluence_agree": 6,
    "confluence_weekly_shock_against": -20,
    "confluence_disagree": -15,
    "confluence_weekly_sideways_directional": -10,
    "confluence_weekly_sideways_ic": 8,
    "confluence_ic_directional_regime": 5,

    "rs_bull_strong": 15, "rs_bull_strong_threshold": 5,
    "rs_bull_mild": 8, "rs_bull_mild_threshold": 2,
    "rs_bull_weak_neg": -6, "rs_bull_weak_neg_threshold": -2,
    "rs_bull_strong_neg": -12, "rs_bull_strong_neg_threshold": -5,
    "rs_ic_neutral": 8, "rs_ic_neutral_threshold": 3,
    "rs_ic_trending_penalty": -5, "rs_ic_trending_threshold": 8,
    "rs_momentum_conflict_zero_threshold": 10,   # |rsi_diff| beyond this -> RS fully nullified
    "rs_momentum_conflict_damp_factor": 0.4,      # otherwise damped to this fraction
    "rs_regime_redundancy_damp_factor": 0.5,
    "rs_regime_redundancy_min_regime_pts": 15,

    "rsi_diff_strong_threshold": 10,
    "rsi_diff_strong_bonus": 6,
    "rsi_diff_mild_bonus": 3,
    "rsi_diff_strong_penalty": 10,
    "rsi_diff_mild_penalty": 5,

    "iv_rank_credit_high": 12, "iv_rank_credit_high_threshold": 65,
    "iv_rank_credit_good": 7, "iv_rank_credit_good_threshold": 45,
    "iv_rank_credit_low": -10, "iv_rank_credit_low_threshold": 20,
    "iv_rank_credit_thin": -5, "iv_rank_credit_thin_threshold": 30,
    "iv_rank_debit_cheap": 12, "iv_rank_debit_cheap_threshold": 25,
    "iv_rank_debit_reasonable": 7, "iv_rank_debit_reasonable_threshold": 35,
    "iv_rank_debit_expensive": -10, "iv_rank_debit_expensive_threshold": 65,

    "pcr_strong": 12, "pcr_strong_threshold": 1.3,
    "pcr_mild": 6, "pcr_mild_threshold": 1.1,
    "pcr_strong_against": -12, "pcr_strong_against_threshold": 0.7,
    "pcr_mild_against": -6, "pcr_mild_against_threshold": 0.9,
    "pcr_ic_balanced": 8, "pcr_ic_low": 0.8, "pcr_ic_high": 1.2,

    "max_pain_bonus": 3, "max_pain_penalty": -3,
    "max_pain_favor_threshold_pct": 1.0, "max_pain_against_threshold_pct": 3.0,

    "wall_strong": 14, "wall_strong_threshold_pct": 2.0,
    "wall_mild": 8, "wall_mild_threshold_pct": 5.0,
    "wall_far_penalty": -5,
    "wall_ic_pinched": 12, "wall_ic_pinched_threshold_pct": 5.0,
    "wall_ic_risk_penalty": -6, "wall_ic_risk_threshold_pct": 3.0,

    "gamma_flip_base_points": 8,
    "gamma_flip_near_penalty": 5,
    "gamma_flip_near_threshold_pct": 1.0,
    "gamma_flip_dte_scale_full": 3,       # <= this many DTE -> scale 1.0
    "gamma_flip_dte_scale_half": 10,      # <= this many DTE -> scale 0.5
    "gamma_flip_dte_scale_quarter": 21,   # <= this many DTE -> scale 0.25, else 0.1

    "width_capped_penalty": 8,

    "pop_rr_strong_edge_pts": 15.0, "pop_rr_strong_edge_bonus": 8,
    "pop_rr_mild_edge_pts": 5.0, "pop_rr_mild_edge_bonus": 4,
    "pop_rr_edge_threshold_pts": -5.0, "pop_rr_penalty": -12,
    "pop_rr_thin_margin_penalty": -6,
}


# ── Schema ──────────────────────────────────────────────────────────────

def _ensure_table():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS scoring_params_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        params_json TEXT NOT NULL,
        note TEXT,
        changed_by TEXT,
        created_at TEXT NOT NULL
    )""")
    con.commit()
    con.close()


_cached_params: Optional[Dict[str, Any]] = None
_cache_loaded_at: float = 0.0
_CACHE_TTL_SECONDS = 30.0  # short TTL -- this is read on every scored trade,
                            # but should pick up a just-saved change quickly


def get_params(force_reload: bool = False) -> Dict[str, Any]:
    """Current active parameter set -- latest saved row, or DEFAULT_PARAMS
    if nothing has ever been saved. Cached briefly since _entry_score()
    calls this on every trade scored, not just when the page loads."""
    global _cached_params, _cache_loaded_at
    import time
    now = time.time()
    if not force_reload and _cached_params is not None and (now - _cache_loaded_at) < _CACHE_TTL_SECONDS:
        return _cached_params
    _ensure_table()
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT params_json FROM scoring_params_history ORDER BY id DESC LIMIT 1"
    ).fetchone()
    con.close()
    if row and row[0]:
        try:
            saved = json.loads(row[0])
            # Merge over defaults so a param added to DEFAULT_PARAMS after
            # someone's last save doesn't silently disappear -- old saved
            # rows won't have newer keys, defaults fill the gap.
            merged = {**DEFAULT_PARAMS, **saved}
            _cached_params, _cache_loaded_at = merged, now
            return merged
        except Exception:
            pass
    _cached_params, _cache_loaded_at = dict(DEFAULT_PARAMS), now
    return _cached_params


def save_params(new_params: Dict[str, Any], note: str = "", changed_by: str = "") -> Dict[str, Any]:
    """Inserts a new version -- never updates in place, so history is
    automatic. Validates keys against DEFAULT_PARAMS so a typo'd/unknown
    key doesn't silently do nothing inside _entry_score()."""
    _ensure_table()
    unknown = set(new_params.keys()) - set(DEFAULT_PARAMS.keys())
    if unknown:
        raise ValueError(f"Unknown parameter key(s), not saved: {sorted(unknown)}")

    current = get_params(force_reload=True)
    merged = {**current, **new_params}

    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO scoring_params_history (params_json, note, changed_by, created_at) VALUES (?,?,?,?)",
        (json.dumps(merged), note, changed_by, datetime.now().isoformat()),
    )
    con.commit()
    con.close()

    global _cached_params
    _cached_params = None  # force reload next get_params() call
    return merged


def get_history(limit: int = 50) -> List[Dict[str, Any]]:
    """Every saved version, newest first, WITH a diff against the
    immediately-prior version so the history table can show exactly what
    changed at each save, not just a wall of full JSON blobs."""
    _ensure_table()
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, params_json, note, changed_by, created_at FROM scoring_params_history "
        "ORDER BY id DESC LIMIT ?", (limit + 1,)  # +1 so the oldest row in the page can diff too
    ).fetchall()
    con.close()

    out = []
    for i, row in enumerate(rows[:limit]):
        _id, params_json, note, changed_by, created_at = row
        try:
            params = json.loads(params_json)
        except Exception:
            params = {}
        prev_params = {}
        if i + 1 < len(rows):
            try:
                prev_params = json.loads(rows[i + 1][1])
            except Exception:
                prev_params = {}
        elif i + 1 == len(rows) and len(rows) > limit:
            pass  # already have prev from the +1 fetch above via rows[i+1]
        else:
            prev_params = DEFAULT_PARAMS

        changes = []
        baseline = prev_params or DEFAULT_PARAMS
        for k, v in params.items():
            if k not in baseline or baseline[k] != v:
                changes.append({"key": k, "from": baseline.get(k), "to": v})

        out.append({
            "id": _id, "note": note, "changed_by": changed_by, "created_at": created_at,
            "changes": changes,
        })
    return out


def reset_to_defaults(note: str = "Reset to defaults") -> Dict[str, Any]:
    return save_params(dict(DEFAULT_PARAMS), note=note)
