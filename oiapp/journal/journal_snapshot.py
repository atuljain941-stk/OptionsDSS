# oiapp/journal/journal_snapshot.py
"""
Trade Snapshot Engine
─────────────────────
Captures market context at trade entry and computes Entry Quality Score.

Stores:
  - Monthly / Weekly / Daily regime
  - RS vs SPY (%)
  - IV, IV Rank, IV Percentile, Expected Move
  - PCR, Call Wall, Put Wall, Gamma Flip, Max Pain, Net GEX, GEX Ratio
  - Total Call OI, Total Put OI, Top-3 Call Walls, Top-3 Put Walls

Also computes:
  - Entry Quality Score (0-100): Should I open this trade?
  - Trade Health Score delta: How has structure changed since entry?

All lookups are non-blocking — missing data returns None gracefully.
"""

from __future__ import annotations
import sqlite3, json, math
from datetime import date, datetime
from pathlib import Path
from typing import Optional

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")


# ── DB helpers ─────────────────────────────────────────────────────────────

def _conn():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def ensure_snapshot_table():
    """Create trade_snapshot table if it doesn't exist (safe migration)."""
    con = _conn()
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_snapshot (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id            INTEGER NOT NULL,
            snapshot_type       TEXT NOT NULL DEFAULT 'entry',  -- entry | refresh
            created_at          TEXT NOT NULL,

            -- Price
            entry_spot          REAL,

            -- Regime (M/W/D)
            regime_monthly      TEXT,
            regime_weekly       TEXT,
            regime_daily        TEXT,

            -- Relative Strength vs SPY
            entry_rs            REAL,

            -- Volatility
            entry_iv            REAL,
            entry_iv_rank       REAL,
            entry_iv_pct        REAL,
            entry_expected_move REAL,

            -- Options structure
            entry_pcr           REAL,
            entry_call_wall     REAL,
            entry_put_wall      REAL,
            entry_gamma_flip    REAL,
            entry_max_pain      REAL,
            entry_net_gex       REAL,
            entry_gex_ratio     REAL,

            -- Top walls (JSON arrays)
            entry_top_call_walls TEXT,
            entry_top_put_walls  TEXT,

            -- OI totals
            entry_call_oi       INTEGER,
            entry_put_oi        INTEGER,

            -- Entry Quality Score
            entry_score         INTEGER,
            entry_grade         TEXT,    -- A / B / C / D
            entry_recommendation TEXT,  -- OPEN / OPEN_SMALL / AVOID
            entry_score_json    TEXT,   -- full breakdown JSON

            UNIQUE(trade_id, snapshot_type)
        )
    """)
    # Add entry_score / entry_grade columns to trades table for quick display
    for sql in (
        "ALTER TABLE trades ADD COLUMN entry_score INTEGER",
        "ALTER TABLE trades ADD COLUMN entry_grade TEXT",
        "ALTER TABLE trades ADD COLUMN entry_recommendation TEXT",
    ):
        try:
            con.execute(sql)
        except Exception:
            pass
    con.commit()
    con.close()


# ── Data collectors ────────────────────────────────────────────────────────

def _fetch_regime(symbol: str) -> dict:
    """Pull latest regime_scan row for symbol."""
    try:
        con = _conn()
        row = con.execute(
            "SELECT bias, confidence, regime, iv_rank, rsi_diff, signals_json "
            "FROM regime_scan WHERE symbol=? ORDER BY scan_date DESC LIMIT 1",
            (symbol.upper(),)
        ).fetchone()
        con.close()
        if row:
            return {
                "bias": row[0] or "",
                "confidence": float(row[1] or 50),
                "regime": row[2] or "",
                "iv_rank": float(row[3]) if row[3] is not None else None,
                "rsi_diff": float(row[4]) if row[4] is not None else None,
            }
    except Exception:
        pass
    return {}


def _fetch_market_structure(symbol: str) -> dict:
    """Run market structure analysis to get M/W/D regimes."""
    try:
        from ..scanners.market_structure import analyze
        data = analyze(symbol)
        regime = data.get("regime", {})
        return {
            "monthly": regime.get("monthly", {}).get("label", ""),
            "weekly":  regime.get("weekly",  {}).get("label", ""),
            "daily":   regime.get("daily",   {}).get("label", ""),
        }
    except Exception:
        pass
    return {}


def _fetch_rs_vs_spy(symbol: str) -> Optional[float]:
    """Compute 20-day RS vs SPY (percent outperformance)."""
    try:
        import yfinance as yf
        import pandas as pd
        sym_df  = yf.Ticker(symbol).history(period="3mo")
        spy_df  = yf.Ticker("SPY").history(period="3mo")
        if sym_df.empty or spy_df.empty:
            return None
        sym_ret  = float(sym_df["Close"].iloc[-1]) / float(sym_df["Close"].iloc[-20]) - 1
        spy_ret  = float(spy_df["Close"].iloc[-1]) / float(spy_df["Close"].iloc[-20]) - 1
        return round((sym_ret - spy_ret) * 100, 2)
    except Exception:
        return None


def _fetch_iv_data(symbol: str) -> dict:
    """Fetch IV, IV Rank, IV percentile, expected move from TA or regime_scan."""
    result = {
        "iv": None, "iv_rank": None, "iv_pct": None, "expected_move": None
    }
    try:
        from ..scanners.spy_strategies import _compute_ta
        ta = _compute_ta(symbol)
        if ta:
            result["iv"]      = ta.get("iv_est")
            result["iv_rank"] = ta.get("iv_rank")
            # IV percentile: approximate via IV Rank (same concept for HV proxy)
            result["iv_pct"]  = ta.get("iv_rank")
            spot = ta.get("price")
            if spot and result["iv"]:
                # 1-week expected move: spot × IV% × sqrt(5/252)
                result["expected_move"] = round(spot * (result["iv"] / 100) * math.sqrt(5 / 252), 2)
    except Exception:
        pass
    # Fallback: regime_scan iv_rank
    if result["iv_rank"] is None:
        reg = _fetch_regime(symbol)
        result["iv_rank"] = reg.get("iv_rank")
    return result


def _fetch_options_structure(symbol: str) -> dict:
    """Fetch PCR, walls, GEX, max pain, OI totals from options DB."""
    result = {
        "pcr": None, "call_wall": None, "put_wall": None,
        "gamma_flip": None, "max_pain": None,
        "net_gex": None, "gex_ratio": None,
        "top_call_walls": [], "top_put_walls": [],
        "call_oi": None, "put_oi": None,
    }
    try:
        from ..services.aggregate import get_pcr_snapshot
        from ..scanners.oi_wall_map import get_oi_wall_map

        # PCR (aggregate across near-term expirations)
        pcr_snaps = get_pcr_snapshot(symbol)
        if pcr_snaps:
            total_calls = sum(s.get("calls", 0) for s in pcr_snaps)
            total_puts  = sum(s.get("puts",  0) for s in pcr_snaps)
            result["pcr"]      = round(total_puts / total_calls, 3) if total_calls else None
            result["call_oi"]  = total_calls
            result["put_oi"]   = total_puts

        # OI Walls
        walls = get_oi_wall_map(symbol, top_n=5, max_dte=60)
        if "error" not in walls:
            put_walls  = walls.get("put_walls",  [])
            call_walls = walls.get("call_walls", [])
            if put_walls:
                result["put_wall"]       = put_walls[-1]["strike"]   # highest put wall below
                result["top_put_walls"]  = [w["strike"] for w in put_walls[:3]]
            if call_walls:
                result["call_wall"]      = call_walls[0]["strike"]   # lowest call wall above
                result["top_call_walls"] = [w["strike"] for w in call_walls[:3]]

        # GEX / Gamma Flip / Max Pain
        try:
            from ..scanners.spy_strategies import _oi_rows, _compute_gex, _compute_ta
            import math as _m
            ta = _compute_ta(symbol)
            if ta:
                spot   = ta["price"]
                iv_atm = ta.get("iv_est", 20.0)
                # Use nearest expiry
                from ..services.aggregate import _future_exps
                exps = _future_exps(symbol)
                if exps:
                    exp   = exps[0]
                    dte   = max(1, (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days)
                    rows  = _oi_rows(symbol, exp)
                    if rows:
                        gex_info = _compute_gex(rows, spot, dte, iv_atm)
                        result["gamma_flip"] = gex_info.get("gamma_flip")
                        result["max_pain"]   = gex_info.get("max_pain")
                        result["net_gex"]    = round(gex_info.get("total_gex", 0), 0)
                        result["gex_ratio"]  = round(gex_info.get("gex_ratio",  0), 3)
                        # Walls from GEX
                        if not result["put_wall"]:
                            result["put_wall"]  = gex_info.get("support")
                            result["call_wall"] = gex_info.get("resistance")
                        if not result["top_put_walls"]:
                            result["top_put_walls"]  = [w[0] for w in gex_info.get("top_put_walls",  [])[:3]]
                            result["top_call_walls"] = [w[0] for w in gex_info.get("top_call_walls", [])[:3]]
        except Exception:
            pass

    except Exception:
        pass
    return result


def capture_entry_snapshot(trade_id: int, symbol: str, trade_type: str,
                            spot: Optional[float] = None) -> dict:
    """
    Capture full market context at trade entry.
    Called immediately after a new trade is saved.
    Returns the snapshot dict (also persists to DB).
    """
    ensure_snapshot_table()

    # Collect all signals in parallel (graceful on missing data)
    ms     = _fetch_market_structure(symbol)
    rs     = _fetch_rs_vs_spy(symbol)
    iv_d   = _fetch_iv_data(symbol)
    opts   = _fetch_options_structure(symbol)
    regime = _fetch_regime(symbol)

    if spot is None:
        try:
            from ..scanners.spy_strategies import _compute_ta
            ta = _compute_ta(symbol)
            spot = ta["price"] if ta else None
        except Exception:
            pass

    snap = {
        "trade_id":           trade_id,
        "snapshot_type":      "entry",
        "created_at":         datetime.now().isoformat(timespec="seconds"),
        "entry_spot":         spot,
        "regime_monthly":     ms.get("monthly", "") or regime.get("regime", ""),
        "regime_weekly":      ms.get("weekly",  ""),
        "regime_daily":       ms.get("daily",   ""),
        "entry_rs":           rs,
        "entry_iv":           iv_d.get("iv"),
        "entry_iv_rank":      iv_d.get("iv_rank"),
        "entry_iv_pct":       iv_d.get("iv_pct"),
        "entry_expected_move":iv_d.get("expected_move"),
        "entry_pcr":          opts.get("pcr"),
        "entry_call_wall":    opts.get("call_wall"),
        "entry_put_wall":     opts.get("put_wall"),
        "entry_gamma_flip":   opts.get("gamma_flip"),
        "entry_max_pain":     opts.get("max_pain"),
        "entry_net_gex":      opts.get("net_gex"),
        "entry_gex_ratio":    opts.get("gex_ratio"),
        "entry_top_call_walls": json.dumps(opts.get("top_call_walls", [])),
        "entry_top_put_walls":  json.dumps(opts.get("top_put_walls",  [])),
        "entry_call_oi":      opts.get("call_oi"),
        "entry_put_oi":       opts.get("put_oi"),
    }

    # Compute Entry Quality Score
    eq = compute_entry_quality_score(
        symbol=symbol, trade_type=trade_type, spot=spot,
        regime_monthly=snap["regime_monthly"],
        regime_weekly=snap["regime_weekly"],
        regime_daily=snap["regime_daily"],
        rs=rs,
        iv_rank=iv_d.get("iv_rank"),
        pcr=opts.get("pcr"),
        call_wall=opts.get("call_wall"),
        put_wall=opts.get("put_wall"),
        gamma_flip=opts.get("gamma_flip"),
    )

    snap.update({
        "entry_score":          eq["score"],
        "entry_grade":          eq["grade"],
        "entry_recommendation": eq["recommendation"],
        "entry_score_json":     json.dumps(eq),
    })

    # Persist
    try:
        con = _conn()
        con.execute("""
            INSERT OR REPLACE INTO trade_snapshot
            (trade_id, snapshot_type, created_at, entry_spot,
             regime_monthly, regime_weekly, regime_daily,
             entry_rs, entry_iv, entry_iv_rank, entry_iv_pct, entry_expected_move,
             entry_pcr, entry_call_wall, entry_put_wall, entry_gamma_flip,
             entry_max_pain, entry_net_gex, entry_gex_ratio,
             entry_top_call_walls, entry_top_put_walls,
             entry_call_oi, entry_put_oi,
             entry_score, entry_grade, entry_recommendation, entry_score_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            snap["trade_id"], snap["snapshot_type"], snap["created_at"], snap["entry_spot"],
            snap["regime_monthly"], snap["regime_weekly"], snap["regime_daily"],
            snap["entry_rs"], snap["entry_iv"], snap["entry_iv_rank"],
            snap["entry_iv_pct"], snap["entry_expected_move"],
            snap["entry_pcr"], snap["entry_call_wall"], snap["entry_put_wall"],
            snap["entry_gamma_flip"], snap["entry_max_pain"],
            snap["entry_net_gex"], snap["entry_gex_ratio"],
            snap["entry_top_call_walls"], snap["entry_top_put_walls"],
            snap["entry_call_oi"], snap["entry_put_oi"],
            snap["entry_score"], snap["entry_grade"],
            snap["entry_recommendation"], snap["entry_score_json"],
        ))
        # Cache entry score back to trades table for quick display
        con.execute(
            "UPDATE trades SET entry_score=?, entry_grade=?, entry_recommendation=? WHERE id=?",
            (eq["score"], eq["grade"], eq["recommendation"], trade_id)
        )
        con.commit()
        con.close()
    except Exception as e:
        print(f"[snapshot] persist error: {e}")

    return snap


