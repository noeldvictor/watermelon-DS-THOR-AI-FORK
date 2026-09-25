"""Before/after image for a game's README and GAMES.md.

    python beforeafter.py off.png on.png --crop X,Y,W,H -o games/<CODE>/media/<scene>.jpg

off.png / on.png: the same frame captured with the pack off and on (see games/README.md,
"Screenshots"). The same box is cut out of both and they are placed side by side, "Original" on
the left and "HD pack" on the right, scaled to --height.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def label(img: Image.Image, text: str) -> None:
    d = ImageDraw.Draw(img)
    size = max(18, img.height // 18)
    font = None
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(name, size)
            break
        except OSError:
            pass
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:                               # Pillow < 10.1 has one fixed size
            font = ImageFont.load_default()
    x0, y0, x1, y1 = d.textbbox((0, 0), text, font=font)
    pad = size // 3
    d.rectangle((0, 0, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), fill=(0, 0, 0))
    d.text((pad - x0, pad - y0), text, font=font, fill=(255, 255, 255))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("off", help="capture with the pack off")
    ap.add_argument("on", help="the same frame with the pack on")
    ap.add_argument("--crop", required=True, help="X,Y,W,H box in the captures")
    ap.add_argument("--height", type=int, default=540, help="output height in pixels (default 540)")
    ap.add_argument("-o", "--out", required=True, help="output .jpg")
    args = ap.parse_args()

    x, y, w, h = (int(v) for v in args.crop.split(","))
    halves = []
    for path, text in ((args.off, "Original"), (args.on, "HD pack")):
        img = Image.open(path).convert("RGB").crop((x, y, x + w, y + h))
        if h != args.height:
            img = img.resize((round(w * args.height / h), args.height), Image.LANCZOS)
        label(img, text)
        halves.append(img)
    gap = 4
    out = Image.new("RGB", (halves[0].width * 2 + gap, args.height), (255, 255, 255))
    out.paste(halves[0], (0, 0))
    out.paste(halves[1], (halves[0].width + gap, 0))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    out.save(args.out, quality=88, optimize=True)
    print(f"{args.out}: {out.width}x{out.height}")


if __name__ == "__main__":
    main()
