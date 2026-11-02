import { useEffect, useState } from "react";
import { Notice, StateLabel, readout } from "../components/bits";
import { type Route, href } from "../router";
import { useTelemetry } from "../state/telemetry";

/** The overlays the engine can really draw. Track IDs, trails and zones are not offered because
 * they do not exist yet (08 §8: an option that cannot work is absent, not disabled). */
const OVERLAYS = [
  { id: "boxes", label: "Boxes" },
  { id: "labels", label: "Labels" },
  { id: "info", label: "Info" },
] as const;

const LAYOUTS = [
  { id: "auto", label: "Auto" },
  { id: "1", label: "1" },
  { id: "2", label: "2 × 2" },
  { id: "3", label: "3 × 3" },
] as const;
type Layout = (typeof LAYOUTS)[number]["id"];

function remembered<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const value = window.localStorage.getItem(key);
    return allowed.includes(value as T) ? (value as T) : fallback;
  } catch {
    return fallback; // storage can be blocked or cleared: the wall must still render
  }
}

function remember(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Not remembering a layout is a convenience lost, not an error.
  }
}

function overlayParam(enabled: ReadonlySet<string>): string {
  return enabled.size === 0
    ? "none"
    : OVERLAYS.filter((o) => enabled.has(o.id))
        .map((o) => o.id)
        .join(",");
}

async function saveSnapshot(cameraId: string, overlay: string): Promise<void> {
  const response = await fetch(
    `/api/v1/cameras/${encodeURIComponent(cameraId)}/snapshot.jpg?overlay=${overlay}`,
    { credentials: "same-origin" },
  );
  if (!response.ok) return;
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = `${cameraId}-${new Date().toISOString().replace(/[:.]/g, "-")}.jpg`;
  link.click();
  URL.revokeObjectURL(url);
}

function Tile({
  cameraId,
  state,
  fps,
  detail,
  overlay,
  large,
}: {
  cameraId: string;
  state: string;
  fps: number | null;
  detail: string | null;
  overlay: string;
  large: boolean;
}) {
  const [watching, setWatching] = useState(true);
  const live = state === "online" || state === "degraded";
  return (
    <figure className={`tile ${large ? "tile-large" : ""}`}>
      <div className="frame">
        {watching && live ? (
          <img
            src={`/api/v1/cameras/${encodeURIComponent(cameraId)}/stream.mjpeg?overlay=${overlay}`}
            alt={`Live view of ${cameraId}`}
          />
        ) : (
          <p className="frame-note" role="status">
            {!live ? `No picture: camera is ${state}.` : "Stopped watching. Resume to see it."}
            {detail !== null && !live ? ` ${detail}` : ""}
          </p>
        )}
      </div>
      <figcaption>
        <span className="tile-name">{cameraId}</span>
        <StateLabel state={state} />
        <span className="num">{readout(fps, " fps", 1)}</span>
        <span className="tile-actions">
          <button
            type="button"
            aria-pressed={!watching}
            onClick={() => {
              setWatching((w) => !w);
            }}
          >
            {watching ? "Stop watching" : "Resume"}
          </button>
          <button
            type="button"
            disabled={!live}
            onClick={() => {
              void saveSnapshot(cameraId, overlay);
            }}
          >
            Save snapshot
          </button>
          <a href={href({ view: "wall", camera: large ? null : cameraId })}>
            {large ? "Back to wall" : "Expand"}
          </a>
        </span>
      </figcaption>
    </figure>
  );
}

export function Wall({ route }: { route: Extract<Route, { view: "wall" }> }) {
  const { snapshot, link } = useTelemetry();
  const [layout, setLayout] = useState<Layout>(() =>
    remembered(
      "vigil.wall.layout",
      LAYOUTS.map((l) => l.id),
      "auto",
    ),
  );
  const [enabled, setEnabled] = useState<ReadonlySet<string>>(
    () => new Set(OVERLAYS.map((o) => o.id)),
  );
  const [full, setFull] = useState(false);

  useEffect(() => {
    const onChange = () => {
      setFull(document.fullscreenElement !== null);
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
    };
  }, []);

  if (snapshot === null) {
    return (
      <>
        <h1>Live wall</h1>
        <p role="status" className="muted">
          {link === "reconnecting" ? "Reconnecting to the engine…" : "Waiting for the engine…"}
        </p>
      </>
    );
  }
  const cameras = snapshot.cameras;
  if (cameras.length === 0) {
    return (
      <>
        <h1>Live wall</h1>
        <Notice kind="info" title="No cameras yet">
          <p>
            No camera is configured. Camera setup arrives in a later release; for now add one to the
            engine&rsquo;s configuration and restart it.
          </p>
        </Notice>
      </>
    );
  }
  const overlay = overlayParam(enabled);
  const shown =
    route.camera === null ? cameras : cameras.filter((c) => c.camera_id === route.camera);
  const columns =
    route.camera !== null || layout === "1"
      ? 1
      : layout === "auto"
        ? cameras.length > 4
          ? 3
          : cameras.length > 1
            ? 2
            : 1
        : Number(layout);

  return (
    <>
      <div className="toolbar">
        <h1>Live wall</h1>
        <fieldset>
          <legend>Layout</legend>
          {LAYOUTS.map((l) => (
            <label key={l.id} className="check">
              <input
                type="radio"
                name="layout"
                checked={layout === l.id}
                onChange={() => {
                  setLayout(l.id);
                  remember("vigil.wall.layout", l.id);
                }}
              />
              {l.label}
            </label>
          ))}
        </fieldset>
        <fieldset>
          <legend>Overlays</legend>
          {OVERLAYS.map((o) => (
            <label key={o.id} className="check">
              <input
                type="checkbox"
                checked={enabled.has(o.id)}
                onChange={(e) => {
                  setEnabled((prev) => {
                    const next = new Set(prev);
                    if (e.target.checked) next.add(o.id);
                    else next.delete(o.id);
                    return next;
                  });
                }}
              />
              {o.label}
            </label>
          ))}
        </fieldset>
        <button
          type="button"
          onClick={() => {
            if (full) void document.exitFullscreen();
            else void document.documentElement.requestFullscreen();
          }}
        >
          {full ? "Leave full screen" : "Full screen"}
        </button>
      </div>
      {route.camera !== null && shown.length === 0 && (
        <Notice kind="warning" title={`No camera called ${route.camera}`}>
          <a href={href({ view: "wall", camera: null })}>Back to the wall</a>
        </Notice>
      )}
      <div
        className="grid"
        style={{ gridTemplateColumns: `repeat(${String(columns)}, minmax(0, 1fr))` }}
      >
        {shown.map((c) => (
          <Tile
            key={`${c.camera_id}:${overlay}`}
            cameraId={c.camera_id}
            state={c.state}
            fps={c.measured_fps}
            detail={c.detail}
            overlay={overlay}
            large={route.camera !== null}
          />
        ))}
      </div>
    </>
  );
}
