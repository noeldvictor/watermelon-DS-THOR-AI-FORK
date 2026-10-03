"""AI-generated 3D models for a pack (Tripo or Meshy), fitted onto the game's own model.

    hd_remaster.py models ai work/<CODE> --model <id> [--provider tripo|meshy] [--polycount 6000]
                             [--budget 100] [--dry-run]
    hd_remaster.py models fit work/<CODE> --model <id> --mesh <mesh.glb>

ai renders the original model from the front, right, back and left (with its own textures),
sends the four pictures to a multiview image-to-3D service for bare geometry (no texture: the
game's own textures go on in the fit) and saves the result as
work/<CODE>/models/<model>/ai/<task>.glb, then fits it.
  tripo (default): Tripo API v3 (openapi.tripo3d.ai/v3, the v2 API retires 2026-11-01): upload
    each view (POST /files -> file_token), POST /generation/multiview-to-model with view-keyed
    inputs, model v3.1-20260211, texture and pbr off, face_limit = --polycount; poll
    GET /tasks/{id}; output.model_url expires 5 minutes after success. 20 credits ($0.20; new
    accounts get 300 free). Key: TRIPO_API_KEY.
  meshy: Meshy multi-image-to-3D, mesh only, remeshed to --polycount. 20 credits. Key: MESHY_API_KEY.
Every call is logged with its credits in work/<CODE>/models/ai_ledger.jsonl and refused once the
ledger would pass --budget credits. Keys live in tools/hd_remaster/.env (never printed).
--dry-run writes the reference pictures and stops.

fit turns any mesh (an AI result, or a GLB/OBJ from a 3D tool, in any scale, position and facing:
Tripo exports +X forward) into replacements: it is scaled and moved onto the original's bounds,
turned to whichever of the four facings ICP fits best, refined with ICP, and every vertex takes from the nearest point on the original's
surface its shape, bone (matrix-stack slot), texture coordinates and normal direction, so the new
mesh wears the game's own (or the pack's HD) textures and follows the game's animation. The
result is written as edited.obj (groups = shapes), which `models build` turns into display lists.
"""
from __future__ import annotations

import base64
import io
import json
import os
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

import models3d
import render3d

HERE = Path(__file__).resolve().parent
MESHY = "https://api.meshy.ai/openapi/v1"
TRIPO = "https://openapi.tripo3d.ai/v3"
TRIPO_MODEL = "v3.1-20260211"
CREDITS_MULTI_IMAGE_MESH = 20          # both services: multiview, geometry only


def log(msg: str) -> None:
    print(msg, flush=True)


def _key(name: str) -> str | None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip() or None
    return os.environ.get(name) or None


# ---------------------------------------------------------------------------- reference pictures

def reference_views(parts: list[dict], size: int = 1024, supersample: int = 2) -> list[np.ndarray]:
    """Front, right, back, left, the order multi-image-to-3D expects (front first), on a
    transparent background (a white one swallowed Link's white trousers). Flat colour: the DS
    lights the result itself, so light painted into these would end up in the generated texture
    and be lit twice. Rendered larger and scaled down for clean edges."""
    out = []
    for y in (0, 90, 180, 270):
        big = render3d.render(parts, size * supersample, yaw=y, pitch=0, background=None, shade=False)
        out.append(np.array(Image.fromarray(big, "RGBA").resize((size, size), Image.LANCZOS)))
    return out


