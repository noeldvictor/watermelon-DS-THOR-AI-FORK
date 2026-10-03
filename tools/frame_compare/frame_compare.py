#!/usr/bin/env python3
"""Frame-exact Vulkan vs software comparison on the device.

    python tools/frame_compare/frame_compare.py --rom "Lufia.7z" --state slot:7 --frames 120
    python tools/frame_compare/frame_compare.py --cases cases.txt --out work/frame_compare

For each case (a ROM on the device's ROM folder and a save state) the ROM is launched once per
renderer, the state loaded paused, and the debug command DUMP_FRAME_SEQUENCE saves the final
screens of N consecutive frames, one exact emulated frame apart. The two sequences are then
compared frame by frame: the report names the frames (and screens) where Vulkan's picture differs
from the software renderer's, the reference.

The Vulkan frontend composes the 3D rendered during frame F with F's 2D, one frame before the
hardware shows it, so moving 3D is one frame ahead on Vulkan. Each Vulkan frame is therefore
compared per pixel against software frames i and i+1 and the closer one counts.

Settings the run changes (renderer, internal resolution, texture packs, renderer debug tools) are
restored at the end, and the emulator is closed. Needs adb, a debug build (the debug receiver) and
numpy + Pillow.

cases.txt: one case per line, "<rom file name> | <slot:N or file.ml in app files> [| frames]";
blank lines and # comments are skipped.
"""
import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
import time
import urllib.parse
from pathlib import Path

import numpy as np
from PIL import Image

PACKAGE = "app.watermelonthor.dev"
ROM_TREE = "content://com.android.externalstorage.documents/tree/2664-21DE%3ARoms%2Fnds/document/2664-21DE%3ARoms%2Fnds%2F"
SCREEN_W, SCREEN_H = 256, 192
# a pixel differs when a channel is off by more than this (DS colours are 6-bit, x4 on output)
PIXEL_THRESHOLD = 40
# a screen is flagged when more than this share of its pixels differ, and well above the run's usual level
FLAG_MIN_SHARE = 0.02
# a pixel counts as changed against a baseline run when a channel moves more than this (runs are
# bit-exact, so anything above 0 is the build's doing)
BASELINE_THRESHOLD = 8
# dumped frames left out at the start: the picture from before the load, and the first frame after
# it, which Vulkan doesn't show
SKIPPED_FRAMES = 2


class Device:
    def __init__(self, serial):
        self.serial = serial

    def adb(self, *args, binary=False, check=True, timeout=120):
        result = subprocess.run(["adb", "-s", self.serial, *args], capture_output=True, timeout=timeout)
        if check and result.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)} failed: {result.stderr.decode(errors='replace')}")
        return result.stdout if binary else result.stdout.decode(errors="replace")

    def broadcast(self, action, *extras, timeout=120):
        out = self.adb("shell", "am", "broadcast", "-f", "32", "-p", PACKAGE, "-a", f"{PACKAGE}.{action}", *extras, timeout=timeout)
        match = re.search(r'result=(-?\d+)(?:, data="(.*)")?', out, re.S)
        if not match:
            raise RuntimeError(f"{action}: no result in {out!r}")
        return int(match.group(1)), match.group(2) or ""

    def preferences(self):
        _, data = self.broadcast("GET_PREFERENCES")
        return json.loads(data)

    def set_preference(self, key, value, kind=None):
        extras = ["--es", "key", key, "--es", "value", str(value)]
        if kind:
            extras += ["--es", "type", kind]
        code, data = self.broadcast("SET_PREFERENCE", *extras)
        if code != 1:
            raise RuntimeError(f"SET_PREFERENCE {key}={value}: {data}")

    def force_stop(self):
        self.adb("shell", "am", "force-stop", PACKAGE)

    def foreground(self):
        out = self.adb("shell", "dumpsys", "activity", "activities", check=False)
        match = re.search(r"topResumedActivity=\S+ \S+ (\S+)", out)
        return match.group(1) if match else ""

    def fps(self):
        _, data = self.broadcast("GET_FPS")
        match = re.search(r'"fps":([0-9.]+)', data)
        return float(match.group(1)) if match else 0.0

    def run_as(self, *args):
        return self.adb("exec-out", "run-as", PACKAGE, *args, binary=True, check=False)


SAVE_DIR = "/storage/2664-21DE/Roms/nds"


