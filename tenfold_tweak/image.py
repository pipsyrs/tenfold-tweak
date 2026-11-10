"""Image validation and the stable per-user copy used by all targets."""

import hashlib
import os
import stat
import tempfile

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

MAX_SIZE = 64 * 1024 * 1024
MAX_DIM = 16384
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
COPY_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
    "backgrounds", "tenfold-tweaks")
# Copies made under the tool's old name; moved by migrate.migrate_user(), else cleaned up once unreferenced.
LEGACY_COPY_DIR = os.path.join(os.path.dirname(COPY_DIR), "tenfold-tweak")
COPY_PREFIX = "wallpaper-"
LOGIN_PREFIX = "login-"
# Login images are decoded in full by the greeter; keep in sync with root_helper.MAX_LOGIN_DIM.
LOGIN_MAX_DIM = 4096


class ImageError(Exception):
    pass


def _read_source(path: str) -> bytes:
    real = os.path.realpath(path)
    if not os.path.exists(real):
        raise ImageError(f"file not found: {path}")
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        raise ImageError(f"cannot read {path}: {e.strerror}") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ImageError(f"not a regular file: {path}")
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not 0 < st.st_size <= MAX_SIZE:
            raise ImageError("image size must be between 1 byte and 64 MiB")
        try:
            data = f.read(MAX_SIZE + 1)
        except OSError as e:
            raise ImageError(f"cannot read {path}: {e.strerror}") from None
    if not 0 < len(data) <= MAX_SIZE:
        raise ImageError("image size must be between 1 byte and 64 MiB")
    return data


def _ext_for(data: bytes) -> str:
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(JPEG_MAGIC):
        return "jpg"
    raise ImageError("unsupported format; supported: PNG, JPEG")


def _short(err: GLib.Error) -> str:
    # Loader errors can carry long source-location prefixes; keep the last part.
    return err.message.rsplit(": ", 1)[-1].strip() or "cannot decode"


def _check_decodes(path: str, ext: str):
    try:
        fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
    except GLib.Error as e:
        raise ImageError(f"corrupt image: {_short(e)}") from None
    expected = "png" if ext == "png" else "jpeg"
    if fmt is None:
        # Magic bytes already matched, so an unrecognised file is damaged.
        raise ImageError("corrupt image: cannot read image header")
    if fmt.get_name() != expected:
        raise ImageError("unsupported format; supported: PNG, JPEG")
    if not (1 <= width <= MAX_DIM and 1 <= height <= MAX_DIM):
        raise ImageError(f"image dimensions must be between 1 and {MAX_DIM} pixels")
    try:
        # Bounded-memory full decode catches truncated/corrupt data.
        GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 256, 256, True)
    except GLib.Error as e:
        raise ImageError(f"corrupt image: {_short(e)}") from None


def prepare(path: str) -> str:
    """Validate the image and return the path of its stable user copy."""
    data = _read_source(path)
    ext = _ext_for(data)
    os.makedirs(COPY_DIR, mode=0o700, exist_ok=True)
    os.chmod(COPY_DIR, 0o700)
    final = os.path.join(COPY_DIR, f"{COPY_PREFIX}{hashlib.sha256(data).hexdigest()[:12]}.{ext}")
    fd, tmp = tempfile.mkstemp(prefix=".incoming-", suffix="." + ext, dir=COPY_DIR)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), 0o600)
            os.fsync(f.fileno())
        _check_decodes(tmp, ext)
        os.replace(tmp, final)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return final


def login_copy(path: str) -> str:
    """Return path, or a copy downscaled to fit LOGIN_MAX_DIM, for the login target."""
    try:
        _, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
        if width <= LOGIN_MAX_DIM and height <= LOGIN_MAX_DIM:
            return path
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, LOGIN_MAX_DIM, LOGIN_MAX_DIM, True)
    except GLib.Error as e:
        raise ImageError(f"cannot downscale image for the login screen: {_short(e)}") from None
    ext = path.rsplit(".", 1)[-1]
    final = os.path.join(COPY_DIR, LOGIN_PREFIX + os.path.basename(path)[len(COPY_PREFIX):])
    fd, tmp = tempfile.mkstemp(prefix=".incoming-", suffix="." + ext, dir=COPY_DIR)
    os.close(fd)
    try:
        if ext == "png":
            pixbuf.savev(tmp, "png", [], [])
        else:
            pixbuf.savev(tmp, "jpeg", ["quality"], ["92"])
        os.chmod(tmp, 0o600)
        os.replace(tmp, final)
    except GLib.Error as e:
        raise ImageError(f"cannot downscale image for the login screen: {_short(e)}") from None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return final


def _scope(directory: str) -> tuple:
    """(data dir, ("backgrounds", name)) of a copy dir, for cleaner._open_scope."""
    parent = os.path.dirname(directory)
    return os.path.dirname(parent), (os.path.basename(parent), os.path.basename(directory))


def tool_copies():
    """Yield (dir_fd, name, path, lstat) of prefixed regular files owned by the user in the copy dirs.

    Every dir is opened via dir_fd without following symlinks and must be the user's (cleaner._open_scope);
    all are validated before the first yield, else cleaner._NotDir is raised."""
    from . import cleaner
    opened = []
    try:
        for directory in (COPY_DIR, LEGACY_COPY_DIR):
            fd = cleaner._open_scope(*_scope(directory))
            if fd is not None:
                opened.append((directory, fd))
        for directory, fd in opened:
            for name in os.listdir(fd):
                if not name.startswith((COPY_PREFIX, LOGIN_PREFIX, ".incoming-")):
                    continue
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                    yield fd, name, os.path.join(directory, name), st
    finally:
        for _directory, fd in opened:
            os.close(fd)


def remove_legacy_dir():
    """rmdir the legacy copy dir via its parent's dir_fd; only succeeds once empty."""
    from . import cleaner
    base, parts = _scope(LEGACY_COPY_DIR)
    try:
        fd = cleaner._open_scope(base, parts[:1])
    except cleaner._NotDir:
        return
    if fd is None:
        return
    try:
        os.rmdir(parts[1], dir_fd=fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def unreferenced_copies(keep: set) -> list:
    """Tool-owned copies not in keep (paths still referenced by settings); read-only."""
    keep = {os.path.realpath(p) for p in keep if p}
    return [path for _fd, _name, path, _st in tool_copies() if os.path.realpath(path) not in keep]


def cleanup_copies(keep: set):
    """Delete tool-owned copies not in keep (paths still referenced by settings)."""
    from . import cleaner
    keep = {os.path.realpath(p) for p in keep if p}
    try:
        for fd, name, path, _st in tool_copies():
            if os.path.realpath(path) not in keep:
                try:
                    os.unlink(name, dir_fd=fd)
                except OSError:
                    pass
    except cleaner._NotDir:
        return  # a copy dir is a symlink or not the user's: touch nothing
    remove_legacy_dir()
