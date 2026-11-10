"""migrate.migrate_user / desktop.relocate: per-user state moves from tenfold-tweak to tenfold-tweaks.

In-memory GSettings, XDG dirs in the test sandbox (tests/__init__), fake xfconf-query for XFCE.
"""

import json
import os
import shutil
import unittest
from unittest import mock

from gi.repository import Gio

from tests import SANDBOX
from tests.helpers import make_image, scratch_dir
from tests.test_desktop import FakeXfconf, gnome, reset_state
from tests.test_lock_detection import UUID, make_extension

from tenfold_tweak import core, desktop, detect, image, migrate

OLD_COPY, NEW_COPY = image.LEGACY_COPY_DIR, image.COPY_DIR
OLD_STATE, NEW_STATE = desktop.LEGACY_STATE_DIR, desktop.STATE_DIR
WALL, LOGIN = "wallpaper-0123456789ab.png", "login-0123456789ab.png"
XBASE = "/backdrop/screen0/monitor"


def uri(path: str) -> str:
    return Gio.File.new_for_path(path).get_uri()


def tree(*roots) -> dict:
    out = {}
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            for name in dirs + files:
                full = os.path.join(dirpath, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    out[full] = os.readlink(full) if os.path.islink(full) else "dir"
                else:
                    with open(full, "rb") as f:
                        out[full] = f.read()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        for d in (OLD_COPY, NEW_COPY, OLD_STATE, NEW_STATE):
            assert d.startswith(SANDBOX), d
        self.clean()
        self.addCleanup(self.clean)
        migrate._done = False
        self.addCleanup(setattr, migrate, "_done", False)
        os.makedirs(OLD_COPY, mode=0o700)
        self.wall = make_image(os.path.join(OLD_COPY, WALL))
        self.login = make_image(os.path.join(OLD_COPY, LOGIN))
        with open(self.wall, "rb") as f:
            self.wall_bytes = f.read()

    def clean(self):
        reset_state()
        for d in (OLD_COPY, NEW_COPY, OLD_STATE, NEW_STATE):
            shutil.rmtree(d, ignore_errors=True)

    def old_backup(self, data: dict):
        os.makedirs(OLD_STATE, mode=0o700, exist_ok=True)
        with open(os.path.join(OLD_STATE, "backup.json"), "w") as f:
            json.dump(data, f)


class GnomeMigrateTest(Base):
    def setUp(self):
        super().setUp()
        self.ext = make_extension(scratch_dir())
        self.lock = desktop._lock_settings(UUID, self.ext)
        self.addCleanup(lambda: [self.lock.reset(k) for k in desktop.GNOME_KEYS])
        s = gnome()
        s.set_string("picture-uri", uri(self.wall))
        s.set_string("picture-uri-dark", uri(self.wall))
        s.set_string("picture-options", "zoom")
        self.lock.set_string("picture-uri", uri(self.login))
        self.lock.set_string("picture-uri-dark", uri(self.login))
        Gio.Settings.sync()
        self.old_backup({"gnome": {k: None for k in desktop.GNOME_KEYS},
                         "gnome-lock": {"dir": self.ext, "picture-uri": "file:///orig/lock.png",
                                        "picture-uri-dark": None, "picture-options": None},
                         "note": uri(self.wall)})

    def test_copies_settings_and_backup_move(self):
        warnings = migrate.migrate_user("gnome", self.ext)
        self.assertEqual(warnings, [])
        new_wall, new_login = os.path.join(NEW_COPY, WALL), os.path.join(NEW_COPY, LOGIN)
        with open(new_wall, "rb") as f:
            self.assertEqual(f.read(), self.wall_bytes)
        self.assertTrue(os.path.isfile(new_login))
        self.assertFalse(os.path.lexists(OLD_COPY))
        self.assertEqual(os.stat(NEW_COPY).st_mode & 0o777, 0o700)
        s = gnome()
        self.assertEqual(s.get_string("picture-uri"), uri(new_wall))
        self.assertEqual(s.get_string("picture-uri-dark"), uri(new_wall))
        self.assertEqual(self.lock.get_string("picture-uri"), uri(new_login))
        self.assertEqual(self.lock.get_string("picture-uri-dark"), uri(new_login))
        self.assertFalse(os.path.lexists(OLD_STATE))
        backup = desktop.load_backup()
        self.assertEqual(backup["gnome"], {k: None for k in desktop.GNOME_KEYS})
        self.assertEqual(backup["gnome-lock"]["picture-uri"], "file:///orig/lock.png")
        self.assertEqual(backup["note"], uri(new_wall))
        self.assertEqual(desktop.current_paths("gnome", self.ext), {new_wall, new_login})

    def test_existing_new_backup_wins_on_merge(self):
        desktop.save_backup({"gnome": {"picture-uri": "file:///new.png", "picture-uri-dark": None,
                                       "picture-options": None}})
        self.assertEqual(migrate.migrate_user("gnome", self.ext), [])
        backup = desktop.load_backup()
        self.assertEqual(backup["gnome"]["picture-uri"], "file:///new.png")
        self.assertIn("gnome-lock", backup)
        self.assertFalse(os.path.lexists(OLD_STATE))

    def test_failure_rolls_back_and_keeps_old_files(self):
        before = (dict(desktop._gnome_state(gnome())), dict(desktop._gnome_state(self.lock)))
        real = desktop._gnome_write
        calls = []

        def flaky(s, values):
            calls.append(s.props.schema_id)
            if s.props.schema_id == desktop.LOCK_EXTENSIONS[UUID] and len(calls) == 2:
                raise desktop.DesktopError("write refused")
            return real(s, values)

        with mock.patch.object(desktop, "_gnome_write", flaky):
            warnings = migrate.migrate_user("gnome", self.ext)
        self.assertEqual(len(warnings), 1)
        self.assertIn("cannot move images from " + OLD_COPY, warnings[0])
        self.assertEqual((desktop._gnome_state(gnome()), desktop._gnome_state(self.lock)), before)
        with open(self.wall, "rb") as f:
            self.assertEqual(f.read(), self.wall_bytes)  # still shown: the referenced file stays
        self.assertTrue(os.path.isfile(self.login))
        self.assertTrue(all(p.startswith(OLD_COPY) for p in desktop.current_paths("gnome", self.ext)))

    def test_retried_next_run_after_failure(self):
        with mock.patch.object(desktop, "_gnome_write", side_effect=desktop.DesktopError("boom")):
            self.assertTrue(migrate.migrate_user("gnome", self.ext))
        migrate._done = False  # a new process
        self.assertEqual(migrate.migrate_user("gnome", self.ext), [])
        self.assertFalse(os.path.lexists(OLD_COPY))
        self.assertEqual(gnome().get_string("picture-uri"), uri(os.path.join(NEW_COPY, WALL)))

    def test_idempotent(self):
        migrate.migrate_user("gnome", self.ext)
        snap = (tree(SANDBOX + "/data", SANDBOX + "/state"), desktop._gnome_state(gnome()),
                desktop._gnome_state(self.lock))
        migrate._done = False
        self.assertEqual(migrate.migrate_user("gnome", self.ext), [])
        self.assertEqual((tree(SANDBOX + "/data", SANDBOX + "/state"), desktop._gnome_state(gnome()),
                          desktop._gnome_state(self.lock)), snap)

    def test_once_per_process_even_after_failure(self):
        with mock.patch.object(desktop, "relocate", side_effect=desktop.DesktopError("boom")) as relocate:
            self.assertTrue(migrate.migrate_user("gnome", self.ext))
            self.assertEqual(migrate.migrate_user("gnome", self.ext), [])
            self.assertEqual(relocate.call_count, 1)
        self.assertTrue(os.path.isfile(self.wall))

    def test_core_runs_it_once_per_process(self):
        e = detect.Environment("gnome", "gdm3", {"desktop": None, "lock": None, "login": "unsupported: test"},
                               lock_provider=UUID, lock_provider_dir=self.ext)
        with mock.patch.object(migrate, "_migrate_backup", wraps=migrate._migrate_backup) as backup:
            core._migrate(e)
            core._migrate(e)
        self.assertEqual(backup.call_count, 1)
        self.assertFalse(os.path.lexists(OLD_COPY))

    def test_foreign_and_non_regular_entries_left_in_place(self):
        with open(os.path.join(OLD_COPY, "notes.txt"), "w") as f:
            f.write("mine")
        os.symlink("/etc/passwd", os.path.join(OLD_COPY, "wallpaper-link.png"))
        warnings = migrate.migrate_user("gnome", self.ext)
        self.assertEqual(warnings, [f"{OLD_COPY} still holds other files; left in place"])
        self.assertEqual(sorted(os.listdir(OLD_COPY)), ["notes.txt", "wallpaper-link.png"])
        self.assertFalse(os.path.lexists(os.path.join(NEW_COPY, "wallpaper-link.png")))
        self.assertTrue(os.path.isfile(os.path.join(NEW_COPY, WALL)))

    def test_unknown_desktop_moves_backup_only(self):
        self.assertEqual(migrate.migrate_user(None, None), [])
        self.assertTrue(os.path.isfile(self.wall))
        self.assertTrue(os.path.isfile(desktop.BACKUP_FILE))
        self.assertFalse(os.path.lexists(OLD_STATE))


class XfceMigrateTest(Base):
    def setUp(self):
        super().setUp()
        self.props = {f"{XBASE}eDP-1/workspace0/last-image": self.wall,
                      f"{XBASE}eDP-1/workspace1/last-image": "/usr/share/backgrounds/kali.png",
                      f"{XBASE}HDMI-1/workspace0/last-image": self.wall,
                      f"{XBASE}eDP-1/workspace0/image-style": "5"}
        self.fake = FakeXfconf(self.props)
        p = mock.patch.object(desktop.subprocess, "run", self.fake)
        p.start()
        self.addCleanup(p.stop)
        self.old_backup({"xfce": {f"{XBASE}eDP-1/workspace0/last-image": "/usr/share/backgrounds/kali.png"}})

    def test_settings_point_at_new_dir(self):
        self.assertEqual(migrate.migrate_user("xfce", None), [])
        new_wall = os.path.join(NEW_COPY, WALL)
        self.assertEqual(self.fake.props[f"{XBASE}eDP-1/workspace0/last-image"], new_wall)
        self.assertEqual(self.fake.props[f"{XBASE}HDMI-1/workspace0/last-image"], new_wall)
        self.assertEqual(self.fake.props[f"{XBASE}eDP-1/workspace1/last-image"], "/usr/share/backgrounds/kali.png")
        self.assertTrue(os.path.isfile(new_wall))
        self.assertFalse(os.path.lexists(OLD_COPY))
        self.assertEqual(desktop.load_backup()["xfce"],
                         {f"{XBASE}eDP-1/workspace0/last-image": "/usr/share/backgrounds/kali.png"})

    def test_write_failure_rolls_back(self):
        before = dict(self.fake.props)
        self.fake.fail_set = {f"{XBASE}HDMI-1/workspace0/last-image"}
        warnings = migrate.migrate_user("xfce", None)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(self.fake.props, before)
        self.assertTrue(os.path.isfile(self.wall))


class UserTraceNamesTest(unittest.TestCase):
    """Rename completeness, user side: every path an apply/restore cycle creates carries tenfold-tweaks."""

    def setUp(self):
        reset_state()
        self.addCleanup(reset_state)
        for d in (OLD_COPY, NEW_COPY, OLD_STATE, NEW_STATE):
            shutil.rmtree(d, ignore_errors=True)
        self.ext = make_extension(scratch_dir())
        self.lock = desktop._lock_settings(UUID, self.ext)
        self.addCleanup(lambda: [self.lock.reset(k) for k in desktop.GNOME_KEYS])

    def test_apply_restore_cycle_paths(self):
        e = detect.Environment("gnome", "gdm3", {"desktop": None, "lock": None, "login": "unsupported: test"},
                               lock_provider=UUID, lock_provider_dir=self.ext)
        roots = [SANDBOX + "/data", SANDBOX + "/state", SANDBOX + "/config"]
        img = make_image(scratch_dir() + "/pick.png")
        before = tree(*roots)
        seen = set()
        no_prompt = lambda m: self.fail(m)  # noqa: E731
        for action, path in (("apply", img), ("apply", make_image(scratch_dir() + "/b.jpg", fmt="jpeg")),
                             ("restore", None)):
            res = core.run(action, path, ["desktop", "lock"], no_prompt, e)
            res = [r for r in res if r.target in ("desktop", "lock")]
            self.assertTrue(all(r.status == core.APPLIED for r in res), [(r.target, r.reason) for r in res])
            seen |= {p for p, v in tree(*roots).items() if before.get(p) != v}
        self.assertTrue(seen)
        # gvfs-metadata is GIO's own store (exists on any desktop), not a tool trace.
        bad = [p for p in seen if "tenfold-tweaks" not in p and "/gvfs-metadata" not in p]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
