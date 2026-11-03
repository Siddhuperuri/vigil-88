import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { expectNoAccessibilityViolations } from "../test-utils";
import { StartWithWindows } from "./StartWithWindows";

const install = (shell: object | undefined) => {
  Object.defineProperty(window, "vigilShell", { value: shell, configurable: true });
};

afterEach(() => {
  install(undefined);
});

describe("start with Windows", () => {
  it("says it belongs to the desktop app when the page has no shell", () => {
    install(undefined);
    render(<StartWithWindows />);
    expect(screen.getByText(/setting of the desktop app, not this page/)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("shows the current state, off by default, and turns it on", async () => {
    const setAutostart = vi.fn(() => Promise.resolve({ enabled: true }));
    install({ autostart: () => Promise.resolve({ enabled: false }), setAutostart });
    const { container } = render(<StartWithWindows />);
    const box = await screen.findByRole("checkbox", { name: /Start VIGIL-88 when I sign in/ });
    await waitFor(() => {
      expect(box).toBeEnabled();
    });
    expect(box).not.toBeChecked();
    await userEvent.click(box);
    await waitFor(() => {
      expect(box).toBeChecked();
    });
    expect(setAutostart).toHaveBeenCalledWith(true);
    await expectNoAccessibilityViolations(container);
  });

  it("does not claim a change the shell refused", async () => {
    install({
      autostart: () => Promise.resolve({ enabled: false }),
      setAutostart: () => Promise.resolve(null),
    });
    render(<StartWithWindows />);
    const box = await screen.findByRole("checkbox");
    await waitFor(() => {
      expect(box).toBeEnabled();
    });
    await userEvent.click(box);
    expect(await screen.findByText("The app would not change it.")).toBeInTheDocument();
    expect(box).not.toBeChecked();
  });

  it("disables the box and says so when the current setting cannot be read", async () => {
    install({ autostart: () => Promise.resolve(null), setAutostart: () => Promise.resolve(null) });
    render(<StartWithWindows />);
    expect(await screen.findByText(/The current setting is unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox")).toBeDisabled();
  });
});
