"""2D sprites (obj1_) and BG tiles (bg1_): pack keys and assembled images from a DS ROM.

Parses NCGR/NCLR/NCER/NSCR from the files nitro.files() finds, assembles every cell and screen
at native resolution and emits, per assembled image, the list of (pack key, crop rectangle)
entries a whole-image upscale can be cut into. Upscaling the whole cell or screen and cutting
it afterwards keeps neighbouring sprites and tiles seamless.

A palette the files don't pin down (8bpp content, whose key hashes the whole 256-colour
palette memory, or a palette row the NCLR lacks) is guessed, and the key is built from the
guess. A wrong guess then simply doesn't match at runtime. 256-colour sprites also get a
colour key (obj1_WxH_<hash>_rgb_8: the colours the sprite shows, see color_hash), which the
runtime tries when the byte key misses, so it matches wherever the game puts the palette.

Key formulas mirror melonDS-android-lib/src/GPU2D_HDPack.cpp:
  obj1_<W>x<H>_<tilehash>_<palhash>_<4|8|bmp>
    tilehash = chained XXH64 (seed = previous, first 0) over each 8x8 tile's
               bytes (32 4bpp / 64 8bpp) at tileOffset + tx*tileBytes +
               ty*tileStride, row-major.
               1D mapping: tileOffset = tilenum << (5+boundary),
                           tileStride = (W/8)*32 (x2 for 8bpp)
               2D mapping: tileOffset = tilenum << 5, tileStride = 1024
    palhash  = 4bpp: XXH64(16 colours at (attr2>>12)*16) of OBJ palette RAM
               8bpp: XXH64(all 256 colours) of OBJ std palette, or of ext
                     palette slot (attr2>>12) when DISPCNT bit31
  bg1_8x8_<tilehash>_<palhash>_<4|8>
    tilehash = XXH64(tile bytes at charbase + tilenum*32|64) (seed 0)
    palhash  = 4bpp: XXH64(16 colours at row (entry>>12))
               8bpp: XXH64(256 colours) std BG palette, or ext slot
PNG pixels: 5-bit channel c -> (2c*510+63)//126, index 0 transparent
(alpha 0, RGB of palette entry kept).

File layouts parsed (offsets from file start, standard 0x10 NITRO header):
  NCGR RGCN/RAHC@0x10: +8 u16 tilesH, +A u16 tilesW (0xFFFF = 1D), +C depth
       (3=4bpp 4=8bpp), +10 mapping (0 = 2D, 0x10|n<<20 = 1D 32K<<n),
       +14 linear flag, +18 data size, +1C data offset rel +8 (-> 0x30)
  NCLR RLCN/TTLP@0x10: +8 depth, +C ext flag, +10 size, +14 offset rel +8
       (-> 0x28); optional PMCP (u16 count, u16 slot ids) remaps rows
  NCER RECN/KBEC@0x10: +8 u16 cells, +A u16 bank attr (1 = 16-byte cell
       entries w/ bbox), +C cell data off rel +8, +10 mapping (0..3 = 1D
       32/64/128/256K, 4 = 2D), +14 VRAM-transfer off rel +8 (0 = none);
       cell = u16 nOAM, u16 attr, u32 OAM offset; OAM = 3 x u16 attrs
  NSCR RCSN/NRCS@0x10: +8 u16 width px, +A u16 height px, +C colour mode,
       +E format (0 text, 1 affine, 2 ext), +10 size, data @+14
  Lufia mcd.dat: mcd_compact.bin 'NLCM' (+4 size-table off, +8 offset-table
       off, +C count) + mcd_path.bin (u32 count, u32 name offsets); each
       entry '$FAB' + type byte (0x00 stored, 0x11 LZ11, 0x28 Huffman8, 0x30
       RLE) + 24-bit size, or raw.

usage:
  python twod.py <rom.nds> [--manifest DIR/manifest.jsonl --dumps DIR]
                  [--out OUTDIR [--crops]] [--no-profile]
  --out writes OUTDIR/assets/*.png (assembled cell / screen, native res) and
  OUTDIR/keys.jsonl (one line per asset: source files, pairing, and entries
  {key, x, y, w, h, hflip, vflip, clean, covered, ...}; crop x,y,w,h from the
  (upscaled) asset, undo hflip/vflip, save as <key>.png).
"""
import argparse
import json
import os
import struct
import sys
from collections import Counter, defaultdict

import numpy as np
import xxhash

import nitro
import recipes


