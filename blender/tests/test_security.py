import json
import os
import stat
import unittest

from support import RelayCase, bpy, server


class Security(RelayCase):
    def test_wrong_or_missing_secret_is_401_without_touching_bpy(self):
        self.assertEqual(self.request("GET", "/status", secret="nope")[0], 401)
        self.assertEqual(self.request("GET", "/status", secret=False)[0], 401)
        self.assertEqual(self.request("POST", "/session", {"id": "x", "agent": "a"}, secret="no")[0], 401)
        self.assertEqual(self.bpy.log, [])
        self.assertIsNone(self.relay.session)

    def test_any_origin_is_refused_even_with_the_secret(self):
        status, _ = self.request("GET", "/status", headers={"Origin": "https://example.com"})
        self.assertEqual(status, 403)
        self.assertEqual(self.request("GET", "/status", headers={"Origin": "null"})[0], 403)

    def test_host_must_be_the_loopback_address_and_port(self):
        self.assertEqual(self.request("GET", "/status", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/status", headers={"Host": "localhost:%d" % self.svc.port})[0], 403)
        self.assertEqual(self.request("GET", "/status")[0], 200)

    def test_no_cors_headers(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.svc.port)
        conn.request("GET", "/status", headers={"Authorization": "Bearer " + self.svc.secret})
        names = {k.lower() for k, _ in conn.getresponse().getheaders()}
        self.assertFalse([n for n in names if n.startswith("access-control")])

    def test_bad_bodies(self):
        self.assertEqual(self.request("POST", "/session", b"not json")[0], 400)
        self.assertEqual(self.request("POST", "/session", b"[1]")[0], 400)
        self.assertEqual(self.request("POST", "/session", b" " * (server.MAX_BODY + 10))[0], 413)
        self.open()
        self.assertEqual(self.request("POST", "/call", {"session": "s1", "call_id": "c", "op": "shell",
                                                        "args": {}})[0], 400)
        big = "x" * 65537
        self.assertEqual(self.exec(big)[0], 413)
        self.assertEqual(self.request("GET", "/nope")[0], 404)

    def test_discovery_file_and_persistent_secret(self):
        data = json.loads(self.svc.discovery_path.read_text())
        self.assertEqual(set(data), {"port", "secret", "pid", "blender", "file"})
        self.assertEqual((data["port"], data["blender"], data["file"]), (self.svc.port, "5.2.2", ""))
        self.assertEqual(len(data["secret"]), 64)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.svc.discovery_path.stat().st_mode), 0o600)
        secret = self.svc.secret
        self.svc.stop()
        self.assertFalse(self.svc.discovery_path.exists())
        again = server.Service(self.bpy, "partyline_relay", directory=self.dir)
        again.start(timers=False)
        self.addCleanup(again.stop)
        self.assertEqual(again.secret, secret)

    def test_discovery_follows_the_open_file(self):
        self.bpy.data.filepath = str(self.dir / "scene.blend")
        self.svc.drain()
        self.assertEqual(json.loads(self.svc.discovery_path.read_text())["file"], "scene.blend")

    def test_config_dir_matches_the_helper(self):
        from pathlib import Path
        self.assertEqual(server.config_dir({"LOCALAPPDATA": "C:/L"}, nt=True), Path("C:/L/Partyline"))
        self.assertEqual(server.config_dir({"XDG_CONFIG_HOME": "/x"}, nt=False), Path("/x/partyline"))

    def test_timers_are_registered_once(self):
        self.svc.stop()
        again = server.Service(self.bpy, "partyline_relay", directory=self.dir)
        again.start()
        self.assertTrue(self.bpy.app.timers.is_registered(again.drain))
        again.stop()
        self.assertFalse(self.bpy.app.timers.is_registered(again.drain))


if __name__ == "__main__":
    unittest.main()
