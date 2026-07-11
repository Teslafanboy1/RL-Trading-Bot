"""gen_icons.py — generate the PWA icons with zero dependencies.

Draws a dark tile with the 4-line EMA "ribbon fan" motif (blue/green/yellow/red,
red lowest = BUY) and writes valid PNGs via zlib. Run once:

    python3 dashboard/gen_icons.py
"""

import os
import zlib
import struct

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "icons")

BG = (13, 17, 25, 255)
LINES = [  # (color, amplitude, phase, thickness) — fan of 4 EMAs
    ((88, 166, 255, 255), 0.10, 0.00, 0.018),   # blue (8)
    ((45, 212, 160, 255), 0.16, 0.06, 0.018),   # green (13)
    ((255, 180, 84, 255), 0.22, 0.12, 0.018),   # yellow (21)
    ((255, 93, 108, 255), 0.30, 0.20, 0.022),   # red (55) — lowest
]


def _px_curve(x, size, amp, phase):
    """y (0..1) of a rising curve with a dip — looks like a momentum leg."""
    t = x / size
    base = 0.72 - 0.42 * t                      # rising left->right (y down = up)
    wob = amp * 0.35 * (1 if t < 0.5 else 1.0) * (0.5 - abs(((t * 2 + phase) % 1) - 0.5))
    return base + amp * 0.5 - wob - amp * t * 0.9


def make_icon(size):
    buf = bytearray()
    rows = []
    corner = size * 0.22
    for y in range(size):
        row = bytearray()
        for x in range(size):
            # rounded-corner mask
            dx = max(corner - x, x - (size - 1 - corner), 0)
            dy = max(corner - y, y - (size - 1 - corner), 0)
            if dx * dx + dy * dy > corner * corner:
                row += bytes((0, 0, 0, 0))
                continue
            r, g, b, a = BG
            # subtle vertical gradient
            g2 = int(g + (y / size) * 6)
            px = [r, g2, b, a]
            fx = x / size
            if 0.12 < fx < 0.92:
                for color, amp, phase, thick in LINES:
                    cy = _px_curve(x - size * 0.12, size * 0.8, amp, phase) * size
                    if abs(y - cy) <= max(1.2, thick * size):
                        px = list(color)
                        break
            row += bytes(px)
        rows.append(bytes(row))
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    os.makedirs(HERE, exist_ok=True)
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512),
                       ("apple-touch-icon.png", 180)):
        with open(os.path.join(HERE, name), "wb") as f:
            f.write(make_icon(size))
        print(f"  wrote icons/{name}")


if __name__ == "__main__":
    main()
