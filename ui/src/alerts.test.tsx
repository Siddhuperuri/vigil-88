import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { planAlert, playTone } from "./alerts";
import { Alerts } from "./components/Alerts";
import { TelemetryProvider } from "./state/telemetry";
import { installSocket, lastSocket, mockFetch } from "./test-support";
import { App } from "./App";
import { authStatus } from "./test-support";
import { screen } from "@testing-library/react";

const notice = (severity: string, change = "new") => ({
  kind: "incident",
  change,
  incident: {
    incident_id: "01A",
    display_ref: "INC-1",
    camera_id: "gate-north",
    event_type: "intrusion",
    severity,
    status: "confirmed",
  },
});

describe("planAlert", () => {
  it("sounds three high beeps for CRITICAL and two lower for HIGH", () => {
    expect(planAlert("new", "CRITICAL", true, true).tone).toEqual({ hz: 880, beeps: 3 });
    expect(planAlert("new", "HIGH", true, true).tone).toEqual({ hz: 660, beeps: 2 });
  });

  it("stays silent for anything quieter, and for an update to an incident already known", () => {
    for (const severity of ["INFO", "LOW", "MODERATE"]) {
      expect(planAlert("new", severity, false, true)).toEqual({ tone: null, notify: false });
    }
    expect(planAlert("updated", "CRITICAL", false, true)).toEqual({ tone: null, notify: false });
  });

  it("respects the sound switch without suppressing the notification", () => {
    expect(planAlert("new", "CRITICAL", false, false)).toEqual({ tone: null, notify: true });
  });

  it("does not toast a person who is already looking at the console", () => {
    expect(planAlert("new", "CRITICAL", true, true).notify).toBe(false);
  });
});

describe("playTone", () => {
  it("returns false when there is no audio, and when audio throws, rather than failing", () => {
    vi.stubGlobal("AudioContext", undefined);
    expect(playTone({ hz: 880, beeps: 1 })).toBe(false);
    vi.stubGlobal("AudioContext", function blocked() {
      throw new Error("blocked");
    });
    expect(playTone({ hz: 880, beeps: 1 })).toBe(false);
  });

  it("schedules one oscillator per beep", () => {
    const starts: number[] = [];
    vi.stubGlobal(
      "AudioContext",
      class {
        currentTime = 0;
        destination = {};
        createGain() {
          return { gain: { value: 0 }, connect: (n: unknown) => n };
        }
        createOscillator() {
          const node = {
            frequency: { value: 0 },
            connect: () => ({ connect: () => undefined }),
            start: (t: number) => starts.push(t),
            stop: () => undefined,
          };
          return node;
        }
        close() {
          return Promise.resolve();
        }
      },
    );
    expect(playTone({ hz: 660, beeps: 3 })).toBe(true);
    expect(starts).toEqual([0, 0.28, 0.56]);
  });
});

describe("Alerts", () => {
  beforeEach(() => {
    installSocket();
    mockFetch({});
  });

  it("asks the desktop shell for a notification when nobody is looking, with only what it needs", () => {
    const notify = vi.fn(() => Promise.resolve(true));
    (window as unknown as { vigilShell: unknown }).vigilShell = { notify };
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    render(
      <TelemetryProvider onEnded={() => undefined}>
        <Alerts soundOn={false} />
      </TelemetryProvider>,
    );
    lastSocket().open();
    lastSocket().send(notice("CRITICAL"));
    expect(notify).toHaveBeenCalledWith({
      severity: "CRITICAL",
      incidentId: "01A",
      displayRef: "INC-1",
      cameraId: "gate-north",
      eventType: "intrusion",
    });
    delete (window as unknown as { vigilShell?: unknown }).vigilShell;
  });

  it("does nothing for a quiet incident, and tolerates running in a plain browser", () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    render(
      <TelemetryProvider onEnded={() => undefined}>
        <Alerts soundOn />
      </TelemetryProvider>,
    );
    lastSocket().open();
    lastSocket().send(notice("LOW"));
    lastSocket().send(notice("CRITICAL")); // no shell bridge: must not throw
  });
});

describe("the sound switch", () => {
  it("defaults to on, flips, and is remembered", async () => {
    installSocket();
    window.localStorage.clear();
    mockFetch({ "/api/v1/auth/status": authStatus() });
    render(<App />);
    const toggle = await screen.findByRole("button", { name: /Alert sound: on/ });
    await userEvent.click(toggle);
    expect(screen.getByRole("button", { name: /Alert sound: off/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(window.localStorage.getItem("vigil.alertSound")).toBe("off");
  });

  it("stays on when storage is blocked, because a lost preference must not silence an alarm", async () => {
    installSocket();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    mockFetch({ "/api/v1/auth/status": authStatus() });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /Alert sound: on/ }));
    expect(screen.getByRole("button", { name: /Alert sound: off/ })).toBeInTheDocument();
  });
});
