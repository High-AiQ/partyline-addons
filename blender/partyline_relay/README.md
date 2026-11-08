# Partyline Relay (Blender extension)

Lets a [Partyline](https://github.com/High-AiQ/partyline) agent run Python inside the Blender session you have
open, **only after you say yes on this computer**, one session at a time. Blender 5.0 or newer, MIT licence.

## Install

Download `partyline_relay-<version>.zip` from the CI artifacts (or build it: `python scripts/build_zip.py`), then
in Blender: Edit > Preferences > Get Extensions > the drop-down arrow > **Install from Disk**. The add-on has no
settings. It does nothing until the Partyline helper on this computer opens a session.

## What happens

1. The add-on listens on `127.0.0.1` (a random port, never a network address) and writes `blender-relay.json`
   (port, secret, pid, Blender version, open file) beside the helper's own config (`%LOCALAPPDATA%\Partyline` on
   Windows, `~/.config/partyline` elsewhere). The secret is generated once per install.
2. When an agent asks, the **helper** shows you a dialog. Only if you allow it does the helper open a session here.
   The status bar then shows **Agent <name> is connected** with an **End** button.
3. At session start the add-on saves a copy of the open file as `<name>.agent-backup.blend` beside it (an unsaved
   scene has none). Each script runs after an undo push, so one Ctrl+Z steps back over it.
4. The helper renews a 15-second lease every few seconds. If it stops (crash, kill, network), the add-on ends the
   session on its own and stops a running script at its next Python line.

## Security model

* Loopback only; every request needs `Authorization: Bearer <secret>` (constant-time compare). A request with any
  `Origin` header, or a `Host` other than `127.0.0.1:<port>`, is refused, and no CORS headers are sent, so a web page
  cannot talk to it.
* **Not a sandbox.** A script runs with your account's rights in your Blender: files, network, everything. There is
  no lexical "safe mode", because a text filter is not a security boundary. The protection is consent, the secret,
  the lease, the audit log the helper and server keep of every script, and the kill switches (this End button, the
  helper's tray menu and hotkey).
* One session and one call at a time. Time limit per call (default 20 s, max 120 s), stdout capped at 16 KB, scripts
  at 64 KB. A Python loop is stopped at its deadline; a single long C call (a render) cannot be interrupted, so the
  session is reported busy until it returns. The stop is an exception raised between lines, so a script that
  deliberately catches every `BaseException` in a loop (a bare `except:`) can outlive its deadline; the session then
  stays busy and the remedy is closing Blender. A script's own `__repr__` runs under the same deadline, while an
  error's text is built from its plain arguments and never calls the script's `__str__`.
* **Renders and screenshots** (`render.py`, `screenshot.py`): the agent may ask for an image, never for a path. Width
  and height are clamped to 16..2048, EEVEE samples to 1..64, the frame to the scene's range, and every clamp is
  reported back; the engine is Workbench (default) or EEVEE, never Cycles. The camera is selected for the render only
  and every setting is put back. The image is written to a private temp directory under a random id and handed to
  the helper once (`POST /artifact`, same secret, session and Host checks); it is deleted when fetched, when the
  session ends and when the add-on stops, and at most 8 unfetched images are kept. A render is one long C call: the
  deadline, a cancel and a killed session take effect when it returns, and the image is then discarded, not handed
  over. `screenshot` draws the 3D viewport with `gpu.types.GPUOffScreen` (needs the GPU; up to 1600 px on the long
  edge); for the whole window use Partyline's `screen_frame`.
* `scene_info` with `project_to_camera: true` adds a `projection` of geometry checks used as evidence when a render
  is checked: for each mesh, where it lies in the camera frame (`inside`, `tight`, `cut`, `outside`, with margins);
  for each pair whose boxes meet, `clear`, `intersecting` (with the depth), `contained` or `unsupported`; and, for
  `supports: {"cup": "table"}`, whether an object `supports`ed, hovers (`floating`, with the gap) or is
  `unsupported`. The camera, `width`, `height`, `margin` (a fraction of the frame, default 0.02) and `tolerance`
  (scene units, default 0.005) are reported as used. These are heuristics (gravity is -Z, evaluated meshes, a
  support is one named object): resting contact, open or leaning shapes, hidden objects, geometry nodes, instances
  and particles come back `unsupported`, never as a pass, so the render goes to visual review instead.
* Telemetry: none. Nothing leaves this computer from the add-on.

## HTTP API (for the helper)

`GET /status`; `POST /session` `{id, agent, checker}`, `/session/renew`, `/session/close`, `/call` `{session,
call_id, op: "exec"|"scene_info"|"render"|"screenshot", args, timeout}`, `/artifact` `{session, id}` (the image
bytes, once), `/cancel`. JSON in and out except the image; see `server.py`.

## Credits

The pattern of a socket thread feeding a queue that one main-thread `bpy.app.timers` callback drains, the structured
error with a traceback, and offscreen viewport capture come from
[blender-mcp](https://github.com/ahujasid/blender-mcp) by Siddharth Ahuja (MIT). This add-on shares no code with it.
