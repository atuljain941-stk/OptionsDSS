"""
Futures Open Interest Service

DATA SOURCES (in priority order):
1. CME Group Settlement API  — free, no key, updated after close daily
   URL: https://www.cmegroup.com/CmeWS/mvc/Settlements/futures/tradeDate/{DATE}/productId/{ID}
   Product IDs: ES=13, NQ=209, YM=239, RTY=239, CL=425, GC=437

2. CFTC Traders in Financial Futures — free, no key, WEEKLY (Tuesdays)
   URL: https://publicreporting.cftc.gov/api/explore/dataset/traders-in-financial-futures-legacy/

3. yfinance volume — fallback only (NOT real OI, just volume proxy)

SIGNALS (based on Price + OI change):
  Price UP   + OI UP   → LONG_BUILDUP     (new longs entering — bullish)
  Price DOWN + OI UP   → SHORT_BUILDUP    (new shorts entering — bearish)
  Price DOWN + OI DOWN → LONG_UNWINDING   (longs exiting — bearish lean)
  Price UP   + OI DOWN → SHORT_COVERING   (shorts covering — mild bullish)
"""

import sqlite3, json, datetime, urllib.request, ssl, time
from pathlib import Path

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

# CME product IDs for common equity futures
CME_PRODUCTS = {
    "SPY": {"id": "13",  "root": "ES", "name": "E-mini S&P 500"},
    "QQQ": {"id": "209", "root": "NQ", "name": "E-mini NASDAQ-100"},
    "IWM": {"id": "239", "root": "RTY","name": "E-mini Russell 2000"},
    "DIA": {"id": "17",  "root": "YM", "name": "E-mini Dow"},
}

# Quarter month codes
MONTH_CODES = {3:"H", 6:"M", 9:"U", 12:"Z"}
MONTH_NAMES  = {"H":3, "M":6, "U":9, "Z":12}

DB_TABLE = """CREATE TABLE IF NOT EXISTS futures_oi (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    contract      TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    expiry_month  TEXT,
    open          REAL, high REAL, low REAL, close REAL,
    settle        REAL,
    volume        INTEGER DEFAULT 0,
    oi            INTEGER DEFAULT 0,
    oi_change     INTEGER DEFAULT 0,
    source        TEXT DEFAULT 'cme',
    UNIQUE(contract, trade_date)
)"""


def _connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _ensure_table():
    con = _connect()
    con.execute(DB_TABLE)
    # Add columns if missing (migration)
    existing = {r[1] for r in con.execute("PRAGMA table_info(futures_oi)").fetchall()}
    for col, sql in [
        ("settle",    "ALTER TABLE futures_oi ADD COLUMN settle REAL"),
        ("oi_change", "ALTER TABLE futures_oi ADD COLUMN oi_change INTEGER DEFAULT 0"),
        ("source",    "ALTER TABLE futures_oi ADD COLUMN source TEXT DEFAULT 'cme'"),
    ]:
        if col not in existing:
            try: con.execute(sql)
            except: pass
    con.commit(); con.close()


def get_quarterly_contracts(equity_sym, count=3):
    """Return next N quarterly contract codes for an equity symbol."""
    info = CME_PRODUCTS.get(equity_sym.upper())
    if not info:
        return []
    root  = info["root"]
    today = datetime.date.today()
    contracts = []
    year  = today.year
    month = today.month
    for _ in range(count * 4):  # scan up to 4*count months ahead
        if month in MONTH_CODES:
            code  = root + MONTH_CODES[month] + str(year)[-2:]
            expiry = datetime.date(year, month, 1)
            if expiry >= today - datetime.timedelta(days=10):
                contracts.append({
                    "contract": code,
                    "year":     year,
                    "month":    month,
                    "expiry_month": f"{year}-{month:02d}",
                    "cme_product_id": info["id"],
                })
                if len(contracts) >= count:
                    break
        month += 1
        if month > 12:
            month = 1; year += 1
    return contracts


# ── CME Settlement API ─────────────────────────────────────────────────────

