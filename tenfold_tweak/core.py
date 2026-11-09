"""Shared apply/restore flow used by the CLI and the GUI."""

import os
import subprocess
from dataclasses import dataclass
from typing import Callable

from . import desktop, detect, image, system
from .image import ImageError  # noqa: F401  (re-exported for front ends)

TARGETS = detect.TARGETS
APPLIED, SKIPPED, FAILED = "applied", "skipped", "failed"

GDM_WARNING = (
    "Changing the GDM login screen modifies system files and needs administrator rights: "
    "on Kali's gnome-shell it replaces the login background image (kept via dpkg-divert, "
    f"undone by Restore); otherwise it patches the system theme file {detect.GRESOURCE}, which a "
    "gnome-shell update replaces and reverts (re-apply needed).\n\n"
    "A faulty theme file could break the login screen; the original is backed up and the "
    "new file is verified before it is installed.\n\n"
    "Recovery if login breaks: Ctrl+Alt+F3, log in, run "
    "'sudo apt install --reinstall gnome-shell-common'.\n\nContinue?"
)
LOCK_NOTE = {
    "gnome": "The GNOME lock screen shows a blurred version of the desktop wallpaper; "
             "it has no separate image.",
    "xfce": "The XFCE lock screen (xfce4-screensaver) shows the desktop wallpaper; "
            "it has no separate image.",
}

LOCK_RESULT = {
    "gnome": "lock screen shows the desktop wallpaper, blurred",
    "xfce": "lock screen shows the desktop wallpaper",
}
LOCK_EXT_RESULT = "set in GNOME Shell extension {}"
DESKTOP_METHOD = {"gnome": "GNOME wallpaper (org.gnome.desktop.background)", "xfce": "XFCE desktop (xfconf)"}
LOGIN_NOTE = "The login screen change is visible after logout or reboot."


@dataclass
class Result:
    target: str
    status: str
    reason: str = ""

    def line(self) -> str:
        return f"{self.target}: {self.status}" + (f" ({self.reason})" if self.reason else "")


@dataclass
class TargetStatus:
    target: str
    available: bool
    reason: str = ""             # why unavailable; "" when available
    method: str = ""             # how this target is changed, human-readable
    current: str | None = None   # image file shown now; None when unknown or embedded
    restorable: bool = False     # a backup exists, so restore would change something
    setup_required: bool = False  # blocked only by the missing/outdated root helper (install_system fixes it)


@dataclass
class SetupState:
    installed: bool       # root helper installed, secure and current
    can_install: bool     # install_system() can run (source tree with install.sh + pkexec)
    can_uninstall: bool   # uninstall_system() can run
    reason: str = ""      # why not installed / why it cannot be (un)installed; "" when all fine


@dataclass
class SetupResult:
    ok: bool
    message: str = ""
    needs_discard_login: bool = False  # retry install_system(discard_login=True) after asking the user


EXIT_NEEDS_DISCARD_LOGIN = 4  # install.sh: old login image cannot be kept
SETUP_CANCELLED = "Administrator authorization was cancelled or denied; nothing was changed."


# confirm(message) -> True (yes), False (declined), None (cannot ask)
ConfirmFn = Callable[[str], "bool | None"]


def notes(results: list) -> list:
    """Follow-up hints shown by both front ends."""
    return [LOGIN_NOTE] if any(r.target == "login" and r.status == APPLIED for r in results) else []


def _consent(action: str, target: str, env: detect.Environment, selected: set,
             separate_lock: bool) -> str | None:
    if target == "lock" and not separate_lock and "desktop" not in selected:
        verb = "set" if action == "apply" else "restore"
        return f"{LOCK_NOTE[env.de]}\n\nThe lock target will {verb} the desktop wallpaper. Continue?"
    if target == "login" and action == "apply" and env.dm == "gdm3":
        return GDM_WARNING
    return None


def _run_desktop(action: str, de: str, copy: str | None) -> tuple:
    try:
        if action == "apply":
            desktop.apply(de, copy)
            return APPLIED, ""
        return (APPLIED, "") if desktop.restore(de) else (SKIPPED, "nothing to restore")
    except Exception as e:
        return FAILED, str(e) or type(e).__name__


def _run_lock(action: str, env: detect.Environment, copy: str | None) -> tuple:
    try:
        if action == "apply":
            desktop.apply_lock(env.de, env.lock_provider, copy, env.lock_provider_dir)
            return APPLIED, LOCK_EXT_RESULT.format(env.lock_provider)
        return (APPLIED, "") if desktop.restore_lock(env.de, env.lock_provider_dir) else (SKIPPED, "nothing to restore")
    except Exception as e:
        return FAILED, str(e) or type(e).__name__


def _lock_backup(env: detect.Environment) -> bool:
    return env.de == "gnome" and desktop.has_backup("gnome-lock")


def _run_login(action: str, dm: str, copy: str | None) -> tuple:
    prefix = {"gdm3": "gdm", "lightdm": "lightdm"}[dm]
    try:
        return system.run_helper(f"{prefix}-{action}", copy)
    except Exception as e:
        return FAILED, str(e) or type(e).__name__


def _cleanup(env: detect.Environment):
    if env.de not in ("gnome", "xfce"):
        return
    try:
        keep = desktop.current_paths(env.de, env.lock_provider_dir)
    except Exception:
        return
    image.cleanup_copies(keep)


