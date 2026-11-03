import { EvidenceContinuity } from "../components/EvidenceContinuity";
import { useEffect, useState } from "react";
import type { RefObject } from "react";
import { sendJson } from "../api/client";
import type { Evidence, IncidentDetail as Detail, Role } from "../api/types";
import { useFetch } from "../api/useFetch";
import { BundleButton } from "../components/BundleButton";
import { ActionPrompt } from "../components/ActionPrompt";
import { Notice, SeverityBadge, bytes, when } from "../components/bits";

export type ActionName = "acknowledge" | "dismiss" | "escalate" | "resolve";

const LABEL: Record<ActionName, string> = {
  acknowledge: "Acknowledge",
  dismiss: "Dismiss",
  escalate: "Escalate",
  resolve: "Resolve",
};
const KEY: Record<ActionName, string> = {
  acknowledge: "A",
  dismiss: "D",
  escalate: "E",
  resolve: "R",
};
/** Terminal or consequential: these ask for a reason first, and say what they do. */
const NEEDS_REASON: Record<ActionName, string | null> = {
  acknowledge: null,
  dismiss:
    "Dismissing marks this as a false alarm and closes it. It is the only measure of false positives, so say why.",
  escalate: "Escalating raises the severity band by one. The computed score is left as it was.",
  resolve:
    "Resolving closes the incident as over. If the situation continues, a new incident opens.",
};

interface EvidenceState {
  url: string | null;
  integrity: "ok" | "mismatch" | "unknown" | null;
  failed: string | null;
}

