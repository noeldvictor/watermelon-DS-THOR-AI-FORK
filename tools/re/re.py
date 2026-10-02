"""Reverse-engineering driver: the Thor's emulator (through the debug build's dev server) and Ghidra.

    re.py dev <tool> ['<json args>']        call a dev-server tool: mem_read, mem_write, mem_search,
                                           mem_refine, regs, watch, trace, gdb, rom_info, ...
    re.py pull <label>                      main RAM + ITCM + rom_info of the running game
    re.py extract <rom.nds>                 ROM -> ARM9 (decompressed), autoloads, overlays
    re.py import-ram <CODE> <label>         Ghidra program from a pull: the code as it runs now
    re.py import-rom <CODE>                 Ghidra program from an extract: every overlay, each in
                                           its own address space
    re.py q <CODE> <program> <command...>   query or annotate (NdsQuery.java): decompile, disasm,
                                           xrefs, calls, funcs, find, rename, comment, info;
                                           several commands: separate them with ' ; '
                                           (programs are named after the imported file:
                                           ram_<label>.bin, arm9.bin)

Work files go to tools/re/work/<CODE>/ (gitignored: RAM dumps and ROM pieces are game data).
Ghidra: GHIDRA_INSTALL_DIR, else the toolchains copy. Device: THOR_SERIAL, else the AYN Thor in
`adb devices -l`. The dev server must run (Settings -> General -> Dev server, or re.py starts it).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import nds_rom

HERE = Path(__file__).resolve().parent
WORK = HERE / "work"
SCRIPTS = HERE / "ghidra_scripts"
GHIDRA = Path(os.environ.get("GHIDRA_INSTALL_DIR", r"F:\Projects\SteamPortableTools\toolchains\ghidra_12.0.4_PUBLIC"))
PACKAGE = os.environ.get("THOR_PACKAGE", "app.watermelonthor.dev")
DEV_PORT = 27184
LANGUAGE = "ARM:LE:32:v5t"

# ARM9 I/O registers, so the decompiler names them (GBATEK names)
IO_REGISTERS = {
    0x04000000: "REG_DISPCNT", 0x04000004: "REG_DISPSTAT", 0x04000006: "REG_VCOUNT",
    0x04000008: "REG_BG0CNT", 0x0400000A: "REG_BG1CNT", 0x0400000C: "REG_BG2CNT", 0x0400000E: "REG_BG3CNT",
    0x04000010: "REG_BG0HOFS", 0x04000012: "REG_BG0VOFS", 0x04000014: "REG_BG1HOFS", 0x04000016: "REG_BG1VOFS",
    0x04000018: "REG_BG2HOFS", 0x0400001A: "REG_BG2VOFS", 0x0400001C: "REG_BG3HOFS", 0x0400001E: "REG_BG3VOFS",
    0x04000020: "REG_BG2PA", 0x04000028: "REG_BG2X", 0x04000030: "REG_BG3PA", 0x04000038: "REG_BG3X",
    0x04000040: "REG_WIN0H", 0x04000042: "REG_WIN1H", 0x04000044: "REG_WIN0V", 0x04000046: "REG_WIN1V",
    0x04000048: "REG_WININ", 0x0400004A: "REG_WINOUT", 0x0400004C: "REG_MOSAIC",
    0x04000050: "REG_BLDCNT", 0x04000052: "REG_BLDALPHA", 0x04000054: "REG_BLDY",
    0x04000060: "REG_DISP3DCNT", 0x04000064: "REG_DISPCAPCNT", 0x04000068: "REG_DISP_MMEM_FIFO",
    0x0400006C: "REG_MASTER_BRIGHT",
    0x040000B0: "REG_DMA0SAD", 0x040000B4: "REG_DMA0DAD", 0x040000B8: "REG_DMA0CNT",
    0x040000BC: "REG_DMA1SAD", 0x040000C0: "REG_DMA1DAD", 0x040000C4: "REG_DMA1CNT",
    0x040000C8: "REG_DMA2SAD", 0x040000CC: "REG_DMA2DAD", 0x040000D0: "REG_DMA2CNT",
    0x040000D4: "REG_DMA3SAD", 0x040000D8: "REG_DMA3DAD", 0x040000DC: "REG_DMA3CNT",
    0x040000E0: "REG_DMA0FILL", 0x040000E4: "REG_DMA1FILL", 0x040000E8: "REG_DMA2FILL", 0x040000EC: "REG_DMA3FILL",
    0x04000100: "REG_TM0CNT", 0x04000104: "REG_TM1CNT", 0x04000108: "REG_TM2CNT", 0x0400010C: "REG_TM3CNT",
    0x04000130: "REG_KEYINPUT", 0x04000132: "REG_KEYCNT",
    0x04000180: "REG_IPCSYNC", 0x04000184: "REG_IPCFIFOCNT", 0x04000188: "REG_IPCFIFOSEND",
    0x040001A0: "REG_AUXSPICNT", 0x040001A2: "REG_AUXSPIDATA", 0x040001A4: "REG_ROMCTRL", 0x040001A8: "REG_CARDCMD",
    0x04000204: "REG_EXMEMCNT", 0x04000208: "REG_IME", 0x04000210: "REG_IE", 0x04000214: "REG_IF",
    0x04000240: "REG_VRAMCNT_A", 0x04000241: "REG_VRAMCNT_B", 0x04000242: "REG_VRAMCNT_C", 0x04000243: "REG_VRAMCNT_D",
    0x04000244: "REG_VRAMCNT_E", 0x04000245: "REG_VRAMCNT_F", 0x04000246: "REG_VRAMCNT_G", 0x04000247: "REG_WRAMCNT",
    0x04000248: "REG_VRAMCNT_H", 0x04000249: "REG_VRAMCNT_I",
    0x04000280: "REG_DIVCNT", 0x04000290: "REG_DIV_NUMER", 0x04000298: "REG_DIV_DENOM",
    0x040002A0: "REG_DIV_RESULT", 0x040002A8: "REG_DIVREM_RESULT",
    0x040002B0: "REG_SQRTCNT", 0x040002B4: "REG_SQRT_RESULT", 0x040002B8: "REG_SQRT_PARAM",
    0x04000300: "REG_POSTFLG", 0x04000304: "REG_POWCNT1",
    0x04000320: "REG_RDLINES_COUNT", 0x04000330: "REG_EDGE_COLOR", 0x04000340: "REG_ALPHA_TEST_REF",
    0x04000350: "REG_CLEAR_COLOR", 0x04000354: "REG_CLEAR_DEPTH", 0x04000356: "REG_CLRIMAGE_OFFSET",
    0x04000358: "REG_FOG_COLOR", 0x0400035C: "REG_FOG_OFFSET", 0x04000360: "REG_FOG_TABLE", 0x04000380: "REG_TOON_TABLE",
    0x04000400: "REG_GXFIFO", 0x04000440: "REG_MTX_MODE", 0x04000444: "REG_MTX_PUSH", 0x04000448: "REG_MTX_POP",
    0x0400044C: "REG_MTX_STORE", 0x04000450: "REG_MTX_RESTORE", 0x04000454: "REG_MTX_IDENTITY",
    0x04000458: "REG_MTX_LOAD_4x4", 0x0400045C: "REG_MTX_LOAD_4x3", 0x04000460: "REG_MTX_MULT_4x4",
    0x04000464: "REG_MTX_MULT_4x3", 0x04000468: "REG_MTX_MULT_3x3", 0x0400046C: "REG_MTX_SCALE",
    0x04000470: "REG_MTX_TRANS", 0x04000480: "REG_COLOR", 0x04000484: "REG_NORMAL", 0x04000488: "REG_TEXCOORD",
    0x0400048C: "REG_VTX_16", 0x04000490: "REG_VTX_10", 0x04000494: "REG_VTX_XY", 0x04000498: "REG_VTX_XZ",
    0x0400049C: "REG_VTX_YZ", 0x040004A0: "REG_VTX_DIFF", 0x040004A4: "REG_POLYGON_ATTR",
    0x040004A8: "REG_TEXIMAGE_PARAM", 0x040004AC: "REG_PLTT_BASE", 0x040004C0: "REG_DIF_AMB",
    0x040004C4: "REG_SPE_EMI", 0x040004C8: "REG_LIGHT_VECTOR", 0x040004CC: "REG_LIGHT_COLOR",
    0x040004D0: "REG_SHININESS", 0x04000500: "REG_BEGIN_VTXS", 0x04000504: "REG_END_VTXS",
    0x04000540: "REG_SWAP_BUFFERS", 0x04000580: "REG_VIEWPORT", 0x040005C0: "REG_BOX_TEST",
    0x040005C4: "REG_POS_TEST", 0x040005C8: "REG_VEC_TEST", 0x04000600: "REG_GXSTAT", 0x04000604: "REG_RAM_COUNT",
    0x04000610: "REG_DISP_1DOT_DEPTH", 0x04000620: "REG_POS_RESULT", 0x04000630: "REG_VEC_RESULT",
    0x04000640: "REG_CLIPMTX_RESULT", 0x04000680: "REG_VECMTX_RESULT",
    0x04001000: "REG_DISPCNT_SUB", 0x04001008: "REG_BG0CNT_SUB", 0x0400100A: "REG_BG1CNT_SUB",
    0x0400100C: "REG_BG2CNT_SUB", 0x0400100E: "REG_BG3CNT_SUB", 0x04001050: "REG_BLDCNT_SUB",
    0x04001052: "REG_BLDALPHA_SUB", 0x04001054: "REG_BLDY_SUB", 0x0400106C: "REG_MASTER_BRIGHT_SUB",
    0x04100000: "REG_IPCFIFORECV", 0x04100010: "REG_GCDATAIN",
}

# memory the code addresses but that no dump holds: uninitialized blocks, so references resolve
HARDWARE_BLOCKS = [
    ("shared_wram", 0x03000000, 0x8000, False),
    ("io", 0x04000000, 0x2000, True),
    ("io_fifo", 0x04100000, 0x20, True),
    ("palette", 0x05000000, 0x800, False),
    ("vram_bg_a", 0x06000000, 0x80000, False),
    ("vram_bg_b", 0x06200000, 0x20000, False),
    ("vram_obj_a", 0x06400000, 0x40000, False),
    ("vram_obj_b", 0x06600000, 0x20000, False),
    ("vram_lcdc", 0x06800000, 0xA4000, False),
    ("oam", 0x07000000, 0x800, False),
    ("bios", 0xFFFF0000, 0x8000, False),
]


# ---- device -------------------------------------------------------------------------------------

def serial():
    if os.environ.get("THOR_SERIAL"):
        return os.environ["THOR_SERIAL"]
    lines = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True).stdout.splitlines()
    devices = [l.split()[0] for l in lines[1:] if l.strip() and " device " in l]
    thor = [l.split()[0] for l in lines[1:] if "AYN_Thor" in l]
    if thor:
        return thor[0]
    if devices:
        return devices[0]
    sys.exit("no adb device")


def adb(*args, **kwargs):
    return subprocess.run(["adb", "-s", serial(), *args], **kwargs)


def dev(tool, arguments=None, timeout=300):
    """POST /tool/<tool> on the app's dev server (forwarded to the PC), starting it if needed."""
    body = json.dumps(arguments or {}).encode()
    for attempt in range(2):
        adb("forward", f"tcp:{DEV_PORT}", f"tcp:{DEV_PORT}", capture_output=True)
        request = urllib.request.Request(f"http://127.0.0.1:{DEV_PORT}/tool/{tool}", data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            # tool errors come back as 400 with a JSON body
            return json.loads(e.read() or b'{"error": "HTTP %d"}' % e.code)
        except (urllib.error.URLError, ConnectionError):
            if attempt:
                raise
            adb("shell", "am", "broadcast", "-f", "32", "-p", PACKAGE, "-a", f"{PACKAGE}.START_DEV_SERVER", capture_output=True)
            time.sleep(1.5)


def pull_file(remote_name, local):
    local.parent.mkdir(parents=True, exist_ok=True)
    with open(local, "wb") as f:
        result = adb("exec-out", "run-as", PACKAGE, "cat", f"files/re/{remote_name}", stdout=f)
    if result.returncode != 0 or local.stat().st_size == 0:
        sys.exit(f"pull of {remote_name} failed")


def require(result):
    if "error" in result:
        sys.exit(f"device: {result['error']}")
    return result


def pull(label):
    info = require(dev("rom_info"))
    code = info["gameCode"]
    out = WORK / code / "pull" / label
    # pause so the three dumps show one moment
    dev("pause")
    try:
        for region in ("main", "itcm", "dtcm"):
            dump = require(dev("mem_dump", {"region": region, "name": f"{label}_{region}"}))
            pull_file(Path(dump["file"]).name, out / (f"ram_{label}.bin" if region == "main" else f"{region}.bin"))
            if region == "dtcm":
                info["dtcmBase"] = dump["address"]
        info["registers"] = dev("regs")
    finally:
        dev("resume")
    info["pulledAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (out / "rom_info.json").write_text(json.dumps(info, indent=1))
    loaded = [o["id"] for o in info.get("arm9Overlays", []) if o.get("loaded") is True]
    print(f"{code} {info['title']}: main RAM + ITCM + DTCM ({info['dtcmBase']}) -> {out}; "
          f"overlays in RAM: {loaded or 'none'}")
    return out


# ---- Ghidra -------------------------------------------------------------------------------------

def headless(code, *args):
    project = WORK / code / "ghidra"
    project.mkdir(parents=True, exist_ok=True)
    bat = GHIDRA / "support" / ("analyzeHeadless.bat" if os.name == "nt" else "analyzeHeadless")
    log = project / "headless.log"
    command = [str(bat), str(project), code, *args, "-scriptPath", str(SCRIPTS), "-log", str(log)]
    result = subprocess.run(command, capture_output=True, text=True)
    # a script that fails to compile or throws doesn't fail the run: the analysis goes on without it
    script_error = "SCRIPT ERROR" in result.stdout or "SCRIPT ERROR" in result.stderr
    if result.returncode != 0 or script_error:
        lines = (result.stdout + result.stderr).splitlines()
        sys.stderr.write("\n".join(l for l in lines if "ERROR" in l or "error:" in l or "Exception" in l)[-6000:] + "\n")
        sys.exit(f"analyzeHeadless {'script error' if script_error else f'failed ({result.returncode})'}; log: {log}")
    return result


def hardware_plan():
    blocks = [dict(name=n, address=f"{a:#x}", size=f"{s:#x}", volatile=v) for n, a, s, v in HARDWARE_BLOCKS]
    labels = [dict(address=f"{a:#x}", name=n) for a, n in IO_REGISTERS.items()]
    return blocks, labels


def import_ram(code, label):
    folder = WORK / code / "pull" / label
    info = json.loads((folder / "rom_info.json").read_text())
    uninitialized, labels = hardware_plan()
    for ov in info.get("arm9Overlays", []):
        if ov.get("loaded") is True:
            labels.append(dict(address=ov["ram"], comment=f"overlay {ov['id']} (in RAM at the pull), "
                                                          f"size {ov['size']}, ROM {ov.get('romOffset')}"))
    entry = info["arm9"]["entry"]
    blocks = [dict(name="main_ram", address="0x02000000", primary=True),
              dict(name="itcm", address="0x01FF8000", file="itcm.bin")]
    if (folder / "dtcm.bin").exists() and "dtcmBase" in info:
        blocks.append(dict(name="dtcm", address=info["dtcmBase"], file="dtcm.bin"))
    plan = dict(
        blocks=blocks,
        uninitialized=uninitialized,
        labels=labels,
        entries=[dict(address=entry, name="_start")],
        options={"ARM Aggressive Instruction Finder": "true"},
    )
    plan_file = folder / "plan.json"
    plan_file.write_text(json.dumps(plan, indent=1))
    print(f"analyzing ram_{label}.bin (a few minutes for 4 MB)...")
    headless(code, "-import", str(folder / f"ram_{label}.bin"), "-overwrite",
             "-loader", "BinaryLoader", "-loader-baseAddr", "0x02000000", "-processor", LANGUAGE,
             "-preScript", "NdsImport.java", str(plan_file))
    print(f"program ram_{label}.bin in {WORK / code / 'ghidra'}")


def import_rom(code):
    folder = WORK / code / "rom"
    layout = json.loads((folder / "layout.json").read_text())
    uninitialized, labels = hardware_plan()
    blocks = [dict(name="arm9", address=f"{layout['arm9']['ram']:#x}", primary=True)]
    entries = [dict(address=f"{layout['arm9']['entry']:#x}", name="_start")]
    for block in layout["autoload"]:
        blocks.append(dict(name=f"autoload_{block['ram']:08X}", address=f"{block['ram']:#x}", file=block["file"]))
    for ov in layout["overlays"]:
        blocks.append(dict(name=f"ov_{ov['id']:03d}", address=f"{ov['ram']:#x}", file=ov["file"], overlay=True))
    plan = dict(blocks=blocks, uninitialized=uninitialized, labels=labels, entries=entries,
                options={"ARM Aggressive Instruction Finder": "true"})
    plan_file = folder / "plan.json"
    plan_file.write_text(json.dumps(plan, indent=1))
    print(f"analyzing arm9.bin + {len(layout['overlays'])} overlays...")
    headless(code, "-import", str(folder / "arm9.bin"), "-overwrite",
             "-loader", "BinaryLoader", "-loader-baseAddr", f"{layout['arm9']['ram']:#x}", "-processor", LANGUAGE,
             "-preScript", "NdsImport.java", str(plan_file))
    print(f"program arm9.bin in {WORK / code / 'ghidra'} (overlays as ov_NNN:: address spaces)")


def query(code, program, words):
    commands = [[]]
    for word in words:
        if word == ";":
            commands.append([])
        else:
            commands[-1].append(word)
    with tempfile.TemporaryDirectory() as tmp:
        command_file = Path(tmp) / "commands.txt"
        command_file.write_text("\n".join(" ".join(c) for c in commands if c) + "\n")
        out_file = Path(tmp) / "out.txt"
        headless(code, "-process", program, "-noanalysis",
                 "-postScript", "NdsQuery.java", str(out_file), f"@{command_file}")
        return out_file.read_text() if out_file.exists() else "(no output: see the headless log)"


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    command, args = argv[0], argv[1:]
    if command == "dev":
        arguments = json.loads(args[1]) if len(args) > 1 else {}
        print(json.dumps(dev(args[0], arguments), indent=1))
    elif command == "pull":
        pull(args[0])
    elif command == "extract":
        code = nds_rom.Rom(Path(args[0]).read_bytes()[:0x200]).game_code
        layout = nds_rom.extract(args[0], WORK / code / "rom")
        print(f"{code}: ARM9 {layout['arm9']['size']:#x} bytes, {len(layout['autoload'])} autoloads, "
              f"{len(layout['overlays'])} overlays -> {WORK / code / 'rom'}")
    elif command == "import-ram":
        import_ram(args[0], args[1])
    elif command == "import-rom":
        import_rom(args[0])
    elif command == "q":
        print(query(args[0], args[1], args[2:]))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
