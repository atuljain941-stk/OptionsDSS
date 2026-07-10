from datetime import datetime
from ..db import _connect

# ---------- ADD ----------
def add_trade(entry_date,symbol, trade_type, quantity, expiry,
              long_strike,short_strike=None, entry_price=0,  notes=""):

    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO trades
    (symbol, trade_type, quantity, expiry,
     long_strike, short_strike, entry_price,
     entry_date, entry_reason)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        symbol.upper(),
        trade_type,
        quantity,
        expiry,
        long_strike,
        short_strike,
        entry_price,
        entry_date,
        notes
    ))

    conn.commit()
    conn.close()


# ---------- UPDATE ----------
def update_trade(trade_id, **fields):
    conn = _connect()
    cur = conn.cursor()

    updates = []
    values = []

    for key, val in fields.items():
        updates.append(f"{key} = ?")
        values.append(val)

    values.append(trade_id)

    sql = f"""
    UPDATE trades SET {', '.join(updates)}
    WHERE id = ?
    """

    cur.execute(sql, values)
    conn.commit()
    conn.close()


# ---------- CLOSE ----------
def close_trade(trade_id, exit_price):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    SELECT trade_type, entry_price, quantity
    FROM trades WHERE id = ?
    """, (trade_id,))
    row = cur.fetchone()

    if not row:
        raise ValueError("Trade not found")

    trade_type, entry_price, qty = row

    if trade_type in ("PS", "CS"):
        pnl = (entry_price - exit_price) * qty * 100
    else:
        pnl = (exit_price - entry_price) * qty * 100

    cur.execute("""
    UPDATE trades
    SET exit_price = ?,
        exit_date = ?,
        pnl = ?,
        status = 'CLOSED'
    WHERE id = ?
    """, (
        exit_price,
        datetime.now().isoformat(),
        pnl,
        trade_id
    ))

    conn.commit()
    conn.close()


# ---------- FETCH ----------
def get_trades(status=None):
    conn = _connect()
    cur = conn.cursor()

    if status:
        cur.execute("SELECT id ,entry_date ,symbol, expiry,trade_type,long_strike,short_strike,entry_price,quantity,entry_reason,entry_oi, current_oi,risk_amt,reward_amt,status, exit_price , exit_date,pnl,outlook,suggested_action,exit_reason FROM trades WHERE status = ?", (status,))
    else:
        cur.execute("select id ,entry_date ,symbol, expiry,trade_type,long_strike,short_strike,entry_price,quantity,entry_reason,entry_oi, current_oi,risk_amt,reward_amt,status, exit_price , exit_date,pnl,outlook,suggested_action,exit_reason FROM trades")

    rows = cur.fetchall()
    conn.close()
    return rows
