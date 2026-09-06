import sqlite3
from ..db import DB_PATH

def get_table_view(table: str, limit: int = 100, symbol: str | None = None, expiration: str | None = None):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    base = f"SELECT * FROM {table}"
    clauses, params = [], []
    if table == "options":
        if symbol:
            clauses.append("symbol=?"); params.append(symbol)
        if expiration:
            clauses.append("expiration=?"); params.append(expiration)
    if clauses:
        base += " WHERE " + " AND ".join(clauses)
    base += " ORDER BY date DESC LIMIT ?"
    params.append(limit)

    try:
        cur.execute(base, params)
        rows = [dict(r) for r in cur.fetchall()]
        cols = [c[0] for c in cur.description]
    except Exception as e:
        cols, rows = ["error"], [{"message": str(e)}]
    finally:
        con.close()
    return {"columns": cols, "rows": rows}