def get_snapshot(trade_id: int) -> Optional[dict]:
    """Retrieve the entry snapshot for a trade."""
    try:
        con = _conn()
        row = con.execute(
            "SELECT * FROM trade_snapshot WHERE trade_id=? AND snapshot_type='entry'",
            (trade_id,)
        ).fetchone()
        con.close()
        if row:
            d = dict(row)
            for k in ("entry_top_call_walls", "entry_top_put_walls", "entry_score_json"):
                if d.get(k):
                    try:
                        d[k] = json.loads(d[k])
                    except Exception:
                        pass
            return d
    except Exception:
        pass
    return None


def get_snapshot_delta(trade_id: int, symbol: str, trade_type: str) -> dict:
    """
    Compare entry snapshot vs current market state.
    Returns a delta table: {metric: {entry, current, delta, direction}}.
    Also returns a structural health score (0-100) based on how much has changed.
    """
    entry = get_snapshot(trade_id)
    if not entry:
        return {"error": "no snapshot", "rows": [], "structural_health": None}

    # Fetch current state
    rs_now   = _fetch_rs_vs_spy(symbol)
    iv_now   = _fetch_iv_data(symbol)
    opts_now = _fetch_options_structure(symbol)

    try:
        from ..scanners.spy_strategies import _compute_ta
        ta = _compute_ta(symbol)
        spot_now = ta["price"] if ta else None
    except Exception:
        spot_now = None

    ms_now = _fetch_market_structure(symbol)

    def _delta_row(label, entry_val, current_val, fmt="num", good_dir=None):
        """good_dir: 'up' means increase is positive for trade, 'down' means decrease is good."""
        if entry_val is None and current_val is None:
            return None
        try:
            ev = float(entry_val) if entry_val is not None else None
            cv = float(current_val) if current_val is not None else None
            if ev is not None and cv is not None:
                delta = round(cv - ev, 3)
                pct = round(delta / abs(ev) * 100, 1) if ev and ev != 0 else 0
            else:
                delta = None
                pct = None

            if fmt == "pct":
                ev_s  = f"{ev:+.1f}%" if ev is not None else "—"
                cv_s  = f"{cv:+.1f}%" if cv is not None else "—"
                d_s   = f"{delta:+.1f}%" if delta is not None else "—"
            elif fmt == "price":
                ev_s  = f"${ev:.2f}" if ev is not None else "—"
                cv_s  = f"${cv:.2f}" if cv is not None else "—"
                d_s   = f"{delta:+.2f}" if delta is not None else "—"
            elif fmt == "ivr":
                ev_s  = f"{ev:.0f}%" if ev is not None else "—"
                cv_s  = f"{cv:.0f}%" if cv is not None else "—"
                d_s   = f"{delta:+.0f}%" if delta is not None else "—"
            else:
                ev_s  = f"{ev:.2f}" if ev is not None else "—"
                cv_s  = f"{cv:.2f}" if cv is not None else "—"
                d_s   = f"{delta:+.2f}" if delta is not None else "—"

            # Signal direction: green if improving for trade
            color = "#64748b"
            if delta is not None and good_dir:
                if (good_dir == "up" and delta > 0) or (good_dir == "down" and delta < 0):
                    color = "#22c55e"
                elif delta != 0:
                    color = "#ef4444"

            return {
                "label": label, "entry": ev_s, "current": cv_s,
                "delta": d_s, "color": color, "raw_delta": delta,
            }
        except Exception:
            return {"label": label, "entry": str(entry_val), "current": str(current_val),
                    "delta": "—", "color": "#64748b", "raw_delta": None}

    is_bull = trade_type in ("PS", "PB", "CB")

    rows = []
    r = _delta_row("Spot",    entry.get("entry_spot"),    spot_now,             fmt="price")
    if r: rows.append(r)
    r = _delta_row("RS vs SPY", entry.get("entry_rs"),    rs_now,               fmt="pct", good_dir="up" if is_bull else "down")
    if r: rows.append(r)
    r = _delta_row("PCR",     entry.get("entry_pcr"),     opts_now.get("pcr"),  fmt="num",   good_dir="down" if is_bull else "up")
    if r: rows.append(r)
    r = _delta_row("Put Wall",  entry.get("entry_put_wall"),  opts_now.get("put_wall"),  fmt="price", good_dir="up"   if is_bull else "down")
    if r: rows.append(r)
    r = _delta_row("Call Wall", entry.get("entry_call_wall"), opts_now.get("call_wall"), fmt="price", good_dir="up"   if is_bull else "down")
    if r: rows.append(r)
    r = _delta_row("IV Rank", entry.get("entry_iv_rank"), iv_now.get("iv_rank"), fmt="ivr", good_dir="down" if is_bull else "up")
    if r: rows.append(r)
    r = _delta_row("Max Pain", entry.get("entry_max_pain"), opts_now.get("max_pain"), fmt="price")
    if r: rows.append(r)
    r = _delta_row("Net GEX",  entry.get("entry_net_gex"),  opts_now.get("net_gex"),  fmt="num")
    if r: rows.append(r)

    # Regime change text rows
    def _text_row(label, e_val, c_val):
        if not e_val and not c_val:
            return None
        changed = str(e_val or "—") != str(c_val or "—")
        return {
            "label": label, "entry": e_val or "—", "current": c_val or "—",
            "delta": "changed" if changed else "same",
            "color": "#ef4444" if changed else "#22c55e",
            "raw_delta": None, "is_text": True,
        }

    r = _text_row("Monthly Regime", entry.get("regime_monthly"), ms_now.get("monthly"))
    if r: rows.append(r)
    r = _text_row("Weekly Regime",  entry.get("regime_weekly"),  ms_now.get("weekly"))
    if r: rows.append(r)
    r = _text_row("Daily Regime",   entry.get("regime_daily"),   ms_now.get("daily"))
    if r: rows.append(r)

    # Structural health score: weighted degradation
    structural_health = _compute_structural_health(
        trade_type=trade_type, rows=rows, entry=entry,
        spot_now=spot_now, rs_now=rs_now, opts_now=opts_now,
        iv_now=iv_now, ms_now=ms_now,
    )

    return {
        "rows": rows,
        "structural_health": structural_health,
        "entry_snapshot": entry,
        "current": {
            "spot": spot_now, "rs": rs_now,
            "pcr": opts_now.get("pcr"), "call_wall": opts_now.get("call_wall"),
            "put_wall": opts_now.get("put_wall"), "iv_rank": iv_now.get("iv_rank"),
            "regime_monthly": ms_now.get("monthly"), "regime_weekly": ms_now.get("weekly"),
            "regime_daily": ms_now.get("daily"),
        }
    }


