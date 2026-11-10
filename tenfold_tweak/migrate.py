"""One-time move of per-user state from the old tool name (tenfold-tweak) to tenfold-tweaks."""

import json
import os
import shutil
import stat
import tempfile

from . import desktop, image

TOOL_PREFIXES = (image.COPY_PREFIX, image.LOGIN_PREFIX, ".incoming-")
_done = False


def _is_dir(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _rmdir_if_empty(path: str, warnings: list):
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass
    except OSError:
        warnings.append(f"{path} still holds other files; left in place")


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def _migrate_backup(warnings: list):
    old_dir = desktop.LEGACY_STATE_DIR
    if not os.path.lexists(old_dir):
        return
    if not _is_dir(old_dir):
        warnings.append(f"{old_dir} is not a directory; left in place")
        return
    old = os.path.join(old_dir, "backup.json")
    try:
        st = os.lstat(old)
    except FileNotFoundError:
        st = None
    if st is not None and stat.S_ISREG(st.st_mode):
        try:
            if not os.path.lexists(desktop.BACKUP_FILE):
                os.makedirs(desktop.STATE_DIR, mode=0o700, exist_ok=True)
                os.replace(old, desktop.BACKUP_FILE)
            else:
                merged = desktop.load_backup()
                for section, value in _read_json(old).items():
                    merged.setdefault(section, value)  # the new file wins
                desktop.save_backup(merged)
                os.unlink(old)
        except (OSError, ValueError, desktop.DesktopError) as e:
            warnings.append(f"cannot move {old}: {e}")
            return
    elif st is not None:
        warnings.append(f"{old} is not a regular file; left in place")
    _rmdir_if_empty(old_dir, warnings)


def _link_or_copy(src: str, dst: str):
    try:
        os.link(src, dst)
        return
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".incoming-", dir=os.path.dirname(dst))
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def _referenced_in(old_dir: str, de: str, lock_dir: str | None) -> bool:
    """Re-read settings and backup: does anything still point inside old_dir?"""
    old = old_dir.rstrip("/") + "/"
    _, n = desktop._relocate_values(desktop.load_backup(), old_dir, old_dir)
    return n > 0 or any(p.startswith(old) for p in desktop.current_paths(de, lock_dir))


def _migrate_copies(de: str | None, lock_dir: str | None, warnings: list) -> bool:
    """True when nothing is left to migrate."""
    old_dir, new_dir = image.LEGACY_COPY_DIR, image.COPY_DIR
    if not os.path.lexists(old_dir):
        return True
    if not _is_dir(old_dir):
        warnings.append(f"{old_dir} is not a directory; left in place")
        return True
    if de not in ("gnome", "xfce"):
        return False  # settings cannot be rewritten here, so files that may be referenced stay
    names = [n for n in sorted(os.listdir(old_dir)) if n.startswith(TOOL_PREFIXES)
             and stat.S_ISREG(os.lstat(os.path.join(old_dir, n)).st_mode)]
    os.makedirs(new_dir, mode=0o700, exist_ok=True)
    os.chmod(new_dir, 0o700)
    for name in names:
        dst = os.path.join(new_dir, name)
        if not os.path.lexists(dst):
            _link_or_copy(os.path.join(old_dir, name), dst)
    try:
        desktop.relocate(de, old_dir, new_dir, lock_dir)  # rolls back on failure
        if _referenced_in(old_dir, de, lock_dir):
            raise desktop.DesktopError("settings still point at the old directory")
    except Exception as e:
        warnings.append(f"cannot move images from {old_dir}: {e}; retried next run")
        return False
    for name in names:
        os.unlink(os.path.join(old_dir, name))
    _rmdir_if_empty(old_dir, warnings)
    return True


def migrate_user(de: str | None = None, lock_dir: str | None = None) -> list:
    """Move backup.json and image copies from the old tool name; returns warnings. Idempotent."""
    global _done
    if _done:
        return []
    _done = True  # once per process, even when the attempt fails; retried next run
    warnings = []
    _migrate_backup(warnings)
    _migrate_copies(de, lock_dir, warnings)
    return warnings
