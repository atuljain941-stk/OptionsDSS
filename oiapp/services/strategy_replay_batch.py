"""Historical option-chain replay and limited parameter calibration.

This module is deliberately separate from the live Greeks scanner.  A replay
must use the chain snapshot available on (or before) the simulated entry date,
not the newest chain in the database and not today's DTE.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from ..config import DB_PATH
from .trade_evaluator import annotate_oi, evaluate_legs

replay_batch_bp = Blueprint("strategy_replay_batch", __name__, url_prefix="/strategy-replay")
_jobs: Dict[str, Dict[str, Any]] = {}
_lock = threading.RLock()


def _num(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _date(v: Any, default: Optional[date] = None) -> date:
    if isinstance(v, date):
        return v
    s = str(v or "")[:10]
    return datetime.strptime(s, "%Y-%m-%d").date() if s else (default or date.today())


def _json(v: Any) -> Any:
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_json(x) for x in v]
    return v


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _ensure_tables() -> None:
    with _connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS strategy_replay_runs (
            run_id TEXT PRIMARY KEY, run_name TEXT, status TEXT, created_at TEXT,
            started_at TEXT, completed_at TEXT, config_json TEXT, summary_json TEXT,
            progress REAL DEFAULT 0, error_text TEXT
        );
        CREATE TABLE IF NOT EXISTS strategy_replay_results (
            run_id TEXT NOT NULL, result_no INTEGER NOT NULL, result_json TEXT NOT NULL,
            PRIMARY KEY(run_id, result_no),
            FOREIGN KEY(run_id) REFERENCES strategy_replay_runs(run_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_replay_runs_created
            ON strategy_replay_runs(created_at DESC);
        """)


def _save_run(run_id: str, **fields: Any) -> None:
    _ensure_tables()
    fields = {k: _json(v) for k, v in fields.items()}
    with _connect() as con:
        if "config" in fields:
            fields["config_json"] = json.dumps(fields.pop("config"), allow_nan=False)
        if "summary" in fields:
            fields["summary_json"] = json.dumps(fields.pop("summary"), allow_nan=False)
        cols = ", ".join(fields)
        vals = list(fields.values())
        if cols:
            con.execute(f"UPDATE strategy_replay_runs SET {', '.join(f'{c}=?' for c in fields)} WHERE run_id=?", vals + [run_id])


def _load_config(config: Dict[str, Any]) -> Dict[str, Any]:
    end = _date(config.get("end_date"), date.today())
    start = _date(config.get("start_date"), end - timedelta(days=30))
    if start >= end or (end - start).days > 60:
        raise ValueError("Replay window must be 1–60 calendar days")
    raw_symbols = config.get("symbols") or config.get("symbol") or []
    if isinstance(raw_symbols, str):
        symbols = [x.strip().upper() for x in raw_symbols.split(",") if x.strip()]
    else:
        symbols = [str(x).strip().upper() for x in raw_symbols if str(x).strip()]
    if not symbols and config.get("watchlist_id"):
        try:
            from ..scanners.watchlist_manager import _get_watchlist_symbols_by_id
            symbols = [str(x).strip().upper() for x in (_get_watchlist_symbols_by_id(config.get("watchlist_id")) or []) if str(x).strip()]
        except Exception as exc:
            raise ValueError(f"Could not load watchlist: {exc}")
    if not symbols:
        raise ValueError("Select one symbol or an explicit symbol list/watchlist")
    if config.get("symbol") and config.get("watchlist_id"):
        raise ValueError("Choose either a symbol or a watchlist, not both")
    strategies = config.get("strategies") or ["iron_fly", "jade_lizard", "broken_wing_fly"]
    if isinstance(strategies, str):
        strategies = [x.strip() for x in strategies.split(",") if x.strip()]
    out = dict(config)
    out.update({"start_date": start.isoformat(), "end_date": end.isoformat(), "symbols": symbols,
                "strategies": strategies, "min_dte": max(15, int(config.get("min_dte", 15))),
                "max_dte": min(45, int(config.get("max_dte", 45))),
                "min_rr": float(config.get("min_rr", 0.60)),
                "min_pop": float(config.get("min_pop", 0.0)),
                "min_oi": int(config.get("min_oi", 50)),
                "max_spread_pct": float(config.get("max_spread_pct", 15.0)),
                "min_strike_oi_pct": float(config.get("min_strike_oi_pct", 0.0)),
                "min_call_oi_total": int(config.get("min_call_oi_total", 0)),
                "min_put_oi_total": int(config.get("min_put_oi_total", 0)),
                "max_candidates_per_day": max(1, int(config.get("max_candidates_per_day", 1))),
                "earnings_gate": str(config.get("earnings_gate", "exclude"))})
    if out["min_dte"] > out["max_dte"]:
        raise ValueError("min_dte must not exceed max_dte")
    return out