def _compute_structural_health(trade_type, rows, entry, spot_now, rs_now,
                                 opts_now, iv_now, ms_now) -> Optional[dict]:
    """
    Compute structural health score (0-100) based on change from entry.

    Weights (matching the design spec):
      Regime Change      20%
      RS Change          20%
      Wall Migration     20%
      IV Change          15%
      PCR Change         15%
      OI Change          10%
    """
    if not entry:
        return None

    score = 70  # neutral starting point
    reasons = []
    is_bull = trade_type in ("PS", "PB", "CB")

    # ── Regime Change (20% = ±20 pts) ────────────────────────────────────
    regime_pts = 0
    for key, label in [("regime_weekly", "Weekly"), ("regime_daily", "Daily"), ("regime_monthly", "Monthly")]:
        e_reg = (entry.get(key) or "").lower()
        c_reg = (ms_now.get(key.replace("regime_", "")) or "").lower()
        if not e_reg or not c_reg:
            continue
        was_trend = "uptrend" in e_reg or "downtrend" in e_reg
        now_range  = "range" in c_reg or "sideways" in c_reg
        bull_break = "uptrend" not in c_reg and "uptrend" in e_reg
        bear_break = "downtrend" not in c_reg and "downtrend" in e_reg
        if is_bull and bull_break:
            regime_pts -= 7; reasons.append(f"{label} regime weakened: {c_reg}")
        elif not is_bull and bear_break:
            regime_pts -= 7; reasons.append(f"{label} regime weakened: {c_reg}")
        elif now_range and was_trend:
            regime_pts -= 3; reasons.append(f"{label} moved to range")
    score += max(-20, regime_pts)

    # ── RS Change (20% = ±20 pts) ─────────────────────────────────────────
    e_rs = entry.get("entry_rs")
    if e_rs is not None and rs_now is not None:
        rs_delta = rs_now - e_rs
        if is_bull:
            if rs_delta < -5:   score -= 15; reasons.append(f"RS deteriorated {rs_delta:+.1f}%")
            elif rs_delta < -2: score -= 7;  reasons.append(f"RS weakening {rs_delta:+.1f}%")
            elif rs_delta > 3:  score += 8;  reasons.append(f"RS strengthening {rs_delta:+.1f}%")
        else:
            if rs_delta > 5:    score -= 15; reasons.append(f"RS strengthened vs entry {rs_delta:+.1f}%")
            elif rs_delta > 2:  score -= 7;  reasons.append(f"RS recovering {rs_delta:+.1f}%")
            elif rs_delta < -3: score += 8;  reasons.append(f"RS weakening (helps bear) {rs_delta:+.1f}%")

    # ── Wall Migration (20% = ±20 pts) ───────────────────────────────────
    e_put = entry.get("entry_put_wall")
    c_put = opts_now.get("put_wall")
    if e_put and c_put and spot_now:
        put_moved = c_put - e_put
        if is_bull and put_moved < -3:
            pts = min(15, int(abs(put_moved) / max(spot_now, 1) * 1000))
            score -= pts; reasons.append(f"Put wall dropped ${put_moved:.0f} (support eroding)")
        elif is_bull and put_moved > 3:
            score += 8; reasons.append(f"Put wall rose ${put_moved:+.0f} (support strengthened)")
        elif not is_bull and put_moved > 3:
            score -= 8; reasons.append(f"Put wall rising (bearish trade pressure)")

    e_call = entry.get("entry_call_wall")
    c_call = opts_now.get("call_wall")
    if e_call and c_call and spot_now:
        call_moved = c_call - e_call
        if not is_bull and call_moved > 3:
            pts = min(10, int(abs(call_moved) / max(spot_now, 1) * 1000))
            score -= pts; reasons.append(f"Call wall rose ${call_moved:+.0f} (resistance strengthened)")
        elif is_bull and call_moved > 3:
            score += 6; reasons.append(f"Call wall rose ${call_moved:+.0f} (room to run)")

    # ── IV Change (15% = ±15 pts) ─────────────────────────────────────────
    e_ivr = entry.get("entry_iv_rank")
    c_ivr = iv_now.get("iv_rank")
    is_credit = trade_type in ("PS", "CS", "IC")
    if e_ivr is not None and c_ivr is not None:
        ivr_delta = c_ivr - e_ivr
        if is_credit and ivr_delta < -15:
            score -= 10; reasons.append(f"IV collapsed {ivr_delta:+.0f}% (credit shrunk)")
        elif is_credit and ivr_delta > 15:
            score += 8;  reasons.append(f"IV elevated {ivr_delta:+.0f}% (credit still juicy)")
        elif not is_credit and ivr_delta > 20:
            score += 10; reasons.append(f"IV expanded {ivr_delta:+.0f}% (debit value)")
        elif not is_credit and ivr_delta < -10:
            score -= 8;  reasons.append(f"IV shrunk {ivr_delta:+.0f}% (debit lost value)")

    # ── PCR Change (15% = ±15 pts) ────────────────────────────────────────
    e_pcr = entry.get("entry_pcr")
    c_pcr = opts_now.get("pcr")
    if e_pcr is not None and c_pcr is not None:
        pcr_delta = c_pcr - e_pcr
        if is_bull and pcr_delta > 0.3:
            score -= 10; reasons.append(f"PCR worsening {pcr_delta:+.2f} (bearish flow)")
        elif is_bull and pcr_delta < -0.2:
            score += 8;  reasons.append(f"PCR improving {pcr_delta:+.2f} (bullish flow)")
        elif not is_bull and pcr_delta < -0.3:
            score -= 10; reasons.append(f"PCR falling {pcr_delta:+.2f} (bearish unwind)")
        elif not is_bull and pcr_delta > 0.2:
            score += 8;  reasons.append(f"PCR rising {pcr_delta:+.2f} (bearish flow)")

    # ── OI Change (10% = ±10 pts) ─────────────────────────────────────────
    e_call_oi = entry.get("entry_call_oi")
    e_put_oi  = entry.get("entry_put_oi")
    c_call_oi = opts_now.get("call_oi")
    c_put_oi  = opts_now.get("put_oi")
    if e_call_oi and e_put_oi and c_call_oi and c_put_oi:
        e_pcr2 = e_put_oi / max(e_call_oi, 1)
        c_pcr2 = c_put_oi / max(c_call_oi, 1)
        oi_pcr_delta = c_pcr2 - e_pcr2
        if is_bull and oi_pcr_delta > 0.2:
            score -= 7; reasons.append("OI shift: puts building vs calls")
        elif is_bull and oi_pcr_delta < -0.2:
            score += 5; reasons.append("OI shift: calls building vs puts")

    score = max(5, min(97, round(score)))

    # Health tier
    if score >= 70:
        tier = "STRONG HOLD"; tier_color = "#22c55e"
    elif score >= 50:
        tier = "WATCH"; tier_color = "#f59e0b"
    elif score >= 35:
        tier = "WARNING"; tier_color = "#f97316"
    elif score >= 20:
        tier = "EXIT CANDIDATE"; tier_color = "#ef4444"
    else:
        tier = "CRITICAL"; tier_color = "#dc2626"

    # Deterioration flag
    deteriorating = score < 60 and len([r for r in reasons if r]) >= 2

    return {
        "score": score,
        "tier": tier,
        "tier_color": tier_color,
        "reasons": reasons[:5],
        "deteriorating": deteriorating,
    }


