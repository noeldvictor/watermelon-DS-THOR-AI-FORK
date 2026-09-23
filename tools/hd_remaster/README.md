# HD remastering

Build an HD texture pack for a DS game straight from its ROM, without playing through it to dump
textures first. Every texture in the ROM is decoded, named by the key Watermelon Thor looks it up
by, upscaled with an AI model on your PC, and pushed to the device.

```
ROM ─▶ extract ─▶ upscale (GPU) ─▶ build ─▶ push ─▶ play
```

## Setup (once)

Windows with an NVIDIA GPU:

```
powershell -ExecutionPolicy Bypass -File tools\hd_remaster\setup.ps1
```

This creates `tools/hd_remaster/.venv` with CUDA PyTorch and
[spandrel](https://github.com/chaiNNer-org/spandrel), and downloads the default model,
[4x-UltraSharp](https://huggingface.co/Kim2091/UltraSharp) (CC BY-NC-SA 4.0, so packs made with
it are for personal use). Any ESRGAN-family model spandrel can load works with `--model`.

## Remaster a game

```
.venv\Scripts\python hd_remaster.py all  path\to\game.nds     # extract + upscale + build
.venv\Scripts\python hd_remaster.py push packs\<GAMECODE>       # install on the device
```

Then start the game. The pack is picked up at game start; no setting needs changing, because a
pack folder for the game switches loading on by itself. Packs apply with the Vulkan renderer
(the default) or the Compute renderer.

The steps can also run one at a time (`extract`, `upscale`, `build`, `push`). `upscale` resumes
where it stopped, and `build --native` makes a 1x pack from the unscaled images, which should
render exactly like no pack at all: a quick check that every key and pixel is right.

## How it works

A DS game uploads a texture's bytes to video memory unchanged, and the pack key is a hash of
those bytes, so the key of every texture can be computed from the ROM.

- **Finding textures.** The ROM filesystem, NARC archives and LZ10/LZ11 compression are
  unpacked. Games that keep their files in a custom archive still store standard Nitro files
  inside it, often compressed; those are found by the magic an LZ stream leaves in the clear.
- **Palettes.** A model's materials say which palette each texture is drawn with, so only those
  pairs are written. Textures no material names fall back to the palettes in their own file.
- **Decoding** matches melonDS bit for bit, including the RGB5 to RGB6 expansion and all seven
  texture formats.
- **Upscaling** pads each texture before inference so the model doesn't invent a border that
  shows as a seam when the texture repeats. The material states whether each axis repeats,
  mirrors or clamps, and the padding follows it per axis. Transparent pixels are filled from
  their visible neighbours first, and binary alpha stays binary.
- **2D sprites and backgrounds** come from the standard NCGR/NCLR/NCER/NSCR files. Every sprite
  cell and background screen is assembled at native size and upscaled as a whole, then cut into
  the per-sprite and per-tile images the emulator looks up, so neighbouring pieces stay seamless.
  A sprite that another sprite overlaps is upscaled on its own instead. Where the files don't
  pin down a palette (256-colour art, whose key covers the whole palette memory), the key is
  built from a best guess; a wrong guess just doesn't match. Game-specific load rules that can't
  be read from the files live in `twod.PROFILES` (Lufia's portraits, for example, are uploaded
  with their colour indices shifted by 48).
- **On the device**, pack images are indexed at game start and decoded the first time the game
  shows them, so a whole-game pack costs memory only for what is on screen.

## Checking a pack against the real game

If you have textures dumped in-game (Settings → Video → dump textures), compare them with an
extraction:

```
.venv\Scripts\python hd_remaster.py verify work\<GAMECODE> --dumps <dumps>\textures --sprites <dumps>\sprites
```

On Lufia: Curse of the Sinistrals:

| | Reproduced from the ROM | Pixel-identical |
| --- | --- | --- |
| 3D textures dumped during play | 181 of 223 | 181 of 181 |
| Sprites dumped during play | 324 of 587 | 324 of 324 |

The rest are built by the game at runtime: character parts assembled into one texture, blank
render buffers, dialogue text drawn into sprite memory, and captures of the 3D scene shown as
sprites. The per-layer filters still apply to those.

## Limits

- Anything a game builds at runtime can't be found in the ROM (see above).
- Games with fully custom formats (not Nitro TEX0 / NCGR) need their own reader.
- Background tile keys haven't been checked against in-game dumps yet.
- Replacement skips rotating/scaled sprites, as the emulator's 2D replacement does.

The upscaler (`upscale.py`) comes from the ARMSX2 Thor fork's disc-texture tooling, adapted to
use the DS addressing modes for its seam padding.
