import urllib.parse
"""
cftc_cot.py  —  CFTC Commitment of Traders (COT) fetcher and analyser

Data: CFTC Legacy Futures-Only report (free, weekly, Tuesday release)
URL:  https://www.cftc.gov/sites/default/files/files/dea/cotarchives/{year}/futures/deacot{year}.zip

Markets tracked:
  /ES  — S&P 500 Consolidated (CME)
  /NQ  — NASDAQ-100 (CME)
  /RTY — E-mini Russell 2000 (CME)
  /GC  — Gold (COMEX)
  /CL  — Crude Oil (NYMEX)
  /ZN  — 10-Year T-Note (CBOT)

Key columns in the CSV:
  [0]  Market_and_Exchange_Names
  [1]  As_of_Date_in_Form_YYMMDD   (YYMMDD)
  [2]  Report_Date_as_YYYY-MM-DD
  [7]  Open_Interest_All
  [8]  NonComm_Positions_Long_All   (large spec long)
  [9]  NonComm_Positions_Short_All  (large spec short)
  [10] NonComm_Postions_Spread_All  (spreading)
  [11] Comm_Positions_Long_All      (commercial / hedger long)
  [12] Comm_Positions_Short_All     (commercial / hedger short)
  [15] NonRept_Positions_Long_All   (small spec long)
  [16] NonRept_Positions_Short_All  (small spec short)
"""
import sqlite3, csv, io, zipfile, urllib.request, urllib.parse, datetime, json
from pathlib import Path
from flask import Blueprint, jsonify, request

cot_bp  = Blueprint("cot_bp", __name__, url_prefix="/cot")
from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

# ── Market name → futures root mapping ─────────────────────────────────────
MARKET_MAP = {
    "S&P 500 CONSOLIDATED - CHICAGO MERCANTILE EXCHANGE":          "/ES",
    "S&P 500 STOCK INDEX - CHICAGO MERCANTILE EXCHANGE":           "/ES",
    "NASDAQ-100 STOCK INDEX (MINI) - CHICAGO MERCANTILE EXCHANGE": "/NQ",
    "NASDAQ-100 CONSOLIDATED - CHICAGO MERCANTILE EXCHANGE":       "/NQ",
    "RUSSELL 2000 MINI INDEX FUTURES - CHICAGO MERCANTILE EXCHANGE":"/RTY",
    "E-MINI RUSSELL 2000 INDEX - CHICAGO MERCANTILE EXCHANGE":     "/RTY",
    "DJIA CONSOLIDATED - CHICAGO BOARD OF TRADE":                  "/YM",
    "GOLD - COMMODITY EXCHANGE INC.":                              "/GC",
    "SILVER - COMMODITY EXCHANGE INC.":                            "/SI",
    "COPPER-GRADE #1 - COMMODITY EXCHANGE INC.":                   "/HG",
    "PLATINUM - NEW YORK MERCANTILE EXCHANGE":                     "/PL",
    "CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE":       "/CL",
    "NATURAL GAS - NEW YORK MERCANTILE EXCHANGE":                  "/NG",
    "GASOLINE BLENDSTOCK (RBOB) - NEW YORK MERCANTILE EXCHANGE":   "/RB",
    "HEATING OIL - NEW YORK MERCANTILE EXCHANGE":                  "/HO",
    "30-YEAR U.S. TREASURY BONDS - CHICAGO BOARD OF TRADE":        "/ZB",
    "10-YEAR U.S. TREASURY NOTES - CHICAGO BOARD OF TRADE":        "/ZN",
    "5-YEAR U.S. TREASURY NOTES - CHICAGO BOARD OF TRADE":         "/ZF",
    "2-YEAR U.S. TREASURY NOTES - CHICAGO BOARD OF TRADE":         "/ZT",
    "EURO FX - CHICAGO MERCANTILE EXCHANGE":                       "/6E",
    "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE":                  "/6J",
    "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE":                 "/6B",
    "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE":             "/6A",
    "CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE":               "/6C",
    "SWISS FRANC - CHICAGO MERCANTILE EXCHANGE":                   "/6S",
    "NEW ZEALAND DOLLAR - CHICAGO MERCANTILE EXCHANGE":            "/6N",
    "MEXICAN PESO - CHICAGO MERCANTILE EXCHANGE":                  "/6M",
}
TRACKED = set(MARKET_MAP.values())


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ensure_table():
    con = _conn()
    con.execute("""
        CREATE TABLE IF NOT EXISTS cot_weekly (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            contract            TEXT NOT NULL,   -- /ES, /NQ etc.
            report_date         TEXT NOT NULL,   -- YYYY-MM-DD (Tuesday)
            open_interest       INTEGER,
            lspec_long          INTEGER,   -- large spec (non-commercial) long
            lspec_short         INTEGER,   -- large spec short
            lspec_net           INTEGER,   -- lspec_long - lspec_short
            comm_long           INTEGER,   -- commercial (hedger) long
            comm_short          INTEGER,
            comm_net            INTEGER,
            sspec_long          INTEGER,   -- small spec (non-reportable) long
            sspec_short         INTEGER,
            sspec_net           INTEGER,
            lspec_net_chg       INTEGER,   -- week-over-week change in lspec_net
            -- derived / rolling
            cot_index_52w       REAL,      -- (net - 52W_min)/(52W_max - 52W_min)*100
            direction           TEXT,      -- Bullish / Bearish / Neutral
            created_at          TEXT,
            UNIQUE(contract, report_date)
        )
    """)
    con.commit()
    con.close()


