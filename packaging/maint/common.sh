# Shared helpers for the tenfold-tweaks maintainer scripts (inserted at build time).
# Every filesystem path is under $DPKG_ROOT; dpkg-divert reads DPKG_ROOT/DPKG_ADMINDIR itself.
# Functions end with "return 0" and test with if/then so set -e behaves the same in dash and bash.

TT_R=${DPKG_ROOT:-}
TT_VERSION=@VERSION@
TT_PKG=tenfold-tweaks
TT_BG=/usr/share/desktop-base/kali-theme/login/background-blurred
TT_OLD_SFX=.tenfold-tweak
TT_NEW_SFX=.tenfold-tweaks
TT_RESTORE_SFX=.tenfold-tweaks-restore
TT_STATE=/var/lib/tenfold-tweaks
TT_OLD_STATE=/var/lib/tenfold-tweak
TT_MARKER=.legacy-migrated
TT_RESCUE=/var/backups/tenfold-tweaks
TT_HELPER=/usr/libexec/tenfold-tweaks/root-helper
TT_LIGHTDM_CONF=/etc/lightdm/lightdm-gtk-greeter.conf
TT_LIGHTDM_ORIG=lightdm-gtk-greeter.conf.orig
TT_GRESOURCE_ORIG=gnome-shell-theme.gresource.orig
TT_OLD_LIGHTDM_DIR=/usr/local/share/backgrounds/tenfold-tweak
TT_LIGHTDM_DIR=$TT_STATE/lightdm
TT_OLD_LIB=/usr/local/lib/tenfold-tweak
TT_OLD_BIN=/usr/local/bin/tenfold-tweak
TT_OLD_DESKTOPS="/usr/local/share/applications/io.github.tenfold.TenfoldTweak.desktop /usr/local/share/applications/tenfold-tweak.desktop"
TT_TMP_DIRS="/usr/share/gnome-shell /usr/share/desktop-base/kali-theme/login /etc/lightdm"
TT_MARKED=

tt_die() {
    echo "tenfold-tweaks: $*" >&2
    exit 1
}

tt_warn() {
    echo "tenfold-tweaks: warning: $*" >&2
}

tt_exists() {
    [ -e "$1" ] || [ -L "$1" ]
}

tt_regular() {
    [ -f "$1" ] && [ ! -L "$1" ]
}

# Real directory owned by root and not writable by group/others.
tt_root_dir() {
    [ -d "$1" ] && [ ! -L "$1" ] && [ "$(stat -c %u -- "$1")" = 0 ] &&
        [ $((0$(stat -c %a -- "$1") & 022)) -eq 0 ]
}

# tt_root_dir for DIR and its parent, so no path component can be swapped by a non-root user.
tt_root_tree() {
    tt_root_dir "$(dirname -- "$1")" && tt_root_dir "$1"
}

# Same stock file: symlinks with the same target, or regular files with the same bytes.
tt_same() {
    if [ -L "$1" ] && [ -L "$2" ]; then
        [ "$(readlink -- "$1")" = "$(readlink -- "$2")" ]
    elif [ -L "$1" ] || [ -L "$2" ]; then
        return 1
    else
        tt_regular "$1" && tt_regular "$2" && cmp -s -- "$1" "$2"
    fi
}

# dpkg-divert as OWNER (LOCAL or a package); database only, files are never renamed.
tt_dv() {
    _o=$1
    shift
    if [ "$_o" = LOCAL ]; then
        dpkg-divert --quiet --local --no-rename "$@"
    else
        dpkg-divert --quiet --package "$_o" --no-rename "$@"
    fi
}

# Marker written before the legacy migration starts; it moves with the state dir.
tt_marker() {
    if tt_exists "$TT_R$TT_STATE/$TT_MARKER"; then
        echo "$TT_R$TT_STATE/$TT_MARKER"
    elif tt_exists "$TT_R$TT_OLD_STATE/$TT_MARKER"; then
        echo "$TT_R$TT_OLD_STATE/$TT_MARKER"
    fi
    return 0
}

