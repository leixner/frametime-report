"""The browser page (docs/ftreport-core.js) must produce exactly the reports ftreport.py does.

Runs both on the same inputs and options and compares the full reports.
Needs Node.js on PATH; skipped otherwise.
"""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ftreport  # noqa: E402

NODE = shutil.which("node")


def write(d, name, text, bom=False, crlf=False):
    if crlf:
        text = text.replace("\n", "\r\n")
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
        f.write(text)
    return p


def jitter_log(rng, n, busy_every=0, marks=(), meta=()):
    """A frame log with values that have many decimals, to stress sums and rounding."""
    lines = ["# frametime-log v1", "t_ms,frame_ms,cpu_ms,gpu_ms"]
    lines += ["# meta %s=%s" % kv for kv in meta]
    t, pending = 0.0, sorted(marks)
    for i in range(n):
        ms = round(rng.uniform(8, 60) if rng.random() > 0.01 else rng.uniform(100, 400), rng.choice([3, 4, 6]))
        t += ms
        while pending and pending[0][0] <= t:
            mt, label = pending.pop(0)
            lines.append("# mark %.3f %s" % (mt, label))
        if busy_every and i % busy_every:
            cpu = "%.3f" % rng.uniform(0.3, 1.1) if False else "%.3f" % (ms * rng.uniform(0.3, 1.0))
            gpu = "NA" if rng.random() < 0.05 else "%.3f" % (ms * rng.uniform(0.3, 1.0))
            lines.append("%.3f,%s,%s,%s" % (t, repr(ms), cpu, gpu))
        else:
            lines.append("%.3f,%s" % (t, repr(ms)))
    return "\n".join(lines) + "\n"


def presentmon_2x(rng, n, apps=("game.exe",), quoted=False):
    head = "Application,ProcessID,SwapChainAddress,PresentMode,FrameTime,CPUBusy,CPUWait,GPUBusy,GPUWait\n"
    rows = []
    for i in range(n):
        app = apps[i % len(apps)] if rng.random() < 0.9 else apps[0]
        ft = rng.uniform(10, 50)
        mode = '"Hardware: Independent Flip, test"' if quoted else "Hardware: Independent Flip"
        rows.append("%s,1,0x%d,%s,%s,%.4f,%.4f,%s,%.4f" % (
            '"%s"' % app if quoted else app, 1 if rng.random() < 0.97 else 2, mode,
            "NA" if rng.random() < 0.01 else "%.5f" % ft,
            ft * rng.uniform(0.4, 1.0), ft * 0.1, "%.4f" % (ft * rng.uniform(0.4, 1.0)), ft * 0.05))
    return head + "\n".join(rows) + "\n"


