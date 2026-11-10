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
| KDE, other DEs, SDDM, other DMs | not supported | `tenfold-tweaks detect` shows the reason. |

### Supported operating systems

| OS | Status | Reason |
|----|--------|--------|
| Kali Linux (rolling) | tested | |
| Other Debian-based distros with apt, dpkg and systemd, e.g. Debian 12+, Ubuntu 22.04+ and derivatives | should work, untested | The `.deb` (`Architecture: all`) needs Python 3.10+, `gir1.2-gtk-4.0` and dpkg 1.19.2+, which these releases ship. Only the DE/DM combinations above apply. If libadwaita is older than 1.5, the GUI runs without it. |
| Non-Debian Linux (Fedora, Arch, openSUSE, ...) | not supported | Packaged only as `.deb`; install and the login target rely on apt/dpkg and `dpkg-divert`. |
| Windows, macOS | not supported | Linux desktop (GNOME/XFCE, GDM3/LightDM) only. |

The `clean` categories `--apt-cache` and `--autoremove` need apt, and `--journal` needs systemd-journald (`journalctl`).

## Requirements

- Python 3.10+, PyGObject: `python3-gi gir1.2-gdkpixbuf-2.0` (preinstalled on Kali desktops)
- GUI: GTK 4 bindings, `gir1.2-gtk-4.0` (with `python3-gi`). Optional: `gir1.2-adw-1` (libadwaita 1.5 or later). zenity is no longer required.
- Login target: `pkexec` (polkit) and the installed package
- XFCE: `xfconf-query` (part of xfconf)

No pip packages are needed.

## Install

Desktop and lock work straight from the project directory. The login target needs the package, because the root helper must be root-owned.

Build the .deb (needs only `dpkg-deb`, preinstalled on Kali; no debhelper or fakeroot):

1. Set the version in `packaging/VERSION`. To upgrade an installed package, bump it first (e.g. `2.0.1`).
2. Build: `sh packaging/build-deb.sh` writes `dist/tenfold-tweaks_<version>_all.deb`. Every module in `tenfold_tweak/` must be listed in `MODULES` in `packaging/build-deb.sh`; otherwise the build fails on purpose.
3. Install with the full file name (`dist/` can hold several versions, so avoid `*.deb`):

```sh
sh packaging/build-deb.sh
sudo apt install ./dist/tenfold-tweaks_2.0.0_all.deb
```

To upgrade, build a higher version and `apt install` it over the old one.

This installs the `tenfold-tweaks` command and the app-menu entry "Tenfold Tweaks". An older `sudo ./install.sh` install (`tenfold-tweak`) is migrated automatically: the active login image stays, its files are removed, and if anything fails the install is cancelled with nothing changed. A login image the old install kept as `migrated-login` is saved in `/var/backups/tenfold-tweaks/`.

## Usage

GUI, no install needed (double-click it, or run it from a terminal):

```sh
./tenfold-tweaks-gui
```

CLI:

```sh
./tenfold-tweaks detect                         # environment and available targets
./tenfold-tweaks status                         # current image and restore state per target
./tenfold-tweaks apply ~/Pictures/bg.jpg --desktop --lock
./tenfold-tweaks apply ~/Pictures/bg.png --login
./tenfold-tweaks apply ~/Pictures/bg.png --all --yes
./tenfold-tweaks restore --all
```

Images must be PNG or JPEG. Login images larger than 4096x4096 are downscaled. A login change shows after you log out or reboot.

If the login screen ever breaks on GNOME: press Ctrl+Alt+F3, log in, then run `sudo apt install --reinstall gnome-shell-common`.

## Clean up

Frees disk space (not RAM). In the GUI, open the "Clean up" page, tick the categories and confirm. CLI:

```sh
tenfold-tweaks clean                            # default categories: preview, then asks before deleting
tenfold-tweaks clean --cache --trash --defaults --yes
```

Categories: `--apt-cache`, `--journal` (logs older than 7 days), `--thumbnails`, `--tool-images` (unused Tenfold Tweaks images) are the defaults; `--cache` (~/.cache), `--trash` and `--autoremove` (orphan packages) are opt-in. Without category flags the defaults are used; `--defaults` adds them to the selected ones. `clean` always prints a preview (size per category and total) first, then asks for confirmation (`[y/N]`) when run in a terminal; `--yes` skips the question. Without `--yes` and without a terminal (scripts, pipes, cron) it only previews and prints `nothing deleted; re-run with --yes`. APT cache, journal, tool images and autoremove need admin rights (asked once).

## Uninstall

```sh
sudo apt remove tenfold-tweaks    # restores the stock login screen; the active login image is saved first
sudo apt purge tenfold-tweaks     # also deletes /var/lib/tenfold-tweaks
```

Saved login images stay in `/var/backups/tenfold-tweaks/` (readable without admin rights; purge keeps them) and their paths are printed. If the login screen cannot be restored safely, the remove stops and the package stays installed.

Full removal:

1. `tenfold-tweaks restore --desktop --lock` (the package never touches your home directory, so undo desktop and lock first)
2. `sudo apt purge tenfold-tweaks`
3. `rm -rf ~/.local/state/tenfold-tweaks ~/.local/share/backgrounds/tenfold-tweaks`
4. Optional: `sudo rm -rf /var/backups/tenfold-tweaks` (saved login images; purge keeps them)
