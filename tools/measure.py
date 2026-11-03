"""Locate probe colours in a PNG and report their bounding boxes.

This is the same instrument as `test/chromium/measure.go` in the ogre repo,
reimplemented dependency-free so the spike needs nothing but python3:

  * pixels are composited over white first (a transparent pixel is not black),
  * a pixel matches a probe colour when every channel is within `TOLERANCE`,
  * the box is half-open on the right and bottom edges.

Both engines are measured with this one ruler, so an instrument error cancels
instead of masquerading as a renderer difference.
"""

import struct
import sys
import zlib

# Probe colours, at least 64 apart in one channel each so an antialiased edge of
# one can never be mistaken for another.
PALETTE = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "magenta": (255, 0, 255),
    "cyan": (0, 255, 255),
    "orange": (255, 128, 0),
    "black": (0, 0, 0),
}

TOLERANCE = 8


def read_png(path):
    """Decode a non-interlaced 8-bit PNG into (width, height, channels, pixels)."""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG")

    pos = 8
    idat = bytearray()
    width = height = channels = None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if depth != 8:
                raise ValueError(f"{path}: unsupported bit depth {depth}")
            if interlace:
                raise ValueError(f"{path}: interlaced PNGs are not supported")
            channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
            if channels is None:
                raise ValueError(f"{path}: unsupported colour type {color_type}")
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break

    if width is None:
        raise ValueError(f"{path}: no IHDR")

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        filt = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if filt == 1:  # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filt == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:  # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filt == 4:  # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif filt != 0:
            raise ValueError(f"{path}: unknown filter {filt} on row {y}")
        out[y * stride : (y + 1) * stride] = line
        prev = line

    return width, height, channels, out


def measure(path):
    """Return {colour: (x, y, w, h)} for every probe colour present in the PNG."""
    width, height, channels, pixels = read_png(path)
    boxes = {}
    stride = width * channels

    for name, (pr, pg, pb) in PALETTE.items():
        min_x, min_y, max_x, max_y = width, height, -1, -1
        for y in range(height):
            row = y * stride
            for x in range(width):
                i = row + x * channels
                if channels >= 3:
                    r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
                    a = pixels[i + 3] if channels == 4 else 255
                else:  # grayscale
                    r = g = b = pixels[i]
                    a = pixels[i + 1] if channels == 2 else 255
                if a != 255:
                    # Composite over white: premultiplied transparency must not
                    # read as a dark pixel.
                    r = (r * a + 255 * (255 - a)) // 255
                    g = (g * a + 255 * (255 - a)) // 255
                    b = (b * a + 255 * (255 - a)) // 255
                if (
                    abs(r - pr) <= TOLERANCE
                    and abs(g - pg) <= TOLERANCE
                    and abs(b - pb) <= TOLERANCE
                ):
                    if x < min_x:
                        min_x = x
                    if x > max_x:
                        max_x = x
                    if y < min_y:
                        min_y = y
                    if y > max_y:
                        max_y = y
        if max_x >= 0:
            boxes[name] = (min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)
    return boxes


def delta(a, b):
    return max(abs(a[i] - b[i]) for i in range(4))


if __name__ == "__main__":
    for png in sys.argv[1:]:
        found = measure(png)
        print(png)
        for name in sorted(found):
            x, y, w, h = found[name]
            print(f"  {name:8s} x={x} y={y} w={w} h={h}")
