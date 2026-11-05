//! Render HTML to PNG at a fixed viewport, with optional device-scale and
//! per-element clipping, so Blitz can be measured against Chromium the way the
//! deck pipeline actually uses it.
//!
//! Usage:
//!   render <html> <out.png> <width> <height> [scale] [--document]
//!   render --clip <html> <outdir> <width> <height> <scale> <selector> [--document]
//!
//! `--document` renders the file as a complete document (no injected reset, no
//! body wrapper). Without it the file is treated as a fragment and wrapped in
//! the same reset the ogre parity harness uses, so both engines see identical
//! markup.
//!
//! Clip mode mirrors Playwright's `locator(...).screenshot()`: every match of
//! `selector` is painted into its own PNG at the element's border box, in
//! physical pixels (CSS box x scale).

use std::env;
use std::fs::File;
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use anyrender::{render_to_buffer, PaintScene as _};
use anyrender_vello_cpu::VelloCpuImageRenderer;
use blitz_dom::{util::Color, Document as _, DocumentConfig, Point};
use blitz_html::HtmlDocument;
use blitz_net::Provider;
use blitz_paint::paint_scene;
use blitz_traits::shell::{ColorScheme, Viewport};
use peniko::kurbo::Rect;
use peniko::Fill;

/// Trace output is capped: an unbounded naive drain loop once wrote 1.12 GB.
const TRACE_PASSES: u32 = 8;

fn main() {
    // The net provider spawns a tokio task for every sub-resource fetch, so the
    // document has to be driven from inside a runtime. Without this, any
    // document with an <img> aborts with `there is no reactor running` before
    // layout starts.
    // BLITZ_RUNTIME selects the tokio flavour. `current` is what the shipped
    // CLI uses (cheaper, deterministic); `multi` reproduces the regime of
    // upstream's examples/screenshot.rs, which is `#[tokio::main]`.
    let multi = std::env::var("BLITZ_RUNTIME").as_deref() == Ok("multi");
    let mut builder = if multi {
        tokio::runtime::Builder::new_multi_thread()
    } else {
        tokio::runtime::Builder::new_current_thread()
    };
    builder
        .enable_all()
        .build()
        .expect("tokio runtime")
        .block_on(real_main());
}

