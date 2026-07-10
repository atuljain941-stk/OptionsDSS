# oiapp/journal/journal_routes.py
from flask import Blueprint, jsonify, request
from datetime import datetime, date
import json, math, os, threading, time
from functools import lru_cache

from ..scanners.watchlist_manager import log_alert_notification
from ..ai.journal_ai import build_entry_analysis, build_trade_alert_analysis, summarize_portfolio_review
try:
    from .journal_snapshot import (
        capture_entry_snapshot, get_snapshot, get_snapshot_delta,
        preview_entry_score, ensure_snapshot_table
    )
    ensure_snapshot_table()
    _SNAPSHOT_AVAILABLE = True
except Exception as _snap_e:
    print(f"[journal] snapshot module not available: {_snap_e}")
    _SNAPSHOT_AVAILABLE = False

journal_bp = Blueprint("journal", __name__)

def _conn():
    import sqlite3
    from ..db import DB_PATH
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

# ── Helpers ────────────────────────────────────────────────────────────────
def _safe(v, dec=2):
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, dec)
    except: return None

def _as_dict(row):
    if isinstance(row, dict):
        return row
    try:
        import sqlite3 as _sqlite3
        if isinstance(row, _sqlite3.Row):
            return dict(row)
    except Exception:
        pass
    return row

def _json_safe(obj):
    """Return a JSON-safe copy with NaN/Infinity converted to None.

    Flask can otherwise serialize NaN as a bare token. Browsers reject that
    when app.js calls response.json(), which caused errors like
    `Unexpected token N ... "pnr": NaN`. Keep this local to journal routes
    because live position metrics can legitimately produce missing/NaN values.
    """
    try:
        import numpy as _np  # optional
        if isinstance(obj, _np.generic):
            obj = obj.item()
    except Exception:
        pass
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, int) or obj is None or isinstance(obj, bool) or isinstance(obj, str):
        return obj
    try:
        f = float(obj)
        if math.isnan(f) or math.isinf(f):
            return None
    except Exception:
        pass
    return obj

def _jsonify_safe(payload, status=None):
    resp = jsonify(_json_safe(payload))
    if status is not None:
        return resp, status
    return resp


# ── Global Telegram alert settings ─────────────────────────────────────────
_ALERT_FREQUENCY_SECONDS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}

def _app_setting_get(key: str, default=None):
    try:
        con = _conn()
        try:
            con.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            if row and row[0] not in (None, ""):
                return row[0]
        finally:
            con.close()
    except Exception:
        pass
    return default

def _app_setting_set(key: str, value) -> None:
    try:
        con = _conn()
        try:
            con.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, "" if value is None else str(value)),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass

def _normalise_alert_frequency(value=None) -> str:
    raw = str(value or _app_setting_get("alerts_global_frequency", os.getenv("ALERTS_GLOBAL_FREQUENCY", "5m")) or "5m").strip().lower()
    aliases = {"300": "5m", "900": "15m", "3600": "1h", "7200": "2h", "14400": "4h", "86400": "1d", "day": "1d", "daily": "1d"}
    raw = aliases.get(raw, raw)
    return raw if raw in _ALERT_FREQUENCY_SECONDS else "5m"

def _alert_frequency_seconds(default_seconds: int = 300) -> int:
    key = _normalise_alert_frequency()
    return int(_ALERT_FREQUENCY_SECONDS.get(key, default_seconds or 300))

def _health_alert_significant_only() -> bool:
    raw = str(_app_setting_get("health_alert_significant_only", os.getenv("HEALTH_ALERT_SIGNIFICANT_ONLY", "1")) or "1").strip().lower()
    return raw not in {"0", "false", "no", "off", "n"}

def _alert_settings_payload() -> dict:
    freq = _normalise_alert_frequency()
    return {
        "ok": True,
        "frequency": freq,
        "frequency_seconds": int(_ALERT_FREQUENCY_SECONDS.get(freq, 300)),
        "frequency_options": ["5m", "15m", "1h", "2h", "4h", "1d"],
        "health_significant_only": _health_alert_significant_only(),
        "pnr_alerts_enabled": _pnr_alerts_enabled(),
        "ai_alerts_enabled": _ai_alerts_enabled(),
        "custom_position_alerts_enabled": _custom_position_alerts_enabled(),
        "note": "Frequency is global for Telegram/watchlist/scanner/position/health alert watchers. Toggle PNR / AI / custom alerts independently. Manual tests are not throttled.",
    }


def _setting_bool(key: str, default: bool = True) -> bool:
    raw = str(_app_setting_get(key, "1" if default else "0") or ("1" if default else "0")).strip().lower()
    return raw not in {"0", "false", "no", "off", "n", ""}


def _setting_set_bool(key: str, value: bool) -> None:
    _app_setting_set(key, "1" if value else "0")


def _pnr_alerts_enabled() -> bool:
    return _setting_bool("journal_pnr_alerts_enabled", True)


def _ai_alerts_enabled() -> bool:
    return _setting_bool("journal_ai_alerts_enabled", True)


def _custom_position_alerts_enabled() -> bool:
    return _setting_bool("journal_custom_position_alerts_enabled", True)

@journal_bp.route("/journal/alert_global_settings", methods=["GET", "POST"])
def alert_global_settings():
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        freq = _normalise_alert_frequency(d.get("frequency"))
        _app_setting_set("alerts_global_frequency", freq)
        if "health_significant_only" in d:
            _app_setting_set("health_alert_significant_only", "1" if d.get("health_significant_only") else "0")
        if "pnr_alerts_enabled" in d or "pnr_enabled" in d:
            _setting_set_bool("journal_pnr_alerts_enabled", bool(d.get("pnr_alerts_enabled", d.get("pnr_enabled"))))
        if "ai_alerts_enabled" in d or "ai_enabled" in d:
            _setting_set_bool("journal_ai_alerts_enabled", bool(d.get("ai_alerts_enabled", d.get("ai_enabled"))))
        if "custom_position_alerts_enabled" in d or "custom_alerts_enabled" in d:
            _setting_set_bool("journal_custom_position_alerts_enabled", bool(d.get("custom_position_alerts_enabled", d.get("custom_alerts_enabled"))))
    return _jsonify_safe(_alert_settings_payload())

