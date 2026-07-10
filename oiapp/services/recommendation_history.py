"""Persist AI / scanner recommendation runs for later fine-tuning.

This module stores run-level summaries and per-trade candidate rows in a simple
SQLite table so the scheduled OI buildup and weekly-plan jobs can build a
history of tradeable setups without altering the existing pages.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..db import _connect

DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        try:
            return json.dumps(str(value), ensure_ascii=False)
        except Exception:
            return "{}"


def _ensure_table() -> None:
    con = _connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS recommendation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                run_kind TEXT NOT NULL,
                row_kind TEXT NOT NULL DEFAULT 'tradeable',
                symbol TEXT,
                expiry TEXT,
                label TEXT,
                score REAL,
                confidence REAL,
                direction TEXT,
                strategy TEXT,
                status TEXT,
                created_at TEXT NOT NULL,
                settings_json TEXT,
                summary_json TEXT,
                payload_json TEXT
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_reco_hist_source_time ON recommendation_history(source, created_at DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_reco_hist_symbol_time ON recommendation_history(symbol, created_at DESC)")
        con.commit()
    finally:
        con.close()


def _score_value(item: Dict[str, Any]) -> float:
    for key in ("score", "confidence", "flow_confidence", "weekly_plan_score", "composite_score"):
        val = item.get(key)
        try:
            if val is None:
                continue
            num = float(val)
            if math.isfinite(num):
                return round(num, 2)
        except Exception:
            continue
    return 0.0


def _strategy_name(item: Dict[str, Any]) -> str:
    return str(
        item.get("suggested_strategy")
        or item.get("strategy")
        or item.get("type")
        or item.get("trade_family")
        or item.get("final_seller_read")
        or ""
    )


def _direction(item: Dict[str, Any]) -> str:
    return str(
        item.get("direction")
        or item.get("bias")
        or item.get("final_seller_read")
        or item.get("trade_family")
        or ""
    )


def _expiry(item: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> str:
    cand = item.get("expiry")
    if cand not in (None, ""):
        return str(cand)
    if payload and payload.get("expiry") not in (None, ""):
        return str(payload.get("expiry"))
    return ""


def _tradeable_filter(row: Dict[str, Any], run_kind: str) -> bool:
    text = " ".join(str(row.get(k) or "") for k in ("suggested_action", "reason", "strategy", "type", "trade_family", "bias", "final_seller_read"))
    score = _score_value(row)
    if run_kind == "oi_buildup":
        if any(x in text.upper() for x in ("WAIT", "NO_TRADE", "MIXED", "NEUTRAL")):
            return score >= 70 and "WAIT" not in text.upper()
        if row.get("earnings_conflict"):
            return False
        return score >= 70 or str(row.get("suggested_action") or "").upper() in {"BULLISH", "BEARISH"}
    if run_kind == "weekly_plan":
        if str(row.get("type") or "").startswith("⚠"):
            return False
        if "NOT RECOMMENDED" in text.upper():
            return False
        return score >= 55 or score >= 0
    return score >= 60


def save_run(
    *,
    source: str,
    run_kind: str,
    rows: Sequence[Dict[str, Any]],
    summary: Optional[Dict[str, Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
    label: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Save a run summary and tradeable candidate rows for later analysis."""
    _ensure_table()
    summary = dict(summary or {})
    payload = dict(payload or {})
    settings = dict(settings or {})
    run_kind = str(run_kind or "generic")
    source = str(source or run_kind or "manual")
    label = str(label or payload.get("label") or source)
    created_at = _now()

    saved_rows: List[Dict[str, Any]] = []
    con = _connect()
    try:
        # Save a run summary row even if no tradeable candidates are found.
        con.execute(
            """
            INSERT INTO recommendation_history(
                source, run_kind, row_kind, symbol, expiry, label,
                score, confidence, direction, strategy, status,
                created_at, settings_json, summary_json, payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source,
                run_kind,
                "run_summary",
                str(summary.get("symbol") or payload.get("symbol") or ""),
                str(summary.get("expiry") or payload.get("expiry") or ""),
                label,
                _score_value(summary or payload),
                _score_value(summary or payload),
                str(summary.get("direction") or summary.get("bias") or payload.get("bias") or ""),
                str(summary.get("strategy") or summary.get("suggested_strategy") or payload.get("plan_name") or ""),
                str(summary.get("status") or summary.get("result") or "saved"),
                created_at,
                _safe_json(settings),
                _safe_json(summary),
                _safe_json(payload),
            ),
        )

        for row in rows:
            if not isinstance(row, dict):
                continue
            if not _tradeable_filter(row, run_kind):
                continue
            symbol = str(row.get("symbol") or payload.get("symbol") or "").upper()
            expiry = _expiry(row, payload)
            score = _score_value(row)
            strategy = _strategy_name(row)
            direction = _direction(row)
            status = str(row.get("suggested_action") or row.get("status") or row.get("type") or "tradeable")
            row_payload = dict(row)
            row_payload.setdefault("saved_source", source)
            row_payload.setdefault("saved_run_kind", run_kind)
            row_payload.setdefault("saved_label", label)
            con.execute(
                """
                INSERT INTO recommendation_history(
                    source, run_kind, row_kind, symbol, expiry, label,
                    score, confidence, direction, strategy, status,
                    created_at, settings_json, summary_json, payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    source,
                    run_kind,
                    "tradeable",
                    symbol,
                    expiry,
                    label,
                    score,
                    _score_value(row),
                    direction,
                    strategy,
                    status,
                    created_at,
                    _safe_json(settings),
                    _safe_json(summary),
                    _safe_json(row_payload),
                ),
            )
            saved_rows.append({"symbol": symbol, "expiry": expiry, "score": score, "strategy": strategy, "direction": direction})
        con.commit()
    finally:
        con.close()
    return {"ok": True, "saved_count": len(saved_rows), "summary_saved": True, "label": label, "created_at": created_at, "rows": saved_rows}


def recent(limit: int = 50, run_kind: Optional[str] = None) -> List[Dict[str, Any]]:
    _ensure_table()
    con = _connect()
    try:
        sql = "SELECT * FROM recommendation_history"
        params: List[Any] = []
        if run_kind:
            sql += " WHERE run_kind=?"
            params.append(run_kind)
        sql += " ORDER BY datetime(created_at) DESC, id DESC LIMIT ?"
        params.append(max(1, min(500, int(limit or 50))))
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()
