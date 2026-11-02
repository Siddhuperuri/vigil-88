import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { TelemetryProvider } from "../state/telemetry";
import { expectNoAccessibilityViolations } from "../test-utils";
import { camera, installSocket, json, lastSocket, mockFetch, snapshot } from "../test-support";
import { System } from "./System";

const storage = {
  disk_total_bytes: 500 * 1024 ** 3,
  disk_free_bytes: 120 * 1024 ** 3,
  database_bytes: 3 * 1024 ** 2,
  evidence_bytes: 800 * 1024 ** 2,
  evidence_files: 42,
  evidence_truncated: false,
  logs_bytes: 2048,
};
const logs = {
  lines: [{ timestamp: "2026-11-02T06:00:00Z", level: "warning", event: "camera degraded" }],
  returned: 1,
  file_bytes: 1000,
  truncated_to_tail: false,
};

const draw = (role: "administrator" | "operator" | "viewer" = "administrator") =>
  render(
    <TelemetryProvider onEnded={() => undefined}>
      <System role={role} />
    </TelemetryProvider>,
  );

beforeEach(() => {
  installSocket();
  mockFetch({ "/api/v1/system/storage": storage, "/api/v1/system/logs": logs });
});

describe("the system view", () => {
  it("waits for the engine before claiming anything", () => {
    draw();
    expect(screen.getByText("Waiting for the engine…")).toBeInTheDocument();
  });

  it("shows health, cameras and machine load, with unavailable for what is not measured", async () => {
    draw();
    lastSocket().open();
    lastSocket().send(
      snapshot({
        health: { level: "degraded", issues: ["camera cam2 is degraded: low fps"] },
        cameras: [camera("cam1"), camera("cam2", "degraded", null)],
        system: {
          cpu_percent: 9,
          memory_used_mb: 4000,
          process_rss_mb: 900,
          gpu_utilization_percent: null,
          gpu_memory_used_mb: null,
          gpu_temperature_c: null,
        },
      }),
    );
    expect(await screen.findByText("camera cam2 is degraded: low fps")).toBeInTheDocument();
    expect(screen.getByText("unavailable (no NVML)")).toBeInTheDocument();
    expect(screen.getAllByText("unavailable").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole("table")).toHaveTextContent("cam2");
  });

  it("says when no issues are reported and when there are no cameras", async () => {
    draw();
    lastSocket().open();
    lastSocket().send(snapshot({ cameras: [] }));
    expect(await screen.findByText("No issues reported.")).toBeInTheDocument();
    expect(screen.getByText("No cameras are configured.")).toBeInTheDocument();
  });

  it("treats a failed engine as an error, not a warning", async () => {
    draw();
    lastSocket().open();
    lastSocket().send(
      snapshot({ health: { level: "failed", issues: ["worker response crashed"] } }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("worker response crashed");
  });

  it("reports storage in sizes, and says when the evidence count is only a lower bound", async () => {
    mockFetch({
      "/api/v1/system/storage": { ...storage, evidence_truncated: true },
      "/api/v1/system/logs": logs,
    });
    draw();
    expect(await screen.findByText(/120 GB of 500 GB/)).toBeInTheDocument();
    expect(screen.getByText(/counting stopped early/)).toBeInTheDocument();
  });

  it("keeps the last storage reading and says so when a refresh fails", async () => {
    draw();
    await screen.findByText(/120 GB of 500 GB/);
    mockFetch({
      "/api/v1/system/storage": json({ detail: "disk busy" }, { status: 500 }),
      "/api/v1/system/logs": logs,
    });
    await userEvent.click(screen.getAllByRole("button", { name: "Refresh" })[0] as HTMLElement);
    expect(await screen.findByText("Could not read storage")).toBeInTheDocument();
    expect(screen.getByText(/120 GB of 500 GB/)).toBeInTheDocument();
  });

  it("shows logs to an administrator and filters by level through the engine", async () => {
    const { calls } = mockFetch({
      "/api/v1/system/storage": storage,
      "/api/v1/system/logs": logs,
    });
    draw("administrator");
    expect(await screen.findByLabelText("Recent log records")).toHaveTextContent(
      "WARNING camera degraded",
    );
    await userEvent.selectOptions(screen.getByLabelText("Level at least"), "error");
    await waitFor(() => {
      expect(calls.at(-1)?.url).toContain("level=error");
    });
  });

  it("says plainly that there is no log file yet", async () => {
    mockFetch({
      "/api/v1/system/storage": storage,
      "/api/v1/system/logs": { lines: [], returned: 0, file_bytes: null, truncated_to_tail: false },
    });
    draw();
    expect(await screen.findByText("There is no log file yet.")).toBeInTheDocument();
  });

  it("says so when the log exists but nothing is recorded at the chosen level", async () => {
    mockFetch({
      "/api/v1/system/storage": storage,
      "/api/v1/system/logs": { lines: [], returned: 0, file_bytes: 900, truncated_to_tail: false },
    });
    draw();
    expect(await screen.findByText("No log records at this level yet.")).toBeInTheDocument();
  });

  it("notes when only the tail of a large log is shown", async () => {
    mockFetch({
      "/api/v1/system/storage": storage,
      "/api/v1/system/logs": { ...logs, truncated_to_tail: true },
    });
    draw();
    expect(await screen.findByText(/most recent part of a large log/)).toBeInTheDocument();
  });

  it("does not ask for logs at all unless the user is an administrator", async () => {
    const { calls } = mockFetch({ "/api/v1/system/storage": storage, "/api/v1/system/logs": logs });
    draw("operator");
    expect(await screen.findByText("Logs are visible to administrators.")).toBeInTheDocument();
    await screen.findByText(/120 GB of 500 GB/);
    expect(calls.some((c) => c.url.includes("/system/logs"))).toBe(false);
  });

  it("has no WCAG 2.2 AA violations", async () => {
    const { container } = draw();
    lastSocket().open();
    lastSocket().send(snapshot());
    await screen.findByLabelText("Recent log records");
    await expectNoAccessibilityViolations(container);
  });
});
