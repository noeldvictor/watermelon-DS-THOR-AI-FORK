---
name: hd-remaster
description: Build, verify and install an AI-upscaled HD texture pack for a DS game straight from its ROM with tools/hd_remaster (no playthrough needed). Use when the user wants to remaster/upscale a game, make or update an HD texture pack, check pack coverage against in-game dumps, or push a pack to the AYN Thor.
---

# HD remaster a DS game

The tool lives in `tools/hd_remaster/` (read its README.md for the design). All commands run
with the venv's python from that directory: `.venv\Scripts\python.exe hd_remaster.py ...`.
Build steps and adb recipes for this machine are in the workspace AGENTS.md.

## 0. Setup check

- `tools/hd_remaster/.venv` must exist and have CUDA torch:
  `.venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available())"` must print True.
- If not, run `powershell -ExecutionPolicy Bypass -File tools\hd_remaster\setup.ps1`.
  Trap: installing spandrel before a matching CUDA torch+torchvision pair pulls CPU-only torch
  from PyPI. setup.ps1 installs them in the right order; `pip install --force-reinstall --no-deps
  torch==X torchvision==Y --index-url https://download.pytorch.org/whl/cu130` repairs it.
- The model is `models/4x-UltraSharp.safetensors` (CC BY-NC-SA: personal use only). Prefer
  .safetensors models; .pth files are pickles that can run code when loaded.

## Recipes

`games/<GAMECODE>/recipe.json` holds what was verified for a game (ROM checksums, scale, model
per category, 2D rules, baseline key counts, verification notes); see `games/README.md`.
`extract` applies it automatically and prints whether the ROM matches. When a game's pack is
verified, write or update its recipe and README - that is what lets other users reproduce it
with `remaster.ps1 game.nds -Push`. Recipes never contain game images.

## 1. Extract

`hd_remaster.py extract <rom.nds>` writes `work/<GAMECODE>/native/textures/*.png` (3D, named by
pack key), `native/assets2d/*.png` (whole sprite cells and BG screens, each with its crop list)
and `work/<GAMECODE>/manifest.jsonl`. Report the counts it prints: texture blocks, textures,
keys, the pairing split (material / name / block / none), and the 2D line (cells/screens,
standalone sprites, sprite keys, BG tile keys). Zero texture blocks means no Nitro TEX0 data
(custom format or 2D-only); zero cells/screens means no NCER/NSCR data - say so.

Game-specific 2D load rules go in `twod.PROFILES[<GAMECODE>]` (see the BSDE entry: portraits
uploaded with indices +48 into a composed 256-colour palette). Only add one when verify shows
sprites whose tiles match but palettes don't, or tiles that match only after a transform.

## 2. Verify (whenever in-game dumps exist)

`hd_remaster.py verify work/<CODE> --dumps <dumpdir>/textures --sprites <dumpdir>/sprites`
compares against dump folders (`manifest.jsonl` + PNGs, pulled from the device's
`files/texturedumps/<CODE>/`). It must report 0 pixel mismatches for both; any mismatch means
the decoder or key scheme drifted from `melonDS-android-lib/src/GPU3D_Texcache.*`,
`GPU2D_HDPack.cpp` or `HDTexPack.cpp` - fix that before shipping a pack. Coverage below 100% is
normal: anything the game builds at runtime (text, captures, composited parts) isn't in the ROM.
Baseline, Lufia (BSDE): textures 181/223 reproduced, 181/181 pixel-identical; sprites 324/587,
324/324 (the misses are 237 dialogue-text sprites and 26 captured-scene bitmaps). BG tile keys
are not yet validated against dumps.

## 3. Upscale

`hd_remaster.py upscale work/<CODE> [--scale 4|2]`. Long-running: run it in the background. It
resumes, so an interrupted run just continues. Spot-check a few results side by side with the
native image at full resolution (thumbnails hide the difference).

## 4. Build and push

- `hd_remaster.py build work/<CODE>` then `hd_remaster.py push packs/<CODE>`.
- `build --native` gives a 1x pack: on the device it must render exactly like no pack at all.
  That is the end-to-end check that keys and pixels are right.
- `push` needs the debuggable (`.dev`) build; it copies via /data/local/tmp and run-as, and moves
  an existing pack for the game aside as `<CODE>.bak-<timestamp>`, never deleting it.

## 5. Test on the Thor

- Follow the SHARED DEVICE RULE in AGENTS.md: the Thor is shared, check the foreground app
  first and never fight another session for it. Pass `-s <serial>` (find it with
  `adb devices -l`, model:AYN_Thor).
- Launch the game, then check logcat (Warn level; release builds drop Info) for
  `HDTexPack: indexed N entries ... (scale 4x)` and the once-a-second
  `HDTexPack[Stats]: textures h/l sprites h/l bg h/l (hits/lookups) 2dInstances=N`. Pair it
  with `VulkanOutput[Stats]`: `overlays` must follow `2dInstances` (x frames), or 2D
  replacements are being dropped between the latch and the compositor. Capture BOTH displays
  (bottom: `--display-id 4630946482288158084`).
- Test with the per-layer filters off (`video_hd_texture_filter`, `video_obj_sprite_filter`,
  `video_bg_layer_filter` = 0) so the pack's effect is what you see.
- While a colour effect (BLDCNT fade/blend) targets a layer, that layer deliberately keeps its
  native art; HD returns when the effect ends. Sample frames across a transition.
- Packs only apply under the Vulkan (default) or Compute renderer.
- Force-stop the app when done: `adb -s <dev> shell am force-stop me.magnum.melondualds.dev`.
