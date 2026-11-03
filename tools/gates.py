#!/usr/bin/env python3
"""The three gates that decide whether Blitz can replace Chromium for slides.

  A  determinism   N renders of the same input are byte-identical
  B  device scale  scale 2 produces exactly 2x the physical output of scale 1,
                   and agrees with Chromium at both scales
  C  element clip  every `section.slide` clipped to its own PNG matches the
                   dimensions Chromium produces for the same element

The deck is not part of this repo (client content): pass it with --deck.

Usage:
  gates.py --binary <render> --deck <deck.html> [--chromium <chrome>] [--runs 10]
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageChops

CSS_W, CSS_H = 1280, 720
SLIDE_SELECTOR = "section.slide"


# ---------------------------------------------------------------- utilities


def run(cmd, **kw):
    """Run a command, returning (rc, stdout, stderr, wall_seconds, unused_rss)."""
    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw
    )
    out, err = proc.communicate()
    wall = time.monotonic() - t0
    return proc.returncode, out, err, wall, 0.0


def peak_rss(cmd):
    """Peak RSS of a child in MiB, via /usr/bin/time if present."""
    timer = shutil.which("time") or "/usr/bin/time"
    if not os.path.exists("/usr/bin/time"):
        rc, out, err, wall, _ = run(cmd)
        return rc, out, err, wall, 0.0
    rc, out, err, wall, _ = run(["/usr/bin/time", "-f", "%M"] + cmd)
    mib = 0.0
    for line in reversed(err.strip().splitlines()):
        if line.strip().isdigit():
            mib = int(line.strip()) / 1024
            break
    return rc, out, err, wall, mib


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pixel_stats(a_path, b_path):
    """Mean abs diff and fraction of pixels off by more than 16, over white."""
    a = Image.open(a_path).convert("RGBA")
    b = Image.open(b_path).convert("RGBA")
    if a.size != b.size:
        return None
    white = Image.new("RGBA", a.size, (255, 255, 255, 255))
    a = Image.alpha_composite(white, a).convert("RGB")
    b = Image.alpha_composite(white, b).convert("RGB")
    diff = ImageChops.difference(a, b).convert("L")
    hist = diff.histogram()
    total = sum(hist)
    mean = sum(i * n for i, n in enumerate(hist)) / total if total else 0.0
    off = sum(hist[17:]) / total if total else 0.0
    return {"mean": round(mean, 2), "off_gt16_pct": round(100 * off, 2)}


def ink_pct(path):
    """Fraction of non-white pixels, as a coarse 'did it draw the document' probe."""
    im = Image.open(path).convert("RGBA")
    white = Image.new("RGBA", im.size, (255, 255, 255, 255))
    im = Image.alpha_composite(white, im).convert("L")
    hist = im.histogram()
    return round(100 * sum(hist[:250]) / sum(hist), 2)


# ------------------------------------------------------------- chromium ref


CHROMIUM_HTML = None


def chromium_render(chrome, html, out_png, w, h, scale, clip_selector=None, outdir=None):
    """Render with Chromium via Playwright. Returns a list of written paths."""
    from playwright.sync_api import sync_playwright

    written = []
    with sync_playwright() as p:
        b = p.chromium.launch(
            executable_path=chrome,
            args=["--force-device-scale-factor=%d" % scale, "--font-render-hinting=none"],
        )
        pg = b.new_page(viewport={"width": w, "height": h}, device_scale_factor=scale)
        pg.goto("file://" + os.path.abspath(html), wait_until="load")
        pg.wait_for_timeout(1500)
        if clip_selector:
            n = pg.locator(clip_selector).count()
            for i in range(n):
                el = pg.locator(clip_selector).nth(i)
                el.scroll_into_view_if_needed()
                pg.wait_for_timeout(120)
                f = os.path.join(outdir, "slide_%02d.png" % (i + 1))
                el.screenshot(path=f)
                written.append(f)
        else:
            pg.screenshot(path=out_png)
            written.append(out_png)
        b.close()
    return written


def image_regions(chrome, html, w, h, scale):
    """Union of <img> boxes, in physical px, as Chromium lays them out.

    Used as the mask for the appearance channel: photos and glyphs fail
    differently (Vello decodes images exactly, but rasterises glyphs with its
    own antialiasing), so scoring them together hides both signals.
    """
    from playwright.sync_api import sync_playwright

    boxes = []
    with sync_playwright() as p:
        b = p.chromium.launch(
            executable_path=chrome,
            args=["--force-device-scale-factor=%d" % scale, "--font-render-hinting=none"],
        )
        pg = b.new_page(viewport={"width": w, "height": h}, device_scale_factor=scale)
        pg.goto("file://" + os.path.abspath(html), wait_until="load")
        pg.wait_for_timeout(1500)
        raw = pg.evaluate("""() => Array.from(document.images).map(i => {
            const r = i.getBoundingClientRect();
            return [r.x + window.scrollX, r.y + window.scrollY, r.width, r.height];
        })""")
        b.close()
    for x, y, iw, ih in raw:
        boxes.append((int(x * scale), int(y * scale),
                      int(iw * scale), int(ih * scale)))
    return boxes

def split_stats(a_path, b_path, regions):
    """Pixel stats over two channels: inside the photo mask and outside it."""
    a = Image.open(a_path).convert("RGBA")
    b = Image.open(b_path).convert("RGBA")
    if a.size != b.size:
        return None
    white = Image.new("RGBA", a.size, (255, 255, 255, 255))
    a = Image.alpha_composite(white, a).convert("RGB")
    b = Image.alpha_composite(white, b).convert("RGB")
    diff = ImageChops.difference(a, b).convert("L")

    mask = Image.new("L", a.size, 0)
    from PIL import ImageDraw
    d = ImageDraw.Draw(mask)
    for x, y, w, h in regions:
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=255)

    out = {}
    for name, want in (("photo", 255), ("text", 0)):
        sel = Image.eval(mask, (lambda v: 255 - v) if want == 0 else (lambda v: v))
        hist = diff.histogram(sel)
        total = sum(hist)
        if not total:
            out[name] = None
            continue
        out[name] = {
            "mean": round(sum(i * n for i, n in enumerate(hist)) / total, 2),
            "off_gt16_pct": round(100 * sum(hist[17:]) / total, 2),
            "share_pct": round(100 * total / (a.size[0] * a.size[1]), 2),
        }
    return out

# ------------------------------------------------------------------- gates


def gate_a(binary, deck, runs, tmp, scale=2):
    print("\n== Gate A: determinism (scale %d) ==" % scale)
    digests, walls, rsss = [], [], []
    for i in range(runs):
        out = os.path.join(tmp, "det_%02d.png" % i)
        rc, _, err, wall, rss = peak_rss(
            [binary, deck, out, str(CSS_W), str(CSS_H), str(scale), "--document"]
        )
        if rc != 0:
            print("  render failed rc=%d\n%s" % (rc, err[-800:]))
            return {"ok": False, "reason": "render failed rc=%d" % rc}
        digests.append(md5(out))
        walls.append(wall)
        rsss.append(rss)
    uniq = sorted(set(digests))
    ok = len(uniq) == 1
    print("  runs=%d  distinct=%d" % (runs, len(uniq)))
    print("  md5     %s" % (digests[0] if ok else ", ".join(u[:12] for u in uniq)))
    print("  wall    median %.2fs  min %.2fs  max %.2fs" % (
        sorted(walls)[len(walls) // 2], min(walls), max(walls)))
    print("  rss     median %.1f MiB  max %.1f MiB" % (
        sorted(rsss)[len(rsss) // 2], max(rsss)))
    print("  VERDICT %s" % ("PASS: byte-identical" if ok else "FAIL: output varies"))
    return {"ok": ok, "distinct": len(uniq), "md5": digests[0],
            "wall_median_s": round(sorted(walls)[len(walls) // 2], 3),
            "rss_median_mib": round(sorted(rsss)[len(rsss) // 2], 1)}


def gate_b(binary, deck, chrome, tmp):
    print("\n== Gate B: device_scale_factor = 2 ==")
    results = {}
    for scale in (1, 2):
        blitz_png = os.path.join(tmp, "scale%d_blitz.png" % scale)
        chrome_png = os.path.join(tmp, "scale%d_chrome.png" % scale)
        rc, out, err, wall, rss = peak_rss(
            [binary, deck, blitz_png, str(CSS_W), str(CSS_H), str(scale), "--document"]
        )
        if rc != 0:
            print("  scale %d: blitz failed rc=%d\n%s" % (scale, rc, err[-800:]))
            return {"ok": False, "reason": "blitz rc=%d at scale %d" % (rc, scale)}
        blitz_size = Image.open(blitz_png).size
        chrome_paths = chromium_render(chrome, deck, chrome_png, CSS_W, CSS_H, scale)
        chrome_size = Image.open(chrome_paths[0]).size
        stats = pixel_stats(blitz_png, chrome_paths[0])
        results[scale] = {
            "blitz_size": blitz_size,
            "chrome_size": chrome_size,
            "size_match": blitz_size == chrome_size,
            "stats": stats,
            "ink_blitz": ink_pct(blitz_png),
            "ink_chrome": ink_pct(chrome_paths[0]),
            "wall_s": round(wall, 2),
            "rss_mib": round(rss, 1),
        }
        print("  scale %d: blitz %s  chrome %s  size_match=%s" % (
            scale, blitz_size, chrome_size, blitz_size == chrome_size))
        print("            ink blitz %.2f%% chrome %.2f%%   mean diff %s  off>16 %s%%" % (
            results[scale]["ink_blitz"], results[scale]["ink_chrome"],
            stats["mean"], stats["off_gt16_pct"]))
    # The 2x invariant: physical output must be exactly double.
    w1, h1 = results[1]["blitz_size"]
    w2, h2 = results[2]["blitz_size"]
    double = (w2, h2) == (w1 * 2, h1 * 2)
    print("  2x invariant: %dx%d -> %dx%d  %s" % (w1, h1, w2, h2,
                                                 "PASS" if double else "FAIL"))
    ok = double and results[1]["size_match"] and results[2]["size_match"]
    print("  VERDICT %s" % ("PASS" if ok else "FAIL"))
    return {"ok": ok, "double": double, "scales": results}


def gate_c(binary, deck, chrome, tmp):
    print("\n== Gate C: per-slide element clipping ==")
    blitz_dir = os.path.join(tmp, "clip_blitz")
    chrome_dir = os.path.join(tmp, "clip_chrome")
    os.makedirs(chrome_dir, exist_ok=True)

    rc, out, err, wall, rss = peak_rss(
        [binary, "--clip", deck, blitz_dir, str(CSS_W), str(CSS_H), "2",
         SLIDE_SELECTOR, "--document"]
    )
    if rc != 0:
        print("  blitz clip failed rc=%d\n%s" % (rc, err[-1500:]))
        return {"ok": False, "reason": "blitz clip rc=%d" % rc}
    boxes = [ln for ln in out.splitlines() if ln.startswith("clip_")]
    blitz_pngs = sorted(os.listdir(blitz_dir))

    chrome_pngs = chromium_render(
        chrome, deck, None, CSS_W, CSS_H, 2, clip_selector=SLIDE_SELECTOR,
        outdir=chrome_dir)

    print("  blitz: %d pngs   chrome: %d pngs" % (len(blitz_pngs), len(chrome_pngs)))
    for ln in boxes[:3]:
        print("    " + ln)
    if len(blitz_pngs) > 3:
        print("    ... (%d more)" % (len(blitz_pngs) - 3))

    # Each slide's CSS origin, parsed back out of the CLI's own clip report, so the
    # photo mask can be translated into slide-local coordinates.
    origins = []
    for ln in boxes:
        m = re.search(r"css x=([-\d.]+) y=([-\d.]+)", ln)
        origins.append((float(m.group(1)), float(m.group(2))) if m else None)

    regions = image_regions(chrome, deck, CSS_W, CSS_H, 2)
    print("  photo regions (Chromium <img> boxes, physical px): %d" % len(regions))

    rows, exact = [], 0
    for idx, (bname, cname) in enumerate(zip(blitz_pngs, sorted(os.listdir(chrome_dir)))):
        bp = os.path.join(blitz_dir, bname)
        cp = os.path.join(chrome_dir, cname)
        bs, cs = Image.open(bp).size, Image.open(cp).size
        st = pixel_stats(bp, cp) if bs == cs else None
        split = None
        if bs == cs and origins[idx]:
            ox, oy = origins[idx]
            local = [(ix - int(ox * 2), iy - int(oy * 2), iw, ih)
                     for ix, iy, iw, ih in regions]
            local = [(x, y, w, h) for x, y, w, h in local
                     if x < bs[0] and y < bs[1] and x + w > 0 and y + h > 0]
            split = split_stats(bp, cp, local) if local else None
        rows.append({"slide": bname, "blitz": bs, "chrome": cs,
                     "size_match": bs == cs, "stats": st, "split": split})
        if bs == cs:
            exact += 1
    for r in rows:
        extra = ""
        if r["split"]:
            ph, tx = r["split"]["photo"], r["split"]["text"]
            extra = "  photo %s/%s%%  text %s/%s%%" % (
                ph["mean"] if ph else "-", ph["off_gt16_pct"] if ph else "-",
                tx["mean"] if tx else "-", tx["off_gt16_pct"] if tx else "-")
        print("    %-12s blitz %-12s chrome %-12s %s%s%s" % (
            r["slide"], r["blitz"], r["chrome"],
            "size ok " if r["size_match"] else "SIZE MISMATCH ",
            ("mean %s off>16 %s%%" % (r["stats"]["mean"], r["stats"]["off_gt16_pct"]))
            if r["stats"] else "", extra))
    ok = len(rows) > 0 and exact == len(rows) and len(blitz_pngs) == len(chrome_pngs)
    print("  %d/%d slides at Chromium's dimensions" % (exact, len(rows)))
    print("  clip wall %.2fs  peak rss %.1f MiB" % (wall, rss))
    print("  VERDICT %s" % ("PASS" if ok else "FAIL"))
    return {"ok": ok, "slides": rows, "wall_s": round(wall, 2),
            "rss_mib": round(rss, 1), "boxes": boxes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True, help="Blitz render binary")
    ap.add_argument("--deck", required=True, help="real deck HTML")
    ap.add_argument("--chromium", default=None, help="Chromium executable")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--json", default=None, help="write results here")
    ap.add_argument("--only", default="abc", help="subset of gates, e.g. 'a'")
    ap.add_argument("--scale", type=int, default=2, help="gate A scale (pipeline ships 2)")
    args = ap.parse_args()

    chrome = args.chromium
    if not chrome:
        import glob
        cands = sorted(glob.glob(os.path.expanduser(
            "~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome")))
        if not cands:
            print("no chromium found; pass --chromium", file=sys.stderr)
            return 2
        chrome = cands[-1]
    print("binary   %s" % args.binary)
    print("deck     %s" % args.deck)
    print("chromium %s" % chrome)

    tmp = tempfile.mkdtemp(prefix="blitz-gates-")
    out = {}
    try:
        if "a" in args.only:
            out["A_determinism"] = gate_a(args.binary, args.deck, args.runs, tmp, args.scale)
        if "b" in args.only:
            out["B_device_scale"] = gate_b(args.binary, args.deck, chrome, tmp)
        if "c" in args.only:
            out["C_element_clip"] = gate_c(args.binary, args.deck, chrome, tmp)
    finally:
        if args.json:
            with open(args.json, "w") as f:
                json.dump(out, f, indent=2)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n== SUMMARY ==")
    for k, v in out.items():
        print("  %-16s %s" % (k, "PASS" if v.get("ok") else "FAIL"))
    return 0 if all(v.get("ok") for v in out.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
