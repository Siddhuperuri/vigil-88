import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PendingBar } from "../components/PendingBar";
import { PendingProvider } from "../state/pending";
import { TelemetryProvider } from "../state/telemetry";
import { expectNoAccessibilityViolations } from "../test-utils";
import { bodyOf, installSocket, json, mockFetch, stubObjectUrls } from "../test-support";
import { Settings } from "./Settings";
import { Tuning } from "./Tuning";
import { Wizard } from "./Wizard";

const none = { pending: false, changes: [], can_restart: true };

function Wrap({ children }: { children: ReactNode }) {
  return (
    <TelemetryProvider onEnded={() => undefined}>
      <PendingProvider>{children}</PendingProvider>
    </TelemetryProvider>
  );
}

beforeEach(() => {
  installSocket();
});

// ------------------------------------------------------------------ settings: the alert policy

const policy = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  min_severity: "MODERATE",
  rules: [],
  quiet_hours: [],
  ...over,
});
const alertSettings = (over: Record<string, unknown> = {}) => ({
  policy: policy(over),
  channels: ["console", "webhook"],
  applied_live: true,
});
const rule = {
  name: "gate",
  camera: "yard",
  event_types: ["intrusion"],
  min_severity: "LOW",
  channels: ["webhook"],
};
const quiet = {
  name: "night",
  schedule: { start: "22:00", end: "06:00", utc_offset_minutes: 0, weekdays: [5, 6] },
  let_through_from: "HIGH",
};

