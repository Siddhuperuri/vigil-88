import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TelemetryProvider } from "../state/telemetry";
import { expectNoAccessibilityViolations } from "../test-utils";
import {
  installSocket,
  json,
  lastSocket,
  mockFetch,
  snapshot,
  stubObjectUrls,
} from "../test-support";
import { DegradationBanner } from "./DegradationBanner";
import { DiagnosticsButton } from "./DiagnosticsButton";
import { EvidenceContinuity } from "./EvidenceContinuity";
import { WorkersPanel } from "./WorkersPanel";

const worker = (name: string, over: Record<string, unknown> = {}) => ({
  name,
  alive: true,
  crashed: false,
  error: null,
  heartbeat_age_ms: 400,
  restartable: true,
  ...over,
});

beforeEach(() => {
  installSocket();
});

describe("the degradation banner", () => {
  const drawBanner = () =>
    render(
      <TelemetryProvider onEnded={() => undefined}>
        <DegradationBanner />
      </TelemetryProvider>,
    );

  it("says nothing while the engine is keeping up", () => {
    const { container } = drawBanner();
    lastSocket().open();
    lastSocket().send(
      snapshot({
        degradation: {
          step: 0,
          label: "normal",
          reason: null,
          since_utc: null,
          suspended_cameras: [],
        },
      }),
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("says nothing to an engine that predates the ladder", () => {
    const { container } = drawBanner();
    lastSocket().open();
    lastSocket().send(snapshot());
    expect(container).toBeEmptyDOMElement();
  });

  it("names the step, the reason and the cameras that are not being analysed", async () => {
    const { container } = drawBanner();
    lastSocket().open();
    lastSocket().send(
      snapshot({
        degradation: {
          step: 2,
          label: "priority cameras only",
          reason: "analysis dropped 40 frames in one interval",
          since_utc: "2026-11-03T10:00:00Z",
          suspended_cameras: ["yard", "garage"],
        },
      }),
    );
    const banner = await screen.findByRole("status", { name: "Analysis is reduced" });
    expect(banner).toHaveTextContent("priority cameras only (step 2)");
    expect(banner).toHaveTextContent("analysis dropped 40 frames");
    expect(banner).toHaveTextContent("Not being analysed right now: yard, garage");
    await expectNoAccessibilityViolations(container);
  });

  it("says every camera is still analysed when only the rate is reduced", async () => {
    drawBanner();
    lastSocket().open();
    lastSocket().send(
      snapshot({
        degradation: {
          step: 1,
          label: "reduced rate",
          reason: null,
          since_utc: null,
          suspended_cameras: [],
        },
      }),
    );
    expect(
      await screen.findByText("Every camera is still analysed, at a lower rate."),
    ).toBeVisible();
    expect(screen.getByText("The engine cannot keep up.")).toBeInTheDocument();
  });
});

describe("the workers panel", () => {
  it("lists workers, marks the crashed one, and offers a restart only where one is safe", async () => {
    mockFetch({
      "/api/v1/system/workers": [
        worker("sampler"),
        worker("preroll", { alive: false, crashed: true, error: "RuntimeError('x')" }),
        worker("inference", { restartable: false }),
        worker("watchdog", { heartbeat_age_ms: null }),
        worker("stopped", { alive: false }),
      ],
    });
    const { container } = render(<WorkersPanel />);
    expect(await screen.findByText("CRASHED")).toBeInTheDocument();
    expect(screen.getAllByText("RUNNING")).toHaveLength(3);
    expect(screen.getByText("STOPPED")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart sampler" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restart inference" })).toBeNull();
    expect(screen.getByText("Restart the engine to restart this one.")).toBeInTheDocument();
    expect(screen.getByText("unavailable")).toBeInTheDocument();
    expect(screen.getAllByText("0.4 s ago").length).toBeGreaterThan(0);
    await expectNoAccessibilityViolations(container);
  });

  it("asks first, then restarts, and says what the engine answered", async () => {
    const { calls } = mockFetch({
      "/api/v1/system/workers/sampler/restart": { ok: true, detail: "restarted" },
      "/api/v1/system/workers": [worker("sampler")],
    });
    render(<WorkersPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Restart sampler" }));
    const dialog = await screen.findByRole("dialog", { name: "Restart sampler?" });
    expect(calls.some((c) => c.url.endsWith("/restart"))).toBe(false); // nothing happens on the first click
    await userEvent.click(within(dialog).getByRole("button", { name: "Restart" }));
    expect(await screen.findByText(/Restarted sampler: restarted/)).toBeInTheDocument();
    expect(calls.filter((c) => c.url.endsWith("/restart"))).toHaveLength(1);
  });

  it("can be cancelled without anything being sent", async () => {
    const { calls } = mockFetch({ "/api/v1/system/workers": [worker("sampler")] });
    render(<WorkersPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Restart sampler" }));
    await userEvent.click(
      within(await screen.findByRole("dialog", { name: "Restart sampler?" })).getByRole("button", {
        name: "Cancel",
      }),
    );
    expect(calls.some((c) => c.url.endsWith("/restart"))).toBe(false);
  });

  it("reports a refusal in the engine's own words", async () => {
    mockFetch({
      "/api/v1/system/workers/sampler/restart": {
        ok: false,
        detail: "the old thread is still running",
      },
      "/api/v1/system/workers": [worker("sampler")],
    });
    render(<WorkersPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Restart sampler" }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Restart" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Not restarted sampler: the old thread is still running",
    );
  });

  it("reports a request that failed outright", async () => {
    mockFetch({
      "/api/v1/system/workers/sampler/restart": json({ detail: "forbidden" }, { status: 403 }),
      "/api/v1/system/workers": [worker("sampler")],
    });
    render(<WorkersPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Restart sampler" }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Restart" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("forbidden");
  });

  it("says when the list cannot be read, and keeps what it had", async () => {
    mockFetch({ "/api/v1/system/workers": json({ detail: "busy" }, { status: 500 }) });
    render(<WorkersPanel />);
    expect(await screen.findByText("Could not read the workers")).toBeInTheDocument();
  });
});

describe("the diagnostics button", () => {
  it("downloads the bundle and says nothing was sent anywhere", async () => {
    const urls = stubObjectUrls();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(new Blob(["zip"]), { status: 200 }))),
    );
    const { container } = render(<DiagnosticsButton />);
    await userEvent.click(screen.getByRole("button", { name: "Download diagnostics (zip)" }));
    expect(await screen.findByText("Saved. Nothing was sent anywhere.")).toBeInTheDocument();
    expect(urls.create).toHaveBeenCalled();
    expect(screen.getByText(/no video, no snapshots, no evidence/)).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("reports a refusal", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("no", { status: 403 }))),
    );
    render(<DiagnosticsButton />);
    await userEvent.click(screen.getByRole("button", { name: "Download diagnostics (zip)" }));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("The engine answered 403.");
    });
  });
});

