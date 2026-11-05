"""Isolate which part of the drain loop actually makes images arrive.

The renderer's drain loop has three shapes (see `BLITZ_DRAIN` in src/main.rs):

  naive - exactly the loop in Blitz's own examples/screenshot.rs:
          `loop { document.resolve(0.0); if net.is_empty() { break } }`
  yield - the same, but yielding to the runtime between iterations
  quiet - four consecutive empty polls, each preceded by a yield and a 2 ms sleep

Every mode is run repeatedly on the same fixture, because a timing bug is a
distribution, not a single observation: one lucky run proves nothing.

The fixture paints a data-URI PNG at a known position, so "did the image arrive"
is a measurement of where the blue box is, not an eyeball judgement.

Usage: python3 tools/drain_probe.py --bin ./render [--runs 8]
"""

import argparse
import collections
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from measure import measure  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The image fixture: green 40x40 marker at 0,0 (proves the page rendered at all)
# and a blue 60x30 data-URI PNG at 20,10 (proves the sub-resource arrived).
FIXTURE = os.path.join(HERE, "fixtures", "image-data-uri.html")
EXPECT_BLUE = (20, 10, 60, 30)
EXPECT_GREEN = (0, 0, 40, 40)

# (drain loop shape, tokio runtime). Upstream's examples/screenshot.rs is
# `#[tokio::main]`, i.e. multi-thread; the shipped CLI is current-thread.
MODES = [
    ("naive", "current"),
    ("naive", "multi"),
    ("yield", "current"),
    ("quiet", "current"),
]

# Keep a starved naive loop cheap: it spins until this deadline.
DEADLINE_SECS = "15"


def render(binary, mode, runtime, out_png, trace=False):
    env = dict(os.environ, BLITZ_DRAIN=mode, BLITZ_RUNTIME=runtime,
               BLITZ_DEADLINE_SECS=DEADLINE_SECS)
    if trace:
        env["BLITZ_TRACE"] = "1"
    proc = subprocess.run(
        [binary, FIXTURE, out_png, "200", "120", "1", "--document"],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return proc


def run_mode(binary, mode, runtime, runs):
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(runs):
            out = os.path.join(tmp, f"{mode}-{i}.png")
            proc = render(binary, mode, runtime, out, trace=(i == 0))
            if proc.returncode != 0 or not os.path.exists(out):
                results.append({"run": i, "error": proc.stderr.strip()[-400:]})
                continue
            boxes = measure(out)
            results.append(
                {
                    "run": i,
                    "blue": boxes.get("blue"),
                    "green": boxes.get("green"),
                    "trace": (proc.stderr.strip().splitlines() if i == 0 else None),
                }
            )
    return results


def summarise(label, results):
    ok = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]
    image_ok = [r for r in ok if r["blue"] == EXPECT_BLUE]
    page_ok = [r for r in ok if r["green"] == EXPECT_GREEN]
    missing = [r for r in ok if r["blue"] is None]

    shapes = collections.Counter(
        "missing" if r["blue"] is None else str(r["blue"]) for r in ok
    )
    return {
        "mode": label,
        "runs": len(results),
        "errors": len(errors),
        "image_ok": len(image_ok),
        "page_ok": len(page_ok),
        "image_missing": len(missing),
        "blue_shapes": dict(shapes),
        "first_error": errors[0]["error"] if errors else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True)
    ap.add_argument("--runs", type=int, default=8)
    ap.add_argument("--out", default="out/drain_probe.json")
    ap.add_argument("--show-trace", action="store_true",
                    help="print the resolve-pass trace for the first run of each mode")
    args = ap.parse_args()

    binary = os.path.abspath(args.bin)
    if not os.access(binary, os.X_OK):
        sys.exit(f"{binary}: not executable")

    report = []
    for mode, runtime in MODES:
        label = f"{mode}/{runtime}"
        results = run_mode(binary, mode, runtime, args.runs)
        summary = summarise(label, results)
        report.append({"summary": summary, "runs": results})
        s = summary
        print(
            f"{label:15s}  image {s['image_ok']}/{s['runs']}  "
            f"page {s['page_ok']}/{s['runs']}  "
            f"missing {s['image_missing']}  errors {s['errors']}  "
            f"shapes {s['blue_shapes']}",
            flush=True,
        )
        if s["first_error"]:
            print(f"        first error: {s['first_error'][:200]}", flush=True)
        if args.show_trace:
            for r in results:
                if r.get("trace"):
                    print(f"        trace ({label} run 0):", flush=True)
                    for line in r["trace"]:
                        print(f"          {line}", flush=True)
                    break

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"expect_blue": EXPECT_BLUE, "expect_green": EXPECT_GREEN,
                   "runs_per_mode": args.runs, "modes": report}, f, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
