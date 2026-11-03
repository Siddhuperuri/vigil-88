import { useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent } from "react";
import { type Point, normalise, tidy } from "./geometry";

const STEP = 0.01;
const BIG_STEP = 0.05;

/**
 * One frame with every zone drawn over it and the selected zone's points editable. Points drag with
 * a pointer and also move with the arrow keys (Shift for a larger step), and the editor beside it
 * offers typed coordinates, so dragging is never the only way (WCAG 2.5.7).
 */
export function ZoneCanvas({
  pictureUrl,
  zones,
  selected,
  onMove,
  onRemovePoint,
  onAddPoint,
}: {
  pictureUrl: string | null;
  zones: { id: string; points: Point[] }[];
  selected: string | null;
  onMove: (index: number, to: Point) => void;
  onRemovePoint: (index: number) => void;
  onAddPoint: (at: Point) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState<number | null>(null);
  const [pictureFailed, setPictureFailed] = useState(false);
  const active = zones.find((z) => z.id === selected) ?? null;

  const at = (event: PointerEvent): Point | null => {
    const el = box.current;
    if (el === null) return null;
    return normalise(event.clientX, event.clientY, el.getBoundingClientRect());
  };

  const key = (event: KeyboardEvent, index: number, p: Point) => {
    const step = event.shiftKey ? BIG_STEP : STEP;
    const delta: Record<string, Point> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    };
    const d = delta[event.key];
    if (d !== undefined) {
      event.preventDefault();
      onMove(index, tidy([p[0] + d[0], p[1] + d[1]]));
    } else if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      onRemovePoint(index);
    }
  };

  return (
    <div
      className="zone-frame"
      ref={box}
      onPointerMove={(e) => {
        if (dragging === null) return;
        const p = at(e);
        if (p !== null) onMove(dragging, p);
      }}
      onPointerUp={() => {
        setDragging(null);
      }}
      onPointerLeave={() => {
        setDragging(null);
      }}
    >
      {pictureUrl !== null && !pictureFailed && (
        <img
          src={pictureUrl}
          alt="Current view from this camera, for placing zones"
          onError={() => {
            setPictureFailed(true);
          }}
        />
      )}
      <svg
        viewBox="0 0 1 1"
        preserveAspectRatio="none"
        role="group"
        aria-label="Zones drawn over the camera frame"
        onDoubleClick={(e) => {
          const el = box.current;
          if (el === null || active === null) return;
          onAddPoint(normalise(e.clientX, e.clientY, el.getBoundingClientRect()));
        }}
      >
        {zones.map((z) => (
          <polygon
            key={z.id}
            points={z.points.map((p) => p.join(",")).join(" ")}
            className={z.id === selected ? "zone zone-on" : "zone"}
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>
      {active?.points.map((p, i) => (
        <button
          // The index is the identity of a point: they have no other.
          key={i}
          type="button"
          className="handle"
          style={{ left: `${String(p[0] * 100)}%`, top: `${String(p[1] * 100)}%` }}
          aria-label={`Point ${String(i + 1)} of ${active.id}, at ${String(p[0])}, ${String(p[1])}`}
          onPointerDown={(e) => {
            e.currentTarget.focus();
            setDragging(i);
          }}
          onKeyDown={(e) => {
            key(e, i, p);
          }}
        />
      ))}
      {(pictureUrl === null || pictureFailed) && (
        <p className="zone-note muted">
          No picture: this camera is not running yet. Zones are placed as fractions of the frame, so
          you can still draw them.
        </p>
      )}
    </div>
  );
}
