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


def subdivide_linear(mesh: Mesh, levels: int = 1) -> Mesh:
    """Each triangle split in four at its edge midpoints, `levels` times; every attribute is
    interpolated and positions stay on the original faces (unlike loop_subdivide). An edge's new
    vertex takes the slot of its first end."""
    for _ in range(levels):
        pos, nrm, uv, col, slots = (list(mesh.positions), list(mesh.normals), list(mesh.texcoords),
                                    list(mesh.colors), list(mesh.slots))
        mid: dict[tuple[int, int], int] = {}

        def middle(a: int, b: int) -> int:
            key = (min(a, b), max(a, b))
            if key not in mid:
                mid[key] = len(pos)
                pos.append((mesh.positions[a] + mesh.positions[b]) / 2)
                nrm.append((mesh.normals[a] + mesh.normals[b]) / 2)
                uv.append((mesh.texcoords[a] + mesh.texcoords[b]) / 2)
                col.append((mesh.colors[a] + mesh.colors[b]) / 2)
                slots.append(mesh.slots[key[0]])
            return mid[key]

        tris = []
        for a, b, c in mesh.triangles:
            ab, bc, ca = middle(a, b), middle(b, c), middle(c, a)
            tris += [(a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca)]
        mesh = Mesh(np.array(pos), np.array(nrm), np.array(uv), np.array(col), np.array(slots, np.int32),
                    np.array(tris, np.int32))
    return mesh


def model_meshes(model: Model) -> list[tuple[Draw, Mesh]]:
    out = []
    for d in model.draws:
        if d.shape < len(model.shapes):
            out.append((d, decode_display_list(model.shapes[d.shape].dl, d.current, d.stack)))
    return out


# ---------------------------------------------------------------------------- replacement display lists

def parse_display_list(dl: bytes) -> list[tuple[int, list[int]]]:
    """A display list as (command, parameters) pairs, unpacked as the GX FIFO does: each packed
    word holds up to four command bytes, zero bytes are NOPs, parameters follow in order."""
    words = struct.unpack_from(f"<{len(dl) // 4}I", dl)
    out: list[tuple[int, list[int]]] = []
    i = 0
    while i < len(words):
        packed = words[i]
        i += 1
        for k in range(4):
            cmd = (packed >> (8 * k)) & 0xFF
            if cmd == 0:
                continue
            n = GX_PARAMS.get(cmd)
            if n is None:
                return out
            out.append((cmd, list(words[i:i + n])))
            i += n
    return out


def encode_display_list(commands: list[tuple[int, list[int]]]) -> bytes:
    """Commands back into a display list, one command per packed word (the rest NOPs)."""
    words: list[int] = []
    for cmd, params in commands:
        words.append(cmd)
        words.extend(p & 0xFFFFFFFF for p in params)
    return struct.pack(f"<{len(words)}I", *words)


def vtx16(x: int, y: int, z: int) -> tuple[int, list[int]]:
    """VTX_16 for s3.12 coordinates (clamped to the 16-bit range)."""
    c = [max(-0x8000, min(0x7FFF, int(round(v)))) & 0xFFFF for v in (x, y, z)]
    return 0x23, [c[0] | (c[1] << 16), c[2]]


def absolute_vertices(commands: list[tuple[int, list[int]]], transform) -> list[tuple[int, list[int]]]:
    """Every vertex command as an absolute VTX_16 of transform(x, y, z) (s3.12 units), so a
    display list's geometry can be changed while everything else (matrix restores, normals,
    texture coordinates, colours, primitive types) stays as it was."""
    out = []
    v = [0, 0, 0]
    for cmd, par in commands:
        if cmd == 0x23:
            v = [sext(par[0] & 0xFFFF, 16), sext(par[0] >> 16, 16), sext(par[1] & 0xFFFF, 16)]
        elif cmd == 0x24:
            c = par[0]
            v = [sext(c & 0x3FF, 10) << 6, sext((c >> 10) & 0x3FF, 10) << 6, sext((c >> 20) & 0x3FF, 10) << 6]
        elif cmd in (0x25, 0x26, 0x27):
            a, b_ = sext(par[0] & 0xFFFF, 16), sext(par[0] >> 16, 16)
            v = [a, b_, v[2]] if cmd == 0x25 else ([a, v[1], b_] if cmd == 0x26 else [v[0], a, b_])
        elif cmd == 0x28:
            c = par[0]
            v = [v[0] + sext(c & 0x3FF, 10), v[1] + sext((c >> 10) & 0x3FF, 10), v[2] + sext((c >> 20) & 0x3FF, 10)]
        else:
            out.append((cmd, par))
            continue
        out.append(vtx16(*transform(*v)))
    return out