describe("the settings screen", () => {
  const draw = () =>
    render(
      <Wrap>
        <Settings />
      </Wrap>,
    );

  it("shows the policy, links to the setup guide, and is accessible", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": alertSettings({ rules: [rule], quiet_hours: [quiet] }),
    });
    const { container } = draw();
    expect(await screen.findByRole("heading", { name: "Alerts" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Run the setup guide again" })).toHaveAttribute(
      "href",
      "#/setup",
    );
    expect(screen.getByText("Rule: gate")).toBeInTheDocument();
    expect(screen.getByText("Quiet hours: night")).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("says when the settings cannot be loaded", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": json({ detail: "busy" }, { status: 500 }),
    });
    draw();
    expect(await screen.findByText("Could not load the alert settings")).toBeInTheDocument();
  });

  it("adds a rule and a quiet window, and saves exactly what was entered", async () => {
    const { calls } = mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": (_u: string, init: RequestInit | undefined) =>
        init?.method === "PUT" ? alertSettings() : alertSettings(),
    });
    draw();
    const save = await screen.findByRole("button", { name: "Save alert settings" });
    expect(save).toBeDisabled(); // nothing to save yet
    await userEvent.click(screen.getByRole("button", { name: "Add a rule" }));
    await userEvent.click(screen.getByRole("button", { name: "Add quiet hours" }));
    await userEvent.selectOptions(
      screen.getAllByLabelText("Alert from severity")[0] as HTMLElement,
      "HIGH",
    );
    await userEvent.click(screen.getByLabelText("intrusion"));
    await userEvent.click(screen.getByLabelText("webhook"));
    await userEvent.click(screen.getByLabelText("Sat"));
    await userEvent.selectOptions(screen.getByLabelText("Still interrupt from severity"), "HIGH");
    await userEvent.click(save);
    expect(await screen.findByText("Saved and in force now.")).toBeInTheDocument();
    const put = calls.find((c) => c.init?.method === "PUT");
    const body = bodyOf(put) as {
      rules: Record<string, unknown>[];
      quiet_hours: Record<string, unknown>[];
    };
    expect(body.rules[0]).toMatchObject({
      name: "Rule 1",
      event_types: ["intrusion"],
      channels: ["webhook"],
    });
    expect(body.quiet_hours[0]).toMatchObject({ name: "Quiet 1", let_through_from: "HIGH" });
    expect(body.quiet_hours[0]?.["schedule"]).toMatchObject({ start: "22:00", weekdays: [5] });
  });

  it("edits and removes what is there", async () => {
    const { calls } = mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": alertSettings({ rules: [rule], quiet_hours: [quiet] }),
    });
    draw();
    await screen.findByText("Rule: gate");
    const name = screen.getAllByLabelText("Name")[0] as HTMLElement;
    await userEvent.clear(name);
    await userEvent.type(name, "front");
    await userEvent.clear(screen.getByLabelText("Camera"));
    await userEvent.selectOptions(
      screen.getAllByLabelText("Alert from severity")[1] as HTMLElement,
      "",
    );
    await userEvent.click(screen.getByLabelText("webhook")); // untick the only channel: back to "all"
    await userEvent.click(screen.getByLabelText("Mon"));
    await userEvent.click(screen.getByLabelText("Sat")); // untick
    fireInput(screen.getByLabelText("From (24 hour)"), "21:00");
    fireInput(screen.getByLabelText("Until"), "05:30");
    await userEvent.click(screen.getByRole("button", { name: "Save alert settings" }));
    await screen.findByText("Saved and in force now.");
    const body = bodyOf(calls.find((c) => c.init?.method === "PUT")) as {
      rules: Record<string, unknown>[];
      quiet_hours: Record<string, unknown>[];
    };
    expect(body.rules[0]).toMatchObject({
      name: "front",
      camera: null,
      min_severity: null,
      channels: null,
    });
    expect(body.quiet_hours[0]?.["schedule"]).toMatchObject({
      start: "21:00",
      end: "05:30",
      weekdays: [0, 6],
    });
    await userEvent.click(screen.getByRole("button", { name: "Remove rule front" }));
    await userEvent.click(screen.getByRole("button", { name: /Remove quiet hours/ }));
    expect(screen.getByText("No rules: the default applies to everything.")).toBeInTheDocument();
    expect(screen.getByText("No quiet hours.")).toBeInTheDocument();
  });

  it("shows the engine's reasons when a policy is refused and keeps the edits", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": (_u: string, init: RequestInit | undefined) =>
        init?.method === "PUT"
          ? json(
              {
                detail: {
                  message: "Those alert settings are not valid.",
                  issues: ["rules.0: bad"],
                },
              },
              { status: 422 },
            )
          : alertSettings(),
    });
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add a rule" }));
    await userEvent.click(screen.getByRole("button", { name: "Save alert settings" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Those alert settings are not valid.");
    expect(alert).toHaveTextContent("rules.0: bad");
    expect(screen.getByText("Rule: Rule 1")).toBeInTheDocument();
  });

  it("changes the default threshold and the on/off switch", async () => {
    const { calls } = mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": alertSettings(),
    });
    draw();
    await userEvent.click(await screen.findByLabelText("Send alerts"));
    await userEvent.selectOptions(screen.getByLabelText("Alert from severity"), "CRITICAL");
    await userEvent.click(screen.getByRole("button", { name: "Save alert settings" }));
    await screen.findByText("Saved and in force now.");
    expect(bodyOf(calls.find((c) => c.init?.method === "PUT"))).toMatchObject({
      enabled: false,
      min_severity: "CRITICAL",
    });
  });
});

function fireInput(element: HTMLElement, value: string): void {
  fireEvent.change(element, { target: { value } });
}

// ------------------------------------------------------------------ settings: moving them

const plan = (over: Record<string, unknown> = {}) => ({
  applied: false,
  changes: [
    { kind: "camera", target: "newcam", change: "add" },
    { kind: "zones", target: "bench", change: "replace" },
    { kind: "alerts", target: "alert policy", change: "same" },
  ],
  warnings: ["Camera newcam: the credential 'x' is not stored on this computer."],
  backup: null,
  restart_needed: true,
  ...over,
});

