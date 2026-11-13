import { memo, useMemo, type KeyboardEvent, type Ref, type SVGAttributes } from "react";
import type { GraphEdge, GraphNode } from "./types";
import { depthOrder } from "./project";
import { maxAbs, nodeName, widthOf } from "./edgeStyle";
import s from "./Graph3D.module.css";

export interface Placed {
  x: number;
  y: number;
  /** Perspective scale (1 in the flat view). */
  scale: number;
  /** Larger is nearer; the paint order. */
  depth: number;
}

export interface GraphSvgProps {
  nodes: readonly GraphNode[];
  edges: readonly GraphEdge[];
  /** Aligned with `nodes`. */
  placed: readonly Placed[];
  width: number;
  height: number;
  selected?: string | null;
  onSelect?: (id: string | null) => void;
  animate: boolean;
  svgRef?: Ref<SVGSVGElement>;
  className: string;
  label: string;
  /** Pointer handlers and data attributes from the owner. */
  svgProps?: SVGAttributes<SVGSVGElement> & Record<`data-${string}`, string | number | undefined>;
  /** Labels for every node (small graphs) or only the selected one. */
  allLabels?: boolean;
  /** Layer captions for the flat view. */
  layerTags?: readonly { x: number; text: string }[];
}

/** Draws placed nodes and edges. Shared by the orbit view and the flat view, so they cannot disagree. */
function GraphSvgInner(p: GraphSvgProps) {
  const { nodes, edges, placed, selected, animate, svgRef } = p;
  const at = new Map<string, Placed>();
  nodes.forEach((n, i) => at.set(n.id, placed[i]));
  // Names walk every edge per node: compute them when the data changes, not at every turn.
  const names = useMemo(() => {
    const labels = new Map(nodes.map((n) => [n.id, n.label]));
    return nodes.map((n) => nodeName(n, edges, labels));
  }, [nodes, edges]);
  const wMax = maxAbs(edges.map((e) => e.weight));
  const vMax = maxAbs(nodes.map((n) => n.value)) || 1;

  const drawn = edges
    .map((e, i) => ({ e, i, a: at.get(e.from), b: at.get(e.to) }))
    .filter((x): x is { e: GraphEdge; i: number; a: Placed; b: Placed } => !!x.a && !!x.b);
  const edgeOrder = depthOrder(drawn.map((d) => (d.a.depth + d.b.depth) / 2));
  const nodeOrder = depthOrder(placed.map((q) => q.depth));

  const pick = (id: string) => p.onSelect?.(selected === id ? null : id);
  const onNodeKey = (ev: KeyboardEvent, id: string) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      ev.stopPropagation();
      pick(id);
    }
  };

  return (
    <svg
      ref={svgRef}
      className={p.className}
      viewBox={`0 0 ${p.width} ${p.height}`}
      role="group"
      aria-label={p.label}
      {...p.svgProps}
    >
      <g>
        {edgeOrder.map((k) => {
          const { e, i, a, b } = drawn[k];
          const sc = (a.scale + b.scale) / 2;
          const w = widthOf(e.weight, wMax) * Math.min(1.8, Math.max(0.5, sc));
          const touches = selected != null && (e.from === selected || e.to === selected);
          const dim = selected != null && !touches;
          const cls = [s.edge, e.weight < 0 ? s.neg : "", e.blocked ? s.blocked : "", dim ? s.dim : ""].filter(Boolean).join(" ");
          const flow = e.blocked ? 0 : (e.flow ?? 0);
          return (
            <g key={`${e.from}>${e.to}#${i}`} aria-hidden="true">
              <line
                className={cls}
                data-edge=""
                data-sign={e.weight < 0 ? "neg" : "pos"}
                data-blocked={e.blocked ? "1" : undefined}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                strokeWidth={w}
              />
              {flow > 0 && !dim ? (
                animate ? (
                  <line className={s.pulse} data-pulse="" x1={a.x} y1={a.y} x2={b.x} y2={b.y} strokeWidth={Math.max(1.2, w * 0.8)} />
                ) : (
                  <line className={s.flowStill} data-flow-still="" x1={a.x} y1={a.y} x2={b.x} y2={b.y} strokeWidth={w + 1 + flow * 5} />
                )
              ) : null}
            </g>
          );
        })}
      </g>
      <g aria-hidden="true">
        {nodeOrder.map((k) => {
          const n = nodes[k];
          const q = placed[k];
          const r = (3 + 7 * (Math.abs(n.value) / vMax)) * Math.min(1.8, Math.max(0.5, q.scale));
          const mix = `${Math.round(15 + 85 * (Math.abs(n.value) / vMax))}%`;
          return (
            <circle
              key={n.id}
              className={n.value < 0 ? `${s.dot} ${s.dotNeg}` : s.dot}
              style={{ "--mix": mix } as React.CSSProperties}
              data-node=""
              cx={q.x}
              cy={q.y}
              r={r}
            />
          );
        })}
      </g>
      {p.layerTags?.map((t) => (
        <text key={t.text} className={s.layerTag} x={t.x} y={p.height - 8} aria-hidden="true">
          {t.text}
        </text>
      ))}
      {/* The focusable layer keeps the data order, so Tab walks layer by layer whatever the angle. */}
      <g>
        {nodes.map((n, i) => {
          const q = placed[i];
          const r = (3 + 7 * (Math.abs(n.value) / vMax)) * Math.min(1.8, Math.max(0.5, q.scale)) + 2;
          const on = selected === n.id;
          return (
            <circle
              key={n.id}
              className={on ? `${s.hit} ${s.picked}` : s.hit}
              data-hit=""
              role="button"
              tabIndex={0}
              aria-pressed={on}
              aria-label={names[i]}
              cx={q.x}
              cy={q.y}
              r={r}
              onClick={(ev) => {
                ev.stopPropagation();
                pick(n.id);
              }}
              onKeyDown={(ev) => onNodeKey(ev, n.id)}
            />
          );
        })}
      </g>
      <g aria-hidden="true">
        {nodeOrder.map((k) => {
          const n = nodes[k];
          if (!p.allLabels && selected !== n.id) return null;
          const q = placed[k];
          return (
            <text key={n.id} className={s.label} x={q.x + 8} y={q.y - 8}>
              {n.label}
            </text>
          );
        })}
      </g>
    </svg>
  );
}

export const GraphSvg = memo(GraphSvgInner);
