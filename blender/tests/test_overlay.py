"""Overlay maths, exact framing and restoration without a Blender dependency."""
import base64
import copy
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import fake_bpy
from support import RelayCase
from partyline_relay import overlay, server
from partyline_relay.render import Refused, applied


class Maths(unittest.TestCase):
    def test_worst_case_reference_body_fits_helper_and_http_limits(self):
        encoded = base64.b64encode(b'x' * overlay.MAX_REFERENCE_BYTES).decode('ascii')
        args = {'reference': {'id': '\U0001f600' * 80, 'png_base64': encoded},
                'camera': '\U0001f600' * 200, 'width': 100000, 'height': 100000,
                'samples': 100000, 'engine': 'workbench', 'frame': -1000000,
                'overlay_opacity': 0.1234567890123456}
        body = {'session': 's' * 128, 'call_id': 'c' * 128, 'op': 'render', 'args': args, 'timeout': 180.0}
        self.assertLessEqual(len(json.dumps(body).encode()), server.MAX_BODY)
        body = {key: value for key, value in body.items() if key != 'session'}
        body.update(type='tool_call', session_id='s' * 128)
        self.assertLessEqual(len(json.dumps(body).encode()), 1_000_000)  # installed helper limit

    def test_source_over_uses_render_alpha_and_opacity(self):
        back = np.array([[[1, 0, 0, 1], [0, 1, 0, 1]]], dtype=np.float32)
        front = np.array([[[0, 0, 1, 0.5], [1, 1, 1, 0]]], dtype=np.float32)
        np.testing.assert_allclose(overlay.composite(back, front), [[[0.75, 0, 0.25, 1], [0, 1, 0, 1]]])
        np.testing.assert_allclose(overlay.composite(back, front, 0), back)
        np.testing.assert_allclose(overlay.composite(back, front, 1)[0, 0], [0.5, 0, 0.5, 1])

    def test_translucent_reference_and_zero_alpha_do_not_divide_by_zero(self):
        back = np.array([[[1, 0, 0, 0.5], [1, 1, 1, 0]]])
        front = np.array([[[0, 0, 1, 0.5], [0, 0, 0, 0]]])
        np.testing.assert_allclose(overlay.composite(back, front, 1), [[[1/3, 0, 2/3, 0.75], [0, 0, 0, 0]]])
        with self.assertRaises(ValueError):
            overlay.composite(back, np.zeros((1, 1, 4)))
        for value in (-1, 2, True, float('nan'), float('inf'), '0.5'):
            with self.assertRaises(Refused):
                overlay.composite(back, front, value)

    def test_proxy_faces_are_translucent_and_edges_remain_visible(self):
        front = np.zeros((7, 7, 4), dtype=np.float32)
        front[1:6, 1:6] = [1, 0, 0, 1]
        out = overlay.proxy_pixels(front)
        np.testing.assert_allclose(out[3, 3], [1, 0, 0, 0.35])
        np.testing.assert_allclose(out[1, 3], [0.02, 0.02, 0.02, 1])
        np.testing.assert_allclose(out[0, 0], [0, 0, 0, 0])
        np.testing.assert_allclose(front[1, 3], [1, 0, 0, 1])  # input not mutated

    def test_live_bpy_arrays_are_snapshotted_before_assignment(self):
        class bpy_prop_array(list):
            pass
        class Owner:
            def __init__(self):
                self._color = bpy_prop_array([1, 1, 1, 1])
            @property
            def color(self):
                return self._color
            @color.setter
            def color(self, values):
                self._color[:] = values
        obj = Owner()
        with applied([(obj, 'color', (0.1, 0.2, 0.3, 1))]):
            self.assertEqual(obj.color, [0.1, 0.2, 0.3, 1])
        self.assertEqual(obj.color, [1, 1, 1, 1])

    def test_exact_resolution_and_aspect_refusal(self):
        scene = fake_bpy.make().context.scene
        image = types.SimpleNamespace(size=(1600, 900))
        self.assertEqual(overlay.dimensions(scene, image, 1920, 1080, bounded=True), (1600, 900))
        self.assertEqual(overlay.dimensions(scene, image), (1600, 900))
        for size, width, height, bounded in (((1600, 900), 1000, 1000, False),
                                            ((4096, 2304), None, None, True),
                                            ((0, 0), None, None, False)):
            image.size = size
            with self.assertRaises(Refused):
                overlay.dimensions(scene, image, width, height, bounded=bounded)

    def test_proxy_overrides_restore_on_success_and_failure(self):
        scene = fake_bpy.make().context.scene
        shading = types.SimpleNamespace(light='STUDIO', color_type='MATERIAL', show_shadows=True,
                                         show_cavity=True, show_object_outline=False,
                                         object_outline_color=(1, 1, 1))
        scene.display = types.SimpleNamespace(shading=shading)
        for obj in scene.objects:
            obj.hide_render, obj.color = False, (0.4, 0.4, 0.4, 1)
        before = copy.deepcopy(vars(shading))
        for fails in (False, True):
            try:
                with overlay.proxy_style(scene, [scene.objects[0]]):
                    self.assertEqual(shading.light, 'FLAT')
                    self.assertEqual(scene.render.engine, 'BLENDER_WORKBENCH')
                    self.assertEqual(scene.objects[0].color, overlay.PALETTE[0])
                    if fails:
                        raise RuntimeError('render failed')
            except RuntimeError:
                pass
            self.assertEqual(vars(shading), before)
            self.assertEqual(scene.render.engine, 'CYCLES')
            self.assertTrue(all(o.color == (0.4, 0.4, 0.4, 1) and not o.hide_render for o in scene.objects))


