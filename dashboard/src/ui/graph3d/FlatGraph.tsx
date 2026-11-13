import { useMemo } from "react";
import type { GraphEdge, GraphNode } from "./types";
import { GraphSvg } from "./GraphSvg";
import { FLAT_H, FLAT_W, flatLayout } from "./flatLayout";
import s from "./Graph3D.module.css";

export function FlatGraph(props: {
  nodes: readonly GraphNode[];
  edges: readonly GraphEdge[];
  selected?: string | null;
  onSelect?: (id: string | null) => void;
  animate: boolean;
  label?: string;
}) {
  const { placed, tags } = useMemo(() => flatLayout(props.nodes), [props.nodes]);
  return (
    <GraphSvg
      nodes={props.nodes}
      edges={props.edges}
      placed={placed}
      width={FLAT_W}
      height={FLAT_H}
      selected={props.selected}
      onSelect={props.onSelect}
      animate={props.animate}
      className={`${s.svg} ${s.flat}`}
      label={props.label ?? "Layered graph, flat view"}
      allLabels={props.nodes.length <= 24}
      layerTags={tags}
    />
  );
}
