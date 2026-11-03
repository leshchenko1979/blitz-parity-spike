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
use blitz_dom::{util::Color, Document as _, DocumentConfig};
use blitz_html::HtmlDocument;
use blitz_net::Provider;
use blitz_paint::paint_scene;
use blitz_traits::shell::{ColorScheme, Viewport};
use peniko::kurbo::Rect;
use peniko::Fill;

fn main() {
    // The net provider spawns a tokio task for every sub-resource fetch, so the
    // document has to be driven from inside a runtime. Without this, any
    // document with an <img> aborts with `there is no reactor running` before
    // layout starts.
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("tokio runtime")
        .block_on(real_main());
}

async fn real_main() {
    let args: Vec<String> = env::args().collect();
    let clip = args.get(1).map(|a| a == "--clip").unwrap_or(false);
    let as_document = args.iter().any(|a| a == "--document");

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
    // `Provider::is_empty` counts Arc references to its waker, and the waker is
    // cloned *inside* the spawned task, i.e. on its first poll. A fetch that has
    // been queued but not yet polled therefore reads as "empty", so a single
    // is_empty() check can exit before an image has even started loading: the
    // paint then happens against a document whose images never arrived. Require
    // several consecutive empty polls, yielding to the runtime in between, and
    // bound the wait so a stalled fetch cannot hang the renderer.
    let layout_started = Instant::now();
    let deadline = layout_started + std::time::Duration::from_secs(120);
    let mut quiet = 0u32;
    loop {
        document.resolve(0.0);
        if net.is_empty() {
            quiet += 1;
            if quiet >= 4 {
                break;
            }
        } else {
            quiet = 0;
        }
        if Instant::now() > deadline {
            eprintln!("warning: asset fetch deadline reached, painting anyway");
            break;
        }
        tokio::task::yield_now().await;
        tokio::time::sleep(std::time::Duration::from_millis(2)).await;
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
            for (i, id) in ids.iter().enumerate() {
                // Two different units meet here, and swapping them fails silently:
                //   * `get_client_bounding_rect` and the painter's node coordinates are
                //     CSS px, so the OFFSET must stay CSS px or the region is culled;
                //   * the canvas and the emitted PNG are physical px, so the SIZE must
                //     be scaled or the slide comes out at 1/scale of a browser's.
                let rect = document
                    .get_client_bounding_rect(*id)
                    .unwrap_or_else(|| panic!("no geometry for {sel} #{i}"));
                let s = scale as f64;
                let x = rect.x.round() as u32;
                let y = rect.y.round() as u32;
                let w = ((rect.width * s).round() as u32).max(1);
                let h = ((rect.height * s).round() as u32).max(1);
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
                let buffer = paint_region(&mut document, x, y, w, h, scale);
                let path = Path::new(out_path).join(format!("clip_{:02}.png", i + 1));
                write_png(File::create(&path).expect("create png"), &buffer, w, h);
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
