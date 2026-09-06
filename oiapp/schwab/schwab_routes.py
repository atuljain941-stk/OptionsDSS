"""Schwab API Integration."""
import time
from flask import Blueprint, jsonify, request
from datetime import datetime, date
from ..db import _connect

schwab_bp = Blueprint("schwab", __name__)

DB_TABLE = """CREATE TABLE IF NOT EXISTS schwab_config (
    id INTEGER PRIMARY KEY, app_key TEXT, app_secret TEXT,
    access_token TEXT, refresh_token TEXT, token_expiry TEXT,
    account_hash TEXT, updated TEXT)"""

def _ensure_table():
    con = _connect(); con.execute(DB_TABLE); con.commit(); con.close()

def _get_config():
    _ensure_table()
    con = _connect(); con.row_factory = __import__('sqlite3').Row
    row = con.execute("SELECT * FROM schwab_config WHERE id=1").fetchone()
    con.close()
    return dict(row) if row else None

def _save_config(**kw):
    _ensure_table(); con = _connect()
    if con.execute("SELECT id FROM schwab_config WHERE id=1").fetchone():
        sets = ", ".join(f"{k}=?" for k in kw)
        con.execute(f"UPDATE schwab_config SET {sets}, updated=? WHERE id=1",
                    (*kw.values(), datetime.now().isoformat()))
    else:
        cols = ", ".join(kw.keys()) + ", updated"
        vals = ", ".join("?" * (len(kw)+1))
        con.execute(f"INSERT INTO schwab_config (id, {cols}) VALUES (1, {vals})",
                    (*kw.values(), datetime.now().isoformat()))
    con.commit(); con.close()

def _headers():
    cfg = _get_config()
    if not cfg or not cfg.get("access_token"): return None
    return {"Authorization": f"Bearer {cfg['access_token']}", "Content-Type": "application/json"}

BASE = "https://api.schwabapi.com"

def _api_get(url, params=None):
    import requests
    h = _headers()
    if not h: return {"error": "Not authenticated"}, 401
    r = requests.get(url, headers=h, params=params)
    if r.status_code == 401: return {"error": "Token expired. Click Refresh Token."}, 401
    try: return r.json(), r.status_code
    except: return {"error": r.text}, r.status_code

@schwab_bp.route("/schwab/config", methods=["GET"])
def get_config_route():
    cfg = _get_config()
    if cfg:
        safe = {k: (v[:8]+"..." if k in ("app_secret","access_token","refresh_token") and v and len(v)>8 else v)
                for k, v in cfg.items() if k != "id"}
        try:
            from ..services.futures_oi_schwab import schwab_auth_status
            safe.update(schwab_auth_status())
        except Exception:
            pass
        return jsonify(safe)
    return jsonify({"configured": False})

@schwab_bp.route("/schwab/access_token_reveal")
def reveal_access_token():
    """Deliberately returns the FULL (not truncated) access token --
    for pasting into a manual curl command when the automated
    account_hash fetch fails for some reason, so you're never fully
    blocked by a bug/outage in this app's own proxy code. Sensitive --
    treat the output like a password, don't share it."""
    cfg = _get_config()
    if not cfg or not cfg.get("access_token"):
        return jsonify({"ok": False, "error": "not connected -- no access_token stored"}), 400
    return jsonify({"ok": True, "access_token": cfg["access_token"]})

@schwab_bp.route("/schwab/config", methods=["POST"])
def save_config_route():
    d = request.get_json() or {}
    # TRUE partial update -- only touch fields the caller actually
    # sent. The old version always overwrote app_key/app_secret/
    # account_hash on every call, defaulting anything missing from the
    # request to an empty string -- so saving JUST the account_hash
    # (e.g. from the Fetch Accounts flow) silently wiped the app_key
    # and app_secret that were already stored. That was a real bug,
    # not intentional partial-update behavior.
    updates = {k: d[k] for k in ("app_key", "app_secret", "account_hash") if k in d}
    if not updates:
        return jsonify({"ok": False, "error": "no fields provided"}), 400
    _save_config(**updates)
    return jsonify({"ok": True})

