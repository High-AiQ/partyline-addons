"""Reference overlays for the relay or a background Blender script (bpy images + numpy only)."""
import base64
import contextlib
import math
import tempfile
from pathlib import Path

from .render import MAX_SIDE, MIN_SIDE, Refused, applied, frame_size, pick_camera

ASPECT_TOLERANCE = 0.005  # relative error, half a percent
MAX_REFERENCE_BYTES = 700 * 1024  # fits installed helpers with a 1,000,000-byte receive limit
PALETTE = ((0.9, 0.15, 0.1, 1), (0.1, 0.6, 0.95, 1), (0.2, 0.85, 0.25, 1),
           (0.95, 0.65, 0.1, 1), (0.7, 0.2, 0.9, 1))


def opacity(value):
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) \
            or not 0 <= value <= 1:
        raise Refused("BadArgument", "overlay opacity must be a finite number in 0..1")
    return value


def composite(reference, foreground, overlay_opacity=0.5):
    """Straight RGBA source-over; transparent render background leaves the reference untouched."""
    import numpy as np
    strength = opacity(overlay_opacity)
    back, front = np.asarray(reference, dtype=np.float32), np.asarray(foreground, dtype=np.float32)
    if back.shape != front.shape or back.ndim != 3 or back.shape[-1] != 4:
        raise ValueError("reference and foreground must have identical H x W x 4 shapes")
    alpha = front[..., 3:4] * strength
    out_alpha = alpha + back[..., 3:4] * (1 - alpha)
    premultiplied = front[..., :3] * alpha + back[..., :3] * back[..., 3:4] * (1 - alpha)
    rgb = np.divide(premultiplied, out_alpha, out=np.zeros_like(premultiplied), where=out_alpha > 0)
    return np.concatenate((rgb, out_alpha), axis=-1)


def proxy_pixels(pixels, alpha=0.35):
    """Fade flat proxy faces, retaining dark, opaque silhouette and inter-object edges."""
    import numpy as np
    out = pixels.copy()
    visible = pixels[..., 3] > 0
    edges = np.zeros(visible.shape, dtype=bool)
    for axis in (0, 1):
        delta = np.any(np.abs(np.diff(pixels, axis=axis)) > 0.15, axis=-1)
        if axis == 0:
            edges[1:, :] |= delta
            edges[:-1, :] |= delta
        else:
            edges[:, 1:] |= delta
            edges[:, :-1] |= delta
    edges &= visible
    out[..., 3] *= opacity(alpha)
    out[edges, :3] = 0.02
    out[edges, 3] = pixels[edges, 3]
    return out


def dimensions(scene, image, width=None, height=None, *, bounded=False):
    w, h = map(int, image.size)
    if w <= 0 or h <= 0:
        raise Refused("BadReference", "reference has no pixels")
    if bounded and not (MIN_SIDE <= w <= MAX_SIDE and MIN_SIDE <= h <= MAX_SIDE):
        raise Refused("BadReference", f"reference dimensions must be {MIN_SIDE}..{MAX_SIDE}; resize it first")
    args = {key: value for key, value in (("width", width), ("height", height)) if value is not None}
    rw, rh = frame_size(scene, args)
    if rw <= 0 or rh <= 0 or abs((rw / rh) / (w / h) - 1) > ASPECT_TOLERANCE:
        raise Refused("AspectMismatch", "requested frame and reference aspects differ by more than 0.5%")
    return w, h


@contextlib.contextmanager
def proxy_style(scene, proxies):
    """Render only the given proxies in flat object colours; restore every override, even on failure."""
    if proxies is None:
        yield
        return
    proxies = list(proxies)
    shading = scene.display.shading
    changes = [(scene.render, "engine", "BLENDER_WORKBENCH"), (shading, "light", "FLAT"),
               (shading, "color_type", "OBJECT"), (shading, "show_shadows", False),
               (shading, "show_cavity", False), (shading, "show_object_outline", True),
               (shading, "object_outline_color", (0.02, 0.02, 0.02))]
    for obj in scene.objects:
        if obj.type in {"CAMERA", "LIGHT"}:
            continue
        changes.append((obj, "hide_render", obj not in proxies))
    for index, obj in enumerate(proxies):
        changes.append((obj, "color", PALETTE[index % len(PALETTE)]))
    with applied(changes):
        yield


