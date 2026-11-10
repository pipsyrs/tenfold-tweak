#!/usr/bin/python3 -I
"""Root-side helper for login-screen targets.

Run only as: pkexec /usr/libexec/tenfold-tweaks/root-helper ACTION [ARG...]
Actions: gdm-apply IMAGE | gdm-restore | lightdm-apply IMAGE | lightdm-restore
         | clean ID... [--expect-autoremove HEX64]  (prints one JSON line)
Maintainer scripts only (never via pkexec): pkg-configure | pkg-remove
Exit codes: 0 ok, 3 nothing to restore, 1 failure (one-line reason on stderr).
Standalone on purpose: stdlib + gi only, imports nothing from the package.
"""

import contextlib
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NOTHING = 3

MAX_IMAGE_SIZE = 64 * 1024 * 1024
# The greeter decodes the image in full; 4096x4096 RGBA is 64 MiB.
MAX_LOGIN_DIM = 4096
JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
CHILD_TIMEOUT = 120
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

PKG = "tenfold-tweaks"
STATE_DIR = "/var/lib/tenfold-tweaks"
# Fixed root-owned install path; the --dump child never re-reads a user-writable copy.
INSTALLED_HELPER = "/usr/libexec/tenfold-tweaks/root-helper"
# Saved login images (pkg-remove); never removed by purge.
RESCUE_DIR = "/var/backups/tenfold-tweaks"
# Every temp file the helper creates in a system dir starts with this.
TMP_PREFIX = ".tenfold-tweaks-"

GRESOURCE = "/usr/share/gnome-shell/gnome-shell-theme.gresource"
GRESOURCE_NEW = "/usr/share/gnome-shell/" + TMP_PREFIX + "gnome-shell-theme.gresource.new"
GRESOURCE_ORIG = STATE_DIR + "/gnome-shell-theme.gresource.orig"
GRESOURCE_META = STATE_DIR + "/gnome-shell-theme.meta"
THEME_PREFIX = "/org/gnome/shell/theme/"
LOGIN_ENTRY = THEME_PREFIX + "tenfold-tweaks-login."
# Entry name used before the rename; still recognised as patched.
LEGACY_LOGIN_ENTRY = THEME_PREFIX + "tenfold-tweak-login."
PATCHED_CSS = ("gnome-shell-dark.css", "gnome-shell-light.css", "gnome-shell-high-contrast.css")
CSS_BLOCK = (
    "\n/* tenfold-tweaks */\n"
    '#lockDialogGroup {{ background: #000 url("resource://{entry}"); '
    "background-size: cover; background-repeat: no-repeat; background-position: center; }}\n"
)
REINSTALL_HINT = "run: sudo apt install --reinstall gnome-shell-common"

# Kali patches gnome-shell (ui/screenShield.js _ensureUnlockDialog) to set an inline style on
# #lockDialogGroup with this hardcoded image; inline style beats the theme CSS, so on such a
# shell the login background is changed by diverting this path instead of patching the gresource.
SHELL_LIBS = "/usr/lib/gnome-shell/libshell-*.so"
KALI_LOGIN_BG = "/usr/share/desktop-base/kali-theme/login/background-blurred"
KALI_LOGIN_BG_DIVERTED = KALI_LOGIN_BG + ".tenfold-tweaks"
# Divert suffix used before the rename (local diversion); migrated by the package preinst.
LEGACY_DIVERT_SUFFIX = ".tenfold-tweak"
LEGACY_HINT = "old tenfold-tweak diversion not migrated; reinstall the tenfold-tweaks package"
RESTORE_SUFFIX = ".tenfold-tweaks-restore"
DPKG_DIVERT = "/usr/bin/dpkg-divert"
# dpkg-divert takes no dpkg lock (spike D0): runtime gdm-* actions hold these so apt cannot run meanwhile.
DPKG_LOCKS = ("/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock")
DPKG_BUSY = "dpkg/apt is running; try again later"
APT_ARCHIVES_LOCK = "/var/cache/apt/archives/lock"

LIGHTDM_CONF = "/etc/lightdm/lightdm-gtk-greeter.conf"
LIGHTDM_ORIG = STATE_DIR + "/lightdm-gtk-greeter.conf.orig"
LIGHTDM_IMAGE_DIR = STATE_DIR + "/lightdm"

# clean: the root side only ever takes these IDs, never a path.
CLEAN_IDS = ("apt-cache", "journal", "tool-images", "autoremove")
JOURNAL_DAYS = 7
JOURNAL_RETENTION = f"{JOURNAL_DAYS}d"
APT_GET = "/usr/bin/apt-get"
JOURNALCTL = "/usr/bin/journalctl"
DPKG_QUERY = "/usr/bin/dpkg-query"
APT_CACHE_DIR = "/var/cache/apt"
JOURNAL_DIR = "/var/log/journal"
AUTOREMOVE_TIMEOUT = 900
CLEAN_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
# Kernel packages, incl. Kali's linux-binary-/linux-base-<version> naming; never removed.
KERNEL_PKG = re.compile(r"^linux-(image|headers|modules|modules-extra|kbuild)-|^linux-.*[0-9]+\.[0-9]+")
PKG_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]*(:[a-z0-9-]+)?$")
REMV = re.compile(r"^Remv (\S+)", re.M)
SIM_OTHER = re.compile(r"^(Inst|Conf) (\S+)", re.M)  # simulated installs/configures besides the removal
INTERRUPTED = "apt-get timed out and was stopped; run 'sudo dpkg --configure -a' to finish interrupted package changes"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
APT_LOCKED = ("Could not get lock", "Unable to acquire", "Unable to lock")
PM_BUSY = "package manager busy"
PLAN_CHANGED = "package list changed; preview again"
CLEAN_USAGE = "usage: root-helper clean ID... [--expect-autoremove HEX64]; ID in " + ", ".join(CLEAN_IDS)

PKG_ACTIONS = ("pkg-configure", "pkg-remove")
WARN_ONLY_ACTIONS = ("pkg-configure",)
MAINTSCRIPT_NAMES = ("preinst", "postinst", "prerm", "postrm")
# Rebased onto DPKG_ROOT for pkg-* actions (and their --dump child).
ROOTED_PATHS = ("INSTALLED_HELPER", "STATE_DIR", "RESCUE_DIR", "GRESOURCE", "GRESOURCE_NEW", "GRESOURCE_ORIG",
                "GRESOURCE_META", "SHELL_LIBS", "KALI_LOGIN_BG", "KALI_LOGIN_BG_DIVERTED", "LIGHTDM_CONF",
                "LIGHTDM_ORIG", "LIGHTDM_IMAGE_DIR")
