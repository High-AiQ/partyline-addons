import sys
import types

import fake_bpy
from support import RelayCase


class Media(RelayCase):
    """render and screenshot: clamps reported, settings restored, one-shot artifacts, discarded when late."""

    def call(self, op, args=None, sid="s1", timeout=5, call_id="m1"):
        return self.request("POST", "/call", {"session": sid, "call_id": call_id, "op": op,
                                              "args": args or {}, "timeout": timeout})

    def fetch(self, artifact, sid="s1"):
        return self.request("POST", "/artifact", {"session": sid, "id": artifact})

    def on_disk(self):
        root = self.relay.artifacts.root
        return sorted(p.name for p in root.iterdir()) if root and root.exists() else []

    def scene(self):
        scene = self.bpy.context.scene
        render = scene.render
        return (scene.camera.name, render.engine, render.resolution_x, render.resolution_y,
                render.resolution_percentage, render.image_settings.file_format,
                render.image_settings.color_mode, scene.eevee.taa_render_samples, scene.frame_current)


class Render(Media):
    def setUp(self):
        super().setUp()
        self.open()

    def test_defaults_render_with_workbench_at_the_scene_size_and_hand_over_the_png_once(self):
        before = self.scene()
        status, body = self.call("render")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["settings"], {"camera": "Camera", "engine": "workbench", "width": 960,
                                            "height": 540, "samples": None, "frame": 1})
        self.assertEqual(body["adjusted"], [])
        artifact = body["artifact"]
        self.assertEqual((artifact["mime"], artifact["bytes"]), ("image/png", len(fake_bpy.PNG)))
        self.assertEqual(self.bpy.ops.render.render.settings[:3], ("Camera", "BLENDER_WORKBENCH", 960))
        self.assertEqual(self.scene(), before)           # everything the render changed is back
        self.assertEqual(self.fetch(artifact["id"]), (200, fake_bpy.PNG))
        self.assertEqual(self.fetch(artifact["id"])[0], 404)   # handed over once
        self.assertEqual(list(self.relay.artifacts.files), [])

    def test_clamps_are_reported_never_silent(self):
        _, body = self.call("render", {"width": 99999, "height": 3, "engine": "eevee", "samples": 5000,
                                       "frame": 9999})
        self.assertEqual(body["settings"], {"camera": "Camera", "engine": "eevee", "width": 2048,
                                            "height": 16, "samples": 64, "frame": 250})
        self.assertEqual(body["adjusted"], ["width 99999 -> 2048", "height 3 -> 16", "frame 9999 -> 250",
                                            "samples 5000 -> 64"])
        self.assertEqual(self.bpy.ops.render.render.settings,
                         ("Camera", "BLENDER_EEVEE", 2048, 16, 100, "PNG", 64, 250))
        self.assertEqual(self.scene()[-2:], (16, 1))      # samples and frame restored

    def test_the_scenes_own_sample_count_is_clamped_when_none_is_asked_for(self):
        self.bpy.context.scene.eevee.taa_render_samples = 4096
        _, body = self.call("render", {"engine": "eevee"})
        self.assertEqual((body["settings"]["samples"], body["adjusted"]), (64, ["samples 4096 -> 64"]))
        self.assertEqual(self.bpy.ops.render.render.settings[6], 64)
        self.assertEqual(self.scene()[-2], 4096)                       # the file's setting is back
        self.bpy.context.scene.eevee.taa_render_samples = 16
        _, body = self.call("render", {"engine": "eevee"}, call_id="m2")
        self.assertEqual((body["settings"]["samples"], body["adjusted"]), (16, []))

    def test_workbench_says_it_ignored_samples(self):
        _, body = self.call("render", {"samples": 8})
        self.assertEqual(body["adjusted"], ["samples ignored: workbench uses the scene's anti-aliasing"])
        self.assertEqual(self.bpy.ops.render.render.settings[6], 16)

    def test_camera_select_and_the_list_when_it_is_wrong(self):
        _, body = self.call("render", {"camera": "Side"})
        self.assertEqual(body["settings"]["camera"], "Side")
        self.assertEqual(self.scene()[0], "Camera")
        _, body = self.call("render", {"camera": "Lamp"}, call_id="m2")
        self.assertEqual((body["ok"], body["error"]["type"]), (False, "NoCamera"))
        self.assertIn("cameras: Camera, Side", body["error"]["message"])
        self.bpy.context.scene.camera = None
        _, body = self.call("render", call_id="m3")
        self.assertIn("no active camera", body["error"]["message"])

    def test_bad_arguments_are_refused_before_anything_is_touched(self):
        for args, kind in (({"engine": "cycles"}, "BadEngine"), ({"width": "big"}, "BadArgument"),
                           ({"height": True}, "BadArgument")):
            _, body = self.call("render", args, call_id="m" + kind)
            self.assertEqual((body["ok"], body["error"]["type"]), (False, kind))
        self.assertFalse([entry for entry in self.bpy.log if entry[0] == "render"])

    def test_a_failed_render_is_an_error_and_leaves_the_scene_alone(self):
        before = self.scene()

        def fail():
            raise RuntimeError("Error: no GPU")

        self.bpy.ops.render.render.during = fail
        _, body = self.call("render")
        self.assertEqual((body["ok"], body["error"]["type"]), (False, "RuntimeError"))
        self.assertIn("render failed: Error: no GPU", body["error"]["message"])
        self.assertEqual(self.scene(), before)
        self.assertEqual(list(self.relay.artifacts.files), [])

    def test_cancel_during_the_render_discards_the_image(self):
        self.bpy.ops.render.render.during = lambda: self.relay.cancel("s1", "m1")
        _, body = self.call("render")
        self.assertEqual((body["ok"], body.get("cancelled"), body["error"]["type"]), (False, True, "Cancelled"))
        self.assertEqual(list(self.relay.artifacts.files), [])
        self.assertEqual(self.on_disk(), [])

    def test_deadline_passing_during_the_render_discards_the_image(self):
        self.bpy.ops.render.render.during = lambda: setattr(self.clock, "now", self.clock.now + 60)
        _, body = self.call("render", timeout=5)
        self.assertEqual((body["ok"], body["error"]["type"]), (False, "Cancelled"))
        self.assertEqual(list(self.relay.artifacts.files), [])

    def test_the_session_ending_during_the_render_discards_the_image(self):
        self.bpy.ops.render.render.during = lambda: self.relay.end("blender")
        _, body = self.call("render")
        self.assertEqual(body["error"]["type"], "Cancelled")
        self.assertEqual(list(self.relay.artifacts.files), [])
        self.assertEqual(self.on_disk(), [])      # the file written after the session ended is gone too

    def test_a_session_that_ended_and_a_new_one_leave_no_image_behind_either(self):
        def restart():
            self.relay.end("blender")
            self.open("s2")
        self.bpy.ops.render.render.during = restart
        _, body = self.call("render", sid="s1")
        self.assertEqual((body["ok"], self.on_disk()), (False, []))


