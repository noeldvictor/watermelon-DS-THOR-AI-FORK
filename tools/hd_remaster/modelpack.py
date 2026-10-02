"""HD models for a pack: export a game's 3D models for editing, and build replacements.

    hd_remaster.py models extract ROM.nds [--trace dl.json] [--previews]
    hd_remaster.py models build work/<CODE> [--smooth 0.6 [--only TEXT] [--seen]]

extract writes work/<CODE>/models/<model>/ for every NSBMD model in the ROM:
    model.obj / model.mtl   bind pose, one group per shape (g <shape>), one material per draw
    tex_<material>.png      the material's texture, decoded as the game shows it
    model.json              shapes (with their pack keys), materials, texture sizes, lighting
    preview.png             front / three-quarter / side / back (with --previews, or for models
                            a dl_trace saw)
and work/<CODE>/models/index.json listing them (with how often a dl_trace saw each shape).

build makes a replacement display list for every shape that has a new mesh:
    edited.obj in a model folder: groups named like the shapes they replace (keep the exported
    ones); each vertex takes the bone (matrix-stack slot) of the nearest original vertex of its
    shape, its texture coordinates from vt (else from the nearest original vertex), its normal
    from vn (else smooth normals); positions are model units like the export.
    --smooth S: PN-triangle smoothing of the original shapes instead (models whose id, name or
    source contains --only, or with --seen the ones a dl_trace saw), no new art needed.
Results go to work/<CODE>/models_built/<key>.dl and, when packs/<CODE> exists, to its models/
folder (build copies them in too), where the emulator's Vulkan renderer picks them up.
"""
from __future__ import annotations

import json
import re
import shutil
import struct
from pathlib import Path

import numpy as np
from PIL import Image

import models3d
import nitro
import render3d
import tex3d


def log(msg: str) -> None:
    print(msg, flush=True)


def model_id(model: models3d.Model) -> str:
    raw = f"{model.source}__{model.name}"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_")[:120]


def _texture_lookup(blobs: list[nitro.Blob]):
    """name -> [(source, Texture | Palette)] for every TEX0 in the ROM; a model's textures sit in its
    own file or in a BTX0 next to it, so the lookup prefers the closest source."""
    textures: dict[str, list] = {}
    palettes: dict[str, list] = {}
    for blk in tex3d.scan(blobs):
        src = blk.source.split("#")[0]
        for name, t in blk.textures.items():
            textures.setdefault(name, []).append((src, t))
        for name, p in blk.palettes.items():
            palettes.setdefault(name, []).append((src, p))

    def stem(path: str) -> str:
        return re.sub(r"\.(nsbmd|nsbtx|bmd|btx)$", "", path.split("/")[-1].lower())

    def pick(table, name, source):
        cands = table.get(name) or []
        if not cands:
            return None
        for src, item in cands:
            if src == source:
                return item
        for src, item in cands:
            if stem(src) == stem(source) or src.rsplit("/", 1)[0] == source.rsplit("/", 1)[0]:
                return item
        return cands[0][1]

    return lambda name, source: pick(textures, name, source), lambda name, source: pick(palettes, name, source)


def _write_obj(folder: Path, model: models3d.Model, meshes, tex_sizes: dict[int, tuple[int, int]]) -> None:
    obj = ["# Watermelon Thor model export: bind pose, model units. Keep the group (g) names: they",
           "# name the shapes a mesh replaces. Edit freely, save as edited.obj, run models build.",
           "mtllib model.mtl"]
    mtl = []
    vbase = 1
    seen_mats = set()
    for d, mesh in meshes:
        shape = model.shapes[d.shape]
        mat = model.materials[d.material] if d.material is not None and d.material < len(model.materials) else None
        mname = mat.name if mat else "none"
        if mname not in seen_mats:
            seen_mats.add(mname)
            mtl.append(f"newmtl {mname}")
            if (folder / f"tex_{mname}.png").exists():
                mtl.append(f"map_Kd tex_{mname}.png")
        w, h = tex_sizes.get(d.material, (0, 0)) if d.material is not None else (0, 0)
        normals = models3d.smooth_normals(mesh) if len(mesh.positions) else mesh.normals
        obj.append(f"g {shape.name}")
        obj.append(f"usemtl {mname}")
        for p in mesh.positions:
            obj.append(f"v {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}")
        for s, t in mesh.texcoords:
            obj.append(f"vt {s / w:.6f} {1 - t / h:.6f}" if w and h else "vt 0 0")
        for n in normals:
            obj.append(f"vn {n[0]:.5f} {n[1]:.5f} {n[2]:.5f}")
        for a, b, c in mesh.triangles:
            obj.append(f"f {a + vbase}/{a + vbase}/{a + vbase} {b + vbase}/{b + vbase}/{b + vbase} "
                       f"{c + vbase}/{c + vbase}/{c + vbase}")
        vbase += len(mesh.positions)
    (folder / "model.obj").write_text("\n".join(obj) + "\n", encoding="utf-8")
    (folder / "model.mtl").write_text("\n".join(mtl) + "\n", encoding="utf-8")


