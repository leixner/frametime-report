"""Tests for ftreport.py. Run: python -I -m unittest discover -s tests (from the repo root)."""

import contextlib
import io
import json
import math
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ftreport  # noqa: E402


def write(dirpath, name, text):
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return p


def ftlog_text(frame_ms, meta=(), marks=(), header_first=True):
    """Text the way the Rust/C recorders write it: magic, column header, then lines."""
    out = ["# frametime-log v1", "t_ms,frame_ms"]
    out += ["# meta %s=%s" % kv for kv in meta]
    t = 0.0
    mark_iter = sorted(marks)
    for ms in frame_ms:
        t += ms
        while mark_iter and mark_iter[0][0] <= t:
            mt, label = mark_iter.pop(0)
            out.append("# mark %.3f %s" % (mt, label))
        out.append("%.3f,%.3f" % (t, ms))
    return "\n".join(out) + "\n"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ftreport.main(argv)
    return code, buf.getvalue()


class StatsTests(unittest.TestCase):
    def test_known_values(self):
        # 98 frames of 40 ms, 2 frames of 200 ms
        frames = [40.0] * 98 + [200.0, 200.0]
        r, secs = ftreport.compute(frames)
        self.assertEqual(r["frames"], 100)
        self.assertAlmostEqual(secs, (98 * 40 + 400) / 1000.0)
        self.assertEqual(r["avgFps"], round(100 / 4.32, 2))   # 23.15
        self.assertEqual(r["medianMs"], 40.0)
        self.assertEqual(r["p95Ms"], 40.0)                    # rank 95 of 100
        self.assertEqual(r["p99Ms"], 200.0)                   # rank 99
        self.assertEqual(r["low1Fps"], 5.0)                   # slowest 1 frame: 200 ms
        self.assertEqual(r["stutters"], 2)                    # > 2.5 * 40
        self.assertEqual(r["maxMs"], 200.0)

    def test_avg_is_time_weighted(self):
        # 10 ms and 90 ms: mean of per-frame fps would be 55.6, time-weighted is 20
        r, _ = ftreport.compute([10.0, 90.0])
        self.assertEqual(r["avgFps"], 20.0)

    def test_nearest_rank(self):
        s = list(range(1, 21))  # 1..20
        self.assertEqual(ftreport.nearest_rank(s, 95), 19)
        self.assertEqual(ftreport.nearest_rank(s, 50), 10)
        self.assertEqual(ftreport.nearest_rank([7.0], 99), 7.0)

    def test_empty_window(self):
        with self.assertRaises(ftreport.FtError):
            ftreport.compute([])


class FtlogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_summarize_with_meta_set_and_skip(self):
        # 30 s of 100 ms frames (loading), then 200 s of 40 ms frames
        frames = [100.0] * 300 + [40.0] * 5000
        p = write(self.d, "run.ftlog", ftlog_text(frames, meta=[("settings.profile", "series-x"),
                                                              ("settings.raytracing", "false"),
                                                              ("settings.output", "1920x1080")]))
        out = os.path.join(self.d, "r.json")
        code, text = run(["summarize", p, "--set", "scene=city-walk", "--set", "pc.ramGB=64", "--json", out])
        self.assertEqual(code, 0, text)
        r = load(out)
        self.assertEqual(r["format"], "frametime-report/0.1")
        self.assertEqual(r["settings"], {"profile": "series-x", "raytracing": False, "output": "1920x1080"})
        self.assertEqual(r["scene"], "city-walk")
        self.assertEqual(r["pc"]["ramGB"], 64)
        self.assertEqual(r["result"]["frames"], 5000)           # the 30 s of loading are skipped
        self.assertEqual(r["result"]["avgFps"], 25.0)
        self.assertEqual(r["capture"]["seconds"], 200.0)
        self.assertTrue(r["checks"]["valid"], r["checks"])

    def test_from_mark(self):
        frames = [100.0] * 600 + [20.0] * 10000   # mark at 60 s
        p = write(self.d, "m.ftlog", ftlog_text(frames, marks=[(60000.0, "loaded")]))
        code, _ = run(["summarize", p, "--from-mark", "loaded", "--skip", "0",
                       "--set", "scene=boat", "--set", "settings.profile=s", "--json", os.path.join(self.d, "m.json")])
        self.assertEqual(code, 0)
        r = json.load(open(os.path.join(self.d, "m.json"), encoding="utf-8"))
        self.assertEqual(r["result"]["frames"], 10000)
        self.assertEqual(r["result"]["avgFps"], 50.0)
        self.assertEqual(r["capture"]["fromMark"], "loaded")

    def test_missing_mark_is_an_error(self):
        p = write(self.d, "x.ftlog", ftlog_text([16.0] * 10))
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code, _ = run(["summarize", p, "--from-mark", "nope", "--json", "-"])
        self.assertEqual(code, 1)
        self.assertIn("mark 'nope'", buf.getvalue())

    def test_short_run_and_missing_fields_are_invalid(self):
        p = write(self.d, "short.ftlog", ftlog_text([33.3] * 3000))   # ~100 s
        out = os.path.join(self.d, "s.json")
        run(["summarize", p, "--json", out])
        r = load(out)
        self.assertFalse(r["checks"]["valid"])
        joined = " ".join(r["checks"]["notes"])
        self.assertIn("shorter than the 180 s minimum", joined)
        self.assertIn("missing settings.profile", joined)
        self.assertIn("missing scene", joined)

    def test_max_seconds(self):
        p = write(self.d, "long.ftlog", ftlog_text([10.0] * 60000))   # 600 s
        out = os.path.join(self.d, "l.json")
        run(["summarize", p, "--skip", "0", "--max-seconds", "300", "--json", out])
        r = load(out)
        self.assertEqual(r["result"]["frames"], 30000)

    def test_single_column_lines_and_bad_lines(self):
        text = "# frametime-log v1\n16\n17\n# mark 20.0 two words? no\n"
        cap = ftreport.read_ftlog(text)
        frames, ends, marks = cap.frames, cap.ends, cap.marks
        self.assertEqual(frames, [16.0, 17.0])
        self.assertEqual(ends, [16.0, 33.0])
        self.assertIn("two words? no", marks)
        with self.assertRaises(ftreport.FtError):
            ftreport.read_ftlog("# frametime-log v1\nabc,def\n")
        with self.assertRaises(ftreport.FtError):
            ftreport.read_ftlog("not a log\n1,2\n")

    def test_c_recorder_output_shape(self):
        # what ftlog.h writes: integer-formatted ms with 3 decimals, marks flushed inline
        text = ("# frametime-log v1\nt_ms,frame_ms\n# meta settings.profile=series-s\n"
                "# meta build.repo=owner/name\n16.667,16.667\n# mark 20.001 loaded\n33.334,16.667\n")
        cap = ftreport.read_ftlog(text)
        frames, meta, marks = cap.frames, cap.meta, cap.marks
        self.assertEqual(len(frames), 2)
        self.assertEqual(meta["settings"]["profile"], "series-s")
        self.assertEqual(meta["build"]["repo"], "owner/name")
        self.assertAlmostEqual(marks["loaded"], 20.001)

    def test_value_parsing(self):
        self.assertIs(ftreport.parse_value("true"), True)
        self.assertEqual(ftreport.parse_value("30"), 30)
        self.assertEqual(ftreport.parse_value("16.5"), 16.5)
        self.assertEqual(ftreport.parse_value("1920x1080"), "1920x1080")
        self.assertEqual(ftreport.parse_value("abc1234"), "abc1234")
        self.assertEqual(ftreport.parse_value("nan"), "nan")
        self.assertEqual(ftreport.parse_value("1_000"), "1_000")
        self.assertEqual(ftreport.parse_value("-2"), -2)
        self.assertEqual(ftreport.parse_value(".5"), 0.5)

    def test_rounding_is_half_away_from_zero(self):
        self.assertEqual(ftreport.r2(0.125), 0.13)     # Python's round() would give 0.12
        self.assertEqual(ftreport.r2(-0.125), -0.13)
        self.assertEqual(ftreport.r2(2.0), 2.0)
        self.assertEqual(ftreport.fmt0(179.5), "180")

    def test_busy_columns_in_log(self):
        lines = ["# frametime-log v1", "t_ms,frame_ms,cpu_ms,gpu_ms"]
        t = 0.0
        for i in range(400):
            t += 40.0
            gpu = "" if i % 10 == 1 else "36.0"          # some frames without GPU time
            lines.append("%.3f,40.000,%s,%s" % (t, "30.0" if i % 2 else "NA", gpu))
        cap = ftreport.read_ftlog("\n".join(lines))
        self.assertEqual(len(cap.frames), 400)
        self.assertEqual(sum(1 for c, g in zip(cap.cpu, cap.gpu) if c is not None and g is not None), 160)
        b = ftreport.analyze_busy(cap.frames, cap.cpu, cap.gpu)
        self.assertIsNone(b)                               # only 40% of frames have both values
        cap2 = ftreport.read_ftlog("\n".join(l.replace(",NA,", ",30.0,") for l in lines))
        b = ftreport.analyze_busy(cap2.frames, cap2.cpu, cap2.gpu)
        self.assertEqual(b, {"frames": 360, "cpuMs": 30.0, "gpuMs": 36.0, "gpuBoundShare": 1.0, "limit": "gpu"})


