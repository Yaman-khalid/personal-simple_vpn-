from __future__ import annotations

import io
import logging
import os
from pathlib import Path
import subprocess
import threading
from unittest.mock import MagicMock

from backend import APP_PREFIX, GLib, Keyring, LOGGER, MAX_PROFILE_BYTES, NM, NetworkBackend, Preferences, VPNError, activation_error, guarded, import_profile_file, legacy_profile_uuid, profile_uuid
from migration import migrate_legacy
from test_backend import IsolatedCase


class LegacyCheckTests(IsolatedCase):
    def test_missing_executable(self):
        run = self.patch("backend.subprocess.run")
        self.assertFalse(NetworkBackend().legacy_service_active())
        run.assert_not_called()

    def test_missing_systemctl_after_executable_check(self):
        self.patch("backend.os.access", return_value=True)
        self.patch("backend.subprocess.run", side_effect=FileNotFoundError())
        backend = NetworkBackend()
        backend._properties = MagicMock(return_value={"ActiveConnections": [], "NetworkingEnabled": True})
        self.assertEqual(backend.snapshot(), {"active": [], "legacy": False, "networking": True})

    def test_timeout_is_nonfatal(self):
        self.patch("backend.os.access", return_value=True)
        self.patch("backend.subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 5))
        self.assertFalse(NetworkBackend().legacy_service_active())

    def test_nonzero_exit_is_false(self):
        self.patch("backend.os.access", return_value=True)
        self.patch("backend.subprocess.run", return_value=MagicMock(returncode=1, stdout="active"))
        self.assertFalse(NetworkBackend().legacy_service_active())

    def test_cache_and_expiry(self):
        self.patch("backend.os.access", return_value=True)
        clock = self.patch("backend.time.monotonic", return_value=100)
        run = self.patch("backend.subprocess.run", return_value=MagicMock(returncode=0, stdout="active\n"))
        backend = NetworkBackend()
        self.assertTrue(backend.legacy_service_active(use_cache=True))
        clock.return_value = 109
        self.assertTrue(backend.legacy_service_active(use_cache=True))
        self.assertEqual(run.call_count, 1)
        clock.return_value = 110
        self.assertTrue(backend.legacy_service_active(use_cache=True))
        self.assertEqual(run.call_count, 2)

    def test_unit_can_be_customized_or_disabled(self):
        prefs = Preferences(self.root / "prefs")
        prefs.data["legacy_unit"] = ""
        run = self.patch("backend.subprocess.run")
        self.assertFalse(NetworkBackend(prefs).legacy_service_active())
        run.assert_not_called()
        prefs.data["legacy_unit"] = "custom-vpn.service"
        self.assertEqual(NetworkBackend(prefs).legacy_unit, "custom-vpn.service")

    def test_default_backend_reads_the_saved_unit_preference(self):
        preferences = Preferences()
        preferences.data["legacy_unit"] = "saved-custom.service"
        preferences.save()
        self.assertEqual(NetworkBackend().legacy_unit, "saved-custom.service")

    def test_connect_bypasses_cached_inactive_result_and_blocks_activation(self):
        self.patch("backend.os.access", return_value=True)
        self.patch("backend.time.monotonic", return_value=100)
        run = self.patch("backend.subprocess.run", side_effect=[MagicMock(returncode=3, stdout="inactive"), MagicMock(returncode=0, stdout="active")])
        backend = NetworkBackend()
        backend._properties = MagicMock(return_value={"ActiveConnections": []})
        backend.prepare_profile = MagicMock()
        backend._activate = MagicMock()
        self.assertFalse(backend.snapshot(background=True)["legacy"])
        self.assertFalse(backend.snapshot(background=True)["legacy"])
        self.assertEqual(run.call_count, 1)
        with self.assertRaisesRegex(VPNError, "older Proton"):
            backend.connect(self.root / "servers/test.ovpn", "fake-user", "fake-password", False, threading.Event())
        self.assertEqual(run.call_count, 2)
        backend.prepare_profile.assert_not_called()
        backend._activate.assert_not_called()

    def test_foreground_checks_are_always_fresh(self):
        self.patch("backend.os.access", return_value=True)
        run = self.patch("backend.subprocess.run", return_value=MagicMock(returncode=3, stdout="inactive"))
        backend = NetworkBackend()
        backend.legacy_service_active()
        backend.legacy_service_active()
        self.assertEqual(run.call_count, 2)


class PathTests(IsolatedCase):
    def test_xdg_helpers(self):
        from paths import config_dir, data_dir, downloads_dir
        self.assertEqual(config_dir(), self.root / "config/simple-vpn")
        self.assertEqual(data_dir(), self.root / "data/simple-vpn")
        self.assertEqual(downloads_dir(), self.root / "Downloads")
        self.assertEqual(Preferences().directory, config_dir())

    def test_empty_xdg_config_variable_still_delegates_to_glib(self):
        from unittest.mock import patch
        from paths import config_dir
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(Preferences().directory, config_dir())

    def test_download_fallback_only_for_none(self):
        from paths import downloads_dir
        self.patch("paths.GLib.get_user_special_dir", return_value=None)
        self.assertEqual(downloads_dir(), self.home / "Downloads")

    def test_uuid_survives_install_move_and_separates_imported_files(self):
        self.assertEqual(profile_uuid(self.root / "old/servers/nl-443.ovpn"), profile_uuid(self.root / "new/servers/nl-443.ovpn"))
        self.assertEqual(profile_uuid(self.root / "Downloads/nl-443.ovpn"), profile_uuid(self.root / "new/servers/nl-443.ovpn"))
        self.assertNotEqual(profile_uuid(Path("servers/a.ovpn")), profile_uuid(Path("profiles/a.ovpn")))
        self.assertNotEqual(legacy_profile_uuid(self.root / "old/a.ovpn"), legacy_profile_uuid(self.root / "new/a.ovpn"))

    def test_discovery_uses_xdg_data_then_imports_and_only_known_download_fallbacks(self):
        from app import Window
        window = MagicMock()
        window.data_root = self.root / "data/simple-vpn"
        window.download_root = self.root / "Downloads"
        window.demo = False
        for folder in [window.data_root / "servers", window.data_root / "profiles", window.download_root]:
            folder.mkdir(parents=True)
        bundled = window.data_root / "servers/nl-443.ovpn"
        imported = window.data_root / "profiles/custom.ovpn"
        fallback = window.download_root / "nl-free-2.protonvpn.tcp.ovpn"
        for file in [bundled, imported, fallback, window.download_root / "nl-443.ovpn", window.download_root / "unrelated.ovpn"]:
            file.touch()
        self.assertEqual([p["path"] for p in Window.discover_profiles(window)], [bundled, fallback, imported])


class ImportTests(IsolatedCase):
    def test_normal_file_is_private_and_exact(self):
        source = self.root / "server.ovpn"
        source.write_bytes(b"remote example.test 443\n")
        target = import_profile_file(source, self.root / "profiles")
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_fifo_is_rejected_without_blocking(self):
        source = self.root / "fifo.ovpn"
        os.mkfifo(source)
        with self.assertRaisesRegex(VPNError, "not a regular file"):
            import_profile_file(source, self.root / "profiles")

    def test_oversized(self):
        source = self.root / "large.ovpn"
        with source.open("wb") as stream:
            stream.truncate(MAX_PROFILE_BYTES + 1)
        with self.assertRaisesRegex(VPNError, "too large"):
            import_profile_file(source, self.root / "profiles")

    def test_duplicate_does_not_overwrite(self):
        source = self.root / "server.ovpn"
        source.write_bytes(b"first")
        directory = self.root / "profiles"
        target = import_profile_file(source, directory)
        source.write_bytes(b"second")
        with self.assertRaisesRegex(VPNError, "already imported"):
            import_profile_file(source, directory)
        self.assertEqual(target.read_bytes(), b"first")

    def test_remote_none_path(self):
        with self.assertRaisesRegex(VPNError, "Remote files"):
            import_profile_file(None, self.root / "profiles")

    def test_unreadable(self):
        self.patch("backend.os.open", side_effect=PermissionError())
        with self.assertRaisesRegex(VPNError, "not readable"):
            import_profile_file(self.root / "server.ovpn", self.root / "profiles")

    def test_read_is_bounded_even_if_stat_reports_small_size(self):
        source = self.root / "growing.ovpn"
        source.write_bytes(b"x" * (MAX_PROFILE_BYTES + 1))
        real_stat = source.stat()
        fake_stat = MagicMock(st_mode=real_stat.st_mode, st_size=0)
        self.patch("backend.os.fstat", return_value=fake_stat)
        with self.assertRaisesRegex(VPNError, "too large"):
            import_profile_file(source, self.root / "profiles")

    def test_unexpected_import_error_is_logged_and_friendly(self):
        self.patch("backend.read_profile_file", side_effect=RuntimeError("internal failure"))
        debug = self.patch("backend.debug_exception")
        with self.assertRaisesRegex(VPNError, "Unexpected import error \\(RuntimeError\\)"):
            import_profile_file(self.root / "test.ovpn", self.root / "profiles")
        debug.assert_called_once()


class ErrorTests(IsolatedCase):
    def fake_connection(self):
        backend = NetworkBackend()
        backend.snapshot = MagicMock(return_value={"legacy": False, "active": [], "networking": True})
        backend.prepare_profile = MagicMock(return_value="id")
        backend._activate = MagicMock()
        backend.keyring = MagicMock()
        return backend

    def test_polkit_failure_is_not_bad_password(self):
        error = activation_error(4, "Not authorized to control networking")
        self.assertNotIn("Authentication failed", str(error))
        self.assertIn("Not authorized", str(error))

    def test_timeout_code_wins_over_auth_wording(self):
        self.assertIn("timed out", str(activation_error(3, "login failed")))

    def test_networkmanager_exit_code(self):
        self.assertIn("NetworkManager is not running", str(activation_error(8, "unknown")))

    def test_authentication_uses_specific_phrases_only(self):
        self.assertIn("Authentication failed", str(activation_error(4, "No valid secrets")))
        self.assertNotIn("Authentication failed", str(activation_error(1, "login failed")))

    def test_unexpected_exception_is_generic_and_logs_redact_both_credentials(self):
        backend = self.fake_connection()
        backend.prepare_profile.side_effect = RuntimeError("alice-secret-name: secret-password-value")
        stream = io.StringIO()
        self.patch("backend.LOGGER.disabled", False)
        self.patch("backend.LOGGER.handlers", [logging.StreamHandler(stream)])
        with self.assertRaisesRegex(VPNError, "Unexpected error \\(RuntimeError\\)"):
            backend.connect(Path("servers/test.ovpn"), "alice-secret-name", "secret-password-value", False, threading.Event())
        output = stream.getvalue()
        self.assertIn("RuntimeError", output)
        self.assertIn("Traceback", output)
        self.assertNotIn("alice-secret-name", output)
        self.assertNotIn("secret-password-value", output)

    def test_guard_resets_busy_connecting_and_action(self):
        owner = MagicMock()
        owner.busy = owner.connecting = owner.polling = True
        owner.connected = False
        owner.set_status.side_effect = lambda state: owner.action.set_sensitive(True)
        @guarded
        def broken(self):
            raise RuntimeError("callback failed")
        broken(owner)
        self.assertFalse(owner.busy)
        self.assertFalse(owner.connecting)
        owner.action.set_sensitive.assert_called_with(True)
        self.assertIn("Unexpected interface error", owner.show_message.call_args.args[0])

    def test_guard_logs_redact_credentials(self):
        owner = MagicMock()
        owner.connected = False
        owner.username.get_text.return_value = "sensitive-user-name"
        owner.password.get_text.return_value = "sensitive-password-value"
        stream = io.StringIO()
        self.patch("backend.LOGGER.disabled", False)
        self.patch("backend.LOGGER.handlers", [logging.StreamHandler(stream)])
        @guarded
        def broken(self):
            raise RuntimeError("sensitive-user-name sensitive-password-value")
        broken(owner)
        self.assertIn("RuntimeError", stream.getvalue())
        self.assertNotIn("sensitive-user-name", stream.getvalue())
        self.assertNotIn("sensitive-password-value", stream.getvalue())

    def test_snapshot_classifies_glib_and_other_errors(self):
        from app import Window
        for error, expected, enabled in [(GLib.Error("test"), "D-Bus", False), (TimeoutError("test"), "TimeoutError", True)]:
            window = MagicMock()
            window.busy = False
            window.profiles = [{}]
            Window.snapshot_done(window, None, error)
            self.assertIn(expected, window.show_status_message.call_args.args[0])
            window.action.set_sensitive.assert_called_with(enabled)

    def test_keyring_wrappers_have_friendly_messages(self):
        for operation, function, expected, args in [
            ("lookup", "password_lookup_sync", "Enter it manually", ("id",)),
            ("store", "password_store_sync", "could not save", ("id", "fake-secret")),
            ("forget", "password_clear_sync", "could not remove", ("id",)),
        ]:
            with self.subTest(operation=operation):
                self.patch("backend.Secret." + function, side_effect=GLib.Error("service unavailable"))
                with self.assertRaisesRegex(VPNError, expected):
                    getattr(Keyring(), operation)(*args)

    def test_optional_forget_failure_does_not_block_connection(self):
        backend = self.fake_connection()
        backend.keyring.forget.side_effect = GLib.Error("locked")
        self.assertIn("Connected", backend.connect(Path("servers/test.ovpn"), "user", "fake-password", False, threading.Event()))
        backend._activate.assert_called_once()

    def test_lookup_failure_allows_manual_retry(self):
        backend = self.fake_connection()
        backend.keyring.lookup.side_effect = GLib.Error("locked")
        with self.assertRaisesRegex(VPNError, "Enter it manually"):
            backend.connect(Path("servers/test.ovpn"), "user", "", False, threading.Event())
        self.assertIn("Connected", backend.connect(Path("servers/test.ovpn"), "user", "manual-secret", False, threading.Event()))

    def test_store_failure_keeps_successful_connection(self):
        backend = self.fake_connection()
        backend.keyring.store.side_effect = GLib.Error("locked")
        self.assertIn("not saved", backend.connect(Path("servers/test.ovpn"), "user", "fake-password", True, threading.Event()))


class PluginTests(IsolatedCase):
    def source(self):
        source = self.root / "test.ovpn"
        source.touch()
        return source

    def plugin(self):
        file = self.root / "plugin.name"
        file.touch()
        self.patch("backend.PLUGIN_FILES", (file,))
        return self.patch("backend.NM.VpnPluginInfo.new_from_file")

    def test_missing_plugin(self):
        self.patch("backend.PLUGIN_FILES", (self.root / "missing.name",))
        with self.assertRaisesRegex(VPNError, "Install network-manager-openvpn-gnome"):
            NetworkBackend().prepare_profile(self.source(), "user")

    def test_load_failure(self):
        plugin = self.plugin()
        plugin.return_value.load_editor_plugin.side_effect = GLib.Error("missing library")
        with self.assertRaisesRegex(VPNError, "Could not load OpenVPN support"):
            NetworkBackend().prepare_profile(self.source(), "user")

    def test_none_vpn_setting(self):
        plugin = self.plugin()
        plugin.return_value.load_editor_plugin.return_value.import_.return_value.get_setting_vpn.return_value = None
        with self.assertRaisesRegex(VPNError, "not a valid OpenVPN"):
            NetworkBackend().prepare_profile(self.source(), "user")

    def test_import_glib_error(self):
        plugin = self.plugin()
        plugin.return_value.load_editor_plugin.return_value.import_.side_effect = GLib.Error("invalid config")
        with self.assertRaisesRegex(VPNError, "Could not import"):
            NetworkBackend().prepare_profile(self.source(), "user")

    def test_refuses_to_update_foreign_profile(self):
        backend = NetworkBackend()
        backend._call = MagicMock(return_value=MagicMock(unpack=lambda: ({"connection": {"id": "Other VPN", "uuid": "id"}},)))
        with self.assertRaisesRegex(VPNError, "did not create"):
            backend._require_owned_profile("/settings/test", "id")

    def test_cleanup_keeps_active_owned_profile(self):
        backend = NetworkBackend()
        backend._connection_path = MagicMock(return_value="/settings/test")
        backend._require_owned_profile = MagicMock()
        backend._properties = MagicMock(side_effect=[{"ActiveConnections": ["/active/test"]}, {"Uuid": "old-id"}])
        backend._call = MagicMock()
        self.assertFalse(backend.remove_legacy_profile("old-id"))
        backend._call.assert_not_called()

    def test_cleanup_deletes_only_verified_owned_inactive_profile(self):
        backend = NetworkBackend()
        backend._connection_path = MagicMock(return_value="/settings/ours")
        backend._require_owned_profile = MagicMock()
        backend._properties = MagicMock(return_value={"ActiveConnections": []})
        backend._call = MagicMock()
        self.assertTrue(backend.remove_legacy_profile("old-id"))
        backend._require_owned_profile.assert_called_once_with("/settings/ours", "old-id")
        self.assertEqual(backend._call.call_args.args[2], "Delete")

    def test_add_and_update_use_memory_only_and_never_save(self):
        plugin = self.plugin()
        for exists in [False, True]:
            with self.subTest(existing=exists):
                connection = NM.SimpleConnection.new()
                setting = NM.SettingConnection.new()
                setting.set_property("id", "imported")
                connection.add_setting(setting)
                vpn = NM.SettingVpn.new()
                vpn.set_property("service-type", "org.freedesktop.NetworkManager.openvpn")
                for key, value in {"connection-type": "password", "remote-cert-tls": "server", "ca": "/tmp/fake-ca.pem"}.items():
                    vpn.add_data_item(key, value)
                connection.add_setting(vpn)
                plugin.return_value.load_editor_plugin.return_value.import_.return_value = connection
                backend = NetworkBackend()
                backend._connection_path = MagicMock(return_value="/settings/ours" if exists else None)
                backend._require_owned_profile = MagicMock()
                backend._call = MagicMock()
                backend.prepare_profile(self.source(), "test-user")
                call = backend._call.call_args
                self.assertEqual(call.args[2], "Update2" if exists else "AddConnection2")
                self.assertEqual(call.args[3].unpack()[1], 2)
                self.assertNotIn("secrets", call.args[3].unpack()[0]["vpn"])

    def test_certificate_verification_is_still_required(self):
        plugin = self.plugin()
        vpn = plugin.return_value.load_editor_plugin.return_value.import_.return_value.get_setting_vpn.return_value
        vpn.get_data_item.side_effect = lambda key: {"connection-type": "password", "remote-cert-tls": "client", "ca": "fake-ca"}.get(key)
        with self.assertRaisesRegex(VPNError, "server certificate verification"):
            NetworkBackend().prepare_profile(self.source(), "test-user")


class MigrationTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.prefs = Preferences(self.root / "config/simple-vpn")
        self.destination = self.root / "data/simple-vpn"
        self.downloads = self.root / "Downloads"
        self.downloads.mkdir()
        self.backend = MagicMock()
        self.backend.remove_legacy_profile.return_value = True
        self.secrets = {}
        self.backend.keyring.lookup.side_effect = lambda ident: self.secrets.get(ident, "")
        self.backend.keyring.store.side_effect = lambda ident, secret: self.secrets.__setitem__(ident, secret)
        self.backend.keyring.forget.side_effect = lambda ident: self.secrets.pop(ident, None)
        self.messages = []

    def migrate(self):
        migrate_legacy(self.prefs, self.backend, self.messages.append, home=self.home, download_directory=self.downloads, target_data=self.destination)

    def legacy(self, imported=False):
        source = self.prefs.directory / "profiles/custom.ovpn" if imported else self.home / "Apps/simple-vpn/servers/nl-443.ovpn"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"remote example.test 443\n")
        old_id = legacy_profile_uuid(source)
        new_path = self.destination / ("profiles" if imported else "servers") / source.name
        new_id = profile_uuid(new_path)
        self.prefs.profile(old_id).update(username="fake-user", remembered=True)
        self.prefs.data["selected"] = old_id
        self.secrets[old_id] = "fake-secret"
        return source, old_id, new_path, new_id

    def test_remembered_password_and_username_survive_then_rerun_is_noop(self):
        source, old_id, target, new_id = self.legacy()
        self.migrate()
        self.assertEqual(self.prefs.profile(new_id), {"username": "fake-user", "remembered": True})
        self.assertEqual(self.secrets[new_id], "fake-secret")
        self.assertEqual(self.secrets[old_id], "fake-secret")
        self.assertEqual(self.prefs.data["selected"], new_id)
        self.assertTrue(source.exists())
        self.assertEqual(source.read_bytes(), target.read_bytes())
        calls = len(self.backend.mock_calls)
        self.migrate()
        self.assertEqual(len(self.backend.mock_calls), calls)
        self.assertTrue(self.prefs.data["migrated_v2"])

    def test_store_failure_keeps_old_secret_and_file_and_retries(self):
        source, old_id, target, new_id = self.legacy(imported=True)
        self.backend.keyring.store.side_effect = GLib.Error("locked")
        self.migrate()
        self.assertEqual(self.secrets[old_id], "fake-secret")
        self.assertTrue(source.exists())
        self.assertFalse(self.prefs.data["migrated_v2"])
        self.backend.keyring.forget.assert_not_called()
        self.assertTrue(self.messages)
        self.backend.keyring.store.side_effect = lambda ident, secret: self.secrets.__setitem__(ident, secret)
        self.migrate()
        self.assertEqual(self.secrets[new_id], "fake-secret")
        self.assertTrue(source.exists())
        self.assertTrue(target.exists())

    def test_new_secret_is_verified_and_original_is_retained(self):
        source, old_id, target, new_id = self.legacy(imported=True)
        self.backend.keyring.store.side_effect = lambda *_: None
        self.migrate()
        self.backend.keyring.forget.assert_not_called()
        self.assertTrue(source.exists())
        self.assertIn(old_id, self.secrets)
        self.assertFalse(self.prefs.data["migrated_v2"])

    def test_migration_never_forgets_old_keyring_entry(self):
        source, old_id, target, new_id = self.legacy()
        self.backend.keyring.forget.side_effect = GLib.Error("locked")
        self.migrate()
        self.assertEqual(self.secrets[new_id], "fake-secret")
        self.assertEqual(self.secrets[old_id], "fake-secret")
        self.backend.keyring.forget.assert_not_called()
        self.assertTrue(any("older keyring entries kept" in m for m in self.messages))

    def test_copy_conflict_preserves_both(self):
        source, old_id, target, new_id = self.legacy(imported=True)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"different")
        self.migrate()
        self.assertTrue(source.exists())
        self.assertEqual(target.read_bytes(), b"different")
        self.backend.keyring.forget.assert_not_called()

    def test_downloads_are_never_deleted(self):
        source = self.downloads / "nl-443.ovpn"
        source.write_bytes(b"test")
        self.migrate()
        self.assertTrue(source.exists())

    def test_migration_leaves_old_networkmanager_profiles_for_explicit_purge(self):
        self.legacy()
        self.backend.remove_legacy_profile.side_effect = GLib.Error("not permitted")
        self.migrate()
        self.assertTrue(self.prefs.data["migrated_v2"])
        self.backend.remove_legacy_profile.assert_not_called()
        self.assertTrue(any("NetworkManager profiles kept" in message for message in self.messages))

    def test_preferences_save_failure_keeps_old_secret_and_file(self):
        source, old_id, target, new_id = self.legacy(imported=True)
        self.prefs.save = MagicMock(side_effect=PermissionError())
        self.migrate()
        self.assertTrue(source.exists())
        self.assertIn(old_id, self.secrets)
        self.backend.keyring.forget.assert_not_called()

    def test_config_and_data_same_location_never_deletes_target(self):
        source, old_id, target, new_id = self.legacy(imported=True)
        self.destination = self.prefs.directory
        self.migrate()
        self.assertTrue(source.exists())

    def test_target_symlink_is_not_used_to_delete_source(self):
        source, old_id, target, new_id = self.legacy(imported=True)
        target.parent.mkdir(parents=True)
        target.symlink_to(source)
        self.migrate()
        self.assertTrue(source.exists())
        self.assertFalse(self.prefs.data["migrated_v2"])


