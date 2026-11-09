"""GDM login on Kali's patched gnome-shell: hardcoded inline background replaced via dpkg-divert."""

import os
import shutil
import subprocess
import unittest
from unittest import mock

from tests.helpers import write
from tests.test_root_helper import STOCK, Sandbox, read

from tenfold_tweak import root_helper as rh



def needle(path: str) -> bytes:
    # The line Kali's gnome-shell carries in ui/screenShield.js.
    return b'set_style(\'background-image: url("' + path.encode() + b'"); background-size: cover;\')'


class FakeDivert:
    """Stateful stand-in for dpkg-divert --local --rename (never runs the real tool)."""

    def __init__(self):
        self.diverted = False
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "--truename":
            return rh.KALI_LOGIN_BG_DIVERTED if self.diverted else rh.KALI_LOGIN_BG
        assert args[:4] == ("--local", "--rename", "--divert", rh.KALI_LOGIN_BG_DIVERTED), args
        assert args[5] == rh.KALI_LOGIN_BG
        path, diverted = rh.KALI_LOGIN_BG, rh.KALI_LOGIN_BG_DIVERTED
        if args[4] == "--add":
            assert not self.diverted, "diverted twice"
            if os.path.lexists(path):
                os.rename(path, diverted)
            self.diverted = True
        else:
            if os.path.lexists(diverted) and not os.path.lexists(path):
                os.rename(diverted, path)
            self.diverted = False
        return ""