class FreezeAndUsageTests(unittest.TestCase):
    def test_freezes_are_frames_over_200_ms(self):
        r, _ = ftreport.compute([40.0] * 100 + [200.0, 200.01, 350.0])
        self.assertEqual(r["freezes"], 2)          # 200.0 itself is not a freeze
        self.assertEqual(r["stutters"], 3)         # all three are > 2.5 x the 40 ms median

    def test_samples_parsed_and_limited_to_the_window(self):
        lines = ["# frametime-log v1"]
        t = 0.0
        for i in range(3000):                      # 3000 x 40 ms = 120 s
            t += 40.0
            lines.append("%.3f,40.000" % t)
            if i % 25 == 24:                       # a sample every second
                lines.append("# sample %.3f cpu=%d ram=2048.5 gpu=97 vram=6000 syscpu=40 sysram=55 junk bad=-1 x=nan"
                             % (t, 10 + (i // 25) % 3))
        lines.append("# sample 999999 cpu=99")     # after the last frame: outside the window
        cap = ftreport.read_ftlog("\n".join(lines))
        self.assertEqual(len(cap.samples), 121)
        self.assertNotIn("bad", cap.samples[0][1])
        self.assertNotIn("x", cap.samples[0][1])
        r = ftreport.build_report_from_text("\n".join(lines), "u.ftlog", {"skip": 30.0, "require": []})
        u = r["result"]["usage"]
        self.assertEqual(u["samples"], 91)          # seconds 30..120
        self.assertEqual(u["cpuPct"], {"median": 11.0, "max": 12.0})
        self.assertEqual(u["gpuPct"], {"median": 97.0, "max": 97.0})
        self.assertEqual(u["ramMB"]["median"], 2048.5)
        self.assertEqual(set(u), {"samples", "cpuPct", "ramMB", "gpuPct", "vramMB", "sysCpuPct", "sysRamPct"})

    def test_no_samples_no_usage(self):
        r = ftreport.build_report_from_text("# frametime-log v1\n" + "\n".join(["16.6"] * 500), "x", {"skip": 0.0})
        self.assertNotIn("usage", r["result"])
        self.assertEqual(r["result"]["freezes"], 0)

    def test_bad_sample_time_is_an_error(self):
        with self.assertRaises(ftreport.FtError):
            ftreport.read_ftlog("# frametime-log v1\n# sample abc cpu=1\n")


class SegmentAndTimeAtTests(unittest.TestCase):
    def test_time_at_30_and_60_fps(self):
        # 10 s at 10 ms, 10 s at 25 ms, 10 s at 50 ms: 30 s in total
        frames = [10.0] * 1000 + [25.0] * 400 + [50.0] * 200
        r, _ = ftreport.compute(frames)
        self.assertEqual(r["timeAt60Pct"], 33.33)       # only the 10 ms frames
        self.assertEqual(r["timeAt30Pct"], 66.67)       # 10 ms and 25 ms frames
        r, _ = ftreport.compute([1000.0 / 30.0] * 10 + [1000.0 / 60.0] * 10)
        self.assertEqual(r["timeAt30Pct"], 100.0)       # exactly 30 fps counts
        self.assertEqual(r["timeAt60Pct"], 33.33)

    def test_until_mark_ends_the_window(self):
        lines = ["# frametime-log v1"]
        t = 0.0
        for i in range(3000):                           # 120 s of 40 ms frames
            t += 40.0
            lines.append("%.3f,40.000" % t)
            if i == 999:
                lines.append("# mark %.3f city" % t)       # at 40 s
            if i == 1999:
                lines.append("# mark %.3f drive" % t)      # at 80 s
        text = "\n".join(lines)
        r = ftreport.build_report_from_text(text, "s.ftlog", {"fromMark": "city", "untilMark": "drive", "skip": 0.0,
                                                              "minSeconds": 0.0, "require": []})
        self.assertEqual(r["result"]["frames"], 1000)
        self.assertEqual(r["capture"]["seconds"], 40.0)
        self.assertEqual(r["capture"]["untilMark"], "drive")
        r = ftreport.build_report_from_text(text, "s.ftlog", {"untilMark": "city", "skip": 0.0, "require": []})
        self.assertEqual(r["result"]["frames"], 1000)
        with self.assertRaises(ftreport.FtError):     # the window is empty
            ftreport.build_report_from_text(text, "s.ftlog", {"fromMark": "drive", "untilMark": "city", "skip": 0.0})
        with self.assertRaises(ftreport.FtError):
            ftreport.build_report_from_text(text, "s.ftlog", {"untilMark": "nope"})

    def test_until_mark_needs_a_frame_log(self):
        csv_text = "Application,MsBetweenPresents\n" + "\n".join(["a.exe,20.0"] * 200)
        with self.assertRaises(ftreport.FtError):
            ftreport.build_report_from_text(csv_text, "x.csv", {"untilMark": "city"})


class ColdBootTests(unittest.TestCase):
    """capture.phase, capture.clockStart, capture.firstPresentMs, settings.shaderCache (PortWatch #233)."""

    META = [("settings.profile", "series-x"), ("scene", "city-walk")]

    def build(self, meta, opts=None, frames=7000, marks=()):
        text = ftlog_text([40.0] * frames, meta=self.META + list(meta), marks=marks)
        o = {"skip": 0.0}
        o.update(opts or {})
        return ftreport.build_report_from_text(text, "boot.ftlog", o)

    def test_cold_boot_counts_the_time_before_the_first_present(self):
        r = self.build([("capture.phase", "cold-boot"), ("capture.firstPresentMs", "10000"),
                        ("settings.shaderCache", "cold")])
        res, cap = r["result"], r["capture"]
        self.assertEqual(cap["phase"], "cold-boot")
        self.assertEqual(cap["clockStart"], "launch")
        self.assertEqual(cap["firstPresentMs"], 10000)
        self.assertEqual(r["settings"]["shaderCache"], "cold")
        self.assertEqual(res["frames"], 7001)                 # the 10 s gap counts as one frame
        self.assertEqual(cap["seconds"], 290.0)               # 280 s of frames + 10 s before them
        self.assertEqual(res["avgFps"], ftreport.r2(7001 / 290.0))
        self.assertEqual(res["freezes"], 1)
        self.assertEqual(res["maxMs"], 10000.0)
        self.assertTrue(r["checks"]["valid"], r["checks"]["notes"])

    def test_cold_boot_without_launch_time_is_not_valid(self):
        r = self.build([("capture.phase", "cold-boot")])
        self.assertEqual(r["result"]["frames"], 7000)
        self.assertNotIn("clockStart", r["capture"])
        self.assertFalse(r["checks"]["valid"])
        self.assertIn("capture.firstPresentMs (launch to first present) is missing", r["checks"]["notes"][0])

    def test_cold_boot_must_not_skip(self):
        r = self.build([("capture.phase", "cold-boot"), ("capture.firstPresentMs", "800")], {"skip": 30.0})
        self.assertFalse(r["checks"]["valid"])
        self.assertIn("use --skip 0", " ".join(r["checks"]["notes"]))

    def test_first_present_clock_keeps_the_frames(self):
        r = self.build([("capture.clockStart", "first-present"), ("capture.firstPresentMs", "4321.5")])
        self.assertEqual(r["result"]["frames"], 7000)
        self.assertEqual(r["capture"]["clockStart"], "first-present")
        self.assertEqual(r["capture"]["firstPresentMs"], 4321.5)
        self.assertNotIn("phase", r["capture"])

    def test_launch_clock_moves_marks_with_the_frames(self):
        meta = [("capture.clockStart", "launch"), ("capture.firstPresentMs", "5000")]
        marks = [(100000.0, "city")]
        plain = self.build([], {"fromMark": "city", "minSeconds": 0.0}, marks=marks)
        moved = self.build(meta, {"fromMark": "city", "minSeconds": 0.0}, marks=marks)
        self.assertEqual(plain["result"], moved["result"])

    def test_bad_values_are_reported_and_dropped(self):
        r = self.build([("capture.phase", "cold_boot"), ("capture.clockStart", "boot"),
                        ("capture.firstPresentMs", "soon"), ("settings.shaderCache", "hot")])
        notes = r["checks"]["notes"]
        self.assertFalse(r["checks"]["valid"])
        for text in ("capture.phase must be", "capture.clockStart must be", "settings.shaderCache must be",
                     "capture.firstPresentMs must be"):
            self.assertTrue(any(n.startswith(text) for n in notes), text)
        for k in ("phase", "clockStart", "firstPresentMs"):
            self.assertNotIn(k, r["capture"])

    def test_set_works_for_presentmon(self):
        csv_text = "Application,MsBetweenPresents\n" + "\n".join(["a.exe,20.0"] * 9000)
        r = ftreport.build_report_from_text(csv_text, "x.csv", {
            "skip": 0.0, "set": ["capture.phase=cold-boot", "capture.firstPresentMs=2500"]})
        self.assertEqual(r["result"]["frames"], 9001)
        self.assertEqual(r["capture"]["clockStart"], "launch")


class DetectPcTests(unittest.TestCase):
    def test_detect_pc_gives_names_only(self):
        pc = ftreport.detect_pc()
        self.assertIn("cores", pc)
        self.assertIn("os", pc)
        allowed = {"cores", "cpu", "gpu", "vramGB", "driver", "os", "ramGB"}
        self.assertTrue(set(pc) <= allowed, set(pc) - allowed)
        if sys.platform == "win32":
            for key in ("cpu", "ramGB"):
                self.assertIn(key, pc)
            self.assertTrue(pc["os"].startswith("Windows"))

    def test_detect_pc_does_not_override_the_log_or_set(self):
        text = "# frametime-log v1\n# meta pc.gpu=From The Log\n" + "\n".join(["16.6"] * 500)
        r = ftreport.build_report_from_text(text, "x", {"skip": 0.0, "detectPc": True, "set": ["pc.cpu=From Set"]})
        self.assertEqual(r["pc"]["gpu"], "From The Log")
        self.assertEqual(r["pc"]["cpu"], "From Set")
        self.assertIn("cores", r["pc"])


class BusyTests(unittest.TestCase):
    def check(self, cpu, gpu, frame, limit, share=None):
        n = len(cpu)
        b = ftreport.analyze_busy([frame] * n, cpu, gpu)
        self.assertEqual(b["limit"], limit, b)
        if share is not None:
            self.assertEqual(b["gpuBoundShare"], share)

    def test_gpu_bound(self):
        self.check([20.0] * 10, [38.0] * 10, 40.0, "gpu", 1.0)

    def test_cpu_bound(self):
        # the case of one submitting thread at 42 ms while the GPU needs 36 ms
        self.check([42.0] * 10, [36.0] * 10, 44.0, "cpu", 0.0)

    def test_mixed(self):
        self.check([30.0, 38.0] * 5, [36.0, 32.0] * 5, 40.0, "mixed", 0.5)

    def test_pacing_when_capped(self):
        # 30 fps cap: frames take 33.3 ms, but neither side is busy for 25 ms
        self.check([12.0] * 10, [18.0] * 10, 33.33, "pacing")

    def test_no_data(self):
        self.assertIsNone(ftreport.analyze_busy([10.0] * 4, [None] * 4, [None] * 4))
        self.assertIsNone(ftreport.analyze_busy([], [], []))


class PresentMonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_presentmon_1x_two_processes(self):
        head = "Application,ProcessID,SwapChainAddress,Runtime,SyncInterval,PresentFlags,Dropped,TimeInSeconds,MsBetweenPresents,MsBetweenDisplayChange\n"
        rows = []
        t = 0.0
        for i in range(8000):
            t += 0.04
            rows.append("app.exe,100,0x1,DXGI,0,0,0,%.4f,40.0,40.0" % t)
            if i % 4 == 0:
                rows.append("dwm.exe,200,0x9,DXGI,1,0,0,%.4f,16.7,16.7" % t)
        p = write(self.d, "pm1.csv", head + "\n".join(rows) + "\n")
        out = os.path.join(self.d, "pm1.json")
        code, text = run(["summarize", p, "--process", "app.exe", "--set", "scene=menu",
                          "--set", "settings.profile=x", "--json", out])
        self.assertEqual(code, 0, text)
        r = load(out)
        self.assertEqual(r["capture"]["tool"], "presentmon:MsBetweenPresents")
        self.assertEqual(r["app"], "app.exe")
        self.assertEqual(r["result"]["avgFps"], 25.0)
        self.assertEqual(r["result"]["frames"], 8000 - 750)     # 30 s / 40 ms skipped

    def test_presentmon_auto_picks_busiest_process(self):
        head = "Application,ProcessID,SwapChainAddress,MsBetweenPresents\n"
        rows = ["a.exe,1,0x1,10.0"] * 30000 + ["b.exe,2,0x2,50.0"] * 100
        p = write(self.d, "pm.csv", head + "\n".join(rows) + "\n")
        with open(p, encoding="utf-8") as f:
            report = ftreport.build_report_from_text(f.read(), "pm.csv", {"skip": 0.0, "require": []})
        self.assertEqual(report["app"], "a.exe")
        self.assertTrue(any("several processes" in n for n in report["checks"]["notes"]))
        self.assertTrue(report["checks"]["valid"])               # a note, not a failure

    def test_presentmon_2x_frametime_and_na(self):
        head = "Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing,PresentMode,FrameType,CPUStartTime,FrameTime,CPUBusy,CPUWait,GPULatency,GPUTime\n"
        rows = ["app.exe,1,0x1,DXGI,0,0,1,Hardware: Independent Flip,Application,%d,%s,10,5,1,30" %
                (i, "NA" if i % 100 == 0 else "45.0") for i in range(6000)]
        p = write(self.d, "pm2.csv", head + "\n".join(rows) + "\n")
        out = os.path.join(self.d, "pm2.json")
        code, _ = run(["summarize", p, "--skip", "0", "--min-seconds", "0", "--require", "", "--json", out])
        self.assertEqual(code, 0)
        r = load(out)
        self.assertEqual(r["capture"]["tool"], "presentmon:FrameTime")
        self.assertEqual(r["result"]["frames"], 5940)            # NA rows dropped
        self.assertEqual(r["result"]["avgFps"], round(1000 / 45.0, 2))
        self.assertNotIn("busy", r["result"])               # GPUBusy column absent: no analysis

    def test_capframex_with_preamble(self):
        text = ("// CapFrameX export\nGameName,SomeGame\n\nApplication,MsBetweenPresents\n" +
                "\n".join(["g.exe,20.0"] * 12000) + "\n")
        p = write(self.d, "cx.csv", text)
        out = os.path.join(self.d, "cx.json")
        code, _ = run(["summarize", p, "--format", "capframex", "--require", "", "--json", out])
        self.assertEqual(code, 0)
        r = load(out)
        self.assertTrue(r["capture"]["tool"].startswith("capframex:"))
        self.assertEqual(r["result"]["avgFps"], 50.0)

    def test_unknown_format(self):
        p = write(self.d, "x.txt", "hello\n")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code, _ = run(["summarize", p])
        self.assertEqual(code, 1)
        self.assertIn("unknown input format", buf.getvalue())


class PresentMonBusyTests(unittest.TestCase):
    def test_presentmon_2x_busy_columns(self):
        head = "Application,ProcessID,SwapChainAddress,FrameTime,CPUBusy,CPUWait,GPULatency,GPUTime,GPUBusy,GPUWait\n"
        rows = ["app.exe,1,0x1,45.0,42.1,1.0,2.0,36.5,36.0,8.0" for _ in range(6000)]
        text = head + "\n".join(rows) + "\n"
        r = ftreport.build_report_from_text(text, "pm2.csv", {"require": []})
        self.assertEqual(r["result"]["busy"], {"frames": 5333, "cpuMs": 42.1, "gpuMs": 36.0,
                                               "gpuBoundShare": 0.0, "limit": "cpu"})
        self.assertEqual(r["capture"]["ftreport"], ftreport.VERSION)

    def test_presentmon_quoted_fields(self):
        head = 'Application,ProcessID,PresentMode,MsBetweenPresents\n'
        rows = ['"my, app.exe",1,"Hardware: Independent Flip",20.0'] * 12000
        r = ftreport.build_report_from_text(head + "\n".join(rows), "q.csv", {"require": []})
        self.assertEqual(r["app"], "my, app.exe")
        self.assertEqual(r["result"]["avgFps"], 50.0)


class CompareValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, name, ms, scene="city-walk", profile="series-x", commit="abc1234", extra=()):
        p = write(self.d, name + ".ftlog", ftlog_text([ms] * 8000, meta=[("settings.profile", profile),
                                                                       ("build.commit", commit)] + list(extra)))
        out = os.path.join(self.d, name + ".json")
        run(["summarize", p, "--set", "scene=" + scene, "--json", out])
        return out

    def test_compare_reproduces_and_not_like_for_like(self):
        a = self.report("a", 40.0)
        b = self.report("b", 42.0)           # 23.81 vs 25.0: within 10%
        c = self.report("c", 60.0)           # 16.67: not within 10%
        e = self.report("e", 40.0, profile="series-s")
        code, text = run(["compare", a, b, c, e])
        self.assertEqual(code, 0)
        self.assertIn("b.json vs a.json: like for like", text)
        self.assertIn("reproduces (within 10%)", text)
        self.assertIn("c.json vs a.json: like for like", text)
        self.assertIn("does NOT reproduce", text)
        self.assertIn("e.json vs a.json: NOT like for like (differs in settings.profile)", text)

    def test_internal_resolution_and_cap_are_not_like_for_like(self):
        # reported on PortWatch issue #233: 1280x720 uncapped vs 1600x900 capped at 30
        a = self.report("a", 40.0, extra=[("settings.output", "1920x1080"), ("settings.internal", "1280x720")])
        b = self.report("b", 44.0, extra=[("settings.output", "1920x1080"), ("settings.internal", "1600x900"),
                                          ("settings.fpsCap", "30")])
        c = self.report("c", 40.5, extra=[("settings.output", "1920x1080"), ("settings.internal", "1280x720")])
        code, text = run(["compare", a, b, c])
        self.assertEqual(code, 0)
        self.assertIn("b.json vs a.json: NOT like for like (differs in settings.internal; "
                      "only one report says settings.fpsCap)", text)
        self.assertIn("c.json vs a.json: like for like", text)

    def test_missing_on_one_side_is_unknown_not_like_for_like(self):
        a = self.report("a", 40.0, extra=[("settings.fpsCap", "30")])
        b = self.report("b", 40.0)
        code, text = run(["compare", a, b])
        self.assertIn("b.json vs a.json: like for like UNKNOWN (only one report says settings.fpsCap), avg +0.0%", text)
        self.assertNotIn("reproduces", text)

    def test_different_phase_is_not_like_for_like(self):
        a = self.report("a", 40.0, extra=[("capture.phase", "scene")])
        b = self.report("b", 40.0, extra=[("capture.phase", "cold-boot"), ("capture.firstPresentMs", "5000")])
        code, text = run(["compare", a, b])
        # a has no clock start (no launch time given), b starts at launch
        self.assertIn("b.json vs a.json: NOT like for like (differs in capture.phase; "
                      "only one report says capture.clockStart)", text)
        r = json.load(open(b, encoding="utf-8"))
        self.assertEqual(r["capture"]["clockStart"], "launch")

    def test_like_for_like_keys_cover_every_rate_setting(self):
        for key in ("settings.profile", "settings.output", "settings.internal", "settings.raytracing",
                    "settings.upscaler", "settings.fpsCap", "settings.shaderCache", "scene",
                    "capture.phase", "capture.clockStart"):
            self.assertIn(key, ftreport.LIKE_FOR_LIKE_KEYS)

    def test_validate_rejects_cold_boot_that_skips(self):
        good = self.report("cold", 40.0, extra=[("capture.phase", "cold-boot"), ("capture.firstPresentMs", "2400")])
        r = json.load(open(good, encoding="utf-8"))
        self.assertEqual(r["capture"]["skippedSeconds"], 30.0)      # the default skip: summarize flags it too
        self.assertFalse(r["checks"]["valid"])
        for change in ({"skippedSeconds": 30.0}, {"skippedSeconds": 0.0, "fromMark": "menu"}):
            with self.subTest(change=change):
                bad = dict(r, capture=dict(r["capture"], **change))
                path = write(self.d, "skip.json", json.dumps(bad))
                code, text = run(["validate", path])
                self.assertEqual(code, 1)
                self.assertIn("a cold-boot run is measured from launch", text)
        ok = dict(r, capture=dict(r["capture"], skippedSeconds=0.0, fromMark=None))
        code, text = run(["validate", write(self.d, "ok.json", json.dumps(ok))])
        self.assertEqual(code, 0, text)

    def test_validate_rejects_bad_launch_metadata(self):
        good = self.report("cold", 40.0, extra=[
            ("capture.phase", "cold-boot"), ("capture.firstPresentMs", "2400"),
            ("settings.shaderCache", "cold"),
        ])
        r = json.load(open(good, encoding="utf-8"))     # a cold boot is measured without a skip
        good = write(self.d, "cold-ok.json", json.dumps(dict(r, capture=dict(r["capture"], skippedSeconds=0.0))))
        self.assertEqual(run(["validate", good])[0], 0)
        mutations = (
            ("phase", "cold_boot", "capture.phase must be"),
            ("clockStart", "boot", "capture.clockStart must be"),
            ("firstPresentMs", -1, "capture.firstPresentMs must be"),
            ("firstPresentMs", float("nan"), "capture.firstPresentMs must be"),
            ("firstPresentMs", None, "capture.firstPresentMs (launch to first present) is missing"),
        )
        with open(good, encoding="utf-8") as report:
            original = report.read()
        for key, value, message in mutations:
            with self.subTest(key=key, value=value):
                r = json.loads(original)
                if value is None:
                    del r["capture"][key]
                else:
                    r["capture"][key] = value
                bad = write(self.d, "bad-launch.json", json.dumps(r))
                code, text = run(["validate", bad])
                self.assertEqual(code, 1)
                self.assertIn(message, text)
        r = json.loads(original)
        r["settings"]["shaderCache"] = "hot"
        bad = write(self.d, "bad-cache.json", json.dumps(r))
        code, text = run(["validate", bad])
        self.assertEqual(code, 1)
        self.assertIn("settings.shaderCache must be", text)

    def test_validate(self):
        good = self.report("g", 40.0)
        bad = write(self.d, "bad.json", json.dumps({"format": "other"}))
        code, text = run(["validate", good, bad])
        self.assertEqual(code, 1)
        self.assertIn("g.json: ok", text)
        self.assertIn("bad.json: INVALID", text)


if __name__ == "__main__":
    unittest.main()
