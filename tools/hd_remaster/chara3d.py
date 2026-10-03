"""HD sprites from a 3D model: a 2D game's character turned into a rigged, animated 3D model that
renders every sprite frame in one consistent style (a per-frame AI redraw would drift between the
frames of a walk cycle).

    python chara3d.py turnaround work/<CODE> --name crono --sheet "Chara/Chara_0000.NCER~lz"
        --front 0 --side 12 --back 3 --art refs/a.jpg,refs/b.jpg [--usd 2] [--dry-run]
    python chara3d.py model work/<CODE> --name crono [--credits 200] [--polycount 12000]
    python chara3d.py animate work/<CODE> --name crono [--anims idle,walk,run] [--credits 200]

turnaround: the sprite's standing frames (front, side, back; the other side is the side frame
mirrored, as the game flips it) on magenta, plus official artwork, go to Gemini (OpenRouter), which
draws a 2x2 character sheet in an A-pose with flat colours: front, the character's right side,
back, left side. The sheet is cut into four transparent views. USD per character is capped by
--usd (ledger chara3d/<name>/redraw_ledger.jsonl).

model: the four views -> Tripo multiview-to-model, textured (ai3d.tripo_multiview).
animate: Tripo rig-check (free), auto rig (model v1.0, biped, 25 credits) and one retarget per
preset animation played in place (10 credits each). Credits are logged in
chara3d/<name>/tripo_ledger.jsonl and capped by --credits (all Tripo calls for this character).
Keys live in tools/hd_remaster/.env (never printed).

Everything goes to work/<CODE>/chara3d/<name>/.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

import ai3d
import turnaround

MODEL = "google/gemini-3-pro-image"
MAGENTA = (255, 0, 255)
VIEWS = ("front", "right", "back", "left")
RIG_MODEL = "v1.0-20240301"          # Tripo's humanoid rig (90+ presets); v2.5 is for creatures
CREDITS_RIG = 25
CREDITS_RETARGET = 10                # per animation

PROMPT = """\
Image 1 is a 2x2 guide sheet of a 16-bit SNES game sprite of {character}, standing: top-left = \
FRONT view, top-right = the character's RIGHT side (facing right), bottom-left = BACK view, \
bottom-right = the character's LEFT side (facing left). The other images are the official \
artwork of the same character by Akira Toriyama.

Draw a 2x2 character turnaround sheet of this character for building a 3D model, in the same \
four cells and the same order, in Akira Toriyama's style (Dragon Ball, Dragon Quest, the \
official artwork): clean, rounded anime shapes, his spiky hair, outfit and colours exactly as in \
the artwork.

