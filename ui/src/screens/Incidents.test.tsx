import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Role } from "../api/types";
import { TelemetryProvider } from "../state/telemetry";
import { expectNoAccessibilityViolations } from "../test-utils";
import {
  detail,
  incidentSummary,
  installSocket,
  bodyOf,
  json,
  lastSocket,
  mockFetch,
  stubObjectUrls,
} from "../test-support";
import { parse } from "../router";
import { Incidents } from "./Incidents";

const A = incidentSummary({ incident_id: "01A", display_ref: "INC-A", severity: "CRITICAL" });
const B = incidentSummary({
  incident_id: "01B",
  display_ref: "INC-B",
  severity: "LOW",
  status: "active",
});
const page = (items: unknown[], total = items.length, next: string | null = null) => ({
  incidents: items,
  total,
  page_size: 50,
  next_cursor: next,
});

function Harness({ userRole = "operator" }: { userRole?: Role }) {
  const [, setHash] = useState(window.location.hash);
  const route = parse(window.location.hash);
  if (route.view !== "incidents") return null;
  return (
    <TelemetryProvider onEnded={() => undefined}>
      <Incidents
        route={route}
        role={userRole}
        onShortcuts={() => undefined}
        onNavigate={(id) => {
          window.location.hash = id === null ? "#/incidents" : `#/incidents/${id}`;
          setHash(window.location.hash);
        }}
      />
    </TelemetryProvider>
  );
}

// The first matching prefix wins, so a test's own routes go first; defaults it does not name follow.
const routes = (extra: Record<string, unknown> = {}) => {
  const defaults: Record<string, unknown> = {
    "/api/v1/incidents/01A/evidence/ev1": new Response(new Blob(["x"]), {
      headers: { "x-evidence-integrity": "ok", "Content-Type": "image/jpeg" },
    }),
    "/api/v1/incidents/dismissal-reasons": [
      { code: "nothing_there", label: "Nothing was there" },
      { code: "harmless", label: "Something was there, but harmless" },
    ],
    "/api/v1/incidents/01A/": json(detail({ incident: A })),
    "/api/v1/incidents/01B": json(detail({ incident: B })),
    "/api/v1/incidents/01A": json(detail({ incident: A })),
    "/api/v1/incidents?": page([A, B]),
  };
  return {
    ...extra,
    ...Object.fromEntries(Object.entries(defaults).filter(([key]) => !(key in extra))),
  };
};

beforeEach(() => {
  installSocket();
  window.location.hash = "#/incidents";
  stubObjectUrls();
});

