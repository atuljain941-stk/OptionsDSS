"""
earnings_calendar.py — Fetch and cache earnings dates for all watchlist symbols.
Runs as a scheduled job (weekly) to pre-populate the DB so pre/post earnings
scans can filter symbols instantly without scanning all 200 symbols via yfinance.
"""
import sqlite3, json, math, threading, time
from pathlib import Path
from datetime import date, datetime, timedelta
from flask import Blueprint, jsonify, request

earn_cal_bp = Blueprint("earn_cal_bp", __name__, url_prefix="/earnings")
from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH

# Same async progress-tracking shape as technical_snapshot.py's bulk-compute
# and the price fetch -- one running background op at a time, polled by the
# frontend, instead of a synchronous request that blocks for 1-3 minutes
# with only a client-side elapsed-time timer (no real progress) to show for it.
_earn_lock = None
_earn_status = {"running": False, "processed": 0, "total": 0, "calendar_updated": 0,
                 "calendar_skipped": 0, "calendar_failed": 0,
                 "fundamentals_fetched": 0, "fundamentals_skipped": 0, "fundamentals_errored": 0}

# ── DB helpers ─────────────────────────────────────────────────────────────
def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

def _ensure_calendar_table():
    con = _conn()
    con.execute("""
        CREATE TABLE IF NOT EXISTS earnings_calendar (
            symbol              TEXT PRIMARY KEY,
            next_earn_date      TEXT,
            next_earn_confirmed INTEGER DEFAULT 0,
            last_earn_date      TEXT,
            last_eps_actual     REAL,
            last_eps_estimate   REAL,
            last_surprise_pct   REAL,
            earn_reaction_pct   REAL,
            surprise_streak     INTEGER DEFAULT 0,
            earn_score          INTEGER DEFAULT 0,
            fetch_date          TEXT
        )
    """)
    try:
        con.execute("ALTER TABLE earnings_calendar ADD COLUMN earn_score INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        con.execute("ALTER TABLE earnings_calendar ADD COLUMN quarter_trend TEXT")
    except Exception:
        pass
    try:
        con.execute("ALTER TABLE earnings_calendar ADD COLUMN quarterly_metrics TEXT")
    except Exception:
        pass
    con.commit(); con.close()


def _get_watchlist():
    try:
        con = _conn()
        rows = con.execute(
            "SELECT symbol FROM symbols WHERE symbol IS NOT NULL ORDER BY symbol"
        ).fetchall()
        con.close()
        return [r[0] for r in rows]
    except:
        return []


def _fetch_next_date_only(sym):
    """Lightweight discovery fetch: JUST the next/last earnings date,
    using only tk.calendar and tk.earnings_dates -- 2 network calls
    instead of the ~5 _fetch_one_symbol makes (which also pulls EPS
    history, surprise%, and a price-reaction window). Used for symbols
    whose next_earn_date is unknown, so a full watchlist's worth of
    "we don't even know when this reports" doesn't cost 5x the network
    calls it actually needs to just find out. Returns
    {"next_earn_date": ..., "last_earn_date": ..., "confirmed": 0/1}
    or None on total failure."""
    try:
        import yfinance as yf
        tk = yf.Ticker(sym)
        today_s = date.today().isoformat()
        next_date = None; last_date = None; confirmed = 0

        try:
            cal = tk.calendar
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date") or cal.get("earningsDate")
                if ed:
                    d = ed[0] if isinstance(ed, (list, tuple)) else ed
                    if hasattr(d, "date"): d = d.date()
                    ds = str(d)[:10]
                    if ds >= today_s: next_date = ds
                    else:             last_date  = ds
                    confirmed = 1
        except Exception:
            pass

        if not next_date:
            try:
                ed_df = tk.earnings_dates
                if ed_df is not None and not ed_df.empty:
                    future = [i for i in ed_df.index if str(i.date()) >= today_s]
                    past   = [i for i in ed_df.index if str(i.date()) <  today_s]
                    if future: next_date = str(min(future).date())
                    if past:   last_date  = str(max(past).date())
            except Exception:
                pass

        if next_date is None and last_date is None:
            return None
        return {"next_earn_date": next_date, "last_earn_date": last_date, "confirmed": confirmed}
    except Exception:
        return None


def _fetch_one_symbol(sym):
    """Fetch earnings info for a single symbol. Returns dict or None."""
    try:
        import yfinance as yf
        tk   = yf.Ticker(sym)
        today_s = date.today().isoformat()

        # ── Earnings dates ──────────────────────────────────────────────
        next_date = None; last_date = None; confirmed = 0

        try:
            cal = tk.calendar
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date") or cal.get("earningsDate")
                if ed:
                    d = ed[0] if isinstance(ed, (list, tuple)) else ed
                    if hasattr(d, "date"): d = d.date()
                    ds = str(d)[:10]
                    if ds >= today_s: next_date = ds
                    else:             last_date  = ds
                    confirmed = 1
        except: pass

        if not next_date:
            try:
                ed_df = tk.earnings_dates
                if ed_df is not None and not ed_df.empty:
                    future = [i for i in ed_df.index if str(i.date()) >= today_s]
                    past   = [i for i in ed_df.index if str(i.date()) <  today_s]
                    if future: next_date = str(min(future).date())
                    if past:   last_date  = str(max(past).date())
            except: pass

        # ── EPS history via earnings_dates DataFrame ────────────────────
        last_actual = last_est = surprise_pct = streak = None
        quarter_trend = None
        eps_by_position = None
        try:
            ed_all = tk.get_earnings_dates(limit=12)
            if ed_all is not None and not ed_all.empty:
                # Only past rows (Reported EPS not null)
                past = ed_all[ed_all["Reported EPS"].notna()].sort_index()
                if not past.empty:
                    r = past.iloc[-1]  # most recent reported
                    last_actual = round(float(r.get("Reported EPS", 0) or 0), 4)
                    last_est    = round(float(r.get("EPS Estimate", 0) or 0), 4)
                    if not last_date:
                        # last_date came from tk.calendar/tk.earnings_dates
                        # above, which are SEPARATE yfinance calls from this
                        # one -- if either of those failed or returned
                        # nothing useful while this call succeeded, we'd
                        # otherwise end up with valid EPS actual/estimate/
                        # surprise data but no last_date to go with it,
                        # which silently excludes the symbol from
                        # get_recent_symbols()'s `WHERE last_earn_date
                        # BETWEEN ? AND ?` filter (SQL NULL BETWEEN never
                        # matches) even though it clearly did report. The
                        # row's own index IS the earnings date for that
                        # report, and we already have it from this same
                        # fetch, so there's no reason to leave it unset.
                        try:
                            last_date = str(r.name.date() if hasattr(r.name, "date") else r.name)[:10]
                        except Exception:
                            pass
                    surp_col    = r.get("Surprise(%)") or r.get("Surprise(%)", None)
                    if surp_col is not None:
                        surprise_pct = round(float(surp_col), 2)
                    elif last_est and last_est != 0:
                        surprise_pct = round((last_actual - last_est) / abs(last_est) * 100, 2)
                    # Beat streak: count consecutive quarters where actual >= estimate.
                    # quarter_trend captures up to 4 quarters regardless of
                    # where the streak breaks -- these are two different
                    # things (streak = how many IN A ROW, trend = what
                    # actually happened each of the last 4), so the trend
                    # loop doesn't stop just because the streak loop did.
                    streak = 0
                    streak_done = False
                    quarter_trend = []  # last 4 quarters, most recent first: True=beat, False=miss
                    eps_by_position = []  # up to 8 quarters, most recent first, each {"date": ..., "eps": ...} -- matched to quarterly_metrics BY DATE below, not raw array position
                    for idx, row in past.iloc[::-1].iterrows():
                        a = float(row.get("Reported EPS", 0) or 0)
                        e = float(row.get("EPS Estimate", 0) or 0)
                        beat = (e != 0 and a >= e)
                        if len(quarter_trend) < 4:
                            quarter_trend.append(beat)
                        if len(eps_by_position) < 8:
                            try:
                                eps_date = idx.date() if hasattr(idx, "date") else None
                            except Exception:
                                eps_date = None
                            eps_by_position.append({"date": eps_date, "eps": a})
                        if not streak_done:
                            if e != 0 and a >= e:
                                streak += 1
                            else:
                                streak_done = True
                        if len(quarter_trend) >= 4 and streak_done and len(eps_by_position) >= 8:
                            break
        except: pass
        # ── Quarterly Revenue / Net Income / Operating Margin history ────
        # Same quarterly_income_stmt pattern already proven in
        # analyze_symbol()'s revenue-history pull -- extended here to also
        # pull Net Income and Operating Income so Revenue/Profit/OPM can
        # all be tracked quarter-by-quarter alongside EPS, for the market-
        # wide QoQ/YoY breadth dashboard. This is ACTUAL-vs-ACTUAL
        # (this quarter vs last quarter, this quarter vs the same quarter
        # a year ago) -- not vs analyst estimate, which (as found while
        # building the per-symbol Revenue column) isn't reliably available
        # for a specific already-reported quarter from base yfinance.
        # Actual-vs-actual sidesteps that entirely and only needs the
        # income statement, which is reliable.
        quarterly_metrics = None
        try:
            qi = tk.quarterly_income_stmt
            if qi is not None and not qi.empty:
                rev_label = next((l for l in qi.index if "Revenue" in str(l) and "Cost" not in str(l)), None)
                ni_label  = next((l for l in qi.index if str(l) == "Net Income"), None) \
                            or next((l for l in qi.index if "Net Income" in str(l) and "Common" not in str(l) and "Discontinuous" not in str(l)), None)
                oi_label  = next((l for l in qi.index if str(l) == "Operating Income"), None) \
                            or next((l for l in qi.index if "Operating Income" in str(l)), None)
                if rev_label:
                    cols = list(qi.columns)[:8]  # newest first, up to 8 quarters -- enough for QoQ (1 back) and YoY (4 back)
                    quarterly_metrics = []
                    for col in cols:
                        try:
                            period = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)[:10]
                        except Exception:
                            period = str(col)[:10]
                        rev_v = qi.loc[rev_label, col] if rev_label else None
                        ni_v  = qi.loc[ni_label, col] if ni_label else None
                        oi_v  = qi.loc[oi_label, col] if oi_label else None
                        rev_f = float(rev_v) if rev_v is not None and rev_v == rev_v else None  # NaN check
                        ni_f  = float(ni_v) if ni_v is not None and ni_v == ni_v else None
                        oi_f  = float(oi_v) if oi_v is not None and oi_v == oi_v else None
                        opm_f = round(oi_f / rev_f * 100, 2) if (oi_f is not None and rev_f not in (None, 0)) else None
                        quarterly_metrics.append({
                            "period": period, "revenue": rev_f, "net_income": ni_f, "op_margin": opm_f,
                        })
        except Exception:
            pass

        if quarterly_metrics and eps_by_position:
            # Match by NEAREST DATE, not raw position -- earnings_dates
            # and quarterly_income_stmt are two independent yfinance
            # sources with their own coverage/gaps per symbol; matching
            # by index alone would silently misalign EPS against the
            # wrong financial quarter if either source is missing an
            # entry the other has. 45 days covers normal fiscal-quarter-
            # end vs reported-date offsets without matching across
            # completely different quarters.
            for m in quarterly_metrics:
                try:
                    m_date = date.fromisoformat(m["period"])
                except Exception:
                    m["eps"] = None
                    continue
                best, best_diff = None, None
                for e in eps_by_position:
                    if e["date"] is None:
                        continue
                    diff = abs((e["date"] - m_date).days)
                    if best_diff is None or diff < best_diff:
                        best, best_diff = e, diff
                m["eps"] = best["eps"] if (best is not None and best_diff <= 45) else None
        elif quarterly_metrics:
            for m in quarterly_metrics:
                m["eps"] = None

        if last_actual is None:
            try:
                eh = tk.earnings_history
                if eh is not None and not eh.empty:
                    r = eh.iloc[0]
                    last_actual  = round(float(r.get("epsActual", 0) or 0), 4)
                    last_est     = round(float(r.get("epsEstimate", 0) or 0), 4)
                    if not last_date:
                        # Same fix as above -- this row's own index is the
                        # earnings date, and we already have it from this
                        # fetch, so backfill rather than leave last_date
                        # null while EPS data is clearly present.
                        try:
                            last_date = str(r.name.date() if hasattr(r.name, "date") else r.name)[:10]
                        except Exception:
                            pass
                    surp = r.get("surprisePercent")
                    if surp is not None: surprise_pct = round(float(surp)*100, 2)
                    streak = 0
                    for _, row in eh.iterrows():
                        a = float(row.get("epsActual", 0) or 0)
                        e = float(row.get("epsEstimate", 0) or 0)
                        if e != 0 and a >= e: streak += 1
                        else: break
            except: pass

        # ── Post-earnings price reaction ─────────────────────────────────
        reaction = None
        if last_date:
            try:
                from datetime import timedelta as _td
                dt  = datetime.strptime(last_date, "%Y-%m-%d")
                pre = dt - _td(days=1)
                aft = dt + _td(days=1)
                ph  = tk.history(start=pre.strftime("%Y-%m-%d"),
                                  end=(aft + _td(days=1)).strftime("%Y-%m-%d"),
                                  interval="1d")
                if ph is not None and len(ph) >= 2:
                    closes = ph["Close"].tolist()
                    reaction = round((closes[-1] - closes[0]) / closes[0] * 100, 2)
            except: pass

        rec = {
            "symbol": sym,
            "next_earn_date": next_date,
            "next_earn_confirmed": confirmed,
            "last_earn_date": last_date,
            "last_eps_actual": last_actual,
            "last_eps_estimate": last_est,
            "last_surprise_pct": surprise_pct,
            "earn_reaction_pct": reaction,
            "surprise_streak": streak,
            "quarter_trend": json.dumps(quarter_trend) if quarter_trend is not None else None,
            "quarterly_metrics": json.dumps(quarterly_metrics) if quarterly_metrics is not None else None,
            "earn_score": None,
            "fetch_date": date.today().isoformat(),
        }
        rec["earn_score"] = _compute_earn_score(rec)
        return rec
    except:
        return None


