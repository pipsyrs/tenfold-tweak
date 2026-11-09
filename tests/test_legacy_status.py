"""Legacy (kali-bg-changer) backup/copy migration and the read-only core.status()."""

import json
import os
import shutil
import unittest
from unittest import mock

from tests import SANDBOX
from tests.helpers import make_image, scratch_dir, write
from tests.test_desktop import gnome, reset_state

from tenfold_tweak import core, desktop, detect, image

UUID = next(iter(desktop.LOCK_EXTENSIONS))
LOGIN = {"method": "Kali login image (dpkg-divert)", "current": "/x/login", "restorable": False}


def env(de="gnome", dm="gdm3", provider=None, unavailable=None):
    u = {"desktop": None, "lock": None, "login": None}
    u.update(unavailable or {})
    return detect.Environment(de, dm, u, lock_provider=provider)


class LegacyBackupTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(desktop.LEGACY_STATE_DIR.startswith(SANDBOX))
        self.assertTrue(desktop.LEGACY_STATE_DIR.endswith("/kali-bg-changer"))
        reset_state()
        self.addCleanup(reset_state)
        self.addCleanup(shutil.rmtree, desktop.LEGACY_STATE_DIR, True)
        self.legacy = os.path.join(desktop.LEGACY_STATE_DIR, "backup.json")

    def test_legacy_backup_moved_once_and_old_dir_removed(self):
        data = {"gnome": {"picture-uri": "file:///old.png", "picture-uri-dark": None, "picture-options": None}}
        write(self.legacy, json.dumps(data).encode())
        self.assertEqual(desktop.load_backup(), data)
        self.assertFalse(os.path.exists(desktop.LEGACY_STATE_DIR))
        self.assertTrue(desktop.has_backup("gnome"))
        self.assertEqual(os.stat(desktop.STATE_DIR).st_mode & 0o777, 0o700)

    def test_existing_new_backup_wins_and_legacy_untouched(self):
        desktop.save_backup({"gnome": {"picture-uri": "file:///new.png"}})
        write(self.legacy, b'{"gnome": {"picture-uri": "file:///old.png"}}')
        self.assertEqual(desktop.load_backup()["gnome"]["picture-uri"], "file:///new.png")
        self.assertTrue(os.path.isfile(self.legacy))

    def test_symlinked_legacy_backup_ignored(self):
        target = write(os.path.join(scratch_dir(), "elsewhere.json"), b'{"gnome": {"x": 1}}')
        os.makedirs(desktop.LEGACY_STATE_DIR, exist_ok=True)
        os.symlink(target, self.legacy)
        self.assertEqual(desktop.load_backup(), {})
        self.assertFalse(os.path.exists(desktop.BACKUP_FILE))
        self.assertTrue(os.path.islink(self.legacy))

    def test_migrated_legacy_backup_restores_desktop(self):
        write(self.legacy, json.dumps({"gnome": {k: None for k in desktop.GNOME_KEYS}}).encode())
        gnome().set_string("picture-uri", "file:///changed.png")
        self.assertTrue(desktop.restore("gnome"))
        self.assertFalse(gnome().get_user_value("picture-uri"))


class LegacyCopiesTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(image.LEGACY_COPY_DIR.startswith(SANDBOX))
        self.addCleanup(shutil.rmtree, image.LEGACY_COPY_DIR, True)

    def legacy_file(self, name):
        return write(os.path.join(image.LEGACY_COPY_DIR, name), b"x")

    def test_unreferenced_legacy_copies_removed_and_dir_dropped(self):
        self.legacy_file("wallpaper-aaa.png")
        self.legacy_file("login-bbb.png")
        image.cleanup_copies(set())
        self.assertFalse(os.path.exists(image.LEGACY_COPY_DIR))

    def test_referenced_legacy_copy_kept(self):
        kept = self.legacy_file("wallpaper-aaa.png")
        gone = self.legacy_file("wallpaper-bbb.png")
        image.cleanup_copies({kept})
        self.assertTrue(os.path.exists(kept))
        self.assertFalse(os.path.exists(gone))

    def test_foreign_files_in_legacy_dir_untouched(self):
        mine = self.legacy_file("notes.txt")
        image.cleanup_copies(set())
        self.assertTrue(os.path.exists(mine))


