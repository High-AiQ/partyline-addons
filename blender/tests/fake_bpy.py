"""A stand-in for `bpy` (and `mathutils`): just enough surface for the add-on's tests, no Blender needed."""
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


def make(directory=None):
    """A fresh fake `bpy`; `directory` holds the add-on's user config and any .blend files."""
    directory = Path(directory or tempfile.mkdtemp())
    log = []
    scene = types.SimpleNamespace(
        name="Scene", frame_current=1, frame_start=1, frame_end=250, camera=Obj("Camera", "CAMERA"),
        unit_settings=types.SimpleNamespace(system="METRIC"),
        objects=Objects([Obj("Cube"), Obj("Lamp", "LIGHT")]))
    bpy = types.ModuleType("bpy")
    bpy.log = log
    bpy.app = types.SimpleNamespace(version_string="5.2.2", background=False, timers=Timers())
    bpy.data = types.SimpleNamespace(filepath="", collections=[types.SimpleNamespace(name="Collection")])
    bpy.context = types.SimpleNamespace(scene=scene, window_manager=None)
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

    bpy.ops = types.SimpleNamespace(ed=types.SimpleNamespace(undo_push=undo_push),
                                    wm=types.SimpleNamespace(save_as_mainfile=save_as_mainfile))
    return bpy


def install(directory=None):
    bpy = make(directory)
    sys.modules["bpy"] = bpy
    sys.modules["mathutils"] = types.SimpleNamespace(Vector=tuple)
    return bpy
