import { useState } from "react";
import { saveBlob } from "../download";
import { messageOf } from "../api/useFetch";

/** Downloads the diagnostics bundle. It says plainly what is in it and what is not, because a person
 * is about to send it to someone else. */
export function DiagnosticsButton() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const run = async () => {
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      const response = await fetch("/api/v1/system/diagnostics", {
        credentials: "same-origin",
        headers: { "X-Vigil-Request": "1" },
      });
      if (!response.ok) throw new Error(`The engine answered ${String(response.status)}.`);
      saveBlob(await response.blob(), "vigil88-diagnostics.zip");
      setDone(true);
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <h2>Diagnostics</h2>
      <p>
        A zip with versions, health, settings with secrets removed, and recent log records. It has
        no video, no snapshots, no evidence, no accounts or passwords, and no file paths. Read it
        before you send it to anyone.
      </p>
      <div className="row actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            void run();
          }}
        >
          {busy ? "Preparing…" : "Download diagnostics (zip)"}
        </button>
        {done && (
          <span role="status" className="hint">
            Saved. Nothing was sent anywhere.
          </span>
        )}
        {error !== null && (
          <span role="alert" className="field-error">
            Could not make the bundle: {error}
          </span>
        )}
      </div>
    </>
  );
}
