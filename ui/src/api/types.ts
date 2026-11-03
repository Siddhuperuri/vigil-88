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
export type LineConfig = Schemas["LineConfig"];
export type ZonesOut = Schemas["ZonesOut"];
export type ProbeResult = Schemas["ProbeOut"];
export type Devices = Schemas["DevicesOut"];
export type Pending = Schemas["PendingOut"];
export type PendingChange = Schemas["PendingChange"];

export type AuditEntry = Schemas["AuditOut"];
export type Summary = Schemas["AnalyticsSummaryOut"];
export type DurationStat = Schemas["DurationOut"];
export type ExportFormat = Schemas["ExportFormatOut"];
export type NewUser = Schemas["NewUserIn"];
export interface ChainReport {
  ok: boolean;
  rows: number;
  first_bad_seq: number | null;
}

export type Candidate = Schemas["CandidateOut"];
export type Timeline = Schemas["TimelineOut"];
export type HealthEvent = Schemas["HealthEventOut"];

export type ModuleAvailability = Schemas["ModuleAvailabilityOut"];

export type WorkerInfo = Schemas["WorkerOut"];
export type RestartResult = Schemas["RestartOut"];
export type AlertSettings = Schemas["AlertSettingsOut"];
export type AlertPolicy = Schemas["AlertPolicyDoc"];
export type ImportOutcome = Schemas["ImportOut"];
export type TuningModule = Schemas["TuningModule"];
export type TuningParam = Schemas["TuningParam"];
export type LiveApply = Schemas["LiveApplyOut"];
export type EvidenceAnnotations = Schemas["EvidenceAnnotationsOut"];
