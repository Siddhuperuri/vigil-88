import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import type { LineConfig } from "../api/types";
import { PendingProvider } from "../state/pending";
import { TelemetryProvider } from "../state/telemetry";
import { expectNoAccessibilityViolations } from "../test-utils";
import { bodyOf, installSocket, mockFetch } from "../test-support";
import { LinesPanel } from "./LinesPanel";
import { Zones } from "./Zones";

const line = (over: Partial<LineConfig> = {}): LineConfig => ({
  line_id: "door",
  start: [0.2, 0.5],
  end: [0.8, 0.5],
  direction: "to_right",
  label: "Back door",
  criticality: 0,
  schedule: null,
  ...over,
});

function Harness({ initial, editable = true }: { initial: LineConfig[]; editable?: boolean }) {
  const [lines, setLines] = useState(initial);
  return (
    <>
      <LinesPanel lines={lines} onChange={setLines} editable={editable} />
      <output aria-label="state">{JSON.stringify(lines)}</output>
    </>
  );
}
const state = (): LineConfig[] =>
  JSON.parse(screen.getByLabelText("state").textContent) as LineConfig[];

beforeEach(() => {
  installSocket();
});

describe("the crossing lines panel", () => {
  it("says there are none, and adds one with a free name", async () => {
    const { container } = render(<Harness initial={[]} />);
    expect(screen.getByText("No crossing lines.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add a crossing line" }));
    await userEvent.click(screen.getByRole("button", { name: "Add a crossing line" }));
    expect(state().map((l) => l.line_id)).toEqual(["line-1", "line-2"]);
    expect(state()[0]).toMatchObject({ start: [0.2, 0.5], end: [0.8, 0.5], direction: "any" });
    await expectNoAccessibilityViolations(container);
  });

  it("edits the name, both points and the direction, keeping the numbers inside the picture", async () => {
    render(<Harness initial={[line()]} />);
    const name = screen.getByLabelText("Name");
    await userEvent.clear(name);
    await userEvent.type(name, "Gate");
    await userEvent.clear(screen.getByLabelText("Start across"));
    await userEvent.type(screen.getByLabelText("Start across"), "0.1");
    await userEvent.clear(screen.getByLabelText("Start down"));
    await userEvent.type(screen.getByLabelText("Start down"), "7"); // out of range: clamped
    await userEvent.clear(screen.getByLabelText("End across"));
    await userEvent.type(screen.getByLabelText("End across"), "abc"); // not a number: kept
    await userEvent.clear(screen.getByLabelText("End down"));
    await userEvent.type(screen.getByLabelText("End down"), "0.9");
    await userEvent.selectOptions(screen.getByLabelText("Direction that counts"), "to_left");
    await userEvent.tab();
    expect(state()[0]).toMatchObject({
      label: "Gate",
      start: [0.1, 1],
      end: [0.8, 0.9],
      direction: "to_left",
    });
  });

  it("clears the name back to the id, and removes a line", async () => {
    render(<Harness initial={[line()]} />);
    await userEvent.clear(screen.getByLabelText("Name"));
    expect(state()[0]?.label).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Remove line door" }));
    expect(screen.getByText("No crossing lines.")).toBeInTheDocument();
  });

  it("stops adding at sixteen", () => {
    render(
      <Harness
        initial={Array.from({ length: 16 }, (_, i) => line({ line_id: `l${String(i)}` }))}
      />,
    );
    expect(screen.getByRole("button", { name: "Add a crossing line" })).toBeDisabled();
  });

  it("can only be read by someone who may not edit", () => {
    render(<Harness initial={[line()]} editable={false} />);
    expect(screen.getByLabelText("Name")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Add a crossing line" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Remove line/ })).toBeNull();
  });
});

describe("saving zones keeps the lines", () => {
  const admin = "administrator" as const;
  const square = {
    zone_id: "dock-1",
    kind: "restricted",
    label: "Dock",
    criticality: 2,
    polygon: [
      [0.1, 0.1],
      [0.5, 0.1],
      [0.5, 0.5],
      [0.1, 0.5],
    ],
    schedule: null,
  };

  it("sends the lines back with the zones, so a zone edit never erases a tripwire", async () => {
    const out = {
      zones_ref: "dock",
      zones: { version: 1, zones: [square], lines: [line()] },
      pending: false,
      editable: true,
    };
    const { calls } = mockFetch({
      "/api/v1/config/pending": { pending: false, changes: [], can_restart: true },
      "/api/v1/config/cameras/dock/zones": out,
      "/api/v1/config/cameras": [
        {
          config: { camera_id: "dock", source: { kind: "video_file" } },
          editable: true,
          state: "online",
          pending: null,
          credential_stored: null,
        },
      ],
    });
    render(
      <TelemetryProvider onEnded={() => undefined}>
        <PendingProvider>
          <Zones route={{ view: "zones", camera: "dock" }} role={admin} />
        </PendingProvider>
      </TelemetryProvider>,
    );
    const name = await screen
      .findByLabelText("Name", { selector: "#lines-title ~ fieldset input" })
      .catch(() => screen.getAllByLabelText("Name").at(-1) as HTMLElement);
    await userEvent.clear(name);
    await userEvent.type(name, "Front door");
    await userEvent.click(screen.getByRole("button", { name: "Save zones" }));
    await screen.findByText(/Saved\. Takes effect when monitoring restarts/);
    const put = calls.find((c) => c.init?.method === "PUT");
    const sent = bodyOf(put) as { zones: unknown[]; lines: { line_id: string; label: string }[] };
    expect(sent.zones).toHaveLength(1);
    expect(sent.lines.map((l) => [l.line_id, l.label])).toEqual([["door", "Front door"]]);
  });
});