async fn real_main() {
    let args: Vec<String> = env::args().collect();
    let clip = args.get(1).map(|a| a == "--clip").unwrap_or(false);
    let as_document = args.iter().any(|a| a == "--document");
    // Clip mode: paint one element-sized canvas per match, with the viewport
    // scrolled to that element, instead of one full-height canvas that is
    // cropped afterwards. Same pixels, a fraction of the peak memory.
    let use_scroll = args.iter().any(|a| a == "--scroll");

    // Positional args start after the program name, plus the `--clip` marker.
    let pos: Vec<&String> = args
        .iter()
        .skip(if clip { 2 } else { 1 })
        .filter(|a| !a.starts_with("--"))
        .collect();

    let usage = if clip {
        "usage: render --clip <html> <outdir> <width> <height> <scale> <selector> [--document]"
    } else {
        "usage: render <html> <out.png> <width> <height> [scale] [--document]"
    };

    let (html_path, out_path, width, height, scale, selector) = if clip {
        if pos.len() < 6 {
            eprintln!("{usage}");
            std::process::exit(2);
        }
        (
            pos[0].as_str(),
            pos[1].as_str(),
            pos[2].parse::<u32>().expect("width"),
            pos[3].parse::<u32>().expect("height"),
            pos[4].parse::<f32>().expect("scale"),
            Some(pos[5].as_str()),
        )
    } else {
        if pos.len() < 4 {
            eprintln!("{usage}");
            std::process::exit(2);
        }
        (
            pos[0].as_str(),
            pos[1].as_str(),
            pos[2].parse::<u32>().expect("width"),
            pos[3].parse::<u32>().expect("height"),
            pos.get(4)
                .map(|s| s.parse::<f32>().expect("scale"))
                .unwrap_or(1.0),
            None,
        )
    };

    let started = Instant::now();
    let source = std::fs::read_to_string(html_path).expect("read html");
    let doc_html = if as_document {
        source
    } else {
        format!(
            "<html><head><style>html,body{{margin:0;padding:0;background:#ffffff;}}</style></head><body>{source}</body></html>"
        )
    };
    let base_url = format!(
        "file://{}",
        Path::new(html_path)
            .canonicalize()
            .expect("canonicalize path")
            .display()
    );

    // The viewport is physical pixels: Blitz divides window_size by the scale to
    // get the CSS viewport, which is how device_scale_factor behaves in a
    // browser (1280x720 CSS at scale 2 => 2560x1440 physical).
    let phys_w = (width as f32 * scale).round() as u32;
    let phys_h = (height as f32 * scale).round() as u32;

    let net = Arc::new(Provider::new(None));
    let mut document = HtmlDocument::from_html(
        &doc_html,
        DocumentConfig {
            base_url: Some(base_url),
            net_provider: Some(Arc::clone(&net) as _),
            viewport: Some(Viewport::new(phys_w, phys_h, scale, ColorScheme::Light)),
            ..Default::default()
        },
    );
    let parsed = started.elapsed();

    // Drain any sub-resource fetches, then resolve style and layout.
    //
    // BLITZ_DRAIN selects the loop shape so the documented pattern and the one
    // that actually works can be compared on the SAME binary:
    //   naive  - exactly the loop in examples/screenshot.rs: resolve, then a
    //            single is_empty() check, no yield
    //   naive2 - as naive, plus the trailing resolve that paint_bench.rs has
    //            and screenshot.rs lacks
    //   yield  - as naive, but yields to the runtime between iterations
    //   quiet  - four consecutive empty polls, each preceded by a yield and a
    //            short sleep (the default, and what the shipped build uses)
    //
    // `naive` loses images on BOTH runtime flavours, for opposite reasons; see
    // tools/drain_probe.py for the measured table.
    let drain_mode = std::env::var("BLITZ_DRAIN").unwrap_or_else(|_| "quiet".to_string());
    let layout_started = Instant::now();
    let deadline_secs: u64 = std::env::var("BLITZ_DEADLINE_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(120);
    let deadline = layout_started + std::time::Duration::from_secs(deadline_secs);
    let mut quiet = 0u32;
    let mut pass = 0u32;
    let trace = std::env::var("BLITZ_TRACE").is_ok();
    loop {
        if trace && pass < TRACE_PASSES {
            eprintln!("pass {pass}: before resolve count={} empty={}", net.count(), net.is_empty());
        }
        document.resolve(0.0);
        if trace && pass < TRACE_PASSES {
            eprintln!("pass {pass}: after  resolve count={} empty={}", net.count(), net.is_empty());
        }
        pass += 1;
        match drain_mode.as_str() {
            "naive" | "naive2" => {
                if net.is_empty() {
                    break;
                }
            }
            "yield" => {
                if net.is_empty() {
                    break;
                }
                tokio::task::yield_now().await;
            }
            _ => {
                if net.is_empty() {
                    quiet += 1;
                    if quiet >= 4 {
                        break;
                    }
                } else {
                    quiet = 0;
                }
                tokio::task::yield_now().await;
                tokio::time::sleep(std::time::Duration::from_millis(2)).await;
            }
        }
        if Instant::now() > deadline {
            eprintln!("warning: asset fetch deadline reached, painting anyway");
            break;
        }
    }
    if trace && pass >= TRACE_PASSES {
        eprintln!("(trace capped at {TRACE_PASSES} passes; loop ran {pass})");
    }
    // naive2: examples/paint_bench.rs resolves once more after its drain loop
    // than examples/screenshot.rs does. Tested rather than assumed.
    if drain_mode == "naive2" {
        document.resolve(0.0);
    }
    let laid_out = layout_started.elapsed();

    let paint_started = Instant::now();
    match selector {
        None => {
            let buffer = paint_region(&mut document, 0, 0, phys_w, phys_h, scale);
            write_png(
                File::create(out_path).expect("create png"),
                &buffer,
                phys_w,
                phys_h,
            );
        }
        Some(sel) => {
            let ids = document
                .query_selector_all(sel)
                .unwrap_or_else(|e| panic!("bad selector {sel:?}: {e:?}"));
            if ids.is_empty() {
                eprintln!("no match for selector {sel:?}");
                std::process::exit(3);
            }
            std::fs::create_dir_all(out_path).expect("create outdir");
            let s = scale as f64;

            // Every matched element's box, in document coordinates, at scroll 0.
            let rects: Vec<_> = ids
                .iter()
                .enumerate()
                .map(|(i, id)| {
                    document
                        .get_client_bounding_rect(*id)
                        .unwrap_or_else(|| panic!("no geometry for {sel} #{i}"))
                })
                .collect();

            if use_scroll {
                // One canvas per element, sized to the element, painted at zero
                // offset with the viewport scrolled to the element's top-left.
                //
                // `set_viewport_scroll` is a PAINT-time offset (`origin =
                // root_pos - viewport_scroll`), not a layout input, so the
                // document is laid out exactly once and `vh` units still
                // resolve against the requested viewport.
                for (i, rect) in rects.iter().enumerate() {
                    let w = (((rect.width * s).round() as u32).max(1));
                    let h = (((rect.height * s).round() as u32).max(1));
                    document.set_viewport_scroll(Point { x: rect.x, y: rect.y });
                    let buffer = paint_region(&mut document, 0, 0, w, h, scale);
                    println!(
                        "clip_{:02} css x={:.2} y={:.2} w={:.2} h={:.2}  physical {}x{} (scroll)",
                        i + 1,
                        rect.x,
                        rect.y,
                        rect.width,
                        rect.height,
                        w,
                        h
                    );
                    let path = Path::new(out_path).join(format!("clip_{:02}.png", i + 1));
                    write_png(File::create(&path).expect("create png"), &buffer, w, h);
                }
                document.set_viewport_scroll(Point::ZERO);
            } else {
                // Paint ONE full-height canvas at zero offset and crop each element out
                // of it, rather than painting one region per element.
                //
                // Two reasons, both measured on the real deck:
                //   * the viewport must stay at the requested size, because `vh` units
                //     resolve against it — enlarging it to the document height would
                //     relayout the page (the deck has 3 `vh` uses);
                //   * a non-zero paint offset is not reliable for replaced content: with
                //     the offset set, slides whose photos sit below the first viewport
                //     lost them, while the same slides were exact when cropped from a
                //     full-document render.
                //
                // The canvas is physical px; the element boxes are CSS px.
                let doc_phys_h = rects
                    .iter()
                    .map(|r| ((r.y + r.height) * s).round() as u32)
                    .max()
                    .unwrap_or(phys_h)
                    .max(phys_h);
                let canvas = paint_region(&mut document, 0, 0, phys_w, doc_phys_h, scale);

                for (i, rect) in rects.iter().enumerate() {
                    let x = ((rect.x * s).round() as u32).min(phys_w.saturating_sub(1));
                    let y = ((rect.y * s).round() as u32).min(doc_phys_h.saturating_sub(1));
                    let w = (((rect.width * s).round() as u32).max(1)).min(phys_w - x);
                    let h = (((rect.height * s).round() as u32).max(1)).min(doc_phys_h - y);
                    println!(
                        "clip_{:02} css x={:.2} y={:.2} w={:.2} h={:.2}  physical {}x{} at {},{}",
                        i + 1,
                        rect.x,
                        rect.y,
                        rect.width,
                        rect.height,
                        w,
                        h,
                        x,
                        y
                    );
                    let mut out = Vec::with_capacity((w * h * 4) as usize);
                    for row in 0..h {
                        let start = (((y + row) * phys_w + x) * 4) as usize;
                        out.extend_from_slice(&canvas[start..start + (w * 4) as usize]);
                    }
                    let path = Path::new(out_path).join(format!("clip_{:02}.png", i + 1));
                    write_png(File::create(&path).expect("create png"), &out, w, h);
                }
            }
        }
    }
    let painted = paint_started.elapsed();

    eprintln!(
        "TIMING parse_ms={} layout_ms={} paint_ms={} total_ms={} scale={}",
        parsed.as_millis(),
        laid_out.as_millis(),
        painted.as_millis(),
        started.elapsed().as_millis(),
        scale
    );
}

/// Paint a `w`x`h` physical-pixel region whose top-left is at (`x`, `y`) in
/// document coordinates.
fn paint_region(
    document: &mut HtmlDocument,
    x: u32,
    y: u32,
    w: u32,
    h: u32,
    scale: f32,
) -> Vec<u8> {
    render_to_buffer::<VelloCpuImageRenderer, _>(
        |scene| {
            scene.fill(
                Fill::NonZero,
                Default::default(),
                Color::WHITE,
                Default::default(),
                &Rect::new(0.0, 0.0, w as f64, h as f64),
            );
            paint_scene(scene, document.as_mut(), scale as f64, w, h, x, y);
        },
        w,
        h,
    )
}

fn write_png<W: std::io::Write>(writer: W, buffer: &[u8], width: u32, height: u32) {
    let mut encoder = png::Encoder::new(writer, width, height);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder.write_header().expect("png header");
    writer.write_image_data(buffer).expect("png data");
    writer.finish().expect("png finish");
}