# ── Download & parse ────────────────────────────────────────────────────────

# ── CFTC Socrata API (primary) ──────────────────────────────────────────────
# Dataset: Legacy Futures Only | ID: 6dca-aqww
# Docs: https://dev.socrata.com/foundry/publicreporting.cftc.gov/6dca-aqww
SOCRATA_BASE = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

# Search terms to find our markets in the Socrata API
MARKET_SEARCH = {
    "/ES":  "S&P 500",
    "/NQ":  "NASDAQ-100",
    "/RTY": "RUSSELL 2000",
    "/YM":  "DJIA",
    "/GC":  "GOLD",
    "/SI":  "SILVER",
    "/HG":  "COPPER",
    "/PL":  "PLATINUM",
    "/CL":  "CRUDE OIL",
    "/NG":  "NATURAL GAS",
    "/RB":  "GASOLINE",
    "/HO":  "HEATING OIL",
    "/ZB":  "30-YEAR",
    "/ZN":  "10-YEAR",
    "/ZF":  "5-YEAR",
    "/ZT":  "2-YEAR",
    "/6E":  "EURO FX",
    "/6J":  "JAPANESE YEN",
    "/6B":  "BRITISH POUND",
    "/6A":  "AUSTRALIAN DOLLAR",
    "/6C":  "CANADIAN DOLLAR",
    "/6S":  "SWISS FRANC",
    "/6N":  "NEW ZEALAND DOLLAR",
    "/6M":  "MEXICAN PESO",
}

def _cftc_zip_url(year: int) -> str:
    return (f"https://www.cftc.gov/sites/default/files/files/"
            f"dea/cotarchives/{year}/futures/deacot{year}.zip")


def _parse_socrata_row(row: dict, root: str) -> dict | None:
    """Parse a single Socrata JSON row into our internal format."""
    def _int(v):
        try: return int(str(v).replace(",", "").strip())
        except: return 0
    report_date = str(row.get("report_date_as_yyyy_mm_dd", ""))[:10]
    if len(report_date) < 8:
        return None
    ls_long   = _int(row.get("noncomm_positions_long_all", 0))
    ls_short  = _int(row.get("noncomm_positions_short_all", 0))
    comm_long = _int(row.get("comm_positions_long_all", 0))
    comm_short= _int(row.get("comm_positions_short_all", 0))
    ss_long   = _int(row.get("nonrept_positions_long_all", 0))
    ss_short  = _int(row.get("nonrept_positions_short_all", 0))
    return {
        "contract":      root,
        "report_date":   report_date,
        "open_interest": _int(row.get("open_interest_all", 0)),
        "lspec_long":  ls_long,  "lspec_short": ls_short,
        "comm_long":   comm_long, "comm_short":  comm_short,
        "sspec_long":  ss_long,   "sspec_short": ss_short,
        "lspec_net":   ls_long  - ls_short,
        "comm_net":    comm_long - comm_short,
        "sspec_net":   ss_long  - ss_short,
    }


