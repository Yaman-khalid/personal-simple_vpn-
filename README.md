# Simple VPN

A small native Linux app for connecting through your own **Proton OpenVPN**
configuration. Import an `.ovpn` file, enter your OpenVPN credentials, and connect.
This is an independent personal project, unaffiliated with Proton AG.

Version 0.1.0. Intended for **Ubuntu 24.04 with GNOME and NetworkManager**.
Other distributions and newer Ubuntu versions have not been validated. The Python
code is architecture independent; the required system packages must also be
available for your computer's architecture.

## Download and install

Download `simple-vpn_0.1.0-1_all.deb` from [GitHub Releases](https://github.com/Yaman-khalid/personal-simple_vpn-/releases).
Install it with a compatible graphical Debian-package installer, or open a
terminal in the download folder and run:

```bash
sudo apt install ./simple-vpn_0.1.0-1_all.deb
```

APT installs the declared dependencies from your configured Ubuntu repositories;
an internet connection may be needed. Ubuntu 24.04's default App Center may not
open downloaded `.deb` files; the command above is the supported fallback.
Launch **Simple VPN** from Applications, or run `simple-vpn`.

The `.deb` installer uses the package manager's administrator permissions. The app
itself runs as your regular user. It installs no background service or new polkit
rules and never connects automatically during installation.

## First connection

1. Download a **Linux OpenVPN configuration** from your Proton account. Use a
   TCP 443 configuration if your network requires it.
2. Click **Import .ovpn** and choose the file. Each person supplies their own file;
   the installer contains no server configurations, accounts or passwords.
3. Enter the **OpenVPN username and password**, which differ from the normal
   Proton account login. Optionally remember the password in your desktop keyring.
4. Click **Connect**. Cancel stops an attempt. Disconnect ends an active tunnel.

Closing an established connection's window leaves the tunnel running. Reopen
Simple VPN to disconnect. Closing during an attempt requests cancellation and
closes after at most ten seconds. A stalled system service can delay cleanup.
Avoid running another VPN or a legacy OpenVPN service at the same time.
The app's saved connections also appear in Ubuntu's VPN settings.

The credentials button opens the existing URL stored as `CREDENTIALS_URL` in
`app.py`; Proton may change its account pages. Its manual setup guide is linked
below. Actual server availability and network compatibility vary.

## Updates and removal

Download a newer `.deb` and install it with the same `apt install ./filename.deb`
command. Your preferences and imported configurations stay in your home folder.
GitHub downloads do **not** configure automatic APT updates or an APT repository.

```bash
sudo apt remove simple-vpn
```

Removal, and even `apt purge simple-vpn`, leave each user's settings, configurations
and keyring entries intact. Package scripts do not inspect or delete home folders,
stop VPN connections, or change existing network profiles. Close the app before
upgrading if you want the running window to use the new code immediately.

If upgrading from the earlier per-user installer, an old launcher at
`~/.local/share/applications/io.github.simplevpn.Desktop.desktop` can override the
system launcher. Retire that old launcher after backing it up and checking its
Exec line. `/usr/bin/simple-vpn` explicitly starts the packaged version. This
package does not remove another installation from your home directory.

## Files and passwords

Package-managed files live in `/usr/share/simple-vpn/`, `/usr/bin/simple-vpn`,
`/usr/share/applications/` and the system hicolor icon theme. Each user has separate
XDG data and preferences. Default locations:

- `~/.config/simple-vpn/preferences.json`: username, selection and flags.
- `~/.local/share/simple-vpn/profiles/`: imported VPN configurations.
- `~/.local/share/simple-vpn/servers/`: preserved earlier server configurations.

New private directories use 0700 and files 0600. Existing parent permissions are
preserved. The package contains no user data. The normal app's optional startup
migration copies older profiles and saved credentials to stable identities;
**original files, old preference entries, keyring entries and NM profiles remain**.
Migration failures are nonfatal and appear in Recent activity. Old preferences
and keyring entries may be shared with an earlier Simple VPN version.

Passwords are not put in argv, environment variables, preferences, disk password
files or app logs. They pass to nmcli through an anonymous inherited `memfd`, with
NetworkManager's password flag set to `NOT_SAVED`. Optional persistent storage is
in the user's libsecret keyring. **The password briefly exists in RAM, and
same-user processes can read the memfd through `/proc` while nmcli runs.** Secure
erasure of Python strings cannot be guaranteed. No password is exported into the
installer or release archive.

New app-created NM profiles are in memory, restricted to the user and have
autoconnect off. In-memory updates do not delete preexisting disk backing files.
The OpenVPN plugin can extract certificates under `~/.cert/nm-openvpn`, outside
this app's package-managed paths. App-created profile names begin `Simple VPN · `.

**No kill switch, dedicated IPv6 policy or independent IP/DNS leak test is
included.** A tunnel dropping can allow normal internet traffic to continue.
The display reports NM's tunnel state, not a privacy audit. Independently check
public IP, DNS and IPv6 routing when using a new setup.

Activity stays in memory. Debug logging is off by default and goes only to stderr:

```bash
SIMPLE_VPN_DEBUG=1 simple-vpn
```

Known credential values are redacted; review other private metadata before sharing
any diagnostics. Background status refreshes every two seconds, with ten-second
caching only for the legacy-service check; Connect always checks it freshly.

## Build and check

No extra Python packages are needed. Build on Ubuntu with `dpkg-deb` available:

```bash
/usr/bin/python3 build_deb.py
/usr/bin/python3 -m unittest discover -v
/usr/bin/python3 qa_desktop.py
/usr/bin/python3 qa_ui.py
```

The build uses an explicit file allowlist, root ownership metadata and normal
system file permissions. It never copies your home data or scans for credentials.
The resulting `.deb` is in `dist/`. `--maintainer 'Name <email>'` can supply an
explicit public maintainer contact when building. Repeated builds of identical
inputs use a fixed timestamp and produce identical package bytes.

System dependencies are Python 3.12+, PyGObject 3.48+, GTK 4.14+, libadwaita 1.5+,
libnm/NetworkManager 1.46+, libsecret, and network-manager-openvpn-gnome. A Secret
Service provider such as gnome-keyring is recommended for remembered passwords.
Optional build validation uses desktop-file-utils. The package lifecycle test
uses fakeroot and a temporary dpkg root; it never installs into the real system.
That fixture bypasses dependency installation; it is not a clean Ubuntu VM test.

UI QA requires a display, uses a private session bus and temporary XDG roots, and
mocks VPN operations. `qa_ui.py --app-root /path/to/extracted/usr/share/simple-vpn`
checks an extracted `.deb` runtime. If available on a headless X11 host, use
`xvfb-run -a /usr/bin/python3 qa_ui.py`. Unit tests block unmocked system/service
calls. No QA command starts a real VPN or touches the real keyring.

The older per-user `install.py` remains in the source for existing installations;
it is not included in the `.deb`. Its explicit user-level purge command removes
shared legacy config and credentials after confirmation. Prefer the package
manager for package-managed files; do not run the legacy installer as root.

## References

- [Proton's manual OpenVPN setup](https://protonvpn.com/support/linux-openvpn)
- [NetworkManager](https://networkmanager.dev/)
- [Debian Python application packaging](https://www.debian.org/doc/packaging-manuals/python-policy/)

## License and support

MIT, copyright 2026 Yaman-khalid. See [LICENSE](LICENSE).
Report issues at [the project issue tracker](https://github.com/Yaman-khalid/personal-simple_vpn-/issues).
