# oiapp/services/live_chain_tracker.py
"""
Live Chain Tracker
────────────────────
The actual live-feed piece: every N minutes (configurable, matching the
GEX Trend Tracker's own interval control), captures a full snapshot of
one options chain's Greeks (delta/gamma/theta/vega/IV) and volume per
strike, PLUS the corresponding futures contract's live price/volume at
that same moment -- so unusual options activity can be checked against
what price actually did afterward, not just observed in isolation.

Built directly on tastytrade_feed.TastytradeFeed.get_live_chain_snapshot(),
which itself is a generalization of the already-proven Greeks-streaming
pattern in realtime_dashboard.py's _fetch_chain_async -- not new,
unverified SDK usage, an extension of code that already works.

Design choice worth stating plainly: this is a snapshot burst on a
schedule (open a streaming session, collect for ~15s, close), not a
permanently-open subscription. A full options chain can be 40-100+
strikes; keeping that many symbols subscribed continuously for hours is
a materially bigger and riskier undertaking (reconnect handling, drift,
memory growth) than this app's other background jobs. Snapshot-on-a-
schedule matches every other background job already in this app
(GEX Trend Tracker, futures OI fetch) and is a safer place to start.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

from ..config import DB_PATH

live_chain_bp = Blueprint("live_chain", __name__, url_prefix="/live-chain")

DEFAULT_INTERVAL_SECONDS = 300  # 5 min

# Which futures contract corresponds to each options underlying this
# tracks -- reuses the same equity<->futures pairing already established
# in realtime_dashboard.py's _FUTURES_POSITIONING_MAP, just the subset
# relevant here (index products, where this kind of flow analysis is
# most meaningful per the option-flow-to-futures-hedging mechanism this
# whole feature is built around).
UNDERLYING_TO_FUTURE = {"SPY": "/ES", "QQQ": "/NQ", "IWM": "/RTY", "DIA": "/YM"}

TRACKED_SYMBOLS = ["SPY", "QQQ", "IWM"]


def _ensure_table():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS live_chain_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        underlying TEXT NOT NULL,
        expiry TEXT NOT NULL,
        strike REAL NOT NULL,
        type TEXT NOT NULL,
        delta REAL, gamma REAL, theta REAL, vega REAL, iv REAL,
        oi REAL, volume REAL,
        future_symbol TEXT, future_price REAL, future_volume REAL,
        captured_at TEXT NOT NULL
    )""")
    # CREATE TABLE IF NOT EXISTS is a no-op on an already-existing table,
    # so a table created before max_print_size/large_print_count existed
    # wouldn't pick up the new columns on its own -- same migration
    # pattern already used elsewhere in this app (dte_pages.py).
    cols = {r[1] for r in con.execute("PRAGMA table_info(live_chain_snapshots)").fetchall()}
    if "max_print_size" not in cols:
        con.execute("ALTER TABLE live_chain_snapshots ADD COLUMN max_print_size REAL DEFAULT 0")
    if "large_print_count" not in cols:
        con.execute("ALTER TABLE live_chain_snapshots ADD COLUMN large_print_count INTEGER DEFAULT 0")
    con.execute("CREATE INDEX IF NOT EXISTS idx_lcs_lookup ON live_chain_snapshots(underlying, expiry, captured_at)")
    con.commit()
    con.close()


