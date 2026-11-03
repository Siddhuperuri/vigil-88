import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { expectNoAccessibilityViolations } from "../test-utils";
import { bodyOf, installSocket, json, mockFetch } from "../test-support";
import { Wrap, none } from "./control-support";
import { Tuning } from "./Tuning";

beforeEach(() => {
  installSocket();
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
