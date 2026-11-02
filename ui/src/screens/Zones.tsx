import { useId, useState } from "react";
import { ApiError, sendJson } from "../api/client";
import type { AdminCamera, Role, ZoneConfig, ZonesOut } from "../api/types";
import { messageOf, useFetch } from "../api/useFetch";
import { Notice } from "../components/bits";
import { href, type Route } from "../router";
import { usePending } from "../state/pending";
import { MAX_POINTS, type Point, problemWith, tidy } from "../zones/geometry";
import { ZoneCanvas } from "../zones/ZoneCanvas";

/** `crossing_line` is a line, not a polygon, and nothing evaluates it, so it is not offered. */
const KINDS: { value: string; label: string }[] = [
  { value: "restricted", label: "Restricted area" },
  { value: "exclusion", label: "Ignore area" },
  { value: "counting", label: "Counting area" },
  { value: "region_of_interest", label: "Region of interest" },
];

function newZone(taken: Set<string>): ZoneConfig {
  let n = taken.size + 1;
  while (taken.has(`zone-${String(n)}`)) n += 1;
  return {
    zone_id: `zone-${String(n)}`,
    kind: "restricted",
    label: null,
    criticality: 0,
    polygon: [
      [0.3, 0.3],
      [0.7, 0.3],
      [0.7, 0.7],
      [0.3, 0.7],
    ],
  };
}

const pointsOf = (zone: ZoneConfig | null): Point[] => zone?.polygon ?? [];