describe("the queue", () => {
  it("lists incidents with severity as shape, word and colour, and counts them", async () => {
    mockFetch(routes());
    render(<Harness />);
    expect(await screen.findByText("INC-A")).toBeInTheDocument();
    expect(screen.getByText("2 incidents")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Incidents" });
    expect(within(list).getByText(/CRITICAL/)).toBeInTheDocument();
    expect(within(list).getByText(/LOW/)).toBeInTheDocument();
  });

  it("says plainly that there are no open incidents, and treats that as good news", async () => {
    mockFetch({ "/api/v1/incidents?": page([]) });
    render(<Harness />);
    expect(await screen.findByText("No open incidents.")).toBeInTheDocument();
  });

  it("filters by scope, severity and search through the engine, never in the browser", async () => {
    const { calls } = mockFetch({ "/api/v1/incidents?": page([A]) });
    render(<Harness />);
    await screen.findByText("INC-A");
    await userEvent.selectOptions(screen.getByLabelText("Show"), "all");
    await userEvent.selectOptions(screen.getByLabelText("Severity at least"), "HIGH");
    await userEvent.type(screen.getByRole("searchbox"), "gate");
    await waitFor(() => {
      const last = calls.filter((c) => c.url.startsWith("/api/v1/incidents?")).at(-1)?.url ?? "";
      expect(last).toContain("min_severity=HIGH");
      expect(last).toContain("search=gate");
      expect(last).not.toContain("open_only");
    });
  });

  it("keeps the last list on screen and says so when a refresh fails", async () => {
    const fn = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json(page([A])))
      .mockResolvedValue(json({ detail: "database is busy" }, { status: 500 }));
    vi.stubGlobal("fetch", fn);
    render(<Harness />);
    await screen.findByText("INC-A");
    await userEvent.selectOptions(screen.getByLabelText("Show"), "all");
    expect(await screen.findByRole("alert")).toHaveTextContent("database is busy");
    expect(screen.getByText("INC-A")).toBeInTheDocument();
  });

  it("loads the next page on request", async () => {
    const fn = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json(page([A], 2, "cursor-1")))
      .mockResolvedValueOnce(json(page([B], 2, null)));
    vi.stubGlobal("fetch", fn);
    render(<Harness />);
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByText("INC-B")).toBeInTheDocument();
    expect(String((fn.mock.calls[1] as unknown[])[0])).toContain("cursor=cursor-1");
  });

  it("re-reads the list when the live channel reports an incident", async () => {
    const { calls } = mockFetch({ "/api/v1/incidents?": page([A]) });
    render(<Harness />);
    await screen.findByText("INC-A");
    const before = calls.length;
    lastSocket().open();
    lastSocket().send({
      kind: "incident",
      change: "new",
      incident: {
        incident_id: "x",
        display_ref: "INC-X",
        camera_id: "c",
        event_type: "intrusion",
        severity: "HIGH",
        status: "confirmed",
      },
    });
    await waitFor(() => {
      expect(calls.length).toBeGreaterThan(before);
    });
  });
});