class ConstantsTest(unittest.TestCase):
    def test_hardcoded_paths(self):
        self.assertEqual(rh.KALI_LOGIN_BG, "/usr/share/desktop-base/kali-theme/login/background-blurred")
        self.assertEqual(rh.DPKG_DIVERT, "/usr/bin/dpkg-divert")
        self.assertEqual(rh.SHELL_LIBS, "/usr/lib/gnome-shell/libshell-*.so")

    def test_divert_command_shape(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"], seen["env"] = cmd, kwargs.get("env")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(rh.subprocess, "run", fake_run):
            rh.dpkg_divert(*rh.divert_args("--add"))
        self.assertEqual(seen["cmd"], ["/usr/bin/dpkg-divert", "--local", "--rename", "--divert",
                                       rh.KALI_LOGIN_BG + ".tenfold-tweak", "--add", rh.KALI_LOGIN_BG])
        self.assertNotIn("HOME", seen["env"])

    def test_divert_failure_reason(self):
        with mock.patch.object(rh.subprocess, "run",
                               lambda cmd, **k: subprocess.CompletedProcess(cmd, 2, "", "dpkg-divert: error: boom\n")):
            with self.assertRaises(rh.HelperError) as cm:
                rh.dpkg_divert("--truename", rh.KALI_LOGIN_BG)
        self.assertIn("boom", str(cm.exception))


@unittest.skipUnless(os.path.isfile(STOCK), "stock gnome-shell theme not installed")
class KaliGdmTest(Sandbox):
    def setUp(self):
        super().setUp()
        shutil.copyfile(STOCK, self.p["GRESOURCE"])
        self.stock_bytes = read(STOCK)
        self.lib = write(os.path.join(self.d, "usr-lib", "libshell-18.so"), b"\x7fELF..." + needle(self.p["KALI_LOGIN_BG"]) + b"...")
        login_dir = os.path.dirname(self.p["KALI_LOGIN_BG"])
        self.original = write(os.path.join(login_dir, "login-blurred.jpg"), b"\xff\xd8\xff kali")
        os.symlink("login-blurred.jpg", self.p["KALI_LOGIN_BG"])
        self.fake = FakeDivert()
        for p in (mock.patch.object(rh, "dpkg_divert", self.fake),
                  mock.patch.object(rh, "check_root_dir", lambda path: None)):
            p.start()
            self.addCleanup(p.stop)

    def assertOriginalBack(self):
        self.assertTrue(os.path.islink(self.p["KALI_LOGIN_BG"]))
        self.assertEqual(os.readlink(self.p["KALI_LOGIN_BG"]), "login-blurred.jpg")
        self.assertFalse(os.path.lexists(self.p["KALI_LOGIN_BG_DIVERTED"]))
        self.assertFalse(self.fake.diverted)

    def test_detects_inline_override(self):
        self.assertTrue(rh.kali_override_active())
        write(self.lib, b"\x7fELF upstream shell without the patch")
        self.assertFalse(rh.kali_override_active())

    def test_apply_replaces_hardcoded_image_and_leaves_theme_stock(self):
        rh.gdm_apply(self.png)
        path = self.p["KALI_LOGIN_BG"]
        self.assertFalse(os.path.islink(path))
        self.assertEqual(read(path), read(self.png))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        self.assertEqual(os.readlink(self.p["KALI_LOGIN_BG_DIVERTED"]), "login-blurred.jpg")
        self.assertEqual(read(self.p["GRESOURCE"]), self.stock_bytes)
        self.assertTrue(self.fake.diverted)

    def test_image_survives_source_deletion(self):
        rh.gdm_apply(self.png)
        expected = read(self.png)
        os.unlink(self.png)
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), expected)

    def test_reapply_diverts_once_and_replaces_image(self):
        rh.gdm_apply(self.png)
        rh.gdm_apply(self.jpg)
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), read(self.jpg))
        self.assertEqual(sum(1 for c in self.fake.calls if "--add" in c), 1)

    def test_restore_puts_original_back_then_nothing_to_restore(self):
        rh.gdm_apply(self.png)
        self.assertEqual(rh.gdm_restore(), rh.EXIT_OK)
        self.assertOriginalBack()
        self.assertEqual(read(self.p["GRESOURCE"]), self.stock_bytes)
        self.assertEqual(rh.gdm_restore(), rh.EXIT_NOTHING)
        self.assertOriginalBack()

    def test_apply_reverts_theme_patch_from_older_version(self):
        with mock.patch.object(rh, "kali_override_active", lambda: False):
            rh.gdm_apply(self.png)  # what earlier versions did
        self.assertNotEqual(read(self.p["GRESOURCE"]), self.stock_bytes)
        rh.gdm_apply(self.jpg)
        self.assertEqual(read(self.p["GRESOURCE"]), self.stock_bytes)
        self.assertFalse(os.path.exists(self.p["GRESOURCE_ORIG"]))
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), read(self.jpg))

    def test_restore_undoes_both_when_both_present(self):
        with mock.patch.object(rh, "kali_override_active", lambda: False):
            rh.gdm_apply(self.png)
        rh.kali_login_apply(read(self.jpg))
        self.assertEqual(rh.gdm_restore(), rh.EXIT_OK)
        self.assertOriginalBack()
        self.assertEqual(read(self.p["GRESOURCE"]), self.stock_bytes)

    def test_failed_first_write_undoes_divert(self):
        real = rh.atomic_write

        def failing(dest, *a, **k):
            if dest == self.p["KALI_LOGIN_BG"]:
                raise OSError(28, "No space left on device")
            return real(dest, *a, **k)
        with mock.patch.object(rh, "atomic_write", failing):
            with self.assertRaises(OSError):
                rh.gdm_apply(self.png)
        self.assertOriginalBack()

    def test_failed_rewrite_keeps_previous_image(self):
        rh.gdm_apply(self.png)
        with mock.patch.object(rh, "atomic_write", side_effect=OSError(28, "full")):
            with self.assertRaises(OSError):
                rh.gdm_apply(self.jpg)
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), read(self.png))
        self.assertTrue(self.fake.diverted)

    def test_oversized_image_rejected_before_any_change(self):
        from tests.helpers import png_header_only
        big = write(os.path.join(self.d, "big.png"), png_header_only(5000, 10))
        with self.assertRaises(rh.HelperError):
            rh.gdm_apply(big)
        self.assertOriginalBack()
        self.assertEqual(self.fake.calls, [])


