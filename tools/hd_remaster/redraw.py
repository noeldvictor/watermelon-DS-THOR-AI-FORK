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
Image 2 is the same portrait from the game with a different facial expression ({expr}), but \
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
    out_dir = Path(args.out) if args.out else work / "redrawn" / "assets2d"
    out_dir.mkdir(parents=True, exist_ok=True)
    keep_dir = work / "redraw" / "portrait" / (args.match.rstrip("_") + ("_" + args.tag if args.tag else ""))
    keep_dir.mkdir(parents=True, exist_ok=True)

    def up_of(m):
        return Image.open(work / "upscaled" / "assets2d" / f"{m['key']}.png").convert("RGBA")

    def native_of(m, size):
        """The game's own pixels, enlarged without smoothing, as the last image (--with-native)."""
        if not args.with_native:
            return []
        n = Image.open(work / "native" / "assets2d" / f"{m['key']}.png").convert("RGBA")
        return [flat(n).resize(size, Image.NEAREST)]

    NATIVE_LINE = ("\nThe last image is the same portrait at the game's true resolution, enlarged "
                   "without smoothing, so it looks pixelated. The smooth enlargement can blur or "
                   "invent features; trust the pixelated one for the colours, the eye colour, and "
                   "whether the eyes and mouth are open or closed.")

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

    def describe(up) -> str:
        """The game's face in words (a vision model reads it), for the prompt."""
        res, cost, _ = ask_json(CHECK_MODEL, DESCRIBE_PROMPT, [flat(up)])
        ledger.add(model=CHECK_MODEL, key="describe", cost=cost, seconds=0, ok=res is not None)
        if not res:
            return ""
        return (f"{res.get('summary', '')}: eyes {res.get('eyes', '')}; eyebrows {res.get('eyebrows', '')}; "
                f"mouth {res.get('mouth', '')}; eye colour {res.get('eye_colour', '')}")

    def verify(up, result) -> tuple[bool, list[str]]:
        res, cost, text = ask_json(CHECK_MODEL, CHECK_PROMPT, [flat(up), flat(result)])
        ledger.add(model=CHECK_MODEL, key="verify", cost=cost, seconds=0, ok=res is not None)
        return check_ok(res or {"error": text[:200]})

    m0 = items[args.master]
    up0 = up_of(m0)
    scale = up0.width // m0["w"]
    aspect = nearest_aspect(up0.width, up0.height)
    master_ok = True
    for attempt in range(args.tries if args.verify and not args.master_image else 1):
        if args.master_image:                              # a raw model output, e.g. from `try`
            raw = Image.open(args.master_image)
        else:
            prompt = PORTRAIT_PROMPT.format(name=args.name)
            if args.auto_hint:
                prompt += (f"\nThe face in image 1 shows: {describe(up0)}. Keep exactly that, "
                           f"including the eye colour.")
            size0 = (up0.width * 2, up0.height * 2)
            raw = run(prompt + (NATIVE_LINE if args.with_native else ""),
                      [flat(up0).resize(size0, Image.LANCZOS)] + refs + native_of(m0, size0), args.master)
        aligned, score = align(raw, flat(up0))
        master = cut_out(aligned, up0)
        master_ok, why = verify(up0, master) if args.verify and not args.master_image else (True, [])
        print(f"  {args.master}: alignment {score:.2f}" + ("" if master_ok else f", check failed: {'; '.join(why)}"))
        if master_ok:
            break
    # a master that fails the check still anchors the other expressions, but isn't shown itself
    if master_ok:
        master.save(out_dir / f"{m0['key']}.png")
    sheet = [label(flat(master), args.master)]
    native0 = Image.open(work / "native" / "assets2d" / f"{m0['key']}.png")
    hints = dict(h.split("=", 1) for h in args.hint)
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
        # the game's expression names can mislead ('amazed' is an exasperated wince), so a
        # --hint (or --auto-hint, a vision model's reading) describes what image 2 shows
        desc = hints.get(expr) or (describe(up) if args.auto_hint else "") or f'"{expr}"'
        ua = np.asarray(up.getchannel("A")).astype(np.float32)
        best = None
        for attempt in range(args.tries):                  # badly aligned or failing the check: again
            size = (up.width * 2, up.height * 2)
            raw = run(EXPRESSION_PROMPT.format(name=args.name, expr=desc) + (NATIVE_LINE if args.with_native else ""),
                      [flat(master).resize(size, Image.LANCZOS), flat(up).resize(size, Image.LANCZOS)] + refs
                      + native_of(m, size), expr)
            aligned, score = align(raw, flat(up))          # scored against its own expression
            own = fill_holes(np.asarray(aligned).astype(np.float32), ua, np.asarray(flat(up)).astype(np.float32))
            rgb = own * w[..., None] + mrgb * (1 - w[..., None])
            res = finish(rgb, ua * w + ma * (1 - w))
            ok, why = verify(up, res) if args.verify else (score >= args.min_score, [f"alignment {score:.2f}"])
            if best is None or (ok, score) > (best[0], best[2]):
                best = (ok, res, score, raw, why)
            if ok or args.reuse:
                break
            print(f"  {expr}: {'; '.join(why)}; asking again")
        ok, res, score, raw, why = best
        if not args.reuse:
            raw.save(keep_dir / f"{expr}_best.png")        # what --reuse rebuilds from
        if ok or not args.verify:
            res.save(out_dir / f"{m['key']}.png")
        else:                                              # failed twice: the game's upscale stays
            res.save(keep_dir / f"{expr}_failed.png")
            (out_dir / f"{m['key']}.png").unlink(missing_ok=True)
        print(f"  {expr}: {'ok' if ok else 'FAILED: ' + '; '.join(why)}, alignment {score:.2f}")
        sheet.append(label(flat(res), expr + ("" if ok else " (failed)")))
    s = Image.new("RGB", (sum(t.width for t in sheet) + 8 * (len(sheet) - 1), sheet[0].height), (255, 255, 255))
    x = 0
    for t in sheet:
        s.paste(t, (x, 0)); x += t.width + 8
    s.save(keep_dir / "sheet.png")
    print(f"wrote {len(items)} images to {out_dir}; sheet {keep_dir / 'sheet.png'}; "
          f"spent so far ${ledger.total:.4f} of ${args.budget:.2f}")


