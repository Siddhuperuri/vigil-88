import { useState } from "react";
import { saveBlob } from "../download";
import type { Role } from "../api/types";
import { messageOf } from "../api/useFetch";

/** Downloads one incident and its evidence as a zip whose manifest says which files still match
 * the hashes recorded at capture. Exporting needs the operator role. */
export function BundleButton({
  incidentId,
  name,
  role,
}: {
  incidentId: string;
  name: string;
  role: Role;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const blocked = role === "viewer";
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/v1/incidents/${encodeURIComponent(incidentId)}/bundle`, {
        credentials: "same-origin",
        headers: { "X-Vigil-Request": "1" },
      });
      if (!response.ok) throw new Error(`The engine answered ${String(response.status)}.`);
      saveBlob(await response.blob(), `${name}.zip`);
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="row actions">
      <button
        type="button"
        disabled={blocked || busy}
        aria-describedby={blocked ? "bundle-why" : undefined}
        onClick={() => {
          void run();
        }}
      >
        {busy ? "Preparing the bundle…" : "Download bundle (zip)"}
      </button>
      {blocked && (
        <span id="bundle-why" className="hint">
          Exporting needs the operator role.
        </span>
      )}
      {error !== null && (
        <span role="alert" className="field-error">
          Could not make the bundle: {error}
        </span>
      )}
    </div>
  );
}
