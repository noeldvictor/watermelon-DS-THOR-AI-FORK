# Reverse-engineering toolkit

Find and change the code behind what a DS game does, with the game running on the device. Built for
game-specific work: widescreen and other enhancement codes, HD hooks (text drawn into BG layers),
HLE patches. The skill `.claude/skills/ghidra/SKILL.md` is the short how-to; this file is the design.

## Pieces

| Where | What |
| --- | --- |
| `melonDS-android-lib/src/DebugHooks.*` | memory watchpoints (CPU loads/stores, TCM included) and the BL/BLX call trace; one relaxed atomic load per access when idle |
| `app/src/main/cpp/MelonInstanceDebugTools.cpp` | memory read/write as the CPU sees it, registers, the JIT/interpreter switch, the GDB stub, ROM reads |
| `app/src/main/cpp/DebugToolsJNI.cpp` | `MelonEmulator.debug*` (debug builds only; release builds answer "no game") |
| `app/src/debug/.../ReTools.kt` | the dev-server (MCP) tools; `DevTools.kt` registers them |
| `tools/re/re.py` | PC driver: dev-server calls, `pull`, Ghidra import and queries |
| `tools/re/nds_rom.py` | ROM -> ARM9 (decompressed), autoload blocks (ITCM/DTCM), ARM7, overlays, `layout.json` |
| `tools/re/ghidra_scripts/` | `NdsImport.java` (pre-analysis setup from a plan), `NdsQuery.java` (queries, annotations) |

## Device tools (dev server, debug builds)

`mem_read`, `mem_write`, `mem_dump`, `mem_search`, `mem_refine`, `regs`, `debug_mode`, `watch`, `trace`,
`gdb`, `rom_info`, `rom_dump`. Through MCP (`watermelon-thor`), curl (`POST /tool/<name>`) or
`python re.py dev <tool> '<json>'`.

- **Memory** reads skip I/O registers (reads there have side effects); writes go through the bus, so a
  write into code drops stale JIT blocks. While the game is paused (the dev server's `pause`) a write
  applies at once; otherwise at the next frame start, on the emulator thread.
- **Watchpoints and call traces** hook the interpreter: `ARMv5/ARMv4::DataRead*/DataWrite*` (CP15.cpp,
  ARM.cpp) and the BL/BLX handlers (ARMInterpreter_Branch.cpp). The JIT compiles memory accesses and
  calls into native code, so starting either switches the CPUs to the interpreter at a frame boundary
  (`SetJITArgs(nullopt)` + `FillPipeline()`, what a save state made under the JIT does for an
  interpreter). `debug_mode on=false` goes back (`JIT.Reset()` + the saved JIT settings; R15 is already
  where the JIT expects it). DMA transfers are not CPU accesses and are not seen. A watch with a
  `value` keeps only accesses of that value, over all memory when no address is given ("who writes
  0x1555 anywhere"); every event carries pc, lr, r0-r3 and sp (the object pointer is usually in r0).
- **GDB stub**: melonDS's own (`src/debug/Gdb*`), compiled into debug builds only (`ENABLE_GDBSTUB`
  follows `MELONDS_ANDROID_DEBUG_BUILD`; the core exports `GDBSTUB_ENABLED` because it changes the ARM
  class layout). It listens on 127.0.0.1 only (patched from INADDR_ANY: it can read and write all
  memory); reach it with `adb forward`. Runs on the interpreter (`RunFrame<InterpreterGDB>`).
- **ROM**: header, overlay tables, which overlays sit in RAM now (the first 256 bytes of each, BLZ
  decompressed if needed, against RAM), and a full dump to `files/re/` for pulling.

Verified 2026-10-01 on the Thor (Spirit Tracks title screen): the live switch to the interpreter and
back at 60 fps; a DISPCNT write watch named the two writers; gdb-multiarch attached, read registers,
disassembled and detached; the stub listened on 127.0.0.1:3333/3334 only and closed with debug mode.
The RAM dump matched `nds_rom.py`'s decompressed ARM9 (99.97%; the rest is runtime data) and ITCM
autoload exactly, and the device's and the PC's "overlays in RAM" lists agreed.

## PC side

```
python re.py pull <label>                 # paused: main RAM, ITCM, DTCM, rom_info (+ registers)
python re.py import-ram <CODE> <label>    # Ghidra program ram_<label>.bin
python re.py extract <rom.nds>            # work/<CODE>/rom/
python re.py import-rom <CODE>            # Ghidra program arm9.bin, overlays as ov_NNN:: spaces
python re.py q <CODE> <program> decompile <addr> ; xrefs <addr> ; find "55 15 00 00"
```

The import plan (`plan.json` next to the inputs) maps the extra blocks (ITCM, DTCM, autoloads,
overlays), uninitialized hardware blocks with the ARM9 I/O register names (so the decompiler prints
`REG_DISPCNT`), the entry point, and turns on the ARM aggressive instruction finder (overlay code has
no static callers). Work files live in `work/<CODE>/` (gitignored).

Ghidra notes: 12.0 dropped `FlatProgramAPI.toAddress(String)` and deprecated the integer comment
types (use `setPlateComment`); `find(String)` is final in the flat API. A script that fails to compile
shows only "class could not be found" in the log and the analysis continues without it, so `re.py`
stops on any SCRIPT ERROR. Compile-check scripts with javac against the Ghidra jars before a long run.

## Ideas taken from ndsrecomp (not its code: PolyForm Noncommercial)

- Work from the code as it runs (RAM dumps per scene) rather than a static whole-ROM lift.
- Name SDK functions by signature so game code reads better (TODO: NitroSDK signatures for Ghidra's
  Function ID).
- Use the emulator as the oracle: change one thing (a write, a hook), compare frames
  (`tools/frame_compare`).