_dpkg_root = ""         # DPKG_ROOT of a pkg-* run; "" means /
_dpkg_admindir = None   # DPKG_ADMINDIR of a pkg-* run


class HelperError(Exception):
    pass


# ---------- GVDB / GResource writer ----------

def _gvdb_hash(key: bytes) -> int:
    h = 5381
    for b in key:
        h = (h * 33 + (b - 256 if b >= 128 else b)) & 0xFFFFFFFF
    return h


def build_gresource(entries: dict) -> bytes:
    """Serialize {"/abs/path": bytes} into an uncompressed GResource (GVDB) file."""
    items = {}
    for path, data in entries.items():
        if not path.startswith("/") or path.endswith("/") or "//" in path:
            raise HelperError(f"invalid resource path: {path}")
        items[path] = {"value": bytes(data), "children": []}
        child = path
        while child != "/":
            parent = child[: child.rstrip("/").rfind("/") + 1]
            node = items.setdefault(parent, {"value": None, "children": []})
            if node["value"] is not None:
                raise HelperError(f"resource path conflict: {parent}")
            if child not in node["children"]:
                node["children"].append(child)
            child = parent
    parents = {c: p for p, n in items.items() for c in n["children"]}

    keys = list(items)
    n_items = len(keys)
    hashes = {k: _gvdb_hash(k.encode()) for k in keys}
    keys.sort(key=lambda k: (hashes[k] % n_items, k))
    index = {k: i for i, k in enumerate(keys)}
    # Bucket b starts at the count of items in lower buckets (items are sorted by bucket).
    counts = [0] * n_items
    for k in keys:
        counts[hashes[k] % n_items] += 1
    bucket_starts, total = [], 0
    for c in counts:
        bucket_starts.append(total)
        total += c

    out = bytearray(24)
    table_start = len(out)
    table_size = 8 + 4 * n_items + 24 * n_items
    out += bytes(table_size)

    def alloc(data: bytes, align: int):
        out.extend(bytes(-len(out) % align))
        start = len(out)
        out.extend(data)
        return start, len(out)

    records = []
    for k in keys:
        node = items[k]
        parent = parents.get(k)
        name = k[len(parent):] if parent else k
        key_start, key_end = alloc(name.encode(), 1)
        if node["value"] is not None:
            data = node["value"]
            # (uuay) wrapped in a variant; ay carries a trailing NUL like glib-compile-resources.
            blob = struct.pack("<II", len(data), 0) + data + b"\x00" + b"\x00(uuay)"
            v_start, v_end = alloc(blob, 8)
            kind = b"v"
        else:
            kids = sorted(node["children"])
            v_start, v_end = alloc(struct.pack(f"<{len(kids)}I", *(index[c] for c in kids)), 4)
            kind = b"L"
        records.append(struct.pack(
            "<IIIHccII", hashes[k], index[parent] if parent else 0xFFFFFFFF,
            key_start, key_end - key_start, kind, b"\x00", v_start, v_end))

    struct.pack_into("<II", out, table_start, 0, n_items)
    struct.pack_into(f"<{n_items}I", out, table_start + 8, *bucket_starts)
    out[table_start + 8 + 4 * n_items: table_start + table_size] = b"".join(records)
    struct.pack_into("<8sIIII", out, 0, b"GVariant", 0, 0, table_start, table_start + table_size)
    if len(out) > 0xFFFFFFFF:
        raise HelperError("resource file too large")
    return bytes(out)


