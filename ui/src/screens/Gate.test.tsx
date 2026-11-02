import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider } from "../state/auth";
import { authStatus, bodyOf, json, mockFetch, stubObjectUrls } from "../test-support";
import { Lock, Login, Setup } from "./Gate";

const wrap = (ui: React.ReactElement) => render(<AuthProvider>{ui}</AuthProvider>);
const post = (calls: { url: string; init: RequestInit | undefined }[], url: string) =>
  calls.find((c) => c.url === url && c.init?.method === "POST");

afterEach(() => {
  delete (window as unknown as { vigilShell?: unknown }).vigilShell;
});

describe("Login", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  it("posts the credentials with the custom header and then re-reads who is signed in", async () => {
    const { calls } = mockFetch({
      "/api/v1/auth/login": json({ user: authStatus().user, idle_timeout_s: 900, locked: false }),
      "/api/v1/auth/status": authStatus(),
    });
    wrap(<Login />);
    await userEvent.type(screen.getByLabelText("Username"), "alice");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => {
      expect(post(calls, "/api/v1/auth/login")).toBeDefined();
    });
    const sent = post(calls, "/api/v1/auth/login");
    expect(bodyOf(sent)).toEqual({
      username: "alice",
      password: "correct horse battery",
    });
    expect(sent?.init?.headers).toMatchObject({ "X-Vigil-Request": "1" });
    await waitFor(() => {
      expect(calls.some((c) => c.url === "/api/v1/auth/status")).toBe(true);
    });
  });

  it("shows the engine's generic failure and clears the password, so a typo is not left on screen", async () => {
    mockFetch({
      "/api/v1/auth/login": json(
        { detail: "That username and password do not match." },
        { status: 401 },
      ),
    });
    wrap(<Login />);
    await userEvent.type(screen.getByLabelText("Username"), "alice");
    await userEvent.type(screen.getByLabelText("Password"), "wrong wrong wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That username and password do not match.",
    );
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(screen.getByLabelText("Username")).toHaveValue("alice"); // the name is kept
  });

  it("shows the lockout wait counting down and keeps the button disabled while it lasts", async () => {
    mockFetch({
      "/api/v1/auth/login": json(
        { detail: "Too many attempts. Try again in 30 s." },
        { status: 429, headers: { "Retry-After": "30" } },
      ),
    });
    wrap(<Login />);
    await userEvent.type(screen.getByLabelText("Username"), "alice");
    await userEvent.type(screen.getByLabelText("Password"), "wrong wrong wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Try again in \d+ s/);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("warns about Caps Lock and can show and hide the password", async () => {
    mockFetch({});
    wrap(<Login />);
    const field = screen.getByLabelText("Password");
    expect(field).toHaveAttribute("type", "password");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(field).toHaveAttribute("type", "text");
    expect(screen.getByRole("button", { name: "Hide" })).toHaveAttribute("aria-pressed", "true");
    field.focus();
    await userEvent.keyboard("{CapsLock}");
    expect(
      document.getElementById(field.getAttribute("aria-describedby") ?? ""),
    ).toBeInTheDocument();
  });
});

describe("Lock", () => {
  it("unlocks with the password, and shows why when it is wrong", async () => {
    const { calls } = mockFetch({
      "/api/v1/auth/unlock": json({ detail: "That password does not match." }, { status: 401 }),
    });
    wrap(<Lock username="alice" />);
    await userEvent.type(screen.getByLabelText("Password"), "nope nope nope");
    await userEvent.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That password does not match.");
    expect(post(calls, "/api/v1/auth/unlock")).toBeDefined();
  });

  it("offers to sign out instead", async () => {
    const { calls } = mockFetch({
      "/api/v1/auth/logout": new Response(null, { status: 204 }),
      "/api/v1/auth/status": authStatus({ authenticated: false, user: null }),
    });
    wrap(<Lock username="alice" />);
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => {
      expect(post(calls, "/api/v1/auth/logout")).toBeDefined();
    });
  });
});

