import unittest

from support import RelayCase


class Exec(RelayCase):
    def setUp(self):
        super().setUp()
        self.open()

    def test_stdout_last_expression_and_persistent_namespace(self):
        status, body = self.exec("x = 40\nprint('hi')\nx + 2")
        self.assertEqual((status, body["ok"], body["stdout"], body["result"]), (200, True, "hi\n", "42"))
        self.assertTrue(body["undo"])
        self.assertTrue(any(e[0] == "undo" and e[1].startswith("agent ") for e in self.bpy.log))
        self.assertEqual(self.exec("x * 2")[1]["result"], "80")      # state survives within the session
        self.relay.end("blender")
        self.open("s2")
        self.assertEqual(self.exec("'x' in dir()", sid="s2")[1]["result"], "False")  # and not beyond it

    def test_error_shape_hides_the_add_on_frames(self):
        _, body = self.exec("a = 1\nraise ValueError('boom')")
        error = body["error"]
        self.assertFalse(body["ok"])
        self.assertEqual((error["type"], error["message"]), ("ValueError", "boom"))
        self.assertIn('File "<agent>", line 2', error["traceback"])
        self.assertNotIn("commands.py", error["traceback"])
        self.assertFalse(body["cancelled"])

    def test_syntax_error_and_system_exit_are_just_errors(self):
        self.assertEqual(self.exec("def (")[1]["error"]["type"], "SyntaxError")
        self.assertEqual(self.exec("import sys\nsys.exit(3)")[1]["error"]["type"], "SystemExit")
        self.assertEqual(self.exec("1")[1]["result"], "1")           # the relay is still fine

    def test_output_is_capped_head_and_tail(self):
        _, body = self.exec("print('a' * 20000)\nprint('b' * 20000)\nprint('END')")
        self.assertTrue(body["truncated"])
        self.assertLessEqual(len(body["stdout"]), 16384)
        self.assertIn("...[truncated]...", body["stdout"])
        self.assertTrue(body["stdout"].startswith("aaa"))
        self.assertTrue(body["stdout"].endswith("bbb\nEND\n"))
        small = self.exec("print('x' * 100)")[1]
        self.assertFalse(small["truncated"])

    def test_traceback_is_capped(self):
        _, body = self.exec("def f(n):\n    return f(n + 1) if n < 60 else 1 / 0\nf(0)")
        self.assertLessEqual(len(body["error"]["traceback"]), 4096)
        self.assertEqual(body["error"]["type"], "ZeroDivisionError")


class Deadlines(RelayCase):
    clock_step = 0.05      # every reading of the clock moves it, so a loop reaches its deadline at once

    def setUp(self):
        super().setUp()
        self.open()

    def test_runaway_loop_is_cancelled_at_its_deadline_and_blender_recovers(self):
        status, body = self.exec("while True:\n    pass", timeout=1)
        self.assertEqual(status, 200)
        self.assertEqual((body["ok"], body["cancelled"], body["error"]["type"]), (False, True, "Deadline"))
        self.assertFalse(self.relay.busy)
        self.assertEqual(self.exec("1 + 1")[1]["result"], "2")

    def test_a_script_cannot_swallow_the_stop(self):
        body = self.exec("while True:\n    try:\n        pass\n    except Exception:\n        pass", timeout=1)[1]
        self.assertEqual(body["error"]["type"], "Deadline")

    def test_a_returned_objects_repr_runs_under_the_deadline(self):
        loop = "class Loop:\n    def __repr__(self):\n        while True:\n            pass\nLoop()"
        body = self.exec(loop, timeout=1)[1]
        self.assertEqual((body["ok"], body["cancelled"], body["error"]["type"]), (False, True, "Deadline"))
        self.assertIsNone(body["result"])
        self.assertFalse(self.relay.busy)
        self.assertEqual(self.exec("1 + 1")[1]["result"], "2")

    def test_a_repr_cannot_swallow_the_stop_or_escape_with_system_exit(self):
        swallow = ("class Swallow:\n    def __repr__(self):\n        while True:\n            try:\n"
                   "                pass\n            except Exception:\n                pass\nSwallow()")
        self.assertEqual(self.exec(swallow, timeout=1)[1]["error"]["type"], "Deadline")
        quits = "import sys\nclass Quit:\n    def __repr__(self):\n        sys.exit(2)\nQuit()"
        body = self.exec(quits)[1]
        self.assertEqual((body["ok"], body["error"]["type"]), (False, "SystemExit"))
        self.assertEqual(self.exec("2 * 2")[1]["result"], "4")

    def test_a_repr_that_raises_an_ordinary_error_still_returns_the_call(self):
        broken = "class Broken:\n    def __repr__(self):\n        raise ValueError('no')\nBroken()"
        body = self.exec(broken)[1]
        self.assertEqual((body["ok"], body["result"]), (True, "<repr failed: ValueError>"))

    def test_cancel_and_end_during_a_repr_stop_it(self):
        self.relay.session.namespace = {"stop": lambda: self.relay.cancel("s1", "c7")}
        body = self.exec("class R:\n    def __repr__(self):\n        stop()\n        while True:\n"
                         "            pass\nR()", call_id="c7", timeout=120)[1]
        self.assertEqual((body["cancelled"], body["error"]["type"]), (True, "Cancelled"))

    def test_cancel_endpoint_stops_the_running_call(self):
        sid, cid = "s1", "c9"
        self.relay.session.namespace = {"stop": lambda: self.relay.cancel(sid, cid)}
        body = self.exec("stop()\nwhile True:\n    pass", call_id=cid, timeout=120)[1]
        self.assertEqual((body["cancelled"], body["error"]["type"]), (True, "Cancelled"))
        self.assertEqual(self.request("POST", "/cancel", {"session": sid, "call_id": "none"})[1], {"cancelled": False})
        self.assertEqual(self.request("POST", "/cancel", {"session": "zz", "call_id": "x"})[0], 404)

    def test_end_from_blender_stops_a_running_script(self):
        self.relay.session.namespace = {"end": lambda: self.relay.end("blender")}
        body = self.exec("end()\nwhile True:\n    pass", timeout=120)[1]
        self.assertEqual((body["cancelled"], body["error"]["type"]), (True, "SessionEnded"))
        self.assertIsNone(self.relay.session)

    def test_lease_lapse_stops_a_running_script(self):
        self.relay.session.namespace = {"later": lambda: setattr(self.clock, "now", self.clock.now + 60)}
        body = self.exec("later()\nwhile True:\n    pass", timeout=120)[1]
        self.assertEqual(body["error"]["type"], "SessionEnded")



