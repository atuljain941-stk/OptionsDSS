"""
gex_predictive_analysis.py
────────────────────────────
Tests whether GEX Trend Tracker's 5-min snapshots (oiapp's gex_trend_log
table) have real predictive power for short-horizon forward price moves,
before building any LLM trade-signal layer on top of them.

WHAT THIS SCRIPT ASSUMES ABOUT YOUR DATA (verified against the actual
oiapp source, not guessed):

  gex_trend_log columns actually available:
    symbol, ts, spot, expiry, dte, net_gex, gamma_flip, pin_strike,
    regime, regime_strength, baseline_captured_at,
    call_wall, put_wall, breakout, breakdown, max_pain,
    call_wall_gamma, put_wall_gamma

  The last 7 columns (call_wall through put_wall_gamma) did NOT exist
  until this same session -- they were added specifically so future
  5-min collection would support the wall-proximity and wall-decay
  analysis you asked for. Any snapshot captured BEFORE that schema
  change will have NULLs in these columns; there is no way to
  retroactively recover wall strikes/gamma for old snapshots, since
  that data was computed live and discarded on every prior tick, never
  persisted. This script detects and reports that split explicitly
  (see the "SCHEMA COVERAGE" section of the printed report) rather than
  silently mixing pre-fix and post-fix rows together.

WHY THERE'S NO SEPARATE PRICE-HISTORY JOIN:
  oiapp has two other price tables (price_cache: daily only;
  intraday_price_cache: hourly bars from yfinance) -- neither is fine
  enough for 5/15/30/60-min forward returns. gex_trend_log's own `spot`
  column, captured on the same 5-min tick as everything else here, IS
  already a native 5-min price series perfectly time-aligned with the
  GEX features -- forward returns are computed by self-joining this
  table against itself at future offsets, not by pulling in an external
  price source with its own alignment/granularity mismatches.

USAGE:
    python gex_predictive_analysis.py --db-path "T:\\ajain33\\data\\options_data.db"
    python gex_predictive_analysis.py --db-path options_data.db --symbols SPY QQQ IWM --min-n 30

Outputs a text report to stdout (redirect to a file if you want it saved)
and, optionally, a CSV of the full engineered feature/forward-return
table for your own further digging (--export-csv).
"""

import argparse
import sqlite3
import sys
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

try:
    from scipy import stats as sps
    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False

warnings.filterwarnings("ignore", category=RuntimeWarning)

FORWARD_HORIZONS_MIN = [5, 15, 30, 60]
# How much slack to allow when matching a snapshot to "N minutes later" --
# captures are approximately every 5 min but not exactly on the second,
# so an exact-timestamp match would silently drop most rows.
MATCH_TOLERANCE_MIN = 2.5
MIN_N_FOR_CONCLUSION = 30  # rows below this get flagged as directional-only, not conclusive


# ── Data loading ────────────────────────────────────────────────────────

def load_gex_log(db_path: str, symbols: list) -> pd.DataFrame:
    con = sqlite3.connect(db_path)
    placeholders = ",".join("?" * len(symbols))
    df = pd.read_sql_query(
        f"SELECT * FROM gex_trend_log WHERE symbol IN ({placeholders}) ORDER BY symbol, ts",
        con, params=symbols,
    )
    con.close()
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def print_schema_coverage(df: pd.DataFrame):
    print("=" * 78)
    print("SCHEMA COVERAGE")
    print("=" * 78)
    if df.empty:
        print("No rows found in gex_trend_log for the requested symbols. Nothing to analyze.")
        return
    total = len(df)
    date_min, date_max = df["ts"].min(), df["ts"].max()
    days_covered = df["ts"].dt.date.nunique()
    print(f"Total rows: {total}")
    print(f"Date range: {date_min} -> {date_max}  ({days_covered} distinct trading day(s) covered)")
    print()
    print("Rows per symbol:")
    for sym, cnt in df.groupby("symbol").size().items():
        print(f"  {sym}: {cnt} rows, {df[df.symbol==sym]['ts'].dt.date.nunique()} day(s)")
    print()
    print("Column NULL rates (fraction of rows missing each field):")
    for col in ["spot", "gamma_flip", "pin_strike", "regime", "net_gex",
                "call_wall", "put_wall", "breakout", "breakdown", "max_pain",
                "call_wall_gamma", "put_wall_gamma"]:
        if col in df.columns:
            null_pct = df[col].isna().mean() * 100
            flag = "  <-- wall data added mid-session; expect high NULL rate on older rows" \
                if col in ("call_wall", "put_wall", "call_wall_gamma", "put_wall_gamma") and null_pct > 50 else ""
            print(f"  {col:20s} {null_pct:5.1f}% NULL{flag}")
    print()
    if days_covered < 5:
        print(f"*** {days_covered} trading day(s) is thin. Directional patterns below are worth noting,")
        print("*** but treat every conclusion in this report as provisional until you have")
        print("*** 10-15 days spanning both trending and range-bound sessions.")
    print()


