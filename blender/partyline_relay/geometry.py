"""Camera-space framing, clipping between objects and floating above a support, as evidence for a render check.

`blender_scene_info(project_to_camera=true)` returns this. The checks are heuristics with stated assumptions
(see `contact.py`) and report the tolerance and margin they used. Whatever geometry cannot settle is
`unsupported`, never a pass: the render then goes to visual review. The frame margin test looks at every
vertex of the evaluated mesh, so a verdict of `inside` means the whole mesh, not its box.
"""
import json

from . import contact
from .render import MAX_SIDE, MIN_SIDE, Refused, applied, clamp, pick_camera
from .world import World, reason

MARGIN, TOLERANCE = 0.02, 0.005   # a fraction of the frame; scene units
MAX_OBJECTS, MAX_PAIRS, MAX_SUPPORTS = 40, 40, 20
LISTED = 20
CAP = 14000
NAME = 80                         # longest name or reason echoed back from the request
SEVERITY = {"intersecting": 0, "contained": 1, "unsupported": 2, "clear": 3}
ASSUMPTIONS = ["gravity is -Z", "evaluated meshes (modifiers applied)", "closed meshes for penetration depth",
               "a support is one named object"]


def short(text, limit=NAME):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def frame_status(world, shape, margin, clip):
    """Where a shape lies in the camera frame: inside, tight (inside but within `margin` of an edge), cut,
    outside, or unsupported (it crosses the camera plane or may cross the frame without a vertex in it)."""
    seen = [world.view(v) for v in shape.verts]
    xs, ys, zs = ([p[i] for p in seen] for i in range(3))
    box = {"bbox": [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)],
           "depth": [round(min(zs), 3), round(max(zs), 3)]}
    if min(zs) <= 0 < max(zs):
        return {**box, "frame": "unsupported", "reason": "it crosses the camera plane"}
    if max(zs) <= 0:
        return {**box, "frame": "outside", "reason": "behind the camera"}
    visible = [0 <= x <= 1 and 0 <= y <= 1 and clip[0] <= z <= clip[1] for x, y, z in seen]
    if not any(visible):
        crossing = max(xs) >= 0 and min(xs) <= 1 and max(ys) >= 0 and min(ys) <= 1 and max(zs) >= clip[0]
        if crossing and min(zs) <= clip[1]:
            return {**box, "frame": "unsupported", "reason": "no vertex is in view but its box overlaps the frame"}
        return {**box, "frame": "outside"}
    margins = {"left": min(xs), "right": 1 - max(xs), "bottom": min(ys), "top": 1 - max(ys)}
    box["margins"] = {k: round(v, 4) for k, v in margins.items()}
    if not all(visible):
        return {**box, "frame": "cut"}
    return {**box, "frame": "tight" if min(margins.values()) < margin else "inside"}


