"""core.run, system.run_helper (pkexec stubbed) and CLI exit codes."""

import io
import os
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import GdkPixbuf, Gio  # noqa: E402

from tests.helpers import make_image, scratch_dir  # noqa: E402
from tests.test_desktop import gnome, reset_state  # noqa: E402

from tenfold_tweak import cli, core, desktop, detect, image, system  # noqa: E402

GNOME_GDM = detect.Environment("gnome", "gdm3", {"desktop": None, "lock": None, "login": None})
XFCE_LIGHTDM = detect.Environment("xfce", "lightdm", {"desktop": None, "lock": None, "login": None})
UNSUPPORTED = detect.Environment("unsupported(KDE)", "unsupported(sddm)", {
    "desktop": "unsupported: desktop environment unsupported(KDE)",
    "lock": "unsupported: desktop environment unsupported(KDE)",
    "login": "unsupported: display manager unsupported(sddm)"})


REAL_RUN_HELPER = system.run_helper


def by_target(results):
    return {r.target: (r.status, r.reason) for r in results}


class Base(unittest.TestCase):
    def setUp(self):
        reset_state()
        shutil.rmtree(image.COPY_DIR, ignore_errors=True)
        self.d = scratch_dir("core-")
        self.img = make_image(os.path.join(self.d, "pic.png"), 40, 30)
        self.helper = mock.MagicMock(return_value=("applied", ""))
        p = mock.patch.object(core.system, "run_helper", self.helper)
        p.start()
        self.addCleanup(p.stop)


class CoreTest(Base):
    def test_desktop_apply_never_asks_for_admin(self):
        res = by_target(core.run("apply", self.img, ["desktop"], lambda m: True, GNOME_GDM))
        self.assertEqual(res["desktop"], ("applied", ""))
        self.assertEqual(res["login"], ("skipped", "not selected"))
        self.helper.assert_not_called()
        self.assertTrue(gnome().get_string("picture-uri").startswith("file://" + image.COPY_DIR))

    def test_lock_alone_needs_confirmation_and_sets_desktop(self):
        asked = []
        res = by_target(core.run("apply", self.img, ["lock"], lambda m: asked.append(m) or True, GNOME_GDM))
        self.assertIn("blurred", asked[0])
        self.assertEqual(res["lock"][0], "applied")
        self.assertIn("blurred", res["lock"][1])
        self.assertEqual(res["desktop"], ("skipped", "not selected"))
        self.helper.assert_not_called()

    def test_lock_declined_changes_nothing(self):
        res = by_target(core.run("apply", self.img, ["lock"], lambda m: False, GNOME_GDM))
        self.assertEqual(res["lock"], ("skipped", "declined"))
        self.assertIsNone(gnome().get_user_value("picture-uri"))

    def test_desktop_and_lock_apply_once_and_lock_informs(self):
        with mock.patch.object(core.desktop, "apply", wraps=desktop.apply) as spy:
            res = by_target(core.run("apply", self.img, ["desktop", "lock"], lambda m: None, GNOME_GDM))
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(res["desktop"][0], "applied")
        self.assertEqual(res["lock"], ("applied", core.LOCK_RESULT["gnome"]))

    def test_gdm_warning_shown_and_decline_skips_login(self):
        asked = []
        res = by_target(core.run("apply", self.img, ["login"], lambda m: asked.append(m) or False, GNOME_GDM))
        self.assertIn("system theme file", asked[0])
        self.assertIn("administrator rights", asked[0])
        self.assertIn("gnome-shell update", asked[0])
        self.assertEqual(res["login"], ("skipped", "declined"))
        self.helper.assert_not_called()

    def test_gdm_login_applied_after_consent(self):
        res = by_target(core.run("apply", self.img, ["login"], lambda m: True, GNOME_GDM))
        self.assertEqual(res["login"], ("applied", ""))
        action, path = self.helper.call_args.args
        self.assertEqual(action, "gdm-apply")
        self.assertTrue(os.path.isabs(path))

    def test_lightdm_login_no_extra_confirmation(self):
        asked = []
        core.run("apply", self.img, ["login"], lambda m: asked.append(m) or True, XFCE_LIGHTDM)
        self.assertEqual(asked, [])
        self.assertEqual(self.helper.call_args.args[0], "lightdm-apply")

    def test_large_login_image_downscaled_then_cleaned(self):
        big = make_image(os.path.join(self.d, "big.png"), 5000, 10)
        seen = {}

        def helper(action, path):
            seen["path"] = path
            seen["dims"] = GdkPixbuf.Pixbuf.get_file_info(path)[1:]
            return "applied", ""
        self.helper.side_effect = helper
        core.run("apply", big, ["login"], lambda m: True, GNOME_GDM)
        self.assertTrue(os.path.basename(seen["path"]).startswith("login-"))
        self.assertLessEqual(max(seen["dims"]), 4096)
        self.assertFalse(os.path.exists(seen["path"]))

    def test_invalid_image_raises_before_any_change(self):
        with self.assertRaises(core.ImageError):
            core.run("apply", os.path.join(self.d, "missing.png"), ["desktop", "login"], lambda m: True, GNOME_GDM)
        self.helper.assert_not_called()
        self.assertFalse(os.path.exists(desktop.BACKUP_FILE))
        self.assertIsNone(gnome().get_user_value("picture-uri"))

    def test_unsupported_env_skips_all_with_reason(self):
        res = by_target(core.run("apply", self.img, list(core.TARGETS), lambda m: True, UNSUPPORTED))
        for t in core.TARGETS:
            self.assertEqual(res[t][0], "skipped")
            self.assertIn("unsupported", res[t][1])
        self.helper.assert_not_called()

    def test_restore_nothing_to_restore(self):
        self.helper.return_value = ("skipped", "nothing to restore")
        res = by_target(core.run("restore", None, ["desktop", "login"], lambda m: True, GNOME_GDM))
        self.assertEqual(res["desktop"], ("skipped", "nothing to restore"))
        self.assertEqual(res["login"], ("skipped", "nothing to restore"))
        self.assertEqual(self.helper.call_args.args, ("gdm-restore", None))

    def test_desktop_apply_then_restore(self):
        core.run("apply", self.img, ["desktop"], lambda m: True, GNOME_GDM)
        res = by_target(core.run("restore", None, ["desktop"], lambda m: True, GNOME_GDM))
        self.assertEqual(res["desktop"], ("applied", ""))
        self.assertIsNone(gnome().get_user_value("picture-uri"))
        self.assertEqual([n for n in os.listdir(image.COPY_DIR) if n.startswith("wallpaper-")], [])

    def test_login_note_only_when_login_applied(self):
        res = core.run("apply", self.img, ["login"], lambda m: True, GNOME_GDM)
        self.assertEqual(core.notes(res), [core.LOGIN_NOTE])
        res = core.run("apply", self.img, ["desktop"], lambda m: True, GNOME_GDM)
        self.assertEqual(core.notes(res), [])


