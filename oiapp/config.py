# oiapp/config.py
"""
Single source of truth for where options_data.db lives.

BACKGROUND: every module in this app used to independently compute this
path as `<project folder>/options_data.db` (~50 duplicate copies of the
same expression). That meant the database always lived inside the
project folder itself -- so a folder-level operation (zip extraction,
git checkout, a careless "copy this folder over that one") could
silently overwrite it. That's exactly what happened once already.

This module is now the ONLY place that decides where the database file
lives. Every other module imports DB_PATH from here instead of computing
its own copy.

Resolution order:
  1. OIAPP_DB_PATH environment variable, if set -- always wins. Lets you
     point at any location (e.g. for a second/test instance) without
     touching code.
  2. The configured default below (DEFAULT_DB_PATH) -- deliberately
     OUTSIDE the project folder, so packaging/zipping/extracting the
     project code can never touch the real data.
  3. If neither location is usable (e.g. running on a machine where that
     drive isn't mapped -- CI, a teammate's laptop, WSL/macOS/Linux dev
     box), falls back to the old in-repo location
     (<project folder>/options_data.db) so the app still runs out of the
     box. You'll see a one-time startup print when this fallback fires.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

# Change this if you ever want to relocate the DB again -- every module
# in the app reads it from here, so this is the only line that needs to
# change.
DEFAULT_DB_PATH = r"T:\ajain33\data\options_data.db"

_ENV_VAR = "OIAPP_DB_PATH"


def _project_root() -> Path:
    # oiapp/config.py -> parent = oiapp/, parent.parent = project root
    return Path(__file__).resolve().parent.parent


def _is_writable_dir(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".oiapp_write_test"
        probe.touch()
        probe.unlink()
        return True
    except Exception:
        return False


def _resolve_db_path() -> str:
    override = os.environ.get(_ENV_VAR)
    if override:
        p = Path(override)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return str(p)

    preferred = Path(DEFAULT_DB_PATH)
    if _is_writable_dir(preferred.parent):
        return str(preferred)

    fallback = _project_root() / "options_data.db"
    print(
        f"[oiapp.config] WARNING: preferred DB location {DEFAULT_DB_PATH} "
        f"is not reachable on this machine -- falling back to {fallback}. "
        f"Set the {_ENV_VAR} environment variable to override."
    )
    return str(fallback)


DB_PATH = _resolve_db_path()


# SQLite permits many readers with WAL, but only one writer.  The application
# has dozens of independently scheduled threads, so relying on SQLite's
# lock-race alone led to periodic "database is locked" errors.  This factory
# serializes write transactions *inside this process* while still allowing
# read-only SELECTs to run concurrently.  It is installed before the app's
# modules open their connections, so legacy sqlite3.connect(...) call sites
# benefit without each needing a bespoke retry loop.
_SQLITE_CONNECT = sqlite3.connect
_SQLITE_WRITER_LOCK = threading.RLock()
_WRITE_PREFIXES = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER",
                   "DROP", "VACUUM", "REINDEX", "ATTACH", "DETACH"}


def _is_write_statement(sql: str) -> bool:
    statement = str(sql or "").lstrip()
    if not statement:
        return False
    token = statement.split(None, 1)[0].upper()
    if token in _WRITE_PREFIXES:
        return True
    if token == "PRAGMA":
        return "JOURNAL_MODE" in statement.upper()
    # CTE writes begin with WITH rather than INSERT/UPDATE.
    if token == "WITH":
        upper = statement.upper()
        return any(f" {word} " in upper for word in (" INSERT ", " UPDATE ", " DELETE ", " REPLACE "))
    return False


class _SerializedSQLiteConnection(sqlite3.Connection):
    """Connection subclass that holds one app-wide lock only for writers."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._oiapp_writer_lock_held = False

    def _lock_for_write(self, sql):
        if _is_write_statement(sql) and not self._oiapp_writer_lock_held:
            _SQLITE_WRITER_LOCK.acquire()
            self._oiapp_writer_lock_held = True

    def _unlock_writer(self):
        if self._oiapp_writer_lock_held:
            self._oiapp_writer_lock_held = False
            _SQLITE_WRITER_LOCK.release()

    def execute(self, sql, parameters=()):
        self._lock_for_write(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        self._lock_for_write(sql)
        return super().executemany(sql, parameters)

    def executescript(self, sql_script):
        self._lock_for_write(sql_script)
        return super().executescript(sql_script)

    def commit(self):
        try:
            return super().commit()
        finally:
            self._unlock_writer()

    def rollback(self):
        try:
            return super().rollback()
        finally:
            self._unlock_writer()

    def close(self):
        try:
            return super().close()
        finally:
            self._unlock_writer()


def _serialized_connect(*args, **kwargs):
    # timeout sets SQLite's native busy handler too, covering a short-lived
    # external reader/writer such as a database inspection tool.
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("factory", _SerializedSQLiteConnection)
    return _SQLITE_CONNECT(*args, **kwargs)


def _configure_sqlite_once() -> None:
    """Set persistent WAL mode once—not on every request/worker connection."""
    try:
        con = _SQLITE_CONNECT(DB_PATH, timeout=30)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=30000")
            con.commit()
        finally:
            con.close()
    except Exception as exc:
        print(f"[oiapp.config] WARNING: could not initialize SQLite WAL mode: {exc}")


_configure_sqlite_once()
sqlite3.connect = _serialized_connect
