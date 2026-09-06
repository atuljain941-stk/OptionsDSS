# oiapp/scanners/gex_predictive_analysis.py
"""
GEX Predictive Analysis -- read-only page.

Same analysis as the original standalone gex_predictive_analysis.py
script (kept in the repo root for anyone who wants to run it from the
CLI with pandas open in a notebook alongside it), refactored to return
structured dicts instead of printed text, so this can run as a proper
in-app page. No writes anywhere in this module -- every DB access here
is a plain SELECT; the only "side effect" is the schema/logging
addition already made to gex_trend_tracker.py's _log() (unrelated to
this file, that's what populates the columns this reads).

See the original script's own module docstring (still in the repo) for
the full reasoning on why gex_trend_log.spot is used as the price
series (self-joined, no external price table) and why the wall
columns will show heavy NULL rates on any pre-fix historical data --
that reasoning is unchanged, just re-stated more briefly in the
docstrings below.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, render_template, request

from ..config import DB_PATH as _OIAPP_DB_PATH

try:
    from scipy import stats as sps
    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False

gex_predictive_bp = Blueprint("gex_predictive_analysis", __name__, url_prefix="/gex-predictive-analysis")

FORWARD_HORIZONS_MIN = [5, 15, 30, 60]
MATCH_TOLERANCE_MIN = 2.5
DEFAULT_MIN_N = 30


# ── Data loading ────────────────────────────────────────────────────────

def _load_gex_log(db_path: str, symbols: List[str]) -> pd.DataFrame:
    con = sqlite3.connect(db_path)
    placeholders = ",".join("?" * len(symbols))
    df = pd.read_sql_query(
        f"SELECT * FROM gex_trend_log WHERE symbol IN ({placeholders}) ORDER BY symbol, ts",
        con, params=symbols,
    )
    con.close()
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"])
    return df


def _schema_coverage(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty:
        return {"has_data": False, "total_rows": 0}
    total = len(df)
    days_covered = df["ts"].dt.date.nunique()
    per_symbol = [{"symbol": sym, "rows": int(cnt), "days": int(df[df.symbol == sym]["ts"].dt.date.nunique())}
                  for sym, cnt in df.groupby("symbol").size().items()]
    null_rates = []
    for col in ["spot", "gamma_flip", "pin_strike", "regime", "net_gex",
                "call_wall", "put_wall", "breakout", "breakdown", "max_pain",
                "call_wall_gamma", "put_wall_gamma"]:
        if col in df.columns:
            pct = round(float(df[col].isna().mean() * 100), 1)
            is_wall_col = col in ("call_wall", "put_wall", "call_wall_gamma", "put_wall_gamma")
            null_rates.append({
                "column": col, "null_pct": pct,
                "note": "Wall data added mid-session -- expect high NULL on older rows" if (is_wall_col and pct > 50) else None,
            })
    return {
        "has_data": True, "total_rows": total,
        "date_min": str(df["ts"].min()), "date_max": str(df["ts"].max()),
        "days_covered": int(days_covered), "per_symbol": per_symbol, "null_rates": null_rates,
        "thin_warning": days_covered < 5,
    }


# ── Feature engineering (identical logic to the standalone script) ─────

def _regime_to_sign(regime_val) -> Optional[str]:
    if not isinstance(regime_val, str):
        return None
    v = regime_val.lower()
    if "short" in v or "negative" in v or "neg_g" in v or "neg-g" in v:
        return "short"
    if "long" in v or "positive" in v or "pos_g" in v or "pos-g" in v:
        return "long"
    return None


def _engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)
    df["flip_dist_dollars"] = df["spot"] - df["gamma_flip"]
    df["flip_dist_pct"] = df["flip_dist_dollars"] / df["spot"] * 100

    df["ret_5min_pct"] = df.groupby("symbol")["spot"].pct_change() * 100
    df["rolling_vol_proxy"] = (
        df.groupby("symbol")["ret_5min_pct"].transform(lambda s: s.rolling(12, min_periods=4).std())
    )
    df["flip_dist_vol_relative"] = df["flip_dist_pct"] / df["rolling_vol_proxy"]

    df["call_wall_dist_pct"] = (df["call_wall"] - df["spot"]) / df["spot"] * 100
    df["put_wall_dist_pct"] = (df["spot"] - df["put_wall"]) / df["spot"] * 100
    df["nearest_wall_dist_pct"] = df[["call_wall_dist_pct", "put_wall_dist_pct"]].min(axis=1)

    for col, out in [("call_wall_gamma", "call_wall_gamma_chg_3"), ("put_wall_gamma", "put_wall_gamma_chg_3")]:
        df[out] = df.groupby("symbol")[col].transform(lambda s: s - s.shift(3))

    df["regime_sign"] = df["regime"].apply(_regime_to_sign)

    tmin = df["ts"].dt.hour * 60 + df["ts"].dt.minute

    def _bucket(t):
        if 9 * 60 + 30 <= t < 10 * 60 + 30:
            return "OPEN_930_1030"
        if t >= 15 * 60:
            return "POWER_HOUR_300_400"
        return "MIDDAY"
    df["time_bucket"] = tmin.apply(_bucket)
    return df


def _add_forward_returns(df: pd.DataFrame) -> pd.DataFrame:
    out_frames = []
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("ts").reset_index(drop=True)
        for horizon in FORWARD_HORIZONS_MIN:
            target_ts = g["ts"] + pd.Timedelta(minutes=horizon)
            idx = pd.merge_asof(
                pd.DataFrame({"target_ts": target_ts}).sort_values("target_ts"),
                g[["ts", "spot"]].rename(columns={"spot": "future_spot"}).sort_values("ts"),
                left_on="target_ts", right_on="ts", direction="nearest",
                tolerance=pd.Timedelta(minutes=MATCH_TOLERANCE_MIN),
            )
            g[f"fwd_ret_{horizon}min_pct"] = ((idx["future_spot"] - g["spot"]) / g["spot"] * 100).values
        out_frames.append(g)
    result = pd.concat(out_frames, ignore_index=True)
    for horizon in FORWARD_HORIZONS_MIN:
        result[f"fwd_abs_move_{horizon}min_pct"] = result[f"fwd_ret_{horizon}min_pct"].abs()
    return result


# ── Analysis (returns structured dicts, no printing) ────────────────────

def _n_flag(n: int, min_n: int) -> Optional[str]:
    return None if n >= min_n else f"n={n} below {min_n} -- directional only, not conclusive"


def _analyze_regime_vs_move_size(df: pd.DataFrame, min_n: int) -> Dict[str, Any]:
    d = df.dropna(subset=["regime_sign"])
    if d.empty:
        return {"available": False, "reason": "No rows with a parseable regime label."}
    rows = []
    for horizon in [15, 30]:
        col = f"fwd_abs_move_{horizon}min_pct"
        for bucket in ["OPEN_930_1030", "MIDDAY", "POWER_HOUR_300_400"]:
            sub = d[d.time_bucket == bucket]
            short_moves = sub[sub.regime_sign == "short"][col].dropna()
            long_moves = sub[sub.regime_sign == "long"][col].dropna()
            n_short, n_long = len(short_moves), len(long_moves)
            entry = {"horizon_min": horizon, "time_bucket": bucket, "n_short": int(n_short), "n_long": int(n_long)}
            if n_short == 0 or n_long == 0:
                entry["comparable"] = False
            else:
                entry["comparable"] = True
                entry["median_short_pct"] = round(float(short_moves.median()), 3)
                entry["median_long_pct"] = round(float(long_moves.median()), 3)
                if HAVE_SCIPY and n_short >= 3 and n_long >= 3:
                    try:
                        _, p = sps.mannwhitneyu(short_moves, long_moves, alternative="two-sided")
                        entry["mannwhitney_p"] = round(float(p), 3)
                    except Exception:
                        pass
                entry["flag"] = _n_flag(min(n_short, n_long), min_n)
            rows.append(entry)
    return {"available": True, "rows": rows}


def _analyze_flip_distance(df: pd.DataFrame, min_n: int) -> Dict[str, Any]:
    rows = []
    for horizon in [15, 30]:
        col = f"fwd_abs_move_{horizon}min_pct"
        d = df.dropna(subset=["flip_dist_pct", col])
        n = len(d)
        entry = {"horizon_min": horizon, "n": n}
        if n < 5:
            entry["available"] = False
        else:
            entry["available"] = True
            if HAVE_SCIPY:
                rho, p = sps.spearmanr(d["flip_dist_pct"].abs(), d[col])
                entry["method"] = "spearman"
                entry["correlation"] = round(float(rho), 3)
                entry["p_value"] = round(float(p), 3)
            else:
                corr = d["flip_dist_pct"].abs().corr(d[col])
                entry["method"] = "pearson (scipy unavailable)"
                entry["correlation"] = round(float(corr), 3)
            entry["flag"] = _n_flag(n, min_n)
        rows.append(entry)
    return {"rows": rows}


def _analyze_wall_proximity_and_decay(df: pd.DataFrame, min_n: int) -> Dict[str, Any]:
    has_wall_data = df["call_wall"].notna().any() or df["put_wall"].notna().any()
    if not has_wall_data:
        return {
            "blocked": True,
            "reason": ("Zero rows have wall data. Expected if all current gex_trend_log rows predate the "
                       "schema fix -- call_wall/put_wall/call_wall_gamma/put_wall_gamma were only added this "
                       "session and are computed live but were never persisted before. Nothing to analyze "
                       "until fresh snapshots accumulate under the new schema."),
        }

    d = df.dropna(subset=["nearest_wall_dist_pct"])
    n = len(d)
    proximity_rows = []
    if n >= 5:
        for horizon in [15, 30]:
            col = f"fwd_abs_move_{horizon}min_pct"
            dd = d.dropna(subset=[col])
            near = dd[dd["nearest_wall_dist_pct"].abs() <= 0.3]
            far = dd[dd["nearest_wall_dist_pct"].abs() > 0.3]
            if len(near) >= 3 and len(far) >= 3:
                proximity_rows.append({
                    "horizon_min": horizon,
                    "n_near": len(near), "n_far": len(far),
                    "median_move_near_pct": round(float(near[col].median()), 3),
                    "median_move_far_pct": round(float(far[col].median()), 3),
                    "flag": _n_flag(min(len(near), len(far)), min_n),
                })

    decay_present = df["call_wall_gamma_chg_3"].notna().any() or df["put_wall_gamma_chg_3"].notna().any()
    return {
        "blocked": False,
        "n_with_wall_distance": n,
        "flag": _n_flag(n, min_n),
        "proximity_rows": proximity_rows,
        "decay_data_present": bool(decay_present),
        "decay_note": ("Wall gamma rate-of-change data is present, but not enough distinct wall-break events "
                       "yet to report a specific decay-vs-failure number -- needs more days, not more code."
                       if decay_present else
                       "No usable decay data yet -- needs at least 3 consecutive post-fix snapshots per symbol."),
    }


# ── Main entry point ─────────────────────────────────────────────────────

def run_analysis(db_path: Optional[str] = None, symbols: Optional[List[str]] = None,
                  min_n: int = DEFAULT_MIN_N) -> Dict[str, Any]:
    db_path = db_path or _OIAPP_DB_PATH
    symbols = symbols or ["SPY", "QQQ", "IWM"]

    df = _load_gex_log(db_path, symbols)
    schema = _schema_coverage(df)
    if not schema.get("has_data"):
        return {"ok": True, "schema": schema, "regime_vs_move": None, "flip_distance": None,
                "wall_analysis": None, "bottom_line": "No rows found in gex_trend_log for these symbols."}

    df = _engineer_features(df)
    df = _add_forward_returns(df)

    regime_vs_move = _analyze_regime_vs_move_size(df, min_n)
    flip_distance = _analyze_flip_distance(df, min_n)
    wall_analysis = _analyze_wall_proximity_and_decay(df, min_n)

    days = schema["days_covered"]
    if days < 5:
        bottom_line = (
            f"{days} trading day(s) covered -- too thin to draw firm conclusions on any question. "
            f"Regime-vs-move-size has the most usable history so far (predates the wall-data schema fix); "
            f"wall-proximity and wall-decay are effectively un-testable on current data regardless of day "
            f"count, since wall data only started being logged this session."
        )
    else:
        bottom_line = (
            f"{days} trading days covered -- enough to start trusting directional patterns. Still worth "
            f"another pass once both a trending and a range-bound session are represented."
        )

    return {
        "ok": True, "schema": schema, "regime_vs_move": regime_vs_move,
        "flip_distance": flip_distance, "wall_analysis": wall_analysis,
        "bottom_line": bottom_line, "min_n_used": min_n, "symbols": symbols,
    }


# ── Routes ──────────────────────────────────────────────────────────────

@gex_predictive_bp.route("/")
def page():
    return render_template("gex_predictive_analysis.html")


@gex_predictive_bp.route("/api/run")
def api_run():
    symbols_param = request.args.get("symbols")
    symbols = [s.strip().upper() for s in symbols_param.split(",")] if symbols_param else None
    min_n = int(request.args.get("min_n", DEFAULT_MIN_N))
    try:
        return jsonify(run_analysis(symbols=symbols, min_n=min_n))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