SHEET_PROMPT = """\
Image 1 is a finished high-resolution close-up of {name} from Lufia: Curse of the Sinistrals, \
with a neutral expression. Image 2 is a 2x2 grid of four close-ups of the same character with \
different facial expressions, blurry and low resolution.{ref_line}

Redraw image 2 as the same 2x2 grid: each of the four cells redrawn cleanly in the style and \
detail of image 1, with that cell's own expression copied exactly. Where a cell's eyes are \
closed or squeezed shut, keep them closed; where its mouth is open, keep it open by the same \
amount, with teeth showing only if they show in that cell; keep that cell's eyebrows, gaze and \
face shadows. Do not use image 1's expression, and do not exaggerate or soften any expression. \
Keep each cell's framing, pose, hair and colours exactly as in that cell, the flat grey \
background, and no borders, gaps or labels between cells.
Return only the image, with the same aspect ratio as image 2."""


def load_native(work: Path, key: str) -> Image.Image:
    return Image.open(work / "native" / "assets2d" / f"{key}.png").convert("RGBA")


def load_up(work: Path, key: str) -> Image.Image:
    return Image.open(work / "upscaled" / "assets2d" / f"{key}.png").convert("RGBA")


def group_expressions(work: Path, exprs: dict[str, str], min_iou: float = 0.85) -> list[list[str]]:
    """Split a character's expressions into groups that share the master's frame: same native
    size and a silhouette overlapping it by `min_iou`. The game cuts some expressions to other
    sizes or poses (Gemine's angry is 128x112, Maxim's m* set is another pose); each group gets
    its own master. The master is 'normal' where there is one."""
    left = sorted(exprs)
    groups = []
    while left:
        head = next((e for e in left if e == "normal"), None) or next((e for e in left if "normal" in e), left[0])
        a0 = np.asarray(load_native(work, exprs[head]))[..., 3] > 0
        group = [head]
        for e in left:
            if e == head:
                continue
            a = np.asarray(load_native(work, exprs[e]))[..., 3] > 0
            if a.shape == a0.shape and (a & a0).sum() / max(1, (a | a0).sum()) >= min_iou:
                group.append(e)
        groups.append(group)
        left = [e for e in left if e not in group]
    return groups


