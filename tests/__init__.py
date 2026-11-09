"""Test sandbox: in-memory GSettings, XDG dirs in a temp dir, and a guard against real system commands."""

import atexit
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SANDBOX = tempfile.mkdtemp(prefix="tenfold-tests-")
atexit.register(shutil.rmtree, SANDBOX, True)

# Must be set before tenfold_tweak (and Gio) is imported.
os.environ["GSETTINGS_BACKEND"] = "memory"
os.environ["XDG_DATA_HOME"] = os.path.join(SANDBOX, "data")
os.environ["XDG_STATE_HOME"] = os.path.join(SANDBOX, "state")
os.environ["XDG_CONFIG_HOME"] = os.path.join(SANDBOX, "config")
os.environ.pop("PKEXEC_UID", None)
os.environ.pop("SUDO_UID", None)

FORBIDDEN = {"pkexec", "sudo", "gsettings", "dconf", "xfconf-query", "xfdesktop", "dpkg-divert"}
_real_run = subprocess.run


def _guarded_run(cmd, *args, **kwargs):
    first = cmd[0] if isinstance(cmd, (list, tuple)) else str(cmd).split()[0]
    if os.path.basename(str(first)) in FORBIDDEN:
        raise AssertionError(f"test tried to run a real system command: {cmd}")
    return _real_run(cmd, *args, **kwargs)


subprocess.run = _guarded_run
