from flask import Blueprint, jsonify, request, render_template
from ..services.market import (
    sanitize_symbol,
    get_spot, get_spot_snapshot, get_expirations, get_history,
    get_live_strikes_and_volume,
    get_oi_map_fromDB, fetch_store_for,
    select_strikes_around_atm,
)
from ..db import (
    get_symbols, save_symbols, delete_symbol,
    get_expirations_for_symbol,get_oi_fromdb,get_expirationOI_date,get_two_latest_dates, _connect,
    save_saved_scanner_run, list_saved_scanner_runs, get_saved_scanner_run
)
from ..services.aggregate import get_aggregate_strike, get_pcr_snapshot
from ..services.oi_significance import build_oi_change_filter_context, oi_change_sig_flags, threshold_from_args
from ..services.scheduler import get_scheduler_status, trigger_fetch_all, stop_scheduler
from ..services.sqlview import get_table_view

api_bp = Blueprint("api", __name__, url_prefix="/api")

# In-memory status for manual Futures OI fetches.
# This prevents the UI from mistaking old rows for the result of the current fetch.
import threading as _futures_threading
import time as _futures_time
_FUTURES_FETCH_LOCK = _futures_threading.Lock()
_FUTURES_FETCH_JOBS = {}


def _futures_job_update(job_id, **updates):
    if not job_id:
        return
    with _FUTURES_FETCH_LOCK:
        job = _FUTURES_FETCH_JOBS.setdefault(job_id, {"job_id": job_id})
        job.update(updates)
        # Keep only recent jobs so the dict never grows indefinitely.
        if len(_FUTURES_FETCH_JOBS) > 20:
            oldest = sorted(_FUTURES_FETCH_JOBS.items(), key=lambda kv: kv[1].get("started_ts", 0))[:-20]
            for old_id, _old in oldest:
                _FUTURES_FETCH_JOBS.pop(old_id, None)


def _futures_job_get(job_id):
    if not job_id:
        return None
    with _FUTURES_FETCH_LOCK:
        job = _FUTURES_FETCH_JOBS.get(job_id)
        return dict(job) if job else None


def _futures_active_contracts(symbol=None):
    """Return currently relevant contract symbols for status checks --
    queried from what's ACTUALLY stored (grouped by root), not
    reconstructed from a guessed symbol format.

    Previously rebuilt via _get_quarterly_contracts(), which generates
    Schwab's 2-digit-year convention (e.g. "/ESU26"). tastytrade's real
    resolved front-month contract symbols use a different convention
    (e.g. "/ESU6" -- confirmed directly against actual stored rows).
    Once tastytrade became the primary fetch layer, this guessed set
    silently stopped matching the real data, so this status check kept
    reporting "no active futures OI data" even with rows sitting right
    there in the table. Querying live from futures_oi_daily by root
    sidesteps this whole class of symbol-format bugs permanently -- it
    doesn't matter which layer wrote a row or what convention it used,
    only that the root column (always written consistently regardless
    of source) matches.
    """
    try:
        from ..services.futures_oi_schwab import SCHWAB_ROOTS, normalize_futures_symbol
        from ..config import DB_PATH as _futures_db_path
        import sqlite3 as _sqlite3
        if symbol:
            syms = [normalize_futures_symbol(symbol)]
        else:
            syms = list(SCHWAB_ROOTS.keys())
        roots = sorted({SCHWAB_ROOTS.get(s.upper()) for s in syms if SCHWAB_ROOTS.get(s.upper())})
        if not roots:
            return set()
        con = _sqlite3.connect(_futures_db_path)
        ph = ",".join(["?"] * len(roots))
        rows = con.execute(f"SELECT DISTINCT contract FROM futures_oi_daily WHERE root IN ({ph})", tuple(roots)).fetchall()
        con.close()
        return {r[0] for r in rows if r[0]}
    except Exception:
        return set()


def _futures_near_term_contracts(symbol=None, near_count=2):
    """V104: subset of _futures_active_contracts limited to the nearest
    `near_count` contracts per root, in expiry order.

    Same root-based fix as _futures_active_contracts() above -- ranks by
    the stored expiry column on real rows instead of a guessed symbol's
    approximate expiry date, so it stays correct regardless of which
    layer's symbol-naming convention actually wrote the data.

    _get_quarterly_contracts(root, 6) looks up to ~1.5 years out for
    quarterly-cycle roots (ES included). Far months routinely come back
    with openInterest=0 from Schwab's quote endpoint (thin/illiquid), and
    once a far contract has any stored row it stays in the active set and
    can go stale indefinitely without that ever being a real problem --
    that used to drag the single any_stale flag down permanently even
    when the front-month contract actually traded was fetching fine every
    morning. Scope the pass/fail badge to the contracts that matter for
    0-10 DTE trading; still show the rest, just don't let them fail the
    top-line status.
    """
    try:
        from ..services.futures_oi_schwab import SCHWAB_ROOTS, normalize_futures_symbol
        from ..config import DB_PATH as _futures_db_path
        import sqlite3 as _sqlite3
        if symbol:
            syms = [normalize_futures_symbol(symbol)]
        else:
            syms = list(SCHWAB_ROOTS.keys())
        roots = sorted({SCHWAB_ROOTS.get(s.upper()) for s in syms if SCHWAB_ROOTS.get(s.upper())})
        if not roots:
            return set()
        con = _sqlite3.connect(_futures_db_path)
        ph = ",".join(["?"] * len(roots))
        rows = con.execute(f"""
            SELECT root, contract, MIN(expiry) as exp FROM futures_oi_daily
            WHERE root IN ({ph}) AND expiry IS NOT NULL AND expiry != ''
            GROUP BY root, contract ORDER BY root, exp ASC
        """, tuple(roots)).fetchall()
        con.close()
        near = set()
        per_root_count = {}
        for root, contract, exp in rows:
            c = per_root_count.get(root, 0)
            if c < max(1, int(near_count)):
                near.add(contract)
                per_root_count[root] = c + 1
        return near
    except Exception:
        return set()



def _wl_key(base: str, watchlist_id=None) -> str:
    return f"{base}_{int(watchlist_id)}" if watchlist_id not in (None, "", 0) else base


def _get_watchlist_symbols(watchlist_id):
    if not watchlist_id:
        return None
    try:
        from ..db import _connect
        con = _connect()
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (int(watchlist_id),)
        ).fetchall()
        con.close()
        return [r[0] for r in rows] if rows else []
    except Exception:
        return None


def _safe_num(v, ndigits=2):
    try:
        import math as _m
        f = float(v)
        if not _m.isfinite(f):
            return None
        return round(f, ndigits)
    except Exception:
        return None


def _spot_payload(symbol):
    try:
        snap = get_spot_snapshot(symbol) or {}
    except Exception:
        snap = {}
    return {
        "symbol": symbol,
        "price": _safe_num(snap.get("price"), 4),
        "source": snap.get("source"),
        "timestamp": snap.get("timestamp"),
        "prev_close": _safe_num(snap.get("prev_close"), 4),
        "change_pct": _safe_num(snap.get("change_pct"), 3),
    }


def _app_setting_get(key, default=None):
    try:
        con = _connect()
        row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        con.close()
        return row[0] if row and row[0] not in (None, '') else default
    except Exception:
        return default

