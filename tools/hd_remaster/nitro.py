"""Read the files inside a DS ROM: NitroFS, NARC archives and LZ10/LZ11 compression.

Games that keep their assets in a custom archive still store standard Nitro files inside it,
often LZ-compressed. Those are found by carving: an LZ stream whose first flag byte is zero
starts with eight literal bytes, so a compressed Nitro file shows its magic in the clear right
after the 4-byte header.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass


def u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


# ---------------------------------------------------------------------------- compression

def _lz10(b: bytes) -> bytes:
    size = u32(b, 0) >> 8
    out = bytearray()
    i = 4
    while len(out) < size:
        flags = b[i]
        i += 1
        for bit in range(8):
            if len(out) >= size:
                break
            if flags & (0x80 >> bit):
                x = (b[i] << 8) | b[i + 1]
                i += 2
                n, d = (x >> 12) + 3, (x & 0xFFF) + 1
                for _ in range(n):
                    out.append(out[-d])
            else:
                out.append(b[i])
                i += 1
    return bytes(out)


def _lz11(b: bytes) -> bytes:
    size = u32(b, 0) >> 8
    i = 4
    if size == 0:
        size = u32(b, 4)
        i = 8
    out = bytearray()
    while len(out) < size:
        flags = b[i]
        i += 1
        for bit in range(8):
            if len(out) >= size:
                break
            if flags & (0x80 >> bit):
                ind = b[i] >> 4
                if ind == 0:
                    n = (((b[i] & 0xF) << 4) | (b[i + 1] >> 4)) + 0x11
                    d = (((b[i + 1] & 0xF) << 8) | b[i + 2]) + 1
                    i += 3
                elif ind == 1:
                    n = (((b[i] & 0xF) << 12) | (b[i + 1] << 4) | (b[i + 2] >> 4)) + 0x111
                    d = (((b[i + 2] & 0xF) << 8) | b[i + 3]) + 1
                    i += 4
                else:
                    n = ind + 1
                    d = (((b[i] & 0xF) << 8) | b[i + 1]) + 1
                    i += 2
                for _ in range(n):
                    out.append(out[-d])
            else:
                out.append(b[i])
                i += 1
    return bytes(out)


def _rle(b: bytes) -> bytes:
    size = u32(b, 0) >> 8
    out = bytearray()
    i = 4
    while len(out) < size:
        f = b[i]
        i += 1
        if f & 0x80:
            out += bytes([b[i]]) * ((f & 0x7F) + 3)
            i += 1
        else:
            n = (f & 0x7F) + 1
            out += b[i:i + n]
            i += n
    return bytes(out[:size])


def _huffman(b: bytes) -> bytes:
    """Nintendo Huffman, 4-bit (0x24) or 8-bit (0x28) symbols."""
    bits = b[0] & 0xF
    size = u32(b, 0) >> 8
    root = 5
    pos = 4 + (b[4] + 1) * 2
    out = bytearray()
    nib = None
    node_addr = root
    while len(out) < size:
        word = u32(b, pos)
        pos += 4
        for bit in range(31, -1, -1):
            node = b[node_addr]
            child = (node_addr & ~1) + (node & 0x3F) * 2 + 2
            if (word >> bit) & 1:
                child += 1
                is_data = node & 0x40
            else:
                is_data = node & 0x80
            if is_data:
                v = b[child]
                if bits == 8:
                    out.append(v)
                elif nib is None:
                    nib = v & 0xF
                else:
                    out.append(nib | ((v & 0xF) << 4))
                    nib = None
                node_addr = root
                if len(out) >= size:
                    break
            else:
                node_addr = child
    return bytes(out[:size])


_DECODERS = {0x10: _lz10, 0x11: _lz11, 0x24: _huffman, 0x28: _huffman, 0x30: _rle}


def decompress(b: bytes, bounded: bool = True, kinds: tuple[int, ...] = (0x10, 0x11)) -> bytes | None:
    """LZ10/LZ11-decompress b, or None if it isn't a plausible stream.

    bounded: b is exactly one stream, so its output must be at least half its length. Carving
    passes False because it hands over the rest of an archive.
    kinds: which compression types to accept. Only LZ is guessed from an unlabelled file by
    default; RLE and Huffman headers are too easy to hit by accident, so they are only tried
    where a container says the entry is compressed.
    """
    if len(b) < 8 or b[0] not in kinds:
        return None
    size = u32(b, 0) >> 8
    if size == 0 and b[0] != 0x11:
        return None
    if size > 32 << 20 or (bounded and size < len(b) // 2):
        return None
    try:
        out = _DECODERS[b[0]](b)
    except (IndexError, struct.error, KeyError):
        return None
    return out if len(out) == size else None


# ---------------------------------------------------------------------------- containers

@dataclass
class Blob:
    path: str   # where it came from, e.g. "data/obj.narc/3~lz" or "mcd.dat@249c04~lz"
    data: bytes

    @property
    def magic(self) -> bytes:
        return self.data[:4]


def nitrofs(rom: bytes) -> dict[str, bytes]:
    """Every named file in the ROM's filesystem (overlays are code and are skipped)."""
    fnt, fat = u32(rom, 0x40), u32(rom, 0x48)
    files: dict[str, bytes] = {}

    def walk(dir_id: int, path: str) -> None:
        base = fnt + 8 * (dir_id & 0xFFF)
        sub = fnt + u32(rom, base)
        fid = u16(rom, base + 4)
        while True:
            t = rom[sub]
            sub += 1
            if t == 0:
                break
            name = rom[sub:sub + (t & 0x7F)].decode("latin1")
            sub += t & 0x7F
            if t & 0x80:
                walk(u16(rom, sub), path + name + "/")
                sub += 2
            else:
                start, end = u32(rom, fat + 8 * fid), u32(rom, fat + 8 * fid + 4)
                files[path + name] = rom[start:end]
                fid += 1

    walk(0xF000, "")
    return files


