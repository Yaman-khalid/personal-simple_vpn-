"""Per-user staged installation, atomic swaps, rollback, and explicit removal."""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

from backend import APP_ID, APP_PREFIX, NetworkBackend, Preferences, SERVICE, ROOT, import_profile_file, legacy_profile_uuid, profile_uuid
from paths import config_dir, data_dir, downloads_dir

RUNTIME_FILES = ("app.py", "backend.py", "paths.py", "migration.py", "icon.svg", "README.md")


class InstallError(Exception):
    pass


@dataclass(frozen=True)
class Layout:
    data: Path
    config: Path
    home: Path
    downloads: Path

    @property
    def app(self):
        return self.data / "app"

    @property
    def launcher(self):
        return self.data.parent / "applications" / (APP_ID + ".desktop")

    @property
    def icon(self):
        return self.data.parent / "icons/hicolor/scalable/apps" / (APP_ID + ".svg")

    @classmethod
    def current(cls):
        return cls(data_dir(), config_dir(), Path.home(), downloads_dir())


def atomic_exchange(left: Path, right: Path) -> None:
    """Linux renameat2(RENAME_EXCHANGE), without an unsafe fallback."""
    library = ctypes.CDLL(None, use_errno=True)
    exchange = getattr(library, "renameat2", None)
    if exchange is None:
        raise InstallError("Atomic upgrades need Linux renameat2; the old installation was kept.")
    exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    exchange.restype = ctypes.c_int
    if exchange(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(right))


class Transaction:
    def __init__(self, parent: Path):
        self.created = []
        self.actions = []
        self.stage = None
        try:
            self.mkdir(parent)
            self.stage = Path(tempfile.mkdtemp(prefix=".simple-vpn-stage-", dir=parent))
        except BaseException:
            self.remove_empty_dirs()
            raise

    def mkdir(self, directory: Path):
        missing = []
        cursor = directory
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for path in reversed(missing):
            path.mkdir(mode=0o700)
            self.created.append(path)

    def place(self, staged: Path, destination: Path):
        self.mkdir(destination.parent)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink():
                raise InstallError("Refusing to replace a symlink: " + str(destination))
            atomic_exchange(staged, destination)
            self.actions.append(("exchange", staged, destination))
        else:
            os.replace(staged, destination)
            self.actions.append(("move", staged, destination))

    def remove(self, destination: Path):
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink():
                raise InstallError("Refusing to remove a symlink: " + str(destination))
            saved = self.stage / ("removed-" + str(len(self.actions)))
            os.replace(destination, saved)
            self.actions.append(("remove", saved, destination))

    def remove_empty_dirs(self):
        for directory in reversed(self.created):
            try:
                directory.rmdir()
            except OSError:
                pass

    def rollback(self):
        errors = []
        for kind, staged, destination in reversed(self.actions):
            try:
                if kind == "exchange":
                    atomic_exchange(staged, destination)
                elif kind == "move":
                    os.replace(destination, staged)
                else:
                    os.replace(staged, destination)
            except Exception as error:
                errors.append(type(error).__name__)
        if errors:
            raise InstallError("Rollback incomplete. Recovery files kept at " + str(self.stage) + "; errors: " + ", ".join(errors))
        shutil.rmtree(self.stage)
        self.remove_empty_dirs()

    def finish(self) -> Path | None:
        if any(kind != "move" for kind, _, _ in self.actions):
            backup = self.stage.with_name(self.stage.name.replace("-stage-", "-backup-", 1))
            os.replace(self.stage, backup)
            return backup
        shutil.rmtree(self.stage)
        return None


def copy_private(source: Path, destination: Path):
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as original:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as copied:
            shutil.copyfileobj(original, copied)
    if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(destination.read_bytes()).digest():
        raise InstallError("Copied file did not verify: " + source.name)


def desktop_text(app: Path) -> str:
    if any(character in str(app) for character in ("\n", "\r", "\x00")):
        raise InstallError("The application path contains unsupported control characters.")
    argument = str(app / "app.py").replace("%", "%%")
    argument = argument.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    argument = argument.replace("\\", "\\\\")
    return (
        "[Desktop Entry]\nType=Application\nName=Simple VPN\n"
        "Comment=Connect to your Proton VPN server\n"
        f'Exec=/usr/bin/python3 "{argument}"\n'
        "TryExec=/usr/bin/python3\n" + f"Icon={APP_ID}\n"
        "Terminal=false\nCategories=Network;\nKeywords=VPN;Proton;OpenVPN;\n"
        "StartupNotify=true\n" + f"StartupWMClass={APP_ID}\n"
    )


