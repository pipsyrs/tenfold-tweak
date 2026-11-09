"""Lock-screen extension routing: restore by backup, resolved extension dir, unusable extension."""

import unittest
from unittest import mock

from tests.helpers import make_image, scratch_dir  # noqa: F401  (sandbox env set by tests/__init__)
from tests.test_desktop import reset_state

from tenfold_tweak import core, desktop, detect

UUID = next(iter(desktop.LOCK_EXTENSIONS))
EXT_DIR = "/opt/ext/" + UUID


def env(provider=None, ext_dir=None, lock_reason=None):
    return detect.Environment("gnome", "gdm3", {"desktop": None, "lock": lock_reason, "login": None},
                              lock_provider=provider, lock_provider_dir=ext_dir)


def no_confirm(message):
    raise AssertionError(f"unexpected prompt: {message}")


class RestoreByBackupTest(unittest.TestCase):
    """Review item 3: a gnome-lock backup is restored even when the extension is gone."""

    def setUp(self):
        reset_state()
        self.addCleanup(reset_state)

    def test_restore_lock_uses_extension_backup_without_provider(self):
        with mock.patch.object(desktop, "has_backup", lambda s: s == "gnome-lock"), \
                mock.patch.object(desktop, "restore_lock", return_value=True) as restore_lock, \
                mock.patch.object(desktop, "restore", side_effect=AssertionError("desktop restored")), \
                mock.patch.object(desktop, "current_paths", return_value=set()):
            res = {r.target: r for r in core.run("restore", None, ["lock"], no_confirm, env())}
        self.assertEqual(res["lock"].status, core.APPLIED)
        restore_lock.assert_called_once_with("gnome", None)

    def test_restore_lock_without_any_extension_backup_keeps_alias(self):
        with mock.patch.object(desktop, "has_backup", lambda s: False), \
                mock.patch.object(desktop, "restore_lock", side_effect=AssertionError("lock routed")), \
                mock.patch.object(desktop, "restore", return_value=False):
            res = {r.target: r for r in core.run("restore", None, ["lock"], lambda m: True, env())}
        self.assertEqual(res["lock"].status, core.SKIPPED)

    def test_status_reports_lock_restorable_from_extension_backup(self):
        with mock.patch.object(desktop, "has_backup", lambda s: s == "gnome-lock"), \
                mock.patch.object(desktop, "current_images", return_value={"desktop": None, "lock": None}), \
                mock.patch.object(core.system, "login_status",
                                  return_value={"method": "", "current": None, "restorable": False}):
            rows = {r.target: r for r in core.status(env())}
        self.assertTrue(rows["lock"].restorable)
        self.assertFalse(rows["desktop"].restorable)


class ExtensionDirTest(unittest.TestCase):
    """Review item 4: the dir resolved at detection is used for every settings lookup."""

    def setUp(self):
        reset_state()
        self.addCleanup(reset_state)
        self.img = make_image(scratch_dir() + "/lock.png")

    def test_apply_and_restore_pass_resolved_dir(self):
        with mock.patch.object(desktop, "_gnome_lock_apply") as apply_, \
                mock.patch.object(desktop, "_gnome_lock_restore", return_value=True) as restore_, \
                mock.patch.object(desktop, "current_paths", return_value=set()) as paths:
            core.run("apply", self.img, ["lock"], no_confirm, env(UUID, EXT_DIR))
            core.run("restore", None, ["lock"], no_confirm, env(UUID, EXT_DIR))
        self.assertEqual(apply_.call_args.args[0], UUID)
        self.assertEqual(apply_.call_args.args[2], EXT_DIR)
        restore_.assert_called_once_with(EXT_DIR)
        for call in paths.call_args_list:
            self.assertEqual(call.args, ("gnome", EXT_DIR))

    def test_current_images_uses_resolved_dir(self):
        with mock.patch.object(desktop, "_lock_settings", return_value=None) as lookup:
            images = desktop.current_images("gnome", UUID, EXT_DIR)
        lookup.assert_called_once_with(UUID, EXT_DIR)
        self.assertIsNone(images["lock"])  # unreadable extension: not the wallpaper

    def test_restore_prefers_dir_saved_in_backup(self):
        values = {k: None for k in desktop.GNOME_KEYS}
        desktop.save_backup({"gnome-lock": {"extension": UUID, "dir": "/saved/dir", "values": values}})
        with mock.patch.object(desktop, "_lock_settings", return_value=None) as lookup:
            with self.assertRaises(desktop.DesktopError):
                desktop.restore_lock("gnome", EXT_DIR)
        lookup.assert_called_once_with(UUID, "/saved/dir")


class UnusableExtensionTest(unittest.TestCase):
    """Review item 5: active extension with an unloadable schema makes lock unavailable."""

    def setUp(self):
        reset_state()
        self.addCleanup(reset_state)

    def test_provider_reports_problem_when_schema_missing(self):
        with mock.patch.object(desktop, "_extension_info", return_value={"state": desktop.EXT_ACTIVE,
                                                                         "path": EXT_DIR}), \
                mock.patch.object(desktop, "_lock_settings", return_value=None):
            uuid, ext_dir, problem = desktop.gnome_lock_provider()
        self.assertEqual((uuid, ext_dir), (UUID, EXT_DIR))
        self.assertIn("cannot be loaded", problem)

    def test_inactive_extension_is_no_provider(self):
        with mock.patch.object(desktop, "_extension_info", return_value={"state": 2.0}):
            self.assertEqual(desktop.gnome_lock_provider(), (None, None, None))

    def detect_with(self, **provider):
        with mock.patch.object(detect, "detect_de", return_value="gnome"), \
                mock.patch.object(detect, "detect_dm", return_value="gdm3"), \
                mock.patch.object(detect, "login_reason", return_value=None), \
                mock.patch.object(detect.desktop, "gnome_lock_provider", **provider):
            return detect.detect()

    def test_detect_marks_lock_unavailable_instead_of_alias(self):
        e = self.detect_with(return_value=(UUID, EXT_DIR, "unsupported: broken extension"))
        self.assertFalse(e.available("lock"))
        self.assertEqual(e.unavailable["lock"], "unsupported: broken extension")
        self.assertEqual((e.lock_provider, e.lock_provider_dir), (UUID, EXT_DIR))
        with mock.patch.object(desktop, "apply", side_effect=AssertionError("desktop changed")):
            res = {r.target: r for r in core.run("apply", make_image(scratch_dir() + "/x.png"),
                                                 ["lock"], no_confirm, e)}
        self.assertEqual(res["lock"].status, core.SKIPPED)
        self.assertEqual(res["lock"].reason, "unsupported: broken extension")

    def test_detect_error_is_reported_not_swallowed(self):
        e = self.detect_with(side_effect=RuntimeError("dbus exploded"))
        self.assertFalse(e.available("lock"))
        self.assertIn("dbus exploded", e.unavailable["lock"])

    def test_detect_healthy_extension(self):
        e = self.detect_with(return_value=(UUID, EXT_DIR, None))
        self.assertTrue(e.available("lock"))
        self.assertEqual(e.lock_provider_dir, EXT_DIR)


if __name__ == "__main__":
    unittest.main()
