import { useState } from "react";
import { ApiError, sendJson } from "../api/client";
import type { AdminCamera, Role, TuningModule, TuningParam } from "../api/types";
import { messageOf, useFetch } from "../api/useFetch";
import { Field } from "../components/Field";
import { Notice } from "../components/bits";
import { href, type Route } from "../router";

const words = (name: string) => name.replaceAll("_", " ");

function bound(p: TuningParam): string {
  if (p.minimum === null && p.maximum === null) return "";
  const lo =
    p.minimum === null ? "" : `${p.exclusive_minimum ? "above" : "at least"} ${String(p.minimum)}`;
  const hi =
    p.maximum === null ? "" : `${p.exclusive_maximum ? "below" : "at most"} ${String(p.maximum)}`;
  return [lo, hi].filter(Boolean).join(", ");
}

function ParamField({
  param,
  value,
  editable,
  onChange,
}: {
  param: TuningParam;
  value: string;
  editable: boolean;
  onChange: (next: string) => void;
}) {
  const range = bound(param);
  const shared = `Shared value ${JSON.stringify(param.file_value ?? param.default)}`;
  return (
    <Field label={words(param.name)} hint={[range, shared].filter(Boolean).join(" · ")}>
      {(p) =>
        param.kind === "boolean" ? (
          <select
            {...p}
            disabled={!editable}
            value={value}
            onChange={(e) => {
              onChange(e.target.value);
            }}
          >
            <option value="">use the shared value</option>
            <option value="true">yes</option>
            <option value="false">no</option>
          </select>
        ) : (
          <input
            {...p}
            type="text"
            inputMode={param.kind === "integer" || param.kind === "number" ? "decimal" : "text"}
            disabled={!editable}
            value={value}
            placeholder={JSON.stringify(param.file_value ?? param.default)}
            onChange={(e) => {
              onChange(e.target.value);
            }}
          />
        )
      }
    </Field>
  );
}

function parse(param: TuningParam, text: string): unknown {
  if (param.kind === "boolean") return text === "true";
  if (param.kind === "integer" || param.kind === "number") return Number(text);
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function ModuleCard({
  camera,
  module,
  editable,
  onSaved,
}: {
  camera: string;
  module: TuningModule;
  editable: boolean;
  onSaved: (next: TuningModule[]) => void;
}) {
  const start = () =>
    Object.fromEntries(
      module.params.map((p) => [p.name, p.has_override ? stringify(p.override) : ""]),
    );
  const [draft, setDraft] = useState<Record<string, string>>(start);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; issues: readonly string[] } | null>(null);
  const changed = JSON.stringify(draft) !== JSON.stringify(start());

  const save = async (overrides: Record<string, unknown>) => {
    setBusy(true);
    setError(null);
    try {
      onSaved(
        await sendJson<TuningModule[]>(
          "PUT",
          `/api/v1/config/cameras/${encodeURIComponent(camera)}/tuning/${encodeURIComponent(module.module_id)}`,
          { overrides },
        ),
      );
    } catch (e) {
      setError({ message: messageOf(e), issues: e instanceof ApiError ? e.issues : [] });
    } finally {
      setBusy(false);
    }
  };

  const submit = () => {
    const overrides: Record<string, unknown> = {};
    for (const p of module.params) {
      const text = draft[p.name] ?? "";
      if (text.trim() !== "") overrides[p.name] = parse(p, text.trim());
    }
    void save(overrides);
  };

  return (
    <form
      className="card"
      aria-labelledby={`m-${module.module_id}`}
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <h2 id={`m-${module.module_id}`}>{words(module.module_id)}</h2>
      <p className="muted">{module.description}</p>
      <p>
        {module.will_run ? (
          <span className="state state-ok">
            <span aria-hidden="true">●</span> RUNS ON THIS CAMERA
          </span>
        ) : (
          <span className="state state-bad">
            <span aria-hidden="true">○</span> {module.enabled ? "CANNOT RUN HERE" : "NOT ENABLED"}
          </span>
        )}
      </p>
      {module.pending && (
        <p className="hint">Saved, but monitoring still uses the old values until it restarts.</p>
      )}
      <p className="hint">
        Leave a box empty to use the shared value. Values outside the limits are refused.
      </p>
      {module.params.map((p) => (
        <ParamField
          key={p.name}
          param={p}
          value={draft[p.name] ?? ""}
          editable={editable}
          onChange={(next) => {
            setDraft({ ...draft, [p.name]: next });
          }}
        />
      ))}
      {error !== null && (
        <Notice kind="error" title="Not saved">
          <p>{error.message}</p>
          {error.issues.length > 0 && (
            <ul>
              {error.issues.map((i) => (
                <li key={i}>{i}</li>
              ))}
            </ul>
          )}
        </Notice>
      )}
      <div className="row actions">
        <button type="submit" className="primary" disabled={!editable || busy || !changed}>
          {busy ? "Saving…" : `Save ${words(module.module_id)} thresholds`}
        </button>
        <button
          type="button"
          disabled={!editable || busy || !module.params.some((p) => p.has_override)}
          onClick={() => {
            setDraft(Object.fromEntries(module.params.map((p) => [p.name, ""])));
            void save({});
          }}
        >
          Use the shared values
        </button>
      </div>
    </form>
  );
}

