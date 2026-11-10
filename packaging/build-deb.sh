#!/bin/sh
# Build dist/tenfold-tweaks_<VERSION>_all.deb with dpkg-deb (no debhelper, no fakeroot).
# Usage: sh packaging/build-deb.sh
# Env (tests): BUILD_DIR, DIST_DIR, DEB_VERSION override build/, dist/ and packaging/VERSION.
set -eu
umask 022

SRC=$(cd "$(dirname "$0")/.." && pwd -P)
PKG=tenfold-tweaks
BUILD=${BUILD_DIR:-$SRC/build}
STAGE=$BUILD/deb
OUT=${DIST_DIR:-$SRC/dist}
SHARE=usr/share/$PKG
HELPER=usr/libexec/$PKG/root-helper
# Explicit file list: nothing is globbed from the source tree into the package.
MODULES="__init__ cleaner cli core desktop detect gui image migrate root_helper system"
LAUNCHERS="tenfold-tweaks tenfold-tweaks-gui"
MAINT="preinst postinst prerm postrm"

die() {
    echo "build-deb: $*" >&2
    exit 1
}

check_regular() {
    if [ -L "$1" ] || [ ! -f "$1" ]; then die "$1 is not a regular file"; fi
}

VERSION=${DEB_VERSION:-$(cat "$SRC/packaging/VERSION")}
printf '%s\n' "$VERSION" | grep -Eqx '[0-9][0-9A-Za-z.+~]*' || die "bad version '$VERSION'"

# Every module in the source tree must be listed (a new module would otherwise be left out).
for f in "$SRC"/tenfold_tweak/*.py; do
    name=$(basename "$f" .py)
    case " $MODULES " in
        *" $name "*) ;;
        *) die "tenfold_tweak/$name.py is not in MODULES" ;;
    esac
done

rm -rf "$STAGE"
install -d -m 0755 "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/$SHARE/tenfold_tweak" "$STAGE/usr/libexec/$PKG" \
    "$STAGE/usr/share/applications" "$STAGE/usr/share/polkit-1/actions"

for name in $MODULES; do
    check_regular "$SRC/tenfold_tweak/$name.py"
    install -m 0644 "$SRC/tenfold_tweak/$name.py" "$STAGE/$SHARE/tenfold_tweak/$name.py"
done
for name in $LAUNCHERS; do
    check_regular "$SRC/$name"
    install -m 0755 "$SRC/$name" "$STAGE/$SHARE/$name"
    ln -s "../share/$PKG/$name" "$STAGE/usr/bin/$name"
done

check_regular "$SRC/tenfold_tweak/root_helper.py"
[ "$(head -n 1 "$SRC/tenfold_tweak/root_helper.py")" = "#!/usr/bin/python3 -I" ] ||
    die "root_helper.py must start with '#!/usr/bin/python3 -I'"
install -m 0755 "$SRC/tenfold_tweak/root_helper.py" "$STAGE/$HELPER"
cmp -s "$SRC/tenfold_tweak/root_helper.py" "$STAGE/$HELPER" || die "root-helper differs from root_helper.py"

for f in io.github.tenfold.tenfold-tweaks.desktop io.github.tenfold.tenfold-tweaks.policy; do
    check_regular "$SRC/packaging/files/$f"
done
install -m 0644 "$SRC/packaging/files/io.github.tenfold.tenfold-tweaks.desktop" "$STAGE/usr/share/applications/"
install -m 0644 "$SRC/packaging/files/io.github.tenfold.tenfold-tweaks.policy" "$STAGE/usr/share/polkit-1/actions/"
grep -qF "<annotate key=\"org.freedesktop.policykit.exec.path\">/$HELPER</annotate>" \
    "$STAGE/usr/share/polkit-1/actions/io.github.tenfold.tenfold-tweaks.policy" || die "polkit exec.path is not /$HELPER"

if [ -n "$(find "$STAGE" \( -name '*.py[co]' -o -name __pycache__ \) -print)" ]; then die "bytecode in the package"; fi

# Placeholders are filled in common.sh before it is inserted: sed 'r' does not re-scan inserted text.
COMMON=$BUILD/common.sh
check_regular "$SRC/packaging/maint/common.sh"
sed "s/@VERSION@/$VERSION/g" "$SRC/packaging/maint/common.sh" >"$COMMON"
for s in $MAINT; do
    check_regular "$SRC/packaging/maint/$s"
    sed -e "/^#COMMON#\$/{r $COMMON" -e 'd' -e '}' "$SRC/packaging/maint/$s" >"$STAGE/DEBIAN/$s"
    if grep -qE '#COMMON#|@[A-Z]+@' "$STAGE/DEBIAN/$s"; then die "placeholder left in $s"; fi
    sh -n "$STAGE/DEBIAN/$s" || die "syntax error in $s"
    chmod 0755 "$STAGE/DEBIAN/$s"
done
rm -f "$COMMON"

SIZE=$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
sed -e "s/@VERSION@/$VERSION/g" -e "s/@SIZE@/$SIZE/g" "$SRC/packaging/control.in" >"$STAGE/DEBIAN/control"
chmod 0644 "$STAGE/DEBIAN/control"
(cd "$STAGE" && find usr -type f | LC_ALL=C sort | xargs md5sum) >"$STAGE/DEBIAN/md5sums"
chmod 0644 "$STAGE/DEBIAN/md5sums"

mkdir -p "$OUT"
DEB=$OUT/${PKG}_${VERSION}_all.deb
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" >/dev/null
echo "$DEB"
