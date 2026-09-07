"""The taskbar icon, drawn rather than shipped.

A pinned shortcut inherits the icon of whatever it points at, and pointing at
`pythonw.exe` puts a Python logo on the taskbar. This draws Ranger's own: the
orb, on the same #0E0F13 the interface uses.

Written out with zlib and struct because the alternative is a binary blob in
the repository that nobody can review or change, or a dependency on Pillow for
one image. Neither is worth it for ninety lines that produce the same result
every time, which a test can assert byte for byte.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

BG = (0x0E, 0x0F, 0x13)
ACCENT = (0x2D, 0xD4, 0xA8)
CORE = (0xB9, 0xFF, 0xEC)

#: Windows picks the nearest size and scales. Giving it the ones it actually
#: asks for means the taskbar and the alt-tab list are both drawn, not guessed.
SIZES = (16, 32, 48, 64, 128, 256)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    """Encode RGBA rows as a PNG. No filtering, which is fine at this size."""
    height = len(pixels)
    width = len(pixels[0])
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type: none
        for red, green, blue, alpha in row:
            raw += bytes((red, green, blue, alpha))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def _blend(under, over, amount: float) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return (
        round(under[0] + (over[0] - under[0]) * amount),
        round(under[1] + (over[1] - under[1]) * amount),
        round(under[2] + (over[2] - under[2]) * amount),
    )


def draw(size: int) -> list[list[tuple[int, int, int, int]]]:
    """The orb: a dark rounded ground, a teal halo, a bright core.

    The same three layers as the real one, without the shader. At 16 pixels
    only the core and a suggestion of halo survive, which is the right thing to
    survive.
    """
    rows: list[list[tuple[int, int, int, int]]] = []
    centre = (size - 1) / 2
    radius = size / 2
    corner = size * 0.22

    for y in range(size):
        row: list[tuple[int, int, int, int]] = []
        for x in range(size):
            dx, dy = x - centre, y - centre
            distance = (dx * dx + dy * dy) ** 0.5

            # Rounded square ground, antialiased at the corners.
            inset_x = max(abs(dx) - (radius - corner), 0.0)
            inset_y = max(abs(dy) - (radius - corner), 0.0)
            corner_distance = (inset_x * inset_x + inset_y * inset_y) ** 0.5
            edge = max(abs(dx), abs(dy))
            inside = 1.0
            if edge > radius - 1:
                inside = max(0.0, radius - edge)
            if corner_distance > corner - 1:
                inside = min(inside, max(0.0, corner - corner_distance))

            if inside <= 0:
                row.append((0, 0, 0, 0))
                continue

            colour = BG
            # Three falloffs, tuned so the thing still reads at sixteen pixels.
            # A soft wide glow looks right at 256 and disappears at 16, so the
            # core is deliberately small, hard and bright.
            atmosphere = max(0.0, 1.0 - distance / (radius * 0.90)) ** 3.4
            colour = _blend(colour, ACCENT, atmosphere * 0.45)
            halo = max(0.0, 1.0 - distance / (radius * 0.52)) ** 1.8
            colour = _blend(colour, ACCENT, halo)
            core = max(0.0, 1.0 - distance / (radius * 0.26)) ** 0.9
            colour = _blend(colour, CORE, core)

            row.append((*colour, round(255 * min(1.0, inside))))
        rows.append(row)
    return rows


def ico(sizes: tuple[int, ...] = SIZES) -> bytes:
    """An ICO holding a PNG per size. Windows Vista and later read PNG in ICO."""
    images = [png(draw(size)) for size in sizes]

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = bytearray()
    for size, image in zip(sizes, images):
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,  # 0 means 256 in an ICO directory
            0 if size >= 256 else size,
            0,  # no palette
            0,  # reserved
            1,  # colour planes
            32,  # bits per pixel
            len(image),
            offset,
        )
        offset += len(image)

    return bytes(header) + bytes(entries) + b"".join(images)


def write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ico())
    return path