def write_replacement(folder, key: str, dl: bytes) -> str:
    """models/<key>.dl in a pack folder; key is the replaced shape's mdl1_<size>_<hash>."""
    import os
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, key + ".dl")
    with open(path, "wb") as f:
        f.write(dl)
    return path


# ---------------------------------------------------------------------------- meshes back to display lists

def _slot_matrix(draw: Draw, slot: int) -> np.ndarray:
    return draw.current if slot < 0 else draw.stack[slot]


def mesh_to_display_list(mesh: Mesh, draw: Draw, lit: bool) -> bytes:
    """A bind-pose mesh as a display list for the shape `draw` draws: each vertex goes back into
    the space of its matrix-stack slot (MTX_RESTORE when the slot changes), with TEXCOORD,
    NORMAL (lit shapes; the engine lights it) or COLOR, and VTX_16, as a triangle list."""
    cmds: list[tuple[int, list[int]]] = []
    inverse = {}
    # slot -1 = the matrix the display list starts with; there is no command to get back to it
    # once a slot was restored, so those triangles go first, and later ones use a stack slot
    # holding the same matrix (or, failing that, the slot in use: same place in bind pose)
    alias = next((k for k, m in enumerate(draw.stack) if np.allclose(m, draw.current, atol=1e-6)), None)
    order = sorted(range(len(mesh.triangles)), key=lambda i: int((mesh.slots[mesh.triangles[i]] >= 0).any()))
    current_slot = -1
    cmds.append((0x40, [0]))                                   # BEGIN_VTXS triangles
    for ti in order:
        tri = mesh.triangles[ti]
        for vi in tri:
            slot = int(mesh.slots[vi])
            if slot < 0 and current_slot >= 0:
                slot = alias if alias is not None else current_slot
            if slot != current_slot:
                cmds.append((0x14, [slot]))                    # MTX_RESTORE
                current_slot = slot
            m = _slot_matrix(draw, slot)
            if slot not in inverse:
                inverse[slot] = np.linalg.inv(m)
            inv = inverse[slot]
            s, t = mesh.texcoords[vi]
            cmds.append((0x22, [(int(round(s * 16)) & 0xFFFF) | ((int(round(t * 16)) & 0xFFFF) << 16)]))
            if lit:
                n = m[:3, :3].T @ mesh.normals[vi]
                length = float(np.linalg.norm(n))
                n = n / length if length > 1e-9 else np.array([0.0, 0.0, 1.0])
                q = [max(-511, min(511, int(round(c * 512)))) & 0x3FF for c in n]
                cmds.append((0x21, [q[0] | (q[1] << 10) | (q[2] << 20)]))
            else:
                r, g, b_ = (int(round(c)) & 31 for c in mesh.colors[vi])
                cmds.append((0x20, [r | (g << 5) | (b_ << 10)]))
            local = inv @ np.append(mesh.positions[vi], 1.0)
            cmds.append(vtx16(*(local[:3] * 4096)))
    cmds.append((0x41, []))                                    # END_VTXS
    return encode_display_list(cmds)


def is_lit(dl: bytes) -> bool:
    """Whether a display list lights its vertices (NORMAL) rather than colouring them (COLOR)."""
    return any(cmd == 0x21 for cmd, _ in parse_display_list(dl))


def weld(mesh: Mesh, eps: float = 1e-5) -> np.ndarray:
    """For each vertex, the index of the first vertex at the same bind-pose position: display
    lists repeat a position for every strip and every texture seam."""
    keys = {}
    out = np.zeros(len(mesh.positions), dtype=np.int64)
    for i, p in enumerate(mesh.positions):
        k = tuple(np.round(p / eps).astype(np.int64))
        out[i] = keys.setdefault(k, i)
    return out


