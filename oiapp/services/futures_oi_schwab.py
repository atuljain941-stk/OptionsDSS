"""
Futures OI via Schwab Market Data API
======================================
Uses the existing Schwab OAuth tokens stored in schwab_config table.
No extra key needed — just authenticate Schwab once via Settings.

Schwab futures symbols: /ES, /NQ, /RTY, /GC, /CL
Contract format:  /ESM25 (root + month code + 2-digit year)
                  /ES    (continuous — always front month)

What Schwab provides per futures quote:
  openInterest      — real OI from exchange
  totalVolume       — daily volume
  lastPrice         — last trade
  52WeekHigh/Low
  mark / askPrice / bidPrice

Daily history (pricehistory endpoint) — NOT included in free tier for futures OI
So we fetch daily snapshot quotes and accumulate them in DB.
"""

import sqlite3
import datetime
import json
from pathlib import Path

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH
BASE    = "https://api.schwabapi.com"

# ── Contract map ──────────────────────────────────────────────────────────────
# Canonical display symbols used by the UI/scheduler.  Values intentionally
# remain simple strings because older code expects SCHWAB_ROOTS[symbol] -> root.
SCHWAB_ROOTS = {
    # Equity index futures
    "SPY": "/ES",
    "QQQ": "/NQ",
    "IWM": "/RTY",
    "DIA": "/YM",

    # Metals / commodities
    "GLD": "/GC",          # Gold
    "SLV": "/SI",          # Silver
    "COPPER": "/HG",
    "PLATINUM": "/PL",

    # Energy
    "USO": "/CL",          # WTI Crude Oil
    "NATGAS": "/NG",
    "GASOLINE": "/RB",
    "HEATINGOIL": "/HO",

    # Rates
    "TLT": "/ZB",          # 30Y bond proxy
    "ZN10Y": "/ZN",
    "ZF5Y": "/ZF",
    "ZT2Y": "/ZT",

    # CME FX futures.  These are futures roots, not spot-FX broker OI.
    "EURUSD": "/6E",
    "JPYUSD": "/6J",
    "GBPUSD": "/6B",
    "AUDUSD": "/6A",
    "CADUSD": "/6C",
    "CHFUSD": "/6S",
    "NZDUSD": "/6N",
    "MXNUSD": "/6M",
}

SCHWAB_ROOT_META = {
    "SPY": {"label": "S&P 500 E-mini", "asset_class": "Equity Index", "display": "SPY / ES"},
    "QQQ": {"label": "Nasdaq-100 E-mini", "asset_class": "Equity Index", "display": "QQQ / NQ"},
    "IWM": {"label": "Russell 2000 E-mini", "asset_class": "Equity Index", "display": "IWM / RTY"},
    "DIA": {"label": "Dow E-mini", "asset_class": "Equity Index", "display": "DIA / YM"},
    "GLD": {"label": "Gold", "asset_class": "Metals", "display": "Gold / GC"},
    "SLV": {"label": "Silver", "asset_class": "Metals", "display": "Silver / SI"},
    "COPPER": {"label": "Copper", "asset_class": "Metals", "display": "Copper / HG"},
    "PLATINUM": {"label": "Platinum", "asset_class": "Metals", "display": "Platinum / PL"},
    "USO": {"label": "WTI Crude Oil", "asset_class": "Energy", "display": "Crude / CL"},
    "NATGAS": {"label": "Natural Gas", "asset_class": "Energy", "display": "Natural Gas / NG"},
    "GASOLINE": {"label": "RBOB Gasoline", "asset_class": "Energy", "display": "Gasoline / RB"},
    "HEATINGOIL": {"label": "Heating Oil", "asset_class": "Energy", "display": "Heating Oil / HO"},
    "TLT": {"label": "30Y Treasury Bond", "asset_class": "Rates", "display": "30Y Bond / ZB"},
    "ZN10Y": {"label": "10Y Treasury Note", "asset_class": "Rates", "display": "10Y Note / ZN"},
    "ZF5Y": {"label": "5Y Treasury Note", "asset_class": "Rates", "display": "5Y Note / ZF"},
    "ZT2Y": {"label": "2Y Treasury Note", "asset_class": "Rates", "display": "2Y Note / ZT"},
    "EURUSD": {"label": "Euro FX", "asset_class": "FX", "display": "EUR/USD / 6E"},
    "JPYUSD": {"label": "Japanese Yen FX", "asset_class": "FX", "display": "JPY/USD / 6J"},
    "GBPUSD": {"label": "British Pound FX", "asset_class": "FX", "display": "GBP/USD / 6B"},
    "AUDUSD": {"label": "Australian Dollar FX", "asset_class": "FX", "display": "AUD/USD / 6A"},
    "CADUSD": {"label": "Canadian Dollar FX", "asset_class": "FX", "display": "CAD/USD / 6C"},
    "CHFUSD": {"label": "Swiss Franc FX", "asset_class": "FX", "display": "CHF/USD / 6S"},
    "NZDUSD": {"label": "New Zealand Dollar FX", "asset_class": "FX", "display": "NZD/USD / 6N"},
    "MXNUSD": {"label": "Mexican Peso FX", "asset_class": "FX", "display": "MXN/USD / 6M"},
}

# Extra aliases that users may type or older pages may pass.  They normalize to
# one of the canonical keys above to avoid duplicate storage/fetches.
SCHWAB_ROOT_ALIASES = {
    "ES": "SPY", "/ES": "SPY", "SPX": "SPY",
    "NQ": "QQQ", "/NQ": "QQQ",
    "RTY": "IWM", "/RTY": "IWM",
    "YM": "DIA", "/YM": "DIA",
    "GC": "GLD", "/GC": "GLD", "GOLD": "GLD",
    "SI": "SLV", "/SI": "SLV", "SILVER": "SLV",
    "HG": "COPPER", "/HG": "COPPER",
    "PL": "PLATINUM", "/PL": "PLATINUM",
    "CL": "USO", "/CL": "USO", "CRUDE": "USO", "WTI": "USO",
    "NG": "NATGAS", "/NG": "NATGAS",
    "RB": "GASOLINE", "/RB": "GASOLINE",
    "HO": "HEATINGOIL", "/HO": "HEATINGOIL",
    "ZB": "TLT", "/ZB": "TLT", "BONDS": "TLT",
    "ZN": "ZN10Y", "/ZN": "ZN10Y", "10Y": "ZN10Y",
    "ZF": "ZF5Y", "/ZF": "ZF5Y", "5Y": "ZF5Y",
    "ZT": "ZT2Y", "/ZT": "ZT2Y", "2Y": "ZT2Y",
    "6E": "EURUSD", "/6E": "EURUSD", "EUR": "EURUSD",
    "6J": "JPYUSD", "/6J": "JPYUSD", "JPY": "JPYUSD",
    "6B": "GBPUSD", "/6B": "GBPUSD", "GBP": "GBPUSD",
    "6A": "AUDUSD", "/6A": "AUDUSD", "AUD": "AUDUSD",
    "6C": "CADUSD", "/6C": "CADUSD", "CAD": "CADUSD",
    "6S": "CHFUSD", "/6S": "CHFUSD", "CHF": "CHFUSD",
    "6N": "NZDUSD", "/6N": "NZDUSD", "NZD": "NZDUSD",
    "6M": "MXNUSD", "/6M": "MXNUSD", "MXN": "MXNUSD",
}

