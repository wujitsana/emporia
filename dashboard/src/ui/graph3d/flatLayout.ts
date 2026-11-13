import type { GraphNode } from "./types";
import type { Placed } from "./GraphSvg";

export const FLAT_W = 640;
export const FLAT_H = 420;

/** The same graph as a layered 2D diagram: one column per layer, nodes evenly down it. */
export function flatLayout(nodes: readonly GraphNode[], width = FLAT_W, height = FLAT_H): { placed: Placed[]; tags: { x: number; text: string }[] } {
  const layers = [...new Set(nodes.map((n) => n.layer))].sort((a, b) => a - b);
  const padX = 48;
  const padY = 30;
  const colX = (layer: number): number => {
    const li = layers.indexOf(layer);
    return layers.length <= 1 ? width / 2 : padX + (li / (layers.length - 1)) * (width - 2 * padX);
  };
  const count = new Map<number, number>();
  for (const n of nodes) count.set(n.layer, (count.get(n.layer) ?? 0) + 1);
  const rank = new Map<string, number>();
  const sorted = [...nodes].sort((a, b) => a.layer - b.layer || a.index - b.index);
  const seen = new Map<number, number>();
  for (const n of sorted) {
    const k = seen.get(n.layer) ?? 0;
    rank.set(n.id, k);
    seen.set(n.layer, k + 1);
  }
  const placed = nodes.map((n) => {
    const c = count.get(n.layer) ?? 1;
    const k = rank.get(n.id) ?? 0;
    const y = c <= 1 ? height / 2 : padY + (k / (c - 1)) * (height - 2 * padY - 12);
    return { x: colX(n.layer), y, scale: 1, depth: 0 };
  });
  return { placed, tags: layers.map((l) => ({ x: colX(l), text: `layer ${l + 1}` })) };
}