def _app_setting_set(key, value):
    try:
        con = _connect()
        con.execute("INSERT INTO app_settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, '' if value is None else str(value)))
        con.commit()
        con.close()
        return True
    except Exception:
        return False

def _oib_saved_days():
    try:
        st = int(_app_setting_get('oib_st_days', 3) or 3)
    except Exception:
        st = 3
    try:
        mt = int(_app_setting_get('oib_mt_days', 10) or 10)
    except Exception:
        mt = 10
    try:
        lt = int(_app_setting_get('oib_lt_days', 30) or 30)
    except Exception:
        lt = 30
    return {
        'st_days': max(1, min(10, st)),
        'mt_days': max(2, min(30, mt)),
        'lt_days': max(5, min(90, lt)),
    }


@api_bp.route("/topbar_context")
def api_topbar_context():
    """Top-bar market context: active symbol spot, VIX, macro reminders, and live headlines."""
    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    try:
        days_ahead = int(request.args.get("days_ahead", 3) or 3)
    except Exception:
        days_ahead = 3
    try:
        days_back = int(request.args.get("days_back", 0) or 0)
    except Exception:
        days_back = 0
    try:
        news_refresh_min = int(request.args.get("news_refresh_min", 15) or 15)
    except Exception:
        news_refresh_min = 15
    news_refresh_min = max(5, min(news_refresh_min, 24 * 60))
    force_news = str(request.args.get("force_news", "0")).lower() in ("1", "true", "yes", "y")

    try:
        from ..services.macro_events import get_macro_message_board
        board = get_macro_message_board(days_back=days_back, days_ahead=days_ahead)
    except Exception as exc:
        board = {"events": [], "messages": [f"⚠️ Macro board unavailable: {exc}"], "source_note": "error"}

    try:
        from ..services.news_service import fetch_topbar_news, build_topbar_news_payload
        extra_symbols = [x.strip().upper() for x in (request.args.get("news_symbols", "DIA,IWM") or "").split(",") if x.strip()]
        syms = []
        for sym in [t] + extra_symbols:
            if sym and sym not in syms:
                syms.append(sym)
        headlines = fetch_topbar_news(
            symbols=syms,
            max_items=10,
            ttl_seconds=news_refresh_min * 60,
            force=force_news,
        )
        news = build_topbar_news_payload(headlines, refresh_minutes=news_refresh_min, max_items=8)
    except Exception as exc:
        news = {
            "ok": False,
            "refresh_minutes": news_refresh_min,
            "items": [],
            "messages": [f"📰 Latest news unavailable: {exc}"],
            "source_note": "error",
        }

    return jsonify({
        "ok": True,
        "symbol": t,
        "spot": _spot_payload(t),
        "vix": _spot_payload("^VIX"),
        "macro": board,
        "news": news,
    })

@api_bp.route("/spot")
def api_spot():
    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    payload = _spot_payload(t)
    payload["spot"] = payload.get("price")
    return jsonify(payload)

@api_bp.route("/expirations")
def api_expirations():
    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    return jsonify({"symbol": t, "expirations": get_expirations(t)})

@api_bp.route("/history")
def api_history():
    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    period = request.args.get("period", "90d")
    return jsonify({"symbol": t, "prices": get_history(t, period)})

@api_bp.route("/options")
def api_options():
    import math as _m
    def _i(v):
        try:
            f=float(v or 0)
            return 0 if (_m.isnan(f) or _m.isinf(f)) else int(f)
        except: return 0

    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    exp = request.args.get("expiration")
    if not exp:
        return jsonify({"error": "missing expiration"}), 400
    per_side = _i(request.args.get("per_side", 12))
    oi_sig_pct = threshold_from_args(request.args, 30.0)

    spot = get_spot(t)

    # Get OI from DB — auto-fetch from yfinance if missing
    oi_map, oi_day = get_oi_map_fromDB(t, exp)
    rows = get_oi_fromdb(t, exp)
    if not oi_map:
        try:
            fetch_store_for(t, expirations=[exp], per_side=per_side)
            oi_map, oi_day = get_oi_map_fromDB(t, exp)
            rows = get_oi_fromdb(t, exp)
        except Exception as e:
            print("[options] auto-fetch error:", e)

    # Build price map from latest DB snapshot (if present)
    price_map = {}
    for r in rows or []:
        try:
            price_map[(r.get("type"), float(r.get("strike")))] = r.get("price")
        except Exception:
            pass

    # Previous OI map for Dashboard/Aggregate strike significance annotations.
    # Use the same previous-distinct-snapshot comparison as /api/oi_change so
    # the gold outlines on the OI chart match the ΔOI chart exactly.
    prev_oi_map = {}
    prev_oi_compare_date = None
    skipped_identical_oi_dates = []
    oi_sig_ctx = build_oi_change_filter_context(t, rows or [], expiry=exp, source="dashboard_options", min_change_pct=oi_sig_pct)
    try:
        def _typ_norm(v):
            x = str(v or "").lower().strip()
            if x.startswith("c"):
                return "call"
            if x.startswith("p"):
                return "put"
            return x
        def _strike_norm(v):
            try:
                return round(float(v), 8)
            except Exception:
                return str(v or "").strip()
        def _snapshot_sig(snap_rows):
            pairs = []
            for rr in snap_rows or []:
                typ = _typ_norm(rr.get("type"))
                if typ not in ("call", "put"):
                    continue
                pairs.append((typ, _strike_norm(rr.get("strike")), _i(rr.get("oi"))))
            return tuple(sorted(pairs, key=lambda x: (x[0], float(x[1]) if isinstance(x[1], (int, float)) else 0)))

        con = _connect()
        try:
            date_rows = con.execute(
                """SELECT DISTINCT date FROM options
                   WHERE UPPER(symbol)=? AND expiration=?
                   ORDER BY date DESC LIMIT 12""",
                (t, exp)
            ).fetchall()
            dates = [r[0] for r in date_rows if r and r[0]]
        finally:
            con.close()

        latest_sig = _snapshot_sig(rows or [])
        for candidate_date in dates[1:]:
            candidate_rows = get_expirationOI_date(t, exp, candidate_date) or []
            if _snapshot_sig(candidate_rows) == latest_sig:
                skipped_identical_oi_dates.append(candidate_date)
                continue
            prev_oi_compare_date = candidate_date
            for pr in candidate_rows:
                typ = _typ_norm(pr.get("type"))
                if typ not in ("call", "put"):
                    continue
                try:
                    prev_oi_map[(typ, float(pr.get("strike")))] = _i(pr.get("oi"))
                except Exception:
                    pass
            break
    except Exception:
        prev_oi_map = {}
        prev_oi_compare_date = None
        skipped_identical_oi_dates = []

    # STRIKE LIST: use DB strikes as primary (ensures OI data exists for every bar)
    # Live yfinance used only for volume overlay — never for the strike list itself
    db_strikes = sorted({s for (_typ, s) in oi_map.keys()}) if oi_map else []

    # Volume from yfinance (best-effort, separate from strike list)
    vol_map = {}
    if db_strikes:
        try:
            _, vol_map = get_live_strikes_and_volume(t, exp)
        except: pass

    if not db_strikes:
        return jsonify({"symbol": t, "expiration": exp, "spot": spot,
                        "calls": [], "puts": [], "oi_snapshot_day": None,
                        "message": "No OI data in DB — run Scheduler to fetch"})

    # Select per_side strikes on each side of spot from DB strikes only
    if spot:
        picked = select_strikes_around_atm(db_strikes, spot, per_side)
    else:
        picked = db_strikes[:per_side*2]

    calls, puts = [], []
    for s in picked:
        s_f = float(s)
        c_oi = _i(oi_map.get(("call", s_f), 0)); c_prev = _i(prev_oi_map.get(("call", s_f), 0)); c_chg = c_oi - c_prev
        p_oi = _i(oi_map.get(("put", s_f), 0));  p_prev = _i(prev_oi_map.get(("put", s_f), 0));  p_chg = p_oi - p_prev
        c_sig = oi_change_sig_flags(c_oi, c_prev, c_chg, oi_sig_ctx)
        p_sig = oi_change_sig_flags(p_oi, p_prev, p_chg, oi_sig_ctx)
        calls.append({"strike": s_f,
                       "price":  price_map.get(("call", s_f)),
                       "oi":     c_oi,
                       "volume": _i(vol_map.get(("call", s_f), 0)),
                       "prev_oi": c_prev, "oi_change": c_chg, "oi_change_pct": c_sig.get("pct"),
                       "oi_change_significant": c_sig.get("significant"),
                       "oi_change_significant_build": c_sig.get("significant_build"),
                       "oi_change_significant_removal": c_sig.get("significant_removal"),
                       "oi_change_significance_reason": c_sig.get("reason")})
        puts.append({"strike":  s_f,
                      "price":  price_map.get(("put", s_f)),
                      "oi":     p_oi,
                      "volume": _i(vol_map.get(("put", s_f), 0)),
                      "prev_oi": p_prev, "oi_change": p_chg, "oi_change_pct": p_sig.get("pct"),
                      "oi_change_significant": p_sig.get("significant"),
                      "oi_change_significant_build": p_sig.get("significant_build"),
                      "oi_change_significant_removal": p_sig.get("significant_removal"),
                      "oi_change_significance_reason": p_sig.get("reason")})

    return jsonify({"symbol": t, "expiration": exp, "spot": spot,
                    "oi_snapshot_day": oi_day,
                    "oi_change_compare_date": prev_oi_compare_date,
                    "oi_change_skipped_identical_dates": skipped_identical_oi_dates,
                    "oi_change_filter": oi_sig_ctx, "calls": calls, "puts": puts})


@api_bp.route("/oi_change")
def api_oi_change():
    import datetime as _dt
    t = (request.args.get("symbol") or "SPY").upper().strip()
    exp = request.args.get("expiration")
    if not exp:
        return jsonify({"error": "missing expiration"}), 400
    oi_sig_pct = threshold_from_args(request.args, 30.0)
    # Optional explicit comparison dates -- when either is provided, the
    # auto "latest vs nearest distinct prior snapshot" selection below is
    # overridden accordingly. Backward compatible: omit both and behavior
    # is unchanged (today vs auto-detected prior day).
    override_latest_date = (request.args.get("latest_date") or "").strip() or None
    override_prior_date = (request.args.get("prior_date") or "").strip() or None

    def _typ(v):
        x = str(v or "").lower().strip()
        if x.startswith("c"):
            return "call"
        if x.startswith("p"):
            return "put"
        return x

    def _strike(v):
        try:
            return round(float(v), 8)
        except Exception:
            return str(v or "").strip()

    def _num(v, default=0):
        try:
            if v is None:
                return default
            return int(float(str(v).replace(",", "").strip() or 0))
        except Exception:
            return default

    def _rows_for(dt):
        return get_expirationOI_date(t, exp, dt) if dt else []

    def _dict(rows):
        out = {}
        for r in rows or []:
            key = (_typ(r.get("type")), _strike(r.get("strike")))
            if key[0] in ("call", "put"):
                out[key] = r
        return out

    def _calc(latest_rows, prev_rows):
        latest_dict = _dict(latest_rows)
        prev_dict = _dict(prev_rows)
        keys = sorted(set(latest_dict) | set(prev_dict), key=lambda k: (k[0], float(k[1]) if isinstance(k[1], (int, float)) else 0))
        changes = []
        for key in keys:
            typ, strike = key
            if typ not in ("call", "put"):
                continue
            new_row = latest_dict.get(key) or {}
            old_row = prev_dict.get(key) or {}
            new_oi = _num(new_row.get("oi"))
            new_vol = _num(new_row.get("volume"))
            old_oi = _num(old_row.get("oi"))
            old_vol = _num(old_row.get("volume"))
            oi_change = new_oi - old_oi
            vol_change = new_vol - old_vol
            change_pct = round(oi_change / old_oi * 100, 2) if old_oi else None
            changes.append({
                "type": typ,
                "strike": float(strike) if isinstance(strike, (int, float)) else strike,
                "oi_change": oi_change,
                "oi_change_pct": change_pct,
                "vol_change": vol_change,
                "latest_oi": new_oi,
                "prev_oi": old_oi,
                "is_new_strike": key not in prev_dict,
                "is_removed_strike": key not in latest_dict,
            })
        return changes

    try:
        from ..db import _connect
        con = _connect()
        try:
            date_rows = con.execute(
                """SELECT DISTINCT date FROM options
                   WHERE UPPER(symbol)=? AND expiration=?
                   ORDER BY date DESC LIMIT 120""",
                (t, exp)
            ).fetchall()
            dates = [r[0] for r in date_rows if r and r[0]]
        finally:
            con.close()

        if not dates:
            return jsonify({
                "symbol": t,
                "expiration": exp,
                "error": "No OI snapshots found for this symbol/expiration",
                "call_change": [],
                "put_change": [],
                "change": [],
            })

        # Exclude weekend dates from comparison entirely, not just when
        # their OI values happen to look identical to the prior weekday.
        # Markets are closed Sat/Sun, so any snapshot stored under a
        # weekend date is either a duplicate of Friday's close or a
        # meaningless artifact of a fetch that ran anyway -- either way
        # it should never be picked as "latest" or "prev". This is what
        # makes Monday correctly compare against Friday instead of
        # Saturday/Sunday, independent of whether the stored weekend
        # values happen to exactly match Friday's (the existing identical-
        # snapshot skip below still also applies, for weekday dates that
        # turn out to be duplicates for other reasons, e.g. no new OI
        # snapshot was actually fetched that day).
        weekday_dates = []
        for d_str in dates:
            try:
                wd = _dt.date.fromisoformat(d_str[:10]).weekday()
            except Exception:
                wd = 0  # unparseable date -- don't drop it, just don't weekend-filter it
            if wd < 5:  # Monday=0 .. Friday=4
                weekday_dates.append(d_str)
        if weekday_dates:
            dates = weekday_dates

        latest_date = dates[0]
        # Explicit latest-date override: anchor the "latest" comparison
        # point to a user-chosen date instead of the newest stored
        # snapshot, and restrict the auto-prior search (below) to dates
        # at or before it so "prior" still means "before latest" rather
        # than searching the whole history.
        if override_latest_date:
            latest_date = override_latest_date
            dates = [d for d in dates if d <= override_latest_date] or [override_latest_date]
        latest_rows = _rows_for(latest_date)

        def _snapshot_signature(rows):
            # Compare OI snapshots by normalized option type + strike + OI.
            # This avoids the confusing all-zero ΔOI chart when the latest two
            # stored dates contain the same clearing snapshot.
            pairs = []
            for r in rows or []:
                typ = _typ(r.get("type"))
                if typ not in ("call", "put"):
                    continue
                pairs.append((typ, _strike(r.get("strike")), _num(r.get("oi"))))
            return tuple(sorted(pairs, key=lambda x: (x[0], float(x[1]) if isinstance(x[1], (int, float)) else 0)))

        latest_sig = _snapshot_signature(latest_rows)
        prev_date = None
        prev_rows = []
        skipped_identical_dates = []
        remaining_dates = [d for d in dates if d != latest_date]
        immediate_prev_date = remaining_dates[0] if remaining_dates else None

        if override_prior_date:
            # Explicit prior-date override: use exactly what was asked
            # for, no auto-skip-if-identical -- the person chose this
            # date on purpose, so an all-zero ΔOI result if it happens
            # to match is itself the answer, not an error to route around.
            prev_date = override_prior_date
            prev_rows = _rows_for(prev_date)
        else:
            for candidate_date in remaining_dates:
                candidate_rows = _rows_for(candidate_date)
                if _snapshot_signature(candidate_rows) == latest_sig:
                    skipped_identical_dates.append(candidate_date)
                    continue
                prev_date = candidate_date
                prev_rows = candidate_rows
                break

        changes = _calc(latest_rows, prev_rows) if prev_date else _calc(latest_rows, [])
        oi_sig_ctx = build_oi_change_filter_context(t, latest_rows or [], expiry=exp, source="dashboard_oi_change", min_change_pct=oi_sig_pct)
        for c in changes:
            flags = oi_change_sig_flags(c.get("latest_oi"), c.get("prev_oi"), c.get("oi_change"), oi_sig_ctx)
            c["oi_change_pct"] = flags.get("pct") if c.get("oi_change_pct") is None else c.get("oi_change_pct")
            c["oi_change_significant"] = flags.get("significant")
            c["oi_change_significant_build"] = flags.get("significant_build")
            c["oi_change_significant_removal"] = flags.get("significant_removal")
            c["oi_change_significance_reason"] = flags.get("reason")
            c["oi_change_base_oi"] = flags.get("base_oi")
        comparison_note = ""
        prior_date_has_data = bool(prev_rows)
        if override_prior_date and not prior_date_has_data:
            comparison_note = f"No stored OI snapshot found for {override_prior_date} -- showing latest OI as if there were no prior data."
        comparison_mode = (
            "manual" if (override_latest_date or override_prior_date)
            else ("previous_distinct_snapshot" if skipped_identical_dates else "previous_snapshot")
        )
        comparison_label = (f"Latest {latest_date} vs {prev_date}" if prev_date else f"Latest {latest_date}")

        call_change = [c for c in changes if c["type"] == "call"]
        put_change = [c for c in changes if c["type"] == "put"]
        call_change.sort(key=lambda x: abs(x["oi_change"]), reverse=True)
        put_change.sort(key=lambda x: abs(x["oi_change"]), reverse=True)
        return jsonify({
            "symbol": t,
            "expiration": exp,
            "latest_date": latest_date,
            "prev_date": prev_date,
            "has_prior": bool(prev_date),
            "prior_date_has_data": prior_date_has_data,
            "latest_date_overridden": bool(override_latest_date),
            "prior_date_overridden": bool(override_prior_date),
            "comparison_note": comparison_note,
            "comparison_mode": comparison_mode,
            "skipped_identical_dates": skipped_identical_dates,
            "immediate_prev_date": immediate_prev_date,
            "comparison_label": (f"{latest_date} vs {prev_date}" if prev_date else f"Latest {latest_date}"),
            "snapshot_dates_checked": dates,
            "oi_change_filter": oi_sig_ctx,
            "call_change": call_change,
            "put_change": put_change,
            "change": changes,
        })
    except Exception as e:
        return jsonify({
            "symbol": t,
            "expiration": exp,
            "error": str(e),
            "call_change": [],
            "put_change": [],
            "change": [],
        })

@api_bp.route("/aggregate_strike")
def api_aggregate_strike():
    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    from_exp = request.args.get("from_expiration")
    count = int(request.args.get("count", 3))
    per_side = int(request.args.get("strikes", 10))
    oi_sig_pct = threshold_from_args(request.args, 30.0)
    return jsonify(get_aggregate_strike(t, from_exp, count, per_side, min_change_pct=oi_sig_pct))

@api_bp.route("/pcr_snapshot")
def api_pcr_snapshot():
    t = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    return jsonify({"symbol": t, "pcr_data": get_pcr_snapshot(t)})

@api_bp.route("/symbols", methods=["GET", "POST", "DELETE"])
def api_symbols():
    if request.method == "POST":
        syms = [sanitize_symbol(s) for s in (request.json or {}).get("symbols", []) if s]
        save_symbols(syms)
        return jsonify({"saved": syms})
    if request.method == "DELETE":
        sym = sanitize_symbol((request.json or {}).get("symbol", ""))
        if sym:
            delete_symbol(sym)
        return jsonify({"deleted": sym})
    return jsonify({"symbols": get_symbols()})

@api_bp.route("/fetch_status")
def api_fetch_status():
    import sqlite3
    from pathlib import Path as _P
    base = get_scheduler_status()
    # Enrich with last scan timestamps from app_config
    try:
        from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
        _db = _OIAPP_DB_PATH
        _c = sqlite3.connect(_db)
        _c.execute("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)")
        for _k in ["regime_scan_completed_at", "oib_completed_at",
                    "post_earnings_completed_at", "pre_earnings_completed_at"]:
            _r = _c.execute("SELECT value FROM app_config WHERE key=?", (_k,)).fetchone()
            if _r: base[_k] = _r[0]
        _c.close()
    except: pass
    return jsonify(base)

@api_bp.route("/fetch_now", methods=["POST"])
def api_fetch_now():
    wl_id = request.args.get("watchlist_id", None, type=int)
    print(f"[api] /fetch_now triggered (watchlist_id={wl_id}, None means ALL watchlists) "
          f"from {request.remote_addr} | UA: {request.headers.get('User-Agent', '?')} | "
          f"Referer: {request.headers.get('Referer', '?')}")
    return jsonify(trigger_fetch_all(watchlist_id=wl_id))

@api_bp.route("/fetch_stop", methods=["POST"])
def api_fetch_stop():
    return jsonify(stop_scheduler())

@api_bp.route("/sql_view")
def api_sql_view():
    table = request.args.get("table", "options")
    limit = int(request.args.get("limit", 100))
    symbol = request.args.get("symbol")
    exp = request.args.get("expiration")
    return jsonify(get_table_view(table, limit, symbol, exp))

@api_bp.route("/db_expirations")
def api_db_expirations():
    from datetime import date as _date
    sym = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    future_only = request.args.get("future", "false").lower() == "true"
    exps = get_expirations_for_symbol(sym)
    if future_only:
        today = _date.today().strftime("%Y-%m-%d")
        exps = [e for e in exps if e >= today]
    return jsonify({"symbol": sym, "expirations": exps})

@api_bp.route("/scanner")
def api_scanner():
    limit = int(request.args.get("limit", 10))
    results = [{"symbol": s, "delta_oi": 0, "delta_vol": 0, "signal": "Neutral"} for s in get_symbols()][:limit]
    return jsonify({"results": results})


# ── Bootstrap endpoint — returns everything the UI needs on startup in ONE call ──
@api_bp.route("/bootstrap")
def api_bootstrap():
    """
    Returns symbols list + DB expirations for the default symbol (SPY)
    in a single fast DB-only call. No yfinance network request.
    Called once on page load so all tabs populate instantly.
    """
    sym = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    symbols    = get_symbols()                   # pure DB read
    from datetime import date as _d
    today = _d.today().strftime("%Y-%m-%d")
    db_exps = [e for e in get_expirations_for_symbol(sym) if e >= today]
    return jsonify({
        "symbols":     symbols,
        "expirations": db_exps,
        "symbol":      sym,
        "today":       today,
    })


@api_bp.route("/oi_intelligence")
def api_oi_intelligence():
    """
    OI Direction Intelligence — one directional verdict per symbol+expiry.
    Applies 5 disambiguation signals to ALL significant OI changes, synthesises:
      BULLISH / MILDLY BULLISH / SIDEWAYS / MILDLY BEARISH / BEARISH
    Also returns top OI walls (support/resistance) closest to spot.
    """
    import math as _m
    from datetime import date as _d, datetime as _dt

    def _sf(v, dec=2):
        try:
            f=float(v); return None if(_m.isnan(f) or _m.isinf(f)) else round(f,dec)
        except: return None

    symbol = (request.args.get("symbol") or "SPY").upper()
    expiry = (request.args.get("expiration") or "").strip()
    if not expiry:
        return jsonify({"error":"expiration required"}),400

    latest_date, prev_date = get_two_latest_dates(symbol, expiry)
    if not latest_date or not prev_date:
        return jsonify({"symbol":symbol,"expiry":expiry,
            "error":"Need ≥2 days of OI data. Run Scheduler to populate.",
            "direction":"UNKNOWN","walls":[]})

    latest_rows = get_expirationOI_date(symbol, expiry, latest_date)
    prev_rows   = get_expirationOI_date(symbol, expiry, prev_date)
    latest_dict = {(r["type"],float(r["strike"])):r for r in latest_rows}
    prev_dict   = {(r["type"],float(r["strike"])):r for r in prev_rows}
    spot = get_spot(symbol) or 0

    # ── Price change on snapshot date ─────────────────────────────────────────
    price_chg_pct = vol_ratio = None
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="30d",interval="1d")
        if not df.empty:
            df.index = df.index.strftime("%Y-%m-%d")
            dates = list(df.index)
            if latest_date in dates:
                idx = dates.index(latest_date)
                if idx>0:
                    c1=float(df.iloc[idx]["Close"]); c0=float(df.iloc[idx-1]["Close"])
                    price_chg_pct = round((c1-c0)/c0*100,2) if c0 else None
                avg_vol=float(df["Volume"].mean()); day_vol=float(df.loc[latest_date]["Volume"])
                vol_ratio=round(day_vol/avg_vol,2) if avg_vol else None
    except Exception as e:
        print(f"[oi_intel] {symbol}: {e}")

    price_up   = price_chg_pct is not None and price_chg_pct >  0.3
    price_down = price_chg_pct is not None and price_chg_pct < -0.3

    # ── OI walls ──────────────────────────────────────────────────────────────
    all_oi = {}
    for (t,s),row in latest_dict.items():
        prev = prev_dict.get((t,s))
        oi_now  = int(row.get("oi") or 0)
        oi_prev = int(prev.get("oi") or 0) if prev else oi_now
        if oi_now < 500: continue
        all_oi[(t,s)] = {"oi":oi_now,"delta":oi_now-oi_prev}

    def wall_score(t,s,d):
        dist = abs(s-spot)/spot*100 if spot else 0
        return d["oi"] * max(0,1-dist/15) * (1.2 if d["delta"]>500 else 0.8 if d["delta"]<-500 else 1.0)

    top_puts  = sorted([(k,v) for k,v in all_oi.items() if k[0]=="put"  and k[1]<spot],
                       key=lambda x:-wall_score(*x[0],x[1]))[:4]
    top_calls = sorted([(k,v) for k,v in all_oi.items() if k[0]=="call" and k[1]>spot],
                       key=lambda x:-wall_score(*x[0],x[1]))[:4]

    walls=[]
    for (t,s),d in top_puts:
        dist=round((s-spot)/spot*100,1) if spot else 0
        walls.append({"strike":s,"type":"put","side":"support","oi":d["oi"],"delta":d["delta"],
            "pct_from_spot":dist,"strength":"STRONG" if d["oi"]>3000 else "MODERATE" if d["oi"]>1500 else "MILD",
            "growing":d["delta"]>200})
    for (t,s),d in top_calls:
        dist=round((s-spot)/spot*100,1) if spot else 0
        walls.append({"strike":s,"type":"call","side":"resistance","oi":d["oi"],"delta":d["delta"],
            "pct_from_spot":dist,"strength":"STRONG" if d["oi"]>3000 else "MODERATE" if d["oi"]>1500 else "MILD",
            "growing":d["delta"]>200})
    walls.sort(key=lambda x:abs(x["pct_from_spot"]))

    # ── 5-signal directional scoring ─────────────────────────────────────────
    bull_pts=0; bear_pts=0; evidence=[]
    MIN_DELTA=300

    changes=[]
    for (t,s),row in latest_dict.items():
        prev=prev_dict.get((t,s))
        if not prev: continue
        delta=int(row.get("oi") or 0)-int(prev.get("oi") or 0)
        if abs(delta)<MIN_DELTA: continue
        changes.append({"type":t,"strike":s,"delta":delta,
            "oi_new":int(row.get("oi") or 0),"vol_new":int(row.get("volume") or 0),
            "itm":(t=="put" and s>spot) or (t=="call" and s<spot),
            "otm":(t=="put" and s<spot) or (t=="call" and s>spot),
            "closing":delta<0})

    for c in changes:
        t,s,delta=c["type"],c["strike"],c["delta"]
        closing=c["closing"]; itm=c["itm"]; otm=c["otm"]
        w=min(3,max(1,abs(delta)//500))

        # Signal 1: Price action
        if price_chg_pct is not None:
            if   t=="put"  and closing and price_up:   bull_pts+=w*2; evidence.append(f"${s}P closed −{abs(delta):,} · price +{price_chg_pct}% → put buyer exiting (bullish)")
            elif t=="put"  and closing and price_down: bear_pts+=w*2; evidence.append(f"${s}P closed −{abs(delta):,} · price {price_chg_pct}% → put seller nervous (bearish)")
            elif t=="put"  and not closing and price_up:  bull_pts+=w; evidence.append(f"${s}P added +{delta:,} · price up → put selling for income (bullish)")
            elif t=="put"  and not closing and price_down: bear_pts+=w*2; evidence.append(f"${s}P added +{delta:,} · price down → fresh put hedging (bearish)")
            elif t=="call" and closing and price_down: bear_pts+=w*2; evidence.append(f"${s}C closed −{abs(delta):,} · price {price_chg_pct}% → call buyer exiting (bearish)")
            elif t=="call" and not closing and price_up:  bull_pts+=w*2; evidence.append(f"${s}C added +{delta:,} · price up → call buying (bullish)")
            elif t=="call" and not closing and price_down: bear_pts+=w; evidence.append(f"${s}C added +{delta:,} · price down → defensive call (mixed)")

        # Signal 3: Moneyness (Signal 2 vol just adjusts weight, already in w)
        if   closing and t=="put" and itm:  bear_pts+=w; evidence.append(f"${s}P ITM closed → put seller cutting loss (bearish)")
        elif closing and t=="put" and otm:  bull_pts+=w; evidence.append(f"${s}P OTM closed → put buyer exiting bearish bet")
        elif not closing and t=="put" and itm: bear_pts+=w*2; evidence.append(f"${s}P ITM added → aggressive downside hedge (bearish)")
        elif not closing and t=="call" and otm: bull_pts+=w; evidence.append(f"${s}C OTM added → bullish call positioning")

        # Signal 4: IV proxy (high vol/OI = event risk, note only)
        vol_oi=round(c["vol_new"]/max(abs(delta),1),1)
        if c["vol_new"]>abs(delta)*3:
            evidence.append(f"${s}{t[0].upper()} vol/ΔOI={vol_oi}× — IV expanding, uncertainty elevated")

        # Signal 5: Roll detection (cancel and re-score)
        for c2 in changes:
            if c2["type"]!=t or c2["strike"]==s: continue
            if abs(c2["strike"]-s)>max(5,abs(s)*0.03): continue
            if (c2["delta"]>0)==(delta>0): continue
            ratio=min(abs(delta),abs(c2["delta"]))/max(abs(delta),abs(c2["delta"]))
            if ratio>0.4:
                bull_pts=max(0,bull_pts-w); bear_pts=max(0,bear_pts-w)
                if t=="put":
                    if c2["strike"]<s: bull_pts+=1; evidence.append(f"${s}P → ${c2['strike']}P roll DOWN — reducing hedge (mildly bullish)")
                    else: bear_pts+=1; evidence.append(f"${s}P → ${c2['strike']}P roll UP — increasing hedge (mildly bearish)")
                break

    # PCR tie-breaker
    total_put_oi  = sum(d["oi"] for (t,s),d in all_oi.items() if t=="put")
    total_call_oi = sum(d["oi"] for (t,s),d in all_oi.items() if t=="call")
    pcr = round(total_put_oi/total_call_oi,3) if total_call_oi else None
    if pcr:
        if   pcr>1.5: bear_pts+=1; evidence.append(f"PCR={pcr} — heavy put loading (bearish lean)")
        elif pcr>1.1: bear_pts+=1
        elif pcr<0.6: bull_pts+=1; evidence.append(f"PCR={pcr} — low hedging, complacent (bullish lean)")
        elif pcr<0.8: bull_pts+=1

    # ── Verdict ───────────────────────────────────────────────────────────────
    net=bull_pts-bear_pts; total_ev=bull_pts+bear_pts
    if total_ev==0:
        direction="SIDEWAYS"; confidence="LOW"
    elif net>=6:   direction="BULLISH";        confidence="HIGH"
    elif net>=3:   direction="MILDLY BULLISH"; confidence="MODERATE"
    elif net>=-2:  direction="SIDEWAYS";       confidence="MODERATE" if total_ev>3 else "LOW"
    elif net>=-5:  direction="MILDLY BEARISH"; confidence="MODERATE"
    else:          direction="BEARISH";        confidence="HIGH"

    dir_color={"BULLISH":"#22c55e","MILDLY BULLISH":"#4ade80",
               "SIDEWAYS":"#f59e0b","MILDLY BEARISH":"#f87171","BEARISH":"#ef4444"}.get(direction,"#64748b")

    supports=[w for w in walls if w["side"]=="support"]
    resistances=[w for w in walls if w["side"]=="resistance"]
    nearest_sup=supports[0]["strike"] if supports else None
    nearest_res=resistances[0]["strike"] if resistances else None

    try: days=(_dt.strptime(expiry,"%Y-%m-%d").date()-_d.today()).days
    except: days=0

    summary_parts=[
        f"OI flow into {expiry} ({days}d) leans {'bullish' if 'BULL' in direction else 'bearish' if 'BEAR' in direction else 'neutral'} "
        f"({confidence.lower()} confidence · bull:{bull_pts} bear:{bear_pts})."]
    if nearest_sup and nearest_res:
        summary_parts.append(f"Range: ${nearest_sup} support → ${nearest_res} resistance.")
    if price_chg_pct is not None:
        summary_parts.append(f"Stock moved {'+' if price_chg_pct>=0 else ''}{price_chg_pct}% on OI snapshot date.")
    if pcr: summary_parts.append(f"PCR: {pcr}.")

    return jsonify({
        "symbol":symbol,"expiry":expiry,"spot":spot,
        "latest_date":latest_date,"prev_date":prev_date,
        "price_chg_pct":price_chg_pct,"vol_ratio":vol_ratio,"pcr":pcr,
        "bull_pts":bull_pts,"bear_pts":bear_pts,"net_score":net,
        "direction":direction,"confidence":confidence,"direction_color":dir_color,
        "summary":" ".join(summary_parts),
        "evidence":evidence[:8],
        "walls":walls,
        "days_to_expiry":days,
        "total_put_oi":total_put_oi,"total_call_oi":total_call_oi,
    })


# ═══════════════════════════════════════════════════════════════════
# FUTURES OI (Schwab real daily OI only)
# ═══════════════════════════════════════════════════════════════════
@api_bp.route("/futures/fetch")
def fetch_futures():
    """Legacy GET endpoint kept for older UI calls.

    IMPORTANT: this no longer calls the yfinance/volume-proxy futures fetcher.
    Futures OI in the dashboard/aggregate tab should come from Schwab real OI
    snapshots only. Use /api/futures/fetch_now for the background job UI.
    """
    sym = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    try:
        from ..services.futures_oi_schwab import fetch_futures_oi_schwab
        return jsonify(fetch_futures_oi_schwab(sym))
    except Exception as e:
        return jsonify({"ok": False, "symbol": sym, "source": "schwab", "error": str(e)})


@api_bp.route("/futures/universe")
def futures_universe():
    """Return grouped futures roots supported by the Schwab/CME/COT OI module."""
    try:
        from ..services.futures_oi_schwab import get_futures_universe
        return jsonify({"ok": True, "groups": get_futures_universe(), "source": "schwab"})
    except Exception as e:
        return jsonify({"ok": False, "groups": {}, "error": str(e)})

@api_bp.route("/futures/contracts")
def futures_contracts():
    """Get active quarterly Schwab futures contracts for a dashboard symbol."""
    sym = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    try:
        from ..services.futures_oi_schwab import get_futures_root, _get_quarterly_contracts, normalize_futures_symbol, SCHWAB_ROOT_META
        sym = normalize_futures_symbol(sym)
        root = get_futures_root(sym)
        contracts = _get_quarterly_contracts(root, count=6)
        return jsonify({"symbol": sym, "root": root, "meta": SCHWAB_ROOT_META.get(sym, {}), "contracts": contracts, "source": "schwab"})
    except Exception as e:
        return jsonify({"symbol": sym, "contracts": [], "source": "schwab", "error": str(e)})

@api_bp.route("/futures/data")
def get_futures_data_route():
    """Get stored Schwab real OI series for one futures contract."""
    sym      = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    contract = (request.args.get("contract", "") or "").strip().upper()
    days     = int(request.args.get("days", "30") or 30)
    try:
        from ..services.futures_oi_schwab import get_latest_oi
        d = get_latest_oi(sym, days=days)
        contracts = d.get("contracts") or []
        if not contract and contracts:
            contract = contracts[0]
        if contract and not contract.startswith("/"):
            contract = "/" + contract
        rows = (d.get("oi_series") or {}).get(contract, []) if contract else []
        data = [{
            "date": r.get("date"),
            "open": r.get("close", 0),
            "high": r.get("close", 0),
            "low": r.get("close", 0),
            "close": r.get("close", 0),
            "volume": r.get("volume", 0),
            "oi": r.get("oi", 0),
            "oi_change": r.get("oi_change", 0),
        } for r in rows]
        return jsonify({"contract": contract, "data": data, "source": "schwab"})
    except Exception as e:
        return jsonify({"contract": contract or None, "data": [], "source": "schwab", "error": str(e)})

@api_bp.route("/futures/all_contracts")
def futures_all_contracts():
    """Return the exact same Schwab futures OI data used by the Dashboard.

    The Aggregate tab used to call this endpoint while the Dashboard used
    /futures/chart_data. If one path normalized symbols or filtered contracts
    differently, the Dashboard could show Schwab OI while Aggregate said no
    data. Keep this endpoint as a compatibility wrapper around the unified
    Schwab-only chart builder so both pages render from one source of truth.
    """
    raw = request.args.get("symbol", "SPY") or "SPY"
    sym = sanitize_symbol(raw) or "SPY"
    # Be forgiving if a UI control passes labels such as "SPY /ES", "/GC", or "EURUSD".
    try:
        from ..services.futures_oi_schwab import normalize_futures_symbol
        sym = normalize_futures_symbol(raw)
    except Exception:
        pass

    try:
        d = _unified_futures_chart(sym)
        series = d.get("oi_series") or {}
        contracts = []
        for ct in d.get("contracts") or []:
            rows = series.get(ct, []) or []
            contracts.append({
                "contract": ct,
                "source": "schwab",
                "data": [{
                    "date": r.get("date"),
                    "close": r.get("close", 0),
                    "volume": r.get("volume", 0),
                    "oi": r.get("oi", 0),
                    "oi_change": r.get("oi_change", 0),
                } for r in rows],
            })
        has_oi = any(any((row.get("oi") or 0) > 0 for row in c["data"]) for c in contracts)
        return jsonify({
            "symbol": sym,
            "source": d.get("source", "schwab"),
            "contracts": contracts,
            "has_oi": has_oi,
            "signal": d.get("signal"),
            "interpretation": d.get("interpretation"),
            "message": d.get("interpretation") if not has_oi else "",
        })
    except Exception as e:
        return jsonify({"symbol": sym, "contracts": [], "source": "schwab", "has_oi": False, "error": str(e)})

@api_bp.route("/futures/analysis")
def futures_analysis():
    """Futures OI snapshot for dashboard widgets.

    Keep this endpoint on the same unified reader as /futures/chart_data so the
    dashboard cannot show futures OI in one section while another section says
    no data.
    """
    raw = request.args.get("symbol", "SPY") or "SPY"
    try:
        d = _unified_futures_chart(raw)
        contracts = []
        total_oi = 0
        for r in d.get("contract_table") or []:
            oi = int(r.get("oi") or 0)
            total_oi += oi
            contracts.append({
                "contract": r.get("contract"),
                "expiry": r.get("expiry"),
                "oi": oi,
                "volume": r.get("volume", 0),
                "price": r.get("close", 0),
                "oi_change": r.get("oi_change", 0),
                "date": r.get("date", ""),
                "source": r.get("source", "schwab"),
            })
        front = next((r for r in contracts if r.get("contract") == d.get("front")), contracts[0] if contracts else {})
        return jsonify({
            "symbol": d.get("symbol", raw),
            "root": d.get("root", ""),
            "name": d.get("label") or d.get("display") or raw,
            "asset_class": d.get("asset_class", ""),
            "front": front.get("contract", d.get("front", "")),
            "active": d.get("active_contract", ""),
            "signal": d.get("signal", "NO_DATA"),
            "contracts": contracts,
            "total_oi": total_oi or d.get("total_oi", 0),
            "has_real_oi": (total_oi or d.get("total_oi", 0)) > 0,
            "is_volume_proxy": False,
            "price": {"today": front.get("price", 0), "change": d.get("price_chg", 0)},
            "oi": front.get("oi", 0),
            "oi_change": front.get("oi_change", d.get("front_oi_chg", 0)),
            "description": d.get("interpretation") or "No Schwab real futures OI rows available.",
            "source": d.get("source", "schwab"),
        })
    except Exception as e:
        return jsonify({
            "symbol": raw,
            "front": "",
            "signal": "NO_DATA",
            "contracts": [],
            "total_oi": 0,
            "description": "No Schwab real futures OI rows available. Use Scheduler -> Fetch OI Now after Schwab is connected.",
            "source": "none",
            "is_volume_proxy": False,
            "error": str(e),
        })


@api_bp.route("/futures/symbol_map")
def futures_symbol_map():
    from ..services.futures_oi import CME_PRODUCTS, get_quarterly_contracts
    sym = request.args.get("symbol","")
    if sym:
        contracts = get_quarterly_contracts(sym, 3)
        return jsonify({"symbol": sym.upper(), "contracts": contracts})
    return jsonify({k: v.get("root","") for k, v in CME_PRODUCTS.items()})
@api_bp.route("/futures_oi_multi")
def futures_oi_multi():
    """OI time series for multiple expiries (upcoming + next N)."""
    symbol = sanitize_symbol(request.args.get("symbol","SPY")) or "SPY"
    count = int(request.args.get("count","3"))
    from ..db import _connect
    con = _connect()
    
    # Get upcoming expiries
    import datetime as _dt
    today = _dt.date.today().isoformat()
    exps = [r[0] for r in con.execute(
        "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration LIMIT ?",
        (symbol, today, count)).fetchall()]
    
    result = []
    for exp in exps:
        rows = con.execute("""
            SELECT date, type, SUM(oi) as total_oi
            FROM options WHERE symbol=? AND expiration=?
            GROUP BY date, type ORDER BY date""",
            (symbol, exp)).fetchall()
        by_date = {}
        for r in rows:
            d = r[0]
            if d not in by_date:
                by_date[d] = {"date": d, "call_oi": 0, "put_oi": 0}
            if r[1] == "call": by_date[d]["call_oi"] = r[2]
            else: by_date[d]["put_oi"] = r[2]
        
        dates = sorted(by_date.keys())
        series = []
        prev_total = 0
        for d in dates:
            rec = by_date[d]
            total = rec["call_oi"] + rec["put_oi"]
            oi_chg = total - prev_total if prev_total > 0 else 0
            rec["total_oi"] = total
            rec["oi_change"] = oi_chg
            rec["pc_ratio"] = round(rec["put_oi"] / max(1, rec["call_oi"]), 3)
            series.append(rec)
            prev_total = total
        
        result.append({"expiry": exp, "series": series,
                        "latest_total": series[-1]["total_oi"] if series else 0,
                        "latest_pc": series[-1]["pc_ratio"] if series else 0})
    
    con.close()
    return jsonify({"symbol": symbol, "expiries": result})

@api_bp.route("/oi_heatmap_scanner")
def oi_heatmap_scanner():
    """OI buildup/covering signals across multiple symbols for heatmap."""
    from ..db import _connect
    import datetime as _dt
    con = _connect()
    today = _dt.date.today().isoformat()
    
    # Get all symbols with recent data
    symbols = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM options WHERE date >= date('now','-3 days')").fetchall()]
    
    results = []
    for sym in symbols:
        # Get the nearest future expiry
        exp_row = con.execute(
            "SELECT MIN(expiration) FROM options WHERE symbol=? AND expiration>=?",
            (sym, today)).fetchone()
        if not exp_row or not exp_row[0]: continue
        exp = exp_row[0]
        
        # Get last 2 dates of aggregate OI
        date_rows = con.execute(
            "SELECT DISTINCT date FROM options WHERE symbol=? AND expiration=? ORDER BY date DESC LIMIT 2",
            (sym, exp)).fetchall()
        if len(date_rows) < 2: continue
        
        d_today, d_prev = date_rows[0][0], date_rows[1][0]
        
        def get_agg(dt):
            rows = con.execute(
                "SELECT type, SUM(oi) FROM options WHERE symbol=? AND expiration=? AND date=? GROUP BY type",
                (sym, exp, dt)).fetchall()
            return {r[0]: r[1] or 0 for r in rows}
        
        today_oi = get_agg(d_today)
        prev_oi = get_agg(d_prev)
        
        t_call = today_oi.get("call", 0); t_put = today_oi.get("put", 0)
        p_call = prev_oi.get("call", 0); p_put = prev_oi.get("put", 0)
        total_today = t_call + t_put; total_prev = p_call + p_put
        oi_chg = total_today - total_prev
        oi_chg_pct = round(oi_chg / max(1, total_prev) * 100, 2)
        
        # Get price change
        try:
            import yfinance as yf
            hist = yf.Ticker(sym).history(period="5d")
            if len(hist) >= 2:
                price_chg = float(hist["Close"].iloc[-1]) - float(hist["Close"].iloc[-2])
                spot = float(hist["Close"].iloc[-1])
            else: price_chg = 0; spot = 0
        except: price_chg = 0; spot = 0
        
        # Signal
        if oi_chg > 0 and price_chg > 0: signal = "LONG_BUILDUP"
        elif oi_chg > 0 and price_chg < 0: signal = "SHORT_BUILDUP"
        elif oi_chg < 0 and price_chg < 0: signal = "LONG_UNWINDING"
        elif oi_chg < 0 and price_chg > 0: signal = "SHORT_COVERING"
        else: signal = "NEUTRAL"
        
        results.append({
            "symbol": sym, "expiry": exp, "spot": round(spot, 2),
            "call_oi": t_call, "put_oi": t_put, "total_oi": total_today,
            "oi_change": oi_chg, "oi_change_pct": oi_chg_pct,
            "pc_ratio": round(t_put / max(1, t_call), 3),
            "price_change": round(price_chg, 2),
            "signal": signal,
        })
    
    con.close()
    results.sort(key=lambda x: abs(x["oi_change"]), reverse=True)
    return jsonify({"results": results, "count": len(results)})


@api_bp.route("/oi_buildup_settings", methods=["GET", "POST"])
def oi_buildup_settings():
    if request.method == 'GET':
        return jsonify({"ok": True, "settings": _oib_saved_days()})
    data = request.get_json(silent=True) or {}
    try:
        st = int(data.get('st_days', 3) or 3)
    except Exception:
        st = 3
    try:
        mt = int(data.get('mt_days', 10) or 10)
    except Exception:
        mt = 10
    try:
        lt = int(data.get('lt_days', 30) or 30)
    except Exception:
        lt = 30
    st = max(1, min(10, st))
    mt = max(2, min(30, mt))
    lt = max(5, min(90, lt))
    if mt < st:
        mt = st
    if lt < mt:
        lt = mt
    ok = all([
        _app_setting_set('oib_st_days', st),
        _app_setting_set('oib_mt_days', mt),
        _app_setting_set('oib_lt_days', lt),
    ])
    return jsonify({"ok": ok, "settings": {"st_days": st, "mt_days": mt, "lt_days": lt}})

@api_bp.route("/oi_buildup_screener")
def oi_buildup_screener():
    """Seller-side OI buildup screener — ST/MT/LT OI + PCR changes. No yfinance; all from DB."""
    # Keep dashboard and AI Hub on the same scanner primitive.  The legacy
    # implementation below remains as fallback if the reusable scanner cannot
    # be imported for any reason.
    try:
        from ..scanners.oi_buildup_scanner import run_oi_buildup_screener as _run_oib
        saved_days = _oib_saved_days()
        st_days = request.args.get("st", saved_days["st_days"], type=int) or saved_days["st_days"]
        mt_days = request.args.get("mt", saved_days["mt_days"], type=int) or saved_days["mt_days"]
        lt_days = request.args.get("lt", saved_days["lt_days"], type=int) or saved_days["lt_days"]
        watchlist_id = request.args.get("watchlist_id", None, type=int)
        max_symbols = request.args.get("max_symbols", None, type=int)
        result = _run_oib(st_days=st_days, mt_days=mt_days, lt_days=lt_days, watchlist_id=watchlist_id, max_symbols=max_symbols, save_cache=True)
        return jsonify(result), 200 if result.get("ok", True) else 500
    except Exception:
        pass
    import traceback as _tb, datetime as _dt, json as _json
    try:
        from ..db import _connect
        st_days = max(1,  min(10, request.args.get("st", 3,  type=int)))
        mt_days = max(2,  min(60, request.args.get("mt", 10,  type=int)))
        lt_days = max(5,  min(90, request.args.get("lt", 30, type=int)))
        watchlist_id = request.args.get("watchlist_id", None, type=int)
        watchlist_syms = _get_watchlist_symbols(watchlist_id)
        max_rows = max(lt_days + 2, 32)
        today    = _dt.date.today().isoformat()
        _errors  = []

        con = _connect()

        # ── Query 1: nearest expiry per symbol ──────────────────────────────
        sym_exp = dict(con.execute(
            "SELECT symbol, MIN(expiration) FROM options WHERE expiration>=? GROUP BY symbol",
            (today,)
        ).fetchall())
        if watchlist_syms is not None:
            wl_set = set(watchlist_syms)
            sym_exp = {s:e for s,e in sym_exp.items() if s in wl_set}
            # If the selected watchlist has symbols but no option rows yet, warm the DB
            # so the screener behaves like the other watchlist-driven tabs.
            if not sym_exp and wl_set:
                try:
                    from ..services.market import fetch_store_for
                    from concurrent.futures import ThreadPoolExecutor
                    ex = ThreadPoolExecutor(max_workers=6)
                    try:
                        list(ex.map(lambda s: fetch_store_for(s), list(wl_set)[:40], timeout=60))
                    finally:
                        ex.shutdown(wait=False)
                    sym_exp = dict(con.execute(
                        "SELECT symbol, MIN(expiration) FROM options WHERE expiration>=? GROUP BY symbol",
                        (today,)
                    ).fetchall())
                    sym_exp = {s:e for s,e in sym_exp.items() if s in wl_set}
                except Exception as _warm_e:
                    _errors.append(f"watchlist warmup: {_warm_e}")

        # ── Query 2: bulk OI history — ALL non-expired options per symbol ──
        # Using all expirations (not just nearest) gives full historical depth
        all_syms_list = list(sym_exp.keys())
        if not all_syms_list:
            return jsonify({"results":[], "count":0, "date":today,
                            "completed_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "st_days":st_days,"mt_days":mt_days,"lt_days":lt_days,
                            "errors":[],"error_count":0})

        cutoff = _dt.date.fromordinal(
            _dt.date.today().toordinal() - max_rows - 2
        ).isoformat()
        sym_ph = ",".join(["?"] * len(all_syms_list))
        oi_rows = con.execute(f"""
            SELECT symbol, date,
                   SUM(CASE WHEN type='call' THEN oi     ELSE 0 END) call_oi,
                   SUM(CASE WHEN type='put'  THEN oi     ELSE 0 END) put_oi,
                   SUM(CASE WHEN type='call' THEN volume ELSE 0 END) call_vol,
                   SUM(CASE WHEN type='put'  THEN volume ELSE 0 END) put_vol
            FROM options
            WHERE date >= ?
              AND symbol IN ({sym_ph})
              AND expiration >= date
            GROUP BY symbol, date
            ORDER BY symbol, date DESC
        """, [cutoff] + all_syms_list).fetchall()

        # Build {sym: [(date, c_oi, p_oi, c_vol, p_vol), ...] newest-first}
        from collections import defaultdict
        oi_by_sym = defaultdict(list)
        for sym, dt, c_oi, p_oi, c_vol, p_vol in oi_rows:
            oi_by_sym[sym].append((dt, c_oi or 0, p_oi or 0, c_vol or 0, p_vol or 0))

        # ── Query 3: OI-weighted spot per symbol per date ───────────────────
        spot_rows = con.execute("""
            SELECT symbol, date, SUM(strike*oi)/NULLIF(SUM(oi),0) spot
            FROM (
                SELECT symbol, date, strike, SUM(oi) oi,
                       ROW_NUMBER() OVER (PARTITION BY symbol,date ORDER BY SUM(oi) DESC) rn
                FROM options
                WHERE date >= date('now','-35 days')
                  AND expiration BETWEEN date('now') AND date('now','+45 days')
                GROUP BY symbol, date, strike
            )
            WHERE rn <= 5
            GROUP BY symbol, date ORDER BY symbol, date
        """).fetchall()
        spot_by_sym = defaultdict(list)
        for sym, dt, sp in spot_rows:
            if sp: spot_by_sym[sym].append((dt, round(sp, 2)))

        con.close()

        FUTURES_ROOTS = {"SPY","QQQ","IWM","DIA","GLD","SLV","TLT","XLE","XLF","XLK"}

        def _oi_delta(rows, n):
            """rows is newest-first list of (date,c_oi,p_oi,c_vol,p_vol)"""
            if len(rows) < 2: return 0, 0, rows[0] if rows else None
            d0  = rows[0]
            dn  = rows[min(n, len(rows)-1)]
            oi0 = d0[1]+d0[2]; oin = dn[1]+dn[2]
            vol0= d0[3]+d0[4]; voln= dn[3]+dn[4]
            return round((oi0-oin)/max(1,oin)*100,2), round((vol0-voln)/max(1,voln)*100,2), dn

        def _price_delta(pts, n):
            """pts is oldest-first list of (date, price)"""
            if not pts: return 0
            cur  = pts[-1][1]
            base = pts[max(0, len(pts)-1-n)][1]
            return round((cur-base)/max(0.01,base)*100, 2)

        def _signal(oi_pct, price_pct):
            if oi_pct > 0 and price_pct > 0: return "Long Buildup"
            if oi_pct > 0 and price_pct < 0: return "Short Buildup"
            if oi_pct < 0 and price_pct < 0: return "Long Unwinding"
            if oi_pct < 0 and price_pct > 0: return "Short Covering"
            return "Neutral"

        results = []
        for sym, exp in sym_exp.items():
            try:
                rows = oi_by_sym.get(sym, [])
                if len(rows) < 2: continue

                d0 = rows[0]
                call_oi_now = d0[1]; put_oi_now = d0[2]
                total_oi    = call_oi_now + put_oi_now
                if total_oi == 0: continue

                pcr = round(put_oi_now / max(1, call_oi_now), 3)

                pts = spot_by_sym.get(sym, [])
                spot = pts[-1][1] if pts else 0.0

                oi_st_pct, vol_st_pct, _ = _oi_delta(rows, st_days)
                oi_mt_pct, vol_mt_pct, _ = _oi_delta(rows, mt_days)
                oi_lt_pct, vol_lt_pct, _ = _oi_delta(rows, lt_days)

                pr_st = _price_delta(pts, st_days)
                pr_mt = _price_delta(pts, mt_days)
                pr_lt = _price_delta(pts, lt_days)

                st_out = _signal(oi_st_pct, pr_st)
                mt_out = _signal(oi_mt_pct, pr_mt)
                lt_out = _signal(oi_lt_pct, pr_lt)

                # Bias score
                bs = 0
                if   pr_mt >  2.0: bs += 2
                elif pr_mt >  0.5: bs += 1
                elif pr_mt < -2.0: bs -= 2
                elif pr_mt < -0.5: bs -= 1
                if   pr_lt >  4.0: bs += 2
                elif pr_lt >  1.5: bs += 1
                elif pr_lt < -4.0: bs -= 2
                elif pr_lt < -1.5: bs -= 1
                OI_SC = {"Long Buildup":2,"Short Covering":1,"Short Buildup":-2,"Long Unwinding":-1,"Neutral":0}
                bs += OI_SC.get(st_out,0)+OI_SC.get(mt_out,0)+OI_SC.get(lt_out,0)
                if pcr>1.5: bs-=1
                elif pcr<0.7: bs+=1

                if   bs>=4:  bias="Strongly Bullish"
                elif bs>=2:  bias="Bullish"
                elif bs>=1:  bias="Mildly Bullish"
                elif bs<=-4: bias="Strongly Bearish"
                elif bs<=-2: bias="Bearish"
                elif bs<=-1: bias="Mildly Bearish"
                else:        bias="Sideways"

                results.append({
                    "symbol":sym, "spot":spot, "expiry":exp,
                    "has_futures": sym.upper() in FUTURES_ROOTS,
                    "call_oi":call_oi_now,"put_oi":put_oi_now,
                    "total_oi":total_oi,"pcr":pcr,"pc_ratio":pcr,
                    "bias":bias,"bias_score":bs,
                    "price_1d_pct":pr_st,"oi_1d_chg":0,"oi_1d_pct":oi_st_pct,"vol_1d_pct":vol_st_pct,"st_outlook":st_out,
                    "price_5d_pct":pr_mt,"oi_5d_chg":0,"oi_5d_pct":oi_mt_pct,"vol_5d_pct":vol_mt_pct,"mt_outlook":mt_out,
                    "price_15d_pct":pr_lt,"oi_15d_chg":0,"oi_15d_pct":oi_lt_pct,"vol_15d_pct":vol_lt_pct,"lt_outlook":lt_out,
                    "st_days":st_days,"mt_days":mt_days,"lt_days":lt_days,
                })
            except Exception as _se:
                _errors.append({"sym":sym,"error":str(_se)})

        results.sort(key=lambda x: abs(x.get("oi_1d_pct",0)), reverse=True)

        # Save to cache
        completed_at = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            import sqlite3 as _sq
            from pathlib import Path as _P
            from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
            _db = _OIAPP_DB_PATH
            _c  = _sq.connect(_db)
            _c.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
            cache_key = _wl_key('oi_buildup_scan', watchlist_id)
            ts_key = _wl_key('oib_completed_at', watchlist_id)
            _c.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                       (cache_key, _json.dumps(results), completed_at))
            _c.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                       (ts_key, 'oib_ts', completed_at))
            _c.commit(); _c.close()
        except: pass

        return jsonify({"results":results,"count":len(results),"date":today,
                        "completed_at":completed_at,
                        "st_days":st_days,"mt_days":mt_days,"lt_days":lt_days,
                        "errors":_errors[:10],"error_count":len(_errors)})
    except Exception as _e:
        return jsonify({"error":str(_e),"trace":_tb.format_exc()[-800:],"results":[],"count":0}), 500


