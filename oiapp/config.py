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
