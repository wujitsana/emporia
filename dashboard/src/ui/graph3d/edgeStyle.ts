import type { GraphEdge, GraphNode } from "./types";

export const widthOf = (weight: number, max: number, lo = 0.6, hi = 4.2): number =>
  lo + (hi - lo) * (max > 0 ? Math.min(1, Math.abs(weight) / max) : 0);

export const maxAbs = (xs: readonly number[]): number => xs.reduce((m, x) => Math.max(m, Math.abs(x)), 0);

export const fmt = (n: number): string => (Math.round(n * 100) / 100).toString();

/** The accessible name of a node: label, layer, value and the strongest edges touching it. */
export function nodeName(n: GraphNode, edges: readonly GraphEdge[], labels: ReadonlyMap<string, string>, top = 2): string {
  const mine = edges.filter((e) => e.from === n.id || e.to === n.id).sort((a, b) => Math.abs(b.weight) - Math.abs(a.weight));
  const strong = mine.slice(0, top).map((e) => {
    const other = e.from === n.id ? e.to : e.from;
    return `${e.from === n.id ? "to" : "from"} ${labels.get(other) ?? other} ${fmt(e.weight)}${e.blocked ? " blocked" : ""}`;
  });
  const tail = strong.length ? `; strongest edges: ${strong.join(", ")}` : "; no edges";
  return `${n.label}, layer ${n.layer + 1}, value ${fmt(n.value)}${tail}`;
}
