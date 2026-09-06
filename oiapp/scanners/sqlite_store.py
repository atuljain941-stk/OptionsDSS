from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQLITE_MARKET_DB = (
    os.environ.get("MARKET_DATA_DB")
    or os.environ.get("BACKTEST_DB_PATH")
    or str(PROJECT_ROOT / "data" / "market_data.db")
)
DEFAULT_PROVIDER = (os.environ.get("MARKET_DATA_PROVIDER") or "auto").strip().lower()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clean_symbol(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(" ", "")


def _date_to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _date_text(value: Any) -> str:
    return _date_to_date(value).isoformat()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value


def _dumps(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


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
            "start": _date_text(start),
            "end": (_date_to_date(end) + timedelta(days=1)).isoformat(),
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


class SQLiteMarketDataStore:
    """Local SQLite store for historical OHLCV and saved backtest runs.

    This store intentionally uses a separate database file from options_data.db
    so market-data syncs and source-only packages cannot overwrite the user's
    application/config database.
    """

    def __init__(self, db_path: Optional[str] = None, timeout: float = 30.0):
        self.db_path = str(db_path or DEFAULT_SQLITE_MARKET_DB)
        self.timeout = float(timeout)
        self._schema_error: Optional[str] = None
        self._last_schema_check = 0.0
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=self.timeout, check_same_thread=False)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        return con

    def ensure_schema(self) -> None:
        now = time.time()
        if self._schema_error is None and (now - self._last_schema_check) < 5.0:
            return
        self._last_schema_check = now
        try:
            with self._connect() as con:
                con.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS market_bars (
                        symbol TEXT NOT NULL,
                        interval TEXT NOT NULL,
                        bar_time TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        adj_close REAL,
                        volume INTEGER,
                        source TEXT,
                        fetched_at TEXT,
                        updated_at TEXT,
                        PRIMARY KEY (symbol, interval, bar_time)
                    );
                    CREATE INDEX IF NOT EXISTS idx_market_bars_symbol_interval_time
                        ON market_bars(symbol, interval, bar_time);
                    CREATE INDEX IF NOT EXISTS idx_market_bars_interval_time
                        ON market_bars(interval, bar_time);

                    CREATE TABLE IF NOT EXISTS market_data_sync (
                        symbol TEXT NOT NULL,
                        interval TEXT NOT NULL,
                        years INTEGER,
                        bars INTEGER,
                        saved_bars INTEGER,
                        first_date TEXT,
                        last_date TEXT,
                        source TEXT,
                        updated_at TEXT,
                        PRIMARY KEY (symbol, interval)
                    );

                    CREATE TABLE IF NOT EXISTS backtest_runs (
                        run_id TEXT PRIMARY KEY,
                        run_name TEXT,
                        created_at TEXT,
                        engine TEXT,
                        config_json TEXT,
                        stats_json TEXT,
                        symbol_performance_json TEXT,
                        strategy_summary_json TEXT,
                        strategy_symbol_summary_json TEXT,
                        daily_log_json TEXT,
                        open_trades_json TEXT,
                        notes_json TEXT,
                        errors_json TEXT,
                        skipped_count INTEGER DEFAULT 0,
                        skipped_sample_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_backtest_runs_created_at
                        ON backtest_runs(created_at DESC);

                    CREATE TABLE IF NOT EXISTS backtest_trades (
                        run_id TEXT NOT NULL,
                        trade_no INTEGER NOT NULL,
                        symbol TEXT,
                        entry_date TEXT,
                        expiry_date TEXT,
                        trade_type TEXT,
                        winner INTEGER,
                        price_change_pct REAL,
                        trade_json TEXT,
                        created_at TEXT,
                        PRIMARY KEY (run_id, trade_no),
                        FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_symbol
                        ON backtest_trades(run_id, symbol);
                    CREATE INDEX IF NOT EXISTS idx_backtest_trades_symbol_entry
                        ON backtest_trades(symbol, entry_date DESC);

                    CREATE TABLE IF NOT EXISTS backtest_signals (
                        run_id TEXT NOT NULL,
                        signal_no INTEGER NOT NULL,
                        signal_date TEXT,
                        module TEXT,
                        symbol TEXT,
                        strategy TEXT,
                        status TEXT,
                        signal_json TEXT,
                        created_at TEXT,
                        PRIMARY KEY (run_id, signal_no),
                        FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_backtest_signals_run_symbol
                        ON backtest_signals(run_id, symbol);
                    CREATE INDEX IF NOT EXISTS idx_backtest_signals_date
                        ON backtest_signals(signal_date DESC);

                    CREATE TABLE IF NOT EXISTS api_test_runs (
                        run_id TEXT PRIMARY KEY,
                        run_name TEXT,
                        created_at TEXT,
                        base_url TEXT,
                        top_n INTEGER,
                        timeout_secs REAL,
                        request_count INTEGER,
                        ok_count INTEGER,
                        failed_count INTEGER,
                        avg_ms REAL,
                        min_ms REAL,
                        max_ms REAL,
                        config_json TEXT,
                        slowest_json TEXT,
                        notes_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_api_test_runs_created_at
                        ON api_test_runs(created_at DESC);

                    CREATE TABLE IF NOT EXISTS api_test_results (
                        run_id TEXT NOT NULL,
                        endpoint_no INTEGER NOT NULL,
                        endpoint_key TEXT,
                        method TEXT,
                        path TEXT,
                        label TEXT,
                        status_code INTEGER,
                        elapsed_ms REAL,
                        ok INTEGER,
                        error TEXT,
                        response_size INTEGER,
                        content_type TEXT,
                        response_preview TEXT,
                        started_at TEXT,
                        created_at TEXT,
                        result_json TEXT,
                        PRIMARY KEY (run_id, endpoint_no),
                        FOREIGN KEY (run_id) REFERENCES api_test_runs(run_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_api_test_results_run_elapsed
                        ON api_test_results(run_id, elapsed_ms DESC);
                    CREATE INDEX IF NOT EXISTS idx_api_test_results_path
                        ON api_test_results(path);
                    """
                )
            self._schema_error = None
        except Exception as e:
            self._schema_error = str(e)

    def available(self) -> bool:
        self.ensure_schema()
        return self._schema_error is None

    def status(self, force: bool = True) -> Dict[str, Any]:
        self.ensure_schema()
        out: Dict[str, Any] = {
            "available": self.available(),
            "store": "sqlite",
            "provider": DEFAULT_PROVIDER,
            "path": self.db_path,
            "error": self._schema_error,
            "fix_hint": None,
        }
        try:
            with self._connect() as con:
                out["market_bars"] = int(con.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0])
                out["backtest_runs"] = int(con.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0])
                out["backtest_trades"] = int(con.execute("SELECT COUNT(*) FROM backtest_trades").fetchone()[0])
                row = con.execute(
                    "SELECT MIN(bar_time), MAX(bar_time), COUNT(DISTINCT symbol) FROM market_bars WHERE interval='1d'"
                ).fetchone()
                out["first_date"] = row[0] if row and row[0] else None
                out["last_date"] = row[1] if row and row[1] else None
                out["symbols"] = int(row[2] or 0) if row else 0
        except Exception as e:
            out["available"] = False
            out["error"] = str(e)
        if out.get("error"):
            out["fix_hint"] = "Check write permission for the data folder or set MARKET_DATA_DB to a writable path."
        return out

    def bars_to_frame(self, rows: List[sqlite3.Row]) -> Optional[pd.DataFrame]:
        if not rows:
            return None
        recs = []
        for r in rows:
            recs.append({
                "Date": r["bar_time"],
                "Open": r["open"],
                "High": r["high"],
                "Low": r["low"],
                "Close": r["close"],
                "Volume": r["volume"],
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
        self.ensure_schema()
        if self._schema_error:
            return None
        symbol = _clean_symbol(symbol)
        try:
            with self._connect() as con:
                rows = con.execute(
                    """
                    SELECT bar_time, open, high, low, close, volume
                    FROM market_bars
                    WHERE symbol=? AND interval=? AND bar_time>=? AND bar_time<=?
                    ORDER BY bar_time ASC
                    """,
                    (symbol, interval, _date_text(start), _date_text(end)),
                ).fetchall()
            return self.bars_to_frame(rows)
        except Exception as e:
            self._schema_error = str(e)
            return None

    def upsert_bars(self, symbol: str, df: pd.DataFrame, interval: str = "1d") -> int:
        self.ensure_schema()
        if self._schema_error or df is None or df.empty:
            return 0
        clean = normalize_ohlcv(df)
        if clean is None or clean.empty:
            return 0
        symbol = _clean_symbol(symbol)
        now = _utc_now().isoformat(timespec="seconds")
        rows = []
        for idx, row in clean.iterrows():
            bar_time = _date_text(idx.date() if hasattr(idx, "date") else idx)
            rows.append((
                symbol,
                interval,
                bar_time,
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
                float(row["Close"]),
                int(float(row["Volume"] or 0)),
                "yfinance",
                now,
                now,
            ))
        if not rows:
            return 0
        try:
            with self._connect() as con:
                con.executemany(
                    """
                    INSERT INTO market_bars
                        (symbol, interval, bar_time, open, high, low, close, adj_close, volume, source, fetched_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, interval, bar_time) DO UPDATE SET
                        open=excluded.open,
                        high=excluded.high,
                        low=excluded.low,
                        close=excluded.close,
                        adj_close=excluded.adj_close,
                        volume=excluded.volume,
                        source=excluded.source,
                        fetched_at=excluded.fetched_at,
                        updated_at=excluded.updated_at
                    """,
                    rows,
                )
            return len(rows)
        except Exception as e:
            self._schema_error = str(e)
            return 0

    def ensure_daily_history(
        self,
        symbol: str,
        start: date,
        end: date,
        provider: str = "auto",
        auto_fetch: bool = True,
    ) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
        """Return daily history, using SQLite cache first and yfinance as fill source.

        provider values:
          auto     -> use SQLite cache; fetch missing data from yfinance
          sqlite   -> SQLite only; no yfinance fallback
          yfinance -> yfinance only; do not read or write SQLite
        """
        provider = (provider or DEFAULT_PROVIDER or "auto").strip().lower()
        if provider == "mongo":
            # Backward compatibility with older UI payloads.
            provider = "sqlite"
        if provider not in {"auto", "sqlite", "yfinance"}:
            provider = "auto"
        symbol = _clean_symbol(symbol)
        meta: Dict[str, Any] = {"symbol": symbol, "provider": provider, "source": None, "sqlite_available": self.available(), "path": self.db_path}

        if provider != "yfinance" and meta["sqlite_available"]:
            cached = self.get_bars(symbol, start, end, "1d")
            if cached is not None and not cached.empty:
                first_day = cached.index[0].date()
                last_day = cached.index[-1].date()
                covers_start = first_day <= (start + timedelta(days=7))
                covers_end = last_day >= (end - timedelta(days=7))
                if (covers_start and covers_end) or not auto_fetch:
                    meta.update({"source": "sqlite", "bars": len(cached), "first_date": first_day.isoformat(), "last_date": last_day.isoformat()})
                    return cached, meta

        if provider == "sqlite" or not auto_fetch:
            meta.update({"source": "sqlite", "error": self._schema_error or "no cached daily data"})
            return None, meta

        df, err = fetch_yfinance_daily(symbol, start, end)
        if df is None or df.empty:
            meta.update({"source": "yfinance", "error": err or "no yfinance data"})
            return None, meta

        saved = 0
        if provider != "yfinance" and meta.get("sqlite_available"):
            saved = self.upsert_bars(symbol, df, "1d")
            cached = self.get_bars(symbol, start, end, "1d")
            if cached is not None and not cached.empty:
                df = cached
                meta["source"] = "sqlite+yfinance_fill"
            else:
                meta["source"] = "yfinance"
        else:
            meta["source"] = "yfinance"
        meta.update({"bars": len(df), "saved_bars": saved, "first_date": df.index[0].date().isoformat(), "last_date": df.index[-1].date().isoformat()})
        return df, meta

    def sync_daily_symbols(self, symbols: Iterable[str], years: int = 5, end: Optional[date] = None) -> Dict[str, Any]:
        self.ensure_schema()
        if self._schema_error:
            return {"ok": False, "error": self._schema_error, "symbols": [], "store": "sqlite", "path": self.db_path}
        symbol_list = list(dict.fromkeys([_clean_symbol(s) for s in symbols if _clean_symbol(s)]))
        end_day = end or date.today()
        start_day = end_day - timedelta(days=max(1, int(years)) * 365)
        results: List[Dict[str, Any]] = []
        total_saved = 0
        now = _utc_now().isoformat(timespec="seconds")
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
            first = df.index[0].date().isoformat()
            last = df.index[-1].date().isoformat()
            results.append({"symbol": sym, "ok": True, "bars": len(df), "saved_bars": saved, "first_date": first, "last_date": last})
            try:
                with self._connect() as con:
                    con.execute(
                        """
                        INSERT INTO market_data_sync
                            (symbol, interval, years, bars, saved_bars, first_date, last_date, source, updated_at)
                        VALUES (?, '1d', ?, ?, ?, ?, ?, 'yfinance', ?)
                        ON CONFLICT(symbol, interval) DO UPDATE SET
                            years=excluded.years,
                            bars=excluded.bars,
                            saved_bars=excluded.saved_bars,
                            first_date=excluded.first_date,
                            last_date=excluded.last_date,
                            source=excluded.source,
                            updated_at=excluded.updated_at
                        """,
                        (sym, int(years), len(df), saved, first, last, now),
                    )
            except Exception:
                pass
        return {
            "ok": True,
            "store": "sqlite",
            "path": self.db_path,
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
        self.ensure_schema()
        if self._schema_error:
            return {"ok": False, "error": self._schema_error, "store": "sqlite"}
        run_id = str(result.get("run_id") or uuid.uuid4())
        created_at = str(result.get("created_at") or _utc_now().isoformat(timespec="seconds"))
        config = dict(result.get("config") or {})
        stats = dict(result.get("stats") or {})
        trades = list(result.get("trades") or [])
        signals = list(result.get("signals") or [])
        run_name_val = str(run_name or result.get("run_name") or config.get("strategy_label") or "Backtest Run").strip()[:160]
        try:
            with self._connect() as con:
                con.execute(
                    """
                    INSERT INTO backtest_runs
                        (run_id, run_name, created_at, engine, config_json, stats_json,
                         symbol_performance_json, strategy_summary_json, strategy_symbol_summary_json,
                         daily_log_json, open_trades_json, notes_json, errors_json,
                         skipped_count, skipped_sample_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        run_name=excluded.run_name,
                        created_at=excluded.created_at,
                        engine=excluded.engine,
                        config_json=excluded.config_json,
                        stats_json=excluded.stats_json,
                        symbol_performance_json=excluded.symbol_performance_json,
                        strategy_summary_json=excluded.strategy_summary_json,
                        strategy_symbol_summary_json=excluded.strategy_symbol_summary_json,
                        daily_log_json=excluded.daily_log_json,
                        open_trades_json=excluded.open_trades_json,
                        notes_json=excluded.notes_json,
                        errors_json=excluded.errors_json,
                        skipped_count=excluded.skipped_count,
                        skipped_sample_json=excluded.skipped_sample_json
                    """,
                    (
                        run_id,
                        run_name_val,
                        created_at,
                        result.get("engine"),
                        _dumps(config),
                        _dumps(stats),
                        _dumps(result.get("symbol_performance") or []),
                        _dumps(result.get("strategy_summary") or []),
                        _dumps(result.get("strategy_symbol_summary") or []),
                        _dumps(result.get("daily_log") or []),
                        _dumps(result.get("open_trades") or []),
                        _dumps(result.get("notes") or []),
                        _dumps(result.get("errors") or []),
                        len(result.get("skipped") or []),
                        _dumps((result.get("skipped") or [])[:100]),
                    ),
                )
                con.execute("DELETE FROM backtest_trades WHERE run_id=?", (run_id,))
                rows = []
                for i, t in enumerate(trades):
                    rows.append((
                        run_id,
                        i + 1,
                        str(t.get("symbol") or ""),
                        str(t.get("entry_date") or ""),
                        str(t.get("expiry_date") or t.get("exit_date") or ""),
                        str(t.get("trade_type") or ""),
                        1 if t.get("winner") else 0,
                        float(t.get("price_change_pct")) if t.get("price_change_pct") is not None else None,
                        _dumps(t),
                        created_at,
                    ))
                if rows:
                    con.executemany(
                        """
                        INSERT INTO backtest_trades
                            (run_id, trade_no, symbol, entry_date, expiry_date, trade_type, winner, price_change_pct, trade_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                con.execute("DELETE FROM backtest_signals WHERE run_id=?", (run_id,))
                sig_rows = []
                for i, sig in enumerate(signals):
                    sig_rows.append((
                        run_id,
                        i + 1,
                        str(sig.get("date") or sig.get("entry_date") or ""),
                        str(sig.get("module") or ""),
                        str(sig.get("symbol") or ""),
                        str(sig.get("strategy") or sig.get("trade_type") or ""),
                        str(sig.get("status") or ""),
                        _dumps(sig),
                        created_at,
                    ))
                if sig_rows:
                    con.executemany(
                        """
                        INSERT INTO backtest_signals
                            (run_id, signal_no, signal_date, module, symbol, strategy, status, signal_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        sig_rows,
                    )
            return {"ok": True, "run_id": run_id, "trades_saved": len(trades), "signals_saved": len(signals), "store": "sqlite", "path": self.db_path}
        except Exception as e:
            return {"ok": False, "error": str(e), "store": "sqlite"}

    def list_backtest_runs(self, limit: int = 50, run_name: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
        self.ensure_schema()
        if self._schema_error:
            return {"ok": False, "error": self._schema_error, "runs": [], "store": "sqlite"}
        limit = max(1, min(int(limit or 50), 200))
        try:
            clauses = []
            params: list[Any] = []
            if run_name:
                clauses.append("run_name LIKE ?")
                params.append(f"%{str(run_name).strip()}%")
            if date_from:
                clauses.append("datetime(created_at) >= datetime(?)")
                params.append(str(date_from).strip())
            if date_to:
                clauses.append("datetime(created_at) <= datetime(?)")
                params.append(str(date_to).strip() + ' 23:59:59')
            where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
            with self._connect() as con:
                rows = con.execute(
                    f"""
                    SELECT run_id, run_name, created_at, engine, config_json, stats_json
                    FROM backtest_runs
                    {where}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    params + [limit],
                ).fetchall()
            out = []
            for r in rows:
                config = _loads(r["config_json"], {}) or {}
                stats = _loads(r["stats_json"], {}) or {}
                out.append({
                    "run_id": r["run_id"],
                    "run_name": r["run_name"],
                    "created_at": r["created_at"],
                    "engine": r["engine"],
                    "config": config,
                    "stats": stats,
                })
            return {"ok": True, "store": "sqlite", "runs": out}
        except Exception as e:
            return {"ok": False, "error": str(e), "runs": [], "store": "sqlite"}

    def get_backtest_run(self, run_id: str) -> Dict[str, Any]:
        self.ensure_schema()
        if self._schema_error:
            return {"ok": False, "error": self._schema_error, "store": "sqlite"}
        try:
            with self._connect() as con:
                r = con.execute("SELECT * FROM backtest_runs WHERE run_id=?", (str(run_id),)).fetchone()
                if not r:
                    return {"ok": False, "error": "Run not found", "store": "sqlite"}
                trade_rows = con.execute(
                    "SELECT trade_json FROM backtest_trades WHERE run_id=? ORDER BY trade_no ASC",
                    (str(run_id),),
                ).fetchall()
                try:
                    signal_rows = con.execute(
                        "SELECT signal_json FROM backtest_signals WHERE run_id=? ORDER BY signal_no ASC",
                        (str(run_id),),
                    ).fetchall()
                except Exception:
                    signal_rows = []
            run = {
                "run_id": r["run_id"],
                "run_name": r["run_name"],
                "created_at": r["created_at"],
                "engine": r["engine"],
                "config": _loads(r["config_json"], {}) or {},
                "stats": _loads(r["stats_json"], {}) or {},
                "symbol_performance": _loads(r["symbol_performance_json"], []) or [],
                "strategy_summary": _loads(r["strategy_summary_json"], []) or [],
                "strategy_symbol_summary": _loads(r["strategy_symbol_summary_json"], []) or [],
                "daily_log": _loads(r["daily_log_json"], []) or [],
                "open_trades": _loads(r["open_trades_json"], []) or [],
                "notes": _loads(r["notes_json"], []) or [],
                "errors": _loads(r["errors_json"], []) or [],
                "skipped_count": int(r["skipped_count"] or 0),
                "skipped_sample": _loads(r["skipped_sample_json"], []) or [],
                "trades": [_loads(t["trade_json"], {}) or {} for t in trade_rows],
                "signals": [_loads(t["signal_json"], {}) or {} for t in signal_rows],
            }
            return {"ok": True, "store": "sqlite", "run": _json_safe(run)}
        except Exception as e:
            return {"ok": False, "error": str(e), "store": "sqlite"}

    def save_api_test_result(self, result: Dict[str, Any], run_name: Optional[str] = None) -> Dict[str, Any]:
        self.ensure_schema()
        if self._schema_error:
            return {"ok": False, "error": self._schema_error, "store": "sqlite"}
        run_id = str(result.get("run_id") or uuid.uuid4())
        created_at = str(result.get("created_at") or _utc_now().isoformat(timespec="seconds"))
        config = dict(result.get("config") or {})
        endpoints = list(result.get("results") or [])
        run_name_val = str(run_name or result.get("run_name") or config.get("run_name") or "API Tester Run").strip()[:160]
        elapsed_vals = [float(r.get("elapsed_ms") or 0.0) for r in endpoints if r.get("elapsed_ms") is not None]
        avg_ms = (sum(elapsed_vals) / len(elapsed_vals)) if elapsed_vals else None
        min_ms = min(elapsed_vals) if elapsed_vals else None
        max_ms = max(elapsed_vals) if elapsed_vals else None
        ok_count = sum(1 for r in endpoints if r.get("ok"))
        failed_count = sum(1 for r in endpoints if not r.get("ok"))
        slowest = sorted(endpoints, key=lambda x: float(x.get("elapsed_ms") or 0.0), reverse=True)[: int(result.get("top_n") or config.get("top_n") or 10)]
        try:
            with self._connect() as con:
                con.execute(
                    """
                    INSERT INTO api_test_runs
                        (run_id, run_name, created_at, base_url, top_n, timeout_secs, request_count, ok_count, failed_count, avg_ms, min_ms, max_ms, config_json, slowest_json, notes_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        run_name=excluded.run_name,
                        created_at=excluded.created_at,
                        base_url=excluded.base_url,
                        top_n=excluded.top_n,
                        timeout_secs=excluded.timeout_secs,
                        request_count=excluded.request_count,
                        ok_count=excluded.ok_count,
                        failed_count=excluded.failed_count,
                        avg_ms=excluded.avg_ms,
                        min_ms=excluded.min_ms,
                        max_ms=excluded.max_ms,
                        config_json=excluded.config_json,
                        slowest_json=excluded.slowest_json,
                        notes_json=excluded.notes_json
                    """,
                    (
                        run_id,
                        run_name_val,
                        created_at,
                        str(result.get("base_url") or config.get("base_url") or ""),
                        int(result.get("top_n") or config.get("top_n") or 10),
                        float(result.get("timeout_secs") or config.get("timeout_secs") or 20),
                        len(endpoints),
                        ok_count,
                        failed_count,
                        avg_ms,
                        min_ms,
                        max_ms,
                        _dumps(config),
                        _dumps(slowest),
                        _dumps(result.get("notes") or []),
                    ),
                )
                con.execute("DELETE FROM api_test_results WHERE run_id=?", (run_id,))
                rows = []
                for i, r in enumerate(endpoints):
                    rows.append((
                        run_id,
                        i + 1,
                        str(r.get("endpoint_key") or ""),
                        str(r.get("method") or "GET"),
                        str(r.get("path") or ""),
                        str(r.get("label") or ""),
                        int(r.get("status_code") or 0) if r.get("status_code") is not None else None,
                        float(r.get("elapsed_ms") or 0.0) if r.get("elapsed_ms") is not None else None,
                        1 if r.get("ok") else 0,
                        str(r.get("error") or ""),
                        int(r.get("response_size") or 0) if r.get("response_size") is not None else None,
                        str(r.get("content_type") or ""),
                        str(r.get("response_preview") or ""),
                        str(r.get("started_at") or ""),
                        created_at,
                        _dumps(r),
                    ))
                if rows:
                    con.executemany(
                        """
                        INSERT INTO api_test_results
                            (run_id, endpoint_no, endpoint_key, method, path, label, status_code, elapsed_ms, ok, error, response_size, content_type, response_preview, started_at, created_at, result_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
            return {"ok": True, "run_id": run_id, "results_saved": len(endpoints), "ok_count": ok_count, "failed_count": failed_count, "store": "sqlite", "path": self.db_path}
        except Exception as e:
            return {"ok": False, "error": str(e), "store": "sqlite"}

    def list_api_test_runs(self, limit: int = 50, run_name: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
        self.ensure_schema()
        if self._schema_error:
            return {"ok": False, "error": self._schema_error, "runs": [], "store": "sqlite"}
        limit = max(1, min(int(limit or 50), 200))
        try:
            clauses = []
            params: list[Any] = []
            if run_name:
                clauses.append("run_name LIKE ?")
                params.append(f"%{str(run_name).strip()}%")
            if date_from:
                clauses.append("datetime(created_at) >= datetime(?)")
                params.append(str(date_from).strip())
            if date_to:
                clauses.append("datetime(created_at) <= datetime(?)")
                params.append(str(date_to).strip() + ' 23:59:59')
            where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
            with self._connect() as con:
                rows = con.execute(
                    f"""
                    SELECT run_id, run_name, created_at, base_url, top_n, timeout_secs, request_count, ok_count, failed_count, avg_ms, min_ms, max_ms, config_json, slowest_json, notes_json
                    FROM api_test_runs
                    {where}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    params + [limit],
                ).fetchall()
            out = []
            for r in rows:
                out.append({
                    "run_id": r["run_id"],
                    "run_name": r["run_name"],
                    "created_at": r["created_at"],
                    "base_url": r["base_url"],
                    "top_n": r["top_n"],
                    "timeout_secs": r["timeout_secs"],
                    "request_count": r["request_count"],
                    "ok_count": r["ok_count"],
                    "failed_count": r["failed_count"],
                    "avg_ms": r["avg_ms"],
                    "min_ms": r["min_ms"],
                    "max_ms": r["max_ms"],
                    "config": _loads(r["config_json"], {}) or {},
                    "slowest": _loads(r["slowest_json"], []) or [],
                    "notes": _loads(r["notes_json"], []) or [],
                })
            return {"ok": True, "store": "sqlite", "runs": out}
        except Exception as e:
            return {"ok": False, "error": str(e), "runs": [], "store": "sqlite"}

    def get_api_test_run(self, run_id: str) -> Dict[str, Any]:
        self.ensure_schema()
        if self._schema_error:
            return {"ok": False, "error": self._schema_error, "store": "sqlite"}
        try:
            with self._connect() as con:
                r = con.execute("SELECT * FROM api_test_runs WHERE run_id=?", (str(run_id),)).fetchone()
                if not r:
                    return {"ok": False, "error": "Run not found", "store": "sqlite"}
                rows = con.execute(
                    "SELECT result_json FROM api_test_results WHERE run_id=? ORDER BY elapsed_ms DESC, endpoint_no ASC",
                    (str(run_id),),
                ).fetchall()
            run = {
                "run_id": r["run_id"],
                "run_name": r["run_name"],
                "created_at": r["created_at"],
                "base_url": r["base_url"],
                "top_n": r["top_n"],
                "timeout_secs": r["timeout_secs"],
                "request_count": r["request_count"],
                "ok_count": r["ok_count"],
                "failed_count": r["failed_count"],
                "avg_ms": r["avg_ms"],
                "min_ms": r["min_ms"],
                "max_ms": r["max_ms"],
                "config": _loads(r["config_json"], {}) or {},
                "slowest": _loads(r["slowest_json"], []) or [],
                "notes": _loads(r["notes_json"], []) or [],
                "results": [_loads(row["result_json"], {}) or {} for row in rows],
            }
            return {"ok": True, "store": "sqlite", "run": _json_safe(run)}
        except Exception as e:
            return {"ok": False, "error": str(e), "store": "sqlite"}


_store: Optional[SQLiteMarketDataStore] = None


def get_sqlite_market_store() -> SQLiteMarketDataStore:
    global _store
    if _store is None:
        _store = SQLiteMarketDataStore()
    return _store


# Generic alias used by the backtest module.  Keeping the function separate
# makes it easy to add DuckDB/Mongo again later without changing the routes.
def get_market_data_store() -> SQLiteMarketDataStore:
    return get_sqlite_market_store()