@unittest.skipUnless(os.path.isfile(STOCK), "stock gnome-shell theme not installed")
class KaliRealShapeTest(Sandbox):
    """QA: the on-disk shape seen on Kali (read-only check): background-blurred is a relative symlink
    ../../../backgrounds/kali/login-blurred owned by kali-themes-common, pointing at a JPEG owned by
    kali-wallpapers-2026. Neither file may be written through."""

    LINK = "../../../backgrounds/kali/login-blurred"

    def setUp(self):
        super().setUp()
        shutil.copyfile(STOCK, self.p["GRESOURCE"])
        self.stock_bytes = read(STOCK)
        root = os.path.join(self.d, "root", "usr", "share")
        bg = os.path.join(root, "desktop-base", "kali-theme", "login", "background-blurred")
        paths = {"KALI_LOGIN_BG": bg, "KALI_LOGIN_BG_DIVERTED": bg + ".tenfold-tweak"}
        p = mock.patch.multiple(rh, **paths)
        p.start()
        self.addCleanup(p.stop)
        self.p.update(paths)
        self.wallpaper = write(os.path.join(root, "backgrounds", "kali", "login-blurred"), b"\xff\xd8\xff kali jpeg")
        self.wallpaper_bytes = read(self.wallpaper)
        os.makedirs(os.path.dirname(bg))
        os.symlink(self.LINK, bg)
        write(os.path.join(self.d, "usr-lib", "libshell-18.so"), b"\x7fELF" + needle(bg))
        self.fake = FakeDivert()
        for q in (mock.patch.object(rh, "dpkg_divert", self.fake),
                  mock.patch.object(rh, "check_root_dir", lambda path: None)):
            q.start()
            self.addCleanup(q.stop)

    def assertStockLink(self, at=None):
        at = at or self.p["KALI_LOGIN_BG"]
        self.assertTrue(os.path.islink(at))
        self.assertEqual(os.readlink(at), self.LINK)
        self.assertEqual(read(at), self.wallpaper_bytes)  # still resolves to the package JPEG

    def assertWallpaperUntouched(self):
        self.assertFalse(os.path.islink(self.wallpaper))
        self.assertEqual(read(self.wallpaper), self.wallpaper_bytes)

    def test_round_trip_keeps_relative_link_and_package_jpeg(self):
        rh.gdm_apply(self.png)
        self.assertTrue(self.fake.diverted)
        self.assertFalse(os.path.islink(self.p["KALI_LOGIN_BG"]))
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), read(self.png))
        self.assertStockLink(self.p["KALI_LOGIN_BG_DIVERTED"])
        self.assertWallpaperUntouched()
        self.assertEqual(rh.gdm_restore(), rh.EXIT_OK)
        self.assertFalse(self.fake.diverted)
        self.assertStockLink()
        self.assertFalse(os.path.lexists(self.p["KALI_LOGIN_BG_DIVERTED"]))
        self.assertWallpaperUntouched()
        self.assertEqual(read(self.p["GRESOURCE"]), self.stock_bytes)

    def test_reapply_twice_keeps_original_backup(self):
        rh.gdm_apply(self.png)
        rh.gdm_apply(self.jpg)
        rh.gdm_apply(self.jpg)
        self.assertEqual(sum(1 for c in self.fake.calls if "--add" in c), 1)
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), read(self.jpg))
        self.assertStockLink(self.p["KALI_LOGIN_BG_DIVERTED"])
        self.assertEqual(rh.gdm_restore(), rh.EXIT_OK)
        self.assertStockLink()

    def test_restore_when_nothing_applied_changes_nothing(self):
        self.assertEqual(rh.gdm_restore(), rh.EXIT_NOTHING)
        self.assertEqual(self.fake.calls, [])
        self.assertStockLink()
        self.assertEqual(read(self.p["GRESOURCE"]), self.stock_bytes)

    def test_divert_without_rename_never_writes_through_symlink(self):
        # Divert registered while the package symlink is still at the path.
        self.fake.diverted = True
        os.symlink(self.LINK, self.p["KALI_LOGIN_BG_DIVERTED"])
        rh.gdm_apply(self.png)
        self.assertFalse(os.path.islink(self.p["KALI_LOGIN_BG"]))
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), read(self.png))
        self.assertWallpaperUntouched()

    def test_divert_add_failure_changes_nothing(self):
        def add_fails(*args):
            if "--add" in args:
                raise rh.HelperError("dpkg-divert failed: dpkg status database is locked")
            return self.fake(*args)
        with mock.patch.object(rh, "dpkg_divert", add_fails):
            with self.assertRaises(rh.HelperError):
                rh.gdm_apply(self.png)
        self.assertStockLink()
        self.assertFalse(os.path.lexists(self.p["KALI_LOGIN_BG_DIVERTED"]))

    def test_upgrade_from_gresource_patch_then_restore(self):
        with mock.patch.object(rh, "kali_override_active", lambda: False):
            rh.gdm_apply(self.png)
        self.assertTrue(rh.is_patched(rh.read_gresource(self.p["GRESOURCE"])))
        self.assertStockLink()  # old method never touched the Kali file
        rh.gdm_apply(self.jpg)
        self.assertEqual(read(self.p["GRESOURCE"]), self.stock_bytes)
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), read(self.jpg))
        self.assertEqual(rh.gdm_restore(), rh.EXIT_OK)
        self.assertStockLink()
        self.assertEqual(rh.gdm_restore(), rh.EXIT_NOTHING)

    def test_upgrade_with_unusable_old_backup_still_applies(self):
        with mock.patch.object(rh, "kali_override_active", lambda: False):
            rh.gdm_apply(self.png)
        write(self.p["GRESOURCE_ORIG"], b"corrupted backup")
        rh.gdm_apply(self.jpg)
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), read(self.jpg))

    def test_failed_restore_keeps_previous_working_state(self):
        rh.gdm_apply(self.png)

        def remove_fails(*args):
            if "--remove" in args:
                raise rh.HelperError("dpkg-divert failed: dpkg status database is locked")
            return self.fake(*args)
        with mock.patch.object(rh, "dpkg_divert", remove_fails):
            with self.assertRaises(rh.HelperError):
                rh.gdm_restore()
        # Either the custom image or the original must still be at the hardcoded path.
        self.assertTrue(os.path.exists(self.p["KALI_LOGIN_BG"]),
                        "login background path left empty after a failed restore")


