# Watermelon Remaster Studio

A local web app for the HD remastering pipeline and the AYN Thor: pick a game, run the pipeline
steps with one click and watch their output live, install the pack, look at both Thor screens,
and run the renderer checks, all from the browser. It wraps the existing tools without changing
them:

- [`tools/hd_remaster`](../hd_remaster/README.md) - ROM to HD texture pack (`extract`, `upscale`,
  `build`, `push`, ...)
- [`tools/thor_mcp`](../thor_mcp/README.md)'s way of driving the device - adb plus the app's debug
  broadcasts (`LIST_ROMS`, `LAUNCH_ROM`, `SAVE_STATE`, `GET_FPS`, ...)
- [`tools/frame_compare`](../frame_compare/README.md) - frame-exact Vulkan vs software checks

It runs only on your PC (127.0.0.1); nothing is uploaded anywhere.

## Start it

Double-click-friendly, from the repo root:

```
powershell -ExecutionPolicy Bypass -File tools\studio\studio.ps1
```

It uses the pipeline's Python (`tools\hd_remaster\.venv`) when that exists, otherwise any Python
3.10+ on PATH, and opens http://127.0.0.1:8765/ in the browser. If another program already uses
port 8765, the next free port is taken (the console says which); if the studio is already
running, the open one is shown instead. Stop it with Ctrl+C in its window: running jobs are
stopped too.

Options: `-Port 8800` picks another port, `-NoBrowser` doesn't open the browser. Or run it
directly: `python tools\studio\studio.py [--port N] [--no-browser] [--settings FILE]`.

Needs: Windows, `adb` on PATH (Android platform-tools) for anything on the Thor, a debug build of
Watermelon Thor (`app.watermelonthor.dev`, it carries the debug receiver), and for upscaling an
NVIDIA GPU (the Setup button installs the rest).

## Pages

**Library** - every game the pipeline knows: recipes in `tools/hd_remaster/games/`, work folders
and built packs. Each card shows the status from `games/GAMES.md`, its before/after picture, the
pack size and which steps are done. **New game** takes the path of an unzipped `.nds` file (or
one found in your ROM folders), reads its game code and starts the extraction. The setup card
shows whether the Python environment, the upscale model and adb are in place, and whether an
OpenRouter key is set for AI redraws (only yes/no: the key itself is never read out or shown).
**Run setup** runs `tools\hd_remaster\setup.ps1` (CUDA PyTorch, about 3 GB).

**Game** (click a card) - the pipeline as buttons, with the next step highlighted:

