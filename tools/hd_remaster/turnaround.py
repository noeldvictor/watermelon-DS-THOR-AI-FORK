"""HD turnaround references for an AI 3D model: the game model's four views redrawn by an image
model in the style of the official artwork, kept in the game model's pose and framing.

    hd_remaster.py models turnaround work/<CODE> --model <id> --art a.jpg,b.jpg
        [--without eyeL,...] [--blank mouth,...] [--character "Link (Toon Link) from ..."] [--usd 2] [--dry-run]

One call draws all four views on one 2x2 sheet (one picture keeps them consistent with each
other; four calls would each invent their own details). Input: the game model rendered flat in
T-pose (front, its right side, back, its left side) on magenta, plus the artwork. The answer is
cut back into four transparent views (models/<id>/ai/hd_ref_<view>.png) that `models ai
--refs hd` sends to Tripo instead of the game renders, and each view's outline is scored against
the game model's (a view the model moved or reshaped would bend the fit).

Parts listed in --without (shape or material names) are left out of the renders: the parts the
game keeps drawing itself (eye decals for their animation, a sword that moves to the hand).
Parts listed in --blank are drawn with their features painted out (dark texels -> the texture's
main colour): face patches that ARE the face surface (Phantom Hourglass Link's mouth and brow
parts are skin patches; leaving them out leaves a hole in the face). Costs go to models/<id>/ai/redraw_ledger.jsonl, capped by --usd (per character).
--dry-run writes the input sheet (ai/turnaround_in.png) and sends nothing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

import models3d

MODEL = "google/gemini-3-pro-image"
ESTIMATE = 0.30                     # USD per call at 2K, for the budget check before a call
MAGENTA = np.array([255, 0, 255], np.float32)
VIEWS = ("front", "right", "back", "left")          # the order ai3d.reference_views renders

PROMPT = """\
Image 1 is a 2x2 character turnaround sheet of a low-poly Nintendo DS 3D model standing in a \
T-pose: top-left = FRONT view, top-right = the character's RIGHT side, bottom-left = BACK view, \
bottom-right = the character's LEFT side.{character} The other images are official artwork of \
the same character.

Redraw all four views as the same character remade for a modern console in the style of the \
official artwork, like an HD remaster on the Wii U: smooth rounded shapes, clean silhouettes, \
crisp painted detail in the cloth folds, hair locks, belt, buckle and boots.