def run(action: str, image_path: str | None, targets, confirm: ConfirmFn,
        env: detect.Environment | None = None) -> list:
    """Apply or restore the selected targets; raises ImageError before any change."""
    if action not in ("apply", "restore"):
        raise ValueError(f"unknown action {action}")
    selected = set(targets)
    if not selected <= set(TARGETS):
        raise ValueError(f"unknown target(s): {', '.join(sorted(selected - set(TARGETS)))}")
    env = env or detect.detect()

    results = {}
    for t in TARGETS:
        if t not in selected:
            results[t] = Result(t, SKIPPED, "not selected")
        elif not env.available(t):
            results[t] = Result(t, SKIPPED, env.unavailable[t])
    pending = [t for t in TARGETS if t not in results]

    copy = login_image = None
    if action == "apply":
        if not image_path:
            raise ImageError("no image given")
        copy = image.prepare(image_path)
        if "login" in pending:
            login_image = image.login_copy(copy)

    # A lock-extension backup is restored even if the extension is no longer active.
    separate_lock = "lock" in pending and (
        env.lock_provider is not None or (action == "restore" and _lock_backup(env)))
    for t in list(pending):
        message = _consent(action, t, env, selected, separate_lock)
        if message is None:
            continue
        answer = confirm(message)
        if answer is not True:
            results[t] = Result(t, SKIPPED, "confirmation required" if answer is None else "declined")
            pending.remove(t)

    # Without a lock-screen extension lock is an alias of desktop: change once, report both.
    separate_lock = separate_lock and "lock" in pending
    wallpaper = [t for t in ("desktop", "lock") if t in pending and not (t == "lock" and separate_lock)]
    if wallpaper:
        status, reason = _run_desktop(action, env.de, copy)
        for t in wallpaper:
            note = LOCK_RESULT[env.de] if t == "lock" and status == APPLIED else reason
            results[t] = Result(t, status, note)
    if separate_lock:
        results["lock"] = Result("lock", *_run_lock(action, env, copy))
    if "login" in pending:
        results["login"] = Result("login", *_run_login(action, env.dm, login_image))

    if copy or wallpaper or separate_lock:
        _cleanup(env)
    return [results[t] for t in TARGETS]


def status(env: detect.Environment | None = None) -> list:
    """Read-only state per target (TargetStatus in TARGETS order) for front ends."""
    env = env or detect.detect()
    images = {"desktop": None, "lock": None}
    if env.de in ("gnome", "xfce"):
        try:
            images = desktop.current_images(env.de, env.lock_provider, env.lock_provider_dir)
        except Exception:
            pass
    wall_backup = env.de in ("gnome", "xfce") and desktop.has_backup(env.de)
    ext_backup = _lock_backup(env)
    if env.lock_provider:
        lock_method, lock_backup = f"GNOME Shell extension {env.lock_provider}", ext_backup
    else:
        lock_method, lock_backup = LOCK_RESULT.get(env.de, ""), wall_backup or ext_backup
    try:
        login = system.login_status(env.dm)
    except Exception:
        login = {"method": "", "current": None, "restorable": False}
    rows = {
        "desktop": (DESKTOP_METHOD.get(env.de, ""), images["desktop"], wall_backup),
        "lock": (lock_method, images["lock"], lock_backup),
        "login": (login["method"], login["current"], login["restorable"]),
    }
    out = [TargetStatus(t, env.available(t), env.unavailable.get(t) or "", *rows[t]) for t in TARGETS]
    login_row = out[TARGETS.index("login")]
    if not login_row.available and detect.login_reason(env.dm, check_helper=False) is None:
        problem = system.setup_problem()
        if problem:
            login_row.reason, login_row.setup_required = problem, True
    return out


def setup_state() -> SetupState:
    """Whether the one-time root setup (needed by the login target) is done or can be done."""
    problem = system.setup_problem()
    pkexec = os.access(system.PKEXEC, os.X_OK)
    can_install = pkexec and system.install_script() is not None
    can_uninstall = pkexec and os.path.isdir(system.INSTALL_LIB) and system.install_script(True) is not None
    reason = problem or ""
    if not pkexec:
        reason = (reason + "; " if reason else "") + "pkexec not found (install policykit-1)"
    elif problem and not can_install:
        reason += "; install.sh not found next to this program"
    return SetupState(problem is None, can_install, can_uninstall, reason)


def _setup_result(rc: int, stdout: str, stderr: str) -> SetupResult:
    """Map install.sh/pkexec exit codes to a SetupResult."""
    text = "\n".join(ln.strip() for ln in (stdout + "\n" + stderr).splitlines() if ln.strip())
    errors = "\n".join(ln.strip() for ln in stderr.splitlines() if ln.strip())
    if rc == 0:
        return SetupResult(True, text)
    if rc in (126, 127):
        return SetupResult(False, SETUP_CANCELLED)
    if rc == EXIT_NEEDS_DISCARD_LOGIN:
        return SetupResult(False, errors, needs_discard_login=True)
    return SetupResult(False, errors or f"install.sh exited with {rc}")


def _run_setup(script: str | None, args: list) -> SetupResult:
    if script is None:
        return SetupResult(False, "install.sh not found next to this program")
    if not os.access(system.PKEXEC, os.X_OK):
        return SetupResult(False, "pkexec not found (install policykit-1)")
    try:
        return _setup_result(*system.run_install_script(script, args))
    except subprocess.TimeoutExpired:
        return SetupResult(False, "install.sh timed out")
    except OSError as e:
        return SetupResult(False, f"cannot run pkexec: {e.strerror}")


def install_system(discard_login: bool = False) -> SetupResult:
    """Install/update the root-owned copy via pkexec install.sh. Blocks; call off the UI thread."""
    return _run_setup(system.install_script(), ["--discard-login"] if discard_login else [])


def uninstall_system() -> SetupResult:
    """Remove the root-owned copy via pkexec install.sh --uninstall. Blocks; call off the UI thread."""
    return _run_setup(system.install_script(for_uninstall=True), ["--uninstall"])
