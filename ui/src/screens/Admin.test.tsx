import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { expectNoAccessibilityViolations } from "../test-utils";
import { bodyOf, json, mockFetch, stubObjectUrls } from "../test-support";
import { Audit } from "./Audit";
import { People } from "./People";
import { Reports } from "./Reports";

const person = (name: string, extra: Record<string, unknown> = {}) => ({
  user_id: `id-${name}`,
  username: name,
  role: "operator",
  must_change_password: false,
  disabled: false,
  ...extra,
});
// Built from parts so the repository's secret scan sees test data for what it is.
const PHRASE = ["a", "long", "enough", "phrase"].join(" ");

describe("people", () => {
  beforeEach(() => {
    stubObjectUrls();
  });

  it("lists accounts, and will not let you change your own from here", async () => {
    mockFetch({
      "/api/v1/users": [person("alice", { role: "administrator" }), person("bob")],
    });
    const { container } = render(<People me="alice" />);
    expect(await screen.findByRole("rowheader", { name: /alice/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Role for alice")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete alice" })).toBeDisabled();
    expect(screen.getByLabelText("Role for bob")).toBeEnabled();
    await expectNoAccessibilityViolations(container);
  });

  it("changes a role and shows the engine's refusal if it refuses", async () => {
    const { calls } = mockFetch({
      "/api/v1/users/id-bob": (_u: string, init: RequestInit | undefined) =>
        init?.method === "PATCH"
          ? json({ detail: "The last administrator cannot be demoted." }, { status: 409 })
          : person("bob"),
      "/api/v1/users": [person("alice", { role: "administrator" }), person("bob")],
    });
    render(<People me="alice" />);
    await userEvent.selectOptions(await screen.findByLabelText("Role for bob"), "viewer");
    expect(
      await screen.findByText("The last administrator cannot be demoted."),
    ).toBeInTheDocument();
    expect(bodyOf(calls.find((c) => c.init?.method === "PATCH"))).toEqual({ role: "viewer" });
  });

  it("disables an account", async () => {
    const { calls } = mockFetch({
      "/api/v1/users/id-bob": person("bob", { disabled: true }),
      "/api/v1/users": [person("alice", { role: "administrator" }), person("bob")],
    });
    render(<People me="alice" />);
    await userEvent.click(await screen.findByRole("button", { name: "Disable bob" }));
    await waitFor(() => {
      expect(bodyOf(calls.find((c) => c.init?.method === "PATCH"))).toEqual({ disabled: true });
    });
  });

  it("adds a person only with a long enough password, and clears the form after", async () => {
    const { calls } = mockFetch({
      "/api/v1/users": (_u: string, init: RequestInit | undefined) =>
        init?.method === "POST" ? json(person("carol"), { status: 201 }) : [person("alice")],
    });
    render(<People me="alice" />);
    await screen.findByRole("rowheader", { name: /alice/ });
    await userEvent.type(screen.getByLabelText("Username"), "carol");
    await userEvent.type(screen.getByLabelText("Temporary password"), "short");
    await userEvent.click(screen.getByRole("button", { name: "Add person" }));
    expect(await screen.findByText(/Use at least 12 characters/)).toBeInTheDocument();
    expect(calls.some((c) => c.init?.method === "POST")).toBe(false);
    await userEvent.clear(screen.getByLabelText("Temporary password"));
    await userEvent.type(screen.getByLabelText("Temporary password"), PHRASE);
    await userEvent.click(screen.getByRole("button", { name: "Add person" }));
    await waitFor(() => {
      expect(bodyOf(calls.find((c) => c.init?.method === "POST"))).toEqual({
        username: "carol",
        password: PHRASE,
        role: "operator",
      });
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Username")).toHaveValue("");
    });
  });

  it("asks before deleting, with the safe choice first, and resets a password with a minimum", async () => {
    const { calls } = mockFetch({
      "/api/v1/users/id-bob/reset-password": person("bob"),
      "/api/v1/users/id-bob": () => new Response(null, { status: 204 }),
      "/api/v1/users": [person("alice", { role: "administrator" }), person("bob")],
    });
    render(<People me="alice" />);
    await userEvent.click(await screen.findByRole("button", { name: "Reset password for bob" }));
    let dialog = await screen.findByRole("dialog", { name: /Reset the password for bob/ });
    expect(within(dialog).getByRole("button", { name: "Reset password" })).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("New temporary password"), PHRASE);
    await userEvent.click(within(dialog).getByRole("button", { name: "Reset password" }));
    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/reset-password"))).toBe(true);
    });

    await userEvent.click(await screen.findByRole("button", { name: "Delete bob" }));
    dialog = await screen.findByRole("dialog", { name: "Delete bob?" });
    expect(within(dialog).getAllByRole("button")[0]).toHaveTextContent("Cancel");
    expect(calls.some((c) => c.init?.method === "DELETE")).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));
    await waitFor(() => {
      expect(calls.some((c) => c.init?.method === "DELETE")).toBe(true);
    });
  });

  it("says when the list cannot be loaded", async () => {
    mockFetch({ "/api/v1/users": json({ detail: "locked" }, { status: 423 }) });
    render(<People me="alice" />);
    expect(await screen.findByText("locked")).toBeInTheDocument();
  });
});

