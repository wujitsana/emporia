import type { GraphEdge, GraphNode } from "./types";

/** A small seeded generator, so a fixture is the same everywhere. */
function rng(seed: number): () => number {
  let s = seed >>> 0 || 1;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/**
 * A fully connected layered graph of the given layer sizes: values and weights in [-1, 1],
 * some edges blocked, some carrying flow, some with owners. `density` thins the edges.
 */
export function layeredGraph(sizes: readonly number[], seed = 7, density = 1): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const r = rng(seed);
  const nodes: GraphNode[] = [];
  sizes.forEach((n, layer) => {
    for (let index = 0; index < n; index++) {
      nodes.push({ id: `n${layer}-${index}`, layer, index, label: `L${layer + 1} unit ${index + 1}`, value: +(r() * 2 - 1).toFixed(2) });
    }
  });
  const edges: GraphEdge[] = [];
  for (let layer = 0; layer + 1 < sizes.length; layer++) {
    for (let a = 0; a < sizes[layer]; a++) {
      for (let b = 0; b < sizes[layer + 1]; b++) {
        if (r() > density) continue;
        const x = r();
        edges.push({
          from: `n${layer}-${a}`,
          to: `n${layer + 1}-${b}`,
          weight: +(r() * 2 - 1).toFixed(2),
          blocked: x < 0.08,
          flow: x > 0.6 ? +r().toFixed(2) : 0,
          owners: x > 0.85 ? ["north"] : x > 0.7 ? ["south"] : undefined,
        });
      }
    }
  }
  return { nodes, edges };
}

/** About 17 nodes and 60 edges, the size the first consumer draws. */
export const SMALL_GRAPH = layeredGraph([4, 5, 5, 3], 11, 0.8);

/** About 100 nodes and 400 edges, for the speed check. */
export const LARGE_GRAPH = layeredGraph([20, 24, 24, 20, 12], 3, 0.23);
