# oiapp/services/oi_wall_service.py
"""
OI Wall Analysis Service.
Implements the core principle: OI walls hold in favor of the SELLER
until price breaks through WITH volume + momentum (seller unwind/roll signal).

Key logic:
  - OI at a strike = open contracts between buyers and sellers
  - HIGH put OI below spot = sellers wrote puts there = acts as SUPPORT (seller defends)
  - HIGH call OI above spot = sellers wrote calls there = acts as RESISTANCE (seller defends)
  - If price closes THROUGH a wall with SIGNIFICANT volume → sellers unwinding/rolling
    → That level is now BROKEN, flip bias
  - "Significant" = price move > 0.5% through the level AND volume > 1.5× average
"""
import math
from datetime import date, datetime, timedelta


def get_strike_oi_days_ago(symbol, expiry, strike, opt_type, days_back=7, con=None):
    """OI at one specific strike from ~days_back days ago -- the closest
    available snapshot on or before that date. This is deliberately
    separate from get_oi_walls()'s own delta_oi, which only ever compares
    the two MOST RECENT snapshot dates (effectively yesterday vs today,
    whatever those happen to be) -- not useful for "how much has this
    strike built up over the last week specifically." Returns None if no
    snapshot exists that far back (e.g. the strike didn't exist / wasn't
    being tracked yet).
    """
    if con is None:
        import sqlite3
        from ..config import DB_PATH as _OIAPP_DB_PATH
        con = sqlite3.connect(_OIAPP_DB_PATH); con.row_factory = sqlite3.Row; local = True
    else:
        local = False
    try:
        target_date = (date.today() - timedelta(days=days_back)).isoformat()
        q = "SELECT oi FROM options WHERE symbol=? AND strike=? AND type=? AND date<=?"
        params = [symbol, strike, opt_type, target_date]
        if expiry:
            q += " AND expiration=?"
            params.append(expiry)
        q += " ORDER BY date DESC LIMIT 1"
        row = con.execute(q, params).fetchone()
        return int(row["oi"]) if row and row["oi"] is not None else None
    finally:
        if local:
            con.close()

def get_oi_walls(symbol, expiry=None, con=None):
    """
    Return put support walls and call resistance walls from DB.
    Returns: {
      'put_walls': [{strike, oi, delta_oi, strength, pct_from_spot},...],
      'call_walls': [{strike, oi, delta_oi, strength, pct_from_spot},...],
      'gamma_wall': float,
      'pcr': float,
    }
    """
    if con is None:
        import sqlite3
        from pathlib import Path
        from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
        DB_PATH = _OIAPP_DB_PATH
        con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row; local = True
    else:
        local = False

    try:
        # Latest snapshot
        dates = [r["date"] for r in con.execute(
            "SELECT DISTINCT date FROM options WHERE symbol=? ORDER BY date DESC LIMIT 2",
            (symbol,)).fetchall()]
        if not dates: return None
        d1 = dates[0]; d2 = dates[1] if len(dates)>1 else d1

        q = "SELECT type,strike,SUM(oi) as oi FROM options WHERE symbol=? AND date=?"
        if expiry: q += f" AND expiration='{expiry}'"
        q += " GROUP BY type,strike ORDER BY strike"
        rows1 = con.execute(q, (symbol, d1)).fetchall()
        rows2 = {(r["type"],float(r["strike"])): r["oi"]
                 for r in con.execute(q, (symbol, d2)).fetchall()} if d2!=d1 else {}

        put_walls=[]; call_walls=[]
        total_put_oi=0; total_call_oi=0
        for r in rows1:
            s=float(r["strike"]); oi=int(r["oi"] or 0)
            prior=rows2.get((r["type"],s), oi)
            d_oi = oi - prior
            if r["type"]=="put":
                total_put_oi+=oi
                if oi>=500:
                    put_walls.append({"strike":s,"oi":oi,"delta_oi":d_oi})
            else:
                total_call_oi+=oi
                if oi>=500:
                    call_walls.append({"strike":s,"oi":oi,"delta_oi":d_oi})

        pcr = round(total_put_oi/total_call_oi,2) if total_call_oi else 1.0
        # Sort by OI descending
        put_walls.sort(key=lambda x: -x["oi"])
        call_walls.sort(key=lambda x: -x["oi"])
        # Gamma wall = highest combined OI
        all_walls = [(w["strike"], w["oi"]) for w in put_walls+call_walls]
        gamma_wall = max(all_walls, key=lambda x:x[1])[0] if all_walls else 0
        return {"put_walls":put_walls[:6],"call_walls":call_walls[:6],
                "gamma_wall":gamma_wall,"pcr":pcr,
                "total_put_oi":total_put_oi,"total_call_oi":total_call_oi}
    finally:
        if local: con.close()


