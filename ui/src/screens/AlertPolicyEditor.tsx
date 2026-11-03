import { useState } from "react";
import { sendJson } from "../api/client";
import { SEVERITIES, type AlertPolicy, type AlertSettings } from "../api/types";
import { ApiError } from "../api/client";
import { messageOf } from "../api/useFetch";
import { Field } from "../components/Field";
import { Notice } from "../components/bits";

type Rule = AlertPolicy["rules"][number];
type Quiet = AlertPolicy["quiet_hours"][number];

export const EVENT_TYPES = [
  "fire",
  "smoke",
  "vehicle_collision",
  "person_down",
  "crowd_anomaly",
  "intrusion",
  "restricted_area_entry",
  "abandoned_object",
  "dangerous_motion",
  "vehicle_anomaly",
] as const;

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;
const words = (type: string) => type.replaceAll("_", " ");

function unique(base: string, taken: string[]): string {
  let n = taken.length + 1;
  while (taken.includes(`${base} ${String(n)}`)) n += 1;
  return `${base} ${String(n)}`;
}

function RuleRow({
  rule,
  channels,
  onChange,
  onRemove,
}: {
  rule: Rule;
  channels: string[];
  onChange: (next: Rule) => void;
  onRemove: () => void;
}) {
  return (
    <fieldset className="card">
      <legend>Rule: {rule.name}</legend>
      <Field label="Name">
        {(p) => (
          <input
            {...p}
            value={rule.name}
            maxLength={64}
            onChange={(e) => {
              onChange({ ...rule, name: e.target.value });
            }}
          />
        )}
      </Field>
      <Field label="Camera" hint="Leave empty for every camera.">
        {(p) => (
          <input
            {...p}
            value={rule.camera ?? ""}
            placeholder="any camera"
            onChange={(e) => {
              onChange({ ...rule, camera: e.target.value === "" ? null : e.target.value });
            }}
          />
        )}
      </Field>
      <fieldset>
        <legend>Event types (none ticked means every type)</legend>
        <div className="row wrap">
          {EVENT_TYPES.map((t) => (
            <label key={t}>
              <input
                type="checkbox"
                checked={rule.event_types.includes(t)}
                onChange={(e) => {
                  const now = new Set(rule.event_types);
                  if (e.target.checked) now.add(t);
                  else now.delete(t);
                  onChange({ ...rule, event_types: [...now] });
                }}
              />{" "}
              {words(t)}
            </label>
          ))}
        </div>
      </fieldset>
      <Field label="Alert from severity" hint="Replaces the default for what this rule matches.">
        {(p) => (
          <select
            {...p}
            value={rule.min_severity ?? ""}
            onChange={(e) => {
              const v = e.target.value;
              onChange({
                ...rule,
                min_severity: v === "" ? null : (v as NonNullable<Rule["min_severity"]>),
              });
            }}
          >
            <option value="">use the default</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        )}
      </Field>
      <fieldset>
        <legend>Send to (none ticked means every channel)</legend>
        <div className="row wrap">
          {channels.map((c) => (
            <label key={c}>
              <input
                type="checkbox"
                checked={(rule.channels ?? []).includes(c as never)}
                onChange={(e) => {
                  const now = new Set<string>(rule.channels ?? []);
                  if (e.target.checked) now.add(c);
                  else now.delete(c);
                  onChange({ ...rule, channels: now.size === 0 ? null : ([...now] as never) });
                }}
              />{" "}
              {c}
            </label>
          ))}
        </div>
      </fieldset>
      <button type="button" className="danger" onClick={onRemove}>
        Remove rule {rule.name}
      </button>
    </fieldset>
  );
}

