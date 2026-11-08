"""Contact checks between shapes: clipping (surfaces cut through each other) and floating above a support.

These are heuristics with stated assumptions (gravity is -Z, meshes are the evaluated ones, tolerance is in
scene units) and they never say "fine" for a case they cannot judge: touching surfaces, open meshes, leaning
objects and doubtful ray counts all come back `unsupported`, which the caller sends to visual review.
"""
import math

DOWN = (0.0, 0.0, -1.0)
OBLIQUE = ((0.5773, 0.3711, 0.7237), (-0.2417, 0.8123, -0.5329))  # off the axes: rays rarely graze an edge
MAX_CROSSINGS = 64
TRI_PAIRS = 5000   # intersecting triangle pairs measured per object pair
LOWEST = 200       # lowest vertices cast down for the support check
STEP = 1e-5


def unsupported(reason, **more):
    return {"verdict": "unsupported", "reason": reason, **more}


def dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def inside(tree, point):
    """True / False by crossing parity along two oblique rays, None when they disagree or never end."""
    answers = []
    for direction in OBLIQUE:
        length = math.sqrt(dot(direction, direction))
        direction = tuple(d / length for d in direction)
        origin, count = point, 0
        for _ in range(MAX_CROSSINGS):
            hit = tree.ray_cast(origin, direction)
            if hit is None or hit[0] is None:
                break
            count += 1
            origin = tuple(h + d * STEP for h, d in zip(hit[0], direction, strict=True))
        else:
            return None
        answers.append(count % 2 == 1)
    return answers[0] if answers[0] == answers[1] else None


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b, strict=True))


def straddle(corners, plane):
    """How far a triangle reaches to each side of another triangle's plane, the smaller of the two (0 when it
    stays on one side, as in a resting contact; orientation of the faces does not matter)."""
    a, b, c = plane
    u, v = sub(b, a), sub(c, a)
    normal = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    length = math.sqrt(dot(normal, normal))
    if length < 1e-12:
        return 0.0
    side = [dot(sub(p, a), normal) / length for p in corners]
    return min(max(0.0, -min(side)), max(0.0, max(side)))


def poke(a, b, pairs):
    """How deep the surfaces of two shapes cut through each other, from the triangles that intersect."""
    depth = 0.0
    for i, j in pairs[:TRI_PAIRS]:
        first, second = [a.verts[k] for k in a.tris[i]], [b.verts[k] for k in b.tris[j]]
        depth = max(depth, straddle(second, first), straddle(first, second))
    return depth


def meet(a, b, tol):
    """The boxes touch or overlap, grown by the tolerance."""
    return all(a.lo[i] - tol <= b.hi[i] and b.lo[i] - tol <= a.hi[i] for i in range(3))


def nested(a, b, tol):
    return all(b.lo[i] - tol <= a.lo[i] and a.hi[i] <= b.hi[i] + tol for i in range(3))


def pair(a, b, trees, tol):
    """Verdict for two shapes whose boxes meet: clear, intersecting, contained or unsupported."""
    ta, tb = trees(a), trees(b)
    meeting = ta.overlap(tb)
    if not meeting:
        if not (nested(a, b, tol) or nested(b, a, tol)):
            return {"verdict": "clear"}
        if not (a.closed and b.closed):
            return unsupported("one box lies inside the other and a mesh is open, so containment is unknown")
        found = (inside(tb, a.verts[0]), inside(ta, b.verts[0]))
        if None in found:
            return unsupported("containment could not be settled (grazing rays)")
        return {"verdict": "contained", "reason": "one object lies entirely inside the other"} if any(found) \
            else {"verdict": "clear"}
    depth = poke(a, b, meeting)
    if depth > tol:
        return {"verdict": "intersecting", "depth": round(depth, 4),
                "reason": "surfaces cut through each other; this may be intended (a plant in soil)"}
    return unsupported(f"surfaces touch or cross by no more than {tol} (resting contact)")


def hull(points):
    points = sorted(set(points))
    if len(points) < 3:
        return points

    def half(sequence):
        out = []
        for p in sequence:
            while len(out) >= 2 and (out[-1][0] - out[-2][0]) * (p[1] - out[-2][1]) \
                    - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0]) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]

    return half(points) + half(points[::-1])


def covers(points, centre):
    """The 2D point lies inside the convex hull of the points (a hull with fewer than 3 corners covers nothing)."""
    corners = hull(points)
    if len(corners) < 3:
        return False
    return all((b[0] - a[0]) * (centre[1] - a[1]) - (b[1] - a[1]) * (centre[0] - a[0]) >= 0
               for a, b in zip(corners, corners[1:] + corners[:1], strict=True))


def floating(shape, support, tree, tol):
    """Is `shape` resting on `support` (looking straight down from its lowest points) or hovering above it."""
    low = min(v[2] for v in shape.verts)
    lowest = [v for v in shape.verts if v[2] <= low + tol][:LOWEST]
    lift, gaps, touching = 2 * tol, [], []
    for point in lowest:
        hit = tree.ray_cast((point[0], point[1], point[2] + lift), DOWN)
        if hit is None or hit[0] is None:
            continue
        gap = hit[3] - lift
        if dot(hit[1], DOWN) > 0 or gap < -tol:   # seen from inside the support, or below its surface
            return unsupported("the object sinks into the support (a lowest point is below its surface)",
                               support=support)
        gaps.append(gap)
        if gap <= tol:
            touching.append(point)
    if not gaps:
        return unsupported("none of the object's lowest points lies above the named support", support=support)
    if not touching:
        return {"verdict": "floating", "gap": round(min(gaps), 4), "support": support,
                "reason": f"the lowest points hover {round(min(gaps), 4)} above the support"}
    centre = tuple(sum(v[i] for v in shape.verts) / len(shape.verts) for i in range(2))
    if covers([(p[0], p[1]) for p in touching], centre):
        return {"verdict": "supported", "gap": round(min(gaps), 4), "support": support}
    return unsupported("it touches the support at too few points to carry its centre (leaning or edge contact)",
                       support=support)
