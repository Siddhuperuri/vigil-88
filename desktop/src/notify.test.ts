import { describe, expect, it } from "vitest";
import { describeAlert } from "./notify";
import { trayColour, trayIconPng } from "./tray-icon";

const alert = (overrides: Record<string, unknown> = {}) => ({
  severity: "CRITICAL",
  incidentId: "01ABCDEF",
  displayRef: "INC-20261102-AAAAAA",
  cameraId: "gate-north",
  eventType: "person_down",
  ...overrides,
});

describe("describeAlert", () => {
  it("builds the words itself from a fixed vocabulary and marks CRITICAL urgent", () => {
    expect(describeAlert(alert())).toEqual({
      title: "VIGIL-88: CRITICAL incident",
      body: "person down on gate-north (INC-20261102-AAAAAA)",
      urgent: true,
      incidentId: "01ABCDEF",
    });
    expect(describeAlert(alert({ severity: "HIGH" }))?.urgent).toBe(false);
  });

  it("stays quiet below HIGH, because an alert storm hides the one that matters", () => {
    for (const severity of ["INFO", "LOW", "MODERATE"]) {
      expect(describeAlert(alert({ severity }))).toBeNull();
    }
  });

  it("refuses anything malformed rather than guessing", () => {
    expect(describeAlert(null)).toBeNull();
    expect(describeAlert("CRITICAL")).toBeNull();
    for (const field of ["severity", "incidentId", "displayRef", "cameraId", "eventType"]) {
      expect(describeAlert(alert({ [field]: undefined }))).toBeNull();
      expect(describeAlert(alert({ [field]: 42 }))).toBeNull();
    }
  });

  it("refuses text a hostile page could use to inject markup, paths or a huge string", () => {
    expect(describeAlert(alert({ cameraId: "<img src=x onerror=alert(1)>" }))).toBeNull();
    expect(describeAlert(alert({ displayRef: "..\\..\\windows" }))).toBeNull();
    expect(describeAlert(alert({ incidentId: "01/../../etc" }))).toBeNull();
    expect(describeAlert(alert({ cameraId: "a".repeat(500) }))).toBeNull();
    expect(describeAlert(alert({ severity: "CRITICAL; rm" }))).toBeNull();
  });

  it("never carries an image or any field it was not asked for", () => {
    const out = describeAlert(alert({ snapshot: "data:image/png;base64,AAAA", secret: "x" }));
    expect(Object.keys(out ?? {}).sort()).toEqual(["body", "incidentId", "title", "urgent"]);
  });
});

describe("trayIconPng", () => {
  it("is a valid PNG of the requested size with the right chunks", () => {
    const png = trayIconPng(trayColour("ok"), 16);
    expect([...png.subarray(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    expect(png.subarray(12, 16).toString("ascii")).toBe("IHDR");
    expect(png.readUInt32BE(16)).toBe(16);
    expect(png.readUInt32BE(20)).toBe(16);
    expect(png.subarray(-8, -4).toString("ascii")).toBe("IEND");
    expect(png.includes(Buffer.from("IDAT"))).toBe(true);
  });

  it("uses a different colour for each state", () => {
    const tones = (["ok", "warning", "failed"] as const).map((t) =>
      trayIconPng(trayColour(t)).toString("hex"),
    );
    expect(new Set(tones).size).toBe(3);
  });
});
