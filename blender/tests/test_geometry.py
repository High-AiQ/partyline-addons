import json
import math
import types
import unittest
from unittest import mock

import fake_bpy
from fake_world import FakeWorld, box

bpy = fake_bpy.install()

from partyline_relay import commands, contact, geometry, world  # noqa: E402


class Mesh(fake_bpy.Obj):
    def __init__(self, name, type="MESH", hidden=False, instancer="NONE", particles=(), modifiers=()):
        super().__init__(name, type)
        self.hidden, self.instance_type, self.particle_systems = hidden, instancer, list(particles)
        self.modifiers = [types.SimpleNamespace(type=m) for m in modifiers]

    def visible_get(self):
        return not self.hidden


def scene_of(*objects):
    bpy = fake_bpy.make()
    scene = bpy.context.scene
    scene.objects = fake_bpy.Objects([scene.camera, scene.objects[3], *objects])
    scene.render.resolution_percentage = 100
    return bpy


class Case(unittest.TestCase):
    def run_geometry(self, shapes, args=None, grazing=(), extra=()):
        """project_to_camera over `shapes` (name -> Shape or a reason) with the fake world."""
        objects = [Mesh(name) for name in shapes] + list(extra)
        fake = scene_of(*objects)
        self.world = FakeWorld(shapes, grazing)
        with mock.patch.object(geometry, "World", lambda *a: self.world):
            self.bpy = fake
            return geometry.project(fake, args or {})

    def pair(self, report, a, b):
        return next((p for p in report["pairs"] if {p["a"], p["b"]} == {a, b}), None)

    def support(self, report, name):
        return next(s for s in report["supports"] if s["name"] == name)

    def frame(self, report, name):
        return next(o for o in report["objects"] if o["name"] == name)


class Framing(Case):
    def framed(self, lo, hi, args=None):
        report = self.run_geometry({"X": box("X", lo, hi)}, args)
        return self.frame(report, "X")

    def test_a_mesh_wholly_inside_with_room_to_spare(self):
        entry = self.framed((-1, 0, 3), (1, 1, 5))
        self.assertEqual(entry["frame"], "inside")
        self.assertEqual(entry["margins"], {"left": 0.4, "right": 0.4, "bottom": 0.3333, "top": 0.3333})
        self.assertEqual(entry["bbox"], [0.4, 0.3333, 0.6, 0.6667])

    def test_inside_but_within_the_margin_is_tight(self):
        entry = self.framed((4.9, 0, 3), (4.95, 1, 5))
        self.assertEqual(entry["frame"], "tight")      # 0.99 across: 0.01 from the right edge, margin 0.02
        self.assertEqual(self.framed((4.9, 0, 3), (4.95, 1, 5), {"margin": 0.005})["frame"], "inside")

    def test_a_vertex_over_the_edge_cuts_it(self):
        entry = self.framed((4, 0, 3), (6, 1, 5))
        self.assertEqual(entry["frame"], "cut")
        self.assertLess(entry["margins"]["right"], 0)

    def test_nothing_in_view_is_outside_and_behind_the_camera_too(self):
        self.assertEqual(self.framed((20, 0, 3), (21, 1, 5))["frame"], "outside")
        behind = self.framed((-1, -30, 3), (1, -20, 5))
        self.assertEqual((behind["frame"], behind["reason"]), ("outside", "behind the camera"))

    def test_crossing_the_camera_plane_is_unsupported_not_a_pass(self):
        entry = self.framed((-1, -12, 3), (1, 0, 5))
        self.assertEqual((entry["frame"], entry["reason"]), ("unsupported", "it crosses the camera plane"))

    def test_no_vertex_in_view_but_the_box_overlaps_the_frame_is_unsupported(self):
        entry = self.framed((-9, 0, -9), (9, 1, 9))     # a huge plane around the whole view
        self.assertEqual(entry["frame"], "unsupported")
        self.assertIn("overlaps the frame", entry["reason"])

    def test_beyond_the_far_clip_is_not_in_view(self):
        entry = self.framed((-1, 150, 3), (1, 160, 5))
        self.assertEqual(entry["frame"], "outside")

    def test_margin_and_tolerance_are_reported_and_validated(self):
        report = self.run_geometry({"X": box("X", (0, 0, 0), (1, 1, 1))}, {"margin": 0.1, "tolerance": 0.5})
        self.assertEqual((report["margin"], report["tolerance"]), (0.1, 0.5))
        self.assertIn("gravity is -Z", report["assumptions"])
        for bad in ({"margin": 0}, {"tolerance": -1}, {"margin": True}, {"tolerance": "x"}):
            self.assertIn("positive number", self.run_geometry({"X": box("X", (0, 0, 0), (1, 1, 1))}, bad)["error"])


