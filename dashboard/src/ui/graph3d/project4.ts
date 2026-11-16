/**
 * Four dimensions down to three: pure math, no DOM. A point is [x, y, z, w]. `rotate4` turns it in two planes at once
 * (x-w and y-z, a double rotation, the one that makes a tesseract look like it turns inside out); `to3` looks at it from
 * `distance` units along w and drops w. Lines stay lines, so a point inside a figure stays inside its drawn outline.
 * What it does not keep is lengths: they shorten with w and change as the figure turns.
 */
import type { Vec3 } from "./project";

export type Vec4 = readonly [number, number, number, number];

/** A double rotation: `a` radians in the x-w plane, `b` in the y-z plane. */
export function rotate4(p: Vec4, a: number, b: number): Vec4 {
  const ca = Math.cos(a), sa = Math.sin(a), cb = Math.cos(b), sb = Math.sin(b);
  return [ca * p[0] - sa * p[3], cb * p[1] - sb * p[2], sb * p[1] + cb * p[2], sa * p[0] + ca * p[3]];
}

/** Perspective from 4D to 3D, the eye `distance` along w (more than the figure's radius, or the point is behind it). */
export function to3(p: Vec4, distance = 3): Vec3 {
  const k = distance / Math.max(0.05, distance - p[3]);
  return [p[0] * k, p[1] * k, p[2] * k];
}
