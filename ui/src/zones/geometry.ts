/** Pure geometry for the zone editor. Coordinates are normalised to [0, 1], like the engine's. */
export type Point = [number, number];

export const MIN_POINTS = 3;
export const MAX_POINTS = 64;

export const clamp01 = (n: number): number => Math.min(1, Math.max(0, n));

export const round3 = (n: number): number => Math.round(n * 1000) / 1000;

/** Keeps a point inside the frame and at a precision a person can read and the file can hold. */
export const tidy = ([x, y]: Point): Point => [round3(clamp01(x)), round3(clamp01(y))];

const cross = (o: Point, a: Point, b: Point): number =>
  (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);

const onSegment = (a: Point, b: Point, p: Point): boolean =>
  Math.min(a[0], b[0]) <= p[0] &&
  p[0] <= Math.max(a[0], b[0]) &&
  Math.min(a[1], b[1]) <= p[1] &&
  p[1] <= Math.max(a[1], b[1]);

/** Whether segments ab and cd touch or cross, endpoints included. */
export function segmentsMeet(a: Point, b: Point, c: Point, d: Point): boolean {
  const d1 = cross(a, b, c);
  const d2 = cross(a, b, d);
  const d3 = cross(c, d, a);
  const d4 = cross(c, d, b);
  if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) {
    return true;
  }
  return (
    (d1 === 0 && onSegment(a, b, c)) ||
    (d2 === 0 && onSegment(a, b, d)) ||
    (d3 === 0 && onSegment(c, d, a)) ||
    (d4 === 0 && onSegment(c, d, b))
  );
}

/** Signed area, so a zero or a near-zero polygon can be told from a real one. */
export function area(points: readonly Point[]): number {
  let sum = 0;
  points.forEach((p, i) => {
    const q = points[(i + 1) % points.length] as Point;
    sum += p[0] * q[1] - q[0] * p[1];
  });
  return Math.abs(sum) / 2;
}

export const MIN_AREA = 1e-4; // the engine refuses anything smaller: a drawing mistake

/** Why a polygon would be refused, in words, or null. Mirrors the engine so the person is told
 * while drawing; the engine still has the last word. */
export function problemWith(points: readonly Point[]): string | null {
  if (points.length < MIN_POINTS) return `Add at least ${String(MIN_POINTS)} points.`;
  if (points.length > MAX_POINTS) return `Use at most ${String(MAX_POINTS)} points.`;
  const n = points.length;
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const adjacent = j === i + 1 || (i === 0 && j === n - 1);
      const a = points[i] as Point;
      const b = points[(i + 1) % n] as Point;
      const c = points[j] as Point;
      const d = points[(j + 1) % n] as Point;
      if (!adjacent && segmentsMeet(a, b, c, d)) {
        return "The outline crosses itself. Move a point so the edges do not cross.";
      }
    }
  }
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const a = points[i] as Point;
      const b = points[j] as Point;
      if (a[0] === b[0] && a[1] === b[1]) return "Two points are in the same place.";
    }
  }
  if (area(points) < MIN_AREA) return "That covers almost none of the frame.";
  return null;
}

/** A screen position inside an element's box, as a normalised point. */
export function normalise(
  clientX: number,
  clientY: number,
  box: { left: number; top: number; width: number; height: number },
): Point {
  if (box.width <= 0 || box.height <= 0) return [0, 0];
  return tidy([(clientX - box.left) / box.width, (clientY - box.top) / box.height]);
}

/** The index of the point nearest to (x, y) if within `radius`, else -1. */
export function nearest(points: readonly Point[], at: Point, radius: number): number {
  let best = -1;
  let bestDistance = radius;
  points.forEach((p, i) => {
    const distance = Math.hypot(p[0] - at[0], p[1] - at[1]);
    if (distance <= bestDistance) {
      best = i;
      bestDistance = distance;
    }
  });
  return best;
}