# Diversion state for FROM_SFX FROM_OWNER TO_SFX TO_OWNER (ADR 002 table):
# done, step4, step1, step2, step3 (crash between 2 and 3, needs TT_MARKED), none, or error:<reason>.
tt_divert_state() {
    _p=$TT_BG
    _f=$TT_R$_p$1
    _t=$TT_R$_p$3
    if ! _true=$(dpkg-divert --truename "$_p") || ! _own=$(dpkg-divert --listpackage "$_p"); then
        echo "error:dpkg-divert cannot read the diversion of $_p"
        return 0
    fi
    if [ "$_true" = "$_p$3" ] && [ "$_own" = "$4" ]; then
        if tt_exists "$_f"; then echo step4; else echo done; fi
    elif [ "$_true" = "$_p$1" ] && [ "$_own" = "$2" ]; then
        if ! tt_exists "$_t"; then
            echo step1
        elif tt_exists "$_f" && tt_same "$_f" "$_t"; then
            echo step2
        else
            echo "error:unexpected file $_p$3"
        fi
    elif [ "$_true" = "$_p" ] && [ -z "$_own" ]; then
        if ! tt_exists "$_f"; then
            echo none
        elif [ -n "$TT_MARKED" ] && tt_exists "$_t" && tt_same "$_f" "$_t" && tt_regular "$TT_R$_p"; then
            echo step3
        else
            echo "error:unexpected file $_p$1 without a diversion"
        fi
    else
        echo "error:$_p is diverted to $_true by ${_own:-unknown}, not by $2"
    fi
    return 0
}

# ADR 002 migration of the login image diversion; the image at the path itself is never touched.
# Each step is undone on failure, so the diversion is left in the FROM or TO state.
tt_divert_migrate() {
    _state=$(tt_divert_state "$@")
    _p=$TT_BG
    _f=$TT_R$_p$1
    _t=$TT_R$_p$3
    _made=
    case $_state in
        error:*) tt_die "${_state#error:}" ;;
        none | done) return 0 ;;
        step4)
            rm -f -- "$_f"
            sync
            return 0
            ;;
    esac
    # 1: duplicate the stock file (a symlink on Kali) under the new divert name.
    if [ "$_state" = step1 ] && tt_exists "$_f"; then
        rm -f -- "$_t.tmp"
        if [ -L "$_f" ]; then
            ln -s -- "$(readlink -- "$_f")" "$_t.tmp" || tt_die "cannot copy $_p$1"
        else
            cp -p -- "$_f" "$_t.tmp" || tt_die "cannot copy $_p$1"
        fi
        if ! mv -f -- "$_t.tmp" "$_t"; then
            rm -f -- "$_t.tmp"
            tt_die "cannot copy $_p$1"
        fi
        _made=1
    fi
    # 2: drop the old diversion record.
    if [ "$_state" != step3 ]; then
        if ! tt_dv "$2" --divert "$_p$1" --remove "$_p"; then
            if [ -n "$_made" ]; then rm -f -- "$_t"; fi
            tt_die "cannot remove the diversion to $_p$1"
        fi
    fi
    # 3: add the new diversion record.
    if ! tt_dv "$4" --divert "$_p$3" --add "$_p"; then
        tt_dv "$2" --divert "$_p$1" --add "$_p" || tt_die "cannot add the diversion to $_p$3, and restoring the old one failed"
        if [ -n "$_made" ]; then rm -f -- "$_t"; fi
        tt_die "cannot add the diversion to $_p$3"
    fi
    # 4: the old stock copy is no longer referenced.
    rm -f -- "$_f"
    sync
    return 0
}

