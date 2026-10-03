"""BBG pictures (Rosario + Vampire: Tanabata no Miss Youkai Gakuen, Capcom/Dimps): backgrounds,
event CGs, title and menu screens, as tiled BG screens for the 2D extraction.

Layout (little endian), all offsets from the start of the file:
    0x00 'BBG\\0'
    0x04 u32 total size of the sections once decompressed
    0x08 u32 offset of the tiles, 0x0C of the map, 0x10 of the palette
    0x14 u16 bpp (1 = 256 colours, 0 = 16), u16 width and u16 height in tiles
    0x1A u16 tile number the map starts at (an offset into the uploaded tiles), u16, u16
Each section is Nintendo-compressed (LZ10) or stored (type byte 0x00 + 24-bit size). The map is
DS screen entries (tile 0-9, h/v flip 10-11, palette 12-15); the palette 256 colours (512 bytes),
uploaded whole to the extended palette slot the game draws the 256-colour BG with, which is
what the runtime's BG key hashes. Whole pictures are 32x24 tiles (one screen) or taller/wider
for scrolling art, stored as a linear map.
"""
from __future__ import annotations

import struct

import nitro

MAGIC = b"BBG\0"


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
        bpp, w, h, first = struct.unpack_from("<4H", b, 0x14)
        self.bpp8 = bpp == 1
        self.tiles = _section(b[t_off:m_off]) or b""
        raw_map = _section(b[m_off:p_off]) or b""
        self.palette = (_section(b[p_off:]) or b"")[:512].ljust(512, b"\0")
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
        return self.palette[i * n * 2:(i + 1) * n * 2]


def pictures(files: dict[str, bytes]):
    """Every BBG in the unpacked files, decoded."""
    for name, b in sorted(files.items()):
        if b[:4] == MAGIC and len(b) >= 0x20:
            try:
                pic = Picture(name, b)
            except struct.error:
                continue
            if pic.tiles and pic.entries:
                yield pic


def build_screens(files: dict[str, bytes], want_rgba: bool = False):
    """BBG pictures as 2D screen assets (see twod.build_screens)."""
    import twod
    for pic in pictures(files):
        asset = twod.assemble_screen(pic, pic, pic, want_rgba)
        if asset is None:
            continue
        asset.update(name=pic.name, nscr=pic.name, ncgr=pic.name, nclr=pic.name, pairing="bbg", gpair="bbg")
        yield asset
