"""3D models: parse NSBMD (MDL0) models, rebuild their bind pose, export them, and list the
display-list keys a runtime model replacement can look up.

A model's geometry lives in its shapes: each shape is a GX display list (packed geometry
commands: vertices, normals, texture coordinates, colours, matrix-stack restores) that the
SDK sends to the geometry FIFO byte for byte (NNS_G3dGeSendDL, usually by DMA). So, like the
texture keys, a shape can be recognised at runtime by a content hash of its display list:

    mdl1_<size>_<xxh64 of the display list>

The bind pose comes from the model's render commands (SBC): they walk the node tree, store
node matrices into matrix-stack slots (NODEDESC), blend skinned slots (NODEMIX, with the
inverse bind matrices), pick materials and draw shapes. Display-list vertices are in the space
of the slot they restore, so each exported vertex is that slot's bind matrix times the vertex.

Matrices here are column-vector 4x4 (world = M @ v). NitroSystem stores row-vector matrices
(translation in the last row), so they are transposed on reading.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np
import xxhash

from nitro import Blob, blocks, read_dict, u16, u32

STACK_SLOTS = 32
GX_PARAMS = {
    0x00: 0, 0x10: 1, 0x11: 0, 0x12: 1, 0x13: 1, 0x14: 1, 0x15: 0, 0x16: 16, 0x17: 12, 0x18: 16,
    0x19: 12, 0x1A: 9, 0x1B: 3, 0x1C: 3, 0x20: 1, 0x21: 1, 0x22: 1, 0x23: 2, 0x24: 1, 0x25: 1,
    0x26: 1, 0x27: 1, 0x28: 1, 0x29: 1, 0x2A: 1, 0x2B: 1, 0x30: 1, 0x31: 1, 0x32: 1, 0x33: 1,
    0x34: 32, 0x40: 1, 0x41: 0, 0x50: 1, 0x60: 1, 0x70: 3, 0x71: 2, 0x72: 1,
}


def fx32(b: bytes, o: int) -> float:
    return struct.unpack_from("<i", b, o)[0] / 4096.0


def fx16(b: bytes, o: int) -> float:
    return struct.unpack_from("<h", b, o)[0] / 4096.0


def sext(v: int, bits: int) -> int:
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def shape_key(dl: bytes) -> str:
    return f"mdl1_{len(dl)}_{xxhash.xxh64_hexdigest(dl)}"


# ---------------------------------------------------------------------------- structures

@dataclass
class Node:
    name: str
    xform: np.ndarray            # local transform, 4x4 column-vector


@dataclass
class Material:
    name: str
    texture: str | None
    palette: str | None
    teximage_param: int
    dif_amb: int
    spe_emi: int
    polygon_attr: int


@dataclass
class Shape:
    name: str
    dl: bytes

    @property
    def key(self) -> str:
        return shape_key(self.dl)


@dataclass
class Draw:
    material: int | None
    shape: int
    current: np.ndarray          # the current matrix when SHP ran
    stack: list[np.ndarray]      # the matrix stack then (display lists restore slots from it)


@dataclass
class Model:
    source: str
    name: str
    nodes: list[Node]
    materials: list[Material]
    shapes: list[Shape]
    draws: list[Draw]
    up_scale: float
    down_scale: float
    bbox: tuple[float, ...]


@dataclass
class Mesh:
    """One draw's triangles in model space (bind pose)."""
    positions: np.ndarray        # (n, 3)
    normals: np.ndarray          # (n, 3), zero where the display list had none
    texcoords: np.ndarray        # (n, 2) in texels (TEXCOORD s/t), before any texture matrix
    colors: np.ndarray           # (n, 3) 0..31
    slots: np.ndarray            # (n,) matrix-stack slot each vertex was transformed by, -1 = current
    triangles: np.ndarray        # (m, 3) vertex indices
    locals_: np.ndarray = field(default=None)   # (n, 3) positions as the display list holds them


# ---------------------------------------------------------------------------- MDL0 parsing

