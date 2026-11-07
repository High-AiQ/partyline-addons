import unittest

from support import Clock, RelayCase, fake_bpy


class Sessions(RelayCase):
    def test_open_unsaved_then_second_is_409(self):
        status, body = self.open()
        self.assertEqual((status, body), (200, {"ok": True, "backup": None, "unsaved": True}))
        self.assertEqual(self.open("s2")[0], 409)
        self.assertEqual(self.request("GET", "/status")[1]["session"], "s1")

    def test_backup_is_saved_beside_the_open_file(self):
        self.bpy.data.filepath = str(self.dir / "shot.blend")
        status, body = self.open()
        target = str(self.dir / "shot.agent-backup.blend")
        self.assertEqual((status, body["backup"], body["unsaved"]), (200, target, False))
        self.assertIn(("save", target, True), self.bpy.log)
        self.assertTrue((self.dir / "shot.agent-backup.blend").exists())

    def test_backup_failure_is_reported_not_fatal(self):
        self.bpy.data.filepath = str(self.dir / "shot.blend")
        self.bpy.ops.wm.save_as_mainfile = lambda **kw: (_ for _ in ()).throw(OSError("disk full"))
        status, body = self.open()
        self.assertEqual((status, body["backup"], body["backup_error"]), (200, None, "disk full"))

    def test_open_gives_up_when_blender_never_answers(self):
        self.relay.wait = lambda event, seconds: False
        self.assertEqual(self.request("POST", "/session", {"id": "s1", "agent": "a"})[0], 503)
        self.assertIsNone(self.relay.session)

    def test_renew_close_and_unknown(self):
        self.open()
        self.assertEqual(self.request("POST", "/session/renew", {"id": "s1"}), (200, {"ok": True, "busy": False}))
        self.assertEqual(self.request("POST", "/session/renew", {"id": "zz"})[0], 404)
        self.assertEqual(self.request("POST", "/session/close", {"id": "s1"})[1]["closed"], True)
        self.assertEqual(self.request("POST", "/session/renew", {"id": "s1"})[0], 404)
        self.assertEqual(self.exec("1")[0], 404)

    def test_lease_expiry_fails_closed(self):
        self.open()
        self.clock.now += 14
        self.assertEqual(self.request("POST", "/session/renew", {"id": "s1"})[0], 200)  # renewing resets it
        self.clock.now += 16
        self.svc.drain()                                    # the idle timer notices
        self.assertIsNone(self.relay.session)
        self.assertEqual(self.request("POST", "/session/renew", {"id": "s1"}), (410, {"ended": "lease"}))
        self.assertEqual(self.exec("1")[0], 404)
        self.assertTrue(self.redraws)                       # the banner was told to go

    def test_end_from_blender_is_410_for_the_helper(self):
        self.open(agent="bob\x07")
        self.assertEqual(self.relay.session.agent, "bob")
        self.relay.end("blender")
        self.assertEqual(self.request("POST", "/session/renew", {"id": "s1"}), (410, {"ended": "blender"}))
        self.assertEqual(self.open("s2")[0], 200)           # a new session may follow

    def test_checker_flag_is_recorded(self):
        self.open(checker=True)
        self.assertTrue(self.relay.session.checker)
        self.assertEqual(self.request("POST", "/session", {"id": "q", "agent": "a", "checker": "yes"})[0], 400)

    def test_scene_info(self):
        self.open()
        call = lambda args: self.request("POST", "/call", {  # noqa: E731
            "session": "s1", "call_id": "i", "op": "scene_info", "args": args})
        status, body = call({})
        self.assertEqual((status, body["ok"], body["scene"]["object_count"]), (200, True, 2))
        self.assertEqual(body["scene"]["camera"], "Camera")
        objects = call({"detail": "objects"})[1]["scene"]["objects"]
        self.assertEqual(objects[0]["bbox"], {"min": [-1.0] * 3, "max": [1.0] * 3})
        one = call({"detail": "object", "name": "Cube"})[1]["scene"]["object"]
        self.assertEqual((one["name"], one["scale"]), ("Cube", [1.0] * 3))
        self.assertEqual(call({"detail": "object", "name": "Nope"})[1]["error"]["type"], "NotFound")
        self.assertEqual(call({"detail": "wat"})[1]["error"]["type"], "BadDetail")

    def test_scene_info_truncates_to_the_cap(self):
        self.open()
        self.bpy.context.scene.objects.extend(fake_bpy.Obj("Thing%04d" % i) for i in range(400))
        scene = self.request("POST", "/call", {"session": "s1", "call_id": "i", "op": "scene_info",
                                               "args": {"detail": "objects"}})[1]["scene"]
        self.assertTrue(scene["truncated"])
        self.assertLessEqual(len(__import__("json").dumps(scene)), 16384)


if __name__ == "__main__":
    unittest.main()
