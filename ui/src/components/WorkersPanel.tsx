import { useRef, useState } from "react";
import { sendJson } from "../api/client";
import type { RestartResult, WorkerInfo } from "../api/types";
import { messageOf, useFetch } from "../api/useFetch";
import { Notice } from "./bits";

/** What the engine's workers are doing, and a restart for one that has crashed or stalled. A restart
 * is a consequential action, so it asks first and says what the engine answered. */
export function WorkersPanel() {
  const [loaded, reload] = useFetch<WorkerInfo[]>("/api/v1/system/workers");
  const [asking, setAsking] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<{ name: string; ok: boolean; detail: string } | null>(
    null,
  );
  const dialog = useRef<HTMLDialogElement>(null);
  const workers = loaded.state === "ready" ? loaded.data : loaded.previous;

  const restart = async (name: string) => {
    setBusy(true);
    try {
      const result = await sendJson<RestartResult>(
        "POST",
        `/api/v1/system/workers/${encodeURIComponent(name)}/restart`,
      );
      setOutcome({ name, ok: result.ok, detail: result.detail });
    } catch (e) {
      setOutcome({ name, ok: false, detail: messageOf(e) });
    } finally {
      setBusy(false);
      setAsking(null);
      reload();
    }
  };

  return (
    <>
      <h2>Workers</h2>
      {loaded.state === "failed" && (
        <Notice kind="warning" title="Could not read the workers">
          <p>{loaded.message}</p>
        </Notice>
      )}
      {outcome !== null && (
        <p role={outcome.ok ? "status" : "alert"} className={outcome.ok ? "muted" : "field-error"}>
          {outcome.ok ? "Restarted" : "Not restarted"} {outcome.name}: {outcome.detail}
        </p>
      )}
      {workers === null ? (
        <p role="status" className="muted">
          Loading workers…
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Worker</th>
              <th scope="col">State</th>
              <th scope="col">Last heartbeat</th>
              <th scope="col">Action</th>
            </tr>
          </thead>
          <tbody>
            {workers.map((w) => (
              <tr key={w.name}>
                <th scope="row">{w.name}</th>
                <td>
                  {w.crashed ? (
                    <span className="state state-bad">
                      <span aria-hidden="true">○</span> CRASHED
                    </span>
                  ) : w.alive ? (
                    <span className="state state-ok">
                      <span aria-hidden="true">●</span> RUNNING
                    </span>
                  ) : (
                    <span className="state state-bad">
                      <span aria-hidden="true">○</span> STOPPED
                    </span>
                  )}
                  {w.error !== null && <span className="muted"> {w.error}</span>}
                </td>
                <td className="num">
                  {w.heartbeat_age_ms === null
                    ? "unavailable"
                    : `${(w.heartbeat_age_ms / 1000).toFixed(1)} s ago`}
                </td>
                <td>
                  {w.restartable ? (
                    <button
                      type="button"
                      onClick={() => {
                        setAsking(w.name);
                        queueMicrotask(() => dialog.current?.showModal());
                      }}
                    >
                      Restart {w.name}
                    </button>
                  ) : (
                    <span className="hint">Restart the engine to restart this one.</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <button type="button" onClick={reload}>
        Refresh workers
      </button>
      <dialog
        ref={dialog}
        aria-labelledby="restart-title"
        onClose={() => {
          setAsking(null);
        }}
      >
        <h2 id="restart-title">Restart {asking ?? "this worker"}?</h2>
        <p>
          Everything else keeps running. If this worker is blocked and cannot be stopped, it will
          not be restarted and you will be told why.
        </p>
        <div className="row actions">
          <button
            type="button"
            onClick={() => {
              dialog.current?.close();
            }}
          >
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={busy || asking === null}
            onClick={() => {
              if (asking !== null) {
                dialog.current?.close();
                void restart(asking);
              }
            }}
          >
            {busy ? "Restarting…" : "Restart"}
          </button>
        </div>
      </dialog>
    </>
  );
}
