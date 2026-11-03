import { useEffect, useRef } from "react";
import { useFetch } from "../api/useFetch";

interface Reason {
  code: string;
  label: string;
}

/**
 * The fixed reasons for dismissing an incident. The choice is what makes a dismissal countable
 * (reports break dismissals down by it), and it is the operator's label, not a verified cause.
 */
export function DismissReasons({
  value,
  onChange,
  onEscape,
}: {
  value: string | null;
  onChange: (code: string) => void;
  onEscape: () => void;
}) {
  const [reasons] = useFetch<Reason[]>("/api/v1/incidents/dismissal-reasons");
  const first = useRef<HTMLInputElement>(null);
  const ready = reasons.state === "ready";
  useEffect(() => {
    // Open on the first choice, so the keyboard path is: d, arrow keys, Enter.
    if (ready) first.current?.focus();
  }, [ready]);

  if (reasons.state === "failed")
    return (
      <p role="alert" className="field-error">
        The list of reasons could not be loaded: {reasons.message} Dismissing needs it.
      </p>
    );
  if (reasons.state !== "ready")
    return (
      <p role="status" className="muted">
        Loading the reasons…
      </p>
    );
  return (
    <fieldset>
      <legend>Why is this being dismissed?</legend>
      {reasons.data.map((r, i) => (
        <label key={r.code} className="check">
          <input
            ref={i === 0 ? first : undefined}
            type="radio"
            name="dismiss-reason"
            value={r.code}
            checked={value === r.code}
            onChange={() => {
              onChange(r.code);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") onEscape();
            }}
          />{" "}
          {r.label}
        </label>
      ))}
    </fieldset>
  );
}
