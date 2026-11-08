---
name: blender-scene-decomposition
description: Decompose a Blender scene brief, reference, or approved mockup into independently built pieces, brief parallel tasks, and assemble their reviewed collections into a final scene.
---

# Decompose a Blender scene

Use a shared blockout to give each piece one focused session of attention. A single
agent building a whole detailed scene can preserve the composition while missing many
local details. The captain owns composition and integration; piece tasks own detail.

Read [local tools](https://github.com/High-AiQ/partyline/blob/main/docs/local-tools.md) for the working tool contract and
[Blender Python relay](https://github.com/High-AiQ/partyline/blob/main/docs/design/blender-python-relay.md) for design context.
When the host has Blender and the deliverable is files, workers run it locally
(`blender -b <file> -P <script.py>`); no relay session is needed. The relay is for a
paired person's machine: use `blender_request` and wait for a granted session. Use
`blender_exec` for bounded modelling and file operations, `blender_scene_info` for
inspection, `blender_render` for acceptance images and `blender_screenshot` for viewport
checks (a supplement to renders). Fetch returned images with `get_media` and look at the pixels.

## Freeze the shared frame

The captain selects the approved reference and records its media id or accessible path,
dimensions, and revision. For a text brief, establish the composition in a blockout
render first. Identify visible silhouettes, interfaces, material families, and detail
priorities at the intended output resolution. Resolve ambiguous major shapes before
dispatching pieces; document assumptions about unseen surfaces.

The image-model steps below are **optional external preparation**, not part of the
add-on. When using them, follow this order and retain the exact mockup as the final
composition reference.

**Room and camera.** Run a single-image metric depth/camera model such as MoGe-2 on the
exact mockup. Use its estimates to recover the FOV and camera pose, fit floor, wall and
ceiling planes from regions that props do not cover, and measure room dimensions. Check
the metric scale against a known-size architectural anchor, record the estimated
dimensions and uncertainty, and lock the hero camera from this reconstruction.

**Prop references (optional).** If an image-to-3D reference would help a piece, never
crop the object out of the mockup. Use an image-editing model, with the mockup as
reference, to generate a fresh, clean image of **only that prop**, fully visible on a
plain white background in a three-quarter view. Remove the background, then run an
image-to-3D model such as TripoSG. The generated mesh is a **non-rendered reference
only**, for proportions: the worker builds clean, low-poly, quad-based, UV-ready geometry
over it, scaled from the metric room or a known anchor, never from the mesh. Never ship
or decimate it; keep it out of acceptance renders and delivered piece files. Do **not**
run image-to-3D on the whole mockup: its scale is not metric and a trial produced
incorrect room proportions. Recover the room from the metric depth reconstruction.

**Build the clean blockout, then iterate by overlay.** With the hero camera established,
place simple proxies using the metric room and known-size anchors. Compare against the
original mockup before adding detail or dispatching pieces: camera/framing first, then
proxy positions and silhouettes, until the main edges coincide. If a camera correction is
needed, its owner updates the reconstruction and re-locks the shared frame before
handoff. Use the same reference revision throughout. Without the optional image models,
establish camera and room from vanishing lines and measured anchors, then use the same
blockout and overlay checks.

With a granted relay session, call `blender_render(camera="Hero", reference="<line media id>",
overlay_opacity=0.5)` and inspect the media with `get_media`. Match the scene aspect to the
reference (a mismatch beyond 0.5% is refused; references are 16..2048 px per side and fit
a 700 KiB PNG). For local Blender, use the [overlay module recipe](../../partyline_relay/README.md#reference-overlays)
from a `blender -b -P` script; select `proxies` for flat translucent colours and outlines.
Keep overlays in a temp directory or on the line, and inspect every iteration.

An overlay constrains **2D silhouettes, not depth along the view axis**. Use known-size
anchors such as door width, floor tiles and grates to measure lengths running away from
the camera. AI mockups can be perspective-inconsistent: match the main features, document
the anchors used, and report remaining pixel/scale residuals instead of distorting all
geometry to force a perfect fit. Pieces judged from an unmatched camera drift.

One named owner edits `master.blend`. It owns:

- The locked hero camera: world transform, lens or orthographic scale, sensor fit,
  shift, clipping, frame, output aspect ratio, and any depth-of-field settings.
- Metric units with `scale_length = 1`: **1 BU = 1 m**, Z up, a documented origin,
  floor height and wall planes.
- World, lighting rig, render settings, color management and the overall look.
  Calibrate the rig early to the reference's mean luminance (measure both renders);
  pieces judge colour at that exposure. A placeholder two stops hot misled every piece.
- A blockout proxy for every piece, each in its own collection named exactly after
  that piece. Keep shared cameras and rig in separate master-owned collections.

Size each proxy from the anchors above, not by eye; workers fill whatever box they get.
Put silhouette architecture (chamfers, soffits, bulkheads, beams) in the blockout so
neighbours such as cable runs are built against it; adding one late forces a rebuild.

Render and approve this blockout from the hero camera. Record each proxy's world-space
axis-aligned bounding box as `min (x, y, z), max (x, y, z)` in metres, plus attachment
planes and tolerances. Preserve those measurements in the handoff before replacement.
Use `blender_scene_info` to inspect the objects; if the installed relay omits bounds,
use `blender_exec` to transform evaluated mesh bounds by each object's `matrix_world`
and union them per collection. Exclude cameras and lights from geometry bounds.

Freeze a master revision for all workers. Changes to the camera, proxy extents or
interfaces go through its owner, who updates affected briefs and tasks together.

## Slice and assign

Slice by spatial region and asset family: architecture, door, window and exterior view,
each prop cluster, cables and trays, decals and grime, and lighting. Prefer boundaries
with explicit contact planes. Split a piece if it needs several unrelated modelling
decisions; group small objects whose arrangement is the detail being judged.

Create one task per piece and run independent tasks in parallel. Modest/cheaper
implementers can handle tightly bounded asset briefs. Hard pieces (dense cable runs,
architectural shells, hero props) need stronger implementers. After two weak rounds, or
one far-off round, restaff with a stronger model and hand over the old script and review
notes. A piece whose review blocker list keeps growing (text objects exploding the
triangle count, UI floating off its screen, detached parts) is often faster rebuilt from
a tight brief, in parallel, than patched item by item; convert display text to textures,
not text geometry. Give every task the same master revision, its own output path, and the
brief below. Workers never edit master or a neighbour's file. Workers with local Blender run in
parallel; relay tasks need separate permitted sessions/devices, since a shared relay
grant belongs to one agent and accepts one call at a time (with one session, serialize
execution through its holder while workers prepare scripts and briefs). Acquire any
declared Blender resource first, following [resource leases](https://github.com/High-AiQ/partyline/blob/main/docs/hierarchy.md#shared-resource-leases).

Express mode (no task review and no code gates) is suitable for asset pieces when enabled.
In either mode, captain-reviewed renders gate the scene merge; express mode does not
waive that check.

### Worked example: industrial sci-fi airlock

Assume Z=0 is the floor and Y=3 is the rear wall. The hero camera faces the sliding
door on that wall, with a planet visible through a nearby window. These are candidate
tasks; measure their actual bounds from the approved blockout before dispatch.

| Piece / owner | Scope and interfaces | Reference crop / detail priority |
| --- | --- | --- |
| `shell` | Floor, ceiling, walls; reserve door and window openings at agreed planes | Whole room: silhouette, panel spacing and openings |
| `door` | Sliding leaves, frame, tracks and controls; fit shell opening and floor threshold | Door crop: seams, thickness, frame and controls |
| `window_view` | Window frame, glass and planet/exterior geometry; fit shell aperture, keep exterior behind it | Window crop: planet position, glass and frame profile |
| `workbench_props` | Bench and its arranged tools/containers; feet meet Z=0, rear edge meets assigned wall plane | Bench crop: silhouette and prop arrangement |
| `cart` | Cart, wheels and contents; floor contact and aisle clearance | Cart crop: wheels, rails, shelf spacing and contents |
| `crate` | Crate and its labels; floor contact and clearance from cart | Crate crop: lid, corner fittings and markings |
| `cable_trays` | Trays, brackets and cable runs; terminate at named shell/door attachment points | Upper-wall crop: route, brackets and cable density |
| `decals_grime` | Localized labels, wear and dirt overlays on named accepted surfaces | Surface crops: scale, placement and wear density |
| Lighting, master owner | World and rig in master; coordinate named practical emitters with pieces | Full frame: light direction, contrast and color |

The shell owns the wall around the door; the door owns its frame and leaves. The window
task owns the view through its aperture, not adjacent wall panels. The workbench task
owns its prop arrangement, not the cart or crate. Set cable attachment points before
parallel work. Do the decals/grime pass after target geometry is accepted (see below):
reference it read-only and keep overlays in that piece's collection; return surface
material edits to the surface owner rather than silently duplicating it. Lighting is an
integration task on master, not another worker editing master in parallel.

## Surfaces, grime and materials

Procedural noise alone gave soft, uniform blotches that never matched the reference's
grime; surface detail needs image maps.

- Generate tileable 2K maps with an open-licence image-generation model: colour overlay,
  drip mask, edge-wear/chip mask, roughness variation, height/normal. Record provenance
  (prompt, seed, hash, licence) next to the maps.
- Build one reusable overlay node group per surface family (painted wall, bare metal)
  that multiplies over each material's existing base colour, roughness and normal, applied
  by a re-runnable script. Use world-space triplanar projection where the shell has only
  box UVs. Replace a material's old blotch or rust layers rather than multiplying new
  grime over them.
- Grime follows geometry and physics. Drips start at real geometry: long upward AO under
  lips, seams and panel tops. On vertical walls gate masks by the vertical weight
  `1 - |N.z|`; a mask multiplied by the horizontal-surface weight silently vanished on
  every wall. Painted walls get dirt, soot and oil runs; orange rust only on bare metal
  (chipped rims, bolt heads, edges).
- Judge drip length at real scale from the hero camera: 30-70 cm runs read as stripes,
  8-20 cm right. Decide drips versus rust per wall with the person.
- Diagnose before retuning a material that "looks too light". Volumetric haze lifts dark
  albedo (about +0.08 sRGB in one build) and sheen or specular lobes grey out dark fabric.
  Render diffuse-only and per-light passes and measure patch means first. Use light
  linking to exclude one object from one practical light instead of dimming the light for
  everything. Change one variable per round.

## Piece file contract

- Deliver `pieces/<name>.blend` with **exactly one top-level collection**, `<name>`.
  Nested collections are allowed. Include all owned objects and dependencies there.
- Model in **WORLD coordinates matching the proxy**. Preserve placement, orientation
  and scale; do not recenter at the origin or add a merge-time offset. Any collection
  instance offset must be zero so linking the collection introduces no translation.
- Prefix every owned object and material name with `<name>_`; prefix other owned
  datablocks too when practical. Pack textures or use relative paths within the piece's
  delivered asset directory. No worker-specific absolute paths or missing external assets.
- Copy the locked master camera into the piece collection as `<name>_hero_check` for
  checking. Preserve its world transform **and projection settings**, baking any
  master-dependent constraints into the copy. Copying avoids a library dependency back
  to master when master later links the piece. This camera never replaces master's camera.
- World, lighting and overall look belong to master. A piece may own practical lights
  or emissive panels only when its brief names them and their intended contribution.
  Do not add compensating fill lights or change exposure to make a piece pass.

Keep neighbouring proxies, master rig and other preview-only context out of the delivered
piece file. Assemble a temporary preview from a copy of the frozen master, replace only
this piece's proxy with its linked collection, and leave other proxies visible; the master
owner can do this for a worker. Never save that preview over master or deliver it.

## Brief every piece

Record all of these before assignment:

```text
Piece / owner / output: <name>, task owner, pieces/<name>.blend
Master revision / reference: frozen master path or id; reference id, dimensions, revision
Scope: owned geometry, materials, details and named practical lights (or none)
Bounds: proxy world min (x, y, z), max (x, y, z), metres
Real size: key dimensions (run length, top height, depth); asset fills or fits within box
Looks like: "what good looks like" paragraph from the reference crop: shapes,
            materials, wear, colours
Interfaces: contact planes, attachment points, clearance and numerical tolerances
Exclusions: neighbouring pieces and shared camera/world/rig changes
Budget: evaluated triangle cap; texture count, maximum dimensions and total memory
Reference crop: pixel rectangle (left, top, right, bottom), origin at image top-left
Acceptance: full hero-camera render with other proxies visible, plus one close-up;
            named visual features to match, contact/bounds checks and budget report
Handoff: piece path/revision, dependencies, images incl. a side-by-side of render vs
         reference crop, measured bounds, remaining limitations
```

For example, a `cart` brief could specify proxy min `(1.6, -0.8, 0.0)` and max
`(2.6, 0.0, 1.1)` m; wheel contact at Z=0 within 0.005 m; at least 0.10 m clearance
from the crate proxy; two shelves, rails, four wheels and the visible contents; no floor,
crate, bench or lighting; 40,000 evaluated triangles, two 2048² RGBA textures, 64 MiB
decoded texture memory including mipmaps; crop `(1040, 490, 1510, 870)` of an
illustrative 1600×900 reference (use the actual crop). Require matching wheel silhouette,
rail thickness and shelf spacing in the hero view, a close-up resolving joints and
contents, and no unnamed emitters. Without real sizes, a 4.5 m × 0.95 m bench slot gave
every modeller a too-long, too-low bench: state the true run, top height and depth.

## Review, replace, and finish

1. Inspect each deliverable's collection structure, prefixes, asset paths, evaluated
   geometry budget and units with `blender_scene_info` and targeted `blender_exec`.
   Reopen the delivered file to confirm it works independently of the worker's session.
2. Make the temporary context preview. Use `blender_render` with the locked hero camera
   and master look, then a close-up camera without changing the hero camera. Workbench
   suits geometry checks; use EEVEE for material and lighting review. Record returned
   settings/clamps, keep the reference aspect ratio, and inspect both images. Repeat the
   reference overlay on the hero preview and compare the assigned pixel rectangle with
   the matching reference crop. Captain approval covers the named crop, silhouette,
   detail, materials and interfaces.
3. The master owner replaces the approved proxy collection with the piece collection:
   **link for iteration**. Remove/unlink the proxy from the render scene; do not render
   both. Use the saved proxy bounds as the baseline. Keep the inserted collection in
   world coordinates with no move, rotation, scale compensation or instance offset.
   Exclude the piece's checking camera from assembly use and retain master's active camera.
   Keep integration re-runnable: one script swaps a single piece in by name and
   re-applies the integration fixes and the rig.
4. Use `blender_scene_info` to check scale and placement against recorded proxy bounds
   and inspect possible overlaps. Use targeted `blender_exec` for bounds or contact tests
   the relay does not expose. Bounding-box intersections are candidates, not proof of
   penetration: inspect contacts, seams and clearances in close-ups and
   `blender_screenshot`. Check practical lights for doubled illumination.
5. Render the assembled scene from the hero camera and compare the full reference and
   named crops at matching aspect ratio. Review in order: camera and composition first,
   then light, then materials; also scale, silhouettes and local details. Keep the
   grade neutral and make warmth or coolness local to the lights; a global cast flattens
   the palette. Reopen a failing piece **alone**, with concrete render findings; keep
   accepted neighbours fixed. Global camera/look issues go to the master owner. Replace
   revised links and repeat the affected checks and final render. Every requested change
   must be obvious in the hero render: each round delivers a before/after side by side and
   numbers (patch means, lengths, hero mean exposure), not just "adjusted".
6. For the final deliverable, **append and make local** the accepted collections and
   their dependencies, replacing the iteration links rather than adding duplicate copies.
   Remove obsolete proxy datablocks that would cause collection-name suffixes, and
   preview-only checking cameras. Pack every image and confirm there are no linked
   libraries. Copy the `.blend` alone into an empty folder and render a low-sample frame:
   no missing files, no pink, and a match to the approved linked render. Ship a README
   with the active camera, render settings (engine, samples, denoiser, view transform,
   compositor), the piece list, texture provenance and a mockup-versus-final side by side.

Keep a manifest of master revision, piece paths/revisions, saved proxy bounds, briefs,
render references and captain decisions so one piece can be rebuilt without rediscovery.
Store final `.blend` files and dependencies in the intended durable asset location and
verify read-back before cleanup. This public repository receives only text instructions
or source snippets: **commit no images, textures, renders or `.blend` files**. Upload
review images to the line or a temp directory; share host-relative media URLs.