class SaveGuard:
    """Keeps a game's .sav on the SD card as it was before a run. Loading a save state makes the
    emulator write the state's save memory into the game's .sav (NDSCart's DoSavestate calls
    WriteNDSSave; this frontend has no "separate savefiles" option), so every test that loads a
    state would roll the user's save back to whatever it was when the state was made. The .sav
    is pulled first (a run is refused if one exists but can't be read) and pushed back, checked by
    MD5, after the emulator has stopped; a .sav the run created is removed."""

    def __init__(self, device, rom_file):
        import hashlib
        import tempfile
        self.device = device
        self.remote = f"{SAVE_DIR}/{Path(rom_file).stem}.sav"
        self.local = Path(tempfile.mkdtemp(prefix="saveguard_")) / "game.sav"
        self.md5 = None
        self._hashlib = hashlib

    def _exists(self):
        out = self.device.adb("shell", f'test -f "{self.remote}" && echo yes', check=False)
        return "yes" in out

    def __enter__(self):
        if self._exists():
            self.device.adb("pull", self.remote, str(self.local), timeout=120)
            if not self.local.exists():
                raise RuntimeError(f"can't back up {self.remote}; not running")
            self.md5 = self._hashlib.md5(self.local.read_bytes()).hexdigest()
        return self

    def __exit__(self, *exc):
        self.device.force_stop()
        if self.md5:
            self.device.adb("push", str(self.local), self.remote, timeout=120)
            out = self.device.adb("shell", f'md5sum "{self.remote}"', check=False)
            if self.md5 not in out:
                print(f"WARNING: {self.remote} could not be restored; the backup is {self.local}", flush=True)
        else:
            self.device.adb("shell", f'rm -f "{self.remote}"', check=False)
        return False


def launch(device, rom_file, settle_s):
    device.force_stop()
    device.adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    device.adb("shell", "am", "start", "-n", f"{PACKAGE}/me.magnum.melonds.ui.romlist.RomListActivity")
    time.sleep(3)
    uri = ROM_TREE + urllib.parse.quote(rom_file)
    device.broadcast("LAUNCH_ROM", "--es", "rom_uri", uri, "--ez", "wait_rom_ready", "true", timeout=180)
    # the ROM counts as ready before a big 7z is unpacked; a state sent before the game runs is dropped
    deadline = time.time() + 120
    running = 0
    while time.time() < deadline and running < 3:
        running = running + 1 if device.fps() > 30 else 0
        time.sleep(1)
    if running < 3:
        raise RuntimeError(f"{rom_file}: the game did not start")
    time.sleep(settle_s)
    if "EmulatorActivity" not in device.foreground():
        raise RuntimeError(f"{rom_file}: the emulator is not in front ({device.foreground()}) - another session may be using the device")


def load_state(device, state):
    if state.startswith("slot:"):
        extras = ["--ei", "slot", state[5:]]
    else:
        extras = ["--es", "path", f"/data/user/0/{PACKAGE}/files/{state}"]
    code, data = device.broadcast("LOAD_STATE", *extras, "--ez", "pause_after", "true")
    if code != 1 or "success=1" not in data:
        raise RuntimeError(f"LOAD_STATE {state}: {data}")
    time.sleep(1)


def dump_sequence(device, name, frames, out_dir):
    code, data = device.broadcast("DUMP_FRAME_SEQUENCE", "--ei", "frames", str(frames), "--es", "path", name)
    if code != 1:
        raise RuntimeError(f"DUMP_FRAME_SEQUENCE: {data}")
    remote = f"cache/frame-sequences/{name}"
    deadline = time.time() + 60 + frames * 2
    while time.time() < deadline:
        # a missing file still prints an error, so look for the file's own text
        if device.run_as("cat", f"{remote}/done.txt").startswith(b"frames="):
            break
        time.sleep(2)
    else:
        raise RuntimeError(f"{name}: the frame dump did not finish")
    archive = device.run_as("tar", "cf", "-", remote)
    target = out_dir / name
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                (target / Path(member.name).name).write_bytes(tar.extractfile(member).read())
    device.run_as("rm", "-rf", remote)
    return target


def load_frames(directory):
    frames = []
    for path in sorted(directory.glob("frame_*.png")):
        frames.append(np.asarray(Image.open(path).convert("RGB"), dtype=np.int16))
    return frames


