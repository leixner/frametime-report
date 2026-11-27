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
//! // ...or, if you measure them, with the frame's CPU and GPU busy times (ms),
//! // which lets the report say whether the frame rate is CPU- or GPU-bound:
//! ftlog::frame_busy(cpu_ms, gpu_ms);
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
//!
//! When recording is on, a short-lived background thread also writes the
//! hardware names once (`pc.cpu`, `pc.gpu`, `pc.vramGB`, `pc.ramGB`, `pc.os`,
//! `pc.driver`, `pc.cores`): model names only, nothing that identifies a person
//! or a machine.
//!
//! Optional usage sampling (Windows): set `FTLOG_SAMPLE=1` (every 500 ms) or
//! `FTLOG_SAMPLE=<ms>` (100 to 10000). A background thread then writes
//! `# sample` lines with this process's CPU % and memory, the system's CPU %
//! and memory load, GPU % and dedicated VRAM in use. The thread that calls
//! `frame()` does no extra work for it.
//!
//! On Windows the first `frame()` also writes `capture.firstPresentMs`: the
//! time from when this process was created to the first present, for
//! cold-boot reports (`ftreport.py --set capture.phase=cold-boot --skip 0`).

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
            // Hardware names and the optional usage sampler run on their own
            // threads: nothing extra happens on the thread that calls frame().
            // They wait for this initialisation to finish before writing.
            background::start();
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
    record(None);
}

/// Like [`frame`], plus how long the CPU and the GPU were busy on this frame,
/// in milliseconds. Typical sources: the CPU time of the thread that builds
/// and submits the frame, and GPU timestamp queries around its work. GPU
/// times that arrive a few frames late are fine: the report uses medians and
/// per-frame comparisons over minutes. Pass a negative or non-finite value
/// for a time you don't have.
pub fn frame_busy(cpu_ms: f64, gpu_ms: f64) {
    record(Some((cpu_ms, gpu_ms)));
}

fn busy_ms(v: f64) -> String {
    if v.is_finite() && v >= 0.0 {
        format!("{:.3}", v)
    } else {
        String::new()
    }
}

