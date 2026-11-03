import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TelemetryProvider } from "../state/telemetry";
import { expectNoAccessibilityViolations } from "../test-utils";
import {
  camera,
  installSocket,
  lastSocket,
  mockFetch,
  snapshot,
  stubObjectUrls,
} from "../test-support";
import { Wall } from "./Wall";

const draw = (camId: string | null = null) =>
  render(
    <TelemetryProvider onEnded={() => undefined}>
      <Wall route={{ view: "wall", camera: camId }} />
    </TelemetryProvider>,
  );
const feed = (cameras: unknown[]) => {
  lastSocket().open();
  lastSocket().send(snapshot({ cameras }));
};

beforeEach(() => {
  installSocket();
  window.localStorage.clear();
  mockFetch({});
});

describe("the wall", () => {
  it("waits honestly for the engine, then says when there are no cameras", async () => {
    draw();
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for the engine");
    feed([]);
    expect(await screen.findByText("No cameras yet")).toBeInTheDocument();
  });

  it("says reconnecting rather than waiting when the link drops before any data", async () => {
    draw();
    lastSocket().open();
    lastSocket().end(1006);
    expect(await screen.findByText(/Reconnecting to the engine/)).toBeInTheDocument();
  });

  it("shows a stream for each online camera, asking the engine for the chosen overlays", async () => {
    draw();
    feed([camera("cam1"), camera("cam2")]);
    const first = await screen.findByAltText("Live view of cam1");
    expect(first).toHaveAttribute(
      "src",
      "/api/v1/cameras/cam1/stream.mjpeg?overlay=boxes,labels,info",
    );
    await userEvent.click(screen.getByRole("checkbox", { name: "Labels" }));
    await waitFor(() => {
      expect(screen.getByAltText("Live view of cam1")).toHaveAttribute(
        "src",
        "/api/v1/cameras/cam1/stream.mjpeg?overlay=boxes,info",
      );
    });
    for (const name of ["Boxes", "Info"]) {
      await userEvent.click(screen.getByRole("checkbox", { name }));
    }
    await waitFor(() => {
      expect(screen.getByAltText("Live view of cam1")).toHaveAttribute(
        "src",
        expect.stringContaining("overlay=none"),
      );
    });
  });

  it("offers only the overlays that exist, so no control promises a track or zone it cannot draw", async () => {
    draw();
    feed([camera("cam1")]);
    const group = await screen.findByRole("group", { name: "Overlays" });
    expect(
      within(group)
        .getAllByRole("checkbox")
        .map((c) => c.closest("label")?.textContent),
    ).toEqual(["Boxes", "Labels", "Info"]);
  });

  it("names an offline camera in words and shows no picture for it", async () => {
    draw();
    feed([camera("cam1", "reconnecting", null), camera("cam2")]);
    expect(await screen.findByText(/No picture: camera is reconnecting/)).toBeInTheDocument();
    expect(screen.getByText("RECONNECTING")).toBeInTheDocument();
    expect(screen.queryByAltText("Live view of cam1")).not.toBeInTheDocument();
    expect(screen.getAllByText("unavailable").length).toBeGreaterThan(0); // unknown, not zero
  });

  it("can stop watching one tile and resume it", async () => {
    draw();
    feed([camera("cam1")]);
    await userEvent.click(await screen.findByRole("button", { name: "Stop watching" }));
    expect(screen.queryByAltText("Live view of cam1")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(await screen.findByAltText("Live view of cam1")).toBeInTheDocument();
  });

  it("remembers the layout, and survives storage being unavailable", async () => {
    draw();
    feed([camera("cam1"), camera("cam2")]);
    await userEvent.click(await screen.findByRole("radio", { name: "3 × 3" }));
    expect(window.localStorage.getItem("vigil.wall.layout")).toBe("3");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const second = draw();
    feed([camera("cam1")]);
    expect((await second.findAllByRole("radio", { name: "Auto" })).length).toBeGreaterThan(0);
    await userEvent.click(second.getAllByRole("radio", { name: "1" })[0] as HTMLElement);
  });

  it("expands one camera and offers the way back", async () => {
    draw("cam2");
    feed([camera("cam1"), camera("cam2")]);
    expect(await screen.findByAltText("Live view of cam2")).toBeInTheDocument();
    expect(screen.queryByAltText("Live view of cam1")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to wall" })).toHaveAttribute("href", "#/wall");
  });

  it("explains a camera that is not there", async () => {
    draw("ghost");
    feed([camera("cam1")]);
    expect(await screen.findByText("No camera called ghost")).toBeInTheDocument();
  });

  it("saves a snapshot through the engine", async () => {
    const { calls } = mockFetch({
      "/api/v1/cameras/cam1/snapshot.jpg": new Response(new Blob(["jpg"])),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const { create } = stubObjectUrls();
    draw();
    feed([camera("cam1")]);
    await userEvent.click(await screen.findByRole("button", { name: "Save snapshot" }));
    await waitFor(() => {
      expect(
        calls.some((c) => c.url.startsWith("/api/v1/cameras/cam1/snapshot.jpg?overlay=")),
      ).toBe(true);
    });
    expect(create).toHaveBeenCalled();
  });

  it("toggles full screen through the browser", async () => {
    const request = vi.fn(() => Promise.resolve());
    Object.defineProperty(document.documentElement, "requestFullscreen", {
      value: request,
      configurable: true,
    });
    draw();
    feed([camera("cam1")]);
    await userEvent.click(await screen.findByRole("button", { name: "Full screen" }));
    expect(request).toHaveBeenCalled();
  });

  it("has no WCAG 2.2 AA violations with cameras showing", async () => {
    const { container } = draw();
    feed([camera("cam1"), camera("cam2", "offline", null)]);
    await screen.findByAltText("Live view of cam1");
    await expectNoAccessibilityViolations(container);
  });
});
