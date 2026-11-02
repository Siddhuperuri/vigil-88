import { describe, expect, it } from "vitest";
import { bytes, readout, when } from "./components/bits";
import { href, parse } from "./router";
import { parseMessage, reconnectDelayMs } from "./api/telemetry";
import { asSeverity } from "./api/types";
import { snapshot } from "./test-support";

describe("router", () => {
  it("parses every view, and a bare or unknown hash lands on the wall", () => {
    expect(parse("")).toEqual({ view: "wall", camera: null });
    expect(parse("#/wall/cam1")).toEqual({ view: "wall", camera: "cam1" });
    expect(parse("#/incidents")).toEqual({ view: "incidents", id: null });
    expect(parse("#/incidents/01ABC")).toEqual({ view: "incidents", id: "01ABC" });
    expect(parse("#/system")).toEqual({ view: "system" });
    expect(parse("#/nonsense")).toEqual({ view: "wall", camera: null });
  });

  it("round-trips through href, escaping what needs it", () => {
    for (const hash of [
      "#/wall",
      "#/wall/cam%201",
      "#/incidents",
      "#/incidents/01ABC",
      "#/system",
    ]) {
      expect(href(parse(hash))).toBe(hash);
    }
  });
});

describe("telemetry messages", () => {
  it("accepts a snapshot and an incident notice", () => {
    expect(parseMessage(JSON.stringify(snapshot()))?.kind).toBe("snapshot");
    const notice = {
      kind: "incident",
      change: "new",
      incident: {
        incident_id: "a",
        display_ref: "INC-1",
        camera_id: "c",
        event_type: "intrusion",
        severity: "HIGH",
        status: "confirmed",
      },
    };
    expect(parseMessage(JSON.stringify(notice))?.kind).toBe("incident");
  });

  it("drops what is not JSON, not a known kind, or the wrong shape, instead of guessing", () => {
    expect(parseMessage("not json")).toBeNull();
    expect(parseMessage(JSON.stringify({ kind: "mystery" }))).toBeNull();
    expect(parseMessage(JSON.stringify({ ...snapshot(), cameras: "none" }))).toBeNull();
    expect(
      parseMessage(JSON.stringify({ ...snapshot(), health: { level: "great", issues: [] } })),
    ).toBeNull();
  });

  it("backs off 1, 2, 4 seconds and stops at 15", () => {
    expect([1, 2, 3, 4, 5, 6, 9].map(reconnectDelayMs)).toEqual([
      1000, 2000, 4000, 8000, 15000, 15000, 15000,
    ]);
    expect(reconnectDelayMs(0)).toBe(1000);
  });
});

describe("formatting never turns an unknown into a zero", () => {
  it("reads null and undefined as unavailable", () => {
    expect(readout(null, "%")).toBe("unavailable");
    expect(readout(undefined, "%")).toBe("unavailable");
    expect(readout(0, "%")).toBe("0%");
    expect(readout(41.26, " fps", 1)).toBe("41.3 fps");
    expect(when(null)).toBe("unavailable");
    expect(when("garbage")).toBe("garbage");
    expect(bytes(null)).toBe("unavailable");
  });

  it("scales bytes", () => {
    expect(bytes(512)).toBe("512 B");
    expect(bytes(2048)).toBe("2.0 KB");
    expect(bytes(5 * 1024 ** 3)).toBe("5.0 GB");
  });

  it("treats an unknown severity word as INFO rather than crashing", () => {
    expect(asSeverity("critical")).toBe("CRITICAL");
    expect(asSeverity("???")).toBe("INFO");
  });
});
