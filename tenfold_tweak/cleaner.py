"""Junk/cache cleaner shared by the CLI and GUI: categories, read-only preview, user-side deletion."""

import os
import stat
from dataclasses import dataclass, field

from . import root_helper as rh  # read-only parts and constants; root work goes through system.run_clean
from . import system

DISK_NOTE = "Frees disk space, not RAM."
TRASH_WARNING = "Everything in the Trash will be deleted permanently."
KERNEL_NOTE = "kernel packages are never removed"
PM_PROCS = {"apt", "apt-get", "aptitude", "dpkg", "unattended-upgr"}
NOT_DIR = "not a regular directory"
MAX_DEPTH = 64
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
DEFAULT_USER_DIRS = ("Desktop", "Documents", "Downloads", "Music", "Pictures", "Public", "Templates", "Videos")


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    level: str  # user | system | mixed
    default: bool
    warning: str = ""


CATEGORIES = (
    Category("apt-cache", "APT package cache", "system", True),
    Category("journal", f"Old system logs (>{rh.JOURNAL_DAYS} days)", "system", True),
    Category("thumbnails", "Thumbnail cache", "user", True),
    Category("tool-images", "Unused Tenfold Tweaks images", "mixed", True),
    Category("cache", "Other files in ~/.cache", "user", False),
    Category("trash", "Trash", "user", False, TRASH_WARNING),
    Category("autoremove", "Orphan packages", "system", False),
)
BY_ID = {c.id: c for c in CATEGORIES}


@dataclass
class Estimate:
    id: str
    size: int | None  # bytes; None = unknown
    count: int | None
    packages: list = field(default_factory=list)
    busy: bool = False
    note: str = ""
    at_least: bool = False  # size is a lower bound (part not readable without admin)
    admin: bool = False  # cleaning this category needs the admin prompt


@dataclass
class Preview:
    items: list
    total: int
    total_unknown: bool
    token: str | None  # autoremove plan token, passed back to the root helper


@dataclass
class CleanResult:
    id: str
    status: str  # cleaned | skipped | failed
    freed: int = 0
    removed: int = 0
    skipped: dict = field(default_factory=lambda: {"in-use": 0, "other-fs": 0})
    reason: str = ""

    def line(self) -> str:
        if self.status != "cleaned":
            return f"{self.id}: {self.status} ({self.reason})"
        text = f"{self.id}: cleaned {human(self.freed)}, {self.removed} items"
        if self.skipped.get("in-use"):
            text += f", {self.skipped['in-use']} skipped (in use)"
        if self.skipped.get("other-fs"):
            text += f", {self.skipped['other-fs']} skipped (other filesystem)"
        return text + (f" ({self.reason})" if self.reason else "")


@dataclass
class Report:
    items: list
    freed: int


@dataclass
class _Tally:
    size: int = 0
    count: int = 0
    in_use: int = 0
    other_fs: int = 0
    errors: int = 0
    complete: bool = True


class _NotDir(Exception):
    pass


def human(n: int | None) -> str:
    if n is None:
        return "unknown"
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            break
        n /= 1000
    if unit == "B":
        return f"{int(n)} B"
    return f"{n:.1f} {unit}" if n < 9.95 else f"{n:.0f} {unit}"


def default_ids() -> list:
    return [c.id for c in CATEGORIES if c.default]


def _check_ids(ids) -> list:
    ids = list(ids)
    unknown = [i for i in ids if i not in BY_ID]
    if unknown:
        raise ValueError(f"unknown cleaner category: {', '.join(unknown)}")
    return [c.id for c in CATEGORIES if c.id in ids]


# ---------- user directories ----------

