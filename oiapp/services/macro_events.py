"""Major market event message-board helpers.

The top-bar message board is intentionally lightweight and dependency-free:
- Uses built-in high-impact U.S. macro dates as a safe fallback.
- Optionally merges user-maintained events from data/macro_events.json.
- Returns only nearby events so the UI can scroll them right-to-left.

User event file format:
[
  {"date":"2026-06-17", "time":"14:00", "title":"FOMC Rate Decision", "impact":"high", "source":"manual", "detail":"Press conference 14:30 ET"}
]
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, date, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

ET = ZoneInfo("America/New_York") if ZoneInfo is not None else timezone.utc


def _today_et() -> date:
    return datetime.now(ET).date()


def _now_et() -> datetime:
    return datetime.now(ET)


def _event_dt(ev: Dict[str, Any]) -> Optional[datetime]:
    try:
        d = date.fromisoformat(str(ev.get("date")))
        raw_t = str(ev.get("time") or "09:30").strip()
        try:
            hh, mm = raw_t.split(":")[:2]
            t = dt_time(int(hh), int(mm))
        except Exception:
            t = dt_time(9, 30)
        return datetime.combine(d, t, tzinfo=ET)
    except Exception:
        return None


def _clean_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(ev)
    out["impact"] = str(out.get("impact") or "medium").lower()
    out["source"] = str(out.get("source") or "built-in")
    out["time"] = str(out.get("time") or "")
    out["title"] = str(out.get("title") or "Market Event")
    out["detail"] = str(out.get("detail") or "")
    return out


# Built-in fallback.  The app can be supplemented by data/macro_events.json;
# this list only covers the highest-impact items needed for the dashboard guardrail.
_BUILTIN_EVENTS_2026: List[Dict[str, Any]] = [
    # FOMC regular policy-decision days, 14:00 ET; press conferences generally 14:30 ET.
    {"date":"2026-01-28", "time":"14:00", "title":"FOMC Rate Decision", "impact":"high", "source":"Fed", "detail":"FOMC day · decision 2:00 ET · press conference usually 2:30 ET"},
    {"date":"2026-03-18", "time":"14:00", "title":"FOMC Rate Decision", "impact":"high", "source":"Fed", "detail":"FOMC day · decision 2:00 ET · press conference usually 2:30 ET"},
    {"date":"2026-04-29", "time":"14:00", "title":"FOMC Rate Decision", "impact":"high", "source":"Fed", "detail":"FOMC day · decision 2:00 ET · press conference usually 2:30 ET"},
    {"date":"2026-06-17", "time":"14:00", "title":"FOMC Rate Decision", "impact":"high", "source":"Fed", "detail":"FOMC day · decision 2:00 ET · press conference usually 2:30 ET"},
    {"date":"2026-07-29", "time":"14:00", "title":"FOMC Rate Decision", "impact":"high", "source":"Fed", "detail":"FOMC day · decision 2:00 ET · press conference usually 2:30 ET"},
    {"date":"2026-09-16", "time":"14:00", "title":"FOMC Rate Decision", "impact":"high", "source":"Fed", "detail":"FOMC day · decision 2:00 ET · press conference usually 2:30 ET"},
    {"date":"2026-10-28", "time":"14:00", "title":"FOMC Rate Decision", "impact":"high", "source":"Fed", "detail":"FOMC day · decision 2:00 ET · press conference usually 2:30 ET"},
    {"date":"2026-12-09", "time":"14:00", "title":"FOMC Rate Decision", "impact":"high", "source":"Fed", "detail":"FOMC day · decision 2:00 ET · press conference usually 2:30 ET"},

    # Selected BLS high-impact releases visible in the 2026 BLS calendar snapshot.
    {"date":"2026-06-05", "time":"08:30", "title":"Employment Situation", "impact":"high", "source":"BLS", "detail":"Monthly payrolls / unemployment release"},
    {"date":"2026-06-10", "time":"08:30", "title":"Consumer Price Index", "impact":"high", "source":"BLS", "detail":"CPI inflation release"},
    {"date":"2026-06-11", "time":"08:30", "title":"Producer Price Index", "impact":"medium", "source":"BLS", "detail":"PPI inflation release"},
    {"date":"2026-07-02", "time":"08:30", "title":"Employment Situation", "impact":"high", "source":"BLS", "detail":"Monthly payrolls / unemployment release"},
    {"date":"2026-07-14", "time":"08:30", "title":"Consumer Price Index", "impact":"high", "source":"BLS", "detail":"CPI inflation release"},
    {"date":"2026-07-15", "time":"08:30", "title":"Producer Price Index", "impact":"medium", "source":"BLS", "detail":"PPI inflation release"},
    {"date":"2026-08-07", "time":"08:30", "title":"Employment Situation", "impact":"high", "source":"BLS", "detail":"Monthly payrolls / unemployment release"},
    {"date":"2026-08-12", "time":"08:30", "title":"Consumer Price Index", "impact":"high", "source":"BLS", "detail":"CPI inflation release"},
]


def _user_events_path() -> Path:
    return Path(os.environ.get("OIAPP_MACRO_EVENTS_FILE", "data/macro_events.json"))


def _load_user_events() -> List[Dict[str, Any]]:
    path = _user_events_path()
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("events") or []
        if not isinstance(data, list):
            return []
        return [_clean_event(x) for x in data if isinstance(x, dict)]
    except Exception as exc:
        return [{
            "date": _today_et().isoformat(),
            "time": "09:30",
            "title": "Macro event file error",
            "impact": "medium",
            "source": "local",
            "detail": str(exc),
        }]


def _all_events() -> List[Dict[str, Any]]:
    # User events override duplicates by date + title + time because they are appended last.
    merged: Dict[str, Dict[str, Any]] = {}
    for ev in [_clean_event(x) for x in _BUILTIN_EVENTS_2026] + _load_user_events():
        key = f"{ev.get('date')}|{ev.get('time')}|{str(ev.get('title','')).lower()}"
        merged[key] = ev
    return list(merged.values())


def _event_status(ev_dt: datetime, now: datetime) -> Dict[str, Any]:
    mins = int(round((ev_dt - now).total_seconds() / 60.0))
    if mins > 0:
        if mins < 60:
            label = f"in {mins}m"
        elif mins < 1440:
            label = f"in {mins // 60}h {mins % 60}m"
        else:
            label = f"in {mins // 1440}d"
        active = mins <= 240
        phase = "upcoming"
    else:
        ago = abs(mins)
        if ago < 60:
            label = f"{ago}m ago"
        elif ago < 1440:
            label = f"{ago // 60}h ago"
        else:
            label = f"{ago // 1440}d ago"
        active = ago <= 360
        phase = "passed"
    return {"minutes_until": mins, "status": label, "active": active, "phase": phase}


def get_macro_message_board(days_back: int = 0, days_ahead: int = 3) -> Dict[str, Any]:
    now = _now_et()
    start = now.date() - timedelta(days=max(0, int(days_back or 0)))
    end = now.date() + timedelta(days=max(0, int(days_ahead or 0)))
    events: List[Dict[str, Any]] = []
    for ev in _all_events():
        dt = _event_dt(ev)
        if not dt:
            continue
        if start <= dt.date() <= end:
            x = _clean_event(ev)
            x["datetime_et"] = dt.isoformat()
            x["date_label"] = dt.strftime("%a %m/%d")
            x["time_label"] = dt.strftime("%I:%M %p ET").lstrip("0")
            x.update(_event_status(dt, now))
            # A high-impact event today deserves extra emphasis even if it has passed.
            x["is_today"] = dt.date() == now.date()
            x["priority"] = (0 if x["is_today"] and x.get("impact") == "high" else 1 if x["is_today"] else 2)
            events.append(x)
    events.sort(key=lambda e: (e.get("priority", 9), abs(int(e.get("minutes_until", 999999))), e.get("datetime_et", "")))

    messages: List[str] = []
    for e in events[:8]:
        impact = str(e.get("impact", "")).upper()
        tag = "🚨" if e.get("impact") == "high" and e.get("is_today") else "⚠️" if e.get("impact") == "high" else "ℹ️"
        detail = f" · {e.get('detail')}" if e.get("detail") else ""
        messages.append(f"{tag} {impact}: {e.get('title')} {e.get('date_label')} {e.get('time_label')} ({e.get('status')}){detail}")
    if not messages:
        messages.append("✅ No high-impact macro events found in the next few days. Check earnings and OI/GEX before entry.")
    return {
        "now_et": now.isoformat(),
        "events": events,
        "messages": messages,
        "source_note": "Built-in high-impact calendar plus optional data/macro_events.json overrides",
    }
