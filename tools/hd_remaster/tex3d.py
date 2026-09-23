"""3D textures: parse TEX0 blocks, decode them exactly like melonDS, and compute pack keys.

A DS game uploads a TEX0 block's texel and palette bytes to VRAM unchanged, and the pack key
is a content hash of those bytes, so every key can be computed from the ROM:

    tex1_<W>x<H>_<texhash>_<palhash|none>_<fmt>

texhash is XXH64 of the texel bytes (for 4x4-compressed textures, XXH64 over the two slot
hashes). palhash is XXH64 of the palette entries the format can address, salted when color 0
is transparent for formats 2-4, and for compressed textures a chained XXH64 over only the
entries the blocks reference. This mirrors Texcache::PackPalHash in GPU3D_Texcache.h; the
`legacy` variant reproduces keys dumped before that scheme changed, for verification only.

Which palette goes with which texture is decided by the model's materials (MDL0), so pairs
are read from there. Textures no material names fall back to the palettes in their own block.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np
import xxhash

from nitro import Blob, blocks, read_dict, u16, u32

PAL_ENTRIES = {1: 32, 2: 4, 3: 16, 4: 256, 6: 8}
TEXEL_BYTES = {1: 1.0, 2: 0.25, 3: 0.5, 4: 1.0, 5: 0.25, 6: 1.0, 7: 2.0}
FALLBACK_PALETTE_CAP = 8     # unpaired textures try at most this many palettes from their block


def xxh(data: bytes, seed: int = 0) -> int:
    return xxhash.xxh64_intdigest(data, seed)


@dataclass
class Palette:
    name: str
    data: bytes          # everything from this palette's offset to the end of the palette block


@dataclass
class Texture:
    name: str
    fmt: int
    w: int
    h: int
    color0: bool         # TexParam bit 29
    texel: bytes         # slot 0 bytes (w*h*bpp)
    aux: bytes = b""     # fmt 5 only: palette-index data from slot 1
    wrap: tuple[bool, bool, bool, bool] | None = None   # repeat S/T, flip S/T, from a material
    raw_palette: int = -1   # raw SDK texture: byte length of its .ntfp; -1 for TEX0 textures

    @property
    def texhash(self) -> int:
        if self.fmt == 5:
            return xxh(struct.pack("<QQ", xxh(self.texel), xxh(self.aux)))
        return xxh(self.texel)


@dataclass
class TexBlock:
    source: str
    textures: dict[str, Texture] = field(default_factory=dict)
    palettes: dict[str, Palette] = field(default_factory=dict)
    pairs: set[tuple[str, str]] = field(default_factory=set)   # from this file's materials
    wraps: dict[str, tuple[bool, bool, bool, bool]] = field(default_factory=dict)


# ---------------------------------------------------------------------------- parsing

def parse_tex0(b: bytes, t: int, source: str) -> TexBlock | None:
    if b[t:t + 4] != b"TEX0":
        return None
    tex_data = t + u32(b, t + 0x14)
    tex_dict = t + u16(b, t + 0x0E)
    c_data = t + u32(b, t + 0x24)
    c_info = t + u32(b, t + 0x28)
    pal_size = u32(b, t + 0x30) << 3
    pal_dict = t + u32(b, t + 0x34)
    pal_data = t + u32(b, t + 0x38)
    blk = TexBlock(source)
    for name, e in read_dict(b, tex_dict):
        p = u32(e, 0)
        fmt = (p >> 26) & 7
        if fmt == 0:
            continue
        w, h = 8 << ((p >> 20) & 7), 8 << ((p >> 23) & 7)
        off = (p & 0xFFFF) << 3
        size = int(w * h * TEXEL_BYTES[fmt])
        if fmt == 5:
            texel = b[c_data + off:c_data + off + size]
            aux = b[c_info + off // 2:c_info + off // 2 + w * h // 8]
            if len(aux) != w * h // 8:
                continue
        else:
            texel = b[tex_data + off:tex_data + off + size]
            aux = b""
        if len(texel) != size:
            continue
        blk.textures[name] = Texture(name, fmt, w, h, bool(p & (1 << 29)), texel, aux)
    if pal_size:
        end = pal_data + pal_size
        for name, e in read_dict(b, pal_dict):
            po = pal_data + (u16(e, 0) << 3)
            if po < end:
                blk.palettes[name] = Palette(name, b[po:end])
    return blk


def parse_mdl0_pairs(b: bytes, m: int) -> tuple[set[tuple[str, str]], dict[str, tuple]]:
    """(texture, palette) name pairs and texture wrap modes from a MDL0 block's materials."""
    pairs: set[tuple[str, str]] = set()
    wraps: dict[str, tuple] = {}
    for _, e in read_dict(b, m + 8):
        model = m + u32(e, 0)
        mat = model + u32(b, model + 0x08)
        tex_of: dict[int, str] = {}
        pal_of: dict[int, str] = {}
        for names, pairing_off in ((tex_of, u16(b, mat)), (pal_of, u16(b, mat + 2))):
            for name, pe in read_dict(b, mat + pairing_off):
                lst, cnt = u16(pe, 0), pe[2]
                for k in range(cnt):
                    names[b[mat + lst + k]] = name
        materials = read_dict(b, mat + 4)
        for idx, (_, me) in enumerate(materials):
            tname = tex_of.get(idx)
            if tname is None:
                continue
            pname = pal_of.get(idx)
            if pname is not None:
                pairs.add((tname, pname))
            param = u32(b, mat + u32(me, 0) + 0x14)
            wraps[tname] = (bool(param & 1 << 16), bool(param & 1 << 17),
                            bool(param & 1 << 18), bool(param & 1 << 19))
    return pairs, wraps