Strict rules:
- Proportions of the sprite: a large head, about one third of the body height (Toriyama's \
super-deformed Dragon Quest look), the same in all four views.
- A-pose: standing straight, legs slightly apart, both arms held away from the body at about \
40 degrees, hands open. The same pose in all four views; nothing in the hands.
- The four views show the same model turned in 90-degree steps, centred in their cells, the \
same size and height, feet at the same level.
- Flat unlit base colours only: no shading, no shadows, no highlights, no gradients, no \
outlines or ink lines. The face keeps its eyes, eyebrows and mouth as flat shapes.
- No sword and no scabbard.
- Background: solid flat magenta (#FF00FF) everywhere outside the character, in all four cells. \
No text, no labels, no frames, no grid lines.
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def char_dir(work: Path, name: str) -> Path:
    d = work / "chara3d" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def cell_image(work: Path, sheet: str, cell: int) -> Image.Image:
    """The native picture of one cell of a sprite sheet (all of its manifest images, placed by
    their offsets in the cell when there are several)."""
    parts = []
    for line in (work / "manifest.jsonl").open(encoding="utf-8"):
        d = json.loads(line)
        if d.get("source") == f"{sheet}#cell{cell}":
            parts.append(d)
    if not parts:
        raise SystemExit(f"no manifest image for {sheet}#cell{cell}")
    return Image.open(work / "native" / "assets2d" / f"{parts[0]['key']}.png").convert("RGBA")


def guide_sheet(views: list[Image.Image], cell: int = 512) -> Image.Image:
    """Front, right / back, left, each scaled up (nearest) to the same pixel size, feet on one line."""
    views = [v.crop(v.getbbox()) if v.getbbox() else v for v in views]
    scale = max(1, int(cell * 0.8 / max(max(v.size) for v in views)))
    out = Image.new("RGB", (cell * 2, cell * 2), MAGENTA)
    for i, v in enumerate(views):
        big = v.resize((v.width * scale, v.height * scale), Image.NEAREST)
        x = (i % 2) * cell + (cell - big.width) // 2
        y = (i // 2) * cell + int(cell * 0.9) - big.height
        out.paste(big, (x, y), big)
    return out


def cmd_turnaround(args) -> None:
    work = Path(args.work)
    d = char_dir(work, args.name)
    front = cell_image(work, args.sheet, args.front)
    side = cell_image(work, args.sheet, args.side)
    back = cell_image(work, args.sheet, args.back)
    # the side frame faces left (the character's left side); its mirror is the right side
    src = guide_sheet([front, side.transpose(Image.FLIP_LEFT_RIGHT), back, side])
    src.save(d / "turnaround_in.png")
    if args.dry_run:
        log(f"input sheet {d / 'turnaround_in.png'}; nothing sent (dry run)")
        return
    ledger = turnaround.Ledger(d / "redraw_ledger.jsonl", args.usd)
    ledger.check(turnaround.ESTIMATE)
    arts = [Image.open(p).convert("RGB") for p in args.art.split(",")]
    prompt = PROMPT.format(character=args.character or args.name)
    t0 = time.time()
    import redraw
    img, cost, text = redraw.generate(MODEL, prompt, [src] + arts, aspect="1:1", size="2K")
    ledger.add(model=MODEL, kind="chara3d turnaround", id=args.name, cost=cost, size="2K",
               result=None if img is None else list(img.size))
    if img is None:
        raise SystemExit(f"no picture came back (${cost:.3f}): {text[:300]}")
    n = len(list(d.glob("turnaround_raw*.png")))
    img.save(d / f"turnaround_raw{'' if n == 0 else n}.png")
    views = turnaround.split(img, 1024)
    for name, v in zip(VIEWS, views):
        Image.fromarray(v, "RGBA").save(d / f"view_{name}.png")
    log(f"turnaround {img.size[0]}x{img.size[1]} in {time.time() - t0:.0f} s, ${cost:.3f} "
        f"(${ledger.total:.2f} of ${args.usd:.2f} for {args.name}); views in {d}")


def _tripo_wait(key: str, task: str) -> dict:
    while True:
        time.sleep(4)
        info = ai3d._tripo("GET", f"/tasks/{task}", key)
        status = info.get("status")
        log(f"  {status} {info.get('progress', 0)}%")
        if status == "success":
            return info
        if status in ("failed", "cancelled", "banned", "expired", "unknown"):
            raise SystemExit(f"tripo task {task} {status}: {info.get('error_code')} {info.get('error_message')}")


def _ledger_add(ledger: Path, **entry) -> None:
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "provider": "tripo", **entry}) + "\n")


def _check(ledger: Path, estimate: int, cap: int) -> None:
    spent = ai3d._ledger_total(ledger)
    if spent + estimate > cap:
        raise SystemExit(f"budget: {spent} credits used, this call costs about {estimate}, cap {cap}")