def _make_request(url: str, timeout: int = 45):
    """Make HTTPS request, bypassing SSL cert verification if needed."""
    import ssl, urllib.error
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)

    # First try: normal verified request
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.URLError as e:
        # Only fall through on SSL errors — re-raise other network errors
        cause = str(e.reason) if hasattr(e, "reason") else str(e)
        if "SSL" not in cause.upper() and "CERTIFICATE" not in cause.upper():
            raise

    # Second try: unverified SSL context (fixes macOS cert issues)
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return resp.read()


def _fetch_via_socrata(years: int = 2) -> tuple[list[dict], list[str]]:
    """
    Fetch COT data via CFTC Public Reporting Environment (Socrata API).
    No API key required.
    Returns (rows, errors).
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=years*365 + 30)).strftime("%Y-%m-%d")
    rows  = []
    errors = []

    for root, search_term in MARKET_SEARCH.items():
        offset = 0
        contract_rows = 0
        while True:
            where = (f"upper(market_and_exchange_names) like upper('%25{search_term}%25')"
                     f" AND report_date_as_yyyy_mm_dd >= '{cutoff}'")
            url = SOCRATA_BASE + "?" + urllib.parse.urlencode(
                {
                    "$where":  f"upper(market_and_exchange_names) like upper('%{search_term}%') AND report_date_as_yyyy_mm_dd >= '{cutoff}'",
                    "$order":  "report_date_as_yyyy_mm_dd ASC",
                    "$limit":  "5000",
                    "$offset": str(offset),
                },
                quote_via=urllib.parse.quote, safe="$"
            )
            try:
                batch = json.loads(_make_request(url, timeout=45))
                if not batch:
                    break
                for row in batch:
                    parsed = _parse_socrata_row(row, root)
                    if parsed:
                        rows.append(parsed)
                        contract_rows += 1
                if len(batch) < 5000:
                    break
                offset += 5000
            except Exception as e:
                errors.append(f"{root}: {str(e)[:60]}")
                break
        if contract_rows == 0 and root not in [e.split(":")[0] for e in errors]:
            errors.append(f"{root}: 0 rows matched")
    return rows, errors


def _download_year(year: int) -> list[dict]:
    """Download and parse one year of COT data via zip file. Returns list of row dicts."""
    url = _cftc_zip_url(year)
    try:
        raw = _make_request(url, timeout=30)
    except Exception as e:
        return []

    rows = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            fname = [n for n in zf.namelist() if n.lower().endswith(".txt")][0]
            with zf.open(fname) as f:
                reader = csv.reader(io.TextIOWrapper(f, encoding="latin-1"))
                next(reader)  # skip header
                for line in reader:
                    if len(line) < 17:
                        continue
                    market = line[0].strip().upper()
                    root = None
                    for name, sym in MARKET_MAP.items():
                        if name in market or market in name:
                            root = sym
                            break
                    if not root:
                        # Try partial match via search terms
                        for sym, term in MARKET_SEARCH.items():
                            if term in market:
                                root = sym; break
                    if not root:
                        continue
                    try:
                        report_date = line[2].strip()
                        if not report_date or len(report_date) < 8:
                            continue
                        def _int(v):
                            try: return int(str(v).replace(",","").strip())
                            except: return 0
                        ls_long  = _int(line[8]); ls_short  = _int(line[9])
                        comm_long= _int(line[11]);comm_short = _int(line[12])
                        ss_long  = _int(line[15]);ss_short  = _int(line[16])
                        rows.append({
                            "contract": root, "report_date": report_date,
                            "open_interest": _int(line[7]),
                            "lspec_long": ls_long, "lspec_short": ls_short,
                            "comm_long": comm_long,"comm_short": comm_short,
                            "sspec_long": ss_long,"sspec_short": ss_short,
                            "lspec_net": ls_long - ls_short,
                            "comm_net":  comm_long - comm_short,
                            "sspec_net": ss_long - ss_short,
                        })
                    except Exception:
                        continue
    except Exception:
        pass
    return rows


def _store_rows(rows: list[dict]):
    """Insert/replace rows, compute week-over-week change."""
    if not rows:
        return 0
    con = _conn()
    stored = 0
    now = datetime.datetime.now().isoformat()
    for r in rows:
        # Get previous week's lspec_net for this contract
        prev = con.execute(
            "SELECT lspec_net FROM cot_weekly WHERE contract=? AND report_date < ? "
            "ORDER BY report_date DESC LIMIT 1",
            (r["contract"], r["report_date"])
        ).fetchone()
        chg = (r["lspec_net"] - prev[0]) if prev else 0
        con.execute("""
            INSERT OR REPLACE INTO cot_weekly
            (contract, report_date, open_interest, lspec_long, lspec_short, lspec_net,
             comm_long, comm_short, comm_net, sspec_long, sspec_short, sspec_net,
             lspec_net_chg, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (r["contract"], r["report_date"], r["open_interest"],
              r["lspec_long"], r["lspec_short"], r["lspec_net"],
              r["comm_long"], r["comm_short"], r["comm_net"],
              r["sspec_long"], r["sspec_short"], r["sspec_net"],
              chg, now))
        stored += 1
    con.commit()
    _recompute_cot_index(con)
    con.close()
    return stored