def changed(a, b):
    return np.abs(a - b).max(axis=2) > PIXEL_THRESHOLD


def flicker_pixels(vulkan, software, index):
    """Pixels where Vulkan's picture leaves and comes back (A-B-A, or A-B-B-A) while the software
    renderer's stays put over the same frames, one more for Vulkan's 3D lead. Reported, not flagged:
    at native resolution Vulkan's polygon edges shimmer by a pixel as 3D moves and look exactly like
    this, so a small glitch in a 3D scene only stands out against a baseline run (--baseline)."""
    result = np.zeros((2 * SCREEN_H, SCREEN_W), bool)
    for length in (1, 2):
        before, after = index - 1, index + length
        if before < 0 or after >= len(vulkan) or after + 1 >= len(software):
            continue
        away = changed(vulkan[index], vulkan[before]) & changed(vulkan[index], vulkan[after])
        back = ~changed(vulkan[before], vulkan[after])
        steady = np.ones_like(away)
        for frame in range(before, after + 1):
            steady &= ~changed(software[frame], software[frame + 1])
        result |= away & back & steady
    return result


def compare(vulkan, software, out_dir, case_name):
    """Per-frame, per-screen share of differing pixels, flickering pixels, and the flagged frames."""
    rows = []
    count = min(len(vulkan), len(software) - 1)
    for index in range(count):
        v = vulkan[index]
        candidates = [software[index]] + ([software[index + 1]] if index + 1 < len(software) else [])
        diff = np.min([np.abs(v - s).max(axis=2) for s in candidates], axis=0)
        bad = diff > PIXEL_THRESHOLD
        flicker = flicker_pixels(vulkan, software, index)
        for screen, (y0, y1) in (("top", (0, SCREEN_H)), ("bottom", (SCREEN_H, 2 * SCREEN_H))):
            share = float(bad[y0:y1].mean())
            rows.append({
                "frame": index, "screen": screen, "share": share,
                "flicker": int(flicker[y0:y1].sum()),
                "vulkanLuma": float(v[y0:y1].mean()), "softwareLuma": float(software[index][y0:y1].mean()),
            })

    flagged = []
    for screen in ("top", "bottom"):
        shares = np.array([r["share"] for r in rows if r["screen"] == screen])
        if len(shares) == 0:
            continue
        # the run's usual level: Vulkan draws edges and blends slightly differently everywhere
        usual = float(np.median(shares))
        limit = max(FLAG_MIN_SHARE, usual * 3 + 0.005)
        for r in rows:
            if r["screen"] == screen and r["share"] > limit:
                r["flagged"] = True
                flagged.append(r)
    flagged.sort(key=lambda r: (r["frame"], r["screen"]))

    # one strip per flagged frame: Vulkan | software i | software i+1 | differing pixels
    for r in flagged:
        index = r["frame"]
        y0 = 0 if r["screen"] == "top" else SCREEN_H
        parts = [vulkan[index], software[index]] + ([software[index + 1]] if index + 1 < len(software) else [])
        crops = [p[y0:y0 + SCREEN_H].astype(np.uint8) for p in parts]
        diff = np.min([np.abs(vulkan[index] - s).max(axis=2) for s in parts[1:]], axis=0)[y0:y0 + SCREEN_H]
        heat = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)
        heat[..., 0] = np.clip(diff * 2, 0, 255)
        heat[diff > PIXEL_THRESHOLD] = (255, 0, 255)
        heat[flicker_pixels(vulkan, software, index)[y0:y0 + SCREEN_H]] = (0, 255, 255)
        strip = np.concatenate(crops + [heat], axis=1)
        Image.fromarray(strip).resize((strip.shape[1] * 2, SCREEN_H * 2), Image.NEAREST).save(
            out_dir / f"{case_name}_frame{index:04d}_{r['screen']}.png")
    return rows, flagged


