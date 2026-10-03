"""In-game PNG screenshots of an HD pack for a game's README (the standard: every games/<CODE>/
README.md shows the pack in game, as PNG, captured on the Thor).

    python gameshots.py "<ROM file on the SD>" <state> <out.png> [--seconds 3] [--serial S]

<state> is a private save state in the app's files (e.g. rv_kotori.ml) or slot:N. The state runs
live for a few seconds before the capture: a paused frame is composed without the 2D sprite pass,
so sprites would show native. Both screens are cropped to the DS picture (top 1440x1080, bottom
1240x930 on the Thor) and stacked, top over bottom, at full resolution: PNG keeps the HD detail a
JPEG would blur. The emulator is closed afterwards.
"""
from __future__ import annotations

import argparse
import io
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "frame_compare"))
import frame_compare as fc  # noqa: E402  (Device, launch)

BOTTOM_DISPLAY = "4630946482288158084"
TOP_CROP = (240, 0, 1680, 1080)      # the DS picture on the 1920x1080 top display
BOTTOM_CROP = (0, 75, 1240, 1005)    # and on the 1240x1080 bottom display


def capture(device: fc.Device, display: str | None) -> Image.Image:
    args = ["exec-out", "screencap", "-p"] + (["-d", display] if display else [])
    return Image.open(io.BytesIO(device.adb(*args, binary=True))).convert("RGB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("state")
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--settle", type=float, default=2.0,
                    help="seconds to wait after the game runs before loading the state (big 7z ROMs: 10+)")
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()

    device = fc.Device(args.serial or "c3ca0370")
    front = device.foreground()
    if front and "launcher" not in front and fc.PACKAGE not in front:
        raise SystemExit(f"the Thor is in use ({front}); try again later")
    # loading the state writes its save memory into the game's .sav: SaveGuard puts the user's back
    with fc.SaveGuard(device, args.rom):
        fc.launch(device, args.rom, settle_s=args.settle)
        if args.state.startswith("slot:"):
            extras = ["--ei", "slot", args.state[5:]]
        else:
            extras = ["--es", "path", f"/data/user/0/{fc.PACKAGE}/files/{args.state}"]
        # twice: right after a big 7z ROM boots a load can report success and still be lost
        # (Chrono Trigger came back on its intro movie)
        for _ in range(2):
            code, data = device.broadcast("LOAD_STATE", *extras)
            if code != 1 or "success=1" not in data:
                raise SystemExit(f"LOAD_STATE {args.state}: {data}")
            time.sleep(1.5)
        time.sleep(args.seconds)
        # the Thor's own overlay panel can cover the bottom screen after a launch; a tap below the
        # DS screen closes it (and ends the burn-in refresh). Not BACK: with no panel open, BACK
        # closes the emulator's bottom-screen presentation and the Thor's launcher shows there
        device.adb("shell", "input", "-d", "4", "tap", "620", "1060", check=False)
        time.sleep(0.5)
        if "EmulatorActivity" not in device.foreground():
            raise SystemExit("the emulator left the front during the capture; not saving")
        top = capture(device, None).crop(TOP_CROP)
        bottom = capture(device, BOTTOM_DISPLAY).crop(BOTTOM_CROP)
    sheet = Image.new("RGB", (top.width, top.height + bottom.height), (0, 0, 0))
    sheet.paste(top, (0, 0))
    sheet.paste(bottom, ((top.width - bottom.width) // 2, top.height))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, optimize=True)
    print(f"{out}: {sheet.width}x{sheet.height}, {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