def cmd_model(args) -> None:
    work = Path(args.work)
    d = char_dir(work, args.name)
    views = [np.array(Image.open(d / f"view_{v}.png").convert("RGBA")) for v in VIEWS]
    # tripo_multiview wants RGB-ish pictures; flatten on white like its reference renders
    flat = []
    for v in views:
        a = v[..., 3:4].astype(np.float32) / 255
        rgb = v[..., :3].astype(np.float32) * a + 255 * (1 - a)
        flat.append(np.dstack([rgb.astype(np.uint8), v[..., 3]]))
    path = ai3d.tripo_multiview(flat, d / "tripo", d / "tripo_ledger.jsonl", args.credits, args.polycount,
                                f"{args.name} turnaround", textured=True)
    (d / "model.json").write_text(json.dumps({"task": path.stem, "glb": str(path)}, indent=1), encoding="utf-8")
    log(f"model {path}")


def cmd_animate(args) -> None:
    work = Path(args.work)
    d = char_dir(work, args.name)
    key = ai3d._key("TRIPO_API_KEY")
    if not key:
        raise SystemExit("no TRIPO_API_KEY in tools/hd_remaster/.env")
    ledger = d / "tripo_ledger.jsonl"
    info = json.loads((d / "model.json").read_text(encoding="utf-8"))
    model_task = info["task"]
    anims = [a.strip() for a in args.anims.split(",") if a.strip()]
    _check(ledger, CREDITS_RIG + CREDITS_RETARGET * len(anims), args.credits)
    if not info.get("rig_task"):
        check = ai3d._tripo("POST", "/animations/rig-check", key, {"input": model_task})["task_id"]
        out = _tripo_wait(key, check).get("output") or {}
        log(f"rig check: {out}")
        if out.get("riggable") is False:
            raise SystemExit("Tripo says the model can't be rigged")
        rig = ai3d._tripo("POST", "/animations/rig", key, {
            "input": model_task, "model": RIG_MODEL, "rig_type": "biped", "spec": "tripo", "out_format": "glb"})["task_id"]
        _ledger_add(ledger, task=rig, model=args.name, credits=CREDITS_RIG, kind="rig")
        res = _tripo_wait(key, rig)
        url = (res.get("output") or {}).get("model_url")
        if url:
            with urllib.request.urlopen(url, timeout=300) as r:
                (d / "tripo" / "rigged.glb").write_bytes(r.read())
        info["rig_task"] = rig
        (d / "model.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    for anim in anims:
        if (d / "tripo" / f"anim_{anim}.glb").exists():
            log(f"{anim}: already there")
            continue
        task = ai3d._tripo("POST", "/animations/retarget", key, {
            "input": info["rig_task"], "animation": f"preset:{anim}", "out_format": "glb",
            "bake_animation": True, "export_with_geometry": True, "animate_in_place": True})["task_id"]
        _ledger_add(ledger, task=task, model=args.name, credits=CREDITS_RETARGET, kind=f"retarget {anim}")
        res = _tripo_wait(key, task)
        url = (res.get("output") or {}).get("model_url")
        if not url:
            raise SystemExit(f"retarget {anim}: no model_url in {list((res.get('output') or {}).keys())}")
        with urllib.request.urlopen(url, timeout=300) as r:
            (d / "tripo" / f"anim_{anim}.glb").write_bytes(r.read())
        log(f"{anim}: {d / 'tripo' / f'anim_{anim}.glb'}")
    log(f"Tripo credits for {args.name}: {ai3d._ledger_total(ledger)} of {args.credits}")


BLENDER = Path(r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe")
DIRECTIONS = {"down": 0, "left": -90, "up": 180, "right": 90}     # yaw: 0 faces the camera
SAMPLES = {"idle": 6, "walk": 24, "run": 16}                       # phases rendered per clip


def _blender(job: dict, path: Path, blender: Path) -> None:
    import subprocess
    path.write_text(json.dumps(job, indent=1), encoding="utf-8")
    r = subprocess.run([str(blender), "-b", "-P", str(Path(__file__).with_name("chara3d_blender.py")), "--",
                        str(path)], capture_output=True, text=True, errors="replace")
    if "RENDERED" not in r.stdout:
        raise SystemExit(f"blender failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")


def render_candidates(d: Path, args) -> Path:
    """Every clip at SAMPLES phases in the four DIRECTIONS, one camera framing for all of them."""
    out = (d / "renders").resolve()          # Blender resolves relative output paths its own way
    blender = Path(args.blender)
    base = {"out": str(out), "size": args.size, "elevation": args.elevation, "outline": args.outline,
            "band": 0.35}
    frames_of = {clip: [{"name": f"{clip}_{i:02d}_{dname}", "action": clip, "phase": i / n, "yaw": yaw}
                        for i in range(n) for dname, yaw in DIRECTIONS.items()]
                 for clip, n in SAMPLES.items()}
    framing = out / "framing.json"
    if not framing.exists():
        # idle over all turns, with room for the stride and arm swing of walk and run
        _blender(dict(base, glb=str((d / "tripo" / "anim_idle.glb").resolve()), frames=frames_of["idle"], margin=1.45),
                 d / "job_idle.json", blender)
        meta = json.loads((out / "render.json").read_text(encoding="utf-8"))
        framing.write_text(json.dumps({"ortho_scale": meta["ortho_scale"], "centre": meta["centre"]}), encoding="utf-8")
    fixed = json.loads(framing.read_text(encoding="utf-8"))
    for clip, frames in frames_of.items():
        todo = [f for f in frames if not (out / f"{f['name']}.png").exists()]
        if todo:
            log(f"rendering {len(todo)} {clip} frames")
            _blender(dict(base, **fixed, glb=str((d / "tripo" / f"anim_{clip}.glb").resolve()), frames=todo),
                     d / f"job_{clip}.json", blender)
    return out


def _rgba_resize(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    # premultiplied, so transparent pixels don't bleed their colour into the edges
    return im.convert("RGBa").resize(size, Image.LANCZOS).convert("RGBA")


def _place_score(cand_m: np.ndarray, cand_c: np.ndarray, nat_m: np.ndarray, nat_c: np.ndarray,
                 ox: int, oy: int) -> tuple[float, float]:
    """IoU of the candidate's native-resolution mask placed at (ox, oy) in the cell, and the mean
    colour distance (0..1) where both are opaque."""
    h, w = nat_m.shape
    ch, cw = cand_m.shape
    m = np.zeros((h, w), bool)
    c = np.zeros((h, w, 3), np.float32)
    x0, y0 = max(0, ox), max(0, oy)
    x1, y1 = min(w, ox + cw), min(h, oy + ch)
    if x1 <= x0 or y1 <= y0:
        return 0.0, 1.0
    m[y0:y1, x0:x1] = cand_m[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
    c[y0:y1, x0:x1] = cand_c[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
    inter = m & nat_m
    # the candidate's pixels that fall outside the cell count too: a standing render must not
    # "fit" a crouching frame by hanging out of the top of the cell
    union = int(nat_m.sum()) + int(cand_m.sum()) - int(inter.sum())
    iou = inter.sum() / union if union else 0.0
    col = float(np.abs(c[inter] - nat_c[inter]).mean() / 255) if inter.any() else 1.0
    return float(iou), col


def _head_blocks(im: Image.Image, n: int = 8) -> np.ndarray:
    """The top 45% of a sprite's opaque box as n x n premultiplied RGBA blocks: where the face,
    the back of the head or a profile shows. Compared between native frames only (one palette)."""
    box = im.getbbox()
    im = im.crop(box) if box else im
    head = im.crop((0, 0, im.width, max(1, round(im.height * 0.45))))
    return np.asarray(head.convert("RGBa").resize((n, n), Image.BOX), np.float32)


def facing(nat: Image.Image, refs: dict[str, np.ndarray]) -> tuple[str, float]:
    """Which way a native frame faces: the standing frame whose head region is closest."""
    h = _head_blocks(nat)
    dist = {k: float(np.abs(h - r).mean()) for k, r in refs.items()}
    k = min(dist, key=dist.get)
    return k, dist[k]


def cmd_sprites(args) -> None:
    work = Path(args.work)
    d = char_dir(work, args.name)
    out = render_candidates(d, args)
    S = json.loads((work / "recipe.json").read_text(encoding="utf-8")).get("scale", 4)
    # the sheet's single-image cells (cells split into several images are other things: effects,
    # a sword drawn apart from the body)
    cells: dict[int, list[dict]] = {}
    for line in (work / "manifest.jsonl").open(encoding="utf-8"):
        m = json.loads(line)
        if m.get("source", "").startswith(args.sheet + "#cell"):
            cells.setdefault(int(m["source"].split("#cell")[1]), []).append(m)
    # candidates: render cropped to its opaque box, at the pack's scale
    ref_native = cell_image(work, args.sheet, args.front)
    ref_box = ref_native.getbbox()
    ref_render = Image.open(out / "idle_00_down.png").convert("RGBA")
    k = (ref_box[3] - ref_box[1]) * S / (ref_render.getbbox()[3] - ref_render.getbbox()[1])
    cands = []
    for p in sorted(out.glob("*_*_*.png")):
        im = Image.open(p).convert("RGBA")
        box = im.getbbox()
        if not box:
            continue
        im = im.crop(box)
        hd = _rgba_resize(im, (max(1, round(im.width * k)), max(1, round(im.height * k))))
        low = _rgba_resize(hd, (max(1, round(hd.width / S)), max(1, round(hd.height / S))))
        la = np.asarray(low)
        cands.append((p.stem, hd, la[..., 3] > 127, la[..., :3].astype(np.float32)))
    log(f"{len(cands)} candidate renders, scale {k:.3f} render px -> HD px")
    # facing of each native frame from the game's own standing frames (the render colours differ
    # from the game's palette too much to tell a face from the back of a head)
    side = cell_image(work, args.sheet, args.side)
    refs = {"down": _head_blocks(ref_native), "up": _head_blocks(cell_image(work, args.sheet, args.back)),
            "left": _head_blocks(side), "right": _head_blocks(side.transpose(Image.FLIP_LEFT_RIGHT))}
    results = []
    wanted = None
    if args.cells:
        wanted = set()
        for part in args.cells.split(","):
            a, _, b = part.partition("-")
            wanted.update(range(int(a), int(b or a) + 1))
    for cell, ms in sorted(cells.items()):
        if len(ms) != 1 or (wanted is not None and cell not in wanted):
            continue
        m = ms[0]
        nat = np.asarray(Image.open(work / "native" / "assets2d" / f"{m['key']}.png").convert("RGBA"))
        nat_m = nat[..., 3] > 0
        if nat_m.sum() < 40:
            continue
        nat_c = nat[..., :3].astype(np.float32)
        ys, xs = np.nonzero(nat_m)
        nb, ncx = ys.max(), xs.mean()
        face, fdist = facing(Image.fromarray(nat, "RGBA"), refs)
        best = None
        for name, hd, cm, cc in cands:
            if not name.endswith("_" + face):
                continue
            cys, cxs = np.nonzero(cm)
            if not len(cys):
                continue
            bx, by = int(round(ncx - cxs.mean())), int(nb - cys.max())
            for dy in (-2, -1, 0, 1):
                for dx in (-2, -1, 0, 1, 2):
                    iou, col = _place_score(cm, cc, nat_m, nat_c, bx + dx, by + dy)
                    score = iou - args.colour_weight * col
                    if best is None or score > best[0]:
                        best = (score, iou, col, name, hd, bx + dx, by + dy)
        results.append((cell, m, best))
        log(f"cell {cell:3d} {m['key']}: faces {face} ({fdist:.0f}), {best[3]} iou {best[1]:.2f} "
            f"colour {best[2]:.2f} score {best[0]:.2f}")
    # review sheet: native x S | HD, score
    from PIL import ImageDraw
    cw, ch = 72 * S // 2, 72 * S // 2
    cols = 8
    rows = (len(results) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cw * 2, rows * (ch + 14)), (90, 90, 110))
    dr = ImageDraw.Draw(sheet)
    accepted = 0
    red = work / "redrawn" / "assets2d"
    for i, (cell, m, best) in enumerate(results):
        score, iou, col, name, hd, ox, oy = best
        canvas = Image.new("RGBA", (m["w"] * S, m["h"] * S), (0, 0, 0, 0))
        canvas.paste(hd, (ox * S, oy * S), hd)
        nat = Image.open(work / "native" / "assets2d" / f"{m['key']}.png").convert("RGBA")
        nat = nat.resize((m["w"] * S, m["h"] * S), Image.NEAREST)
        x, y = (i % cols) * cw * 2, (i // cols) * (ch + 14)
        for j, im in enumerate((nat, canvas)):
            t = im.copy()
            t.thumbnail((cw - 4, ch - 4))
            sheet.paste(t, (x + j * cw + 2, y + 14), t)
        ok = score >= args.min_score
        dr.text((x + 2, y + 1), f"{cell} {name} {score:.2f}", fill=(140, 255, 140) if ok else (255, 140, 140))
        if ok and args.apply:
            red.mkdir(parents=True, exist_ok=True)
            canvas.save(red / f"{m['key']}.png")
            accepted += 1
    sheet.save(d / "review_sprites.png")
    good = sum(1 for _, _, b in results if b[0] >= args.min_score)
    log(f"{good}/{len(results)} cells fit (score >= {args.min_score}); review {d / 'review_sprites.png'}"
        + (f"; wrote {accepted} to {red}" if args.apply else "; --apply writes them to redrawn/"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("turnaround")
    t.add_argument("work"); t.add_argument("--name", required=True)
    t.add_argument("--sheet", required=True, help="manifest source of the sprite sheet, e.g. Chara/Chara_0000.NCER~lz")
    t.add_argument("--front", type=int, required=True); t.add_argument("--side", type=int, required=True)
    t.add_argument("--back", type=int, required=True)
    t.add_argument("--art", required=True, help="official artwork, comma-separated files")
    t.add_argument("--character", help="who it is, for the prompt (e.g. 'Crono from Chrono Trigger')")
    t.add_argument("--usd", type=float, default=2.0); t.add_argument("--dry-run", action="store_true")
    t.set_defaults(fn=cmd_turnaround)
    m = sub.add_parser("model")
    m.add_argument("work"); m.add_argument("--name", required=True)
    m.add_argument("--credits", type=int, default=200); m.add_argument("--polycount", type=int, default=12000)
    m.set_defaults(fn=cmd_model)
    a = sub.add_parser("animate")
    a.add_argument("work"); a.add_argument("--name", required=True)
    a.add_argument("--anims", default="idle,walk,run"); a.add_argument("--credits", type=int, default=200)
    a.set_defaults(fn=cmd_animate)
    s = sub.add_parser("sprites")
    s.add_argument("work"); s.add_argument("--name", required=True)
    s.add_argument("--sheet", required=True); s.add_argument("--front", type=int, required=True,
                                                             help="the standing front cell (sets the scale)")
    s.add_argument("--size", type=int, default=768, help="render size in px (downscaled to the pack's scale)")
    s.add_argument("--elevation", type=float, default=12.0, help="camera angle above the horizon, degrees")
    s.add_argument("--outline", type=float, default=6.0, help="ink line width at --size, px")
    s.add_argument("--side", type=int, required=True, help="the standing side cell, facing left")
    s.add_argument("--back", type=int, required=True, help="the standing back cell")
    s.add_argument("--min-score", type=float, default=0.6)
    s.add_argument("--colour-weight", type=float, default=0.6)
    s.add_argument("--cells", help="only these cells, e.g. 0-39,147-168 (the poses the clips cover)")
    s.add_argument("--apply", action="store_true", help="write the fitted cells to redrawn/assets2d")
    s.add_argument("--blender", default=str(BLENDER))
    s.set_defaults(fn=cmd_sprites)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