def narc(b: bytes) -> list[bytes] | None:
    if b[:4] != b"NARC":
        return None
    o = u16(b, 0x0C)
    if b[o:o + 4] != b"BTAF":
        return None
    n = u16(b, o + 8)
    fat = o + 12
    btnf = o + u32(b, o + 4)
    gmif = btnf + u32(b, btnf + 4)
    data = gmif + 8
    return [b[data + u32(b, fat + 8 * i):data + u32(b, fat + 8 * i + 4)] for i in range(n)]


def narc_named(b: bytes) -> list[tuple[str, bytes]] | None:
    """NARC members as (name, bytes), with names from its filename table when it has one."""
    subs = narc(b)
    if subs is None:
        return None
    names = [str(i) for i in range(len(subs))]
    try:
        o = u16(b, 0x0C)
        btnf = o + u32(b, o + 4)
        fnt = btnf + 8
        if u32(b, btnf + 4) > 0x10:
            i, p = u16(b, fnt + 4), fnt + u32(b, fnt)
            while i < len(subs):
                t = b[p]
                p += 1
                if t == 0:
                    break
                nm = b[p:p + (t & 0x7F)].decode("latin1")
                p += t & 0x7F
                if t & 0x80:
                    p += 2
                    continue
                names[i] = nm
                i += 1
    except (IndexError, struct.error):
        pass
    return list(zip(names, subs))


def _fab(b: bytes) -> bytes | None:
    """An entry of Lufia's archive: '$FAB' + type (0 stored, else Nintendo compression) + size."""
    if b[4] == 0x00:
        return b[8:8 + (u32(b, 4) >> 8)]
    return decompress(b[4:], kinds=tuple(_DECODERS))


def mcd_archive(files: dict[str, bytes]) -> dict[str, bytes] | None:
    """Lufia: Curse of the Sinistrals' indexed archive. mcd_compact.bin ('NLCM': sizes and
    offsets) and mcd_path.bin (names) index mcd.dat."""
    comp, path, dat = files.get("mcd_compact.bin"), files.get("mcd_path.bin"), files.get("mcd.dat")
    if not (comp and path and dat) or comp[:4] != b"NLCM":
        return None
    n = u32(comp, 0xC)
    sizes = struct.unpack_from(f"<{n}I", comp, u32(comp, 4))
    offs = struct.unpack_from(f"<{n}I", comp, u32(comp, 8))
    names = struct.unpack_from(f"<{n}I", path, 4)
    out = {}
    for i in range(n):
        if sizes[i]:
            name = path[names[i]:path.index(b"\0", names[i])].decode("latin1")
            out["mcd:" + name] = dat[offs[i]:offs[i] + sizes[i]]
    return out


def ssam_archive(b: bytes) -> list[tuple[str, bytes]] | None:
    """Nostalgia's MASS/*.dat: 'SSAM', u32 count, then per entry u32 offset (from the end of
    the table), u32 size and a 32-byte name."""
    if b[:4] != b"SSAM" or len(b) < 8:
        return None
    n = u32(b, 4)
    base = 8 + 40 * n
    if n == 0 or base > len(b):
        return None
    out = []
    for i in range(n):
        e = 8 + 40 * i
        off, size = u32(b, e), u32(b, e + 4)
        name = b[e + 8:e + 40].split(b"\0")[0].decode("latin1")
        if size and base + off + size <= len(b):
            out.append((name, b[base + off:base + off + size]))
    return out


