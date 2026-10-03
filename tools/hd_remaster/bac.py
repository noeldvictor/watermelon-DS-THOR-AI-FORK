"""BAC sprite animations (Rosario + Vampire: Tanabata no Miss Youkai Gakuen, Capcom/Dimps):
character portraits (up / down / eye / mouth), menu and minigame art, as 2D sprite cells.

Layout (little endian; each section starts with its own u32 size):
    0x00 'BAC\\0', then five u32 section offsets from the start of the file
    section 0  animations: (u32 offset into section 1, u32 record count) pairs (not used: records
               are found by the frames they name)
    section 1  records: u16, u16, u32 offset of the frame in section 2, u16, u16 (duration?),
               then per piece of that frame: u32 offset of its pixel chunk in section 4, u16 size
               in 64-byte tiles, u16 flag
    section 2  frames: u32 piece count, s16 left, top, right, bottom of the frame relative to the
               character's anchor (and the anchor's offset in the frame), then per piece OAM
               attributes 0 and 1 (y, shape, 256-colour flag / x, size) and two u16 the game
               fills in at runtime; piece x/y count from the frame's left/top
    section 3  u32 size, u16, u16 palette count, then 256-colour palettes (512 bytes each)
    section 4  u32 size, then pixel chunks: a Nintendo compression header (type 0 = stored,
               0x10/0x11 = LZ) and the piece's tiles as they go to OBJ VRAM (1D mapping)
A piece's VRAM bytes are exactly its chunk, so the runtime's sprite key (its tiles' XXH64 chained
tile by tile, XXH64 of the 256-colour extended palette it uses) comes straight from the file. Checked
on the Thor: Kotori's portrait pieces in OBJ VRAM equal her up.bac chunks byte for byte, and
the frame layout equals her OAM shifted by her position.
"""
from __future__ import annotations

import struct

import numpy as np

import nitro

MAGIC = b"BAC\0"
SIZES = {0: [(8, 8), (16, 16), (32, 32), (64, 64)],
         1: [(16, 8), (32, 8), (32, 16), (64, 32)],
         2: [(8, 16), (8, 32), (16, 32), (32, 64)]}


def _u32(b, o): return struct.unpack_from("<I", b, o)[0]
def _u16(b, o): return struct.unpack_from("<H", b, o)[0]


def _chunk(s4: bytes, off: int, size: int) -> bytes | None:
    if off + 4 > len(s4):
        return None
    head = _u32(s4, off)
    if head & 0xFF == 0:
        data = s4[off + 4:off + 4 + (head >> 8)]
    else:
        data = nitro.decompress(s4[off:])
    if data is None or len(data) < size:
        return None
    return data[:size]


class Frame:
    def __init__(self, pieces, record):
        self.pieces = pieces           # (x, y, w, h, bpp8, tile bytes)
        self.record = record


def _frame_offsets(s2: bytes) -> list[int]:
    """Section 2's frames, walked one after another."""
    out, p = [], 4
    while p + 16 <= len(s2):
        n = _u32(s2, p)
        if not 0 < n <= 128 or p + 16 + 8 * n > len(s2):
            break
        out.append(p)
        p += 16 + 8 * n
    return out


def frames(b: bytes):
    """Every distinct frame of a BAC (pieces with their pixels) and its palettes. A record in
    section 1 names a frame (u32 at +4) and then each piece's chunk; animations chain records in
    ways section 0 doesn't spell out (blinks, mouth movements), so every position of section 1
    that names a frame is tried, and kept when every piece's chunk has that piece's size."""
    offs = list(struct.unpack_from("<5I", b, 4))
    s0, s1, s2, s3, s4 = [b[offs[i]:offs[i + 1]] for i in range(4)] + [b[offs[4]:]]
    n_pal = _u16(s3, 6)
    palettes = [s3[8 + 512 * k:8 + 512 * (k + 1)] for k in range(n_pal) if 8 + 512 * (k + 1) <= len(s3)]
    layouts = {}
    for f_off in _frame_offsets(s2):
        n = _u32(s2, f_off)
        # the frame's corner relative to the character's anchor: up, eye, mouth and down parts
        # share that anchor, so placed by it they line up as the game draws them
        ax, ay = struct.unpack_from("<2h", s2, f_off + 4)
        lay = []
        for i in range(n):
            a0, a1 = _u16(s2, f_off + 16 + 8 * i), _u16(s2, f_off + 18 + 8 * i)
            shape, size = a0 >> 14, a1 >> 14
            if shape == 3:
                lay = None
                break
            w, h = SIZES[shape][size]
            x = a1 & 0x1FF
            x = x - 512 if x >= 256 else x
            lay.append((ax + x, ay + (a0 & 0xFF), w, h, bool(a0 & 0x2000)))   # y as is (0-255)
        if lay:
            layouts[f_off] = lay
    out, seen = [], set()
    for rec in range(4, len(s1) - 11, 4):
        lay = layouts.get(_u32(s1, rec + 4))
        if lay is None or rec + 12 + 8 * len(lay) > len(s1):
            continue
        pieces = []
        for i, (x, y, w, h, bpp8) in enumerate(lay):
            e = rec + 12 + 8 * i
            c_off, tiles = _u32(s1, e), _u16(s1, e + 4)
            need = w * h if bpp8 else w * h // 2
            if tiles * (64 if bpp8 else 32) != need:
                pieces = None
                break
            data = _chunk(s4, c_off, need)
            if data is None:
                pieces = None
                break
            pieces.append((x, y, w, h, bpp8, data))
        if not pieces:
            continue
        key = tuple((p[0], p[1], p[2], p[3], p[5]) for p in pieces)
        if key not in seen:
            seen.add(key)
            out.append(Frame(pieces, rec))
    return out, palettes


