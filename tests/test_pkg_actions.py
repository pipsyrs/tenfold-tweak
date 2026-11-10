"""pkg-* maintainer-script actions, DPKG_ROOT/DPKG_ADMINDIR handling and the runtime dpkg lock.

Everything runs as a normal user against scratch paths; dpkg-divert is a fake or a stubbed subprocess.
"""

import io
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests.helpers import scratch_dir, write
from tests.test_gdm_kali import FakeDivert, needle
from tests.test_root_helper import STOCK, Sandbox, read

from tenfold_tweak import root_helper as rh

MAINT = {"DPKG_MAINTSCRIPT_PACKAGE": "tenfold-tweaks", "DPKG_MAINTSCRIPT_NAME": "prerm",
         "DPKG_ROOT": "", "DPKG_ADMINDIR": "/var/lib/dpkg"}


def keep_rooted_state(test):
    """Undo use_dpkg_root() after the test."""
    p = mock.patch.multiple(rh, _dpkg_root=rh._dpkg_root, _dpkg_admindir=rh._dpkg_admindir,
                            **{n: getattr(rh, n) for n in rh.ROOTED_PATHS})
    p.start()
    test.addCleanup(p.stop)


def run_main(argv, env, euid=0):
    err, out = io.StringIO(), io.StringIO()
    clean = {k: v for k, v in os.environ.items() if not k.startswith("DPKG_") and k != "PKEXEC_UID"}
    with mock.patch.dict(os.environ, {**clean, **env}, clear=True), \
            mock.patch.object(rh.os, "geteuid", return_value=euid), redirect_stderr(err), redirect_stdout(out):
        rc = rh.main(argv)
    return rc, out.getvalue(), err.getvalue()


class PkgDivert(FakeDivert):
    """FakeDivert plus --listpackage and a pre-rename local diversion."""

    legacy = False
    owner = "tenfold-tweaks"

    def __call__(self, *args):
        if args[0] == "--truename" and self.legacy:
            self.calls.append(args)
            return rh.legacy_diverted()
        if args[0] == "--listpackage":
            self.calls.append(args)
            return self.owner if self.diverted else ""
        return super().__call__(*args)


class GatingTest(unittest.TestCase):
    def setUp(self):
        keep_rooted_state(self)
        self.d = scratch_dir("gate-")
        self.ran = []
        for p in (mock.patch.object(rh, "STATE_DIR", self.d),
                  mock.patch.object(rh, "ensure_state_dir", lambda: None),
                  mock.patch.dict(rh.ACTIONS, {"pkg-remove": (0, lambda: self.ran.append(1))})):
            p.start()
            self.addCleanup(p.stop)

    def test_valid_maintainer_env_runs(self):
        rc, _, err = run_main(["pkg-remove"], MAINT)
        self.assertEqual((rc, self.ran), (rh.EXIT_OK, [1]), err)

    def test_refused_outside_maintainer_scripts(self):
        cases = {
            "no dpkg env": {},
            "pkexec": {**MAINT, "PKEXEC_UID": "1000"},
            "other package": {**MAINT, "DPKG_MAINTSCRIPT_PACKAGE": "kali-themes-common"},
            "no script name": {k: v for k, v in MAINT.items() if k != "DPKG_MAINTSCRIPT_NAME"},
            "bad script name": {**MAINT, "DPKG_MAINTSCRIPT_NAME": "config"},
            "relative root": {**MAINT, "DPKG_ROOT": "chroot"},
            "missing root": {**MAINT, "DPKG_ROOT": os.path.join(self.d, "nope")},
            "newline in admindir": {**MAINT, "DPKG_ADMINDIR": self.d + "\n/var/lib/dpkg"},
            "root without admindir": {**MAINT, "DPKG_ROOT": self.d, "DPKG_ADMINDIR": ""},
        }
        for name, env in cases.items():
            rc, _, err = run_main(["pkg-remove"], env)
            self.assertEqual(rc, rh.EXIT_FAIL, name)
            self.assertTrue(err.strip(), name)
        rc, _, err = run_main(["pkg-remove"], MAINT, euid=1000)
        self.assertEqual(rc, rh.EXIT_FAIL)
        self.assertIn("root", err)
        self.assertEqual(self.ran, [])

    def test_configure_failure_is_only_a_warning(self):
        with mock.patch.dict(rh.ACTIONS, {"pkg-configure": (0, lambda: rh.fail("boom"))}):
            rc, _, err = run_main(["pkg-configure"], {**MAINT, "DPKG_MAINTSCRIPT_NAME": "postinst"})
        self.assertEqual(rc, rh.EXIT_OK)
        self.assertIn("warning: boom", err)