# Root-specific active month cycles.  The UI stores/fetches several forward
# contracts and then computes cumulative OI across all stored active contracts.
ALL_MONTHS = list(range(1, 13))
QUARTERLY_MONTHS = [3, 6, 9, 12]
ROOT_MONTH_CYCLES = {
    "/ES": QUARTERLY_MONTHS, "/NQ": QUARTERLY_MONTHS, "/RTY": QUARTERLY_MONTHS, "/YM": QUARTERLY_MONTHS,
    "/ZB": QUARTERLY_MONTHS, "/ZN": QUARTERLY_MONTHS, "/ZF": QUARTERLY_MONTHS, "/ZT": QUARTERLY_MONTHS,
    "/6E": QUARTERLY_MONTHS, "/6J": QUARTERLY_MONTHS, "/6B": QUARTERLY_MONTHS, "/6A": QUARTERLY_MONTHS,
    "/6C": QUARTERLY_MONTHS, "/6S": QUARTERLY_MONTHS, "/6N": QUARTERLY_MONTHS, "/6M": QUARTERLY_MONTHS,
    "/GC": [2, 4, 6, 8, 10, 12],
    "/SI": [3, 5, 7, 9, 12],
    "/HG": [3, 5, 7, 9, 12],
    "/PL": [1, 4, 7, 10],
    "/CL": ALL_MONTHS, "/NG": ALL_MONTHS, "/RB": ALL_MONTHS, "/HO": ALL_MONTHS,
}

def normalize_futures_symbol(value, default="SPY"):
    """Normalize UI input such as SPY, /ES, GC, Gold, EURUSD into canonical key."""
    raw = str(value or "").strip().upper()
    if not raw:
        return default
    raw = raw.replace("—", " ").replace("–", " ").replace("/", " /")
    tokens = [t.strip().upper() for t in raw.replace("-", " ").replace("(", " ").replace(")", " ").split() if t.strip()]
    candidates = [str(value or "").strip().upper()] + tokens
    for cand in candidates:
        clean = cand.strip().upper()
        if clean in SCHWAB_ROOTS:
            return clean
        if clean in SCHWAB_ROOT_ALIASES:
            return SCHWAB_ROOT_ALIASES[clean]
        no_slash = clean.replace("/", "")
        if no_slash in SCHWAB_ROOT_ALIASES:
            return SCHWAB_ROOT_ALIASES[no_slash]
    return default

def get_futures_root(value, default="SPY"):
    key = normalize_futures_symbol(value, default=default)
    return SCHWAB_ROOTS.get(key, SCHWAB_ROOTS.get(default, "/ES"))

def get_futures_universe():
    """Return grouped futures universe for dropdowns and diagnostics."""
    groups = {}
    for key, root in SCHWAB_ROOTS.items():
        meta = dict(SCHWAB_ROOT_META.get(key, {}))
        meta.update({"symbol": key, "root": root})
        groups.setdefault(meta.get("asset_class", "Other"), []).append(meta)
    for rows in groups.values():
        rows.sort(key=lambda r: (r.get("display") or r.get("symbol") or ""))
    return groups

MONTH_CODES = {
    1:'F', 2:'G', 3:'H', 4:'J', 5:'K', 6:'M',
    7:'N', 8:'Q', 9:'U', 10:'V', 11:'X', 12:'Z'
}


def _get_quarterly_contracts(root, count=6):
    """Return next N active contract symbols for Schwab.

    The historical name is kept for backward compatibility, but the function is
    now root-aware.  Equity/rates/FX roots use quarterly contracts; metals and
    energy use their normal active month cycles.  This lets the dashboard keep
    history and compute cumulative OI across the curve instead of only looking
    at a front contract.
    """
    root = str(root or "/ES").upper()
    if not root.startswith("/"):
        root = "/" + root
    months = ROOT_MONTH_CYCLES.get(root, QUARTERLY_MONTHS)
    today = datetime.date.today()
    contracts = []
    year = today.year
    # scan far enough ahead to cover monthly commodities and quarterly FX/rates
    while len(contracts) < max(1, int(count or 6)) and year <= today.year + 3:
        for m in months:
            if len(contracts) >= max(1, int(count or 6)):
                break
            # Approximate expiry around the third week.  We keep near-expired
            # contracts for a short grace period because OI can be useful during
            # roll week and some roots use month-specific expiration rules.
            exp_approx = datetime.date(year, int(m), 21)
            if exp_approx < today - datetime.timedelta(days=35):
                continue
            code = MONTH_CODES.get(int(m))
            if not code:
                continue
            sym = f"{root}{code}{str(year)[-2:]}"
            contracts.append({
                "symbol":  sym,
                "expiry":  exp_approx.isoformat(),
                "month":   int(m),
                "year":    int(year),
                "root":    root,
            })
        year += 1
    return contracts[:max(1, int(count or 6))]


def _get_schwab_config():
    """Read Schwab OAuth config from the existing SQLite config table."""
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM schwab_config WHERE id=1").fetchone()
        con.close()
        return dict(row) if row else None
    except Exception:
        return None