@unittest.skipUnless(NODE, "Node.js not on PATH")
class ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        rng = random.Random(1234)
        cases = []

        def add(path, **options):
            cases.append({"path": path, "source": os.path.basename(path), "options": options})

        log = write(d, "a.ftlog", jitter_log(rng, 9000, busy_every=3, marks=[(20000.0, "loaded"), (90000.0, "city")],
                                              meta=[("settings.profile", "series-x"), ("settings.raytracing", "false"),
                                                    ("settings.fpsCap", "30"), ("build.commit", "0abc123"),
                                                    ("pc.ramGB", "64"), ("odd", "1_000"), ("neg", "-2.50")]))
        add(log)
        add(log, skip=0.0, minSeconds=0.0, require=[])
        add(log, fromMark="loaded", skip=5.0, set=["scene=city-walk", "pc.gpu=Example GPU", "settings.raytracing=yes"])
        add(log, fromMark="city", maxSeconds=60.0, set=["scene=custom:x"])
        add(log, fromMark="loaded", untilMark="city", skip=0.0, minSeconds=0.0)
        add(log, untilMark="city", skip=3.0)
        add(log, fromMark="city", untilMark="loaded")          # empty window: an error on both sides
        add(log, untilMark="missing")
        add(log, fromMark="missing")
        add(write(d, "b.ftlog", jitter_log(rng, 3000), bom=True, crlf=True), skip=1.0, set=["scene=boat"])
        add(write(d, "c.ftlog", "# frametime-log v1\n16\n17.5\n\n-3\n0\n18.25\n"), skip=0.0)
        # continuous spread from fast to very slow frames, so every threshold has frames near it
        spread = ["# frametime-log v1"]
        t = 0.0
        for _ in range(20000):
            ms = round(rng.uniform(4, 160), rng.choice([2, 3, 5]))
            cpu, gpu = ms * rng.uniform(0.2, 1.0), ms * rng.uniform(0.2, 1.0)
            t += ms
            spread.append("%.3f,%s,%.3f,%.3f" % (t, repr(ms), cpu, gpu))
        spread_log = write(d, "spread.ftlog", "\n".join(spread) + "\n")
        add(spread_log)
        add(spread_log, skip=0.0, maxSeconds=777.7)
        add(spread_log, skip=0.0, maxSeconds=33.3, minSeconds=0.0)
        # heavy tail around the stutter threshold, busy times around the pacing threshold,
        # and frames where CPU and GPU busy are exactly equal
        for k, scale in enumerate((0.62, 0.68, 0.72, 0.74, 0.76, 0.78, 0.84)):
            lines, t = ["# frametime-log v1"], 0.0
            for _ in range(6000):
                ms = round(rng.uniform(10, 30) if rng.random() < 0.85 else rng.uniform(30, 120), 3)
                cpu = "%.3f" % (ms * rng.uniform(scale - 0.06, scale + 0.06))
                gpu = cpu if rng.random() < 0.25 else "%.3f" % (ms * rng.uniform(scale - 0.06, scale + 0.06))
                t += ms
                lines.append("%.3f,%s,%s,%s" % (t, repr(ms), cpu, gpu))
            add(write(d, "edge%d.ftlog" % k, "\n".join(lines) + "\n"), skip=0.0)
        # GPU/CPU balance swept across the 0.4 and 0.6 bound thresholds
        for k, off in enumerate((-0.045, -0.03, -0.015, -0.005, 0.005, 0.015, 0.03, 0.045)):
            lines, t = ["# frametime-log v1"], 0.0
            for _ in range(4000):
                ms = round(rng.uniform(15, 45), 3)
                t += ms
                lines.append("%.3f,%s,%.3f,%.3f" % (t, repr(ms), ms * rng.uniform(0.75, 0.95),
                                                    ms * rng.uniform(0.75 + off * 4, 0.95 + off * 4)))
            add(write(d, "bal%d.ftlog" % k, "\n".join(lines) + "\n"), skip=0.0)
        # usage samples: irregular times, some keys missing, junk values, samples outside the window,
        # and frame times around the 200 ms freeze threshold
        lines, t = ["# frametime-log v1", "# sample 0 cpu=1"], 0.0
        for i in range(5000):
            ms = round(rng.uniform(150, 260) if rng.random() < 0.05 else rng.uniform(20, 60), 3)
            t += ms
            lines.append("%.3f,%s" % (t, repr(ms)))
            if rng.random() < 0.05:
                fields = ["cpu=%.1f" % rng.uniform(0, 100), "ram=%.1f" % rng.uniform(500, 9000)]
                if rng.random() < 0.7:
                    fields += ["gpu=%.1f" % rng.uniform(0, 100), "vram=%.1f" % rng.uniform(100, 8000)]
                fields += rng.choice([["syscpu=%.2f" % rng.uniform(0, 100)], ["sysram=-3"], ["oops"], []])
                lines.append("# sample %.3f %s" % (t - rng.uniform(0, 30), " ".join(fields)))
        lines.append("# sample %.3f cpu=100" % (t + 5000))
        usage_log = write(d, "usage.ftlog", "\n".join(lines) + "\n")
        add(usage_log)
        add(usage_log, skip=0.0, maxSeconds=100.0)
        add(usage_log, skip=60.0, maxSeconds=33.3, minSeconds=0.0)

        # busy times on 55% of frames: just above the 50% the analysis needs
        lines, t = ["# frametime-log v1"], 0.0
        for i in range(4000):
            ms = round(rng.uniform(15, 45), 3)
            t += ms
            if i % 20 < 11:
                lines.append("%.3f,%s,%.3f,%.3f" % (t, repr(ms), ms * 0.9, ms * 0.8))
            else:
                lines.append("%.3f,%s" % (t, repr(ms)))
        add(write(d, "partial.ftlog", "\n".join(lines) + "\n"), skip=0.0)
        add(write(d, "bad.ftlog", "# frametime-log v1\n1,abc\n"))
        # cold boot: the launch clock moves frames, marks and samples; bad values; presentmon with --set
        boot = jitter_log(rng, 6000, busy_every=2, marks=[(30000.0, "menu"), (60000.0, "city")],
                          meta=[("capture.firstPresentMs", "11180.25"), ("settings.shaderCache", "cold")])
        boot = boot.replace("t_ms,frame_ms,cpu_ms,gpu_ms\n", "t_ms,frame_ms,cpu_ms,gpu_ms\n# sample 5.5 cpu=40 gpu=12\n"
                            "# sample 20000 cpu=80 gpu=97.5\n")
        boot_log = write(d, "boot.ftlog", boot)
        add(boot_log, skip=0.0, set=["capture.phase=cold-boot"])
        add(boot_log, skip=0.0, maxSeconds=60.0, minSeconds=0.0, set=["capture.phase=cold-boot"])
        add(boot_log, set=["capture.phase=cold-boot"])                       # skip 30: a problem
        add(boot_log, fromMark="menu", skip=0.0, set=["capture.phase=cold-boot"])  # from a mark: a problem
        add(boot_log, fromMark="menu", untilMark="city", skip=0.0,
            set=["capture.clockStart=launch", "capture.phase=scene"])
        add(boot_log, skip=0.0, set=["capture.clockStart=first-present"])
        add(boot_log, skip=0.0, set=["capture.phase=cold-boot", "capture.firstPresentMs=0"])
        add(boot_log, skip=0.0, set=["capture.phase=Cold-Boot", "capture.clockStart=yes", "settings.shaderCache=1",
                                     "capture.firstPresentMs=-4"])
        add(boot_log, skip=0.0, set=["capture.phase=cold-boot", "capture.firstPresentMs=soon"])
        add(boot_log, skip=0.0, set=["capture.clockStart=launch", "capture.firstPresentMs=true"])

        pm = write(d, "pm2.csv", presentmon_2x(rng, 12000, apps=("game.exe", "dwm.exe")))
        add(pm)
        add(pm, process="dwm.exe", skip=0.0, set=["scene=menu", "settings.profile=series-s"])
        add(pm, process="nope.exe")
        add(pm, skip=0.0, set=["capture.phase=cold-boot", "capture.firstPresentMs=9000.5"])
        add(write(d, "pmq.csv", presentmon_2x(rng, 8000, apps=("my, game.exe",), quoted=True)), skip=10.0)
        add(write(d, "cfx.csv", "// CapFrameX\nGameName,Test\n\nApplication,MsBetweenPresents\n"
                  + "\n".join("g.exe,%.3f" % rng.uniform(15, 25) for _ in range(9000)) + "\n", crlf=True),
            format="capframex")
        add(write(d, "none.txt", "hello\nworld\n"))
        cls.cases = cases

        cases_path = os.path.join(d, "cases.json")
        with open(cases_path, "w", encoding="utf-8") as f:
            json.dump(cases, f)
        out = subprocess.run([NODE, os.path.join(HERE, "js_report.js"), cases_path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        cls.js = json.loads(out.stdout.decode("utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_reports_are_identical(self):
        self.assertEqual(len(self.js), len(self.cases))
        for case, js in zip(self.cases, self.js):
            with self.subTest(case=case["source"], options=case["options"]):
                try:
                    py = {"report": ftreport.build_report_from_text(
                        ftreport.read_text(case["path"]), case["source"], case["options"])}
                except ftreport.FtError as e:
                    py = {"error": str(e)}
                self.assertEqual(set(py), set(js), "one side failed: py=%r js=%r" % (py, js))
                if "error" in py:
                    continue
                # compare through JSON so 30.0 and 30 are the same number
                self.assertEqual(json.loads(json.dumps(py["report"])), js["report"])

    def test_like_for_like_keys_match(self):
        core = os.path.join(os.path.dirname(HERE), "docs", "ftreport-core.js").replace("\\", "/")
        out = subprocess.run([NODE, "-e", "process.stdout.write(JSON.stringify(require(%s).LIKE_FOR_LIKE_KEYS))"
                              % json.dumps(core)], stdout=subprocess.PIPE, check=True)
        self.assertEqual(json.loads(out.stdout.decode("utf-8")), list(ftreport.LIKE_FOR_LIKE_KEYS))

    def test_cases_cover_busy_and_errors(self):
        busy = sum(1 for j in self.js if "report" in j and "busy" in j["report"]["result"])
        errors = sum(1 for j in self.js if "error" in j)
        self.assertGreaterEqual(busy, 4)
        self.assertGreaterEqual(errors, 3)


if __name__ == "__main__":
    unittest.main()
