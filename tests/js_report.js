// Runs docs/ftreport-core.js from Node for the parity test.
// Usage: node tests/js_report.js <cases.json>
// cases.json: [{ "path": "...", "source": "...", "options": {...} }, ...]
// Prints a JSON array with either { "report": ... } or { "error": "..." } per case.
"use strict";
const fs = require("fs");
const path = require("path");
const core = require(path.join(__dirname, "..", "docs", "ftreport-core.js"));

const cases = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = cases.map((c) => {
  try {
    const text = fs.readFileSync(c.path, "utf8");
    return { report: core.buildReport(text, c.source, c.options) };
  } catch (e) {
    if (e instanceof core.FtError) return { error: e.message };
    throw e;
  }
});
process.stdout.write(JSON.stringify(out));