def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def s16(b, o): return struct.unpack_from("<h", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def xxh(data, seed=0): return xxhash.xxh64_intdigest(bytes(data), seed)
def h16(v): return "%016x" % v


def collect_files(rom):
    """name -> bytes for every file in the ROM (see nitro.files)."""
    return nitro.files(rom)


# ============================================================ formats
class NCGR:
    def __init__(self, b, name=""):
        assert b[:4] == b"RGCN"
        self.name = name
        o = u16(b, 0x0C)
        assert b[o:o + 4] == b"RAHC", "no RAHC"
        self.th, self.tw = u16(b, o + 8), u16(b, o + 0xA)
        depth = u32(b, o + 0xC)
        self.bpp = 8 if depth == 4 else 4
        self.mapping = u32(b, o + 0x10)          # 0 = 2D char, 0x10|n<<20 = 1D
        self.linear = u32(b, o + 0x14) & 1       # 1 = bitmap-scanned data
        size = u32(b, o + 0x18); doff = u32(b, o + 0x1C)
        self.data = b[o + 8 + doff:o + 8 + doff + size]
        self.tile_bytes = 32 if self.bpp == 4 else 64
        self.ntiles = len(self.data) // self.tile_bytes


class NCLR:
    def __init__(self, b, name=""):
        assert b[:4] == b"RLCN"
        self.name = name
        o = u16(b, 0x0C)
        assert b[o:o + 4] == b"TTLP"
        depth = u32(b, o + 8)
        self.bpp = 8 if depth == 4 else 4
        self.extpal = u32(b, o + 0xC)
        size = u32(b, o + 0x10); doff = u32(b, o + 0x14)
        if size == 0:
            # some tools leave the size 0 and let the colours run to the end of the block
            # (Nostalgia's title screens): read as empty, the screen came out all black
            size = u32(b, o + 4)
        end = min(o + 8 + doff + size, o + u32(b, o + 4), len(b))
        raw = b[o + 8 + doff:end]
        self.raw = raw[:len(raw) & ~1]
        # PMCP: sparse palette slot list
        self.slots = None
        p = o + u32(b, o + 4)
        if p + 12 <= len(b) and b[p:p + 4] == b"PMCP":
            n = u16(b, p + 8); io = u32(b, p + 0xC)
            self.slots = [u16(b, p + 8 + io + 2 * i) for i in range(n)]
        self.colors = np.frombuffer(self.raw, dtype="<u2").copy()

    def row(self, i, n=16):
        """bytes of n colours for palette row i (honours PMCP slot remap)."""
        if self.slots is not None and self.bpp == 4 and n == 16:
            if i not in self.slots: return None
            i = self.slots.index(i)
        a = i * n * 2
        if a + n * 2 > len(self.raw): return None
        return self.raw[a:a + n * 2]


SIZES = {(0, 0): (8, 8), (0, 1): (16, 16), (0, 2): (32, 32), (0, 3): (64, 64),
         (1, 0): (16, 8), (1, 1): (32, 8), (1, 2): (32, 16), (1, 3): (64, 32),
         (2, 0): (8, 16), (2, 1): (8, 32), (2, 2): (16, 32), (2, 3): (32, 64)}


class OAM:
    __slots__ = ("x", "y", "w", "h", "tile", "pal", "bpp8", "hflip", "vflip",
                 "rotscale", "double", "mode", "prio", "a0", "a1", "a2")

    def __init__(self, a0, a1, a2):
        self.a0, self.a1, self.a2 = a0, a1, a2
        self.y = (a0 & 0xFF) - ((a0 & 0x80) << 1)
        self.x = (a1 & 0x1FF) - ((a1 & 0x100) << 1)
        self.rotscale = bool(a0 & 0x100)
        self.double = bool(a0 & 0x200) and self.rotscale
        self.mode = (a0 >> 10) & 3
        self.bpp8 = bool(a0 & 0x2000)
        shape = a0 >> 14
        self.w, self.h = SIZES.get((shape, a1 >> 14), (8, 8))
        self.hflip = bool(a1 & 0x1000) and not self.rotscale
        self.vflip = bool(a1 & 0x2000) and not self.rotscale
        self.tile = a2 & 0x3FF
        self.prio = (a2 >> 10) & 3
        self.pal = a2 >> 12


class NCER:
    def __init__(self, b, name=""):
        assert b[:4] == b"RECN"
        self.name = name
        o = u16(b, 0x0C)
        assert b[o:o + 4] == b"KBEC"
        base = o + 8
        n = u16(b, base); self.bank_attr = u16(b, base + 2)
        cdo = u32(b, base + 4)
        self.mapping = u32(b, base + 8)          # 0..3 1D 32/64/128/256K, 4 2D
        vto = u32(b, base + 0xC)
        esz = 16 if self.bank_attr == 1 else 8
        cells_at = base + cdo
        oam_at = cells_at + n * esz
        self.cells = []
        for i in range(n):
            e = cells_at + i * esz
            cnt, attr, oo = u16(b, e), u16(b, e + 2), u32(b, e + 4)
            oams = []
            for k in range(cnt):
                p = oam_at + oo + k * 6
                if p + 6 > len(b): break
                oams.append(OAM(u16(b, p), u16(b, p + 2), u16(b, p + 4)))
            self.cells.append(oams)
        # VRAM transfer: per-cell (srcOffset, size) into the NCGR char data
        self.vram = None
        if vto:
            v = base + vto
            try:
                self.vram_max = u32(b, v)
                eo = u32(b, v + 4)
                self.vram = [(u32(b, v + eo + 8 * i), u32(b, v + eo + 8 * i + 4)) for i in range(n)]
            except struct.error:
                self.vram = None

    @property
    def is2d(self):
        return self.mapping == 4

    @property
    def boundary_shift(self):
        return self.mapping if self.mapping < 4 else 0


class NSCR:
    def __init__(self, b, name=""):
        assert b[:4] == b"RCSN"
        self.name = name
        o = u16(b, 0x0C)
        assert b[o:o + 4] == b"NRCS"
        self.wpx, self.hpx = u16(b, o + 8), u16(b, o + 0xA)
        self.colmode = u16(b, o + 0xC)           # 0 = 16x16, 1 = 256x1
        self.fmt = u16(b, o + 0xE)               # 0 text, 1 affine, 2 ext
        size = u32(b, o + 0x10)
        self.data = b[o + 0x14:o + 0x14 + size]
        if self.fmt == 1:
            self.entries = np.frombuffer(self.data, dtype=np.uint8).astype(np.uint16)
        else:
            self.entries = np.frombuffer(self.data[:len(self.data) & ~1], dtype="<u2")


# ============================================================ decoding / keys
def _expand5(c5):
    v6 = c5 << 1
    return (v6 * 510 + 63) // 126


LUT5 = np.array([_expand5(i) for i in range(32)], dtype=np.uint8)


def pal_to_rgb(raw):
    """palette bytes -> (n,3) uint8 exactly like Pal555ToRGBA8 (bit15 ignored)."""
    c = np.frombuffer(raw, dtype="<u2")
    return np.stack([LUT5[c & 31], LUT5[(c >> 5) & 31], LUT5[(c >> 10) & 31]], axis=1)


def decode_tile(tb, bpp8):
    a = np.frombuffer(tb, dtype=np.uint8)
    if bpp8:
        return a.reshape(8, 8)
    out = np.empty((8, 8), dtype=np.uint8)
    a = a.reshape(8, 4)
    out[:, 0::2] = a & 0xF
    out[:, 1::2] = a >> 4
    return out


def obj_layout(tile, w, bpp8, shift, is2d):
    """runtime (tileOffset, tileStride, tileBytes) for a non-bitmap OBJ."""
    tb = 64 if bpp8 else 32
    if is2d:
        return tile << 5, 32 * 32, tb
    stride = (w >> 3) * 32 * (2 if bpp8 else 1)
    return tile << (5 + shift), stride, tb


def obj_tiles(vram, off, stride, tb, w, h):
    """list of tile byte strings in hash order, or None if out of range."""
    out = []
    for ty in range(h // 8):
        for tx in range(w // 8):
            a = off + tx * tb + ty * stride
            if a < 0 or a + tb > len(vram): return None
            out.append(vram[a:a + tb])
    return out


def chain_hash(tiles):
    h = 0
    for t in tiles: h = xxh(t, h)
    return h


def tiles_to_indices(tiles, w, h, bpp8):
    img = np.zeros((h, w), dtype=np.uint8)
    k = 0
    for ty in range(h // 8):
        for tx in range(w // 8):
            img[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8] = decode_tile(tiles[k], bpp8); k += 1
    return img


def indices_to_rgba(idx, palraw):
    """idx (h,w) palette-local indices -> RGBA; index 0 transparent, RGB kept."""
    rgb = pal_to_rgb(palraw)
    out = np.zeros(idx.shape + (4,), dtype=np.uint8)
    safe = np.minimum(idx, len(rgb) - 1)
    out[..., :3] = rgb[safe]
    out[..., 3] = np.where(idx != 0, 255, 0)
    return out


def obj_palette(nclr, oam, extpal=False):
    """bytes the runtime palette hash covers for this OBJ, or None."""
    if nclr is None: return None
    if not oam.bpp8:
        return nclr.row(oam.pal, 16)
    slot = oam.pal if extpal else 0
    a = slot * 512
    if len(nclr.raw) < a + 512: return None
    return nclr.raw[a:a + 512]


# ============================================================ game rules
# Load rules that can't be read from the file headers come from the game's recipe
# (games/<GAMECODE>/recipe.json, "twod"). Each "obj" rule applies to NCERs whose path contains
# "dir" at the given "bpp":
#   index_shift  every non-zero colour index is uploaded shifted by this much
#   palram       the 256-colour palette memory the key hashes, as [file, first colour, count]
#                pieces ("@self" = the cell's own NCLR)
#   also_shifts  other shifts the game sometimes uses; each adds a '$'-palette key per piece
#   wildcard     also key every piece under the '$' palette wildcard (when the palette memory
#                around the art depends on what else is on screen)
#   color_keys   also key every piece by the colours it shows (obj1_WxH_<hash>_rgb_<bpp>), which
#                matches however the game lays the art out in palette memory
# and at the top level of "twod": color_keys_8bpp (default true) gives every 256-colour sprite
# piece a colour key


# ============================================================ pairing
def split_name(n):
    d, _, f = n.rpartition("/")
    # a compressed file's name keeps its own type: 'ci_01_d.NSCR.lz~lz' is the screen
    # 'ci_01_d', which must pair with ci_d.NCLR, not tie with ci_u.NCLR
    f = f.split("~", 1)[0]
    if f.lower().endswith(".lz"):
        f = f[:-3]
    return d, f.rsplit(".", 1)[0]


def norm_dir(d):
    """2d/localize/us/etc -> 2d/etc so localized files pair with base ones."""
    parts = d.split("/")
    if "localize" in parts:
        i = parts.index("localize")
        parts = parts[:i] + parts[i + 2:]
    return "/".join(parts)


def name_score(a, b):
    a, b = a.lower(), b.lower()
    ta, tb = set(a.split("_")), set(b.split("_"))
    pre = 0
    for x, y in zip(a, b):
        if x != y: break
        pre += 1
    sub = 1 if (a in b or b in a) else 0
    return (len(ta & tb) + sub, pre)


class Library:
    """All parsed 2D resources of a ROM, indexed for pairing."""

    KINDS = {b"RGCN": "g", b"RLCN": "p", b"RECN": "e", b"RCSN": "s"}

    def __init__(self, leaves):
        self.leaves = leaves
        self.stems = defaultdict(dict)            # (dir, stem) -> kind -> name
        self.by_dir = defaultdict(lambda: defaultdict(list))   # normdir -> kind -> [(stem, name)]
        self.cache = {}
        for n, b in leaves.items():
            k = self.KINDS.get(b[:4])
            if not k: continue
            d, s = split_name(n)
            self.stems[(d, s)][k] = n
            self.by_dir[norm_dir(d)][k].append((s, n))

    def get(self, name):
        if name not in self.cache:
            b = self.leaves[name]
            cls = {b"RGCN": NCGR, b"RLCN": NCLR, b"RECN": NCER, b"RCSN": NSCR}[b[:4]]
            try:
                self.cache[name] = cls(b, name)
            except (AssertionError, struct.error, ValueError, IndexError):
                self.cache[name] = None
        return self.cache[name]

    def partners(self, name, kind, limit=16):
        """ranked candidate files of `kind` for resource `name`:
        same stem first, then same (normalised) dir by name similarity."""
        d, s = split_name(name)
        same = self.stems.get((d, s), {}).get(kind)
        if same: return [(same, "stem")]
        cands = self.by_dir.get(norm_dir(d), {}).get(kind, [])
        if not cands: return []
        if len(cands) == 1: return [(cands[0][1], "only-in-dir")]
        scored = sorted(((name_score(s, cs), cn) for cs, cn in cands), reverse=True)
        best = scored[0][0]
        top, seen = [], set()
        for sc, cn in scored:
            if sc != best or len(top) >= limit: break
            body = self.leaves[cn]
            if body in seen: continue            # identical copies (e.g. localized dup)
            seen.add(body); top.append(cn)
        return [(cn, "dir-best" if len(top) == 1 else "dir-tie") for cn in top]


# ============================================================ assembly
def palram_for(lib, spec, self_nclr):
    out = b""
    for fn, start, count in spec:
        p = self_nclr if fn == "@self" else lib.get(fn)
        if p is None: return None
        out += p.raw[start * 2:(start + count) * 2].ljust(count * 2, b"\0")
    return out[:512].ljust(512, b"\0")


def guess_row(p, row):
    """best-guess 16-colour row when the map/OAM names a row the NCLR lacks
    (typically a 1-row NCLR the game uploads into a higher palette row)."""
    if p is None or len(p.raw) < 32: return None
    n = len(p.raw) // 32
    return p.raw[(row % n) * 32:(row % n) * 32 + 32]


def shift_indices(data, k):
    if not k: return data
    a = np.frombuffer(data, dtype=np.uint8).astype(np.int32)
    return np.where(a != 0, (a + k) & 255, 0).astype(np.uint8).tobytes()


COLOR_SEED = 0x484443504958454C   # "HDCPIXEL", as HDTexPack::SpriteColorHash


def color_hash(rgba):
    """HDTexPack::SpriteColorHash: XXH64 over the sprite's RGBA8 words (R in the low byte),
    unflipped, row-major, transparent pixels as 0."""
    a = np.ascontiguousarray(rgba, dtype=np.uint8).copy()
    a[a[..., 3] == 0] = 0
    return xxh(a.tobytes(), COLOR_SEED)


def color_key(w, h, rgba, bpp8):
    return "obj1_%dx%d_%s_rgb_%s" % (w, h, h16(color_hash(rgba)), "8" if bpp8 else "4")


def obj_key(w, h, th, ph, bpp8):
    return "obj1_%dx%d_%s_%s_%s" % (w, h, h16(th), "$" if ph is None else h16(ph), "8" if bpp8 else "4")


def bg_key(th, ph, bpp8):
    return "bg1_8x8_%s_%s_%s" % (h16(th), "$" if ph is None else h16(ph), "8" if bpp8 else "4")


def flip_img(img, hflip, vflip):
    if hflip: img = img[:, ::-1]
    if vflip: img = img[::-1]
    return img


def build_cells(lib, profile, want_rgba=False):
    """yield one asset dict per (NCER cell, palette candidate)."""
    rules = (profile or {}).get("obj", [])
    for (d, s), kinds in sorted(lib.stems.items()):
        if "e" not in kinds: continue
        ename = kinds["e"]
        e = lib.get(ename)
        if e is None or not e.cells: continue
        gcands = lib.partners(ename, "g", limit=1)
        if not gcands: continue
        g = lib.get(gcands[0][0])
        if g is None: continue
        rule = next((r for r in rules if r["dir"] in ename and r.get("bpp", g.bpp) == g.bpp), None)
        data = shift_indices(g.data, rule.get("index_shift", 0)) if rule else g.data
        # other ways the game uploads the same art: other index shifts, and (wildcard) any palette
        alts = [(shift_indices(g.data, s), True) for s in rule.get("also_shifts", ())] if rule else []
        if rule and rule.get("wildcard"):
            alts.insert(0, (data, True))
        # 256-colour sprites are keyed with all of palette memory, so where the game puts their
        # colours (which depends on what else is loaded) changes the key; the colour key doesn't.
        # 16-colour sprites hash only their own row, wherever it sits, and need none.
        if (rule and rule.get("color_keys")) or (g.bpp == 8 and (profile or {}).get("color_keys_8bpp", True)):
            alts.append(("rgb", None))
        pcands = lib.partners(ename, "p") or [(None, "none")]
        for pname, pmode in pcands:
            p = lib.get(pname) if pname else None
            palram = palram_for(lib, rule["palram"], p) if (rule and "palram" in rule and p is not None) else None
            for ci, cell in enumerate(e.cells):
                asset = assemble_cell(e, ci, cell, data, p, palram, want_rgba, alts)
                if asset is None: continue
                asset.update(name=f"{ename}#cell{ci}", ncer=ename, ncgr=g.name, nclr=pname,
                             pairing=pmode, gpair=gcands[0][1])
                yield asset


def assemble_cell(e, ci, cell, data, p, palram, want_rgba, alts=()):
    """alts: (data, wildcard) pairs for other ways the game uploads this art; each OBJ gets an
    extra entry per alt, the same crop keyed by the alt's tile hash ('$' palette if wildcard)."""
    objs = []
    for oi, o in enumerate(cell):
        off, stride, tb = obj_layout(o.tile, o.w, o.bpp8, e.boundary_shift, e.is2d)
        vram = data
        if e.vram is not None:
            so, sz = e.vram[ci]
            vram = data[so:so + sz]
        tiles = obj_tiles(vram, off, stride, tb, o.w, o.h)
        if tiles is None: continue
        th = chain_hash(tiles)
        idx = tiles_to_indices(tiles, o.w, o.h, o.bpp8)
        guessed = False
        if o.bpp8:
            if palram is not None:
                praw = palram
            else:
                # the key hashes all 256 entries of palette memory, which the NCLR alone
                # doesn't determine: guess that it holds this NCLR from entry 0
                praw = p.raw[:512].ljust(512, b"\0") if p is not None else None
                guessed = True
        else:
            praw = p.row(o.pal, 16) if p is not None else None
            if praw is None:
                praw = guess_row(p, o.pal)          # a 1-row NCLR uploaded into a higher row
                guessed = True
        if praw is None:
            continue                                # no palette at all: nothing to key by
        ph = xxh(praw)
        rgba = indices_to_rgba(idx, praw)
        alt_keys = []
        for adata, wild in alts:
            if isinstance(adata, str):              # "rgb": keyed by the colours shown
                alt_keys.append(color_key(o.w, o.h, rgba, o.bpp8))
                continue
            avram = adata
            if e.vram is not None:
                so, sz = e.vram[ci]
                avram = adata[so:so + sz]
            atiles = obj_tiles(avram, off, stride, tb, o.w, o.h)
            if atiles is not None:
                alt_keys.append(obj_key(o.w, o.h, chain_hash(atiles), None if wild else ph, o.bpp8))
        objs.append(dict(oam=oi, o=o, th=th, ph=ph, guessed=guessed, rgba=rgba,
                         key=obj_key(o.w, o.h, th, ph, o.bpp8), alt_keys=alt_keys))
    if not objs: return None
    x0 = min(ob["o"].x for ob in objs); y0 = min(ob["o"].y for ob in objs)
    x1 = max(ob["o"].x + ob["o"].w for ob in objs); y1 = max(ob["o"].y + ob["o"].h for ob in objs)
    W, H = x1 - x0, y1 - y0
    canvas = np.zeros((H, W, 4), dtype=np.uint8)
    owner = np.full((H, W), -1, dtype=np.int32)
    # draw back to front: higher priority value / higher OAM index first
    order = sorted(range(len(objs)), key=lambda i: (objs[i]["o"].prio, objs[i]["oam"]), reverse=True)
    for i in order:
        ob = objs[i]; o = ob["o"]
        img = flip_img(ob["rgba"], o.hflip, o.vflip)
        ys, xs = o.y - y0, o.x - x0
        m = img[..., 3] > 0
        reg = canvas[ys:ys + o.h, xs:xs + o.w]
        reg[m] = img[m]
        own = owner[ys:ys + o.h, xs:xs + o.w]
        own[m] = i
    entries = []
    for i, ob in enumerate(objs):
        o = ob["o"]; ys, xs = o.y - y0, o.x - x0
        own = owner[ys:ys + o.h, xs:xs + o.w]
        mine = flip_img(ob["rgba"], o.hflip, o.vflip)[..., 3] > 0
        foreign = (own != -1) & (own != i)
        base = dict(key=ob["key"], x=int(xs), y=int(ys), w=o.w, h=o.h,
                    hflip=o.hflip, vflip=o.vflip, oam=ob["oam"],
                    clean=not bool(foreign.any()),
                    covered=int((foreign & mine).sum()),
                    opaque=bool(ob["rgba"][..., 3].any()),
                    rotscale=o.rotscale, objmode=o.mode, pal_guess=ob["guessed"])
        entries.append(base)
    # alternate keys after all primary ones, so the primaries keep their order
    for ob, base in zip(objs, list(entries)):
        for k in ob["alt_keys"]:
            if k != ob["key"]:
                entries.append(dict(base, key=k))
    asset = dict(kind="cell", image=canvas, origin=(int(x0), int(y0)), entries=entries)
    if want_rgba:
        asset["obj_rgba"] = {ob["key"]: ob["rgba"] for ob in objs}
        asset["obj_rgba"].update({k: ob["rgba"] for ob in objs for k in ob["alt_keys"]})
    return asset


def build_screens(lib, profile, want_rgba=False):
    for (d, s), kinds in sorted(lib.stems.items()):
        if "s" not in kinds: continue
        sname = kinds["s"]
        sc = lib.get(sname)
        if sc is None or sc.fmt != 0:          # runtime keys text BGs only
            continue
        gc = lib.partners(sname, "g", limit=1)
        if not gc: continue
        g = lib.get(gc[0][0])
        if g is None: continue
        for pname, pmode in (lib.partners(sname, "p") or [(None, "none")]):
            p = lib.get(pname) if pname else None
            asset = assemble_screen(sc, g, p, want_rgba)
            if asset is None: continue
            asset.update(name=sname, nscr=sname, ncgr=g.name, nclr=pname, pairing=pmode, gpair=gc[0][1])
            yield asset


def screen_entries(sc, blocked=None):
    """(tx, ty, entry) in screen pixel order. blocked=True reads the data as
    32x32-tile hardware screen blocks (VRAM order), False as a linear map;
    None picks whichever gives the smoother image (decided by caller)."""
    tw, th = sc.wpx // 8, sc.hpx // 8
    ents = sc.entries
    out = []
    bcols = (tw + 31) // 32
    for ty in range(th):
        for tx in range(tw):
            if blocked:
                # screen block n = 32x32 entries (2 KiB), blocks left->right, top->bottom
                i = ((ty // 32) * bcols + tx // 32) * 1024 + (ty % 32) * 32 + (tx % 32)
            else:
                i = ty * tw + tx
            if i < len(ents): out.append((tx, ty, int(ents[i])))
    return out


def render_screen(sc, g, p, blocked):
    bpp8 = g.bpp == 8
    tb = 64 if bpp8 else 32
    canvas = np.zeros((sc.hpx, sc.wpx, 4), dtype=np.uint8)
    tiles = []
    for tx, ty, ent in screen_entries(sc, blocked):
        tn = ent & 0x3FF
        a = tn * tb
        if a + tb > len(g.data): continue
        tbytes = g.data[a:a + tb]
        guessed = False
        if bpp8:
            # the key hashes the whole 256-colour BG palette (or an extended slot): guess
            # that it holds this NCLR from entry 0
            praw = p.raw[:512].ljust(512, b"\0") if p is not None else None
            guessed = True
        else:
            praw = p.row(ent >> 12, 16) if p is not None else None
            if praw is None:
                praw = guess_row(p, ent >> 12)       # a 1-row NCLR uploaded into a higher row
                guessed = True
        if praw is None:
            continue                                 # no palette at all: nothing to key by
        rgba = indices_to_rgba(decode_tile(tbytes, bpp8), praw)
        canvas[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8] = flip_img(rgba, bool(ent & 0x400), bool(ent & 0x800))
        tiles.append((tx, ty, ent, xxh(tbytes), xxh(praw), guessed, rgba))
    return canvas, tiles


def seam_cost(img):
    a = img[..., :3].astype(np.int32)
    v = np.abs(a[:, 8::8] - a[:, 7:-1:8]).sum() if a.shape[1] > 8 else 0
    h = np.abs(a[8::8] - a[7:-1:8]).sum() if a.shape[0] > 8 else 0
    return int(v + h)


def assemble_screen(sc, g, p, want_rgba):
    bpp8 = g.bpp == 8
    layout = "linear"
    canvas, tiles = render_screen(sc, g, p, False)
    # >256px hardware-sized screens are stored as 32x32 screen blocks (verified
    # visually on Lufia's 512x512 worldmap/option BGs); keep the seam test as
    # a guard in case a tool wrote one linearly
    if (sc.wpx > 256 or sc.hpx > 256) and sc.wpx % 256 == 0 and sc.hpx % 256 == 0:
        c2, t2 = render_screen(sc, g, p, True)
        if seam_cost(c2) < seam_cost(canvas):
            canvas, tiles, layout = c2, t2, "blocked"
    entries = {}
    rgbas = {}
    for tx, ty, ent, th, ph, guessed, rgba in tiles:
        if not rgba[..., 3].any():
            continue                                  # runtime never dumps fully transparent tiles
        key = bg_key(th, ph, bpp8)
        hf, vf = bool(ent & 0x400), bool(ent & 0x800)
        e = entries.get(key)
        if e is None:
            entries[key] = dict(key=key, x=tx * 8, y=ty * 8, w=8, h=8, hflip=hf, vflip=vf,
                                count=1, pal_guess=guessed)
            if want_rgba: rgbas[key] = rgba
        else:
            e["count"] += 1
            if (e["hflip"] or e["vflip"]) and not (hf or vf):   # prefer an unflipped crop
                e.update(x=tx * 8, y=ty * 8, hflip=False, vflip=False)
    if not entries: return None
    asset = dict(kind="screen", image=canvas, origin=(0, 0), entries=list(entries.values()), layout=layout)
    if want_rgba: asset["obj_rgba"] = rgbas
    return asset


# ============================================================ validation
def load_manifest(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    keys = {}
    for j in rows:
        if j.get("kind") != "obj1": continue
        k = "obj1_%dx%d_%s_%s_%s" % (j["w"], j["h"], j["tilehash"], j["palhash"] or "none", j["bpp"])
        keys[k] = j
    return rows, keys


def validate(assets_keys, rgba_of, manifest, dumps_dir):
    rows, dumped = load_manifest(manifest)
    tile_of = lambda k: k.split("_")[2]
    gen_tiles = {tile_of(k) for k in assets_keys}
    gen_exact = set(assets_keys)
    gen_wild = {("_".join(k.split("_")[:3]), k.split("_")[4]) for k in assets_keys if k.split("_")[3] == "$"}
    d_tiles = {tile_of(k) for k in dumped}
    res = Counter()
    per_bpp = defaultdict(Counter)
    misses = defaultdict(list)
    pix_ok = pix_bad = 0
    bad_examples = []
    for k, j in dumped.items():
        b = j["bpp"]
        per_bpp[b]["dumped"] += 1
        t = tile_of(k) in gen_tiles
        ex = k in gen_exact
        wild = ("_".join(k.split("_")[:3]), k.split("_")[4]) in gen_wild
        per_bpp[b]["tile"] += t
        per_bpp[b]["tile+pal"] += ex
        per_bpp[b]["tile+pal|$"] += ex or wild
        if not t: misses[(b, j["palhash"])].append(k)
        if ex and dumps_dir:
            p = os.path.join(dumps_dir, k + ".png")
            if os.path.exists(p):
                from PIL import Image
                d = np.array(Image.open(p).convert("RGBA"))
                g = rgba_of[k]
                if d.shape == g.shape and np.array_equal(d, g): pix_ok += 1
                else:
                    pix_bad += 1
                    if len(bad_examples) < 5: bad_examples.append(k)
    uniq_d = len(d_tiles)
    print(f"  dumped sprite keys {len(dumped)} ({uniq_d} unique tile hashes)")
    print(f"  unique tile hashes reproduced: {len(d_tiles & gen_tiles)}/{uniq_d}")
    for b, c in sorted(per_bpp.items()):
        print(f"   bpp {b:3s}: keys {c['dumped']:4d} | tile {c['tile']:4d} | tile+pal exact {c['tile+pal']:4d} | exact-or-$ {c['tile+pal|$']:4d}")
    print(f"  pixel check on exact matches: {pix_ok} identical, {pix_bad} differ {bad_examples}")
    print("  unmatched tile hashes by (bpp, palhash):")
    for (b, ph), ks in sorted(misses.items(), key=lambda kv: -len(kv[1])):
        print(f"    {b:3s} {ph}: {len(ks)}")
    return misses


# ============================================================ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("--manifest", help="device sprites/manifest.jsonl to validate against")
    ap.add_argument("--dumps", help="folder with the dumped obj1_*.png (pixel check)")
    ap.add_argument("--out", help="write assembled PNGs + keys.jsonl here")
    ap.add_argument("--crops", action="store_true", help="also write native per-key PNGs (named by key)")
    ap.add_argument("--no-profile", action="store_true", help="ignore the game recipe's 2D rules")
    a = ap.parse_args()

    rom = open(a.rom, "rb").read()
    code = rom[0x0C:0x10].decode("latin1")
    profile = None if a.no_profile else (recipes.load(code)["twod"] or None)
    leaves = collect_files(rom)
    lib = Library(leaves)
    kc = Counter(b[:4] for b in leaves.values() if b[:4] in Library.KINDS)
    print(f"== {code}: {len(leaves)} leaf files; " + ", ".join(f"{k.decode()}={v}" for k, v in kc.items())
          + f"; profile={'yes' if profile else 'none'}")

    want_rgba = bool(a.manifest) or a.crops
    all_keys, rgba_of = set(), {}
    stats = Counter()
    pair_modes = Counter()
    out_f = None
    if a.out:
        os.makedirs(os.path.join(a.out, "assets"), exist_ok=True)
        if a.crops:
            os.makedirs(os.path.join(a.out, "sprites"), exist_ok=True)
            os.makedirs(os.path.join(a.out, "bgtiles"), exist_ok=True)
        out_f = open(os.path.join(a.out, "keys.jsonl"), "w", encoding="utf-8")
    from PIL import Image
    for gen in (build_cells(lib, profile, want_rgba), build_screens(lib, profile, want_rgba)):
        for asset in gen:
            kind = asset["kind"]
            stats[kind + "s"] += 1
            pair_modes[(kind, asset["pairing"])] += 1
            for en in asset["entries"]:
                if kind == "cell" and not en["opaque"]: continue
                all_keys.add(en["key"])
                stats[kind + "_entries"] += 1
                if kind == "cell" and not en["clean"]: stats["cell_entries_not_clean"] += 1
            if want_rgba:
                for k, v in asset["obj_rgba"].items(): rgba_of.setdefault(k, v)
            if out_f:
                safe = asset["name"].replace(":", "_").replace("/", "__").replace("#", "_")
                if asset.get("nclr"):
                    safe += "__" + os.path.basename(asset["nclr"]).rsplit(".", 1)[0]
                png = os.path.join("assets", safe + ".png")
                Image.fromarray(asset["image"], "RGBA").save(os.path.join(a.out, png))
                rec = {k: v for k, v in asset.items() if k not in ("image", "obj_rgba")}
                rec["png"] = png
                out_f.write(json.dumps(rec) + "\n")
                if a.crops:
                    for k, v in asset["obj_rgba"].items():
                        sub = "sprites" if k.startswith("obj1") else "bgtiles"
                        fn = os.path.join(a.out, sub, k.replace("$", "$") + ".png")
                        if v[..., 3].any() and not os.path.exists(fn):
                            Image.fromarray(v, "RGBA").save(fn)
    if out_f: out_f.close()
    nobj = sum(1 for k in all_keys if k.startswith("obj1"))
    nbg = len(all_keys) - nobj
    print(f"  cells {stats['cells']} ({stats['cell_entries']} OBJ entries, {stats['cell_entries_not_clean']} overlap another OBJ), "
          f"screens {stats['screens']} ({stats['screen_entries']} unique-per-screen tile entries)")
    print(f"  unique keys: obj1 {nobj} (with '$' pal: {sum(1 for k in all_keys if k.startswith('obj1') and '_$_' in k)}), "
          f"bg1 {nbg} (with '$' pal: {sum(1 for k in all_keys if k.startswith('bg1') and '_$_' in k)})")
    print("  pairing modes:", dict(pair_modes))
    if a.manifest:
        validate(all_keys, rgba_of, a.manifest, a.dumps)


if __name__ == "__main__":
    main()