def _tile_pixels(data: bytes, w: int, h: int, bpp8: bool) -> np.ndarray:
    """A 1D-mapped piece's colour indices, (h, w)."""
    idx = np.zeros((h, w), np.uint8)
    tb = 64 if bpp8 else 32
    tw = w // 8
    for t in range((w // 8) * (h // 8)):
        tile = data[t * tb:(t + 1) * tb]
        if bpp8:
            px = np.frombuffer(tile, np.uint8).reshape(8, 8)
        else:
            raw = np.frombuffer(tile, np.uint8)
            px = np.stack([raw & 0xF, raw >> 4], -1).reshape(8, 8)
        ty, tx = divmod(t, tw)
        idx[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8] = px
    return idx


def _render(frame: Frame, rgb) -> tuple[np.ndarray, int, int, list]:
    """A frame drawn on its own: (RGBA canvas, origin x, origin y, [(x, y, w, h, data, rgba)])."""
    x0 = min(p[0] for p in frame.pieces)
    y0 = min(p[1] for p in frame.pieces)
    x1 = max(p[0] + p[2] for p in frame.pieces)
    y1 = max(p[1] + p[3] for p in frame.pieces)
    canvas = np.zeros((y1 - y0, x1 - x0, 4), np.uint8)
    drawn = []
    for x, y, w, h, bpp8, data in frame.pieces:
        if not bpp8:
            continue                    # the portraits are 256-colour; 16-colour needs a palette row
        idx = _tile_pixels(data, w, h, True)
        rgba = np.zeros((h, w, 4), np.uint8)
        rgba[..., :3] = rgb[idx]
        rgba[..., 3] = np.where(idx == 0, 0, 255)
        if not rgba[..., 3].any():
            continue
        sub = canvas[y - y0:y - y0 + h, x - x0:x - x0 + w]
        sub[rgba[..., 3] > 0] = rgba[rgba[..., 3] > 0]
        drawn.append((x - x0, y - y0, w, h, data, rgba))
    return canvas, x0, y0, drawn


def build_cells(files: dict[str, bytes], want_rgba: bool = False):
    """BAC frames as 2D cell assets (see twod.build_cells): one per frame and palette. A
    character's parts (up, down, eye, mouth .bac in one archive entry) are drawn over its whole
    body (the first upper- and lower-body frames, placed by their anchor) and upscaled with it,
    so eyes, mouth and waist join without a seam; an asset's entries are only its own pieces."""
    import twod
    decoded = {}
    for name, b in sorted(files.items()):
        if b[:4] != MAGIC or len(b) < 0x18:
            continue
        try:
            decoded[name] = frames(b)
        except struct.error:
            continue
    for name, (fr, palettes) in decoded.items():
        folder, _, leaf = name.rpartition("/")
        body_parts = []
        if leaf in ("up.bac", "down.bac", "eye.bac", "mouth.bac") and f"{folder}/up.bac" in decoded:
            body_parts = [decoded[f"{folder}/{part}"][0][0] for part in ("down.bac", "up.bac")
                          if f"{folder}/{part}" in decoded and decoded[f"{folder}/{part}"][0]]
        for p_i, praw in enumerate(palettes):
            rgb = twod.pal_to_rgb(praw)
            ph = twod.xxh(praw)
            for f_i, frame in enumerate(fr):
                pieces = list(frame.pieces)
                if body_parts:
                    # the body under this frame, then the frame on top
                    layout = Frame([pc for part in body_parts for pc in part.pieces] + pieces, frame.record)
                    canvas, bx, by, _ = _render(layout, rgb)
                else:
                    canvas, bx, by, _ = _render(frame, rgb)
                _, fx, fy, drawn = _render(frame, rgb)
                entries, rgbas = {}, {}
                for x, y, w, h, data, rgba in drawn:
                    # the runtime chains the hash tile by tile (twod.chain_hash), in 1D order
                    tiles = [data[k:k + 64] for k in range(0, w * h, 64)]
                    key = twod.obj_key(w, h, twod.chain_hash(tiles), ph, True)
                    if key not in entries:
                        entries[key] = dict(key=key, x=x + fx - bx, y=y + fy - by, w=w, h=h, hflip=False,
                                            vflip=False, pal_guess=False, opaque=True, rotscale=False,
                                            objmode=0, clean=True)
                        if want_rgba:
                            rgbas[key] = rgba
                if not entries:
                    continue
                asset = dict(kind="cell", image=canvas, origin=(bx, by), entries=list(entries.values()),
                             name=f"{name}#f{f_i}", nclr=f"{name}#p{p_i}", pairing="bac")
                if want_rgba:
                    asset["obj_rgba"] = rgbas
                yield asset
