# frametime-report

Frame-rate numbers you can compare.

**[Try it in your browser →](https://leixner.github.io/frametime-report/)** Drop in a capture and get the report. Nothing is uploaded.

"20 fps" can mean a peak or an average, a light scene or a heavy one, a 10-second clip or an hour of play. frametime-report turns a frame-time log into a small JSON report with fixed definitions:

- the time-weighted average fps;
- median, p95 and p99 frame times;
- 1% lows, stutters (relative hitches) and freezes (frames over 200 ms);
- how much of the run was at 30 or 60 fps or better;
- **whether the run was CPU-bound, GPU-bound or capped**, when CPU and GPU busy times are available;
- **CPU, RAM, GPU and VRAM usage** during the run, and the hardware it ran on, recorded automatically on Windows;
- the settings and scene it was measured in, and whether the run was long enough to count.

It doesn't depend on any particular application, engine or graphics API. It works for games, emulators, compatibility layers and ports.

```
pc-b.ftlog  |  series-x 1920x1080  |  scene city-walk
  7085 frames over 320.7 s (skipped 30 s, from mark loaded)
  avg 22.09 fps | median 44.99 ms | p95 50.00 ms | p99 51.98 ms | 1% low 12.86 fps | max 305.49 ms
  stutters 16 (> 2.5 x median) | freezes 3 (> 200 ms) | time at >= 30 fps 0% | >= 60 fps 0%
  cpu busy 41.12 ms | gpu busy 32.47 ms | gpu >= cpu on 0% of frames | CPU-bound
  usage (median, 641 samples): cpu 11% (peak 17) | ram 9898 MB (peak 10034) | gpu 71% (peak 77) | vram 9400 MB (peak 9497) | system cpu 38% (peak 47) | system ram 81% (peak 82)
  pc: Example CPU B 6-Core | Example GPU B 12 GB | 32 GB RAM | Windows 10 (build 19045, 22H2)
  valid: yes
```

| Part | What it is | Needs |
|---|---|---|
| [Web page](https://leixner.github.io/frametime-report/) | Drop a capture, see the report and a frame-time chart, download the JSON | A browser |
| [`ftreport.py`](ftreport.py) | CLI: frame log or PresentMon/CapFrameX CSV → report, compare, validate | Python 3.8+, no packages |
| [`recorders/rust/ftlog.rs`](recorders/rust/ftlog.rs) | Drop-in recorder for Rust projects, one file | std only, Rust 1.70+ |
| [`recorders/c/ftlog.h`](recorders/c/ftlog.h) | Drop-in recorder for C/C++ projects, single header | C99 or C++, Windows or POSIX |
| [JSON Schema](docs/schema/report-0.1.schema.json) | Validates reports without this tool | Any JSON Schema 2020-12 validator |

The web page and the CLI produce identical reports; a test checks this. The formats are in [SPEC.md](SPEC.md).

---

## For testers: no code changes needed

Record with [PresentMon](https://github.com/GameTechDev/PresentMon) or [CapFrameX](https://github.com/CXWorld/CapFrameX). Then either drop the CSV on the [web page](https://leixner.github.io/frametime-report/), or:

```powershell
python ftreport.py summarize capture.csv --process app.exe --set scene=city-walk --set settings.profile=series-x --set settings.output=1920x1080 --set "pc.gpu=RX 7900 XTX" --set pc.ramGB=64
```

That writes `capture.report.json` next to the CSV. Attach it wherever you post your result.

**PresentMon 2.x captures include `CPUBusy` and `GPUBusy`,** so the report also says whether the run was CPU-bound or GPU-bound. That tells you whether a faster graphics card would help at all.

Defaults (see `python ftreport.py summarize -h`):
- the first 30 s are skipped (`--skip`);
- a run must be at least 180 s long (`--min-seconds`);
- a report needs `settings.profile` and `scene` to count as valid (`--require`).

Pick `scene` from the short list in [SPEC.md](SPEC.md#3-scene-ids), so results can be grouped.

Add `--detect-pc` to fill in this computer's CPU, GPU and VRAM, driver, RAM, OS and cores (model names only) wherever the capture doesn't already say. Values from the log and from `--set` win.

**One session, several scenes:** if the log has marks (`ftlog::mark("city")`, `ftlog::mark("drive")`), report each stretch separately:

```powershell
python ftreport.py summarize run.ftlog --from-mark city --until-mark drive --skip 0 --set scene=city-walk --json city.report.json
python ftreport.py summarize run.ftlog --from-mark drive --skip 0 --set scene=city-drive --json drive.report.json
```

The web page has the same choices under *Start at mark* and *End at mark*.

**Cold boot (start-up from launch):** on Windows the recorders note how long the first frame took after launch. Report the whole start-up, black screen and shader compilation included:

```powershell
python ftreport.py summarize boot.ftlog --skip 0 --max-seconds 300 --set capture.phase=cold-boot --set settings.shaderCache=cold --set scene=menu --json boot.report.json
```

The time before the first frame counts as one long frame, so it shows in avg fps, freezes and the longest frame. For PresentMon captures, add `--set capture.firstPresentMs=<ms from launch to first frame>`. `compare` keeps cold-boot and scene reports apart, and says *unknown* when only one report declares a phase, cache state or other setting.

To compare runs, the first report is the baseline:

```powershell
python ftreport.py compare before.report.json after.report.json other-pc.report.json
```

`compare` says whether two runs are **like for like**: same profile, output and internal resolution, ray tracing, upscaler, fps cap and scene. A setting given in one report but not the other counts as a difference. On the same commit it also says whether they **reproduce** each other: average fps within 10%.

## For trackers and dashboards

Validate incoming reports against the published schema, with no need for this tool:

```
https://leixner.github.io/frametime-report/schema/report-0.1.schema.json
```

`ftreport validate report.json` does the same checks from the command line.

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
// ...or, if you measure them, with the frame's CPU and GPU busy times in ms:
ftlog::frame_busy(submit_thread_cpu_ms, gpu_timestamp_ms);

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
ftlog_frame();                         // right after the host Present / swap call
ftlog_frame_busy(cpu_ms, gpu_ms);      // ...or with the frame's busy times
ftlog_mark("loaded");
ftlog_close();                         // optional, at shutdown
```

It isn't thread-safe: call it from the present thread or guard it yourself. Numbers are written without `printf("%f")`, so the C locale can't turn decimal points into commas.

### Hardware and usage (Windows)

With recording on, both recorders write the hardware names once from a background thread: CPU, GPU and its memory, driver, RAM, Windows version and logical cores. Model names only, nothing that identifies a person or a machine.

Set `FTLOG_SAMPLE=1` (every 500 ms) or `FTLOG_SAMPLE=<ms>` (100 to 10000) to also sample usage on that thread:
- this process's CPU % and memory;
- GPU % of the busiest engine, the same figure Task Manager shows;
- dedicated VRAM in use;
- system CPU % and memory load.

It reads Windows' own counters, so it works on NVIDIA, AMD and Intel without vendor SDKs, and loads everything beyond kernel32 at run time: nothing to link. On Linux and macOS the Rust recorder writes the OS and core count; usage sampling is Windows-only for now.

```powershell
$env:FTLOG = "C:\logs\run.ftlog"; $env:FTLOG_SAMPLE = "1"
```

### Overhead

Measured with the Rust demo (`ftlog-demo bench` and `ftlog-demo idle`) on a Ryzen 7 5700X with an RTX 3070, Windows 11, release build:

| | Cost |
|---|---|
| `frame()`, recording off | 2.4 ns per call: one check |
| `frame()`, recording on | about 170 ns per call: about 10 µs per second at 60 fps |
| Hardware names | once, on a background thread |
| Usage sampler, setup | about 280 ms of CPU once, on its own thread (Windows opens the GPU counters) |
| Usage sampler, each sample | about 0.4 ms of CPU on its own thread: 0.08% of one core at 2 samples per second |

The thread that calls `frame()` never waits for the sampler: the sampler collects outside the lock and holds it only to write one line. GPU % was checked against Task Manager's method under full load: 99.8 to 100% from the sampler vs 99 to 100% read independently.

### Busy times

Pass whatever you already measure:
- **CPU:** usually the time the thread that builds and submits the frame spent on it.
- **GPU:** usually from timestamp queries around the frame's work.

GPU times that arrive a few frames late are fine: the report uses medians and per-frame comparisons over minutes. Pass a negative value for a time you don't have. The verdict needs busy times on at least half of the frames.

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
| `stutters` | frames longer than 2.5 × the median: hitches relative to the run's own pace |
| `freezes` | frames longer than 200 ms: hangs you notice at any frame rate (same threshold as KLBF's BM-v1) |
| `timeAt30Pct`, `timeAt60Pct` | share of the run's time at 30 / 60 fps or better: "how much of it was playable" in one number |
| `usage` | median and peak of the sampled CPU, RAM, GPU, VRAM and system load during the measured window |
| `busy.limit` | `gpu` / `cpu`: which side is busy longer on most frames. `mixed`: neither clearly. `pacing`: neither is busy for 75% of the frame, so a cap, vsync or waits set the pace |
| `checks.valid` | long enough, enough frames, required fields present |

Full definitions are in [SPEC.md](SPEC.md#definitions).

## Development

```powershell
python -I -m unittest discover -s tests
```

- `test_ftreport.py`: statistics, readers, busy analysis, validity, compare and validate.
- `test_parity.py`: runs the web page's [`ftreport-core.js`](docs/ftreport-core.js) in Node on the same 30+ inputs and requires identical reports. Skipped without Node.
- `test_schema.py`: generated and example reports match the JSON Schema, and broken ones don't. Needs `pip install jsonschema`; skipped without it.

Both recorders have a small demo program that records 120 frames. `tests/check_demo_log.py` checks the log it writes: frame count, marks, metadata cleaning, busy times, decimal points and stutters.

```powershell
cargo build --release --manifest-path recorders/rust/demo/Cargo.toml
$env:FTLOG = "$PWD\rust.ftlog"; recorders\rust\demo\target\release\ftlog-demo.exe; Remove-Item Env:FTLOG
python -I tests/check_demo_log.py rust.ftlog
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs all of this on every push:
- the Python tests on 3.8 and 3.13, including parity and schema;
- the Rust demo on Rust 1.70 and stable, on Linux, Windows and macOS;
- the C demo as C99 and C++11 with GCC, Clang and MSVC, with all warnings as errors.

To run the web page locally: `python -m http.server --directory docs`.

`examples/` has two synthetic runs from made-up machines, one GPU-bound and one CPU-bound, with their reports.

## Status

- Version `0.4.0`. The format may still change based on feedback; changes so far are additive.
- The CLI and the web page are covered by the tests above.
- Both recorders are built, run and checked in CI on Linux, Windows and macOS, including MSVC and Rust 1.70. On Windows, CI also runs the usage sampler and checks the hardware names. The C recorder keeps decimal points under a German locale.

## License

MIT, see [LICENSE](LICENSE).
