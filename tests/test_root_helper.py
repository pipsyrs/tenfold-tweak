"""Root helper run as a normal user against scratch copies; every system path is overridden."""

import json
import os
import shutil
import sys
import unittest
from unittest import mock

from tests.helpers import jpeg_header_only, make_image, png_header_only, scratch_dir, write

from tenfold_tweak import root_helper as rh

SYSTEM_THEME = "/usr/share/gnome-shell/gnome-shell-theme.gresource"


def _stock_fixture() -> str:
    # The installed theme may already be patched by a real run; then use the tool's stock backup.
    for path in (SYSTEM_THEME, rh.GRESOURCE_ORIG):
        try:
            if not rh.is_patched(rh.read_gresource(path)):
                return path
        except rh.HelperError:
            continue
    return SYSTEM_THEME


STOCK = _stock_fixture()
BGDIR = os.path.dirname(os.path.abspath(rh.__file__))

# Child for load_entries(): same module, with the sandbox paths applied.
DUMP_WRAPPER = """
import json, sys
sys.path.insert(0, sys.argv[1])
import root_helper as rh
for k, v in json.loads(sys.argv[2]).items():
    setattr(rh, k, v)
try:
    sys.exit(rh.dump_main(sys.argv[3]))
except rh.HelperError as e:
    print(e, file=sys.stderr)
    sys.exit(1)
"""


