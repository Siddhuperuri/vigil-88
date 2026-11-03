import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { expectNoAccessibilityViolations } from "../test-utils";
import { incidentSummary, json, mockFetch } from "../test-support";
import { Activity } from "./Activity";

const timeline = {
  since_utc: "2026-11-02T00:00:00Z",
  until_utc: "2026-11-03T00:00:00Z",
  incidents: [
    incidentSummary({
      incident_id: "01A",
      display_ref: "INC-A",
      first_observed_utc: "2026-11-02T10:00:00Z",
    }),
  ],
  health_events: [
    {
      camera_id: "cam1",
      state: "reconnecting",
      at_utc: "2026-11-02T10:00:02Z",
      detail: "no frames for 6 s",
    },
    { camera_id: "cam1", state: "online", at_utc: "2026-11-02T09:00:00Z", detail: null },
  ],
};
const candidate = (id: string, extra: Record<string, unknown> = {}) => ({
  candidate_id: id,
  camera_id: "cam1",
  module_id: "intrusion",
  event_type: "intrusion",
  subject: "track:1",
  outcome: "rejected",
  rejection_reason: "scene unstable",
  confidence: 0.62,
  log_odds: 0.5,
  support_ratio: 0.4,
  observations: 4,
  promoted_utc: "2026-11-02T10:00:00Z",
  incident_id: null,
  ...extra,
});

describe("the timeline", () => {
  it("puts incidents and camera state changes on one list, newest first, each with its words", async () => {
    mockFetch({ "/api/v1/events/timeline": timeline });
    const { container } = render(<Activity route={{ view: "activity", tab: "timeline" }} />);
    const rows = await screen.findAllByRole("row");
    // header, then reconnecting (10:00:02), the incident (10:00:00), online (09:00)
    expect(within(rows[1] as HTMLElement).getByText(/RECONNECTING/)).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByRole("link", { name: /INC-A/ })).toHaveAttribute(
      "href",
      "#/incidents/01A",
    );
    expect(within(rows[3] as HTMLElement).getByText(/ONLINE/)).toBeInTheDocument();
    expect(screen.getByText("no frames for 6 s")).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("re-reads for another period, says when nothing happened, and when it cannot load", async () => {
    const { calls } = mockFetch({
      "/api/v1/events/timeline": { ...timeline, incidents: [], health_events: [] },
    });
    const first = render(<Activity route={{ view: "activity", tab: "timeline" }} />);
    expect(await screen.findByText("Nothing was recorded in this period.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: "Last 7 days" }));
    await waitFor(() => {
      expect(calls.some((c) => c.url.includes("hours=168"))).toBe(true);
    });
    first.unmount();
    mockFetch({ "/api/v1/events/timeline": json({ detail: "db busy" }, { status: 503 }) });
    render(<Activity route={{ view: "activity", tab: "timeline" }} />);
    expect(await screen.findByText("db busy")).toBeInTheDocument();
  });
});

describe("candidates and rejections", () => {
  it("lists what was proposed and why it was refused, and says a candidate is not an incident", async () => {
    mockFetch({
      "/api/v1/events/candidates/reasons": [{ reason: "scene unstable", count: 7 }],
      "/api/v1/events/candidates": [
        candidate("c1"),
        candidate("c2", { outcome: "verified", rejection_reason: null, incident_id: "01A" }),
      ],
    });
    const { container } = render(<Activity route={{ view: "activity", tab: "candidates" }} />);
    expect(await screen.findByText("Which checks refuse the most")).toBeInTheDocument();
    expect(await screen.findAllByText("scene unstable")).toHaveLength(2);
    expect(screen.getByText(/not a finding that nothing was there/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "incident" })).toHaveAttribute(
      "href",
      "#/incidents/01A",
    );
    expect(screen.getAllByText("62%").length).toBeGreaterThan(0);
    await expectNoAccessibilityViolations(container);
  });

  it("filters by outcome and period, and handles empty and failed loads", async () => {
    const { calls } = mockFetch({
      "/api/v1/events/candidates/reasons": [],
      "/api/v1/events/candidates": [],
    });
    const first = render(<Activity route={{ view: "activity", tab: "candidates" }} />);
    expect(await screen.findByText("No candidates match in this period.")).toBeInTheDocument();
    expect(screen.getByText("No candidate was refused in this period.")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Outcome"), "All outcomes");
    await userEvent.click(screen.getByRole("radio", { name: "Last hour" }));
    await waitFor(() => {
      expect(
        calls.some((c) => c.url.includes("candidates?hours=1") && !c.url.includes("outcome=")),
      ).toBe(true);
    });
    first.unmount();
    mockFetch({
      "/api/v1/events/candidates/reasons": [],
      "/api/v1/events/candidates": json({ detail: "db busy" }, { status: 503 }),
    });
    render(<Activity route={{ view: "activity", tab: "candidates" }} />);
    expect(await screen.findByText("db busy")).toBeInTheDocument();
  });
});
