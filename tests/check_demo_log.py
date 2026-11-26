"""Check a log written by a recorder demo. Usage: python -I tests/check_demo_log.py <log>"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ftreport  # noqa: E402

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
for p in problems:
    print("FAIL: " + p)
sys.exit(1 if problems else 0)
