import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PendingBar } from "../components/PendingBar";
import type { Route } from "../router";
import { PendingProvider } from "../state/pending";
import { TelemetryProvider } from "../state/telemetry";
import { expectNoAccessibilityViolations } from "../test-utils";
import { bodyOf, installSocket, json, lastSocket, mockFetch, snapshot } from "../test-support";
import { Cameras } from "./Cameras";

const source = { kind: "video_file", path: "C:/clips/lobby.mp4", loop: true };
const row = (id: string, extra: Record<string, unknown> = {}) => ({
  config: {
    camera_id: id,
    name: null,
    enabled: true,
    priority: 5,
    target_inference_fps: 5,
    source,
  },
  editable: true,
  state: "online",
  pending: null,
  credential_stored: null,
  ...extra,
});
const none = { pending: false, changes: [], can_restart: true };

function Wrap({ children }: { children: ReactNode }) {
  return (
    <TelemetryProvider onEnded={() => undefined}>
      <PendingProvider>{children}</PendingProvider>
    </TelemetryProvider>
  );
}

const list: Route = { view: "cameras", id: null };
const draw = (
  route: Route = list,
  role: "administrator" | "viewer" = "administrator",
  go = vi.fn(),
) =>
  render(
    <Wrap>
      <PendingBar role={role} />
      <Cameras route={route as Extract<Route, { view: "cameras" }>} role={role} go={go} />
    </Wrap>,
  );

beforeEach(() => {
  installSocket();
});

describe("the camera list", () => {
  it("lists cameras with their saved-but-not-applied state in words", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": [row("lobby"), row("dock", { state: null, pending: "added" })],
    });
    const { container } = draw();
    expect(await screen.findByRole("rowheader", { name: "lobby" })).toBeInTheDocument();
    expect(screen.getByText("Saved. Starts when monitoring restarts.")).toBeInTheDocument();
    expect(screen.getByText("Not running")).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("says plainly when there are no cameras, and a viewer cannot add one", async () => {
    mockFetch({ "/api/v1/config/pending": none, "/api/v1/config/cameras": [] });
    draw(list, "viewer");
    expect(await screen.findByText("No cameras yet")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Add a camera" })).not.toBeInTheDocument();
    expect(screen.getByText("Adding cameras needs the administrator role.")).toBeInTheDocument();
  });

  it("shows a failure with a way to retry instead of an empty list", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": json({ detail: "database is busy" }, { status: 503 }),
    });
    draw();
    expect(await screen.findByText("database is busy")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("removes a camera only after confirmation, keeping the safe choice first", async () => {
    const { calls } = mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras/lobby": () => new Response(null, { status: 204 }),
      "/api/v1/config/cameras": [row("lobby")],
    });
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Remove lobby" }));
    const dialog = await screen.findByRole("dialog", { name: /Remove lobby/ });
    const buttons = within(dialog).getAllByRole("button");
    expect(buttons[0]).toHaveTextContent("Keep it");
    expect(calls.some((c) => c.init?.method === "DELETE")).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove camera" }));
    await waitFor(() => {
      expect(calls.some((c) => c.init?.method === "DELETE")).toBe(true);
    });
  });
});

