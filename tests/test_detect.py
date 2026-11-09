import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests.helpers import scratch_dir, write

from tenfold_tweak import cli, detect


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("detect-")
        self.dm_file = os.path.join(self.d, "default-display-manager")
        self.gresource = write(os.path.join(self.d, "theme.gresource"), b"x")
        self.xgreeters = os.path.join(self.d, "xgreeters")
        self.conf_d = os.path.join(self.d, "lightdm.conf.d")
        write(os.path.join(self.xgreeters, "lightdm-gtk-greeter.desktop"), b"[Desktop Entry]\n")
        write(os.path.join(self.xgreeters, "slick-greeter.desktop"), b"[Desktop Entry]\n")
        self.service = ""
        self.lock_enabled = "true"
        patches = [
            mock.patch.object(detect, "DEFAULT_DM_FILE", self.dm_file),
            mock.patch.object(detect, "GRESOURCE", self.gresource),
            mock.patch.object(detect, "XGREETERS", self.xgreeters),
            mock.patch.object(detect, "LIGHTDM_CONFS", (os.path.join(self.conf_d, "*.conf"),)),
            mock.patch.object(detect, "LIGHTDM_MAIN_CONF", os.path.join(self.d, "lightdm.conf")),
            mock.patch.object(detect, "PKEXEC", sys.executable),
            mock.patch.object(detect, "PYTHON", sys.executable),
            mock.patch.object(detect, "_run", self._fake_run),
            mock.patch.object(detect.shutil, "which", lambda name: "/usr/bin/" + name),
            mock.patch.object(detect.system, "helper_problem", lambda path=None: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _fake_run(self, cmd):
        if cmd[0] == "systemctl":
            return self.service
        if cmd[0] == "xfconf-query":
            return self.lock_enabled
        raise AssertionError(cmd)

    def env(self, desktop, dm_path, service, greeter=None):
        write(self.dm_file, (dm_path + "\n").encode())
        self.service = service
        if greeter:
            write(os.path.join(self.conf_d, "50-kali.conf"),
                  f"[Seat:*]\ngreeter-session={greeter}\n".encode())
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": desktop}):
            return detect.detect()

    def test_gnome_gdm3_all_targets_available(self):
        env = self.env("GNOME", "/usr/sbin/gdm3", "gdm.service")
        self.assertEqual((env.de, env.dm), ("gnome", "gdm3"))
        self.assertEqual(env.available_targets, ["desktop", "lock", "login"])

    def test_gnome_variant_desktop_string(self):
        env = self.env("ubuntu:GNOME", "/usr/sbin/gdm3", "gdm.service")
        self.assertEqual(env.de, "gnome")

    def test_xfce_lightdm_gtk_greeter_all_available(self):
        env = self.env("XFCE", "/usr/sbin/lightdm", "lightdm.service", "lightdm-gtk-greeter")
        self.assertEqual((env.de, env.dm), ("xfce", "lightdm"))
        self.assertEqual(env.available_targets, ["desktop", "lock", "login"])

    def test_lightdm_other_greeter_login_unsupported(self):
        env = self.env("XFCE", "/usr/sbin/lightdm", "lightdm.service", "slick-greeter")
        self.assertFalse(env.available("login"))
        self.assertIn("lightdm-gtk-greeter", env.unavailable["login"])
        self.assertTrue(env.available("desktop"))

    def test_xfce_without_screensaver_lock_unsupported(self):
        with mock.patch.object(detect.shutil, "which", lambda name: None), \
                mock.patch.object(detect.os.path, "exists", lambda p: False if "screensaver" in p else os.path.lexists(p)):
            env = self.env("XFCE", "/usr/sbin/lightdm", "lightdm.service", "lightdm-gtk-greeter")
        self.assertIn("xfce4-screensaver", env.unavailable["lock"])
        self.assertTrue(env.available("desktop"))

    def test_xfce_lock_disabled_unsupported(self):
        self.lock_enabled = "false"
        env = self.env("XFCE", "/usr/sbin/lightdm", "lightdm.service", "lightdm-gtk-greeter")
        self.assertIn("disabled", env.unavailable["lock"])

    def test_unsupported_de_and_dm_named(self):
        env = self.env("KDE", "/usr/bin/sddm", "sddm.service")
        self.assertEqual(env.de, "unsupported(KDE)")
        self.assertEqual(env.dm, "unsupported(sddm)")
        self.assertEqual(env.available_targets, [])
        for t in detect.TARGETS:
            self.assertTrue(env.unavailable[t].startswith("unsupported"), t)

    def test_dm_file_and_running_service_disagree(self):
        env = self.env("GNOME", "/usr/sbin/gdm3", "lightdm.service")
        self.assertTrue(env.dm.startswith("unsupported"))
        self.assertFalse(env.available("login"))
        self.assertTrue(env.available("desktop"))

    def test_gdm_without_theme_file_login_unsupported(self):
        os.unlink(self.gresource)
        env = self.env("GNOME", "/usr/sbin/gdm3", "gdm.service")
        self.assertIn("not found", env.unavailable["login"])

    def test_cli_detect_reports_de_dm_and_targets(self):
        fake = detect.Environment("xfce", "unsupported(sddm)",
                                  {"desktop": None, "lock": None, "login": "unsupported: display manager x"})
        out = io.StringIO()
        with mock.patch.object(cli.detect, "detect", return_value=fake), redirect_stdout(out):
            rc = cli.main(["detect"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("desktop-env: xfce", text)
        self.assertIn("display-manager: unsupported(sddm)", text)
        self.assertIn("target login: unavailable (unsupported: display manager x)", text)


if __name__ == "__main__":
    unittest.main()
