"""Validate desktop metadata only in a temporary directory; launch nothing."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile

from backend import APP_ID
from install import desktop_text


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="simple-vpn-desktop-qa-") as root:
        directory = Path(root)
        desktop = directory / (APP_ID + ".desktop")
        desktop.write_text(desktop_text(directory / "data with spaces % and $literal/simple-vpn/app"))
        assert "MimeType=" not in desktop.read_text()
        for name, arguments in [("desktop-file-validate", [str(desktop)])]:
            executable = shutil.which(name)
            if executable is None:
                print(name + ": unavailable, skipped")
                continue
            result = subprocess.run([executable, *arguments], capture_output=True, text=True, timeout=30)
            print(name + " exit: " + str(result.returncode))
            if result.stdout or result.stderr:
                print((result.stdout + result.stderr).strip())
            if result.returncode:
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