def check_wall_breach(symbol, wall_strike, wall_type, lookback_days=3):
    """
    Check if a wall has been BREACHED by price with volume+momentum.
    wall_type: 'put' (support wall below spot) or 'call' (resistance wall above spot)

    Returns:
      'HOLDING'  — wall intact, sellers still defending
      'BREACHED' — price closed through with volume momentum
      'TESTING'  — price is at the wall level but hasn't confirmed close
    """
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="10d", interval="1d")
        if df.empty or len(df) < 2: return "UNKNOWN"
        closes = df["Close"].tolist()
        vols   = df["Volume"].tolist()
        opens  = df["Open"].tolist()
        n = len(closes)-1
        # Volume average
        avg_vol = sum(vols[max(0,n-20):n]) / min(20, n) if n > 0 else 1

        for i in range(max(0, n-lookback_days), n+1):
            c = closes[i]; o = opens[i]; v = vols[i]
            price_move_pct = abs(c - o) / o * 100 if o > 0 else 0
            vol_ratio = v / avg_vol if avg_vol > 0 else 1

            if wall_type == "put":   # support wall — breach = close BELOW
                if c < wall_strike * 0.995:  # 0.5% through
                    if vol_ratio >= 1.5 and price_move_pct >= 0.5:
                        return "BREACHED"
                    elif c < wall_strike:
                        return "TESTING"
            else:  # call wall — resistance wall — breach = close ABOVE
                if c > wall_strike * 1.005:
                    if vol_ratio >= 1.5 and price_move_pct >= 0.5:
                        return "BREACHED"
                    elif c > wall_strike:
                        return "TESTING"
        return "HOLDING"
    except: return "UNKNOWN"


def oi_wall_context(symbol, spot, expiry=None, con=None):
    """
    Full OI wall context for a symbol:
    - Nearest support wall below spot + its breach status
    - Nearest resistance wall above spot + its breach status
    - Bias based on wall positions and breach status
    """
    walls = get_oi_walls(symbol, expiry, con)
    if not walls: return {"bias":"NEUTRAL","walls":None,"breach_context":"No OI data"}

    put_walls  = walls["put_walls"]
    call_walls = walls["call_walls"]

    # Nearest walls relative to spot
    support   = [w for w in put_walls  if w["strike"] < spot]
    resist    = [w for w in call_walls if w["strike"] > spot]
    support.sort(key=lambda x: -x["strike"])  # nearest first (closest below)
    resist.sort(key=lambda x:  x["strike"])   # nearest first (closest above)

    result = {"put_walls":put_walls,"call_walls":call_walls,
              "pcr":walls["pcr"],"gamma_wall":walls["gamma_wall"]}

    # Check nearest wall status
    sup_breach   = "NONE"
    res_breach   = "NONE"
    sup_strike   = resist_strike = None

    if support:
        sup_strike = support[0]["strike"]
        sup_breach = check_wall_breach(symbol, sup_strike, "put")
        result["nearest_support"] = {**support[0], "breach_status": sup_breach}

    if resist:
        resist_strike = resist[0]["strike"]
        res_breach  = check_wall_breach(symbol, resist_strike, "call")
        result["nearest_resistance"] = {**resist[0], "breach_status": res_breach}

    # Determine bias
    if sup_breach == "BREACHED" and res_breach != "BREACHED":
        bias = "BEARISH"
        context = f"Support wall at ${sup_strike} BREACHED with volume — sellers unwinding/rolling. Downside momentum."
    elif res_breach == "BREACHED" and sup_breach != "BREACHED":
        bias = "BULLISH"
        context = f"Resistance wall at ${resist_strike} BREACHED with volume — short sellers covering. Upside momentum."
    elif sup_breach == "TESTING":
        bias = "MILDLY_BEARISH"
        context = f"Testing support wall at ${sup_strike} — watch for confirmed close below."
    elif res_breach == "TESTING":
        bias = "MILDLY_BULLISH"
        context = f"Testing resistance wall at ${resist_strike} — watch for confirmed close above."
    else:
        # Both holding — price contained
        if walls["pcr"] > 1.3:
            bias = "BEARISH_LEAN"
            context = f"PCR {walls['pcr']} — heavy put hedging. Walls holding."
        elif walls["pcr"] < 0.7:
            bias = "BULLISH_LEAN"
            context = f"PCR {walls['pcr']} — call activity dominant. Walls holding."
        else:
            bias = "NEUTRAL"
            context = f"Price contained between walls. PCR {walls['pcr']}."

    result["bias"] = bias
    result["breach_context"] = context
    return result
