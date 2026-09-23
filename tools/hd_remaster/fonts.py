"""NFTR fonts: the Nitro SDK font format games draw their text with at runtime.

A game renders text into sprite or background tiles one glyph at a time, so text never exists in
the ROM as an image and no pack key can match it. What does exist is the font: every glyph's
bitmap, width and character code. This module reads it so glyphs can be upscaled once and
recognised on the device (see the fonts/ folder of a pack).

Layout (all little-endian):
    RTFN header (0x10)
    FINF  font info: line feed, default widths, encoding, offsets of CGLP / CWDH / CMAP data
    CGLP  glyph bitmaps: cell width/height, bytes per cell, bits per pixel (1, 2 or 4), then
          one packed MSB-first bitstream per glyph, cellW * cellH pixels, rows not padded
    CWDH  per glyph: left offset, ink width, advance (chained blocks)
    CMAP  character code -> glyph index (direct range, table or scan list; chained)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Glyph:
    index: int
    bitmap: np.ndarray        # (cellH, cellW) uint8, 0 = transparent, 1..2^bpp-1 = ink shades
    left: int = 0
    width: int = 0
    advance: int = 0
    chars: list[int] = field(default_factory=list)


@dataclass
class Font:
    name: str
    bpp: int
    cell_w: int
    cell_h: int
    line_feed: int
    baseline: int
    glyphs: list[Glyph]
    char_map: dict[int, int]  # character code -> glyph index

    def glyph_for(self, ch: str) -> Glyph | None:
        i = self.char_map.get(ord(ch))
        return self.glyphs[i] if i is not None else None

    def render(self, text: str, x: int = 0, y: int = 0, size: tuple[int, int] | None = None) -> np.ndarray:
        """Shade image of `text` laid out like the Nitro SDK's text canvas: each glyph's cell is
        drawn at pen x + left, only ink pixels are written, the pen then moves by advance."""
        lines = text.split("\n")
        w = size[0] if size else x + max(sum((g.advance if g else 0) for g in map(self.glyph_for, ln))
                                          for ln in lines) + self.cell_w
        h = size[1] if size else y + len(lines) * self.line_feed + self.cell_h
        out = np.zeros((h, w), np.uint8)
        for li, ln in enumerate(lines):
            pen = x
            top = y + li * self.line_feed
            for ch in ln:
                g = self.glyph_for(ch)
                if g is None:
                    continue
                gx = pen + g.left
                for r in range(self.cell_h):
                    for c in range(self.cell_w):
                        v = g.bitmap[r, c]
                        if v and 0 <= top + r < h and 0 <= gx + c < w:
                            out[top + r, gx + c] = v
                pen += g.advance
        return out


ATLAS_COLUMNS = 32
ATLAS_PAD = 2


def atlas(font: Font) -> np.ndarray:
    """Grey shade atlas as the emulator reads it (melonDS-android-lib/src/HDFont.h): glyph i in
    slot (i % 32, i // 32), slots of (cellW + 4) x (cellH + 4) with the cell 2 pixels in, black
    for no ink and white for the top shade. The padding keeps the upscaler from blending
    neighbouring glyphs and leaves room for an edge it rounds past the cell."""
    sw, sh = font.cell_w + 2 * ATLAS_PAD, font.cell_h + 2 * ATLAS_PAD
    rows = (len(font.glyphs) + ATLAS_COLUMNS - 1) // ATLAS_COLUMNS
    out = np.zeros((rows * sh, ATLAS_COLUMNS * sw), np.uint8)
    top = (1 << font.bpp) - 1
    for g in font.glyphs:
        x = (g.index % ATLAS_COLUMNS) * sw + ATLAS_PAD
        y = (g.index // ATLAS_COLUMNS) * sh + ATLAS_PAD
        out[y:y + font.cell_h, x:x + font.cell_w] = (g.bitmap.astype(np.uint32) * 255 // top).astype(np.uint8)
    return out


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def is_nftr(b: bytes) -> bool:
    return len(b) > 0x30 and b[:4] == b"RTFN" and b[0x10:0x14] == b"FNIF"


def parse(b: bytes, name: str = "") -> Font:
    finf = 0x10
    line_feed = b[finf + 9]
    p_glyph, p_width, p_map = _u32(b, finf + 0x10), _u32(b, finf + 0x14), _u32(b, finf + 0x18)

    cell_w, cell_h = b[p_glyph], b[p_glyph + 1]
    cell_size = _u16(b, p_glyph + 2)
    baseline = struct.unpack_from("<b", b, p_glyph + 4)[0]
    bpp = b[p_glyph + 6]
    block_size = _u32(b, p_glyph - 4)
    data = p_glyph + 8
    count = (p_glyph - 8 + block_size - data) // cell_size
    glyphs = []
    for i in range(count):
        cell = np.frombuffer(b, np.uint8, cell_size, data + i * cell_size)
        bits = np.unpackbits(cell)[: cell_w * cell_h * bpp].reshape(-1, bpp)
        vals = np.zeros(len(bits), np.uint8)
        for k in range(bpp):
            vals = (vals << 1) | bits[:, k]
        glyphs.append(Glyph(i, vals.reshape(cell_h, cell_w)))

    # widths: chained CWDH blocks, each covering a glyph index range
    p = p_width
    while p:
        first, last, nxt = _u16(b, p), _u16(b, p + 2), _u32(b, p + 4)
        for i in range(first, last + 1):
            if i < len(glyphs):
                o = p + 8 + (i - first) * 3
                glyphs[i].left = struct.unpack_from("<b", b, o)[0]
                glyphs[i].width, glyphs[i].advance = b[o + 1], b[o + 2]
        p = nxt

    # character map: chained CMAP blocks of three kinds
    char_map: dict[int, int] = {}
    p = p_map
    while p:
        first, last, kind, nxt = _u16(b, p), _u16(b, p + 2), _u16(b, p + 4), _u32(b, p + 8)
        if kind == 0:        # direct: a run of consecutive glyphs
            base = _u16(b, p + 12)
            for c in range(first, last + 1):
                char_map[c] = base + c - first
        elif kind == 1:      # table: one glyph index per code, 0xFFFF = none
            for c in range(first, last + 1):
                g = _u16(b, p + 12 + (c - first) * 2)
                if g != 0xFFFF:
                    char_map[c] = g
        elif kind == 2:      # scan: explicit (code, glyph) pairs
            n = _u16(b, p + 12)
            for k in range(n):
                char_map[_u16(b, p + 14 + 4 * k)] = _u16(b, p + 16 + 4 * k)
        p = nxt
    for c, g in char_map.items():
        if g < len(glyphs):
            glyphs[g].chars.append(c)
    return Font(name, bpp, cell_w, cell_h, line_feed, baseline, glyphs, char_map)