class Pairs(Case):
    def verdicts(self, shapes, **kw):
        report = self.run_geometry(shapes, **kw)
        return report, report["pairs"]

    def test_separate_objects_list_no_pair(self):
        _, pairs = self.verdicts({"A": box("A", (0, 0, 0), (1, 1, 1)), "D": box("D", (4, 0, 0), (5, 1, 1))})
        self.assertEqual(pairs, [])
        self.assertEqual(self.world.built, [])          # boxes apart: no tree is even built

    def test_a_resting_contact_is_unsupported_where_a_bounding_box_would_flag_clipping(self):
        report, _ = self.verdicts({"A": box("A", (0, 0, 0), (1, 1, 1)), "B": box("B", (0, 0, 1), (1, 1, 2))})
        verdict = self.pair(report, "A", "B")
        self.assertEqual(verdict["verdict"], "unsupported")
        self.assertIn("resting contact", verdict["reason"])

    def test_penetration_is_measured_even_when_the_footprints_coincide(self):
        report, _ = self.verdicts({"A": box("A", (0, 0, 0), (1, 1, 1)), "C": box("C", (0, 0, 0.9), (1, 1, 1.9))})
        verdict = self.pair(report, "A", "C")
        self.assertEqual((verdict["verdict"], verdict["depth"]), ("intersecting", 0.1))

    def test_a_penetration_under_the_tolerance_is_not_clipping(self):
        report, _ = self.verdicts({"A": box("A", (0, 0, 0), (1, 1, 1)), "C": box("C", (0, 0, 0.998), (1, 1, 2))})
        self.assertEqual(self.pair(report, "A", "C")["verdict"], "unsupported")

    def test_an_open_shell_cutting_through_is_still_measured(self):
        report, _ = self.verdicts({"A": box("A", (0, 0, 0), (1, 1, 1)),
                                   "Shell": box("Shell", (0.2, 0.2, 0.7), (0.8, 0.8, 1.9), open_top=True)})
        self.assertEqual(self.pair(report, "A", "Shell")["verdict"], "intersecting")

    def test_one_object_inside_another_is_contained(self):
        report, _ = self.verdicts({"Big": box("Big", (-2, -2, -2), (2, 2, 2)),
                                   "Small": box("Small", (-0.5, -0.5, -0.5), (0.5, 0.5, 0.5))})
        self.assertEqual(self.pair(report, "Big", "Small")["verdict"], "contained")

    def test_boxes_that_meet_without_touching_are_clear(self):
        """An L of two bars: the boxes overlap, the surfaces do not, and neither contains the other."""
        report, _ = self.verdicts({"Bar": box("Bar", (0, 0, 0), (4, 1, 1)),
                                   "Post": box("Post", (3, 0, 1.0005), (4, 1, 4))})
        self.assertEqual(self.pair(report, "Bar", "Post")["verdict"], "clear")

    def test_a_shell_with_an_open_edge_inside_a_box_cannot_be_judged(self):
        report, _ = self.verdicts({"Box": box("Box", (-2, -2, -2), (2, 2, 2)),
                                   "Shell": box("Shell", (-.5, -.5, -.5), (.5, .5, .5), open_top=True)})
        verdict = self.pair(report, "Box", "Shell")
        self.assertEqual(verdict["verdict"], "unsupported")
        self.assertIn("open", verdict["reason"])

    def test_doubtful_ray_counts_are_unsupported_not_clear(self):
        report, _ = self.verdicts({"Big": box("Big", (-2, -2, -2), (2, 2, 2)),
                                   "Small": box("Small", (-.5, -.5, -.5), (.5, .5, .5))}, grazing=["Big"])
        verdict = self.pair(report, "Big", "Small")
        self.assertEqual(verdict["verdict"], "unsupported")
        self.assertIn("grazing", verdict["reason"])

    def test_pairs_are_capped_and_the_report_says_when_it_was_trimmed(self):
        shapes = {f"O{i}": box(f"O{i}", (0, 0, i * 0.01), (1, 1, 1 + i * 0.01)) for i in range(12)}
        report = self.run_geometry(shapes)
        self.assertLessEqual(len(json.dumps(report)), geometry.CAP)
        small = geometry.fit({"objects": [{"n": "x" * 200}] * 100, "pairs": [], "skipped": [], "supports": []})
        self.assertTrue(small["truncated"])
        self.assertLess(len(small["objects"]), 100)
        self.assertLessEqual(len(json.dumps(small)), geometry.CAP)


