// D0 spike (plan section 5.2): launches the real shell against a 4-camera engine and measures the
// three budgets that can reverse the Electron choice. Prints one JSON object. Windows only.
//   node scripts/spike.mjs [--detector yolox_s] [--models <dir>] [--seconds 25]
import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const electronExe = require("electron");
const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const arg = (name, fallback) => {
  const at = process.argv.indexOf(`--${name}`);
  return at >= 0 ? process.argv[at + 1] : fallback;
};
const detector = arg("detector");
const models = arg("models");
const seconds = Number(arg("seconds", "25"));

const root = mkdtempSync(path.join(tmpdir(), "vigil-spike-"));
const config = path.join(root, "config");
mkdirSync(path.join(config, "cameras"), { recursive: true });
writeFileSync(
  path.join(config, "vigil.yaml"),
  `version: 1
paths:
  data_dir: ../var
${models ? `  models_dir: ${models.replaceAll("\\", "/")}\n` : ""}logging:
  level: ERROR
pipeline:
  global_inference_fps: 60
cameras:
  - $include: cameras/*.yaml
`,
);
for (const id of [1, 2, 3, 4]) {
  writeFileSync(
    path.join(config, "cameras", `cam${id}.yaml`),
    `camera_id: cam${id}
name: Synthetic ${id}
enabled: true
target_inference_fps: 10
source:
  kind: synthetic
  realtime: true
  fps: 15
  width_px: 640
  height_px: 480
`,
  );
}

const ps = (script) =>
  execFileSync("powershell", ["-NoProfile", "-NonInteractive", "-Command", script], {
    encoding: "utf8",
    windowsHide: true,
  });
const vramMb = () =>
  Number(
    execFileSync("nvidia-smi", ["--query-gpu=memory.used", "--format=csv,noheader,nounits"], {
      encoding: "utf8",
    }).trim(),
  );

/** Memory of a process tree, split into the shell (Chromium) side and the engine side. */
function treeMemory(rootPid) {
  const out = ps(`
$all = Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name
$kids = @{}; foreach ($p in $all) { if (-not $kids.ContainsKey($p.ParentProcessId)) { $kids[$p.ParentProcessId] = @() }; $kids[$p.ParentProcessId] += $p }
function Walk($id, $engine) {
  foreach ($c in $kids[[uint32]$id]) {
    $isEngine = $engine -or ($c.Name -match '^(uv|python|pythonw)[.]exe$')
    [pscustomobject]@{ Id=$c.ProcessId; Name=$c.Name; Engine=$isEngine }
    Walk $c.ProcessId $isEngine
  }
}
$tree = @([pscustomobject]@{ Id=${rootPid}; Name='electron.exe'; Engine=$false }) + @(Walk ${rootPid} $false)
$rows = foreach ($t in $tree) { $g = Get-Process -Id $t.Id -ErrorAction SilentlyContinue; if ($g) { [pscustomobject]@{ Name=$t.Name; Engine=$t.Engine; Private=$g.PrivateMemorySize64; WS=$g.WorkingSet64; Cpu=$g.CPU } } }
$rows | ConvertTo-Json -Compress`);
  const rows = JSON.parse(out || "[]");
  const list = Array.isArray(rows) ? rows : [rows];
  const sum = (filter, key) =>
    Math.round(list.filter(filter).reduce((n, r) => n + r[key], 0) / 2 ** 20);
  return {
    shell_processes: list.filter((r) => !r.Engine).length,
    shell_private_mb: sum((r) => !r.Engine, "Private"),
    shell_working_set_mb: sum((r) => !r.Engine, "WS"),
    engine_private_mb: sum((r) => r.Engine, "Private"),
    cpu_seconds: Object.fromEntries(list.map((r, i) => [`${i}:${r.Name}`, Math.round(r.Cpu)])),
    ids: list.length,
  };
}

const vramBefore = vramMb();
const launched = Date.now();
const env = {
  ...process.env,
  VIGIL_UI_DIR: path.join(desktop, "scripts", "spike-ui"),
  VIGIL_ENGINE_CONFIG_DIR: config,
  ...(detector ? { VIGIL_DETECTOR: detector } : {}),
};
const child = spawn(electronExe, [desktop, `--user-data-dir=${path.join(root, "user")}`], {
  env,
  stdio: ["ignore", "pipe", "pipe"],
});
const marks = {};
child.stdout.setEncoding("utf8");
let buffer = "";
child.stdout.on("data", (chunk) => {
  buffer += chunk;
  for (const line of buffer.split("\n").slice(0, -1)) {
    try {
      const m = JSON.parse(line);
      if (m.shell) marks[m.shell + (m.title ? `:${m.title}` : "")] ??= Date.now() - launched;
    } catch {
      /* engine or Chromium noise */
    }
  }
  buffer = buffer.slice(buffer.lastIndexOf("\n") + 1);
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const deadline = Date.now() + 150_000;
while (!marks["title:first-frame"] && Date.now() < deadline) await sleep(200);
const result = {
  detector: detector ?? "null",
  first_frame_ms: marks["title:first-frame"] ?? null,
  marks,
};
if (marks["title:first-frame"]) {
  await sleep(seconds * 1000);
  result.memory_after_steady = treeMemory(child.pid);
  result.vram_before_mb = vramBefore;
  result.vram_after_mb = vramMb();
  result.vram_delta_mb = result.vram_after_mb - vramBefore;
}
child.kill();
await sleep(3000);
console.log(JSON.stringify(result, null, 2));
process.exit(marks["title:first-frame"] ? 0 : 1);
