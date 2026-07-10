"""
India Market OI Service — NIFTY / BANKNIFTY / SENSEX / FINNIFTY
Uses curl_cffi for browser-grade TLS fingerprinting to pass NSE bot protection.
Falls back to requests if curl_cffi unavailable.
"""
import time, json as _json
from datetime import datetime, date
from ..db import _connect

INDIA_SYMBOLS = {
    "NIFTY":      {"lot_size": 75,  "expiry": "weekly_thu",  "strike_gap": 50,  "desc": "Nifty 50"},
    "BANKNIFTY":  {"lot_size": 30,  "expiry": "monthly_thu", "strike_gap": 100, "desc": "Bank Nifty"},
    "FINNIFTY":   {"lot_size": 40,  "expiry": "weekly_tue",  "strike_gap": 50,  "desc": "Fin Nifty"},
    "MIDCPNIFTY": {"lot_size": 75,  "expiry": "monthly",     "strike_gap": 50,  "desc": "Midcap Nifty"},
    "SENSEX":     {"lot_size": 20,  "expiry": "weekly_fri",  "strike_gap": 100, "desc": "BSE Sensex"},
}

NSE_BASE = "https://www.nseindia.com"
_session = None
_session_ts = 0
_SESSION_TTL = 180  # seconds

def _make_session():
    """Build a browser-impersonating session using curl_cffi."""
    try:
        from curl_cffi import requests as cffi_req
        # Try each impersonation target
        for target in ["chrome120", "chrome116", "chrome110", "chrome107", "safari17_0", "firefox117"]:
            try:
                s = cffi_req.Session(impersonate=target)
                s._nse_using_cffi = True
                s._impersonate = target
                return s
            except Exception:
                continue
    except ImportError:
        pass
    # Fallback: regular requests with enhanced headers
    import requests
    s = requests.Session()
    s._nse_using_cffi = False
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    return s


def _nse_session():
    """Get or refresh NSE session with warm-up requests."""
    global _session, _session_ts
    now = time.time()
    if _session and (now - _session_ts) < _SESSION_TTL:
        return _session

    s = _make_session()
    using_cffi = getattr(s, '_nse_using_cffi', False)

    warmup_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }
    api_headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{NSE_BASE}/option-chain",
    }

    try:
        # Phase 1: homepage
        if using_cffi:
            r1 = s.get(f"{NSE_BASE}/", headers=warmup_headers, timeout=12)
        else:
            s.headers.update(warmup_headers)
            r1 = s.get(f"{NSE_BASE}/", timeout=12)
        time.sleep(1.0)

        # Phase 2: option-chain landing page (sets more cookies)
        if using_cffi:
            r2 = s.get(f"{NSE_BASE}/option-chain",
                       headers={**warmup_headers, "Referer": NSE_BASE + "/"}, timeout=12)
        else:
            s.headers["Referer"] = NSE_BASE + "/"
            r2 = s.get(f"{NSE_BASE}/option-chain", timeout=12)
        time.sleep(0.8)

        # Phase 3: switch to JSON API headers
        if not using_cffi:
            s.headers.update(api_headers)

        _session = s
        _session_ts = now
        cffi_info = f" (curl_cffi {getattr(s,'_impersonate','?')})" if using_cffi else " (requests fallback)"
        print(f"[india_oi] Session ready{cffi_info}, home={r1.status_code}, chain={r2.status_code}")
        return s

    except Exception as e:
        print(f"[india_oi] Session warmup error: {e}")
        _session = s
        _session_ts = now
        return s


