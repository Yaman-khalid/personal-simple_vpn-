#!/usr/bin/python3
"""Build an allowlisted .deb without root privileges or personal data."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

APP_ID = "io.github.simplevpn.Desktop"
RUNTIME_FILES = ("app.py", "backend.py", "paths.py", "migration.py")
PACKAGE_VERSION = "0.1.0-1"
SOURCE_EPOCH = 1792454400  # 2026-10-20T00:00:00Z
DEPENDENCIES = ("python3", "python3-gi", "gir1.2-gtk-4.0", "gir1.2-adw-1", "gir1.2-nm-1.0", "gir1.2-secret-1", "network-manager", "network-manager-openvpn-gnome")


def put(root: Path, relative: str, content: bytes, mode=0o644) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    target.chmod(mode)


def build(source: Path, output: Path, *, version=PACKAGE_VERSION,
          maintainer="Yaman-khalid", epoch=SOURCE_EPOCH) -> Path:
    source, output = source.resolve(), output.resolve()
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", version):
        raise ValueError("Invalid Debian version")
    if any(character in maintainer for character in "\r\n\x00"):
        raise ValueError("Invalid maintainer")
    # License is an explicit input, never inferred from local account data.
    license_text = (source / "LICENSE").read_bytes()
    for name in (*RUNTIME_FILES, "README.md", "icon.svg"):
        if (source / name).is_symlink():
            raise ValueError("Runtime source must not be a symlink: " + name)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"simple-vpn_{version}_all.deb"
    with tempfile.TemporaryDirectory(prefix="simple-vpn-deb-") as temporary:
        root = Path(temporary) / "package"
        root.mkdir()
        for name in RUNTIME_FILES:
            put(root, "usr/share/simple-vpn/" + name, (source / name).read_bytes())
        put(root, "usr/bin/simple-vpn", (source / "packaging/simple-vpn").read_bytes(), 0o755)
        put(root, "usr/share/applications/" + APP_ID + ".desktop", (source / "packaging" / (APP_ID + ".desktop")).read_bytes())
        put(root, "usr/share/icons/hicolor/scalable/apps/" + APP_ID + ".svg", (source / "icon.svg").read_bytes())
        put(root, "usr/share/doc/simple-vpn/README.md", (source / "README.md").read_bytes())
        put(root, "usr/share/doc/simple-vpn/copyright", license_text)
        date = datetime.fromtimestamp(epoch, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        changelog = (f"simple-vpn ({version}) noble; urgency=medium\n\n"
                     "  * Initial Debian package for Ubuntu 24.04.\n"
                     "  * No bundled credentials, VPN profiles or per-user settings.\n\n"
                     f" -- {maintainer}  {date}\n").encode()
        put(root, "usr/share/doc/simple-vpn/changelog.gz", gzip.compress(changelog, mtime=0))
        payload = sorted(p for p in root.rglob("*") if p.is_file())
        installed_size = (sum(p.stat().st_size for p in payload) + 1023) // 1024
        control = (source / "packaging/control.in").read_text()
        for old, new in {"@VERSION@": version, "@MAINTAINER@": maintainer, "@INSTALLED_SIZE@": str(installed_size)}.items():
            control = control.replace(old, new)
        put(root, "DEBIAN/control", control.encode())
        checksums = "".join(hashlib.md5(p.read_bytes(), usedforsecurity=False).hexdigest() + "  " + str(p.relative_to(root)) + "\n" for p in payload)
        put(root, "DEBIAN/md5sums", checksums.encode())
        for path in root.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
            os.utime(path, (epoch, epoch))
        root.chmod(0o755)
        os.utime(root, (epoch, epoch))
        executable = shutil.which("desktop-file-validate")
        if executable:
            subprocess.run([executable, str(root / "usr/share/applications" / (APP_ID + ".desktop"))], check=True)
        environment = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch)}
        subprocess.run(["/usr/bin/dpkg-deb", "--root-owner-group", "--threads-max=1", "-Zxz", "--build", str(root), str(target)], check=True, env=environment)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "dist")
    parser.add_argument("--version", default=PACKAGE_VERSION)
    parser.add_argument("--maintainer", default="Yaman-khalid")
    args = parser.parse_args()
    target = build(Path(__file__).resolve().parent, args.output, version=args.version, maintainer=args.maintainer)
    print("Built:", target)
    print("SHA256:", hashlib.sha256(target.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