class PkexecTest(Base):
    """system.run_helper with subprocess.run stubbed: nothing real is executed."""

    def setUp(self):
        super().setUp()
        self.helper.side_effect = REAL_RUN_HELPER
        self.cmds = []
        self.rc, self.stderr = 0, ""

        def fake_run(cmd, **kwargs):
            self.cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, self.rc, "", self.stderr)
        p = mock.patch.object(system.subprocess, "run", fake_run)
        p.start()
        self.addCleanup(p.stop)
        # Pretend the root-owned install exists; the ownership check itself is in test_security.
        p = mock.patch.object(system, "helper_problem", lambda path=None: None)
        p.start()
        self.addCleanup(p.stop)

    def test_command_shape(self):
        core.run("apply", self.img, ["login"], lambda m: True, GNOME_GDM)
        cmd = self.cmds[0]
        self.assertEqual(cmd[:5], [system.PKEXEC, system.PYTHON, "-I", system.HELPER, "gdm-apply"])
        self.assertTrue(cmd[5].startswith(image.COPY_DIR))

    def test_cancel_126_and_127_change_nothing(self):
        for rc in (126, 127):
            reset_state()
            self.rc = rc
            res = by_target(core.run("apply", self.img, ["login"], lambda m: True, GNOME_GDM))
            self.assertEqual(res["login"], ("skipped", "authorization cancelled"), rc)
            self.assertFalse(os.path.exists(desktop.BACKUP_FILE))
            self.assertIsNone(gnome().get_user_value("picture-uri"))
            self.assertEqual(os.listdir(image.COPY_DIR), [])

    def test_cancel_does_not_affect_desktop_result(self):
        self.rc = 126
        res = by_target(core.run("apply", self.img, ["desktop", "login"], lambda m: True, GNOME_GDM))
        self.assertEqual(res["desktop"], ("applied", ""))
        self.assertEqual(res["login"], ("skipped", "authorization cancelled"))

    def test_helper_failure_reason_is_last_stderr_line(self):
        self.rc, self.stderr = 1, "warning\nbackup checksum mismatch; run: sudo apt ...\n"
        res = by_target(core.run("restore", None, ["login"], lambda m: True, GNOME_GDM))
        self.assertEqual(res["login"], ("failed", "backup checksum mismatch; run: sudo apt ..."))

    def test_exit_3_nothing_to_restore(self):
        self.rc = 3
        res = by_target(core.run("restore", None, ["login"], lambda m: True, XFCE_LIGHTDM))
        self.assertEqual(res["login"], ("skipped", "nothing to restore"))
        self.assertEqual(self.cmds[0][-1], "lightdm-restore")

    def test_pkexec_missing(self):
        with mock.patch.object(system.subprocess, "run", side_effect=FileNotFoundError(2, "No such file")):
            self.assertEqual(REAL_RUN_HELPER("gdm-restore")[0], "failed")

    def test_relative_image_refused(self):
        with self.assertRaises(ValueError):
            REAL_RUN_HELPER("gdm-apply", "rel.png")