def validator(name: str, arguments: list[str], report) -> bool:
    executable = shutil.which(name)
    if executable is None:
        report("Warning: " + name + " is unavailable; continuing without it.")
        return False
    try:
        result = subprocess.run([executable, *arguments], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError(name + " could not run (" + type(error).__name__ + "); nothing committed.") from None
    if result.returncode:
        raise InstallError(name + " failed: " + (result.stderr or result.stdout).strip()[:1200])
    return True


def maintain_backups(parent: Path, report) -> None:
    """Remove obsolete validation fixtures and retain the three newest backups."""
    try:
        backups = [p for p in parent.glob(".simple-vpn-backup-*") if not p.is_symlink() and p.is_dir()]
        backups.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
        for backup in backups:
            timestamps = backup.stat()
            validation = backup / "desktop-validation"
            if validation.is_symlink():
                validation.unlink()
                report("Removed backup validation symlink: " + str(validation))
            elif validation.exists():
                shutil.rmtree(validation)
                report("Removed backup validation files: " + str(validation))
            os.utime(backup, ns=(timestamps.st_atime_ns, timestamps.st_mtime_ns))
        for backup in backups[3:]:
            shutil.rmtree(backup)
            report("Removed older recovery backup: " + str(backup))
    except OSError as error:
        report("Backup maintenance incomplete (" + type(error).__name__ + "). Inspect: " + str(parent))


def install(source: Path, layout: Layout, *, upgrade=False, report=print):
    if not upgrade and any(p.exists() or p.is_symlink() for p in (layout.app, layout.launcher, layout.icon)):
        raise InstallError("Already installed. Use --upgrade to replace code, or --uninstall [--purge] to remove it.")
    for name in RUNTIME_FILES:
        if not (source / name).is_file():
            raise InstallError("Missing runtime source: " + name)
    transaction = Transaction(layout.data.parent)
    report("Staging directory: " + str(transaction.stage))
    copied_servers = []
    try:
        app = transaction.stage / "app"
        app.mkdir(mode=0o700)
        for name in RUNTIME_FILES:
            copy_private(source / name, app / name)
        icon = transaction.stage / "icon.svg"
        copy_private(source / "icon.svg", icon)
        launcher = transaction.stage / (APP_ID + ".desktop")
        text = desktop_text(layout.app)
        launcher.write_text(text)
        launcher.chmod(0o600)
        validator("desktop-file-validate", [str(launcher)], report)
        legacy = layout.home / "Apps/simple-vpn"
        candidates = sorted((legacy / "servers").glob("*.ovpn"))
        candidates += [layout.downloads / n for n in ("nl-443.ovpn", "nl-free-2.protonvpn.tcp.ovpn")]
        for original in candidates:
            destination = layout.data / "servers" / original.name
            if not original.exists() or destination.exists() or any(p.name == original.name for p in copied_servers):
                continue
            copied_servers.append(import_profile_file(original, transaction.stage / "servers"))
        transaction.place(app, layout.app)
        transaction.place(icon, layout.icon)
        transaction.place(launcher, layout.launcher)
        for server in copied_servers:
            transaction.place(server, layout.data / "servers" / server.name)
        for name in RUNTIME_FILES:
            if (layout.app / name).read_bytes() != (source / name).read_bytes():
                raise InstallError("Installed runtime did not verify: " + name)
        backup = transaction.finish()
    except BaseException:
        transaction.rollback()
        raise
    for directory in transaction.created:
        report("Created directory: " + str(directory))
    for name in RUNTIME_FILES:
        report("Installed: " + str(layout.app / name))
    for path in [layout.app, layout.launcher, layout.icon, *[layout.data / "servers" / p.name for p in copied_servers]]:
        report("Installed: " + str(path))
    if backup:
        report("Previous files retained for recovery: " + str(backup))
    maintain_backups(layout.data.parent, report)
    if legacy.exists():
        report("Legacy install kept: " + str(legacy) + ". Delete it manually only after checking the new app works.")
    return backup


def purge_services(layout: Layout, report, backend=None):
    backend = backend if backend is not None else NetworkBackend(Preferences(layout.config))
    preferences = Preferences(layout.config)
    identities = set(preferences.data.get("profiles", {}))
    for directory in [layout.data / "servers", layout.data / "profiles", layout.home / "Apps/simple-vpn/servers", layout.config / "profiles"]:
        for file in directory.glob("*.ovpn"):
            identities.update((profile_uuid(file), legacy_profile_uuid(file)))
    for ident in identities:
        try:
            backend.keyring.forget(ident)
        except Exception as error:
            code = "from backend import Keyring; Keyring().forget(" + repr(ident) + ")"
            report("Keyring cleanup failed (" + type(error).__name__ + "). From this source directory run: " + shlex.join(["/usr/bin/python3", "-c", code]))
    try:
        connections = backend._call(ROOT + "/Settings", SERVICE + ".Settings", "ListConnections", None, "(ao)").unpack()[0]
        for path in connections:
            settings = backend._call(path, SERVICE + ".Settings.Connection", "GetSettings", None, "(a{sa{sv}})").unpack()[0]
            connection = settings.get("connection", {})
            if not connection.get("id", "").startswith(APP_PREFIX):
                continue
            ident = connection["uuid"]
            try:
                if not backend.remove_legacy_profile(ident):
                    raise InstallError("Profile is still active")
            except Exception as error:
                report("App-owned profile kept (" + type(error).__name__ + "). Disconnect it, then run: " + shlex.join(["/usr/bin/nmcli", "connection", "delete", "uuid", ident]))
    except Exception as error:
        report("NetworkManager cleanup incomplete (" + type(error).__name__ + "). Review app-owned profiles with: /usr/bin/nmcli connection show")


def uninstall(layout: Layout, *, purge=False, confirm=input, report=print, backend=None):
    if purge:
        try:
            answer = confirm("Permanently remove Simple VPN data, the config directory " + str(layout.config) + " and saved keyring entries SHARED WITH THE LEGACY APP, plus inactive app-owned VPN profiles? The legacy app may lose its settings, imported configs and saved passwords. Type yes: ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "yes":
            raise InstallError("Purge cancelled; nothing removed.")
    backup = None
    removed = [path for path in (layout.app, layout.launcher, layout.icon) if path.exists() or path.is_symlink()]
    if removed:
        transaction = Transaction(layout.data.parent)
        report("Staging directory: " + str(transaction.stage))
        try:
            for path in removed:
                transaction.remove(path)
            backup = transaction.finish()
        except BaseException:
            transaction.rollback()
            raise
    else:
        report("No installed runtime was found.")
    for path in removed:
        report("Removed installed path: " + str(path))
    if backup:
        report("Removed installation retained for recovery: " + str(backup))
    maintain_backups(layout.data.parent, report)
    if purge:
        try:
            purge_services(layout, report, backend)
        except Exception as error:
            report("Service cleanup incomplete (" + type(error).__name__ + "); retry from this source with --uninstall --purge.")
        for path in dict.fromkeys([layout.data, layout.config]):
            try:
                if path.is_symlink():
                    raise InstallError("Refusing to purge a symlink")
                if path.exists():
                    shutil.rmtree(path)
                report("Purged: " + str(path))
            except Exception as error:
                report("Purge failed (" + type(error).__name__ + "). After inspecting this path, remove it manually: " + shlex.join(["/usr/bin/rm", "-r", "--", str(path)]))
    else:
        report("Kept user data: " + str(layout.data))
        report("Kept preferences: " + str(layout.config))
    report("Legacy install folders are kept. Only the three newest recovery backups are retained.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install Simple VPN for the current user")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--upgrade", action="store_true")
    operation.add_argument("--uninstall", action="store_true")
    parser.add_argument("--purge", action="store_true")
    args = parser.parse_args(argv)
    if args.purge and not args.uninstall:
        parser.error("--purge requires --uninstall")
    try:
        layout = Layout.current()
        if args.uninstall:
            uninstall(layout, purge=args.purge)
        else:
            install(Path(__file__).resolve().parent, layout, upgrade=args.upgrade)
    except (InstallError, OSError) as error:
        print("Installation operation failed: " + str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
