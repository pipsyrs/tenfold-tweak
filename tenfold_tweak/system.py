"""Runs the root helper through pkexec and maps its exit codes to results."""

import configparser
import json
import os
import stat
import subprocess

# Only this root-owned copy (installed by the .deb) is ever run through pkexec.
HELPER = "/usr/libexec/tenfold-tweaks/root-helper"
INSTALL_HINT = "sudo apt install ./tenfold-tweaks_*.deb"
PKEXEC = "/usr/bin/pkexec"
PYTHON = "/usr/bin/python3"
ACTIONS = ("gdm-apply", "gdm-restore", "lightdm-apply", "lightdm-restore")

INSTALL_LIB = "/usr/share/tenfold-tweaks"
# Login images saved by package removal (root_helper.RESCUE_DIR).
RESCUE_DIR = "/var/backups/tenfold-tweaks"
# Directory holding the tenfold_tweak package in use (source tree or INSTALL_LIB).
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def helper_problem(path: str | None = None) -> str | None:
    """None when the helper and every parent dir are root-owned, non-symlink and not group/other-writable."""
    path = path or HELPER
    try:
        st = os.lstat(path)
    except OSError:
        return f"root helper not installed; {INSTALL_HINT}"
    if not stat.S_ISREG(st.st_mode):
        return f"{path} is not a regular file; {INSTALL_HINT}"
    cur = path
    while True:
        try:
            st = os.lstat(cur)
        except OSError:
            return f"cannot check {cur}; {INSTALL_HINT}"
        if st.st_uid != 0 or st.st_mode & 0o022 or (cur != path and not stat.S_ISDIR(st.st_mode)):
            return f"{cur} is not root-owned or is writable by others; {INSTALL_HINT}"
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def run_helper(action: str, image: str | None = None) -> tuple:
    """Return (status, reason) with status in applied|skipped|failed."""
    if action not in ACTIONS:
        raise ValueError(f"unknown helper action {action}")
    cmd = [PKEXEC, HELPER, action]
    if action.endswith("-apply"):
        if not image or not os.path.isabs(image):
            raise ValueError("helper apply needs an absolute image path")
        cmd.append(image)
    problem = helper_problem()
    if problem:
        return "skipped", f"unsupported: {problem}"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return "failed", f"cannot run pkexec: {e.strerror}"
    if proc.returncode == 0:
        return "applied", ""
    if proc.returncode == 3:
        return "skipped", "nothing to restore"
    if proc.returncode in (126, 127):
        return "skipped", "authorization cancelled"
    lines = [ln for ln in proc.stderr.strip().splitlines() if ln.strip()]
    return "failed", lines[-1].strip() if lines else f"helper exited with {proc.returncode}"


def run_clean(ids: list, expect: str | None = None) -> dict:
    """Run the helper's `clean` once; {id: {status, freed, removed, skipped, reason}} for every id."""
    def every(status, reason):
        return {i: {"status": status, "freed": 0, "removed": 0, "skipped": {"in-use": 0, "other-fs": 0},
                    "reason": reason} for i in ids}

    if not ids:
        return {}
    cmd = [PKEXEC, HELPER, "clean", *ids] + (["--expect-autoremove", expect] if expect else [])
    problem = helper_problem()
    if problem:
        return every("skipped", f"unsupported: {problem}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return every("failed", f"cannot run pkexec: {e.strerror}")
    if proc.returncode in (126, 127):
        return every("skipped", "admin cancelled")
    lines = [ln for ln in proc.stderr.strip().splitlines() if ln.strip()]
    if proc.returncode != 0:
        return every("failed", lines[-1].strip() if lines else f"helper exited with {proc.returncode}")
    try:
        results = json.loads(proc.stdout.strip().splitlines()[-1])["results"]
    except (ValueError, IndexError, KeyError, TypeError):
        return every("failed", "unreadable result from the root helper")
    out = every("failed", "no result from the root helper")
    for i in ids:
        r = results.get(i) if isinstance(results, dict) else None
        if isinstance(r, dict) and r.get("status") in ("cleaned", "skipped", "failed"):
            skipped = r.get("skipped") if isinstance(r.get("skipped"), dict) else {}
            out[i] = {"status": r["status"], "freed": int(r.get("freed") or 0), "removed": int(r.get("removed") or 0),
                      "skipped": {k: int(skipped.get(k) or 0) for k in ("in-use", "other-fs")},
                      "reason": str(r.get("reason") or "")}
    return out


def login_status(dm: str) -> dict:
    """{"method", "current", "restorable"} for the login screen; read-only, no root needed."""
    from . import root_helper as rh  # paths only; keeps them in sync with the helper

    if dm == "gdm3":
        if rh.kali_override_active():
            restorable = os.path.lexists(rh.KALI_LOGIN_BG_DIVERTED) or os.path.lexists(rh.divert_marker())
            current = os.path.realpath(rh.KALI_LOGIN_BG) if os.path.exists(rh.KALI_LOGIN_BG) else None
            return {"method": "Kali login image (dpkg-divert)", "current": current, "restorable": restorable}
        # The image is embedded in the theme gresource; no file to point at.
        return {"method": "GNOME Shell theme (gresource)", "current": None,
                "restorable": os.path.exists(rh.GRESOURCE_ORIG)}
    if dm == "lightdm":
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            cp.read(rh.LIGHTDM_CONF, encoding="utf-8")
            current = cp.get("greeter", "background", fallback="").strip() or None
        except (configparser.Error, UnicodeDecodeError):
            current = None
        if current and not current.startswith("/"):
            current = None  # a colour, not an image
        return {"method": "lightdm-gtk-greeter.conf", "current": current,
                "restorable": os.path.exists(rh.LIGHTDM_ORIG)}
    return {"method": "", "current": None, "restorable": False}


def saved_login_images() -> list:
    """Login images saved in RESCUE_DIR by an earlier package removal; read-only."""
    try:
        names = sorted(os.listdir(RESCUE_DIR))
    except OSError:
        return []
    paths = [os.path.join(RESCUE_DIR, n) for n in names if not n.startswith(".")]
    return [p for p in paths if os.path.isfile(p) and not os.path.islink(p)]


# ---------- installed package state ----------

def running_installed() -> bool:
    return PROJECT_DIR == INSTALL_LIB


def helper_outdated() -> bool:
    """True when run from a source tree whose root_helper.py differs from the installed one."""
    if running_installed():
        return False
    try:
        with open(HELPER, "rb") as a, open(os.path.join(PROJECT_DIR, "tenfold_tweak", "root_helper.py"), "rb") as b:
            return a.read() != b.read()
    except OSError:
        return False


def setup_problem() -> str | None:
    """Why the login target needs the tenfold-tweaks package (re)installed (no CLI hint); None when ready."""
    problem = helper_problem()
    if problem:
        if not os.path.lexists(HELPER):
            return "login-screen support is not installed (install the tenfold-tweaks package)"
        return problem.removesuffix("; " + INSTALL_HINT) + " (reinstall needed)"
    if helper_outdated():
        return "login-screen support is outdated (reinstall needed)"
    return None

