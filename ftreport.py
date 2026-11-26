#!/usr/bin/env python3
"""ftreport: turn a frame-time log into a comparable report.

Reads:
  * frame logs written by the ftlog recorders (frametime-log v1, see SPEC.md)
  * PresentMon CSV (1.x "MsBetweenPresents" or 2.x "FrameTime")
  * CapFrameX CSV exports (PresentMon columns)

Commands:
  summarize LOG [options]     print a summary and write a JSON report
  compare REPORT.json ...     compare reports side by side
  validate REPORT.json ...    check reports against the format

Standard library only; Python 3.8 or newer.
"""

import argparse
import csv
import io
import json
import math
import os
import statistics
import sys
from collections import Counter

FORMAT = "frametime-report/0.1"
LOG_MAGIC = "# frametime-log v1"
LIKE_FOR_LIKE_KEYS = (
    "settings.profile",
    "settings.output",
    "settings.raytracing",
    "settings.upscaler",
    "scene",
)
FRAME_COLUMNS = ("MsBetweenPresents", "FrameTime", "msBetweenPresents")
APP_COLUMNS = ("Application", "ProcessName")
SWAPCHAIN_COLUMNS = ("SwapChainAddress", "SwapChain")
STUTTER_FACTOR = 2.5
REPRODUCE_TOLERANCE = 0.10


class FtError(Exception):
    pass


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
    try:
        if t and (t.lstrip("+-").isdigit()):
            return int(t)
        f = float(t)
        if math.isfinite(f):
            return f
    except ValueError:
        pass
    return t


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
# readers: each returns (frame_ms list, t_end_ms list, meta dict, marks dict, tool)