class ErrorText(RelayCase):
    """An error's text is built from plain fields: no script-defined hook runs while describing it."""

    def setUp(self):
        super().setUp()
        self.open()

    def test_custom_str_and_args_hooks_never_run_even_if_they_loop_or_exit(self):
        hooks = ("import sys\nran = []\n"
                 "class Loop(Exception):\n    def __str__(self):\n        ran.append(1)\n"
                 "        while True:\n            pass\n"
                 "class Quit(Exception):\n    def __str__(self):\n        ran.append(2)\n"
                 "        sys.exit(99)\n"
                 "class Args(Exception):\n    @property\n    def args(self):\n        ran.append(3)\n"
                 "        while True:\n            pass\n")
        self.exec(hooks)
        for name in ("Loop", "Quit"):
            body = self.exec(f"raise {name}('why')")[1]
            self.assertEqual((body["ok"], body["error"]["type"], body["cancelled"]), (False, name, False))
            self.assertEqual(body["error"]["message"], "why")
            self.assertIn('File "<agent>", line 1', body["error"]["traceback"])
        body = self.exec("raise Args('x')")[1]
        self.assertEqual(body["error"]["type"], "Args")
        self.assertEqual(self.exec("ran")[1]["result"], "[]")      # no hook was ever called
        self.assertEqual(self.exec("3")[1]["result"], "3")

    def test_argument_objects_are_named_not_printed(self):
        loop = ("class Loop:\n    def __repr__(self):\n        while True:\n            pass\n"
                "    __str__ = __repr__\n")
        self.exec(loop)
        body = self.exec("raise ValueError('a', 2, (1, 'b'), Loop(), [Loop()], {'k': 1})")[1]
        self.assertEqual(body["error"]["message"], "a, 2, (1, 'b'), <Loop>, <list>, <dict>")
        self.assertEqual(self.exec("raise KeyError('missing')")[1]["error"]["message"], "missing")
        self.assertEqual(self.exec("raise RuntimeError()")[1]["error"]["message"], "")

    def test_a_metaclass_cannot_rename_the_error_type(self):
        meta = ("class M(type):\n    @property\n    def __name__(cls):\n        while True:\n"
                "            pass\nclass Weird(Exception, metaclass=M):\n    pass\n")
        self.exec(meta)
        self.assertEqual(self.exec("raise Weird('w')")[1]["error"]["type"], "Weird")



