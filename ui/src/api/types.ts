import type { components } from "./types.gen";

type Schemas = components["schemas"];

export type Camera = Schemas["CameraOut"];
export type Incident = Schemas["IncidentSummaryOut"];
export type IncidentPage = Schemas["IncidentPageOut"];
export type IncidentDetail = Schemas["IncidentDetailOut"];
export type Evidence = Schemas["EvidenceOut"];
export type Verdict = Schemas["VerdictOut"];
export type SystemLoad = Schemas["SystemOut"];
export type Storage = Schemas["StorageOut"];
export type Logs = Schemas["LogsOut"];
export type SessionInfo = Schemas["SessionOut"];
export type SetupResult = Schemas["SetupOut"];
export type User = Schemas["UserOut"];
export type Role = User["role"];

export const SEVERITIES = ["INFO", "LOW", "MODERATE", "HIGH", "CRITICAL"] as const;
export type Severity = (typeof SEVERITIES)[number];

export function asSeverity(value: string): Severity {
  const upper = value.toUpperCase();
  return (SEVERITIES as readonly string[]).includes(upper) ? (upper as Severity) : "INFO";
}

/** A status that still needs a person: the engine has confirmed it and nobody has taken it yet. */
export const NEEDS_ACKNOWLEDGEMENT = "confirmed";
export const CLOSED_STATUSES = ["resolved", "dismissed", "suppressed"];

export type AdminCamera = Schemas["AdminCamera"];
export type CameraConfig = Schemas["CameraConfig"];
export type SourceConfig = Schemas["SourceConfig"];
/** What the console sends: the engine fills every default, so only what is set is sent. */
export type SourceInput = Partial<SourceConfig> & Pick<SourceConfig, "kind">;
export type CameraInput = Partial<Omit<CameraConfig, "source">> &
  Pick<CameraConfig, "camera_id"> & { source: SourceInput };
export type ZoneConfig = Schemas["ZoneConfig"];
export type ZoneSet = Schemas["ZoneSetConfig"];
export type ZonesOut = Schemas["ZonesOut"];
export type ProbeResult = Schemas["ProbeOut"];
export type Devices = Schemas["DevicesOut"];
export type Pending = Schemas["PendingOut"];
export type PendingChange = Schemas["PendingChange"];