describe("Setup", () => {
  const codes = Array.from({ length: 10 }, (_, i) => `AAAA-BBBB-CCCC-DDD${String(i)}`);

  it("refuses mismatched passwords before asking the engine anything", async () => {
    const { calls } = mockFetch({});
    wrap(<Setup />);
    await userEvent.type(screen.getByLabelText("Setup token"), "tok");
    await userEvent.type(screen.getByLabelText("Administrator username"), "alice");
    await userEvent.type(
      screen.getAllByLabelText("Password")[0] as HTMLElement,
      "correct horse battery",
    );
    await userEvent.type(screen.getByLabelText("Repeat the password"), "correct horse batterY");
    await userEvent.click(screen.getByRole("button", { name: "Create administrator" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("do not match");
    expect(calls.filter((c) => c.init?.method === "POST")).toHaveLength(0);
  });

  it("shows the engine's reason when the password is refused", async () => {
    mockFetch({
      "/api/v1/auth/setup": json({ detail: "Use at least 12 characters." }, { status: 422 }),
    });
    wrap(<Setup />);
    await userEvent.type(screen.getByLabelText("Setup token"), "tok");
    await userEvent.type(screen.getByLabelText("Administrator username"), "alice");
    await userEvent.type(screen.getAllByLabelText("Password")[0] as HTMLElement, "short");
    await userEvent.type(screen.getByLabelText("Repeat the password"), "short");
    await userEvent.click(screen.getByRole("button", { name: "Create administrator" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Use at least 12 characters.");
  });

  it("shows the recovery codes once and will not continue until they are confirmed stored", async () => {
    const { calls } = mockFetch({
      "/api/v1/auth/setup": json(
        { user: authStatus().user, idle_timeout_s: 900, recovery_codes: codes },
        { status: 201 },
      ),
      "/api/v1/auth/status": authStatus(),
    });
    wrap(<Setup />);
    await userEvent.type(screen.getByLabelText("Setup token"), "tok-123");
    await userEvent.type(screen.getByLabelText("Administrator username"), "alice");
    await userEvent.type(
      screen.getAllByLabelText("Password")[0] as HTMLElement,
      "correct horse battery",
    );
    await userEvent.type(screen.getByLabelText("Repeat the password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Create administrator" }));

    expect(
      await screen.findByRole("heading", { name: "Save your recovery codes" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(10);
    expect(bodyOf(post(calls, "/api/v1/auth/setup"))).toMatchObject({
      token: "tok-123",
      username: "alice",
    });
    const next = screen.getByRole("button", { name: "Continue" });
    expect(next).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: /stored these codes/ }));
    expect(next).toBeEnabled();
    await userEvent.click(next);
    await waitFor(() => {
      expect(calls.some((c) => c.url === "/api/v1/auth/status")).toBe(true);
    });
  });

  it("takes the token from the desktop shell when there is one, and then does not ask for it", async () => {
    mockFetch({});
    (window as unknown as { vigilShell: unknown }).vigilShell = {
      setupToken: () => Promise.resolve("from-the-shell"),
    };
    wrap(<Setup />);
    await waitFor(() => {
      expect(screen.queryByLabelText("Setup token")).not.toBeInTheDocument();
    });
    expect(screen.queryByText("No setup token?")).not.toBeInTheDocument();
  });

  it("explains where the token comes from when run without the desktop app", () => {
    mockFetch({});
    wrap(<Setup />);
    expect(screen.getByText("No setup token?")).toBeInTheDocument();
  });

  it("can copy the codes and save them as a file", async () => {
    mockFetch({
      "/api/v1/auth/setup": json(
        { user: authStatus().user, idle_timeout_s: 900, recovery_codes: codes },
        { status: 201 },
      ),
    });
    const write = vi.fn(() => Promise.resolve());
    vi.stubGlobal("navigator", { clipboard: { writeText: write } });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const { create } = stubObjectUrls();
    wrap(<Setup />);
    await userEvent.type(screen.getByLabelText("Setup token"), "t");
    await userEvent.type(screen.getByLabelText("Administrator username"), "alice");
    await userEvent.type(
      screen.getAllByLabelText("Password")[0] as HTMLElement,
      "correct horse battery",
    );
    await userEvent.type(screen.getByLabelText("Repeat the password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Create administrator" }));
    await userEvent.click(await screen.findByRole("button", { name: "Copy" }));
    expect(write).toHaveBeenCalledWith(codes.join("\n"));
    await userEvent.click(screen.getByRole("button", { name: "Save as a file" }));
    expect(create).toHaveBeenCalled();
  });
});