def view_plan(scene, args):
    """(settings, changes): the camera and resolution the projection uses (the render's aspect, not the scene's)."""
    adjusted, out = [], scene.render
    camera = pick_camera(scene, args.get("camera"))
    percent = out.resolution_percentage       # as `render` does: the default size is the one it would write
    width = clamp("width", args.get("width", out.resolution_x * percent // 100), MIN_SIDE, MAX_SIDE, adjusted)
    height = clamp("height", args.get("height", out.resolution_y * percent // 100), MIN_SIDE, MAX_SIDE, adjusted)
    changes = [(scene, "camera", camera), (out, "resolution_x", width), (out, "resolution_y", height),
               (out, "resolution_percentage", 100)]
    return {"camera": camera.name, "width": width, "height": height, "adjusted": adjusted}, changes


def number(args, name, default):
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise Refused("BadArgument", f"{name} must be a positive number")
    return float(value)


def analyse(world, scene, args, settings):
    margin, tol = number(args, "margin", MARGIN), number(args, "tolerance", TOLERANCE)
    clip = (scene.camera.data.clip_start, scene.camera.data.clip_end)
    shapes, skipped = {}, []
    meshes = [o for o in scene.objects if o.type not in ("CAMERA", "LIGHT", "EMPTY", "LIGHT_PROBE", "SPEAKER")]
    for obj in meshes[:MAX_OBJECTS]:
        why = reason(obj)
        shape = None if why else world.shape(obj)
        if isinstance(shape, str):
            why = shape
        if why:
            skipped.append({"name": obj.name, "verdict": "unsupported", "reason": why})
        else:
            shapes[obj.name] = shape
    if len(meshes) > MAX_OBJECTS:
        skipped.append({"name": f"{len(meshes) - MAX_OBJECTS} more objects", "verdict": "unsupported",
                        "reason": f"only the first {MAX_OBJECTS} objects are analysed"})
    objects = [{"name": name, **frame_status(world, shape, margin, clip)} for name, shape in shapes.items()]
    trees, names, pairs = {}, list(shapes), []

    def tree(shape):
        if shape.name not in trees:
            trees[shape.name] = world.tree(shape)
        return trees[shape.name]

    for i, first in enumerate(names):
        for second in names[i + 1:]:
            a, b = shapes[first], shapes[second]
            if contact.meet(a, b, tol):
                pairs.append({"a": first, "b": second, **contact.pair(a, b, tree, tol)})
    supports = []
    for name, support in list(args.get("supports", {}).items())[:MAX_SUPPORTS]:
        if name not in shapes or support not in shapes:
            missing = name if name not in shapes else support
            supports.append({"name": short(name), "verdict": "unsupported", "support": short(support),
                             "reason": f"{short(missing)!r} is not an analysable mesh object"})
        else:
            supports.append({"name": name, **contact.floating(shapes[name], support, tree(shapes[support]), tol)})
    wanted = len(args.get("supports", {}))
    if wanted > MAX_SUPPORTS:
        supports.append({"name": f"{wanted - MAX_SUPPORTS} more supports", "verdict": "unsupported",
                         "reason": f"only the first {MAX_SUPPORTS} supports are analysed"})
    pairs.sort(key=lambda p: SEVERITY.get(p["verdict"], 2))     # the worst first, so a cut loses the mildest
    report = {**settings, "frame": scene.frame_current, "margin": margin, "tolerance": tol,
              "assumptions": ASSUMPTIONS, "objects": objects, "skipped": skipped, "pairs": pairs,
              "pairs_clear": "pairs whose boxes do not meet are clear and not listed", "supports": supports}
    for key, limit in (("skipped", LISTED), ("pairs", MAX_PAIRS)):
        drop(report, key, limit)
    return report


def drop(report, key, start):
    """Cut `report[key]` from `start` and say what went: `omitted[key]` has the count and, where items carry
    one, how many of each verdict, so a cut list is never mistaken for a complete one."""
    gone = report[key][start:]
    if not gone:
        return
    del report[key][start:]
    kept = report.setdefault("omitted", {}).setdefault(key, {"count": 0, "verdicts": {}})
    kept["count"] += len(gone)
    for item in gone:
        word = item.get("verdict") or item.get("frame")
        if word:
            kept["verdicts"][word] = kept["verdicts"].get(word, 0) + 1
    report["truncated"] = True


def fit(report):
    """Trim the longest list until the report fits. Each pass drops a quarter of it (all of a single item),
    so it always ends; names echoed from the request are already short."""
    keys = ("objects", "pairs", "skipped", "supports")
    while len(json.dumps(report)) > CAP:
        longest = max(keys, key=lambda k: len(report[k]))
        if not report[longest]:
            break
        drop(report, longest, len(report[longest]) * 3 // 4)
    return report


def project(bpy, args):
    """The `projection` part of scene_info, or {"error": ...} when the request cannot be served."""
    scene = bpy.context.scene
    supports = args.get("supports", {})
    if not isinstance(supports, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                 for k, v in supports.items()):
        return {"error": "supports maps an object name to the name of the object it rests on"}
    try:
        settings, changes = view_plan(scene, args)
        with applied(changes):
            return fit(analyse(World(bpy, scene, scene.camera), scene, args, settings))
    except Refused as exc:
        return {"error": short(exc, 400)}