def _save_schwab_config(**updates):
    if not updates:
        return
    con = sqlite3.connect(DB_PATH)
    cols = {r[1] for r in con.execute("PRAGMA table_info(schwab_config)").fetchall()}
    safe = {k: v for k, v in updates.items() if k in cols}
    if safe:
        sets = ", ".join(f"{k}=?" for k in safe)
        con.execute(f"UPDATE schwab_config SET {sets}, updated=? WHERE id=1",
                    (*safe.values(), datetime.datetime.now().isoformat(timespec="seconds")))
        con.commit()
    con.close()


def schwab_auth_status():
    """Return non-secret Schwab auth status used by diagnostics and UI."""
    cfg = _get_schwab_config() or {}
    expiry_raw = cfg.get("token_expiry") or ""
    expires_in = None
    expired = False
    try:
        expires_in = float(expiry_raw) - datetime.datetime.now().timestamp()
        expired = expires_in <= 0
    except Exception:
        pass
    return {
        "configured": bool(cfg.get("app_key") and cfg.get("app_secret")),
        "has_access_token": bool(cfg.get("access_token")),
        "has_refresh_token": bool(cfg.get("refresh_token")),
        "token_expiry": expiry_raw,
        "expires_in_seconds": expires_in,
        "access_token_expired": expired,
    }


def refresh_schwab_access_token():
    """Refresh Schwab access token and store it back in SQLite.

    Returns a structured result instead of raising so API routes can show a
    clear reconnect message when Schwab returns invalid_grant/400.
    """
    import base64
    import requests

    cfg = _get_schwab_config() or {}
    if not cfg.get("app_key") or not cfg.get("app_secret"):
        return {"ok": False, "error": "Schwab app key/secret are missing. Save credentials, then authorize Schwab.",
                "reauthorize_required": True}
    if not cfg.get("refresh_token"):
        return {"ok": False, "error": "No Schwab refresh token is stored. Re-authorize Schwab.",
                "reauthorize_required": True}

    creds = base64.b64encode(f"{cfg['app_key']}:{cfg['app_secret']}".encode()).decode()
    try:
        r = requests.post(
            f"{BASE}/v1/oauth/token",
            headers={"Authorization": f"Basic {creds}",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            data={"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"]},
            timeout=20,
        )
    except Exception as e:
        return {"ok": False, "error": f"Schwab token refresh request failed: {e}",
                "reauthorize_required": False}

    body = r.text or ""
    if r.status_code != 200:
        # 400 commonly means the stored refresh token is invalid/expired or the
        # app credentials no longer match the token. Do not keep retrying.
        return {
            "ok": False,
            "status_code": r.status_code,
            "error": f"Schwab token refresh failed with HTTP {r.status_code}. Re-authorize Schwab in Settings.",
            "detail": body[:500],
            "reauthorize_required": r.status_code in (400, 401, 403),
        }

    try:
        token = r.json()
    except Exception:
        return {"ok": False, "status_code": r.status_code,
                "error": "Schwab token refresh returned non-JSON response.",
                "detail": body[:500], "reauthorize_required": True}

    access_token = token.get("access_token") or ""
    if not access_token:
        return {"ok": False, "error": "Schwab token refresh did not return an access token.",
                "detail": body[:500], "reauthorize_required": True}

    _save_schwab_config(
        access_token=access_token,
        refresh_token=token.get("refresh_token") or cfg.get("refresh_token", ""),
        token_expiry=str(datetime.datetime.now().timestamp() + float(token.get("expires_in", 1800) or 1800)),
    )
    return {"ok": True, "expires_in": token.get("expires_in", 1800)}


def _schwab_headers(auto_refresh=True):
    """Get Schwab auth headers; refresh access token when it is expired/near expiry."""
    cfg = _get_schwab_config() or {}
    token = cfg.get("access_token") or ""

    if auto_refresh and cfg.get("refresh_token"):
        try:
            expiry = float(cfg.get("token_expiry") or 0)
        except Exception:
            expiry = 0
        if not token or expiry <= datetime.datetime.now().timestamp() + 60:
            refreshed = refresh_schwab_access_token()
            if not refreshed.get("ok"):
                _schwab_headers.last_error = refreshed.get("error") or "Schwab token refresh failed"
                _schwab_headers.last_detail = refreshed.get("detail", "")
                _schwab_headers.reauthorize_required = bool(refreshed.get("reauthorize_required"))
                return None
            cfg = _get_schwab_config() or {}
            token = cfg.get("access_token") or ""

    if token:
        _schwab_headers.last_error = ""
        _schwab_headers.last_detail = ""
        _schwab_headers.reauthorize_required = False
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    _schwab_headers.last_error = "Not authenticated - connect Schwab first"
    _schwab_headers.last_detail = ""
    _schwab_headers.reauthorize_required = True
    return None


def _schwab_get(url, params=None):
    """Make authenticated GET to Schwab API with one automatic refresh retry."""
    import requests
    headers = _schwab_headers(auto_refresh=True)
    if not headers:
        err = getattr(_schwab_headers, "last_error", "Not authenticated - connect Schwab first")
        detail = getattr(_schwab_headers, "last_detail", "")
        return None, (err + (f" Detail: {detail}" if detail else ""))
    try:
        r = requests.get(url, headers=headers, params=params, timeout=20)
        if r.status_code == 401:
            refreshed = refresh_schwab_access_token()
            if refreshed.get("ok"):
                headers = _schwab_headers(auto_refresh=False)
                r = requests.get(url, headers=headers, params=params, timeout=20)
            else:
                detail = refreshed.get("detail", "")
                return None, (refreshed.get("error") or "Schwab token expired") + (f" Detail: {detail}" if detail else "")
        if r.status_code != 200:
            return None, f"Schwab HTTP {r.status_code}: {(r.text or '')[:500]}"
        return r.json(), None
    except Exception as e:
        return None, str(e)


