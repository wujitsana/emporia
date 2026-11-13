/**
 * Pure projection math for the graph viewer: rotation, perspective, depth order, and the
 * layered layout. No DOM, no React, no dependency.
 *
 * Conventions: x runs right, y runs up, z runs toward the viewer. A point is rotated by yaw
 * (about y) then pitch (about x), then seen from `distance` units in front of the origin.
 * Screen y grows downward, so the projected y is flipped.
 */

export type Vec3 = readonly [number, number, number];
export type Mat3 = readonly [Vec3, Vec3, Vec3];

export interface View {
  /** Turn about the vertical axis, radians. */
  yaw: number;
  /** Tilt about the horizontal axis, radians; clamped to +-PITCH_LIMIT. */
  pitch: number;
  /** 1 = fit; clamped to [ZOOM_MIN, ZOOM_MAX]. */
  zoom: number;
  /** Pan in viewport units (pixels of the drawing box). */
  panX: number;
  panY: number;
}

export const PITCH_LIMIT = 1.45;
export const ZOOM_MIN = 0.4;
export const ZOOM_MAX = 4;
/** Camera distance from the origin, in scene radii. Larger = flatter perspective. */
export const CAMERA_DISTANCE = 4;

export const DEFAULT_VIEW: View = { yaw: 0.55, pitch: 0.3, zoom: 1, panX: 0, panY: 0 };

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

/** A view with every field in range and finite. */
export function clampView(v: View): View {
  const f = (n: number, d: number): number => (Number.isFinite(n) ? n : d);
  return {
    yaw: f(v.yaw, 0),
    pitch: clamp(f(v.pitch, 0), -PITCH_LIMIT, PITCH_LIMIT),
    zoom: clamp(f(v.zoom, 1), ZOOM_MIN, ZOOM_MAX),
    panX: f(v.panX, 0),
    panY: f(v.panY, 0),
  };
}

/** Rotation by `yaw` about y, then `pitch` about x. Rows are the image of x, y, z (row-major). */
export function rotationMatrix(yaw: number, pitch: number): Mat3 {
  const cy = Math.cos(yaw);
  const sy = Math.sin(yaw);
  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);
  // R = Rx(pitch) * Ry(yaw)
  return [
    [cy, 0, sy],
    [sp * sy, cp, -sp * cy],
    [-cp * sy, sp, cp * cy],
  ];
}

export function applyMatrix(m: Mat3, p: Vec3): Vec3 {
  return [
    m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2],
    m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2],
    m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2],
  ];
}

export interface Projected {
  /** Screen position in the drawing box. */
  x: number;
  y: number;
  /** Rotated z: larger is nearer the viewer. Sort ascending to paint far first. */
  depth: number;
  /** Perspective factor relative to the origin's plane (1 at the origin; nearer > 1). */
  scale: number;
}

export interface Box {
  width: number;
  height: number;
}

/** Pixels per scene unit at zoom 1: the scene radius (1) fits the shorter side with a margin. */
export function unitPixels(box: Box): number {
  return Math.min(box.width, box.height) / 2.5;
}

export function projectRotated(r: Vec3, view: View, box: Box): Projected {
  const k = CAMERA_DISTANCE / Math.max(0.05, CAMERA_DISTANCE - r[2]);
  const u = unitPixels(box) * view.zoom * k;
  return {
    x: box.width / 2 + view.panX + r[0] * u,
    y: box.height / 2 + view.panY - r[1] * u,
    depth: r[2],
    scale: k * view.zoom,
  };
}

export function project(p: Vec3, view: View, box: Box): Projected {
  return projectRotated(applyMatrix(rotationMatrix(view.yaw, view.pitch), p), view, box);
}

/** Project many points with one matrix. */
export function projectAll(points: readonly Vec3[], view: View, box: Box): Projected[] {
  const m = rotationMatrix(view.yaw, view.pitch);
  return points.map((p) => projectRotated(applyMatrix(m, p), view, box));
}

/** Indices ordered far to near (paint order). Ties keep their input order. */
export function depthOrder(depths: readonly number[]): number[] {
  return depths
    .map((d, i) => [d, i] as const)
    .sort((a, b) => a[0] - b[0] || a[1] - b[1])
    .map((e) => e[1]);
}

export interface LayoutNode {
  id: string;
  layer: number;
  index: number;
}

/**
 * Layers along the x axis (the depth of a network), the nodes of one layer on a ring in the y-z
 * plane (a layer of one sits on the axis, a layer of two on a line). Everything fits the unit
 * sphere, so zoom 1 shows the whole thing.
 */
export function layerLayout(nodes: readonly LayoutNode[]): Map<string, Vec3> {
  const byLayer = new Map<number, LayoutNode[]>();
  for (const n of nodes) {
    const l = byLayer.get(n.layer);
    if (l) l.push(n);
    else byLayer.set(n.layer, [n]);
  }
  const layers = [...byLayer.keys()].sort((a, b) => a - b);
  const out = new Map<string, Vec3>();
  const span = layers.length;
  layers.forEach((layer, li) => {
    const members = (byLayer.get(layer) ?? []).slice().sort((a, b) => a.index - b.index);
    const x = span <= 1 ? 0 : (li / (span - 1)) * 1.7 - 0.85;
    const n = members.length;
    const ring = n <= 1 ? 0 : Math.min(0.62, 0.22 + 0.06 * n);
    members.forEach((m, i) => {
      // Start at the top and go round, so index 0 is always up.
      const a = n <= 1 ? 0 : (i / n) * Math.PI * 2;
      out.set(m.id, [x, ring * Math.cos(a), ring * Math.sin(a)]);
    });
  });
  return out;
}

/** Zoom after a wheel notch: `deltaY` > 0 zooms out. */
export function zoomBy(view: View, factor: number): View {
  return clampView({ ...view, zoom: view.zoom * factor });
}

export function orbitBy(view: View, dYaw: number, dPitch: number): View {
  return clampView({ ...view, yaw: view.yaw + dYaw, pitch: view.pitch + dPitch });
}

export function panBy(view: View, dx: number, dy: number): View {
  return clampView({ ...view, panX: view.panX + dx, panY: view.panY + dy });
}