class DpkgRootTest(unittest.TestCase):
    def setUp(self):
        keep_rooted_state(self)
        self.root = scratch_dir("dpkgroot-")
        self.admin = os.path.join(self.root, "var", "lib", "dpkg")
        os.makedirs(self.admin)
        self.env = {**MAINT, "DPKG_ROOT": self.root, "DPKG_ADMINDIR": self.admin}

    def test_paths_rebased_and_divert_gets_explicit_db_args(self):
        with mock.patch.dict(os.environ, self.env):
            rh.use_dpkg_root()
        self.assertEqual(rh.STATE_DIR, self.root + "/var/lib/tenfold-tweaks")
        self.assertEqual(rh.KALI_LOGIN_BG, self.root + "/usr/share/desktop-base/kali-theme/login/background-blurred")
        self.assertEqual(rh.RESCUE_DIR, self.root + "/var/backups/tenfold-tweaks")
        self.assertEqual(rh.INSTALLED_HELPER, self.root + "/usr/libexec/tenfold-tweaks/root-helper")
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append((cmd, kwargs.get("env")))
            out = "/usr/share/desktop-base/kali-theme/login/background-blurred" if "--truename" in cmd else ""
            return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
        with mock.patch.object(rh.subprocess, "run", fake_run):
            self.assertEqual(rh.dpkg_divert("--truename", rh.KALI_LOGIN_BG), rh.KALI_LOGIN_BG)
            rh.dpkg_divert(*rh.divert_args("--add"))
        bg = "/usr/share/desktop-base/kali-theme/login/background-blurred"
        self.assertEqual(seen[1][0], ["/usr/bin/dpkg-divert", "--instdir", self.root, "--admindir", self.admin,
                                      "--package", "tenfold-tweaks", "--rename", "--divert",
                                      bg + ".tenfold-tweaks", "--add", bg])
        self.assertNotIn("--root", seen[1][0])
        self.assertEqual(seen[1][1], {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})

    def test_host_run_passes_admindir_only(self):
        with mock.patch.dict(os.environ, MAINT):
            rh.use_dpkg_root()
        self.assertEqual(rh.STATE_DIR, "/var/lib/tenfold-tweaks")
        self.assertEqual(rh.dpkg_db_args(), ["--admindir", "/var/lib/dpkg"])

    def test_runtime_divert_has_no_db_args(self):
        self.assertEqual(rh.dpkg_db_args(), [])

    def test_pkg_remove_stays_inside_the_root(self):
        calls = []
        with mock.patch.object(rh, "ensure_state_dir", lambda: os.makedirs(rh.STATE_DIR, exist_ok=True)), \
                mock.patch.object(rh, "dpkg_divert", lambda *a: calls.append(a) or ""):
            rc, out, err = run_main(["pkg-remove"], self.env)
        self.assertEqual(rc, rh.EXIT_OK, err)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "var", "lib", "tenfold-tweaks")))
        self.assertEqual((out, calls), ("", []))  # nothing installed in the root: nothing to do


