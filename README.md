# frametime-report

Frame-rate numbers you can compare.

"20 fps" can mean a peak or an average, a light scene or a heavy one, a 10-second clip or an hour of play. frametime-report turns a frame-time log into a small JSON report with fixed definitions:

- the time-weighted average fps;
- median, p95 and p99 frame times;
- 1% lows and stutter count;
- the settings and scene it was measured in, and whether the run was long enough to count.

It doesn't depend on any particular application, engine or graphics API. It works for games, emulators, compatibility layers and ports.

```
pc-a.ftlog  |  series-x 1920x1080  |  scene city-walk
  7062 frames over 304.9 s (skipped 30 s, from mark loaded)
  avg 23.16 fps | median 43.03 ms | p95 48.04 ms | p99 50.37 ms | 1% low 16.07 fps | stutters 10 | max 135.54 ms
  valid: yes
```

There are three parts. Use whichever fits:

| Part | What it is | Needs |
|---|---|---|
| [`ftreport.py`](ftreport.py) | CLI: frame log or PresentMon/CapFrameX CSV → report, compare, validate | Python 3.8+, no packages |
| [`recorders/rust/ftlog.rs`](recorders/rust/ftlog.rs) | Drop-in recorder for Rust projects, one file | std only, Rust 1.70+ |
| [`recorders/c/ftlog.h`](recorders/c/ftlog.h) | Drop-in recorder for C/C++ projects, single header | C99 or C++, Windows or POSIX |

The formats are in [SPEC.md](SPEC.md).

---

## For testers: no code changes needed

Record with [PresentMon](https://github.com/GameTechDev/PresentMon) or [CapFrameX](https://github.com/CXWorld/CapFrameX), then:

```powershell
python ftreport.py summarize capture.csv --process app.exe --set scene=city-walk --set settings.profile=series-x --set settings.output=1920x1080 --set "pc.gpu=RX 7900 XTX" --set pc.ramGB=64
```

That writes `capture.report.json` next to the CSV. Attach it wherever you post your result.

Defaults (see `python ftreport.py summarize -h`):
- the first 30 s are skipped (`--skip`);
- a run must be at least 180 s long (`--min-seconds`);
- a report needs `settings.profile` and `scene` to count as valid (`--require`).

Pick `scene` from the short list in [SPEC.md](SPEC.md#3-scene-ids), so results can be grouped.

To compare runs, the first report is the baseline:

```powershell
python ftreport.py compare before.report.json after.report.json other-pc.report.json
```

`compare` says whether two runs are **like for like**: same profile, output, ray tracing, upscaler and scene. On the same commit it also says whether they **reproduce** each other: average fps within 10%.

## For projects: built-in recording in three lines

Building the recorder in means testers don't need PresentMon. The project also fills in the settings it knows (profile, resolution, ray tracing, commit) itself, so reports are complete and consistent.

Both recorders:
- are **off unless the `FTLOG` environment variable is set** to an output path;
- when off, do nothing but a single check;
- buffer in memory and flush once a second;
- write the same `frametime-log v1` text.

### Rust

Copy `recorders/rust/ftlog.rs` to `src/ftlog.rs` and add `mod ftlog;`. Then:

```rust
// where the settings are known (optional, any time):
ftlog::meta("settings.profile", profile_name);
ftlog::meta("settings.output", format_args!("{}x{}", width, height));
ftlog::meta("settings.raytracing", raytracing_enabled);
ftlog::meta("build.commit", option_env!("GIT_COMMIT").unwrap_or("unknown"));

// right after the host Present / swap call:
ftlog::frame();

// optional, e.g. when the first loading screen is over:
ftlog::mark("loaded");
```

It's thread-safe (a `Mutex` behind a `OnceLock`). Call `frame()` from the one present path you want to measure.

### C / C++

```c
// in exactly one .c/.cpp file:
#define FTLOG_IMPLEMENTATION
#include "ftlog.h"

// anywhere:
ftlog_meta("settings.profile", "series-x");
ftlog_frame();            // right after the host Present / swap call
ftlog_mark("loaded");
ftlog_close();            // optional, at shutdown
```

It isn't thread-safe: call it from the present thread or guard it yourself. Numbers are written without `printf("%f")`, so the C locale can't turn decimal points into commas.

### Then

```powershell
$env:FTLOG = "C:\logs\run.ftlog"
# run the application, play the scene for 3+ minutes, quit
python ftreport.py summarize C:\logs\run.ftlog --from-mark loaded --set scene=city-walk --set "pc.gpu=RTX 4080"
```

### Where to call `frame()`

Call it once per image actually handed to the display, on the host side: after `IDXGISwapChain::Present`, `vkQueuePresentKHR` or the equivalent. If your project presents through more than one path, such as a menu path and a game path, call it on the path that carries the frames you want to measure. Don't call it per command list or per submission.

## What the numbers mean

| Field | Meaning |
|---|---|
| `avgFps` | frames ÷ seconds: the time-weighted average, not the mean of per-frame fps |
| `medianMs` | median frame time |
| `p95Ms`, `p99Ms` | 95% and 99% of frames are this fast or faster (nearest-rank) |
| `low1Fps` | fps over the slowest 1% of frames |
| `stutters` | frames longer than 2.5 × the median |
| `checks.valid` | long enough, enough frames, required fields present |

Full definitions are in [SPEC.md](SPEC.md#definitions).

## Development

```powershell
python -I -m unittest discover -s tests
```

Both recorders have a small demo program that records 120 frames. `tests/check_demo_log.py` checks the log it writes: frame count, marks, metadata cleaning, decimal points and stutters.

```powershell
cargo build --release --manifest-path recorders/rust/demo/Cargo.toml
$env:FTLOG = "$PWD\rust.ftlog"; recorders\rust\demo\target\release\ftlog-demo.exe; Remove-Item Env:FTLOG
python -I tests/check_demo_log.py rust.ftlog
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs all of this on every push:
- the Python tests on 3.8 and 3.13;
- the Rust demo on Rust 1.70 and stable, on Linux, Windows and macOS;
- the C demo as C99 and C++11 with GCC, Clang and MSVC, with all warnings as errors.

`examples/` has two synthetic logs from made-up machines, with their reports.

## Status

- Version `0.1`. The format may still change based on feedback.
- `ftreport.py` is covered by the tests.
- The Rust recorder is built and run on Windows with Rust 1.70 and 1.100, with `-D warnings`. Its output goes through `ftreport` unchanged.
- The C recorder compiles with `-Wall -Wextra -Werror -pedantic` (Clang via Zig 0.17.1) for:
  - C99 and C++11 on Windows;
  - C99 (with the documented `_POSIX_C_SOURCE`), gnu11 and C++17 on Linux;
  - C11 on macOS.
- On Windows, both the C and C++ builds run, including under a German locale: decimal points stay points. Their output goes through `ftreport` unchanged.

## License

MIT, see [LICENSE](LICENSE).
