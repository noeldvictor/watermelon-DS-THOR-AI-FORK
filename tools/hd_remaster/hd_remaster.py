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

import nitro  # noqa: E402
import tex3d  # noqa: E402

WORK = HERE / "work"
PACKS = HERE / "packs"
DEFAULT_MODEL = HERE / "models" / "4x-UltraSharp.safetensors"
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
    files = nitro.files(rom)
    blobs = [nitro.Blob(name, data) for name, data in files.items()]
    log(f"{code} ({nitro.game_title(rom)}): {len(blobs)} files after unpacking, {time.time() - t0:.0f}s")
    blocks = tex3d.scan(blobs)
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
            path = tex_dir / f"{e.key}.png"
            if not path.exists():
                Image.fromarray(img, "RGBA").save(path, optimize=False, compress_level=1)
            mf.write(json.dumps({
                "kind": "tex1", "key": e.key, "legacy_key": e.legacy_key,
                "w": e.tex.w, "h": e.tex.h, "fmt": e.tex.fmt,
                "texture": e.tex.name, "palette": e.pal_name, "pairing": e.pairing,
                "source": e.source, "wrap": e.tex.wrap, "alpha": alpha_kind(img),
            }) + "\n")
        log(f"wrote {len(ents)} textures to {tex_dir} in {time.time() - t0:.0f}s")
        extract_2d(files, code, out, mf)
    log(f"done in {time.time() - t0:.0f}s")
    return out


def extract_2d(files: dict[str, bytes], code: str, out: Path, mf) -> None:
    """Sprites and BG tiles: whole cells and screens, each with the crops its keys cut out."""
    import twod

    lib = twod.Library(files)
    profile = twod.PROFILES.get(code)
    adir = out / "native" / "assets2d"
    adir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    n_assets = n_solo = n_obj = n_bg = 0

    def write_asset(name: str, img: np.ndarray, entries: list[dict], asset: dict) -> None:
        Image.fromarray(img, "RGBA").save(adir / f"{name}.png", compress_level=1)
        mf.write(json.dumps({
            "kind": "asset2d", "key": name, "w": int(img.shape[1]), "h": int(img.shape[0]),
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
    dumps = Path(args.dumps)
    dumped = {dump_key(j) for j in read_manifest(dumps / "manifest.jsonl") if j.get("kind") == "tex1"}

    cur = [k for k in dumped if k in by_key]
    leg = [k for k in dumped if k not in by_key and k in by_legacy]
    log(f"dumped keys: {len(dumped)} | reproduced: {len(cur) + len(leg)} "
        f"({100 * (len(cur) + len(leg)) / max(1, len(dumped)):.0f}%) "
        f"[{len(cur)} current-scheme, {len(leg)} legacy-scheme]")

    same = diff = missing = 0
    for k in cur + leg:
        m = by_key.get(k) or by_legacy[k]
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

KINDS = {"tex1": "textures", "asset2d": "assets2d"}
EDGE = (False, False, False, False)   # 2D art is clamped: pad by edge replication


def cmd_upscale(args) -> None:
    import upscale

    work = Path(args.work)
    items = read_manifest(work / "manifest.jsonl")
    up = upscale.Upscaler(Path(args.model), args.tile, fp16=not args.fp32)
    if args.scale > up.scale:
        raise SystemExit(f"--scale {args.scale} needs a {args.scale}x model; {Path(args.model).name} is {up.scale}x")
    log(f"{Path(args.model).name}: {up.arch} {up.scale}x on {up.device}"
        f"{' fp16' if up.fp16 is not None else ' fp32'}; output {args.scale}x")
    if up.device.type != "cuda":
        log("warning: no CUDA device, this will be very slow")

    t0 = last = time.perf_counter()
    done = skipped = failed = 0
    for i, m in enumerate(items, 1):
        sub = KINDS[m["kind"]]
        src = work / "native" / sub / f"{m['key']}.png"
        dst = work / "upscaled" / sub / f"{m['key']}.png"
        if dst.exists() and not args.force:
            skipped += 1
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                rgba = np.asarray(Image.open(src).convert("RGBA"))
                wrap = m.get("wrap") if m["kind"] == "tex1" else EDGE
                out = upscale.upscale_texture(rgba, up, args.scale, args.pad, args.alpha, wrap=wrap)
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
    (work / "upscaled" / "upscale.json").write_text(json.dumps({
        "scale": args.scale, "model": Path(args.model).name, "model_scale": up.scale,
        "alpha": args.alpha, "pad": args.pad}, indent=1))


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
    for sub in ("textures", "sprites", "bgtiles"):
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
        p.add_argument("--model", default=str(DEFAULT_MODEL), help="model file (default 4x-UltraSharp)")
        p.add_argument("--scale", type=int, default=4, choices=(2, 4), help="output scale (default 4)")
        p.add_argument("--tile", type=int, default=512, help="tile size in native pixels, 0 = never")
        p.add_argument("--pad", type=int, default=16, help="seam margin in native pixels")
        p.add_argument("--alpha", choices=("model", "resize"), default="model")
        p.add_argument("--fp32", action="store_true", help="never use fp16")
        p.add_argument("--force", action="store_true", help="redo images already upscaled")

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
