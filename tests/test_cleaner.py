"""Cleaner 2.0.0: categories, CLI selection, safe walk, admin flow, root-helper clean (all sandboxed)."""

import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from tests.helpers import scratch_dir, write
from tests.test_root_helper import Sandbox

from tenfold_tweak import cleaner, cli, desktop, detect, image, system
from tenfold_tweak import root_helper as rh

REAL_OPEN_INODES = cleaner.open_inodes
REAL_RUN_TOOL = rh.run_tool
HEX = "a" * 64


def est(cid, admin=None, size=0):
    level = cleaner.BY_ID[cid].level
    return cleaner.Estimate(cid, size, 0, admin=level != "user" if admin is None else admin)


def pv(*ests, token=None):
    return cleaner.Preview(list(ests), 0, False, token)


def root_results(ids, status="cleaned", reason=""):
    return {i: {"status": status, "freed": 0, "removed": 0, "skipped": {"in-use": 0, "other-fs": 0},
                "reason": reason} for i in ids}


class SymlinkedRootTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("cleaner-")
        self.data, self.cache = os.path.join(self.d, "data"), os.path.join(self.d, "cache")
        os.makedirs(self.data)
        os.makedirs(self.cache)
        self.victim = os.path.join(self.d, "victim")
        for p in ("files/a", "info/a.trashinfo", "expunged/b", "thumbnails/normal/t.png", "keep"):
            write(os.path.join(self.victim, p), b"x" * 10)
        for name, value in (("data_dir", self.data), ("cache_dir", self.cache)):
            patch = mock.patch.object(cleaner, name, lambda v=value: v)
            patch.start()
            self.addCleanup(patch.stop)

    def victim_files(self):
        return sorted(os.path.relpath(os.path.join(r, f), self.victim)
                      for r, _, fs in os.walk(self.victim) for f in fs)

    def clean(self, cid):
        before = self.victim_files()
        report = cleaner.clean([cid], cleaner.preview([cid]))
        self.assertEqual(self.victim_files(), before)
        return report.items[0]

    def test_symlinked_trash_deletes_nothing(self):
        os.symlink(self.victim, os.path.join(self.data, "Trash"))
        r = self.clean("trash")
        self.assertEqual((r.status, r.reason), ("skipped", cleaner.NOT_DIR))

    def test_symlinked_trash_subdir_deletes_nothing(self):
        trash = os.path.join(self.data, "Trash")
        write(os.path.join(trash, "info", "own.trashinfo"), b"y")
        os.symlink(os.path.join(self.victim, "files"), os.path.join(trash, "files"))
        self.assertEqual(self.clean("trash").status, "skipped")
        self.assertTrue(os.path.exists(os.path.join(trash, "info", "own.trashinfo")))

    def test_symlinked_cache_base_deletes_no_thumbnails(self):
        os.rmdir(self.cache)
        os.symlink(self.victim, self.cache)
        self.assertEqual(self.clean("thumbnails").status, "skipped")

    def test_real_trash_is_cleaned(self):
        trash = os.path.join(self.data, "Trash")
        write(os.path.join(trash, "files", "a"), b"z")
        write(os.path.join(trash, "info", "a.trashinfo"), b"z")
        r = cleaner.clean(["trash"], cleaner.preview(["trash"])).items[0]
        self.assertEqual((r.status, r.removed), ("cleaned", 2))
        self.assertEqual(os.listdir(os.path.join(trash, "files")), [])


class ToolLeftoversTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.img_dir = self.p["LIGHTDM_IMAGE_DIR"]
        self.active = write(os.path.join(self.img_dir, "login.png"), b"a")
        self.stale = write(os.path.join(self.img_dir, "login.jpg"), b"b")

    def conf(self, text):
        write(self.p["LIGHTDM_CONF"], text.encode())

    def test_restore_aside_never_listed(self):
        write(self.p["KALI_LOGIN_BG"], b"stock")
        aside = write(self.p["KALI_LOGIN_BG"] + rh.RESTORE_SUFFIX, b"only copy")
        self.assertNotIn(aside, rh.tool_leftovers())

    def test_active_image_matched_by_realpath(self):
        unnormalized = os.path.join(self.img_dir, "..", os.path.basename(self.img_dir), "login.png")
        self.conf(f"[greeter]\nbackground = {unnormalized.replace('/', '//')}\n")
        found = rh.tool_leftovers()
        self.assertNotIn(self.active, found)
        self.assertIn(self.stale, found)

    def test_malformed_conf_keeps_every_login_image(self):
        self.conf("background = /x\n")  # no section header
        found = rh.tool_leftovers()
        self.assertNotIn(self.active, found)
        self.assertNotIn(self.stale, found)