def _schema_columns(con: sqlite3.Connection) -> set:
    return {str(r[1]).lower() for r in con.execute("PRAGMA table_info(options)").fetchall()}


def _spot(con: sqlite3.Connection, symbol: str, asof: str, rows: List[sqlite3.Row]) -> Optional[float]:
    vals = [_num(r["underlying"]) for r in rows if "underlying" in r.keys()]
    vals = [x for x in vals if x and x > 0]
    if vals:
        return vals[0]
    try:
        from ..scanners.sqlite_store import get_market_data_store
        bars = get_market_data_store().get_bars(symbol, date.fromisoformat(asof) - timedelta(days=5), date.fromisoformat(asof), "1d")
        return _num(bars["Close"].iloc[-1]) if bars is not None and not bars.empty else None
    except Exception:
        return None


def _snapshot(con: sqlite3.Connection, symbol: str, expiry: str, asof: str) -> Tuple[Optional[str], List[sqlite3.Row]]:
    row = con.execute("SELECT MAX(date) FROM options WHERE symbol=? AND expiration=? AND date<=?", (symbol, expiry, asof)).fetchone()
    snap = row[0] if row and row[0] else None
    if not snap:
        return None, []
    return snap, con.execute("SELECT * FROM options WHERE symbol=? AND expiration=? AND date=?", (symbol, expiry, snap)).fetchall()


def _chain(rows: List[sqlite3.Row]) -> Tuple[Dict[Tuple[str, float], dict], float]:
    q: Dict[Tuple[str, float], dict] = {}
    ivs: List[float] = []
    for r in rows:
        typ = "call" if str(r["type"]).lower().startswith("c") else "put"
        k = _num(r["strike"])
        if k is None:
            continue
        d = {key: r[key] for key in r.keys()}
        d["oi"] = int(_num(d.get("oi"), 0) or 0)
        for key in ("price", "bid", "ask", "last", "iv"):
            d[key] = _num(d.get(key))
        if d.get("iv") and 0.03 <= d["iv"] <= 3:
            ivs.append(d["iv"])
        q[(typ, float(k))] = d
    return q, (sum(ivs) / len(ivs) if ivs else 0.30)


def _builders() -> Dict[str, Any]:
    from ..scanners.greeks_strategy_scanner import STRATEGY_BUILDERS
    return STRATEGY_BUILDERS


