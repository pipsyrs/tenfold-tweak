"""packaging/: build smoke test and maintainer scripts run by a real dpkg in a scratch root.

dpkg runs with --root (chrootless scripts, so every script path must carry $DPKG_ROOT) inside an
unprivileged user namespace (unshare -r): the scripts and the root helper see euid 0, while the host
system stays out of reach. Never point ScratchRoot at /.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "packaging", "build-deb.sh")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}

PKG = "tenfold-tweaks"
with open(os.path.join(ROOT, "packaging", "VERSION"), encoding="utf-8") as _f:
    VERSION = _f.read().strip()
BG = "/usr/share/desktop-base/kali-theme/login/background-blurred"
STOCK_TARGET = "../../../backgrounds/kali/login-blurred"
OLD = BG + ".tenfold-tweak"
NEW = BG + ".tenfold-tweaks"
STATE = "/var/lib/tenfold-tweaks"
OLD_STATE = "/var/lib/tenfold-tweak"
RESCUE = "/var/backups/tenfold-tweaks"
LIGHTDM_CONF = "/etc/lightdm/lightdm-gtk-greeter.conf"
OLD_LIGHTDM_IMAGE = "/usr/local/share/backgrounds/tenfold-tweak/login.jpg"
NEW_LIGHTDM_IMAGE = STATE + "/lightdm/login.jpg"

USER_IMAGE = b"\x89PNG\r\n\x1a\n" + b"user login image" * 64
MIGRATED_IMAGE = b"\xff\xd8\xff\xe0" + b"migrated login" * 64
LIGHTDM_IMAGE = b"\xff\xd8\xff\xe0" + b"lightdm login" * 64


def tool_ok() -> bool:
    if not (shutil.which("dpkg-deb") and shutil.which("dpkg") and shutil.which("unshare")):
        return False
    return subprocess.run(["unshare", "-r", "true"], capture_output=True).returncode == 0


def sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def build(out_dir: str, version: str | None = None) -> str:
    """Run packaging/build-deb.sh into out_dir; returns the .deb path."""
    env = dict(ENV, BUILD_DIR=os.path.join(out_dir, "build"), DIST_DIR=out_dir)
    if version:
        env["DEB_VERSION"] = version
    res = subprocess.run(["sh", BUILD], env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"build-deb.sh failed: {res.stderr}")
    return res.stdout.strip().splitlines()[-1]


class ScratchRoot:
    """A dpkg instdir/admindir under a temp dir, with helpers to seed tenfold-tweak (install.sh) state."""

    def __init__(self, base: str):
        self.root = os.path.join(base, "root")
        self.admindir = self.root + "/var/lib/dpkg"
        for d in ("info", "updates", "triggers"):
            os.makedirs(os.path.join(self.admindir, d), exist_ok=True)
        for f in ("status", "available"):
            open(os.path.join(self.admindir, f), "w").close()

    def p(self, path: str) -> str:
        return self.root + path

    def run(self, *cmd, check: bool = True) -> subprocess.CompletedProcess:
        res = subprocess.run(["unshare", "-r", *cmd], env=ENV, capture_output=True, text=True, timeout=300)
        if check and res.returncode != 0:
            raise AssertionError(f"{cmd} exited {res.returncode}:\n{res.stdout}\n{res.stderr}")
        return res

    def dpkg(self, *args, check: bool = True) -> subprocess.CompletedProcess:
        return self.run("dpkg", "--force-not-root", "--force-script-chrootless", "--force-depends",
                        f"--log={self.root}/dpkg.log", f"--root={self.root}", *args, check=check)

    def make_chroot(self):
        """Let the scratch root run host tools: host /usr shows up read-only at /.host/usr inside chroot()."""
        os.makedirs(self.p("/.host/usr"))
        os.makedirs(self.p("/usr/bin"), exist_ok=True)
        os.makedirs(self.p("/tmp"), exist_ok=True)
        os.makedirs(self.p("/dev"), exist_ok=True)
        open(self.p("/dev/null"), "w").close()
        for name in ("lib", "lib32", "lib64", "libx32"):
            if os.path.isdir("/usr/" + name):
                os.symlink(".host/usr/" + name, self.p("/" + name))
                os.symlink("../.host/usr/" + name, self.p("/usr/" + name))
        os.symlink("usr/bin", self.p("/bin"))
        os.symlink("usr/sbin", self.p("/sbin"))
        # Shebangs of the maintainer scripts and of the root helper, and the helper's absolute dpkg-divert.
        for name in ("sh", "python3", "dpkg-divert"):
            os.symlink("/.host/usr/bin/" + name, self.p("/usr/bin/" + name))

    def chroot(self, *cmd, check: bool = True) -> subprocess.CompletedProcess:
        """Run cmd chrooted into the scratch root, in a private user + mount namespace (gone on exit)."""
        setup = ('set -e; R=$1; shift; mount --rbind /usr "$R/.host/usr"; mount -o remount,bind,ro "$R/.host/usr"; '
                 'mount --bind /dev/null "$R/dev/null"; exec chroot "$R" "$@"')
        env = dict(ENV, PATH=ENV["PATH"] + ":/.host/usr/sbin:/.host/usr/bin")
        res = subprocess.run(["unshare", "-r", "-m", "sh", "-c", setup, "sh", self.root, *cmd],
                             env=env, capture_output=True, text=True, timeout=300)
        if check and res.returncode != 0:
            raise AssertionError(f"chroot {cmd} exited {res.returncode}:\n{res.stdout}\n{res.stderr}")
        return res

    def divert(self, *args) -> str:
        return self.run("dpkg-divert", "--instdir", self.root, "--admindir", self.admindir, *args).stdout.strip()

    def status(self) -> str:
        res = self.run("dpkg-query", f"--admindir={self.admindir}", "-W", "-f=${Status} ${Version}", PKG, check=False)
        return res.stdout.strip()

    def write(self, path: str, data: bytes, mode: int = 0o644):
        os.makedirs(os.path.dirname(self.p(path)), exist_ok=True)
        with open(self.p(path), "wb") as f:
            f.write(data)
        os.chmod(self.p(path), mode)

    def read(self, path: str) -> bytes:
        with open(self.p(path), "rb") as f:
            return f.read()

    def symlink(self, target: str, path: str):
        os.makedirs(os.path.dirname(self.p(path)), exist_ok=True)
        os.symlink(target, self.p(path))

    def exists(self, path: str) -> bool:
        return os.path.lexists(self.p(path))

    def seed_kali(self):
        """Stock kali-themes-common layout: the login image path is a symlink."""
        self.write("/usr/share/backgrounds/kali/login-blurred", b"stock")
        self.symlink(STOCK_TARGET, BG)

    def seed_legacy(self, migrated_login: bool = False, lightdm: bool = False):
        """What install.sh plus a 'tenfold-tweak apply --login' left on this machine."""
        self.seed_kali()
        self.divert("--local", "--rename", "--divert", OLD, "--add", BG)
        self.write(BG, USER_IMAGE)
        self.write(OLD_STATE + "/lock", b"", 0o600)
        self.write(OLD_STATE + "/kali-login.divert", b"")
        self.write("/usr/local/lib/tenfold-tweak/tenfold-tweak", b"#!/usr/bin/env python3\n", 0o755)
        self.write("/usr/local/lib/tenfold-tweak/tenfold_tweak/root_helper.py", b"")
        self.symlink("/usr/local/lib/tenfold-tweak/tenfold-tweak", "/usr/local/bin/tenfold-tweak")
        self.write("/usr/local/share/applications/io.github.tenfold.TenfoldTweak.desktop", b"[Desktop Entry]\n")
        if migrated_login:
            self.write(OLD_STATE + "/migrated-login", MIGRATED_IMAGE)
        if lightdm:
            self.write(LIGHTDM_CONF + "", b"[greeter]\nbackground=" + OLD_LIGHTDM_IMAGE.encode() + b"\n")
            self.write(OLD_STATE + "/lightdm-gtk-greeter.conf.orig", b"[greeter]\nbackground=stock\n")
            self.write(OLD_LIGHTDM_IMAGE, LIGHTDM_IMAGE)

    def install_blocker(self, path: str):
        """Install a dummy package owning path, so unpacking tenfold-tweaks fails after its preinst."""
        tree = os.path.join(os.path.dirname(self.root), "blocker")
        os.makedirs(os.path.join(tree, "DEBIAN"))
        with open(os.path.join(tree, "DEBIAN", "control"), "w") as f:
            f.write("Package: blocker\nVersion: 1\nArchitecture: all\nMaintainer: t <t@t>\nDescription: t\n")
        os.makedirs(os.path.dirname(tree + path))
        with open(tree + path, "w") as f:
            f.write("blocker\n")
        deb = tree + ".deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", tree, deb], capture_output=True, check=True)
        self.dpkg("-i", deb)

    def snapshot(self) -> dict:
        """Path -> (kind, mode, content) for everything outside the dpkg database."""
        snap = {}
        for dirpath, dirs, files in os.walk(self.root):
            rel = dirpath[len(self.root):] or "/"
            if rel.startswith("/var/lib/dpkg"):
                dirs[:] = []
                continue
            for name in dirs + files:
                full = os.path.join(dirpath, name)
                path = full[len(self.root):]
                st = os.lstat(full)
                if os.path.islink(full):
                    snap[path] = ("link", os.readlink(full))
                elif os.path.isdir(full):
                    snap[path] = ("dir", st.st_mode & 0o7777)
                elif path != "/dpkg.log":
                    with open(full, "rb") as f:
                        snap[path] = ("file", st.st_mode & 0o7777, hashlib.sha256(f.read()).hexdigest())
        return snap


@unittest.skipUnless(tool_ok(), "needs dpkg-deb, dpkg and unprivileged user namespaces")
class PackagingTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tt-deb-")
        cls.deb = build(os.path.join(cls.tmp, "dist"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))  # the scripts refuse group-writable state dirs
        self.base = tempfile.mkdtemp(prefix="case-", dir=self.tmp)
        self.addCleanup(shutil.rmtree, self.base, True)
        self.r = ScratchRoot(self.base)


class BuildTest(PackagingTestBase):
    def contents(self) -> list:
        out = subprocess.run(["dpkg-deb", "-c", self.deb], capture_output=True, text=True, check=True).stdout
        rows = []
        for line in out.splitlines():
            parts = line.split()
            rows.append((parts[0], parts[1], parts[5].lstrip("."), parts[7] if len(parts) > 7 else None))
        return rows

    def test_name_and_fields(self):
        self.assertEqual(os.path.basename(self.deb), f"tenfold-tweaks_{VERSION}_all.deb")
        info = subprocess.run(["dpkg-deb", "-f", self.deb], capture_output=True, text=True, check=True).stdout
        self.assertIn("Package: tenfold-tweaks\n", info)
        self.assertIn(f"Version: {VERSION}\n", info)
        self.assertIn("Architecture: all\n", info)
        self.assertRegex(info, r"Installed-Size: \d+\n")

    def test_layout_owner_mode(self):
        rows = {path: (perm, owner, link) for perm, owner, path, link in self.contents()}
        self.assertTrue(all(owner == "root/root" for _, owner, _ in rows.values()))
        self.assertFalse([p for p in rows if p.endswith((".pyc", ".pyo")) or "__pycache__" in p])
        self.assertEqual(rows["/usr/libexec/tenfold-tweaks/root-helper"][0], "-rwxr-xr-x")
        self.assertEqual(rows["/usr/share/tenfold-tweaks/tenfold-tweaks"][0], "-rwxr-xr-x")
        self.assertEqual(rows["/usr/share/tenfold-tweaks/tenfold-tweaks-gui"][0], "-rwxr-xr-x")
        self.assertEqual(rows["/usr/bin/tenfold-tweaks"][2], "../share/tenfold-tweaks/tenfold-tweaks")
        self.assertEqual(rows["/usr/bin/tenfold-tweaks-gui"][2], "../share/tenfold-tweaks/tenfold-tweaks-gui")
        for p in ("/usr/share/applications/io.github.tenfold.tenfold-tweaks.desktop",
                  "/usr/share/polkit-1/actions/io.github.tenfold.tenfold-tweaks.policy",
                  "/usr/share/tenfold-tweaks/tenfold_tweak/migrate.py"):
            self.assertEqual(rows[p][0], "-rw-r--r--", p)
        for path, (perm, _, _) in rows.items():
            if perm.startswith("d"):
                self.assertEqual(perm, "drwxr-xr-x", path)
        modules = sorted(p for p in rows if p.startswith("/usr/share/tenfold-tweaks/tenfold_tweak/") and p.endswith(".py"))
        expected = sorted("/usr/share/tenfold-tweaks/tenfold_tweak/" + n
                          for n in os.listdir(os.path.join(ROOT, "tenfold_tweak")) if n.endswith(".py"))
        self.assertEqual(modules, expected)

    def test_helper_byte_identical_and_maint_scripts(self):
        out = os.path.join(self.base, "x")
        subprocess.run(["dpkg-deb", "-R", self.deb, out], check=True)
        with open(os.path.join(out, "usr/libexec/tenfold-tweaks/root-helper"), "rb") as a, \
                open(os.path.join(ROOT, "tenfold_tweak/root_helper.py"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        for s in ("preinst", "postinst", "prerm", "postrm"):
            path = os.path.join(out, "DEBIAN", s)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o755)
            with open(path) as f:
                text = f.read()
            self.assertNotIn("#COMMON#", text)
            self.assertNotIn("@VERSION@", text)
            self.assertIn(f"TT_VERSION={VERSION}", text)
            self.assertEqual(subprocess.run(["sh", "-n", path]).returncode, 0, s)
        with open(os.path.join(out, "usr/share/applications/io.github.tenfold.tenfold-tweaks.desktop")) as f:
            desktop = f.read()
        for line in ("Name=Tenfold Tweaks", "Exec=tenfold-tweaks gui", "StartupWMClass=tenfold-tweaks",
                     "Icon=preferences-desktop-wallpaper"):
            self.assertIn(line + "\n", desktop)


class MaintScriptTest(PackagingTestBase):
    def assert_migrated(self):
        r = self.r
        self.assertEqual(r.divert("--list"), f"diversion of {BG} to {NEW} by {PKG}")
        self.assertEqual(r.read(BG), USER_IMAGE)
        self.assertEqual(os.readlink(r.p(NEW)), STOCK_TARGET)
        self.assertFalse(r.exists(OLD))
        self.assertFalse(r.exists(OLD_STATE))
        self.assertTrue(r.exists(STATE + "/kali-login.divert"))
        self.assertFalse(r.exists(STATE + "/.legacy-migrated"))
        for gone in ("/usr/local/lib/tenfold-tweak", "/usr/local/bin/tenfold-tweak",
                     "/usr/local/share/applications/io.github.tenfold.TenfoldTweak.desktop"):
            self.assertFalse(r.exists(gone), gone)

    def assert_no_traces(self):
        r = self.r
        self.assertEqual(r.divert("--list"), "")
        left = [p for p in r.snapshot() if "tenfold" in p and not p.startswith(RESCUE)]
        self.assertEqual(left, [])

    def test_install_remove_purge_with_legacy(self):
        r = self.r
        r.seed_legacy(migrated_login=True, lightdm=True)
        out = r.dpkg("-i", self.deb).stdout
        self.assertEqual(r.status(), "install ok installed " + VERSION)
        self.assert_migrated()
        saved = f"{RESCUE}/migrated-login-{sha12(MIGRATED_IMAGE)}.jpg"
        self.assertIn("saved login image: " + r.p(saved), out)
        self.assertEqual(r.read(saved), MIGRATED_IMAGE)
        self.assertFalse(r.exists(STATE + "/migrated-login"))
        self.assertIn(b"background=" + NEW_LIGHTDM_IMAGE.encode(), r.read(LIGHTDM_CONF))
        self.assertEqual(r.read(NEW_LIGHTDM_IMAGE), LIGHTDM_IMAGE)
        self.assertFalse(r.exists("/usr/local/share/backgrounds/tenfold-tweak"))

        out = r.dpkg("-r", PKG).stdout
        self.assertEqual(r.divert("--list"), "")
        self.assertEqual(os.readlink(r.p(BG)), STOCK_TARGET)
        self.assertEqual(r.read(LIGHTDM_CONF), b"[greeter]\nbackground=stock\n")
        for data, ext in ((USER_IMAGE, "png"), (LIGHTDM_IMAGE, "jpg")):
            path = f"{RESCUE}/login-{sha12(data)}.{ext}"
            self.assertEqual(r.read(path), data)
            self.assertIn("saved login image: " + r.p(path), out)
        self.assertFalse(r.exists("/usr/bin/tenfold-tweaks"))
        self.assertFalse(r.exists("/usr/share/polkit-1/actions/io.github.tenfold.tenfold-tweaks.policy"))

        before = {p: v for p, v in r.snapshot().items() if p.startswith(RESCUE)}
        out = r.dpkg("--purge", PKG).stdout
        self.assertNotIn(PKG, r.status())
        self.assert_no_traces()
        self.assertEqual({p: v for p, v in r.snapshot().items() if p.startswith(RESCUE)}, before)
        self.assertIn("~/.local/state/tenfold-tweaks", out)
        self.assertIn("saved login image: ", out)

    def test_upgrade_and_reinstall_keep_diversion(self):
        r = self.r
        r.seed_legacy()
        r.dpkg("-i", self.deb)
        self.assert_migrated()
        r.dpkg("-i", self.deb)  # reinstall: preinst upgrade with nothing left to migrate
        self.assert_migrated()
        newer = build(os.path.join(self.base, "dist"), VERSION + "+1")
        r.dpkg("-i", newer)
        self.assertEqual(r.status(), "install ok installed " + VERSION + "+1")
        self.assert_migrated()
        r.dpkg("-i", newer)
        self.assert_migrated()
        r.dpkg("--purge", PKG)
        self.assert_no_traces()

    def test_plain_install_purge(self):
        r = self.r
        r.seed_kali()
        before = r.snapshot()
        r.dpkg("-i", self.deb)
        self.assertEqual(r.divert("--list"), "")
        r.dpkg("--purge", PKG)
        self.assertEqual(r.snapshot(), before)

    def test_preinst_resumes_crash_between_steps_2_and_3(self):
        r = self.r
        r.seed_legacy()
        # Hard crash after step 2: marker written, NEW duplicated, the old diversion record dropped.
        r.write(OLD_STATE + "/.legacy-migrated", b"")
        r.symlink(STOCK_TARGET, NEW)
        r.divert("--local", "--no-rename", "--divert", OLD, "--remove", BG)
        r.dpkg("-i", self.deb)
        self.assert_migrated()

    def test_failed_unpack_reverses_migration(self):
        r = self.r
        r.seed_legacy(migrated_login=True, lightdm=True)
        r.install_blocker("/usr/share/applications/io.github.tenfold.tenfold-tweaks.desktop")
        before = {p: v for p, v in r.snapshot().items() if not p.startswith("/var/backups")}
        diversions = r.divert("--list")
        res = r.dpkg("-i", self.deb, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not-installed", r.status())
        self.assertEqual(r.divert("--list"), diversions)
        after = {p: v for p, v in r.snapshot().items() if not p.startswith("/var/backups")}
        self.assertEqual(after, before)

    def test_preflight_failure_changes_nothing(self):
        r = self.r
        r.seed_legacy()
        r.write(BG + ".tenfold-tweaks", b"foreign")
        before = r.snapshot()
        res = r.dpkg("-i", self.deb, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("unexpected file", res.stderr)
        self.assertEqual(r.snapshot(), before)
        self.assertEqual(r.divert("--list"), f"local diversion of {BG} to {OLD}")

    def test_preflight_refuses_unsafe_old_lightdm_dir(self):
        """Old LightDM image dir that is a symlink, or group-writable: refused, nothing copied."""
        old_dir = os.path.dirname(OLD_LIGHTDM_IMAGE)

        def symlinked(r):
            os.rename(r.p(old_dir), r.p(old_dir + "-elsewhere"))
            os.symlink(os.path.basename(old_dir) + "-elsewhere", r.p(old_dir))

        for name, unsafe in (("symlink", symlinked), ("group-writable", lambda r: os.chmod(r.p(old_dir), 0o775))):
            with self.subTest(name):
                shutil.rmtree(self.base)
                os.makedirs(self.base)
                r = self.r = ScratchRoot(self.base)
                r.seed_legacy(lightdm=True)
                unsafe(r)
                before = r.snapshot()
                res = r.dpkg("-i", self.deb, check=False)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(f"{old_dir} is not a root-owned directory", res.stderr)
                self.assertEqual(r.snapshot(), before)
                self.assertFalse(r.exists(NEW_LIGHTDM_IMAGE))
                self.assertEqual(r.divert("--list"), f"local diversion of {BG} to {OLD}")

    def test_preflight_refuses_new_state_without_old_state(self):
        r = self.r
        r.seed_legacy()
        shutil.rmtree(r.p(OLD_STATE))
        r.write(STATE + "/kali-login.divert", b"")  # left by a removed, not purged, tenfold-tweaks
        before = r.snapshot()
        res = r.dpkg("-i", self.deb, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f"{STATE} exists but {OLD_STATE} does not", res.stderr)
        self.assertEqual(r.snapshot(), before)
        self.assertEqual(r.divert("--list"), f"local diversion of {BG} to {OLD}")


@unittest.skipUnless(shutil.which("dash"), "needs dash")
class CleanupLegacyTest(unittest.TestCase):
    def test_failed_rescue_does_not_fail_configure(self):
        with tempfile.TemporaryDirectory(prefix="tt-cleanup-") as root:
            os.makedirs(root + STATE)
            with open(root + STATE + "/migrated-login", "wb") as f:
                f.write(MIGRATED_IMAGE)
            os.makedirs(os.path.dirname(root + RESCUE))
            open(root + RESCUE, "w").close()  # not a directory: tt_rescue dies
            common = os.path.join(ROOT, "packaging", "maint", "common.sh")
            res = subprocess.run(["dash", "-ec", '. "$1"; tt_cleanup_legacy; echo configured', "sh", common],
                                 env=dict(ENV, DPKG_ROOT=root), capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(res.stdout, "configured\n")
            self.assertIn("kept /var/lib/tenfold-tweaks/migrated-login", res.stderr)
            self.assertTrue(os.path.exists(root + STATE + "/migrated-login"))


@unittest.skipUnless(tool_ok() and shutil.which("dash"), "needs dash and unprivileged user namespaces")
class LightdmMigrateTest(unittest.TestCase):
    """S4 alone (dash, euid 0 in unshare -r): what the preflight saw may be swapped before the copy."""

    def migrate(self, root: str) -> subprocess.CompletedProcess:
        common = os.path.join(ROOT, "packaging", "maint", "common.sh")
        return subprocess.run(["unshare", "-r", "dash", "-ec", '. "$1"; tt_lightdm_migrate; echo migrated', "sh",
                               common], env=dict(ENV, DPKG_ROOT=root), capture_output=True, text=True)

    def check_refused(self, swap):
        with tempfile.TemporaryDirectory(prefix="tt-lightdm-") as root:
            conf = b"[greeter]\nbackground=" + OLD_LIGHTDM_IMAGE.encode() + b"\n"
            for path, data in ((STATE + "/lightdm-gtk-greeter.conf.orig", b"[greeter]\n"), (LIGHTDM_CONF, conf),
                               ("/etc/secret", b"secret"), (OLD_LIGHTDM_IMAGE, LIGHTDM_IMAGE)):
                os.makedirs(os.path.dirname(root + path), mode=0o755, exist_ok=True)
                with open(root + path, "wb") as f:
                    f.write(data)
            os.chmod(root + os.path.dirname(OLD_LIGHTDM_IMAGE), 0o755)
            swap(root)
            res = self.migrate(root)
            self.assertNotEqual(res.returncode, 0, res.stdout)
            self.assertNotIn("migrated", res.stdout)
            self.assertFalse(os.path.lexists(root + NEW_LIGHTDM_IMAGE))
            self.assertEqual(os.listdir(root + STATE + "/lightdm"), [])
            with open(root + LIGHTDM_CONF, "rb") as f:
                self.assertEqual(f.read(), conf)

    def test_symlinked_old_image_is_not_followed(self):
        def swap(root):
            os.unlink(root + OLD_LIGHTDM_IMAGE)
            os.symlink("../../../../../etc/secret", root + OLD_LIGHTDM_IMAGE)
        self.check_refused(swap)

    def test_symlinked_old_dir_is_not_followed(self):
        def swap(root):
            old_dir = os.path.dirname(OLD_LIGHTDM_IMAGE)
            os.rename(root + old_dir, root + old_dir + "-elsewhere")
            os.symlink(os.path.basename(old_dir) + "-elsewhere", root + old_dir)
        self.check_refused(swap)


@unittest.skipUnless(tool_ok() and shutil.which("chroot") and os.path.islink("/bin"),
                     "needs chroot and a merged-/usr host")
class EmptyDpkgRootTest(PackagingTestBase):
    """The normal install: dpkg without --root, so DPKG_ROOT is empty in every maintainer script.

    dpkg runs chrooted into the scratch root inside unshare -r -m: host /usr is bound read-only at
    /.host/usr (tools only), and the bind mounts die with the namespace, so no host path is written.
    """

    def dpkg(self, *args) -> subprocess.CompletedProcess:
        return self.r.chroot("dpkg", "--force-depends", "--log=/dpkg.log", *args)

    def test_install_remove_purge_with_legacy(self):
        r = self.r
        r.seed_legacy(migrated_login=True, lightdm=True)
        r.make_chroot()
        shutil.copy(self.deb, r.p("/tmp/pkg.deb"))
        res = self.dpkg("-i", "/tmp/pkg.deb")
        os.unlink(r.p("/tmp/pkg.deb"))
        self.assertEqual(r.status(), "install ok installed " + VERSION)
        MaintScriptTest.assert_migrated(self)
        saved = f"{RESCUE}/migrated-login-{sha12(MIGRATED_IMAGE)}.jpg"
        self.assertIn("saved login image: " + saved, res.stdout.splitlines())  # no root prefix: DPKG_ROOT was empty
        self.assertEqual(r.read(saved), MIGRATED_IMAGE)
        self.assertFalse(r.exists(STATE + "/migrated-login"))
        self.assertNotIn("tenfold-tweaks: warning", res.stderr)
        self.assertEqual(r.read(NEW_LIGHTDM_IMAGE), LIGHTDM_IMAGE)

        self.dpkg("-r", PKG)
        self.assertEqual(r.divert("--list"), "")
        self.assertEqual(os.readlink(r.p(BG)), STOCK_TARGET)
        self.assertEqual(r.read(LIGHTDM_CONF), b"[greeter]\nbackground=stock\n")
        self.dpkg("--purge", PKG)
        self.assertNotIn(PKG, r.status())
        MaintScriptTest.assert_no_traces(self)


if __name__ == "__main__":
    unittest.main()