def loop_subdivide(mesh: Mesh, smooth: float = 1.0) -> Mesh:
    """One level of Loop subdivision: every triangle into four. Positions are smoothed on the
    welded surface (boundary edges, where the surface is open, keep the boundary rules);
    texture coordinates, colours, normals and slots follow each corner, so texture seams stay
    sharp. `smooth` blends between plain midpoint splitting (0) and full Loop smoothing (1)."""
    wid = weld(mesh)
    tris = mesh.triangles
    n = len(mesh.positions)
    P = mesh.positions
    # welded edges -> the opposite welded vertices (one for a boundary edge, two inside)
    edge_opp: dict[tuple[int, int], list[int]] = {}
    neighbours: dict[int, set[int]] = {}
    for a, b, c in tris:
        wa, wb, wc = wid[a], wid[b], wid[c]
        for u, v, o in ((wa, wb, wc), (wb, wc, wa), (wc, wa, wb)):
            edge_opp.setdefault((min(u, v), max(u, v)), []).append(o)
            neighbours.setdefault(u, set()).add(v)
            neighbours.setdefault(v, set()).add(u)
    boundary_nb: dict[int, list[int]] = {}
    for (u, v), opp in edge_opp.items():
        if len(opp) == 1:
            boundary_nb.setdefault(u, []).append(v)
            boundary_nb.setdefault(v, []).append(u)

    # new positions of the old (welded) vertices
    moved = {}
    for w, nb in neighbours.items():
        if w in boundary_nb:
            bn = boundary_nb[w]
            target = 0.75 * P[w] + 0.125 * (P[bn[0]] + P[bn[-1]]) if len(bn) >= 2 else P[w]
        else:
            k = len(nb)
            beta = 3.0 / 16 if k == 3 else 3.0 / (8 * k)
            target = (1 - k * beta) * P[w] + beta * sum(P[x] for x in nb)
        moved[w] = P[w] + smooth * (target - P[w])

    # edge points, by welded edge
    edge_point = {}
    for (u, v), opp in edge_opp.items():
        mid = 0.5 * (P[u] + P[v])
        if len(opp) == 2:
            target = 0.375 * (P[u] + P[v]) + 0.125 * (P[opp[0]] + P[opp[1]])
        else:
            target = mid
        edge_point[(u, v)] = mid + smooth * (target - mid)

    pos, nrm, uv, col, slots, out = [], [], [], [], [], []

    def add(p, nn, t, c, s):
        pos.append(p); nrm.append(nn); uv.append(t); col.append(c); slots.append(s)
        return len(pos) - 1

    for a, b, c in tris:
        corners = (a, b, c)
        old = [add(moved[wid[i]], mesh.normals[i], mesh.texcoords[i], mesh.colors[i], mesh.slots[i]) for i in corners]
        mids = []
        for i, j in ((a, b), (b, c), (c, a)):
            key = (min(wid[i], wid[j]), max(wid[i], wid[j]))
            nn = mesh.normals[i] + mesh.normals[j]
            ln = float(np.linalg.norm(nn))
            mids.append(add(edge_point[key], nn / ln if ln > 1e-9 else nn,
                            0.5 * (mesh.texcoords[i] + mesh.texcoords[j]),
                            0.5 * (mesh.colors[i] + mesh.colors[j]),
                            mesh.slots[i]))
        A, B, C = old
        ab, bc, ca = mids
        out += [(A, ab, ca), (ab, B, bc), (ca, bc, C), (ab, bc, ca)]
    as_np = lambda x, w: np.array(x, dtype=np.float64).reshape(-1, w)
    return Mesh(as_np(pos, 3), as_np(nrm, 3), as_np(uv, 2), as_np(col, 3),
                np.array(slots, dtype=np.int32), np.array(out, dtype=np.int32).reshape(-1, 3))


def smooth_normals(mesh: Mesh) -> np.ndarray:
    """One normal per welded position (angle-weighted face normals), so curved patches built from
    them meet without cracks."""
    wid = weld(mesh)
    acc = np.zeros_like(mesh.positions)
    P = mesh.positions
    for a, b, c in mesh.triangles:
        pa, pb, pc = P[a], P[b], P[c]
        n = np.cross(pb - pa, pc - pa)
        ln = np.linalg.norm(n)
        if ln < 1e-12:
            continue
        n = n / ln
        for i, (u, v) in zip((a, b, c), ((pb - pa, pc - pa), (pc - pb, pa - pb), (pa - pc, pb - pc))):
            cu, cv = np.linalg.norm(u), np.linalg.norm(v)
            if cu < 1e-12 or cv < 1e-12:
                continue
            angle = np.arccos(np.clip(np.dot(u, v) / (cu * cv), -1, 1))
            acc[wid[i]] += angle * n
    out = acc[wid]
    ln = np.linalg.norm(out, axis=1, keepdims=True)
    return np.where(ln > 1e-12, out / np.maximum(ln, 1e-12), mesh.normals)


