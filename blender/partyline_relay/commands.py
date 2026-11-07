"""Main-thread commands: exec with a deadline and caps, scene info, and the session-start backup.

Only the timer drain calls `execute`, so `bpy` is touched on Blender's main thread alone.
The deadline, cancel and lease checks run from `sys.settrace`, i.e. between Python lines: a single long
C call (a render, `sum(range(10**9))`) cannot be interrupted, which is why the relay reports it as busy.
"""
import ast
import contextlib
import hashlib
import json
import sys
import traceback
from pathlib import Path

MAX_CODE = 65536
TAIL = 4096
MARK = "\n...[truncated]...\n"
HEAD = 16384 - TAIL - len(MARK)  # head + marker + tail never exceed 16384 characters
TRACEBACK = 4096
RESULT = 4096
SCENE_CAP = 16384


PLAIN = (str, int, float, bool, type(None))
_ARGS = BaseException.__dict__["args"]
_TRACEBACK = BaseException.__dict__["__traceback__"]
_NAME = type.__dict__["__name__"]


def tname(cls):
    """A class's name as the interpreter stores it, not as a metaclass property might say it."""
    return _NAME.__get__(cls)


class Stop(BaseException):
    """Raised by the tracer; a BaseException so a script's `except Exception` cannot swallow it."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class Capture:
    """A stdout sink that keeps the head and the tail of whatever is printed, however much it is."""

    def __init__(self):
        self.head, self.tail, self.total = "", "", 0

    def write(self, text):
        size = len(text)
        self.total += size
        room = HEAD - len(self.head)
        if room > 0:
            self.head, text = self.head + text[:room], text[room:]
        if text:
            self.tail = (self.tail + text)[-TAIL:]
        return size

    def flush(self):
        pass

    @property
    def truncated(self):
        return self.total > HEAD + TAIL

    def text(self):
        return self.head + (MARK if self.truncated else "") + self.tail


def failure(kind, message, trace=""):
    return {"type": kind, "message": str(message)[:2000], "traceback": trace[-TRACEBACK:]}


def stopped(error):
    return {"ok": False, "stdout": "", "truncated": False, "result": None, "error": error,
            "undo": False, "elapsed": 0.0, "cancelled": True}


def make_tracer(relay, call):
    session = call.session

    def trace(frame, event, arg):
        now = relay.clock()
        if call.cancel.is_set():
            raise Stop("Cancelled", "cancelled")
        if relay.session is not session or now - session.renewed > relay.lease:
            raise Stop("SessionEnded", f"the session ended ({session.ended or 'lease'})")
        if now > call.deadline:
            raise Stop("Deadline", "the call's deadline passed")
        return trace

    return trace


def exact(text, limit=500):
    """An exact `str` copy of a str (or str subclass): its own __format__, __str__ or slicing never runs."""
    return str.__getitem__(text, slice(0, limit))


def is_plain(value):
    kind = type(value)
    return any(kind is exact_type for exact_type in PLAIN)  # identity: a metaclass __eq__ is never asked


def plain(value):
    """Text for an argument, only for exact builtin types; anything else shows as its type's name."""
    kind = type(value)
    if is_plain(value):
        return exact(str(value))
    if (kind is tuple or kind is list) and len(value) <= 20 and all(is_plain(v) for v in value):
        return exact(repr(value))
    return "<" + exact(tname(kind)) + ">"


def describe_error(exc, tracer, previous):
    """(error, cancelled) for a script's exception, built without calling anything the script defined.

    `str(exc)`, the traceback module, attribute reads, `==` on its types and formatting a str subclass all
    dispatch to Python code the script may have defined, and the traceback module even swallows what a tracer
    raises. So the text comes from plain C-level fields, normalised to exact `str`s, with identity checks.
    As a net under that, the work runs under the deadline tracer and contains any BaseException: a hook that
    was missed can neither loop forever nor raise out of here. The tracer is detached again before this
    function builds a result of its own.
    """
    sys.settrace(tracer)
    try:
        name = exact(tname(type(exc)))
        message = ", ".join([plain(a) for a in _ARGS.__get__(exc)[:10]])
        frames, tb = [], _TRACEBACK.__get__(exc)
        tb = tb.tb_next if tb is not None else None  # hide this module's frame
        while tb is not None:
            code = tb.tb_frame.f_code
            frames.append('  File "' + exact(code.co_filename) + '", line ' + str(tb.tb_lineno) + ", in "
                          + exact(code.co_name))
            tb = tb.tb_next
        trace = ("Traceback (most recent call last):\n" + "\n".join(frames[-60:]) + "\n" + name + ": "
                 + message + "\n")
    except Stop as stop:
        sys.settrace(previous)
        return failure(stop.kind, stop), True
    except BaseException:  # noqa: BLE001 - a hook we did not foresee: say only that
        sys.settrace(previous)
        return failure("Error", "the error could not be described"), False
    sys.settrace(previous)
    return failure(name, message, trace), False


def push_undo(bpy, code):
    try:
        bpy.ops.ed.undo_push(message="agent " + hashlib.sha256(code.encode()).hexdigest()[:8])
        return True
    except Exception:  # noqa: BLE001 - no undo context (e.g. no active window): run anyway
        return False