def _pcr_pick_expiry(con, sym: str, expiry: str | None = None) -> str:
    """Pick requested expiry or nearest future expiry with option rows."""
    import datetime as _dt
    expiry = (expiry or "").strip()
    if expiry:
        return expiry
    today = _dt.date.today().isoformat()
    row = con.execute(
        "SELECT MIN(expiration) FROM options WHERE symbol=? AND expiration>=?",
        (sym, today),
    ).fetchone()
    return row[0] if row and row[0] else ""


def _pcr_latest_day(con, sym: str, expiry: str) -> str:
    row = con.execute(
        "SELECT MAX(date) FROM options WHERE symbol=? AND expiration=?",
        (sym, expiry),
    ).fetchone()
    return row[0] if row and row[0] else ""


def _pcr_nearest_strike(con, sym: str, expiry: str, target_strike=None) -> float | None:
    """Nearest available strike for this expiry/latest snapshot. Defaults to nearest spot/median."""
    day = _pcr_latest_day(con, sym, expiry)
    if not day:
        return None
    rows = con.execute(
        "SELECT DISTINCT strike FROM options WHERE symbol=? AND expiration=? AND date=? ORDER BY strike",
        (sym, expiry, day),
    ).fetchall()
    strikes = [float(r[0]) for r in rows if r[0] is not None]
    if not strikes:
        return None
    try:
        target = float(target_strike) if target_strike not in (None, "") else float(get_spot(sym) or 0)
    except Exception:
        target = 0
    if not target or target <= 0:
        target = strikes[len(strikes)//2]
    return min(strikes, key=lambda x: abs(x - target))


def _pcr_sentiment(pcr: float | None) -> str:
    if pcr is None:
        return "N/A"
    if pcr > 1.5:
        return "EXTREME_FEAR"
    if pcr > 1.1:
        return "CAUTIOUS"
    if pcr < 0.6:
        return "COMPLACENT"
    if pcr < 0.8:
        return "BULLISH"
    return "NEUTRAL"


@api_bp.route("/pcr_strike_timeseries")
def pcr_strike_timeseries():
    """PCR over time for one symbol + expiry + strike.

    This is different from /pcr_timeseries, which aggregates all strikes for an expiry.
    Here PCR is computed as selected-strike put OI / selected-strike call OI by snapshot date.
    """
    sym = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    expiry_arg = request.args.get("expiry", "")
    strike_arg = request.args.get("strike", "")

    from ..db import _connect
    con = _connect()
    try:
        expiry = _pcr_pick_expiry(con, sym, expiry_arg)
        if not expiry:
            return jsonify({"symbol": sym, "expiry": "", "selected_strike": None, "series": [], "summary": {}})
        strike = _pcr_nearest_strike(con, sym, expiry, strike_arg)
        if strike is None:
            return jsonify({"symbol": sym, "expiry": expiry, "selected_strike": None, "series": [], "summary": {}})

        rows = con.execute("""
            SELECT date,
                   SUM(CASE WHEN type='call' THEN oi ELSE 0 END) AS call_oi,
                   SUM(CASE WHEN type='put' THEN oi ELSE 0 END) AS put_oi,
                   SUM(CASE WHEN type='call' THEN volume ELSE 0 END) AS call_vol,
                   SUM(CASE WHEN type='put' THEN volume ELSE 0 END) AS put_vol
            FROM options
            WHERE symbol=? AND expiration=? AND ABS(strike - ?) < 0.0001
            GROUP BY date ORDER BY date
        """, (sym, expiry, float(strike))).fetchall()
    finally:
        con.close()

    series = []
    prev_pcr = None
    for r in rows:
        calls = int(r[1] or 0)
        puts = int(r[2] or 0)
        pcr = round(puts / calls, 3) if calls > 0 else None
        if pcr is not None and prev_pcr is not None:
            pcr_change = round(pcr - prev_pcr, 3)
            pcr_change_pct = round((pcr - prev_pcr) / max(0.001, prev_pcr) * 100, 2)
        else:
            pcr_change = 0
            pcr_change_pct = 0
        series.append({
            "date": r[0],
            "strike": float(strike),
            "call_oi": calls,
            "put_oi": puts,
            "call_vol": int(r[3] or 0),
            "put_vol": int(r[4] or 0),
            "pcr": pcr,
            "pcr_change": pcr_change,
            "pcr_change_pct": pcr_change_pct,
            "sentiment": _pcr_sentiment(pcr),
        })
        if pcr is not None:
            prev_pcr = pcr

    valid = [s["pcr"] for s in series if s.get("pcr") is not None]
    if valid:
        current = valid[-1]
        high = max(valid)
        low = min(valid)
        pcr_rank = round((current - low) / (high - low) * 100, 1) if high != low else 50
        trend = "RISING" if len(valid) >= 2 and valid[-1] > valid[-2] else "FALLING" if len(valid) >= 2 and valid[-1] < valid[-2] else "FLAT"
    else:
        current = high = low = pcr_rank = None
        trend = "N/A"

    return jsonify({
        "symbol": sym,
        "expiry": expiry,
        "selected_strike": float(strike),
        "series": series,
        "summary": {
            "current": current,
            "high": high,
            "low": low,
            "pcr_rank": pcr_rank,
            "trend": trend,
        },
    })


@api_bp.route("/pcr_strike_expiries")
def pcr_strike_expiries():
    """Current selected-strike PCR across the selected expiry plus future expiries.

    Uses only each expiry's latest OI snapshot, not history.
    """
    import datetime as _dt
    sym = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    from_exp = (request.args.get("from_expiration", "") or "").strip()
    try:
        count = max(1, min(12, int(request.args.get("count", 3))))
    except Exception:
        count = 3
    strike_arg = request.args.get("strike", "")

    today = _dt.date.today().isoformat()
    all_exps = [e for e in get_expirations_for_symbol(sym) if e >= today]
    if from_exp and from_exp in all_exps:
        exps = all_exps[all_exps.index(from_exp):all_exps.index(from_exp) + count]
    else:
        exps = all_exps[:count]

    from ..db import _connect
    con = _connect()
    out = []
    try:
        for exp in exps:
            day = _pcr_latest_day(con, sym, exp)
            if not day:
                continue
            strike = _pcr_nearest_strike(con, sym, exp, strike_arg)
            if strike is None:
                continue
            row = con.execute("""
                SELECT
                    SUM(CASE WHEN type='call' THEN oi ELSE 0 END) AS call_oi,
                    SUM(CASE WHEN type='put' THEN oi ELSE 0 END) AS put_oi,
                    SUM(CASE WHEN type='call' THEN volume ELSE 0 END) AS call_vol,
                    SUM(CASE WHEN type='put' THEN volume ELSE 0 END) AS put_vol
                FROM options
                WHERE symbol=? AND expiration=? AND date=? AND ABS(strike - ?) < 0.0001
            """, (sym, exp, day, float(strike))).fetchone()
            calls = int(row[0] or 0) if row else 0
            puts = int(row[1] or 0) if row else 0
            pcr = round(puts / calls, 3) if calls > 0 else None
            out.append({
                "expiration": exp,
                "date": day,
                "strike": float(strike),
                "call_oi": calls,
                "put_oi": puts,
                "call_vol": int(row[2] or 0) if row else 0,
                "put_vol": int(row[3] or 0) if row else 0,
                "pcr": pcr,
                "sentiment": _pcr_sentiment(pcr),
            })
    finally:
        con.close()

    try:
        target_strike = float(strike_arg) if str(strike_arg).strip() else None
    except Exception:
        target_strike = None
    return jsonify({
        "symbol": sym,
        "target_strike": target_strike,
        "from_expiration": from_exp,
        "count": count,
        "expiries": out,
    })

@api_bp.route("/pcr_timeseries")
def pcr_timeseries():
    """PCR change over time for a specific expiry — time series."""
    sym = sanitize_symbol(request.args.get("symbol","SPY")) or "SPY"
    expiry = request.args.get("expiry","")
    
    from ..db import _connect
    import datetime as _dt
    
    con = _connect()
    
    # If no expiry specified, use nearest future expiry
    if not expiry:
        today = _dt.date.today().isoformat()
        row = con.execute(
            "SELECT MIN(expiration) FROM options WHERE symbol=? AND expiration>=?",
            (sym, today)).fetchone()
        expiry = row[0] if row and row[0] else ""
    
    if not expiry:
        con.close()
        return jsonify({"error": "No expiry found", "series": [], "expiry": ""})
    
    # Get PCR for each date we have data for this expiry
    rows = con.execute("""
        SELECT date,
               SUM(CASE WHEN type='call' THEN oi ELSE 0 END) as call_oi,
               SUM(CASE WHEN type='put' THEN oi ELSE 0 END) as put_oi,
               SUM(CASE WHEN type='call' THEN volume ELSE 0 END) as call_vol,
               SUM(CASE WHEN type='put' THEN volume ELSE 0 END) as put_vol
        FROM options WHERE symbol=? AND expiration=?
        GROUP BY date ORDER BY date
    """, (sym, expiry)).fetchall()
    con.close()
    
    series = []
    prev_pcr = None
    for r in rows:
        calls = r[1] or 0; puts = r[2] or 0
        total = calls + puts
        pcr = round(puts / max(1, calls), 3)
        pcr_chg = round(pcr - prev_pcr, 3) if prev_pcr is not None else 0
        pcr_chg_pct = round((pcr - prev_pcr) / max(0.001, prev_pcr) * 100, 2) if prev_pcr else 0
        
        # Sentiment from PCR
        if pcr > 1.5: sentiment = "EXTREME_FEAR"
        elif pcr > 1.1: sentiment = "CAUTIOUS"
        elif pcr < 0.6: sentiment = "COMPLACENT"
        elif pcr < 0.8: sentiment = "BULLISH"
        else: sentiment = "NEUTRAL"
        
        series.append({
            "date": r[0], "call_oi": calls, "put_oi": puts,
            "total_oi": total, "pcr": pcr,
            "pcr_change": pcr_chg, "pcr_change_pct": pcr_chg_pct,
            "sentiment": sentiment,
            "call_vol": r[3] or 0, "put_vol": r[4] or 0,
        })
        prev_pcr = pcr
    
    # Summary stats
    if series:
        pcrs = [s["pcr"] for s in series]
        current = series[-1]["pcr"]
        high = max(pcrs); low = min(pcrs)
        pcr_rank = round((current - low) / (high - low) * 100, 1) if high != low else 50
    else:
        current = high = low = pcr_rank = 0
    
    return jsonify({
        "symbol": sym, "expiry": expiry,
        "series": series,
        "summary": {
            "current": current, "high": high, "low": low,
            "pcr_rank": pcr_rank,
            "trend": "RISING" if len(series)>=2 and series[-1]["pcr"] > series[-2]["pcr"] else "FALLING" if len(series)>=2 else "N/A",
        }
    })

@api_bp.route("/iv_rank/<symbol>")
def api_iv_rank(symbol):
    """
    IV Rank, IV Percentile, current HV, and credit/debit recommendation.
    Uses 252 days of price history.
    """
    import math, numpy as np
    sym = sanitize_symbol(symbol) or "SPY"
    
    try:
        import yfinance as yf
        hist = yf.Ticker(sym).history(period="1y")
        closes = list(hist["Close"])
    except:
        return jsonify({"error": "Cannot fetch price history", "symbol": sym}), 500
    
    if len(closes) < 60:
        return jsonify({"error": "Insufficient price data", "symbol": sym}), 400
    
    # Compute daily log returns
    log_ret = np.log(np.array(closes[1:]) / np.array(closes[:-1]))
    
    # Rolling 20-day HV (annualized %)
    window = 20
    hvs = []
    for i in range(window, len(log_ret)):
        hv = np.std(log_ret[i-window:i]) * math.sqrt(252) * 100
        hvs.append(round(hv, 2))
    
    if len(hvs) < 30:
        return jsonify({"error": "Not enough HV data", "symbol": sym}), 400
    
    current_hv = hvs[-1]
    hv_30 = round(np.mean(hvs[-30:]), 2)   # 30-day avg
    hv_high = max(hvs)
    hv_low  = min(hvs)
    
    # IV Rank: where current HV sits in the 1-year range
    iv_rank = round((current_hv - hv_low) / max(0.01, hv_high - hv_low) * 100, 1)
    
    # IV Percentile: % of days where HV was BELOW current
    iv_pct = round(sum(1 for h in hvs if h < current_hv) / len(hvs) * 100, 1)
    
    # HV term structure
    hv_5  = round(np.std(log_ret[-5:])  * math.sqrt(252) * 100, 2) if len(log_ret) >= 5  else current_hv
    hv_10 = round(np.std(log_ret[-10:]) * math.sqrt(252) * 100, 2) if len(log_ret) >= 10 else current_hv
    hv_21 = round(np.std(log_ret[-21:]) * math.sqrt(252) * 100, 2) if len(log_ret) >= 21 else current_hv
    hv_63 = round(np.std(log_ret[-63:]) * math.sqrt(252) * 100, 2) if len(log_ret) >= 63 else current_hv
    
    # Regime classification
    if iv_rank >= 70:
        regime = "HIGH"
        recommendation = "SELL PREMIUM"
        advice = "IV is elevated — options are expensive. Favor credit strategies: Iron Condors, Vertical Spreads, Cash-Secured Puts."
        strategies = ["Iron Condor", "Bull Put Spread", "Bear Call Spread", "Short Strangle", "Cash-Secured Put"]
    elif iv_rank >= 40:
        regime = "MODERATE"
        recommendation = "MIXED"
        advice = "IV is moderate. Credit spreads still viable. Consider defined-risk structures over naked premium."
        strategies = ["Bull Put Spread", "Bear Call Spread", "Iron Condor", "Calendar Spread"]
    else:
        regime = "LOW"
        recommendation = "BUY OPTIONS"
        advice = "IV is compressed — options are cheap. Favor debit strategies or structures that benefit from IV expansion."
        strategies = ["Call Debit Spread", "Put Debit Spread", "Long Straddle", "Backspread", "LEAP Calls/Puts"]
    
    # IV trend (rising or falling)
    if len(hvs) >= 5:
        recent_avg = np.mean(hvs[-5:])
        prior_avg  = np.mean(hvs[-10:-5]) if len(hvs) >= 10 else recent_avg
        iv_trend = "RISING" if recent_avg > prior_avg * 1.05 else "FALLING" if recent_avg < prior_avg * 0.95 else "STABLE"
    else:
        iv_trend = "STABLE"
    
    return jsonify({
        "symbol": sym,
        "current_hv": current_hv,
        "hv_5": hv_5, "hv_10": hv_10, "hv_21": hv_21, "hv_63": hv_63,
        "hv_high": hv_high, "hv_low": hv_low, "hv_30d_avg": hv_30,
        "iv_rank": iv_rank,
        "iv_percentile": iv_pct,
        "regime": regime,
        "recommendation": recommendation,
        "advice": advice,
        "preferred_strategies": strategies,
        "iv_trend": iv_trend,
        "data_days": len(hvs),
    })

@api_bp.route("/oi_buildup_cached")
def oi_buildup_cached():
    """Return last OI buildup scan from DB cache."""
    import json as _json
    from ..db import _connect
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key=?", (_wl_key('oi_buildup_scan', watchlist_id),)).fetchone()
        if row:
            results = _json.loads(row[0])
            try:
                from ..scanners.oi_buildup_scanner import refresh_seller_flow_display_context
                if isinstance(results, list):
                    results = refresh_seller_flow_display_context(results)
            except Exception:
                try:
                    from ..scanners.oi_buildup_scanner import ensure_seller_flow_strategy
                    if isinstance(results, list):
                        results = [ensure_seller_flow_strategy(x) for x in results]
                except Exception:
                    pass
            return jsonify({"results": results, "date": row[1], "from_cache": True, "watchlist_id": watchlist_id, "display_context_refreshed": True})
        return jsonify({"results": [], "date": None, "from_cache": True, "watchlist_id": watchlist_id})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})
    finally:
        con.close()