def _cme_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept":     "application/json, text/plain, */*",
        "Referer":    "https://www.cmegroup.com/trading/equity-index/",
        "Origin":     "https://www.cmegroup.com",
    }


def fetch_cme_oi(equity_sym, trade_date=None):
    """
    Fetch OI from CME Group Settlement API for a given date.
    Returns list of {contract, settle, volume, oi, oi_change}.
    """
    info = CME_PRODUCTS.get(equity_sym.upper())
    if not info:
        return {"error": f"No CME mapping for {equity_sym}", "rows": []}

    if not trade_date:
        # Use most recent business day
        td = datetime.date.today()
        while td.weekday() >= 5:  # skip weekends
            td -= datetime.timedelta(days=1)
        trade_date = td.strftime("%Y%m%d")

    url = (f"https://www.cmegroup.com/CmeWS/mvc/Settlements/futures/tradeDate/"
           f"{trade_date}/productId/{info['id']}")

    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers=_cme_headers())
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e), "rows": [], "source": "cme"}

    # CME response structure: {"settlements": [...]}
    settlements = raw.get("settlements", raw.get("items", []))
    if not settlements:
        return {"error": "No settlements data", "rows": [], "source": "cme"}

    rows = []
    root = info["root"]
    for item in settlements:
        # Filter to quarterly contracts of this root
        contract = str(item.get("contract", "")).strip()
        if not contract.startswith(root):
            continue
        # Extract OI — CME uses "openInterest" or "oi"
        oi       = _parse_int(item.get("openInterest") or item.get("oi") or 0)
        oi_chg   = _parse_int(item.get("openInterestChange") or item.get("oiChange") or 0)
        vol      = _parse_int(item.get("volume") or item.get("estimatedVolume") or 0)
        settle   = _parse_float(item.get("settle") or item.get("settlementPrice") or 0)
        rows.append({
            "contract":   contract,
            "trade_date": trade_date[:4] + "-" + trade_date[4:6] + "-" + trade_date[6:],
            "settle":     settle,
            "volume":     vol,
            "oi":         oi,
            "oi_change":  oi_chg,
            "source":     "cme",
        })

    return {"rows": rows, "trade_date": trade_date, "source": "cme", "count": len(rows)}


def _parse_int(v):
    try: return int(str(v).replace(",","").strip() or 0)
    except: return 0

def _parse_float(v):
    try: return float(str(v).replace(",","").strip() or 0)
    except: return 0.0


# ── CFTC COT (weekly institutional data) ─────────────────────────────────

def fetch_cftc_cot(equity_sym, weeks=8):
    """
    Fetch CFTC Commitments of Traders (weekly, Tuesdays).
    Returns asset manager / leveraged fund net positions — the real institutional view.
    """
    info = CME_PRODUCTS.get(equity_sym.upper())
    if not info:
        return {"error": f"No CFTC mapping for {equity_sym}", "rows": []}

    market_code = info["root"]  # "ES" for S&P

    url = (f"https://publicreporting.cftc.gov/api/explore/dataset/"
           f"traders-in-financial-futures-legacy/records/"
           f"?select=report_date_as_yyyy_mm_dd,open_interest_all,"
           f"asset_mgr_positions_long,asset_mgr_positions_short,"
           f"lev_money_positions_long,lev_money_positions_short,"
           f"dealer_positions_long,dealer_positions_short"
           f"&where=cftc_market_code%3D%22{market_code}%22"
           f"&order_by=report_date_as_yyyy_mm_dd+desc"
           f"&limit={weeks}&referer=legacy")
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        records = raw.get("results", raw.get("records", []))
        rows = []
        for r in records:
            fields = r.get("record", {}).get("fields", r)
            am_long  = _parse_int(fields.get("asset_mgr_positions_long",   0))
            am_short = _parse_int(fields.get("asset_mgr_positions_short",  0))
            lm_long  = _parse_int(fields.get("lev_money_positions_long",   0))
            lm_short = _parse_int(fields.get("lev_money_positions_short",  0))
            rows.append({
                "date":          fields.get("report_date_as_yyyy_mm_dd",""),
                "open_interest": _parse_int(fields.get("open_interest_all", 0)),
                "asset_mgr_net": am_long - am_short,
                "lev_money_net": lm_long - lm_short,
                "asset_mgr_long":  am_long,  "asset_mgr_short":  am_short,
                "lev_money_long":  lm_long,  "lev_money_short":  lm_short,
            })
        return {"rows": rows, "source": "cftc_cot", "count": len(rows)}
    except Exception as e:
        return {"error": str(e), "rows": [], "source": "cftc_cot"}


# ── Store to DB ───────────────────────────────────────────────────────────

def store_cme_oi(equity_sym, rows):
    """Store CME OI rows into futures_oi table."""
    if not rows:
        return 0
    _ensure_table()
    con   = _connect()
    saved = 0
    for r in rows:
        try:
            # Also get price from yfinance if settle is 0
            settle = r.get("settle") or 0
            con.execute("""INSERT OR REPLACE INTO futures_oi
                (symbol, contract, trade_date, expiry_month, settle,
                 volume, oi, oi_change, source)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (equity_sym.upper(), r["contract"], r["trade_date"],
                 r.get("expiry_month", r["contract"][-3:]),
                 settle, r.get("volume",0), r.get("oi",0),
                 r.get("oi_change",0), r.get("source","cme")))
            saved += 1
        except: pass
    con.commit(); con.close()
    return saved




def _fetch_real_oi(contract_root, contract_code):
    """
    Try to fetch real Open Interest for a futures contract.
    Attempts: yfinance (often 0), then returns None if unavailable.
    Future: could integrate CME direct API here.
    """
    try:
        import yfinance as yf
        # Some futures tickers DO have OI in yfinance
        for suffix in ['=F', '.CME', '']:
            try:
                tk = yf.Ticker(contract_code + suffix)
                hist = tk.history(period='5d')
                if not hist.empty and 'Open Interest' in hist.columns:
                    last_oi = hist['Open Interest'].dropna()
                    if len(last_oi) and last_oi.iloc[-1] > 0:
                        return int(last_oi.iloc[-1])
            except: continue
    except: pass
    return None

def fetch_futures_oi(equity_sym, period="60d"):
    """
    Main entry point: fetch OI from CME (primary) with yfinance price as supplement.
    Stores results in futures_oi table.
    """
    _ensure_table()
    result = {"symbol": equity_sym, "rows_saved": 0, "source": "none"}

    # 1. Try CME Settlement API (real OI)
    today = datetime.date.today()
    dates_to_fetch = []
    for i in range(30):   # last 30 business days
        td = today - datetime.timedelta(days=i)
        if td.weekday() < 5:  # weekdays only
            dates_to_fetch.append(td.strftime("%Y%m%d"))
        if len(dates_to_fetch) >= 30:
            break

    all_rows = []
    cme_success = False
    for date_str in dates_to_fetch[:5]:  # Try last 5 days to get most recent
        r = fetch_cme_oi(equity_sym, date_str)
        if r.get("rows"):
            all_rows.extend(r["rows"])
            cme_success = True

    if cme_success:
        saved = store_cme_oi(equity_sym, all_rows)
        result.update({"rows_saved": saved, "source": "cme", "cme_rows": len(all_rows)})
        # Supplement with historical dates
        for date_str in dates_to_fetch[5:]:
            r = fetch_cme_oi(equity_sym, date_str)
            if r.get("rows"):
                store_cme_oi(equity_sym, r["rows"])
                time.sleep(0.3)  # be polite to CME
        return result

    # 2. Fallback: yfinance volume (clearly labelled as proxy)
    try:
        import yfinance as yf
        contracts = get_quarterly_contracts(equity_sym, count=3)
        for c in contracts:
            for yf_sym in [f"{c['root']}{MONTH_CODES[c['month']]}{str(c['year'])[-2:]}.CME",
                           f"{c['contract']}=F", c["contract"]]:
                try:
                    hist = yf.Ticker(yf_sym).history(period=period, interval="1d")
                    if hist.empty: continue
                    rows = []
                    for idx, row in hist.iterrows():
                        vol    = int(row.get("Volume", 0) or 0)
                        # Try to get real OI; yfinance rarely provides it for ES
                        oi_raw = 0
                        for oi_col in ("Open Interest", "openInterest", "OI"):
                            try:
                                v = row.get(oi_col)
                                if v is not None and str(v).replace(".","").isdigit():
                                    oi_raw = int(float(v))
                                    break
                            except: pass
                        oi_val  = oi_raw if oi_raw > 0 else vol
                        source  = "yfinance_oi" if oi_raw > 0 else "yfinance_volume_proxy"
                        rows.append({
                            "contract":   c["contract"],
                            "trade_date": idx.strftime("%Y-%m-%d"),
                            "settle":     float(row.get("Close", 0) or 0),
                            "volume":     vol,
                            "oi":         oi_val,
                            "oi_change":  0,
                            "source":     source,
                        })
                    saved = store_cme_oi(equity_sym, rows)
                    result.update({"rows_saved": result["rows_saved"] + saved,
                                   "source": "yfinance_proxy"})
                    break
                except: continue
    except Exception as e:
        result["error"] = str(e)

    return result


# ── Analysis ──────────────────────────────────────────────────────────────

def analyze_oi_buildup(equity_sym, days=5):
    """
    Compute OI-based signal for a symbol using stored data.
    Returns: signal, score, description, and whether using real OI or volume proxy.
    """
    _ensure_table()
    con   = _connect()
    ctrs  = get_quarterly_contracts(equity_sym, count=1)
    if not ctrs:
        con.close()
        return {"signal": "NO_DATA", "score": 0, "description": "No contract mapping"}

    contract = ctrs[0]["contract"]
    rows = con.execute(
        "SELECT trade_date, settle, oi, oi_change, volume, source FROM futures_oi "
        "WHERE contract=? ORDER BY trade_date DESC LIMIT ?",
        (contract, days + 2)).fetchall()
    con.close()

    if len(rows) < 2:
        return {"signal": "NO_DATA", "score": 0, "description": "Run Scheduler to fetch futures data",
                "contract": contract, "is_real_oi": False}

    latest = rows[0]; prior = rows[1]
    is_real_oi = (latest["source"] or "").startswith("cme")

    price_now  = latest["settle"] or 0
    price_prev = prior["settle"]  or 0
    oi_now     = latest["oi"]     or 0
    oi_prev    = prior["oi"]      or 0
    oi_chg     = latest["oi_change"] if latest["oi_change"] else (oi_now - oi_prev)

    price_up = price_now > price_prev
    oi_up    = oi_chg   > 0

    if price_up and oi_up:
        signal = "LONG_BUILDUP"
        score  = 3
        desc   = f"Price ↑ ${price_now:.0f} (+{price_now-price_prev:.0f}) + OI ↑ {oi_chg:+,}. New longs entering — bullish."
    elif not price_up and oi_up:
        signal = "SHORT_BUILDUP"
        score  = -3
        desc   = f"Price ↓ ${price_now:.0f} ({price_now-price_prev:.0f}) + OI ↑ {oi_chg:+,}. New shorts entering — bearish."
    elif not price_up and not oi_up:
        signal = "LONG_UNWINDING"
        score  = -2
        desc   = f"Price ↓ ${price_now:.0f} ({price_now-price_prev:.0f}) + OI ↓ {oi_chg:+,}. Longs exiting — mild bearish."
    else:
        signal = "SHORT_COVERING"
        score  = 1
        desc   = f"Price ↑ ${price_now:.0f} (+{price_now-price_prev:.0f}) + OI ↓ {oi_chg:+,}. Shorts covering — mild bullish."

    return {
        "contract":    contract,
        "signal":      signal,
        "score":       score,
        "description": desc,
        "price":       price_now,
        "price_change":price_now - price_prev,
        "oi":          oi_now,
        "oi_change":   oi_chg,
        "volume":      latest["volume"] or 0,
        "is_real_oi":  is_real_oi,
        "source":      latest["source"] or "unknown",
    }


def analyze_roll_adjusted(equity_sym="SPY", days=5):
    """
    Compare front vs next month OI to detect roll vs true directional change.
    Uses real OI from CME when available.
    """
    _ensure_table()
    contracts = get_quarterly_contracts(equity_sym, count=2)
    if len(contracts) < 2:
        return {"signal": "NO_DATA", "front": None, "back": None,
                "note": "Need 2 contracts", "oi_series": {}, "contracts": []}

    front = contracts[0]["contract"]
    back  = contracts[1]["contract"]
    con   = _connect()

    def get_series(contract, limit=days+2):
        return con.execute(
            "SELECT trade_date, oi, oi_change, volume, settle, source FROM futures_oi "
            "WHERE contract=? ORDER BY trade_date DESC LIMIT ?",
            (contract, limit)).fetchall()

    f_rows = get_series(front)
    b_rows = get_series(back)

    # Build OI series for charts (last 30 data points)
    contracts_all = get_quarterly_contracts(equity_sym, count=3)
    oi_series = {}
    for c in contracts_all:
        ct     = c["contract"]
        ct_rows = con.execute(
            "SELECT trade_date, oi, settle, volume FROM futures_oi "
            "WHERE contract=? ORDER BY trade_date ASC",
            (ct,)).fetchall()
        ct_rows = list(ct_rows)[-30:]
        oi_series[ct] = [{"date": r[0], "oi": r[1] or 0,
                          "close": r[2] or 0, "volume": r[3] or 0}
                         for r in ct_rows]
    con.close()

    if len(f_rows) < 2:
        return {"signal": "NO_DATA", "front": front, "back": back,
                "note": "Run Scheduler → ▶ Run Now to fetch futures OI",
                "oi_series": oi_series,
                "contracts": [c["contract"] for c in contracts_all],
                "front_oi_chg": 0, "back_oi_chg": 0, "net_oi_chg": 0,
                "is_real_oi": False}

    is_real_oi = (f_rows[0]["source"] or "").startswith("cme")

    f_oi_now  = f_rows[0]["oi"] or 0
    f_oi_prev = f_rows[1]["oi"] or 0 if len(f_rows) > 1 else f_oi_now
    b_oi_now  = b_rows[0]["oi"] or 0 if b_rows else 0
    b_oi_prev = b_rows[1]["oi"] or 0 if len(b_rows) > 1 else b_oi_now

    # Prefer stored oi_change (CME provides it directly)
    f_chg = (f_rows[0]["oi_change"] or 0) if f_rows[0]["oi_change"] else (f_oi_now - f_oi_prev)
    b_chg = (b_rows[0]["oi_change"] or 0) if b_rows and b_rows[0]["oi_change"] else (b_oi_now - b_oi_prev)
    net   = f_chg + b_chg

    is_roll = f_chg < 0 and b_chg > 0
    oi_label = "OI" if is_real_oi else "Volume (proxy — CME data not yet fetched)"

    if is_roll:
        signal = "NET_LONG_BUILDUP" if net > 0 else "ROLL"
        interp = (f"Roll in progress. {front} {f_chg:+,} {oi_label} → {back} {b_chg:+,}. "
                  + ("Net positive — underlying demand remains." if net > 0
                     else "Net neutral — contract switch only."))
    elif net > 0:
        signal = "NET_LONG_BUILDUP"
        interp = f"Both contracts gaining {oi_label}: {front} {f_chg:+,}, {back} {b_chg:+,}. Net {net:+,} — bullish."
    elif net < 0:
        signal = "NET_LONG_UNWINDING"
        interp = f"Both contracts losing {oi_label}: {front} {f_chg:+,}, {back} {b_chg:+,}. Net {net:+,} — bearish."
    else:
        signal = "NEUTRAL"
        interp = f"No net change in {oi_label}."

    score_map = {"NET_LONG_BUILDUP": 3, "ROLL": 0, "NET_LONG_UNWINDING": -3,
                 "SHORT_COVERING": 1, "SHORT_BUILDUP": -3, "NEUTRAL": 0}
    return {
        "front": front, "back": back,
        "front_oi": f_oi_now, "front_oi_chg": f_chg,
        "back_oi":  b_oi_now, "back_oi_chg": b_chg,
        "net_oi_chg": net, "is_roll": is_roll,
        "signal": signal, "interpretation": interp,
        "score":  score_map.get(signal, 0),
        "is_real_oi": is_real_oi,
        "front_price": round(float(f_rows[0]["settle"] or 0), 2) if f_rows else None,
        "front_vol":   f_rows[0]["volume"] if f_rows else None,
        "roll_note":   f"Roll: {front}→{back}" if is_roll else None,
        "oi_series":   oi_series,
        "contracts":   [c["contract"] for c in contracts_all],
    }


# Legacy compat
def get_futures_symbol(equity_sym, offset=0):
    ctrs = get_quarterly_contracts(equity_sym, count=offset+1)
    return ctrs[offset]["contract"] if len(ctrs) > offset else None
