"""Telegram alert framework for watchlist symbol price crossings.

The service watches enabled watchlist-symbol alert rules and sends a Telegram
message when the live price crosses the configured level.

Configuration can be supplied either through environment variables or the app
settings table:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Blueprint, jsonify, request

from ..scanners.watchlist_manager import (
    _ensure_tables,
    _conn,
    get_alert_watchlist_rows,
    _get_setting,
    _set_setting,
    _ALERT_FREQUENCY_OPTIONS,
    get_global_alert_frequency,
    get_global_alert_interval_seconds,
    set_global_alert_frequency,
)

telegram_bp = Blueprint("telegram_bp", __name__, url_prefix="/telegram")

_WATCHER_STARTED = False
_WATCHER_LOCK = threading.Lock()


def _runtime_config() -> Tuple[str, str]:
    token = (_get_setting("telegram_bot_token", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = (_get_setting("telegram_chat_id", "") or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    return token, chat_id


def telegram_configured() -> bool:
    token, chat_id = _runtime_config()
    return bool(token and chat_id)


def send_telegram_message(message: str) -> Dict[str, str]:
    token, chat_id = _runtime_config()
    if not token or not chat_id:
        return {"ok": False, "error": "Telegram credentials not configured"}

    payload = urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = Request(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = {"ok": False, "raw": raw}
    return data if isinstance(data, dict) else {"ok": False, "raw": str(data)}


def _format_alert_message(watchlist_name: str, symbol: str, price: float, level: float, direction: str) -> str:
    side = "above" if direction == "above" else "below"
    return (
        f"📣 Price alert\n"
        f"Watchlist: {watchlist_name}\n"
        f"Symbol: {symbol}\n"
        f"Price: {price:.2f}\n"
        f"Level: {level:.2f}\n"
        f"Crossed {side} the alert level at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def _live_price(symbol: str) -> Optional[float]:
    try:
        from .market import get_spot
        price = get_spot(symbol)
        if price is None:
            return None
        return float(price)
    except Exception:
        return None


def check_watchlist_price_alerts() -> Dict[str, int]:
    """Check all enabled alerts and send Telegram notifications on crossings."""
    _ensure_tables()
    if not telegram_configured():
        return {"checked": 0, "triggered": 0, "sent": 0, "skipped": 0}

    rows = get_alert_watchlist_rows()
    checked = triggered = sent = skipped = 0
    con = _conn()
    try:
        for row in rows:
            checked += 1
            symbol = row["symbol"]
            level = row.get("alert_price")
            if level is None:
                skipped += 1
                continue
            current = _live_price(symbol)
            if current is None:
                skipped += 1
                continue
            prev = row.get("alert_last_price")
            if prev is None:
                prev = current
            direction = (row.get("alert_direction") or "both").lower()
            crossing = None
            if current >= float(level) and float(prev) < float(level):
                crossing = "above"
            elif current <= float(level) and float(prev) > float(level):
                crossing = "below"

            if crossing and (direction == "both" or direction == crossing):
                triggered += 1
                message = _format_alert_message(row.get("watchlist_name", "Watchlist"), symbol, current, float(level), crossing)
                result = send_telegram_message(message)
                if result.get("ok"):
                    sent += 1
                    con.execute(
                        "UPDATE watchlist_symbols SET alert_last_price=?, alert_last_side=?, alert_last_sent_at=? WHERE watchlist_id=? AND symbol=?",
                        (current, crossing, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), row["watchlist_id"], symbol),
                    )
            else:
                con.execute(
                    "UPDATE watchlist_symbols SET alert_last_price=? WHERE watchlist_id=? AND symbol=?",
                    (current, row["watchlist_id"], symbol),
                )
        con.commit()
    finally:
        con.close()
    return {"checked": checked, "triggered": triggered, "sent": sent, "skipped": skipped}


def _watcher_loop(interval_seconds: int = 60):
    from .job_registry import register_job, is_enabled, mark_run
    register_job(
        "telegram_price_alerts", "Telegram price alerts", "Checks watchlist price alerts and pushes Telegram messages.",
        kind="interval", default_schedule={"interval_min": max(1, int((interval_seconds or 60) / 60))},
        group="Alert Watchers", run_now_fn=check_watchlist_price_alerts,
    )
    while True:
        if is_enabled("telegram_price_alerts"):
            try:
                result = check_watchlist_price_alerts()
                mark_run("telegram_price_alerts", True, f"checked={result.get('checked')} sent={result.get('sent')}")
            except Exception as e:
                mark_run("telegram_price_alerts", False, str(e))
        try:
            sleep_for = get_global_alert_interval_seconds(interval_seconds or 60)
        except Exception:
            sleep_for = int(interval_seconds or 60)
        time.sleep(max(60, int(sleep_for)))


def start_telegram_alert_watcher(interval_seconds: int = 60):
    global _WATCHER_STARTED
    if _WATCHER_STARTED:
        return False
    with _WATCHER_LOCK:
        if _WATCHER_STARTED:
            return False
        t = threading.Thread(target=_watcher_loop, kwargs={"interval_seconds": interval_seconds}, daemon=True)
        t.start()
        _WATCHER_STARTED = True
        return True


# ── Telegram config API ───────────────────────────────────────────────────
@telegram_bp.route("/alert_settings", methods=["GET"])
def telegram_alert_settings_get():
    freq = get_global_alert_frequency("15m")
    return jsonify({
        "ok": True,
        "frequency": freq,
        "seconds": int(_ALERT_FREQUENCY_OPTIONS.get(freq, 900)),
        "options": [{"value": k, "seconds": v} for k, v in _ALERT_FREQUENCY_OPTIONS.items()],
        "health_score_delta_points": int(_get_setting("telegram_alert_score_delta_points", "0") or 0),
        "score_only_alerts_enabled": int(_get_setting("telegram_alert_score_delta_points", "0") or 0) > 0,
    })


@telegram_bp.route("/alert_settings", methods=["POST"])
def telegram_alert_settings_set():
    d = request.get_json(silent=True) or {}
    freq = set_global_alert_frequency(d.get("frequency") or d.get("alert_frequency") or "15m")
    # Score-only health alerts are disabled by default.  Keep this optional for
    # advanced users, but do not send Telegram for minor score movement unless
    # a positive threshold is explicitly configured.
    if "health_score_delta_points" in d:
        try:
            delta = max(0, int(d.get("health_score_delta_points") or 0))
        except Exception:
            delta = 0
        _set_setting("telegram_alert_score_delta_points", str(delta))
    return jsonify({
        "ok": True,
        "frequency": freq,
        "seconds": int(_ALERT_FREQUENCY_OPTIONS.get(freq, 900)),
        "health_score_delta_points": int(_get_setting("telegram_alert_score_delta_points", "0") or 0),
    })


@telegram_bp.route("/config", methods=["GET"])
def telegram_config_get():
    token, chat_id = _runtime_config()
    return jsonify({
        "ok": True,
        "configured": bool(token and chat_id),
        "telegram_bot_token": token[:4] + "…" if token else "",
        "telegram_chat_id": chat_id,
        "source": "db" if (_get_setting("telegram_bot_token", "") or _get_setting("telegram_chat_id", "")) else "env",
    })


@telegram_bp.route("/config", methods=["POST"])
def telegram_config_set():
    d = request.get_json(force=True) or {}
    token = (d.get("telegram_bot_token") or "").strip()
    chat_id = (d.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        return jsonify({"error": "telegram_bot_token and telegram_chat_id are required"}), 400
    _set_setting("telegram_bot_token", token)
    _set_setting("telegram_chat_id", chat_id)
    return jsonify({"ok": True, "configured": True})


@telegram_bp.route("/test", methods=["POST"])
def telegram_test_message():
    d = request.get_json(silent=True) or {}
    message = (d.get("message") or "Telegram test message from your OI app.").strip()
    result = send_telegram_message(message)
    code = 200 if result.get("ok") else 400
    return jsonify(result), code