class Limits(Case):
    """Every bounded list says what it left out; nothing is cut in silence."""

    def test_pairs_beyond_the_cap_are_counted_worst_first_with_their_verdicts(self):
        shapes = {f"O{i}": box(f"O{i}", (0, 0, i * 0.01), (1, 1, 1 + i * 0.01)) for i in range(12)}
        report = self.run_geometry(shapes)
        self.assertEqual(len(report["pairs"]), geometry.MAX_PAIRS)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["omitted"]["pairs"], {"count": 66 - geometry.MAX_PAIRS,
                                                      "verdicts": {"intersecting": 66 - geometry.MAX_PAIRS}})

    def test_a_cut_keeps_the_serious_pairs_and_loses_the_clear_ones(self):
        shapes = {f"O{i}": box(f"O{i}", (i * 3, 0, 0), (i * 3 + 1, 1, 1)) for i in range(10)}
        shapes["A"], shapes["B"] = box("A", (0, 5, 0), (1, 6, 1)), box("B", (0.5, 5.5, 0.5), (1.5, 6.5, 1.5))
        with mock.patch.object(geometry, "MAX_PAIRS", 1):
            report = self.run_geometry(shapes)
        self.assertEqual([p["verdict"] for p in report["pairs"]], ["intersecting"])
        self.assertNotIn("omitted", report)

    def test_supports_beyond_the_cap_leave_an_unsupported_entry_naming_the_rest(self):
        shapes = {"Table": box("Table", (0, 0, 0), (4, 4, 1))}
        wanted = {f"Thing{i}": "Table" for i in range(geometry.MAX_SUPPORTS + 3)}
        report = self.run_geometry(shapes, {"supports": wanted})
        last = report["supports"][-1]
        self.assertEqual((last["name"], last["verdict"]), ("3 more supports", "unsupported"))
        self.assertEqual(len(report["supports"]), geometry.MAX_SUPPORTS + 1)

    def test_skipped_beyond_the_listing_are_counted(self):
        extra = [Mesh(f"Hidden{i}", hidden=True) for i in range(geometry.LISTED + 4)]
        report = self.run_geometry({}, extra=extra)
        self.assertEqual(len(report["skipped"]), geometry.LISTED)
        self.assertEqual(report["omitted"]["skipped"], {"count": 4, "verdicts": {"unsupported": 4}})

    def test_trimming_the_report_to_fit_records_what_it_dropped(self):
        objects = [{"name": "x" * 200, "frame": "inside"}] * 100
        small = geometry.fit({"objects": objects, "pairs": [], "skipped": [], "supports": []})
        self.assertLessEqual(len(json.dumps(small)), geometry.CAP)
        self.assertEqual(small["omitted"]["objects"]["count"], 100 - len(small["objects"]))
        self.assertEqual(small["omitted"]["objects"]["verdicts"], {"inside": 100 - len(small["objects"])})

    def test_one_oversized_item_ends_the_trimming_instead_of_looping(self):
        big = {"name": "x" * (geometry.CAP * 2), "verdict": "unsupported"}
        report = geometry.fit({"objects": [], "pairs": [], "skipped": [], "supports": [big]})
        self.assertEqual(report["supports"], [])
        self.assertEqual(report["omitted"]["supports"]["count"], 1)
        self.assertTrue(report["truncated"])

    def test_names_echoed_from_the_request_are_shortened_so_the_report_stays_whole(self):
        shapes = {"Table": box("Table", (0, 0, 0), (4, 4, 1))}
        report = self.run_geometry(shapes, {"supports": {"n" * 15000: "S" * 15000}})
        self.assertLessEqual(len(json.dumps(report)), geometry.CAP)
        entry = report["supports"][0]
        self.assertEqual(entry["verdict"], "unsupported")
        self.assertLessEqual(len(entry["name"]), geometry.NAME)
        self.assertNotIn("omitted", report)
        self.assertLessEqual(len(self.run_geometry(shapes, {"camera": "c" * 5000})["error"]), 400)


