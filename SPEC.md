# frametime-report: formats (v0.1)

There are two formats:

- **frame log** (`.ftlog`): written while an application runs. It's a plain text file, one line per frame.
- **report** (`.json`): written by `ftreport summarize` from a frame log or a PresentMon/CapFrameX CSV.

Both are independent of any particular application, engine or graphics API.

---

## 1. Frame log (`frametime-log v1`)

UTF-8 text with `\n` line endings. Lines starting with `#` are header or annotation lines. Every other non-empty line is one frame.

```
# frametime-log v1
# meta settings.profile=series-x
# meta settings.output=1920x1080
# meta settings.raytracing=false
# meta build.repo=owner/name
# meta build.commit=abc1234
t_ms,frame_ms
16.71,16.710
33.40,16.690
# mark 31250.00 loaded
...
```

| Line | Meaning |
|---|---|
| `# frametime-log v1` | Must be the first line. |
| `# meta <key>=<value>` | Metadata. The key is a dotted path into the report, such as `settings.profile`. It may appear anywhere, and later values win. |
| `# mark <t_ms> <label>` | A named point in time, such as `loaded` or `scene-start`. Labels contain no spaces. |
| `# sample <t_ms> key=value ...` | Resource usage at a point in time, written by the recorders' optional sampler. Keys: `cpu` (this process, % of all logical cores), `ram` (this process, private MB), `gpu` (% of the busiest GPU engine, summed over all processes, like Task Manager), `vram` (dedicated GPU memory in use, MB), `syscpu` (whole system, %), `sysram` (system memory load, %). Missing keys mean "not measured"; negative or non-numeric values are ignored. |
| `t_ms,frame_ms` | Column header for the frame lines. Optional. It may also be `t_ms,frame_ms,cpu_ms,gpu_ms`. |
| `<t_ms>,<frame_ms>` | One frame. `t_ms` is the time since logging started at the end of the frame, in milliseconds. `frame_ms` is the time since the previous frame ended. |
| `<t_ms>,<frame_ms>,<cpu_ms>,<gpu_ms>` | One frame, plus how long the CPU and the GPU were busy on it. An empty value or `NA` means unknown. Lines with and without busy times can be mixed. |

Numbers are plain decimals (`16.667`, `-2`, `.5`, `1e3`). Values like `inf`, `nan`, `1_000` or a decimal comma are not numbers.

**What counts as a frame:** one presented image, measured on the host between consecutive present or swap calls on one swap chain. Recorders measure with a monotonic clock (`QueryPerformanceCounter`, `std::time::Instant`, `CLOCK_MONOTONIC`).

**Recommended metadata keys** (all optional):

