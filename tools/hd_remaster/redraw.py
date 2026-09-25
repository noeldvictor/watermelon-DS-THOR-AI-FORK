"""AI redraws of 2D art (portraits first) through OpenRouter image models.

    python redraw.py models                   image-output models and their prices
    python redraw.py credits                  what the account has used so far
    python redraw.py try work/BSDE a00060 --models <id>,<id> --ref work/BSDE/refs/gades.jpg

The key is read from tools/hd_remaster/.env (OPENROUTER_API_KEY=...), which git ignores, or
from the OPENROUTER_API_KEY environment variable. Every call's cost goes to
work/<CODE>/redraw/ledger.jsonl, and --budget stops a run before that total passes it.

A redraw must drop into the pack in place of the upscaled image, so each result is aligned
back onto the upscale (scale + offset found by correlation) and cut out with the upscale's
own alpha: the outline stays the game's, only what is inside it changes.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
API = "https://openrouter.ai/api/v1"
BG = (128, 128, 128)            # flat background the art is shown on (the alpha is re-applied)


# ---------------------------------------------------------------------------- OpenRouter

def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    env = HERE / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "OPENROUTER_API_KEY":
                key = value.strip().strip('"').strip("'")
    if not key:
        raise SystemExit(f"no OpenRouter key: put OPENROUTER_API_KEY=... in {env}")
    return key


def call(method: str, path: str, body: dict | None = None, timeout: int = 600) -> dict:
    req = urllib.request.Request(
        API + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
                 "X-Title": "Watermelon Thor hd_remaster"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: HTTP {e.code}: {e.read()[:600].decode(errors='replace')}")


def data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def generate(model: str, prompt: str, images: list[Image.Image],
             aspect: str | None = None) -> tuple[Image.Image | None, float, str]:
    """One chat call asking for an image back. Returns (image or None, cost in USD, text).
    aspect ('4:5'...) pins the output shape where the model supports it (Gemini); left to
    itself Gemini sometimes answers in a reference image's shape instead of the first image's."""
    content = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": data_url(im)}} for im in images]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
        "usage": {"include": True},
    }
    if aspect and model.startswith("google/"):
        body["image_config"] = {"aspect_ratio": aspect}
    r = call("POST", "/chat/completions", body)
    cost = float((r.get("usage") or {}).get("cost") or 0.0)
    msg = (r.get("choices") or [{}])[0].get("message") or {}
    urls = [im.get("image_url", {}).get("url", "") for im in msg.get("images") or []]
    if isinstance(msg.get("content"), list):          # some providers put images in content
        urls += [p.get("image_url", {}).get("url", "") for p in msg["content"] if p.get("type") == "image_url"]
    text = msg.get("content") if isinstance(msg.get("content"), str) else ""
    for u in urls:
        if u.startswith("data:image"):
            return Image.open(io.BytesIO(base64.b64decode(u.split(",", 1)[1]))), cost, text or ""
        if u.startswith("http"):
            with urllib.request.urlopen(u, timeout=120) as f:
                return Image.open(io.BytesIO(f.read())), cost, text or ""
    return None, cost, text or json.dumps(r)[:600]


class Ledger:
    """Cost of every call, kept per game so a run can stop at its budget."""

    def __init__(self, work: Path, budget: float):
        self.path = work / "redraw" / "ledger.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.total = sum(json.loads(l)["cost"] for l in self.path.open(encoding="utf-8")) if self.path.exists() else 0.0

    def check(self) -> None:
        if self.total >= self.budget:
            raise SystemExit(f"budget reached: ${self.total:.2f} of ${self.budget:.2f} spent (see {self.path})")

    def add(self, **entry) -> None:
        self.total += entry["cost"]
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}) + "\n")


# ---------------------------------------------------------------------------- alignment

def flat(im: Image.Image, bg=BG) -> Image.Image:
    base = Image.new("RGBA", im.size, bg + (255,))
    base.alpha_composite(im.convert("RGBA"))
    return base.convert("RGB")


def edges(a: np.ndarray) -> np.ndarray:
    g = a.astype(np.float32).mean(axis=2) if a.ndim == 3 else a.astype(np.float32)
    gx = np.zeros_like(g); gy = np.zeros_like(g)
    gx[:, 1:-1] = g[:, 2:] - g[:, :-2]
    gy[1:-1] = g[2:] - g[:-2]
    e = np.hypot(gx, gy)
    return (e - e.mean()) / (e.std() + 1e-6)


def align(out: Image.Image, target: Image.Image) -> tuple[Image.Image, float]:
    """Place `out` over `target`'s frame: search a scale, then the offset by phase correlation
    of edge maps. Returns the aligned image (target's size, RGB) and the match score."""
    tw, th = target.size
    work = 256 / max(tw, th)                         # correlate at a small size
    tsz = (round(tw * work), round(th * work))
    t = edges(np.asarray(target.convert("RGB").resize(tsz, Image.BILINEAR)))
    best = None
    fit = min(tw / out.width, th / out.height), max(tw / out.width, th / out.height)
    for s in np.unique(np.concatenate([np.linspace(fit[0] * 0.9, fit[1] * 1.1, 25), fit])):
        ow, oh = round(out.width * s * work), round(out.height * s * work)
        if ow < 8 or oh < 8:
            continue
        o = edges(np.asarray(out.convert("RGB").resize((ow, oh), Image.BILINEAR)))
        H, W = max(oh, tsz[1]) * 2, max(ow, tsz[0]) * 2
        F = np.fft.rfft2(t, (H, W)) * np.conj(np.fft.rfft2(o, (H, W)))
        c = np.fft.irfft2(F / (np.abs(F) + 1e-3), (H, W))
        y, x = np.unravel_index(np.argmax(c), c.shape)
        dy, dx = (y if y < H // 2 else y - H), (x if x < W // 2 else x - W)
        # score: plain correlation of the overlap at that offset
        ys, xs = max(0, dy), max(0, dx)
        ye, xe = min(tsz[1], dy + oh), min(tsz[0], dx + ow)
        if ye - ys < tsz[1] // 2 or xe - xs < tsz[0] // 2:
            continue
        a = t[ys:ye, xs:xe]; b = o[ys - dy:ye - dy, xs - dx:xe - dx]
        score = float((a * b).mean())
        if best is None or score > best[0]:
            best = (score, s, dx / work, dy / work)
    score, s, dx, dy = best
    canvas = Image.new("RGB", (tw, th), BG)
    canvas.paste(out.convert("RGB").resize((round(out.width * s), round(out.height * s)), Image.LANCZOS),
                 (round(dx), round(dy)))
    return canvas, score


def bleed(rgb: np.ndarray, alpha: np.ndarray, solid: int = 240, iters: int = 24) -> np.ndarray:
    """Colours for the soft edge: every pixel less opaque than `solid` takes the average of its
    already-filled neighbours, growing outward from the solid interior. The model paints on
    the grey background, so its edge pixels are part grey; left as they are, the cut-out shows
    a grey fringe over the game's own background."""
    col = rgb.astype(np.float32).copy()
    ok = alpha >= solid
    H, W = ok.shape
    for _ in range(iters):
        if ok.all():
            break
        acc = np.zeros_like(col); cnt = np.zeros((H, W), np.float32)
        po = np.pad(ok, 1); pc = np.pad(col, ((1, 1), (1, 1), (0, 0)))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    o = po[1 + dy:1 + dy + H, 1 + dx:1 + dx + W]
                    acc += pc[1 + dy:1 + dy + H, 1 + dx:1 + dx + W] * o[..., None]
                    cnt += o
        new = ~ok & (cnt > 0)
        col[new] = acc[new] / cnt[new][:, None]
        ok |= new
    return col


def finish(rgb: np.ndarray, alpha: np.ndarray) -> Image.Image:
    col = bleed(rgb, alpha)
    return Image.fromarray(np.dstack([col, alpha]).clip(0, 255).astype(np.uint8), "RGBA")


def fill_holes(rgb: np.ndarray, alpha: np.ndarray, fallback: np.ndarray, tol: int = 6) -> np.ndarray:
    """Where the redraw shows its flat background inside the game's outline (the model kept an
    outline that the game's own image extends, e.g. hair swept further), use `fallback`
    (the game's upscaled pixels) instead of grey."""
    hole = (np.abs(rgb - np.array(BG, np.float32)).max(axis=2) < tol) & (alpha > 128)
    out = rgb.copy()
    out[hole] = fallback[hole]
    return out


def cut_out(rgb: Image.Image, alpha_from: Image.Image) -> Image.Image:
    src = alpha_from.convert("RGBA").resize(rgb.size, Image.LANCZOS)
    a = np.asarray(src.getchannel("A")).astype(np.float32)
    col = fill_holes(np.asarray(rgb.convert("RGB")).astype(np.float32), a,
                     np.asarray(flat(src)).astype(np.float32))
    return finish(col, a)


# ---------------------------------------------------------------------------- commands

PORTRAIT_PROMPT = """\
Image 1 is a character portrait from the Nintendo DS RPG Lufia: Curse of the Sinistrals, \
enlarged from a tiny 128x160 pixel original, so the face, eyes and fine details are blurry or \
malformed. Image 2 is the official character artwork of the same character ({name}), to show \
what his face, eyes, hair and outfit really look like.

Redraw image 1 as a clean, sharp, high-resolution version of the same painting:
- Keep exactly the same framing, crop, pose, head angle, silhouette, colours and lighting as \
image 1. It must line up with image 1 when laid on top of it.
- Keep the original painted look and the same expression. Do not add or remove anything, and \
keep the flat grey background.
- Fix what the low resolution destroyed: clean, correct eyes, eyebrows, nose, mouth and facial \
markings matching the official artwork; crisp hair strands and armour edges.
Return only the image, with the same aspect ratio as image 1."""


def cmd_models(args) -> None:
    ms = call("GET", "/models")["data"]
    rows = []
    for m in ms:
        arch = m.get("architecture") or {}
        if "image" in (arch.get("output_modalities") or []):
            p = m.get("pricing") or {}
            rows.append((m["id"], ",".join(arch.get("input_modalities") or []), p.get("prompt"),
                         p.get("completion"), p.get("image"), p.get("request")))
    for r in sorted(rows):
        print(f"{r[0]:<52} in={r[1]:<22} prompt={r[2]} completion={r[3]} image={r[4]} request={r[5]}")


def cmd_credits(args) -> None:
    d = call("GET", "/credits")["data"]
    print(f"credits ${d.get('total_credits', 0):.2f}, used ${d.get('total_usage', 0):.2f}")


def label(img: Image.Image, text: str) -> Image.Image:
    img = img.copy()
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arialbd.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    x0, y0, x1, y1 = d.textbbox((0, 0), text, font=font)
    d.rectangle((0, 0, x1 - x0 + 12, y1 - y0 + 12), fill=(0, 0, 0))
    d.text((6 - x0, 6 - y0), text, font=font, fill=(255, 255, 255))
    return img


def cmd_try(args) -> None:
    work = Path(args.work)
    ledger = Ledger(work, args.budget)
    native = Image.open(work / "native" / "assets2d" / f"{args.key}.png").convert("RGBA")
    up = Image.open(work / "upscaled" / "assets2d" / f"{args.key}.png").convert("RGBA")
    base = flat(up).resize((up.width * 2, up.height * 2), Image.LANCZOS)   # the model sees more pixels
    refs = [Image.open(r).convert("RGB") for r in args.ref]
    prompt = PORTRAIT_PROMPT.format(name=args.name)
    outdir = work / "redraw" / "try" / args.key
    outdir.mkdir(parents=True, exist_ok=True)
    tiles = [label(flat(native).resize(up.size, Image.NEAREST), "native"), label(flat(up), "4x upscale")]
    for model in args.models.split(","):
        ledger.check()
        t0 = time.time()
        try:
            img, cost, text = generate(model, prompt, [base] + refs)
        except RuntimeError as e:
            print(f"{model}: {e}")
            continue
        ledger.add(model=model, key=args.key, cost=cost, seconds=round(time.time() - t0, 1),
                   ok=img is not None)
        if img is None:
            print(f"{model}: no image (${cost:.4f}): {text[:300]}")
            continue
        safe = model.replace("/", "_").replace(":", "_")
        img.save(outdir / f"{safe}_raw.png")
        aligned, score = align(img, flat(up))
        result = cut_out(aligned, up)
        result.save(outdir / f"{safe}.png")
        print(f"{model}: {img.size[0]}x{img.size[1]} in {time.time() - t0:.0f}s, ${cost:.4f}, "
              f"alignment score {score:.2f}")
        tiles.append(label(flat(result), model.split("/")[-1]))
    sheet = Image.new("RGB", (sum(t.width for t in tiles) + 8 * (len(tiles) - 1), tiles[0].height), (255, 255, 255))
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0)); x += t.width + 8
    sheet.save(outdir / "compare.png")
    print(f"sheet: {outdir / 'compare.png'}; spent so far ${ledger.total:.4f} of ${args.budget:.2f}")


