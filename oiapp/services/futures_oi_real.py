"""
Real Futures OI Data Service
============================
Fetches daily Open Interest for ES/NQ/etc futures from multiple sources:

Priority order:
  1. Barchart OnDemand API  (free key from barchart.com/ondemand/free-api-key)
  2. CME Group public data  (via requests with browser headers)
  3. CFTC COT bulk download (weekly, free, official gov data)
  4. yfinance volume proxy  (last resort — not real OI)

To use Barchart (recommended):
  - Sign up free at: https://www.barchart.com/ondemand/free-api-key
  - Add key to DB:   INSERT OR REPLACE INTO app_config VALUES ('barchart_api_key', 'YOUR_KEY');
  - Or set env var:  BARCHART_API_KEY=YOUR_KEY
"""

import os
import json
import sqlite3
import datetime
import requests
from pathlib import Path

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

# ── Contract map ──────────────────────────────────────────────────────────────
FUTURES_MAP = {
    "SPY": {"root": "ES", "name": "E-Mini S&P 500"},
    "QQQ": {"root": "NQ", "name": "E-Mini NASDAQ-100"},
    "IWM": {"root": "RTY", "name": "E-Mini Russell 2000"},
    "GLD": {"root": "GC", "name": "Gold"},
    "USO": {"root": "CL", "name": "Crude Oil"},
    "TLT": {"root": "ZB", "name": "30-Year T-Bond"},
}

# Month codes: F=Jan G=Feb H=Mar J=Apr K=May M=Jun N=Jul Q=Aug U=Sep V=Oct X=Nov Z=Dec
MONTH_CODES = {1:'F',2:'G',3:'H',4:'J',5:'K',6:'M',7:'N',8:'Q',9:'U',10:'V',11:'X',12:'Z'}


def _get_api_key():
    """Get Barchart API key from DB or environment."""
    key = os.environ.get("BARCHART_API_KEY", "")
    if key: return key
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)")
        row = con.execute("SELECT value FROM app_config WHERE key='barchart_api_key'").fetchone()
        con.close()
        return row[0] if row else ""
    except:
        return ""


def _save_api_key(key):
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT OR REPLACE INTO app_config VALUES ('barchart_api_key', ?)", (key,))
    con.commit(); con.close()


def _quarterly_contracts(root, count=3):
    """Return next N quarterly contract codes. E.g. ESM26, ESU26, ESZ26."""
    today = datetime.date.today()
    quarterly = [3, 6, 9, 12]  # H, M, U, Z
    contracts = []
    year = today.year
    month = today.month
    while len(contracts) < count:
        for qm in quarterly:
            if len(contracts) >= count: break
            if year == today.year and qm < month - 1: continue
            code = root + MONTH_CODES[qm] + str(year)[-2:]
            exp_date = datetime.date(year, qm, 15)  # approx expiry
            contracts.append({"contract": code, "expiry": exp_date.isoformat(),
                              "month": qm, "year": year})
        year += 1
    return contracts[:count]


# ── Source 1: Barchart OnDemand (free tier) ───────────────────────────────────
def _fetch_barchart(root, contracts, api_key):
    """
    Barchart OnDemand free API — provides volume + OI for futures.
    Free key: https://www.barchart.com/ondemand/free-api-key
    """
    symbols = ",".join(c["contract"] for c in contracts)
    url = (f"https://ondemand.websol.barchart.com/getFuturesSpecifications.json"
           f"?apikey={api_key}&symbols={symbols}")
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        if data.get("status", {}).get("code") != 200:
            return None
        results = {}
        for row in data.get("results", []):
            sym = row.get("symbol", "")
            oi  = int(row.get("openInterest") or 0)
            vol = int(row.get("volume") or 0)
            results[sym] = {"oi": oi, "volume": vol, "source": "barchart"}
        return results if results else None
    except Exception as e:
        print(f"[barchart] {e}")
        return None


