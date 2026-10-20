"""Inspect and install packages only in disposable roots; never the host system."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from build_deb import APP_ID, DEPENDENCIES, RUNTIME_FILES, build


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="simple-vpn-package-tests-")
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "source"
        cls.source.mkdir()
        real = Path(__file__).resolve().parent
        for name in (*RUNTIME_FILES, "README.md", "icon.svg"):
            shutil.copyfile(real / name, cls.source / name)
        shutil.copytree(real / "packaging", cls.source / "packaging")
        # Not a license grant: this is private disposable test-package content.
        (cls.source / "LICENSE").write_text("TEST FIXTURE ONLY; not a distribution license.\n")
        (cls.source / "preferences.json").write_text('fake private data MUST NOT SHIP')
        (cls.source / "personal.ovpn").write_text('fake credentials MUST NOT SHIP')
        cls.package = build(cls.source, cls.root / "out", version="0.1.0-1")
        raw = subprocess.run(["/usr/bin/dpkg-deb", "--fsys-tarfile", str(cls.package)], check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            cls.members = archive.getmembers()
            cls.files = {member.name.removeprefix("./"): archive.extractfile(member).read() for member in cls.members if member.isfile()}

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_payload_is_exact_allowlist_with_no_personal_files_or_bytecode(self):
        expected={*("usr/share/simple-vpn/" + name for name in RUNTIME_FILES),
                  "usr/bin/simple-vpn", "usr/share/applications/" + APP_ID + ".desktop",
                  "usr/share/icons/hicolor/scalable/apps/" + APP_ID + ".svg",
                  "usr/share/doc/simple-vpn/README.md", "usr/share/doc/simple-vpn/copyright",
                  "usr/share/doc/simple-vpn/changelog.gz"}
        self.assertEqual(set(self.files), expected)
        self.assertNotIn(b"MUST NOT SHIP", b"".join(self.files.values()))
        for name in self.files:
            self.assertFalse(name.endswith((".ovpn", ".pyc")))
            self.assertNotIn("preferences.json", name)
            self.assertNotIn("__pycache__", name)

    def test_root_ownership_and_normal_system_permissions(self):
        for item in self.members:
            self.assertEqual((item.uid,item.gid),(0,0))
            self.assertEqual(item.mode,0o755 if item.isdir() or item.name.endswith("usr/bin/simple-vpn") else 0o644)
            self.assertFalse(item.issym() or item.islnk())

    def test_dependencies_metadata_and_no_privileged_maintainer_scripts(self):
        control=subprocess.run(["/usr/bin/dpkg-deb","--field",str(self.package)],check=True,capture_output=True,text=True).stdout
        self.assertIn("Architecture: all",control)
        for name in DEPENDENCIES:
            self.assertIn(name,control)
        raw=subprocess.run(["/usr/bin/dpkg-deb","--ctrl-tarfile",str(self.package)],check=True,capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            self.assertEqual({m.name.removeprefix('./') for m in archive.getmembers() if m.isfile()},{'control','md5sums'})

    def test_reproducible_build(self):
        second=build(self.source,self.root/'out-repeat',version='0.1.0-1')
        self.assertEqual(hashlib.sha256(self.package.read_bytes()).digest(),hashlib.sha256(second.read_bytes()).digest())

    def test_invalid_metadata_is_rejected(self):
        for version in ('bad version','../evil','1.0\nX: injected'):
            with self.assertRaises(ValueError):build(self.source,self.root/'invalid',version=version)
        with self.assertRaises(ValueError):build(self.source,self.root/'invalid',maintainer='Name\nX: injected')

    def test_install_upgrade_remove_and_purge_in_disposable_dpkg_root(self):
        if not shutil.which('fakeroot'):
            self.skipTest('fakeroot is unavailable; host install is never a fallback')
        mappings=[line.split() for line in Path('/proc/self/uid_map').read_text().splitlines()]
        if not any(int(start)==0 and int(length)>0 for start,outside,length in mappings):
            self.skipTest('UID 0 is unmapped in this restricted namespace; lifecycle QA needs an ordinary user session')
        root=self.root/'dpkg-root' 
        (root/'var/lib/dpkg/updates').mkdir(parents=True)
        (root/'var/lib/dpkg/status').write_text('')
        (root/'var/log').mkdir()
        user_data=root/'home/tester/.config/simple-vpn/preferences.json'
        user_data.parent.mkdir(parents=True)
        user_data.write_text('preserve fake per-user data')
        base=['/usr/bin/fakeroot','/usr/bin/dpkg','--root='+str(root),'--force-not-root','--force-depends','--log='+str(root/'var/log/dpkg.log')]
        def run(*arguments):
            result=subprocess.run([*base,*arguments],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        run('--install',str(self.package))
        self.assertTrue((root/'usr/bin/simple-vpn').exists())
        self.assertTrue((root/'usr/share/simple-vpn/app.py').exists())
        next_package=build(self.source,self.root/'out-upgrade',version='0.1.0-2')
        run('--install',str(next_package))
        self.assertIn('Version: 0.1.0-2',(root/'var/lib/dpkg/status').read_text())
        run('--remove','simple-vpn')
        self.assertFalse((root/'usr/bin/simple-vpn').exists())
        run('--purge','simple-vpn')
        self.assertEqual(user_data.read_text(),'preserve fake per-user data')
