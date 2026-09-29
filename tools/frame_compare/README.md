# Frame-exact renderer comparison

`frame_compare.py` checks the Vulkan renderer against the software renderer (the reference) frame
by frame, on the device, from save states:

1. The ROM is launched on one renderer, the state loaded paused.
2. The debug command `DUMP_FRAME_SEQUENCE` saves the final screens of N consecutive frames
   (`cache/frame-sequences/<name>/frame_NNNN.png`, 256x384, top over bottom), stepping exactly one
   emulated frame between them (`MelonEmulator.debugStepFrame`).
3. Same again on the other renderer; both sequences are pulled and compared.

Output: per case, the frames and screens where Vulkan's picture differs, a `report.json` with the
share of differing pixels for every frame, and a strip per flagged frame (Vulkan | software frame
i | software frame i+1 | differing pixels in magenta).

```
python tools/frame_compare/frame_compare.py --rom "Lufia.7z" --state glitch_boss.ml --frames 120
python tools/frame_compare/frame_compare.py --cases tools/frame_compare/cases_thor.txt
python tools/frame_compare/frame_compare.py ... --renderers vulkan,vulkan   # determinism check: 0.00%
```

- Needs a debug build (the debug receiver), adb and numpy + Pillow. `--serial` picks the device.
- The run switches the renderer, sets the internal resolution (`--ir`, default 1, the cleanest
  comparison), turns texture packs off (`--packs` keeps them) and turns renderer debug tools on;
  all four are restored afterwards and the emulator is closed. It refuses to start while an
  emulator is in front (another session may be using the device).
- States are slots (`slot:N`) or files in the app's files dir; save one without touching a slot
  with `SAVE_STATE --es path /data/user/0/app.watermelonthor.dev/files/<name>.ml`.

## Two checks: against software, and against a baseline

- **Against the software renderer** (always): catches large errors - a black, white or stale
  screen, a missing layer (more than 2% of a screen, well above the run's usual level).
- **Against a baseline** (`--baseline <earlier run folder>`): runs are bit-exact (a Vulkan vs
  Vulkan run gives 0.00%), so every pixel a new build draws differently from a known-good build's
  run is the new build's doing. Each changed frame gets a verdict from the software renderer:
  WORSE (pixels that matched it no longer do), better, or changed. This is the check that catches
  small regressions. Strips: new | baseline | software | yellow = changed, magenta = newly wrong,
  green = newly right.

Why both: small glitches in 3D scenes can't be told apart from Vulkan's normal edge shimmer by
comparing with software alone. At native resolution polygon edges flicker by a pixel as 3D moves on
Vulkan and stay put on the software renderer, exactly like a small real glitch; `flicker` in
report.json counts those pixels but doesn't flag them. Proven with a control (2026-09-29):
with the Hotel Dusk fix (`88818973`) switched off, the software check flagged nothing (the 237
blinking text pixels are 0.5% of the screen), the baseline check flagged every other frame as
"237 px newly wrong -> WORSE", and with the fix back it reported "same as the baseline on every
frame".

```
python tools/frame_compare/frame_compare.py --cases tools/frame_compare/cases_thor.txt --baseline ../frame_compare_baselines/<run>
python tools/frame_compare/frame_compare.py --reanalyze <run>/<case> --baseline <baseline run>/<case>   # offline
```

Keep baselines outside the repo (a 13-case run is ~200 MB of PNGs); make a new one after an
intended rendering change.

## Reading the results

- Vulkan never matches the software renderer exactly: polygon edges and blending differ by a few
  percent of the pixels on every frame ("usual differing share"). A screen is flagged when its
  share is above 2% and well above that usual level for the run.
- The Vulkan frontend composes the 3D rendered during frame F with F's 2D, one frame before the
  hardware shows it, so moving 3D runs one frame ahead. Each Vulkan frame is compared per pixel
  with software frames i and i+1 and the closer one counts, so that lead alone is not flagged.
- What it sees: the composed picture (latch, compositor, 3D). What it does not: presentation (the
  frame queue and the presenter's history holds decide which composed frame reaches the panel);
  screen recordings still cover those.
- Games that read the real-time clock can differ between runs (Lufia's title screen): check a
  new state with `--renderers vulkan,vulkan` first.
- The first two dumped frames are left out: the picture from before the state loaded, and the
  first frame after it, which Vulkan prepares but does not show (a load clears the latch's
  history; see `vulkanPostLoadHiddenFrames`). Frame 0 in a report is the second frame after the
  load. The software renderer isn't a ground truth right after a load either: its 3D for that
  frame was due during the frame before the load, so it shows no 3D there.
