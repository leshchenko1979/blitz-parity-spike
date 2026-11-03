"""Render a real document with every engine and compare what was painted.

The probe-colour corpus measures geometry on synthetic fixtures. This does the
opposite: one real 4 MB deck, no probes, and the question is blunt — did the
engine paint the page, and how much of it?

Three numbers per engine, all against the browser's own screenshot:

  ink        fraction of non-white pixels, as a percentage of Chromium's
  mean diff  mean per-channel absolute difference, 0-255 (lower is closer)
  exact px   fraction of pixels matching the browser within 8/255

An engine that drops the stylesheet scores near 0 % ink and a huge mean diff,
which is exactly what a "renders but ignores the cascade" failure looks like.

Usage: python3 tools/deck_check.py --html deck.html --chrome <path> [--blitz ...] [--ogre ...]
"""

import argparse
import os
import statistics
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import measure  # noqa: E402
from run_parity import CHROME_FLAGS, find_chrome, timed  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "deck")
WIDTH, HEIGHT = 1280, 720
NEAR = 8  # per-channel distance still counted as a match


def pixels(path):
    """Decode a PNG into a flat list of (r,g,b) composited over white."""
    w, h, channels, buf = measure.read_png(path)
    out = []
    stride = w * channels
    for y in range(h):
        row = y * stride
        for x in range(w):
            i = row + x * channels
            if channels >= 3:
                r, g, b = buf[i], buf[i + 1], buf[i + 2]
                a = buf[i + 3] if channels == 4 else 255
            else:
                r = g = b = buf[i]
                a = buf[i + 1] if channels == 2 else 255
            if a != 255:
                r = (r * a + 255 * (255 - a)) // 255
                g = (g * a + 255 * (255 - a)) // 255
                b = (b * a + 255 * (255 - a)) // 255
            out.append((r, g, b))
    return w, h, out


def is_ink(px):
    return px[0] < 250 or px[1] < 250 or px[2] < 250


def compare(reference, candidate):
    rw, rh, ref = reference
    cw, ch, cand = candidate
    if (rw, rh) != (cw, ch):
        raise SystemExit(f"size mismatch: reference {rw}x{rh}, candidate {cw}x{ch}")
    ink_ref = sum(1 for p in ref if is_ink(p))
    ink_cand = sum(1 for p in cand if is_ink(p))
    total_diff = 0
    near = 0
    for a, b in zip(ref, cand):
        d = max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))
        total_diff += d
        if d <= NEAR:
            near += 1
    n = len(ref)
    return {
        "ink_ref_pct": 100.0 * ink_ref / n,
        "ink_cand_pct": 100.0 * ink_cand / n,
        "ink_ratio_pct": (100.0 * ink_cand / ink_ref) if ink_ref else 0.0,
        "mean_diff": total_diff / n,
        "near_pct": 100.0 * near / n,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True)
    ap.add_argument("--chrome", default=None)
    ap.add_argument("--blitz", default=os.path.join(ROOT, "target", "release", "render"))
    ap.add_argument("--ogre", default=None)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    chrome = find_chrome(args.chrome)
    os.makedirs(OUT, exist_ok=True)
    tmp = tempfile.mkdtemp()
    html = os.path.abspath(args.html)

    commands = {
        "chromium": [chrome] + CHROME_FLAGS
        + [f"--window-size={WIDTH},{HEIGHT}", f"--screenshot={os.path.join(OUT, 'chromium.png')}",
           f"file://{html}"],
    }
    if os.path.isfile(args.blitz):
        commands["blitz"] = [args.blitz, html, os.path.join(OUT, "blitz.png"),
                             str(WIDTH), str(HEIGHT)]
    if args.ogre:
        commands["ogre"] = [args.ogre, "--render", html, "--format", "png",
                            "--output", os.path.join(OUT, "ogre.png"),
                            "--width", str(WIDTH), "--height", str(HEIGHT)]

    print(f"document: {html} ({os.path.getsize(html)} bytes) at {WIDTH}x{HEIGHT}")
    print()

    for name, cmd in commands.items():
        walls, rss = [], []
        for _ in range(args.repeats):
            try:
                wall, rss_kib = timed(cmd)
            except SystemExit as exc:
                print(f"{name}: FAILED to render ({exc})")
                break
            walls.append(wall)
            rss.append(rss_kib / 1024.0)
        else:
            print(f"{name}: {statistics.median(walls):.3f}s  {statistics.median(rss):.1f} MiB")
            continue

    print()
    ref_path = os.path.join(OUT, "chromium.png")
    if not os.path.exists(ref_path):
        raise SystemExit("chromium produced no screenshot")
    reference = pixels(ref_path)

    header = f"{'engine':10s} {'png bytes':>10s} {'ink %':>7s} {'of browser':>11s} {'mean diff':>10s} {'near px %':>10s}"
    print(header)
    print("-" * len(header))
    rows = []
    for name in commands:
        path = os.path.join(OUT, f"{name}.png")
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            print(f"{name:10s} {'-':>10s} {'-':>7s} {'NO OUTPUT':>11s}")
            continue
        stats = compare(reference, pixels(path))
        rows.append((name, stats))
        print(
            f"{name:10s} {os.path.getsize(path):>10d} {stats['ink_cand_pct']:>6.2f}% "
            f"{stats['ink_ratio_pct']:>10.1f}% {stats['mean_diff']:>10.1f} {stats['near_pct']:>9.1f}%"
        )


if __name__ == "__main__":
    main()