class LabelTests(IsolatedCase):
    def label(self, content):
        from backend import server_label
        file = self.root / "arbitrary.ovpn"
        file.write_text(content)
        return server_label(file)

    def test_endpoint_and_transport_come_from_content(self):
        self.assertEqual(self.label("remote host.example 443\nproto tcp-client\n"), "host.example · TCP 443")
        self.assertEqual(self.label("proto udp6\nremote 2001:db8::1 1195\n"), "2001:db8::1 · UDP 1195")

    def test_remote_override_defaults_comments_and_first_endpoint(self):
        self.assertEqual(self.label('proto udp\nremote "one.example" 8443 tcp-client # comment\nremote two.example 123\n'), "one.example · TCP 8443")
        self.assertEqual(self.label("rport 443\nremote host.example\n"), "host.example · UDP 443")
        self.assertEqual(self.label("remote host.example\n"), "host.example · UDP 1194")

    def test_ignores_inline_certificate_text_and_falls_back_on_bad_file(self):
        self.assertEqual(self.label("<ca>\nremote fake.example 443\n</ca>\n"), "arbitrary")
        self.assertEqual(self.label('remote "broken\n'), "arbitrary")
        self.assertEqual(self.label("remote host.example 99999\n"), "arbitrary")
        from backend import server_label
        self.assertEqual(server_label(self.root / "missing.ovpn"), "missing")