Strict rules:
- Keep every view exactly where it is in its cell: the same T-pose, position, size, silhouette \
and proportions as image 1, so the four views line up with the 3D model.
- Flat, unlit base colours only: no shading, no shadows, no highlights, no rim light, no \
outlines, no lighting gradients. The colours match the official artwork.
- The face has no eyes, no eyebrows and no mouth (those are separate animated parts); keep only \
the small nose.
- Nothing in the hands and nothing on the back: no sword, no shield, no sheath, no fairy.
- Background: solid flat magenta (#FF00FF) everywhere outside the character, in all four cells. \
No text, no labels, no frames, no grid lines.
"""


def log(msg: str) -> None:
    print(msg, flush=True)


class Ledger:
    """USD spent per character, one json line per call."""

    def __init__(self, path: Path, budget: float):
        self.path, self.budget = path, budget
        self.total = (sum(json.loads(line)["cost"] for line in path.open(encoding="utf-8"))
                      if path.exists() else 0.0)

    def check(self, estimate: float) -> None:
        if self.total + estimate > self.budget:
            raise SystemExit(f"budget: ${self.total:.2f} of ${self.budget:.2f} spent, a call costs about "
                             f"${estimate:.2f} (see {self.path})")

    def add(self, **entry) -> None:
        self.total += entry["cost"]
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}) + "\n")


def paint_out(tex: np.ndarray) -> np.ndarray:
    """A texture with its dark features (eye lines, brows, mouth) in its most common opaque colour."""
    opaque = tex[..., 3] > 127
    if not opaque.any():
        return tex
    cols, counts = np.unique(tex[opaque][:, :3], axis=0, return_counts=True)
    main = cols[np.argmax(counts)]
    luma = tex[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    out = tex.copy()
    out[opaque & (luma < 90), :3] = main
    return out


def game_views(model, folder: Path, info: dict, hd: dict, without: set[str], size: int,
               blank: set[str] = frozenset()) -> list[np.ndarray]:
    """The model's four views (RGBA), flat, without the listed parts, features of `blank` parts
    painted out."""
    import ai3d
    import modelpack
    parts = modelpack._textured_parts(model, folder, info, hd)
    keep = []
    for (d, _), part in zip(models3d.model_meshes(model), parts):
        mat = model.materials[d.material] if d.material is not None and d.material < len(model.materials) else None
        names = {model.shapes[d.shape].name} | ({mat.name} if mat else set())
        if names & without:
            continue
        if names & blank and "texture" in part:
            part = dict(part, texture=paint_out(part["texture"]))
        keep.append(part)
    if not keep:
        raise SystemExit("--without left no parts to draw")
    return ai3d.reference_views(keep, size)


def sheet(views: list[np.ndarray]) -> Image.Image:
    """The four views in a 2x2 grid on magenta (front, right / back, left)."""
    cell = views[0].shape[0]
    out = Image.new("RGB", (cell * 2, cell * 2), tuple(int(c) for c in MAGENTA))
    for i, v in enumerate(views):
        rgba = Image.fromarray(v, "RGBA")
        out.paste(rgba, ((i % 2) * cell, (i // 2) * cell), rgba)
    return out


def key_magenta(rgb: np.ndarray) -> np.ndarray:
    """RGBA from a picture on flat magenta: alpha from the distance to magenta, edge colours
    un-blended from it (no pink fringe)."""
    c = rgb.astype(np.float32)
    dist = np.sqrt(((c - MAGENTA) ** 2).sum(-1))
    a = np.clip((dist - 60) / 90, 0, 1)
    solid = np.maximum(a, 1e-3)[..., None]
    fg = np.clip((c - (1 - a)[..., None] * MAGENTA) / solid, 0, 255)
    out = np.zeros(rgb.shape[:2] + (4,), np.uint8)
    out[..., :3] = np.rint(fg)
    out[..., 3] = np.rint(a * 255)
    return out


def split(img: Image.Image, cell: int) -> list[np.ndarray]:
    """The answer's four cells, keyed, at the input's cell size."""
    w, h = img.size
    out = []
    for i in range(4):
        x, y = (i % 2) * w // 2, (i // 2) * h // 2
        part = img.convert("RGB").crop((x, y, x + w // 2, y + h // 2)).resize((cell, cell), Image.LANCZOS)
        out.append(key_magenta(np.array(part)))
    return out


def overlap(a: np.ndarray, b: np.ndarray) -> float:
    """Intersection over union of two RGBA pictures' opaque areas."""
    ma, mb = a[..., 3] > 127, b[..., 3] > 127
    union = (ma | mb).sum()
    return float((ma & mb).sum() / union) if union else 0.0


def review(game: list[np.ndarray], hd: list[np.ndarray]) -> Image.Image:
    """Game views over HD views, on grey, for a person to look at."""
    cell = 384
    out = Image.new("RGB", (cell * 4, cell * 2), (96, 96, 96))
    for i, (g, n) in enumerate(zip(game, hd)):
        for row, v in enumerate((g, n)):
            im = Image.fromarray(v, "RGBA").resize((cell, cell), Image.LANCZOS)
            out.paste(im, (i * cell, row * cell), im)
    return out


def make(work: Path, mid: str, art: list[Path], without: list[str], character: str,
         budget: float, packs_root: Path, size: str = "2K", cell: int = 1024,
         dry_run: bool = False, blank: list[str] = (), decals: list[str] = ()) -> list[Path]:
    import modelpack
    import redraw
    model, folder, info, blobs = modelpack._load_model(work, mid)
    keys = modelpack._material_keys(model, blobs)
    hd = {i: img for i, k in keys.items() if (img := modelpack._hd_image(work, packs_root, k)) is not None}
    ai_dir = folder / "ai"
    ai_dir.mkdir(exist_ok=True)
    game = game_views(model, folder, info, hd, set(without), cell, set(blank))
    src = sheet(game)
    src.save(ai_dir / "turnaround_in.png")
    if dry_run:
        log(f"input sheet {ai_dir / 'turnaround_in.png'}; nothing sent (dry run)")
        return []
    ledger = Ledger(ai_dir / "redraw_ledger.jsonl", budget)
    ledger.check(ESTIMATE)
    prompt = PROMPT.format(character=f" The character is {character}." if character else "")
    arts = [Image.open(p).convert("RGB") for p in art]
    t0 = time.time()
    img, cost, text = redraw.generate(MODEL, prompt, [src] + arts, aspect="1:1", size=size)
    ledger.add(model=MODEL, kind="turnaround", id=mid, cost=cost, size=size,
               result=None if img is None else list(img.size))
    if img is None:
        raise SystemExit(f"no picture came back (${cost:.3f}): {text[:300]}")
    img.save(ai_dir / "turnaround_raw.png")
    views = split(img, cell)
    scores = [overlap(g, v) for g, v in zip(game, views)]
    # what the fit needs to know: parts the game keeps drawing over or beside the new mesh
    (ai_dir / "turnaround.json").write_text(json.dumps(
        {"without": list(without), "blank": list(blank), "decals": list(decals), "character": character,
         "art": [str(p) for p in art], "model": MODEL, "size": size}, indent=1), encoding="utf-8")
    paths = []
    for name, v in zip(VIEWS, views):
        p = ai_dir / f"hd_ref_{name}.png"
        Image.fromarray(v, "RGBA").save(p)
        paths.append(p)
    review(game, views).save(ai_dir / "turnaround_review.png")
    log(f"turnaround {img.size[0]}x{img.size[1]} in {time.time() - t0:.0f} s, ${cost:.3f} "
        f"(${ledger.total:.2f} of ${budget:.2f} for this model); outline overlap with the game model: "
        + ", ".join(f"{n} {s:.2f}" for n, s in zip(VIEWS, scores)))
    log(f"look at {ai_dir / 'turnaround_review.png'} (game top, HD below); next: models ai {work} "
        f"--model {mid} --refs hd --textured")
    return paths