def read_gresource(path: str) -> dict:
    """Load a GResource with Gio and return {"/abs/path": bytes}."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    try:
        res = Gio.Resource.load(path)
    except GLib.Error as e:
        raise HelperError(f"cannot load {path}: {e.message}") from None
    entries = {}

    def walk(prefix):
        for child in res.enumerate_children(prefix, Gio.ResourceLookupFlags.NONE):
            full = prefix + child
            if child.endswith("/"):
                walk(full)
            else:
                entries[full] = res.lookup_data(full, Gio.ResourceLookupFlags.NONE).get_data()

    try:
        walk("/")
    except GLib.Error as e:
        raise HelperError(f"cannot read {path}: {e.message}") from None
    return entries


def _dump_command(path: str) -> list:
    return ["/usr/bin/python3", "-I", INSTALLED_HELPER, "--dump", path]


def load_entries(path: str) -> dict:
    """Read a GResource in a child process: Gio trusts the file, so bad input could crash or hang it."""
    try:
        proc = subprocess.run(_dump_command(path), capture_output=True, timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        fail(f"theme file check timed out: {path}")
    if proc.returncode < 0:
        fail(f"theme file check crashed (signal {-proc.returncode}): {path}")
    if proc.returncode != 0:
        msg = proc.stderr.decode(errors="replace").strip().splitlines()
        fail(msg[-1] if msg else f"cannot read {path}")
    entries, buf, i = {}, proc.stdout, 0
    while i < len(buf):
        if i + 8 > len(buf):
            fail("truncated theme file dump")
        plen, dlen = struct.unpack_from("<II", buf, i)
        i += 8
        if i + plen + dlen > len(buf):
            fail("truncated theme file dump")
        entries[buf[i:i + plen].decode()] = buf[i + plen:i + plen + dlen]
        i += plen + dlen
    return entries


def dump_main(path: str) -> int:
    """Child mode: load an allowed GResource with Gio and write framed entries to stdout."""
    build = os.path.dirname(path)
    allowed = path in (GRESOURCE, GRESOURCE_ORIG) or (
        os.path.dirname(build) == STATE_DIR and os.path.basename(build).startswith("build-")
        and os.path.basename(path) == "new.gresource")
    if not allowed:
        fail("refusing to load this path")
    out = sys.stdout.buffer
    for p, data in sorted(read_gresource(path).items()):
        name = p.encode()
        out.write(struct.pack("<II", len(name), len(data)) + name + data)
    out.flush()
    return EXIT_OK


# ---------- common helpers ----------

def fail(msg: str):
    raise HelperError(msg)


def ensure_state_dir():
    try:
        st = os.lstat(STATE_DIR)
    except FileNotFoundError:
        os.mkdir(STATE_DIR, 0o755)
        st = os.lstat(STATE_DIR)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        fail(f"{STATE_DIR} is not a root-owned directory")


def fsync_dir(path: str):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(dest: str, data: bytes, mode: int, tmp: str = None, before_replace=None):
    """Write data to a unique temp file next to dest, fsync, chmod, then os.replace onto dest."""
    prefix = os.path.basename(tmp) if tmp else TMP_PREFIX + os.path.basename(dest)
    fd, tmp = tempfile.mkstemp(prefix=prefix + ".", suffix=".tmp", dir=os.path.dirname(dest))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        if before_replace:
            before_replace()
        os.replace(tmp, dest)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    fsync_dir(os.path.dirname(dest))


@contextlib.contextmanager
def action_lock():
    """Exclusive lock so concurrent helper runs cannot interleave."""
    ensure_state_dir()
    fd = os.open(os.path.join(STATE_DIR, "lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("another tenfold-tweaks run is in progress; close tenfold-tweaks and retry")
        yield
    finally:
        os.close(fd)


def require_regular(fd: int, msg: str):
    """Close fd and fail unless it refers to a regular file."""
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        fail(msg)


def read_regular(path: str, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    require_regular(fd, f"not a regular file: {path}")
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if st.st_size > limit:
            fail(f"file too large: {path}")
        return f.read(limit + 1)


def load_image(path: str):
    """Validate the image argument; return (bytes, ext)."""
    if not os.path.isabs(path) or "\x00" in path:
        fail("image path must be absolute")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        fail(f"cannot open image: {e.strerror}")
    require_regular(fd, "image is not a regular file")
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        # Only read files owned by the user who authorized pkexec/sudo.
        caller = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
        if caller is not None and str(st.st_uid) != caller:
            fail("image is not owned by the calling user")
        if not 0 < st.st_size <= MAX_IMAGE_SIZE:
            fail("image size must be between 1 byte and 64 MiB")
        data = f.read(MAX_IMAGE_SIZE + 1)
    if not 0 < len(data) <= MAX_IMAGE_SIZE:
        fail("image size must be between 1 byte and 64 MiB")
    if data.startswith(PNG_MAGIC):
        ext = "png"
    elif data.startswith(JPEG_MAGIC):
        ext = "jpg"
    else:
        fail("unsupported image format; supported: PNG, JPEG")
    width, height = image_dims(data, ext)
    if not (1 <= width <= MAX_LOGIN_DIM and 1 <= height <= MAX_LOGIN_DIM):
        fail(f"image is {width}x{height}; login images must be at most {MAX_LOGIN_DIM}x{MAX_LOGIN_DIM}")
    return data, ext


def image_dims(data: bytes, ext: str) -> tuple:
    """Width/height from the PNG IHDR or JPEG SOF header; no decoding as root."""
    if ext == "png":
        if len(data) < 24 or data[12:16] != b"IHDR":
            fail("malformed PNG header")
        return struct.unpack(">II", data[16:24])
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            fail("malformed JPEG header")
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
        elif marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
        elif marker in (0xD9, 0xDA):
            break
        elif marker in JPEG_SOF:
            if i + 9 > len(data):
                break
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        else:
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    fail("JPEG size header not found")


# ---------- GDM (gnome-shell theme gresource) ----------

def shell_version() -> str:
    try:
        out = subprocess.run(
            ["/usr/bin/dpkg-query", *dpkg_db_args(instdir=False), "-W", "-f=${Version}", "gnome-shell-common"],
            capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.CalledProcessError):
        fail("cannot read gnome-shell-common version")
    return out.stdout.strip()


def is_patched(entries: dict) -> bool:
    return any(p.startswith((LOGIN_ENTRY, LEGACY_LOGIN_ENTRY)) for p in entries)


def read_meta():
    try:
        with open(GRESOURCE_META, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def drop_gdm_backup():
    for p in (GRESOURCE_ORIG, GRESOURCE_META):
        if os.path.lexists(p):
            os.unlink(p)


def stock_entries(version: str) -> dict:
    """Return the stock theme entries, writing the backup when the installed file is stock."""
    meta = read_meta()
    if meta and meta.get("version") == version and os.path.isfile(GRESOURCE_ORIG):
        raw = read_regular(GRESOURCE_ORIG, 256 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != meta.get("sha256"):
            fail("backup checksum mismatch; " + REINSTALL_HINT)
        entries = load_entries(GRESOURCE_ORIG)
        if is_patched(entries):
            fail("backup is not stock; " + REINSTALL_HINT)
        return entries
    current = load_entries(GRESOURCE)
    if is_patched(current):
        fail("no stock backup for the installed gnome-shell; " + REINSTALL_HINT)
    raw = read_regular(GRESOURCE, 256 * 1024 * 1024)
    drop_gdm_backup()
    atomic_write(GRESOURCE_ORIG, raw, 0o644)
    meta = {"version": version, "sha256": hashlib.sha256(raw).hexdigest()}
    atomic_write(GRESOURCE_META, json.dumps(meta).encode(), 0o644)
    return current


def verify_entries(path: str, expected: dict):
    """Load path with Gio (in a child) and require every entry to round-trip byte-identical."""
    got = load_entries(path)
    if set(got) != set(expected):
        fail("rebuilt theme entry set mismatch")
    for p, data in expected.items():
        if got[p] != data:
            fail(f"rebuilt theme entry differs: {p}")


def gdm_gresource_apply(data: bytes, ext: str):
    if not os.path.isfile(GRESOURCE):
        fail(f"missing {GRESOURCE}")
    ensure_state_dir()
    version = shell_version()
    stock = stock_entries(version)

    entry = LOGIN_ENTRY + ext
    block = CSS_BLOCK.format(entry=entry).encode()
    new = dict(stock)
    for name in PATCHED_CSS:
        key = THEME_PREFIX + name
        if key not in new:
            fail(f"unexpected theme layout: missing {name}")
        new[key] = new[key] + block
    new[entry] = data

    tmpdir = tempfile.mkdtemp(prefix="build-", dir=STATE_DIR)
    try:
        built = build_gresource(new)
        tmpfile = os.path.join(tmpdir, "new.gresource")
        atomic_write(tmpfile, built, 0o600)
        try:
            verify_entries(tmpfile, new)
        except HelperError as e:
            fail(f"{e}; stock file left untouched")

        try:
            previous = read_regular(GRESOURCE, 256 * 1024 * 1024)
        except (OSError, HelperError):
            previous = None
        atomic_write(GRESOURCE, built, 0o644, tmp=GRESOURCE_NEW)
        try:
            if read_regular(GRESOURCE, len(built)) != built:
                fail("installed theme file differs from the verified build")
            verify_entries(GRESOURCE, new)
        except Exception as e:
            # Put back the previously installed (working) theme; stock if it was unreadable.
            if previous is None:
                previous = read_regular(GRESOURCE_ORIG, 256 * 1024 * 1024)
            atomic_write(GRESOURCE, previous, 0o644, tmp=GRESOURCE_NEW)
            fail(f"post-install check failed, previous theme restored: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def gdm_gresource_restore() -> int:
    if not os.path.isfile(GRESOURCE):
        fail(f"missing {GRESOURCE}")
    current = load_entries(GRESOURCE)
    if not is_patched(current):
        drop_gdm_backup()
        return EXIT_NOTHING
    ensure_state_dir()
    meta = read_meta()
    if not meta or meta.get("version") != shell_version() or not os.path.isfile(GRESOURCE_ORIG):
        fail("no matching stock backup; " + REINSTALL_HINT)
    raw = read_regular(GRESOURCE_ORIG, 256 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != meta.get("sha256"):
        fail("backup checksum mismatch; " + REINSTALL_HINT)
    stock = load_entries(GRESOURCE_ORIG)
    if is_patched(stock):
        fail("backup is not stock; " + REINSTALL_HINT)
    atomic_write(GRESOURCE, raw, 0o644, tmp=GRESOURCE_NEW)
    verify_entries(GRESOURCE, stock)
    drop_gdm_backup()
    return EXIT_OK


# ---------- GDM on Kali's patched gnome-shell (dpkg-divert) ----------

def kali_override_active() -> bool:
    """True when the installed gnome-shell hardcodes KALI_LOGIN_BG as the login background."""
    needle = b'url("' + KALI_LOGIN_BG[len(_dpkg_root):].encode() + b'")'
    for lib in sorted(glob.glob(SHELL_LIBS)):
        try:
            with open(lib, "rb") as f:
                if needle in f.read():
                    return True
        except OSError:
            continue
    return False


def dpkg_db_args(instdir: bool = True) -> list:
    """Explicit --instdir/--admindir of a pkg-* run; the child env never carries DPKG_* (spike D0)."""
    args = ["--instdir", _dpkg_root] if instdir and _dpkg_root else []
    return args + (["--admindir", _dpkg_admindir] if _dpkg_admindir else [])


def dpkg_divert(*args) -> str:
    # dpkg-divert takes paths relative to --instdir; map rooted paths back and forth.
    root = _dpkg_root
    args = [a[len(root):] if root and a.startswith(root + "/") else a for a in args]
    cmd = [DPKG_DIVERT, *dpkg_db_args(), *args]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                             env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    except (OSError, subprocess.SubprocessError) as e:
        fail(f"dpkg-divert failed: {e}")
    if out.returncode != 0:
        msg = (out.stderr or out.stdout).strip().splitlines()
        fail(f"dpkg-divert failed: {msg[-1] if msg else out.returncode}")
    res = out.stdout.strip()
    return root + res if root and res.startswith("/") else res


def divert_marker() -> str:
    # Written before our dpkg-divert --add, so restore finds the diversion even if the renamed file is gone.
    return os.path.join(STATE_DIR, "kali-login.divert")


def legacy_diverted() -> str:
    return KALI_LOGIN_BG + LEGACY_DIVERT_SUFFIX


def divert_registered() -> bool:
    truename = dpkg_divert("--truename", KALI_LOGIN_BG)
    # Old diversion, or the preinst migration was cut off between dropping it and adding ours.
    if truename == legacy_diverted() or (truename == KALI_LOGIN_BG and os.path.lexists(legacy_diverted())):
        fail(LEGACY_HINT)
    return truename == KALI_LOGIN_BG_DIVERTED


def divert_active() -> bool:
    """Our diversion is registered; dpkg-divert is asked only when we may have made one."""
    if not any(os.path.lexists(p) for p in (divert_marker(), KALI_LOGIN_BG_DIVERTED, legacy_diverted())):
        return False
    return divert_registered()


def divert_args(action: str) -> list:
    return ["--package", PKG, "--rename", "--divert", KALI_LOGIN_BG_DIVERTED, action, KALI_LOGIN_BG]


def check_root_dir(path: str):
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        fail(f"{path} is not a root-owned directory")


def kali_login_apply(data: bytes):
    check_root_dir(os.path.dirname(KALI_LOGIN_BG))
    ensure_state_dir()
    added = []

    def divert_if_needed():
        # Runs after the new image is written and fsynced, right before the rename onto the path.
        if divert_registered():
            return
        atomic_write(divert_marker(), b"", 0o644)
        try:
            dpkg_divert(*divert_args("--add"))
        except BaseException:
            os.unlink(divert_marker())
            raise
        added.append(True)

    try:
        atomic_write(KALI_LOGIN_BG, data, 0o644, before_replace=divert_if_needed)
    except BaseException as e:
        if added:
            try:
                dpkg_divert(*divert_args("--remove"))
                os.unlink(divert_marker())
            except Exception as rollback:
                raise HelperError(f"{e}; rollback of dpkg-divert also failed: {rollback}") from e
        raise


def kali_login_restore() -> bool:
    if not divert_active():
        if os.path.lexists(divert_marker()):
            os.unlink(divert_marker())
        return False
    # Move our image aside (dpkg-divert --rename needs the path free); put it back if --remove fails.
    aside = KALI_LOGIN_BG + RESTORE_SUFFIX
    if os.path.lexists(aside):
        if os.path.lexists(KALI_LOGIN_BG):
            # Stale (superseded by a later apply), or ours after a --remove that renamed stock back but failed.
            rescue_image(read_regular(aside, MAX_IMAGE_SIZE))
            os.unlink(aside)
        else:
            os.rename(aside, KALI_LOGIN_BG)  # undo an interrupted restore before retrying it
    # No diverted file: the path is already stock (or the only copy left), so only the record is removed.
    moved = os.path.lexists(KALI_LOGIN_BG) and os.path.lexists(KALI_LOGIN_BG_DIVERTED)
    if moved:
        os.rename(KALI_LOGIN_BG, aside)
    try:
        dpkg_divert(*divert_args("--remove"))
    except BaseException:
        if moved and not os.path.lexists(KALI_LOGIN_BG):
            os.rename(aside, KALI_LOGIN_BG)
        raise
    if moved:
        if os.path.lexists(KALI_LOGIN_BG):
            os.unlink(aside)  # stock is back at the path; our image goes as the restore asked
        else:
            os.rename(aside, KALI_LOGIN_BG)  # nothing was renamed back: never leave the path empty
    if os.path.lexists(divert_marker()):
        os.unlink(divert_marker())
    fsync_dir(os.path.dirname(KALI_LOGIN_BG))
    return True


def gdm_apply(image_path: str):
    data, ext = load_image(image_path)
    if not kali_override_active():
        gdm_gresource_apply(data, ext)
        return
    kali_login_apply(data)
    # A theme patch from an older version is overridden here anyway; put stock back if possible.
    try:
        gdm_gresource_restore()
    except Exception as e:  # the login change already succeeded; this cleanup is optional
        print(f"warning: could not revert the old theme patch: {e}", file=sys.stderr)


def gdm_restore() -> int:
    restored = kali_login_restore()
    rc = gdm_gresource_restore()
    return EXIT_OK if restored or rc == EXIT_OK else EXIT_NOTHING


# ---------- LightDM gtk greeter ----------

def patch_greeter_conf(text: str, image: str) -> str:
    """Set background and user-background in [greeter], keeping comments and other keys."""
    wanted = {"background": image, "user-background": "false"}
    lines = text.splitlines(keepends=True)
    out, section, done, greeter_seen = [], None, set(), False

    def flush_missing():
        for k, v in wanted.items():
            if k not in done:
                out.append(f"{k} = {v}\n")
                done.add(k)

    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if section == "greeter":
                flush_missing()
            section = s[1:-1].strip()
            greeter_seen = greeter_seen or section == "greeter"
            out.append(line if line.endswith("\n") else line + "\n")
            continue
        if section == "greeter" and s and not s.startswith(("#", ";")) and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in wanted:
                if key not in done:
                    out.append(f"{key} = {wanted[key]}\n")
                    done.add(key)
                continue
        out.append(line)
    if section == "greeter":
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        flush_missing()
    if not greeter_seen:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append("[greeter]\n")
        flush_missing()
    return "".join(out)


def ensure_image_dir():
    os.makedirs(os.path.dirname(LIGHTDM_IMAGE_DIR), mode=0o755, exist_ok=True)
    try:
        os.mkdir(LIGHTDM_IMAGE_DIR, 0o755)
    except FileExistsError:
        pass
    st = os.lstat(LIGHTDM_IMAGE_DIR)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        fail(f"{LIGHTDM_IMAGE_DIR} is not a root-owned directory")


def lightdm_apply(image_path: str):
    data, ext = load_image(image_path)
    raw = read_regular(LIGHTDM_CONF, 1024 * 1024)
    mode = stat.S_IMODE(os.stat(LIGHTDM_CONF).st_mode)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail(f"{LIGHTDM_CONF} is not UTF-8")
    ensure_state_dir()
    if not os.path.lexists(LIGHTDM_ORIG):
        atomic_write(LIGHTDM_ORIG, raw, 0o644)

    ensure_image_dir()
    target = os.path.join(LIGHTDM_IMAGE_DIR, "login." + ext)
    staged = os.path.join(LIGHTDM_IMAGE_DIR, ".login-staged." + ext)
    conf = patch_greeter_conf(text, target).encode("utf-8")
    atomic_write(staged, data, 0o644)
    try:
        if os.path.lexists(target):
            # Image name in use: switch conf first so a failed write keeps the old image.
            atomic_write(LIGHTDM_CONF, conf, mode)
            os.replace(staged, target)
        else:
            os.replace(staged, target)
            try:
                atomic_write(LIGHTDM_CONF, conf, mode)
            except BaseException:
                os.unlink(target)
                raise
    finally:
        if os.path.lexists(staged):
            os.unlink(staged)
    for name in os.listdir(LIGHTDM_IMAGE_DIR):
        p = os.path.join(LIGHTDM_IMAGE_DIR, name)
        if p != target and name.startswith("login."):
            os.unlink(p)


def lightdm_restore() -> int:
    if not os.path.lexists(LIGHTDM_ORIG):
        return EXIT_NOTHING
    ensure_state_dir()
    raw = read_regular(LIGHTDM_ORIG, 1024 * 1024)
    mode = stat.S_IMODE(os.stat(LIGHTDM_CONF).st_mode) if os.path.exists(LIGHTDM_CONF) else 0o644
    atomic_write(LIGHTDM_CONF, raw, mode)
    if os.path.isdir(LIGHTDM_IMAGE_DIR) and not os.path.islink(LIGHTDM_IMAGE_DIR):
        shutil.rmtree(LIGHTDM_IMAGE_DIR)
    os.unlink(LIGHTDM_ORIG)
    return EXIT_OK


# ---------- package maintainer scripts (pkg-*) ----------

def image_ext(data: bytes) -> str | None:
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(JPEG_MAGIC):
        return "jpg"
    return None


def rescue_image(data: bytes) -> str:
    """Keep a copy in RESCUE_DIR named by content hash; an existing name is never overwritten."""
    os.makedirs(os.path.dirname(RESCUE_DIR), mode=0o755, exist_ok=True)
    try:
        os.mkdir(RESCUE_DIR, 0o755)
    except FileExistsError:
        pass
    check_root_dir(RESCUE_DIR)
    stem, ext = f"login-{hashlib.sha256(data).hexdigest()[:12]}", image_ext(data) or "img"
    tmp, n = None, 0
    try:
        while n < 100:
            final = os.path.join(RESCUE_DIR, f"{stem}-{n}.{ext}" if n else f"{stem}.{ext}")
            if os.path.lexists(final):
                if holds_bytes(final, data):
                    return final
                n += 1  # name taken by other bytes: try the next one
                continue
            if tmp is None:
                fd, tmp = tempfile.mkstemp(prefix=TMP_PREFIX, suffix=".tmp", dir=RESCUE_DIR)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fchmod(f.fileno(), 0o644)
                    os.fsync(f.fileno())
            try:
                os.link(tmp, final)  # unlike rename, fails instead of replacing
            except FileExistsError:
                continue
            break
        else:
            fail(f"cannot save login image to {RESCUE_DIR}: no free name")
    finally:
        if tmp is not None:
            os.unlink(tmp)
    fsync_dir(RESCUE_DIR)
    return final


def holds_bytes(path: str, data: bytes) -> bool:
    try:
        return read_regular(path, len(data)) == data
    except (OSError, HelperError):
        return False


def saved_images() -> list:
    try:
        names = sorted(os.listdir(RESCUE_DIR))
    except OSError:
        return []
    paths = [os.path.join(RESCUE_DIR, n) for n in names if not n.startswith(".")]
    return [p for p in paths if stat.S_ISREG(os.lstat(p).st_mode)]


def divert_preflight():
    """Fail on a legacy or foreign-owned diversion of the Kali login image; no changes."""
    if not any(os.path.lexists(p) for p in (divert_marker(), KALI_LOGIN_BG_DIVERTED, legacy_diverted())):
        return
    if divert_registered() and dpkg_divert("--listpackage", KALI_LOGIN_BG) != PKG:
        fail(f"login image diversion is not owned by {PKG}")


def gresource_backup_check():
    meta = read_meta()
    if not meta or meta.get("version") != shell_version() or not os.path.isfile(GRESOURCE_ORIG):
        fail(f"no matching stock theme backup; {REINSTALL_HINT}, then remove {PKG} again")
    raw = read_regular(GRESOURCE_ORIG, 256 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != meta.get("sha256"):
        fail(f"theme backup checksum mismatch; {REINSTALL_HINT}, then remove {PKG} again")


def pkg_remove() -> int:
    """prerm remove: check, save the active login images, then restore the stock login screen."""
    divert_preflight()
    entries = load_entries(GRESOURCE) if os.path.isfile(GRESOURCE) else {}
    if is_patched(entries):
        gresource_backup_check()
    if os.path.lexists(LIGHTDM_ORIG):
        read_regular(LIGHTDM_ORIG, 1024 * 1024)

    images = []
    if divert_active() and not os.path.islink(KALI_LOGIN_BG) and os.path.isfile(KALI_LOGIN_BG):
        images.append(read_regular(KALI_LOGIN_BG, MAX_IMAGE_SIZE))
    aside = KALI_LOGIN_BG + RESTORE_SUFFIX  # left by an interrupted restore --login
    if not os.path.islink(aside) and os.path.isfile(aside):
        images.append(read_regular(aside, MAX_IMAGE_SIZE))
    if os.path.isdir(LIGHTDM_IMAGE_DIR) and not os.path.islink(LIGHTDM_IMAGE_DIR):
        for name in sorted(os.listdir(LIGHTDM_IMAGE_DIR)):
            p = os.path.join(LIGHTDM_IMAGE_DIR, name)
            if name.startswith("login.") and stat.S_ISREG(os.lstat(p).st_mode):
                images.append(read_regular(p, MAX_IMAGE_SIZE))
    images += [data for p, data in sorted(entries.items()) if p.startswith((LOGIN_ENTRY, LEGACY_LOGIN_ENTRY))]
    for data in images:
        rescue_image(data)

    kali_login_restore()
    if os.path.isfile(GRESOURCE):
        gdm_gresource_restore()
    lightdm_restore()
    for p in saved_images():
        print(f"saved login image: {p}")
    return EXIT_OK


def pkg_configure() -> int:
    """postinst configure: re-embed a login image patched under the old entry name."""
    if not os.path.isfile(GRESOURCE):
        return EXIT_OK
    entries = load_entries(GRESOURCE)
    legacy = sorted(p for p in entries if p.startswith(LEGACY_LOGIN_ENTRY))
    if not legacy:
        return EXIT_OK
    data, ext = entries[legacy[0]], legacy[0][len(LEGACY_LOGIN_ENTRY):]
    if ext not in ("png", "jpg") or image_ext(data) != ext:
        fail(f"unexpected old theme entry {legacy[0]}")
    gdm_gresource_apply(data, ext)
    return EXIT_OK


def maintscript_problem() -> str | None:
    """pkg-* run only from this package's maintainer scripts, never through pkexec."""
    env = os.environ
    if ("PKEXEC_UID" in env or env.get("DPKG_MAINTSCRIPT_PACKAGE") != PKG
            or env.get("DPKG_MAINTSCRIPT_NAME") not in MAINTSCRIPT_NAMES):
        return f"pkg-* actions are only for the {PKG} maintainer scripts"
    for name in ("DPKG_ROOT", "DPKG_ADMINDIR"):
        value = env.get(name, "")
        if value and (not os.path.isabs(value) or "\n" in value or "\x00" in value or not os.path.isdir(value)):
            return f"{name} must be an absolute existing directory"
    if env.get("DPKG_ROOT") and not env.get("DPKG_ADMINDIR"):
        return "DPKG_ROOT is set without DPKG_ADMINDIR"
    return None