# Keep a copy of FILE as RESCUE/PREFIX-<sha12>.<ext>; never overwrites; prints the path.
tt_rescue() {
    if ! tt_regular "$1"; then return 0; fi
    case $(od -An -N8 -tx1 -- "$1" | tr -d ' \n') in
        89504e470d0a1a0a*) _ext=png ;;
        ffd8ff*) _ext=jpg ;;
        *) _ext=img ;;
    esac
    _sum=$(sha256sum -- "$1" | cut -c1-12)
    _dir=$TT_R$TT_RESCUE
    mkdir -p -- "$(dirname -- "$_dir")"
    if ! tt_exists "$_dir"; then mkdir -m 0755 -- "$_dir"; fi
    tt_root_dir "$_dir" || tt_die "$TT_RESCUE is not a root-owned directory"
    _dst=$_dir/$2-$_sum.$_ext
    if ! tt_exists "$_dst"; then
        _tmp=$(mktemp "$_dir/.tenfold-tweaks-XXXXXX.tmp")
        if cp -- "$1" "$_tmp" && chmod 0644 "$_tmp"; then
            ln -- "$_tmp" "$_dst" 2>/dev/null || true
        fi
        rm -f -- "$_tmp"
        sync
    fi
    cmp -s -- "$1" "$_dst" || tt_die "cannot save $1 to $TT_RESCUE"
    echo "$_dst"
    return 0
}

