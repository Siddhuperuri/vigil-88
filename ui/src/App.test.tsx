import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { expectNoAccessibilityViolations } from "./test-utils";

function answer(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

const status = (overrides: Record<string, unknown> = {}) =>
  answer({
    accounts_enabled: true,
    setup_required: false,
    authenticated: false,
    locked: false,
    user: null,
    ...overrides,
  });

describe("App", () => {
  it("says it is connecting while it waits, and announces it politely", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => undefined)),
    );
    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("Connecting to the engine");
  });

  it("asks for setup when the engine has no administrator yet", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(status({ setup_required: true }))),
    );
    render(<App />);
    expect(await screen.findByText(/First-run setup is required/)).toBeInTheDocument();
  });

  it("asks for sign-in when setup is done and nobody is signed in", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(status())),
    );
    render(<App />);
    expect(await screen.findByText(/Sign-in is required/)).toBeInTheDocument();
  });

  it("says plainly when the engine cannot be reached, and lets the person try again", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockRejectedValueOnce(new TypeError("network down"))
      .mockResolvedValueOnce(status());
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The engine is not responding");
    expect(alert).toHaveTextContent("starting or restarting");

    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/Sign-in is required/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows the engine's own reason when it answers with an error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(answer({ detail: "unexpected Host header" }, { status: 400 }))),
    );
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("unexpected Host header");
  });

  it("describes a failure that is not an Error without leaking what it was", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(answer("not an object", { status: 500 }))),
    );
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The engine is not responding");
  });

  it("does not update after it has been unmounted while a request was in flight", async () => {
    let resolve: (r: Response) => void = () => undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init?: RequestInit) =>
          new Promise<Response>((res, rej) => {
            resolve = res;
            init?.signal?.addEventListener("abort", () => {
              rej(new DOMException("aborted", "AbortError"));
            });
          }),
      ),
    );
    const { unmount } = render(<App />);
    unmount();
    resolve(status());
    await Promise.resolve();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows a status line for a signed-in person", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(status({ authenticated: true }))),
    );
    render(<App />);
    expect(await screen.findByText(/You are signed in/)).toBeInTheDocument();
  });

  it("sends the custom request header and same-origin credentials", async () => {
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(status()));
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/api/v1/auth/status");
    expect(init).toMatchObject({
      credentials: "same-origin",
      headers: { "X-Vigil-Request": "1" },
    });
  });

  it("has one h1, a main landmark and no WCAG 2.2 AA violations in any state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(status())),
    );
    const { container } = render(<App />);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("main")).toBeInTheDocument();
    await screen.findByText(/Sign-in is required/);
    await expectNoAccessibilityViolations(container);
  });

  it("is accessible when the engine has failed too", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("down"))),
    );
    const { container } = render(<App />);
    await screen.findByRole("alert");
    await expectNoAccessibilityViolations(container);
  });
});
