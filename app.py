#!/usr/bin/python3
"""Simple VPN: a small native desktop interface for your Proton configuration."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import signal
import tempfile
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from backend import APP_ID, Cancelled, NetworkBackend, Preferences, VPNError, debug_exception, guarded, import_profile_file, profile_uuid, server_label
from migration import migrate_legacy
from paths import data_dir, downloads_dir

VERSION = "0.1.0"

CREDENTIALS_URL = "https://account.protonvpn.com/account"  # User to verify current destination.

CSS = """
.simple-vpn .brand { font-weight: 700; letter-spacing: 1px; }
.simple-vpn .eyebrow { font-size: 11px; font-weight: 700; letter-spacing: 1px; }
.simple-vpn .status-title { font-size: 28px; font-weight: 750; }
.simple-vpn .status-orb { background: alpha(@window_fg_color, .08); color: @window_fg_color; border: 1px solid alpha(@window_fg_color, .3); border-radius: 40px; min-width: 68px; min-height: 68px; }
.simple-vpn .status-orb.pending { color: @warning_color; }
.simple-vpn .sv-card { padding: 14px; }
.simple-vpn .card-title { font-weight: 700; font-size: 14px; }
.simple-vpn .badge { background: alpha(@window_fg_color, .08); border-radius: 8px; padding: 5px 9px; font-size: 11px; font-weight: 650; }
.simple-vpn .primary { border-radius: 12px; min-height: 46px; font-weight: 750; font-size: 15px; }
.simple-vpn .primary.stop { background: @window_bg_color; color: @window_fg_color; border: 2px solid @window_fg_color; }
.simple-vpn .primary.stop:hover { background: alpha(@window_fg_color, .12); }
.simple-vpn .primary.stop:disabled { color: alpha(@window_fg_color, .45); border-color: alpha(@window_fg_color, .45); }
.simple-vpn .feedback { background: alpha(@warning_color, .12); color: @window_fg_color; border: 1px solid alpha(@warning_color, .6); border-radius: 12px; padding: 12px; }
.simple-vpn .activity { font-size: 11px; }
"""


def theme_css(dark: bool, high_contrast: bool) -> str:
    if high_contrast:
        return CSS + "\n.simple-vpn .status-orb, .simple-vpn .feedback { border: 2px solid @window_fg_color; }"
    foreground = "#91e5c3" if dark else "#176946"
    return CSS + "\n" + f".simple-vpn .status-orb.online {{ background: alpha({foreground}, .16); color: {foreground}; border-color: {foreground}; }}" + "\n" + """
