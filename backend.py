"""Local VPN backend. Passwords travel to nmcli through an anonymous RAM file."""
from __future__ import annotations

import getpass
from functools import wraps
import json
import logging
import os
from pathlib import Path
import stat
import shlex
import subprocess
import threading
import time
import traceback
import uuid

import gi
gi.require_version("NM", "1.0")
gi.require_version("Secret", "1")
from gi.repository import Gio, GLib, NM, Secret

from paths import config_dir

APP_ID = "io.github.simplevpn.Desktop"
APP_PREFIX = "Simple VPN · "
LEGACY_UNIT = "openvpn-client@proton.service"
MAX_PROFILE_BYTES = 2 * 1024 * 1024
PLUGIN_FILES = (
    Path("/usr/lib/NetworkManager/VPN/nm-openvpn-service.name"),
    Path("/etc/NetworkManager/VPN/nm-openvpn-service.name"),
)
ROOT = "/org/freedesktop/NetworkManager"
SERVICE = "org.freedesktop.NetworkManager"
SCHEMA = Secret.Schema.new(
    "io.github.simplevpn.Credentials", Secret.SchemaFlags.NONE,
    {"profile": Secret.SchemaAttributeType.STRING},
)
LOGGER = logging.getLogger("simple_vpn")
LOGGER.propagate = False
LOGGER.setLevel(logging.DEBUG)
LOGGER.disabled = os.environ.get("SIMPLE_VPN_DEBUG") != "1"
if not LOGGER.handlers:
    LOGGER.addHandler(logging.StreamHandler())


class VPNError(Exception):
    pass


class Cancelled(VPNError):
    pass


def profile_uuid(path: Path) -> str:
    path = Path(path)
    category = "profiles" if path.parent.name == "profiles" else "servers"
    key = category + "/" + path.name
    return str(uuid.uuid5(uuid.NAMESPACE_URL, APP_ID + ":" + key))


def legacy_profile_uuid(path: Path) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, APP_ID + ":" + str(path.resolve())))


def validate_credentials(username: str, password: str) -> None:
    if not username.strip():
        raise VPNError("Enter your Proton OpenVPN username.")
    if not password:
        raise VPNError("Enter your OpenVPN password, or use a saved password.")
    if any(c in username + password for c in ("\r", "\n", "\x00")):
        raise VPNError("Credentials cannot contain line breaks or null characters.")
    if len(username) > 512 or len(password) > 4096:
        raise VPNError("These credentials are too long. Check your OpenVPN credentials.")


def safe_detail(message: str, *secrets: str) -> str:
    for value in sorted((s for s in secrets if s), key=len, reverse=True):
        message = message.replace(value, "[hidden]")
    return message.strip()[:1200]


def debug_exception(error: Exception, *secrets: str) -> None:
    if not LOGGER.disabled:
        detail = "".join(traceback.format_exception(type(error), error, error.__traceback__, chain=False))
        LOGGER.debug("%s: %s", type(error).__name__, safe_detail(detail, *secrets))


def guarded(callback=None, *, keep_alive=False):
    """Guard Window signal handlers and worker completions without importing GTK."""
    if callback is None:
        return lambda function: guarded(function, keep_alive=keep_alive)
    @wraps(callback)
    def wrapped(self, *args, **kwargs):
        secrets = []
        for name in ("username", "password"):
            try:
                secrets.append(getattr(self, name).get_text())
            except Exception:
                pass
        try:
            return callback(self, *args, **kwargs)
        except Exception as error:
            debug_exception(error, *secrets)
            self.busy = self.connecting = self.polling = False
            if hasattr(self, "migrating"):
                self.migrating = False
            try:
                self.set_status("connected" if self.connected else "disconnected")
                self.show_message(f"Unexpected interface error ({type(error).__name__}). Try again; enable SIMPLE_VPN_DEBUG=1 for details.")
            except Exception:
                pass  # Do not let a second UI error escape the signal handler.
            return GLib.SOURCE_CONTINUE if keep_alive else GLib.SOURCE_REMOVE
    return wrapped