def pn_triangles(mesh: Mesh, level: int = 3, strength: float = 1.0) -> Mesh:
    """Curved PN triangles (Vlachos et al., the ATI TruForm scheme): each triangle becomes a cubic
    patch through its three corners, shaped by smooth vertex normals, tessellated into level^2
    triangles. Corners stay where they were, so outlines and points (a cap's tip, ears) keep
    their place while the faces between them round out. `strength` scales the bulge (0 = flat).
    Texture coordinates, colours and the lit normals interpolate linearly; each new vertex takes
    the matrix-stack slot of its nearest corner."""
    N = smooth_normals(mesh)
    P = mesh.positions
    pos, nrm, uv, col, slots, out = [], [], [], [], [], []
    for a, b, c in mesh.triangles:
        p1, p2, p3 = P[a], P[b], P[c]
        n1, n2, n3 = N[a], N[b], N[c]
        w = lambda i, j, ni: np.dot(P_[j] - P_[i], ni)
        P_ = {0: p1, 1: p2, 2: p3}
        b300, b030, b003 = p1, p2, p3
        b210 = (2 * p1 + p2 - w(0, 1, n1) * n1) / 3
        b120 = (2 * p2 + p1 - w(1, 0, n2) * n2) / 3
        b021 = (2 * p2 + p3 - w(1, 2, n2) * n2) / 3
        b012 = (2 * p3 + p2 - w(2, 1, n3) * n3) / 3
        b102 = (2 * p3 + p1 - w(2, 0, n3) * n3) / 3
        b201 = (2 * p1 + p3 - w(0, 2, n1) * n1) / 3
        e = (b210 + b120 + b021 + b012 + b102 + b201) / 6
        v = (p1 + p2 + p3) / 3
        b111 = e + (e - v) / 2
        grid = {}
        for i in range(level + 1):
            for j in range(level + 1 - i):
                k = level - i - j
                u_, v_, w_ = i / level, j / level, k / level     # weights of corners a, b, c
                curved = (b300 * u_ ** 3 + b030 * v_ ** 3 + b003 * w_ ** 3
                          + 3 * (b210 * u_ * u_ * v_ + b120 * u_ * v_ * v_ + b201 * u_ * u_ * w_
                                 + b021 * v_ * v_ * w_ + b102 * u_ * w_ * w_ + b012 * v_ * w_ * w_)
                          + 6 * b111 * u_ * v_ * w_)
                flat = u_ * p1 + v_ * p2 + w_ * p3
                corner = (a, b, c)[int(np.argmax((u_, v_, w_)))]
                nn = u_ * mesh.normals[a] + v_ * mesh.normals[b] + w_ * mesh.normals[c]
                ln = float(np.linalg.norm(nn))
                pos.append(flat + strength * (curved - flat))
                nrm.append(nn / ln if ln > 1e-9 else nn)
                uv.append(u_ * mesh.texcoords[a] + v_ * mesh.texcoords[b] + w_ * mesh.texcoords[c])
                col.append(u_ * mesh.colors[a] + v_ * mesh.colors[b] + w_ * mesh.colors[c])
                slots.append(mesh.slots[corner])
                grid[(i, j)] = len(pos) - 1
        for i in range(level):
            for j in range(level - i):
                out.append((grid[(i + 1, j)], grid[(i, j + 1)], grid[(i, j)]))
                if i + j < level - 1:
                    out.append((grid[(i + 1, j)], grid[(i + 1, j + 1)], grid[(i, j + 1)]))
    as_np = lambda x, w_: np.array(x, dtype=np.float64).reshape(-1, w_)
    return Mesh(as_np(pos, 3), as_np(nrm, 3), as_np(uv, 2), as_np(col, 3),
                np.array(slots, dtype=np.int32), np.array(out, dtype=np.int32).reshape(-1, 3))


def write_originals(folder, originals: dict) -> None:
    """models/originals.txt: each replaced display list's key and first four words. The emulator
    recognises lists the game writes with the CPU (no DMA to hash) by these as they arrive."""
    import os
    path = os.path.join(folder, "originals.txt")
    known = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            parts = line.split()
            if parts:
                known[parts[0]] = line.rstrip("\n")
    for key, dl in originals.items():
        words = struct.unpack_from(f"<{min(4, len(dl) // 4)}I", dl)
        known[key] = key + " " + " ".join(f"{w:08X}" for w in words)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(known[k] for k in sorted(known)) + "\n")