def _recompute_cot_index(con=None):
    """Recompute 52-week COT Index and direction for all rows."""
    close_con = con is None
    if con is None:
        con = _conn()
    contracts = [r[0] for r in con.execute(
        "SELECT DISTINCT contract FROM cot_weekly").fetchall()]
    for sym in contracts:
        rows = con.execute(
            "SELECT id, report_date, lspec_net FROM cot_weekly "
            "WHERE contract=? ORDER BY report_date", (sym,)
        ).fetchall()
        if len(rows) < 2:
            continue
        # Sliding 52-week window (52 rows ≈ 1 year)
        W = 52
        for i, row in enumerate(rows):
            window = rows[max(0, i-W+1): i+1]
            nets   = [w[2] for w in window]
            mn, mx = min(nets), max(nets)
            rng    = mx - mn
            net    = row[2]
            idx    = round((net - mn) / rng * 100, 1) if rng > 0 else 50.0
            direction = "Bullish" if idx >= 60 else "Bearish" if idx <= 40 else "Neutral"
            con.execute(
                "UPDATE cot_weekly SET cot_index_52w=?, direction=? WHERE id=?",
                (idx, direction, row[0])
            )
    con.commit()
    if close_con:
        con.close()


# ── Public fetch function ───────────────────────────────────────────────────

def fetch_cot_data(years: int = 2, force: bool = False) -> dict:
    """
    Download COT data. Strategy:
    1. Try CFTC Socrata API (no auth, works everywhere, returns JSON)
    2. Fallback to annual zip files from cftc.gov
    """
    _ensure_table()

    if not force:
        con = _conn()
        newest = con.execute(
            "SELECT MAX(report_date), MAX(created_at) FROM cot_weekly"
        ).fetchone()
        con.close()
        if newest and newest[1]:
            try:
                fetched = datetime.datetime.fromisoformat(newest[1])
                if (datetime.datetime.now() - fetched).days < 7:
                    return {"ok": True, "skipped": True,
                            "message": f"Already fresh (last fetched {newest[1][:10]}, latest COT: {newest[0]})"}
            except Exception:
                pass

    errors = []

    # ── Strategy 1: Socrata JSON API ──────────────────────────────────────
    soc_rows, soc_errors = _fetch_via_socrata(years=years)
    rows   = soc_rows
    source = "Socrata API"
    errors.extend(soc_errors)

    # ── Strategy 2: Zip file fallback ────────────────────────────────────
    if not rows:
        errors.append("Socrata API returned 0 rows — trying annual zip files")
        current_year = datetime.date.today().year
        for yr in range(current_year - years + 1, current_year + 1):
            yr_rows = _download_year(yr)
            if yr_rows:
                rows.extend(yr_rows)
            else:
                errors.append(f"Zip {yr}: download failed")
        source = "zip files"

    if not rows:
        err_detail = " | ".join(errors[:4]) if errors else "unknown"
        return {
            "ok": False,
            "stored": 0,
            "errors": errors,
            "message": f"Could not download COT data. Details: {err_detail}",
            "debug_url": SOCRATA_BASE + "?$limit=3",
        }

    total_stored = _store_rows(rows)
    return {
        "ok": True,
        "stored": total_stored,
        "source": source,
        "errors": errors,
        "message": f"Stored {total_stored} COT rows via {source} ({years} years, {len(MARKET_SEARCH)} contracts)",
    }


# ── Analysis helpers ────────────────────────────────────────────────────────