EXPRESSION_PROMPT = """\
Image 1 is a finished high-resolution portrait of {name} from Lufia: Curse of the Sinistrals. \
Image 2 is the same portrait from the game with a different facial expression ("{expr}"), but \
blurry and low resolution. Image 3 is official artwork of {name} for reference.

Redraw image 1 with the facial expression of image 2, matched exactly: the same eye \
openness and gaze, eyebrow shape, mouth shape and how far it is open, and the same shadows and \
lighting on the face (if image 2's face is in dark shadow, so is yours). Do not exaggerate or \
soften the expression. Change anything else that differs in image 2 too, drawn in the clean \
style of image 1. Keep everything else exactly as in image 1: framing, pose, hair, armour, \
colours and the flat grey background. It must line up with image 1 when laid on top of it.
Return only the image, with the same aspect ratio as image 1."""


def read_manifest(work: Path) -> list[dict]:
    return [json.loads(l) for l in (work / "manifest.jsonl").open(encoding="utf-8")]


def box_blur(a: np.ndarray, r: int) -> np.ndarray:
    """Mean over a (2r+1)^2 box, edges clamped."""
    if r <= 0:
        return a
    p = np.pad(a, r, mode="edge").cumsum(0).cumsum(1)
    p = np.pad(p, ((1, 0), (1, 0)))
    n = 2 * r + 1
    return (p[n:, n:] - p[:-n, n:] - p[n:, :-n] + p[:-n, :-n]) / (n * n)


