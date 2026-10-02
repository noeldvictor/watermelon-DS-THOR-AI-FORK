"""The HD remastering pipeline (tools/hd_remaster) as the studio sees it.

Reads the game library (recipes, GAMES.md, work and pack folders) and builds the command lines
for hd_remaster.py's subcommands. The commands are the tool's own CLI, unchanged:

    extract <rom>                       verify <work> --dumps D [--sprites S]
    upscale <work> [--model M] [--scale 2|4] [--force] [--only TEXT] [--redo-cutouts]
    build <work> [--native]             push <pack> [--serial S]
    all <rom> [upscale options]         misses <work> <rom> [--serial S] [--apply]
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from config import RUNNER, Settings

CODE = re.compile(r"^[A-Z0-9]{4}$")
MEDIA_NAME = re.compile(r"^[\w.\-]+\.(jpg|jpeg|png|webp)$", re.I)
STEPS = ("extract", "verify", "upscale", "build", "push", "all", "misses")
DEVICE_STEPS = ("push",)


class PipelineError(ValueError):
    """The step can't run as asked; the message tells the user what to do instead."""


# ---------------------------------------------------------------------------- ROM files

def read_rom_header(path: Path) -> dict[str, str]:
    """Game code and internal title from a .nds header (0x00 title, 0x0C code)."""
    try:
        with open(path, "rb") as f:
            head = f.read(0x10)
    except OSError as e:
        raise PipelineError(f"The ROM can't be read: {e.strerror or e}") from None
    if len(head) < 0x10:
        raise PipelineError("That file is too small to be a DS ROM.")
    code = head[0x0C:0x10].decode("ascii", errors="replace")
    if not CODE.match(code):
        raise PipelineError("That file doesn't look like a DS ROM (.nds): its header has no game code. "
                            "Zipped or 7z ROMs have to be unpacked first.")
    title = head[:0x0C].split(b"\0", 1)[0].decode("ascii", errors="replace").strip()
    return {"code": code, "title": title}


def clean_path(text: str) -> Path:
    """A path as pasted by a user: surrounding quotes and spaces removed."""
    return Path(str(text or "").strip().strip('"').strip("'"))


def scan_rom_dirs(settings: Settings) -> list[dict[str, Any]]:
    """The .nds files in the configured ROM folders (top level only), with their game codes."""
    found = []
    for folder in settings.get("rom_dirs") or []:
        d = Path(folder)
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.suffix.lower() != ".nds" or not p.is_file():
                continue
            try:
                head = read_rom_header(p)
            except PipelineError:
                continue
            found.append({"path": str(p), "file": p.name, "size": p.stat().st_size, **head})
    return found


# ---------------------------------------------------------------------------- library

def parse_games_md(text: str) -> dict[str, dict[str, Any]]:
    """Rows of the 'Recipes' table in games/GAMES.md, by game code."""
    rows: dict[str, dict[str, Any]] = {}
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if section != "Recipes" or not line.startswith("|"):
            continue
        m = re.search(r"\]\(([A-Z0-9]{4})/README\.md\)", line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        img = re.search(r'<img src="([^"]+)"', cells[0])
        rows[m.group(1)] = {
            "status": _md_text(cells[2]), "pack": _md_text(cells[3]), "build_time": _md_text(cells[4]),
            "coverage": _md_text(cells[5]), "image": img.group(1) if img else None,
        }
    return rows


def _md_text(cell: str) -> str:
    cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)
    cell = cell.replace("<br>", " · ").replace("**", "").replace("`", "")
    return re.sub(r"<[^>]+>", "", cell).strip()


