import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { PendingBar } from "../components/PendingBar";
import { expectNoAccessibilityViolations } from "../test-utils";
import { installSocket, json, mockFetch } from "../test-support";
import { Wrap, none } from "./control-support";
import { Wizard } from "./Wizard";

beforeEach(() => {
  installSocket();
});

// The cameras the guide and the thresholds screen both list.
const camerasList = (editable = true) => [
  {
    config: { camera_id: "yard", name: "Yard", source: { kind: "synthetic" } },
    editable,
    state: "online",
    pending: null,
    credential_stored: null,
  },
];

// ------------------------------------------------------------------ the setup guide

describe("the setup guide", () => {
  const draw = (role: "administrator" | "operator" = "administrator") =>
    render(
      <Wrap>
        <Wizard role={role} />
      </Wrap>,
    );

  it("needs an administrator", () => {
    mockFetch({ "/api/v1/config/pending": none, "/api/v1/config/cameras": [] });
    draw("operator");
    expect(screen.getByText(/needs an administrator/)).toBeInTheDocument();
  });

  it("walks through every step, can go back, and ends at the live wall", async () => {
    mockFetch({ "/api/v1/config/pending": none, "/api/v1/config/cameras": camerasList() });
    const { container } = draw();
    expect(screen.getByRole("heading", { name: "Welcome" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Step 1 of 5: Welcome");
    await userEvent.click(screen.getByRole("button", { name: "Next" }));

    expect(await screen.findByRole("heading", { name: "Add a camera" })).toBeInTheDocument();
    expect(await screen.findByText(/1 camera is set up: yard/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add another camera" }));
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));

    expect(screen.getByRole("heading", { name: "Zones" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Draw zones for yard" })).toHaveAttribute(
      "href",
      "#/zones/yard",
    );
    await userEvent.click(screen.getByRole("button", { name: "Next (or skip)" }));

    expect(screen.getByRole("heading", { name: "Alerts" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open alert settings" })).toHaveAttribute(
      "href",
      "#/settings",
    );
    await expectNoAccessibilityViolations(container);
    await userEvent.click(screen.getByRole("button", { name: "Next (or skip)" }));

    expect(screen.getByRole("heading", { name: "Finish" })).toBeInTheDocument();
    expect(screen.getByText(/Everything you saved is in use/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Done" })).toHaveAttribute("href", "#/wall");
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByRole("heading", { name: "Alerts" })).toBeInTheDocument();
  });

  it("shows the camera form when there is no camera yet, and skips the zones step honestly", async () => {
    mockFetch({ "/api/v1/config/pending": none, "/api/v1/config/cameras": [] });
    draw();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("button", { name: /Add camera|Save/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Add a camera first, or skip this step.")).toBeInTheDocument();
  });

  it("offers the restart only when something saved is waiting, and states its cost", async () => {
    const waiting = {
      pending: true,
      changes: [{ kind: "camera", target: "yard", change: "added" }],
      can_restart: true,
    };
    const { calls } = mockFetch({
      "/api/v1/config/pending": waiting,
      "/api/v1/config/cameras": camerasList(),
      "/api/v1/config/apply": { ...waiting, pending: false },
    });
    draw();
    for (let i = 0; i < 4; i++)
      await userEvent.click(screen.getByRole("button", { name: /^Next/ }));
    expect(await screen.findByText(/every camera pauses for a few seconds/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Restart monitoring now" }));
    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/config/apply"))).toBe(true);
    });
  });
});

// ------------------------------------------------------------------ applying zone edits now

describe("the pending bar", () => {
  const waiting = (kind: string) => ({
    pending: true,
    changes: [{ kind, target: "yard", change: "changed" }],
    can_restart: true,
  });
  const admin = "administrator" as const;
  const drawBar = () =>
    render(
      <Wrap>
        <PendingBar role={admin} />
      </Wrap>,
    );

  it("offers to apply zone changes now, and says what took effect", async () => {
    const { calls } = mockFetch({
      "/api/v1/config/apply-live": {
        applied: ["yard"],
        needs_restart: [],
        pending: none,
      },
      "/api/v1/config/pending": waiting("zones"),
    });
    drawBar();
    await userEvent.click(await screen.findByRole("button", { name: "Apply zone changes now" }));
    expect(await screen.findByText("Applied now: yard.")).toBeInTheDocument();
    expect(calls.filter((c) => c.url.endsWith("/apply-live"))).toHaveLength(1);
  });

  it("explains what could not be applied now", async () => {
    mockFetch({
      "/api/v1/config/apply-live": {
        applied: [],
        needs_restart: [{ target: "yard", reason: "it applies after a restart" }],
        pending: waiting("zones"),
      },
      "/api/v1/config/pending": waiting("zones"),
    });
    drawBar();
    await userEvent.click(await screen.findByRole("button", { name: "Apply zone changes now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("yard: it applies after a restart");
  });

  it("reports a failure to apply", async () => {
    mockFetch({
      "/api/v1/config/apply-live": json({ detail: "forbidden" }, { status: 403 }),
      "/api/v1/config/pending": waiting("zones"),
    });
    drawBar();
    await userEvent.click(await screen.findByRole("button", { name: "Apply zone changes now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("forbidden");
  });

  it("does not offer it for a change that needs a restart anyway", async () => {
    mockFetch({ "/api/v1/config/pending": waiting("camera") });
    drawBar();
    await screen.findByText(/waiting for monitoring to restart/);
    expect(screen.queryByRole("button", { name: "Apply zone changes now" })).toBeNull();
  });
});