class Artifacts(Media):
    def setUp(self):
        super().setUp()
        self.open()

    def test_a_render_is_the_sessions_own(self):
        artifact = self.call("render")[1]["artifact"]["id"]
        self.open("s2")                                   # refused (409): s1 is live
        self.assertEqual(self.fetch(artifact, "s2")[0], 404)
        self.assertEqual(self.fetch("nope")[0], 404)
        self.assertEqual(self.fetch(artifact)[0], 200)

    def test_needs_the_secret(self):
        artifact = self.call("render")[1]["artifact"]["id"]
        status, _ = self.request("POST", "/artifact", {"session": "s1", "id": artifact}, secret="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(self.fetch(artifact)[0], 200)    # the wrong try did not consume it

    def test_ending_the_session_deletes_the_files(self):
        artifact = self.call("render")[1]["artifact"]["id"]
        path = self.relay.artifacts.files[artifact][1]
        self.assertTrue(path.exists())
        self.request("POST", "/session/close", {"id": "s1"})
        self.assertFalse(path.exists())
        self.assertEqual(self.fetch(artifact)[0], 404)

    def test_a_lapsed_lease_refuses_the_fetch(self):
        artifact = self.call("render")[1]["artifact"]["id"]
        self.clock.now += 30
        self.assertEqual(self.fetch(artifact)[0], 404)

    def test_stopping_the_add_on_removes_the_directory(self):
        self.call("render")
        root = self.relay.artifacts.root
        self.assertTrue(root.exists())
        self.svc.stop()
        self.assertFalse(root.exists())

    def test_only_the_newest_unfetched_images_are_kept(self):
        ids = [self.call("render", call_id=f"k{n}")[1]["artifact"]["id"] for n in range(10)]
        self.assertEqual(list(self.relay.artifacts.files), ids[-8:])

    def test_a_file_over_the_cap_is_413_and_removed(self):
        from partyline_relay import artifacts
        artifact = self.call("render")[1]["artifact"]["id"]
        self.relay.artifacts.files[artifact][1].write_bytes(b"x" * 64)
        self.addCleanup(setattr, artifacts, "MAX_BYTES", artifacts.MAX_BYTES)
        artifacts.MAX_BYTES = 10
        self.assertEqual(self.fetch(artifact)[0], 413)
        self.assertEqual(self.fetch(artifact)[0], 404)


class Screenshot(Media):
    def setUp(self):
        super().setUp()
        self.open()
        self.gpu = fake_bpy.install_gpu()

    def test_largest_viewport_is_drawn_off_screen_at_a_reduced_size(self):
        status, body = self.call("screenshot", {"max_size": 800})
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], {"width": 800, "height": 450, "viewport": [1600, 900]})
        self.assertEqual(body["artifact"]["mime"], "image/png")
        self.assertEqual(self.fetch(body["artifact"]["id"]), (200, fake_bpy.PNG))
        shot = self.gpu.made[-1]
        self.assertEqual((shot.size, shot.freed), ((800, 450), True))
        args, kwargs = shot.drawn[0]
        self.assertEqual((args[1], args[4:], kwargs), ("layer", ("view", "proj"), {"do_color_management": True}))
        self.assertEqual(list(self.bpy.data.images), ["Render Result"])   # the temporary image is gone

    def test_default_size_clamps_and_never_enlarges(self):
        _, body = self.call("screenshot")
        self.assertEqual(body["settings"]["width"], 1024)
        _, body = self.call("screenshot", {"max_size": 99999}, call_id="m2")
        self.assertEqual((body["settings"]["width"], body["adjusted"]), (1600, ["max_size 99999 -> 1600"]))
        _, body = self.call("screenshot", {"max_size": 1}, call_id="m3")
        self.assertEqual(body["settings"]["width"], 64)

    def test_no_viewport_is_a_plain_error(self):
        self.bpy.context.window_manager.windows[0].screen.areas.clear()
        _, body = self.call("screenshot")
        self.assertEqual((body["ok"], body["error"]["type"]), (False, "NoViewport"))
        self.bpy.context.window_manager = None
        self.assertEqual(self.call("screenshot", call_id="m2")[1]["error"]["type"], "NoViewport")

    def test_a_missing_gpu_is_reported_and_frees_nothing_it_did_not_make(self):
        self.gpu.ok = False
        _, body = self.call("screenshot")
        self.assertEqual((body["ok"], body["error"]["type"]), (False, "RuntimeError"))
        self.assertIn("screenshot failed: GPUOffScreen: no GPU context", body["error"]["message"])
        self.assertEqual(list(self.relay.artifacts.files), [])

    def test_no_gpu_module_at_all(self):
        saved = sys.modules.pop("gpu")
        self.addCleanup(sys.modules.__setitem__, "gpu", saved)
        sys.modules["gpu"] = None            # `import gpu` raises ImportError
        _, body = self.call("screenshot")
        self.assertEqual((body["ok"], body["error"]["type"]), (False, "NoGpu"))

    def test_the_session_ending_during_the_capture_leaves_no_file(self):
        self.gpu.draw_view3d = lambda *a, **k: self.relay.end("blender")
        _, body = self.call("screenshot")
        self.assertEqual((body["ok"], body["error"]["type"]), (False, "Cancelled"))
        self.assertEqual((list(self.relay.artifacts.files), self.on_disk()), ([], []))
        del self.gpu.draw_view3d

    def test_the_largest_viewport_is_drawn_with_its_own_windows_scene_and_layer(self):
        other = types.SimpleNamespace(name="Other")
        window = types.SimpleNamespace(scene=other, view_layer="other-layer", screen=types.SimpleNamespace(
            areas=[fake_bpy.viewport(2400, 1200)]))
        self.bpy.context.window_manager.windows.append(window)
        _, body = self.call("screenshot", {"max_size": 1200})
        self.assertEqual(body["settings"]["viewport"], [2400, 1200])
        args, _ = self.gpu.made[-1].drawn[0]
        self.assertEqual((args[0], args[1]), (other, "other-layer"))
        self.bpy.context.window_manager.windows.remove(window)             # the first window again
        self.call("screenshot", call_id="m2")
        args, _ = self.gpu.made[-1].drawn[0]
        self.assertEqual((args[0], args[1]), (self.bpy.context.scene, "layer"))

    def test_cancelled_during_the_capture_discards_the_image(self):
        self.gpu.draw_view3d = lambda *a, **k: self.relay.cancel("s1", "m1")
        _, body = self.call("screenshot")
        self.assertEqual((body["ok"], body["error"]["type"]), (False, "Cancelled"))
        self.assertEqual(list(self.relay.artifacts.files), [])
        del self.gpu.draw_view3d