def change_mask(native: Image.Image, native_master: Image.Image, scale: int, grow: int, feather: int) -> np.ndarray:
    """Where an expression differs from the master, at output size: the native pixels that
    changed, grown by `grow` native pixels, softened over about `feather` output pixels."""
    a = np.asarray(native.convert("RGBA")).astype(np.int16)
    b = np.asarray(native_master.convert("RGBA")).astype(np.int16)
    m = (np.abs(a - b).max(axis=2) > 8).astype(np.float32)
    m = (box_blur(m, grow) > 0).astype(np.float32)
    m = np.kron(m, np.ones((scale, scale), np.float32))
    return np.clip(box_blur(m, feather // 2) * 2, 0, 1)


ASPECTS = {"1:1": 1, "2:3": 2 / 3, "3:2": 3 / 2, "3:4": 3 / 4, "4:3": 4 / 3, "4:5": 4 / 5,
           "5:4": 5 / 4, "9:16": 9 / 16, "16:9": 16 / 9, "21:9": 21 / 9}


def nearest_aspect(w: int, h: int) -> str:
    return min(ASPECTS, key=lambda k: abs(np.log(ASPECTS[k] / (w / h))))


def cmd_portrait(args) -> None:
    """Redraw every expression of one character: the master first, then each expression as an
    edit of the master. Only where the game's own expression differs from the master (the face,
    grown a little and softened) comes from the expression's redraw; everything else is the
    master's pixels, so the body doesn't change between expressions."""
    work = Path(args.work)
    ledger = Ledger(work, args.budget)
    items = {m["source"].split("/")[-1].split(".")[0][len(args.match):]: m
             for m in read_manifest(work) if m["kind"] == "asset2d" and args.match in m["source"]}
    if args.master not in items:
        raise SystemExit(f"no '{args.match}{args.master}' in the manifest; found {sorted(items)}")
    refs = [Image.open(r).convert("RGB") for r in args.ref]
    out_dir = work / "redrawn" / "assets2d"
    out_dir.mkdir(parents=True, exist_ok=True)
    keep_dir = work / "redraw" / "portrait" / args.match.rstrip("_")
    keep_dir.mkdir(parents=True, exist_ok=True)

    def up_of(m):
        return Image.open(work / "upscaled" / "assets2d" / f"{m['key']}.png").convert("RGBA")

    def run(prompt, images, tag):
        if args.reuse:                                     # rebuild from earlier model output
            saved = (sorted(keep_dir.glob(f"{tag}_best.png"))
                     or sorted(keep_dir.glob(f"{tag}_raw*.png"), key=lambda f: f.stat().st_mtime))
            if saved:
                print(f"  {tag}: reusing {saved[-1].name}")
                return Image.open(saved[-1])
        for attempt in range(args.tries):
            ledger.check()
            t0 = time.time()
            img, cost, text = generate(args.model, prompt, images, aspect)
            ledger.add(model=args.model, key=tag, cost=cost, seconds=round(time.time() - t0, 1), ok=img is not None)
            if img is not None:
                img.save(keep_dir / f"{tag}_raw{time.strftime('%Y%m%d-%H%M%S')}.png")
                print(f"  {tag}: {img.size[0]}x{img.size[1]} in {time.time() - t0:.0f}s, ${cost:.4f}")
                return img
            print(f"  {tag}: no image (${cost:.4f}): {text[:200]}")
        raise SystemExit(f"{tag}: no image after {args.tries} tries")

    m0 = items[args.master]
    up0 = up_of(m0)
    scale = up0.width // m0["w"]
    aspect = nearest_aspect(up0.width, up0.height)
    if args.master_image:                                  # a raw model output, e.g. from `try`
        raw = Image.open(args.master_image)
    else:
        raw = run(PORTRAIT_PROMPT.format(name=args.name),
                  [flat(up0).resize((up0.width * 2, up0.height * 2), Image.LANCZOS)] + refs, args.master)
    aligned, score = align(raw, flat(up0))
    print(f"  {args.master}: alignment {score:.2f}")
    master = cut_out(aligned, up0)
    master.save(out_dir / f"{m0['key']}.png")
    sheet = [label(flat(master), args.master)]
    native0 = Image.open(work / "native" / "assets2d" / f"{m0['key']}.png")
    mrgb = np.asarray(flat(master)).astype(np.float32)
    ma = np.asarray(master.getchannel("A")).astype(np.float32)

    for expr, m in sorted(items.items()):
        if expr == args.master or (args.only and expr not in args.only.split(",")):
            continue
        up = up_of(m)
        native = Image.open(work / "native" / "assets2d" / f"{m['key']}.png")
        w = change_mask(native, native0, scale, args.grow, args.feather)
        if not w.any():
            master.save(out_dir / f"{m['key']}.png")
            continue
        best = None
        for attempt in range(args.tries):                  # a badly aligned answer leaves ghosts
            raw = run(EXPRESSION_PROMPT.format(name=args.name, expr=expr),
                      [flat(master).resize((up.width * 2, up.height * 2), Image.LANCZOS),
                       flat(up).resize((up.width * 2, up.height * 2), Image.LANCZOS)] + refs, expr)
            aligned, score = align(raw, flat(up))          # scored against its own expression
            if best is None or score > best[1]:
                best = (aligned, score, raw)
            if score >= args.min_score or args.reuse:
                break
            print(f"  {expr}: alignment {score:.2f} < {args.min_score}, asking again")
        aligned, score, raw = best
        if not args.reuse:
            raw.save(keep_dir / f"{expr}_best.png")        # what --reuse rebuilds from
        ua = np.asarray(up.getchannel("A")).astype(np.float32)
        own = fill_holes(np.asarray(aligned).astype(np.float32), ua, np.asarray(flat(up)).astype(np.float32))
        rgb = own * w[..., None] + mrgb * (1 - w[..., None])
        a = ua * w + ma * (1 - w)
        res = finish(rgb, a)
        res.save(out_dir / f"{m['key']}.png")
        print(f"  {expr}: {100 * (w > 0).mean():.0f}% of the frame changed, alignment {score:.2f}")
        sheet.append(label(flat(res), expr))
    s = Image.new("RGB", (sum(t.width for t in sheet) + 8 * (len(sheet) - 1), sheet[0].height), (255, 255, 255))
    x = 0
    for t in sheet:
        s.paste(t, (x, 0)); x += t.width + 8
    s.save(keep_dir / "sheet.png")
    print(f"wrote {len(items)} images to {out_dir}; sheet {keep_dir / 'sheet.png'}; "
          f"spent so far ${ledger.total:.4f} of ${args.budget:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models", help="image-output models and prices").set_defaults(fn=cmd_models)
    sub.add_parser("credits", help="account usage").set_defaults(fn=cmd_credits)
    p = sub.add_parser("try", help="redraw one image with several models, side by side")
    p.add_argument("work"); p.add_argument("key")
    p.add_argument("--models", required=True, help="comma-separated OpenRouter model ids")
    p.add_argument("--ref", action="append", default=[], help="reference image (repeatable)")
    p.add_argument("--name", default="the character", help="character name for the prompt")
    p.add_argument("--budget", type=float, default=20.0, help="stop before the game's ledger passes this (USD)")
    p.set_defaults(fn=cmd_try)
    p = sub.add_parser("portrait", help="redraw every expression of one character into work/<CODE>/redrawn")
    p.add_argument("work")
    p.add_argument("--match", required=True, help="source-name prefix of the character, e.g. talk_f_gades_")
    p.add_argument("--master", default="normal", help="expression redrawn first; the others are edits of it")
    p.add_argument("--master-image", help="use this raw model output as the master (e.g. a `try` *_raw.png)")
    p.add_argument("--reuse", action="store_true", help="rebuild from the saved model outputs, no new calls")
    p.add_argument("--model", default="google/gemini-3-pro-image")
    p.add_argument("--ref", action="append", default=[], help="reference image (repeatable)")
    p.add_argument("--name", default="the character", help="character name for the prompt")
    p.add_argument("--grow", type=int, default=3, help="grow the changed area by this many native pixels")
    p.add_argument("--feather", type=int, default=12, help="blend width at its edge, in output pixels")
    p.add_argument("--only", help="comma-separated expressions to redo (the master is reused from --master-image)")
    p.add_argument("--tries", type=int, default=2, help="calls per image when one returns nothing or aligns badly")
    p.add_argument("--min-score", type=float, default=0.85, help="alignment score below which an expression is redrawn")
    p.add_argument("--budget", type=float, default=20.0, help="stop before the game's ledger passes this (USD)")
    p.set_defaults(fn=cmd_portrait)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
