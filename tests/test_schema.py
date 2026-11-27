"""Reports written by ftreport.py must match docs/schema/report-0.1.schema.json.

Needs the jsonschema package (CI installs it); skipped without it.
"""

import glob
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import ftreport  # noqa: E402

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

SCHEMA_PATH = os.path.join(ROOT, "docs", "schema", "report-0.1.schema.json")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class SchemaFileTests(unittest.TestCase):
    def test_schema_is_json_and_versioned(self):
        schema = load(SCHEMA_PATH)
        self.assertEqual(schema["properties"]["format"]["const"], ftreport.FORMAT)
        self.assertEqual(set(schema["properties"]["result"]["properties"]["busy"]["properties"]["limit"]["enum"]),
                         {"gpu", "cpu", "mixed", "pacing"})


class PageExampleTests(unittest.TestCase):
    def test_page_example_is_the_repo_example(self):
        with open(os.path.join(ROOT, "docs", "example.ftlog"), "rb") as a:
            page = a.read().replace(b"\r\n", b"\n")
        with open(os.path.join(ROOT, "examples", "pc-a.ftlog"), "rb") as b:
            repo = b.read().replace(b"\r\n", b"\n")
        self.assertEqual(page, repo)


@unittest.skipUnless(jsonschema, "jsonschema not installed")
class SchemaValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load(SCHEMA_PATH)
        jsonschema.Draft202012Validator.check_schema(cls.schema)
        cls.validator = jsonschema.Draft202012Validator(cls.schema)

    def errors(self, report):
        return [e.message for e in self.validator.iter_errors(report)]

    def test_examples_match(self):
        paths = glob.glob(os.path.join(ROOT, "examples", "*.report.json"))
        self.assertTrue(paths)
        for p in paths:
            with self.subTest(p=os.path.basename(p)):
                self.assertEqual(self.errors(load(p)), [])

    def test_generated_reports_match(self):
        lines = ["# frametime-log v1", "# meta settings.profile=series-x", "# meta settings.raytracing=false",
                 "# meta build.commit=abc1234"]
        t = 0.0
        for i in range(6000):
            ms = 40.0 + (i % 7)
            t += ms
            lines.append("%.3f,%.3f,%.3f,%.3f" % (t, ms, ms * 0.9, ms * 0.7))
        text = "\n".join(lines) + "\n"
        for opts in ({}, {"set": ["scene=city-walk", "pc.ramGB=64", "pc.gpu=Example"]},
                     {"skip": 0.0, "minSeconds": 0.0, "require": [], "set": ["scene=custom:test"]},
                     {"skip": 0.0, "set": ["scene=menu", "capture.phase=cold-boot", "capture.firstPresentMs=11180.5",
                                           "settings.shaderCache=cold"]},
                     {"skip": 0.0, "set": ["capture.phase=cold_boot", "settings.shaderCache=hot"]}):
            with self.subTest(opts=opts):
                report = ftreport.build_report_from_text(text, "gen.ftlog", opts)
                self.assertIn("busy", report["result"])
                self.assertEqual(self.errors(report), [])

    def test_rejects_bad_reports(self):
        good = load(glob.glob(os.path.join(ROOT, "examples", "*.report.json"))[0])
        bad_cases = {
            "wrong format": dict(good, format="frametime-report/9"),
            "unknown scene": dict(good, scene="somewhere"),
            "fps not a number": dict(good, result=dict(good["result"], avgFps="23")),
            "bad limit": dict(good, result=dict(good["result"], busy={
                "frames": 1, "cpuMs": 1, "gpuMs": 1, "gpuBoundShare": 0.5, "limit": "fast"})),
            "no checks": {k: v for k, v in good.items() if k != "checks"},
            "unknown phase": dict(good, capture=dict(good["capture"], phase="cold_boot")),
            "negative first present": dict(good, capture=dict(good["capture"], firstPresentMs=-1)),
        }
        for name, report in bad_cases.items():
            with self.subTest(name=name):
                self.assertTrue(self.errors(report))


if __name__ == "__main__":
    unittest.main()
