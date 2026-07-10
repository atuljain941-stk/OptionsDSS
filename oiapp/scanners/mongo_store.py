from __future__ import annotations

import io
import logging
import os
import time
import uuid
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd


DEFAULT_MONGO_URI = os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI") or "mongodb://localhost:27017"
DEFAULT_MONGO_DB = os.environ.get("MONGO_DB") or os.environ.get("MONGODB_DB") or "oiapp"
DEFAULT_PROVIDER = (os.environ.get("MARKET_DATA_PROVIDER") or "auto").strip().lower()


def _safe_uri(uri: str) -> str:
    """Return a display-safe MongoDB URI with credentials hidden."""
    raw = str(uri or "")
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(raw)
        netloc = parts.netloc
        if "@" in netloc:
            _auth, host = netloc.rsplit("@", 1)
            netloc = "***:***@" + host
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return raw.replace(raw.split("@", 1)[0], "***:***") if "@" in raw else raw


def _mongo_fix_hint(error: Optional[str], uri: str, db_name: str) -> str:
    e = str(error or "").lower()
    if "no module named" in e or "pymongo" in e and "not installed" in e:
        return "Install pymongo with: pip install pymongo, then restart the Flask app."
    if "connection refused" in e or "serverselectiontimeout" in e or "timed out" in e or "localhost" in e:
        return "Start MongoDB locally, then retry. Default connection is MONGO_URI=mongodb://localhost:27017 and MONGO_DB=oiapp."
    if "authentication" in e or "auth" in e:
        return "Check the username, password, authSource, and database name in MONGO_URI."
    return f"Check that MongoDB is reachable at {_safe_uri(uri)} and that database '{db_name}' is allowed."


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _date_to_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return datetime.strptime(str(value)[:10], "%Y-%m-%d")