class CategoryDefaultsTest(unittest.TestCase):
    def test_defaults_on_and_opt_in_off(self):
        self.assertEqual(cleaner.default_ids(), ["apt-cache", "journal", "thumbnails", "tool-images"])
        self.assertEqual([c.id for c in cleaner.CATEGORIES if not c.default], ["cache", "trash", "autoremove"])

    def test_no_kernel_category_and_disk_not_ram(self):
        self.assertFalse([c for c in cleaner.CATEGORIES if "kernel" in (c.id + c.label).lower()])
        self.assertIn("not RAM", cleaner.DISK_NOTE)
        self.assertEqual(cleaner.BY_ID["trash"].warning, cleaner.TRASH_WARNING)

    def test_unknown_id_rejected(self):
        with self.assertRaises(ValueError):
            cleaner.preview(["kernels"])


class CliSelectionTest(unittest.TestCase):
    def setUp(self):
        self.ids = None
        self.clean = mock.Mock(return_value=cleaner.Report([cleaner.CleanResult("trash", "cleaned")], 0))

        def fake_preview(ids):
            self.ids = list(ids)
            return pv(*(est(i) for i in ids))
        for name, value in (("preview", fake_preview), ("clean", self.clean)):
            p = mock.patch.object(cleaner, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["clean", *argv])
        return rc, out.getvalue()

    def test_no_flags_uses_defaults(self):
        self.run_cli("--yes")
        self.assertEqual(self.ids, cleaner.default_ids())

    def test_explicit_flag_only_that_category(self):
        self.run_cli("--trash", "--yes")
        self.assertEqual(self.ids, ["trash"])

    def test_defaults_flag_adds_defaults(self):
        self.run_cli("--autoremove", "--defaults", "--yes")
        self.assertEqual(self.ids, cleaner.default_ids() + ["autoremove"])

    def test_kernel_flag_does_not_exist(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["clean", "--kernel"])

    def test_non_tty_without_yes_deletes_nothing(self):
        rc, out = self.run_cli("--trash")
        self.assertEqual(rc, 0)
        self.clean.assert_not_called()
        self.assertIn("nothing deleted", out)
        self.assertIn(cleaner.DISK_NOTE, out)

    def test_tty_trash_needs_second_confirmation(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        out = Tty()
        with mock.patch.object(sys, "stdin", Tty()), mock.patch("builtins.input", side_effect=["y", "n"]) as ask, \
                redirect_stdout(out):
            cli.main(["clean", "--trash"])
        self.clean.assert_not_called()
        self.assertIn(cleaner.TRASH_WARNING, ask.call_args_list[1].args[0])


class UserWalkTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("cleaner-walk-")
        self.cache, self.data = os.path.join(self.d, "cache"), os.path.join(self.d, "data")
        os.makedirs(self.cache)
        os.makedirs(self.data)
        self.outside = write(os.path.join(self.d, "Documents", "precious.txt"), b"keep me")
        self.in_use = set()
        for name, value in (("cache_dir", lambda: self.cache), ("data_dir", lambda: self.data),
                            ("open_inodes", lambda: self.in_use)):
            p = mock.patch.object(cleaner, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_clean(self, *ids):
        with mock.patch.object(cleaner.system, "run_clean") as run:
            report = cleaner.clean(list(ids), cleaner.preview(list(ids)))
        run.assert_not_called()  # user categories never ask for admin
        return {r.id: r for r in report.items}

    def test_symlinks_removed_as_links_targets_intact(self):
        os.symlink(self.outside, os.path.join(self.cache, "file-link"))
        os.symlink(os.path.dirname(self.outside), os.path.join(self.cache, "dir-link"))
        r = self.run_clean("cache")["cache"]
        self.assertEqual((r.status, r.removed), ("cleaned", 2))
        self.assertEqual(os.listdir(self.cache), [])
        with open(self.outside, "rb") as f:
            self.assertEqual(f.read(), b"keep me")

    def test_other_filesystem_not_entered(self):
        kept = write(os.path.join(self.cache, "mnt", "data.bin"), b"m")
        write(os.path.join(self.cache, "junk"), b"j")
        real_stat = os.stat

        def fake_stat(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            if path == "mnt" and kw.get("dir_fd") is not None:
                fields = list(st)
                fields[2] = st.st_dev + 1  # st_dev
                return os.stat_result(fields)
            return st
        with mock.patch.object(os, "stat", fake_stat):
            r = self.run_clean("cache")["cache"]
        self.assertTrue(os.path.exists(kept))
        self.assertEqual((r.removed, r.skipped["other-fs"]), (1, 1))

    def test_in_use_file_skipped(self):
        busy = write(os.path.join(self.cache, "busy.db"), b"b" * 100)
        write(os.path.join(self.cache, "idle"), b"i")
        with open(busy, "rb"):
            self.in_use = REAL_OPEN_INODES()
            r = self.run_clean("cache")["cache"]
        self.assertTrue(os.path.exists(busy))
        self.assertEqual((r.removed, r.skipped["in-use"]), (1, 1))
        self.assertIn("1 skipped (in use)", r.line())

    def test_hardlink_and_thumbnails_counted_once(self):
        a = write(os.path.join(self.cache, "app", "a"), b"x" * 9000)
        os.link(a, os.path.join(self.cache, "app", "b"))
        os.makedirs(os.path.join(self.cache, "thumbnails", "normal"))
        os.link(a, os.path.join(self.cache, "thumbnails", "normal", "t.png"))
        self.assertEqual(cleaner.preview(["thumbnails", "cache"]).total, os.stat(a).st_blocks * 512)
        self.assertEqual(cleaner.preview(["cache"]).items[0].count, 2)  # thumbnails excluded from ~/.cache

    def test_category_roots_never_deleted(self):
        write(os.path.join(self.cache, "thumbnails", "large", "t.png"), b"t")
        write(os.path.join(self.cache, "app", "x"), b"x")
        for d in ("files", "info"):
            write(os.path.join(self.data, "Trash", d, "a"), b"a")
        self.run_clean("thumbnails", "cache", "trash")
        self.assertEqual(os.listdir(os.path.join(self.cache, "thumbnails")), [])
        self.assertEqual(os.listdir(self.cache), ["thumbnails"])
        for d in ("files", "info"):
            self.assertEqual(os.listdir(os.path.join(self.data, "Trash", d)), [])
        self.assertTrue(os.path.exists(self.outside))

    def test_one_category_failure_does_not_stop_others(self):
        write(os.path.join(self.data, "Trash", "files", "a"), b"a")
        real_scan = cleaner._scan

        def flaky(cid, *a, **kw):
            if cid == "cache" and kw.get("delete"):
                raise OSError("boom")
            return real_scan(cid, *a, **kw)
        with mock.patch.object(cleaner, "_scan", flaky):
            r = self.run_clean("cache", "trash")
        self.assertEqual((r["cache"].status, r["cache"].reason), ("failed", "boom"))
        self.assertEqual((r["trash"].status, r["trash"].removed), ("cleaned", 1))

    def test_xdg_cache_pointing_at_user_dir_falls_back(self):
        home = self.d
        for bad in (os.path.join(home, "Documents"), home, "/tmp", "relative/cache"):
            with self.subTest(bad), mock.patch.dict(os.environ, {"HOME": home, "XDG_CACHE_HOME": bad}):
                self.assertEqual(cleaner._xdg_base("XDG_CACHE_HOME", ".cache"), os.path.join(home, ".cache"))

    def test_xdg_base_overlapping_other_base_falls_back(self):
        home = self.d
        j = lambda *p: os.path.join(home, *p)
        dirs = lambda: (cleaner._xdg_base("XDG_CACHE_HOME", ".cache"),
                        cleaner._xdg_base("XDG_DATA_HOME", os.path.join(".local", "share")))
        clean = {"HOME": home, "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "", "XDG_STATE_HOME": "", "XDG_CACHE_HOME": ""}
        cases = [  # (env overrides, cache_dir expected, data_dir expected)
            ({"XDG_CACHE_HOME": j(".local")}, j(".cache"), j(".local", "share")),
            ({"XDG_CACHE_HOME": j(".config")}, j(".cache"), j(".local", "share")),
            ({"XDG_CACHE_HOME": j(".local", "share", "c")}, j(".cache"), j(".local", "share")),
            ({"XDG_CACHE_HOME": j(".local", "state")}, j(".cache"), j(".local", "share")),
            ({"XDG_DATA_HOME": j(".config")}, j(".cache"), j(".local", "share")),
            ({"XDG_DATA_HOME": j(".cache", "d")}, j(".cache"), j(".local", "share")),
            ({"XDG_DATA_HOME": j("d"), "XDG_CACHE_HOME": j("d", "c")}, j(".cache"), j(".local", "share")),
            ({"XDG_CONFIG_HOME": j("cfg"), "XDG_CACHE_HOME": j("cfg")}, j(".cache"), j(".local", "share")),
        ]
        for env, cache, data in cases:
            with self.subTest(env), mock.patch.dict(os.environ, {**clean, **env}):
                self.assertEqual(dirs(), (cache, data))
        with mock.patch.dict(os.environ, {**clean, "XDG_CACHE_HOME": j("mycache"),
                                          "XDG_DATA_HOME": j(".local", "share")}):
            self.assertEqual(dirs(),
                             (os.path.realpath(j("mycache")), os.path.realpath(j(".local", "share"))))


class AdminFlowTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("cleaner-admin-")
        self.data = os.path.join(self.d, "data")
        self.trashed = write(os.path.join(self.data, "Trash", "files", "old"), b"o")
        for name, value in (("data_dir", lambda: self.data), ("cache_dir", lambda: os.path.join(self.d, "c")),
                            ("open_inodes", lambda: set()), ("_user_copies", lambda: [])):
            p = mock.patch.object(cleaner, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_user_only_never_asks_admin(self):
        p = pv(est("trash"), est("tool-images", admin=False))
        self.assertFalse(cleaner.needs_admin(["trash", "tool-images"], p))
        with mock.patch.object(cleaner.system, "run_clean") as run:
            cleaner.clean(["trash", "tool-images"], p)
        run.assert_not_called()

    def test_system_part_runs_once_in_order_with_token(self):
        ids = ["autoremove", "trash", "journal", "apt-cache"]
        p = pv(*(est(i) for i in ids), token=HEX)
        with mock.patch.object(cleaner.system, "run_clean", side_effect=lambda i, e: root_results(i)) as run:
            report = cleaner.clean(ids, p)
        run.assert_called_once_with(["apt-cache", "journal", "autoremove"], HEX)
        self.assertEqual([r.id for r in report.items], ["apt-cache", "journal", "trash", "autoremove"])

    def test_no_token_without_autoremove(self):
        with mock.patch.object(cleaner.system, "run_clean", side_effect=lambda i, e: root_results(i)) as run:
            cleaner.clean(["journal"], pv(est("journal"), token=HEX))
        run.assert_called_once_with(["journal"], None)

    def test_pkexec_cancel_skips_system_user_proceeds(self):
        for rc in (126, 127):
            with self.subTest(rc=rc):
                trashed = write(self.trashed, b"o")
                calls = []

                def fake_run(cmd, *a, **kw):
                    calls.append(cmd)
                    return subprocess.CompletedProcess(cmd, rc, "", "")
                ids = ["apt-cache", "journal", "tool-images", "trash"]
                with mock.patch.object(system, "helper_problem", lambda path=None: None), \
                        mock.patch.object(system.subprocess, "run", fake_run):
                    report = cleaner.clean(ids, pv(*(est(i) for i in ids)))
                r = {x.id: x for x in report.items}
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], system.PKEXEC)
                self.assertEqual(calls[0][2:], ["clean", "apt-cache", "journal", "tool-images"])
                for cid in ("apt-cache", "journal"):
                    self.assertEqual((r[cid].status, r[cid].reason), ("skipped", "admin cancelled"))
                    self.assertEqual(r[cid].line(), f"{cid}: skipped (admin cancelled)")
                self.assertIn("admin cancelled", r["tool-images"].reason)
                self.assertEqual(r["tool-images"].freed, 0)
                self.assertEqual(r["trash"].status, "cleaned")
                self.assertFalse(os.path.exists(trashed))

    def test_system_failure_isolated(self):
        def fake(ids, expect):
            out = root_results(ids)
            out["apt-cache"] = dict(out["apt-cache"], status="failed", reason="apt-get exited with 100")
            return out
        with mock.patch.object(cleaner.system, "run_clean", fake):
            ids = ["apt-cache", "journal", "trash"]
            report = cleaner.clean(ids, pv(*(est(i) for i in ids)))
        self.assertEqual([r.status for r in report.items], ["failed", "cleaned", "cleaned"])


class UserToolImagesTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("cleaner-copies-")
        self.copies, self.legacy = os.path.join(self.d, "copies"), os.path.join(self.d, "legacy")
        self.active = write(os.path.join(self.copies, image.COPY_PREFIX + "active.png"), b"a")
        self.stale = write(os.path.join(self.copies, image.COPY_PREFIX + "old.png"), b"s")
        self.login = write(os.path.join(self.legacy, image.LOGIN_PREFIX + "old.png"), b"l")
        self.other = write(os.path.join(self.copies, "notes.txt"), b"n")
        self.source = write(os.path.join(self.d, "Pictures", "chosen.png"), b"c")
        patches = [
            mock.patch.object(image, "COPY_DIR", self.copies),
            mock.patch.object(image, "LEGACY_COPY_DIR", self.legacy),
            mock.patch.object(detect, "detect", lambda: SimpleNamespace(de="gnome", lock_provider_dir=None)),
            mock.patch.object(desktop, "current_paths", lambda de, lock=None: {self.active, self.source}),
            mock.patch.object(cleaner, "open_inodes", lambda: set()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_only_unreferenced_copies_removed(self):
        with mock.patch.object(cleaner.system, "run_clean") as run:
            report = cleaner.clean(["tool-images"], pv(est("tool-images", admin=False)))
        run.assert_not_called()
        r = report.items[0]
        self.assertEqual((r.status, r.removed), ("cleaned", 2))
        for kept in (self.active, self.other, self.source):
            self.assertTrue(os.path.exists(kept), kept)
        self.assertFalse(os.path.exists(self.stale) or os.path.exists(self.login))

    def test_unreadable_settings_keep_everything(self):
        with mock.patch.object(desktop, "current_paths", side_effect=RuntimeError("dconf")):
            r = cleaner.clean(["tool-images"], pv(est("tool-images", admin=False))).items[0]
        self.assertEqual(r.status, "skipped")
        self.assertTrue(os.path.exists(self.stale))


class RootCleanArgsTest(unittest.TestCase):
    BAD = [[], ["cache"], ["trash"], ["thumbnails"], ["apt-cache", "apt-cache"], ["/var/cache/apt"],
           ["apt-cache;id"], ["--expect-autoremove", HEX], ["apt-cache", "--expect-autoremove"],
           ["apt-cache", "--expect-autoremove", "A" * 64], ["apt-cache", "--expect-autoremove", "a" * 63],
           ["apt-cache", "--expect-autoremove", HEX, "--expect-autoremove", HEX]]

    def test_bad_argv_is_usage_error(self):
        for argv in self.BAD:
            with self.subTest(argv=argv), self.assertRaises(rh.HelperError):
                rh.clean_args(argv)

    def test_valid_argv(self):
        self.assertEqual(rh.clean_args(["journal", "--expect-autoremove", HEX, "autoremove"]),
                         (["journal", "autoremove"], HEX))

    def test_main_rejects_before_any_lock(self):
        lock = mock.Mock(side_effect=AssertionError("lock taken"))
        with mock.patch.object(rh.os, "geteuid", lambda: 0), \
                mock.patch.object(rh, "maintscript_problem", lambda: None), \
                mock.patch.object(rh, "action_lock", lock), \
                mock.patch.object(rh, "ensure_state_dir", mock.Mock(side_effect=AssertionError("state dir"))):
            for argv in (["clean"], ["clean", "bogus"], ["clean", "apt-cache", "apt-cache"],
                         ["clean", "apt-cache", "--expect-autoremove", "zz"]):
                with self.subTest(argv=argv):
                    err = io.StringIO()
                    with redirect_stderr(err), redirect_stdout(io.StringIO()):
                        self.assertEqual(rh.main(argv), rh.EXIT_FAIL)
                    self.assertIn("usage", err.getvalue())
        lock.assert_not_called()


class RootCleanTest(unittest.TestCase):
    APT_SIM = "\n".join("Remv " + n + " [1.0]" for n in (
        "linux-image-6.12.38+kali-amd64", "linux-image-6.11.2-amd64", "linux-headers-6.11.2-common",
        "linux-modules-6.11.2-amd64", "linux-modules-extra-6.11.2", "linux-kbuild-6.11", "linux-binary-6.11.2",
        "linux-base-6.11", "tenfold-tweaks", "tenfold-tweaks:all", "acme-dkms-6.12.38+kali-amd64",
        "libfoo1", "python3-bar"))

    def setUp(self):
        self.calls = []
        p = mock.patch.object(rh, "run_tool", self.fake_tool)
        p.start()
        self.addCleanup(p.stop)

    def fake_tool(self, cmd, *a, **kw):
        self.calls.append(cmd)
        return self.APT_SIM if cmd[1:3] == ["-s", "autoremove"] else ""

    def plan(self):
        with mock.patch.object(rh.os, "uname", lambda: SimpleNamespace(release="6.12.38+kali-amd64")):
            return rh.autoremove_plan()

    def test_autoremove_plan_excludes_kernels_and_self(self):
        pkgs, token = self.plan()
        self.assertEqual(pkgs, ["libfoo1", "python3-bar"])
        self.assertEqual(token, hashlib.sha256(b"libfoo1\npython3-bar").hexdigest())

    def test_autoremove_plan_changed_removes_nothing(self):
        with mock.patch.object(rh, "dpkg_busy", lambda: False):
            r = rh.clean_autoremove("0" * 64)
        self.assertEqual((r["status"], r["reason"]), ("skipped", rh.PLAN_CHANGED))
        self.assertFalse([c for c in self.calls if "-y" in c])

    def test_autoremove_removes_only_plan(self):
        _pkgs, token = self.plan()

        def tool(cmd, *a, **kw):
            if cmd[1:3] == ["-s", "remove"]:
                self.calls.append(cmd)
                return "Remv libfoo1 [1]\nRemv python3-bar [2]\n"
            return self.fake_tool(cmd, *a, **kw)
        with mock.patch.object(rh, "dpkg_busy", lambda: False), mock.patch.object(rh, "run_tool", tool), \
                mock.patch.object(rh.os, "uname", lambda: SimpleNamespace(release="6.12.38+kali-amd64")):
            r = rh.clean_autoremove(token)
        self.assertEqual(r["status"], "cleaned")
        self.assertEqual([c for c in self.calls if "-y" in c],
                         [[rh.APT_GET, "-y", "remove", "--", "libfoo1", "python3-bar"]])

    def test_dpkg_busy_skips_apt_categories(self):
        with mock.patch.object(rh, "dpkg_busy", lambda: True):
            for fn in (rh.clean_apt_cache, rh.clean_autoremove):
                self.assertEqual(fn(HEX)["reason"], rh.PM_BUSY)
        self.assertEqual(self.calls, [])

    def test_apt_lock_message_maps_to_busy(self):
        locked = subprocess.CompletedProcess([], 100, "", "E: Could not get lock /var/lib/dpkg/lock-frontend")
        out = io.StringIO()
        with mock.patch.object(rh, "run_tool", REAL_RUN_TOOL), \
                mock.patch.object(rh.subprocess, "run", lambda *a, **kw: locked), \
                mock.patch.object(rh, "dpkg_busy", lambda: False), \
                mock.patch.object(rh, "apt_cache_files", lambda: ([], True)), redirect_stdout(out):
            self.assertEqual(rh.clean("apt-cache"), rh.EXIT_OK)
        self.assertEqual(json.loads(out.getvalue())["results"]["apt-cache"]["reason"], rh.PM_BUSY)

    def test_failure_isolated_per_category(self):
        def boom(_e):
            raise RuntimeError("disk on fire")
        out = io.StringIO()
        with mock.patch.dict(rh.CLEANERS, {"journal": boom, "apt-cache": lambda e: rh.clean_result(removed=3)}), \
                redirect_stdout(out):
            self.assertEqual(rh.clean("journal", "apt-cache"), rh.EXIT_OK)
        res = json.loads(out.getvalue())
        self.assertEqual(res["v"], 1)
        self.assertEqual(res["results"]["journal"]["status"], "failed")
        self.assertIn("disk on fire", res["results"]["journal"]["reason"])
        self.assertEqual(res["results"]["apt-cache"]["removed"], 3)

    def test_journal_uses_retention(self):
        with mock.patch.object(rh, "JOURNALCTL", sys.executable), mock.patch.object(rh, "journal_archives", list):
            rh.clean_journal(None)
        self.assertEqual(self.calls, [[sys.executable, "--vacuum-time=7d"]])



class RootToolImagesTest(Sandbox):
    def test_protected_files_never_touched(self):
        p, img = self.p, self.p["LIGHTDM_IMAGE_DIR"]
        active = write(os.path.join(img, "login.png"), b"active")
        write(p["LIGHTDM_CONF"], f"[greeter]\nbackground = {active}\n".encode())
        victim = write(os.path.join(self.d, "victim.png"), b"v")
        protected = [active, p["LIGHTDM_CONF"], victim, self.png,
                     write(p["GRESOURCE_ORIG"], b"orig"), write(p["GRESOURCE_META"], b"{}"),
                     write(p["LIGHTDM_ORIG"], b"conf"), write(os.path.join(self.state, "lock"), b""),
                     write(p["KALI_LOGIN_BG"], b"stock"), write(p["KALI_LOGIN_BG"] + rh.RESTORE_SUFFIX, b"only copy"),
                     write(os.path.join(p["RESCUE_DIR"], "login-1.png"), b"rescued")]
        link = os.path.join(os.path.dirname(p["GRESOURCE"]), rh.TMP_PREFIX + "link.tmp")
        os.symlink(victim, link)
        leftovers = [write(os.path.join(os.path.dirname(p["GRESOURCE"]), rh.TMP_PREFIX + "a.tmp"), b"1"),
                     write(os.path.join(os.path.dirname(p["KALI_LOGIN_BG"]), rh.TMP_PREFIX + "b.tmp"), b"2"),
                     write(os.path.join(img, ".login-staged.x"), b"3"),
                     write(os.path.join(img, "login.jpg"), b"4"),
                     write(os.path.join(self.state, "build-abc", "theme.gresource"), b"5")]
        r = rh.clean_tool_images(None)
        for f in protected:
            self.assertTrue(os.path.exists(f), f)
        self.assertTrue(os.path.islink(link))
        for f in leftovers:
            self.assertFalse(os.path.exists(f), f)
        self.assertFalse(os.path.exists(os.path.join(self.state, "build-abc")))
        self.assertEqual(r["removed"], 5)



class CopyDirSymlinkTest(unittest.TestCase):
    """Security cleaner 2.0.0 #1: the tool-images user part never follows a symlinked copy dir."""

    def setUp(self):
        self.d = scratch_dir("cleaner-copylink-")
        self.wp = os.path.join(self.d, "Pictures", "wp")
        self.victim = write(os.path.join(self.wp, image.COPY_PREFIX + "beach.jpg"), b"v")
        patches = [
            mock.patch.object(image, "LEGACY_COPY_DIR", os.path.join(self.d, "data", "backgrounds", "legacy")),
            mock.patch.object(detect, "detect", lambda: SimpleNamespace(de="gnome", lock_provider_dir=None)),
            mock.patch.object(desktop, "current_paths", lambda de, lock=None: set()),
            mock.patch.object(cleaner, "open_inodes", lambda: set()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def link_copy_dir(self, rel_link, target, copy_dir):
        os.makedirs(os.path.dirname(os.path.join(self.d, rel_link)), exist_ok=True)
        os.symlink(target, os.path.join(self.d, rel_link))
        p = mock.patch.object(image, "COPY_DIR", copy_dir)
        p.start()
        self.addCleanup(p.stop)

    def check_kept(self):
        r = cleaner.clean(["tool-images"], pv(est("tool-images", admin=False))).items[0]
        self.assertEqual((r.status, r.reason), ("skipped", cleaner.NOT_DIR))
        e = cleaner.preview(["tool-images"]).items[0]
        self.assertIn(cleaner.NOT_DIR, e.note)
        image.cleanup_copies(set())
        self.assertTrue(os.path.exists(self.victim))

    def test_symlinked_copy_dir_skipped(self):
        copy_dir = os.path.join(self.d, "data", "backgrounds", "tenfold-tweaks")
        self.link_copy_dir(os.path.join("data", "backgrounds", "tenfold-tweaks"), self.wp, copy_dir)
        with mock.patch.object(rh, "tool_leftovers", lambda: []):
            self.check_kept()

    def test_symlinked_backgrounds_component_skipped(self):
        self.link_copy_dir(os.path.join("data", "backgrounds"), os.path.join(self.d, "Pictures"),
                           os.path.join(self.d, "data", "backgrounds", "wp"))
        with mock.patch.object(rh, "tool_leftovers", lambda: []):
            self.check_kept()

    def test_symlinked_file_in_copy_dir_kept(self):
        copies = os.path.join(self.d, "data", "backgrounds", "tenfold-tweaks")
        stale = write(os.path.join(copies, image.COPY_PREFIX + "old.png"), b"s")
        os.symlink(self.victim, os.path.join(copies, image.COPY_PREFIX + "link.jpg"))
        with mock.patch.object(image, "COPY_DIR", copies):
            self.assertEqual(image.unreferenced_copies(set()), [stale])
            image.cleanup_copies(set())
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.islink(os.path.join(copies, image.COPY_PREFIX + "link.jpg")))
        self.assertTrue(os.path.exists(self.victim))


class XdgBaseAllowlistTest(unittest.TestCase):
    """Security cleaner 2.0.0 #2: hidden top-level dirs only as the spec default; fallback checked too."""

    def setUp(self):
        self.home = scratch_dir("cleaner-xdg-")
        self.env = {"HOME": self.home, "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
                    "XDG_DATA_HOME": "", "XDG_STATE_HOME": "", "XDG_CACHE_HOME": ""}

    def j(self, *p):
        return os.path.join(self.home, *p)

    def test_hidden_dotdirs_rejected(self):
        for bad in (self.j(".ssh"), self.j(".gnupg"), self.j(".password-store"), self.j(".mozilla", "c")):
            with self.subTest(bad), mock.patch.dict(os.environ, {**self.env, "XDG_CACHE_HOME": bad}):
                self.assertEqual(cleaner.cache_dir(), self.j(".cache"))

    def test_inside_spec_default_accepted(self):
        with mock.patch.dict(os.environ, {**self.env, "XDG_CACHE_HOME": self.j(".cache", "sub")}):
            self.assertEqual(cleaner.cache_dir(), os.path.realpath(self.j(".cache", "sub")))

    def test_fallback_holding_user_dir_skipped(self):
        write(self.j(".config", "user-dirs.dirs"), b'XDG_DOWNLOAD_DIR="$HOME/.cache/dl"\n')
        with mock.patch.dict(os.environ, {**self.env, "XDG_CACHE_HOME": self.j(".ssh")}):
            with self.assertRaises(cleaner._NotDir):
                cleaner.cache_dir()
            e = cleaner.preview(["cache"]).items[0]
        self.assertTrue(e.note.startswith("skipped (") and "user directory" in e.note, e.note)


class AutoremoveSimulationTest(unittest.TestCase):
    """Security cleaner 2.0.0 #3: Inst/foreign Conf in the simulation skips; timeout never SIGKILLs apt."""

    def test_inst_or_foreign_conf_skips(self):
        for extra in ("Inst libnew (1.0 Debian:stable)\n", "Conf other-pkg (1.0)\n"):
            calls = []

            def tool(cmd, *a, extra=extra, **kw):
                calls.append(cmd)
                if cmd[1:3] == ["-s", "remove"]:
                    return "Remv libfoo1 [1]\n" + extra
                return ""
            with self.subTest(extra), mock.patch.object(rh, "dpkg_busy", lambda: False), \
                    mock.patch.object(rh, "autoremove_plan", lambda: (["libfoo1"], HEX)), \
                    mock.patch.object(rh, "run_tool", tool):
                r = rh.clean_autoremove(HEX)
            self.assertEqual(r["status"], "skipped")
            self.assertIn(rh.PLAN_CHANGED, r["reason"])
            self.assertFalse([c for c in calls if "-y" in c])

    def test_timeout_sends_sigterm_and_points_to_dpkg_configure(self):
        marker = os.path.join(scratch_dir("cleaner-term-"), "term")
        script = f"trap 'echo term > {marker}; exit 0' TERM; sleep 2 & wait"
        with self.assertRaises(rh.HelperError) as cm:
            REAL_RUN_TOOL(["/bin/sh", "-c", script], 0.3, gentle=True)
        self.assertIn("dpkg --configure -a", str(cm.exception))
        with open(marker, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "term")


if __name__ == "__main__":
    unittest.main()