function QuietRow({
  quiet,
  onChange,
  onRemove,
}: {
  quiet: Quiet;
  onChange: (next: Quiet) => void;
  onRemove: () => void;
}) {
  const days = quiet.schedule.weekdays;
  return (
    <fieldset className="card">
      <legend>Quiet hours: {quiet.name}</legend>
      <Field label="Name">
        {(p) => (
          <input
            {...p}
            value={quiet.name}
            maxLength={64}
            onChange={(e) => {
              onChange({ ...quiet, name: e.target.value });
            }}
          />
        )}
      </Field>
      <div className="row">
        <Field label="From (24 hour)">
          {(p) => (
            <input
              {...p}
              type="time"
              value={quiet.schedule.start}
              onChange={(e) => {
                onChange({ ...quiet, schedule: { ...quiet.schedule, start: e.target.value } });
              }}
            />
          )}
        </Field>
        <Field label="Until">
          {(p) => (
            <input
              {...p}
              type="time"
              value={quiet.schedule.end}
              onChange={(e) => {
                onChange({ ...quiet, schedule: { ...quiet.schedule, end: e.target.value } });
              }}
            />
          )}
        </Field>
      </div>
      <fieldset>
        <legend>Days (none ticked means every day)</legend>
        <div className="row wrap">
          {WEEKDAYS.map((name, index) => (
            <label key={name}>
              <input
                type="checkbox"
                checked={days.includes(index)}
                onChange={(e) => {
                  const now = new Set(days);
                  if (e.target.checked) now.add(index);
                  else now.delete(index);
                  onChange({
                    ...quiet,
                    schedule: { ...quiet.schedule, weekdays: [...now].sort() },
                  });
                }}
              />{" "}
              {name}
            </label>
          ))}
        </div>
      </fieldset>
      <Field
        label="Still interrupt from severity"
        hint="CRITICAL always interrupts. Incidents are recorded whatever you choose here."
      >
        {(p) => (
          <select
            {...p}
            value={quiet.let_through_from}
            onChange={(e) => {
              onChange({ ...quiet, let_through_from: e.target.value as Quiet["let_through_from"] });
            }}
          >
            {SEVERITIES.slice(1).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        )}
      </Field>
      <button type="button" className="danger" onClick={onRemove}>
        Remove quiet hours {quiet.name}
      </button>
    </fieldset>
  );
}

/** The alert policy: who is interrupted, for what, and when. It takes effect the moment it is saved,
 * and it never changes what is detected or recorded. */
export function AlertPolicyEditor({
  initial,
  channels,
  editable,
}: {
  initial: AlertPolicy;
  channels: string[];
  editable: boolean;
}) {
  const [policy, setPolicy] = useState<AlertPolicy>(initial);
  const [baseline, setBaseline] = useState<AlertPolicy>(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<{ message: string; issues: readonly string[] } | null>(null);
  const [saved, setSaved] = useState<AlertSettings | null>(null);
  const rules = policy.rules;
  const quiet = policy.quiet_hours;
  const dirty = JSON.stringify(policy) !== JSON.stringify(baseline);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const out = await sendJson<AlertSettings>("PUT", "/api/v1/settings/alerts", policy);
      setSaved(out);
      setBaseline(policy);
    } catch (e) {
      setError({ message: messageOf(e), issues: e instanceof ApiError ? e.issues : [] });
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <p className="muted">
        This decides who is interrupted. Every incident is still recorded, and CRITICAL always gets
        through.
      </p>
      <label>
        <input
          type="checkbox"
          checked={policy.enabled}
          disabled={!editable}
          onChange={(e) => {
            setPolicy({ ...policy, enabled: e.target.checked });
          }}
        />{" "}
        Send alerts
      </label>
      <Field
        label="Alert from severity"
        hint="Anything below this is recorded but does not interrupt."
      >
        {(p) => (
          <select
            {...p}
            disabled={!editable}
            value={policy.min_severity}
            onChange={(e) => {
              setPolicy({ ...policy, min_severity: e.target.value as AlertPolicy["min_severity"] });
            }}
          >
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        )}
      </Field>

      <h3>Rules</h3>
      <p className="hint">The first rule that matches an incident decides. Up to 32.</p>
      {rules.length === 0 && <p className="muted">No rules: the default applies to everything.</p>}
      {rules.map((rule, i) => (
        <RuleRow
          key={i}
          rule={rule}
          channels={channels}
          onChange={(next) => {
            setPolicy({ ...policy, rules: rules.map((r, j) => (j === i ? next : r)) });
          }}
          onRemove={() => {
            setPolicy({ ...policy, rules: rules.filter((_, j) => j !== i) });
          }}
        />
      ))}
      <button
        type="button"
        disabled={!editable || rules.length >= 32}
        onClick={() => {
          setPolicy({
            ...policy,
            rules: [
              ...rules,
              {
                name: unique(
                  "Rule",
                  rules.map((r) => r.name),
                ),
                camera: null,
                event_types: [],
                min_severity: null,
                channels: null,
              },
            ],
          });
        }}
      >
        Add a rule
      </button>

      <h3>Quiet hours</h3>
      <p className="hint">
        During a quiet window only the chosen severity and above interrupt. Up to 8 windows.
      </p>
      {quiet.length === 0 && <p className="muted">No quiet hours.</p>}
      {quiet.map((q, i) => (
        <QuietRow
          key={i}
          quiet={q}
          onChange={(next) => {
            setPolicy({ ...policy, quiet_hours: quiet.map((x, j) => (j === i ? next : x)) });
          }}
          onRemove={() => {
            setPolicy({ ...policy, quiet_hours: quiet.filter((_, j) => j !== i) });
          }}
        />
      ))}
      <button
        type="button"
        disabled={!editable || quiet.length >= 8}
        onClick={() => {
          setPolicy({
            ...policy,
            quiet_hours: [
              ...quiet,
              {
                name: unique(
                  "Quiet",
                  quiet.map((q) => q.name),
                ),
                schedule: { start: "22:00", end: "06:00", utc_offset_minutes: 0, weekdays: [] },
                let_through_from: "CRITICAL",
              },
            ],
          });
        }}
      >
        Add quiet hours
      </button>

      {error !== null && (
        <Notice kind="error" title="Not saved">
          <p>{error.message}</p>
          {error.issues.length > 0 && (
            <ul>
              {error.issues.map((i) => (
                <li key={i}>{i}</li>
              ))}
            </ul>
          )}
        </Notice>
      )}
      <div className="row actions">
        <button type="submit" className="primary" disabled={!editable || saving || !dirty}>
          {saving ? "Saving…" : "Save alert settings"}
        </button>
        {saved !== null && !dirty && (
          <span role="status" className="hint">
            Saved and in force now.
          </span>
        )}
        {!editable && (
          <span className="hint">Changing alert settings needs the administrator role.</span>
        )}
      </div>
    </form>
  );
}
