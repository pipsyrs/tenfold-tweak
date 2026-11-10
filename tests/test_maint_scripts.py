"""ADR 002 state matrix: crash (kill -9 of dpkg) or failure after every migration step, then re-run.

A variant .deb has failpoints spliced into a copy of common.sh. Every failpoint also logs the login
image at the Kali path and the stock target, so the migration invariant is checked at each step.
Same scratch-root setup as test_packaging (dpkg --root under unshare -r); the host is never touched.
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests.test_packaging import (BG, LIGHTDM_CONF, LIGHTDM_IMAGE, MIGRATED_IMAGE, NEW, NEW_LIGHTDM_IMAGE, OLD,
                                  OLD_STATE, PKG, RESCUE, ROOT, STATE, STOCK_TARGET, USER_IMAGE, MaintScriptTest,
                                  PackagingTestBase, VERSION, build, sha12)

STOCK_FILE = "/usr/share/backgrounds/kali/login-blurred"
SHA = lambda data: hashlib.sha256(data).hexdigest()  # noqa: E731

FP_FUNC = r'''
tt_fp() {
    _fpd=$(dirname -- "$TT_R")
    _fpb=$TT_R$TT_BG
    if [ -L "$_fpb" ]; then _fpk="link:$(readlink -- "$_fpb")"
    elif [ -f "$_fpb" ]; then _fpk="file:$(sha256sum <"$_fpb" | cut -c1-64)"
    else _fpk=missing; fi
    _fps=$(sha256sum <"$TT_R/usr/share/backgrounds/kali/login-blurred" | cut -c1-64)
    echo "$DPKG_MAINTSCRIPT_NAME $1 $_fpk $_fps" >>"$_fpd/fp.log"
    _fpf=$_fpd/fp.$DPKG_MAINTSCRIPT_NAME.$1
    if [ -e "$_fpf" ]; then
        _fpm=$(cat "$_fpf")
        rm -f -- "$_fpf"
        if [ "$_fpm" = kill ]; then kill -9 "$PPID" "$$"; sleep 5; fi
        exit 1
    fi
    return 0
}
'''

# (anchor, failpoint inserted before it); each anchor must occur once in common.sh.
FAILPOINTS = [
    ("    # 2: drop the old diversion record.\n", "d1"),
    ("    # 3: add the new diversion record.\n", "d2"),
    ("    # 4: the old stock copy is no longer referenced.\n", "d3"),
    ("    sync\n    return 0\n}\n\n# Keep a copy", "d4"),
    ("    tt_preflight_legacy\n    if [ -z \"$TT_DO\" ]", "p0"),
    ("    # S1\n", "s0"),
    ("    # S2: the only copy", "s1"),
    ("    # S3\n", "s2"),
    ("    # S4\n", "s3"),
    ("    sync\n    return 0\n}\n\n# postrm abort-install", "s4"),
    ("    tt_divert_migrate \"$TT_NEW_SFX\"", "u1"),
    ("    rm -f -- \"$_os/$TT_MARKER\"\n", "u2"),
]
FORWARD = ["s0", "d1", "d2", "d3", "d4", "s1", "s2", "s3", "s4"]


def build_variant(out_dir: str, version: str | None = None) -> str:
    """build-deb.sh on a copy of the source tree whose common.sh carries the failpoints."""
    src = os.path.join(out_dir, "src")
    shutil.copytree(os.path.join(ROOT, "packaging"), os.path.join(src, "packaging"))
    os.makedirs(os.path.join(src, "tenfold_tweak"))
    for name in os.listdir(os.path.join(ROOT, "tenfold_tweak")):
        if name.endswith(".py"):
            shutil.copy(os.path.join(ROOT, "tenfold_tweak", name), os.path.join(src, "tenfold_tweak", name))
    for name in ("tenfold-tweaks", "tenfold-tweaks-gui"):
        shutil.copy(os.path.join(ROOT, name), os.path.join(src, name))
    common = os.path.join(src, "packaging", "maint", "common.sh")
    with open(common) as f:
        text = f.read()
    for anchor, name in FAILPOINTS:
        assert text.count(anchor) == 1, anchor
        text = text.replace(anchor, f"    tt_fp {name}\n" + anchor)
    text = text.replace("TT_MARKED=\n", "TT_MARKED=\n" + FP_FUNC, 1)
    with open(common, "w") as f:
        f.write(text)
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
           "BUILD_DIR": os.path.join(out_dir, "build"), "DIST_DIR": out_dir}
    if version:
        env["DEB_VERSION"] = version
    res = subprocess.run(["sh", os.path.join(src, "packaging", "build-deb.sh")], env=env,
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    return res.stdout.strip().splitlines()[-1]


class MatrixBase(PackagingTestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fp_deb = build_variant(os.path.join(cls.tmp, "fp"))

    def arm(self, script: str, point: str, mode: str):
        with open(os.path.join(self.base, f"fp.{script}.{point}"), "w") as f:
            f.write(mode)

    def fp_log(self) -> list:
        try:
            with open(os.path.join(self.base, "fp.log")) as f:
                return [line.split() for line in f]
        except FileNotFoundError:
            return []

    def assert_invariant(self, where=""):
        """User image present and byte-identical at the Kali path; stock target never written."""
        r = self.r
        self.assertFalse(os.path.islink(r.p(BG)), where)
        self.assertEqual(SHA(r.read(BG)), SHA(USER_IMAGE), where)
        self.assertEqual(r.read(STOCK_FILE), b"stock", where)
        for name in (OLD, NEW):
            if r.exists(name):
                self.assertEqual(os.readlink(r.p(name)), STOCK_TARGET, where + name)

    def assert_log_invariant(self):
        rows = self.fp_log()
        self.assertTrue(rows)
        for script, point, kind, stock in rows:
            self.assertEqual(kind, "file:" + SHA(USER_IMAGE), (script, point))
            self.assertEqual(stock, SHA(b"stock"), (script, point))

    def assert_full_migration(self, out: str = ""):
        MaintScriptTest.assert_migrated(self)
        r = self.r
        self.assertEqual(r.read(f"{RESCUE}/migrated-login-{sha12(MIGRATED_IMAGE)}.jpg"), MIGRATED_IMAGE)
        self.assertFalse(r.exists(STATE + "/migrated-login"))
        self.assertIn(b"background=" + NEW_LIGHTDM_IMAGE.encode(), r.read(LIGHTDM_CONF))
        self.assertEqual(r.read(NEW_LIGHTDM_IMAGE), LIGHTDM_IMAGE)
        self.assertEqual(r.status(), "install ok installed " + VERSION)


def symlink_conf(test) -> tuple:
    """Turn the greeter conf into a relative symlink to a regular file beside it; returns (target, bytes)."""
    r = test.r
    real = LIGHTDM_CONF + ".real"
    os.rename(r.p(LIGHTDM_CONF), r.p(real))
    os.symlink(os.path.basename(real), r.p(LIGHTDM_CONF))
    return real, r.read(real)


def assert_conf_link_kept(test, real: str, data: bytes):
    """The symlink is still there, its target is untouched, and nothing in /etc/lightdm is g/o-writable."""
    r = test.r
    for name in os.listdir(r.p("/etc/lightdm")):
        path = r.p("/etc/lightdm/" + name)
        if not os.path.islink(path):
            test.assertEqual(os.lstat(path).st_mode & 0o022, 0, f"{name} is {os.lstat(path).st_mode & 0o7777:o}")
    test.assertTrue(os.path.islink(r.p(LIGHTDM_CONF)))
    test.assertEqual(os.readlink(r.p(LIGHTDM_CONF)), os.path.basename(real))
    test.assertEqual(r.read(real), data)
    test.assertEqual(os.lstat(r.p(real)).st_mode & 0o7777, 0o644)


class CrashResumeTest(MatrixBase):
    """Hard crash (dpkg and preinst killed) after each step; the next install resumes."""

    def crash_then_resume(self, point: str):
        r = self.r
        r.seed_legacy(migrated_login=True, lightdm=True)
        self.arm("preinst", point, "kill")
        res = r.dpkg("-i", self.fp_deb, check=False)
        self.assertEqual(res.returncode, -9, res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.base, f"fp.preinst.{point}")), "failpoint not reached")
        self.assert_invariant(f"after crash at {point}: ")
        diversions = r.divert("--list")
        if point == "d2":  # the documented window: no record between steps 2 and 3
            self.assertEqual(diversions, "")
        elif point in ("s0", "d1"):
            self.assertEqual(diversions, f"local diversion of {BG} to {OLD}")
        else:
            self.assertEqual(diversions, f"diversion of {BG} to {NEW} by {PKG}")
        res = r.dpkg("-i", self.deb)
        self.assert_invariant(f"after resume from {point}: ")
        self.assert_full_migration(res.stdout)
        self.assert_log_invariant()

    def test_crash_after_marker(self):
        self.crash_then_resume("s0")

    def test_crash_after_step1_duplicate(self):
        self.crash_then_resume("d1")

    def test_crash_after_step2_record_dropped(self):
        self.crash_then_resume("d2")

    def test_crash_after_step3_record_added(self):
        self.crash_then_resume("d3")

    def test_crash_after_step4_old_removed(self):
        self.crash_then_resume("d4")

    def test_crash_after_s1(self):
        self.crash_then_resume("s1")

    def test_crash_after_s2_rescue(self):
        self.crash_then_resume("s2")

    def test_crash_after_s3_state_moved(self):
        self.crash_then_resume("s3")

    def test_crash_after_s4_lightdm(self):
        self.crash_then_resume("s4")

    def test_crash_in_resume_then_resume_again(self):
        r = self.r
        r.seed_legacy(migrated_login=True, lightdm=True)
        self.arm("preinst", "d2", "kill")
        r.dpkg("-i", self.fp_deb, check=False)
        self.arm("preinst", "s3", "kill")
        self.assertEqual(r.dpkg("-i", self.fp_deb, check=False).returncode, -9)
        self.assert_invariant()
        r.dpkg("-i", self.deb)
        self.assert_full_migration()
        self.assert_log_invariant()


class FailReverseTest(MatrixBase):
    """preinst fails (exit 1) after each step: dpkg runs postrm abort-install, which undoes it all."""

    def fail_then_reverse(self, point: str):
        r = self.r
        r.seed_legacy(migrated_login=True, lightdm=True)
        before = {p: v for p, v in r.snapshot().items() if not p.startswith("/var/backups")}
        diversions = r.divert("--list")
        self.arm("preinst", point, "exit")
        res = r.dpkg("-i", self.fp_deb, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.base, f"fp.preinst.{point}")), "failpoint not reached")
        self.assertIn("not-installed", r.status())
        self.assertEqual(r.divert("--list"), diversions)
        self.assertEqual({p: v for p, v in r.snapshot().items() if not p.startswith("/var/backups")}, before)
        self.assert_invariant()
        r.dpkg("-i", self.deb)
        self.assert_full_migration()
        self.assert_log_invariant()

    def test_fail_after_each_step(self):
        for point in FORWARD:
            with self.subTest(point=point):
                shutil.rmtree(self.base)
                os.makedirs(self.base)
                from tests.test_packaging import ScratchRoot
                self.r = ScratchRoot(self.base)
                self.fail_then_reverse(point)

    def test_crash_during_abort_install_then_resume(self):
        """Killed while postrm abort-install reverses the migration; the next install still finishes."""
        r = self.r
        r.seed_legacy(migrated_login=True, lightdm=True)
        r.install_blocker("/usr/share/applications/io.github.tenfold.tenfold-tweaks.desktop")
        for point in ("u1", "d2"):
            with self.subTest(point=point):
                self.arm("postrm", point, "kill")
                res = r.dpkg("-i", self.fp_deb, check=False)
                self.assertEqual(res.returncode, -9, res.stderr)
                self.assert_invariant(f"postrm killed at {point}: ")
        r.dpkg("--purge", "blocker")
        r.dpkg("-i", self.deb)
        self.assert_full_migration()
        self.assert_log_invariant()


class AbortUpgradeTest(MatrixBase):
    def test_crash_then_failed_resume_reverses_via_abort_upgrade(self):
        """After a crash the package is half-installed, so the re-run is preinst upgrade / postrm abort-upgrade."""
        r = self.r
        r.seed_legacy(migrated_login=True, lightdm=True)
        before = {p: v for p, v in r.snapshot().items() if not p.startswith("/var/backups")}
        self.arm("preinst", "d2", "kill")
        r.dpkg("-i", self.fp_deb, check=False)
        self.assertIn("half-installed", r.status())
        self.arm("preinst", "s3", "exit")
        res = r.dpkg("-i", self.fp_deb, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.base, "fp.preinst.s3")), "failpoint not reached")
        self.assertIn("restoring the tenfold-tweak install", res.stdout)
        self.assertEqual(r.divert("--list"), f"local diversion of {BG} to {OLD}")
        self.assertEqual({p: v for p, v in r.snapshot().items() if not p.startswith("/var/backups")}, before)
        self.assert_invariant()
        r.dpkg("-i", self.deb)
        self.assert_full_migration()
        self.assert_log_invariant()

    def test_abort_leaves_symlinked_conf_untouched(self):
        """postrm abort-upgrade with a symlinked greeter conf: warned, never followed, no 0777 file."""
        r = self.r
        r.seed_legacy(migrated_login=True, lightdm=True)
        self.arm("preinst", "s4", "kill")
        r.dpkg("-i", self.fp_deb, check=False)
        self.assertIn("half-installed", r.status())
        real, data = symlink_conf(self)
        self.assertIn(NEW_LIGHTDM_IMAGE.encode(), data)
        res = r.dpkg("-i", self.deb, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("restoring the tenfold-tweak install", res.stdout)
        assert_conf_link_kept(self, real, data)
        self.assertIn(f"{LIGHTDM_CONF} is not a regular file, left untouched", res.stderr)
        self.assertEqual(r.divert("--list"), f"local diversion of {BG} to {OLD}")
        self.assertFalse(r.exists(STATE))
        self.assert_invariant()

    def test_crash_then_purge_is_refused_and_changes_nothing(self):
        """After a crash dpkg demands a reinstall (reinstreq) before any removal; the login keeps working."""
        r = self.r
        r.seed_legacy(migrated_login=True)
        self.arm("preinst", "s3", "kill")
        r.dpkg("-i", self.fp_deb, check=False)
        before, diversions = r.snapshot(), r.divert("--list")
        res = r.dpkg("--purge", PKG, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("should\n be reinstalled", res.stderr)
        self.assertEqual(r.snapshot(), before)
        self.assertEqual(r.divert("--list"), diversions)
        self.assert_invariant()
        r.dpkg("-i", self.deb)
        r.dpkg("--purge", PKG)
        self.assertEqual(r.divert("--list"), "")
        self.assertEqual(os.readlink(r.p(BG)), STOCK_TARGET)
        self.assertEqual(r.read(f"{RESCUE}/login-{sha12(USER_IMAGE)}.png"), USER_IMAGE)
        self.assertEqual(r.read(f"{RESCUE}/migrated-login-{sha12(MIGRATED_IMAGE)}.jpg"), MIGRATED_IMAGE)
        MaintScriptTest.assert_no_traces(self)

    def test_failed_upgrade_keeps_installed_state(self):
        r = self.r
        r.seed_legacy(migrated_login=True, lightdm=True)
        r.dpkg("-i", self.deb)
        before = r.snapshot()
        diversions = r.divert("--list")
        newer = build_variant(os.path.join(self.base, "v2"), VERSION + "+1")
        self.arm("preinst", "p0", "exit")
        res = r.dpkg("-i", newer, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(r.status(), "install ok installed " + VERSION)
        self.assertEqual(r.divert("--list"), diversions)
        self.assertEqual(r.snapshot(), before)
        self.assert_invariant()

    def test_plain_upgrade_runs_no_migration(self):
        r = self.r
        r.seed_legacy()
        r.dpkg("-i", self.deb)
        before = r.snapshot()
        newer = build_variant(os.path.join(self.base, "v2"), VERSION + "+1")
        self.arm("postrm", "u2", "exit")  # must not be reached: no marker on a plain upgrade
        self.arm("preinst", "s4", "exit")  # must not be reached either: nothing to migrate
        res = r.dpkg("-i", newer, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.base, "fp.postrm.u2")))
        self.assertTrue(os.path.exists(os.path.join(self.base, "fp.preinst.s4")))
        self.assertEqual(r.status(), "install ok installed " + VERSION + "+1")
        after = r.snapshot()
        self.assertEqual({k: v for k, v in after.items() if k.startswith(("/var", BG))},
                         {k: v for k, v in before.items() if k.startswith(("/var", BG))})
        self.assert_invariant()


class PurgeLeftoverTest(MatrixBase):
    def stub_helper(self):
        """A prerm that restores nothing (forced remove / broken helper): the diversion is left behind."""
        helper = self.r.p("/usr/libexec/tenfold-tweaks/root-helper")
        with open(helper, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")

    def test_purge_releases_leftover_diversion_and_saves_image(self):
        r = self.r
        r.seed_legacy(lightdm=True)
        r.write(STATE_GRES := OLD_STATE + "/gnome-shell-theme.gresource.orig", b"gresource")
        r.dpkg("-i", self.deb)
        self.assertEqual(r.read(STATE + "/gnome-shell-theme.gresource.orig"), b"gresource")  # moved by S3
        self.stub_helper()
        r.dpkg("-r", PKG)
        self.assertEqual(r.divert("--list"), f"diversion of {BG} to {NEW} by {PKG}")
        self.assert_invariant("after the stubbed remove: ")
        out = r.dpkg("--purge", PKG).stdout
        self.assertEqual(r.divert("--list"), "")
        self.assertEqual(os.readlink(r.p(BG)), STOCK_TARGET)
        self.assertEqual(r.read(STOCK_FILE), b"stock")
        saved = f"{RESCUE}/login-{sha12(USER_IMAGE)}.png"
        self.assertEqual(r.read(saved), USER_IMAGE)
        self.assertIn("saved login image: " + r.p(saved), out)
        lightdm = f"{RESCUE}/login-{sha12(LIGHTDM_IMAGE)}.jpg"
        self.assertEqual(r.read(lightdm), LIGHTDM_IMAGE)
        self.assertEqual(r.read(LIGHTDM_CONF), b"[greeter]\nbackground=stock\n")
        self.assertIn("apt install --reinstall gnome-shell-common", out)
        self.assertFalse(r.exists(STATE_GRES.replace(OLD_STATE, STATE)))
        MaintScriptTest.assert_no_traces(self)

    def test_purge_leaves_symlinked_conf_untouched(self):
        """Purge with a symlinked greeter conf: warned and kept; the purge itself still succeeds."""
        r = self.r
        r.seed_legacy(lightdm=True)
        r.dpkg("-i", self.deb)
        self.stub_helper()
        r.dpkg("-r", PKG)
        real, data = symlink_conf(self)
        res = r.dpkg("--purge", PKG)
        self.assertNotIn(PKG, r.status())
        assert_conf_link_kept(self, real, data)
        self.assertIn(f"{LIGHTDM_CONF} is not a regular file, left untouched", res.stderr)
        self.assertEqual(r.read(f"{RESCUE}/login-{sha12(LIGHTDM_IMAGE)}.jpg"), LIGHTDM_IMAGE)
        self.assertFalse(r.exists(STATE))

    def test_purge_restores_aside_file_left_by_crashed_restore(self):
        r = self.r
        r.seed_legacy()
        r.dpkg("-i", self.deb)
        self.stub_helper()
        r.dpkg("-r", PKG)
        # Crash inside kali_login_restore: image moved aside, diversion still recorded.
        os.rename(r.p(BG), r.p(BG + ".tenfold-tweaks-restore"))
        r.dpkg("--purge", PKG)
        self.assertEqual(r.divert("--list"), "")
        self.assertEqual(os.readlink(r.p(BG)), STOCK_TARGET)
        self.assertEqual(r.read(f"{RESCUE}/login-{sha12(USER_IMAGE)}.png"), USER_IMAGE)
        MaintScriptTest.assert_no_traces(self)

    def test_purge_saves_leftover_aside_next_to_active_image(self):
        """An old aside from a crashed restore plus a newer image at the path: both are saved."""
        r = self.r
        r.seed_legacy()
        r.dpkg("-i", self.deb)
        self.stub_helper()
        r.dpkg("-r", PKG)
        older = b"\x89PNG\r\n\x1a\n" + b"older aside image" * 64
        r.write(BG + ".tenfold-tweaks-restore", older)
        r.dpkg("--purge", PKG)
        self.assertEqual(r.divert("--list"), "")
        self.assertEqual(os.readlink(r.p(BG)), STOCK_TARGET)
        self.assertEqual(r.read(f"{RESCUE}/login-{sha12(older)}.png"), older)
        self.assertEqual(r.read(f"{RESCUE}/login-{sha12(USER_IMAGE)}.png"), USER_IMAGE)
        MaintScriptTest.assert_no_traces(self)

    def test_purge_after_rename_done_but_record_kept(self):
        """dpkg-divert renamed the stock back but did not drop the record: the path is stock and is kept."""
        r = self.r
        r.seed_legacy()
        r.dpkg("-i", self.deb)
        self.stub_helper()
        r.dpkg("-r", PKG)
        os.rename(r.p(BG), r.p(BG + ".tenfold-tweaks-restore"))
        os.rename(r.p(NEW), r.p(BG))
        self.assertEqual(r.divert("--list"), f"diversion of {BG} to {NEW} by {PKG}")
        r.dpkg("--purge", PKG)
        self.assertEqual(r.divert("--list"), "")
        self.assertEqual(os.readlink(r.p(BG)), STOCK_TARGET)
        self.assertEqual(r.read(STOCK_FILE), b"stock")
        self.assertEqual(r.read(f"{RESCUE}/login-{sha12(USER_IMAGE)}.png"), USER_IMAGE)
        MaintScriptTest.assert_no_traces(self)


class RescueNoOverwriteTest(MatrixBase):
    def apply_login(self, data: bytes):
        """What runtime gdm-apply leaves on Kali: our --rename diversion, marker, image at the path."""
        r = self.r
        r.divert("--package", PKG, "--rename", "--divert", NEW, "--add", BG)
        r.write(STATE + "/kali-login.divert", b"")
        r.write(BG, data)

    def rescue_state(self) -> dict:
        d = self.r.p(RESCUE)
        return {n: (os.lstat(os.path.join(d, n)).st_ino, self.r.read(RESCUE + "/" + n)) for n in sorted(os.listdir(d))}

    def test_repeated_remove_and_purge_never_overwrite(self):
        r = self.r
        r.seed_legacy(migrated_login=True)
        r.dpkg("-i", self.deb)
        r.dpkg("-r", PKG)
        first = self.rescue_state()
        self.assertEqual(sorted(first), sorted([f"login-{sha12(USER_IMAGE)}.png",
                                                f"migrated-login-{sha12(MIGRATED_IMAGE)}.jpg"]))
        for image in (USER_IMAGE, b"\x89PNG\r\n\x1a\n" + b"second" * 50):
            r.dpkg("-i", self.deb)
            self.apply_login(image)
            out = r.dpkg("-r", PKG).stdout
            self.assertIn("saved login image: " + r.p(f"{RESCUE}/login-{sha12(image)}.png"), out)
            now = self.rescue_state()
            for name, value in first.items():
                self.assertEqual(now[name], value, name)
        r.dpkg("-i", self.deb)
        self.apply_login(USER_IMAGE)
        r.dpkg("--purge", PKG)
        now = self.rescue_state()
        for name, value in first.items():
            self.assertEqual(now[name], value, name)
        self.assertEqual(len(now), 3)
        MaintScriptTest.assert_no_traces(self)

    def test_rescue_name_taken_by_other_bytes_is_not_trusted(self):
        """A file already at the hash name but with other bytes must not count as the saved copy."""
        r = self.r
        r.seed_kali()
        r.dpkg("-i", self.deb)
        self.apply_login(USER_IMAGE)
        r.write(f"{RESCUE}/login-{sha12(USER_IMAGE)}.png", b"truncated")
        os.chmod(r.p(RESCUE), 0o755)
        res = r.dpkg("-r", PKG, check=False)
        self.assertEqual(r.read(f"{RESCUE}/login-{sha12(USER_IMAGE)}.png"), b"truncated")  # never overwritten
        kept = any(r.read(f"{RESCUE}/{n}") == USER_IMAGE for n in os.listdir(r.p(RESCUE)))  # saved under another name
        still_there = r.exists(BG) and not os.path.islink(r.p(BG)) and r.read(BG) == USER_IMAGE
        self.assertTrue(kept or still_there or res.returncode != 0,
                        "user image deleted while the rescue file holds other bytes")


class RemoveAfterCrashedRestoreTest(MatrixBase):
    def test_remove_saves_image_left_aside_by_crashed_restore(self):
        """Power loss inside kali_login_restore (image renamed aside, diversion still there), then apt remove."""
        r = self.r
        r.seed_legacy()
        r.dpkg("-i", self.deb)
        os.rename(r.p(BG), r.p(BG + ".tenfold-tweaks-restore"))
        r.dpkg("-r", PKG, check=False)
        self.assertEqual(r.read(f"{RESCUE}/login-{sha12(USER_IMAGE)}.png"), USER_IMAGE)


class NoImageNoMessageTest(MatrixBase):
    def test_remove_and_purge_without_login_image_print_nothing_saved(self):
        r = self.r
        r.seed_kali()
        r.dpkg("-i", self.deb)
        out = r.dpkg("-r", PKG).stdout + r.dpkg("--purge", PKG).stdout
        self.assertNotIn("saved login image", out)
        self.assertFalse(r.exists(RESCUE))


class RemoveRefusedTest(MatrixBase):
    def test_unsafe_remove_is_refused_and_changes_nothing(self):
        """The active image cannot be saved: prerm fails, the package stays and nothing moves."""
        r = self.r
        r.seed_legacy(lightdm=True)
        r.dpkg("-i", self.deb)
        r.write(RESCUE, b"not a directory")
        before, diversions = r.snapshot(), r.divert("--list")
        res = r.dpkg("-r", PKG, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("the package stays installed", res.stderr)
        self.assertTrue(r.status().endswith(" ok installed " + VERSION), r.status())  # selection is "deinstall"
        self.assertEqual(r.divert("--list"), diversions)
        self.assertEqual(r.snapshot(), before)
        self.assert_invariant()


class RenameCompletenessTest(MatrixBase):
    """Requirement 1: every path created or changed carries tenfold-tweaks; nothing named tenfold-tweak is left."""

    OLD_NAME = re.compile(r"tenfold-tweak(?!s)")

    def changed(self, before: dict, after: dict) -> list:
        paths = [p for p, v in after.items() if before.get(p) != v]
        ours = [p for p in paths if "tenfold-tweaks" in p]
        # Standard parent dirs created for our files (e.g. /usr/libexec) are not traces.
        return [p for p in paths if "tenfold-tweaks" not in p
                and not (after[p][0] == "dir" and any(o.startswith(p + "/") for o in ours))]

    def test_install_remove_purge_names(self):
        r = self.r
        r.seed_legacy(migrated_login=True, lightdm=True)
        r.write(OLD_STATE + "/gnome-shell-theme.gresource.orig", b"gresource")
        before = r.snapshot()
        r.dpkg("-i", self.deb)
        after = r.snapshot()
        # The edited system file is the LightDM conf; its backup carries the new name.
        self.assertEqual(self.changed(before, after), [LIGHTDM_CONF])
        self.assertTrue(r.exists(STATE + "/lightdm-gtk-greeter.conf.orig"))
        self.assertEqual([p for p in after if self.OLD_NAME.search(p)], [])
        self.assertNotRegex(r.divert("--list"), self.OLD_NAME)
        self.assertNotIn(b"/tenfold-tweak/", r.read(LIGHTDM_CONF))
        r.dpkg("-r", PKG)
        removed = r.snapshot()
        self.assertEqual([p for p in removed if self.OLD_NAME.search(p)], [])
        self.assertEqual([p for p in self.changed(after, removed) if p not in (BG, LIGHTDM_CONF)], [])
        r.dpkg("--purge", PKG)
        self.assertEqual([p for p in r.snapshot() if "tenfold" in p and not p.startswith(RESCUE)], [])


if __name__ == "__main__":
    unittest.main()