def _json_sanitize(value):
    """Recursively convert NaN/Infinity and non-JSON values to safe JSON."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_sanitize(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        # Keep integer-like numerics compact; otherwise use the float.
        return int(f) if f.is_integer() else f
    except Exception:
        return str(value)


def _jsonify_safe(payload, status_code=None):
    resp = jsonify(_json_sanitize(payload))
    if status_code is not None:
        resp.status_code = int(status_code)
    return resp

def _live_option_price(symbol, expiry, strike, side):
    """Return the current option mid for journal/alert valuation.

    Journal and alerts need current marks, so this path is live-first.  It still
    falls back to the latest local DB option snapshot when the provider chain is
    unavailable or yfinance hits its NaN openInterest/volume parser issue.
    """
    try:
        from ..services.option_prices import fetch_chain
        symbol = str(symbol or "").upper().strip()
        expiry = str(expiry or "").strip()[:10]
        opt_type = "call" if str(side or "").lower() in ("call", "cs", "cb", "c") else "put"
        k = _safe(strike, 4)
        if not symbol or not expiry or k is None:
            return None

        chain = fetch_chain(symbol, expiry, prefer_live=True, use_cache=True)
        actual_expiry = expiry

        # If the requested expiry is not listed live and there is no local chain,
        # snap to nearest live expiry.  This mirrors the older direct-yfinance path.
        if not chain:
            try:
                import yfinance as yf
                from datetime import datetime as _dt
                tk = yf.Ticker(symbol)
                opts = list(tk.options or [])
                if opts:
                    target = _dt.strptime(expiry, "%Y-%m-%d")
                    nearest = min(opts, key=lambda e: abs((_dt.strptime(e, "%Y-%m-%d") - target).days))
                    if nearest and nearest != expiry:
                        chain = fetch_chain(symbol, nearest, prefer_live=True, use_cache=True)
                        actual_expiry = nearest
            except Exception:
                pass

        if not chain:
            return None

        def _lookup(strike_val):
            try:
                sv = round(float(strike_val), 4)
            except Exception:
                return None
            return (chain.get((opt_type, sv))
                    or chain.get((opt_type, round(sv, 2)))
                    or chain.get((opt_type, round(sv)))
                    or chain.get((opt_type, round(sv * 2) / 2)))

        row = _lookup(k)
        if not row:
            # Last attempt: nearest strike within 0.51.
            best = None
            best_dist = 999999.0
            for (typ, st), data in chain.items():
                if typ != opt_type:
                    continue
                try:
                    dist = abs(float(st) - float(k))
                except Exception:
                    continue
                if dist < best_dist:
                    best = data
                    best_dist = dist
            row = best if best is not None and best_dist <= 0.51 else None
        if not row:
            return None

        mid = _safe(row.get("mid"))
        if mid is not None:
            return mid
        bid = _safe(row.get("bid")); ask = _safe(row.get("ask"))
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2)
        last = _safe(row.get("last")) or _safe(row.get("price"))
        return last
    except Exception:
        return None

def _live_spot(symbol):
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="1d")
        return round(float(df["Close"].iloc[-1]), 2) if not df.empty else None
    except: return None

def _live_atr(symbol):
    """Fetch ATR-14 for PNR calculation."""
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="3mo")
        if df.empty or len(df) < 15: return None
        H=df["High"].tolist(); L=df["Low"].tolist(); C=df["Close"].tolist()
        tr = [max(H[i]-L[i], abs(H[i]-C[i-1]), abs(L[i]-C[i-1])) for i in range(1,len(C))]
        # Wilder EMA
        atr = sum(tr[:14])/14
        for i in range(14, len(tr)): atr = (atr*13 + tr[i])/14
        return round(atr, 2)
    except: return None


def _is_open_option_trade(t):
    tt = (t.get("trade_type") or "").upper()
    status = (t.get("status") or "").upper()
    return status == "OPEN" and tt in {"PS", "CS", "PB", "CB", "IC"}


def _pnr_breach_summary(t, spot, pnr, pnr_upper):
    tt = (t.get("trade_type") or "").upper()
    breached = False
    status = "SAFE"
    side = None
    if spot is None:
        return False, status, side
    if tt in ("PS", "PB") and pnr:
        breached = spot < pnr
        side = "put"
        status = "⚠ LOWER PNR BREACHED" if breached else f"Lower PNR ${pnr} safe"
    elif tt in ("CS", "CB") and pnr_upper:
        breached = spot > pnr_upper
        side = "call"
        status = "⚠ UPPER PNR BREACHED" if breached else f"Upper PNR ${pnr_upper} safe"
    elif tt == "IC":
        put_breached = pnr is not None and spot < pnr
        call_breached = pnr_upper is not None and spot > pnr_upper
        if put_breached and call_breached:
            breached = True
            side = "both"
            status = "⚠ BOTH PNR BREACHED"
        elif put_breached:
            breached = True
            side = "put"
            status = f"⚠ PUT PNR ${pnr} BREACHED"
        elif call_breached:
            breached = True
            side = "call"
            status = f"⚠ CALL PNR ${pnr_upper} BREACHED"
        else:
            status = f"Puts safe ${pnr} / Calls safe ${pnr_upper}"
    return breached, status, side


def _maybe_log_trade_pnr_alert(t, live):
    try:
        t = _as_dict(t)
        if not _is_open_option_trade(t) or not _pnr_alerts_enabled():
            return
        breached = bool(live.get("pnr_breached"))
        last_breached = int(t.get("pnr_alert_last_breached") or 0)
        if breached and not last_breached:
            log_alert_notification(
                "PNR_BREACH",
                f"{t.get('symbol','').upper()} PNR breach",
                live.get("pnr_status") or "PNR breached",
                symbol=(t.get("symbol") or "").upper(),
                trade_id=t.get("id"),
                severity="warn",
                source="journal",
                metadata={"trade_id": t.get("id"), "symbol": t.get("symbol"), "pnr": live.get("pnr"), "pnr_upper": live.get("pnr_upper"), "spot": live.get("spot"), "trade_type": t.get("trade_type")},
            )
            _send_trade_pnr_telegram(t, live)
        con = _conn()
        try:
            con.execute(
                "UPDATE trades SET pnr_alert_last_breached=?, pnr_alert_last_sent_at=? WHERE id=?",
                (1 if breached else 0, datetime.now().isoformat(timespec='seconds') if breached else t.get("pnr_alert_last_sent_at"), t.get("id")),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _send_trade_pnr_telegram(t, live, *, prefix='📣 PNR alert'):
    """Send a Telegram alert for a trade PNR breach or test."""
    try:
        t = _as_dict(t)
        from ..services.telegram_alerts import telegram_configured, send_telegram_message
    except Exception as e:
        return {'configured': False, 'sent': 0, 'error': f'telegram service unavailable: {e}'}

    if not telegram_configured():
        return {'configured': False, 'sent': 0, 'error': 'Telegram credentials not configured'}

    spot = live.get('spot')
    pnr = live.get('pnr')
    pnr_upper = live.get('pnr_upper')
    status = live.get('pnr_status') or 'PNR breached'
    parts = [
        prefix,
        f"Trade: #{t.get('id')} {str(t.get('symbol') or '').upper()}",
        f"Type: {t.get('trade_type') or '—'}",
        f"Spot: {float(spot):.2f}" if spot is not None else 'Spot: —',
        f"PNR: {float(pnr):.2f}" if pnr is not None else 'PNR: —',
    ]
    if pnr_upper is not None:
        try:
            parts.append(f"PNR Upper: {float(pnr_upper):.2f}")
        except Exception:
            parts.append(f"PNR Upper: {pnr_upper}")
    parts.append(f"Status: {status}")
    try:
        result = send_telegram_message('\n'.join(parts))
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    return {'configured': True, 'sent': 1 if result.get('ok') else 0, 'error': None if result.get('ok') else (result.get('error') or result.get('description') or 'Telegram send failed')}


_trade_alert_watcher_started = False
_trade_alert_watcher_lock = threading.Lock()


def _global_telegram_alert_sleep_seconds(default_seconds: int) -> int:
    try:
        from ..scanners.watchlist_manager import get_global_alert_interval_seconds
        return int(get_global_alert_interval_seconds(default_seconds))
    except Exception:
        return int(default_seconds or 300)


def _scan_trade_pnr_alerts_once():
    global _trade_alert_last_run_at, _trade_alert_last_result
    if not _pnr_alerts_enabled():
        _trade_alert_last_run_at = datetime.now().isoformat(timespec='seconds')
        _trade_alert_last_result = {'ok': True, 'enabled': False, 'triggered': 0, 'alerts': []}
        return
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
    finally:
        con.close()
    alerts = []
    for row in rows:
        try:
            live = _compute_live_pnl(row)
            before = bool(live.get('pnr_breached'))
            _maybe_log_trade_pnr_alert(row, live)
            if before:
                alerts.append({'id': row['id'], 'symbol': row['symbol'], 'pnr': live.get('pnr'), 'pnr_upper': live.get('pnr_upper'), 'spot': live.get('spot')})
        except Exception:
            continue
    _trade_alert_last_run_at = datetime.now().isoformat(timespec='seconds')
    _trade_alert_last_result = {'ok': True, 'triggered': len(alerts), 'alerts': alerts}


def start_trade_alert_watcher(interval_seconds=60):
    global _trade_alert_watcher_started
    with _trade_alert_watcher_lock:
        if _trade_alert_watcher_started:
            return False
        _trade_alert_watcher_started = True

    def _loop():
        from ..services.job_registry import register_job, is_enabled, mark_run
        register_job(
            "trade_pnr_alerts", "Trade P&L/risk alerts", "Scans open journal positions for P&L / risk-level alerts.",
            kind="interval", default_schedule={"interval_min": max(1, int((interval_seconds or 60) / 60))},
            group="Alert Watchers", run_now_fn=_scan_trade_pnr_alerts_once,
        )
        while True:
            if is_enabled("trade_pnr_alerts"):
                try:
                    _scan_trade_pnr_alerts_once()
                    mark_run("trade_pnr_alerts", True, "")
                except Exception as e:
                    mark_run("trade_pnr_alerts", False, str(e))
            time.sleep(max(60, _global_telegram_alert_sleep_seconds(int(interval_seconds or 60))))

    t = threading.Thread(target=_loop, name="trade-alert-watcher", daemon=True)
    t.start()
    return True

def _trade_width(t):
    """Correct width for any trade type including IC."""
    tt = t.get("trade_type","")
    if tt == "IC":
        # IC width = max of put spread width or call spread width
        ps = _safe(t.get("put_sell")); pb = _safe(t.get("put_buy"))
        cs = _safe(t.get("call_sell")); cb = _safe(t.get("call_buy"))
        pw = abs(ps-pb) if ps and pb else 0
        cw = abs(cb-cs) if cs and cb else 0
        return max(pw, cw)
    else:
        ls = _safe(t.get("long_strike")); ss = _safe(t.get("short_strike"))
        return abs(ss-ls) if ss and ls else 0

def _trade_max_risk(t):
    """Max dollar risk.

    Stock  -> entry_price × shares.
    Credit  -> (width - credit_per_contract) × qty × 100.
    Debit   -> debit_per_contract × qty × 100.

    net_premium is TOTAL for all contracts; per-contract = net/qty.
    """
    ep   = _safe(t.get("entry_price")) or 0
    net  = _trade_net_premium(t)
    qty  = _trade_quantity(t)
    tt   = t.get("trade_type","")
    if tt == "Stock":
        return round(ep * qty, 2)

    w = _trade_width(t)
    prem_per = abs(net / max(qty, 1) if (net is not None and qty) else (net if net is not None else ep))
    if w and w > 0:
        if net is not None and net >= 0:
            # Credit: max risk = (width - credit_per_contract) × qty × 100
            return round(max(0, (w - prem_per)) * qty * 100, 2)
        # Debit: max risk = debit_per_contract × qty × 100
        return round(prem_per * qty * 100, 2)
    return round(prem_per * qty * 100, 2)


def _trade_max_reward(t):
    """Max dollar reward.

    Stock  -> unlimited (0 stored for display).
    Credit  -> credit_per_contract × qty × 100.
    Debit   -> (width - debit_per_contract) × qty × 100.

    Key: net_premium is stored as the TOTAL for all qty contracts.
    Per-contract = net / qty.  Dollar reward = per_contract * qty * 100 = net * 100.
    """
    ep   = _safe(t.get("entry_price")) or 0
    net  = _trade_net_premium(t)
    qty  = _trade_quantity(t)
    tt   = t.get("trade_type","")
    if tt == "Stock":
        return 0  # shown as "unlimited"

    w = _trade_width(t)
    # per-contract point value
    prem_per = abs(net / max(qty, 1) if (net is not None and qty) else (net if net is not None else ep))
    if w and w > 0:
        if net is not None and net >= 0:
            # Credit: max reward = premium per contract × qty × 100
            return round(prem_per * qty * 100, 2)
        # Debit: max reward = (width - debit_per_contract) × qty × 100
        return round(max(0, (w - prem_per)) * qty * 100, 2)
    return round(prem_per * qty * 100, 2)

def _option_qty_from_legs(legs, fallback=1):
    try:
        qtys = [max(1, int(l.get("qty", 1) or 1)) for l in (legs or []) if l.get("option_type") != "stock"]
        if qtys:
            from math import gcd
            qty = qtys[0]
            for q in qtys[1:]:
                qty = gcd(qty, q)
            return max(1, qty)
    except Exception:
        pass
    return int(fallback or 1)


def _row_value(row, key, default=None):
    try:
        if isinstance(row, dict):
            return row.get(key, default)
        if row is not None and hasattr(row, 'keys') and key in row.keys():
            return row[key]
    except Exception:
        pass
    try:
        return row[key]
    except Exception:
        return default

def _trade_quantity(t):
    """Resolve the effective trade size.

    For option trades with legs_json, trust the leg structure rather than the
    legacy quantity column, which has historically drifted out of sync.
    For stock trades, use the stock leg qty when present.
    """
    import json as _json
    qty = int(_row_value(t, "quantity", 1) or 1)
    tt = _row_value(t, "trade_type", "")
    try:
        legs = _json.loads(_row_value(t, "legs_json", "") or "[]")
    except Exception:
        legs = []
    if not legs:
        return max(1, qty)
    if tt == "Stock":
        try:
            sl = next((l for l in legs if l.get("option_type") == "stock"), None)
            if sl:
                return max(1, int(sl.get("qty", qty) or qty))
        except Exception:
            pass
        return max(1, qty)
    inferred = _option_qty_from_legs(legs, qty)
    return max(1, inferred)


def _normalize_option_points(v):
    """Normalize an option premium value to option points.

    Some older journal edit flows accidentally stored option premium as dollars
    (for example 250 instead of 2.50).  P&L math should use points first and
    multiply by 100 exactly once.
    """
    try:
        x = float(v or 0)
    except Exception:
        return 0.0
    return x / 100.0 if abs(x) > 50 else x


def _entry_net_points_from_legs(legs, qty=1):
    """Return entry sell-minus-buy net in option points per spread unit."""
    total = 0.0
    any_option = False
    for l in legs or []:
        try:
            if str(l.get("option_type", "")).lower() == "stock":
                continue
            px = float(l.get("price") if l.get("price") is not None else l.get("entry_price") or 0)
            q = int(l.get("qty") or 1)
            sign = 1 if str(l.get("side", "")).lower() == "sell" else -1
            total += sign * px * q
            any_option = True
        except Exception:
            continue
    if not any_option:
        return None
    return total / max(1, int(qty or 1))



def _apply_net_premium_to_option_legs(legs, net_points):
    """Return a copy of option legs whose prices encode a requested net.

    This is used when the user enters only the combined roll order cashflow
    instead of explicit new-leg prices.  The synthetic prices keep the journal
    math internally consistent without pretending we know the true leg fills.
    """
    out = [dict(l or {}) for l in (legs or [])]
    try:
        net = float(net_points or 0)
    except Exception:
        net = 0.0
    opt_indices = [i for i, l in enumerate(out) if str(l.get("option_type", "")).lower() in ("call", "put")]
    if not opt_indices:
        return out
    for i in opt_indices:
        out[i]["price"] = 0.0
    sell_i = next((i for i in opt_indices if str(out[i].get("side", "")).lower() == "sell"), opt_indices[0])
    buy_i = next((i for i in opt_indices if str(out[i].get("side", "")).lower() == "buy"), opt_indices[-1])
    if net >= 0:
        out[sell_i]["price"] = round(abs(net), 4)
    else:
        out[buy_i]["price"] = round(abs(net), 4)
    return out

def _trade_net_premium(t):
    """Return entry net premium in option points, preferring legs as truth."""
    import json as _json
    try:
        legs = _json.loads(_row_value(t, "legs_json", "") or "[]")
    except Exception:
        legs = []
    if legs:
        leg_net = _entry_net_points_from_legs(legs, _trade_quantity(t))
        if leg_net is not None:
            return leg_net * max(1, _trade_quantity(t))

    net = _safe(_row_value(t, "net_premium"))
    if net is not None:
        tt = str(_row_value(t, "trade_type", ""))
        if tt != "Stock":
            return _normalize_option_points(net)
        return net
    ep = _safe(_row_value(t, "entry_price"))
    if ep is not None:
        tt = str(_row_value(t, "trade_type", ""))
        return ep if tt == "Stock" else _normalize_option_points(ep)
    return 0

# ── PNR Calculation (from PNR Method PDF) ──────────────────────────────────


def _resolve_trade_strikes(t):
    """Resolve buy/sell strikes from legacy columns and legs_json.

    Returns a dict with generic buy/sell strikes plus side-specific leg strikes
    so outlook logic can work even when one or both legacy strike columns are
    blank or stale.
    """
    import json as _json
    tt = (_row_value(t, "trade_type", "") or "").upper()
    try:
        legs = _json.loads(_row_value(t, "legs_json", "") or "[]")
    except Exception:
        legs = []

    put_buy = put_sell = call_buy = call_sell = None
    for leg in legs or []:
        try:
            strike = _safe(leg.get("strike") or leg.get("k") or leg.get("strike_price"))
            if strike is None:
                continue
            side = str(leg.get("side") or leg.get("action") or leg.get("position") or "").strip().lower()
            if side in ("b", "bot", "bto", "buy to open", "long"):
                side = "buy"
            elif side in ("s", "sold", "sto", "sell to open", "short"):
                side = "sell"
            opt = str(leg.get("option_type") or leg.get("type") or leg.get("right") or leg.get("put_call") or leg.get("putCall") or "").strip().lower()
            if opt in ("p", "puts"):
                opt = "put"
            elif opt in ("c", "calls"):
                opt = "call"
            if opt == "put":
                if side == "buy" and put_buy is None:
                    put_buy = strike
                elif side == "sell" and put_sell is None:
                    put_sell = strike
            elif opt == "call":
                if side == "buy" and call_buy is None:
                    call_buy = strike
                elif side == "sell" and call_sell is None:
                    call_sell = strike
        except Exception:
            continue

    legacy_ls = _safe(_row_value(t, "long_strike"))
    legacy_ss = _safe(_row_value(t, "short_strike"))

    # If the row was created without reliable strike columns, infer from legs.
    if tt in ("PS", "PB"):
        buy = put_buy or legacy_ls
        sell = put_sell or legacy_ss
        return {"buy": buy, "sell": sell, "put_buy": put_buy, "put_sell": put_sell,
                "call_buy": None, "call_sell": None}
    if tt in ("CS", "CB"):
        buy = call_buy or legacy_ls
        sell = call_sell or legacy_ss
        return {"buy": buy, "sell": sell, "put_buy": None, "put_sell": None,
                "call_buy": call_buy, "call_sell": call_sell}
    if tt == "IC":
        return {"buy": None, "sell": None,
                "put_buy": put_buy or _safe(_row_value(t, "put_buy")) or legacy_ls,
                "put_sell": put_sell or _safe(_row_value(t, "put_sell")) or legacy_ss,
                "call_buy": call_buy or _safe(_row_value(t, "call_buy")),
                "call_sell": call_sell or _safe(_row_value(t, "call_sell"))}
    return {"buy": legacy_ls, "sell": legacy_ss, "put_buy": put_buy, "put_sell": put_sell,
            "call_buy": call_buy, "call_sell": call_sell}
def _compute_pnr(long_strike, dte, atr):
    """
    PNR = long_strike - (long_strike * dte * atr) / 2000
    From The PNR Method paper.
    """
    if not (long_strike and dte and atr): return None
    return round(long_strike - (long_strike * dte * atr) / 2000, 2)

# ── Outlook logic (from spreadsheet formula) ───────────────────────────────
def _compute_outlook(t, spot, dte, rolling_days=5):
    """Return the journal outlook using the user's spreadsheet rules.

    Open trades are classified by price location relative to the spread strikes.
    Closed trades fall back to exit-price vs entry-price directionality.
    The function tolerates stale/blank strike columns by inferring strikes from
    legs_json whenever possible.
    """
    if spot is None:
        return "UNKNOWN"

    status = (_row_value(t, "status", "OPEN") or "OPEN").upper()
    tt     = (_row_value(t, "trade_type", "") or "").upper()
    ep     = _safe(_row_value(t, "entry_price")) or 0
    exit_px= _safe(_row_value(t, "exit_price")) or 0
    s      = _resolve_trade_strikes(t)
    buy    = _safe(s.get("buy"))
    sell   = _safe(s.get("sell"))
    put_buy   = _safe(s.get("put_buy"))
    put_sell  = _safe(s.get("put_sell"))
    call_buy  = _safe(s.get("call_buy"))
    call_sell = _safe(s.get("call_sell"))

    # Closed rows: use realised directionality.
    if status == "CLOSED":
        if tt in ("PS", "CS", "IC"):
            return "✓ CLOSED/WINNER" if exit_px <= ep else "✓ CLOSED AT LOSS"
        return "✓ CLOSED/WINNER" if exit_px >= ep else "✓ CLOSED AT LOSS"

    # Iron condor: winner only when spot remains between the short strikes.
    if tt == "IC":
        if put_sell is None or call_sell is None:
            return "UNKNOWN"
        if spot > put_sell and spot < call_sell:
            return "PROJECTED WINNER"
        if put_buy is not None and spot < put_buy:
            return "PROJECTED LOSER (put breach)" if dte <= rolling_days else "PUT ITM"
        if call_buy is not None and spot > call_buy:
            return "PROJECTED LOSER (call breach)" if dte <= rolling_days else "CALL ITM"
        if put_sell is not None and put_buy is not None and put_buy < spot <= put_sell:
            return "PUT ITM"
        if call_sell is not None and call_buy is not None and call_sell <= spot < call_buy:
            return "CALL ITM"
        return "NEUTRAL"

    # Generic strike order.
    # buy_strike and sell_strike are inferred from the legs, not assumed.
    if buy is None and sell is None:
        return "UNKNOWN"

    lo = min(buy, sell) if (buy is not None and sell is not None) else (buy if buy is not None else sell)
    hi = max(buy, sell) if (buy is not None and sell is not None) else (buy if buy is not None else sell)

    if tt == "PS":
        if sell is not None and spot > hi:
            return "PROJECTED WINNER"
        if buy is not None and spot < lo:
            return "PROJECTED LOSER" if dte <= rolling_days else "OTM"
        if lo < spot <= hi:
            return "ITM"
        return "NEUTRAL"

    if tt == "CS":
        if sell is not None and spot < lo:
            return "PROJECTED WINNER"
        if buy is not None and spot > hi:
            return "PROJECTED LOSER"
        if lo <= spot < hi:
            return "ITM"
        return "NEUTRAL"

    if tt == "PB":
        if buy is not None and spot < hi:
            return "PROJECTED WINNER"
        if sell is not None and spot > lo:
            return "PROJECTED LOSER"
        if lo < spot < hi:
            return "ITM"
        return "NEUTRAL"

    if tt == "CB":
        if buy is not None and spot > lo:
            return "PROJECTED WINNER"
        if sell is not None and spot < hi:
            return "PROJECTED LOSER"
        if lo < spot < hi:
            return "ITM"
        return "NEUTRAL"

    return "UNKNOWN"

    if tt == "PS":
        if not ss or not ls: return "UNKNOWN"
        if spot > ss:              return "PROJECTED WINNER"
        if spot < ls and dte <= rolling_days: return "PROJECTED LOSER"
        if spot < ls and dte > rolling_days:  return "OTM"
        if ls < spot <= ss:        return "ITM"

    elif tt == "CS":
        if not ss or not ls: return "UNKNOWN"
        if spot < ss:              return "PROJECTED WINNER"
        if spot > ls:              return "PROJECTED LOSER"
        if ss <= spot < ls:        return "ITM"

    elif tt == "PB":
        if not ss or not ls: return "UNKNOWN"
        if spot < ls:              return "PROJECTED WINNER"
        if spot > ss:              return "PROJECTED LOSER"
        if ls <= spot <= ss:       return "ITM"

    elif tt == "CB":
        if not ss or not ls: return "UNKNOWN"
        if spot > ss:              return "PROJECTED WINNER"
        if spot < ls:              return "PROJECTED LOSER"
        if ls <= spot <= ss:       return "ITM"

    return "NEUTRAL"


def _fetch_regime_signals(symbol):
    """
    Fetch RS regime, IV rank, and OI shift signals from the database.
    Returns dict with regime bias/confidence, iv_rank, and oi_signal.
    Used by Trade Health Score to incorporate market context.
    """
    result = {
        "symbol_bias": None, "symbol_confidence": 50, "symbol_regime": None,
        "iv_rank": None, "iv_rank_note": None,
        "oi_signal": None, "oi_score": 0, "oi_note": None,
        "rs_score_delta": 0, "iv_delta": 0, "oi_delta": 0,
    }
    if not symbol:
        return result
    try:
        import sqlite3 as _rsq
        from pathlib import Path as _rp
        _db = str(_rp(__file__).resolve().parents[2] / "options_data.db")
        _con = _rsq.connect(_db)

        # RS / Regime signal for the specific symbol
        row = _con.execute(
            "SELECT bias, confidence, regime, iv_rank, rsi_diff, signals_json "
            "FROM regime_scan WHERE symbol=? ORDER BY scan_date DESC LIMIT 1",
            (symbol.upper(),)
        ).fetchone()
        if row:
            result["symbol_bias"]       = (row[0] or "").lower()
            result["symbol_confidence"] = float(row[1] or 50) / 100
            result["symbol_regime"]     = row[2]
            result["iv_rank"]           = float(row[3]) if row[3] is not None else None
            result["rsi_diff"]          = float(row[4]) if row[4] is not None else None

            conf = result["symbol_confidence"]
            bias = result["symbol_bias"]
            if "bull" in bias:   result["rs_score_delta"] = int(8 * conf)
            elif "bear" in bias: result["rs_score_delta"] = -int(8 * conf)

            iv = result["iv_rank"]
            if iv is not None:
                if iv > 70:   result["iv_rank_note"] = f"IV Rank {iv:.0f}% — elevated, premium rich"
                elif iv > 50: result["iv_rank_note"] = f"IV Rank {iv:.0f}% — good for credit spreads"
                elif iv > 30: result["iv_rank_note"] = f"IV Rank {iv:.0f}% — moderate IV"
                else:         result["iv_rank_note"] = f"IV Rank {iv:.0f}% — low IV, thin premium"

        # OI Shift signal
        try:
            from ..services.futures_oi import analyze_oi_buildup
            oi_data = analyze_oi_buildup(symbol)
            sig = oi_data.get("signal", "NO_DATA")
            result["oi_signal"] = sig
            result["oi_score"]  = int(oi_data.get("score", 0))
            if sig == "LONG_BUILDUP":
                result["oi_note"]  = "OI: Long buildup — new longs entering ↑"
                result["oi_delta"] = 6
            elif sig == "SHORT_BUILDUP":
                result["oi_note"]  = "OI: Short buildup — new shorts entering ↓"
                result["oi_delta"] = -6
            elif sig == "LONG_UNWINDING":
                result["oi_note"]  = "OI: Long unwinding — longs exiting"
                result["oi_delta"] = -3
            elif sig == "SHORT_COVERING":
                result["oi_note"]  = "OI: Short covering — mild bullish squeeze"
                result["oi_delta"] = 3
        except Exception:
            pass

        _con.close()
    except Exception:
        pass
    return result


def _trade_probability_score(t, spot, dte, atr, pnr, pnr_upper,
                              pnr_breached, outlook, pct_of_max, unrealised_pnl, max_loss):
    """
    Compute Trade Health Score 0-100 incorporating:
      - Strike/outlook positioning (base)
      - DTE / theta decay
      - P&L momentum
      - PNR proximity / breach
      - ATR volatility
      - RS Market Regime (symbol-level bias + confidence)
      - IV Rank (supports or undermines trade type)
      - OI Shift (futures OI buildup/unwinding)
    Returns health score, 5-action recommendation (HOLD/ADD/TRIM/HEDGE/EXIT), and signal notes.
    """
    tt     = t.get("trade_type","")
    symbol = t.get("symbol","")

    # Base health from outlook
    ol = (outlook or "").upper()
    base = 65
    if "PROJECTED WINNER" in ol:       base = 78
    elif "CALL ITM" in ol:             base = 55
    elif "PUT ITM" in ol:              base = 45
    elif "ITM" in ol:                  base = 50
    elif "OTM" in ol:                  base = 60
    elif "PROJECTED LOSER" in ol:      base = 25
    elif "LOSER (put breach)" in ol:   base = 20
    elif "LOSER (call breach)" in ol:  base = 20

    score = base
    score_notes = []

    # DTE / Theta
    if dte > 21:     score -= 3;  score_notes.append("Long DTE: theta working slowly")
    elif dte > 14:   score += 3;  score_notes.append("Good DTE window: theta accelerating")
    elif dte > 7:    score += 5;  score_notes.append("Sweet spot: theta in overdrive")
    elif dte > 2:    score += 2;  score_notes.append("\u26a0 <7 DTE: gamma risk rising")
    else:            score -= 10; score_notes.append("\U0001f6a8 Expiry imminent: gamma critical")

    # P&L Momentum
    if pct_of_max is not None:
        if pct_of_max >= 60:   score += 8; score_notes.append(f"Strong: {pct_of_max:.0f}% profit captured")
        elif pct_of_max >= 40: score += 5; score_notes.append(f"Healthy: {pct_of_max:.0f}% profit")
        elif pct_of_max >= 10: score += 2
        elif pct_of_max < -50: score -= 15; score_notes.append("Deep loss: stop-out zone")
        elif pct_of_max < 0:   score -= 8;  score_notes.append(f"Loss: {pct_of_max:.0f}% of max")

    # PNR Proximity
    if pnr_breached:
        score -= 20; score_notes.append("\u26a0 PNR breached: recovery probability low")
    elif pnr and spot:
        if tt in ("PS","PB"):
            pnr_dist = (spot - pnr) / spot * 100
            if pnr_dist < 2:   score -= 5;  score_notes.append("Near lower PNR boundary")
            elif pnr_dist > 5: score += 5;  score_notes.append("Safe distance above PNR")
        elif tt in ("CS","CB") and pnr_upper:
            pnr_dist = (pnr_upper - spot) / spot * 100
            if pnr_dist < 2:   score -= 5;  score_notes.append("Near upper PNR boundary")
            elif pnr_dist > 5: score += 5;  score_notes.append("Safe distance below PNR")

    # ATR Volatility
    if atr and spot:
        atr_pct = atr / spot * 100
        if atr_pct > 3:   score -= 3; score_notes.append(f"High ATR ({atr_pct:.1f}%): volatile")
        elif atr_pct < 1: score += 3; score_notes.append(f"Low ATR ({atr_pct:.1f}%): stable")

    # RS / Regime + IV Rank + OI Shift (market context layer)
    reg = _fetch_regime_signals(symbol)
    rs_delta  = reg["rs_score_delta"]
    iv_delta  = 0
    oi_delta  = reg["oi_delta"]
    is_credit = tt in ("PS","CS","IC")
    is_bull   = tt in ("PS","PB","CB")

    # IV Rank modifier
    iv = reg.get("iv_rank")
    if iv is not None:
        if is_credit:
            if iv > 60:   iv_delta = 6;  score_notes.append(f"IVR {iv:.0f}%: premium rich \u2713")
            elif iv < 25: iv_delta = -6; score_notes.append(f"IVR {iv:.0f}%: thin premium \u26a0")
        else:
            if iv < 30:   iv_delta = 6;  score_notes.append(f"IVR {iv:.0f}%: options cheap \u2713")
            elif iv > 65: iv_delta = -6; score_notes.append(f"IVR {iv:.0f}%: options expensive \u26a0")

    # RS regime alignment
    bias = reg.get("symbol_bias","")
    if rs_delta != 0:
        if is_bull and "bull" in bias:
            score_notes.append(f"RS regime: {reg['symbol_regime']} \u2014 aligned \u2713")
        elif is_bull and "bear" in bias:
            score_notes.append(f"RS regime: {reg['symbol_regime']} \u2014 headwind \u26a0")
            rs_delta = -abs(rs_delta)
        elif not is_bull and "bear" in bias:
            score_notes.append(f"RS regime: {reg['symbol_regime']} \u2014 aligned \u2713")
        elif not is_bull and "bull" in bias:
            score_notes.append(f"RS regime: {reg['symbol_regime']} \u2014 headwind \u26a0")
            rs_delta = -abs(rs_delta)

    # OI shift
    oi_sig = reg.get("oi_signal","")
    if oi_delta != 0 and reg.get("oi_note"):
        score_notes.append(reg["oi_note"])

    score = score + rs_delta + iv_delta + oi_delta
    score = max(5, min(97, round(score)))

    # Signal breakdown for UI
    signal_factors = []
    if reg.get("symbol_regime"):
        signal_factors.append({
            "label": "RS Regime",
            "value": f"{reg['symbol_regime']} ({bias})",
            "delta": rs_delta,
            "note":  reg.get("iv_rank_note") or ""
        })
    if iv is not None:
        signal_factors.append({
            "label": "IV Rank",
            "value": f"{iv:.0f}%",
            "delta": iv_delta,
            "note":  reg.get("iv_rank_note","")
        })
    if oi_sig and oi_sig != "NO_DATA":
        signal_factors.append({
            "label": "OI Shift",
            "value": oi_sig.replace("_"," "),
            "delta": oi_delta,
            "note":  reg.get("oi_note","")
        })

    # 5-Action Recommendation: HOLD / ADD / TRIM / HEDGE / EXIT
    trade_action = "HOLD"
    action_color = "#22c55e"
    action_reason = ""

    if pnr_breached and dte <= 3:
        trade_action = "EXIT"; action_color = "#ef4444"
        action_reason = "PNR breached with < 3 DTE. No recovery window. Exit now."
    elif pct_of_max is not None and pct_of_max >= 75:
        trade_action = "TRIM"; action_color = "#a78bfa"
        action_reason = f"\u226575% of max profit ({pct_of_max:.0f}%). Lock in gains."
    elif pct_of_max is not None and pct_of_max >= 50:
        trade_action = "TRIM"; action_color = "#c084fc"
        action_reason = f"50% profit target hit ({pct_of_max:.0f}%). MRT 50% rule \u2014 exit or trim."
    elif score < 30:
        trade_action = "EXIT"; action_color = "#ef4444"
        action_reason = "Health score critical. High probability of max loss. Exit on next green candle."
    elif pnr_breached:
        trade_action = "HEDGE"; action_color = "#f97316"
        action_reason = "PNR breached. Add protective hedge before considering close."
    elif score < 45:
        if dte > 7 and is_credit:
            trade_action = "HEDGE"; action_color = "#f97316"
            action_reason = f"Score {score}/100 \u2014 spread under pressure. Add hedge or reduce size."
        else:
            trade_action = "EXIT"; action_color = "#ef4444"
            action_reason = f"Score {score}/100 with {dte} DTE. Risk/reward no longer favourable."
    elif score >= 80 and pct_of_max is not None and 10 <= pct_of_max < 50 and dte > 14:
        trade_action = "ADD"; action_color = "#3b82f6"
        action_reason = f"High health ({score}/100), regime aligned. Consider adding to position."
    elif score >= 65:
        trade_action = "HOLD"; action_color = "#22c55e"
        action_reason = f"Score {score}/100. Trade progressing well. Let theta work."
    else:
        trade_action = "HOLD"; action_color = "#f59e0b"
        action_reason = f"Score {score}/100. Monitor PNR and DTE closely."

    # Suggestions list
    suggestions = []

    if trade_action in ("TRIM","EXIT") and pct_of_max is not None and pct_of_max >= 50:
        suggestions.append({"type":"TRIM","priority":"HIGH",
            "text":f"Profit at {pct_of_max:.0f}% of max (${abs(unrealised_pnl or 0):.0f}). MRT 50% rule: close now."})

    if pnr_breached and dte > 3:
        suggestions.append({"type":"EXIT","priority":"HIGH",
            "text":"PNR breached. Per PNR Method: if candle closes beyond PNR next session, exit immediately."})

    if trade_action == "HEDGE":
        if is_bull:
            suggestions.append({"type":"HEDGE","priority":"HIGH",
                "text":f"Buy bear put spread on {symbol} to cap downside. Or SPY put spread as portfolio hedge."})
        elif tt == "CS":
            suggestions.append({"type":"HEDGE","priority":"HIGH",
                "text":f"Buy bull call spread on {symbol} to neutralise upside. Consider SPY call spread."})
        elif tt == "IC" and "PUT" in ol:
            suggestions.append({"type":"HEDGE","priority":"HIGH",
                "text":"Put side threatened. Roll put spread down 2-3 strikes or buy back put side."})

    if trade_action == "ADD" and score >= 80:
        iv_str = f"{iv:.0f}" if iv else "\u2014"
        suggestions.append({"type":"ADD","priority":"MEDIUM",
            "text":f"Regime {reg.get('symbol_regime','')} + IVR {iv_str}% supports entry. Add 1/2 position at same strikes."})

    if score < 50 and dte > 5 and is_credit:
        suggestions.append({"type":"ROLL","priority":"MEDIUM",
            "text":"Roll out 2-4 weeks for credit. Close spread, re-open same or better strikes next expiry."})

    if iv is not None and iv < 25 and is_credit:
        suggestions.append({"type":"HEDGE","priority":"MEDIUM",
            "text":f"IVR {iv:.0f}% is very low \u2014 thin premium. Avoid adding unless vol expands."})

    if oi_sig == "SHORT_BUILDUP" and is_bull:
        suggestions.append({"type":"HEDGE","priority":"MEDIUM",
            "text":"Futures OI short buildup detected. Bearish institutional positioning may pressure your bull trade."})
    elif oi_sig == "LONG_BUILDUP" and not is_bull and tt != "IC":
        suggestions.append({"type":"HEDGE","priority":"MEDIUM",
            "text":"Futures OI long buildup detected. Bullish positioning may pressure your bear spread."})

    if not suggestions:
        suggestions.append({"type": trade_action,"priority":"LOW",
            "text": action_reason or f"Trade health {score}/100. No immediate action required."})

    return {
        "probability_score":  score,
        "trade_health_score": score,
        "probability_notes":  score_notes[:4],
        "signal_factors":     signal_factors,
        "recommendation":     trade_action,
        "trade_action":       trade_action,
        "rec_color":          action_color,
        "rec_reason":         action_reason,
        "suggestions":        suggestions,
        "regime_bias":        reg.get("symbol_bias",""),
        "regime_name":        reg.get("symbol_regime",""),
        "iv_rank":            iv,
        "oi_signal":          oi_sig,
    }

def _compute_live_pnl(trade, spot=None):
    """Compute live P&L, PNR, outlook, action for one trade."""
    import json as _json
    t   = dict(trade)
    tt  = t.get("trade_type","")
    qty = _trade_quantity(t)
    ep  = _safe(t.get("entry_price")) or 0
    ls  = _safe(t.get("long_strike"))
    ss  = _safe(t.get("short_strike"))
    expiry = t.get("expiry","")
    symbol = t.get("symbol","")
    legs   = _json.loads(t.get("legs_json") or "[]")
    # Reconstruct strikes from the same resolver used by outlook.
    _resolved = _resolve_trade_strikes(t)
    if tt == "IC":
        put_buy_s   = _safe(_resolved.get("put_buy"))
        put_sell_s  = _safe(_resolved.get("put_sell"))
        call_buy_s  = _safe(_resolved.get("call_buy"))
        call_sell_s = _safe(_resolved.get("call_sell"))
    else:
        # Fill any missing legacy strike columns from legs_json.
        if ls is None:
            ls = _safe(_resolved.get("buy"))
        if ss is None:
            ss = _safe(_resolved.get("sell"))


    num_legs = int(t.get("num_legs") or 0)
    net_prem = _trade_net_premium(t) or 0

    dte = max(0, (datetime.strptime(expiry,"%Y-%m-%d").date()-date.today()).days) if expiry else 0

    if spot is None:
        spot = _live_spot(symbol)

    # Fetch ATR for PNR
    atr = _live_atr(symbol)

    #print(
    #    "PNR DEBUG",
    #    symbol,
    #    "ATR=", atr,
    #    "LS=", ls,
    #    "SS=", ss,
    #    "TYPE=", tt
    #)

    # ── PNR calculation per trade type ────────────────────────────────────
    # PDF formula: PNR = long_strike - (long_strike * dte * atr) / 2000
    # Bull spreads (PS/PB): price going DOWN is risk. PNR = lower bound below long strike.
    # Bear spreads (CS/CB): price going UP is risk. PNR = upper bound above short strike.
    # IC: two-sided PNR — put side (lower) and call side (upper).
    pnr = None; pnr_upper = None
    resolved = _resolve_trade_strikes(t)
    buy_strike = _safe(resolved.get("buy"))
    sell_strike = _safe(resolved.get("sell"))

    # Debit trades -> long leg anchor
    if tt == "PB" and buy_strike and atr:
        pnr = _compute_pnr(buy_strike, dte, atr)
    elif tt == "CB" and buy_strike and atr:
        pnr_upper = round(buy_strike + (buy_strike * dte * atr) / 2000, 2)
        pnr = pnr_upper
    # Credit trades -> short leg anchor
    elif tt == "PS" and sell_strike and atr:
        pnr = _compute_pnr(sell_strike, dte, atr)
    elif tt == "CS" and sell_strike and atr:
        pnr_upper = round(sell_strike + (sell_strike * dte * atr) / 2000, 2)
        pnr = pnr_upper
    elif tt == "IC" and atr:
        put_buy_s   = _safe(resolved.get("put_buy"))
        call_sell_s = _safe(resolved.get("call_sell"))
        if put_buy_s:  pnr       = _compute_pnr(put_buy_s, dte, atr)              # lower bound
        if call_sell_s:pnr_upper = round(call_sell_s + (call_sell_s*dte*atr)/2000,2)  # upper bound

    # Fetch option marks
    current_mark = None
    unrealised_pnl = None  # will be set by multi-leg path or single-leg path below

    # Multi-leg: fetch each leg separately
    if legs and num_legs > 0:
        total_current = 0.0; any_missing = False
        for leg in legs:
            leg_strike  = _safe(leg.get("strike"))
            leg_expiry  = leg.get("expiry") or expiry
            leg_side    = leg.get("side","buy")  # buy or sell
            leg_type    = leg.get("option_type","call")  # call, put, stock
            leg_qty     = int(leg.get("qty") or 1)
            leg_entry   = _safe(leg.get("price")) or 0
            if leg_type == "stock":
                # Stock leg: use live spot price
                # P&L perspective: net_prem + total_current = unrealised P&L
                # BUY stock: net_prem = -entry*qty (paid). Close = +live*qty (receive). 
                # SELL stock: net_prem = +entry*qty (received). Close = -live*qty (pay to cover).
                live_px = _live_spot(symbol) or spot or 0
                if leg_side == "buy":   total_current += live_px * leg_qty   # closing: receive cash
                else:                   total_current -= live_px * leg_qty   # closing: pay cash
            elif leg_strike:
                live_px = _live_option_price(symbol, leg_expiry, leg_strike,
                                             "call" if leg_type=="call" else "put")
                if live_px is None: any_missing = True; continue
                if leg_side == "sell": total_current += live_px * leg_qty
                else:                  total_current -= live_px * leg_qty
        if not any_missing:
            # net_prem = what we collected at entry (positive=credit, negative=debit)
            # current_total = what we'd collect/pay to close now
            # P&L = entry cashflow minus current mark (same sign convention as net_prem)
            # For credit positions net_prem is positive; for debit positions it is negative.
            has_stock_leg = any(l.get("option_type")=="stock" for l in legs)
            multiplier = 1 if has_stock_leg else 100
            unrealised_pnl = round((net_prem - total_current) * multiplier, 2)
            current_mark = round(abs(total_current), 2)

    if tt == "IC":
        ps = _safe(t.get("put_sell"))  or ss
        pb = _safe(t.get("put_buy"))   or (round(float(ls or 0)-5,2) if ls else None)
        cs2= _safe(t.get("call_sell")) or ss
        cb = _safe(t.get("call_buy"))  or (round(float(ss or 0)+5,2) if ss else None)
        pp  = _live_option_price(symbol, expiry, ps, "put")  if ps else None
        ppl = _live_option_price(symbol, expiry, pb, "put")  if pb else None
        cp  = _live_option_price(symbol, expiry, cs2,"call") if cs2 else None
        cpl = _live_option_price(symbol, expiry, cb, "call") if cb else None
        pm  = round(pp-ppl,2) if pp is not None and ppl is not None else None
        cm  = round(cp-cpl,2) if cp is not None and cpl is not None else None
        if pm is not None and cm is not None: current_mark = round(pm+cm,2)
        elif pm is not None: current_mark = pm
        elif cm is not None: current_mark = cm
    elif tt in ("PS","CS"):
        side = "put" if tt=="PS" else "call"
        sp = _live_option_price(symbol, expiry, ss, side) if ss else None
        lp = _live_option_price(symbol, expiry, ls, side) if ls else None
        if sp is not None and lp is not None: current_mark = round(sp-lp,2)
    elif ls:
        side = "put" if tt=="PB" else "call"
        lp = _live_option_price(symbol, expiry, ls, side)
        sp = _live_option_price(symbol, expiry, ss, side) if ss else 0
        if lp is not None: current_mark = round(lp-(sp or 0),2)

    # P&L — Note: unrealised_pnl may already be set by multi-leg path above
    # Only reset if it was never set
    pct_of_max = None
    max_profit = _trade_max_reward(t)
    max_loss   = _trade_max_risk(t)

    # Only compute single-leg P&L if multi-leg path didn't already do it
    if current_mark is not None and unrealised_pnl is None:
        if tt == "Stock":
            # Stock P&L = (current_price - entry_price) × shares (NO ×100)
            try:
                import json as _jj2
                legs_t = _jj2.loads(t.get("legs_json") or "[]")
                sl = next((l for l in legs_t if l.get("option_type")=="stock"), None)
                shares = max(qty, int(sl.get("qty", qty) or qty)) if sl else qty
            except:
                shares = qty
            unrealised_pnl = round((current_mark - ep) * shares, 2)
        elif tt in ("PS","CS","IC"):
            # Use net_prem/qty (per-contract point value) not ep to avoid
            # dollar-vs-points confusion. net_prem is stored as total for all qty.
            net_prem_per = (net_prem / max(qty, 1)) if qty and net_prem else ep
            unrealised_pnl = round((net_prem_per - current_mark) * qty * 100, 2)
        else:
            net_prem_per = (abs(net_prem) / max(qty, 1)) if qty and net_prem else ep
            unrealised_pnl = round((current_mark - net_prem_per) * qty * 100, 2)

    # pct_of_max (for credit spreads only)
    if unrealised_pnl is not None and tt in ("PS","CS","IC"):
        pct_of_max = round(unrealised_pnl / max_profit * 100, 1) if max_profit else 0

    # Outlook
    outlook = _compute_outlook(t, spot, dte)

    # PNR breach check (options trades only)
    pnr_breached = False
    pnr_status   = "SAFE"
    if _is_open_option_trade(t) and spot:
        pnr_breached, pnr_status, _pnr_side = _pnr_breach_summary(t, spot, pnr, pnr_upper)

    # ── Action recommendation (MRT Rule Book + PNR Method) ──────────────
    action = "HOLD"; action_reason = ""; urgency = "low"

    if dte == 0:
        action = "CLOSE / EXPIRE"
        action_reason = "Expiry today — close ITM spreads, let OTM expire"
        urgency = "critical"

    elif dte <= 2:
        action = "CLOSE"
        action_reason = f"Only {dte} DTE — gamma risk too high, close regardless of P&L"
        urgency = "high"

    elif pnr_breached:
        # PNR Method: if candle CLOSES below/above PNR line, place 50% loss order
        action = f"⚠ PNR BREACHED"
        action_reason = f"{pnr_status}. Per PNR Method: if candle closes beyond PNR next session, place 50% loss order. ATR={atr}"
        urgency = "high"

    elif "LOSER" in (outlook or "").upper() and unrealised_pnl is not None and unrealised_pnl < -(max_loss * 0.3) and dte <= 15:
        # MRT 50% loss rule: apply when < 15 DTE and losing
        loss_pct = round(abs(unrealised_pnl)/max_loss*100, 0) if max_loss else 0
        action = "CLOSE — 50% Loss Rule"
        action_reason = f"ITM + {loss_pct:.0f}% of max loss with {dte} DTE. MRT Rule: close on next green day"
        urgency = "high"

    elif unrealised_pnl is not None and max_loss and unrealised_pnl < -(max_loss * 0.5):
        action = "CLOSE / ROLL"
        action_reason = f"Loss > 50% of max (${abs(unrealised_pnl):.0f} vs max loss ${max_loss:.0f}). Stop out or roll out to next expiry."
        urgency = "high"

    elif pct_of_max is not None and pct_of_max >= 75:
        action = "CLOSE"
        action_reason = f"≥75% of max profit captured ({pct_of_max:.0f}% = ${unrealised_pnl:.0f}). Take the win — avoid gamma risk."
        urgency = "medium"

    elif pct_of_max is not None and pct_of_max >= 50:
        action = "CLOSE (50% rule)"
        action_reason = f"50% profit target hit ({pct_of_max:.0f}% = ${unrealised_pnl:.0f}). MRT standard exit. Close on current day."
        urgency = "low"

    elif dte <= 7 and pct_of_max is not None and pct_of_max < 20:
        action = "CLOSE"
        action_reason = f"< 1 week left, only {pct_of_max:.0f}% profit. Limited upside vs rising gamma risk."
        urgency = "medium"

    elif "PROJECTED WINNER" in (outlook or "").upper():
        pct_str = f"{pct_of_max:.0f}% profit, " if pct_of_max is not None else ""
        action = "HOLD"
        action_reason = f"{pct_str}{dte} DTE — theta decaying in your favor. {pnr_status}."
        urgency = "low"

    elif "OTM" in (outlook or "").upper():
        action = "MONITOR"
        action_reason = f"Spread is OTM with {dte} DTE. Watch PNR level. {pnr_status}."
        urgency = "low"

    else:
        action = "HOLD"
        action_reason = f"Outlook: {outlook}. {dte} DTE remaining. {pnr_status}."
        urgency = "low"

    # ── Probability & analytics scoring ────────────────────────────────────
    score = _trade_probability_score(t, spot, dte, atr, pnr, pnr_upper,
                                     pnr_breached, outlook, pct_of_max, unrealised_pnl,
                                     max_loss)
    # ── Market + Sector Regime Scoring (sector = 2× weight) ──────────────
    try:
        import sqlite3 as _sq3b
        from pathlib import Path as _P3
        _db3 = str(_P3(__file__).resolve().parents[2] / "options_data.db")
        _con3 = _sq3b.connect(_db3)

        # Market regime via SPY (weight 1×: ±5 pts)
        _spy = _con3.execute(
            "SELECT bias, confidence FROM regime_scan WHERE symbol='SPY' ORDER BY scan_date DESC LIMIT 1"
        ).fetchone()
        if _spy:
            _bias3, _conf3 = (_spy[0] or "").lower(), float(_spy[1] or 50) / 100
            if "bull" in _bias3:   score += int(5 * _conf3)
            elif "bear" in _bias3: score -= int(5 * _conf3)

        # Sector regime via ETF proxy (weight 2×: ±10 pts)
        _sec_row = _con3.execute(
            "SELECT sector FROM sector_cache WHERE symbol=?", (symbol.upper(),)
        ).fetchone() if symbol else None
        if _sec_row and _sec_row[0]:
            _ETFS = {"Technology":"XLK","Healthcare":"XLV","Financials":"XLF",
                     "Energy":"XLE","Consumer Cyclical":"XLY","Communication Services":"XLC",
                     "Industrials":"XLI","Basic Materials":"XLB","Utilities":"XLU",
                     "Real Estate":"XLRE","Consumer Defensive":"XLP"}
            _etf3 = _ETFS.get(_sec_row[0])
            if _etf3:
                _sr = _con3.execute(
                    "SELECT bias, confidence FROM regime_scan WHERE symbol=? ORDER BY scan_date DESC LIMIT 1",
                    (_etf3,)
                ).fetchone()
                if _sr:
                    _sb3, _sc3 = (_sr[0] or "").lower(), float(_sr[1] or 50) / 100
                    if "bull" in _sb3:   score += int(10 * _sc3)
                    elif "bear" in _sb3: score -= int(10 * _sc3)
        _con3.close()
    except: pass

    return {
        "spot": spot, "atr": atr,
        "pnr": pnr, "pnr_upper": pnr_upper, "pnr_status": pnr_status, "pnr_breached": pnr_breached,
        "current_mark": current_mark, "unrealised_pnl": unrealised_pnl,
        "pct_of_max_profit": pct_of_max, "max_profit": max_profit, "max_loss": max_loss,
        "dte": dte, "outlook": outlook,
        "action": action, "action_reason": action_reason, "urgency": urgency,
        **score,
    }

# ── Routes ─────────────────────────────────────────────────────────────────
@journal_bp.route("/journal/portfolio_stats", methods=["GET"])
def portfolio_stats():
    """Portfolio header — computes from DB trades + last cached live prices."""
    con = _conn()
    open_trades  = [dict(r) for r in con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()]
    closed_trades= [dict(r) for r in con.execute("SELECT * FROM trades WHERE status='CLOSED'").fetchall()]
    con.close()

    winners  = [t for t in closed_trades if (t.get("pnl") or 0) > 0]
    losers   = [t for t in closed_trades if (t.get("pnl") or 0) <= 0]
    overall_pnl  = round(sum(t.get("pnl",0) or 0 for t in closed_trades), 2)
    win_rate_pct = round(len(winners)/len(closed_trades)*100,2) if closed_trades else 0

    # Bull/bear risk breakdown — IC counted separately, correct width
    bull_trades = [t for t in open_trades if t.get("trade_type") in ("PS","PB","CB")]
    bear_trades = [t for t in open_trades if t.get("trade_type") in ("CS",)]
    ic_trades   = [t for t in open_trades if t.get("trade_type") == "IC"]

    bull_risk   = sum(_trade_max_risk(t)   for t in bull_trades)
    bull_reward = sum(_trade_max_reward(t) for t in bull_trades)
    bear_risk   = sum(_trade_max_risk(t)   for t in bear_trades)
    bear_reward = sum(_trade_max_reward(t) for t in bear_trades)
    ic_risk     = sum(_trade_max_risk(t)   for t in ic_trades)
    ic_reward   = sum(_trade_max_reward(t) for t in ic_trades)

    total_risk   = bull_risk + bear_risk + ic_risk
    total_reward = bull_reward + bear_reward + ic_reward
    overall_rr   = round(total_reward/total_risk,2) if total_risk else 0

    capital = 30000
    capital_deployed = round(total_risk/capital*100,2) if capital else 0

    # Outlook-based counts (from last cached outlook in DB)
    proj_win  = sum(1 for t in open_trades if "WINNER" in (t.get("outlook") or "").upper())
    proj_lose = sum(1 for t in open_trades if "LOSER"  in (t.get("outlook") or "").upper())
    itm_count = sum(1 for t in open_trades if "ITM"    in (t.get("outlook") or "").upper())
    otm_count = sum(1 for t in open_trades if "OTM"    in (t.get("outlook") or "").upper())

    # Current P/L = sum of unrealised_pnl cached in DB (from last refresh)
    current_pnl = round(sum(_safe(t.get("current_pnl") or 0) or 0 for t in open_trades), 2)

    return jsonify({
        "current_pnl": current_pnl, "overall_pnl": overall_pnl,
        "winners": len(winners), "losers": len(losers), "win_rate": win_rate_pct,
        "bull_risk": bull_risk, "bull_reward": bull_reward,
        "bear_risk": bear_risk, "bear_reward": bear_reward,
        "ic_risk": ic_risk, "ic_reward": ic_reward,
        "total_risk": total_risk, "total_reward": total_reward, "overall_rr": overall_rr,
        "capital_deployed_pct": capital_deployed, "open_count": len(open_trades),
        "closed_count": len(closed_trades), "bull_trades": len(bull_trades),
        "bear_trades": len(bear_trades), "ic_trades": len(ic_trades),
        "itm_count": itm_count, "otm_count": otm_count,
        "proj_winners": proj_win, "proj_losers": proj_lose,
        "net_pnl": round(overall_pnl + current_pnl, 2),
    })

def _format_trade_for_grid(row: dict) -> dict:
    """Return a JSON row with display-sensitive money fields rounded to 2 decimals."""
    d = dict(row or {})
    for key in ("entry_price", "exit_price", "put_credit", "call_credit", "current_pnl", "risk_amt", "reward_amt"):
        if key in d and d.get(key) not in (None, ""):
            val = _safe(d.get(key), 2)
            if val is not None:
                d[key] = val
    return d


@journal_bp.route("/journal/trades", methods=["GET"])
def get_trades():
    status = request.args.get("status")
    con = _conn()
    if status and status != "all":
        rows = con.execute("SELECT * FROM trades WHERE status=? ORDER BY entry_date DESC",(status,)).fetchall()
    else:
        rows = con.execute("SELECT * FROM trades ORDER BY entry_date DESC").fetchall()
    con.close()
    out = []
    money_cols = {"entry_price", "exit_price", "net_premium", "risk_amt", "reward_amt",
                  "current_pnl", "spot_price", "put_credit", "call_credit", "pnl"}
    for r in rows:
        d = dict(r)
        for k in money_cols:
            if k in d and d.get(k) is not None:
                v = _safe(d.get(k))
                if v is not None:
                    d[k] = round(float(v), 2)
        out.append(d)
    return _jsonify_safe(out)

@journal_bp.route("/journal/trade-alerts", methods=["GET"])
def get_trade_alerts():
    """Return open option-position PNR alerts for the alerts hub."""
    status = (request.args.get("status") or "open").strip().lower()
    con = _conn()
    try:
        if status == "all":
            rows = con.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY entry_date DESC").fetchall()
        else:
            rows = con.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY entry_date DESC").fetchall()
    except Exception:
        rows = []
    finally:
        con.close()

    items = []
    for row in rows:
        try:
            live = _compute_live_pnl(row)
            _maybe_log_trade_pnr_alert(row, live)
            items.append({
                "id": row["id"],
                "symbol": row["symbol"],
                "trade_type": row["trade_type"],
                "status": row["status"],
                "spot": live.get("spot"),
                "pnr": live.get("pnr"),
                "pnr_upper": live.get("pnr_upper"),
                "pnr_status": live.get("pnr_status"),
                "pnr_breached": live.get("pnr_breached"),
                "pnr_alert_enabled": _pnr_alerts_enabled(),
                "pnr_alert_last_breached": int(row["pnr_alert_last_breached"] or 0) if "pnr_alert_last_breached" in row.keys() else int(live.get("pnr_breached") or 0),
                "pnr_alert_last_sent_at": row["pnr_alert_last_sent_at"] if "pnr_alert_last_sent_at" in row.keys() else None,
                "entry_date": row["entry_date"],
                "expiry": row["expiry"],
                "direction": "bear" if row["trade_type"] in ("CS", "CB") else "bull",
            })
        except Exception:
            continue
    return _jsonify_safe({"alerts": items, "count": len(items)})


@journal_bp.route("/journal/trade/<int:tid>/pnr_alert", methods=["POST"])
def set_trade_pnr_alert(tid):
    # PNR alerts are always on; keep endpoint for backwards compatibility.
    return jsonify({"ok": True, "id": tid, "enabled": 1})


@journal_bp.route("/journal/trade/<int:tid>/pnr_alert/test", methods=["POST", "GET"])
def test_trade_pnr_alert(tid):
    """Evaluate the live PNR alert state for a trade without mutating state."""
    con = _conn()
    try:
        row = con.execute('SELECT * FROM trades WHERE id=?', (tid,)).fetchone()
    finally:
        con.close()
    if not row:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    row = _as_dict(row)
    live = _compute_live_pnl(row)
    telegram = {'configured': True, 'sent': 0, 'error': 'not breached'}
    if not _pnr_alerts_enabled():
        return jsonify({'ok': True, 'enabled': False, 'status': 'disabled', 'message': 'PNR alerts are disabled in settings', 'trade_id': tid, 'symbol': row.get('symbol'), 'trade_type': row.get('trade_type'), 'pnr': live.get('pnr'), 'pnr_upper': live.get('pnr_upper'), 'pnr_status': live.get('pnr_status'), 'pnr_breached': live.get('pnr_breached'), 'telegram': {'ok': False, 'reason': 'PNR alerts disabled'}})
    if live.get('pnr_breached'):
        telegram = _send_trade_pnr_telegram(row, live, prefix='📣 PNR test')
    status = 'breached' if live.get('pnr_breached') else 'safe'
    message = live.get('pnr_status') or ('PNR breached' if live.get('pnr_breached') else 'PNR safe')
    if telegram.get('error') and live.get('pnr_breached'):
        message = f"{message} · Telegram: {telegram.get('error')}"
    return _jsonify_safe({
        'ok': True,
        'status': status,
        'message': message,
        'trade_id': tid,
        'symbol': row['symbol'],
        'trade_type': row['trade_type'],
        'spot': live.get('spot'),
        'pnr': live.get('pnr'),
        'pnr_upper': live.get('pnr_upper'),
        'pnr_status': live.get('pnr_status'),
        'pnr_breached': live.get('pnr_breached'),
        'direction': 'bear' if row['trade_type'] in ('CS', 'CB') else 'bull',
        'telegram': telegram,
    })


@journal_bp.route("/journal/trade-alerts/status", methods=["GET"])
def trade_alerts_status():
    """Return trade PNR watcher status and last run summary."""
    return jsonify({
        'watcher_running': bool(_trade_alert_watcher_started),
        'last_run_at': _trade_alert_last_run_at,
        'last_result': _trade_alert_last_result,
    })

@journal_bp.route("/journal/trade/<int:tid>/live", methods=["GET"])
def trade_live(tid):
    con = _conn()
    row = con.execute("SELECT * FROM trades WHERE id=?",(tid,)).fetchone()
    con.close()
    if not row: return jsonify({"error":"not found"}),404
    data = _compute_live_pnl(row)
    try:
        ai = _build_ai_alert_analysis(_as_dict(row), data)
        data.update({
            "ai_score": ai.get("score"),
            "ai_grade": ai.get("grade"),
            "ai_confidence": ai.get("confidence"),
            "ai_headline": ai.get("headline"),
            "ai_summary": ai.get("summary"),
            "ai_recommendation": ai.get("recommendation"),
            "ai_action_mode": ai.get("action_mode"),
            "ai_action_text": ai.get("action_text") or ai.get("explicit_action"),
            "ai_roll_side": ai.get("roll_side"),
            "ai_roll_expiry": ai.get("roll_expiry"),
            "ai_roll_strategy": ai.get("roll_strategy"),
            "ai_roll_rr": ai.get("roll_rr"),
            "ai_roll_pop": ai.get("roll_pop"),
            "ai_thesis": ai.get("thesis", []),
            "ai_risks": ai.get("risks", []),
            "ai_next_actions": ai.get("next_actions", []),
            "ai_watch_items": ai.get("watch_items", []),
            "ai_decision_reasons": ai.get("decision_reasons", []),
            "ai_price_alerts": ai.get("critical_price_alerts", []),
        })
    except Exception:
        pass
    # Cache spot, outlook, current_pnl back to DB
    _cache_live(tid, data)
    return _jsonify_safe(data)

def _cache_live(tid, data):
    """Store live data back to DB for portfolio header calculations."""
    try:
        con = _conn()
        con.execute("""UPDATE trades SET
            suggested_action=?, outlook=?, current_pnl=?,
            spot_price=?, prob_score=?, analytics_rec=?, analytics_json=?
            WHERE id=?""", (
            data.get("action",""),
            data.get("outlook",""),
            data.get("unrealised_pnl"),
            data.get("spot"),
            data.get("probability_score"),
            data.get("recommendation",""),
            json.dumps(_json_sanitize({
                "rec_reason":  data.get("rec_reason",""),
                "suggestions": data.get("suggestions",[]),
                "pnr":         data.get("pnr"),
                "pnr_upper":   data.get("pnr_upper"),
                "pnr_status":  data.get("pnr_status",""),
                "pnr_breached":data.get("pnr_breached",False),
                "notes":       data.get("probability_notes",[]),
            })),
            tid
        ))
        con.commit(); con.close()
    except: pass

@journal_bp.route("/journal/trades/refresh_all", methods=["POST"])
def refresh_all():
    """Refresh all open trades and update DB cache."""
    con = _conn()
    trades = con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
    con.close()
    results = []
    for t in trades:
        live = _compute_live_pnl(t)
        _maybe_log_trade_pnr_alert(t, live)
        _cache_live(t["id"], live)
        results.append({"id":t["id"],"symbol":t["symbol"],**live})
    return _jsonify_safe(results)



_STRATEGY_TYPE_MAP = {
    "PS": "bull_put",
    "CS": "bear_call",
    "PB": "bull_call",
    "CB": "bear_put",
    "IC": "condor",
}


@lru_cache(maxsize=128)
def _journal_strategy_preview(symbol: str, expiry: str, trade_type: str) -> dict:
    symbol = (symbol or "").upper().strip()
    expiry = (expiry or "").strip()
    trade_type = (trade_type or "").strip().upper()
    if not symbol or not expiry:
        return {"strategies": []}
    try:
        from ..scanners.routes_strategy import _full_analysis
        analysis = _full_analysis(symbol, expiry, requested_type=_STRATEGY_TYPE_MAP.get(trade_type), prefer_live_prices=True)
        strategies = list(analysis.get("strategies") or [])
        if not strategies and trade_type in _STRATEGY_TYPE_MAP:
            analysis = _full_analysis(symbol, expiry, requested_type=None, prefer_live_prices=True)
            strategies = list(analysis.get("strategies") or [])
        best = strategies[0] if strategies else None
        return {
            "symbol": symbol,
            "expiry": expiry,
            "trade_type": trade_type,
            "spot": analysis.get("spot"),
            "oi_summary": analysis.get("oi_summary") or {},
            "best_strategy": best,
            "strategies": strategies[:3],
            "summary": analysis.get("ta") or {},
            "error": None,
        }
    except Exception as exc:
        return {"symbol": symbol, "expiry": expiry, "trade_type": trade_type, "strategies": [], "error": str(exc)[:180]}


@lru_cache(maxsize=256)
def _future_expirations_for_symbol(symbol: str) -> list[str]:
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return []
    try:
        con = _conn()
        try:
            rows = con.execute(
                "SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>=? ORDER BY expiration",
                (symbol, date.today().isoformat()),
            ).fetchall()
        finally:
            con.close()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def _needs_ai_roll_review(t: dict, live: dict) -> bool:
    score = float(live.get("trade_health_score") or live.get("probability_score") or 0)
    dte = int(round(_safe(live.get("dte"), 0) or 0))
    pct = _safe(live.get("pct_of_max_profit"))
    return bool(
        live.get("pnr_breached")
        or score < 58
        or dte <= 14
        or (pct is not None and pct >= 50)
    )


def _roll_candidates_for_trade(t: dict, live: dict, limit: int = 3) -> list[dict]:
    symbol = (t.get("symbol") or "").upper().strip()
    expiry = str(t.get("expiry") or "").strip()
    trade_type = str(t.get("trade_type") or "").strip().upper()
    if not symbol or not expiry:
        return []
    expirations = [e for e in _future_expirations_for_symbol(symbol) if e > expiry]
    if not expirations:
        return []

    candidates: list[dict] = []
    for exp in expirations[:4]:
        try:
            analysis = _journal_strategy_preview(symbol, exp, trade_type)
            best = analysis.get("best_strategy") or {}
            if not best:
                continue
            rr = _safe(best.get("rr"))
            pop = _safe(best.get("pop"))
            score = _safe(best.get("score")) or _safe(best.get("signal_score"))
            candidates.append({
                "expiry": exp,
                "name": best.get("name") or best.get("strategy") or "Best strategy",
                "legs": best.get("legs") or best.get("suggested_trade") or best.get("suggested_spread") or "",
                "rr": rr,
                "pop": pop,
                "manage": best.get("manage") or "",
                "rationale": best.get("rationale") or best.get("bias") or "",
                "score": score,
            })
        except Exception:
            continue
    candidates.sort(key=lambda x: ((x.get("rr") or 0), (x.get("pop") or 0), (x.get("score") or 0)), reverse=True)
    return candidates[:limit]


def _roll_threat_side(t: dict, live: dict) -> str:
    tt = str(t.get("trade_type") or "").upper().strip()
    pnr = _safe(live.get("pnr"))
    pnr_upper = _safe(live.get("pnr_upper"))
    spot = _safe(live.get("spot"))
    pnr_status = str(live.get("pnr_status") or "").upper()
    if "PUT" in pnr_status:
        return "put"
    if "CALL" in pnr_status:
        return "call"
    if tt == "IC" and spot is not None and pnr is not None and pnr_upper is not None:
        width = max(pnr_upper - pnr, 0.01)
        if spot <= pnr + width * 0.30:
            return "put"
        if spot >= pnr_upper - width * 0.30:
            return "call"
    if tt in {"PS", "PB"} and pnr is not None and spot is not None and spot <= pnr:
        return "put"
    if tt in {"CS", "CB"} and pnr_upper is not None and spot is not None and spot >= pnr_upper:
        return "call"
    return ""


def _draft_trade_from_payload(d):
    """Build a trade-like dict from the Add Trade form without saving it.

    The Add Trade screen should preview the same journal health score that the
    Open Trades journal shows.  This helper converts unsaved form data into the
    same shape as a row from the trades table, keeping legs as the source of
    truth for strikes, quantities, expiry, and net premium.
    """
    import json as _json
    legs = d.get("legs") or []
    norm_legs = []
    for leg in legs:
        try:
            opt = str(leg.get("option_type") or "call").lower()
            norm_legs.append({
                "side": str(leg.get("side") or "buy").lower(),
                "option_type": opt,
                "strike": float(leg.get("strike") or 0) if opt != "stock" else 0,
                "expiry": leg.get("expiry") or "",
                "qty": max(1, int(leg.get("qty") or 1)),
                "price": float(leg.get("price") or 0),
            })
        except Exception:
            continue

    net_premium = 0.0
    for leg in norm_legs:
        px = float(leg.get("price") or 0)
        q = int(leg.get("qty") or 1)
        if leg.get("option_type") == "stock":
            net_premium += (-px * q) if leg.get("side") == "buy" else (px * q)
        else:
            net_premium += (px * q) if leg.get("side") == "sell" else (-px * q)

    raw_tt = d.get("trade_type") or "IC"
    tt = "Stock" if str(raw_tt).lower() == "stock" else str(raw_tt).upper()
    symbol = (d.get("symbol") or "").upper().strip()
    expiry = d.get("expiry") or ""
    for leg in norm_legs:
        if leg.get("option_type") != "stock" and leg.get("expiry"):
            expiry = leg.get("expiry")
            break

    stock_leg = next((l for l in norm_legs if l.get("option_type") == "stock"), None)
    option_qty = _option_qty_from_legs(norm_legs, d.get("quantity", 1))
    if stock_leg:
        quantity = max(1, int(stock_leg.get("qty") or d.get("quantity") or 1))
        entry_price = float(stock_leg.get("price") or d.get("entry_price") or 0)
    elif norm_legs:
        quantity = max(1, int(d.get("quantity") or option_qty or 1))
        entry_price = abs(net_premium) / max(quantity, 1)
    else:
        quantity = max(1, int(d.get("quantity") or 1))
        entry_price = float(d.get("entry_price") or 0)
        if tt in ("PS", "CS", "IC", "CC", "CP", "STRANGLE", "STRADDLE"):
            net_premium = abs(entry_price) * quantity
        elif tt != "STOCK":
            net_premium = -abs(entry_price) * quantity

    put_buy = put_sell = call_buy = call_sell = None
    for leg in norm_legs:
        opt = leg.get("option_type")
        side = leg.get("side")
        strike = _safe(leg.get("strike"))
        if not strike:
            continue
        if opt == "put" and side == "buy" and put_buy is None:
            put_buy = strike
        elif opt == "put" and side == "sell" and put_sell is None:
            put_sell = strike
        elif opt == "call" and side == "buy" and call_buy is None:
            call_buy = strike
        elif opt == "call" and side == "sell" and call_sell is None:
            call_sell = strike

    long_strike = short_strike = None
    if tt in ("PS", "PB"):
        long_strike, short_strike = put_buy, put_sell
    elif tt in ("CS", "CB"):
        long_strike, short_strike = call_buy, call_sell
    elif tt == "IC":
        long_strike = put_buy or call_buy
        short_strike = put_sell or call_sell

    return {
        "id": 0,
        "status": "OPEN",
        "entry_date": d.get("entry_date") or date.today().isoformat(),
        "symbol": symbol,
        "expiry": expiry,
        "trade_type": tt,
        "trade_subtype": d.get("trade_subtype", "vertical"),
        "long_strike": long_strike,
        "short_strike": short_strike,
        "entry_price": round(entry_price, 2),
        "quantity": int(quantity),
        "entry_reason": d.get("entry_reason", ""),
        "sector": d.get("sector", ""),
        "risk_amt": float(d.get("risk_amt") or 0),
        "reward_amt": float(d.get("reward_amt") or 0),
        "put_sell": put_sell,
        "put_buy": put_buy,
        "call_sell": call_sell,
        "call_buy": call_buy,
        "put_credit": None,
        "call_credit": None,
        "legs_json": _json.dumps(norm_legs) if norm_legs else None,
        "num_legs": len(norm_legs),
        "net_premium": round(net_premium, 4),
        "pnr_alert_last_breached": 0,
        "pnr_alert_last_sent_at": None,
    }


@journal_bp.route("/journal/draft_health_score", methods=["POST"])
def draft_health_score():
    """Preview Add Trade score using the same health logic as journal rows."""
    d = request.get_json(force=True) or {}
    trade = _draft_trade_from_payload(d)
    if not trade.get("symbol"):
        return jsonify({"error": "symbol required"}), 400
    try:
        live = _compute_live_pnl(trade)
        score = int(round(float(live.get("trade_health_score") or live.get("probability_score") or 0)))
        grade = "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D" if score >= 35 else "F"
        ai = build_entry_analysis(trade.get("symbol") or "", trade.get("trade_type") or d.get("trade_type") or "IC", trade)
        strat = _journal_strategy_preview(trade.get("symbol") or "", trade.get("expiry") or d.get("expiry") or "", trade.get("trade_type") or d.get("trade_type") or "IC")
        best = strat.get("best_strategy") or {}
        alternatives = strat.get("strategies") or []
        payload = {
            "ok": True,
            "score": score,
            "grade": grade,
            "recommendation": ai.get("recommendation") or live.get("action") or live.get("trade_action") or live.get("recommendation") or "HOLD",
            "action_reason": live.get("action_reason") or live.get("rec_reason") or "",
            "spot": live.get("spot"),
            "dte": live.get("dte"),
            "outlook": live.get("outlook"),
            "pnr": live.get("pnr"),
            "pnr_upper": live.get("pnr_upper"),
            "pnr_status": live.get("pnr_status"),
            "pnr_breached": live.get("pnr_breached"),
            "max_profit": live.get("max_profit"),
            "max_loss": live.get("max_loss"),
            "current_mark": live.get("current_mark"),
            "unrealised_pnl": live.get("unrealised_pnl"),
            "pct_of_max_profit": live.get("pct_of_max_profit"),
            "notes": live.get("probability_notes", []),
            "signal_factors": live.get("signal_factors", []),
            "suggestions": live.get("suggestions", []),
            "analytics_json": live,
            # AI layer
            "ai_summary": ai.get("summary"),
            "ai_headline": ai.get("headline"),
            "ai_confidence": ai.get("confidence"),
            "ai_score_meaning": ai.get("score_meaning"),
            "ai_confidence_meaning": ai.get("confidence_meaning"),
            "ai_explicit_action": ai.get("explicit_action"),
            "ai_action_mode": ai.get("action_mode"),
            "ai_action_text": ai.get("action_text") or ai.get("explicit_action"),
            "ai_roll_side": ai.get("roll_side"),
            "ai_roll_expiry": ai.get("roll_expiry"),
            "ai_roll_strategy": ai.get("roll_strategy"),
            "ai_roll_rr": ai.get("roll_rr"),
            "ai_roll_pop": ai.get("roll_pop"),
            "ai_decision_reasons": ai.get("decision_reasons", []),
            "ai_watch_items": ai.get("watch_items", []),
            "ai_thesis": ai.get("thesis", []),
            "ai_risks": ai.get("risks", []),
            "ai_missing_inputs": ai.get("missing_inputs", []),
            "ai_next_actions": ai.get("next_actions", []),
            "ai_action_steps": ai.get("action_steps", []),
            "ai_monitor_items": ai.get("monitor_items", []),
            "ai_price_alerts": ai.get("critical_price_alerts", []),
            "ai_trade_family": ai.get("trade_family"),
            "ai_best_strategy": best,
            "ai_strategy_alternatives": alternatives[1:3] if len(alternatives) > 1 else [],
            "ai_strategy_summary": strat.get("summary") or {},
            "ai_strategy_error": strat.get("error"),
        }
        if best:
            payload.update({
                "ai_best_strategy_name": best.get("name"),
                "ai_best_strategy_legs": best.get("legs"),
                "ai_best_strategy_expiry": best.get("expiry"),
                "ai_best_strategy_rr": best.get("rr"),
                "ai_best_strategy_pop": best.get("pop"),
                "ai_best_strategy_risk": f"Max loss {best.get('max_loss')} · Max gain {best.get('max_gain')}",
                "ai_best_strategy_manage": best.get("manage"),
                "ai_best_strategy_rationale": best.get("rationale"),
                "ai_best_strategy_score": best.get("score"),
                "ai_best_strategy_grade": best.get("grade"),
                "ai_best_strategy_bias": best.get("bias"),
                "ai_best_strategy_trade": best.get("name"),
                "ai_best_strategy_text": f"{best.get('name')} — {best.get('legs')} · RR {best.get('rr')} · PoP {best.get('pop')}%",
            })
        return jsonify(payload)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:700]}), 500

@journal_bp.route("/journal/trade/add", methods=["POST"])
def add_trade():
    d = request.get_json()
    is_ic = d.get("trade_type") == "IC"
    legs  = d.get("legs", [])  # multi-leg: [{side,option_type,strike,expiry,qty,price}]
    num_legs = len(legs)

    # Net premium: sum of all legs (buy = negative, sell = positive)
    net_premium = 0.0
    for leg in legs:
        leg_px  = float(leg.get("price") or 0)
        leg_qty = int(leg.get("qty") or 1)
        if leg.get("side") == "sell":
            net_premium += leg_px * leg_qty
        else:
            net_premium -= leg_px * leg_qty

    # For simple trades (no legs), use entry_price
    if not legs:
        ep = float(d.get("entry_price") or 0)
        tt = d.get("trade_type","")
        net_premium = ep if tt in ("PS","CS","IC") else -ep

    # Use first leg's expiry if multi-leg and no top-level expiry
    expiry = d.get("expiry") or (legs[0].get("expiry") if legs else "")
    stock_leg = next((l for l in legs if l.get("option_type")=="stock"), None)
    option_qty = _option_qty_from_legs(legs, d.get("quantity", 1))
    entry_price = 0.0
    quantity = 1
    if stock_leg:
        entry_price = float(stock_leg.get("price") or d.get("entry_price") or 0)
        quantity = int(stock_leg.get("qty") or d.get("quantity") or 1)
    elif legs:
        quantity = max(int(d.get("quantity") or 1), int(option_qty or 1))
        entry_price = abs(net_premium) / max(1, quantity)
    else:
        entry_price = float(d.get("entry_price") or 0)
        quantity = int(d.get("quantity") or 1)

    con = _conn()
    con.execute("""
        INSERT INTO trades
        (entry_date,symbol,expiry,trade_type,trade_subtype,long_strike,short_strike,
         entry_price,quantity,entry_reason,sector,risk_amt,reward_amt,status,
         put_sell,put_buy,call_sell,call_buy,put_credit,call_credit,
         legs_json,num_legs,net_premium)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?)
    """, (
        d.get("entry_date", date.today().isoformat()),
        d["symbol"].upper(), expiry, d["trade_type"],
        d.get("trade_subtype","vertical"),
        float(d.get("long_strike") or 0),
        float(d.get("short_strike") or 0) if d.get("short_strike") else None,
        round(float(entry_price), 2),
        int(quantity),
        d.get("entry_reason",""), d.get("sector",""),
        float(d.get("risk_amt",0)), float(d.get("reward_amt",0)),
        float(d.get("put_sell") or 0) if is_ic else None,
        float(d.get("put_buy") or 0) if is_ic else None,
        float(d.get("call_sell") or 0) if is_ic else None,
        float(d.get("call_buy") or 0) if is_ic else None,
        float(d.get("put_credit") or 0) if is_ic else None,
        float(d.get("call_credit") or 0) if is_ic else None,
        json.dumps(legs) if legs else None,
        num_legs,
        round(net_premium, 4),
    ))
    new_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.commit(); con.close()
    # Capture entry snapshot in background thread (non-blocking)
    if _SNAPSHOT_AVAILABLE:
        import threading
        threading.Thread(
            target=capture_entry_snapshot,
            args=(new_id, d["symbol"].upper(), d["trade_type"]),
            kwargs={"spot": float(d.get("entry_price") or 0) or None},
            daemon=True,
        ).start()

    # Cache the same journal health score asynchronously so the open journal row
    # starts with the score previewed on Add Trade.  Alerts still use the normal
    # journal refresh path and are not changed by this preview route.
    def _cache_new_live(_tid):
        try:
            con2 = _conn()
            row2 = con2.execute("SELECT * FROM trades WHERE id=?", (_tid,)).fetchone()
            con2.close()
            if row2:
                live2 = _compute_live_pnl(row2)
                _cache_live(_tid, live2)
        except Exception as _e:
            print(f"[journal] initial live score cache failed for trade {_tid}: {_e}")
    try:
        threading.Thread(target=_cache_new_live, args=(new_id,), daemon=True).start()
    except Exception:
        pass

    return jsonify({"ok": True, "id": new_id})



# ── Journal close / partial-close / side-roll helpers ─────────────────────
def _trade_row_dict(row):
    try:
        return dict(row)
    except Exception:
        if isinstance(row, dict):
            return dict(row)
        return {}


def _trade_table_columns(con):
    try:
        return [r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()]
    except Exception:
        return []


def _insert_trade_clone(con, source_row, updates):
    """Insert a row copied from source_row, applying updates and only using runtime columns."""
    src = _trade_row_dict(source_row)
    payload = {k: v for k, v in src.items() if k != "id"}
    payload.update(updates or {})
    cols = [c for c in _trade_table_columns(con) if c != "id" and c in payload]
    if not cols:
        raise ValueError("No trade columns available for cloned journal row")
    sql = "INSERT INTO trades (" + ",".join(cols) + ") VALUES (" + ",".join(["?"] * len(cols)) + ")"
    con.execute(sql, [payload.get(c) for c in cols])
    try:
        return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
    except Exception:
        return None


def _leg_qty(leg, fallback=1):
    try:
        return max(1, int(float((leg or {}).get("qty", fallback) or fallback)))
    except Exception:
        return max(1, int(fallback or 1))


def _leg_entry_price(leg):
    try:
        v = (leg or {}).get("price")
        if v is None or v == "":
            v = (leg or {}).get("entry_price")
        return float(v or 0)
    except Exception:
        return 0.0


def _safe_close_qty(raw, max_qty):
    try:
        max_qty = max(1, int(float(max_qty or 1)))
    except Exception:
        max_qty = 1
    if raw in (None, "", 0, "0"):
        return max_qty
    try:
        q = int(float(raw))
    except Exception:
        q = max_qty
    return max(1, min(max_qty, q))


def _scale_legs_to_qty(legs, new_qty, base_qty=None):
    """Scale every leg in a spread to a new strategy quantity."""
    try:
        new_qty = max(0, int(float(new_qty or 0)))
    except Exception:
        new_qty = 0
    if new_qty <= 0:
        return []
    if base_qty is None:
        base_qty = _option_qty_from_legs(legs, 1)
    try:
        base_qty = max(1, int(base_qty or 1))
    except Exception:
        base_qty = 1
    out = []
    for leg in legs or []:
        l = dict(leg or {})
        old_q = _leg_qty(l, base_qty)
        scaled = int(round(old_q * (new_qty / base_qty)))
        if scaled <= 0:
            continue
        l["qty"] = scaled
        out.append(l)
    return out


def _split_leg_quantities(legs, selected_indices, close_qty=None):
    """Return (closed_legs, remaining_legs) after closing selected legs.

    close_qty=None means close selected legs in full.  close_qty=N closes N
    contracts from each selected option leg and leaves the remainder open.
    """
    selected = set(int(i) for i in (selected_indices or []))
    closed, remaining = [], []
    for i, leg in enumerate(legs or []):
        l = dict(leg or {})
        q = _leg_qty(l)
        if i not in selected:
            remaining.append(l)
            continue
        q_close = q if close_qty is None else min(q, max(0, int(close_qty or 0)))
        if q_close > 0:
            cl = dict(l); cl["qty"] = q_close; closed.append(cl)
        rem = q - q_close
        if rem > 0:
            rl = dict(l); rl["qty"] = rem; remaining.append(rl)
    return closed, remaining


def _infer_trade_type_from_legs(legs, fallback="Custom"):
    opts = [l for l in (legs or []) if str((l or {}).get("option_type", "")).lower() in ("call", "put")]
    if not opts:
        return "Stock" if any(str((l or {}).get("option_type", "")).lower() == "stock" for l in (legs or [])) else (fallback or "Custom")
    has_put = any(str(l.get("option_type", "")).lower() == "put" for l in opts)
    has_call = any(str(l.get("option_type", "")).lower() == "call" for l in opts)
    has_sell_put = any(str(l.get("option_type", "")).lower() == "put" and str(l.get("side", "")).lower() == "sell" for l in opts)
    has_buy_put = any(str(l.get("option_type", "")).lower() == "put" and str(l.get("side", "")).lower() == "buy" for l in opts)
    has_sell_call = any(str(l.get("option_type", "")).lower() == "call" and str(l.get("side", "")).lower() == "sell" for l in opts)
    has_buy_call = any(str(l.get("option_type", "")).lower() == "call" and str(l.get("side", "")).lower() == "buy" for l in opts)
    if has_put and has_call:
        return "IC"
    if has_sell_put and has_buy_put:
        return "PS"
    if has_sell_call and has_buy_call:
        return "CS"
    if len(opts) == 1 and str(opts[0].get("side", "")).lower() == "buy" and str(opts[0].get("option_type", "")).lower() == "call":
        return "CB"
    if len(opts) == 1 and str(opts[0].get("side", "")).lower() == "buy" and str(opts[0].get("option_type", "")).lower() == "put":
        return "PB"
    return fallback or "Custom"


def _fields_from_legs(legs, fallback_type="Custom", fallback_qty=1, entry_reason=None):
    """Derive trade columns from a legs_json structure."""
    legs = [dict(l or {}) for l in (legs or [])]
    qty = _option_qty_from_legs(legs, fallback_qty)
    stock_leg = next((l for l in legs if str(l.get("option_type", "")).lower() == "stock"), None)
    if stock_leg and not any(str(l.get("option_type", "")).lower() in ("call", "put") for l in legs):
        qty = _leg_qty(stock_leg, fallback_qty)

    net_total = 0.0
    put_credit = 0.0
    call_credit = 0.0
    put_sell = put_buy = call_sell = call_buy = None
    long_strike = 0.0
    short_strike = None
    expiries = []

    for l in legs:
        typ = str(l.get("option_type", "")).lower()
        side = str(l.get("side", "")).lower()
        px = _leg_entry_price(l)
        q = _leg_qty(l, qty)
        if l.get("expiry"):
            expiries.append(str(l.get("expiry"))[:10])
        if typ == "stock":
            net_total += (px * q) * (1 if side == "sell" else -1)
            continue
        sign = 1 if side == "sell" else -1
        net_total += sign * px * q
        try:
            strike = float(l.get("strike") or 0)
        except Exception:
            strike = 0.0
        if typ == "put":
            put_credit += sign * px * q
            if side == "sell":
                put_sell = strike; short_strike = strike
            elif side == "buy":
                put_buy = strike; long_strike = strike
        elif typ == "call":
            call_credit += sign * px * q
            if side == "sell":
                call_sell = strike; short_strike = strike
            elif side == "buy":
                call_buy = strike; long_strike = strike

    tt = _infer_trade_type_from_legs(legs, fallback_type)
    if stock_leg and tt == "Stock":
        entry_price = _leg_entry_price(stock_leg)
        net_premium = net_total
    else:
        entry_price = abs(net_total) / max(1, qty)
        net_premium = net_total
    expiry = expiries[0] if expiries else ""
    subtype = "side_split" if tt not in (fallback_type, "IC") else "vertical"
    return {
        "expiry": expiry,
        "trade_type": tt,
        "trade_subtype": subtype,
        "entry_price": round(float(entry_price or 0), 2),
        "quantity": int(qty or 1),
        "legs_json": json.dumps(legs) if legs else None,
        "num_legs": len(legs),
        "net_premium": round(float(net_premium or 0), 4),
        "put_sell": put_sell,
        "put_buy": put_buy,
        "call_sell": call_sell,
        "call_buy": call_buy,
        "put_credit": round(float(put_credit), 4) if abs(put_credit) > 1e-9 else None,
        "call_credit": round(float(call_credit), 4) if abs(call_credit) > 1e-9 else None,
        "long_strike": float(long_strike or 0),
        "short_strike": short_strike,
        "entry_reason": entry_reason,
    }


def _update_open_trade_from_legs(con, tid, source_row, remaining_legs, note=""):
    """Mutate an open trade to represent its remaining open legs."""
    if not remaining_legs:
        return None
    src = _trade_row_dict(source_row)
    f = _fields_from_legs(remaining_legs, src.get("trade_type") or "Custom", src.get("quantity") or 1)
    er = (src.get("entry_reason") or "")
    if note:
        er = (er + " | " if er else "") + str(note)
    f["entry_reason"] = er[:1000]
    f["status"] = "OPEN"
    f["exit_price"] = None
    f["exit_date"] = None
    f["pnl"] = None
    f["current_pnl"] = None
    cols = [c for c in _trade_table_columns(con) if c in f]
    if cols:
        con.execute("UPDATE trades SET " + ",".join([f"{c}=?" for c in cols]) + " WHERE id=?", [f.get(c) for c in cols] + [tid])
    return f


def _pnl_from_entry_close_net(entry_net, close_net, qty):
    """P&L dollars from a selected spread side using per-spread option points."""
    try:
        entry_net = float(entry_net or 0)
        close_net = float(close_net or 0)
        qty = max(1, int(float(qty or 1)))
    except Exception:
        return 0.0
    if entry_net >= 0:
        return (entry_net - abs(close_net)) * qty * 100
    return (abs(close_net) - abs(entry_net)) * qty * 100


def _leg_pnl_points(leg, exit_price, close_qty=None):
    entry_px = _leg_entry_price(leg)
    try:
        exit_px = float(exit_price or 0)
    except Exception:
        exit_px = 0.0
    qty = close_qty if close_qty is not None else _leg_qty(leg)
    qty = max(0, int(qty or 0))
    sign = -1 if str((leg or {}).get("side", "")).lower() == "sell" else 1
    mult = 1 if str((leg or {}).get("option_type", "")).lower() == "stock" else 100
    return sign * (exit_px - entry_px) * qty * mult


def _pnl_from_leg_exits(original_legs, selected_indices, exit_by_index, close_qty=None):
    total = 0.0
    for i, leg in enumerate(original_legs or []):
        if i not in selected_indices:
            continue
        q = _leg_qty(leg) if close_qty is None else min(_leg_qty(leg), int(close_qty or 0))
        total += _leg_pnl_points(leg, exit_by_index.get(i, 0), q)
    return total


def _selected_indices_from_payload(legs, payload, default_all=True):
    selected = set()
    for le in payload.get("leg_exits") or []:
        try:
            idx = int(le.get("leg_index", 0))
        except Exception:
            continue
        if le.get("closed"):
            selected.add(idx)
    if selected or not default_all:
        return selected
    return set(range(len(legs or [])))


def _exit_by_index_from_payload(payload):
    out = {}
    for le in payload.get("leg_exits") or payload.get("close_leg_exits") or []:
        try:
            idx = int(le.get("leg_index", 0))
            if le.get("exit_price") not in (None, ""):
                out[idx] = float(le.get("exit_price") or 0)
        except Exception:
            continue
    return out


def _insert_closed_leg_event(con, source_row, closed_legs, total_pnl, close_reason, comments, exit_price=None, extra_reason=""):
    src = _trade_row_dict(source_row)
    f = _fields_from_legs(closed_legs, src.get("trade_type") or "Custom", src.get("quantity") or 1)
    reason = extra_reason or f"Closed portion from #{src.get('id')}"
    if comments:
        reason = reason + ": " + str(comments)
    updates = dict(f)
    updates.update({
        "entry_date": src.get("entry_date") or date.today().isoformat(),
        "exit_date": date.today().isoformat(),
        "exit_price": round(float(exit_price or 0), 4) if exit_price is not None else None,
        "pnl": round(float(total_pnl or 0), 2),
        "status": "CLOSED",
        "close_reason": close_reason or "manual",
        "exit_reason": reason[:1000],
        "roll_from_id": src.get("id"),
        "current_pnl": None,
        "analytics_json": None,
        "analytics_rec": None,
    })
    return _insert_trade_clone(con, source_row, updates)

@journal_bp.route("/journal/trade/<int:tid>/close", methods=["POST"])
def close_trade(tid):
    """Close a trade fully, partially by quantity, or by selected legs/sides.

    Examples supported:
      - close 3 of 5 contracts and keep 2 open
      - close only the put side of an IC and keep the call side open
      - close selected legs with per-leg exit prices or a net close price
    """
    import json as _json
    d = request.get_json(silent=True) or {}
    con = _conn()
    row = con.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    if not row:
        con.close(); return jsonify({"error": "not found"}), 404
    src = _trade_row_dict(row)
    tt = src.get("trade_type") or ""
    ep = float(src.get("entry_price") or 0)
    qty = _trade_quantity(row)
    try:
        legs = _json.loads(src.get("legs_json") or "[]")
    except Exception:
        legs = []
    close_qty = _safe_close_qty(d.get("close_quantity", d.get("quantity_to_close")), qty)
    close_reason = d.get("close_reason", "manual")
    comments = d.get("comments", "")

    try:
        # Structured leg/spread close path.  Works for full, partial qty and one-side IC closes.
        if legs and (d.get("leg_exits") or d.get("_net_close") or d.get("close_scope") in ("legs", "selected")):
            selected = _selected_indices_from_payload(legs, d, default_all=True)
            if not selected:
                con.close(); return jsonify({"error": "select at least one leg to close"}), 400
            q_arg = close_qty if close_qty < qty else None
            closed_legs, remaining_legs = _split_leg_quantities(legs, selected, q_arg)
            if not closed_legs:
                con.close(); return jsonify({"error": "nothing selected to close"}), 400

            close_net_raw = d.get("exit_price", d.get("close_price", d.get("side_close_price")))
            use_net = bool(d.get("_net_close")) and close_net_raw not in (None, "")
            if use_net:
                entry_net = _entry_net_points_from_legs(closed_legs, _option_qty_from_legs(closed_legs, close_qty)) or 0.0
                close_net = abs(_normalize_option_points(float(close_net_raw or 0)))
                total_pnl = _pnl_from_entry_close_net(entry_net, close_net, _option_qty_from_legs(closed_legs, close_qty))
                exit_display = close_net
            else:
                exit_by_index = _exit_by_index_from_payload(d)
                total_pnl = _pnl_from_leg_exits(legs, selected, exit_by_index, q_arg)
                try:
                    exit_display = sum(float(exit_by_index.get(i, 0)) for i in selected) / max(1, len(selected))
                except Exception:
                    exit_display = None

            # Full close of all legs: close original row in place.
            if not remaining_legs:
                con.execute("""UPDATE trades SET exit_price=?,exit_date=?,pnl=?,status='CLOSED',
                    close_reason=?,exit_reason=? WHERE id=?""",
                    (exit_display or 0, date.today().isoformat(), round(total_pnl, 2),
                     close_reason, comments, tid))
                con.commit(); con.close()
                return jsonify({"ok": True, "pnl": round(total_pnl, 2), "partial": False, "closed_trade_id": tid})

            closed_id = _insert_closed_leg_event(
                con, row, closed_legs, total_pnl, close_reason, comments,
                exit_price=exit_display,
                extra_reason=f"Partial/side close from #{tid}: closed {'/'.join(str(i) for i in sorted(selected))}"
            )
            _update_open_trade_from_legs(con, tid, row, remaining_legs,
                note=f"Remaining position after close event #{closed_id or ''}".strip())
            con.commit(); con.close()
            return jsonify({
                "ok": True,
                "pnl": round(total_pnl, 2),
                "partial": True,
                "closed_qty": close_qty if q_arg is not None else None,
                "remaining_qty": _option_qty_from_legs(remaining_legs, max(1, qty-close_qty)),
                "closed_trade_id": closed_id,
                "open_trade_id": tid,
                "message": "Closed selected quantity/legs; remaining position is still open.",
            })

        # Net/simple spread close path.  Supports partial quantity close even without per-leg exits.
        exit_price = float(d.get("exit_price", 0) or 0)
        if d.get("_net_close"):
            entry_net = None
            try:
                entry_net = _entry_net_points_from_legs(legs, qty) if legs else None
            except Exception:
                entry_net = None
            if entry_net is None:
                try:
                    sent_entry = d.get("_entry_net")
                    if sent_entry is not None:
                        entry_net = _normalize_option_points(float(sent_entry))
                except Exception:
                    entry_net = None
            if entry_net is None:
                try:
                    net_prem = float(src.get("net_premium") or 0)
                    entry_net = _normalize_option_points(net_prem / max(1, qty)) if abs(net_prem) > 1e-9 else None
                except Exception:
                    entry_net = None
            if entry_net is None:
                entry_net = _normalize_option_points(ep)
                if tt not in ("PS", "CS", "IC", "CC", "CP", "Strangle", "Straddle"):
                    entry_net = -abs(entry_net)
            close_pts = _normalize_option_points(exit_price)
            pnl = _pnl_from_entry_close_net(entry_net, close_pts, close_qty)
            if legs and close_qty < qty:
                closed_legs = _scale_legs_to_qty(legs, close_qty, qty)
                remaining_legs = _scale_legs_to_qty(legs, qty - close_qty, qty)
                closed_id = _insert_closed_leg_event(con, row, closed_legs, pnl, close_reason, comments,
                    exit_price=close_pts, extra_reason=f"Partial quantity close {close_qty}/{qty} from #{tid}")
                _update_open_trade_from_legs(con, tid, row, remaining_legs,
                    note=f"Remaining {qty-close_qty}/{qty} after partial close #{closed_id or ''}".strip())
                con.commit(); con.close()
                return jsonify({"ok": True, "pnl": round(pnl, 2), "partial": True,
                                "closed_qty": close_qty, "remaining_qty": qty-close_qty,
                                "closed_trade_id": closed_id, "open_trade_id": tid})
        else:
            if tt == "Stock":
                pnl = (exit_price - ep) * close_qty
            elif tt in ("PS", "CS", "IC"):
                pnl = (ep - exit_price) * close_qty * 100
            else:
                pnl = (exit_price - ep) * close_qty * 100

        if close_qty < qty:
            closed_updates = {
                "quantity": close_qty,
                "exit_price": exit_price,
                "exit_date": date.today().isoformat(),
                "pnl": round(pnl, 2),
                "status": "CLOSED",
                "close_reason": close_reason,
                "exit_reason": (comments or f"Partial quantity close {close_qty}/{qty} from #{tid}")[:1000],
                "roll_from_id": tid,
                "current_pnl": None,
            }
            if legs:
                closed_legs = _scale_legs_to_qty(legs, close_qty, qty)
                remaining_legs = _scale_legs_to_qty(legs, qty - close_qty, qty)
                closed_updates.update(_fields_from_legs(closed_legs, tt, close_qty))
                closed_updates.update({"exit_price": exit_price, "exit_date": date.today().isoformat(), "pnl": round(pnl, 2),
                                       "status": "CLOSED", "close_reason": close_reason, "exit_reason": closed_updates["exit_reason"], "roll_from_id": tid})
                _update_open_trade_from_legs(con, tid, row, remaining_legs,
                    note=f"Remaining {qty-close_qty}/{qty} after partial close")
            else:
                con.execute("UPDATE trades SET quantity=? WHERE id=?", (qty - close_qty, tid))
            closed_id = _insert_trade_clone(con, row, closed_updates)
            con.commit(); con.close()
            return jsonify({"ok": True, "pnl": round(pnl, 2), "partial": True,
                            "closed_qty": close_qty, "remaining_qty": qty - close_qty,
                            "closed_trade_id": closed_id, "open_trade_id": tid})

        con.execute("""UPDATE trades SET exit_price=?,exit_date=?,pnl=?,status='CLOSED',
            close_reason=?,exit_reason=? WHERE id=?""",
            (exit_price, date.today().isoformat(), round(pnl, 2), close_reason, comments, tid))
        con.commit(); con.close()
        return jsonify({"ok": True, "pnl": round(pnl, 2), "partial": False})
    except Exception as e:
        con.rollback(); con.close()
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:1200]}), 500

@journal_bp.route("/journal/trade/<int:tid>/delete", methods=["DELETE"])
def delete_trade(tid):
    con = _conn()
    con.execute("DELETE FROM trades WHERE id=?",(tid,))
    con.commit(); con.close()
    return jsonify({"ok":True})

@journal_bp.route("/journal/trade/<int:tid>/roll", methods=["POST"])
def roll_trade(tid):
    """Roll a whole trade or selected legs/sides into a new expiry/structure.

    IC side-roll behavior:
      - selected tested side is closed and recorded as a CLOSED child row
      - untested side remains open in the original row
      - rolled side opens as a new trade linked via roll_from_id
    """
    import json as _json
    d = request.get_json(silent=True) or {}
    con = _conn()
    orig = con.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    if not orig:
        con.close(); return jsonify({"error": "not found"}), 404
    src = _trade_row_dict(orig)
    try:
        legs = _json.loads(src.get("legs_json") or "[]")
    except Exception:
        legs = []
    qty = _trade_quantity(orig)
    roll_qty = _safe_close_qty(d.get("roll_quantity", d.get("close_quantity")), qty)
    roll_reason = d.get("roll_reason", "rolled")

    try:
        structured = bool(legs and (d.get("new_legs") or d.get("roll_side") or d.get("roll_legs")))
        if structured:
            roll_side = str(d.get("roll_side") or "").lower().strip()
            if d.get("roll_legs"):
                selected = {int(x) for x in (d.get("roll_legs") or [])}
            elif roll_side in ("put", "puts"):
                selected = {i for i, l in enumerate(legs) if str(l.get("option_type", "")).lower() == "put"}
            elif roll_side in ("call", "calls"):
                selected = {i for i, l in enumerate(legs) if str(l.get("option_type", "")).lower() == "call"}
            else:
                selected = set(range(len(legs)))
            if not selected:
                con.close(); return jsonify({"error": "no legs selected for roll"}), 400

            q_arg = roll_qty if roll_qty < qty else None
            closed_legs, remaining_legs = _split_leg_quantities(legs, selected, q_arg)
            if not closed_legs:
                con.close(); return jsonify({"error": "selected roll legs produced no closed quantity"}), 400

            close_net_raw = d.get("side_close_price", d.get("close_price"))
            close_net = None
            if close_net_raw not in (None, ""):
                close_net = abs(_normalize_option_points(float(close_net_raw or 0)))
            exit_by_index = _exit_by_index_from_payload(d)
            side_qty = _option_qty_from_legs(closed_legs, roll_qty)
            old_side_net = _entry_net_points_from_legs(closed_legs, side_qty) or 0.0
            if close_net is not None:
                total_pnl = _pnl_from_entry_close_net(old_side_net, close_net, side_qty)
                close_display = close_net
            else:
                total_pnl = _pnl_from_leg_exits(legs, selected, exit_by_index, q_arg)
                # Also derive close_net from per-leg exit marks for roll-adjustment math.
                close_net = 0.0
                for i, leg in enumerate(legs):
                    if i not in selected:
                        continue
                    sign = 1 if str(leg.get("side", "")).lower() == "sell" else -1
                    q = min(_leg_qty(leg), roll_qty) if q_arg is not None else _leg_qty(leg)
                    close_net += sign * float(exit_by_index.get(i, 0)) * q
                close_net = close_net / max(1, side_qty)
                try:
                    close_display = sum(float(exit_by_index.get(i, 0)) for i in selected) / max(1, len(selected))
                except Exception:
                    close_display = None

            closed_id = _insert_closed_leg_event(
                con, orig, closed_legs, total_pnl, "rolled", roll_reason,
                exit_price=close_display,
                extra_reason=f"Rolled {'/'.join(sorted({str(legs[i].get('option_type','')).upper() for i in selected}))} side from #{tid}"
            )

            if remaining_legs:
                _update_open_trade_from_legs(con, tid, orig, remaining_legs,
                    note=f"Split by roll; closed side event #{closed_id or ''}".strip())
                remaining_open_id = tid
            else:
                con.execute("""UPDATE trades SET exit_price=?,exit_date=?,pnl=?,status='CLOSED',
                    close_reason='rolled',exit_reason=? WHERE id=?""",
                    (close_display or 0, date.today().isoformat(), round(total_pnl, 2), roll_reason, tid))
                remaining_open_id = None

            new_legs = [dict(l or {}) for l in (d.get("new_legs") or [])]
            if not new_legs:
                con.rollback(); con.close(); return jsonify({"error": "new_legs required for structured roll"}), 400
            new_exp = d.get("new_expiry") or next((l.get("expiry") for l in new_legs if l.get("expiry")), "")
            for l in new_legs:
                if new_exp and not l.get("expiry"):
                    l["expiry"] = new_exp
                if not l.get("qty"):
                    l["qty"] = roll_qty

            # Roll premium semantics:
            #   - signed_roll_adjustment / net_roll_credit_debit is the TOTAL roll-order cashflow.
            #     Positive = extra credit collected; negative = debit paid.
            #   - This value is added directly to the old side basis.  It must NOT be reduced
            #     by the close cost again, otherwise a -1.03 roll debit becomes -3.53 when
            #     close cost is 2.50.
            #   - The new trade uses the actual/implied new opening net so realized close P&L
            #     plus new open credit stays mathematically correct.  Effective carry basis is
            #     preserved in the note/API response.
            explicit_roll_cashflow = False
            roll_adjustment = None
            for _rk in ("signed_roll_adjustment", "net_roll_credit_debit", "roll_adjustment_signed"):
                if d.get(_rk) not in (None, ""):
                    try:
                        roll_adjustment = float(d.get(_rk) or 0)
                        explicit_roll_cashflow = True
                        break
                    except Exception:
                        roll_adjustment = None
            new_qty = _option_qty_from_legs(new_legs, roll_qty)
            actual_new_entry_net = _entry_net_points_from_legs(new_legs, new_qty) or 0.0
            if explicit_roll_cashflow or bool(d.get("roll_net_is_cashflow")):
                if roll_adjustment is None:
                    try:
                        roll_adjustment = float(d.get("signed_new_net") or 0)
                    except Exception:
                        roll_adjustment = 0.0
                implied_new_open_net = float(close_net or 0) + float(roll_adjustment or 0)
                # If the user did not provide actual new-leg prices, encode the implied new
                # opening net into the synthetic new legs so the open trade tracks correctly.
                if not bool(d.get("new_leg_prices_provided")):
                    new_legs = _apply_net_premium_to_option_legs(new_legs, implied_new_open_net)
                    new_qty = _option_qty_from_legs(new_legs, roll_qty)
                    actual_new_entry_net = _entry_net_points_from_legs(new_legs, new_qty) or implied_new_open_net
            else:
                implied_new_open_net = actual_new_entry_net
                roll_adjustment = float(actual_new_entry_net or 0) - float(close_net or 0)
            cumulative_basis = float(old_side_net or 0) + float(roll_adjustment or 0)

            new_type = _infer_trade_type_from_legs(new_legs, src.get("trade_type") or "Custom")
            nf = _fields_from_legs(new_legs, new_type, roll_qty)
            basis_note = (f"Rolled from #{tid}; closed side event #{closed_id}; old side basis {old_side_net:.2f}; "
                          f"close side cost/net {float(close_net or 0):.2f}; actual/implied new open net {actual_new_entry_net:+.2f}; "
                          f"net roll cashflow {roll_adjustment:+.2f} (positive=credit, negative=debit); "
                          f"effective position credit {cumulative_basis:+.2f}. {roll_reason}")
            nf.update({
                "entry_date": date.today().isoformat(),
                "symbol": src.get("symbol"),
                "entry_reason": basis_note[:1000],
                "status": "OPEN",
                "exit_price": None,
                "exit_date": None,
                "pnl": None,
                "current_pnl": None,
                "roll_from_id": tid,
                "sector": src.get("sector") or "",
            })
            new_id = _insert_trade_clone(con, orig, nf)
            con.commit(); con.close()
            return jsonify({
                "ok": True,
                "mode": "structured_side_roll",
                "closed_pnl": round(total_pnl, 2),
                "closed_trade_id": closed_id,
                "remaining_open_id": remaining_open_id,
                "new_trade_id": new_id,
                "roll_adjustment": round(roll_adjustment, 2),
                "net_roll_cashflow": round(roll_adjustment, 2),
                "actual_new_entry_net": round(actual_new_entry_net, 2),
                "implied_new_open_net": round(implied_new_open_net, 2),
                "cumulative_side_basis": round(cumulative_basis, 2),
                "effective_position_credit": round(cumulative_basis, 2),
                "message": "Roll split complete: unrolled side remains open; rolled side opened as a new trade.",
            })

        # Legacy whole-trade roll path.
        exit_px = float(d.get("close_price", src.get("entry_price") or 0) or 0)
        tt = src.get("trade_type") or ""
        ep = float(src.get("entry_price") or 0)
        pnl = (ep - exit_px) * qty * 100 if tt in ("PS", "CS", "IC") else (exit_px - ep) * qty * 100
        con.execute("""UPDATE trades SET exit_price=?,exit_date=?,pnl=?,status='CLOSED',
            close_reason='rolled',exit_reason=? WHERE id=?""",
            (exit_px, date.today().isoformat(), round(pnl, 2), roll_reason, tid))
        con.execute("""INSERT INTO trades
            (entry_date,symbol,expiry,trade_type,trade_subtype,long_strike,short_strike,
             entry_price,quantity,entry_reason,sector,risk_amt,reward_amt,status,roll_from_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?)""", (
            date.today().isoformat(), src.get("symbol"), d["new_expiry"],
            src.get("trade_type"), src.get("trade_subtype") or "vertical",
            float(d.get("new_long_strike") or 0), float(d.get("new_short_strike") or 0) or None,
            float(d.get("new_entry_price") or 0), qty,
            f"Rolled from #{tid}: {roll_reason}",
            src.get("sector") or "", float(d.get("risk_amt", 0)), float(d.get("reward_amt", 0)), tid))
        con.commit(); con.close()
        return jsonify({"ok": True, "closed_pnl": round(pnl, 2)})
    except Exception as e:
        con.rollback(); con.close()
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:1200]}), 500

@journal_bp.route("/journal/portfolio_alignment", methods=["GET"])
def portfolio_alignment():
    """
    Check overall portfolio bull/bear exposure vs market sentiment.
    Compares open trade directional bias against SPY/QQQ trend.
    """
    try:
        con = _conn()
        open_trades = [dict(r) for r in con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()]
        con.close()

        # ── Bull/Bear counts ─────────────────────────────────────────────
        bull_trades = [t for t in open_trades if t.get("trade_type") in ("PS","PB","CB")]
        bear_trades = [t for t in open_trades if t.get("trade_type") in ("CS",)]
        ic_trades   = [t for t in open_trades if t.get("trade_type") == "IC"]
        bull_risk   = sum(_trade_max_risk(t)   for t in bull_trades)
        bear_risk   = sum(_trade_max_risk(t)   for t in bear_trades)
        ic_risk     = sum(_trade_max_risk(t)   for t in ic_trades)
        total_risk  = bull_risk + bear_risk + ic_risk
        bull_pct    = round(bull_risk / total_risk * 100) if total_risk else 0
        bear_pct    = round(bear_risk / total_risk * 100) if total_risk else 0
        ic_pct      = round(ic_risk   / total_risk * 100) if total_risk else 0

        if bull_pct > bear_pct + 20:   port_bias = "BULLISH"
        elif bear_pct > bull_pct + 20: port_bias = "BEARISH"
        else:                           port_bias = "NEUTRAL/BALANCED"

        # ── Market sentiment from SPY TA ─────────────────────────────────
        market_bias = "NEUTRAL"; spy_rsi_diff = 0; spy_trend = "SIDEWAYS"
        vix_level = None; vix_signal = "NEUTRAL"
        try:
            import yfinance as yf, math as _m
            # SPY TA
            df_spy = yf.Ticker("SPY").history(period="3mo")
            if not df_spy.empty and len(df_spy) >= 50:
                closes = df_spy["Close"].tolist()
                n = len(closes)-1
                # RSI-14
                def rsi14(c):
                    g=l=0.0
                    for i in range(1,15):
                        d=c[i]-c[i-1]
                        if d>0: g+=d
                        else: l-=d
                    ag,al=g/14,l/14
                    for i in range(15,len(c)):
                        d=c[i]-c[i-1]
                        ag=(ag*13+max(d,0))/14; al=(al*13+max(-d,0))/14
                    return 100 if al==0 else 100-100/(1+ag/al)
                def ema_fn(a,p):
                    k=2/(p+1); o=list(a)
                    for i in range(1,len(o)): o[i]=a[i]*k+o[i-1]*(1-k)
                    return o
                rsi_vals=[50.0]*(len(closes))
                # compute rolling RSI
                for i in range(14, len(closes)):
                    rsi_vals[i] = rsi14(closes[max(0,i-30):i+1])
                ema90_rsi = ema_fn(rsi_vals, 90)
                spy_rsi_diff = round(rsi_vals[-1] - ema90_rsi[-1], 1)
                e20 = ema_fn(closes,20); e50 = ema_fn(closes,50)
                if e20[-1] > e50[-1] and closes[-1] > e20[-1]:
                    spy_trend = "UPTREND"
                elif e20[-1] < e50[-1] and closes[-1] < e20[-1]:
                    spy_trend = "DOWNTREND"
                else:
                    spy_trend = "SIDEWAYS"
                if spy_rsi_diff >= 15:   market_bias = "OVERBOUGHT"
                elif spy_rsi_diff <= -15: market_bias = "OVERSOLD"
                elif spy_trend == "UPTREND":   market_bias = "BULLISH"
                elif spy_trend == "DOWNTREND": market_bias = "BEARISH"
                else:                          market_bias = "NEUTRAL"
            # VIX
            df_vix = yf.Ticker("^VIX").history(period="5d")
            if not df_vix.empty:
                vix_level = round(float(df_vix["Close"].iloc[-1]), 2)
                if vix_level >= 30:    vix_signal = "FEAR — high premium, favour selling"
                elif vix_level >= 20:  vix_signal = "ELEVATED — good for credit spreads"
                elif vix_level >= 15:  vix_signal = "NORMAL"
                else:                  vix_signal = "COMPLACENT — premium thin, be selective"
        except: pass

        # ── Alignment check ───────────────────────────────────────────────
        aligned = True; alignment_issues = []; alignment_suggestions = []

        if port_bias == "BULLISH" and market_bias in ("BEARISH","OVERBOUGHT"):
            aligned = False
            alignment_issues.append(f"Portfolio is {bull_pct}% bullish but market is {market_bias}")
            alignment_suggestions.append("Reduce bull exposure or add bear call spreads as hedge")
            alignment_suggestions.append("Consider rolling bull put spreads to higher strikes or closing weakest positions")

        elif port_bias == "BEARISH" and market_bias in ("BULLISH","OVERSOLD"):
            aligned = False
            alignment_issues.append(f"Portfolio is {bear_pct}% bearish but market is {market_bias}")
            alignment_suggestions.append("Reduce bear exposure or add bull put spreads as hedge")
            alignment_suggestions.append("Consider closing bear positions on next red day")

        elif port_bias == "BULLISH" and market_bias == "NEUTRAL":
            alignment_suggestions.append("Market neutral — consider balancing with a bear call spread")

        if vix_level and vix_level < 15 and total_risk > 0:
            alignment_issues.append(f"VIX={vix_level} (complacent) — premium thin, avoid opening new positions")

        if not alignment_issues:
            if market_bias in ("BULLISH","OVERSOLD") and port_bias in ("BULLISH","NEUTRAL/BALANCED"):
                alignment_suggestions.append("Portfolio aligned with bullish market. Good positioning.")
            elif market_bias in ("BEARISH","OVERBOUGHT") and port_bias in ("BEARISH","NEUTRAL/BALANCED"):
                alignment_suggestions.append("Portfolio aligned with bearish market. Good positioning.")
            else:
                alignment_suggestions.append("Portfolio reasonably aligned with market conditions.")

        # ── Overall status colour ─────────────────────────────────────────
        if aligned and market_bias in ("BULLISH","NEUTRAL"):
            status_color = "#22c55e"; status_label = "ALIGNED"
        elif not aligned:
            status_color = "#ef4444"; status_label = "MISALIGNED — ACTION NEEDED"
        else:
            status_color = "#f59e0b"; status_label = "MONITOR"

        return jsonify({
            "portfolio_bias":   port_bias,
            "bull_pct":         bull_pct, "bear_pct": bear_pct, "ic_pct": ic_pct,
            "bull_risk":        bull_risk, "bear_risk": bear_risk, "ic_risk": ic_risk,
            "market_bias":      market_bias, "spy_trend": spy_trend,
            "spy_rsi_diff":     spy_rsi_diff,
            "vix":              vix_level, "vix_signal": vix_signal,
            "aligned":          aligned,
            "status_label":     status_label, "status_color": status_color,
            "alignment_issues": alignment_issues,
            "suggestions":      alignment_suggestions,
            "open_count":       len(open_trades),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:300]})

@journal_bp.route("/journal/ai/portfolio_review", methods=["GET"])
def ai_portfolio_review():
    """AI coaching summary for open trades."""
    con = _conn()
    open_trades = [dict(r) for r in con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()]
    con.close()
    live_rows = []
    for trade in open_trades:
        try:
            live = _compute_live_pnl(trade)
        except Exception:
            live = {}
        merged = dict(trade)
        merged.update(live or {})
        live_rows.append(merged)
    review = summarize_portfolio_review(live_rows)
    return jsonify(review)

@journal_bp.route("/journal/analytics", methods=["GET"])
def analytics():
    con = _conn()
    trades = [dict(r) for r in con.execute("SELECT * FROM trades WHERE status='CLOSED'").fetchall()]
    con.close()
    if not trades: return jsonify({"error":"no closed trades yet"})
    total=len(trades)
    winners=[t for t in trades if (t.get("pnl") or 0)>0]
    losers =[t for t in trades if (t.get("pnl") or 0)<=0]
    win_rate=round(len(winners)/total*100,1)
    avg_win =round(sum(t["pnl"] for t in winners)/len(winners),2) if winners else 0
    avg_loss=round(sum(t["pnl"] for t in losers)/len(losers),2)   if losers  else 0
    total_pnl=round(sum(t.get("pnl",0) for t in trades),2)
    expectancy=round((win_rate/100)*avg_win+(1-win_rate/100)*avg_loss,2)
    by_type={}
    for t in trades:
        tt=t.get("trade_type","?")
        if tt not in by_type: by_type[tt]={"count":0,"wins":0,"total_pnl":0}
        by_type[tt]["count"]+=1
        if (t.get("pnl") or 0)>0: by_type[tt]["wins"]+=1
        by_type[tt]["total_pnl"]+=(t.get("pnl") or 0)
    for tt in by_type:
        by_type[tt]["win_rate"]=round(by_type[tt]["wins"]/by_type[tt]["count"]*100,1)
        by_type[tt]["avg_pnl"]=round(by_type[tt]["total_pnl"]/by_type[tt]["count"],2)
    by_sym={}
    for t in trades:
        s=t.get("symbol","?")
        if s not in by_sym: by_sym[s]={"count":0,"wins":0,"total_pnl":0}
        by_sym[s]["count"]+=1
        if (t.get("pnl") or 0)>0: by_sym[s]["wins"]+=1
        by_sym[s]["total_pnl"]+=(t.get("pnl") or 0)
    for s in by_sym:
        by_sym[s]["win_rate"]=round(by_sym[s]["wins"]/by_sym[s]["count"]*100,1)
    dte_buckets={"0-7":{"wins":0,"losses":0},"8-21":{"wins":0,"losses":0},
                 "22-45":{"wins":0,"losses":0},"46+":{"wins":0,"losses":0}}
    for t in trades:
        try:
            ed=datetime.strptime(t["entry_date"][:10],"%Y-%m-%d")
            ex=datetime.strptime(t["expiry"][:10],"%Y-%m-%d")
            dte=(ex-ed).days
        except: dte=0
        bucket="0-7" if dte<=7 else "8-21" if dte<=21 else "22-45" if dte<=45 else "46+"
        key="wins" if (t.get("pnl") or 0)>0 else "losses"
        dte_buckets[bucket][key]+=1
    running=0; max_dd=0
    for t in sorted(trades,key=lambda x:x.get("exit_date","")[:10]):
        running+=(t.get("pnl") or 0)
        if running<max_dd: max_dd=running
    working=sorted([k for k in by_type if by_type[k]["win_rate"]>=60],key=lambda k:-by_type[k]["win_rate"])
    not_working=sorted([k for k in by_type if by_type[k]["win_rate"]<40],key=lambda k:by_type[k]["win_rate"])
    return jsonify({"total_trades":total,"win_rate":win_rate,"avg_win":avg_win,"avg_loss":avg_loss,
        "total_pnl":total_pnl,"expectancy":expectancy,"max_drawdown":round(max_dd,2),
        "by_strategy":by_type,"by_symbol":by_sym,"by_dte_bucket":dte_buckets,
        "what_works":working,"what_doesnt":not_working,
        "insight":_generate_insight(win_rate,avg_win,avg_loss,by_type,dte_buckets,working,not_working)})

def _generate_insight(wr,aw,al,by_type,dte,working,not_working):
    lines=[]
    if wr>=65:   lines.append(f"✅ Win rate {wr}% — strong, well above 50% threshold.")
    elif wr>=55: lines.append(f"✅ Win rate {wr}% — solid. 1 winner cancels 2 losers is working.")
    elif wr>=50: lines.append(f"⚠ Win rate {wr}% — break-even. Improve entry selectivity.")
    else:        lines.append(f"❌ Win rate {wr}% — below 50%. Review entry criteria and stop-loss discipline.")
    rr=abs(aw/al) if al and al!=0 else 0
    if rr>=1.8:  lines.append(f"✅ Winners {rr:.1f}× bigger than losers — excellent risk management.")
    elif rr>=1.2:lines.append(f"✅ R:R {rr:.1f}× — positive expectancy maintained.")
    elif rr>=0.9:lines.append(f"⚠ R:R {rr:.1f}× — marginal. Tighten exits on losers.")
    else:        lines.append(f"❌ R:R {rr:.1f}× — losers too large. Apply 50% loss rule strictly.")
    ev=(wr/100*aw+(1-wr/100)*(al or 0))
    lines.append(f"{'✅' if ev>0 else '❌'} Expectancy: ${ev:.2f}/trade.")
    if working:    lines.append(f"📈 Best: {', '.join(working)}.")
    if not_working:lines.append(f"📉 Struggling: {', '.join(not_working)} — review or pause.")
    best_dte=max(dte.keys(),key=lambda k:dte[k]["wins"]/(dte[k]["wins"]+dte[k]["losses"]) if(dte[k]["wins"]+dte[k]["losses"])>0 else 0)
    lines.append(f"⏱ Best DTE: {best_dte}.")
    if wr<60: lines.append("📋 MRT: Take trades on pullbacks. Max 30% capital. 50% loss rule <15 DTE.")
    return " ".join(lines)


@journal_bp.route("/journal/trade/update/<int:tid>", methods=["POST"])
def update_trade(tid):
    import json as _json
    d = request.get_json(force=True) or {}
    legs = d.get("legs", [])
    legs_json = _json.dumps(legs) if legs else None
    net_premium = 0.0
    for leg in legs:
        px = float(leg.get("price") or 0); q = int(leg.get("qty") or 1)
        # Store option premium in points, not dollars.  Dollar P&L is calculated
        # later by multiplying by contract quantity and 100 exactly once.
        mult = 1 if leg.get("option_type")=="stock" else 1
        net_premium += px*q*mult if leg.get("side")=="sell" else -px*q*mult
    stock_leg = next((l for l in legs if l.get("option_type")=="stock"), None)
    option_qty = _option_qty_from_legs(legs, d.get("quantity", 1))
    if stock_leg:
        entry_price = float(stock_leg.get("price", 0))
        quantity = int(stock_leg.get("qty", 1))
    elif legs:
        quantity = max(int(d.get("quantity") or 1), int(option_qty or 1))
        entry_price = abs(net_premium) / max(1, quantity)
    else:
        entry_price = abs(net_premium)
        quantity = int(d.get("quantity", 1))
    con = _conn()
    con.execute("""UPDATE trades SET symbol=?,trade_type=?,expiry=?,entry_date=?,
        entry_price=?,quantity=?,entry_reason=?,legs_json=?,num_legs=?,net_premium=?,
        long_strike=0,short_strike=NULL WHERE id=?""",
        (d.get("symbol"), d.get("trade_type"),
         legs[0].get("expiry","") if legs else d.get("expiry",""),
         d.get("entry_date"), round(float(entry_price), 2), quantity, d.get("entry_reason",""),
         legs_json, len(legs), round(net_premium,2), tid))
    con.commit(); con.close()
    return jsonify({"ok":True,"id":tid,"action":"updated"})

@journal_bp.route("/journal/trade/<int:tid>/edit_pnl", methods=["POST"])
def edit_pnl(tid):
    d = request.get_json()
    pnl = float(d.get("pnl", 0))
    con = _conn()
    con.execute("UPDATE trades SET pnl=? WHERE id=?", (round(pnl, 2), tid))
    con.commit(); con.close()
    return jsonify({"ok": True, "pnl": round(pnl, 2)})

@journal_bp.route("/trade/<int:tid>/live_pnl", methods=["POST"])
def save_live_pnl(tid):
    """Save live P&L data for a trade."""
    import json as _json
    d = request.get_json()
    con = _conn()
    try:
        # Store in analytics_json field (merge with existing)
        row = con.execute("SELECT analytics_json FROM trades WHERE id=?", (tid,)).fetchone()
        existing = {}
        if row and row[0]:
            try: existing = _json.loads(row[0])
            except: pass
        existing['live_pnl'] = d.get('live_pnl')
        existing['live_pnl_pct'] = d.get('live_pnl_pct')
        existing['live_spot'] = d.get('live_spot')
        existing['live_updated'] = d.get('updated') or __import__('datetime').datetime.now().isoformat()
        con.execute("UPDATE trades SET analytics_json=? WHERE id=?",
                    (_json.dumps(existing), tid))
        con.commit()
    finally: con.close()
    return jsonify({"ok": True})

@journal_bp.route("/trade/<int:tid>/live_pnl", methods=["GET"])
def get_live_pnl(tid):
    """Get saved live P&L data for a trade."""
    import json as _json
    con = _conn()
    row = con.execute("SELECT analytics_json FROM trades WHERE id=?", (tid,)).fetchone()
    con.close()
    if row and row[0]:
        try:
            d = _json.loads(row[0])
            return jsonify({k: d.get(k) for k in ['live_pnl','live_pnl_pct','live_spot','live_updated']})
        except: pass
    return jsonify({"live_pnl": None})


# ── Snapshot & Entry Score Routes ───────────────────────────────────────────

@journal_bp.route("/journal/entry_score_preview", methods=["GET"])
def entry_score_preview():
    """
    Called from Add Trade form: returns entry quality score + full market snapshot.
    Query params: symbol, trade_type
    """
    symbol     = (request.args.get("symbol") or "").upper().strip()
    trade_type = (request.args.get("trade_type") or "PS").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    if not _SNAPSHOT_AVAILABLE:
        return jsonify({"error": "snapshot module unavailable"}), 503
    try:
        result = preview_entry_score(symbol, trade_type)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:400]}), 500


@journal_bp.route("/journal/trade/<int:tid>/snapshot", methods=["GET"])
def trade_snapshot(tid):
    """Return the stored entry snapshot for a trade."""
    if not _SNAPSHOT_AVAILABLE:
        return jsonify({"error": "snapshot module unavailable"}), 503
    snap = get_snapshot(tid)
    if not snap:
        return jsonify({"error": "no snapshot", "trade_id": tid}), 404
    return jsonify(snap)


@journal_bp.route("/journal/trade/<int:tid>/snapshot/refresh", methods=["POST"])
def trade_snapshot_refresh(tid):
    """Re-capture entry snapshot (useful if data wasn't available at entry time)."""
    if not _SNAPSHOT_AVAILABLE:
        return jsonify({"error": "snapshot module unavailable"}), 503
    con = _conn()
    row = con.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    con.close()
    if not row:
        return jsonify({"error": "trade not found"}), 404
    try:
        snap = capture_entry_snapshot(tid, row["symbol"], row["trade_type"])
        return jsonify({"ok": True, "snapshot": snap})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@journal_bp.route("/journal/trade/<int:tid>/delta", methods=["GET"])
def trade_delta(tid):
    """
    Return the snapshot delta: Entry vs Current for all market structure metrics.
    Used for the Replay panel in the journal.
    """
    if not _SNAPSHOT_AVAILABLE:
        return jsonify({"error": "snapshot module unavailable"}), 503
    con = _conn()
    row = con.execute("SELECT symbol, trade_type FROM trades WHERE id=?", (tid,)).fetchone()
    con.close()
    if not row:
        return jsonify({"error": "trade not found"}), 404
    try:
        delta = get_snapshot_delta(tid, row["symbol"], row["trade_type"])
        return jsonify(delta)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[:400]}), 500


