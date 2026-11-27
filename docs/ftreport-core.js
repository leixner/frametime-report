/* ftreport-core.js: the ftreport pipeline for the browser and Node.
 *
 * A step-by-step mirror of ftreport.py: same parsing rules, same sums in the
 * same order, same rounding (half away from zero). tests/test_parity.py runs
 * both on the same inputs and requires identical reports.
 *
 * Browser: window.FtReport.buildReport(text, sourceName, options)
 * Node:    require("./ftreport-core.js").buildReport(...)
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.FtReport = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var VERSION = "0.1.1";
  var FORMAT = "frametime-report/0.1";
  var LOG_MAGIC = "# frametime-log v1";
  var LIKE_FOR_LIKE_KEYS = ["settings.profile", "settings.output", "settings.internal", "settings.raytracing",
                            "settings.upscaler", "settings.fpsCap", "scene"];
  var FRAME_COLUMNS = ["MsBetweenPresents", "FrameTime", "msBetweenPresents"];
  var CPU_BUSY_COLUMNS = ["CPUBusy"];
  var GPU_BUSY_COLUMNS = ["GPUBusy", "MsGPUActive", "msGPUActive"];
  var APP_COLUMNS = ["Application", "ProcessName"];
  var SWAPCHAIN_COLUMNS = ["SwapChainAddress", "SwapChain"];
  var STUTTER_FACTOR = 2.5;
  var BUSY_MIN_SHARE = 0.5;
  var PACING_FACTOR = 0.75;
  var GPU_BOUND_AT = 0.6;
  var CPU_BOUND_AT = 0.4;
  var NUMBER_RE = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
  var INT_RE = /^[+-]?\d+$/;

  function FtError(message) {
    this.name = "FtError";
    this.message = message;
  }
  FtError.prototype = Object.create(Error.prototype);

  // ---- numbers: identical rules in ftreport.py

  function r2(x) {
    var v = Math.floor(Math.abs(x) * 100 + 0.5) / 100;
    return x >= 0 ? v : -v;
  }

  function fmt0(x) {
    return String(Math.floor(x + 0.5));
  }

  function strip(s) {
    return String(s == null ? "" : s).replace(/^\s+|\s+$/g, "");
  }

  function num(text) {
    var t = strip(text);
    if (!NUMBER_RE.test(t)) return null;
    var v = Number(t);
    return isFinite(v) ? v : null;
  }

  function total(values) {
    var s = 0.0;
    for (var i = 0; i < values.length; i++) s += values[i];
    return s;
  }

  function sortedCopy(values) {
    return values.slice().sort(function (a, b) { return a - b; });
  }

  function median(s) {
    var n = s.length;
    if (n % 2) return s[(n - 1) / 2];
    return (s[n / 2 - 1] + s[n / 2]) / 2;
  }

  function nearestRank(s, p) {
    var n = s.length;
    var k = Math.max(1, Math.ceil(p / 100.0 * n));
    return s[Math.min(k, n) - 1];
  }

  function splitLines(text) {
    var lines = text.split(/\r\n|\r|\n/);
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    return lines;
  }

  // ---- values and dotted keys

  function parseValue(text) {
    var t = strip(text);
    var low = t.toLowerCase();
    if (low === "true" || low === "yes" || low === "on") return true;
    if (low === "false" || low === "no" || low === "off") return false;
    if (INT_RE.test(t)) return parseInt(t, 10);
    var v = num(t);
    return v !== null ? v : t;
  }

  function setDotted(d, key, value) {
    var parts = strip(key).split(".").filter(function (p) { return p; });
    if (!parts.length) throw new FtError("empty metadata key");
    var cur = d;
    for (var i = 0; i < parts.length - 1; i++) {
      var nxt = cur[parts[i]];
      if (!nxt || typeof nxt !== "object" || Array.isArray(nxt)) {
        nxt = {};
        cur[parts[i]] = nxt;
      }
      cur = nxt;
    }
    cur[parts[parts.length - 1]] = value;
  }

  function getDotted(d, key) {
    var cur = d;
    var parts = key.split(".");
    for (var i = 0; i < parts.length; i++) {
      if (!cur || typeof cur !== "object" || !Object.prototype.hasOwnProperty.call(cur, parts[i])) return null;
      cur = cur[parts[i]];
    }
    return cur === undefined ? null : cur;
  }

  // ---- readers

  function Capture(tool) {
    this.tool = tool;
    this.frames = []; this.ends = []; this.cpu = []; this.gpu = [];
    this.meta = {}; this.marks = {}; this.notes = [];
  }
  Capture.prototype.add = function (ms, end, cpu, gpu) {
    if (end === null || end === undefined) end = (this.ends.length ? this.ends[this.ends.length - 1] : 0.0) + ms;
    this.frames.push(ms); this.ends.push(end);
    this.cpu.push(cpu === undefined ? null : cpu); this.gpu.push(gpu === undefined ? null : gpu);
  };

  function stripBom(text) {
    return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  }

  function detectFormat(text) {
    var lines = splitLines(text), first = "";
    for (var i = 0; i < lines.length; i++) if (strip(lines[i])) { first = lines[i]; break; }
    if (strip(first).toLowerCase() === LOG_MAGIC) return "ftlog";
    var head = text.slice(0, 20000);
    for (var j = 0; j < FRAME_COLUMNS.length; j++) if (head.indexOf(FRAME_COLUMNS[j]) >= 0) return "presentmon";
    throw new FtError("unknown input format (expected '" + LOG_MAGIC + "' or a PresentMon/CapFrameX CSV)");
  }

  function busyValue(text) {
    var v = num(text);
    return v !== null && v >= 0 ? v : null;
  }

  function readFtlog(text) {
    var lines = splitLines(text);
    if (!lines.length || strip(lines[0]).toLowerCase() !== LOG_MAGIC) throw new FtError("not a frametime-log v1 file");
    var cap = new Capture("ftlog");
    for (var li = 1; li < lines.length; li++) {
      var no = li + 1, line = strip(lines[li]);
      if (!line) continue;
      if (line.charAt(0) === "#") {
        var body = strip(line.slice(1));
        if (body.indexOf("meta ") === 0) {
          var kv = body.slice(5), eq = kv.indexOf("=");
          if (eq < 0) throw new FtError("line " + no + ": meta needs key=value");
          setDotted(cap.meta, kv.slice(0, eq), parseValue(kv.slice(eq + 1)));
        } else if (body.indexOf("mark ") === 0) {
          var m = /^\s*(\S+)\s+([\s\S]+)$/.exec(body.slice(5));
          if (!m) throw new FtError("line " + no + ": mark needs '<t_ms> <label>'");
          var t = num(m[1]);
          if (t === null) throw new FtError("line " + no + ": bad mark time");
          var label = strip(m[2]);
          if (!Object.prototype.hasOwnProperty.call(cap.marks, label)) cap.marks[label] = t;
        }
        continue;
      }
      if (line.toLowerCase().indexOf("t_ms") === 0) continue;
      var parts = line.split(","), te, ms;
      if (parts.length >= 2) {
        te = num(parts[0]); ms = num(parts[1]);
        if (te === null || ms === null) throw new FtError("line " + no + ": expected '<t_ms>,<frame_ms>[,<cpu_ms>,<gpu_ms>]'");
      } else {
        te = null; ms = num(parts[0]);
        if (ms === null) throw new FtError("line " + no + ": expected '<t_ms>,<frame_ms>'");
      }
      if (ms <= 0) continue;
      cap.add(ms, te, parts.length > 2 ? busyValue(parts[2]) : null, parts.length > 3 ? busyValue(parts[3]) : null);
    }
    return cap;
  }

  /* CSV with quoted fields ("a, b" and "" escapes), like Python's csv.reader. */
  function parseCsv(text) {
    var rows = [], row = [], field = "", i = 0, q = false, c;
    while (i < text.length) {
      c = text.charAt(i);
      if (q) {
        if (c === '"') {
          if (text.charAt(i + 1) === '"') { field += '"'; i += 2; continue; }
          q = false; i++; continue;
        }
        field += c; i++; continue;
      }
      if (c === '"' && field === "") { q = true; i++; continue; }
      if (c === ",") { row.push(field); field = ""; i++; continue; }
      if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; i++; continue; }
      field += c; i++;
    }
    if (field !== "" || row.length) { row.push(field); rows.push(row); }
    return rows.filter(function (r) { return !(r.length === 1 && r[0] === ""); });
  }

  function countInOrder(items) {
    var keys = [], counts = {};
    items.forEach(function (x) {
      if (!Object.prototype.hasOwnProperty.call(counts, x)) { counts[x] = 0; keys.push(x); }
      counts[x]++;
    });
    return { keys: keys, counts: counts };
  }

  function mostCommon(c) {
    var best = null, bestN = -1;
    c.keys.forEach(function (k) { if (c.counts[k] > bestN) { best = k; bestN = c.counts[k]; } });
    return best;
  }

  function readPresentmon(text, process, column) {
    var lines = splitLines(text), start = -1;
    for (var i = 0; i < lines.length && start < 0; i++) {
      for (var j = 0; j < FRAME_COLUMNS.length; j++) if (lines[i].indexOf(FRAME_COLUMNS[j]) >= 0) { start = i; break; }
      if (start < 0 && column && lines[i].indexOf(column) >= 0) start = i;
    }
    if (start < 0) throw new FtError("no PresentMon header row found");
    var rows = parseCsv(lines.slice(start).join("\n"));
    var fields = rows.length ? rows[0].map(strip) : [];
    var col = column || null;
    if (!col) for (var k = 0; k < FRAME_COLUMNS.length; k++) if (fields.indexOf(FRAME_COLUMNS[k]) >= 0) { col = FRAME_COLUMNS[k]; break; }
    if (!col || fields.indexOf(col) < 0) throw new FtError("frame-time column not found (have: " + fields.slice(0, 12).join(", ") + ")");

    function indexOf(names) {
      for (var n = 0; n < names.length; n++) { var x = fields.indexOf(names[n]); if (x >= 0) return x; }
      return null;
    }
    var iMs = fields.indexOf(col), iApp = indexOf(APP_COLUMNS), iSc = indexOf(SWAPCHAIN_COLUMNS);
    var iCpu = indexOf(CPU_BUSY_COLUMNS), iGpu = indexOf(GPU_BUSY_COLUMNS);
    function cell(row, idx) { return idx !== null && idx < row.length ? row[idx] : ""; }

    var data = [];
    for (var r = 1; r < rows.length; r++) {
      var ms = num(cell(rows[r], iMs));
      if (ms === null || ms <= 0) continue;
      data.push([cell(rows[r], iApp), cell(rows[r], iSc), ms, busyValue(cell(rows[r], iCpu)), busyValue(cell(rows[r], iGpu))]);
    }
    if (!data.length) throw new FtError("no frame rows in column " + col);

    var cap = new Capture("presentmon:" + col);
    if (iApp !== null) {
      var apps = countInOrder(data.map(function (d) { return d[0]; }));
      cap.apps = apps.keys.map(function (k) { return { name: k, frames: apps.counts[k] }; });  // for the page
      if (process) {
        var want = process.toLowerCase();
        data = data.filter(function (d) { return d[0].toLowerCase() === want; });
        if (!data.length) throw new FtError("process '" + process + "' not in log (have: " + apps.keys.slice().sort().join(", ") + ")");
      } else if (apps.keys.length > 1) {
        var pick = mostCommon(apps);
        data = data.filter(function (d) { return d[0] === pick; });
        cap.notes.push("several processes in log; used " + pick + " (most frames), pick one with --process");
      }
    }
    if (iSc !== null) {
      var chains = countInOrder(data.map(function (d) { return d[1]; }));
      if (chains.keys.length > 1) {
        var pickSc = mostCommon(chains);
        data = data.filter(function (d) { return d[1] === pickSc; });
        cap.notes.push("several swap chains; used the one with most frames");
      }
    }
    data.forEach(function (d) { cap.add(d[2], null, d[3], d[4]); });
    if (iApp !== null && data[0][0]) cap.meta.app = data[0][0];
    return cap;
  }

  // ---- window and statistics

  function window_(cap, skipS, fromMark, maxS) {
    var startMs = 0.0;
    if (fromMark) {
      if (!Object.prototype.hasOwnProperty.call(cap.marks, fromMark)) {
        throw new FtError("mark '" + fromMark + "' not in log (have: " + (Object.keys(cap.marks).sort().join(", ") || "none") + ")");
      }
      startMs = cap.marks[fromMark];
    }
    startMs += skipS * 1000.0;
    var idx = [];
    for (var i = 0; i < cap.frames.length; i++) if (cap.ends[i] - cap.frames[i] >= startMs - 1e-9) idx.push(i);
    if (maxS) {
      var out = [], acc = 0.0;
      for (var j = 0; j < idx.length; j++) {
        if (acc + cap.frames[idx[j]] > maxS * 1000.0 + 1e-9) break;
        acc += cap.frames[idx[j]];
        out.push(idx[j]);
      }
      idx = out;
    }
    return idx;
  }

  function compute(frames) {
    var n = frames.length;
    if (!n) throw new FtError("no frames in the measured window");
    var sumMs = total(frames), s = sortedCopy(frames), med = median(s);
    var k = Math.max(1, Math.ceil(0.01 * n)), worst = s.slice(n - k), stutters = 0;
    for (var i = 0; i < n; i++) if (frames[i] > STUTTER_FACTOR * med) stutters++;
    return {
      result: {
        frames: n,
        avgFps: r2(n / (sumMs / 1000.0)),
        medianMs: r2(med),
        p95Ms: r2(nearestRank(s, 95)),
        p99Ms: r2(nearestRank(s, 99)),
        low1Fps: r2(1000.0 / (total(worst) / k)),
        stutters: stutters,
        maxMs: r2(s[s.length - 1])
      },
      seconds: sumMs / 1000.0
    };
  }

  function analyzeBusy(frames, cpu, gpu) {
    var both = [];
    for (var i = 0; i < frames.length; i++) if (cpu[i] !== null && gpu[i] !== null) both.push(i);
    if (!frames.length || both.length < BUSY_MIN_SHARE * frames.length) return null;
    var c = sortedCopy(both.map(function (x) { return cpu[x]; }));
    var g = sortedCopy(both.map(function (x) { return gpu[x]; }));
    var f = sortedCopy(both.map(function (x) { return frames[x]; }));
    var cpuMed = median(c), gpuMed = median(g), frameMed = median(f), gpuFirst = 0;
    both.forEach(function (x) { if (gpu[x] >= cpu[x]) gpuFirst++; });
    var share = gpuFirst / both.length, limit;
    if (Math.max(cpuMed, gpuMed) < PACING_FACTOR * frameMed) limit = "pacing";
    else if (share >= GPU_BOUND_AT) limit = "gpu";
    else if (share <= CPU_BOUND_AT) limit = "cpu";
    else limit = "mixed";
    return { frames: both.length, cpuMs: r2(cpuMed), gpuMs: r2(gpuMed), gpuBoundShare: r2(share), limit: limit };
  }

  // ---- report

  function defaultOptions() {
    return {
      format: "auto", process: null, column: null, skip: 30.0, fromMark: null,
      maxSeconds: 0.0, minSeconds: 180.0, require: ["settings.profile", "scene"], set: []
    };
  }

  /* Returns { report, capture, window } so a page can also draw the frames. */
  function analyze(text, source, opts) {
    text = stripBom(text);
    var o = defaultOptions();
    Object.keys(opts || {}).forEach(function (key) { if (opts[key] !== null && opts[key] !== undefined) o[key] = opts[key]; });
    var fmt = o.format !== "auto" ? o.format : detectFormat(text), cap;
    if (fmt === "ftlog") cap = readFtlog(text);
    else if (fmt === "presentmon" || fmt === "capframex") {
      cap = readPresentmon(text, o.process, o.column);
      if (fmt === "capframex") cap.tool = cap.tool.replace("presentmon", "capframex");
    } else throw new FtError("unknown format " + fmt);

    o.set.forEach(function (item) {
      var eq = item.indexOf("=");
      if (eq < 0) throw new FtError("--set needs key=value, got '" + item + "'");
      setDotted(cap.meta, item.slice(0, eq), parseValue(item.slice(eq + 1)));
    });

    if (fmt !== "ftlog" && o.fromMark) throw new FtError("--from-mark only works with frame logs");
    var idx = window_(cap, o.skip, o.fromMark, o.maxSeconds);
    var frames = idx.map(function (i) { return cap.frames[i]; });
    var stats = compute(frames), result = stats.result, seconds = stats.seconds;
    var busy = analyzeBusy(frames, idx.map(function (i) { return cap.cpu[i]; }), idx.map(function (i) { return cap.gpu[i]; }));
    if (busy) result.busy = busy;

    var report = { format: FORMAT };
    ["app", "build", "settings", "scene", "pc"].forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(cap.meta, key)) report[key] = cap.meta[key];
    });
    Object.keys(cap.meta).forEach(function (key) {
      if (!(key in report) && ["format", "capture", "result", "checks"].indexOf(key) < 0) report[key] = cap.meta[key];
    });
    report.capture = {
      tool: cap.tool, source: source, seconds: r2(seconds), skippedSeconds: o.skip,
      fromMark: o.fromMark, ftreport: VERSION
    };
    report.result = result;

    var problems = [];
    if (seconds < o.minSeconds) problems.push("window is " + fmt0(seconds) + " s, shorter than the " + fmt0(o.minSeconds) + " s minimum");
    if (result.frames < 100) problems.push("only " + result.frames + " frames in the window");
    o.require.forEach(function (key) {
      key = strip(key);
      if (key && getDotted(report, key) === null) problems.push("missing " + key + " (add it with --set " + key + "=...)");
    });
    report.checks = { valid: problems.length === 0, notes: problems.concat(cap.notes) };
    return { report: report, capture: cap, window: idx };
  }

  function buildReport(text, source, opts) {
    return analyze(text, source, opts).report;
  }

  function likeForLike(a, b) {
    return LIKE_FOR_LIKE_KEYS.filter(function (k) {
      return JSON.stringify(getDotted(a, k)) !== JSON.stringify(getDotted(b, k));
    });
  }

  return {
    VERSION: VERSION, FORMAT: FORMAT, FtError: FtError, LIKE_FOR_LIKE_KEYS: LIKE_FOR_LIKE_KEYS,
    r2: r2, num: num, parseValue: parseValue, parseCsv: parseCsv, getDotted: getDotted,
    readFtlog: readFtlog, readPresentmon: readPresentmon, compute: compute, analyzeBusy: analyzeBusy,
    analyze: analyze, buildReport: buildReport, likeForLike: likeForLike
  };
});