def _nse_api_call(symbol, retries=3):
    """Call NSE option chain API with retries and session refresh."""
    global _session, _session_ts

    endpoint = f"{NSE_BASE}/api/option-chain-indices?symbol={symbol}"
    api_headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{NSE_BASE}/option-chain",
        "X-Requested-With": "XMLHttpRequest",
    }

    for attempt in range(retries):
        s = _nse_session()
        using_cffi = getattr(s, '_nse_using_cffi', False)
        try:
            if using_cffi:
                r = s.get(endpoint, headers=api_headers, timeout=18)
            else:
                s.headers.update(api_headers)
                r = s.get(endpoint, timeout=18)

            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    raise RuntimeError(f"Invalid JSON from NSE (body: {r.text[:100]})")

            if r.status_code in (401, 403):
                _session_ts = 0  # force refresh
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"NSE blocked request (HTTP {r.status_code}). Try: 1) Run from your local browser IP, 2) Disable VPN, 3) Open NSE website manually first.")

            raise RuntimeError(f"NSE returned HTTP {r.status_code}")

        except RuntimeError:
            raise
        except Exception as e:
            if attempt == retries - 1:
                raise RuntimeError(f"NSE connection failed: {e}")
            _session_ts = 0
            time.sleep(2)


def _parse_nse_chain(data, symbol):
    """Parse NSE option chain JSON → per-expiry dict."""
    records = data.get("records", {}).get("data", [])
    underlying = float(data.get("records", {}).get("underlyingValue", 0) or 0)
    expiry_data = {}

    for rec in records:
        raw = rec.get("expiryDate", "")
        if not raw: continue
        try:
            d = datetime.strptime(raw, "%d-%b-%Y").date()
            if d < date.today(): continue
            iso = d.isoformat()
        except: continue

        if iso not in expiry_data:
            expiry_data[iso] = {"calls": [], "puts": []}

        def _row(sub):
            return {
                "strikePrice":       rec.get("strikePrice", 0),
                "openInterest":      sub.get("openInterest", 0),
                "changeinOpenInterest": sub.get("changeinOpenInterest", 0),
                "totalTradedVolume": sub.get("totalTradedVolume", 0),
                "impliedVolatility": sub.get("impliedVolatility", 0),
                "lastPrice":         sub.get("lastPrice", 0),
            }

        if "CE" in rec and isinstance(rec["CE"], dict):
            expiry_data[iso]["calls"].append(_row(rec["CE"]))
        if "PE" in rec and isinstance(rec["PE"], dict):
            expiry_data[iso]["puts"].append(_row(rec["PE"]))

    return expiry_data, underlying


def _store(symbol, expiry, calls, puts, trade_date):
    """Write parsed OI rows into DB."""
    con = _connect(); saved = 0
    for opt_type, rows in [("call", calls), ("put", puts)]:
        for row in rows:
            k = row.get("strikePrice") or 0
            oi = int(row.get("openInterest") or 0)
            vol = int(row.get("totalTradedVolume") or 0)
            if not k: continue
            try:
                con.execute("""INSERT OR REPLACE INTO options
                    (symbol,expiration,type,strike,oi,volume,date) VALUES (?,?,?,?,?,?,?)""",
                    (symbol, expiry, opt_type, float(k), oi, vol, trade_date))
                saved += 1
            except: pass
    con.commit(); con.close()
    return saved


def fetch_and_store_india(symbol):
    """Fetch + store full option chain for one Indian index."""
    sym = symbol.upper().strip()
    if sym not in INDIA_SYMBOLS:
        return {"error": f"Unknown: {sym}. Use: {list(INDIA_SYMBOLS)}"}

    today = date.today().isoformat()
    result = {"symbol": sym, "rows_saved": 0, "expiries_stored": [], "spot": 0}

    data = _nse_api_call(sym)
    expiry_data, spot = _parse_nse_chain(data, sym)
    result["spot"] = spot

    for exp_iso, d in expiry_data.items():
        saved = _store(sym, exp_iso, d["calls"], d["puts"], today)
        result["rows_saved"] += saved
        result["expiries_stored"].append(exp_iso)

    result["expiries_stored"] = sorted(result["expiries_stored"])
    return result


