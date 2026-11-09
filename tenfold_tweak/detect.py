"""Desktop environment / display manager detection and target availability."""

import configparser
import glob
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from . import desktop, system
from .system import PKEXEC, PYTHON

TARGETS = ("desktop", "lock", "login")

GRESOURCE = "/usr/share/gnome-shell/gnome-shell-theme.gresource"
DEFAULT_DM_FILE = "/etc/X11/default-display-manager"
DM_SERVICES = {"gdm3": "gdm.service", "lightdm": "lightdm.service"}
LIGHTDM_CONFS = ("/usr/share/lightdm/lightdm.conf.d/*.conf", "/etc/lightdm/lightdm.conf.d/*.conf")
LIGHTDM_MAIN_CONF = "/etc/lightdm/lightdm.conf"
XGREETERS = "/usr/share/xgreeters"


@dataclass
class Environment:
    de: str
    dm: str
    # target -> None when available, else the reason it is not
    unavailable: dict = field(default_factory=dict)
    # GNOME Shell extension that draws its own lock-screen image; None = lock shows the wallpaper
    lock_provider: str | None = None
    lock_provider_dir: str | None = None  # extension dir its settings schema is loaded from

    def available(self, target: str) -> bool:
        return self.unavailable.get(target) is None

    @property
    def available_targets(self) -> list:
        return [t for t in TARGETS if self.available(t)]


def _run(cmd: list) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def detect_de() -> str:
    raw = os.environ.get("XDG_CURRENT_DESKTOP", "")
    parts = [p.strip().upper() for p in raw.split(":")]
    if "GNOME" in parts:
        return "gnome"
    if "XFCE" in parts:
        return "xfce"
    return f"unsupported({raw or 'unknown'})"


def detect_dm() -> str:
    try:
        with open(DEFAULT_DM_FILE, encoding="utf-8") as f:
            name = os.path.basename(f.read().strip())
    except OSError:
        name = ""
    service = _run(["systemctl", "show", "-p", "Id", "--value", "display-manager.service"])
    if name not in DM_SERVICES:
        return f"unsupported({name or service or 'unknown'})"
    if service and service != DM_SERVICES[name]:
        return f"unsupported({name} vs {service})"
    return name


def lightdm_greeter() -> str:
    """Resolve the configured greeter-session to its .desktop file basename."""
    greeter = ""
    files = [f for pattern in LIGHTDM_CONFS for f in sorted(glob.glob(pattern))] + [LIGHTDM_MAIN_CONF]
    for path in files:
        cp = configparser.ConfigParser(strict=False, interpolation=None, allow_no_value=True)
        try:
            cp.read(path, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            continue
        if cp.has_option("Seat:*", "greeter-session"):
            greeter = cp.get("Seat:*", "greeter-session").strip()
    if not greeter or "/" in greeter:
        return ""
    desktop = os.path.join(XGREETERS, greeter + ".desktop")
    if not os.path.exists(desktop):
        return ""
    return os.path.basename(os.path.realpath(desktop))


def xfce_lock_reason() -> str | None:
    if not (shutil.which("xfce4-screensaver") or os.path.exists("/usr/libexec/xfce4-screensaver-dialog")):
        return "unsupported: xfce4-screensaver is not installed"
    if _run(["xfconf-query", "-c", "xfce4-screensaver", "-p", "/lock/enabled"]).lower() == "false":
        return "unsupported: xfce4-screensaver locking is disabled"
    return None


def login_reason(dm: str, check_helper: bool = True) -> str | None:
    if dm == "gdm3":
        if not os.path.isfile(GRESOURCE):
            return f"unsupported: {GRESOURCE} not found"
    elif dm == "lightdm":
        if lightdm_greeter() != "lightdm-gtk-greeter.desktop":
            return "unsupported: LightDM greeter is not lightdm-gtk-greeter"
    else:
        return f"unsupported: display manager {dm}"
    if check_helper:
        problem = system.helper_problem()
        if problem:
            return f"unsupported: {problem}"
        if system.helper_outdated():
            return f"unsupported: installed root helper is outdated; {system.INSTALL_HINT}"
    if not os.access(PKEXEC, os.X_OK):
        return "unsupported: pkexec not found"
    if not os.access(PYTHON, os.X_OK):
        return f"unsupported: {PYTHON} not found"
    return None


def detect() -> Environment:
    de, dm = detect_de(), detect_dm()
    env = Environment(de=de, dm=dm)
    if de in ("gnome", "xfce"):
        env.unavailable["desktop"] = None
        env.unavailable["lock"] = xfce_lock_reason() if de == "xfce" else None
        if de == "gnome":
            try:
                env.lock_provider, env.lock_provider_dir, problem = desktop.gnome_lock_provider()
            except Exception as e:
                problem = f"unsupported: cannot check lock-screen extensions: {e}"
            if problem:
                env.unavailable["lock"] = problem
    else:
        env.unavailable["desktop"] = env.unavailable["lock"] = f"unsupported: desktop environment {de}"
    env.unavailable["login"] = login_reason(dm)
    return env
