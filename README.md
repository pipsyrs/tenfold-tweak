# Tenfold Tweak

Tenfold Tweak is a small tool for customizing Kali Linux. For now it changes the background of three targets:

| Target  | What changes | Needs root |
|---------|--------------|------------|
| desktop | the desktop wallpaper of the current user | no |
| lock    | the lock screen background | no |
| login   | the login (display manager) screen background | yes (pkexec) |

Each target can be restored. The tool keeps a backup of the original settings and files.

## Support

| Component | Status | Notes |
|-----------|--------|-------|
| Kali Linux, GNOME (tested on GNOME Shell 50, Wayland) | tested | desktop: `org.gnome.desktop.background` |
| GNOME lock screen, stock | tested | GNOME shows the desktop wallpaper, blurred. The lock target sets the desktop wallpaper (after confirmation). |
| GNOME lock screen with the [Lock screen background](https://github.com/sunwxg/gnome-shell-extension-unlockDialogBackground) extension (`unlockDialogBackground@sun.wxg@gmail.com`) | tested | When the extension is active, the lock target writes the extension's own image and leaves the desktop alone. |
| GDM3 login on Kali's patched gnome-shell | applied on Kali; check it visually at logout | The login image is replaced with `dpkg-divert`. |
| GDM3 login on other gnome-shell builds | best-effort | Patches `gnome-shell-theme.gresource`. A gnome-shell update reverts it, so re-apply afterwards. |
| XFCE desktop (xfconf / xfdesktop) | best-effort, unit-tested only | |
| XFCE lock (xfce4-screensaver) | best-effort | Shows the desktop wallpaper, so lock sets the desktop wallpaper. |
| LightDM with lightdm-gtk-greeter | best-effort, unit-tested only | Other LightDM greeters are not supported. |
| KDE, other DEs, SDDM, other DMs | not supported | `tenfold-tweak detect` shows the reason. |

Other Debian-based distros with the same DE and DM may work, but they are untested.

## Requirements

- Python 3.10+, PyGObject: `python3-gi gir1.2-gdkpixbuf-2.0` (preinstalled on Kali desktops)
- GUI: GTK 4 bindings, `gir1.2-gtk-4.0` (with `python3-gi`). Optional: `gir1.2-adw-1` (libadwaita 1.5 or later). zenity is no longer required.
- Login target: `pkexec` (polkit), and running `sudo ./install.sh` once
- XFCE: `xfconf-query` (part of xfconf)

No pip packages are needed.

## Install

Desktop and lock work straight from the project directory. Installing is required only for the login target, because the root helper must be a root-owned copy:

```sh
sudo ./install.sh
```

This installs to `/usr/local/lib/tenfold-tweak` and creates the `/usr/local/bin/tenfold-tweak` command and an app-menu entry "Tenfold Tweak". If an older install named `kali-bg-changer` exists, the installer undoes its login change, removes its files and re-applies the same login image with the new helper. If that image cannot be kept, it stops without changing anything. Run `sudo ./install.sh --discard-login` to reset the login background and continue.

## Usage

GUI, no install needed (double-click it, or run it from a terminal):

```sh
./tenfold-tweak-gui
```

CLI:

```sh
./tenfold-tweak detect                         # environment and available targets
./tenfold-tweak status                         # current image and restore state per target
./tenfold-tweak apply ~/Pictures/bg.jpg --desktop --lock
./tenfold-tweak apply ~/Pictures/bg.png --login
./tenfold-tweak apply ~/Pictures/bg.png --all --yes
./tenfold-tweak restore --all
```

Images must be PNG or JPEG. Login images larger than 4096x4096 are downscaled. A login change shows after you log out or reboot.

If the login screen ever breaks on GNOME: press Ctrl+Alt+F3, log in, then run `sudo apt install --reinstall gnome-shell-common`.

## Uninstall

```sh
tenfold-tweak restore --login     # required first if a login change is active
sudo ./install.sh --uninstall
```

To drop your desktop and lock-screen backups as well, run `tenfold-tweak restore --desktop --lock` first, then remove `~/.local/state/tenfold-tweak` and `~/.local/share/backgrounds/tenfold-tweak`.