def run_exec(bpy, relay, call):
    code = call.args["code"]
    session = call.session
    if session.namespace is None:
        session.namespace = {"__name__": "__agent__", "bpy": bpy}
    namespace, out = session.namespace, Capture()
    started, undo = relay.clock(), push_undo(bpy, code)
    value, shown, error, cancelled = None, None, None, False
    previous = sys.gettrace()
    tracer = make_tracer(relay, call)
    sys.settrace(tracer)
    try:
        with contextlib.redirect_stdout(out):
            tree = ast.parse(code, "<agent>", "exec")
            last = ast.Expression(tree.body.pop().value) if tree.body and isinstance(
                tree.body[-1], ast.Expr) else None
            exec(compile(tree, "<agent>", "exec"), namespace)  # noqa: S102 - the point of the add-on
            if last is not None:
                value = eval(compile(last, "<agent>", "eval"), namespace)  # noqa: S307
            if value is not None:  # a script-defined __repr__ is script code: it runs under the tracer too
                try:
                    shown = repr(value)[:RESULT]
                except Exception as exc:  # noqa: BLE001 - an ordinary failure to format is not a failed call
                    shown = f"<repr failed: {tname(type(exc))}>"
    except Stop as stop:
        sys.settrace(previous)
        error, cancelled = failure(stop.kind, stop), True
    except BaseException as exc:  # noqa: BLE001 - SystemExit and friends must not leave the script
        sys.settrace(previous)
        error, cancelled = describe_error(exc, tracer, previous)
    finally:
        sys.settrace(previous)
    return {"ok": error is None, "stdout": out.text(), "truncated": out.truncated, "result": shown,
            "error": error, "undo": undo, "elapsed": round(relay.clock() - started, 3),
            "cancelled": cancelled}


def backup(bpy):
    """Save a copy beside the open file before the agent touches it; an unsaved scene has none."""
    path = bpy.data.filepath
    if not path:
        return {"ok": True, "backup": None, "unsaved": True}
    source = Path(path)
    target = source.with_name(source.stem + ".agent-backup.blend")
    try:
        bpy.ops.wm.save_as_mainfile(filepath=str(target), copy=True)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal to the session
        return {"ok": True, "backup": None, "unsaved": False, "backup_error": str(exc)[:300]}
    return {"ok": True, "backup": str(target), "unsaved": False}


def round3(values):
    return [round(float(v), 3) for v in values]


def bounds(obj):
    try:
        from mathutils import Vector
        corners = [tuple(obj.matrix_world @ Vector(c)) for c in obj.bound_box]
    except Exception:  # noqa: BLE001 - empties and cameras have no box
        return None
    return {"min": round3(map(min, zip(*corners, strict=True))),
            "max": round3(map(max, zip(*corners, strict=True)))}


def describe(obj, deep=False):
    info = {"name": obj.name, "type": obj.type, "parent": obj.parent.name if obj.parent else None,
            "collection": obj.users_collection[0].name if obj.users_collection else None,
            "location": round3(obj.location), "bbox": bounds(obj)}
    if deep:
        info.update(rotation=round3(obj.rotation_euler), scale=round3(obj.scale),
                    dimensions=round3(obj.dimensions), modifiers=[m.name for m in obj.modifiers],
                    materials=[s.material.name for s in obj.material_slots if s.material])
    return info


def scene_info(bpy, args):
    detail, scene = args.get("detail", "summary"), bpy.context.scene
    info = {"blender": bpy.app.version_string, "file": Path(bpy.data.filepath).name, "scene": scene.name,
            "frame": scene.frame_current, "frame_range": [scene.frame_start, scene.frame_end],
            "units": scene.unit_settings.system, "camera": scene.camera.name if scene.camera else None,
            "object_count": len(scene.objects), "truncated": False}
    if detail == "summary":
        info["collections"] = [c.name for c in bpy.data.collections][:50]
    elif detail == "object":
        obj = scene.objects.get(args.get("name", ""))
        if obj is None:
            return {"ok": False, "error": failure("NotFound", f"no object named {args.get('name')!r}")}
        info["object"] = describe(obj, deep=True)
    elif detail == "objects":
        info["objects"] = [describe(o) for o in scene.objects]
        while len(json.dumps(info)) > SCENE_CAP and info["objects"]:
            del info["objects"][max(1, len(info["objects"]) * 9 // 10):]
            info["truncated"] = True
    else:
        return {"ok": False, "error": failure("BadDetail", "detail is summary, objects or object")}
    return {"ok": True, "scene": info}


def execute(bpy, relay, call):
    """Run one queued call; never raises."""
    try:
        if call.cancel.is_set() or not relay.alive(call.session):
            return stopped(failure("Cancelled", "not run: cancelled or the session ended"))
        if call.op == "exec":
            return run_exec(bpy, relay, call)
        if call.op == "scene_info":
            return scene_info(bpy, call.args)
        if call.op == "backup":
            return backup(bpy)
        return {"ok": False, "error": failure("BadOp", f"unknown op {call.op!r}")}
    except Exception as exc:  # noqa: BLE001 - a bad command must not kill the timer
        return {"ok": False, "error": failure(type(exc).__name__, exc, traceback.format_exc())}
