#!/usr/bin/env python3
"""ftreport: turn a frame-time log into a comparable report.

Reads:
  * frame logs written by the ftlog recorders (frametime-log v1, see SPEC.md)
  * PresentMon CSV (1.x "MsBetweenPresents" or 2.x "FrameTime", with CPUBusy/GPUBusy)
  * CapFrameX CSV exports (PresentMon columns)

Commands:
  summarize LOG [options]     print a summary and write a JSON report
  compare REPORT.json ...     compare reports side by side
  validate REPORT.json ...    check reports against the format

Standard library only; Python 3.8 or newer.

The browser page (docs/ftreport-core.js) implements the same arithmetic step by
step; tests/test_parity.py checks that both produce identical reports.
"""

import argparse
import csv
import io
import json
import math
import os
import re
import sys

VERSION = "0.3.0"
FORMAT = "frametime-report/0.1"
LOG_MAGIC = "# frametime-log v1"
# Every setting that changes the frame rate. A key set in one report and missing
# in the other counts as a difference: unknown is not the same as equal.
LIKE_FOR_LIKE_KEYS = (
    "settings.profile",
    "settings.output",
    "settings.internal",
    "settings.raytracing",
    "settings.upscaler",
    "settings.fpsCap",
    "scene",
)
FRAME_COLUMNS = ("MsBetweenPresents", "FrameTime", "msBetweenPresents")
CPU_BUSY_COLUMNS = ("CPUBusy",)
GPU_BUSY_COLUMNS = ("GPUBusy", "MsGPUActive", "msGPUActive")
APP_COLUMNS = ("Application", "ProcessName")
SWAPCHAIN_COLUMNS = ("SwapChainAddress", "SwapChain")
STUTTER_FACTOR = 2.5
FREEZE_MS = 200.0         # same threshold as KLBF's BM-v1 "freezes"
MS_AT_30 = 1000.0 / 30.0  # frames this fast or faster count toward "time at >= 30 fps"
MS_AT_60 = 1000.0 / 60.0
# "# sample" keys -> report keys under result.usage
SAMPLE_KEYS = (
    ("cpu", "cpuPct"),
    ("ram", "ramMB"),
    ("gpu", "gpuPct"),
    ("vram", "vramMB"),
    ("syscpu", "sysCpuPct"),
    ("sysram", "sysRamPct"),
)
REPRODUCE_TOLERANCE = 0.10
BUSY_MIN_SHARE = 0.5      # busy analysis needs busy times on half of the window's frames
PACING_FACTOR = 0.75      # neither CPU nor GPU busy for 75% of the frame: paced or capped
GPU_BOUND_AT = 0.6
CPU_BOUND_AT = 0.4

NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
INT_RE = re.compile(r"^[+-]?\d+$")
LINE_RE = re.compile(r"\r\n|\r|\n")


class FtError(Exception):
    pass


# --------------------------------------------------------------------------
# numbers: identical rules in ftreport-core.js

def r2(x):
    """Round to 2 decimals, half away from zero."""
    v = math.floor(abs(x) * 100 + 0.5) / 100
    return v if x >= 0 else -v


def fmt0(x):
    """Whole number for messages, half away from zero."""
    return "%d" % math.floor(x + 0.5)


def num(text):
    """A plain decimal number, or None. No 'inf', 'nan', '1_000' or hex."""
    t = (text or "").strip()
    if not NUMBER_RE.match(t):
        return None
    v = float(t)
    return v if math.isfinite(v) else None


def total(values):
    """Left-to-right sum, the same operation order as JavaScript."""
    s = 0.0
    for v in values:
        s += v
    return s