tt_list_rescued() {
    for _f in "$TT_R$TT_RESCUE"/*; do
        if tt_regular "$_f"; then echo "saved login image: $_f"; fi
    done
    return 0
}

# Atomic replace of the LightDM greeter conf with SRC filtered by sed ARGS; always 0644, never via a symlink.
tt_write_conf() {
    _src=$1
    shift
    _conf=$TT_R$TT_LIGHTDM_CONF
    if tt_exists "$_conf" && ! tt_regular "$_conf"; then tt_die "$TT_LIGHTDM_CONF is not a regular file"; fi
    _tmp=$(mktemp "$(dirname -- "$_conf")/.tenfold-tweaks-conf.XXXXXX") || tt_die "cannot write $TT_LIGHTDM_CONF"
    if ! { sed -e '' "$@" "$_src" >"$_tmp" &&
        chmod 0644 "$_tmp" && mv -f -- "$_tmp" "$_conf"; }; then
        rm -f -- "$_tmp"
        tt_die "cannot write $TT_LIGHTDM_CONF"
    fi
    return 0
}

tt_legacy_present() {
    for _x in "$TT_OLD_STATE" "$TT_OLD_LIB" "$TT_OLD_LIB.new" "$TT_OLD_BIN" "$TT_OLD_LIGHTDM_DIR" \
        $TT_OLD_DESKTOPS "$TT_BG$TT_OLD_SFX"; do
        if tt_exists "$TT_R$_x"; then return 0; fi
    done
    [ "$(dpkg-divert --truename "$TT_BG")" = "$TT_BG$TT_OLD_SFX" ]
}

# Read-only checks before anything changes; sets TT_DO when legacy state or a diversion needs moving.
# Leftover install.sh files alone need no migration (postinst removes them).
tt_preflight_legacy() {
    TT_DO=
    if ! tt_legacy_present; then return 0; fi
    _os=$TT_R$TT_OLD_STATE
    _ns=$TT_R$TT_STATE
    if [ -n "$(tt_marker)" ]; then TT_MARKED=1; fi
    if tt_exists "$_os"; then
        tt_root_dir "$_os" || tt_die "$TT_OLD_STATE is not a root-owned directory; nothing was changed"
        if tt_exists "$_ns"; then
            tt_root_dir "$_ns" || tt_die "$TT_STATE is not a root-owned directory; nothing was changed"
            if [ -n "$(ls -A -- "$_ns" | grep -vx lock || true)" ]; then
                tt_die "both $TT_OLD_STATE and $TT_STATE hold state; nothing was changed"
            fi
        fi
        _orig=$_os/$TT_LIGHTDM_ORIG
    else
        _orig=$_ns/$TT_LIGHTDM_ORIG
    fi
    _st=$(tt_divert_state "$TT_OLD_SFX" LOCAL "$TT_NEW_SFX" "$TT_PKG")
    case $_st in
        error:*) tt_die "${_st#error:}; nothing was changed" ;;
        step*) TT_DO=1 ;;
    esac
    if tt_exists "$_os" || [ -n "$TT_MARKED" ]; then TT_DO=1; fi
    # The marker must land in the old state dir, so abort-install never moves a state dir that was not ours.
    if [ -n "$TT_DO" ] && [ -z "$TT_MARKED" ] && ! tt_exists "$_os" && tt_exists "$_ns"; then
        tt_die "$TT_STATE exists but $TT_OLD_STATE does not; remove $TT_STATE by hand; nothing was changed"
    fi
    if tt_exists "$_orig"; then
        _conf=$TT_R$TT_LIGHTDM_CONF
        tt_regular "$_conf" || tt_die "$TT_LIGHTDM_CONF is not a regular file; nothing was changed"
        for _e in png jpg; do
            if grep -qF "$TT_OLD_LIGHTDM_DIR/login.$_e" "$_conf"; then
                _img=$TT_R$TT_OLD_LIGHTDM_DIR/login.$_e
                if tt_exists "$TT_R$TT_OLD_LIGHTDM_DIR" && ! tt_root_dir "$TT_R$TT_OLD_LIGHTDM_DIR"; then
                    tt_die "$TT_OLD_LIGHTDM_DIR is not a root-owned directory; nothing was changed"
                elif ! tt_exists "$_img"; then
                    tt_warn "$TT_OLD_LIGHTDM_DIR/login.$_e is missing; the LightDM background will be empty"
                elif ! tt_regular "$_img"; then
                    tt_die "$TT_OLD_LIGHTDM_DIR/login.$_e is not a regular file; nothing was changed"
                fi
            fi
        done
    fi
    return 0
}

# S4: copy the LightDM image under the state dir and point the greeter conf at it.
tt_lightdm_migrate() {
    _ns=$TT_R$TT_STATE
    _conf=$TT_R$TT_LIGHTDM_CONF
    if ! tt_exists "$_ns/$TT_LIGHTDM_ORIG" || ! grep -qsF "$TT_OLD_LIGHTDM_DIR/login." "$_conf"; then
        return 0
    fi
    _ld=$TT_R$TT_LIGHTDM_DIR
    if ! tt_exists "$_ld"; then mkdir -m 0755 -- "$_ld"; fi
    for _e in png jpg; do
        _img=$TT_R$TT_OLD_LIGHTDM_DIR/login.$_e
        if grep -qF "$TT_OLD_LIGHTDM_DIR/login.$_e" "$_conf" && tt_exists "$_img"; then
            tt_root_dir "$TT_R$TT_OLD_LIGHTDM_DIR" || tt_die "$TT_OLD_LIGHTDM_DIR is not a root-owned directory"
            _tmp=$(mktemp "$_ld/.tenfold-tweaks-XXXXXX.tmp") || tt_die "cannot copy $TT_OLD_LIGHTDM_DIR/login.$_e"
            # cp -P plus the checks: a symlink swapped in after the preflight is copied as a link and refused.
            if ! { cp -P -- "$_img" "$_tmp" && tt_regular "$_tmp" && cmp -s -- "$_img" "$_tmp" &&
                chmod 0644 "$_tmp" && mv -f -- "$_tmp" "$_ld/login.$_e"; }; then
                rm -f -- "$_tmp"
                tt_die "cannot copy $TT_OLD_LIGHTDM_DIR/login.$_e"
            fi
        fi
    done
    tt_write_conf "$_conf" -e "s#$TT_OLD_LIGHTDM_DIR/login\\.png#$TT_LIGHTDM_DIR/login.png#g" \
        -e "s#$TT_OLD_LIGHTDM_DIR/login\\.jpg#$TT_LIGHTDM_DIR/login.jpg#g"
    return 0
}

# preinst install|upgrade: move an install.sh (tenfold-tweak) install over; exit 1 aborts the install.
tt_migrate_legacy() {
    tt_preflight_legacy
    if [ -z "$TT_DO" ]; then return 0; fi
    _os=$TT_R$TT_OLD_STATE
    _ns=$TT_R$TT_STATE
    echo "tenfold-tweaks: migrating the tenfold-tweak install"
    if ! tt_exists "$_os" && ! tt_exists "$_ns"; then mkdir -m 0755 -- "$_os"; fi
    # S0: the old helper holds this lock while it works.
    if tt_regular "$_os/lock"; then
        exec 9<"$_os/lock"
        flock -n 9 || tt_die "tenfold-tweak is running; close it and try again"
    fi
    if [ -z "$(tt_marker)" ]; then : >"$_os/$TT_MARKER"; fi
    TT_MARKED=1
    # S1
    tt_divert_migrate "$TT_OLD_SFX" LOCAL "$TT_NEW_SFX" "$TT_PKG"
    # S2: the only copy of an old login image; the source goes in postinst once the copy is verified.
    for _m in "$_os/migrated-login" "$_ns/migrated-login"; do
        if tt_regular "$_m"; then
            _saved=$(tt_rescue "$_m" migrated-login) || exit 1
            echo "saved login image: $_saved"
        fi
    done
    # S3
    if tt_exists "$_os"; then
        if tt_exists "$_ns"; then
            rm -f -- "$_ns/lock"
            rmdir -- "$_ns" || tt_die "cannot replace $TT_STATE"
        fi
        mv -- "$_os" "$_ns" || tt_die "cannot move $TT_OLD_STATE to $TT_STATE"
    fi
    # S4
    tt_lightdm_migrate
    sync
    return 0
}

# postrm abort-install|abort-upgrade: reverse tt_migrate_legacy (only with the marker).
tt_unmigrate_legacy() {
    _mk=$(tt_marker)
    if [ -z "$_mk" ]; then return 0; fi
    TT_MARKED=1
    _os=$TT_R$TT_OLD_STATE
    _ns=$TT_R$TT_STATE
    echo "tenfold-tweaks: restoring the tenfold-tweak install"
    if [ "$_mk" = "$_ns/$TT_MARKER" ]; then
        _conf=$TT_R$TT_LIGHTDM_CONF
        if ! tt_regular "$_conf"; then
            if tt_exists "$_conf"; then
                tt_warn "$TT_LIGHTDM_CONF is not a regular file, left untouched; point its background back to $TT_OLD_LIGHTDM_DIR"
            fi
        elif grep -qF "$TT_LIGHTDM_DIR/login." "$_conf"; then
            tt_write_conf "$_conf" -e "s#$TT_LIGHTDM_DIR/login\\.png#$TT_OLD_LIGHTDM_DIR/login.png#g" \
                -e "s#$TT_LIGHTDM_DIR/login\\.jpg#$TT_OLD_LIGHTDM_DIR/login.jpg#g"
        fi
        rm -rf -- "${_ns:?}/lightdm"
        if tt_exists "$_os"; then tt_die "both $TT_OLD_STATE and $TT_STATE exist; restore them by hand"; fi
        mv -- "$_ns" "$_os"
    fi
    tt_divert_migrate "$TT_NEW_SFX" "$TT_PKG" "$TT_OLD_SFX" LOCAL
    rm -f -- "$_os/$TT_MARKER"
    sync
    return 0
}

# postinst configure: remove what install.sh created; never fails the configure.
tt_cleanup_legacy() {
    for _d in "$TT_OLD_LIB" "$TT_OLD_LIB.new"; do
        if tt_root_tree "$TT_R$_d"; then
            rm -rf -- "$TT_R$_d" || tt_warn "cannot remove $_d"
        elif tt_exists "$TT_R$_d"; then
            tt_warn "left $_d in place (not a root-owned directory)"
        fi
    done
    if [ -L "$TT_R$TT_OLD_BIN" ]; then
        case $(readlink -- "$TT_R$TT_OLD_BIN") in
            "$TT_OLD_LIB"/*) rm -f -- "$TT_R$TT_OLD_BIN" ;;
        esac
    fi
    for _f in $TT_OLD_DESKTOPS; do
        if tt_regular "$TT_R$_f"; then rm -f -- "$TT_R$_f"; fi
    done
    _l=$TT_R$TT_OLD_LIGHTDM_DIR
    if tt_root_tree "$_l" && ! grep -qsF "$TT_OLD_LIGHTDM_DIR/" "$TT_R$TT_LIGHTDM_CONF"; then
        rm -rf -- "$_l" || tt_warn "cannot remove $TT_OLD_LIGHTDM_DIR"
        rmdir -- "$(dirname -- "$_l")" 2>/dev/null || true
    fi
    for _t in "$TT_R"/var/lib/.tenfold-tweak-migrate.*; do
        if tt_exists "$_t"; then rm -rf -- "$_t"; fi
    done
    _m=$TT_R$TT_STATE/migrated-login
    if tt_regular "$_m"; then
        if (tt_rescue "$_m" migrated-login) >/dev/null; then
            rm -f -- "$_m"
        else
            tt_warn "kept $TT_STATE/migrated-login"
        fi
    fi
    return 0
}

# postrm purge fallback: a diversion left by a forced remove; the image is saved first.
tt_divert_release() {
    _own=$(dpkg-divert --listpackage "$TT_BG") || tt_die "dpkg-divert cannot read the diversion of $TT_BG"
    if [ "$_own" != "$TT_PKG" ]; then return 0; fi
    _p=$TT_R$TT_BG
    _aside=$_p$TT_RESTORE_SFX
    if tt_regular "$_p"; then
        tt_rescue "$_p" login >/dev/null || exit 1
    fi
    # A leftover aside may be the only copy of a user image; tt_purge removes it after this.
    if tt_regular "$_aside"; then
        tt_rescue "$_aside" login >/dev/null || exit 1
    fi
    # Stock renamed back but the record still there: PATH is the stock file, only drop the record.
    if ! tt_exists "$_p$TT_NEW_SFX" && tt_exists "$_p"; then
        tt_dv "$TT_PKG" --divert "$TT_BG$TT_NEW_SFX" --remove "$TT_BG" ||
            tt_die "cannot release the diversion of $TT_BG"
        return 0
    fi
    _moved=
    if tt_exists "$_p"; then
        rm -f -- "$_aside"
        mv -- "$_p" "$_aside"
        _moved=1
    fi
    if ! dpkg-divert --quiet --package "$TT_PKG" --rename --divert "$TT_BG$TT_NEW_SFX" --remove "$TT_BG"; then
        if [ -n "$_moved" ] && ! tt_exists "$_p"; then mv -- "$_aside" "$_p"; fi
        tt_die "cannot release the diversion of $TT_BG"
    fi
    if [ -n "$_moved" ]; then rm -f -- "$_aside"; fi
    return 0
}

# postrm purge: drop every system trace except the saved images in RESCUE.
tt_purge() {
    tt_divert_release
    _ns=$TT_R$TT_STATE
    _p=$TT_R$TT_BG
    if tt_regular "$_p$TT_RESTORE_SFX"; then
        tt_rescue "$_p$TT_RESTORE_SFX" login >/dev/null || exit 1
        rm -f -- "$_p$TT_RESTORE_SFX"
    fi
    if tt_regular "$_ns/$TT_LIGHTDM_ORIG"; then
        for _i in "$_ns"/lightdm/login.*; do
            if tt_regular "$_i"; then tt_rescue "$_i" login >/dev/null || exit 1; fi
        done
        if tt_regular "$TT_R$TT_LIGHTDM_CONF"; then
            tt_write_conf "$_ns/$TT_LIGHTDM_ORIG"
        elif tt_exists "$TT_R$TT_LIGHTDM_CONF"; then
            tt_warn "$TT_LIGHTDM_CONF is not a regular file, left untouched; check its background (the image under $TT_LIGHTDM_DIR is removed)"
        fi
    fi
    if tt_exists "$_ns/$TT_GRESOURCE_ORIG"; then
        echo "tenfold-tweaks: the GNOME login theme may still show a custom image;"
        echo "  run: sudo apt install --reinstall gnome-shell-common"
    fi
    for _d in $TT_TMP_DIRS; do
        for _t in "$TT_R$_d"/.tenfold-tweaks-*; do
            if tt_regular "$_t"; then rm -f -- "$_t"; fi
        done
    done
    rm -rf -- "${_ns:?}"
    rmdir -- "$TT_R$TT_RESCUE" 2>/dev/null || true
    tt_list_rescued
    echo "tenfold-tweaks: files in your home directory were not touched. To remove them, as your user run:"
    echo "  rm -rf ~/.local/state/tenfold-tweaks ~/.local/share/backgrounds/tenfold-tweaks"
    echo "  (next time, run 'tenfold-tweaks restore --all' before removing the package to undo desktop and lock changes)"
    return 0
}