def compare_baseline(vulkan, baseline, software, out_dir, case_name):
    """Frames where this build's Vulkan picture differs from a known-good run's. Runs are bit-exact,
    so every change is the build's; the software renderer says whether it moved closer or further."""
    changes = []
    count = min(len(vulkan), len(baseline), len(software) - 1)
    for index in range(count):
        new, old = vulkan[index], baseline[index]
        moved = np.abs(new - old).max(axis=2) > BASELINE_THRESHOLD
        reference = [software[index], software[index + 1]]
        new_bad = np.min([np.abs(new - r).max(axis=2) for r in reference], axis=0) > PIXEL_THRESHOLD
        old_bad = np.min([np.abs(old - r).max(axis=2) for r in reference], axis=0) > PIXEL_THRESHOLD
        for screen, (y0, y1) in (("top", (0, SCREEN_H)), ("bottom", (SCREEN_H, 2 * SCREEN_H))):
            changed_px = int(moved[y0:y1].sum())
            if changed_px == 0:
                continue
            gained = int((new_bad[y0:y1] & ~old_bad[y0:y1] & moved[y0:y1]).sum())
            fixed = int((old_bad[y0:y1] & ~new_bad[y0:y1] & moved[y0:y1]).sum())
            verdict = "WORSE" if gained > fixed else ("better" if fixed > gained else "changed")
            changes.append({"frame": index, "screen": screen, "changed": changed_px,
                            "newlyWrong": gained, "newlyRight": fixed, "verdict": verdict})
            strip = np.concatenate([new[y0:y1], old[y0:y1], software[index][y0:y1]], axis=1).astype(np.uint8)
            heat = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)
            heat[moved[y0:y1]] = (255, 255, 0)
            heat[(new_bad & ~old_bad & moved)[y0:y1]] = (255, 0, 255)
            heat[(old_bad & ~new_bad & moved)[y0:y1]] = (0, 255, 0)
            strip = np.concatenate([strip, heat], axis=1)
            Image.fromarray(strip).resize((strip.shape[1] * 2, SCREEN_H * 2), Image.NEAREST).save(
                out_dir / f"{case_name}_baseline_frame{index:04d}_{screen}.png")
    return changes


def report_baseline(changes):
    if not changes:
        print("  same as the baseline on every frame")
        return 0
    worse = [c for c in changes if c["verdict"] == "WORSE"]
    print(f"  CHANGED from the baseline on {len(changes)} screens ({len(worse)} further from software):")
    for c in changes[:40]:
        print(f"    frame {c['frame']:4d} {c['screen']:6s} {c['changed']:5d} px changed, "
              f"{c['newlyWrong']} newly wrong, {c['newlyRight']} newly right -> {c['verdict']}")
    if len(changes) > 40:
        print(f"    ... {len(changes) - 40} more in report.json")
    return len(worse)