@journal_bp.route("/journal/trade/<int:tid>/health_alerts", methods=["GET"])
def trade_health_alerts(tid):
    """
    Return health alert status for a trade combining live PnL + structural delta.
    Used by Alert Hub.
    """
    con = _conn()
    row = con.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    con.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    row = _as_dict(row)
    live = _compute_live_pnl(row)
    ai = _build_ai_alert_analysis(row, live)
    alerts = []
    ths = live.get("trade_health_score") or live.get("probability_score") or 0
    prev_score = _safe(row.get("prob_score")) or ths
    score_drop = prev_score - ths if prev_score and ths else 0

    if ths < 20:
        alerts.append({"severity": "critical", "type": "health_critical",
                       "message": f"Trade Health CRITICAL ({ths}/100). Exit recommended."})
    elif ths < 35:
        alerts.append({"severity": "high", "type": "health_exit_candidate",
                       "message": f"Trade Health {ths}/100 — Exit Candidate."})
    elif ths < 50:
        alerts.append({"severity": "medium", "type": "health_warning",
                       "message": f"Trade Health {ths}/100 — Warning."})
    elif ths < 70:
        alerts.append({"severity": "low", "type": "health_watch",
                       "message": f"Trade Health {ths}/100 — Watch."})

    if score_drop >= 20:
        alerts.append({"severity": "high", "type": "score_drop",
                       "message": f"Score dropped {score_drop:.0f} pts since last check."})

    if ai.get("critical_price_alerts"):
        ca = list(ai.get("critical_price_alerts") or [])[:3]
        lines.append("Price alerts:")
        for a in ca:
            level = a.get("level")
            label = a.get("label") or "Level"
            action = a.get("action") or "Monitor"
            reason = a.get("reason") or ""
            lines.append(f"  - {label} @ {level}: {reason} Action: {action}")
    if ai.get("action_mode"):
        lines.append(f"AI plan: {ai.get('action_mode')}" + (f" / {ai.get('roll_side')}" if ai.get('roll_side') else ""))
        if ai.get("roll_expiry"):
            lines.append(f"Roll expiry: {ai.get('roll_expiry')}" + (f" · {ai.get('roll_strategy')}" if ai.get('roll_strategy') else ""))
        if ai.get("action_text"):
            lines.append(f"Plan detail: {ai.get('action_text')}")
    if live.get("pnr_breached"):
        alerts.append({"severity": "critical", "type": "pnr_breach",
                       "message": f"PNR Breached — {live.get('pnr_status','')}."})

    return _jsonify_safe({
        "trade_id": tid, "symbol": row.get("symbol"),
        "trade_type": row.get("trade_type"),
        "pnr_alerts_enabled": _pnr_alerts_enabled(),
        "trade_health_score": ths, "prev_score": prev_score, "score_drop": score_drop,
        "action": live.get("trade_action") or live.get("recommendation"),
        "rec_reason": live.get("rec_reason", ""),
        "alerts": alerts,
        "urgency": "critical" if any(a["severity"] == "critical" for a in alerts)
                   else "high" if any(a["severity"] == "high" for a in alerts)
                   else "medium" if any(a["severity"] == "medium" for a in alerts)
                   else "low",
    })


