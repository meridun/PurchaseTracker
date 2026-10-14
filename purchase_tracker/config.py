"""Resolve where instance data (the SQLite database, imports, backups) lives.

Resolution order, first match wins:
  1. Explicit ``--data-dir`` on the command line.
  2. ``PURCHASE_TRACKER_DATA_DIR`` environment variable.
  3. ``data_dir`` in ``config.local.toml`` at the repo root (git-ignored).
  4. Platform default:
       Windows  -> %LOCALAPPDATA%\\PurchaseTracker
       macOS    -> ~/Library/Application Support/PurchaseTracker
       Linux    -> $XDG_DATA_HOME/purchase-tracker or ~/.local/share/purchase-tracker

Code (this repo) and data (the resolved directory) are deliberately separate so the
repo can be public while the data stays private.
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

ENV_VAR = "PURCHASE_TRACKER_DATA_DIR"
DB_FILENAME = "purchases.db"
REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = REPO_ROOT / "config.local.toml"


def _platform_default() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "PurchaseTracker"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PurchaseTracker"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "purchase-tracker"


def _from_local_config() -> Path | None:
    if not LOCAL_CONFIG.exists():
        return None
    with LOCAL_CONFIG.open("rb") as f:
        cfg = tomllib.load(f)
    value = cfg.get("data_dir")
    return Path(value).expanduser() if value else None


def resolve_data_dir(explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if env := os.environ.get(ENV_VAR):
        return Path(env).expanduser().resolve()
    if local := _from_local_config():
        return local.resolve()
    return _platform_default()


def db_path(data_dir: Path) -> Path:
    return data_dir / DB_FILENAME