class KaliDivertStateTest(KaliGdmTest):
    """Security round: diversion state from dpkg (not file existence), rollback, and no-file windows."""

    def marker(self):
        return os.path.join(self.p["STATE_DIR"], "kali-login.divert")

    def test_restore_after_package_purge_removes_diversion(self):
        rh.gdm_apply(self.png)
        os.unlink(self.p["KALI_LOGIN_BG_DIVERTED"])  # dpkg deletes the diverted file on purge
        self.assertEqual(rh.gdm_restore(), rh.EXIT_OK)
        self.assertFalse(self.fake.diverted)
        self.assertFalse(os.path.lexists(self.p["KALI_LOGIN_BG"]))
        self.assertFalse(os.path.exists(self.marker()))

    def test_rollback_keeps_preexisting_diversion(self):
        self.fake.diverted = True  # registered earlier; renamed file missing and no marker
        os.unlink(self.p["KALI_LOGIN_BG"])
        with mock.patch.object(rh.os, "replace", side_effect=OSError(28, "full")):
            with self.assertRaises(OSError):
                rh.gdm_apply(self.png)
        self.assertTrue(self.fake.diverted)
        self.assertEqual([c for c in self.fake.calls if "--remove" in c], [])

    def test_failed_rollback_reports_both_errors(self):
        def remove_fails(*args):
            if "--remove" in args:
                raise rh.HelperError("dpkg-divert failed: database locked")
            return self.fake(*args)
        real_replace = os.replace

        def replace(src, dst):
            if dst == self.p["KALI_LOGIN_BG"]:
                raise OSError(28, "No space left on device")
            return real_replace(src, dst)
        with mock.patch.object(rh, "dpkg_divert", remove_fails), mock.patch.object(rh.os, "replace", replace):
            with self.assertRaises(rh.HelperError) as cm:
                rh.gdm_apply(self.png)
        self.assertIn("No space left", str(cm.exception))
        self.assertIn("database locked", str(cm.exception))

    def test_divert_happens_after_new_image_is_staged(self):
        seen = []

        def spy(*args):
            if "--add" in args:
                d = os.path.dirname(self.p["KALI_LOGIN_BG"])
                seen.append([n for n in os.listdir(d) if n.endswith(".tmp")])
            return self.fake(*args)
        with mock.patch.object(rh, "dpkg_divert", spy):
            rh.gdm_apply(self.png)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), 1, "staged image must exist before the path is renamed away")
        self.assertTrue(os.path.exists(self.marker()))

    def test_add_failure_leaves_no_marker(self):
        def add_fails(*args):
            if "--add" in args:
                raise rh.HelperError("dpkg-divert failed")
            return self.fake(*args)
        with mock.patch.object(rh, "dpkg_divert", add_fails):
            with self.assertRaises(rh.HelperError):
                rh.gdm_apply(self.png)
        self.assertFalse(os.path.exists(self.marker()))
        self.assertOriginalBack()

    def test_failed_restore_puts_image_back(self):
        rh.gdm_apply(self.png)

        def remove_fails(*args):
            if "--remove" in args:
                raise rh.HelperError("dpkg-divert failed")
            return self.fake(*args)
        with mock.patch.object(rh, "dpkg_divert", remove_fails):
            with self.assertRaises(rh.HelperError):
                rh.gdm_restore()
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), read(self.png))
        self.assertTrue(self.fake.diverted)
        self.assertEqual(rh.gdm_restore(), rh.EXIT_OK)  # a later restore still works
        self.assertOriginalBack()

    def test_apply_succeeds_even_if_old_theme_revert_errors(self):
        with mock.patch.object(rh, "gdm_gresource_restore", side_effect=OSError(5, "I/O error")):
            rh.gdm_apply(self.png)
        self.assertEqual(read(self.p["KALI_LOGIN_BG"]), read(self.png))


if __name__ == "__main__":
    unittest.main()
