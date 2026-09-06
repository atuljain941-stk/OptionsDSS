# scanners/pcr_change.py
"""
PCR Change Scanner — fixed for duplicate-date rows.
Each snapshot aggregates with SUM+GROUP BY (symbol,expiry,type,date).
Spot from shared yfinance cache (real price).
"""
from .common import get_conn, get_latest_two_asof, TABLE, COLS
from ._spot_cache import get_spot, get_spot_and_prev
from datetime import datetime, date


def _dte(expiry_str: str) -> int:
    try:
        return (datetime.strptime(expiry_str, "%Y-%m-%d").date() - date.today()).days
    except Exception:
        return 999


def _expiry_type(dte: int) -> str:
    if dte <= 0:   return "0DTE"
    if dte <= 7:   return "Weekly"
    if dte <= 35:  return "Monthly"
    return "LEAPS"


def _pcr_regime(pcr: float) -> str:
    if pcr >= 1.3:  return "Fearful (Bearish Sentiment)"
    if pcr >= 0.9:  return "Neutral"
    if pcr >= 0.6:  return "Complacent (Bullish Sentiment)"
    return "Very Complacent"


def run_pcr_scanner(threshold=10, max_dte=60, per_expiry=True):
    conn = get_conn()
    try:
        latest, prev = get_latest_two_asof(conn)
    except RuntimeError:
        conn.close()
        return []

    # ── Build put/call OI per (symbol [,expiry]) for each snapshot date.
    # SUM+GROUP BY collapses duplicate rows from same-day scheduler re-runs.
    def _snap(ts, by_expiry):
        if by_expiry:
            q = f"""
            SELECT {COLS['symbol']}, {COLS['expiry']},
                   SUM(CASE WHEN {COLS['type']} LIKE 'P%' THEN {COLS['oi']} ELSE 0 END) AS put_oi,
                   SUM(CASE WHEN {COLS['type']} LIKE 'C%' THEN {COLS['oi']} ELSE 0 END) AS call_oi
            FROM {TABLE}
            WHERE {COLS['asof']}=?
            GROUP BY {COLS['symbol']}, {COLS['expiry']}
            """
            return {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(q, (ts,)).fetchall()}
        else:
            q = f"""
            SELECT {COLS['symbol']},
                   SUM(CASE WHEN {COLS['type']} LIKE 'P%' THEN {COLS['oi']} ELSE 0 END) AS put_oi,
                   SUM(CASE WHEN {COLS['type']} LIKE 'C%' THEN {COLS['oi']} ELSE 0 END) AS call_oi
            FROM {TABLE}
            WHERE {COLS['asof']}=?
            GROUP BY {COLS['symbol']}
            """
            return {r[0]: (r[1], r[2]) for r in conn.execute(q, (ts,)).fetchall()}

    now_snap  = _snap(latest, per_expiry)
    prev_snap = _snap(prev,   per_expiry)
    conn.close()

    # batch-fetch real spot prices (cached)
    syms = {(k[0] if per_expiry else k) for k in now_snap}
    spot_cache = {s: get_spot_and_prev(s) for s in syms}

    rows = []
    for key in now_snap:
        if key not in prev_snap:
            continue
        p_put,  p_call  = now_snap[key]
        pp_put, pp_call = prev_snap[key]
        if not p_call or not pp_call:
            continue

        sym    = key[0] if per_expiry else key
        expiry = key[1] if per_expiry else None
        dte    = _dte(expiry) if expiry else 999
        if dte > max_dte or dte < 0:
            continue

        now_pcr  = p_put  / p_call
        prev_pcr = pp_put / pp_call
        pct_chg  = 100 * (now_pcr - prev_pcr) / prev_pcr if prev_pcr else 0
        if abs(pct_chg) < threshold:
            continue

        spot_now, spot_prev = spot_cache.get(sym, (None, None))

        divergence = ""
        if spot_now and spot_prev:
            price_up = spot_now > spot_prev
            pcr_up   = now_pcr > prev_pcr
            if pcr_up and price_up:
                divergence = "Bearish Div (PCR↑ + Price↑ → hedge against rally → sell calls/IC)"
            elif not pcr_up and not price_up:
                divergence = "Bullish Div (PCR↓ + Price↓ → complacency on dip → tighten put spreads)"

        regime   = _pcr_regime(now_pcr)
        exp_type = _expiry_type(dte) if expiry else "All"

        score_pct = min(30, int(abs(pct_chg) / 2))
        score_mag = min(20, int(now_pcr * 10))
        score_dte = 25 if dte <= 7 else (20 if dte <= 14 else (15 if dte <= 35 else 5))
        score_div = 15 if divergence else 0
        signal_score = min(100, score_pct + score_mag + score_dte + score_div)

        rows.append({
            "symbol":       sym,
            "expiry":       expiry or "ALL",
            "expiry_type":  exp_type,
            "dte":          dte if dte < 999 else None,
            "prev_puts":    int(pp_put),
            "prev_calls":   int(pp_call),
            "now_puts":     int(p_put),
            "now_calls":    int(p_call),
            "pcr_prev":     round(prev_pcr, 3),
            "pcr_now":      round(now_pcr, 3),
            "pct_change":   round(pct_chg, 2),
            "direction":    "PCR Rising ▲" if pct_chg > 0 else "PCR Falling ▼",
            "regime":       regime,
            "divergence":   divergence,
            "signal_score": signal_score,
            "spot":         round(spot_now, 2) if spot_now else None,
        })

    rows.sort(key=lambda r: -r["signal_score"])
    return rows
