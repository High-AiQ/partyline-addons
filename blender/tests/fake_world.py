"""A stand-in for `world.World`: boxes as meshes, a pinhole camera and a brute-force BVH, no Blender needed.

`FakeTree.overlap` mirrors what the real `BVHTree` was measured to do in Blender 5.2.2: triangles that cross
are reported, and so are coplanar triangles that touch (a resting contact gives two pairs).
"""
import math

from partyline_relay.world import Shape

EPS = 1e-9


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b, strict=True))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


FACES = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]


def box(name, lo, hi, turn=0.0, open_top=False):
    """A closed box (12 triangles) between two corners, optionally turned about Y through its centre."""
    centre = tuple((a + b) / 2 for a, b in zip(lo, hi, strict=True))
    verts = []
    for x in (lo[0], hi[0]):
        for y in (lo[1], hi[1]):
            for z in (lo[2], hi[2]):
                dx, dz = x - centre[0], z - centre[2]
                verts.append((centre[0] + dx * math.cos(turn) + dz * math.sin(turn), y,
                              centre[2] - dx * math.sin(turn) + dz * math.cos(turn)))
    faces = FACES[:5] if open_top else FACES
    tris = [t for a, b, c, d in faces for t in ((a, b, c), (a, c, d))]
    return Shape(name, verts, tris, closed=not open_top)


def hit_triangle(origin, direction, tri):
    """Moller-Trumbore: distance along the ray and the triangle's normal, or None."""
    a, b, c = tri
    e1, e2 = sub(b, a), sub(c, a)
    p = cross(direction, e2)
    det = dot(e1, p)
    if abs(det) < EPS:
        return None
    t0 = sub(origin, a)
    u = dot(t0, p) / det
    q = cross(t0, e1)
    v = dot(direction, q) / det
    if u < 0 or v < 0 or u + v > 1:
        return None
    t = dot(e2, q) / det
    return (t, cross(e1, e2)) if t > EPS else None


def segment_hits(p, q, tri):
    direction = sub(q, p)
    length = math.sqrt(dot(direction, direction))
    hit = hit_triangle(p, tuple(d / length for d in direction), tri)
    return hit is not None and hit[0] <= length + EPS


def coplanar(a, b):
    n = cross(sub(a[1], a[0]), sub(a[2], a[0]))
    return all(abs(dot(sub(p, a[0]), n)) < EPS for p in b)


def boxes_meet(a, b):
    lo = [min(v[i] for v in a) for i in range(3)], [min(v[i] for v in b) for i in range(3)]
    hi = [max(v[i] for v in a) for i in range(3)], [max(v[i] for v in b) for i in range(3)]
    return all(lo[0][i] <= hi[1][i] + EPS and lo[1][i] <= hi[0][i] + EPS for i in range(3))


class FakeTree:
    def __init__(self, shape, grazing=False):
        self.shape, self.grazing = shape, grazing
        self.corners = [[shape.verts[i] for i in tri] for tri in shape.tris]

    def overlap(self, other):
        out = []
        for i, mine in enumerate(self.corners):
            for j, theirs in enumerate(other.corners):
                if not boxes_meet(mine, theirs):
                    continue
                edges = [(mine[k], mine[(k + 1) % 3]) for k in range(3)]
                others = [(theirs[k], theirs[(k + 1) % 3]) for k in range(3)]
                if (any(segment_hits(p, q, theirs) for p, q in edges)
                        or any(segment_hits(p, q, mine) for p, q in others)
                        or (coplanar(mine, theirs) and coplanar(theirs, mine))):
                    out.append((i, j))
        return out

    def ray_cast(self, origin, direction, distance=None):
        if self.grazing:   # a ray that never leaves the mesh: its crossing count cannot be trusted
            return (origin, (0, 0, 1), 0, 0.0)
        best = None
        for tri in self.corners:
            hit = hit_triangle(origin, direction, tri)
            if hit and (best is None or hit[0] < best[0]):
                best = hit
        if best is None:
            return (None, None, None, None)
        point = tuple(o + d * best[0] for o, d in zip(origin, direction, strict=True))
        normal = best[1]
        length = math.sqrt(dot(normal, normal))
        return (point, tuple(n / length for n in normal), 0, best[0])


class FakeWorld:
    """Shapes by object name (or a plain-words reason); a pinhole at (0, -10, 4) looking along +Y."""

    def __init__(self, shapes, grazing=()):
        self.shapes, self.grazing = shapes, set(grazing)
        self.built = []

    def shape(self, obj):
        return self.shapes[obj.name]

    def tree(self, shape):
        self.built.append(shape.name)
        return FakeTree(shape, shape.name in self.grazing)

    def view(self, point):
        depth = point[1] + 10
        return (0.5 + point[0] / 10, 0.5 + (point[2] - 4) / 6, depth)
