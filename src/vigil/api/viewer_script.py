"""The provisional viewer's behaviour (08 §5, §8).

Three rules it holds to, each of which is a way an interface can lie if broken:

  * **The page holds no authoritative state.** It renders what the server reports, and
    re-reads from the server after every action rather than patching its own copy.
  * **Every control does what it says.** An action the lifecycle forbids is a *disabled*
    button carrying the API's own reason, never an enabled one that returns an error.
  * **All data reaches the DOM through `textContent`.** An operator note and a validator
    reason are untrusted text as far as the browser is concerned.

An unavailable measurement renders as "unavailable" with the reason, never as 0: on a
dashboard a zero and an absence look identical and only one of them is true.
"""

VIEWER_SCRIPT_LIVE = r"""<script>
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
"""