@api_bp.route("/saved_runs", methods=["GET"])
def saved_runs_list():
    scanner_key = (request.args.get("scanner_key") or request.args.get("scanner") or "").strip().lower()
    limit = request.args.get("limit", 50, type=int) or 50
    run_name = (request.args.get("run_name") or request.args.get("name") or "").strip() or None
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    watchlist_id = request.args.get("watchlist_id")
    symbol = (request.args.get("symbol") or "").strip() or None
    runs = list_saved_scanner_runs(scanner_key or None, limit=limit, run_name=run_name, date_from=date_from, date_to=date_to, watchlist_id=watchlist_id, symbol=symbol)
    return jsonify({"ok": True, "scanner_key": scanner_key or None, "runs": runs, "count": len(runs), "filters": {"run_name": run_name, "date_from": date_from, "date_to": date_to, "watchlist_id": watchlist_id, "symbol": symbol}})


@api_bp.route("/saved_runs", methods=["POST"])
def saved_runs_save():
    data = request.get_json(silent=True) or {}
    scanner_key = str(data.get("scanner_key") or data.get("scanner") or "").strip().lower()
    run_name = str(data.get("run_name") or data.get("name") or "").strip()
    if not scanner_key or not run_name:
        return jsonify({"ok": False, "error": "scanner_key and run_name are required"}), 400
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else data.get("payload")
    if payload is None:
        payload = data.get("data") if isinstance(data.get("data"), dict) else data.get("data")
    try:
        saved = save_saved_scanner_run(
            scanner_key,
            run_name,
            payload or {},
            watchlist_id=data.get("watchlist_id"),
            symbol=data.get("symbol"),
            summary=data.get("summary") if isinstance(data.get("summary"), dict) else {},
            note=data.get("note"),
        )
    except Exception as exc:
        msg = str(exc)
        low = msg.lower()
        if "locked" in low or "busy" in low:
            return jsonify({"ok": False, "error": "Database is busy. The save was retried but another long write is still active. Please try again after the current scan/fetch finishes.", "detail": msg}), 423
        return jsonify({"ok": False, "error": msg}), 500
    if not saved:
        return jsonify({"ok": False, "error": "Saved run was not written. Check OIAPP_READ_ONLY_MODE or database write permissions."}), 423
    return jsonify({"ok": True, "run": saved})