| Button | Runs |
| --- | --- |
| Extract | `hd_remaster.py extract <rom>` |
| Upscale | `hd_remaster.py upscale work\<CODE>` with the chosen model, scale, force, only |
| Build | `hd_remaster.py build work\<CODE>` (optionally `--native`, a 1x test pack) |
| Install on Thor | `hd_remaster.py push packs\<CODE> --serial <serial>` |
| All in one | `hd_remaster.py all <rom>` (extract + upscale + build) |
| Verify | `hd_remaster.py verify work\<CODE> --dumps ... [--sprites ...]` |
| Find misses | `hd_remaster.py misses work\<CODE> <rom> [--apply]` (reads the Thor's log) |

Output streams in live; **Cancel** stops a job (see below). The page also shows the ROM (with a
one-click pick when a ROM with the right game code is in your ROM folders, and whether it is
one the recipe was verified on), the job history, the recipe (read-only) and the pack and work
folders, and the 3D models section (next).

### 3D models

The Game page's **3D models** section wraps `hd_remaster.py models` (see
[the pipeline's 3D models notes](../hd_remaster/README.md#3d-models-first-step-extraction-and-keys)):
replacement meshes for a game's NSBMD models, drawn by the Vulkan renderer with the game's (or
the pack's HD) textures and following its animation.

| Button | Runs |
| --- | --- |
| Extract models | `hd_remaster.py models extract <rom> [--trace T.json] [--cpu-words W] [--previews]` |
| Build models | `hd_remaster.py models build work\<CODE> [--smooth S] [--only=TEXT] [--seen]` |
| Install models on Thor | `hd_remaster.py push packs\<CODE> --models-only --serial <serial>` |
| Fit mesh... (per model) | `hd_remaster.py models fit work\<CODE> --model=<id> --mesh <file.glb\|.obj>` |
| Smooth this model | `hd_remaster.py models build work\<CODE> --smooth S --only=<id>` |
| AI dry run | `hd_remaster.py models ai work\<CODE> --model=<id> --provider P --polycount N --budget C --dry-run` |
| AI generate... | the same without `--dry-run`: **spends money** (see below) |

The list comes from `work\<CODE>\models\index.json`, most seen (by a `dl_trace`) first, with each
model's `preview.png` as a thumbnail and badges for an `edited.obj`, AI reference pictures and AI
meshes; a filter box and "Seen in a trace" narrow it (1285 models in Phantom Hourglass). Picking a
model shows its shapes (key, material, texture size, lit, vertices, triangles, how often seen,
whether a replacement is built in `models_built`), its pictures (original preview, the fitted
mesh's `edited_preview.png`, the four AI reference views, its textures; click one to enlarge) and
the per-model buttons. **Fit mesh...** takes the full path of a GLB or OBJ (or one of the model's
own AI results) and asks before it writes over an existing `edited.obj`. Build models without a
smooth strength builds the edited models; "Only" and "seen" pick which models to smooth (`--only`
matches part of the id, name or source, so Smooth this model can also catch a model whose id
contains this one's). Installing needs the replacements in `packs\<CODE>\models`: `models build`
copies them there only when that pack exists, so build the texture pack first (a native test build
is enough). It replaces just that folder on the Thor (no backup; the textures stay) through the
Thor lane with the shared-device check, and the models load the next time the game starts.

AI generate sends four rendered views of the model to Tripo (default) or Meshy and fits the
returned mesh onto it as `edited.obj`. A call costs 20 credits (Tripo: $1 = 100 credits). The
studio only starts one when all of these hold, checked on the server:

- the provider's key is set: `TRIPO_API_KEY` or `MESHY_API_KEY` in `tools\hd_remaster\.env` (or
  the environment). The studio only says yes or no (here and on the Library's setup card); it never
  reads a key out, logs it or puts it on a command line. The tool reads it itself.
- the game's ledger (`work\<CODE>\models\ai_ledger.jsonl`, shown as "AI ledger: N credits") plus
  20 stays within the budget field (a cap over the whole ledger, default 100), and
- the request carries `confirm_spend: true`, which the page only sends after its cost dialog
  (provider, 20 credits per call, the ledger total) was confirmed.

A dry run needs none of that: it renders the reference pictures and sends nothing.

**Checks** - runs `frame_compare.py --cases <file>` on the Thor, optionally against a baseline
run, with the internal resolution, renderers and "keep packs" options. Results from the studio's
output folder and from your baseline folders are listed; pick a run to see its cases and the
flagged frames as image strips (click one to enlarge). Each case launches its game twice, so a
13-case file keeps the Thor busy for a while.

**Jobs** - everything run in this session, with output.

**Settings** - device serial (blank = the attached AYN Thor), app package, bottom display id, the
device's ROM folder URI, ROM folders on this PC, the hd_remaster folder, the Python for jobs, the
checks output folder and baseline folders, and the port. Stored in
`tools/studio/studio_settings.json` (created on first start, not committed).

**Thor panel** (right side, the "Thor" button toggles it; a drawer on narrow windows) - connection
and model, what is in front on the Thor, FPS while a game runs, a live view of both screens
(refreshed about every 1.5 s while the panel is open and the tab visible), and:

- **Launch a game**: load the Thor's ROM list and launch one, or type a file name from its ROM
  folder.
- **Save states (private files)**: save/load `files/<name>.ml` in the app's data, so the game's
  own slots stay untouched (the same files `frame_compare` cases use).
- **Texture packs** on/off (`enable_texture_packs`, applies to a running game).
- **Close emulator** (`am force-stop`). The Thor is shared: close it when you're done.

## The Thor is shared

Other sessions may be using the device. Every command that changes something on it (launch,
states, packs, close, install, install models, checks) first looks at what is in front. If it's anything other
than Watermelon Thor or the home screen, the studio asks before it does anything. Status, FPS and
screenshots only read. FPS and the pack setting are read only while the app already runs, so
polling never starts it.

## Jobs, queues and cancelling

Jobs run in two queues: pipeline steps (and setup, and the 3D model steps) one at a time, and
jobs that use the Thor (install, install models, checks) one at a time; a second job waits for the
first. Each Python job runs through
`runner.py` in its own process group, so **Cancel** first asks it to stop (CTRL_BREAK, which the
tool sees as Ctrl+C): `upscale` keeps what it finished and resumes next time, and `frame_compare`
puts back the renderer, internal resolution, texture-pack and debug-tool settings it changed and
closes the emulator. Only if it doesn't stop in time is it killed; for checks the studio then
restores those settings itself from a snapshot taken before the run. History lives in memory and
is gone after a restart.

## Files

| File | What |
| --- | --- |
| `studio.py` | entry point: settings, port, browser |
| `server.py` | HTTP routes, static files, Server-Sent Events for job output |
| `app.py` | the operations behind the API |
| `jobs.py` | background jobs, queues, cancel |
| `runner.py` | wrapper that makes Python jobs cancellable cleanly |
| `device.py` | adb: status, screenshots, debug broadcasts, the shared-device guard |
| `pipeline.py` | library, recipes, GAMES.md, ROM headers, hd_remaster command lines |
| `checks.py` | frame_compare runs, reports and strips |
| `config.py` | settings file and paths |
| `static/` | the page: plain HTML, CSS and JavaScript modules, no build step, nothing from the internet (`js/views/models3d.js`: the Game page's 3D models section) |
| `test_studio.py` | smoke tests (fake adb, fake pipeline) |

Tests: `tools\hd_remaster\.venv\Scripts\python.exe -m unittest tools\studio\test_studio.py -v`.
They start a real server on a free port with adb replaced by a fake and a stand-in hd_remaster
folder, so they never touch the device or your packs.

## Limits (v1)

- Windows only (paths, PowerShell setup, process groups). Only tested in Edge/Chromium.
- One user, one browser: no login. It binds to 127.0.0.1 and refuses requests from other web
  pages (Host, Origin and Sec-Fetch-Site checks), but any program on this PC can call it.
- Job history is not saved across restarts. Recipes are shown read-only; edit
  `recipe.json` in an editor.
- No AI redraw (`redraw.py`) buttons yet; the setup card only says whether its key is set.
- The pack size is measured in the background (a pack can hold 240,000 files) and shows
  "measuring..." for a few seconds after start.
- The device's ROM folder URI defaults to this Thor's SD card layout; change it in Settings for
  another device. Launching by file name needs it; the ROM list doesn't.
- Install (`push`) copies the whole pack every time, as `hd_remaster.py push` does; large packs
  take minutes. Install models copies only `models\`.
- 3D models: the list shows 120 rows at a time ("Show more"); model pictures are only those the
  tool wrote (PNGs in the model's folder and its `ai\` folder). Meshes to fit are typed as paths:
  there is no upload.
