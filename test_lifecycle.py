from __future__ import annotations

import threading
from unittest.mock import MagicMock

from app import Application, Window, theme_css
from backend import NetworkBackend
from test_backend import IsolatedCase


class LifecycleTests(IsolatedCase):
    def window(self):
        window = MagicMock()
        window._allow_close = window._closing = window._closed = False
        window.connecting = True
        window.connected = False
        window.demo = False
        window.migrating = False
        window._pending_uuid = "ours"
        window._attempt_done = threading.Event()
        window._cleanup_done = threading.Event()
        window.cancel = threading.Event()
        window._cleanup_done.set()
        window._poll_source = window._close_wait_source = window._close_deadline_source = None
        window.selected.return_value = {"uuid": "ours"}
        return window

    def test_close_while_connecting_requests_cancel_and_ten_second_deadline(self):
        window = self.window()
        timer = self.patch("app.GLib.timeout_add", side_effect=[101, 102])
        self.assertTrue(Window.on_close(window))
        self.assertTrue(window._closing)
        window.request_cancel.assert_called_once_with("ours")
        self.assertEqual([c.args[0] for c in timer.call_args_list], [100, 10000])
        window.show_message.assert_called_with("Cancelling…")

    def test_close_while_connected_leaves_tunnel_running(self):
        window = self.window()
        window.connecting = False
        window.connected = True
        self.assertFalse(Window.on_close(window))
        window.backend.cancel_pending.assert_not_called()
        window.backend.disconnect.assert_not_called()
        self.assertTrue(window._closed)

    def test_finished_workers_allow_close(self):
        window = self.window()
        self.assertTrue(Window.finish_close(window))
        window._attempt_done.set()
        window._cleanup_done.set()
        self.assertFalse(Window.finish_close(window))
        self.assertTrue(window._allow_close)
        window.close.assert_called_once()

    def test_force_close_kills_only_pending_uuid(self):
        window = self.window()
        self.assertFalse(Window.force_close(window))
        window.backend.terminate_pending.assert_called_once_with("ours")
        window.close.assert_called_once()

    def test_force_close_still_closes_if_child_cleanup_fails(self):
        window = self.window()
        window.backend.terminate_pending.side_effect = OSError("child already unavailable")
        self.assertFalse(Window.force_close(window))
        self.assertTrue(window._allow_close)
        window.close.assert_called_once()

    def test_signals_take_the_window_close_path(self):
        application = MagicMock()
        Application.on_unix_signal(application)
        application.get_active_window.return_value.close.assert_called_once()
        application.quit.assert_not_called()

    def test_terminate_pending_never_kills_a_different_uuid(self):
        backend = NetworkBackend()
        child = MagicMock()
        child.poll.return_value = None
        backend._process, backend._process_uuid = child, "ours"
        backend.terminate_pending("foreign")
        child.kill.assert_not_called()
        backend.terminate_pending("ours")
        child.kill.assert_called_once()

    def test_cancel_pending_disconnects_only_exact_uuid(self):
        backend = NetworkBackend()
        backend.disconnect = MagicMock()
        backend.terminate_pending = MagicMock()
        self.patch("backend.time.monotonic", side_effect=[0, 3])
        backend.cancel_pending("ours")
        backend.terminate_pending.assert_called_once_with("ours")
        backend.disconnect.assert_called_once_with("ours")

    def test_high_contrast_omits_custom_mint_colors(self):
        self.assertNotIn("#91e5c3", theme_css(False, True))
        self.assertIn("#91e5c3", theme_css(True, False))

    def test_file_dialog_uses_async_open_and_ovpn_filter(self):
        window = self.window()
        window.demo = False
        dialog = self.patch("app.Gtk.FileDialog")
        Window.import_profile(window)
        dialog.return_value.set_filters.assert_called_once()
        dialog.return_value.open.assert_called_once_with(window, None, window.import_response)

    def test_uri_launcher_is_async_and_never_launches_a_real_browser_in_test(self):
        window = self.window()
        launcher = self.patch("app.Gtk.UriLauncher.new")
        Window.open_credentials(window)
        launcher.return_value.launch.assert_called_once_with(window, None, window.uri_launched)

    def test_new_help_dialog_presents_on_parent(self):
        window = self.window()
        alert = self.patch("app.Adw.AlertDialog")
        Window.show_help(window)
        alert.return_value.present.assert_called_once_with(window)

    def test_css_loader_has_documented_older_runtime_fallback(self):
        application = MagicMock()
        application.provider = MagicMock(spec=["load_from_data"])
        manager = self.patch("app.Adw.StyleManager.get_default")
        manager.return_value.get_dark.return_value = False
        manager.return_value.get_high_contrast.return_value = False
        Application.refresh_css(application)
        self.assertIsInstance(application.provider.load_from_data.call_args.args[0], bytes)