def get_cot_summary(contract: str, weeks: int = 52) -> dict:
    """
    Return the latest COT data + historical context for a contract.
    """
    _ensure_table()
    con = _conn()
    rows = con.execute(
        "SELECT * FROM cot_weekly WHERE contract=? "
        "ORDER BY report_date DESC LIMIT ?",
        (contract, weeks)
    ).fetchall()
    con.close()

    if not rows:
        return {"contract": contract, "found": False}

    latest = dict(rows[0])
    history = [dict(r) for r in rows]

    # Trend: is lspec_net rising or falling over last 4 weeks?
    recent_nets = [r["lspec_net"] for r in history[:4] if r["lspec_net"] is not None]
    if len(recent_nets) >= 2:
        trend_4w  = recent_nets[0] - recent_nets[-1]   # positive = rising
        trend_dir = "Rising" if trend_4w > 5000 else "Falling" if trend_4w < -5000 else "Flat"
    else:
        trend_4w = 0; trend_dir = "Flat"

    # COT Index extremes
    cot_idx = latest.get("cot_index_52w", 50)
    extreme = ("Top 10%" if cot_idx >= 90 else "Top 25%" if cot_idx >= 75
               else "Bottom 10%" if cot_idx <= 10 else "Bottom 25%" if cot_idx <= 25
               else "Neutral range")

    return {
        "contract":    contract,
        "found":       True,
        "latest":      latest,
        "cot_index":   cot_idx,
        "direction":   latest.get("direction", "Neutral"),
        "extreme":     extreme,
        "lspec_net":   latest.get("lspec_net", 0),
        "lspec_chg":   latest.get("lspec_net_chg", 0),
        "trend_4w":    trend_4w,
        "trend_dir":   trend_dir,
        "report_date": latest.get("report_date"),
        "history":     history[:12],   # last 12 weeks for chart
    }


def get_combined_signal(contract: str) -> dict:
    """
    Combine Schwab daily OI trend with COT positioning
    into a single directional signal.
    """
    # COT signal
    cot = get_cot_summary(contract)

    # Schwab OI trend (daily, cumulative across all stored active contracts).
    # Do not read the legacy futures_oi table here; this is the same Schwab
    # daily-history table used by the Dashboard Futures OI widget.  Grouping by
    # trade_date prevents contract-roll noise from dominating the COT overlay.
    con = _conn()
    root = "/" + str(contract or "").upper().lstrip("/")
    root_no = root.lstrip("/")
    oi_rows = con.execute(
        """SELECT trade_date, SUM(oi) AS oi, AVG(NULLIF(settle,0)) AS settle
             FROM futures_oi_daily
             WHERE LOWER(COALESCE(source,'')) IN ('schwab','cme','tastytrade')
               AND (UPPER(root)=?
                OR UPPER(root)=?
                OR UPPER(contract) LIKE ?
                OR UPPER(contract) LIKE ?)
             GROUP BY trade_date
             ORDER BY trade_date DESC
             LIMIT 20""",
        (root.upper(), root_no.upper(), root.upper() + "%", root_no.upper() + "%")
    ).fetchall()
    con.close()

    schwab_signal = "No data"
    schwab_score  = 0
    oi_trend_pct  = None
    price_trend_pct = None

    if len(oi_rows) >= 5:
        recent_oi   = [r["oi"]     for r in oi_rows[:5]]
        prior_oi    = [r["oi"]     for r in oi_rows[5:10]] or [r["oi"] for r in oi_rows[-5:]]
        recent_px   = [r["settle"] for r in oi_rows[:5]  if r["settle"]]
        prior_px    = [r["settle"] for r in oi_rows[5:10] if r["settle"]]
        avg_oi_now  = sum(recent_oi)  / len(recent_oi)
        avg_oi_then = sum(prior_oi)   / len(prior_oi)  if prior_oi else avg_oi_now
        avg_px_now  = sum(recent_px)  / len(recent_px)  if recent_px  else 0
        avg_px_then = sum(prior_px)   / len(prior_px)   if prior_px   else avg_px_now
        oi_trend_pct  = round((avg_oi_now - avg_oi_then) / max(1, avg_oi_then) * 100, 1)
        price_trend_pct = round((avg_px_now - avg_px_then) / max(0.01, avg_px_then) * 100, 2) if avg_px_then else 0

        if oi_trend_pct > 3 and price_trend_pct > 0:
            schwab_signal = "OI expanding with price ↑ (New longs)"
            schwab_score  = 8
        elif oi_trend_pct > 3 and price_trend_pct < 0:
            schwab_signal = "OI expanding but price ↓ (New shorts)"
            schwab_score  = 2
        elif oi_trend_pct < -3 and price_trend_pct > 0:
            schwab_signal = "OI shrinking, price ↑ (Short covering)"
            schwab_score  = 6
        elif oi_trend_pct < -3 and price_trend_pct < 0:
            schwab_signal = "OI shrinking, price ↓ (Longs exiting)"
            schwab_score  = 3
        else:
            schwab_signal = "OI flat — no strong directional signal"
            schwab_score  = 5

    # COT score
    cot_score = 5   # neutral default
    if cot.get("found"):
        idx = cot.get("cot_index", 50) or 50
        if idx >= 75:   cot_score = 8
        elif idx >= 60: cot_score = 7
        elif idx <= 25: cot_score = 2
        elif idx <= 40: cot_score = 3
        else:           cot_score = 5
        # Crowded signals invert
        if idx >= 90: cot_score = 4  # crowded long — fade risk
        if idx <= 10: cot_score = 6  # crowded short — squeeze potential

    # Combined score
    if cot.get("found") and schwab_score > 0:
        combined = round((schwab_score * 0.5 + cot_score * 0.5), 1)
    elif schwab_score > 0:
        combined = schwab_score
    else:
        combined = cot_score

    label = ("🔥 Strongly Bullish" if combined >= 8
             else "✅ Bullish"       if combined >= 6.5
             else "⚖ Neutral"       if combined >= 4.5
             else "🔴 Bearish"       if combined >= 3
             else "💀 Strongly Bearish")

    return {
        "contract":        contract,
        "combined_score":  combined,
        "label":           label,
        "schwab": {
            "signal":      schwab_signal,
            "score":       schwab_score,
            "oi_trend_pct":    oi_trend_pct,
            "price_trend_pct": price_trend_pct,
            "data_points": len(oi_rows),
        },
        "cot": cot if cot.get("found") else {"found": False},
    }


