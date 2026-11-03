import { useState } from "react";
import type { SyntheticEvent } from "react";
import { ApiError, sendJson } from "../api/client";
import type { AdminCamera, CameraInput, Devices, ProbeResult } from "../api/types";
import { useFetch, messageOf } from "../api/useFetch";
import { Notice } from "../components/bits";
import { Field } from "../components/Field";
import { KINDS, type Draft, type Kind, draftOf, problemsOf, sourceOf } from "./cameraDraft";
import { SourceFields } from "./SourceFields";

function ProbeNote({ result, busy }: { result: ProbeResult | null; busy: boolean }) {
  if (busy)
    return (
      <p role="status" className="muted">
        Trying the camera… this can take up to ten seconds.
      </p>
    );
  if (result === null) return <p role="status" className="sr-only" />;
  return (
    <p role="status" className={result.ok ? "state-ok" : "field-error"}>
      <span aria-hidden="true">{result.ok ? "●" : "✕"}</span> {result.message}
    </p>
  );
}

/**
 * Adds or edits one camera. Saving writes the camera file and says plainly that it takes effect when
 * monitoring restarts (ADR 0017); it never claims the camera is running.
 */
export function CameraForm({
  existing,
  onDone,
  onCancel,
}: {
  existing: AdminCamera | null;
  onDone: () => void;
  onCancel: () => void;
}) {
  const creating = existing === null;
  const [d, setD] = useState<Draft>(() => draftOf(existing?.config ?? null));
  const [shown, setShown] = useState(false); // problems appear after the first attempt, not while typing
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<{ message: string; issues: readonly string[] } | null>(
    null,
  );
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [probing, setProbing] = useState(false);
  const [devices] = useFetch<Devices>(d.kind === "webcam" ? "/api/v1/config/devices" : null);
  const set = (patch: Partial<Draft>) => {
    setD((prev) => ({ ...prev, ...patch }));
    setProbe(null);
  };
  const problems = problemsOf(d, creating);
  const visible = shown ? problems : {};

  const test = async () => {
    setProbing(true);
    setProbe(null);
    try {
      setProbe(await sendJson<ProbeResult>("POST", "/api/v1/config/cameras/test", sourceOf(d)));
    } catch (e) {
      setProbe({
        ok: false,
        message: messageOf(e),
        width_px: null,
        height_px: null,
        fps: null,
        backend: null,
        detail: null,
        timed_out: false,
      });
    } finally {
      setProbing(false);
    }
  };

  const save = async (event: SyntheticEvent) => {
    event.preventDefault();
    setShown(true);
    setFailure(null);
    if (Object.keys(problems).length > 0) return;
    setSaving(true);
    try {
      if (d.kind === "rtsp" && d.credentialRef.trim() !== "" && d.password !== "") {
        await sendJson(
          "PUT",
          `/api/v1/config/credentials/${encodeURIComponent(d.credentialRef.trim())}`,
          {
            username: d.username,
            password: d.password,
          },
        );
      }
      const config: CameraInput = {
        ...(existing?.config ?? {}),
        camera_id: creating ? d.id : existing.config.camera_id,
        name: d.name.trim() === "" ? null : d.name.trim(),
        source: sourceOf(d),
        enabled: d.enabled,
        priority: Number(d.priority),
        target_inference_fps: Number(d.fps),
      };
      if (creating) await sendJson("POST", "/api/v1/config/cameras", config);
      else
        await sendJson(
          "PUT",
          `/api/v1/config/cameras/${encodeURIComponent(config.camera_id)}`,
          config,
        );
      onDone();
    } catch (e) {
      setFailure({ message: messageOf(e), issues: e instanceof ApiError ? e.issues : [] });
    } finally {
      setSaving(false);
      // The password is never kept after it has been sent, successful or not.
      setD((prev) => ({ ...prev, password: "" }));
    }
  };

  return (
    <form
      onSubmit={(e) => {
        void save(e);
      }}
      noValidate
      aria-labelledby="camera-form-title"
    >
      <h2 id="camera-form-title">
        {creating ? "Add a camera" : `Edit ${existing.config.camera_id}`}
      </h2>
      {existing?.pending === "added" && (
        <Notice kind="info" title="Not running yet">
          <p>This camera was saved and starts when monitoring restarts.</p>
        </Notice>
      )}
      <Field
        label="Camera name"
        error={visible["id"]}
        hint={
          creating ? "Used in the URL and in every record. Cannot be changed later." : undefined
        }
      >
        {(p) =>
          creating ? (
            <input
              {...p}
              autoComplete="off"
              value={d.id}
              onChange={(e) => {
                set({ id: e.target.value.toLowerCase() });
              }}
            />
          ) : (
            <input {...p} value={existing.config.camera_id} readOnly />
          )
        }
      </Field>
      <Field label="Display label (optional)">
        {(p) => (
          <input
            {...p}
            value={d.name}
            onChange={(e) => {
              set({ name: e.target.value });
            }}
          />
        )}
      </Field>
      <Field label="Kind of source">
        {(p) => (
          <select
            {...p}
            value={d.kind}
            onChange={(e) => {
              set({ kind: e.target.value as Kind });
            }}
          >
            {KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </select>
        )}
      </Field>
      <SourceFields
        d={d}
        set={set}
        problems={visible}
        devices={devices.state === "ready" ? devices.data : null}
        credentialKnown={existing?.credential_stored ?? null}
      />
      <div className="row">
        <Field
          label="Priority (0 to 9)"
          error={visible["priority"]}
          hint="Higher gets more of the GPU when it is short."
        >
          {(p) => (
            <input
              {...p}
              type="number"
              min={0}
              max={9}
              value={d.priority}
              onChange={(e) => {
                set({ priority: e.target.value });
              }}
            />
          )}
        </Field>
        <Field label="Analysis rate (frames per second)" error={visible["fps"]}>
          {(p) => (
            <input
              {...p}
              type="number"
              min={0.1}
              step={0.5}
              value={d.fps}
              onChange={(e) => {
                set({ fps: e.target.value });
              }}
            />
          )}
        </Field>
      </div>
      <label className="check">
        <input
          type="checkbox"
          checked={d.enabled}
          onChange={(e) => {
            set({ enabled: e.target.checked });
          }}
        />{" "}
        Monitor this camera
      </label>

      <div className="row actions">
        <button
          type="button"
          disabled={probing}
          onClick={() => {
            void test();
          }}
        >
          {probing ? "Trying…" : "Test connection"}
        </button>
        <ProbeNote result={probe} busy={probing} />
      </div>

      {failure && (
        <Notice kind="error" title="Not saved">
          <p>{failure.message}</p>
          {failure.issues.length > 0 && (
            <ul>
              {failure.issues.map((i) => (
                <li key={i}>{i}</li>
              ))}
            </ul>
          )}
          <p>Nothing was changed. Your entries are still here.</p>
        </Notice>
      )}
      <div className="row actions">
        <button type="submit" className="primary" disabled={saving}>
          {saving ? "Saving…" : creating ? "Save camera" : "Save changes"}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
      <p className="hint">
        Saving does not interrupt monitoring. The change takes effect when monitoring restarts.
      </p>
    </form>
  );
}