def face_box(work: Path, exprs: dict[str, str], group: list[str], pad: int = 4) -> tuple[int, int, int, int]:
    """Native-pixel box (4:3) around where the group's expressions differ from its master."""
    n0 = np.asarray(load_native(work, exprs[group[0]])).astype(np.int16)
    h, w = n0.shape[:2]
    acc = np.zeros((h, w), bool)
    for e in group[1:]:
        acc |= np.abs(np.asarray(load_native(work, exprs[e])).astype(np.int16) - n0).max(axis=2) > 40
    if acc.sum() < 16:
        return (0, 0, w, h)
    ys, xs = np.nonzero(acc)
    x0, x1 = np.percentile(xs, 1) - pad, np.percentile(xs, 99) + 1 + pad
    y0, y1 = np.percentile(ys, 1) - pad, np.percentile(ys, 99) + 1 + pad
    bw, bh = x1 - x0, y1 - y0
    if bw / bh < 4 / 3:                                   # widen or heighten to 4:3
        x0 -= (bh * 4 / 3 - bw) / 2; bw = bh * 4 / 3
    else:
        y0 -= (bw * 3 / 4 - bh) / 2; bh = bw * 3 / 4
    bw, bh = min(bw, w), min(bh, h)
    x0 = min(max(0, x0), w - bw); y0 = min(max(0, y0), h - bh)
    return (int(round(x0)), int(round(y0)), int(round(x0 + bw)), int(round(y0 + bh)))


