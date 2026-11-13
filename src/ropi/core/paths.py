"""Application path resolution for Ropi (Windows standard directories & assets)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ropi import __app_name__


def _default_assets_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if isinstance(meipass, str) and meipass:
        candidate = Path(meipass) / "assets"
        if candidate.exists():
            return candidate
    # src/ropi/core/paths.py -> repo root is 4 levels up
    return Path(__file__).resolve().parents[3] / "assets"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved filesystem paths used by Ropi."""

    config_dir: Path
    local_data_dir: Path
    default_library_dir: Path
    assets_dir: Path

    @property
    def db_path(self) -> Path:
        return self.config_dir / "ropi.db"

    @property
    def db_backup_dir(self) -> Path:
        return self.config_dir / "backups"

    @property
    def settings_path(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def models_dir(self) -> Path:
        return self.local_data_dir / "models"

    @property
    def cache_dir(self) -> Path:
        return self.local_data_dir / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.local_data_dir / "logs"

    @property
    def keymaps_dir(self) -> Path:
        return self.assets_dir / "keymaps"

    @property
    def themes_dir(self) -> Path:
        return self.assets_dir / "themes"

    @property
    def icons_dir(self) -> Path:
        return self.assets_dir / "icons"

    @property
    def fonts_dir(self) -> Path:
        return self.assets_dir / "fonts"

    def ensure_dirs(self) -> None:
        """Create required application directories if they do not exist."""
        for path in (
            self.config_dir,
            self.db_backup_dir,
            self.local_data_dir,
            self.models_dir,
            self.cache_dir,
            self.logs_dir,
            self.default_library_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def get_app_paths(base_dir: Path | None = None) -> AppPaths:
    """Resolve standard Windows paths or isolated test paths when base_dir is given."""
    if base_dir is not None:
        return AppPaths(
            config_dir=base_dir / "appdata",
            local_data_dir=base_dir / "localappdata",
            default_library_dir=base_dir / "library",
            assets_dir=_default_assets_dir(),
        )

    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    localappdata = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    documents = Path.home() / "Documents"

    return AppPaths(
        config_dir=appdata / __app_name__,
        local_data_dir=localappdata / __app_name__,
        default_library_dir=documents / __app_name__ / "Library",
        assets_dir=_default_assets_dir(),
    )