describe("moving settings between computers", () => {
  const settingsFile = () =>
    new File(['{"format":"vigil88-settings"}'], "s.json", { type: "application/json" });

  async function chooseFile() {
    await userEvent.upload(await screen.findByLabelText("Settings file"), settingsFile());
  }

  it("exports a file", async () => {
    const urls = stubObjectUrls();
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": alertSettings(),
      "/api/v1/settings/export": new Response("{}", { status: 200 }),
    });
    render(
      <Wrap>
        <Settings />
      </Wrap>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Export settings" }));
    await waitFor(() => {
      expect(urls.create).toHaveBeenCalled();
    });
  });

  it("reports a failed export", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": alertSettings(),
      "/api/v1/settings/export": new Response("no", { status: 403 }),
    });
    render(
      <Wrap>
        <Settings />
      </Wrap>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Export settings" }));
    expect(
      await screen.findByText(/Could not export: The engine answered 403/),
    ).toBeInTheDocument();
  });

  it("checks a file first, shows what it would do and what to look at, and writes nothing", async () => {
    const { calls } = mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": alertSettings(),
      "/api/v1/settings/import": plan(),
    });
    const { container } = render(
      <Wrap>
        <Settings />
      </Wrap>,
    );
    const check = await screen.findByRole("button", { name: "Check the file" });
    expect(check).toBeDisabled();
    await chooseFile();
    await userEvent.click(check);
    const section = await screen.findByRole("region", { name: "What the import would do" });
    expect(within(section).getByText("newcam")).toBeInTheDocument();
    expect(within(section).getByText("replaces what is here")).toBeInTheDocument();
    expect(within(section).getByText("already the same")).toBeInTheDocument();
    expect(screen.getByText(/credential 'x' is not stored/)).toBeInTheDocument();
    expect(screen.getByText(/take effect when monitoring restarts/)).toBeInTheDocument();
    expect(calls.filter((c) => c.url.includes("dry_run=true"))).toHaveLength(1);
    expect(calls.some((c) => c.url.includes("dry_run=false"))).toBe(false);
    await expectNoAccessibilityViolations(container);
  });

  it("applies only after the person confirms, and says what was backed up", async () => {
    const { calls } = mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": alertSettings(),
      "/api/v1/settings/import?dry_run=true": plan(),
      "/api/v1/settings/import?dry_run=false": plan({
        applied: true,
        backup: "backups/import-20261103T100000Z",
      }),
    });
    render(
      <Wrap>
        <Settings />
      </Wrap>,
    );
    await chooseFile();
    await userEvent.click(await screen.findByRole("button", { name: "Check the file" }));
    await userEvent.click(await screen.findByRole("button", { name: "Import these settings" }));
    expect(await screen.findByText(/2 settings were applied/)).toBeInTheDocument();
    expect(screen.getByText(/backups\/import-20261103T100000Z/)).toBeInTheDocument();
    expect(screen.getByText(/Restart monitoring from the bar above/)).toBeInTheDocument();
    expect(calls.filter((c) => c.url.includes("dry_run=false"))).toHaveLength(1);
  });

  it("says plainly when there is nothing to import", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": alertSettings(),
      "/api/v1/settings/import": plan({
        changes: [{ kind: "camera", target: "bench", change: "same" }],
        warnings: [],
        restart_needed: false,
      }),
    });
    render(
      <Wrap>
        <Settings />
      </Wrap>,
    );
    await chooseFile();
    await userEvent.click(await screen.findByRole("button", { name: "Check the file" }));
    expect(
      await screen.findByText(/every setting in the file is already the same/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import these settings" })).toBeDisabled();
  });

  it("explains a refusal, lists the problems, and says nothing was changed", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": alertSettings(),
      "/api/v1/settings/import": json(
        {
          detail: {
            message: "The file has problems and nothing was changed.",
            issues: ["cameras.0: bad"],
          },
        },
        { status: 422 },
      ),
    });
    render(
      <Wrap>
        <Settings />
      </Wrap>,
    );
    await chooseFile();
    await userEvent.click(await screen.findByRole("button", { name: "Check the file" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The file has problems");
    expect(alert).toHaveTextContent("cameras.0: bad");
    expect(alert).toHaveTextContent("Nothing was changed.");
  });

  it.each([
    [json({ detail: "too big" }, { status: 413 }), "too big"],
    [new Response("not json", { status: 500 }), "The engine answered 500."],
  ])("reads any other refusal sensibly", async (reply, words) => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/settings/alerts": alertSettings(),
      "/api/v1/settings/import": reply,
    });
    render(
      <Wrap>
        <Settings />
      </Wrap>,
    );
    await chooseFile();
    await userEvent.click(await screen.findByRole("button", { name: "Check the file" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(words);
  });

  it("reports an engine that does not answer", async () => {
    mockFetch({ "/api/v1/config/pending": none, "/api/v1/settings/alerts": alertSettings() });
    render(
      <Wrap>
        <Settings />
      </Wrap>,
    );
    await chooseFile();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("down"))),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Check the file" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("down");
  });
});