| Key | Example |
|---|---|
| `app` | free text |
| `build.repo` | `owner/name` |
| `build.commit` | `abc1234` |
| `settings.profile` | `series-x`, `series-s`, or any short id |
| `settings.output` | `1920x1080` |
| `settings.internal` | `1280x720` |
| `settings.raytracing` | `true` / `false` |
| `settings.upscaler` | `none`, `fsr2`, `fsr4`, … |
| `settings.fpsCap` | `30`, `60`, `0` (none) |
| `settings.shaderCache` | `cold` (graphics and shader caches empty) or `warm` |
| `scene` | see the scene list below |
| `capture.phase` | `cold-boot` (measured from launch) or `scene` (a stretch of gameplay) |
| `capture.clockStart` | `launch` or `first-present`; see [Cold boot](#cold-boot) |
| `capture.firstPresentMs` | ms from launch to the first present |
| `pc.cpu` / `pc.gpu` / `pc.driver` / `pc.os` | model and version names |
| `pc.ramGB` / `pc.vramGB` / `pc.cores` | numbers (logical cores) |

On Windows the recorders fill in all `pc.*` keys themselves, once, from a background thread. They write model names only: no host names, user names, serial numbers or paths. They also write `capture.firstPresentMs` on the first present: the time since the process was created (under Wine or Proton, since Wine created the game's process, so Wine's own start-up before that isn't included).

The application usually knows `build.*` and `settings.*`. The person testing adds `scene` and `pc.*` with `ftreport summarize --set`.

## 2. Report (`frametime-report/0.1`)

```json
{
  "format": "frametime-report/0.1",
  "app": "…",
  "build":    { "repo": "owner/name", "commit": "abc1234" },
  "settings": { "profile": "series-x", "output": "1920x1080", "internal": "1920x1080",
                "raytracing": false, "upscaler": "none", "fpsCap": 30 },
  "scene":    "city-walk",
  "pc":       { "cpu": "…", "gpu": "…", "ramGB": 64, "driver": "…", "os": "…" },
  "capture":  { "tool": "ftlog", "source": "run.ftlog", "seconds": 300.0,
                "skippedSeconds": 30.0, "fromMark": null, "untilMark": null,
                "phase": "scene", "ftreport": "0.4.1" },
  "result":   { "frames": 6912, "avgFps": 23.04, "medianMs": 43.1, "p95Ms": 47.0,
                "p99Ms": 61.2, "low1Fps": 15.8, "stutters": 4, "freezes": 1, "maxMs": 230.2,
                "timeAt30Pct": 3.1, "timeAt60Pct": 0.0,
                "busy": { "frames": 6912, "cpuMs": 41.2, "gpuMs": 32.5,
                          "gpuBoundShare": 0.04, "limit": "cpu" } },
  "checks":   { "valid": true, "notes": [] }
}
```

A JSON Schema for this format is at [`docs/schema/report-0.1.schema.json`](docs/schema/report-0.1.schema.json), published at `https://leixner.github.io/frametime-report/schema/report-0.1.schema.json`.

### Definitions

The **measured window** is every frame after the skip point. The skip point is `--skip` seconds (default 30) after the start, or after the mark given with `--from-mark`. With `--until-mark`, the window ends at that mark: only frames that end at or before it count. `--max-seconds` can shorten the window further. An empty window is an error.

| Field | Definition |
|---|---|
| `capture.seconds` | Sum of `frame_ms` in the window ÷ 1000 |
| `frames` | Number of frames in the window |
| `avgFps` | `frames ÷ capture.seconds`. This is the time-weighted average, **not** the mean of per-frame fps values. |
| `medianMs` | Median frame time |
| `p95Ms`, `p99Ms` | Nearest-rank percentiles of frame time: sort ascending, take element `ceil(p/100 × n)`, counting from 1 |
| `low1Fps` | `1000 ÷ mean(the slowest ceil(1% × n) frame times)` |
| `stutters` | Number of frames longer than `2.5 × medianMs`: hitches relative to the run's own pace |
| `freezes` | Number of frames longer than 200 ms: hangs a player notices at any frame rate (the same threshold as KLBF's BM-v1) |
| `timeAt30Pct`, `timeAt60Pct` | Share of the window's time spent in frames of at most 1000/30 ms or 1000/60 ms (that is, at 30 or 60 fps or faster), in % of the summed frame time |
| `maxMs` | Longest frame |

### Cold boot

A cold-boot report covers start-up as a player sees it: from launch, black screen and shader compilation included. Set `capture.phase=cold-boot` and measure with `--skip 0` and no `--from-mark`; `settings.shaderCache` says whether the caches were empty.

- `capture.clockStart` is `launch` for a cold boot unless the log or `--set` says otherwise. `first-present` keeps the frames as they are.
- With a `launch` clock, the time before the first present (`capture.firstPresentMs`) goes in front of the frames as **one frame of that length**, and every time (marks, samples) moves with it, so 0 is the launch. It then counts toward `capture.seconds`, `avgFps`, `freezes`, `maxMs` and the time-at shares, like any other wait.
- A `launch` clock without `capture.firstPresentMs` can't be honoured: the report says so, is not valid, and leaves `clockStart` out.
- `capture.phase`, `capture.clockStart` and `capture.firstPresentMs` appear in the report's `capture` object only when known and valid. A wrong value (`cold_boot`, a negative time) is dropped with a note.

Sums run left to right in frame order. All values are rounded to 2 decimals, **half away from zero** (`0.125` → `0.13`). These rules make every implementation give the same digits.

### Usage (`result.usage`)

Present when the log has `# sample` lines inside the measured window (from the first window frame's start to the last window frame's end). For each sampled key, the median and the highest value of those samples:

| Field | From `# sample` key |
|---|---|
| `cpuPct` | `cpu` |
| `ramMB` | `ram` |
| `gpuPct` | `gpu` |
| `vramMB` | `vram` |
| `sysCpuPct` | `syscpu` |
| `sysRamPct` | `sysram` |

Each is `{ "median": …, "max": … }`; `samples` is the number of samples in the window.

### CPU or GPU bound (`result.busy`)

Present only when at least half of the frames in the window have **both** a CPU and a GPU busy time. These come from the frame log's `cpu_ms,gpu_ms` columns, or PresentMon 2.x `CPUBusy` and `GPUBusy` (or 1.x `MsGPUActive`, which has no CPU column, so it gives no analysis). Only frames with both values count:

| Field | Definition |
|---|---|
| `frames` | Frames with both busy times |
| `cpuMs`, `gpuMs` | Median CPU and GPU busy time |
| `gpuBoundShare` | Share of those frames where GPU busy ≥ CPU busy |
| `limit` | `pacing` if both medians are below 75% of the median frame time: a cap, vsync or waits set the pace. Otherwise `gpu` if `gpuBoundShare` ≥ 0.6, `cpu` if it is ≤ 0.4, else `mixed`. |

`pacing` is checked first. A run capped at 30 fps with both sides busy for only half of each frame isn't limited by either.

### Validity checks

`checks.valid` is `false`, with a note for each failed check, when:

- the window is shorter than `--min-seconds` (default 180);
- fewer than 100 frames remain;
- `--require` names a field that is missing. The default requires `settings.profile` and `scene`;
- `capture.phase`, `capture.clockStart`, `capture.firstPresentMs` or `settings.shaderCache` has a value outside the ones above;
- the clock starts at launch but `capture.firstPresentMs` is missing;
- a cold-boot run skips time or starts at a mark.

An invalid report is still written. It just shouldn't be counted as a result.

### Comparing reports

`compare` checks `settings.profile`, `settings.output`, `settings.internal`, `settings.raytracing`, `settings.upscaler`, `settings.fpsCap`, `settings.shaderCache`, `scene`, `capture.phase` and `capture.clockStart`:

- any of them set in both reports with different values: **not like for like**;
- otherwise, any of them set in only one report: **unknown**. It is never counted as like for like, and the reproduce check is skipped;
- otherwise (equal, or missing in both): **like for like**.

The start and end marks (`capture.fromMark`, `capture.untilMark`) are not compared on purpose: marker names differ between projects and valid runs. Use `scene` and `capture.phase` to say what was measured.

Two like-for-like reports on the same `build.commit` from different PCs or people **reproduce** each other when their `avgFps` are within 10% of each other.

## 3. Scene ids

Keep the list small, so results can be grouped:

| Scene | What it covers |
|---|---|
| `menu` | menus, front end |
| `interior` | indoors, on foot |
| `city-walk` | outdoors in a town or city, on foot, turning the camera |
| `city-drive` | driving through a town or city |
| `open-drive` | driving outside towns |
| `boat` | on water |
| `flight` | in the air |
| `chase` | heavy traffic and crowds, fast movement |
| `custom:<name>` | anything else |
