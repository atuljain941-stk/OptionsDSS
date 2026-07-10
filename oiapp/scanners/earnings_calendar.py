"""
earnings_calendar.py — Fetch and cache earnings dates for all watchlist symbols.
Runs as a scheduled job (weekly) to pre-populate the DB so pre/post earnings
scans can filter symbols instantly without scanning all 200 symbols via yfinance.
"""
import sqlite3, json, math
from pathlib import Path
from datetime import date, datetime, timedelta
from flask import Blueprint, jsonify, request

earn_cal_bp = Blueprint("earn_cal_bp", __name__, url_prefix="/earnings")
DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")

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
        try:
            ed_all = tk.get_earnings_dates(limit=12)
            if ed_all is not None and not ed_all.empty:
                # Only past rows (Reported EPS not null)
                past = ed_all[ed_all["Reported EPS"].notna()].sort_index()
                if not past.empty:
                    r = past.iloc[-1]  # most recent reported
                    last_actual = round(float(r.get("Reported EPS", 0) or 0), 4)
                    last_est    = round(float(r.get("EPS Estimate", 0) or 0), 4)
                    surp_col    = r.get("Surprise(%)") or r.get("Surprise(%)", None)
                    if surp_col is not None:
                        surprise_pct = round(float(surp_col), 2)
                    elif last_est and last_est != 0:
                        surprise_pct = round((last_actual - last_est) / abs(last_est) * 100, 2)
                    # Beat streak: count consecutive quarters where actual >= estimate
                    streak = 0
                    for _, row in past.iloc[::-1].iterrows():
                        a = float(row.get("Reported EPS", 0) or 0)
                        e = float(row.get("EPS Estimate", 0) or 0)
                        if e != 0 and a >= e: streak += 1
                        else: break
        except: pass
        # Fallback: try earnings_history
        if last_actual is None:
            try:
                eh = tk.earnings_history
                if eh is not None and not eh.empty:
                    r = eh.iloc[0]
                    last_actual  = round(float(r.get("epsActual", 0) or 0), 4)
                    last_est     = round(float(r.get("epsEstimate", 0) or 0), 4)
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
            "earn_score": None,
            "fetch_date": date.today().isoformat(),
        }
        rec["earn_score"] = _compute_earn_score(rec)
        return rec
    except:
        return None


def refresh_calendar(symbols=None, force=False):
    """
    Fetch and store earnings calendar for symbols.
    Skips symbols fetched within 7 days (unless force=True).
    Returns (updated, skipped, failed) counts.
    """
    _ensure_calendar_table()
    if symbols is None:
        symbols = _get_watchlist()

    cutoff  = (date.today() - timedelta(days=7)).isoformat()
    con     = _conn()
    updated = skipped = failed = 0

    for sym in symbols:
        if not force:
            row = con.execute(
                "SELECT fetch_date FROM earnings_calendar WHERE symbol=?", (sym,)
            ).fetchone()
            if row and row[0] and row[0] >= cutoff:
                skipped += 1; continue
        rec = _fetch_one_symbol(sym)
        if rec:
            con.execute("""
                INSERT OR REPLACE INTO earnings_calendar
                (symbol, next_earn_date, next_earn_confirmed, last_earn_date,
                 last_eps_actual, last_eps_estimate, last_surprise_pct,
                 earn_reaction_pct, surprise_streak, earn_score, fetch_date)
                VALUES
                (:symbol, :next_earn_date, :next_earn_confirmed, :last_earn_date,
                 :last_eps_actual, :last_eps_estimate, :last_surprise_pct,
                 :earn_reaction_pct, :surprise_streak, :earn_score, :fetch_date)
            """, rec)
            con.commit()
            updated += 1
        else:
            failed += 1

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


def get_recent_symbols(days_back=14):
    """Return [(symbol, last_earn_date)] with earnings in last days_back."""
    _ensure_calendar_table()
    today  = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    con = _conn()
    rows = con.execute("""
        SELECT symbol, last_earn_date, last_eps_actual, last_eps_estimate,
               last_surprise_pct, earn_reaction_pct, surprise_streak, earn_score
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
    """Trigger earnings calendar refresh for watchlist symbols."""
    force   = request.args.get("force","false").lower() == "true"
    wl_id   = request.args.get("watchlist_id", None, type=int)
    if wl_id:
        try:
            import sqlite3 as _sq2
            from pathlib import Path as _P2
            _db2 = str(_P2(__file__).resolve().parents[2] / "options_data.db")
            _c2  = _sq2.connect(_db2)
            rows = _c2.execute("SELECT symbol FROM watchlist_symbols WHERE watchlist_id=?", (wl_id,)).fetchall()
            symbols = [r[0] for r in rows]
            _c2.close()
        except: symbols = _get_watchlist()
    else:
        symbols = _get_watchlist()
    updated, skipped, failed = refresh_calendar(symbols, force=force)
    total   = len(symbols)
    # Save timestamp
    try:
        con = _conn()
        con.execute("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT OR REPLACE INTO app_config VALUES (?,?)",
                    ("earn_calendar_updated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        con.commit(); con.close()
    except: pass
    return jsonify({"ok": True, "updated": updated, "skipped": skipped,
                    "failed": failed, "total": total})


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
