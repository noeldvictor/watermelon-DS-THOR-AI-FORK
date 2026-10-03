"""Standard HD packs (from 2026-10-03): one zip per game, every image ASTC 4x4.

`build_zip(pack_dir, zip_path)` turns the PNG staging folder `build` assembles
(packs/<CODE>/) into packs/<CODE>.zip:

- every .png becomes <same name>.astc: astcenc's file layout (16-byte header: magic
  13 AB A1 5C, block 4x4x1, 24-bit width/height/depth) then the 16-byte blocks in rows;
- text files (fonts' .nftr, models' .dl and originals.txt, camera.txt) go in as they are;
- pack.txt "scale <N>" lets the emulator size images from their key names without opening them
  (model textures, which keep their own scale, are read from their headers);
- deflate compression: ASTC blocks still shrink in the zip, PNGs wouldn't.

Encoding: ASTC blocks are independent, so images are laid out on shared sheets (each padded to
whole 4x4 blocks by edge replication), a sheet is encoded once with every thread, and each
image's blocks are cut back out. One call per small image costs ~20 ms of setup; the sheets make
a 100k-image pack minutes instead of an hour. Results are cached by PNG content
(work/<CODE>/astc_cache), so a rebuild only encodes what changed.

Profile LDR (linear, as the emulator's UNORM data), quality "thorough", alpha-weighted (colour
under transparent texels doesn't count). PSNR on Rosario's pack: median 46 dB, minimum 35 dB.
"""
from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

MAGIC = bytes([0x13, 0xAB, 0xA1, 0x5C])
SHEET_W = 2048
SHEET_MAX_H = 2048
TEXT_SUFFIXES = {".nftr", ".dl", ".txt", ".json"}


def astc_header(w: int, h: int) -> bytes:
    return MAGIC + bytes([4, 4, 1]) + w.to_bytes(3, "little") + h.to_bytes(3, "little") + (1).to_bytes(3, "little")


class _Encoder:
    def __init__(self, threads: int | None = None):
        import astc_encoder as ae
        self.ae = ae
        cfg = ae.ASTCConfig(ae.ASTCProfile.LDR, 4, 4, 1, ae.ASTCQualityPreset.THOROUGH,
                            ae.ASTCConfigFlags.USE_APLHA_WEIGHT)
        self.ctx = ae.ASTCContext(cfg, threads=threads or os.cpu_count() or 4)
        self.swizzle = ae.ASTCSwizzle.from_str("RGBA")

    def encode_sheet(self, sheet: np.ndarray) -> bytes:
        h, w = sheet.shape[:2]
        img = self.ae.ASTCImage(self.ae.ASTCType.U8, w, h, 1, np.ascontiguousarray(sheet).tobytes())
        return self.ctx.compress(img, self.swizzle)

    def decode(self, blocks: bytes, w: int, h: int) -> np.ndarray:
        out = self.ae.ASTCImage(self.ae.ASTCType.U8, w, h, 1)
        self.ctx.decompress(blocks, out, self.swizzle)
        return np.frombuffer(out.data, np.uint8).reshape(h, w, 4)


def _pad4(a: np.ndarray) -> np.ndarray:
    h, w = a.shape[:2]
    ph, pw = (-h) % 4, (-w) % 4
    if ph or pw:
        a = np.pad(a, ((0, ph), (0, pw), (0, 0)), mode="edge")
    return a