def read(path):
    with open(path, "rb") as f:
        return f.read()


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("helper-")
        self.state = os.path.join(self.d, "var-lib")
        share = os.path.join(self.d, "usr-share")
        os.makedirs(share)
        paths = {
            "STATE_DIR": self.state,
            "GRESOURCE": os.path.join(share, "gnome-shell-theme.gresource"),
            "GRESOURCE_NEW": os.path.join(share, ".tenfold-tweaks-gnome-shell-theme.gresource.new"),
            "GRESOURCE_ORIG": os.path.join(self.state, "gnome-shell-theme.gresource.orig"),
            "GRESOURCE_META": os.path.join(self.state, "gnome-shell-theme.meta"),
            "LIGHTDM_CONF": os.path.join(self.d, "etc", "lightdm-gtk-greeter.conf"),
            "LIGHTDM_ORIG": os.path.join(self.state, "lightdm-gtk-greeter.conf.orig"),
            "LIGHTDM_IMAGE_DIR": os.path.join(self.state, "lightdm"),
            # No Kali shell override by default: these tests cover the gresource method.
            "SHELL_LIBS": os.path.join(self.d, "usr-lib", "libshell-*.so"),
            "KALI_LOGIN_BG": os.path.join(self.d, "kali-login", "background-blurred"),
            "KALI_LOGIN_BG_DIVERTED": os.path.join(self.d, "kali-login", "background-blurred.tenfold-tweaks"),
            "RESCUE_DIR": os.path.join(self.d, "var-backups"),
            "DPKG_DIVERT": os.path.join(self.d, "no-such-dpkg-divert"),
        }
        self.p = paths
        wrapper = write(os.path.join(self.d, "dump_wrapper.py"), DUMP_WRAPPER.encode())
        cfg = json.dumps({k: paths[k] for k in ("STATE_DIR", "GRESOURCE", "GRESOURCE_ORIG")})
        self.version = "50.4-1+kali1"
        patches = [
            mock.patch.multiple(rh, **paths),
            mock.patch.object(rh, "_dump_command", lambda path: [sys.executable, "-I", wrapper, BGDIR, cfg, path]),
            mock.patch.object(rh, "ensure_state_dir", lambda: os.makedirs(self.state, exist_ok=True)),
            mock.patch.object(rh, "ensure_image_dir", lambda: os.makedirs(paths["LIGHTDM_IMAGE_DIR"], exist_ok=True)),
            mock.patch.object(rh, "shell_version", lambda: self.version),
            mock.patch.dict(os.environ, {"PKEXEC_UID": str(os.getuid())}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.png = make_image(os.path.join(self.d, "login.png"), 64, 48)
        self.jpg = make_image(os.path.join(self.d, "login.jpg"), 64, 48, fmt="jpeg")


@unittest.skipUnless(os.path.isfile(STOCK), "stock gnome-shell theme not installed")
class GResourceWriterTest(Sandbox):
    def test_rebuild_of_stock_round_trips(self):
        stock = rh.read_gresource(STOCK)
        out = write(os.path.join(self.d, "rebuilt.gresource"), rh.build_gresource(stock))
        self.assertEqual(rh.read_gresource(out), stock)

    def test_patched_rebuild_round_trips_with_new_entries(self):
        entries = rh.read_gresource(STOCK)
        entries[rh.THEME_PREFIX + "gnome-shell-dark.css"] += b"/* x */"
        entries[rh.LOGIN_ENTRY + "png"] = read(self.png)
        entries["/a/b/c/deep/empty"] = b""
        entries["/a/b/été.txt"] = b"utf8"
        out = write(os.path.join(self.d, "patched.gresource"), rh.build_gresource(entries))
        self.assertEqual(rh.read_gresource(out), entries)

    def test_invalid_paths_rejected(self):
        for bad in ("relative", "/dir/", "/a//b"):
            with self.assertRaises(rh.HelperError):
                rh.build_gresource({bad: b"x"})


@unittest.skipUnless(os.path.isfile(STOCK), "stock gnome-shell theme not installed")
class GdmTest(Sandbox):
    def setUp(self):
        super().setUp()
        shutil.copyfile(STOCK, self.p["GRESOURCE"])
        self.stock_bytes = read(STOCK)
        self.assertFalse(rh.is_patched(rh.read_gresource(STOCK)), "system theme is not stock")

    def installed(self):
        return rh.read_gresource(self.p["GRESOURCE"])

    def assertStock(self):
        self.assertEqual(read(self.p["GRESOURCE"]), self.stock_bytes)

    def assertNoBuildLeftovers(self):
        left = [n for n in os.listdir(self.state) if n.startswith("build-")] if os.path.isdir(self.state) else []
        self.assertEqual(left, [])
        self.assertFalse(os.path.lexists(self.p["GRESOURCE_NEW"]))

    def test_apply_embeds_image_and_patches_css(self):
        rh.gdm_apply(self.png)
        entries = self.installed()
        self.assertEqual(entries[rh.LOGIN_ENTRY + "png"], read(self.png))
        for css in rh.PATCHED_CSS:
            self.assertIn(b"#lockDialogGroup", entries[rh.THEME_PREFIX + css])
            self.assertIn(b"resource://" + (rh.LOGIN_ENTRY + "png").encode(), entries[rh.THEME_PREFIX + css])
        self.assertEqual(read(self.p["GRESOURCE_ORIG"]), self.stock_bytes)
        meta = json.loads(read(self.p["GRESOURCE_META"]))
        self.assertEqual(meta["version"], self.version)
        self.assertNoBuildLeftovers()

    def test_image_survives_source_deletion(self):
        data = read(self.png)
        rh.gdm_apply(self.png)
        os.unlink(self.png)
        self.assertEqual(self.installed()[rh.LOGIN_ENTRY + "png"], data)

    def test_reapply_keeps_original_backup_and_replaces_image(self):
        rh.gdm_apply(self.png)
        rh.gdm_apply(self.jpg)
        entries = self.installed()
        self.assertNotIn(rh.LOGIN_ENTRY + "png", entries)
        self.assertEqual(entries[rh.LOGIN_ENTRY + "jpg"], read(self.jpg))
        self.assertEqual(entries[rh.THEME_PREFIX + "gnome-shell-dark.css"].count(b"tenfold-tweaks */"), 1)
        self.assertEqual(read(self.p["GRESOURCE_ORIG"]), self.stock_bytes)

    def test_restore_is_byte_identical_then_nothing_to_restore(self):
        rh.gdm_apply(self.png)
        self.assertEqual(rh.gdm_restore(), rh.EXIT_OK)
        self.assertStock()
        self.assertFalse(os.path.exists(self.p["GRESOURCE_ORIG"]))
        self.assertEqual(rh.gdm_restore(), rh.EXIT_NOTHING)
        self.assertStock()

    def test_restore_without_any_backup_changes_nothing(self):
        self.assertEqual(rh.gdm_restore(), rh.EXIT_NOTHING)
        self.assertStock()

    def test_garbage_build_rolls_back(self):
        with mock.patch.object(rh, "build_gresource", lambda e: b"GVariant" + b"\xff" * 4096):
            with self.assertRaises(rh.HelperError) as cm:
                rh.gdm_apply(self.png)
        self.assertIn("stock file left untouched", str(cm.exception))
        self.assertStock()
        self.assertNoBuildLeftovers()

    def test_build_missing_entry_rolls_back(self):
        real = rh.build_gresource

        def drop_image(entries):
            return real({k: v for k, v in entries.items() if not k.startswith(rh.LOGIN_ENTRY)})
        with mock.patch.object(rh, "build_gresource", drop_image):
            with self.assertRaises(rh.HelperError) as cm:
                rh.gdm_apply(self.png)
        self.assertIn("mismatch", str(cm.exception))
        self.assertStock()
        self.assertNoBuildLeftovers()

    def test_post_install_check_failure_restores_stock(self):
        with mock.patch.object(rh, "verify_entries", side_effect=[None, rh.HelperError("boom")]):
            with self.assertRaises(rh.HelperError) as cm:
                rh.gdm_apply(self.png)
        self.assertIn("post-install check failed", str(cm.exception))
        self.assertStock()

    def test_failed_reapply_keeps_previous_working_state(self):
        # AC: "If applying any target fails midway, that target is left in its previous working state".
        rh.gdm_apply(self.png)
        before = read(self.p["GRESOURCE"])
        with mock.patch.object(rh, "verify_entries", side_effect=[None, rh.HelperError("boom")]):
            with self.assertRaises(rh.HelperError):
                rh.gdm_apply(self.jpg)
        self.assertEqual(read(self.p["GRESOURCE"]), before)

    def test_gnome_shell_update_refreshes_backup(self):
        rh.gdm_apply(self.png)
        shutil.copyfile(STOCK, self.p["GRESOURCE"])  # package update reinstalled stock
        self.version = "50.5-1+kali1"
        rh.gdm_apply(self.jpg)
        self.assertEqual(json.loads(read(self.p["GRESOURCE_META"]))["version"], "50.5-1+kali1")
        self.assertEqual(rh.gdm_restore(), rh.EXIT_OK)
        self.assertStock()

    def test_restore_with_version_mismatch_gives_reinstall_hint(self):
        rh.gdm_apply(self.png)
        patched = read(self.p["GRESOURCE"])
        self.version = "99"
        with self.assertRaises(rh.HelperError) as cm:
            rh.gdm_restore()
        self.assertIn("reinstall", str(cm.exception))
        self.assertEqual(read(self.p["GRESOURCE"]), patched)

    def test_tampered_backup_rejected(self):
        rh.gdm_apply(self.png)
        with open(self.p["GRESOURCE_ORIG"], "ab") as f:
            f.write(b"x")
        with self.assertRaises(rh.HelperError) as cm:
            rh.gdm_restore()
        self.assertIn("checksum", str(cm.exception))

    def test_dump_child_refuses_other_paths(self):
        with self.assertRaises(rh.HelperError):
            rh.load_entries(STOCK)


class LoadImageTest(Sandbox):
    def assertRejected(self, path, fragment):
        with self.assertRaises(rh.HelperError) as cm:
            rh.load_image(path)
        self.assertIn(fragment, str(cm.exception))

    def test_png_and_jpeg_accepted(self):
        self.assertEqual(rh.load_image(self.png)[1], "png")
        self.assertEqual(rh.load_image(self.jpg)[1], "jpg")

    def test_symlink_rejected(self):
        link = os.path.join(self.d, "link.png")
        os.symlink(self.png, link)
        self.assertRejected(link, "cannot open image")

    def test_relative_path_rejected(self):
        self.assertRejected("login.png", "absolute")

    def test_directory_rejected(self):
        self.assertRejected(self.d, "not a regular file")

    def test_other_owner_rejected(self):
        with mock.patch.dict(os.environ, {"PKEXEC_UID": str(os.getuid() + 1)}):
            self.assertRejected(self.png, "not owned by the calling user")

    def test_non_image_rejected(self):
        self.assertRejected(write(os.path.join(self.d, "x.png"), b"GIF89a....."), "supported: PNG, JPEG")

    def test_too_big_rejected(self):
        p = os.path.join(self.d, "huge.png")
        with open(p, "wb") as f:
            f.write(png_header_only(10, 10))
            f.truncate(rh.MAX_IMAGE_SIZE + 1)
        self.assertRejected(p, "64 MiB")

    def test_dimension_limits(self):
        self.assertRejected(write(os.path.join(self.d, "w.png"), png_header_only(5000, 10)), "at most 4096x4096")
        self.assertRejected(write(os.path.join(self.d, "h.jpg"), jpeg_header_only(10, 4097)), "at most 4096x4096")
        ok = write(os.path.join(self.d, "ok.jpg"), jpeg_header_only(4096, 4096))
        self.assertEqual(rh.load_image(ok)[1], "jpg")

    def test_main_requires_root_and_exact_args(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        self.assertEqual(rh.main(["gdm-apply", self.png]), rh.EXIT_FAIL)
        self.assertEqual(rh.main(["gdm-apply"]), rh.EXIT_FAIL)
        self.assertEqual(rh.main(["gdm-restore", "extra"]), rh.EXIT_FAIL)
        self.assertEqual(rh.main(["rm-rf"]), rh.EXIT_FAIL)


CONF = """# Kali greeter
[greeter]
# background=/old/commented.png
background = /usr/share/desktop-base/kali-theme/login/background
user-background = true
theme-name = Kali-Dark
"""


class LightdmTest(Sandbox):
    def setUp(self):
        super().setUp()
        write(self.p["LIGHTDM_CONF"], CONF.encode())
        os.chmod(self.p["LIGHTDM_CONF"], 0o644)

    def conf(self):
        return read(self.p["LIGHTDM_CONF"]).decode()

    def test_apply_sets_background_keeps_rest(self):
        rh.lightdm_apply(self.png)
        target = os.path.join(self.p["LIGHTDM_IMAGE_DIR"], "login.png")
        self.assertEqual(read(target), read(self.png))
        text = self.conf()
        self.assertIn(f"background = {target}\n", text)
        self.assertIn("user-background = false\n", text)
        self.assertIn("theme-name = Kali-Dark", text)
        self.assertIn("# background=/old/commented.png", text)
        self.assertNotIn("kali-theme/login/background", text)
        self.assertEqual(os.stat(self.p["LIGHTDM_CONF"]).st_mode & 0o777, 0o644)
        self.assertEqual(read(self.p["LIGHTDM_ORIG"]).decode(), CONF)

    def test_reapply_keeps_original_backup_and_old_image_removed(self):
        rh.lightdm_apply(self.png)
        rh.lightdm_apply(self.jpg)
        self.assertEqual(read(self.p["LIGHTDM_ORIG"]).decode(), CONF)
        self.assertEqual(sorted(os.listdir(self.p["LIGHTDM_IMAGE_DIR"])), ["login.jpg"])
        self.assertIn("login.jpg", self.conf())

    def test_restore_then_nothing_to_restore(self):
        rh.lightdm_apply(self.png)
        self.assertEqual(rh.lightdm_restore(), rh.EXIT_OK)
        self.assertEqual(self.conf(), CONF)
        self.assertFalse(os.path.exists(self.p["LIGHTDM_IMAGE_DIR"]))
        self.assertFalse(os.path.exists(self.p["LIGHTDM_ORIG"]))
        self.assertEqual(rh.lightdm_restore(), rh.EXIT_NOTHING)
        self.assertEqual(self.conf(), CONF)

    def test_restore_without_backup_changes_nothing(self):
        sentinel = write(os.path.join(self.p["LIGHTDM_IMAGE_DIR"], "login.png"), b"keep")
        self.assertEqual(rh.lightdm_restore(), rh.EXIT_NOTHING)
        self.assertTrue(os.path.exists(sentinel))
        self.assertEqual(self.conf(), CONF)

    def test_conf_without_greeter_section(self):
        out = rh.patch_greeter_conf("[other]\nx = 1", "/img.png")
        self.assertEqual(out, "[other]\nx = 1\n[greeter]\nbackground = /img.png\nuser-background = false\n")

    def test_failed_conf_write_keeps_previous_state(self):
        # AC: failure midway leaves the target in its previous working state.
        rh.lightdm_apply(self.png)
        conf_before = self.conf()
        target = os.path.join(self.p["LIGHTDM_IMAGE_DIR"], "login.png")
        image_before = read(target)
        other = make_image(os.path.join(self.d, "other.png"), 16, 16, rgba=0xFF0000FF)
        real = rh.atomic_write

        def failing(dest, *a, **k):
            if dest == self.p["LIGHTDM_CONF"]:
                raise OSError(28, "No space left on device")
            return real(dest, *a, **k)
        with mock.patch.object(rh, "atomic_write", failing):
            with self.assertRaises(OSError):
                rh.lightdm_apply(other)
        self.assertEqual(self.conf(), conf_before)
        self.assertEqual(read(target), image_before)


if __name__ == "__main__":
    unittest.main()
