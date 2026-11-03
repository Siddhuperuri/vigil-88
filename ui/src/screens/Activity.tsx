import { useState } from "react";
import type { Candidate, Timeline } from "../api/types";
import { useFetch } from "../api/useFetch";
import { Notice, SeverityBadge, StateLabel, when } from "../components/bits";
import { href, type Route } from "../router";

const WINDOWS: { hours: number; label: string }[] = [
  { hours: 1, label: "Last hour" },
  { hours: 24, label: "Last 24 hours" },
  { hours: 168, label: "Last 7 days" },
];

const OUTCOMES = ["", "rejected", "verified", "pending", "expired"];

type Row =
  | { at: string; camera: string; kind: "incident"; id: string; severity: string; text: string }
  | { at: string; camera: string; kind: "camera"; text: string; state: string };

function Windows({ hours, onChange }: { hours: number; onChange: (h: number) => void }) {
  return (
    <fieldset className="row">
      <legend>Period</legend>
      {WINDOWS.map((w) => (
        <label key={w.hours} className="check">
          <input
            type="radio"
            name="activity-window"
            checked={hours === w.hours}
            onChange={() => {
              onChange(w.hours);
            }}
          />{" "}
          {w.label}
        </label>
      ))}
    </fieldset>
  );
}

/** Incidents and camera state changes on one list, because "the camera was reconnecting when that
 * fired" answers many "why did this fire" questions. Newest first. */
function TimelineTab() {
  const [hours, setHours] = useState(24);
  const [timeline, reload] = useFetch<Timeline>(`/api/v1/events/timeline?hours=${String(hours)}`);
  const data = timeline.state === "ready" ? timeline.data : (timeline.previous ?? null);
  const rows: Row[] =
    data === null
      ? []
      : [
          ...data.incidents.map((i): Row => ({
            at: i.first_observed_utc,
            camera: i.camera_id,
            kind: "incident",
            id: i.incident_id,
            severity: i.severity,
            text: `${i.event_type.replace(/_/g, " ")} (${i.display_ref}), ${i.status}`,
          })),
          ...data.health_events.map((e): Row => ({
            at: e.at_utc,
            camera: e.camera_id,
            kind: "camera",
            state: e.state,
            text: e.detail ?? "",
          })),
        ].sort((a, b) => b.at.localeCompare(a.at));
  return (
    <>
      <Windows hours={hours} onChange={setHours} />
      {timeline.state === "failed" && (
        <Notice kind="error" title="Could not load the timeline">
          <p>{timeline.message}</p>
          <button type="button" onClick={reload}>
            Try again
          </button>
        </Notice>
      )}
      {timeline.state === "loading" && (
        <p role="status" className="muted">
          Loading…
        </p>
      )}
      {data !== null && rows.length === 0 && (
        <p className="muted">Nothing was recorded in this period.</p>
      )}
      {rows.length > 0 && (
        <table>
          <caption className="sr-only">Incidents and camera state changes, newest first</caption>
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Camera</th>
              <th scope="col">What</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.kind}-${r.at}-${r.camera}-${r.text}`}>
                <td>{when(r.at)}</td>
                <th scope="row">{r.camera}</th>
                <td>
                  {r.kind === "incident" ? (
                    <>
                      <SeverityBadge severity={r.severity} />{" "}
                      <a href={href({ view: "incidents", id: r.id })}>{r.text}</a>
                    </>
                  ) : (
                    <>
                      <StateLabel state={r.state} /> <span className="muted">{r.text}</span>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data !== null && (
        <p className="hint">
          Shows up to 100 of each kind, newest first. Incidents are placed at the moment they were
          first seen.
        </p>
      )}
    </>
  );
}

interface Reason {
  reason: string;
  count: number;
}

/** What the detector proposed and which checks refused it. Without this view you only ever see what
 * got through, and you are tuning blind. A candidate is not an incident. */
function CandidatesTab() {
  const [hours, setHours] = useState(24);
  const [outcome, setOutcome] = useState("rejected");
  const query = `hours=${String(hours)}${outcome === "" ? "" : `&outcome=${outcome}`}`;
  const [rows, reload] = useFetch<Candidate[]>(`/api/v1/events/candidates?${query}`);
  const [reasons] = useFetch<Reason[]>(`/api/v1/events/candidates/reasons?hours=${String(hours)}`);
  const list = rows.state === "ready" ? rows.data : (rows.previous ?? null);
  return (
    <>
      <p className="muted">
        A candidate is something a module proposed. Most never become incidents, and the check that
        refused each is recorded here. A refusal is a decision by the software, not a finding that
        nothing was there.
      </p>
      <Windows hours={hours} onChange={setHours} />
      <div className="field">
        <label htmlFor="outcome">Outcome</label>
        <select
          id="outcome"
          value={outcome}
          onChange={(e) => {
            setOutcome(e.target.value);
          }}
        >
          {OUTCOMES.map((o) => (
            <option key={o} value={o}>
              {o === "" ? "All outcomes" : o}
            </option>
          ))}
        </select>
      </div>

      <h2>Which checks refuse the most</h2>
      {reasons.state === "ready" && reasons.data.length === 0 && (
        <p className="muted">No candidate was refused in this period.</p>
      )}
      {reasons.state === "ready" && reasons.data.length > 0 && (
        <table>
          <tbody>
            {reasons.data.map((r) => (
              <tr key={r.reason}>
                <th scope="row">{r.reason}</th>
                <td className="num">{r.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Candidates</h2>
      {rows.state === "failed" && (
        <Notice kind="error" title="Could not load the candidates">
          <p>{rows.message}</p>
          <button type="button" onClick={reload}>
            Try again
          </button>
        </Notice>
      )}
      {rows.state === "loading" && (
        <p role="status" className="muted">
          Loading…
        </p>
      )}
      {list !== null && list.length === 0 && (
        <p className="muted">No candidates match in this period.</p>
      )}
      {list !== null && list.length > 0 && (
        <table>
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Camera</th>
              <th scope="col">Proposed</th>
              <th scope="col">Outcome</th>
              <th scope="col">Why</th>
              <th scope="col">Confidence</th>
            </tr>
          </thead>
          <tbody>
            {list.map((c) => (
              <tr key={c.candidate_id}>
                <td>{when(c.promoted_utc)}</td>
                <th scope="row">{c.camera_id}</th>
                <td>
                  {c.event_type.replace(/_/g, " ")} <span className="muted">({c.module_id})</span>
                </td>
                <td>
                  {c.outcome}
                  {c.incident_id !== null && (
                    <>
                      {" "}
                      <a href={href({ view: "incidents", id: c.incident_id })}>incident</a>
                    </>
                  )}
                </td>
                <td>{c.rejection_reason ?? ""}</td>
                <td className="num">{(c.confidence * 100).toFixed(0)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

export function Activity({ route }: { route: Extract<Route, { view: "activity" }> }) {
  return (
    <>
      <h1>Activity</h1>
      <nav aria-label="Activity views" className="chips">
        <a
          href={href({ view: "activity", tab: "timeline" })}
          aria-current={route.tab === "timeline" ? "page" : undefined}
        >
          Timeline
        </a>
        <a
          href={href({ view: "activity", tab: "candidates" })}
          aria-current={route.tab === "candidates" ? "page" : undefined}
        >
          Candidates and rejections
        </a>
      </nav>
      {route.tab === "timeline" ? <TimelineTab /> : <CandidatesTab />}
    </>
  );
}
