import { describe, expect, it } from "vitest";
import {
  type Point,
  area,
  clamp01,
  nearest,
  normalise,
  problemWith,
  round3,
  segmentsMeet,
  tidy,
} from "./geometry";

const square: Point[] = [
  [0.1, 0.1],
  [0.9, 0.1],
  [0.9, 0.9],
  [0.1, 0.9],
];

describe("tidy", () => {
  it("keeps points inside the frame and at a readable precision", () => {
    expect(tidy([-0.2, 1.4])).toEqual([0, 1]);
    expect(tidy([0.123456, 0.987654])).toEqual([0.123, 0.988]);
    expect(clamp01(0.5)).toBe(0.5);
    expect(round3(0.0004)).toBe(0);
  });
});

describe("segmentsMeet", () => {
  it("finds a proper crossing, a touch at an end, and a collinear overlap", () => {
    expect(segmentsMeet([0, 0], [1, 1], [0, 1], [1, 0])).toBe(true);
    expect(segmentsMeet([0, 0], [1, 0], [1, 0], [1, 1])).toBe(true);
    expect(segmentsMeet([0, 0], [2, 0], [1, 0], [3, 0])).toBe(true);
  });
  it("does not join segments that miss, including collinear ones apart", () => {
    expect(segmentsMeet([0, 0], [1, 0], [0, 1], [1, 1])).toBe(false);
    expect(segmentsMeet([0, 0], [1, 0], [2, 0], [3, 0])).toBe(false);
  });
});

describe("problemWith", () => {
  it("accepts a simple polygon, convex or not", () => {
    expect(problemWith(square)).toBeNull();
    expect(
      problemWith([
        [0.1, 0.1],
        [0.5, 0.4],
        [0.9, 0.1],
        [0.9, 0.9],
        [0.1, 0.9],
      ]),
    ).toBeNull();
  });
  it("says why a polygon would be refused, in words", () => {
    expect(problemWith(square.slice(0, 2))).toMatch(/at least 3/);
    expect(
      problemWith([
        [0, 0],
        [1, 1],
        [1, 0],
        [0, 1],
      ]),
    ).toMatch(/crosses itself/);
    expect(
      problemWith([
        [0.2, 0.2],
        [0.2, 0.2],
        [0.8, 0.8],
      ]),
    ).toMatch(/same place/);
    expect(
      problemWith([
        [0.5, 0.5],
        [0.5001, 0.5],
        [0.5, 0.5001],
      ]),
    ).toMatch(/almost none/);
    expect(
      problemWith(Array.from({ length: 65 }, (_, i): Point => [i / 100, ((i * 7) % 100) / 100])),
    ).toMatch(/at most 64/);
  });
});

describe("area, normalise, nearest", () => {
  it("computes area regardless of winding", () => {
    expect(area(square)).toBeCloseTo(0.64);
    expect(area([...square].reverse())).toBeCloseTo(0.64);
  });
  it("turns a click into a normalised point, and a zero-size box into the origin", () => {
    expect(normalise(150, 100, { left: 50, top: 0, width: 200, height: 200 })).toEqual([0.5, 0.5]);
    expect(normalise(5, 5, { left: 0, top: 0, width: 0, height: 0 })).toEqual([0, 0]);
    expect(normalise(-50, 500, { left: 0, top: 0, width: 100, height: 100 })).toEqual([0, 1]);
  });
  it("picks the closest point within the grab radius, else none", () => {
    expect(nearest(square, [0.12, 0.11], 0.05)).toBe(0);
    expect(nearest(square, [0.5, 0.5], 0.05)).toBe(-1);
  });
});