function stringify(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** One camera's detection thresholds. The limits shown are the ones the engine enforces. */
export function Tuning({ route, role }: { route: Extract<Route, { view: "tuning" }>; role: Role }) {
  const [cameras] = useFetch<AdminCamera[]>("/api/v1/config/cameras");
  const [modules, , setModules] = useModules(route.camera);
  const list = cameras.state === "ready" ? cameras.data : [];
  const editable =
    role === "administrator" &&
    list.find((c) => c.config.camera_id === route.camera)?.editable === true;
  return (
    <>
      <h1>Thresholds</h1>
      <nav aria-label="Cameras">
        <ul className="row wrap">
          {list.map((c) => (
            <li key={c.config.camera_id}>
              <a
                href={href({ view: "tuning", camera: c.config.camera_id })}
                aria-current={c.config.camera_id === route.camera ? "page" : undefined}
              >
                {c.config.name ?? c.config.camera_id}
              </a>
            </li>
          ))}
        </ul>
      </nav>
      {route.camera === null && (
        <p className="muted">
          {list.length === 0 ? "Add a camera first." : "Choose a camera to tune its detection."}
        </p>
      )}
      {route.camera !== null && modules.state === "failed" && (
        <Notice kind="error" title="Could not load the thresholds">
          <p>{modules.message}</p>
        </Notice>
      )}
      {route.camera !== null && modules.state === "loading" && (
        <p role="status" className="muted">
          Loading thresholds…
        </p>
      )}
      {route.camera !== null && modules.state === "ready" && (
        <>
          {!editable && (
            <p className="muted">
              {role === "administrator"
                ? "This camera is defined outside the console, so its thresholds are read-only here."
                : "Changing thresholds needs the administrator role."}
            </p>
          )}
          {modules.data.map((m) => (
            <ModuleCard
              key={`${route.camera}:${m.module_id}:${JSON.stringify(m.params.map((p) => p.override))}`}
              camera={route.camera ?? ""}
              module={m}
              editable={editable}
              onSaved={setModules}
            />
          ))}
        </>
      )}
    </>
  );
}

/** The thresholds of one camera, with a setter so a save replaces the list without a second fetch. */
function useModules(camera: string | null) {
  const [loaded, reload] = useFetch<TuningModule[]>(
    camera === null ? null : `/api/v1/config/cameras/${encodeURIComponent(camera)}/tuning`,
  );
  const [override, setOverride] = useState<{ camera: string | null; data: TuningModule[] } | null>(
    null,
  );
  const merged =
    override !== null && override.camera === camera && loaded.state === "ready"
      ? ({ state: "ready", data: override.data } as const)
      : loaded;
  return [
    merged,
    reload,
    (data: TuningModule[]) => {
      setOverride({ camera, data });
    },
  ] as const;
}