def _ensure_table():
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    con.execute("""CREATE TABLE IF NOT EXISTS futures_oi_daily (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol     TEXT NOT NULL,
        contract   TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        settle     REAL DEFAULT 0,
        volume     INTEGER DEFAULT 0,
        oi         INTEGER DEFAULT 0,
        oi_change  INTEGER DEFAULT 0,
        source     TEXT DEFAULT 'schwab',
        fetched_at TEXT DEFAULT '',
        root       TEXT DEFAULT '',
        asset_class TEXT DEFAULT '',
        display_name TEXT DEFAULT '',
        expiry     TEXT DEFAULT '',
        UNIQUE(contract, trade_date)
    )""")
    # Existing user DBs may have been created before fetched_at existed.
    # Add it safely so installing a code update never requires clearing data.
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(futures_oi_daily)").fetchall()}
        for col, ddl in [
            ("fetched_at", "ALTER TABLE futures_oi_daily ADD COLUMN fetched_at TEXT DEFAULT ''"),
            ("root", "ALTER TABLE futures_oi_daily ADD COLUMN root TEXT DEFAULT ''"),
            ("asset_class", "ALTER TABLE futures_oi_daily ADD COLUMN asset_class TEXT DEFAULT ''"),
            ("display_name", "ALTER TABLE futures_oi_daily ADD COLUMN display_name TEXT DEFAULT ''"),
            ("expiry", "ALTER TABLE futures_oi_daily ADD COLUMN expiry TEXT DEFAULT ''"),
        ]:
            if col not in cols:
                con.execute(ddl)
    except Exception:
        pass
    con.execute("CREATE INDEX IF NOT EXISTS idx_foid_contract ON futures_oi_daily(contract)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_foid_date    ON futures_oi_daily(trade_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_foid_symbol_date ON futures_oi_daily(symbol, trade_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_foid_root_date ON futures_oi_daily(root, trade_date)")
    # Directly matches queries filtering by symbol+source (e.g. the reported
    # 3-minute query) -- none of the indexes above cover `source` at all,
    # so a query filtering on it fell back to a full table scan regardless
    # of how large the table has grown.
    con.execute("CREATE INDEX IF NOT EXISTS idx_foid_symbol_source ON futures_oi_daily(symbol, source)")
    con.commit()
    # Refresh the query planner's statistics immediately so it actually
    # starts using the new indexes right away, rather than waiting for
    # SQLite's own internal heuristics to decide it's worth analyzing.
    try:
        con.execute("ANALYZE futures_oi_daily")
        con.commit()
    except Exception:
        pass
    con.close()