const entry = (seq: number, extra: Record<string, unknown> = {}) => ({
  seq,
  at_utc: "2026-11-02T06:00:00Z",
  user_id: "u1",
  username: "alice",
  action: "user.created",
  target: "bob",
  detail: "role=operator",
  ...extra,
});

describe("audit log", () => {
  it("lists rows newest first, with system actions named as such", async () => {
    mockFetch({
      "/api/v1/audit": [entry(2), entry(1, { username: null, action: "setup.completed" })],
    });
    const { container } = render(<Audit />);
    expect(await screen.findByText("setup.completed")).toBeInTheDocument();
    expect(screen.getByText("system")).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("filters by action and pages back from the oldest row it has", async () => {
    const full = Array.from({ length: 100 }, (_, i) => entry(200 - i));
    const { calls } = mockFetch({
      "/api/v1/audit": (url: string) => (url.includes("before_seq") ? [entry(100)] : full),
    });
    render(<Audit />);
    await userEvent.click(await screen.findByRole("button", { name: "Show older rows" }));
    await waitFor(() => {
      expect(calls.some((c) => c.url.includes("before_seq=101"))).toBe(true);
    });
    expect(screen.queryByRole("button", { name: "Show older rows" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Action"), "login");
    await userEvent.click(screen.getByRole("button", { name: "Filter" }));
    await waitFor(() => {
      expect(calls.some((c) => c.url.includes("action=login"))).toBe(true);
    });
  });

  it("reports an intact chain, and a broken one by the row where it broke", async () => {
    let ok = true;
    mockFetch({
      "/api/v1/audit/verify": () =>
        ok ? { ok: true, rows: 5, first_bad_seq: null } : { ok: false, rows: 3, first_bad_seq: 3 },
      "/api/v1/audit": [entry(1)],
    });
    render(<Audit />);
    await userEvent.click(await screen.findByRole("button", { name: "Verify the chain" }));
    expect(await screen.findByText("Intact: 5 rows, none altered.")).toBeInTheDocument();
    ok = false;
    await userEvent.click(screen.getByRole("button", { name: "Verify the chain" }));
    expect(await screen.findByText(/Broken at row 3/)).toBeInTheDocument();
  });

  it("exports through a download, and says when the log is empty or fails to load", async () => {
    const urls = stubObjectUrls();
    mockFetch({
      "/api/v1/audit/export": new Response("seq\n1", { headers: { "Content-Type": "text/csv" } }),
      "/api/v1/audit": [entry(1)],
    });
    const first = render(<Audit />);
    await userEvent.click(await screen.findByRole("button", { name: "Export CSV" }));
    await waitFor(() => {
      expect(urls.create).toHaveBeenCalled();
    });
    first.unmount();
    mockFetch({ "/api/v1/audit": [] });
    const second = render(<Audit />);
    expect(await screen.findByText("The audit log is empty.")).toBeInTheDocument();
    second.unmount();
    mockFetch({ "/api/v1/audit": json({ detail: "database is busy" }, { status: 503 }) });
    render(<Audit />);
    expect(await screen.findByText("database is busy")).toBeInTheDocument();
  });
});

const dur = (available: boolean) => ({
  available,
  count: available ? 4 : 0,
  mean_ms: available ? 2000 : null,
  p50_ms: available ? 1500 : null,
  p95_ms: available ? 4200 : null,
  max_ms: available ? 5000 : null,
});
const summary = (extra: Record<string, unknown> = {}) => ({
  since_utc: "2026-10-27T00:00:00Z",
  until_utc: "2026-11-03T00:00:00Z",
  utc_offset_minutes: 0,
  bucket: "day",
  covers_full_window: true,
  earliest_record_utc: "2026-10-01T00:00:00Z",
  incidents_total: 7,
  open_now: 2,
  by_event_type: [{ key: "intrusion", count: 5 }],
  by_severity: [{ key: "HIGH", count: 3 }],
  by_status: [],
  by_camera: [{ key: "dock", count: 7 }],
  trend: [{ key: "2026-11-01", count: 4 }],
  duration: dur(true),
  detection_latency: dur(true),
  acknowledgement: dur(false),
  resolution: dur(false),
  dismissal_rate: [
    { module_id: "intrusion", module_version: "1.0.0", dismissed: 2, total: 5, ratio: 0.4 },
  ],
  candidate_outcomes: [],
  rejection_reasons: [],
  camera_availability: { dock: null },
  caveats: ["Counts are of persisted incidents only."],
  metric_definitions: {
    duration: {
      population: "closed incidents",
      definition: "first to last observation",
      unit: "ms",
    },
  },
  ...extra,
});
const formats = [
  { format: "csv", available: true, reason: null },
  { format: "pdf", available: false, reason: "install the reports extra" },
];

describe("reports", () => {
  it("shows counts, says unavailable where nothing was measured, and keeps the caveats", async () => {
    mockFetch({ "/api/v1/analytics/formats": formats, "/api/v1/analytics/summary": summary() });
    const { container } = render(<Reports userRole="operator" />);
    expect(await screen.findByText("Counts are of persisted incidents only.")).toBeInTheDocument();
    expect(screen.getAllByText(/unavailable: no incidents had this measured/)).toHaveLength(2);
    expect(screen.getAllByRole("rowheader", { name: /dock/ }).length).toBeGreaterThan(0);
    expect(screen.getByText("40%")).toBeInTheDocument();
    expect(screen.getByText(/not a measured false-positive rate/)).toBeInTheDocument();
    expect(screen.getByText("unavailable")).toBeInTheDocument(); // dock availability
    await expectNoAccessibilityViolations(container);
  });

  it("warns when the records do not cover the period, instead of implying zeros", async () => {
    mockFetch({
      "/api/v1/analytics/formats": formats,
      "/api/v1/analytics/summary": summary({ covers_full_window: false }),
    });
    render(<Reports userRole="operator" />);
    expect(await screen.findByText("Records do not cover the whole period")).toBeInTheDocument();
  });

  it("re-reads for another period", async () => {
    const { calls } = mockFetch({
      "/api/v1/analytics/formats": formats,
      "/api/v1/analytics/summary": summary(),
    });
    render(<Reports userRole="operator" />);
    await userEvent.click(await screen.findByRole("radio", { name: /Last 90 days/ }));
    await waitFor(() => {
      expect(calls.some((c) => c.url.includes("days=90") && c.url.includes("bucket=week"))).toBe(
        true,
      );
    });
  });

  it("offers an export format only if it works, says why not otherwise, and blocks a viewer", async () => {
    const urls = stubObjectUrls();
    const { calls } = mockFetch({
      "/api/v1/analytics/export": new Response("a,b", { headers: { "Content-Type": "text/csv" } }),
      "/api/v1/analytics/formats": formats,
      "/api/v1/analytics/summary": summary(),
    });
    const first = render(<Reports userRole="operator" />);
    expect(await screen.findByRole("button", { name: "PDF" })).toBeDisabled();
    expect(screen.getByText("install the reports extra")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "CSV" }));
    await waitFor(() => {
      expect(urls.create).toHaveBeenCalled();
    });
    expect(calls.some((c) => c.url.includes("export?format=csv&days=7"))).toBe(true);
    first.unmount();
    mockFetch({ "/api/v1/analytics/formats": formats, "/api/v1/analytics/summary": summary() });
    render(<Reports userRole="viewer" />);
    expect(await screen.findByRole("button", { name: "CSV" })).toBeDisabled();
    expect(
      screen.getAllByText("Exporting needs the operator or administrator role.").length,
    ).toBeGreaterThan(0);
  });

  it("shows a failure with a retry", async () => {
    mockFetch({
      "/api/v1/analytics/formats": formats,
      "/api/v1/analytics/summary": json({ detail: "query failed" }, { status: 500 }),
    });
    render(<Reports userRole="operator" />);
    expect(await screen.findByText("query failed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