def _fetch_barchart_history(contract, api_key, days=30):
    """Barchart getHistory endpoint for daily OHLCV + OI."""
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    url = (f"https://ondemand.websol.barchart.com/getHistory.json"
           f"?apikey={api_key}&symbol={contract}&type=daily"
           f"&startDate={start.replace('-','')}&maxRecords={days}")
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        if data.get("status", {}).get("code") != 200:
            return []
        rows = []
        for row in data.get("results", []):
            rows.append({
                "date":   row.get("tradingDay", "")[:10],
                "open":   float(row.get("open")  or 0),
                "high":   float(row.get("high")  or 0),
                "low":    float(row.get("low")   or 0),
                "close":  float(row.get("close") or 0),
                "volume": int(row.get("volume")  or 0),
                "oi":     int(row.get("openInterest") or 0),
                "source": "barchart",
            })
        return rows
    except Exception as e:
        print(f"[barchart history {contract}] {e}")
        return []


# ── Source 2: CME Group public (via requests) ─────────────────────────────────
def _fetch_cme_oi(contracts):
    """
    Attempt to scrape CME's public settlement/OI data.
    Works intermittently — CME blocks some IPs/user-agents.
    """
    today = datetime.date.today()
    month_year = today.strftime("%Y%m")
    url = (f"https://www.cmegroup.com/CmeWS/mvc/Volume/Download/F"
           f"?monthYear={month_year}&exchangeSeg=XCME&reportType=D")
    try:
        r = requests.get(url, timeout=12, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36"),
            "Accept": "text/csv,application/octet-stream,*/*",
            "Referer": "https://www.cmegroup.com/",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if r.status_code != 200:
            return None
        import io, csv
        reader = csv.DictReader(io.StringIO(r.text))
        results = {}
        for row in reader:
            globex = (row.get("Globex") or row.get("Globex Symbol", "")).strip()
            exp_mo = (row.get("Expiration Month") or row.get("Contract Month", "")).strip()
            if not globex: continue
            for c in contracts:
                if c["contract"].startswith(globex):
                    oi  = int((row.get("Open Interest") or "0").replace(",","") or 0)
                    vol = int((row.get("Total Volume")  or "0").replace(",","") or 0)
                    results[c["contract"]] = {"oi": oi, "volume": vol, "source": "cme"}
        return results if results else None
    except Exception as e:
        print(f"[cme] {e}")
        return None


# ── Source 3: CFTC COT Bulk Download (weekly, official) ──────────────────────
def _fetch_cftc_cot(root_name):
    """
    CFTC Commitments of Traders — weekly, every Friday.
    Free official government data with real aggregate OI.
    Returns latest long + short + OI data.
    """
    year = datetime.date.today().year
    url = f"https://www.cftc.gov/sites/default/files/files/dea/newcot/fut_disagg_txt_{year}.zip"
    try:
        import zipfile, io, csv
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        fname = [n for n in zf.namelist() if n.endswith('.txt')][0]
        content = zf.read(fname).decode('utf-8', errors='replace')
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            market = row.get("Market and Exchange Names", "")
            if root_name.upper() in market.upper():
                oi = int(row.get("Open_Interest_All", 0) or 0)
                report_date = row.get("Report_Date_as_YYYY-MM-DD", "")
                return {
                    "oi":      oi,
                    "date":    report_date,
                    "market":  market,
                    "source":  "cftc_cot",
                    "note":    "Weekly COT data — updated each Friday"
                }
        return None
    except Exception as e:
        print(f"[cftc] {e}")
        return None


# ── Source 4: yfinance volume proxy (last resort) ────────────────────────────
def _fetch_yfinance_volume(contract_code, days=30):
    """Last resort: yfinance OHLCV. OI column not available; uses volume."""
    try:
        import yfinance as yf
        suffixes = [".CME", "=F", ""]
        for sfx in suffixes:
            try:
                hist = yf.Ticker(contract_code + sfx).history(period=f"{days}d")
                if hist.empty: continue
                rows = []
                for idx, r in hist.iterrows():
                    vol = int(r.get("Volume", 0) or 0)
                    # Check if real OI exists
                    oi_raw = int(r.get("Open Interest", 0) or 0)
                    rows.append({
                        "date":   idx.strftime("%Y-%m-%d"),
                        "open":   round(float(r.get("Open",  0) or 0), 2),
                        "high":   round(float(r.get("High",  0) or 0), 2),
                        "low":    round(float(r.get("Low",   0) or 0), 2),
                        "close":  round(float(r.get("Close", 0) or 0), 2),
                        "volume": vol,
                        "oi":     oi_raw if oi_raw > 0 else vol,
                        "source": "yfinance_oi" if oi_raw > 0 else "yfinance_volume_proxy",
                    })
                if rows: return rows
            except: continue
    except Exception as e:
        print(f"[yfinance {contract_code}] {e}")
    return []


# ── DB storage ────────────────────────────────────────────────────────────────
def _ensure_table():
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    con.execute("""CREATE TABLE IF NOT EXISTS futures_oi_daily (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT NOT NULL,
        contract    TEXT NOT NULL,
        trade_date  TEXT NOT NULL,
        settle      REAL DEFAULT 0,
        volume      INTEGER DEFAULT 0,
        oi          INTEGER DEFAULT 0,
        oi_change   INTEGER DEFAULT 0,
        source      TEXT DEFAULT 'unknown',
        UNIQUE(contract, trade_date)
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_foid_contract ON futures_oi_daily(contract)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_foid_date ON futures_oi_daily(trade_date)")
    # Same fix as futures_oi_schwab.py's _ensure_table -- neither version
    # had an index covering `symbol`+`source` together, which is exactly
    # the query pattern that was taking 3+ minutes (full table scan on a
    # growing table). Both files define the same table name via CREATE
    # TABLE IF NOT EXISTS, so whichever runs first "wins" the schema, but
    # CREATE INDEX IF NOT EXISTS from either is safe to apply regardless.
    con.execute("CREATE INDEX IF NOT EXISTS idx_foid_symbol_source ON futures_oi_daily(symbol, source)")
    con.commit()
    try:
        con.execute("ANALYZE futures_oi_daily")
        con.commit()
    except Exception:
        pass
    con.close()


def store_rows(symbol, contract, rows):
    if not rows: return 0
    _ensure_table()
    con = sqlite3.connect(DB_PATH)
    saved = 0
    # Compute OI change
    rows_sorted = sorted(rows, key=lambda r: r["date"])
    prev_oi = None
    for row in rows_sorted:
        oi_chg = (row["oi"] - prev_oi) if prev_oi is not None else 0
        prev_oi = row["oi"]
        try:
            con.execute("""INSERT OR REPLACE INTO futures_oi_daily
                (symbol, contract, trade_date, settle, volume, oi, oi_change, source)
                VALUES (?,?,?,?,?,?,?,?)""",
                (symbol.upper(), contract, row["date"],
                 row.get("close", row.get("settle", 0)),
                 row.get("volume", 0), row.get("oi", 0),
                 oi_chg, row.get("source", "unknown")))
            saved += 1
        except: pass
    con.commit(); con.close()
    return saved


# ── Main fetch function ───────────────────────────────────────────────────────
def fetch_real_futures_oi(equity_sym="SPY", days=35):
    """
    Fetch real daily futures OI for an equity's underlying contract.
    Tries: Barchart → CME → CFTC COT → yfinance volume proxy.
    Returns summary dict.
    """
    _ensure_table()
    info = FUTURES_MAP.get(equity_sym.upper())
    if not info:
        return {"error": f"No futures mapping for {equity_sym}"}

    root      = info["root"]
    contracts = _quarterly_contracts(root, count=3)
    api_key   = _get_api_key()
    results   = {"symbol": equity_sym, "contracts": [], "source": None, "rows_saved": 0}

    # ── Source 1: Barchart (real OI) ──────────────────────────────────────
    if api_key:
        all_saved = 0
        for c in contracts:
            hist = _fetch_barchart_history(c["contract"], api_key, days=days)
            if hist:
                saved = store_rows(equity_sym, c["contract"], hist)
                all_saved += saved
                results["contracts"].append({"contract": c["contract"], "rows": saved,
                                              "source": "barchart"})
        if all_saved > 0:
            results["source"] = "barchart"; results["rows_saved"] = all_saved
            print(f"✅ Barchart: {equity_sym} {all_saved} rows")
            return results

    # ── Source 2: CME public ──────────────────────────────────────────────
    cme_data = _fetch_cme_oi(contracts)
    if cme_data:
        today = datetime.date.today().isoformat()
        all_saved = 0
        for c in contracts:
            if c["contract"] in cme_data:
                d = cme_data[c["contract"]]
                row = {"date": today, "close": 0, "volume": d["volume"],
                       "oi": d["oi"], "source": "cme"}
                saved = store_rows(equity_sym, c["contract"], [row])
                all_saved += saved
                results["contracts"].append({"contract": c["contract"], "rows": saved,
                                              "source": "cme", "oi": d["oi"]})
        if all_saved > 0:
            results["source"] = "cme"; results["rows_saved"] = all_saved
            print(f"✅ CME: {equity_sym} {all_saved} rows")
            return results

    # ── Source 3: CFTC COT (weekly aggregate) ────────────────────────────
    cot = _fetch_cftc_cot(info["name"])
    if cot and cot.get("oi", 0) > 0:
        row = {"date": cot["date"] or datetime.date.today().isoformat(),
               "close": 0, "volume": 0, "oi": cot["oi"], "source": "cftc_cot"}
        saved = store_rows(equity_sym, contracts[0]["contract"], [row])
        results["source"] = "cftc_cot"; results["rows_saved"] = saved
        results["contracts"].append({"contract": contracts[0]["contract"],
                                      "source": "cftc_cot", "oi": cot["oi"],
                                      "note": cot.get("note", "")})
        print(f"✅ CFTC COT: {equity_sym} OI={cot['oi']:,} (weekly)")
        return results

    # ── Source 4: yfinance volume proxy (fallback) ────────────────────────
    all_saved = 0
    for c in contracts:
        hist = _fetch_yfinance_volume(c["contract"], days=days)
        if hist:
            saved = store_rows(equity_sym, c["contract"], hist)
            all_saved += saved
            results["contracts"].append({"contract": c["contract"], "rows": saved,
                                          "source": hist[0].get("source","yfinance")})
    results["source"] = "yfinance_volume_proxy" if all_saved else "none"
    results["rows_saved"] = all_saved
    if all_saved: print(f"⚠ yfinance volume proxy: {equity_sym} {all_saved} rows (not real OI)")
    return results


def get_latest_oi(equity_sym="SPY", contracts=None, days=30):
    """Get latest OI series from DB for dashboard/charts."""
    _ensure_table()
    info = FUTURES_MAP.get(equity_sym.upper(), {})
    root = info.get("root", equity_sym)
    if not contracts:
        contracts = [c["contract"] for c in _quarterly_contracts(root, 3)]

    con = sqlite3.connect(DB_PATH)
    oi_series = {}
    for ct in contracts:
        rows = con.execute(
            """SELECT trade_date, oi, volume, settle, oi_change, source
               FROM futures_oi_daily WHERE contract=?
               ORDER BY trade_date DESC LIMIT ?""",
            (ct, days)).fetchall()
        oi_series[ct] = [{"date": r[0], "oi": r[1], "volume": r[2],
                           "close": r[3], "oi_change": r[4], "source": r[5]}
                          for r in reversed(rows)]
    con.close()
    return {"symbol": equity_sym, "contracts": contracts, "oi_series": oi_series}


def analyze_roll_adjusted(equity_sym="SPY", days=5):
    """Roll-adjusted signal using REAL OI from futures_oi_daily table."""
    _ensure_table()
    info = FUTURES_MAP.get(equity_sym.upper(), {})
    root = info.get("root", equity_sym)
    contracts = _quarterly_contracts(root, 2)
    front = contracts[0]["contract"]; back = contracts[1]["contract"]

    con = sqlite3.connect(DB_PATH)
    def get_rows(ct):
        return con.execute(
            "SELECT trade_date, oi, volume, settle FROM futures_oi_daily "
            "WHERE contract=? ORDER BY trade_date DESC LIMIT ?",
            (ct, days+2)).fetchall()

    f_rows = get_rows(front); b_rows = get_rows(back)

    # Build OI series for all 3 contracts
    all_contracts = _quarterly_contracts(root, 3)
    oi_series = {}
    for c in all_contracts:
        ct = c["contract"]
        ct_rows = con.execute(
            "SELECT trade_date, oi, settle, volume FROM futures_oi_daily "
            "WHERE contract=? ORDER BY trade_date ASC",
            (ct,)).fetchall()
        oi_series[ct] = [{"date": r[0], "oi": r[1], "close": r[2],
                           "volume": r[3]} for r in ct_rows[-30:]]
    con.close()

    if len(f_rows) < 2:
        return {"signal": "NO_DATA", "front": front, "back": back,
                "note": "Run Scheduler to fetch futures OI data",
                "contracts": [c["contract"] for c in all_contracts],
                "oi_series": oi_series,
                "front_oi_chg": 0, "back_oi_chg": 0, "net_oi_chg": 0}

    f_oi_now = f_rows[0][1] or 0; f_oi_prev = f_rows[1][1] or 0
    b_oi_now = b_rows[0][1] or 0 if b_rows else 0
    b_oi_prev= b_rows[1][1] or 0 if len(b_rows)>1 else b_oi_now
    f_chg = f_oi_now - f_oi_prev
    b_chg = b_oi_now - b_oi_prev
    net   = f_chg + b_chg

    # Check data source — note if using volume proxy
    source = f_rows[0][3] if len(f_rows[0]) > 3 else ""

    is_roll = f_chg < 0 and b_chg > 0

    if is_roll:
        signal = "NET_LONG_BUILDUP" if net > 0 else "ROLL"
        interp = (f"Roll in progress. {front} {f_chg:+,} → {back} {b_chg:+,}. "
                  f"NET {net:+,} = {'bullish' if net>0 else 'neutral'}.")
    elif net > 0:
        signal = "NET_LONG_BUILDUP"
        interp = f"Combined OI rising {net:+,} across {front}+{back}. Bullish positioning."
    elif net < 0:
        signal = "NET_LONG_UNWINDING"
        interp = f"Combined OI falling {net:+,}. Bearish lean / position reduction."
    else:
        signal = "NEUTRAL"; interp = "No net OI change."

    score_map = {"NET_LONG_BUILDUP": 3, "ROLL": 0, "NET_LONG_UNWINDING": -3, "NEUTRAL": 0}
    return {
        "front": front, "back": back, "signal": signal,
        "score": score_map.get(signal, 0), "interpretation": interp,
        "is_roll": is_roll, "roll_note": f"Roll: {front}→{back}" if is_roll else None,
        "front_oi_chg": f_chg, "back_oi_chg": b_chg, "net_oi_chg": net,
        "front_price": round(float(f_rows[0][3] if len(f_rows[0])>3 else f_rows[0][2] or 0), 2),
        "contracts": [c["contract"] for c in all_contracts],
        "oi_series": oi_series,
        "data_source": source,
        "is_volume_proxy": "volume_proxy" in (source or ""),
    }


def fetch_all_watchlist_futures_oi() -> dict:
    """Runs fetch_real_futures_oi() for SPY/QQQ/IWM specifically -- the
    three symbols api_weekly_rolling() actually uses, not the full
    equity watchlist (futures OI isn't meaningful per-stock the way it
    is for these three broad-index ETFs). This is the function that was
    genuinely missing a scheduled trigger entirely -- confirmed no
    register_scheduler_job() existed anywhere in this module, which is
    exactly why the rolling scanner has been showing NO_DATA(0) for all
    three futures roots: nothing was ever populating the table.
    """
    results = {}
    for sym in ("SPY", "QQQ", "IWM"):
        try:
            results[sym] = fetch_real_futures_oi(equity_sym=sym)
        except Exception as e:
            results[sym] = {"error": str(e)}
    return results


def register_scheduler_job(interval_seconds: int = 21600):
    """Every 6h by default (21600s) -- futures OI updates once per
    trading day in practice (it's a daily settlement figure, not an
    intraday-streaming one), so this doesn't need tastytrade-backfill-
    style frequent polling. A few checks a day is enough to pick up the
    daily update promptly without hammering whichever data source
    (Barchart/CME/CFTC/yfinance-proxy) actually serves the request.
    """
    from . import unified_scheduler
    from .job_registry import register_job
    register_job(
        "futures_oi_daily_fetch", "Futures OI (SPY/QQQ/IWM -> ES/NQ/RTY)",
        "Daily futures open interest for the three index ETFs the rolling scanner uses",
        kind="interval", default_schedule={"interval_min": round(interval_seconds / 60, 2)},
        group="Live Capture", run_now_fn=fetch_all_watchlist_futures_oi, editable=True,
    )
    return unified_scheduler.register(
        "futures_oi_daily_fetch", fetch_all_watchlist_futures_oi,
        interval_seconds=interval_seconds, low_priority=True,  # not time-critical the way OI/Greeks fetches are -- fine to be delayed by an active scan
    )