def activation_error(code: int, output: str, *secrets: str) -> VPNError:
    detail = safe_detail(output, *secrets)
    if code == 3:
        return VPNError("The VPN connection timed out. Check your network or try another configuration.")
    if code == 8:
        return VPNError("NetworkManager is not running. Start it, then try again.")
    # VERIFY ON REAL SYSTEM: actual nmcli/plugin wording varies with versions.
    phrases = ("no secrets", "no valid secrets", "login failed", "authentication failed", "auth_failed")
    if code == 4 and any(phrase in detail.lower() for phrase in phrases):
        return VPNError("Authentication failed. Check your Proton OpenVPN credentials (different from your account login).")
    return VPNError("The VPN could not connect." + (" " + detail if detail else ""))


def read_profile_file(source: str | Path | None) -> bytes:
    if source is None:
        raise VPNError("Choose a local file. Remote files are not supported.")
    path = Path(source)
    if path.suffix.lower() != ".ovpn":
        raise VPNError("Choose an OpenVPN .ovpn configuration.")
    try:
        # NONBLOCK prevents a FIFO swapped in before open from blocking the UI.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise VPNError("That configuration is not a regular file.")
            if info.st_size > MAX_PROFILE_BYTES:
                raise VPNError("That configuration is too large. The limit is 2 MiB.")
            content = stream.read(MAX_PROFILE_BYTES + 1)
            if len(content) > MAX_PROFILE_BYTES:
                raise VPNError("That configuration is too large. The limit is 2 MiB.")
            return content
    except VPNError:
        raise
    except OSError:
        raise VPNError("That configuration file is not readable.") from None


def server_label(path: Path) -> str:
    """Describe the first endpoint; display metadata only, not an OpenVPN parser."""
    try:
        remote, protocol, port, inline = None, "udp", "1194", None
        for line in read_profile_file(path).decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if inline:
                if line.lower() == "</" + inline + ">":
                    inline = None
                continue
            if line.startswith("<") and line.endswith(">"):
                tag = line[1:-1].lower()
                if tag not in ("connection", "/connection") and not tag.startswith("/"):
                    inline = tag
                continue
            lexer = shlex.shlex(line, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = "#;"
            parts = list(lexer)
            if not parts:
                continue
            key = parts[0].removeprefix("--").lower()
            if key == "remote" and len(parts) >= 2 and remote is None:
                remote = parts[1:4]
            elif key == "proto" and len(parts) == 2:
                protocol = parts[1]
            elif key in ("port", "rport") and len(parts) == 2:
                port = parts[1]
        if not remote:
            return path.stem
        host = remote[0]
        port = remote[1] if len(remote) > 1 else port
        protocol = remote[2] if len(remote) > 2 else protocol
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            return path.stem
        if protocol not in ("tcp", "tcp-client", "tcp4", "tcp4-client", "tcp6", "tcp6-client", "udp", "udp4", "udp6"):
            return path.stem
        transport = "TCP" if protocol.startswith("tcp") else "UDP"
        return f"{host} · {transport} {port}"
    except (VPNError, ValueError, OSError):
        return path.stem


def import_profile_file(source: str | Path | None, directory: Path) -> Path:
    """Bounded local-file import, with no GTK, services, or credential access."""
    created = False
    target = None
    try:
        content = read_profile_file(source)
        target = Path(directory) / Path(source).name
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return target
    except VPNError:
        raise
    except FileExistsError:
        raise VPNError("A configuration with that name is already imported.") from None
    except Exception as error:
        if created and target is not None:
            try:
                target.unlink()
            except OSError:
                pass
        debug_exception(error)
        if isinstance(error, OSError):
            raise VPNError("The imported configuration could not be saved. Check folder permissions and free space.") from None
        raise VPNError(f"Unexpected import error ({type(error).__name__}). Run with SIMPLE_VPN_DEBUG=1 for details.") from None


class Preferences:
    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory) if directory is not None else config_dir()
        self.path = self.directory / "preferences.json"
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.data, stream, indent=2)
        os.replace(temporary, self.path)

    def profile(self, ident: str) -> dict:
        return self.data.setdefault("profiles", {}).setdefault(ident, {})


