"""HD remastering for Watermelon Thor: build a texture pack straight from a DS ROM.

    python hd_remaster.py extract ROM.nds                  # every texture, native size, named by key
    python hd_remaster.py verify  work/BSDE --dumps DIR    # check keys and pixels against real dumps
    python hd_remaster.py upscale work/BSDE                # AI upscale (see upscale.py)
    python hd_remaster.py build   work/BSDE                # assemble packs/BSDE
    python hd_remaster.py push    packs/BSDE               # install on the device over adb
    python hd_remaster.py all     ROM.nds                  # extract + upscale + build

Work goes to tools/hd_remaster/work/<GAMECODE>/, packs to tools/hd_remaster/packs/<GAMECODE>/.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fonts  # noqa: E402
import nitro  # noqa: E402
import recipes  # noqa: E402
import tex3d  # noqa: E402

WORK = HERE / "work"
PACKS = HERE / "packs"
PACKAGE = "me.magnum.melondualds.dev"


def log(msg: str) -> None:
    print(msg, flush=True)


def alpha_kind(img: np.ndarray) -> str:
    a = img[..., 3]
    if a.min() == 255:
        return "opaque"
    if np.isin(a, (0, 255)).all():
        return "binary"
    return "soft"


# ---------------------------------------------------------------------------- extract

def cmd_extract(args) -> Path:
    rom = Path(args.rom).read_bytes()
    code = nitro.game_code(rom)
    out = Path(args.out or WORK) / code
    tex_dir = out / "native" / "textures"
    tex_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    recipe = recipes.load(code, rom)
    log(f"{code} ({nitro.game_title(rom)}): {recipes.describe(recipe)}")
    (out / "recipe.json").write_text(json.dumps(recipe, indent=1), encoding="utf-8")
    files = nitro.files(rom)
    blobs = [nitro.Blob(name, data) for name, data in files.items()]
    log(f"{len(blobs)} files after unpacking, {time.time() - t0:.0f}s")
    blocks = tex3d.scan(blobs) + tex3d.scan_raw(blobs)
    ents = tex3d.entries(blocks)
    ntex = sum(len(b.textures) for b in blocks)
    by = {}
    for e in ents:
        by[e.pairing] = by.get(e.pairing, 0) + 1
    log(f"3D: {sum(1 for b in blocks if b.textures)} texture blocks, {ntex} textures, "
        f"{len(ents)} keys (pairing: {by})")

    with open(out / "manifest.jsonl", "w", encoding="utf-8") as mf:
        for e in ents:
            img = tex3d.decode(e.tex, e.pal)
            write_native(out, "textures", e.key, img)
            mf.write(json.dumps({
                "kind": "tex1", "category": "textures", "key": e.key, "legacy_key": e.legacy_key,
                "w": e.tex.w, "h": e.tex.h, "fmt": e.tex.fmt,
                "texture": e.tex.name, "palette": e.pal_name, "pairing": e.pairing,
                "source": e.source, "wrap": e.tex.wrap, "alpha": alpha_kind(img),
            }) + "\n")
        log(f"wrote {len(ents)} textures to {tex_dir} in {time.time() - t0:.0f}s")
        n_obj, n_bg = extract_2d(files, out, mf, recipe["twod"])
        n_fonts = extract_fonts(files, out, mf)
    log(f"done in {time.time() - t0:.0f}s")
    recipes.check_baseline(recipe, {"textures": len(ents), "sprites": n_obj, "backgrounds": n_bg,
                                    "fonts": n_fonts})
    return out


def write_native(work: Path, sub: str, name: str, img: np.ndarray) -> None:
    """Write a native image, and drop its upscaled copy if the pixels changed since last time.

    A key names the texture, not every detail of how it's decoded (a '$' key covers any
    palette, and transparency guesses can change), so an unchanged name can still need a new
    image; upscale skips files that exist, so the stale one has to go.
    """
    path = work / "native" / sub / f"{name}.png"
    if path.exists():
        if np.array_equal(np.asarray(Image.open(path).convert("RGBA")), img):
            return
        (work / "upscaled" / sub / f"{name}.png").unlink(missing_ok=True)
    Image.fromarray(img, "RGBA").save(path, optimize=False, compress_level=1)


def extract_2d(files: dict[str, bytes], out: Path, mf, rules: dict) -> tuple[int, int]:
    """Sprites and BG tiles: whole cells and screens, each with the crops its keys cut out.

    rules: the recipe's game-specific 2D load rules (see twod.py). Returns the sprite and BG
    tile key counts.
    """
    import twod

    lib = twod.Library(files)
    profile = rules or None
    adir = out / "native" / "assets2d"
    adir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    n_assets = n_solo = n_obj = n_bg = 0

    def write_asset(name: str, img: np.ndarray, entries: list[dict], asset: dict) -> None:
        write_native(out, "assets2d", name, img)
        mf.write(json.dumps({
            "kind": "asset2d", "category": "sprites" if asset["kind"] == "cell" else "backgrounds",
            "key": name, "w": int(img.shape[1]), "h": int(img.shape[0]),
            "source": asset["name"], "palette": asset.get("nclr"), "pairing": asset["pairing"],
            "entries": [{k: en[k] for k in ("key", "x", "y", "w", "h", "hflip", "vflip", "pal_guess")}
                        for en in entries],
        }) + "\n")

    for gen in (twod.build_cells(lib, profile, want_rgba=True), twod.build_screens(lib, profile, want_rgba=True)):
        for asset in gen:
            keep = []
            for en in asset["entries"]:
                if en["key"] in seen:
                    continue
                if asset["kind"] == "cell":
                    # replacement skips rotscale and window OBJs, and fully transparent ones never match
                    if not en["opaque"] or en["rotscale"] or en["objmode"] == 2:
                        continue
                    if not en["clean"]:
                        # another OBJ in this cell covers part of it; a crop from the composite
                        # would pick up its pixels, so this one is upscaled on its own
                        solo = asset["obj_rgba"][en["key"]]
                        write_asset(f"solo_{en['key']}", solo, [dict(en, x=0, y=0, hflip=False, vflip=False)], asset)
                        seen.add(en["key"])
                        n_solo += 1
                        n_obj += 1
                        continue
                keep.append(en)
                seen.add(en["key"])
                if en["key"].startswith("obj1"):
                    n_obj += 1
                else:
                    n_bg += 1
            if keep:
                n_assets += 1
                write_asset(f"a{n_assets:05d}", asset["image"], keep, asset)
    log(f"2D: {n_assets} cells/screens + {n_solo} standalone sprites -> {n_obj} sprite keys, {n_bg} BG tile keys")
    return n_obj, n_bg


def extract_fonts(files: dict[str, bytes], out: Path, mf) -> int:
    """NFTR fonts: text is drawn from these at runtime, so the pack carries the font file and
    an upscaled atlas of its glyphs, and the emulator redraws the glyphs it finds in sprites."""
    fdir = out / "native" / "fonts"
    fdir.mkdir(parents=True, exist_ok=True)
    names: set[str] = set()
    n = 0
    for path, data in sorted(files.items()):
        if not fonts.is_nftr(data):
            continue
        name = Path(path.split("~")[0]).stem
        while name in names:
            name += "_"
        names.add(name)
        font = fonts.parse(data, name)
        grey = fonts.atlas(font)
        rgba = np.dstack([grey, grey, grey, np.full_like(grey, 255)])
        write_native(out, "fonts", name, rgba)
        (fdir / f"{name}.nftr").write_bytes(data)
        mf.write(json.dumps({
            "kind": "font", "category": "fonts", "key": name, "w": int(grey.shape[1]),
            "h": int(grey.shape[0]), "source": path, "glyphs": len(font.glyphs),
            "cell": [font.cell_w, font.cell_h], "bpp": font.bpp,
        }) + "\n")
        n += 1
    if n:
        log(f"fonts: {n} ({', '.join(sorted(names))})")
    return n


# ---------------------------------------------------------------------------- verify

def read_manifest(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def dump_key(j: dict) -> str:
    pal = j["palhash"] if j.get("palhash") else "none"
    return f"tex1_{j['w']}x{j['h']}_{j['texhash']}_{pal}_{j['fmt']}"


def cmd_verify(args) -> None:
    work = Path(args.work)
    ours = [m for m in read_manifest(work / "manifest.jsonl") if m["kind"] == "tex1"]
    by_key = {m["key"]: m for m in ours}
    by_legacy = {m["legacy_key"]: m for m in ours}
    # a '$' key matches the same texture with any palette
    wild = {k.replace("_$_", "_"): m for k, m in by_key.items() if "_$_" in k}
    dumps = Path(args.dumps)
    dumped = {dump_key(j) for j in read_manifest(dumps / "manifest.jsonl") if j.get("kind") == "tex1"}

    def no_pal(k: str) -> str:
        p = k.split("_")
        return "_".join(p[:3] + p[4:])

    cur = [k for k in dumped if k in by_key]
    leg = [k for k in dumped if k not in by_key and k in by_legacy]
    wc = [k for k in dumped if k not in by_key and k not in by_legacy and no_pal(k) in wild]
    total = len(cur) + len(leg) + len(wc)
    log(f"dumped keys: {len(dumped)} | reproduced: {total} ({100 * total / max(1, len(dumped)):.0f}%) "
        f"[{len(cur)} current-scheme, {len(leg)} legacy-scheme, {len(wc)} by palette wildcard]")

    same = diff = missing = 0
    for k in cur + leg + wc:
        m = by_key.get(k) or by_legacy.get(k) or wild[no_pal(k)]
        dump_png = dumps / f"{k}.png"
        if not dump_png.exists():
            missing += 1
            continue
        a = np.asarray(Image.open(dump_png).convert("RGBA"))
        b = np.asarray(Image.open(work / "native" / "textures" / f"{m['key']}.png").convert("RGBA"))
        if a.shape == b.shape and np.array_equal(a, b):
            same += 1
        else:
            diff += 1
            if diff <= 5:
                d = np.abs(a.astype(int) - b.astype(int)) if a.shape == b.shape else None
                log(f"  pixel mismatch {k} (fmt {m['fmt']}): "
                    + (f"max diff {d.max()}, {int((d.max(axis=2) > 0).sum())} px" if d is not None
                       else f"shape {a.shape} vs {b.shape}"))
    log(f"pixels: {same} identical, {diff} different, {missing} dump PNGs missing")

    if args.sprites:
        verify_sprites(work, Path(args.sprites))


def sprite_dump_key(j: dict) -> str:
    return f"obj1_{j['w']}x{j['h']}_{j['tilehash']}_{j['palhash'] or 'none'}_{j['bpp']}"


def verify_sprites(work: Path, dumps: Path) -> None:
    crops = {}
    for m in read_manifest(work / "manifest.jsonl"):
        if m["kind"] == "asset2d":
            for en in m["entries"]:
                if en["key"].startswith("obj1"):
                    crops[en["key"]] = (m["key"], en)
    dumped = {sprite_dump_key(j) for j in read_manifest(dumps / "manifest.jsonl") if j.get("kind") == "obj1"}
    hit = [k for k in dumped if k in crops]
    by_bpp = {}
    for k in dumped:
        b = k.rsplit("_", 1)[1]
        t = by_bpp.setdefault(b, [0, 0])
        t[0] += 1
        t[1] += k in crops
    log(f"sprites: {len(hit)}/{len(dumped)} dumped keys reproduced "
        + ", ".join(f"{b}bpp {h}/{n}" for b, (n, h) in sorted(by_bpp.items())))
    same = diff = 0
    for k in hit:
        dump_png = dumps / f"{k}.png"
        if not dump_png.exists():
            continue
        asset, en = crops[k]
        img = np.asarray(Image.open(work / "native" / "assets2d" / f"{asset}.png").convert("RGBA"))
        crop = img[en["y"]:en["y"] + en["h"], en["x"]:en["x"] + en["w"]]
        if en["hflip"]:
            crop = crop[:, ::-1]
        if en["vflip"]:
            crop = crop[::-1]
        d = np.asarray(Image.open(dump_png).convert("RGBA"))
        # only visible pixels count: a composite can't know the colour under alpha 0
        vis = d[..., 3] > 0
        if d.shape == crop.shape and np.array_equal(d[vis], crop[vis]) and np.array_equal(d[..., 3], crop[..., 3]):
            same += 1
        else:
            diff += 1
    log(f"sprite pixels: {same} identical, {diff} different")


# ---------------------------------------------------------------------------- upscale

KINDS = {"tex1": "textures", "asset2d": "assets2d", "font": "fonts"}
EDGE = (False, False, False, False)   # 2D art is clamped: pad by edge replication


def cmd_upscale(args) -> None:
    import upscale

    work = Path(args.work)
    items = read_manifest(work / "manifest.jsonl")
    recipe = work_recipe(work)
    # the pack has one scale (the emulator requires it); each category may use its own model
    scale = args.scale or recipe["scale"]
    names = {c: args.model or recipe["models"][c] for c in recipes.CATEGORIES}
    info_path = work / "upscaled" / "upscale.json"
    if info_path.exists() and not args.force:
        before = json.loads(info_path.read_text())
        if before.get("scale") != scale or before.get("models", names) != names:
            log(f"note: images already upscaled with {before.get('models')} at {before.get('scale')}x are "
                f"kept; pass --force to redo them with {names} at {scale}x")
    ups: dict[str, object] = {}

    def upscaler(category: str):
        name = names[category]
        if name not in ups:
            path = recipes.model_path(name)
            up = upscale.Upscaler(path, args.tile, fp16=not args.fp32)
            if scale > up.scale:
                raise SystemExit(f"scale {scale} needs a {scale}x model; {name} is {up.scale}x")
            log(f"{category}: {name} ({up.arch} {up.scale}x) on {up.device}"
                f"{' fp16' if up.fp16 is not None else ' fp32'}, output {scale}x")
            if up.device.type != "cuda":
                log("warning: no CUDA device, this will be very slow")
            ups[name] = up
        return ups[name]

    t0 = last = time.perf_counter()
    done = skipped = failed = 0
    for i, m in enumerate(items, 1):
        sub = KINDS[m["kind"]]
        src = work / "native" / sub / f"{m['key']}.png"
        dst = work / "upscaled" / sub / f"{m['key']}.png"
        redo = args.force or (args.redo_cutouts and dst.exists()
                              and int(np.asarray(Image.open(src))[..., 3].min()) < 255)
        if dst.exists() and not redo:
            skipped += 1
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                rgba = np.asarray(Image.open(src).convert("RGBA"))
                wrap = m.get("wrap") if m["kind"] == "tex1" else EDGE
                category = m.get("category", "textures")
                out = upscale.upscale_texture(rgba, upscaler(category), scale, args.pad, args.alpha, wrap=wrap)
                tmp = dst.with_name(dst.name + ".tmp")
                Image.fromarray(out, "RGBA").save(tmp, format="PNG")
                os.replace(tmp, dst)
                done += 1
            except Exception as e:  # keep going, report at the end
                failed += 1
                log(f"  FAILED {m['key']}: {type(e).__name__}: {e}")
        now = time.perf_counter()
        if now - last >= 15 or i == len(items):
            rate = done / (now - t0) if done else 0.0
            log(f"[{i}/{len(items)}] {done} upscaled, {skipped} already done, {failed} failed, "
                f"{rate:.1f}/s, eta {(len(items) - i) / rate if rate else 0:.0f}s")
            last = now
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(json.dumps({"scale": scale, "models": names, "alpha": args.alpha, "pad": args.pad},
                                    indent=1))


def work_recipe(work: Path) -> dict:
    """The recipe snapshot extract saved (the ROM isn't needed after extract)."""
    snap = work / "recipe.json"
    if snap.exists():
        return json.loads(snap.read_text(encoding="utf-8"))
    return recipes.load(work.name)


# ---------------------------------------------------------------------------- build

def cmd_build(args) -> Path:
    work = Path(args.work)
    code = work.name
    stage = "native" if args.native else "upscaled"
    if not (work / stage).is_dir():
        raise SystemExit(f"{work / stage} does not exist; run upscale first, or build --native")
    items = read_manifest(work / "manifest.jsonl")
    out = Path(args.out or PACKS) / code
    if out.exists():
        shutil.rmtree(out)
    scales, copied, absent = set(), 0, 0
    for sub in ("textures", "sprites", "bgtiles", "fonts"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for m in items:
        src = work / stage / KINDS[m["kind"]] / f"{m['key']}.png"
        if not src.exists():
            absent += 1
            continue
        if m["kind"] == "tex1":
            with Image.open(src) as im:
                scales.add(im.width // m["w"])
            shutil.copyfile(src, out / "textures" / src.name)
            copied += 1
            continue
        if m["kind"] == "font":
            # the font file itself plus its atlas, stored grey (see fonts.atlas)
            with Image.open(src) as im:
                scales.add(im.width // m["w"])
                im.convert("L").save(out / "fonts" / src.name)
            shutil.copyfile(work / "native" / "fonts" / f"{m['key']}.nftr", out / "fonts" / f"{m['key']}.nftr")
            copied += 1
            continue
        # 2D: cut each key's rectangle out of the whole upscaled cell or screen
        img = np.asarray(Image.open(src).convert("RGBA"))
        s = img.shape[1] // m["w"]
        scales.add(s)
        for en in m["entries"]:
            dst = out / ("sprites" if en["key"].startswith("obj1") else "bgtiles") / f"{en['key']}.png"
            if dst.exists():
                continue
            crop = img[en["y"] * s:(en["y"] + en["h"]) * s, en["x"] * s:(en["x"] + en["w"]) * s]
            if en["hflip"]:
                crop = crop[:, ::-1]
            if en["vflip"]:
                crop = crop[::-1]
            Image.fromarray(np.ascontiguousarray(crop), "RGBA").save(dst)
            copied += 1
    if len(scales) > 1:
        raise SystemExit(f"mixed scales {sorted(scales)} in {work / stage}; a pack must have one scale")
    info = {"game": code, "scale": scales.pop() if scales else 1, "images": copied, "source": stage}
    up_info = work / "upscaled" / "upscale.json"
    if stage == "upscaled" and up_info.exists():
        info["upscale"] = json.loads(up_info.read_text())
    (out / "pack.json").write_text(json.dumps(info, indent=1))
    log(f"pack {out}: {copied} images at {info['scale']}x" + (f", {absent} not upscaled yet" if absent else ""))
    return out


# ---------------------------------------------------------------------------- push

def adb(serial: str | None, *a: str, check: bool = True) -> str:
    cmd = ["adb"] + (["-s", serial] if serial else []) + list(a)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def find_device(serial: str | None) -> str:
    if serial:
        return serial
    lines = [l for l in adb(None, "devices", "-l").splitlines()[1:] if " device " in l]
    thor = [l.split()[0] for l in lines if "model:AYN_Thor" in l]
    if thor:
        return thor[0]
    if len(lines) == 1:
        return lines[0].split()[0]
    raise SystemExit("more than one device attached and none is an AYN Thor; pass --serial")


def cmd_push(args) -> None:
    pack = Path(args.pack)
    code = pack.name
    serial = find_device(args.serial)
    tmp = f"/data/local/tmp/hd_remaster_{code}"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log(f"pushing {pack} to {serial}")
    adb(serial, "shell", f"rm -rf {tmp}")
    adb(serial, "push", str(pack), tmp)
    adb(serial, "shell", f"chmod -R a+rX {tmp}")
    # an existing pack for this game is kept as a backup, never deleted
    script = (f"mkdir -p files/texturepacks && "
              f"if [ -d files/texturepacks/{code} ]; then "
              f"mv files/texturepacks/{code} files/texturepacks/{code}.bak-{stamp}; fi && "
              f"cp -r {tmp} files/texturepacks/{code} && ls files/texturepacks/{code}/* | wc -l")
    n = adb(serial, "shell", f"run-as {PACKAGE} sh -c '{script}'").strip()
    adb(serial, "shell", f"rm -rf {tmp}")
    log(f"installed files/texturepacks/{code} ({n} entries). The pack loads the next time the game starts.")


def cmd_all(args) -> None:
    work = cmd_extract(args)
    args.work = str(work)
    cmd_upscale(args)
    args.native = False
    args.out = None          # --out named the work root; the pack goes to the default packs root
    cmd_build(args)


# ---------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("extract", help="decode every texture in a ROM, named by pack key")
    p.add_argument("rom")
    p.add_argument("--out", help=f"work root (default {WORK})")
    p.set_defaults(fn=cmd_extract)

    p = sub.add_parser("verify", help="compare an extraction with textures dumped in-game")
    p.add_argument("work", help="work/<GAMECODE> from extract")
    p.add_argument("--dumps", required=True, help="a texture dump folder (manifest.jsonl + PNGs)")
    p.add_argument("--sprites", help="a sprite dump folder to check as well")
    p.set_defaults(fn=cmd_verify)

    def upscale_opts(p):
        p.add_argument("--model", help="model name from models.json or a model file, for every category "
                                       "(default: the game's recipe, else 4x-UltraSharp)")
        p.add_argument("--scale", type=int, choices=(2, 4), help="output scale (default: the recipe's, else 4)")
        p.add_argument("--tile", type=int, default=512, help="tile size in native pixels, 0 = never")
        p.add_argument("--pad", type=int, default=16, help="seam margin in native pixels")
        p.add_argument("--alpha", choices=("model", "resize"), default="model")
        p.add_argument("--fp32", action="store_true", help="never use fp16")
        p.add_argument("--force", action="store_true", help="redo images already upscaled")
        p.add_argument("--redo-cutouts", action="store_true",
                       help="redo only images with transparency (after an alpha-handling change)")

    p = sub.add_parser("upscale", help="AI-upscale an extraction")
    p.add_argument("work")
    upscale_opts(p)
    p.set_defaults(fn=cmd_upscale)

    p = sub.add_parser("build", help="assemble a pack folder from an extraction")
    p.add_argument("work")
    p.add_argument("--native", action="store_true", help="build a 1x pack from the native images")
    p.add_argument("--out", help=f"packs root (default {PACKS})")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("push", help="install a pack on the device (adb, debuggable build)")
    p.add_argument("pack", help="packs/<GAMECODE>")
    p.add_argument("--serial", help="adb serial (default: the attached AYN Thor)")
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("all", help="extract, upscale and build in one go")
    p.add_argument("rom")
    p.add_argument("--out", help="work root")
    upscale_opts(p)
    p.set_defaults(fn=cmd_all)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
