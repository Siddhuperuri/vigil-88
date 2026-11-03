import type { ModuleAvailability } from "../api/types";
import { useFetch } from "../api/useFetch";
import { Notice } from "../components/bits";
import { useTelemetry } from "../state/telemetry";

const WORDS: Record<string, string> = {
  available: "Will run",
  degraded: "Runs with limits",
  unavailable: "Cannot run",
  disabled: "Switched off",
};

/** Features the plan names that this build does not have. Listed so their absence is a statement,
 * not a surprise. */
const NOT_BUILT: { name: string; note: string }[] = [
  {
    name: "Line-crossing zones and module",
    note: "A line is not a polygon and nothing evaluates one, so the zone editor does not offer it.",
  },
  {
    name: "A screen for per-camera detection thresholds",
    note: "They can be set per camera in the camera file (module_params). The console keeps them when it saves a camera but cannot edit them.",
  },
  {
    name: "Overlays drawn in the console",
    note: "The engine draws boxes and labels into the picture. Nothing showed that to be limiting, so the console does not draw its own.",
  },
  {
    name: "Windows Hello unlock and evidence encryption",
    note: "Not designed. Evidence on disk is hashed to detect change, not encrypted.",
  },
];

function Modules({ cameraId }: { cameraId: string }) {
  const [modules] = useFetch<ModuleAvailability[]>(
    `/api/v1/cameras/${encodeURIComponent(cameraId)}/modules`,
  );
  if (modules.state === "loading")
    return (
      <p role="status" className="muted">
        Loading…
      </p>
    );
  if (modules.state === "failed")
    return (
      <p className="muted">
        Unavailable: {modules.message}
        {modules.status === 404 ? " The camera is saved but not running yet." : ""}
      </p>
    );
  return (
    <table>
      <caption className="sr-only">What {cameraId} can detect</caption>
      <thead>
        <tr>
          <th scope="col">Module</th>
          <th scope="col">State</th>
          <th scope="col">Why, and what to change</th>
        </tr>
      </thead>
      <tbody>
        {modules.data.map((m) => (
          <tr key={`${m.module_id}@${m.version}`}>
            <th scope="row">
              {m.event_type.replace(/_/g, " ")} <span className="muted">v{m.version}</span>
            </th>
            <td>
              <span aria-hidden="true">{m.will_run ? "●" : "○"}</span>{" "}
              {m.enabled ? (WORDS[m.status] ?? m.status) : WORDS["disabled"]}
            </td>
            <td>
              {m.reason ?? m.description}
              {m.caveats.length > 0 && (
                <ul className="plain">
                  {m.caveats.map((c) => (
                    <li key={c} className="muted">
                      {c}
                    </li>
                  ))}
                </ul>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** What each running camera can detect, as the engine grants it, and what this build lacks. */
export function Capabilities() {
  const { snapshot } = useTelemetry();
  const cameras = snapshot?.cameras ?? [];
  return (
    <section aria-labelledby="capabilities-title">
      <h2 id="capabilities-title">What each camera can detect</h2>
      <Notice kind="info" title="Detection is unmeasured">
        <p>
          No module has been checked against labelled footage, so there is no false-positive or
          false-negative rate for any of them. Fire and smoke are declarations of intent, not
          perception. This list says what will run, not how well.
        </p>
      </Notice>
      {cameras.length === 0 && <p className="muted">No camera is running.</p>}
      {cameras.map((c) => (
        <div key={c.camera_id}>
          <h3>{c.camera_id}</h3>
          <Modules cameraId={c.camera_id} />
        </div>
      ))}
      <h3>Not built in this version</h3>
      <ul className="plain">
        {NOT_BUILT.map((f) => (
          <li key={f.name}>
            <strong>{f.name}.</strong> <span className="muted">{f.note}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