class RuntimeDpkgLockTest(unittest.TestCase):
    HOLDER = ("import fcntl, os, sys, time\n"
              "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
              "fcntl.lockf(fd, fcntl.LOCK_EX)\n"
              "print('locked', flush=True)\n"
              "time.sleep(30)\n")

    def setUp(self):
        keep_rooted_state(self)
        self.d = scratch_dir("dpkglock-")
        self.locks = (os.path.join(self.d, "lock-frontend"), os.path.join(self.d, "lock"))
        self.ran = []
        actions = {a: (0, lambda a=a: self.ran.append(a)) for a in ("gdm-restore", "lightdm-restore", "pkg-remove")}
        for p in (mock.patch.object(rh, "DPKG_LOCKS", self.locks),
                  mock.patch.object(rh, "STATE_DIR", self.d),
                  mock.patch.object(rh, "ensure_state_dir", lambda: None),
                  mock.patch.dict(rh.ACTIONS, actions)):
            p.start()
            self.addCleanup(p.stop)

    def hold(self, path):
        proc = subprocess.Popen([sys.executable, "-I", "-c", self.HOLDER, path], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), "locked")
        return proc

    def test_free_locks_are_taken_and_released(self):
        self.assertEqual(run_main(["gdm-restore"], {})[0], rh.EXIT_OK)
        self.hold(self.locks[1])  # released again, so another process can take it
        self.assertEqual(self.ran, ["gdm-restore"])

    def test_busy_dpkg_refuses_gdm_actions_without_change(self):
        for held in self.locks:
            with self.subTest(held=os.path.basename(held)):
                proc = self.hold(held)
                rc, _, err = run_main(["gdm-restore"], {})
                proc.kill()
                proc.wait()
                self.assertEqual(rc, rh.EXIT_FAIL)
                self.assertIn(rh.DPKG_BUSY, err)
        self.assertEqual(self.ran, [])

    def test_lightdm_and_pkg_actions_skip_the_dpkg_lock(self):
        for held in self.locks:
            self.hold(held)
        self.assertEqual(run_main(["lightdm-restore"], {})[0], rh.EXIT_OK)
        self.assertEqual(run_main(["pkg-remove"], MAINT)[0], rh.EXIT_OK)
        self.assertEqual(self.ran, ["lightdm-restore", "pkg-remove"])


@unittest.skipUnless(os.path.isfile(STOCK), "stock gnome-shell theme not installed")
class KaliPkgRemoveTest(Sandbox):
    LINK = "../../../backgrounds/kali/login-blurred"

    def setUp(self):
        super().setUp()
        shutil.copyfile(STOCK, self.p["GRESOURCE"])
        root = os.path.join(self.d, "root", "usr", "share")
        bg = os.path.join(root, "desktop-base", "kali-theme", "login", "background-blurred")
        paths = {"KALI_LOGIN_BG": bg, "KALI_LOGIN_BG_DIVERTED": bg + ".tenfold-tweaks"}
        self.p.update(paths)
        write(os.path.join(root, "backgrounds", "kali", "login-blurred"), b"\xff\xd8\xff kali jpeg")
        os.makedirs(os.path.dirname(bg))
        os.symlink(self.LINK, bg)
        write(os.path.join(self.d, "usr-lib", "libshell-18.so"), b"\x7fELF" + needle(bg))
        self.fake = PkgDivert()
        for p in (mock.patch.multiple(rh, **paths), mock.patch.object(rh, "dpkg_divert", self.fake),
                  mock.patch.object(rh, "check_root_dir", lambda path: None)):
            p.start()
            self.addCleanup(p.stop)

    def remove(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(rh.pkg_remove(), rh.EXIT_OK)
        return out.getvalue()

    def test_saves_image_then_restores_stock_and_never_duplicates(self):
        rh.gdm_apply(self.png)
        out = self.remove()
        self.assertEqual(os.readlink(self.p["KALI_LOGIN_BG"]), self.LINK)
        self.assertFalse(self.fake.diverted)
        [name] = os.listdir(self.p["RESCUE_DIR"])
        saved = os.path.join(self.p["RESCUE_DIR"], name)
        self.assertRegex(name, r"^login-[0-9a-f]{12}\.png$")
        self.assertEqual(read(saved), read(self.png))
        self.assertEqual(os.stat(saved).st_mode & 0o777, 0o644)
        self.assertEqual(out, f"saved login image: {saved}\n")
        rh.gdm_apply(self.png)
        self.remove()
        self.assertEqual(os.listdir(self.p["RESCUE_DIR"]), [name])

    def test_foreign_owner_refused_before_any_change(self):
        rh.gdm_apply(self.png)
        self.fake.owner = "LOCAL"
        with self.assertRaises(rh.HelperError) as cm:
            rh.pkg_remove()
        self.assertIn("not owned by tenfold-tweaks", str(cm.exception))
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), read(self.png))
        self.assertFalse(os.path.exists(self.p["RESCUE_DIR"]))

    def user_image_at_path(self):
        os.unlink(self.p["KALI_LOGIN_BG"])
        write(self.p["KALI_LOGIN_BG"], b"\x89PNG\r\n\x1a\nuser image")
        os.symlink(self.LINK, rh.legacy_diverted())

    def assertUntouched(self):
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), b"\x89PNG\r\n\x1a\nuser image")
        self.assertEqual(os.readlink(rh.legacy_diverted()), self.LINK)
        self.assertFalse(os.path.exists(self.p["RESCUE_DIR"]))
        self.assertEqual([c for c in self.fake.calls if "--add" in c or "--remove" in c], [])

    def test_legacy_diversion_refused_everywhere(self):
        self.user_image_at_path()
        self.fake.legacy = True
        for action in (rh.pkg_remove, rh.gdm_restore, lambda: rh.gdm_apply(self.png)):
            with self.assertRaises(rh.HelperError) as cm:
                action()
            self.assertIn("reinstall the tenfold-tweaks package", str(cm.exception))
        self.assertUntouched()

    def test_migration_cut_between_remove_and_add_is_refused(self):
        # Hard crash in preinst after step 2: no diversion in the db, marker + OLD + NEW on disk.
        self.user_image_at_path()
        os.symlink(self.LINK, self.p["KALI_LOGIN_BG_DIVERTED"])
        write(rh.divert_marker(), b"")
        for action in (rh.pkg_remove, rh.gdm_restore, lambda: rh.gdm_apply(self.png)):
            with self.assertRaises(rh.HelperError) as cm:
                action()
            self.assertIn("reinstall the tenfold-tweaks package", str(cm.exception))
        self.assertUntouched()
        self.assertTrue(os.path.islink(self.p["KALI_LOGIN_BG_DIVERTED"]))
        self.assertTrue(os.path.exists(rh.divert_marker()))