def refresh_calendar(symbols=None, force=False, progress_cb=None):
    """
    Fetch and store earnings calendar for symbols, in two phases:

    Phase 1 (cheap, ~2 calls/symbol): for any symbol whose next_earn_date
    is unknown (never fetched), run the lightweight date-only discovery
    fetch. Symbols with a KNOWN future date are skipped entirely here --
    no network call at all.

    Phase 2 (expensive, ~5 calls/symbol): the full fetch (EPS actual/
    estimate, surprise%, reaction, streak) runs ONLY for symbols now
    confirmed due -- next_earn_date is today, in the past, or still
    unknown after phase 1 tried. Then updates next_earn_date so they
    won't be refetched again until THAT date arrives.

    This two-phase split exists because a single-phase "fetch everyone
    with no known date" approach makes ~5 network calls per symbol
    regardless of whether that symbol is anywhere near reporting --
    across a watchlist of hundreds of symbols this reliably runs into
    yfinance rate limiting partway through, and everything after that
    point fails silently (0 updated, not counted as skipped either).
    Splitting date-discovery (cheap) from full fetch (expensive, and
    only for the small subset actually due) keeps the real call volume
    proportional to how many symbols genuinely need a full refresh.

    progress_cb(done_count), if provided, is called once per symbol as
    phase 1 completes for it, and again once per symbol as phase 2
    completes for it -- so total progress ticks = len(symbols) +
    len(due_symbols), reflecting both phases' actual work.
    Returns (updated, skipped, failed) counts.
    """
    _ensure_calendar_table()
    if symbols is None:
        symbols = _get_watchlist()

    today = date.today().isoformat()
    con     = _conn()
    updated = skipped = failed = 0
    ticks = 0

    def _tick(total_hint=None):
        nonlocal ticks
        ticks += 1
        if progress_cb:
            progress_cb(ticks, total_hint)

    # ── Phase 1: cheap date-discovery for symbols with no known date ──
    due_symbols = []
    for sym in symbols:
        row = con.execute(
            "SELECT next_earn_date FROM earnings_calendar WHERE symbol=?", (sym,)
        ).fetchone()
        known_date = row[0] if row else None

        if not force and known_date and known_date > today:
            skipped += 1
            _tick()
            continue

        if known_date is None or force:
            disc = _fetch_next_date_only(sym)
            if disc and disc.get("next_earn_date"):
                con.execute("""
                    INSERT INTO earnings_calendar (symbol, next_earn_date, next_earn_confirmed, last_earn_date, fetch_date)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        next_earn_date=excluded.next_earn_date,
                        next_earn_confirmed=excluded.next_earn_confirmed,
                        last_earn_date=COALESCE(excluded.last_earn_date, earnings_calendar.last_earn_date),
                        fetch_date=excluded.fetch_date
                """, (sym, disc["next_earn_date"], disc.get("confirmed", 0), disc.get("last_earn_date"), today))
                con.commit()
                known_date = disc["next_earn_date"]
            elif disc and disc.get("last_earn_date"):
                # No FUTURE date found but a past one exists -- the symbol
                # likely just reported and yfinance hasn't posted the next
                # date yet. Treat as due so the full fetch below picks up
                # the recent results now instead of waiting indefinitely.
                known_date = disc["last_earn_date"]

        if force or known_date is None or known_date <= today:
            due_symbols.append(sym)
        else:
            skipped += 1
        _tick()

    true_total = len(symbols) + len(due_symbols)

    # ── Phase 2: full (expensive) fetch, ONLY for symbols actually due ──
    for sym in due_symbols:
        rec = _fetch_one_symbol(sym)
        if rec:
            con.execute("""
                INSERT OR REPLACE INTO earnings_calendar
                (symbol, next_earn_date, next_earn_confirmed, last_earn_date,
                 last_eps_actual, last_eps_estimate, last_surprise_pct,
                 earn_reaction_pct, surprise_streak, quarter_trend, quarterly_metrics, earn_score, fetch_date)
                VALUES
                (:symbol, :next_earn_date, :next_earn_confirmed, :last_earn_date,
                 :last_eps_actual, :last_eps_estimate, :last_surprise_pct,
                 :earn_reaction_pct, :surprise_streak, :quarter_trend, :quarterly_metrics, :earn_score, :fetch_date)
            """, rec)
            con.commit()
            updated += 1
        else:
            failed += 1
        _tick(true_total)

    con.close()
    return updated, skipped, failed


