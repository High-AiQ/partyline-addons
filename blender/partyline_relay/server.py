"""Loopback HTTP service: secret, Host/Origin checks, discovery file and the main-thread timer drain.

HTTP threads only touch `state.Relay` (plain Python under a lock); every `bpy` call happens in `drain`, a
single `bpy.app.timers` callback on the main thread, so `/status` and `/session/renew` keep answering while
a long script blocks Blender.
"""
import hmac
import json
import os
import secrets
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer

from . import commands
from .state import Conflict, Relay

MAX_BODY = 128 * 1024
TICK = 0.05
JOB_WAIT = 60.0            # a backup of a large file may take a while
OPS = ("exec", "scene_info", "render", "screenshot")
DEFAULT_TIMEOUT, MIN_TIMEOUT, MAX_TIMEOUT = 20, 1, 120


class BadRequest(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def config_dir(environ=None, nt=None):
    """The directory the helper keeps helper.json in; the discovery file lives beside it."""
    environ = os.environ if environ is None else environ
    if os.name == "nt" if nt is None else nt:
        return Path(environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Partyline"
    return Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "partyline"


def write_private(path, text):
    """Atomic replace; owner-only on POSIX (Windows keeps the user-profile ACL)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_secret(bpy, package):
    """One 32-byte secret per install, generated on first use and kept in the add-on's user directory."""
    try:
        base = Path(bpy.utils.extension_path_user(package, path="", create=True))
    except Exception:  # noqa: BLE001 - not installed as an extension (development): use the config dir
        base = Path(bpy.utils.user_resource("CONFIG"))
    path = base / "relay-secret"
    secret = path.read_text().strip() if path.exists() else ""
    if len(secret) < 64:
        secret = secrets.token_hex(32)
        write_private(path, secret)
    return secret


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):  # skip HTTPServer's getfqdn(), which can stall on some systems
        TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def text(body, key, limit=128):
    value = body.get(key)
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        raise BadRequest(f"{key} must be a non-empty string")
    return value


def route_status(svc, body):
    return 200, {"ok": True, "blender": svc.blender, "file": svc.file, **svc.relay.status()}


def route_open(svc, body):
    sid, agent = text(body, "id"), "".join(c for c in text(body, "agent") if c.isprintable())[:60]
    checker = body.get("checker", False)
    if not isinstance(checker, bool):
        raise BadRequest("checker must be a boolean")
    try:
        session = svc.relay.open(sid, agent or "agent", checker)
    except Conflict:
        return 409, {"error": "session already active"}
    done = svc.relay.job(session, "backup", {}, svc.job_wait)
    if done is None:
        svc.relay.close(sid)
        return 503, {"error": "Blender did not respond (busy); try again"}
    session.renewed = svc.relay.clock()  # the lease starts once the backup is done
    return 200, {**{k: v for k, v in done.items() if k != "ok"}, "ok": True}


def route_renew(svc, body):
    return svc.relay.renew(text(body, "id"))


def route_close(svc, body):
    return svc.relay.close(text(body, "id"))


def route_call(svc, body):
    sid, call_id, op = text(body, "session"), text(body, "call_id"), body.get("op")
    args, timeout = body.get("args", {}), body.get("timeout", DEFAULT_TIMEOUT)
    if op not in OPS or not isinstance(args, dict):
        raise BadRequest("op must be exec, scene_info, render or screenshot, with an args object")
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise BadRequest("timeout must be a number of seconds")
    if op == "exec":
        code = args.get("code")
        if not isinstance(code, str):
            raise BadRequest("args.code must be a string")
        if len(code) > commands.MAX_CODE:
            raise BadRequest(f"code is longer than {commands.MAX_CODE} characters", 413)
    return svc.relay.submit(sid, call_id, op, args, max(MIN_TIMEOUT, min(MAX_TIMEOUT, timeout)))


def route_artifact(svc, body):
    """The bytes of an image the session rendered, once. 200 carries raw bytes, anything else a message."""
    sid, session = text(body, "session"), svc.relay.session
    if session is None or session.id != sid or not svc.relay.alive(session):
        return 404, {"error": "unknown session"}
    status, data = svc.relay.artifacts.take(sid, text(body, "id"))
    return (200, data) if status == 200 else (status, {"error": data.decode()})


def route_cancel(svc, body):
    return svc.relay.cancel(text(body, "session"), text(body, "call_id"))


ROUTES = {("GET", "/status"): route_status, ("POST", "/session"): route_open,
          ("POST", "/session/renew"): route_renew, ("POST", "/session/close"): route_close,
          ("POST", "/call"): route_call, ("POST", "/artifact"): route_artifact, ("POST", "/cancel"): route_cancel}


class Handler(BaseHTTPRequestHandler):
    server_version = "PartylineRelay"

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        raw = isinstance(body, bytes)
        data = body if raw else json.dumps(body).encode()
        self.send_response(status)
        for name, value in (("Content-Type", "application/octet-stream" if raw else "application/json"),
                            ("Content-Length", str(len(data))),
                            ("Cache-Control", "no-store")):  # deliberately no CORS headers
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("bad Content-Length") from None
        if length > MAX_BODY:
            self.rfile.read(min(length, 4 * MAX_BODY))
            raise BadRequest("body too large", 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise BadRequest("body is not JSON") from None
        if not isinstance(body, dict):
            raise BadRequest("body must be a JSON object")
        return body

    def route(self, method):
        svc = self.server.service
        # Order matters: browser-origin and rebinding defences first, the secret before any route.
        if "Origin" in self.headers:
            return self.reply(403, {"error": "browser requests are refused"})
        if self.headers.get("Host") != f"127.0.0.1:{svc.port}":
            return self.reply(403, {"error": "unexpected Host"})
        if not svc.authorized(self.headers.get("Authorization")):
            return self.reply(401, {"error": "bad or missing secret"})
        handler = ROUTES.get((method, self.path.split("?")[0]))
        if handler is None:
            return self.reply(404, {"error": "unknown route"})
        try:
            status, payload = handler(svc, self.read_json() if method == "POST" else {})
        except BadRequest as exc:
            status, payload = exc.status, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - answer rather than drop the connection
            status, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
        self.reply(status, payload)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")


class Service:
    def __init__(self, bpy, package, relay=None, directory=None, redraw=None):
        self.bpy, self.package = bpy, package
        self.relay = relay or Relay()
        self.directory = Path(directory) if directory else config_dir()
        self.redraw = redraw or (lambda: None)
        self.job_wait = JOB_WAIT
        self.secret = self.http = self.thread = None
        self.blender = self.file = ""
        self.seen = -1

    @property
    def port(self):
        return self.http.server_address[1]

    @property
    def discovery_path(self):
        return self.directory / "blender-relay.json"

    def authorized(self, header):
        given = (header or "").removeprefix("Bearer ")
        return bool(self.secret) and hmac.compare_digest(given.encode(), self.secret.encode())

    def write_discovery(self):
        write_private(self.discovery_path, json.dumps({
            "port": self.port, "secret": self.secret, "pid": os.getpid(),
            "blender": self.blender, "file": self.file}))

    def start(self, timers=True):
        self.secret = load_secret(self.bpy, self.package)
        self.blender = self.bpy.app.version_string
        self.file = Path(self.bpy.data.filepath).name
        self.http = Server(("127.0.0.1", 0), Handler)
        self.http.service = self
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.write_discovery()
        if timers:
            self.bpy.app.timers.register(self.drain, first_interval=TICK, persistent=True)

    def stop(self):
        self.relay.end("closed")
        self.relay.artifacts.clear()
        if self.http:
            self.http.shutdown()
            self.http.server_close()
            self.http = None
        if self.bpy.app.timers.is_registered(self.drain):
            self.bpy.app.timers.unregister(self.drain)
        self.discovery_path.unlink(missing_ok=True)

    def drain(self):
        """The one main-thread timer: expire the lease, run queued calls, keep the UI and file in step."""
        try:
            self.relay.expire()
            while (call := self.relay.take()) is not None:
                self.relay.finish(call, commands.execute(self.bpy, self.relay, call))
            name = Path(self.bpy.data.filepath).name
            if name != self.file:
                self.file = name
                self.write_discovery()
            if self.seen != self.relay.version:
                self.seen = self.relay.version
                self.redraw()
        except Exception as exc:  # noqa: BLE001 - an exception would unregister the timer
            print("partyline_relay: drain failed:", exc)
        return TICK