def fetch_futures_oi_tastytrade(equity_sym="SPY"):
    """
    Fetch today's front-month futures OI via tastytrade instead of
    Schwab -- added because Schwab's auth/connection has repeatedly gone
    stale unattended (see the Metals OI Gate staleness detector).

    CORRECTED from the first version of this function: that version used
    feed.get_snapshot() (a REST get_market_data() call) and guessed
    contract symbols via _get_quarterly_contracts() (Schwab's symbol
    convention). Both were wrong for this specific case -- confirmed by
    reading realtime_dashboard.py's already-working
    get_futures_open_interest(), which has an explicit comment stating
    the static REST instrument/market-data response has NO open_interest
    field for futures at all; OI only exists in the DXLink STREAMING
    Summary event. That's why every contract came back empty last round
    -- it wasn't a symbol-format or entitlement problem, it was asking
    the wrong endpoint for a field that endpoint doesn't carry.

    This now reuses that existing, already-correct function instead of
    re-deriving the same fix: it resolves the real front-month contract
    via Future.get(product_codes=[...]) (asking tastytrade what's
    actually listed, not guessing a symbol string) and reads OI from the
    DXLink Summary stream. Only covers the front-month contract (not a
    6-contract curve like the Schwab layer) -- front month is what
    Metals OI Gate and the Futures OI Dashboard's front_continuous_series
    actually read, so this covers the case that matters.

    Still genuinely unverified against a live account -- realtime_dashboard.py's
    own docstring says the same. If this fails, the error message now
    comes straight from that function (e.g. "No active future found for
    product code GC" or "No open interest in the Summary event"), not a
    generic one.
    """
    _ensure_table()
    equity_sym = normalize_futures_symbol(equity_sym)
    root = SCHWAB_ROOTS.get(equity_sym.upper())
    if not root:
        return {"ok": False, "error": f"No futures root mapping for {equity_sym}", "source": "tastytrade"}
    meta = SCHWAB_ROOT_META.get(equity_sym.upper(), {})
    product_code = root.lstrip("/")

    try:
        from ..scanners.realtime_dashboard import get_futures_open_interest
    except Exception as e:
        return {"ok": False, "error": f"realtime_dashboard not available: {e}", "source": "tastytrade"}

    result = get_futures_open_interest(product_code)
    if result.get("error"):
        return {"ok": False, "contracts": [], "source": "tastytrade", "symbol": equity_sym,
                "error": f"tastytrade front-month fetch failed for {product_code}: {result['error']}"}

    sym = result.get("symbol") or f"{root}"
    oi = int(result.get("open_interest") or 0)
    price = float(result.get("prev_day_close") or 0)
    expiry = result.get("expiration_date") or ""
    today = datetime.date.today().isoformat()
    fetched_at = datetime.datetime.now().isoformat(timespec="seconds")

    # Best-effort live price/volume on top of the streamed OI -- separate
    # call, separate data path (REST snapshot), allowed to fail
    # independently without losing the OI value that already worked.
    vol = 0
    try:
        from .tastytrade_feed import feed
        snap = feed.get_snapshot(sym)
        if snap.get("status") == "live":
            price = float(snap.get("last") or snap.get("mark") or snap.get("mid") or price)
            vol = int(snap.get("volume") or 0)
    except Exception:
        pass

    if oi <= 0:
        return {"ok": False, "contracts": [], "source": "tastytrade", "symbol": equity_sym,
                "error": f"tastytrade resolved front-month contract {sym} but open_interest was 0/missing "
                         f"-- exchange may not have published today's snapshot yet."}

    con = sqlite3.connect(DB_PATH)
    prev = con.execute(
        "SELECT oi FROM futures_oi_daily WHERE contract=? AND trade_date < ? "
        "ORDER BY trade_date DESC LIMIT 1", (sym, today)).fetchone()
    oi_change = (oi - prev[0]) if prev and prev[0] is not None else 0

    con.execute("""INSERT OR REPLACE INTO futures_oi_daily
        (symbol, contract, trade_date, settle, volume, oi, oi_change, source, fetched_at,
         root, asset_class, display_name, expiry)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (equity_sym.upper(), sym, today, price, vol, oi, oi_change, "tastytrade", fetched_at,
         root, meta.get("asset_class", ""), meta.get("display", equity_sym.upper()), expiry))
    con.commit()
    con.close()

    return {"ok": True, "source": "tastytrade", "symbol": equity_sym, "stored": 1, "root": root,
            "contracts": [{"contract": sym, "date": today, "price": price, "volume": vol,
                            "oi": oi, "oi_change": oi_change, "stored": True,
                            "fetched_at": fetched_at, "expiry": expiry, "root": root,
                            "asset_class": meta.get("asset_class", ""),
                            "display": meta.get("display", equity_sym.upper())}]}


def fetch_futures_oi_schwab(equity_sym="SPY"):
    """
    Fetch today's OI for futures contracts via Schwab quotes API.
    Stores daily snapshot in futures_oi_daily table.
    Returns: {"ok": bool, "contracts": [...], "error": str|None}
    """
    _ensure_table()
    equity_sym = normalize_futures_symbol(equity_sym)
    root = SCHWAB_ROOTS.get(equity_sym.upper())
    if not root:
        return {"ok": False, "error": f"No Schwab futures root for {equity_sym}"}
    meta = SCHWAB_ROOT_META.get(equity_sym.upper(), {})

    contracts = _get_quarterly_contracts(root, count=6)
    symbols   = ",".join(c["symbol"] for c in contracts)
    # Also fetch continuous contract for reference
    symbols   = symbols + f",{root}"

    data, err = _schwab_get(
        f"{BASE}/marketdata/v1/quotes",
        {"symbols": symbols, "fields": "quote,reference"}
    )
    if err:
        return {"ok": False, "contracts": [], "source": "schwab", "symbol": equity_sym,
                "error": err,
                "reauthorize_required": "Re-authorize Schwab" in err or "refresh failed" in err}
    if not isinstance(data, dict):
        return {"ok": False, "contracts": [], "source": "schwab", "symbol": equity_sym,
                "error": "Schwab quotes response was not a JSON object"}

    today       = datetime.date.today().isoformat()
    fetched_at  = datetime.datetime.now().isoformat(timespec="seconds")
    results     = []
    missing     = []
    stored_count = 0
    con         = sqlite3.connect(DB_PATH)

    for c in contracts:
        sym = c["symbol"]
        q   = data.get(sym) or data.get(sym.replace("/", "")) or {}
        if not q:
            missing.append(sym)
        quote_data = q.get("quote", q) if isinstance(q, dict) else {}

        oi     = int(quote_data.get("openInterest", 0) or 0)
        vol    = int(quote_data.get("totalVolume", 0) or
                     quote_data.get("volume", 0) or 0)
        price  = float(quote_data.get("lastPrice", 0) or
                        quote_data.get("mark", 0) or 0)
        desc   = (q.get("reference", {}) or {}).get("description", "") if isinstance(q, dict) else ""
        expiry = c.get("expiry", "")

        # Compute true daily OI change vs the prior stored trading date.
        # Do NOT compare against another snapshot from today; otherwise a same-day
        # re-fetch makes oi_change look like 0 and masks the real day-over-day move.
        prev = con.execute(
            "SELECT oi FROM futures_oi_daily WHERE contract=? AND trade_date < ? "
            "ORDER BY trade_date DESC LIMIT 1", (sym, today)).fetchone()
        oi_change = (oi - prev[0]) if prev and prev[0] is not None else 0

        # Store only rows with real OI. Do not overwrite a good prior Schwab OI
        # snapshot with quote-only/zero-OI data.
        stored = oi > 0
        if stored:
            con.execute("""INSERT OR REPLACE INTO futures_oi_daily
                (symbol, contract, trade_date, settle, volume, oi, oi_change, source, fetched_at,
                 root, asset_class, display_name, expiry)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (equity_sym.upper(), sym, today, price, vol, oi, oi_change, "schwab", fetched_at,
                 root, meta.get("asset_class", ""), meta.get("display", equity_sym.upper()), expiry))
            stored_count += 1

        results.append({
            "contract":  sym,
            "date":      today,
            "price":     price,
            "volume":    vol,
            "oi":        oi,
            "oi_change": oi_change,
            "desc":      desc,
            "stored":    stored,
            "fetched_at": fetched_at,
            "expiry":    expiry,
            "root":      root,
            "asset_class": meta.get("asset_class", ""),
            "display":   meta.get("display", equity_sym.upper()),
        })

    con.commit()
    con.close()

    if stored_count <= 0:
        available = [k for k in data.keys() if k not in ("errors",)]
        extra = f" Available keys: {available[:8]}" if available else ""
        miss = f" Missing: {missing[:5]}" if missing else ""
        return {
            "ok": False,
            "contracts": results,
            "source": "schwab",
            "symbol": equity_sym,
            "date": today,
            "error": f"Schwab returned no usable openInterest for {symbols}.{miss}{extra}",
        }

    return {"ok": True, "contracts": results, "source": "schwab",
            "symbol": equity_sym, "date": today}


def fetch_all_schwab_futures():
    """Fetch OI for all mapped symbols. Called by 8 AM scheduler."""
    results = {}
    for sym in SCHWAB_ROOTS:
        r = fetch_futures_oi_schwab(sym)
        results[sym] = r
        if r.get("ok"):
            total_oi = sum(c.get("oi", 0) for c in r.get("contracts", []))
            print(f"  ✅ Schwab futures {sym}: {len(r['contracts'])} contracts, "
                  f"total OI={total_oi:,}")
        else:
            print(f"  ❌ Schwab futures {sym}: {r.get('error')}")
    return results


