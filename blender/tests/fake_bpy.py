"""A stand-in for `bpy` (and `mathutils`): just enough surface for the add-on's tests, no Blender needed."""
import contextlib
import sys
import tempfile
import types
from pathlib import Path


class Matrix:
    def __matmul__(self, vector):
        return tuple(vector)


class Obj:
    def __init__(self, name, type="MESH", parent=None):
        self.name, self.type, self.parent = name, type, parent
        self.users_collection = [types.SimpleNamespace(name="Collection")]
        self.location, self.rotation_euler, self.scale = (1.0, 2.0, 3.0), (0, 0, 0), (1, 1, 1)
        self.dimensions, self.modifiers, self.material_slots = (2, 2, 2), [], []
        self.matrix_world = Matrix()
        self.bound_box = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]


class Objects(list):
    def get(self, name, default=None):
        return next((o for o in self if o.name == name), default)


class Header:
    drawn = []

    @classmethod
    def append(cls, fn):
        cls.drawn.append(fn)

    @classmethod
    def remove(cls, fn):
        cls.drawn.remove(fn)


class Timers:
    def __init__(self):
        self.registered = []

    def register(self, fn, first_interval=0, persistent=False):
        self.registered.append(fn)

    def unregister(self, fn):
        self.registered.remove(fn)

    def is_registered(self, fn):
        return fn in self.registered


PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image"


class Image:
    """`Render Result`, or a datablock made by `images.new`: saving writes a PNG stand-in."""

    def __init__(self, name, width=0, height=0):
        self.name, self.size = name, (width, height)
        self.pixels, self.file_format, self.filepath_raw = [], "", ""

    def save_render(self, filepath, scene=None):
        Path(filepath).write_bytes(PNG)

    def save(self):
        Path(self.filepath_raw).write_bytes(PNG)


class Images(dict):
    def new(self, name, width, height, alpha=False):
        self[name] = Image(name, width, height)
        return self[name]

    def remove(self, image):
        del self[image.name]


class Buffer(list):
    dimensions = None


class OffScreen:
    """`gpu.types.GPUOffScreen`; `ok=False` plays a machine whose GPU context cannot be used."""
    ok, made = True, []

    def __init__(self, width, height):
        if not self.ok:
            raise RuntimeError("GPUOffScreen: no GPU context")
        self.size, self.freed, self.drawn = (width, height), False, []
        self.made.append(self)

    def draw_view3d(self, *args, **kwargs):
        self.drawn.append((args, kwargs))

    def bind(self):
        return contextlib.nullcontext()

    def free(self):
        self.freed = True


def install_gpu():
    framebuffer = types.SimpleNamespace(
        read_color=lambda x, y, width, height, channels, slot, kind: Buffer([128] * (width * height * 4)))
    gpu = types.ModuleType("gpu")
    fresh = type("OffScreen", (OffScreen,), {"ok": True, "made": []})  # state a test sets stays with the test
    gpu.types = types.SimpleNamespace(GPUOffScreen=fresh)
    gpu.state = types.SimpleNamespace(active_framebuffer_get=lambda: framebuffer)
    sys.modules["gpu"] = gpu
    return fresh


def viewport(width, height):
    region = types.SimpleNamespace(type="WINDOW", width=width, height=height)
    space = types.SimpleNamespace(region_3d=types.SimpleNamespace(view_matrix="view", window_matrix="proj"))
    return types.SimpleNamespace(type="VIEW_3D", regions=[region], spaces=types.SimpleNamespace(active=space))


def make(directory=None):
    """A fresh fake `bpy`; `directory` holds the add-on's user config and any .blend files."""
    directory = Path(directory or tempfile.mkdtemp())
    log = []
    camera = Obj("Camera", "CAMERA")
    scene = types.SimpleNamespace(
        name="Scene", frame_current=1, frame_start=1, frame_end=250, camera=camera,
        unit_settings=types.SimpleNamespace(system="METRIC"),
        objects=Objects([Obj("Cube"), Obj("Lamp", "LIGHT"), camera, Obj("Side", "CAMERA")]),
        render=types.SimpleNamespace(
            engine="CYCLES", resolution_x=1920, resolution_y=1080, resolution_percentage=50,
            image_settings=types.SimpleNamespace(file_format="OPEN_EXR", color_mode="RGBA")),
        eevee=types.SimpleNamespace(taa_render_samples=16))

    def frame_set(frame):
        scene.frame_current = frame
        log.append(("frame", frame))

    scene.frame_set = frame_set
    bpy = types.ModuleType("bpy")
    bpy.log = log
    bpy.app = types.SimpleNamespace(version_string="5.2.2", background=False, timers=Timers())
    images = Images({"Render Result": Image("Render Result", 0, 0)})
    bpy.data = types.SimpleNamespace(filepath="", collections=[types.SimpleNamespace(name="Collection")],
                                     images=images)
    window = types.SimpleNamespace(
        scene=scene, view_layer="layer", screen=types.SimpleNamespace(areas=[types.SimpleNamespace(
            type="TIMELINE", regions=[], spaces=None), viewport(1600, 900), viewport(400, 300)]))
    manager = types.SimpleNamespace(windows=[window])
    bpy.context = types.SimpleNamespace(scene=scene, window_manager=manager)
    bpy.types = types.SimpleNamespace(Operator=type("Operator", (), {}), STATUSBAR_HT_header=Header)
    bpy.utils = types.SimpleNamespace(
        extension_path_user=lambda package, path="", create=False: str(directory / "user"),
        user_resource=lambda kind: str(directory / "config"),
        register_class=lambda cls: log.append(("register", cls.__name__)),
        unregister_class=lambda cls: log.append(("unregister", cls.__name__)))

    def undo_push(message=""):
        log.append(("undo", message))

    def save_as_mainfile(filepath="", copy=False):
        log.append(("save", filepath, copy))
        Path(filepath).write_text("blend")

    def render(write_still=False):
        render.settings = (scene.camera.name, scene.render.engine, scene.render.resolution_x,
                           scene.render.resolution_y, scene.render.resolution_percentage,
                           scene.render.image_settings.file_format, scene.eevee.taa_render_samples,
                           scene.frame_current)
        log.append(("render", render.settings))
        render.during()
        images["Render Result"].size = (scene.render.resolution_x, scene.render.resolution_y)

    render.during = lambda: None  # a test hooks in here to play something happening mid-render
    bpy.ops = types.SimpleNamespace(ed=types.SimpleNamespace(undo_push=undo_push),
                                    wm=types.SimpleNamespace(save_as_mainfile=save_as_mainfile),
                                    render=types.SimpleNamespace(render=render))
    return bpy


def install(directory=None):
    bpy = make(directory)
    sys.modules["bpy"] = bpy
    sys.modules["mathutils"] = types.SimpleNamespace(Vector=tuple)
    install_gpu()
    return bpy
