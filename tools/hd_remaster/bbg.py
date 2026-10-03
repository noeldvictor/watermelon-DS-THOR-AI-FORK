"""BBG pictures (Rosario + Vampire: Tanabata no Miss Youkai Gakuen, Capcom/Dimps): backgrounds,
event CGs, title and menu screens, as tiled BG screens for the 2D extraction.

Layout (little endian), all offsets from the start of the file:
    0x00 'BBG\\0'
    0x04 u32 total size of the sections once decompressed
    0x08 u32 offset of the tiles, 0x0C of the map, 0x10 of the palette
    0x14 u16 bpp (1 = 256 colours, 0 = 16), u16 width and u16 height in tiles
    0x1A u16 tile number the map starts at (an offset into the uploaded tiles), u16 palette row
         base, u16: a 16-colour picture's palette is uploaded from row <base> on, and its map
         entries already count rows from there (near.bbg: base 3, map rows 3-6, file rows 0-3)
Each section is Nintendo-compressed (LZ10) or stored (type byte 0x00 + 24-bit size). The map is
DS screen entries (tile 0-9, h/v flip 10-11, palette 12-15); the palette 256 colours (512 bytes),
uploaded whole to the extended palette slot the game draws the 256-colour BG with, which is
what the runtime's BG key hashes. Whole pictures are 32x24 tiles (one screen) or taller/wider
for scrolling art, stored as a linear map.

#BPA palette animations sit beside some pictures (the battle's bottom screen cycles all its
colours): '#BPA', u32 section count, u32 section offsets; a section is u16 frame count, u8 first
colour it writes, u8 last colour (relative to the first), u16, u16 frame size, then frames of
u32 duration + the colours, as many as the section holds (the first u16 is not their count). The
colour index counts from the picture's palette upload (its base row for 16 colours). A section
belongs to a picture when one of its frames equals the picture's own colours there (bit 15 aside:
the animation's colours carry it, the picture's don't); every other frame is a palette the
runtime keys the tiles with, bit 15 included, as the game copies them into palette memory.
"""
from __future__ import annotations

import struct

import nitro

MAGIC = b"BBG\0"
BPA = b"#BPA"


def _section(b: bytes) -> bytes | None:
    if not b:
        return None
    if b[0] == 0x00:
        size = struct.unpack_from("<I", b, 0)[0] >> 8
        return b[4:4 + size] if 4 + size <= len(b) else None
    return nitro.decompress(b)


class Picture:
    """A decoded BBG in the shapes twod.assemble_screen takes (screen, tiles, palette)."""

    def __init__(self, name: str, b: bytes):
        self.name = name
        t_off, m_off, p_off = struct.unpack_from("<3I", b, 8)
        bpp, w, h, first, pal_base = struct.unpack_from("<5H", b, 0x14)
        self.bpp8 = bpp == 1
        self.tiles = _section(b[t_off:m_off]) or b""
        raw_map = _section(b[m_off:p_off]) or b""
        raw_pal = (_section(b[p_off:]) or b"")[:512]
        self.palette = raw_pal.ljust(512, b"\0")
        # where the palette's rows land; a full 16-row palette can only start at row 0
        # (adv_talk_win01_bg names base 15 with one)
        rows = len(raw_pal) // 32
        self.pal_base = pal_base if pal_base and pal_base + rows <= 16 else 0
        self.wpx, self.hpx = w * 8, h * 8
        self.first = first
        n = min(len(raw_map) // 2, w * h)
        self.entries = list(struct.unpack_from(f"<{n}H", raw_map, 0)) if n else []
        self.fmt = 0                             # a text BG, as twod expects of an NSCR

    # twod's NCGR / NCLR stand-ins
    @property
    def bpp(self) -> int:
        return 8 if self.bpp8 else 4

    @property
    def data(self) -> bytes:
        # map tile numbers count from where the game uploads the picture's tiles (`first`):
        # index the decoded tiles from there
        tb = 64 if self.bpp8 else 32
        return b"\0" * (self.first * tb) + self.tiles

    @property
    def raw(self) -> bytes:
        return self.palette

    def row(self, i: int, n: int) -> bytes | None:
        if n == 16 and not self.bpp8:
            i -= self.pal_base               # map rows count from the upload's base row
            if i < 0:
                return None
        return self.palette[i * n * 2:(i + 1) * n * 2]


def bpa_sections(b: bytes) -> list[tuple[int, int, list[bytes]]]:
    """A #BPA's sections as (first colour, colour count, [frame colours])."""
    out = []
    try:
        n = struct.unpack_from("<I", b, 4)[0]
        offs = list(struct.unpack_from(f"<{n}I", b, 8)) + [len(b)]
        for i in range(n):
            sec = b[offs[i]:offs[i + 1]]
            frames, first, last, _, size = struct.unpack_from("<HBBHH", sec, 0)
            count = last + 1
            if size != 4 + 2 * count:
                continue
            colours = [sec[8 + f * size + 4:8 + (f + 1) * size] for f in range((len(sec) - 8) // size)]
            out.append((first, count, colours))
    except struct.error:
        pass
    return out


def _variants(pic: Picture, sections) -> list[tuple[str, bytes]]:
    """Palettes of `pic` with one animated section in each of its other frames."""
    def colour15(c: bytes) -> bytes:
        v = struct.unpack(f"<{len(c) // 2}H", c)
        return struct.pack(f"<{len(v)}H", *(x & 0x7FFF for x in v))

    out = []
    for s_i, (first, count, frames) in enumerate(sections):
        a, z = first * 2, (first + count) * 2
        if z > 512 or pic.palette[a:z] not in {colour15(f) for f in frames}:
            continue
        seen = set()
        for f_i, colours in enumerate(frames):
            if colours in seen:
                continue
            seen.add(colours)
            out.append((f"#pa{s_i}.{f_i}", pic.palette[:a] + colours + pic.palette[z:]))
    return out


def pictures(files: dict[str, bytes]):
    """Every BBG in the unpacked files, decoded, then its palette-animation variants."""
    anims: dict[str, list] = {}
    for name, b in files.items():
        if b[:4] == BPA:
            anims.setdefault(name.rpartition("/")[0], []).extend(bpa_sections(b))
    for name, b in sorted(files.items()):
        if b[:4] == MAGIC and len(b) >= 0x20:
            try:
                pic = Picture(name, b)
            except struct.error:
                continue
            if not (pic.tiles and pic.entries):
                continue
            yield pic
            for suffix, palette in _variants(pic, anims.get(name.rpartition("/")[0], [])):
                var = Picture(name, b)
                var.name = name + suffix
                var.palette = palette
                yield var


def build_screens(files: dict[str, bytes], want_rgba: bool = False):
    """BBG pictures as 2D screen assets (see twod.build_screens)."""
    import twod
    for pic in pictures(files):
        asset = twod.assemble_screen(pic, pic, pic, want_rgba)
        if asset is None:
            continue
        asset.update(name=pic.name, nscr=pic.name, ncgr=pic.name, nclr=pic.name, pairing="bbg", gpair="bbg")
        yield asset
