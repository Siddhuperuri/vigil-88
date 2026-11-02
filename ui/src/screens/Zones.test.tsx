import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { PendingProvider } from "../state/pending";
import { TelemetryProvider } from "../state/telemetry";
import { expectNoAccessibilityViolations } from "../test-utils";
import { bodyOf, installSocket, json, mockFetch } from "../test-support";
import { Zones } from "./Zones";

const square = (id: string) => ({
  zone_id: id,
  kind: "restricted",
  label: "Loading dock",
  criticality: 2,
  polygon: [
    [0.1, 0.1],
    [0.5, 0.1],
    [0.5, 0.5],
    [0.1, 0.5],
  ],
  schedule: null,
});
const zonesOut = (over: Record<string, unknown> = {}) => ({
  zones_ref: "dock",
  zones: { version: 1, zones: [square("dock-1")] },
  pending: false,
  editable: true,
  ...over,
});
const cameras = [
  {
    config: { camera_id: "dock", source: { kind: "video_file" } },
    editable: true,
    state: "online",
    pending: null,
    credential_stored: null,
  },
];
const none = { pending: false, changes: [], can_restart: true };

function Wrap({ children }: { children: ReactNode }) {
  return (
    <TelemetryProvider onEnded={() => undefined}>
      <PendingProvider>{children}</PendingProvider>
    </TelemetryProvider>
  );
}
const draw = (role: "administrator" | "viewer" = "administrator", camera: string | null = "dock") =>
  render(
    <Wrap>
      <Zones route={{ view: "zones", camera }} role={role} />
    </Wrap>,
  );

const routes = (put?: unknown, zones: unknown = zonesOut()) => ({
  "/api/v1/config/pending": none,
  "/api/v1/config/cameras/dock/zones": (_u: string, init: RequestInit | undefined) =>
    init?.method === "PUT" ? (put ?? zones) : zones,
  "/api/v1/config/cameras": cameras,
});

beforeEach(() => {
  installSocket();
});