@api_bp.route("/saved_runs/<int:run_id>", methods=["GET"])
def saved_runs_get(run_id: int):
    run = get_saved_scanner_run(run_id=run_id)
    if not run:
        return jsonify({"ok": False, "error": "Saved run not found"}), 404
    return jsonify({"ok": True, "run": run})


@api_bp.route("/saved_runs/load", methods=["GET"])
def saved_runs_load_by_name():
    scanner_key = (request.args.get("scanner_key") or request.args.get("scanner") or "").strip().lower()
    run_name = (request.args.get("run_name") or request.args.get("name") or "").strip()
    if not scanner_key or not run_name:
        return jsonify({"ok": False, "error": "scanner_key and run_name are required"}), 400
    run = get_saved_scanner_run(scanner_key=scanner_key, run_name=run_name)
    if not run:
        return jsonify({"ok": False, "error": "Saved run not found"}), 404
    return jsonify({"ok": True, "run": run})


@api_bp.route("/update_sectors", methods=["POST"])
def update_sectors():
    """
    Fetch and store sector for watchlist symbols using yfinance.
    Runs in a background thread — returns immediately.
    Results committed every 10 symbols so progress is never lost.
    """
    import yfinance as yf, threading, datetime, time
    from ..db import _connect

    force  = request.args.get("force","false").lower() == "true"
    wl_id  = request.args.get("watchlist_id", None, type=int)

    # Gather symbols
    con = _connect()
    con.execute("""CREATE TABLE IF NOT EXISTS sector_cache (
        symbol TEXT PRIMARY KEY, sector TEXT, industry TEXT, updated TEXT)""")
    con.commit()

    if wl_id:
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (wl_id,)
        ).fetchall()
        all_syms = [r[0] for r in rows] if rows else []
    if not wl_id or not all_syms:
        all_syms = [r[0] for r in con.execute(
            "SELECT symbol FROM symbols WHERE symbol IS NOT NULL ORDER BY symbol"
        ).fetchall()]
    con.close()

    if not all_syms:
        return jsonify({"ok": False, "error": "No symbols found", "total": 0})

    today  = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()

    # Store run state in app_cache for status polling
    import json, sqlite3 as _sq
    from pathlib import Path as _P
    from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
    _db = _OIAPP_DB_PATH
    def _save_status(updated, skipped, failed, done=False):
        try:
            c = _sq.connect(_db)
            c.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
            c.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                      ("sector_update_status", json.dumps({
                          "updated": updated, "skipped": skipped, "failed": failed,
                          "total": len(all_syms), "done": done, "ts": today
                      }), today))
            c.commit(); c.close()
        except: pass

    def _run():
        updated = skipped = failed = 0
        batch_con = _sq.connect(_db)
        try:
            for i, sym in enumerate(all_syms):
                # Skip if fresh (unless force)
                if not force:
                    row = batch_con.execute(
                        "SELECT sector, updated FROM sector_cache WHERE symbol=?", (sym,)
                    ).fetchone()
                    # Skip if we already KNOW the sector, regardless of
                    # when it was fetched -- sector classification is
                    # essentially permanent (a company doesn't change
                    # sectors), so a stale-but-present value shouldn't
                    # trigger a refetch. Only re-fetch when the sector
                    # is genuinely unknown, or (as a fallback for the
                    # rare reclassification) when it's both stale AND
                    # older than the freshness cutoff.
                    if row and row[0]:
                        skipped += 1
                        continue
                    if row and row[1] and row[1] >= cutoff:
                        skipped += 1
                        continue
                try:
                    info     = yf.Ticker(sym).info or {}
                    sector   = info.get("sector","")   or info.get("sectorDisp","")
                    industry = info.get("industry","") or info.get("industryDisp","")
                    if sector:
                        batch_con.execute(
                            "INSERT OR REPLACE INTO sector_cache VALUES (?,?,?,?)",
                            (sym.upper(), sector, industry, today)
                        )
                        updated += 1
                    else:
                        failed += 1
                    time.sleep(0.15)
                except:
                    failed += 1
                # Commit every 10 symbols so progress is never lost
                if (i + 1) % 10 == 0:
                    batch_con.commit()
                    _save_status(updated, skipped, failed)
            batch_con.commit()
        finally:
            batch_con.close()
        _save_status(updated, skipped, failed, done=True)

    t = threading.Thread(target=_run, daemon=True); t.start()
    return jsonify({"ok": True, "started": True, "total": len(all_syms),
                    "mode": "background", "updated": 0, "skipped": 0, "failed": 0})


@api_bp.route("/update_sectors_status")
def update_sectors_status():
    """Poll the background sector update progress."""
    from ..db import _connect
    import json
    con = _connect()
    try:
        row = con.execute("SELECT value FROM app_cache WHERE key='sector_update_status'").fetchone()
        con.close()
        if row:
            return jsonify(json.loads(row[0]))
        return jsonify({"done": False, "updated": 0, "skipped": 0, "failed": 0, "total": 0})
    except:
        con.close()
        return jsonify({"done": False, "updated": 0, "total": 0})


@api_bp.route("/pre_earnings_cache", methods=["GET"])
def get_pre_earnings_cache():
    """Load last pre-earnings scan from DB."""
    import json as _j
    from ..db import _connect
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key=?", (_wl_key('pre_earnings_scan', watchlist_id),)).fetchone()
        if row:
            return jsonify({"results": _j.loads(row[0]), "date": row[1], "from_cache": True, "watchlist_id": watchlist_id})
        return jsonify({"results": [], "date": None, "from_cache": True, "watchlist_id": watchlist_id})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})
    finally:
        con.close()

@api_bp.route("/pre_earnings_cache", methods=["POST"])
def save_pre_earnings_cache():
    """Save pre-earnings scan results to DB."""
    import json as _j
    from ..db import _connect
    d = request.get_json()
    results = d.get("results", [])
    date_str = d.get("date", __import__('datetime').date.today().isoformat())
    watchlist_id = d.get("watchlist_id")
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                    (_wl_key('pre_earnings_scan', watchlist_id), _j.dumps(results), date_str))
        con.commit()
        return jsonify({"ok": True, "saved": len(results), "watchlist_id": watchlist_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        con.close()

@api_bp.route("/post_earnings_cache", methods=["GET"])
def get_post_earnings_cache():
    import json as _j; from ..db import _connect
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key=?", (_wl_key('post_earnings_scan', watchlist_id),)).fetchone()
        if row: return jsonify({"results": _j.loads(row[0]), "date": row[1], "from_cache": True, "watchlist_id": watchlist_id})
        return jsonify({"results": [], "date": None, "from_cache": True, "watchlist_id": watchlist_id})
    except Exception as e: return jsonify({"results": [], "error": str(e)})
    finally: con.close()

@api_bp.route("/post_earnings_cache", methods=["POST"])
def save_post_earnings_cache():
    import json as _j; from ..db import _connect
    d = request.get_json()
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                    (_wl_key('post_earnings_scan', d.get('watchlist_id')), _j.dumps(d.get("results",[])), d.get("date",__import__('datetime').date.today().isoformat())))
        con.commit()
        return jsonify({"ok": True, "saved": len(d.get("results",[])), "watchlist_id": d.get('watchlist_id')})
    except Exception as e: return jsonify({"ok": False, "error": str(e)})
    finally: con.close()

@api_bp.route("/sr_proximity_cache", methods=["GET"])
def get_sr_proximity_cache():
    import json as _j; from ..db import _connect
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key='sr_proximity_scan'").fetchone()
        if row: return jsonify({"results": _j.loads(row[0]), "date": row[1], "from_cache": True})
        return jsonify({"results": [], "date": None})
    except Exception as e: return jsonify({"results": [], "error": str(e)})
    finally: con.close()

@api_bp.route("/sr_proximity_cache", methods=["POST"])
def save_sr_proximity_cache():
    import json as _j; from ..db import _connect
    d = request.get_json(); con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES ('sr_proximity_scan',?,?)",
                    (_j.dumps(d.get("results",[])), d.get("date", __import__('datetime').date.today().isoformat())))
        con.commit()
        return jsonify({"ok": True, "saved": len(d.get("results",[]))})
    except Exception as e: return jsonify({"ok": False, "error": str(e)})
    finally: con.close()

@api_bp.route("/run_scheduler", methods=["POST"])
def run_scheduler_now():
    """Manually trigger scheduler tasks in background thread."""
    import threading
    def _bg():
        try:
            from ..app_factory import _run_all_tasks
            _run_all_tasks()
        except Exception as e:
            print(f"[manual scheduler] {e}")
    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Scheduler started in background"})

# ── Futures OI (Real Data) ────────────────────────────────────────────────────
@api_bp.route("/futures/config", methods=["GET"])
def get_futures_config():
    """Get current futures data source config."""
    from ..services.futures_oi_real import _get_api_key, FUTURES_MAP
    key = _get_api_key()
    return jsonify({
        "barchart_key_set": bool(key),
        "barchart_key_hint": key[:6]+"…" if key else "",
        "supported_symbols": list(FUTURES_MAP.keys()),
        "instructions": "Get free key at https://www.barchart.com/ondemand/free-api-key"
    })

@api_bp.route("/futures/config", methods=["POST"])
def save_futures_config():
    """Save Barchart API key."""
    from ..services.futures_oi_real import _save_api_key
    d = request.get_json() or {}
    key = d.get("barchart_api_key", "").strip()
    if not key: return jsonify({"ok": False, "error": "key required"}), 400
    _save_api_key(key)
    return jsonify({"ok": True, "message": "Barchart API key saved"})

@api_bp.route("/futures/roll_adjusted")
def futures_roll_adjusted_real():
    """Roll-adjusted signal — reads from best available source in DB."""
    sym = sanitize_symbol(request.args.get("symbol","SPY")) or "SPY"
    return jsonify(_unified_futures_signal(sym))

@api_bp.route("/futures/chart_data")
def futures_chart_data():
    """OHLCV + OI/volume series for all 3 contracts — for dashboard charts."""
    sym = sanitize_symbol(request.args.get("symbol","SPY")) or "SPY"
    return jsonify(_unified_futures_chart(sym))

def _unified_futures_chart(sym):
    """Build dashboard futures signal from Schwab real OI snapshots only.

    Now supports equity index, commodity, rates, and CME FX futures.  It keeps
    front/active-contract history and cumulative OI across active contracts so
    roll weeks do not look like false liquidation.
    """
    try:
        from ..services.futures_oi_schwab import get_latest_oi, normalize_futures_symbol
        sym = normalize_futures_symbol(sym)
        d = get_latest_oi(sym, days=120)
    except Exception as e:
        return {
            "symbol": sym,
            "signal": "NO_DATA",
            "interpretation": f"Unable to read Schwab futures OI: {e}",
            "contracts": [],
            "oi_series": {},
            "cumulative_series": [],
            "contract_table": [],
            "source": "schwab",
            "is_volume_proxy": False,
            "score": 0,
        }

    series = d.get("oi_series") or {}
    contracts_ordered = d.get("contracts") or []
    contracts = [ct for ct in contracts_ordered if any((r.get("oi") or 0) > 0 for r in (series.get(ct) or []))]
    oi_series = {ct: (series.get(ct) or []) for ct in contracts}
    cumulative_series = d.get("cumulative_series") or []
    front_continuous_series = d.get("front_continuous_series") or []

    # Compatibility fallback: if the reader found latest contract-table rows but
    # could not build a history series because rows came from an older table or
    # older contract symbol format, synthesize a one-point visible series.  The
    # Aggregate page has historically rendered from this ladder-style data, so
    # this keeps Dashboard and Aggregate aligned.
    if not contracts:
        table_rows = [r for r in (d.get("contract_table") or []) if int(r.get("oi") or 0) > 0]
        for r in table_rows:
            ct = str(r.get("contract") or "").upper().strip()
            if not ct:
                continue
            if ct not in contracts:
                contracts.append(ct)
            oi_series[ct] = [{
                "date": r.get("date") or "",
                "oi": int(r.get("oi") or 0),
                "volume": int(r.get("volume") or 0),
                "close": float(r.get("close") or 0),
                "oi_change": int(r.get("oi_change") or 0),
                "source": r.get("source") or "schwab",
                "expiry": r.get("expiry") or "",
            }]
        if contracts and not cumulative_series:
            total = sum(int((oi_series.get(ct) or [{}])[-1].get("oi") or 0) for ct in contracts)
            chg = sum(int((oi_series.get(ct) or [{}])[-1].get("oi_change") or 0) for ct in contracts)
            dt = next(((oi_series.get(ct) or [{}])[-1].get("date") for ct in contracts if oi_series.get(ct)), "")
            cumulative_series = [{"date": dt, "oi": total, "volume": 0, "close": 0, "oi_change": chg}] if total else []

    if not contracts:
        return {
            "symbol": d.get("symbol", sym),
            "root": d.get("root", ""),
            "display": d.get("display", sym),
            "asset_class": d.get("asset_class", ""),
            "signal": "NO_DATA",
            "interpretation": "No Schwab real futures OI rows are stored yet. Connect Schwab, then run Scheduler → Fetch OI Now.",
            "contracts": contracts_ordered,
            "oi_series": {},
            "cumulative_series": [],
            "contract_table": d.get("contract_table", []),
            "source": "schwab",
            "is_volume_proxy": False,
            "score": 0,
        }

    front = d.get("front") or contracts[0]
    active = d.get("active_contract") or front
    back = None
    for ct in contracts:
        if ct != front:
            back = ct; break

    def _latest_change(rows, key="oi", stored_key="oi_change"):
        if not rows:
            return 0
        # Prefer the actual visible series difference whenever at least two
        # history points exist.  Older DB rows may contain stored oi_change=0
        # even though the current OI differs from the prior day, which made the
        # dashboard look like there was no OI change.
        if len(rows) >= 2:
            try:
                return int((rows[-1].get(key) or 0) - (rows[-2].get(key) or 0))
            except Exception:
                pass
        latest = rows[-1]
        stored = latest.get(stored_key)
        if stored not in (None, ""):
            try:
                return int(stored)
            except Exception:
                pass
        return 0

    active_rows = oi_series.get(active, []) or oi_series.get(front, []) or []
    front_rows = oi_series.get(front, []) or []
    back_rows = oi_series.get(back, []) if back else []
    cum_rows = cumulative_series or []
    cumulative_oi_chg = _latest_change(cum_rows, key="oi", stored_key="oi_change")
    front_chg = _latest_change(front_rows)
    back_chg = _latest_change(back_rows) if back_rows else 0
    active_chg = _latest_change(active_rows)
    price_chg = 0.0
    if len(active_rows) >= 2:
        p0 = float(active_rows[-2].get("close") or 0)
        p1 = float(active_rows[-1].get("close") or 0)
        price_chg = p1 - p0 if p0 else 0.0

    is_roll = front_chg < 0 and (back_chg > 0 or active_chg > 0) and cumulative_oi_chg >= 0
    if len(cum_rows) < 2 and cumulative_oi_chg == 0:
        signal = "FIRST_FETCH"
        interp = f"First Schwab OI snapshot — {active}: {(active_rows[-1].get('oi',0) if active_rows else 0):,}. Check again tomorrow for daily OI change."
    elif is_roll:
        signal = "ROLL"
        interp = f"Roll/position transfer: {front} {front_chg:+,}, {active} {active_chg:+,}, cumulative {cumulative_oi_chg:+,}."
    elif cumulative_oi_chg > 0 and price_chg > 0:
        signal = "LONG_BUILDUP"
        interp = f"Price up with cumulative OI +{cumulative_oi_chg:,}: potential long buildup across the curve."
    elif cumulative_oi_chg > 0 and price_chg < 0:
        signal = "SHORT_BUILDUP"
        interp = f"Price down with cumulative OI +{cumulative_oi_chg:,}: potential short buildup across the curve."
    elif cumulative_oi_chg < 0 and price_chg > 0:
        signal = "SHORT_COVERING"
        interp = f"Price up while cumulative OI {cumulative_oi_chg:,}: likely short covering / position reduction."
    elif cumulative_oi_chg < 0 and price_chg < 0:
        signal = "LONG_UNWINDING"
        interp = f"Price down while cumulative OI {cumulative_oi_chg:,}: likely long liquidation / unwind."
    elif cumulative_oi_chg > 0:
        signal = "OI_BUILDUP"
        interp = f"Cumulative OI +{cumulative_oi_chg:,}; price confirmation is flat/mixed."
    elif cumulative_oi_chg < 0:
        signal = "OI_UNWINDING"
        interp = f"Cumulative OI {cumulative_oi_chg:,}; participation is falling."
    else:
        signal = "NEUTRAL"
        interp = "No meaningful cumulative OI change."

    score_map = {"LONG_BUILDUP": 4, "SHORT_BUILDUP": -4, "SHORT_COVERING": 2, "LONG_UNWINDING": -2,
                 "OI_BUILDUP": 1, "OI_UNWINDING": -1, "ROLL": 0, "FIRST_FETCH": 0, "NEUTRAL": 0}

    selected_sentiment = {}
    try:
        from ..services.cftc_cot import get_combined_signal
        root_for_cot = d.get("root") or ""
        if root_for_cot:
            selected_sentiment = get_combined_signal(root_for_cot) or {}
    except Exception as _sent_e:
        selected_sentiment = {"error": str(_sent_e), "found": False}

    return {
        "symbol": d.get("symbol", sym),
        "root": d.get("root", ""),
        "display": d.get("display", sym),
        "label": d.get("label", sym),
        "asset_class": d.get("asset_class", ""),
        "signal": signal,
        "interpretation": interp,
        "is_roll": is_roll,
        "front": front,
        "active_contract": active,
        "back": back,
        "front_oi_chg": front_chg,
        "back_oi_chg": back_chg,
        "active_oi_chg": active_chg,
        "net_oi_chg": cumulative_oi_chg,
        "cumulative_oi_chg": cumulative_oi_chg,
        "price_chg": round(price_chg, 4),
        "total_oi": d.get("total_oi", 0),
        "contracts": contracts,
        "oi_series": oi_series,
        "front_continuous_series": front_continuous_series,
        "cumulative_series": cumulative_series,
        "contract_table": d.get("contract_table", []),
        "source": "schwab",
        "is_volume_proxy": False,
        "roll_note": f"Roll: {front}→{active}" if is_roll else None,
        "score": score_map.get(signal, 0),
        "selected_sentiment": selected_sentiment,
        "history_table": d.get("history_table", "futures_oi_daily"),
        "legacy_table_used": bool(d.get("legacy_table_used", False)),
        "history_rows": int(d.get("history_rows") or 0),
        "history_start": d.get("history_start", ""),
        "history_end": d.get("history_end", ""),
    }