describe("adding a camera", () => {
  it("refuses a bad name and an address with the password in it, before any request", async () => {
    const { calls } = mockFetch({ "/api/v1/config/pending": none, "/api/v1/config/cameras": [] });
    draw({ view: "cameras", id: "new" });
    await userEvent.selectOptions(await screen.findByLabelText("Kind of source"), "rtsp");
    await userEvent.type(screen.getByLabelText("Camera name"), "Bad Name");
    await userEvent.type(
      screen.getByLabelText("Stream address"),
      "rtsp://admin:hunter2@10.0.0.5/s",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save camera" }));
    expect(await screen.findByText(/Use lower-case letters/)).toBeInTheDocument();
    expect(screen.getByText(/Do not put the username or password/)).toBeInTheDocument();
    expect(screen.getByLabelText("Camera name")).toHaveAttribute("aria-invalid", "true");
    expect(calls.some((c) => c.init?.method === "POST")).toBe(false);
  });

  it("stores the credential first, then saves the camera without it, and clears the password", async () => {
    const { calls } = mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/credentials/gate": json({ ref: "gate", stored: true }),
      "/api/v1/config/cameras": (_u: string, init: RequestInit | undefined) =>
        init?.method === "POST" ? json(row("gate"), { status: 201 }) : [],
    });
    const go = vi.fn();
    draw({ view: "cameras", id: "new" }, "administrator", go);
    await userEvent.type(await screen.findByLabelText("Camera name"), "gate");
    await userEvent.selectOptions(screen.getByLabelText("Kind of source"), "rtsp");
    await userEvent.type(screen.getByLabelText("Stream address"), "rtsp://10.0.0.5/s");
    await userEvent.type(screen.getByLabelText("Credential name"), "gate");
    await userEvent.type(screen.getByLabelText("Username"), "admin");
    await userEvent.type(screen.getByLabelText("Password"), "s3cret-value");
    await userEvent.click(screen.getByRole("button", { name: "Save camera" }));
    await waitFor(() => {
      expect(go).toHaveBeenCalledWith({ view: "cameras", id: null });
    });
    const put = calls.find((c) => c.init?.method === "PUT");
    expect(bodyOf(put)).toEqual({ username: "admin", password: "s3cret-value" });
    const post = calls.find((c) => c.init?.method === "POST");
    expect(JSON.stringify(bodyOf(post))).not.toContain("s3cret-value");
    expect(bodyOf(post)).toMatchObject({
      camera_id: "gate",
      source: { kind: "rtsp", url: "rtsp://10.0.0.5/s", credential_ref: "gate" },
    });
  });

  it("keeps the entries and lists the engine's reasons when it refuses", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": (_u: string, init: RequestInit | undefined) =>
        init?.method === "POST"
          ? json(
              {
                detail: {
                  message: "That camera was refused.",
                  issues: ["source.path: no such file"],
                },
              },
              { status: 422 },
            )
          : [],
    });
    draw({ view: "cameras", id: "new" });
    await userEvent.type(await screen.findByLabelText("Camera name"), "lobby");
    await userEvent.selectOptions(screen.getByLabelText("Kind of source"), "video_file");
    await userEvent.type(screen.getByLabelText("Video file path"), "C:/nope.mp4");
    await userEvent.click(screen.getByRole("button", { name: "Save camera" }));
    expect(await screen.findByText("source.path: no such file")).toBeInTheDocument();
    expect(
      screen.getByText("Nothing was changed. Your entries are still here."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Video file path")).toHaveValue("C:/nope.mp4");
  });

  it("reports a failed connection test in the engine's words and an unreadable device list honestly", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/devices": {
        available: false,
        reason: "install the capture extra",
        devices: [],
      },
      "/api/v1/config/cameras/test": {
        ok: false,
        message: "could not connect",
        width_px: null,
        height_px: null,
        fps: null,
        backend: null,
        detail: null,
        timed_out: false,
      },
      "/api/v1/config/cameras": [],
    });
    const { container } = draw({ view: "cameras", id: "new" });
    expect(
      await screen.findByText(/Device names are unavailable: install the capture extra/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("could not connect")).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("does not offer a camera defined by hand for editing", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": [row("legacy", { editable: false })],
    });
    draw({ view: "cameras", id: "legacy" });
    expect(await screen.findByText("Defined outside the console")).toBeInTheDocument();
  });
});

describe("applying saved changes", () => {
  const waiting = {
    pending: true,
    changes: [{ kind: "camera", target: "dock", change: "added" }],
    can_restart: true,
  };

  it("states the cost, defaults to not restarting, and shows the restart until the engine is back", async () => {
    const { calls } = mockFetch({
      "/api/v1/config/pending": waiting,
      "/api/v1/config/apply": json(waiting, { status: 202 }),
      "/api/v1/config/cameras": [row("lobby")],
    });
    draw();
    lastSocket().open();
    lastSocket().send(snapshot());
    expect(await screen.findByText(/1 saved change is waiting/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Apply and restart/ }));
    const dialog = await screen.findByRole("dialog", { name: "Restart monitoring?" });
    expect(
      within(dialog).getByText(/Anything that happens in that time is not seen/),
    ).toBeInTheDocument();
    expect(within(dialog).getAllByRole("button")[0]).toHaveTextContent("Not now");
    expect(calls.some((c) => c.url.includes("/apply"))).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: "Restart now" }));
    expect(await screen.findByText(/Restarting monitoring…/)).toBeInTheDocument();
  });

  it("tells a viewer an administrator must apply, and a bare engine that it must be restarted by hand", async () => {
    mockFetch({ "/api/v1/config/pending": waiting, "/api/v1/config/cameras": [] });
    const first = draw(list, "viewer");
    expect(await screen.findByText("An administrator can apply them.")).toBeInTheDocument();
    first.unmount();
    mockFetch({
      "/api/v1/config/pending": { ...waiting, can_restart: false },
      "/api/v1/config/cameras": [],
    });
    draw();
    expect(await screen.findByText(/not running under the desktop app/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Apply and restart/ })).not.toBeInTheDocument();
  });

  it("shows why a restart failed instead of waiting for ever", async () => {
    mockFetch({
      "/api/v1/config/pending": waiting,
      "/api/v1/config/apply": json(
        { detail: "This engine cannot restart itself." },
        { status: 409 },
      ),
      "/api/v1/config/cameras": [],
    });
    draw();
    await userEvent.click(await screen.findByRole("button", { name: /Apply and restart/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Restart now" }));
    expect(
      await screen.findByText(/Could not restart monitoring: This engine cannot restart itself\./),
    ).toBeInTheDocument();
  });
});
