import sqlite3
import yfinance as yf
from ..db import _connect

# Connect to your SQLite database
conn = _connect()
cur = conn.cursor()

# Function to get current stock price
def get_stock_price(symbol):
    ticker = yf.Ticker(symbol)
    price = ticker.history(period="1d")['Close'].iloc[-1]
    return price

# Function to get current OI for a specific option
def get_current_oi(symbol, strike, expiry, option_type="CS"):
    try:
        from ..services.option_prices import fetch_chain
        opt_type = "call" if str(option_type).lower() in ("call", "cs", "cb", "c") else "put"
        chain = fetch_chain(str(symbol).upper(), expiry, prefer_live=True, db_first=False)
        if not chain:
            return None
        st = float(strike)
        for k in (round(st, 4), round(st, 2), round(st), round(st * 2) / 2):
            data = chain.get((opt_type, k)) or {}
            if data.get("oi") is not None:
                return float(data.get("oi") or 0)
    except Exception:
        return None
    return None

# Function to determine outlook
# Mirrors the journal page rules so cached scheduler data and live refresh match.
def determine_outlook(long_strike, short_strike, current_price, option_type="call", dte=None, rolling_days=5):
    try:
        long_strike = float(long_strike)
        short_strike = float(short_strike)
        current_price = float(current_price)
    except Exception:
        return "UNKNOWN"

    opt = (option_type or "").lower()
    lo = min(long_strike, short_strike)
    hi = max(long_strike, short_strike)

    if opt in ("cs", "ps"):  # credit spreads
        if opt == "cs":
            if current_price < lo:
                return "PROJECTED WINNER"
            if current_price > hi:
                return "PROJECTED LOSER"
            if lo <= current_price < hi:
                return "ITM"
            return "NEUTRAL"
        else:
            if current_price > hi:
                return "PROJECTED WINNER"
            if current_price < lo:
                return "PROJECTED LOSER" if (dte is not None and dte <= rolling_days) else "OTM"
            if lo < current_price <= hi:
                return "ITM"
            return "NEUTRAL"
    elif opt in ("pb", "cb"):  # debit spreads
        if opt == "pb":
            if current_price < hi:
                return "PROJECTED WINNER"
            if current_price > lo:
                return "PROJECTED LOSER"
            if lo < current_price < hi:
                return "ITM"
            return "NEUTRAL"
        else:
            if current_price > lo:
                return "PROJECTED WINNER"
            if current_price < hi:
                return "PROJECTED LOSER"
            if lo < current_price < hi:
                return "ITM"
            return "NEUTRAL"
    return "UNKNOWN"


def update_journal():
# Fetch all open trades
  cur.execute("SELECT id, symbol, short_strike,long_strike, expiry, trade_type FROM trades WHERE status='OPEN'")
  trades = cur.fetchall()

  for trade in trades:
      trade_id, symbol, short_strike, long_strike,expiry, trade_type = trade
      if trade_type =="PS" or trade_type=="CS" :
          strike = short_strike  # skip trades without short strike
      else:
          strike = long_strike

      option_type = "call" if "C" in trade_type else "put"  # simple mapping, adjust if needed

      current_price = get_stock_price(symbol)
      current_oi = get_current_oi(symbol, strike, expiry, option_type)
      outlook = determine_outlook(long_strike,short_strike, current_price, option_type)
      suggested_action = "Review" if outlook == "Projected Looser" else "Shoot for full profit"

      # Update the SQLite table
      cur.execute("""
          UPDATE trades
          SET current_oi = ?, outlook = ?, suggested_action = ?
          WHERE id = ?
      """, (current_oi, outlook, suggested_action, trade_id))

  conn.commit()
  conn.close()
  print("Trading journal updated in SQLite!")
