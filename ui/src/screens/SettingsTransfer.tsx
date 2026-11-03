import { useRef, useState } from "react";
import { ApiError } from "../api/client";
import type { ImportOutcome } from "../api/types";
import { messageOf } from "../api/useFetch";
import { saveBlob } from "../download";
import { Notice } from "../components/bits";
import { usePending } from "../state/pending";

const HEADERS = { "X-Vigil-Request": "1", "Content-Type": "application/json" } as const;

const KIND_WORDS: Record<string, string> = {
  alerts: "Alert settings",
  camera: "Camera",
  zones: "Zones and lines",
  module_params: "Shared thresholds",
};
const CHANGE_WORDS: Record<string, string> = {
  add: "new",
  replace: "replaces what is here",
  same: "already the same",
};

interface Refusal {
  message: string;
  issues: readonly string[];
}

/** Move settings between computers. A file is checked in full first and nothing is written until the
 * person has seen what it would do. Passwords and the webhook address are never in a file. */
export function SettingsTransfer() {
  const pending = usePending();
  const picker = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [plan, setPlan] = useState<ImportOutcome | null>(null);
  const [applied, setApplied] = useState<ImportOutcome | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [busy, setBusy] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const exportNow = async () => {
    setExportError(null);
    try {
      const response = await fetch("/api/v1/settings/export", {
        credentials: "same-origin",
        headers: { "X-Vigil-Request": "1" },
      });
      if (!response.ok) throw new Error(`The engine answered ${String(response.status)}.`);
      saveBlob(await response.blob(), "vigil88-settings.json");
    } catch (e) {
      setExportError(messageOf(e));
    }
  };

  const send = async (dryRun: boolean): Promise<ImportOutcome | null> => {
    if (file === null) return null;
    setBusy(true);
    setRefusal(null);
    try {
      const response = await fetch(`/api/v1/settings/import?dry_run=${String(dryRun)}`, {
        method: "POST",
        credentials: "same-origin",
        headers: HEADERS,
        body: await file.text(),
      });
      if (!response.ok) {
        let message = `The engine answered ${String(response.status)}.`;
        let issues: string[] = [];
        try {
          const body = (await response.json()) as { detail?: unknown };
          const detail = body.detail;
          if (typeof detail === "string") message = detail;
          else if (typeof detail === "object" && detail !== null) {
            const d = detail as { message?: unknown; issues?: unknown };
            if (typeof d.message === "string") message = d.message;
            if (Array.isArray(d.issues)) issues = d.issues.filter((i) => typeof i === "string");
          }
        } catch {
          // keep the status line
        }
        setRefusal({ message, issues });
        return null;
      }
      return (await response.json()) as ImportOutcome;
    } catch (e) {
      setRefusal({ message: e instanceof ApiError ? e.message : messageOf(e), issues: [] });
      return null;
    } finally {
      setBusy(false);
    }
  };

  const check = async () => {
    setApplied(null);
    setPlan(await send(true));
  };
  const apply = async () => {
    const result = await send(false);
    if (result !== null) {
      setApplied(result);
      setPlan(null);
      pending.refresh();
    }
  };

  const effective = plan?.changes.filter((c) => c.change !== "same") ?? [];

  return (
    <>
      <h2>Move settings between computers</h2>
      <p>
        Export saves the alert policy, cameras, zones and shared thresholds as a file. It does not
        include passwords, stored camera credentials, the webhook address or any account.
      </p>
      <div className="row actions">
        <button
          type="button"
          onClick={() => {
            void exportNow();
          }}
        >
          Export settings
        </button>
        {exportError !== null && (
          <span role="alert" className="field-error">
            Could not export: {exportError}
          </span>
        )}
      </div>

      <h3>Import</h3>
      <p>
        Importing adds and replaces. It never deletes a camera. The file is checked first, the files
        it would replace are backed up, and if anything fails the previous settings are put back.
      </p>
      <div className="field">
        <label htmlFor="import-file">Settings file</label>
        <input
          id="import-file"
          ref={picker}
          type="file"
          accept="application/json,.json"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setPlan(null);
            setApplied(null);
            setRefusal(null);
          }}
        />
      </div>
      <div className="row actions">
        <button
          type="button"
          disabled={file === null || busy}
          onClick={() => {
            void check();
          }}
        >
          {busy && plan === null ? "Checking…" : "Check the file"}
        </button>
      </div>

      {refusal !== null && (
        <Notice kind="error" title="This file was not accepted">
          <p>{refusal.message}</p>
          {refusal.issues.length > 0 && (
            <ul>
              {refusal.issues.map((i) => (
                <li key={i}>{i}</li>
              ))}
            </ul>
          )}
          <p>Nothing was changed.</p>
        </Notice>
      )}

      {plan !== null && (
        <section aria-label="What the import would do">
          <h3>What this would do</h3>
          {effective.length === 0 ? (
            <p role="status">Nothing: every setting in the file is already the same here.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th scope="col">Kind</th>
                  <th scope="col">Name</th>
                  <th scope="col">Result</th>
                </tr>
              </thead>
              <tbody>
                {plan.changes.map((c) => (
                  <tr key={`${c.kind}:${c.target}`}>
                    <td>{KIND_WORDS[c.kind] ?? c.kind}</td>
                    <th scope="row">{c.target}</th>
                    <td>{CHANGE_WORDS[c.change] ?? c.change}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {plan.warnings.length > 0 && (
            <Notice kind="warning" title="Check these on this computer">
              <ul>
                {plan.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </Notice>
          )}
          {plan.restart_needed && effective.length > 0 && (
            <p className="hint">
              Cameras, zones and thresholds take effect when monitoring restarts. Alert settings
              apply at once.
            </p>
          )}
          <div className="row actions">
            <button
              type="button"
              className="primary"
              disabled={busy || effective.length === 0}
              onClick={() => {
                void apply();
              }}
            >
              {busy ? "Importing…" : "Import these settings"}
            </button>
          </div>
        </section>
      )}

      {applied !== null && (
        <Notice kind="info" title="Imported">
          <p>
            {applied.changes.filter((c) => c.change !== "same").length} settings were applied.
            {applied.backup !== null && ` The files they replaced were saved in ${applied.backup}.`}
          </p>
          {applied.restart_needed && (
            <p>Restart monitoring from the bar above to start using the camera and zone changes.</p>
          )}
        </Notice>
      )}
    </>
  );
}
