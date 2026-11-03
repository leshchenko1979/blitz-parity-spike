# Blitz vs Chromium on the ogre parity corpus

A measurement spike, not a product. It answers one question with numbers: **how
close is [Blitz](https://github.com/DioxusLabs/blitz) to a real browser on the
geometry our own corpus already pins?**

Blitz is the nearest existing thing to "Stylo cascade + Taffy layout + a paint
backend, no JavaScript". Before treating it as a foundation, its geometry has to
be measured the same way ogre's was: against Chromium, with one ruler.

## What is measured

29 fixtures from the ogre Chromium-parity corpus (`macawls/ogre`,
`test/chromium/fixtures`). Each fixture paints elements with exact probe colours
(`#ff0000`, `#00ff00`, …) so the geometry can be read out of a PNG without the
instrument knowing anything about the document.

Both engines render the same document at the same viewport, and **both PNGs are
measured by the same instrument** (`tools/measure.py`): every probe colour is
located and its bounding box reported. A difference in the table is therefore a
difference between two engines, not between two rulers.

Four KPI numbers come out of the run: exact agreement, wall time, peak RSS, and
the divergent/missing counts.

## What is *not* measured

- **Text fixtures are confounded by fonts.** Six of the 46 probe boxes belong to
  fixtures that render glyphs. The ogre harness embeds a data-URI `@font-face` so
  both engines get identical bytes; this spike does not, so a text row measures
  font availability as much as layout. They are reported separately and are
  **not** part of the verdict.
- **JavaScript.** Blitz ships a script engine behind a flag; it is not enabled
  here, and a JS-free renderer is the point of the comparison.
- **Pixel fidelity.** Only geometry is compared. Two engines can agree on every
  box and still disagree on antialiasing, shadows and gradients.
- **Real pages.** Fixtures are synthetic and small by design. A real page
  exercises the cascade, external stylesheets and images, none of which appear
  in this corpus.

## Running it

The host this was written on runs `cargo` as a shim that refuses to compile
(host policy: no local Rust builds), so the build lives in CI:

```bash
gh workflow run parity --repo leshchenko1979/blitz-parity-spike
gh run watch --repo leshchenko1979/blitz-parity-spike
```

Locally, where a Rust toolchain is allowed:

```bash
cargo build --release
python3 tools/run_parity.py            # needs Chromium on PATH
```

`--blitz <path>` and `--chrome <path>` override discovery; `--bench-repeats N`
sets the KPI sample size.

## Reading the output

```
VERDICT  text-free (40 boxes): 40 exact, 0 within 1px, 0 divergent, 0 missing, 1 absence
reported text-dependent (6 boxes): ...
```

The `text-free` line is the verdict. `absence` is a fixture where both engines
paint nothing (auto cross margins cancel the stretch), so the assertion is
agreement on absence rather than a box.

## Control

`tools/run_parity.py --blitz <shim calling ogre>` reproduces the corpus against
an engine whose behaviour is already known, which validates the driver and the
instrument before either is pointed at Blitz: it returns **40/40 text-free boxes
exact**, and its only divergences are the four text fixtures plus ogre's known
missing inline formatting context.

## Pinned

Blitz crates are pinned to `=0.3.0-beta.4`: the API is pre-1.0 and moves between
betas, so an unpinned build would silently measure a different engine than the
one recorded. Chromium's version is printed in every run and stored in
`out/parity.json`.
