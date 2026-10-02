"""Studio settings (studio_settings.json) and the paths everything else is resolved from.

The settings file is created with defaults on first run and is gitignored: it holds machine
paths and the device serial. Secrets never go in it (API keys stay in tools/hd_remaster/.env).
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

STUDIO_DIR = Path(__file__).resolve().parent
REPO_DIR = STUDIO_DIR.parent.parent
STATIC_DIR = STUDIO_DIR / "static"
DEFAULT_SETTINGS_PATH = STUDIO_DIR / "studio_settings.json"
RUNNER = STUDIO_DIR / "runner.py"

ROM_TREE_URI = ("content://com.android.externalstorage.documents/tree/2664-21DE%3ARoms%2Fnds/"
                "document/2664-21DE%3ARoms%2Fnds%2F")


def _defaults() -> dict[str, Any]:
    workspace = REPO_DIR.parent
    rom_dirs = [str(workspace / "roms")] if (workspace / "roms").is_dir() else []
    baselines = [str(workspace / "frame_compare_baselines")] if (workspace / "frame_compare_baselines").is_dir() else []
    return {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
        # device
        "serial": "c3ca0370",
        "package": "app.watermelonthor.dev",
        "bottom_display": "4630946482288158084",
        "rom_tree_uri": ROM_TREE_URI,
        # what counts as "nobody is using the Thor": activities whose name contains one of these
        "idle_markers": ["launcher"],
        # pipeline
        "hd_remaster_dir": "",      # empty: tools/hd_remaster next to the studio
        "python": "",               # empty: hd_remaster's .venv, else the Python running the studio
        "rom_dirs": rom_dirs,       # folders scanned for .nds files (not recursive)
        "game_roms": {},            # game code -> {"path": ..., "title": ...}
        # checks
        "checks_out_dir": "",       # empty: tools/studio/runs/frame_compare
        "checks_baseline_roots": baselines,
    }


# what the settings API accepts, and how each value is checked
_PATTERNS = {
    "serial": re.compile(r"^[\w.:\-]{0,64}$"),
    "package": re.compile(r"^[A-Za-z][\w]*(\.[A-Za-z_][\w]*)+$"),
    "bottom_display": re.compile(r"^\d{1,25}$"),
    "host": re.compile(r"^(127\.0\.0\.1|localhost)$"),
}


class SettingsError(ValueError):
    """A settings value the user typed is not acceptable; the message says why."""


class Settings:
    """Thread-safe settings backed by a JSON file. Unknown keys in the file are kept."""

    def __init__(self, path: Path | str = DEFAULT_SETTINGS_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = _defaults()
        if self.path.exists():
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    self._data.update(stored)
            except (OSError, json.JSONDecodeError) as e:
                print(f"studio: {self.path} could not be read ({e}); using defaults", file=sys.stderr)
        else:
            self.save()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate and apply the keys the settings page may change; returns the new snapshot."""
        clean: dict[str, Any] = {}
        for key, value in patch.items():
            if key in _PATTERNS:
                value = str(value).strip()
                if not _PATTERNS[key].match(value):
                    raise SettingsError(f"'{value}' is not a valid {key.replace('_', ' ')}")
                clean[key] = value
            elif key == "port":
                try:
                    port = int(value)
                except (TypeError, ValueError):
                    raise SettingsError("the port must be a number") from None
                if not 1024 <= port <= 65535:
                    raise SettingsError("the port must be between 1024 and 65535")
                clean[key] = port
            elif key == "open_browser":
                clean[key] = bool(value)
            elif key == "rom_tree_uri":
                value = str(value).strip()
                if not value.startswith("content://"):
                    raise SettingsError("the ROM folder URI must start with content://")
                clean[key] = value
            elif key in ("hd_remaster_dir", "python", "checks_out_dir"):
                clean[key] = str(value or "").strip().strip('"')
            elif key in ("rom_dirs", "checks_baseline_roots", "idle_markers"):
                items = value if isinstance(value, list) else str(value or "").splitlines()
                clean[key] = [str(v).strip().strip('"') for v in items if str(v).strip()]
            else:
                raise SettingsError(f"unknown setting '{key}'")
        with self._lock:
            self._data.update(clean)
            self.save()
            return copy.deepcopy(self._data)

    def remember_rom(self, code: str, path: str, title: str) -> None:
        with self._lock:
            roms = self._data.setdefault("game_roms", {})
            roms[code] = {"path": path, "title": title}
            self.save()

    # ------------------------------------------------------------------ derived paths

    def hd_dir(self) -> Path:
        custom = self.get("hd_remaster_dir")
        return Path(custom) if custom else REPO_DIR / "tools" / "hd_remaster"

    def frame_compare_dir(self) -> Path:
        return REPO_DIR / "tools" / "frame_compare"

    def checks_out_dir(self) -> Path:
        custom = self.get("checks_out_dir")
        return Path(custom) if custom else STUDIO_DIR / "runs" / "frame_compare"

    def venv_python(self) -> Path:
        return self.hd_dir() / ".venv" / "Scripts" / "python.exe"

    def python(self) -> str:
        """The interpreter for pipeline and check jobs: the setting, the venv, or our own."""
        custom = self.get("python")
        if custom:
            return custom
        venv = self.venv_python()
        if venv.exists():
            return str(venv)
        return sys.executable


def adb_available() -> bool:
    return shutil.which("adb") is not None