@contextlib.contextmanager
def loaded(bpy, path):
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        yield image
    finally:
        bpy.data.images.remove(image)


def pixels(image):
    import numpy as np
    values = np.empty(int(image.size[0]) * int(image.size[1]) * 4, dtype=np.float32)
    image.pixels.foreach_get(values)
    return values.reshape((int(image.size[1]), int(image.size[0]), 4))


def render_overlay(bpy, reference, *, scene=None, camera=None, output=None, overlay_opacity=0.5,
                   proxies=None, proxy_opacity=0.35, width=None, height=None, bounded=False):
    """Render over a reference. Return a new bpy image (caller removes it), or write and return a PNG path.

    `proxies` optionally selects objects for a flat, outlined blockout; other geometry is hidden temporarily.
    No scene is saved. With `bounded=True` the relay's resolution ceiling is enforced rather than resized.
    """
    scene = scene or bpy.context.scene
    camera = pick_camera(scene, camera) if camera is None or isinstance(camera, str) else camera
    opacity(overlay_opacity)
    opacity(proxy_opacity)
    with loaded(bpy, reference) as back:
        w, h = dimensions(scene, back, width, height, bounded=bounded)
        out = scene.render
        changes = [(scene, "camera", camera), (out, "resolution_x", w), (out, "resolution_y", h),
                   (out, "resolution_percentage", 100), (out, "pixel_aspect_x", 1.0),
                   (out, "pixel_aspect_y", 1.0), (out, "use_border", False),
                   (out, "film_transparent", True), (out.image_settings, "file_format", "PNG"),
                   (out.image_settings, "color_mode", "RGBA")]
        with tempfile.TemporaryDirectory() as directory:
            rendered = Path(directory) / "foreground.png"
            with applied(changes), proxy_style(scene, proxies):
                bpy.ops.render.render(write_still=False, scene=scene.name)
                bpy.data.images["Render Result"].save_render(str(rendered), scene=scene)
            # Save then load the foreground so both inputs use the same display-referred PNG values.
            with loaded(bpy, rendered) as front:
                foreground = pixels(front)
                if proxies is not None:
                    foreground = proxy_pixels(foreground, proxy_opacity)
                merged = composite(pixels(back), foreground, overlay_opacity)
        result = bpy.data.images.new("Reference Overlay", width=w, height=h, alpha=True)
        keep = False
        try:
            result.pixels.foreach_set(merged.ravel())
            result.update()
            if output is None:
                keep = True
                return result
            result.file_format, result.filepath_raw = "PNG", str(output)
            result.save()
            return Path(output)
        finally:
            if not keep:
                bpy.data.images.remove(result)


def relay_overlay(bpy, args, path):
    """Decode only bounded inline bytes (never an agent-supplied path), then use the reusable renderer."""
    payload = args.get("reference")
    if not isinstance(payload, dict) or not isinstance(payload.get("png_base64"), str):
        raise Refused("BadReference", "reference must be PNG bytes resolved by the Partyline server")
    encoded = payload["png_base64"]
    if len(encoded) > 4 * ((MAX_REFERENCE_BYTES + 2) // 3):
        raise Refused("BadReference", "reference exceeds 700 KiB")
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise Refused("BadReference", "invalid reference encoding") from None
    if len(data) > MAX_REFERENCE_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise Refused("BadReference", "reference must be a PNG of at most 700 KiB")
    with tempfile.TemporaryDirectory() as directory:
        reference = Path(directory) / "reference.png"
        reference.write_bytes(data)
        image = render_overlay(bpy, reference, overlay_opacity=args.get("overlay_opacity", 0.5),
                               width=args.get("width"), height=args.get("height"), bounded=True)
        try:
            image.file_format, image.filepath_raw = "PNG", str(path)
            image.save()
            return tuple(image.size)
        finally:
            bpy.data.images.remove(image)
