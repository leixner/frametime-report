//! Exercises ftlog.rs: run with FTLOG=<path> and check the file with
//! `python -I tests/check_demo_log.py <path>`.

#[path = "../../ftlog.rs"]
mod ftlog;

use std::{thread, time::Duration};

fn main() {
    println!("enabled: {}", ftlog::enabled());
    ftlog::meta("settings.profile", "demo");
    ftlog::meta("settings.output", format_args!("{}x{}", 1920, 1080));
    ftlog::meta("settings.raytracing", false);
    ftlog::meta("build.commit", option_env!("GIT_COMMIT").unwrap_or("unknown"));
    ftlog::meta("weird=key\nx", "multi\nline value");
    for i in 0..120 {
        if i == 20 {
            ftlog::mark("loaded scene");
        }
        thread::sleep(Duration::from_millis(if i % 50 == 49 { 40 } else { 8 }));
        ftlog::frame();
    }
    let threads: Vec<_> = (0..4)
        .map(|n| thread::spawn(move || ftlog::mark(&format!("thread-{}", n))))
        .collect();
    for t in threads {
        t.join().unwrap();
    }
    ftlog::flush();
}