def _unified_futures_signal(sym):
    return _unified_futures_chart(sym)

@api_bp.route("/futures/fetch_now", methods=["POST"])
def futures_fetch_now():
    """Manually trigger Futures OI fetch and expose job progress to the UI.

    The old implementation started a background thread, then the browser polled
    /fetch_status. If stale rows already existed, the browser stopped polling
    before the new Schwab request finished. This job_id lets the frontend wait
    for the current fetch rather than treating old data as success.
    """
    import threading
    import datetime as _dt

    sym = sanitize_symbol(request.args.get("symbol", "")) or None
    job_id = f"futoi-{int(_futures_time.time() * 1000)}"

    try:
        from ..services.futures_oi_schwab import SCHWAB_ROOTS, normalize_futures_symbol
        syms = [normalize_futures_symbol(sym)] if sym else list(SCHWAB_ROOTS.keys())
    except Exception:
        syms = [sym.upper()] if sym else ["SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "USO", "NATGAS", "TLT", "EURUSD", "JPYUSD", "GBPUSD"]

    allow_proxy_fallback = str(request.args.get("proxy_fallback", request.args.get("fallback", "0"))).lower() in ("1", "true", "yes", "y")

    _futures_job_update(
        job_id,
        running=True,
        done=False,
        ok=False,
        started_at=_dt.datetime.now().isoformat(timespec="seconds"),
        started_ts=_futures_time.time(),
        symbols=syms,
        results=[],
        errors=[],
        stored_total=0,
        oi_total=0,
        used_proxy=False,
        reauthorize_required=False,
        message="Fetching real futures OI (tastytrade -> Schwab -> CME -> COT)...",
    )

    def _bg():
        results = []
        errors = []
        stored_total = 0
        oi_total_all = 0
        used_proxy = False
        reauthorize_required = False
        try:
            from ..services.futures_oi_schwab import fetch_futures_oi_three_layer
            for s in syms:
                r = fetch_futures_oi_three_layer(s, use_cme_fallback=True, include_cot=True)
                contracts = r.get("contracts", []) or []
                oi_total = sum(c.get("oi", 0) or 0 for c in contracts)
                stored = int(r.get("stored", 0) or 0) or sum(1 for c in contracts if c.get("stored"))
                # If Schwab returned auth problems but CME fallback stored rows, keep ok=True
                row = {
                    "symbol": s,
                    "ok": bool(r.get("ok")),
                    "contracts": len(contracts),
                    "stored": stored,
                    "oi_total": oi_total,
                    "source": r.get("source", "schwab"),
                    "layers": r.get("layers", []),
                    "date": r.get("date", ""),
                    "error": r.get("error", ""),
                    "reauthorize_required": bool(r.get("reauthorize_required", False)),
                }
                results.append(row)
                stored_total += stored
                oi_total_all += oi_total
                reauthorize_required = reauthorize_required or bool(row.get("reauthorize_required"))
                if row.get("error") and stored <= 0:
                    errors.append(f"{s}: {row['error']}")
                elif row["ok"] and stored == 0:
                    errors.append(f"{s}: no usable OI rows stored from tastytrade/Schwab/CME")
                print(f"  Futures {s}: {len(contracts)} contracts OI={oi_total:,} stored={stored} source={row.get('source')}")
                _futures_job_update(
                    job_id,
                    results=list(results),
                    errors=list(errors),
                    stored_total=stored_total,
                    oi_total=oi_total_all,
                    reauthorize_required=reauthorize_required,
                    message=f"Fetched {len(results)}/{len(syms)} symbols (tastytrade -> Schwab -> CME -> COT)...",
                )
        except Exception as e:
            errors.append(f"Futures OI fetch pipeline failed: {e}")
            print(f"[schwab fetch] {e}")

        # Do not silently store yfinance volume proxy rows as Futures OI. It is not
        # exchange open interest and can make stale data look refreshed. Keep this
        # opt-in for emergency diagnostics only.
        if stored_total <= 0 and allow_proxy_fallback:
            used_proxy = True
            try:
                from ..services.futures_oi_real import fetch_real_futures_oi
                fallback_results = []
                for s in syms:
                    r = fetch_real_futures_oi(s)
                    fallback_results.append({
                        "symbol": s,
                        "ok": bool(r.get("rows_saved", 0)),
                        "rows_saved": r.get("rows_saved", 0),
                        "source": r.get("source", "fallback"),
                        "error": r.get("error", ""),
                    })
                    print(f"  {s}: {r.get('rows_saved',0)} rows [{r.get('source','?')}] proxy/fallback")
                results.extend(fallback_results)
                errors.append("Proxy fallback was used. These rows may not be real exchange OI.")
            except Exception as e:
                errors.append(f"fallback fetch failed: {e}")
                print(f"[futures fallback] {e}")
        elif stored_total <= 0:
            errors.append("No new Schwab OI rows were stored. Existing cached rows, if any, were left unchanged.")

        ok = stored_total > 0
        if ok:
            msg = f"Schwab futures OI updated: {stored_total} contract rows stored."
        elif reauthorize_required:
            msg = "Schwab auth failed. Re-authorize Schwab in Settings, then Fetch OI Now again."
        else:
            msg = "Schwab futures OI fetch finished but did not store new rows."
        _futures_job_update(
            job_id,
            running=False,
            done=True,
            ok=ok,
            finished_at=_dt.datetime.now().isoformat(timespec="seconds"),
            finished_ts=_futures_time.time(),
            results=results,
            errors=errors,
            stored_total=stored_total,
            oi_total=oi_total_all,
            used_proxy=used_proxy,
            reauthorize_required=reauthorize_required,
            message=msg,
        )

    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id, "symbols": syms,
                    "message": "Fetching futures OI in background..."})

@api_bp.route("/futures/oi_data")
def futures_oi_data():
    """Get latest Schwab real OI series for dashboard charts.

    Do not fall back to yfinance/volume-proxy rows here. If Schwab is not
    connected or no rows are stored, return an explicit no-data payload.
    """
    sym = sanitize_symbol(request.args.get("symbol","SPY")) or "SPY"
    try:
        from ..services.futures_oi_schwab import get_latest_oi as _gl
        d = _gl(sym)
        d["source"] = "schwab"
        d["is_volume_proxy"] = False
        return jsonify(d)
    except Exception as e:
        return jsonify({"symbol": sym, "contracts": [], "oi_series": {}, "source": "none", "is_volume_proxy": False, "error": str(e)})

@api_bp.route("/futures/fetch_status")
def futures_fetch_status():
    """Check latest active Futures OI rows, with staleness and optional job status."""
    import sqlite3, datetime as _dt2
    from pathlib import Path

    symbol = sanitize_symbol(request.args.get("symbol", "")) or None
    job_id = request.args.get("job_id", "")
    job = _futures_job_get(job_id)

    from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
    db = _OIAPP_DB_PATH
    try:
        # Make sure the fetched_at column exists for upgraded user DBs.
        try:
            from ..services.futures_oi_schwab import _ensure_table
            _ensure_table()
        except Exception:
            pass

        active = _futures_active_contracts(symbol)
        near_term = _futures_near_term_contracts(symbol, near_count=2)
        con = sqlite3.connect(db)

        # Only report currently relevant contracts. Old expired contracts should
        # not keep the status badge stale forever.
        if active:
            ph = ",".join(["?"] * len(active))
            rows = con.execute(f"""
                SELECT f.symbol, f.contract, f.trade_date, f.oi, f.volume, f.source, f.settle,
                       COALESCE(f.fetched_at, '')
                FROM futures_oi_daily f
                INNER JOIN (
                    SELECT contract, MAX(trade_date) as max_date
                    FROM futures_oi_daily
                    WHERE contract IN ({ph})
                    GROUP BY contract
                ) m ON f.contract = m.contract AND f.trade_date = m.max_date
                ORDER BY f.symbol, f.contract
            """, tuple(active)).fetchall()
        else:
            rows = con.execute("""
                SELECT f.symbol, f.contract, f.trade_date, f.oi, f.volume, f.source, f.settle,
                       COALESCE(f.fetched_at, '')
                FROM futures_oi_daily f
                INNER JOIN (
                    SELECT contract, MAX(trade_date) as max_date
                    FROM futures_oi_daily GROUP BY contract
                ) m ON f.contract = m.contract AND f.trade_date = m.max_date
                ORDER BY f.symbol, f.contract
            """).fetchall()
        con.close()

        today = _dt2.date.today().isoformat()
        if not rows:
            job_errors = list((job or {}).get("errors") or [])
            job_done = bool(job and job.get("done"))
            job_failed = bool(job_done and not (job or {}).get("ok") and job_errors)
            return jsonify({
                "has_data": False,
                "rows": [],
                "job": job,
                "job_failed": job_failed,
                "job_errors": job_errors,
                "active_contracts": sorted(active),
                "message": "Current fetch failed - no active futures OI rows were stored" if job_failed else "No active futures OI data in DB yet",
            })

        result = []
        for r in rows:
            last_date = r[2]
            try:
                ld = _dt2.date.fromisoformat(last_date)
                td = _dt2.date.today()
                days_old = max(0, (td - ld).days)
                # Business days elapsed after the stored date. Friday -> Monday is 1.
                biz_days_old = sum(1 for i in range(days_old)
                                   if (ld + _dt2.timedelta(days=i+1)).weekday() < 5)
                stale = biz_days_old > 1
                stale_msg = f" ⚠ {biz_days_old}d old" if stale else " ✅ current"
            except Exception:
                stale = False
                stale_msg = ""
                biz_days_old = 0
            is_near = r[1] in near_term
            result.append({
                "symbol":    r[0],
                "contract":  r[1],
                "last_date": last_date,
                "oi":        r[3],
                "volume":    r[4],
                "source":    r[5],
                "settle":    r[6],
                "fetched_at": r[7] or "",
                "is_today":  last_date == today,
                "near_term": is_near,
                # V104: far-dated/thin contracts are informational only --
                # they never fail the top-line badge (see any_stale below).
                "stale":     stale if is_near else False,
                "stale_display": stale,
                "stale_msg": stale_msg,
                "biz_days_old": biz_days_old,
            })

        # V104: only the front 1-2 contracts per root drive the badge.
        # Far/thin contracts are shown in `rows` with their real
        # stale_display flag, but can't make the whole page look broken.
        near_rows = [r for r in result if r["near_term"]]
        any_stale = any(r["stale_display"] for r in near_rows) if near_rows else any(r["stale_display"] for r in result)
        far_stale_count = sum(1 for r in result if not r["near_term"] and r["stale_display"])
        running = bool(job and job.get("running"))
        job_done = bool(job and job.get("done"))
        job_errors = list((job or {}).get("errors") or [])
        job_failed = bool(job_done and not (job or {}).get("ok") and job_errors)
        msg = f"{len(result)} active contracts ({len(near_rows)} near-term) · "
        if running:
            msg += "fetch running"
        elif job_failed:
            msg += "current fetch failed; showing existing DB rows"
        else:
            msg += "near-term data stale" if any_stale else "up to date"
            if far_stale_count:
                msg += f" ({far_stale_count} far-dated contract{'s' if far_stale_count != 1 else ''} thin/no recent data, informational only)"
        return jsonify({
            "has_data": True,
            "rows": result,
            "count": len(result),
            "any_stale": any_stale,
            "far_stale_count": far_stale_count,
            "today": today,
            "job": job,
            "job_failed": job_failed,
            "job_errors": job_errors,
            "active_contracts": sorted(active),
            "near_term_contracts": sorted(near_term),
            "message": msg,
        })
    except Exception as e:
        return jsonify({"has_data": False, "job": job, "error": str(e)})

@api_bp.route("/futures/clear", methods=["POST"])
def futures_clear():
    """Wipe both futures tables so we can test clean Schwab fetch."""
    import sqlite3
    from pathlib import Path
    from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
    DB = _OIAPP_DB_PATH
    tables_cleared = []
    try:
        con = sqlite3.connect(DB)
        for tbl in ("futures_oi_daily", "futures_oi"):
            try:
                before = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                con.execute(f"DELETE FROM {tbl}")
                con.commit()
                tables_cleared.append({"table": tbl, "rows_deleted": before})
            except Exception as e:
                tables_cleared.append({"table": tbl, "error": str(e)})
        con.close()
        return jsonify({"ok": True, "cleared": tables_cleared})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── Momentum Retracement Scanner ─────────────────────────────────────────────
@api_bp.route("/scanner/momentum_retrace", methods=["GET","POST"])
def momentum_retrace_scan():
    """First pullback after sharp momentum move — bull and bear."""
    d             = request.get_json(silent=True) or {}
    def _p(k, default): return request.args.get(k, d.get(k, default))
    move_window   = int(_p("lookback",     15))
    move_pct      = float(_p("move_pct",   6.0))
    rsi_ob        = int(_p("rsi_peak",     68))
    rsi_os        = int(_p("rsi_trough",   32))
    delta_rsi_thr = int(_p("delta_rsi",    18))
    retrace_min   = int(_p("retrace_min",  3))
    retrace_max   = int(_p("retrace_max",  8))
    recovery_days = int(_p("recovery_days", 5))
    min_earn_days = _p("min_earn_days", None)
    if min_earn_days in ("", "None", "null"):
        min_earn_days = None
    else:
        try:
            min_earn_days = int(min_earn_days)
        except Exception:
            min_earn_days = None
    symbols_raw   = _p("symbols", "")
    watchlist_id  = _p("watchlist_id", None)
    symbols       = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()] or None
    if not symbols and watchlist_id:
        try:
            from ..db import _connect as _mr_con
            _c = _mr_con()
            rows = _c.execute("SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol", (int(watchlist_id),)).fetchall()
            _c.close()
            symbols = [r[0] for r in rows] if rows else None
        except Exception:
            pass
    workers       = int(_p("workers", 25))
    try:
        from ..scanners.momentum_retrace_scanner import run_momentum_retrace_scan
        result = run_momentum_retrace_scan(
            symbols=symbols, lookback=move_window, workers=workers,
            rsi_peak_thr=rsi_ob, rsi_trough_thr=rsi_os,
            delta_rsi_thr=delta_rsi_thr, move_pct=move_pct,
            retrace_min=retrace_min, retrace_max=retrace_max,
            recovery_days=recovery_days
        )
        if min_earn_days is not None:
            from ..scanners.scoring_service import filter_by_min_earnings
            result["bulls"] = filter_by_min_earnings(result.get("bulls", []), min_earn_days)
            result["bears"] = filter_by_min_earnings(result.get("bears", []), min_earn_days)
            result["bull_count"] = len(result["bulls"])
            result["bear_count"] = len(result["bears"])
            result["count"] = result["bull_count"] + result["bear_count"]
        if watchlist_id:
            result.setdefault("params", {})["watchlist_id"] = int(watchlist_id)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500



# ── Trend Exhaustion Second Pullback Scanner ─────────────────────────────────
@api_bp.route("/scanner/momentum_retrace_exhaustion", methods=["GET", "POST"])
def momentum_retrace_exhaustion_scan():
    """Find stocks that had an exhaustion signal in the last X days and are now on the second pullback."""
    d = request.get_json(silent=True) or {}
    def _p(k, default):
        return request.args.get(k, d.get(k, default))

    watchlist_id = _p("watchlist_id", None)
    symbols_raw = _p("symbols", "")
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()] or None
    if not symbols and watchlist_id:
        try:
            from ..db import _connect as _mr_con
            _c = _mr_con()
            rows = _c.execute("SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol", (int(watchlist_id),)).fetchall()
            _c.close()
            symbols = [r[0] for r in rows] if rows else None
        except Exception:
            pass

    lookback_days = int(_p("lookback_days", 20))
    rsi_hi = float(_p("rsi_hi", 68))
    rsi_lo = float(_p("rsi_lo", 32))
    diff_thr = float(_p("diff_thr", 20))
    min_bounce_pct = float(_p("min_bounce_pct", 4))
    min_second_bars = int(_p("min_second_bars", 1))
    min_earn_days = _p("min_earn_days", None)
    if min_earn_days in ("", "None", "null"):
        min_earn_days = None
    else:
        try:
            min_earn_days = int(min_earn_days)
        except Exception:
            min_earn_days = None
    exhaust_tf = str(_p("exhaust_tf", "1d")).strip() or "1d"
    pullback_tf = str(_p("pullback_tf", "1h")).strip() or "1h"
    workers = int(_p("workers", 18))

    try:
        from ..scanners.trend_second_pullback_scanner import run_trend_second_pullback_scan
        result = run_trend_second_pullback_scan(
            symbols=symbols,
            watchlist_id=int(watchlist_id) if watchlist_id else None,
            workers=workers,
            lookback_days=lookback_days,
            rsi_hi=rsi_hi,
            rsi_lo=rsi_lo,
            diff_thr=diff_thr,
            min_bounce_pct=min_bounce_pct,
            min_second_bars=min_second_bars,
            exhaust_tf=exhaust_tf,
            pullback_tf=pullback_tf,
        )
        if min_earn_days is not None:
            from ..scanners.scoring_service import filter_by_min_earnings
            result["bulls"] = filter_by_min_earnings(result.get("bulls", []), min_earn_days)
            result["bears"] = filter_by_min_earnings(result.get("bears", []), min_earn_days)
            result["bull_count"] = len(result["bulls"])
            result["bear_count"] = len(result["bears"])
            result["count"] = result["bull_count"] + result["bear_count"]
        if watchlist_id:
            result.setdefault("params", {})["watchlist_id"] = int(watchlist_id)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500

# ── RSI Multi-Timeframe Scanner ───────────────────────────────────────────────
@api_bp.route("/scanner/rsi_mtf", methods=["GET","POST"])
def rsi_mtf_scan():
    """Daily RSI delta diverging from intraday RSI delta — bull/bear retrace scanner."""
    try:
        from ..scanners.rsi_mtf_scanner import run_rsi_mtf_scan, load_params
        # Allow param override from request
        saved = load_params()
        req   = request.get_json(silent=True) or {}
        for k in saved:
            if k in req: saved[k] = req[k]
        # Also check query args
        for k in ["daily_delta_bull","daily_delta_bear","intra_delta_bull","intra_delta_bear",
                  "daily_lookback","intra_lookback","intra_tf","rsi_period","min_price","symbols"]:
            if request.args.get(k) is not None:
                v = request.args.get(k)
                try: v = float(v) if "." in v else int(v)
                except: pass
                saved[k] = v
        for kb in ["macd_filter","ema_filter"]:
            if request.args.get(kb) is not None:
                saved[kb] = request.args.get(kb).lower() in ("1","true","yes")
        # Support watchlist_id — override symbols from watchlist
        min_earn_days = request.args.get("min_earn_days", None)
        if min_earn_days in (None, "", "None", "null"):
            min_earn_days = req.get("min_earn_days")
        if min_earn_days in (None, "", "None", "null"):
            min_earn_days = None
        else:
            try:
                min_earn_days = int(min_earn_days)
            except Exception:
                min_earn_days = None
        wl_id = request.args.get("watchlist_id", None, type=int) or req.get("watchlist_id")
        if wl_id:
            try:
                from ..db import _connect as _rwlcon
                _wlc = _rwlcon()
                wl_rows = _wlc.execute(
                    "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
                    (int(wl_id),)
                ).fetchall()
                _wlc.close()
                if wl_rows:
                    saved["symbols"] = ",".join(r[0] for r in wl_rows)
            except: pass
        result = run_rsi_mtf_scan(params=saved, workers=25)
        if min_earn_days is not None:
            from ..scanners.scoring_service import filter_by_min_earnings
            result["bulls"] = filter_by_min_earnings(result.get("bulls", []), min_earn_days)
            result["bears"] = filter_by_min_earnings(result.get("bears", []), min_earn_days)
            result["bull_count"] = len(result["bulls"])
            result["bear_count"] = len(result["bears"])
            result["total_scanned"] = result.get("total_scanned", 0)
        # Save to cache
        import json as _rj, datetime as _rd
        from ..db import _connect as _rc
        _ts_rsi = _rd.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            _cc = _rc()
            _cc.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
            _cc.execute("INSERT OR REPLACE INTO app_cache VALUES ('rsi_mtf_scan',?,?)",
                        (_rj.dumps(result), _ts_rsi))
            _cc.commit(); _cc.close()
        except: pass
        result["completed_at"] = _ts_rsi
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500

