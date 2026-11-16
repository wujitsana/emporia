/** The data the viewer draws. Generic: it knows layers, nodes and weighted edges, nothing else. */
export interface GraphNode {
  id: string;
  /** Which layer (0-based); layers lie along one axis. */
  layer: number;
  /** Position inside the layer. */
  index: number;
  label: string;
  /** Signed activation; drives size and colour. */
  value: number;
}

export interface GraphEdge {
  from: string;
  to: string;
  /** Signed; thickness is |weight|, sign is line style and colour. */
  weight: number;
  /** Drawn dotted and faint; carries no flow. */
  blocked?: boolean;
  /** Activation flow along the edge, 0..1; moves a dash when animation is allowed. */
  flow?: number;
  /** Who pushed it, for the table. */
  owners?: readonly string[];
}

export type GraphViewMode = "3d" | "flat" | "table";

/** Extra geometry drawn through the same projection as the graph, in the graph's own 3D coordinates. The kit knows
 * nothing of what it means. */
export interface GraphDecor {
  /** Filled, translucent polygons (a shaded solid's faces). */
  faces?: readonly { pts: readonly (readonly [number, number, number])[] }[];
  lines?: readonly { a: readonly [number, number, number]; b: readonly [number, number, number]; width?: number; dashed?: boolean }[];
  /** A dot, or a ring (a target), with a short label. */
  points?: readonly { p: readonly [number, number, number]; kind: "dot" | "ring" | "corner"; label?: string }[];
}
