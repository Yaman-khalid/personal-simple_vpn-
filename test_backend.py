from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from backend import Cancelled, NetworkBackend, NM, Preferences, VPNError, safe_detail, validate_credentials

REAL_RUN = subprocess.run
REAL_POPEN = subprocess.Popen


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.patch("pathlib.Path.home", return_value=self.home)
        self.patch("paths.GLib.get_user_config_dir", return_value=str(self.root / "config"))
        self.patch("paths.GLib.get_user_data_dir", return_value=str(self.root / "data"))
        self.patch("paths.GLib.get_user_special_dir", return_value=str(self.root / "Downloads"))
        self.patch("backend.getpass.getuser", return_value="test-user")
        for name in ["backend.Gio.bus_get_sync", "backend.Secret.password_lookup_sync", "backend.Secret.password_store_sync", "backend.Secret.password_clear_sync", "backend.NM.VpnPluginInfo.new_from_file", "backend.subprocess.run", "backend.subprocess.Popen"]:
            self.patch(name, side_effect=AssertionError("A test attempted an unmocked system/service call: " + name))
        self.patch("backend.os.access", return_value=False)
        self.patch("backend.LOGGER.disabled", True)

    def patch(self, name, *args, **kwargs):
        patcher = patch(name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value


class CredentialsTests(IsolatedCase):
    def test_rejects_password_file_injection(self):
        for username, password in [("user", "secret\nvpn.secrets.cert-pass:injected"), ("user\rname", "secret"), ("user", "secret\x00")]:
            with self.subTest(username=username), self.assertRaises(VPNError):
                validate_credentials(username, password)

    def test_redacts_credentials_from_diagnostics(self):
        result = safe_detail("login alice password secret-value failed", "alice", "secret-value")
        self.assertNotIn("alice", result)
        self.assertNotIn("secret-value", result)

    def test_preferences_are_private_and_contain_no_password(self):
        with tempfile.TemporaryDirectory() as directory:
            prefs = Preferences(Path(directory) / "settings")
            prefs.profile("id")["username"] = "test-user"
            prefs.save()
            self.assertEqual(prefs.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(prefs.directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(Preferences(prefs.directory).profile("id")["username"], "test-user")
            self.assertNotIn("password", prefs.path.read_text())

    def test_password_passes_in_memory_not_in_command_or_environment(self):
        backend = NetworkBackend()
        observed = {}
        def start(argv, **kwargs):
            observed["argv"] = argv
            observed["env"] = kwargs["env"]
            fd, = kwargs["pass_fds"]
            observed["fd"] = fd
            observed["password_input"] = os.read(fd, 1000).decode()
            observed["mode"] = os.fstat(fd).st_mode & 0o777
            child = MagicMock()
            child.communicate.return_value = ("Connection activated", None)
            child.returncode = 0
            child.poll.return_value = 0
            return child
        with patch("backend.subprocess.Popen", side_effect=start):
            backend._activate("example-uuid", "super-secret-value", threading.Event())
        self.assertEqual(observed["password_input"], "vpn.secrets.password:super-secret-value\n")
        self.assertNotIn("super-secret-value", " ".join(observed["argv"]))
        self.assertNotIn("super-secret-value", str(observed["env"]))
        self.assertEqual(observed["mode"], 0o600)
        with self.assertRaises(OSError):
            os.fstat(observed["fd"])

    def test_real_process_can_read_inherited_anonymous_password_file(self):
        fd = os.memfd_create("test-vpn-secret", os.MFD_CLOEXEC)
        try:
            os.write(fd, b"vpn.secrets.password:fake-test-only\n")
            os.lseek(fd, 0, os.SEEK_SET)
            expected = hashlib.sha256(b"vpn.secrets.password:fake-test-only\n").hexdigest()
            with patch("backend.subprocess.Popen", REAL_POPEN):
                result = REAL_RUN(["/usr/bin/python3", "-I", "-S", "-c", "import hashlib,pathlib,sys; assert hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest() == sys.argv[2]", f"/proc/self/fd/{fd}", expected], pass_fds=(fd,), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        finally:
            os.close(fd)


class ConnectionTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.backend = NetworkBackend()
        self.backend.keyring = MagicMock()
        self.backend.keyring.lookup.return_value = "fake-password"
        self.backend.snapshot = MagicMock(return_value={"legacy": False, "active": [], "networking": True})
        self.backend.prepare_profile = MagicMock(return_value="fake-id")
        self.backend._activate = MagicMock()

    def test_running_old_service_blocks_new_tunnel(self):
        self.backend.snapshot.return_value["legacy"] = True
        with self.assertRaisesRegex(VPNError, "older Proton"):
            self.backend.connect(Path("test.ovpn"), "user", "fake-password", False, threading.Event())
        self.backend.prepare_profile.assert_not_called()
        self.backend._activate.assert_not_called()

    def test_other_vpn_blocks_new_tunnel(self):
        self.backend.snapshot.return_value["active"] = [{"uuid": "another-vpn"}]
        with self.assertRaisesRegex(VPNError, "Another VPN"):
            self.backend.connect(Path("test.ovpn"), "user", "fake-password", False, threading.Event())
        self.backend._activate.assert_not_called()

    def test_cancel_before_connect_never_registers_profile(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            self.backend.connect(Path("test.ovpn"), "user", "fake-password", True, cancel)
        self.backend.prepare_profile.assert_not_called()
        self.backend._activate.assert_not_called()
        self.backend.keyring.store.assert_not_called()

    def test_only_remembers_password_after_success(self):
        self.backend._activate.side_effect = VPNError("fake-password failed for user")
        with self.assertRaises(VPNError) as caught:
            self.backend.connect(Path("test.ovpn"), "user", "fake-password", True, threading.Event())
        self.assertNotIn("fake-password", str(caught.exception))
        self.backend.keyring.store.assert_not_called()

    def test_keyring_failure_does_not_misreport_successful_tunnel(self):
        self.backend.keyring.store.side_effect = RuntimeError("keyring locked")
        result = self.backend.connect(Path("test.ovpn"), "user", "fake-password", True, threading.Event())
        self.assertIn("Connected", result)
        self.assertIn("not saved", result)

    def test_disconnect_does_not_touch_other_vpn(self):
        backend = NetworkBackend()
        backend.snapshot = MagicMock(return_value={"active": [{"uuid": "other"}]})
        with patch("backend.subprocess.run") as run:
            backend.disconnect("ours")
        run.assert_not_called()

    def test_cancelling_pending_activation_deactivates_exact_tunnel(self):
        backend = NetworkBackend()
        cancel = threading.Event()
        cancel.set()
        backend.snapshot = MagicMock(return_value={"active": [{"uuid": "ours"}]})
        backend.disconnect = MagicMock()
        child = MagicMock()
        child.communicate.return_value = ("cancelled", None)
        child.poll.return_value = 0
        with patch("backend.subprocess.Popen", return_value=child):
            with self.assertRaises(Cancelled):
                backend._activate("ours", "fake-password", cancel)
        backend.disconnect.assert_called_once_with("ours")
        child.terminate.assert_called_once()

    def test_cancel_waits_for_in_flight_activation_before_cleaning_up(self):
        backend = NetworkBackend()
        cancel = threading.Event()
        cancel.set()
        backend.snapshot = MagicMock(side_effect=[{"active": []}, {"active": [{"uuid": "ours"}]}])
        backend.disconnect = MagicMock()
        child = MagicMock()
        child.communicate.side_effect = [subprocess.TimeoutExpired("nmcli", 0.25), ("cancelled", None)]
        child.poll.return_value = 0
        with patch("backend.subprocess.Popen", return_value=child):
            with self.assertRaises(Cancelled):
                backend._activate("ours", "fake-password", cancel)
        self.assertEqual(backend.snapshot.call_count, 2)
        backend.disconnect.assert_called_once_with("ours")

    def test_generated_profile_has_no_saved_secret_and_no_autoconnect(self):
        backend = NetworkBackend()
        connection = NM.SimpleConnection.new()
        setting = NM.SettingConnection.new()
        setting.set_property("id", "test")
        setting.set_property("uuid", "17738bfe-f06a-4831-a756-48fb8a83b87e")
        connection.add_setting(setting)
        vpn = NM.SettingVpn.new()
        vpn.set_property("service-type", "org.freedesktop.NetworkManager.openvpn")
        for key, value in {"connection-type": "password", "remote-cert-tls": "server", "ca": "/tmp/test-ca.pem", "remote": "example.test:443"}.items():
            vpn.add_data_item(key, value)
        connection.add_setting(vpn)
        plugin = MagicMock()
        plugin.load_editor_plugin.return_value.import_.return_value = connection
        backend._call = MagicMock()
        backend._connection_path = MagicMock(return_value="/settings/test")
        backend._require_owned_profile = MagicMock()
        plugin_file = self.root / "plugin.name"
        plugin_file.touch()
        self.patch("backend.PLUGIN_FILES", (plugin_file,))
        with tempfile.NamedTemporaryFile(suffix=".ovpn") as config, patch("backend.NM.VpnPluginInfo.new_from_file", return_value=plugin):
            backend.prepare_profile(Path(config.name), "example-user")
        settings = backend._call.call_args.args[3].unpack()[0]
        self.assertFalse(settings["connection"]["autoconnect"])
        self.assertTrue(settings["connection"]["permissions"])
        self.assertEqual(settings["vpn"]["data"]["password-flags"], "2")
        self.assertNotIn("secrets", settings["vpn"])
        self.assertEqual(settings["ipv4"]["dns-priority"], -50)
        self.assertEqual(backend._call.call_args.args[3].unpack()[1], 2)


if __name__ == "__main__":
    unittest.main()