def scan(blobs: list[Blob]) -> list[TexBlock]:
    """Every TEX0 block in the ROM, standalone (BTX0/BMD0) or embedded in a custom container."""
    out: list[TexBlock] = []
    for blob in blobs:
        b = blob.data
        pairs, wraps = set(), {}
        if blob.magic == b"BMD0":
            mdl0 = blocks(b).get(b"MDL0")
            if mdl0 is not None:
                try:
                    pairs, wraps = parse_mdl0_pairs(b, mdl0)
                except (IndexError, struct.error):
                    pass
        pos = b.find(b"TEX0")
        while pos != -1:
            try:
                blk = parse_tex0(b, pos, f"{blob.path}#{pos:x}")
            except (IndexError, struct.error, UnicodeError):
                blk = None
            if blk and blk.textures:
                out.append(blk)
            pos = b.find(b"TEX0", pos + 4)
        # pairs are matched by name, so a model's materials also cover textures kept in a BTX0
        if pairs:
            out.append(TexBlock(f"{blob.path}#materials", pairs=pairs, wraps=wraps))
    return out


# ---------------------------------------------------------------------------- raw SDK textures

def _pow2(n: int) -> bool:
    return 8 <= n <= 1024 and n & (n - 1) == 0


def _raw_formats(texel: bytes, colours: int, has_index: bool) -> list[int]:
    """Formats a raw texture could be. The byte count fits several (a 1 KB file is 32x128 at
    2 bits or 32x32 at 8), so every format the palette and texel values allow is a candidate
    and the decode that looks most like a picture wins (see scan_raw)."""
    if has_index:
        return [5]
    if colours == 0:
        return [7]
    raw = np.frombuffer(texel, np.uint8)
    # the largest colour index each reading would use; a texture only uses colours its own
    # palette file defines, which rules most readings out (a one-colour shadow whose shape
    # lives in A5I3's alpha bits reads as garbage indices at 2 or 8 bits per pixel)
    top = {
        2: max(int(((raw >> s) & 3).max()) for s in (0, 2, 4, 6)),
        3: max(int((raw & 15).max()), int((raw >> 4).max())),
        4: int(raw.max()),
        6: int((raw & 7).max()),
        1: int((raw & 31).max()),
    }
    limit = {2: 4, 3: 16, 4: 256, 6: 8, 1: 32}
    fits = [f for f in (2, 3, 4, 6, 1) if colours <= limit[f] or f == 4]
    inside = [f for f in fits if top[f] < colours]
    return inside or fits