class PackSizer:
    """Folder sizes computed in the background: a pack can hold 240,000 files."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, int, int]] = {}
        self._busy: set[str] = set()

    def get(self, folder: Path, stamp: float) -> tuple[int | None, int | None]:
        """(bytes, files) if known for this stamp, else (None, None) and a scan starts."""
        key = str(folder)
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] == stamp:
                return hit[1], hit[2]
            if key not in self._busy:
                self._busy.add(key)
                threading.Thread(target=self._scan, args=(folder, stamp), daemon=True).start()
        return None, None

    def _scan(self, folder: Path, stamp: float) -> None:
        total = files = 0
        try:
            stack = [str(folder)]
            while stack:
                with os.scandir(stack.pop()) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
        except OSError:
            pass
        with self._lock:
            self._cache[str(folder)] = (stamp, total, files)
            self._busy.discard(str(folder))


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Library:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sizer = PackSizer()

    @property
    def hd(self) -> Path:
        return self.settings.hd_dir()

    def codes(self) -> list[str]:
        codes: set[str] = set()
        for sub in ("games", "work", "packs"):
            d = self.hd / sub
            if d.is_dir():
                codes.update(p.name for p in d.iterdir() if p.is_dir() and CODE.match(p.name))
        codes.update(c for c in (self.settings.get("game_roms") or {}) if CODE.match(c))
        return sorted(codes)

    def games_md(self) -> dict[str, dict[str, Any]]:
        p = self.hd / "games" / "GAMES.md"
        try:
            return parse_games_md(p.read_text(encoding="utf-8"))
        except OSError:
            return {}

    def recipe(self, code: str) -> dict[str, Any] | None:
        data = _read_json(self.hd / "games" / code / "recipe.json")
        return data if isinstance(data, dict) else None

    def summary(self, code: str, md: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        md = self.games_md() if md is None else md
        recipe = self.recipe(code)
        rom = (self.settings.get("game_roms") or {}).get(code) or {}
        row = md.get(code) or {}
        work = self.hd / "work" / code
        pack = self.hd / "packs" / code
        snapshot = _read_json(work / "recipe.json") or {}
        title = (recipe or {}).get("title") or snapshot.get("title") or rom.get("title") or code
        if row.get("status"):
            status = row["status"]
        elif recipe:
            status = "Recipe, no write-up yet"
        else:
            status = "New project"
        image = None
        if row.get("image") and row["image"].startswith(f"{code}/media/"):
            name = row["image"].split("/")[-1]
            if MEDIA_NAME.match(name) and (self.hd / "games" / code / "media" / name).exists():
                image = f"/api/games/{code}/media/{name}"
        return {
            "code": code, "title": title, "status": status, "has_recipe": recipe is not None,
            "image": image, "listed": row,
            "stages": {
                "extracted": _mtime(work / "manifest.jsonl"),
                "upscaled": _mtime(work / "upscaled" / "upscale.json"),
                "built": _mtime(pack / "pack.json"),
            },
            "pack": self.pack_info(code),
            "rom_path": rom.get("path"),
        }

    def pack_info(self, code: str) -> dict[str, Any]:
        pack = self.hd / "packs" / code
        info_path = pack / "pack.json"
        if not info_path.exists():
            return {"exists": pack.is_dir(), "path": str(pack)}
        info = _read_json(info_path) or {}
        stamp = _mtime(info_path) or 0.0
        size, files = self.sizer.get(pack, stamp)
        return {"exists": True, "path": str(pack), "images": info.get("images"), "scale": info.get("scale"),
                "source": info.get("source"), "built": stamp, "size_bytes": size, "files": files,
                "size_pending": size is None, "upscale": info.get("upscale")}

    def detail(self, code: str) -> dict[str, Any]:
        if not CODE.match(code):
            raise PipelineError("A game code is 4 letters or digits, like BSDE.")
        out = self.summary(code)
        out["recipe"] = self.recipe(code)
        work = self.hd / "work" / code
        snapshot = _read_json(work / "recipe.json") or {}
        out["work"] = {
            "path": str(work), "exists": work.is_dir(),
            "rom_match": snapshot.get("rom_match"), "rom_sha256": snapshot.get("rom_sha256"),
            "upscale": _read_json(work / "upscaled" / "upscale.json"),
            "has_native": (work / "native").is_dir(),
            "has_redrawn": (work / "redrawn").is_dir(),
        }
        rom_path = out.get("rom_path")
        rom: dict[str, Any] = {"path": rom_path, "exists": False}
        if rom_path:
            p = Path(rom_path)
            rom["exists"] = p.is_file()
            if rom["exists"]:
                try:
                    head = read_rom_header(p)
                    rom.update(head)
                    rom["code_matches"] = head["code"] == code
                except PipelineError as e:
                    rom["error"] = str(e)
        if not rom["exists"]:
            # ROMs with this game code in the ROM folders, offered on the game page
            rom["suggestions"] = [r["path"] for r in scan_rom_dirs(self.settings) if r["code"] == code]
        out["rom"] = rom
        out["readme"] = (self.hd / "games" / code / "README.md").exists()
        out["models"] = self.models()
        return out

    def models(self) -> list[str]:
        data = _read_json(self.hd / "models.json")
        return sorted(data) if isinstance(data, dict) else []

    def media_path(self, code: str, name: str) -> Path | None:
        if not CODE.match(code) or not MEDIA_NAME.match(name):
            return None
        p = self.hd / "games" / code / "media" / name
        return p if p.is_file() else None

    # ------------------------------------------------------------------ setup

    def setup_status(self) -> dict[str, Any]:
        venv = self.settings.venv_python()
        hd = self.hd
        return {
            "hd_remaster_dir": str(hd),
            "tool_found": (hd / "hd_remaster.py").exists(),
            "venv_ready": venv.exists(),
            "python": self.settings.python(),
            "model_ready": (hd / "models" / "4x-UltraSharp.safetensors").exists(),
            "openrouter_key_set": openrouter_key_set(hd / ".env"),
        }

    def setup_command(self) -> list[str]:
        script = self.hd / "setup.ps1"
        if not script.exists():
            raise PipelineError(f"{script} is missing. Is the hd_remaster folder setting right?")
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]

    # ------------------------------------------------------------------ steps

    def step_command(self, code: str, step: str, options: dict[str, Any]) -> tuple[str, list[str]]:
        """(title, argv) for one hd_remaster.py step. Raises PipelineError with a fix-it message."""
        if not CODE.match(code):
            raise PipelineError("A game code is 4 letters or digits, like BSDE.")
        if step not in STEPS:
            raise PipelineError(f"Unknown step '{step}'.")
        hd = self.hd
        tool = hd / "hd_remaster.py"
        if not tool.exists():
            raise PipelineError(f"{tool} is missing. Check the hd_remaster folder in Settings.")
        work = hd / "work" / code
        pack = hd / "packs" / code
        serial = self.settings.get("serial") or ""

        def need_rom() -> str:
            raw = options.get("rom_path") or (self.settings.get("game_roms") or {}).get(code, {}).get("path")
            if not raw:
                raise PipelineError(f"This step reads the ROM. Set the ROM file for {code} first.")
            p = clean_path(raw)
            if not p.is_file():
                raise PipelineError(f"The ROM file {p} doesn't exist (moved or renamed?). Set it again.")
            head = read_rom_header(p)
            if head["code"] != code:
                raise PipelineError(f"{p.name} is game {head['code']}, not {code}. Pick the right ROM.")
            return str(p)

        def need_work() -> str:
            if not (work / "manifest.jsonl").exists():
                raise PipelineError(f"{code} hasn't been extracted yet. Run Extract first.")
            return str(work)

        def need_venv() -> None:
            if not self.settings.get("python") and not self.settings.venv_python().exists():
                raise PipelineError("The upscaler's Python environment isn't set up. Run Setup on the "
                                    "Library page first (it installs PyTorch and the model).")

        args: list[str]
        if step == "extract":
            need_venv()
            args = ["extract", need_rom()]
        elif step == "verify":
            need_venv()
            dumps = clean_path(options.get("dumps", ""))
            if not str(dumps) or str(dumps) == "." or not (dumps / "manifest.jsonl").exists():
                raise PipelineError("Verify needs a texture dump folder (one with manifest.jsonl in it).")
            args = ["verify", need_work(), "--dumps", str(dumps)]
            sprites = str(options.get("sprites") or "").strip()
            if sprites:
                sp = clean_path(sprites)
                if not (sp / "manifest.jsonl").exists():
                    raise PipelineError("The sprite dump folder has no manifest.jsonl.")
                args += ["--sprites", str(sp)]
        elif step == "upscale":
            need_venv()
            args = ["upscale", need_work()] + self._upscale_options(options)
        elif step == "build":
            need_venv()
            w = need_work()
            native = bool(options.get("native"))
            if not native and not (work / "upscaled").is_dir():
                raise PipelineError(f"Nothing is upscaled for {code} yet. Run Upscale first, or build a "
                                    "native (1x) test pack.")
            args = ["build", w] + (["--native"] if native else [])
        elif step == "push":
            if not (pack / "pack.json").exists():
                raise PipelineError(f"No pack is built for {code} yet. Run Build first.")
            args = ["push", str(pack)] + (["--serial", serial] if serial else [])
        elif step == "all":
            need_venv()
            args = ["all", need_rom()] + self._upscale_options(options)
        else:  # misses
            need_venv()
            args = ["misses", need_work(), need_rom()] + (["--serial", serial] if serial else [])
            if options.get("apply"):
                args.append("--apply")
        title = f"{step.capitalize()} {code}" + (" (native 1x)" if step == "build" and options.get("native") else "")
        return title, [self.settings.python(), str(RUNNER), str(tool)] + args

    def _upscale_options(self, options: dict[str, Any]) -> list[str]:
        out: list[str] = []
        model = str(options.get("model") or "").strip()
        if model:
            if model not in self.models():
                raise PipelineError(f"Unknown model '{model}'. Pick one from the list (models.json).")
            out += ["--model", model]
        scale = options.get("scale")
        if scale not in (None, "", 0, "0"):
            if str(scale) not in ("2", "4"):
                raise PipelineError("Scale must be 2 or 4 (or the recipe's).")
            out += ["--scale", str(scale)]
        if options.get("force"):
            out.append("--force")
        if options.get("redo_cutouts"):
            out.append("--redo-cutouts")
        only = str(options.get("only") or "").strip()
        if only:
            if any(ch in only for ch in "\r\n\0"):
                raise PipelineError("'Only' must be one line of text.")
            out += ["--only", only]
        return out


def openrouter_key_set(env_file: Path) -> bool:
    """Whether .env has a non-empty OPENROUTER_API_KEY. The value itself is never returned."""
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return bool(os.environ.get("OPENROUTER_API_KEY"))
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "OPENROUTER_API_KEY" and value.strip().strip('"').strip("'"):
            return True
    return False

