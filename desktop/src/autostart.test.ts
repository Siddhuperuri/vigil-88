import { describe, expect, it } from "vitest";
import { HIDDEN_FLAG, loginItem, startedHidden } from "./autostart";

describe("the login item", () => {
  it("registers a packaged app with only the hidden flag", () => {
    expect(
      loginItem({
        enabled: true,
        execPath: "C:/VIGIL-88/VIGIL-88.exe",
        appPath: "x",
        packaged: true,
      }),
    ).toEqual({ openAtLogin: true, path: "C:/VIGIL-88/VIGIL-88.exe", args: [HIDDEN_FLAG] });
  });

  it("registers a development run with the app folder first, so the start can work at all", () => {
    expect(
      loginItem({
        enabled: true,
        execPath: "electron.exe",
        appPath: "C:/repo/desktop",
        packaged: false,
      }).args,
    ).toEqual(["C:/repo/desktop", HIDDEN_FLAG]);
  });

  it("turning it off keeps the same path and arguments, so the same entry is the one removed", () => {
    const on = loginItem({ enabled: true, execPath: "a.exe", appPath: "d", packaged: true });
    const off = loginItem({ enabled: false, execPath: "a.exe", appPath: "d", packaged: true });
    expect(off).toEqual({ ...on, openAtLogin: false });
  });
});

describe("a start nobody made by hand", () => {
  it("is recognised by the flag, wherever it sits", () => {
    expect(startedHidden(["VIGIL-88.exe", HIDDEN_FLAG])).toBe(true);
    expect(startedHidden(["electron.exe", "app", HIDDEN_FLAG])).toBe(true);
  });

  it("is not guessed from anything else", () => {
    expect(startedHidden(["VIGIL-88.exe"])).toBe(false);
    expect(startedHidden(["VIGIL-88.exe", "--hide"])).toBe(false);
    expect(startedHidden([])).toBe(false);
  });
});
