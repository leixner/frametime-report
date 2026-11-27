"""Check a log written by a recorder demo.

Usage: python -I tests/check_demo_log.py <log> [--windows]

--windows: also require the hardware names and usage samples the recorders
write on Windows (run the demo with FTLOG_SAMPLE=100).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ftreport  # noqa: E402

windows = "--windows" in sys.argv[2:]
with open(sys.argv[1], encoding="utf-8") as f:
    text = f.read()
problems = []
try:
    cap = ftreport.read_ftlog(text)
except ftreport.FtError as e:
    print("FAIL: %s" % e)
    sys.exit(1)

if len(cap.frames) != 119:
    problems.append("expected 119 frames, got %d" % len(cap.frames))
if "loaded-scene" not in cap.marks:
    problems.append("mark 'loaded-scene' missing (have %s)" % sorted(cap.marks))
if ftreport.get_dotted(cap.meta, "settings.profile") != "demo":
    problems.append("settings.profile is not 'demo'")
if cap.meta.get("weird_key x") != "multi line value":
    problems.append("unsafe meta key/value not cleaned: %r" % {k: v for k, v in cap.meta.items() if k.startswith("weird")})
for line in text.splitlines():
    if line and not line.startswith("#") and not line.startswith("t_ms") and line.count(",") not in (1, 3):
        problems.append("bad frame line %r (locale?)" % line)
        break
    if line.startswith("# sample ") and "," in line:
        problems.append("decimal comma in sample line %r (locale?)" % line)
        break

pc = cap.meta.get("pc") or {}
if windows:
    for key in ("cpu", "gpu", "ramGB", "os", "cores"):
        if key not in pc:
            problems.append("hardware name pc.%s missing" % key)
    if not str(pc.get("os", "")).startswith("Windows"):
        problems.append("pc.os is %r" % pc.get("os"))
    if len(cap.samples) < 3:
        problems.append("expected usage samples (FTLOG_SAMPLE=100), got %d" % len(cap.samples))
    elif not all("cpu" in v and "ram" in v and "sysram" in v for _, v in cap.samples):
        problems.append("samples without cpu/ram/sysram: %r" % cap.samples[:2])

summary = ""
if "loaded-scene" in cap.marks and cap.frames:
    idx = ftreport.window(cap, 0, "loaded-scene", 0)
    frames = [cap.frames[i] for i in idx]
    result, _ = ftreport.compute(frames)
    if result["stutters"] < 1:
        problems.append("the slow demo frames were not counted as stutters")
    busy = ftreport.analyze_busy(frames, [cap.cpu[i] for i in idx], [cap.gpu[i] for i in idx])
    # the demos report cpu 5.0 ms and gpu 7.5 ms on every frame after the first 10
    want = {"cpuMs": 5.0, "gpuMs": 7.5, "gpuBoundShare": 1.0}
    if not busy or any(busy.get(k) != v for k, v in want.items()):
        problems.append("busy times not recorded as expected: %r" % (busy,))
    without = sum(1 for c in cap.cpu if c is None)
    if without != 9:   # 10 plain frame() calls; the first only starts the clock
        problems.append("expected 9 frames without busy times (plain frame()), got %d" % without)
    summary = ", avg %.1f fps, stutters %d, busy %s" % (
        result["avgFps"], result["stutters"], busy and busy["limit"])
print("%s: %d frames, marks %s%s" % (os.path.basename(sys.argv[1]), len(cap.frames), sorted(cap.marks), summary))
if pc or cap.samples:
    print("  pc: %s | %d samples, last %r" % (pc, len(cap.samples), cap.samples[-1][1] if cap.samples else None))
for p in problems:
    print("FAIL: " + p)
sys.exit(1 if problems else 0)