def table_container(b: bytes) -> list[bytes] | None:
    """Nostalgia's .mmc (and the containers nested in it): u32 count, three zero words, then
    count (offset, size) pairs from the start of the container. Empty slots have size 0."""
    if len(b) < 16:
        return None
    n = u32(b, 0)
    if not 0 < n <= 256 or u32(b, 4) or u32(b, 8) or u32(b, 12) or 16 + 8 * n > len(b):
        return None
    out = []
    for i in range(n):
        off, size = u32(b, 16 + 8 * i), u32(b, 20 + 8 * i)
        if not size:
            continue
        if off < 16 + 8 * n or off + size > len(b):
            return None
        out.append(b[off:off + size])
    return out or None


def nmdp_payload(b: bytes) -> bytes | None:
    """Nostalgia's 'NMDP' wrapper around a standard Nitro file: size at +0x18, offset at +0x1C."""
    if b[:4] != b"NMDP" or len(b) < 0x30:
        return None
    size, off = u32(b, 0x18), u32(b, 0x1C)
    return b[off:off + size] if off + size <= len(b) else None


NITRO = (b"BTX0", b"BMD0", b"BTP0", b"BCA0", b"BMA0", b"BTA0", b"NARC",
         b"RGCN", b"RLCN", b"RECN", b"RCSN", b"RNAN")
_CARVE = re.compile(rb"[\x10\x11][\x00-\xff]{3}\x00(?:" + b"|".join(NITRO) + rb")")
_CARVE_MIN = 1 << 20   # only large unknown files are worth scanning


def files(rom: bytes) -> dict[str, bytes]:
    """name -> bytes for every file in the ROM, containers unwrapped and decompressed.

    Known archive layouts are read through their index. Any other large file that isn't a
    Nitro file is carved for LZ-compressed Nitro files, which is how standard assets are found
    inside archive formats nobody has described yet.
    """
    top = nitrofs(rom)
    out: dict[str, bytes] = {}

    def add(name: str, b: bytes, depth: int = 0) -> None:
        if depth > 10 or not b:
            return
        if b[:4] == b"SSAM":
            subs = ssam_archive(b)
            if subs is not None:
                for nm, s in subs:
                    add(f"{name}/{nm}", s, depth + 1)
                return
        if b[:4] == b"NMDP":
            payload = nmdp_payload(b)
            if payload is not None:
                add(name + "#nmdp", payload, depth + 1)
                return
        if ".mmc" in name and b[:4] not in NITRO:
            parts = table_container(b)
            if parts is not None:
                for i, s in enumerate(parts):
                    add(f"{name}#{i}", s, depth + 1)
                return
        if b[:4] == b"$FAB":
            b = _fab(b)
            if b is None:
                return
        elif b[:4] not in NITRO:
            d = decompress(b)
            if d is not None:
                add(name + "~lz", d, depth + 1)
                return
        if b[:4] == b"NARC":
            subs = narc_named(b)
            if subs is not None:
                for nm, s in subs:
                    add(f"{name}/{nm}", s, depth + 1)
                return
        if len(b) >= _CARVE_MIN and b[:4] not in NITRO and b[:4] != b"SDAT":
            for m in _CARVE.finditer(b):
                d = decompress(b[m.start():m.start() + (8 << 20)], bounded=False)
                if d is not None:
                    add(f"{name}@{m.start():x}~lz", d, depth + 1)
        out[name] = b

    mcd = mcd_archive(top)
    for name, b in top.items():
        if not (mcd is not None and name == "mcd.dat"):
            add(name, b)
    for name, b in (mcd or {}).items():
        add(name, b)
    return out


def blobs(rom: bytes) -> list[Blob]:
    """Every file in the ROM as a Blob (see files())."""
    return [Blob(name, data) for name, data in files(rom).items()]


def game_code(rom: bytes) -> str:
    return rom[0x0C:0x10].decode("latin1")


def game_title(rom: bytes) -> str:
    return rom[0x00:0x0C].rstrip(b"\0").decode("latin1")


# ---------------------------------------------------------------------------- nitro dictionaries

def read_dict(b: bytes, d: int) -> list[tuple[str, bytes]]:
    """A Nitro name dictionary at offset d: [(name, entry bytes)]."""
    n = b[d + 1]
    p = d + u16(b, d + 6)          # the patricia-tree block size counts from the dict header
    entry_size = u16(b, p)
    entries = [b[p + 4 + i * entry_size:p + 4 + (i + 1) * entry_size] for i in range(n)]
    names_at = p + 4 + n * entry_size
    names = [b[names_at + 16 * i:names_at + 16 * i + 16].split(b"\0")[0].decode("latin1", "replace")
             for i in range(n)]
    return list(zip(names, entries))


def blocks(b: bytes) -> dict[bytes, int]:
    """Section offsets of a standard Nitro file (BMD0/BTX0/...), keyed by section magic."""
    if len(b) < 16:
        return {}
    n = u16(b, 0x0E)
    out = {}
    for i in range(n):
        o = u32(b, 0x10 + 4 * i)
        if o + 4 <= len(b):
            out[b[o:o + 4]] = o
    return out
