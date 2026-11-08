"""Scene geometry as plain data: world-space meshes, BVH trees and the camera, the only part of the geometry
checks that touches `bpy` and `mathutils`.

`geometry.py` and `contact.py` take a `World` and decide on `Shape`s and trees, so their decisions are tested
with a fake world and the real API is exercised only here (and in a real Blender, see the README).
"""
from dataclasses import dataclass

MAX_TRIS = 50000   # beyond this a mesh is not analysed: Python loops over every vertex


@dataclass
class Shape:
    name: str
    verts: list    # world-space (x, y, z)
    tris: list     # index triples
    closed: bool   # every edge is shared by exactly two faces

    def __post_init__(self):
        self.lo = tuple(min(v[i] for v in self.verts) for i in range(3))
        self.hi = tuple(max(v[i] for v in self.verts) for i in range(3))


def reason(obj):
    """Why an object cannot be analysed from its evaluated mesh, or None when it can."""
    if obj.type != "MESH":
        return f"not a mesh object ({obj.type.lower()})"
    if not obj.visible_get():
        return "hidden"
    if obj.instance_type != "NONE" or obj.particle_systems:
        return "instances or particles are not part of the evaluated mesh"
    if any(m.type in ("NODES", "PARTICLE_SYSTEM") for m in obj.modifiers):
        return "geometry nodes or particles may add geometry the mesh does not show"
    return None


class World:
    def __init__(self, bpy, scene, camera):
        from bpy_extras.object_utils import world_to_camera_view
        from mathutils import Vector
        from mathutils.bvhtree import BVHTree
        self.bpy, self.scene, self.camera = bpy, scene, camera
        self._view, self._vector, self._bvh = world_to_camera_view, Vector, BVHTree
        self.depsgraph = bpy.context.evaluated_depsgraph_get()

    def shape(self, obj):
        """The evaluated (modifiers applied) mesh in world space, or a plain-words reason it cannot be had."""
        evaluated = obj.evaluated_get(self.depsgraph)
        mesh = evaluated.to_mesh()
        try:
            mesh.transform(obj.matrix_world)
            mesh.calc_loop_triangles()
            if len(mesh.loop_triangles) > MAX_TRIS:
                return f"{len(mesh.loop_triangles)} triangles, over the limit of {MAX_TRIS}"
            if not mesh.vertices:
                return "no geometry"
            uses = {}
            for poly in mesh.polygons:
                for key in poly.edge_keys:
                    uses[key] = uses.get(key, 0) + 1
            return Shape(obj.name, [tuple(v.co) for v in mesh.vertices],
                         [tuple(t.vertices) for t in mesh.loop_triangles],
                         bool(uses) and all(count == 2 for count in uses.values()))
        finally:
            evaluated.to_mesh_clear()

    def tree(self, shape):
        return self._bvh.FromPolygons(shape.verts, shape.tris)

    def view(self, point):
        """(x, y, depth): x and y are 0..1 across the frame, depth is the distance along the view axis."""
        return tuple(self._view(self.scene, self.camera, self._vector(point)))
