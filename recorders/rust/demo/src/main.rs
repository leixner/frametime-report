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
        // every 50th frame is far slower, so it counts as a stutter even on
        // machines whose sleeps overshoot a lot (CI runners)
        thread::sleep(Duration::from_millis(if i % 50 == 49 { 200 } else { 8 }));
        if i < 10 {
            ftlog::frame();
        } else {
            ftlog::frame_busy(5.0, 7.5);
        }
    }
    let threads: Vec<_> = (0..4)
        .map(|n| thread::spawn(move || ftlog::mark(&format!("thread-{}", n))))
        .collect();
    for t in threads {
        t.join().unwrap();
    }
    ftlog::flush();
}