def _row_break(img: np.ndarray) -> float:
    """How badly neighbouring pixels disagree, down and across, colour and alpha: low for the
    right format and width, high for a wrong one (a wrong width shears rows apart, a wrong
    bit depth scrambles pixels within a row). Alpha counts because formats 1 and 6 keep the
    shape in it."""
    px = img.astype(np.int32)
    px[..., :3] = px[..., :3] * (px[..., 3:4] > 0)      # colour under alpha 0 doesn't show
    down = np.abs(px[1:] - px[:-1]).mean() if img.shape[0] > 1 else 0.0
    across = np.abs(px[:, 1:] - px[:, :-1]).mean() if img.shape[1] > 1 else 0.0
    return float(down + across)


def scan_raw(blobs: list[Blob]) -> list[TexBlock]:
    """Raw SDK texture files: .ntft texels, .ntfp palette, .ntfi 4x4 index data, no header.

    Pairing is by file stem. The size isn't stored, so every power-of-two width and height that
    fits the byte count is decoded and the one whose rows join up best is kept. A palette file
    shorter than what the key hashes (all 256 colours for 8-bit textures) gets the '$' wildcard:
    the rest of that palette memory holds whatever else is loaded there, which the ROM can't
    say, and the picture only uses the colours in its own file.
    """
    by_stem: dict[str, dict[str, bytes]] = {}
    for blob in blobs:
        stem, dot, ext = blob.path.rpartition(".")
        if dot and ext in ("ntft", "ntfp", "ntfi"):
            by_stem.setdefault(stem, {})[ext] = blob.data
    out: list[TexBlock] = []
    for stem, parts in sorted(by_stem.items()):
        texel = parts.get("ntft")
        if not texel:
            continue
        pal = parts.get("ntfp", b"")
        index = parts.get("ntfi")
        palette = Palette("raw", pal.ljust(512, b"\0"))
        best = None
        for fmt in _raw_formats(texel, len(pal) // 2, index is not None):
            pixels = int(len(texel) / TEXEL_BYTES[fmt])
            for w in (8, 16, 32, 64, 128, 256, 512, 1024):
                h = pixels // w
                if not _pow2(h) or w * h != pixels or max(w, h) > 8 * min(w, h):
                    continue
                tex = Texture(stem.rsplit("/", 1)[-1], fmt, w, h, False, texel,
                              index[:w * h // 8] if index else b"")
                try:
                    score = _row_break(decode(tex, palette if fmt != 7 else None))
                except (IndexError, ValueError):
                    continue
                if best is None or score < best[0]:
                    best = (score, tex)
        if best is None:
            continue
        tex = best[1]
        fmt = tex.fmt
        tex.raw_palette = len(pal)
        blk = TexBlock(f"{stem}.ntft")
        blk.textures[tex.name] = tex
        if fmt in (2, 3, 4):
            # Whether colour 0 is transparent is set by the game's code, not the file. With an
            # exact palette the key differs between the two, so both are written and the one
            # the game uses matches. A '$' key can only be one of them: guess transparent when
            # index 0 covers most of the border, as it does around a cut-out like a logo.
            if len(pal) >= PAL_ENTRIES[fmt] * 2:
                alt = Texture(tex.name, fmt, tex.w, tex.h, True, tex.texel, tex.aux,
                              raw_palette=len(pal))
                blk.textures[tex.name + "#c0"] = alt
            else:
                tex.color0 = _border_index0(tex) >= 0.5
        if pal:
            blk.palettes[tex.name + "_pl"] = Palette(tex.name + "_pl", pal)
        out.append(blk)
    return out


def _border_index0(tex: Texture) -> float:
    """Share of border pixels that use palette index 0 (formats 2-4)."""
    bits = {2: 2, 3: 4, 4: 8}[tex.fmt]
    raw = np.frombuffer(tex.texel, np.uint8)
    idx = np.stack([(raw >> (bits * k)) & ((1 << bits) - 1) for k in range(8 // bits)], axis=1)
    idx = idx.reshape(tex.h, tex.w)
    border = np.concatenate([idx[0], idx[-1], idx[:, 0], idx[:, -1]])
    return float((border == 0).mean())


# ---------------------------------------------------------------------------- keys

def _palette_words(pal: Palette, n: int) -> bytes | None:
    return pal.data[:n * 2] if len(pal.data) >= n * 2 else None


def compressed_used(tex: Texture) -> tuple[list[int], int, int]:
    """Palette byte offsets the 4x4 blocks reference, and their bounding span."""
    used: set[int] = set()
    lo, hi = 1 << 30, 0
    for i in range(0, len(tex.aux), 2):
        aux = u16(tex.aux, i)
        start = (aux & 0x3FFF) * 4
        mode = aux >> 14
        n = 4 if mode == 2 else (3 if mode == 0 else 2)
        used.update(start + 2 * k for k in range(n))
        lo, hi = min(lo, start), max(hi, start + 2 * n)
    return sorted(used), lo, hi


def palhash(tex: Texture, pal: Palette | None, legacy: bool = False) -> int | None:
    """Palette part of the key; None when this palette can't serve this texture."""
    if tex.fmt == 7:
        return 0
    if pal is None:
        return None
    if tex.fmt == 5:
        used, lo, hi = compressed_used(tex)
        if not used or hi > len(pal.data):
            return None
        if legacy:
            return xxh(pal.data[lo:hi])
        h = 0
        for a in used:
            h = xxh(pal.data[a:a + 2], h)
        return h
    words = _palette_words(pal, PAL_ENTRIES[tex.fmt])
    if words is None:
        return None
    h = xxh(words)
    if not legacy and tex.color0 and tex.fmt in (2, 3, 4):
        h = xxh(b"\x01", h)
    return h


WILDCARD = -1   # palette part of the key is '$': matches any palette


def key_name(tex: Texture, ph: int) -> str:
    pal = "none" if tex.fmt == 7 else ("$" if ph == WILDCARD else f"{ph:016x}")
    return f"tex1_{tex.w}x{tex.h}_{tex.texhash:016x}_{pal}_{tex.fmt}"


# ---------------------------------------------------------------------------- decoding

def _rgb5_to_rgba8(c: np.ndarray, alpha5: np.ndarray) -> np.ndarray:
    """melonDS's RGB5 -> RGB6 (nonzero channels +1) then the dump's RGB6A5 -> RGBA8."""
    c = c.astype(np.uint32)
    r6 = (c & 0x1F) << 1
    g6 = (c & 0x3E0) >> 4
    b6 = (c & 0x7C00) >> 9
    out = np.empty(c.shape + (4,), np.uint8)
    for i, ch in enumerate((r6, g6, b6)):
        ch = ch + (ch > 0)
        out[..., i] = (ch << 2) | (ch >> 4)
    a = alpha5.astype(np.uint32)
    out[..., 3] = (a << 3) | (a >> 2)
    return out


def decode(tex: Texture, pal: Palette | None) -> np.ndarray:
    """RGBA8 image (h, w, 4) identical to what the emulator dumps for this texture."""
    w, h, fmt = tex.w, tex.h, tex.fmt
    if fmt == 7:
        c = np.frombuffer(tex.texel, "<u2").reshape(h, w)
        return _rgb5_to_rgba8(c, np.where(c & 0x8000, 31, 0))
    data = pal.data[:len(pal.data) & ~1]
    palw = np.frombuffer(data.ljust(512, b"\0") if len(data) < 512 else data, "<u2")
    if fmt == 5:
        return _decode_4x4(tex, palw)
    raw = np.frombuffer(tex.texel, np.uint8)
    if fmt in (1, 6):
        ibits = 5 if fmt == 1 else 3
        idx = raw & ((1 << ibits) - 1)
        a = raw >> ibits
        if fmt == 1:
            a = a * 4 + a // 2
        return _rgb5_to_rgba8(palw[idx].reshape(h, w), a.reshape(h, w))
    bits = {2: 2, 3: 4, 4: 8}[fmt]
    per = 8 // bits
    idx = np.stack([(raw >> (bits * k)) & ((1 << bits) - 1) for k in range(per)], axis=1).reshape(-1)
    alpha = np.where((idx == 0) & tex.color0, 0, 31)
    return _rgb5_to_rgba8(palw[idx].reshape(h, w), alpha.reshape(h, w))


def _decode_4x4(tex: Texture, palw: np.ndarray) -> np.ndarray:
    w, h = tex.w, tex.h
    bw = w // 4
    data = np.frombuffer(tex.texel, "<u4")
    aux = np.frombuffer(tex.aux, "<u2")
    out = np.zeros((h, w, 4), np.uint8)

    def mix(c0, c1, f0, f1, shift):
        r = ((c0 & 0x1F) * f0 + (c1 & 0x1F) * f1) >> shift
        g = (((c0 & 0x3E0) * f0 + (c1 & 0x3E0) * f1) >> shift) & 0x3E0
        b = (((c0 & 0x7C00) * f0 + (c1 & 0x7C00) * f1) >> shift) & 0x7C00
        return r | g | b | 0x8000

    for bi in range(len(data)):
        a = int(aux[bi])
        base = (a & 0x3FFF) * 2
        mode = a >> 14
        cols = [int(palw[base + k]) | 0x8000 if base + k < len(palw) else 0x8000 for k in range(4)]
        c0, c1 = cols[0], cols[1]
        if mode == 0:
            cols[3] = 0
        elif mode == 1:
            cols[2] = mix(c0, c1, 1, 1, 1)
            cols[3] = 0
        elif mode == 3:
            cols[2] = mix(c0, c1, 5, 3, 3)
            cols[3] = mix(c0, c1, 3, 5, 3)
        word = int(data[bi])
        px = np.array([cols[(word >> (2 * i)) & 3] for i in range(16)], np.uint32).reshape(4, 4)
        y, x = (bi // bw) * 4, (bi % bw) * 4
        out[y:y + 4, x:x + 4] = _rgb5_to_rgba8(px, np.where(px & 0x8000, 31, 0))
    return out


# ---------------------------------------------------------------------------- pairing

@dataclass
class Entry:
    key: str
    legacy_key: str
    tex: Texture
    pal: Palette | None
    pal_name: str
    source: str
    pairing: str         # material | name | block | none


def entries(blocks_: list[TexBlock]) -> list[Entry]:
    """Every (texture, palette) combination worth writing, keyed by its current pack key."""
    material_pairs: dict[str, set[str]] = {}
    wraps: dict[str, tuple] = {}
    for blk in blocks_:
        for t, p in blk.pairs:
            material_pairs.setdefault(t, set()).add(p)
        wraps.update(blk.wraps)

    out: dict[str, Entry] = {}
    for blk in blocks_:
        for tex in blk.textures.values():
            tex.wrap = wraps.get(tex.name)
            if tex.fmt == 7:
                cands = [(None, "", "none")]
            else:
                named = [blk.palettes[p] for p in material_pairs.get(tex.name, ()) if p in blk.palettes]
                if named:
                    cands = [(p, p.name, "material") for p in named]
                elif tex.name + "_pl" in blk.palettes:
                    p = blk.palettes[tex.name + "_pl"]
                    cands = [(p, p.name, "name")]
                else:
                    cands = [(p, p.name, "block")
                             for p in list(blk.palettes.values())[:FALLBACK_PALETTE_CAP]]
            for pal, pname, how in cands:
                ph = palhash(tex, pal)
                lph = palhash(tex, pal, legacy=True)
                if ph is None and tex.raw_palette >= 0 and pal is not None and tex.fmt not in (5, 7):
                    # a raw palette file shorter than what the key hashes (see scan_raw)
                    ph = lph = WILDCARD
                    how = "raw-wildcard"
                if ph is None:
                    continue
                key = key_name(tex, ph)
                if key in out:
                    continue
                out[key] = Entry(key, key_name(tex, lph), tex, pal, pname, blk.source, how)
    return list(out.values())