def use_dpkg_root():
    """Take DPKG_ROOT/DPKG_ADMINDIR (validated) and rebase every system path onto the root."""
    global _dpkg_root, _dpkg_admindir
    root = os.path.realpath(os.environ.get("DPKG_ROOT") or "/")
    _dpkg_root = "" if root == "/" else root
    _dpkg_admindir = os.environ.get("DPKG_ADMINDIR") or None
    for name in ROOTED_PATHS if _dpkg_root else ():
        globals()[name] = _dpkg_root + globals()[name]


@contextlib.contextmanager
def dpkg_lock():
    """Hold dpkg's frontend and database locks (non-blocking, like apt) for a runtime action."""
    fds = []
    try:
        for path in DPKG_LOCKS:
            fds.append(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640))
            try:
                fcntl.lockf(fds[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fail(DPKG_BUSY)
        yield
    finally:
        for fd in reversed(fds):
            os.close(fd)


def dpkg_busy() -> bool:
    """Probe dpkg's and apt's locks and release them at once; apt-get takes them itself."""
    for path in DPKG_LOCKS + (APT_ARCHIVES_LOCK,):
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            continue
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        finally:
            os.close(fd)
    return False


# ---------- junk cleaner (clean); the read-only parts also serve the unprivileged preview ----------

class Busy(HelperError):
    pass


def _run_gentle(cmd: list, timeout: int, env: dict) -> subprocess.CompletedProcess:
    """subprocess.run, but on timeout SIGTERM then wait (no SIGKILL: dpkg may be mid-transaction) and re-raise."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            stdin=subprocess.DEVNULL, env=env)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.communicate()  # until apt-get and any dpkg child holding the pipes are done
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run_tool(cmd: list, timeout: int = CHILD_TIMEOUT, env: dict = None, gentle: bool = False) -> str:
    """Run a fixed system command; stdout, or Busy/HelperError with its last stderr line."""
    try:
        if gentle:
            proc = _run_gentle(cmd, timeout, dict(CLEAN_ENV, **(env or {})))
        else:
            proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                  env=dict(CLEAN_ENV, **(env or {})), timeout=timeout)
    except subprocess.TimeoutExpired:
        fail(INTERRUPTED if gentle else f"{os.path.basename(cmd[0])} timed out")
    except OSError as e:
        fail(f"cannot run {cmd[0]}: {e.strerror}")
    if any(m in proc.stderr for m in APT_LOCKED):
        raise Busy(PM_BUSY)
    if proc.returncode != 0:
        lines = [ln.strip() for ln in proc.stderr.splitlines() if ln.strip()]
        fail(lines[-1] if lines else f"{os.path.basename(cmd[0])} exited with {proc.returncode}")
    return proc.stdout


def disk_usage(paths, seen: set = None) -> tuple:
    """(bytes, files, complete) by allocated blocks; hardlinks once, symlinks never followed."""
    seen = set() if seen is None else seen
    size = count = 0
    complete = True

    def add(st):
        nonlocal size, count
        count += 1
        if (st.st_dev, st.st_ino) not in seen:
            seen.add((st.st_dev, st.st_ino))
            size += st.st_blocks * 512

    def walk_error(_):
        nonlocal complete
        complete = False

    for path in paths:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
            continue
        if not stat.S_ISDIR(st.st_mode):
            add(st)
            continue
        for top, dirs, files in os.walk(path, onerror=walk_error):
            for name in files + [d for d in dirs if os.path.islink(os.path.join(top, d))]:
                try:
                    add(os.lstat(os.path.join(top, name)))
                except OSError:
                    complete = False
    return size, count, complete


def apt_cache_files() -> tuple:
    """(paths, complete) of what `apt-get clean` deletes; partial/ is unreadable without root."""
    archives = os.path.join(APT_CACHE_DIR, "archives")
    paths = glob.glob(os.path.join(archives, "*.deb")) + glob.glob(os.path.join(APT_CACHE_DIR, "*.bin"))
    try:
        paths += [os.path.join(archives, "partial", n) for n in os.listdir(os.path.join(archives, "partial"))]
    except FileNotFoundError:
        pass
    except OSError:
        return paths, False
    return paths, True


def journal_archives(max_age_days: int = None) -> list:
    """Archived journal files (what --vacuum-time may delete), optionally only older than max_age_days."""
    cutoff = None if max_age_days is None else time.time() - max_age_days * 86400
    found = []
    for d in sorted(os.listdir(JOURNAL_DIR)) if os.path.isdir(JOURNAL_DIR) else ():
        sub = os.path.join(JOURNAL_DIR, d)
        if os.path.islink(sub) or not os.path.isdir(sub):
            continue
        for name in os.listdir(sub):
            if "@" in name and name.endswith((".journal", ".journal~")):
                p = os.path.join(sub, name)
                st = os.lstat(p)
                if stat.S_ISREG(st.st_mode) and (cutoff is None or st.st_mtime < cutoff):
                    found.append(p)
    return found


def lightdm_image() -> str | None:
    """Image path set in the greeter conf; raises when the conf exists but cannot be read."""
    import configparser
    if not os.path.lexists(LIGHTDM_CONF):
        return None
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    with open(LIGHTDM_CONF, encoding="utf-8") as f:
        cp.read_file(f)
    return cp.get("greeter", "background", fallback="").strip() or None


def tool_leftovers() -> list:
    """System part of tool-images: fixed allowlist of leftovers; never state, backups, lock or active images.

    The restore-aside login image is left out: it may be the only copy (see kali_login_restore/pkg_remove).
    """
    found = []
    for d in (os.path.dirname(GRESOURCE), os.path.dirname(KALI_LOGIN_BG), os.path.dirname(LIGHTDM_CONF),
              LIGHTDM_IMAGE_DIR):
        found += glob.glob(os.path.join(glob.escape(d), glob.escape(TMP_PREFIX) + "*.tmp"))
    if os.path.isdir(LIGHTDM_IMAGE_DIR) and not os.path.islink(LIGHTDM_IMAGE_DIR):
        found += glob.glob(os.path.join(glob.escape(LIGHTDM_IMAGE_DIR), ".login-staged.*"))
        import configparser
        try:
            active = lightdm_image()
        except (OSError, UnicodeDecodeError, ValueError, configparser.Error):
            active = ""  # unknown: keep every login.* image
        if active != "":
            active = os.path.realpath(active) if active else None
            found += [p for p in glob.glob(os.path.join(glob.escape(LIGHTDM_IMAGE_DIR), "login.*"))
                      if os.path.realpath(p) != active]
    found = [p for p in found if os.path.lexists(p) and stat.S_ISREG(os.lstat(p).st_mode)]
    # mkdtemp build dirs of gdm_gresource_apply
    found += [p for p in glob.glob(os.path.join(glob.escape(STATE_DIR), "build-*"))
              if not os.path.islink(p) and os.path.isdir(p)]
    return sorted(found)


def autoremove_plan() -> tuple:
    """(packages, token) apt would autoremove, minus kernels and this package; no root needed."""
    out = run_tool([APT_GET, "-s", "autoremove"])
    running = os.uname().release
    pkgs = sorted({n for n in REMV.findall(out)
                   if not KERNEL_PKG.match(n) and running not in n and n.split(":")[0] != PKG})
    for n in pkgs:
        if not PKG_NAME.match(n):
            fail(f"unexpected package name from apt-get: {n[:60]!r}")
    return pkgs, hashlib.sha256("\n".join(pkgs).encode()).hexdigest()


def package_sizes(pkgs: list) -> int:
    """Installed-Size sum in bytes."""
    if not pkgs:
        return 0
    out = run_tool([DPKG_QUERY, "-W", "-f=${Installed-Size}\n", "--", *pkgs])
    return sum(int(v) for v in out.split() if v.isdigit()) * 1024


def clean_args(argv) -> tuple:
    """(ids, expect) from `clean` argv; anything else is a usage error."""
    ids, expect, rest = [], None, list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--expect-autoremove" and rest and expect is None and HEX64.match(rest[0]):
            expect = rest.pop(0)
        elif arg in CLEAN_IDS and arg not in ids:
            ids.append(arg)
        else:
            fail(CLEAN_USAGE)
    if not ids:
        fail(CLEAN_USAGE)
    return ids, expect


def clean_result(status: str = "cleaned", freed: int = 0, removed: int = 0, reason: str = "") -> dict:
    return {"status": status, "freed": freed, "removed": removed,
            "skipped": {"in-use": 0, "other-fs": 0}, "reason": reason}


def file_sizes(paths) -> dict:
    sizes = {}
    for p in paths:
        try:
            sizes[p] = os.lstat(p).st_blocks * 512
        except OSError:
            pass
    return sizes


def removed_since(before: dict) -> dict:
    gone = [p for p in before if not os.path.lexists(p)]
    return clean_result(freed=sum(before[p] for p in gone), removed=len(gone))


def clean_apt_cache(_expect) -> dict:
    if dpkg_busy():
        return clean_result("skipped", reason=PM_BUSY)
    before = file_sizes(apt_cache_files()[0])
    run_tool([APT_GET, "clean"])
    return removed_since(before)


def clean_journal(_expect) -> dict:
    if not os.path.exists(JOURNALCTL):
        return clean_result("skipped", reason="journalctl not available")
    before = file_sizes(journal_archives())
    run_tool([JOURNALCTL, f"--vacuum-time={JOURNAL_RETENTION}"])
    return removed_since(before)


def clean_tool_images(_expect) -> dict:
    freed = removed = 0
    for p in tool_leftovers():
        size, count, _ = disk_usage([p])
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)
        freed, removed = freed + size, removed + count
    return clean_result(freed=freed, removed=removed)


def clean_autoremove(expect) -> dict:
    if dpkg_busy():
        return clean_result("skipped", reason=PM_BUSY)
    pkgs, token = autoremove_plan()
    if token != expect:
        return clean_result("skipped", reason=PLAN_CHANGED)
    if not pkgs:
        return clean_result()
    sim = run_tool([APT_GET, "-s", "remove", "--", *pkgs])
    if set(REMV.findall(sim)) != set(pkgs) or any(k == "Inst" or n not in pkgs for k, n in SIM_OTHER.findall(sim)):
        return clean_result("skipped", reason="removal would change other packages; " + PLAN_CHANGED)
    fs = os.statvfs("/")
    free_before = fs.f_bfree * fs.f_frsize
    run_tool([APT_GET, "-y", "remove", "--", *pkgs], AUTOREMOVE_TIMEOUT, {"DEBIAN_FRONTEND": "noninteractive"},
             gentle=True)
    fs = os.statvfs("/")
    return clean_result(freed=max(0, fs.f_bfree * fs.f_frsize - free_before), removed=len(pkgs))


CLEANERS = {"apt-cache": clean_apt_cache, "journal": clean_journal,
            "tool-images": clean_tool_images, "autoremove": clean_autoremove}


def clean(*argv) -> int:
    """Each category independent; one JSON line on stdout, exit 0 even when some failed."""
    ids, expect = clean_args(argv)
    results = {}
    for cid in ids:
        try:
            results[cid] = CLEANERS[cid](expect)
        except Busy:
            results[cid] = clean_result("skipped", reason=PM_BUSY)
        except Exception as e:
            msg = str(e) if isinstance(e, HelperError) else f"{type(e).__name__}: {e}"
            results[cid] = clean_result("failed", reason=msg.replace("\n", " "))
    print(json.dumps({"v": 1, "results": results}))
    return EXIT_OK


# ---------- entry point ----------

# Arity None: the action validates its own argv.
ACTIONS = {
    "gdm-apply": (1, gdm_apply),
    "gdm-restore": (0, gdm_restore),
    "lightdm-apply": (1, lightdm_apply),
    "lightdm-restore": (0, lightdm_restore),
    "clean": (None, clean),
    "pkg-configure": (0, pkg_configure),
    "pkg-remove": (0, pkg_remove),
}
# dpkg-divert takes no dpkg lock (spike D0): gdm-* also hold dpkg's so apt cannot run meanwhile.
# clean only probes dpkg (dpkg_busy), since apt-get must take those locks itself.
ACTION_LOCKS = {
    "gdm-apply": (action_lock, dpkg_lock),
    "gdm-restore": (action_lock, dpkg_lock),
    "clean": (action_lock,),
}


def main(argv) -> int:
    if argv[:1] == ["--dump"] and len(argv) == 2:
        func = dump_main  # internal: child process for Gio loads
    elif argv[:1] and argv[0] in ACTIONS and ACTIONS[argv[0]][0] in (None, len(argv) - 1):
        func = ACTIONS[argv[0]][1]
    else:
        print("usage: root-helper gdm-apply IMAGE | gdm-restore | lightdm-apply IMAGE | lightdm-restore"
              " | clean ID... [--expect-autoremove HEX64] | pkg-configure | pkg-remove", file=sys.stderr)
        return EXIT_FAIL
    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return EXIT_FAIL
    problem = maintscript_problem()
    if argv[0] in PKG_ACTIONS and problem:
        print(problem, file=sys.stderr)
        return EXIT_FAIL
    if problem is None and (argv[0] in PKG_ACTIONS or func is dump_main):
        use_dpkg_root()  # a --dump child of a pkg-* run inherits its env
    os.umask(0o022)
    try:
        if func is dump_main:
            rc = func(*argv[1:])  # runs under the parent's lock
        else:
            if func is clean:
                clean_args(argv[1:])  # usage errors before any lock or state dir
            with contextlib.ExitStack() as stack:
                for lock in ACTION_LOCKS.get(argv[0], (action_lock,)):
                    stack.enter_context(lock())
                rc = func(*argv[1:])
    except Exception as e:
        msg = (str(e) if isinstance(e, HelperError) else f"{type(e).__name__}: {e}").replace("\n", " ")
        if argv[0] in WARN_ONLY_ACTIONS:
            print(f"warning: {msg}", file=sys.stderr)
            return EXIT_OK
        print(msg, file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK if rc is None else rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