class Pixels(list):
    def foreach_get(self, target):
        target[:] = self

    def foreach_set(self, source):
        self[:] = source


class Renderer(unittest.TestCase):
    def setUp(self):
        self.bpy = fake_bpy.make()
        self.scene = self.bpy.context.scene
        out = self.scene.render
        out.resolution_x, out.resolution_y = 640, 480
        out.film_transparent, out.pixel_aspect_x, out.pixel_aspect_y, out.use_border = False, 2, 2, True
        self.before = copy.deepcopy(vars(out))
        self.made = []
        self.removed = []
        images = self.bpy.data.images
        def load(path, check_existing):
            image = fake_bpy.Image(path, 32, 24)
            image.pixels = Pixels([1, 0, 0, 1] * (32 * 24))
            image.update = lambda: None
            self.made.append(image)
            return image
        def remove(image):
            self.removed.append(image)
        def new(name, width, height, alpha):
            return load(name, False)
        images.load, images.remove, images.new = load, remove, new
        self.bpy.ops.render.render = lambda **kwargs: None

    def assert_restored(self):
        self.assertEqual(vars(self.scene.render), self.before)
        self.assertEqual(set(map(id, self.made)), set(map(id, self.removed)))

    def test_write_and_return_image_paths_restore_and_clean_up(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'overlay.png'
            self.assertEqual(overlay.render_overlay(self.bpy, 'reference', output=output), output)
            self.assertEqual(output.read_bytes(), fake_bpy.PNG)
        self.assert_restored()
        result = overlay.render_overlay(self.bpy, 'reference')
        self.assertNotIn(result, self.removed)
        self.bpy.data.images.remove(result)
        self.assert_restored()

    def test_render_failure_restores_all_settings_and_releases_reference(self):
        def fail(**kwargs):
            self.assertTrue(self.scene.render.film_transparent)
            self.assertFalse(self.scene.render.use_border)
            self.assertEqual(self.scene.render.pixel_aspect_x, 1)
            self.assertEqual((self.scene.render.resolution_x, self.scene.render.resolution_y), (32, 24))
            raise RuntimeError('no GPU')
        self.bpy.ops.render.render = fail
        with self.assertRaisesRegex(RuntimeError, 'no GPU'):
            overlay.render_overlay(self.bpy, 'reference')
        self.assert_restored()

    def test_relay_accepts_only_bounded_png_bytes_and_removes_temp_file(self):
        encoded = base64.b64encode(fake_bpy.PNG).decode()
        with patch.object(overlay, 'render_overlay') as render:
            overlay.relay_overlay(self.bpy, {'reference': {'png_base64': encoded}}, 'output')
            reference = render.call_args.args[1]
            self.assertFalse(reference.exists())
            self.assertTrue(render.call_args.kwargs['bounded'])
        for payload in ('/some/path', {}, {'png_base64': '!'},
                        {'png_base64': base64.b64encode(b'not PNG').decode()},
                        {'png_base64': 'x' * (3 * 1024 * 1024)}):
            with self.assertRaises(Refused):
                overlay.relay_overlay(self.bpy, {'reference': payload}, 'output')


class RelayOverlay(RelayCase):
    def test_bad_reference_is_refused_and_leaves_no_artifact(self):
        self.open()
        _, body = self.request('POST', '/call', {'session': 's1', 'call_id': 'bad',
            'op': 'render', 'args': {'reference': '/some/path'}, 'timeout': 5})
        self.assertFalse(body['ok'])
        self.assertEqual(body['error']['type'], 'BadReference')
        self.assertFalse(self.relay.artifacts.files)
        self.assertEqual(self.bpy.context.scene.render.engine, 'CYCLES')

    def test_large_scene_frame_is_checked_before_independent_axis_clamps(self):
        self.open()
        scene = self.bpy.context.scene
        reference = {'png_base64': base64.b64encode(fake_bpy.PNG).decode()}
        for index, (w, h, percentage, px, py) in enumerate(((2560, 1440, 100, 1, 1),
                (3840, 2160, 100, 1, 1), (3840, 2160, 50, 1, 1), (1920, 2160, 100, 2, 1))):
            with self.subTest(size=(w, h), percentage=percentage, pixels=(px, py)):
                scene.render.resolution_x, scene.render.resolution_y = w, h
                scene.render.resolution_percentage = percentage
                scene.render.pixel_aspect_x, scene.render.pixel_aspect_y = px, py
                def render(bpy, path, w=w, h=h, percentage=percentage, px=px, py=py, **kwargs):
                    # This runs while scene.render contains the independently clamped sizes.
                    image = bpy.data.images.new('Overlay', width=1280, height=720, alpha=True)
                    self.assertEqual(overlay.dimensions(scene, image, kwargs['width'], kwargs['height'],
                                                        bounded=True), (1280, 720))
                    self.assertEqual(kwargs['width'], w * percentage // 100 * px)
                    self.assertEqual(kwargs['height'], h * percentage // 100 * py)
                    return image
                with patch.object(overlay, 'render_overlay', side_effect=render):
                    _, body = self.request('POST', '/call', {'session': 's1', 'call_id': str(index),
                        'op': 'render', 'args': {'reference': reference}, 'timeout': 5})
                self.assertTrue(body['ok'], body)
                self.assertEqual((body['settings']['width'], body['settings']['height']), (1280, 720))
                self.assertEqual((scene.render.resolution_x, scene.render.resolution_y), (w, h))
                self.assertEqual(scene.render.resolution_percentage, percentage)
                self.request('POST', '/artifact', {'session': 's1', 'id': body['artifact']['id']})

    def test_overlay_runs_through_existing_cancel_deadline_and_restore_path(self):
        self.open()
        def render(bpy, args, path):
            self.assertEqual(bpy.context.scene.frame_current, 10)
            self.assertEqual(args['overlay_opacity'], 0.4)
            path.write_bytes(fake_bpy.PNG)
            return (64, 48)  # Blender may leave Render Result.size at zero
        args = {'reference': {'png_base64': 'payload'}, 'frame': 10, 'overlay_opacity': 0.4}
        for reason in ('success', 'cancel', 'deadline', 'session'):
            with self.subTest(reason=reason):
                def run(*a, reason=reason):
                    size = render(*a)
                    if reason == 'cancel':
                        self.relay.cancel('s1', reason)
                    elif reason == 'deadline':
                        self.clock.now += 10
                    elif reason == 'session':
                        self.relay.end('blender')
                    return size
                with patch.object(overlay, 'relay_overlay', side_effect=run):
                    _, body = self.request('POST', '/call', {'session': 's1', 'call_id': reason,
                        'op': 'render', 'args': args, 'timeout': 5})
                self.assertEqual(self.bpy.context.scene.frame_current, 1)
                self.assertEqual(self.bpy.context.scene.render.engine, 'CYCLES')
                if reason == 'success':
                    self.assertTrue(body['ok'], body)
                    self.assertEqual(body['settings']['width'], 64)
                    self.request('POST', '/artifact', {'session': 's1', 'id': body['artifact']['id']})
                else:
                    self.assertTrue(body['cancelled'], body)
                    self.assertFalse(self.relay.artifacts.files)
