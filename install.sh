#!/bin/sh
# Install tenfold-tweak root-owned under /usr/local (needed for the login target).
# Usage: sudo ./install.sh            install or update (migrates an old kali-bg-changer install)
#        sudo ./install.sh --discard-login   same, but reset an old login change whose image cannot be kept
#        sudo ./install.sh --uninstall
set -eu
LIB=/usr/local/lib/tenfold-tweak
BIN=/usr/local/bin/tenfold-tweak
STATE=/var/lib/tenfold-tweak
# Named after the GUI's application id so GNOME matches the window to the menu entry.
DESKTOP_FILE=/usr/local/share/applications/io.github.tenfold.TenfoldTweak.desktop
OLD_DESKTOP_FILE=/usr/local/share/applications/tenfold-tweak.desktop
DISCARD_LOGIN=0
HELPER="$LIB/tenfold_tweak/root_helper.py"
# Explicit file list: nothing is globbed from the user-writable source tree.
FILES="tenfold-tweak tenfold_tweak/__init__.py tenfold_tweak/cli.py tenfold_tweak/core.py tenfold_tweak/desktop.py
tenfold_tweak/detect.py tenfold_tweak/gui.py tenfold_tweak/image.py tenfold_tweak/root_helper.py tenfold_tweak/system.py"

# Previous name of the tool (kali-bg-changer); removed on install after undoing its login change.
OLD_LIB=/usr/local/lib/kali-bg-changer
OLD_BIN=/usr/local/bin/kali-bg-changer
OLD_STATE=/var/lib/kali-bg-changer
OLD_HELPER="$OLD_LIB/bgchanger/root_helper.py"
OLD_LIGHTDM_DIR=/usr/local/share/backgrounds/kali-bg-changer
KALI_LOGIN_BG=/usr/share/desktop-base/kali-theme/login/background-blurred
GRESOURCE=/usr/share/gnome-shell/gnome-shell-theme.gresource

# Refuse symlinks and non-regular files so root never copies something it was pointed at.
check_regular() {
    if [ -L "$1" ] || [ ! -f "$1" ]; then
        echo "refusing to install $1: not a regular file (symlink or missing)" >&2
        exit 1
    fi
}

# Run a root helper directly as root; no caller uid so it accepts root-owned images.
run_helper() {
    env -u PKEXEC_UID -u SUDO_UID /usr/bin/python3 -I "$@"
}

# Command that copies the kept migration image to the invoking (sudo) user's home.
kept_image_hint() {
    user=${SUDO_USER:-$(id -un)}
    home=$(getent passwd "$user" | cut -d: -f6)
    echo "copy it with: sudo cp $STATE/migrated-login ${home:-~}/login-image && sudo chown $user ${home:-~}/login-image" >&2
    echo "then run: tenfold-tweak apply ${home:-~}/login-image --login" >&2
}

old_divert_active() {
    command -v dpkg-divert >/dev/null 2>&1 &&
        [ "$(dpkg-divert --truename "$KALI_LOGIN_BG")" = "$KALI_LOGIN_BG.kali-bg-changer" ]
}

# Old-install migration, part 1 (before installing): save the active login image.
# Aborts with nothing changed when the image cannot be kept, unless --discard-login.
migrate_prepare() {
    migrating=""
    [ -e "$OLD_LIB" ] || [ -e "$OLD_STATE" ] || [ -L "$OLD_BIN" ] || [ -e "$OLD_BIN" ] || return 0
    migrating=1
    tmp=$(mktemp -d /var/lib/.tenfold-tweak-migrate.XXXXXX)
    reapply=""
    gdm_active=""
    if old_divert_active || [ -e "$OLD_STATE/kali-login.divert" ] || [ -e "$OLD_STATE/gnome-shell-theme.gresource.orig" ]; then
        gdm_active=1
    fi
    lightdm_active=""
    [ -e "$OLD_STATE/lightdm-gtk-greeter.conf.orig" ] && lightdm_active=1
    [ -n "$gdm_active$lightdm_active" ] || return 0

    if [ -L "$OLD_HELPER" ] || [ ! -f "$OLD_HELPER" ] || [ "$(stat -c %u "$OLD_HELPER")" != 0 ]; then
        rm -rf "$tmp"
        echo "an old kali-bg-changer login change is active but its helper is missing;" >&2
        echo "restore it first ('kali-bg-changer restore --login') or 'sudo apt install --reinstall gnome-shell-common'" >&2
        exit 1
    fi
    # need_image: a custom login image is visible now and would be lost by the restore.
    need_image=""
    entry=""
    if [ -n "$lightdm_active" ]; then
        need_image=1
    fi
    if old_divert_active; then
        need_image=1
    elif [ -e "$OLD_STATE/gnome-shell-theme.gresource.orig" ]; then
        need_image=1
        if command -v gresource >/dev/null 2>&1; then
            entry=$(gresource list "$GRESOURCE" 2>/dev/null | grep '^/org/gnome/shell/theme/kali-bg-changer-login\.' | head -n 1 || true)
            [ -n "$entry" ] || need_image=""  # theme already stock
        fi
    fi
    if [ -n "$gdm_active" ] && [ -n "$lightdm_active" ]; then
        :  # only one image could be re-applied; treat as not saved
    elif [ -n "$lightdm_active" ]; then
        for f in "$OLD_LIGHTDM_DIR"/login.*; do
            if [ -f "$f" ] && [ ! -L "$f" ] && cp -- "$f" "$tmp/login"; then reapply=lightdm-apply; break; fi
        done
    elif old_divert_active; then
        if [ -f "$KALI_LOGIN_BG" ] && [ ! -L "$KALI_LOGIN_BG" ] && cp -- "$KALI_LOGIN_BG" "$tmp/login"; then
            reapply=gdm-apply
        fi
    elif [ -n "$entry" ]; then
        if gresource extract "$GRESOURCE" "$entry" > "$tmp/login" 2>/dev/null; then reapply=gdm-apply; fi
    fi
    if [ -n "$reapply" ] && [ ! -s "$tmp/login" ]; then
        reapply=""
    fi
    if [ -n "$need_image" ] && [ -z "$reapply" ] && [ "$DISCARD_LOGIN" != 1 ]; then
        rm -rf "$tmp"
        echo "cannot keep the login background set by the old kali-bg-changer install; nothing was changed." >&2
        echo "to reset it to the default and continue: sudo $0 --discard-login" >&2
        exit 1
    fi
}

