import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { TelemetryProvider } from "../state/telemetry";
import { expectNoAccessibilityViolations } from "../test-utils";
import { installSocket, json, lastSocket, mockFetch, snapshot } from "../test-support";
import { Capabilities } from "./Capabilities";

const module_ = (extra: Record<string, unknown>) => ({
  module_id: "intrusion",
  event_type: "intrusion",
  version: "1.0.0",
  enabled: true,
  status: "available",
  will_run: true,
  requires: ["tracking"],
  missing_required: [],
  missing_optional: [],
  reason: null,
  description: "A person inside a restricted zone.",
  caveats: [],
  ...extra,
});

const draw = () =>
  render(
    <TelemetryProvider onEnded={() => undefined}>
      <Capabilities />
    </TelemetryProvider>,
  );

beforeEach(() => {
  installSocket();
});

describe("what each camera can detect", () => {
  it("states that detection is unmeasured and what the build lacks, before any camera is known", () => {
    mockFetch({});
    draw();
    expect(screen.getByText("Detection is unmeasured")).toBeInTheDocument();
    expect(screen.getByText(/Fire and smoke are declarations of intent/)).toBeInTheDocument();
    expect(screen.getByText("No camera is running.")).toBeInTheDocument();
    expect(screen.getByText(/Line-crossing zones and module/)).toBeInTheDocument();
  });

  it("lists each module with the engine's own state and reason, and carries caveats along", async () => {
    mockFetch({
      "/api/v1/cameras/cam1/modules": [
        module_({ caveats: ["Velocity is in pixels per second without calibration."] }),
        module_({
          module_id: "fire",
          event_type: "fire",
          status: "unavailable",
          will_run: false,
          reason: "needs CLASSIFICATION, which nothing in this build can grant",
        }),
        module_({
          module_id: "loitering",
          event_type: "loitering",
          enabled: false,
          will_run: false,
        }),
      ],
      "/api/v1/cameras/cam2/modules": json({ detail: "unknown camera" }, { status: 404 }),
    });
    const { container } = draw();
    lastSocket().open();
    lastSocket().send(snapshot());
    expect(await screen.findByText("Will run")).toBeInTheDocument();
    expect(screen.getByText("Cannot run")).toBeInTheDocument();
    expect(screen.getByText("Switched off")).toBeInTheDocument();
    expect(screen.getByText(/needs CLASSIFICATION/)).toBeInTheDocument();
    expect(screen.getByText(/Velocity is in pixels per second/)).toBeInTheDocument();
    expect(
      await screen.findByText(/The camera is saved but not running yet\./),
    ).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });
});
