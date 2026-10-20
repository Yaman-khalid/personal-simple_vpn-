"""Restart-safe migration; callers supply service fakes in tests."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Callable

from backend import NetworkBackend, Preferences, VPNError, import_profile_file, legacy_profile_uuid, profile_uuid, read_profile_file
from paths import data_dir, downloads_dir

LEGACY_DOWNLOADS = ("nl-443.ovpn", "nl-free-2.protonvpn.tcp.ovpn")


def migrate_legacy(
    preferences: Preferences,
    backend: NetworkBackend,
    report: Callable[[str], None],
    *,
    home: Path | None = None,
    download_directory: Path | None = None,
    target_data: Path | None = None,
) -> None:
    if preferences.data.get("migrated_v2"):
        return
    home = Path(home) if home is not None else Path.home()
    downloads = Path(download_directory) if download_directory is not None else downloads_dir()
    target_data = Path(target_data) if target_data is not None else data_dir()
    complete = True
    sources = []
    try:
        sources.extend((p, "servers") for p in sorted((home / "Apps/simple-vpn/servers").glob("*.ovpn")))
        sources.extend((downloads / name, "servers") for name in LEGACY_DOWNLOADS if (downloads / name).exists())
        sources.extend((p, "profiles") for p in sorted((preferences.directory / "profiles").glob("*.ovpn")))
    except Exception as error:
        report(f"Legacy locations could not be read ({type(error).__name__}); migration will retry next startup.")
        complete = False
    for source, category in sources:
        before = deepcopy(preferences.data)
        target = target_data / category / source.name
        old_id, new_id = legacy_profile_uuid(source), profile_uuid(target)
        secret = ""
        try:
            content = read_profile_file(source)
            if target.is_symlink():
                raise VPNError("Destination is a symlink; original kept.")
            if target.exists():
                if read_profile_file(target) != content:
                    raise VPNError("Existing destination differs; both copies kept.")
            else:
                import_profile_file(source, target.parent)
                if read_profile_file(target) != content:
                    raise VPNError("Copied configuration could not be verified.")
            old = preferences.data.get("profiles", {}).get(old_id, {})
            new = preferences.profile(new_id)
            if old.get("username") and not new.get("username"):
                new["username"] = old["username"]
            if old.get("remembered"):
                existing = backend.keyring.lookup(new_id)
                if existing:
                    new["remembered"] = True
                    report("Existing saved password preserved; older keyring entry kept.")
                else:
                    secret = backend.keyring.lookup(old_id)
                    if not secret:
                        raise VPNError("The older saved password is unavailable.")
                    backend.keyring.store(new_id, secret)
                    if backend.keyring.lookup(new_id) != secret:
                        raise VPNError("The copied password could not be verified.")
                    new["remembered"] = True
            else:
                new.setdefault("remembered", False)
            if preferences.data.get("selected") == old_id:
                preferences.data["selected"] = new_id
            # Copy-only migration: keep original files, preferences and keyring entries.
            preferences.save()
        except Exception as error:
            preferences.data = before
            complete = False
            report(f"Migration deferred for {source.name} ({type(error).__name__}); original settings and files kept. Retry next startup.")
            continue
        finally:
            secret = ""
        report(f"Migrated {source.name}; all original files and older keyring entries kept; NetworkManager profiles kept.")
    preferences.data["migrated_v2"] = complete
    try:
        preferences.save()
    except Exception as error:
        preferences.data["migrated_v2"] = False
        report(f"Migration completion could not be saved ({type(error).__name__}); originals and old preference entries kept.")
