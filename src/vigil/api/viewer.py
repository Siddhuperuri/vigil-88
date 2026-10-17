"""A minimal live viewer served at `/`.

Provisional by design: it exists so the stream, the metrics and the incident record can be
looked at, not to stand in for the operations console (08). It follows the console's rules even
so, because they are the rules that stop an interface lying:

  * **Every control does what it says.** The overlay checkboxes change the server-side stream;
    acknowledge, dismiss, escalate and annotate call the real endpoints and re-render from the
    server's answer. An action the lifecycle forbids is **disabled with its reason** from the
    API's `permissions` block -- never enabled-then-error (08 §8).
  * **Nothing is faked.** With no incident store the desk says so rather than showing an empty
    list as if that were good news. An evidence file served with an integrity mismatch is
    labelled; a capability that is unavailable names what would provide it.
  * **All data reaches the DOM through `textContent`,** never `innerHTML`: an operator note and
    a validator reason are untrusted text as far as the browser is concerned.
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

  /* Incident desk. Dense, hairline-ruled, colour reserved for severity (08 section 6). */
  #desk { padding:0 16px 16px; }
  #desk h2 { font-size:12px; letter-spacing:.12em; text-transform:uppercase; color:var(--text2);
             margin:16px 0 8px; font-weight:600; }
  #desk table { width:100%; border-collapse:collapse; font-size:12px; }
  #desk th { text-align:left; color:var(--dim); font-weight:500; padding:4px 8px;
             border-bottom:1px solid var(--line); white-space:nowrap; }
  #desk td { padding:4px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
  #desk tbody tr { cursor:pointer; }
  #desk tbody tr:hover { background:var(--raised); }
  #desk tbody tr.selected { background:var(--raised); }
  .sev { font-weight:600; }
  .sev-INFO { color:#5a93d4; } .sev-LOW { color:#8a9bb0; } .sev-MODERATE { color:var(--warn); }
  .sev-HIGH { color:#e8762c; } .sev-CRITICAL { color:var(--bad); }
  .pill { border:1px solid var(--line-strong,#3a404f); border-radius:2px; padding:0 5px;
          font-size:11px; color:var(--text2); }
  #detail { background:var(--panel); border:1px solid var(--line); border-radius:2px;
            margin-top:12px; padding:12px; }
  #detail h3 { margin:0 0 8px; font-size:13px; }
  #detail dl { display:grid; grid-template-columns:max-content 1fr; gap:2px 12px; margin:0 0 10px; }
  #detail dt { color:var(--dim); } #detail dd { margin:0; }
  #detail section { border-top:1px solid var(--line); padding-top:8px; margin-top:8px; }
  #detail ul { margin:0; padding-left:16px; }
  #detail li { margin:1px 0; }
  #actions { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; align-items:center; }
  #actions button { background:var(--raised); color:var(--text); border:1px solid var(--line-strong,#3a404f);
                    border-radius:2px; padding:4px 10px; font:inherit; cursor:pointer; }
  #actions button:disabled { color:var(--dim); cursor:not-allowed; border-color:var(--line); }
  #actions input { background:var(--void); color:var(--text); border:1px solid var(--line);
                   border-radius:2px; padding:4px 6px; font:inherit; min-width:220px; }
  .verdict-pass { color:var(--text2); } .verdict-reject { color:var(--bad); }
  .verdict-defer { color:var(--warn); }
  .warnbox { color:var(--warn); }
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
  <span><span class="k">mutes</span><span id="s-mutes" class="num">-</span></span>
</div>
<div id="grid"></div>
<div id="empty" hidden>No cameras are configured and enabled.</div>
<div id="desk">
  <h2>Incidents <span id="desk-count" class="num pill"></span>
      <span id="desk-note" class="num warnbox"></span></h2>
  <table>
    <thead><tr>
      <th>Reference</th><th>When</th><th>Severity</th><th>Type</th><th>Camera</th>
      <th>Status</th><th>Confidence</th><th>Latency</th><th>Evidence</th>
    </tr></thead>
    <tbody id="desk-rows"></tbody>
  </table>
  <div id="desk-empty" hidden>No incidents have been recorded.</div>
  <div id="detail" hidden></div>
</div>
<div id="note">Provisional viewer. The overlay is drawn on the server from the detections made on
that exact frame, and every incident control calls the real API. The operations console arrives in
a later phase.</div>
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

// ---------------------------------------------------------------- incident desk
let selected = null;
let persists = true;

const SEV = ["INFO", "LOW", "MODERATE", "HIGH", "CRITICAL"];
const time = (iso) => iso ? new Date(iso).toISOString().replace("T", " ").slice(0, 19) : "n/a";

function cell(row, text, cls) {
  const td = document.createElement("td");
  td.textContent = text;
  if (cls) td.className = cls;
  row.append(td);
  return td;
}

async function pollIncidents() {
  if (!persists) return;
  try {
    const page = await (await fetch("/api/v1/incidents?limit=25")).json();
    $("desk-count").textContent = String(page.total);
    $("desk-empty").hidden = page.incidents.length > 0;
    const body = $("desk-rows");
    body.replaceChildren();
    for (const i of page.incidents) {
      const tr = document.createElement("tr");
      if (i.incident_id === selected) tr.className = "selected";
      cell(tr, i.display_ref, "num");
      cell(tr, time(i.confirmed_utc), "num");
      cell(tr, `${i.severity} ${i.severity_score.toFixed(1)}`, "num sev sev-" + i.severity);
      cell(tr, i.event_type);
      cell(tr, i.camera_id);
      cell(tr, i.status + (i.has_note ? "  (noted)" : ""));
      cell(tr, i.confidence.toFixed(2), "num");
      cell(tr, fmt(i.detection_latency_ms, 0, " ms"), "num");
      // "pending" and "partial" are real states, not errors: evidence completes after the
      // post-roll window closes, and a partial bundle says in its manifest what is missing.
      cell(tr, `${i.evidence_count} / ${i.evidence_status || "n/a"}`, "num");
      tr.addEventListener("click", () => { selected = i.incident_id; showDetail(i.incident_id); pollIncidents(); });
      body.append(tr);
    }
  } catch (e) { /* the camera poll already reports an unreachable server */ }
}

function dl(parent, pairs) {
  const list = document.createElement("dl");
  for (const [k, v] of pairs) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.className = "num"; dd.textContent = v;
    list.append(dt, dd);
  }
  parent.append(list);
}

function section(parent, title) {
  const s = document.createElement("section");
  const h = document.createElement("h3"); h.textContent = title;
  s.append(h); parent.append(s); return s;
}

async function act(id, action, body) {
  await fetch(`/api/v1/incidents/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  showDetail(id); pollIncidents();
}

function actionBar(parent, detail) {
  const bar = document.createElement("div"); bar.id = "actions";
  const note = document.createElement("input");
  note.placeholder = "reason or note";
  note.setAttribute("aria-label", "reason or note");
  for (const name of ["acknowledge", "dismiss", "escalate", "annotate"]) {
    const permission = detail.permissions[name] || { allowed: false, reason: "unknown action" };
    const b = document.createElement("button");
    b.textContent = name;
    b.disabled = !permission.allowed;
    // Disabled controls say why. A control that is enabled does what it says (08 section 8).
    if (!permission.allowed && permission.reason) b.title = permission.reason;
    b.addEventListener("click", () => {
      const text = note.value.trim();
      act(detail.incident.incident_id, name, name === "annotate" ? { note: text } : { reason: text });
    });
    bar.append(b);
  }
  bar.append(note);
  parent.append(bar);
}

async function showDetail(id) {
  const box = $("detail");
  box.hidden = false;
  box.replaceChildren();
  const reply = await fetch(`/api/v1/incidents/${encodeURIComponent(id)}`);
  if (!reply.ok) { box.textContent = `incident unavailable (HTTP ${reply.status})`; return; }
  const d = await reply.json();
  const i = d.incident;

  const head = document.createElement("h3");
  head.textContent = `${i.display_ref}  ${i.severity} ${i.severity_score.toFixed(1)}  ${i.status}`;
  head.className = "sev sev-" + i.severity;
  box.append(head);

  const trigger = document.createElement("div");
  trigger.textContent = d.explanation.trigger_summary;
  box.append(trigger);

  actionBar(box, d);

  dl(box, [
    ["camera", `${i.camera_id}${i.location ? "  (" + i.location + ")" : ""}`],
    ["module", `${i.module_id} ${i.module_version}`],
    ["first observed", time(i.first_observed_utc)],
    ["confirmed", time(i.confirmed_utc)],
    ["last observed", time(i.last_observed_utc)],
    ["detection latency", fmt(i.detection_latency_ms, 0, " ms")],
    ["duration", i.duration_ms === null ? "ongoing" : fmt(i.duration_ms, 0, " ms")],
    ["confidence", i.confidence.toFixed(4)],
    ["zones", i.zone_ids.join(", ") || "none"],
    ["tracks", d.involved_track_ids.join(", ") || "none"],
    ["model", `${d.model.name} ${d.model.backend} ${d.model.device}`],
    ["config hash", String(d.system.config_hash).slice(0, 16) + "..."],
  ]);
  if (d.operator_note) {
    const n = section(box, "Operator note");
    const t = document.createElement("div"); t.textContent = d.operator_note; n.append(t);
  }
  if (d.explanation.capability_caveats.length) {
    const c = section(box, "Caveats");
    c.classList.add("warnbox");
    const ul = document.createElement("ul");
    for (const text of d.explanation.capability_caveats) {
      const li = document.createElement("li"); li.textContent = text; ul.append(li);
    }
    c.append(ul);
  }

  // The severity factors must sum to the score; showing both is what lets an operator argue
  // with a severity rather than just distrust it (06 section 8).
  const sev = section(box, "Severity factors");
  const sevList = document.createElement("ul");
  for (const f of d.explanation.severity_factors) {
    const li = document.createElement("li"); li.className = "num";
    li.textContent = `${f.name}: raw ${f.raw_value.toFixed(3)} x weight ${f.weight.toFixed(2)} = ${f.contribution.toFixed(4)}`;
    sevList.append(li);
  }
  const total = d.explanation.severity_factors.reduce((a, f) => a + f.contribution, 0);
  const sum = document.createElement("li"); sum.className = "num";
  sum.textContent = `sum x 100 = ${(total * 100).toFixed(2)}  (score ${i.severity_score.toFixed(2)})`;
  sevList.append(sum); sev.append(sevList);

  const ver = section(box, `Validators (${d.explanation.validator_verdicts.length})`);
  const verList = document.createElement("ul");
  for (const v of d.explanation.validator_verdicts) {
    const li = document.createElement("li");
    li.className = "verdict-" + v.outcome;
    li.textContent = `${v.outcome.toUpperCase()}  ${v.validator_id}: ${v.reason}`;
    verList.append(li);
  }
  ver.append(verList);

  const obs = section(box, `Observations (${d.explanation.observations.length})`);
  const obsList = document.createElement("ul");
  for (const o of d.explanation.observations.slice(-8)) {
    const li = document.createElement("li"); li.className = "num";
    li.textContent = `signal ${o.signal.toFixed(3)}  ` +
      Object.entries(o.facts).map(([k, v]) => `${k}=${v}`).join("  ");
    obsList.append(li);
  }
  obs.append(obsList);

  const ev = section(box, `Evidence (${d.evidence.length})`);
  if (!d.evidence.length) {
    const none = document.createElement("div");
    none.className = "warnbox";
    none.textContent = i.evidence_status === "pending"
      ? "capture is still open: evidence is written once the post-roll window closes"
      : `no evidence was captured (${i.evidence_status || "unknown"}) - see the incident manifest`;
    ev.append(none);
  } else {
    const evList = document.createElement("ul");
    for (const e of d.evidence) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = `/api/v1/incidents/${encodeURIComponent(i.incident_id)}/evidence/${encodeURIComponent(e.evidence_id)}`;
      a.target = "_blank"; a.rel = "noreferrer";
      a.textContent = `${e.kind}  ${(e.bytes / 1024).toFixed(1)} KiB  ${e.media_type}`;
      a.style.color = "var(--signal)";
      li.append(a);
      const hash = document.createElement("span");
      hash.className = "num"; hash.style.color = "var(--dim)";
      hash.textContent = `  sha256 ${e.sha256.slice(0, 12)}...`;
      li.append(hash);
      evList.append(li);
    }
    ev.append(evList);
  }

  const hist = section(box, "History");
  const histList = document.createElement("ul");
  for (const t of d.transitions) {
    const li = document.createElement("li"); li.className = "num";
    li.textContent = `${time(t.at_utc)}  ${t.from_status || "-"} -> ${t.to_status}  [${t.actor}]  ${t.reason}`;
    histList.append(li);
  }
  hist.append(histList);
}

async function pollMutes() {
  try {
    const mutes = await (await fetch("/api/v1/mutes")).json();
    // A mute is never silent: it is shown with a countdown, because a muted camera that looks
    // normal is how a real incident gets missed (06 section 10).
    $("s-mutes").textContent = mutes.length
      ? mutes.map((m) => `${m.camera_id || "all"}${m.event_type ? ":" + m.event_type : ""} ${Math.round(m.remaining_s)}s`).join("  ")
      : "none";
    $("s-mutes").className = "num " + (mutes.length ? "warn" : "");
  } catch (e) { /* reported elsewhere */ }
}

async function checkPersistence() {
  try {
    const h = await (await fetch("/api/v1/system/health")).json();
    persists = h.persists_incidents === true;
    $("desk-note").textContent = persists
      ? ""
      : "storage.enabled is false: events are analysed but no incident is stored";
    if (!persists) { $("desk-empty").hidden = true; $("desk-rows").replaceChildren(); }
  } catch (e) { /* reported elsewhere */ }
}

setInterval(pollCameras, 1500); setInterval(pollMetrics, 1000);
setInterval(pollIncidents, 2000); setInterval(pollMutes, 1000);
pollCameras(); pollMetrics(); checkPersistence(); pollIncidents(); pollMutes();
</script>
</body>
</html>
"""