def get_india_oi_summary(symbol, max_expiries=6):
    """OI analysis per expiry from DB."""
    sym = symbol.upper().strip()
    con = _connect()
    today = date.today().isoformat()
    exps = [r[0] for r in con.execute(
        "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration LIMIT ?",
        (sym, today, max_expiries)).fetchall()]

    if not exps:
        con.close()
        return {"symbol": sym, "expiries": [], "error": "No data — click Fetch first"}

    result = {"symbol": sym, "expiries": []}

    for exp in exps:
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM options WHERE symbol=? AND expiration=? ORDER BY date DESC LIMIT 10",
            (sym, exp)).fetchall()]
        if not dates: continue

        d0 = dates[0]; d1 = dates[1] if len(dates) > 1 else dates[0]

        def agg(dt):
            rows = con.execute("""SELECT type, SUM(oi), SUM(volume) FROM options
                WHERE symbol=? AND expiration=? AND date=? GROUP BY type""",
                (sym, exp, dt)).fetchall()
            return {r[0]: {"oi": r[1] or 0, "vol": r[2] or 0} for r in rows}

        a0 = agg(d0); a1 = agg(d1) if d1 != d0 else {}
        call_oi  = a0.get("call", {}).get("oi", 0)
        put_oi   = a0.get("put",  {}).get("oi", 0)
        cprev    = a1.get("call", {}).get("oi", call_oi) if a1 else call_oi
        pprev    = a1.get("put",  {}).get("oi", put_oi)  if a1 else put_oi
        total_oi = call_oi + put_oi
        oi_chg   = total_oi - (cprev + pprev)
        pcr      = round(put_oi / max(1, call_oi), 3)
        pcr_prev = round(pprev  / max(1, cprev),   3) if a1 else pcr

        top_calls = [{"strike": r[0], "oi": r[1]} for r in con.execute(
            "SELECT strike,oi FROM options WHERE symbol=? AND expiration=? AND date=? AND type='call' ORDER BY oi DESC LIMIT 5",
            (sym, exp, d0)).fetchall()]
        top_puts = [{"strike": r[0], "oi": r[1]} for r in con.execute(
            "SELECT strike,oi FROM options WHERE symbol=? AND expiration=? AND date=? AND type='put' ORDER BY oi DESC LIMIT 5",
            (sym, exp, d0)).fetchall()]
        series = [{"date": r[0], "call_oi": r[1] or 0, "put_oi": r[2] or 0,
                   "total_oi": (r[1] or 0)+(r[2] or 0)} for r in con.execute(
            """SELECT date, SUM(CASE WHEN type='call' THEN oi ELSE 0 END),
               SUM(CASE WHEN type='put' THEN oi ELSE 0 END)
               FROM options WHERE symbol=? AND expiration=? GROUP BY date ORDER BY date""",
            (sym, exp)).fetchall()]
        top_movers = []
        if a1:
            top_movers = [{"strike": r[0], "type": r[1], "oi": r[2], "prev_oi": r[3],
                           "chg": (r[2] or 0) - (r[3] or 0)} for r in con.execute("""
                SELECT t.strike, t.type, t.oi, COALESCE(p.oi,0)
                FROM options t
                LEFT JOIN options p ON t.symbol=p.symbol AND t.expiration=p.expiration
                    AND t.strike=p.strike AND t.type=p.type AND p.date=?
                WHERE t.symbol=? AND t.expiration=? AND t.date=?
                ORDER BY ABS(t.oi - COALESCE(p.oi,0)) DESC LIMIT 8""",
                (d1, sym, exp, d0)).fetchall()]

        try: dte = max(0, (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days)
        except: dte = 0

        result["expiries"].append({
            "expiry": exp, "dte": dte, "latest_date": d0,
            "call_oi": call_oi, "put_oi": put_oi, "total_oi": total_oi,
            "oi_change": oi_chg,
            "oi_change_pct": round(oi_chg / max(1, cprev + pprev) * 100, 2),
            "pcr": pcr, "pcr_change": round(pcr - pcr_prev, 3),
            "series": series, "top_call_strikes": top_calls,
            "top_put_strikes": top_puts, "top_movers": top_movers,
        })

    con.close()
    return result
