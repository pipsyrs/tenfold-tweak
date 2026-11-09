"""Shared fixtures: image factories and sandbox dirs."""

import os
import struct
import tempfile
import zlib

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

from tests import SANDBOX  # noqa: E402


def scratch_dir(prefix="case-") -> str:
    return tempfile.mkdtemp(prefix=prefix, dir=SANDBOX)


def make_image(path: str, width=32, height=24, fmt="png", rgba=0x3366CCFF) -> str:
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
    pb.fill(rgba)
    pb.savev(path, fmt, [], [])
    return path


def png_header_only(width: int, height: int) -> bytes:
    """PNG signature + a valid IHDR chunk (no image data)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk + struct.pack(">I", zlib.crc32(chunk))


def jpeg_header_only(width: int, height: int) -> bytes:
    """SOI + APP0 + SOF0 header carrying the given size (no scan data)."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def write(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path