def _data_uri(img: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------- Meshy

def _request(method: str, url: str, key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def _png_bytes(img: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG")
    return buf.getvalue()


def _tripo(method: str, path: str, key: str, body: dict | None = None, upload: bytes | None = None) -> dict:
    """One Tripo v3 call; returns `data` (raises with the service's message on code != 0)."""
    headers = {"Authorization": f"Bearer {key}"}
    if upload is not None:
        boundary = "----watermelonthor" + os.urandom(8).hex()
        data = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"view.png\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode("ascii") + upload + f"\r\n--{boundary}--\r\n".encode("ascii")
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        data = None
    req = urllib.request.Request(TRIPO + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            reply = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        reply = json.loads(e.read().decode("utf-8", "replace") or "{}")
    if reply.get("code") != 0:
        raise SystemExit(f"tripo {path}: {reply.get('code')} {reply.get('message')} ({reply.get('suggestion')})")
    return reply["data"]


# a textured multiview model: 30 credits with texture, +20 for detailed texture quality (Tripo
# pricing, 2026-10); the ledger records what the task reports afterwards
CREDITS_MULTI_IMAGE_TEXTURED = 50


def tripo_multiview(images: list[np.ndarray], out_dir: Path, ledger: Path, budget: int, polycount: int,
                    label: str, textured: bool = False) -> Path:
    """images: front, right, back, left (reference_views order). textured: also a base-colour
    texture (detailed quality, newest texture model, no de-lighting: the views are already flat)."""
    key = _key("TRIPO_API_KEY")
    if not key:
        raise SystemExit("no TRIPO_API_KEY in tools/hd_remaster/.env")
    estimate = CREDITS_MULTI_IMAGE_TEXTURED if textured else CREDITS_MULTI_IMAGE_MESH
    spent = _ledger_total(ledger)
    if spent + estimate > budget:
        raise SystemExit(f"budget: {spent} credits used, a call costs about {estimate}, cap {budget}")
    tokens = {}
    for view, img in zip(("front", "right", "back", "left"), images):
        tokens[view] = _tripo("POST", "/files", key, upload=_png_bytes(img))["file_token"]
    body = {
        "inputs": [{"front": tokens["front"]}, {"left": tokens["left"]},
                   {"back": tokens["back"]}, {"right": tokens["right"]}],
        "model": TRIPO_MODEL,
        "texture": textured,
        "pbr": False,
        "face_limit": int(polycount),
    }
    if textured:
        body.update({"texture_quality": "detailed", "texture_version": "v3.5-20260815", "delight": False,
                     "orientation": "align_image"})
    task = _tripo("POST", "/generation/multiview-to-model", key, body)["task_id"]
    log(f"tripo task {task}")
    entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "provider": "tripo", "task": task, "model": label,
             "credits": estimate, "kind": "multiview-to-model " + ("textured" if textured else "geometry")}
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    while True:
        time.sleep(5)
        info = _tripo("GET", f"/tasks/{task}", key)
        status = info.get("status")
        log(f"  {status} {info.get('progress', 0)}%")
        if status == "success":
            break
        if status in ("failed", "cancelled", "banned"):
            raise SystemExit(f"tripo task {task} {status}: {info.get('error_code')} {info.get('error_message')}")
    url = (info.get("output") or {}).get("model_url")
    if not url:
        raise SystemExit(f"tripo task {task}: no model_url in {list((info.get('output') or {}).keys())}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{task}.glb"
    with urllib.request.urlopen(url, timeout=300) as r:        # the URL expires after 5 minutes
        path.write_bytes(r.read())
    actual = info.get("credits_consumed")
    if actual is not None and float(actual) != estimate:
        # the ledger keeps the estimate it was checked against; this line makes its total exact
        with ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "provider": "tripo", "task": task,
                                "model": label, "credits": round(float(actual) - estimate, 2),
                                "kind": "correction to the task's reported cost"}) + "\n")
    log(f"saved {path} ({actual} credits)")
    return path


def _ledger_total(ledger: Path) -> int:
    if not ledger.exists():
        return 0
    return round(sum(float(json.loads(line).get("credits", 0))
                     for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()), 2)


def meshy_multi_image(images: list[np.ndarray], out_dir: Path, ledger: Path, budget: int, polycount: int,
                      label: str) -> Path:
    key = _key("MESHY_API_KEY")
    if not key:
        raise SystemExit("no MESHY_API_KEY in tools/hd_remaster/.env")
    spent = _ledger_total(ledger)
    if spent + CREDITS_MULTI_IMAGE_MESH > budget:
        raise SystemExit(f"budget: {spent} credits used, a call costs {CREDITS_MULTI_IMAGE_MESH}, cap {budget}")
    body = {
        "image_urls": [_data_uri(i) for i in images],
        "should_texture": False,            # the game's own textures go on through the fit
        "should_remesh": True,
        "topology": "triangle",
        "target_polycount": int(polycount),
        "target_formats": ["glb"],
    }
    task = _request("POST", f"{MESHY}/multi-image-to-3d", key, body)["result"]
    log(f"meshy task {task}")
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "task": task, "model": label,
                            "credits": CREDITS_MULTI_IMAGE_MESH, "kind": "multi-image-to-3d mesh"}) + "\n")
    while True:
        time.sleep(10)
        info = _request("GET", f"{MESHY}/multi-image-to-3d/{task}", key)
        status = info.get("status")
        log(f"  {status} {info.get('progress', 0)}%")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "CANCELED"):
            raise SystemExit(f"meshy task {task} {status}: {info.get('task_error')}")
    url = (info.get("model_urls") or {}).get("glb")
    if not url:
        raise SystemExit(f"meshy task {task}: no glb in {list((info.get('model_urls') or {}).keys())}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{task}.glb"
    with urllib.request.urlopen(url, timeout=300) as r:
        path.write_bytes(r.read())
    log(f"saved {path} ({info.get('consumed_credits')} credits)")
    return path


# ---------------------------------------------------------------------------- meshes in and out

def read_glb(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Every triangle of a GLB (node transforms applied): positions (n,3), triangles (m,3)."""
    data = path.read_bytes()
    if data[:4] != b"glTF":
        raise ValueError(f"{path} is not a GLB")
    pos = 12
    gltf, binary = None, b""
    while pos < len(data):
        length, kind = struct.unpack_from("<II", data, pos)
        chunk = data[pos + 8:pos + 8 + length]
        if kind == 0x4E4F534A:
            gltf = json.loads(chunk.decode("utf-8"))
        elif kind == 0x004E4942:
            binary = chunk
        pos += 8 + length
    comp = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
    ncomp = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}

    def accessor(i: int) -> np.ndarray:
        a = gltf["accessors"][i]
        view = gltf["bufferViews"][a["bufferView"]]
        fmt, size = comp[a["componentType"]]
        n = ncomp[a["type"]]
        start = view.get("byteOffset", 0) + a.get("byteOffset", 0)
        stride = view.get("byteStride") or size * n
        dt = np.dtype("<" + fmt)
        out = np.empty((a["count"], n), dt)
        for k in range(a["count"]):
            out[k] = np.frombuffer(binary, dt, n, start + k * stride)
        return out

    def node_matrix(node: dict) -> np.ndarray:
        if "matrix" in node:
            return np.array(node["matrix"], np.float64).reshape(4, 4).T
        m = np.eye(4)
        if "scale" in node:
            m = np.diag(list(node["scale"]) + [1.0]) @ m
        if "rotation" in node:
            x, y, z, w = node["rotation"]
            r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                          [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                          [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
            rm = np.eye(4); rm[:3, :3] = r
            m = rm @ m
        if "translation" in node:
            t = np.eye(4); t[:3, 3] = node["translation"]
            m = t @ m
        return m

    positions, triangles = [], []
    base = 0

    def walk(i: int, parent: np.ndarray) -> None:
        nonlocal base
        node = gltf["nodes"][i]
        m = parent @ node_matrix(node)
        if "mesh" in node:
            for prim in gltf["meshes"][node["mesh"]]["primitives"]:
                if prim.get("mode", 4) != 4:
                    continue
                p = accessor(prim["attributes"]["POSITION"]).astype(np.float64)
                p = (m @ np.c_[p, np.ones(len(p))].T).T[:, :3]
                idx = accessor(prim["indices"]).reshape(-1) if "indices" in prim else np.arange(len(p))
                positions.append(p)
                triangles.append(idx.reshape(-1, 3).astype(np.int64) + base)
                base += len(p)
        for c in node.get("children", []):
            walk(c, m)

    scene = gltf.get("scenes", [{}])[gltf.get("scene", 0)]
    for i in scene.get("nodes", range(len(gltf.get("nodes", [])))):
        walk(i, np.eye(4))
    return np.concatenate(positions), np.concatenate(triangles)


def write_glb(path: Path, positions: np.ndarray, triangles: np.ndarray) -> None:
    """A minimal GLB (positions + indices), for tests and hand-offs."""
    p = positions.astype("<f4").tobytes()
    i = triangles.astype("<u4").tobytes()
    gltf = {
        "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "buffers": [{"byteLength": len(p) + len(i)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(p)},
                        {"buffer": 0, "byteOffset": len(p), "byteLength": len(i)}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(positions), "type": "VEC3",
             "min": positions.min(0).tolist(), "max": positions.max(0).tolist()},
            {"bufferView": 1, "componentType": 5125, "count": triangles.size, "type": "SCALAR"}],
    }
    js = json.dumps(gltf).encode("utf-8")
    js += b" " * (-len(js) % 4)
    binary = p + i
    binary += b"\0" * (-len(binary) % 4)
    out = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(binary))
    out += struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(binary), 0x004E4942) + binary
    path.write_bytes(out)


# ---------------------------------------------------------------------------- fitting

def _closest_on_triangles(points: np.ndarray, tri_pts: np.ndarray):
    """For each point: the closest triangle (index) and barycentric coordinates there."""
    chunk = max(8, 2_000_000 // max(len(tri_pts), 1))      # keeps the work arrays near 50 MB
    a, b, c = tri_pts[:, 0], tri_pts[:, 1], tri_pts[:, 2]
    ab, ac = b - a, c - a
    best_tri = np.zeros(len(points), np.int64)
    best_bary = np.zeros((len(points), 3))
    for s in range(0, len(points), chunk):
        p = points[s:s + chunk][:, None, :]                # (k,1,3)
        ap = p - a[None]
        d1 = (ab[None] * ap).sum(-1); d2 = (ac[None] * ap).sum(-1)
        bp = p - b[None]
        d3 = (ab[None] * bp).sum(-1); d4 = (ac[None] * bp).sum(-1)
        cp = p - c[None]
        d5 = (ab[None] * cp).sum(-1); d6 = (ac[None] * cp).sum(-1)
        va = d3 * d6 - d5 * d4; vb = d5 * d2 - d1 * d6; vc = d1 * d4 - d3 * d2
        denom = va + vb + vc
        denom = np.where(np.abs(denom) < 1e-20, 1e-20, denom)
        v = vb / denom; w = vc / denom
        # clamp to the triangle (projecting onto its plane, then into its edges, is enough here)
        u = 1 - v - w
        bary = np.stack([u, v, w], -1)
        bary = np.clip(bary, 0, None)
        bary /= np.maximum(bary.sum(-1, keepdims=True), 1e-12)
        q = bary[..., :1] * a[None] + bary[..., 1:2] * b[None] + bary[..., 2:] * c[None]
        d = ((q - p) ** 2).sum(-1)
        idx = d.argmin(1)
        best_tri[s:s + chunk] = idx
        best_bary[s:s + chunk] = bary[np.arange(len(idx)), idx]
    return best_tri, best_bary


def _chamfer(a: np.ndarray, b: np.ndarray, samples: int = 600) -> float:
    """Mean squared nearest distance both ways (a to b and b to a), on a sample of each: one way
    alone rewards a mesh shrunk inside the other."""
    sa = a[::max(1, len(a) // samples)]
    sb = b[::max(1, len(b) // samples)]
    ab = np.mean([((b - q) ** 2).sum(1).min() for q in sa])
    ba = np.mean([((a - q) ** 2).sum(1).min() for q in sb])
    return float(ab + ba)


def _icp(src: np.ndarray, dst: np.ndarray, iterations: int = 20, max_scale: float = 1.15) -> np.ndarray:
    """Similarity transform (4x4) moving src onto dst (nearest-point ICP). The scale may only move
    within 1/max_scale..max_scale overall: the caller has already scaled by height, and a free
    scale lets a wrongly turned mesh shrink into the original's core and look like a good fit."""
    m = np.eye(4)
    cur = src.copy()
    for _ in range(iterations):
        # nearest dst point for each src point (chunked brute force)
        nn = np.empty(len(cur), np.int64)
        for s in range(0, len(cur), 2048):
            nn[s:s + 2048] = ((cur[s:s + 2048, None, :] - dst[None]) ** 2).sum(-1).argmin(1)
        q = dst[nn]
        mu_p, mu_q = cur.mean(0), q.mean(0)
        P, Q = cur - mu_p, q - mu_q
        U, S, Vt = np.linalg.svd(P.T @ Q)
        d = np.sign(np.linalg.det(Vt.T @ U.T))
        D = np.diag([1, 1, d])
        R = Vt.T @ D @ U.T
        scale = (S * np.diag(D)).sum() / max((P ** 2).sum(), 1e-12)
        total = np.cbrt(abs(np.linalg.det(m[:3, :3]))) if np.any(m[:3, :3]) else 1.0
        scale = float(np.clip(scale * total, 1 / max_scale, max_scale)) / total
        t = mu_q - scale * R @ mu_p
        step = np.eye(4); step[:3, :3] = scale * R; step[:3, 3] = t
        cur = (step[:3, :3] @ cur.T).T + step[:3, 3]
        m = step @ m
    return m


def _place(new_pos: np.ndarray, meshes, icp: bool = True) -> np.ndarray:
    """The new mesh's vertices placed on the original model (see fit)."""
    orig_pos = np.concatenate([m.positions for _, m in meshes])
    lo, hi = orig_pos.min(0), orig_pos.max(0)
    nlo, nhi = new_pos.min(0), new_pos.max(0)
    scale = (hi[1] - lo[1]) / max(nhi[1] - nlo[1], 1e-9)          # by height, the most reliable
    placed = (new_pos - (nlo + nhi) / 2) * scale + (lo + hi) / 2
    if icp:
        # services disagree on which way a model faces (Tripo: +X forward): try the four turns
        # about the vertical axis and keep the one ICP fits best
        centre = (lo + hi) / 2
        sample = placed[::max(1, len(placed) // 3000)]
        best = None
        for quarter in range(4):
            a = quarter * np.pi / 2
            turn = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
            turned = (sample - centre) @ turn.T + centre
            m = _icp(turned, orig_pos)
            moved = (m[:3, :3] @ turned.T).T + m[:3, 3]
            residual = _chamfer(moved, orig_pos)
            if best is None or residual < best[0]:
                best = (residual, turn, m)
        _, turn, m = best
        placed = (placed - centre) @ turn.T + centre
        placed = (m[:3, :3] @ placed.T).T + m[:3, 3]

    return placed


def fit(new_pos: np.ndarray, new_tris: np.ndarray, model: models3d.Model, icp: bool = True):
    """The new mesh placed on the original and split into its shapes: {shape index: Mesh}."""
    meshes = models3d.model_meshes(model)
    placed = _place(new_pos, meshes, icp)

    # the original's triangles, with where each came from
    tri_pts, owner = [], []
    for di, (d, mesh) in enumerate(meshes):
        for t in mesh.triangles:
            tri_pts.append(mesh.positions[t]); owner.append((di, t))
    tri_pts = np.array(tri_pts)
    tri_idx, bary = _closest_on_triangles(placed, tri_pts)

    n = len(placed)
    draw_of = np.array([owner[t][0] for t in tri_idx])
    texcoords = np.zeros((n, 2)); normals = np.zeros((n, 3)); colors = np.zeros((n, 3))
    slots = np.zeros(n, np.int32)
    for vi in range(n):
        di, corners = owner[tri_idx[vi]]
        mesh = meshes[di][1]
        w = bary[vi]
        texcoords[vi] = w @ mesh.texcoords[corners]
        normals[vi] = w @ mesh.normals[corners]
        colors[vi] = w @ mesh.colors[corners]
        slots[vi] = mesh.slots[corners[int(np.argmax(w))]]
    # Texture coordinates per triangle, not per vertex: a new triangle whose corners landed on
    # different texture islands (a face next to hair) would stretch across the whole atlas. Each
    # one takes the original triangle nearest its centre and maps its three corners through that
    # triangle's own (affine) texture mapping, extrapolating a little past its edges if needed.
    centres = placed[new_tris].mean(1)
    ctri, _ = _closest_on_triangles(centres, tri_pts)
    out: dict[int, models3d.Mesh] = {}
    tri_draw = np.array([owner[t][0] for t in ctri])
    for di in sorted(set(tri_draw.tolist())):
        sel = np.nonzero(tri_draw == di)[0]
        pos, nrm, uv, col, slot = [], [], [], [], []
        for k in sel:
            _, corners = owner[ctri[k]]
            mesh = meshes[di][1]
            a, b, c = mesh.positions[corners]
            e1, e2 = b - a, c - a
            g = np.array([[e1 @ e1, e1 @ e2], [e1 @ e2, e2 @ e2]])
            det = np.linalg.det(g)
            for vi in new_tris[k]:
                if abs(det) > 1e-18:
                    r = placed[vi] - a
                    v, w = np.linalg.solve(g, [r @ e1, r @ e2])
                    bc = np.array([1 - v - w, v, w])
                else:
                    bc = np.array([1.0, 0.0, 0.0])
                pos.append(placed[vi]); nrm.append(normals[vi]); col.append(colors[vi])
                uv.append(bc @ mesh.texcoords[corners]); slot.append(slots[vi])
        m = models3d.Mesh(np.array(pos), np.array(nrm), np.array(uv), np.array(col), np.array(slot, np.int32),
                          np.arange(len(pos)).reshape(-1, 3))
        m.normals = models3d.smooth_normals(m)
        out[di] = m
    return out, meshes


def write_edited_obj(path: Path, model: models3d.Model, fitted: dict, meshes, tex_sizes: dict[str, tuple]) -> None:
    """edited.obj for `models build`: one group per shape, uv in 0..1 of the shape's texture.
    The model's other shapes go to hidden.txt next to it: the new mesh covers them."""
    covered = {model.shapes[meshes[di][0].shape].name for di in fitted}
    hidden = sorted({model.shapes[d.shape].name for d, _ in meshes} - covered)
    (path.parent / "hidden.txt").write_text("".join(n + "\n" for n in hidden), encoding="utf-8")
    lines = ["# fitted by ai3d.py: groups are the shapes they replace"]
    base = 1
    for di, mesh in fitted.items():
        d, _ = meshes[di]
        name = model.shapes[d.shape].name
        w, h = tex_sizes.get(name, (0, 0))
        lines.append(f"g {name}")
        for p in mesh.positions:
            lines.append(f"v {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}")
        for s, t in mesh.texcoords:
            lines.append(f"vt {s / w:.6f} {1 - t / h:.6f}" if w and h else "vt 0 0")
        for nn in mesh.normals:
            lines.append(f"vn {nn[0]:.5f} {nn[1]:.5f} {nn[2]:.5f}")
        for a, b, c in mesh.triangles:
            lines.append(f"f {a + base}/{a + base}/{a + base} {b + base}/{b + base}/{b + base} {c + base}/{c + base}/{c + base}")
        base += len(mesh.positions)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------- textured models

def uv_coverage(uv: np.ndarray, tris: np.ndarray, width: int, height: int) -> np.ndarray:
    """Which texels of a width x height texture the triangles' UVs (v down, 0..1) cover."""
    from PIL import ImageDraw
    img = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(img)
    px = uv * np.array([width, height])
    for t in tris:
        draw.polygon([tuple(px[i]) for i in t], fill=255, outline=255)
    return np.array(img) > 0


def fill_atlas_gaps(atlas: np.ndarray, uv: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """The atlas, fully opaque, with every texel no triangle covers filled from the nearest covered
    colours (push-pull). AI atlases are many small islands on transparent ground; scaled down to a
    DS texture slot, island borders would otherwise blend with that ground (torn edges)."""
    h, w = atlas.shape[:2]
    covered = uv_coverage(uv, tris, w, h) & (atlas[..., 3] > 0)
    # push: sums of colour and coverage per 2x2 block, down to one texel
    levels = [(atlas[..., :3].astype(np.float32) * covered[..., None], covered.astype(np.float32))]
    while max(levels[-1][1].shape) > 1:
        c, m = levels[-1]
        ph, pw = m.shape[0] % 2, m.shape[1] % 2
        c = np.pad(c, ((0, ph), (0, pw), (0, 0)))
        m = np.pad(m, ((0, ph), (0, pw)))
        y, x = m.shape[0] // 2, m.shape[1] // 2
        levels.append((c.reshape(y, 2, x, 2, 3).sum((1, 3)), m.reshape(y, 2, x, 2).sum((1, 3))))
    # pull: each level keeps its own average where it has coverage, the coarser fill elsewhere
    c, m = levels[-1]
    filled = c / np.maximum(m, 1e-6)[..., None]
    for c, m in reversed(levels[:-1]):
        up = np.repeat(np.repeat(filled, 2, 0), 2, 1)[:m.shape[0], :m.shape[1]]
        weight = np.minimum(m, 1.0)[..., None]
        filled = c / np.maximum(m, 1e-6)[..., None] * weight + up * (1 - weight)
    out = np.empty((h, w, 4), np.uint8)
    out[..., :3] = np.clip(np.rint(filled), 0, 255)
    out[..., 3] = 255
    return out


def read_glb_textured(path: Path):
    """positions (n,3), triangles (m,3), uvs (n,2, v down) and the base-colour texture (RGBA array)
    of a GLB. Primitives with different textures are packed side by side into one atlas."""
    data = path.read_bytes()
    pos = 12
    gltf, binary = None, b""
    while pos < len(data):
        length, kind = struct.unpack_from("<II", data, pos)
        chunk = data[pos + 8:pos + 8 + length]
        if kind == 0x4E4F534A:
            gltf = json.loads(chunk.decode("utf-8"))
        elif kind == 0x004E4942:
            binary = chunk
        pos += 8 + length
    if gltf is None:
        raise ValueError(f"{path} is not a GLB")
    if "EXT_meshopt_compression" in gltf.get("extensionsUsed", []):
        raise SystemExit(f"{path} uses meshopt compression; ask for the model without `compress`")
    comp = {5120: "b", 5121: "B", 5122: "h", 5123: "H", 5125: "I", 5126: "f"}
    size = {"b": 1, "B": 1, "h": 2, "H": 2, "I": 4, "f": 4}
    ncomp = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}

    def accessor(i: int) -> np.ndarray:
        a = gltf["accessors"][i]
        view = gltf["bufferViews"][a["bufferView"]]
        fmt = comp[a["componentType"]]
        n = ncomp[a["type"]]
        start = view.get("byteOffset", 0) + a.get("byteOffset", 0)
        stride = view.get("byteStride") or size[fmt] * n
        rows = np.ndarray((a["count"], n), dtype=np.dtype("<" + fmt), buffer=binary, offset=start,
                          strides=(stride, size[fmt]))
        out = rows.astype(np.float64)
        if a.get("normalized") and fmt in "BH":
            out /= 255.0 if fmt == "B" else 65535.0
        return out

    def image(tex_index: int) -> Image.Image:
        src = gltf["textures"][tex_index]["source"]
        img = gltf["images"][src]
        view = gltf["bufferViews"][img["bufferView"]]
        raw = binary[view.get("byteOffset", 0):view.get("byteOffset", 0) + view["byteLength"]]
        return Image.open(io.BytesIO(raw)).convert("RGBA")

    def node_matrix(node: dict) -> np.ndarray:
        if "matrix" in node:
            return np.array(node["matrix"], np.float64).reshape(4, 4).T
        m = np.eye(4)
        if "scale" in node:
            m = np.diag(list(node["scale"]) + [1.0]) @ m
        if "rotation" in node:
            x, y, z, w = node["rotation"]
            r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                          [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                          [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
            rm = np.eye(4); rm[:3, :3] = r
            m = rm @ m
        if "translation" in node:
            tm = np.eye(4); tm[:3, 3] = node["translation"]
            m = tm @ m
        return m

    prims = []          # (positions, triangles, uvs, texture index or None)

    def walk(i: int, parent: np.ndarray) -> None:
        node = gltf["nodes"][i]
        m = parent @ node_matrix(node)
        if "mesh" in node:
            for prim in gltf["meshes"][node["mesh"]]["primitives"]:
                if prim.get("mode", 4) != 4:
                    continue
                p = accessor(prim["attributes"]["POSITION"])
                p = (m @ np.c_[p, np.ones(len(p))].T).T[:, :3]
                idx = accessor(prim["indices"]).reshape(-1).astype(np.int64) if "indices" in prim else np.arange(len(p))
                uv = accessor(prim["attributes"]["TEXCOORD_0"]) if "TEXCOORD_0" in prim["attributes"] else np.zeros((len(p), 2))
                tex = None
                if "material" in prim:
                    pbr = gltf["materials"][prim["material"]].get("pbrMetallicRoughness", {})
                    if "baseColorTexture" in pbr:
                        tex = pbr["baseColorTexture"]["index"]
                prims.append((p, idx.reshape(-1, 3), uv, tex))
        for c in node.get("children", []):
            walk(c, m)

    scene = gltf.get("scenes", [{}])[gltf.get("scene", 0)]
    for i in scene.get("nodes", range(len(gltf.get("nodes", [])))):
        walk(i, np.eye(4))

    textures = sorted({t for *_, t in prims if t is not None})
    images = {t: image(t) for t in textures}
    if images:
        height = max(im.height for im in images.values())
        resized = {t: im.resize((max(1, im.width * height // im.height), height), Image.LANCZOS) for t, im in images.items()}
        width = sum(im.width for im in resized.values())
        atlas = Image.new("RGBA", (width, height))
        offset = {}
        x = 0
        for t in textures:
            atlas.paste(resized[t], (x, 0))
            offset[t] = (x / width, resized[t].width / width)
            x += resized[t].width
    else:
        atlas, offset = Image.new("RGBA", (8, 8), (200, 200, 200, 255)), {}
    positions, triangles, uvs = [], [], []
    base = 0
    for p, tris, uv, tex in prims:
        u = uv.copy()
        if tex is not None:
            x0, w = offset[tex]
            u[:, 0] = x0 + np.mod(u[:, 0], 1.0) * w
        positions.append(p)
        triangles.append(tris + base)
        uvs.append(u)
        base += len(p)
    return np.concatenate(positions), np.concatenate(triangles), np.concatenate(uvs), np.array(atlas)


def part_names(model: models3d.Model, draw) -> set[str]:
    """A draw's shape name and its material's name (either names a part on the command line)."""
    mat = model.materials[draw.material] if draw.material is not None and draw.material < len(model.materials) else None
    return {model.shapes[draw.shape].name} | ({mat.name} if mat else set())


def _first_hits(origins: np.ndarray, direction: np.ndarray, tri_pts: np.ndarray, max_t: float):
    """Where rays from `origins` along one `direction` first meet the triangles (Moller-Trumbore):
    (t, triangle index, barycentric u, v); t = inf where a ray meets nothing before max_t."""
    a, e1, e2 = tri_pts[:, 0], tri_pts[:, 1] - tri_pts[:, 0], tri_pts[:, 2] - tri_pts[:, 0]
    p = np.cross(direction[None], e2)                      # (m,3)
    det = (e1 * p).sum(1)
    ok_tri = np.abs(det) > 1e-12
    inv = np.where(ok_tri, 1.0 / np.where(ok_tri, det, 1.0), 0.0)
    best = np.full(len(origins), np.inf)
    best_tri = np.zeros(len(origins), np.int64)
    best_uv = np.zeros((len(origins), 2))
    for s in range(0, len(origins), 64):
        o = origins[s:s + 64]
        tv = o[:, None, :] - a[None]                       # (k,m,3)
        u = (tv * p[None]).sum(-1) * inv[None]
        q = np.cross(tv, e1[None])
        v = (q * direction[None, None]).sum(-1) * inv[None]
        t = (q * e2[None]).sum(-1) * inv[None]
        hit = ok_tri[None] & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0) & (t < max_t)
        t = np.where(hit, t, np.inf)
        idx = t.argmin(1)
        rows = np.arange(len(o))
        best[s:s + 64] = t[rows, idx]
        best_tri[s:s + 64] = idx
        best_uv[s:s + 64] = np.stack([u[rows, idx], v[rows, idx]], 1)
    return best, best_tri, best_uv


def conform(decal: models3d.Mesh, surface: models3d.Mesh, gap: float, center: np.ndarray,
            reach: float) -> models3d.Mesh:
    """A decal part (eye, mouth or brow patch) laid onto a new surface: subdivided, then each
    vertex moved along the patch's facing to the FRONTMOST point of the surface there (hair
    included: eyes and brows sit on top of bangs, as Wind Waker and Phantom Hourglass draw them),
    plus `gap`, and lit with that surface's normal. The game keeps drawing the part, so its
    animation stays. Where nothing is hit within `reach`, the nearest surface point is used."""
    fine = models3d.subdivide_linear(decal, 2)
    normals = surface.normals
    if (normals * (surface.positions - surface.positions.mean(0))).sum() < 0:
        normals = -normals                         # outward, whatever the mesh's winding
    tri_pts = surface.positions[surface.triangles]
    # the patch's facing: its area-weighted normal, pointing away from the model's centre
    pp = decal.positions[decal.triangles]
    facing = np.cross(pp[:, 1] - pp[:, 0], pp[:, 2] - pp[:, 0])
    facing = np.where(((facing * (pp.mean(1) - center)).sum(1) < 0)[:, None], -facing, facing).sum(0)
    facing /= max(np.linalg.norm(facing), 1e-12)
    t, hit_tri, uv = _first_hits(fine.positions + facing * reach, -facing, tri_pts, 2 * reach)
    bary = np.stack([1 - uv[:, 0] - uv[:, 1], uv[:, 0], uv[:, 1]], 1)
    missed = ~np.isfinite(t)
    if missed.any():
        near_tri, near_bary = _closest_on_triangles(fine.positions[missed], tri_pts)
        hit_tri[missed], bary[missed] = near_tri, near_bary
    q = (bary[:, :, None] * tri_pts[hit_tri]).sum(1)
    n = (bary[:, :, None] * normals[surface.triangles[hit_tri]]).sum(1)
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    return models3d.Mesh(q + facing * gap, n, fine.texcoords, fine.colors, fine.slots, fine.triangles)


def clear_skin(img: np.ndarray) -> np.ndarray:
    """A face patch's texture as a decal: transparent except its dark features (mouth line,
    brows, eye lines), the parts the HD turnaround painted out."""
    luma = img[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    out = img.copy()
    out[..., 3] = np.minimum(img[..., 3], np.clip((150 - luma) / 50, 0, 1) * 255).astype(np.uint8)
    return out


def _bone_key(matrix) -> bytes:
    """A bone's identity across draws: its bind-pose matrix (the same node loaded into a stack slot
    holds the same matrix in every draw; slot NUMBERS are reused between draws)."""
    return np.round(np.asarray(matrix, np.float64), 4).tobytes()


def fit_textured(new_pos: np.ndarray, new_tris: np.ndarray, new_uv: np.ndarray, model: models3d.Model,
                 hosts: list[str], texture_size: tuple[int, int], ignore: set[str] = frozenset()):
    """The new mesh placed on the original (as in fit), keeping its own texture coordinates (into
    the AI texture, which takes the hosts' texture slot), split over the `hosts`: parts (shape
    names) drawn with that texture, whose display lists it replaces. Every vertex takes the bone of
    the nearest point on the original's surface. A part can only use the bones its draw has loaded
    into the matrix stack (the game reloads slots between draws: Link's head and hat bones are
    loaded for the cap, not for the body), so each triangle goes to the host that has its bones,
    and a vertex whose bone that host lacks takes the nearest original vertex's bone among the ones
    it has. Parts in `ignore` (shape or material names: ones the new mesh leaves out, like a sword
    the game keeps drawing) play no part in placing it or in its bones. Returns the whole mesh
    (bind pose, for previews and decals), [(host draw, its mesh)] and every draw."""
    meshes = models3d.model_meshes(model)
    host_ids = [di for di, (d, _) in enumerate(meshes) if model.shapes[d.shape].name in hosts]
    if not host_ids:
        raise SystemExit(f"{model.name} has none of the parts {hosts}")
    used = [(d, m) for d, m in meshes if not part_names(model, d) & ignore]
    placed = _place(new_pos, used)
    # every original vertex's bone
    bones: dict[bytes, int] = {}
    tri_pts, tri_bones, vert_pts, vert_bones = [], [], [], []
    for di, (d, mesh) in enumerate(meshes):
        if part_names(model, d) & ignore:
            continue
        keys = [bones.setdefault(_bone_key(d.current if s < 0 else d.stack[s]), len(bones)) for s in mesh.slots]
        vert_pts.append(mesh.positions)
        vert_bones += keys
        for tr in mesh.triangles:
            tri_pts.append(mesh.positions[tr])
            tri_bones.append([keys[i] for i in tr])
    vert_pts = np.concatenate(vert_pts)
    vert_bones = np.array(vert_bones)
    tri_idx, bary = _closest_on_triangles(placed, np.array(tri_pts))
    vbone = np.array([tri_bones[t][int(np.argmax(b))] for t, b in zip(tri_idx, bary)])
    # the bones each host's draw can reach: stack slots first, the current matrix if no slot holds it
    tables = []
    for di in host_ids:
        d = meshes[di][0]
        table: dict[int, int] = {}
        for k, mat in enumerate(d.stack):
            if mat is not None and _bone_key(mat) in bones:
                table.setdefault(bones[_bone_key(mat)], k)
        if _bone_key(d.current) in bones:
            table.setdefault(bones[_bone_key(d.current)], -1)
        tables.append(table)
    # each triangle to the host holding most of its bones (ties: the first host drawn)
    tri_host = np.array([max(range(len(host_ids)), key=lambda h: (sum(int(b) in tables[h] for b in vbone[t]), -h))
                         for t in new_tris])
    w, h = texture_size
    whole = models3d.Mesh(placed, np.zeros_like(placed), new_uv * np.array([w, h]),
                          np.full((len(placed), 3), 31.0), np.zeros(len(placed), np.int32),
                          new_tris.astype(np.int32))
    whole.normals = models3d.smooth_normals(whole)
    out, remapped = [], 0
    for hi, di in enumerate(host_ids):
        tris = new_tris[tri_host == hi]
        if not len(tris):
            continue
        verts, inverse = np.unique(tris.ravel(), return_inverse=True)
        table = tables[hi]
        slots = np.zeros(len(verts), np.int32)
        reachable = np.isin(vert_bones, list(table))
        for j, v in enumerate(verts):
            b = int(vbone[v])
            if b not in table:
                remapped += 1
                cand = np.flatnonzero(reachable)
                b = int(vert_bones[cand[((vert_pts[cand] - placed[v]) ** 2).sum(1).argmin()]])
            slots[j] = table[b]
        sub = models3d.Mesh(placed[verts], whole.normals[verts], whole.texcoords[verts],
                            whole.colors[verts], slots, inverse.reshape(-1, 3).astype(np.int32))
        out.append((meshes[di][0], sub))
    log(f"{len(bones)} bones; triangles per host: " + ", ".join(
        f"{model.shapes[d.shape].name} {len(m.triangles)}" for d, m in out)
        + (f"; {remapped} vertices took the nearest bone their host has" if remapped else ""))
    whole.slots = np.zeros(len(placed), np.int32)
    return whole, out, meshes