def _pivot(select: int, neg: int, a: float, b: float) -> np.ndarray:
    """The compressed rotation NitroSystem stores for axis-aligned joints (NNSi_G3dPivot...)."""
    one = -1.0 if neg & 1 else 1.0
    c = -b if neg & 2 else b
    d = -a if neg & 4 else a
    rows = {
        0: (one, 0, 0, 0, a, b, 0, c, d),
        1: (0, one, 0, a, 0, b, c, 0, d),
        2: (0, 0, one, a, b, 0, c, d, 0),
        3: (0, a, b, one, 0, 0, 0, c, d),
        4: (a, 0, b, 0, one, 0, c, 0, d),
        5: (a, b, 0, 0, 0, one, c, d, 0),
        6: (0, a, b, 0, c, d, one, 0, 0),
        7: (a, 0, b, c, 0, d, 0, one, 0),
        8: (a, b, 0, c, d, 0, 0, 0, one),
    }.get(select, (1, 0, 0, 0, 1, 0, 0, 0, 1))
    return np.array(rows, dtype=np.float64).reshape(3, 3)


def _read_node(b: bytes, o: int, name: str, transpose_rot: bool) -> Node:
    flags = u16(b, o)
    m0 = fx16(b, o + 2)
    p = o + 4
    t = np.zeros(3)
    if not flags & 1:
        t = np.array([fx32(b, p), fx32(b, p + 4), fx32(b, p + 8)])
        p += 12
    rot = np.eye(3)
    if not flags & 2:
        if flags & 8:
            a, bb = fx16(b, p), fx16(b, p + 2)
            p += 4
            rot = _pivot((flags >> 4) & 0xF, (flags >> 8) & 0xF, a, bb)
        else:
            vals = [m0] + [fx16(b, p + 2 * i) for i in range(8)]
            p += 16
            rot = np.array(vals).reshape(3, 3)
        if transpose_rot:
            rot = rot.T
    s = np.ones(3)
    if not flags & 4:
        s = np.array([fx32(b, p), fx32(b, p + 4), fx32(b, p + 8)])
    m = np.eye(4)
    m[:3, :3] = rot @ np.diag(s)
    m[:3, 3] = t
    return Node(name, m)


def _read_inv_binds(b: bytes, o: int, count: int) -> list[np.ndarray]:
    out = []
    for i in range(count):
        q = o + i * 0x54                       # MtxFx43 position + MtxFx33 direction
        if q + 0x30 > len(b):                  # unskinned models point this at their end
            break
        rows = np.array([fx32(b, q + 4 * k) for k in range(12)]).reshape(4, 3)
        m = np.eye(4)
        m[:3, :3] = rows[:3].T
        m[:3, 3] = rows[3]
        out.append(m)
    return out


def _scale(f: float) -> np.ndarray:
    m = np.eye(4)
    m[0, 0] = m[1, 1] = m[2, 2] = f
    return m


def parse_model(b: bytes, model: int, name: str, source: str, transpose_rot: bool = True) -> Model:
    render = model + u32(b, model + 0x04)
    mat = model + u32(b, model + 0x08)
    shp = model + u32(b, model + 0x0C)
    inv = model + u32(b, model + 0x10)
    up, down = fx32(b, model + 0x1C), fx32(b, model + 0x20)
    bbox = tuple(fx16(b, model + 0x2C + 2 * i) for i in range(6))
    objs = model + 0x40

    nodes = [_read_node(b, objs + u32(e, 0), n, transpose_rot) for n, e in read_dict(b, objs)]

    # materials, with the texture and palette each one names (as in tex3d.parse_mdl0_pairs)
    tex_of: dict[int, str] = {}
    pal_of: dict[int, str] = {}
    for names, pairing_off in ((tex_of, u16(b, mat)), (pal_of, u16(b, mat + 2))):
        for pname, pe in read_dict(b, mat + pairing_off):
            lst, cnt = u16(pe, 0), pe[2]
            for k in range(cnt):
                names[b[mat + lst + k]] = pname
    materials = []
    for idx, (mname, me) in enumerate(read_dict(b, mat + 4)):
        mo = mat + u32(me, 0)
        materials.append(Material(mname, tex_of.get(idx), pal_of.get(idx), u32(b, mo + 0x14),
                                  u32(b, mo + 0x04), u32(b, mo + 0x08), u32(b, mo + 0x0C)))

    shapes = []
    for sname, se in read_dict(b, shp):
        so = shp + u32(se, 0)
        dl_off, dl_size = u32(b, so + 8), u32(b, so + 12)
        shapes.append(Shape(sname, bytes(b[so + dl_off:so + dl_off + dl_size])))

    inv_binds = _read_inv_binds(b, inv, len(nodes)) if u32(b, model + 0x10) else []
    draws = _run_render_commands(b, render, nodes, inv_binds, up, down)
    return Model(source, name, nodes, materials, shapes, draws, up, down, bbox)


