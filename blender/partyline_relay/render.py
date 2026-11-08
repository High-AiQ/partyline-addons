"""Render the open scene into an artifact: settings are clamped (the clamps are reported), applied for the
render only and always put back.

The render is one long C call on Blender's main thread and cannot be interrupted. Cancel, the deadline and
the end of the session are therefore honoured when it returns: the image is then deleted, not handed over.
Only the timer drain calls these functions, so `bpy` is touched on the main thread alone.
"""
import contextlib

MIN_SIDE, MAX_SIDE = 16, 2048
MIN_SAMPLES, MAX_SAMPLES = 1, 64
ENGINES = {"workbench": "BLENDER_WORKBENCH", "eevee": "BLENDER_EEVEE"}
LISTED = 20
MIME = "image/png"


class Refused(Exception):
    """A request the add-on will not run; the message reaches the agent as written."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def error(kind, message):
    return {"ok": False, "error": {"type": kind, "message": str(message)[:2000], "traceback": ""}}


def clamp(name, value, low, high, adjusted):
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise Refused("BadArgument", f"{name} must be a number")
    used = int(max(low, min(high, round(value))))
    if used != value:
        adjusted.append(f"{name} {value} -> {used}")
    return used


@contextlib.contextmanager
def applied(changes):
    """Set (object, attribute, value) triples for the duration; the old values always come back."""
    old = []
    try:
        for obj, attr, value in changes:
            previous = getattr(obj, attr)
            # bpy_prop_array is a live view: snapshot it before assigning a new colour.
            if type(previous).__name__ == "bpy_prop_array" or isinstance(previous, list | tuple):
                previous = previous[:]
            old.append((obj, attr, previous))
            setattr(obj, attr, value)
        yield
    finally:
        for obj, attr, value in reversed(old):
            with contextlib.suppress(Exception):
                setattr(obj, attr, value)


def pick_camera(scene, name):
    cameras = [o for o in scene.objects if o.type == "CAMERA"]
    if name is None:
        if scene.camera is None:
            raise Refused("NoCamera", "the scene has no active camera; pass camera=<name> "
                          f"(cameras: {', '.join(c.name for c in cameras[:LISTED]) or 'none'})")
        return scene.camera
    found = next((c for c in cameras if c.name == name), None)
    if found is None:
        raise Refused("NoCamera", f"no camera named {name!r} (cameras: "
                      f"{', '.join(c.name for c in cameras[:LISTED]) or 'none'})")
    return found


def frame_size(scene, args):
    """The intended frame before per-axis render clamps; explicit sizes describe square pixels."""
    out = scene.render
    width = args.get("width", out.resolution_x * out.resolution_percentage // 100)
    height = args.get("height", out.resolution_y * out.resolution_percentage // 100)
    if "width" not in args and "height" not in args:
        width *= getattr(out, "pixel_aspect_x", 1)
        height *= getattr(out, "pixel_aspect_y", 1)
    return width, height


def plan(scene, args):
    """(settings, changes to apply, frame, adjustments made) for a render request, or Refused."""
    adjusted, out = [], scene.render
    engine = args.get("engine", "workbench")
    if engine not in ENGINES:
        raise Refused("BadEngine", "engine is workbench or eevee")
    camera = pick_camera(scene, args.get("camera"))
    width = clamp("width", args.get("width", out.resolution_x * out.resolution_percentage // 100),
                  MIN_SIDE, MAX_SIDE, adjusted)
    height = clamp("height", args.get("height", out.resolution_y * out.resolution_percentage // 100),
                   MIN_SIDE, MAX_SIDE, adjusted)
    frame = args.get("frame")
    if frame is not None:
        frame = clamp("frame", frame, scene.frame_start, scene.frame_end, adjusted)
    changes = [(scene, "camera", camera), (out, "engine", ENGINES[engine]), (out, "resolution_x", width),
               (out, "resolution_y", height), (out, "resolution_percentage", 100),
               (out.image_settings, "file_format", "PNG"), (out.image_settings, "color_mode", "RGB")]
    samples = None
    if engine == "eevee":  # the scene's own count is clamped too: a file may ask for thousands
        samples = clamp("samples", args.get("samples", scene.eevee.taa_render_samples),
                        MIN_SAMPLES, MAX_SAMPLES, adjusted)
        changes.append((scene.eevee, "taa_render_samples", samples))
    elif "samples" in args:
        adjusted.append("samples ignored: workbench uses the scene's anti-aliasing")
    settings = {"camera": camera.name, "engine": engine, "width": width, "height": height,
                "samples": samples, "frame": scene.frame_current if frame is None else frame}
    return settings, changes, frame, adjusted


def deliver(relay, call, artifact, path, settings, adjusted, started):
    """The call's result for a finished image, or a refusal (and no file) if the call was cancelled meanwhile."""
    elapsed = round(relay.clock() - started, 2)
    if (call.cancel.is_set() or not relay.alive(call.session) or relay.clock() > call.deadline
            or not relay.artifacts.has(artifact)):
        relay.artifacts.discard(artifact, path)
        result = error("Cancelled", "the call was cancelled or past its deadline before the image was ready; "
                       "the image was discarded")
        return {**result, "cancelled": True, "elapsed": elapsed}
    try:
        size = path.stat().st_size
    except OSError:
        relay.artifacts.discard(artifact, path)
        return error("RenderFailed", "Blender wrote no image")
    return {"ok": True, "artifact": {"id": artifact, "mime": MIME, "bytes": size}, "settings": settings,
            "adjusted": adjusted, "elapsed": elapsed}


def render(bpy, relay, call):
    scene = bpy.context.scene
    try:
        settings, changes, frame, adjusted = plan(scene, call.args)
    except Refused as exc:
        return error(exc.kind, exc)
    overlay_args = call.args
    if "reference" in call.args:
        width, height = frame_size(scene, call.args)
        overlay_args = {**call.args, "width": width, "height": height}
    artifact, path = relay.artifacts.new(call.session.id)
    started, before = relay.clock(), scene.frame_current
    try:
        with applied(changes):
            if frame is not None and frame != before:
                scene.frame_set(frame)
            try:
                if "reference" in call.args:
                    from .overlay import relay_overlay
                    overlay_size = relay_overlay(bpy, overlay_args, path)
                else:
                    bpy.ops.render.render(write_still=False)
            finally:
                if scene.frame_current != before:
                    scene.frame_set(before)
            if "reference" not in call.args:
                bpy.data.images["Render Result"].save_render(str(path), scene=scene)
            else:
                adjusted = [a for a in adjusted if not a.startswith(("width ", "height "))]
                for axis, used in zip(("width", "height"), overlay_size):
                    wanted = overlay_args[axis]
                    if wanted != used:
                        adjusted.append(f"{axis} {wanted} -> {used} (reference resolution)")
                    settings[axis] = used
                settings["overlay_opacity"] = call.args.get("overlay_opacity", 0.5)
    except Exception as exc:  # noqa: BLE001 - Blender reports a failed render (no GPU, no camera) as RuntimeError
        relay.artifacts.discard(artifact, path)
        return error(exc.kind if isinstance(exc, Refused) else type(exc).__name__, f"render failed: {exc}")
    return deliver(relay, call, artifact, path, settings, adjusted, started)
