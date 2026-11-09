"""Lock-screen extension detection (D-Bus and settings fallback) and the real schema write path."""

import os
import shutil
import subprocess
import unittest
from unittest import mock

from gi.repository import Gio, GLib

from tests import SANDBOX
from tests.helpers import make_image, scratch_dir
from tests.test_desktop import gnome, reset_state

from tenfold_tweak import core, desktop, detect

UUID = "unlockDialogBackground@sun.wxg@gmail.com"
SCHEMA_ID = desktop.LOCK_EXTENSIONS[UUID]
SCHEMA_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<schemalist>
  <schema id="{SCHEMA_ID}" path="/org/gnome/shell/extensions/unlock-dialog-background/">
    <key name="picture-uri" type="s"><default>'file:///orig/lock.png'</default></key>
    <key name="picture-uri-dark" type="s"><default>'file:///orig/lock-dark.png'</default></key>
    <key name="picture-options" type="s"><default>'zoom'</default></key>
  </schema>
</schemalist>
"""


def make_extension(root: str, schema=True) -> str:
    """Installed extension dir (metadata.json, optional compiled schema) under root."""
    ext = os.path.join(root, "gnome-shell", "extensions", UUID)
    os.makedirs(os.path.join(ext, "schemas"), exist_ok=True)
    with open(os.path.join(ext, "metadata.json"), "w") as f:
        f.write('{"uuid": "%s"}' % UUID)
    if schema:
        with open(os.path.join(ext, "schemas", SCHEMA_ID + ".gschema.xml"), "w") as f:
            f.write(SCHEMA_XML)
        subprocess.run(["glib-compile-schemas", os.path.join(ext, "schemas")], check=True)
    return ext


class FakeBus:
    def __init__(self, info=None, error=False):
        self.info, self.error, self.calls = info or {}, error, []

    def call_sync(self, name, path, iface, method, params, *rest):
        self.calls.append((name, method, params.unpack()))
        if self.error:
            raise GLib.Error("org.freedesktop.DBus.Error.ServiceUnknown")
        return GLib.Variant("(a{sv})", ({k: GLib.Variant(t, v) for k, (t, v) in self.info.items()},))


def shell_settings(enabled=(), disabled=(), disable_user=False):
    s = Gio.Settings.new(desktop.SHELL_SCHEMA)
    s.set_strv("enabled-extensions", list(enabled))
    s.set_strv("disabled-extensions", list(disabled))
    s.set_boolean("disable-user-extensions", disable_user)


def reset_shell():
    s = Gio.Settings.new(desktop.SHELL_SCHEMA)
    for k in ("enabled-extensions", "disabled-extensions", "disable-user-extensions"):
        s.reset(k)


class Base(unittest.TestCase):
    def setUp(self):
        assert os.environ["GSETTINGS_BACKEND"] == "memory"
        reset_state()
        self.addCleanup(reset_state)
        self.addCleanup(reset_shell)
        root = scratch_dir("lockdet-")
        self.user = os.path.join(root, "user")
        self.system = os.path.join(root, "system")
        os.makedirs(self.user)
        os.makedirs(self.system)
        # Keep the real data dirs after ours: GdkPixbuf/glycin find their loaders through them.
        real = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
        p = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.user, "XDG_DATA_DIRS": f"{self.system}:{real}"})
        p.start()
        self.addCleanup(p.stop)

    def bus(self, **kw):
        bus = FakeBus(**kw)
        p = mock.patch.object(desktop.Gio, "bus_get_sync", return_value=bus)
        p.start()
        self.addCleanup(p.stop)
        return bus


class DbusDetectionTest(Base):
    def test_active_via_dbus_uses_reported_path(self):
        ext = make_extension(self.system)
        bus = self.bus(info={"state": ("d", 1.0), "path": ("s", ext)})
        self.assertEqual(desktop.gnome_lock_provider(), (UUID, ext, None))
        self.assertEqual(bus.calls[0][1:], ("GetExtensionInfo", (UUID,)))

    def test_inactive_via_dbus_is_no_provider_even_if_enabled_in_settings(self):
        make_extension(self.user)
        shell_settings(enabled=[UUID])
        self.bus(info={"state": ("d", 2.0), "path": ("s", "/x")})
        self.assertEqual(desktop.gnome_lock_provider(), (None, None, None))

    def test_unknown_extension_empty_info_is_no_provider(self):
        self.bus(info={})
        self.assertEqual(desktop.gnome_lock_provider(), (None, None, None))

    def test_active_but_schema_missing_reports_problem(self):
        ext = make_extension(self.user, schema=False)
        self.bus(info={"state": ("d", 1.0), "path": ("s", ext)})
        uuid, ext_dir, problem = desktop.gnome_lock_provider()
        self.assertEqual((uuid, ext_dir), (UUID, ext))
        self.assertIn("schema cannot be loaded", problem)


class SettingsFallbackTest(Base):
    """gnome-shell unreachable: org.gnome.shell keys plus the installed dir decide."""

    def setUp(self):
        super().setUp()
        self.bus(error=True)

    def test_enabled_user_extension_is_provider(self):
        ext = make_extension(self.user)
        shell_settings(enabled=[UUID])
        self.assertEqual(desktop.gnome_lock_provider(), (UUID, ext, None))

    def test_not_in_enabled_list(self):
        make_extension(self.user)
        self.assertEqual(desktop.gnome_lock_provider(), (None, None, None))

    def test_listed_in_disabled_extensions(self):
        make_extension(self.user)
        shell_settings(enabled=[UUID], disabled=[UUID])
        self.assertEqual(desktop.gnome_lock_provider(), (None, None, None))

    def test_enabled_but_not_installed(self):
        shell_settings(enabled=[UUID])
        self.assertEqual(desktop.gnome_lock_provider(), (None, None, None))

    def test_disable_user_extensions_blocks_user_dir(self):
        make_extension(self.user)
        shell_settings(enabled=[UUID], disable_user=True)
        self.assertEqual(desktop.gnome_lock_provider(), (None, None, None))

    def test_disable_user_extensions_keeps_system_dir(self):
        ext = make_extension(self.system)
        shell_settings(enabled=[UUID], disable_user=True)
        self.assertEqual(desktop.gnome_lock_provider(), (UUID, ext, None))

    def test_user_dir_preferred_over_system_dir(self):
        user_ext = make_extension(self.user)
        make_extension(self.system)
        shell_settings(enabled=[UUID])
        self.assertEqual(desktop.gnome_lock_provider()[1], user_ext)

    def test_enabled_without_schema_reports_problem(self):
        make_extension(self.user, schema=False)
        shell_settings(enabled=[UUID])
        self.assertIn("cannot be loaded", desktop.gnome_lock_provider()[2])


class ExtensionWriteTest(Base):
    """Criterion 1: lock writes the extension's picture-uri, restore brings it back, desktop untouched."""

    def setUp(self):
        super().setUp()
        self.ext = make_extension(self.user)
        self.bus(info={"state": ("d", 1.0), "path": ("s", self.ext)})
        self.lock = desktop._lock_settings(UUID, self.ext)
        self.assertIsNotNone(self.lock)
        self.addCleanup(lambda: [self.lock.reset(k) for k in desktop.GNOME_KEYS])
        self.img = make_image(scratch_dir() + "/lock.png")

    def env(self):
        with mock.patch.object(detect, "detect_de", return_value="gnome"), \
                mock.patch.object(detect, "detect_dm", return_value="gdm3"), \
                mock.patch.object(detect, "login_reason", return_value="unsupported: test"):
            return detect.detect()

    def no_prompt(self, message):
        raise AssertionError(f"unexpected prompt: {message}")

    def state(self, s):
        return {k: s.get_string(k) for k in desktop.GNOME_KEYS}

    def test_apply_writes_extension_and_restore_returns_it(self):
        e = self.env()
        self.assertEqual((e.lock_provider, e.lock_provider_dir), (UUID, self.ext))
        before_lock, before_desktop = self.state(self.lock), self.state(gnome())
        res = {r.target: r for r in core.run("apply", self.img, ["lock"], self.no_prompt, e)}
        self.assertEqual(res["lock"].status, core.APPLIED, res["lock"].reason)
        uri = self.lock.get_string("picture-uri")
        self.assertTrue(uri.startswith("file://" + SANDBOX), uri)
        self.assertEqual(self.lock.get_string("picture-uri-dark"), uri)
        self.assertTrue(os.path.isfile(desktop._uri_path(uri)), "copy kept while referenced")
        self.assertEqual(self.state(gnome()), before_desktop)
        self.assertFalse(desktop.has_backup("gnome"))
        self.assertEqual(desktop.load_backup()["gnome-lock"]["dir"], self.ext)
        self.assertEqual(desktop.current_images("gnome", UUID, self.ext)["lock"], desktop._uri_path(uri))

        res = {r.target: r for r in core.run("restore", None, ["lock"], self.no_prompt, e)}
        self.assertEqual(res["lock"].status, core.APPLIED, res["lock"].reason)
        self.assertEqual(self.state(self.lock), before_lock)
        self.assertEqual(self.state(gnome()), before_desktop)
        self.assertFalse(desktop.has_backup("gnome-lock"))
        self.assertFalse(os.path.exists(desktop._uri_path(uri)), "unreferenced copy cleaned up")

    def test_restore_twice_is_nothing_to_restore(self):
        e = self.env()
        core.run("apply", self.img, ["lock"], self.no_prompt, e)
        core.run("restore", None, ["lock"], self.no_prompt, e)
        res = {r.target: r for r in core.run("restore", None, ["lock"], self.no_prompt, e)}
        self.assertEqual((res["lock"].status, res["lock"].reason), (core.SKIPPED, "nothing to restore"))

    def test_reapply_keeps_first_backup(self):
        e = self.env()
        before = self.state(self.lock)
        core.run("apply", self.img, ["lock"], self.no_prompt, e)
        core.run("apply", make_image(scratch_dir() + "/b.jpg", fmt="jpeg"), ["lock"], self.no_prompt, e)
        core.run("restore", None, ["lock"], self.no_prompt, e)
        self.assertEqual(self.state(self.lock), before)

    def test_without_extension_lock_is_desktop_alias(self):
        shutil.rmtree(self.ext)
        self.bus(info={})
        e = self.env()
        self.assertIsNone(e.lock_provider)
        self.assertTrue(e.available("lock"))
        asked = []
        res = {r.target: r for r in core.run("apply", self.img, ["lock"], lambda m: asked.append(m) or True, e)}
        self.assertEqual(len(asked), 1)
        self.assertEqual(res["lock"].status, core.APPLIED)
        self.assertTrue(gnome().get_string("picture-uri").startswith("file://" + SANDBOX))


if __name__ == "__main__":
    unittest.main()
