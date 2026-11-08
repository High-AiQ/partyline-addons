"""Capture the 3D viewport the person is looking at, off screen, into an artifact.

Uses `gpu.types.GPUOffScreen` and `draw_view3d` with the viewport's own matrices, so nothing is drawn over the
person's window and nothing outside Blender is captured (for the whole screen use `screen_frame`). It needs the
GPU, which is the main open risk of this op; a failure is reported as an error, never as an empty image.
"""
from .render import Refused, clamp, deliver, error

MIN_EDGE, MAX_EDGE, DEFAULT_EDGE = 64, 1600, 1024  # the pixel copy below is a Python list: keep it small


def viewport(bpy):
    """(window, space, region) of the largest open 3D viewport."""
    found = []
    manager = bpy.context.window_manager
    for window in manager.windows if manager else ():
        for area in window.screen.areas:
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if area.type == "VIEW_3D" and region is not None:
                found.append((region.width * region.height, window, area.spaces.active, region))
    if not found:
        raise Refused("NoViewport", "no 3D viewport is open in Blender")
    _, window, space, region = max(found, key=lambda item: item[0])
    return window, space, region


def size_for(region, edge):
    scale = min(1.0, edge / max(region.width, region.height))
    return max(1, round(region.width * scale)), max(1, round(region.height * scale))


def draw(gpu, window, space, region, width, height):
    """RGBA as floats 0..1, bottom row first, exactly as the viewport shows them (the window's own scene)."""
    offscreen = gpu.types.GPUOffScreen(width, height)
    try:
        view3d = space.region_3d
        offscreen.draw_view3d(window.scene, window.view_layer, space, region, view3d.view_matrix,
                              view3d.window_matrix, do_color_management=True)
        with offscreen.bind():
            buffer = gpu.state.active_framebuffer_get().read_color(0, 0, width, height, 4, 0, "UBYTE")
        buffer.dimensions = width * height * 4
        return [value / 255 for value in buffer]
    finally:
        offscreen.free()


def screenshot(bpy, relay, call):
    adjusted = []
    try:
        import gpu  # only exists inside Blender
        edge = call.args.get("max_size", DEFAULT_EDGE)
        edge = clamp("max_size", edge, MIN_EDGE, MAX_EDGE, adjusted)
        window, space, region = viewport(bpy)
        width, height = size_for(region, edge)
    except (Refused, ImportError) as exc:
        return error(getattr(exc, "kind", "NoGpu"), exc)
    artifact, path = relay.artifacts.new(call.session.id)
    started = relay.clock()
    try:
        pixels = draw(gpu, window, space, region, width, height)
        image = bpy.data.images.new("partyline-screenshot", width, height, alpha=False)
        try:
            image.pixels = pixels
            image.file_format, image.filepath_raw = "PNG", str(path)
            image.save()
        finally:
            bpy.data.images.remove(image)
    except Exception as exc:  # noqa: BLE001 - no GPU context, a headless display or a driver error
        relay.artifacts.discard(artifact, path)
        return error(type(exc).__name__, f"screenshot failed: {exc}")
    settings = {"width": width, "height": height, "viewport": [region.width, region.height]}
    return deliver(relay, call, artifact, path, settings, adjusted, started)