def capture_snapshot(underlying: str, expiry: Optional[str] = None,
                      strikes_each_side: Optional[int] = 12) -> Dict[str, Any]:
    """One capture cycle for one symbol: chain Greeks+volume, plus the
    corresponding future's live price/volume at the same moment, all
    stamped with the same captured_at so later analysis can line
    "unusual volume at strike X" up against "what did price do right
    after this snapshot" using nothing but timestamp joins.

    strikes_each_side: passed straight through to
    get_live_chain_snapshot() -- limits both what gets subscribed (fewer
    symbols in the streaming burst) and what gets stored/charted.
    Default 12 each side of spot; None captures the full chain."""
    _ensure_table()
    from .tastytrade_feed import feed

    chain = feed.get_live_chain_snapshot(underlying, expiry=expiry, strikes_each_side=strikes_each_side)
    if not chain.get("ok"):
        return {"ok": False, "error": chain.get("error"), "underlying": underlying}

    future_root = UNDERLYING_TO_FUTURE.get(underlying.upper())
    future_symbol, future_price, future_volume = None, None, None
    if future_root:
        # get_snapshot() needs a SPECIFIC resolved contract (e.g. "/ESU6"),
        # not a bare root ("/ES") -- a root isn't a quotable instrument by
        # itself. Same class of bug already found and fixed in
        # futures_oi_schwab.fetch_futures_oi_tastytrade(): resolve the
        # real front-month contract via the proven get_futures_open_interest()
        # (which already does this resolution correctly via
        # Future.get(product_codes=[...])) before quoting it, instead of
        # guessing/passing the root straight to get_snapshot().
        try:
            from ..scanners.realtime_dashboard import get_futures_open_interest
            resolved = get_futures_open_interest(future_root.lstrip("/"))
            future_symbol = resolved.get("symbol") if not resolved.get("error") else None
        except Exception:
            future_symbol = None
        if future_symbol:
            try:
                snap = feed.get_snapshot(future_symbol)
                if snap.get("status") == "live":
                    future_price = snap.get("last") or snap.get("mark") or snap.get("mid")
                    future_volume = snap.get("volume")
            except Exception:
                pass

    captured_at = datetime.now().isoformat(timespec="seconds")
    con = sqlite3.connect(DB_PATH)
    for r in chain.get("rows", []):
        con.execute("""INSERT INTO live_chain_snapshots
            (underlying, expiry, strike, type, delta, gamma, theta, vega, iv, oi, volume,
             max_print_size, large_print_count, future_symbol, future_price, future_volume, captured_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (underlying.upper(), chain["expiry"], r["strike"], r["type"],
             r.get("delta"), r.get("gamma"), r.get("theta"), r.get("vega"), r.get("iv"),
             r.get("oi"), r.get("volume"), r.get("max_print_size", 0), r.get("large_print_count", 0),
             future_symbol, future_price, future_volume, captured_at))
    con.commit()
    con.close()
    return {"ok": True, "underlying": underlying, "expiry": chain["expiry"],
            "rows_stored": len(chain.get("rows", [])), "future_price": future_price,
            "captured_at": captured_at}


def run_all_ticks():
    for sym in TRACKED_SYMBOLS:
        try:
            result = capture_snapshot(sym)
            if not result.get("ok"):
                print(f"[live_chain_tracker] capture failed for {sym}: {result.get('error')}")
        except Exception as e:
            print(f"[live_chain_tracker] tick failed for {sym}: {e}")


def register_scheduler_job(interval_seconds: int = DEFAULT_INTERVAL_SECONDS):
    from . import unified_scheduler
    from .job_registry import register_job
    # Registering here (not just with unified_scheduler) is what makes
    # this job show up in Scheduler Hub at all -- unified_scheduler.register()
    # handles actually RUNNING the job and already calls job_registry's
    # mark_run() automatically after every execution, but that state was
    # invisible because list_jobs() only shows jobs with metadata
    # registered here. The last-run data already existed; it just had
    # nowhere to surface, which is exactly why this job's actual refresh
    # behavior couldn't be confirmed from Scheduler Hub before now.
    register_job(
        "live_chain_tracker", "Live Chain Tracker", "Live options chain Greeks + volume capture (SPY/QQQ/IWM)",
        kind="interval", default_schedule={"interval_min": max(1, int(interval_seconds / 60))},
        group="Live Capture", run_now_fn=run_all_ticks, editable=True,
    )
    # NOT low_priority: that flag makes the dispatcher skip this job
    # ENTIRELY (not just delay it) for the whole duration of any active
    # Scanner Builder query running anywhere in the app, however long that
    # takes. Fine for genuine bulk backfill jobs that can wait; wrong for
    # a time-sensitive live capture someone is actively watching update
    # through their first-two-hours window -- being silently starved
    # indefinitely defeats the entire point of this job.
    return unified_scheduler.register(
        "live_chain_tracker", run_all_ticks,
        interval_seconds=interval_seconds, low_priority=False,
    )


# ── Analysis ────────────────────────────────────────────────────────────

def _get_snapshots(underlying: str, expiry: Optional[str] = None, limit_captures: int = 20,
                    today_only: bool = False) -> List[Dict[str, Any]]:
    _ensure_table()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    # today_only never deletes anything -- purely a WHERE-clause filter on
    # the read side, so historical captures stay in the DB for future
    # analysis. Was previously missing entirely: "most recent N rows"
    # with no date boundary meant that on a day with few captures so far
    # (e.g. early in the session, or after a gap), the "most recent" set
    # could quietly include yesterday's rows, and any caller treating the
    # earliest timestamp in that set as "today's session start" (the
    # early-institutional-footprint window) or "earlier captures today"
    # (the unusual-volume baseline) was silently comparing across days.
    today_clause = " AND date(captured_at) = date('now', 'localtime')" if today_only else ""
    if expiry:
        rows = con.execute(
            f"SELECT * FROM live_chain_snapshots WHERE underlying=? AND expiry=?{today_clause} "
            "ORDER BY captured_at DESC LIMIT ?",
            (underlying.upper(), expiry, limit_captures * 60)  # generous row cap across captures
        ).fetchall()
    else:
        rows = con.execute(
            f"SELECT * FROM live_chain_snapshots WHERE underlying=?{today_clause} "
            "ORDER BY captured_at DESC LIMIT ?",
            (underlying.upper(), limit_captures * 60)
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def unusual_volume_report(underlying: str, expiry: Optional[str] = None,
                           min_captures_for_baseline: int = 3) -> Dict[str, Any]:
    """Compares each strike's volume in the MOST RECENT capture against
    that same strike's own average volume across earlier captures today
    -- a strike suddenly doing 5x its typical per-snapshot volume is the
    "unusual volume" signal, and it's flagged alongside where price
    (the corresponding future) was at capture time versus where it is
    now, so you can see directly whether price has already reacted."""
    rows = _get_snapshots(underlying, expiry, today_only=True)
    if not rows:
        return {"ok": False, "error": "No snapshots captured yet TODAY for this symbol/expiry (older data exists but is intentionally excluded from this same-day view).", "flagged": []}

    captures = sorted(set(r["captured_at"] for r in rows))
    if len(captures) < min_captures_for_baseline:
        return {"ok": False, "flagged": [],
                "error": f"Only {len(captures)} capture(s) so far -- need {min_captures_for_baseline}+ "
                         f"to establish a per-strike baseline. This accumulates automatically as the "
                         f"background job runs."}

    latest_ts = captures[-1]
    by_strike: Dict[tuple, List[Dict]] = {}
    for r in rows:
        key = (r["strike"], r["type"])
        by_strike.setdefault(key, []).append(r)

    latest_future_price = next((r["future_price"] for r in rows if r["captured_at"] == latest_ts and r.get("future_price")), None)
    earliest_future_price = next((r["future_price"] for r in rows if r["captured_at"] == captures[0] and r.get("future_price")), None)

    flagged = []
    for (strike, typ), snaps in by_strike.items():
        snaps_sorted = sorted(snaps, key=lambda r: r["captured_at"])
        latest = snaps_sorted[-1]
        if latest["captured_at"] != latest_ts:
            continue
        history = snaps_sorted[:-1]
        if len(history) < min_captures_for_baseline - 1:
            continue
        avg_vol = sum((h["volume"] or 0) for h in history) / len(history)
        latest_vol = latest["volume"] or 0
        if avg_vol <= 0 or latest_vol < 20:  # ignore noise-level volume entirely
            continue
        ratio = latest_vol / max(1, avg_vol)
        if ratio < 3.0:  # needs to be at least 3x this strike's own typical snapshot volume
            continue
        price_move_pct = None
        if latest_future_price and earliest_future_price:
            price_move_pct = round((latest_future_price - earliest_future_price) / earliest_future_price * 100, 3)
        flagged.append({
            "strike": strike, "type": typ, "latest_volume": latest_vol, "avg_volume": round(avg_vol, 1),
            "volume_ratio": round(ratio, 1), "gamma": latest.get("gamma"), "vega": latest.get("vega"),
            "iv": latest.get("iv"), "oi": latest.get("oi"),
            "future_price_at_capture": latest.get("future_price"),
            "future_price_move_since_first_capture_pct": price_move_pct,
        })

    flagged.sort(key=lambda f: f["volume_ratio"], reverse=True)
    return {"ok": True, "flagged": flagged[:15], "captures_count": len(captures),
            "latest_captured_at": latest_ts, "underlying": underlying,
            "note": "Ratio compares this strike's volume in the latest capture against its own average "
                    "across earlier captures today -- not a claim about statistical significance, a "
                    "relative-to-itself spike detector."}


BLOCK_SIZE_THRESHOLD = 50  # matches the threshold used at capture time in tastytrade_feed.py --
                            # kept as a separate constant here since this function reads stored data,
                            # not live prints, and a future re-tune of one shouldn't silently desync the other
                            # without it being visible in a diff.


def early_institutional_footprint(underlying: str, expiry: Optional[str] = None,
                                   session_window_minutes: int = 120) -> Dict[str, Any]:
    """Finds strikes where a LARGE SINGLE TRADE PRINT happened during the
    first part of today's session -- not aggregate volume, which 500
    retail 1-lots can produce just as easily as one institutional block.
    This is the actual answer to "where are institutions creating
    positions today": OI (used by GEX Trend Tracker's Peak Gamma Zone)
    is static and aggregate across every participant who's ever held
    that strike, not evidence of TODAY's activity or WHO specifically is
    behind it. A large print in the first ~2 hours (matching Fredy
    Sarmiento's own framing of when institutions actually position) is
    much closer to real evidence of an institutional entry at a specific
    strike -- and that specific strike is the one worth watching for the
    later peak-gamma/profit-taking reversal, not wherever aggregate OI
    happens to sit for reasons that could be weeks old.

    session_window_minutes: how much of the session counts as "early" --
    default 120 (two hours), matching that same framing directly.
    """
    rows = _get_snapshots(underlying, expiry, limit_captures=60, today_only=True)
    if not rows:
        return {"ok": False, "error": "No snapshots captured yet TODAY for this symbol/expiry (older data exists but is intentionally excluded from this same-day view).", "flagged": []}

    captures = sorted(set(r["captured_at"] for r in rows))
    session_start = captures[0]
    try:
        start_dt = datetime.fromisoformat(session_start)
        cutoff_dt = start_dt.timestamp() + session_window_minutes * 60
    except Exception:
        cutoff_dt = None

    early_rows = []
    for r in rows:
        if cutoff_dt is not None:
            try:
                if datetime.fromisoformat(r["captured_at"]).timestamp() > cutoff_dt:
                    continue
            except Exception:
                pass
        early_rows.append(r)

    by_strike: Dict[tuple, Dict[str, Any]] = {}
    for r in early_rows:
        key = (r["strike"], r["type"])
        max_print = r.get("max_print_size") or 0
        if max_print < BLOCK_SIZE_THRESHOLD:
            continue
        existing = by_strike.get(key)
        if existing is None or max_print > existing["max_print_size"]:
            by_strike[key] = {
                "strike": r["strike"], "type": r["type"], "max_print_size": max_print,
                "large_print_count": r.get("large_print_count") or 0,
                "captured_at": r["captured_at"], "future_price_at_capture": r.get("future_price"),
            }

    flagged = sorted(by_strike.values(), key=lambda f: f["max_print_size"], reverse=True)
    return {
        "ok": True, "underlying": underlying, "flagged": flagged[:10],
        "session_start": session_start, "session_window_minutes": session_window_minutes,
        "block_size_threshold": BLOCK_SIZE_THRESHOLD,
        "note": f"Strikes with at least one single trade print of {BLOCK_SIZE_THRESHOLD}+ contracts within "
                f"the first {session_window_minutes} minutes of today's captures -- a block-size print, not "
                f"aggregate volume, since size distribution is what actually distinguishes institutional "
                f"activity from many small retail trades adding up to the same total. Threshold is a rough "
                f"line, adjustable per symbol liquidity -- not itself a claim about who placed the trade.",
    }


def vega_exposure_by_strike(underlying: str, expiry: Optional[str] = None) -> Dict[str, Any]:
    """Latest snapshot's full per-strike picture -- gamma, vega, IV,
    volume, delta side by side. Was only returning gamma/vega/iv/delta;
    volume was already being captured and stored every snapshot but
    never made it into this function's output, so nothing downstream
    could chart it even though the data existed. Backs three views on
    the same data: gamma/vega (dealer hedge pressure vs volatility
    positioning), volume by strike (where today's actual trading
    concentrated), and call-vs-put IV skew (the volatility-surface
    slice Fredy's framework calls the "skew" -- split by side here
    since a single blended IV-by-strike line hides which side is
    actually carrying the skew)."""
    rows = _get_snapshots(underlying, expiry, limit_captures=1)
    if not rows:
        return {"ok": False, "error": "No snapshots captured yet.", "rows": []}
    latest_ts = max(r["captured_at"] for r in rows)
    latest = [r for r in rows if r["captured_at"] == latest_ts]
    out = [{
        "strike": r["strike"], "type": r["type"], "vega": r.get("vega"), "gamma": r.get("gamma"),
        "iv": r.get("iv"), "oi": r.get("oi"), "delta": r.get("delta"), "volume": r.get("volume"),
    } for r in latest]
    out.sort(key=lambda r: r["strike"])
    return {"ok": True, "rows": out, "captured_at": latest_ts, "underlying": underlying,
            "expiry": latest[0]["expiry"] if latest else None}


# ── Synthesis: cross-references GEX Trend Tracker against Live Chain ──────
# The actual "built-in recommendation" layer -- combines two independently-
# reliable sources (GEX Trend Tracker's OI-derived levels, which work even
# without a live tastytrade session; Live Chain Tracker's live volume/vega,
# which need one) and only surfaces a recommendation when a GEX level has
# at least one LIVE confirmation. This is deliberately conservative:
# no confirmation, no recommendation, rather than showing every GEX level
# as if it were equally actionable. Matches Fredy Sarmiento's framework
# directly -- a level only matters once there's evidence someone is
# actually positioned there today, not just that OI theoretically sits there.

STRIKE_MATCH_TOLERANCE_PCT = 0.5  # GEX level and live-chain strike are
                                   # "the same level" if within this % of each other


def synthesize_recommendations(underlying: str, expiry: Optional[str] = None) -> Dict[str, Any]:
    """Cross-references GEX Trend Tracker's current Key Levels against
    Live Chain Tracker's live unusual-volume and vega signals. Returns
    ranked recommendations, but ONLY for levels with at least one live
    confirmation -- a GEX level alone (no live signal nearby) is left out
    entirely rather than shown as if it were confirmed.
    """
    try:
        from .gex_trend_tracker import read_tick
        gex = read_tick(underlying, log=False)
    except Exception as e:
        return {"ok": False, "error": f"GEX Trend Tracker read failed: {e}"}
    if gex.get("error"):
        return {"ok": False, "error": gex["error"]}

    current = gex.get("current") or {}
    levels = current.get("levels") or []
    regime = current.get("regime_label")
    spot = gex.get("spot")
    plan = (current.get("plan") or {}).get("primary") or {}

    vol_report = unusual_volume_report(underlying, expiry)
    vega_data = vega_exposure_by_strike(underlying, expiry)
    flagged = vol_report.get("flagged") or [] if vol_report.get("ok") else []
    vega_rows = vega_data.get("rows") or [] if vega_data.get("ok") else []

    def _nearest(strike_list, target_price, key):
        best = None
        for row in strike_list:
            s = row.get("strike")
            if s is None or not target_price:
                continue
            if abs(s - target_price) / target_price * 100 <= STRIKE_MATCH_TOLERANCE_PCT:
                if best is None or (row.get(key) or 0) > (best.get(key) or 0):
                    best = row
        return best

    recommendations = []
    for lv in levels:
        label, price = lv.get("label"), lv.get("price")
        if not price or not spot or label == "SPOT":
            continue

        vol_match = _nearest(flagged, price, "volume_ratio")
        vega_match = _nearest(vega_rows, price, "vega")
        confirmations = []
        confidence = 0
        if vol_match:
            confirmations.append(f"unusual volume {vol_match['volume_ratio']}x today")
            confidence += 45
        if vega_match and (vega_match.get("vega") or 0) > 0:
            confirmations.append(f"live vega concentration ({vega_match['vega']:.3f})")
            confidence += 30
        if label in ("PUT WALL", "CALL WALL", "GAMMA FLIP", "BALANCE / PIN", "MAX PAIN"):
            confidence += 15  # these are the levels the strategy actually trades, per Fredy's framework

        if not confirmations:
            continue  # no live confirmation -- leave it out, don't recommend on OI alone

        if label == "GAMMA FLIP":
            direction = "regime boundary — above favors mean-reversion (dealers stabilize), below favors trend continuation (dealers amplify)"
        elif price < spot:
            direction = "support — bullish lean if price reaches and holds here"
        else:
            direction = "resistance — bearish lean if price reaches and rejects here"

        recommendations.append({
            "label": label, "price": price, "confidence": min(100, confidence),
            "confirmations": confirmations, "direction": direction,
            "distance_pct": round((price - spot) / spot * 100, 2),
            "text": f"{label} ${price:.2f} — confirmed by {', '.join(confirmations)}. {direction}.",
        })

    # Peak gamma zone (from GEX Trend Tracker's _compute_full_levels) --
    # the specific Fredy Sarmiento mechanic: spot sitting AT a strike
    # with large pre-existing OI means gamma there is at its peak for
    # any position opened at that strike, making it the natural
    # profit-taking point for a long holder and, once they sell back,
    # the dealer's unwind creates reversal pressure. This is a
    # DIFFERENT signal than the wall/volume/vega cross-referencing
    # above (it doesn't need a live confirmation to be meaningful --
    # it's a structural read of where spot sits right now relative to
    # existing positioning), so it's surfaced separately with its own
    # framing rather than folded into the scored list above.
    #
    # But OI alone doesn't tell you WHO established that position or
    # WHEN -- it could be weeks-old, retail, or many participants
    # averaging out. Cross-referencing against early_institutional_footprint()
    # closes that gap: if the SAME strike also shows a large single-print
    # trade from the first part of today's session, that's actual same-day
    # evidence an institution (not aggregate OI of unknown origin) entered
    # here, meaningfully strengthening the case for this specific level.
    peak_zone = current.get("peak_gamma_zone")
    if peak_zone:
        try:
            footprint = early_institutional_footprint(underlying, expiry)
            if footprint.get("ok"):
                for f in footprint.get("flagged", []):
                    if abs(f["strike"] - peak_zone["strike"]) < 0.01 and f["type"] == peak_zone["side"]:
                        peak_zone["early_footprint_confirmed"] = True
                        peak_zone["early_footprint_detail"] = (
                            f"Same-day confirmation: a {f['max_print_size']:.0f}-contract single print hit "
                            f"this exact strike at {f['captured_at'][11:16]}, within the early session window — "
                            f"this isn't just aggregate OI of unknown age, there's direct evidence of a same-day "
                            f"institutional-sized entry here.")
                        peak_zone["note"] = peak_zone["note"] + " " + peak_zone["early_footprint_detail"]
                        break
        except Exception:
            pass  # cross-reference is an enrichment, not a dependency -- peak_zone still shows without it

    # Combined conviction: cross-references the SAME-DAY institutional
    # footprint (which side is seeing real size, and how much) against
    # the gamma profile's structural playbook call. These are genuinely
    # independent signals -- one reads today's actual block-print flow,
    # the other reads the mechanical dealer-hedging shape -- so agreement
    # between them is a real signal, not the same thing counted twice.
    #
    # Translating gex_playbook into a plain bullish/bearish stance needs
    # care: a credit CALL spread is a BEARISH structure (selling calls
    # above spot, betting price stays under them), not a bullish one
    # despite the word "call" -- getting this backwards would silently
    # invert the whole comparison.
    combined_conviction = None
    try:
        gex_playbook = gex.get("gex_playbook")
        footprint_all = early_institutional_footprint(underlying, expiry)
        if gex_playbook and gex_playbook.get("available") and gex_playbook.get("call") != "no_trade" \
                and footprint_all.get("ok") and footprint_all.get("flagged"):
            call_size = sum(f["max_print_size"] for f in footprint_all["flagged"] if f["type"] == "call")
            put_size = sum(f["max_print_size"] for f in footprint_all["flagged"] if f["type"] == "put")
            footprint_stance = "bullish" if call_size > put_size * 1.3 else \
                                "bearish" if put_size > call_size * 1.3 else "neutral"

            pb_call, pb_dir = gex_playbook.get("call"), gex_playbook.get("direction")
            if pb_call == "naked_long":
                playbook_stance = "bullish" if pb_dir == "call" else "bearish"
            elif pb_call == "credit_vertical":
                playbook_stance = "bearish" if pb_dir == "call" else "bullish"  # selling THAT side's spread bets AGAINST it
            else:
                playbook_stance = "neutral"

            if footprint_stance != "neutral" and footprint_stance == playbook_stance:
                combined_conviction = {
                    "agreement": True, "stance": footprint_stance,
                    "text": f"Same-day institutional footprint ({'call' if footprint_stance=='bullish' else 'put'}-side "
                            f"size dominant, {max(call_size, put_size):.0f} vs {min(call_size, put_size):.0f} contracts) "
                            f"agrees with the gamma profile's structural read ({gex_playbook.get('call')} "
                            f"{pb_dir}) -- two independent signals pointing the same way is meaningfully "
                            f"higher conviction than either alone.",
                }
            elif footprint_stance != "neutral" and playbook_stance != "neutral":
                combined_conviction = {
                    "agreement": False, "stance": None,
                    "text": f"Same-day institutional footprint leans {footprint_stance} ({'call' if footprint_stance=='bullish' else 'put'}-side "
                            f"size dominant) but the gamma profile's structural read leans {playbook_stance} "
                            f"({gex_playbook.get('call')} {pb_dir}) -- these disagree. Worth treating as a "
                            f"flag to slow down and look closer, not a reason to default to either one.",
                }
    except Exception:
        pass  # enrichment, not a dependency -- recommendations still return without it

    recommendations.sort(key=lambda r: r["confidence"], reverse=True)
    return {
        "ok": True, "underlying": underlying, "spot": spot, "regime": regime,
        "recommendations": recommendations[:6],
        "peak_gamma_zone": peak_zone,
        "gex_playbook": gex.get("gex_playbook"),
        "combined_conviction": combined_conviction,
        "primary_plan_trigger": plan.get("trigger"), "primary_plan_direction": plan.get("direction"),
        "disclaimer": "Only shows GEX levels with at least one LIVE confirmation (unusual volume or "
                       "vega concentration captured today) — structured cross-referencing of this app's "
                       "own existing signals, not a new predictive model, and no substitute for your own "
                       "risk management. A level with no live confirmation isn't wrong, it just doesn't "
                       "have same-day evidence backing it yet.",
    }




# ── Routes ──────────────────────────────────────────────────────────────

@live_chain_bp.route("/")
def page():
    return render_template("live_chain_tracker.html")


@live_chain_bp.route("/api/capture_now", methods=["POST"])
def api_capture_now():
    symbol = (request.args.get("symbol") or "SPY").upper()
    expiry = request.args.get("expiry") or None
    strikes_param = request.args.get("strikes_each_side", "12")
    strikes_each_side = None if strikes_param.lower() in ("none", "all", "0") else int(strikes_param)
    try:
        return jsonify(capture_snapshot(symbol, expiry, strikes_each_side))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@live_chain_bp.route("/api/unusual_volume")
def api_unusual_volume():
    symbol = (request.args.get("symbol") or "SPY").upper()
    expiry = request.args.get("expiry") or None
    try:
        return jsonify(unusual_volume_report(symbol, expiry))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@live_chain_bp.route("/api/early_footprint")
def api_early_footprint():
    symbol = (request.args.get("symbol") or "SPY").upper()
    expiry = request.args.get("expiry") or None
    window = int(request.args.get("window_minutes", 120))
    try:
        return jsonify(early_institutional_footprint(symbol, expiry, window))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@live_chain_bp.route("/api/vega_exposure")
def api_vega_exposure():
    symbol = (request.args.get("symbol") or "SPY").upper()
    expiry = request.args.get("expiry") or None
    try:
        return jsonify(vega_exposure_by_strike(symbol, expiry))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@live_chain_bp.route("/api/recommendations")
def api_recommendations():
    symbol = (request.args.get("symbol") or "SPY").upper()
    expiry = request.args.get("expiry") or None
    try:
        return jsonify(synthesize_recommendations(symbol, expiry))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@live_chain_bp.route("/api/interval", methods=["GET", "POST"])
def api_interval():
    from .job_registry import get_schedule, set_schedule
    try:
        if request.method == "POST":
            minutes = float((request.json or {}).get("minutes") or 5)
            minutes = max(1.0, min(60.0, minutes))
            set_schedule("live_chain_tracker", {"interval_min": minutes})
            return jsonify({"ok": True, "interval_min": minutes})
        sched = get_schedule("live_chain_tracker")
        return jsonify({"ok": True, "interval_min": sched.get("interval_min", DEFAULT_INTERVAL_SECONDS / 60)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
