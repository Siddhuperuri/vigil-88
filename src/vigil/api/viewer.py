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

from vigil.api.viewer_analytics import VIEWER_SCRIPT_ANALYTICS
from vigil.api.viewer_script import VIEWER_SCRIPT_LIVE
from vigil.api.viewer_styles import VIEWER_STYLES

_DOCUMENT = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VIGIL-88 Live</title>
__VIGIL_STYLES__
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

  <h2>Analytics <span id="stat-window" class="num pill"></span></h2>
  <div id="stat-controls">
    <label>window
      <select id="stat-days">
        <option value="1">24 hours</option>
        <option value="7" selected>7 days</option>
        <option value="30">30 days</option>
        <option value="365">1 year</option>
      </select>
    </label>
    <label>bucket
      <select id="stat-bucket">
        <option value="hour">hour</option>
        <option value="day" selected>day</option>
        <option value="week">week</option>
        <option value="month">month</option>
      </select>
    </label>
    <button id="export-csv">export CSV</button>
    <button id="export-json">export JSON</button>
    <button id="export-pdf" disabled>export PDF</button>
  </div>
  <div id="stats"></div>
  <ul id="stat-caveats"></ul>
</div>
<div id="note">Provisional viewer. The overlay is drawn on the server from the detections made on
that exact frame, and every incident control calls the real API. The operations console arrives in
a later phase.</div>
__VIGIL_SCRIPT__
</body>
</html>
"""

# One `<script>` element, assembled from the live half (which opens the tag) and the analytics
# half (which closes it). They are separate files because they are separate concerns -- live
# view and operator desk against reporting -- not because the browser cares.
_SCRIPT = VIEWER_SCRIPT_LIVE + "\n" + VIEWER_SCRIPT_ANALYTICS

VIEWER_HTML = _DOCUMENT.replace("__VIGIL_STYLES__", VIEWER_STYLES).replace(
    "__VIGIL_SCRIPT__", _SCRIPT
)