function useEvidence(incidentId: string, item: Evidence): EvidenceState {
  const [state, setState] = useState<EvidenceState>({ url: null, integrity: null, failed: null });
  useEffect(() => {
    let revoked = false;
    let objectUrl: string | null = null;
    fetch(
      `/api/v1/incidents/${encodeURIComponent(incidentId)}/evidence/${encodeURIComponent(item.evidence_id)}`,
      { credentials: "same-origin" },
    )
      .then(async (response) => {
        if (!response.ok) throw new Error(`The engine answered ${String(response.status)}.`);
        const header = response.headers.get("x-evidence-integrity");
        const blob = await response.blob();
        if (revoked) return;
        objectUrl = URL.createObjectURL(blob);
        setState({
          url: objectUrl,
          integrity: header === "ok" ? "ok" : header === "mismatch" ? "mismatch" : "unknown",
          failed: null,
        });
      })
      .catch((error: unknown) => {
        if (!revoked) {
          setState({
            url: null,
            integrity: null,
            failed: error instanceof Error ? error.message : "Failed.",
          });
        }
      });
    return () => {
      revoked = true;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [incidentId, item.evidence_id]);
  return state;
}

function IntegrityBadge({ integrity }: { integrity: EvidenceState["integrity"] }) {
  if (integrity === null) return <span className="muted">checking…</span>;
  if (integrity === "ok") {
    return (
      <span className="state state-ok">
        <span aria-hidden="true">✔</span> Integrity verified
      </span>
    );
  }
  return (
    <span className="state state-bad">
      <span aria-hidden="true">✖</span>{" "}
      {integrity === "mismatch"
        ? "Integrity MISMATCH: do not rely on this file"
        : "Integrity not reported"}
    </span>
  );
}

function EvidenceItem({ incidentId, item }: { incidentId: string; item: Evidence }) {
  const ev = useEvidence(incidentId, item);
  const kind = item.kind.replace(/_/g, " ");
  return (
    <li className="evidence">
      <div className="row">
        <strong>{kind}</strong>
        <span className="num muted">{bytes(item.bytes)}</span>
        <span className="num muted" title={item.sha256}>
          sha256 {item.sha256.slice(0, 12)}…
        </span>
        <IntegrityBadge integrity={ev.integrity} />
      </div>
      {ev.failed !== null && <p role="alert">Could not load this file: {ev.failed}</p>}
      {ev.url !== null && item.media_type.startsWith("image/") && (
        <img src={ev.url} alt={`Evidence: ${kind}`} className="evidence-media" />
      )}
      {ev.url !== null && item.media_type.startsWith("video/") && (
        <video src={ev.url} controls className="evidence-media" aria-label={`Evidence: ${kind}`}>
          <track kind="captions" />
        </video>
      )}
      {ev.url !== null &&
        !item.media_type.startsWith("image/") &&
        !item.media_type.startsWith("video/") && (
          <a href={ev.url} download={`${item.kind}-${item.evidence_id}`}>
            Download {kind}
          </a>
        )}
    </li>
  );
}

function Explanation({ detail }: { detail: Detail }) {
  const e = detail.explanation;
  const total = e.severity_factors.reduce((n, f) => n + f.contribution, 0);
  return (
    <section aria-labelledby="why">
      <h3 id="why">Why this was raised</h3>
      <p>{e.trigger_summary}</p>
      <h4>Severity: the factors add up to the score</h4>
      <table>
        <thead>
          <tr>
            <th scope="col">Factor</th>
            <th scope="col">Value</th>
            <th scope="col">Weight</th>
            <th scope="col">Adds</th>
          </tr>
        </thead>
        <tbody>
          {e.severity_factors.map((f) => (
            <tr key={f.name}>
              <th scope="row">{f.name}</th>
              <td className="num">{f.raw_value.toFixed(2)}</td>
              <td className="num">{f.weight.toFixed(2)}</td>
              <td className="num">{f.contribution.toFixed(1)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row" colSpan={3}>
              Sum, against the incident score of {detail.incident.severity_score.toFixed(1)}
            </th>
            <td className="num">{total.toFixed(1)}</td>
          </tr>
        </tfoot>
      </table>
      <h4>Checks it passed</h4>
      <table>
        <thead>
          <tr>
            <th scope="col">Check</th>
            <th scope="col">Outcome</th>
            <th scope="col">Reason</th>
          </tr>
        </thead>
        <tbody>
          {e.validator_verdicts.map((v) => (
            <tr key={v.validator_id}>
              <th scope="row">{v.validator_id}</th>
              <td>{v.outcome}</td>
              <td>{v.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {e.capability_caveats.length > 0 && (
        <>
          <h4>What this run could not see</h4>
          <ul>
            {e.capability_caveats.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </>
      )}
      <p className="muted">
        {e.observations.length} observations and {e.accumulator_trace.length} accumulator steps
        support this. A confidence is a score, not a probability: it orders incidents and is not a
        chance of being right.
      </p>
    </section>
  );
}

/** Loads one incident, then hands the loaded data to a body that owns the form state. Keyed by id
 * in the parent, so choosing another incident starts clean without any reset logic. */
export function IncidentDetail({
  id,
  role,
  noteRef,
  promptRef,
  onChanged,
  actionRef,
}: {
  id: string;
  role: Role;
  noteRef: RefObject<HTMLTextAreaElement | null>;
  promptRef: RefObject<HTMLTextAreaElement | null>;
  onChanged: () => void;
  /** The desk calls this when a shortcut asks for an action; the body registers itself here. */
  actionRef: RefObject<((action: ActionName) => void) | null>;
}) {
  const [loaded, reload] = useFetch<Detail>(`/api/v1/incidents/${encodeURIComponent(id)}`);
  const detail = loaded.state === "ready" ? loaded.data : loaded.previous;
  if (detail === null) {
    return loaded.state === "failed" ? (
      <Notice kind="error" title="Could not load this incident">
        <p>{loaded.message}</p>
        <button type="button" onClick={reload}>
          Try again
        </button>
      </Notice>
    ) : (
      <p role="status" className="muted">
        Loading the incident…
      </p>
    );
  }
  return (
    <DetailBody
      id={id}
      detail={detail}
      staleMessage={loaded.state === "failed" ? loaded.message : null}
      role={role}
      noteRef={noteRef}
      promptRef={promptRef}
      actionRef={actionRef}
      reload={reload}
      onChanged={onChanged}
    />
  );
}

function DetailBody({
  id,
  detail,
  staleMessage,
  role,
  noteRef,
  promptRef,
  actionRef,
  reload,
  onChanged,
}: {
  id: string;
  detail: Detail;
  staleMessage: string | null;
  role: Role;
  noteRef: RefObject<HTMLTextAreaElement | null>;
  promptRef: RefObject<HTMLTextAreaElement | null>;
  actionRef: RefObject<((action: ActionName) => void) | null>;
  reload: () => void;
  onChanged: () => void;
}) {
  const [pending, setPending] = useState<ActionName | null>(null);
  const [reason, setReason] = useState("");
  const [reasonCode, setReasonCode] = useState<string | null>(null);
  const [note, setNote] = useState(detail.operator_note ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const canAct = role !== "viewer";
  const run = async (action: ActionName, text: string | null) => {
    setBusy(true);
    setError(null);
    try {
      await sendJson("POST", `/api/v1/incidents/${encodeURIComponent(id)}/${action}`, {
        ...(text === null ? {} : { reason: text, note: text }),
        ...(action === "dismiss" && reasonCode !== null ? { reason_code: reasonCode } : {}),
      });
      setPending(null);
      setReason("");
      setReasonCode(null);
      reload();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The action failed.");
    } finally {
      setBusy(false);
    }
  };

  const choose = (action: ActionName) => {
    if (!canAct || detail.permissions[action]?.allowed !== true) return;
    if (NEEDS_REASON[action] === null) void run(action, null);
    else {
      setPending(action);
      // The prompt mounts on the next render; focus it then, so a keyboard user can type at once.
      // A dismissal opens on its list of reasons instead, which focuses itself.
      if (action !== "dismiss")
        requestAnimationFrame(() => {
          promptRef.current?.focus();
        });
    }
  };

  useEffect(() => {
    actionRef.current = choose;
    return () => {
      actionRef.current = null;
    };
  });

  const inc = detail.incident;
  return (
    <article aria-labelledby="incident-title" className="detail">
      <header>
        <h2 id="incident-title">
          {inc.display_ref} <SeverityBadge severity={inc.severity} />
        </h2>
        <p>
          <strong>{inc.event_type.replace(/_/g, " ")}</strong> on {inc.camera_id}
          {inc.location ? `, ${inc.location}` : ""} ·{" "}
          <span className="num">{inc.status.toUpperCase()}</span>
        </p>
        <p className="muted">
          First seen {when(inc.first_observed_utc)} · confirmed {when(inc.confirmed_utc)} · detected
          in <span className="num">{Math.round(inc.detection_latency_ms)} ms</span>
        </p>
      </header>

      {staleMessage !== null && (
        <Notice kind="warning" title="Showing the last data received">
          <p>{staleMessage}</p>
        </Notice>
      )}

      <section aria-labelledby="actions">
        <h3 id="actions">Actions</h3>
        <div className="row">
          {(Object.keys(LABEL) as ActionName[]).map((a) => {
            const standing = detail.permissions[a];
            const why = !canAct
              ? "This needs the operator role."
              : standing?.allowed === false
                ? (standing.reason ?? "Not available now.")
                : null;
            return (
              <span key={a} className="action">
                <button
                  type="button"
                  disabled={why !== null || busy}
                  aria-describedby={why === null ? undefined : `why-${a}`}
                  onClick={() => {
                    choose(a);
                  }}
                >
                  {LABEL[a]} <kbd>{KEY[a]}</kbd>
                </button>
                {why !== null && (
                  <span id={`why-${a}`} className="why">
                    {why}
                  </span>
                )}
              </span>
            );
          })}
        </div>
        {error !== null && (
          <p role="alert" className="form-error">
            {error}
          </p>
        )}
        {pending !== null && (
          <ActionPrompt
            action={pending}
            label={LABEL[pending]}
            explanation={NEEDS_REASON[pending] ?? ""}
            reason={reason}
            onReason={setReason}
            reasonCode={reasonCode}
            onReasonCode={setReasonCode}
            busy={busy}
            promptRef={promptRef}
            onSubmit={() => {
              void run(pending, reason.trim() === "" ? null : reason.trim());
            }}
            onCancel={() => {
              setPending(null);
            }}
          />
        )}
      </section>

      <section aria-labelledby="note-title">
        <h3 id="note-title">Note</h3>
        <label htmlFor="note" className="sr-only">
          Operator note
        </label>
        <textarea
          id="note"
          ref={noteRef}
          value={note}
          rows={3}
          maxLength={2000}
          disabled={!canAct}
          onChange={(e) => {
            setNote(e.target.value);
          }}
        />
        <button
          type="button"
          disabled={!canAct || busy || note === (detail.operator_note ?? "")}
          onClick={() => {
            setBusy(true);
            sendJson("POST", `/api/v1/incidents/${encodeURIComponent(id)}/annotate`, { note })
              .then(() => {
                reload();
                onChanged();
              })
              .catch((e: unknown) => {
                setError(e instanceof Error ? e.message : "Could not save the note.");
              })
              .finally(() => {
                setBusy(false);
              });
          }}
        >
          Save note
        </button>
      </section>

      <Explanation detail={detail} />

      <section aria-labelledby="evidence-title">
        <h3 id="evidence-title">Evidence</h3>
        <EvidenceContinuity notes={detail.evidence_annotations} />
        {detail.evidence.length === 0 ? (
          <p className="muted">No evidence was captured for this incident.</p>
        ) : (
          <ul className="plain">
            {detail.evidence.map((item) => (
              <EvidenceItem key={item.evidence_id} incidentId={id} item={item} />
            ))}
          </ul>
        )}
        <BundleButton incidentId={id} name={inc.display_ref} role={role} />
      </section>

      <section aria-labelledby="history-title">
        <h3 id="history-title">History</h3>
        <ol className="plain">
          {detail.transitions.map((t) => (
            <li key={`${t.at_utc}-${t.to_status}`}>
              <span className="num">{when(t.at_utc)}</span> ·{" "}
              {(t.from_status ?? "new").toUpperCase()} →{" "}
              <strong>{t.to_status.toUpperCase()}</strong> by {t.actor}: {t.reason}
            </li>
          ))}
        </ol>
      </section>
    </article>
  );
}