@api_bp.route("/scanner/rsi_mtf/params", methods=["GET"])
def rsi_mtf_get_params():
    from ..scanners.rsi_mtf_scanner import load_params
    return jsonify(load_params())

@api_bp.route("/scanner/rsi_mtf/params", methods=["POST"])
def rsi_mtf_save_params():
    from ..scanners.rsi_mtf_scanner import save_params, load_params
    d = request.get_json(force=True) or {}
    current = load_params()
    current.update(d)
    save_params(current)
    return jsonify({"ok": True, "params": current})

@api_bp.route("/rsi_mtf_cache", methods=["GET"])
def get_rsi_mtf_cache():
    import json as _json
    from ..db import _connect
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key='rsi_mtf_scan'").fetchone()
        if row:
            d = _json.loads(row[0])
            return jsonify({"bulls": d.get("bulls",[]), "bears": d.get("bears",[]),
                            "bull_count": d.get("bull_count",0), "bear_count": d.get("bear_count",0),
                            "total_scanned": d.get("total_scanned",0),
                            "params": d.get("params",{}), "date": row[1], "from_cache": True})
        return jsonify({"bulls": [], "bears": [], "date": None, "from_cache": True})
    except Exception as e:
        return jsonify({"bulls": [], "bears": [], "error": str(e)})
    finally: con.close()

@api_bp.route("/rsi_mtf_cache", methods=["POST"])
def save_rsi_mtf_cache():
    import json as _json
    from ..db import _connect
    import datetime as _dt
    d = request.get_json(force=True) or {}
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES ('rsi_mtf_scan',?,?)",
                    (_json.dumps(d), _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally: con.close()

@api_bp.route("/sr_breakout_cache", methods=["GET"])
def get_sr_breakout_cache():
    import json as _json
    from ..db import _connect
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        row = con.execute("SELECT value, updated FROM app_cache WHERE key='sr_breakout_scan'").fetchone()
        if row:
            return jsonify({"results": _json.loads(row[0]), "date": row[1], "from_cache": True})
        return jsonify({"results": [], "date": None, "from_cache": True})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})
    finally: con.close()

