"""install.sh migration/uninstall branches, run on a rewritten copy against scratch paths.

Every absolute system path in the copy is redirected into a scratch root, the root check is
replaced, and run_helper/dpkg-divert/gresource/install/chown/stat are stubs that only log.
Nothing here runs sudo, pkexec or a real root helper.
"""

import os
import re
import shutil
import stat
import subprocess
import unittest

from tests.helpers import scratch_dir

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_INSTALL = os.path.join(ROOT, "install.sh")
KALI_BG = "/usr/share/desktop-base/kali-theme/login/background-blurred"
GRESOURCE = "/usr/share/gnome-shell/gnome-shell-theme.gresource"
IMG = b"\x89PNG\r\n\x1a\nmigration-image"

TOOLS = ("dirname", "id", "mktemp", "rm", "cp", "grep", "head", "mv", "getent", "cut", "chmod", "ln",
         "cat", "mkdir")
STUBS = {
    # dpkg-divert --truename PATH: diverted to the old name when STUB_DIVERT=1
    "dpkg-divert": 'echo "dpkg-divert $*" >> "$STUB_LOG"\n'
                   '[ "$STUB_DIVERT" = 1 ] && echo "$2.kali-bg-changer" || echo "$2"',
    "gresource": 'echo "gresource $*" >> "$STUB_LOG"\n'
                 'case "$1" in\n'
                 '  list) [ -n "$STUB_ENTRY" ] && echo "$STUB_ENTRY"; echo /org/gnome/shell/theme/gnome-shell.css ;;\n'
                 '  extract) printf "%s" "$STUB_EXTRACT" ;;\n'
                 'esac',
    # install -d [-o -g -m] DIRS...: create dirs, ignore ownership
    "install": 'for a; do case "$a" in -*|root|0755) ;; *) mkdir -p "$a" ;; esac; done',
    "chown": ":",
    "stat": 'echo 0',  # stat -c %u OLD_HELPER: pretend root-owned
    "sudo": "echo 'sudo must not run' >&2; exit 99",
    "pkexec": "echo 'pkexec must not run' >&2; exit 99",
}