class Supports(Case):
    def table(self, *extra, **more):
        shapes = {"S": box("S", (-1, -1, 0), (1, 1, 1)), **{s.name: s for s in extra}}
        return self.run_geometry(shapes, {"supports": {s.name: "S" for s in extra}, **more})

    def test_a_cube_resting_flat_is_supported(self):
        verdict = self.support(self.table(box("B", (-.5, -.5, 1), (.5, .5, 2))), "B")
        self.assertEqual(verdict["verdict"], "supported")
        self.assertLessEqual(abs(verdict["gap"]), 0.005)

    def test_hovering_over_an_off_centre_support_is_floating_with_the_measured_gap(self):
        verdict = self.support(self.table(box("F", (0.2, -.5, 1.3), (1.2, .5, 2.3))), "F")
        self.assertEqual((verdict["verdict"], verdict["gap"], verdict["support"]), ("floating", 0.3, "S"))

    def test_not_above_the_named_support_is_unsupported(self):
        verdict = self.support(self.table(box("H", (3, -.5, 1), (4, .5, 2))), "H")
        self.assertEqual(verdict["verdict"], "unsupported")
        self.assertIn("none of the object's lowest points", verdict["reason"])

    def test_sinking_into_the_support_is_unsupported(self):
        verdict = self.support(self.table(box("P", (-.5, -.5, 0.8), (.5, .5, 1.8))), "P")
        self.assertEqual(verdict["verdict"], "unsupported")
        self.assertIn("sinks", verdict["reason"])

    def test_an_object_wholly_inside_the_support_is_unsupported(self):
        verdict = self.support(self.table(box("P", (-.5, -.5, 0.2), (.5, .5, 0.9))), "P")
        self.assertEqual(verdict["verdict"], "unsupported")
        self.assertIn("sinks", verdict["reason"])

    def test_sinking_a_hair_under_the_surface_is_unsupported_too(self):
        verdict = self.support(self.table(box("P", (-.5, -.5, 0.99), (.5, .5, 2))), "P")
        self.assertEqual(verdict["verdict"], "unsupported")
        self.assertIn("sinks", verdict["reason"])

    def test_a_cube_balanced_on_an_edge_is_leaning_not_supported(self):
        tilted = box("L", (-.5, -.5, 1), (.5, .5, 2), turn=math.radians(45))
        shift = 1 - min(v[2] for v in tilted.verts)
        tilted.verts = [(x, y, z + shift) for x, y, z in tilted.verts]
        verdict = self.support(self.table(tilted), "L")
        self.assertEqual(verdict["verdict"], "unsupported")
        self.assertIn("leaning", verdict["reason"])

    def test_a_support_that_is_not_a_mesh_is_named_in_the_reason(self):
        report = self.run_geometry({"B": box("B", (0, 0, 0), (1, 1, 1))}, {"supports": {"B": "Nope"}})
        self.assertIn("'Nope'", self.support(report, "B")["reason"])

    def test_supports_must_map_names_to_names(self):
        for bad in ({"A": 1}, ["A"], {1: "A"}):
            report = self.run_geometry({"A": box("A", (0, 0, 0), (1, 1, 1))}, {"supports": bad})
            self.assertIn("maps an object name", report["error"])