/** The typed alternative to dragging: every point as two numbers. */
function PointTable({ points, onChange }: { points: Point[]; onChange: (next: Point[]) => void }) {
  const set = (i: number, axis: 0 | 1, raw: string) => {
    const n = Number(raw);
    if (raw === "" || !Number.isFinite(n)) return;
    onChange(points.map((p, j) => (j === i ? tidy(axis === 0 ? [n, p[1]] : [p[0], n]) : p)));
  };
  return (
    <table>
      <caption className="sr-only">Zone points as fractions of the frame, 0 to 1</caption>
      <thead>
        <tr>
          <th scope="col">Point</th>
          <th scope="col">Across</th>
          <th scope="col">Down</th>
          <th scope="col">
            <span className="sr-only">Remove</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {points.map((p, i) => (
          // A point has no identity other than its position in the list.
          <tr key={i}>
            <th scope="row">{i + 1}</th>
            {([0, 1] as const).map((axis) => (
              <td key={axis}>
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.01}
                  value={p[axis]}
                  aria-label={`Point ${String(i + 1)} ${axis === 0 ? "across" : "down"}`}
                  onChange={(e) => {
                    set(i, axis, e.target.value);
                  }}
                />
              </td>
            ))}
            <td>
              <button
                type="button"
                disabled={points.length <= 3}
                title={points.length <= 3 ? "A zone needs at least 3 points" : undefined}
                aria-label={`Remove point ${String(i + 1)}`}
                onClick={() => {
                  onChange(points.filter((_, j) => j !== i));
                }}
              >
                Remove
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ZoneFields({
  zone,
  problem,
  onPatch,
  onDelete,
}: {
  zone: ZoneConfig;
  problem: string | null;
  onPatch: (change: Partial<ZoneConfig>) => void;
  onDelete: () => void;
}) {
  const base = useId();
  const points = pointsOf(zone);
  return (
    <fieldset>
      <legend>{zone.label ?? zone.zone_id}</legend>
      <div className="field">
        <label htmlFor={`${base}-label`}>Label</label>
        <input
          id={`${base}-label`}
          value={zone.label ?? ""}
          onChange={(e) => {
            onPatch({ label: e.target.value === "" ? null : e.target.value });
          }}
        />
      </div>
      <div className="field">
        <label htmlFor={`${base}-kind`}>What it is</label>
        <select
          id={`${base}-kind`}
          value={zone.kind}
          onChange={(e) => {
            onPatch({ kind: e.target.value as ZoneConfig["kind"] });
          }}
        >
          {KINDS.map((k) => (
            <option key={k.value} value={k.value}>
              {k.label}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor={`${base}-crit`}>Criticality (0 to 4)</label>
        <input
          id={`${base}-crit`}
          type="number"
          min={0}
          max={4}
          value={zone.criticality}
          onChange={(e) => {
            onPatch({
              criticality: Math.min(4, Math.max(0, Math.round(Number(e.target.value) || 0))),
            });
          }}
        />
      </div>
      {zone.schedule && (
        <p className="hint">
          This zone has a schedule ({zone.schedule.start} to {zone.schedule.end}). It is kept as it
          is; edit it in the zone file.
        </p>
      )}
      <PointTable
        points={points}
        onChange={(next) => {
          onPatch({ polygon: next });
        }}
      />
      <div className="row actions">
        <button
          type="button"
          disabled={points.length >= MAX_POINTS}
          onClick={() => {
            const a = points[points.length - 1] ?? [0.5, 0.5];
            const b = points[0] ?? [0.5, 0.5];
            onPatch({ polygon: [...points, tidy([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2])] });
          }}
        >
          Add a point
        </button>
        <button type="button" className="danger" onClick={onDelete}>
          Delete this zone
        </button>
      </div>
      {problem !== null && (
        <p className="field-error" role="alert">
          <span aria-hidden="true">✕</span> {problem}
        </p>
      )}
    </fieldset>
  );
}

function Editor({
  camera,
  loaded,
  reload,
}: {
  camera: string;
  loaded: ZonesOut;
  reload: () => void;
}) {
  const pending = usePending();
  const [zones, setZones] = useState<ZoneConfig[]>(loaded.zones.zones);
  const [selected, setSelected] = useState<string | null>(loaded.zones.zones[0]?.zone_id ?? null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [failure, setFailure] = useState<{ message: string; issues: readonly string[] } | null>(
    null,
  );
  const current = zones.find((z) => z.zone_id === selected) ?? null;
  const dirty = JSON.stringify(zones) !== JSON.stringify(loaded.zones.zones);
  const problems = new Map(zones.map((z) => [z.zone_id, problemWith(pointsOf(z))]));
  const firstProblem = [...problems.entries()].find(([, p]) => p !== null);
  const editable = loaded.editable;

  const patch = (id: string, change: Partial<ZoneConfig>) => {
    setSaved(false);
    setZones((all) => all.map((z) => (z.zone_id === id ? { ...z, ...change } : z)));
  };
  const points = pointsOf(current);

  const save = async () => {
    setSaving(true);
    setFailure(null);
    try {
      await sendJson("PUT", `/api/v1/config/cameras/${encodeURIComponent(camera)}/zones`, {
        version: loaded.zones.version,
        zones,
      });
      setSaved(true);
      pending.refresh();
      reload();
    } catch (e) {
      setFailure({ message: messageOf(e), issues: e instanceof ApiError ? e.issues : [] });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="zones-layout">
      <div>
        <ZoneCanvas
          pictureUrl={`/api/v1/cameras/${encodeURIComponent(camera)}/snapshot.jpg?overlay=none`}
          zones={zones.map((z) => ({ id: z.zone_id, points: pointsOf(z) }))}
          selected={editable ? selected : null}
          onMove={(i, to) => {
            if (current !== null)
              patch(current.zone_id, { polygon: points.map((p, j) => (j === i ? to : p)) });
          }}
          onRemovePoint={(i) => {
            if (current !== null && points.length > 3)
              patch(current.zone_id, { polygon: points.filter((_, j) => j !== i) });
          }}
          onAddPoint={(at) => {
            if (current !== null && points.length < MAX_POINTS)
              patch(current.zone_id, { polygon: [...points, at] });
          }}
        />
        {editable && (
          <p className="hint">
            Drag a point, or focus it and use the arrow keys (Shift for bigger steps, Delete to
            remove it). Double-click the picture to add a point to the selected zone. The table
            beside it does the same with typed numbers.
          </p>
        )}
      </div>

      <div>
        {!editable && (
          <Notice kind="info" title="View only">
            <p>
              Changing zones needs the administrator role, and zones configured by hand in a file
              are only shown here.
            </p>
          </Notice>
        )}
        <ul className="zone-list" aria-label="Zones">
          {zones.map((z) => (
            <li key={z.zone_id}>
              <button
                type="button"
                aria-pressed={z.zone_id === selected}
                onClick={() => {
                  setSelected(z.zone_id);
                }}
              >
                {z.label ?? z.zone_id}
                {problems.get(z.zone_id) !== null && (
                  <span className="field-error"> (needs fixing)</span>
                )}
              </button>
            </li>
          ))}
        </ul>
        {zones.length === 0 && <p className="muted">This camera has no zones.</p>}
        <button
          type="button"
          disabled={!editable}
          onClick={() => {
            const zone = newZone(new Set(zones.map((z) => z.zone_id)));
            setZones([...zones, zone]);
            setSelected(zone.zone_id);
            setSaved(false);
          }}
        >
          Add a zone
        </button>

        {current !== null && editable && (
          <ZoneFields
            zone={current}
            problem={problems.get(current.zone_id) ?? null}
            onPatch={(change) => {
              patch(current.zone_id, change);
            }}
            onDelete={() => {
              setZones(zones.filter((z) => z.zone_id !== current.zone_id));
              setSelected(null);
              setSaved(false);
            }}
          />
        )}

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
            <p>Nothing was changed. Your edits are still here.</p>
          </Notice>
        )}
        {saved && (
          <p role="status" className="state-ok">
            <span aria-hidden="true">●</span> Saved. Takes effect when monitoring restarts.
          </p>
        )}
        {loaded.pending && !saved && (
          <p role="status" className="muted">
            Saved zones are waiting for monitoring to restart.
          </p>
        )}
        {editable && (
          <div className="row actions">
            <button
              type="button"
              className="primary"
              disabled={!dirty || saving || firstProblem !== undefined}
              onClick={() => {
                void save();
              }}
            >
              {saving ? "Saving…" : "Save zones"}
            </button>
            <button
              type="button"
              disabled={!dirty || saving}
              onClick={() => {
                setZones(loaded.zones.zones);
                setSaved(false);
                setFailure(null);
              }}
            >
              Discard changes
            </button>
          </div>
        )}
        {firstProblem !== undefined && (
          <p className="hint">
            Save is off until “{firstProblem[0]}” is fixed: {firstProblem[1] ?? ""}
          </p>
        )}
      </div>
    </div>
  );
}

export function Zones({ route, role }: { route: Extract<Route, { view: "zones" }>; role: Role }) {
  const [cameras] = useFetch<AdminCamera[]>("/api/v1/config/cameras");
  const { settled } = usePending();
  const [zones, reload] = useFetch<ZonesOut>(
    route.camera === null
      ? null
      : `/api/v1/config/cameras/${encodeURIComponent(route.camera)}/zones?after=${String(settled)}`,
  );
  const list = cameras.state === "ready" ? cameras.data : [];
  return (
    <>
      <h1>Zones</h1>
      <nav aria-label="Cameras" className="chips">
        {list.map((c) => (
          <a
            key={c.config.camera_id}
            href={href({ view: "zones", camera: c.config.camera_id })}
            aria-current={route.camera === c.config.camera_id ? "page" : undefined}
          >
            {c.config.camera_id}
          </a>
        ))}
      </nav>
      {route.camera === null && (
        <p className="muted">
          {list.length === 0 ? "Add a camera first, then draw its zones." : "Choose a camera."}
        </p>
      )}
      {route.camera !== null && zones.state === "failed" && (
        <Notice kind="error" title="Could not load the zones">
          <p>{zones.message}</p>
          <button type="button" onClick={reload}>
            Try again
          </button>
        </Notice>
      )}
      {route.camera !== null && zones.state === "ready" && (
        <Editor
          key={`${route.camera}:${JSON.stringify(zones.data.zones)}`}
          camera={route.camera}
          loaded={role === "administrator" ? zones.data : { ...zones.data, editable: false }}
          reload={reload}
        />
      )}
    </>
  );
}