@schwab_bp.route("/schwab/token", methods=["POST"])
def save_token():
    d = request.get_json()
    _save_config(access_token=d.get("access_token",""), refresh_token=d.get("refresh_token",""),
                 token_expiry=d.get("token_expiry",""))
    return jsonify({"ok": True})

@schwab_bp.route("/schwab/auth_url")
def auth_url():
    cfg = _get_config()
    if not cfg or not cfg.get("app_key"): return jsonify({"error": "Configure app_key first"}), 400
    url = f"https://api.schwabapi.com/v1/oauth/authorize?client_id={cfg['app_key']}&redirect_uri=https://127.0.0.1&response_type=code"
    return jsonify({"url": url})

@schwab_bp.route("/schwab/exchange_code", methods=["POST"])
def exchange_code():
    import requests as req, base64
    d = request.get_json(); cfg = _get_config()
    if not cfg: return jsonify({"error": "Not configured"}), 400
    creds = base64.b64encode(f"{cfg['app_key']}:{cfg['app_secret']}".encode()).decode()
    r = req.post(f"{BASE}/v1/oauth/token",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code", "code": d.get("code",""), "redirect_uri": "https://127.0.0.1"})
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = (r.text or "")[:500]
        return jsonify({
            "error": f"Schwab authorization failed: {r.status_code}",
            "detail": detail,
        }), 400
    t = r.json()
    _save_config(access_token=t.get("access_token",""), refresh_token=t.get("refresh_token",""),
                 token_expiry=str(time.time() + t.get("expires_in", 1800)))
    return jsonify({"ok": True})

@schwab_bp.route("/schwab/refresh", methods=["POST"])
def refresh_token():
    try:
        from ..services.futures_oi_schwab import refresh_schwab_access_token
        res = refresh_schwab_access_token()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Refresh failed: {e}", "reauthorize_required": True}), 400
    if not res.get("ok"):
        code = 401 if res.get("reauthorize_required") else 400
        return jsonify(res), code
    return jsonify(res)

@schwab_bp.route("/schwab/quote/<symbol>")
def get_quote(symbol):
    data, code = _api_get(f"{BASE}/marketdata/v1/quotes", {"symbols": symbol.upper(), "fields": "quote"})
    return jsonify(data), code

@schwab_bp.route("/schwab/options/<symbol>")
def get_options_chain(symbol):
    params = {"symbol": symbol.upper(), "contractType": request.args.get("type","ALL"),
              "strikeCount": request.args.get("strikes","20"), "includeUnderlyingQuote": "true"}
    if request.args.get("expiry"): params["fromDate"] = params["toDate"] = request.args.get("expiry")
    data, code = _api_get(f"{BASE}/marketdata/v1/chains", params)
    return jsonify(data), code

@schwab_bp.route("/schwab/accounts")
def get_accounts():
    data, code = _api_get(f"{BASE}/trader/v1/accounts", {"fields": "positions"})
    return jsonify(data), code

@schwab_bp.route("/schwab/account_numbers")
def get_account_numbers():
    """Schwab's dedicated endpoint for mapping plain account numbers to
    the encrypted account_hash trading calls actually need -- lets the
    UI offer a pick-and-save flow instead of requiring the user to
    somehow already know/paste an opaque encrypted string. Requires
    OAuth to already be connected (same as any other Schwab call)."""
    data, code = _api_get(f"{BASE}/trader/v1/accounts/accountNumbers")
    return jsonify(data), code

@schwab_bp.route("/schwab/positions")
def get_positions():
    cfg = _get_config(); acct = cfg.get("account_hash","") if cfg else ""
    if not acct: return jsonify({"error": "No account_hash"}), 400
    data, code = _api_get(f"{BASE}/trader/v1/accounts/{acct}", {"fields": "positions"})
    if code == 200:
        pos = data.get("securitiesAccount",{}).get("positions",[])
        return jsonify({"positions": pos})
    return jsonify(data), code

@schwab_bp.route("/schwab/orders")
def get_orders():
    cfg = _get_config(); acct = cfg.get("account_hash","") if cfg else ""
    if not acct: return jsonify({"error": "No account_hash"}), 400
    data, code = _api_get(f"{BASE}/trader/v1/accounts/{acct}/orders",
        {"fromEnteredTime": request.args.get("from", date.today().replace(day=1).isoformat())})
    return jsonify(data), code

@schwab_bp.route("/schwab/order", methods=["POST"])
def place_order():
    import requests as req
    h = _headers(); cfg = _get_config()
    if not h: return jsonify({"error": "Not authenticated"}), 401
    acct = cfg.get("account_hash","")
    r = req.post(f"{BASE}/trader/v1/accounts/{acct}/orders", headers=h, json=request.get_json())
    if r.status_code in (200, 201):
        return jsonify({"ok": True, "order_id": r.headers.get("Location","").split("/")[-1]})
    return jsonify({"error": f"Order failed: {r.status_code}", "detail": r.text}), r.status_code

@schwab_bp.route("/schwab/order/<order_id>", methods=["DELETE"])
def cancel_order(order_id):
    import requests as req
    h = _headers(); cfg = _get_config()
    if not h: return jsonify({"error": "Not authenticated"}), 401
    acct = cfg.get("account_hash","")
    r = req.delete(f"{BASE}/trader/v1/accounts/{acct}/orders/{order_id}", headers=h)
    return jsonify({"ok": r.status_code in (200,204)})

@schwab_bp.route("/schwab/journal_trades")
def schwab_journal_trades():
    con = _connect(); con.row_factory = __import__('sqlite3').Row
    try:
        rows = con.execute("SELECT * FROM trades WHERE schwab_order_id IS NOT NULL AND schwab_order_id != '' ORDER BY entry_date DESC").fetchall()
    except: rows = []
    con.close()
    return jsonify([dict(r) for r in rows])

@schwab_bp.route("/schwab/order_to_journal", methods=["POST"])
def schwab_to_journal():
    import json as _json
    d = request.get_json()
    legs = d.get("legs", [])
    net_premium = sum((float(l.get("price",0))*int(l.get("qty",1))*(1 if l.get("option_type")=="stock" else 100)*(1 if l.get("side")=="sell" else -1)) for l in legs)
    first_opt = next((l for l in legs if l.get("option_type")!="stock"), legs[0] if legs else {})
    qty = int(first_opt.get("qty", 1))
    per_contract = abs(sum((1 if l.get("side")=="sell" else -1)*float(l.get("price",0)) for l in legs if l.get("option_type")!="stock")) if legs else 0
    con = _connect()
    try:
        con.execute("""INSERT INTO trades (entry_date,symbol,expiry,trade_type,entry_price,quantity,
            entry_reason,status,legs_json,num_legs,net_premium,long_strike,short_strike,
            schwab_order_id,schwab_status) VALUES (?,?,?,?,?,?,?,'OPEN',?,?,?,0,NULL,?,?)""",
            (d.get("entry_date", date.today().isoformat()), d.get("symbol","").upper(),
             d.get("expiry", first_opt.get("expiry","")), d.get("trade_type","CS"),
             per_contract, qty, d.get("entry_reason", "[SCHWAB]"),
             _json.dumps(legs), len(legs), round(net_premium, 4),
             d.get("schwab_order_id",""), d.get("schwab_status","FILLED")))
        con.commit()
        tid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    except Exception as e:
        con.close(); return jsonify({"error": str(e)}), 500
    con.close()
    return jsonify({"ok": True, "trade_id": tid})