class Hull(unittest.TestCase):
    def test_the_hull_of_a_square_covers_its_centre_but_a_line_covers_nothing(self):
        square = [(0, 0), (2, 0), (2, 2), (0, 2), (1, 1)]
        self.assertTrue(contact.covers(square, (1, 1)))
        self.assertFalse(contact.covers(square, (3, 1)))
        self.assertFalse(contact.covers([(0, 0), (1, 1), (2, 2)], (1, 1)))
        self.assertEqual(contact.hull([(0, 0)]), [(0, 0)])

    def test_a_degenerate_triangle_does_not_straddle(self):
        flat = ((0, 0, 0), (1, 0, 0), (2, 0, 0))
        self.assertEqual(contact.straddle([(0, 0, -1), (0, 0, 1), (1, 1, 1)], flat), 0.0)


class Skipped(Case):
    def test_what_the_evaluated_mesh_does_not_show_is_unsupported_with_a_reason(self):
        shapes = {"Ok": box("Ok", (0, 0, 0), (1, 1, 1)), "Big": "70000 triangles, over the limit"}
        extra = [Mesh("Hid2", hidden=True), Mesh("Inst2", instancer="VERTS"), Mesh("Part2", particles=[1]),
                 Mesh("Nodes2", modifiers=["NODES"]), Mesh("Cur2", type="CURVE")]
        report = self.run_geometry(shapes, extra=extra)
        reasons = {s["name"]: s["reason"] for s in report["skipped"]}
        self.assertEqual(set(reasons), {"Hid2", "Inst2", "Part2", "Nodes2", "Cur2", "Big"})
        self.assertEqual(reasons["Hid2"], "hidden")
        self.assertIn("not a mesh object (curve)", reasons["Cur2"])
        self.assertIn("geometry nodes", reasons["Nodes2"])
        self.assertTrue(all(s["verdict"] == "unsupported" for s in report["skipped"]))
        self.assertEqual([o["name"] for o in report["objects"]], ["Ok"])

    def test_lights_cameras_and_empties_are_not_geometry_and_are_not_reported(self):
        report = self.run_geometry({"M": box("M", (0, 0, 0), (1, 1, 1))}, extra=[Mesh("Lamp", "LIGHT")])
        self.assertEqual(report["skipped"], [])

    def test_more_objects_than_the_cap_are_said_not_silently_dropped(self):
        shapes = {f"O{i}": box(f"O{i}", (i * 3, 0, 0), (i * 3 + 1, 1, 1)) for i in range(geometry.MAX_OBJECTS + 5)}
        report = self.run_geometry(shapes)
        self.assertIn("5 more objects", [s["name"] for s in report["skipped"]])


