import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { expectNoAccessibilityViolations } from "../test-utils";
import { bodyOf, installSocket, json, mockFetch, stubObjectUrls } from "../test-support";
import { Wrap, none } from "./control-support";
import { Settings } from "./Settings";

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
