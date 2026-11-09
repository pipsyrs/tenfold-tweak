"""Per-user desktop wallpaper for GNOME (Gio.Settings) and XFCE (xfconf); no root."""

import json
import os
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "tenfold-tweak")
BACKUP_FILE = os.path.join(STATE_DIR, "backup.json")
LEGACY_STATE_DIR = os.path.join(os.path.dirname(STATE_DIR), "kali-bg-changer")

GNOME_SCHEMA = "org.gnome.desktop.background"
GNOME_KEYS = ("picture-uri", "picture-uri-dark", "picture-options")

# GNOME >= 3.36 blurs the desktop wallpaper on the lock screen unless one of these
# GNOME Shell extensions replaces UnlockDialog._createBackground with its own setting.
LOCK_EXTENSIONS = {
    "unlockDialogBackground@sun.wxg@gmail.com": "org.gnome.shell.extensions.unlockDialogBackground",
}
SHELL_SCHEMA = "org.gnome.shell"
EXT_ACTIVE = 1  # org.gnome.Shell.Extensions state ACTIVE (ENABLED before GNOME 45)

XFCE_CHANNEL = "xfce4-desktop"
MONITORS_ARG = "--list-monitors"  # child-process entry for _xfce_monitors
XFCE_PROP_RE = re.compile(r"^/backdrop/screen0/monitor[^/]+/workspace\d+/last-image$")


class DesktopError(Exception):
    pass


# ---------- backup file ----------

def _migrate_legacy_backup():
    """Move the backup written under the tool's old name, once."""
    legacy = os.path.join(LEGACY_STATE_DIR, "backup.json")
    if os.path.exists(BACKUP_FILE) or not os.path.isfile(legacy) or os.path.islink(legacy):
        return
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    try:
        os.replace(legacy, BACKUP_FILE)
        os.rmdir(LEGACY_STATE_DIR)
    except OSError:
        pass


