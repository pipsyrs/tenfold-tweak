"""GNOME via the in-memory GSettings backend; XFCE via a fake xfconf-query."""

import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

from tests import SANDBOX  # noqa: E402

from tenfold_tweak import desktop  # noqa: E402
from tenfold_tweak.desktop import DesktopError  # noqa: E402


def reset_state():
    assert os.environ["GSETTINGS_BACKEND"] == "memory"
    assert desktop.STATE_DIR.startswith(SANDBOX)
    shutil.rmtree(desktop.STATE_DIR, ignore_errors=True)
    s = Gio.Settings.new(desktop.GNOME_SCHEMA)
    for k in desktop.GNOME_KEYS:
        s.reset(k)
    Gio.Settings.sync()


def gnome():
    return Gio.Settings.new(desktop.GNOME_SCHEMA)


class GnomeDesktopTest(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.path = os.path.join(SANDBOX, "wall paper.png")

    def test_apply_sets_light_and_dark(self):
        desktop.apply("gnome", self.path)
        s = gnome()
        uri = Gio.File.new_for_path(self.path).get_uri()
        self.assertEqual(s.get_string("picture-uri"), uri)
        self.assertEqual(s.get_string("picture-uri-dark"), uri)
        self.assertEqual(s.get_string("picture-options"), "zoom")
        self.assertEqual(desktop.current_paths("gnome"), {self.path})

    def test_backup_file_mode_and_defaults_as_null(self):
        desktop.apply("gnome", self.path)
        self.assertEqual(os.stat(desktop.BACKUP_FILE).st_mode & 0o777, 0o600)
        with open(desktop.BACKUP_FILE) as f:
            self.assertEqual(json.load(f)["gnome"], {k: None for k in desktop.GNOME_KEYS})

    def test_restore_resets_defaults(self):
        desktop.apply("gnome", self.path)
        self.assertTrue(desktop.restore("gnome"))
        s = gnome()
        for k in desktop.GNOME_KEYS:
            self.assertIsNone(s.get_user_value(k), k)
        self.assertFalse(os.path.exists(desktop.BACKUP_FILE))

    def test_reapply_keeps_original_then_restore_returns_it(self):
        s = gnome()
        s.set_string("picture-uri", "file:///orig.png")
        Gio.Settings.sync()
        desktop.apply("gnome", self.path)
        desktop.apply("gnome", os.path.join(SANDBOX, "second.png"))
        self.assertTrue(desktop.restore("gnome"))
        s = gnome()
        self.assertEqual(s.get_string("picture-uri"), "file:///orig.png")
        self.assertIsNone(s.get_user_value("picture-uri-dark"))

    def test_restore_without_backup_is_nothing(self):
        gnome().set_string("picture-uri", "file:///mine.png")
        self.assertFalse(desktop.restore("gnome"))
        self.assertEqual(gnome().get_string("picture-uri"), "file:///mine.png")

    def test_failed_write_rolls_back(self):
        gnome().set_string("picture-uri", "file:///orig.png")
        real = desktop._gnome_write
        calls = []

        def flaky(s, values):
            calls.append(values)
            real(s, values)
            if len(calls) == 1:
                raise DesktopError("injected")
        with mock.patch.object(desktop, "_gnome_write", flaky):
            with self.assertRaises(DesktopError):
                desktop.apply("gnome", self.path)
        self.assertEqual(gnome().get_string("picture-uri"), "file:///orig.png")
        self.assertIsNone(gnome().get_user_value("picture-uri-dark"))


class FakeXfconf:
    """Minimal xfconf-query emulation for the xfce4-desktop and xfwm4 channels."""

    def __init__(self, props, workspaces=2):
        self.props = dict(props)
        self.workspaces = workspaces
        self.calls = []
        self.fail_set = set()

    def __call__(self, cmd, **kwargs):
        assert isinstance(cmd, list) and not kwargs.get("shell"), cmd
        self.calls.append(cmd)
        assert cmd[0] == "xfconf-query" and cmd[1] == "-c"
        channel, args = cmd[2], cmd[3:]
        ok = lambda out="": subprocess.CompletedProcess(cmd, 0, out, "")  # noqa: E731
        err = lambda msg: subprocess.CompletedProcess(cmd, 1, "", msg)  # noqa: E731
        if channel == "xfwm4":
            return ok(str(self.workspaces))
        if args == ["-l"]:
            return ok("\n".join(sorted(self.props)))
        prop = args[1]
        if len(args) == 2:
            return ok(self.props[prop]) if prop in self.props else err(f'Property "{prop}" does not exist')
        if args[2] == "-r":
            self.props.pop(prop, None)
            return ok()
        if prop in self.fail_set:
            return err("write refused")
        if args[2] == "-n":
            self.props[prop] = args[-1]
            return ok()
        if args[2] == "-s":
            if prop not in self.props:
                return err("does not exist")
            self.props[prop] = args[3]
            return ok()
        raise AssertionError(cmd)


BASE = "/backdrop/screen0/monitor"
REAL_MONITORS = desktop._xfce_monitors


class XfceDesktopTest(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.original = {
            f"{BASE}eDP-1/workspace0/last-image": "/usr/share/backgrounds/kali.png",
            f"{BASE}eDP-1/workspace0/image-style": "0",
            f"{BASE}eDP-1/workspace1/last-image": "/usr/share/backgrounds/kali.png",
            f"{BASE}eDP-1/workspace1/image-style": "5",
        }
        self.fake = FakeXfconf(self.original)
        for p in (mock.patch.object(desktop.subprocess, "run", self.fake),
                  mock.patch.object(desktop, "_xfce_monitors", lambda: ["eDP-1", "HDMI-1"])):
            p.start()
            self.addCleanup(p.stop)
        self.path = os.path.join(SANDBOX, "wall.png")

    def test_apply_covers_every_monitor_and_workspace(self):
        desktop.apply("xfce", self.path)
        for mon in ("eDP-1", "HDMI-1"):
            for ws in (0, 1):
                key = f"{BASE}{mon}/workspace{ws}/"
                self.assertEqual(self.fake.props[key + "last-image"], self.path, key)
                self.assertEqual(self.fake.props[key + "image-style"], "5", key)
        new_style = [c for c in self.fake.calls if f"{BASE}HDMI-1/workspace0/image-style" in c and "-n" in c]
        self.assertIn("int", new_style[0])

    def test_backup_records_previous_and_absent_values(self):
        desktop.apply("xfce", self.path)
        with open(desktop.BACKUP_FILE) as f:
            saved = json.load(f)["xfce"]
        self.assertEqual(saved[f"{BASE}eDP-1/workspace0/last-image"], "/usr/share/backgrounds/kali.png")
        self.assertEqual(saved[f"{BASE}eDP-1/workspace0/image-style"], "0")
        self.assertIsNone(saved[f"{BASE}HDMI-1/workspace1/last-image"])

    def test_restore_returns_exact_original(self):
        desktop.apply("xfce", self.path)
        desktop.apply("xfce", os.path.join(SANDBOX, "second.png"))
        self.assertTrue(desktop.restore("xfce"))
        self.assertEqual(self.fake.props, self.original)
        self.assertFalse(desktop.restore("xfce"))

    def test_no_props_and_no_monitors_fails_clearly(self):
        self.fake.props.clear()
        with mock.patch.object(desktop, "_xfce_monitors", lambda: []):
            with self.assertRaises(DesktopError) as cm:
                desktop.apply("xfce", self.path)
        self.assertIn("monitors could not be listed", str(cm.exception))

    def test_failed_write_rolls_back(self):
        self.fake.fail_set.add(f"{BASE}HDMI-1/workspace1/last-image")
        with self.assertRaises(DesktopError):
            desktop.apply("xfce", self.path)
        self.assertEqual(self.fake.props, self.original)

    def test_monitor_probe_missing_typelib_falls_back(self):
        with mock.patch.object(desktop, "_monitors_gtk4", side_effect=ValueError("Namespace Gtk not available")), \
                mock.patch.object(desktop, "_monitors_gtk3", return_value=["DP-2"]):
            self.assertEqual(desktop._probe_monitors(), ["DP-2"])

    def test_monitor_enumeration_never_touches_gtk_in_process(self):
        # Regression: core.run runs in a GUI worker thread; GTK must stay in a child process.
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "eDP-1\nHDMI-1\n", "")

        boom = mock.Mock(side_effect=AssertionError("GTK probed in-process"))
        with mock.patch.object(desktop.subprocess, "run", fake_run), \
                mock.patch.object(desktop, "_monitors_gtk4", boom), \
                mock.patch.object(desktop, "_monitors_gtk3", boom):
            self.assertEqual(REAL_MONITORS(), ["eDP-1", "HDMI-1"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:], ["-I", os.path.abspath(desktop.__file__), desktop.MONITORS_ARG])

    def test_monitor_child_failure_gives_empty_list(self):
        failed = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "boom"))
        with mock.patch.object(desktop.subprocess, "run", failed):
            self.assertEqual(REAL_MONITORS(), [])

    def test_unsupported_de(self):
        with self.assertRaises(DesktopError):
            desktop.apply("kde", self.path)


if __name__ == "__main__":
    unittest.main()