def read_text(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        return f.read()


def detect_format(text):
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first.strip().lower() == LOG_MAGIC:
        return "ftlog"
    head = text[:20000]
    if any(c in head for c in FRAME_COLUMNS):
        return "presentmon"
    raise FtError("unknown input format (expected '%s' or a PresentMon/CapFrameX CSV)" % LOG_MAGIC)


def read_ftlog(text):
    frames, ends, meta, marks = [], [], {}, {}
    lines = text.splitlines()
    if not lines or lines[0].strip().lower() != LOG_MAGIC:
        raise FtError("not a frametime-log v1 file")
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
                set_dotted(meta, k, parse_value(v))
            elif body.startswith("mark "):
                bits = body[5:].split(None, 1)
                if len(bits) != 2:
                    raise FtError("line %d: mark needs '<t_ms> <label>'" % no)
                try:
                    marks.setdefault(bits[1].strip(), float(bits[0]))
                except ValueError:
                    raise FtError("line %d: bad mark time" % no)
            continue
        if line.lower().startswith("t_ms"):
            continue
        parts = line.split(",")
        try:
            if len(parts) >= 2:
                t, ms = float(parts[0]), float(parts[1])
            else:
                t, ms = None, float(parts[0])
        except ValueError:
            raise FtError("line %d: expected '<t_ms>,<frame_ms>'" % no)
        if not (math.isfinite(ms) and ms > 0):
            continue
        if t is None:
            t = (ends[-1] if ends else 0.0) + ms
        frames.append(ms)
        ends.append(t)
    return frames, ends, meta, marks, "ftlog"


def read_presentmon(text, process=None, column=None):
    lines = text.splitlines()
    start = None
    for i, l in enumerate(lines):
        if any(c in l for c in FRAME_COLUMNS) or (column and column in l):
            start = i
            break
    if start is None:
        raise FtError("no PresentMon header row found")
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    fields = reader.fieldnames or []
    col = column if column else next((c for c in FRAME_COLUMNS if c in fields), None)
    if not col or col not in fields:
        raise FtError("frame-time column not found (have: %s)" % ", ".join(fields[:12]))
    app_col = next((c for c in APP_COLUMNS if c in fields), None)
    sc_col = next((c for c in SWAPCHAIN_COLUMNS if c in fields), None)

    rows = []
    for r in reader:
        v = (r.get(col) or "").strip()
        try:
            ms = float(v)
        except ValueError:
            continue
        if not (math.isfinite(ms) and ms > 0):
            continue
        rows.append((r.get(app_col, "") if app_col else "", r.get(sc_col, "") if sc_col else "", ms))
    if not rows:
        raise FtError("no frame rows in column %s" % col)

    notes = []
    if app_col:
        apps = Counter(a for a, _, _ in rows)
        if process:
            want = process.lower()
            rows = [x for x in rows if x[0].lower() == want]
            if not rows:
                raise FtError("process %r not in log (have: %s)" % (process, ", ".join(sorted(apps))))
        elif len(apps) > 1:
            pick = apps.most_common(1)[0][0]
            rows = [x for x in rows if x[0] == pick]
            notes.append("several processes in log; used %s (most frames), pick one with --process" % pick)
    if sc_col:
        chains = Counter(s for _, s, _ in rows)
        if len(chains) > 1:
            pick = chains.most_common(1)[0][0]
            rows = [x for x in rows if x[1] == pick]
            notes.append("several swap chains; used the one with most frames")

    frames, ends, t = [], [], 0.0
    for _, _, ms in rows:
        t += ms
        frames.append(ms)
        ends.append(t)
    meta = {}
    if app_col and rows and rows[0][0]:
        meta["app"] = rows[0][0]
    return frames, ends, meta, {}, "presentmon:" + col, notes


# --------------------------------------------------------------------------
# statistics

def nearest_rank(sorted_vals, p):
    n = len(sorted_vals)
    k = max(1, math.ceil(p / 100.0 * n))
    return sorted_vals[min(k, n) - 1]


def compute(frames):
    n = len(frames)
    if n == 0:
        raise FtError("no frames in the measured window")
    total = sum(frames)
    s = sorted(frames)
    median = statistics.median(s)
    worst = s[-max(1, math.ceil(0.01 * n)):]
    r2 = lambda x: round(x, 2)
    return {
        "frames": n,
        "avgFps": r2(n / (total / 1000.0)),
        "medianMs": r2(median),
        "p95Ms": r2(nearest_rank(s, 95)),
        "p99Ms": r2(nearest_rank(s, 99)),
        "low1Fps": r2(1000.0 / (sum(worst) / len(worst))),
        "stutters": sum(1 for x in frames if x > STUTTER_FACTOR * median),
        "maxMs": r2(s[-1]),
    }, total / 1000.0


def window(frames, ends, skip_s, marks, from_mark, max_s):
    start_ms = 0.0
    if from_mark:
        if from_mark not in marks:
            raise FtError("mark %r not in log (have: %s)" % (from_mark, ", ".join(sorted(marks)) or "none"))
        start_ms = marks[from_mark]
    start_ms += skip_s * 1000.0
    sel = [(f, e) for f, e in zip(frames, ends) if e - f >= start_ms - 1e-9]
    if max_s:
        out, acc = [], 0.0
        for f, e in sel:
            if acc + f > max_s * 1000.0 + 1e-9:
                break
            acc += f
            out.append((f, e))
        sel = out
    return [f for f, _ in sel]


# --------------------------------------------------------------------------
# commands

def build_report(args):
    text = read_text(args.log)
    fmt = args.format if args.format != "auto" else detect_format(text)
    notes = []
    if fmt == "ftlog":
        frames, ends, meta, marks, tool = read_ftlog(text)
    elif fmt in ("presentmon", "capframex"):
        frames, ends, meta, marks, tool, notes = read_presentmon(text, args.process, args.column)
        if fmt == "capframex":
            tool = tool.replace("presentmon", "capframex", 1)
    else:
        raise FtError("unknown format %s" % fmt)

    for item in args.set or []:
        if "=" not in item:
            raise FtError("--set needs key=value, got %r" % item)
        k, v = item.split("=", 1)
        set_dotted(meta, k, parse_value(v))

    if fmt != "ftlog" and args.from_mark:
        raise FtError("--from-mark only works with frame logs")
    sel = window(frames, ends, args.skip, marks, args.from_mark, args.max_seconds)
    result, seconds = compute(sel)

    report = {"format": FORMAT}
    for k in ("app", "build", "settings", "scene", "pc"):
        if k in meta:
            report[k] = meta[k]
    for k, v in meta.items():
        if k not in report and k not in ("format", "capture", "result", "checks"):
            report[k] = v
    report["capture"] = {
        "tool": tool,
        "source": os.path.basename(args.log),
        "seconds": round(seconds, 2),
        "skippedSeconds": args.skip,
        "fromMark": args.from_mark,
    }
    report["result"] = result

    problems = []
    if seconds < args.min_seconds:
        problems.append("window is %.0f s, shorter than the %.0f s minimum" % (seconds, args.min_seconds))
    if result["frames"] < 100:
        problems.append("only %d frames in the window" % result["frames"])
    for key in args.require.split(",") if args.require else []:
        key = key.strip()
        if key and get_dotted(report, key) is None:
            problems.append("missing %s (add it with --set %s=...)" % (key, key))
    report["checks"] = {"valid": not problems, "notes": problems + notes}
    return report


def fmt_settings(r):
    s = r.get("settings") or {}
    bits = [str(s.get("profile", "?")), str(s.get("output", "?"))]
    if s.get("raytracing") is True:
        bits.append("RT")
    if s.get("upscaler") not in (None, "none"):
        bits.append(str(s.get("upscaler")))
    return " ".join(bits)


def cmd_summarize(args):
    report = build_report(args)
    res = report["result"]
    print("%s  |  %s  |  scene %s" % (report["capture"]["source"], fmt_settings(report), report.get("scene", "?")))
    print("  %d frames over %.1f s (skipped %.0f s%s)" % (
        res["frames"], report["capture"]["seconds"], args.skip,
        ", from mark %s" % args.from_mark if args.from_mark else ""))
    print("  avg %.2f fps | median %.2f ms | p95 %.2f ms | p99 %.2f ms | 1%% low %.2f fps | stutters %d | max %.2f ms" % (
        res["avgFps"], res["medianMs"], res["p95Ms"], res["p99Ms"], res["low1Fps"], res["stutters"], res["maxMs"]))
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
    head = ("report", "commit", "settings", "scene", "avg fps", "p95 ms", "1% low", "stutters", "secs", "valid")
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
            "%.0f" % (get_dotted(r, "capture.seconds") or 0),
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
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("summarize", help="summarize a frame log or PresentMon/CapFrameX CSV")
    s.add_argument("log")
    s.add_argument("--format", default="auto", choices=("auto", "ftlog", "presentmon", "capframex"))
    s.add_argument("--process", help="PresentMon: application name to keep (e.g. app.exe)")
    s.add_argument("--column", help="PresentMon: frame-time column to use")
    s.add_argument("--skip", type=float, default=30.0, help="seconds to drop at the start (default 30)")
    s.add_argument("--from-mark", help="frame logs: start counting at this mark (plus --skip)")
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