def median(sorted_vals):
    n = len(sorted_vals)
    if n % 2:
        return sorted_vals[n // 2]
    return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2


def nearest_rank(sorted_vals, p):
    n = len(sorted_vals)
    k = max(1, math.ceil(p / 100.0 * n))
    return sorted_vals[min(k, n) - 1]


def split_lines(text):
    lines = LINE_RE.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


# --------------------------------------------------------------------------
# values and dotted keys

def parse_value(text):
    """Turn metadata text into bool / int / float where it clearly is one."""
    t = text.strip()
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if INT_RE.match(t):
        return int(t)
    v = num(t)
    return v if v is not None else t


def set_dotted(d, key, value):
    parts = [p for p in key.strip().split(".") if p]
    if not parts:
        raise FtError("empty metadata key")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def get_dotted(d, key):
    cur = d
    for p in key.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


# --------------------------------------------------------------------------
# readers

class Capture:
    """Frames with their end times, optional CPU/GPU busy times, metadata and marks."""

    def __init__(self, tool):
        self.tool = tool
        self.frames, self.ends, self.cpu, self.gpu = [], [], [], []
        self.meta, self.marks, self.notes = {}, {}, []
        self.samples = []   # (t_ms, {key: value}) from "# sample" lines

    def add(self, ms, end=None, cpu=None, gpu=None):
        if end is None:
            end = (self.ends[-1] if self.ends else 0.0) + ms
        self.frames.append(ms)
        self.ends.append(end)
        self.cpu.append(cpu)
        self.gpu.append(gpu)


def read_text(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        return f.read()


def detect_format(text):
    first = next((l for l in split_lines(text) if l.strip()), "")
    if first.strip().lower() == LOG_MAGIC:
        return "ftlog"
    head = text[:20000]
    if any(c in head for c in FRAME_COLUMNS):
        return "presentmon"
    raise FtError("unknown input format (expected '%s' or a PresentMon/CapFrameX CSV)" % LOG_MAGIC)


def busy_value(text):
    v = num(text)
    return v if v is not None and v >= 0 else None


def read_ftlog(text):
    lines = split_lines(text)
    if not lines or lines[0].strip().lower() != LOG_MAGIC:
        raise FtError("not a frametime-log v1 file")
    cap = Capture("ftlog")
    for no, raw in enumerate(lines[1:], start=2):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line[1:].strip()
            if body.startswith("meta "):
                kv = body[5:]
                if "=" not in kv:
                    raise FtError("line %d: meta needs key=value" % no)
                k, v = kv.split("=", 1)
                set_dotted(cap.meta, k, parse_value(v))
            elif body.startswith("mark "):
                bits = body[5:].split(None, 1)
                if len(bits) != 2:
                    raise FtError("line %d: mark needs '<t_ms> <label>'" % no)
                t = num(bits[0])
                if t is None:
                    raise FtError("line %d: bad mark time" % no)
                label = bits[1].strip()
                if label not in cap.marks:
                    cap.marks[label] = t
            elif body.startswith("sample "):
                bits = body[7:].split()
                t = num(bits[0]) if bits else None
                if t is None:
                    raise FtError("line %d: bad sample time" % no)
                values = {}
                for kv in bits[1:]:
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        x = num(v)
                        if x is not None and x >= 0:
                            values[k] = x
                cap.samples.append((t, values))
            continue
        if line.lower().startswith("t_ms"):
            continue
        parts = line.split(",")
        if len(parts) >= 2:
            t, ms = num(parts[0]), num(parts[1])
            if t is None or ms is None:
                raise FtError("line %d: expected '<t_ms>,<frame_ms>[,<cpu_ms>,<gpu_ms>]'" % no)
        else:
            t, ms = None, num(parts[0])
            if ms is None:
                raise FtError("line %d: expected '<t_ms>,<frame_ms>'" % no)
        if ms <= 0:
            continue
        cpu = busy_value(parts[2]) if len(parts) > 2 else None
        gpu = busy_value(parts[3]) if len(parts) > 3 else None
        cap.add(ms, t, cpu, gpu)
    return cap


def csv_rows(lines):
    return list(csv.reader(io.StringIO("\n".join(lines))))


def read_presentmon(text, process=None, column=None):
    lines = split_lines(text)
    start = None
    for i, l in enumerate(lines):
        if any(c in l for c in FRAME_COLUMNS) or (column and column in l):
            start = i
            break
    if start is None:
        raise FtError("no PresentMon header row found")
    rows = csv_rows(lines[start:])
    fields = [f.strip() for f in rows[0]] if rows else []
    col = column if column else next((c for c in FRAME_COLUMNS if c in fields), None)
    if not col or col not in fields:
        raise FtError("frame-time column not found (have: %s)" % ", ".join(fields[:12]))

    def index_of(names):
        for n in names:
            if n in fields:
                return fields.index(n)
        return None

    i_ms = fields.index(col)
    i_app, i_sc = index_of(APP_COLUMNS), index_of(SWAPCHAIN_COLUMNS)
    i_cpu, i_gpu = index_of(CPU_BUSY_COLUMNS), index_of(GPU_BUSY_COLUMNS)

    def cell(row, i):
        return row[i] if i is not None and i < len(row) else ""

    data = []
    for row in rows[1:]:
        ms = num(cell(row, i_ms))
        if ms is None or ms <= 0:
            continue
        data.append((cell(row, i_app), cell(row, i_sc), ms,
                     busy_value(cell(row, i_cpu)), busy_value(cell(row, i_gpu))))
    if not data:
        raise FtError("no frame rows in column %s" % col)

    cap = Capture("presentmon:" + col)
    if i_app is not None:
        apps = count_in_order(d[0] for d in data)
        if process:
            want = process.lower()
            data = [d for d in data if d[0].lower() == want]
            if not data:
                raise FtError("process %r not in log (have: %s)" % (process, ", ".join(sorted(apps))))
        elif len(apps) > 1:
            pick = most_common(apps)
            data = [d for d in data if d[0] == pick]
            cap.notes.append("several processes in log; used %s (most frames), pick one with --process" % pick)
    if i_sc is not None:
        chains = count_in_order(d[1] for d in data)
        if len(chains) > 1:
            pick = most_common(chains)
            data = [d for d in data if d[1] == pick]
            cap.notes.append("several swap chains; used the one with most frames")

    for _, _, ms, cpu, gpu in data:
        cap.add(ms, None, cpu, gpu)
    if i_app is not None and data[0][0]:
        cap.meta["app"] = data[0][0]
    return cap


def count_in_order(items):
    counts = {}
    for x in items:
        counts[x] = counts.get(x, 0) + 1
    return counts


def most_common(counts):
    best, best_n = None, -1
    for k, n in counts.items():   # insertion order: ties go to the first seen
        if n > best_n:
            best, best_n = k, n
    return best


# --------------------------------------------------------------------------
# window and statistics

def mark_time(cap, label):
    if label not in cap.marks:
        raise FtError("mark %r not in log (have: %s)" % (label, ", ".join(sorted(cap.marks)) or "none"))
    return cap.marks[label]


def window(cap, skip_s, from_mark, max_s, until_mark=None):
    """Indices of the frames in the measured window."""
    start_ms = mark_time(cap, from_mark) if from_mark else 0.0
    start_ms += skip_s * 1000.0
    end_ms = mark_time(cap, until_mark) if until_mark else None
    idx = [i for i in range(len(cap.frames))
           if cap.ends[i] - cap.frames[i] >= start_ms - 1e-9 and (end_ms is None or cap.ends[i] <= end_ms + 1e-9)]
    if max_s:
        out, acc = [], 0.0
        for i in idx:
            if acc + cap.frames[i] > max_s * 1000.0 + 1e-9:
                break
            acc += cap.frames[i]
            out.append(i)
        idx = out
    return idx


def compute(frames):
    n = len(frames)
    if n == 0:
        raise FtError("no frames in the measured window")
    sum_ms = total(frames)
    s = sorted(frames)
    med = median(s)
    k = max(1, math.ceil(0.01 * n))
    worst = s[n - k:]
    stutters = freezes = 0
    at30 = at60 = 0.0
    for x in frames:
        if x > STUTTER_FACTOR * med:
            stutters += 1
        if x > FREEZE_MS:
            freezes += 1
        if x <= MS_AT_30:
            at30 += x
            if x <= MS_AT_60:
                at60 += x
    return {
        "frames": n,
        "avgFps": r2(n / (sum_ms / 1000.0)),
        "medianMs": r2(med),
        "p95Ms": r2(nearest_rank(s, 95)),
        "p99Ms": r2(nearest_rank(s, 99)),
        "low1Fps": r2(1000.0 / (total(worst) / k)),
        "stutters": stutters,
        "freezes": freezes,
        "maxMs": r2(s[-1]),
        "timeAt30Pct": r2(at30 / sum_ms * 100.0),
        "timeAt60Pct": r2(at60 / sum_ms * 100.0),
    }, sum_ms / 1000.0


def summarize_usage(samples, start_ms, end_ms):
    """Median and peak of each sampled value within the window. None without samples."""
    inside = [v for t, v in samples if start_ms - 1e-9 <= t <= end_ms + 1e-9]
    if not inside:
        return None
    usage = {"samples": len(inside)}
    for key, name in SAMPLE_KEYS:
        vals = sorted(v[key] for v in inside if key in v)
        if vals:
            usage[name] = {"median": r2(median(vals)), "max": r2(vals[-1])}
    return usage


def analyze_busy(frames, cpu, gpu):
    """CPU vs GPU busy time per frame -> what limits the frame rate. None without data."""
    both = [i for i in range(len(frames)) if cpu[i] is not None and gpu[i] is not None]
    if not frames or len(both) < BUSY_MIN_SHARE * len(frames):
        return None
    c = sorted(cpu[i] for i in both)
    g = sorted(gpu[i] for i in both)
    f = sorted(frames[i] for i in both)
    cpu_med, gpu_med, frame_med = median(c), median(g), median(f)
    gpu_first = 0
    for i in both:
        if gpu[i] >= cpu[i]:
            gpu_first += 1
    share = gpu_first / len(both)
    if max(cpu_med, gpu_med) < PACING_FACTOR * frame_med:
        limit = "pacing"
    elif share >= GPU_BOUND_AT:
        limit = "gpu"
    elif share <= CPU_BOUND_AT:
        limit = "cpu"
    else:
        limit = "mixed"
    return {
        "frames": len(both),
        "cpuMs": r2(cpu_med),
        "gpuMs": r2(gpu_med),
        "gpuBoundShare": r2(share),
        "limit": limit,
    }


# --------------------------------------------------------------------------
# this computer's hardware names (--detect-pc)

def _gb(nbytes):
    """Whole GB, rounded half up: the same rule as the recorders."""
    return int(math.floor(nbytes / 1073741824.0 + 0.5))


def detect_pc():
    """Model names of this computer: CPU, GPU and VRAM, driver, RAM, OS, cores.

    Model names only: nothing that identifies a person or a machine. Windows reads
    the same registry keys as the recorders; elsewhere only what is easy to get.
    """
    import platform
    pc = {}
    n = os.cpu_count()
    if n:
        pc["cores"] = n
    if sys.platform == "win32":
        import ctypes
        import winreg

        def reg(path, name):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                    return winreg.QueryValueEx(key, name)[0]
            except OSError:
                return None

        def as_int(v):
            if isinstance(v, int):
                return v
            if isinstance(v, (bytes, bytearray)) and len(v) in (4, 8):
                return int.from_bytes(bytes(v), "little")
            return 0

        cpu = reg(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
        if cpu and str(cpu).strip():
            pc["cpu"] = str(cpu).strip()
        best = None
        for i in range(16):
            path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\%04d" % i
            name = reg(path, "DriverDesc")
            if not name or not str(name).strip():
                continue
            vram = as_int(reg(path, "HardwareInformation.qwMemorySize")) or as_int(reg(path, "HardwareInformation.MemorySize"))
            if best is None or vram > best[0]:
                best = (vram, str(name).strip(), reg(path, "DriverVersion"))
        if best:
            pc["gpu"] = best[1]
            if best[0]:
                pc["vramGB"] = _gb(best[0])
            if best[2]:
                pc["driver"] = str(best[2]).strip()
        nt = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        build = reg(nt, "CurrentBuild")
        if build:
            b = int(build) if str(build).isdigit() else 0
            name = "Windows 11" if b >= 22000 else "Windows 10" if b >= 10240 else "Windows"
            display = reg(nt, "DisplayVersion")
            pc["os"] = "%s (build %s, %s)" % (name, build, display) if display else "%s (build %s)" % (name, build)

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MemoryStatusEx()
        m.dwLength = ctypes.sizeof(MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            pc["ramGB"] = _gb(m.ullTotalPhys)
    else:
        pc["os"] = "%s %s" % (platform.system(), platform.release())
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        pc["cpu"] = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
        try:
            pc["ramGB"] = _gb(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except (ValueError, OSError, AttributeError):
            pass
    return pc


# --------------------------------------------------------------------------
# report

def default_options():
    return {
        "format": "auto", "process": None, "column": None, "skip": 30.0, "fromMark": None, "untilMark": None,
        "maxSeconds": 0.0, "minSeconds": 180.0, "require": ["settings.profile", "scene"], "set": [],
        "detectPc": False,
    }


def build_report_from_text(text, source, opts):
    """The whole pipeline; ftreport-core.js buildReport() mirrors it."""
    o = default_options()
    o.update({k: v for k, v in opts.items() if v is not None})
    fmt = o["format"] if o["format"] != "auto" else detect_format(text)
    if fmt == "ftlog":
        cap = read_ftlog(text)
    elif fmt in ("presentmon", "capframex"):
        cap = read_presentmon(text, o["process"], o["column"])
        if fmt == "capframex":
            cap.tool = cap.tool.replace("presentmon", "capframex", 1)
    else:
        raise FtError("unknown format %s" % fmt)

    if o["detectPc"]:
        # this PC's names fill in only what the log doesn't already say; --set still wins
        for k, v in detect_pc().items():
            if get_dotted(cap.meta, "pc." + k) is None:
                set_dotted(cap.meta, "pc." + k, v)

    for item in o["set"]:
        if "=" not in item:
            raise FtError("--set needs key=value, got %r" % item)
        k, v = item.split("=", 1)
        set_dotted(cap.meta, k, parse_value(v))

    if fmt != "ftlog" and (o["fromMark"] or o["untilMark"]):
        raise FtError("--from-mark and --until-mark only work with frame logs")
    idx = window(cap, o["skip"], o["fromMark"], o["maxSeconds"], o["untilMark"])
    if not idx:
        raise FtError("no frames in the measured window")
    frames = [cap.frames[i] for i in idx]
    result, seconds = compute(frames)
    busy = analyze_busy(frames, [cap.cpu[i] for i in idx], [cap.gpu[i] for i in idx])
    if busy:
        result["busy"] = busy
    usage = summarize_usage(cap.samples, cap.ends[idx[0]] - cap.frames[idx[0]], cap.ends[idx[-1]])
    if usage:
        result["usage"] = usage

    report = {"format": FORMAT}
    for k in ("app", "build", "settings", "scene", "pc"):
        if k in cap.meta:
            report[k] = cap.meta[k]
    for k, v in cap.meta.items():
        if k not in report and k not in ("format", "capture", "result", "checks"):
            report[k] = v
    report["capture"] = {
        "tool": cap.tool,
        "source": source,
        "seconds": r2(seconds),
        "skippedSeconds": o["skip"],
        "fromMark": o["fromMark"],
        "untilMark": o["untilMark"],
        "ftreport": VERSION,
    }
    report["result"] = result

    problems = []
    if seconds < o["minSeconds"]:
        problems.append("window is %s s, shorter than the %s s minimum" % (fmt0(seconds), fmt0(o["minSeconds"])))
    if result["frames"] < 100:
        problems.append("only %d frames in the window" % result["frames"])
    for key in o["require"]:
        key = key.strip()
        if key and get_dotted(report, key) is None:
            problems.append("missing %s (add it with --set %s=...)" % (key, key))
    report["checks"] = {"valid": not problems, "notes": problems + cap.notes}
    return report


def options_from_args(args):
    return {
        "format": args.format, "process": args.process, "column": args.column, "skip": args.skip,
        "fromMark": args.from_mark, "untilMark": args.until_mark,
        "maxSeconds": args.max_seconds, "minSeconds": args.min_seconds, "detectPc": args.detect_pc,
        "require": [k for k in args.require.split(",") if k.strip()] if args.require else [],
        "set": args.set or [],
    }


def build_report(args):
    return build_report_from_text(read_text(args.log), os.path.basename(args.log), options_from_args(args))


# --------------------------------------------------------------------------
# commands

def fmt_settings(r):
    s = r.get("settings") or {}
    bits = [str(s.get("profile", "?")), str(s.get("output", "?"))]
    if s.get("raytracing") is True:
        bits.append("RT")
    if s.get("upscaler") not in (None, "none"):
        bits.append(str(s.get("upscaler")))
    return " ".join(bits)


def busy_line(b):
    what = {"gpu": "GPU-bound", "cpu": "CPU-bound", "mixed": "mixed CPU/GPU",
            "pacing": "paced or capped (neither busy most of the frame)"}[b["limit"]]
    return "cpu busy %.2f ms | gpu busy %.2f ms | gpu >= cpu on %s%% of frames | %s" % (
        b["cpuMs"], b["gpuMs"], fmt0(b["gpuBoundShare"] * 100), what)


def usage_line(u):
    def part(label, key, unit):
        v = u.get(key)
        return "%s %s%s (peak %s)" % (label, fmt0(v["median"]), unit, fmt0(v["max"])) if v else None
    bits = [part("cpu", "cpuPct", "%"), part("ram", "ramMB", " MB"), part("gpu", "gpuPct", "%"),
            part("vram", "vramMB", " MB"), part("system cpu", "sysCpuPct", "%"), part("system ram", "sysRamPct", "%")]
    return "usage (median, %d samples): %s" % (u["samples"], " | ".join(b for b in bits if b))


def pc_line(pc):
    gpu = str(pc.get("gpu", "?")) + (" %s GB" % pc["vramGB"] if "vramGB" in pc else "")
    bits = [str(pc.get("cpu", "?")), gpu]
    if "ramGB" in pc:
        bits.append("%s GB RAM" % pc["ramGB"])
    if "os" in pc:
        bits.append(str(pc["os"]))
    return "pc: " + " | ".join(bits)


def cmd_summarize(args):
    report = build_report(args)
    res = report["result"]
    print("%s  |  %s  |  scene %s" % (report["capture"]["source"], fmt_settings(report), report.get("scene", "?")))
    print("  %d frames over %.1f s (skipped %s s%s%s)" % (
        res["frames"], report["capture"]["seconds"], fmt0(args.skip),
        ", from mark %s" % args.from_mark if args.from_mark else "",
        ", until mark %s" % args.until_mark if args.until_mark else ""))
    print("  avg %.2f fps | median %.2f ms | p95 %.2f ms | p99 %.2f ms | 1%% low %.2f fps | max %.2f ms" % (
        res["avgFps"], res["medianMs"], res["p95Ms"], res["p99Ms"], res["low1Fps"], res["maxMs"]))
    print("  stutters %d (> %s x median) | freezes %d (> %s ms) | time at >= 30 fps %s%% | >= 60 fps %s%%" % (
        res["stutters"], STUTTER_FACTOR, res["freezes"], fmt0(FREEZE_MS), fmt0(res["timeAt30Pct"]),
        fmt0(res["timeAt60Pct"])))
    if "busy" in res:
        print("  " + busy_line(res["busy"]))
    if "usage" in res:
        print("  " + usage_line(res["usage"]))
    if report.get("pc"):
        print("  " + pc_line(report["pc"]))
    print("  valid: %s" % ("yes" if report["checks"]["valid"] else "NO"))
    for n in report["checks"]["notes"]:
        print("  note: %s" % n)
    out = args.json or os.path.splitext(args.log)[0] + ".report.json"
    if out != "-":
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print("  report: %s" % out)
    else:
        json.dump(report, sys.stdout, indent=2)
        print()
    return 0


def load_report(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise FtError("%s: not JSON (%s)" % (path, e))


def validate_report(r):
    errs = []
    if r.get("format") != FORMAT:
        errs.append("format is %r, expected %r" % (r.get("format"), FORMAT))
    res = r.get("result")
    if not isinstance(res, dict):
        errs.append("result missing")
    else:
        for k in ("frames", "avgFps", "medianMs", "p95Ms", "p99Ms", "low1Fps", "stutters"):
            if not isinstance(res.get(k), (int, float)) or isinstance(res.get(k), bool):
                errs.append("result.%s missing or not a number" % k)
        b = res.get("busy")
        if b is not None and (not isinstance(b, dict) or b.get("limit") not in ("gpu", "cpu", "mixed", "pacing")):
            errs.append("result.busy is malformed")
    cap = r.get("capture")
    if not isinstance(cap, dict) or not isinstance(cap.get("seconds"), (int, float)):
        errs.append("capture.seconds missing")
    for k in ("settings.profile", "scene"):
        if get_dotted(r, k) is None:
            errs.append("%s missing (recommended)" % k)
    return errs


def cmd_validate(args):
    bad = 0
    for p in args.reports:
        errs = validate_report(load_report(p))
        hard = [e for e in errs if "recommended" not in e]
        print("%s: %s" % (p, "ok" if not errs else ("warnings" if not hard else "INVALID")))
        for e in errs:
            print("  - %s" % e)
        bad += bool(hard)
    return 1 if bad else 0


def cmd_compare(args):
    reps = [(os.path.basename(p), load_report(p)) for p in args.reports]
    head = ("report", "commit", "settings", "scene", "avg fps", "p95 ms", "1% low", "stutters", "freezes",
            "limit", "gpu %", "secs", "valid")
    rows = []
    for name, r in reps:
        res = r.get("result", {})
        rows.append((
            name,
            str(get_dotted(r, "build.commit") or "-")[:10],
            fmt_settings(r),
            str(r.get("scene", "?")),
            "%.2f" % res.get("avgFps", float("nan")),
            "%.2f" % res.get("p95Ms", float("nan")),
            "%.2f" % res.get("low1Fps", float("nan")),
            str(res.get("stutters", "?")),
            str(res.get("freezes", "-")),
            str(get_dotted(res, "busy.limit") or "-"),
            fmt0(get_dotted(res, "usage.gpuPct.median")) if get_dotted(res, "usage.gpuPct.median") is not None else "-",
            fmt0(get_dotted(r, "capture.seconds") or 0),
            "yes" if get_dotted(r, "checks.valid") else "no",
        ))
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(head)]
    line = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths))
    print(line(head))
    print(line(["-" * w for w in widths]))
    for row in rows:
        print(line(row))

    base_name, base = reps[0]
    print()
    for name, r in reps[1:]:
        diff = [k for k in LIKE_FOR_LIKE_KEYS if get_dotted(r, k) != get_dotted(base, k)]
        a, b = get_dotted(base, "result.avgFps"), get_dotted(r, "result.avgFps")
        if diff:
            print("%s vs %s: NOT like for like (differs in %s)" % (name, base_name, ", ".join(diff)))
            continue
        change = (b - a) / a * 100 if a else float("nan")
        same_commit = get_dotted(r, "build.commit") and get_dotted(r, "build.commit") == get_dotted(base, "build.commit")
        msg = "%s vs %s: like for like, avg %+.1f%%" % (name, base_name, change)
        if same_commit:
            within = abs(b - a) <= REPRODUCE_TOLERANCE * max(a, b)
            msg += ", same commit: %s" % ("reproduces (within 10%)" if within else "does NOT reproduce (over 10% apart)")
        print(msg)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ftreport", description="Comparable frame-time reports (frametime-report/0.1).")
    ap.add_argument("--version", action="version", version="ftreport " + VERSION)
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("summarize", help="summarize a frame log or PresentMon/CapFrameX CSV")
    s.add_argument("log")
    s.add_argument("--format", default="auto", choices=("auto", "ftlog", "presentmon", "capframex"))
    s.add_argument("--process", help="PresentMon: application name to keep (e.g. app.exe)")
    s.add_argument("--column", help="PresentMon: frame-time column to use")
    s.add_argument("--skip", type=float, default=30.0, help="seconds to drop at the start (default 30)")
    s.add_argument("--from-mark", help="frame logs: start counting at this mark (plus --skip)")
    s.add_argument("--until-mark", help="frame logs: stop counting at this mark")
    s.add_argument("--detect-pc", action="store_true",
                   help="fill in pc.* from this computer where the log doesn't say (model names only)")
    s.add_argument("--max-seconds", type=float, default=0.0, help="cap the measured window")
    s.add_argument("--min-seconds", type=float, default=180.0, help="shorter windows are invalid (default 180)")
    s.add_argument("--require", default="settings.profile,scene", help="comma-separated fields a valid report needs")
    s.add_argument("--set", action="append", metavar="KEY=VALUE", help="add metadata, e.g. scene=city-walk pc.gpu='RX 7900 XTX'")
    s.add_argument("--json", help="report path ('-' for stdout; default <log>.report.json)")
    s.set_defaults(fn=cmd_summarize)

    c = sub.add_parser("compare", help="compare reports; the first is the baseline")
    c.add_argument("reports", nargs="+")
    c.set_defaults(fn=cmd_compare)

    v = sub.add_parser("validate", help="check reports against the format")
    v.add_argument("reports", nargs="+")
    v.set_defaults(fn=cmd_validate)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 2
    try:
        return args.fn(args)
    except (FtError, OSError) as e:
        print("ftreport: error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
