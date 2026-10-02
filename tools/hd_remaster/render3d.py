"""A small software rasteriser for model previews (no GPU, numpy only): z-buffered triangles,
textured with the model's own textures when they are given, lit by one directional light.

Used to check extracted bind poses and to make the reference images an AI model generator is
given (front / side / three-quarter views of the original model).
"""
from __future__ import annotations

import math

import numpy as np


def look(yaw_deg: float, pitch_deg: float) -> np.ndarray:
    """A rotation that turns the model by yaw (around Y) and tilts it by pitch (around X)."""
    y, p = math.radians(yaw_deg), math.radians(pitch_deg)
    ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
    rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
    return rx @ ry


def render(parts: list[dict], size: int = 512, yaw: float = 0.0, pitch: float = 10.0,
           background: tuple[int, int, int] | None = (40, 44, 52), frame: tuple | None = None,
           shade: bool = True) -> np.ndarray:
    """parts: dicts with positions (n,3), triangles (m,3) and optionally uv (n,2, 0..1 with v down),
    texture (h,w,4 uint8), colors (n,3 0..1). Orthographic, fitted to the parts' bounds (or to
    `frame` = (center xyz, half extent) so several renders share a scale). shade=False draws flat
    colour (no light), for pictures a model generator must not mistake for paint. background=None
    returns RGBA, transparent where nothing is drawn."""
    rot = look(yaw, pitch)
    allp = np.concatenate([p["positions"] for p in parts if len(p["positions"])])
    if frame is None:
        lo, hi = allp.min(0), allp.max(0)
        center, half = (lo + hi) / 2, float(max(hi - lo) / 2 * 1.1) or 1.0
    else:
        center, half = frame
    img = np.zeros((size, size, 3), np.float32)
    img[:] = np.array(background or (0, 0, 0), np.float32) / 255
    zbuf = np.full((size, size), np.inf, np.float32)
    light = np.array([0.4, 0.6, 0.7])
    light /= np.linalg.norm(light)
    for part in parts:
        pos = (part["positions"] - center) @ rot.T
        sx = (pos[:, 0] / half * 0.5 + 0.5) * size
        sy = (0.5 - pos[:, 1] / half * 0.5) * size
        sz = -pos[:, 2]
        tex = part.get("texture")
        uv = part.get("uv")
        cols = part.get("colors")
        for tri in part["triangles"]:
            a, b, c = tri
            x0, y0, x1, y1, x2, y2 = sx[a], sy[a], sx[b], sy[b], sx[c], sy[c]
            minx, maxx = max(int(min(x0, x1, x2)), 0), min(int(max(x0, x1, x2)) + 1, size)
            miny, maxy = max(int(min(y0, y1, y2)), 0), min(int(max(y0, y1, y2)) + 1, size)
            if minx >= maxx or miny >= maxy:
                continue
            den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
            if abs(den) < 1e-9:
                continue
            xs, ys = np.meshgrid(np.arange(minx, maxx) + 0.5, np.arange(miny, maxy) + 0.5)
            w0 = ((y1 - y2) * (xs - x2) + (x2 - x1) * (ys - y2)) / den
            w1 = ((y2 - y0) * (xs - x2) + (x0 - x2) * (ys - y2)) / den
            w2 = 1 - w0 - w1
            inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            if not inside.any():
                continue
            z = w0 * sz[a] + w1 * sz[b] + w2 * sz[c]
            region = zbuf[miny:maxy, minx:maxx]
            vis = inside & (z < region)
            if not vis.any():
                continue
            n = np.cross(pos[b] - pos[a], pos[c] - pos[a])
            ln = np.linalg.norm(n)
            lit = 0.45 + 0.55 * abs(float(n @ (rot @ light)) / ln) if ln > 1e-12 and shade else 1.0
            if tex is not None and uv is not None:
                u = w0 * uv[a, 0] + w1 * uv[b, 0] + w2 * uv[c, 0]
                v = w0 * uv[a, 1] + w1 * uv[b, 1] + w2 * uv[c, 1]
                th, tw = tex.shape[:2]
                tx = (np.floor(u * tw).astype(int)) % tw
                ty = (np.floor(v * th).astype(int)) % th
                texel = tex[ty, tx].astype(np.float32) / 255
                vis &= texel[..., 3] > 0.5
                color = texel[..., :3]
            elif cols is not None:
                color = (w0[..., None] * cols[a] + w1[..., None] * cols[b] + w2[..., None] * cols[c])
            else:
                color = np.full(z.shape + (3,), 0.8, np.float32)
            if not vis.any():
                continue
            region[vis] = z[vis]
            img[miny:maxy, minx:maxx][vis] = (color * lit)[vis]
    rgb = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    if background is not None:
        return rgb
    alpha = np.where(np.isfinite(zbuf), 255, 0).astype(np.uint8)
    return np.dstack([rgb, alpha])
