import { useState } from "react";
import type { Logs, Role, Storage } from "../api/types";
import { useFetch } from "../api/useFetch";
import { Notice, StateLabel, bytes, readout } from "../components/bits";
import { useTelemetry } from "../state/telemetry";
import { Capabilities } from "./Capabilities";

const text = (value: unknown): string => (typeof value === "string" ? value : "");

function Health() {
  const { snapshot } = useTelemetry();
  if (snapshot === null)
    return (
      <p role="status" className="muted">
        Waiting for the engine…
      </p>
    );
  const { health, cameras, system, uptime_ms: uptime } = snapshot;
  return (
    <>
      <h2>Engine</h2>
      <p>
        State <strong className="num">{snapshot.state.toUpperCase()}</strong> · health{" "}
        <strong className="num">{health.level.toUpperCase()}</strong> · up{" "}
        <span className="num">
          {uptime === null ? "unavailable" : `${String(Math.round(uptime / 60000))} min`}
        </span>
      </p>
      {health.issues.length > 0 ? (
        <Notice kind={health.level === "failed" ? "error" : "warning"} title="Issues">
          <ul>
            {health.issues.map((i) => (
              <li key={i}>{i}</li>
            ))}
          </ul>
        </Notice>
      ) : (
        <p className="muted">No issues reported.</p>
      )}

      <h2>Cameras</h2>
      {cameras.length === 0 ? (
        <p className="muted">No cameras are configured.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Camera</th>
              <th scope="col">State</th>
              <th scope="col">Rate</th>
              <th scope="col">Received</th>
              <th scope="col">Skipped</th>
              <th scope="col">Dropped</th>
            </tr>
          </thead>
          <tbody>
            {cameras.map((c) => (
              <tr key={c.camera_id}>
                <th scope="row">{c.camera_id}</th>
                <td>
                  <StateLabel state={c.state} />
                  {c.detail !== null && <span className="muted"> {c.detail}</span>}
                </td>
                <td className="num">{readout(c.measured_fps, " fps", 1)}</td>
                <td className="num">{c.frames_received}</td>
                <td className="num">{c.frames_skipped}</td>
                <td className="num">{c.frames_dropped}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Machine</h2>
      <dl className="facts">
        <dt>CPU</dt>
        <dd className="num">{readout(system?.cpu_percent, "%")}</dd>
        <dt>Memory in use</dt>
        <dd className="num">{readout(system?.memory_used_mb, " MB")}</dd>
        <dt>Engine memory</dt>
        <dd className="num">{readout(system?.process_rss_mb, " MB")}</dd>
        <dt>GPU</dt>
        <dd className="num">
          {system?.gpu_utilization_percent === null || system === null
            ? "unavailable (no NVML)"
            : readout(system.gpu_utilization_percent, "%")}
        </dd>
        <dt>GPU memory</dt>
        <dd className="num">{readout(system?.gpu_memory_used_mb, " MB")}</dd>
        <dt>GPU temperature</dt>
        <dd className="num">{readout(system?.gpu_temperature_c, " °C")}</dd>
      </dl>
    </>
  );
}

function StoragePanel() {
  const [loaded, reload] = useFetch<Storage>("/api/v1/system/storage");
  const data = loaded.state === "ready" ? loaded.data : loaded.previous;
  return (
    <>
      <h2>Storage</h2>
      {loaded.state === "failed" && (
        <Notice kind="warning" title="Could not read storage">
          <p>{loaded.message}</p>
        </Notice>
      )}
      {data === null ? (
        <p role="status" className="muted">
          Loading storage…
        </p>
      ) : (
        <dl className="facts">
          <dt>Disk free</dt>
          <dd className="num">
            {bytes(data.disk_free_bytes)} of {bytes(data.disk_total_bytes)}
          </dd>
          <dt>Database</dt>
          <dd className="num">{bytes(data.database_bytes)}</dd>
          <dt>Evidence</dt>
          <dd className="num">
            {bytes(data.evidence_bytes)} in {data.evidence_files} files
            {data.evidence_truncated ? " (at least: counting stopped early)" : ""}
          </dd>
          <dt>Logs</dt>
          <dd className="num">{bytes(data.logs_bytes)}</dd>
        </dl>
      )}
      <button type="button" onClick={reload}>
        Refresh
      </button>
    </>
  );
}

function LogsPanel() {
  const [level, setLevel] = useState("info");
  const [loaded, reload] = useFetch<Logs>(`/api/v1/system/logs?lines=200&level=${level}`);
  const data = loaded.state === "ready" ? loaded.data : loaded.previous;
  return (
    <>
      <h2>Logs</h2>
      <div className="row">
        <label>
          Level at least
          <select
            value={level}
            onChange={(e) => {
              setLevel(e.target.value);
            }}
          >
            {["debug", "info", "warning", "error"].map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <button type="button" onClick={reload}>
          Refresh
        </button>
      </div>
      {loaded.state === "failed" && (
        <Notice kind="warning" title="Could not read the logs">
          <p>{loaded.message}</p>
        </Notice>
      )}
      {data !== null && data.file_bytes === null && (
        <p className="muted">There is no log file yet.</p>
      )}
      {data !== null && data.file_bytes !== null && data.lines.length === 0 && (
        <p className="muted">No log records at this level yet.</p>
      )}
      {data !== null && data.lines.length > 0 && (
        // A scrollable region must be reachable by keyboard (WCAG 2.1.1), so it takes focus.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        <pre className="logs" aria-label="Recent log records" tabIndex={0}>
          {data.lines
            .map(
              (l) =>
                `${text(l["timestamp"])} ${text(l["level"]).toUpperCase()} ${text(l["event"])}`,
            )
            .join("\n")}
        </pre>
      )}
      {data !== null && data.truncated_to_tail && (
        <p className="muted">Showing the most recent part of a large log.</p>
      )}
    </>
  );
}

export function System({ role }: { role: Role }) {
  return (
    <>
      <h1>System</h1>
      <Health />
      <StoragePanel />
      <Capabilities />
      {role === "administrator" ? (
        <LogsPanel />
      ) : (
        <>
          <h2>Logs</h2>
          <p className="muted">Logs are visible to administrators.</p>
        </>
      )}
    </>
  );
}
