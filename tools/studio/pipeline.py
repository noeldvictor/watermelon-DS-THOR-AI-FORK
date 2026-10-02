"""The HD remastering pipeline (tools/hd_remaster) as the studio sees it.

Reads the game library (recipes, GAMES.md, work and pack folders) and builds the command lines
for hd_remaster.py's subcommands. The commands are the tool's own CLI, unchanged:

    extract <rom>                       verify <work> --dumps D [--sprites S]
    upscale <work> [--model M] [--scale 2|4] [--force] [--only TEXT] [--redo-cutouts]
    build <work> [--native]             push <pack> [--serial S] [--models-only]
    all <rom> [upscale options]         misses <work> <rom> [--serial S] [--apply]

and the 3D model steps (modelpack.py, ai3d.py):

    models extract <rom> [--trace T] [--cpu-words W] [--previews]
    models build <work> [--smooth S [--only TEXT] [--seen]] [--rom R]
    models fit <work> --model ID --mesh M.glb|.obj
    models ai <work> --model ID --provider P --polycount N --budget C [--dry-run]
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

from config import RUNNER, Settings

CODE = re.compile(r"^[A-Z0-9]{4}$")
MEDIA_NAME = re.compile(r"^[\w.\-]+\.(jpg|jpeg|png|webp)$", re.I)
STEPS = ("extract", "verify", "upscale", "build", "push", "all", "misses",
         "models_extract", "models_build", "models_fit", "models_ai", "models_push")
DEVICE_STEPS = ("push", "models_push")

# 3D models: ids are modelpack.model_id() (source + name, other characters turned into '_'); the
# pictures served are PNGs in a model's folder or its ai/ subfolder
MODEL_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
MODEL_FILE = re.compile(r"^(?:ai/)?[A-Za-z0-9._-]+\.png$")
SHAPE_KEY = re.compile(r"^mdl1_\d+_[0-9a-f]+$")
AI_PROVIDERS = {"tripo": "TRIPO_API_KEY", "meshy": "MESHY_API_KEY"}
AI_CREDITS_PER_CALL = 20        # ai3d.CREDITS_MULTI_IMAGE_MESH: both services, multiview geometry only
AI_VIEWS = ("front", "right", "back", "left")


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


def _names(folder: Path) -> set[str]:
    """The entry names in a folder (empty when it doesn't exist): one call instead of a stat per file."""
    try:
        return set(os.listdir(folder))
    except OSError:
        return set()


def _count_files(folder: Path, suffix: str) -> int:
    return sum(1 for name in _names(folder) if name.lower().endswith(suffix))


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

    # ------------------------------------------------------------------ 3D models

    def model_folder(self, code: str, mid: str) -> Path | None:
        """work/<CODE>/models/<id> when that is an extracted model (it has model.json), else None."""
        if not CODE.match(code or "") or not MODEL_ID.match(mid or "") or not mid.strip("."):
            return None
        root = self.hd / "work" / code / "models"
        folder = root / mid
        try:
            folder.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return None
        return folder if (folder / "model.json").is_file() else None

    def models_rom(self, code: str) -> str | None:
        """The ROM `models extract` read (models/rom.txt): fit, ai and build read their models from it."""
        try:
            return (self.hd / "work" / code / "models" / "rom.txt").read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def ai_status(self, code: str) -> dict[str, Any]:
        """The game's AI ledger total and whether each provider's key is set (yes/no, never the key)."""
        ledger = self.hd / "work" / code / "models" / "ai_ledger.jsonl"
        credits, calls = ledger_total(ledger)
        env = self.hd / ".env"
        return {"ledger_credits": credits, "ledger_calls": calls, "ledger": str(ledger),
                "credits_per_call": AI_CREDITS_PER_CALL,
                "keys": {provider: api_key_set(env, name) for provider, name in AI_PROVIDERS.items()}}

    def model_list(self, code: str) -> dict[str, Any]:
        """The extracted 3D models (models/index.json, most seen first) and what each folder holds."""
        if not CODE.match(code):
            raise PipelineError("A game code is 4 letters or digits, like BSDE.")
        work = self.hd / "work" / code
        root = work / "models"
        pack = self.hd / "packs" / code
        index = _read_json(root / "index.json")
        models = []
        for entry in index if isinstance(index, list) else []:
            mid = str(entry.get("id") or "") if isinstance(entry, dict) else ""
            if not MODEL_ID.match(mid) or not mid.strip("."):
                continue
            names = _names(root / mid)
            ai = _names(root / mid / "ai") if "ai" in names else set()
            models.append({
                "id": mid, "source": entry.get("source"), "name": entry.get("name"),
                "shapes": entry.get("shapes"), "triangles": entry.get("triangles"), "seen": entry.get("seen") or 0,
                "has_preview": "preview.png" in names, "has_edited": "edited.obj" in names,
                "has_edited_preview": "edited_preview.png" in names,
                "ai_refs": [v for v in AI_VIEWS if f"ref_{v}.png" in ai],
                "ai_meshes": sum(1 for n in ai if n.lower().endswith(".glb")),
            })
        rom = self.models_rom(code)
        return {
            "code": code, "path": str(root), "extracted": _mtime(root / "index.json"),
            "rom": rom, "rom_exists": bool(rom) and Path(rom).is_file(),
            "models": models,
            "counts": {"models": len(models), "seen": sum(1 for m in models if m["seen"]),
                       "edited": sum(1 for m in models if m["has_edited"]),
                       "previews": sum(1 for m in models if m["has_preview"])},
            "built": {"path": str(work / "models_built"), "count": _count_files(work / "models_built", ".dl")},
            "pack": {"path": str(pack / "models"), "pack_exists": pack.is_dir(),
                     "count": _count_files(pack / "models", ".dl")},
            "ai": self.ai_status(code),
        }

    def model_detail(self, code: str, mid: str) -> dict[str, Any] | None:
        """One model: model.json, which shapes have a built replacement, and its pictures. None if unknown."""
        folder = self.model_folder(code, mid)
        if folder is None:
            return None
        info = _read_json(folder / "model.json")
        if not isinstance(info, dict):
            raise PipelineError(f"{folder / 'model.json'} can't be read. Run Extract models again.")
        built = _names(self.hd / "work" / code / "models_built")
        shapes = []
        for s in info.get("shapes") or []:
            if not isinstance(s, dict):
                continue
            key = str(s.get("key") or "")
            shapes.append({**s, "built": bool(SHAPE_KEY.match(key)) and f"{key}.dl" in built})
        names = _names(folder)
        ai = _names(folder / "ai") if "ai" in names else set()

        def url(rel: str) -> str | None:
            # the stamp makes the page fetch a picture again after a fit or a dry run rewrote it
            stamp = _mtime(folder / rel)
            return f"/api/games/{code}/models3d/{mid}/file/{rel}?v={int(stamp)}" if stamp else None

        hidden: list[str] = []
        if "hidden.txt" in names:
            try:
                text = (folder / "hidden.txt").read_text(encoding="utf-8")
                hidden = [line.strip() for line in text.splitlines() if line.strip()][:200]
            except OSError:
                pass
        return {
            "code": code, "id": mid, "source": info.get("source"), "name": info.get("name"),
            "bbox": info.get("bbox"), "shapes": shapes, "folder": str(folder),
            "triangles": sum(int(s.get("triangles") or 0) for s in shapes),
            "seen": sum(int(s.get("seen") or 0) for s in shapes),
            "edited": str(folder / "edited.obj") if "edited.obj" in names else None,
            "hidden": hidden,
            "images": {
                "preview": url("preview.png") if "preview.png" in names else None,
                "edited_preview": url("edited_preview.png") if "edited_preview.png" in names else None,
                "ai_refs": [{"view": v, "url": url(f"ai/ref_{v}.png")} for v in AI_VIEWS if f"ref_{v}.png" in ai],
                "textures": [{"name": n[4:-4], "url": url(n)} for n in sorted(names)
                             if n.startswith("tex_") and MODEL_FILE.match(n)],
            },
            "ai_meshes": [str(folder / "ai" / n) for n in sorted(ai) if n.lower().endswith(".glb")],
            "ai": self.ai_status(code),
        }

    def model_file(self, code: str, mid: str, rel: str) -> Path | None:
        """A PNG in a model's folder or its ai/ subfolder; None for anything else."""
        folder = self.model_folder(code, mid)
        if folder is None or not MODEL_FILE.match(rel or ""):
            return None
        root = folder.resolve()
        p = (folder / rel).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            return None
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
            "ai3d_keys": {provider: api_key_set(hd / ".env", name) for provider, name in AI_PROVIDERS.items()},
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
        if step.startswith("models_"):
            if step != "models_push":
                need_venv()
            title, args = self._models_args(code, step, options, need_rom)
            return title, [self.settings.python(), str(RUNNER), str(tool)] + args
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

    def _models_args(self, code: str, step: str, options: dict[str, Any],
                     need_rom: Callable[[], str]) -> tuple[str, list[str]]:
        """(title, hd_remaster.py arguments) for a 3D model step. Raises PipelineError with a fix-it message."""
        work = self.hd / "work" / code
        root = work / "models"
        if step == "models_push":
            pack = self.hd / "packs" / code
            if not _count_files(pack / "models", ".dl"):
                if not pack.is_dir() and _count_files(work / "models_built", ".dl"):
                    raise PipelineError(f"packs\\{code} doesn't exist, so Build models left its replacements in "
                                        f"work\\{code}\\models_built. Build the texture pack (a native test build "
                                        "is enough): it copies them in. Then install the models.")
                raise PipelineError(f"No built models in packs\\{code}\\models yet. Run Build models first.")
            serial = self.settings.get("serial") or ""
            return f"Install models {code}", ["push", str(pack), "--models-only"] + (["--serial", serial] if serial else [])
        if step == "models_extract":
            args = ["models", "extract", need_rom()]
            args += _file_option(options, "trace", "--trace", "The dl_trace file", ".json")
            args += _file_option(options, "cpu_words", "--cpu-words", "The CPU words file (gx_cpu_words.bin)")
            if options.get("previews"):
                args.append("--previews")
            return f"Extract models {code}", args
        if not (root / "index.json").exists():
            raise PipelineError(f"No 3D models are extracted for {code} yet. Run Extract models first.")

        if step == "models_build":
            smooth = _smooth_option(options.get("smooth"))
            only = str(options.get("only") or "").strip()
            if any(ch in only for ch in "\r\n\0"):
                raise PipelineError("'Only' must be one line of text.")
            seen = bool(options.get("seen"))
            if smooth is None and (only or seen):
                raise PipelineError("'Only' and 'seen' pick the models to smooth: set a smooth strength too, or "
                                    "clear them to build the edited models.")
            if smooth is None and not any((root / name / "edited.obj").is_file() for name in _names(root)):
                raise PipelineError("Nothing to build: no model has an edited.obj yet. Fit a mesh onto a model (or "
                                    "save an edit of its model.obj as edited.obj), or set a smooth strength.")
            args = ["models", "build", str(work)]
            parts = []
            if smooth is not None:
                args += ["--smooth", f"{smooth:g}"]
                parts.append(f"smooth {smooth:g}")
            if only:
                args.append(f"--only={only}")       # '=' form: a model id may start with '-'
                parts.append(f"only '{only}'")
            if seen:
                args.append("--seen")
                parts.append("seen")
            rom = self.models_rom(code)
            if not rom or not Path(rom).is_file():
                args += ["--rom", need_rom()]   # extract's ROM moved: the game's own ROM file
            return f"Build models {code}" + (f" ({', '.join(parts)})" if parts else ""), args

        # fit and ai work on one model, which they read again from the ROM extract read
        mid = str(options.get("model") or "").strip()
        folder = self.model_folder(code, mid)
        if folder is None:
            raise PipelineError(f"There is no extracted model '{mid}' for {code}. Pick one from the 3D models list.")
        rom = self.models_rom(code)
        if not rom or not Path(rom).is_file():
            raise PipelineError(f"This reads the ROM the models were extracted from, and {rom or 'models/rom.txt'} "
                                "isn't there any more. Run Extract models again with the ROM's current location.")
        label = str((_read_json(folder / "model.json") or {}).get("name") or mid)
        if step == "models_fit":
            raw = str(options.get("mesh") or "").strip()
            if not raw:
                raise PipelineError("Paste the full path of the mesh to fit (a .glb or .obj file).")
            mesh = clean_path(raw)
            if not mesh.is_absolute():
                raise PipelineError("Use the full path of the mesh file, starting with the drive letter.")
            if mesh.suffix.lower() not in (".glb", ".obj"):
                raise PipelineError(f"{mesh.name} isn't a .glb or .obj file. Export the mesh as GLB or OBJ first.")
            if not mesh.is_file():
                raise PipelineError(f"There is no file at {mesh}.")
            return (f"Fit {mesh.name} onto {label} ({code})",
                    ["models", "fit", str(work), f"--model={mid}", "--mesh", str(mesh)])

        # models_ai
        provider = str(options.get("provider") or "tripo").strip().lower()
        if provider not in AI_PROVIDERS:
            raise PipelineError("Pick Tripo or Meshy as the AI provider.")
        polycount = _int_option(options.get("polycount"), 6000, 100, 100000, "The polygon count")
        budget = _int_option(options.get("budget"), 100, 0, 100000, "The budget")
        args = ["models", "ai", str(work), f"--model={mid}", "--provider", provider,
                "--polycount", str(polycount), "--budget", str(budget)]
        if options.get("dry_run"):
            return f"AI dry run: {label} ({code})", args + ["--dry-run"]
        # a real call spends money: the key, the budget and an explicit confirmation, in that order
        name = provider.capitalize()
        key = AI_PROVIDERS[provider]
        if not api_key_set(self.hd / ".env", key):
            raise PipelineError(f"No {key} is set, so nothing can be sent to {name}. Add {key}=... to "
                                f"{self.hd / '.env'} (it is never committed), or run a dry run, which needs no key.")
        spent, _ = ledger_total(root / "ai_ledger.jsonl")
        if spent + AI_CREDITS_PER_CALL > budget:
            raise PipelineError(f"The {code} ledger already has {spent} credits and a call costs {AI_CREDITS_PER_CALL}, "
                                f"which passes the budget of {budget}. Raise the budget to at least "
                                f"{spent + AI_CREDITS_PER_CALL} to allow one more call.")
        if options.get("confirm_spend") is not True:
            raise PipelineError(f"Generating a model with {name} spends {AI_CREDITS_PER_CALL} credits. Confirm the "
                                "cost first (AI generate asks), or run a dry run, which is free.")
        return f"AI model: {label} ({code}, {name}, {AI_CREDITS_PER_CALL} credits)", args


def _file_option(options: dict[str, Any], key: str, flag: str, label: str, suffix: str | None = None) -> list[str]:
    """[flag, path] for an optional input file the user pasted, or [] when the field is empty."""
    raw = str(options.get(key) or "").strip()
    if not raw:
        return []
    p = clean_path(raw)
    if not p.is_absolute():
        raise PipelineError(f"{label}: use the full path, starting with the drive letter.")
    if suffix and p.suffix.lower() != suffix:
        raise PipelineError(f"{label} must be a {suffix} file.")
    if not p.is_file():
        raise PipelineError(f"{label} isn't there: no file at {p}.")
    return [flag, str(p)]


def _smooth_option(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        value = math.nan
    if not 0 <= value <= 1:      # NaN fails this too
        raise PipelineError("The smooth strength must be a number from 0 to 1, like 0.6 (or empty).")
    return value


def _int_option(raw: Any, default: int, low: int, high: int, label: str) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        value = low - 1
    if not low <= value <= high:
        raise PipelineError(f"{label} must be a whole number from {low} to {high} (default {default}).")
    return value


def ledger_total(ledger: Path) -> tuple[int, int]:
    """(credits, calls) in an AI ledger, one JSON object per line; unreadable lines count as nothing."""
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError:
        return 0, 0
    credits = calls = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            credits += int(json.loads(line).get("credits", 0))
        except (ValueError, TypeError, AttributeError):
            continue
        calls += 1
    return credits, calls


def api_key_set(env_file: Path, name: str) -> bool:
    """Whether ai3d would find the key `name`: its first line in .env, else the environment.
    Only yes/no: the value itself is never returned."""
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if line.startswith(name + "="):
            return bool(line.split("=", 1)[1].strip())
    return bool(os.environ.get(name))


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

