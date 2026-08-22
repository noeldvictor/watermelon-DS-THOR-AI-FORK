# Watermelon Thor

An experimental dual-screen Android fork of [melonDS](https://melonds.kuribo64.net/) tuned for the
AYN Thor, with HD texture pack support and per-layer upscaling filters.

Based on [WatermelonDS](https://github.com/SapphireRhodonite/WatermelonDS) (formerly melonDualDS) by
SapphireRhodonite, itself built on [rafaelvcaetano's melonDS Android port](https://github.com/rafaelvcaetano/melonDS-android).
Currently synced to WatermelonDS **0.7.0**.

"Experimental" is meant literally: this fork exists to try renderer ideas on one specific handheld.
Expect rough edges on anything that isn't a Thor.

This repository is self-contained — the emulator core (`melonDS-android-lib/`, derived from the
[melonDS](https://github.com/melonDS-emu/melonDS) core) is part of the tree, no core submodule.

## What this fork adds

### HD texture packs
* **3D texture dump & replace** — content-hash keyed (texture hash + palette hash), compatible
  with the desktop melonDS HD pack format, so packs can be authored and verified on PC and used
  on device unchanged.
* **2D sprite (OBJ) and BG tile dump & replace** — sprites and background tiles are dumped as
  assembled art and can be replaced with scaled versions (up to 4x).
* Packs live in `files/texturepacks/<GAMECODE>/`; dumps are written to `files/texturedumps/`.

### Per-layer upscaling filters
* Independent filter selection for **3D textures**, **OBJ sprites**, and **BG layers**
  (Settings → Video): nearest, bilinear, and a set of pixel-art filters including Scale2x,
  HQ2x, SaI, Eagle, MMPX, a faithful multi-pass **ScaleFX** port, and **Anime4K (DoG)**.
* **Anime4K** is applied *per producer*, not to the finished frame — so sprite art can get it
  while 3D geometry and backgrounds are left alone. It is Anime4K v4.0.1's
  Difference-of-Gaussians upscaler with the separable passes fused into one 3×3 neighbourhood,
  which keeps full-frame temporaries (and their bandwidth cost) off tile-based mobile GPUs.
* Filters run in the Vulkan compositor as a cached pre-pass — static scenes cost nothing
  (content-hashed reuse), and 3D texture filtering is cached per texture at upload.
* A persistent on-disk filter cache (Settings → Video → "Filter disk cache") keeps filtered
  results between sessions, so a second launch of the same game does no filtering work at all.
* No full-screen smoothing: original pixels stay sharp unless a layer's filter says otherwise.

### In-game overlay
* **Turbo** with a speed picker (1.5x/2x/3x/4x/8x/uncapped) that takes effect while turbo is
  already held, rather than only at configuration time.
* **Texture filter switching** for the 3D, sprite and BG producers without leaving the game —
  the renderer applies these to the live `Renderer3D`, so no reload is needed.
* **Stretch to fit both screens**, see below.

### Input
* Key bindings can take an optional **modifier**, so a hotkey can sit behind a chord and leave
  the plain button free for the game. Combos are matched before plain bindings; the unmodified
  key still works on its own. Existing configurations load unchanged.

### Cheats
* A cheat database bundled at `app/src/main/assets/usrcheat.xml` is imported on first launch when
  the cheat database is empty, so a fresh install starts with cheats available. The format is the
  R4CCE/DeSmuME `codelist` XML the in-app importer already understands.

### Dual-screen and stability work
* **Stretch to fit both screens** (in-game pause menu -> Dual Screen Presets):
  one toggle drops both letterboxing rules so each DS screen fills its physical
  panel edge to edge. The individual "Keep DS aspect ratio" and "Integer scale"
  switches remain below it for finer control.
* Fixes for dual-display presentation on the Thor's two panels (screen-swap alternation,
  capture-backed scenes, frame pacing under load), verified with per-display captures.
* Renderer thread-safety and Vulkan lifecycle fixes throughout the compositor and presenter.
* JIT and fastmem fixes in the core (coprocessor register access, DTCM host mapping) that
  recovered a large chunk of CPU time on heavy scenes.
* Optimized native build settings for smooth performance at high internal resolution.

## Relationship to upstream

This fork tracks WatermelonDS and merges its releases rather than diverging. Where both projects
changed the same subsystem, the merge keeps whichever implementation was verified on the Thor and
adopts upstream's where it is strictly broader. Notable current examples:

* The exact-capture line cache is banked per screen-swap here, crossed with upstream's
  `CaptureSourceIdentity` rather than replaced by it.
* `createComputePipelineFromSpirv` stays a member function because the per-mode pre-pass shader
  split needs it from more than one caller.
* Upstream's structured-capture branch tree in the soft-packed frame latch supersedes the
  narrower payload merge this fork used to carry.

## Building

Requirements: JDK 21, Android NDK 28.x, CMake 3.22+, Rust (for librashader).

```
git clone --recurse-submodules https://github.com/noeldvictor/watermelon-DS-THOR-AI-FORK.git
cd watermelon-DS-THOR-AI-FORK
./gradlew :app:assembleGitHubProdDebug
```

The emulator core is part of this repository; the remaining submodules are third-party
libraries (oboe, faad2, enet).

The APK lands in `app/build/outputs/apk/gitHubProd/debug/`. On Windows, build from PowerShell and
set `JAVA_HOME` (JDK 21), `ANDROID_NDK_HOME`, and `CARGO`/`RUSTUP` to the executable paths —
the librashader plugin resolves its tools from those variables. Windows checkouts need
`git config core.longpaths true`.

Shader binaries are checked in. After editing any `.comp`/`.frag`/`.vert`, regenerate them with
`scripts/regenerate_vulkan_spirv.sh`, which finds `glslc` in the NDK's `shader-tools`.

## Credits

* [melonDS](https://github.com/melonDS-emu/melonDS) by Arisotura and the melonDS team
* [melonDS Android port](https://github.com/rafaelvcaetano/melonDS-android) by rafaelvcaetano
* [WatermelonDS](https://github.com/SapphireRhodonite/WatermelonDS) by SapphireRhodonite — Vulkan renderer,
  dual-screen and external display support, RetroAchievements, RetroArch shader presets
* [librashader](https://github.com/SnowflakePowered/librashader) — RetroArch shader preset support
* [Anime4K](https://github.com/bloc97/Anime4K) by bloc97 — the DoG upscaler used by the sprite
  filter (MIT). The mobile single-pass formulation follows the one in
  [Azahar](https://github.com/azahar-emu/azahar).
* HD pack format inspired by the texture replacement systems of Dolphin and DuckStation

melonDS is free software licensed under the GPLv3; this fork retains that license.