# ── Feature engineering ─────────────────────────────────────────────────

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)

    df["flip_dist_dollars"] = df["spot"] - df["gamma_flip"]
    df["flip_dist_pct"] = df["flip_dist_dollars"] / df["spot"] * 100

    # Expected-move proxy: rolling realized volatility of spot's own 5-min
    # returns over the trailing 12 snapshots (~1hr), scaled to a rough
    # "typical 5-min move in $" for this symbol/session so far today.
    # This is a PROXY, not a real ATR/expected-move figure -- no intraday
    # high/low OHLC is stored per snapshot, only a single spot price, so a
    # true ATR can't be computed from this table. Said explicitly here and
    # in the printed report rather than silently presenting it as a real ATR.
    df["ret_5min_pct"] = df.groupby("symbol")["spot"].pct_change() * 100
    df["rolling_vol_proxy"] = (
        df.groupby("symbol")["ret_5min_pct"]
        .transform(lambda s: s.rolling(12, min_periods=4).std())
    )
    df["flip_dist_vol_relative"] = df["flip_dist_pct"] / df["rolling_vol_proxy"]

    # Wall distances -- NaN wherever the wall columns are NULL (pre-fix rows).
    df["call_wall_dist_pct"] = (df["call_wall"] - df["spot"]) / df["spot"] * 100
    df["put_wall_dist_pct"] = (df["spot"] - df["put_wall"]) / df["spot"] * 100
    df["nearest_wall_dist_pct"] = df[["call_wall_dist_pct", "put_wall_dist_pct"]].min(axis=1)

    # Wall gamma rate of change over the prior 1-3 snapshots -- building
    # (growing more negative/positive in magnitude) vs decaying.
    for col, out in [("call_wall_gamma", "call_wall_gamma_chg_3"), ("put_wall_gamma", "put_wall_gamma_chg_3")]:
        df[out] = df.groupby("symbol")[col].transform(lambda s: s - s.shift(3))

    df["regime_sign"] = df["regime"].apply(_regime_to_sign)

    hour = df["ts"].dt.hour
    minute = df["ts"].dt.minute
    tmin = hour * 60 + minute

    def _bucket(t):
        if 9 * 60 + 30 <= t < 10 * 60 + 30:
            return "OPEN_930_1030"
        if t >= 15 * 60:
            return "POWER_HOUR_300_400"
        return "MIDDAY"
    df["time_bucket"] = tmin.apply(_bucket)

    return df


def _regime_to_sign(regime_val):
    """regime is a free-text field from _compute_full_levels()'s
    regime_label -- matched case-insensitively against short/negative vs
    long/positive gamma phrasing rather than assuming one exact string,
    since the label wording has changed at least once already this
    session (peak-gamma-zone additions etc). Returns 'short', 'long', or
    None if it doesn't clearly match either."""
    if not isinstance(regime_val, str):
        return None
    v = regime_val.lower()
    if "short" in v or "negative" in v or "neg_g" in v or "neg-g" in v:
        return "short"
    if "long" in v or "positive" in v or "pos_g" in v or "pos-g" in v:
        return "long"
    return None


# ── Forward returns (self-join, not an external price table) ───────────