class Projection(Case):
    def test_the_requested_camera_and_size_are_used_and_the_scene_is_left_alone(self):
        report = self.run_geometry({"M": box("M", (0, 0, 0), (1, 1, 1))}, {"camera": "Side", "width": 100, "height": 99999})
        self.assertEqual((report["camera"], report["width"], report["height"]), ("Side", 100, 2048))
        self.assertEqual(report["adjusted"], ["height 99999 -> 2048"])
        scene = self.bpy.context.scene
        self.assertEqual((scene.camera.name, scene.render.resolution_x, scene.render.resolution_y),
                         ("Camera", 1920, 1080))

    def test_the_default_size_is_the_one_a_render_would_use_percentage_and_clamps_together(self):
        fake = scene_of()
        out = fake.context.scene.render
        out.resolution_x, out.resolution_y, out.resolution_percentage = 3840, 2160, 50
        with mock.patch.object(geometry, "World", lambda *a: FakeWorld({})):
            report = geometry.project(fake, {})
            clamped = geometry.project(fake, {"width": 4000})
        self.assertEqual((report["width"], report["height"]), (1920, 1080))
        self.assertEqual(report["adjusted"], [])
        self.assertEqual((clamped["width"], clamped["height"]), (2048, 1080))   # as `render` would clamp it
        self.assertEqual((out.resolution_x, out.resolution_y, out.resolution_percentage), (3840, 2160, 50))
        scene = fake.context.scene
        scene.render.resolution_percentage = 100
        out.resolution_x, out.resolution_y = 6000, 3000
        with mock.patch.object(geometry, "World", lambda *a: FakeWorld({})):
            big = geometry.project(fake, {})
        self.assertEqual((big["width"], big["height"]), (2048, 2048))

    def test_an_unknown_camera_is_an_error_naming_the_cameras(self):
        report = self.run_geometry({"M": box("M", (0, 0, 0), (1, 1, 1))}, {"camera": "Nope"})
        self.assertIn("no camera named 'Nope'", report["error"])

    def test_scene_info_adds_the_projection_only_when_asked(self):
        fake = scene_of()
        with mock.patch.object(geometry, "World", lambda *a: FakeWorld({})):
            plain = commands.scene_info(fake, {})
            asked = commands.scene_info(fake, {"project_to_camera": True, "detail": "objects"})
            off = commands.scene_info(fake, {"project_to_camera": "yes"})
        self.assertNotIn("projection", plain["scene"])
        self.assertNotIn("projection", off["scene"])
        self.assertEqual(asked["scene"]["projection"]["objects"], [])


class Adapter(unittest.TestCase):
    def test_reason_and_the_world_space_mesh_with_its_open_edges(self):
        self.assertIsNone(world.reason(Mesh("M")))
        self.assertEqual(world.reason(Mesh("M", type="EMPTY")), "not a mesh object (empty)")
        quad = types.SimpleNamespace(
            vertices=[types.SimpleNamespace(co=(0, 0, 0)), types.SimpleNamespace(co=(1, 0, 0)),
                      types.SimpleNamespace(co=(1, 1, 0)), types.SimpleNamespace(co=(0, 1, 0))],
            polygons=[types.SimpleNamespace(edge_keys=[(0, 1), (1, 2), (2, 3), (0, 3)])],
            loop_triangles=[types.SimpleNamespace(vertices=(0, 1, 2)), types.SimpleNamespace(vertices=(0, 2, 3))],
            transform=lambda matrix: moved.append(matrix), calc_loop_triangles=lambda: None)
        moved, cleared = [], []
        evaluated = types.SimpleNamespace(to_mesh=lambda: quad, to_mesh_clear=lambda: cleared.append(1))
        obj = Mesh("Quad")
        obj.evaluated_get = lambda depsgraph: evaluated
        fake = types.SimpleNamespace(context=types.SimpleNamespace(evaluated_depsgraph_get=lambda: "graph"))
        with mock.patch.dict("sys.modules", {"bpy_extras": types.SimpleNamespace(),
                                             "bpy_extras.object_utils": types.SimpleNamespace(
                                                 world_to_camera_view=lambda *a: (0.5, 0.5, 2.0)),
                                             "mathutils": types.SimpleNamespace(Vector=tuple),
                                             "mathutils.bvhtree": types.SimpleNamespace(BVHTree=types.SimpleNamespace(
                                                 FromPolygons=lambda verts, tris: ("tree", len(verts), len(tris))))}):
            w = world.World(fake, types.SimpleNamespace(), "camera")
            shape = w.shape(obj)
            self.assertEqual(w.view((1, 2, 3)), (0.5, 0.5, 2.0))
            self.assertEqual(w.tree(shape), ("tree", 4, 2))
        self.assertEqual((shape.closed, len(shape.tris), shape.lo, shape.hi), (False, 2, (0, 0, 0), (1, 1, 0)))
        self.assertEqual((len(moved), cleared), (1, [1]))   # transformed to world space, then released
        quad.loop_triangles = quad.loop_triangles * (world.MAX_TRIS // 2 + 1)
        self.assertIn("over the limit", w.shape(obj))
        quad.loop_triangles, quad.vertices = quad.loop_triangles[:2], []
        self.assertEqual(w.shape(obj), "no geometry")
