"""./tenfold-tweaks-gui: dependency checks and a single exec into the GUI (python3/zenity stubbed)."""

import os
import shutil
import subprocess
import unittest

from tests.helpers import scratch_dir

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, "tenfold-tweaks-gui")

# python3 stub: fails the import check named in $STUB_MISSING, logs the final exec.
PYTHON_STUB = """#!/bin/sh
if [ "$1" = "-I" ]; then
  case "$*" in *"$STUB_MISSING"*) [ -n "$STUB_MISSING" ] && exit 1;; esac; exit 0
fi
echo "python3 $*" >> "$STUB_LOG"
"""
NOTICE_STUB = '#!/bin/sh\necho "${0##*/} $*" >> "$STUB_LOG"\n'


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.base = scratch_dir("launcher-")
        self.bin = os.path.join(self.base, "bin")
        self.log = os.path.join(self.base, "log")
        os.makedirs(self.bin)
        open(self.log, "w").close()
        for tool in ("dirname", "readlink"):
            os.symlink(shutil.which(tool, path="/usr/bin:/bin"), os.path.join(self.bin, tool))
        self.stub("zenity", NOTICE_STUB)
        self.stub("notify-send", NOTICE_STUB)
        self.stub("python3", PYTHON_STUB)

    def stub(self, name, text):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)

    def launch(self, display=True, **extra):
        env = {"PATH": self.bin, "STUB_LOG": self.log}
        if display:
            env["WAYLAND_DISPLAY"] = "wayland-0"
        env.update(extra)
        r = subprocess.run(["/bin/sh", LAUNCHER], env=env, capture_output=True, text=True, timeout=10)
        with open(self.log) as f:
            r.calls = f.read().splitlines()
        return r

    def test_all_deps_present_execs_gui_once(self):
        r = self.launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.calls, [f"python3 {ROOT}/tenfold-tweaks gui"])

    def test_no_display_fails_with_cli_hint_and_notice(self):
        r = self.launch(display=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no graphical display", r.stderr)
        self.assertTrue(r.calls and r.calls[0].startswith("zenity --error"), r.calls)

    def test_missing_python3(self):
        os.unlink(os.path.join(self.bin, "python3"))
        r = self.launch()
        self.assertEqual(r.returncode, 1)
        self.assertIn("sudo apt install python3", r.stderr)

    def test_missing_pygobject(self):
        r = self.launch(STUB_MISSING="GdkPixbuf")
        self.assertEqual(r.returncode, 1)
        self.assertIn("python3-gi", r.stderr)

    def test_missing_gtk4(self):
        r = self.launch(STUB_MISSING='"Gtk", "4.0"')
        self.assertEqual(r.returncode, 1)
        self.assertIn("gir1.2-gtk-4.0", r.stderr)
        self.assertFalse(any(c.startswith("python3 ") for c in r.calls), "GUI started anyway")

    def test_falls_back_to_notify_send(self):
        os.unlink(os.path.join(self.bin, "zenity"))
        r = self.launch(display=False)
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.calls and r.calls[0].startswith("notify-send"), r.calls)

    def test_real_dependency_checks_pass_here(self):
        """The launcher's own import checks succeed with the real python3 on this machine."""
        for code in ('import gi; gi.require_version("Gio", "2.0"); gi.require_version("GdkPixbuf", "2.0"); '
                     'from gi.repository import Gio, GdkPixbuf',
                     'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk'):
            with open(LAUNCHER) as f:
                self.assertIn(code, f.read())
            r = subprocess.run(["python3", "-I", "-c", code], capture_output=True)
            if r.returncode:
                self.skipTest("GTK 4 bindings not installed")


if __name__ == "__main__":
    unittest.main()