class Keyring:
    def lookup(self, ident: str) -> str:
        try:
            return Secret.password_lookup_sync(SCHEMA, {"profile": ident}, None) or ""
        except GLib.Error:
            raise VPNError("Saved password unavailable (keyring locked or missing). Enter it manually.") from None

    def store(self, ident: str, password: str):
        try:
            stored = Secret.password_store_sync(
                SCHEMA, {"profile": ident}, Secret.COLLECTION_DEFAULT,
                "Simple VPN · Proton OpenVPN", password, None,
            )
        except GLib.Error:
            stored = False
        if not stored:
            raise VPNError("The keyring could not save your password. Unlock it and try again.")

    def forget(self, ident: str):
        try:
            Secret.password_clear_sync(SCHEMA, {"profile": ident}, None)
        except GLib.Error:
            raise VPNError("The keyring could not remove the saved password. Unlock it and try again.") from None


class NetworkBackend:
    """All operations run on worker threads; no global networking overrides."""
    def __init__(self, preferences: Preferences | None = None):
        self.keyring = Keyring()
        preferences = preferences if preferences is not None else Preferences()
        value = preferences.data.get("legacy_unit", LEGACY_UNIT)
        self.legacy_unit = value if isinstance(value, str) else LEGACY_UNIT
        self._legacy_cache = None
        self._legacy_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._process = None
        self._process_uuid = None
        self._aborted_uuid = None

    def terminate_pending(self, ident: str) -> None:
        """Stop only this app's nmcli child; never run a blocking call in GTK."""
        with self._process_lock:
            self._aborted_uuid = ident
            process = self._process if self._process_uuid == ident else None
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    def cancel_pending(self, ident: str) -> None:
        self.terminate_pending(ident)
        # Briefly repeat exact-UUID cleanup to catch an in-flight activation.
        # VERIFY ON REAL SYSTEM: a stalled system bus can outlast window closure.
        deadline = time.monotonic() + 2
        while True:
            self.disconnect(ident)
            if time.monotonic() >= deadline:
                return
            time.sleep(0.15)

    def legacy_service_active(self, *, use_cache=False) -> bool:
        """Fresh by default; only background status polling opts into the cache."""
        with self._legacy_lock:
            now = time.monotonic()
            cached = self._legacy_cache
            if use_cache and cached and cached[0] == self.legacy_unit and now - cached[1] < 10:
                return cached[2]
            active = False
            try:
                if self.legacy_unit and os.access("/usr/bin/systemctl", os.X_OK):
                    result = subprocess.run(
                        ["/usr/bin/systemctl", "is-active", self.legacy_unit],
                        capture_output=True, text=True, timeout=5,
                    )
                    active = result.returncode == 0 and result.stdout.strip() in {"active", "activating", "deactivating"}
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
            self._legacy_cache = (self.legacy_unit, time.monotonic(), active)
            return active

    def _call(self, path, interface, method, parameters=None, reply=None):
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        return bus.call_sync(
            SERVICE, path, interface, method, parameters,
            GLib.VariantType.new(reply) if reply else None,
            Gio.DBusCallFlags.NONE, 15000, None,
        )

    def _properties(self, path, interface):
        return self._call(
            path, "org.freedesktop.DBus.Properties", "GetAll",
            GLib.Variant("(s)", (interface,)), "(a{sv})",
        ).unpack()[0]

    def snapshot(self, *, background=False) -> dict:
        """Read NM state and probe the legacy service; cache only background reads."""
        manager = self._properties(ROOT, SERVICE)
        active = []
        for path in manager.get("ActiveConnections", []):
            try:
                item = self._properties(path, SERVICE + ".Connection.Active")
            except GLib.Error:
                continue  # A connection may disappear while this snapshot is read.
            if item.get("Vpn") or item.get("Type") == "wireguard":
                active.append({"uuid": item.get("Uuid"), "name": item.get("Id"), "state": item.get("State"), "path": path})
        legacy = self.legacy_service_active(use_cache=background)
        return {"active": active, "legacy": legacy, "networking": manager.get("NetworkingEnabled", True)}

    def prepare_profile(self, path: Path, username: str) -> str:
        if not path.is_file():
            raise VPNError("The server configuration is missing. Import an .ovpn file again.")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise VPNError("This configuration is too large. Choose a Proton .ovpn file.")
        plugin_file = next((candidate for candidate in PLUGIN_FILES if candidate.is_file()), None)
        if plugin_file is None:
            raise VPNError("Install network-manager-openvpn-gnome, then try again.")
        try:
            plugin = NM.VpnPluginInfo.new_from_file(str(plugin_file))
            editor = plugin.load_editor_plugin()
        except GLib.Error:
            raise VPNError("Could not load OpenVPN support. Install network-manager-openvpn-gnome, then try again.") from None
        try:
            connection = editor.import_(str(path))
        except GLib.Error:
            raise VPNError("Could not import that OpenVPN configuration. Download a fresh Proton .ovpn file.") from None
        if connection is None:
            raise VPNError("That file is not a valid OpenVPN configuration.")
        vpn = connection.get_setting_vpn()
        if vpn is None:
            raise VPNError("That file is not a valid OpenVPN configuration.")
        if vpn.get_data_item("connection-type") != "password":
            raise VPNError("Choose a username/password OpenVPN configuration from Proton VPN.")
        if vpn.get_data_item("remote-cert-tls") != "server" or not vpn.get_data_item("ca"):
            raise VPNError("This configuration is missing server certificate verification. Download a fresh Proton configuration.")
        ident = profile_uuid(path)
        setting = connection.get_setting_connection()
        setting.set_property("id", APP_PREFIX + path.stem)
        setting.set_property("uuid", ident)
        setting.set_property("type", "vpn")
        setting.set_property("autoconnect", False)
        setting.set_property("permissions", [])
        setting.add_permission("user", getpass.getuser(), None)
        vpn.add_data_item("username", username)
        vpn.set_secret_flags("password", NM.SettingSecretFlags.NOT_SAVED)
        connection.clear_secrets()
        ip4 = connection.get_setting_ip4_config()
        if ip4 is None:
            ip4 = NM.SettingIP4Config.new()
            ip4.set_property("method", "auto")
            connection.add_setting(ip4)
        ip4.set_property("dns-priority", -50)
        ip4.set_property("never-default", False)
        connection.verify()
        settings = connection.to_dbus(NM.ConnectionSerializationFlags.NO_SECRETS)
        existing = self._connection_path(ident)
        # Documented since NM 1.20/1.12; VERIFY ON REAL SYSTEM on NM 1.46.
        # IN_MEMORY updates leave any existing on-disk backing file unchanged.
        if existing:
            self._require_owned_profile(existing, ident)
            flag = int(NM.SettingsUpdate2Flags.IN_MEMORY)
            params = GLib.Variant.new_tuple(settings, GLib.Variant("u", flag), GLib.Variant("a{sv}", {}))
            self._call(existing, SERVICE + ".Settings.Connection", "Update2", params, "(a{sv})")
        else:
            flag = int(NM.SettingsAddConnection2Flags.IN_MEMORY)
            params = GLib.Variant.new_tuple(settings, GLib.Variant("u", flag), GLib.Variant("a{sv}", {}))
            self._call(ROOT + "/Settings", SERVICE + ".Settings", "AddConnection2", params, "(oa{sv})")
        return ident

    def _connection_path(self, ident: str) -> str | None:
        try:
            return self._call(ROOT + "/Settings", SERVICE + ".Settings", "GetConnectionByUuid", GLib.Variant("(s)", (ident,)), "(o)").unpack()[0]
        except GLib.Error as error:
            remote = Gio.DBusError.get_remote_error(error) or ""
            if remote.endswith((".InvalidConnection", ".UnknownConnection")):
                return None
            raise

    def _require_owned_profile(self, path: str, ident: str) -> None:
        settings = self._call(path, SERVICE + ".Settings.Connection", "GetSettings", None, "(a{sa{sv}})").unpack()[0]
        connection = settings.get("connection", {})
        if not connection.get("id", "").startswith(APP_PREFIX) or connection.get("uuid") != ident:
            raise VPNError("Refusing to change a VPN profile that Simple VPN did not create.")

    def remove_legacy_profile(self, ident: str) -> bool:
        path = self._connection_path(ident)
        if path is None:
            return True
        self._require_owned_profile(path, ident)
        manager = self._properties(ROOT, SERVICE)
        for active_path in manager.get("ActiveConnections", []):
            active = self._properties(active_path, SERVICE + ".Connection.Active")
            if active.get("Uuid") == ident:
                return False
        self._call(path, SERVICE + ".Settings.Connection", "Delete", None, "()")
        return True

    def connect(self, path: Path, username: str, password: str, remember: bool, cancel: threading.Event) -> str:
        ident = profile_uuid(path)
        try:
            if cancel.is_set():
                raise Cancelled("Connection cancelled.")
            with self._process_lock:
                self._aborted_uuid = None
            if not password:
                try:
                    password = self.keyring.lookup(ident)
                except (VPNError, GLib.Error):
                    raise VPNError("Saved password unavailable (keyring locked or missing). Enter it manually.") from None
            validate_credentials(username, password)
            state = self.snapshot(background=False)
            if state["legacy"]:
                raise VPNError("Your older Proton VPN service is running. Stop it before connecting here.")
            if state["active"]:
                raise VPNError("Another VPN is already active. Disconnect it before starting this connection.")
            if not state["networking"]:
                raise VPNError("Networking is turned off. Enable Wi-Fi or Ethernet first.")
            if cancel.is_set():
                raise Cancelled("Connection cancelled.")
            ident = self.prepare_profile(path, username)
            if cancel.is_set():
                raise Cancelled("Connection cancelled.")
            if not remember:
                try:
                    self.keyring.forget(ident)
                except Exception as error:
                    debug_exception(error, username, password)
            self._activate(ident, password, cancel)
            if cancel.is_set():
                self.disconnect(ident)
                raise Cancelled("Connection cancelled.")
            if remember:
                try:
                    self.keyring.store(ident, password)
                except Exception as error:
                    debug_exception(error, username, password)
                    return "Connected. Password was not saved; unlock your keyring and try saving again."
            return "Connected to your VPN server."
        except VPNError as error:
            debug_exception(error, username, password)
            detail = safe_detail(str(error), username, password)
            if detail != str(error):
                raise type(error)(detail) from None
            raise
        except Exception as error:
            debug_exception(error, username, password)
            raise VPNError(f"Unexpected error ({type(error).__name__}). Run with SIMPLE_VPN_DEBUG=1 for details.") from None
        finally:
            password = ""

    def _activate(self, ident: str, password: str, cancel: threading.Event):
        fd = os.memfd_create("simple-vpn-auth", os.MFD_CLOEXEC)
        process = None
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, ("vpn.secrets.password:" + password + "\n").encode())
            os.lseek(fd, 0, os.SEEK_SET)
            process = subprocess.Popen(
                ["/usr/bin/nmcli", "--wait", "90", "connection", "up", "uuid", ident,
                 "passwd-file", f"/proc/self/fd/{fd}"],
                pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "LC_ALL": "C"},
            )
            with self._process_lock:
                self._process, self._process_uuid = process, ident
                aborted = self._aborted_uuid == ident
            if aborted:
                self.terminate_pending(ident)
            deadline = time.monotonic() + 100
            while True:
                if self._aborted_uuid == ident:
                    self._stop_process(process)
                    self.disconnect(ident)
                    raise Cancelled("Connection cancelled.")
                if cancel.is_set():
                    # Keep nmcli alive until its activation request is visible to
                    # NetworkManager. Killing it earlier could leave an in-flight
                    # D-Bus activation that completes after our disconnect check.
                    if any(item["uuid"] == ident for item in self.snapshot()["active"]):
                        self.disconnect(ident)
                        self._stop_process(process)
                        raise Cancelled("Connection cancelled.")
                if time.monotonic() > deadline:
                    self._stop_process(process)
                    self.disconnect(ident)
                    raise VPNError("The server did not respond in time. Check your network or try another configuration.")
                try:
                    output, _ = process.communicate(timeout=0.25)
                    if cancel.is_set():
                        self.disconnect(ident)
                        raise Cancelled("Connection cancelled.")
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise activation_error(process.returncode, output, password)
        finally:
            os.close(fd)
            if process is not None and process.poll() is None:
                self._stop_process(process)
            with self._process_lock:
                if self._process is process:
                    self._process = self._process_uuid = None

    @staticmethod
    def _stop_process(process):
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.communicate()

    def disconnect(self, ident: str):
        # Deactivate only our exact UUID, never all tunnels or the base connection.
        active = next((item for item in self.snapshot()["active"] if item["uuid"] == ident), None)
        if active is None:
            return
        if not active.get("name", "").startswith(APP_PREFIX):
            raise VPNError("Refusing to disconnect a VPN that Simple VPN did not create.")
        result = subprocess.run(
            ["/usr/bin/nmcli", "--wait", "20", "connection", "down", "uuid", ident],
            capture_output=True, text=True, timeout=25, env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode:
            raise VPNError("Could not disconnect the VPN. " + safe_detail(result.stderr))