def box_window(size: tuple[int, int], box: tuple[int, int, int, int], feather: int) -> np.ndarray:
    """1 inside `box`, fading to 0 over `feather` pixels at its edges (0 outside)."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.minimum.reduce([xx - box[0], box[2] - 1 - xx, yy - box[1], box[3] - 1 - yy]).astype(np.float32)
    return np.clip((d + 1) / feather, 0, 1)


def cmd_cast(args) -> None:
    """Every character matching --prefix: a single-call master per expression group, the other
    expressions of the group redrawn four at a time as a 2x2 sheet of face close-ups."""
    work = Path(args.work)
    ledger = Ledger(work, args.budget)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {}
    names, refmap = cfg.get("names", {}), cfg.get("refs", {})
    chars: dict[str, dict[str, str]] = {}
    for m in read_manifest(work):
        stem = m["source"].split("/")[-1].split(".")[0] if m["kind"] == "asset2d" else ""
        if stem.startswith(args.prefix):
            ch, _, ex = stem[len(args.prefix):].partition("_")
            chars.setdefault(ch, {})[ex or "normal"] = m["key"]
    only = set(args.only.split(",")) if args.only else None
    out_dir = work / "redrawn" / "assets2d"
    out_dir.mkdir(parents=True, exist_ok=True)
    root = work / "redraw" / "cast"

    def call_model(prompt, images, aspect, keep: Path, tag: str) -> Image.Image:
        saved = sorted(keep.glob(f"{tag}_raw*.png"), key=lambda f: f.stat().st_mtime)
        if args.reuse and saved:
            return Image.open(saved[-1])
        for _ in range(args.tries):
            ledger.check()
            t0 = time.time()
            img, cost, text = generate(args.model, prompt, images, aspect)
            ledger.add(model=args.model, key=f"{keep.name}/{tag}", cost=cost, seconds=round(time.time() - t0, 1),
                       ok=img is not None)
            if img is not None:
                img.save(keep / f"{tag}_raw{time.strftime('%Y%m%d-%H%M%S')}.png")
                return img
            print(f"    {tag}: no image (${cost:.4f}): {text[:160]}")
        raise RuntimeError(f"{tag}: no image")

    for ch in sorted(chars):
        if only and ch not in only:
            continue
        exprs = chars[ch]
        if not args.force and all((out_dir / f"{k}.png").exists() for k in exprs.values()):
            print(f"{ch}: done already")
            continue
        name = names.get(ch, ch.capitalize())
        refs = [Image.open(work / "refs" / r).convert("RGB") for r in refmap.get(ch, [])]
        keep = root / ch
        keep.mkdir(parents=True, exist_ok=True)
        groups = group_expressions(work, exprs)
        print(f"{ch} ({name}): {len(exprs)} expressions in {len(groups)} group(s), {len(refs)} reference(s); "
              f"${ledger.total:.2f} spent")
        sheet_tiles = []
        for group in groups:
            head = group[0]
            up0 = load_up(work, exprs[head])
            scale = up0.width // load_native(work, exprs[head]).width
            try:
                raw = call_model(PORTRAIT_PROMPT.format(name=name) if refs else PORTRAIT_PROMPT_NOREF.format(name=name),
                                 [flat(up0).resize((up0.width * 2, up0.height * 2), Image.LANCZOS)] + refs,
                                 nearest_aspect(up0.width, up0.height), keep, head)
            except RuntimeError as e:
                print(f"    {e}")
                continue
            aligned, score = align(raw, flat(up0))
            master = cut_out(aligned, up0)
            master.save(out_dir / f"{exprs[head]}.png")
            sheet_tiles.append(label(flat(master), head))
            print(f"    {head}: master, alignment {score:.2f}")
            rest = group[1:]
            if not rest:
                continue
            box = face_box(work, exprs, group)
            bx = tuple(v * scale for v in box)
            cw, ch_ = (bx[2] - bx[0]), (bx[3] - bx[1])
            f = max(1.0, 640 / cw)                          # cells at least 640 px wide for the model
            cw, ch_ = round(cw * f), round(ch_ * f)
            mface = flat(master).crop(bx).resize((cw, ch_), Image.LANCZOS)
            mrgb = np.asarray(flat(master)).astype(np.float32)
            ma = np.asarray(master.getchannel("A")).astype(np.float32)
            win = box_window(master.size, bx, args.feather)
            native0 = load_native(work, exprs[head])
            for si in range(0, len(rest), 4):
                part = rest[si:si + 4]
                sheet = Image.new("RGB", (cw * 2, ch_ * 2), BG)
                for i in range(4):
                    src = flat(load_up(work, exprs[part[i]])).crop(bx).resize((cw, ch_), Image.LANCZOS) \
                        if i < len(part) else mface
                    sheet.paste(src, ((i % 2) * cw, (i // 2) * ch_))
                ref_line = f" Image 3 is official artwork of {name} for reference." if refs else ""
                try:
                    raw = call_model(SHEET_PROMPT.format(name=name, ref_line=ref_line), [mface, sheet] + refs,
                                     nearest_aspect(sheet.width, sheet.height), keep, f"{head}_sheet{si // 4}")
                except RuntimeError as e:
                    print(f"    {e}")
                    continue
                W, H = raw.size
                for i, e in enumerate(part):
                    cell = raw.crop(((i % 2) * W // 2, (i // 2) * H // 2, (i % 2 + 1) * W // 2, (i // 2 + 1) * H // 2))
                    up = load_up(work, exprs[e])
                    target = flat(up).crop(bx)
                    al, sc = align(cell, target)
                    own = np.asarray(flat(master)).astype(np.float32).copy()
                    own[bx[1]:bx[3], bx[0]:bx[2]] = np.asarray(al).astype(np.float32)
                    ua = np.asarray(up.getchannel("A")).astype(np.float32)
                    own = fill_holes(own, ua, np.asarray(flat(up)).astype(np.float32))
                    w = change_mask(load_native(work, exprs[e]), native0, scale, args.grow, args.feather) * win
                    rgb = own * w[..., None] + mrgb * (1 - w[..., None])
                    res = finish(rgb, ua * w + ma * (1 - w))
                    res.save(out_dir / f"{exprs[e]}.png")
                    sheet_tiles.append(label(flat(res), e))
                    print(f"    {e}: sheet {si // 4} cell {i}, alignment {sc:.2f}")
        if sheet_tiles:
            hmax = max(t.height for t in sheet_tiles)
            s = Image.new("RGB", (sum(t.width for t in sheet_tiles) + 8 * (len(sheet_tiles) - 1), hmax), (255, 255, 255))
            x = 0
            for t in sheet_tiles:
                s.paste(t, (x, 0)); x += t.width + 8
            s.save(keep / "sheet.png")
    print(f"spent ${ledger.total:.2f} of ${args.budget:.2f} ({ledger.path})")


LOGO_PROMPT = """\
Image 1 is {what} from the Nintendo DS game Lufia: Curse of the Sinistrals, enlarged from a \
low-resolution original, so its edges, lettering and metallic shading are soft and jagged. \
Image 2 is the official high-resolution artwork of the same logo.

