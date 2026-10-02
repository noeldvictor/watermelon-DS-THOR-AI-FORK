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

## 3D models (first step: extraction and keys)

Models are NSBMD files (MDL0 blocks). `models3d.py` reads them the way the SDK draws them: the
node tree, the render commands (SBC: node matrices into matrix-stack slots, skinning blends,
materials, shapes) and each shape's display list, decoded like the geometry engine does, into
bind-pose triangles with texture coordinates, normals, colours and the stack slot every vertex
used. `render3d.py` draws them (numpy only) with the model's own textures, which is how bind
poses are checked and how reference pictures for an AI model generator are made.

A shape's display list reaches the geometry FIFO byte for byte (the SDK DMAs it from the model
file), so it is keyed like the textures, by content: `mdl1_<size>_<xxh64>`. The debug build's
`dl_trace` tool (tools/re) lists the keys a scene sends. Phantom Hourglass, Mercay beach: all 33
display lists sent in 6 seconds (7351 transfers: Link, the island, palm trees, bridge, beach,
sea) are shapes extracted from the ROM; 1285 models, 4844 distinct shapes in the ROM. Very small
shapes (Link's eyes) don't show up there; the SDK seems to send short lists with the CPU.

**Runtime replacement** (Vulkan renderer): a pack's `models/mdl1_<size>_<hash>.dl` is a display
list that replaces the shape with that key. When the shape's DMA into the GX FIFO starts, its
commands are tagged on their way through the FIFO; the original still runs (its timing and its
polygons' count against the DS limits stay, so the game sees no difference), its polygons are
left out of the render list, and the replacement runs through the geometry engine's own
transform, lighting, clipping and viewport code from the state the original started with,
into separate buffers without the hardware limits. A replacement is the same kind of command
list as the original (matrix-stack restores, normals or colours, texture coordinates, vertices),
so skinned and animated models follow their bones. `models3d.parse_display_list`,
`encode_display_list` and `absolute_vertices` read, write and reshape them.

Checked on Phantom Hourglass (Mercay beach, `HDModels[Stats]` ~2500 replaced display lists and
~15700 polygons per 60 frames, 60 fps): re-encoded copies of all 33 shapes give frames
bit-identical to no replacement (frame_compare, 60 frames, both screens); Link's shapes with
every vertex scaled 1.25 draw a puffier Link that still animates; save states made and loaded
with models on work (a state keeps only the hardware's polygons).

Commands (`modelpack.py`):

```
.venv\Scripts\python hd_remaster.py models extract ROM.nds [--trace dl.json] [--previews]
.venv\Scripts\python hd_remaster.py models build work\<CODE> [--smooth 0.6 --only TEXT | --seen]
```

`extract` writes `work\<CODE>\models\<model>\`: `model.obj`/`.mtl` (bind pose, one group per
shape), the decoded textures, `model.json` (shape keys, materials, texture sizes, lighting) and
`preview.png` for the models a `dl_trace` json saw (all with `--previews`); `index.json` lists
them, most used first (Phantom Hourglass: 1285 models in 86 s). Edit a model in any 3D tool
keeping the group names and save it as `edited.obj` next to `model.obj`; `build` ties every new
vertex to the bone of the nearest original vertex of its shape (texture coordinates from the
OBJ, else from that vertex), writes `work\<CODE>\models_built\<key>.dl` and copies them into
`packs\<CODE>\models\` (texture `build` does too, so a rebuild keeps them). `--smooth` makes
PN-smoothed replacements instead, no new art. An edit round-trips exactly: Link widened 10% and
built comes back from the display lists at s3.12 precision, skinned shapes included.

AI models (`ai3d.py`; Tripo by default, or Meshy):

```
.venv\Scripts\python hd_remaster.py models ai work\<CODE> --model <id> [--provider tripo|meshy] [--dry-run] [--polycount 6000] [--budget 100]
.venv\Scripts\python hd_remaster.py models fit work\<CODE> --model <id> --mesh new.glb
```

`ai` renders the model from the front, right, back and left with its own textures
(`models\<id>\ai\ref_*.png`; `--dry-run` stops there) and sends them to a multiview image-to-3D
service for bare geometry (the game's own textures go on in the fit):

- `tripo` (default): Tripo API v3 (`https://openapi.tripo3d.ai/v3`; the v2 API retires on
  2026-11-01). Each view is uploaded (`POST /files`), a `multiview-to-model` task is created with
  view-keyed inputs, model `v3.1-20260211`, texture and PBR off, `face_limit` = `--polycount`, then
  `GET /tasks/{id}` is polled and `output.model_url` downloaded at once (it expires 5 minutes after
  success). Key `TRIPO_API_KEY`.
- `meshy`: Meshy multi-image-to-3D, mesh only, remeshed to `--polycount`. Key `MESHY_API_KEY`.

Either costs 20 credits a call (Tripo: $1 = 100 credits, new accounts get 300 free). Every call is
logged in `models\ai_ledger.jsonl` and refused once the ledger would pass `--budget`. Keys live in
`.env` (gitignored) and are never printed.

`fit` takes any GLB or OBJ (an AI result or a 3D tool's, any scale, position or facing; Tripo
exports +X forward): scaled onto the original by height, turned to whichever of four facings ICP
fits best, refined with ICP; then every vertex takes the bone of the nearest point on the
original surface, and every triangle maps its corners through the texture mapping of the original
triangle nearest its centre (per-vertex transfer smeared triangles that straddle texture islands,
like face and hair). It writes `edited.obj`, `hidden.txt` (shapes the new mesh covers, replaced by
nothing) and `edited_preview.png`; then `models build`. Tested offline: Link moved, scaled 3.7x and
turned (4 degrees, and to face +X) comes back to about 1% of his height with his textures in
place; the Tripo client against a local fake of its v3 API (uploads, request body, polling,
download, ledger, budget).

Making replacements from meshes: `mesh_to_display_list` turns a bind-pose mesh back into a
shape's display list (each vertex into its matrix-stack slot's space, TEXCOORD, NORMAL for lit
shapes or COLOR, VTX_16). `pn_triangles` is an automatic remaster that needs no new art: curved
PN triangles (the ATI TruForm scheme) through the original corners, so outlines and points stay
where they are while the faces between them round out (strength 0.6, 3x3 per triangle looks
faithful; `loop_subdivide` rounds more but shrinks points like Link's cap tip). PN-smoothed Link
in PH: 9x the triangles (~1400 more polygons a frame), 60 fps.

## Limits

- Anything a game builds at runtime can't be found in the ROM (see above), except text drawn
  from the game's fonts.
- HD text needs the glyphs drawn exactly as the font stores them. Text with a one-pixel
  outline in its own palette index (Lufia's battle HUD, name plates, money and play time)
  is recognised and drawn with an HD outline; text in background layers, drop shadows and
  fonts that aren't NFTR stay native.
- Games with fully custom formats (not Nitro TEX0 / NCGR) need their own reader.
- Background tile keys haven't been checked against in-game dumps yet.
- Replacement skips rotating/scaled sprites, as the emulator's 2D replacement does.

The upscaler (`upscale.py`) comes from the ARMSX2 Thor fork's disc-texture tooling, adapted to
use the DS addressing modes for its seam padding.
