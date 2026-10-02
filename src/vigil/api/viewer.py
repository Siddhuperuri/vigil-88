"""A minimal live viewer served at `/`.

Provisional by design: it exists so the P1 stream and metrics can be looked at, not to stand in
for the operations console (08). Every control does what it says: the overlay checkboxes
change the server-side stream, and there is nothing else to click. All data reaches the DOM
through `textContent`, never `innerHTML`.
"""

VIEWER_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VIGIL-88 Live</title>
<style>
  :root {
    --void:#0a0b0d; --panel:#12141a; --raised:#1a1d25; --line:#262a35;
    --text:#e6e9ef; --text2:#9aa3b2; --dim:#6b7383; --signal:#2dd4bf;
    --warn:#e0a93b; --bad:#e5484d;
    --mono: ui-monospace, "Cascadia Mono", "JetBrains Mono", Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--void); color:var(--text);
         font:13px/1.4 system-ui, "Segoe UI", sans-serif; }
  .num { font-family:var(--mono); font-variant-numeric:tabular-nums; }
  #strip { display:flex; gap:20px; align-items:center; flex-wrap:wrap; padding:10px 16px;
           background:var(--panel); border-bottom:1px solid var(--line); position:sticky; top:0; }
  #strip .brand { font-weight:700; letter-spacing:.12em; }
  #strip .k { color:var(--dim); margin-right:6px; }
  #strip .warn { color:var(--warn); } #strip .bad { color:var(--bad); }
  #grid { display:grid; gap:12px; padding:16px;
          grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); }
  .tile { background:var(--panel); border:1px solid var(--line); border-radius:2px; }
  .tile header { display:flex; justify-content:space-between; gap:8px; padding:6px 10px;
                 border-bottom:1px solid var(--line); }
  .tile .state { text-transform:uppercase; letter-spacing:.06em; font-size:11px; }
  .state.online { color:var(--signal); } .state.degraded,.state.reconnecting { color:var(--warn); }
  .state.failed,.state.offline { color:var(--bad); }
  .tile img { display:block; width:100%; background:#000; min-height:120px; }
  .tile footer { display:flex; gap:14px; padding:6px 10px; border-top:1px solid var(--line);
                 color:var(--text2); flex-wrap:wrap; }
  label { cursor:pointer; user-select:none; }
  #empty, #note { color:var(--dim); padding:16px; }
</style>
</head>
<body>
<div id="strip">
  <span class="brand">VIGIL-88</span>
  <span><span class="k">state</span><span id="s-state" class="num">-</span></span>
  <span><span class="k">pipeline</span><span id="s-fps" class="num">-</span></span>
  <span><span class="k">infer p50/p95</span><span id="s-lat" class="num">-</span></span>
  <span><span class="k">capture-to-stream p95</span><span id="s-e2e" class="num">-</span></span>
  <span><span class="k">GPU</span><span id="s-gpu" class="num">-</span></span>
  <span><span class="k">CPU</span><span id="s-cpu" class="num">-</span></span>
  <span><span class="k">model</span><span id="s-model" class="num">-</span></span>
</div>
<div id="grid"></div>
<div id="empty" hidden>No cameras are configured and enabled.</div>
<div id="note">Provisional live viewer. The overlay is drawn on the server from the detections made
on that exact frame. The operations console arrives in a later phase.</div>
<script>
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 1, unit = "") => (v === null || v === undefined) ? "n/a" : v.toFixed(d) + unit;
const tiles = new Map();

function overlayQuery(tile) {
  const on = ["boxes", "labels", "info"].filter((k) => tile.checks[k].checked);
  return on.length ? on.join(",") : "none";
}
function setStream(tile) {
  tile.img.src = `/api/v1/cameras/${encodeURIComponent(tile.id)}/stream.mjpeg?overlay=${overlayQuery(tile)}`;
}
function makeTile(id) {
  const root = document.createElement("div"); root.className = "tile";
  const head = document.createElement("header");
  const name = document.createElement("span"); name.className = "num"; name.textContent = id;
  const state = document.createElement("span"); state.className = "state num";
  head.append(name, state);
  const img = document.createElement("img"); img.alt = "live stream for " + id;
  const foot = document.createElement("footer");
  const stats = document.createElement("span"); stats.className = "num";
  const checks = {};
  for (const k of ["boxes", "labels", "info"]) {
    const l = document.createElement("label"); const c = document.createElement("input");
    c.type = "checkbox"; c.checked = true; checks[k] = c;
    l.append(c, " " + k); foot.append(l);
  }
  foot.append(stats); root.append(head, img, foot);
  const tile = { id, root, img, state, stats, checks };
  for (const c of Object.values(checks)) c.addEventListener("change", () => setStream(tile));
  setStream(tile); $("grid").append(root); tiles.set(id, tile);
}
async function pollCameras() {
  try {
    const cams = await (await fetch("/api/v1/cameras")).json();
    $("empty").hidden = cams.length > 0;
    for (const c of cams) {
      if (!tiles.has(c.camera_id)) makeTile(c.camera_id);
      const t = tiles.get(c.camera_id);
      t.state.textContent = c.state; t.state.className = "state num " + c.state;
      t.stats.textContent = `${fmt(c.measured_fps, 1)} fps  rx ${c.frames_received}  ` +
        `skipped ${c.frames_skipped}  dropped ${c.frames_dropped}` + (c.detail ? "  | " + c.detail : "");
    }
  } catch (e) { $("s-state").textContent = "unreachable"; $("s-state").className = "num bad"; }
}
async function pollMetrics() {
  try {
    const [h, m] = await Promise.all([
      fetch("/api/v1/system/health").then((r) => r.json()),
      fetch("/api/v1/system/metrics").then((r) => r.json())]);
    const s = $("s-state"); s.textContent = `${h.state} / ${h.level}`;
    s.className = "num " + (h.level === "ok" ? "" : h.level === "failed" ? "bad" : "warn");
    $("s-fps").textContent = fmt(m.pipeline_fps, 1, " fps");
    $("s-lat").textContent = `${fmt(m.inference_latency.p50_ms)} / ${fmt(m.inference_latency.p95_ms)} ms`;
    const e2e = Object.values(m.end_to_end)[0];
    $("s-e2e").textContent = e2e ? fmt(e2e.p95_ms, 1, " ms") : "n/a";
    const sy = m.system;
    $("s-gpu").textContent = sy && sy.gpu_utilization_percent !== null
      ? `${fmt(sy.gpu_utilization_percent, 0, "%")} ${fmt(sy.gpu_memory_used_mb, 0, " MB")} ${fmt(sy.gpu_temperature_c, 0, "C")}`
      : "n/a";
    $("s-gpu").className = "num " + (sy && sy.gpu_throttle_reasons.length ? "warn" : "");
    $("s-cpu").textContent = sy ? fmt(sy.cpu_percent, 0, "%") : "n/a";
    $("s-model").textContent = h.detector ? `${h.detector.name} ${h.detector.input_size_px || ""}px ${h.detector.device}` : "none";
  } catch (e) { /* the camera poll already reports an unreachable server */ }
}
setInterval(pollCameras, 1500); setInterval(pollMetrics, 1000); pollCameras(); pollMetrics();
</script>
</body>
</html>
"""