# ── Flask routes ────────────────────────────────────────────────────────────

@cot_bp.route("/fetch", methods=["POST"])
def cot_fetch():
    """Trigger COT data download."""
    body  = request.get_json(silent=True, force=True) or {}
    years = int(body.get("years", 2))
    force = bool(body.get("force", False))
    result = fetch_cot_data(years=years, force=force)
    return jsonify(result)


@cot_bp.route("/summary/<contract>")
def cot_summary(contract):
    sym = "/" + contract.upper().lstrip("/")
    return jsonify(get_cot_summary(sym))


@cot_bp.route("/signal/<contract>")
def cot_signal(contract):
    sym = "/" + contract.upper().lstrip("/")
    return jsonify(get_combined_signal(sym))


@cot_bp.route("/all_signals")
def cot_all_signals():
    """Return combined signals for all tracked contracts."""
    contracts = list(MARKET_SEARCH.keys())
    results = {}
    for c in contracts:
        results[c] = get_combined_signal(c)
    return jsonify({"signals": results,
                    "generated_at": datetime.datetime.now().isoformat()})


@cot_bp.route("/status")
def cot_status():
    """Return latest COT data date and row count."""
    try:
        _ensure_table()
        con = _conn()
        row = con.execute(
            "SELECT MAX(report_date) as latest, COUNT(*) as total, "
            "COUNT(DISTINCT contract) as contracts FROM cot_weekly"
        ).fetchone()
        con.close()
        return jsonify({
            "latest_report": row["latest"],
            "total_rows":    row["total"],
            "contracts":     row["contracts"],
            "has_data":      bool(row["latest"]),
        })
    except Exception as e:
        return jsonify({"error": str(e), "has_data": False})


# ── Context enrichment for GEX/Weekly plan ─────────────────────────────────

