import { describe, expect, it } from "vitest";
import {
  CAMERA_DISTANCE,
  PITCH_LIMIT,
  ZOOM_MAX,
  ZOOM_MIN,
  applyMatrix,
  clampView,
  depthOrder,
  layerLayout,
  orbitBy,
  project,
  projectAll,
  rotationMatrix,
  zoomBy,
  type Vec3,
} from "./project";

const BOX = { width: 600, height: 400 };
const V0 = { yaw: 0, pitch: 0, zoom: 1, panX: 0, panY: 0 };
const near = (a: number, b: number, d = 1e-9) => expect(Math.abs(a - b)).toBeLessThan(d);

describe("rotation", () => {
  it("is the identity at zero", () => {
    const r = applyMatrix(rotationMatrix(0, 0), [1, 2, 3]);
    [1, 2, 3].forEach((v, i) => near(r[i], v));
  });
  it("a quarter turn of yaw sends +x to -z", () => {
    const r = applyMatrix(rotationMatrix(Math.PI / 2, 0), [1, 0, 0]);
    near(r[0], 0);
    near(r[2], -1);
  });
  it("a quarter turn of pitch sends +z to -y (the top is seen from above)", () => {
    const r = applyMatrix(rotationMatrix(0, Math.PI / 2), [0, 0, 1]);
    near(r[1], -1);
    near(r[2], 0);
  });
  it("preserves length", () => {
    const p: Vec3 = [0.3, -0.7, 0.5];
    const r = applyMatrix(rotationMatrix(1.1, -0.6), p);
    near(Math.hypot(...r), Math.hypot(...p));
  });
});

describe("perspective", () => {
  it("puts the origin at the centre with scale 1", () => {
    const p = project([0, 0, 0], V0, BOX);
    near(p.x, 300);
    near(p.y, 200);
    near(p.scale, 1);
  });
  it("flips y for the screen", () => {
    expect(project([0, 1, 0], V0, BOX).y).toBeLessThan(200);
  });
  it("makes a nearer point larger and farther from the centre", () => {
    const nearP = project([0.5, 0, 1], V0, BOX);
    const farP = project([0.5, 0, -1], V0, BOX);
    expect(nearP.scale).toBeGreaterThan(farP.scale);
    expect(nearP.x - 300).toBeGreaterThan(farP.x - 300);
    expect(nearP.depth).toBeGreaterThan(farP.depth);
  });
  it("matches the divide k = D / (D - z)", () => {
    const p = project([0, 0, 1], V0, BOX);
    near(p.scale, CAMERA_DISTANCE / (CAMERA_DISTANCE - 1));
  });
  it("zoom scales offsets and pan moves them", () => {
    const a = project([1, 0, 0], V0, BOX);
    const b = project([1, 0, 0], { ...V0, zoom: 2, panX: 10 }, BOX);
    near(b.x - 310, 2 * (a.x - 300));
  });
  it("projectAll agrees with project", () => {
    const pts: Vec3[] = [[1, 0, 0], [0, 1, 0.5]];
    const view = { ...V0, yaw: 0.4, pitch: 0.2 };
    projectAll(pts, view, BOX).forEach((p, i) => expect(p).toEqual(project(pts[i], view, BOX)));
  });
});

describe("depth order", () => {
  it("is far to near and stable on ties", () => {
    expect(depthOrder([0.5, -1, 0.5, 2])).toEqual([1, 0, 2, 3]);
  });
  it("follows a half turn: what was near becomes far", () => {
    const pts: Vec3[] = [[0, 0, 1], [0, 0, -1]];
    const front = projectAll(pts, V0, BOX).map((p) => p.depth);
    const back = projectAll(pts, { ...V0, yaw: Math.PI }, BOX).map((p) => p.depth);
    expect(depthOrder(front)).toEqual([1, 0]);
    expect(depthOrder(back)).toEqual([0, 1]);
  });
});

describe("view limits", () => {
  it("clamps pitch and zoom and repairs NaN", () => {
    expect(clampView({ ...V0, pitch: 9 }).pitch).toBe(PITCH_LIMIT);
    expect(clampView({ ...V0, pitch: -9 }).pitch).toBe(-PITCH_LIMIT);
    expect(zoomBy(V0, 100).zoom).toBe(ZOOM_MAX);
    expect(zoomBy(V0, 0.001).zoom).toBe(ZOOM_MIN);
    expect(clampView({ ...V0, yaw: NaN, zoom: NaN }).zoom).toBe(1);
  });
  it("orbit adds to yaw and pitch", () => {
    const v = orbitBy(V0, 0.1, 0.2);
    near(v.yaw, 0.1);
    near(v.pitch, 0.2);
  });
});

describe("layer layout", () => {
  const nodes = [
    { id: "a", layer: 0, index: 0 },
    { id: "b", layer: 0, index: 1 },
    { id: "c", layer: 1, index: 0 },
    { id: "d", layer: 2, index: 0 },
    { id: "e", layer: 2, index: 1 },
    { id: "f", layer: 2, index: 2 },
  ];
  const pos = layerLayout(nodes);
  it("orders layers along x and shares x within a layer", () => {
    expect(pos.get("a")![0]).toBeLessThan(pos.get("c")![0]);
    expect(pos.get("c")![0]).toBeLessThan(pos.get("d")![0]);
    expect(pos.get("a")![0]).toBe(pos.get("b")![0]);
  });
  it("puts a lone node on the axis and ring members apart, inside the unit sphere", () => {
    expect(pos.get("c")![1]).toBe(0);
    expect(pos.get("d")).not.toEqual(pos.get("e"));
    for (const p of pos.values()) expect(Math.hypot(...p)).toBeLessThanOrEqual(1.4);
  });
  it("is deterministic", () => {
    expect([...layerLayout(nodes)]).toEqual([...pos]);
  });
});

describe("projection speed", () => {
  it("projects and orders 100 nodes plus 400 edges well under a frame", async () => {
    const { LARGE_GRAPH } = await import("./fixture");
    const pos = layerLayout(LARGE_GRAPH.nodes);
    const pts = LARGE_GRAPH.nodes.map((n) => pos.get(n.id)!);
    const view = { yaw: 0.5, pitch: 0.2, zoom: 1, panX: 0, panY: 0 };
    const t0 = performance.now();
    for (let i = 0; i < 100; i++) {
      const pr = projectAll(pts, view, BOX);
      depthOrder(pr.map((p) => p.depth));
      depthOrder(LARGE_GRAPH.edges.map((_, k) => pr[k % pr.length].depth));
    }
    const per = (performance.now() - t0) / 100;
    console.info(`projection of ${pts.length} nodes / ${LARGE_GRAPH.edges.length} edges: ${per.toFixed(3)} ms per frame`);
    expect(per).toBeLessThan(5);
  });
});