# ── Entry Quality Score ────────────────────────────────────────────────────

def compute_entry_quality_score(
    symbol: str, trade_type: str, spot: Optional[float],
    regime_monthly: str = "", regime_weekly: str = "", regime_daily: str = "",
    rs: Optional[float] = None,
    iv_rank: Optional[float] = None,
    pcr: Optional[float] = None,
    call_wall: Optional[float] = None,
    put_wall: Optional[float] = None,
    gamma_flip: Optional[float] = None,
    earn_days: Optional[int] = None,
) -> dict:
    """
    Entry Quality Score (0-100): Should I open this trade?

    Factors:
      - Regime alignment (M/W/D)  25 pts
      - RS vs SPY                 20 pts
      - IV Rank (trade type fit)  15 pts
      - PCR                       15 pts
      - Wall support              15 pts
      - Gamma Flip alignment      10 pts
    """
    score = 50
    pros  = []
    cons  = []
    is_bull   = trade_type in ("PS", "PB", "CB")
    is_bear   = trade_type in ("CS",)
    is_credit = trade_type in ("PS", "CS", "IC")
    is_ic     = trade_type == "IC"

    # ── Regime alignment (25 pts) ─────────────────────────────────────────
    def _regime_score(label, weight):
        lbl = (label or "").lower()
        if "strong uptrend" in lbl:
            return weight if is_bull or is_ic else -weight + 5
        elif "uptrend" in lbl:
            return int(weight * 0.7) if is_bull or is_ic else -int(weight * 0.4)
        elif "strong downtrend" in lbl:
            return weight if is_bear or is_ic else -weight + 5
        elif "downtrend" in lbl:
            return int(weight * 0.7) if is_bear or is_ic else -int(weight * 0.4)
        elif "range" in lbl or "sideways" in lbl:
            return int(weight * 0.5) if is_ic else 0
        return 0

    m_pts = _regime_score(regime_monthly, 8)
    w_pts = _regime_score(regime_weekly,  10)
    d_pts = _regime_score(regime_daily,   7)
    score += m_pts + w_pts + d_pts

    if m_pts > 0 and w_pts > 0:
        pros.append(f"M/W/D regimes aligned: {regime_monthly} / {regime_weekly}")
    elif m_pts < 0 or w_pts < 0:
        cons.append(f"Regime headwind: {regime_weekly or regime_monthly}")

    # ── RS vs SPY (20 pts) ────────────────────────────────────────────────
    if rs is not None:
        if is_bull:
            if rs > 5:    score += 15; pros.append(f"RS +{rs:.1f}% vs SPY — strong outperformance")
            elif rs > 2:  score += 8;  pros.append(f"RS +{rs:.1f}% vs SPY — mild outperformance")
            elif rs < -5: score -= 15; cons.append(f"RS {rs:.1f}% vs SPY — underperforming")
            elif rs < -2: score -= 7;  cons.append(f"RS {rs:.1f}% vs SPY — mild underperformance")
        elif is_bear:
            if rs < -5:   score += 15; pros.append(f"RS {rs:.1f}% vs SPY — confirmed weakness")
            elif rs < -2: score += 8;  pros.append(f"RS {rs:.1f}% vs SPY — underperforming SPY")
            elif rs > 5:  score -= 15; cons.append(f"RS +{rs:.1f}% — stock outperforming, headwind for bears")
            elif rs > 2:  score -= 7;  cons.append(f"RS +{rs:.1f}% vs SPY — mild outperformance")
        else:  # IC: prefer neutral RS
            if abs(rs) <= 3: score += 8;  pros.append(f"RS {rs:+.1f}% — neutral (ideal for IC)")
            elif abs(rs) > 8: score -= 5; cons.append(f"RS {rs:+.1f}% — trending hard, risky for IC")

    # ── IV Rank (15 pts) ──────────────────────────────────────────────────
    if iv_rank is not None:
        if is_credit:
            if iv_rank > 65:   score += 12; pros.append(f"IVR {iv_rank:.0f}% — premium rich, sell favoured")
            elif iv_rank > 45: score += 7;  pros.append(f"IVR {iv_rank:.0f}% — good credit opportunity")
            elif iv_rank > 30: score += 3
            elif iv_rank < 20: score -= 10; cons.append(f"IVR {iv_rank:.0f}% — very low, thin premium")
            elif iv_rank < 30: score -= 5;  cons.append(f"IVR {iv_rank:.0f}% — low, limited credit")
        else:  # debit
            if iv_rank < 25:   score += 12; pros.append(f"IVR {iv_rank:.0f}% — options cheap, buy favoured")
            elif iv_rank < 35: score += 7;  pros.append(f"IVR {iv_rank:.0f}% — reasonable debit cost")
            elif iv_rank > 65: score -= 10; cons.append(f"IVR {iv_rank:.0f}% — expensive, debit suffers")
            elif iv_rank > 50: score -= 5;  cons.append(f"IVR {iv_rank:.0f}% — elevated, debit costly")

    # ── PCR (15 pts) ──────────────────────────────────────────────────────
    if pcr is not None:
        if is_bull:
            if pcr < 0.7:   score += 12; pros.append(f"PCR {pcr:.2f} — call-heavy, bullish flow")
            elif pcr < 0.9: score += 6;  pros.append(f"PCR {pcr:.2f} — balanced/bullish")
            elif pcr > 1.3: score -= 12; cons.append(f"PCR {pcr:.2f} — heavy put protection, bearish flow")
            elif pcr > 1.1: score -= 6;  cons.append(f"PCR {pcr:.2f} — mildly bearish flow")
        elif is_bear:
            if pcr > 1.3:   score += 12; pros.append(f"PCR {pcr:.2f} — elevated puts, bearish flow")
            elif pcr > 1.1: score += 6;  pros.append(f"PCR {pcr:.2f} — mildly bearish flow")
            elif pcr < 0.7: score -= 12; cons.append(f"PCR {pcr:.2f} — call-heavy, bearish headwind")
            elif pcr < 0.9: score -= 6;  cons.append(f"PCR {pcr:.2f} — balanced, low bear flow")
        else:  # IC
            if 0.8 < pcr < 1.2: score += 8;  pros.append(f"PCR {pcr:.2f} — balanced (ideal for IC)")
            elif pcr > 1.5:     score -= 5;  cons.append(f"PCR {pcr:.2f} — strong put bias")
            elif pcr < 0.6:     score -= 5;  cons.append(f"PCR {pcr:.2f} — strong call bias")

    # ── Wall Support (15 pts) ─────────────────────────────────────────────
    if spot and put_wall and call_wall:
        put_dist  = (spot - put_wall) / spot * 100
        call_dist = (call_wall - spot) / spot * 100
        if is_bull:
            if put_dist < 2:   score += 10; pros.append(f"Put wall ${put_wall:.0f} close by — strong support")
            elif put_dist < 5: score += 6;  pros.append(f"Put wall ${put_wall:.0f} ({put_dist:.1f}% away) — support nearby")
            else:              score -= 3;  cons.append(f"Put wall ${put_wall:.0f} far ({put_dist:.1f}%) — limited support")
        elif is_bear:
            if call_dist < 2:  score += 10; pros.append(f"Call wall ${call_wall:.0f} close — resistance confirmed")
            elif call_dist < 5:score += 6;  pros.append(f"Call wall ${call_wall:.0f} ({call_dist:.1f}% away)")
            else:              score -= 3;  cons.append(f"Call wall ${call_wall:.0f} far ({call_dist:.1f}%) — limited resistance")
        elif is_ic:
            if put_dist < 5 and call_dist < 5:
                score += 10; pros.append(f"Pinched in walls ${put_wall:.0f}–${call_wall:.0f}")
            elif call_dist < 3 or put_dist < 3:
                score -= 5; cons.append("Spot too close to one wall — IC risky")

    # ── Gamma Flip (10 pts) ───────────────────────────────────────────────
    if spot and gamma_flip:
        gf_dist = (gamma_flip - spot) / spot * 100
        if is_bull and spot > gamma_flip:
            score += 8; pros.append(f"Spot above gamma flip ${gamma_flip:.0f} — dealers long gamma")
        elif is_bull and gf_dist < 1:
            score -= 5; cons.append(f"Near gamma flip ${gamma_flip:.0f} — vol amplification risk")
        elif is_bear and spot < gamma_flip:
            score += 8; pros.append(f"Spot below gamma flip ${gamma_flip:.0f} — trend amplification")
        elif is_bear and abs(gf_dist) < 1:
            score -= 5; cons.append(f"Near gamma flip ${gamma_flip:.0f} — unstable vol regime")

    # ── Earnings penalty ──────────────────────────────────────────────────
    if earn_days is not None and earn_days < 14:
        pts = 20 if earn_days < 7 else 10
        score -= pts
        cons.append(f"Earnings in {earn_days} days — binary risk")

    score = max(5, min(97, round(score)))

    # Grade + recommendation
    if score >= 80:
        grade = "A"; rec = "OPEN"
        summary = f"Strong setup. {len(pros)} factors aligned."
    elif score >= 65:
        grade = "B"; rec = "OPEN"
        summary = f"Good setup. Proceed with standard size."
    elif score >= 50:
        grade = "C"; rec = "OPEN_SMALL"
        summary = f"Marginal setup. Consider half size."
    elif score >= 35:
        grade = "D"; rec = "OPEN_SMALL"
        summary = f"Weak setup. Only open if thesis is very strong."
    else:
        grade = "F"; rec = "AVOID"
        summary = f"Poor setup. {len(cons)} factors against. Avoid this trade."

    return {
        "score": score, "grade": grade, "recommendation": rec, "summary": summary,
        "pros": pros[:5], "cons": cons[:5],
    }


