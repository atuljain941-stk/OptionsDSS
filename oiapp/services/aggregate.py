import sqlite3
from datetime import date
from ..db import DB_PATH, get_expirations_for_symbol
from .market import get_spot, select_strikes_around_atm

try:
    from .weekly_oi import aggregate_strike_payload
except Exception:  # pragma: no cover
    aggregate_strike_payload = None


def _future_exps(symbol):
    today = date.today().strftime('%Y-%m-%d')
    return [e for e in get_expirations_for_symbol(symbol) if e >= today]


def _latest_day_for(sym, exp):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT MAX(date) FROM options WHERE symbol=? AND expiration=?", (sym, exp))
    row = cur.fetchone()
    con.close()
    return row[0] if row and row[0] else None


def _legacy_get_aggregate_strike(symbol: str, from_expiration: str | None, count: int, per_side: int, min_change_pct: float | None = None):
    exps = _future_exps(symbol)
    if not exps:
        return {"symbol": symbol, "expirations": [], "strikes": [], "call_sum": [], "put_sum": [],
                "call_vol_sum": [], "put_vol_sum": [], "message": "No future expiry data — run Scheduler"}

    if from_expiration and from_expiration in exps:
        i = exps.index(from_expiration)
        exps = exps[i:i+count]
    else:
        exps = exps[:count]

    spot = get_spot(symbol)
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT DISTINCT strike FROM options WHERE symbol=? AND expiration=? ORDER BY strike",
                (symbol, exps[0]))
    all_strikes = [float(r[0]) for r in cur.fetchall()]
    con.close()
    strikes = select_strikes_around_atm(all_strikes, spot, per_side)

    from collections import defaultdict
    call_oi = defaultdict(int); put_oi = defaultdict(int)
    call_vol = defaultdict(int); put_vol = defaultdict(int)

    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    for exp in exps:
        day = _latest_day_for(symbol, exp)
        if not day: continue
        cur.execute("""
            SELECT type, strike, SUM(oi), SUM(volume)
            FROM options
            WHERE symbol=? AND expiration=? AND date=?
            GROUP BY type, strike
            """, (symbol, exp, day))
        for typ, strike, soi, svol in cur.fetchall():
            strike = float(strike)
            if strike not in strikes:
                continue
            if typ == "call":
                call_oi[strike] += int(soi or 0)
                call_vol[strike] += int(svol or 0)
            else:
                put_oi[strike] += int(soi or 0)
                put_vol[strike] += int(svol or 0)
    con.close()

    return {
        "symbol": symbol,
        "spot": spot,
        "expirations": exps,
        "strikes": strikes,
        "call_sum": [call_oi[s] for s in strikes],
        "put_sum": [put_oi[s] for s in strikes],
        "call_vol_sum": [call_vol[s] for s in strikes],
        "put_vol_sum": [put_vol[s] for s in strikes],
        "source": "legacy_aggregate",
    }


def get_aggregate_strike(symbol: str, from_expiration: str | None, count: int, per_side: int, min_change_pct: float | None = None):
    """Aggregate screen source of truth.

    Weekly Plan and AI Hub now use the same aggregation utility so displayed OI
    walls and strategy-selection walls cannot drift apart.
    """
    if aggregate_strike_payload is not None:
        try:
            return aggregate_strike_payload(symbol, from_expiration, count, per_side, min_change_pct=min_change_pct)
        except Exception as exc:
            legacy = _legacy_get_aggregate_strike(symbol, from_expiration, count, per_side, min_change_pct=min_change_pct)
            legacy["warning"] = f"weekly_oi aggregate fallback failed: {exc}"
            return legacy
    return _legacy_get_aggregate_strike(symbol, from_expiration, count, per_side, min_change_pct=min_change_pct)


def get_pcr_snapshot(symbol: str):
    exps = _future_exps(symbol)
    out = []
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    for exp in exps:
        cur.execute("SELECT MAX(date) FROM options WHERE symbol=? AND expiration=?", (symbol, exp))
        day = cur.fetchone()[0]
        if not day: continue
        cur.execute("""
            SELECT type, SUM(oi) FROM options
            WHERE symbol=? AND expiration=? AND date=?
            GROUP BY type
            """, (symbol, exp, day))
        row = dict(cur.fetchall())
        puts = int(row.get("put", 0)); calls = int(row.get("call", 0))
        pcr = round(puts / calls, 2) if calls > 0 else None
        out.append({"expiration": exp, "puts": puts, "calls": calls, "pcr": pcr})
    con.close()
    return out