def encode_images(images: list[tuple[str, np.ndarray]], enc: _Encoder | None = None) -> dict[str, bytes]:
    """name -> .astc file bytes for RGBA uint8 arrays, encoded on shared sheets."""
    enc = enc or _Encoder()
    out: dict[str, bytes] = {}
    # tall images first packs the shelves tighter
    order = sorted(images, key=lambda t: (-_pad4(t[1]).shape[0], t[0]))
    pending: list[tuple[str, np.ndarray, int, int]] = []   # name, padded image, x, y
    x = y = shelf_h = 0

    def flush():
        nonlocal pending, x, y, shelf_h
        if not pending:
            return
        sheet_h = max(py + p.shape[0] for _, p, _, py in pending)
        sheet = np.zeros((sheet_h, SHEET_W, 4), np.uint8)
        for _, p, px, py in pending:
            sheet[py:py + p.shape[0], px:px + p.shape[1]] = p
        blocks = enc.encode_sheet(sheet)
        bw = SHEET_W // 4
        for name, p, px, py in pending:
            ph, pw = p.shape[0] // 4, p.shape[1] // 4
            rows = []
            for by in range(py // 4, py // 4 + ph):
                start = (by * bw + px // 4) * 16
                rows.append(blocks[start:start + pw * 16])
            out[name] = b"".join(rows)
        pending, x, y, shelf_h = [], 0, 0, 0

    sizes = {name: a.shape[:2] for name, a in images}
    for name, a in order:
        p = _pad4(np.asarray(a, np.uint8))
        h, w = p.shape[:2]
        if w > SHEET_W:
            # wider than a sheet: on its own
            flush()
            blocks = enc.encode_sheet(p)
            out[name] = blocks
            continue
        if x + w > SHEET_W:
            y += shelf_h
            x, shelf_h = 0, 0
        if y + h > SHEET_MAX_H:
            flush()
        pending.append((name, p, x, y))
        x += w
        shelf_h = max(shelf_h, h)
    flush()
    return {name: astc_header(sizes[name][1], sizes[name][0]) + blocks for name, blocks in out.items()}


def build_zip(pack_dir: Path, zip_path: Path, cache_dir: Path | None = None, log=print) -> dict:
    """The standard pack: pack_dir (PNG staging) -> zip_path. Returns counts."""
    pack_dir, zip_path = Path(pack_dir), Path(zip_path)
    files = sorted(p for p in pack_dir.rglob("*") if p.is_file())
    pngs = [p for p in files if p.suffix.lower() == ".png"]
    others = [p for p in files if p.suffix.lower() in TEXT_SUFFIXES and p.name != "pack.json"]
    scale = 1
    info_path = pack_dir / "pack.json"
    if info_path.exists():
        import json
        info = json.loads(info_path.read_text())
        scale = int(info.get("scale", 1))

    # cached encodes by PNG content
    cache_dir = Path(cache_dir) if cache_dir else None
    encoded: dict[str, bytes] = {}
    todo: list[tuple[str, np.ndarray]] = []
    digests: dict[str, str] = {}
    for p in pngs:
        rel = p.relative_to(pack_dir).as_posix()
        data = p.read_bytes()
        digest = hashlib.sha1(data).hexdigest()
        digests[rel] = digest
        cached = cache_dir / digest[:2] / (digest + ".astc") if cache_dir else None
        if cached and cached.exists():
            encoded[rel] = cached.read_bytes()
            continue
        todo.append((rel, np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))))
    log(f"astc: {len(pngs)} images, {len(pngs) - len(todo)} cached, encoding {len(todo)}")
    if todo:
        enc = _Encoder()
        done = 0
        chunk = 4000
        for i in range(0, len(todo), chunk):
            part = encode_images(todo[i:i + chunk], enc)
            for rel, data in part.items():
                encoded[rel] = data
                if cache_dir:
                    d = digests[rel]
                    target = cache_dir / d[:2] / (d + ".astc")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            done += len(part)
            log(f"astc: {done}/{len(todo)} encoded")

    tmp = zip_path.with_suffix(".zip.part")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("pack.txt", f"format astc4x4\nscale {scale}\n")
        for rel in sorted(encoded):
            z.writestr(rel[:-4] + ".astc", encoded[rel])
        for p in others:
            z.write(p, p.relative_to(pack_dir).as_posix())
    os.replace(tmp, zip_path)
    size = zip_path.stat().st_size
    log(f"pack {zip_path}: {len(encoded)} ASTC images + {len(others)} files, {size / 1e6:.0f} MB")
    return {"images": len(encoded), "files": len(others), "bytes": size, "scale": scale}


def verify_zip(pack_dir: Path, zip_path: Path, samples: int = 200, log=print) -> float:
    """Decode a sample of the zip's images and compare with the PNGs: the lowest PSNR (dB)."""
    import random
    enc = _Encoder(threads=1)
    worst = 99.0
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.endswith(".astc")]
        random.Random(0).shuffle(names)
        for n in names[:samples]:
            data = z.read(n)
            w = int.from_bytes(data[7:10], "little")
            h = int.from_bytes(data[10:13], "little")
            got = enc.decode(data[16:], w, h)
            want = np.asarray(Image.open(pack_dir / (n[:-5] + ".png")).convert("RGBA"))
            vis = want[..., 3] > 0
            if not vis.any():
                continue
            mse = ((want[..., :3].astype(float) - got[..., :3]) ** 2)[vis].mean()
            worst = min(worst, 99.0 if mse == 0 else 10 * np.log10(255 ** 2 / mse))
    log(f"verify {zip_path.name}: {min(samples, len(names))} images decoded, lowest PSNR {worst:.1f} dB")
    return worst