@api_bp.route("/sr_breakout_cache", methods=["POST"])
def save_sr_breakout_cache():
    import json as _json
    from ..db import _connect
    import datetime as _dt
    d = request.get_json(force=True) or {}
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        con.execute("INSERT OR REPLACE INTO app_cache VALUES ('sr_breakout_scan',?,?)",
                    (_json.dumps(d), _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally: con.close()


# ── Trend Exhaustion Scanner ───────────────────────────────────────────────
@api_bp.route("/scanner/trend_exhaustion", methods=["GET"])
def trend_exhaustion_scan():
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    min_earn_days = request.args.get("min_earn_days", None)
    if min_earn_days in (None, "", "None", "null"):
        min_earn_days = None
    else:
        try:
            min_earn_days = int(min_earn_days)
        except Exception:
            min_earn_days = None
    symbols_raw = request.args.get("symbols", "")
    sector = (request.args.get("sector") or "").strip()
    workers = request.args.get("workers", 18, type=int)
    min_score = request.args.get("min_score", 50, type=int)
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()] or None
    try:
        from ..scanners.trend_exhaustion_scanner import run_trend_exhaustion_scan
        result = run_trend_exhaustion_scan(symbols=symbols, watchlist_id=watchlist_id, workers=workers)
        rows = result.get("results", [])
        if sector:
            try:
                from ..services.sector_service import get_symbol_sector
                rows = [r for r in rows if get_symbol_sector(r.get("symbol", "")) == sector]
            except Exception:
                pass
        rows = [r for r in rows if (r.get("score") or 0) >= min_score]
        if min_earn_days is not None:
            from ..scanners.scoring_service import filter_by_min_earnings
            rows = filter_by_min_earnings(rows, min_earn_days)
        result["results"] = rows
        result["count"] = len(rows)
        import json as _j, datetime as _d
        from ..db import _connect as _c
        try:
            con = _c()
            con.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
            con.execute("INSERT OR REPLACE INTO app_cache VALUES ('trend_exhaustion_scan', ?, ?)", (_j.dumps(rows), _d.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            con.commit(); con.close()
        except Exception:
            pass
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500


# =====================================================================
# V104: 0-10 DTE Intraday Buildup + Positional Trend pages
# (SPY / QQQ / SPX / IWM). Backed by oiapp/services/dte_pages.py.
# Pages read pre-built cache tables; nothing here fires a live scan.
#
# NOTE: api_bp already carries url_prefix="/api" (see top of file), so
# routes registered on it must NOT repeat "/api" in the path -- that
# was the bug in the first cut of this file (routes ended up at
# /api/api/dte/... -> 404). The two page routes below intentionally
# live on a separate, prefix-free blueprint so they resolve to
# /dte/intraday and /dte/positional exactly as documented on the pages
# themselves and in the chat. The JSON + refresh endpoints stay on
# api_bp and resolve to /api/dte/intraday_data, /api/dte/positional_data,
# /api/dte/refresh.
# =====================================================================

dte_pages_bp = Blueprint("dte_pages_bp", __name__)


@dte_pages_bp.route("/dte/intraday")
def dte_intraday_page():
    from ..services.dte_pages import TRACKED_SYMBOLS
    return render_template("dte_intraday.html", symbols=TRACKED_SYMBOLS)


@dte_pages_bp.route("/dte/positional")
def dte_positional_page():
    from ..services.dte_pages import TRACKED_SYMBOLS
    return render_template("dte_positional.html", symbols=TRACKED_SYMBOLS)


@api_bp.route("/dte/intraday_data")
def dte_intraday_data():
    from ..services.dte_pages import read_intraday_cache
    symbol = sanitize_symbol(request.args.get("symbol", "")) or None
    try:
        rows = read_intraday_cache(symbol)
        return jsonify({"ok": True, "rows": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"ok": False, "rows": [], "error": str(e)}), 500


@api_bp.route("/dte/positional_data")
def dte_positional_data():
    from ..services.dte_pages import read_positional_cache, get_vix_skew_panel, get_futures_context
    symbol = sanitize_symbol(request.args.get("symbol", "")) or None
    try:
        rows = read_positional_cache(symbol)
        return jsonify({
            "ok": True,
            "rows": rows,
            "count": len(rows),
            "vix_skew": get_vix_skew_panel(),
            "futures": get_futures_context(),
        })
    except Exception as e:
        return jsonify({"ok": False, "rows": [], "error": str(e)}), 500


@api_bp.route("/dte/refresh", methods=["POST"])
def dte_refresh():
    """Manual 'Refresh Now' for either page. Synchronous -- the
    computation is a handful of symbols x a handful of expiries, so it
    finishes in low single-digit seconds and doesn't need a background
    job/polling UI the way the Schwab futures fetch does."""
    from ..services.dte_pages import run_full_refresh, run_intraday_refresh_only
    scope = request.args.get("scope", "full")
    symbol = sanitize_symbol(request.args.get("symbol", "")) or None
    symbols = [symbol] if symbol else None
    try:
        if scope == "intraday":
            result = run_intraday_refresh_only(symbols)
        else:
            result = run_full_refresh(symbols)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =====================================================================
# V106: merged DTE page -- one page instead of two, PCR strip (volume-
# based, all expiries in the 0-10 DTE window) at top, expiry dropdown,
# and a per-expiry detail view that defaults to the volume chart for
# near-dated (0-2 DTE) expiries and the OI-trend chart for the rest --
# volume matters most for 0DTE, OI trend matters more further out.
# =====================================================================

@dte_pages_bp.route("/dte/merged")
def dte_merged_page():
    from ..services.dte_pages import TRACKED_SYMBOLS
    return render_template("dte_merged.html", symbols=TRACKED_SYMBOLS)


@api_bp.route("/dte/pcr_strip")
def dte_pcr_strip():
    from ..services.dte_pages import get_pcr_strip
    symbol = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    try:
        rows = get_pcr_strip(symbol)
        return jsonify({"ok": True, "symbol": symbol, "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "rows": [], "error": str(e)}), 500


@api_bp.route("/dte/expiry_detail")
def dte_expiry_detail():
    from ..services.dte_pages import get_expiry_detail, get_dte_expirations
    symbol = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    expiration = request.args.get("expiration", "").strip()
    try:
        if not expiration:
            exps = get_dte_expirations(symbol)
            expiration = exps[0] if exps else ""
        if not expiration:
            return jsonify({"ok": False, "error": "no expiries in 0-10 DTE window"}), 404
        detail = get_expiry_detail(symbol, expiration)
        return jsonify({"ok": True, **detail})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/dte/live_volume")
def dte_live_volume():
    """V108: real-time, uncached volume per strike, straight from
    Schwab. Called on every load/refresh of the merged DTE page's
    Volume view -- OI stays DB-cached (see /api/dte/expiry_detail),
    only volume needs to be live since it changes continuously
    through the day."""
    from ..services.schwab_options_chain import fetch_live_volume_schwab
    symbol = sanitize_symbol(request.args.get("symbol", "SPY")) or "SPY"
    expiration = request.args.get("expiration", "").strip() or None
    try:
        result = fetch_live_volume_schwab(symbol, expiration)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "rows": [], "error": str(e)}), 500


# =====================================================================
# V113: ICICI Direct auto-trading -- multi-leg (vertical) order
# execution with safe leg sequencing, position tracking, live P&L,
# and target/stop-loss auto-close. See vertical_executor.py for the
# core safety logic (LONG legs open first / SHORT legs close first).
# =====================================================================

@dte_pages_bp.route("/icici/auto-trading")
def icici_auto_trading_page():
    return render_template("icici_auto_trading.html")


@api_bp.route("/icici/session", methods=["POST"])
def icici_set_session():
    """Paste today's Breeze session token here once per trading day --
    see icici_breeze.py module docstring for why this can't be fully
    automated (Breeze's login flow requires a browser step)."""
    from ..services.icici_breeze import set_session_token
    payload = request.get_json(force=True) or {}
    token = str(payload.get("session_token") or "").strip()
    result = set_session_token(token)
    return jsonify(result), (200 if result.get("ok") else 400)


@api_bp.route("/icici/session/status")
def icici_session_status():
    from ..services.icici_breeze import session_status
    return jsonify(session_status())


@api_bp.route("/icici/session/connect", methods=["POST"])
def icici_session_connect():
    """Explicit 'Connect' action -- tries to re-establish the session
    from whatever token was last saved to the DB, without requiring a
    fresh paste. Useful right after an app restart. If the saved token
    has actually expired, returns a clear error saying so."""
    from ..services.icici_breeze import try_reconnect
    result = try_reconnect()
    return jsonify(result), (200 if result.get("ok") else 400)


@api_bp.route("/icici/positions", methods=["GET"])
def icici_list_positions():
    from ..services.icici_positions import list_positions
    status = request.args.get("status", "").strip() or None
    try:
        return jsonify({"ok": True, "positions": list_positions(status)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/positions/<int:position_id>/log")
def icici_position_log(position_id):
    from ..services.icici_positions import get_order_log
    try:
        return jsonify({"ok": True, "log": get_order_log(position_id)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/positions/open", methods=["POST"])
def icici_open_position():
    from ..services.icici_positions import open_position
    payload = request.get_json(force=True) or {}
    try:
        legs = payload.get("legs") or []
        for i, leg in enumerate(legs):
            leg.setdefault("leg_index", i)
        # Per-leg limit price: any leg with a positive "limit_price" in
        # its payload goes as a limit order at that price; legs without
        # one use the position's default order_type (normally market).
        price_by_leg = {
            leg["leg_index"]: float(leg["limit_price"])
            for leg in legs if leg.get("limit_price") not in (None, "", 0)
        }
        result = open_position(
            strategy_name=str(payload.get("strategy_name") or "").strip(),
            stock_code=str(payload.get("stock_code") or "").strip().upper(),
            expiry_date=str(payload.get("expiry_date") or "").strip(),
            legs=legs,
            target_pnl_rupees=float(payload.get("target_pnl_rupees") or 0),
            stop_loss_pnl_rupees=float(payload.get("stop_loss_pnl_rupees") or 0),
            lot_size=int(payload.get("lot_size") or 1),
            order_type=str(payload.get("order_type") or "market"),
            execution_mode=str(payload.get("execution_mode") or "safe_sequential"),
            price_by_leg=price_by_leg,
            dry_run=bool(payload.get("dry_run")),
        )
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/positions/<int:position_id>/close", methods=["POST"])
def icici_close_position(position_id):
    from ..services.icici_positions import close_position
    payload = request.get_json(force=True) or {}
    try:
        result = close_position(
            position_id, reason=str(payload.get("reason") or "MANUAL"),
            execution_mode_override=payload.get("execution_mode") or None,
        )
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/monitor/tick", methods=["POST"])
def icici_monitor_tick():
    """Manual 'check P&L now' -- same function the background job calls."""
    from ..services.icici_positions import monitor_tick
    try:
        return jsonify(monitor_tick())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/monitor/status")
def icici_monitor_status():
    """Self-diagnosis for 'is the 30s background poll actually running'
    -- reads the real last-run timestamp from job_registry rather than
    just asserting it's scheduled. If this is stale (many minutes old,
    or null) despite the app having been up for a while, the
    background dispatcher isn't reaching this job -- worth checking
    Scheduler Hub directly for the job's enabled state."""
    import time as _time
    from ..services.job_registry import get_last_run_at
    try:
        last = get_last_run_at("icici_pnl_monitor")
        return jsonify({
            "ok": True,
            "last_run_at": last,
            "seconds_ago": (round(_time.time() - last, 1) if last else None),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/positions/<int:position_id>", methods=["DELETE"])
def icici_delete_position(position_id):
    from ..services.icici_positions import delete_position
    try:
        result = delete_position(position_id)
        return jsonify(result), (200 if result.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/positions/<int:position_id>/mark_closed", methods=["POST"])
def icici_mark_closed(position_id):
    from ..services.icici_positions import mark_closed_manual
    payload = request.get_json(force=True) or {}
    try:
        result = mark_closed_manual(position_id, note=str(payload.get("note") or ""))
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/positions/<int:position_id>/retry_close", methods=["POST"])
def icici_retry_close(position_id):
    from ..services.icici_positions import retry_close
    payload = request.get_json(force=True) or {}
    try:
        result = retry_close(position_id, execution_mode_override=payload.get("execution_mode") or None)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/broker_positions")
def icici_broker_positions():
    """Raw live positions from the broker (whether opened through this
    app or not) -- used to reconcile against our tracked table."""
    from ..services.icici_breeze import get_portfolio_positions
    from ..services.icici_positions import list_positions
    try:
        broker = get_portfolio_positions()
        if not broker.get("ok"):
            return jsonify(broker), 400
        # Flag which broker positions are already represented in a
        # tracked row (best-effort match on stock_code/expiry/right/
        # strike) so the UI can distinguish "already monitored" from
        # "not yet tracked -- offer to adopt".
        tracked = list_positions(status="OPEN")
        tracked_keys = set()
        for pos in tracked:
            for leg in pos.get("legs", []):
                tracked_keys.add((pos["stock_code"], pos["expiry_date"], leg.get("right"), leg.get("strike_price")))
        for p in broker["positions"]:
            key = (p["stock_code"], p["expiry_date"], p["right"], p["strike_price"])
            p["already_tracked"] = key in tracked_keys
        return jsonify(broker)
    except Exception as e:
        return jsonify({"ok": False, "positions": [], "error": str(e)}), 500


@api_bp.route("/icici/positions/adopt", methods=["POST"])
def icici_adopt_positions():
    from ..services.icici_positions import adopt_broker_positions
    payload = request.get_json(force=True) or {}
    try:
        legs = payload.get("legs") or []
        for i, leg in enumerate(legs):
            leg.setdefault("leg_index", i)
        result = adopt_broker_positions(
            strategy_name=str(payload.get("strategy_name") or "").strip(),
            stock_code=str(payload.get("stock_code") or "").strip().upper(),
            expiry_date=str(payload.get("expiry_date") or "").strip(),
            legs=legs,
            target_pnl_rupees=float(payload.get("target_pnl_rupees") or 0),
            stop_loss_pnl_rupees=float(payload.get("stop_loss_pnl_rupees") or 0),
            lot_size=int(payload.get("lot_size") or 1),
            execution_mode=str(payload.get("execution_mode") or "safe_sequential"),
        )
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/option_chain")
def icici_option_chain():
    from ..services.icici_breeze import get_option_chain, get_spot_price
    stock_code = request.args.get("stock_code", "").strip().upper()
    expiry_date = request.args.get("expiry_date", "").strip()
    if not stock_code or not expiry_date:
        return jsonify({"ok": False, "error": "stock_code and expiry_date are required"}), 400
    try:
        result = get_option_chain(stock_code, expiry_date)
        spot_result = get_spot_price(stock_code)
        result["spot"] = spot_result.get("spot")
        result["spot_error"] = spot_result.get("error") if not spot_result.get("ok") else None
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "rows": [], "error": str(e)}), 500


@api_bp.route("/icici/config", methods=["GET"])
def icici_get_config():
    """Mirrors /schwab/config -- masked view of stored credentials."""
    from ..services.icici_breeze import get_config
    cfg = get_config()
    if not cfg:
        return jsonify({"configured": False})
    safe = {
        k: (v[:8] + "..." if k in ("api_secret", "session_token") and v and len(v) > 8 else v)
        for k, v in cfg.items() if k != "id"
    }
    safe["configured"] = bool(cfg.get("api_key") and cfg.get("api_secret"))
    return jsonify(safe)


@api_bp.route("/icici/config", methods=["POST"])
def icici_save_config():
    from ..services.icici_breeze import save_config
    d = request.get_json(force=True) or {}
    save_config(api_key=d.get("api_key", ""), api_secret=d.get("api_secret", ""))
    return jsonify({"ok": True})


@api_bp.route("/icici/expiries")
def icici_expiries():
    from ..services.icici_breeze import get_available_expiries
    stock_code = request.args.get("stock_code", "").strip().upper()
    if not stock_code:
        return jsonify({"ok": False, "expiries": [], "error": "stock_code required"}), 400
    try:
        return jsonify(get_available_expiries(stock_code))
    except Exception as e:
        return jsonify({"ok": False, "expiries": [], "error": str(e)}), 500


@api_bp.route("/icici/leg_price")
def icici_leg_price():
    """Live price for a single leg while building a strategy in the
    Open New Position form -- thin wrapper over get_quote()."""
    from ..services.icici_breeze import get_quote
    stock_code = request.args.get("stock_code", "").strip().upper()
    expiry_date = request.args.get("expiry_date", "").strip()
    right = request.args.get("right", "").strip().lower()
    try:
        strike_price = float(request.args.get("strike_price", "0"))
    except ValueError:
        return jsonify({"ok": False, "ltp": None, "error": "invalid strike_price"}), 400
    if not stock_code or not expiry_date or right not in ("call", "put") or not strike_price:
        return jsonify({"ok": False, "ltp": None, "error": "stock_code, expiry_date, right, strike_price all required"}), 400
    try:
        return jsonify(get_quote(stock_code, expiry_date, right, strike_price))
    except Exception as e:
        return jsonify({"ok": False, "ltp": None, "error": str(e)}), 500


# =====================================================================
# V125: ICICI Strategy Engine -- reusable strategy definitions (legs
# relative to spot via ITM/ATM/OTM, opening/closing conditions, target/
# stop-loss, re-entry cap) that fire tracked positions automatically.
# See icici_strategy_engine.py for the evaluator logic.
# =====================================================================

@api_bp.route("/icici/strategies", methods=["GET"])
def icici_list_strategies():
    from ..services.icici_strategy_engine import list_strategies
    try:
        return jsonify({"ok": True, "strategies": list_strategies()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/strategies", methods=["POST"])
def icici_create_strategy():
    from ..services.icici_strategy_engine import create_strategy
    payload = request.get_json(force=True) or {}
    try:
        legs = payload.get("legs") or []
        for i, leg in enumerate(legs):
            leg.setdefault("leg_index", i)
        payload["legs"] = legs
        result = create_strategy(payload)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/strategies/<int:strategy_id>", methods=["DELETE"])
def icici_delete_strategy(strategy_id):
    from ..services.icici_strategy_engine import delete_strategy
    try:
        result = delete_strategy(strategy_id)
        return jsonify(result), (200 if result.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/strategies/<int:strategy_id>/enable", methods=["POST"])
def icici_enable_strategy(strategy_id):
    from ..services.icici_strategy_engine import set_enabled
    try:
        result = set_enabled(strategy_id, True)
        return jsonify(result), (200 if result.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/strategies/<int:strategy_id>/disable", methods=["POST"])
def icici_disable_strategy(strategy_id):
    from ..services.icici_strategy_engine import set_enabled
    try:
        result = set_enabled(strategy_id, False)
        return jsonify(result), (200 if result.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/strategies/<int:strategy_id>/reset_reentries", methods=["POST"])
def icici_reset_reentries(strategy_id):
    from ..services.icici_strategy_engine import reset_reentries
    try:
        result = reset_reentries(strategy_id)
        return jsonify(result), (200 if result.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/strategies/evaluate_now", methods=["POST"])
def icici_evaluate_strategies_now():
    from ..services.icici_strategy_engine import evaluate_strategies_tick
    try:
        return jsonify(evaluate_strategies_tick())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/live_trading/status")
def icici_live_trading_status():
    from ..services.icici_breeze import is_live_trading_enabled
    return jsonify({"enabled": is_live_trading_enabled()})


@api_bp.route("/icici/live_trading/toggle", methods=["POST"])
def icici_live_trading_toggle():
    """Master switch: when disabled, no NEW real positions open (see
    icici_strategy_engine._fire_strategy) -- strategies instead open
    fully-tracked paper positions with live entry prices. Positions
    already open when this gets flipped keep being managed by their
    normal rules either way (this only gates NEW opens, never touches
    something already running)."""
    from ..services.icici_breeze import set_live_trading_enabled, is_live_trading_enabled
    payload = request.get_json(force=True) or {}
    set_live_trading_enabled(bool(payload.get("enabled")))
    return jsonify({"ok": True, "enabled": is_live_trading_enabled()})


@api_bp.route("/icici/diagnostics/run", methods=["POST"])
def icici_run_diagnostics():
    from ..services.icici_diagnostics import run_diagnostics
    payload = request.get_json(force=True) or {}
    test_stock_code = str(payload.get("test_stock_code") or "NIFTY").strip().upper()
    try:
        return jsonify(run_diagnostics(test_stock_code))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/icici/market_hours/status")
def icici_market_hours_status():
    from ..services.icici_breeze import is_nse_market_hours
    return jsonify({"is_market_hours": is_nse_market_hours()})


# =====================================================================
# V134: Schwab auto-trading -- mirrors the ICICI system's architecture
# (safe leg sequencing, position tracking, strategy engine, paper mode,
# diagnostics), generalized to support stock legs alongside options.
# Built on the EXISTING Schwab OAuth infrastructure (oiapp.schwab.
# schwab_routes, oiapp.services.futures_oi_schwab) -- no new auth flow.
# =====================================================================

@dte_pages_bp.route("/schwab/auto-trading")
def schwab_auto_trading_page():
    return render_template("schwab_auto_trading.html")


@api_bp.route("/schwab-auto/connection_status")
def schwab_auto_connection_status():
    from ..services.schwab_trading import is_connected, has_account_hash
    return jsonify({"connected": is_connected(), "has_account_hash": has_account_hash()})


@api_bp.route("/schwab-auto/live_trading/status")
def schwab_auto_live_status():
    from ..services.schwab_trading import is_live_trading_enabled
    return jsonify({"enabled": is_live_trading_enabled()})


@api_bp.route("/schwab-auto/live_trading/toggle", methods=["POST"])
def schwab_auto_live_toggle():
    from ..services.schwab_trading import set_live_trading_enabled, is_live_trading_enabled
    payload = request.get_json(force=True) or {}
    set_live_trading_enabled(bool(payload.get("enabled")))
    return jsonify({"ok": True, "enabled": is_live_trading_enabled()})


@api_bp.route("/schwab-auto/market_hours/status")
def schwab_auto_market_hours_status():
    from ..services.schwab_trading import is_market_hours
    return jsonify({"is_market_hours": is_market_hours()})


@api_bp.route("/schwab-auto/expiries")
def schwab_auto_expiries():
    from ..services.schwab_trading import get_available_expiries
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"ok": False, "expiries": [], "error": "symbol required"}), 400
    try:
        return jsonify(get_available_expiries(symbol))
    except Exception as e:
        return jsonify({"ok": False, "expiries": [], "error": str(e)}), 500


@api_bp.route("/schwab-auto/option_chain")
def schwab_auto_option_chain():
    from ..services.schwab_trading import get_option_chain
    symbol = request.args.get("symbol", "").strip().upper()
    expiry_date = request.args.get("expiry_date", "").strip()
    if not symbol or not expiry_date:
        return jsonify({"ok": False, "error": "symbol and expiry_date required"}), 400
    try:
        return jsonify(get_option_chain(symbol, expiry_date))
    except Exception as e:
        return jsonify({"ok": False, "rows": [], "error": str(e)}), 500


@api_bp.route("/schwab-auto/leg_price")
def schwab_auto_leg_price():
    from ..services.schwab_trading import get_quote, to_occ_symbol
    symbol = request.args.get("symbol", "").strip().upper()
    instrument_type = request.args.get("instrument_type", "stock").strip().lower()
    try:
        if instrument_type == "stock":
            return jsonify(get_quote(symbol))
        expiry_date = request.args.get("expiry_date", "").strip()
        right = request.args.get("right", "").strip().lower()
        strike_price = float(request.args.get("strike_price", "0"))
        occ = to_occ_symbol(symbol, expiry_date, right, strike_price)
        return jsonify(get_quote(occ))
    except Exception as e:
        return jsonify({"ok": False, "ltp": None, "error": str(e)}), 500


@api_bp.route("/schwab-auto/broker_positions")
def schwab_auto_broker_positions():
    from ..services.schwab_trading import get_account_positions
    from ..services.schwab_positions import list_positions, parse_occ_option_symbol
    try:
        broker = get_account_positions()
        if not broker.get("ok"):
            return jsonify(broker), 400
        tracked = list_positions(status="OPEN")
        tracked_keys = set()
        for pos in tracked:
            for leg in pos.get("legs", []):
                tracked_keys.add((pos["stock_code"], leg.get("instrument_type"), leg.get("right"), leg.get("strike_price")))
        for p in broker["positions"]:
            if p.get("asset_type") == "EQUITY":
                key = (p["symbol"], "stock", None, None)
            else:
                parsed = parse_occ_option_symbol(p["symbol"])
                # Same bug pattern as before if this parse fails -- fall
                # back to a key that will never match rather than
                # silently mismatching against garbage.
                key = (parsed["underlying"], "option", parsed["right"], parsed["strike_price"]) if parsed else (None, None, None, None)
            p["already_tracked"] = key in tracked_keys
        return jsonify(broker)
    except Exception as e:
        return jsonify({"ok": False, "positions": [], "error": str(e)}), 500


@api_bp.route("/schwab-auto/positions", methods=["GET"])
def schwab_auto_list_positions():
    from ..services.schwab_positions import list_positions
    status = request.args.get("status", "").strip() or None
    try:
        return jsonify({"ok": True, "positions": list_positions(status)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/positions/<int:position_id>/log")
def schwab_auto_position_log(position_id):
    from ..services.schwab_positions import get_order_log
    try:
        return jsonify({"ok": True, "log": get_order_log(position_id)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/order_log")
def schwab_auto_global_order_log():
    from ..services.schwab_positions import get_all_order_logs
    since = request.args.get("since", "").strip() or None
    try:
        return jsonify({"ok": True, "log": get_all_order_logs(since=since)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/positions/<int:position_id>/live_legs")
def schwab_auto_position_live_legs(position_id):
    from ..services.schwab_positions import list_positions
    from ..services.schwab_vertical_executor import compute_combined_pnl
    try:
        pos = next((p for p in list_positions() if p["id"] == position_id), None)
        if not pos:
            return jsonify({"ok": False, "error": "position not found"}), 404
        result = compute_combined_pnl(pos["stock_code"], pos["legs"])
        # Merge entry_price (already stored on each leg) alongside the
        # live current price/pnl this call computes fresh -- one
        # response with everything the UI needs per leg.
        entry_by_idx = {leg["leg_index"]: leg.get("entry_price") for leg in pos["legs"]}
        for lp in result.get("leg_pnls", []):
            lp["entry_price"] = entry_by_idx.get(lp["leg_index"])
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/positions/open", methods=["POST"])
def schwab_auto_open_position():
    from ..services.schwab_positions import open_position
    payload = request.get_json(force=True) or {}
    try:
        legs = payload.get("legs") or []
        for i, leg in enumerate(legs):
            leg.setdefault("leg_index", i)
        price_by_leg = {leg["leg_index"]: float(leg["limit_price"]) for leg in legs if leg.get("limit_price") not in (None, "", 0)}
        result = open_position(
            strategy_name=str(payload.get("strategy_name") or "").strip(),
            stock_code=str(payload.get("stock_code") or "").strip().upper(),
            legs=legs,
            target_pnl=float(payload.get("target_pnl") or 0),
            stop_loss_pnl=float(payload.get("stop_loss_pnl") or 0),
            order_type=str(payload.get("order_type") or "MARKET"),
            execution_mode=str(payload.get("execution_mode") or "safe_sequential"),
            price_by_leg=price_by_leg,
            dry_run=bool(payload.get("dry_run")),
            combo_net_price=(float(payload["combo_net_price"]) if payload.get("combo_net_price") not in (None, "") else None),
            oco_target_price=(float(payload["oco_target_price"]) if payload.get("oco_target_price") not in (None, "") else None),
            oco_stop_price=(float(payload["oco_stop_price"]) if payload.get("oco_stop_price") not in (None, "") else None),
            oco_trailing=bool(payload.get("oco_trailing")),
            oco_trail_amount=(float(payload["oco_trail_amount"]) if payload.get("oco_trail_amount") not in (None, "") else None),
            oco_trail_is_percent=bool(payload.get("oco_trail_is_percent")),
            duration=str(payload.get("duration") or "DAY"),
            cancel_time=(str(payload["cancel_time"]) if payload.get("cancel_time") else None),
        )
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/orders/open")
def schwab_auto_open_orders():
    from ..services.schwab_trading import get_open_orders
    try:
        return jsonify(get_open_orders())
    except Exception as e:
        return jsonify({"ok": False, "orders": [], "error": str(e)}), 500


@api_bp.route("/schwab-auto/orders/<order_id>/cancel", methods=["POST"])
def schwab_auto_cancel_order(order_id):
    from ..services.schwab_trading import cancel_order
    try:
        result = cancel_order(order_id)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/pending_entries/check", methods=["POST"])
def schwab_auto_check_pending_entries():
    from ..services.schwab_positions import check_pending_entries
    try:
        return jsonify(check_pending_entries())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/positions/adopt", methods=["POST"])
def schwab_auto_adopt_positions():
    from ..services.schwab_positions import adopt_broker_positions
    payload = request.get_json(force=True) or {}
    try:
        legs = payload.get("legs") or []
        for i, leg in enumerate(legs):
            leg.setdefault("leg_index", i)
        result = adopt_broker_positions(
            strategy_name=str(payload.get("strategy_name") or "Adopted position").strip(),
            stock_code=str(payload.get("stock_code") or "").strip().upper(),
            legs=legs,
            target_pnl=float(payload.get("target_pnl") or 0),
            stop_loss_pnl=float(payload.get("stop_loss_pnl") or 0),
            execution_mode=str(payload.get("execution_mode") or "safe_sequential"),
        )
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/parse_occ", methods=["POST"])
def schwab_auto_parse_occ():
    from ..services.schwab_positions import parse_occ_option_symbol
    payload = request.get_json(force=True) or {}
    symbol = str(payload.get("symbol") or "")
    parsed = parse_occ_option_symbol(symbol)
    if parsed is None:
        return jsonify({"ok": False, "error": f"could not parse OCC symbol {symbol!r}"}), 400
    return jsonify({"ok": True, **parsed})


@api_bp.route("/schwab-auto/positions/<int:position_id>/close", methods=["POST"])
def schwab_auto_close_position(position_id):
    from ..services.schwab_positions import close_position
    payload = request.get_json(force=True) or {}
    try:
        result = close_position(position_id, reason=str(payload.get("reason") or "MANUAL"),
                                 execution_mode_override=payload.get("execution_mode") or None,
                                 close_pct=float(payload.get("close_pct") or 100.0))
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/positions/<int:position_id>", methods=["DELETE"])
def schwab_auto_delete_position(position_id):
    from ..services.schwab_positions import delete_position
    try:
        result = delete_position(position_id)
        return jsonify(result), (200 if result.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/positions/<int:position_id>/mark_closed", methods=["POST"])
def schwab_auto_mark_closed(position_id):
    from ..services.schwab_positions import mark_closed_manual
    payload = request.get_json(force=True) or {}
    try:
        result = mark_closed_manual(position_id, note=str(payload.get("note") or ""))
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/positions/<int:position_id>/retry_close", methods=["POST"])
def schwab_auto_retry_close(position_id):
    from ..services.schwab_positions import retry_close
    payload = request.get_json(force=True) or {}
    try:
        result = retry_close(position_id, execution_mode_override=payload.get("execution_mode") or None)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/monitor/tick", methods=["POST"])
def schwab_auto_monitor_tick():
    from ..services.schwab_positions import monitor_tick
    try:
        return jsonify(monitor_tick())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/monitor/status")
def schwab_auto_monitor_status():
    import time as _time
    from ..services.job_registry import get_last_run_at
    try:
        last = get_last_run_at("schwab_pnl_monitor")
        return jsonify({"ok": True, "last_run_at": last, "seconds_ago": (round(_time.time() - last, 1) if last else None)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/strategies", methods=["GET"])
def schwab_auto_list_strategies():
    from ..services.schwab_strategy_engine import list_strategies
    try:
        return jsonify({"ok": True, "strategies": list_strategies()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/strategies", methods=["POST"])
def schwab_auto_create_strategy():
    from ..services.schwab_strategy_engine import create_strategy
    payload = request.get_json(force=True) or {}
    try:
        legs = payload.get("legs") or []
        for i, leg in enumerate(legs):
            leg.setdefault("leg_index", i)
        payload["legs"] = legs
        result = create_strategy(payload)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/strategies/<int:strategy_id>", methods=["DELETE"])
def schwab_auto_delete_strategy(strategy_id):
    from ..services.schwab_strategy_engine import delete_strategy
    try:
        result = delete_strategy(strategy_id)
        return jsonify(result), (200 if result.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/strategies/<int:strategy_id>/enable", methods=["POST"])
def schwab_auto_enable_strategy(strategy_id):
    from ..services.schwab_strategy_engine import set_enabled
    try:
        result = set_enabled(strategy_id, True)
        return jsonify(result), (200 if result.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/strategies/<int:strategy_id>/disable", methods=["POST"])
def schwab_auto_disable_strategy(strategy_id):
    from ..services.schwab_strategy_engine import set_enabled
    try:
        result = set_enabled(strategy_id, False)
        return jsonify(result), (200 if result.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/strategies/<int:strategy_id>/reset_reentries", methods=["POST"])
def schwab_auto_reset_reentries(strategy_id):
    from ..services.schwab_strategy_engine import reset_reentries
    try:
        result = reset_reentries(strategy_id)
        return jsonify(result), (200 if result.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/strategies/evaluate_now", methods=["POST"])
def schwab_auto_evaluate_strategies_now():
    from ..services.schwab_strategy_engine import evaluate_strategies_tick
    try:
        return jsonify(evaluate_strategies_tick())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/schwab-auto/diagnostics/run", methods=["POST"])
def schwab_auto_run_diagnostics():
    from ..services.schwab_diagnostics import run_diagnostics
    payload = request.get_json(force=True) or {}
    test_symbol = str(payload.get("test_symbol") or "AAPL").strip().upper()
    try:
        return jsonify(run_diagnostics(test_symbol))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/oi_buildup_trend")
def api_oi_buildup_trend():
    """Multi-day OI buildup view -- unlike /api/oi_change (single day
    vs prior day), this walks the last N captured days for a symbol/
    expiration and returns a proper time series: total call/put OI
    each day, plus which SPECIFIC strike had the largest OI buildup
    on each side over the window (not just the largest OI overall --
    buildup means the increase from the first day in the window to
    the last), so the chart can highlight the strike that's actually
    accumulating fresh interest, not just the biggest static number.
    """
    from ..db import _connect
    symbol = (request.args.get("symbol") or "").upper().strip()
    expiration = request.args.get("expiration", "").strip()
    days = request.args.get("days", "10").strip()
    if not symbol or not expiration:
        return jsonify({"ok": False, "error": "symbol and expiration are required"}), 400
    try:
        days = max(2, int(days))
    except ValueError:
        days = 10

    con = _connect()
    try:
        date_rows = con.execute(
            "SELECT DISTINCT date FROM options WHERE UPPER(symbol)=? AND expiration=? ORDER BY date DESC LIMIT ?",
            (symbol, expiration, days)
        ).fetchall()
        dates = sorted([r["date"] for r in date_rows])
        if len(dates) < 2:
            sample_exps = con.execute(
                "SELECT DISTINCT expiration FROM options WHERE UPPER(symbol)=? ORDER BY expiration DESC LIMIT 8",
                (symbol,)
            ).fetchall()
            return jsonify({
                "ok": False,
                "error": f"only {len(dates)} day(s) of history for {symbol} {expiration!r} -- need at least 2 to show a trend",
                "debug": {
                    "sent_symbol": symbol, "sent_expiration": expiration,
                    "dates_found_for_this_exact_expiration": dates,
                    "other_expirations_available_for_this_symbol": [r["expiration"] for r in sample_exps],
                },
            }), 400

        # Per-day totals, and a per-strike OI matrix (strike -> {date: oi})
        total_call_oi, total_put_oi = [], []
        call_by_strike: dict = {}
        put_by_strike: dict = {}
        for d in dates:
            rows = con.execute(
                "SELECT type, strike, oi FROM options WHERE UPPER(symbol)=? AND expiration=? AND date=?",
                (symbol, expiration, d)
            ).fetchall()
            call_sum, put_sum = 0, 0
            for r in rows:
                oi = int(r["oi"] or 0)
                strike = float(r["strike"])
                if r["type"] == "call":
                    call_sum += oi
                    call_by_strike.setdefault(strike, {})[d] = oi
                elif r["type"] == "put":
                    put_sum += oi
                    put_by_strike.setdefault(strike, {})[d] = oi
            total_call_oi.append(call_sum)
            total_put_oi.append(put_sum)

        def _top_buildup(by_strike: dict):
            best_strike, best_buildup = None, None
            for strike, series in by_strike.items():
                # Only consider strikes with data on both the first and
                # last day of the window -- a strike that only appeared
                # partway through isn't a fair "buildup" comparison.
                if dates[0] not in series or dates[-1] not in series:
                    continue
                buildup = series[dates[-1]] - series[dates[0]]
                if best_buildup is None or buildup > best_buildup:
                    best_strike, best_buildup = strike, buildup
            if best_strike is None:
                return None
            series = by_strike[best_strike]
            return {
                "strike": best_strike, "buildup": best_buildup,
                "oi_series": [series.get(d, None) for d in dates],
            }

        def _build_matrix(by_strike: dict, top_n: int = 10):
            """Table-friendly view: top N strikes by absolute OI
            change over the window (both build-UP and build-DOWN are
            useful to see, so this ranks by magnitude, not just
            positive buildup), each with its full day-by-day series.
            No Plotly dependency at all -- a plain table always
            renders if the data exists, unlike the line chart."""
            scored = []
            for strike, series in by_strike.items():
                if dates[0] not in series or dates[-1] not in series:
                    continue
                change = series[dates[-1]] - series[dates[0]]
                scored.append((strike, change, series))
            scored.sort(key=lambda t: abs(t[1]), reverse=True)
            rows = []
            for strike, change, series in scored[:top_n]:
                rows.append({
                    "strike": strike, "change": change,
                    "values": [series.get(d, None) for d in dates],
                })
            return rows

        return jsonify({
            "ok": True, "symbol": symbol, "expiration": expiration, "dates": dates,
            "total_call_oi": total_call_oi, "total_put_oi": total_put_oi,
            "top_call_buildup": _top_buildup(call_by_strike),
            "top_put_buildup": _top_buildup(put_by_strike),
            "call_matrix": _build_matrix(call_by_strike),
            "put_matrix": _build_matrix(put_by_strike),
        })
    finally:
        con.close()