def _clean_symbol(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(" ", "")


def _json_safe(value: Any) -> Any:
    # Keep this local so Mongo ObjectIds/datetimes never leak to jsonify.
    try:
        from bson import ObjectId  # type: ignore
    except Exception:  # pragma: no cover - bson only present with pymongo
        ObjectId = ()  # type: ignore
    if ObjectId and isinstance(value, ObjectId):  # type: ignore[arg-type]
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value


@contextmanager
def _suppress_yfinance_noise():
    names = ("yfinance", "yfinance.ticker", "yfinance.multi", "yfinance.scrapers.history")
    states = []
    for name in names:
        logger = logging.getLogger(name)
        states.append((logger, logger.level, logger.disabled, logger.propagate))
        logger.setLevel(logging.CRITICAL + 1)
        logger.disabled = True
        logger.propagate = False
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            yield
    finally:
        for logger, level, disabled, propagate in states:
            logger.setLevel(level)
            logger.disabled = disabled
            logger.propagate = propagate


def normalize_ohlcv(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [str(c[-1] if c[-1] else c[0]).strip().title() for c in out.columns]
    else:
        out.columns = [str(c).strip().title() for c in out.columns]
    # yfinance can return "Adj Close" while the simulator only needs OHLCV.
    needed = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in out.columns]
    if len(needed) < 5:
        return None
    out = out[needed].copy()
    out.index = pd.to_datetime(out.index, errors="coerce")
    out = out[~out.index.isna()]
    try:
        if getattr(out.index, "tz", None) is not None:
            out.index = out.index.tz_convert(None)
    except Exception:
        try:
            out.index = out.index.tz_localize(None)
        except Exception:
            pass
    out = out.sort_index()
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    return out if not out.empty else None


def fetch_yfinance_daily(symbol: str, start: date, end: date) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    try:
        import yfinance as yf
    except Exception as e:
        return None, f"yfinance is not available: {e}"

    symbol = _clean_symbol(symbol)
    if not symbol:
        return None, "symbol is required"
    try:
        kwargs = {
            "start": _date_to_dt(start).date().isoformat(),
            "end": (_date_to_dt(end).date() + timedelta(days=1)).isoformat(),
            "interval": "1d",
            "auto_adjust": False,
        }
        ticker = yf.Ticker(symbol)
        try:
            with _suppress_yfinance_noise():
                df = ticker.history(**kwargs, raise_errors=True)
        except TypeError:
            with _suppress_yfinance_noise():
                df = ticker.history(**kwargs)
        out = normalize_ohlcv(df)
        if out is None or out.empty:
            return None, "no 1d yfinance data"
        return out, None
    except Exception as e:
        return None, str(e) or "no 1d yfinance data"


class MongoStore:
    """Optional MongoDB store for daily market bars and saved backtest runs.

    The class is intentionally safe to import when pymongo is not installed or
    MongoDB is not running. Call status()/available before assuming persistence.
    """

    def __init__(self, uri: Optional[str] = None, db_name: Optional[str] = None, timeout_ms: int = 1500):
        self.uri = uri or DEFAULT_MONGO_URI
        self.db_name = db_name or DEFAULT_MONGO_DB
        self.timeout_ms = int(timeout_ms)
        self._client = None
        self._db = None
        self._connect_error: Optional[str] = None
        self._last_connect_attempt = 0.0

    def _connect(self, force: bool = False):
        if self._db is not None and not force:
            return self._db
        now = time.time()
        # Avoid a Mongo ping for every symbol when MongoDB is offline. Status
        # calls pass force=True so the UI can retry immediately after MongoDB is
        # started.
        if not force and self._connect_error and (now - self._last_connect_attempt) < 30.0:
            return None
        self._last_connect_attempt = now
        if force and self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            self._db = None
        try:
            from pymongo import MongoClient  # type: ignore
        except Exception as e:
            self._connect_error = f"pymongo is not installed or could not be imported: {e}"
            self._client = None
            self._db = None
            return None
        try:
            self._client = MongoClient(self.uri, serverSelectionTimeoutMS=self.timeout_ms)
            self._client.admin.command("ping")
            self._db = self._client[self.db_name]
            self.ensure_indexes()
            self._connect_error = None
            return self._db
        except Exception as e:
            self._connect_error = str(e)
            try:
                if self._client is not None:
                    self._client.close()
            except Exception:
                pass
            self._client = None
            self._db = None
            return None

    @property
    def db(self):
        return self._connect()

    def available(self) -> bool:
        return self._connect() is not None

    def status(self, force: bool = True) -> Dict[str, Any]:
        db = self._connect(force=force)
        out: Dict[str, Any] = {
            "available": bool(db is not None),
            "uri": _safe_uri(self.uri),
            "db": self.db_name,
            "provider": DEFAULT_PROVIDER,
            "timeout_ms": self.timeout_ms,
            "error": self._connect_error,
            "fix_hint": None,
        }
        if db is not None:
            try:
                out.update({
                    "market_bars": int(db.market_bars.estimated_document_count()),
                    "backtest_runs": int(db.backtest_runs.estimated_document_count()),
                    "backtest_trades": int(db.backtest_trades.estimated_document_count()),
                })
            except Exception as e:
                out["error"] = str(e)
        if out.get("error"):
            out["fix_hint"] = _mongo_fix_hint(out.get("error"), self.uri, self.db_name)
        elif not out["available"]:
            out["fix_hint"] = _mongo_fix_hint("MongoDB is unavailable", self.uri, self.db_name)
        return out

    def ensure_indexes(self) -> None:
        if self._db is None:
            return
        try:
            self._db.market_bars.create_index(
                [("symbol", 1), ("interval", 1), ("date", 1)],
                unique=True,
                name="uniq_symbol_interval_date",
            )
            self._db.market_bars.create_index(
                [("interval", 1), ("date", 1)],
                name="interval_date",
            )
            self._db.backtest_runs.create_index([("created_at", -1)], name="created_at_desc")
            self._db.backtest_runs.create_index([("run_id", 1)], unique=True, name="uniq_run_id")
            self._db.backtest_runs.create_index([("config.symbol", 1), ("created_at", -1)], name="symbol_created")
            self._db.backtest_trades.create_index([("run_id", 1), ("symbol", 1)], name="run_symbol")
            self._db.backtest_trades.create_index([("symbol", 1), ("entry_date", -1)], name="trade_symbol_entry")
        except Exception as e:
            self._connect_error = str(e)

    def bars_to_frame(self, rows: List[Dict[str, Any]]) -> Optional[pd.DataFrame]:
        if not rows:
            return None
        recs = []
        for r in rows:
            recs.append({
                "Date": r.get("date"),
                "Open": r.get("open"),
                "High": r.get("high"),
                "Low": r.get("low"),
                "Close": r.get("close"),
                "Volume": r.get("volume"),
            })
        df = pd.DataFrame(recs)
        if df.empty:
            return None
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date"]).set_index("Date").sort_index()
        for c in ["Open", "High", "Low", "Close", "Volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        return df if not df.empty else None

    def get_bars(self, symbol: str, start: date, end: date, interval: str = "1d") -> Optional[pd.DataFrame]:
        db = self._connect()
        if db is None:
            return None
        symbol = _clean_symbol(symbol)
        try:
            rows = list(db.market_bars.find(
                {
                    "symbol": symbol,
                    "interval": interval,
                    "date": {"$gte": _date_to_dt(start), "$lte": _date_to_dt(end)},
                },
                {"_id": 0, "date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            ).sort("date", 1))
            return self.bars_to_frame(rows)
        except Exception as e:
            self._connect_error = str(e)
            return None

    def upsert_bars(self, symbol: str, df: pd.DataFrame, interval: str = "1d") -> int:
        db = self._connect()
        if db is None or df is None or df.empty:
            return 0
        try:
            from pymongo import UpdateOne  # type: ignore
            symbol = _clean_symbol(symbol)
            now = _utc_now()
            ops = []
            clean = normalize_ohlcv(df)
            if clean is None or clean.empty:
                return 0
            for idx, row in clean.iterrows():
                dt = _date_to_dt(idx.date() if hasattr(idx, "date") else idx)
                doc = {
                    "symbol": symbol,
                    "interval": interval,
                    "date": dt,
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(float(row["Volume"] or 0)),
                    "source": "yfinance",
                    "updated_at": now,
                }
                ops.append(UpdateOne(
                    {"symbol": symbol, "interval": interval, "date": dt},
                    {"$set": doc, "$setOnInsert": {"created_at": now}},
                    upsert=True,
                ))
            if not ops:
                return 0
            db.market_bars.bulk_write(ops, ordered=False)
            return len(ops)
        except Exception as e:
            self._connect_error = str(e)
            return 0

    def ensure_daily_history(
        self,
        symbol: str,
        start: date,
        end: date,
        provider: str = "auto",
        auto_fetch: bool = True,
    ) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
        """Return daily history, using Mongo first and yfinance as fill source.

        provider values:
          auto     -> use Mongo if available; fetch missing data from yfinance
          mongo    -> Mongo only; no yfinance fallback
          yfinance -> yfinance only; do not read or write Mongo
        """
        provider = (provider or DEFAULT_PROVIDER or "auto").strip().lower()
        if provider not in {"auto", "mongo", "yfinance"}:
            provider = "auto"
        symbol = _clean_symbol(symbol)
        meta: Dict[str, Any] = {"symbol": symbol, "provider": provider, "source": None, "mongo_available": self.available()}

        if provider != "yfinance" and meta["mongo_available"]:
            cached = self.get_bars(symbol, start, end, "1d")
            if cached is not None and not cached.empty:
                # Trust the cache only when it covers both ends of the requested
                # range.  Otherwise yfinance fills the missing section and the
                # merged range is re-read from MongoDB.
                first_day = cached.index[0].date()
                last_day = cached.index[-1].date()
                covers_start = first_day <= (start + timedelta(days=7))
                covers_end = last_day >= (end - timedelta(days=7))
                if (covers_start and covers_end) or not auto_fetch:
                    meta.update({"source": "mongodb", "bars": len(cached), "first_date": first_day.isoformat(), "last_date": last_day.isoformat()})
                    return cached, meta

        if provider == "mongo" or not auto_fetch:
            meta.update({"source": "mongodb", "error": self._connect_error or "no cached daily data"})
            return None, meta

        df, err = fetch_yfinance_daily(symbol, start, end)
        if df is None or df.empty:
            meta.update({"source": "yfinance", "error": err or "no yfinance data"})
            return None, meta

        saved = 0
        if provider != "yfinance" and meta.get("mongo_available"):
            saved = self.upsert_bars(symbol, df, "1d")
            cached = self.get_bars(symbol, start, end, "1d")
            if cached is not None and not cached.empty:
                df = cached
                meta["source"] = "mongodb+yfinance_fill"
            else:
                meta["source"] = "yfinance"
        else:
            meta["source"] = "yfinance"
        meta.update({"bars": len(df), "saved_bars": saved, "first_date": df.index[0].date().isoformat(), "last_date": df.index[-1].date().isoformat()})
        return df, meta

    def sync_daily_symbols(self, symbols: Iterable[str], years: int = 5, end: Optional[date] = None) -> Dict[str, Any]:
        db = self._connect(force=True)
        if db is None:
            return {"ok": False, "error": self._connect_error or "MongoDB is unavailable", "symbols": []}
        symbol_list = list(dict.fromkeys([_clean_symbol(s) for s in symbols if _clean_symbol(s)]))
        end_day = end or date.today()
        start_day = end_day - timedelta(days=max(1, int(years)) * 365)
        results: List[Dict[str, Any]] = []
        total_saved = 0
        for raw in symbol_list:
            sym = _clean_symbol(raw)
            if not sym:
                continue
            df, err = fetch_yfinance_daily(sym, start_day, end_day)
            if df is None or df.empty:
                results.append({"symbol": sym, "ok": False, "error": err or "no data", "saved_bars": 0})
                continue
            saved = self.upsert_bars(sym, df, "1d")
            total_saved += saved
            results.append({
                "symbol": sym,
                "ok": True,
                "bars": len(df),
                "saved_bars": saved,
                "first_date": df.index[0].date().isoformat(),
                "last_date": df.index[-1].date().isoformat(),
            })
            try:
                db.market_data_sync.update_one(
                    {"symbol": sym, "interval": "1d"},
                    {"$set": {"symbol": sym, "interval": "1d", "years": int(years), "bars": len(df), "saved_bars": saved, "first_date": results[-1]["first_date"], "last_date": results[-1]["last_date"], "updated_at": _utc_now(), "source": "yfinance"}},
                    upsert=True,
                )
            except Exception:
                pass
        return {
            "ok": True,
            "years": int(years),
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "symbols_requested": len(symbol_list),
            "symbols_ok": sum(1 for r in results if r.get("ok")),
            "symbols_failed": sum(1 for r in results if not r.get("ok")),
            "total_saved_bars": total_saved,
            "symbols": results,
        }

    def save_backtest_result(self, result: Dict[str, Any], run_name: Optional[str] = None) -> Dict[str, Any]:
        db = self._connect(force=True)
        if db is None:
            return {"ok": False, "error": self._connect_error or "MongoDB is unavailable"}
        run_id = str(result.get("run_id") or uuid.uuid4())
        created_at = _utc_now()
        config = dict(result.get("config") or {})
        stats = dict(result.get("stats") or {})
        trades = list(result.get("trades") or [])
        run_doc = {
            "run_id": run_id,
            "run_name": str(run_name or result.get("run_name") or config.get("strategy_label") or "Backtest Run").strip()[:160],
            "created_at": created_at,
            "engine": result.get("engine"),
            "config": config,
            "stats": stats,
            "symbol_performance": result.get("symbol_performance") or [],
            "strategy_summary": result.get("strategy_summary") or [],
            "strategy_symbol_summary": result.get("strategy_symbol_summary") or [],
            "daily_log": result.get("daily_log") or [],
            "open_trades": result.get("open_trades") or [],
            "notes": result.get("notes") or [],
            "errors": result.get("errors") or [],
            "skipped_count": len(result.get("skipped") or []),
            "skipped_sample": (result.get("skipped") or [])[:100],
        }
        try:
            db.backtest_runs.update_one({"run_id": run_id}, {"$set": run_doc}, upsert=True)
            db.backtest_trades.delete_many({"run_id": run_id})
            if trades:
                docs = []
                for i, t in enumerate(trades):
                    d = dict(t)
                    d["run_id"] = run_id
                    d["trade_no"] = i + 1
                    d["created_at"] = created_at
                    docs.append(d)
                db.backtest_trades.insert_many(docs, ordered=False)
            return {"ok": True, "run_id": run_id, "trades_saved": len(trades)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def list_backtest_runs(self, limit: int = 50) -> Dict[str, Any]:
        db = self._connect(force=True)
        if db is None:
            return {"ok": False, "error": self._connect_error or "MongoDB is unavailable", "runs": []}
        limit = max(1, min(int(limit or 50), 200))
        try:
            projection = {
                "_id": 0,
                "run_id": 1,
                "run_name": 1,
                "created_at": 1,
                "engine": 1,
                "config.strategy_label": 1,
                "config.scanner_name": 1,
                "config.query_text": 1,
                "config.watchlist_id": 1,
                "config.symbol": 1,
                "config.start_date": 1,
                "config.end_date": 1,
                "config.trade_type": 1,
                "config.dte": 1,
                "config.universe_count": 1,
                "stats": 1,
            }
            rows = list(db.backtest_runs.find({}, projection).sort("created_at", -1).limit(limit))
            return {"ok": True, "runs": _json_safe(rows)}
        except Exception as e:
            return {"ok": False, "error": str(e), "runs": []}

    def get_backtest_run(self, run_id: str) -> Dict[str, Any]:
        db = self._connect(force=True)
        if db is None:
            return {"ok": False, "error": self._connect_error or "MongoDB is unavailable"}
        try:
            run = db.backtest_runs.find_one({"run_id": str(run_id)}, {"_id": 0})
            if not run:
                return {"ok": False, "error": "Run not found"}
            trades = list(db.backtest_trades.find({"run_id": str(run_id)}, {"_id": 0}).sort("trade_no", 1))
            run["trades"] = trades
            return {"ok": True, "run": _json_safe(run)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


_store: Optional[MongoStore] = None


def get_mongo_store() -> MongoStore:
    global _store
    if _store is None:
        _store = MongoStore()
    return _store