.simple-vpn .primary.suggested-action { background: #91e5c3; color: #10271e; }
.simple-vpn .primary.suggested-action:hover { background: #afeed5; }
.simple-vpn .primary.suggested-action:disabled { background: alpha(@window_fg_color, .12); color: alpha(@window_fg_color, .45); }
"""


def label(text, css=None, wrap=False):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_wrap(wrap)
    if css:
        for item in css.split():
            widget.add_css_class(item)
            if item == "muted":
                widget.add_css_class("dim-label")
            elif item == "small":
                widget.add_css_class("caption")
    return widget


def box(vertical=True, spacing=10):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


class Window(Adw.ApplicationWindow):
    def __init__(self, app, demo=False, screenshot=None):
        super().__init__(application=app, title="Simple VPN")
        self.add_css_class("simple-vpn")
        self.demo = demo
        self.screenshot = screenshot
        self.set_default_size(460, 700)
        self.set_size_request(360, -1)
        self.prefs = Preferences(Path(app.preview_dir) / "config") if demo else Preferences()
        self.backend = None if demo else NetworkBackend(self.prefs)
        self.data_root = Path(app.preview_dir) / "data" if demo else data_dir()
        self.download_root = Path(app.preview_dir) / "Downloads" if demo else downloads_dir()
        if demo:
            self.prefs.data = {}
            fixture = self.data_root / "servers/nl-443.ovpn"
            fixture.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            fixture.write_text("remote demo.example.test 443\nproto tcp-client\n")
            fixture.chmod(0o600)
        self.profiles = self.discover_profiles()
        self.busy = False
        self.migrating = False
        self.connecting = False
        self.cancel = threading.Event()
        self.polling = False
        self.cancel_guard = None
        self.connected = False
        self.last_state = "disconnected"
        self.external_pending = False
        self._status_message = False
        self.events = []
        self._closing = self._closed = self._allow_close = False
        self._pending_uuid = None
        self._attempt_done = threading.Event()
        self._attempt_done.set()
        self._cleanup_done = threading.Event()
        self._cleanup_done.set()
        self._poll_source = self._close_wait_source = self._close_deadline_source = None
        self.connect("close-request", self.on_close)

        outer = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(label("SIMPLE VPN", "brand"))
        help_button = Gtk.Button(icon_name="help-about-symbolic", tooltip_text="Connection help")
        help_button.add_css_class("flat")
        help_button.connect("clicked", self.show_help)
        header.pack_start(help_button)
        outer.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_vexpand(True)
        clamp = Adw.Clamp(maximum_size=480, tightening_threshold=440)
        content = box(spacing=12)
        content.set_margin_top(8)
        content.set_margin_bottom(16)
        content.set_margin_start(26)
        content.set_margin_end(26)
        clamp.set_child(content)
        scroll.set_child(clamp)
        outer.set_content(scroll)
        self.set_content(outer)

        hero = box(spacing=9)
        hero.set_halign(Gtk.Align.CENTER)
        self.orb = Gtk.Image.new_from_icon_name("network-vpn-symbolic")
        self.orb.set_pixel_size(31)
        self.orb.add_css_class("status-orb")
        self.orb.set_halign(Gtk.Align.CENTER)
        hero.append(self.orb)
        self.status_title = label("Ready to connect", "status-title")
        self.status_title.set_halign(Gtk.Align.CENTER)
        hero.append(self.status_title)
        self.status_sub = label("Your VPN, one click away.", "muted small", True)
        self.status_sub.set_halign(Gtk.Align.CENTER)
        self.status_sub.set_justify(Gtk.Justification.CENTER)
        hero.append(self.status_sub)
        content.append(hero)

        server = box(spacing=11)
        server.add_css_class("card")
        server.add_css_class("sv-card")
        row = box(False)
        title = label("YOUR SERVER", "eyebrow")
        title.set_hexpand(True)
        row.append(title)
        row.append(label("PROTON VPN", "badge"))
        server.append(row)
        self.dropdown = Gtk.DropDown.new_from_strings([p["name"] for p in self.profiles] or ["Import a server to begin"])
        self.dropdown.set_tooltip_text("Choose a VPN configuration")
        self.dropdown.set_sensitive(bool(self.profiles))
        server.append(self.dropdown)
        row = box(False)
        self.server_detail = label("OpenVPN · TCP 443", "muted small", True)
        self.server_detail.set_hexpand(True)
        row.append(self.server_detail)
        self.import_button = Gtk.Button(label="Import .ovpn")
        self.import_button.add_css_class("flat")
        self.import_button.connect("clicked", self.import_profile)
        row.append(self.import_button)
        server.append(row)
        content.append(server)

        credentials = box(spacing=10)
        credentials.add_css_class("card")
        credentials.add_css_class("sv-card")
        credentials.add_css_class("credentials")
        row = box(False)
        title = label("OPENVPN CREDENTIALS", "eyebrow")
        title.set_hexpand(True)
        row.append(title)
        credentials_help = Gtk.Button(icon_name="dialog-question-symbolic", tooltip_text="Where do I find these credentials?")
        credentials_help.add_css_class("flat")
        credentials_help.connect("clicked", self.open_credentials)
        row.append(credentials_help)
        credentials.append(row)
        self.username = Gtk.Entry(placeholder_text="OpenVPN username")
        self.username.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
        self.username.update_property([Gtk.AccessibleProperty.LABEL], ["Proton OpenVPN username"])
        credentials.append(self.username)
        self.password = Gtk.PasswordEntry(placeholder_text="OpenVPN password", show_peek_icon=True)
        self.password.update_property([Gtk.AccessibleProperty.LABEL], ["Proton OpenVPN password"])
        self.password.connect("activate", lambda *_: self.on_connect())
        credentials.append(self.password)
        row = box(False)
        words = box(spacing=2)
        words.set_hexpand(True)
        words.append(label("Remember password", "card-title"))
        words.append(label("Save securely in your system keyring", "muted small"))
        row.append(words)
        self.remember = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.remember.update_property([Gtk.AccessibleProperty.LABEL], ["Remember password in system keyring"])
        row.append(self.remember)
        credentials.append(row)
        self.forget = Gtk.Button(label="Forget saved password")
        self.forget.add_css_class("flat")
        self.forget.set_halign(Gtk.Align.START)
        self.forget.connect("clicked", self.forget_password)
        credentials.append(self.forget)
        credentials.append(label("Use your OpenVPN credentials, separate from your Proton account login.", "muted small", True))
        content.append(credentials)
        self.credentials = credentials

        self.feedback = label("", "feedback small", True)
        self.feedback.set_visible(False)
        content.append(self.feedback)
        self.action = Gtk.Button(label="Connect")
        self.action.add_css_class("primary")
        self.action.add_css_class("suggested-action")
        self.action.connect("clicked", lambda *_: self.on_connect())
        content.append(self.action)
        self.footer = label("Closing this window keeps an active VPN connected.", "muted small", True)
        self.footer.set_halign(Gtk.Align.CENTER)
        self.footer.set_justify(Gtk.Justification.CENTER)
        content.append(self.footer)
        self.activity_label = label("No connection activity yet", "activity", True)
        expander = Gtk.Expander(label="Recent activity")
        expander.set_child(self.activity_label)
        content.append(expander)

        selected = self.prefs.data.get("selected")
        index = next((i for i, p in enumerate(self.profiles) if p["uuid"] == selected), 0)
        self.dropdown.set_selected(index)
        self.dropdown.connect("notify::selected", lambda *_: self.load_selection())
        if demo or self.prefs.data.get("migrated_v2"):
            self.load_selection()
        else:
            self.busy = self.migrating = True
            self.set_status("disconnected")
            self.show_message("Checking saved settings for the new data location…")
            def migration_work():
                messages = []
                migrate_legacy(self.prefs, self.backend, messages.append, target_data=self.data_root)
                return messages
            self.run_worker(migration_work, self.migration_done)
        self.present()
        if not demo:
            self._poll_source = GLib.timeout_add_seconds(2, self.poll)
            self.poll()
        if screenshot:
            self.add_tick_callback(self.capture_when_ready)

    def discover_profiles(self):
        bundled = self.data_root / "servers"
        paths = sorted(bundled.glob("*.ovpn"))
        for name in ["nl-443.ovpn", "nl-free-2.protonvpn.tcp.ovpn"]:
            if not (bundled / name).is_file():
                paths.append(self.download_root / name)
        paths.extend(sorted((self.data_root / "profiles").glob("*.ovpn")))
        result = []
        for path in paths:
            if path.is_file() or self.demo and path.name == "nl-443.ovpn":
                name = server_label(path)
                if any(item["name"] == name for item in result):
                    name += " (imported)"
                result.append({"path": path, "name": name, "uuid": profile_uuid(path)})
        return result

    @guarded
    def migration_done(self, result, error):
        for message in result or []:
            self.event(message)
        if error:
            debug_exception(error)
            self.event(f"Migration could not finish ({type(error).__name__}); originals kept.")
        self.profiles = self.discover_profiles()
        self.dropdown.set_model(Gtk.StringList.new([p["name"] for p in self.profiles]))
        selected = self.prefs.data.get("selected")
        self.dropdown.set_selected(next((i for i, p in enumerate(self.profiles) if p["uuid"] == selected), 0))
        self.busy = self.migrating = False
        self.show_message("")
        self.load_selection()
        self.set_status("disconnected")
        self.poll()
        return GLib.SOURCE_REMOVE

    def selected(self):
        i = self.dropdown.get_selected()
        return self.profiles[i] if i < len(self.profiles) else None

    @guarded
    def load_selection(self):
        if self.migrating:
            return
        profile = self.selected()
        if not profile:
            self.server_detail.set_text("Import an .ovpn file to choose a server.")
            self.show_message("Import a Proton OpenVPN .ovpn file to get started.")
            self.action.set_sensitive(False)
            return
        saved = self.prefs.profile(profile["uuid"])
        self.username.set_text(saved.get("username", ""))
        self.password.set_text("")
        self.password.set_property("placeholder-text", "Saved in your keyring" if saved.get("remembered") else "OpenVPN password")
        self.remember.set_active(bool(saved.get("remembered")))
        self.forget.set_visible(bool(saved.get("remembered")))
        self.prefs.data["selected"] = profile["uuid"]
        self.server_detail.set_text("OpenVPN · " + profile["name"])
        self.feedback.set_visible(False)
        self.action.set_sensitive(True)

    def run_worker(self, work, done):
        @guarded
        def finish(window, result, error):
            if window._closed:
                return GLib.SOURCE_REMOVE
            return done(result, error)
        def runner():
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(finish, self, result, error)
        threading.Thread(target=runner, daemon=True).start()

    def event(self, message):
        self.events.append(time.strftime("%H:%M") + "  " + message)
        self.activity_label.set_text("\n".join(self.events[-6:]))

    def show_message(self, text):
        self._status_message = False
        self.feedback.set_text(text)
        self.feedback.set_visible(bool(text))

    def show_status_message(self, text):
        self.show_message(text)
        self._status_message = bool(text)

    def set_status(self, state):
        self.last_state = state
        if state == "connected":
            self.action.remove_css_class("suggested-action")
        else:
            self.action.add_css_class("suggested-action")
        self.orb.remove_css_class("online")
        self.orb.remove_css_class("pending")
        if state == "connected":
            self.status_title.set_text("Connected")
            self.status_sub.set_text("Your VPN tunnel is active.")
            self.orb.add_css_class("online")
            self.action.set_label("Disconnect")
            self.action.add_css_class("stop")
        elif state in ("connecting", "external-pending", "disconnecting"):
            self.status_title.set_text("Connecting… (started elsewhere)" if state == "external-pending" else "Connecting…" if state == "connecting" else "Disconnecting…")
            self.status_sub.set_text("Waiting for the other connection attempt." if state == "external-pending" else "This may take up to 90 seconds." if state == "connecting" else "Closing your VPN connection.")
            self.orb.add_css_class("pending")
            self.action.set_label("Connecting…" if state == "external-pending" else "Cancel connection" if state == "connecting" else "Disconnecting…")
            self.action.remove_css_class("stop")
        else:
            self.status_title.set_text("Ready to connect")
            self.status_sub.set_text("Your VPN, one click away.")
            self.action.set_label("Connect")
            self.action.remove_css_class("stop")
        locked = self.busy or self.connected or self.external_pending
        self.dropdown.set_sensitive(bool(self.profiles) and not locked)
        self.import_button.set_sensitive(not locked)
        self.credentials.set_sensitive(not locked)
        self.action.set_sensitive(bool(self.profiles) and not self.external_pending and (not self.busy or self.connecting and not self.cancel.is_set()))

    @guarded
    def on_connect(self):
        if self.migrating or self._closing or self.external_pending:
            return
        if not self._cleanup_done.is_set():
            self.show_message("Wait for cancellation cleanup to finish before reconnecting.")
            return
        if self.demo:
            self.show_message("Preview only. Launch normally to connect with your credentials.")
            return
        profile = self.selected()
        if not profile:
            return
        if self.busy:
            if self.connecting:
                self.cancel.set()
                self.cancel_guard = profile["uuid"]
                self.show_message("Cancelling the connection…")
                self.action.set_sensitive(False)
                self.request_cancel(profile["uuid"])
            return
        if self.connected:
            self.busy = True
            self.set_status("disconnecting")
            self.run_worker(lambda: self.backend.disconnect(profile["uuid"]), self.disconnected)
            return
        username = self.username.get_text().strip()
        password = self.password.get_text()
        remembered = bool(self.prefs.profile(profile["uuid"]).get("remembered"))
        if not username or not password and not remembered:
            self.show_message("Enter your Proton OpenVPN username and password to connect.")
            (self.username if not username else self.password).grab_focus()
            return
        self.feedback.set_visible(False)
        self.busy = self.connecting = True
        self.cancel = threading.Event()
        self.cancel_guard = None
        self._pending_uuid = profile["uuid"]
        self._attempt_done.clear()
        remember = self.remember.get_active()
        self.prefs.profile(profile["uuid"])["username"] = username
        try:
            self.prefs.save()
        except OSError:
            self.event("Preferences could not be saved.")
        self.set_status("connecting")
        self.event("Connecting to " + profile["name"])
        self.password.set_text("")
        def connect_work():
            try:
                return self.backend.connect(profile["path"], username, password, remember, self.cancel)
            finally:
                self._attempt_done.set()
        self.run_worker(connect_work, lambda result, error: self.connected_result(profile, remember, result, error))

    @guarded
    def connected_result(self, profile, remember, result, error):
        self.busy = self.connecting = False
        if self._closing:
            return GLib.SOURCE_REMOVE
        if error:
            self.connected = False
            self.set_status("disconnected")
            self.show_message(str(error))
            self.event("Connection cancelled." if isinstance(error, Cancelled) else "Connection failed.")
        else:
            self.connected = True
            self.set_status("connected")
            saved = self.prefs.profile(profile["uuid"])
            saved["remembered"] = remember and "not saved" not in result
            try:
                self.prefs.save()
            except OSError:
                self.event("Preferences could not be saved.")
            self.password.set_property("placeholder-text", "Saved in your keyring" if saved["remembered"] else "OpenVPN password")
            self.forget.set_visible(saved["remembered"])
            if "not saved" in result:
                self.show_message(result)
            self.event("VPN tunnel connected.")
        self.poll()
        return GLib.SOURCE_REMOVE

    @guarded
    def disconnected(self, result, error):
        self.busy = False
        if error:
            self.show_message(str(error))
        else:
            self.connected = False
            self.set_status("disconnected")
            self.show_message("")
            self.event("VPN disconnected.")
        self.poll()
        return GLib.SOURCE_REMOVE

    @guarded(keep_alive=True)
    def poll(self):
        if self._closing or self._closed:
            return GLib.SOURCE_CONTINUE
        if self.polling or self.demo or self.migrating:
            return GLib.SOURCE_CONTINUE
        self.polling = True
        self.run_worker(lambda: self.backend.snapshot(background=True), self.snapshot_done)
        return GLib.SOURCE_CONTINUE

    @guarded
    def snapshot_done(self, result, error):
        self.polling = False
        if error:
            if not self.busy:
                debug_exception(error, self.username.get_text(), self.password.get_text())
                if isinstance(error, GLib.Error):
                    self.show_status_message("Cannot reach NetworkManager over D-Bus. Check that the service and system bus are available.")
                    self.action.set_sensitive(False)
                else:
                    self.show_status_message(f"Could not refresh VPN status ({type(error).__name__}). Retry; enable SIMPLE_VPN_DEBUG=1 for details.")
                    self.action.set_sensitive(bool(self.profiles))
            return GLib.SOURCE_REMOVE
        if self._status_message:
            self.show_status_message("")
        profile = self.selected()
        if not profile:
            return GLib.SOURCE_REMOVE
        ours = next((a for a in result["active"] if a["uuid"] == profile["uuid"]), None)
        if self.cancel_guard and any(a["uuid"] == self.cancel_guard for a in result["active"]) and not self.busy:
            # Catch an activation that completed while Cancel was being processed.
            self.busy = True
            self.set_status("disconnecting")
            self.run_worker(lambda: self.backend.disconnect(self.cancel_guard), self.disconnected)
            return GLib.SOURCE_REMOVE
        if not self.busy:
            was_connected = self.connected
            self.connected = bool(ours and ours["state"] == 2)
            self.external_pending = bool(ours and ours["state"] == 1)
            self.set_status("connected" if self.connected else "external-pending" if self.external_pending else "disconnected")
            if was_connected and not self.connected:
                self.event("VPN connection ended.")
                self.show_message("The VPN connection ended. Your normal internet connection may still be active.")
            if result["legacy"] or result["active"] and not ours:
                self.show_status_message("Another VPN is active. Disconnect it before connecting here.")
                self.action.set_sensitive(False)
        return GLib.SOURCE_REMOVE

    @guarded
    def forget_password(self, *_):
        profile = self.selected()
        if not profile or self.demo:
            return
        self.busy = True
        self.credentials.set_sensitive(False)
        self.action.set_sensitive(False)
        self.dropdown.set_sensitive(False)
        self.import_button.set_sensitive(False)
        self.forget.set_sensitive(False)
        def done(result, error):
            self.busy = False
            self.forget.set_sensitive(True)
            self.set_status(self.last_state)
            if error:
                self.show_message("Could not access the keyring. Unlock it and try again.")
            else:
                self.prefs.profile(profile["uuid"])["remembered"] = False
                try:
                    self.prefs.save()
                except OSError:
                    pass
                self.load_selection()
                self.event("Saved password removed.")
            return GLib.SOURCE_REMOVE
        self.run_worker(lambda: self.backend.keyring.forget(profile["uuid"]), done)

    @guarded
    def import_profile(self, *_):
        if self.demo:
            return
        dialog = Gtk.FileDialog(title="Import Proton OpenVPN configuration", accept_label="Import")
        file_filter = Gtk.FileFilter()
        file_filter.set_name("OpenVPN configurations (*.ovpn)")
        file_filter.add_pattern("*.ovpn")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        dialog.set_filters(filters)
        dialog.open(self, None, self.import_response)

    @guarded
    def import_response(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            target = import_profile_file(file.get_path() if file is not None else None, self.data_root / "profiles")
            self.profiles = self.discover_profiles()
            self.dropdown.set_model(Gtk.StringList.new([p["name"] for p in self.profiles]))
            self.dropdown.set_selected(next(i for i, p in enumerate(self.profiles) if p["path"] == target))
            self.load_selection()
            self.event("Server configuration imported.")
        except VPNError as error:
            self.show_message(str(error))
        except GLib.Error as error:
            if not self.dialog_cancelled(error):
                self.show_message("The file chooser could not open that file. Try a local .ovpn file.")

    @staticmethod
    def dialog_cancelled(error):
        return any(error.matches(Gtk.DialogError.quark(), code) for code in (Gtk.DialogError.CANCELLED, Gtk.DialogError.DISMISSED)) or error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)

    @guarded
    def open_credentials(self, *_):
        launcher = Gtk.UriLauncher.new(CREDENTIALS_URL)
        launcher.launch(self, None, self.uri_launched)

    @guarded
    def uri_launched(self, launcher, result):
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if not self.dialog_cancelled(error):
                self.show_message("The credentials page could not be opened. Open your Proton account in your browser.")

    @guarded
    def show_help(self, *_):
        dialog = Adw.AlertDialog(heading="Your Proton VPN connection", body="Use the OpenVPN username and password from Proton Account → Account → OpenVPN username.\n\nThis app uses your existing Netherlands TCP 443 configuration. Import a fresh .ovpn file to change servers.\n\nRemember password saves it in Ubuntu’s keyring. Disconnect before using another VPN app. Closing this window leaves an active tunnel connected.\n\nConnected means the tunnel is active. This app has no kill switch; traffic can use your normal connection when the VPN drops. Verify routing and DNS/IPv6 on your first connection.")
        dialog.add_response("close", "Got it")
        dialog.set_default_response("close")
        self.help_dialog = dialog
        dialog.present(self)

    def request_cancel(self, ident):
        self.cancel.set()
        self.cancel_guard = ident
        if not self._cleanup_done.is_set():
            return
        self._cleanup_done.clear()
        def cleanup():
            try:
                self.backend.cancel_pending(ident)
            finally:
                self._cleanup_done.set()
        self.run_worker(cleanup, lambda result, error: self.event("Cancellation cleanup requested." if error is None else "Cancellation cleanup could not finish; check the VPN status."))

    def stop_close_sources(self):
        for name in ("_poll_source", "_close_wait_source", "_close_deadline_source"):
            source = getattr(self, name)
            if source:
                GLib.source_remove(source)
                setattr(self, name, None)

    @guarded
    def finish_close(self):
        if not (self._attempt_done.is_set() and self._cleanup_done.is_set()):
            return GLib.SOURCE_CONTINUE
        self._close_wait_source = None
        self._allow_close = True
        self.close()
        return GLib.SOURCE_REMOVE

    @guarded
    def force_close(self):
        self._close_deadline_source = None
        try:
            if self.backend is not None and self._pending_uuid:
                self.backend.terminate_pending(self._pending_uuid)
        except Exception as error:
            debug_exception(error, self.username.get_text(), self.password.get_text())
        finally:
            self._allow_close = True
            self.close()
        return GLib.SOURCE_REMOVE

    @guarded
    def on_close(self, *_):
        if not self._allow_close and (self.connecting or self._closing or self._pending_uuid and not self._cleanup_done.is_set()):
            if not self._closing:
                self._closing = True
                ident = self._pending_uuid or (self.selected() or {}).get("uuid")
                self.show_message("Cancelling…")
                self.action.set_sensitive(False)
                if ident and self.backend is not None:
                    self.request_cancel(ident)
                self._close_wait_source = GLib.timeout_add(100, self.finish_close)
                self._close_deadline_source = GLib.timeout_add(10000, self.force_close)
            return True
        self._closed = True
        self.stop_close_sources()
        profile = self.selected()
        if profile and not self.demo and not self.migrating and not self._closing:
            self.prefs.profile(profile["uuid"])["username"] = self.username.get_text().strip()
            try:
                self.prefs.save()
            except OSError:
                pass
        return False

    @guarded
    def capture_when_ready(self, widget, frame_clock):
        # Two allocated frames allow layout and rendering to settle without a timer.
        if self.get_mapped() and self.get_width() > 0 and self.get_height() > 0:
            if getattr(self, "_capture_frame_seen", False):
                GLib.idle_add(self.capture)
                return GLib.SOURCE_REMOVE
            self._capture_frame_seen = True
        return GLib.SOURCE_CONTINUE

    @guarded
    def capture(self):
        from gi.repository import Graphene
        paintable = Gtk.WidgetPaintable.new(self)
        snapshot = Gtk.Snapshot.new()
        paintable.snapshot(snapshot, self.get_width(), self.get_height())
        node = snapshot.to_node()
        if node is None:
            raise RuntimeError("The application has not rendered")
        rectangle = Graphene.Rect().init(0, 0, self.get_width(), self.get_height())
        texture = self.get_renderer().render_texture(node, rectangle)
        texture.save_to_png(self.screenshot)
        self.get_application().quit()
        return GLib.SOURCE_REMOVE


class Application(Adw.Application):
    def __init__(self, demo=False, screenshot=None, preview_scheme=None):
        GLib.set_prgname(APP_ID)
        GLib.set_application_name("Simple VPN")
        Gtk.Window.set_default_icon_name(APP_ID)
        super().__init__(application_id=APP_ID + (".Preview" if demo else ""), flags=Gio.ApplicationFlags.NON_UNIQUE if demo else Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.demo, self.screenshot = demo, screenshot
        self.preview_scheme = preview_scheme
        self.preview_dir = tempfile.mkdtemp(prefix="simple-vpn-preview-") if demo else None
        self.signal_sources = []
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        if not self.demo:
            for number in (signal.SIGTERM, signal.SIGINT):
                self.signal_sources.append(GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, number, self.on_unix_signal))

    def on_unix_signal(self, *_):
        window = self.get_active_window()
        if window:
            window.close()
        else:
            self.quit()
        return GLib.SOURCE_CONTINUE

    def do_shutdown(self):
        try:
            if self.window and self.window.backend is not None and self.window._pending_uuid and not self.window._attempt_done.is_set():
                try:
                    self.window.backend.terminate_pending(self.window._pending_uuid)
                except Exception as error:
                    debug_exception(error, self.window.username.get_text(), self.window.password.get_text())
            for source in self.signal_sources:
                GLib.source_remove(source)
            self.signal_sources.clear()
            if self.preview_dir:
                shutil.rmtree(self.preview_dir)
                self.preview_dir = None
        finally:
            Adw.Application.do_shutdown(self)

    def do_activate(self):
        if self.get_active_window():
            self.get_active_window().present()
            return
        manager = Adw.StyleManager.get_default()
        if self.demo and self.preview_scheme:
            manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK if self.preview_scheme == "dark" else Adw.ColorScheme.FORCE_LIGHT)
        self.provider = Gtk.CssProvider()
        self.refresh_css()
        manager.connect("notify::dark", self.refresh_css)
        manager.connect("notify::high-contrast", self.refresh_css)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window = Window(self, self.demo, self.screenshot)

    def refresh_css(self, *_):
        manager = Adw.StyleManager.get_default()
        css = theme_css(manager.get_dark(), manager.get_high_contrast())
        if hasattr(self.provider, "load_from_string"):
            self.provider.load_from_string(css)
        else:
            self.provider.load_from_data(css.encode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simple VPN for Proton OpenVPN")
    parser.add_argument("--version", action="version", version="Simple VPN " + VERSION)
    parser.add_argument("--demo", action="store_true", help="Preview without networking or credential access")
    parser.add_argument("--screenshot", help="Render the preview to a PNG and exit")
    parser.add_argument("--check", action="store_true", help="Read backend status without changing the network")
    parser.add_argument("--theme", choices=["light", "dark"], help="Override theme only in preview/screenshot mode")
    args = parser.parse_args()
    if args.theme and not (args.demo or args.screenshot):
        parser.error("--theme is available only with --demo or --screenshot")
    if args.check:
        print(json.dumps(NetworkBackend().snapshot(), indent=2))
    else:
        raise SystemExit(Application(args.demo or bool(args.screenshot), args.screenshot, args.theme).run([]))
