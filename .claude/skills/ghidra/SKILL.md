---
name: ghidra
description: Reverse engineer a DS game running on the AYN Thor - find the code behind a value, a scene or an effect with the emulator's debug tools (memory search, watchpoints, call traces, GDB stub) and Ghidra (headless import of the live RAM or the ROM, decompile, xrefs, byte search). Use for game-specific fixes and enhancements - widescreen or cheat codes, HLE hooks, text/HD hooks, finding a routine or a variable.
---

# Reverse engineering a DS game

Two halves that feed each other:
- **Device** (debug build): the app's dev server tools `mem_read`, `mem_write`, `mem_dump`, `mem_search`,
  `mem_refine`, `regs`, `debug_mode`, `watch`, `trace`, `gdb`, `rom_info`, `rom_dump` (code:
  `app/src/debug/.../ReTools.kt`; MCP server `watermelon-thor`, or `tools/re/re.py dev <tool> '<json>'`).
- **PC**: `tools/re/re.py` drives Ghidra 12 headless (`F:\Projects\SteamPortableTools\toolchains\ghidra_12.0.4_PUBLIC`,
  or `GHIDRA_INSTALL_DIR`) with `tools/re/ghidra_scripts/NdsImport.java` / `NdsQuery.java`.

Read `tools/re/README.md` for details. The Thor is shared: check the foreground app before driving it
(workspace AGENTS.md), close the emulator when done, and never leave `debug_mode` on.

## 1. Get the code into Ghidra (once per game / scene)

```
python tools/re/re.py dev launch '{"query":"Spirit Tracks","first":true}'   # ROM list must be in front
python tools/re/re.py pull title          # paused dump: main RAM + ITCM + DTCM + overlay table
python tools/re/re.py import-ram BKIE title   # program ram_title.bin, a few minutes of analysis
```
The RAM dump is the code as it runs (ARM9 decompressed, the overlays loaded for that scene).
For every overlay at once: `re.py extract <rom.nds>` then `re.py import-rom <CODE>` (program
`arm9.bin`, overlays in `ov_NNN::` address spaces). No local ROM? `rom_dump` writes the Thor's copy to
`files/re/<CODE>.nds` (pull command in the result). ROMs and dumps never go in git (`tools/re/work/`
is ignored).

## 2. Find the code

Pick by what you know:
- **A value on screen** (HP, a timer, a flag): `mem_search` value=... then change it in game and
  `mem_refine` mode=equal value=... (or snapshot=true + changed/unchanged/increased) until a few
  addresses are left. Confirm with `mem_write` (pause first; it applies at once).
- **Who writes/reads an address**: `watch` action=start address=... (writes default, reads=true for
  readers), play a moment, `watch` action=read: grouped by instruction (pc, lr, values, r0-r3/sp).
  DMA writes are invisible (DMA isn't the CPU); I/O registers (0x04xxxxxx) work.
- **Who writes a value anywhere**: `watch` action=start value=0x1555 (no address = all memory).
- **A heap object that moves between sessions**: don't ship its address. Search RAM for pointers to
  it (`mem_search` value=<object address>); one in ARM9 static memory (below the overlays) is stable.
  Ship an AR pointer code (`B` load offset, `DC` add, guarded by `4`/`3` range and `5` equality
  checks; with address 0 a conditional tests the offset). Example: Star Fox Command in
  `app/src/main/assets/cheats/code_fixes.txt`.
- **Break and inspect**: `gdb` (below) with `break *ADDR if $r3 == X` catches one caller among many.
- **Which functions run in a scene**: `trace` action=start, `trace` read (discard), do the thing (open
  a dialogue), `trace` read again; diff the `functions` lists of a read with and without the thing.
  `target_start`/`target_end` narrow it to one overlay or region.
- **Step through it**: `gdb` action=start, `adb -s <serial> forward tcp:3333 tcp:3333`,
  `C:\msys64\ucrt64\bin\gdb-multiarch.exe -ex "set architecture armv5te" -ex "target remote :3333"`.
  Breakpoints, `x/8i $pc`, `info registers`. A halted CPU freezes the game until `continue`/`detach`.

`watch`, `trace` and `gdb` run the CPUs on the interpreter (the JIT compiles memory accesses and calls
into native code the hooks never see). It switches on by itself; turn it off with `debug_mode
on=false` when done (it also stops the GDB stub). Lufia-class games still reach ~60 fps on the
interpreter; heavy 3D scenes run slower.

## 3. Read it in Ghidra

```
python tools/re/re.py q BKIE ram_title.bin decompile 0x020C591C ; xrefs 0x020C5950 ; calls 0x0201B8C4
python tools/re/re.py q BKIE ram_title.bin find "55 15 00 00"     # a constant (here 0x1555) as bytes
python tools/re/re.py q BKIE ram_title.bin rename 0x020C591C ui_SetDispcnt ; comment 0x020C591C toggles bit 15
```
Commands: decompile, disasm, xrefs (literal-pool words are followed to the loads), calls, funcs,
find (?? = any byte), rename, comment, info. Several per run with ` ; ` (each run costs ~10 s of
Ghidra startup). Names and comments are saved in the project, so findings accumulate.

DS specifics:
- ARM9 code is ARM or Thumb; bit 0 of an address/target = Thumb (call-trace targets and `lr` show it).
- Constants live in literal pools after the function: search for the 4 bytes, then xrefs.
- ITCM is at 0x01FF8000 (hot code: memcpy, math, IRQ), DTCM wherever `regs` says (stack, small vars).
- Overlays share addresses (several at 0x020B6500 in Spirit Tracks): in the RAM program the one loaded
  at the pull is there; `rom_info` lists which are loaded now.
- Fixed point is everywhere: fx32 = 20.12 (0x1000 = 1.0), fx16 = 4.12. Aspect ratios: 4:3 = 0x1555,
  16:9 = 0x1C72, 16:10 = 0x199A.

## 4. Ship it

- A **cheat / widescreen code** (AR codes: `0XXXXXXX YYYYYYYY` 32-bit write, `1`/`2` 16/8-bit, `5`/`9`
  conditionals, `D2000000 00000000` end): test with `mem_write` first; prefer patching the instruction
  or literal in code over a RAM value that the game reloads.
- A **runtime hook** (HD text, HLE patch): note the function, its arguments and the overlay it lives in
  (an overlay must be loaded before patching it) in AGENTS.md / the game's notes.
- Record the finding where the next session will look: function names in the Ghidra project via
  `rename`/`comment`, and a line in the workspace AGENTS.md.