@journal_bp.route("/journal/health_alerts_all", methods=["GET"])
def health_alerts_all():
    """
    Return ALL open trades with health scores for the Alert Hub.
    Shows every trade regardless of score — healthy ones shown in green.
    """
    con = _conn()
    trades = con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
    con.close()
    results = []
    for t in trades:
        t = _as_dict(t)
        try:
            live = _compute_live_pnl(t)
            ai = _build_ai_alert_analysis(t, live)
            ths  = live.get("trade_health_score") or live.get("probability_score") or 0
            prev = _safe(t.get("prob_score")) or ths
            drop = (prev - ths) if prev and ths else 0
            tt = str(t.get("trade_type", "") or "").upper().strip()
            severity = "healthy"
            if ths < 20:    severity = "critical"
            elif ths < 35:  severity = "exit_candidate"
            elif ths < 50:  severity = "warning"
            elif ths < 70:  severity = "watch"

            # Build strikes string for display using the same resolver as the
            # journal and custom position-alert engine. This covers legacy
            # strike columns, IC columns, and legs_json rows.
            strikes_str = _position_alert_strike_summary(t, live)
            strike_vars = _position_alert_trade_variables(t, live)


            results.append({
                "id": t.get("id"), "symbol": t.get("symbol"),
                "trade_type": tt,
                "strikes": strikes_str,
                "short_strike": strike_vars.get("short_strike"),
                "long_strike": strike_vars.get("long_strike"),
                "sell_strike": strike_vars.get("sell_strike"),
                "buy_strike": strike_vars.get("buy_strike"),
                "put_sell": strike_vars.get("put_sell"),
                "put_buy": strike_vars.get("put_buy"),
                "call_sell": strike_vars.get("call_sell"),
                "call_buy": strike_vars.get("call_buy"),
                "expiry": t.get("expiry",""),
                "qty": _trade_quantity(t),
                "spot": _safe(live.get("spot")),
                "pnr": _safe(live.get("pnr")), "pnr_upper": _safe(live.get("pnr_upper")),
                "pnr_breached": bool(live.get("pnr_breached")),
                "pnr_status": live.get("pnr_status",""),
                "unrealised_pnl": _safe(live.get("unrealised_pnl")),
                "pct_of_max": _safe(live.get("pct_of_max_profit")),
                "trade_health_score": ths, "prev_score": prev, "score_drop": drop,
                "action": live.get("trade_action") or live.get("recommendation"),
                "rec_reason": live.get("rec_reason",""),
                "severity": severity,
                "regime_bias": live.get("regime_bias",""),
                "iv_rank": _safe(live.get("iv_rank")),
                "oi_signal": live.get("oi_signal",""),
                "dte": _safe(live.get("dte"), 0),
                "ai_action_mode": ai.get("action_mode"),
                "ai_action_text": ai.get("action_text") or ai.get("explicit_action"),
                "ai_roll_side": ai.get("roll_side"),
                "ai_roll_expiry": ai.get("roll_expiry"),
                "ai_roll_strategy": ai.get("roll_strategy"),
                "ai_roll_rr": ai.get("roll_rr"),
                "ai_roll_pop": ai.get("roll_pop"),
            })
        except Exception as e:
            # Still include the trade even if live data fetch fails
            tt = t.get("trade_type","")
            try:
                fallback_strikes = _position_alert_strike_summary(t, {})
            except Exception:
                fallback_strikes = "—"
            results.append({
                "id": t.get("id"), "symbol": t.get("symbol"),
                "trade_type": tt, "strikes": fallback_strikes or "—",
                "expiry": t.get("expiry",""),
                "qty": _trade_quantity(t),
                "trade_health_score": 0, "prev_score": 0, "score_drop": 0,
                "severity": "unknown", "action": "—", "rec_reason": f"Error: {str(e)[:50]}",
            })
    results.sort(key=lambda x: x.get("trade_health_score", 0))
    return _jsonify_safe({"alerts": results, "count": len(results)})