// ------------------------------------------------------------------ thresholds

const param = (name: string, over: Record<string, unknown> = {}) => ({
  name,
  kind: "number",
  default: 0.7,
  file_value: null,
  override: null,
  has_override: false,
  effective: 0.7,
  minimum: 0.5,
  maximum: 1,
  exclusive_minimum: true,
  exclusive_maximum: true,
  ...over,
});
const module_ = (id: string, params: unknown[], over: Record<string, unknown> = {}) => ({
  module_id: id,
  description: `The ${id} module.`,
  enabled: true,
  will_run: true,
  params,
  pending: false,
  ...over,
});
const camerasList = (editable = true) => [
  {
    config: { camera_id: "yard", name: "Yard", source: { kind: "synthetic" } },
    editable,
    state: "online",
    pending: null,
    credential_stored: null,
  },
];

describe("per-camera thresholds", () => {
  const draw = (
    role: "administrator" | "operator" = "administrator",
    camera: string | null = "yard",
  ) =>
    render(
      <Wrap>
        <Tuning route={{ view: "tuning", camera }} role={role} />
      </Wrap>,
    );

  it("asks for a camera first", async () => {
    mockFetch({ "/api/v1/config/pending": none, "/api/v1/config/cameras": camerasList() });
    draw("administrator", null);
    expect(await screen.findByText("Choose a camera to tune its detection.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Yard" })).toHaveAttribute("href", "#/tuning/yard");
  });

  it("tells a new installation to add a camera", async () => {
    mockFetch({ "/api/v1/config/pending": none, "/api/v1/config/cameras": [] });
    draw("administrator", null);
    expect(await screen.findByText("Add a camera first.")).toBeInTheDocument();
  });

  it("shows each module with its limits and what it is set to, and is accessible", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": camerasList(),
      "/api/v1/config/cameras/yard/tuning": [
        module_("intrusion", [
          param("base_signal", { file_value: 0.72 }),
          param("min_track_hits", {
            kind: "integer",
            default: 3,
            minimum: 1,
            maximum: null,
            exclusive_minimum: false,
            effective: 3,
          }),
          param("allow_interpolated", {
            kind: "boolean",
            default: false,
            minimum: null,
            maximum: null,
            effective: false,
          }),
          param("object_classes", {
            kind: "list",
            default: ["person"],
            minimum: null,
            maximum: null,
            effective: ["person"],
          }),
        ]),
        module_("fire", [], { will_run: false, enabled: false }),
        module_("person_down", [], { will_run: false, enabled: true, pending: true }),
      ],
    });
    const { container } = draw();
    expect(await screen.findByRole("heading", { name: "intrusion" })).toBeInTheDocument();
    expect(screen.getByText("RUNS ON THIS CAMERA")).toBeInTheDocument();
    expect(screen.getByText("NOT ENABLED")).toBeInTheDocument();
    expect(screen.getByText("CANNOT RUN HERE")).toBeInTheDocument();
    expect(screen.getByText(/above 0.5, below 1 · Shared value 0.72/)).toBeInTheDocument();
    expect(screen.getByText(/at least 1 · Shared value 3/)).toBeInTheDocument();
    expect(screen.getByText(/Saved, but monitoring still uses the old values/)).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("saves only what was changed, and shows the engine's list afterwards", async () => {
    const saved = [
      module_(
        "intrusion",
        [param("base_signal", { override: 0.72, has_override: true, effective: 0.72 })],
        { pending: true },
      ),
    ];
    const { calls } = mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": camerasList(),
      "/api/v1/config/cameras/yard/tuning/intrusion": saved,
      "/api/v1/config/cameras/yard/tuning": [
        module_("intrusion", [
          param("base_signal"),
          param("allow_interpolated", {
            kind: "boolean",
            default: false,
            minimum: null,
            maximum: null,
          }),
        ]),
      ],
    });
    draw();
    const save = await screen.findByRole("button", { name: "Save intrusion thresholds" });
    expect(save).toBeDisabled();
    await userEvent.type(screen.getByLabelText("base signal"), "0.72");
    await userEvent.selectOptions(screen.getByLabelText("allow interpolated"), "true");
    await userEvent.click(save);
    expect(
      await screen.findByText(/Saved, but monitoring still uses the old values/),
    ).toBeInTheDocument();
    const put = calls.find((c) => c.init?.method === "PUT");
    expect(bodyOf(put)).toEqual({ overrides: { base_signal: 0.72, allow_interpolated: true } });
  });

  it("reads list values as JSON and falls back to plain text", async () => {
    const { calls } = mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": camerasList(),
      "/api/v1/config/cameras/yard/tuning/intrusion": [module_("intrusion", [])],
      "/api/v1/config/cameras/yard/tuning": [
        module_("intrusion", [
          param("object_classes", {
            kind: "list",
            default: ["person"],
            minimum: null,
            maximum: null,
          }),
        ]),
      ],
    });
    draw();
    const box = await screen.findByLabelText("object classes");
    await userEvent.type(box, '[["person", "vehicle.car"]');
    await userEvent.click(screen.getByRole("button", { name: "Save intrusion thresholds" }));
    await waitFor(() => {
      expect(calls.some((c) => c.init?.method === "PUT")).toBe(true);
    });
    expect(bodyOf(calls.find((c) => c.init?.method === "PUT"))).toEqual({
      overrides: { object_classes: ["person", "vehicle.car"] },
    });
  });

  it("explains a refusal with the module's own words and keeps what was typed", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": camerasList(),
      "/api/v1/config/cameras/yard/tuning/intrusion": json(
        { detail: { message: "Those values are not valid.", issues: ["base_signal: too high"] } },
        { status: 422 },
      ),
      "/api/v1/config/cameras/yard/tuning": [module_("intrusion", [param("base_signal")])],
    });
    draw();
    await userEvent.type(await screen.findByLabelText("base signal"), "0.99");
    await userEvent.click(screen.getByRole("button", { name: "Save intrusion thresholds" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Those values are not valid.");
    expect(alert).toHaveTextContent("base_signal: too high");
    expect(screen.getByLabelText("base signal")).toHaveValue("0.99");
  });

  it("returns to the shared values", async () => {
    const { calls } = mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": camerasList(),
      "/api/v1/config/cameras/yard/tuning/intrusion": [
        module_("intrusion", [param("base_signal")]),
      ],
      "/api/v1/config/cameras/yard/tuning": [
        module_("intrusion", [
          param("base_signal", { override: 0.72, has_override: true, effective: 0.72 }),
        ]),
      ],
    });
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Use the shared values" }));
    await waitFor(() => {
      expect(bodyOf(calls.find((c) => c.init?.method === "PUT"))).toEqual({ overrides: {} });
    });
  });

  it("is read-only for a camera defined by hand, and for anyone who is not an administrator", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": camerasList(false),
      "/api/v1/config/cameras/yard/tuning": [module_("intrusion", [param("base_signal")])],
    });
    draw();
    expect(await screen.findByText(/defined outside the console/)).toBeInTheDocument();
    expect(screen.getByLabelText("base signal")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Use the shared values" })).toBeDisabled();
  });

  it("says an operator cannot change them", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": camerasList(),
      "/api/v1/config/cameras/yard/tuning": [module_("intrusion", [param("base_signal")])],
    });
    draw("operator");
    expect(await screen.findByText(/needs the administrator role/)).toBeInTheDocument();
  });

  it("says when the thresholds cannot be loaded", async () => {
    mockFetch({
      "/api/v1/config/pending": none,
      "/api/v1/config/cameras": camerasList(),
      "/api/v1/config/cameras/yard/tuning": json(
        { detail: "analysis stage is not running" },
        { status: 409 },
      ),
    });
    draw();
    expect(await screen.findByText("Could not load the thresholds")).toBeInTheDocument();
  });
});

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
