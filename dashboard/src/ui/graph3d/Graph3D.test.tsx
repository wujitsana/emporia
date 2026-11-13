// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Graph3D } from "./Graph3D";
import { SMALL_GRAPH, LARGE_GRAPH } from "./fixture";
import { nodeName } from "./edgeStyle";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const { nodes, edges } = SMALL_GRAPH;
const stage = () => screen.getByTestId("graph3d-stage");
const svg = () => stage().querySelector("svg") as SVGSVGElement;
const attr = (n: string) => Number(svg().getAttribute(n));
const mode = (name: string) => fireEvent.click(screen.getByRole("button", { name }));

describe("Graph3D story: the fixture in each view", () => {
  it("draws every edge and node, every node focusable with a name", () => {
    render(<Graph3D nodes={nodes} edges={edges} initialMode="3d" animate={false} label="Fixture" />);
    expect(svg().querySelectorAll("[data-edge]").length).toBe(edges.length);
    expect(svg().querySelectorAll("[data-node]").length).toBe(nodes.length);
    const hits = [...svg().querySelectorAll<SVGElement>("[data-hit]")];
    expect(hits.length).toBe(nodes.length);
    for (const h of hits) {
      expect(h.getAttribute("tabindex")).toBe("0");
      expect(h.getAttribute("aria-label")).toMatch(/^L\d unit \d, layer \d, value -?[\d.]+;/);
    }
  });

  it("the name states layer, value and the strongest edges", () => {
    const labels = new Map(nodes.map((n) => [n.id, n.label]));
    const name = nodeName(nodes[0], edges, labels);
    expect(name).toContain("layer 1");
    expect(name).toContain("strongest edges: to ");
  });

  it("the three views show the same edges", () => {
    render(<Graph3D nodes={nodes} edges={edges} initialMode="3d" animate={false} />);
    const n3 = svg().querySelectorAll("[data-edge]").length;
    mode("Flat");
    const nFlat = document.querySelectorAll("svg [data-edge]").length;
    mode("Table");
    const nTable = document.querySelectorAll("tbody tr[data-edge]").length;
    expect([n3, nFlat, nTable]).toEqual([edges.length, edges.length, edges.length]);
    expect(within(document.body).getAllByRole("columnheader").length).toBe(8);
  });

  it("edge sign is style and colour class, blocked is marked", () => {
    render(<Graph3D nodes={nodes} edges={edges} initialMode="3d" animate={false} />);
    const neg = edges.filter((e) => e.weight < 0).length;
    expect(svg().querySelectorAll('[data-sign="neg"]').length).toBe(neg);
    expect(svg().querySelectorAll("[data-blocked]").length).toBe(edges.filter((e) => e.blocked).length);
  });
});

