import { useRef, useState } from "react";
import type { Role } from "../api/types";
import { usePending } from "../state/pending";

/**
 * Saved changes that have not taken effect yet. Applying them restarts monitoring, so the cost is
 * stated before the person agrees, and the restart is shown until the engine is back.
 */
export function PendingBar({ role }: { role: Role }) {
  const { pending, phase, error, apply } = usePending();
  const dialog = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  if (phase === "applying") {
    return (
      <div className="pending-bar" role="status">
        Restarting monitoring… every camera is paused until the engine is back. You stay signed in.
      </div>
    );
  }
  if (phase === "failed" && error !== null) {
    return (
      <div className="pending-bar pending-bad" role="alert">
        Could not restart monitoring: {error}
      </div>
    );
  }
  if (pending === null || !pending.pending) return null;
  const n = pending.changes.length;
  return (
    <div className="pending-bar" role="status">
      <span>
        {n === 1 ? "1 saved change is" : `${String(n)} saved changes are`} waiting for monitoring to
        restart: {pending.changes.map((c) => `${c.kind} ${c.target} ${c.change}`).join(", ")}.
      </span>
      {role === "administrator" ? (
        pending.can_restart ? (
          <button
            type="button"
            className="primary"
            onClick={() => {
              setOpen(true);
              dialog.current?.showModal();
            }}
          >
            Apply and restart…
          </button>
        ) : (
          <span className="muted">
            This engine is not running under the desktop app, so stop it and start it again to apply
            these.
          </span>
        )
      ) : (
        <span className="muted">An administrator can apply them.</span>
      )}
      <dialog
        ref={dialog}
        aria-labelledby="apply-title"
        onClose={() => {
          setOpen(false);
        }}
      >
        <h2 id="apply-title">Restart monitoring?</h2>
        <p>
          Every camera stops for a few seconds while the engine restarts. Anything that happens in
          that time is not seen. You will not be signed out.
        </p>
        <div className="row actions">
          <button
            type="button"
            onClick={() => {
              dialog.current?.close();
            }}
          >
            Not now
          </button>
          <button
            type="button"
            className="primary"
            disabled={!open}
            onClick={() => {
              dialog.current?.close();
              void apply();
            }}
          >
            Restart now
          </button>
        </div>
      </dialog>
    </div>
  );
}