def _is_option_trade_for_alert(t: dict) -> bool:
    """True only for option positions; stock/share alerts should not show option strike/DTE lines."""
    tt = str((t or {}).get("trade_type") or "").upper().strip()
    if tt in {"PS", "CS", "PB", "CB", "IC", "CALL", "PUT", "LONG_CALL", "LONG_PUT", "SHORT_CALL", "SHORT_PUT", "CAL", "CALENDAR", "DIAGONAL"}:
        return True
    if tt in {"STOCK", "SHARES", "EQUITY", ""}:
        return False
    try:
        legs = json.loads((t or {}).get("legs_json") or "[]")
        for leg in legs or []:
            opt = str(leg.get("option_type") or leg.get("type") or leg.get("right") or leg.get("put_call") or leg.get("putCall") or "").lower()
            if opt in {"call", "put", "c", "p", "calls", "puts"}:
                return True
    except Exception:
        pass
    return False


def _trade_expiry_summary_for_alert(t: dict, live: dict | None = None) -> str:
    """Return option expiry/DTE text; supports calendars with multiple leg expiries."""
    expiries = []
    def add_exp(x):
        x = str(x or "").strip()[:10]
        if x and x not in expiries:
            expiries.append(x)
    add_exp((t or {}).get("expiry"))
    try:
        legs = json.loads((t or {}).get("legs_json") or "[]")
        for leg in legs or []:
            if str(leg.get("option_type") or leg.get("type") or "").lower() not in {"stock", "shares"}:
                add_exp(leg.get("expiry"))
    except Exception:
        pass
    if not expiries and live and live.get("dte") is not None:
        return f"DTE: {live.get('dte')}d"
    parts = []
    for ex in sorted(expiries):
        dte_txt = ""
        try:
            dte = max(0, (datetime.strptime(ex, "%Y-%m-%d").date() - date.today()).days)
            dte_txt = f" ({dte}d)"
        except Exception:
            pass
        parts.append(f"{ex}{dte_txt}")
    if not parts:
        return ""
    return ("Expiries" if len(parts) > 1 else "Expiry") + ": " + ", ".join(parts)