@unittest.skipUnless(os.path.isfile(STOCK), "stock gnome-shell theme not installed")
class GResourcePkgTest(Sandbox):
    def setUp(self):
        super().setUp()
        shutil.copyfile(STOCK, self.p["GRESOURCE"])
        self.stock = read(STOCK)
        p = mock.patch.object(rh, "check_root_dir", lambda path: None)
        p.start()
        self.addCleanup(p.stop)

    def entries(self):
        return rh.load_entries(self.p["GRESOURCE"])

    def apply_legacy(self):
        with mock.patch.object(rh, "LOGIN_ENTRY", rh.LEGACY_LOGIN_ENTRY):
            rh.gdm_apply(self.png)
        self.assertIn(rh.LEGACY_LOGIN_ENTRY + "png", self.entries())

    def test_remove_restores_stock_and_saves_embedded_image(self):
        rh.gdm_apply(self.png)
        with redirect_stdout(io.StringIO()):
            rh.pkg_remove()
        self.assertEqual(read(self.p["GRESOURCE"]), self.stock)
        [name] = os.listdir(self.p["RESCUE_DIR"])
        self.assertEqual(read(os.path.join(self.p["RESCUE_DIR"], name)), read(self.png))

    def test_remove_refused_without_matching_backup(self):
        rh.gdm_apply(self.png)
        before = read(self.p["GRESOURCE"])
        self.version = "99"
        with self.assertRaises(rh.HelperError) as cm:
            rh.pkg_remove()
        self.assertIn("reinstall gnome-shell-common", str(cm.exception))
        self.assertEqual(read(self.p["GRESOURCE"]), before)
        self.assertFalse(os.path.exists(self.p["RESCUE_DIR"]))

    def test_configure_reembeds_old_entry_under_new_name(self):
        self.apply_legacy()
        self.assertEqual(rh.pkg_configure(), rh.EXIT_OK)
        entries = self.entries()
        self.assertNotIn(rh.LEGACY_LOGIN_ENTRY + "png", entries)
        self.assertEqual(entries[rh.LOGIN_ENTRY + "png"], read(self.png))
        self.assertEqual(entries[rh.THEME_PREFIX + "gnome-shell-dark.css"].count(b"/* tenfold-tweaks */"), 1)
        self.assertEqual(read(self.p["GRESOURCE_ORIG"]), self.stock)
        self.assertEqual(rh.pkg_configure(), rh.EXIT_OK)  # idempotent
        self.assertEqual(rh.gdm_restore(), rh.EXIT_OK)
        self.assertEqual(read(self.p["GRESOURCE"]), self.stock)

    def test_old_entry_still_restorable(self):
        self.apply_legacy()
        self.assertEqual(rh.gdm_restore(), rh.EXIT_OK)
        self.assertEqual(read(self.p["GRESOURCE"]), self.stock)


if __name__ == "__main__":
    unittest.main()
