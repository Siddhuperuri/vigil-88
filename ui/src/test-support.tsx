import { act, waitFor } from "@testing-library/react";
import { expect, vi } from "vitest";

/** A route table for `fetch`: first matching prefix wins; the body is JSON unless a Response. */
export type Handler = (url: string, init: RequestInit | undefined) => unknown;
export type Routes = Record<string, unknown>;

export function json(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

export function mockFetch(routes: Routes) {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  const fn = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    calls.push({ url, init });
    const key = Object.keys(routes).find((k) => url.startsWith(k));
    if (key === undefined)
      return Promise.resolve(json({ detail: `no mock for ${url}` }, { status: 404 }));
    const route = routes[key];
    const out = typeof route === "function" ? (route as Handler)(url, init) : route;
    return Promise.resolve(out instanceof Response ? out.clone() : json(out));
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

/** A WebSocket the test drives by hand. */
export class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
  open(): void {
    act(() => {
      this.onopen?.();
    });
  }
  send(message: unknown): void {
    act(() => {
      this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(message) }));
    });
  }
  sendRaw(data: unknown): void {
    act(() => {
      this.onmessage?.(new MessageEvent("message", { data }));
    });
  }
  end(code: number): void {
    act(() => {
      this.onclose?.({ code });
    });
  }
}

export function installSocket(): void {
  FakeSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeSocket);
}

/** Waits for the app to open its socket: effects run after the render a test just awaited, so under
 * load the socket may not exist yet. Using this instead of `lastSocket` removes that race. */
export async function liveSocket(): Promise<FakeSocket> {
  await waitFor(() => {
    expect(FakeSocket.instances.length).toBeGreaterThan(0);
  });
  return lastSocket();
}

export const lastSocket = (): FakeSocket => {
  const s = FakeSocket.instances.at(-1);
  if (s === undefined) throw new Error("no socket was opened");
  return s;
};

export const camera = (id: string, state = "online", fps: number | null = 10) => ({
  camera_id: id,
  state,
  measured_fps: fps,
  frames_received: 100,
  frames_skipped: 3,
  frames_dropped: 0,
  detail: null,
});

export const snapshot = (overrides: Record<string, unknown> = {}) => ({
  kind: "snapshot",
  state: "running",
  health: { level: "ok", issues: [] },
  uptime_ms: 120_000,
  cameras: [camera("cam1"), camera("cam2")],
  system: {
    cpu_percent: 12.3,
    memory_used_mb: 4096,
    process_rss_mb: 900,
    gpu_utilization_percent: 41,
    gpu_memory_used_mb: 2100,
    gpu_temperature_c: 60,
  },
  ...overrides,
});

export const incidentSummary = (overrides: Record<string, unknown> = {}) => ({
  incident_id: "01INC1",
  display_ref: "INC-20261102-AAAAAA",
  camera_id: "cam1",
  event_type: "intrusion",
  module_id: "intrusion",
  module_version: "1.0.0",
  status: "confirmed",
  severity: "HIGH",
  severity_score: 61.2,
  confidence: 0.9,
  zone_ids: [],
  location: null,
  first_observed_utc: "2026-11-02T06:25:02Z",
  confirmed_utc: "2026-11-02T06:25:06Z",
  last_observed_utc: "2026-11-02T06:25:12Z",
  resolved_utc: null,
  duration_ms: 6000,
  detection_latency_ms: 3400,
  evidence_count: 2,
  evidence_status: "complete",
  has_note: false,
  ...overrides,
});

export const detail = (overrides: Record<string, unknown> = {}) => ({
  incident: incidentSummary(),
  available_transitions: ["active"],
  evidence: [
    {
      evidence_id: "ev1",
      kind: "snapshot_annotated",
      media_type: "image/jpeg",
      bytes: 20_000,
      sha256: "ab".repeat(32),
      captured_utc: "2026-11-02T06:25:06Z",
      expires_utc: null,
      relative_path: "x/y.jpg",
      span_ms: null,
    },
  ],
  explanation: {
    trigger_summary: "intrusion reported for a person in zone yard",
    observations: [{ t_monotonic_ns: 1, signal: 0.7, facts: {} }],
    accumulator_trace: [],
    validator_verdicts: [
      { validator_id: "camera_health", outcome: "pass", reason: "camera online" },
    ],
    severity_factors: [
      { name: "base", raw_value: 0.65, weight: 0.3, contribution: 40.2 },
      { name: "confidence", raw_value: 0.9, weight: 0.2, contribution: 21.0 },
    ],
    thresholds: {},
    capability_caveats: ["intrusion: running without velocity"],
  },
  involved_track_ids: [1],
  model: {},
  system: {},
  tracker_epoch: 1,
  operator_note: null,
  permissions: {
    acknowledge: { allowed: true, reason: null },
    dismiss: { allowed: true, reason: null },
    escalate: { allowed: true, reason: null },
    resolve: { allowed: true, reason: null },
    annotate: { allowed: true, reason: null },
  },
  transitions: [
    {
      at_utc: "2026-11-02T06:25:06Z",
      from_status: null,
      to_status: "confirmed",
      actor: "system",
      reason: "confirmed",
    },
  ],
  ...overrides,
});

export const authStatus = (overrides: Record<string, unknown> = {}) => ({
  accounts_enabled: true,
  setup_required: false,
  authenticated: true,
  locked: false,
  user: {
    user_id: "u1",
    username: "alice",
    role: "operator",
    must_change_password: false,
    disabled: false,
  },
  ...overrides,
});

/** The JSON body a recorded call sent. */
export function bodyOf(call: { init: RequestInit | undefined } | undefined): unknown {
  const body = call?.init?.body;
  return typeof body === "string" ? (JSON.parse(body) as unknown) : undefined;
}

/** jsdom has no object URLs; these are the stand-ins the download paths need. */
export function stubObjectUrls(): {
  create: ReturnType<typeof vi.fn>;
  revoke: ReturnType<typeof vi.fn>;
} {
  const create = vi.fn(() => "blob:x");
  const revoke = vi.fn();
  Object.defineProperty(URL, "createObjectURL", { value: create, configurable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: revoke, configurable: true });
  return { create, revoke };
}
