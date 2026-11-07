"""Partyline Relay: lets a consented Partyline agent run Python in this Blender session.

The Partyline helper on this computer asks you first, then talks to this add-on over 127.0.0.1 with a
per-install secret. While a session is live the status bar shows who is connected and an End button.
"""
import bpy

from . import server

service = None


class PARTYLINE_OT_end_session(bpy.types.Operator):
    bl_idname = "partyline_relay.end_session"
    bl_label = "End agent session"
    bl_description = "Disconnect the agent now and stop the script it is running"

    def execute(self, context):
        if service:
            service.relay.end("blender")
        return {"FINISHED"}


def draw_banner(self, context):
    session = service.relay.session if service else None
    if session is not None:
        row = self.layout.row()
        row.alert = True
        row.label(text=f"Agent {session.agent} is connected", icon="ERROR")
        row.operator(PARTYLINE_OT_end_session.bl_idname, text="End")


def redraw():
    manager = bpy.context.window_manager
    for window in manager.windows if manager else ():
        for area in window.screen.areas:
            area.tag_redraw()


def register():
    global service
    if bpy.app.background:  # timers never fire without a main loop, so nothing could be served
        print("partyline_relay: not starting in background mode")
        return
    bpy.utils.register_class(PARTYLINE_OT_end_session)
    bpy.types.STATUSBAR_HT_header.append(draw_banner)
    service = server.Service(bpy, __package__, redraw=redraw)
    service.start()


def unregister():
    global service
    if service:
        service.stop()
        service = None
        bpy.types.STATUSBAR_HT_header.remove(draw_banner)
        bpy.utils.unregister_class(PARTYLINE_OT_end_session)
