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

    def report(self, name, ms, scene="city-walk", profile="series-x", commit="abc1234"):
        p = write(self.d, name + ".ftlog", ftlog_text([ms] * 8000, meta=[("settings.profile", profile),
                                                                       ("build.commit", commit)]))
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

    def test_validate(self):
        good = self.report("g", 40.0)
        bad = write(self.d, "bad.json", json.dumps({"format": "other"}))
        code, text = run(["validate", good, bad])
        self.assertEqual(code, 1)
        self.assertIn("g.json: ok", text)
        self.assertIn("bad.json: INVALID", text)


if __name__ == "__main__":
    unittest.main()
