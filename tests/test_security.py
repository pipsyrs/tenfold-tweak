"""Security fixes: installed-helper ownership check, helper action lock, temp names, private copies."""

import io
import os
import unittest
from contextlib import redirect_stderr
from unittest import mock

from tests.helpers import make_image, scratch_dir, write

from tenfold_tweak import detect, image, system
from tenfold_tweak import root_helper as rh


class HelperOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("own-")

    def test_root_owned_chain_is_trusted(self):
        # /etc/hostname: root:root 0644 under root-owned 0755 / and /etc.
        self.assertIsNone(system.helper_problem("/etc/hostname"))

    def test_default_path_is_fixed_install_location(self):
        self.assertEqual(system.HELPER, "/usr/libexec/tenfold-tweaks/root-helper")
        self.assertEqual(rh.INSTALLED_HELPER, system.HELPER)
        self.assertEqual(rh._dump_command("/x")[:3], ["/usr/bin/python3", "-I", system.HELPER])

    def test_missing_helper(self):
        problem = system.helper_problem(os.path.join(self.d, "nope.py"))
        self.assertIn("not installed", problem)
        self.assertIn("sudo apt install", problem)

    def test_user_owned_helper_refused(self):
        path = write(os.path.join(self.d, "root_helper.py"), b"")
        self.assertIn("not root-owned", system.helper_problem(path))

    def test_symlinked_helper_refused(self):
        link = os.path.join(self.d, "link.py")
        os.symlink("/etc/hostname", link)
        self.assertIn("not a regular file", system.helper_problem(link))

    def test_symlinked_parent_refused(self):
        # A symlink anywhere in the chain is refused (here a user-owned one in front of /etc).
        link = os.path.join(self.d, "etc")
        os.symlink("/etc", link)
        self.assertIsNotNone(system.helper_problem(os.path.join(link, "hostname")))

    def test_run_helper_refuses_without_calling_pkexec(self):
        calls = []
        with mock.patch.object(system.subprocess, "run", lambda *a, **k: calls.append(a)), \
                mock.patch.object(system, "HELPER", os.path.join(self.d, "missing.py")):
            status, reason = system.run_helper("gdm-restore")
        self.assertEqual(status, "skipped")
        self.assertIn("sudo apt install", reason)
        self.assertEqual(calls, [])

    def test_detect_reports_install_hint_for_login(self):
        with mock.patch.object(system, "HELPER", os.path.join(self.d, "missing.py")):
            reason = detect.login_reason("gdm3")
        self.assertIn("sudo apt install", reason)


class ActionLockTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("lock-")
        for p in (mock.patch.object(rh, "STATE_DIR", self.d),
                  mock.patch.object(rh, "ensure_state_dir", lambda: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_lock_is_exclusive_and_released(self):
        with rh.action_lock():
            with self.assertRaises(rh.HelperError) as cm:
                with rh.action_lock():
                    pass
            self.assertIn("in progress", str(cm.exception))
        with rh.action_lock():
            pass
        self.assertEqual(os.stat(os.path.join(self.d, "lock")).st_mode & 0o777, 0o600)

    def test_main_refuses_while_another_run_holds_the_lock(self):
        ran = []
        err = io.StringIO()
        with mock.patch.dict(rh.ACTIONS, {"lightdm-restore": (0, lambda: ran.append(1))}), \
                mock.patch.object(rh.os, "geteuid", return_value=0), redirect_stderr(err):
            with rh.action_lock():
                rc = rh.main(["lightdm-restore"])
            rc_free = rh.main(["lightdm-restore"])
        self.assertEqual(rc, rh.EXIT_FAIL)
        self.assertIn("in progress", err.getvalue())
        self.assertEqual(rc_free, rh.EXIT_OK)
        self.assertEqual(ran, [1])


class AtomicWriteTest(unittest.TestCase):
    def test_unique_temp_names_and_no_leftovers(self):
        d = scratch_dir("aw-")
        dest = os.path.join(d, "target")
        stale = write(os.path.join(d, ".target.tmp"), b"other run")  # old fixed name is not touched
        rh.atomic_write(dest, b"one", 0o644, tmp=os.path.join(d, ".target.new"))
        rh.atomic_write(dest, b"two", 0o644)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"two")
        self.assertEqual(sorted(os.listdir(d)), sorted([".target.tmp", "target"]))
        self.assertTrue(os.path.exists(stale))

    def test_failed_write_removes_temp(self):
        d = scratch_dir("aw-")
        with mock.patch.object(rh.os, "replace", side_effect=OSError(28, "full")):
            with self.assertRaises(OSError):
                rh.atomic_write(os.path.join(d, "target"), b"x", 0o644)
        self.assertEqual(os.listdir(d), [])


class PrivateCopiesTest(unittest.TestCase):
    def test_copy_dir_0700_and_files_0600(self):
        d = scratch_dir("priv-")
        os.makedirs(image.COPY_DIR, mode=0o755, exist_ok=True)
        os.chmod(image.COPY_DIR, 0o755)  # pre-existing dir from an older version gets tightened
        copy = image.prepare(make_image(os.path.join(d, "a.png"), 5000, 10))
        login = image.login_copy(copy)
        self.addCleanup(image.cleanup_copies, set())
        self.assertEqual(os.stat(image.COPY_DIR).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(copy).st_mode & 0o777, 0o600)
        self.assertNotEqual(login, copy)
        self.assertEqual(os.stat(login).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