describe("the zone editor", () => {
  it("asks for a camera first, and is accessible with a zone drawn", async () => {
    mockFetch(routes());
    const first = draw("administrator", null);
    expect(await screen.findByText("Choose a camera.")).toBeInTheDocument();
    first.unmount();
    mockFetch(routes());
    const { container } = draw();
    expect(await screen.findByRole("button", { name: /Point 1 of dock-1/ })).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("moves a point with the arrow keys and offers the same change as typed numbers", async () => {
    mockFetch(routes());
    draw();
    const handle = await screen.findByRole("button", { name: /Point 1 of dock-1/ });
    handle.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(
      screen.getByRole("button", { name: /Point 1 of dock-1, at 0.11, 0.1/ }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Point 1 across")).toHaveValue(0.11);
    fireEvent.change(screen.getByLabelText("Point 1 down"), { target: { value: "0.2" } });
    expect(
      screen.getByRole("button", { name: /Point 1 of dock-1, at 0.11, 0.2/ }),
    ).toBeInTheDocument();
  });

  it("will not save a zone that crosses itself, and says which one and why", async () => {
    mockFetch(routes());
    draw();
    await screen.findByLabelText("Point 1 across");
    // Swap the last two corners so the edges cross in a bow-tie.
    fireEvent.change(screen.getByLabelText("Point 3 across"), { target: { value: "0.1" } });
    fireEvent.change(screen.getByLabelText("Point 4 across"), { target: { value: "0.5" } });
    expect(screen.getByRole("button", { name: "Save zones" })).toBeDisabled();
    expect(screen.getByText(/Save is off until “dock-1” is fixed/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("will not remove the third-from-last point", async () => {
    mockFetch(
      routes(
        {},
        zonesOut({
          zones: {
            version: 1,
            zones: [
              {
                ...square("dock-1"),
                polygon: [
                  [0.1, 0.1],
                  [0.5, 0.1],
                  [0.5, 0.5],
                ],
              },
            ],
          },
        }),
      ),
    );
    draw();
    expect(await screen.findByRole("button", { name: "Remove point 1" })).toBeDisabled();
  });

  it("adds a zone, saves it, and says it takes effect on restart", async () => {
    const { calls } = mockFetch(routes());
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add a zone" }));
    expect(await screen.findByLabelText("Point 4 down")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("What it is"), "exclusion");
    await userEvent.click(screen.getByRole("button", { name: "Save zones" }));
    await waitFor(() => {
      expect(calls.some((c) => c.init?.method === "PUT")).toBe(true);
    });
    const body = bodyOf(calls.find((c) => c.init?.method === "PUT")) as {
      zones: { zone_id: string; kind: string }[];
    };
    expect(body.zones.map((z) => [z.zone_id, z.kind])).toEqual([
      ["dock-1", "restricted"],
      ["zone-2", "exclusion"],
    ]);
  });

  it("keeps the edits and shows the engine's reasons when saving is refused", async () => {
    mockFetch(
      routes(
        json(
          { detail: { message: "Zones refused.", issues: ["zones[0]: overlaps another"] } },
          { status: 422 },
        ),
      ),
    );
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add a zone" }));
    await userEvent.click(screen.getByRole("button", { name: "Save zones" }));
    expect(await screen.findByText("zones[0]: overlaps another")).toBeInTheDocument();
    expect(screen.getByText("Nothing was changed. Your edits are still here.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save zones" })).toBeEnabled();
  });

  it("can discard edits, and shows a viewer the zones without any way to change them", async () => {
    mockFetch(routes());
    const first = draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add a zone" }));
    await userEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.queryByRole("button", { name: "zone-2" })).not.toBeInTheDocument();
    first.unmount();
    mockFetch(routes());
    draw("viewer");
    expect(await screen.findByText("View only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save zones" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add a zone" })).toBeDisabled();
  });

  it("says when a zone file is waiting for a restart, and keeps a schedule it cannot edit", async () => {
    mockFetch(
      routes(
        {},
        zonesOut({
          pending: true,
          zones: {
            version: 1,
            zones: [
              {
                ...square("dock-1"),
                schedule: { start: "22:00", end: "06:00", utc_offset_minutes: 0, weekdays: [] },
              },
            ],
          },
        }),
      ),
    );
    draw();
    expect(await screen.findByText(/waiting for monitoring to restart/)).toBeInTheDocument();
    expect(screen.getByText(/22:00 to 06:00/)).toBeInTheDocument();
  });

  it("drags a point with a pointer, adds one by double-click and removes one with Delete", async () => {
    mockFetch(routes());
    const { container } = draw();
    const handle = await screen.findByRole("button", { name: /Point 1 of dock-1/ });
    const frame = container.querySelector(".zone-frame") as HTMLElement;
    frame.getBoundingClientRect = () =>
      ({
        left: 0,
        top: 0,
        width: 200,
        height: 100,
        right: 200,
        bottom: 100,
        x: 0,
        y: 0,
      }) as DOMRect;
    fireEvent.pointerDown(handle);
    fireEvent.pointerMove(frame, { clientX: 100, clientY: 50 });
    expect(
      screen.getByRole("button", { name: /Point 1 of dock-1, at 0.5, 0.5/ }),
    ).toBeInTheDocument();
    fireEvent.pointerUp(frame);
    fireEvent.pointerMove(frame, { clientX: 20, clientY: 20 }); // no longer dragging
    expect(
      screen.getByRole("button", { name: /Point 1 of dock-1, at 0.5, 0.5/ }),
    ).toBeInTheDocument();

    fireEvent.doubleClick(container.querySelector("svg") as SVGElement, {
      clientX: 150,
      clientY: 75,
    });
    expect(screen.getByLabelText("Point 5 across")).toHaveValue(0.75);

    screen.getByRole("button", { name: /Point 5 of dock-1/ }).focus();
    await userEvent.keyboard("{Delete}");
    expect(screen.queryByLabelText("Point 5 across")).not.toBeInTheDocument();
    screen.getByRole("button", { name: /Point 1 of dock-1/ }).focus();
    await userEvent.keyboard("{Shift>}{ArrowDown}{/Shift}x");
    expect(
      screen.getByRole("button", { name: /Point 1 of dock-1, at 0.5, 0.55/ }),
    ).toBeInTheDocument();
  });

  it("says there is no picture when the camera cannot give one, and zones can still be drawn", async () => {
    mockFetch(routes());
    const { container } = draw();
    await screen.findByRole("button", { name: /Point 1 of dock-1/ });
    fireEvent.error(container.querySelector("img") as HTMLImageElement);
    expect(
      await screen.findByText(/No picture: this camera is not running yet/),
    ).toBeInTheDocument();
  });

  it("deletes a zone and refuses to take the polygon below three points with the keyboard", async () => {
    mockFetch(routes());
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add a point" }));
    expect(screen.getByLabelText("Point 5 across")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete this zone" }));
    expect(screen.getByText("This camera has no zones.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save zones" })).toBeEnabled();
  });
});
