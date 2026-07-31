"""Locate the OSCAR data folder and its SQLite database.

Resolution order:

1. ``OSCAR_DATA_DIR`` environment variable (or an explicit path argument).
2. The path OSCAR itself recorded in the Windows registry.
3. Well-known locations under the user's documents folder.

Steps 2 and 3 run only when nothing was asked for. A folder that *was* named
but holds no database is an error, never a reason to go looking elsewhere:
silently opening a different person's therapy data is worse than not starting.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

DB_FILENAME = "oscar.db"

_REGISTRY_KEYS = (
    r"Software\OSCAR_Team\OSCAR 2.0\Settings",
    r"Software\OSCAR_Team\OSCAR\Settings",
)

_FOLDER_NAMES = (
    "OSCAR20_Data",
    "OSCAR_Data",
    "OSCAR",
)


class DataFolderNotFound(RuntimeError):
    """Raised when no readable OSCAR database can be located."""


@dataclass(frozen=True)
class OscarLocation:
    """A resolved OSCAR data folder."""

    data_dir: Path
    db_path: Path
    source: str

    def as_dict(self) -> dict:
        return {
            "data_dir": str(self.data_dir),
            "db_path": str(self.db_path),
            "discovered_via": self.source,
        }


def _candidate_from_dir(path: Path) -> Path | None:
    """Return the database inside ``path`` if it exists."""
    if not path:
        return None
    db = path / DB_FILENAME
    return db if db.is_file() else None


def _registry_candidates() -> list[tuple[Path, str]]:
    """Read data folder paths that OSCAR stored in the Windows registry."""
    if sys.platform != "win32":
        return []

    import winreg

    found: list[tuple[Path, str]] = []
    for key_path in _REGISTRY_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "AppData")
        except OSError:
            continue
        if value:
            found.append((Path(str(value)), f"registry:HKCU\\{key_path}"))
    return found


def _documents_dirs() -> list[Path]:
    """Return plausible documents folders, including the OneDrive redirect."""
    home = Path.home()
    dirs = [home / "Documents", home / "OneDrive" / "Documents"]

    # Localised Windows installs use a translated folder name, and OneDrive
    # redirection means the real folder may live under any OneDrive root.
    if sys.platform == "win32":
        for env in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            root = os.environ.get(env)
            if root:
                dirs.append(Path(root))
        for root in (home, *[Path(os.environ[e]) for e in ("OneDrive",) if os.environ.get(e)]):
            try:
                dirs.extend(child for child in root.iterdir() if child.is_dir())
            except OSError:
                continue

    return dirs


def discover(explicit: str | os.PathLike[str] | None = None) -> OscarLocation:
    """Find the OSCAR database, raising :class:`DataFolderNotFound` on failure."""
    from_env = explicit is None
    requested = os.environ.get("OSCAR_DATA_DIR") if from_env else explicit

    if requested:
        data_dir = Path(requested).expanduser()
        db = _candidate_from_dir(data_dir)
        if db:
            return OscarLocation(
                data_dir, db, "env:OSCAR_DATA_DIR" if from_env else "argument"
            )
        # A named folder is an instruction, not a hint. Searching on from here
        # would quietly open whichever database happened to be found next --
        # a backup, a second profile, another household member's data -- and
        # every answer afterwards would be confidently about the wrong person.
        origin = "OSCAR_DATA_DIR" if from_env else "The requested data folder"
        reason = "does not exist" if not data_dir.exists() else f"contains no {DB_FILENAME}"
        raise DataFolderNotFound(
            f"{origin} points to {data_dir}, which {reason}. "
            "Correct the path, or remove the setting to search the usual locations."
        )

    tried: list[str] = []

    for data_dir, source in _registry_candidates():
        db = _candidate_from_dir(data_dir)
        if db:
            return OscarLocation(data_dir, db, source)
        tried.append(str(data_dir))

    for parent in _documents_dirs():
        for name in _FOLDER_NAMES:
            data_dir = parent / name
            db = _candidate_from_dir(data_dir)
            if db:
                return OscarLocation(data_dir, db, "documents-folder")
            tried.append(str(data_dir))

    raise DataFolderNotFound(
        "Could not locate "
        f"{DB_FILENAME}. Set OSCAR_DATA_DIR to the folder that contains it. "
        f"Checked {len(tried)} location(s)."
    )