def get_latest_oi(equity_sym="SPY", days=90):
    """Get stored OI history for active contracts plus cumulative series.

    This reader is intentionally forgiving so code upgrades do not hide rows
    already stored in a user's SQLite DB.  Older versions may have stored
    contracts with or without the leading slash, and may not have populated
    root/asset_class columns.  We therefore read generated active contracts
    first, then fall back to discovering stored contracts by symbol/root/prefix.
    """
    _ensure_table()
    equity_sym = normalize_futures_symbol(equity_sym)
    root = SCHWAB_ROOTS.get(equity_sym.upper(), "/ES")
    meta = SCHWAB_ROOT_META.get(equity_sym.upper(), {})
    contracts_meta = _get_quarterly_contracts(root, 6)
    generated_contracts = [c["symbol"] for c in contracts_meta]
    expiry_map = {c["symbol"]: c.get("expiry", "") for c in contracts_meta}

    def _variants(ct):
        raw = str(ct or "").upper().strip()
        no = raw.replace("/", "")
        vals = [raw]
        if no:
            vals.extend([no, "/" + no])
        out = []
        for v in vals:
            if v and v not in out:
                out.append(v)
        return out

    def _canon_contract(ct):
        raw = str(ct or "").upper().strip()
        if raw.startswith("/"):
            return raw
        # Schwab futures contracts are root + month + year.  Add slash for roots
        # such as ES, NQ, 6E, GC, CL, etc. if an older row omitted it.
        if raw and any(raw.startswith(r.replace("/", "")) for r in SCHWAB_ROOTS.values()):
            return "/" + raw
        return raw

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Discover stored contracts too.  This fixes cases where the generated active
    # month list changed after an update, while existing DB rows are still valid.
    # Includes 'tastytrade' alongside schwab/cme -- this filter previously
    # excluded it entirely (added before tastytrade existed as a fetch
    # layer), which meant even a correct root-based match here could
    # never surface tastytrade-sourced rows regardless of symbol format.
    root_no = root.replace("/", "")
    discovered = []
    try:
        rows = con.execute(
            """SELECT contract, MAX(trade_date) AS latest_date, MAX(oi) AS max_oi
               FROM futures_oi_daily
               WHERE LOWER(COALESCE(source,'')) IN ('schwab','cme','tastytrade')
                 AND (UPPER(symbol)=?
                  OR UPPER(symbol)=?
                  OR UPPER(root)=?
                  OR UPPER(root)=?
                  OR UPPER(contract) LIKE ?
                  OR UPPER(contract) LIKE ?)
               GROUP BY contract
               ORDER BY latest_date DESC, max_oi DESC
               LIMIT 18""",
            (equity_sym.upper(), root.upper(), root.upper(), root_no.upper(), root.upper() + "%", root_no.upper() + "%")
        ).fetchall()
        discovered = [_canon_contract(r["contract"]) for r in rows if r["contract"]]
    except Exception:
        discovered = []

    # Do not mix the legacy futures_oi table into the Schwab dashboard reader.
    # That legacy table may contain older CME/proxy history and can make the
    # Dashboard show dates that are not present in the current Schwab OI table.
    # CME fallback rows should be migrated into futures_oi_daily by the fetcher
    # before they are displayed as daily futures OI.

    contracts = []
    for ct in generated_contracts + discovered:
        ct = _canon_contract(ct)
        if ct and ct not in contracts:
            contracts.append(ct)

    oi_series = {}
    latest_rows = []
    for ct in contracts:
        vars_ = _variants(ct)
        ph = ",".join("?" for _ in vars_)
        rows = con.execute(
            f"""SELECT trade_date, oi, volume, settle, oi_change, source, fetched_at,
                       COALESCE(expiry, '') as expiry, contract
                FROM futures_oi_daily
                WHERE UPPER(contract) IN ({ph})
                  AND LOWER(COALESCE(source,'')) IN ('schwab','cme','tastytrade')
                ORDER BY trade_date DESC LIMIT ?""",
            tuple(v.upper() for v in vars_) + (int(days or 90),)
        ).fetchall()

        # Use only futures_oi_daily here.  This keeps the Dashboard/
        # Aggregate futures OI panels tied to the same Schwab/CME daily-history
        # store and prevents old proxy/legacy rows from appearing as fresh OI.

        # Reverse to chronological order and dedupe same date if both slash/no-slash
        # rows exist. Prefer the row with larger OI for a date.
        by_date = {}
        for r in rows:
            dt = r["trade_date"]
            if not dt:
                continue
            old = by_date.get(dt)
            if old is None or int(r["oi"] or 0) >= int(old["oi"] or 0):
                by_date[dt] = r
        series = []
        prev_oi = None
        for r in sorted(by_date.values(), key=lambda x: x["trade_date"]):
            oi_val = int(r["oi"] or 0)
            if prev_oi is None:
                try:
                    chg_val = int(r["oi_change"] or 0)
                except Exception:
                    chg_val = 0
            else:
                chg_val = oi_val - prev_oi
            series.append({
                "date": r["trade_date"],
                "oi": oi_val,
                "volume": int(r["volume"] or 0),
                "close": float(r["settle"] or 0),
                "oi_change": chg_val,
                "source": r["source"] or "schwab",
                "fetched_at": r["fetched_at"] or "",
                "expiry": r["expiry"] or expiry_map.get(ct, ""),
            })
            prev_oi = oi_val
        oi_series[ct] = series
        if series:
            latest = dict(series[-1])
            latest.update({"contract": ct, "expiry": latest.get("expiry") or expiry_map.get(ct, "")})
            latest_rows.append(latest)
    con.close()

    # Drop contracts that have no rows from the visible list, but keep the generated
    # front in metadata if needed.
    visible_contracts = [ct for ct in contracts if oi_series.get(ct)]

    by_date = {}
    for ct, rows in oi_series.items():
        if not rows:
            continue
        for r in rows:
            dt = r.get("date")
            if not dt:
                continue
            bucket = by_date.setdefault(dt, {"date": dt, "oi": 0, "volume": 0, "close_sum": 0.0, "close_count": 0})
            bucket["oi"] += int(r.get("oi") or 0)
            bucket["volume"] += int(r.get("volume") or 0)
            if r.get("close"):
                bucket["close_sum"] += float(r.get("close") or 0)
                bucket["close_count"] += 1

    cumulative_series = []
    prev_cum_oi = None
    for r in sorted(by_date.values(), key=lambda x: x["date"]):
        cum_oi = int(r["oi"] or 0)
        cum_chg = 0 if prev_cum_oi is None else cum_oi - prev_cum_oi
        cumulative_series.append({
            "date": r["date"],
            "oi": cum_oi,
            "volume": r["volume"],
            "oi_change": cum_chg,
            "close": round(r["close_sum"] / r["close_count"], 4) if r["close_count"] else 0,
        })
        prev_cum_oi = cum_oi

    active_contract = None
    if latest_rows:
        active_contract = max(latest_rows, key=lambda r: int(r.get("oi") or 0)).get("contract")

    # Front = first generated contract with data; otherwise first visible contract.
    front_contract = next((ct for ct in generated_contracts if oi_series.get(_canon_contract(ct))), None)
    if front_contract:
        front_contract = _canon_contract(front_contract)
    elif visible_contracts:
        front_contract = visible_contracts[0]
    elif generated_contracts:
        front_contract = _canon_contract(generated_contracts[0])

    total_oi = int(cumulative_series[-1]["oi"]) if cumulative_series else 0
    total_oi_change = int(cumulative_series[-1]["oi_change"]) if cumulative_series else 0

    contract_table = []
    table_contracts = visible_contracts or contracts
    for ct in table_contracts:
        latest = next((r for r in latest_rows if r.get("contract") == ct), None)
        contract_table.append({
            "contract": ct,
            "root": root,
            "expiry": (latest or {}).get("expiry") or expiry_map.get(ct, ""),
            "oi": int((latest or {}).get("oi") or 0),
            "oi_change": int((latest or {}).get("oi_change") or 0),
            "volume": int((latest or {}).get("volume") or 0),
            "close": float((latest or {}).get("close") or 0),
            "date": (latest or {}).get("date", ""),
            "source": (latest or {}).get("source", "schwab"),
            "active": ct == active_contract,
            "front": ct == front_contract,
        })

    history_dates = sorted({r.get("date") for rows in oi_series.values() for r in rows if r.get("date")})
    history_rows = sum(len(rows or []) for rows in oi_series.values())

    # Continuous "front month" series: for each date in history, pick whichever
    # contract was nearest-to-expiry with real OI on that date, using each
    # contract's own expiry metadata. This is the standard futures-charting
    # technique for the front-month line so it doesn't go blank/short every
    # time the front contract rolls to a new ticker — it splices the prior
    # front contract's tail onto the new one's history instead of only
    # showing whatever rows exist under today's front-contract symbol.
    def _expiry_sort_key(ct):
        exp = expiry_map.get(ct, "") or ""
        return exp if exp else "9999-99-99"

    rows_by_ct_date = {}
    for ct, rows in oi_series.items():
        for r in rows or []:
            if r.get("date") and int(r.get("oi") or 0) > 0:
                rows_by_ct_date.setdefault(r["date"], []).append((ct, r))

    front_continuous_series = []
    for dt in history_dates:
        candidates = rows_by_ct_date.get(dt) or []
        if not candidates:
            continue
        # soonest-to-expire contract with real OI on this date = that day's front month
        ct, row = min(candidates, key=lambda pair: _expiry_sort_key(pair[0]))
        out = dict(row)
        out["contract"] = ct
        front_continuous_series.append(out)
    # recompute day-over-day oi_change across the spliced series (a change at
    # the exact roll date will reflect the two different contracts, same as
    # how the roll shows up in cumulative OI).
    prev_oi = None
    for r in front_continuous_series:
        oi_val = int(r.get("oi") or 0)
        r["oi_change"] = 0 if prev_oi is None else oi_val - prev_oi
        prev_oi = oi_val

    return {
        "symbol": equity_sym,
        "root": root,
        "display": meta.get("display", equity_sym),
        "label": meta.get("label", equity_sym),
        "asset_class": meta.get("asset_class", ""),
        "contracts": visible_contracts or contracts,
        "front": front_contract,
        "active_contract": active_contract,
        "oi_series": {ct: rows for ct, rows in oi_series.items() if rows},
        "front_continuous_series": front_continuous_series,
        "cumulative_series": cumulative_series,
        "contract_table": contract_table,
        "total_oi": total_oi,
        "total_oi_change": total_oi_change,
        "history_table": "futures_oi_daily",
        "legacy_table_used": False,
        "history_rows": history_rows,
        "history_dates": history_dates,
        "history_start": history_dates[0] if history_dates else "",
        "history_end": history_dates[-1] if history_dates else "",
    }