def _run_render_commands(b: bytes, p: int, nodes: list[Node], inv_binds: list[np.ndarray],
                         up: float, down: float) -> list[Draw]:
    """Run a model's SBC in bind pose: which shapes are drawn, with which material and matrices."""
    stack = [np.eye(4) for _ in range(STACK_SLOTS)]
    cur = np.eye(4)
    material: int | None = None
    draws: list[Draw] = []
    for _ in range(100000):
        op = b[p]
        cmd = op & 0x1F
        if cmd == 0x01:                                 # RET
            break
        if cmd == 0x00:                                 # NOP
            p += 1
        elif cmd == 0x02:                               # NODE id, visible
            p += 3
        elif cmd == 0x03:                               # MTX restore
            cur = stack[b[p + 1] % STACK_SLOTS].copy()
            p += 2
        elif cmd == 0x04:                               # MAT
            material = b[p + 1]
            p += 2
        elif cmd == 0x05:                               # SHP
            draws.append(Draw(material, b[p + 1], cur.copy(), [s.copy() for s in stack]))
            p += 2
        elif cmd == 0x06:                               # NODEDESC node, parent, flags [dest] [src]
            node = b[p + 1]
            q = p + 4
            dest = src = None
            if op & 0x20:
                dest = b[q]
                q += 1
            if op & 0x40:
                src = b[q]
                q += 1
            if src is not None:
                cur = stack[src % STACK_SLOTS].copy()
            if node < len(nodes):
                cur = cur @ nodes[node].xform
            if dest is not None:
                stack[dest % STACK_SLOTS] = cur.copy()
            p = q
        elif cmd in (0x07, 0x08):                       # BB / BBY node [dest] [src]
            q = p + 2
            if op & 0x20:
                q += 1
            if op & 0x40:
                src = b[q]
                cur = stack[src % STACK_SLOTS].copy()
                q += 1
            p = q
        elif cmd == 0x09:                               # NODEMIX dest, n, n x (slot, node, weight)
            dest, n = b[p + 1], b[p + 2]
            m = np.zeros((4, 4))
            for k in range(n):
                slot, node, weight = b[p + 3 + 3 * k:p + 6 + 3 * k]
                ib = inv_binds[node] if node < len(inv_binds) else np.eye(4)
                m += (weight / 256.0) * (stack[slot % STACK_SLOTS] @ ib)
            stack[dest % STACK_SLOTS] = m
            p += 3 + 3 * n
        elif cmd == 0x0A:                               # CALLDL offset, size
            p += 9
        elif cmd == 0x0B:                               # POSSCALE (0x0B up, 0x2B down)
            cur = cur @ _scale(down if op & 0x20 else up)
            p += 1
        elif cmd in (0x0C, 0x0D):                       # ENVMAP / PRJMAP
            p += 3
        else:
            break
    return draws


