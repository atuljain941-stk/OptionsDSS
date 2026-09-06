# scanners/large_oi_change.py
"""
Large OI Change Scanner — fixed for duplicate-date rows.
All OI values are SUM'd + GROUP BY (symbol, expiry, strike, type, date)
so running the scheduler multiple times on the same day doesn't inflate numbers.
Spot price from shared yfinance cache (real price, not mid-strike proxy).
"""
from .common import get_conn, get_latest_two_asof, TABLE, COLS
from ._spot_cache import get_spot
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


def _get_max_oi_per_chain(conn, asof: str):
    """Highest OI strike per (symbol, expiry, type) on the given date.
       Uses SUM inside a subquery to collapse any duplicate rows for that date."""
    q = f"""
    WITH deduped AS (
        SELECT {COLS['symbol']}, {COLS['expiry']}, {COLS['type']}, {COLS['strike']},
               SUM({COLS['oi']}) AS oi
        FROM {TABLE}
        WHERE {COLS['asof']}=?
        GROUP BY {COLS['symbol']}, {COLS['expiry']}, {COLS['type']}, {COLS['strike']}
    )
    SELECT symbol, expiration, type, MAX(oi) AS max_oi
    FROM deduped
    GROUP BY symbol, expiration, type
    """
    return {(r[0], r[1], r[2]): r[3] for r in conn.execute(q, (asof,)).fetchall()}


def run_large_oi_scanner(min_abs=1000, min_pct=15, max_dte=60):
    conn = get_conn()
    try:
        latest, prev = get_latest_two_asof(conn)
    except RuntimeError as e:
        conn.close()
        return []

    # ── Core query: SUM inside each CTE to deduplicate same-day rows ──
    q = f"""
    WITH latest AS (
        SELECT {COLS['symbol']}, {COLS['expiry']}, {COLS['strike']}, {COLS['type']},
               SUM({COLS['oi']})        AS oi_now,
               SUM(COALESCE(volume,0))  AS vol_now
        FROM {TABLE}
        WHERE {COLS['asof']}=?
        GROUP BY {COLS['symbol']}, {COLS['expiry']}, {COLS['strike']}, {COLS['type']}
    ),
    prev AS (
        SELECT {COLS['symbol']}, {COLS['expiry']}, {COLS['strike']}, {COLS['type']},
               SUM({COLS['oi']})        AS oi_prev,
               SUM(COALESCE(volume,0))  AS vol_prev
        FROM {TABLE}
        WHERE {COLS['asof']}=?
        GROUP BY {COLS['symbol']}, {COLS['expiry']}, {COLS['strike']}, {COLS['type']}
    )
    SELECT l.{COLS['symbol']}, l.{COLS['expiry']}, l.{COLS['strike']}, l.{COLS['type']},
           COALESCE(p.oi_prev, 0)  AS oi_prev,
           l.oi_now,
           (l.oi_now - COALESCE(p.oi_prev, 0)) AS diff,
           CASE WHEN COALESCE(p.oi_prev,0)=0 THEN NULL
                ELSE ROUND(100.0*(l.oi_now - p.oi_prev)/p.oi_prev, 2)
           END AS pct,
           l.vol_now,
           COALESCE(p.vol_prev, 0) AS vol_prev
    FROM latest l
    LEFT JOIN prev p
      ON  l.{COLS['symbol']} = p.{COLS['symbol']}
      AND l.{COLS['expiry']}  = p.{COLS['expiry']}
      AND l.{COLS['strike']}  = p.{COLS['strike']}
      AND l.{COLS['type']}    = p.{COLS['type']}
    WHERE l.oi_now > 200 AND COALESCE(p.oi_prev,0) > 200
    """
    rows = conn.execute(q, (latest, prev)).fetchall()

    # filter by thresholds and DTE
    filtered = []
    for r in rows:
        sym, expiry, strike, typ, oi_prev, oi_now, diff, pct, vol_now, vol_prev = r
        dte = _dte(expiry)
        if dte > max_dte or dte < 0:
            continue
        if not (abs(diff) >= min_abs or (pct and abs(pct) >= min_pct)):
            continue
        filtered.append((sym, expiry, strike, typ, oi_prev, oi_now, diff, pct, vol_now, vol_prev, dte))

    if not filtered:
        conn.close()
        return []

    max_oi_map = _get_max_oi_per_chain(conn, latest)
    conn.close()

    # fetch real spot prices in batch (cached)
    spot_cache = {}
    for sym, *_ in filtered:
        if sym not in spot_cache:
            spot_cache[sym] = get_spot(sym)

    result = []
    for sym, expiry, strike, typ, oi_prev, oi_now, diff, pct, vol_now, vol_prev, dte in filtered:
        spot = spot_cache.get(sym)

        exp_type = _expiry_type(dte)
        is_wall  = (oi_now == max_oi_map.get((sym, expiry, typ), -1))

        sr_label = ""
        if spot is not None:
            sf, t = float(strike), typ.upper()
            if t.startswith("P"):
                sr_label = "Put Support" if sf <= spot else "Put Ceiling (ITM)"
            else:
                sr_label = "Call Resistance" if sf >= spot else "Call Floor (ITM)"

        vol_confirmed = bool(vol_now > 0 and vol_now > (vol_prev or 0) * 1.2)

        score_size = min(25, int(abs(diff) / 400))
        score_pct  = min(25, int(abs(pct or 0) / 2))
        score_dte  = 25 if dte <= 7 else (20 if dte <= 14 else (15 if dte <= 35 else 8))
        score_qual = (10 if is_wall else 0) + (8 if vol_confirmed else 0)
        signal_score = min(100, score_size + score_pct + score_dte + score_qual)

        result.append({
            "symbol":        sym,
            "expiry":        expiry,
            "expiry_type":   exp_type,
            "dte":           dte,
            "strike":        float(strike),
            "type":          typ.upper(),
            "oi_prev":       int(oi_prev),
            "oi_now":        int(oi_now),
            "oi_change":     int(diff),
            "pct_change":    round(pct, 2) if pct else None,
            "direction":     "OI Buildup ▲" if diff > 0 else "OI Unwind ▼",
            "is_wall":       is_wall,
            "sr_label":      sr_label,
            "vol_confirmed": vol_confirmed,
            "signal_score":  signal_score,
            "spot":          round(spot, 2) if spot else None,
        })

    result.sort(key=lambda x: (-x["signal_score"], x["dte"]))
    return result