class RenameTest(unittest.TestCase):
    """Old names survive only in the legacy-migration code paths."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ALLOWED = {"install.sh", "README.md", "tenfold_tweak/desktop.py", "tenfold_tweak/image.py"}

    def test_no_leftover_old_names(self):
        hits = {}
        for dirpath, dirs, files in os.walk(self.ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", ".claude", "__pycache__", "tests")]
            for name in files:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, self.ROOT)
                self.assertNotIn("bgchanger", rel)
                self.assertNotIn("kali-bg-changer", rel)
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = [ln for ln in f if "kali-bg-changer" in ln or "bgchanger" in ln]
                if lines:
                    hits[rel] = lines
        self.assertLessEqual(set(hits), self.ALLOWED, hits)
        for rel in ("tenfold_tweak/desktop.py", "tenfold_tweak/image.py"):
            for line in hits.get(rel, []):
                self.assertRegex(line, r"^LEGACY_\w+ = ")
        self.assertEqual(len(hits.get("README.md", [])), 1)  # the migration paragraph


class StatusTest(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.addCleanup(reset_state)
        p = mock.patch.object(core.system, "login_status", return_value=LOGIN)
        p.start()
        self.addCleanup(p.stop)

    def rows(self, e):
        return {r.target: r for r in core.status(e)}

    def test_order_and_desktop_alias(self):
        img = make_image(scratch_dir() + "/a.png")
        core.run("apply", img, ["desktop"], lambda m: True, env())
        result = core.status(env())
        self.assertEqual([r.target for r in result], list(core.TARGETS))
        rows = {r.target: r for r in result}
        self.assertTrue(rows["desktop"].current.startswith(image.COPY_DIR))
        self.assertEqual(rows["lock"].current, rows["desktop"].current)
        self.assertTrue(rows["desktop"].restorable and rows["lock"].restorable)
        self.assertEqual(rows["desktop"].method, core.DESKTOP_METHOD["gnome"])
        self.assertEqual(rows["lock"].method, core.LOCK_RESULT["gnome"])
        self.assertEqual((rows["login"].method, rows["login"].current), (LOGIN["method"], LOGIN["current"]))

    def test_extension_provider_rows(self):
        with mock.patch.object(desktop, "current_images", return_value={"desktop": "/d.png", "lock": "/l.png"}):
            rows = self.rows(env(provider=UUID))
        self.assertEqual(rows["lock"].method, f"GNOME Shell extension {UUID}")
        self.assertEqual(rows["lock"].current, "/l.png")
        self.assertFalse(rows["lock"].restorable)

    def test_unavailable_target_reason(self):
        rows = self.rows(env(de="unsupported(KDE)", unavailable={
            "desktop": "unsupported: desktop environment KDE", "lock": "unsupported: desktop environment KDE"}))
        self.assertFalse(rows["desktop"].available)
        self.assertEqual(rows["desktop"].reason, "unsupported: desktop environment KDE")
        self.assertEqual((rows["desktop"].current, rows["desktop"].method), (None, ""))
        self.assertTrue(rows["login"].available)
        self.assertEqual(rows["login"].reason, "")

    def test_errors_degrade_instead_of_raising(self):
        with mock.patch.object(desktop, "current_images", side_effect=RuntimeError("dconf")), \
                mock.patch.object(core.system, "login_status", side_effect=OSError("boom")):
            rows = self.rows(env())
        self.assertIsNone(rows["desktop"].current)
        self.assertEqual((rows["login"].method, rows["login"].current, rows["login"].restorable), ("", None, False))

    def test_status_is_read_only(self):
        before = {k: gnome().get_string(k) for k in desktop.GNOME_KEYS}
        with mock.patch.object(desktop, "save_backup", side_effect=AssertionError("wrote backup")), \
                mock.patch.object(image, "cleanup_copies", side_effect=AssertionError("cleaned")):
            self.rows(env())
        self.assertEqual({k: gnome().get_string(k) for k in desktop.GNOME_KEYS}, before)
        self.assertFalse(os.path.exists(desktop.BACKUP_FILE))


if __name__ == "__main__":
    unittest.main()
