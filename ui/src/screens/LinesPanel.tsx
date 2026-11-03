import type { LineConfig } from "../api/types";
import { Field } from "../components/Field";

const DIRECTIONS = [
  { value: "any", label: "either way" },
  { value: "to_right", label: "left to right (walking from start to end)" },
  { value: "to_left", label: "right to left (walking from start to end)" },
] as const;

const next = (taken: string[]): string => {
  let n = taken.length + 1;
  while (taken.includes(`line-${String(n)}`)) n += 1;
  return `line-${String(n)}`;
};

function coordinate(text: string, fallback: number): number {
  const n = Number(text);
  return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : fallback;
}

function Point({
  label,
  value,
  editable,
  onChange,
}: {
  label: string;
  value: [number, number] | readonly number[];
  editable: boolean;
  onChange: (to: [number, number]) => void;
}) {
  const x = value[0];
  const y = value[1];
  return (
    <fieldset>
      <legend>{label} (0 to 1 across the picture)</legend>
      <div className="row">
        <Field label={`${label} across`}>
          {(p) => (
            <input
              {...p}
              inputMode="decimal"
              disabled={!editable}
              defaultValue={String(x)}
              onBlur={(e) => {
                onChange([coordinate(e.target.value, x), y]);
              }}
            />
          )}
        </Field>
        <Field label={`${label} down`}>
          {(p) => (
            <input
              {...p}
              inputMode="decimal"
              disabled={!editable}
              defaultValue={String(y)}
              onBlur={(e) => {
                onChange([x, coordinate(e.target.value, y)]);
              }}
            />
          )}
        </Field>
      </div>
    </fieldset>
  );
}

/** Tripwires: a line across the picture and the direction that counts. They are saved with the zones
 * and read by the line_crossing module, which has to be enabled for them to do anything. */
export function LinesPanel({
  lines,
  onChange,
  editable,
}: {
  lines: LineConfig[];
  onChange: (next: LineConfig[]) => void;
  editable: boolean;
}) {
  return (
    <section aria-labelledby="lines-title">
      <h3 id="lines-title">Crossing lines</h3>
      <p className="hint">
        A line reports a person who walks across it in the direction you choose. It needs the
        line_crossing module to be enabled. It has been tried on test scenes, not on real footage.
      </p>
      {lines.length === 0 && <p className="muted">No crossing lines.</p>}
      {lines.map((line, i) => (
        <fieldset className="card" key={`${line.line_id}:${String(i)}`}>
          <legend>Line: {line.label ?? line.line_id}</legend>
          <Field label="Name">
            {(p) => (
              <input
                {...p}
                disabled={!editable}
                value={line.label ?? ""}
                placeholder={line.line_id}
                maxLength={80}
                onChange={(e) => {
                  onChange(
                    lines.map((l, j) => (j === i ? { ...l, label: e.target.value || null } : l)),
                  );
                }}
              />
            )}
          </Field>
          <Point
            label="Start"
            value={line.start}
            editable={editable}
            onChange={(to) => {
              onChange(lines.map((l, j) => (j === i ? { ...l, start: to } : l)));
            }}
          />
          <Point
            label="End"
            value={line.end}
            editable={editable}
            onChange={(to) => {
              onChange(lines.map((l, j) => (j === i ? { ...l, end: to } : l)));
            }}
          />
          <Field label="Direction that counts">
            {(p) => (
              <select
                {...p}
                disabled={!editable}
                value={line.direction}
                onChange={(e) => {
                  onChange(
                    lines.map((l, j) =>
                      j === i ? { ...l, direction: e.target.value as LineConfig["direction"] } : l,
                    ),
                  );
                }}
              >
                {DIRECTIONS.map((d) => (
                  <option key={d.value} value={d.value}>
                    {d.label}
                  </option>
                ))}
              </select>
            )}
          </Field>
          {editable && (
            <button
              type="button"
              className="danger"
              onClick={() => {
                onChange(lines.filter((_, j) => j !== i));
              }}
            >
              Remove line {line.label ?? line.line_id}
            </button>
          )}
        </fieldset>
      ))}
      {editable && (
        <button
          type="button"
          disabled={lines.length >= 16}
          onClick={() => {
            onChange([
              ...lines,
              {
                line_id: next(lines.map((l) => l.line_id)),
                start: [0.2, 0.5],
                end: [0.8, 0.5],
                direction: "any",
                criticality: 0,
              },
            ]);
          }}
        >
          Add a crossing line
        </button>
      )}
    </section>
  );
}