def analyze_roll_adjusted(equity_sym="SPY", days=5):
    """Roll-adjusted OI signal using Schwab real OI data."""
    _ensure_table()
    equity_sym = normalize_futures_symbol(equity_sym)
    root      = SCHWAB_ROOTS.get(equity_sym.upper(), "/ES")
    meta      = _get_quarterly_contracts(root, 2)
    front     = meta[0]["symbol"]
    back      = meta[1]["symbol"]
    all_meta  = _get_quarterly_contracts(root, 6)

    con = sqlite3.connect(DB_PATH)
    def get_rows(ct):
        return con.execute(
            "SELECT trade_date, oi, volume, settle FROM futures_oi_daily "
            "WHERE contract=? ORDER BY trade_date DESC LIMIT ?",
            (ct, days + 2)).fetchall()

    f_rows = get_rows(front)
    b_rows = get_rows(back)

    # OI series for charts
    oi_series = {}
    for c in all_meta:
        ct = c["symbol"]
        rows = con.execute(
            "SELECT trade_date, oi, settle, volume FROM futures_oi_daily "
            "WHERE contract=? ORDER BY trade_date ASC", (ct,)).fetchall()
        oi_series[ct] = [{"date": r[0], "oi": r[1], "close": r[2],
                           "volume": r[3]} for r in rows[-30:]]
    con.close()

    if len(f_rows) < 1:
        return {
            "signal": "NO_DATA", "front": front, "back": back,
            "note": "Fetch OI first — go to Scheduler tab → ⚡ Fetch OI Now",
            "contracts": [c["symbol"] for c in all_meta],
            "oi_series": oi_series,
            "front_oi_chg": 0, "back_oi_chg": 0, "net_oi_chg": 0,
        }

    # Only 1 day of data — show current OI, change = unknown
    if len(f_rows) < 2:
        f_oi_now = f_rows[0][1] or 0
        b_oi_now = b_rows[0][1] or 0 if b_rows else 0
        return {
            "signal": "FIRST_FETCH",
            "front": front, "back": back,
            "interpretation": (f"First fetch — {front} OI: {f_oi_now:,}   "
                               f"{back} OI: {b_oi_now:,}. "
                               "Check again tomorrow to see OI change direction."),
            "is_roll": False, "roll_note": None,
            "front_oi_chg": 0, "back_oi_chg": 0, "net_oi_chg": 0,
            "front_price": round(float(f_rows[0][3] or 0), 2),
            "front_oi": f_oi_now, "back_oi": b_oi_now,
            "score": 0,
            "contracts": [c["symbol"] for c in all_meta],
            "oi_series": oi_series,
            "data_source": "schwab",
            "is_volume_proxy": False,
        }

    f_now  = f_rows[0][1] or 0;  f_prev = f_rows[1][1] or 0
    b_now  = b_rows[0][1] or 0 if b_rows else 0
    b_prev = b_rows[1][1] or 0 if len(b_rows) > 1 else b_now
    f_chg  = f_now  - f_prev
    b_chg  = b_now  - b_prev
    net    = f_chg  + b_chg

    is_roll = f_chg < 0 and b_chg > 0

    if is_roll:
        signal = "NET_LONG_BUILDUP" if net > 0 else "ROLL"
        interp = (f"Roll in progress. {front} {f_chg:+,} → {back} {b_chg:+,}. "
                  f"NET {net:+,} = {'bullish' if net > 0 else 'neutral'}.")
    elif net > 0:
        signal = "NET_LONG_BUILDUP"
        interp = f"Combined OI rising {net:+,}. Institutions adding longs."
    elif net < 0:
        signal = "NET_LONG_UNWINDING"
        interp = f"Combined OI falling {net:+,}. Position reduction / longs exiting."
    else:
        signal = "NEUTRAL"
        interp = "No net OI change."

    score_map = {"NET_LONG_BUILDUP": 3, "ROLL": 0, "NET_LONG_UNWINDING": -3, "NEUTRAL": 0}
    return {
        "front": front, "back": back, "signal": signal,
        "score": score_map.get(signal, 0),
        "interpretation": interp, "is_roll": is_roll,
        "roll_note": f"Roll: {front}→{back}" if is_roll else None,
        "front_oi_chg": f_chg, "back_oi_chg": b_chg, "net_oi_chg": net,
        "front_price": round(float(f_rows[0][3] or 0), 2),
        "contracts":   [c["symbol"] for c in all_meta],
        "oi_series":   oi_series,
        "data_source": "schwab",
        "is_volume_proxy": False,
    }

