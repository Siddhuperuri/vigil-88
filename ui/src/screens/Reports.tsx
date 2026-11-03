import { useState } from "react";
import type { ReactNode } from "react";
import type { DurationStat, ExportFormat, Role, Summary } from "../api/types";
import { messageOf, useFetch } from "../api/useFetch";
import { Notice, when } from "../components/bits";
import { saveBlob } from "../download";

const WINDOWS = [7, 30, 90];

interface Counted {
  key: string;
  count: number;
}

/** A count table with a proportional bar. The number is always written; the bar only helps the eye. */
function Counts({ title, rows }: { title: string; rows: Counted[] }) {
  const top = Math.max(1, ...rows.map((r) => r.count));
  return (
    <section>
      <h3>{title}</h3>
      {rows.length === 0 ? (
        <p className="muted">None in this period.</p>
      ) : (
        <table>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key}>
                <th scope="row">{r.key}</th>
                <td className="num">{r.count}</td>
                <td aria-hidden="true">
                  <span className="bar" style={{ width: `${String((r.count / top) * 100)}%` }} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

const seconds = (ms: number | null): string =>
  ms === null ? "unavailable" : ms < 1000 ? `${ms.toFixed(0)} ms` : `${(ms / 1000).toFixed(1)} s`;

/** An empty population is "unavailable", never zero: nothing was measured, so nothing is small. */
function Duration({ name, stat }: { name: string; stat: DurationStat }) {
  return (
    <tr>
      <th scope="row">{name}</th>
      {stat.available ? (
        <>
          <td className="num">{stat.count}</td>
          <td className="num">{seconds(stat.p50_ms)}</td>
          <td className="num">{seconds(stat.p95_ms)}</td>
          <td className="num">{seconds(stat.max_ms)}</td>
        </>
      ) : (
        <td colSpan={4}>unavailable: no incidents had this measured</td>
      )}
    </tr>
  );
}

function Exports({ role, days }: { role: Role; days: number }) {
  const [formats] = useFetch<ExportFormat[]>("/api/v1/analytics/formats");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const allowed = role !== "viewer";
  const run = async (format: string) => {
    setBusy(format);
    setError(null);
    try {
      const response = await fetch(
        `/api/v1/analytics/export?format=${format}&days=${String(days)}`,
        {
          credentials: "same-origin",
          headers: { "X-Vigil-Request": "1" },
        },
      );
      if (!response.ok) throw new Error(`The engine answered ${String(response.status)}.`);
      saveBlob(await response.blob(), `vigil-report-${String(days)}d.${format}`);
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(null);
    }
  };
  if (formats.state !== "ready") return null;
  return (
    <section aria-labelledby="export-title">
      <h2 id="export-title">Export this period</h2>
      <div className="row actions">
        {formats.data.map((f) => {
          const reason = !allowed
            ? "Exporting needs the operator or administrator role."
            : f.available
              ? undefined
              : (f.reason ?? "This format is not available here.");
          return (
            <span key={f.format}>
              <button
                type="button"
                disabled={reason !== undefined || busy !== null}
                aria-describedby={reason === undefined ? undefined : `why-${f.format}`}
                onClick={() => {
                  void run(f.format);
                }}
              >
                {busy === f.format ? "Preparing…" : f.format.toUpperCase()}
              </button>
              {reason !== undefined && (
                <span id={`why-${f.format}`} className="hint">
                  {" "}
                  {reason}
                </span>
              )}
            </span>
          );
        })}
      </div>
      {error !== null && (
        <Notice kind="error" title="Export failed">
          <p>{error}</p>
        </Notice>
      )}
    </section>
  );
}

function Body({ s }: { s: Summary }): ReactNode {
  const rates = s.dismissal_rate;
  const avail = Object.entries(s.camera_availability);
  return (
    <>
      {!s.covers_full_window && (
        <Notice kind="warning" title="Records do not cover the whole period">
          <p>
            The earliest record is {when(s.earliest_record_utc)}. Counts before then are not zero,
            they are unknown.
          </p>
        </Notice>
      )}
      {s.caveats.map((c) => (
        <p key={c} className="hint">
          {c}
        </p>
      ))}
      <dl className="facts">
        <dt>Incidents</dt>
        <dd className="num">{s.incidents_total}</dd>
        <dt>Open now</dt>
        <dd className="num">{s.open_now}</dd>
      </dl>

      <div className="grid-2">
        <Counts title="By severity" rows={s.by_severity} />
        <Counts title="By type" rows={s.by_event_type} />
        <Counts title="By camera" rows={s.by_camera} />
        <Counts title="By status" rows={s.by_status} />
      </div>
      <Counts title={`Per ${s.bucket}`} rows={s.trend} />

      <h3>How long things took</h3>
      <table>
        <thead>
          <tr>
            <th scope="col">Measure</th>
            <th scope="col">Incidents</th>
            <th scope="col">Typical (median)</th>
            <th scope="col">Slow (95th percentile)</th>
            <th scope="col">Slowest</th>
          </tr>
        </thead>
        <tbody>
          <Duration name="Incident length" stat={s.duration} />
          <Duration name="Time to detect" stat={s.detection_latency} />
          <Duration name="Time to acknowledge" stat={s.acknowledgement} />
          <Duration name="Time to resolve" stat={s.resolution} />
        </tbody>
      </table>

      <h3>Dismissed by operators, per module</h3>
      {rates.length === 0 ? (
        <p className="muted">No module has any incidents in this period.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Module</th>
              <th scope="col">Dismissed</th>
              <th scope="col">Of</th>
              <th scope="col">Share</th>
            </tr>
          </thead>
          <tbody>
            {rates.map((r) => (
              <tr key={`${r.module_id}@${r.module_version}`}>
                <th scope="row">
                  {r.module_id} <span className="muted">v{r.module_version}</span>
                </th>
                <td className="num">{r.dismissed}</td>
                <td className="num">{r.total}</td>
                <td className="num">{(r.ratio * 100).toFixed(0)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="hint">
        A dismissal means an operator judged the incident not worth acting on. It is a signal about
        noise, not a measured false-positive rate: nobody has labelled what was really there.
      </p>

      <Counts
        title="Why operators dismissed"
        rows={s.dismissal_reasons.map((r) => ({
          key: r.key === "not_given" ? "no reason given" : r.key.replace(/_/g, " "),
          count: r.count,
        }))}
      />
      <p className="hint">
        The reason is the operator&apos;s label. Nobody checked what was in the picture.
      </p>

      <h3>Camera availability</h3>
      {avail.length === 0 ? (
        <p className="muted">unavailable: no availability was recorded for this period</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Camera</th>
              <th scope="col">Recorded</th>
            </tr>
          </thead>
          <tbody>
            {avail.map(([camera, totals]) => (
              <tr key={camera}>
                <th scope="row">{camera}</th>
                <td>
                  {totals === null
                    ? "unavailable"
                    : Object.entries(totals)
                        .map(([k, v]) => `${k}: ${v.toFixed(1)}`)
                        .join(", ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <details>
        <summary>What each number means</summary>
        <dl className="facts">
          {Object.entries(s.metric_definitions).map(([name, d]) => (
            <div key={name}>
              <dt>{name}</dt>
              <dd>
                {d["definition"]}{" "}
                <span className="muted">
                  ({d["population"]}; {d["unit"]})
                </span>
              </dd>
            </div>
          ))}
        </dl>
      </details>
    </>
  );
}

export function Reports({ userRole }: { userRole: Role }) {
  const [days, setDays] = useState(7);
  const offset = -new Date().getTimezoneOffset();
  const [summary, reload] = useFetch<Summary>(
    `/api/v1/analytics/summary?days=${String(days)}&bucket=${days > 30 ? "week" : "day"}&utc_offset_minutes=${String(offset)}`,
  );
  const data = summary.state === "ready" ? summary.data : (summary.previous ?? null);
  return (
    <>
      <h1>Reports</h1>
      <fieldset className="row">
        <legend>Period</legend>
        {WINDOWS.map((d) => (
          <label key={d} className="check">
            <input
              type="radio"
              name="period"
              checked={days === d}
              onChange={() => {
                setDays(d);
              }}
            />{" "}
            Last {d} days
          </label>
        ))}
      </fieldset>
      {summary.state === "failed" && (
        <Notice kind="error" title="Could not load the report">
          <p>{summary.message}</p>
          <button type="button" onClick={reload}>
            Try again
          </button>
        </Notice>
      )}
      {summary.state === "loading" && (
        <p role="status" className="muted">
          Loading…
        </p>
      )}
      {data !== null && <Body s={data} />}
      <Exports role={userRole} days={days} />
    </>
  );
}