def models_in(blob: Blob, transpose_rot: bool = True) -> list[Model]:
    b = blob.data
    if blob.magic != b"BMD0":
        return []
    m = blocks(b).get(b"MDL0")
    if m is None:
        return []
    out = []
    for name, e in read_dict(b, m + 8):
        try:
            out.append(parse_model(b, m + u32(e, 0), name, blob.path, transpose_rot))
        except (IndexError, struct.error, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------- display lists

def decode_display_list(dl: bytes, current: np.ndarray, stack: list[np.ndarray]) -> Mesh:
    """Run a shape's display list the way the geometry engine does, in bind pose."""
    words = struct.unpack_from(f"<{len(dl) // 4}I", dl)
    pos, nrm, uv, col, slots, tris, loc = [], [], [], [], [], [], []
    m = current
    slot = -1
    v = [0, 0, 0]
    normal = (0.0, 0.0, 0.0)
    tc = (0.0, 0.0)
    color = (31, 31, 31)
    prim: list[int] = []
    mode = 0

    def emit() -> None:
        world = m @ np.array([v[0] / 4096, v[1] / 4096, v[2] / 4096, 1.0])
        n = m[:3, :3] @ np.array(normal)
        length = float(np.linalg.norm(n))
        pos.append(world[:3])
        nrm.append(n / length if length > 1e-9 else n)
        uv.append(tc)
        col.append(color)
        slots.append(slot)
        loc.append((v[0] / 4096, v[1] / 4096, v[2] / 4096))
        prim.append(len(pos) - 1)
        if mode == 0 and len(prim) == 3:
            tris.append(tuple(prim)); prim.clear()
        elif mode == 1 and len(prim) == 4:
            tris.append((prim[0], prim[1], prim[2])); tris.append((prim[0], prim[2], prim[3])); prim.clear()
        elif mode == 2 and len(prim) >= 3:
            a, b_, c = prim[-3:]
            tris.append((a, b_, c) if len(prim) % 2 else (b_, a, c))
        elif mode == 3 and len(prim) >= 4 and len(prim) % 2 == 0:
            a, b_, c, d = prim[-4:]
            tris.append((a, b_, d)); tris.append((a, d, c))

    i = 0
    while i < len(words):
        packed = words[i]
        i += 1
        cmds = [(packed >> (8 * k)) & 0xFF for k in range(4)]
        # trailing NOPs in a packed word take no parameters
        while len(cmds) > 1 and cmds[-1] == 0:
            cmds.pop()
        for cmd in cmds:
            n = GX_PARAMS.get(cmd)
            if n is None:
                i = len(words)
                break
            par = words[i:i + n]
            i += n
            if len(par) < n:
                break
            if cmd == 0x14:                                  # MTX_RESTORE
                slot = par[0] & 0x1F
                m = stack[slot] if slot < len(stack) else np.eye(4)
            elif cmd == 0x15:                                # MTX_IDENTITY
                m = np.eye(4)
            elif cmd == 0x1B:                                # MTX_SCALE
                m = m @ np.diag([sext(par[0], 32) / 4096, sext(par[1], 32) / 4096, sext(par[2], 32) / 4096, 1.0])
            elif cmd == 0x1C:                                # MTX_TRANS
                t = np.eye(4); t[:3, 3] = [sext(par[k], 32) / 4096 for k in range(3)]
                m = m @ t
            elif cmd == 0x20:                                # COLOR
                c = par[0]
                color = (c & 31, (c >> 5) & 31, (c >> 10) & 31)
            elif cmd == 0x21:                                # NORMAL
                c = par[0]
                normal = (sext(c & 0x3FF, 10) / 512, sext((c >> 10) & 0x3FF, 10) / 512, sext((c >> 20) & 0x3FF, 10) / 512)
            elif cmd == 0x22:                                # TEXCOORD
                c = par[0]
                tc = (sext(c & 0xFFFF, 16) / 16, sext(c >> 16, 16) / 16)
            elif cmd == 0x23:                                # VTX_16
                v = [sext(par[0] & 0xFFFF, 16), sext(par[0] >> 16, 16), sext(par[1] & 0xFFFF, 16)]
                emit()
            elif cmd == 0x24:                                # VTX_10
                c = par[0]
                v = [sext(c & 0x3FF, 10) << 6, sext((c >> 10) & 0x3FF, 10) << 6, sext((c >> 20) & 0x3FF, 10) << 6]
                emit()
            elif cmd in (0x25, 0x26, 0x27):                  # VTX_XY / XZ / YZ
                a, b_ = sext(par[0] & 0xFFFF, 16), sext(par[0] >> 16, 16)
                if cmd == 0x25: v = [a, b_, v[2]]
                elif cmd == 0x26: v = [a, v[1], b_]
                else: v = [v[0], a, b_]
                emit()
            elif cmd == 0x28:                                # VTX_DIFF
                c = par[0]
                v = [v[0] + sext(c & 0x3FF, 10), v[1] + sext((c >> 10) & 0x3FF, 10), v[2] + sext((c >> 20) & 0x3FF, 10)]
                emit()
            elif cmd == 0x40:                                # BEGIN_VTXS
                mode = par[0] & 3
                prim = []
            elif cmd == 0x41:                                # END_VTXS
                prim = []
    as_np = lambda x, w: np.array(x, dtype=np.float64).reshape(-1, w)
    return Mesh(as_np(pos, 3), as_np(nrm, 3), as_np(uv, 2), as_np(col, 3),
                np.array(slots, dtype=np.int32), np.array(tris, dtype=np.int32).reshape(-1, 3), as_np(loc, 3))


def model_meshes(model: Model) -> list[tuple[Draw, Mesh]]:
    out = []
    for d in model.draws:
        if d.shape < len(model.shapes):
            out.append((d, decode_display_list(model.shapes[d.shape].dl, d.current, d.stack)))
    return out
