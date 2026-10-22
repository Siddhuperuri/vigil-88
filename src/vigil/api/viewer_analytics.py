"""The provisional viewer's analytics panel (08 §3 Analytics, 03 §5).

Charts are inline SVG and plain elements, with no charting library: `03 §5` rejects one
until there is a need the hand-rolled version cannot meet, and a sparkline plus a few bars
is not that need.

The honesty rules matter more here than anywhere else in the page, because a dashboard is
where a wrong number is most readily believed:

  * a metric the server reports as unavailable renders as **"unavailable"** with the reason,
    never as 0 -- on a chart a zero and an absence are indistinguishable;
  * a camera with no recorded health history renders as **unknown**, never as 100% online,
    because a blind camera that looks healthy is the most dangerous thing this page could
    draw;
  * the dismissal rate is labelled as the dismissal rate, not as a false-positive rate;
  * a series with no data draws nothing rather than an empty chart frame, which would imply
    a measurement that was never made;
  * the caveats the server generated are rendered verbatim.
"""

VIEWER_SCRIPT_ANALYTICS = r"""// ---------------------------------------------------------------- analytics
function card(title) {
  const el = document.createElement("div"); el.className = "card";
  const h = document.createElement("h4"); h.textContent = title;
  el.append(h); return el;
}

function bars(parent, counts, { label = (k) => k } = {}) {
  // Inline SVG and plain divs: no chart library (03 section 5), and nothing is drawn for a
  // series that has no data -- an empty chart frame implies a measurement that was not made.
  if (!counts.length) {
    const none = document.createElement("div");
    none.className = "na"; none.textContent = "no data in this window";
    parent.append(none); return;
  }
  const max = Math.max(...counts.map((c) => c.count), 1);
  const wrap = document.createElement("div"); wrap.className = "bars";
  for (const c of counts) {
    const row = document.createElement("div"); row.className = "bar";
    const k = document.createElement("span"); k.textContent = label(c.key);
    const track = document.createElement("span"); track.className = "track";
    const fill = document.createElement("span"); fill.className = "fill";
    fill.style.width = `${Math.round((c.count / max) * 100)}%`;
    track.append(fill);
    const n = document.createElement("span"); n.className = "n num"; n.textContent = c.count;
    row.append(k, track, n); wrap.append(row);
  }
  parent.append(wrap);
}

function duration(parent, title, stat) {
  const el = card(title);
  const value = document.createElement("div"); value.className = "big num";
  if (!stat.available) {
    // Unavailable, never 0: on a dashboard a zero and an absence look identical and only one
    // of them is true (08 section 8).
    value.className = "big num na";
    value.textContent = "unavailable";
    const why = document.createElement("div"); why.className = "na";
    why.textContent = "no incident in this window has this measurement";
    el.append(value, why);
  } else {
    value.textContent = `${(stat.p50_ms / 1000).toFixed(1)} s`;
    const detail = document.createElement("div"); detail.className = "num na";
    detail.textContent = `p50 ${(stat.p50_ms / 1000).toFixed(1)}s  ` +
      `p95 ${(stat.p95_ms / 1000).toFixed(1)}s  max ${(stat.max_ms / 1000).toFixed(1)}s  ` +
      `n=${stat.count}`;
    el.append(value, detail);
  }
  parent.append(el);
}

function sparkline(parent, points) {
  const el = card(`Trend (${points.length} buckets)`);
  if (!points.length) { parent.append(el); return; }
  const peak = Math.max(...points.map((p) => p.count), 0);
  if (peak === 0) {
    // A flat line at the floor, labelled "peak 1" because the scale needed a denominator, is a
    // measurement nobody made. Nothing happened in this window; say that instead of drawing it.
    const none = document.createElement("div");
    none.className = "na";
    none.textContent = `no incidents in any of these ${points.length} buckets`;
    el.append(none);
    const span = document.createElement("div"); span.className = "num na";
    span.textContent = `${points[0].key} .. ${points[points.length - 1].key}`;
    el.append(span);
    parent.append(el);
    return;
  }
  const max = peak;
  const width = 600, height = 70, step = width / Math.max(points.length - 1, 1);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("id", "trend");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `incidents per bucket, peak ${max}`);
  const path = points
    .map((p, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)},${(height - (p.count / max) * (height - 6) - 3).toFixed(1)}`)
    .join(" ");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
  line.setAttribute("d", path);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "var(--signal)");
  line.setAttribute("stroke-width", "1.5");
  svg.append(line);
  el.append(svg);
  const range = document.createElement("div"); range.className = "num na";
  range.textContent = `${points[0].key} .. ${points[points.length - 1].key}   peak ${max}`;
  el.append(range);
  parent.append(el);
}

function statParams() {
  return new URLSearchParams({
    days: $("stat-days").value,
    bucket: $("stat-bucket").value,
  });
}

async function pollStats() {
  if (!persists) return;
  const box = $("stats");
  try {
    const body = await (await fetch(`/api/v1/analytics/summary?${statParams()}`)).json();
    $("stat-window").textContent = `${body.since_utc.slice(0, 16)} .. ${body.until_utc.slice(0, 16)}`;
    box.replaceChildren();

    const total = card("Incidents in window");
    const n = document.createElement("div"); n.className = "big num";
    n.textContent = body.incidents_total;
    const open = document.createElement("div"); open.className = "num na";
    open.textContent = `${body.open_now} open now`;
    total.append(n, open);
    if (!body.covers_full_window) {
      const warn = document.createElement("div"); warn.className = "warnbox";
      warn.textContent = body.earliest_record_utc
        ? `records begin ${body.earliest_record_utc.slice(0, 16)}`
        : "no records at all";
      total.append(warn);
    }
    box.append(total);

    sparkline(box, body.trend);

    const byType = card("By event type"); bars(byType, body.by_event_type); box.append(byType);
    const bySev = card("By severity"); bars(bySev, body.by_severity); box.append(bySev);
    const byStatus = card("By status"); bars(byStatus, body.by_status); box.append(byStatus);
    const byCam = card("By camera"); bars(byCam, body.by_camera); box.append(byCam);

    duration(box, "Detection latency", body.detection_latency);
    duration(box, "Time to acknowledge", body.acknowledgement);
    duration(box, "Time to resolve", body.resolution);
    duration(box, "Incident duration", body.duration);

    const dismissal = card("Dismissal rate by module version");
    if (!body.dismissal_rate.length) {
      const none = document.createElement("div");
      none.className = "na"; none.textContent = "no incidents in this window";
      dismissal.append(none);
    } else {
      for (const r of body.dismissal_rate) {
        const row = document.createElement("div"); row.className = "num";
        row.textContent = `${r.module_id} ${r.module_version}: ${r.dismissed}/${r.total} ` +
          `(${(r.ratio * 100).toFixed(1)}%)`;
        dismissal.append(row);
      }
      const note = document.createElement("div"); note.className = "na";
      note.textContent = "dismissal rate, not the detector's false-positive rate";
      dismissal.append(note);
    }
    box.append(dismissal);

    const candidates = card("Candidate outcomes");
    bars(candidates, body.candidate_outcomes); box.append(candidates);
    const reasons = card("Top rejection reasons");
    bars(reasons, body.rejection_reasons, { label: (k) => k.split(":")[0] });
    box.append(reasons);

    const cams = card("Camera availability");
    const entries = Object.entries(body.camera_availability);
    if (!entries.length) {
      const none = document.createElement("div");
      none.className = "na"; none.textContent = "no health history recorded";
      cams.append(none);
    } else {
      for (const [camera, totals] of entries) {
        const row = document.createElement("div");
        if (totals === null) {
          // Unknown, never 100%: a blind camera must not look healthy.
          row.className = "num na";
          row.textContent = `${camera}: unknown (no transitions recorded)`;
        } else {
          const all = Object.values(totals).reduce((a, b) => a + b, 0) || 1;
          const online = (totals.online || 0) / all;
          row.className = "num";
          row.textContent = `${camera}: ${(online * 100).toFixed(1)}% online`;
        }
        cams.append(row);
      }
    }
    box.append(cams);

    // The caveats are generated from the data, so they only appear when they apply.
    const list = $("stat-caveats");
    list.replaceChildren();
    for (const text of body.caveats) {
      const li = document.createElement("li"); li.textContent = text; list.append(li);
    }
  } catch (e) {
    box.replaceChildren();
    const err = document.createElement("div");
    err.className = "warnbox"; err.textContent = "analytics unavailable";
    box.append(err);
  }
}

const EXPORT_BUTTONS = [
  ["export-csv", "csv"], ["export-json", "json"], ["export-pdf", "pdf"],
];

async function wireExports() {
  for (const [id, format] of EXPORT_BUTTONS) {
    $(id).addEventListener("click", () => {
      const params = statParams();
      params.set("format", format);
      // A plain navigation, so the browser handles the download and the
      // Content-Disposition filename the server chose.
      window.location.href = `/api/v1/analytics/export?${params}`;
    });
  }
  for (const id of ["stat-days", "stat-bucket"]) {
    $(id).addEventListener("change", pollStats);
  }

  // Ask the server which formats it can actually produce. A button whose format is unavailable
  // is DISABLED and carries the server's own reason as its tooltip -- never enabled and then
  // answered with an error (08 section 8). PDF starts disabled in the markup, so a failed
  // probe leaves it disabled rather than offering something that cannot be delivered.
  try {
    const formats = await (await fetch("/api/v1/analytics/formats")).json();
    const byName = new Map(formats.map((f) => [f.format, f]));
    for (const [id, format] of EXPORT_BUTTONS) {
      const info = byName.get(format);
      const button = $(id);
      button.disabled = !(info && info.available);
      button.title = button.disabled
        ? (info && info.reason) || "this export format is not available on this install"
        : "";
    }
  } catch (e) {
    for (const [id] of EXPORT_BUTTONS) {
      $(id).title = "export availability unknown: the API did not answer";
    }
  }
}

setInterval(pollCameras, 1500); setInterval(pollMetrics, 1000);
setInterval(pollIncidents, 2000); setInterval(pollMutes, 1000);
setInterval(pollStats, 15000);
wireExports();
pollCameras(); pollMetrics(); checkPersistence(); pollIncidents(); pollMutes(); pollStats();
</script>"""