def run_case(device, rom, state, frames, args, out_root):
    case_name = re.sub(r"[^A-Za-z0-9]+", "_", Path(rom).stem)[:40] + "_" + re.sub(r"[^A-Za-z0-9]+", "_", state)
    out_dir = out_root / case_name
    out_dir.mkdir(parents=True, exist_ok=True)
    sequences = {}
    for renderer in args.renderers:
        device.set_preference("video_renderer", renderer, "string")
        # loading the state writes its save memory into the game's .sav: put the user's back
        with SaveGuard(device, rom):
            launch(device, rom, args.settle)
            load_state(device, state)
            started = time.time()
            # frame 0 is the picture from before the state loaded and frame 1 the first after it,
            # which Vulkan prepares but doesn't show (MelonInstance::processFrameTail); the last
            # frame has no successor for the lag rule. Three extra frames keep all N requested ones
            # comparable
            sequences[renderer] = dump_sequence(device, f"{case_name}_{renderer}", frames + SKIPPED_FRAMES + 1, out_dir)
            print(f"  {renderer}: {frames} frames in {time.time() - started:.0f} s", flush=True)

    reference, tested = args.renderers[1], args.renderers[0]
    software = load_frames(sequences[reference])[SKIPPED_FRAMES:]
    vulkan = load_frames(sequences[tested])[SKIPPED_FRAMES:]
    rows, flagged = compare(vulkan, software, out_dir, case_name)
    baseline_changes = None
    if args.baseline:
        baseline_dir = next(Path(args.baseline).glob(f"{case_name}/{case_name}_{tested}"), None)
        if baseline_dir is None:
            print(f"  no baseline for {case_name} in {args.baseline}")
        else:
            baseline_changes = compare_baseline(vulkan, load_frames(baseline_dir)[SKIPPED_FRAMES:], software, out_dir, case_name)
    (out_dir / "report.json").write_text(json.dumps(
        {"rom": rom, "state": state, "frames": frames, "rows": rows, "baselineChanges": baseline_changes}, indent=1))
    usual = {s: float(np.median([r["share"] for r in rows if r["screen"] == s])) for s in ("top", "bottom")}
    print(f"  usual differing share: top {usual['top']:.2%}, bottom {usual['bottom']:.2%}")
    if flagged:
        print(f"  FLAGGED {len(flagged)}:")
        for r in flagged:
            print(f"    frame {r['frame']:4d} {r['screen']:6s} {r['share']:.1%} differ, {r['flicker']} px flicker "
                  f"(luma vulkan {r['vulkanLuma']:.0f} / software {r['softwareLuma']:.0f})")
    else:
        print("  no flagged frames")
    if baseline_changes is not None:
        report_baseline(baseline_changes)
    return flagged


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial", default="c3ca0370")
    parser.add_argument("--rom", help="ROM file name in the device's ROM folder")
    parser.add_argument("--state", help="slot:N or a state file in the app's files")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--cases", help="file with one 'rom | state [| frames]' per line")
    parser.add_argument("--ir", default="1", help="Vulkan internal resolution for the run (1 compares cleanest)")
    parser.add_argument("--packs", action="store_true", help="keep texture packs on (they change pixels on purpose)")
    parser.add_argument("--settle", type=float, default=3.0, help="seconds to let the game run before loading the state")
    parser.add_argument("--renderers", default="vulkan,software", help="tested,reference (vulkan,vulkan = determinism check)")
    parser.add_argument("--out", default="work/frame_compare")
    parser.add_argument("--reanalyze", help="a case folder from an earlier run: compare its pulled frames again, no device")
    parser.add_argument("--baseline", help="an earlier run's folder (a known-good build): also report every frame this build draws differently")
    args = parser.parse_args()
    args.renderers = args.renderers.split(",")

    if args.reanalyze:
        case_dir = Path(args.reanalyze)
        tested = next(case_dir.glob(f"*_{args.renderers[0]}"))
        reference = next(d for d in case_dir.glob(f"*_{args.renderers[1]}") if d != tested)
        vulkan, software = load_frames(tested)[SKIPPED_FRAMES:], load_frames(reference)[SKIPPED_FRAMES:]
        rows, flagged = compare(vulkan, software, case_dir, case_dir.name)
        for r in flagged:
            print(f"frame {r['frame']:4d} {r['screen']:6s} {r['share']:.1%} differ, {r['flicker']} px flicker")
        print(f"{len(flagged)} flagged screens")
        if args.baseline:
            baseline_dir = next(Path(args.baseline).glob(f"*_{args.renderers[0]}"))
            report_baseline(compare_baseline(vulkan, load_frames(baseline_dir)[SKIPPED_FRAMES:], software, case_dir, case_dir.name))
        return

    cases = []
    if args.cases:
        for line in Path(args.cases).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            cases.append((parts[0], parts[1], int(parts[2]) if len(parts) > 2 else args.frames))
    elif args.rom and args.state:
        cases.append((args.rom, args.state, args.frames))
    else:
        parser.error("give --rom and --state, or --cases")

    device = Device(args.serial)
    if "EmulatorActivity" in device.foreground():
        sys.exit("The emulator is already in front - another session may be using the device.")
    saved = device.preferences()
    keys = ("video_renderer", "video_internal_resolution", "enable_texture_packs", "video_renderer_debug_tools_enabled")
    out_root = Path(args.out) / time.strftime("%Y%m%d_%H%M%S")
    out_root.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        device.set_preference("video_internal_resolution", args.ir, "string")
        if not args.packs:
            device.set_preference("enable_texture_packs", "false")
        for rom, state, frames in cases:
            print(f"{rom} {state} ({frames} frames)", flush=True)
            try:
                total += len(run_case(device, rom, state, frames, args, out_root))
            except Exception as error:  # one broken case must not stop the sweep
                print(f"  FAILED: {error}")
                device.force_stop()
    finally:
        for key in keys:
            if key in saved:
                value = saved[key]
                device.set_preference(key, str(value).lower() if isinstance(value, bool) else value,
                                      None if isinstance(value, bool) else "string")
        device.force_stop()
    print(f"{total} flagged screens; report and strips in {out_root}")


if __name__ == "__main__":
    main()
