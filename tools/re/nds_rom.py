"""Nintendo DS ROM layout for reverse engineering: header, ARM9 (decompressed, with its autoload
blocks for ITCM/DTCM), ARM7 and overlays, as files plus a layout.json that re.py turns into a Ghidra
program.

    python nds_rom.py extract game.nds out_dir
"""
import argparse
import json
import struct
import sys
from pathlib import Path

NITROCODE = b"\x21\x06\xC0\xDE\xDE\xC0\x06\x21"


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def blz_decompress(data):
    """Nintendo's backward LZ (compressed ARM9 static code and overlays). The footer at the end gives
    the compressed span and the extra length; decoding runs from the end towards the start."""
    n = len(data)
    if n < 8:
        return bytes(data)
    extra = u32(data, n - 4)
    if extra == 0:
        return bytes(data)
    header_length = data[n - 5]
    encoded_length = u32(data, n - 8) & 0xFFFFFF
    out = bytearray(data) + bytearray(extra)
    src = n - header_length
    dst = n + extra
    end = n - encoded_length
    while src > end:
        src -= 1
        flags = out[src]
        for bit in range(7, -1, -1):
            if src <= end:
                break
            if not flags & (1 << bit):
                src -= 1
                dst -= 1
                out[dst] = out[src]
            else:
                src -= 2
                token = (out[src + 1] << 8) | out[src]
                length = (token >> 12) + 3
                distance = (token & 0xFFF) + 3
                for _ in range(length):
                    dst -= 1
                    out[dst] = out[dst + distance]
    return bytes(out)


class Rom:
    def __init__(self, data):
        self.data = data
        h = data[:0x200]
        self.title = h[0:12].rstrip(b"\0").decode("ascii", "replace")
        self.game_code = h[12:16].decode("ascii", "replace")
        self.maker = h[16:18].decode("ascii", "replace")
        self.arm9 = dict(rom=u32(h, 0x20), entry=u32(h, 0x24), ram=u32(h, 0x28), size=u32(h, 0x2C))
        self.arm7 = dict(rom=u32(h, 0x30), entry=u32(h, 0x34), ram=u32(h, 0x38), size=u32(h, 0x3C))
        self.fat = (u32(h, 0x48), u32(h, 0x4C))
        self.arm9_ovt = (u32(h, 0x50), u32(h, 0x54))
        self.arm7_ovt = (u32(h, 0x58), u32(h, 0x5C))

    def file(self, file_id):
        start, end = struct.unpack_from("<II", self.data, self.fat[0] + file_id * 8)
        return self.data[start:end], start

    @staticmethod
    def code_settings_offset(arm9):
        """The SDK's module parameters, found by their closing magic words. (The footer after ARM9 in
        the ROM starts with the same magic, but its offset field doesn't point at them: Spirit Tracks
        says 0xAD8, the parameters sit at 0xB64.)"""
        index = arm9.find(NITROCODE, 0, 0x8000)
        return index - 0x1C if index >= 0x1C else None

    def arm9_code(self):
        """The ARM9 binary as the CPU sees it after boot: static code decompressed, and the autoload
        blocks (ITCM, DTCM and others) split out to their own addresses."""
        raw = self.data[self.arm9["rom"]:self.arm9["rom"] + self.arm9["size"]]
        ram = self.arm9["ram"]
        settings = self.code_settings_offset(raw)
        if settings is None:
            return raw, [], None
        compressed_end = u32(raw, settings + 0x14)
        code = raw
        if compressed_end:
            split = compressed_end - ram
            code = blz_decompress(raw[:split]) + raw[split:]
        list_start, list_end, data_start = (u32(code, settings + o) - ram for o in (0, 4, 8))
        blocks = []
        cursor = data_start
        for entry in range(list_start, list_end, 12):
            address, size, bss = struct.unpack_from("<III", code, entry)
            blocks.append(dict(ram=address, size=size, bss=bss, data=code[cursor:cursor + size]))
            cursor += size
        info = dict(settings_offset=settings, compressed=bool(compressed_end),
                    sdk_version=u32(code, settings + 0x18), static_end=ram + data_start,
                    bss=(u32(code, settings + 0x0C), u32(code, settings + 0x10)))
        return code[:data_start], blocks, info

    def overlays(self, arm7=False):
        offset, size = self.arm7_ovt if arm7 else self.arm9_ovt
        result = []
        for b in range(offset, offset + size, 32):
            ov_id, ram, ram_size, bss, sinit_start, sinit_end, file_id, flags = struct.unpack_from("<8I", self.data, b)
            data, rom_offset = self.file(file_id)
            compressed = bool((flags >> 24) & 1)
            if compressed:
                data = blz_decompress(data)
            result.append(dict(id=ov_id, ram=ram, size=ram_size, bss=bss, sinit=(sinit_start, sinit_end),
                               file_id=file_id, rom_offset=rom_offset, compressed=compressed, data=data))
        return result


def extract(rom_path, out_dir):
    rom = Rom(Path(rom_path).read_bytes())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    layout = dict(title=rom.title, game_code=rom.game_code, maker=rom.maker, rom=str(Path(rom_path).resolve()),
                  arm9=dict(entry=rom.arm9["entry"], ram=rom.arm9["ram"]), autoload=[], overlays=[], arm7_overlays=[])

    code, blocks, info = rom.arm9_code()
    (out / "arm9.bin").write_bytes(code)
    layout["arm9"].update(file="arm9.bin", size=len(code), rom_offset=rom.arm9["rom"])
    if info:
        layout["arm9"].update(compressed=info["compressed"], sdk_version=info["sdk_version"],
                              bss=list(info["bss"]), settings_offset=info["settings_offset"])
    for block in blocks:
        name = f"autoload_{block['ram']:08X}.bin"
        (out / name).write_bytes(block["data"])
        layout["autoload"].append(dict(file=name, ram=block["ram"], size=block["size"], bss=block["bss"]))

    arm7 = rom.data[rom.arm7["rom"]:rom.arm7["rom"] + rom.arm7["size"]]
    (out / "arm7.bin").write_bytes(arm7)
    layout["arm7"] = dict(file="arm7.bin", entry=rom.arm7["entry"], ram=rom.arm7["ram"], size=len(arm7))

    for key, arm7_side in (("overlays", False), ("arm7_overlays", True)):
        for ov in rom.overlays(arm7=arm7_side):
            name = f"{'ov7' if arm7_side else 'ov'}_{ov['id']:03d}.bin"
            (out / name).write_bytes(ov["data"])
            layout[key].append(dict(id=ov["id"], file=name, ram=ov["ram"], size=ov["size"], bss=ov["bss"],
                                    sinit=list(ov["sinit"]), file_id=ov["file_id"], rom_offset=ov["rom_offset"],
                                    compressed=ov["compressed"]))

    (out / "layout.json").write_text(json.dumps(layout, indent=1))
    return layout


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("extract", help="split a ROM into ARM9/ARM7/autoload/overlay binaries + layout.json")
    p.add_argument("rom")
    p.add_argument("out")
    args = parser.parse_args()
    if args.command == "extract":
        layout = extract(args.rom, args.out)
        print(f"{layout['game_code']} {layout['title']}: ARM9 {layout['arm9']['size']:#x} bytes at "
              f"{layout['arm9']['ram']:#010x}, {len(layout['autoload'])} autoload blocks, "
              f"{len(layout['overlays'])} ARM9 overlays, {len(layout['arm7_overlays'])} ARM7 overlays -> {args.out}")


if __name__ == "__main__":
    sys.exit(main())