Redraw image 1 as a clean, sharp, high-resolution version: the same letters, layout, size, \
position, outline and colours as image 1 (it must line up with image 1 when laid on top of it), \
with the crisp lettering, bevels and shading of image 2. Keep the flat grey background and add \
nothing.
Return only the image, with the same aspect ratio as image 1."""


def cmd_one(args) -> None:
    """Redraw one image (a texture or a 2D asset) with its reference, e.g. a title logo. The
    input is padded to the nearest shape the model can return, and the padding cut off after."""
    work = Path(args.work)
    ledger = Ledger(work, args.budget)
    up = Image.open(work / "upscaled" / args.kind / f"{args.key}.png").convert("RGBA")
    aspect = nearest_aspect(up.width, up.height)
    ar = ASPECTS[aspect]
    cw, chh = (up.width, round(up.width / ar)) if up.width / up.height > ar else (round(up.height * ar), up.height)
    ox, oy = (cw - up.width) // 2, (chh - up.height) // 2
    canvas = Image.new("RGB", (cw, chh), BG)
    canvas.paste(flat(up), (ox, oy))
    refs = [Image.open(r).convert("RGB") for r in args.ref]
    keep = work / "redraw" / "one" / args.key
    keep.mkdir(parents=True, exist_ok=True)
    saved = sorted(keep.glob("raw*.png"), key=lambda f: f.stat().st_mtime)
    if args.reuse and saved:
        raw = Image.open(saved[-1])
    else:
        ledger.check()
        t0 = time.time()
        raw, cost, text = generate(args.model, LOGO_PROMPT.format(what=args.what),
                                   [canvas.resize((cw * 2, chh * 2), Image.LANCZOS)] + refs, aspect)
        ledger.add(model=args.model, key=args.key, cost=cost, seconds=round(time.time() - t0, 1), ok=raw is not None)
        if raw is None:
            raise SystemExit(f"no image (${cost:.4f}): {text[:300]}")
        raw.save(keep / f"raw{time.strftime('%Y%m%d-%H%M%S')}.png")
        print(f"{raw.size[0]}x{raw.size[1]} in {time.time() - t0:.0f}s, ${cost:.4f}")
    aligned, score = align(raw, canvas)
    res = cut_out(aligned.crop((ox, oy, ox + up.width, oy + up.height)), up)
    out = work / "redrawn" / args.kind
    out.mkdir(parents=True, exist_ok=True)
    res.save(out / f"{args.key}.png")
    s = Image.new("RGB", (up.width, up.height * 2 + 8), (255, 255, 255))
    s.paste(flat(up), (0, 0)); s.paste(flat(res), (0, up.height + 8))
    s.save(keep / "compare.png")
    print(f"alignment {score:.2f}; wrote {out / (args.key + '.png')}; ledger ${ledger.total:.2f}")


CHECK_PROMPT = """\
Image A is a character portrait from a video game (low detail). Image B is a redrawn version \
that must show the SAME character with the SAME facial expression. Compare the faces and \
answer with JSON only, no other text:
{"eyes_A": "open | closed | half-closed | one eye closed", "eyes_B": "...",
 "mouth_A": "closed | slightly open | open | wide open", "mouth_B": "...",
 "teeth_A": true/false, "teeth_B": true/false,
 "eye_colour_A": "...", "eye_colour_B": "...",
 "expression_A": "a few words", "expression_B": "a few words",
 "same_person": true/false, "same_expression": true/false, "eye_colour_match": true/false,
 "defects_B": "anything wrong in B: bulging, oversized or uneven eyes, distorted or melted \
features, extra or missing features, smeared areas, text or labels; empty string if none"}
Judge the expression by eyes, eyebrows and mouth. A only looks blurrier; that is not a \
difference."""


def ask_json(model: str, prompt: str, images: list[Image.Image]) -> tuple[dict | None, float, str]:
    content = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": data_url(im)}} for im in images]
    r = call("POST", "/chat/completions", {"model": model, "messages": [{"role": "user", "content": content}],
                                           "usage": {"include": True}}, timeout=180)
    cost = float((r.get("usage") or {}).get("cost") or 0.0)
    text = ((r.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    s, e = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[s:e + 1]), cost, text
    except ValueError:
        return None, cost, text


DESCRIBE_PROMPT = """\
This is a character portrait from a video game (low detail). Describe only the face, as JSON \
with no other text:
{"eyes": "open / closed / half-closed / one eye closed, and where they look",
 "eyebrows": "...", "mouth": "closed / slightly open / open / wide open, teeth showing or not",
 "eye_colour": "...", "summary": "the expression in a few words"}"""

CHECK_MODEL = "google/gemini-3-flash-preview"


def check_ok(res: dict) -> tuple[bool, list[str]]:
    """The pass rule for a CHECK_PROMPT verdict, and why it failed."""
    why = [k for k in ("same_expression", "same_person") if not res.get(k)]
    # the game's colour can't be judged through closed or unreadable eyes
    colour_known = res.get("eyes_A") == "open" and str(res.get("eye_colour_A", "")).lower() not in (
        "", "n/a", "unknown", "not visible", "none")
    if colour_known and not res.get("eye_colour_match"):
        why.append(f"eye colour {res.get('eye_colour_A')} -> {res.get('eye_colour_B')}")
    if res.get("eyes_A") != res.get("eyes_B"):
        why.append(f"eyes {res.get('eyes_A')} -> {res.get('eyes_B')}")
    if res.get("defects_B"):
        why.append(f"defects: {res['defects_B']}")
    if "error" in res:
        why.append(str(res["error"])[:120])
    return not why, why


def cmd_check(args) -> None:
    """Compare every redrawn portrait with the game's (its upscale) through a cheap vision
    model; list the ones whose expression, eyes, mouth or eye colour differ, or that have
    defects. Results go to work/<CODE>/redraw/check.json."""
    from concurrent.futures import ThreadPoolExecutor
    work = Path(args.work)
    ledger = Ledger(work, args.budget)
    ledger.check()
    jobs = []
    for m in read_manifest(work):
        stem = m["source"].split("/")[-1].split(".")[0] if m["kind"] == "asset2d" else ""
        if stem.startswith(args.prefix) and (work / "redrawn" / "assets2d" / f"{m['key']}.png").exists():
            if args.only and not any(o in stem for o in args.only.split(",")):
                continue
            jobs.append((stem[len(args.prefix):], m["key"]))

    def one(job):
        name, key = job
        a = flat(load_up(work, key))
        b = flat(Image.open(work / "redrawn" / "assets2d" / f"{key}.png").convert("RGBA"))
        for _ in range(2):
            try:
                res, cost, text = ask_json(args.model, CHECK_PROMPT, [a, b])
            except RuntimeError as e:
                res, cost, text = None, 0.0, str(e)
            if res is not None:
                return name, key, res, cost
        return name, key, {"error": text[:300]}, cost

    results, spent = {}, 0.0
    with ThreadPoolExecutor(args.workers) as ex:
        for name, key, res, cost in ex.map(one, jobs):
            spent += cost
            ok, why = check_ok(res)
            res["ok"] = ok
            results[name] = {"key": key, **res}
            if not ok:
                print(f"FAIL {name} ({key}): {'; '.join(map(str, why)) or res.get('error', '')}")
    ledger.add(model=args.model, key=f"check {len(jobs)}", cost=spent, seconds=0, ok=True)
    out = work / "redraw" / "check.json"
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    bad = sum(1 for r in results.values() if not r["ok"])
    print(f"{len(results)} checked, {bad} flagged, ${spent:.3f}; {out}")


PORTRAIT_PROMPT_NOREF = """\
Image 1 is a character portrait of {name} from the Nintendo DS RPG Lufia: Curse of the \
Sinistrals, enlarged from a tiny original, so the face, eyes and fine details are blurry or \
malformed.