describe("the evidence continuity notice", () => {
  const gap = (kind: string) => ({ kind, start_ms: -3200, end_ms: -400 });

  it("is silent when nothing is known or nothing is wrong", () => {
    const a = render(<EvidenceContinuity notes={null} />);
    expect(a.container).toBeEmptyDOMElement();
    const b = render(
      <EvidenceContinuity
        notes={{ stream_interrupted: false, gaps: [], preroll_shortfall_ms: 100 }}
      />,
    );
    expect(b.container).toBeEmptyDOMElement();
    const c = render(<EvidenceContinuity notes={undefined} />);
    expect(c.container).toBeEmptyDOMElement();
  });

  it("states where the hole is, in words, on the clips' own timeline", async () => {
    const { container } = render(
      <EvidenceContinuity
        notes={{
          stream_interrupted: true,
          gaps: [gap("stream_restarted"), gap("frame_gap"), gap("something_new")],
          preroll_shortfall_ms: null,
        }}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("This recording has a gap");
    expect(screen.getByText(/the camera connection was restarted/)).toBeInTheDocument();
    expect(screen.getByText(/the camera stopped sending pictures/)).toBeInTheDocument();
    expect(screen.getByText(/something new/)).toBeInTheDocument();
    expect(screen.getAllByText(/From -3.2 s to -0.4 s/).length).toBe(3);
    await expectNoAccessibilityViolations(container);
  });

  it("explains a pre-roll that starts late", () => {
    render(
      <EvidenceContinuity
        notes={{ stream_interrupted: false, gaps: [], preroll_shortfall_ms: 2500 }}
      />,
    );
    expect(
      screen.getByText(/starts 2.5 s later than the time before the event/),
    ).toBeInTheDocument();
  });
});
