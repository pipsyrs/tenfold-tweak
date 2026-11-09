import hashlib
import os
import shutil
import unittest

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

from tests import SANDBOX  # noqa: E402
from tests.helpers import make_image, png_header_only, scratch_dir, write  # noqa: E402

from tenfold_tweak import image  # noqa: E402
from tenfold_tweak.image import ImageError  # noqa: E402


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(image.COPY_DIR.startswith(SANDBOX))
        shutil.rmtree(image.COPY_DIR, ignore_errors=True)
        self.d = scratch_dir("img-")

    def assertRejected(self, path, fragment):
        with self.assertRaises(ImageError) as cm:
            image.prepare(path)
        self.assertIn(fragment, str(cm.exception))
        leftovers = os.listdir(image.COPY_DIR) if os.path.isdir(image.COPY_DIR) else []
        self.assertEqual(leftovers, [], "rejected image must leave no copy behind")

    def test_png_accepted_into_stable_copy(self):
        src = make_image(os.path.join(self.d, "a.png"))
        copy = image.prepare(src)
        with open(src, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:12]
        self.assertEqual(copy, os.path.join(image.COPY_DIR, f"wallpaper-{digest}.png"))
        self.assertEqual(os.stat(copy).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(image.COPY_DIR).st_mode & 0o777, 0o700)

    def test_jpeg_accepted(self):
        src = make_image(os.path.join(self.d, "a.jpeg"), fmt="jpeg")
        self.assertTrue(image.prepare(src).endswith(".jpg"))

    def test_copy_survives_source_deletion(self):
        src = make_image(os.path.join(self.d, "tmp.png"))
        copy = image.prepare(src)
        os.unlink(src)
        self.assertTrue(os.path.isfile(copy))
        self.assertFalse(copy.startswith(self.d))

    def test_missing_file(self):
        self.assertRejected(os.path.join(self.d, "nope.png"), "file not found")

    def test_directory(self):
        self.assertRejected(self.d, "not a regular file")

    def test_empty_file(self):
        self.assertRejected(write(os.path.join(self.d, "e.png"), b""), "between 1 byte and 64 MiB")

    def test_text_file_lists_supported_formats(self):
        self.assertRejected(write(os.path.join(self.d, "t.png"), b"hello"), "supported: PNG, JPEG")

    def test_bmp_rejected_with_supported_formats(self):
        src = make_image(os.path.join(self.d, "a.bmp"), fmt="bmp")
        self.assertRejected(src, "supported: PNG, JPEG")

    def test_corrupt_png(self):
        good = make_image(os.path.join(self.d, "g.png"), 64, 64)
        with open(good, "rb") as f:
            data = f.read()
        bad = write(os.path.join(self.d, "bad.png"), data[:60] + b"\x00" * 40)
        self.assertRejected(bad, "corrupt image")

    def test_png_header_without_data_is_corrupt(self):
        # Valid PNG magic + IHDR, no data: must say corrupt, not "unsupported format".
        self.assertRejected(write(os.path.join(self.d, "h.png"), png_header_only(10, 10)), "corrupt")

    def test_dimensions_over_limit(self):
        self.assertRejected(make_image(os.path.join(self.d, "big.png"), image.MAX_DIM + 1, 2), "dimensions")

    def test_file_over_64mib(self):
        p = os.path.join(self.d, "huge.png")
        with open(p, "wb") as f:
            f.truncate(image.MAX_SIZE + 1)
        self.assertRejected(p, "64 MiB")

    @unittest.skipIf(os.geteuid() == 0, "root can read 000 files")
    def test_unreadable_file(self):
        p = make_image(os.path.join(self.d, "r.png"))
        os.chmod(p, 0)
        self.addCleanup(os.chmod, p, 0o644)
        self.assertRejected(p, "cannot read")


class LoginCopyTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(image.COPY_DIR, ignore_errors=True)
        self.d = scratch_dir("login-")

    def test_small_image_passed_through(self):
        copy = image.prepare(make_image(os.path.join(self.d, "s.png"), 320, 200))
        self.assertEqual(image.login_copy(copy), copy)

    def test_exactly_4096_passed_through(self):
        copy = image.prepare(make_image(os.path.join(self.d, "e.png"), 4096, 8))
        self.assertEqual(image.login_copy(copy), copy)

    def test_wider_than_4096_downscaled(self):
        copy = image.prepare(make_image(os.path.join(self.d, "w.png"), 5000, 10))
        small = image.login_copy(copy)
        self.assertNotEqual(small, copy)
        self.assertTrue(os.path.basename(small).startswith("login-"))
        _, w, h = GdkPixbuf.Pixbuf.get_file_info(small)
        self.assertLessEqual(max(w, h), 4096)
        self.assertTrue(os.path.isfile(copy), "original copy is kept")

    def test_cleanup_removes_unreferenced_copies_only(self):
        a = image.prepare(make_image(os.path.join(self.d, "a.png"), rgba=0xFF0000FF))
        b = image.prepare(make_image(os.path.join(self.d, "b.png"), rgba=0x00FF00FF))
        foreign = write(os.path.join(image.COPY_DIR, "user-file.png"), b"x")
        image.cleanup_copies({a})
        self.assertTrue(os.path.exists(a))
        self.assertFalse(os.path.exists(b))
        self.assertTrue(os.path.exists(foreign))


if __name__ == "__main__":
    unittest.main()