def _preview(meshes, model, textures: dict[int, np.ndarray], tex_sizes) -> np.ndarray:
    parts = []
    for d, mesh in meshes:
        part = dict(positions=mesh.positions, triangles=mesh.triangles)
        if d.material in textures:
            w, h = tex_sizes[d.material]
            part["texture"] = textures[d.material]
            part["uv"] = mesh.texcoords / np.array([w, h])
        parts.append(part)
    if not parts or not any(len(p["positions"]) for p in parts):
        return np.zeros((256, 1024, 3), np.uint8)
    return np.concatenate([render3d.render(parts, 256, yaw=y, pitch=10) for y in (0, 40, 90, 180)], 1)


def cpu_list_counts(words_path: Path, shapes: dict[str, bytes]) -> dict[str, int]:
    """How often each shape's display list occurs in the words the CPU wrote to the GX FIFO
    (dl_trace's gx_cpu_words.bin): short lists NitroSystem writes itself, which the DMA trace
    can't see. Indexed by their first four words, then compared in full."""
    import struct as _struct
    raw = words_path.read_bytes()
    stream = np.frombuffer(raw[:len(raw) // 4 * 4], dtype="<u4")
    if len(stream) < 4:
        return {}
    index: dict[tuple, list[int]] = {}
    for i in range(len(stream) - 3):
        index.setdefault((int(stream[i]), int(stream[i + 1]), int(stream[i + 2]), int(stream[i + 3])), []).append(i)
    counts: dict[str, int] = {}
    for key, dl in shapes.items():
        n = len(dl) // 4
        if n < 4:
            continue
        words = np.frombuffer(dl[:n * 4], dtype="<u4")
        for i in index.get(tuple(int(w) for w in words[:4]), []):
            if i + n <= len(stream) and np.array_equal(stream[i:i + n], words):
                counts[key] = counts.get(key, 0) + 1
    return counts


def extract(rom_path: str, work_root: Path, trace: str | None, previews: bool,
            cpu_words: str | None = None) -> Path:
    rom = Path(rom_path).read_bytes()
    code = nitro.game_code(rom)
    out = work_root / code / "models"
    out.mkdir(parents=True, exist_ok=True)
    seen: dict[str, int] = {}
    if trace:
        for entry in json.loads(Path(trace).read_text(encoding="utf-8")).get("lists", []):
            seen[entry["key"]] = seen.get(entry["key"], 0) + int(entry.get("count", 1))
    blobs = nitro.blobs(rom)
    if cpu_words:
        shapes = {s.key: s.dl for blob in blobs for m in models3d.models_in(blob) for s in m.shapes}
        cpu = cpu_list_counts(Path(cpu_words), shapes)
        for key, n in cpu.items():
            seen[key] = seen.get(key, 0) + n
        log(f"{len(cpu)} shapes found in the CPU's GX FIFO words ({sum(cpu.values())} times)")
    tex_of, pal_of = _texture_lookup(blobs)
    index = []
    count = 0
    for blob in blobs:
        for model in models3d.models_in(blob):
            mid = model_id(model)
            folder = out / mid
            folder.mkdir(parents=True, exist_ok=True)
            meshes = models3d.model_meshes(model)
            textures: dict[int, np.ndarray] = {}
            tex_sizes: dict[int, tuple[int, int]] = {}
            for i, mat in enumerate(model.materials):
                if not mat.texture:
                    continue
                tex = tex_of(mat.texture, model.source)
                if tex is None:
                    continue
                pal = pal_of(mat.palette, model.source) if mat.palette else None
                try:
                    img = tex3d.decode(tex, pal)
                except Exception:
                    continue
                textures[i] = img
                tex_sizes[i] = (tex.w, tex.h)
                Image.fromarray(img).save(folder / f"tex_{mat.name}.png")
            _write_obj(folder, model, meshes, tex_sizes)
            shapes = []
            for d, mesh in meshes:
                shape = model.shapes[d.shape]
                shapes.append({
                    "key": shape.key, "name": shape.name,
                    "material": model.materials[d.material].name if d.material is not None and d.material < len(model.materials) else None,
                    "texture_size": list(tex_sizes.get(d.material, (0, 0))) if d.material is not None else [0, 0],
                    "lit": models3d.is_lit(shape.dl), "vertices": len(mesh.positions),
                    "triangles": len(mesh.triangles), "seen": seen.get(shape.key, 0),
                })
            info = {"source": model.source, "name": model.name, "id": mid, "bbox": list(model.bbox),
                    "shapes": shapes}
            (folder / "model.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
            if previews or any(s["seen"] for s in shapes):
                Image.fromarray(_preview(meshes, model, textures, tex_sizes)).save(folder / "preview.png")
            index.append({"id": mid, "source": model.source, "name": model.name,
                          "shapes": len(shapes), "triangles": sum(s["triangles"] for s in shapes),
                          "seen": sum(s["seen"] for s in shapes)})
            count += 1
    (out / "rom.txt").write_text(str(Path(rom_path).resolve()), encoding="utf-8")
    index.sort(key=lambda e: (-e["seen"], e["id"]))
    (out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    log(f"{count} models -> {out} ({sum(1 for e in index if e['seen'])} seen in the trace)")
    return out


# ---------------------------------------------------------------------------- build

def _read_obj(path: Path):
    """groups: name -> (positions, uvs | None, normals | None, triangles) with per-corner data."""
    V, VT, VN = [], [], []
    groups: dict[str, dict] = {}
    cur = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        tag = parts[0]
        if tag == "v":
            V.append([float(x) for x in parts[1:4]])
        elif tag == "vt":
            VT.append([float(x) for x in parts[1:3]])
        elif tag == "vn":
            VN.append([float(x) for x in parts[1:4]])
        elif tag in ("g", "o"):
            cur = groups.setdefault(" ".join(parts[1:]) or "default", {"corners": []})
        elif tag == "f":
            if cur is None:
                cur = groups.setdefault("default", {"corners": []})
            idx = []
            for c in parts[1:]:
                f = c.split("/")
                vi = int(f[0]); vi = vi - 1 if vi > 0 else len(V) + vi
                ti = int(f[1]) if len(f) > 1 and f[1] else None
                ni = int(f[2]) if len(f) > 2 and f[2] else None
                ti = (ti - 1 if ti > 0 else len(VT) + ti) if ti is not None else None
                ni = (ni - 1 if ni > 0 else len(VN) + ni) if ni is not None else None
                idx.append((vi, ti, ni))
            for k in range(1, len(idx) - 1):
                cur["corners"] += [idx[0], idx[k], idx[k + 1]]
    out = {}
    for name, g in groups.items():
        corners = g["corners"]
        if not corners:
            continue
        pos = np.array([V[c[0]] for c in corners], np.float64)
        uv = np.array([VT[c[1]] for c in corners], np.float64) if all(c[1] is not None for c in corners) else None
        nrm = np.array([VN[c[2]] for c in corners], np.float64) if all(c[2] is not None for c in corners) else None
        out[name] = (pos, uv, nrm, np.arange(len(corners)).reshape(-1, 3))
    return out


def mesh_from_edit(orig: models3d.Mesh, pos, uv, nrm, tris, tex_size) -> models3d.Mesh:
    """An edited mesh with each vertex tied to the original shape: its bone (slot) and, where the
    edit has none, texture coordinates and colour from the nearest original vertex."""
    nearest = np.empty(len(pos), dtype=np.int64)
    for start in range(0, len(pos), 4096):           # chunks: an AI mesh can have 100k vertices
        chunk = pos[start:start + 4096]
        d2 = ((chunk[:, None, :] - orig.positions[None, :, :]) ** 2).sum(-1)
        nearest[start:start + len(chunk)] = d2.argmin(1)
    w, h = tex_size
    if uv is not None and w and h:
        texcoords = np.stack([uv[:, 0] * w, (1 - uv[:, 1]) * h], 1)
    else:
        texcoords = orig.texcoords[nearest]
    mesh = models3d.Mesh(pos, np.zeros_like(pos), texcoords, orig.colors[nearest], orig.slots[nearest], tris)
    mesh.normals = nrm if nrm is not None else models3d.smooth_normals(mesh)
    return mesh


def build(work: Path, packs_root: Path, smooth: float | None, only: str | None, seen_only: bool,
          rom_path: str | None) -> None:
    code = work.name
    models_dir = work / "models"
    if not models_dir.is_dir():
        raise SystemExit(f"{models_dir} is missing: run models extract first")
    built = work / "models_built"
    built.mkdir(exist_ok=True)
    rom_file = rom_path
    if not rom_file and (models_dir / "rom.txt").exists():
        rom_file = (models_dir / "rom.txt").read_text(encoding="utf-8").strip()
    if not rom_file or not Path(rom_file).exists():
        raise SystemExit("models build needs the ROM the models came from (--rom)")
    rom = Path(rom_file).read_bytes()
    wanted_ids = set()
    for folder in models_dir.iterdir():
        if not (folder / "model.json").exists():
            continue
        info = json.loads((folder / "model.json").read_text(encoding="utf-8"))
        edited = (folder / "edited.obj").exists()
        named = only is None or any(only.lower() in str(info[k]).lower() for k in ("id", "source", "name"))
        auto = smooth is not None and named and (not seen_only or any(s["seen"] for s in info["shapes"]))
        if edited or auto:
            wanted_ids.add(info["id"])
    if not wanted_ids:
        log("nothing to build: no edited.obj, and no --smooth selection")
        return
    made = 0
    originals: dict[str, bytes] = {}      # key -> the replaced display list (first words go to originals.txt)
    for blob in nitro.blobs(rom):
        for model in models3d.models_in(blob):
            mid = model_id(model)
            if mid not in wanted_ids:
                continue
            folder = models_dir / mid
            info = json.loads((folder / "model.json").read_text(encoding="utf-8"))
            sizes = {s["name"]: tuple(s["texture_size"]) for s in info["shapes"]}
            edits = _read_obj(folder / "edited.obj") if (folder / "edited.obj").exists() else {}
            # shapes a new mesh covers (hidden.txt, written by ai3d fit): replaced by nothing
            hidden = set()
            if edits and (folder / "hidden.txt").exists():
                hidden = {l.strip() for l in (folder / "hidden.txt").read_text(encoding="utf-8").splitlines() if l.strip()}
            done = set()
            for d, mesh in models3d.model_meshes(model):
                shape = model.shapes[d.shape]
                if shape.key in done or not len(mesh.triangles):
                    continue
                if shape.name in hidden and shape.name not in edits:
                    models3d.write_replacement(built, shape.key, struct.pack("<I", 0))   # a NOP: draws nothing
                    originals[shape.key] = shape.dl
                    done.add(shape.key)
                    made += 1
                    log(f"{mid}: {shape.name} hidden ({shape.key})")
                    continue
                if shape.name in edits:
                    pos, uv, nrm, tris = edits[shape.name]
                    new = mesh_from_edit(mesh, pos, uv, nrm, tris, sizes.get(shape.name, (0, 0)))
                elif smooth is not None and not edits:
                    new = models3d.pn_triangles(mesh, 3, smooth)
                else:
                    continue
                dl = models3d.mesh_to_display_list(new, d, models3d.is_lit(shape.dl))
                models3d.write_replacement(built, shape.key, dl)
                originals[shape.key] = shape.dl
                done.add(shape.key)
                made += 1
                log(f"{mid}: {shape.name} {len(mesh.triangles)} -> {len(new.triangles)} triangles ({shape.key})")
    models3d.write_originals(built, originals)
    log(f"{made} replacements in {built}")
    install(work, packs_root)


def install(work: Path, packs_root: Path) -> None:
    """Copy built replacements into packs/<CODE>/models when that pack exists."""
    pack = packs_root / work.name
    built = work / "models_built"
    if not pack.is_dir() or not built.is_dir():
        return
    target = pack / "models"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(built, target)
    log(f"copied {len(list(target.glob('*.dl')))} replacements to {target}")


# ---------------------------------------------------------------------------- AI models / fitting

def _load_model(work: Path, mid: str):
    """The model with this id from the ROM extract read, its folder, and its textured meshes."""
    folder = work / "models" / mid
    if not (folder / "model.json").exists():
        raise SystemExit(f"{folder} has no model.json: run models extract, ids are in models/index.json")
    rom = Path((work / "models" / "rom.txt").read_text(encoding="utf-8").strip()).read_bytes()
    blobs = nitro.blobs(rom)
    info = json.loads((folder / "model.json").read_text(encoding="utf-8"))
    for blob in blobs:
        if blob.path != info["source"]:
            continue
        for model in models3d.models_in(blob):
            if model.name == info["name"]:
                return model, folder, info, blobs
    raise SystemExit(f"{mid} not found in the ROM")


def _textured_parts(model, folder: Path, info: dict):
    parts = []
    sizes = {s["name"]: tuple(s["texture_size"]) for s in info["shapes"]}
    for d, mesh in models3d.model_meshes(model):
        part = dict(positions=mesh.positions, triangles=mesh.triangles)
        mat = model.materials[d.material] if d.material is not None and d.material < len(model.materials) else None
        shape = model.shapes[d.shape]
        tex = folder / f"tex_{mat.name}.png" if mat else None
        w, h = sizes.get(shape.name, (0, 0))
        if tex and tex.exists() and w and h:
            part["texture"] = np.array(Image.open(tex).convert("RGBA"))
            part["uv"] = mesh.texcoords / np.array([w, h])
        parts.append(part)
    return parts


def fit_mesh(work: Path, mid: str, mesh_path: Path) -> None:
    import ai3d
    model, folder, info, _ = _load_model(work, mid)
    if mesh_path.suffix.lower() == ".glb":
        pos, tris = ai3d.read_glb(mesh_path)
    else:
        groups = _read_obj(mesh_path)
        pos = np.concatenate([g[0] for g in groups.values()])
        base = np.cumsum([0] + [len(g[0]) for g in list(groups.values())[:-1]])
        tris = np.concatenate([g[3] + b for g, b in zip(groups.values(), base)])
    fitted, meshes = ai3d.fit(pos, tris, model)
    sizes = {s["name"]: tuple(s["texture_size"]) for s in info["shapes"]}
    ai3d.write_edited_obj(folder / "edited.obj", model, fitted, meshes, sizes)
    preview = []
    for di, m in fitted.items():
        d, _ = meshes[di]
        part = dict(positions=m.positions, triangles=m.triangles)
        mat = model.materials[d.material] if d.material is not None and d.material < len(model.materials) else None
        w, h = sizes.get(model.shapes[d.shape].name, (0, 0))
        if mat and (folder / f"tex_{mat.name}.png").exists() and w and h:
            part["texture"] = np.array(Image.open(folder / f"tex_{mat.name}.png").convert("RGBA"))
            part["uv"] = m.texcoords / np.array([w, h])
        preview.append(part)
    Image.fromarray(np.concatenate([render3d.render(preview, 256, yaw=y, pitch=10) for y in (0, 40, 90, 180)], 1)) \
        .save(folder / "edited_preview.png")
    log(f"{len(pos)} vertices, {len(tris)} triangles fitted onto {mid}: {folder / 'edited.obj'} "
        f"(+ edited_preview.png, hidden.txt); next: models build {work}")


def ai_model(work: Path, mid: str, polycount: int, budget: int, dry_run: bool, provider: str = "tripo") -> None:
    import ai3d
    model, folder, info, _ = _load_model(work, mid)
    refs = ai3d.reference_views(_textured_parts(model, folder, info))
    ai_dir = folder / "ai"
    ai_dir.mkdir(exist_ok=True)
    for name, img in zip(("front", "right", "back", "left"), refs):
        Image.fromarray(img).save(ai_dir / f"ref_{name}.png")
    if dry_run:
        log(f"reference pictures in {ai_dir}; nothing sent (dry run)")
        return
    generate = ai3d.tripo_multiview if provider == "tripo" else ai3d.meshy_multi_image
    glb = generate(refs, ai_dir, work / "models" / "ai_ledger.jsonl", budget, polycount, mid)
    fit_mesh(work, mid, glb)
