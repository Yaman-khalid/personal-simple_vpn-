from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

from backend import APP_ID, GLib, Preferences
from install import InstallError, Layout, RUNTIME_FILES, Transaction, install, uninstall
from test_backend import IsolatedCase


class InstallerTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.source = self.root / "source"
        self.source.mkdir()
        for name in RUNTIME_FILES:
            (self.source / name).write_text("original " + name)
        self.layout = Layout(self.root / "xdg-data/simple-vpn", self.root / "xdg-config/simple-vpn", self.home, self.root / "Downloads")
        self.messages = []
        self.patch("install.shutil.which", return_value=None)

    def run_install(self, **kwargs):
        return install(self.source, self.layout, report=self.messages.append, **kwargs)

    def tree(self):
        return {str(p.relative_to(self.root)): (p.stat().st_mode & 0o777, p.read_bytes() if p.is_file() else None) for p in self.root.rglob("*")}

    def user_data(self):
        files = [self.layout.data / "servers/existing.ovpn", self.layout.data / "profiles/custom.ovpn", self.layout.config / "preferences.json"]
        for file in files:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("keep " + file.name)
        return {file: file.read_bytes() for file in files}

    def test_missing_validators_are_optional_and_runtime_files_only(self):
        self.run_install()
        self.assertEqual({f.name for f in self.layout.app.iterdir()}, set(RUNTIME_FILES))
        for file in self.layout.app.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.layout.app.stat().st_mode & 0o777, 0o700)
        self.assertTrue(any("unavailable" in message for message in self.messages))
        self.assertEqual(self.layout.launcher.name, APP_ID + ".desktop")
        self.assertIn("Icon=" + APP_ID, self.layout.launcher.read_text())
        self.assertIn("Keywords=VPN;Proton;OpenVPN;", self.layout.launcher.read_text())

    def test_validation_failure_leaves_nothing_behind(self):
        self.patch("install.shutil.which", return_value="/fake/desktop-file-validate")
        self.patch("install.subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="invalid launcher"))
        before = self.tree()
        with self.assertRaisesRegex(InstallError, "invalid launcher"):
            self.run_install()
        self.assertEqual(self.tree(), before)

    def test_only_own_launcher_validated_no_cache_or_other_desktop_staging(self):
        self.layout.launcher.parent.mkdir(parents=True)
        unrelated = self.layout.launcher.parent / "unrelated.desktop"
        unrelated.write_text("private unrelated launcher")
        cache = self.layout.launcher.parent / "mimeinfo.cache"
        cache.write_text("keep original cache")
        which = self.patch("install.shutil.which", return_value="/fake/desktop-file-validate")
        run = self.patch("install.subprocess.run", return_value=MagicMock(returncode=0))
        self.run_install()
        which.assert_called_once_with("desktop-file-validate")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(run.call_args.args[0]), 2)
        self.assertEqual(cache.read_text(), "keep original cache")
        backup = self.run_install(upgrade=True)
        self.assertFalse((backup / "desktop-validation").exists())
        self.assertFalse(any(p.name == unrelated.name for p in backup.rglob("*")))
        run.reset_mock()
        uninstall(self.layout, report=self.messages.append)
        run.assert_not_called()
        self.assertEqual(cache.read_text(), "keep original cache")

    def test_copy_failure_leaves_nothing_behind(self):
        self.patch("install.copy_private", side_effect=OSError("copy failed"))
        before = self.tree()
        with self.assertRaises(OSError):
            self.run_install()
        self.assertEqual(self.tree(), before)

    def test_keyboard_interrupt_rolls_back(self):
        self.patch("install.copy_private", side_effect=KeyboardInterrupt())
        before = self.tree()
        with self.assertRaises(KeyboardInterrupt):
            self.run_install()
        self.assertEqual(self.tree(), before)

    def test_mid_commit_failure_rolls_back_fresh_install(self):
        original = Transaction.place
        def fail_at_icon(transaction, staged, target):
            if target == self.layout.icon:
                raise OSError("injected failure")
            return original(transaction, staged, target)
        self.patch("install.Transaction.place", fail_at_icon)
        before = self.tree()
        with self.assertRaises(OSError):
            self.run_install()
        self.assertEqual(self.tree(), before)

    def test_upgrade_keeps_user_data_and_old_code_backup(self):
        self.run_install()
        data = self.user_data()
        (self.source / "app.py").write_text("upgraded")
        backup = self.run_install(upgrade=True)
        self.assertEqual((self.layout.app / "app.py").read_text(), "upgraded")
        self.assertEqual((backup / "app/app.py").read_text(), "original app.py")
        for file, original in data.items():
            self.assertEqual(file.read_bytes(), original)

    def test_failed_upgrade_restores_old_code_and_launcher(self):
        self.run_install()
        (self.source / "app.py").write_text("upgraded")
        original = Transaction.place
        def fail_at_launcher(transaction, staged, target):
            if target == self.layout.launcher:
                raise OSError("injected upgrade failure")
            return original(transaction, staged, target)
        self.patch("install.Transaction.place", fail_at_launcher)
        before = self.tree()
        with self.assertRaises(OSError):
            self.run_install(upgrade=True)
        self.assertEqual(self.tree(), before)

    def test_already_installed_explains_flags(self):
        self.run_install()
        with self.assertRaisesRegex(InstallError, "--upgrade.*--uninstall"):
            self.run_install()

    def test_normal_uninstall_keeps_data_and_never_calls_services(self):
        self.run_install()
        data = self.user_data()
        purge = self.patch("install.purge_services")
        uninstall(self.layout, report=self.messages.append)
        for file, original in data.items():
            self.assertEqual(file.read_bytes(), original)
        self.assertFalse(self.layout.app.exists())
        self.assertFalse(self.layout.launcher.exists())
        self.assertFalse(self.layout.icon.exists())
        purge.assert_not_called()

    def test_uninstall_when_not_installed_creates_nothing(self):
        before = self.tree()
        uninstall(self.layout, report=self.messages.append)
        self.assertEqual(self.tree(), before)
        self.assertFalse(any("Removed installed path" in m for m in self.messages))

    def test_purge_requires_exact_confirmation(self):
        self.run_install()
        before = self.tree()
        with self.assertRaisesRegex(InstallError, "Purge cancelled"):
            uninstall(self.layout, purge=True, confirm=lambda _: "YES", report=self.messages.append)
        self.assertEqual(self.tree(), before)

    def test_purge_without_interactive_input_is_cancelled(self):
        self.run_install()
        before = self.tree()
        def no_input(prompt):
            raise EOFError()
        with self.assertRaisesRegex(InstallError, "Purge cancelled"):
            uninstall(self.layout, purge=True, confirm=no_input, report=self.messages.append)
        self.assertEqual(self.tree(), before)

    def test_purge_handles_mocked_service_failures_and_keeps_foreign_profiles(self):
        self.run_install()
        self.user_data()
        preferences = Preferences(self.layout.config)
        preferences.data = {"profiles": {"owned-id": {"remembered": True}}}
        preferences.save()
        backend = MagicMock()
        backend.keyring.forget.side_effect = GLib.Error("locked")
        backend.remove_legacy_profile.return_value = False
        def call(path, interface, method, *args):
            if method == "ListConnections":
                value = ["/own", "/other"]
            else:
                value = {"connection": {"uuid": "owned-id" if path == "/own" else "foreign-id", "id": "Simple VPN · test" if path == "/own" else "Other VPN"}}
            return MagicMock(unpack=lambda: (value,))
        backend._call.side_effect = call
        uninstall(self.layout, purge=True, confirm=lambda _: "yes", backend=backend, report=self.messages.append)
        backend.remove_legacy_profile.assert_called_once_with("owned-id")
        self.assertFalse(self.layout.data.exists())
        self.assertFalse(self.layout.config.exists())
        self.assertTrue(any("/usr/bin/python3" in message for message in self.messages))
        self.assertTrue(any("/usr/bin/nmcli connection delete uuid owned-id" in message for message in self.messages))

    def test_legacy_servers_are_copied_and_legacy_folder_kept(self):
        old = self.home / "Apps/simple-vpn/servers/nl-443.ovpn"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"remote example.test 443\n")
        self.run_install()
        self.assertEqual((self.layout.data / "servers/nl-443.ovpn").read_bytes(), old.read_bytes())
        self.assertTrue(old.exists())
        self.assertTrue(any("Legacy install kept" in message for message in self.messages))

    def test_partial_uninstall_reports_only_paths_that_existed(self):
        self.run_install()
        self.layout.icon.unlink()
        self.messages.clear()
        uninstall(self.layout, report=self.messages.append)
        removed = [m for m in self.messages if m.startswith("Removed installed path:")]
        self.assertEqual(removed, ["Removed installed path: " + str(self.layout.app), "Removed installed path: " + str(self.layout.launcher)])

    def test_backup_maintenance_removes_validation_and_keeps_three_newest(self):
        from install import maintain_backups
        parent = self.layout.data.parent
        parent.mkdir(parents=True)
        backups = []
        for index in range(5):
            backup = parent / (".simple-vpn-backup-fixture-" + str(index))
            validation = backup / "desktop-validation"
            validation.mkdir(parents=True)
            (validation / "unrelated.desktop").write_text("old fixture")
            (backup / "old-app.py").write_text(str(index))
            os.utime(backup, ns=(1000000000 + index, 1000000000 + index))
            backups.append(backup)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        (parent / ".simple-vpn-backup-symlink").symlink_to(outside, target_is_directory=True)
        maintain_backups(parent, self.messages.append)
        self.assertEqual([p.exists() for p in backups], [False, False, True, True, True])
        for backup in backups[2:]:
            self.assertFalse((backup / "desktop-validation").exists())
            self.assertEqual((backup / "old-app.py").read_text(), backup.name[-1])
        self.assertEqual((outside / "keep").read_text(), "keep")
        maintain_backups(parent, self.messages.append)
        self.assertEqual([p.exists() for p in backups], [False, False, True, True, True])

    def test_upgrade_and_uninstall_automatically_limit_backups(self):
        self.run_install()
        for index in range(5):
            (self.source / "app.py").write_text(str(index))
            self.run_install(upgrade=True)
        self.assertEqual(len(list(self.layout.data.parent.glob(".simple-vpn-backup-*"))), 3)
        uninstall(self.layout, report=self.messages.append)
        self.assertEqual(len(list(self.layout.data.parent.glob(".simple-vpn-backup-*"))), 3)
        self.assertTrue(any("Removed older recovery backup" in m for m in self.messages))

    def test_purge_prompt_discloses_shared_legacy_config_and_keyring(self):
        prompt = MagicMock(return_value="no")
        with self.assertRaises(InstallError):
            uninstall(self.layout, purge=True, confirm=prompt, report=self.messages.append)
        text = prompt.call_args.args[0]
        self.assertIn(str(self.layout.config), text)
        self.assertIn("keyring entries SHARED WITH THE LEGACY APP", text)

    def test_explicit_purge_removes_legacy_imports_and_both_credential_ids(self):
        from backend import legacy_profile_uuid, profile_uuid
        old = self.layout.config / "profiles/old.ovpn"
        old.parent.mkdir(parents=True)
        old.write_text("remote fake.example 443\n")
        backend = MagicMock()
        backend._call.return_value.unpack.return_value = ([],)
        uninstall(self.layout, purge=True, confirm=lambda _: "yes", backend=backend, report=self.messages.append)
        backend.keyring.forget.assert_any_call(legacy_profile_uuid(old))
        backend.keyring.forget.assert_any_call(profile_uuid(old))
        self.assertFalse(old.exists())