@unittest.skipIf(os.geteuid() == 0, "never run install.sh harness as root")
class InstallMigrationTest(unittest.TestCase):
    def setUp(self):
        self.base = scratch_dir("install-")
        self.root = os.path.join(self.base, "root")
        self.src = os.path.join(self.base, "src")
        self.log = os.path.join(self.base, "log")
        self.bin = os.path.join(self.base, "bin")
        os.makedirs(os.path.join(self.src, "tenfold_tweak"))
        for d in ("var/lib", "usr/local/lib", "usr/local/bin", "usr/local/share/applications"):
            os.makedirs(self.p("/" + d))
        os.makedirs(self.bin)
        # Only the plain tools the script needs; dpkg-divert/gresource/install... are stubs.
        for tool in TOOLS:
            real = shutil.which(tool, path="/usr/bin:/bin")
            if real:
                os.symlink(real, os.path.join(self.bin, tool))
        open(self.log, "w").close()
        for name, body in STUBS.items():
            self._exe(os.path.join(self.bin, name), "#!/bin/sh\n" + body + "\n")
        self._write_script()
        for f in ("tenfold-tweak",) + tuple(f"tenfold_tweak/{n}.py" for n in (
                "__init__", "cli", "core", "desktop", "detect", "gui", "image", "root_helper", "system")):
            with open(os.path.join(self.src, f), "w") as fh:
                fh.write("# stub\n")

    # ---------- harness ----------

    def p(self, path):
        return self.root + path

    def _exe(self, path, text):
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)

    def _write_script(self):
        with open(REAL_INSTALL) as f:
            s = f.read()
        root_check = '[ "$(id -u)" -eq 0 ] ||'
        helper_fn = 'run_helper() {\n    env -u PKEXEC_UID -u SUDO_UID /usr/bin/python3 -I "$@"\n}'
        self.assertIn(root_check, s)
        self.assertIn(helper_fn, s)
        s = s.replace(root_check, "true ||")
        s = s.replace(helper_fn, 'run_helper() {\n    echo "helper $*" >> "$STUB_LOG"\n'
                                 '    case "$1" in *bgchanger*) return "${STUB_OLD_RC:-0}";; esac\n'
                                 '    [ -f "$3" ] && cp "$3" "$STUB_APPLIED"\n    return "${STUB_NEW_RC:-0}"\n}')
        s = re.sub(r"(?<![\w.$/-])/(usr|var)/", lambda m: self.root + m.group(0), s)
        # Safety: no system path left outside the scratch root.
        marked = s.replace(self.root, "@ROOT@")
        for path in re.findall(r"(?<![\w.$/@-])/(?:usr|var|etc)/[\w./-]*", marked):
            self.fail(f"unredirected path in harness copy: {path}")
        self.script = os.path.join(self.src, "install.sh")
        self._exe(self.script, s)

    def run_install(self, *args, **stub):
        env = {"PATH": self.bin, "STUB_LOG": self.log,
               "STUB_APPLIED": os.path.join(self.base, "applied"), "SUDO_USER": os.environ.get("USER", "nobody")}
        env.update({k: str(v) for k, v in stub.items()})
        r = subprocess.run(["/bin/sh", self.script, *args], env=env, capture_output=True, text=True, timeout=30)
        with open(self.log) as f:
            r.calls = f.read().splitlines()
        return r

    def helper_calls(self, r):
        return [c.split()[1:3] for c in r.calls if c.startswith("helper ")]

    def touch(self, path, data=b"x"):
        full = self.p(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        return full

    def old_install(self):
        self.touch("/usr/local/lib/kali-bg-changer/bgchanger/root_helper.py")
        self.touch("/usr/local/lib/kali-bg-changer/kali-bg-changer")
        os.symlink(self.p("/usr/local/lib/kali-bg-changer/kali-bg-changer"), self.p("/usr/local/bin/kali-bg-changer"))
        os.makedirs(self.p("/var/lib/kali-bg-changer"), exist_ok=True)

    def assert_old_kept(self):
        self.assertTrue(os.path.isdir(self.p("/usr/local/lib/kali-bg-changer")))
        self.assertTrue(os.path.islink(self.p("/usr/local/bin/kali-bg-changer")))

    def assert_old_removed(self):
        for path in ("/usr/local/lib/kali-bg-changer", "/var/lib/kali-bg-changer"):
            self.assertFalse(os.path.exists(self.p(path)), path)
        self.assertFalse(os.path.lexists(self.p("/usr/local/bin/kali-bg-changer")))

    def assert_no_temp_left(self):
        self.assertEqual([n for n in os.listdir(self.p("/var/lib")) if n.startswith(".tenfold")], [])

    def assert_nothing_installed(self):
        self.assertFalse(os.path.exists(self.p("/usr/local/lib/tenfold-tweak")))
        self.assertFalse(os.path.exists(self.p("/usr/local/lib/tenfold-tweak.new")))

    # ---------- fresh install ----------

    def test_fresh_install_desktop_file_and_no_migration(self):
        old_desktop = self.touch("/usr/local/share/applications/tenfold-tweak.desktop")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        desk = self.p("/usr/local/share/applications/io.github.tenfold.TenfoldTweak.desktop")
        with open(desk) as f:
            text = f.read()
        self.assertIn(f"Exec={self.p('/usr/local/bin/tenfold-tweak')} gui", text)
        self.assertEqual(stat.S_IMODE(os.stat(desk).st_mode), 0o644)
        self.assertFalse(os.path.exists(old_desktop))
        self.assertTrue(os.path.isfile(self.p("/usr/local/lib/tenfold-tweak/tenfold_tweak/root_helper.py")))
        self.assertEqual(os.readlink(self.p("/usr/local/bin/tenfold-tweak")),
                         self.p("/usr/local/lib/tenfold-tweak/tenfold-tweak"))
        self.assertNotIn("migrating", r.stdout)
        self.assertEqual(self.helper_calls(r), [])

    # ---------- migrate_prepare / migrate_finish ----------

    def test_old_install_without_login_change_is_removed(self):
        self.old_install()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_old_removed()
        self.assertEqual(self.helper_calls(r), [])
        self.assert_no_temp_left()

    def test_kali_divert_image_saved_restored_and_reapplied(self):
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/kali-login.divert")
        self.touch(KALI_BG, IMG)
        r = self.run_install(STUB_DIVERT=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.helper_calls(r)
        self.assertEqual(calls[0], [self.p("/usr/local/lib/kali-bg-changer/bgchanger/root_helper.py"), "gdm-restore"])
        self.assertEqual(calls[1], [self.p("/usr/local/lib/tenfold-tweak/tenfold_tweak/root_helper.py"), "gdm-apply"])
        with open(os.path.join(self.base, "applied"), "rb") as f:
            self.assertEqual(f.read(), IMG)
        self.assertIn("re-applied", r.stdout)
        self.assert_old_removed()
        self.assert_no_temp_left()

    def test_unsaveable_divert_image_aborts_before_any_change(self):
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/kali-login.divert")
        os.makedirs(os.path.dirname(self.p(KALI_BG)), exist_ok=True)
        os.symlink("/nonexistent", self.p(KALI_BG))  # cannot be copied safely
        r = self.run_install(STUB_DIVERT=1)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing was changed", r.stderr)
        self.assertIn("--discard-login", r.stderr)
        self.assertEqual(self.helper_calls(r), [])
        self.assert_old_kept()
        self.assert_nothing_installed()
        self.assert_no_temp_left()

    def test_discard_login_resets_and_continues(self):
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/kali-login.divert")
        r = self.run_install("--discard-login", STUB_DIVERT=1)  # image file missing
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c[1] for c in self.helper_calls(r)], ["gdm-restore"])
        self.assertIn("login background was reset", r.stderr)
        self.assert_old_removed()

    def test_reapply_failure_keeps_image(self):
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/kali-login.divert")
        self.touch(KALI_BG, IMG)
        r = self.run_install(STUB_DIVERT=1, STUB_NEW_RC=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        kept = self.p("/var/lib/tenfold-tweak/migrated-login")
        with open(kept, "rb") as f:
            self.assertEqual(f.read(), IMG)
        self.assertIn("kept at", r.stderr)
        self.assertIn("sudo cp", r.stderr)
        self.assert_no_temp_left()

    def test_old_restore_failure_leaves_old_install(self):
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/kali-login.divert")
        self.touch(KALI_BG, IMG)
        r = self.run_install(STUB_DIVERT=1, STUB_OLD_RC=1)
        self.assertEqual(r.returncode, 1)
        self.assertIn("old install left in place", r.stderr)
        self.assert_old_kept()
        self.assertEqual([c[1] for c in self.helper_calls(r)], ["gdm-restore"])
        self.assert_no_temp_left()

    def test_old_restore_nothing_to_restore_rc3_continues(self):
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/kali-login.divert")
        self.touch(KALI_BG, IMG)
        r = self.run_install(STUB_DIVERT=1, STUB_OLD_RC=3)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_old_removed()

    def test_active_change_with_missing_old_helper_aborts(self):
        self.old_install()
        os.unlink(self.p("/usr/local/lib/kali-bg-changer/bgchanger/root_helper.py"))
        self.touch("/var/lib/kali-bg-changer/kali-login.divert")
        r = self.run_install(STUB_DIVERT=1)
        self.assertEqual(r.returncode, 1)
        self.assertIn("helper is missing", r.stderr)
        self.assert_old_kept()
        self.assert_nothing_installed()

    def test_lightdm_image_saved_and_reapplied(self):
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/lightdm-gtk-greeter.conf.orig")
        self.touch("/usr/local/share/backgrounds/kali-bg-changer/login.png", IMG)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c[1] for c in self.helper_calls(r)], ["lightdm-restore", "lightdm-apply"])
        with open(os.path.join(self.base, "applied"), "rb") as f:
            self.assertEqual(f.read(), IMG)

    def test_gdm_and_lightdm_both_active_aborts(self):
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/kali-login.divert")
        self.touch("/var/lib/kali-bg-changer/lightdm-gtk-greeter.conf.orig")
        self.touch(KALI_BG, IMG)
        self.touch("/usr/local/share/backgrounds/kali-bg-changer/login.png", IMG)
        r = self.run_install(STUB_DIVERT=1)
        self.assertEqual(r.returncode, 1)
        self.assert_old_kept()
        self.assert_nothing_installed()

    def test_gresource_entry_extracted_and_reapplied(self):
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/gnome-shell-theme.gresource.orig")
        entry = "/org/gnome/shell/theme/kali-bg-changer-login.png"
        r = self.run_install(STUB_ENTRY=entry, STUB_EXTRACT="imagebytes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"gresource extract {self.p(GRESOURCE)} {entry}", r.calls)
        self.assertEqual([c[1] for c in self.helper_calls(r)], ["gdm-restore", "gdm-apply"])

    def test_gresource_already_stock_needs_no_image(self):
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/gnome-shell-theme.gresource.orig")
        r = self.run_install(STUB_ENTRY="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c[1] for c in self.helper_calls(r)], ["gdm-restore"])

    def test_gresource_empty_extract_aborts(self):
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/gnome-shell-theme.gresource.orig")
        r = self.run_install(STUB_ENTRY="/org/gnome/shell/theme/kali-bg-changer-login.png", STUB_EXTRACT="")
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing was changed", r.stderr)
        self.assert_nothing_installed()

    def test_gresource_tool_missing_aborts(self):
        os.unlink(os.path.join(self.bin, "gresource"))
        self.old_install()
        self.touch("/var/lib/kali-bg-changer/gnome-shell-theme.gresource.orig")
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assert_nothing_installed()

    # ---------- uninstall ----------

    def test_uninstall_refuses_while_migrated_login_exists(self):
        self.assertEqual(self.run_install().returncode, 0)
        kept = self.touch("/var/lib/tenfold-tweak/migrated-login", IMG)
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 1)
        self.assertIn("only copy", r.stderr)
        self.assertTrue(os.path.isfile(kept))
        self.assertTrue(os.path.isdir(self.p("/usr/local/lib/tenfold-tweak")))

    def test_uninstall_refuses_while_divert_marker_exists(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.touch("/var/lib/tenfold-tweak/kali-login.divert")
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 1)
        self.assertIn("restore --login", r.stderr)

    def test_uninstall_removes_everything(self):
        self.assertEqual(self.run_install().returncode, 0)
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        for path in ("/usr/local/lib/tenfold-tweak", "/var/lib/tenfold-tweak",
                     "/usr/local/share/applications/io.github.tenfold.TenfoldTweak.desktop"):
            self.assertFalse(os.path.exists(self.p(path)), path)
        self.assertFalse(os.path.lexists(self.p("/usr/local/bin/tenfold-tweak")))


if __name__ == "__main__":
    unittest.main()
