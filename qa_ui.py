"""Exercise the real GTK interface with a fake VPN backend, never a real tunnel."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock

# Use a private session bus and private XDG roots, never the desktop's bus/data.
# Requires a display (e.g. xvfb-run) and the distro's dbus-run-session.
if "--isolated" not in sys.argv:
    if not Path("/usr/bin/dbus-run-session").is_file():
        raise SystemExit("UI QA needs /usr/bin/dbus-run-session; no tests were run.")
    with tempfile.TemporaryDirectory(prefix="simple-vpn-qa-") as root:
        runtime = Path(root) / "runtime"
        runtime.mkdir(mode=0o700)
        environment = {**os.environ, "SIMPLE_VPN_QA_ROOT": root, "GTK_A11Y": "none", "GIO_USE_VFS": "local", "GSETTINGS_BACKEND": "memory", "GTK_USE_PORTAL": "0"}
        wayland = environment.get("WAYLAND_DISPLAY")
        if wayland and not Path(wayland).is_absolute():
            environment["WAYLAND_DISPLAY"] = str(Path(environment.get("XDG_RUNTIME_DIR", "/nonexistent")) / wayland)
        environment["XDG_RUNTIME_DIR"] = str(runtime)
        for variable, folder in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")]:
            directory = Path(root) / folder
            directory.mkdir()
            environment[variable] = str(directory)
        # No <servicedir> or standard_session_servicedirs: no portal, GVfs,
        # keyring or other desktop service can auto-start on this test bus.
        config = Path(root) / "session.conf"
        config.write_text('<busconfig><type>session</type><listen>unix:tmpdir=' + str(runtime) + '</listen><auth>EXTERNAL</auth><policy context="default"><allow user="*"/><allow send_destination="*"/><allow receive_sender="*"/><allow own="*"/></policy></busconfig>')
        if "--high-contrast" in sys.argv:
            environment["ADW_DEBUG_HIGH_CONTRAST"] = "1"
        result = subprocess.run(["/usr/bin/dbus-run-session", "--config-file", str(config), "--", "/usr/bin/python3", str(Path(__file__).resolve()), "--isolated", *sys.argv[1:]], env=environment, timeout=30)
        raise SystemExit(result.returncode)

# Optional extracted-package runtime, for checking the actual .deb payload.
if "--app-root" in sys.argv:
    runtime_root = Path(sys.argv[sys.argv.index("--app-root") + 1]).resolve()
    if not (runtime_root / "app.py").is_file():
        raise SystemExit("--app-root must contain the extracted app.py")
    sys.path.insert(0, str(runtime_root))

from gi.repository import GLib
from app import Adw, Application, Gtk, Window
from backend import Preferences, VPNError


import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--isolated", action="store_true")
parser.add_argument("--app-root")
parser.add_argument("--screenshot")
parser.add_argument("--theme", choices=["light", "dark"])
parser.add_argument("--high-contrast", action="store_true")
parser.add_argument("--connected", action="store_true", help="Preview the connected state with no VPN")
parser.add_argument("--inspect-css", action="store_true")
parser.add_argument("--inspector-output")
args = parser.parse_args()
application = Application(demo=True, screenshot=args.screenshot, preview_scheme=args.theme)
failure = []


def after_frame(widget, callback, *arguments):
    """Run once after allocation and two frame ticks; no startup delay assumption."""
    seen = False
    def tick(target, clock):
        nonlocal seen
        if target.get_mapped() and target.get_width() > 0 and target.get_height() > 0:
            if seen:
                GLib.idle_add(callback, *arguments)
                return GLib.SOURCE_REMOVE
            seen = True
        return GLib.SOURCE_CONTINUE
    widget.add_tick_callback(tick)


def when_window_ready(callback):
    window = application.get_active_window()
    if window is None:
        return GLib.SOURCE_CONTINUE
    after_frame(window, callback)
    return GLib.SOURCE_REMOVE


def check():
    try:
        window = application.get_active_window()
        assert window is not None
        assert window.status_title.get_text() == "Ready to connect"
        assert window.selected()["path"].name == "nl-443.ovpn"
        assert window.get_default_size() == (460, 700)
        assert window.get_size_request() == (360, -1)
        visible, bounds = window.action.compute_bounds(window)
        assert visible and bounds.get_y() + bounds.get_height() <= window.get_height()
        print("PasswordEntry CSS node:", window.password.get_css_name(), "; classes:", ", ".join(window.password.get_css_classes()), flush=True)
        window.show_help()
        assert isinstance(window.help_dialog, Adw.AlertDialog)
        window.help_dialog.close()
        empty = Window(application, demo=True)
        empty.demo = False
        empty.backend = MagicMock()
        empty.profiles = []
        empty.dropdown.set_model(Gtk.StringList.new([]))
        empty.load_selection()
        empty.set_status("disconnected")
        assert not empty.action.get_sensitive()
        assert empty.import_button.get_sensitive()
        assert "Import a Proton OpenVPN .ovpn" in empty.feedback.get_text()
        empty.on_connect()
        empty.backend.connect.assert_not_called()
        empty.close()
        window.demo = False
        window.backend = MagicMock()
        window.backend.snapshot.return_value = {"active": [], "legacy": False, "networking": True}
        temporary = tempfile.TemporaryDirectory()
        window.prefs = Preferences(Path(temporary.name))
        window.on_connect()
        assert window.feedback.get_visible()
        window.backend.connect.assert_not_called()
        window.username.set_text("fake-test-user")
        window.password.set_text("fake-test-password")
        window.backend.connect.return_value = "Connected to your VPN server."
        def worker(work, done):
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, exc
            done(result, error)
        window.run_worker = worker
        # Simulate the status that NetworkManager returns after activation.
        ident = window.selected()["uuid"]
        window.backend.snapshot.return_value = {"active": [{"uuid": ident, "state": 2}], "legacy": False, "networking": True}
        window.on_connect()
        assert window.status_title.get_text() == "Connected"
        assert window.action.get_label() == "Disconnect"
        assert not window.dropdown.get_sensitive()
        assert window.password.get_text() == ""
        # Exercise actual GTK close signals with a mocked backend.
        closing = Window(application, demo=True)
        closing.demo = False
        closing.backend = MagicMock()
        closing.run_worker = worker
        closing.busy = closing.connecting = True
        closing._pending_uuid = closing.selected()["uuid"]
        closing._attempt_done.clear()
        assert closing.on_close() is True
        assert closing.cancel.is_set()
        closing.backend.cancel_pending.assert_called_once_with(closing._pending_uuid)
        GLib.source_remove(closing._close_wait_source)
        closing._close_wait_source = None
        assert closing.finish_close() is True
        closing._attempt_done.set()
        assert closing.finish_close() is False
        assert closing._closed
        connected = Window(application, demo=True)
        connected.demo = False
        connected.backend = MagicMock()
        connected.connected = True
        connected.close()
        assert connected._closed
        connected.backend.disconnect.assert_not_called()
        connected.backend.cancel_pending.assert_not_called()
        assert "fake-test-password" not in window.prefs.path.read_text()
        window.backend.snapshot.return_value["active"] = []
        window.on_connect()
        assert window.action.get_label() == "Connect"
        assert window.dropdown.get_sensitive()
        window.password.set_text("fake-test-password")
        window.backend.connect.side_effect = VPNError("Authentication failed. Check your OpenVPN credentials.")
        window.on_connect()
        assert window.feedback.get_visible()
        assert "Authentication failed" in window.feedback.get_text()
        assert window.action.get_sensitive()
        assert window.password.get_text() == ""
        # External activation must never start a second connection attempt.
        before = window.backend.connect.call_count
        window.snapshot_done({"active": [{"uuid": ident, "state": 1}], "legacy": False, "networking": True}, None)
        assert "started elsewhere" in window.status_title.get_text()
        assert not window.action.get_sensitive()
        window.on_connect()
        assert window.backend.connect.call_count == before
        window.snapshot_done({"active": [], "legacy": True, "networking": True}, None)
        assert "Another VPN" in window.feedback.get_text()
        window.snapshot_done({"active": [], "legacy": False, "networking": True}, None)
        assert not window.feedback.get_visible()
        window.snapshot_done(None, GLib.Error("mock bus failure"))
        assert window.feedback.get_visible() and not window.action.get_sensitive()
        window.snapshot_done({"active": [], "legacy": False, "networking": True}, None)
        assert not window.feedback.get_visible() and window.action.get_sensitive()
        temporary.cleanup()
        print("GTK checks passed: validation, connect, disconnect, errors, password clearing, close-while-connecting and close-while-connected, external activation and status recovery.", flush=True)
    except BaseException as error:
        failure.append(error)
        import traceback
        traceback.print_exc()
    finally:
        application.quit()
    return GLib.SOURCE_REMOVE


def inspect_css():
    Gtk.Window.set_interactive_debugging(True)
    def children(root):
        result, pending = [], [root]
        while pending:
            widget = pending.pop()
            result.append(widget)
            child = widget.get_first_child()
            while child:
                pending.append(child)
                child = child.get_next_sibling()
        return result
    def capture_inspector(inspector):
        try:
            from gi.repository import Graphene
            snapshot = Gtk.Snapshot.new()
            Gtk.WidgetPaintable.new(inspector).snapshot(snapshot, inspector.get_width(), inspector.get_height())
            rectangle = Graphene.Rect().init(0, 0, inspector.get_width(), inspector.get_height())
            inspector.get_renderer().render_texture(snapshot.to_node(), rectangle).save_to_png(args.inspector_output)
            print("Inspector screenshot:", args.inspector_output, flush=True)
        except Exception as error:
            failure.append(error)
        finally:
            application.quit()
        return GLib.SOURCE_REMOVE
    def probe():
        try:
            inspector = next(w for w in Gtk.Window.list_toplevels() if "Inspector" in w.__gtype__.name)
            inspector.set_default_size(1120, 760)
            widgets = children(inspector)
            selected = False
            for widget in widgets:
                if isinstance(widget, Gtk.ColumnView) and isinstance(widget.get_model(), Gtk.SingleSelection):
                    model = widget.get_model()
                    index = 0
                    while index < min(model.get_n_items(), 1500):
                        row = model.get_item(index)
                        if not isinstance(row, Gtk.TreeListRow):
                            break
                        row.set_expanded(True)
                        if row.get_item() == application.window.password:
                            model.set_selected(index)
                            widget.emit("activate", index)
                            selected = True
                            break
                        index += 1
            assert selected, "The password widget was not found in Inspector"
            for widget in widgets:
                if isinstance(widget, Gtk.Stack):
                    pages = widget.get_pages()
                    names = [pages.get_item(i).get_name() for i in range(pages.get_n_items())]
                    for name in ("object-details", "css-nodes"):
                        if name in names:
                            widget.set_visible_child_name(name)
            print("GTK Inspector selected PasswordEntry; CSS node:", application.window.password.get_css_name(), "; classes:", ", ".join(application.window.password.get_css_classes()), flush=True)
            if args.inspector_output:
                after_frame(inspector, capture_inspector, inspector)
            else:
                application.quit()
        except Exception as error:
            failure.append(error)
            print("Inspector check failed:", type(error).__name__, flush=True)
            application.quit()
        return GLib.SOURCE_REMOVE
    GLib.idle_add(probe)
    return GLib.SOURCE_REMOVE


if args.connected:
    def prepare_connected():
        window = application.get_active_window()
        if window is None:
            return GLib.SOURCE_CONTINUE
        window.connected = True
        window.set_status("connected")
        return GLib.SOURCE_REMOVE
    GLib.idle_add(prepare_connected)

if args.inspect_css:
    GLib.idle_add(when_window_ready, inspect_css)
elif not args.screenshot:
    GLib.idle_add(when_window_ready, check)
application.run([])
if args.screenshot:
    if not Path(args.screenshot).is_file():
        failure.append(AssertionError("Screenshot was not created"))
    print("Screenshot:", args.screenshot, "; high contrast:", Adw.StyleManager.get_default().get_high_contrast(), flush=True)
if application.preview_dir is not None:
    failure.append(AssertionError("Preview temporary directory was not cleaned up"))
if failure:
    raise SystemExit(1)