class ErrorHooks(RelayCase):
    """Hooks reached through comparisons and formatting of an error's metadata (reviewer round 3)."""

    def setUp(self):
        super().setUp()
        self.open()
        self.exec("import sys\nran = []\n"
                  "class Name(str):\n    def __format__(self, spec):\n        ran.append('format')\n"
                  "        sys.exit(92)\n    def __getitem__(self, key):\n        ran.append('item')\n"
                  "        sys.exit(94)\n")

    def check(self, code, kind, filename=None):
        body = self.exec(code)[1]
        self.assertEqual((body["ok"], body["cancelled"], body["error"]["type"]), (False, False, kind))
        if filename:
            self.assertIn(filename, body["error"]["traceback"])
        self.assertEqual(self.exec("ran")[1]["result"], "[]")      # no hook ran
        self.assertEqual(self.exec("5")[1]["result"], "5")         # and the relay is fine

    def test_a_metaclass_eq_is_never_asked(self):
        self.check("class M(type):\n    def __eq__(cls, other):\n        ran.append('eq')\n        sys.exit(91)\n"
                   "    __hash__ = type.__hash__\n"
                   "class Bad(Exception, metaclass=M):\n    pass\nraise Bad('x', (1, 2))", "Bad")

    def test_a_str_subclass_filename_is_not_formatted(self):
        self.check("def f():\n    raise ValueError('x')\n"
                   "f.__code__ = f.__code__.replace(co_filename=Name('agent.py'), co_name='g')\nf()",
                   "ValueError", "agent.py")

    def test_a_str_subclass_type_name_is_not_formatted(self):
        self.check("class Bad(Exception):\n    pass\nBad.__name__ = Name('Bad')\nraise Bad()", "Bad")


class ErrorNet(RelayCase):
    clock_step = 0.05      # a loop reaches its deadline at once

    def setUp(self):
        super().setUp()
        self.open()

    def test_the_net_contains_a_missed_hook_that_raises_or_loops(self):
        from unittest import mock

        from partyline_relay import commands
        for effect, kind, cancelled, timeout in ((SystemExit(7), "Error", False, 100),
                                                 (None, "Deadline", True, 1)):
            def hook(*args, effect=effect):
                if effect:
                    raise effect
                while True:
                    pass
            with mock.patch.object(commands, "plain", hook):
                body = self.exec("raise ValueError('x')", timeout=timeout)[1]
            self.assertEqual((body["error"]["type"], body["cancelled"]), (kind, cancelled))
            self.assertEqual(self.exec("1")[1]["result"], "1")


class Busy(RelayCase):
    def test_an_overrun_call_blocks_new_ones_until_blender_frees_up(self):
        self.open()
        self.relay.wait = lambda event, seconds: False       # the main thread never got to it in time
        status, body = self.relay.submit("s1", "late", "exec", {"code": "1"}, 2)
        self.assertEqual((status, body["error"]["type"], body["busy"], body["cancelled"]), (200, "Timeout", True, True))
        self.assertEqual(self.relay.submit("s1", "next", "exec", {"code": "1"}, 2)[0], 409)
        renewed = self.request("POST", "/session/renew", {"id": "s1"}, pump=False)  # Blender is "stuck"
        self.assertEqual(renewed, (200, {"ok": True, "busy": True}))
        self.assertTrue(self.request("GET", "/status", pump=False)[1]["busy"])
        self.svc.drain()                                     # Blender gets back: the cancelled call is dropped
        self.assertFalse(self.relay.busy)
        self.relay.wait = lambda event, seconds: event.wait(seconds)
        self.assertEqual(self.exec("5")[1]["result"], "5")

    def test_end_before_the_call_runs_does_not_run_it(self):
        self.open()
        self.relay.wait = lambda event, seconds: False
        self.relay.submit("s1", "late", "exec", {"code": "print('ran')"}, 2)
        self.relay.end("blender")
        self.svc.drain()
        self.assertFalse(self.relay.busy)
        self.assertNotIn("undo", [e[0] for e in self.bpy.log])


class Register(unittest.TestCase):
    def test_register_banner_and_end_operator(self):
        from support import fake_bpy
        import sys
        bpy = fake_bpy.install()
        sys.modules.pop("partyline_relay", None)
        sys.modules.pop("partyline_relay.server", None)
        import partyline_relay
        try:
            partyline_relay.register()
            self.assertIn(partyline_relay.draw_banner, bpy.types.STATUSBAR_HT_header.drawn)
            relay = partyline_relay.service.relay
            relay.open("s", "carol", False)
            drawn = []
            layout = type("L", (), {"row": lambda self: type("R", (), {
                "alert": False, "label": lambda s, **kw: drawn.append(kw["text"]),
                "operator": lambda s, name, text: drawn.append(name)})()})()
            partyline_relay.draw_banner(type("H", (), {"layout": layout})(), None)
            self.assertEqual(drawn, ["Agent carol is connected", "partyline_relay.end_session"])
            partyline_relay.PARTYLINE_OT_end_session().execute(None)
            self.assertIsNone(relay.session)
        finally:
            partyline_relay.unregister()
        self.assertEqual(bpy.types.STATUSBAR_HT_header.drawn, [])
        self.assertIsNone(partyline_relay.service)

    def test_background_mode_does_not_start(self):
        from support import fake_bpy
        import sys
        bpy = fake_bpy.install()
        bpy.app.background = True
        sys.modules.pop("partyline_relay", None)
        sys.modules.pop("partyline_relay.server", None)
        import partyline_relay
        partyline_relay.register()
        self.assertIsNone(partyline_relay.service)


if __name__ == "__main__":
    unittest.main()