def _result_metrics(candidate: Dict[str, Any], q: Dict[Tuple[str, float], dict], spot: float, expiry: str, dte: int, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ev = evaluate_legs(candidate.get("legs") or [], q, spot, expiry, candidate.get("avg_iv"), as_of=candidate.get("entry_date"))
    annotate_oi(ev, q, float(cfg["min_strike_oi_pct"]))
    if not ev.get("valid") or (ev.get("rr_value") is not None and ev["rr_value"] < cfg["min_rr"]):
        return None
    if ev.get("model_pop") is not None and ev["model_pop"] < cfg["min_pop"]:
        return None
    if ev.get("expiry_call_oi", 0) < cfg["min_call_oi_total"] or ev.get("expiry_put_oi", 0) < cfg["min_put_oi_total"]:
        return None
    rr = _num(ev.get("rr_value"))
    pop = _num(ev.get("model_pop"))
    breakeven_pop = 1.0 / (1.0 + rr) if rr is not None and rr >= 0 else None
    expectancy_r = pop * rr - (1 - pop) if pop is not None and rr is not None else None
    return {"symbol": candidate.get("symbol"), "strategy": candidate.get("strategy"), "expiry": expiry,
            "dte": dte, "spot": spot, "legs": ev.get("legs"), "model_pop": pop,
            "breakeven_pop": breakeven_pop, "pop_edge": pop - breakeven_pop if pop is not None and breakeven_pop is not None else None,
            "rr": rr, "expectancy_r": expectancy_r, "max_profit": ev.get("max_profit"), "max_loss": ev.get("max_loss"),
            "credit": ev.get("net_credit"), "breakeven_lower": ev.get("breakeven_lower"),
            "breakeven_upper": ev.get("breakeven_upper"), "expiry_call_oi": ev.get("expiry_call_oi"),
            "expiry_put_oi": ev.get("expiry_put_oi"), "oi_filter": ev.get("strike_oi_filter"),
            "snapshot_date": candidate.get("snapshot_date"), "earnings_gate": cfg.get("earnings_gate"),
            "earnings_filter_status": "historical earnings series unavailable; verify manually",
            "model_assumptions": "lognormal constant-IV; not historical win probability"}


def _realize(con: sqlite3.Connection, row: Dict[str, Any], asof: str) -> None:
    expiry = row["expiry"]
    try:
        from ..scanners.sqlite_store import get_market_data_store
        d0, d1 = date.fromisoformat(expiry), date.fromisoformat(expiry) + timedelta(days=5)
        bars = get_market_data_store().get_bars(row["symbol"], d0, d1, "1d")
        spot = _num(bars["Close"].iloc[0]) if bars is not None and not bars.empty else None
    except Exception:
        spot = None
    if spot is None:
        row.update({"realized_outcome": "UNRESOLVED", "realized_pnl": None, "realized_r": None})
        return
    pnl = 0.0
    for leg in row.get("legs") or []:
        intrinsic = max(spot - leg["strike"], 0) if leg["option_type"] == "call" else max(leg["strike"] - spot, 0)
        pnl += (intrinsic - leg["entry_price"]) * (1 if leg["side"] == "buy" else -1)
    risk = abs(_num(row.get("max_loss"), 0) or 0)
    row.update({"realized_pnl": round(pnl, 4), "realized_r": round(pnl / risk, 4) if risk else None,
                "realized_outcome": "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"})


def _run(run_id: str, cfg: Dict[str, Any]) -> None:
    try:
        con = _connect()
        builder_map = _builders()
        days = [date.fromisoformat(cfg["start_date"]) + timedelta(days=i) for i in range((date.fromisoformat(cfg["end_date"]) - date.fromisoformat(cfg["start_date"])).days + 1)]
        results: List[Dict[str, Any]] = []
        total = max(1, len(days) * len(cfg["symbols"]))
        done = 0
        for asof in days:
            for symbol in cfg["symbols"]:
                done += 1
                with _lock:
                    _jobs[run_id]["progress"] = round(done / total * 100, 2)
                exps = [r[0] for r in con.execute("SELECT DISTINCT expiration FROM options WHERE symbol=? AND expiration>? AND expiration<=? ORDER BY expiration", (symbol, asof.isoformat(), (asof + timedelta(days=cfg["max_dte"])).isoformat())).fetchall()]
                candidates = []
                for expiry in exps:
                    dte = (date.fromisoformat(expiry) - asof).days
                    if dte < cfg["min_dte"] or dte > cfg["max_dte"]:
                        continue
                    snap, rows = _snapshot(con, symbol, expiry, asof.isoformat())
                    if not rows:
                        continue
                    q, iv = _chain(rows); spot = _spot(con, symbol, asof.isoformat(), rows)
                    if not spot or spot <= 0:
                        continue
                    sides = {"calls": {}, "puts": {}}
                    for (typ, strike), value in q.items():
                        sides[typ + "s"][strike] = value
                    T = dte / 365.0
                    for name in cfg["strategies"]:
                        builder = builder_map.get(name)
                        if not builder:
                            continue
                        try:
                            c = builder(symbol, expiry, spot, iv, T, dte, sides, min_oi=cfg["min_oi"], max_spread_pct=cfg["max_spread_pct"])
                        except Exception:
                            c = None
                        if c:
                            c["snapshot_date"] = snap
                            r = _result_metrics(c, q, spot, expiry, dte, cfg)
                            if r:
                                r["entry_date"] = asof.isoformat(); _realize(con, r, asof.isoformat()); candidates.append(r)
                candidates.sort(key=lambda x: (x.get("expectancy_r") is not None, x.get("expectancy_r") or -999), reverse=True)
                results.extend(candidates[:cfg["max_candidates_per_day"]])
        closed = [r for r in results if r.get("realized_outcome") in {"WIN", "LOSS", "FLAT"}]
        wins = sum(r.get("realized_outcome") == "WIN" for r in closed)
        realized_r = [r["realized_r"] for r in closed if r.get("realized_r") is not None]
        exp_r = [r["expectancy_r"] for r in results if r.get("expectancy_r") is not None]
        chronological_r = [r.get("realized_r") for r in sorted(closed, key=lambda x: (x.get("entry_date") or "", x.get("expiry") or "")) if r.get("realized_r") is not None]
        equity = peak = max_dd = 0.0
        for value in chronological_r:
            equity += float(value)
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        gross_profit = sum(v for v in chronological_r if v > 0)
        gross_loss = abs(sum(v for v in chronological_r if v < 0))
        summary = {"total_candidates": len(results), "resolved": len(closed), "unresolved": len(results)-len(closed),
                   "realized_win_rate": wins / len(closed) if closed else None, "avg_realized_r": sum(realized_r)/len(realized_r) if realized_r else None,
                   "total_realized_r": sum(realized_r) if realized_r else None, "max_drawdown_r": round(max_dd, 4),
                   "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (None if not gross_profit else "INF"),
                   "avg_model_expectancy_r": sum(exp_r)/len(exp_r) if exp_r else None,
                   "model_vs_realized_gap": (wins / len(closed) - sum(r.get("model_pop") or 0 for r in closed)/len(closed)) if closed else None,
                   "warnings": ["Historical earnings exclusion was not applied because no as-of earnings series is available; do not treat excluded earnings as verified."]}
        with con:
            for i, r in enumerate(results):
                con.execute("INSERT INTO strategy_replay_results(run_id,result_no,result_json) VALUES(?,?,?)", (run_id, i, json.dumps(_json(r), allow_nan=False)))
        con.close()
        _save_run(run_id, status="completed", completed_at=datetime.utcnow().isoformat(timespec="seconds"), progress=100, summary=summary)
        with _lock: _jobs[run_id].update({"status": "completed", "summary": summary})
    except Exception as exc:
        _save_run(run_id, status="failed", completed_at=datetime.utcnow().isoformat(timespec="seconds"), error_text=str(exc))
        with _lock: _jobs[run_id].update({"status": "failed", "error": str(exc)})


@replay_batch_bp.route("/api/runs", methods=["GET"])
def runs():
    _ensure_tables()
    with _connect() as con:
        rows = con.execute("SELECT run_id,run_name,status,created_at,completed_at,progress,summary_json,error_text FROM strategy_replay_runs ORDER BY created_at DESC LIMIT 25").fetchall()
    return jsonify([dict(r) | {"summary": json.loads(r["summary_json"]) if r["summary_json"] else None} for r in rows])


@replay_batch_bp.route("/api/run", methods=["POST"])
def start():
    cfg = _load_config(request.get_json(force=True) or {})
    _ensure_tables(); run_id = uuid.uuid4().hex
    name = str(cfg.get("run_name") or f"Replay {cfg['start_date']} to {cfg['end_date']}")
    now = datetime.utcnow().isoformat(timespec="seconds")
    with _connect() as con:
        con.execute("INSERT INTO strategy_replay_runs(run_id,run_name,status,created_at,started_at,config_json) VALUES(?,?,?,?,?,?)", (run_id,name,"running",now,now,json.dumps(_json(cfg),allow_nan=False)))
    with _lock: _jobs[run_id] = {"status":"running","progress":0}
    ThreadPoolExecutor(max_workers=1).submit(_run, run_id, cfg)
    return jsonify({"ok": True, "run_id": run_id, "run_name": name, "status": "running"}), 202


@replay_batch_bp.route("/api/runs/<run_id>")
def run_detail(run_id: str):
    _ensure_tables()
    with _connect() as con:
        run = con.execute("SELECT * FROM strategy_replay_runs WHERE run_id=?", (run_id,)).fetchone()
        if not run: return jsonify({"ok": False, "error": "run not found"}), 404
        results = [json.loads(r[0]) for r in con.execute("SELECT result_json FROM strategy_replay_results WHERE run_id=? ORDER BY result_no", (run_id,)).fetchall()]
    out = dict(run); out["config"] = json.loads(out.pop("config_json") or "{}"); out["summary"] = json.loads(out.pop("summary_json") or "null"); out["results"] = results
    with _lock: out.update(_jobs.get(run_id, {}))
    return jsonify(out)


@replay_batch_bp.route("/api/health")
def health():
    return jsonify({"ok": True, "description": "manual historical option-chain replay; no startup scheduler"})

@replay_batch_bp.route("/api/recommend")
def recommend():
    """Compare completed named runs without changing live scanner weights."""
    ids = [x.strip() for x in (request.args.get("run_ids") or "").split(",") if x.strip()]
    if not ids:
        return jsonify({"ok": False, "error": "run_ids is required"}), 400
    _ensure_tables()
    rows = []
    with _connect() as con:
        for run_id in ids:
            r = con.execute("SELECT run_id,run_name,status,config_json,summary_json FROM strategy_replay_runs WHERE run_id=?", (run_id,)).fetchone()
            if not r:
                continue
            summary = json.loads(r["summary_json"] or "{}")
            rows.append({"run_id": r["run_id"], "run_name": r["run_name"], "status": r["status"],
                         "config": json.loads(r["config_json"] or "{}"), "summary": summary})
    eligible = [r for r in rows if r["status"] == "completed" and (r["summary"].get("resolved") or 0) >= 10]
    eligible.sort(key=lambda r: (r["summary"].get("total_realized_r") is not None,
                                 r["summary"].get("total_realized_r") or -999), reverse=True)
    best = eligible[0] if eligible else None
    return jsonify({"ok": True, "runs": rows, "recommendation": (
        {"run_id": best["run_id"], "run_name": best["run_name"],
         "reason": "Highest total realized R among completed runs with at least 10 resolved trades; validate drawdown and calibration before adoption."}
        if best else {"status": "INSUFFICIENT_DATA", "reason": "Need completed runs with at least 10 resolved trades."}
    )})

_autopilot_groups: Dict[str, Dict[str, Any]] = {}


def _enqueue(cfg: Dict[str, Any]) -> str:
    _ensure_tables()
    run_id = uuid.uuid4().hex
    name = str(cfg.get("run_name") or f"Replay {cfg['start_date']} to {cfg['end_date']}")
    now = datetime.utcnow().isoformat(timespec="seconds")
    with _connect() as con:
        con.execute(
            "INSERT INTO strategy_replay_runs(run_id,run_name,status,created_at,started_at,config_json) VALUES(?,?,?,?,?,?)",
            (run_id, name, "running", now, now, json.dumps(_json(cfg), allow_nan=False)),
        )
    with _lock:
        _jobs[run_id] = {"status": "running", "progress": 0}
    ThreadPoolExecutor(max_workers=1).submit(_run, run_id, cfg)
    return run_id


@replay_batch_bp.route("/api/autopilot", methods=["POST"])
def autopilot():
    """Launch a bounded, explicit sweep; no unconstrained optimizer."""
    base = _load_config(request.get_json(force=True) or {})
    base_rr = float(base["min_rr"])
    base_oi = float(base["min_strike_oi_pct"])
    rr_values = sorted(set(round(max(0.60, min(1.20, x)), 2) for x in (base_rr, base_rr + 0.20, base_rr + 0.40)))
    oi_values = sorted(set(round(max(0.0, min(30.0, x)), 2) for x in (base_oi, max(base_oi, 10.0), max(base_oi, 20.0))))
    group_id = uuid.uuid4().hex
    run_ids = []
    configs = []
    for rr in rr_values:
        for oi in oi_values:
            cfg = dict(base)
            cfg["min_rr"] = rr
            cfg["min_strike_oi_pct"] = oi
            cfg["run_name"] = f"{base.get('run_name') or 'Autopilot'} | RR>={rr:.2f} | OI>={oi:.0f}%"
            run_ids.append(_enqueue(cfg))
            configs.append({"run_id": run_ids[-1], "min_rr": rr, "min_strike_oi_pct": oi})
    _autopilot_groups[group_id] = {"group_id": group_id, "run_ids": run_ids, "configs": configs}
    return jsonify({"ok": True, "group_id": group_id, "run_ids": run_ids, "configs": configs,
                    "message": "Bounded sweep started. Poll each run, then call /api/recommend with the run IDs."}), 202


@replay_batch_bp.route("/api/autopilot/<group_id>")
def autopilot_status(group_id: str):
    group = _autopilot_groups.get(group_id)
    if not group:
        return jsonify({"ok": False, "error": "autopilot group not found in this process"}), 404
    _ensure_tables()
    with _connect() as con:
        rows = []
        for run_id in group["run_ids"]:
            row = con.execute(
                "SELECT run_id,run_name,status,progress,summary_json,error_text FROM strategy_replay_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row:
                d = dict(row)
                d["summary"] = json.loads(d.pop("summary_json") or "null")
                rows.append(d)
    done = [r for r in rows if r["status"] in {"completed", "failed"}]
    recommendation = None
    if len(done) == len(rows) and rows:
        eligible = [r for r in rows if r["status"] == "completed" and (r["summary"] or {}).get("resolved", 0) >= 10]
        eligible.sort(key=lambda r: ((r["summary"] or {}).get("total_realized_r") is not None,
                                     (r["summary"] or {}).get("total_realized_r") or -999), reverse=True)
        recommendation = ({"run_id": eligible[0]["run_id"], "run_name": eligible[0]["run_name"],
                           "reason": "Best total realized R among variants with at least 10 resolved trades; review drawdown and calibration before adoption."}
                          if eligible else {"status": "INSUFFICIENT_DATA"})
    return jsonify({"ok": True, **group, "runs": rows, "recommendation": recommendation})