describe("Graph3D navigation", () => {
  it("arrows turn, shift-arrows pan, +/- zoom, R and 0 reset", () => {
    render(<Graph3D nodes={nodes} edges={edges} initialMode="3d" animate={false} />);
    const y0 = attr("data-yaw");
    fireEvent.keyDown(stage(), { key: "ArrowRight" });
    expect(attr("data-yaw")).toBeGreaterThan(y0);
    const p0 = attr("data-pitch");
    fireEvent.keyDown(stage(), { key: "ArrowDown" });
    expect(attr("data-pitch")).toBeGreaterThan(p0);
    fireEvent.keyDown(stage(), { key: "ArrowLeft", shiftKey: true });
    expect(svg().getAttribute("data-pan")).toBe("-24,0");
    fireEvent.keyDown(stage(), { key: "+" });
    expect(attr("data-zoom")).toBeGreaterThan(1);
    fireEvent.keyDown(stage(), { key: "0" });
    expect([attr("data-yaw"), attr("data-zoom"), svg().getAttribute("data-pan")]).toEqual([0.55, 1, "0,0"]);
    fireEvent.keyDown(stage(), { key: "ArrowUp" });
    fireEvent.keyDown(stage(), { key: "r" });
    expect(attr("data-pitch")).toBe(0.3);
  });

  it("keys pressed on a node reach the viewer, Enter selects", () => {
    const onSelect = vi.fn();
    render(<Graph3D nodes={nodes} edges={edges} initialMode="3d" animate={false} onSelect={onSelect} />);
    const hit = svg().querySelector("[data-hit]") as SVGElement;
    const y0 = attr("data-yaw");
    fireEvent.keyDown(hit, { key: "ArrowLeft" });
    expect(attr("data-yaw")).toBeLessThan(y0);
    fireEvent.keyDown(hit, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith(nodes[0].id);
    expect(hit.getAttribute("aria-pressed")).toBe("true");
  });

  it("dragging turns, shift-dragging pans, the wheel zooms", () => {
    render(<Graph3D nodes={nodes} edges={edges} initialMode="3d" animate={false} />);
    const y0 = attr("data-yaw");
    fireEvent.pointerDown(svg(), { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(svg(), { pointerId: 1, clientX: 60, clientY: 10 });
    fireEvent.pointerUp(svg(), { pointerId: 1 });
    expect(attr("data-yaw")).not.toBe(y0);
    fireEvent.pointerDown(svg(), { pointerId: 2, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(svg(), { pointerId: 2, clientX: 30, clientY: 20, shiftKey: true });
    fireEvent.pointerUp(svg(), { pointerId: 2 });
    expect(svg().getAttribute("data-pan")).not.toBe("0,0");
    const z0 = attr("data-zoom");
    fireEvent.wheel(svg(), { deltaY: -200 });
    expect(attr("data-zoom")).toBeGreaterThan(z0);
  });

  it("two fingers pinch to zoom", () => {
    render(<Graph3D nodes={nodes} edges={edges} initialMode="3d" animate={false} />);
    fireEvent.pointerDown(svg(), { pointerId: 1, clientX: 100, clientY: 100 });
    fireEvent.pointerDown(svg(), { pointerId: 2, clientX: 200, clientY: 100 });
    fireEvent.pointerMove(svg(), { pointerId: 2, clientX: 300, clientY: 100 });
    expect(attr("data-zoom")).toBeGreaterThan(1);
  });
});

describe("Graph3D motion", () => {
  it("animate pulses edges that carry flow; off draws a still underlay", () => {
    const { unmount } = render(<Graph3D nodes={nodes} edges={edges} initialMode="3d" animate />);
    expect(svg().querySelectorAll("[data-pulse]").length).toBeGreaterThan(0);
    expect(svg().querySelectorAll("[data-flow-still]").length).toBe(0);
    unmount();
    render(<Graph3D nodes={nodes} edges={edges} initialMode="3d" animate={false} />);
    expect(svg().querySelectorAll("[data-pulse]").length).toBe(0);
    expect(svg().querySelectorAll("[data-flow-still]").length).toBeGreaterThan(0);
  });

  it("prefers-reduced-motion stops the pulse and opens the flat view", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce"), media: q }));
    Object.defineProperty(window, "matchMedia", { value: (q: string) => ({ matches: q.includes("reduce"), media: q }), configurable: true });
    const { container } = render(<Graph3D nodes={nodes} edges={edges} />);
    expect(container.querySelectorAll("[data-pulse]").length).toBe(0);
    expect(screen.queryByTestId("graph3d-stage")).toBeNull();
    expect(container.querySelectorAll("[data-flow-still]").length).toBeGreaterThan(0);
  });
});

describe("Graph3D selection", () => {
  it("a selected node dims the edges that do not touch it", () => {
    render(<Graph3D nodes={nodes} edges={edges} initialMode="3d" animate={false} selected={nodes[0].id} />);
    const touching = edges.filter((e) => e.from === nodes[0].id || e.to === nodes[0].id).length;
    const dimmed = [...svg().querySelectorAll("[data-edge]")].filter((l) => /dim/.test(l.getAttribute("class") ?? "")).length;
    expect(dimmed).toBe(edges.length - touching);
  });
});

describe("Graph3D speed (about 100 nodes, 400 edges)", () => {
  it("renders and re-projects the large fixture quickly", () => {
    const { nodes: N, edges: E } = LARGE_GRAPH;
    expect(N.length).toBeGreaterThanOrEqual(90);
    expect(E.length).toBeGreaterThanOrEqual(350);
    const t0 = performance.now();
    render(<Graph3D nodes={N} edges={E} initialMode="3d" animate={false} />);
    const first = performance.now() - t0;
    const t1 = performance.now();
    for (let i = 0; i < 10; i++) act(() => void fireEvent.keyDown(stage(), { key: "ArrowRight" }));
    const perTurn = (performance.now() - t1) / 10;
    console.info(`graph3d ${N.length} nodes / ${E.length} edges: first render ${first.toFixed(0)} ms, ${perTurn.toFixed(1)} ms per turn (jsdom)`);
    expect(first).toBeLessThan(3000);
    expect(perTurn).toBeLessThan(500);
  });
});
