import { z } from "zod";

/**
 * The live channel messages, validated on arrival (08 §5 rule 3). A message that does not match is
 * dropped and counted, never allowed to corrupt a view.
 */
const camera = z.object({
  camera_id: z.string(),
  state: z.string(),
  measured_fps: z.number().nullable(),
  frames_received: z.number(),
  frames_skipped: z.number(),
  frames_dropped: z.number(),
  detail: z.string().nullable(),
});

const system = z
  .object({
    cpu_percent: z.number(),
    memory_used_mb: z.number(),
    process_rss_mb: z.number(),
    gpu_utilization_percent: z.number().nullable(),
    gpu_memory_used_mb: z.number().nullable(),
    gpu_temperature_c: z.number().nullable(),
  })
  .nullable();

const degradation = z.object({
  step: z.number(),
  label: z.string(),
  reason: z.string().nullable(),
  since_utc: z.string().nullable(),
  suspended_cameras: z.array(z.string()),
});

export const snapshotSchema = z.object({
  kind: z.literal("snapshot"),
  state: z.string(),
  health: z.object({ level: z.enum(["ok", "degraded", "failed"]), issues: z.array(z.string()) }),
  uptime_ms: z.number().nullable(),
  // Absent from an engine that predates the ladder; the console then shows nothing, not a zero.
  degradation: degradation.optional(),
  cameras: z.array(camera),
  system,
});

export const incidentSchema = z.object({
  kind: z.literal("incident"),
  change: z.enum(["new", "updated"]),
  incident: z.object({
    incident_id: z.string(),
    display_ref: z.string(),
    camera_id: z.string(),
    event_type: z.string(),
    severity: z.string(),
    status: z.string(),
  }),
});

export type Snapshot = z.infer<typeof snapshotSchema>;
export type IncidentNotice = z.infer<typeof incidentSchema>;
export type Message = Snapshot | IncidentNotice;

const messageSchema = z.discriminatedUnion("kind", [snapshotSchema, incidentSchema]);

/** Null when the text is not JSON or not one of the known messages. */
export function parseMessage(text: string): Message | null {
  try {
    const parsed = messageSchema.safeParse(JSON.parse(text));
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

/** Close codes the engine uses: the session ended, or the console was locked. */
export const CLOSE_SIGNED_OUT = 4401;
export const CLOSE_LOCKED = 4423;

/** 1 s, 2 s, 4 s and so on, capped at 15 s: hammering a restarting engine helps no one. */
export function reconnectDelayMs(attempt: number): number {
  return Math.min(1000 * 2 ** Math.max(0, attempt - 1), 15_000);
}
