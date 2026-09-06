"""
outcome_tracker.py — Trade Outcome Tracker

Adds columns to `trades` table:
  conviction_score  REAL    — score at entry (0-12)
  conviction_label  TEXT    — label at entry
  signals_at_entry  TEXT    — JSON list of active signals
  regime_at_entry   TEXT    — market regime when trade entered
  regime_at_exit    TEXT    — market regime when trade closed
  r_multiple        REAL    — actual PnL / initial risk amount
  setup_grade       TEXT    — A/B/C/D trader's own grading of setup quality
  followed_rules    INTEGER — 1=yes, 0=no
  expected_move     REAL    — expected % move at entry
  actual_move       REAL    — actual % move by close
  hindsight_notes   TEXT    — what you missed / would do differently
  iv_rank_entry     REAL    — IV rank at entry (future use)

Routes:
  POST /outcome/migrate                — add new columns (safe, idempotent)
  GET  /outcome/summary                — aggregate analysis across all closed trades
  POST /outcome/grade/<trade_id>       — save setup_grade, followed_rules, hindsight_notes
"""
import sqlite3, json
from pathlib import Path
from datetime import datetime
from flask import Blueprint, jsonify, request

outcome_bp = Blueprint("outcome_bp", __name__, url_prefix="/outcome")
from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH


def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c


def _migrate():
    """Add outcome columns to trades table if not present."""
    new_cols = [
        ("conviction_score",  "REAL"),
        ("conviction_label",  "TEXT"),
        ("signals_at_entry",  "TEXT"),
        ("regime_at_entry",   "TEXT"),
        ("regime_at_exit",    "TEXT"),
        ("r_multiple",        "REAL"),
        ("setup_grade",       "TEXT"),
        ("followed_rules",    "INTEGER"),
        ("expected_move",     "REAL"),
        ("actual_move",       "REAL"),
        ("hindsight_notes",   "TEXT"),
    ]
    con = _conn()
    existing = {r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()}
    added = []
    for col, typ in new_cols:
        if col not in existing:
            con.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
            added.append(col)
    con.commit(); con.close()
    return added


def _r_multiple(pnl, risk_amt):
    """Compute R-multiple: pnl / risk_amt. Returns None if risk_amt is 0."""
    if not risk_amt or risk_amt == 0:
        return None
    return round(float(pnl) / abs(float(risk_amt)), 2)


@outcome_bp.route("/migrate", methods=["POST"])
def migrate():
    added = _migrate()
    return jsonify({"ok": True, "added_columns": added})


@outcome_bp.route("/grade/<int:trade_id>", methods=["POST"])
def save_grade(trade_id):
    """Save post-close analysis fields for a trade."""
    _migrate()
    d = request.get_json(force=True) or {}
    fields = {}
    if "setup_grade"      in d: fields["setup_grade"]      = str(d["setup_grade"])[:2]
    if "followed_rules"   in d: fields["followed_rules"]   = int(bool(d["followed_rules"]))
    if "hindsight_notes"  in d: fields["hindsight_notes"]  = str(d["hindsight_notes"])[:500]
    if "expected_move"    in d: fields["expected_move"]    = float(d["expected_move"])
    if "actual_move"      in d: fields["actual_move"]      = float(d["actual_move"])
    if not fields:
        return jsonify({"error": "no fields provided"}), 400
    con = _conn()
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE trades SET {sets} WHERE id=?", list(fields.values()) + [trade_id])
    con.commit(); con.close()
    return jsonify({"ok": True})


@outcome_bp.route("/summary")
def summary():
    """
    Aggregate outcome analysis across all closed trades.
    Returns conviction-stratified performance, R-multiple distribution,
    setup grade analysis, followed-rules win rates.
    """
    _migrate()
    con = _conn()
    trades = con.execute("""
        SELECT t.*, ts.total_score, ts.signals_json
        FROM trades t
        LEFT JOIN trade_signals ts ON ts.trade_id = t.id
        WHERE t.status='CLOSED' AND t.pnl IS NOT NULL
        ORDER BY t.exit_date DESC
    """).fetchall()
    con.close()

    if not trades:
        return jsonify({"message": "No closed trades yet."})

    # R-multiple distribution
    r_multiples = []
    by_conviction = {"strong(8+)": [], "good(5-7)": [], "moderate(3-4)": [], "weak(<3)": []}
    by_grade = {}
    by_rules = {"followed": [], "broke": []}
    by_signals = {}

    for t in trades:
        pnl  = t["pnl"] or 0
        risk = t["risk_amt"] or 0
        r    = _r_multiple(pnl, risk)
        win  = 1 if pnl > 0 else 0

        if r is not None: r_multiples.append(r)

        # Conviction bucket
        cs = t["total_score"] or t["conviction_score"]
        if cs is not None:
            if   cs >= 8: by_conviction["strong(8+)"].append(win)
            elif cs >= 5: by_conviction["good(5-7)"].append(win)
            elif cs >= 3: by_conviction["moderate(3-4)"].append(win)
            else:         by_conviction["weak(<3)"].append(win)

        # Setup grade
        grade = t["setup_grade"]
        if grade:
            by_grade.setdefault(grade, []).append(win)

        # Followed rules
        fr = t["followed_rules"]
        if fr is not None:
            if fr == 1: by_rules["followed"].append(win)
            else:       by_rules["broke"].append(win)

        # Per-signal performance
        sigs_raw = t["signals_json"]
        if sigs_raw:
            try:
                for sig in json.loads(sigs_raw):
                    sig_key = sig[:40]
                    by_signals.setdefault(sig_key, []).append(win)
            except: pass

    def _stats(wins_list):
        if not wins_list: return {"count": 0}
        wr = round(sum(wins_list) / len(wins_list) * 100, 1)
        return {"count": len(wins_list), "wins": sum(wins_list), "win_rate": wr}

    r_arr = sorted(r_multiples)
    r_stats = {}
    if r_arr:
        import statistics as _st
        r_stats = {
            "avg":    round(_st.mean(r_arr), 2),
            "median": round(_st.median(r_arr), 2),
            "min":    round(r_arr[0], 2),
            "max":    round(r_arr[-1], 2),
            "above_1R": sum(1 for r in r_arr if r >= 1),
            "below_neg1R": sum(1 for r in r_arr if r <= -1),
            "total": len(r_arr),
        }

    return jsonify({
        "by_conviction": {k: _stats(v) for k, v in by_conviction.items() if v},
        "by_grade":      {k: _stats(v) for k, v in by_grade.items()},
        "by_rules":      {k: _stats(v) for k, v in by_rules.items() if v},
        "by_signal":     {k: _stats(v) for k, v in sorted(by_signals.items(),
                          key=lambda x: len(x[1]), reverse=True)[:10]},
        "r_multiple":    r_stats,
    })