fn record(busy: Option<(f64, f64)>) {
    with(|r| {
        let now = Instant::now();
        let t = now.duration_since(r.start).as_secs_f64() * 1000.0;
        if let Some(prev) = r.last {
            let ms = now.duration_since(prev).as_secs_f64() * 1000.0;
            let _ = match busy {
                Some((cpu, gpu)) => writeln!(r.out, "{:.3},{:.3},{},{}", t, ms, busy_ms(cpu), busy_ms(gpu)),
                None => writeln!(r.out, "{:.3},{:.3}", t, ms),
            };
        } else if let Some(ms) = platform::since_launch_ms() {
            // once, on the first present
            let _ = writeln!(r.out, "# meta capture.firstPresentMs={:.1}", ms);
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

// ---------------------------------------------------------------------------
// Hardware names and usage sampling. Everything below runs on background
// threads; frame() never touches it.

/// Environment variable for usage sampling: `1` (every 500 ms) or an interval in ms.
pub const SAMPLE_ENV_VAR: &str = "FTLOG_SAMPLE";

fn sample_interval_ms() -> Option<u64> {
    let v = std::env::var(SAMPLE_ENV_VAR).ok()?;
    match v.trim() {
        "" | "0" | "off" | "false" => None,
        "1" | "on" | "true" => Some(500),
        ms => ms.parse::<u64>().ok().map(|ms| ms.clamp(100, 10_000)),
    }
}

fn write_sample(fields: &str) {
    if fields.is_empty() {
        return;
    }
    with(|r| {
        let t = r.start.elapsed().as_secs_f64() * 1000.0;
        let _ = writeln!(r.out, "# sample {:.3} {}", t, fields);
    });
}

fn common_hardware() -> Vec<(String, String)> {
    let mut v = Vec::new();
    if let Ok(n) = std::thread::available_parallelism() {
        v.push(("pc.cores".to_string(), n.get().to_string()));
    }
    v
}

fn gb(bytes: u64) -> String {
    format!("{}", (bytes as f64 / 1_073_741_824.0).round() as u64)
}

mod background {
    use std::thread;
    use std::time::Duration;

    pub(super) fn start() {
        let _ = thread::Builder::new().name("ftlog-hardware".into()).spawn(|| {
            for (key, value) in super::platform::hardware() {
                super::meta(&key, value);
            }
        });
        if !super::platform::SAMPLING {
            return;
        }
        if let Some(ms) = super::sample_interval_ms() {
            let _ = thread::Builder::new().name("ftlog-sample".into()).spawn(move || {
                let mut sampler = super::platform::Sampler::new();
                loop {
                    thread::sleep(Duration::from_millis(ms));
                    // collect outside the lock; the lock is held only to write one line
                    let fields = sampler.sample();
                    super::write_sample(&fields);
                }
            });
        }
    }
}

#[cfg(not(windows))]
mod platform {
    pub(super) const SAMPLING: bool = false;

    pub(super) fn since_launch_ms() -> Option<f64> {
        None
    }

    pub(super) fn hardware() -> Vec<(String, String)> {
        let mut v = super::common_hardware();
        v.push(("pc.os".to_string(), std::env::consts::OS.to_string()));
        v
    }

    pub(super) struct Sampler;
    impl Sampler {
        pub(super) fn new() -> Self {
            Sampler
        }
        pub(super) fn sample(&mut self) -> String {
            String::new()
        }
    }
}

#[cfg(windows)]
mod platform {
    //! Windows: registry for names, kernel32 for CPU and memory, PDH (the
    //! counters Task Manager uses) for GPU load and VRAM on any vendor.
    //! Everything beyond kernel32 is loaded at run time, so this file adds no
    //! link-time dependency.

    #[allow(unused_imports)]
    use std::convert::TryInto;
    use std::collections::HashMap;
    use std::ffi::c_void;
    use std::ptr::{null, null_mut};
    use std::time::Instant;

    pub(super) const SAMPLING: bool = true;

    type Handle = *mut c_void;

    #[repr(C)]
    #[derive(Default, Clone, Copy)]
    struct FileTime {
        low: u32,
        high: u32,
    }
    impl FileTime {
        fn ticks(self) -> u64 {
            ((self.high as u64) << 32) | self.low as u64
        }
    }

    #[repr(C)]
    #[derive(Default)]
    struct MemoryStatusEx {
        length: u32,
        load: u32,
        total_phys: u64,
        avail_phys: u64,
        total_page: u64,
        avail_page: u64,
        total_virtual: u64,
        avail_virtual: u64,
        avail_ext_virtual: u64,
    }

    #[repr(C)]
    #[derive(Default)]
    struct ProcessMemoryCountersEx {
        cb: u32,
        page_fault_count: u32,
        peak_working_set: usize,
        working_set: usize,
        quota_peak_paged: usize,
        quota_paged: usize,
        quota_peak_nonpaged: usize,
        quota_nonpaged: usize,
        pagefile: usize,
        peak_pagefile: usize,
        private_usage: usize,
    }

    #[repr(C)]
    struct PdhValue {
        status: u32,
        value: f64,
    }

    #[repr(C)]
    struct PdhItem {
        name: *const u16,
        value: PdhValue,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentProcess() -> Handle;
        fn GetProcessTimes(p: Handle, c: *mut FileTime, e: *mut FileTime, k: *mut FileTime, u: *mut FileTime) -> i32;
        fn GetSystemTimes(idle: *mut FileTime, kernel: *mut FileTime, user: *mut FileTime) -> i32;
        fn GetSystemTimeAsFileTime(t: *mut FileTime);
        fn GlobalMemoryStatusEx(m: *mut MemoryStatusEx) -> i32;
        fn LoadLibraryW(name: *const u16) -> Handle;
        fn GetProcAddress(module: Handle, name: *const u8) -> *mut c_void;
    }

    type GetProcessMemoryInfo = unsafe extern "system" fn(Handle, *mut ProcessMemoryCountersEx, u32) -> i32;
    type RegGetValueW = unsafe extern "system" fn(Handle, *const u16, *const u16, u32, *mut u32, *mut c_void, *mut u32) -> i32;
    type PdhOpenQueryW = unsafe extern "system" fn(*const u16, usize, *mut Handle) -> u32;
    type PdhAddEnglishCounterW = unsafe extern "system" fn(Handle, *const u16, usize, *mut Handle) -> u32;
    type PdhCollectQueryData = unsafe extern "system" fn(Handle) -> u32;
    type PdhGetFormattedCounterArrayW = unsafe extern "system" fn(Handle, u32, *mut u32, *mut u32, *mut c_void) -> u32;

    const HKEY_LOCAL_MACHINE: isize = 0x8000_0002u32 as i32 as isize;
    const RRF_RT_REG_SZ: u32 = 0x0000_0002;
    const RRF_RT_ANY: u32 = 0x0000_ffff;
    const PDH_FMT_DOUBLE: u32 = 0x0000_0200;
    const PDH_FMT_NOCAP100: u32 = 0x0000_8000;
    const PDH_MORE_DATA: u32 = 0x8000_07D2;

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    unsafe fn wide_to_string(mut p: *const u16) -> String {
        let mut v = Vec::new();
        while !p.is_null() && *p != 0 {
            v.push(*p);
            p = p.add(1);
        }
        String::from_utf16_lossy(&v)
    }

    fn load(dll: &str, name: &str) -> Option<*mut c_void> {
        let dll = wide(dll);
        let mut name = name.as_bytes().to_vec();
        name.push(0);
        unsafe {
            let module = LoadLibraryW(dll.as_ptr());
            if module.is_null() {
                return None;
            }
            let p = GetProcAddress(module, name.as_ptr());
            if p.is_null() {
                None
            } else {
                Some(p)
            }
        }
    }

    fn reg_raw(get: RegGetValueW, key: &str, value: &str, flags: u32) -> Option<Vec<u8>> {
        let (key, value) = (wide(key), wide(value));
        let hklm = HKEY_LOCAL_MACHINE as Handle;
        let mut size: u32 = 0;
        unsafe {
            if get(hklm, key.as_ptr(), value.as_ptr(), flags, null_mut(), null_mut(), &mut size) != 0 || size == 0 {
                return None;
            }
            let mut buf = vec![0u8; size as usize];
            if get(hklm, key.as_ptr(), value.as_ptr(), flags, null_mut(), buf.as_mut_ptr() as *mut c_void, &mut size) != 0 {
                return None;
            }
            buf.truncate(size as usize);
            Some(buf)
        }
    }

    fn reg_string(get: RegGetValueW, key: &str, value: &str) -> Option<String> {
        let b = reg_raw(get, key, value, RRF_RT_REG_SZ)?;
        let w: Vec<u16> = b
            .chunks_exact(2)
            .map(|c| u16::from_le_bytes([c[0], c[1]]))
            .take_while(|&c| c != 0)
            .collect();
        let s = String::from_utf16_lossy(&w).trim().to_string();
        if s.is_empty() {
            None
        } else {
            Some(s)
        }
    }

    fn reg_u64(get: RegGetValueW, key: &str, value: &str) -> Option<u64> {
        let b = reg_raw(get, key, value, RRF_RT_ANY)?;
        match b.len() {
            8 => Some(u64::from_le_bytes(b[..8].try_into().ok()?)),
            4 => Some(u32::from_le_bytes(b[..4].try_into().ok()?) as u64),
            _ => None,
        }
    }

    fn mem_status() -> Option<MemoryStatusEx> {
        let mut m = MemoryStatusEx::default();
        m.length = std::mem::size_of::<MemoryStatusEx>() as u32;
        if unsafe { GlobalMemoryStatusEx(&mut m) } != 0 {
            Some(m)
        } else {
            None
        }
    }

    pub(super) fn hardware() -> Vec<(String, String)> {
        let mut out = super::common_hardware();
        if let Some(p) = load("advapi32.dll", "RegGetValueW") {
            let get: RegGetValueW = unsafe { std::mem::transmute(p) };
            if let Some(cpu) = reg_string(get, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString") {
                out.push(("pc.cpu".into(), cpu));
            }
            // display adapters: take the one with the most dedicated memory
            let class = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}";
            let mut best: Option<(u64, String, Option<String>)> = None;
            for i in 0..16 {
                let key = format!(r"{}\{:04}", class, i);
                let name = match reg_string(get, &key, "DriverDesc") {
                    Some(n) => n,
                    None => continue,
                };
                let vram = reg_u64(get, &key, "HardwareInformation.qwMemorySize")
                    .or_else(|| reg_u64(get, &key, "HardwareInformation.MemorySize"))
                    .unwrap_or(0);
                if best.as_ref().map_or(true, |b| vram > b.0) {
                    best = Some((vram, name, reg_string(get, &key, "DriverVersion")));
                }
            }
            if let Some((vram, name, driver)) = best {
                out.push(("pc.gpu".into(), name));
                if vram > 0 {
                    out.push(("pc.vramGB".into(), super::gb(vram)));
                }
                if let Some(d) = driver {
                    out.push(("pc.driver".into(), d));
                }
            }
            // ProductName still says "Windows 10" on Windows 11, so go by the build number
            let nt = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion";
            if let Some(build) = reg_string(get, nt, "CurrentBuild") {
                let n: u32 = build.parse().unwrap_or(0);
                let name = if n >= 22000 {
                    "Windows 11"
                } else if n >= 10240 {
                    "Windows 10"
                } else {
                    "Windows"
                };
                let os = match reg_string(get, nt, "DisplayVersion") {
                    Some(d) => format!("{} (build {}, {})", name, build, d),
                    None => format!("{} (build {})", name, build),
                };
                out.push(("pc.os".into(), os));
            }
        }
        if let Some(m) = mem_status() {
            out.push(("pc.ramGB".into(), super::gb(m.total_phys)));
        }
        out
    }

    /// Milliseconds since this process was created (under Wine: since Wine created it).
    pub(super) fn since_launch_ms() -> Option<f64> {
        let (mut c, mut e, mut k, mut u, mut now) =
            (FileTime::default(), FileTime::default(), FileTime::default(), FileTime::default(), FileTime::default());
        if unsafe { GetProcessTimes(GetCurrentProcess(), &mut c, &mut e, &mut k, &mut u) } == 0 {
            return None;
        }
        unsafe { GetSystemTimeAsFileTime(&mut now) };
        let (start, now) = (c.ticks(), now.ticks());
        if start == 0 || now < start {
            return None;
        }
        Some((now - start) as f64 / 10_000.0) // 100 ns ticks
    }

    fn process_ticks() -> u64 {
        let (mut c, mut e, mut k, mut u) = (FileTime::default(), FileTime::default(), FileTime::default(), FileTime::default());
        unsafe { GetProcessTimes(GetCurrentProcess(), &mut c, &mut e, &mut k, &mut u) };
        k.ticks() + u.ticks()
    }

    fn system_ticks() -> (u64, u64) {
        let (mut i, mut k, mut u) = (FileTime::default(), FileTime::default(), FileTime::default());
        unsafe { GetSystemTimes(&mut i, &mut k, &mut u) };
        (i.ticks(), k.ticks() + u.ticks()) // kernel time includes idle time
    }

    struct Pdh {
        collect: PdhCollectQueryData,
        array: PdhGetFormattedCounterArrayW,
        query: Handle,
        engine: Handle,
        memory: Handle,
        buf: Vec<u64>,
    }

    impl Pdh {
        fn new() -> Option<Pdh> {
            unsafe {
                let open: PdhOpenQueryW = std::mem::transmute(load("pdh.dll", "PdhOpenQueryW")?);
                let add: PdhAddEnglishCounterW = std::mem::transmute(load("pdh.dll", "PdhAddEnglishCounterW")?);
                let collect: PdhCollectQueryData = std::mem::transmute(load("pdh.dll", "PdhCollectQueryData")?);
                let array: PdhGetFormattedCounterArrayW =
                    std::mem::transmute(load("pdh.dll", "PdhGetFormattedCounterArrayW")?);
                let mut query: Handle = null_mut();
                if open(null(), 0, &mut query) != 0 {
                    return None;
                }
                let (mut engine, mut memory): (Handle, Handle) = (null_mut(), null_mut());
                let engine_path = wide(r"\GPU Engine(*)\Utilization Percentage");
                let memory_path = wide(r"\GPU Adapter Memory(*)\Dedicated Usage");
                if add(query, engine_path.as_ptr(), 0, &mut engine) != 0 {
                    engine = null_mut();
                }
                if add(query, memory_path.as_ptr(), 0, &mut memory) != 0 {
                    memory = null_mut();
                }
                if engine.is_null() && memory.is_null() {
                    return None;
                }
                collect(query); // rate counters need a first reading
                Some(Pdh { collect, array, query, engine, memory, buf: Vec::new() })
            }
        }

        fn items(&mut self, counter: Handle) -> Option<Vec<(String, f64)>> {
            let fmt = PDH_FMT_DOUBLE | PDH_FMT_NOCAP100;
            let (mut size, mut count) = (0u32, 0u32);
            let r = unsafe { (self.array)(counter, fmt, &mut size, &mut count, null_mut()) };
            if r == 0 {
                return Some(Vec::new());
            }
            if r != PDH_MORE_DATA || size == 0 {
                return None;
            }
            let words = (size as usize + 7) / 8;
            if self.buf.len() < words {
                self.buf.resize(words, 0);
            }
            let r = unsafe { (self.array)(counter, fmt, &mut size, &mut count, self.buf.as_mut_ptr() as *mut c_void) };
            if r != 0 {
                return None;
            }
            let items = unsafe { std::slice::from_raw_parts(self.buf.as_ptr() as *const PdhItem, count as usize) };
            Some(
                items
                    .iter()
                    .filter(|it| it.value.status <= 1 && it.value.value.is_finite())
                    .map(|it| (unsafe { wide_to_string(it.name) }, it.value.value))
                    .collect(),
            )
        }

        /// (GPU %, dedicated VRAM in MB) like Task Manager: per engine, the sum
        /// over all processes; the GPU figure is the busiest engine.
        fn sample(&mut self) -> (Option<f64>, Option<f64>) {
            if unsafe { (self.collect)(self.query) } != 0 {
                return (None, None);
            }
            let mut gpu = None;
            if !self.engine.is_null() {
                if let Some(items) = self.items(self.engine) {
                    let mut per_engine: HashMap<String, f64> = HashMap::new();
                    for (name, v) in items {
                        let key = match name.find("luid_") {
                            Some(i) => name[i..].to_string(),
                            None => name,
                        };
                        *per_engine.entry(key).or_insert(0.0) += v;
                    }
                    if !per_engine.is_empty() {
                        gpu = Some(per_engine.values().cloned().fold(0.0, f64::max).min(100.0));
                    }
                }
            }
            let mut vram = None;
            if !self.memory.is_null() {
                if let Some(items) = self.items(self.memory) {
                    if !items.is_empty() {
                        vram = Some(items.iter().map(|i| i.1).fold(0.0, f64::max) / 1_048_576.0);
                    }
                }
            }
            (gpu, vram)
        }
    }

    pub(super) struct Sampler {
        cores: f64,
        last: Instant,
        process: u64,
        system: (u64, u64),
        memory_info: Option<GetProcessMemoryInfo>,
        pdh: Option<Pdh>,
    }

    fn push(out: &mut String, key: &str, v: f64) {
        if v.is_finite() {
            if !out.is_empty() {
                out.push(' ');
            }
            out.push_str(&format!("{}={:.1}", key, v));
        }
    }

    impl Sampler {
        pub(super) fn new() -> Self {
            let cores = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1) as f64;
            let memory_info = load("kernel32.dll", "K32GetProcessMemoryInfo")
                .map(|p| unsafe { std::mem::transmute::<*mut c_void, GetProcessMemoryInfo>(p) });
            Sampler {
                cores,
                last: Instant::now(),
                process: process_ticks(),
                system: system_ticks(),
                memory_info,
                pdh: Pdh::new(),
            }
        }

        /// One `# sample` line's fields: cpu (this process, % of all cores),
        /// ram (this process, MB private), syscpu (%), sysram (memory load %),
        /// gpu (%), vram (dedicated, MB).
        pub(super) fn sample(&mut self) -> String {
            let mut out = String::new();
            let now = Instant::now();
            let wall = now.duration_since(self.last).as_secs_f64();
            self.last = now;

            let p = process_ticks();
            if wall > 0.0 {
                let cpu = p.saturating_sub(self.process) as f64 / 1e7 / wall / self.cores * 100.0;
                push(&mut out, "cpu", cpu.min(100.0));
            }
            self.process = p;

            if let Some(get) = self.memory_info {
                let mut c = ProcessMemoryCountersEx::default();
                c.cb = std::mem::size_of::<ProcessMemoryCountersEx>() as u32;
                if unsafe { get(GetCurrentProcess(), &mut c, c.cb) } != 0 {
                    push(&mut out, "ram", c.private_usage as f64 / 1_048_576.0);
                }
            }

            let (idle, total) = system_ticks();
            let (di, dt) = (idle.saturating_sub(self.system.0), total.saturating_sub(self.system.1));
            if dt > 0 {
                push(&mut out, "syscpu", ((1.0 - di as f64 / dt as f64) * 100.0).max(0.0).min(100.0));
            }
            self.system = (idle, total);

            if let Some(m) = mem_status() {
                push(&mut out, "sysram", m.load as f64);
            }

            if let Some(pdh) = self.pdh.as_mut() {
                let (gpu, vram) = pdh.sample();
                if let Some(g) = gpu {
                    push(&mut out, "gpu", g);
                }
                if let Some(v) = vram {
                    push(&mut out, "vram", v);
                }
            }
            out
        }
    }
}