class PollingTests(IsolatedCase):
    def window(self):
        window = MagicMock()
        window.busy = window.connecting = window.polling = False
        window.demo = window.migrating = window.connected = False
        window._closing = window._closed = window.external_pending = False
        window._status_message = False
        window.cancel_guard = None
        window.cancel = threading.Event()
        window.profiles = [{"uuid": "ours"}]
        window.selected.return_value = window.profiles[0]
        window.show_message = lambda text: Window.show_message(window, text)
        window.show_status_message = lambda text: Window.show_status_message(window, text)
        window.set_status = lambda state: Window.set_status(window, state)
        return window

    def state(self, active=None, legacy=False):
        return {"active": active or [], "legacy": legacy, "networking": True}

    def test_poll_survives_callback_exception_without_losing_timer(self):
        window = self.window()
        window._poll_source = 123
        window.run_worker.side_effect = RuntimeError("injected callback failure")
        self.assertTrue(Window.poll(window))
        self.assertEqual(window._poll_source, 123)
        self.assertFalse(window.polling)
        window.run_worker.side_effect = None
        self.assertTrue(Window.poll(window))
        self.assertEqual(window.run_worker.call_count, 2)
        work = window.run_worker.call_args.args[0]
        work()
        window.backend.snapshot.assert_called_once_with(background=True)

    def test_poll_all_early_returns_continue(self):
        for name in ("_closing", "_closed", "polling", "demo", "migrating"):
            window = self.window()
            setattr(window, name, True)
            self.assertTrue(Window.poll(window), name)
            window.run_worker.assert_not_called()

    def test_external_activation_is_disabled_and_click_cannot_connect(self):
        window = self.window()
        Window.snapshot_done(window, self.state([{"uuid": "ours", "state": 1}]), None)
        self.assertTrue(window.external_pending)
        self.assertFalse(window.connecting)
        self.assertEqual(window.last_state, "external-pending")
        self.assertIn("started elsewhere", window.status_title.set_text.call_args.args[0])
        window.action.set_sensitive.assert_called_with(False)
        Window.on_connect(window)
        window.backend.connect.assert_not_called()
        window.run_worker.assert_not_called()
        Window.snapshot_done(window, self.state([{"uuid": "ours", "state": 2}]), None)
        self.assertFalse(window.external_pending)
        self.assertTrue(window.connected)
        window.action.set_label.assert_called_with("Disconnect")
        window.action.set_sensitive.assert_called_with(True)

    def test_status_error_is_cleared_after_successful_refresh(self):
        from backend import GLib
        window = self.window()
        Window.snapshot_done(window, None, GLib.Error("mock bus failure"))
        self.assertTrue(window._status_message)
        window.feedback.set_visible.assert_called_with(True)
        Window.snapshot_done(window, self.state(), None)
        self.assertFalse(window._status_message)
        window.feedback.set_visible.assert_called_with(False)
        window.action.set_sensitive.assert_called_with(True)

    def test_other_vpn_or_legacy_warning_clears_when_condition_clears(self):
        for state in (self.state([{"uuid": "other", "state": 2}]), self.state(legacy=True)):
            window = self.window()
            Window.snapshot_done(window, state, None)
            self.assertTrue(window._status_message)
            window.action.set_sensitive.assert_called_with(False)
            Window.snapshot_done(window, self.state(), None)
            self.assertFalse(window._status_message)
            window.feedback.set_visible.assert_called_with(False)
            window.action.set_sensitive.assert_called_with(True)

    def test_status_recovery_does_not_erase_operation_feedback(self):
        window = self.window()
        window.show_message("Authentication failed.")
        Window.snapshot_done(window, self.state(), None)
        window.feedback.set_text.assert_called_with("Authentication failed.")
        window.feedback.set_visible.assert_called_with(True)

    def test_disconnect_css_is_visible_in_all_themes(self):
        for dark, contrast in ((False, False), (True, False), (False, True)):
            css = theme_css(dark, contrast)
            self.assertIn(".simple-vpn .primary.stop { background: @window_bg_color; color: @window_fg_color; border: 2px solid @window_fg_color; }", css)

    def test_credentials_link_uses_named_constant(self):
        from app import CREDENTIALS_URL
        window = self.window()
        launcher = self.patch("app.Gtk.UriLauncher.new")
        Window.open_credentials(window)
        launcher.assert_called_once_with(CREDENTIALS_URL)


class FirstLaunchTests(IsolatedCase):
    def test_empty_profile_list_explains_import_and_disables_connect(self):
        window = MagicMock()
        window.migrating = False
        window.selected.return_value = None
        Window.load_selection(window)
        window.action.set_sensitive.assert_called_once_with(False)
        window.show_message.assert_called_once_with("Import a Proton OpenVPN .ovpn file to get started.")
        window.backend.connect.assert_not_called()
