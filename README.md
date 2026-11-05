# blitz-render

**HTML + CSS → PNG, with no browser.**

A small Rust CLI that renders real documents — stylesheets, flexbox, grid, images,
web fonts — using [Blitz](https://github.com/DioxusLabs/blitz)'s Stylo + Taffy +
Vello pipeline. No Chromium, no JavaScript engine, one process.

It exists to answer a concrete question: can a 25 MB binary replace a 500 MB
browser for server-side rendering? On a real 12-slide, 4 MB deck, yes:

| 12-slide deck, 2560×1440 | wall | peak RSS |
|---|---|---|
| headless Chromium (Playwright) | 13.36 s | 858 MiB |
| **blitz-render** | **5.56 s** | **95 MiB** |

**2.4× faster, 9× lighter** — and byte-for-byte deterministic, which a browser is
not: ten renders of the same input produce one md5, so output can be diffed in CI.

## Install

Linux x86_64, glibc ≥ 2.35. From a [release](https://github.com/leshchenko1979/blitz-render/releases):

```bash
V=v0.3.0-beta.4-r1
N=blitz-render_0.3.0-beta.4-r1_linux_amd64
BASE="https://github.com/leshchenko1979/blitz-render/releases/download/$V"

curl -fsSL -O "$BASE/$N.tar.gz"
curl -fsSL -O "$BASE/checksums.txt"
sha256sum -c checksums.txt
tar xzf "$N.tar.gz"
install -m755 blitz-render /usr/local/bin/blitz-render
```

Every release verifies its own asset: the workflow downloads the published
tarball back over HTTPS, checks `checksums.txt`, byte-compares it with the build,
renders a PNG and asserts its dimensions. A release serving the wrong file fails
its own run.

## Usage

Two modes. Both take the **viewport in CSS pixels**; `scale` multiplies the
output resolution (scale 2 → a 1280×720 viewport produces 2560×1440).

```bash
# one image
blitz-render page.html out.png 1280 720 [scale] [--document]

# one PNG per element matching a selector
blitz-render --clip deck.html png/ 1280 720 2 'section.slide' [--document] [--scroll]
```

`--document` renders a complete HTML file as-is. **Use it for real documents**:
without it the input is wrapped in a synthetic `<html><body>`, which is right for
fragments and wrong for anything with its own `<head>`.

`--scroll` (clip mode) paints one element-sized canvas per match instead of one
full-height canvas that is cropped afterwards — the same pixels for a third of
the peak memory.

```bash
# a 12-slide deck, 2560×1440 per slide, ~95 MiB peak
blitz-render --clip deck.html slides/ 1280 720 2 'section.slide' --document --scroll
```

## Fidelity

Measured against Chromium 151 on the same document, same viewport, same scale —
both PNGs read by one instrument, so a difference in the table is a difference
between two engines rather than between two rulers.

| Deck, scale 2, per slide vs `element.screenshot()` | |
|---|---|
| size | 12/12 at 2560×1440 |
| pixels off by >16 | **4.13 %** (per slide 1.41 – 8.29 %) |
| photo regions | 0.31 – 3.92 % |
| best integer shift on every photo region | **0 px** |

The residual is antialiasing, not layout: wrap points match, tables match, boxes
sit at Chromium's coordinates, and images are sampled at zero offset. Vello's
glyph AA is slightly softer than Skia's, so **you cannot diff blitz-render
against Chromium at 99 %** — and you don't need to, because against *itself* it
is exact.

Determinism is a first-class property, not a side effect:

| | |
|---|---|
| 10 renders, scale 2, one md5 | `1dffe1ae…` |
| two separate builds of one commit | different binary md5s, **identical pixels** (12/12 slides) |

## What it does

Cascade and selectors, flexbox, grid, inline formatting and line boxes,
`@font-face` (including data URIs), images over `http(s)://`, `file://` and
`data:`, backgrounds, gradients, borders, transforms, absolute and relative
positioning, CSS variables, `box-sizing`, `min`/`max` sizing, auto margins,
`aspect-ratio`, `overflow: clip`.

## What it doesn't

- **JavaScript.** No script engine is compiled in. That is the point: no
  script-execution surface, and no 200 MB of V8.
- **PDF.** PNG only.
- **Everything CSS.** It is Blitz's CSS support, not Chromium's — expect gaps in
  the long tail (print layout, exotic selectors, `@page`).

## Building

The build is a GitHub Actions job, because the host this was written on forbids
local Rust compilation. Two workflows:

| Workflow | Trigger | Output |
|---|---|---|
| `parity.yml` | push to any branch | builds, measures the corpus, uploads results |
| `release.yml` | push a `v*` tag | builds, packages, publishes, **then verifies the published asset** |

Two hard requirements, both learned the hard way:

- **`runs-on: ubuntu-22.04`, never `ubuntu-latest`.** The `latest` image produced
  a `GLIBC_2.43` binary that would not start on a glibc 2.39 host. The runner is
  pinned, and a guard fails the build if any `GLIBC_*` symbol exceeds 2.35 —
  otherwise a future image bump silently raises the floor again.
- **`libfontconfig1-dev`** is needed to build Stylo.

Locally, where Rust is allowed:

```bash
cargo build --release
python3 tools/run_parity.py --blitz target/release/render
```

## Corpus

`fixtures/` holds 31 documents that each paint elements in exact probe colours, so
geometry can be read out of a PNG without the instrument knowing anything about
the markup. `tools/run_parity.py` renders every fixture through Blitz and through
Chromium and reports per-box deltas:

```
VERDICT  text-free (47 boxes): 42 exact, 0 within 1px, 4 divergent, 0 missing, 1 absence
```

The 4 divergent boxes are one open defect: consecutive `<br>` runs in a
`font-size:0` inline-block context, where the deltas grow 16 px per extra break.
Six further boxes involve text and are reported separately, because without an
identical embedded font they measure font availability rather than layout.

## Licence

Not yet chosen. Until one is added, default copyright applies and the code is not
free to reuse — which defeats the purpose of publishing it.
