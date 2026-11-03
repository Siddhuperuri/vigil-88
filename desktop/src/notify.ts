/**
 * What may become a native notification, decided in one pure place.
 *
 * The page asks for a notification, and a page is not trusted: it may be showing hostile text (an
 * operator note, a camera name). So the main process re-validates every field, bounds every length,
 * and builds the title and body itself from a fixed vocabulary. Only HIGH and CRITICAL interrupt a
 * person; anything quieter stays in the console (an alert storm destroys the ability to see
 * anything, 06 §10). A notification names severity and camera and never carries an image.
 */

const LOUD = new Set(["HIGH", "CRITICAL"]);
const ID = /^[A-Za-z0-9_-]{1,64}$/;
const WORDS = /^[A-Za-z0-9 _.-]{1,60}$/;

export interface Alert {
  title: string;
  body: string;
  urgent: boolean;
  /** The incident to open when the notification is clicked. Already validated as a plain id. */
  incidentId: string;
}

function text(value: unknown, pattern: RegExp): string | null {
  return typeof value === "string" && pattern.test(value) ? value : null;
}

/** Null when the request is malformed or not loud enough to interrupt anyone. */
export function describeAlert(raw: unknown): Alert | null {
  if (typeof raw !== "object" || raw === null) return null;
  const input = raw as Record<string, unknown>;
  const severity = text(input["severity"], /^[A-Z]{3,10}$/);
  const incidentId = text(input["incidentId"], ID);
  const displayRef = text(input["displayRef"], WORDS);
  const cameraId = text(input["cameraId"], WORDS);
  const eventType = text(input["eventType"], WORDS);
  if (!severity || !LOUD.has(severity)) return null;
  if (!incidentId || !displayRef || !cameraId || !eventType) return null;
  return {
    title: `VIGIL-88: ${severity} incident`,
    body: `${eventType.replace(/_/g, " ")} on ${cameraId} (${displayRef})`,
    urgent: severity === "CRITICAL",
    incidentId,
  };
}
