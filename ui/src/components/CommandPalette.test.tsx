import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { expectNoAccessibilityViolations } from "../test-utils";
import { type Command, CommandPalette, matches } from "./CommandPalette";

const make = (run = vi.fn()): Command[] => [
  { id: "a", label: "Go to Incident desk", run },
  { id: "b", label: "Go to Cameras", keywords: "sources", run },
  { id: "c", label: "Sign out", keywords: "log out", run },
];

describe("matching", () => {
  it("needs every typed word to start a word of the label or keywords", () => {
    const [desk, cameras] = make() as [Command, Command, Command];
    expect(matches(desk, "inc des")).toBe(true);
    expect(matches(desk, "des inc")).toBe(true);
    expect(matches(desk, "cident")).toBe(false);
    expect(matches(cameras, "sour")).toBe(true);
    expect(matches(cameras, "")).toBe(true);
  });
});

describe("the palette", () => {
  it("opens on the input, filters as you type, and Enter runs the first match", async () => {
    const run = vi.fn();
    const onClose = vi.fn();
    const { container } = render(<CommandPalette commands={make(run)} open onClose={onClose} />);
    const box = await screen.findByLabelText("Go to or do");
    expect(box).toHaveFocus();
    expect(screen.getAllByRole("button")).toHaveLength(3);
    await userEvent.type(box, "log");
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(
      screen.getByText("1 match. Enter runs the first; Down moves into the list."),
    ).toBeInTheDocument();
    await userEvent.keyboard("{Enter}");
    expect(run).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalled();
    await expectNoAccessibilityViolations(container);
  });

  it("moves through the list with the arrow keys and back to the input", async () => {
    const run = vi.fn();
    render(<CommandPalette commands={make(run)} open onClose={() => undefined} />);
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByRole("button", { name: "Go to Incident desk" })).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}");
    expect(screen.getByRole("button", { name: "Sign out" })).toHaveFocus();
    await userEvent.keyboard("{ArrowUp}{ArrowUp}{ArrowUp}");
    expect(screen.getByLabelText("Go to or do")).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("says when nothing matches, and Enter then does nothing", async () => {
    const run = vi.fn();
    render(<CommandPalette commands={make(run)} open onClose={() => undefined} />);
    await userEvent.type(await screen.findByLabelText("Go to or do"), "zzz{Enter}");
    expect(screen.getByText("Nothing matches that.")).toBeInTheDocument();
    expect(run).not.toHaveBeenCalled();
  });

  it("tells its owner when the dialog closes, which is what Escape does in a real browser", () => {
    // jsdom does not turn Escape into a close, so the close event is raised by hand; the real
    // Escape is exercised in the Electron test.
    const onClose = vi.fn();
    render(<CommandPalette commands={make()} open onClose={onClose} />);
    fireEvent(
      screen.getByRole("dialog", { name: "Command palette", hidden: true }),
      new Event("close"),
    );
    expect(onClose).toHaveBeenCalled();
  });
});
