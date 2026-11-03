import { useRef, useState } from "react";
import { sendJson } from "../api/client";
import type { AdminCamera, Role } from "../api/types";
import { messageOf, useFetch } from "../api/useFetch";
import { Notice, StateLabel } from "../components/bits";
import { href, type Route } from "../router";
import { usePending } from "../state/pending";
import { useTelemetry } from "../state/telemetry";
import { CameraForm } from "./CameraForm";

const PENDING_WORDS: Record<string, string> = {
  added: "Saved. Starts when monitoring restarts.",
  changed: "Changed. Takes effect when monitoring restarts.",
  removed: "Removed. Stops when monitoring restarts.",
};

const SOURCE_WORDS: Record<string, string> = {
  webcam: "Webcam",
  rtsp: "Network camera",
  video_file: "Video file",
  synthetic: "Synthetic test source",
  image: "Still image",
};

function RemoveDialog({
  camera,
  onClose,
  onRemoved,
}: {
  camera: AdminCamera;
  onClose: () => void;
  onRemoved: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDialogElement>(null);
  const id = camera.config.camera_id;
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await sendJson("DELETE", `/api/v1/config/cameras/${encodeURIComponent(id)}`);
      onRemoved();
    } catch (e) {
      setError(messageOf(e));
      setBusy(false);
    }
  };
  return (
    <dialog
      ref={(el) => {
        ref.current = el;
        if (el !== null && !el.open) el.showModal();
      }}
      onClose={onClose}
      aria-labelledby="remove-title"
    >
      <h2 id="remove-title">Remove {id}?</h2>
      <p>
        The camera file is deleted. Existing incidents and their evidence are kept. Monitoring of
        this camera stops when monitoring next restarts.
      </p>
      {error !== null && (
        <Notice kind="error" title="Not removed">
          <p>{error}</p>
        </Notice>
      )}
      <div className="row actions">
        <button
          type="button"
          onClick={() => {
            ref.current?.close();
          }}
        >
          Keep it
        </button>
        <button
          type="button"
          className="danger"
          disabled={busy}
          onClick={() => {
            void confirm();
          }}
        >
          {busy ? "Removing…" : "Remove camera"}
        </button>
      </div>
    </dialog>
  );
}

export function Cameras({
  route,
  role,
  go,
}: {
  route: Extract<Route, { view: "cameras" }>;
  role: Role;
  go: (r: Route) => void;
}) {
  const pending = usePending();
  // Re-read whenever the pending state is re-read: after an apply the list must show what runs now.
  const [list, reload] = useFetch<AdminCamera[]>(
    `/api/v1/config/cameras?after=${String(pending.settled)}`,
  );
  const { snapshot } = useTelemetry();
  // The running state comes from the live channel; the fetched one is only the fallback.
  const live = (c: AdminCamera): string | null =>
    snapshot?.cameras.find((x) => x.camera_id === c.config.camera_id)?.state ?? c.state;
  const [removing, setRemoving] = useState<AdminCamera | null>(null);
  const canEdit = role === "administrator";
  const cameras =
    list.state === "ready"
      ? list.data
      : list.state === "loading"
        ? (list.previous ?? [])
        : (list.previous ?? []);

  const done = () => {
    reload();
    pending.refresh();
    go({ view: "cameras", id: null });
  };

  if (route.id !== null) {
    const existing =
      route.id === "new"
        ? null
        : (cameras.find((c) => c.config.camera_id === route.id) ?? undefined);
    if (existing === undefined) {
      return list.state === "loading" ? (
        <p role="status" className="muted">
          Loading…
        </p>
      ) : (
        <Notice kind="warning" title="No such camera">
          <p>
            <a href={href({ view: "cameras", id: null })}>Back to cameras</a>
          </p>
        </Notice>
      );
    }
    if (!canEdit)
      return (
        <Notice kind="info" title="Administrators only">
          <p>Changing cameras needs the administrator role.</p>
        </Notice>
      );
    if (existing !== null && !existing.editable)
      return (
        <Notice kind="info" title="Defined outside the console">
          <p>This camera is configured by hand in a file, so the console does not edit it.</p>
        </Notice>
      );
    return (
      <CameraForm
        existing={existing}
        onDone={done}
        onCancel={() => {
          go({ view: "cameras", id: null });
        }}
      />
    );
  }

  return (
    <>
      <div className="row between">
        <h1>Cameras</h1>
        {canEdit ? (
          <a className="button primary" href={href({ view: "cameras", id: "new" })}>
            Add a camera
          </a>
        ) : (
          <span className="muted">Adding cameras needs the administrator role.</span>
        )}
      </div>
      {list.state === "failed" && (
        <Notice kind="error" title="Could not load the cameras">
          <p>{list.message}</p>
          <button type="button" onClick={reload}>
            Try again
          </button>
        </Notice>
      )}
      {list.state === "loading" && cameras.length === 0 && (
        <p role="status" className="muted">
          Loading…
        </p>
      )}
      {list.state !== "loading" && cameras.length === 0 && list.state === "ready" && (
        <Notice kind="info" title="No cameras yet">
          <p>Add a webcam, a network camera or a video file to start monitoring.</p>
        </Notice>
      )}
      {cameras.length > 0 && (
        <table>
          <thead>
            <tr>
              <th scope="col">Camera</th>
              <th scope="col">Source</th>
              <th scope="col">State</th>
              <th scope="col">Saved changes</th>
              <th scope="col">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {cameras.map((c) => {
              const id = c.config.camera_id;
              return (
                <tr key={id}>
                  <th scope="row">
                    {id}
                    {c.config.name !== null && c.config.name !== undefined && (
                      <span className="muted"> {c.config.name}</span>
                    )}
                  </th>
                  <td>{SOURCE_WORDS[c.config.source.kind] ?? c.config.source.kind}</td>
                  <td>
                    {live(c) === null ? (
                      <span className="muted">Not running</span>
                    ) : (
                      <StateLabel state={live(c) ?? ""} />
                    )}
                    {!c.config.enabled && <span className="muted"> (monitoring off)</span>}
                  </td>
                  <td>
                    {c.pending ? PENDING_WORDS[c.pending] : <span className="muted">None</span>}
                  </td>
                  <td>
                    <a href={href({ view: "zones", camera: id })}>Zones</a>{" "}
                    <a
                      href={href({ view: "tuning", camera: id })}
                      aria-label={`Thresholds for ${id}`}
                    >
                      Thresholds
                    </a>{" "}
                    {canEdit && c.editable && c.pending !== "removed" && (
                      <>
                        <a href={href({ view: "cameras", id })} aria-label={`Edit ${id}`}>
                          Edit
                        </a>{" "}
                        <button
                          type="button"
                          onClick={() => {
                            setRemoving(c);
                          }}
                          aria-label={`Remove ${id}`}
                        >
                          Remove
                        </button>
                      </>
                    )}
                    {canEdit && !c.editable && (
                      <span className="muted" title="Configured by hand">
                        Read only
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {removing !== null && (
        <RemoveDialog
          camera={removing}
          onClose={() => {
            setRemoving(null);
          }}
          onRemoved={() => {
            setRemoving(null);
            reload();
            pending.refresh();
          }}
        />
      )}
    </>
  );
}