def _health_alert_option_context_lines(t: dict, live: dict | None = None) -> list:
    """Extra Telegram lines for option trades only: strikes and expiry/DTE."""
    if not _is_option_trade_for_alert(t):
        return []
    lines = []
    try:
        strike_summary = _position_alert_strike_summary(t, live or {})
    except Exception:
        strike_summary = ""
    if strike_summary and strike_summary != "—":
        lines.append(f"Strikes: {strike_summary}")
    exp = _trade_expiry_summary_for_alert(t, live or {})
    if exp:
        lines.append(exp)
    return lines

@journal_bp.route("/journal/trade/<int:tid>/health_alert/telegram", methods=["POST"])
def send_health_alert_telegram(tid):
    """
    Send a Trade Health Alert via Telegram for a specific trade.
    Called manually or by the background watcher when score drops below threshold.
    """
    con = _conn()
    row = con.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    con.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    row = _as_dict(row)
    live = _compute_live_pnl(row)
    ths  = live.get("trade_health_score") or live.get("probability_score") or 0

    # Build Telegram message
    act   = live.get("trade_action") or live.get("recommendation") or "—"
    reason= live.get("rec_reason", "")
    spot  = live.get("spot")
    pnr   = live.get("pnr")
    pnr_u = live.get("pnr_upper")
    pnl   = live.get("unrealised_pnl")
    iv    = live.get("iv_rank")
    regime= live.get("regime_name") or live.get("regime_bias") or ""
    oi_sig= live.get("oi_signal") or ""

    sev   = "🔴 CRITICAL" if ths < 20 else "🟠 EXIT CANDIDATE" if ths < 35 else "🟡 WARNING" if ths < 50 else "⚪ WATCH"

    option_lines = _health_alert_option_context_lines(row, live)
    parts = [
        f"⚠ Trade Health Alert",
        f"",
        f"Symbol: #{row.get('id')} {row.get('symbol','').upper()} ({row.get('trade_type','')})",
        *option_lines,
        f"Health Score: {ths}/100 — {sev}",
        f"",
        f"Action: {act}",
        f"Reason: {reason}" if reason else "",
        f"",
        f"Spot: ${spot:.2f}" if spot else "",
        f"P&L: ${pnl:+.0f}" if pnl is not None else "",
        f"PNR: ${pnr:.2f} ↓" if pnr else "",
        f"PNR: ${pnr_u:.2f} ↑" if pnr_u else "",
        f"",
        f"Signals:",
        f"  Regime: {regime}" if regime else "",
        f"  IV Rank: {iv:.0f}%" if iv else "",
        f"  OI: {oi_sig.replace('_',' ')}" if oi_sig and oi_sig != "NO_DATA" else "",
    ]
    msg = "\n".join(p for p in parts if p is not None)

    try:
        from ..services.telegram_alerts import telegram_configured, send_telegram_message
        if not telegram_configured():
            return jsonify({"ok": False, "error": "Telegram not configured"})
        result = send_telegram_message(msg)
        if result.get("ok"):
            try:
                _save_health_alert_state(
                    row,
                    _health_severity(int(round(float(ths or 0)))),
                    act,
                    int(round(float(ths or 0))),
                    bool(live.get("pnr_breached")),
                    reason="manual health alert sent",
                    sent=True,
                )
            except Exception:
                pass
        return jsonify({"ok": bool(result.get("ok")), "ths": ths,
                        "action": act, "severity": sev,
                        "error": None if result.get("ok") else result.get("description")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@journal_bp.route("/journal/health_alerts_telegram_scan", methods=["POST"])
def health_alerts_telegram_scan():
    """
    Scan open trades and send Telegram alerts only when the persisted
    significant status/action/PNR state has changed from the last alert baseline.
    Score-only drift is recorded but not sent unless explicitly enabled.
    """
    threshold = int(request.json.get("threshold", 50) if request.is_json else 50)
    con = _conn()
    trades = [_as_dict(r) for r in con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()]
    con.close()
    sent = 0; skipped = 0; suppressed = 0; reasons = []
    for t in trades:
        try:
            live = _compute_live_pnl(t)
            ths  = int(round(float(live.get("trade_health_score") or live.get("probability_score") or 0)))
            if ths < threshold:
                result = _send_health_alert_telegram_smart(t, live)
                if result.get("ok"):
                    sent += 1
                elif result.get("suppressed"):
                    suppressed += 1
                    reasons.append({"id": t.get("id"), "symbol": t.get("symbol"), "reason": result.get("reason")})
                else:
                    skipped += 1
                    reasons.append({"id": t.get("id"), "symbol": t.get("symbol"), "reason": result.get("reason")})
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            reasons.append({"id": t.get("id") if isinstance(t, dict) else None, "reason": str(e)[:120]})
    return jsonify({"ok": True, "sent": sent, "skipped": skipped, "suppressed": suppressed, "threshold": threshold, "reasons": reasons[:20]})

# ── Smart Health Alert Telegram (persistent dedup + status/score change) ──

# Severity order: healthy > watch > warning > exit_candidate > critical
_SEVERITY_ORDER = {"healthy": 0, "watch": 1, "warning": 2, "exit_candidate": 3, "critical": 4}
_SEVERITY_LABELS = {"healthy": "✅ Healthy", "watch": "⚪ Watch", "warning": "🟡 Warning",
                    "exit_candidate": "🟠 Exit Candidate", "critical": "🔴 Critical"}

# Runtime cache mirrors the SQLite table so alert state survives app restarts.
_health_alert_state: dict = {}
_health_alert_state_lock = threading.RLock()
_health_alert_state_table_ready = False


def _health_severity(ths: int) -> str:
    if ths < 20:   return "critical"
    if ths < 35:   return "exit_candidate"
    if ths < 50:   return "warning"
    if ths < 70:   return "watch"
    return "healthy"


def _health_alert_score_delta_points() -> int:
    """Minimum score-only movement that should alert.

    Score-only Telegram alerts are disabled by default because minor health-score
    changes are noisy.  A score change is only considered alert-worthy when this
    setting is explicitly set to a positive value via app_settings or the legacy
    HEALTH_ALERT_SCORE_DELTA_POINTS environment variable.
    """
    raw = None
    try:
        from ..scanners.watchlist_manager import _get_setting as _wl_get_setting
        raw = _wl_get_setting("telegram_alert_score_delta_points", None)
    except Exception:
        raw = None
    if raw in (None, ""):
        raw = os.getenv("HEALTH_ALERT_SCORE_DELTA_POINTS", "0")
    try:
        return max(0, int(raw or 0))
    except Exception:
        return 0


def _health_alert_send_on_first_seen() -> bool:
    """Default false so existing open trades are baselined after app restart."""
    return str(os.getenv("HEALTH_ALERT_SEND_ON_FIRST_SEEN", "0")).strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalise_health_action(action) -> str:
    return str(action or "—").strip().upper() or "—"


def _health_alert_signature(severity: str, action: str, score: int, pnr_breached: bool) -> str:
    # Significant health-alert state intentionally excludes raw score.  Telegram
    # should fire when status/action/PNR changes, not when a trade drifts by a
    # few health-score points.  The score is still persisted for display/history.
    return f"{severity}|{_normalise_health_action(action)}|{1 if pnr_breached else 0}"


def _ensure_health_alert_state_table():
    """Create the persistent alert-state table if an older SQLite DB is in use."""
    global _health_alert_state_table_ready
    if _health_alert_state_table_ready:
        return
    with _health_alert_state_lock:
        if _health_alert_state_table_ready:
            return
        con = _conn()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS trade_health_alert_state (
                    trade_id INTEGER PRIMARY KEY,
                    symbol TEXT,
                    trade_type TEXT,
                    severity TEXT,
                    action TEXT,
                    score INTEGER,
                    pnr_breached INTEGER DEFAULT 0,
                    signature TEXT,
                    last_reason TEXT,
                    last_sent_at TEXT,
                    last_observed_at TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_trade_health_alert_state_symbol ON trade_health_alert_state(symbol)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_trade_health_alert_state_updated ON trade_health_alert_state(updated_at)")
            con.commit()
            _health_alert_state_table_ready = True
        finally:
            con.close()


def _row_to_health_alert_state(row) -> dict:
    if not row:
        return {}
    d = dict(row)
    score = int(d.get("score") or 0)
    pnr = bool(d.get("pnr_breached") or 0)
    severity = d.get("severity") or _health_severity(score)
    action = _normalise_health_action(d.get("action"))
    sig = d.get("signature") or _health_alert_signature(severity, action, score, pnr)
    return {
        "trade_id": d.get("trade_id"),
        "symbol": d.get("symbol"),
        "trade_type": d.get("trade_type"),
        "severity": severity,
        "action": action,
        "ths": score,
        "score": score,
        "pnr_breached": pnr,
        "signature": sig,
        "reason": d.get("last_reason") or "",
        "sent_at": d.get("last_sent_at"),
        "last_sent_at": d.get("last_sent_at"),
        "observed_at": d.get("last_observed_at"),
        "last_observed_at": d.get("last_observed_at"),
        "updated_at": d.get("updated_at"),
    }


def _load_health_alert_state(trade_id: int) -> dict:
    if trade_id is None:
        return {}
    tid = int(trade_id)
    with _health_alert_state_lock:
        if tid in _health_alert_state:
            return dict(_health_alert_state.get(tid) or {})
        _ensure_health_alert_state_table()
        con = _conn()
        try:
            row = con.execute("SELECT * FROM trade_health_alert_state WHERE trade_id=?", (tid,)).fetchone()
        finally:
            con.close()
        state = _row_to_health_alert_state(row)
        if state:
            _health_alert_state[tid] = state
        return dict(state)


def _save_health_alert_state(t, severity: str, action: str, score: int, pnr_breached: bool,
                             *, reason: str = "", sent: bool = False) -> dict:
    """Persist the latest alert baseline/last-sent state for one trade."""
    td = _as_dict(t) if isinstance(t, dict) else {"id": t}
    tid = td.get("id") or td.get("trade_id")
    if tid is None:
        return {}
    tid = int(tid)
    score = int(round(float(score or 0)))
    action = _normalise_health_action(action)
    pnr = bool(pnr_breached)
    sig = _health_alert_signature(severity, action, score, pnr)
    now = datetime.now().isoformat(timespec="seconds")
    prev = _load_health_alert_state(tid)
    last_sent_at = now if sent else (prev.get("last_sent_at") or prev.get("sent_at"))
    symbol = (td.get("symbol") or prev.get("symbol") or "").upper()
    trade_type = td.get("trade_type") or prev.get("trade_type") or ""
    state = {
        "trade_id": tid,
        "symbol": symbol,
        "trade_type": trade_type,
        "severity": severity,
        "action": action,
        "ths": score,
        "score": score,
        "pnr_breached": pnr,
        "signature": sig,
        "reason": reason or "",
        "sent_at": last_sent_at,
        "last_sent_at": last_sent_at,
        "observed_at": now,
        "last_observed_at": now,
        "updated_at": now,
    }
    with _health_alert_state_lock:
        _ensure_health_alert_state_table()
        con = _conn()
        try:
            cur = con.execute("""
                UPDATE trade_health_alert_state
                   SET symbol=?, trade_type=?, severity=?, action=?, score=?,
                       pnr_breached=?, signature=?, last_reason=?,
                       last_sent_at=?, last_observed_at=?, updated_at=?
                 WHERE trade_id=?
            """, (symbol, trade_type, severity, action, score, 1 if pnr else 0, sig,
                  reason or "", last_sent_at, now, now, tid))
            if cur.rowcount == 0:
                con.execute("""
                    INSERT INTO trade_health_alert_state
                        (trade_id, symbol, trade_type, severity, action, score, pnr_breached,
                         signature, last_reason, last_sent_at, last_observed_at, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (tid, symbol, trade_type, severity, action, score, 1 if pnr else 0,
                      sig, reason or "", last_sent_at, now, now, now))
            con.commit()
            _health_alert_state[tid] = state
        finally:
            con.close()
    return dict(state)


def _touch_health_alert_observed(trade_id: int):
    """Update only the observed timestamp for unchanged alerts."""
    try:
        _ensure_health_alert_state_table()
        now = datetime.now().isoformat(timespec="seconds")
        con = _conn()
        try:
            con.execute("UPDATE trade_health_alert_state SET last_observed_at=?, updated_at=? WHERE trade_id=?",
                        (now, now, int(trade_id)))
            con.commit()
        finally:
            con.close()
        with _health_alert_state_lock:
            if int(trade_id) in _health_alert_state:
                _health_alert_state[int(trade_id)]["observed_at"] = now
                _health_alert_state[int(trade_id)]["last_observed_at"] = now
                _health_alert_state[int(trade_id)]["updated_at"] = now
    except Exception:
        pass


def _health_action_is_significant(action: str) -> bool:
    """Return True when a recommendation/action deserves Telegram attention."""
    a = _normalise_health_action(action)
    if not a or a in {"—", "HOLD", "WATCH", "MONITOR", "OK"}:
        return False
    significant_words = ("EXIT", "CLOSE", "STOP", "ROLL", "ADJUST", "REDUCE", "HEDGE", "CUT", "DEFEND")
    return any(w in a for w in significant_words)


def _health_severity_is_significant(sev: str) -> bool:
    """Only Warning/Exit/Critical transitions should produce health Telegram alerts."""
    return int(_SEVERITY_ORDER.get(sev or "healthy", 0)) >= int(_SEVERITY_ORDER.get("warning", 2))


def _should_send_health_telegram(trade_id: int, new_sev: str, new_action: str,
                                   new_ths: int, pnr_breached: bool) -> tuple:
    """
    Returns (should_send, reason). State is persisted in SQLite so restarts do
    not resend the same position alert.

    Default behavior is intentionally quiet: Telegram is sent only for a
    significant adverse state transition, such as:
      - PNR becomes breached
      - Health tier worsens into WARNING / EXIT CANDIDATE / CRITICAL
      - Action changes into EXIT / CLOSE / ROLL / ADJUST / REDUCE / STOP

    Pure score movement within the same tier is recorded but not sent. This
    avoids noisy messages like 70 -> 68 or 63 -> 62 while still alerting on
    meaningful trade-management changes.
    """
    new_ths = int(round(float(new_ths or 0)))
    new_action = _normalise_health_action(new_action)
    prev = _load_health_alert_state(trade_id)
    if not prev:
        if _health_alert_send_on_first_seen() and _health_severity_is_significant(new_sev):
            return True, f"Initial significant alert state observed: {_SEVERITY_LABELS.get(new_sev, new_sev)} {new_ths}/100"
        return False, "initial baseline recorded; waiting for significant status/action change"

    prev_sev = prev.get("severity") or "healthy"
    prev_action = _normalise_health_action(prev.get("action"))
    prev_ths = int(prev.get("ths") if prev.get("ths") is not None else prev.get("score") or new_ths)
    prev_pnr = bool(prev.get("pnr_breached"))

    prev_order = int(_SEVERITY_ORDER.get(prev_sev, 0))
    new_order = int(_SEVERITY_ORDER.get(new_sev, 0))

    if bool(pnr_breached) != prev_pnr:
        if pnr_breached:
            return True, "PNR status changed: breached"
        return False, "PNR status changed back inside safe range; baseline updated"

    if new_action != prev_action:
        if _health_action_is_significant(new_action):
            return True, f"Action changed: {prev_action} -> {new_action}"
        return False, f"Action changed to non-critical state: {prev_action} -> {new_action}; baseline updated"

    if new_sev != prev_sev:
        # Alert only on adverse transitions into warning/exit/critical.
        if new_order > prev_order and _health_severity_is_significant(new_sev):
            return True, f"Status worsened: {_SEVERITY_LABELS.get(prev_sev, prev_sev)} -> {_SEVERITY_LABELS.get(new_sev, new_sev)}"
        return False, f"Status changed but not alert-worthy: {_SEVERITY_LABELS.get(prev_sev, prev_sev)} -> {_SEVERITY_LABELS.get(new_sev, new_sev)}; baseline updated"

    delta = new_ths - prev_ths
    if delta:
        # Score-only changes are no longer Telegram-worthy by default. Keep the
        # baseline current so future real state changes compare against the
        # latest observed score.
        return False, f"score-only change ignored: {prev_ths} -> {new_ths}"

    return False, "no significant status/action/PNR change since last alert"

def _send_health_alert_telegram_smart(t: dict, live: dict) -> dict:
    """Send health alert Telegram for a trade if threshold transition warrants it."""
    try:
        from ..services.telegram_alerts import telegram_configured, send_telegram_message
        if not telegram_configured():
            return {"ok": False, "reason": "not configured"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}

    tid    = t.get("id")
    sym    = (t.get("symbol") or "").upper()
    tt     = t.get("trade_type","")
    ths    = int(round(float(live.get("trade_health_score") or live.get("probability_score") or 0)))
    action = _normalise_health_action(live.get("trade_action") or live.get("recommendation") or "—")
    pnr_b  = bool(live.get("pnr_breached"))
    new_sev= _health_severity(ths)

    prev_state = _load_health_alert_state(tid)
    should, reason = _should_send_health_telegram(tid, new_sev, action, ths, pnr_b)
    if not should:
        # Persist the observed baseline/current state even when Telegram is
        # suppressed.  This keeps restarts clean and prevents minor score drift
        # from being treated as a pending alert.
        try:
            _save_health_alert_state(t, new_sev, action, ths, pnr_b, reason=reason, sent=False)
        except Exception:
            if prev_state:
                _touch_health_alert_observed(tid)
        return {"ok": False, "reason": reason or "dedup suppressed", "suppressed": True}

    # Build message
    prev_ths = (prev_state or {}).get("ths", ths)
    spot   = live.get("spot")
    pnl    = live.get("unrealised_pnl")
    pct    = live.get("pct_of_max_profit")
    pnr    = live.get("pnr")
    pnr_u  = live.get("pnr_upper")
    regime = live.get("regime_name") or live.get("regime_bias") or ""
    iv     = live.get("iv_rank")
    oi_sig = (live.get("oi_signal") or "").replace("_"," ")

    option_lines = _health_alert_option_context_lines(t, live)

    sev_emoji = {"critical":"🔴","exit_candidate":"🟠","warning":"🟡","watch":"⚪","healthy":"✅"}.get(new_sev,"⚪")

    lines = [
        f"{sev_emoji} Trade Health Alert — {new_sev.replace('_',' ').upper()}",
        f"",
        f"#{tid} {sym} ({tt})",
        *option_lines,
        f"Health Score: {ths}/100  (was: {prev_ths}/100)",
        f"Action: {action}",
        f"Reason: {reason}",
        f"",
    ]
    if spot:    lines.append(f"Spot: ${spot:.2f}")
    if pnl is not None:
        pnl_str = f"+${pnl:.0f}" if pnl >= 0 else f"-${abs(pnl):.0f}"
        lines.append(f"P&L: {pnl_str}" + (f" ({pct:.0f}% of max)" if pct is not None else ""))
    if pnr or pnr_u:
        pnr_str = f"${pnr:.2f}↓" if pnr else ""
        pnr_u_str = f" / ${pnr_u:.2f}↑" if pnr_u else ""
        lines.append(f"PNR: {pnr_str}{pnr_u_str}{' ⚠ BREACHED' if pnr_b else ''}")
    if regime:  lines.append(f"Regime: {regime}")
    if iv:      lines.append(f"IV Rank: {iv:.0f}%")
    if oi_sig and oi_sig != "NO DATA": lines.append(f"OI Signal: {oi_sig}")
    lines.append(f"")
    lines.append(live.get("rec_reason",""))

    try:
        result = send_telegram_message("\n".join(l for l in lines if l is not None))
        if result.get("ok"):
            # Persist the sent state so app restarts do not resend the same alert.
            _save_health_alert_state(t, new_sev, action, ths, pnr_b, reason=reason, sent=True)
            return {"ok": True, "reason": reason, "severity": new_sev}
        else:
            return {"ok": False, "reason": result.get("description","Telegram failed")}
    except Exception as e:
        return {"ok": False, "reason": str(e)}



# -- AI trade alerts ------------------------------------------------------------
# AI alerts are lightweight and use the already-computed live state so they stay
# fast when the alert watcher scans a lot of open trades.

def _build_ai_alert_analysis(t: dict, live: dict) -> dict:
    strike_summary = _position_alert_strike_summary(t, live)
    enriched_live = dict(live or {})
    if _needs_ai_roll_review(t, enriched_live):
        enriched_live["threat_side"] = _roll_threat_side(t, enriched_live)
        enriched_live["roll_candidates"] = _roll_candidates_for_trade(t, enriched_live)
        if enriched_live.get("roll_candidates"):
            best = enriched_live["roll_candidates"][0]
            enriched_live["roll_expiry"] = best.get("expiry") or ""
            enriched_live["roll_strategy"] = best.get("name") or ""
    return build_trade_alert_analysis(
        (t.get("symbol") or "").upper(),
        t.get("trade_type") or "",
        live=enriched_live,
        trade=t,
        strike_summary=strike_summary,
    )

# These are additional alert rules for open position trades. They evaluate the
# existing journal score plus the AI decision layer so the alerts hub can surface
# actionable changes separately from the normal health/PNR flow.
_AI_ALERT_SEVERITIES = {"watch", "warning", "high", "critical"}
_AI_ALERT_STATE: dict = {}
_AI_ALERT_STATE_LOCK = threading.RLock()
_AI_ALERT_STATE_READY = False


def _ensure_trade_ai_alert_state_table():
    global _AI_ALERT_STATE_READY
    if _AI_ALERT_STATE_READY:
        return
    with _AI_ALERT_STATE_LOCK:
        if _AI_ALERT_STATE_READY:
            return
        con = _conn()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS trade_ai_alert_state (
                    trade_id INTEGER PRIMARY KEY,
                    symbol TEXT,
                    trade_type TEXT,
                    severity TEXT,
                    recommendation TEXT,
                    score INTEGER,
                    confidence REAL,
                    signature TEXT,
                    last_reason TEXT,
                    last_sent_at TEXT,
                    last_observed_at TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_trade_ai_alert_state_symbol ON trade_ai_alert_state(symbol)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_trade_ai_alert_state_updated ON trade_ai_alert_state(updated_at)")
            con.commit()
            _AI_ALERT_STATE_READY = True
        finally:
            con.close()


def _ai_alert_signature(severity: str, recommendation: str, score: int, pnr_breached: bool) -> str:
    return f"{severity}|{_normalise_health_action(recommendation)}|{score // 5}|{1 if pnr_breached else 0}"


def _row_to_trade_ai_alert_state(row) -> dict:
    if not row:
        return {}
    d = dict(row)
    score = int(d.get("score") or 0)
    severity = d.get("severity") or "watch"
    recommendation = _normalise_health_action(d.get("recommendation"))
    return {
        "trade_id": d.get("trade_id"),
        "symbol": d.get("symbol"),
        "trade_type": d.get("trade_type"),
        "severity": severity,
        "recommendation": recommendation,
        "score": score,
        "confidence": _safe(d.get("confidence"), 1),
        "signature": d.get("signature") or _ai_alert_signature(severity, recommendation, score, bool(d.get("pnr_breached") or 0)),
        "reason": d.get("last_reason") or "",
        "sent_at": d.get("last_sent_at"),
        "last_sent_at": d.get("last_sent_at"),
        "observed_at": d.get("last_observed_at"),
        "last_observed_at": d.get("last_observed_at"),
        "updated_at": d.get("updated_at"),
    }


def _load_trade_ai_alert_state(trade_id: int) -> dict:
    if trade_id is None:
        return {}
    tid = int(trade_id)
    with _AI_ALERT_STATE_LOCK:
        if tid in _AI_ALERT_STATE:
            return dict(_AI_ALERT_STATE.get(tid) or {})
        _ensure_trade_ai_alert_state_table()
        con = _conn()
        try:
            row = con.execute("SELECT * FROM trade_ai_alert_state WHERE trade_id=?", (tid,)).fetchone()
        finally:
            con.close()
        state = _row_to_trade_ai_alert_state(row)
        if state:
            _AI_ALERT_STATE[tid] = state
        return dict(state)


def _save_trade_ai_alert_state(t, severity: str, recommendation: str, score: int, confidence: float,
                               *, reason: str = "", sent: bool = False, pnr_breached: bool = False) -> dict:
    td = _as_dict(t) if isinstance(t, dict) else {"id": t}
    tid = td.get("id") or td.get("trade_id")
    if tid is None:
        return {}
    tid = int(tid)
    score = int(round(float(score or 0)))
    confidence = float(confidence or 0)
    recommendation = _normalise_health_action(recommendation)
    sig = _ai_alert_signature(severity, recommendation, score, bool(pnr_breached))
    now = datetime.now().isoformat(timespec="seconds")
    prev = _load_trade_ai_alert_state(tid)
    last_sent_at = now if sent else (prev.get("last_sent_at") or prev.get("sent_at"))
    symbol = (td.get("symbol") or prev.get("symbol") or "").upper()
    trade_type = td.get("trade_type") or prev.get("trade_type") or ""
    state = {
        "trade_id": tid,
        "symbol": symbol,
        "trade_type": trade_type,
        "severity": severity,
        "recommendation": recommendation,
        "score": score,
        "confidence": confidence,
        "signature": sig,
        "reason": reason or "",
        "sent_at": last_sent_at,
        "last_sent_at": last_sent_at,
        "observed_at": now,
        "last_observed_at": now,
        "updated_at": now,
    }
    with _AI_ALERT_STATE_LOCK:
        _ensure_trade_ai_alert_state_table()
        con = _conn()
        try:
            cur = con.execute("""
                UPDATE trade_ai_alert_state
                   SET symbol=?, trade_type=?, severity=?, recommendation=?, score=?, confidence=?,
                       signature=?, last_reason=?, last_sent_at=?, last_observed_at=?, updated_at=?
                 WHERE trade_id=?
            """, (symbol, trade_type, severity, recommendation, score, confidence, sig,
                  reason or "", last_sent_at, now, now, tid))
            if cur.rowcount == 0:
                con.execute("""
                    INSERT INTO trade_ai_alert_state
                        (trade_id, symbol, trade_type, severity, recommendation, score, confidence,
                         signature, last_reason, last_sent_at, last_observed_at, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (tid, symbol, trade_type, severity, recommendation, score, confidence,
                      sig, reason or "", last_sent_at, now, now, now))
            con.commit()
            _AI_ALERT_STATE[tid] = state
        finally:
            con.close()
    return dict(state)


def _ai_alert_state_reason(trade_id: int, severity: str, recommendation: str, score: int, pnr_breached: bool) -> tuple:
    prev = _load_trade_ai_alert_state(trade_id)
    if not prev:
        if score < 50 or recommendation in {"AVOID", "OPEN_SMALL"} or pnr_breached:
            return True, "initial high-attention AI state observed"
        return False, "initial baseline recorded"
    prev_sig = prev.get("signature")
    sig = _ai_alert_signature(severity, recommendation, score, pnr_breached)
    if sig == prev_sig:
        return False, "no meaningful AI change since last alert"
    prev_score = int(prev.get("score") or 0)
    prev_rec = _normalise_health_action(prev.get("recommendation"))
    if pnr_breached and not bool(prev.get("pnr_breached")):
        return True, "PNR breach now part of AI view"
    if recommendation in {"AVOID", "OPEN_SMALL"} and prev_rec not in {"AVOID", "OPEN_SMALL"}:
        return True, f"AI recommendation tightened: {prev_rec} -> {recommendation}"
    if score <= 50 and prev_score > 50:
        return True, f"AI score weakened: {prev_score} -> {score}"
    if severity in {"critical", "high"} and prev.get("severity") not in {"critical", "high"}:
        return True, f"AI severity worsened: {prev.get('severity') or 'watch'} -> {severity}"
    return False, "AI state changed but not alert-worthy"


def _ai_alert_severity(ai: dict, live: dict | None = None) -> str:
    live = live or {}
    score = int(round(float(live.get("trade_health_score") or live.get("probability_score") or ai.get("score") or 0)))
    rec = _normalise_health_action(ai.get("recommendation") or live.get("recommendation") or live.get("trade_action") or "HOLD")
    pnr_breached = bool(live.get("pnr_breached"))
    if score < 35 or (rec == "AVOID" and pnr_breached):
        return "critical"
    if score < 50 or rec in {"AVOID", "OPEN_SMALL"} or pnr_breached:
        return "high"
    if score < 65:
        return "warning"
    return "watch"


def _send_trade_ai_telegram(t, live: dict, ai: dict, *, prefix: str = "🤖 AI trade alert") -> dict:
    try:
        from ..services.telegram_alerts import telegram_configured, send_telegram_message
    except Exception as e:
        return {"configured": False, "sent": 0, "error": f"telegram service unavailable: {e}"}

    if not telegram_configured():
        return {"configured": False, "sent": 0, "error": "Telegram credentials not configured"}

    score = int(round(float(live.get("trade_health_score") or live.get("probability_score") or ai.get("score") or 0)))
    confidence = ai.get("confidence")
    lines = [
        prefix,
        f"Trade: #{t.get('id')} {(t.get('symbol') or '').upper()} ({t.get('trade_type') or '—'})",
        f"Strikes: {ai.get('strike_summary') or _position_alert_strike_summary(t, live) or '—'}",
        f"AI Score: {score}/100" + (f"  | Confidence: {float(confidence):.1f}%" if confidence is not None else ""),
        f"Recommendation: {ai.get('recommendation') or live.get('recommendation') or live.get('trade_action') or 'HOLD'}",
        f"Action: {ai.get('explicit_action') or ai.get('summary') or live.get('rec_reason') or live.get('action_reason') or 'Monitor the trade.'}",
    ]
    if ai.get("headline"):
        lines.append(f"Headline: {ai.get('headline')}")
    if ai.get("thesis"):
        lines.append("Thesis: " + " | ".join(str(x) for x in list(ai.get("thesis") or [])[:4]))
    if ai.get("risks"):
        lines.append("Risks: " + " | ".join(str(x) for x in list(ai.get("risks") or [])[:4]))
    if ai.get("action_steps"):
        lines.append("Next: " + " | ".join(str(x) for x in list(ai.get("action_steps") or [])[:3]))
    if ai.get("missing_inputs"):
        lines.append("Missing: " + ", ".join(str(x) for x in list(ai.get("missing_inputs") or [])[:4]))
    if ai.get("critical_price_alerts"):
        ca = list(ai.get("critical_price_alerts") or [])[:3]
        lines.append("Price alerts:")
        for a in ca:
            level = a.get("level")
            label = a.get("label") or "Level"
            action = a.get("action") or "Monitor"
            reason = a.get("reason") or ""
            lines.append(f"  - {label} @ {level}: {reason} Action: {action}")
    if live.get("pnr_breached"):
        lines.append(f"PNR: BREACHED ({live.get('pnr_status') or 'review immediately'})")
    elif live.get("pnr_status"):
        lines.append(f"PNR: {live.get('pnr_status')}")
    if live.get("action_reason") or live.get("rec_reason"):
        lines.append(str(live.get("action_reason") or live.get("rec_reason")))
    try:
        result = send_telegram_message("\n".join(lines))
        if result.get("ok"):
            sev = _ai_alert_severity(ai, live)
            try:
                log_alert_notification(
                    "AI_TRADE_ALERT",
                    f"{(t.get('symbol') or '').upper()} AI review",
                    ai.get("headline") or ai.get("summary") or "AI trade review",
                    symbol=(t.get("symbol") or "").upper(),
                    trade_id=t.get("id"),
                    severity="warn" if sev in {"warning", "high"} else sev,
                    source="journal_ai",
                    scanner_name="AI Trade Analyst",
                    metadata={
                        "trade_id": t.get("id"),
                        "symbol": t.get("symbol"),
                        "trade_type": t.get("trade_type"),
                        "score": score,
                        "confidence": confidence,
                        "recommendation": ai.get("recommendation") or live.get("recommendation") or live.get("trade_action") or "HOLD",
                        "severity": sev,
                    },
                )
            except Exception:
                pass
            return {"ok": True, "severity": sev}
        return {"ok": False, "error": result.get("description", "Telegram failed")}
    except Exception as e:
        return {"ok": False, "error": str(e)}



def _scan_ai_alerts_once(*, detailed: bool = False, send: bool = True) -> dict:
    if not _ai_alerts_enabled():
        return {"ok": True, "enabled": False, "checked": 0, "sent": 0, "suppressed": 0, "skipped": 0, "alerts": [] if detailed else None}
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
    finally:
        con.close()
    stats = {"ok": True, "enabled": True, "checked": 0, "sent": 0, "suppressed": 0, "skipped": 0, "alerts": [], "reasons": []}
    for row in rows:
        try:
            stats["checked"] += 1
            t = _as_dict(row)
            live = _compute_live_pnl(t)
            sym = (t.get("symbol") or "").upper()
            tt = t.get("trade_type") or ""
            ai = _build_ai_alert_analysis(t, live)
            score = int(round(float(live.get("trade_health_score") or live.get("probability_score") or ai.get("score") or 0)))
            confidence = float(ai.get("confidence") or 0)
            severity = _ai_alert_severity(ai, live)
            rec = _normalise_health_action(ai.get("recommendation") or live.get("recommendation") or live.get("trade_action") or "HOLD")
            pnr_breached = bool(live.get("pnr_breached"))
            should, reason = _ai_alert_state_reason(int(t.get("id")), severity, rec, score, pnr_breached)
            if not should:
                _save_trade_ai_alert_state(t, severity, rec, score, confidence, reason=reason, sent=False, pnr_breached=pnr_breached)
                stats["suppressed"] += 1
                if detailed:
                    stats["reasons"].append({"id": t.get("id"), "symbol": sym, "reason": reason})
                continue
            if send:
                res = _send_trade_ai_telegram(t, live, ai)
                if res.get("ok"):
                    stats["sent"] += 1
                    _save_trade_ai_alert_state(t, severity, rec, score, confidence, reason=reason, sent=True, pnr_breached=pnr_breached)
                else:
                    stats["skipped"] += 1
                    _save_trade_ai_alert_state(t, severity, rec, score, confidence, reason=res.get("error") or reason, sent=False, pnr_breached=pnr_breached)
                    if detailed:
                        stats["reasons"].append({"id": t.get("id"), "symbol": sym, "reason": res.get("error") or reason})
            if detailed:
                stats["alerts"].append({
                    "id": t.get("id"),
                    "symbol": sym,
                    "trade_type": tt,
                    "score": score,
                    "confidence": confidence,
                    "recommendation": rec,
                    "severity": severity,
                    "headline": ai.get("headline"),
                    "summary": ai.get("summary"),
                    "thesis": ai.get("thesis", []),
                    "risks": ai.get("risks", []),
                    "next_actions": ai.get("next_actions", []),
                    "missing_inputs": ai.get("missing_inputs", []),
                    "pnr_breached": pnr_breached,
                    "pnr_status": live.get("pnr_status"),
                })
        except Exception as e:
            stats["skipped"] += 1
            if detailed:
                stats["reasons"].append({"id": row["id"] if hasattr(row, "keys") and "id" in row.keys() else None, "reason": str(e)[:160]})
    if not detailed:
        stats.pop("alerts", None)
        stats.pop("reasons", None)
    else:
        stats["alerts"] = stats["alerts"][:50]
        stats["reasons"] = stats["reasons"][:50]
    return stats


@journal_bp.route("/journal/ai_alerts_all", methods=["GET"])
def ai_alerts_all():
    if not _ai_alerts_enabled():
        return _jsonify_safe({"alerts": [], "count": 0, "enabled": False, "note": "AI alerts are disabled."})
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
    finally:
        con.close()
    results = []
    for row in rows:
        try:
            t = _as_dict(row)
            live = _compute_live_pnl(t)
            ai = _build_ai_alert_analysis(t, live)
            score = int(round(float(live.get("trade_health_score") or live.get("probability_score") or ai.get("score") or 0)))
            severity = _ai_alert_severity(ai, live)
            results.append({
                "id": t.get("id"),
                "symbol": (t.get("symbol") or "").upper(),
                "trade_type": t.get("trade_type") or "",
                "score": score,
                "confidence": ai.get("confidence"),
                "recommendation": ai.get("recommendation") or live.get("recommendation") or live.get("trade_action") or "HOLD",
                "severity": severity,
                "headline": ai.get("headline"),
                "summary": ai.get("summary"),
                "thesis": ai.get("thesis", []),
                "risks": ai.get("risks", []),
                "next_actions": ai.get("next_actions", ai.get("action_steps", [])),
                "missing_inputs": ai.get("missing_inputs", []),
                "strike_summary": ai.get("strike_summary"),
                "explicit_action": ai.get("explicit_action"),
                "decision_phrase": ai.get("decision_phrase"),
                "action_mode": ai.get("action_mode"),
                "action_text": ai.get("action_text") or ai.get("explicit_action"),
                "roll_side": ai.get("roll_side"),
                "roll_expiry": ai.get("roll_expiry"),
                "roll_strategy": ai.get("roll_strategy"),
                "roll_rr": ai.get("roll_rr"),
                "roll_pop": ai.get("roll_pop"),
                "roll_candidates": ai.get("roll_candidates", []),
                "decision_reasons": ai.get("decision_reasons", []),
                "watch_items": ai.get("watch_items", []),
                "critical_price_alerts": ai.get("critical_price_alerts", []),
                "pnr_breached": bool(live.get("pnr_breached")),
                "pnr_status": live.get("pnr_status", ""),
                "spot": _safe(live.get("spot")),
                "dte": _safe(live.get("dte"), 0),
                "unrealised_pnl": _safe(live.get("unrealised_pnl")),
            })
        except Exception as e:
            results.append({"id": t.get("id") if isinstance(t, dict) else None, "symbol": t.get("symbol") if isinstance(t, dict) else None, "error": str(e)[:120]})
    results.sort(key=lambda x: x.get("score", 0))
    return _jsonify_safe({"alerts": results, "count": len(results), "enabled": True})


@journal_bp.route("/journal/ai_alert/manual_scan", methods=["POST"])
def ai_alert_manual_scan():
    return _jsonify_safe(_scan_ai_alerts_once(detailed=True, send=True))


# -- Custom position scanner alerts -------------------------------------------------
# These are additional alert rules for open position trades. They do not replace the
# existing health/PNR alerts. Each rule can define a condition for bullish positions
# (PS/CB), bearish positions (CS/PB), and neutral positions (IC). Results are capped
# at one Telegram per trade + rule + day to avoid refresh duplicates.
_POSITION_ALERT_SEVERITIES = {"info", "watch", "warning", "critical"}


def _ensure_position_alert_rule_tables():
    con = _conn()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS trade_position_alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                severity TEXT DEFAULT 'warning',
                benchmark TEXT DEFAULT 'SPY',
                bull_condition TEXT DEFAULT '',
                bear_condition TEXT DEFAULT '',
                neutral_condition TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS trade_position_alert_state (
                trade_id INTEGER NOT NULL,
                rule_id INTEGER NOT NULL,
                symbol TEXT,
                trade_type TEXT,
                side TEXT,
                last_match INTEGER DEFAULT 0,
                last_trigger_date TEXT,
                last_signature TEXT,
                last_reason TEXT,
                last_sent_at TEXT,
                last_observed_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (trade_id, rule_id)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_trade_position_alert_rules_enabled ON trade_position_alert_rules(enabled)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_trade_position_alert_state_symbol ON trade_position_alert_state(symbol)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_trade_position_alert_state_sent ON trade_position_alert_state(last_sent_at)")
        con.commit()
    finally:
        con.close()


def _trade_position_side(t: dict) -> str:
    tt = str((t or {}).get("trade_type") or "").upper().strip()
    if tt in {"PS", "CB", "CALL", "LONG_CALL"}:
        return "bull"
    if tt in {"CS", "PB", "PUT", "LONG_PUT"}:
        return "bear"
    if tt == "IC":
        return "neutral"
    outlook = str((t or {}).get("outlook") or "").upper()
    if "BULL" in outlook:
        return "bull"
    if "BEAR" in outlook:
        return "bear"
    return "neutral"


def _position_alert_expr_for_trade(rule: dict, trade: dict) -> tuple[str, str]:
    side = _trade_position_side(trade)
    if side == "bull":
        expr = rule.get("bull_condition") or ""
    elif side == "bear":
        expr = rule.get("bear_condition") or ""
    else:
        expr = rule.get("neutral_condition") or ""
    return side, str(expr or "").strip()


def _normalise_position_alert_rule_payload(d: dict) -> dict:
    name = str(d.get("name") or "").strip()
    if not name:
        raise ValueError("rule name is required")
    severity = str(d.get("severity") or "warning").lower().strip()
    if severity not in _POSITION_ALERT_SEVERITIES:
        severity = "warning"
    return {
        "name": name,
        "enabled": 1 if bool(d.get("enabled", True)) else 0,
        "severity": severity,
        "benchmark": str(d.get("benchmark") or "SPY").strip().upper() or "SPY",
        "bull_condition": str(d.get("bull_condition") or "").strip(),
        "bear_condition": str(d.get("bear_condition") or "").strip(),
        "neutral_condition": str(d.get("neutral_condition") or "").strip(),
        "notes": str(d.get("notes") or "").strip(),
    }


def _load_position_alert_rules(enabled_only: bool = False) -> list[dict]:
    _ensure_position_alert_rule_tables()
    if not _custom_position_alerts_enabled():
        return {"ok": True, "enabled": False, "rules": 0, "checked": 0, "matched": 0, "sent": 0, "suppressed": 0, "skipped": 0, "reasons": [] if detailed else None}
    con = _conn()
    try:
        where = "WHERE enabled=1" if enabled_only else ""
        rows = con.execute(f"""
            SELECT id, name, enabled, severity, benchmark, bull_condition, bear_condition,
                   neutral_condition, notes, created_at, updated_at
            FROM trade_position_alert_rules
            {where}
            ORDER BY enabled DESC, lower(name)
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _position_alert_state(trade_id: int, rule_id: int) -> dict:
    _ensure_position_alert_rule_tables()
    con = _conn()
    try:
        row = con.execute(
            "SELECT * FROM trade_position_alert_state WHERE trade_id=? AND rule_id=?",
            (int(trade_id), int(rule_id)),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def _save_position_alert_state(trade: dict, rule: dict, *, side: str, matched: bool,
                               reason: str = "", sent: bool = False, signature: str = "") -> dict:
    _ensure_position_alert_rule_tables()
    tid = int(trade.get("id") or trade.get("trade_id"))
    rid = int(rule.get("id"))
    now = datetime.now().isoformat(timespec="seconds")
    today = date.today().isoformat()
    prev = _position_alert_state(tid, rid)
    last_trigger_date = today if sent else prev.get("last_trigger_date")
    last_sent_at = now if sent else prev.get("last_sent_at")
    symbol = str(trade.get("symbol") or prev.get("symbol") or "").upper()
    trade_type = str(trade.get("trade_type") or prev.get("trade_type") or "")
    state = {
        "trade_id": tid,
        "rule_id": rid,
        "symbol": symbol,
        "trade_type": trade_type,
        "side": side,
        "last_match": 1 if matched else 0,
        "last_trigger_date": last_trigger_date,
        "last_signature": signature or prev.get("last_signature") or "",
        "last_reason": reason or "",
        "last_sent_at": last_sent_at,
        "last_observed_at": now,
        "updated_at": now,
    }
    con = _conn()
    try:
        cur = con.execute("""
            UPDATE trade_position_alert_state
               SET symbol=?, trade_type=?, side=?, last_match=?, last_trigger_date=?,
                   last_signature=?, last_reason=?, last_sent_at=?, last_observed_at=?, updated_at=?
             WHERE trade_id=? AND rule_id=?
        """, (symbol, trade_type, side, 1 if matched else 0, last_trigger_date,
              signature or prev.get("last_signature") or "", reason or "", last_sent_at,
              now, now, tid, rid))
        if cur.rowcount == 0:
            con.execute("""
                INSERT INTO trade_position_alert_state
                    (trade_id, rule_id, symbol, trade_type, side, last_match, last_trigger_date,
                     last_signature, last_reason, last_sent_at, last_observed_at, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (tid, rid, symbol, trade_type, side, 1 if matched else 0, last_trigger_date,
                  signature or "", reason or "", last_sent_at, now, now, now))
        con.commit()
    finally:
        con.close()
    return state



def _position_alert_trade_variables(trade: dict, live: dict | None = None) -> dict:
    """Expose safe trade/strike variables to custom position-alert expressions.

    These values are numeric so a rule can compare market primitives to the
    actual position structure, for example:
      close[1d] < short_strike
      Between(spot, long_strike, short_strike)
      distance_pct_to_short <= 1
    """
    t = dict(trade or {})
    live = dict(live or {})
    resolved = _resolve_trade_strikes(t)
    tt = str(t.get("trade_type") or "").upper().strip()
    qty = _trade_quantity(t)
    spot = _safe(live.get("spot")) or _safe(t.get("current_price"))
    buy = _safe(resolved.get("buy"))
    sell = _safe(resolved.get("sell"))
    put_buy = _safe(resolved.get("put_buy"))
    put_sell = _safe(resolved.get("put_sell"))
    call_buy = _safe(resolved.get("call_buy"))
    call_sell = _safe(resolved.get("call_sell"))

    short_strike = sell
    long_strike = buy
    if tt == "IC":
        # For IC, keep both sides explicit.  short_strike/long_strike are left
        # as None to avoid ambiguous one-sided comparisons.
        short_strike = None
        long_strike = None

    entry_net = _safe(_trade_net_premium(t))
    if qty and entry_net is not None and abs(entry_net) > 50:
        # Defensive normalization in case old rows stored dollar premium.
        entry_net = _normalize_option_points(entry_net)
    entry_price = _safe(t.get("entry_price"))
    dte = live.get("dte")
    if dte is None:
        expiry = str(t.get("expiry") or "").strip()
        try:
            dte = max(0, (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days) if expiry else None
        except Exception:
            dte = None
    pnl = _safe(live.get("unrealised_pnl"))
    max_loss = _safe(live.get("max_loss")) or _safe(_trade_max_risk(t))
    max_profit = _safe(live.get("max_profit")) or _safe(_trade_max_reward(t))
    pnl_pct = None
    try:
        if pnl is not None and max_loss:
            pnl_pct = round(float(pnl) / float(max_loss) * 100.0, 4)
    except Exception:
        pnl_pct = None

    breakeven = lower_be = upper_be = None
    try:
        net_per = float(entry_net or 0) / max(1, int(qty or 1))
        # Credit spreads: BE is short strike +/- credit. Debit spreads: long +/- debit.
        if tt == "PS" and put_sell is not None:
            breakeven = put_sell - abs(net_per)
        elif tt == "CS" and call_sell is not None:
            breakeven = call_sell + abs(net_per)
        elif tt == "CB" and call_buy is not None:
            breakeven = call_buy + abs(net_per)
        elif tt == "PB" and put_buy is not None:
            breakeven = put_buy - abs(net_per)
        elif tt == "IC":
            total_credit = abs(net_per)
            if put_sell is not None:
                lower_be = put_sell - total_credit
            if call_sell is not None:
                upper_be = call_sell + total_credit
    except Exception:
        pass

    def dist_to(x):
        try:
            if spot is None or x is None:
                return None, None
            d = round(float(spot) - float(x), 4)
            pct = round(d / float(x) * 100.0, 4) if float(x) else None
            return d, pct
        except Exception:
            return None, None

    d_short, dp_short = dist_to(short_strike)
    d_long, dp_long = dist_to(long_strike)
    d_pnr, dp_pnr = dist_to(live.get("pnr"))

    out = {
        "spot": spot,
        "underlying": spot,
        "short_strike": short_strike,
        "long_strike": long_strike,
        "sell_strike": sell,
        "buy_strike": buy,
        "put_sell": put_sell,
        "put_buy": put_buy,
        "call_sell": call_sell,
        "call_buy": call_buy,
        "short_put": put_sell,
        "long_put": put_buy,
        "short_call": call_sell,
        "long_call": call_buy,
        "breakeven": breakeven,
        "lower_breakeven": lower_be,
        "upper_breakeven": upper_be,
        "breakeven_lower": lower_be,
        "breakeven_upper": upper_be,
        "dte": _safe(dte, 0),
        "days_to_expiry": _safe(dte, 0),
        "entry_price": entry_price,
        "entry_net": entry_net,
        "net_premium": entry_net,
        "quantity": qty,
        "qty": qty,
        "pnl": pnl,
        "unrealised_pnl": pnl,
        "unrealized_pnl": pnl,
        "pnl_pct": pnl_pct,
        "max_loss": max_loss,
        "max_profit": max_profit,
        "risk": max_loss,
        "reward": max_profit,
        "pnr": _safe(live.get("pnr")),
        "pnr_upper": _safe(live.get("pnr_upper")),
        "distance_to_short": d_short,
        "distance_pct_to_short": dp_short,
        "distance_to_long": d_long,
        "distance_pct_to_long": dp_long,
        "distance_to_pnr": d_pnr,
        "distance_pct_to_pnr": dp_pnr,
    }
    return {k: v for k, v in out.items() if v is not None}


def _position_alert_strike_summary(trade: dict, live: dict | None = None) -> str:
    vals = _position_alert_trade_variables(trade, live or {})
    tt = str((trade or {}).get("trade_type") or "").upper().strip()

    def _fmt_strike(x):
        try:
            f = float(x)
            return str(int(f)) if abs(f - int(f)) < 1e-9 else f"{f:g}"
        except Exception:
            return "—"

    if tt == "IC":
        parts = []
        ps, pb = vals.get("put_sell"), vals.get("put_buy")
        cs, cb = vals.get("call_sell"), vals.get("call_buy")
        if ps is not None or pb is not None:
            parts.append(f"P S{_fmt_strike(ps)}/B{_fmt_strike(pb)}")
        if cs is not None or cb is not None:
            parts.append(f"C S{_fmt_strike(cs)}/B{_fmt_strike(cb)}")
        return " · ".join(parts) if parts else "—"

    side = "P" if tt in {"PS", "PB"} else "C" if tt in {"CS", "CB"} else ""
    buy = vals.get("buy_strike")
    sell = vals.get("sell_strike")
    if buy is None and sell is None:
        return "—"
    return f"{side} S{_fmt_strike(sell)}/B{_fmt_strike(buy)}".strip()


def _option_trade_alert_lines(trade: dict, live: dict | None = None) -> list[str]:
    """Return strike/expiry detail lines for option trades only.

    Stock-only trades should not display option metadata.  Option trades may
    have legacy strike columns or legs_json; calendars can have multiple row
    expiries, so summarize unique expiries with DTE where possible.
    """
    t = trade or {}
    live = live or {}
    tt = str(_row_value(t, "trade_type", "") or "").upper().strip()
    try:
        legs = json.loads(_row_value(t, "legs_json", "") or "[]")
    except Exception:
        legs = []
    has_option_leg = any(str((leg or {}).get("option_type") or (leg or {}).get("type") or "").lower() in {"call", "put", "c", "p"} for leg in legs or [])
    # Existing position alerts are option-focused.  Still guard stock rows.
    if tt in {"STOCK", "SHARES", "EQUITY"} and not has_option_leg:
        return []
    strike_summary = _position_alert_strike_summary(t, live)
    has_strikes = bool(strike_summary and strike_summary != "—")
    expiry_values = []
    for leg in legs or []:
        try:
            opt = str(leg.get("option_type") or leg.get("type") or leg.get("right") or "").lower()
            if opt in {"stock", "share", "shares"}:
                continue
            exp = str(leg.get("expiry") or leg.get("expiration") or "").strip()
            if exp and exp not in expiry_values:
                expiry_values.append(exp)
        except Exception:
            continue
    row_exp = str(_row_value(t, "expiry", "") or "").strip()
    if row_exp and row_exp not in expiry_values:
        expiry_values.append(row_exp)
    # If no strikes and no expiry, there is nothing option-specific to add.
    if not has_strikes and not expiry_values and not has_option_leg:
        return []

    def _fmt_exp(exp):
        try:
            d = datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
            dte = max(0, (d - date.today()).days)
            return f"{d.isoformat()} ({dte}d)"
        except Exception:
            return str(exp)

    lines = []
    if has_strikes:
        lines.append(f"Strikes: {strike_summary}")
    if expiry_values:
        label = "Expiries" if len(expiry_values) > 1 else "Expiry"
        lines.append(f"{label}: {', '.join(_fmt_exp(e) for e in expiry_values[:4])}")
    elif live.get("dte") is not None:
        lines.append(f"DTE: {live.get('dte')}d")
    return lines

def _eval_position_alert_condition(symbol: str, condition_text: str, benchmark: str = "SPY",
                                   ctx_cache: dict | None = None, trade: dict | None = None,
                                   live: dict | None = None) -> dict:
    condition_text = str(condition_text or "").strip()
    if not condition_text:
        return {"ok": False, "reason": ["blank condition"], "timeframes": []}
    from ..scanners.scanner_builder import _parse_query, _expand_scan_nodes, _required_timeframes, _symbol_ctx, _eval, _explain
    raw_root = _parse_query(condition_text)
    root = _expand_scan_nodes(raw_root, ())
    req_tfs = _required_timeframes(root)
    key = (str(symbol).upper(), str(benchmark or "SPY").upper(), tuple(req_tfs))
    if ctx_cache is not None and key in ctx_cache:
        ctx = dict(ctx_cache[key])
    else:
        ctx = _symbol_ctx(str(symbol).upper(), str(benchmark or "SPY").upper(), req_tfs)
        if ctx_cache is not None:
            ctx_cache[key] = dict(ctx)
    if trade is not None:
        ctx.update(_position_alert_trade_variables(trade, live or {}))
    ok = bool(_eval(root, ctx, shift=0, tf_default="1d"))
    reason = _explain(root, ctx, shift=0, tf_default="1d") if ok else []
    return {"ok": ok, "reason": reason, "timeframes": req_tfs, "ctx": ctx}


def _position_alert_severity_label(sev: str) -> tuple[str, str]:
    s = str(sev or "warning").lower()
    if s == "critical": return "🔴", "CRITICAL"
    if s == "watch": return "⚪", "WATCH"
    if s == "info": return "🔵", "INFO"
    return "🟡", "WARNING"


def _send_position_alert_telegram(trade: dict, live: dict, rule: dict, *, side: str, condition_text: str, eval_result: dict) -> dict:
    try:
        from ..services.telegram_alerts import telegram_configured, send_telegram_message
        if not telegram_configured():
            return {"ok": False, "reason": "Telegram not configured"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    tid = trade.get("id")
    sym = str(trade.get("symbol") or "").upper()
    tt = str(trade.get("trade_type") or "").upper()
    sev_emoji, sev_text = _position_alert_severity_label(rule.get("severity"))
    side_label = {"bull": "bullish position", "bear": "bearish position", "neutral": "neutral/IC position"}.get(side, side)
    reason_lines = eval_result.get("reason") or []
    reason_txt = "; ".join(str(x) for x in reason_lines[:4]) or "condition matched"
    spot = live.get("spot")
    pnl = live.get("unrealised_pnl")
    dte = live.get("dte")
    strike_summary = _position_alert_strike_summary(trade, live)
    vars_for_msg = _position_alert_trade_variables(trade, live)
    option_ctx_lines = _health_alert_option_context_lines(trade, live)
    # Custom position alerts only add strike/expiry metadata for option trades.
    # If the helper cannot resolve details, keep the older strike line as a fallback.
    if not option_ctx_lines and strike_summary and strike_summary != "—":
        option_ctx_lines = [f"Strikes: {strike_summary}"]
    lines = [
        f"{sev_emoji} Position Custom Alert — {sev_text}",
        "",
        f"#{tid} {sym} ({tt})",
        *option_ctx_lines,
        f"Rule: {rule.get('name')}",
        f"Applies to: {side_label}",
        f"Reason: {reason_txt}",
        "",
    ]
    if spot is not None:
        try: lines.append(f"Spot: ${float(spot):.2f}")
        except Exception: pass
    if pnl is not None:
        try: lines.append(f"P&L: ${float(pnl):+.0f}")
        except Exception: pass
    if dte is not None:
        lines.append(f"DTE: {dte}d")
    for k, label in (("short_strike", "Short"), ("long_strike", "Long"), ("put_sell", "Put sell"), ("put_buy", "Put buy"), ("call_sell", "Call sell"), ("call_buy", "Call buy"), ("breakeven", "Breakeven")):
        if vars_for_msg.get(k) is not None:
            try:
                lines.append(f"{label}: {float(vars_for_msg.get(k)):.2f}")
            except Exception:
                pass
    lines += ["", f"Condition: {condition_text}"]
    try:
        result = send_telegram_message("\n".join(lines))
        ok = bool(result.get("ok"))
        if ok:
            try:
                log_alert_notification(
                    "POSITION_CUSTOM_ALERT",
                    f"{sym} {rule.get('name')} matched",
                    reason_txt,
                    symbol=sym,
                    trade_id=tid,
                    severity=str(rule.get("severity") or "warning"),
                    source="position_custom_alert",
                    scanner_name=rule.get("name"),
                    metadata={"rule_id": rule.get("id"), "side": side, "condition": condition_text, "strikes": strike_summary},
                )
            except Exception:
                pass
        return {"ok": ok, "reason": None if ok else result.get("description") or "Telegram failed"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def _scan_custom_position_alerts_and_notify(detailed: bool = False, *, send: bool = True) -> dict:
    _ensure_position_alert_rule_tables()
    rules = _load_position_alert_rules(enabled_only=True)
    stats = {"ok": True, "checked": 0, "matched": 0, "sent": 0, "suppressed": 0, "skipped": 0, "rules": len(rules), "reasons": []}
    if not rules:
        return stats
    con = _conn()
    try:
        trades = [_as_dict(r) for r in con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()]
    finally:
        con.close()
    today = date.today().isoformat()
    ctx_cache = {}
    for t in trades:
        try:
            if not _is_open_option_trade(t):
                continue
            live = _compute_live_pnl(t)
            for rule in rules:
                stats["checked"] += 1
                side, expr = _position_alert_expr_for_trade(rule, t)
                if not expr:
                    stats["skipped"] += 1
                    continue
                try:
                    ev = _eval_position_alert_condition(t.get("symbol"), expr, rule.get("benchmark") or "SPY", ctx_cache, trade=t, live=live)
                except Exception as e:
                    stats["skipped"] += 1
                    if detailed:
                        stats["reasons"].append({"id": t.get("id"), "symbol": t.get("symbol"), "trade_type": t.get("trade_type"), "strikes": _position_alert_strike_summary(t, live), "expiry": t.get("expiry"), "side": side, "rule": rule.get("name"), "condition": expr, "reason": str(e)[:160]})
                    continue
                signature = f"{rule.get('id')}|{side}|{expr}"
                reason_txt = "; ".join(str(x) for x in (ev.get("reason") or [])[:4]) or "condition matched"
                if not ev.get("ok"):
                    _save_position_alert_state(t, rule, side=side, matched=False, reason="not matched", sent=False, signature=signature)
                    continue
                stats["matched"] += 1
                prev = _position_alert_state(int(t.get("id")), int(rule.get("id")))
                if prev.get("last_trigger_date") == today and prev.get("last_signature") == signature:
                    stats["suppressed"] += 1
                    _save_position_alert_state(t, rule, side=side, matched=True, reason="already sent today", sent=False, signature=signature)
                    if detailed:
                        stats["reasons"].append({"id": t.get("id"), "symbol": t.get("symbol"), "trade_type": t.get("trade_type"), "strikes": _position_alert_strike_summary(t, live), "expiry": t.get("expiry"), "side": side, "rule": rule.get("name"), "condition": expr, "reason": "already sent today"})
                    continue
                if send:
                    tg = _send_position_alert_telegram(t, live, rule, side=side, condition_text=expr, eval_result=ev)
                    if tg.get("ok"):
                        stats["sent"] += 1
                        _save_position_alert_state(t, rule, side=side, matched=True, reason=reason_txt, sent=True, signature=signature)
                    else:
                        stats["skipped"] += 1
                        _save_position_alert_state(t, rule, side=side, matched=True, reason=tg.get("reason") or reason_txt, sent=False, signature=signature)
                        if detailed:
                            stats["reasons"].append({"id": t.get("id"), "symbol": t.get("symbol"), "trade_type": t.get("trade_type"), "strikes": _position_alert_strike_summary(t, live), "expiry": t.get("expiry"), "side": side, "rule": rule.get("name"), "condition": expr, "reason": tg.get("reason")})
                else:
                    stats["sent"] += 0
                    if detailed:
                        stats["reasons"].append({"id": t.get("id"), "symbol": t.get("symbol"), "trade_type": t.get("trade_type"), "strikes": _position_alert_strike_summary(t, live), "expiry": t.get("expiry"), "side": side, "rule": rule.get("name"), "condition": expr, "reason": reason_txt})
        except Exception as e:
            stats["skipped"] += 1
            if detailed:
                stats["reasons"].append({"id": t.get("id"), "symbol": t.get("symbol"), "trade_type": t.get("trade_type"), "strikes": _position_alert_strike_summary(t, {}) if isinstance(t, dict) else "—", "reason": str(e)[:160]})
    if not detailed:
        stats["reasons"] = []
    else:
        stats["reasons"] = stats["reasons"][:50]
    return stats


@journal_bp.route("/journal/position_alert_rules", methods=["GET"])
def position_alert_rules_list():
    return _jsonify_safe({"ok": True, "rules": _load_position_alert_rules(enabled_only=False)})


@journal_bp.route("/journal/position_alert_rules", methods=["POST"])
def position_alert_rules_create():
    _ensure_position_alert_rule_tables()
    try:
        d = _normalise_position_alert_rule_payload(request.get_json(force=True) or {})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    con = _conn()
    try:
        cur = con.execute("""
            INSERT INTO trade_position_alert_rules
                (name, enabled, severity, benchmark, bull_condition, bear_condition, neutral_condition, notes, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
        """, (d["name"], d["enabled"], d["severity"], d["benchmark"], d["bull_condition"], d["bear_condition"], d["neutral_condition"], d["notes"]))
        con.commit()
        row = con.execute("SELECT * FROM trade_position_alert_rules WHERE id=?", (cur.lastrowid,)).fetchone()
        return _jsonify_safe({"ok": True, "rule": dict(row)})
    finally:
        con.close()


@journal_bp.route("/journal/position_alert_rules/<int:rule_id>", methods=["PUT"])
def position_alert_rules_update(rule_id: int):
    _ensure_position_alert_rule_tables()
    try:
        d = _normalise_position_alert_rule_payload(request.get_json(force=True) or {})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    con = _conn()
    try:
        con.execute("""
            UPDATE trade_position_alert_rules
               SET name=?, enabled=?, severity=?, benchmark=?, bull_condition=?, bear_condition=?,
                   neutral_condition=?, notes=?, updated_at=datetime('now')
             WHERE id=?
        """, (d["name"], d["enabled"], d["severity"], d["benchmark"], d["bull_condition"], d["bear_condition"], d["neutral_condition"], d["notes"], int(rule_id)))
        con.commit()
        row = con.execute("SELECT * FROM trade_position_alert_rules WHERE id=?", (int(rule_id),)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "rule not found"}), 404
        return _jsonify_safe({"ok": True, "rule": dict(row)})
    finally:
        con.close()


@journal_bp.route("/journal/position_alert_rules/<int:rule_id>", methods=["DELETE"])
def position_alert_rules_delete(rule_id: int):
    _ensure_position_alert_rule_tables()
    con = _conn()
    try:
        con.execute("DELETE FROM trade_position_alert_rules WHERE id=?", (int(rule_id),))
        con.execute("DELETE FROM trade_position_alert_state WHERE rule_id=?", (int(rule_id),))
        con.commit()
        return _jsonify_safe({"ok": True})
    finally:
        con.close()


@journal_bp.route("/journal/position_alerts/test", methods=["POST"])
def position_alerts_test():
    return _jsonify_safe(_scan_custom_position_alerts_and_notify(detailed=True, send=False))


@journal_bp.route("/journal/position_alerts/manual_scan", methods=["POST"])
def position_alerts_manual_scan():
    return _jsonify_safe(_scan_custom_position_alerts_and_notify(detailed=True, send=True))


def _scan_health_alerts_and_notify(detailed: bool = False):
    """
    Called by the background watcher every 5 minutes.
    Sends Telegram only when persistent status/score state changed.
    """
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
    finally:
        con.close()
    stats = {"ok": True, "sent": 0, "suppressed": 0, "skipped": 0, "checked": 0, "reasons": []}
    for row in rows:
        try:
            stats["checked"] += 1
            t    = _as_dict(row)
            live = _compute_live_pnl(t)
            result = _send_health_alert_telegram_smart(t, live)
            if result.get("ok"):
                stats["sent"] += 1
            elif result.get("suppressed"):
                stats["suppressed"] += 1
                if detailed:
                    stats["reasons"].append({"id": t.get("id"), "symbol": t.get("symbol"), "reason": result.get("reason")})
            else:
                stats["skipped"] += 1
                if detailed:
                    stats["reasons"].append({"id": t.get("id"), "symbol": t.get("symbol"), "reason": result.get("reason")})
        except Exception as e:
            stats["skipped"] += 1
            if detailed:
                try:
                    stats["reasons"].append({"id": row["id"], "reason": str(e)[:120]})
                except Exception:
                    stats["reasons"].append({"reason": str(e)[:120]})
    health_sent = int(stats.get("sent") or 0)
    try:
        custom_stats = _scan_custom_position_alerts_and_notify(detailed=detailed, send=True)
    except Exception as e:
        custom_stats = {"ok": False, "sent": 0, "suppressed": 0, "skipped": 1, "checked": 0, "matched": 0, "rules": 0, "error": str(e)}
    try:
        ai_stats = _scan_ai_alerts_once(detailed=detailed, send=True)
    except Exception as e:
        ai_stats = {"ok": False, "enabled": _ai_alerts_enabled(), "sent": 0, "suppressed": 0, "skipped": 1, "checked": 0, "alerts": [], "error": str(e)}
    if not detailed:
        return health_sent + int(custom_stats.get("sent") or 0) + int(ai_stats.get("sent") or 0)
    stats["health_sent"] = health_sent
    stats["position_custom_sent"] = int(custom_stats.get("sent") or 0)
    stats["position_custom_matched"] = int(custom_stats.get("matched") or 0)
    stats["position_custom_rules"] = int(custom_stats.get("rules") or 0)
    stats["ai_sent"] = int(ai_stats.get("sent") or 0)
    stats["ai_checked"] = int(ai_stats.get("checked") or 0)
    stats["ai_alerts"] = ai_stats
    stats["sent"] = health_sent + int(custom_stats.get("sent") or 0) + int(ai_stats.get("sent") or 0)
    stats["suppressed"] = int(stats.get("suppressed") or 0) + int(custom_stats.get("suppressed") or 0) + int(ai_stats.get("suppressed") or 0)
    stats["skipped"] = int(stats.get("skipped") or 0) + int(custom_stats.get("skipped") or 0) + int(ai_stats.get("skipped") or 0)
    stats["custom_position_alerts"] = custom_stats
    stats["reasons"] = (stats["reasons"] + list(custom_stats.get("reasons") or []) + list(ai_stats.get("reasons") or []))[:25]
    return stats


# Background health alert watcher
_health_alert_watcher_started = False
_health_alert_watcher_lock    = threading.Lock()


def start_health_alert_watcher(interval_seconds: int = 300):
    """Start background watcher that checks health every 5 min and fires Telegram on threshold change."""
    global _health_alert_watcher_started
    with _health_alert_watcher_lock:
        if _health_alert_watcher_started:
            return False
        _health_alert_watcher_started = True

    def _loop():
        from ..services.job_registry import register_job, is_enabled, mark_run
        register_job(
            "trade_health_alerts", "Trade health alerts", "Checks position health scores every few minutes and pushes Telegram alerts on threshold changes.",
            kind="interval", default_schedule={"interval_min": max(1, int((interval_seconds or 300) / 60))},
            group="Alert Watchers", run_now_fn=_scan_health_alerts_and_notify,
        )
        while True:
            if is_enabled("trade_health_alerts"):
                try:
                    _scan_health_alerts_and_notify()
                    mark_run("trade_health_alerts", True, "")
                except Exception as e:
                    mark_run("trade_health_alerts", False, str(e))
            time.sleep(max(60, _global_telegram_alert_sleep_seconds(int(interval_seconds or 300))))

    th = threading.Thread(target=_loop, name="health-alert-watcher", daemon=True)
    th.start()
    return True


@journal_bp.route("/journal/health_alert/manual_scan", methods=["POST"])
def health_alert_manual_scan():
    """Manually trigger a deduped health alert scan and Telegram send."""
    return _jsonify_safe(_scan_health_alerts_and_notify(detailed=True))


@journal_bp.route("/journal/health_alert/state", methods=["GET"])
def health_alert_state():
    """Return persistent health alert state for debugging / UI."""
    _ensure_health_alert_state_table()
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM trade_health_alert_state ORDER BY updated_at DESC LIMIT 500").fetchall()
        state_rows = [_row_to_health_alert_state(r) for r in rows]
    finally:
        con.close()
    payload = {
        "state": {str(r.get("trade_id")): r for r in state_rows if r.get("trade_id") is not None},
        "runtime_cache_count": len(_health_alert_state),
        "watcher": _health_alert_watcher_started,
        "score_delta_points": _health_alert_score_delta_points(),
        "send_on_first_seen": _health_alert_send_on_first_seen(),
        "pnr_alerts_enabled": _pnr_alerts_enabled(),
        "ai_alerts_enabled": _ai_alerts_enabled(),
        "custom_position_alerts_enabled": _custom_position_alerts_enabled(),
    }
    payload.update({"global_alert_settings": _alert_settings_payload()})
    return _jsonify_safe(payload)