# Part 2 (after installing): undo the old login change, remove old files, re-apply the image.
migrate_finish() {
    [ -n "$migrating" ] || return 0
    echo "migrating from kali-bg-changer"
    for action in ${gdm_active:+gdm-restore} ${lightdm_active:+lightdm-restore}; do
        rc=0
        run_helper "$OLD_HELPER" "$action" || rc=$?
        if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
            rm -rf "$tmp"
            echo "could not undo the old kali-bg-changer login change ($action exited $rc); old install left in place" >&2
            exit 1
        fi
    done
    rm -rf "$OLD_LIB" "$OLD_STATE"
    rm -f "$OLD_BIN"
    echo "removed old kali-bg-changer files"
    if [ -n "$reapply" ]; then
        if run_helper "$HELPER" "$reapply" "$tmp/login"; then
            echo "login background re-applied with tenfold-tweak"
        else
            # Keep the only copy of the image.
            install -d -o root -g root -m 0755 "$STATE"
            mv -f -- "$tmp/login" "$STATE/migrated-login"
            echo "warning: could not re-apply the login background; the image is kept at $STATE/migrated-login" >&2
            kept_image_hint
        fi
    elif [ -n "$gdm_active$lightdm_active" ]; then
        echo "note: the login background was reset; run: tenfold-tweak apply IMAGE --login" >&2
    fi
    rm -rf "$tmp"
}

case "${1:-}" in
    ""|--uninstall) ;;
    --discard-login) DISCARD_LOGIN=1 ;;
    *) echo "usage: sudo $0 [--discard-login | --uninstall]" >&2; exit 2 ;;
esac

if [ "${1:-}" != "--uninstall" ]; then
    SRC=$(cd "$(dirname "$0")" && pwd -P)
    if [ -L "$SRC/tenfold_tweak" ] || [ ! -d "$SRC/tenfold_tweak" ]; then
        echo "refusing to install: $SRC/tenfold_tweak is not a directory" >&2
        exit 1
    fi
    for f in $FILES; do check_regular "$SRC/$f"; done
fi

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0 ${1:-}" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    if [ -e "$STATE/migrated-login" ]; then
        echo "refusing to uninstall: $STATE/migrated-login is the only copy of your old login image" >&2
        kept_image_hint
        echo "then remove it: sudo rm $STATE/migrated-login" >&2
        exit 1
    fi
    # The helper is needed to undo a login-screen change; refuse while one is active.
    if [ -e "$STATE/gnome-shell-theme.gresource.orig" ] || [ -e "$STATE/lightdm-gtk-greeter.conf.orig" ] ||
        [ -e "$STATE/kali-login.divert" ]; then
        echo "a login-screen change is active; run 'tenfold-tweak restore --login' first" >&2
        exit 1
    fi
    rm -rf "$LIB" "$STATE"
    rm -f "$BIN" "$DESKTOP_FILE" "$OLD_DESKTOP_FILE"
    echo "removed $LIB, $BIN and $DESKTOP_FILE"
    exit 0
fi

tmp=""
trap 'rm -rf "$tmp"' EXIT  # temp copy of the old login image; kept copies are moved out first
migrate_prepare

rm -rf "$LIB.new"
install -d -o root -g root -m 0755 "$LIB.new" "$LIB.new/tenfold_tweak"
for f in $FILES; do
    # cp -P never dereferences; re-check the copy inside the root-owned staging dir (no TOCTOU).
    cp -P -- "$SRC/$f" "$LIB.new/$f"
    check_regular "$LIB.new/$f"
    chown root:root "$LIB.new/$f"
    chmod 0644 "$LIB.new/$f"
done
chmod 0755 "$LIB.new/tenfold-tweak"
rm -rf "$LIB"
mv "$LIB.new" "$LIB"
ln -sfn "$LIB/tenfold-tweak" "$BIN"

install -d -m 0755 "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Tenfold Tweak
Comment=Change desktop, lock screen and login screen backgrounds
Exec=$BIN gui
Icon=preferences-desktop-wallpaper
Terminal=false
Categories=Settings;DesktopSettings;
EOF
chmod 0644 "$DESKTOP_FILE"
rm -f "$OLD_DESKTOP_FILE"

migrate_finish
echo "installed to $LIB; run: tenfold-tweak"