def add_forward_returns(df: pd.DataFrame) -> pd.DataFrame:
    out_frames = []
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("ts").reset_index(drop=True)
        for horizon in FORWARD_HORIZONS_MIN:
            target_ts = g["ts"] + pd.Timedelta(minutes=horizon)
            # asof-style nearest match within tolerance, not an exact
            # timestamp equality -- captures aren't exactly every 5.000 min
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


# ── Analysis ─────────────────────────────────────────────────────────────

def _n_flag(n):
    return "" if n >= MIN_N_FOR_CONCLUSION else f"  [n={n} -- BELOW {MIN_N_FOR_CONCLUSION}, directional only, not conclusive]"


def analyze_regime_vs_move_size(df: pd.DataFrame):
    print("=" * 78)
    print("Q1: Does short-gamma regime correlate with LARGER forward moves than long-gamma?")
    print("=" * 78)
    d = df.dropna(subset=["regime_sign"])
    if d.empty:
        print("No rows with a parseable regime label. Cannot test this.")
        print()
        return
    for horizon in [15, 30]:
        col = f"fwd_abs_move_{horizon}min_pct"
        print(f"\n-- {horizon}-min forward |move|, by regime and time bucket --")
        for bucket in ["OPEN_930_1030", "MIDDAY", "POWER_HOUR_300_400"]:
            sub = d[d.time_bucket == bucket]
            short_moves = sub[sub.regime_sign == "short"][col].dropna()
            long_moves = sub[sub.regime_sign == "long"][col].dropna()
            n_short, n_long = len(short_moves), len(long_moves)
            if n_short == 0 or n_long == 0:
                print(f"  {bucket:22s} short n={n_short}, long n={n_long} -- not enough of both to compare")
                continue
            med_short, med_long = short_moves.median(), long_moves.median()
            line = (f"  {bucket:22s} short-gamma median={med_short:.3f}% (n={n_short})  "
                    f"vs long-gamma median={med_long:.3f}% (n={n_long})")
            if HAVE_SCIPY and n_short >= 3 and n_long >= 3:
                try:
                    stat, p = sps.mannwhitneyu(short_moves, long_moves, alternative="two-sided")
                    line += f"  Mann-Whitney p={p:.3f}"
                except Exception:
                    pass
            line += _n_flag(min(n_short, n_long))
            print(line)
    print()


def analyze_flip_distance_correlation(df: pd.DataFrame):
    print("=" * 78)
    print("Flip-distance (zero-gamma level) vs forward move magnitude")
    print("=" * 78)
    for horizon in [15, 30]:
        col = f"fwd_abs_move_{horizon}min_pct"
        d = df.dropna(subset=["flip_dist_pct", col])
        n = len(d)
        if n < 5:
            print(f"{horizon}-min: n={n}, too few rows to compute a correlation.")
            continue
        if HAVE_SCIPY:
            rho, p = sps.spearmanr(d["flip_dist_pct"].abs(), d[col])
            print(f"{horizon}-min: Spearman rho={rho:.3f} (p={p:.3f}), n={n}{_n_flag(n)}")
        else:
            corr = d["flip_dist_pct"].abs().corr(d[col])
            print(f"{horizon}-min: Pearson corr={corr:.3f} (scipy unavailable, Spearman not computed), n={n}{_n_flag(n)}")
    print()


