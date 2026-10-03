"""Blender side of chara3d.py: toon renders of a (rigged, animated) GLB character as sprite frames.
Run by chara3d.py through Blender in the background:

    blender -b -P chara3d_blender.py -- <job.json>

job.json: {"glb": path, "out": folder, "size": px, "elevation": degrees, "outline": px,
           "frames": [{"name": "walk_000_down", "action": "walk" | null, "frame": n,
                       "yaw": degrees}, ...], "forward": "+X"}
Each frame: the model turned by `yaw` (0 = facing the camera), posed at `frame` of the action
whose name contains `action` (none = the GLB's rest pose), rendered orthographically from
`elevation` degrees above the horizon, toon shaded (two flat bands of the base colour texture
lit by one key light) with Freestyle ink outlines, transparent background, as <out>/<name>.png.
The camera frames the model's bounding box over all frames, so every frame shares one scale.
"""
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

job = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text(encoding="utf-8"))
out = Path(job["out"])
out.mkdir(parents=True, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["glb"])
scene = bpy.context.scene
arm = next((o for o in scene.objects if o.type == "ARMATURE"), None)
meshes = [o for o in scene.objects if o.type == "MESH"]
if arm is not None:
    # Tripo's animated GLBs carry a stray 2x2x2 icosphere besides the character: keep the skinned meshes
    for o in list(meshes):
        if o.parent is not arm and not any(m.type == "ARMATURE" for m in o.modifiers):
            bpy.data.objects.remove(o)
    meshes = [o for o in scene.objects if o.type == "MESH"]

# one root to turn the whole character
root = bpy.data.objects.new("turn", None)
scene.collection.objects.link(root)
for o in scene.objects:
    if o.parent is None and o is not root:
        o.parent = root
# Tripo exports the character facing +X; turn it to face -Y (towards a camera on -Y)
forward = job.get("forward", "+X")
base_yaw = {"+X": -90.0, "-X": 90.0, "+Y": 180.0, "-Y": 0.0}[forward]


# ---------------------------------------------------------------- toon material
def toon(mat):
    nt = mat.node_tree
    tex = next((n for n in nt.nodes if n.type == "TEX_IMAGE"), None)
    keep = tex.name if tex else None          # bpy wraps nodes anew on every access: compare names
    for n in list(nt.nodes):
        if n.name != keep:
            nt.nodes.remove(n)
    tex = nt.nodes.get(keep) if keep else None
    outn = nt.nodes.new("ShaderNodeOutputMaterial")
    diff = nt.nodes.new("ShaderNodeBsdfDiffuse")
    s2r = nt.nodes.new("ShaderNodeShaderToRGB")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.interpolation = "CONSTANT"
    ramp.color_ramp.elements[0].position = 0.0
    ramp.color_ramp.elements[0].color = (0.72, 0.70, 0.80, 1)    # shadow band, slightly cool
    ramp.color_ramp.elements[1].position = job.get("band", 0.35)
    ramp.color_ramp.elements[1].color = (1, 1, 1, 1)
    mul = nt.nodes.new("ShaderNodeMix")
    mul.data_type = "RGBA"
    mul.blend_type = "MULTIPLY"
    mul.inputs["Factor"].default_value = 1.0
    emit = nt.nodes.new("ShaderNodeEmission")
    diff.inputs["Color"].default_value = (1, 1, 1, 1)
    # sockets by kind, not name: names and order change between Blender versions
    a_in, b_in = [i for i in mul.inputs if i.type == "RGBA"][:2]
    res = next(o for o in mul.outputs if o.type == "RGBA")
    nt.links.new(diff.outputs[0], s2r.inputs[0])
    nt.links.new(s2r.outputs[0], ramp.inputs[0])
    if tex:
        nt.links.new(tex.outputs[0], a_in)
    else:
        a_in.default_value = (0.8, 0.8, 0.8, 1)
    nt.links.new(ramp.outputs[0], b_in)
    nt.links.new(res, emit.inputs[0])
    nt.links.new(emit.outputs[0], outn.inputs[0])
    if tex:
        tex.interpolation = "Linear"


for o in meshes:
    for slot in o.material_slots:
        if slot.material and slot.material.node_tree:
            toon(slot.material)

# ---------------------------------------------------------------- light, render settings
sun = bpy.data.lights.new("key", "SUN")
sun.energy = 3.0
sun_o = bpy.data.objects.new("key", sun)
scene.collection.objects.link(sun_o)
sun_o.rotation_euler = (math.radians(50), 0, math.radians(-35))   # from the upper front left

engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
scene.render.film_transparent = True
scene.render.resolution_x = scene.render.resolution_y = job["size"]
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
scene.view_settings.view_transform = "Standard"
scene.view_settings.look = "None"
scene.world = bpy.data.worlds.new("w")
scene.world.color = (0, 0, 0)
if job.get("outline", 0) > 0:
    scene.render.use_freestyle = True
    scene.render.line_thickness_mode = "ABSOLUTE"
    scene.render.line_thickness = 1.0          # a multiplier on the line style's thickness below
    vl = scene.view_layers[0]
    vl.use_freestyle = True
    ls = vl.freestyle_settings.linesets[0] if vl.freestyle_settings.linesets else vl.freestyle_settings.linesets.new("ink")
    ls.select_by_visibility = True
    ls.select_by_edge_types = True
    # which Freestyle edge types to ink (default: the outer outline only). An AI mesh is dense and
    # lumpy: its crease, border and contour edges are everywhere and thick lines cover the body
    lines = set(job.get("lines", ["external_contour"]))
    ls.select_silhouette = "silhouette" in lines
    ls.select_border = "border" in lines
    ls.select_crease = "crease" in lines
    ls.select_contour = "contour" in lines
    ls.select_external_contour = "external_contour" in lines
    if ls.linestyle is None:                   # a factory-empty scene has no line style yet
        ls.linestyle = bpy.data.linestyles.new("ink")
    ls.linestyle.color = (0.10, 0.06, 0.05)
    ls.linestyle.thickness = job["outline"]

# ---------------------------------------------------------------- camera
cam_d = bpy.data.cameras.new("cam")
cam_d.type = "ORTHO"
cam = bpy.data.objects.new("cam", cam_d)
scene.collection.objects.link(cam)
scene.camera = cam
elev = math.radians(job.get("elevation", 25))


def actions_named(part):
    return [a for a in bpy.data.actions if part.lower() in a.name.lower()]


def pose(fr):
    if arm is None:
        return
    if fr.get("action"):
        acts = actions_named(fr["action"])
        if not acts:
            raise SystemExit(f"no action named like {fr['action']}: {[a.name for a in bpy.data.actions]}")
        if arm.animation_data is None:
            arm.animation_data_create()
        arm.animation_data.action = acts[0]
        if "phase" in fr:
            # 0..1 through the clip (its length isn't known to the caller)
            f0, f1 = acts[0].frame_range
            f = f0 + fr["phase"] * (f1 - f0)
            scene.frame_set(int(f), subframe=f - int(f))
        else:
            scene.frame_set(int(fr.get("frame", 0)))
    else:
        if arm.animation_data:
            arm.animation_data.action = None
        for pb in arm.pose.bones:
            pb.matrix_basis.identity()
        scene.frame_set(0)


def world_box():
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    lo = Vector((1e9, 1e9, 1e9)); hi = Vector((-1e9, -1e9, -1e9))
    for o in meshes:
        ev = o.evaluated_get(deps)
        me = ev.to_mesh()
        for v in me.vertices:
            p = ev.matrix_world @ v.co
            lo = Vector(map(min, lo, p)); hi = Vector(map(max, hi, p))
        ev.to_mesh_clear()
    return lo, hi


if "ortho_scale" in job:
    # a framing given by the caller: several jobs (one per animation file) render at one scale
    centre = Vector(job["centre"])
    radius = job["ortho_scale"] / 2
    cam_d.ortho_scale = job["ortho_scale"]
else:
    # one framing for all frames: the box over every pose and turn
    lo = Vector((1e9, 1e9, 1e9)); hi = Vector((-1e9, -1e9, -1e9))
    for fr in job["frames"]:
        pose(fr)
        root.rotation_euler = (0, 0, math.radians(base_yaw + fr.get("yaw", 0)))
        a, b = world_box()
        lo = Vector(map(min, lo, a)); hi = Vector(map(max, hi, b))
    centre = (lo + hi) / 2
    radius = (hi - lo).length / 2
    cam_d.ortho_scale = radius * 2 * job.get("margin", 1.05)
d = radius * 4
cam.location = centre + Vector((0, -math.cos(elev) * d, math.sin(elev) * d))
cam.rotation_euler = (math.radians(90) - elev, 0, 0)
cam_d.clip_end = d * 4
meta = {"ortho_scale": cam_d.ortho_scale, "centre": list(centre), "elevation": job.get("elevation", 25),
        "actions": [a.name for a in bpy.data.actions], "frames": {}}

for fr in job["frames"]:
    pose(fr)
    root.rotation_euler = (0, 0, math.radians(base_yaw + fr.get("yaw", 0)))
    scene.render.filepath = str(out / f"{fr['name']}.png")
    bpy.ops.render.render(write_still=True)
    if arm is not None and fr.get("action"):
        a = actions_named(fr["action"])[0]
        meta["frames"][fr["name"]] = {"range": list(a.frame_range)}
(out / "render.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
print("RENDERED", len(job["frames"]))
