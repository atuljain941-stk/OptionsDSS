"""
conviction_scorer.py — Compute a composite conviction score for a symbol
by aggregating signals from all available scanners at the time of query.

Score components (each 0-2, total 0-12):
  1. OI Buildup          — call OI growth, call/put bias, PCR
  2. Regime              — trending bullish regime
  3. RSI MTF             — bull setup (daily RSI high + intraday pullback)
  4. S/R Breakout        — price breaking above a key level
  5. Institutional       — accumulation + volume + base quality
  6. Price Momentum      — price change + EMA stack
"""
import sqlite3, json
from pathlib import Path
from datetime import date, datetime, timedelta
from flask import Blueprint, jsonify, request

conv_bp = Blueprint("conv_bp", __name__, url_prefix="/conviction")
DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")

def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c


def score_symbol(symbol: str) -> dict:
    """
    Compute conviction score for a symbol from the current state of the DB.
    Returns dict with total_score (0-12), component scores, and signal summary.
    """
    sym = symbol.upper()
    score = 0.0
    signals = []
    details = {}

    # ── 1. OI Buildup (0-2) ─────────────────────────────────────────────
    try:
        con = _conn()
        cutoff = (date.today() - timedelta(days=35)).isoformat()
        oi_rows = con.execute("""
            SELECT date,
                   SUM(CASE WHEN type='call' THEN oi ELSE 0 END) call_oi,
                   SUM(CASE WHEN type='put'  THEN oi ELSE 0 END) put_oi
            FROM options
            WHERE symbol=? AND date>=? AND expiration>=date
            GROUP BY date ORDER BY date
        """, (sym, cutoff)).fetchall()
        con.close()
        if len(oi_rows) >= 5:
            first_oi = oi_rows[0]["call_oi"] + oi_rows[0]["put_oi"]
            last_oi  = oi_rows[-1]["call_oi"] + oi_rows[-1]["put_oi"]
            call_pct = oi_rows[-1]["call_oi"] / max(1, last_oi) * 100
            pcr = oi_rows[-1]["put_oi"] / max(1, oi_rows[-1]["call_oi"])
            oi_growth = (last_oi - first_oi) / max(1, first_oi) * 100
            oi_score = 0
            if oi_growth > 30: oi_score = 2.0
            elif oi_growth > 10: oi_score = 1.5
            elif oi_growth > 0:  oi_score = 1.0
            if call_pct > 55: oi_score += 0.3  # calls dominate
            if pcr < 0.7:     oi_score += 0.2  # low put activity
            oi_score = min(2, round(oi_score, 1))
            score += oi_score
            details["oi"] = {"score": oi_score, "growth_pct": round(oi_growth,1),
                             "call_pct": round(call_pct,1), "pcr": round(pcr,3)}
            if oi_score >= 1.5: signals.append(f"OI building +{round(oi_growth)}% (calls {round(call_pct)}%)")
    except: details["oi"] = {"score": 0, "error": True}

    # ── 2. Regime (0-2) ─────────────────────────────────────────────────
    try:
        con = _conn()
        today = date.today().isoformat()
        rg = con.execute("""
            SELECT regime, confidence, bias FROM regime_scan
            WHERE symbol=? AND scan_date=?
        """, (sym, today)).fetchone()
        if not rg:
            rg = con.execute("""
                SELECT regime, confidence, bias FROM regime_scan
                WHERE symbol=? ORDER BY scan_date DESC LIMIT 1
            """, (sym,)).fetchone()
        con.close()
        if rg:
            regime = rg["regime"] or ""
            conf   = rg["confidence"] or 0
            bias   = rg["bias"] or ""
            rg_score = 0
            if "trending" in regime.lower() and "bullish" in bias.lower():
                rg_score = 1.5 if conf >= 70 else 1.0
            elif "consolidating" in regime.lower():
                rg_score = 0.5
            score += rg_score
            details["regime"] = {"score": rg_score, "regime": regime,
                                  "bias": bias, "confidence": conf}
            if rg_score >= 1.0: signals.append(f"Regime: {regime} ({bias})")
    except: details["regime"] = {"score": 0}

    # ── 3. S/R Proximity / Breakout (0-2) ───────────────────────────────
    try:
        con = _conn()
        sr_cached = con.execute(
            "SELECT value FROM app_cache WHERE key='sr_breakout_scan'"
        ).fetchone()
        con.close()
        sr_score = 0
        if sr_cached:
            sr_data = json.loads(sr_cached[0])
            sym_sr  = next((r for r in sr_data if r.get("symbol")==sym), None)
            if sym_sr:
                sr_score = 1.5
                signals.append(f"S/R Breakout detected")
        details["sr"] = {"score": sr_score}
        score += sr_score
    except: details["sr"] = {"score": 0}

    # ── 4. Institutional Setup (0-2) ────────────────────────────────────
    try:
        con = _conn()
        inst_cached = con.execute(
            "SELECT value FROM app_cache WHERE key='institutional_scan'"
        ).fetchone()
        con.close()
        inst_score = 0
        if inst_cached:
            inst_data = json.loads(inst_cached[0])
            sym_inst  = next((r for r in inst_data if r.get("symbol")==sym), None)
            if sym_inst:
                raw_score = sym_inst.get("score", 0)
                inst_score = min(2, round(raw_score / 5, 1))
                signals.append(f"Institutional setup: {sym_inst.get('breakout_type','?')} (score {raw_score})")
        details["institutional"] = {"score": inst_score}
        score += inst_score
    except: details["institutional"] = {"score": 0}

    # ── 5. RSI MTF (0-2) ────────────────────────────────────────────────
    try:
        con = _conn()
        rsi_cached = con.execute(
            "SELECT value FROM app_cache WHERE key='rsi_mtf_scan'"
        ).fetchone()
        con.close()
        rsi_score = 0
        if rsi_cached:
            rsi_data  = json.loads(rsi_cached[0])
            rsi_syms  = rsi_data if isinstance(rsi_data, list) else rsi_data.get("results", [])
            sym_rsi   = next((r for r in rsi_syms if r.get("symbol")==sym), None)
            if sym_rsi:
                setup = sym_rsi.get("setup","")
                if "bull" in str(setup).lower():
                    rsi_score = 2.0
                    signals.append(f"RSI MTF Bull setup")
        details["rsi_mtf"] = {"score": rsi_score}
        score += rsi_score
    except: details["rsi_mtf"] = {"score": 0}

    # ── 6. OI Buildup Cache (price momentum) (0-2) ──────────────────────
    try:
        con = _conn()
        oib_cached = con.execute(
            "SELECT value FROM app_cache WHERE key='oi_buildup_scan'"
        ).fetchone()
        con.close()
        oib_score = 0
        if oib_cached:
            oib_data = json.loads(oib_cached[0])
            sym_oib  = next((r for r in oib_data if r.get("symbol")==sym), None)
            if sym_oib:
                bias   = sym_oib.get("bias","")
                p5d    = sym_oib.get("price_5d_pct",0) or 0
                bs     = sym_oib.get("bias_score",0)  or 0
                if "Bullish" in bias and bs >= 2:   oib_score = 2.0
                elif "Bullish" in bias:              oib_score = 1.5
                elif "Sideways" not in bias and bs > 0: oib_score = 0.5
                signals.append(f"OI Buildup: {bias} (5d: {p5d:+.1f}%)")
        details["oi_buildup"] = {"score": oib_score}
        score += oib_score
    except: details["oi_buildup"] = {"score": 0}

    total = round(min(12, score), 1)
    label = ("🔥 Strong" if total >= 8 else
             "✅ Good"   if total >= 5 else
             "👀 Moderate" if total >= 3 else
             "⚠ Weak")

    return {
        "symbol":      sym,
        "total_score": total,
        "max_score":   12,
        "label":       label,
        "signals":     signals,
        "components":  details,
        "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


@conv_bp.route("/score/<symbol>")
def get_conviction(symbol):
    return jsonify(score_symbol(symbol))


@conv_bp.route("/snapshot", methods=["POST"])
def save_snapshot():
    """
    Save a conviction snapshot at the time of trade entry.
    Body: {trade_id, symbol}
    """
    d   = request.get_json(force=True) or {}
    sym = d.get("symbol","").upper()
    tid = d.get("trade_id")
    if not sym or not tid:
        return jsonify({"error": "symbol and trade_id required"}), 400
    result = score_symbol(sym)
    result["trade_id"] = tid
    # Save to trade_signals table
    con = _conn()
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_signals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id   INTEGER NOT NULL,
            symbol     TEXT NOT NULL,
            total_score REAL,
            label      TEXT,
            signals_json TEXT,
            components_json TEXT,
            snapshot_at TEXT
        )
    """)
    con.execute("""
        INSERT OR REPLACE INTO trade_signals
        (trade_id, symbol, total_score, label, signals_json, components_json, snapshot_at)
        VALUES (?,?,?,?,?,?,?)
    """, (tid, sym, result["total_score"], result["label"],
          json.dumps(result["signals"]), json.dumps(result["components"]),
          result["computed_at"]))
    con.commit(); con.close()
    return jsonify({"ok": True, "snapshot": result})


@conv_bp.route("/snapshot/<int:trade_id>")
def get_snapshot(trade_id):
    """Get saved conviction snapshot for a trade."""
    con = _conn()
    row = con.execute(
        "SELECT * FROM trade_signals WHERE trade_id=? ORDER BY id DESC LIMIT 1",
        (trade_id,)
    ).fetchone()
    con.close()
    if not row: return jsonify({"found": False})
    return jsonify({"found": True, "snapshot": dict(row)})