Redraw image 1 as a clean, sharp, high-resolution version of the same painting:
- Keep exactly the same framing, crop, pose, head angle, silhouette, colours and lighting as \
image 1. It must line up with image 1 when laid on top of it.
- Keep the original painted look and the same expression. Do not add or remove anything, and \
keep the flat grey background.
- Fix what the low resolution destroyed: clean, correct eyes, eyebrows, nose, mouth and small \
details; crisp hair strands, clothing and armour edges.
Return only the image, with the same aspect ratio as image 1."""


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
    p.add_argument("--hint", action="append", default=[],
                   help="expr=description of what the game's expression shows, used instead of its name")
    p.add_argument("--with-native", action="store_true",
                   help="also send the game's own pixels (enlarged, unsmoothed) as the colour/expression truth")
    p.add_argument("--out", help="write results here instead of work/<CODE>/redrawn/assets2d (for tests)")
    p.add_argument("--tag", help="suffix for the folder the raw model outputs are kept in (for tests)")
    p.add_argument("--auto-hint", action="store_true",
                   help="describe each game face with a vision model and put that in the prompt")
    p.add_argument("--verify", action="store_true",
                   help="check each result against the game's face; retry, and leave the upscale if it fails")
    p.add_argument("--tries", type=int, default=2, help="calls per image when one returns nothing or aligns badly")
    p.add_argument("--min-score", type=float, default=0.85, help="alignment score below which an expression is redrawn")
    p.add_argument("--budget", type=float, default=20.0, help="stop before the game's ledger passes this (USD)")
    p.set_defaults(fn=cmd_portrait)
    p = sub.add_parser("cast", help="every character: single-call masters, other expressions in 2x2 face sheets")
    p.add_argument("work")
    p.add_argument("--prefix", required=True, help="source-name prefix before the character, e.g. talk_f_")
    p.add_argument("--config", help="JSON with 'names' (id -> display name) and 'refs' (id -> files in work/<CODE>/refs)")
    p.add_argument("--only", help="comma-separated character ids")
    p.add_argument("--force", action="store_true", help="redo characters already redrawn")
    p.add_argument("--model", default="google/gemini-3-pro-image")
    p.add_argument("--grow", type=int, default=3)
    p.add_argument("--feather", type=int, default=12)
    p.add_argument("--tries", type=int, default=2)
    p.add_argument("--reuse", action="store_true", help="rebuild from the saved model outputs, no new calls")
    p.add_argument("--budget", type=float, default=20.0)
    p.set_defaults(fn=cmd_cast)
    p = sub.add_parser("check", help="vision-model check of every redrawn portrait against the game's")
    p.add_argument("work")
    p.add_argument("--prefix", default="talk_f_")
    p.add_argument("--only", help="comma-separated name fragments")
    p.add_argument("--model", default="google/gemini-3-flash-preview")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--budget", type=float, default=20.0)
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("one", help="redraw a single image, e.g. a title logo, with its reference")
    p.add_argument("work"); p.add_argument("key")
    p.add_argument("--kind", default="textures", help="textures or assets2d")
    p.add_argument("--what", default="the title logo", help="what the image is, for the prompt")
    p.add_argument("--ref", action="append", default=[], help="reference image (repeatable)")
    p.add_argument("--model", default="google/gemini-3-pro-image")
    p.add_argument("--reuse", action="store_true")
    p.add_argument("--budget", type=float, default=20.0)
    p.set_defaults(fn=cmd_one)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
