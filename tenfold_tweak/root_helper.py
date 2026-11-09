"""Root-side helper for login-screen targets.

Run only as: pkexec /usr/bin/python3 -I <this file> ACTION [IMAGE]
Actions: gdm-apply IMAGE | gdm-restore | lightdm-apply IMAGE | lightdm-restore
Exit codes: 0 ok, 3 nothing to restore, 1 failure (one-line reason on stderr).
Standalone on purpose: stdlib + gi only, imports nothing from the package.
"""

import contextlib
import fcntl
import glob
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

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

STATE_DIR = "/var/lib/tenfold-tweak"
# Fixed root-owned install path; the --dump child never re-reads a user-writable copy.
INSTALLED_HELPER = "/usr/local/lib/tenfold-tweak/tenfold_tweak/root_helper.py"

GRESOURCE = "/usr/share/gnome-shell/gnome-shell-theme.gresource"
GRESOURCE_NEW = "/usr/share/gnome-shell/.gnome-shell-theme.gresource.new"
GRESOURCE_ORIG = STATE_DIR + "/gnome-shell-theme.gresource.orig"
GRESOURCE_META = STATE_DIR + "/gnome-shell-theme.meta"
THEME_PREFIX = "/org/gnome/shell/theme/"
LOGIN_ENTRY = THEME_PREFIX + "tenfold-tweak-login."
PATCHED_CSS = ("gnome-shell-dark.css", "gnome-shell-light.css", "gnome-shell-high-contrast.css")
CSS_BLOCK = (
    "\n/* tenfold-tweak */\n"
    '#lockDialogGroup {{ background: #000 url("resource://{entry}"); '
    "background-size: cover; background-repeat: no-repeat; background-position: center; }}\n"
)
REINSTALL_HINT = "run: sudo apt install --reinstall gnome-shell-common"

# Kali patches gnome-shell (ui/screenShield.js _ensureUnlockDialog) to set an inline style on
# #lockDialogGroup with this hardcoded image; inline style beats the theme CSS, so on such a
# shell the login background is changed by diverting this path instead of patching the gresource.
SHELL_LIBS = "/usr/lib/gnome-shell/libshell-*.so"
KALI_LOGIN_BG = "/usr/share/desktop-base/kali-theme/login/background-blurred"
KALI_LOGIN_BG_DIVERTED = KALI_LOGIN_BG + ".tenfold-tweak"
DPKG_DIVERT = "/usr/bin/dpkg-divert"

LIGHTDM_CONF = "/etc/lightdm/lightdm-gtk-greeter.conf"
LIGHTDM_ORIG = STATE_DIR + "/lightdm-gtk-greeter.conf.orig"
LIGHTDM_IMAGE_DIR = "/usr/local/share/backgrounds/tenfold-tweak"


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
    prefix = os.path.basename(tmp) if tmp else "." + os.path.basename(dest)
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
            fail("another tenfold-tweak run is in progress")
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
            ["/usr/bin/dpkg-query", "-W", "-f=${Version}", "gnome-shell-common"],
            capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.CalledProcessError):
        fail("cannot read gnome-shell-common version")
    return out.stdout.strip()


def is_patched(entries: dict) -> bool:
    return any(p.startswith(LOGIN_ENTRY) for p in entries)


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
    needle = b'url("' + KALI_LOGIN_BG.encode() + b'")'
    for lib in sorted(glob.glob(SHELL_LIBS)):
        try:
            with open(lib, "rb") as f:
                if needle in f.read():
                    return True
        except OSError:
            continue
    return False


def dpkg_divert(*args) -> str:
    cmd = [DPKG_DIVERT, *args]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                             env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    except (OSError, subprocess.SubprocessError) as e:
        fail(f"dpkg-divert failed: {e}")
    if out.returncode != 0:
        msg = (out.stderr or out.stdout).strip().splitlines()
        fail(f"dpkg-divert failed: {msg[-1] if msg else out.returncode}")
    return out.stdout.strip()


def divert_marker() -> str:
    # Written before our dpkg-divert --add, so restore finds the diversion even if the renamed file is gone.
    return os.path.join(STATE_DIR, "kali-login.divert")


def divert_registered() -> bool:
    return dpkg_divert("--truename", KALI_LOGIN_BG) == KALI_LOGIN_BG_DIVERTED


def divert_active() -> bool:
    """Our diversion is registered; dpkg-divert is asked only when we may have made one."""
    if not (os.path.lexists(divert_marker()) or os.path.lexists(KALI_LOGIN_BG_DIVERTED)):
        return False
    return divert_registered()


def divert_args(action: str) -> list:
    return ["--local", "--rename", "--divert", KALI_LOGIN_BG_DIVERTED, action, KALI_LOGIN_BG]


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
    aside = KALI_LOGIN_BG + ".tenfold-tweak-restore"
    if os.path.lexists(aside):
        os.unlink(aside)
    moved = os.path.lexists(KALI_LOGIN_BG)
    if moved:
        os.rename(KALI_LOGIN_BG, aside)
    try:
        dpkg_divert(*divert_args("--remove"))
    except BaseException:
        if moved and not os.path.lexists(KALI_LOGIN_BG):
            os.rename(aside, KALI_LOGIN_BG)
        raise
    if moved:
        os.unlink(aside)
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


# ---------- entry point ----------

ACTIONS = {
    "gdm-apply": (1, gdm_apply),
    "gdm-restore": (0, gdm_restore),
    "lightdm-apply": (1, lightdm_apply),
    "lightdm-restore": (0, lightdm_restore),
}


def main(argv) -> int:
    if argv[:1] == ["--dump"] and len(argv) == 2:
        func = dump_main  # internal: child process for Gio loads
    elif argv[:1] and argv[0] in ACTIONS and len(argv) - 1 == ACTIONS[argv[0]][0]:
        func = ACTIONS[argv[0]][1]
    else:
        print("usage: root_helper.py gdm-apply IMAGE | gdm-restore | lightdm-apply IMAGE | lightdm-restore",
              file=sys.stderr)
        return EXIT_FAIL
    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return EXIT_FAIL
    os.umask(0o022)
    try:
        if func is dump_main:
            rc = func(*argv[1:])  # runs under the parent's lock
        else:
            with action_lock():
                rc = func(*argv[1:])
    except HelperError as e:
        print(str(e).replace("\n", " "), file=sys.stderr)
        return EXIT_FAIL
    except Exception as e:
        print(f"{type(e).__name__}: {e}".replace("\n", " "), file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK if rc is None else rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