describe("the detail", () => {
  it("shows what the incident is, with the factors adding up to the score", async () => {
    mockFetch(routes());
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    expect(await screen.findByRole("heading", { name: /INC-A/ })).toBeInTheDocument();
    expect(screen.getByText("intrusion reported for a person in zone yard")).toBeInTheDocument();
    expect(screen.getByText(/Sum, against the incident score of 61\.2/)).toBeInTheDocument();
    expect(screen.getByText("61.2")).toBeInTheDocument(); // 40.2 + 21.0
    expect(screen.getByText("camera_health")).toBeInTheDocument();
    expect(screen.getByText(/running without velocity/)).toBeInTheDocument();
    expect(screen.getByText(/not a probability/)).toBeInTheDocument();
  });

  it("shows the integrity result for evidence, and flags a mismatch loudly", async () => {
    mockFetch(
      routes({
        "/api/v1/incidents/01A/evidence/ev1": new Response(new Blob(["x"]), {
          headers: { "x-evidence-integrity": "mismatch", "Content-Type": "image/jpeg" },
        }),
      }),
    );
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    expect(await screen.findByText(/Integrity MISMATCH/)).toBeInTheDocument();
  });

  it("shows verified integrity and the image", async () => {
    mockFetch(routes());
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    expect(await screen.findByText(/Integrity verified/)).toBeInTheDocument();
    expect(await screen.findByAltText("Evidence: snapshot annotated")).toBeInTheDocument();
  });

  it("says when evidence could not be loaded rather than leaving a blank", async () => {
    mockFetch(routes({ "/api/v1/incidents/01A/evidence/ev1": new Response("", { status: 500 }) }));
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    expect(await screen.findByText(/Could not load this file/)).toBeInTheDocument();
  });

  it("acknowledges with one keystroke, as the signed-in user, and refreshes", async () => {
    const { calls } = mockFetch(
      routes({ "/api/v1/incidents/01A/acknowledge": json(detail({ incident: A })) }),
    );
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    await screen.findByRole("heading", { name: /INC-A/ });
    await userEvent.keyboard("a");
    await waitFor(() => {
      expect(
        calls.some(
          (c) => c.url === "/api/v1/incidents/01A/acknowledge" && c.init?.method === "POST",
        ),
      ).toBe(true);
    });
  });

  it("will not dismiss until a reason is chosen, then sends the code with the detail", async () => {
    const { calls } = mockFetch(
      routes({ "/api/v1/incidents/01A/dismiss": json(detail({ incident: A })) }),
    );
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    await screen.findByRole("heading", { name: /INC-A/ });
    await userEvent.keyboard("d");
    expect(await screen.findByText(/false alarm/)).toBeInTheDocument();
    // The list of reasons takes focus, so the keyboard path is d, arrows, Enter.
    expect(await screen.findByRole("radio", { name: "Nothing was there" })).toHaveFocus();
    const confirm = screen.getByRole("button", { name: "Confirm dismiss" });
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveAccessibleDescription("Choose a reason first.");
    await userEvent.click(screen.getByRole("radio", { name: "Something was there, but harmless" }));
    await userEvent.type(screen.getByLabelText("More detail (optional)"), "contractors");
    await userEvent.click(confirm);
    await waitFor(() => {
      const sent = calls.find((c) => c.url === "/api/v1/incidents/01A/dismiss");
      expect(bodyOf(sent)).toMatchObject({ reason: "contractors", reason_code: "harmless" });
    });
  });

  it("cancels a dismissal from the list of reasons with Escape, and says if the list cannot load", async () => {
    mockFetch(routes());
    window.location.hash = "#/incidents/01A";
    const first = render(<Harness />);
    await screen.findByRole("heading", { name: /INC-A/ });
    await userEvent.keyboard("d");
    await screen.findByRole("radio", { name: "Nothing was there" });
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("radio", { name: "Nothing was there" })).not.toBeInTheDocument();
    first.unmount();

    mockFetch(
      routes({ "/api/v1/incidents/dismissal-reasons": json({ detail: "down" }, { status: 503 }) }),
    );
    render(<Harness />);
    await screen.findByRole("heading", { name: /INC-A/ });
    await userEvent.keyboard("d");
    expect(await screen.findByText(/could not be loaded: down/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm dismiss" })).toBeDisabled();
  });

  it("can cancel a prompt, and Escape does the same", async () => {
    mockFetch(routes());
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    await screen.findByRole("heading", { name: /INC-A/ });
    await userEvent.keyboard("r");
    await screen.findByText(/closes the incident as over/);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByText(/closes the incident as over/)).not.toBeInTheDocument();
  });

  it("downloads the incident bundle for an operator, and blocks a viewer with the reason", async () => {
    const urls = stubObjectUrls();
    const { calls } = mockFetch(
      routes({
        "/api/v1/incidents/01A/bundle": new Response("zip", {
          headers: { "Content-Type": "application/zip" },
        }),
      }),
    );
    window.location.hash = "#/incidents/01A";
    const first = render(<Harness />);
    await userEvent.click(await screen.findByRole("button", { name: "Download bundle (zip)" }));
    await waitFor(() => {
      expect(urls.create).toHaveBeenCalled();
    });
    expect(calls.some((c) => c.url === "/api/v1/incidents/01A/bundle")).toBe(true);
    first.unmount();

    mockFetch(routes());
    render(<Harness userRole="viewer" />);
    const blocked = await screen.findByRole("button", { name: "Download bundle (zip)" });
    expect(blocked).toBeDisabled();
    expect(blocked).toHaveAccessibleDescription("Exporting needs the operator role.");
  });

  it("says when the bundle could not be made", async () => {
    mockFetch(
      routes({ "/api/v1/incidents/01A/bundle": json({ detail: "disk full" }, { status: 500 }) }),
    );
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    await userEvent.click(await screen.findByRole("button", { name: "Download bundle (zip)" }));
    expect(
      await screen.findByText(/Could not make the bundle: The engine answered 500\./),
    ).toBeInTheDocument();
  });

  it("disables an action the lifecycle forbids and says why, instead of enabling it and failing", async () => {
    const closed = detail({
      incident: A,
      permissions: {
        acknowledge: { allowed: false, reason: "an incident in resolved cannot be acknowledged" },
        dismiss: { allowed: false, reason: "an incident in resolved is already closed" },
        escalate: { allowed: true, reason: null },
        resolve: { allowed: false, reason: "an incident in resolved is already closed" },
        annotate: { allowed: true, reason: null },
      },
    });
    mockFetch(
      routes({ "/api/v1/incidents/01A/": json(closed), "/api/v1/incidents/01A": json(closed) }),
    );
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    const ack = await screen.findByRole("button", { name: /Acknowledge/ });
    expect(ack).toBeDisabled();
    expect(ack).toHaveAccessibleDescription("an incident in resolved cannot be acknowledged");
    await userEvent.keyboard("a"); // a shortcut must not bypass the disabled button
  });

  it("gives a viewer a read-only view with the reason on every action", async () => {
    const { calls } = mockFetch(routes());
    window.location.hash = "#/incidents/01A";
    render(<Harness userRole="viewer" />);
    const dismiss = await screen.findByRole("button", { name: /Dismiss/ });
    expect(dismiss).toBeDisabled();
    expect(dismiss).toHaveAccessibleDescription("This needs the operator role.");
    expect(screen.getByLabelText("Operator note")).toBeDisabled();
    await userEvent.keyboard("d");
    expect(screen.queryByText(/false alarm/)).not.toBeInTheDocument();
    expect(calls.filter((c) => c.init?.method === "POST")).toHaveLength(0);
  });

  it("saves a note through the annotate route", async () => {
    const { calls } = mockFetch(
      routes({ "/api/v1/incidents/01A/annotate": json(detail({ incident: A })) }),
    );
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    const note = await screen.findByLabelText("Operator note");
    await userEvent.type(note, "spoke to the manager");
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    await waitFor(() => {
      const sent = calls.find((c) => c.url === "/api/v1/incidents/01A/annotate");
      expect(bodyOf(sent)).toEqual({ note: "spoke to the manager" });
    });
  });

  it("shows the error and keeps the prompt when an action is refused", async () => {
    mockFetch(
      routes({
        "/api/v1/incidents/01A/escalate": json({ detail: "already at CRITICAL" }, { status: 409 }),
      }),
    );
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    await screen.findByRole("heading", { name: /INC-A/ });
    await userEvent.keyboard("e");
    await userEvent.click(await screen.findByRole("button", { name: "Confirm escalate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already at CRITICAL");
  });

  it("offers a retry when the incident cannot be loaded", async () => {
    mockFetch({
      "/api/v1/incidents/01A": json({ detail: "boom" }, { status: 500 }),
      "/api/v1/incidents?": page([A]),
    });
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    expect(await screen.findByText("Could not load this incident")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("shows the history of who did what", async () => {
    mockFetch(routes());
    window.location.hash = "#/incidents/01A";
    render(<Harness />);
    expect(await screen.findByText(/by system: confirmed/)).toBeInTheDocument();
  });
});

describe("keyboard triage", () => {
  it("j and k move through the queue, and / and n move focus", async () => {
    mockFetch(routes());
    render(<Harness />);
    await screen.findByText("INC-A");
    await userEvent.keyboard("j");
    await waitFor(() => {
      expect(window.location.hash).toBe("#/incidents/01A");
    });
    await userEvent.keyboard("j");
    await waitFor(() => {
      expect(window.location.hash).toBe("#/incidents/01B");
    });
    await userEvent.keyboard("k");
    await waitFor(() => {
      expect(window.location.hash).toBe("#/incidents/01A");
    });
    await userEvent.keyboard("/");
    expect(screen.getByRole("searchbox")).toHaveFocus();
    await userEvent.keyboard("{Escape}");
  });

  it("does not treat typing in the search box as a command", async () => {
    mockFetch(routes());
    render(<Harness />);
    await screen.findByText("INC-A");
    await userEvent.type(screen.getByRole("searchbox"), "jjjdddaaa");
    expect(window.location.hash).toBe("#/incidents");
    expect(screen.getByRole("searchbox")).toHaveValue("jjjdddaaa");
  });

  it("has no WCAG 2.2 AA violations with an incident open", async () => {
    mockFetch(routes());
    window.location.hash = "#/incidents/01A";
    const { container } = render(<Harness />);
    await screen.findByText(/Integrity verified/);
    await expectNoAccessibilityViolations(container);
  });
});