# ── Preview: compute entry score BEFORE saving ─────────────────────────────

def preview_entry_score(symbol: str, trade_type: str) -> dict:
    """
    Called from the Add Trade form before saving.
    Returns entry quality score + full market snapshot preview.
    """
    rs     = _fetch_rs_vs_spy(symbol)
    iv_d   = _fetch_iv_data(symbol)
    opts   = _fetch_options_structure(symbol)
    ms     = _fetch_market_structure(symbol)

    spot = None
    try:
        from ..scanners.spy_strategies import _compute_ta
        ta = _compute_ta(symbol)
        spot = ta["price"] if ta else None
    except Exception:
        pass

    eq = compute_entry_quality_score(
        symbol=symbol, trade_type=trade_type, spot=spot,
        regime_monthly=ms.get("monthly", ""),
        regime_weekly=ms.get("weekly", ""),
        regime_daily=ms.get("daily", ""),
        rs=rs,
        iv_rank=iv_d.get("iv_rank"),
        pcr=opts.get("pcr"),
        call_wall=opts.get("call_wall"),
        put_wall=opts.get("put_wall"),
        gamma_flip=opts.get("gamma_flip"),
    )

    return {
        "symbol": symbol,
        "trade_type": trade_type,
        "spot": spot,
        "rs_vs_spy": rs,
        "iv": iv_d.get("iv"),
        "iv_rank": iv_d.get("iv_rank"),
        "iv_pct": iv_d.get("iv_pct"),
        "expected_move": iv_d.get("expected_move"),
        "pcr": opts.get("pcr"),
        "call_wall": opts.get("call_wall"),
        "put_wall": opts.get("put_wall"),
        "gamma_flip": opts.get("gamma_flip"),
        "max_pain": opts.get("max_pain"),
        "net_gex": opts.get("net_gex"),
        "top_call_walls": opts.get("top_call_walls"),
        "top_put_walls": opts.get("top_put_walls"),
        "call_oi": opts.get("call_oi"),
        "put_oi": opts.get("put_oi"),
        "regime_monthly": ms.get("monthly"),
        "regime_weekly": ms.get("weekly"),
        "regime_daily": ms.get("daily"),
        **eq,
    }