def get_market_context(equity_sym: str, target_expiry: str | None = None, count: int = 5) -> dict:
    """
    Return a market context dict combining futures/COT with the same actionable
    weekly option-OI view used by the Aggregate screen and Weekly Plan.

    Older code summed every non-expired option expiration and estimated spot as
    an OI-weighted average strike.  That made SPY show deep stale walls like
    505/555/565 and a fake spot estimate around 676.  This version uses:
      - latest available option snapshot date, not date.today() blindly;
      - current/cached spot, not OI-weighted strike;
      - the active weekly expiry window only;
      - actionable strikes around spot so the panel matches Aggregate OI.
    """
    import datetime as _d
    equity_sym = (equity_sym or "SPY").upper().strip()
    try:
        count = max(1, min(10, int(count or 5)))
    except Exception:
        count = 5

    EQUITY_TO_FUTURES = {
        "SPY": "/ES", "QQQ": "/NQ", "IWM": "/RTY", "DIA": "/YM",
        "NVDA": "/ES", "AAPL": "/ES", "TSLA": "/ES", "AMD": "/ES", "MSFT": "/ES",
        "GLD": "/GC", "SLV": "/SI", "USO": "/CL", "NATGAS": "/NG", "TLT": "/ZB",
        "EURUSD": "/6E", "JPYUSD": "/6J", "GBPUSD": "/6B",
    }
    contract = EQUITY_TO_FUTURES.get(equity_sym, "/ES")
    combined = get_combined_signal(contract)

    # Resolve weekly option context from the shared source of truth.
    opt_ctx = {}
    try:
        from .weekly_oi import build_weekly_oi_context, future_expirations, estimate_spot_from_db
        if not target_expiry:
            fut = future_expirations(equity_sym)
            if fut:
                # For the market-context panel, use the same default as Aggregate:
                # first five future expiries unless Weekly Plan passes a target.
                target_expiry = fut[min(len(fut) - 1, count - 1)]
        spot = estimate_spot_from_db(equity_sym)
        opt_ctx = build_weekly_oi_context(equity_sym, target_expiry, spot=spot, count=count, per_side=12, max_expiries=max(5, count))
    except Exception as exc:
        opt_ctx = {"error": str(exc), "aggregate_rows": [], "actionable_rows": []}

    action_totals = opt_ctx.get("totals_actionable") or {}
    all_totals = opt_ctx.get("totals_all") or {}
    call_oi_recent = int(action_totals.get("call_oi") or all_totals.get("call_oi") or 0)
    put_oi_recent = int(action_totals.get("put_oi") or all_totals.get("put_oi") or 0)
    pcr = opt_ctx.get("pcr_actionable") if action_totals.get("total_oi") else opt_ctx.get("pcr_all")
    call_pct = round(call_oi_recent / max(1, call_oi_recent + put_oi_recent) * 100, 1) if (call_oi_recent + put_oi_recent) else None

    # OI trend across recent snapshots, scoped to the same selected expiries.
    oi_trend = None
    try:
        con = _conn()
        exps = opt_ctx.get("expirations") or []
        if exps:
            ph = ",".join("?" for _ in exps)
            rows = con.execute(f"""
                SELECT date,
                       SUM(CASE WHEN type='call' THEN oi ELSE 0 END) call_oi,
                       SUM(CASE WHEN type='put'  THEN oi ELSE 0 END) put_oi
                FROM options
                WHERE symbol=? AND expiration IN ({ph})
                GROUP BY date ORDER BY date DESC LIMIT 6
            """, [equity_sym] + list(exps)).fetchall()
            if len(rows) >= 2:
                latest = rows[0]
                prior = rows[min(len(rows)-1, 4)]
                t1 = int(latest["call_oi"] or 0) + int(latest["put_oi"] or 0)
                t0 = int(prior["call_oi"] or 0) + int(prior["put_oi"] or 0)
                oi_trend = round((t1 - t0) / max(1, t0) * 100.0, 1)
        con.close()
    except Exception:
        try:
            con.close()
        except Exception:
            pass

    call_walls = [
        {"strike": w.get("strike"), "oi": w.get("oi"), "distance_pct": w.get("distance_pct")}
        for w in (opt_ctx.get("call_walls") or [])[:5]
    ]
    put_walls = [
        {"strike": w.get("strike"), "oi": w.get("oi"), "distance_pct": w.get("distance_pct")}
        for w in (opt_ctx.get("put_walls") or [])[:5]
    ]

    # Seller-side read for options: call-heavy overhead is bearish/ceiling risk;
    # put-heavy support is bullish/supportive.  Balanced walls are neutral/range.
    if call_pct is None:
        options_bias = "No Data"
    elif pcr is not None and float(pcr) >= 1.15 and put_walls:
        options_bias = "Bullish Support / Put Sellers"
    elif pcr is not None and float(pcr) <= 0.85 and call_walls:
        options_bias = "Bearish Resistance / Call Sellers"
    elif call_walls and put_walls:
        options_bias = "Range / Two-sided Walls"
    elif call_walls:
        options_bias = "Bearish Resistance"
    elif put_walls:
        options_bias = "Bullish Support"
    else:
        options_bias = "Neutral"

    futures_dir = combined.get("label", "⚖ Neutral")
    options_bias_score = (
        7 if "Bullish" in options_bias else
        3 if "Bearish" in options_bias else
        5 if options_bias in {"Range / Two-sided Walls", "Neutral"} else
        4
    )
    futures_score = combined.get("combined_score", 5)
    holistic_score = round((futures_score * 0.5 + options_bias_score * 0.5), 1)
    holistic = ("🔥 Strongly Bullish"  if holistic_score >= 8
                else "✅ Bullish"       if holistic_score >= 6.5
                else "⚖ Neutral"       if holistic_score >= 4.5
                else "🔴 Bearish"       if holistic_score >= 3
                else "💀 Strongly Bearish")

    return {
        "equity_sym": equity_sym,
        "futures_contract": contract,
        "holistic_score": holistic_score,
        "holistic_label": holistic,
        "futures_signal": combined,
        "options": {
            "pcr": pcr,
            "pcr_all": opt_ctx.get("pcr_all"),
            "pcr_actionable": opt_ctx.get("pcr_actionable"),
            "call_pct": call_pct,
            "oi_trend_pct": oi_trend,
            "bias": options_bias,
            "call_walls": call_walls,
            "put_walls": put_walls,
            "raw_call_walls": opt_ctx.get("raw_call_walls", [])[:5],
            "raw_put_walls": opt_ctx.get("raw_put_walls", [])[:5],
            "spot_est": round(float(opt_ctx.get("spot")), 2) if opt_ctx.get("spot") else None,
            "call_oi": call_oi_recent,
            "put_oi": put_oi_recent,
            "call_oi_all": all_totals.get("call_oi"),
            "put_oi_all": all_totals.get("put_oi"),
            "expirations": opt_ctx.get("expirations") or [],
            "latest_dates": opt_ctx.get("latest_dates") or {},
            "target_expiry": opt_ctx.get("target_expiry") or target_expiry,
            "selected_strikes": opt_ctx.get("selected_strikes") or [],
            "max_pain": opt_ctx.get("actionable_max_pain") or opt_ctx.get("target_max_pain") or opt_ctx.get("aggregate_max_pain"),
            "source": "weekly_oi_context/actionable weekly aggregate",
        },
        "has_futures_data": combined.get("schwab", {}).get("data_points", 0) > 0,
        "has_cot_data": (combined.get("cot") or {}).get("found", False),
        "has_options_data": bool(opt_ctx.get("actionable_rows") or opt_ctx.get("aggregate_rows")),
    }


@cot_bp.route("/context/<equity_sym>")
def market_context(equity_sym):
    """Full market context for GEX/Weekly plan enrichment."""
    target_expiry = request.args.get("target_expiry") or request.args.get("expiry")
    try:
        count = int(request.args.get("count", 5))
    except Exception:
        count = 5
    return jsonify(get_market_context(equity_sym.upper(), target_expiry=target_expiry, count=count))


@cot_bp.route("/test")
def cot_test():
    """Test CFTC Socrata API connectivity and return the first 2 rows."""
    import urllib.parse as _up
    test_url = SOCRATA_BASE + "?" + _up.urlencode(
        {"$limit": "2", "$order": "report_date_as_yyyy_mm_dd DESC"},
        quote_via=_up.quote, safe="$"
    )
    try:
        data = json.loads(_make_request(test_url, timeout=15))
        return jsonify({
            "ok": True,
            "url": test_url,
            "rows_returned": len(data),
            "sample_keys": list(data[0].keys())[:8] if data else [],
            "sample": data[0] if data else {},
            "message": "CFTC Socrata API is reachable ✅"
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "url": test_url,
            "error": str(e),
            "message": "CFTC Socrata API is not reachable — check firewall/internet connection",
        })

