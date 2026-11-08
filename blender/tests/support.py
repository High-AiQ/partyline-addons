"""Shared test plumbing: a Service on a real loopback port, with the main-thread timer pumped by hand."""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_bpy  # noqa: E402

bpy = fake_bpy.install()

from partyline_relay import server  # noqa: E402
from partyline_relay.state import Relay  # noqa: E402


class Clock:
    """Time that only moves when the test says so (or, with `step`, a little at every reading)."""

    def __init__(self, step=0.0):
        self.now, self.step = 1000.0, step

    def __call__(self):
        self.now += self.step
        return self.now


class RelayCase(unittest.TestCase):
    clock_step = 0.0

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.bpy = fake_bpy.make(self.dir)
        self.clock = Clock(self.clock_step)
        self.relay = Relay(clock=self.clock)
        self.redraws = []
        self.svc = server.Service(self.bpy, "partyline_relay", relay=self.relay, directory=self.dir,
                                  redraw=lambda: self.redraws.append(1))
        self.svc.start(timers=False)
        self.addCleanup(self.svc.stop)

    def request(self, method, path, body=None, secret=True, headers=None, pump=True):
        """Send one request from a thread while this thread plays Blender's timer; (status, json)."""
        out = {}

        def send():
            head = {"Authorization": "Bearer " + (self.svc.secret if secret is True else secret or "")}
            head.update(headers or {})
            if not secret:
                head.pop("Authorization")
            conn = http.client.HTTPConnection("127.0.0.1", self.svc.port, timeout=10)
            payload = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
            conn.request(method, path, payload if method == "POST" else None, head)
            response = conn.getresponse()
            raw = response.read()
            binary = response.getheader("Content-Type") == "application/octet-stream"
            out["status"], out["body"] = response.status, raw if binary else json.loads(raw or b"{}")
            conn.close()

        thread = threading.Thread(target=send)
        thread.start()
        while thread.is_alive():
            if pump:
                self.svc.drain()
            thread.join(0.001)
        return out["status"], out["body"]

    def open(self, sid="s1", agent="alice", checker=False):
        return self.request("POST", "/session", {"id": sid, "agent": agent, "checker": checker})

    def exec(self, code, sid="s1", timeout=5, call_id="c1"):
        return self.request("POST", "/call", {"session": sid, "call_id": call_id, "op": "exec",
                                              "args": {"code": code}, "timeout": timeout})
