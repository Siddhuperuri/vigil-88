import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { expectNoAccessibilityViolations } from "./test-utils";
import { authStatus, installSocket, json, liveSocket, mockFetch, snapshot } from "./test-support";

beforeEach(() => {
  installSocket();
  window.location.hash = "";
});

describe("the gate", () => {
  it("says it is connecting, politely, while it waits", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => undefined)),
    );
    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("Connecting to the engine");
  });

  it("says plainly when the engine cannot be reached, and lets the person try again", async () => {
    const fn = vi
      .fn<typeof fetch>()
      .mockRejectedValueOnce(new TypeError("down"))
      .mockResolvedValueOnce(json(authStatus({ authenticated: false, user: null })));
    vi.stubGlobal("fetch", fn);
    render(<App />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The engine is not responding");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Sign in to VIGIL-88" })).toBeInTheDocument();
  });

  it("shows setup when no administrator exists, and nothing else", async () => {
    mockFetch({
      "/api/v1/auth/status": authStatus({ setup_required: true, authenticated: false, user: null }),
    });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Set up VIGIL-88" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("shows the lock screen for a locked session, naming who it will unlock", async () => {
    mockFetch({ "/api/v1/auth/status": authStatus({ locked: true }) });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Console locked" })).toBeInTheDocument();
    expect(screen.getByText(/Monitoring continues/)).toHaveTextContent("alice");
  });

  it("has no WCAG 2.2 AA violations on the sign-in, setup and lock screens", async () => {
    for (const status of [
      authStatus({ authenticated: false, user: null }),
      authStatus({ setup_required: true, authenticated: false, user: null }),
      authStatus({ locked: true }),
    ]) {
      mockFetch({ "/api/v1/auth/status": status });
      const { container, unmount } = render(<App />);
      await screen.findByRole("main");
      await expectNoAccessibilityViolations(container);
      unmount();
    }
  });
});

describe("the signed-in shell", () => {
  const signedIn = () =>
    mockFetch({
      "/api/v1/auth/status": authStatus(),
      "/api/v1/auth/lock": new Response(null, { status: 204 }),
      "/api/v1/auth/logout": new Response(null, { status: 204 }),
      "/api/v1/cameras/": new Response("", { status: 200 }),
    });

  it("lands on the live wall and offers the views an operator may use", async () => {
    signedIn();
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Live wall" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(
      within(nav)
        .getAllByRole("link")
        .map((a) => a.textContent),
    ).toEqual(["Live wall", "Incident desk", "Cameras", "Zones", "Reports", "System"]);
    expect(within(nav).getByRole("link", { name: "Live wall" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("offers People and the audit log to an administrator only, and refuses them by address to others", async () => {
    mockFetch({
      "/api/v1/auth/status": authStatus({
        user: { ...authStatus().user, role: "administrator" },
      }),
      "/api/v1/users": [],
      "/api/v1/audit": [],
    });
    const admin = render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "People" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Audit log" })).toBeInTheDocument();
    admin.unmount();

    signedIn();
    window.location.hash = "#/people";
    render(<App />);
    expect(await screen.findByText("Administrators only")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "People" })).not.toBeInTheDocument();
  });

  it("shows engine state, cameras and load in the strip once the live link delivers them, and says unavailable before", async () => {
    signedIn();
    render(<App />);
    const strip = await screen.findByRole("banner");
    expect(strip).toHaveTextContent("cameras unavailable");
    (await liveSocket()).open();
    (await liveSocket()).send(snapshot());
    expect(await within(strip).findByText("2/2")).toBeInTheDocument();
    expect(strip).toHaveTextContent("LIVE");
    expect(strip).toHaveTextContent("41%");
    expect(strip).toHaveTextContent("2100 MB");
  });

  it("says RECONNECTING when the link drops, and does not claim the numbers are live", async () => {
    signedIn();
    render(<App />);
    await screen.findByRole("banner");
    (await liveSocket()).open();
    (await liveSocket()).send(snapshot());
    (await liveSocket()).end(1006);
    expect(await screen.findByText("RECONNECTING")).toBeInTheDocument();
  });

  it("announces a new CRITICAL incident once, through an assertive live region, and not a LOW one", async () => {
    signedIn();
    const { container } = render(<App />);
    await screen.findByRole("banner");
    const region = container.querySelector('[aria-live="assertive"]');
    expect(region).not.toBeNull();
    const notice = (severity: string) => ({
      kind: "incident",
      change: "new",
      incident: {
        incident_id: "x",
        display_ref: "INC-9",
        camera_id: "cam1",
        event_type: "intrusion",
        severity,
        status: "confirmed",
      },
    });
    (await liveSocket()).open();
    (await liveSocket()).send(notice("LOW"));
    expect(region).toHaveTextContent("");
    (await liveSocket()).send(notice("CRITICAL"));
    await waitFor(() => {
      expect(region).toHaveTextContent("New CRITICAL incident INC-9 on camera cam1");
    });
  });

  it("locks and signs out through the engine, not just on screen", async () => {
    const { calls } = signedIn();
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Lock console" }));
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    const urls = calls.filter((c) => c.init?.method === "POST").map((c) => c.url);
    expect(urls).toEqual(["/api/v1/auth/lock", "/api/v1/auth/logout"]);
  });

  it("returns to the gate when the engine ends the session or locks it", async () => {
    signedIn();
    render(<App />);
    await screen.findByRole("banner");
    (await liveSocket()).open();
    mockFetch({ "/api/v1/auth/status": authStatus({ authenticated: false, user: null }) });
    (await liveSocket()).end(4401);
    expect(
      await screen.findByRole("heading", { name: "Sign in to VIGIL-88" }, { timeout: 4000 }),
    ).toBeInTheDocument();
  });

  it("opens and closes the shortcut sheet, and is accessible", async () => {
    signedIn();
    const { container } = render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Shortcuts" }));
    expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await expectNoAccessibilityViolations(container);
  });

  it("has a skip link that moves focus to the main view", async () => {
    signedIn();
    render(<App />);
    await userEvent.click(await screen.findByRole("link", { name: "Skip to content" }));
    expect(document.getElementById("main")).toHaveFocus();
  });
});
