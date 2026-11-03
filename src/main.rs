//! Render one HTML fragment to a PNG at a fixed viewport.
//!
//! The document wrapper, the viewport and the canvas are kept byte-for-byte in
//! step with the ogre parity harness (`test/chromium/browser.go`), so the PNG
//! this produces can be measured by the same instrument as a browser screenshot
//! and the two numbers compared directly.
//!
//! Usage: render <html-file> <out.png> <width> <height>

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

// The net provider spawns a tokio task for every sub-resource fetch, so main
// must run inside a runtime. Without this, any document with an <img> aborts
// with `there is no reactor running` before layout starts.
#[tokio::main]
async fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 5 {
        eprintln!("usage: render <html-file> <out.png> <width> <height>");
        std::process::exit(2);
    }
    let html_path = Path::new(&args[1]);
    let out_path = &args[2];
    let width: u32 = args[3].parse().expect("width must be a number");
    let height: u32 = args[4].parse().expect("height must be a number");

    let body = std::fs::read_to_string(html_path).expect("read fixture");
    let doc_html = format!(
        "<html><head><style>html,body{{margin:0;padding:0;background:#ffffff;}}</style></head><body>{body}</body></html>"
    );
    let base_url = format!(
        "file://{}",
        html_path
            .canonicalize()
            .expect("canonicalize fixture path")
            .display()
    );

    let started = Instant::now();

    let net = Arc::new(Provider::new(None));
    let mut document = HtmlDocument::from_html(
        &doc_html,
        DocumentConfig {
            base_url: Some(base_url),
            net_provider: Some(Arc::clone(&net) as _),
            viewport: Some(Viewport::new(width, height, 1.0, ColorScheme::Light)),
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
    let deadline = layout_started + std::time::Duration::from_secs(30);
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
    let buffer = render_to_buffer::<VelloCpuImageRenderer, _>(
        |scene| {
            scene.fill(
                Fill::NonZero,
                Default::default(),
                Color::WHITE,
                Default::default(),
                &Rect::new(0.0, 0.0, width as f64, height as f64),
            );
            paint_scene(scene, document.as_mut(), 1.0, width, height, 0, 0);
        },
        width,
        height,
    );
    let painted = paint_started.elapsed();

    let file = File::create(out_path).expect("create png");
    write_png(file, &buffer, width, height);

    eprintln!(
        "TIMING parse_ms={} layout_ms={} paint_ms={} total_ms={}",
        parsed.as_millis(),
        laid_out.as_millis(),
        painted.as_millis(),
        started.elapsed().as_millis()
    );
}

fn write_png<W: std::io::Write>(writer: W, buffer: &[u8], width: u32, height: u32) {
    let mut encoder = png::Encoder::new(writer, width, height);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder.write_header().expect("png header");
    writer.write_image_data(buffer).expect("png data");
    writer.finish().expect("png finish");
}
