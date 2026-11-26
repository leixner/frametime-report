"""Check a log written by a recorder demo. Usage: python -I tests/check_demo_log.py <log>"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ftreport  # noqa: E402

with open(sys.argv[1], encoding="utf-8") as f:
    text = f.read()
frames, ends, meta, marks, _ = ftreport.read_ftlog(text)
problems = []
if len(frames) != 119:
    problems.append("expected 119 frames, got %d" % len(frames))
if "loaded-scene" not in marks:
    problems.append("mark 'loaded-scene' missing (have %s)" % sorted(marks))
if ftreport.get_dotted(meta, "settings.profile") != "demo":
    problems.append("settings.profile is not 'demo'")
if meta.get("weird_key x") != "multi line value":
    problems.append("unsafe meta key/value not cleaned: %r" % {k: v for k, v in meta.items() if k.startswith("weird")})
for line in text.splitlines():
    if line and not line.startswith("#") and not line.startswith("t_ms") and line.count(",") != 1:
        problems.append("bad frame line %r (locale?)" % line)
        break
summary = ""
if "loaded-scene" in marks and frames:
    result, _ = ftreport.compute(ftreport.window(frames, ends, 0, marks, "loaded-scene", 0))
    if result["stutters"] < 1:
        problems.append("the slow demo frames were not counted as stutters")
    summary = ", avg %.1f fps, stutters %d" % (result["avgFps"], result["stutters"])
print("%s: %d frames, marks %s%s" % (os.path.basename(sys.argv[1]), len(frames), sorted(marks), summary))
for p in problems:
    print("FAIL: " + p)
sys.exit(1 if problems else 0)
