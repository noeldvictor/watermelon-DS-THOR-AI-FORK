"""Per-game recipes (games/<GAMECODE>/recipe.json) and the model registry (models.json).

A recipe records what was learned making a good pack for one game, so anyone with the same ROM
gets the same result by running `hd_remaster.py all game.nds`:

- which ROMs it was verified on (by SHA-256; other revisions still run, with a warning),
- the output scale and the model for each category (textures, sprites, backgrounds),
- game-specific 2D load rules that can't be read from the files (see twod.py),
- the key counts an extraction should produce, and what was verified against real play.

Recipes hold no game data. Every image is generated from the user's own ROM.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
GAMES = HERE / "games"
MODELS = HERE / "models"
REGISTRY = HERE / "models.json"
CATEGORIES = ("textures", "sprites", "backgrounds", "fonts")
DEFAULT_MODEL = "4x-UltraSharp"


def load(code: str, rom: bytes | None = None) -> dict:
    """The recipe for a game code, filled in with defaults when there is none."""
    path = GAMES / code / "recipe.json"
    recipe = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    recipe.setdefault("game", code)
    recipe.setdefault("scale", 4)
    models = recipe.setdefault("models", {})
    for c in CATEGORIES:
        models.setdefault(c, DEFAULT_MODEL)
    recipe.setdefault("twod", {})
    recipe.setdefault("baseline", {})
    recipe["has_recipe"] = path.exists()
    if rom is not None:
        sha = hashlib.sha256(rom).hexdigest()
        match = next((r for r in recipe.get("roms", []) if r["sha256"] == sha), None)
        recipe["rom_sha256"] = sha
        recipe["rom_match"] = match["name"] if match else None
    return recipe


def describe(recipe: dict) -> str:
    if not recipe["has_recipe"]:
        return f"no recipe for {recipe['game']} yet: using defaults"
    title = recipe.get("title", recipe["game"])
    if recipe.get("rom_match"):
        return f"recipe: {title}, ROM matches {recipe['rom_match']}"
    if "rom_sha256" in recipe:
        return (f"recipe: {title}, but this ROM ({recipe['rom_sha256'][:12]}...) isn't one it was verified "
                f"on - a different revision or a patched ROM. It will still run; results may differ.")
    return f"recipe: {title}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_path(name: str) -> Path:
    """A model file for a registry name (downloaded and checked on first use) or a path."""
    p = Path(name)
    if p.suffix and p.exists():
        return p
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if name not in registry:
        raise SystemExit(f"unknown model '{name}': add it to {REGISTRY.name} or pass a model file path")
    entry = registry[name]
    dst = MODELS / entry["file"]
    if not dst.exists():
        MODELS.mkdir(parents=True, exist_ok=True)
        print(f"downloading {name} ({entry['license']})", flush=True)
        tmp = dst.with_name(dst.name + ".part")
        urllib.request.urlretrieve(entry["url"], tmp)
        tmp.replace(dst)
    if entry.get("sha256") and _sha256(dst) != entry["sha256"]:
        raise SystemExit(f"{dst} doesn't match the checksum in {REGISTRY.name}; delete it to download again")
    return dst


def check_baseline(recipe: dict, counts: dict) -> bool:
    """Compare key counts with the recipe's baseline; True when they all agree."""
    base = recipe.get("baseline") or {}
    if not base:
        return True
    ok = True
    for c in CATEGORIES:
        if c in base:
            same = counts.get(c, 0) == base[c]
            ok &= same
            print(f"  {c:11s} {counts.get(c, 0):6d} keys  (recipe: {base[c]}) {'ok' if same else 'DIFFERENT'}",
                  flush=True)
    if not ok:
        print("  counts differ from the recipe: a different ROM revision, or a tool change since it was "
              "verified", file=sys.stderr, flush=True)
    return ok