def analyze_wall_proximity_and_decay(df: pd.DataFrame):
    print("=" * 78)
    print("Q2: Does proximity to a wall predict reversal? Q3: Does wall decay predict failure?")
    print("=" * 78)
    has_wall_data = df["call_wall"].notna().any() or df["put_wall"].notna().any()
    if not has_wall_data:
        print("BLOCKED: zero rows have wall data. This is expected if all your current")
        print("gex_trend_log rows predate this session's schema fix -- call_wall/put_wall/")
        print("call_wall_gamma/put_wall_gamma were never persisted before now, only computed")
        print("live and discarded each tick. Nothing to analyze until fresh snapshots")
        print("accumulate under the new schema. Re-run this script after a few days of")
        print("collection with the updated _log() in place.")
        print()
        return

    d = df.dropna(subset=["nearest_wall_dist_pct"])
    n = len(d)
    print(f"Rows with usable wall-distance data: {n}")
    if n < MIN_N_FOR_CONCLUSION:
        print(f"n={n} is below the {MIN_N_FOR_CONCLUSION}-row threshold this script uses for a")
        print("conclusion -- reporting what's here, but treat it as directional only.")
    if n >= 5:
        for horizon in [15, 30]:
            col = f"fwd_abs_move_{horizon}min_pct"
            dd = d.dropna(subset=[col])
            if len(dd) < 5:
                continue
            near = dd[dd["nearest_wall_dist_pct"].abs() <= 0.3]  # within 0.3% of a wall
            far = dd[dd["nearest_wall_dist_pct"].abs() > 0.3]
            if len(near) >= 3 and len(far) >= 3:
                print(f"\n{horizon}-min forward |move| when near a wall (<=0.3% away, n={len(near)}): "
                      f"median {near[col].median():.3f}%")
                print(f"{horizon}-min forward |move| when NOT near a wall (n={len(far)}): "
                      f"median {far[col].median():.3f}%")
                print("(Lower moves near a wall would suggest rejection/pin behavior; similar or")
                print(" higher moves near a wall would suggest walls get run through, not respected.)")
                print(_n_flag(min(len(near), len(far))))

    decay_cols_present = df["call_wall_gamma_chg_3"].notna().any() or df["put_wall_gamma_chg_3"].notna().any()
    if not decay_cols_present:
        print("\nWall decay (call_wall_gamma_chg_3 / put_wall_gamma_chg_3): no usable data yet --")
        print("needs at least 3 consecutive post-fix snapshots per symbol to compute a")
        print("rate of change. Will populate as collection continues.")
    else:
        print("\nWall gamma rate-of-change data is present -- decay-vs-failure analysis can run")
        print("once there are enough instances of an actual wall break to compare against.")
        print("(Not enough distinct wall-break events in the current sample to report a")
        print(" specific number yet -- this needs more days of data, not more code.)")
    print()


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    global MIN_N_FOR_CONCLUSION
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", required=True, help="Path to options_data.db")
    parser.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "IWM"])
    parser.add_argument("--min-n", type=int, default=MIN_N_FOR_CONCLUSION,
                         help="Minimum sample size to treat a result as conclusive rather than directional-only")
    parser.add_argument("--export-csv", default=None, help="Optional path to dump the full engineered feature table")
    args = parser.parse_args()

    MIN_N_FOR_CONCLUSION = args.min_n

    print(f"Loading gex_trend_log from {args.db_path} for {args.symbols} ...")
    df = load_gex_log(args.db_path, args.symbols)
    print_schema_coverage(df)
    if df.empty:
        sys.exit(0)

    df = engineer_features(df)
    df = add_forward_returns(df)

    analyze_regime_vs_move_size(df)
    analyze_flip_distance_correlation(df)
    analyze_wall_proximity_and_decay(df)

    print("=" * 78)
    print("BOTTOM LINE")
    print("=" * 78)
    days = df["ts"].dt.date.nunique()
    print(f"Trading days covered: {days}")
    if days < 5:
        print("Too thin to draw firm conclusions on any question. Everything above should be")
        print("read as 'here's the shape of a possible pattern,' not 'this is confirmed.'")
        print("The regime-vs-move-size test is the one to watch first once more data comes in --")
        print("it's the only one with enough historical rows (pre-dating the schema fix) to")
        print("already show a trend, however weak. The wall-proximity and wall-decay questions")
        print("are effectively un-testable on your CURRENT data regardless of day count, since")
        print("wall data was never persisted before this session's fix -- they'll only start")
        print("accumulating real sample size from today forward.")
    else:
        print("Enough days to start trusting directional patterns; still worth another pass")
        print("once you have both a trending and a range-bound session represented.")

    if args.export_csv:
        df.to_csv(args.export_csv, index=False)
        print(f"\nFull engineered feature table exported to {args.export_csv}")


if __name__ == "__main__":
    main()