def load_backup() -> dict:
    _migrate_legacy_backup()
    try:
        with open(BACKUP_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise DesktopError(f"cannot read {BACKUP_FILE}: {e}") from None
    return data if isinstance(data, dict) else {}


def save_backup(data: dict):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    if not data:
        if os.path.exists(BACKUP_FILE):
            os.unlink(BACKUP_FILE)
        return
    fd, tmp = tempfile.mkstemp(prefix=".backup-", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, BACKUP_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------- GNOME ----------

def _gnome_settings() -> Gio.Settings:
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(GNOME_SCHEMA, True) is None:
        raise DesktopError(f"GSettings schema {GNOME_SCHEMA} not installed")
    return Gio.Settings.new(GNOME_SCHEMA)


def _gnome_read(s: Gio.Settings) -> dict:
    return {k: s.get_string(k) for k in GNOME_KEYS}


def _gnome_state(s: Gio.Settings) -> dict:
    """User-set values; None where the key is at its schema default."""
    return {k: None if s.get_user_value(k) is None else s.get_string(k) for k in GNOME_KEYS}


def _gnome_write(s: Gio.Settings, values: dict):
    """Set values; None resets the key to its schema default."""
    for k in GNOME_KEYS:
        if values[k] is None:
            s.reset(k)
        elif not s.set_string(k, values[k]):
            raise DesktopError(f"cannot set {GNOME_SCHEMA} {k}")
    Gio.Settings.sync()
    got = _gnome_state(Gio.Settings.new_full(s.props.settings_schema, None, None))
    if got != {k: values[k] for k in GNOME_KEYS}:
        raise DesktopError(f"{s.props.schema_id} did not take the new values")


def _gnome_apply(path: str):
    s = _gnome_settings()
    snapshot = _gnome_state(s)
    backup = load_backup()
    if "gnome" not in backup:
        backup["gnome"] = snapshot
        save_backup(backup)
    uri = Gio.File.new_for_path(path).get_uri()
    try:
        _gnome_write(s, {"picture-uri": uri, "picture-uri-dark": uri, "picture-options": "zoom"})
    except Exception:
        _gnome_write(s, snapshot)
        raise


def _gnome_restore() -> bool:
    backup = load_backup()
    saved = backup.get("gnome")
    if not isinstance(saved, dict) or any(
            k not in saved or not (saved[k] is None or isinstance(saved[k], str)) for k in GNOME_KEYS):
        return False
    s = _gnome_settings()
    snapshot = _gnome_state(s)
    try:
        _gnome_write(s, saved)
    except Exception:
        _gnome_write(s, snapshot)
        raise
    del backup["gnome"]
    save_backup(backup)
    return True


def _uri_path(value: str) -> str | None:
    """Local path of a file:// URI or absolute path setting; None otherwise."""
    if value.startswith("/"):
        return value
    u = urlparse(value)
    return unquote(u.path) if u.scheme == "file" else None


def _picture_path(s: Gio.Settings) -> str | None:
    """Image shown for the current color scheme (picture-uri-dark under prefer-dark)."""
    iface = "org.gnome.desktop.interface"
    dark = _has_schema(iface) and Gio.Settings.new(iface).get_string("color-scheme") == "prefer-dark"
    return _uri_path(s.get_string("picture-uri-dark" if dark else "picture-uri"))


def _picture_paths(s: Gio.Settings) -> set:
    return {p for p in (_uri_path(s.get_string(k)) for k in ("picture-uri", "picture-uri-dark")) if p}


def _gnome_current_paths(lock_dir: str | None = None) -> set:
    paths = _picture_paths(_gnome_settings())
    saved = load_backup().get("gnome-lock")
    saved_dir = saved.get("dir") if isinstance(saved, dict) and isinstance(saved.get("dir"), str) else None
    for uuid in LOCK_EXTENSIONS:
        for d in {lock_dir, saved_dir, None}:
            s = _lock_settings(uuid, d)
            if s is not None:
                paths |= _picture_paths(s)
    return paths


# ---------- GNOME lock screen (extension) ----------

def _has_schema(schema_id: str) -> bool:
    source = Gio.SettingsSchemaSource.get_default()
    return source is not None and source.lookup(schema_id, True) is not None


def _extension_info(uuid: str) -> dict | None:
    """Live extension info from gnome-shell; None when the shell cannot be asked."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.gnome.Shell.Extensions", "/org/gnome/Shell/Extensions",
            "org.gnome.Shell.Extensions", "GetExtensionInfo", GLib.Variant("(s)", (uuid,)),
            GLib.VariantType("(a{sv})"), Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
    except GLib.Error:
        return None
    return reply.unpack()[0]


def _extension_dirs(uuid: str) -> list:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    data_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    return [os.path.join(d, "gnome-shell", "extensions", uuid) for d in [data_home, *data_dirs] if d]


def _enabled_in_settings(uuid: str) -> bool:
    """Fallback when gnome-shell cannot be asked: enabled in org.gnome.shell and installed."""
    if not _has_schema(SHELL_SCHEMA):
        return False
    s = Gio.Settings.new(SHELL_SCHEMA)
    schema = s.props.settings_schema
    if uuid not in s.get_strv("enabled-extensions"):
        return False
    if schema.has_key("disabled-extensions") and uuid in s.get_strv("disabled-extensions"):
        return False
    dirs = [d for d in _extension_dirs(uuid) if os.path.isfile(os.path.join(d, "metadata.json"))]
    if not dirs:
        return False
    user_ext = dirs[0] == _extension_dirs(uuid)[0]
    return not (user_ext and schema.has_key("disable-user-extensions")
                and s.get_boolean("disable-user-extensions"))


def _lock_settings(uuid: str, ext_dir: str | None = None) -> Gio.Settings | None:
    """Settings of a lock-screen extension (its own schemas dir first), None if not found."""
    schema_id = LOCK_EXTENSIONS[uuid]
    default = Gio.SettingsSchemaSource.get_default()
    for d in ([ext_dir] if ext_dir else []) + _extension_dirs(uuid):
        sdir = os.path.join(d, "schemas")
        if not os.path.isfile(os.path.join(sdir, "gschemas.compiled")):
            continue
        try:
            schema = Gio.SettingsSchemaSource.new_from_directory(sdir, default, False).lookup(schema_id, False)
        except GLib.Error:
            continue
        if schema is not None:
            break
    else:
        schema = default.lookup(schema_id, True) if default is not None else None
    if schema is None or not all(schema.has_key(k) for k in GNOME_KEYS):
        return None
    return Gio.Settings.new_full(schema, None, None)


def _installed_dir(uuid: str) -> str | None:
    dirs = [d for d in _extension_dirs(uuid) if os.path.isfile(os.path.join(d, "metadata.json"))]
    return dirs[0] if dirs else None


def gnome_lock_provider() -> tuple:
    """(uuid, ext_dir, problem) of the active extension drawing the lock screen.

    (None, None, None) when none is active; problem is set when it is active but unusable.
    """
    for uuid in LOCK_EXTENSIONS:
        info = _extension_info(uuid)
        if info is None:
            active, ext_dir = _enabled_in_settings(uuid), _installed_dir(uuid)
        else:
            active, ext_dir = info.get("state") == EXT_ACTIVE, info.get("path") or None
        if not active:
            continue
        if _lock_settings(uuid, ext_dir) is None:
            return uuid, ext_dir, (f"unsupported: lock-screen extension {uuid} is active "
                                   "but its settings schema cannot be loaded")
        return uuid, ext_dir, None
    return None, None, None


def _gnome_lock_apply(uuid: str, path: str, ext_dir: str | None = None):
    s = _lock_settings(uuid, ext_dir)
    if s is None:
        raise DesktopError(f"settings of lock-screen extension {uuid} not found")
    snapshot = _gnome_state(s)
    backup = load_backup()
    if "gnome-lock" not in backup:
        backup["gnome-lock"] = {"extension": uuid, "dir": ext_dir, "values": snapshot}
        save_backup(backup)
    uri = Gio.File.new_for_path(path).get_uri()
    try:
        _gnome_write(s, {"picture-uri": uri, "picture-uri-dark": uri, "picture-options": "zoom"})
    except Exception:
        _gnome_write(s, snapshot)
        raise


def _gnome_lock_restore(ext_dir: str | None = None) -> bool:
    backup = load_backup()
    saved = backup.get("gnome-lock")
    if not isinstance(saved, dict):
        return False
    uuid, values, saved_dir = saved.get("extension"), saved.get("values"), saved.get("dir")
    if uuid not in LOCK_EXTENSIONS or not isinstance(values, dict) or not (
            saved_dir is None or isinstance(saved_dir, str)) or any(
            k not in values or not (values[k] is None or isinstance(values[k], str)) for k in GNOME_KEYS):
        raise DesktopError("invalid gnome-lock section in backup file")
    s = _lock_settings(uuid, saved_dir or ext_dir)
    if s is None:
        raise DesktopError(f"lock-screen extension {uuid} is no longer installed")
    snapshot = _gnome_state(s)
    try:
        _gnome_write(s, values)
    except Exception:
        _gnome_write(s, snapshot)
        raise
    del backup["gnome-lock"]
    save_backup(backup)
    return True


# ---------- XFCE ----------

def _xfconf(*args, check=True) -> str:
    try:
        out = subprocess.run(["xfconf-query", "-c", XFCE_CHANNEL, *args],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise DesktopError(f"xfconf-query failed: {e}") from None
    if check and out.returncode != 0:
        msg = (out.stderr or out.stdout).strip().splitlines()
        raise DesktopError(f"xfconf-query {' '.join(args[:2])} failed: {msg[-1] if msg else out.returncode}")
    return out.stdout.strip() if out.returncode == 0 else None


def _xfce_props() -> list:
    listing = _xfconf("-l", check=False) or ""
    return sorted(p for p in listing.splitlines() if XFCE_PROP_RE.match(p.strip()))


def _monitors_gtk4() -> list:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gtk

    if not Gtk.init_check():
        return []
    display = Gdk.Display.get_default()
    if display is None:
        return []
    model = display.get_monitors()
    return [model.get_item(i).get_connector() for i in range(model.get_n_items())]


def _monitors_gtk3() -> list:
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk  # noqa: F401  (importing Gtk 3 initialises GDK)

    screen = Gdk.Screen.get_default()
    if screen is None:
        return []
    return [screen.get_monitor_plug_name(i) for i in range(screen.get_n_monitors())]


def _probe_monitors() -> list:
    """In-process GTK probe; only run in the child started by _xfce_monitors."""
    for probe in (_monitors_gtk4, _monitors_gtk3):
        try:
            names = [n for n in probe() if n]
        except (ValueError, ImportError, AttributeError, TypeError):
            continue  # typelib missing or the other GTK major already loaded
        if names:
            return names
    return []


def _xfce_monitors() -> list:
    """Monitor connector names (xfdesktop 4.20 keys backdrops by connector); [] if unknown.

    Probed in a child process: GTK must not run in the caller's (possibly worker) thread.
    """
    cmd = [sys.executable or "/usr/bin/python3", "-I", os.path.abspath(__file__), MONITORS_ARG]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [n.strip() for n in out.stdout.splitlines() if n.strip()]


def _xfce_workspaces() -> int:
    try:
        out = subprocess.run(["xfconf-query", "-c", "xfwm4", "-p", "/general/workspace_count"],
                             capture_output=True, text=True, timeout=15)
        count = int(out.stdout.strip()) if out.returncode == 0 else 1
    except (OSError, subprocess.SubprocessError, ValueError):
        count = 1
    return max(1, min(count, 64))


def _xfce_get(prop: str):
    return _xfconf("-p", prop, check=False)


def _xfce_set(prop: str, value):
    """Set prop to value; None removes it. Creates the prop if missing."""
    if value is None:
        if _xfce_get(prop) is not None:
            _xfconf("-p", prop, "-r")
        return
    if _xfce_get(prop) is not None:
        _xfconf("-p", prop, "-s", value)
    else:
        kind = "int" if prop.endswith("/image-style") else "string"
        _xfconf("-p", prop, "-n", "-t", kind, "-s", value)


def _xfce_write(values: dict):
    for prop, value in values.items():
        _xfce_set(prop, value)
    for prop, value in values.items():
        if _xfce_get(prop) != value:
            raise DesktopError(f"xfconf did not take the new value for {prop}")


def _xfce_apply(path: str):
    props = set(_xfce_props())
    # Cover every monitor x workspace, including ones without props yet.
    monitors = set(_xfce_monitors()) | {p.split("/")[3][len("monitor"):] for p in props}
    workspaces = _xfce_workspaces()
    for mon in monitors:
        for ws in range(workspaces):
            props.add(f"/backdrop/screen0/monitor{mon}/workspace{ws}/last-image")
    if not props:
        raise DesktopError("no xfdesktop backdrop properties found and monitors could not be listed")
    targets = {}
    for p in sorted(props):
        targets[p] = path
        style = p[: -len("last-image")] + "image-style"
        if _xfce_get(style) in (None, "0"):  # absent or "none" shows no image
            targets[style] = "5"
    snapshot = {p: _xfce_get(p) for p in targets}
    backup = load_backup()
    saved = backup.get("xfce") if isinstance(backup.get("xfce"), dict) else {}
    new_keys = {p: v for p, v in snapshot.items() if p not in saved}
    if new_keys or "xfce" not in backup:
        backup["xfce"] = {**saved, **new_keys}
        save_backup(backup)
    try:
        _xfce_write(targets)
    except Exception:
        _xfce_write(snapshot)
        raise


def _xfce_restore() -> bool:
    backup = load_backup()
    saved = backup.get("xfce")
    if not isinstance(saved, dict) or not saved:
        return False
    for prop, value in saved.items():
        if not isinstance(prop, str) or not prop.startswith("/backdrop/") or not (value is None or isinstance(value, str)):
            raise DesktopError("invalid xfce section in backup file")
    snapshot = {p: _xfce_get(p) for p in saved}
    try:
        _xfce_write(saved)
    except Exception:
        _xfce_write(snapshot)
        raise
    del backup["xfce"]
    save_backup(backup)
    return True


def _xfce_current_paths() -> set:
    listing = _xfconf("-l").splitlines()
    return {v for v in (_xfce_get(p) for p in listing if XFCE_PROP_RE.match(p.strip())) if v}


# ---------- public API ----------

def apply(de: str, path: str):
    """Set the wallpaper; raises on failure after rolling back."""
    if de == "gnome":
        _gnome_apply(path)
    elif de == "xfce":
        _xfce_apply(path)
    else:
        raise DesktopError(f"unsupported desktop environment {de}")


def restore(de: str) -> bool:
    """Restore the original wallpaper; returns False when there is nothing to restore."""
    if de == "gnome":
        return _gnome_restore()
    if de == "xfce":
        return _xfce_restore()
    raise DesktopError(f"unsupported desktop environment {de}")


def current_paths(de: str, lock_dir: str | None = None) -> set:
    """Wallpaper files currently referenced; raises when settings cannot be read."""
    if de == "gnome":
        return _gnome_current_paths(lock_dir)
    if de == "xfce":
        return _xfce_current_paths()
    return set()


def apply_lock(de: str, provider: str, path: str, ext_dir: str | None = None):
    """Set a separate lock-screen image (GNOME lock-screen extension); rolls back on failure."""
    if de != "gnome" or provider not in LOCK_EXTENSIONS:
        raise DesktopError(f"no separate lock-screen image on {de}")
    _gnome_lock_apply(provider, path, ext_dir)


def restore_lock(de: str, ext_dir: str | None = None) -> bool:
    """Restore the lock-screen extension's image; False when there is nothing to restore."""
    return _gnome_lock_restore(ext_dir) if de == "gnome" else False


def current_images(de: str, lock_provider: str | None = None, lock_dir: str | None = None) -> dict:
    """{"desktop": path|None, "lock": path|None} as currently configured; raises if unreadable."""
    if de == "gnome":
        wall = _picture_path(_gnome_settings())
        if lock_provider not in LOCK_EXTENSIONS:
            return {"desktop": wall, "lock": wall}
        lock_settings = _lock_settings(lock_provider, lock_dir)
        return {"desktop": wall, "lock": _picture_path(lock_settings) if lock_settings else None}
    if de == "xfce":
        paths = sorted(_xfce_current_paths())
        wall = paths[0] if paths else None
        return {"desktop": wall, "lock": wall}
    return {"desktop": None, "lock": None}


def has_backup(section: str) -> bool:
    """True when the backup file holds a section ("gnome", "xfce", "gnome-lock") to restore."""
    try:
        return bool(load_backup().get(section))
    except DesktopError:
        return False


if __name__ == "__main__" and sys.argv[1:] == [MONITORS_ARG]:
    print("\n".join(_probe_monitors()))
