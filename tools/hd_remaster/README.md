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

One command sets up on first run, builds the pack and installs it on the attached device:

```
powershell -ExecutionPolicy Bypass -File tools\hd_remaster\remaster.ps1 path\to\game.nds -Push
```

If the game has a recipe in [`games/`](games/README.md), it's applied automatically: the
models, scale and game-specific rules that were verified for it, plus a check that your ROM and
the key counts match. Games without one run with the defaults.

The individual steps, run with the venv's python:

```
.venv\Scripts\python hd_remaster.py all  path\to\game.nds     # extract + upscale + build
.venv\Scripts\python hd_remaster.py push packs\<GAMECODE>       # install on the device
```

Then start the game. The pack is picked up at game start; Settings → Video → Load texture packs
is on by default (and switches packs off, also in a running game). Packs apply with the Vulkan renderer
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
- **Pictures smaller than their texture.** DS texture sizes are powers of two, so a 256x192
  screen picture (Phantom Hourglass's storybook pages) is uploaded into a 256x256 texture whose
  last 64 rows hold whatever video memory held before. Its key ends in `_rows192` and hashes
  only the rows the picture fills; the emulator retries a missed texture with that shorter hash
  for the sizes a pack has such entries for.
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
- **Text** is drawn by games at runtime from Nitro fonts (NFTR), one glyph at a time, so it
  never exists as an image. Each font goes into the pack's `fonts/` folder as the font file
  itself plus an upscaled atlas of its glyphs (grey shade levels; `fonts.py`). On the device,
  sprites the pack has no image for are searched for glyphs by exact pixel match, and each glyph
  is redrawn from the atlas in the palette colours the game drew it with.
- **On the device**, pack images are indexed at game start and decoded the first time the game
  shows them, so a whole-game pack costs memory only for what is on screen.

## AI redraws (portraits)

**The full guide, with what worked, what didn't and what it cost: [REDRAW.md](REDRAW.md).**

An upscaler can only sharpen what the pixels hold. A 128x160 portrait has two or three pixels
per eye, so even a good model draws the eyes wrong. `redraw.py` sends the upscale plus
reference artwork (the game's official character art) to an image model on
[OpenRouter](https://openrouter.ai) and asks for a cleaner version of the same painting: same
framing, pose, colours and expression, with the eyes, mouth and details redrawn properly.

- **Key:** put `OPENROUTER_API_KEY=...` in `tools/hd_remaster/.env` (git ignores it).
- **Pick a model:** `redraw.py models` lists the image models; `redraw.py try work\<CODE> <key>
  --models a,b,c --ref refs\x.jpg --name X` runs one image through several and writes a
  side-by-side sheet. On Lufia, `google/gemini-3-pro-image` won (clean anatomy, kept the
  character's own markings, aligns well; about $0.14 per image).
- **A character:** `redraw.py portrait work\<CODE> --match talk_f_gades_ --name Gades --ref
  refs\gades.jpg` redraws the master expression (`--master normal`), then every other expression
  as an edit of the master, so the body stays the same between expressions. Only where the
  game's own expression differs from the master (grown a little and softened) comes from the
  expression's redraw.
- **The whole cast, cheaper:** `redraw.py cast work\<CODE> --prefix talk_f_ --config
  games\<CODE>\redraw.json` groups each character's expressions by frame (the game cuts some
  to other sizes or poses; each group gets its own master), redraws each master in one call,
  then the rest of the group four at a time: a 2x2 sheet of face close-ups (the box where the
  expressions differ) in one call, split and blended back into the master. A quarter of the
  cost per expression, but not recommended: on Lufia a third of the results failed the check
  (eye colours, drifting expressions, seams); see [REDRAW.md](REDRAW.md#what-didnt-work). `redraw.json` maps character ids to display names and reference files.
- **One image:** `redraw.py one work\<CODE> <key> --kind textures --ref refs\logo.jpg --what
  "the title logo"` (the input is padded to the nearest shape the model returns, then cropped).
- Every result is aligned back onto the upscale and cut out with the upscale's own alpha: the
  outline stays the game's, so the art drops into the pack in place. Soft edge pixels get the
  interior colours, not the model's grey background; grey the model left inside the outline is
  filled from the upscale.
- Results go to `work\<CODE>\redrawn\`, which `build` prefers over `upscaled\`. Every call's
  cost goes to `work\<CODE>\redraw\ledger.jsonl`; `--budget` (default $20) stops a run before
  that total passes it. `--reuse` rebuilds from the saved model outputs without new calls.
- References are not in the repo (the art belongs to its publisher). For Lufia they are the
  official character art on Creative Uncut, e.g.
  `https://cucdn.creativeuncut.com/gallery-50/art/lcots-gades.jpg` (the CDN wants the gallery
  page as Referer), saved to `work\<CODE>\refs\`.

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

- Anything a game builds at runtime can't be found in the ROM (see above), except text drawn
  from the game's fonts.
- HD text needs the glyphs drawn exactly as the font stores them: text in background layers,
  outlined or shadowed text drawn as two overlapping passes, and fonts that aren't NFTR stay
  native.
- Games with fully custom formats (not Nitro TEX0 / NCGR) need their own reader.
- Background tile keys haven't been checked against in-game dumps yet.
- Replacement skips rotating/scaled sprites, as the emulator's 2D replacement does.

The upscaler (`upscale.py`) comes from the ARMSX2 Thor fork's disc-texture tooling, adapted to
use the DS addressing modes for its seam padding.
