//! ftlog: frame-time recorder writing `frametime-log v1` (see SPEC.md).
//!
//! A single file using only std (Rust 1.70+). Copy it into your crate as
//! `src/ftlog.rs` and add `mod ftlog;`.
//!
//! It is off unless the `FTLOG` environment variable names an output file:
//!
//! ```text
//! set FTLOG=C:\logs\run.ftlog
//! ```
//!
//! When it is off, each call is one atomic load and nothing else.
//!
//! ```ignore
//! // once, when settings are known (optional):
//! ftlog::meta("settings.profile", "series-x");
//! ftlog::meta("settings.output", format_args!("{}x{}", w, h));
//! ftlog::meta("build.commit", option_env!("GIT_COMMIT").unwrap_or("unknown"));
//!
//! // once per presented frame, right after the present / swap call:
//! ftlog::frame();
//!
//! // optional named points, e.g. when loading is done:
//! ftlog::mark("loaded");
//!
//! // optional, at clean shutdown (data is also flushed every second):
//! ftlog::flush();
//! ```
//!
//! All functions are thread-safe. `frame()` must be called from one present
//! path (one swap chain), so that the deltas are between consecutive presents.

#![allow(dead_code)]

use std::fmt::Display;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::sync::{Mutex, OnceLock};
use std::time::Instant;

/// Environment variable naming the output file.
pub const ENV_VAR: &str = "FTLOG";

struct Recorder {
    out: BufWriter<File>,
    start: Instant,
    last: Option<Instant>,
    last_flush_ms: f64,
}

static STATE: OnceLock<Option<Mutex<Recorder>>> = OnceLock::new();

fn state() -> Option<&'static Mutex<Recorder>> {
    STATE
        .get_or_init(|| {
            let path = std::env::var_os(ENV_VAR)?;
            if path.is_empty() {
                return None;
            }
            let file = File::create(&path).ok()?;
            let mut out = BufWriter::with_capacity(1 << 16, file);
            writeln!(out, "# frametime-log v1").ok()?;
            writeln!(out, "t_ms,frame_ms").ok()?;
            Some(Mutex::new(Recorder {
                out,
                start: Instant::now(),
                last: None,
                last_flush_ms: 0.0,
            }))
        })
        .as_ref()
}

fn with<F: FnOnce(&mut Recorder)>(f: F) {
    if let Some(m) = state() {
        let mut guard = match m.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        f(&mut *guard);
    }
}

fn one_line(s: &str) -> String {
    s.replace(|c: char| c == '\r' || c == '\n', " ")
}

/// True when recording is on (`FTLOG` is set and the file could be created).
pub fn enabled() -> bool {
    state().is_some()
}

/// Metadata line: `# meta key=value`. Use dotted keys such as `settings.profile`.
pub fn meta(key: &str, value: impl Display) {
    with(|r| {
        let key = one_line(key).replace('=', "_");
        let value = one_line(&value.to_string());
        let _ = writeln!(r.out, "# meta {}={}", key.trim(), value.trim());
    });
}

/// Named point in time: `# mark <t_ms> <label>`. Spaces in the label become `-`.
pub fn mark(label: &str) {
    with(|r| {
        let t = r.start.elapsed().as_secs_f64() * 1000.0;
        let label = one_line(label).split_whitespace().collect::<Vec<_>>().join("-");
        let label = if label.is_empty() { "mark".to_string() } else { label };
        let _ = writeln!(r.out, "# mark {:.3} {}", t, label);
        let _ = r.out.flush();
    });
}

/// Record one presented frame. Call once per present, right after it.
/// The first call only starts the clock; every later call writes one line.
pub fn frame() {
    with(|r| {
        let now = Instant::now();
        let t = now.duration_since(r.start).as_secs_f64() * 1000.0;
        if let Some(prev) = r.last {
            let ms = now.duration_since(prev).as_secs_f64() * 1000.0;
            let _ = writeln!(r.out, "{:.3},{:.3}", t, ms);
        }
        r.last = Some(now);
        if t - r.last_flush_ms >= 1000.0 {
            let _ = r.out.flush();
            r.last_flush_ms = t;
        }
    });
}

/// Write buffered lines to disk.
pub fn flush() {
    with(|r| {
        let _ = r.out.flush();
    });
}