class CliTest(Base):
    def run_cli(self, argv, env=GNOME_GDM, environ=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.detect, "detect", return_value=env), \
                mock.patch.dict(os.environ, environ or {}), redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_apply_ok_exit_0_with_report(self):
        rc, out, _ = self.run_cli(["apply", self.img, "--desktop"])
        self.assertEqual(rc, 0)
        self.assertIn("desktop-env: gnome", out)
        self.assertIn("display-manager: gdm3", out)
        self.assertIn("desktop: applied", out)
        self.assertIn("login: skipped (not selected)", out)

    def test_failure_exit_1(self):
        with mock.patch.object(core.desktop, "apply", side_effect=desktop.DesktopError("xfconf broke")):
            rc, out, _ = self.run_cli(["apply", self.img, "--desktop"])
        self.assertEqual(rc, 1)
        self.assertIn("desktop: failed (xfconf broke)", out)

    def test_invalid_image_exit_2(self):
        bad = os.path.join(self.d, "bad.png")
        with open(bad, "w") as f:
            f.write("not an image")
        rc, _, err = self.run_cli(["apply", bad, "--desktop"])
        self.assertEqual(rc, 2)
        self.assertIn("supported: PNG, JPEG", err)
        self.assertIsNone(gnome().get_user_value("picture-uri"))

    def test_no_target_flag_exit_2(self):
        rc, _, err = self.run_cli(["apply", self.img])
        self.assertEqual(rc, 2)
        self.assertIn("select at least one", err)

    def test_no_args_no_display_exit_2(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISPLAY", None)
            os.environ.pop("WAYLAND_DISPLAY", None)
            rc, _, err = self.run_cli([])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_login_without_tty_or_yes_skipped(self):
        rc, out, _ = self.run_cli(["apply", self.img, "--login"])
        self.assertEqual(rc, 0)
        self.assertIn("login: skipped (confirmation required)", out)
        self.helper.assert_not_called()

    def test_yes_applies_login_and_prints_note(self):
        rc, out, _ = self.run_cli(["apply", self.img, "--login", "--yes"])
        self.assertEqual(rc, 0)
        self.assertIn("login: applied", out)
        self.assertIn(core.LOGIN_NOTE, out)

    def test_all_on_unsupported_env_reports_skips(self):
        rc, out, _ = self.run_cli(["restore", "--all"], env=UNSUPPORTED)
        self.assertEqual(rc, 0)
        self.assertIn("login: skipped (unsupported: display manager unsupported(sddm))", out)

    def test_restore_nothing_exit_0(self):
        rc, out, _ = self.run_cli(["restore", "--desktop"])
        self.assertEqual(rc, 0)
        self.assertIn("desktop: skipped (nothing to restore)", out)


if __name__ == "__main__":
    unittest.main()
