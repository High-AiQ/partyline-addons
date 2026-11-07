"""Plain-Python relay state: one session, its lease, one call at a time. Never imports bpy.

Everything here may run on an HTTP thread, so it must stay free of Blender access; the main-thread timer
drains `queue` and reports back through `finish`.
"""
import queue
import threading
import time

LEASE = 15.0     # seconds without a renewal before the session fails closed
GRACE = 1.0      # extra seconds the HTTP thread waits past a call's deadline
GONE = ("blender", "lease")


class Conflict(Exception):
    """A session is already live."""


class Session:
    def __init__(self, sid, agent, checker, now):
        self.id, self.agent, self.checker = sid, agent, checker
        self.renewed = now
        self.namespace = None  # created on the main thread by the first exec
        self.current = None
        self.ended = None


class Call:
    def __init__(self, op, args, call_id, session, deadline, tracked=True):
        self.op, self.args, self.id, self.session = op, args, call_id, session
        self.deadline, self.tracked = deadline, tracked
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.result = None


class Relay:
    def __init__(self, clock=time.monotonic, lease=LEASE, wait=None):
        self.clock, self.lease = clock, lease
        self.wait = wait or (lambda event, seconds: event.wait(seconds))
        self.lock = threading.RLock()
        self.queue = queue.Queue()
        self.session = None
        self.busy = False      # a call is queued, running or overran
        self.gone = {}         # session id -> why it ended (blender | lease), for the helper's renew
        self.version = 0       # bumped whenever the session appears or disappears

    def alive(self, session):
        """Lock-free, so the tracer can call it on every line."""
        return self.session is session and self.clock() - session.renewed <= self.lease

    def expire(self):
        with self.lock:
            if self.session and self.clock() - self.session.renewed > self.lease:
                self.end("lease")

    def end(self, reason):
        """Close the live session and cancel its running call (cooperatively)."""
        with self.lock:
            session, self.session = self.session, None
            if session is None:
                return None
            session.ended, session.namespace = reason, None
            if session.current:
                session.current.cancel.set()
            if reason in GONE:
                self.gone = {**dict(list(self.gone.items())[-7:]), session.id: reason}
            self.version += 1
            return session

    def open(self, sid, agent, checker):
        with self.lock:
            self.expire()
            if self.session:
                raise Conflict
            self.gone.pop(sid, None)
            self.session = Session(sid, agent, checker, self.clock())
            self.version += 1
            return self.session

    def renew(self, sid):
        with self.lock:
            self.expire()
            session = self.session
            if session and session.id == sid:
                session.renewed = self.clock()
                return 200, {"ok": True, "busy": self.busy}
            if sid in self.gone:
                return 410, {"ended": self.gone[sid]}
            return 404, {"error": "unknown session"}

    def close(self, sid):
        with self.lock:
            closed = bool(self.session and self.session.id == sid)
            if closed:
                self.end("closed")
            return 200, {"ok": True, "closed": closed}

    def cancel(self, sid, call_id):
        with self.lock:
            self.expire()
            session = self.session
            if not session or session.id != sid:
                return 404, {"error": "unknown session"}
            current = session.current
            hit = bool(current and current.id == call_id)
            if hit:
                current.cancel.set()
            return 200, {"cancelled": hit}

    def status(self):
        with self.lock:
            self.expire()
            return {"busy": self.busy, "session": self.session.id if self.session else None}

    def submit(self, sid, call_id, op, args, timeout):
        """Queue one call for the main thread and wait for it; (HTTP status, body)."""
        with self.lock:
            self.expire()
            session = self.session
            if not session or session.id != sid:
                return 404, {"error": "unknown session"}
            if self.busy:
                return 409, {"busy": True, "error": "a call is running, or an earlier one overran"}
            call = Call(op, args, call_id, session, self.clock() + timeout)
            self.busy, session.current = True, call
            self.queue.put(call)
        if self.wait(call.done, timeout + GRACE):
            return 200, call.result
        call.cancel.set()  # still queued or stuck in C: it stops at its next Python line, or never
        return 200, {"ok": False, "cancelled": True, "busy": True, "error": {
            "type": "Timeout", "traceback": "",
            "message": f"no answer within {timeout}s; Blender is still busy with this call"}}

    def job(self, session, op, args, seconds):
        """A main-thread task outside the one-call rule (the backup); None when Blender never got to it."""
        call = Call(op, args, "", session, self.clock() + seconds, tracked=False)
        self.queue.put(call)
        if self.wait(call.done, seconds):
            return call.result
        call.cancel.set()
        return None

    def take(self):
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    def finish(self, call, result):
        with self.lock:
            call.result = result
            if call.tracked:
                self.busy = False
                if call.session.current is call:
                    call.session.current = None
            call.done.set()