def _user_dirs(home: str) -> set:
    """XDG user dirs (Documents, Pictures, ...) that a cleaner root may never equal or contain."""
    dirs = {os.path.join(home, d) for d in DEFAULT_USER_DIRS}
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    try:
        with open(os.path.join(config, "user-dirs.dirs"), encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and key.startswith("XDG_") and key.endswith("_DIR"):
                    value = value.strip().strip('"').replace("$HOME", home)
                    if os.path.isabs(value):
                        dirs.add(value)
    except (OSError, UnicodeDecodeError):
        pass
    return {os.path.realpath(d) for d in dirs}


def _xdg_base(var: str, default: str) -> str:
    """$var when absolute, inside $HOME, not $HOME and clear of user dirs; else ~/default (_NotDir if that isn't)."""
    home = os.path.realpath(os.path.expanduser("~"))
    blocked = _user_dirs(home) | {home}

    def fallback():
        path = os.path.join(os.path.expanduser("~"), default)
        real_fb = os.path.realpath(path)
        if any(d == real_fb or d.startswith(real_fb + os.sep) for d in blocked):
            raise _NotDir(f"~/{default} is or holds a user directory")
        return path

    value = os.environ.get(var, "")
    if not os.path.isabs(value):
        return fallback()
    real = os.path.realpath(value)
    if not real.startswith(home + os.sep):
        return fallback()
    if any(d == real or d.startswith(real + os.sep) for d in blocked):
        return fallback()
    own = os.path.realpath(os.path.join(home, default))
    # allowlist: under a hidden top-level dir of $HOME (.ssh, .gnupg, ...) only the spec default or inside it
    if os.path.relpath(real, home).startswith(".") and real != own and not real.startswith(own + os.sep):
        return fallback()
    # other XDG bases (defaults and env values): never equal, contain or sit inside one
    bases = {os.path.join(home, d) for d in (".config", ".cache", os.path.join(".local", "share"),
                                             os.path.join(".local", "state"))}
    for other in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        v = os.environ.get(other, "")
        if other != var and os.path.isabs(v):
            bases.add(os.path.realpath(v))
    for b in bases - {own}:
        if b == real or b.startswith(real + os.sep) or real.startswith(b + os.sep):
            return fallback()
    return real


def cache_dir() -> str:
    return _xdg_base("XDG_CACHE_HOME", ".cache")


def data_dir() -> str:
    return _xdg_base("XDG_DATA_HOME", os.path.join(".local", "share"))


def _scopes(cid: str) -> list:
    """(base, parts, only, skip) per category: contents of base/parts..., limited to/excluding top-level names."""
    if cid == "thumbnails":
        return [(cache_dir(), ("thumbnails",), None, ())]
    if cid == "cache":
        return [(cache_dir(), (), None, ("thumbnails",))]
    if cid == "trash":
        return [(data_dir(), ("Trash", d), None, ()) for d in ("files", "info", "expunged")] + \
               [(data_dir(), ("Trash",), {"directorysizes"}, ())]
    return []


# ---------- safe walk (dir fds, no symlink follow, -xdev, in-use skip) ----------

def open_inodes() -> set:
    """(dev, ino) of files open or mapped by this user's processes."""
    uid, found = os.getuid(), set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        base = os.path.join("/proc", pid)
        try:
            if os.stat(base).st_uid != uid:
                continue
            fds = os.listdir(os.path.join(base, "fd"))
        except OSError:
            continue
        for fd in fds:
            try:
                st = os.stat(os.path.join(base, "fd", fd))
                found.add((st.st_dev, st.st_ino))
            except OSError:
                pass
        try:
            with open(os.path.join(base, "maps"), encoding="utf-8", errors="replace") as f:
                paths = {ln.split(None, 5)[5].strip() for ln in f if len(ln.split(None, 5)) == 6}
        except OSError:
            continue
        for p in paths:
            if p.startswith("/") and not p.endswith(" (deleted)"):
                try:
                    st = os.stat(p)
                    found.add((st.st_dev, st.st_ino))
                except OSError:
                    pass
    return found


def _open_root(path: str) -> int | None:
    """fd of a category root: own, real directory; None when absent, _NotDir otherwise."""
    try:
        fd = os.open(path, DIR_FLAGS)
    except FileNotFoundError:
        return None
    except OSError:
        raise _NotDir() from None
    if os.fstat(fd).st_uid != os.getuid():
        os.close(fd)
        raise _NotDir()
    return fd


def _open_scope(base: str, parts: tuple) -> int | None:
    """fd of base/parts...: every component opened via dir_fd, no symlinks, owned by the user."""
    fd = _open_root(base)
    for name in parts:
        if fd is None:
            return None
        try:
            child = os.open(name, DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            child = None
        except OSError:
            raise _NotDir() from None
        finally:
            os.close(fd)
        if child is not None and os.fstat(child).st_uid != os.getuid():
            os.close(child)
            raise _NotDir()
        fd = child
    return fd


def _walk(dfd: int, dev: int, tally: _Tally, seen: set, in_use: set | None, delete: bool,
          only=None, skip=(), depth: int = 0):
    try:
        with os.scandir(dfd) as it:
            names = [e.name for e in it]
    except OSError:
        tally.complete = False
        return
    for name in names:
        if (only is not None and name not in only) or name in skip:
            continue
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            tally.complete = False
            continue
        if stat.S_ISDIR(st.st_mode):
            if st.st_dev != dev:
                tally.other_fs += 1
                continue
            if depth >= MAX_DEPTH:
                tally.complete = False
                continue
            try:
                cfd = os.open(name, DIR_FLAGS, dir_fd=dfd)
            except OSError:
                tally.complete = False
                continue
            try:
                _walk(cfd, dev, tally, seen, in_use, delete, depth=depth + 1)
            finally:
                os.close(cfd)
            if delete:
                try:
                    os.rmdir(name, dir_fd=dfd)
                except OSError:
                    pass  # not empty: something inside was kept
            continue
        key = (st.st_dev, st.st_ino)
        if delete:
            if key in in_use:
                tally.in_use += 1
                continue
            try:
                os.unlink(name, dir_fd=dfd)  # a symlink goes as a link
            except FileNotFoundError:
                continue
            except OSError:
                tally.errors += 1
                continue
        tally.count += 1
        if key not in seen:
            seen.add(key)
            tally.size += st.st_blocks * 512


def _scan(cid: str, seen: set, in_use: set | None = None, delete: bool = False) -> _Tally:
    tally, opened = _Tally(), []
    try:
        # validate every root before anything is deleted
        for base, parts, only, skip in _scopes(cid):
            fd = _open_scope(base, parts)
            if fd is not None:
                opened.append((fd, only, skip))
        for fd, only, skip in opened:
            _walk(fd, os.fstat(fd).st_dev, tally, seen, in_use, delete, only, skip)
    finally:
        for fd, _only, _skip in opened:
            os.close(fd)
    return tally


# ---------- tool-images (user part) ----------

def _user_copies() -> list | None:
    """Unreferenced tool copies in the user's data dir; None when the settings cannot be read."""
    from . import desktop, detect, image
    env = detect.detect()
    if env.de not in ("gnome", "xfce"):
        return None
    try:
        keep = desktop.current_paths(env.de, env.lock_provider_dir)
    except Exception:
        return None
    return image.unreferenced_copies(keep)


def _delete_copies(paths: list, seen: set, in_use: set) -> _Tally:
    from . import image
    tally, wanted = _Tally(), set(paths)
    for dfd, name, path, st in image.tool_copies():  # dir_fd, no symlinks, own regular files only
        if path not in wanted:
            continue
        if (st.st_dev, st.st_ino) in in_use:
            tally.in_use += 1
            continue
        try:
            os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            continue
        except OSError:
            tally.errors += 1
            continue
        tally.count += 1
        if (st.st_dev, st.st_ino) not in seen:
            seen.add((st.st_dev, st.st_ino))
            tally.size += st.st_blocks * 512
    image.remove_legacy_dir()
    return tally


# ---------- preview ----------

def pm_running() -> bool:
    """Hint without root: a package manager process is running."""
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(os.path.join("/proc", pid, "comm"), encoding="utf-8") as f:
                    if f.read().strip() in PM_PROCS:
                        return True
            except OSError:
                pass
    return False


def _estimate(cid: str, seen: set, busy: bool) -> tuple:
    """(Estimate, autoremove token or None)."""
    level = BY_ID[cid].level
    est = Estimate(cid, 0, 0, admin=level != "user")
    if cid == "apt-cache":
        paths, complete = rh.apt_cache_files()
        est.size, est.count, ok = rh.disk_usage(paths, seen)
        est.at_least = not (complete and ok)
        est.note = "" if complete else "partial downloads not counted"
        est.busy = busy
    elif cid == "journal":
        if not os.path.exists(rh.JOURNALCTL):
            est.note = "skipped (journalctl not available)"
        else:
            est.size, est.count, ok = rh.disk_usage(rh.journal_archives(rh.JOURNAL_DAYS), seen)
            est.at_least = not ok
    elif cid == "tool-images":
        system_paths = rh.tool_leftovers()
        size, count, ok = rh.disk_usage(system_paths, seen)
        est.admin = bool(system_paths) or not ok
        try:
            copies = _user_copies()
        except _NotDir as e:
            est.note, copies = f"your own copies skipped ({str(e) or NOT_DIR})", []
        if copies is None:
            est.note = "your own copies kept (wallpaper settings unreadable)"
            copies = []
        usize, ucount, uok = rh.disk_usage(copies, seen)
        est.size, est.count, est.at_least = size + usize, count + ucount, not (ok and uok)
    elif cid == "autoremove":
        est.packages, token = rh.autoremove_plan()
        est.size, est.count = rh.package_sizes(est.packages), len(est.packages)
        est.note = KERNEL_NOTE
        est.busy = busy
        return est, token
    else:
        tally = _scan(cid, seen)
        est.size, est.count, est.at_least = tally.size, tally.count, not tally.complete
        if cid == "trash":
            est.note = TRASH_WARNING
    return est, None


def preview(ids=None) -> Preview:
    """Read-only estimate per category (all categories when ids is None); never asks for admin."""
    ids = [c.id for c in CATEGORIES] if ids is None else _check_ids(ids)
    seen, items, token = set(), [], None
    busy = pm_running() if {"apt-cache", "autoremove"} & set(ids) else False
    for cid in ids:
        try:
            est, tok = _estimate(cid, seen, busy)
            token = tok or token
        except _NotDir as e:
            est = Estimate(cid, None, None, note=f"skipped ({str(e) or NOT_DIR})")
        except Exception as e:
            est = Estimate(cid, None, None, admin=BY_ID[cid].level != "user",
                           note=f"cannot read: {e}".replace("\n", " "))
        if est.busy:
            est.note = "package manager busy" + (f"; {est.note}" if est.note else "")
        items.append(est)
    total, unknown = selected_total(ids, Preview(items, 0, False, token))
    return Preview(items, total, unknown, token)


def selected_total(ids, preview: Preview) -> tuple:
    """(bytes, unknown) over the preview items in ids; unknown means the real total may be larger."""
    total, unknown = 0, False
    for est in preview.items:
        if est.id in ids:
            total += est.size or 0
            unknown = unknown or est.size is None or est.at_least
    return total, unknown


def estimate_text(est: Estimate) -> str:
    """Size/count text of a preview row, e.g. "≥ 45 MB · 12 items"; shared by CLI and GUI."""
    if est.size is None:
        return "unknown"
    text = ("≥ " if est.at_least else "") + human(est.size)
    if est.count:
        unit = "package" if est.id == "autoremove" else "item"
        text += f" · {est.count} {unit}{'s' if est.count != 1 else ''}"
    return text


def system_ids(ids, preview: Preview) -> list:
    """Selected categories whose (part) runs through the root helper, i.e. need the admin prompt."""
    est = {e.id: e for e in preview.items}
    return [i for i in _check_ids(ids) if BY_ID[i].level == "system"
            or (BY_ID[i].level == "mixed" and (est[i].admin if i in est else True))]


def needs_admin(ids, preview: Preview) -> bool:
    return bool(system_ids(ids, preview))


def confirm_text(ids, preview: Preview) -> str:
    ids = _check_ids(ids)
    total, unknown = selected_total(ids, preview)
    labels = ", ".join(BY_ID[i].label for i in ids)
    return f"Delete {'≥ ' if unknown else ''}{human(total)} from: {labels}?"


# ---------- clean ----------

def _result(cid: str, tally: _Tally) -> CleanResult:
    reason = f"{tally.errors} could not be deleted" if tally.errors else ""
    return CleanResult(cid, "cleaned", tally.size, tally.count,
                       {"in-use": tally.in_use, "other-fs": tally.other_fs}, reason)


def _merge_tool_images(system_part: CleanResult | None, user: CleanResult) -> CleanResult:
    if system_part is None:
        return user
    if user.status != "cleaned" and system_part.status != "cleaned":
        return system_part
    base = user if user.status == "cleaned" else system_part
    other = system_part if base is user else user
    reasons = [r for r in (base.reason, "" if other.status == "cleaned" else
                           f"{'system' if other is system_part else 'user'} part {other.status}: {other.reason}") if r]
    skipped = {k: base.skipped.get(k, 0) + other.skipped.get(k, 0) for k in ("in-use", "other-fs")}
    return CleanResult("tool-images", "cleaned", base.freed + other.freed, base.removed + other.removed,
                       skipped, "; ".join(reasons))


def clean(ids, preview: Preview) -> Report:
    """Blocking (pkexec at most once, system part first); call it off the UI thread."""
    ids = _check_ids(ids)
    root, admin_ids = {}, system_ids(ids, preview)
    if admin_ids:
        expect = preview.token if "autoremove" in admin_ids else None
        for cid, r in system.run_clean(admin_ids, expect).items():
            root[cid] = CleanResult(cid, r["status"], r["freed"], r["removed"], r["skipped"], r["reason"])
    seen, in_use = set(), None
    results = []
    for cid in ids:
        if BY_ID[cid].level == "system":
            results.append(root[cid])
            continue
        try:
            if in_use is None:
                in_use = open_inodes()
            if cid == "tool-images":
                try:
                    copies = _user_copies()
                    if copies is None:
                        user = CleanResult(cid, "skipped", reason="wallpaper settings unreadable")
                    else:
                        user = _result(cid, _delete_copies(copies, seen, in_use))
                except _NotDir as e:
                    user = CleanResult(cid, "skipped", reason=str(e) or NOT_DIR)
                results.append(_merge_tool_images(root.get(cid), user))
            else:
                results.append(_result(cid, _scan(cid, seen, in_use, delete=True)))
        except _NotDir as e:
            results.append(CleanResult(cid, "skipped", reason=str(e) or NOT_DIR))
        except Exception as e:
            results.append(CleanResult(cid, "failed", reason=str(e).replace("\n", " ") or type(e).__name__))
    return Report(results, sum(r.freed for r in results))