# ── Three-layer futures positioning helper ─────────────────────────────────

def _store_cme_rows_into_daily(canonical_symbol, rows):
    """Mirror CME fallback rows into futures_oi_daily so dashboards use one table.

    The legacy futures_oi table remains for backward compatibility, but the
    Dashboard/Aggregate Schwab futures widgets read futures_oi_daily only.
    """
    if not rows:
        return 0
    _ensure_table()
    canonical_symbol = normalize_futures_symbol(canonical_symbol)
    root = SCHWAB_ROOTS.get(canonical_symbol, "/ES")
    meta = SCHWAB_ROOT_META.get(canonical_symbol, {})
    fetched_at = datetime.datetime.now().isoformat(timespec="seconds")
    saved = 0
    con = sqlite3.connect(DB_PATH)
    for r in rows:
        try:
            raw_ct = str(r.get("contract") or "").upper().strip()
            if not raw_ct:
                continue
            ct = raw_ct if raw_ct.startswith("/") else "/" + raw_ct
            trade_date = str(r.get("trade_date") or datetime.date.today().isoformat())[:10]
            oi = int(r.get("oi") or 0)
            if oi <= 0:
                continue
            prev = con.execute(
                "SELECT oi FROM futures_oi_daily WHERE contract=? AND trade_date < ? "
                "ORDER BY trade_date DESC LIMIT 1", (ct, trade_date)
            ).fetchone()
            oi_change = int(r.get("oi_change") or ((oi - prev[0]) if prev and prev[0] is not None else 0))
            con.execute("""INSERT OR REPLACE INTO futures_oi_daily
                (symbol, contract, trade_date, settle, volume, oi, oi_change, source, fetched_at,
                 root, asset_class, display_name, expiry)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (canonical_symbol, ct, trade_date, float(r.get("settle") or r.get("close") or 0),
                 int(r.get("volume") or 0), oi, oi_change, "cme", fetched_at,
                 root, meta.get("asset_class", ""), meta.get("display", canonical_symbol), str(r.get("expiry", ""))))
            saved += 1
        except Exception:
            continue
    con.commit(); con.close()
    return saved


def fetch_futures_oi_three_layer(symbol="SPY", *, use_cme_fallback=True, include_cot=True):
    """Fetch futures positioning using the app's layered approach.

    Layer 0: tastytrade live snapshot (new, preferred -- Schwab's session
             has repeatedly gone stale unattended, tastytrade's is the
             one already kept alive for quotes/greeks elsewhere).
    Layer 1: Schwab real futures OI snapshots (fallback, daily history source).
    Layer 2: CME settlement/VOI fallback where a product mapping exists.
    Layer 3: CFTC COT weekly positioning overlay (macro context, not daily OI).
    """
    key = normalize_futures_symbol(symbol)
    root = SCHWAB_ROOTS.get(key, "/ES")

    result = fetch_futures_oi_tastytrade(key)
    layers = [{"name": "tastytrade", "ok": bool(result.get("ok")),
               "message": result.get("error", "ok") if not result.get("ok") else "stored tastytrade OI rows"}]

    if not result.get("ok"):
        schwab_result = fetch_futures_oi_schwab(key)
        layers.append({"name": "schwab", "ok": bool(schwab_result.get("ok")),
                        "message": schwab_result.get("error", "ok") if not schwab_result.get("ok") else "stored Schwab OI rows"})
        if schwab_result.get("ok"):
            result = schwab_result

    if (not result.get("ok")) and use_cme_fallback:
        try:
            from . import futures_oi as _cme
            cme = _cme.fetch_cme_oi(key)
            rows = cme.get("rows") or []
            legacy_stored = _cme.store_cme_oi(key, rows)
            daily_stored = _store_cme_rows_into_daily(key, rows)
            stored = daily_stored or legacy_stored
            layers.append({"name": "cme", "ok": stored > 0, "stored": stored, "daily_stored": daily_stored, "message": cme.get("error", "ok")})
            if stored > 0:
                result = {"ok": True, "source": "cme", "symbol": key, "contracts": rows, "stored": stored}
        except Exception as e:
            layers.append({"name": "cme", "ok": False, "message": str(e)})

    cot = None
    if include_cot:
        try:
            from .cftc_cot import get_cot_summary
            cot = get_cot_summary(root)
            layers.append({"name": "cftc_cot", "ok": bool(cot.get("found")), "message": "weekly COT overlay available" if cot.get("found") else "no cached COT rows"})
        except Exception as e:
            layers.append({"name": "cftc_cot", "ok": False, "message": str(e)})

    result["layers"] = layers
    result["cot"] = cot
    result["canonical_symbol"] = key
    result["root"] = root
    return result
