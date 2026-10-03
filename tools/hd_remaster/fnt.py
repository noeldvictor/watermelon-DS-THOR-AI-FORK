"""#FNT fonts (Rosario + Vampire: Tanabata no Miss Youkai Gakuen, Capcom/Dimps), converted to
the NFTR layout fonts.py and the emulator's HD text read.

Layout (little endian):
    0x00 '#FNT', u8 cell width, u8 cell height, u16 glyph count
    0x08 u16, u16, u32 offset of the glyph table, u32 offset of the widths (one byte per glyph),
         u32 offset of a 16-colour palette, u32, u32
    widths   one advance per glyph
    table    per glyph 8 bytes: u16, u16 character code, u32 offset of its bitmap
    bitmaps  LZ10-compressed, 128 bytes each once unpacked: 16x16 pixels at 4 bpp as 2x2 tiles
             of 8x8 (DS tile order, low nibble first), shades 0 (none) to 15
The converted font has CGLP (2 bpp when the glyphs use only shades 0-3, as Rosario's do, else 4;
MSB-first as NFTR stores it) and CWDH; it has no character map: text is recognised from the
glyphs' pixels, not their codes.
"""
from __future__ import annotations

import struct

import nitro

MAGIC = b"#FNT"


def glyphs(b: bytes) -> tuple[int, int, list[tuple[int, list[int]]]]:
    """(cell width, cell height, [(width, 256 shades row by row)])."""
    cw, ch, n = b[4], b[5], struct.unpack_from("<H", b, 6)[0]
    table, widths = struct.unpack_from("<2I", b, 0x0C)
    out = []
    for i in range(n):
        off = struct.unpack_from("<I", b, table + 8 * i + 4)[0]
        data = nitro.decompress(b[off:off + 512], bounded=False)
        if data is None or len(data) < cw * ch // 2:
            data = bytes(cw * ch // 2)
        px = [0] * (cw * ch)
        tiles_x = cw // 8
        for t in range((cw // 8) * (ch // 8)):
            ty, tx = divmod(t, tiles_x)
            for r in range(8):
                for c in range(8):
                    v = data[t * 32 + r * 4 + c // 2]
                    px[(ty * 8 + r) * cw + tx * 8 + c] = (v >> 4) if c & 1 else (v & 0xF)
        out.append((b[widths + i], px))
    return cw, ch, out


def to_nftr(b: bytes) -> bytes | None:
    if b[:4] != MAGIC or len(b) < 0x20:
        return None
    cw, ch, gl = glyphs(b)
    # the shades in use: Rosario's glyphs are ink 1 and edge 2 (a few symbols have 213 pixels of
    # 3-7 between them); stored as 2 bpp, so the atlas and the HD text's shading span 0-3 rather
    # than the bottom eighth of 0-15 (those few pixels clamp to 3)
    ink = [v for _, px in gl for v in px if v]
    bpp = 2 if ink and sum(v > 3 for v in ink) < len(ink) // 1000 else 4
    top = (1 << bpp) - 1
    cell_size = (cw * ch * bpp + 7) // 8
    # CGLP: cell width/height, bytes per cell, baseline, max width, bpp, rotation, then cells
    # packed MSB first, as NFTR stores them
    cells = bytearray()
    for _, px in gl:
        cell = bytearray(cell_size)
        for i, v in enumerate(px):
            v = min(v, top)
            for k in range(bpp):
                bit = i * bpp + k
                if (v >> (bpp - 1 - k)) & 1:
                    cell[bit >> 3] |= 0x80 >> (bit & 7)
        cells += cell
    cglp_data = struct.pack("<BBHbBBB", cw, ch, cell_size, ch - 2, cw, bpp, 0) + bytes(cells)
    # CWDH: first, last, next, then (left, ink width, advance) per glyph
    cwdh_data = struct.pack("<HHI", 0, len(gl) - 1, 0) + b"".join(struct.pack("<bBB", 0, w, w) for w, _ in gl)

    def block(tag: bytes, data: bytes) -> bytes:
        data += b"\0" * (-len(data) % 4)
        return tag + struct.pack("<I", 8 + len(data)) + data

    finf_at, finf_size = 0x10, 0x1C
    cglp_at = finf_at + finf_size
    cglp = block(b"PLGC", cglp_data)
    cwdh_at = cglp_at + len(cglp)
    cwdh = block(b"HDWC", cwdh_data)
    # FINF: font type, line feed, default char, default left/width/advance, encoding, then the
    # offsets of the CGLP and CWDH data (8 past their block headers) and of CMAP (none)
    finf = b"FNIF" + struct.pack("<IBBHBBBBIII", finf_size, 0, ch, 0, 0, cw, cw, 1,
                                 cglp_at + 8, cwdh_at + 8, 0)
    body = finf + cglp + cwdh
    header = b"RTFN" + struct.pack("<HHIHH", 0xFEFF, 0x0100, 0x10 + len(body), 0x10, 3)
    return header + body