# ── Helpers used by pre/post earnings scans ────────────────────────────────
def get_upcoming_symbols(days_ahead=30):
    """Return [(symbol, next_earn_date)] with earnings in next days_ahead."""
    _ensure_calendar_table()
    today = date.today().isoformat()
    cutoff = (date.today() + timedelta(days=days_ahead)).isoformat()
    con = _conn()
    rows = con.execute("""
        SELECT symbol, next_earn_date, next_earn_confirmed,
               last_eps_actual, last_eps_estimate, last_surprise_pct,
               earn_reaction_pct, surprise_streak, earn_score
        FROM earnings_calendar
        WHERE next_earn_date BETWEEN ? AND ?
        ORDER BY next_earn_date
    """, (today, cutoff)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def compute_market_monitor(watchlist_id=None):
    """
    Aggregates each symbol's quarterly_metrics (EPS/Revenue/Net Income/
    Op Margin history, from _fetch_one_symbol) into a market-wide QoQ/
    YoY earnings-breadth dashboard: for the most recent 3 calendar
    quarters, how many companies' EPS/Revenue/Profit/OPM improved
    quarter-over-quarter and year-over-year, plus a per-sector
    composite score (out of 8 = 4 metrics x 2 comparison bases).

    Two things worth being explicit about, since they're real
    simplifications rather than hidden precision:

    1. Quarter buckets are CALENDAR quarters (Jan-Mar=Q1, Apr-Jun=Q2,
       etc), not each company's own fiscal quarter -- companies report
       on staggered schedules throughout a calendar quarter, so
       "ongoing quarter" means "the most recent calendar quarter with
       ANY companies reported so far," and its beat-count denominators
       are expected to grow as more filers report over time (matching
       how this kind of breadth dashboard naturally behaves).

    2. "Beat" here is ACTUAL vs ACTUAL -- this quarter's reported
       number vs last quarter's (QoQ) or the same quarter a year ago
       (YoY) -- not vs analyst consensus. A reliable analyst estimate
       for a specific already-reported quarter's Revenue/Profit/OPM
       isn't available from base yfinance (the same limitation found
       fixing the per-symbol Revenue column). EPS does have an
       estimate available, but this dashboard uses the same actual-vs-
       actual basis for all 4 metrics for internal consistency.
    """
    _ensure_calendar_table()
    con = _conn()
    if watchlist_id:
        rows = con.execute("""
            SELECT ec.symbol, ec.quarterly_metrics FROM earnings_calendar ec
            JOIN watchlist_symbols ws ON ws.symbol = ec.symbol
            WHERE ws.watchlist_id=? AND ec.quarterly_metrics IS NOT NULL
        """, (watchlist_id,)).fetchall()
    else:
        rows = con.execute(
            "SELECT symbol, quarterly_metrics FROM earnings_calendar WHERE quarterly_metrics IS NOT NULL"
        ).fetchall()
    con.close()

    from ..services.sector_service import get_symbol_sector

    def _quarter_key(dt):
        return (dt.year, (dt.month - 1) // 3 + 1)

    def _quarter_label(key):
        return f"Q{key[1]} {key[0]}"

    def _shift_key(key, n):
        y, q = key
        idx = y * 4 + (q - 1) - n
        return (idx // 4, idx % 4 + 1)

    symbol_quarters, symbol_sectors = {}, {}
    for r in rows:
        sym = r["symbol"]
        try:
            qm = json.loads(r["quarterly_metrics"] or "null")
        except Exception:
            qm = None
        if not qm:
            continue
        qdict = {}
        for m in qm:
            try:
                dt = datetime.strptime(m["period"], "%Y-%m-%d")
            except Exception:
                continue
            qdict[_quarter_key(dt)] = m
        if qdict:
            symbol_quarters[sym] = qdict
            try:
                symbol_sectors[sym] = get_symbol_sector(sym) or "other"
            except Exception:
                symbol_sectors[sym] = "other"

    if not symbol_quarters:
        return {"quarters": [], "error": "No quarterly metrics fetched yet -- run Scan New / Fetch All first"}

    all_keys = set()
    for qdict in symbol_quarters.values():
        all_keys.update(qdict.keys())
    ongoing_key = max(all_keys)

    metrics = ["eps", "revenue", "net_income", "op_margin"]
    quarter_defs = [
        ("ongoing quarter", ongoing_key),
        ("prior quarter", _shift_key(ongoing_key, 1)),
        ("same quarter last year", _shift_key(ongoing_key, 4)),
    ]

    quarter_rows = []
    for label, key in quarter_defs:
        qoq_key, yoy_key = _shift_key(key, 1), _shift_key(key, 4)
        counts = {"qoq": {m: [0, 0] for m in metrics}, "yoy": {m: [0, 0] for m in metrics}}
        sector_scores = {}

        for sym, qdict in symbol_quarters.items():
            cur = qdict.get(key)
            if not cur:
                continue
            qoq_ref, yoy_ref = qdict.get(qoq_key), qdict.get(yoy_key)
            sec = sector_scores.setdefault(symbol_sectors.get(sym, "other"), [0, 0])
            for m in metrics:
                cv = cur.get(m)
                if cv is None:
                    continue
                if qoq_ref and qoq_ref.get(m) is not None:
                    counts["qoq"][m][1] += 1
                    sec[1] += 1
                    if cv > qoq_ref[m]:
                        counts["qoq"][m][0] += 1
                        sec[0] += 1
                if yoy_ref and yoy_ref.get(m) is not None:
                    counts["yoy"][m][1] += 1
                    sec[1] += 1
                    if cv > yoy_ref[m]:
                        counts["yoy"][m][0] += 1
                        sec[0] += 1

        breadth_total = sum(1 for qd in symbol_quarters.values() if key in qd)
        adv = counts["qoq"]["eps"][0]
        dec = counts["qoq"]["eps"][1] - adv

        sector_avg = [
            {"sector": s, "score": round(b / t * 8, 1), "beats": b, "total": t}
            for s, (b, t) in sector_scores.items() if t > 0
        ]
        sector_avg.sort(key=lambda x: -x["score"])

        quarter_rows.append({
            "label": label, "quarter": _quarter_label(key),
            "qoq": {m: {"beats": counts["qoq"][m][0], "total": counts["qoq"][m][1]} for m in metrics},
            "yoy": {m: {"beats": counts["yoy"][m][0], "total": counts["yoy"][m][1]} for m in metrics},
            "breadth": {"advance": adv, "decline": dec, "total": breadth_total},
            "strongest_sectors": sector_avg[:5],
            "weakest_sectors": sorted(sector_avg, key=lambda x: x["score"])[:5],
        })

    return {"quarters": quarter_rows}


def get_recent_symbols(days_back=14):
    """Return [(symbol, last_earn_date)] with earnings in last days_back."""
    _ensure_calendar_table()
    today  = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    con = _conn()
    rows = con.execute("""
        SELECT symbol, last_earn_date, last_eps_actual, last_eps_estimate,
               last_surprise_pct, earn_reaction_pct, surprise_streak, quarter_trend, earn_score
        FROM earnings_calendar
        WHERE last_earn_date BETWEEN ? AND ?
        ORDER BY last_earn_date DESC
    """, (cutoff, today)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_symbol_calendar(symbol: str):
    """Return the cached earnings calendar row for a symbol, if present."""
    _ensure_calendar_table()
    sym = str(symbol or '').strip().upper()
    if not sym:
        return None
    con = _conn()
    try:
        row = con.execute("SELECT * FROM earnings_calendar WHERE symbol=?", (sym,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _compute_earn_score(rec: dict) -> int:
    """Lightweight calendar-based earnings score, 0..100."""
    score = 50.0
    next_ed = str(rec.get("next_earn_date") or "")[:10] or None
    last_ed = str(rec.get("last_earn_date") or "")[:10] or None

    # Upcoming earnings risk/availability.
    if next_ed:
        try:
            days = (date.fromisoformat(next_ed) - date.today()).days
            if days <= 7:
                score -= 35
            elif days <= 14:
                score -= 20
            elif days <= 30:
                score -= 5
            else:
                score += 10
        except Exception:
            score -= 5
    elif last_ed:
        # Missing next date; keep score neutral-ish but not overly punitive.
        score += 0
    else:
        score -= 10

    streak = int(rec.get("surprise_streak") or 0)
    if streak >= 4:
        score += 10
    elif streak >= 2:
        score += 5

    # Quarter trend: how many of the last (up to) 4 quarters beat, not
    # just the current consecutive streak -- a stock that beat this
    # quarter after 3 misses (streak=1) is a very different situation
    # than one that beat all 4, and streak alone can't distinguish them.
    try:
        qt = json.loads(rec.get("quarter_trend") or "null")
    except Exception:
        qt = None
    if qt:
        beats = sum(1 for b in qt if b)
        total_q = len(qt)
        if total_q >= 3:
            if beats == total_q:
                score += 8   # beat every quarter available
            elif beats <= total_q - 3:
                score -= 8   # missed most/all of them

    last_surprise = rec.get("last_surprise_pct")
    if last_surprise is not None:
        try:
            ls = float(last_surprise)
            if ls >= 20:
                score += 10
            elif ls >= 10:
                score += 6
            elif ls >= 0:
                score += 3
            elif ls <= -20:
                score -= 10
            elif ls <= -10:
                score -= 6
            else:
                score -= 2
        except Exception:
            pass

    reaction = rec.get("earn_reaction_pct")
    if reaction is not None:
        try:
            rp = float(reaction)
            if rp >= 10:
                score += 8
            elif rp >= 5:
                score += 4
            elif rp <= -10:
                score -= 8
            elif rp <= -5:
                score -= 4
        except Exception:
            pass

    # Revenue/Profit QoQ+YoY trend -- actual-vs-actual (see
    # quarterly_metrics / Market Monitor), a genuine growth-quality
    # signal distinct from the EPS-beat-vs-estimate factors above.
    try:
        qm_list = json.loads(rec.get("quarterly_metrics") or "null")
    except Exception:
        qm_list = None
    if qm_list and len(qm_list) > 1:
        cur = qm_list[0]
        growth_hits = 0
        growth_checks = 0
        for key in ("revenue", "net_income"):
            cv = cur.get(key)
            if cv is None:
                continue
            if qm_list[1].get(key) is not None:
                growth_checks += 1
                if cv > qm_list[1][key]:
                    growth_hits += 1
            if len(qm_list) > 4 and qm_list[4].get(key) is not None:
                growth_checks += 1
                if cv > qm_list[4][key]:
                    growth_hits += 1
        if growth_checks >= 3:
            if growth_hits == growth_checks:
                score += 6   # grew on every QoQ/YoY comparison available
            elif growth_hits == 0:
                score -= 6   # declined on every comparison available

    return int(max(0, min(100, round(score))))


def get_earnings_info(symbol: str):
    """Return a single canonical earnings snapshot with derived earn_days / earn_score."""
    row = get_symbol_calendar(symbol) or {}
    next_ed = str(row.get("next_earn_date") or "")[:10] or None
    last_ed = str(row.get("last_earn_date") or "")[:10] or None

    earn_days = 999
    earn_date = next_ed or last_ed
    if next_ed:
        try:
            earn_days = int((date.fromisoformat(next_ed) - date.today()).days)
        except Exception:
            earn_days = 999

    if earn_days is None:
        earn_days = 999

    row["earn_days"] = earn_days
    row["earn_date"] = earn_date
    row["earn_score"] = int(row.get("earn_score") or _compute_earn_score(row))
    return row


@earn_cal_bp.route("/fetch_calendar", methods=["POST"])
def fetch_calendar_route():
    """Trigger earnings calendar + fundamentals refresh for watchlist
    symbols, in the background -- runs async with real X/Y progress
    (polled via /earnings/fetch_status) instead of blocking the
    request for 1-3 minutes with only a client-side elapsed-time timer
    to show for it. Incremental by default (force=false): only
    symbols whose next earnings date is today/past/unknown actually
    get fetched -- see refresh_calendar()'s docstring."""
    global _earn_lock
    if _earn_lock is None:
        _earn_lock = threading.Lock()
    force = request.args.get("force", "false").lower() == "true"
    wl_id = request.args.get("watchlist_id", None, type=int)
    if wl_id:
        try:
            import sqlite3 as _sq2
            from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
            _c2 = _sq2.connect(_OIAPP_DB_PATH)
            rows = _c2.execute("SELECT symbol FROM watchlist_symbols WHERE watchlist_id=?", (wl_id,)).fetchall()
            symbols = [r[0] for r in rows]
            _c2.close()
        except Exception:
            symbols = _get_watchlist()
    else:
        symbols = _get_watchlist()

    if _earn_status["running"]:
        return jsonify({"error": "An earnings fetch is already running", "status": _earn_status}), 409

    def _run():
        global _earn_status
        with _earn_lock:
            _earn_status = {"running": True, "processed": 0, "total": len(symbols),
                             "calendar_updated": 0, "calendar_skipped": 0, "calendar_failed": 0,
                             "fundamentals_fetched": 0, "fundamentals_skipped": 0, "fundamentals_errored": 0}

        def _cal_progress(done, total_hint=None):
            with _earn_lock:
                _earn_status["processed"] = done
                if total_hint:
                    _earn_status["total"] = total_hint

        updated, skipped, failed = refresh_calendar(symbols, force=force, progress_cb=_cal_progress)
        with _earn_lock:
            _earn_status["calendar_updated"] = updated
            _earn_status["calendar_skipped"] = skipped
            _earn_status["calendar_failed"] = failed

        try:
            con = _conn()
            con.execute("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT OR REPLACE INTO app_config VALUES (?,?)",
                        ("earn_calendar_updated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            con.commit(); con.close()
        except Exception:
            pass

        # Chained on purpose, not a separate button: the fundamentals
        # smart-refresh decision (fetch only if earnings happened since
        # the last pull) depends on the calendar data that just
        # refreshed above, so this is using genuinely current earnings
        # dates, not whatever was cached from the last click.
        try:
            from .earnings import bulk_fetch_fundamentals
            fund_result = bulk_fetch_fundamentals(symbols, force=force)
            with _earn_lock:
                _earn_status["fundamentals_fetched"] = fund_result.get("fetched_count", 0)
                _earn_status["fundamentals_skipped"] = fund_result.get("skipped_count", 0)
                _earn_status["fundamentals_errored"] = fund_result.get("errored_count", 0)
        except Exception as e:
            print(f"[earnings_calendar] fundamentals fetch after calendar refresh failed: {e}")

        with _earn_lock:
            _earn_status["processed"] = _earn_status["total"]
            _earn_status["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "started": True, "symbol_count": len(symbols), "force": force})


@earn_cal_bp.route("/fetch_status")
def fetch_calendar_status():
    return jsonify(_earn_status)


@earn_cal_bp.route("/market_monitor")
def market_monitor_route():
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    return jsonify(compute_market_monitor(watchlist_id))



@earn_cal_bp.route("/calendar_status")
def calendar_status():
    """Return stats about the earnings calendar."""
    _ensure_calendar_table()
    con = _conn()
    today  = date.today().isoformat()
    in_30  = (date.today() + timedelta(days=30)).isoformat()
    in_14  = (date.today() - timedelta(days=14)).isoformat()
    total  = con.execute("SELECT COUNT(*) FROM earnings_calendar").fetchone()[0]
    upcoming = con.execute(
        "SELECT COUNT(*) FROM earnings_calendar WHERE next_earn_date BETWEEN ? AND ?",
        (today, in_30)).fetchone()[0]
    recent = con.execute(
        "SELECT COUNT(*) FROM earnings_calendar WHERE last_earn_date BETWEEN ? AND ?",
        (in_14, today)).fetchone()[0]
    try:
        ts = con.execute("SELECT value FROM app_config WHERE key='earn_calendar_updated'").fetchone()
        last_updated = ts[0] if ts else None
    except: last_updated = None
    con.close()
    return jsonify({"total": total, "upcoming_30d": upcoming, "recent_14d": recent,
                    "last_updated": last_updated})
